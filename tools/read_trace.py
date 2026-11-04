#!/usr/bin/env python3
"""One aircraft's track from adsb.lol's trace file (the last ~24 h), as text: for judging an alert from its track.

  python tools/read_trace.py 896678 --around 2026-11-03T13:02 --minutes 20    # UTC; without --around: the whole file

Reads adsb.lol's trace files and nothing else, one request, and keeps nothing. adsb.lol's coverage is thinner than what the
monitor received (AGENTS.md); a 401 / 403 (its browser check, 18 Oct - 2 Nov 2026) is reported, not to be worked around.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import re
import sys
import urllib.error
import urllib.request

URL = "https://adsb.lol/data/traces/{xx}/trace_full_{hex}.json"


def trace_url(icao: str) -> str:
    icao = icao.strip().lower()
    if not re.fullmatch(r"~?[0-9a-f]{6}", icao):
        raise ValueError(f"not an ICAO hex: {icao!r}")
    return URL.format(xx=icao[-2:], hex=icao)


def rows(trace: dict, around: float | None = None, minutes: float = 15) -> list[str]:
    """Time, position, altitude, speed, track, rate; callsign and squawk where the feed gives them."""
    out = []
    for r in trace.get("trace") or []:
        t = trace["timestamp"] + r[0]
        if around is not None and abs(t - around) > minutes * 60:
            continue
        info = r[8] if len(r) > 8 and isinstance(r[8], dict) else {}
        extra = " ".join(str(info[k]).strip() for k in ("flight", "squawk") if info.get(k))
        out.append(" ".join(str(x) for x in (dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%d %H:%M:%S"),
                                             r[1], r[2], r[3], r[4], r[5], r[7], extra)).rstrip())
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("hex")
    p.add_argument("--around", help="UTC time, YYYY-MM-DDTHH:MM[:SS]")
    p.add_argument("--minutes", type=float, default=15, help="each side of --around (default 15)")
    a = p.parse_args(argv)
    around = None
    if a.around:
        around = dt.datetime.fromisoformat(a.around).replace(tzinfo=dt.timezone.utc).timestamp()
    req = urllib.request.Request(trace_url(a.hex), headers={"User-Agent": "flight-watch/0.3", "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
    except (urllib.error.URLError, OSError) as e:
        print(f"no trace: {e}")
        return 1
    tr = json.loads(gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body)
    print(f"{tr.get('icao')} {tr.get('r', '')} {tr.get('t', '')}; UTC lat lon alt_ft gs_kt track rate_ft_min [callsign squawk]")
    print("\n".join(rows(tr, around, a.minutes)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
