"""Offline simulation tests: a fake feed replaces the network, a fake clock replaces time.

Run with:  python -m unittest discover -s tests
"""
import os
import sys
import unittest
from datetime import datetime
from urllib.parse import urlparse

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import flight_watch as fw  # noqa: E402

TLV = (32.0114, 34.8867)


class Clock:
    def __init__(self, t=1_791_187_200.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeResponse:
    def __init__(self, status, payload=None, url=""):
        self.status_code, self.payload, self.url = status, payload, url
        self.ok = status < 400

    def json(self):
        if self.payload is None:
            raise ValueError("no JSON body")
        return self.payload

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code} for {self.url}", response=self)


class FakeFeed:
    """Answers readsb-style endpoints from a scripted list of aircraft; can return 429s."""

    def __init__(self, clock):
        self.clock = clock
        self.aircraft = {}        # hex -> dict (lat/lon/alt...); visible everywhere
        self.local = set()        # hexes inside the /point circle
        self.calls = []           # (method, path)
        self.limited = set()      # path prefixes answered with 429
        self.route_status = 201   # adsb.lol routeset currently answers 201 with no body
        self.headers = {}

    def ac(self, hexid):
        a = dict(self.aircraft[hexid], hex=hexid)
        a.setdefault("seen", 0)
        a.setdefault("seen_pos", 0)
        return a

    def request(self, method, url, timeout=None, **kw):
        u = urlparse(url)
        path = u.path
        self.calls.append((method, path))
        if any(path.startswith(p) for p in self.limited):
            return FakeResponse(429, url=url)
        now_ms = self.clock() * 1000
        if "/point/" in path:
            return FakeResponse(200, {"now": now_ms, "ac": [self.ac(h) for h in sorted(self.local)]})
        if "/hex/" in path:
            ids = path.rsplit("/", 1)[1].split(",")
            return FakeResponse(200, {"now": now_ms, "ac": [self.ac(h) for h in ids if h in self.aircraft]})
        if "/callsign/" in path:
            ids = set(path.rsplit("/", 1)[1].split(","))
            return FakeResponse(200, {"now": now_ms, "ac": [
                self.ac(h) for h, a in self.aircraft.items() if a.get("flight", "").strip() in ids]})
        if "routeset" in path:
            return FakeResponse(self.route_status, None, url=url)
        return FakeResponse(404, url=url)


def make(argv=(), with_schedule=False):
    clock = Clock()
    args = fw.parse_args(["--jsonl", "", "--no-schedule", *argv])
    http = fw.Http(min_gap=0, rate=args.rate, rate_max=args.rate_max, clock=clock, rng=lambda: 0.5)
    feed = FakeFeed(clock)
    http.s = feed
    schedule = None
    if with_schedule:
        schedule = fw.Schedule(http, dict(fw.AIRLINE_ICAO), 1e9, 16)
        schedule.fetched = 1e18  # never refetch; tests load records directly
    mon = fw.Monitor(args, fw.Notifier(args, http), http, schedule)
    mon.clock = clock
    mon.started = clock()
    alerts = []
    mon.notifier.send = alerts.append
    return mon, feed, clock, alerts


def step(mon, clock, seconds=10):
    """One main-loop cycle: poll, swallowing the errors main() would log."""
    try:
        mon.poll()
    except requests.RequestException:
        pass
    clock.t += seconds


def airborne(lat, lon, alt, track=90, gs=450, rate=0, flight="ELY315", **kw):
    return dict(lat=lat, lon=lon, alt_baro=alt, track=track, gs=gs, baro_rate=rate,
                flight=flight + "  ", r="4X-EDC", t="B789", **kw)


class AlertSimulation(unittest.TestCase):
    def test_vertical_rate_confirmed_by_history(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        feed.local = {"738001"}
        alt = 30000
        for _ in range(6):
            feed.aircraft["738001"] = airborne(32.5, 34.5, alt, rate=-6000)
            step(mon, clock)
            alt -= 1000
        kinds = [a["kind"] for a in alerts]
        self.assertIn("VERTICAL_RATE", kinds)
        self.assertEqual(kinds.count("VERTICAL_RATE"), 1, "cooldown should suppress repeats")

    def test_vertical_rate_glitch_ignored(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        feed.local = {"738001"}
        for i in range(8):  # level flight, then reported -6000 ft/min while altitude stays put
            feed.aircraft["738001"] = airborne(32.5, 34.5, 30000, rate=-6000 if i >= 4 else 0)
            step(mon, clock)
        self.assertNotIn("VERTICAL_RATE", [a["kind"] for a in alerts])

    def test_emergency_squawk(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        feed.local = {"738001"}
        feed.aircraft["738001"] = airborne(32.5, 34.5, 30000, squawk="7700")
        step(mon, clock)
        step(mon, clock)
        em = [a for a in alerts if a["kind"] == "EMERGENCY"]
        self.assertEqual(len(em), 1)
        self.assertIn("7700", em[0]["message"])

    def test_local_lost_contact(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        feed.local = {"738001", "738002"}
        feed.aircraft["738002"] = airborne(31.0, 34.0, 20000, flight="ELY999")  # keeps feed alive
        for i in range(4):
            feed.aircraft["738001"] = airborne(32.6, 34.0 + i * 0.03, 20000)
            step(mon, clock)
        feed.local.discard("738001")
        for _ in range(8):
            step(mon, clock)
        lost = [a for a in alerts if a["kind"] == "LOST_CONTACT"]
        self.assertEqual(len(lost), 1)
        self.assertEqual(lost[0]["hex"], "738001")

    def test_course_change(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        feed.local = {"738001"}
        for i, trk in enumerate([90] * 8 + [200] * 8):
            feed.aircraft["738001"] = airborne(33.0, 33.5 + i * 0.01, 30000, track=trk)
            step(mon, clock, 15)
        self.assertIn("COURSE_CHANGE", [a["kind"] for a in alerts])

    def test_position_jump(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        feed.local = {"738001"}
        for lon in (33.0, 33.02, 34.5):  # last hop ~75 nm in 10 s
            feed.aircraft["738001"] = airborne(33.0, lon, 30000)
            step(mon, clock)
        self.assertIn("POSITION_JUMP", [a["kind"] for a in alerts])


def board_rows():
    """Flight board rows: LY25 with DL7441 codeshare, an old departure, a future departure."""
    def row(op, num, d, sched, loc, status="DEPARTED", est=None):
        return {"CHOPER": op, "CHFLTN": num, "CHAORD": d, "CHSTOL": sched, "CHPTOL": est or sched,
                "CHLOC1": loc, "CHLOC1D": loc, "CHRMINE": status}
    return [
        row("LY", "025", "D", "2026-10-05T10:00:00", "EWR"),
        row("DL", "7441", "D", "2026-10-05T10:00:00", "EWR"),
        row("LY", "5183", "D", "2026-10-04T19:10:00", "TIA"),               # left 15 h ago
        row("LY", "001", "D", "2026-10-05T18:00:00", "JFK", "ON TIME"),     # still at the gate
        row("6H", "662", "A", "2026-10-05T14:00:00", "DXB", "ON TIME"),     # inbound
    ]


class ScheduleTests(unittest.TestCase):
    NOW = datetime(2026, 10, 5, 11, 0, tzinfo=fw.ZoneInfo("Asia/Jerusalem"))

    def load(self):
        mon, feed, clock, alerts = make(with_schedule=True)
        mon.schedule.load(board_rows(), self.NOW)
        return mon, feed, clock, alerts

    def test_codeshares_collapse_to_operating_flight(self):
        mon, *_ = self.load()
        sf = mon.schedule.by_callsign["DAL7441"]
        self.assertIs(sf, mon.schedule.by_callsign["ELY025"])
        self.assertEqual(sf.flight, "LY25")
        self.assertEqual(sf.codeshares, ("DL7441",))
        self.assertEqual(len(mon.schedule.flights), 4)

    def test_search_order_skips_old_and_future_departures(self):
        mon, *_ = self.load()
        order = mon.schedule.search_order(self.NOW.timestamp(), set(), 6)
        self.assertNotIn("ELY5183", order)     # landed long ago
        self.assertNotIn("ELY001", order)      # on the ground at TLV
        self.assertLess(order.index("ELY25"), order.index("DAL7441"))  # codeshare only as fallback
        self.assertIn("ISR662", order)

    def test_board_is_paged_until_total(self):
        mon, *_ = make(with_schedule=True)
        rows = board_rows()
        pages = []

        def get_json(url, priority=fw.HIGH, params=None):
            pages.append(params["offset"])
            chunk = rows[params["offset"]:params["offset"] + 2]
            return {"result": {"records": chunk, "total": len(rows)}}
        mon.schedule.http = type("H", (), {"get_json": staticmethod(get_json)})()
        self.assertEqual(len(mon.schedule.fetch_records()), len(rows))
        self.assertEqual(pages, [0, 2, 4])


class FollowAndDiscovery(unittest.TestCase):
    def test_light_aircraft_not_followed_on_guess(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        feed.local = {"738a00", "738b00"}
        for i in range(3):  # both climbing out of TLV
            feed.aircraft["738a00"] = dict(airborne(32.02, 34.9 + i * 0.01, 2000 + i * 500, rate=1500,
                                                    flight="4XHSC"), r="4X-HSC", t="EV97", category="A1")
            feed.aircraft["738b00"] = airborne(32.02, 34.88 + i * 0.01, 2000 + i * 500, rate=1500,
                                               flight="WZZ5TL")
            step(mon, clock)
        self.assertFalse(mon.tracks["738a00"].followed)
        self.assertTrue(mon.tracks["738b00"].followed)

    def test_follow_uses_one_combined_request_per_interval(self):
        mon, feed, clock, alerts = make(["--no-routes", "--rate", "60"])
        for h in ("738001", "738002", "738003"):
            feed.aircraft[h] = airborne(45.0, 10.0, 35000, flight="X" + h)
            t = mon.tracks.setdefault(h, fw.Track(h))
            t.followed, t.last_msg = True, clock()
        feed.local = {"999999"}
        feed.aircraft["999999"] = airborne(32.5, 34.5, 10000, flight="LOC1")
        for _ in range(6):  # 60 s
            step(mon, clock)
        hex_calls = [p for m, p in feed.calls if "/hex/" in p]
        point_calls = [p for m, p in feed.calls if "/point/" in p]
        self.assertEqual(len(point_calls), 6, "local poll runs every cycle")
        self.assertEqual(len(hex_calls), 2, "one combined follow request per 30 s")
        self.assertTrue(all(p.count(",") == 2 for p in hex_calls))

    def test_route_api_disabled_after_repeated_failures(self):
        mon, feed, clock, alerts = make(["--rate", "60"])
        feed.local = {"738001"}
        for i in range(6):
            feed.aircraft["738001"] = airborne(32.5, 34.5, 30000, flight=f"ABC{i}")
            step(mon, clock)
        self.assertEqual(sum("routeset" in p for m, p in feed.calls), 3)
        self.assertTrue(mon.routes_off)


class RateLimitTests(unittest.TestCase):
    def test_429_cools_down_with_jitter_and_halves_budget(self):
        mon, feed, clock, alerts = make(["--no-routes", "--rate", "20"])
        feed.local = {"738001"}
        feed.aircraft["738001"] = airborne(32.5, 34.5, 30000)
        step(mon, clock)
        host = "api.adsb.lol"
        b = mon.http.budget(host)
        rate_before = b.rate

        feed.limited = {"/v2/point"}
        step(mon, clock, 0)  # this cycle gets HTTP 429
        self.assertAlmostEqual(b.rate, rate_before / 2)
        self.assertAlmostEqual(b.cooling(), 10.0)  # 10 s base * jitter (0.5 + 0.5)

        feed.limited = set()
        n = len(feed.calls)
        clock.t += 5
        with self.assertRaises(fw.Throttled):  # still cooling: no request goes out at all
            mon.poll()
        self.assertEqual(len(feed.calls), n)

        clock.t += 6  # cooldown over: local poll resumes at the normal cadence
        mon.poll()
        self.assertEqual(feed.calls[-1][1].split("/")[2], "point")

    def test_repeated_429_backs_off_exponentially(self):
        clock = Clock()
        jit = iter([0.0, 1.0, 0.5])
        b = fw.Budget(8, 60, clock=clock, rng=lambda: next(jit))
        self.assertAlmostEqual(b.limited(), 5.0)    # 10 s * 0.5
        self.assertAlmostEqual(b.limited(), 30.0)   # 20 s * 1.5
        self.assertAlmostEqual(b.limited(), 40.0)   # 40 s * 1.0
        self.assertEqual(b.rate, 3)                 # 8 -> 4 -> 3 (floor)
        b.ok()
        self.assertEqual(b.strikes, 0)

    def test_budget_grows_without_429s(self):
        clock = Clock()
        b = fw.Budget(8, 12, clock=clock)
        for _ in range(20):
            b.ok()
        self.assertEqual(b.rate, 12)

    def test_429_on_remote_layer_stops_it_and_keeps_local_poll(self):
        mon, feed, clock, alerts = make(["--no-routes", "--rate", "60"])
        feed.local = {"999999", "999998"}
        feed.aircraft["999999"] = airborne(32.5, 34.5, 10000, flight="LOC1")
        feed.aircraft["999998"] = airborne(32.4, 34.4, 10000, flight="LOC2")
        for h in ("738001", "738002"):
            feed.aircraft[h] = airborne(45.0, 10.0, 35000, flight="X" + h)
            t = mon.tracks.setdefault(h, fw.Track(h))
            t.followed, t.last_msg = True, clock()
            for k in range(3):
                t.samples.append(fw.Sample(clock() - 30 + k * 10, 45.0, 10.0, 35000, 90, 450, 0, False))
        mon.multi[("adsb.lol", "hex")] = True
        feed.limited = {"/v2/hex"}
        step(mon, clock)
        self.assertEqual(sum("/hex/" in p for m, p in feed.calls), 1, "no retries after a 429")
        # The 429 cooldown applies to the whole host: the next cycle sends nothing, then local
        # polling resumes on its normal cadence.
        feed.limited = set()
        for _ in range(4):
            step(mon, clock)
        self.assertGreaterEqual(sum("/point/" in p for m, p in feed.calls), 3)
        # Remote silence while follow was throttled never counts as lost contact.
        self.assertNotIn("LOST_CONTACT", [a["kind"] for a in alerts])

    def test_probe_429_is_not_cached_as_unsupported(self):
        mon, feed, clock, alerts = make(["--no-routes", "--rate", "60"])
        feed.local = {"999999", "999998"}
        feed.aircraft["999999"] = airborne(32.5, 34.5, 10000, flight="LOC1")
        feed.aircraft["999998"] = airborne(32.4, 34.4, 10000, flight="LOC2")
        feed.limited = {"/v2/hex"}
        self.assertIsNone(mon.multi_supported("adsb.lol", "hex", [feed.ac("999999"), feed.ac("999998")]))
        self.assertNotIn(("adsb.lol", "hex"), mon.multi)
        feed.limited = set()
        clock.t += 60
        self.assertTrue(mon.multi_supported("adsb.lol", "hex", [feed.ac("999999"), feed.ac("999998")]))

    def test_refusing_extra_provider_is_dropped(self):
        mon, feed, clock, alerts = make(["--no-routes", "--rate", "60",
                                         "--remote-providers", "adsb.lol,adsb.fi"])
        orig = feed.request

        def request(method, url, **kw):
            if "adsb.fi" in url:
                feed.calls.append((method, url))
                return FakeResponse(403, url=url)
            return orig(method, url, **kw)
        feed.request = request
        feed.local = {"999999", "999998"}
        feed.aircraft["999999"] = airborne(32.5, 34.5, 10000, flight="LOC1")
        feed.aircraft["999998"] = airborne(32.4, 34.4, 10000, flight="LOC2")
        t = mon.tracks.setdefault("738001", fw.Track("738001"))
        t.followed, t.last_msg = True, clock()
        feed.aircraft["738001"] = airborne(45.0, 10.0, 35000, flight="X1")
        for _ in range(7):
            step(mon, clock)
        self.assertIn("adsb.fi", mon.disabled)
        self.assertEqual(sum("adsb.fi" in p for m, p in feed.calls), 1)
        self.assertTrue(any("/hex/" in p for m, p in feed.calls if "adsb.fi" not in p))


if __name__ == "__main__":
    unittest.main()
