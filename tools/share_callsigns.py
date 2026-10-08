#!/usr/bin/env python3
"""Fill the map page's callsign map once: aircraft near TLV now -> the relay's /learn.

The flight board does not name the aircraft, and once a flight has landed the live feed no longer
finds it by callsign; docs/flight_map.html draws a landed flight's track from the callsign -> hex
pairs the relay (tools/cors_worker.js) keeps for 36 h. The worker cannot ask adsb.lol itself
(429 to Cloudflare's addresses), so this runs elsewhere: every 10 min in GitHub Actions
(.github/workflows/share_callsigns.yml), or by hand. One adsb.lol request per run.

  RELAY_TOKEN=... python tools/share_callsigns.py      # same value as the worker's LEARN_TOKEN

Standard library only, so the workflow needs no installs. Exit code 1 when the relay refuses or
is unreachable (a red run tells the owner); adsb.lol being busy is only a warning.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

HOME = (32.0114, 34.8867)  # TLV, as in flight_watch.AIRPORTS
FEED = "https://api.adsb.lol/v2/point/{:.4f}/{:.4f}/250"
LEARN = os.getenv("RELAY_LEARN_URL", "https://flight-watch-relay.yuvharpaz.workers.dev/learn")
UA = {"User-Agent": "flight-watch-share/0.1 (+https://github.com/yuval-harpaz/flight-watch)"}


def pairs(feed: dict) -> dict:
    """readsb answer -> {callsign: [hex, epoch heard]} (aircraft without a callsign are skipped)."""
    now = feed.get("now", time.time() * 1000) / 1000
    out = {}
    for ac in feed.get("ac") or []:
        cs = str(ac.get("flight") or "").strip().upper()
        if cs and ac.get("hex"):
            out[cs] = [ac["hex"], round(now - (ac.get("seen") or 0))]
    return out


def get_feed(opener=urllib.request.urlopen, sleep=time.sleep):
    """The aircraft within 250 nm of TLV; None when adsb.lol stays busy (429) after one retry."""
    req = urllib.request.Request(FEED.format(*HOME), headers=UA)
    for attempt in range(2):
        try:
            with opener(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt:
                raise
            sleep(30)
    return None


def main(opener=urllib.request.urlopen, sleep=time.sleep) -> int:
    token = os.getenv("RELAY_TOKEN")
    if not token:
        print("RELAY_TOKEN is not set (the worker's LEARN_TOKEN value)", file=sys.stderr)
        return 1
    try:
        feed = get_feed(opener, sleep)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"::warning::adsb.lol: {e}")
        return 0
    if feed is None:
        print("::warning::adsb.lol answered 429 twice - nothing sent this run")
        return 0
    found = pairs(feed)
    req = urllib.request.Request(LEARN, data=json.dumps(found).encode(), method="POST",
                                 headers={**UA, "Content-Type": "application/json",
                                          "Authorization": f"Bearer {token}"})
    try:
        with opener(req, timeout=30) as r:
            answer = r.read().decode()[:200]
    except urllib.error.HTTPError as e:
        print(f"relay refused: HTTP {e.code} {e.read().decode()[:200]}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"relay unreachable: {e}", file=sys.stderr)
        return 1
    print(f"{len(feed.get('ac') or [])} aircraft near TLV, {len(found)} callsigns sent: {answer}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
