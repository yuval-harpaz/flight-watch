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

# readsb / ADSBExchange-v2 compatible feeds. Free, no key, ~1 request/second.
# hex and callsign endpoints take comma-separated lists (verified for airplanes.live).
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
PRIORITY = {"EMERGENCY": 5, "LOST_CONTACT": 5, "DIVERSION": 5, "RETURNED": 4,
            "VERTICAL_RATE": 4, "COURSE_CHANGE": 4, "POSITION_JUMP": 3, "CONTACT_RESTORED": 3}


# --------------------------------------------------------------------------- helpers
def haversine_nm(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 3440.065 * math.asin(math.sqrt(a))


def heading_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


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


class Http:
    """requests.Session with a per-host minimum gap, to respect ~1 req/s limits."""

    def __init__(self, min_gap: float = 1.1):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "flight-watch/0.2"
        self.min_gap, self.last = min_gap, {}

    def _wait(self, url: str) -> None:
        host = url.split("/")[2]
        gap = time.monotonic() - self.last.get(host, -1e9)
        if gap < self.min_gap:
            time.sleep(self.min_gap - gap)
        self.last[host] = time.monotonic()

    def get_json(self, url: str, **kw):
        self._wait(url)
        r = self.s.get(url, timeout=10, **kw)
        r.raise_for_status()
        return r.json()

    def post(self, url: str, **kw):
        self._wait(url)
        r = self.s.post(url, timeout=8, **kw)
        r.raise_for_status()
        return r


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
        self.fetched = -1e9
        self.tz = ZoneInfo("Asia/Jerusalem")

    def refresh_if_due(self) -> None:
        if time.monotonic() - self.fetched < self.refresh:
            return
        self.fetched = time.monotonic()
        try:
            data = self.http.get_json(FLYDATA_URL, params={"resource_id": FLYDATA_RESOURCE,
                                                           "limit": 3000})
            records = data["result"]["records"]
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            log.warning("flight board fetch failed (%s) - keeping %d known flights",
                        e, len(self.by_callsign))
            return
        now = datetime.now(self.tz)
        out, unmapped = {}, set()
        for r in records:
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
            iata = str(r.get("CHOPER") or "").strip().upper()
            num = str(r.get("CHFLTN") or "").strip().lstrip("0") or "0"
            icao = self.airline_map.get(iata) or (iata if len(iata) == 3 else None)
            if not icao:
                unmapped.add(iata)
                continue
            sf = SchedFlight(f"{iata}{num}", direction, str(r.get("CHLOC1") or "?"),
                             str(r.get("CHLOC1D") or ""), when.strftime("%Y-%m-%d %H:%M"), status, when.timestamp())
            for cs in {f"{icao}{num}", f"{icao}{num.zfill(3)}"}:  # ELY1 / ELY001
                out[cs] = sf
        self.by_callsign = out
        log.info("flight board: %d active flights (%d callsign variants)%s",
                 len({id(v) for v in out.values()}), len(out),
                 f"; unmapped airlines: {', '.join(sorted(unmapped))}" if unmapped else "")


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
        self.tracks: dict[str, Track] = {}
        self.routes: dict[str, tuple[float, str | None]] = {}
        self.last_ok: float | None = None
        self.last_count = 0
        self.last_discovery = -1e9
        self.stats = {}
        self.multi: dict[tuple, bool] = {}   # (provider, kind) -> accepts comma-separated ids
        self.rr: dict[tuple, int] = {}       # round-robin position for one-id-per-request mode
        self.disabled: set[str] = set()      # providers that refused us (401/403)

    # ---- remote queries
    def multi_supported(self, prov: str, kind: str, local: list) -> bool | None:
        """Probe once whether `prov` accepts several ids per request, using 2 live local aircraft."""
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
            return None  # can't tell yet
        try:
            _, acs = self.fetch(kind, prov, ids=",".join(vals))
            got = {(x.get("hex") or "").lower() if kind == "hex" else (x.get("flight") or "").strip()
                   for x in acs}
            ok = all(v in got for v in vals)
        except (requests.RequestException, ValueError):
            ok = False
        self.multi[key] = ok
        log.info("%s %s lookups: %s", prov, kind,
                 "many ids per request" if ok else "one id per request (rotating)")
        return ok

    def remote_query(self, prov: str, kind: str, ids: list[str], local: list) -> tuple[list, bool]:
        """Look up ids on a provider within --remote-budget requests. Returns (batches, any_ok)."""
        if not ids or prov in self.disabled:
            return [], False
        if self.multi_supported(prov, kind, local):
            groups = list(chunks(ids, 150 if kind == "hex" else 120))
        else:
            start = self.rr.get((prov, kind), 0) % len(ids)
            order = ids[start:] + ids[:start]
            groups = [[x] for x in order[:self.args.remote_budget]]
            self.rr[(prov, kind)] = start + len(groups)
        out, any_ok = [], False
        for g in groups[:self.args.remote_budget]:
            try:
                now_r, acs = self.fetch(kind, prov, ids=",".join(g))
                out += [(now_r, ac) for ac in acs]
                any_ok = True
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else 0
                if code in (401, 403):
                    self.disabled.add(prov)
                    log.error("%s refused access (HTTP %s) - dropping it for this run", prov, code)
                    break
                log.error("%s query failed (%s): %s", kind, prov, e)
            except (requests.RequestException, ValueError) as e:
                log.error("%s query failed (%s): %s", kind, prov, e)
        return out, any_ok

    # ---- data
    def fetch(self, kind: str, provider: str | None = None, **fmt):
        p = PROVIDERS[provider or self.args.provider]
        data = self.http.get_json(p["base"] + p[kind].format(**fmt))
        now = data.get("now")
        server_now = now / 1000 if now and now > 1e11 else (now or time.time())
        return float(server_now), data.get("ac") or data.get("aircraft") or []

    def lookup_routes(self, tracks: list[Track]) -> None:
        now = time.time()
        need = [t for t in tracks if t.callsign and not t.sched and (
            t.callsign not in self.routes or now - self.routes[t.callsign][0] > ROUTE_TTL)][:100]
        if not need:
            return
        got = {}
        try:
            r = self.http.post(ROUTE_API, json={"planes": [
                {"callsign": t.callsign, "lat": t.last.lat, "lng": t.last.lon} for t in need]})
            for item in r.json():
                cs = (item.get("callsign") or "").strip()
                codes = item.get("_airport_codes_iata") or item.get("airport_codes")
                if cs and codes and codes.lower() != "unknown":
                    got[cs] = codes.upper()
        except (requests.RequestException, ValueError, AttributeError) as e:
            log.debug("route lookup failed: %s", e)
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

        # 1) local circle - failure here aborts the cycle
        server_now, local = self.fetch("point", lat=a.lat, lon=a.lon, radius=int(a.radius))
        batches = [(server_now, ac) for ac in local]

        # 2) follow known TLV flights wherever they are
        # Remote layers ask several networks: coverage differs a lot outside Israel.
        followed = [h for h, t in self.tracks.items() if t.followed][:a.follow_max]
        follow_ok = not followed
        for prov in a.remote_providers:
            got, ok = self.remote_query(prov, "hex", followed, local)
            batches += got
            follow_ok = follow_ok or ok

        # 3) discover scheduled flights not seen yet (searched globally by callsign)
        if self.schedule and time.monotonic() - self.last_discovery >= a.discovery_interval:
            self.last_discovery = time.monotonic()
            known = {t.callsign for t in self.tracks.values() if t.followed}
            now_wall = time.time()

            def likely_airborne(cs):  # arrivals due soon / departures that recently left first
                sf = self.schedule.by_callsign[cs]
                if sf.direction == "ARR":
                    return 0 if -3600 <= sf.ts - now_wall <= 6 * 3600 else 1
                return 0 if -14 * 3600 <= sf.ts - now_wall <= 0 else 1
            wanted = sorted((cs for cs in self.schedule.by_callsign if cs not in known),
                            key=likely_airborne)
            for prov in a.remote_providers:
                batches += self.remote_query(prov, "callsign", wanted, local)[0]

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
            t.last_msg = max(t.last_msg, msg_t)
            if self.schedule and t.callsign in self.schedule.by_callsign:
                t.sched = self.schedule.by_callsign[t.callsign]
            prev = t.last
            if prev is None or sample.t > prev.t + 0.5:
                t.samples.append(sample)
            updated.append((t, prev, ac))

        if not a.no_routes:
            self.lookup_routes([t for t, _, _ in updated])

        for t, prev, ac in updated:
            s = t.last
            d = self.dist(s)
            kind, route = self.classify(t)
            if not t.followed and (kind in CONFIRMED or (kind == "DEP?" and d < 15)):
                t.followed = True
                log.info("following %s (%s %s)", t.label(), kind, route or "")
            if t.lost and server_now - t.last_msg < a.lost_after:
                t.lost = False
                self.alert(t, "CONTACT_RESTORED", f"heard again at {s.alt} ft")
            self.check_emergency(t, ac)
            self.check_vrate(t)
            self.check_turn(t)
            self.check_jump(t, prev)
            self.check_landing(t, kind, d)
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
            self.check_lost(server_now, follow_ok)

        self.last_ok, self.last_count = server_now, len(local)
        self.tracks = {h: t for h, t in self.tracks.items()
                       if server_now - t.last_msg < (3 * 3600 if t.followed else 1800)}
        self.stats = {"local": len(local), "followed": sum(t.followed for t in self.tracks.values()),
                      "scheduled": len(self.schedule.by_callsign) if self.schedule else 0}

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
        s = t.last
        if s.on_ground or s.vrate is None or s.alt is None or abs(s.vrate) < self.args.vrate:
            return
        computed = t.computed_vrate(30)
        if computed is not None and abs(computed) < self.args.vrate * 0.5:
            return  # reported rate not backed by actual altitude change -> likely a glitch
        direction = "DESCENT" if s.vrate < 0 else "CLIMB"
        extra = f" (history: {computed:+.0f} ft/min)" if computed is not None else ""
        self.alert(t, "VERTICAL_RATE", f"{direction} {s.vrate:+d} ft/min at {s.alt} ft{extra}",
                   key=f"VERTICAL_RATE:{direction}", severity=abs(s.vrate))

    def check_turn(self, t: Track) -> None:
        s = t.last
        if (s.on_ground or s.track is None or (s.alt or 0) < self.args.turn_min_alt
                or self.dist(s) < self.args.turn_ignore_radius):
            return
        old = t.sample_ago(self.args.turn_window)
        if not old or old.track is None:
            return
        d = heading_diff(s.track, old.track)
        if d >= self.args.turn:
            self.alert(t, "COURSE_CHANGE", f"track {old.track:.0f} -> {s.track:.0f} deg "
                       f"({d:.0f} deg in {s.t - old.t:.0f}s) at {s.alt} ft")

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
            self.alert(t, "DIVERSION", f"{self.args.airport} arrival on the ground "
                       f"{d:.0f} nm away at {s.lat:.4f},{s.lon:.4f}")
        elif kind == "DEP" and d < 15 and t.max_dist > 40:
            self.alert(t, "RETURNED", f"{self.args.airport} departure landed back at "
                       f"{self.args.airport} (had reached {t.max_dist:.0f} nm)")

    def check_lost(self, now: float, follow_ok: bool) -> None:
        a = self.args
        for t in self.tracks.values():
            s = t.last
            if t.lost or not s or s.on_ground or len(t.samples) < 3:
                continue
            d = self.dist(s)
            if (s.alt or 0) < a.lost_min_alt:
                # A TLV arrival vanishing low and far from TLV is most likely landing elsewhere
                # (low-altitude coverage is poor, so we may never see it on the ground).
                if (t.followed and follow_ok and d > 30 and (s.vrate or 0) <= 0
                        and self.classify(t)[0] == "ARR" and now - t.last_msg >= 3 * a.lost_after):
                    t.lost = True
                    self.alert(t, "DIVERSION", f"{a.airport} arrival went silent descending through "
                               f"{s.alt} ft, {d:.0f} nm from {a.airport} at {s.lat:.4f},{s.lon:.4f}"
                               " - probably landing elsewhere")
                continue
            if d > a.radius - a.edge_margin:
                if not (t.followed and follow_ok):
                    continue  # unfollowed aircraft simply leaving the local circle
                limit = a.lost_after_remote
            else:
                if d < a.lost_ignore_radius:
                    continue  # landing at the airport
                limit = a.lost_after
            silent = now - t.last_msg
            if silent < limit:
                continue
            t.lost = True
            trk = f"{s.track:.0f}" if s.track is not None else "?"
            self.alert(t, "LOST_CONTACT", f"silent {silent:.0f}s; last {s.alt} ft, "
                       f"{d:.0f} nm from {a.airport}, track {trk} deg, at {s.lat:.4f},{s.lon:.4f}")

    # ---- output
    def alert(self, t: Track, kind: str, msg: str, key: str | None = None,
              severity: float = 0, force: bool = False) -> None:
        """Cooldown per key; a clearly worse reading (1.5x severity) or force bypasses it."""
        now = t.last.t  # time of the data, not of the poll
        key = key or kind
        last_t, last_sev = t.alerted.get(key, (-1e12, 0))
        escalated = severity and severity >= 1.5 * last_sev
        if not (force or escalated or kind == "CONTACT_RESTORED") and now - last_t < self.args.cooldown:
            return
        traffic, route = self.classify(t)
        if self.args.airport_traffic_only and traffic not in AIRPORT_TRAFFIC:
            return
        t.alerted[key] = (now, max(severity, last_sev if now - last_t < self.args.cooldown else 0))
        s = t.last
        d = self.dist(s)
        self.notifier.send({
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "kind": kind, "priority": PRIORITY.get(kind, 3), "message": msg,
            "hex": t.hex, "aircraft": t.label(), "callsign": t.callsign,
            "traffic": traffic, "route": route, "remote": d > self.args.radius,
            "scheduled": t.sched.when if t.sched else None,
            "lat": s.lat, "lon": s.lon, "alt": s.alt, "track": s.track, "gs": s.gs,
            "dist_nm": round(d, 1), "links": external_links(t, now),
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
                   help="comma-separated feeds for following/discovering distant flights")
    g.add_argument("--no-routes", action="store_true", help="skip callsign->route lookups")
    g.add_argument("--airport-traffic-only", action="store_true",
                   help="only alert for flights arriving/departing the airport")
    g.add_argument("--once", action="store_true", help="single poll then exit")
    g = p.add_argument_group("distant flights")
    g.add_argument("--no-schedule", action="store_true", help="don't use the TLV flight board")
    g.add_argument("--schedule-refresh", type=float, default=600, help="flight board refresh, s")
    g.add_argument("--schedule-window", type=float, default=16, help="+/- hours of flights to track")
    g.add_argument("--discovery-interval", type=float, default=60,
                   help="seconds between global callsign searches for scheduled flights")
    g.add_argument("--follow-max", type=int, default=600, help="max flights followed globally")
    g.add_argument("--remote-budget", type=int, default=6,
                   help="max requests per provider per layer per cycle (follow / discover)")
    g.add_argument("--airline-map", help="JSON file of extra IATA->ICAO airline codes")
    g = p.add_argument_group("thresholds")
    g.add_argument("--vrate", type=int, default=4000, help="alert above this |ft/min|")
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
    http = Http()
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
    backoff = 0.0
    while True:
        started = time.monotonic()
        try:
            monitor.poll()
            st = monitor.stats
            log.info("%d local | %d followed globally | %d scheduled callsigns",
                     st["local"], st["followed"], st["scheduled"])
            backoff = 0.0
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
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
