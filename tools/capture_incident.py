#!/usr/bin/env python3
"""Capture a historical incident as a replay fixture for flight_watch.

Selects every aircraft that was within --radius nm of the airport during the window, plus every
flight that took off from / landed at the airport within a few hours of it (so remote follow and
discovery are exercised too). Positions come from the ADS-B Exchange globe history:

  heatmap/NN.bin.ttf      all aircraft worldwide, one position per 10 s, 30 min per file
                          (used only to pick the aircraft)
  traces/XX/trace_full_HEX.json
                          the full day's trace of one aircraft (positions, track, rates,
                          callsign, squawk, emergency status)

The flight board of that day is no longer online, so board rows are derived from the traces
(arrival/departure = the aircraft was on the ground at the airport after/before the window) plus
any --board rows given by hand (e.g. a flight that never arrived).

Example (FZ1073 squawking 7700/7500 and turning back over Jordan):
  python tools/capture_incident.py --date 2026-09-30 --start 05:00 --end 06:15 \\
      --focus 8965d1 --board "FDB1073:A:DXB:2026-09-30T08:55" \\
      --name "FZ1073 7700/7500 and U-turn over Jordan" \\
      --out tests/data/fz1073_2026-09-30.json.gz
"""
from __future__ import annotations

import argparse
import gzip
import http.client
import json
import os
import struct
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import flight_watch as fw  # noqa: E402

BASE = "https://globe.adsbexchange.com/globe_history"
HEADERS = {"User-Agent": "flight-watch-capture/0.1", "Referer": "https://globe.adsbexchange.com/"}
MAGIC = 0x0E7F7C9D
GROUND = -123  # heatmap altitude value for "on the ground"


def get(url: str, cache: str | None = None) -> bytes:
    if cache and os.path.exists(cache):
        with open(cache, "rb") as f:
            return f.read()
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={**HEADERS, "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
                    data = gzip.decompress(data)
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            time.sleep(2 ** attempt)
        except (OSError, http.client.HTTPException):  # reset / truncated transfer
            time.sleep(2 ** attempt)
    else:
        raise RuntimeError(f"failed: {url}")
    if cache:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "wb") as f:
            f.write(data)
    time.sleep(0.5)  # be polite
    return data


def heatmap(day: datetime, n: int, cache_dir: str):
    """Yield (epoch, hex, lat, lon, alt_raw) for ICAO-addressed positions in half-hour file n."""
    d = get(f"{BASE}/{day:%Y/%m/%d}/heatmap/{n:02d}.bin.ttf",
            os.path.join(cache_dir, f"heatmap_{day:%Y%m%d}_{n:02d}.bin"))
    ts = None
    for i in range(len(d) // 16):
        h, a, b, x = struct.unpack_from("<iiii", d, i * 16)
        if h == MAGIC:
            ts = (a * 2 ** 32 + (b & 0xFFFFFFFF)) / 1000
            continue
        if ts is None or a & (1 << 30) or h >> 24:  # callsign entry / non-ICAO address
            continue
        alt = struct.unpack("<h", struct.pack("<H", x & 0xFFFF))[0]
        yield ts, f"{h & 0xFFFFFF:06x}", a / 1e6, b / 1e6, alt


def trace_points(tr: dict, t0: float, t1: float, step: float):
    """Trace points in [t0, t1] as compact rows, at most one per `step` s unless something changes.

    Row: [t, lat, lon, alt ("ground" / ft / None), gs, track, vrate, flight, squawk, emergency,
    category]; flight..category are None when unchanged since the previous row.
    """
    out, last_t, state = [], -1e18, {}
    for p in tr["trace"]:
        t = tr["timestamp"] + p[0]
        ex = p[8] if len(p) > 8 and isinstance(p[8], dict) else {}
        changed = {}
        for k in ("flight", "squawk", "emergency", "category"):
            v = ex.get(k)
            if isinstance(v, str):
                v = v.strip()
            if v and state.get(k) != v:
                changed[k] = v
        state.update(changed)
        if t < t0 - 600 or t > t1:  # 10 min of history before the window
            continue
        if not out:  # the first stored point carries the full state, set before the window
            changed = dict(state)
        elif t - last_t < step and not changed:
            continue
        last_t = t
        alt = p[3] if p[3] == "ground" or p[3] is None else int(p[3])
        out.append([round(t, 1), round(p[1], 5), round(p[2], 5), alt,
                    None if p[4] is None else round(p[4], 1),
                    None if p[5] is None else round(p[5], 1), p[7],
                    *(changed.get(k) for k in ("flight", "squawk", "emergency", "category"))])
    return out


def flights(tr: dict) -> list[dict]:
    """Split a day's trace into flights: airborne stretches between ground stops (or gaps > 30 min).

    Each flight: start/end epoch, first/last (lat, lon, alt) and its most used callsign. A flight
    that starts within 10 min of a ground point starts from that point (alt 0)."""
    out, cur, prev_t, ground = [], None, None, None
    for p in tr["trace"]:
        t = tr["timestamp"] + p[0]
        ex = p[8] if len(p) > 8 and isinstance(p[8], dict) else {}
        cs = ex.get("flight").strip() if isinstance(ex.get("flight"), str) else ""
        if cur and prev_t is not None and t - prev_t > 1800:
            out.append(cur)
            cur = None
        prev_t = t
        if p[3] == "ground":
            if cur:
                cur["end"], cur["last"] = t, (p[1], p[2], 0)
                out.append(cur)
                cur = None
            ground = (t, p[1], p[2])
            continue
        if cur is None:
            if ground and t - ground[0] < 600:
                cur = {"start": ground[0], "first": (ground[1], ground[2], 0)}
            else:
                cur = {"start": t, "first": (p[1], p[2], p[3])}
            cur["callsigns"] = {}
        cur["end"], cur["last"] = t, (p[1], p[2], p[3])
        if cs:
            cur["callsigns"][cs] = cur["callsigns"].get(cs, 0) + 1
    if cur:
        out.append(cur)
    for f in out:
        f["callsign"] = max(f["callsigns"], key=f["callsigns"].get) if f["callsigns"] else None
    return out


def legs(tr: dict, lat0: float, lon0: float, t0: float, t1: float) -> list[tuple]:
    """(direction, epoch, callsign) for flights from/to the airport that were airborne in [t0, t1].

    A flight departs the airport if it starts within 8 nm of it on the ground or below 2500 ft,
    and arrives if it ends there the same way."""
    out = []
    for f in flights(tr):
        if not f["callsign"] or f["end"] < t0 or f["start"] > t1:
            continue
        for direction, (lat, lon, alt), when in (("D", f["first"], f["start"]), ("A", f["last"], f["end"])):
            if isinstance(alt, (int, float)) and alt < 2500 and fw.haversine_nm(lat0, lon0, lat, lon) <= 8:
                out.append((direction, when, f["callsign"]))
    return out


def attach_routes(fixture: dict, source: str = fw.STANDING_DATA) -> None:
    """Add the VRS standing-data route of every callsign in the fixture, plus the positions of the
    airports on those routes, so replays classify traffic offline like flight_watch does live."""
    import requests
    sd = fw.StandingData(fw.Http(min_gap=0.1, rate=6000, rate_max=6000), source)
    callsigns = {p[7] for a in fixture["aircraft"].values() for p in a["points"] if p[7]}
    routes, airports = {}, {}
    for cs in sorted(callsigns):
        try:
            codes = sd.route(cs)
            if codes:
                routes[cs] = "-".join(codes)
                for icao in codes:
                    pos = sd.airport(icao)
                    if pos:
                        airports[icao] = [round(pos[0], 5), round(pos[1], 5)]
        except requests.RequestException as e:
            print(f"  route of {cs} not fetched: {e}", file=sys.stderr)
    fixture["routes"], fixture["airports"] = routes, airports
    fixture["meta"]["routes_source"] = "VRS standing data (CC0), " + source
    print(f"routes: {len(routes)} of {len(callsigns)} callsigns, {len(airports)} airports", file=sys.stderr)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--date", help="UTC date, YYYY-MM-DD")
    p.add_argument("--start", help="UTC window start, HH:MM")
    p.add_argument("--end", help="UTC window end, HH:MM")
    p.add_argument("--airport", default="TLV")
    p.add_argument("--radius", type=float, default=150)
    p.add_argument("--before-h", type=float, default=3, help="hours before the window to look for departures")
    p.add_argument("--after-h", type=float, default=6, help="hours after the window to look for arrivals")
    p.add_argument("--focus", action="append", default=[], help="hex id(s) always included")
    p.add_argument("--board", action="append", default=[],
                   help="extra board row CALLSIGN:A|D:OTHER_IATA:LOCAL_ISO_TIME")
    p.add_argument("--step", type=float, default=5, help="min seconds between stored points")
    p.add_argument("--name", default="")
    p.add_argument("--cache", default=os.path.join(os.path.expanduser("~"), ".cache", "flight-watch"))
    p.add_argument("--standing-data", default=fw.STANDING_DATA,
                   help="VRS standing-data repository (URL or local checkout) for callsign routes")
    p.add_argument("--add-routes", metavar="FIXTURE",
                   help="only add/refresh routes in an existing fixture (rewritten in place)")
    p.add_argument("--out", help="fixture to write (.json.gz)")
    a = p.parse_args()
    if a.add_routes:
        with gzip.open(a.add_routes, "rt", encoding="utf-8") as f:
            fixture = json.load(f)
        attach_routes(fixture, a.standing_data)
        with gzip.open(a.add_routes, "wt", encoding="utf-8") as f:
            json.dump(fixture, f, separators=(",", ":"))
        return
    if not (a.out and a.date and a.start and a.end):
        p.error("--date, --start, --end and --out are required (or use --add-routes)")

    _, lat0, lon0 = fw.AIRPORTS[a.airport]
    day = datetime.strptime(a.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    t0 = (day + timedelta(hours=int(a.start[:2]), minutes=int(a.start[3:]))).timestamp()
    t1 = (day + timedelta(hours=int(a.end[:2]), minutes=int(a.end[3:]))).timestamp()
    tz = ZoneInfo("Asia/Jerusalem")

    # 1) pick aircraft from the heatmaps (same UTC day only)
    near, touch = set(a.focus), {}  # touch: hex -> [(epoch, on_ground)] near the airport
    first = max(0, int((t0 - a.before_h * 3600 - day.timestamp()) // 1800))
    last = min(47, int((t1 + a.after_h * 3600 - day.timestamp()) // 1800))
    for n in range(first, last + 1):
        for ts, h, lat, lon, alt in heatmap(day, n, a.cache):
            if abs(lat - lat0) > 3.5 or abs(lon - lon0) > 4.5:
                continue
            d = fw.haversine_nm(lat0, lon0, lat, lon)
            if t0 <= ts <= t1 and d <= a.radius + 10:
                near.add(h)
            if d <= 8 and (alt == GROUND or 0 <= alt * 25 < 2500):
                touch.setdefault(h, []).append(ts)
        print(f"heatmap {n:02d}: {len(near)} near, {len(touch)} at {a.airport}", file=sys.stderr)

    # flights at the airport outside the window: arrivals landing after it, departures before it
    remote = {h for h, ts in touch.items() if h not in near and (min(ts) > t1 or max(ts) < t0)}
    wanted = sorted(near | remote)
    print(f"{len(near)} aircraft in the circle, {len(remote)} more to/from {a.airport}; "
          f"fetching {len(wanted)} traces", file=sys.stderr)

    # 2) traces
    aircraft, board = {}, []
    for i, h in enumerate(wanted):
        try:
            tr = json.loads(get(f"{BASE}/{day:%Y/%m/%d}/traces/{h[-2:]}/trace_full_{h}.json",
                                os.path.join(a.cache, f"trace_{day:%Y%m%d}_{h}.json")))
        except urllib.error.HTTPError:
            continue
        pts = trace_points(tr, t0, t1, a.step)
        if not any(t0 <= q[0] <= t1 for q in pts):
            continue
        aircraft[h] = {"r": tr.get("r"), "t": tr.get("t"), "desc": tr.get("desc"), "points": pts}
        if i % 20 == 0:
            print(f"  {i}/{len(wanted)} traces", file=sys.stderr)

        # derived board rows: flights leaving / reaching the airport around the window
        for direction, when, callsign in legs(tr, lat0, lon0, t0, t1):
            board.append((callsign, direction, "?",
                          datetime.fromtimestamp(when, tz).strftime("%Y-%m-%dT%H:%M")))

    for row in a.board:
        callsign, direction, other, when = row.split(":", 3)
        board = [b for b in board if b[0] != callsign] + [(callsign, direction, other, when)]

    # board rows in data.gov.il format, as seen at the start of the window
    records = []
    for callsign, direction, other, when in board:
        icao, num = callsign[:3], callsign[3:]
        if not num.isdigit():
            continue  # alphanumeric callsigns (WZZ5TL) never match the board anyway
        iata = fw.ICAO_TO_IATA.get(icao, icao)
        left = datetime.fromisoformat(when).replace(tzinfo=tz).timestamp() < t0
        status = "DEPARTED" if direction == "D" and left else "ON TIME"
        records.append({"CHOPER": iata, "CHFLTN": num, "CHAORD": direction, "CHSTOL": when,
                        "CHPTOL": when, "CHLOC1": other, "CHLOC1D": other, "CHRMINE": status})

    fixture = {
        "meta": {"name": a.name, "airport": a.airport, "date": a.date, "start": t0, "end": t1,
                 "radius": a.radius, "focus": a.focus, "step": a.step,
                 "source": "ADS-B Exchange globe history (heatmap + trace_full)",
                 "board_note": "derived from traces and --board rows, not the real flight board",
                 "point_fields": ["t", "lat", "lon", "alt", "gs", "track", "vrate",
                                  "flight", "squawk", "emergency", "category"]},
        "board": records,
        "aircraft": aircraft,
    }
    attach_routes(fixture, a.standing_data)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with gzip.open(a.out, "wt", encoding="utf-8") as f:
        json.dump(fixture, f, separators=(",", ":"))
    n = sum(len(v["points"]) for v in aircraft.values())
    print(f"wrote {a.out}: {len(aircraft)} aircraft, {n} points, {len(records)} board rows, "
          f"{os.path.getsize(a.out) / 1e6:.1f} MB", file=sys.stderr)


if __name__ == "__main__":
    main()
