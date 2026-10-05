#!/usr/bin/env python3
"""Replay an alert as a rotatable 3D track (plus altitude over time), with the alerts marked.

Lists alerts.jsonl from newest to oldest, asks which one to plot, fetches that aircraft's trace
and writes a self-contained HTML page (plotly.js from a CDN; nothing else is stored).

Trace sources (readsb trace_full JSON, positions at full rate):
  adsb.lol   https://adsb.lol/data/traces/<last 2 hex>/trace_full_<hex>.json   (recent ~day)
  ADS-B Exchange globe history, per UTC day, for anything older

Examples:
  python tools/plot_alert.py                  # choose from the list
  python tools/plot_alert.py --pick 1         # newest alert
  python tools/plot_alert.py --pick 3 --minutes 20 --out tmp_plot.html
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import flight_watch as fw  # noqa: E402

TZ = ZoneInfo("Asia/Jerusalem")
HEADERS = {"User-Agent": "flight-watch-plot/0.1", "Referer": "https://globe.adsbexchange.com/"}
ADSBLOL = "https://adsb.lol/data/traces/{xx}/trace_full_{hex}.json"
ADSBX = "https://globe.adsbexchange.com/globe_history/{day:%Y/%m/%d}/traces/{xx}/trace_full_{hex}.json"
PLOTLY = "https://cdn.plot.ly/plotly-2.35.2.min.js"
LEG_GAP = 30 * 60  # a silence this long (or a ground stop) ends a flight leg


def epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def local(t: float, fmt: str = "%H:%M:%S") -> str:
    return datetime.fromtimestamp(t, TZ).strftime(fmt)


def load_alerts(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()][::-1]  # newest first


def describe(rec: dict) -> str:
    ident = rec.get("flight") or rec.get("callsign") or rec["hex"].upper()
    extra = ", ".join(x for x in (rec.get("airline"), "military" if rec.get("military") else None,
                                  rec.get("reg"), rec.get("type")) if x)
    route = rec.get("route_text") or "route unknown"
    when = datetime.fromisoformat(rec["time"].replace("Z", "+00:00")).astimezone(TZ)
    return (f"{when:%d %b %H:%M} {rec['kind']:<16} {ident} ({extra}) {route} - "
            f"{rec['message'].split(' [also:')[0][:70]}")


def choose(alerts: list[dict], pick: int | None) -> dict:
    if pick is None:
        for i, rec in enumerate(alerts[:40], 1):
            print(f"{i:3d}  {describe(rec)}")
        if len(alerts) > 40:
            print(f"     ... {len(alerts) - 40} older (use --pick N)")
        pick = int(input("plot which? [1] ").strip() or 1)
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
    tr = fetch(ADSBLOL.format(xx=xx, hex=hexid))
    pts = points(tr) if tr else []
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


def hover(p: dict) -> str:
    alt = "ground" if p["ground"] else f"{p['alt']} ft" if p["alt"] is not None else "alt ?"
    parts = [f"{local(p['t'])} {local(p['t'], '%Z')} ({datetime.fromtimestamp(p['t'], timezone.utc):%H:%M:%SZ})",
             alt, f"{p['gs']:.0f} kt" if p["gs"] is not None else "",
             f"track {p['track']:.0f}°" if p["track"] is not None else "",
             f"{p['vrate']:+d} ft/min" if isinstance(p["vrate"], int) else "",
             f"{p['src']}", f"sq {p.get('squawk')}" if p.get("squawk") else ""]
    return "<br>".join(x for x in parts if x)


def build(rec: dict, alerts: list[dict], pts: list[dict], source: str, max_speed: float = 1200) -> str:
    t0, t1 = pts[0]["t"], pts[-1]["t"]
    mine = [a for a in alerts if a["hex"] == rec["hex"] and t0 - 60 <= epoch(a["time"]) <= t1 + 60]
    if rec not in mine:
        mine.append(rec)
    air = [p for p in pts if p["alt"] is not None]
    lat0 = sum(p["lat"] for p in air) / len(air)
    kx = math.cos(math.radians(lat0))
    xs = [p["lon"] for p in air]
    ys = [p["lat"] for p in air]
    pad = max(0.05, 0.1 * max(max(xs) - min(xs), max(ys) - min(ys)))
    xr, yr = [min(xs) - pad / kx, max(xs) + pad / kx], [min(ys) - pad, max(ys) + pad]
    w, h = (xr[1] - xr[0]) * kx, yr[1] - yr[0]
    m = max(w, h)
    aspect = {"x": w / m * 2, "y": h / m * 2, "z": 0.7}

    traces = [{"type": "scatter3d", "mode": "lines", "name": "track (colour = time)",
               "x": xs, "y": ys, "z": [p["alt"] for p in air],
               "line": {"width": 5, "color": [p["t"] - t0 for p in air], "colorscale": "Viridis"},
               "hoverinfo": "skip"}]
    for src, colour in (("adsb", "#1f77b4"), ("mlat", "#ff7f0e")):
        sel = [p for p in air if p["src"] == src]
        other = src == "adsb" and [p for p in air if p["src"] not in ("adsb", "mlat")]
        sel += other or []
        if sel:
            traces.append({"type": "scatter3d", "mode": "markers", "name": f"{src} positions" +
                           (" (+other)" if other else ""), "x": [p["lon"] for p in sel],
                           "y": [p["lat"] for p in sel], "z": [p["alt"] for p in sel],
                           "marker": {"size": 2, "color": colour}, "text": [hover(p) for p in sel],
                           "hovertemplate": "%{text}<extra></extra>"})
    # shadow on the ground, Israel's outline and airports for orientation
    traces.append({"type": "scatter3d", "mode": "lines", "name": "ground track", "x": xs, "y": ys,
                   "z": [0] * len(xs), "line": {"width": 2, "color": "rgba(128,128,128,0.5)"},
                   "hoverinfo": "skip"})
    ring = [(min(max(la, yr[0]), yr[1]), min(max(lo, xr[0]), xr[1])) for la, lo in fw.ISRAEL + fw.ISRAEL[:1]]
    traces.append({"type": "scatter3d", "mode": "lines", "name": "Israel (rough)",
                   "x": [p[1] for p in ring], "y": [p[0] for p in ring], "z": [0] * len(ring),
                   "line": {"width": 2, "color": "#2ca02c"}, "hoverinfo": "skip"})
    apts = [(k, v) for k, v in fw.REGION_AIRPORTS.items()
            if yr[0] <= v[0] <= yr[1] and xr[0] <= v[1] <= xr[1]]
    if apts:
        traces.append({"type": "scatter3d", "mode": "markers+text", "name": "airports",
                       "x": [v[1] for _, v in apts], "y": [v[0] for _, v in apts], "z": [0] * len(apts),
                       "text": [k for k, _ in apts], "textposition": "top center",
                       "marker": {"size": 3, "color": "#555", "symbol": "square"}, "hoverinfo": "text"})

    # jumps: every hop that breaks the monitor's POSITION_JUMP rule, alerted or not
    jx, jy, jz, jt, bad = [], [], [], [], set()
    for i in range(1, len(air)):
        p, q = air[i - 1], air[i]
        dt, d = q["t"] - p["t"], fw.haversine_nm(p["lat"], p["lon"], q["lat"], q["lon"])
        if not (0 < dt <= 120 and d >= 2 and d / (dt / 3600) > max_speed):
            continue
        bad.add(i)
        info = f"jump: {d:.1f} nm in {dt:.0f} s (~{d / (dt / 3600):.0f} kt) at {local(q['t'])}"
        jx += [p["lon"], q["lon"], None]
        jy += [p["lat"], q["lat"], None]
        jz += [p["alt"], q["alt"], None]
        jt += [info, info, None]
    if jx:
        traces.append({"type": "scatter3d", "mode": "lines", "name": f"jumps > {max_speed:.0f} kt ({len(bad)})",
                       "x": jx, "y": jy, "z": jz, "hovertext": jt, "hoverinfo": "text",
                       "line": {"width": 6, "color": "#ff9900"}})

    # alerts: a small red marker where the monitor raised it, a drop line to the ground
    shapes, notes = [], []
    for a in sorted(mine, key=lambda a: a["time"]):
        ta = epoch(a["time"])
        alt = a.get("alt")
        if not isinstance(alt, (int, float)):
            near = min(air, key=lambda p: abs(p["t"] - ta))
            alt = near["alt"]
        label = f"{a['kind']} {local(ta, '%H:%M')}"
        traces.append({"type": "scatter3d", "mode": "markers+text", "name": f"alert: {label}",
                       "x": [a["lon"]], "y": [a["lat"]], "z": [alt], "text": [label],
                       "textposition": "top center",
                       "textfont": {"color": "red", "size": 13 if a is rec else 11},
                       "marker": {"size": 4, "color": "red"},
                       "hovertext": [f"<b>{label}</b><br>{a['message']}"], "hoverinfo": "text"})
        traces.append({"type": "scatter3d", "mode": "lines", "showlegend": False, "hoverinfo": "skip",
                       "x": [a["lon"]] * 2, "y": [a["lat"]] * 2, "z": [0, alt],
                       "line": {"width": 2, "color": "red", "dash": "dot"}})
        shapes.append({"type": "line", "x0": ta, "x1": ta, "yref": "paper", "y0": 0, "y1": 1,
                       "line": {"color": "red", "dash": "dot"}})
        notes.append({"x": ta, "y": 1, "yref": "paper", "text": label, "showarrow": False,
                      "textangle": -90, "xanchor": "right", "yanchor": "top", "font": {"color": "red"}})
    # the black "you are here" marker, moved by hovering / clicking the time chart
    cursor = len(traces)
    traces.append({"type": "scatter3d", "mode": "markers", "name": "cursor", "showlegend": False,
                   "x": [None], "y": [None], "z": [None], "hoverinfo": "skip",
                   "marker": {"size": 6, "color": "black"}})

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
    layout3d = {"title": {"text": f"{title}<br><sub>{sub}</sub>"}, "height": 720,
                "margin": {"l": 0, "r": 0, "t": 70, "b": 0}, "legend": {"x": 0, "y": 1},
                "scene": {"xaxis": {"title": "lon", "range": xr}, "yaxis": {"title": "lat", "range": yr},
                          "zaxis": {"title": "altitude (ft)", "rangemode": "tozero"},
                          "aspectmode": "manual", "aspectratio": aspect}}

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
    for s, n in zip(shapes, notes):
        s["x0"] = s["x1"] = n["x"] = iso(s["x0"])
    layout2d = {"height": 340, "margin": {"l": 60, "r": 60, "t": 20, "b": 40}, "shapes": shapes,
                "annotations": notes, "hovermode": "x", "yaxis": {"title": "altitude (ft)", "rangemode": "tozero"},
                "yaxis2": {"title": "kt", "overlaying": "y", "side": "right", "showgrid": False,
                           "range": [0, top * 1.05]},
                "legend": {"orientation": "h"}}
    data = json.dumps({"t3": traces, "l3": layout3d, "t2": t2, "l2": layout2d, "cursor": cursor,
                       "pos": [[p["lon"], p["lat"], p["alt"]] for p in air]})
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{ident} replay</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="{PLOTLY}"></script>
<style>body{{font-family:sans-serif;margin:8px;background:#fff}} a{{margin-right:1em}}</style>
</head><body>
<div id="d3"></div><div id="d2"></div>
<p>{" ".join(f'<a href="{u}" target="_blank">{k}</a>' for k, u in rec.get("links", {}).items())}</p>
<script>
const D = {data};
Plotly.newPlot("d3", D.t3, D.l3, {{responsive: true}});
Plotly.newPlot("d2", D.t2, D.l2, {{responsive: true}});
// hovering (or clicking) the time chart puts a black marker on that position in 3D
let shown = -1;
function show(ev) {{
  const p = ev.points.find(p => p.curveNumber === 0) || ev.points[0];
  const i = p.pointIndex;
  if (i === shown || !D.pos[i]) return;
  shown = i;
  const [x, y, z] = D.pos[i];
  Plotly.restyle("d3", {{x: [[x]], y: [[y]], z: [[z]]}}, [D.cursor]);
}}
const d2 = document.getElementById("d2");
d2.on("plotly_hover", show);
d2.on("plotly_click", show);
</script></body></html>
"""

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--alerts", default="alerts.jsonl")
    p.add_argument("--pick", type=int, help="alert number in the list (1 = newest)")
    p.add_argument("--minutes", type=float, help="plot only this long before and after the alert "
                                                  "(default: the whole flight leg)")
    p.add_argument("--max-speed", type=float, default=1200,
                   help="kt; faster hops in the trace are drawn as jumps (as flight_watch.py --max-speed)")
    p.add_argument("--out", default="tmp_plot.html")
    p.add_argument("--no-open", action="store_true", help="don't open the page in a browser")
    a = p.parse_args(argv)
    alerts = load_alerts(a.alerts)
    if not alerts:
        sys.exit(f"no alerts in {a.alerts}")
    rec = choose(alerts, a.pick)
    t = epoch(rec["time"])
    print(f"fetching trace of {rec['hex']} ...", file=sys.stderr)
    pts, source = trace_for(rec["hex"], t)
    pts = leg(pts, t)
    if a.minutes:
        pts = [q for q in pts if abs(q["t"] - t) <= a.minutes * 60]
    if not [q for q in pts if q["alt"] is not None]:
        sys.exit("no positions found around the alert time")
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(build(rec, alerts, pts, source, a.max_speed))
    print(f"wrote {a.out} ({len(pts)} positions, {source})")
    if not a.no_open:
        webbrowser.open("file://" + os.path.abspath(a.out))


if __name__ == "__main__":
    main()
