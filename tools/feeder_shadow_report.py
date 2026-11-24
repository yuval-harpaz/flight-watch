#!/usr/bin/env python3
"""What the private feeder would have added, from the monitor's shadow files (--feeder-shadow).

    python tools/feeder_shadow_report.py feeder_shadow_2026-11-2*.jsonl

Per class of traffic (the monitor's ARR / DEP / ARR? / DEP? / OVR / ?): aircraft whose reports were missing from
our answered local polls, heard during polls of ours that failed, fresher than ours, heard while our track was
silent >= 60 s or marked lost, and heard first (with the lead before our first report). Then the LOST_CONTACT /
DIVERSION alerts on aircraft the feeder heard after our last report. Stdlib only."""
import collections
import json
import sys


def main(paths):
    eps, alerts = [], []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                (eps if r.get("type") == "episode" else alerts).append(r)
    by = collections.defaultdict(list)
    for e in eps:
        by[e.get("traffic") or "unknown to us"].append(e)
    cols = ("missing", "failed", "fresher", "silent", "lost")
    print(f"{len(eps)} aircraft episodes the feeder added to")
    print(f"{'traffic':14} {'aircraft':>8} " + " ".join(f"{c:>8}" for c in cols) + f" {'first':>6} {'lead s (median)':>16}")
    for k in sorted(by, key=lambda k: -len(by[k])):
        es = by[k]
        leads = sorted(e["lead_s"] for e in es if (e.get("lead_s") or 0) > 0)
        print(f"{k:14} {len(es):8d} " + " ".join(f"{sum(1 for e in es if e[c]):8d}" for c in cols)
              + f" {len(leads):6d} {leads[len(leads) // 2] if leads else '-':>16}")
    print("\n(columns: aircraft with at least one such report)")
    low = [e for e in eps if e["missing"] and e.get("min_alt") is not None and e["min_alt"] < 3000]
    print(f"missing from our answer below 3,000 ft: {len(low)} aircraft")
    for e in sorted(eps, key=lambda e: -(e["silent"] + e["lost"]))[:15]:
        if e["silent"] or e["lost"]:
            print(f"  {e['first']} {e.get('callsign') or e['hex']:9} {e.get('traffic') or '':5} {e.get('route') or '':16} "
                  f"heard while we did not: {e['silent']} reads (lost: {e['lost']}), min alt {e.get('min_alt')}")
    print(f"\nalerts of silence on aircraft the feeder still heard: {len(alerts)}")
    for a in alerts:
        print(f"  {a['alert']} {a['kind']:13} {a.get('callsign') or a['hex']:9} ours until {a['our_last']}, "
              f"the feeder's until {a['feeder_last']}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
