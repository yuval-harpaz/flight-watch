#!/usr/bin/env python3
"""Find aborted landings in adsb.lol's daily archive (github.com/adsblol/globe_history_YYYY, ODbL), for analysis.

One pass over a day's tar (tools/archive_replay.Parts), every airliner's trace: a descent to <= --max-agl within --near nm
of an airport, then a climb of >= --min-climb without slowing on the ground. Kinds: "air" (never touched) and "touch" (on the
ground <= 90 s, never below 60 kt: a bounce or a touch-and-go). Rows go to <out>/go_arounds.jsonl. Kept in <out>/traces/: the aircraft
named by --reg / --callsign (a named incident, whatever the detector says) and go-arounds within --keep-nm of --lat / --lon.

  python tools/find_go_arounds.py --day 2026-10-08 --reg A6-EFO --out goarounds
"""
from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
import math
import os
import re
import sys
import tarfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
from archive_replay import RELEASE, Parts  # noqa: E402
from flight_watch import LARGE_TYPES, haversine_nm  # noqa: E402


def airports(root: str) -> dict:
    """1-degree cell -> [(icao, lat, lon, elevation ft)], from the standing data's checkout."""
    cells: dict = {}
    for path in glob.glob(os.path.join(root, "airports", "schema-01", "*", "*.csv")):
        with open(path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                try:
                    lat, lon = float(r["Latitude"]), float(r["Longitude"])
                except (TypeError, ValueError):
                    continue
                cells.setdefault((math.floor(lat), math.floor(lon)), []).append(
                    (r.get("ICAO") or r.get("Code"), lat, lon, float(r.get("AltitudeFeet") or 0)))
    return cells


def nearest(cells: dict, lat: float, lon: float, nm: float):
    best = None
    i, j = math.floor(lat), math.floor(lon)
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            for a in cells.get((i + di, j + dj), ()):
                if abs(a[1] - lat) < 0.2:
                    d = haversine_nm(lat, lon, a[1], a[2])
                    if d <= nm and (best is None or d < best[0]):
                        best = (d, a)
    return best


def rows_of(tr: dict) -> list:
    """[(t, lat, lon, alt or None when on the ground, gs, flight)] of a readsb trace."""
    out, flight = [], ""
    base = tr.get("timestamp") or 0
    for r in tr.get("trace") or []:
        det = r[8] if len(r) > 8 and isinstance(r[8], dict) else None
        flight = ((det or {}).get("flight") or flight).strip()
        alt = r[3]
        if alt != "ground" and not isinstance(alt, (int, float)):
            continue
        out.append((base + r[0], r[1], r[2], None if alt == "ground" else alt, r[4] or 0, flight))
    return out


def detect(rows: list, cells: dict, max_agl=300, near=3.0, min_climb=800) -> list:
    """Go-arounds in a trace's rows. Each low point: descending into it, near an airport, climbing out without stopping."""
    found, skip = [], 0
    for i, (t, lat, lon, alt, gs, flight) in enumerate(rows):
        if i < skip or alt is None or gs < 80 or alt > 15000:
            continue
        if rows[i - 1][3] is None if i else True:
            continue
        before = [r for r in rows[max(0, i - 60):i] if t - 150 <= r[0]]
        if not before or any(r[3] is None for r in before) or before[0][3] < alt + 500 or t - before[0][0] < 45:
            continue
        ap = nearest(cells, lat, lon, near)
        if not ap or not -600 <= alt - ap[1][3] <= max_agl:
            continue  # (far below the listed field: a level-off, CKS704 at 4,000 ft "at" LLJR)
        low, touch, climbed, ground_from = alt, False, None, None
        for j in range(i + 1, min(i + 200, len(rows))):
            r = rows[j]
            if r[0] - rows[j - 1][0] > 45 or r[0] - t > 300:
                break
            if r[3] is None:
                ground_from = ground_from or r[0]
                touch = True
                if r[4] < 60 or r[0] - ground_from > 90:
                    break
                continue
            if touch and r[3] < low:
                low = r[3]  # off the ground again: its climb counts from here
            elif r[3] < low:
                break  # still coming down: judged from its lower point
            if r[3] >= low + min_climb:
                climbed = j
                break
        if climbed is None:
            continue
        after = rows[climbed:climbed + 4]
        if len(after) < 3 or any(r[3] is None or r[3] < low + min_climb - 200 for r in after):
            continue  # one bad altitude is no climb
        found.append({"t": t, "lat": lat, "lon": lon, "airport": ap[1][0], "dist_nm": round(ap[0], 1),
                      "low_agl": round(low - ap[1][3]), "kind": "touch" if touch else "air", "gs": round(gs), "flight": flight})
        skip = climbed + 30
    return found


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--day", required=True)
    p.add_argument("--reg", action="append", default=[])
    p.add_argument("--callsign", action="append", default=[])
    p.add_argument("--standing-data", default="standing-data")
    p.add_argument("--lat", type=float, default=32.0114)
    p.add_argument("--lon", type=float, default=34.8867)
    p.add_argument("--keep-nm", type=float, default=200)
    p.add_argument("--out", default="goarounds")
    a = p.parse_args()
    cells = airports(a.standing_data)
    os.makedirs(os.path.join(a.out, "traces"), exist_ok=True)
    regs, calls = {r.upper() for r in a.reg}, {c.upper() for c in a.callsign}
    t0, seen, large, n = time.time(), 0, 0, 0
    # (a day of 2023 is one .tar, not parts)
    source = Parts(a.day) if a.day >= "2024" else urllib.request.urlopen(
        RELEASE.format(y=a.day[:4], d=a.day.replace("-", "."), part="").removesuffix("."), timeout=120)
    with tarfile.open(fileobj=source, mode="r|") as tf, open(os.path.join(a.out, "go_arounds.jsonl"), "a") as out:
        for m in tf:
            if not m.isfile() or "trace_full_" not in m.name:
                continue
            raw = tf.extractfile(m).read()
            seen += 1
            try:
                tr = json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
            except ValueError:
                continue
            hexid = (tr.get("icao") or "").lower()
            typ, reg = tr.get("t") or "", (tr.get("r") or "").upper()
            named = reg in regs or (calls and re.search(
                r'"flight":\s*"(' + "|".join(map(re.escape, calls)) + r')\s*"', json.dumps(tr.get("trace") or [])))
            if not (named or LARGE_TYPES.fullmatch(typ)):
                continue
            large += 1
            hits = detect(rows_of(tr), cells)
            for h in hits:
                out.write(json.dumps({"day": a.day, "hex": hexid, "reg": reg, "type": typ, **h}) + "\n")
                n += 1
            if named or any(haversine_nm(a.lat, a.lon, h["lat"], h["lon"]) <= a.keep_nm for h in hits):
                with open(os.path.join(a.out, "traces", f"{a.day}_{hexid}.json"), "w") as f:
                    json.dump(tr, f)
                print(f"  kept {hexid} {reg} {typ}{' (named)' if named else ''}: {len(hits)} go-around(s)", flush=True)
            out.flush()
    print(f"{a.day}: {seen} traces, {large} airliners, {n} go-arounds, {time.time() - t0:.0f} s", flush=True)


if __name__ == "__main__":
    main()
