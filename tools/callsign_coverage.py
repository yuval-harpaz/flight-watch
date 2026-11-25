#!/usr/bin/env python3
"""How many of the board's arrivals the monitor found on their callsign, per day, from its logs.

The monitor logs one line per board arrival 25 min after it landed (Monitor.learn_at_home):
  board arrival EY599 from AUH, landed 25 Nov 06:36: found as ETD3QF (learned callsign)
  board arrival W62327 from BUD, landed 24 Nov 18:14: not found on its callsign; ...
"Found" means the aircraft carried the board row when it landed: a callsign the board's number gives, or one learned
(at our airport, or from its rotation), so discovery could search for it far away. Before learning (24 Nov 2026): about
1 arrival in 5 was not found.

  python tools/callsign_coverage.py [flight_watch_2026-11-2*.log ...]   (default: all flight_watch_*.log here)
"""
import collections
import glob
import re
import sys

LINE = re.compile(r"^(\d{4}-\d\d-\d\d) .*board arrival (\S+) from (\S+), landed (\d\d \w+ \d\d:\d\d): "
                  r"(found as \S+ \((board|learned) callsign\)|not found)")


def main(argv=None):
    files = (argv if argv is not None else sys.argv[1:]) or sorted(glob.glob("flight_watch_*.log"))
    days = collections.defaultdict(collections.Counter)
    missed = collections.defaultdict(collections.Counter)
    seen = set()
    for name in files:
        with open(name, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = LINE.match(line)
                if not m:
                    continue
                key = (m.group(2), m.group(4))
                if key in seen:  # (a log line repeated across a restart)
                    continue
                seen.add(key)
                day = m.group(1)
                kind = m.group(6) or "not found"
                days[day][kind] += 1
                if kind == "not found":
                    missed[day][m.group(2)[:2]] += 1
    if not days:
        print("no 'board arrival' lines in", ", ".join(files) or "(no files)")
        return
    print(f"{'day':10}  {'arrivals':>8}  {'board cs':>8}  {'learned':>7}  {'not found':>9}  share not found  airlines not found")
    total = collections.Counter()
    for day in sorted(days):
        c = days[day]
        n = sum(c.values())
        total += c
        print(f"{day:10}  {n:8}  {c['board']:8}  {c['learned']:7}  {c['not found']:9}  {c['not found'] / n:15.0%}  "
              + ", ".join(f"{a} {k}" for a, k in missed[day].most_common(6)))
    n = sum(total.values())
    print(f"{'all':10}  {n:8}  {total['board']:8}  {total['learned']:7}  {total['not found']:9}  "
          f"{total['not found'] / n:15.0%}")


if __name__ == "__main__":
    main()
