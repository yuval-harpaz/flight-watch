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
  COURSE_CHANGE     track change above --turn degrees within --turn-window seconds, new track
                    held --turn-confirm seconds (S-turns and holding patterns are not reversals)
  HOLDING           an airliner circling longer than --holding-minutes (not military)
  EMERGENCY         squawk 7500/7600/7700 or an ADS-B emergency status
  POSITION_JUMP     physically impossible position jump (typical of GPS spoofing), reappearing
                    too far away after a gap, or one position off the track and back
  DIVERSION         a TLV arrival lands somewhere else (incl. return to origin)
  RETURNED          a TLV departure lands back at TLV after leaving
"""
from __future__ import annotations

import argparse
import csv
import io
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
from urllib.parse import urlencode
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
# Callsign -> route and airport positions: Virtual Radar Server standing data (CC0, crowd-sourced;
# adsb.lol's own route API now redirects to it). Per-airline CSV files, fetched on first use and
# kept in memory only. A local checkout of the repository can be used instead (--standing-data).
STANDING_DATA = "https://raw.githubusercontent.com/vradarserver/standing-data/main"
# The last ~10-20 min of one aircraft's reports (readsb trace: every change, sub-second in a dive), on
# another host than the API: answered 12 of 12 quick requests from a home IP on 10 Oct 2026 while
# api.adsb.lol allowed ~3.5 req/min. Rows: [dt, lat, lon, alt|"ground", gs, track, flags, vrate,
# {details}|None, source, geometric alt, ...]; flags 1 stale position, 8 alt is GPS altitude.
TRACE_RECENT = "https://adsb.lol/data/traces/{xx}/trace_recent_{hex}.json"
ROUTE_TTL = 6 * 3600
RELAY_URL = "https://flight-watch-relay.yuvharpaz.workers.dev"  # tools/cors_worker.js (Cloudflare)
VIEWER_URL = "https://yuval-harpaz.github.io/flight-watch/flight_map.html"  # docs/ on GitHub Pages
ROUTE_FILES_PER_CYCLE = 3  # new standing-data files per poll (a cold start needs ~60)
FLYDATA_URL = "https://data.gov.il/api/3/action/datastore_search"
FLYDATA_RESOURCE = "e83f763b-b7d7-479e-b172-ae981ddc6de5"  # Ben Gurion flight board
# the board columns we use: operator, flight number, scheduled / updated time, arrival or departure,
# other airport code and name, status (asking for these only halves the download)
FLYDATA_FIELDS = "CHOPER,CHFLTN,CHSTOL,CHPTOL,CHAORD,CHLOC1,CHLOC1D,CHRMINE"
# a local checkout used instead of STANDING_DATA when present (git clone, then git pull daily)
LOCAL_STANDING_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "standing-data")

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
            "COURSE_CHANGE": 4, "HOLDING": 4, "POSITION_JUMP": 3, "GPS_SPOOFING": 3, "MASS_SILENCE": 3,
            "GPS_DEGRADED": 3, "SKYDIVE_PATTERN": 5,
            "CONTACT_RESTORED": 2}
# kinds that describe abnormal flying; two different ones on one flight within --hot-minutes
# are reported as a pattern (priority 5)
ANOMALIES = {"EMERGENCY", "LOST_CONTACT", "DIVERSION", "TOWARD_ISRAEL", "OFF_COURSE", "TURNING_BACK",
             "VERTICAL_RATE", "SHARP_TURN", "COURSE_CHANGE", "HOLDING", "POSITION_JUMP",
             "SKYDIVE_PATTERN"}

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
# conditions that last while a flight keeps flying the same way: repeated every --repeat-minutes, not
# every --cooldown (6 Oct 2026: one tanker over Israel for 75 min gave 15 TOWARD_ISRAEL alerts)
PERSISTING = {"TOWARD_ISRAEL", "OFF_COURSE", "TURNING_BACK"}
# GPS spoofing (Monitor.spoofed): an aircraft reported motionless at altitude is spoofed unless it
# climbs / descends at least this fast (a stall or spin falls 10,000+ ft/min) - then it is checked.
SPOOF_MAX_RATE = 6000
# alerts that rest on GPS positions / velocities: logged under a spoofing episode, not alerted
SPOOF_KINDS = {"POSITION_JUMP", "COURSE_CHANGE", "SHARP_TURN", "OFF_COURSE", "TURNING_BACK",
               "TOWARD_ISRAEL", "LOST_CONTACT", "HOLDING", "SKYDIVE_PATTERN"}
# Skydiving lift (Monitor.check_skydive): a jump run - slow (<= SKYDIVE_RUN_KT) at height for a
# while - then a steep descent. 4XDAN (P750) over the Dead Sea, 10 Oct 2026: climb to ~12,000 ft
# every 20-40 min, ~50 kt while jumpers exit, then -5,600 ft/min at 150 kt out of coverage.
SKYDIVE_RUN_KT, SKYDIVE_RUN_ALT, SKYDIVE_DESCENT = 80, 6000, 3000
# common jump aircraft (ICAO type -> model, for the alert text)
JUMP_PLANES = {
    "P750": "PAC 750XL", "C208": "Cessna 208 Caravan", "PC6P": "Pilatus PC-6 Porter",
    "PC6T": "Pilatus PC-6 Turbo Porter", "DHC6": "DHC-6 Twin Otter", "DHC2": "DHC-2 Beaver",
    "DHC3": "DHC-3 Otter", "SC7": "Shorts Skyvan", "L410": "Let L-410", "AN2": "Antonov An-2",
    "AN28": "Antonov An-28", "C212": "CASA C-212", "KODI": "Kodiak 100", "BE99": "Beech 99",
    "C182": "Cessna 182", "C206": "Cessna 206", "C210": "Cessna 210", "GA8": "GippsAero Airvan",
}
# emitter categories of large aircraft (A3 large, A4 B757-class, A5 heavy) and airliner type codes:
# such an aircraft flying a jump run is not skydiving (slow flight near the stall at height)
LARGE_CATEGORIES = {"A3", "A4", "A5"}
LARGE_TYPES = re.compile(r"A[0-9]{3}|A[23][0-9]N|B7[0-9]{2}|B[37][0-9]M|BCS[13]|E1[79]0|E[0-9]{2}[05LS]|"
                         r"E2[0-9]{2}|CRJ[0-9X]|AT[47][0-9]|DH8[A-D]|MD[0-9]{2}")
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


def iata_flight(callsign: str) -> str | None:
    """Callsign -> IATA flight number where the airline is known, e.g. FDB1073 -> FZ1073."""
    m = re.fullmatch(r"([A-Z]{3})0*(\d{1,4})", callsign or "")
    return ICAO_TO_IATA[m.group(1)] + m.group(2) if m and m.group(1) in ICAO_TO_IATA else None


def external_links(t: "Track", when: float, viewer: str = "") -> dict:
    """Links to the flight on existing sites (nothing is stored locally), and to our map page
    (docs/flight_map.html) when `viewer` is its address."""
    day = time.strftime("%Y-%m-%d", time.gmtime(when))
    links = {}
    if viewer:
        q = {"hex": t.hex}
        if t.sched:  # the page then shows the board's data too
            q.update(f=t.sched.flight, d=t.sched.direction[0])
        links["map"] = viewer + "?" + urlencode(q)
    flight = t.sched.flight if t.sched else iata_flight(t.callsign)
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

    def get_text(self, url: str, priority: str = HIGH, **kw) -> str:
        r = self.request("GET", url, priority, **kw)
        r.encoding = "utf-8-sig"
        return r.text

    def get_json(self, url: str, priority: str = HIGH, **kw):
        return self.request("GET", url, priority, **kw).json()

    def post(self, url: str, priority: str = HIGH, **kw):
        return self.request("POST", url, priority, timeout=8, **kw)


class StandingData:
    """Routes by callsign and airport positions from VRS standing data, cached per file.

    `source` is the repository's base URL or a local checkout; `preload` (tests, replays) gives
    {"routes": {callsign: "LHBP-LLBG"}, "airports": {icao: [lat, lon]}} and disables fetching."""

    def __init__(self, http: "Http | None", source: str = STANDING_DATA, preload: dict | None = None,
                 ttl: float = 24 * 3600, clock=time.monotonic):
        self.http, self.source, self.ttl, self.clock = http, source.rstrip("/"), ttl, clock
        self.files: dict[str, tuple[float, dict]] = {}
        self.preload = preload
        self.remote = self.source.startswith(("http://", "https://"))  # else a local checkout
        if http and self.source.startswith(("http://", "https://")):
            # static files on a CDN: a far larger budget than the feed's, still backing off on 429
            b = http.budget(self.source.split("/")[2])
            b.rate, b.rate_max, b.burst = max(b.rate, 60), max(b.rate_max, 300), 10

    def _file(self, path: str) -> dict:
        """Rows keyed by their first column; {} when the file does not exist.
        Raises Throttled / RequestException when it could not be fetched (try again later)."""
        hit = self.files.get(path)
        if hit and self.clock() - hit[0] < self.ttl:
            return hit[1]
        if self.source.startswith(("http://", "https://")):
            try:
                text = self.http.get_text(f"{self.source}/{path}", LOW)
            except requests.HTTPError as e:
                if e.response is None or e.response.status_code != 404:
                    raise
                text = ""
        else:
            try:
                with open(os.path.join(self.source, path), encoding="utf-8-sig") as f:
                    text = f.read()
            except FileNotFoundError:
                text = ""
        rows = {}
        for row in csv.reader(io.StringIO(text)):
            if row and row[0] not in ("Callsign", "Code"):
                rows[row[0]] = row
        self.files[path] = (self.clock(), rows)
        return rows

    def route(self, callsign: str) -> list[str] | None:
        """ICAO airport codes along the route (["LHBP", "LLBG"]), or None when not in the data."""
        if self.preload is not None:
            r = self.preload.get("routes", {}).get(callsign)
            return r.split("-") if r else None
        m = re.fullmatch(r"([A-Z]{3})([0-9][0-9A-Z]*)", callsign or "")
        if not m:
            return None
        code, num = m.groups()
        for name in (f"{code}-{num[0]}.csv", f"{code}-all.csv"):  # big airlines are split by digit
            rows = self._file(f"routes/schema-01/{code[0]}/{name}")
            if rows:
                row = rows.get(callsign)
                return row[4].split("-") if row and len(row) > 4 and row[4] else None
        return None

    AIRLINES = "airlines/schema-01/airlines.csv"  # Code,Name,ICAO,IATA,...

    def airline(self, code: str, fetch: bool = False) -> tuple[str, str] | None:
        """(name, IATA) of an ICAO airline code, e.g. FDB -> ("Flydubai", "FZ"). Reads only the
        loaded file unless `fetch` (alerting never waits for it)."""
        if self.preload is not None:
            row = self.preload.get("airlines", {}).get(code)
            return tuple(row[:2]) if row else None
        if fetch:
            self._file(self.AIRLINES)
        hit = self.files.get(self.AIRLINES)
        row = hit[1].get(code) if hit and code else None
        return (row[1], row[3]) if row and len(row) > 3 and row[1] else None

    def airline_by_iata(self, iata: str, airport: str) -> str | None:
        """ICAO code of the airline behind a board's IATA code. IATA codes are reused (NO is Neos
        and Aus-Air), so with several candidates the one with routes through `airport` wins.
        None when unknown or still ambiguous. Raises Throttled / RequestException (try later)."""
        if self.preload is not None:
            return None
        cands = sorted({r[2] for r in self._file(self.AIRLINES).values()
                        if len(r) > 3 and r[3] == iata and re.fullmatch(r"[A-Z]{3}", r[2] or "")})
        if len(cands) <= 1:
            return cands[0] if cands else None
        serving = [c for c in cands if self.serves(c, airport)]
        return serving[0] if len(serving) == 1 else None

    def serves(self, code: str, airport: str) -> bool:
        """Any route of airline `code` through `airport` (ICAO)? Big airlines' routes are split
        by the flight number's first digit."""
        for name in [f"{code}-all.csv"] + [f"{code}-{d}.csv" for d in "123456789"]:
            rows = self._file(f"routes/schema-01/{code[0]}/{name}")
            if any(airport in (r[4] if len(r) > 4 else "").split("-") for r in rows.values()):
                return True
            if name.endswith("-all.csv") and rows:
                return False  # not split: that was all of it
        return False

    def airport_info(self, icao: str) -> tuple[str, str] | None:
        """(IATA, city) from files already loaded - never fetches (used while alerting)."""
        if self.preload is not None:
            pos = self.preload.get("airports", {}).get(icao)
            return (pos[2], pos[3]) if pos and len(pos) >= 4 else None
        hit = self.files.get(f"airports/schema-01/{(icao or 'X')[0]}/{(icao or 'XX')[:2]}.csv")
        row = hit[1].get(icao) if hit else None
        return (row[3], row[4]) if row and len(row) > 4 else None

    def airport(self, icao: str) -> tuple[float, float] | None:
        if self.preload is not None:
            pos = self.preload.get("airports", {}).get(icao)
            return tuple(pos[:2]) if pos else None
        if not re.fullmatch(r"[A-Z0-9]{4}", icao or ""):
            return None
        row = self._file(f"airports/schema-01/{icao[0]}/{icao[:2]}.csv").get(icao)
        try:
            return float(row[6]), float(row[7])
        except (TypeError, ValueError, IndexError):
            return None


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
    mlat: bool = False     # position from multilateration (ground receivers' timing), not the aircraft's GPS
    nic: int | None = None  # the aircraft's own position-integrity category (0 = it does not trust it)
    geo: bool = False      # alt is GPS (geometric) altitude: the feed had no barometric one


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
    lost_pending: float = 0.0   # time its silence reached the limit; alerted --lost-confirm s later
    israeli: bool = False    # seen low at an Israeli airport (so it is Israel traffic)
    route_dir: int = 0       # +1 flying the route as listed, -1 the reverse leg, 0 not known yet
    military: bool = False   # readsb aircraft database flag (dbFlags bit 0)
    pending_turn: dict | None = None  # a reversal waiting to show whether it is one (see check_turn)
    spoof: str = ""          # label of the GPS-spoofing episode it was caught in (spoof-YYYYMMDDTHHMMZ)
    gps_free_until: float = 0.0  # hot from an alert that does not rest on GPS (squawk, baro altitude)
    spoof_last: float = 0.0  # data time of its last spoofed (frozen) report
    spoof_prev: Sample | None = None  # its last spoofed report
    nic_last: int | None = None  # integrity of its previous ADS-B report (to see drops to 0)
    gps: str = ""            # label of the GPS-degraded episode it was caught in (gps-YYYYMMDDTHHMMZ)
    gps_last: float = 0.0    # data time of its last NIC-0 report in that episode
    turn_ref: tuple | None = None     # (track before a manoeuvre found not to be a reversal, until)
    hold_since: float = 0.0  # data time the current circling (holding pattern / orbit) began
    hold_last: float = 0.0   # last data time it was circling
    hold_alerted: int = 0    # --holding-minutes periods of this hold already alerted
    geo_offset: float | None = None  # its GPS altitude minus barometric (weather: a few hundred ft)
    skydive: dict | None = None  # its last skydiving lift (see check_skydive)
    skydive_lifts: int = 0   # lifts seen since we first heard it
    trace_end: float = 0.0   # data time of the newest report in its last recent trace (see recent_trace)

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

    def course_into(self, x: Sample) -> float | None:
        """Direction of travel from the positions (not the reported track) arriving at sample x:
        from the latest earlier sample 2-60 s before it, if it moved at least 0.1 nm."""
        prev = None
        for y in self.samples:
            if y.t > x.t - 2:
                break
            prev = y
        if prev is None or x.t - prev.t > 60 or haversine_nm(prev.lat, prev.lon, x.lat, x.lon) < 0.1:
            return None
        return bearing(prev.lat, prev.lon, x.lat, x.lon)

    def track_ok(self, x: Sample) -> bool:
        """The reported track agrees with the path into x (when known). The path's direction is the
        track halfway between the two positions, so a real turn may differ by its rate (up to
        ~3 deg/s) times half the time between them."""
        c = self.course_into(x)
        if c is None or x.track is None:
            return True
        prev = max((y.t for y in self.samples if y.t <= x.t - 2), default=x.t)
        return heading_diff(x.track, c) <= 20 + 1.5 * (x.t - prev)

    def turned(self, since: float) -> float:
        """Signed track change summed over the samples since `since` (+ right, - left): circling
        adds up, S-turns cancel out."""
        total, prev = 0.0, None
        for s in self.samples:
            if s.t < since or s.track is None:
                continue
            if prev is not None:
                total += (s.track - prev + 540) % 360 - 180
            prev = s.track
        return total

    def circling(self, window: float = 720) -> float | None:
        """Radius (nm) of the area it has circled in over the last `window` s - a holding pattern
        or an orbit: >= 330 deg of turn one way, staying within 25 nm. None when not circling."""
        s = self.last
        recent = [x for x in self.samples if x.t >= s.t - window and not x.on_ground]
        if len(recent) < 6 or recent[-1].t - recent[0].t < 240 or abs(self.turned(s.t - window)) < 330:
            return None
        lat = sum(x.lat for x in recent) / len(recent)
        lon = sum(x.lon for x in recent) / len(recent)
        radius = max(haversine_nm(lat, lon, x.lat, x.lon) for x in recent)
        return radius if radius <= 25 else None

    def computed_vrate(self, span: float = 30) -> float | None:
        now, old = self.last, self.sample_ago(span)
        if not now or not old or now.alt is None or old.alt is None or now.t == old.t:
            return None
        return (now.alt - old.alt) / ((now.t - old.t) / 60)

    def computed_vrate_near(self, span: float = 30, lo: float = 15, hi: float = 90) -> float | None:
        """As computed_vrate, from the report nearest `span` s old within lo..hi s (gaps in polls or
        reception: 4XDAN, 10 Oct 2026 08:55 IDT, had none 30-45 s before its dive was seen)."""
        now = self.last
        if not now or now.alt is None:
            return None
        old = min((x for x in self.samples if x.alt is not None and lo <= now.t - x.t <= hi),
                  key=lambda x: abs(now.t - x.t - span), default=None)
        return None if old is None else (now.alt - old.alt) / ((now.t - old.t) / 60)


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
    track, gs = ac.get("track", ac.get("true_heading")), ac.get("gs")
    mlat_fields = ac.get("mlat") or []
    mlat = ac.get("type") == "mlat" or "lat" in mlat_fields
    if mlat:  # speed / track are the aircraft's own (GNSS) unless MLAT computed them too
        gs = gs if "gs" in mlat_fields else None
        track = track if "track" in mlat_fields else None
    nic = ac.get("nic")  # (an MLAT record's nic is the feed's, not the aircraft's: ignored)
    sample = Sample(pos_t, float(lat), float(lon),
                    int(alt) if alt is not None else None,
                    track, gs, int(vrate) if vrate is not None else None, on_ground, mlat,
                    None if mlat or not isinstance(nic, int) else nic,
                    geo=not on_ground and not isinstance(alt_raw, (int, float)) and alt is not None)
    return hexid, server_now - seen, sample


# --------------------------------------------------------------------------- schedule
class Schedule:
    """Ben Gurion flight board (data.gov.il) -> expected callsigns of active flights."""

    def __init__(self, http: Http, airline_map: dict, refresh: float, window_h: float):
        self.http, self.airline_map = http, airline_map
        self.refresh, self.window = refresh, timedelta(hours=window_h)
        self.by_callsign: dict[str, SchedFlight] = {}
        self.flights: list[SchedFlight] = []
        self.resolve = None  # IATA airline code -> ICAO callsign prefix, for codes not in airline_map
        self.fetched = -1e9
        self.tz = ZoneInfo("Asia/Jerusalem")

    def fetch_records(self) -> list[dict]:
        """All board rows, paging until `total` (one page of 3000 is not always enough)."""
        records: list[dict] = []
        for _ in range(20):
            data = self.http.get_json(FLYDATA_URL, params={"resource_id": FLYDATA_RESOURCE,
                                                           "fields": FLYDATA_FIELDS,
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
                if not icao and iata and iata not in unmapped and self.resolve:
                    icao = self.resolve(iata)
                    if icao:
                        self.airline_map[iata] = icao
                        log.info("airline %s -> %s (from route data: the one flying to TLV)", iata, icao)
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


# --------------------------------------------------------------------------- announcements
def compass(brg: float) -> str:
    return ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[int((brg % 360 + 22.5) // 45) % 8]


class Announcer:
    """Alerts as short social-media posts, one thread per flight.

    Written locally for now (console + --posts JSON lines). A post is a list of segments - plain
    text, or {"link": label, "url": url} - which maps one-to-one onto atproto's TextBuilder
    (.text / .link), and replies carry the thread's root and parent ids (atproto ReplyRef)."""

    LIMIT = 280  # Bluesky allows 300 graphemes; keep a margin (emoji, combining characters)
    HEADLINES = {"TOWARD_ISRAEL": ("⚠️", "Not bound for Israel, turned toward it"),
                 "OFF_COURSE": ("↩️", "Flying away from its destination"),
                 "TURNING_BACK": ("↩️", "Turning back"),
                 "RETURNED": ("\U0001f6ec", "Landed back"),
                 "DIVERSION": ("\U0001f6ec", "Diversion"),
                 "LOST_CONTACT": ("\U0001f4e1", "Lost contact"),
                 "CONTACT_RESTORED": ("\U0001f4e1", "Contact restored"),
                 "SHARP_TURN": ("\U0001f504", "Sharp turn"),
                 "COURSE_CHANGE": ("\U0001f504", "Course reversal"),
                 "HOLDING": ("⏳", "Holding unusually long"),
                 "POSITION_JUMP": ("\U0001f6f0️", "Position jump (GPS spoofing?)"),
                 "GPS_SPOOFING": ("\U0001f6f0️", "GPS spoofing"),
                 "MASS_SILENCE": ("\U0001f4e1", "Many aircraft silent at once"),
                 "GPS_DEGRADED": ("\U0001f6f0️", "GPS jamming / spoofing"),
                 "SKYDIVE_PATTERN": ("\U0001fa82", "Large aircraft flying like a jump plane")}

    def __init__(self, args, out=None):
        self.args = args
        self.out = out or (print if args.verbose else (lambda text: None))  # quiet: posts.jsonl only
        self.threads: dict[str, dict] = {}   # hex -> {"root", "parent", "t"}
        self.sent: deque = deque()           # data times of recent posts (hourly cap)
        self.count = 0
        self.tz = ZoneInfo("Asia/Jerusalem")

    def headline(self, rec: dict) -> tuple[str, str]:
        kind, msg = rec["kind"], rec["message"]
        if kind == "EMERGENCY":
            for code, text in (("7500", "Hijack code 7500"), ("7600", "Radio failure code 7600"),
                               ("7700", "Emergency code 7700")):
                if f"squawk {code}" in msg:
                    return "\U0001f6a8", text
            return "\U0001f6a8", "Emergency status"
        if kind == "VERTICAL_RATE":
            return ("⬇️", "Steep descent") if "DESCENT" in msg else ("⬆️", "Steep climb")
        return self.HEADLINES.get(kind, ("ℹ️", kind.replace("_", " ").title()))

    def compose(self, rec: dict, reply: bool) -> list:
        a = self.args
        emoji, head = self.headline(rec)
        ident = rec.get("flight") or rec["callsign"] or rec["hex"].upper()
        if not reply:
            ident += f" {rec['airline']}" if rec.get("airline") else ""
            extra = ", ".join(x for x in ("military" if rec.get("military") else None,
                                          rec.get("reg"), rec.get("type")) if x)
            ident += f" ({extra})" if extra else ""
            ident += f" {rec['route_text']}" if rec.get("route_text") else ", route unknown"
        alt = rec.get("alt")
        level = ("on the ground" if alt == 0 and rec["kind"] in ("DIVERSION", "RETURNED") else
                 f"FL{alt // 100:03d}" if isinstance(alt, int) and alt >= 10000 else
                 f"{alt} ft" if isinstance(alt, int) else "")
        where = f"{rec['dist_nm']:.0f} nm {compass(rec.get('bearing', 0))} of {a.airport}"
        if rec.get("near_airport") and rec["near_airport"] != a.airport and rec.get("near_nm", 99) <= 40:
            where += f", near {rec['near_airport']}"
        when = datetime.fromisoformat(rec["time"].replace("Z", "+00:00")).astimezone(self.tz)
        stamp = f"{when:%H:%M} {when.tzname()}"
        detail = self.detail(rec)
        links = [("Map", rec["links"].get("map")), ("Live", rec["links"].get("live_adsbx")),
                 ("Replay", rec["links"].get("replay_adsbx")),
                 ("FR24", rec["links"].get("fr24_flight"))]
        links = [(k, v) for k, v in links if v]
        lead = f"{emoji} {head}: {ident} - {', '.join(x for x in (level, where) if x)}. "
        tail = f" {stamp}\n"
        room = self.LIMIT - len(lead) - len(tail) - sum(len(k) + 3 for k, _ in links)
        if room < len(detail):
            detail = detail[:max(0, room - 1)].rstrip(" ,;") + "…" if room > 10 else ""
        segments = [{"text": (lead + detail).rstrip() + tail}]
        for i, (label, url) in enumerate(links):
            if i:
                segments.append({"text": " · "})
            segments.append({"link": label, "url": url})
        return segments

    NAMES = {"EMERGENCY": "emergency", "LOST_CONTACT": "lost contact", "DIVERSION": "diversion",
             "TOWARD_ISRAEL": "turn toward Israel", "OFF_COURSE": "off course", "TURNING_BACK": "turning back",
             "VERTICAL_RATE": "steep climb/descent", "SHARP_TURN": "sharp turn",
             "COURSE_CHANGE": "course reversal", "HOLDING": "long holding", "POSITION_JUMP": "position jump",
             "SKYDIVE_PATTERN": "skydiving pattern"}
    DETAILS = {  # kind -> (pattern in the alert message, compact wording)
        "LOST_CONTACT": (r"silent (\d+)s.*?track (\S+) deg", "silent {0} s, last track {1}°"),
        "VERTICAL_RATE": (r"([+-]\d+) ft/min, (-?[\d.]+) deg", "{0} ft/min ({1}°)"),
        "COURSE_CHANGE": (r"track (\d+) -> (\d+) deg \(\d+ deg in (\d+)s", "track {0}° → {1}° in {2} s"),
        "SHARP_TURN": (r"(\d+) -> (\d+) deg in (\d+)s at (\d+) kt \(~(\d+) deg bank",
                       "{0}° → {1}° in {2} s at {3} kt (~{4}° bank)"),
        "POSITION_JUMP": (r"([\d.]+) nm in (\d+)s \(~(\d+) kt", "{0} nm in {1} s (~{2} kt)"),
        "OFF_COURSE": (r"heading (\d+) deg, (\d+) deg away from (\S+)", "heading {0}°, {1}° away from {2}"),
        "TURNING_BACK": (r"track (\d+) deg\) for (\d+)s", "track {0}° for {1} s"),
    }

    def detail(self, rec: dict) -> str:
        """The facts the headline and position don't already say, compactly."""
        kind, msg = rec["kind"], re.sub(r" \[(also|skydiving pattern|from the feed's recent trace)[^\]]*\]", "", rec["message"])
        if kind in self.DETAILS:
            m = re.search(self.DETAILS[kind][0], msg)
            out = self.DETAILS[kind][1].format(*m.groups()) if m else msg
        elif kind == "CONTACT_RESTORED" or (kind == "EMERGENCY" and "squawk" in msg):
            out = ""
        else:
            out = msg
        k = rec.get("skydive")
        if k and kind != "SKYDIVE_PATTERN":
            out = (out + ". " if out else "") + f"Skydiving lift {k['lift']} ({k['model']})"
        also = re.search(r"\[also: ([^\]]+)\]", rec["message"])
        if also:
            names = [self.NAMES.get(k.strip(), k.strip().lower()) for k in also.group(1).split(",")]
            out = (out + ". " if out else "") + "Also: " + ", ".join(names)
        return out

    @staticmethod
    def rank(rec: dict) -> int:
        """Seriousness for threading: a hijack code outranks everything."""
        if rec["kind"] == "EMERGENCY" and "squawk 7500" in rec["message"]:
            return 6
        return PRIORITY.get(rec["kind"], 3)  # the kind's own priority, not a pattern bump

    def announce(self, rec: dict) -> dict | None:
        a = self.args
        t = datetime.fromisoformat(rec["time"].replace("Z", "+00:00")).timestamp()
        thread = self.threads.get(rec["hex"])
        if thread and t - thread["t"] > a.thread_hours * 3600:
            thread = None
        if rec["priority"] < a.announce_min_priority and not thread:
            return None  # e.g. CONTACT_RESTORED only as a reply to an announced loss
        if thread and self.rank(rec) >= 5 and self.rank(rec) > thread["rank"]:
            thread = None  # serious, and more so than what the thread started with: a new top-level post
        while self.sent and t - self.sent[0] > 3600:
            self.sent.popleft()
        if len(self.sent) >= a.announce_max_per_hour and rec["priority"] < 5:
            log.info("announcement suppressed (%d posts in the last hour): %s %s",
                     len(self.sent), rec["kind"], rec["aircraft"])
            return None
        self.count += 1
        post = {"id": f"p{self.count}", "time": rec["time"], "kind": rec["kind"],
                "priority": rec["priority"], "hex": rec["hex"],
                "root": thread["root"] if thread else None, "reply_to": thread["parent"] if thread else None,
                "segments": self.compose(rec, reply=bool(thread))}
        post["text"] = "".join(s.get("text") or s["link"] for s in post["segments"])
        self.threads[rec["hex"]] = {"root": thread["root"] if thread else post["id"], "parent": post["id"],
                                    "t": t, "rank": thread["rank"] if thread else self.rank(rec)}
        self.sent.append(t)
        if a.posts:
            with open(a.posts, "a", encoding="utf-8") as f:
                f.write(json.dumps(post, ensure_ascii=False) + "\n")
        self.out(self.render(post))
        return post

    @staticmethod
    def render(post: dict) -> str:
        """Console view: the post as it would read, links spelled out underneath."""
        mark = "  ↳ " if post["reply_to"] else "━━ "
        body = "".join(s["text"] if "text" in s else s["link"] for s in post["segments"]).rstrip()
        urls = "  ".join(f"{s['link']}: {s['url']}" for s in post["segments"] if "link" in s)
        indent = "     " if post["reply_to"] else "   "
        return f"{mark}{body.replace(chr(10), chr(10) + indent)}\n{indent}{urls}"


# --------------------------------------------------------------------------- notifier
class Notifier:
    def __init__(self, args, http: Http, announcer: Announcer | None = None, console=None):
        self.args, self.http = args, http
        # without --verbose the console shows one line per alert (the log is off below ERROR)
        self.console = console or (print if not args.verbose else (lambda text: None))
        self.announcer = announcer or Announcer(args)
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.tg_chat = os.getenv("TELEGRAM_CHAT_ID")

    LINK_NAMES = {"map": "Map (flight-watch)", "fr24_flight": "FR24 flight", "fr24_aircraft": "FR24 aircraft",
                  "live_adsbx": "Live (ADSBx)", "live": "Live (airplanes.live)",
                  "replay_adsbx": "Replay (ADSBx)", "replay": "Replay (airplanes.live)"}

    def send(self, rec: dict) -> None:
        where = " [REMOTE]" if rec["remote"] else ""
        text = (f"[{rec['kind']}]{where} {rec['aircraft']} {rec['traffic']}"
                f"{' ' + rec['route'] if rec['route'] else ''} - {rec['message']}")
        log.info("%s | %s", text, rec["links"]["live_adsbx"])
        self.console(self.row(rec))
        self.announcer.announce(rec)
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

    @staticmethod
    def row(rec: dict) -> str:
        """One console line per alert: time (Israel), flight, route if known, alert type."""
        when = datetime.fromisoformat(rec["time"].replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Jerusalem"))
        ident = rec.get("flight") or rec.get("callsign") or rec["hex"].upper()
        ident += f" {rec['airline']}" if rec.get("airline") else ""
        ident += " (military)" if rec.get("military") else ""
        route = f"  {rec['route_text']}" if rec.get("route_text") else ""
        spoof = "".join(f"  [{rec[k]}]" for k in ("spoof", "gps") if rec.get(k))
        return f"{when:%H:%M:%S}  {ident}{route}  {rec['kind']}{spoof}"

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
        self.standing = None if args.no_routes else StandingData(http, args.standing_data)
        if schedule and self.standing:
            schedule.resolve = self.resolve_airline
        self.last_ok: float | None = None
        self.shared_at = 0.0  # data time of the last callsign push to the relay (share_callsigns)
        self.last_count = 0
        self.stats = {}
        self.multi: dict[tuple, bool] = {}   # (provider, kind) -> accepts comma-separated ids
        self.cursor: dict[str, int] = {}     # follow rotation per provider (when > 1 request)
        self.disabled: set[str] = set()      # providers that refused us (401/403)
        self.last_follow = -1e9
        self.started = self.clock()
        self.starved_warned = -1e9
        self.zones = TurnZones(args.turn_zones)
        self.spoofs: list[dict] = []  # GPS-spoofing episodes (see spoofed())
        self.silent: dict[str, float] = {}  # hex -> last message, aircraft whose silence reached the limit
        self.silences: list[dict] = []      # group silences (see check_lost)
        self.nic_drops: list[dict] = []     # GPS-degraded episodes (see integrity())
        self.protect = args.protect == "israel"
        self.disc_queue: dict[str, list[str]] = {}  # discovery round in progress, per provider
        self.disc_round = -1e9
        self.traces: dict[str, tuple[float, list]] = {}  # hex -> (clock, parsed recent trace)
        self.trace_left = 0       # trace requests still allowed this cycle
        self.from_trace = False   # checks are running on reports read from a trace (alert() says so)

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
        """Route (origin..destination) per callsign, from the standing data; at most a few new
        files per cycle, on spare request budget."""
        if self.routes_off or not self.standing:
            return
        now = self.wall()
        loaded = len(self.standing.files)
        for t in tracks:
            cs = t.callsign
            if not cs or (cs in self.routes and now - self.routes[cs][0] < ROUTE_TTL):
                continue
            if self.standing.remote and len(self.standing.files) - loaded >= ROUTE_FILES_PER_CYCLE:
                return  # the rest next cycle: downloads are 1.1 s apart and must not hold up the poll
            try:
                if airline_like(t):  # airline names / IATA codes for alerts: one file, first
                    self.standing.airline(cs[:3], fetch=True)
                codes = self.standing.route(cs)
                for icao in (codes or [])[:1] + (codes or [])[-1:]:
                    self.standing.airport(icao)  # warm the position cache for both ends
                self.route_failures = 0
            except Throttled:
                return  # try again next cycle
            except requests.RequestException as e:
                self.route_failures += 1
                log.debug("route lookup failed: %s", e)
                if self.route_failures >= 3:
                    self.routes_off = True
                    log.info("route lookups failed %d times in a row - disabled for this run "
                             "(flight board and heuristics still classify traffic)", self.route_failures)
                return
            self.routes[cs] = (now, "-".join(codes) if codes else None)

    def resolve_airline(self, iata: str) -> str | None:
        try:
            return self.standing.airline_by_iata(iata, self.args.icao)
        except requests.RequestException as e:  # incl. Throttled: tried again at the next board refresh
            log.debug("airline %s not resolved: %s", iata, e)
            return None

    def airline_name(self, t: Track) -> str | None:
        info = self.standing.airline(t.callsign[:3]) if self.standing and airline_like(t) else None
        return info[0] if info else None

    def flight_number(self, t: Track) -> str | None:
        """IATA flight number: from the board, the built-in airline map, or the airline list."""
        if t.sched:
            return t.sched.flight
        flight = iata_flight(t.callsign)
        m = re.fullmatch(r"([A-Z]{3})0*(\d{1,4})", t.callsign or "")
        if not flight and m and self.standing:
            info = self.standing.airline(m.group(1))
            flight = info[1] + m.group(2) if info and re.fullmatch(r"[A-Z0-9]{2}", info[1] or "") else None
        return flight

    def route_text(self, t: Track, traffic: str, route: str | None) -> str | None:
        """Readable route for announcements: "Dubai (DXB) -> TLV", "Budapest -> Tel Aviv"."""
        known = (t.callsign in self.routes and self.routes[t.callsign][1]) or None
        if t.sched and t.sched.other not in ("", "?"):
            name = t.sched.other_name.title() if t.sched.other_name else ""
            other = f"{name} ({t.sched.other})" if name and name.upper() != t.sched.other else t.sched.other
            return (f"{other} → {self.args.airport}" if t.sched.direction == "ARR"
                    else f"{self.args.airport} → {other}")
        route = known if t.sched else (route or known)
        if not route:
            return None
        names = []
        for code in route.split("-"):
            info = self.standing.airport_info(code) if self.standing else None
            names.append(info[1] or info[0] or code if info else code)
        return " → ".join(names)

    def route_ends(self, t: Track) -> list[tuple[str, float, float]]:
        """[(icao, lat, lon)] for the first and last airport of the flight's known route, if the
        flight is plausibly on it: crowd-sourced routes can be stale or belong to another leg, so a
        route is ignored while the aircraft is far off the corridor between its ends."""
        route = self.routes.get(t.callsign, (0, None))[1]
        if not route or not self.standing:
            return []
        out = []
        for icao in dict.fromkeys(route.split("-")[:1] + route.split("-")[-1:]):
            try:
                pos = self.standing.airport(icao)
            except requests.RequestException:
                pos = None
            if pos:
                out.append((icao, *pos))
        s = t.last
        if len(out) == 2 and s:
            (_, olat, olon), (_, dlat, dlon) = out
            length = haversine_nm(olat, olon, dlat, dlon)
            detour = haversine_nm(olat, olon, s.lat, s.lon) + haversine_nm(s.lat, s.lon, dlat, dlon) - length
            if detour > max(250, 0.3 * length):
                return []
        return out

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

        self.trace_left = a.trace_per_cycle
        # 1) local circle every cycle, top priority - failure here aborts the cycle (after the fallback)
        try:
            server_now, local = self.fetch("point", priority=HIGH, lat=a.lat, lon=a.lon,
                                           radius=int(a.radius))
        except requests.RequestException:
            self.fallback(self.watch_list())
            raise
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
            t.military = t.military or bool(int(ac.get("dbFlags") or 0) & 1)
            t.last_msg = max(t.last_msg, msg_t)
            if self.schedule and t.callsign in self.schedule.by_callsign:
                t.sched = self.schedule.by_callsign[t.callsign]
            prev = t.last
            # GPS and barometric altitude differ by hundreds of feet (4XDAN, 10 Oct 2026: +500-600):
            # a GPS value among barometric ones is a fake step. Converted with this aircraft's offset.
            geom, baro = ac.get("alt_geom"), ac.get("alt_baro")
            if isinstance(geom, (int, float)) and isinstance(baro, (int, float)):
                t.geo_offset = geom - baro
            if sample.geo and t.geo_offset is not None:
                sample.alt = int(sample.alt - t.geo_offset)
            label = self.spoofed(t, sample) or self.integrity(t, sample)  # a fake / untrustworthy
            if not label and (prev is None or sample.t > prev.t + 0.5):  # position is kept out of the track
                t.samples.append(sample)
            updated.append((t, prev, ac, label))

        self.lookup_routes([t for t, *_ in updated])
        # hot flights outside the circle: the follow request gives one report per --follow-interval at
        # best (FZ1073's 5-s wobble fell between them); their trace has every report
        self.fallback([t for t in self.tracks.values() if self.hot(t) and t.hex not in local_hexes
                       and not t.lost and self.clock() - self.traces.get(t.hex, (-1e9,))[0] >= 30])

        for t, prev, ac, spoofed in updated:
            self.check_track(t, prev, ac, spoofed, server_now)

        # Don't declare mass "lost contact" after our own outage or a feed glitch.
        gap_ok = self.last_ok is not None and server_now - self.last_ok < 3 * a.interval + 15
        feed_ok = not (self.last_count > 10 and len(local) < 0.5 * self.last_count)
        if not feed_ok:
            log.warning("feed returned %d aircraft (was %d) - skipping lost-contact check",
                        len(local), self.last_count)
        if gap_ok and feed_ok:
            self.check_lost(server_now)
        self.end_spoofs(server_now)

        self.last_ok, self.last_count = server_now, len(local)
        self.share_callsigns(server_now)
        self.tracks = {h: t for h, t in self.tracks.items()
                       if server_now - t.last_msg < (3 * 3600 if t.followed else 1800)}
        b = self.http.budget(PROVIDERS[a.provider]["base"].split("/")[2])
        self.stats = {"local": len(local), "followed": sum(t.followed for t in self.tracks.values()),
                      "scheduled": len(self.schedule.flights) if self.schedule else 0,
                      "rate": b.rate}

    def check_track(self, t: Track, prev: Sample | None, ac: dict, spoofed: str, server_now: float) -> None:
        """Every check on a track's newest report (from a poll, or one our polls missed - backfill)."""
        a = self.args
        s = t.last
        if t.lost_pending and server_now - t.last_msg < a.lost_after:
            log.info("%s heard again within --lost-confirm: not lost", t.label())
            t.lost_pending = 0.0
            self.silent.pop(t.hex, None)
        if spoofed:
            # Squawks are not GPS: still checked. Barometric altitude is not GPS either, but a
            # report is only "spoofed" while that altitude is steady (see spoofed()), so a real
            # dive or climb never lands here and goes through the vertical-rate check.
            if t.lost and server_now - t.last_msg < a.lost_after:
                t.lost = False
                if t.lost_alerted:
                    self.alert(t, "CONTACT_RESTORED", f"heard again ({spoofed})")
            self.check_emergency(t, ac)
            return
        if s is None:
            return
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
        self.check_skydive(t)  # before check_vrate, so the dive's alert says what it is
        self.check_vrate(t)
        self.check_turn(t)
        self.check_sharp_turn(t)
        self.check_holding(t)
        self.check_jump(t, prev)
        self.check_landing(t, kind, d)
        self.check_destination(t, kind, d)
        self.check_protected(t, kind, route)
        if not s.on_ground:
            t.was_airborne = True
            t.max_dist = max(t.max_dist, d)
        elif t.was_airborne and (s.gs is None or s.gs < 50):
            # Landed (at taxi speed, so not a stray "ground" reading in flight): the leg is
            # over. Otherwise its next flight inherits it - an arrival turning around as a
            # departure was reported as RETURNED (LOT7MA -> LOT4CG at TLV, 6 Oct 2026).
            t.was_airborne, t.max_dist, t.route_dir = False, 0.0, 0

    # ---- an aircraft's recent trace: what our polls missed
    def recent_trace(self, t: Track) -> list[tuple[Sample, dict]] | None:
        """The aircraft's last ~10-20 min of reports from the feed's trace file, oldest first, as
        (sample, {"squawk", "emergency"}); None if not available. At most --trace-per-cycle requests
        per poll, cached 5 s (one read per aircraft per poll), never at the expense of the local poll (another
        host, LOW priority). 4XDAN, 10 Oct 2026: the API refused half the polls (HTTP 429) and dives
        went unseen or unconfirmed; the trace had every report."""
        a = self.args
        hit = self.traces.get(t.hex)
        if hit and self.clock() - hit[0] < 5:  # (asked already this cycle)
            return hit[1]
        if not a.trace_url or self.trace_left <= 0:
            return None
        self.trace_left -= 1
        try:
            data = self.http.get_json(a.trace_url.format(xx=t.hex[-2:], hex=t.hex), LOW)
            rows, base = data["trace"], float(data["timestamp"])
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            log.info("%s: no recent trace (%s)", t.label(), e)
            return None
        out, info, offset = [], {}, t.geo_offset
        for r in rows:
            try:
                flags = r[6] if isinstance(r[6], int) else 0
                alt = r[3]
                det = r[8] if len(r) > 8 and isinstance(r[8], dict) else {}
                for k in ("squawk", "emergency"):
                    if det.get(k):
                        info[k] = str(det[k])
                if len(r) > 10 and isinstance(r[10], (int, float)) and isinstance(alt, (int, float)) and not flags & 8:
                    offset = r[10] - alt  # GPS minus barometric, for rows that carry only GPS altitude
                if flags & 1:
                    continue  # stale position
                ground = alt == "ground"
                if flags & 8 and isinstance(alt, (int, float)):
                    alt = alt - offset if offset is not None else None
                mlat = "mlat" in str(r[9] if len(r) > 9 else "")
                out.append((Sample(t=base + r[0], lat=r[1], lon=r[2], alt=0 if ground else
                                   (int(alt) if isinstance(alt, (int, float)) else None),
                                   track=None if mlat else r[5], gs=None if mlat else r[4],
                                   vrate=r[7] if isinstance(r[7], int) else None, on_ground=ground,
                                   mlat=mlat), dict(info)))
            except (IndexError, TypeError):
                continue
        self.traces = {h: v for h, v in self.traces.items() if self.clock() - v[0] < 60}
        self.traces[t.hex] = (self.clock(), out)
        if out:
            t.trace_end = out[-1][0].t
        return out

    def watch_list(self) -> list[Track]:
        """Aircraft worth reading from their trace while the API refuses us, most urgent first: hot,
        emergency squawk, a steep climb / descent or slow flight at height (a jump run: 4XDAN at
        10:17:04 IDT, 10 Oct 2026, 40 s before a dive no poll saw) at the last report, a skydiving lift."""
        def rank(t):
            s = t.last
            if not s or t.lost or s.on_ground or self.clock() - self.traces.get(t.hex, (-1e9,))[0] < 5:
                return None
            if self.hot(t) or t.squawk in EMERGENCY_SQUAWKS:
                return 0
            steep = s.vrate is not None and (abs(s.vrate) >= 4000 or s.gs and s.gs >= 120
                                             and abs(path_angle(s.vrate, s.gs)) >= 6)
            slow = (s.alt or 0) >= SKYDIVE_RUN_ALT and s.gs is not None and s.gs <= 90
            lift = t.skydive and s.t - t.skydive["t"] <= 600
            return 1 if steep else 2 if slow or lift else None
        ranked = sorted((r, -t.last_msg, t.hex) for t in self.tracks.values() if (r := rank(t)) is not None)
        return [self.tracks[h] for _, _, h in ranked]

    def fallback(self, tracks: list[Track]) -> None:
        """Read the recent trace of these aircraft (at most --trace-per-cycle) and run what our polls
        did not get through every check."""
        for t in tracks:
            if self.trace_left <= 0:
                break
            self.backfill(t)

    @staticmethod
    def thin(rows: list, gap: float) -> list:
        """Rows at least `gap` s apart (the last always kept): a dive has a report every second."""
        kept = []
        for i, x in enumerate(rows):
            if not kept or x[0].t - kept[-1][0].t >= gap or i == len(rows) - 1:
                kept.append(x)
        return kept

    def fill_history(self, t: Track) -> bool:
        """Reports from the trace in the 90 s before the newest one, to back a vertical rate when our
        polls have none from 30-45 s earlier (4XDAN, 10 Oct 2026, 10:17:54 IDT: -15 deg, but the last
        poll that got through was 50 s earlier; then contact was lost - no alert)."""
        last = t.last
        if not last or t.spoof_last and last.t - t.spoof_last < 1800:
            return False  # in a spoofing episode: the trace has its fake reports too
        rows = self.recent_trace(t)
        if not rows:
            return False
        have = [x.t for x in t.samples]
        older = [x for x in self.thin([r for r in rows if last.t - 90 <= r[0].t < last.t - 0.5], 5)
                 if x[0].alt is not None and all(abs(x[0].t - h) > 0.5 for h in have)]
        if not older:
            return False
        t.samples = deque(sorted(list(t.samples) + [x for x, _ in older], key=lambda x: x.t),
                          maxlen=t.samples.maxlen)
        log.info("%s: %d reports from its recent trace fill the altitude history", t.label(), len(older))
        return True

    def backfill(self, t: Track) -> bool | None:
        """Run the reports our polls missed (newer than the last we heard) through every check, from
        the trace. True: it had some (we were blind, it was not silent); False: the trace ends where
        we lost it too; None: no trace."""
        rows = self.recent_trace(t)
        if rows is None:
            return None
        newer = [r for r in rows if r[0].t > t.last_msg + 0.5]
        if not newer:
            return False
        log.info("%s: %d reports our polls missed, from its recent trace (%s-%s)", t.label(), len(newer),
                 time.strftime("%H:%M:%S", time.gmtime(newer[0][0].t)),
                 time.strftime("%H:%M:%S", time.gmtime(newer[-1][0].t)))
        self.from_trace = True
        try:
            for x, info in self.thin(newer, 2):
                prev = t.last
                label = self.spoofed(t, x) or self.integrity(t, x)
                if not label and (prev is None or x.t > prev.t + 0.5):
                    t.samples.append(x)
                t.last_msg = max(t.last_msg, x.t)
                self.check_track(t, prev, info, label, x.t)
        finally:
            self.from_trace = False
        return True

    def share_callsigns(self, now: float) -> None:
        """Every --share-interval, send the relay (tools/cors_worker.js /learn) callsign -> hex of the
        aircraft heard since the last send. The flight board does not name the aircraft, so the map
        page finds a landed flight's track this way; the worker cannot ask adsb.lol itself (429 to
        Cloudflare). One small POST at LOW priority; a failure is logged and never stops the poll."""
        a, token = self.args, os.getenv("RELAY_TOKEN")
        if not (a.share_url and token) or now - self.shared_at < a.share_interval:
            return
        pairs = {t.callsign.strip().upper(): [t.hex, round(t.last_msg)] for t in self.tracks.values()
                 if t.callsign.strip() and t.last_msg > self.shared_at}
        self.shared_at = now
        if not pairs:
            return
        try:
            r = self.http.post(a.share_url, LOW, json=pairs, headers={"Authorization": f"Bearer {token}"})
            log.debug("shared %d callsigns with the relay: %s", len(pairs), r.text[:80])
        except requests.RequestException as e:  # Throttled included
            log.warning("could not share callsigns with the relay: %s", e)

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
        gs = self.trusted_gs(t)
        angle = path_angle(s.vrate, gs) if gs else None
        apt, ad = nearest_airport(s.lat, s.lon)
        terminal = ad <= a.terminal_radius and s.alt < a.terminal_alt
        limit = (a.terminal_climb_angle if terminal else a.climb_angle) if s.vrate > 0 else a.descent_angle
        if not ((angle is not None and abs(angle) >= limit) or abs(s.vrate) >= a.vrate):
            return
        computed = t.computed_vrate(30)
        if computed is None and self.fill_history(t):
            computed = t.computed_vrate(30)
        if computed is None:
            computed = t.computed_vrate_near(30)  # (a longer span only averages a real dive down)
        if computed is None:
            return  # nothing to back the reported rate yet (just heard, e.g. back from a spoofing
            #         gap - QTR94R "+12,224 ft/min" on 6 Oct): judged on the next reports
        c_angle = path_angle(computed, gs) if gs else 0.0
        if abs(computed) < 0.5 * a.vrate and abs(c_angle) < 0.5 * limit:
            return  # reported rate not backed by actual altitude change -> likely a glitch
        direction = "DESCENT" if s.vrate < 0 else "CLIMB"
        extra = f" (history: {computed:+.0f} ft/min)" if computed is not None else ""
        ang = f", {angle:+.1f} deg" if angle is not None else ""
        near = f", {ad:.0f} nm from {apt}" if terminal else ""
        self.alert(t, "VERTICAL_RATE", f"{direction} {s.vrate:+d} ft/min{ang} at {s.alt} ft{near}{extra}",
                   key=f"VERTICAL_RATE:{direction}", severity=abs(s.vrate))

    def trusted_gs(self, t: Track) -> float | None:
        """Ground speed fit for judging a climb / descent angle, else None (then only the --vrate
        rate limit applies). Spoofed / corrupted velocity made normal climbs look like 40-60 deg
        dives on 6 Oct 2026 (B789s at FL380 "at" 117-147 kt, a B738 at FL190 "at" 82 kt), and for
        slow light aircraft an angle limit means little."""
        s = t.last
        if s.gs is None or s.gs < 120:
            return None
        if airline_like(t) and (s.alt or 0) >= 10000 and s.gs < 150:
            return None  # an airliner up there cannot be that slow over the ground
        prev = next((y for y in reversed(t.samples) if 20 <= s.t - y.t <= 120), None)
        if prev is not None:
            moved = haversine_nm(prev.lat, prev.lon, s.lat, s.lon) / ((s.t - prev.t) / 3600)
            # the average lies between the speeds at both ends: 4XDAN (P750) sped up from 55 to
            # 138 kt into a dive, 67 kt on average, and its alert came 30 s late
            speeds = [s.gs] + ([prev.gs] if prev.gs else [])
            if not min(speeds) / 1.6 <= moved <= max(speeds) * 1.6:
                return None  # positions disagree with the reported speed
        return s.gs

    def check_turn(self, t: Track) -> None:
        """Large course change (e.g. a U-turn). Routine turns are skipped: in terminal areas and at
        learned route corners (--turn-zones). A flight that already alerted is always reported."""
        a, s = self.args, t.last
        if s.on_ground or s.track is None:
            return
        if t.pending_turn:
            self.confirm_turn(t)
        if ((s.alt or 0) < a.turn_min_alt or self.dist(s) < a.turn_ignore_radius or t.pending_turn):
            return
        old = t.sample_ago(a.turn_window)
        if not old or old.track is None:
            return
        d = heading_diff(s.track, old.track)
        if d < a.turn:
            return
        if not (t.track_ok(old) and t.track_ok(s)):
            return  # a corrupted track reading (THY6685: 255 deg while flying 23)
        c_old, c_new = t.course_into(old), t.course_into(s)
        if c_old is not None and c_new is not None and heading_diff(c_new, c_old) < a.turn / 2:
            return  # the path barely bent
        msg = f"track {old.track:.0f} -> {s.track:.0f} deg ({d:.0f} deg in {s.t - old.t:.0f}s) at {s.alt} ft"
        if t.circling():
            return  # holding pattern / orbit, also when hot: check_holding reports it if unusually long
        if self.hot(t):
            self.alert(t, "COURSE_CHANGE", msg)
            return
        apt, ad = nearest_airport(s.lat, s.lon)
        if ad <= a.terminal_radius + 5 and s.alt < 25000:
            return  # departure / arrival routing
        if self.zones.expected(s.lat, s.lon, s.track):
            return  # route corner seen on many flights
        before = t.sample_ago(2 * a.turn_window)
        if before and before.track is not None and heading_diff(s.track, before.track) < a.turn:
            return  # back near the course it had before: the second half of an S-turn
        if t.turn_ref and s.t < t.turn_ref[1] and heading_diff(s.track, t.turn_ref[0]) < a.turn:
            return  # rolling out of an orbit / S-turn near the course it had before it (BBG251)
        # Not yet: S-turns (vectoring) and the first half of a holding pattern look the same
        # as a reversal for a minute or two (6 Oct 2026: 4 of 5 alerts overnight).
        t.pending_turn = {"t": s.t, "old_t": old.t, "from": old.track, "msg": msg}

    def confirm_turn(self, t: Track) -> None:
        """A reversal is reported once the new heading has held for --turn-confirm s; dropped if it
        turns back (S-turn) or keeps circling (holding). A flight that alerted meanwhile: at once."""
        a, s, p = self.args, t.last, t.pending_turn
        held = [x for x in t.samples if x.t >= s.t - a.turn_confirm and x.track is not None]
        if self.hot(t):
            t.pending_turn = None
            self.alert(t, "COURSE_CHANGE", p["msg"])
        elif abs(t.turned(p["old_t"])) >= 300 or t.circling():
            t.pending_turn, t.turn_ref = None, (p["from"], s.t + 600)
            log.info("%s circling, not a reversal (%s)", t.label(), p["msg"])
        elif heading_diff(s.track, p["from"]) < a.turn:
            t.pending_turn, t.turn_ref = None, (p["from"], s.t + 600)
            log.debug("%s turned back, not a reversal (%s)", t.label(), p["msg"])
        elif (s.t - p["t"] >= a.turn_confirm and held and held[0].t <= s.t - a.turn_confirm * 0.8
              and all(heading_diff(x.track, s.track) <= 20 for x in held)):
            t.pending_turn = None
            self.alert(t, "COURSE_CHANGE", f"{p['msg']}; new track {s.track:.0f} held {a.turn_confirm:.0f}s")
        elif s.t - p["t"] > 600:
            t.pending_turn = None  # neither: no clear reversal

    def check_skydive(self, t: Track) -> None:
        """A skydiving lift: a jump run (slow at height, positions agreeing) and then a steep
        descent. A jump plane's alerts (the dive, going silent low) then say so, with its model.
        An airliner (airline callsign) or a large aircraft doing it is alerted at priority 5:
        that is near-stall flight at height and a dive, not skydiving. Military transports drop
        paratroopers: only described."""
        s = t.last
        if s.on_ground or s.alt is None:
            return
        computed = t.computed_vrate(30)
        if not ((s.vrate or 0) <= -SKYDIVE_DESCENT or (computed or 0) <= -SKYDIVE_DESCENT):
            return
        if t.skydive and s.t - t.skydive["t"] < 600:
            return  # the same lift
        run = [x for x in t.samples if s.t - 300 <= x.t < s.t and not x.on_ground and x.gs is not None
               and x.gs <= SKYDIVE_RUN_KT and (x.alt or 0) >= SKYDIVE_RUN_ALT]
        if len(run) < 2 or run[-1].t - run[0].t < 15:
            return
        moved = haversine_nm(run[0].lat, run[0].lon, run[-1].lat, run[-1].lon) / ((run[-1].t - run[0].t) / 3600)
        if moved > SKYDIVE_RUN_KT * 1.3:
            return  # positions say it was not slow: corrupted / spoofed speeds (QTR94R "117 kt" at FL390)
        top = max(x.alt for x in run)
        if top - s.alt < 1000:
            return
        t.skydive_lifts += 1
        slow = min(x.gs for x in run)
        model = JUMP_PLANES.get(t.actype, t.actype or "unknown type")
        t.skydive = {"t": s.t, "top": top, "gs": round(slow), "lift": t.skydive_lifts, "model": model}
        large = t.category in LARGE_CATEGORIES or bool(LARGE_TYPES.fullmatch(t.actype or ""))
        why = ("an airliner" if airline_like(t) else "a large aircraft") if (airline_like(t) or large) else ""
        if why and not t.military:
            self.alert(t, "SKYDIVE_PATTERN", f"{why} ({model}) flew a skydiving pattern: {slow:.0f} kt at "
                       f"{top} ft, then descending {min(s.vrate or 0, computed or 0):+.0f} ft/min at {s.alt} ft")
        else:
            log.info("%s skydiving lift %d: %s, %.0f kt at %d ft, descending at %d ft",
                     t.label(), t.skydive_lifts, model, slow, top, s.alt)

    def skydive_note(self, t: Track, now: float) -> str:
        """' [skydiving pattern: ...]' for alerts within 30 min of a lift, else ''."""
        k = t.skydive
        if not k or now - k["t"] > 1800:
            return ""
        return (f" [skydiving pattern, {k['model']}: {k['gs']} kt at {k['top']} ft then a dive; "
                f"lift {k['lift']}]")

    def check_holding(self, t: Track) -> None:
        """An airliner circling (holding pattern / orbit) for unusually long instead of landing:
        reported after --holding-minutes, and again after each further period. Military aircraft
        and non-airline traffic (training, patrols, survey) circle as their job: not reported."""
        a, s = self.args, t.last
        if s.on_ground or t.military or not airline_like(t):
            return
        radius = t.circling()
        if radius is None:
            if t.hold_since and s.t - t.hold_last > 300:
                t.hold_since, t.hold_alerted = 0.0, 0
            return
        if not t.hold_since:  # began about when the circling window did
            t.hold_since = min(x.t for x in t.samples if x.t >= s.t - 720)
        t.hold_last = s.t
        minutes = (s.t - t.hold_since) / 60
        periods = int(minutes // a.holding_minutes)
        if periods > t.hold_alerted:
            t.hold_alerted = periods
            self.alert(t, "HOLDING", f"circling for {minutes:.0f} min within ~{radius:.0f} nm at {s.alt} ft "
                       f"({self.dist(s):.0f} nm from {a.airport})", severity=minutes, force=True)

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
        # ...and the path must bend like the reported tracks: a corrupted velocity message (4X-CZF,
        # 6 Oct 2026: straight on 131 deg, one reading 262 deg / 3,571 kt) is a "turn" otherwise
        c_old, c_new = t.course_into(old), t.course_into(s)
        if c_old is None or c_new is None:
            return
        by_track = (s.track - old.track + 540) % 360 - 180
        by_path = (c_new - c_old + 540) % 360 - 180
        if by_track * by_path <= 0 or abs(by_path) < abs(by_track) / 2:
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
        if dt <= 0 or d < 2:
            return
        kt = d / (dt / 3600)
        # Over a long gap position noise is negligible, so the limit is what an aircraft can
        # average (airliner ground-speed record ~700 kt); military jets keep the looser limit.
        gap_limit = self.args.max_speed if t.military else self.args.max_gap_speed
        if dt <= 120 and kt > self.args.max_speed:
            self.alert(t, "POSITION_JUMP", f"{d:.1f} nm in {dt:.0f}s (~{kt:.0f} kt) - "
                       "possible GPS spoofing or bad data")
        elif dt > 120 and kt > gap_limit:  # shorter gaps: noise; a plausible distance: just unheard
            self.alert(t, "POSITION_JUMP", f"reappeared {d:.1f} nm away after {dt:.0f}s without "
                       f"position (~{kt:.0f} kt average) - possible GPS spoofing or bad data")
        elif len(t.samples) >= 3 and t.samples[-2] is prev:
            # one position off the track and the next back on it: A -> B -> C would need an
            # impossible speed, A -> C directly is ordinary (spoofing / MLAT outlier)
            a, b = t.samples[-3], prev
            span = s.t - a.t
            ab, ac = haversine_nm(a.lat, a.lon, b.lat, b.lon), haversine_nm(a.lat, a.lon, s.lat, s.lon)
            if span > 0 and min(ab, d) >= 5 and (ab + d) / (span / 3600) > gap_limit \
                    and ac / (span / 3600) <= gap_limit:
                self.alert(t, "POSITION_JUMP", f"one position {min(ab, d):.1f} nm off the track, then back "
                           f"({ab + d:.0f} nm in {span:.0f}s via it, ~{(ab + d) / (span / 3600):.0f} kt) - "
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
        if not self.hot(t) and (t.pending_turn or t.circling()):
            return  # a turn not yet known to be a reversal, or a holding pattern / orbit (BBG251)
        if kind not in ("ARR", "DEP"):
            return self.check_route_course(t)
        if s.on_ground or s.track is None or (s.alt or 0) < 5000 or d < a.off_course_min_dist:
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

    def check_route_course(self, t: Track) -> None:
        """Any flight with a known route flying away from its destination, long before landing.

        Crowd-sourced routes are sometimes stored the wrong way round (a return leg under the
        outbound callsign), so the direction is first learned from the flight itself: closing on
        the listed destination confirms it, closing on the listed origin swaps the two."""
        a, s = self.args, t.last
        if s.on_ground or s.track is None or (s.alt or 0) < 10000:
            return
        ends = self.route_ends(t)
        if len(ends) < 2:
            return
        old = t.sample_ago(a.course_hold)
        if not old:
            return
        (o, olat, olon), (dst, dlat, dlon) = ends[0], ends[-1]
        d_dest, d_orig = haversine_nm(s.lat, s.lon, dlat, dlon), haversine_nm(s.lat, s.lon, olat, olon)
        was_dest, was_orig = haversine_nm(old.lat, old.lon, dlat, dlon), haversine_nm(old.lat, old.lon, olat, olon)
        if not t.route_dir:
            # learned away from both ends (departure loops and approaches mislead) while heading
            # roughly toward the end it is closing on
            if min(d_dest, d_orig) < a.off_course_min_dist:
                return
            if (d_dest < was_dest - 5 and d_orig > was_orig
                    and heading_diff(s.track, bearing(s.lat, s.lon, dlat, dlon)) <= 60):
                t.route_dir = 1
            elif (d_orig < was_orig - 5 and d_dest > was_dest
                    and heading_diff(s.track, bearing(s.lat, s.lon, olat, olon)) <= 60):
                t.route_dir = -1
            return
        if t.route_dir < 0:
            (o, olat, olon), (dst, dlat, dlon) = (dst, dlat, dlon), (o, olat, olon)
            d_dest, was_dest = d_orig, was_orig
        if d_dest < a.off_course_min_dist:
            return
        window = [x for x in t.samples if x.t >= old.t and x.track is not None]
        if len(window) < 3:
            return
        devs = [heading_diff(x.track, bearing(x.lat, x.lon, dlat, dlon)) for x in window]
        if min(devs) >= a.off_course and d_dest > was_dest + 3:
            self.alert(t, "OFF_COURSE", f"bound for {dst} (route {self.routes[t.callsign][1]}) but heading "
                       f"{s.track:.0f} deg, {devs[-1]:.0f} deg away from it for {s.t - old.t:.0f}s, "
                       f"{d_dest:.0f} nm out and opening ({was_dest:.0f} -> {d_dest:.0f} nm)")

    def toward_own_airport(self, t: Track) -> str | None:
        """ICAO of the route's origin/destination the flight is pointing at, if any."""
        s = t.last
        for icao, lat, lon in self.route_ends(t):
            if (haversine_nm(s.lat, s.lon, lat, lon) > 20
                    and heading_diff(s.track, bearing(s.lat, s.lon, lat, lon)) <= self.args.toward_dest_tol):
                return icao
        return None

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
        if t.military:
            return  # tankers, transports, patrols over Israel are routine (29 of 33 alerts, 6 Oct 2026)
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
        if self.toward_own_airport(t):
            return  # pointing at its own destination (or origin) beyond Israel
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
            if not self.hot(t) and a.lost_confirm > 0 and self.wait_or_group(t, now):
                continue
            # Our polls may have missed its last reports (HTTP 429, a dive between polls): the trace
            # tells our blindness from its silence, and its missed reports go through every check.
            heard = self.backfill(t)
            if heard:
                log.info("%s was not silent: its trace goes on to %s - judged on that", t.label(),
                         time.strftime("%H:%M:%S", time.gmtime(t.last_msg)))
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
            # (not alerted - e.g. part of a spoofing episode - means no "restored" later either)
            also = "; the feed's own trace ends there too" if heard is False else ""
            t.lost_alerted = self.alert(t, "LOST_CONTACT", f"silent {silent:.0f}s; {where}{also}")

    def wait_or_group(self, t: Track, now: float) -> bool:
        """True when a loss of contact is not (yet) alerted: within --lost-confirm of reaching the
        limit, or one of a group - 3+ aircraft silent within 2 min of each other (reception,
        jamming, the onset of spoofing: on 6 Oct 2026 aircraft went silent ~1 min before showing
        up frozen at the spoofing point). A group gets one alert, labelled silence-YYYYMMDDTHHMMZ."""
        if not t.lost_pending:
            t.lost_pending = now
            self.silent[t.hex] = t.last_msg
            return True
        for h, lm in list(self.silent.items()):
            if now - lm > 900:
                del self.silent[h]
        group = {h for h, lm in self.silent.items() if abs(lm - t.last_msg) <= 120}
        if len(group | {t.hex}) >= 3:
            g = next((g for g in self.silences if abs(g["start"] - t.last_msg) <= 300), None)
            if g is None:
                g = {"label": self.episode_label("silence", t.last_msg),
                     "start": t.last_msg, "hexes": set(), "alerted": False}
                self.silences = [x for x in self.silences if now - x["start"] <= 3600] + [g]
            g["hexes"] |= group | {t.hex}
            t.lost, t.lost_alerted, t.lost_pending = True, False, 0.0
            log.info("%s silent with %d other aircraft - %s, not alerted alone", t.label(),
                     len(g["hexes"]) - 1, g["label"])
            if not g["alerted"]:
                g["alerted"] = True
                self.alert(t, "MASS_SILENCE", f"{g['label']}: {len(g['hexes'])} aircraft went silent within "
                           f"2 min - reception, jamming or spoofing; not alerted one by one",
                           key=g["label"], force=True)
            return True
        if now - t.lost_pending < self.args.lost_confirm:
            return True
        t.lost_pending = 0.0
        self.silent.pop(t.hex, None)
        return False

    # ---- GPS spoofing
    def spoof_part(self, t: Track, kind: str, s: Sample) -> str:
        """Label of the spoofing episode an alert of this kind on t would be part of, else "".
        Position-based alerts on a flight caught in an episode (for 30 min after its last fake
        report), and jumps landing within 100 nm of an active spoofing point (fake positions there
        also drift at believable speeds), are logged under the episode instead of alerted -
        unless the flight is hot from an alert that does not rest on GPS (emergency squawk,
        vertical rate, diversion)."""
        if kind not in SPOOF_KINDS or (t.last and t.last.t < t.gps_free_until):
            return ""  # (hot from GPS-based alerts - e.g. this spoofing's own onset - does not count)
        if t.spoof and s.t - t.spoof_last <= 1800:
            return t.spoof
        if kind == "POSITION_JUMP":
            for ep in self.spoofs:
                if ep["alerted"] and s.t - ep["last"] <= 900 and haversine_nm(ep["lat"], ep["lon"], s.lat, s.lon) <= 100:
                    t.spoof, t.spoof_last = ep["label"], s.t
                    ep["hexes"].add(t.hex)
                    return ep["label"]
        return ""

    def episode_label(self, prefix: str, when: float) -> str:
        """prefix-YYYYMMDDTHHMMZ, with -2, -3... when another episode started the same minute
        (on 6 Oct two areas 300 km apart lost GPS integrity at once)."""
        base = f"{prefix}-{time.strftime('%Y%m%dT%H%MZ', time.gmtime(when))}"
        taken = {e["label"] for e in self.spoofs + self.silences + self.nic_drops}
        label, n = base, 1
        while label in taken:
            n += 1
            label = f"{base}-{n}"
        return label

    def integrity(self, t: Track, x: Sample) -> str:
        """The aircraft's own integrity flag. NIC 0 means its avionics do not trust the position:
        GPS jammed (it then navigates on inertial, positions roughly right) or a spoof it noticed
        (AEE925 on 6 Oct: NIC 0 / NACp 0 / SIL 0 on every fake position, 8 again a minute after).
        Such a report is used only if the flight could have flown there since its last trusted
        position; else it is dropped (returns a label) - no jump alert for it. Healthy NIC proves
        nothing: 61% of the reports frozen at the Amman spoofing point still said NIC 8.
        Drops to NIC 0 by 3+ aircraft within 2 min and 100 nm form a GPS-degraded episode (one
        alert, label gps-YYYYMMDDTHHMMZ; one more when 15 min pass without NIC-0 reports)."""
        if x.mlat or x.nic is None or x.on_ground:
            return ""
        dropped = x.nic == 0 and t.nic_last not in (0, None)
        t.nic_last = x.nic
        if x.nic != 0:
            return ""
        ep = next((e for e in self.nic_drops if x.t - e["last"] <= 900
                   and haversine_nm(e["lat"], e["lon"], x.lat if t.last is None else t.last.lat,
                                    x.lon if t.last is None else t.last.lon) <= 100), None)
        if dropped:
            ref = t.last or x
            if ep is None or x.t - ep["last_drop"] > 120 and len(ep["hexes"]) < 3:
                ep = {"label": self.episode_label("gps", x.t), "lat": ref.lat,
                      "lon": ref.lon, "start": x.t, "last": x.t, "last_drop": x.t, "hexes": set(),
                      "alerted": False, "track": t}
                self.nic_drops = [e for e in self.nic_drops if x.t - e["last"] <= 3600] + [ep]
            ep["hexes"].add(t.hex)
            ep["last_drop"] = x.t
            if len(ep["hexes"]) >= 3 and not ep["alerted"]:
                ep["alerted"] = True
                apt, ad = nearest_airport(ep["lat"], ep["lon"])
                self.alert(t, "GPS_DEGRADED", f"{ep['label']}: {len(ep['hexes'])} aircraft report they no "
                           f"longer trust their GPS position (NIC 0) within 2 min, around {ep['lat']:.2f},"
                           f"{ep['lon']:.2f} ({ad:.0f} nm from {apt}) - jamming or spoofing",
                           key=ep["label"], force=True, sample=t.last or x)
        if ep is not None:
            ep["last"], ep["track"] = x.t, t
            t.gps, t.gps_last = ep["label"], x.t
        last = t.last
        if last is None or (x.t > last.t and haversine_nm(last.lat, last.lon, x.lat, x.lon)
                            <= 3 + 650 * (x.t - last.t) / 3600):
            return ""  # untrusted by the aircraft, but where it could be: keep watching it
        log.info("%s NIC 0 position %.4f,%.4f out of reach of its last trusted one - dropped", t.label(), x.lat, x.lon)
        return t.gps or "nic0"

    def off_mlat(self, t: Track, x: Sample) -> bool:
        """A GPS report farther from the flight's latest MLAT fix (<= 3 min old) than it could have
        flown: fake. On 6 Oct 2026 MLAT and clean GPS positions agreed to a median 0.2 km, and while
        GPS was spoofed MLAT followed the real path (AEE925 climbing out of TLV)."""
        if x.mlat:
            return False
        fix = next((y for y in reversed(t.samples) if y.mlat), None)
        if fix is None or not 0 <= x.t - fix.t <= 180:
            return False
        return haversine_nm(fix.lat, fix.lon, x.lat, x.lon) > 3 + 650 * (x.t - fix.t) / 3600

    def spoofed(self, t: Track, x: Sample) -> str:
        """Label of the spoofing episode when report x is a fake position, else "".

        Only what a fixed-wing aircraft physically cannot do counts: hanging motionless in the air,
        i.e. under 50 kt at 5,000 ft or more while its barometric altitude (from its own air data,
        not GPS) is steady. A real upset - a stall, a spin, a near-vertical dive - can also show a
        very low ground speed, but its altitude changes fast, so it is still checked as usual.
        Helicopters and balloons can hover and are never judged. On 6 Oct 2026 (12:42-14:25Z) at
        least 43 aircraft were reported motionless at 31.717 N 35.999 E (Amman airport) at cruise
        and then reappeared 100+ nm away: ~190 of 562 alerts that day.
        Episodes are spoofing points (within 2 km) shared by aircraft; labelled by start time."""
        if x.mlat:
            return ""  # computed on the ground from signal timing, not by the aircraft's GPS
        if t.spoof and x.t - t.spoof_last <= 1800 and self.off_mlat(t, x):
            t.spoof_last = x.t  # a flight in an episode, its GPS report far from its MLAT fix
            log.info("%s GPS position at %.4f,%.4f, far from its MLAT fix - spoofed (%s)", t.label(),
                     x.lat, x.lon, t.spoof)
            return t.spoof
        if (x.on_ground or (x.alt or 0) < 5000 or x.gs is None or x.gs >= 50
                or t.category in LIGHT_CATEGORIES or t.military):  # (some military aircraft can hover)
            return ""
        # A stall / spin falls 10,000+ ft/min; zero headway while climbing or descending at a
        # normal rate (spoofed aircraft keep flying their real profile) is impossible.
        rate = x.vrate
        ref = t.spoof_prev if t.spoof_prev and x.t - t.spoof_prev.t <= 120 else t.last
        if rate is None and ref is not None and ref.alt is not None and 0 < x.t - ref.t <= 120:
            rate = (x.alt - ref.alt) / (x.t - ref.t) * 60
        if rate is not None and abs(rate) >= SPOOF_MAX_RATE:
            return ""
        ep = next((e for e in self.spoofs if x.t - e["last"] <= 900
                   and haversine_nm(e["lat"], e["lon"], x.lat, x.lon) <= 1.1), None)
        if ep is None:
            ep = {"label": self.episode_label("spoof", x.t), "lat": x.lat, "lon": x.lon,
                  "start": x.t, "last": x.t, "hexes": set(), "alerted": False, "track": t}
            self.spoofs.append(ep)
        ep["last"], ep["track"] = max(ep["last"], x.t), t
        t.spoof, t.spoof_last, t.spoof_prev = ep["label"], x.t, x
        if t.hex not in ep["hexes"]:
            ep["hexes"].add(t.hex)
            log.info("%s reported motionless at %s ft, %.4f,%.4f - GPS spoofing (%s, %d aircraft)",
                     t.label(), x.alt, x.lat, x.lon, ep["label"], len(ep["hexes"]))
        if len(ep["hexes"]) >= 2 and not ep["alerted"]:
            ep["alerted"] = True
            apt, ad = nearest_airport(ep["lat"], ep["lon"])
            self.alert(t, "GPS_SPOOFING", f"{ep['label']}: {len(ep['hexes'])} aircraft reported motionless in "
                       f"the air at {ep['lat']:.4f},{ep['lon']:.4f} ({ad:.0f} nm from {apt}) - positions, "
                       f"speeds and tracks there are fake", key=ep["label"], force=True, sample=x)
        return ep["label"]

    def end_spoofs(self, now: float) -> None:
        """An episode with no spoofed (or NIC-0) report for 15 min is over: one summary alert."""
        for ep in list(self.nic_drops):
            if now - ep["last"] > 900:
                self.nic_drops.remove(ep)
                if ep["alerted"]:
                    t = ep["track"]
                    self.alert(t, "GPS_DEGRADED", f"{ep['label']} ended: {len(ep['hexes'])} aircraft reported "
                               f"untrusted GPS (NIC 0) from {time.strftime('%H:%M', time.gmtime(ep['start']))} "
                               f"to {time.strftime('%H:%M', time.gmtime(ep['last']))} UTC",
                               key=ep["label"] + ":end", force=True)
        for ep in list(self.spoofs):
            if now - ep["last"] <= 900:
                continue
            self.spoofs.remove(ep)
            if ep["alerted"]:
                t = ep["track"]
                self.alert(t, "GPS_SPOOFING", f"{ep['label']} ended: {len(ep['hexes'])} aircraft reported "
                           f"motionless at {ep['lat']:.4f},{ep['lon']:.4f} from "
                           f"{time.strftime('%H:%M', time.gmtime(ep['start']))} to "
                           f"{time.strftime('%H:%M', time.gmtime(ep['last']))} UTC",
                           key=ep["label"] + ":end", force=True, sample=t.spoof_prev)

    # ---- output
    def hot(self, t: Track) -> bool:
        """Alerted within --hot-minutes (data time): followed every cycle, every anomaly reported."""
        return bool(t.last) and t.last.t < t.hot_until

    def alert(self, t: Track, kind: str, msg: str, key: str | None = None,
              severity: float = 0, force: bool = False, sample: Sample | None = None) -> bool:
        """Cooldown per key; a clearly worse reading (1.5x severity) or force bypasses it.
        `sample`: where / when, if not the track's last (trusted) report (a spoofed report)."""
        a = self.args
        s = sample or t.last
        if s is None:
            return False
        now = s.t  # time of the data, not of the poll
        part = self.spoof_part(t, kind, s)
        if part:
            if kind == "POSITION_JUMP":
                t.spoof_last = now  # still being spoofed
            log.info("%s %s - part of GPS spoofing %s, not alerted: %s", t.label(), kind, part, msg)
            return False
        key = key or kind
        last_t, last_sev = t.alerted.get(key, (-1e12, 0))
        escalated = severity and severity >= 1.5 * last_sev
        cooldown = a.repeat_minutes * 60 if kind in PERSISTING else a.cooldown
        if not (force or escalated or kind == "CONTACT_RESTORED") and now - last_t < cooldown:
            return False
        traffic, route = self.classify(t)
        if a.airport_traffic_only and traffic not in AIRPORT_TRAFFIC:
            return False
        recent = sorted({k.split(":")[0] for k, (tt, _) in t.alerted.items()
                         if now - tt <= a.hot_minutes * 60 and k.split(":")[0] in ANOMALIES} - {kind})
        pattern = kind in ANOMALIES and bool(recent)
        t.alerted[key] = (now, max(severity, last_sev if now - last_t < a.cooldown else 0))
        if not t.followed:
            log.info("following %s (alerted)", t.label())
        t.followed = True  # keep its fate visible wherever it goes
        if kind in ANOMALIES:
            t.hot_until = max(t.hot_until, now + a.hot_minutes * 60)
            if kind not in SPOOF_KINDS:
                t.gps_free_until = max(t.gps_free_until, now + a.hot_minutes * 60)
        d = self.dist(s)
        apt, ad = nearest_airport(s.lat, s.lon)
        if kind != "SKYDIVE_PATTERN":
            msg += self.skydive_note(t, now)
        if self.from_trace:  # raised on a report our polls missed (backfill), at that report's time
            msg += " [from the feed's recent trace: missed by our polls]"
        self.notifier.send({
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "kind": kind, "priority": 5 if pattern else PRIORITY.get(kind, 3),
            "message": msg + (f" [also: {', '.join(recent)}]" if pattern else ""),
            "hex": t.hex, "aircraft": t.label(), "callsign": t.callsign,
            "traffic": traffic, "route": route, "remote": d > a.radius,
            "scheduled": t.sched.when if t.sched else None,
            "lat": s.lat, "lon": s.lon, "alt": s.alt, "track": s.track, "gs": s.gs,
            "dist_nm": round(d, 1), "near": f"{apt} {ad:.0f} nm", "near_airport": apt,
            "near_nm": round(ad, 1), "bearing": round(bearing(a.lat, a.lon, s.lat, s.lon)),
            "flight": self.flight_number(t), "airline": self.airline_name(t),
            "military": t.military, "reg": t.reg, "type": t.actype, "squawk": t.squawk, "route_text": self.route_text(t, traffic, route),
            "recent": recent, "links": external_links(t, now, self.args.viewer_url),
            # the spoofing episode this flight was caught in (same label on every flight in it)
            "spoof": t.spoof if t.spoof and now - t.spoof_last <= 1800 else None,
            "gps": t.gps if t.gps and now - t.gps_last <= 1800 else None,  # its GPS-degraded episode
            "skydive": t.skydive if t.skydive and now - t.skydive["t"] <= 1800 else None,
            "from_trace": self.from_trace,
        })
        return True


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
    g.add_argument("--trace-url", default=TRACE_RECENT,
                   help="an aircraft's recent reports ({xx}: last 2 hex digits), read to fill altitude "
                        "history and reports our polls missed; '' = never")
    g.add_argument("--trace-per-cycle", type=int, default=3, help="at most this many trace requests per poll")
    g.add_argument("--standing-data",
                   help="VRS standing-data repository (URL or local checkout) for routes and airports "
                        f"(default: {LOCAL_STANDING_DATA} if it exists, else {STANDING_DATA})")
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
    g.add_argument("--lost-confirm", type=float, default=60,
                   help="then wait this long: heard again = nothing; 2+ other aircraft silent within "
                        "2 min = one group-silence alert (reception / jamming); else LOST_CONTACT")
    g.add_argument("--lost-after-remote", type=float, default=600,
                   help="seconds of silence = lost for followed flights outside the local circle")
    g.add_argument("--lost-min-alt", type=int, default=5000, help="ignore losses below this ft")
    g.add_argument("--lost-ignore-radius", type=float, default=8, help="nm around airport to ignore")
    g.add_argument("--edge-margin", type=float, default=20, help="nm inside radius edge to ignore")
    g.add_argument("--turn", type=float, default=70, help="course change alert, degrees")
    g.add_argument("--turn-window", type=float, default=120, help="seconds for course change")
    g.add_argument("--turn-confirm", type=float, default=120,
                   help="a course change is reported once the new track has held this long (s); "
                        "S-turns and holding patterns are not (flights already alerted: at once)")
    g.add_argument("--holding-minutes", type=float, default=30,
                   help="HOLDING: an airliner circling this long (again after each further period); "
                        "military and non-airline traffic are not reported")
    g.add_argument("--turn-min-alt", type=int, default=12000, help="ignore turns below this ft")
    g.add_argument("--turn-ignore-radius", type=float, default=25, help="nm around airport to ignore")
    g.add_argument("--max-speed", type=float, default=1200, help="kt; faster jumps are flagged")
    g.add_argument("--max-gap-speed", type=float, default=750,
                   help="kt; reappearing after > 120 s without position farther than this average speed "
                        "allows, or one position off the track and back, is a POSITION_JUMP")
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
    g.add_argument("--toward-dest-tol", type=float, default=25,
                   help="no TOWARD_ISRAEL while the track is within this many degrees of the "
                        "flight's own destination (or origin) from the route data")
    g.add_argument("--toward-min-alt", type=int, default=8000, help="ft; ignore lower traffic")
    g.add_argument("--hot-minutes", type=float, default=20,
                   help="after an alert, follow the flight every cycle and report every anomaly")
    g.add_argument("--cooldown", type=float, default=300, help="seconds between repeat alerts")
    g.add_argument("--repeat-minutes", type=float, default=30,
                   help="a condition that persists (toward Israel, off course, turning back) is "
                        "repeated this rarely instead of every --cooldown")
    g = p.add_argument_group("notifications")
    g.add_argument("--jsonl", default="alerts.jsonl", help="append alerts here ('' to disable)")
    g.add_argument("--posts", default="posts.jsonl",
                   help="append announcements (social-media style posts, threaded per flight) here")
    g.add_argument("--announce-min-priority", type=int, default=3,
                   help="announce alerts from this priority; lower ones only as replies in a thread")
    g.add_argument("--announce-max-per-hour", type=int, default=20,
                   help="cap on announcements per hour (priority 5 is never held back)")
    g.add_argument("--share-url", default=os.getenv("RELAY_LEARN_URL", RELAY_URL + "/learn"),
                   help="send the map page's relay callsign -> aircraft pairs heard near the airport "
                        "(only with RELAY_TOKEN set in the environment; '' to disable)")
    g.add_argument("--share-interval", type=float, default=300, help="seconds between those sends")
    g.add_argument("--viewer-url", default=os.getenv("VIEWER_URL", VIEWER_URL),
                   help="our flight map page, linked from alerts and posts as 'Map' ('' to leave out)")
    g.add_argument("--thread-hours", type=float, default=6,
                   help="later alerts on a flight reply to its thread for this long")
    g.add_argument("--ntfy-topic", default=os.getenv("NTFY_TOPIC"), help="ntfy topic name")
    g.add_argument("--ntfy-server", default=os.getenv("NTFY_SERVER", "https://ntfy.sh"))
    g.add_argument("--log-file", help="also write the full log (as with -v) to this file")
    g.add_argument("-v", "--verbose", action="count", default=0,
                   help="-v: the full log and the announcement feed; -vv: also debug detail. "
                        "Without it: the first aircraft count, then one line per alert (and errors)")
    a = p.parse_args(argv)
    if not a.standing_data:
        a.standing_data = LOCAL_STANDING_DATA if os.path.isdir(LOCAL_STANDING_DATA) else STANDING_DATA

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
    console = {0: logging.ERROR, 1: logging.INFO}.get(args.verbose, logging.DEBUG)
    logging.basicConfig(level=min(console, logging.INFO if args.log_file else console),
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger().handlers[0].setLevel(console)
    if args.log_file:
        fh = logging.FileHandler(args.log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
        logging.getLogger().addHandler(fh)
    log.info("standing data (routes, airports, airlines): %s", args.standing_data)
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
    backoff, counted = 0.0, False
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
            if not args.verbose and not counted:
                counted = True
                print(f"{time.strftime('%H:%M:%S')}  watching {args.airport}: {st['local']} aircraft within "
                      f"{args.radius:.0f} nm, {st['scheduled']} scheduled flights", flush=True)
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
