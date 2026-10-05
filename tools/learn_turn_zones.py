#!/usr/bin/env python3
"""Learn where airliners routinely turn (route corners, detours around closed airspace).

Reads ADS-B Exchange globe-history heatmaps (every aircraft, one position per 10 s; downloaded
and cached by tools/capture_incident.py, or fetched here), finds turns of >= --min-turn degrees
above --min-alt, and keeps grid cells where at least --min-aircraft different aircraft turned
onto a similar heading. flight_watch treats a course change ending on such a heading in such a
cell as expected (it still alerts on sharp turns there).

The output stores statistics only - cell, out-heading, count - no tracks:
  {"cell": 0.5, "zones": [{"lat": 31.25, "lon": 37.25, "out": 300, "n": 14}, ...]}

  python tools/learn_turn_zones.py --date 2026-09-30 --hours 4-15 --out turn_zones.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import capture_incident as cap  # noqa: E402

BOX = (24.0, 38.0, 26.0, 46.0)  # lat/lon box: Eastern Mediterranean, Levant, Sinai, N. Saudi


def bearing(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    return math.degrees(math.atan2(math.sin(dl) * math.cos(p2),
                                   math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) % 360


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--date", action="append", required=True, help="UTC date(s), YYYY-MM-DD")
    p.add_argument("--hours", default="0-24", help="UTC hour range per date, e.g. 4-15")
    p.add_argument("--cell", type=float, default=0.5, help="grid cell size, degrees")
    p.add_argument("--min-turn", type=float, default=30)
    p.add_argument("--min-alt", type=float, default=8000)
    p.add_argument("--min-aircraft", type=int, default=4)
    p.add_argument("--exclude", action="append", default=[], help="hex id(s) to leave out (the incident)")
    p.add_argument("--cache", default=os.path.join(os.path.expanduser("~"), ".cache", "flight-watch"))
    p.add_argument("--out", required=True)
    a = p.parse_args()
    h0, h1 = (int(x) for x in a.hours.split("-"))

    # cell -> 10-degree out-heading bucket -> set of aircraft
    turns: dict[tuple, dict[int, set]] = defaultdict(lambda: defaultdict(set))
    for date in a.date:
        day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        for n in range(h0 * 2, min(48, h1 * 2)):
            tracks: dict[str, list] = defaultdict(list)
            for ts, h, lat, lon, alt in cap.heatmap(day, n, a.cache):
                if (BOX[0] <= lat <= BOX[1] and BOX[2] <= lon <= BOX[3] and alt != cap.GROUND
                        and h not in a.exclude):
                    tracks[h].append((ts, lat, lon, alt * 25))
            for h, pts in tracks.items():
                pts.sort()
                # heading over 60 s, compared with the heading 120 s earlier
                for i in range(len(pts)):
                    t, lat, lon, alt = pts[i]
                    if alt < a.min_alt:
                        continue
                    j = next((k for k in range(i, -1, -1) if t - pts[k][0] >= 60), None)
                    if j is None:
                        continue
                    k = next((m for m in range(j, -1, -1) if pts[j][0] - pts[m][0] >= 120), None)
                    if k is None or t - pts[k][0] > 300:
                        continue
                    l = next((m for m in range(k, -1, -1) if pts[k][0] - pts[m][0] >= 60), None)
                    if l is None:
                        continue
                    out = bearing(pts[j][1], pts[j][2], lat, lon)
                    inn = bearing(pts[l][1], pts[l][2], pts[k][1], pts[k][2])
                    if abs((out - inn + 540) % 360 - 180) >= a.min_turn:
                        mlat, mlon = pts[j][1], pts[j][2]
                        cell = (math.floor(mlat / a.cell), math.floor(mlon / a.cell))
                        turns[cell][int(out // 10) % 36].add(h)
            print(f"{date} heatmap {n:02d}: {len(turns)} cells with turns", file=sys.stderr)

    zones = []
    for (ci, cj), buckets in turns.items():
        for b in range(36):  # merge neighbouring 10-degree buckets (headings within +-15 deg)
            ac = buckets.get(b, set()) | buckets.get((b - 1) % 36, set()) | buckets.get((b + 1) % 36, set())
            if len(ac) >= a.min_aircraft and len(buckets.get(b, ())) >= max(
                    len(buckets.get((b - 1) % 36, ())), len(buckets.get((b + 1) % 36, ()))):
                zones.append({"lat": round((ci + 0.5) * a.cell, 3), "lon": round((cj + 0.5) * a.cell, 3),
                              "out": b * 10 + 5, "n": len(ac)})
    zones.sort(key=lambda z: -z["n"])
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"cell": a.cell, "dates": a.date, "min_turn": a.min_turn, "min_alt": a.min_alt,
                   "min_aircraft": a.min_aircraft, "source": "ADS-B Exchange globe-history heatmaps",
                   "zones": zones}, f, indent=0)
    print(f"wrote {a.out}: {len(zones)} zones", file=sys.stderr)


if __name__ == "__main__":
    main()
