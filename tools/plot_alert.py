#!/usr/bin/env python3
"""Replay an alert as a rotatable 3D track (plus altitude over time), with the alerts marked.

Lists alerts.jsonl from newest to oldest, asks which one to plot, fetches that aircraft's trace
and writes a self-contained HTML page (plotly.js from a CDN; nothing else is stored).

Trace sources (readsb trace_full JSON, positions at full rate):
  adsb.lol   https://adsb.lol/data/traces/<last 2 hex>/trace_full_<hex>.json   (recent ~day,
             rewritten only every ~20-30 min) + trace_recent_<hex>.json (the latest minutes)
  ADS-B Exchange globe history, per UTC day, for anything older

Examples:
  python tools/plot_alert.py                  # choose from the list
  python tools/plot_alert.py --pick 1         # newest alert
  python tools/plot_alert.py --pick 3 --minutes 20 --out tmp_plot.html
  python tools/plot_alert.py --filter RETURNED Zurich   # only rows containing both
  python tools/plot_alert.py --pick 1 --map           # over a map, tilt / rotate with right-drag
"""
from __future__ import annotations

import argparse
import base64
import csv
import glob
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import flight_watch as fw  # noqa: E402
from track_segments import (FT, GAP, GLITCH_RGB, KT, NM, PURPLE, T, jumps, points,  # noqa: E402,F401
                            segments, steep)

TZ = ZoneInfo("Asia/Jerusalem")
HEADERS = {"User-Agent": "flight-watch-plot/0.1", "Referer": "https://globe.adsbexchange.com/"}
ADSBLOL = "https://adsb.lol/data/traces/{xx}/trace_{kind}_{hex}.json"  # kind: full / recent
ADSBX = "https://globe.adsbexchange.com/globe_history/{day:%Y/%m/%d}/traces/{xx}/trace_full_{hex}.json"
PLOTLY = "https://cdn.plot.ly/plotly-2.35.2.min.js"
# the project's logo as the tab icon, embedded: the pages stand alone (docs/, examples/, tmp files)
LOGO = os.path.join(os.path.dirname(__file__), "..", "docs", "logo.png")
# a silence this long (or a ground stop) ends a flight leg. Not readsb's "new leg" flag: it is set
# on every reappearance after a low-level silence - 4XDAN (skydiving, 10 Oct 2026) came back after
# each 13-40 min below coverage, and its plot showed one lift of the morning's session.
# A silence that starts and ends low is an unseen landing: one of LOW_GAP or more ends the leg (AEE941,
# 10 Oct 2026: lost at 3,100 ft into TLV, back 81 min later at 2,875 ft as the return flight). With
# either end high it is a coverage gap in flight (AUA98 the same night: lost climbing out of Amman at
# 8,050 ft, heard again 96 min later at 38,025 ft), unless longer than any flight's.
LEG_GAP, LOW_GAP, LOW_ALT = 6 * 3600, 45 * 60, 10000
# Alerts are drawn by group: each its own colour and marker (plotly 3D symbol / the same shape as a
# character on the map), switched on and off - marker and text separately - above the plot.
# (key, name, colour, 3D symbol, map glyph, kinds); a kind in none of them goes to "other"
GROUPS = [
    ("emergency", "emergency squawk", "#8b0000", "square", "■", {"EMERGENCY"}),
    ("altitude", "altitude (rate, wobble, early descent) / skydive", "#d62728", "diamond", "◆",
     {"VERTICAL_RATE", "SKYDIVE_PATTERN", "ALTITUDE_WOBBLE", "EARLY_DESCENT"}),
    ("trace", "vertical rate in the trace, not alerted", "#9467bd", "diamond-open", "◇", {"TRACE_VERTICAL_RATE"}),
    ("unwatched", "vertical rate in the trace, monitor not running", "#a0a0a0", "circle", "●", {"TRACE_NO_MONITOR"}),
    ("course", "course / turn / heading", "#c2185b", "cross", "✚",
     {"COURSE_CHANGE", "SHARP_TURN", "OFF_COURSE", "TURNING_BACK", "TOWARD_ISRAEL", "HOLDING"}),
    ("landing", "diversion / return", "#8c564b", "square-open", "□", {"DIVERSION", "RETURNED"}),
    ("gps", "position jump / GPS", "#e67e00", "x", "✕", {"POSITION_JUMP", "GPS_SPOOFING", "GPS_DEGRADED"}),
    ("contact", "lost / restored contact", "#7f7f7f", "circle-open", "○",
     {"LOST_CONTACT", "CONTACT_RESTORED", "MASS_SILENCE"}),
    # not an alert: an event of the incident kept in the fixture's meta (import_911.py)
    ("event", "transponder switched off", "#000000", "circle", "⊘", {"TRANSPONDER_OFF"}),
    ("goaround", "aborted landing", "#1f77b4", "circle", "↺", {"ABORTED_LANDING"}),  # (tools/go_around_maps.py: not an alert)
    ("other", "other", "#17becf", "circle", "●", set()),
]
GROUP_OF = {k: g[0] for g in GROUPS for k in g[5]}
# dives / climbs the trace shows but no alert came for (see vrate_episodes): menu name, label
TRACE_NAMES = {"TRACE_VERTICAL_RATE": ("VERTICAL_RATE not alerted (trace)", "not alerted"),
               "TRACE_NO_MONITOR": ("VERTICAL_RATE, monitor not running (trace)", "no monitor")}
TEXT_OFF_OVER = 4  # contact labels start hidden when there are more than this (4XDAN: 8 of 10)


def favicon() -> str:
    if not os.path.exists(LOGO):
        return ""
    with open(LOGO, "rb") as f:
        return f'\n<link rel="icon" type="image/png" href="data:image/png;base64,{base64.b64encode(f.read()).decode()}">'


def epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def local(t: float, fmt: str = "%H:%M:%S") -> str:
    return datetime.fromtimestamp(t, TZ).strftime(fmt)


def load_alerts(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()][::-1]  # newest first


def describe(rec: dict) -> str:
    ident = rec.get("flight") or rec.get("callsign") or rec["hex"].upper()
    if rec.get("callsign") and rec["callsign"] != ident:
        ident += f"/{rec['callsign']}"  # LY347/ELY347: either can be filtered on
    extra = ", ".join(x for x in (rec.get("airline"), "military" if rec.get("military") else None,
                                  rec.get("reg"), rec.get("type")) if x)
    route = rec.get("route_text") or "route unknown"
    when = datetime.fromisoformat(rec["time"].replace("Z", "+00:00")).astimezone(TZ)
    return (f"{when:%d %b %H:%M} {rec['kind']:<16} {ident} ({extra}) {route} - "
            f"{rec['message'].split(' [also:')[0][:70]}")


def choose(alerts: list[dict], pick: int | None, terms: list[str]) -> dict:
    """The list in a pager (newest first, numbers stay those of the full list), then a prompt.
    `terms`: show only rows containing all of them (case-insensitive)."""
    if pick is None:
        rows = [f"{i:4d}  {describe(rec)}" for i, rec in enumerate(alerts, 1)]
        rows = [r for r in rows if all(t.lower() in r.lower() for t in terms)]
        if not rows:
            sys.exit(f"no alerts match {' '.join(terms)!r}")
        text = "\n".join(rows) + "\n"
        if sys.stdout.isatty() and shutil.which("less"):
            # -F: no pager when it fits one screen, -X: the list stays visible after quitting (q)
            subprocess.run(["less", "-FX"], input=text, text=True)
        else:
            print(text, end="")
        first = rows[0].split()[0]
        answer = input(f"plot which? [{first}] ").strip() or first
        pick = int(answer)
    if not 1 <= pick <= len(alerts):
        sys.exit(f"no alert number {pick} (1..{len(alerts)})")
    return alerts[pick - 1]


def fetch(url: str) -> dict | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=60)
    except requests.RequestException as e:
        print(f"  {url}: {e}", file=sys.stderr)
        return None
    if r.status_code != 200:
        print(f"  {url}: HTTP {r.status_code}", file=sys.stderr)
        return None
    try:
        return r.json()
    except ValueError:
        return None


_NAMED: dict | None = None
CITY_FIX = {"ETM": "Ramon", "LCA": "Larnaca"}  # the standing data's "Southern District", "Larnarca"


def named_airports() -> dict:
    """IATA -> (lat, lon, city, ICAO) of the airports with an IATA code in the local standing-data checkout."""
    global _NAMED
    if _NAMED is None:
        _NAMED = {}
        for name in glob.glob(os.path.join(os.path.dirname(__file__), "..", "standing-data", "airports", "schema-01",
                                           "*", "*.csv")):
            with open(name, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    try:
                        if row.get("IATA"):
                            _NAMED[row["IATA"]] = (float(row["Latitude"]), float(row["Longitude"]),
                                                   row.get("Location") or row.get("Name") or "", row["ICAO"])
                    except (KeyError, TypeError, ValueError):
                        continue
    return _NAMED


def place_labels(air: list[dict], mine: list[dict], places: dict) -> dict:
    """Map labels "SOF Sofia" -> (lat, lon): the given places (IATA codes get their city), the airports the flight
    took off from and landed at, those named in its alerts (ICAO codes) and the nearest to each alert (40 nm).
    The map's own place names are small at the zoom of a whole flight (LY5102 Warsaw - Sofia)."""
    named = named_airports()
    by_icao = {v[3]: k for k, v in named.items()}
    out, seen = {}, set()

    def add(code, lat, lon):
        if code in seen:
            return
        seen.add(code)
        city = CITY_FIX.get(code) or (named[code][2] if code in named else "")
        out[f"{code} {city}" if city and city.lower() != code.lower() else code] = (lat, lon)

    def nearest(lat, lon, nm):
        d, code = min(((fw.haversine_nm(lat, lon, v[0], v[1]), k) for k, v in named.items()
                       if abs(v[0] - lat) < 1 and abs(v[1] - lon) < 1.5), default=(1e9, None))
        return code if d <= nm else None

    for p in (air[0], air[-1]):  # the leg's ends: where it took off / landed when low there
        code = nearest(p["lat"], p["lon"], 15) if (p["alt"] or 0) < 10000 else None
        if code:
            add(code, *named[code][:2])
    for a in mine:
        for icao in re.findall(r"\b[A-Z]{4}\b", a.get("message") or ""):
            if icao in by_icao:
                add(by_icao[icao], *named[by_icao[icao]][:2])
        code = isinstance(a.get("lat"), (int, float)) and nearest(a["lat"], a["lon"], 40)
        if code:
            add(code, *named[code][:2])
    for code, v in places.items():  # after the flight's own: where names would overlap, those are kept
        add(code, v[0], v[1])
    return out


def airfields() -> list[tuple[float, float, int]]:
    """(lat, lon, elevation ft) of the airports in the local standing-data checkout; [] without it.
    Heliports are left out: many are on roofs (2.2 nm from PDX at 296 ft, 2.5 nm from EWR at 1,041 ft)."""
    out = []
    for name in glob.glob(os.path.join(os.path.dirname(__file__), "..", "standing-data", "airports", "schema-01",
                                       "*", "*.csv")):
        with open(name, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if re.search(r"heli", row.get("Name") or "", re.I):
                    continue
                try:
                    out.append((float(row["Latitude"]), float(row["Longitude"]), int(float(row["AltitudeFeet"]))))
                except (KeyError, TypeError, ValueError):
                    continue
    return out


def on_terrain(pts: list[dict], fields: list[tuple[float, float, int]]) -> int:
    """Fit the track to the map's terrain (heights above sea level). ADS-B sends no altitude on the ground
    ("ground", or 0 in some exports): ground reports go at the elevation of the airfield they are at (within 3 nm), else at the
    nearest airborne report's. Its altitude is pressure altitude (1013 hPa), off the true one by the
    weather (~30 ft per hPa) or a failing air-data sensor: ET302 left Addis Ababa (7,630 ft) at "7,200 ft".
    A track below the airfield it took off from or landed at is raised by the difference (returned, ft)."""
    near = {}
    def field(p):
        k = (round(p["lat"], 3), round(p["lon"], 3))
        if k not in near:
            d, f = min(((fw.haversine_nm(la, lo, p["lat"], p["lon"]), (la, lo, e)) for la, lo, e in fields
                        if abs(la - p["lat"]) < 0.06 and abs(lo - p["lon"]) < 0.08), default=(99, None))
            near[k] = f if d <= 3 else None
        return near[k]
    for p in pts:  # FR24's CSVs send 0 on the take-off roll (ET302)
        p["ground"] = p["ground"] or p["alt"] == 0
    air = [p for p in pts if not p["ground"] and isinstance(p["alt"], (int, float))]
    lift = 0
    for end in (air[:1], air[-1:]):  # the take-off and landing airfields
        f = field(end[0]) if end else None
        low = [p["alt"] for p in air if f is not None and field(p) == f]
        if low and min(low) < f[2]:
            lift = max(lift, f[2] - min(low))
    for p in air:
        p["alt"] += lift
    for p in pts:
        if p["ground"]:
            f = field(p)
            p["alt"] = f[2] if f is not None else min(air, key=lambda q: abs(q["t"] - p["t"]))["alt"] if air else 0
    return lift


def trace_for(hexid: str, t: float) -> tuple[list[dict], str]:
    """Positions of the aircraft around time t: adsb.lol's recent trace if it reaches back to t,
    else ADS-B Exchange's history of that UTC day (and the day before, for legs over midnight)."""
    xx = hexid[-2:]
    pts = []
    for kind in ("full", "recent"):  # full lags by up to ~half an hour; recent fills the gap
        tr = fetch(ADSBLOL.format(xx=xx, kind=kind, hex=hexid))
        pts += points(tr) if tr else []
    pts = list({round(p["t"], 1): p for p in sorted(pts, key=lambda p: p["t"])}.values())
    if pts and pts[0]["t"] <= t - 600:
        return pts, "adsb.lol"
    day = datetime.fromtimestamp(t, timezone.utc)
    pts = []
    for d in (day - timedelta(days=1), day):
        if d.date() == day.date() or t - d.replace(hour=23, minute=59).timestamp() < 6 * 3600:
            tr = fetch(ADSBX.format(day=d, xx=xx, hex=hexid))
            pts += points(tr) if tr else []
    return sorted(pts, key=lambda p: p["t"]), "ADS-B Exchange history"


def fixture_points(fixture: dict, hexid: str) -> list[dict]:
    """A replay fixture's rows of one aircraft (tests/replay.py, tools/capture_incident.py,
    tools/import_track.py) in the shape of points(); callsign and squawk carry on until they change."""
    out, state = [], {}
    for t, lat, lon, alt, gs, trk, vr, flight, squawk, _, _ in fixture["aircraft"][hexid]["points"]:
        state.update({k: v for k, v in (("flight", flight), ("squawk", squawk)) if v})
        out.append({"t": t, "lat": lat, "lon": lon, "alt": 0 if alt == "ground" else alt, "ground": alt == "ground",
                    "geo": False, "gs": gs, "track": trk, "new_leg": False, "vrate": vr, "src": "adsb", **state})
    return out


def replay_alerts(path: str) -> tuple[dict, list[dict]]:
    """The fixture and the alerts the monitor raises on it now (newest first, as load_alerts)."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))
    import replay
    fixture = replay.load(path)
    print(f"replaying {path} ...", file=sys.stderr)
    alerts, *_ = replay.run(fixture)
    return fixture, alerts[::-1]


def apart(pts: list[dict], k: int) -> bool:
    """pts[k - 1] and pts[k] belong to different flights."""
    a, b = pts[k - 1], pts[k]
    gap = b["t"] - a["t"]
    if gap >= LEG_GAP or (gap >= LOW_GAP and (a["alt"] or 0) < LOW_ALT and (b["alt"] or 0) < LOW_ALT):
        return True
    if gap >= LOW_GAP:  # a new callsign after the silence: landed and left again unseen (LMU625 ->
        # LMU626, 4 h at cruise). The new one shows up a little after the first position (a trace
        # carries the callsign only when it changes); in flight it alone means nothing (PGT1885 ->
        # PGT7DR 18 s apart at FL370).
        after = next((p.get("flight") for p in pts[k:] if p["t"] > b["t"] + 60), b.get("flight"))
        return bool(a.get("flight") and after and after != a["flight"])
    return False


def leg(pts: list[dict], t: float) -> list[dict]:
    """The flight around t: between ground stops seen and silences that end a flight (apart()).
    Shorter silences stay in (drawn dashed), even when the aircraft may have landed unseen
    meanwhile (a jump plane's reloads)."""
    if not pts:
        return []
    i = min(range(len(pts)), key=lambda k: abs(pts[k]["t"] - t))
    lo = hi = i
    while lo > 0 and not apart(pts, lo):
        lo -= 1
        if pts[lo]["ground"] and pts[lo]["t"] < t - 600:  # take-off point
            break
    while hi < len(pts) - 1 and not apart(pts, hi + 1):
        hi += 1
        if pts[hi]["ground"] and pts[hi]["t"] > t + 600:  # landed
            break
    return pts[lo:hi + 1]


def m(ft) -> str:
    return f"{ft * FT:,.0f} m"


def metric(msg: str) -> str:
    """The monitor's alert text (ft, kt, nm, ft/min) in metric units."""
    num = r"(-?\d[\d,]*(?:\.\d+)?)"
    val = lambda g: float(g.replace(",", ""))
    msg = re.sub(r"\+?" + num + r" ft/min", lambda x: f"{val(x.group(1)) * FT / 60:+.0f} m/s", msg)
    msg = re.sub(num + r" ft\b", lambda x: f"{val(x.group(1)) * FT:,.0f} m", msg)
    km = lambda v: f"{v * NM:,.1f}" if v * NM < 100 else f"{v * NM:,.0f}"
    msg = re.sub(num + r" -> " + num + r" nm\b",  # a range: "(130 -> 141 nm)"
                 lambda x: f"{km(val(x.group(1)))} -> {km(val(x.group(2)))} km", msg)
    msg = re.sub(num + r" nm\b", lambda x: f"{km(val(x.group(1)))} km", msg)
    msg = re.sub(num + r" kt\b", lambda x: f"{val(x.group(1)) * KT:,.0f} km/h", msg)
    return re.sub(r"(\d) deg\b", r"\1°", msg)


def vrate_episodes(air: list[dict], rec: dict) -> list[dict]:
    """flight_watch's VERTICAL_RATE check (Monitor.check_vrate: angle at a trusted speed, --vrate, backed
    by the altitude history) run on every report of the trace, not only on those a poll happened to
    get. Reports that pass it <= GAP apart in one direction form an episode: its first passing report
    (the first moment the monitor could have alerted) and its strongest values. 4XDAN, 10 Oct 2026:
    7 dives passed, the live run (half its polls refused with HTTP 429) alerted 2."""
    probe = object.__new__(fw.Monitor)  # only the check: no feed, no notifier
    probe.args = fw.parse_args(["--no-routes"])
    probe.traces, probe.trace_left, probe.from_trace = {}, 0, False  # never reads the feed's trace
    probe.standing = None  # (check_vrate asks it for airports far from the region: not here)
    hits = []
    probe.alert = lambda t, kind, msg, key=None, severity=0, **_: hits.append(
        {"i": i, "dir": key.split(":")[1], "rate": t.last.vrate,
         "angle": fw.path_angle(t.last.vrate, gs) if (gs := probe.trusted_gs(t)) else None, "msg": msg})
    t = fw.Track(rec["hex"], callsign=rec.get("callsign") or "", reg=rec.get("reg") or "",
                 actype=rec.get("type") or "")
    for i, p in enumerate(air):
        t.samples.append(fw.Sample(p["t"], p["lat"], p["lon"], p["alt"], p["track"], p["gs"], p["vrate"],
                                   p["ground"]))
        fw.Monitor.check_vrate(probe, t)
    eps = []
    for h in hits:
        e = eps[-1] if eps else None
        if e and e["dir"] == h["dir"] and air[h["i"]]["t"] - air[e["hits"][-1]["i"]]["t"] <= GAP:
            e["hits"].append(h)
        else:
            eps.append({"dir": h["dir"], "hits": [h]})
    for e in eps:
        hs = e["hits"]
        e["first"], e["t"], e["end"] = hs[0], air[hs[0]["i"]]["t"], air[hs[-1]["i"]]["t"]
        e["rate"] = max((h["rate"] for h in hs), key=abs)
        e["angle"] = max((h["angle"] for h in hs if h["angle"] is not None), key=abs, default=None)
        ang = f", {e['angle']:+.1f} deg" if e["angle"] is not None else ""
        e["text"] = metric(f"in the trace: passes the check first at {T(e['t'])} ({hs[0]['msg']}), {len(hs)} "
                           f"reports pass until {T(e['end'])}, strongest {e['rate']:+d} ft/min{ang}")
    return eps


SOURCES = (("adsb", "ADS-B (the aircraft's GPS)", "#1f77b4", [31, 119, 180]),
           ("mlat", "MLAT (ground receivers)", "#2ca02c", [44, 160, 44]))  # (orange is for jumps)


def source_lines(air: list[dict]) -> dict[str, list[tuple[dict, dict]]]:
    """For --sources: each position source as its own line - consecutive points of that source,
    broken where it has none for over 3 min - so GPS and MLAT positions can be compared."""
    out = {}
    for src, *_ in SOURCES:
        sel = [p for p in air if p["src"] == src]
        out[src] = [(p, q) for p, q in zip(sel, sel[1:]) if q["t"] - p["t"] <= 180]
    return out


def mlat_covered(air: list[dict]):
    """For --sources: does MLAT cover the moment of a GPS segment (an MLAT point or stretch within
    30 s)? Then the GPS line is drawn dotted - the ground-based position is there to compare."""
    spans = [(p["t"] - 30, q["t"] + 30) for p, q in source_lines(air)["mlat"]]
    spans += [(p["t"] - 30, p["t"] + 30) for p in air if p["src"] == "mlat"]
    return lambda p, q: any(a <= q["t"] and p["t"] <= b for a, b in spans)


def hover(p: dict) -> str:
    alt = "ground" if p["ground"] else m(p["alt"]) if p["alt"] is not None else "alt ?"
    if p.get("geo"):
        alt += " (from GPS altitude)"
    parts = [f"{T(p['t'])} {T(p['t'], 'z')}",
             alt, f"{p['gs'] * KT:,.0f} km/h" if p["gs"] is not None else "",
             f"track {p['track']:.0f}°" if p["track"] is not None else "",
             f"{p['vrate'] * FT / 60:+.1f} m/s" if isinstance(p["vrate"], int) else "",
             f"{p['src']}" + (f", NIC {p['nic']}" if p.get("nic") is not None and p["src"] == "adsb" else ""),
             f"sq {p.get('squawk')}" if p.get("squawk") else ""]
    return "<br>".join(x for x in parts if x)


VIRIDIS = [(68, 1, 84), (59, 82, 139), (33, 145, 140), (94, 201, 98), (253, 231, 37)]
DECK = "https://unpkg.com/deck.gl@9.1.14/dist.min.js"
# deck.gl's bundle decodes terrain only in a worker it fetches itself (never ran in headless Chrome);
# the loader of its loaders.gl version, parsing on the page
TERRAIN_LOADER = "https://unpkg.com/@loaders.gl/terrain@4.2.1/dist/dist.min.js"
# Keyless map tiles that also load from a local file (tile.openstreetmap.org blocks pages without a
# Referer - "Access blocked" - and CARTO now needs an API key). --tiles takes any {z}/{x}/{y} URL.
TILES = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}"
# The ground's height (Mapzen / AWS open terrain tiles, terrarium PNG, metres, CORS *): flight altitudes are
# above sea level, so on a flat map a high airfield's take-off started in the air (ET302 at Addis Ababa,
# 7,630 ft). Below sea level (sea floor) is drawn at 0. Zoom 15 at most.
TERRAIN = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
TERRAIN_CREDIT = ('Elevation: <a href="https://registry.opendata.aws/terrain-tiles/">Terrain Tiles</a> (Mapzen, AWS '
                  'Open Data: SRTM, GMTED, ETOPO1, NED, 3DEP, EU-DEM and others)')
# Esri's attribution: "Powered by Esri" linked to esri.com, plus the service's copyrightText
TILES_CREDIT = ('Map: Powered by <a href="https://www.esri.com">Esri</a> | Sources: Esri, HERE, Garmin, USGS, '
                'Intermap, INCREMENT P, NRCan, Esri Japan, METI, Esri China (Hong Kong), Esri Korea, '
                'Esri (Thailand), NGCC, © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> '
                'contributors, and the GIS User Community')
DATA_CREDIT = {"ADS-B Exchange history": 'Flight data: <a href="https://www.adsbexchange.com">ADS-B Exchange</a>',
               "adsb.lol": 'Flight data: <a href="https://adsb.lol">adsb.lol</a> '
                           '(<a href="https://opendatacommons.org/licenses/odbl/">ODbL</a>)'}
OTHER_CREDITS = ('Routes: Israel Airports Authority flight board '
                 '(<a href="https://data.gov.il">data.gov.il</a>) and Virtual Radar Server '
                 '<a href="https://github.com/vradarserver/standing-data">standing data</a>. Alerts: '
                 '<a href="https://github.com/yuval-harpaz/flight-watch">flight-watch</a>')


def viridis(f: float) -> list[int]:
    f = min(max(f, 0.0), 1.0) * (len(VIRIDIS) - 1)
    i = min(int(f), len(VIRIDIS) - 2)
    return [round(a + (b - a) * (f - i)) for a, b in zip(VIRIDIS[i], VIRIDIS[i + 1])]


def build(rec: dict, alerts: list[dict], pts: list[dict], source: str, max_speed: float = 1200,
          gap_speed: float = 750, airport: str = "TLV", use_map: bool = False, tiles: str = TILES,
          sources: bool = False, origin: tuple | None = None, base: float = 0) -> str:
    t0, t1 = pts[0]["t"], pts[-1]["t"]
    mine = [a for a in alerts if a["hex"] == rec["hex"] and t0 - 60 <= epoch(a["time"]) <= t1 + 60]
    if rec not in mine:
        mine.append(rec)
    air = [p for p in pts if p["alt"] is not None]
    # when the trace first passed the VERTICAL_RATE check, per episode: the alert's "first detection",
    # or an episode no alert came for (polls refused, a dive between polls)
    eps = vrate_episodes(air, rec)
    for a in mine:
        if a["kind"] == "VERTICAL_RATE":
            ta, way = epoch(a["time"]), a["message"].split()[0]
            a["_det"] = next((e for e in eps if e["dir"] == way and "alert" not in e
                              and e["t"] - 60 <= ta <= e["end"] + GAP), None)
            if a["_det"]:
                a["_det"]["alert"] = a
    ran = [epoch(a["time"]) for a in alerts]  # the alert file's span: the monitor ran then (roughly)
    for e in eps:
        if "alert" not in e:
            p = air[e["first"]["i"]]
            out = not min(ran) - 600 <= e["t"] <= max(ran)
            mine.append({"hex": rec["hex"], "kind": "TRACE_NO_MONITOR" if out else "TRACE_VERTICAL_RATE", "lat": p["lat"], "lon": p["lon"],
                         "alt": p["alt"], "time": datetime.fromtimestamp(e["t"], timezone.utc).strftime(
                             "%Y-%m-%dT%H:%M:%S.%fZ"),
                         "message": ("outside the alert file's time span; " if out else "not alerted; ") + e["text"]})
    mine.sort(key=lambda a: epoch(a["time"]))
    if rec.get("military"):
        gap_speed = max_speed  # as the monitor: jets may really be that fast
    bad = jumps(air, max_speed, gap_speed)
    gaps = [(air[i - 1]["t"], air[i]["t"]) for i in range(1, len(air)) if air[i]["t"] - air[i - 1]["t"] > GAP]

    # distances in km east / north of the airport (equirectangular: fine at these ranges)
    lat0, lon0 = origin or (fw.AIRPORTS[airport][1:] if airport in fw.AIRPORTS else (air[0]["lat"], air[0]["lon"]))
    # the region's airports and an incident's own watched point, the flight's own airports, with their cities
    places = place_labels(air, mine, {**fw.REGION_AIRPORTS, airport: (lat0, lon0)})
    kx = 111.32 * math.cos(math.radians(sum(p["lat"] for p in air) / len(air)))

    def km(lat, lon):
        return round((lon - lon0) * kx, 3), round((lat - lat0) * 110.57, 3)
    for p in air:
        p["x"], p["y"] = km(p["lat"], p["lon"])
    for a in mine:
        a["_x"], a["_y"] = km(a["lat"], a["lon"])
        alt = a.get("alt")
        a["_alt"] = alt if isinstance(alt, (int, float)) else min(air, key=lambda p: abs(p["t"] - epoch(a["time"])))["alt"]
        code = re.search(r"squawk (\d{4})", a["message"]) if a["kind"] == "EMERGENCY" else None
        a["_label"] = f"{a['kind']}{' ' + code.group(1) if code else ''} {T(epoch(a['time']))}"
        a["_msg"] = metric(a["message"])
        if a["kind"] in TRACE_NAMES:
            a["_label"] = f"{TRACE_NAMES[a['kind']][1]} {T(epoch(a['time']))}"
        e = a.get("_det")
        if e:  # where / when the trace first passed the check (the lag: in the tooltip)
            p, lag = air[e["first"]["i"]], round(epoch(a["time"]) - e["t"])  # alert times: whole seconds
            a["_d"] = {"t": e["t"], "x": p["x"], "y": p["y"], "z": p["alt"] * FT, "lon": p["lon"], "lat": p["lat"],
                       "label": f"{a['kind']} {T(e['t'])}"}
            a["_msg"] += (f"<br><i>{e['text']}; {abs(lag):.0f} s {'before' if lag >= 0 else 'after'} this alert</i>")
        elif a["kind"] == "VERTICAL_RATE":
            a["_msg"] += "<br><i>in the trace: no report passes the check near it</i>"
        a["_g"] = GROUP_OF.get(a["kind"], "other")
    # labels of alerts raised at about the same place / time are stacked in the page, among the
    # labels shown (e.g. CONTACT_RESTORED, POSITION_JUMP and COURSE_CHANGE when a flight reappears)
    count = {g[0]: sum(1 for a in mine if a["_g"] == g[0]) for g in GROUPS}
    groups = [{"key": k, "name": n, "color": c, "symbol": sym, "glyph": gl, "n": count[k],
               "rgb": [int(c[i:i + 2], 16) for i in (1, 3, 5)],
               "text": not (k == "contact" and count[k] > TEXT_OFF_OVER and count[k] < len(mine))}
              for k, n, c, sym, gl, _ in GROUPS if count[k]]
    # the first view: everything incl. the alert positions, at least 100 km across, 0-40,000 ft
    ax, ay = [p["x"] for p in air] + [a["_x"] for a in mine], [p["y"] for p in air] + [a["_y"] for a in mine]
    span = max(100.0, 1.1 * max(max(ax) - min(ax), max(ay) - min(ay)))
    cx, cy = (max(ax) + min(ax)) / 2, (max(ay) + min(ay)) / 2
    xr, yr = [cx - span / 2, cx + span / 2], [cy - span / 2, cy + span / 2]
    ztop = max(40000, max(p["alt"] for p in air) * 1.05) * FT  # m

    vert, signs = steep(air)
    glitches = [c for c in signs if c["glitch"]]
    signs = [c for c in signs if not c["glitch"]]
    for c in glitches:
        c["first"], c["label"] = False, "altitude glitch " + T(c["t"])
    first_alert = min(epoch(a["time"]) for a in mine)
    early = [c for c in signs if first_alert - 600 <= c["t"] < first_alert]
    for c in signs:  # the earliest steep change in the 10 min before the first alert
        c["first"] = bool(early) and c is early[0]
        c["label"] = ("first sign " if c["first"] else "altitude ") + T(c["t"])

    fx = [a["_x"] for a in mine] + [air[c["start"]]["x"] for c in signs if c["t"] >= first_alert - 600]
    fy = [a["_y"] for a in mine] + [air[c["start"]]["y"] for c in signs if c["t"] >= first_alert - 600]
    fspan = max(100.0, 1.3 * max(max(fx) - min(fx), max(fy) - min(fy)))
    fcx, fcy = (max(fx) + min(fx)) / 2, (max(fy) + min(fy)) / 2
    focus_km = [[fcx - fspan / 2, fcx + fspan / 2], [fcy - fspan / 2, fcy + fspan / 2]]
    flon = [a["lon"] for a in mine] + [air[c["start"]]["lon"] for c in signs if c["t"] >= first_alert - 600]
    flat = [a["lat"] for a in mine] + [air[c["start"]]["lat"] for c in signs if c["t"] >= first_alert - 600]
    focus_ll = [[min(flon), min(flat)], [max(flon), max(flat)]]

    segs = {g["i"]: (g["kind"], g["text"]) for g in segments(air, bool(rec.get("military")), max_speed, gap_speed)}
    hops = [(i, *segs.get(i, ("track", None))) for i in range(1, len(air))]

    # ground speed: reported where the feed has it (MLAT positions usually don't), else the
    # straight-line distance covered in the last ~60 s (zig-zag noise cancels), skipping jumps
    derived = []
    for i, q in enumerate(air):
        k = i
        while k > 0 and q["t"] - air[k - 1]["t"] <= 60 and k not in bad:
            k -= 1
        dt = q["t"] - air[k]["t"]
        derived.append(round(fw.haversine_nm(air[k]["lat"], air[k]["lon"], q["lat"], q["lon"]) / (dt / 3600) * KT)
                       if dt >= 30 else None)
    reported = [None if p["gs"] is None else round(p["gs"] * KT) for p in air]
    top = max([v for v in reported + derived if v is not None] or [500])

    ident = rec.get("flight") or rec.get("callsign") or rec["hex"].upper()
    extra = ", ".join(x for x in (rec.get("airline"), "military" if rec.get("military") else None,
                                  rec.get("reg"), rec.get("type"), rec["hex"]) if x)
    title = f"{ident} ({extra}) {rec.get('route_text') or 'route unknown'}"
    sub = (f"{T(t0, 'dhm')}–{T(t1, 'hm')} {T(t1, 'z')}, {len(pts)} positions from {source}. "
           f"Selected: {rec['kind']} - {metric(rec['message'])}")

    def tip(p):
        return hover(p) + f"<br>{p['lat']:.4f}, {p['lon']:.4f}<br>" \
               f"{fw.haversine_nm(lat0, lon0, p['lat'], p['lon']) * NM:.0f} km from {airport}"

    if use_map:
        view = deck_view(air, hops, mine, rec, signs, tip, tiles, focus_ll, sources, glitches, places, base)
    else:
        view = plotly_view(air, hops, mine, rec, signs, xr, yr, ztop, airport, title, sub, tip, km, focus_km,
                           sources, glitches, places)

    def iso(t):  # epoch: the page turns it into wall-clock time in the zone picked
        return t
    when = [p["t"] for p in air]
    t2 = [{"type": "scatter", "mode": "lines+markers", "name": "altitude", "x": when,
           "y": [round(p["alt"] * FT) for p in air], "marker": {"size": 3},
           "text": [hover(p) for p in air], "hovertemplate": "%{text}<extra></extra>"},
          {"type": "scatter", "mode": "lines", "name": "ground speed from positions (km/h)", "x": when,
           "y": derived, "yaxis": "y2", "line": {"width": 1, "color": "#aaa", "dash": "dot"},
           "hoverinfo": "skip"},
          {"type": "scatter", "mode": "lines", "name": "reported ground speed (km/h)", "x": when,
           "y": reported, "yaxis": "y2", "line": {"width": 1.5, "color": "#555"}, "hoverinfo": "skip"}]
    # jumps orange, gaps without positions shaded; the alerts (lines in their group's colour) are
    # added in the page, as their groups are switched on
    notes = []
    shapes = [{"type": "line", "x0": iso(air[i]["t"]), "x1": iso(air[i]["t"]), "yref": "paper", "y0": 0,
                "y1": 1, "layer": "below", "line": {"color": "#ff9900", "width": 2}} for i in sorted(bad)]
    shapes += [{"type": "line", "x0": iso(c["t"]), "x1": iso(c["t"]), "yref": "paper", "y0": 0, "y1": 1,
                "name": "_steep", "line": {"color": "rgb(155,48,217)", "width": 2 if c["first"] else 1}} for c in signs]
    shapes += [{"type": "line", "x0": iso(c["t"]), "x1": iso(c["t"]), "yref": "paper", "y0": 0, "y1": 1,
                "name": "_glitch", "line": {"color": "rgb(%d,%d,%d)" % tuple(GLITCH_RGB), "width": 1, "dash": "dot"}}
               for c in glitches]
    notes += [{"x": iso(c["t"]), "y": 1, "yref": "paper", "text": c["label"], "showarrow": False, "textangle": -90,
               "name": "_steep",
               "xanchor": "right", "yanchor": "top", "font": {"color": "rgb(155,48,217)"},
               "bgcolor": "rgba(255,255,255,0.8)"} for c in signs if c["first"]]
    shapes += [{"type": "rect", "x0": iso(a), "x1": iso(b), "yref": "paper", "y0": 0, "y1": 1,
                "layer": "below", "fillcolor": "rgba(128,128,128,0.15)", "line": {"width": 0}} for a, b in gaps]
    layout2d = {"height": 340, "margin": {"l": 60, "r": 60, "t": 20, "b": 40}, "shapes": shapes,
                "annotations": notes, "hovermode": "x", "uirevision": "keep",
                "yaxis": {"title": "altitude (m)", "rangemode": "tozero"},
                "yaxis2": {"title": "km/h", "overlaying": "y", "side": "right", "showgrid": False,
                           "range": [0, top * 1.05]},
                "legend": {"orientation": "h"}}
    # chart presets: whole flight, the alerts, each steep altitude change (seconds long: invisible otherwise)
    presets = [("whole flight", None),
               ("alerts", [iso(min(epoch(a["time"]) for a in mine + [rec]) - 120),
                           iso(max(epoch(a["time"]) for a in mine + [rec]) + 120)])]
    presets += [(c["label"], [iso(c["t"] - 30), iso(air[c["end"]]["t"] + 30)]) for c in signs]
    chart_buttons = " ".join(f'<button class="cz tz" data-i="{i}" data-raw="{name}">{name}</button>'
                             for i, (name, _) in enumerate(presets))
    alerts_js = [{"t": epoch(a["time"]), "g": a["_g"], "kind": a["kind"], "label": a["_label"], "sel": a is rec,
                  "tip": f"<b>{a['_label']}</b><br>{a['_msg']}", "x": a["_x"], "y": a["_y"],
                  "z": a["_alt"] * FT, "lon": a["lon"], "lat": a["lat"], **({"d": a["_d"]} if "_d" in a else {})}
                 for a in mine]
    # the "show" menu: each alert kind (marker / text), the steep altitude changes and the data glitches
    kinds = [(k, g) for g in groups for k in sorted({a["kind"] for a in mine if a["_g"] == g["key"]})]
    many = {a["kind"] for a in mine if a["_g"] == "contact"} if any(not g["text"] for g in groups) else set()
    rows = [(k, g["color"], f'{g["glyph"]} {TRACE_NAMES[k][0] if k in TRACE_NAMES else k}', sum(1 for a in mine if a["kind"] == k), k not in many)
            for k, g in kinds]
    rows += [(key, col, name, n, True) for key, col, name, n in
             (("_steep", "#9b30d9", "━ steep altitude change", len(signs)),
              ("_glitch", "rgb(%d,%d,%d)" % tuple(GLITCH_RGB), "━ altitude data glitch", len(glitches))) if n]
    menu = "".join(
        f'<div class="row" style="color:{col}"><label><input type="checkbox" data-k="{key}" data-w="show" '
        f'checked> {name} ({n})</label><label class="txt"><input type="checkbox" data-k="{key}" data-w="text"'
        f'{" checked" if text else ""}> text</label></div>' for key, col, name, n, text in rows)
    shown_note = (f'<details class="menu"><summary>show: <span id="menusum"></span></summary><div class="menubody">'
                  f'{menu}<div class="row"><button id="all">all</button> <button id="none">none</button> '
                  f'<span style="color:#666">the selected alert\'s text always shows</span></div></div></details>')
    when_note = ("" if not any("_d" in a for a in mine) else
                 ' · alerts at: <select id="when"><option value="alert">alert time</option><option value="first">'
                 'first detection in the trace</option></select>')
    data = json.dumps({"t2": t2, "l2": layout2d, "presets": [r for _, r in presets], "alerts": alerts_js,
                       "groups": groups, "menu": [r[0] for r in rows], **view["data"]})
    links = " ".join(f'<a href="{u}" target="_blank">{k}</a>' for k, u in rec.get("links", {}).items())
    credits = " · ".join(x for x in (view.get("credit"), DATA_CREDIT.get(source, f"Flight data: {source}"),
                                     OTHER_CREDITS) if x)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{ident} replay</title>{favicon()}
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="{PLOTLY}"></script>{view["head"]}
<style>html,body{{overflow-x:hidden}} body{{font-family:sans-serif;margin:8px;background:#fff}} a{{margin-right:1em}}
h1{{font-size:18px;margin:4px 0}} .sub{{font-size:13px;color:#444;margin-bottom:6px}}
#d3{{position:relative;background:linear-gradient(#b9d3ea,#eef4fa 60%)}} .legend{{font-size:12px;margin:4px 0}} .legend span{{margin-right:14px}}
.credits{{font-size:11px;color:#666;margin:6px 0}} .credits a{{margin-right:0}}
.menu{{display:inline-block;position:relative;margin-left:8px}} .menu summary{{cursor:pointer;border:1px solid #bbb;
border-radius:4px;padding:1px 8px;background:#f6f6f6}} .menubody{{position:absolute;z-index:20;background:#fff;
border:1px solid #bbb;box-shadow:0 2px 8px rgba(0,0,0,.2);padding:6px 10px;white-space:nowrap;font-size:13px}}
.menu .row{{display:flex;justify-content:space-between;gap:18px;padding:2px 0;font-weight:600}}
.menu .txt{{font-weight:normal;font-size:11px;color:#444}}
body.embed{{margin:0}} body.embed>:not(#d3):not(.credits):not(.maplegend):not(script){{display:none}}
body.embed{{display:flex;flex-direction:column;height:100vh}} body.embed .credits,body.embed .maplegend{{margin:2px 6px}}
body.embed #d3{{flex:1;height:auto!important;min-height:0}}
body.embed #helpbtn,body.embed #viewlink{{display:none}}</style>
</head><body>
<div class="legend">times: <select id="zone"><option value="Asia/Jerusalem">Israel</option>
<option value="UTC">UTC</option><option value="local">this computer's time zone</option></select>
· units: m, km, km/h {shown_note}{when_note}</div>
{view["html"]}
<div class="legend">chart below: {chart_buttons} · drag across it to zoom in, double-click to zoom out</div>
<div id="d2"></div>
<p>{links}</p>
<div class="credits">{credits}</div>
<script>
const D = {data};
// link options (see the map's help): embed=1 shows the map alone (with the credits) for an iframe
const Q = new URLSearchParams(location.search);
if (Q.get("embed") === "1") {{
  document.body.classList.add("embed");
  const full = new URL(location.href); full.searchParams.delete("embed");
  document.querySelector(".credits").insertAdjacentHTML("afterbegin",
    `<a href="${{full.href}}" target="_blank" style="margin-right:1em">open the full page ↗</a>`);
}}
// times are stored as ⟦format:epoch⟧ and shown in the zone picked above
let ZONE = ["UTC", "local"].includes(Q.get("zone")) ? Q.get("zone") : "Asia/Jerusalem";
document.getElementById("zone").value = ZONE;
const zoneId = () => ZONE === "local" ? Intl.DateTimeFormat().resolvedOptions().timeZone : ZONE;
function parts(t) {{
  const o = {{timeZone: zoneId(), hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
             hour: "2-digit", minute: "2-digit", second: "2-digit"}};
  return Object.fromEntries(new Intl.DateTimeFormat("en-GB", o).formatToParts(new Date(t * 1000))
                            .map(p => [p.type, p.value]));
}}
function zoneName(t) {{
  if (zoneId() === "UTC") return "UTC";
  const off = -new Date(new Date(t * 1000).toLocaleString("en-US", {{timeZone: "UTC"}})).getTime()
              + new Date(new Date(t * 1000).toLocaleString("en-US", {{timeZone: zoneId()}})).getTime();
  const h = Math.round(off / 36e5);
  if (zoneId() === "Asia/Jerusalem") return h === 3 ? "IDT" : "IST";
  return "UTC" + (h >= 0 ? "+" : "") + h;
}}
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function fmt(t, f) {{
  if (f === "z") return zoneName(t);
  const p = parts(t), hm = p.hour + ":" + p.minute;
  return f === "hm" ? hm : f === "dhm" ? `${{p.day}} ${{MON[+p.month - 1]}} ${{p.year}} ${{hm}}` : hm + ":" + p.second;
}}
const tz = (s) => typeof s === "string" ? s.replace(/⟦([a-z]+):([0-9.]+)⟧/g, (_, f, t) => fmt(+t, f)) : s;
function wall(t) {{  // epoch -> "YYYY-MM-DD HH:MM:SS.mmm" in the zone: plotly's date axis shows it as is
  const p = parts(t), ms = String(Math.round((t % 1) * 1000)).padStart(3, "0");
  return `${{p.year}}-${{p.month}}-${{p.day}} ${{p.hour}}:${{p.minute}}:${{p.second}}.${{ms}}`;
}}
function chart() {{
  const data = D.t2.map(tr => ({{...tr, x: tr.x.map(wall), text: tr.text && tr.text.map(tz)}}));
  const l = JSON.parse(JSON.stringify(D.l2));
  l.shapes.forEach(s => {{ s.x0 = wall(s.x0); s.x1 = wall(s.x1); }});
  l.shapes = l.shapes.filter(s => !hidden(s.name));
  l.annotations = l.annotations.filter(a => !hidden(a.name) && !(a.name && !TEXT[a.name]));
  l.annotations.forEach(a => {{ a.x = wall(a.x); a.text = tz(a.text); }});
  for (const a of visible()) l.shapes.push({{type: "line", x0: wall(a.t), x1: wall(a.t), yref: "paper", y0: 0, y1: 1,
    line: {{color: G[a.g].color, dash: "dot", width: a.sel ? 2 : 1}}}});
  const lab = labelled();  // vertical labels of alerts close in time: side by side
  lab.forEach((a, i) => l.annotations.push({{x: wall(a.t), y: 1, yref: "paper", text: tz(a.label),
    xshift: 14 * lab.slice(0, i).filter(b => a.t - b.t <= 180).length, showarrow: false, textangle: -90,
    xanchor: "right", yanchor: "top", font: {{color: G[a.g].color}}, bgcolor: "rgba(255,255,255,0.8)"}}));
  Plotly.react("d2", data, l, {{responsive: true}});
}}
// the "show" menu: each alert kind, steep changes (_steep) and data glitches (_glitch): marker, text
const G = Object.fromEntries(D.groups.map(g => [g.key, g]));
const SHOW = {{}}, TEXT = {{}};
document.querySelectorAll(".menu input").forEach(cb => (cb.dataset.w === "show" ? SHOW : TEXT)[cb.dataset.k] = cb.checked);
const visible = () => D.alerts.filter(a => SHOW[a.kind]);
const labelled = () => D.alerts.filter(a => SHOW[a.kind] && (TEXT[a.kind] || a.sel));
const hidden = (name) => name && name[0] === "_" && !SHOW[name];  // a steep / glitch item switched off
function stacked() {{  // each label by its own marker; raised (a.k steps) above earlier ones at about the
  const lab = labelled();  // same point (within 1.5 km and 300 m), e.g. VERTICAL_RATE and LOST_CONTACT
  lab.forEach((a, i) => a.k = lab.slice(0, i).filter(b => Math.hypot(a.x - b.x, a.y - b.y) <= 1.5
                                                      && Math.abs(a.z - b.z) <= 300).length);
  return lab;
}}
function menuSum() {{
  const on = D.menu.filter(k => SHOW[k]).length;
  document.getElementById("menusum").textContent = on === D.menu.length ? "everything" : `${{on}} of ${{D.menu.length}}`;
}}
document.querySelectorAll(".menu input").forEach(cb => cb.onchange = () => {{
  (cb.dataset.w === "show" ? SHOW : TEXT)[cb.dataset.k] = cb.checked;
  menuSum(); retime();
}});
const setAll = (on) => {{
  document.querySelectorAll('.menu input[data-w="show"]').forEach(cb => {{ cb.checked = on; SHOW[cb.dataset.k] = on; }});
  menuSum(); retime();
}};
document.getElementById("all").onclick = () => setAll(true);
document.getElementById("none").onclick = () => setAll(false);
menuSum();
// alerts at the time they were raised, or where the trace first passed the same check (a.d): how
// late the polled data let the monitor see it
D.alerts.forEach(a => a.at = {{t: a.t, x: a.x, y: a.y, z: a.z, lon: a.lon, lat: a.lat, label: a.label}});
const when = document.getElementById("when");
if (when) when.onchange = () => {{
  D.alerts.forEach(a => Object.assign(a, when.value === "first" && a.d ? a.d : a.at));
  retime();
}};
function retime() {{
  document.querySelectorAll(".tz").forEach(e => e.innerHTML = tz(e.dataset.raw));
  chart();
  if (typeof retime3d === "function") retime3d();
}}
document.querySelectorAll("button.cz").forEach(b => b.onclick = () => {{
  const r = D.presets[+b.dataset.i];
  Plotly.relayout("d2", r ? {{"xaxis.range": r.map(wall)}} : {{"xaxis.autorange": true}});
}});
{view["js"]}
document.getElementById("zone").onchange = (e) => {{ ZONE = e.target.value; retime(); }};
retime();
// hovering (or clicking) the time chart puts a black marker on that position in 3D
let shown = -1;
function show(ev) {{
  const p = ev.points.find(p => p.curveNumber === 0) || ev.points[0];
  const i = p.pointIndex;
  if (i === shown || !D.pos[i]) return;
  shown = i;
  moveCursor(D.pos[i]);
}}
const d2 = document.getElementById("d2");
d2.on("plotly_hover", show);
d2.on("plotly_click", show);
</script></body></html>
"""


def plotly_view(air, hops, mine, rec, signs, xr, yr, ztop, airport, title, sub, tip, km, focus,
                sources=False, glitches=(), places=None) -> dict:
    """Rotatable 3D with plotly: km east / north of the airport, altitude in m."""
    def line(kind):
        x, y, z, txt = [], [], [], []
        for i, k, info in hops:
            if k == kind:
                p, q = air[i - 1], air[i]
                x += [p["x"], q["x"], None]
                y += [p["y"], q["y"], None]
                z += [p["alt"] * FT, q["alt"] * FT, None]
                txt += [info, info, None]
        return x, y, z, txt
    # the track, broken at gaps and jumps (drawn separately)
    tx, ty, tz, tc = [], [], [], []
    t0 = air[0]["t"]
    for i, q in enumerate(air):
        if i and hops[i - 1][1] not in ("track", "steep", "glitch"):  # (those drawn over it, switchable)
            tx.append(None), ty.append(None), tz.append(None), tc.append(q["t"] - t0)
        tx.append(q["x"]), ty.append(q["y"]), tz.append(q["alt"] * FT), tc.append(q["t"] - t0)
    traces = [{"type": "scatter3d", "mode": "lines", "name": "track (colour = time)", "x": tx, "y": ty, "z": tz,
               "line": {"width": 5, "color": tc, "colorscale": "Viridis"}, "hoverinfo": "skip",
               **({"visible": "legendonly"} if sources else {})}]  # (--sources: click the legend to show)
    if sources:
        covered = mlat_covered(air)
        for src, name, colour, _ in SOURCES:
            for dotted in (False, True):  # GPS where MLAT covers the same moment: dotted
                x, y, z = [], [], []
                for p, q in source_lines(air)[src]:
                    if (src == "adsb" and covered(p, q)) != dotted:
                        continue
                    x += [p["x"], q["x"], None]
                    y += [p["y"], q["y"], None]
                    z += [p["alt"] * FT, q["alt"] * FT, None]
                if x:
                    traces.append({"type": "scatter3d", "mode": "lines", "x": x, "y": y, "z": z,
                                   "name": name + (" while MLAT is available" if dotted else ""),
                                   "line": {"width": 4, "color": colour, **({"dash": "dot"} if dotted else {})},
                                   "hoverinfo": "skip"})
    gx, gy, gz, gt = line("gap")
    if gx:
        traces.append({"type": "scatter3d", "mode": "lines", "name": f"no data > {GAP} s ({len(gx) // 3})",
                       "x": gx, "y": gy, "z": gz, "hovertext": gt, "hoverinfo": "text",
                       "line": {"width": 3, "color": "#888", "dash": "dash"}})
    for src, colour in (("adsb", "#1f77b4"), ("mlat", "#2ca02c")):
        sel = [p for p in air if p["src"] == src]
        other = src == "adsb" and [p for p in air if p["src"] not in ("adsb", "mlat")]
        sel += other or []
        if sel:
            traces.append({"type": "scatter3d", "mode": "markers", "name": f"{src} positions" +
                           (" (+other)" if other else ""), "x": [p["x"] for p in sel],
                           "y": [p["y"] for p in sel], "z": [p["alt"] * FT for p in sel],
                           "marker": {"size": 2, "color": colour}, "text": [tip(p) for p in sel],
                           "hovertemplate": "%{text}<extra></extra>"})
    # shadow on the ground, Israel's outline and airports for orientation
    traces.append({"type": "scatter3d", "mode": "lines", "name": "ground track", "x": [p["x"] for p in air],
                   "y": [p["y"] for p in air], "z": [0] * len(air),
                   "line": {"width": 2, "color": "rgba(128,128,128,0.5)"}, "hoverinfo": "skip"})
    ring = [km(la, lo) for la, lo in fw.ISRAEL + fw.ISRAEL[:1]]
    if any(xr[0] <= x <= xr[1] and yr[0] <= y <= yr[1] for x, y in ring):  # not for incidents elsewhere
        ring = [(min(max(x, xr[0]), xr[1]), min(max(y, yr[0]), yr[1])) for x, y in ring]
        traces.append({"type": "scatter3d", "mode": "lines", "name": "Israel (rough)",
                       "x": [p[0] for p in ring], "y": [p[1] for p in ring], "z": [0] * len(ring),
                       "line": {"width": 2, "color": "#888", "dash": "dot"}, "hoverinfo": "skip"})  # (green: MLAT)
    apts = [(k, km(v[0], v[1])) for k, v in (places or fw.REGION_AIRPORTS).items()]
    apts = [(k, (x, y)) for k, (x, y) in apts if xr[0] <= x <= xr[1] and yr[0] <= y <= yr[1]]
    if apts:
        traces.append({"type": "scatter3d", "mode": "markers+text", "name": "airports",
                       "x": [p[0] for _, p in apts], "y": [p[1] for _, p in apts], "z": [0] * len(apts),
                       "text": [k for k, _ in apts], "textposition": "top center",
                       "marker": {"size": 3, "color": "#555", "symbol": "square"}, "hoverinfo": "text"})
    gx, gy, gz, gt = line("glitch")
    if gx:
        colour = "rgb(%d,%d,%d)" % tuple(GLITCH_RGB)
        traces.append({"type": "scatter3d", "mode": "lines", "name": f"altitude data glitch ({len(glitches)})",
                       "x": gx, "y": gy, "z": gz, "hovertext": gt, "hoverinfo": "text", "meta": "_glitch",
                       "line": {"width": 5, "color": colour}})
        traces.append({"type": "scatter3d", "mode": "text", "showlegend": False, "meta": "_glitch:text",
                       "x": [air[c["start"]]["x"] for c in glitches], "y": [air[c["start"]]["y"] for c in glitches],
                       "z": [air[c["start"]]["alt"] * FT for c in glitches], "text": [c["label"] for c in glitches],
                       "textposition": "bottom center", "hovertext": [c["text"] for c in glitches],
                       "hoverinfo": "text", "textfont": {"color": colour, "size": 10}})
    vx, vy, vz, vt = line("steep")
    if vx:
        traces.append({"type": "scatter3d", "mode": "lines", "name": f"steep altitude change ({len(signs)})",
                       "meta": "_steep", "x": vx, "y": vy, "z": vz, "hovertext": vt, "hoverinfo": "text",
                       "line": {"width": 7, "color": "rgb(155,48,217)"}})
        traces.append({"type": "scatter3d", "mode": "text", "showlegend": False, "meta": "_steep:text",
                       "x": [air[c["start"]]["x"] for c in signs], "y": [air[c["start"]]["y"] for c in signs],
                       "z": [air[c["start"]]["alt"] * FT for c in signs], "text": [c["label"] for c in signs],
                       "textposition": "bottom center", "hovertext": [c["text"] for c in signs], "hoverinfo": "text",
                       "textfont": {"color": "rgb(155,48,217)", "size": [13 if c["first"] else 10 for c in signs]}})
    jx, jy, jz, jt = line("jump")
    if jx:
        traces.append({"type": "scatter3d", "mode": "lines", "name": f"position jumps ({len(jx) // 3})",
                       "x": jx, "y": jy, "z": jz, "hovertext": jt, "hoverinfo": "text",
                       "line": {"width": 6, "color": "#ff9900"},
                       **({"visible": "legendonly"} if sources else {})})  # (mostly GPS <-> MLAT hops)
    layout = {"title": {"text": f"{title}<br><sub>{sub}</sub>"}, "height": 720, "uirevision": "keep",
              "margin": {"l": 0, "r": 0, "t": 70, "b": 0}, "legend": {"x": 0, "y": 1},
              "scene": {"xaxis": {"title": f"km east of {airport}", "range": xr},
                        "yaxis": {"title": f"km north of {airport}", "range": yr},
                        "zaxis": {"title": "altitude (m)", "range": [0, ztop]},
                        "aspectmode": "manual", "aspectratio": {"x": 2, "y": 2, "z": 0.7}}}
    helptext = ("Left-drag: rotate · right-drag: move · scroll: zoom · hover a point for details; "
                "hover the chart below to find that moment above")
    html = (f'<div class="legend"><button id="zoomin">zoom to alerts</button> <button id="whole">whole flight'
            f'</button> · {helptext}</div><div id="d3"></div>')
    js = r"""
// alerts by group: a drop line to the ground, a marker, the label (stacked where several meet)
function alertTraces() {
  const out = [], dz = D.l3.scene.zaxis.range[1] * 0.045;
  for (const g of D.groups) {
    const list = visible().filter(a => a.g === g.key);
    if (!list.length) continue;
    out.push({type: "scatter3d", mode: "lines", showlegend: false, hoverinfo: "skip",
      x: list.flatMap(a => [a.x, a.x, null]), y: list.flatMap(a => [a.y, a.y, null]),
      z: list.flatMap(a => [0, a.z, null]), line: {width: 2, color: g.color, dash: "dot"}});
    out.push({type: "scatter3d", mode: "markers", showlegend: false, x: list.map(a => a.x),
      y: list.map(a => a.y), z: list.map(a => a.z), hovertext: list.map(a => tz(a.tip)), hoverinfo: "text",
      marker: {size: list.map(a => a.sel ? 9 : 6), color: g.color, symbol: g.symbol, line: {color: g.color}}});
  }
  const lab = stacked();
  if (lab.length) {
    out.push({type: "scatter3d", mode: "text", showlegend: false, x: lab.map(a => a.x), y: lab.map(a => a.y),
      z: lab.map(a => a.z + a.k * dz), text: lab.map(a => tz(a.label)), textposition: "top center",
      textfont: {color: lab.map(a => G[a.g].color), size: lab.map(a => a.sel ? 13 : 11)},
      hovertext: lab.map(a => tz(a.tip)), hoverinfo: "text"});
    const up = lab.filter(a => a.k);  // thin lines from a raised label down to its marker
    if (up.length) out.push({type: "scatter3d", mode: "lines", showlegend: false, hoverinfo: "skip",
      x: up.flatMap(a => [a.x, a.x, null]), y: up.flatMap(a => [a.y, a.y, null]),
      z: up.flatMap(a => [a.z, a.z + a.k * dz, null]), line: {width: 1, color: "rgba(0,0,0,0.35)"}});
  }
  return out;
}
let cursorAt = [null, null, null], cursorIdx = 0;
function draw3d() {
  const m = (a) => Array.isArray(a) ? a.map(tz) : a;
  const off = (meta) => meta && (meta.endsWith(":text") ? hidden(meta.split(":")[0]) || !TEXT[meta.split(":")[0]]
                                                       : hidden(meta));  // switched off in the menu
  const traces = D.t3.filter(tr => !off(tr.meta))
    .map(tr => ({...tr, name: tz(tr.name), text: m(tr.text), hovertext: m(tr.hovertext)}))
    .concat(alertTraces());
  cursorIdx = traces.length;  // the black "you are here" marker, moved from the time chart
  traces.push({type: "scatter3d", mode: "markers", name: "cursor", showlegend: false, hoverinfo: "skip",
    x: [cursorAt[0]], y: [cursorAt[1]], z: [cursorAt[2]], marker: {size: 6, color: "black"}});
  Plotly.react("d3", traces, {...D.l3, title: {text: tz(D.l3.title.text)}}, {responsive: true});
}
function moveCursor(p) {
  cursorAt = p;
  Plotly.restyle("d3", {x: [[p[0]]], y: [[p[1]]], z: [[p[2]]]}, [cursorIdx]);
}
const retime3d = draw3d;
""" + (
          'const box = (r) => ({"scene.xaxis.range": r[0], "scene.yaxis.range": r[1]});\n'
          'document.getElementById("zoomin").onclick = () => Plotly.relayout("d3", box(D.focus));\n'
          'document.getElementById("whole").onclick = () => Plotly.relayout("d3", box(D.whole));\n')
    return {"head": "", "html": html,
            "data": {"t3": traces, "l3": layout, "focus": focus, "whole": [xr, yr],
                     "pos": [[p["x"], p["y"], p["alt"] * FT] for p in air]},
            "js": js}


def deck_view(air, hops, mine, rec, signs, tip, tiles=TILES, focus=None, sources=False, glitches=(),
              places=None, base=0) -> dict:
    """The same over a street map (Esri World Street Map tiles by default) with deck.gl: drag to pan,
    right-drag / Ctrl+drag to tilt and rotate. Altitude exaggerated (adjustable)."""
    t0, t1 = air[0]["t"], air[-1]["t"]
    ft = 0.3048
    segs = []
    for i, kind, info in hops:
        p, q = air[i - 1], air[i]
        colour = viridis((q["t"] - t0) / max(1, t1 - t0)) if kind == "track" else \
            [255, 153, 0] if kind == "jump" else PURPLE if kind == "steep" else \
            GLITCH_RGB if kind == "glitch" else [110, 110, 110]
        a, b = [p["lon"], p["lat"], p["alt"] * ft], [q["lon"], q["lat"], q["alt"] * ft]
        if kind != "gap":  # c0: the track's colour, when the menu hides a steep change / glitch
            segs.append({"a": a, "b": b, "c": colour, "k": kind, "tip": info,
                         "c0": viridis((q["t"] - t0) / max(1, t1 - t0))})
            continue
        n = 30  # no data: dashed (deck.gl lines have no dash style, so draw every other piece)
        for k in range(0, n, 2):
            f0, f1 = k / n, (k + 1) / n
            segs.append({"a": [x + (y - x) * f0 for x, y in zip(a, b)], "b": [x + (y - x) * f1 for x, y in zip(a, b)],
                         "c": colour, "k": kind, "tip": info})
    if sources:  # each position source as its own line instead of the time-coloured track; no jump
        segs = [x for x in segs if x["k"] not in ("track", "jump")]  # lines (mostly GPS <-> MLAT hops)
        covered = mlat_covered(air)
        for src, _, _, rgb in SOURCES:
            for p, q in source_lines(air)[src]:
                a, b = [p["lon"], p["lat"], p["alt"] * ft], [q["lon"], q["lat"], q["alt"] * ft]
                if not (src == "adsb" and covered(p, q)):
                    segs.append({"a": a, "b": b, "c": rgb, "k": "track", "tip": src.upper()})
                    continue
                # GPS while MLAT is available: dotted (every other ~2 km piece)
                n = max(2, 2 * round(fw.haversine_nm(p["lat"], p["lon"], q["lat"], q["lon"]) * NM / 4))
                segs += [{"a": [u + (v - u) * k / n for u, v in zip(a, b)],
                          "b": [u + (v - u) * (k + 1) / n for u, v in zip(a, b)],
                          "c": rgb, "k": "track", "tip": "ADSB (MLAT available)"} for k in range(0, n, 2)]
    data = {"segs": segs,
            "pts": [{"p": [p["lon"], p["lat"], p["alt"] * ft], "tip": tip(p),
                     "c": [44, 160, 44] if p["src"] == "mlat" else [31, 119, 180]} for p in air],
            "airports": [{"p": [v[1], v[0], 0], "label": k} for k, v in (places or fw.REGION_AIRPORTS).items()],
            "signs": [{"p": [air[c["start"]]["lon"], air[c["start"]]["lat"], air[c["start"]]["alt"] * ft],
                       "label": c["label"], "tip": c["text"], "first": c["first"], "glitch": c["glitch"]}
                      for c in list(signs) + list(glitches)],
            "bounds": [[min(p["lon"] for p in air + [{"lon": a["lon"]} for a in mine]),
                        min(p["lat"] for p in air + [{"lat": a["lat"]} for a in mine])],
                       [max(p["lon"] for p in air + [{"lon": a["lon"]} for a in mine]),
                        max(p["lat"] for p in air + [{"lat": a["lat"]} for a in mine])]],
            "focus": focus,
            # the camera aims at this height (m), not the ground: zooming in on a track drawn high
            # above the map otherwise flies underneath it
            "focusAlt": sorted([a["_alt"] for a in mine] + [air[c["start"]]["alt"] for c in signs if c["first"]])[
                len(mine) // 2] * ft,
            "tiles": tiles, "terrain": TERRAIN, "base": round(base * FT),
            "pos": [[p["lon"], p["lat"], p["alt"] * ft] for p in air]}
    ident = rec.get("flight") or rec.get("callsign") or rec["hex"].upper()
    credit = (TILES_CREDIT if tiles == TILES else f"map tiles: {tiles.split('/')[2]}") + " · " + TERRAIN_CREDIT
    jump_legend = "" if sources else '<span style="color:#ff9900">━ position jumps</span>'
    track_legend = ("".join(f'<span style="color:{c}">━ {n}</span>' for _, n, c, _ in SOURCES)
                    + f'<span style="color:{SOURCES[0][2]}">┅ ADS-B while MLAT is available</span>' if sources
                    else '<span style="color:#3b528b">━ track (colour = time)</span>')
    first = next((c for c in signs if c["first"]), None)
    first_btn = (f' <button id="firstsign" class="tz" data-raw="{first["label"]}">{first["label"]}</button>'
                 if first else "")
    head = html.escape(f"{ident} {rec.get('route_text') or 'route unknown'}", quote=True)
    sub = html.escape(f"{rec['kind']} - {metric(rec['message'])} ({len(air)} positions, "
                      f"{T(air[0]['t'], 'dhm')}–{T(air[-1]['t'], 'hm')} {T(air[-1]['t'], 'z')})", quote=True)
    page = f"""<h1 class="tz" data-raw="{head}"></h1>
<div class="sub tz" data-raw="{sub}"></div>
<div class="legend maplegend">{track_legend}
{jump_legend}
<span style="color:#888">╌ no data &gt; {GAP} s</span>
· altitude ×<input id="zx" type="range" min="1" max="20" value="5" style="vertical-align:middle;width:110px"><b
id="zxv">5</b> <button id="terrainbtn" title="the ground's heights (loads more slowly)">show terrain</button>
<button id="zoomin">zoom to alerts</button> <button id="top">top view</button>
<button id="reset">whole flight</button>{first_btn}
<button id="helpbtn">? how to move</button> <button id="viewlink" title="copy a link that opens the map at this
view (for posts and iframes)">link to this view</button></div>
<div id="help" style="display:none;font-size:13px;background:#fffbe6;border:1px solid #e0c060;padding:8px 12px;
margin:4px 0;max-width:720px"><b>Moving around the map</b><br>
• <b>Drag</b> with the left mouse button: move the map.<br>
• <b>Scroll</b>: zoom in and out toward the <b>middle</b> of the view, at the height of the alerts
(the track is drawn high above the map). Drag what you want to see to the middle first.<br>
• <b>Ctrl + drag</b> (or Shift + drag, or drag with the <b>right</b> button): turn the view.
Drag <b>sideways</b> to rotate around, <b>up and down</b> to tilt between looking straight down and
looking from the side.<br>
• Keyboard: arrows move, + / − zoom, Shift + arrows rotate and tilt.<br>
• <b>zoom to alerts</b> flies to where the alerts are; <b>first sign</b> (when there is one) to the earliest
steep altitude change; <b>top view</b> looks straight down (like a normal map); <b>whole flight</b> goes back
to the start.<br>

• Hover a point or line for details; hover the chart below the map to put a black dot on that moment.<br>
• <b>times</b> (top): Israel time, UTC or your computer's time zone.<br>
• The altitude slider stretches heights so climbs and dives are visible (×1 is true scale).<br>
• <b>show terrain</b> draws the ground's hills and valleys (slower to load). Without it the map is flat, at the
height of the airfield nearest the alerts.<br>
• <b>link to this view</b> copies a link that opens the map as it is now. Link options:
<code>view=lat,lon,zoom,pitch,bearing,height</code> (height in m, where the camera aims),
<code>x=</code> altitude ×, <code>terrain=1</code>, <code>go=alerts|first|top</code>, <code>zone=UTC|local</code>,
<code>embed=1</code> (map only, for an iframe).</div>
<div id="d3" style="height:640px"></div>"""
    js = r"""
let Z = Math.min(20, Math.max(1, +Q.get("x") || 5)), cursor = null;
// terrain off at first (the flat map loads much faster); the button or terrain=1 turns it on
let TERRAIN = Q.get("terrain") === "1";
const tb = document.getElementById("terrainbtn");
tb.textContent = TERRAIN ? "flat map" : "show terrain";
document.getElementById("zx").value = Z; document.getElementById("zxv").textContent = Z;
// heights are exaggerated (x Z) above the airfield (D.base, m), not above sea level: x 6 would lift Addis
// Ababa's ground (2,326 m) to 14 km, where deck.gl, picking tiles as if the ground were at 0, left out the near ones
const H = (h) => D.base + (h - D.base) * Z;
const P = (p) => [p[0], p[1], H(p[2])];
const A = (a) => [a.lon, a.lat, a.z];
const GLITCH = """ + json.dumps(GLITCH_RGB) + r""";
// terrain heights x Z, as the track; the sea floor raised to sea level (terrarium: m = R*256 + G + B/256 - 32768)
const baseFetch = deck.Layer.defaultProps.fetch.value || deck.Layer.defaultProps.fetch;
async function seaLevel(url, ctx) {
  if (ctx.propName !== "elevationData") return baseFetch(url, ctx);
  const r = await fetch(url, {signal: ctx.signal});
  const img = await createImageBitmap(await r.blob(), {colorSpaceConversion: "none", premultiplyAlpha: "none"});
  const c = new OffscreenCanvas(img.width, img.height), g = c.getContext("2d", {willReadFrequently: true});
  g.drawImage(img, 0, 0);
  const d = g.getImageData(0, 0, c.width, c.height), a = d.data;
  let sea = false;
  for (let i = 0; i < a.length; i += 4) if (a[i] < 128) { a[i] = 128; a[i + 1] = 0; a[i + 2] = 0; sea = true; }
  if (!sea) return baseFetch(url, ctx);  // land only: the original (from the browser's cache), no re-encoding
  g.putImageData(d, 0, 0);
  const u = URL.createObjectURL(await c.convertToBlob({type: "image/png"}));
  try { return await baseFetch(u, ctx); } finally { URL.revokeObjectURL(u); }
}
// with terrain: on it; flat: the map, shadow and airports at the airfield's height (D.base), which the
// exaggeration leaves unchanged, so the track keeps its height above that ground
const onGround = () => TERRAIN ? {extensions: [new deck._TerrainExtension()]} : {};
const g0 = (p) => [p[0], p[1], TERRAIN ? 0 : D.base];
const off = (d) => (d.k === "steep" || d.k === "glitch") && hidden("_" + d.k);  // switched off in the menu
// a map tile flat at height z, darkened (as the terrain's shading) so the yellow track stands out
function flat(props, z, more) {
  const [[w, s], [e, n]] = props.tile.boundingBox;
  return new deck.BitmapLayer(props, {data: null, image: props.data, tintColor: [185, 185, 185],
                                      bounds: [[w, s, z], [w, n, z], [e, n, z], [e, s, z]], ...more});
}
function layers() {
  return [
    // deck.gl leaves out the tiles nearest the camera in a steep view aimed high above the ground (white
    // patches at the bottom): coarse tiles behind everything (drawn first, not in the depth buffer) fill them
    new deck.TileLayer({id: "under", data: D.tiles, minZoom: 0, maxZoom: 19, tileSize: 256, zoomOffset: -3,
      zRange: [D.base, D.base], renderSubLayers: props => flat(props, D.base,
        {parameters: {depthWriteEnabled: false, depthCompare: "always"}})}),
    // the ground with its heights (x Z, as the track): see TERRAIN
    TERRAIN ? new deck.TerrainLayer({id: "terrain" + Z, elevationData: D.terrain, texture: D.tiles, minZoom: 0, maxZoom: 15,
      // a quarter of the tiles of zoomOffset 1 (each is two downloads and a mesh); a coarser mesh (m x Z)
      tileSize: 256, zoomOffset: 0, operation: "terrain+draw", fetch: seaLevel, meshMaxError: 5 * Z,
      loaders: [loaders.TerrainLoader], loadOptions: {worker: false},
      elevationDecoder: {rScaler: 256 * Z, gScaler: Z, bScaler: Z / 256, offset: -32768 * Z + D.base * (1 - Z)}})
    // flat; zoomOffset: the camera aims above the ground (aim()), which would otherwise pick blurry tiles
    : new deck.TileLayer({id: "tiles", data: D.tiles, minZoom: 0, maxZoom: 19, tileSize: 256, zoomOffset: 1,
      zRange: [D.base, D.base], renderSubLayers: props => flat(props, D.base)}),
    new deck.LineLayer({id: "shadow" + TERRAIN, data: D.segs, getSourcePosition: d => g0(d.a),
      getTargetPosition: d => g0(d.b), getColor: [100, 100, 100, 90], getWidth: 1,
      ...onGround(), terrainDrawMode: "drape"}),
    new deck.LineLayer({id: "drops" + TERRAIN, data: visible(), getSourcePosition: d => g0([d.lon, d.lat]),
      getTargetPosition: d => P(A(d)), getColor: d => [...G[d.g].rgb, 160], getWidth: 1,
      updateTriggers: {getTargetPosition: Z}}),
    new deck.LineLayer({id: "track", data: D.segs, pickable: true, getSourcePosition: d => P(d.a),
      getTargetPosition: d => P(d.b), getColor: d => off(d) ? d.c0 : d.c,
      getWidth: d => off(d) ? 2 : d.k === "jump" ? 3 : d.k === "steep" || d.k === "glitch" ? 4 : d.k === "gap" ? 1.5 : 2,
      updateTriggers: {getSourcePosition: Z, getTargetPosition: Z, getColor: Math.random(), getWidth: Math.random()}}),
    new deck.ScatterplotLayer({id: "pts", data: D.pts, pickable: true, getPosition: d => P(d.p),
      getFillColor: d => d.c, radiusUnits: "pixels", getRadius: 1.5, updateTriggers: {getPosition: Z}}),
    new deck.ScatterplotLayer({id: "airports" + TERRAIN, data: D.airports, getPosition: d => g0(d.p),
      getFillColor: [80, 80, 80], radiusUnits: "pixels", getRadius: 3, ...onGround(), terrainDrawMode: "offset"}),
    // alert markers: their group's shape as a character (deck.gl points are only circles)
    new deck.TextLayer({id: "alerts", data: visible(), pickable: true, getPosition: d => P(A(d)),
      getText: d => G[d.g].glyph, characterSet: D.groups.map(g => g.glyph).join(""), fontFamily: "sans-serif",
      getSize: d => d.sel ? 24 : 18, getColor: d => G[d.g].rgb, fontWeight: 700, fontSettings: {sdf: true},
      outlineWidth: 3, outlineColor: [255, 255, 255, 230], updateTriggers: {getPosition: Z}}),
    new deck.ScatterplotLayer({id: "cursor", data: cursor ? [cursor] : [], getPosition: d => P(d),
      getFillColor: [0, 0, 0], radiusUnits: "pixels", getRadius: 5, updateTriggers: {getPosition: Z}}),
  ];
}
const el = document.getElementById("d3");
const fit = new deck.WebMercatorViewport({width: el.clientWidth, height: el.clientHeight})
  .fitBounds(D.bounds, {padding: 60});
const aim = (alt) => [0, 0, H(alt === undefined ? D.focusAlt : alt)];  // camera target height
const start = {longitude: fit.longitude, latitude: fit.latitude, zoom: Math.min(fit.zoom, 10),
               pitch: 50, bearing: 0, maxPitch: 85, position: aim()};
// ?view=lat,lon,zoom,pitch,bearing[,height m]: open there (a post's or blog's chosen angle)
const V = (Q.get("view") || "").split(",").map(Number);
let aimAlt = V.length > 5 && isFinite(V[5]) ? V[5] : undefined;
const first = V.length >= 5 && V.slice(0, 5).every(isFinite) ? {...start, latitude: V[0], longitude: V[1], zoom: V[2],
  pitch: Math.min(85, V[3]), bearing: V[4], position: aim(aimAlt)} : start;
let view = first, cam = first;  // current view (the user's moves and the buttons'); the camera as shown
const map = new deck.DeckGL({container: "d3", initialViewState: first,
  // deck.gl would zoom toward the ground under the pointer, far from a track drawn high above it
  controller: {scrollZoom: false, doubleClickZoom: false}, layers: layers(),
  onViewStateChange: ({viewState, interactionState: i}) => {  // only the user's moves (a button's
    cam = viewState;
    if (i && (i.isDragging || i.isPanning || i.isRotating || i.isZooming)) view = viewState;  // own: go())
  },
  getTooltip: ({object}) => object && object.tip ? {html: tz(object.tip)} : null});
const go = (v, alt) => {
  aimAlt = alt;
  view = {...view, ...v, position: aim(alt)};
  map.setProps({initialViewState: {...view, transitionDuration: 700,
                                   transitionInterpolator: new deck.FlyToInterpolator(), _t: Date.now()}});
};
// the wheel zooms toward the middle of the view, at the height aimed at (where the alerts are)
el.addEventListener("wheel", (e) => {
  e.preventDefault();
  const step = -e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0025);
  view = {...view, zoom: Math.max(1, Math.min(16, view.zoom + step)), position: aim(aimAlt)};
  map.setProps({initialViewState: {...view, _t: Date.now()}});
}, {passive: false, capture: true});
document.getElementById("top").onclick = () => go({pitch: 0, bearing: 0}, aimAlt);
document.getElementById("reset").onclick = () => go(start);
const near = new deck.WebMercatorViewport({width: el.clientWidth, height: el.clientHeight})
  .fitBounds(D.focus, {padding: 120});
document.getElementById("zoomin").onclick = () =>
  go({longitude: near.longitude, latitude: near.latitude, zoom: Math.min(near.zoom, 9), pitch: 50, bearing: 0});
const fs = document.getElementById("firstsign"), sign = D.signs.find(s => s.first);
if (fs) fs.onclick = () => go({longitude: sign.p[0], latitude: sign.p[1], zoom: 12, pitch: 55, bearing: 0}, sign.p[2]);
({alerts: "zoomin", first: "firstsign", top: "top"})[Q.get("go")] &&
  setTimeout(() => (document.getElementById({alerts: "zoomin", first: "firstsign", top: "top"}[Q.get("go")]) || {}).onclick?.(), 300);
document.getElementById("viewlink").onclick = (e) => {
  const c = cam, alt = c.position ? D.base + (c.position[2] - D.base) / Z : 0;  // H()'s inverse
  const u = new URL(location.href);
  u.searchParams.set("view", [c.latitude.toFixed(4), c.longitude.toFixed(4), c.zoom.toFixed(2),
                              Math.round(c.pitch), Math.round(c.bearing), Math.round(alt)].join(","));
  u.searchParams.set("x", Z);
  TERRAIN ? u.searchParams.set("terrain", 1) : u.searchParams.delete("terrain");
  u.searchParams.delete("go");
  history.replaceState(null, "", u);
  const b = e.target, done = (t) => { b.textContent = t; setTimeout(() => b.textContent = "link to this view", 2000); };
  navigator.clipboard ? navigator.clipboard.writeText(u.href).then(() => done("copied"), () => done("in the address bar"))
                      : done("in the address bar");
};
document.getElementById("helpbtn").onclick = () => {
  const h = document.getElementById("help"); h.style.display = h.style.display === "none" ? "block" : "none";
};
document.getElementById("zx").oninput = (e) => {
  Z = +e.target.value; document.getElementById("zxv").textContent = Z;
  view = {...view, position: aim(aimAlt)};
  map.setProps({layers: layers(), initialViewState: {...view, _t: Date.now()}});
};
tb.onclick = () => {
  TERRAIN = !TERRAIN; tb.textContent = TERRAIN ? "flat map" : "show terrain";
  map.setProps({layers: layers()});
};
function moveCursor(p) { cursor = p; map.setProps({layers: layers()}); }
function retime3d() { labelsDirty = true; map.setProps({layers: layers()}); }
// alert labels: HTML over the map, laid out on screen after every frame - each to the right of its
// marker, moved up / down where it would cover another (markers of one place are pixels apart:
// 4XDAN's dives), with a thin line back to its marker
const over = document.createElement("div");
over.style.cssText = "position:absolute;inset:0;pointer-events:none;overflow:hidden;z-index:1";
document.getElementById("d3").appendChild(over);
// airport names ("SOF Sofia"): HTML too, one size at any tilt (deck.gl's text shrank with the distance); a name
// that would cover one placed before it (the flight's own airports come first) is left out
const aptBox = document.createElement("div");
aptBox.style.cssText = over.style.cssText;
document.getElementById("d3").insertBefore(aptBox, over);
const apts = D.airports.map(d => {
  const e = document.createElement("span");
  e.textContent = d.label;
  e.style.cssText = "position:absolute;white-space:nowrap;font:700 14px sans-serif;color:#222;" +
    "text-shadow:0 0 3px #fff,0 0 3px #fff,0 0 2px #fff";
  aptBox.appendChild(e);
  return {d, e, w: e.offsetWidth, h: e.offsetHeight};
});
function layoutAirports(vp) {
  const placed = [];
  for (const a of apts) {
    const p = vp.project(g0(a.d.p)), x = p[0] - a.w / 2, y = p[1] - a.h - 3;
    const ok = p[0] > 0 && p[0] < vp.width && p[1] > 0 && p[1] < vp.height &&
      !placed.some(b => x < b.x + b.w && b.x < x + a.w && y < b.y + b.h && b.y < y + a.h);
    a.e.style.display = ok ? "" : "none";
    if (!ok) continue;
    placed.push({x, y, w: a.w, h: a.h});
    a.e.style.transform = `translate(${x}px,${y}px)`;
  }
}
let labelsDirty = true, labs = [];
function layoutLabels() {
  const vp = map.getViewports && map.getViewports()[0];
  if (!vp) return;
  layoutAirports(vp);
  if (labelsDirty) {
    labelsDirty = false;
    over.innerHTML = '<svg style="position:absolute;inset:0;width:100%;height:100%"></svg>';
    // the alerts', then the steep changes' / glitches' (their labels too: same layout)
    const items = stacked().map(d => ({p: A(d), text: d.label, big: d.sel, color: G[d.g].color})).concat(
      D.signs.filter(d => { const k = d.glitch ? "_glitch" : "_steep"; return SHOW[k] && TEXT[k]; })
        .map(d => ({p: d.p, text: d.label, big: d.first, color: d.glitch ? `rgb(${GLITCH})` : "rgb(155,48,217)"})));
    labs = items.map(d => {
      const e = document.createElement("span");
      e.textContent = tz(d.text);
      e.style.cssText = `position:absolute;white-space:nowrap;font:600 ${d.big ? 15 : 12}px sans-serif;color:${d.color};` +
        "text-shadow:0 0 3px #fff,0 0 3px #fff,0 0 2px #fff";
      over.appendChild(e);
      return {d, e, w: e.offsetWidth, h: e.offsetHeight};
    });
  }
  const placed = [], lines = [];
  const at = labs.map(l => ({l, p: vp.project(P(l.d.p))})).sort((a, b) => a.p[1] - b.p[1]);
  for (const {l, p} of at) {
    const x = p[0] + 12;
    let y = p[1] - l.h / 2;
    for (let k = 1; k < 40 && placed.some(b => x < b.x + b.w && b.x < x + l.w && y < b.y + b.h && b.y < y + l.h); k++)
      y = p[1] - l.h / 2 + (k % 2 ? 1 : -1) * Math.ceil(k / 2) * (l.h + 1);
    placed.push({x, y, w: l.w, h: l.h});
    l.e.style.transform = `translate(${x}px,${y}px)`;
    if (Math.abs(y + l.h / 2 - p[1]) > 2)
      lines.push(`<line x1="${p[0]}" y1="${p[1]}" x2="${x}" y2="${y + l.h / 2}" stroke="${l.d.color}" stroke-opacity="0.6"/>`);
  }
  over.firstChild.innerHTML = lines.join("");
}
map.setProps({onAfterRender: layoutLabels});
"""
    return {"head": f'\n<script src="{DECK}"></script>\n<script src="{TERRAIN_LOADER}"></script>', "html": page, "data": data, "js": js, "credit": credit}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--alerts", default="alerts.jsonl")
    p.add_argument("--fixture", help="plot a replay fixture offline (tests/data/*.json.gz): its alerts are the "
                                     "monitor's on a replay now, its positions those of the fixture")
    p.add_argument("--pick", type=int, help="alert number in the list (1 = newest)")
    p.add_argument("--event", action="store_true", help="with --fixture: its first meta event, not an alert")
    p.add_argument("--filter", nargs="+", default=[], metavar="TEXT",
                   help="list only alerts whose row contains all these (any case): LY347, Zurich, RETURNED")
    p.add_argument("--minutes", type=float, help="plot this long before and after the alert, across "
                                                  "landings and gaps (default: the alert's flight leg)")
    p.add_argument("--map", action="store_true",
                   help="draw over a street map (deck.gl; tilt / rotate) instead of km axes")
    p.add_argument("--tiles", default=TILES, help="map tile URL template ({z}, {x}, {y}) for --map")
    p.add_argument("--sources", action="store_true",
                   help="draw the aircraft's GPS (ADS-B) positions and the ground receivers' MLAT "
                        "positions as two separate lines, to compare them (e.g. during GPS spoofing)")
    p.add_argument("--airport", default="TLV", help="km axes are measured from this airport")
    p.add_argument("--max-speed", type=float, default=1200,
                   help="kt; faster hops in the trace are drawn as jumps (as flight_watch.py --max-speed)")
    p.add_argument("--max-gap-speed", type=float, default=750,
                   help="kt; average speed over a gap / via one off-track position (as flight_watch.py)")
    p.add_argument("--out", default="tmp_plot.html")
    p.add_argument("--no-open", action="store_true", help="don't open the page in a browser")
    a = p.parse_args(argv)
    origin = None
    if a.fixture:
        fixture, alerts = replay_alerts(a.fixture)
        meta = fixture["meta"]
        alerts = [x for x in alerts if x["hex"] in fixture["aircraft"]]
        if not alerts and not meta.get("events"):
            sys.exit(f"no alerts on a replay of {a.fixture}")
        if not a.filter and a.pick is None:  # the incident aircraft's first alert (the list is newest first)
            a.pick = max((i for i, x in enumerate(alerts, 1) if x["hex"] in meta["focus"]), default=len(alerts))
        # incident events (meta "events": time, kind, message, lat, lon, alt) drawn as alerts; appended
        # after the pick, so they do not move the alert numbers
        alerts += [dict(e, hex=meta["focus"][0]) for e in meta.get("events", [])]
        if a.event:  # centred on the fixture's event, not on an alert
            a.pick = len(alerts) - len(meta.get("events", [])) + 1
        if "--airport" not in (argv if argv is not None else sys.argv[1:]):
            a.airport = meta["airport"]
        origin = tuple(meta["airport_coords"]) if "airport_coords" in meta else None
    else:
        alerts = load_alerts(a.alerts)
        if not alerts:
            sys.exit(f"no alerts in {a.alerts}")
    rec = choose(alerts, a.pick, a.filter)
    t = epoch(rec["time"])
    if a.fixture:
        pts, source = fixture_points(fixture, rec["hex"]), meta["source"]
    else:
        print(f"fetching trace of {rec['hex']} ...", file=sys.stderr)
        pts, source = trace_for(rec["hex"], t)
    if pts and pts[-1]["t"] < t:
        print(f"warning: the trace ends at {local(pts[-1]['t'])}, before the alert ({local(t)}); "
              "adsb.lol publishes the full trace with a delay - try again in ~30 min", file=sys.stderr)
    if a.minutes:
        pts = [q for q in pts if abs(q["t"] - t) <= a.minutes * 60]
    else:
        pts = leg(pts, t)
    if not [q for q in pts if q["alt"] is not None]:
        sys.exit("no positions found around the alert time")
    fields = airfields()
    lift = on_terrain(pts, fields)
    # the map exaggerates heights above the ground near the alert: the nearest airfield's (40 nm), else sea level
    d, base = min(((fw.haversine_nm(la, lo, rec["lat"], rec["lon"]), e) for la, lo, e in fields
                   if abs(la - rec["lat"]) < 1 and abs(lo - rec["lon"]) < 1.5), default=(99, 0))
    base = max(0, base) if d <= 40 else 0
    if lift:
        for x in alerts:
            if x["hex"] == rec["hex"] and isinstance(x.get("alt"), (int, float)):
                x["alt"] += lift
        source += f" (altitudes raised {lift:,} ft to the airfield's height)"
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(build(rec, alerts, pts, source, a.max_speed, a.max_gap_speed, a.airport, a.map, a.tiles,
                      a.sources, origin, base))
    print(f"wrote {a.out} ({len(pts)} positions, {source})")
    if not a.no_open:
        webbrowser.open("file://" + os.path.abspath(a.out))


if __name__ == "__main__":
    main()
