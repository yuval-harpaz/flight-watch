#!/usr/bin/env python3
"""
flight_watch.py - early-warning monitor for irregular aircraft activity on flights
to/from an airport (default: TLV / LLBG, Ben Gurion), near AND far.

Three data layers, polled every --interval seconds:
  local     every aircraft within --radius nm of the airport
  follow    flights known to be TLV arrivals/departures, tracked anywhere in the world
            by their ICAO hex id (identified via the TLV flight board or callsign->route DB)
  discover  scheduled TLV flights (Israel Airports Authority open data, data.gov.il)
            searched globally by callsign, so inbound flights are caught long before
            they reach the local radius

Alerts:
  LOST_CONTACT      airborne aircraft silent too long (separate limit for remote flights)
  VERTICAL_RATE     |climb/descent| above --vrate ft/min (confirmed by altitude history)
  COURSE_CHANGE     track change above --turn degrees within --turn-window seconds
  EMERGENCY         squawk 7500/7600/7700 or an ADS-B emergency status
  POSITION_JUMP     physically impossible position jump (typical of GPS spoofing)
  DIVERSION         a TLV arrival lands somewhere else (incl. return to origin)
  RETURNED          a TLV departure lands back at TLV after leaving
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import re
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("flight_watch")

# IATA -> (ICAO, lat, lon)
AIRPORTS = {
    "TLV": ("LLBG", 32.0114, 34.8867),
    "ETM": ("LLER", 29.7236, 35.0114),
    "HFA": ("LLHA", 32.8094, 35.0431),
    "LCA": ("LCLK", 34.8751, 33.6249),
    "AMM": ("OJAI", 31.7226, 35.9932),
}

# readsb / ADSBExchange-v2 compatible feeds. Free, no key. Rate limits are undocumented and much
# stricter than 1 req/s from shared (cloud) IPs, hence the adaptive Budget.
# hex and callsign endpoints take comma-separated lists (verified for adsb.lol).
# airplanes.live is feeder-only now (HTTP 403 for everyone else).
PROVIDERS = {
    "airplanes.live": {"base": "https://api.airplanes.live/v2",
                       "point": "/point/{lat}/{lon}/{radius}",
                       "hex": "/hex/{ids}", "callsign": "/callsign/{ids}"},
    "adsb.lol": {"base": "https://api.adsb.lol/v2",
                 "point": "/point/{lat}/{lon}/{radius}",
                 "hex": "/hex/{ids}", "callsign": "/callsign/{ids}"},
    "adsb.fi": {"base": "https://opendata.adsb.fi/api/v2",
                "point": "/lat/{lat}/lon/{lon}/dist/{radius}",
                "hex": "/hex/{ids}", "callsign": "/callsign/{ids}"},
}
ROUTE_API = "https://api.adsb.lol/api/0/routeset"  # callsign -> route (crowd-sourced)
ROUTE_TTL = 6 * 3600
FLYDATA_URL = "https://data.gov.il/api/3/action/datastore_search"
FLYDATA_RESOURCE = "e83f763b-b7d7-479e-b172-ae981ddc6de5"  # Ben Gurion flight board

# IATA airline code -> ICAO callsign prefix, for carriers commonly seen at TLV.
# Extend with --airline-map file.json ({"XX": "XXX"}).
AIRLINE_ICAO = {
    "LY": "ELY", "6H": "ISR", "IZ": "AIZ", "5C": "ICL", "W6": "WZZ", "W4": "WMT",
    "W9": "WUK", "U2": "EZY", "EC": "EJU", "DS": "EZS", "FR": "RYR", "LH": "DLH",
    "LX": "SWR", "OS": "AUA", "SN": "BEL", "AF": "AFR", "KL": "KLM", "BA": "BAW",
    "IB": "IBE", "UX": "AEA", "VY": "VLG", "AZ": "ITY", "LO": "LOT", "TP": "TAP",
    "UA": "UAL", "DL": "DAL", "AA": "AAL", "AC": "ACA", "TK": "THY", "PC": "PGT",
    "A3": "AEE", "OA": "OAL", "CY": "CYP", "FZ": "FDB", "EK": "UAE", "EY": "ETD",
    "GF": "GFA", "J2": "AHY", "ET": "ETH", "AI": "AIC", "6E": "IGO", "HU": "CHH",
    "CA": "CCA", "RO": "ROT", "FB": "LZB", "OU": "CTN", "JU": "ASL", "9U": "MLD",
    "PS": "AUI", "B2": "BRU", "HY": "UZB", "KC": "KZR", "BT": "BTI", "EW": "EWG",
    "HV": "TRA", "TO": "TVF", "LS": "EXS", "XQ": "SXS", "BY": "TOM", "QS": "TVS",
    "MS": "MSR", "RJ": "RJA", "SU": "AFL", "4Y": "OCN", "EN": "DLA", "XC": "CAI",
    "AM": "AMX", "AR": "ARG", "MU": "CES", "QF": "QFA", "SK": "SAS", "TG": "THA",
    "UL": "ALK", "VN": "HVN", "VS": "VIR", "DE": "CFG", "BZ": "BBG", "5F": "FIA",
    "GQ": "SEH", "WZ": "RWZ",
}

ICAO_TO_IATA = {v: k for k, v in AIRLINE_ICAO.items()}

EMERGENCY_SQUAWKS = {"7500": "HIJACK", "7600": "RADIO FAILURE", "7700": "GENERAL EMERGENCY"}
AIRPORT_TRAFFIC = {"ARR", "DEP", "VIA", "ARR?", "DEP?"}
CONFIRMED = {"ARR", "DEP", "VIA"}
# ADS-B emitter categories never followed on a guess: light, rotorcraft, glider, balloon,
# parachutist, ultralight, UAV
LIGHT_CATEGORIES = {"A1", "A7", "B1", "B2", "B3", "B4", "B6"}
PRIORITY = {"EMERGENCY": 5, "LOST_CONTACT": 5, "DIVERSION": 5, "TOWARD_ISRAEL": 5, "OFF_COURSE": 5,
            "RETURNED": 4, "TURNING_BACK": 4, "VERTICAL_RATE": 4, "SHARP_TURN": 4,
            "COURSE_CHANGE": 4, "POSITION_JUMP": 3, "CONTACT_RESTORED": 2}
# kinds that describe abnormal flying; two different ones on one flight within --hot-minutes
# are reported as a pattern (priority 5)
ANOMALIES = {"EMERGENCY", "LOST_CONTACT", "DIVERSION", "TOWARD_ISRAEL", "OFF_COURSE", "TURNING_BACK",
             "VERTICAL_RATE", "SHARP_TURN", "COURSE_CHANGE", "POSITION_JUMP"}

# Airports in and around the watch area: terminal areas (normal turns, steep-but-normal climbs),
# "probably landing there" for aircraft going silent low nearby. IATA -> (lat, lon, Israeli)
REGION_AIRPORTS = {
    "TLV": (32.0114, 34.8867, True), "HFA": (32.8094, 35.0431, True), "ETM": (29.7236, 35.0114, True),
    "VDA": (29.9403, 34.9358, True), "AMM": (31.7226, 35.9932, False), "ADJ": (31.9727, 35.9916, False),
    "AQJ": (29.6116, 35.0181, False), "BEY": (33.8209, 35.4884, False), "DAM": (33.4115, 36.5156, False),
    "LCA": (34.8751, 33.6249, False), "PFO": (34.7180, 32.4857, False), "ECN": (35.1547, 33.4961, False),
    "AAC": (31.0733, 33.8358, False), "CAI": (30.1219, 31.4056, False), "SSH": (27.9773, 34.3950, False),
    "TCP": (29.5878, 34.7781, False), "TUU": (28.3654, 36.6189, False), "ULH": (26.4834, 38.1171, False),
    "AJF": (29.7851, 40.1000, False), "MED": (24.5534, 39.7051, False), "ALP": (36.1807, 37.2244, False),
    "LTK": (35.4011, 35.9487, False), "AYT": (36.8987, 30.8005, False), "ADA": (36.9822, 35.2804, False),
}
# Rough outline of Israeli-controlled airspace over land (incl. West Bank, Golan), lat/lon.
# Israeli airlines (ICAO): El Al, Israir, Arkia, CAL Cargo, Sun d'Or, Challenge Airlines IL, Air Haifa
ISRAELI_AIRLINES = {"ELY", "ISR", "AIZ", "ICL", "ERO", "CHG", "HFA"}
ISRAELI_CODES = {c for c, v in REGION_AIRPORTS.items() if v[2]} | {"LLBG", "LLHA", "LLER", "LLOV"}
ISRAEL = [(33.09, 35.10), (33.28, 35.58), (33.33, 35.82), (32.70, 35.87), (32.30, 35.57), (31.50, 35.48),
          (30.90, 35.40), (29.55, 34.98), (29.50, 34.90), (30.88, 34.40), (31.22, 34.25), (31.60, 34.48),
          (32.10, 34.77), (32.83, 34.95)]


# --------------------------------------------------------------------------- helpers
def haversine_nm(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 3440.065 * math.asin(math.sqrt(a))


def heading_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def bearing(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    return math.degrees(math.atan2(math.sin(dl) * math.cos(p2),
                                   math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) % 360


def destination(lat, lon, track, nm) -> tuple[float, float]:
    """Point `nm` nautical miles from lat/lon along `track` (great circle)."""
    d, b = nm / 3440.065, math.radians(track)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2)


def in_polygon(lat, lon, poly) -> bool:
    inside = False
    for i in range(len(poly)):
        (y1, x1), (y2, x2) = poly[i], poly[i - 1]
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def minutes_to(poly, lat, lon, track, gs, horizon_min) -> float | None:
    """Minutes until the current track enters `poly` (0 = inside), None if not within the horizon."""
    if in_polygon(lat, lon, poly):
        return 0.0
    if not gs or track is None:
        return None
    steps = max(1, int(horizon_min * 2))  # check every 30 s of flight
    for k in range(1, steps + 1):
        m = horizon_min * k / steps
        if in_polygon(*destination(lat, lon, track, gs * m / 60), poly):
            return m
    return None


def nearest_airport(lat, lon, israeli: bool | None = None, exclude=()) -> tuple[str | None, float]:
    best = (None, 1e9)
    for code, (alat, alon, isr) in REGION_AIRPORTS.items():
        if code in exclude or (israeli is not None and isr != israeli):
            continue
        d = haversine_nm(lat, lon, alat, alon)
        if d < best[1]:
            best = (code, d)
    return best


def path_angle(vrate: float, gs: float) -> float:
    """Climb (+) / descent (-) angle in degrees from ft/min and knots."""
    return math.degrees(math.atan2(vrate, gs * 101.27))


class TurnZones:
    """Grid cells where many aircraft routinely turn onto a given heading (tools/learn_turn_zones.py)."""

    def __init__(self, path: str | None, tolerance: float = 20):
        self.cells: dict[tuple, list[int]] = {}
        self.size, self.tol = 0.5, tolerance
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            log.warning("turn zones not loaded (%s) - every large course change will alert", e)
            return
        self.size = data.get("cell", 0.5)
        for z in data.get("zones", []):
            key = (math.floor(z["lat"] / self.size), math.floor(z["lon"] / self.size))
            self.cells.setdefault(key, []).append(z["out"])
        log.info("turn zones: %d learned headings in %d cells", sum(map(len, self.cells.values())),
                 len(self.cells))

    def expected(self, lat, lon, track) -> bool:
        """A turn ending on `track` here is routine (route corner / detour)."""
        if track is None:
            return False
        i, j = math.floor(lat / self.size), math.floor(lon / self.size)
        return any(heading_diff(track, out) <= self.tol
                   for di in (-1, 0, 1) for dj in (-1, 0, 1) for out in self.cells.get((i + di, j + dj), ()))


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def airline_like(t: "Track") -> bool:
    """Airline-style callsign (ELY315, WZZ5TL) rather than local GA (4XHSC, light aircraft)."""
    if t.category in LIGHT_CATEGORIES:
        return False
    cs = (t.callsign or "").upper()
    return (re.fullmatch(r"[A-Z]{3}\d[0-9A-Z]{0,3}", cs) is not None
            and cs != t.reg.replace("-", "").upper())


def external_links(t: "Track", when: float) -> dict:
    """Links to the flight on existing sites (nothing is stored locally)."""
    day = time.strftime("%Y-%m-%d", time.gmtime(when))
    links = {}
    flight = t.sched.flight if t.sched else None
    if not flight:  # callsign -> IATA flight number, e.g. FDB1073 -> FZ1073
        m = re.fullmatch(r"([A-Z]{3})0*(\d{1,4})", t.callsign or "")
        if m and m.group(1) in ICAO_TO_IATA:
            flight = ICAO_TO_IATA[m.group(1)] + m.group(2)
    if flight:
        links["fr24_flight"] = f"https://www.flightradar24.com/data/flights/{flight.lower()}"
    if t.reg:
        links["fr24_aircraft"] = f"https://www.flightradar24.com/data/aircraft/{t.reg.lower()}"
    links["live_adsbx"] = f"https://globe.adsbexchange.com/?icao={t.hex}"
    links["live"] = f"https://globe.airplanes.live/?icao={t.hex}"
    links["replay_adsbx"] = f"https://globe.adsbexchange.com/?icao={t.hex}&showTrace={day}"
    links["replay"] = f"https://globe.airplanes.live/?icao={t.hex}&showTrace={day}"
    return links


HIGH, LOW = "high", "low"   # request priority: local poll / everything that can wait


class Throttled(requests.RequestException):
    """Request not sent: the host is cooling down after HTTP 429, or the budget is spent."""


class Budget:
    """Adaptive per-host request budget (token bucket, additive increase / multiplicative decrease).

    The rate creeps up after every success and halves on HTTP 429, so it settles just under
    whatever the host (or a shared cloud IP) tolerates. HIGH requests (the local poll) are only
    held back during a 429 cooldown and may overdraw the bucket; LOW requests (follow, discovery,
    routes) need a spare token and always leave one for the next local poll.
    """

    def __init__(self, rate: float, rate_max: float, burst: float = 4, rate_min: float = 3,
                 clock=time.monotonic, rng=random.random):
        self.rate, self.rate_min, self.rate_max = rate, rate_min, rate_max  # requests/minute
        self.burst, self.tokens = burst, burst
        self.clock, self.rng = clock, rng
        self.t = clock()
        self.cool_until = 0.0
        self.strikes = 0  # consecutive 429s

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rate / 60)
        self.t = now

    def cooling(self) -> float:
        return max(0.0, self.cool_until - self.clock())

    def take(self, priority: str) -> bool:
        self._refill()
        if self.cooling() or (priority == LOW and self.tokens < 2):
            return False
        self.tokens = max(-self.burst, self.tokens - 1)
        return True

    def ok(self) -> None:
        self.strikes = 0
        self.rate = min(self.rate_max, self.rate + 0.5)

    def limited(self) -> float:
        """HTTP 429: halve the rate and cool down with jitter. Returns the cooldown in seconds."""
        self.strikes += 1
        self.rate = max(self.rate_min, self.rate / 2)
        self.tokens = min(self.tokens, 0)
        cool = min(120.0, 10.0 * 2 ** (self.strikes - 1)) * (0.5 + self.rng())
        self.cool_until = self.clock() + cool
        return cool


class Http:
    """requests.Session with a per-host minimum gap and an adaptive per-host Budget."""

    def __init__(self, min_gap: float = 1.1, rate: float = 8, rate_max: float = 60,
                 clock=time.monotonic, rng=random.random):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "flight-watch/0.3"
        self.min_gap, self.last = min_gap, {}
        self.rate, self.rate_max, self.clock, self.rng = rate, rate_max, clock, rng
        self.budgets: dict[str, Budget] = {}

    def budget(self, host: str) -> Budget:
        if host not in self.budgets:
            self.budgets[host] = Budget(self.rate, self.rate_max, clock=self.clock, rng=self.rng)
        return self.budgets[host]

    def _wait(self, host: str) -> None:
        gap = self.clock() - self.last.get(host, -1e9)
        if gap < self.min_gap:
            time.sleep(self.min_gap - gap)
        self.last[host] = self.clock()

    def request(self, method: str, url: str, priority: str = HIGH, timeout: float = 10, **kw):
        host = url.split("/")[2]
        b = self.budget(host)
        if not b.take(priority):
            raise Throttled(f"{host}: " + (f"cooling down {b.cooling():.0f}s after HTTP 429"
                                           if b.cooling() else "request budget spent"))
        self._wait(host)
        r = self.s.request(method, url, timeout=timeout, **kw)
        if r.status_code == 429:
            cool = b.limited()
            log.warning("HTTP 429 from %s - cooling down %.0fs, budget now %.1f req/min",
                        host, cool, b.rate)
        elif r.ok:
            b.ok()
        r.raise_for_status()
        return r

    def get_json(self, url: str, priority: str = HIGH, **kw):
        return self.request("GET", url, priority, **kw).json()

    def post(self, url: str, priority: str = HIGH, **kw):
        return self.request("POST", url, priority, timeout=8, **kw)


@dataclass
class Sample:
    t: float               # epoch seconds of this position
    lat: float
    lon: float
    alt: int | None        # baro altitude, ft (0 on ground)
    track: float | None    # degrees true
    gs: float | None       # knots
    vrate: int | None      # ft/min
    on_ground: bool


@dataclass
class SchedFlight:
    flight: str            # e.g. "LY001"
    direction: str         # "ARR" / "DEP"
    other: str             # IATA of the other airport
    other_name: str
    when: str              # scheduled/estimated local time
    status: str
    ts: float = 0.0        # same time as epoch seconds
    codeshares: tuple = ()         # other flight numbers on the same physical flight
    callsigns: tuple = ()          # searched first (operating carrier, best guess)
    alt_callsigns: tuple = ()      # codeshare callsigns, searched only as a fallback

    def route(self, home: str) -> str:
        return (f"{self.flight} {self.other}->{home}" if self.direction == "ARR"
                else f"{self.flight} {home}->{self.other}")


@dataclass
class Track:
    hex: str
    callsign: str = ""
    reg: str = ""
    actype: str = ""
    samples: deque = field(default_factory=lambda: deque(maxlen=120))
    last_msg: float = 0.0
    lost: bool = False
    followed: bool = False
    was_airborne: bool = False
    max_dist: float = 0.0
    sched: SchedFlight | None = None
    alerted: dict = field(default_factory=dict)      # key -> (time, severity)
    squawk: str = ""
    category: str = ""     # ADS-B emitter category, e.g. A1 = light aircraft
    last_query: float = 0.0  # server time we last asked a remote feed for this hex and got an answer
    hot_until: float = 0.0   # data time until which the flight is "hot" (alerted recently)
    lost_alerted: bool = False  # the current loss of contact was alerted (not just noted)
    israeli: bool = False    # seen low at an Israeli airport (so it is Israel traffic)

    @property
    def last(self) -> Sample | None:
        return self.samples[-1] if self.samples else None

    def label(self) -> str:
        parts = [self.callsign or self.hex.upper()]
        if self.sched and self.sched.flight != self.callsign:
            parts.append(self.sched.flight)
        parts += [p for p in (self.reg, self.actype) if p]
        return " / ".join(parts)

    def sample_ago(self, seconds: float) -> Sample | None:
        """Most recent sample at least `seconds` old (but not much older)."""
        if not self.samples:
            return None
        target = self.samples[-1].t - seconds
        for s in reversed(self.samples):
            if s.t <= target:
                return s if self.samples[-1].t - s.t <= seconds * 1.5 else None
        return None

    def computed_vrate(self, span: float = 30) -> float | None:
        now, old = self.last, self.sample_ago(span)
        if not now or not old or now.alt is None or old.alt is None or now.t == old.t:
            return None
        return (now.alt - old.alt) / ((now.t - old.t) / 60)


def parse_aircraft(ac: dict, server_now: float):
    hexid = (ac.get("hex") or "").lower()
    lat, lon = ac.get("lat"), ac.get("lon")
    if not hexid or lat is None or lon is None:
        return None
    alt_raw = ac.get("alt_baro")
    on_ground = alt_raw == "ground"
    alt = 0 if on_ground else (alt_raw if isinstance(alt_raw, (int, float)) else ac.get("alt_geom"))
    vrate = ac.get("baro_rate", ac.get("geom_rate"))
    seen = float(ac.get("seen", 0) or 0)
    seen_pos = ac.get("seen_pos")
    pos_t = server_now - float(seen_pos) if seen_pos is not None else server_now - seen
    sample = Sample(pos_t, float(lat), float(lon),
                    int(alt) if alt is not None else None,
                    ac.get("track", ac.get("true_heading")), ac.get("gs"),
                    int(vrate) if vrate is not None else None, on_ground)
    return hexid, server_now - seen, sample


# --------------------------------------------------------------------------- schedule
class Schedule:
    """Ben Gurion flight board (data.gov.il) -> expected callsigns of active flights."""

    def __init__(self, http: Http, airline_map: dict, refresh: float, window_h: float):
        self.http, self.airline_map = http, airline_map
        self.refresh, self.window = refresh, timedelta(hours=window_h)
        self.by_callsign: dict[str, SchedFlight] = {}
        self.flights: list[SchedFlight] = []
        self.fetched = -1e9
        self.tz = ZoneInfo("Asia/Jerusalem")

    def fetch_records(self) -> list[dict]:
        """All board rows, paging until `total` (one page of 3000 is not always enough)."""
        records: list[dict] = []
        for _ in range(20):
            data = self.http.get_json(FLYDATA_URL, params={"resource_id": FLYDATA_RESOURCE,
                                                           "limit": 3000, "offset": len(records)})
            page = data["result"]["records"]
            records += page
            if not page or len(records) >= int(data["result"].get("total") or 0):
                break
        return records

    def refresh_if_due(self) -> None:
        if time.monotonic() - self.fetched < self.refresh:
            return
        self.fetched = time.monotonic()
        try:
            records = self.fetch_records()
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            log.warning("flight board fetch failed (%s) - keeping %d known flights",
                        e, len(self.flights))
            return
        self.load(records, datetime.now(self.tz))

    def load(self, records: list[dict], now: datetime) -> None:
        # The board has one row per marketing flight number. Rows sharing direction, scheduled
        # time and other airport are one physical flight (e.g. LY25 / DL7441).
        slots: dict[tuple, list[dict]] = {}
        for r in records:
            key = (str(r.get("CHAORD", "")).upper()[:1], r.get("CHSTOL"), r.get("CHLOC1"))
            slots.setdefault(key, []).append(r)
        out, flights, unmapped = {}, [], set()
        for rows in slots.values():
            r = rows[0]
            status = str(r.get("CHRMINE") or "").upper()
            direction = "ARR" if str(r.get("CHAORD", "")).upper().startswith("A") else "DEP"
            if "CANCEL" in status or (direction == "ARR" and status == "LANDED"):
                continue
            try:
                when = datetime.fromisoformat(str(r.get("CHPTOL") or r.get("CHSTOL")))
                when = when if when.tzinfo else when.replace(tzinfo=self.tz)
            except ValueError:
                continue
            if not (now - self.window <= when <= now + self.window):
                continue
            numbers = []  # (iata, num, icao)
            for row in rows:
                iata = str(row.get("CHOPER") or "").strip().upper()
                num = str(row.get("CHFLTN") or "").strip().lstrip("0") or "0"
                icao = self.airline_map.get(iata) or (iata if len(iata) == 3 else None)
                if icao:
                    numbers.append((iata, num, icao))
                else:
                    unmapped.add(iata)
            if not numbers:
                continue
            # Codeshare numbers are usually long (DL7441, SK3161, LY9613): the lowest number
            # is the best guess for the operating carrier, whose callsign the aircraft sends.
            numbers.sort(key=lambda n: (len(n[1]), n[1]))

            def variants(n):  # ELY1 / ELY001
                return tuple(dict.fromkeys((f"{n[2]}{n[1]}", f"{n[2]}{n[1].zfill(3)}")))
            sf = SchedFlight(f"{numbers[0][0]}{numbers[0][1]}", direction, str(r.get("CHLOC1") or "?"),
                             str(r.get("CHLOC1D") or ""), when.strftime("%Y-%m-%d %H:%M"), status,
                             when.timestamp(),
                             codeshares=tuple(f"{n[0]}{n[1]}" for n in numbers[1:]),
                             callsigns=variants(numbers[0]),
                             alt_callsigns=tuple(cs for n in numbers[1:] for cs in variants(n)))
            flights.append(sf)
            for cs in sf.callsigns + sf.alt_callsigns:
                out[cs] = sf
        self.by_callsign, self.flights = out, flights
        log.info("flight board: %d active flights (%d with codeshares, %d callsign variants)%s",
                 len(flights), sum(bool(f.codeshares) for f in flights), len(out),
                 f"; unmapped airlines: {', '.join(sorted(unmapped))}" if unmapped else "")

    def search_order(self, now: float, exclude: set, departed_max_h: float) -> list[str]:
        """Callsigns worth a global search, most likely airborne first.

        Arrivals due within -1..+6 h and departures that left within 3 h come first. Departures
        not yet gone are on the ground at TLV (the local poll sees them) and departures older than
        `departed_max_h` have landed or are long since followed, so both are skipped. Codeshare
        callsigns of the first group go last, as a fallback for a wrong operator guess.
        """
        ranked = []
        for sf in self.flights:
            if id(sf) in exclude:
                continue
            dt = sf.ts - now
            if sf.direction == "ARR":
                tier = 0 if -3600 <= dt <= 6 * 3600 else 1
            elif dt > 0 and sf.status != "DEPARTED":
                continue
            elif -dt > departed_max_h * 3600:
                continue
            else:
                tier = 0 if -dt <= 3 * 3600 else 1
            ranked.append((tier, abs(dt), sf))
        ranked.sort(key=lambda x: x[:2])
        out = [cs for _, _, sf in ranked for cs in sf.callsigns]
        out += [cs for tier, _, sf in ranked if tier == 0 for cs in sf.alt_callsigns]
        return list(dict.fromkeys(out))


# --------------------------------------------------------------------------- notifier
class Notifier:
    def __init__(self, args, http: Http):
        self.args, self.http = args, http
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.tg_chat = os.getenv("TELEGRAM_CHAT_ID")

    LINK_NAMES = {"fr24_flight": "FR24 flight", "fr24_aircraft": "FR24 aircraft",
                  "live_adsbx": "Live (ADSBx)", "live": "Live (airplanes.live)",
                  "replay_adsbx": "Replay (ADSBx)", "replay": "Replay (airplanes.live)"}

    def send(self, rec: dict) -> None:
        where = " [REMOTE]" if rec["remote"] else ""
        text = (f"[{rec['kind']}]{where} {rec['aircraft']} {rec['traffic']}"
                f"{' ' + rec['route'] if rec['route'] else ''} - {rec['message']}")
        log.warning("%s | %s", text, rec["links"]["live_adsbx"])
        if self.args.jsonl:
            with open(self.args.jsonl, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        links = rec["links"]
        link_lines = "\n".join(f"{self.LINK_NAMES[k]}: {v}" for k, v in links.items())
        main = links["live_adsbx"]  # tapping an alert shows the aircraft right now
        if self.args.ntfy_topic:
            actions = "; ".join(f"view, {self.LINK_NAMES[k]}, {links[k]}"
                                for k in ("live_adsbx", "replay_adsbx", "fr24_flight") if k in links)
            self._post(f"{self.args.ntfy_server.rstrip('/')}/{self.args.ntfy_topic}",
                       data=text.encode(),
                       headers={"Title": f"{rec['kind']} {rec['aircraft']}",
                                "Priority": str(rec["priority"]), "Tags": "airplane",
                                "Click": main, "Actions": actions})
        if self.tg_token and self.tg_chat:
            self._post(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                       json={"chat_id": self.tg_chat, "text": f"{text}\n{link_lines}",
                             "disable_web_page_preview": True})

    def _post(self, url, **kw):
        try:
            self.http.post(url, **kw)
        except requests.RequestException as e:
            log.error("notification failed (%s): %s", url.split("/")[2], e)


# --------------------------------------------------------------------------- monitor
class Monitor:
    def __init__(self, args, notifier: Notifier, http: Http, schedule: Schedule | None):
        self.args, self.notifier, self.http, self.schedule = args, notifier, http, schedule
        self.clock = time.monotonic  # cadence (tests and replays substitute their own clocks)
        self.wall = time.time
        self.tracks: dict[str, Track] = {}
        self.routes: dict[str, tuple[float, str | None]] = {}
        self.route_failures = 0
        self.routes_off = args.no_routes
        self.last_ok: float | None = None
        self.last_count = 0
        self.stats = {}
        self.multi: dict[tuple, bool] = {}   # (provider, kind) -> accepts comma-separated ids
        self.cursor: dict[str, int] = {}     # follow rotation per provider (when > 1 request)
        self.disabled: set[str] = set()      # providers that refused us (401/403)
        self.last_follow = -1e9
        self.started = self.clock()
        self.starved_warned = -1e9
        self.zones = TurnZones(args.turn_zones)
        self.protect = args.protect == "israel"
        self.disc_queue: dict[str, list[str]] = {}  # discovery round in progress, per provider
        self.disc_round = -1e9

    # ---- remote queries
    def multi_supported(self, prov: str, kind: str, local: list) -> bool | None:
        """Probe once whether `prov` accepts several ids per request, using 2 live local aircraft.

        Only a clean answer is cached: a 429 or other error says nothing about the API, so the
        probe is retried later (None = not known yet, skip this layer for now)."""
        key = (prov, kind)
        if key in self.multi:
            return self.multi[key]
        vals = []
        for ac in local:
            v = (ac.get("hex") or "").lower() if kind == "hex" else (ac.get("flight") or "").strip()
            if v and v not in vals:
                vals.append(v)
            if len(vals) == 2:
                break
        if len(vals) < 2:
            return True  # nothing to probe with; these readsb APIs normally take lists
        try:
            _, acs = self.fetch(kind, prov, LOW, ids=",".join(vals))
        except requests.HTTPError as e:
            self.note_refusal(prov, e)
            log.debug("%s %s probe failed (%s) - will retry", prov, kind, e)
            return None
        except (requests.RequestException, ValueError) as e:
            log.debug("%s %s probe not sent/failed (%s) - will retry", prov, kind, e)
            return None
        got = {(x.get("hex") or "").lower() if kind == "hex" else (x.get("flight") or "").strip()
               for x in acs}
        ok = all(v in got for v in vals)
        self.multi[key] = ok
        log.info("%s %s lookups: %s", prov, kind,
                 "many ids per request" if ok else "one id per request")
        return ok

    def note_refusal(self, prov: str, e: requests.HTTPError) -> None:
        code = e.response.status_code if e.response is not None else 0
        if code in (401, 403) and prov not in self.disabled:
            self.disabled.add(prov)
            log.error("%s refused access (HTTP %s) - dropping it for this run", prov, code)

    def remote_query(self, prov: str, kind: str, ids: list[str], local: list,
                     max_requests: int) -> tuple[list, list]:
        """Look up ids (in order) on one provider, while its budget allows.

        Returns (batches, done): [(server_now, aircraft)] and [(server_now, ids answered)].
        Stops at the first 429, refusal or spent budget; the caller resumes later."""
        if not ids or prov in self.disabled:
            return [], []
        multi = self.multi_supported(prov, kind, local)
        if multi is None:
            return [], []
        size = (150 if kind == "hex" else 120) if multi else 1
        out, done = [], []
        for g in list(chunks(ids, size))[:max_requests]:
            try:
                now_r, acs = self.fetch(kind, prov, LOW, ids=",".join(g))
            except Throttled as e:
                log.debug("%s %s lookup deferred: %s", prov, kind, e)
                break
            except requests.HTTPError as e:
                self.note_refusal(prov, e)
                if prov not in self.disabled and (e.response is None or e.response.status_code != 429):
                    log.error("%s query failed (%s): %s", kind, prov, e)
                break
            except (requests.RequestException, ValueError) as e:
                log.error("%s query failed (%s): %s", kind, prov, e)
                break
            out += [(now_r, ac) for ac in acs]
            done.append((now_r, g))
        return out, done

    def follow(self, local_hexes: set, local: list) -> list:
        """One combined hex request per --follow-interval for followed flights not in the local circle.

        Hot flights (alerted within --hot-minutes) are asked for every cycle, so the critical
        minutes of an incident are seen at the local poll's resolution."""
        a = self.args
        ids = [h for h, t in self.tracks.items() if t.followed and h not in local_hexes][:a.follow_max]
        hot = [h for h in ids if self.hot(self.tracks[h])]
        due = self.clock() - self.last_follow >= a.follow_interval
        if not ids or not (due or hot):
            return []
        batches, sent = [], False
        for prov in a.remote_providers:
            if due:
                rest = [h for h in ids if h not in hot]
                start = self.cursor.get(prov, 0) % len(rest) if rest else 0
                order = hot + rest[start:] + rest[:start]
            else:
                order = hot
            got, done = self.remote_query(prov, "hex", order, local, a.remote_budget)
            batches += got
            for now_r, g in done:
                sent = True
                self.cursor[prov] = self.cursor.get(prov, 0) + len([h for h in g if h not in hot])
                for h in g:
                    if h in self.tracks:
                        self.tracks[h].last_query = max(self.tracks[h].last_query, now_r)
        if sent and due:
            self.last_follow = self.clock()
        elif due and not sent and (self.clock() - max(self.last_follow, self.starved_warned) > 10 * a.follow_interval
              and self.clock() - self.started > 10 * a.follow_interval):
            self.starved_warned = self.clock()
            log.warning("follow requests starved for %.0fs: the feed allows only ~%.1f req/min "
                        "from this IP and the local poll comes first",
                        self.clock() - max(self.last_follow, self.started),
                        self.http.budget(PROVIDERS[a.remote_providers[0]]["base"].split("/")[2]).rate)
        return batches

    def discover(self, local: list) -> list:
        """Search scheduled flights globally by callsign.

        A round (every --discovery-interval) queues the whole search list per provider, and is
        worked off as fast as the request budget allows: in one cycle with a generous budget,
        over several cycles after 429s."""
        a = self.args
        if not self.schedule:
            return []
        if not any(self.disc_queue.values()):
            if self.clock() - self.disc_round < a.discovery_interval:
                return []
            exclude = {id(t.sched) for t in self.tracks.values() if t.followed and t.sched}
            wanted = self.schedule.search_order(self.wall(), exclude, a.departed_max_h)
            self.disc_round = self.clock()
            self.disc_queue = {p: list(wanted) for p in a.remote_providers if p not in self.disabled}
            log.debug("discovery round: %d callsigns", len(wanted))
        batches = []
        for prov, queue in self.disc_queue.items():
            if prov in self.disabled:
                queue.clear()
                continue
            got, done = self.remote_query(prov, "callsign", queue, local, a.remote_budget)
            batches += got
            del queue[:sum(len(g) for _, g in done)]
        return batches

    # ---- data
    def fetch(self, kind: str, provider: str | None = None, priority: str = HIGH, **fmt):
        p = PROVIDERS[provider or self.args.provider]
        data = self.http.get_json(p["base"] + p[kind].format(**fmt), priority)
        now = data.get("now")
        server_now = now / 1000 if now and now > 1e11 else (now or self.wall())
        return float(server_now), data.get("ac") or data.get("aircraft") or []

    def lookup_routes(self, tracks: list[Track]) -> None:
        if self.routes_off:
            return
        now = self.wall()
        need = [t for t in tracks if t.callsign and not t.sched and (
            t.callsign not in self.routes or now - self.routes[t.callsign][0] > ROUTE_TTL)][:100]
        if not need:
            return
        got = {}
        try:
            r = self.http.post(ROUTE_API, LOW, json={"planes": [
                {"callsign": t.callsign, "lat": t.last.lat, "lng": t.last.lon} for t in need]})
            for item in r.json():
                cs = (item.get("callsign") or "").strip()
                codes = item.get("_airport_codes_iata") or item.get("airport_codes")
                if cs and codes and codes.lower() != "unknown":
                    got[cs] = codes.upper()
            self.route_failures = 0
        except Throttled:
            return  # try again next cycle
        except (requests.RequestException, ValueError, AttributeError) as e:
            self.route_failures += 1
            log.debug("route lookup failed: %s", e)
            if self.route_failures >= 3:
                self.routes_off = True
                log.info("route lookups failed %d times in a row - disabled for this run "
                         "(flight board and heuristics still classify traffic)", self.route_failures)
            return
        for t in need:
            self.routes[t.callsign] = (now, got.get(t.callsign))

    def dist(self, s: Sample) -> float:
        return haversine_nm(self.args.lat, self.args.lon, s.lat, s.lon)

    def classify(self, t: Track) -> tuple[str, str | None]:
        a = self.args
        if t.sched:
            return t.sched.direction, t.sched.route(a.airport)
        route = self.routes.get(t.callsign, (0, None))[1]
        codes = {a.airport, a.icao}
        if route:
            parts = route.split("-")
            if parts[-1] in codes:
                return "ARR", route
            if parts[0] in codes:
                return "DEP", route
            return ("VIA" if codes & set(parts) else "OVR"), route
        s = t.last  # no route known: crude heuristic near the airport
        if s and s.vrate is not None and s.alt is not None and s.alt < 15000 and self.dist(s) < 40:
            if s.vrate < -300:
                return "ARR?", None
            if s.vrate > 300:
                return "DEP?", None
        return "?", None

    # ---- main step
    def poll(self) -> None:
        a = self.args
        if self.schedule:
            self.schedule.refresh_if_due()

        # 1) local circle every cycle, top priority - failure here aborts the cycle
        server_now, local = self.fetch("point", priority=HIGH, lat=a.lat, lon=a.lon,
                                       radius=int(a.radius))
        batches = [(server_now, ac) for ac in local]
        local_hexes = {(ac.get("hex") or "").lower() for ac in local}

        # 2) follow known TLV flights wherever they are (own, slower cadence)
        # 3) discover scheduled flights not seen yet (searched globally by callsign)
        # Remote layers may ask several networks: coverage differs a lot outside Israel.
        batches += self.follow(local_hexes, local)
        batches += self.discover(local)

        # merge (same aircraft may come from several queries) and update tracks
        latest: dict[str, tuple] = {}
        for now_r, ac in batches:
            parsed = parse_aircraft(ac, now_r)
            if parsed and (parsed[0] not in latest or parsed[1] > latest[parsed[0]][0][1]):
                latest[parsed[0]] = (parsed, ac)

        updated: list[tuple[Track, Sample | None, dict]] = []
        for (hexid, msg_t, sample), ac in latest.values():
            t = self.tracks.setdefault(hexid, Track(hexid))
            t.callsign = (ac.get("flight") or t.callsign).strip()
            t.reg = ac.get("r") or t.reg
            t.actype = ac.get("t") or t.actype
            t.category = ac.get("category") or t.category
            t.last_msg = max(t.last_msg, msg_t)
            if self.schedule and t.callsign in self.schedule.by_callsign:
                t.sched = self.schedule.by_callsign[t.callsign]
            prev = t.last
            if prev is None or sample.t > prev.t + 0.5:
                t.samples.append(sample)
            updated.append((t, prev, ac))

        self.lookup_routes([t for t, _, _ in updated])

        for t, prev, ac in updated:
            s = t.last
            d = self.dist(s)
            kind, route = self.classify(t)
            if not t.followed and (kind in CONFIRMED or (kind == "DEP?" and d < 15 and airline_like(t))):
                t.followed = True
                log.info("following %s (%s %s)", t.label(), kind, route or "")
            if t.lost and server_now - t.last_msg < a.lost_after:
                t.lost = False
                if t.lost_alerted:
                    self.alert(t, "CONTACT_RESTORED", f"heard again at {s.alt} ft")
            if (s.on_ground or (s.alt or 0) < 3000) and nearest_airport(s.lat, s.lon, israeli=True)[1] < 8:
                t.israeli = True
            self.check_emergency(t, ac)
            self.check_vrate(t)
            self.check_turn(t)
            self.check_sharp_turn(t)
            self.check_jump(t, prev)
            self.check_landing(t, kind, d)
            self.check_destination(t, kind, d)
            self.check_protected(t, kind, route)
            if not s.on_ground:
                t.was_airborne = True
            t.max_dist = max(t.max_dist, d)

        # Don't declare mass "lost contact" after our own outage or a feed glitch.
        gap_ok = self.last_ok is not None and server_now - self.last_ok < 3 * a.interval + 15
        feed_ok = not (self.last_count > 10 and len(local) < 0.5 * self.last_count)
        if not feed_ok:
            log.warning("feed returned %d aircraft (was %d) - skipping lost-contact check",
                        len(local), self.last_count)
        if gap_ok and feed_ok:
            self.check_lost(server_now)

        self.last_ok, self.last_count = server_now, len(local)
        self.tracks = {h: t for h, t in self.tracks.items()
                       if server_now - t.last_msg < (3 * 3600 if t.followed else 1800)}
        b = self.http.budget(PROVIDERS[a.provider]["base"].split("/")[2])
        self.stats = {"local": len(local), "followed": sum(t.followed for t in self.tracks.values()),
                      "scheduled": len(self.schedule.flights) if self.schedule else 0,
                      "rate": b.rate}

    # ---- checks
    def check_emergency(self, t: Track, ac: dict) -> None:
        sq = str(ac.get("squawk") or "")
        em = ac.get("emergency")
        changed, t.squawk = sq != t.squawk, sq or t.squawk
        if sq in EMERGENCY_SQUAWKS:
            if changed:  # alert when the code is set or changes, not every cooldown
                self.alert(t, "EMERGENCY", f"squawk {sq} ({EMERGENCY_SQUAWKS[sq]})",
                           key=f"EMERGENCY:{sq}", force=True)
        elif em and em != "none":
            self.alert(t, "EMERGENCY", f"ADS-B emergency status: {em}")

    def check_vrate(self, t: Track) -> None:
        """Too steep a climb or descent, as a flight-path angle (rate alone flags every fast jet).

        Terminal areas allow steeper climbs (light jets, initial climb), but are still checked;
        an extreme rate (--vrate) alerts whatever the speed."""
        a, s = self.args, t.last
        if s.on_ground or s.vrate is None or s.alt is None or s.alt < 1000:
            return
        angle = path_angle(s.vrate, s.gs) if s.gs and s.gs >= 60 else None
        apt, ad = nearest_airport(s.lat, s.lon)
        terminal = ad <= a.terminal_radius and s.alt < a.terminal_alt
        limit = (a.terminal_climb_angle if terminal else a.climb_angle) if s.vrate > 0 else a.descent_angle
        if not ((angle is not None and abs(angle) >= limit) or abs(s.vrate) >= a.vrate):
            return
        computed = t.computed_vrate(30)
        if computed is not None:
            c_angle = path_angle(computed, s.gs) if s.gs and s.gs >= 60 else 0.0
            if abs(computed) < 0.5 * a.vrate and abs(c_angle) < 0.5 * limit:
                return  # reported rate not backed by actual altitude change -> likely a glitch
        direction = "DESCENT" if s.vrate < 0 else "CLIMB"
        extra = f" (history: {computed:+.0f} ft/min)" if computed is not None else ""
        ang = f", {angle:+.1f} deg" if angle is not None else ""
        near = f", {ad:.0f} nm from {apt}" if terminal else ""
        self.alert(t, "VERTICAL_RATE", f"{direction} {s.vrate:+d} ft/min{ang} at {s.alt} ft{near}{extra}",
                   key=f"VERTICAL_RATE:{direction}", severity=abs(s.vrate))

    def check_turn(self, t: Track) -> None:
        """Large course change (e.g. a U-turn). Routine turns are skipped: in terminal areas and at
        learned route corners (--turn-zones). A flight that already alerted is always reported."""
        a, s = self.args, t.last
        if (s.on_ground or s.track is None or (s.alt or 0) < a.turn_min_alt
                or self.dist(s) < a.turn_ignore_radius):
            return
        old = t.sample_ago(a.turn_window)
        if not old or old.track is None:
            return
        d = heading_diff(s.track, old.track)
        if d < a.turn:
            return
        if not self.hot(t):
            apt, ad = nearest_airport(s.lat, s.lon)
            if ad <= a.terminal_radius + 5 and s.alt < 25000:
                return  # departure / arrival routing
            if self.zones.expected(s.lat, s.lon, s.track):
                return  # route corner seen on many flights
        self.alert(t, "COURSE_CHANGE", f"track {old.track:.0f} -> {s.track:.0f} deg "
                   f"({d:.0f} deg in {s.t - old.t:.0f}s) at {s.alt} ft")

    def check_sharp_turn(self, t: Track) -> None:
        """Turn tighter than airliners fly (implied bank angle), anywhere - also near airports."""
        a, s = self.args, t.last
        if s.on_ground or s.track is None or not s.gs or s.gs < 150 or (s.alt or 0) < 1500:
            return
        old = t.sample_ago(25)
        if not old or old.track is None or not old.gs:
            return
        dt = s.t - old.t
        dtrk = heading_diff(s.track, old.track)
        if dt <= 0 or dtrk < 20:
            return
        # positions must agree with the speeds, or the "turn" is a GNSS/position glitch
        moved = haversine_nm(old.lat, old.lon, s.lat, s.lon)
        expect = (s.gs + old.gs) / 2 * dt / 3600
        if not 0.6 <= moved / expect <= 1.4:
            return
        bank = math.degrees(math.atan(math.radians(dtrk) / dt * s.gs * 0.5144 / 9.81))
        if bank >= a.max_bank:
            self.alert(t, "SHARP_TURN", f"{old.track:.0f} -> {s.track:.0f} deg in {dt:.0f}s at "
                       f"{s.gs:.0f} kt (~{bank:.0f} deg bank) at {s.alt} ft", severity=bank)

    def check_jump(self, t: Track, prev: Sample | None) -> None:
        s = t.last
        if prev is None or s is prev:
            return
        dt = s.t - prev.t
        d = haversine_nm(prev.lat, prev.lon, s.lat, s.lon)
        if dt <= 0 or dt > 120 or d < 2:  # long gaps: it just moved while unheard
            return
        kt = d / (dt / 3600)
        if kt > self.args.max_speed:
            self.alert(t, "POSITION_JUMP", f"{d:.1f} nm in {dt:.0f}s (~{kt:.0f} kt) - "
                       "possible GPS spoofing or bad data")

    def check_landing(self, t: Track, kind: str, d: float) -> None:
        s = t.last
        if not (s.on_ground and t.was_airborne):
            return
        if kind == "ARR" and d > 15:
            apt, ad = nearest_airport(s.lat, s.lon)
            at = f" ({ad:.0f} nm from {apt})" if ad < 20 else ""
            self.alert(t, "DIVERSION", f"{self.args.airport} arrival on the ground "
                       f"{d:.0f} nm away at {s.lat:.4f},{s.lon:.4f}{at}")
        elif kind == "DEP" and d < 15 and t.max_dist > 40:
            self.alert(t, "RETURNED", f"{self.args.airport} departure landed back at "
                       f"{self.args.airport} (had reached {t.max_dist:.0f} nm)")

    def check_destination(self, t: Track, kind: str, d: float) -> None:
        """Long before landing: an arrival flying away from the airport, or a departure flying back.

        The deviation must hold over --course-hold seconds while the distance opens (arrival) or
        closes (departure); route bends on the way (e.g. via Saudi Arabia and Jordan) stay below it."""
        a, s = self.args, t.last
        if (kind not in ("ARR", "DEP") or s.on_ground or s.track is None or (s.alt or 0) < 5000
                or d < a.off_course_min_dist):
            return
        old = t.sample_ago(a.course_hold)
        if not old:
            return
        window = [x for x in t.samples if x.t >= old.t and x.track is not None]
        if len(window) < 3:
            return
        devs = [heading_diff(x.track, bearing(x.lat, x.lon, a.lat, a.lon)) for x in window]
        was = haversine_nm(a.lat, a.lon, old.lat, old.lon)
        if kind == "ARR" and min(devs) >= a.off_course and d > was + 3:
            self.alert(t, "OFF_COURSE", f"{a.airport} arrival heading {s.track:.0f} deg, "
                       f"{devs[-1]:.0f} deg away from {a.airport} for {s.t - old.t:.0f}s, "
                       f"{d:.0f} nm out and opening ({was:.0f} -> {d:.0f} nm)")
        elif kind == "DEP" and max(devs) <= a.turn_back and d < was - 5:
            self.alert(t, "TURNING_BACK", f"{a.airport} departure heading back to {a.airport} "
                       f"(track {s.track:.0f} deg) for {s.t - old.t:.0f}s, {d:.0f} nm out and "
                       f"closing ({was:.0f} -> {d:.0f} nm)")

    def arrival_guess(self, t: Track) -> bool:
        """Looks like an Israeli-airport arrival the board/route DB did not identify."""
        s = t.last
        apt, ad = nearest_airport(s.lat, s.lon, israeli=True)
        alat, alon, _ = REGION_AIRPORTS[apt]
        return (ad < 150 and heading_diff(s.track, bearing(s.lat, s.lon, alat, alon)) <= 20
                and ((s.vrate or 0) <= -300 or (s.alt or 0) < 20000))

    def check_protected(self, t: Track, kind: str, route: str | None) -> None:
        """A flight not bound for Israel turning toward it, or about to enter its airspace."""
        a, s = self.args, t.last
        if (not self.protect or s.on_ground or (s.alt or 0) < a.toward_min_alt or not s.gs
                or s.gs < 150 or s.track is None):
            return
        if (kind in CONFIRMED or t.sched or t.israeli or t.reg.upper().startswith("4X")
                or t.callsign[:3] in ISRAELI_AIRLINES
                or (route and ISRAELI_CODES & set(route.split("-")))
                or (s.alt < 15000 and nearest_airport(s.lat, s.lon, israeli=True)[1] < 60)):
            return  # Israel traffic (incl. low near an Israeli airport: just left / about to land)
        eta = minutes_to(ISRAEL, s.lat, s.lon, s.track, s.gs, a.toward_eta)
        if eta is None:
            return
        apt, ad = nearest_airport(s.lat, s.lon, israeli=False)
        if ad < 40 and (s.vrate or 0) < -300:
            return  # descending into its own (non-Israeli) airport
        prev = t.samples[-2] if len(t.samples) > 1 else None
        if (not prev or prev.track is None
                or minutes_to(ISRAEL, prev.lat, prev.lon, prev.track, prev.gs or s.gs, a.toward_eta) is None):
            return  # must point there on two samples in a row
        old = t.sample_ago(240)
        turned = (old is not None and old.track is not None
                  and heading_diff(s.track, old.track) >= a.toward_turn
                  and minutes_to(ISRAEL, old.lat, old.lon, old.track, old.gs or s.gs, 30) is None)
        what = f"not bound for Israel ({kind}{' ' + route if route else ''})"
        if turned and not self.zones.expected(s.lat, s.lon, s.track):
            msg = (f"{what} - turned {old.track:.0f} -> {s.track:.0f} deg toward Israeli airspace, "
                   f"~{eta:.0f} min away, at {s.alt} ft")
        elif eta <= 3 and kind != "OVR" and not self.arrival_guess(t):
            msg = (f"{what} - {'over' if eta == 0 else 'about to enter'} Israeli airspace"
                   f"{'' if eta == 0 else f' in ~{eta:.0f} min'}, track {s.track:.0f} deg at {s.alt} ft")
        else:
            return
        self.alert(t, "TOWARD_ISRAEL", msg)

    def check_lost(self, now: float) -> None:
        a = self.args
        for t in self.tracks.values():
            s = t.last
            if t.lost or not s or s.on_ground or len(t.samples) < 3:
                continue
            d = self.dist(s)
            remote = d > a.radius - a.edge_margin
            # Silence only counts up to the last time someone actually answered for this aircraft:
            # the local poll (this cycle) inside the circle, the last follow request outside it.
            # A throttled or failed follow therefore never turns into a false LOST_CONTACT.
            silent = (t.last_query if remote else now) - t.last_msg
            kind = self.classify(t)[0]
            apt, ad = nearest_airport(s.lat, s.lon, exclude=(a.airport,))
            if (s.alt or 0) < a.lost_min_alt:
                # A TLV arrival vanishing low and far from TLV is most likely landing elsewhere
                # (low-altitude coverage is poor, so we may never see it on the ground).
                if (t.followed and d > 30 and (s.vrate or 0) <= 0
                        and kind == "ARR" and silent >= 3 * a.lost_after):
                    t.lost = t.lost_alerted = True
                    near = f", {ad:.0f} nm from {apt}" if ad <= 30 else ""
                    self.alert(t, "DIVERSION", f"{a.airport} arrival went silent descending through "
                               f"{s.alt} ft, {d:.0f} nm from {a.airport} at {s.lat:.4f},{s.lon:.4f}"
                               f"{near} - probably landing {'at ' + apt if near else 'elsewhere'}")
                continue
            if remote:
                if not t.followed:
                    continue  # unfollowed aircraft simply leaving the local circle
                limit = a.lost_after_remote
            else:
                if d < a.lost_ignore_radius:
                    continue  # landing at the airport
                limit = a.lost_after
            if silent < limit:
                continue
            t.lost, t.lost_alerted = True, False
            trk = f"{s.track:.0f}" if s.track is not None else "?"
            where = (f"last {s.alt} ft, {d:.0f} nm from {a.airport}, track {trk} deg, "
                     f"at {s.lat:.4f},{s.lon:.4f}")
            descending = (s.vrate or 0) <= 0
            if (s.alt or 0) < 12000 and descending and ad <= 30:
                if kind == "ARR":  # a TLV arrival descending into another airport
                    t.lost_alerted = True
                    self.alert(t, "DIVERSION", f"{a.airport} arrival went silent {silent:.0f}s, "
                               f"{where}, {ad:.0f} nm from {apt} - probably landing at {apt}")
                else:
                    log.info("%s silent %.0fs descending near %s - probably landing there",
                             t.label(), silent, apt)
                continue
            if remote and not self.hot(t) and (kind == "DEP" and descending
                                               or (s.vrate or 0) > -500 and (s.alt or 0) >= 20000):
                # cruising far away (or a departure descending into its destination): coverage gap
                log.info("%s silent %.0fs, %s - remote coverage gap, not alerted",
                         t.label(), silent, where)
                continue
            t.lost_alerted = True
            self.alert(t, "LOST_CONTACT", f"silent {silent:.0f}s; {where}")

    # ---- output
    def hot(self, t: Track) -> bool:
        """Alerted within --hot-minutes (data time): followed every cycle, every anomaly reported."""
        return bool(t.last) and t.last.t < t.hot_until

    def alert(self, t: Track, kind: str, msg: str, key: str | None = None,
              severity: float = 0, force: bool = False) -> None:
        """Cooldown per key; a clearly worse reading (1.5x severity) or force bypasses it."""
        a = self.args
        now = t.last.t  # time of the data, not of the poll
        key = key or kind
        last_t, last_sev = t.alerted.get(key, (-1e12, 0))
        escalated = severity and severity >= 1.5 * last_sev
        if not (force or escalated or kind == "CONTACT_RESTORED") and now - last_t < a.cooldown:
            return
        traffic, route = self.classify(t)
        if a.airport_traffic_only and traffic not in AIRPORT_TRAFFIC:
            return
        recent = sorted({k.split(":")[0] for k, (tt, _) in t.alerted.items()
                         if now - tt <= a.hot_minutes * 60 and k.split(":")[0] in ANOMALIES} - {kind})
        pattern = kind in ANOMALIES and bool(recent)
        t.alerted[key] = (now, max(severity, last_sev if now - last_t < a.cooldown else 0))
        if not t.followed:
            log.info("following %s (alerted)", t.label())
        t.followed = True  # keep its fate visible wherever it goes
        if kind in ANOMALIES:
            t.hot_until = max(t.hot_until, now + a.hot_minutes * 60)
        s = t.last
        d = self.dist(s)
        apt, ad = nearest_airport(s.lat, s.lon)
        self.notifier.send({
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "kind": kind, "priority": 5 if pattern else PRIORITY.get(kind, 3),
            "message": msg + (f" [also: {', '.join(recent)}]" if pattern else ""),
            "hex": t.hex, "aircraft": t.label(), "callsign": t.callsign,
            "traffic": traffic, "route": route, "remote": d > a.radius,
            "scheduled": t.sched.when if t.sched else None,
            "lat": s.lat, "lon": s.lon, "alt": s.alt, "track": s.track, "gs": s.gs,
            "dist_nm": round(d, 1), "near": f"{apt} {ad:.0f} nm",
            "recent": recent, "links": external_links(t, now),
        })


# --------------------------------------------------------------------------- cli
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Alert on irregular aircraft activity for an airport.",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    g = p.add_argument_group("area")
    g.add_argument("--airport", default="TLV", help="IATA code (known: %s)" % ", ".join(AIRPORTS))
    g.add_argument("--icao", help="ICAO code (needed with --lat/--lon for unknown airports)")
    g.add_argument("--lat", type=float, help="override airport latitude")
    g.add_argument("--lon", type=float, help="override airport longitude")
    g.add_argument("--radius", type=float, default=150, help="local watch radius, nm (max 250)")
    g = p.add_argument_group("polling")
    g.add_argument("--interval", type=float, default=10, help="seconds between polls")
    g.add_argument("--provider", choices=PROVIDERS, default="adsb.lol",
                   help="feed for the local circle")
    g.add_argument("--remote-providers", default="adsb.lol",
                   help="comma-separated feeds for following/discovering distant flights "
                        "(e.g. adsb.lol,adsb.fi; a feed answering 401/403 is dropped)")
    g.add_argument("--rate", type=float, default=8,
                   help="starting request budget per feed host, requests/min (adapts: +0.5 per "
                        "success, halved on HTTP 429)")
    g.add_argument("--rate-max", type=float, default=60, help="upper limit for the adaptive budget")
    g.add_argument("--no-routes", action="store_true", help="skip callsign->route lookups")
    g.add_argument("--airport-traffic-only", action="store_true",
                   help="only alert for flights arriving/departing the airport")
    g.add_argument("--once", action="store_true", help="single poll then exit")
    g = p.add_argument_group("distant flights")
    g.add_argument("--no-schedule", action="store_true", help="don't use the TLV flight board")
    g.add_argument("--schedule-refresh", type=float, default=600, help="flight board refresh, s")
    g.add_argument("--schedule-window", type=float, default=16, help="+/- hours of flights to track")
    g.add_argument("--follow-interval", type=float, default=30,
                   help="seconds between follow requests (all followed hex ids in one request)")
    g.add_argument("--discovery-interval", type=float, default=180,
                   help="seconds between rounds of global callsign searches for scheduled flights")
    g.add_argument("--departed-max-h", type=float, default=6,
                   help="stop searching for departures that left more than this many hours ago")
    g.add_argument("--follow-max", type=int, default=600, help="max flights followed globally")
    g.add_argument("--remote-budget", type=int, default=4,
                   help="max requests per provider per layer per cycle (follow / discover)")
    g.add_argument("--airline-map", help="JSON file of extra IATA->ICAO airline codes")
    g = p.add_argument_group("thresholds")
    g.add_argument("--climb-angle", type=float, default=12,
                   help="alert on climbs steeper than this, degrees (normal traffic stays below ~9.5)")
    g.add_argument("--descent-angle", type=float, default=10,
                   help="alert on descents steeper than this, degrees, also near airports "
                        "(glide slope 3, steep approaches ~6, normal maximum seen ~8)")
    g.add_argument("--terminal-climb-angle", type=float, default=18,
                   help="climb angle limit within --terminal-radius of an airport, below --terminal-alt")
    g.add_argument("--terminal-radius", type=float, default=30, help="nm around airports for terminal rules")
    g.add_argument("--terminal-alt", type=int, default=15000, help="ft; terminal rules apply below this")
    g.add_argument("--vrate", type=int, default=8000,
                   help="alert above this |ft/min| whatever the angle")
    g.add_argument("--max-bank", type=float, default=35,
                   help="SHARP_TURN above this implied bank angle, degrees (airliners turn at <= 25-30)")
    g.add_argument("--lost-after", type=float, default=60, help="seconds of silence = lost (local)")
    g.add_argument("--lost-after-remote", type=float, default=600,
                   help="seconds of silence = lost for followed flights outside the local circle")
    g.add_argument("--lost-min-alt", type=int, default=5000, help="ignore losses below this ft")
    g.add_argument("--lost-ignore-radius", type=float, default=8, help="nm around airport to ignore")
    g.add_argument("--edge-margin", type=float, default=20, help="nm inside radius edge to ignore")
    g.add_argument("--turn", type=float, default=70, help="course change alert, degrees")
    g.add_argument("--turn-window", type=float, default=120, help="seconds for course change")
    g.add_argument("--turn-min-alt", type=int, default=12000, help="ignore turns below this ft")
    g.add_argument("--turn-ignore-radius", type=float, default=25, help="nm around airport to ignore")
    g.add_argument("--max-speed", type=float, default=1200, help="kt; faster jumps are flagged")
    g.add_argument("--turn-zones", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                         "turn_zones.json"),
                   help="learned route corners where large turns are routine ('' to disable)")
    g.add_argument("--off-course", type=float, default=100,
                   help="OFF_COURSE when an arrival's track stays this many degrees away from the airport")
    g.add_argument("--turn-back", type=float, default=60,
                   help="TURNING_BACK when a departure's track stays within this many degrees of the airport")
    g.add_argument("--course-hold", type=float, default=150, help="seconds OFF_COURSE / TURNING_BACK must hold")
    g.add_argument("--off-course-min-dist", type=float, default=60, help="nm; closer in, vectors are normal")
    g.add_argument("--protect", choices=("israel", "none"), default=None,
                   help="warn about flights not bound for Israel heading into it (default: israel "
                        "for Israeli airports)")
    g.add_argument("--toward-eta", type=float, default=12,
                   help="TOWARD_ISRAEL when a turn brings Israeli airspace within this many minutes")
    g.add_argument("--toward-turn", type=float, default=45, help="degrees of turn for TOWARD_ISRAEL")
    g.add_argument("--toward-min-alt", type=int, default=8000, help="ft; ignore lower traffic")
    g.add_argument("--hot-minutes", type=float, default=20,
                   help="after an alert, follow the flight every cycle and report every anomaly")
    g.add_argument("--cooldown", type=float, default=300, help="seconds between repeat alerts")
    g = p.add_argument_group("notifications")
    g.add_argument("--jsonl", default="alerts.jsonl", help="append alerts here ('' to disable)")
    g.add_argument("--ntfy-topic", default=os.getenv("NTFY_TOPIC"), help="ntfy topic name")
    g.add_argument("--ntfy-server", default=os.getenv("NTFY_SERVER", "https://ntfy.sh"))
    g.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    a.airport = a.airport.upper()
    known = AIRPORTS.get(a.airport)
    if known:
        a.icao = a.icao or known[0]
        a.lat = a.lat if a.lat is not None else known[1]
        a.lon = a.lon if a.lon is not None else known[2]
    elif a.lat is None or a.lon is None:
        p.error(f"unknown airport {a.airport}: pass --lat and --lon (and --icao)")
    a.icao = (a.icao or a.airport).upper()
    if a.protect is None:
        a.protect = "israel" if REGION_AIRPORTS.get(a.airport, (0, 0, False))[2] else "none"
    a.radius = min(a.radius, 250)
    a.remote_providers = [x.strip() for x in a.remote_providers.split(",") if x.strip()]
    bad = [x for x in a.remote_providers if x not in PROVIDERS]
    if bad:
        p.error(f"unknown provider(s): {', '.join(bad)}")
    return a


def main(argv=None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    http = Http(rate=args.rate, rate_max=args.rate_max)
    schedule = None
    if args.no_schedule:
        pass
    elif args.airport != "TLV":
        log.info("flight board is TLV-only; distant %s flights are followed via route DB only",
                 args.airport)
    else:
        airline_map = dict(AIRLINE_ICAO)
        if args.airline_map:
            with open(args.airline_map, encoding="utf-8") as f:
                airline_map.update({k.upper(): v.upper() for k, v in json.load(f).items()})
        schedule = Schedule(http, airline_map, args.schedule_refresh, args.schedule_window)
    monitor = Monitor(args, Notifier(args, http), http, schedule)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log.info("watching %s (%s) r=%.0fnm every %.0fs via %s",
             args.airport, args.icao, args.radius, args.interval, args.provider)
    local_host = PROVIDERS[args.provider]["base"].split("/")[2]
    backoff = 0.0
    while True:
        started = time.monotonic()
        wait = http.budget(local_host).cooling()
        if 0 < wait < args.interval / 2:  # cooldown ends just after this tick: wait, don't skip it
            time.sleep(wait)
        try:
            monitor.poll()
            st = monitor.stats
            log.info("%d local | %d followed globally | %d scheduled flights | budget %.1f req/min",
                     st["local"], st["followed"], st["scheduled"], st["rate"])
            backoff = 0.0
        except Throttled as e:
            # Cooling down after a 429: skip this cycle's local poll but keep the --interval
            # cadence; the Budget's jittered cooldown decides when requests resume.
            log.info("local poll skipped - %s", e)
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            if code != 429:  # on 429 the Budget has already set a jittered cooldown
                backoff = min(max(backoff * 2, args.interval), 300)
                log.error("HTTP %s from %s - backing off %.0fs", code, args.provider, backoff)
            if code in (401, 403):
                others = [p for p in PROVIDERS if p != args.provider]
                log.error("%s refuses access (some feeds are now feeder-only). "
                          "Try --provider %s", args.provider, " or --provider ".join(others))
        except (requests.RequestException, ValueError) as e:
            backoff = min(max(backoff * 2, args.interval), 300)
            log.error("fetch failed: %s - backing off %.0fs", e, backoff)
        if args.once:
            break
        time.sleep(max(0.0, args.interval - (time.monotonic() - started)) + backoff)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
