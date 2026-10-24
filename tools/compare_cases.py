#!/usr/bin/env python3
"""Compare two squawk replays (tools/squawk_cases.py's cases.jsonl): the cases whose alert kinds changed.

  python tools/compare_cases.py squawks/cases.jsonl /tmp/x/cases.jsonl   # exits 1 when any changed
"""
import json
import sys


def load(path):
    with open(path, encoding="utf-8") as f:
        return {r["uri"]: r for r in map(json.loads, f)}


def kinds(r):
    return sorted({x["kind"] for x in r.get("alerts") or []})


a, b = load(sys.argv[1]), load(sys.argv[2])
diff = 0
for u, r in a.items():
    if u in b and kinds(r) != kinds(b[u]):
        diff += 1
        print(f'{r["post_t"][:16]} {r["iata"]}{r["number"]} {r["from"]}->{r["to"]}: '
              f'-{sorted(set(kinds(r)) - set(kinds(b[u])))} +{sorted(set(kinds(b[u])) - set(kinds(r)))}')
print(diff, "changed of", len(a))
sys.exit(1 if diff else 0)
