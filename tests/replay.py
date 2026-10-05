#!/usr/bin/env python3
"""Replay a captured incident (tools/capture_incident.py) through the real Monitor.

The fixture's traces are served as readsb-style /point, /hex and /callsign answers at a
simulated clock, and the Monitor polls them every --interval exactly as it would live. Any
flight_watch option can be passed through to try other thresholds:

  python tests/replay.py                                   # default fixture, timeline of alerts
  python tests/replay.py tests/data/fz1073_2026-09-30.json.gz --turn 60 --vrate 3000
  python tests/replay.py --rate 4                          # with a cloud-IP-sized request budget
"""
from __future__ import annotations

import bisect
import gzip
import json
import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime
from urllib.parse import urlparse

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import flight_watch as fw  # noqa: E402

HERE = os.path.dirname(__file__)
DEFAULT_FIXTURE = os.path.join(HERE, "data", "fz1073_2026-09-30.json.gz")
STALE = 60  # readsb drops an aircraft from its answers after 60 s without messages


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


class Response:
    def __init__(self, status: int, payload=None, url: str = ""):
        self.status_code, self.payload, self.url = status, payload, url
        self.ok = status < 400

    def json(self):
        if self.payload is None:
            raise ValueError("no JSON body")
        return self.payload

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code} for {self.url}", response=self)


class ReplayFeed:
    """Stands in for requests.Session: answers feed URLs from the fixture at clock()."""

    def __init__(self, fixture: dict, clock: Clock):
        self.clock = clock
        self.calls: list[str] = []
        self.planes = {}
        for hexid, a in fixture["aircraft"].items():
            times, states, cur = [], [], {}
            for t, lat, lon, alt, gs, trk, vr, flight, squawk, emergency, category in a["points"]:
                for k, v in (("flight", flight), ("squawk", squawk), ("emergency", emergency),
                             ("category", category)):
                    if v is not None:
                        cur[k] = v
                times.append(t)
                states.append((t, lat, lon, alt, gs, trk, vr, dict(cur)))
            self.planes[hexid] = (a, times, states)

    def state(self, hexid: str, now: float) -> dict | None:
        a, times, states = self.planes[hexid]
        i = bisect.bisect_right(times, now) - 1
        if i < 0 or now - times[i] > STALE:
            return None
        t, lat, lon, alt, gs, trk, vr, ex = states[i]
        ac = {"hex": hexid, "lat": lat, "lon": lon, "seen": round(now - t, 1),
              "seen_pos": round(now - t, 1), "r": a.get("r"), "t": a.get("t")}
        if alt is not None:
            ac["alt_baro"] = alt
        for k, v in (("gs", gs), ("track", trk), ("baro_rate", vr)):
            if v is not None:
                ac[k] = v
        if "flight" in ex:
            ac["flight"] = ex["flight"].ljust(8)
        for k in ("squawk", "emergency", "category"):
            if k in ex:
                ac[k] = ex[k]
        return ac

    def request(self, method, url, timeout=None, **kw):
        path = urlparse(url).path
        self.calls.append(path)
        now = self.clock()
        payload = {"now": now * 1000}
        if "/point/" in path:
            lat, lon, radius = (float(x) for x in path.rstrip("/").split("/")[-3:])
            acs = [s for h in self.planes if (s := self.state(h, now))
                   and fw.haversine_nm(lat, lon, s["lat"], s["lon"]) <= radius]
        elif "/hex/" in path:
            ids = path.rsplit("/", 1)[1].split(",")
            acs = [s for h in ids if h in self.planes and (s := self.state(h, now))]
        elif "/callsign/" in path:
            ids = set(path.rsplit("/", 1)[1].split(","))
            acs = [s for h in self.planes if (s := self.state(h, now))
                   and s.get("flight", "").strip() in ids]
        else:
            return Response(404, url=url)
        payload["ac"] = acs
        return Response(200, payload, url)


def load(path: str = DEFAULT_FIXTURE) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def run(fixture: dict, argv=(), warmup: float = 300) -> tuple[list[dict], fw.Monitor, ReplayFeed]:
    """Replay the fixture window; returns (alerts, monitor, feed)."""
    meta = fixture["meta"]
    args = fw.parse_args(["--airport", meta["airport"], "--radius", str(meta["radius"]),
                          "--jsonl", "", "--rate", "60", "--rate-max", "600",
                          *argv])
    clock = Clock(meta["start"] - warmup)
    http = fw.Http(min_gap=0, rate=args.rate, rate_max=args.rate_max, clock=clock, rng=lambda: 0.5)
    feed = ReplayFeed(fixture, clock)
    http.s = feed
    schedule = None
    if not args.no_schedule and args.airport == "TLV":
        schedule = fw.Schedule(http, dict(fw.AIRLINE_ICAO), args.schedule_refresh, args.schedule_window)
        schedule.fetched = float("inf")  # never fetch: the day's board comes from the fixture
        schedule.load(fixture["board"], datetime.fromtimestamp(clock(), schedule.tz))
    mon = fw.Monitor(args, fw.Notifier(args, http), http, schedule)
    if mon.standing:  # routes captured with the fixture: classify offline, like the live route lookup
        mon.standing = fw.StandingData(None, preload={"routes": fixture.get("routes", {}),
                                                      "airports": fixture.get("airports", {})})
    mon.clock = mon.wall = clock
    mon.started = clock()
    alerts: list[dict] = []
    mon.notifier.send = alerts.append
    while clock() <= meta["end"]:
        try:
            mon.poll()
        except requests.RequestException:
            pass  # main() logs and carries on; so does the replay
        clock.t += args.interval
    return alerts, mon, feed


def timeline(alerts: list[dict], focus=()) -> str:
    lines = []
    for r in alerts:
        mark = "*" if r["hex"] in focus else " "
        where = "REMOTE" if r["remote"] else "local "
        lines.append(f"{mark} {r['time'][11:19]}  {r['kind']:<16} {where} {r['aircraft'][:34]:<34} "
                     f"{r['traffic']:<4} {r['dist_nm']:>5.0f}nm  {r['message']}")
    return "\n".join(lines)


def main() -> None:
    argv = sys.argv[1:]
    path = DEFAULT_FIXTURE
    if argv and argv[0].endswith(".json.gz"):
        path, argv = argv[0], argv[1:]
    logging.basicConfig(level=logging.DEBUG if "-v" in argv else logging.ERROR,
                        format="%(levelname)-7s %(message)s")
    fixture = load(path)
    meta = fixture["meta"]
    started = time.monotonic()
    alerts, mon, feed = run(fixture, argv)
    print(f"{meta['name']} - {meta['date']} "
          f"{time.strftime('%H:%M', time.gmtime(meta['start']))}-"
          f"{time.strftime('%H:%M', time.gmtime(meta['end']))} UTC, "
          f"{len(fixture['aircraft'])} aircraft ({time.monotonic() - started:.1f}s)\n")
    print(timeline(alerts, set(meta["focus"])))
    counts = Counter(r["kind"] for r in alerts)
    print(f"\n{len(alerts)} alerts: " + ", ".join(f"{k} {v}" for k, v in counts.most_common()))
    print(f"requests: {len(feed.calls)} ({Counter(p.split('/')[2] for p in feed.calls)})"
          f" | followed at end: {sum(t.followed for t in mon.tracks.values())}")


if __name__ == "__main__":
    main()
