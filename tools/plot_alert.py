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

TZ = ZoneInfo("Asia/Jerusalem")
HEADERS = {"User-Agent": "flight-watch-plot/0.1", "Referer": "https://globe.adsbexchange.com/"}
ADSBLOL = "https://adsb.lol/data/traces/{xx}/trace_{kind}_{hex}.json"  # kind: full / recent
ADSBX = "https://globe.adsbexchange.com/globe_history/{day:%Y/%m/%d}/traces/{xx}/trace_full_{hex}.json"
PLOTLY = "https://cdn.plot.ly/plotly-2.35.2.min.js"
LEG_GAP = 30 * 60  # a silence this long (or a ground stop) ends a flight leg
GAP = 120          # longer without positions is a gap, not a jump (as in flight_watch.check_jump)


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


def points(tr: dict) -> list[dict]:
    """Trace rows -> dicts. Row: [dt, lat, lon, alt|"ground"|None, gs, track, flags, vrate,
    {details}|None, source, ...]; details (callsign, squawk) appear only when they change."""
    out, state = [], {}
    for p in tr.get("trace", []):
        ex = p[8] if len(p) > 8 and isinstance(p[8], dict) else {}
        for k in ("flight", "squawk"):
            if ex.get(k):
                state[k] = str(ex[k]).strip()
        src = p[9] if len(p) > 9 and p[9] else ex.get("type") or "?"
        out.append({"t": tr["timestamp"] + p[0], "lat": p[1], "lon": p[2],
                    "alt": 0 if p[3] == "ground" else p[3], "ground": p[3] == "ground",
                    "gs": p[4], "track": p[5], "new_leg": bool(p[6] & 2) if isinstance(p[6], int) else False,
                    "vrate": p[7], "src": "mlat" if "mlat" in src else "adsb" if "adsb" in src else src,
                    **state})
    return out


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


def leg(pts: list[dict], t: float) -> list[dict]:
    """The flight leg around t: airborne stretch between ground stops / long silences."""
    if not pts:
        return []
    i = min(range(len(pts)), key=lambda k: abs(pts[k]["t"] - t))
    lo = hi = i
    while lo > 0 and pts[lo]["t"] - pts[lo - 1]["t"] < LEG_GAP and not pts[lo]["new_leg"]:
        lo -= 1
        if pts[lo]["ground"] and pts[lo]["t"] < t - 600:  # take-off point
            break
    while hi < len(pts) - 1 and pts[hi + 1]["t"] - pts[hi]["t"] < LEG_GAP and not pts[hi + 1]["new_leg"]:
        hi += 1
        if pts[hi]["ground"] and pts[hi]["t"] > t + 600:  # landed
            break
    return pts[lo:hi + 1]


def jumps(air: list[dict], max_speed: float, gap_speed: float) -> dict[int, str]:
    """Hops (index of their end point -> description) that break flight_watch's POSITION_JUMP rules:
    too fast between close positions, reappearing too far after a gap, one position off and back."""
    def hop(i):
        p, q = air[i - 1], air[i]
        return q["t"] - p["t"], fw.haversine_nm(p["lat"], p["lon"], q["lat"], q["lon"])
    out = {}
    for i in range(1, len(air)):
        dt, d = hop(i)
        if dt <= 0 or d < 2:
            continue
        kt, at = d / (dt / 3600), local(air[i]["t"])
        if dt <= GAP and kt > max_speed:
            out[i] = f"jump: {d:.1f} nm in {dt:.0f} s (~{kt:.0f} kt) at {at}"
        elif dt > GAP and kt > gap_speed:
            out[i] = f"reappeared {d:.1f} nm away after {dt / 60:.1f} min without position (~{kt:.0f} kt) at {at}"
        elif i >= 2 and i - 1 not in out:
            a, b, c = air[i - 2], air[i - 1], air[i]
            span, ab = c["t"] - a["t"], hop(i - 1)[1]
            ac = fw.haversine_nm(a["lat"], a["lon"], c["lat"], c["lon"])
            if span > 0 and min(ab, d) >= 5 and (ab + d) / (span / 3600) > gap_speed \
                    and ac / (span / 3600) <= gap_speed:
                info = (f"one position {min(ab, d):.1f} nm off the track and back "
                        f"(~{(ab + d) / (span / 3600):.0f} kt via it) at {local(b['t'])}")
                out[i - 1] = out[i] = info
    return out


STEEP = 8000     # ft/min: altitude changing faster than this between positions (flight_watch --vrate)
STEEP_FT = 300   # ...by at least this much (25-ft steps a fraction of a second apart are not)
PURPLE = [155, 48, 217]


def steep(air: list[dict]) -> tuple[set, list[dict]]:
    """Altitude changing faster than airliners fly (a wobble, a dive, a glitch): the hops (index of
    their end point) and clusters of them (hops <= 10 s apart) with a short description."""
    hops = set()
    for i in range(1, len(air)):
        dt, dz = air[i]["t"] - air[i - 1]["t"], air[i]["alt"] - air[i - 1]["alt"]
        if 0 < dt <= GAP and abs(dz) >= STEEP_FT and abs(dz) / dt * 60 > STEEP:
            hops.add(i)
    clusters = []
    for i in sorted(hops):
        if clusters and air[i - 1]["t"] - air[clusters[-1]["end"]]["t"] <= 10:
            clusters[-1]["end"] = i
        else:
            clusters.append({"start": i - 1, "end": i})
    for c in clusters:
        alts = [air[k]["alt"] for k in range(c["start"], c["end"] + 1)]
        turns = [alts[0]] + [b for a, b, d in zip(alts, alts[1:], alts[2:]) if (b - a) * (d - b) < 0] + [alts[-1]]
        rate = max(abs(air[k]["alt"] - air[k - 1]["alt"]) / max(air[k]["t"] - air[k - 1]["t"], 0.1) * 60
                   for k in range(c["start"] + 1, c["end"] + 1) if k in hops)
        dt = air[c["end"]]["t"] - air[c["start"]]["t"]
        c["t"] = air[c["start"]]["t"]
        c["text"] = (f"altitude {' → '.join(f'{a:,}' for a in turns)} ft in {dt:.0f} s "
                     f"(up to {rate:,.0f} ft/min) at {local(c['t'])}")
    return hops, clusters


def hover(p: dict) -> str:
    alt = "ground" if p["ground"] else f"{p['alt']} ft" if p["alt"] is not None else "alt ?"
    parts = [f"{local(p['t'])} {local(p['t'], '%Z')} ({datetime.fromtimestamp(p['t'], timezone.utc):%H:%M:%SZ})",
             alt, f"{p['gs']:.0f} kt" if p["gs"] is not None else "",
             f"track {p['track']:.0f}°" if p["track"] is not None else "",
             f"{p['vrate']:+d} ft/min" if isinstance(p["vrate"], int) else "",
             f"{p['src']}", f"sq {p.get('squawk')}" if p.get("squawk") else ""]
    return "<br>".join(x for x in parts if x)


VIRIDIS = [(68, 1, 84), (59, 82, 139), (33, 145, 140), (94, 201, 98), (253, 231, 37)]
DECK = "https://unpkg.com/deck.gl@9.1.14/dist.min.js"
# Keyless map tiles that also load from a local file (tile.openstreetmap.org blocks pages without a
# Referer - "Access blocked" - and CARTO now needs an API key). --tiles takes any {z}/{x}/{y} URL.
TILES = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}"
TILES_CREDIT = ('Tiles © <a href="https://www.esri.com">Esri</a> — sources: Esri, HERE, Garmin, '
                '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors')


def viridis(f: float) -> list[int]:
    f = min(max(f, 0.0), 1.0) * (len(VIRIDIS) - 1)
    i = min(int(f), len(VIRIDIS) - 2)
    return [round(a + (b - a) * (f - i)) for a, b in zip(VIRIDIS[i], VIRIDIS[i + 1])]


def build(rec: dict, alerts: list[dict], pts: list[dict], source: str, max_speed: float = 1200,
          gap_speed: float = 750, airport: str = "TLV", use_map: bool = False, tiles: str = TILES) -> str:
    t0, t1 = pts[0]["t"], pts[-1]["t"]
    mine = [a for a in alerts if a["hex"] == rec["hex"] and t0 - 60 <= epoch(a["time"]) <= t1 + 60]
    if rec not in mine:
        mine.append(rec)
    mine.sort(key=lambda a: a["time"])
    air = [p for p in pts if p["alt"] is not None]
    if rec.get("military"):
        gap_speed = max_speed  # as the monitor: jets may really be that fast
    bad = jumps(air, max_speed, gap_speed)
    gaps = [(air[i - 1]["t"], air[i]["t"]) for i in range(1, len(air)) if air[i]["t"] - air[i - 1]["t"] > GAP]

    # distances in km east / north of the airport (equirectangular: fine at these ranges)
    lat0, lon0 = fw.AIRPORTS[airport][1:] if airport in fw.AIRPORTS else (air[0]["lat"], air[0]["lon"])
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
        a["_label"] = f"{a['kind']}{' ' + code.group(1) if code else ''} {local(epoch(a['time']), '%H:%M')}"
    # the first view: everything incl. the alert positions, at least 100 km across, 0-40,000 ft
    ax, ay = [p["x"] for p in air] + [a["_x"] for a in mine], [p["y"] for p in air] + [a["_y"] for a in mine]
    span = max(100.0, 1.1 * max(max(ax) - min(ax), max(ay) - min(ay)))
    cx, cy = (max(ax) + min(ax)) / 2, (max(ay) + min(ay)) / 2
    xr, yr = [cx - span / 2, cx + span / 2], [cy - span / 2, cy + span / 2]
    ztop = max(40000, max(p["alt"] for p in air) * 1.05)

    vert, signs = steep(air)
    first_alert = min(epoch(a["time"]) for a in mine)
    early = [c for c in signs if first_alert - 600 <= c["t"] < first_alert]
    for c in signs:  # the earliest steep change in the 10 min before the first alert
        c["first"] = bool(early) and c is early[0]
        c["label"] = ("first sign " if c["first"] else "altitude ") + local(c["t"], "%H:%M:%S")

    fx = [a["_x"] for a in mine] + [air[c["start"]]["x"] for c in signs if c["t"] >= first_alert - 600]
    fy = [a["_y"] for a in mine] + [air[c["start"]]["y"] for c in signs if c["t"] >= first_alert - 600]
    fspan = max(100.0, 1.3 * max(max(fx) - min(fx), max(fy) - min(fy)))
    fcx, fcy = (max(fx) + min(fx)) / 2, (max(fy) + min(fy)) / 2
    focus_km = [[fcx - fspan / 2, fcx + fspan / 2], [fcy - fspan / 2, fcy + fspan / 2]]
    flon = [a["lon"] for a in mine] + [air[c["start"]]["lon"] for c in signs if c["t"] >= first_alert - 600]
    flat = [a["lat"] for a in mine] + [air[c["start"]]["lat"] for c in signs if c["t"] >= first_alert - 600]
    focus_ll = [[min(flon), min(flat)], [max(flon), max(flat)]]

    def hop_info(i):
        p, q = air[i - 1], air[i]
        if i in bad:
            return "jump", bad[i]
        if i in vert:
            c = next(c for c in signs if c["start"] < i <= c["end"])
            return "steep", c["text"]
        if q["t"] - p["t"] > GAP:
            d, dt = fw.haversine_nm(p["lat"], p["lon"], q["lat"], q["lon"]), q["t"] - p["t"]
            return "gap", (f"no positions {local(p['t'])}-{local(q['t'])} ({dt / 60:.1f} min): "
                           f"{d * 1.852:.0f} km, average {d / (dt / 3600):.0f} kt")
        return "track", None
    hops = [(i, *hop_info(i)) for i in range(1, len(air))]

    # ground speed: reported where the feed has it (MLAT positions usually don't), else the
    # straight-line distance covered in the last ~60 s (zig-zag noise cancels), skipping jumps
    derived = []
    for i, q in enumerate(air):
        k = i
        while k > 0 and q["t"] - air[k - 1]["t"] <= 60 and k not in bad:
            k -= 1
        dt = q["t"] - air[k]["t"]
        derived.append(round(fw.haversine_nm(air[k]["lat"], air[k]["lon"], q["lat"], q["lon"]) / (dt / 3600))
                       if dt >= 30 else None)
    reported = [p["gs"] for p in air]
    top = max([v for v in reported + derived if v is not None] or [500])

    ident = rec.get("flight") or rec.get("callsign") or rec["hex"].upper()
    extra = ", ".join(x for x in (rec.get("airline"), "military" if rec.get("military") else None,
                                  rec.get("reg"), rec.get("type"), rec["hex"]) if x)
    title = f"{ident} ({extra}) {rec.get('route_text') or 'route unknown'}"
    sub = (f"{local(t0, '%d %b %Y %H:%M')}–{local(t1, '%H:%M %Z')}, {len(pts)} positions from {source}. "
           f"Selected: {rec['kind']} - {rec['message']}")

    def tip(p):
        return hover(p) + f"<br>{p['lat']:.4f}, {p['lon']:.4f}<br>" \
               f"{fw.haversine_nm(lat0, lon0, p['lat'], p['lon']) * 1.852:.0f} km from {airport}"

    if use_map:
        view = deck_view(air, hops, mine, rec, signs, tip, tiles, focus_ll)
    else:
        view = plotly_view(air, hops, mine, rec, signs, xr, yr, ztop, airport, title, sub, tip, km, focus_km)

    def iso(t):
        return datetime.fromtimestamp(t, TZ).isoformat()
    when = [iso(p["t"]) for p in air]
    t2 = [{"type": "scatter", "mode": "lines+markers", "name": "altitude", "x": when,
           "y": [p["alt"] for p in air], "marker": {"size": 3},
           "text": [hover(p) for p in air], "hovertemplate": "%{text}<extra></extra>"},
          {"type": "scatter", "mode": "lines", "name": "ground speed from positions (kt)", "x": when,
           "y": derived, "yaxis": "y2", "line": {"width": 1, "color": "#aaa", "dash": "dot"},
           "hoverinfo": "skip"},
          {"type": "scatter", "mode": "lines", "name": "reported ground speed (kt)", "x": when,
           "y": reported, "yaxis": "y2", "line": {"width": 1.5, "color": "#555"}, "hoverinfo": "skip"}]
    # alerts as red lines, jumps orange, gaps without positions shaded
    shapes = [{"type": "line", "x0": iso(epoch(a["time"])), "x1": iso(epoch(a["time"])), "yref": "paper",
               "y0": 0, "y1": 1, "line": {"color": "red", "dash": "dot"}} for a in mine]
    notes = [{"x": iso(epoch(a["time"])), "y": 1, "yref": "paper", "text": a["_label"], "showarrow": False,
              "textangle": -90, "xanchor": "right", "yanchor": "top", "font": {"color": "red"}} for a in mine]
    shapes += [{"type": "line", "x0": iso(air[i]["t"]), "x1": iso(air[i]["t"]), "yref": "paper", "y0": 0,
                "y1": 1, "layer": "below", "line": {"color": "#ff9900", "width": 2}} for i in sorted(bad)]
    shapes += [{"type": "line", "x0": iso(c["t"]), "x1": iso(c["t"]), "yref": "paper", "y0": 0, "y1": 1,
                "line": {"color": "rgb(155,48,217)", "width": 2 if c["first"] else 1}} for c in signs]
    notes += [{"x": iso(c["t"]), "y": 1, "yref": "paper", "text": c["label"], "showarrow": False, "textangle": -90,
               "xanchor": "right", "yanchor": "top", "font": {"color": "rgb(155,48,217)"}} for c in signs if c["first"]]
    shapes += [{"type": "rect", "x0": iso(a), "x1": iso(b), "yref": "paper", "y0": 0, "y1": 1,
                "layer": "below", "fillcolor": "rgba(128,128,128,0.15)", "line": {"width": 0}} for a, b in gaps]
    layout2d = {"height": 340, "margin": {"l": 60, "r": 60, "t": 20, "b": 40}, "shapes": shapes,
                "annotations": notes, "hovermode": "x", "yaxis": {"title": "altitude (ft)", "rangemode": "tozero"},
                "yaxis2": {"title": "kt", "overlaying": "y", "side": "right", "showgrid": False,
                           "range": [0, top * 1.05]},
                "legend": {"orientation": "h"}}
    # chart presets: whole flight, the alerts, each steep altitude change (seconds long: invisible otherwise)
    presets = [("whole flight", None),
               ("alerts", [iso(min(epoch(a["time"]) for a in mine + [rec]) - 120),
                           iso(max(epoch(a["time"]) for a in mine + [rec]) + 120)])]
    presets += [(c["label"], [iso(c["t"] - 30), iso(air[c["end"]]["t"] + 30)]) for c in signs]
    chart_buttons = " ".join(f'<button class="cz" data-i="{i}">{name}</button>' for i, (name, _) in enumerate(presets))
    data = json.dumps({"t2": t2, "l2": layout2d, "presets": [r for _, r in presets], **view["data"]})
    links = " ".join(f'<a href="{u}" target="_blank">{k}</a>' for k, u in rec.get("links", {}).items())
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{ident} replay</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="{PLOTLY}"></script>{view["head"]}
<style>html,body{{overflow-x:hidden}} body{{font-family:sans-serif;margin:8px;background:#fff}} a{{margin-right:1em}}
h1{{font-size:18px;margin:4px 0}} .sub{{font-size:13px;color:#444;margin-bottom:6px}}
#d3{{position:relative}} .legend{{font-size:12px;margin:4px 0}} .legend span{{margin-right:14px}}</style>
</head><body>
{view["html"]}
<div class="legend">chart below: {chart_buttons} · drag across it to zoom in, double-click to zoom out</div>
<div id="d2"></div>
<p>{links}</p>
<script>
const D = {data};
Plotly.newPlot("d2", D.t2, D.l2, {{responsive: true}});
document.querySelectorAll("button.cz").forEach(b => b.onclick = () => {{
  const r = D.presets[+b.dataset.i];
  Plotly.relayout("d2", r ? {{"xaxis.range": r}} : {{"xaxis.autorange": true}});
}});
{view["js"]}
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


def plotly_view(air, hops, mine, rec, signs, xr, yr, ztop, airport, title, sub, tip, km, focus) -> dict:
    """Rotatable 3D with plotly: km east / north of the airport, altitude in ft."""
    def line(kind):
        x, y, z, txt = [], [], [], []
        for i, k, info in hops:
            if k == kind:
                p, q = air[i - 1], air[i]
                x += [p["x"], q["x"], None]
                y += [p["y"], q["y"], None]
                z += [p["alt"], q["alt"], None]
                txt += [info, info, None]
        return x, y, z, txt
    # the track, broken at gaps and jumps (drawn separately)
    tx, ty, tz, tc = [], [], [], []
    t0 = air[0]["t"]
    for i, q in enumerate(air):
        if i and hops[i - 1][1] != "track":
            tx.append(None), ty.append(None), tz.append(None), tc.append(q["t"] - t0)
        tx.append(q["x"]), ty.append(q["y"]), tz.append(q["alt"]), tc.append(q["t"] - t0)
    traces = [{"type": "scatter3d", "mode": "lines", "name": "track (colour = time)", "x": tx, "y": ty, "z": tz,
               "line": {"width": 5, "color": tc, "colorscale": "Viridis"}, "hoverinfo": "skip"}]
    gx, gy, gz, gt = line("gap")
    if gx:
        traces.append({"type": "scatter3d", "mode": "lines", "name": f"no data > {GAP} s ({len(gx) // 3})",
                       "x": gx, "y": gy, "z": gz, "hovertext": gt, "hoverinfo": "text",
                       "line": {"width": 3, "color": "#888", "dash": "dash"}})
    for src, colour in (("adsb", "#1f77b4"), ("mlat", "#ff7f0e")):
        sel = [p for p in air if p["src"] == src]
        other = src == "adsb" and [p for p in air if p["src"] not in ("adsb", "mlat")]
        sel += other or []
        if sel:
            traces.append({"type": "scatter3d", "mode": "markers", "name": f"{src} positions" +
                           (" (+other)" if other else ""), "x": [p["x"] for p in sel],
                           "y": [p["y"] for p in sel], "z": [p["alt"] for p in sel],
                           "marker": {"size": 2, "color": colour}, "text": [tip(p) for p in sel],
                           "hovertemplate": "%{text}<extra></extra>"})
    # shadow on the ground, Israel's outline and airports for orientation
    traces.append({"type": "scatter3d", "mode": "lines", "name": "ground track", "x": [p["x"] for p in air],
                   "y": [p["y"] for p in air], "z": [0] * len(air),
                   "line": {"width": 2, "color": "rgba(128,128,128,0.5)"}, "hoverinfo": "skip"})
    ring = [km(la, lo) for la, lo in fw.ISRAEL + fw.ISRAEL[:1]]
    ring = [(min(max(x, xr[0]), xr[1]), min(max(y, yr[0]), yr[1])) for x, y in ring]
    traces.append({"type": "scatter3d", "mode": "lines", "name": "Israel (rough)",
                   "x": [p[0] for p in ring], "y": [p[1] for p in ring], "z": [0] * len(ring),
                   "line": {"width": 2, "color": "#2ca02c"}, "hoverinfo": "skip"})
    apts = [(k, km(v[0], v[1])) for k, v in fw.REGION_AIRPORTS.items()]
    apts = [(k, (x, y)) for k, (x, y) in apts if xr[0] <= x <= xr[1] and yr[0] <= y <= yr[1]]
    if apts:
        traces.append({"type": "scatter3d", "mode": "markers+text", "name": "airports",
                       "x": [p[0] for _, p in apts], "y": [p[1] for _, p in apts], "z": [0] * len(apts),
                       "text": [k for k, _ in apts], "textposition": "top center",
                       "marker": {"size": 3, "color": "#555", "symbol": "square"}, "hoverinfo": "text"})
    vx, vy, vz, vt = line("steep")
    if vx:
        traces.append({"type": "scatter3d", "mode": "lines", "name": f"steep altitude change ({len(signs)})",
                       "x": vx, "y": vy, "z": vz, "hovertext": vt, "hoverinfo": "text",
                       "line": {"width": 7, "color": "rgb(155,48,217)"}})
        traces.append({"type": "scatter3d", "mode": "text", "showlegend": False,
                       "x": [air[c["start"]]["x"] for c in signs], "y": [air[c["start"]]["y"] for c in signs],
                       "z": [air[c["start"]]["alt"] for c in signs], "text": [c["label"] for c in signs],
                       "textposition": "bottom center", "hovertext": [c["text"] for c in signs], "hoverinfo": "text",
                       "textfont": {"color": "rgb(155,48,217)", "size": [13 if c["first"] else 10 for c in signs]}})
    jx, jy, jz, jt = line("jump")
    if jx:
        traces.append({"type": "scatter3d", "mode": "lines", "name": f"position jumps ({len(jx) // 3})",
                       "x": jx, "y": jy, "z": jz, "hovertext": jt, "hoverinfo": "text",
                       "line": {"width": 6, "color": "#ff9900"}})
    # alerts: a small red marker where the monitor raised it, a drop line to the ground
    for a in mine:
        traces.append({"type": "scatter3d", "mode": "markers+text", "name": f"alert: {a['_label']}",
                       "x": [a["_x"]], "y": [a["_y"]], "z": [a["_alt"]], "text": [a["_label"]],
                       "textposition": "top center",
                       "textfont": {"color": "red", "size": 13 if a is rec else 11},
                       "marker": {"size": 4, "color": "red"},
                       "hovertext": [f"<b>{a['_label']}</b><br>{a['message']}"], "hoverinfo": "text"})
        traces.append({"type": "scatter3d", "mode": "lines", "showlegend": False, "hoverinfo": "skip",
                       "x": [a["_x"]] * 2, "y": [a["_y"]] * 2, "z": [0, a["_alt"]],
                       "line": {"width": 2, "color": "red", "dash": "dot"}})
    cursor = len(traces)  # the black "you are here" marker, moved from the time chart
    traces.append({"type": "scatter3d", "mode": "markers", "name": "cursor", "showlegend": False,
                   "x": [None], "y": [None], "z": [None], "hoverinfo": "skip",
                   "marker": {"size": 6, "color": "black"}})
    layout = {"title": {"text": f"{title}<br><sub>{sub}</sub>"}, "height": 720,
              "margin": {"l": 0, "r": 0, "t": 70, "b": 0}, "legend": {"x": 0, "y": 1},
              "scene": {"xaxis": {"title": f"km east of {airport}", "range": xr},
                        "yaxis": {"title": f"km north of {airport}", "range": yr},
                        "zaxis": {"title": "altitude (ft)", "range": [0, ztop]},
                        "aspectmode": "manual", "aspectratio": {"x": 2, "y": 2, "z": 0.7}}}
    helptext = ("Left-drag: rotate · right-drag: move · scroll: zoom · hover a point for details; "
                "hover the chart below to find that moment above")
    html = (f'<div class="legend"><button id="zoomin">zoom to alerts</button> <button id="whole">whole flight'
            f'</button> · {helptext}</div><div id="d3"></div>')
    js = ('Plotly.newPlot("d3", D.t3, D.l3, {responsive: true});\n'
          'function moveCursor([x, y, z]) {\n'
          '  Plotly.restyle("d3", {x: [[x]], y: [[y]], z: [[z]]}, [D.cursor]);\n}\n'
          'const box = (r) => ({"scene.xaxis.range": r[0], "scene.yaxis.range": r[1]});\n'
          'document.getElementById("zoomin").onclick = () => Plotly.relayout("d3", box(D.focus));\n'
          'document.getElementById("whole").onclick = () => Plotly.relayout("d3", box(D.whole));')
    return {"head": "", "html": html,
            "data": {"t3": traces, "l3": layout, "cursor": cursor, "focus": focus, "whole": [xr, yr],
                     "pos": [[p["x"], p["y"], p["alt"]] for p in air]},
            "js": js}


def deck_view(air, hops, mine, rec, signs, tip, tiles=TILES, focus=None) -> dict:
    """The same over a street map (Esri World Street Map tiles by default) with deck.gl: drag to pan,
    right-drag / Ctrl+drag to tilt and rotate. Altitude exaggerated (adjustable)."""
    t0, t1 = air[0]["t"], air[-1]["t"]
    ft = 0.3048
    segs = []
    for i, kind, info in hops:
        p, q = air[i - 1], air[i]
        colour = viridis((q["t"] - t0) / max(1, t1 - t0)) if kind == "track" else \
            [255, 153, 0] if kind == "jump" else PURPLE if kind == "steep" else [110, 110, 110]
        a, b = [p["lon"], p["lat"], p["alt"] * ft], [q["lon"], q["lat"], q["alt"] * ft]
        if kind != "gap":
            segs.append({"a": a, "b": b, "c": colour, "k": kind, "tip": info})
            continue
        n = 30  # no data: dashed (deck.gl lines have no dash style, so draw every other piece)
        for k in range(0, n, 2):
            f0, f1 = k / n, (k + 1) / n
            segs.append({"a": [x + (y - x) * f0 for x, y in zip(a, b)], "b": [x + (y - x) * f1 for x, y in zip(a, b)],
                         "c": colour, "k": kind, "tip": info})
    data = {"segs": segs,
            "pts": [{"p": [p["lon"], p["lat"], p["alt"] * ft], "tip": tip(p),
                     "c": [255, 127, 14] if p["src"] == "mlat" else [31, 119, 180]} for p in air],
            "alerts": [{"p": [a["lon"], a["lat"], a["_alt"] * ft], "label": a["_label"],
                        "tip": f"<b>{a['_label']}</b><br>{a['message']}", "sel": a is rec} for a in mine],
            "airports": [{"p": [v[1], v[0], 0], "label": k} for k, v in fw.REGION_AIRPORTS.items()],
            "signs": [{"p": [air[c["start"]]["lon"], air[c["start"]]["lat"], air[c["start"]]["alt"] * ft],
                       "label": c["label"], "tip": c["text"], "first": c["first"]} for c in signs],
            "bounds": [[min(p["lon"] for p in air + [{"lon": a["lon"]} for a in mine]),
                        min(p["lat"] for p in air + [{"lat": a["lat"]} for a in mine])],
                       [max(p["lon"] for p in air + [{"lon": a["lon"]} for a in mine]),
                        max(p["lat"] for p in air + [{"lat": a["lat"]} for a in mine])]],
            "focus": focus,
            # the camera aims at this height (m), not the ground: zooming in on a track drawn high
            # above the map otherwise flies underneath it
            "focusAlt": sorted([a["_alt"] for a in mine] + [air[c["start"]]["alt"] for c in signs if c["first"]])[
                len(mine) // 2] * ft,
            "tiles": tiles,
            "pos": [[p["lon"], p["lat"], p["alt"] * ft] for p in air]}
    ident = rec.get("flight") or rec.get("callsign") or rec["hex"].upper()
    credit = TILES_CREDIT if tiles == TILES else f"map tiles: {tiles.split('/')[2]}"
    first = next((c for c in signs if c["first"]), None)
    first_btn = f' <button id="firstsign">{first["label"]}</button>' if first else ""
    html = f"""<h1>{ident} {rec.get('route_text') or 'route unknown'}</h1>
<div class="sub">{rec['kind']} - {rec['message']} ({len(air)} positions)</div>
<div class="legend"><span style="color:#3b528b">━ track (colour = time)</span>
<span style="color:#ff9900">━ position jumps</span><span style="color:#9b30d9">━ steep altitude change</span>
<span style="color:#888">╌ no data &gt; {GAP} s</span><span style="color:red">● alerts</span>
· altitude ×<input id="zx" type="range" min="1" max="20" value="5" style="vertical-align:middle;width:110px"><b
id="zxv">5</b> <button id="zoomin">zoom to alerts</button> <button id="top">top view</button>
<button id="reset">whole flight</button>{first_btn}
<button id="helpbtn">? how to move</button></div>
<div id="help" style="display:none;font-size:13px;background:#fffbe6;border:1px solid #e0c060;padding:8px 12px;
margin:4px 0;max-width:720px"><b>Moving around the map</b><br>
• <b>Drag</b> with the left mouse button: move the map.<br>
• <b>Scroll</b> (or pinch on a touchpad): zoom in and out. Double-click zooms in.<br>
• <b>Ctrl + drag</b> (or Shift + drag, or drag with the <b>right</b> button): turn the view.
Drag <b>sideways</b> to rotate around, <b>up and down</b> to tilt between looking straight down and
looking from the side.<br>
• Keyboard: arrows move, + / − zoom, Shift + arrows rotate and tilt.<br>
• <b>zoom to alerts</b> flies to where the alerts are; <b>first sign</b> (when there is one) to the earliest
steep altitude change; <b>top view</b> looks straight down (like a normal map); <b>whole flight</b> goes back
to the start.<br>
• Zooming heads for the height of the alerts, not the ground below them (the track is drawn high above
the map), so the alerts stay in view as you zoom in.<br>
• Hover a point or line for details; hover the chart below the map to put a black dot on that moment.<br>
• The altitude slider stretches heights so climbs and dives are visible (×1 is true scale).</div>
<div id="d3" style="height:640px"></div>
<div style="font-size:11px;color:#666">{credit}</div>"""
    js = r"""
let Z = 5, cursor = null;
const P = (p) => [p[0], p[1], p[2] * Z];
function layers() {
  return [
    new deck.TileLayer({id: "tiles", data: D.tiles, minZoom: 0, maxZoom: 19, tileSize: 256,
      renderSubLayers: props => {
        const [[w, s], [e, n]] = props.tile.boundingBox;
        return new deck.BitmapLayer(props, {data: null, image: props.data, bounds: [w, s, e, n]});
      }}),
    new deck.LineLayer({id: "shadow", data: D.segs, getSourcePosition: d => [d.a[0], d.a[1], 0],
      getTargetPosition: d => [d.b[0], d.b[1], 0], getColor: [100, 100, 100, 90], getWidth: 1}),
    new deck.LineLayer({id: "drops", data: D.alerts, getSourcePosition: d => [d.p[0], d.p[1], 0],
      getTargetPosition: d => P(d.p), getColor: [255, 0, 0, 160], getWidth: 1}),
    new deck.LineLayer({id: "track", data: D.segs, pickable: true, getSourcePosition: d => P(d.a),
      getTargetPosition: d => P(d.b), getColor: d => d.c,
      getWidth: d => d.k === "jump" ? 3 : d.k === "steep" ? 3 : d.k === "gap" ? 1.5 : 2,
      updateTriggers: {getSourcePosition: Z, getTargetPosition: Z}}),
    new deck.ScatterplotLayer({id: "pts", data: D.pts, pickable: true, getPosition: d => P(d.p),
      getFillColor: d => d.c, radiusUnits: "pixels", getRadius: 1.5, updateTriggers: {getPosition: Z}}),
    new deck.ScatterplotLayer({id: "airports", data: D.airports, getPosition: d => d.p,
      getFillColor: [80, 80, 80], radiusUnits: "pixels", getRadius: 3}),
    new deck.TextLayer({id: "airport-names", data: D.airports, getPosition: d => d.p, getText: d => d.label,
      getSize: 12, getColor: [60, 60, 60], getPixelOffset: [0, -12]}),
    new deck.ScatterplotLayer({id: "alerts", data: D.alerts, pickable: true, getPosition: d => P(d.p),
      getFillColor: [255, 0, 0], radiusUnits: "pixels", getRadius: d => d.sel ? 4 : 3,
      updateTriggers: {getPosition: Z}}),
    new deck.TextLayer({id: "alert-names", data: D.alerts, getPosition: d => P(d.p), getText: d => d.label,
      getSize: d => d.sel ? 15 : 12, getColor: [220, 0, 0], getPixelOffset: [0, -14],
      fontWeight: 600, updateTriggers: {getPosition: Z}}),
    new deck.TextLayer({id: "signs", data: D.signs, pickable: true, getPosition: d => P(d.p), getText: d => d.label,
      getSize: d => d.first ? 15 : 11, getColor: [155, 48, 217], getPixelOffset: [0, 16], fontWeight: 600,
      updateTriggers: {getPosition: Z}}),
    new deck.ScatterplotLayer({id: "cursor", data: cursor ? [cursor] : [], getPosition: d => P(d),
      getFillColor: [0, 0, 0], radiusUnits: "pixels", getRadius: 5, updateTriggers: {getPosition: Z}}),
  ];
}
const el = document.getElementById("d3");
const fit = new deck.WebMercatorViewport({width: el.clientWidth, height: el.clientHeight})
  .fitBounds(D.bounds, {padding: 60});
const aim = (alt) => [0, 0, (alt === undefined ? D.focusAlt : alt) * Z];  // camera target height
const start = {longitude: fit.longitude, latitude: fit.latitude, zoom: Math.min(fit.zoom, 10),
               pitch: 50, bearing: 0, maxPitch: 85, position: aim()};
let view = start, aimAlt;  // current view; height aimed at (undefined: the alerts')
const map = new deck.DeckGL({container: "d3", initialViewState: start,
  controller: true, layers: layers(), onViewStateChange: ({viewState}) => { view = viewState; },
  getTooltip: ({object}) => object && object.tip ? {html: object.tip} : null});
const go = (v, alt) => {
  aimAlt = alt;
  map.setProps({initialViewState: {...view, ...v, position: aim(alt), transitionDuration: 700,
                                   transitionInterpolator: new deck.FlyToInterpolator(), _t: Date.now()}});
};
document.getElementById("top").onclick = () => go({pitch: 0, bearing: 0}, aimAlt);
document.getElementById("reset").onclick = () => go(start);
const near = new deck.WebMercatorViewport({width: el.clientWidth, height: el.clientHeight})
  .fitBounds(D.focus, {padding: 120});
document.getElementById("zoomin").onclick = () =>
  go({longitude: near.longitude, latitude: near.latitude, zoom: Math.min(near.zoom, 9), pitch: 50, bearing: 0});
const fs = document.getElementById("firstsign"), sign = D.signs.find(s => s.first);
if (fs) fs.onclick = () => go({longitude: sign.p[0], latitude: sign.p[1], zoom: 12, pitch: 55, bearing: 0}, sign.p[2]);
document.getElementById("helpbtn").onclick = () => {
  const h = document.getElementById("help"); h.style.display = h.style.display === "none" ? "block" : "none";
};
document.getElementById("zx").oninput = (e) => {
  Z = +e.target.value; document.getElementById("zxv").textContent = Z;
  map.setProps({layers: layers(), initialViewState: {...view, position: aim(aimAlt), _t: Date.now()}});
};
function moveCursor(p) { cursor = p; map.setProps({layers: layers()}); }
"""
    return {"head": f'\n<script src="{DECK}"></script>', "html": html, "data": data, "js": js}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--alerts", default="alerts.jsonl")
    p.add_argument("--pick", type=int, help="alert number in the list (1 = newest)")
    p.add_argument("--filter", nargs="+", default=[], metavar="TEXT",
                   help="list only alerts whose row contains all these (any case): LY347, Zurich, RETURNED")
    p.add_argument("--minutes", type=float, help="plot this long before and after the alert, across "
                                                  "landings and gaps (default: the alert's flight leg)")
    p.add_argument("--map", action="store_true",
                   help="draw over a street map (deck.gl; tilt / rotate) instead of km axes")
    p.add_argument("--tiles", default=TILES, help="map tile URL template ({z}, {x}, {y}) for --map")
    p.add_argument("--airport", default="TLV", help="km axes are measured from this airport")
    p.add_argument("--max-speed", type=float, default=1200,
                   help="kt; faster hops in the trace are drawn as jumps (as flight_watch.py --max-speed)")
    p.add_argument("--max-gap-speed", type=float, default=750,
                   help="kt; average speed over a gap / via one off-track position (as flight_watch.py)")
    p.add_argument("--out", default="tmp_plot.html")
    p.add_argument("--no-open", action="store_true", help="don't open the page in a browser")
    a = p.parse_args(argv)
    alerts = load_alerts(a.alerts)
    if not alerts:
        sys.exit(f"no alerts in {a.alerts}")
    rec = choose(alerts, a.pick, a.filter)
    t = epoch(rec["time"])
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
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(build(rec, alerts, pts, source, a.max_speed, a.max_gap_speed, a.airport, a.map, a.tiles))
    print(f"wrote {a.out} ({len(pts)} positions, {source})")
    if not a.no_open:
        webbrowser.open("file://" + os.path.abspath(a.out))


if __name__ == "__main__":
    main()
