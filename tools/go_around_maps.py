#!/usr/bin/env python3
"""Draw an aborted landing on the incident map (tools/plot_alert.py --map), from a trace kept by tools/find_go_arounds.py.

Its fixture goes to goarounds/ (git-ignored), the page to docs/ga_<name>_map.html. It is drawn as a fixture's meta
`event`, not by replaying the monitor.

  python tools/go_around_maps.py goarounds/traces/2026-10-08_8963a2.json --name uae9648 --time 2026-10-08T07:27:15 \\
      --title "..." --note "..." --route UAE9648=HKJK-EHAM
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import capture_incident as ci  # noqa: E402
import find_go_arounds as fga  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("trace")
    p.add_argument("--name", required=True, help="short name: docs/ga_<name>_map.html")
    p.add_argument("--time", action="append", required=True, help="UTC time of an aborted landing (repeatable)")
    p.add_argument("--title", required=True)
    p.add_argument("--note", default="aborted landing", help="the text of its marker")
    p.add_argument("--route", default="", metavar="CALLSIGN=ICAO-ICAO")
    p.add_argument("--route-text", default="", help="the page's heading, e.g. DXB -> TLV")
    p.add_argument("--standing-data", default="standing-data")
    a = p.parse_args()
    with open(a.trace) as f:
        tr = json.load(f)
    hexid, rows = tr["icao"].lower(), fga.rows_of(tr)
    times = [datetime.fromisoformat(x).replace(tzinfo=timezone.utc).timestamp() for x in a.time]
    events = []
    for t in times:
        r = min(rows, key=lambda x: abs(x[0] - t))
        events.append({"time": datetime.fromtimestamp(r[0], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "kind": "ABORTED_LANDING",
                       "message": a.note, "lat": r[1], "lon": r[2], "alt": r[3] if r[3] is not None else 0,
                       "callsign": r[5], "reg": tr.get("r"), "type": tr.get("t"), "route_text": a.route_text})
    ap = fga.nearest(fga.airports(a.standing_data), events[0]["lat"], events[0]["lon"], 15)
    day = tr["trace"][0][0] + tr["timestamp"]
    start, end = rows[0][0], rows[-1][0]
    pts = ci.trace_points(tr, start, end, 0)
    meta = {"name": a.title, "airport": "XXX", "airport_coords": [ap[1][1], ap[1][2]] if ap else [events[0]["lat"], events[0]["lon"]],
            "date": datetime.fromtimestamp(day, timezone.utc).strftime("%Y-%m-%d"), "start": start, "end": end, "radius": 250,
            "focus": [hexid], "step": 0,
            "source": "adsb.lol daily archive (github.com/adsblol/globe_history, ODbL)",
            "point_fields": ["t", "lat", "lon", "alt", "gs", "track", "vrate", "flight", "squawk", "emergency", "category"],
            "route_overrides": dict([a.route.split("=")]) if a.route else {}, "events": events}
    fixture = {"meta": meta, "aircraft": {hexid: {"r": tr.get("r"), "t": tr.get("t"), "points": pts}}}
    os.makedirs("goarounds", exist_ok=True)
    path = os.path.join("goarounds", f"{a.name}.json.gz")
    with gzip.open(path, "wt") as f:
        json.dump(fixture, f)
    out = os.path.join("docs", f"ga_{a.name}_map.html")
    subprocess.run([sys.executable, os.path.join(HERE, "plot_alert.py"), "--fixture", path, "--map", "--no-open", "--event",
                    "--out", out], check=True)


if __name__ == "__main__":
    main()
