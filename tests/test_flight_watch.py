"""Offline simulation tests: a fake feed replaces the network, a fake clock replaces time.

Run with:  python -m unittest discover -s tests
"""
import json
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

    @property
    def text(self):
        return self.payload if isinstance(self.payload, str) else ""

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
        self.route_status = 200   # standing-data host: 200 serves self.files, else this status
        self.files = {}           # standing-data path ("routes/schema-01/W/WZZ-3.csv") -> CSV text
        self.headers = {}
        self.learned = []         # (json body, Authorization header) of POSTs to the relay's /learn

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
        if path == "/learn" and method == "POST":
            self.learned.append((kw.get("json"), (kw.get("headers") or {}).get("Authorization")))
            return FakeResponse(200, {"stored": len(kw.get("json") or {})})
        if "/standing-data/" in path:
            rel = path.split("/standing-data/main/", 1)[1]
            if self.route_status != 200:
                return FakeResponse(self.route_status, url=url)
            return FakeResponse(200, self.files[rel], url=url) if rel in self.files else FakeResponse(404, url=url)
        return FakeResponse(404, url=url)


def make(argv=(), with_schedule=False):
    clock = Clock()
    args = fw.parse_args(["--jsonl", "", "--no-schedule", "--turn-zones", "",
                          "--standing-data", fw.STANDING_DATA, *argv])  # the fake feed, never a local copy
    http = fw.Http(min_gap=0, rate=args.rate, rate_max=args.rate_max, clock=clock, rng=lambda: 0.5)
    feed = FakeFeed(clock)
    http.s = feed
    schedule = None
    if with_schedule:
        schedule = fw.Schedule(http, dict(fw.AIRLINE_ICAO), 1e9, 16)
        schedule.fetched = 1e18  # never refetch; tests load records directly
    mon = fw.Monitor(args, fw.Notifier(args, http, console=lambda text: None), http, schedule)
    mon.clock = mon.wall = clock
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
        for _ in range(6):  # -12000 ft/min at 450 kt: ~15 deg, far steeper than any normal descent
            feed.aircraft["738001"] = airborne(32.5, 34.5, alt, rate=-12000)
            step(mon, clock)
            alt -= 2000
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
        for _ in range(14):  # 60 s silent, then --lost-confirm 60 s for others to go silent too
            step(mon, clock)
        lost = [a for a in alerts if a["kind"] == "LOST_CONTACT"]
        self.assertEqual(len(lost), 1)
        self.assertEqual(lost[0]["hex"], "738001")

    def test_heard_again_within_lost_confirm_is_not_lost(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        feed.local = {"738001", "738002"}
        feed.aircraft["738002"] = airborne(31.0, 34.0, 20000, flight="ELY999")
        for i in range(4):
            feed.aircraft["738001"] = airborne(32.6, 34.0 + i * 0.03, 20000)
            step(mon, clock)
        feed.local.discard("738001")
        for _ in range(9):  # 90 s: past --lost-after, within --lost-confirm
            step(mon, clock)
        feed.aircraft["738001"] = airborne(32.6, 34.25, 20000)
        feed.local.add("738001")
        for _ in range(10):
            step(mon, clock)
        self.assertEqual(kinds(alerts), [])

    def test_group_silence_is_one_alert(self):
        """Three aircraft silent together (reception, jamming, spoofing onset): one MASS_SILENCE,
        no LOST_CONTACT each, and no CONTACT_RESTORED when they come back."""
        mon, feed, clock, alerts = make(["--no-routes"])
        hexes = ["738001", "738003", "738004"]
        feed.aircraft["738002"] = airborne(31.0, 34.0, 20000, flight="ELY999")  # keeps the feed alive
        for i in range(4):
            for k, h in enumerate(hexes):
                feed.aircraft[h] = airborne(32.4 + 0.2 * k, 34.0 + i * 0.03, 20000, flight=f"ELY31{k}")
            feed.local = set(hexes) | {"738002"}
            step(mon, clock)
        feed.local = {"738002"}
        for h in hexes:  # not heard at all (also not by a follow request)
            feed.aircraft.pop(h)
        for _ in range(16):
            step(mon, clock)
        for k, h in enumerate(hexes):
            feed.aircraft[h] = airborne(32.4 + 0.2 * k, 34.3, 20000, flight=f"ELY31{k}")
        feed.local = set(hexes) | {"738002"}
        for _ in range(3):
            step(mon, clock)
        self.assertEqual(kinds(alerts), ["MASS_SILENCE"], [a["message"] for a in alerts])
        self.assertRegex(alerts[0]["message"], r"^silence-\d{8}T\d{4}Z: 3 aircraft")

    def test_course_change(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        tracks = [90] * 8 + [200] * 12  # reported once the new track held 2 min
        fly(mon, feed, clock, "738001", path(33.0, 33.5, tracks, [30000] * len(tracks), dt=15), dt=15,
            flight="ELY315", r="4X-EDC", t="B789")
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
        feed.route_status = 500
        feed.local = {"738001"}
        for i in range(6):
            feed.aircraft["738001"] = airborne(32.5, 34.5, 30000, flight=f"ABC{i}")
            step(mon, clock)
        self.assertEqual(sum("/standing-data/" in p for m, p in feed.calls), 3)
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


def path(lat, lon, tracks, alts, gs=450, dt=10, **kw):
    """Physically consistent states every `dt` s: positions follow the tracks at `gs` knots."""
    out = []
    for i, (trk, alt) in enumerate(zip(tracks, alts)):
        nxt = alts[i + 1] if i + 1 < len(alts) else alt
        out.append(dict(lat=lat, lon=lon, alt_baro=alt, track=trk % 360, gs=gs,
                        baro_rate=int((nxt - alt) * 60 / dt), **kw))
        lat, lon = fw.destination(lat, lon, trk, gs * dt / 3600)
    return out


def fly(mon, feed, clock, hexid, states, local=True, dt=10, **ident):
    ident = {"flight": "TST001", "r": "SX-ABC", "t": "A320", **ident}
    for st in states:
        feed.aircraft[hexid] = {**st, "flight": ident["flight"].ljust(8), "r": ident["r"], "t": ident["t"],
                                **{k: v for k, v in ident.items() if k not in ("flight", "r", "t")}}
        if local:
            feed.local.add(hexid)
        step(mon, clock, dt)


def kinds(alerts, hexid=None):
    return [a["kind"] for a in alerts if hexid is None or a["hex"] == hexid]


def racetrack(laps, start=(32.5, 33.8), alt=16000, gs=250):
    """Holding pattern every 10 s: 1-min legs, 180-deg turns at 3 deg/s (4 min a lap)."""
    tracks = []
    for _ in range(laps):
        for heading in (90, 270):
            tracks += [heading] * 6 + [heading + 30 * i for i in range(1, 7)]
    return path(*start, tracks, [alt] * len(tracks), gs=gs)


class ShareCallsigns(unittest.TestCase):
    """The monitor tells the map page's relay which aircraft flew which callsign (tools/cors_worker.js
    /learn): the board does not name the aircraft, and the worker gets 429 from adsb.lol itself."""
    def test_sends_new_pairs_every_interval_only_with_a_token(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"RELAY_TOKEN": "s3cret"}):
            mon, feed, clock, _ = make(["--share-interval", "60"])
            path_ = path(32.3, 34.5, [180] * 3, [9000] * 3)
            fly(mon, feed, clock, "738062", path_, flight="ELY5064")
            self.assertEqual(len(feed.learned), 1)                      # first cycle: what it heard
            body, auth = feed.learned[0]
            self.assertEqual(list(body), ["ELY5064"])
            self.assertEqual(body["ELY5064"][0], "738062")              # [hex, data time heard]
            self.assertLessEqual(body["ELY5064"][1], mon.tracks["738062"].last_msg)
            self.assertEqual(auth, "Bearer s3cret")
            feed.local.discard("738062")                                # ELY5064 no longer heard
            fly(mon, feed, clock, "4b1805", path(32.2, 34.6, [90] * 6, [5000] * 6), flight="CFG4308")
            self.assertEqual(len(feed.learned), 2)                      # 60 s later, only new hearings
            self.assertIn("CFG4308", feed.learned[1][0])
            feed.local.clear(); feed.aircraft.clear()                   # nothing heard any more:
            for _ in range(8):                                          # (the last hearings go out once)
                step(mon, clock)
            sent = len(feed.learned)
            for _ in range(8):
                step(mon, clock)
            self.assertEqual(len(feed.learned), sent)                   # nothing new, nothing sent
        with mock.patch.dict(os.environ, {"RELAY_TOKEN": ""}):
            mon, feed, clock, _ = make()
            fly(mon, feed, clock, "738062", path_, flight="ELY5064")
            self.assertEqual(feed.learned, [])                          # no token: nothing sent

    def test_a_failing_relay_does_not_stop_the_poll(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"RELAY_TOKEN": "s3cret"}):
            mon, feed, clock, alerts = make(["--share-interval", "10"])
            feed.limited.add("/learn")
            fly(mon, feed, clock, "738062", path(32.3, 34.5, [180] * 4, [9000] * 4), flight="ELY5064")
            self.assertIn("738062", mon.tracks)                         # still polled every cycle
            self.assertGreater(sum(1 for m, p in feed.calls if "/point/" in p), 3)


class HoldingTests(unittest.TestCase):
    def test_s_turn_is_not_a_course_change(self):
        """6H254, 6 Oct 2026: 148 -> 76 -> 175 deg at FL320 approaching TLV (vectoring)."""
        mon, feed, clock, alerts = make(["--no-routes"])
        tracks = [148] * 8 + [148 - 6 * i for i in range(1, 13)] + [76 + 8 * i for i in range(1, 13)] + [172] * 20
        fly(mon, feed, clock, "4a0481", path(33.8, 32.9, tracks, [32000] * len(tracks)), flight="ISR254")
        self.assertNotIn("COURSE_CHANGE", kinds(alerts))

    def test_holding_pattern_is_not_a_course_change(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        fly(mon, feed, clock, "4a0482", racetrack(4), flight="ISR724")
        self.assertEqual(kinds(alerts), [])

    def test_delay_orbit_of_an_arrival_is_quiet(self):
        """BBG251, 6 Oct 2026: a 360-deg orbit ~60 nm out, then on towards TLV (no COURSE_CHANGE,
        no OFF_COURSE for the outbound half)."""
        mon, feed, clock, alerts = with_board([board_row("BZ", "251", "A", local_time(Clock(), 1), "BCN")])
        tracks = ([143] * 8 + [143 + 12 * i for i in range(1, 15)] + [313] * 7    # out, away from TLV (70 s)
                  + [313 + 12 * i for i in range(1, 15)] + [120] * 20)                # and back in
        fly(mon, feed, clock, "4d2251", path(32.9, 33.5, tracks, [14000] * len(tracks), gs=300),
            flight="BBG251", r="9H-SHO", t="B738")
        self.assertEqual(kinds(alerts), [])

    def test_airliner_holding_unusually_long(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        fly(mon, feed, clock, "4a0483", racetrack(17), flight="ISR724")  # 68 min
        holds = [a["message"] for a in alerts if a["kind"] == "HOLDING"]
        self.assertEqual(len(holds), 2, holds)  # after 30 and 60 min
        self.assertRegex(holds[0], r"circling for 3\d min")
        self.assertNotIn("COURSE_CHANGE", kinds(alerts), "turns in a hold are never reversals, also when hot")

    def test_military_and_non_airline_holding_is_their_job(self):
        for ident in ({"flight": "SHUFL", "dbFlags": 1}, {"flight": "4XCJA"}):
            mon, feed, clock, alerts = make(["--no-routes"])
            fly(mon, feed, clock, "738bed", racetrack(10), **ident)
            self.assertNotIn("HOLDING", kinds(alerts), ident)


class LandingTests(unittest.TestCase):
    TLV = (32.0114, 34.8867)

    def on_ground(self, mon, feed, clock, flight, minutes, step_s=60):
        for _ in range(int(minutes * 60 / step_s)):
            fly(mon, feed, clock, "48af08", [dict(lat=self.TLV[0], lon=self.TLV[1], alt_baro="ground", gs=0,
                                                  track=90)], dt=step_s, flight=flight, r="SP-LVI", t="B38M")

    def test_departure_landing_back_alerts_once(self):
        mon, feed, clock, alerts = with_board([board_row("LO", "4", "D", local_time(Clock(), 0), "WAW")])
        self.on_ground(mon, feed, clock, "LOT4", 3)
        out = path(*self.TLV, [270] * 40 + [90] * 40, [20000] * 80)  # 50 nm out and back
        fly(mon, feed, clock, "48af08", out, flight="LOT4", r="SP-LVI", t="B38M")
        self.on_ground(mon, feed, clock, "LOT4", 20)
        self.assertEqual(kinds(alerts, "48af08").count("RETURNED"), 1, kinds(alerts))

    def test_arrival_turning_around_as_a_departure_is_not_returned(self):
        """LOT7MA landed at TLV; 1.5 h later the parked aircraft was LOT4CG, a departure."""
        mon, feed, clock, alerts = with_board([board_row("LO", "7", "A", local_time(Clock(), 0), "WAW"),
                                               board_row("LO", "4", "D", local_time(Clock(), 2), "WAW")])
        start = fw.destination(*self.TLV, 270, 60)
        fly(mon, feed, clock, "48af08", path(*start, [90] * 48, [20000] * 48), flight="LOT7",
            r="SP-LVI", t="B38M")
        self.on_ground(mon, feed, clock, "LOT7", 30)
        self.on_ground(mon, feed, clock, "LOT4", 60)
        self.assertNotIn("RETURNED", kinds(alerts, "48af08"))


AMMAN_SPOOF = (31.7171, 35.9993)  # 6 Oct 2026: aircraft at cruise reported motionless here


def frozen(alt=35000):
    return dict(lat=AMMAN_SPOOF[0], lon=AMMAN_SPOOF[1], alt_baro=alt, track=0, gs=0.7, baro_rate=0)


class SpoofingTests(unittest.TestCase):
    def run_flights(self, mon, feed, clock, flights, steps):
        """flights: hex -> (states per step or None when not heard, ident)."""
        for i in range(steps):
            feed.local = set()
            for hexid, (states, ident) in flights.items():
                st = states[i] if i < len(states) else None
                if st is None:
                    feed.aircraft.pop(hexid, None)
                    continue
                feed.aircraft[hexid] = {**st, "flight": ident.ljust(8), "r": "SX-" + hexid[-3:].upper(), "t": "A320"}
                feed.local.add(hexid)
            step(mon, clock)

    def test_mass_spoofing_is_one_labelled_episode(self):
        """Three airliners frozen at one point for 3 min, then on their real courses again."""
        mon, feed, clock, alerts = make(["--no-routes"])
        flights = {}
        for k, (hexid, start) in enumerate((("4ba001", 0), ("4ba002", 6), ("4ba003", 12))):
            states = path(32.6 + 0.1 * k, 33.9, [300] * 60, [35000] * 60)
            for i in range(12 + start, 30 + start):  # 3 min motionless, starting 0 / 1 / 2 min apart
                states[i] = frozen()
            flights[hexid] = (states, f"TST10{k}")
        self.run_flights(mon, feed, clock, flights, 60)
        spoof = [a for a in alerts if a["kind"] == "GPS_SPOOFING"]
        self.assertEqual(len(spoof), 1, [a["message"] for a in spoof])
        label = spoof[0]["message"].split(":")[0]
        self.assertRegex(label, r"^spoof-\d{8}T\d{4}Z$")
        self.assertEqual(spoof[0]["spoof"], label)
        noise = {"LOST_CONTACT", "POSITION_JUMP", "COURSE_CHANGE", "SHARP_TURN", "OFF_COURSE", "TOWARD_ISRAEL"}
        self.assertEqual([a for a in alerts if a["kind"] in noise], [])
        for _ in range(100):  # 16+ min without spoofed reports: the episode ends, once
            step(mon, clock)
        ended = [a for a in alerts if a["kind"] == "GPS_SPOOFING" and "ended" in a["message"]]
        self.assertEqual(len(ended), 1)
        self.assertIn(f"{label} ended: 3 aircraft", ended[0]["message"])

    def test_single_frozen_aircraft_raises_no_position_alerts(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        states = path(32.6, 33.9, [300] * 40, [35000] * 40)
        states[12:30] = [frozen()] * 18
        self.run_flights(mon, feed, clock, {"4ba004": (states, "TST104")}, 40)
        self.assertEqual(kinds(alerts), [], "one aircraft is not an episode; its fake reports are ignored")

    def test_upset_with_low_ground_speed_is_not_spoofing(self):
        """A deep stall: 40 kt over the ground but falling 15,000 ft/min - a real emergency."""
        mon, feed, clock, alerts = make(["--no-routes"])
        alts = [35000 - 2500 * i for i in range(9)]
        states = path(32.6, 33.9, [300] * 9, alts, gs=40)[:-1]  # (path()'s last point has no rate)
        self.run_flights(mon, feed, clock, {"4ba005": (path(32.6, 33.9, [300] * 6, [35000] * 6) + states, "TST105")}, 14)
        self.assertIn("VERTICAL_RATE", kinds(alerts))
        self.assertNotIn("GPS_SPOOFING", kinds(alerts))
        self.assertFalse(mon.tracks["4ba005"].spoof)

    def test_hovering_helicopter_is_not_spoofing(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        hover = [dict(lat=32.6, lon=34.6, alt_baro=6000, track=0, gs=0, baro_rate=0, category="A7")] * 20
        self.run_flights(mon, feed, clock, {"738a07": (hover, "4XBHA")}, 20)
        self.assertFalse(mon.tracks["738a07"].spoof)
        self.assertEqual(len(mon.tracks["738a07"].samples), 20)


class MlatTests(unittest.TestCase):
    def test_mlat_report_with_the_aircrafts_spoofed_velocity_is_not_frozen(self):
        """MLAT position, but gs / track copied from the aircraft (0.7 kt, 0 deg): not spoofed."""
        mon, feed, clock, alerts = make(["--no-routes"])
        states = path(32.0, 34.8, [270] * 10, [12000] * 10, gs=270)
        states = [{**st, "gs": 0.7, "track": 0, "type": "mlat", "mlat": ["lat", "lon"]} for st in states]
        fly(mon, feed, clock, "46b828", states, flight="AEE925", r="SX-NAE", t="A21N")
        t = mon.tracks["46b828"]
        self.assertFalse(t.spoof)
        self.assertTrue(all(x.mlat and x.gs is None and x.track is None for x in t.samples))

    def test_spoofed_flight_follows_its_mlat_fixes(self):
        """AEE925, 6 Oct 2026: climbing out of TLV with GPS spoofed (frozen at Amman, then drifting
        far away at believable speeds) while MLAT followed the real departure."""
        mon, feed, clock, alerts = make(["--no-routes"])
        real = path(32.0, 34.7, [287] * 30, [8000 + 300 * i for i in range(30)], gs=280)
        states = []
        for i, st in enumerate(real):
            if i < 4:
                states.append(st)
            elif i in (4, 5):
                states.append(frozen(st["alt_baro"]))                       # GPS: motionless at Amman
            elif i % 2 or i == 6:
                states.append({**st, "type": "mlat", "mlat": ["lat", "lon"]})  # MLAT: the real path
            else:
                states.append({**st, "lat": 31.7, "lon": 36.4 + 0.02 * i})      # GPS: drifting, 150 km off
        fly(mon, feed, clock, "46b828", states, flight="AEE925", r="SX-NAE", t="A21N")
        t = mon.tracks["46b828"]
        self.assertTrue(t.spoof)
        self.assertNotIn("POSITION_JUMP", kinds(alerts))
        self.assertTrue(all(x.lon < 35 for x in t.samples), "only real positions in the track")


class TrustedSpeedTests(unittest.TestCase):
    def test_spoofed_low_speed_does_not_make_a_climb_steep(self):
        """QTR94R, 6 Oct 2026: B789 at FL390 'at' 147 kt during spoofing, reporting +12,224 ft/min
        while climbing normally. The altitude history unmasks the rate as a glitch - but judged with
        the fake speed, even the real climb looked steep enough to confirm it."""
        mon, feed, clock, alerts = make(["--no-routes"])
        states = path(32.6, 33.9, [300] * 15, [37000 + 300 * i for i in range(15)], gs=480)
        # positions move at 480 kt, reported 117; reported rate +12,224 ft/min, real climb 1,800
        states = [{**st, "gs": 117, "baro_rate": 12224} for st in states]
        fly(mon, feed, clock, "4ba020", states, flight="QTR94R", r="A7-BHA", t="B789")
        self.assertNotIn("VERTICAL_RATE", kinds(alerts))

    def test_speeding_up_into_a_dive_is_trusted_at_once(self):
        """4XDAN (P750), 10 Oct 2026: ~55 kt at 11,500 ft, then a dive at 140-155 kt and -5,600
        ft/min. Averaged over the last minute, positions gave 67 kt against the reported 138 kt,
        so the speed was distrusted and the alert came 30 s late, on its last sample before going
        out of coverage. Speed changes: the average must lie between the speeds at both ends."""
        mon, feed, clock, alerts = make(["--no-routes"])
        slow = path(31.36, 35.387, [185] * 6, [11800 - 50 * i for i in range(6)], gs=55)
        lat, lon = fw.destination(slow[-1]["lat"], slow[-1]["lon"], 185, 55 * 20 / 3600)
        dive = path(lat, lon, [190] * 5, [10600 - 930 * i for i in range(5)], gs=140)
        fly(mon, feed, clock, "739215", slow, flight="4XDAN", r="4X-DAN", t="P750")
        clock.t += 10  # a poll lost to HTTP 429
        fly(mon, feed, clock, "739215", dive[:1], flight="4XDAN", r="4X-DAN", t="P750")
        self.assertEqual(kinds(alerts), ["VERTICAL_RATE"])


def lift(gs_run=55, gs_dive=150, run_reported=None):
    """A skydiving lift like 4XDAN's: a 2-min jump run at ~11,800 ft, then -5,600 ft/min."""
    run = path(31.36, 35.387, [185] * 12, [11800 - 40 * i for i in range(12)], gs=gs_run)
    if run_reported is not None:
        run = [{**st, "gs": run_reported} for st in run]
    lat, lon = fw.destination(run[-1]["lat"], run[-1]["lon"], 185, gs_run * 10 / 3600)
    return run + path(lat, lon, [190] * 4, [11300 - 930 * i for i in range(4)], gs=gs_dive)


class SkydiveTests(unittest.TestCase):
    def test_jump_plane_dive_is_described(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        fly(mon, feed, clock, "739215", lift(), flight="4XDAN", r="4X-DAN", t="P750", category="A1")
        self.assertEqual(kinds(alerts), ["VERTICAL_RATE"])
        self.assertIn("skydiving pattern, PAC 750XL: 55 kt at 11800 ft", alerts[0]["message"])
        self.assertEqual(alerts[0]["skydive"]["lift"], 1)

    def test_airliner_flying_it_is_priority_5(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        fly(mon, feed, clock, "738001", lift(), flight="ELY315", r="4X-EKT", t="B738")
        self.assertEqual(kinds(alerts)[0], "SKYDIVE_PATTERN")
        self.assertEqual(alerts[0]["priority"], 5)
        self.assertIn("an airliner (B738) flew a skydiving pattern", alerts[0]["message"])

    def test_large_aircraft_without_airline_callsign(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        fly(mon, feed, clock, "738002", lift(), flight="4XABC", r="4X-ABC", t="E190")
        self.assertIn("a large aircraft (E190)", alerts[0]["message"])

    def test_military_paradrop_is_only_described(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        fly(mon, feed, clock, "738003", lift(), flight="RCH123", r="", t="C30J", dbFlags=1)
        self.assertNotIn("SKYDIVE_PATTERN", kinds(alerts))

    def test_slow_speed_belied_by_positions_is_no_jump_run(self):
        """Spoofed / corrupted velocity: 'slow' while the positions move at 300 kt."""
        mon, feed, clock, alerts = make(["--no-routes"])
        fly(mon, feed, clock, "738004", lift(gs_run=300, gs_dive=300, run_reported=70),
            flight="ELY316", r="4X-EKU", t="B738")
        self.assertNotIn("SKYDIVE_PATTERN", kinds(alerts))


class AltitudeSourceTests(unittest.TestCase):
    def test_gps_altitude_is_converted_to_barometric(self):
        """4XDAN, 10 Oct 2026: GPS altitude ~575 ft above barometric; one report with only the GPS
        value read as a 575-ft step within a second."""
        mon, feed, clock, alerts = make(["--no-routes"])
        states = path(31.36, 35.387, [190] * 6, [10000 - 100 * i for i in range(6)], gs=140)
        states = [{**st, "alt_geom": st["alt_baro"] + 575} for st in states]
        states[4] = {k: v for k, v in states[4].items() if k != "alt_baro"}
        fly(mon, feed, clock, "739215", states[:5], flight="4XDAN", r="4X-DAN", t="P750")
        self.assertEqual(mon.tracks["739215"].last.alt, 9600)


class IntegrityTests(unittest.TestCase):
    def test_jammed_but_on_track_is_still_watched(self):
        """GPS jammed: NIC 0, the aircraft navigates on inertial - positions roughly right."""
        mon, feed, clock, alerts = make(["--no-routes"])
        states = [{**st, "nic": 0, "nac_p": 0} for st in path(32.6, 33.9, [300] * 20, [35000] * 20)]
        fly(mon, feed, clock, "4ba010", states, flight="TST110")
        self.assertEqual(kinds(alerts), [])
        self.assertEqual(len(mon.tracks["4ba010"].samples), 20)

    def test_untrusted_position_out_of_reach_is_dropped(self):
        """AEE925: NIC 0 on a fake position 787 km away, NIC 8 again on the real track."""
        mon, feed, clock, alerts = make(["--no-routes"])
        states = [{**st, "nic": 8} for st in path(32.6, 33.9, [300] * 12, [35000] * 12)]
        states[6] = {**states[6], "lat": 31.73, "lon": 36.4, "nic": 0, "nac_p": 0}
        fly(mon, feed, clock, "4ba011", states, flight="TST111")
        self.assertNotIn("POSITION_JUMP", kinds(alerts))
        self.assertTrue(all(x.lon < 34 for x in mon.tracks["4ba011"].samples))

    def test_nic_drops_together_are_one_episode(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        hexes = ["4ba012", "4ba013", "4ba014"]
        tracks = {h: path(32.4 + 0.2 * k, 33.9, [300] * 160, [35000] * 160) for k, h in enumerate(hexes)}
        for i in range(160):
            feed.local = set(hexes)
            for k, h in enumerate(hexes):
                bad = 6 + 3 * k <= i < 40  # NIC 0 from 1 / 1.5 / 2 min to 6.5 min
                feed.aircraft[h] = {**tracks[h][i], "nic": 0 if bad else 8, "flight": f"TST11{k}  ",
                                    "r": "SX-" + h[-3:], "t": "A320"}
            step(mon, clock)
        gps = [a for a in alerts if a["kind"] == "GPS_DEGRADED"]
        self.assertEqual(len(gps), 2, [a["message"] for a in alerts])  # start, end
        self.assertRegex(gps[0]["message"], r"^gps-\d{8}T\d{4}Z: 3 aircraft")
        self.assertIn("ended", gps[1]["message"])
        self.assertEqual([k for k in kinds(alerts) if k != "GPS_DEGRADED"], [])


class SharpTurnGlitchTests(unittest.TestCase):
    def test_corrupted_velocity_reading_is_not_a_sharp_turn(self):
        """4X-CZF, 6 Oct 2026: straight on 131 deg at FL300, one reading 191 deg at a plausible
        449 kt (the live feed's copy of a corrupted message). Also THY6685 in the FZ1073 replay:
        a gentle climbing turn through 23 deg, one reading 255 deg."""
        mon, feed, clock, alerts = make(["--no-routes"])
        states = path(33.38, 32.87, [131] * 12, [30000] * 12, gs=450)
        states[6] = {**states[6], "track": 191}
        fly(mon, feed, clock, "73934e", states, flight="4XCZF", r="4X-CZF", t="H25B")
        self.assertNotIn("SHARP_TURN", kinds(alerts))


class GapAndDetourJumps(unittest.TestCase):
    """POSITION_JUMP beyond short hops: reappearing too far after a gap (THY1KG, 5 Oct 2026: 94 nm
    in 408 s, ~833 kt, over the Eastern Mediterranean) and one position off the track and back."""

    def reappear(self, nm, gap=300, **kw):
        mon, feed, clock, alerts = make(["--no-routes"])
        states = path(32.3, 34.0, [90] * 4, [30000] * 4)
        fly(mon, feed, clock, "4bb1e1", states, **kw)
        feed.local.discard("4bb1e1")
        last = feed.aircraft.pop("4bb1e1")
        for _ in range(gap // 10 - 1):
            step(mon, clock)
        lat, lon = fw.destination(last["lat"], last["lon"], 90, nm)
        fly(mon, feed, clock, "4bb1e1", path(lat, lon, [90] * 3, [30000] * 3), **kw)
        return [a["message"] for a in alerts if a["kind"] == "POSITION_JUMP"]

    def test_reappearing_too_far_after_a_gap(self):
        jumps = self.reappear(70)  # 70 nm in 300 s: ~840 kt average
        self.assertEqual(len(jumps), 1, jumps)
        self.assertIn("reappeared 70", jumps[0])

    def test_reappearing_where_it_could_have_flown_is_quiet(self):
        self.assertEqual(self.reappear(40), [])  # ~480 kt: just unheard

    def test_military_jets_keep_the_short_hop_limit(self):
        self.assertEqual(self.reappear(70, dbFlags=1), [])

    def test_one_position_off_the_track_and_back(self):
        """8 nm aside at 30 s samples: each hop ~1060 kt (under --max-speed), but via it the
        aircraft would average ~1060 kt while the direct track is 450 kt."""
        mon, feed, clock, alerts = make(["--no-routes"])
        states = path(32.3, 34.0, [90] * 8, [30000] * 8, dt=30)
        lat, lon = fw.destination(states[5]["lat"], states[5]["lon"], 0, 8)
        states[5] = {**states[5], "lat": lat, "lon": lon}
        fly(mon, feed, clock, "4bb1e1", states, dt=30)
        jumps = [a["message"] for a in alerts if a["kind"] == "POSITION_JUMP"]
        self.assertEqual(len(jumps), 1, jumps)
        self.assertIn("off the track, then back", jumps[0])


def board_row(callsign_iata, num, direction, when_local, other="?"):
    return {"CHOPER": callsign_iata, "CHFLTN": num, "CHAORD": direction, "CHSTOL": when_local,
            "CHPTOL": when_local, "CHLOC1": other, "CHLOC1D": other, "CHRMINE": "ON TIME"}


def with_board(rows, argv=()):
    mon, feed, clock, alerts = make(["--no-routes", "--rate", "60", *argv], with_schedule=True)
    mon.schedule.load(rows, datetime.fromtimestamp(clock(), mon.schedule.tz))
    return mon, feed, clock, alerts


def local_time(clock, hours):
    return datetime.fromtimestamp(clock() + hours * 3600, fw.ZoneInfo("Asia/Jerusalem")).strftime("%Y-%m-%dT%H:%M")


class SteepnessTests(unittest.TestCase):
    def test_fast_but_shallow_descent_is_normal(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        fly(mon, feed, clock, "738001", path(32.5, 33.0, [90] * 8, [30000 - 1000 * i for i in range(8)]))
        self.assertNotIn("VERTICAL_RATE", kinds(alerts))  # -6000 ft/min at 450 kt is ~7.5 deg

    def test_steep_approach_alerts_even_near_an_airport(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        # 15 nm west of Amman, 160 kt, -3600 ft/min: ~12.5 deg (glide slope 3, steep approach ~6)
        fly(mon, feed, clock, "740001", path(31.72, 35.70, [90] * 8, [7000 - 600 * i for i in range(8)], gs=160))
        hits = [a for a in alerts if a["kind"] == "VERTICAL_RATE"]
        self.assertEqual(len(hits), 1)
        self.assertIn("from AMM", hits[0]["message"])

    def test_normal_departure_climb_is_not_steep(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        # 10 nm from TLV, 250 kt, +4500 ft/min (~10 deg): normal initial climb
        fly(mon, feed, clock, "738001", path(32.1, 35.05, [40] * 8, [6000 + 750 * i for i in range(8)], gs=250))
        self.assertNotIn("VERTICAL_RATE", kinds(alerts))

    def test_dive_after_a_descent_alert_is_not_suppressed(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        alts = [34000] * 4 + [33000, 32000, 31000] + [27500, 24000, 20500, 17000]  # -6000 then -21000
        fly(mon, feed, clock, "8965d1", path(31.0, 33.5, [90] * len(alts), alts, gs=250))
        hits = [a for a in alerts if a["kind"] == "VERTICAL_RATE"]
        self.assertEqual(len(hits), 2, [a["message"] for a in hits])
        self.assertIn("-21000", hits[1]["message"])


class TurnTests(unittest.TestCase):
    def test_sharp_turn(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        tracks = [90] * 6 + [120, 150, 180] + [180] * 5  # 90 deg in 30 s at 450 kt: ~50 deg bank
        fly(mon, feed, clock, "738001", path(33.5, 31.5, tracks, [30000] * len(tracks)))
        self.assertEqual(kinds(alerts).count("SHARP_TURN"), 1)

    def test_regular_turn_is_a_course_change_not_sharp(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        tracks = [90] * 6 + [90 + 7.5 * i for i in range(1, 13)] + [180] * 16  # 90 deg in 2 min, then held
        fly(mon, feed, clock, "738001", path(33.5, 31.5, tracks, [30000] * len(tracks)))
        self.assertIn("COURSE_CHANGE", kinds(alerts))
        self.assertNotIn("SHARP_TURN", kinds(alerts))

    def routine_turn_near_beirut(self, **extra):
        mon, feed, clock, alerts = make(["--no-routes"])
        tracks = [270] * 6 + [270 + 7.5 * i for i in range(1, 13)] + [0] * 4
        fly(mon, feed, clock, "748001", path(33.75, 35.20, tracks, [15000] * len(tracks)), flight="MEA424",
            **extra)
        return kinds(alerts)

    def test_routine_turn_near_an_airport_is_not_reported(self):
        self.assertNotIn("COURSE_CHANGE", self.routine_turn_near_beirut())

    def test_same_turn_is_reported_for_a_flight_already_alerting(self):
        self.assertIn("COURSE_CHANGE", self.routine_turn_near_beirut(squawk="7700"))

    def test_learned_route_corner_is_not_reported(self):
        import tempfile
        tracks = [90] * 6 + [90 + 7.5 * i for i in range(1, 13)] + [180] * 16
        for out, expect in ((180, False), (0, True)):
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump({"cell": 0.5, "zones": [{"lat": 33.25, "lon": 31.75, "out": out, "n": 9}]}, f)
            mon, feed, clock, alerts = make(["--no-routes", "--turn-zones", f.name])
            fly(mon, feed, clock, "738001", path(33.3, 31.6, tracks, [30000] * len(tracks)))
            os.unlink(f.name)
            self.assertEqual("COURSE_CHANGE" in kinds(alerts), expect, f"zone heading {out}")


class DestinationTests(unittest.TestCase):
    def test_arrival_turning_away_long_before_landing(self):
        mon, feed, clock, alerts = with_board([board_row("LY", "26", "A", local_time(mon_clock(), 1), "EWR")])
        # 150 nm west of TLV, inbound (track 80), then a U-turn and 4 min heading away
        tracks = [80] * 6 + [80 + 15 * i for i in range(1, 13)] + [260] * 24
        fly(mon, feed, clock, "738026", path(32.3, 32.0, tracks, [30000] * len(tracks)), flight="ELY026",
            r="4X-ECA")
        self.assertIn("OFF_COURSE", kinds(alerts))

    def test_off_course_repeats_every_repeat_minutes(self):
        """A persisting condition: once, then every --repeat-minutes (30), not every --cooldown (5)."""
        mon, feed, clock, alerts = with_board([board_row("LY", "26", "A", local_time(mon_clock(), 1), "EWR")])
        tracks = [80] * 6 + [80 + 15 * i for i in range(1, 13)] + [260] * 200  # ~36 min heading away
        fly(mon, feed, clock, "738026", path(32.3, 33.0, tracks, [30000] * len(tracks)), flight="ELY026",
            r="4X-ECA")
        self.assertEqual(kinds(alerts).count("OFF_COURSE"), 2, kinds(alerts))

    def test_route_bend_is_not_off_course(self):
        mon, feed, clock, alerts = with_board([board_row("LY", "26", "A", local_time(mon_clock(), 1), "EWR")])
        fly(mon, feed, clock, "738026", path(32.3, 32.0, [20] * 30, [30000] * 30), flight="ELY026", r="4X-ECA")
        self.assertNotIn("OFF_COURSE", kinds(alerts))  # 60 deg off the direct line: a route bend

    def test_departure_turning_back(self):
        mon, feed, clock, alerts = with_board([board_row("LY", "315", "D", local_time(mon_clock(), -0.5), "LHR")])
        tracks = [255] * 6 + [255 + 15 * i for i in range(1, 13)] + [75] * 24
        fly(mon, feed, clock, "738315", path(32.45, 32.6, tracks, [33000] * len(tracks)), flight="ELY315",
            r="4X-EDC")
        self.assertIn("TURNING_BACK", kinds(alerts))


def mon_clock():
    return Clock()


class TowardIsraelTests(unittest.TestCase):
    # Over Jordan at FL350 heading north (not toward Israel), then a turn west toward the Golan
    TRACKS = [0] * 30 + [360 - 12 * i for i in range(1, 8)] + [270] * 8

    def run_track(self, **ident):
        mon, feed, clock, alerts = make(["--no-routes"])
        fly(mon, feed, clock, "740111", path(32.2, 36.9, self.TRACKS, [35000] * len(self.TRACKS)), **ident)
        return alerts

    def test_foreign_flight_turning_toward_israel(self):
        alerts = self.run_track(flight="RJA111", r="JY-AYA")
        hits = [a for a in alerts if a["kind"] == "TOWARD_ISRAEL"]
        self.assertEqual(len(hits), 1, kinds(alerts))
        self.assertIn("turned", hits[0]["message"])
        self.assertEqual(hits[0]["priority"], 5)

    def test_military_aircraft_over_israel_is_routine(self):
        """6 Oct 2026: KC-135 tankers and a military Gulfstream over Israel for hours - 29 alerts."""
        self.assertNotIn("TOWARD_ISRAEL", kinds(self.run_track(flight="IRON", r="", dbFlags=1)))

    def test_israeli_flight_on_the_same_track_is_not_flagged(self):
        self.assertNotIn("TOWARD_ISRAEL", kinds(self.run_track(flight="ELY111", r="4X-EHA")))

    def test_straight_flight_past_israel_is_not_flagged(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        fly(mon, feed, clock, "740112", path(31.0, 36.9, [0] * 40, [35000] * 40), flight="RJA112", r="JY-AYB")
        self.assertNotIn("TOWARD_ISRAEL", kinds(alerts))


class LostContactTests(unittest.TestCase):
    def descend_and_vanish(self, mon, feed, clock, **ident):
        feed.local.add("999999")
        feed.aircraft["999999"] = airborne(31.0, 34.0, 20000, flight="KEEP1")
        # 15 nm west of Amman, descending through 8000 ft, then silence
        fly(mon, feed, clock, "740200", path(31.72, 35.70, [90] * 5, [8000 - 250 * i for i in range(5)], gs=220),
            **ident)
        feed.local.discard("740200")
        del feed.aircraft["740200"]
        for _ in range(12):
            step(mon, clock)

    def test_silent_while_descending_into_another_airport_is_a_probable_landing(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        self.descend_and_vanish(mon, feed, clock, flight="RJA200", r="JY-AYC")
        self.assertNotIn("LOST_CONTACT", kinds(alerts))

    def test_tlv_arrival_doing_that_is_a_diversion(self):
        mon, feed, clock, alerts = with_board([board_row("LY", "200", "A", local_time(mon_clock(), 0.5))])
        self.descend_and_vanish(mon, feed, clock, flight="ELY200", r="4X-EKA")
        hits = [a for a in alerts if a["kind"] == "DIVERSION"]
        self.assertEqual(len(hits), 1, kinds(alerts))
        self.assertIn("landing at AMM", hits[0]["message"])

    def remote_silence(self, hot):
        mon, feed, clock, alerts = make(["--no-routes", "--rate", "60"])
        feed.local = {"999999"}
        feed.aircraft["999999"] = airborne(32.5, 34.5, 10000, flight="LOC1")
        t = mon.tracks.setdefault("738001", fw.Track("738001"))
        t.followed, t.last_msg, t.callsign = True, clock(), "ELY001"
        for k in range(3):
            t.samples.append(fw.Sample(clock() - 20 + k * 10, 45.0, 10.0, 36000, 90, 450, 0, False))
        if hot:
            t.hot_until = clock() + 3600
        mon.multi[("adsb.lol", "hex")] = True
        for _ in range(70):  # ~12 min of silence, follow requests answered (aircraft absent)
            step(mon, clock)
        return kinds(alerts)

    def test_remote_cruise_gap_is_logged_not_alerted(self):
        self.assertNotIn("LOST_CONTACT", self.remote_silence(hot=False))

    def test_remote_silence_of_an_alerting_flight_is_alerted(self):
        self.assertIn("LOST_CONTACT", self.remote_silence(hot=True))


class FollowTests(unittest.TestCase):
    def test_alerting_flight_is_followed_every_cycle(self):
        mon, feed, clock, alerts = make(["--no-routes", "--rate", "60"])
        feed.local = {"999999", "740300"}
        feed.aircraft["999999"] = airborne(32.5, 34.5, 10000, flight="LOC1")
        feed.aircraft["740300"] = dict(airborne(33.5, 33.0, 30000, flight="RJA300", squawk="7700"), r="JY-AYD")
        step(mon, clock)
        self.assertTrue(mon.tracks["740300"].followed)  # not TLV traffic, but it alerted
        feed.local.discard("740300")  # leaves the circle
        n = len(feed.calls)
        for _ in range(6):
            step(mon, clock)
        hex_calls = [p for m, p in feed.calls[n:] if "/hex/" in p and "740300" in p]
        self.assertGreaterEqual(len(hex_calls), 5, "hot flights are asked for every cycle, not every 30 s")


class DistantArrival(unittest.TestCase):
    def test_found_far_away_lost_restored_and_diverted(self):
        """ELY002 (JFK->TLV) found by callsign search over Italy, followed by hex, silent for 13 min
        at cruise (logged, not alerted), heard again and on the ground near Rome -> DIVERSION."""
        mon, feed, clock, alerts = with_board([board_row("LY", "002", "A", local_time(mon_clock(), 3), "JFK")])
        feed.local = {"999999"}
        feed.aircraft["999999"] = airborne(32.5, 34.5, 10000, flight="LOC1")
        cruise = path(42.5, 14.0, [100] * 12, [38000] * 12, dt=10)
        fly(mon, feed, clock, "738002", cruise, local=False, flight="ELY002", r="4X-EDH")
        self.assertTrue(mon.tracks["738002"].followed)
        self.assertEqual(mon.tracks["738002"].sched.flight, "LY2")
        del feed.aircraft["738002"]
        for _ in range(80):
            step(mon, clock)
        self.assertTrue(mon.tracks["738002"].lost)
        landing = path(41.85, 12.35, [160] * 6, [3000, 2000, 1200, 600, 200, 0], gs=150)
        landing[-1]["alt_baro"] = "ground"
        fly(mon, feed, clock, "738002", landing, local=False, flight="ELY002", r="4X-EDH")
        k = kinds(alerts, "738002")
        self.assertIn("DIVERSION", k)
        self.assertNotIn("POSITION_JUMP", k)
        self.assertNotIn("LOST_CONTACT", k)


ROUTE_FILES = {
    "routes/schema-01/W/WZZ-3.csv": "﻿Callsign,Code,Number,AirlineCode,AirportCodes\nWZZ3W,WZZ,3W,WZZ,LHBP-LLBG\n",
    "routes/schema-01/R/RJA-all.csv": ("Callsign,Code,Number,AirlineCode,AirportCodes\n"
                                       "RJA111,RJA,111,RJA,OJAI-HEAL\nRJA112,RJA,112,RJA,OJAI-EGLL\n"),
    "routes/schema-01/U/UAE-all.csv": ("Callsign,Code,Number,AirlineCode,AirportCodes\n"
                                       "UAE121,UAE,121,UAE,OMDB-LTFM\nUAE122,UAE,122,UAE,LTFM-OMDB\n"),
    "airports/schema-01/L/LH.csv": "Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,Longitude,AltitudeFeet\n"
                                   "LHBP,Budapest,LHBP,BUD,Budapest,HU,47.436901,19.255600,495\n",
    "airports/schema-01/L/LL.csv": "Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,Longitude,AltitudeFeet\n"
                                   "LLBG,Ben Gurion,LLBG,TLV,Tel Aviv,IL,32.011398,34.886700,135\n",
    "airports/schema-01/O/OJ.csv": "Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,Longitude,AltitudeFeet\n"
                                   "OJAI,Queen Alia,OJAI,AMM,Amman,JO,31.722601,35.993198,2395\n",
    # a made-up destination due west of the TOWARD_ISRAEL test track, beyond Israel
    "airports/schema-01/H/HE.csv": "Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,Longitude,AltitudeFeet\n"
                                   "HEAL,Test West,HEAL,,,EG,32.60,30.00,100\n",
    "airports/schema-01/E/EG.csv": "Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,Longitude,AltitudeFeet\n"
                                   "EGLL,Heathrow,EGLL,LHR,London,GB,51.4706,-0.461941,83\n",
    "airports/schema-01/O/OM.csv": "Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,Longitude,AltitudeFeet\n"
                                   "OMDB,Dubai,OMDB,DXB,Dubai,AE,25.2528,55.3644,62\n",
    "airports/schema-01/L/LT.csv": "Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,Longitude,AltitudeFeet\n"
                                   "LTFM,Istanbul,LTFM,IST,Istanbul,TR,41.2753,28.7519,325\n",
}


def with_routes(argv=()):
    mon, feed, clock, alerts = make(["--rate", "600", "--rate-max", "6000", *argv])
    feed.files = dict(ROUTE_FILES)
    return mon, feed, clock, alerts


class StandingDataTests(unittest.TestCase):
    def test_airline_name_and_flight_number_in_alerts(self):
        """An airline missing from AIRLINE_ICAO still gets its name and IATA number from the list."""
        mon, feed, clock, alerts = with_routes()
        feed.files["airlines/schema-01/airlines.csv"] = ("﻿Code,Name,ICAO,IATA,PositioningFlightPattern,"
                                                         "CharterFlightPattern\nNOS,Neos,NOS,NO,,\n")
        feed.files["routes/schema-01/N/NOS-all.csv"] = ("Callsign,Code,Number,AirlineCode,AirportCodes\n"
                                                        "NOS123,NOS,123,NOS,LIMC-LLBG\n")
        feed.local = {"300001"}
        for _ in range(3):
            feed.aircraft["300001"] = airborne(32.5, 34.5, 30000, flight="NOS123", squawk="7700")
            step(mon, clock)
            clock.t += 10
        rec = [r for r in alerts if r["kind"] == "EMERGENCY"][0]
        self.assertEqual((rec["flight"], rec["airline"], rec["military"]), ("NO123", "Neos", False))

    def test_route_and_airport_lookup_with_one_request_per_file(self):
        mon, feed, clock, alerts = with_routes()
        sd = mon.standing

        def later(value):  # each new file is a low-priority request: let the budget refill
            clock.t += 10
            return value
        self.assertEqual(later(sd.route("WZZ3W")), ["LHBP", "LLBG"])     # airline split by first digit
        self.assertEqual(later(sd.route("RJA111")), ["OJAI", "HEAL"])    # small airline: one -all file
        self.assertIsNone(later(sd.route("WZZ3X")))
        self.assertIsNone(sd.route("4XHSC"))                            # not an airline callsign
        self.assertEqual(later(sd.airport("LLBG")), (32.011398, 34.8867))
        n = len(feed.calls)
        sd.route("WZZ3W"), sd.airport("LLBG")
        self.assertEqual(len(feed.calls), n, "files are cached")

    def test_board_airline_code_resolved_by_who_flies_to_tlv(self):
        """IATA codes are reused (NO = Neos and Aus-Air): the airline with TLV routes wins."""
        mon, feed, clock, alerts = make(["--rate", "600", "--rate-max", "6000"], with_schedule=True)
        feed.files = dict(ROUTE_FILES)
        feed.files["airlines/schema-01/airlines.csv"] = (
            "Code,Name,ICAO,IATA,PositioningFlightPattern,CharterFlightPattern\n"
            "AUS,Aus-Air,AUS,NO,,\nNOS,Neos,NOS,NO,,\nXYZ,Nowhere Air,XYZ,Q9,,\nXYW,Elsewhere,XYW,Q9,,\n")
        feed.files["routes/schema-01/A/AUS-all.csv"] = "Callsign,Code,Number,AirlineCode,AirportCodes\nAUS1,AUS,1,AUS,YMML-YSSY\n"
        feed.files["routes/schema-01/N/NOS-all.csv"] = ("Callsign,Code,Number,AirlineCode,AirportCodes\n"
                                                        "NOS1382,NOS,1382,NOS,LIMC-LLBG\n")
        mon.schedule.load([board_row("NO", "1382", "A", local_time(clock, 2), "MXP"),
                           board_row("Q9", "1", "A", local_time(clock, 2), "XXX")],
                          datetime.fromtimestamp(clock(), mon.schedule.tz))
        self.assertIn("NOS1382", mon.schedule.by_callsign)
        self.assertEqual(mon.schedule.airline_map["NO"], "NOS")
        self.assertNotIn("Q9", mon.schedule.airline_map, "neither candidate flies to TLV: left unmapped")

    def test_route_downloads_are_spread_over_cycles(self):
        """A cold start sees dozens of new callsigns; fetching all their files at once held up the
        first local poll for over a minute."""
        mon, feed, clock, alerts = with_routes()
        for i, cs in enumerate(("WZZ3W", "RJA111", "UAE121", "ABC12", "DEF34", "GHI56")):
            feed.aircraft[f"30000{i}"] = airborne(32.5, 34.0 + i / 10, 30000, flight=cs)
        feed.local = set(feed.aircraft)
        counts = []
        for _ in range(4):
            n = sum("/standing-data/" in p for m, p in feed.calls)
            step(mon, clock)
            counts.append(sum("/standing-data/" in p for m, p in feed.calls) - n)
        self.assertLessEqual(max(counts), 5, counts)  # the cap (3), and one callsign may overrun it
        self.assertGreater(counts[1], 0, "the rest follow in later cycles")

    def test_alphanumeric_callsign_classified_by_route(self):
        """Wizz-style callsigns never match the flight board; the route says it is a TLV arrival."""
        mon, feed, clock, alerts = with_routes()
        fly(mon, feed, clock, "471d61", path(34.0, 32.0, [130] * 4, [35000] * 4), flight="WZZ3W", r="HA-LDH")
        t = mon.tracks["471d61"]
        self.assertEqual(mon.classify(t), ("ARR", "LHBP-LLBG"))
        self.assertTrue(t.followed)

    def test_capture_attaches_routes_from_a_local_checkout(self):
        import tempfile
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
        import capture_incident
        with tempfile.TemporaryDirectory() as d:
            for rel, text in ROUTE_FILES.items():
                os.makedirs(os.path.join(d, os.path.dirname(rel)), exist_ok=True)
                with open(os.path.join(d, rel), "w", encoding="utf-8") as f:
                    f.write(text)
            fixture = {"meta": {}, "aircraft": {"471d61": {"points": [[0, 0, 0, 0, 0, 0, 0, "WZZ3W", None, None, None]]},
                                                "738001": {"points": [[0, 0, 0, 0, 0, 0, 0, "ELY999", None, None, None]]}}}
            capture_incident.attach_routes(fixture, d)
        self.assertEqual(fixture["routes"], {"WZZ3W": "LHBP-LLBG"})
        self.assertEqual(set(fixture["airports"]), {"LHBP", "LLBG"})


class RouteAwareTowardIsrael(unittest.TestCase):
    def run_track(self, callsign):
        mon, feed, clock, alerts = with_routes()
        tracks = TowardIsraelTests.TRACKS
        fly(mon, feed, clock, "740111", path(32.2, 36.9, tracks, [35000] * len(tracks)), flight=callsign,
            r="JY-AYA")
        return kinds(alerts)

    def test_flight_pointing_at_its_own_destination_beyond_israel_is_not_flagged(self):
        self.assertNotIn("TOWARD_ISRAEL", self.run_track("RJA111"))   # OJAI-HEAL, HEAL due west

    def test_implausible_route_does_not_exempt(self):
        self.assertIn("TOWARD_ISRAEL", self.run_track("RJA112"))      # "OJAI-EGLL", but turning west


class RouteOffCourse(unittest.TestCase):
    # over Iraq at FL370 heading northwest toward Istanbul for 5 min, then a U-turn and 5 min southeast
    TRACKS = [315] * 30 + [315 - 15 * i for i in range(1, 13)] + [135] * 30

    def run_track(self, callsign):
        mon, feed, clock, alerts = with_routes()
        fly(mon, feed, clock, "896121", path(33.0, 43.0, self.TRACKS, [37000] * len(self.TRACKS)),
            flight=callsign, r="A6-EUA")
        return [a for a in alerts if a["kind"] == "OFF_COURSE"]

    def test_flight_turning_away_from_its_destination(self):
        hits = self.run_track("UAE121")
        self.assertTrue(hits)
        self.assertIn("bound for LTFM", hits[0]["message"])

    def test_route_stored_the_wrong_way_round_is_learned_from_the_flight(self):
        hits = self.run_track("UAE122")       # listed LTFM-OMDB, flown toward Istanbul
        self.assertTrue(hits)
        self.assertIn("bound for LTFM", hits[0]["message"])

    def test_no_alert_while_flying_the_route(self):
        mon, feed, clock, alerts = with_routes()
        fly(mon, feed, clock, "896121", path(33.0, 43.0, [315] * 60, [37000] * 60),
            flight="UAE121", r="A6-EUA")
        self.assertNotIn("OFF_COURSE", kinds(alerts))


def alert_rec(kind, message, minute=0, hexid="8965d1", priority=None, **kw):
    rec = {"time": f"2026-09-30T05:{minute:02d}:00Z", "kind": kind, "message": message,
           "priority": priority or fw.PRIORITY.get(kind, 3), "hex": hexid, "callsign": "FDB1073",
           "aircraft": "FDB1073 / FZ1073 / A6-FKF / B38M", "flight": "FZ1073", "reg": "A6-FKF",
           "type": "B38M", "route_text": "Dubai (DXB) → TLV", "alt": 15000, "dist_nm": 163.0,
           "bearing": 120, "near_airport": "AMM", "near_nm": 70.0,
           "links": {"live_adsbx": f"https://globe.adsbexchange.com/?icao={hexid}",
                     "replay_adsbx": f"https://globe.adsbexchange.com/?icao={hexid}&showTrace=2026-09-30",
                     "fr24_flight": "https://www.flightradar24.com/data/flights/fz1073"}}
    rec.update(kw)
    return rec


class AnnouncerTests(unittest.TestCase):
    def make(self, *argv):
        args = fw.parse_args(["--jsonl", "", "--posts", "", "--turn-zones", "", *argv])
        printed = []
        return fw.Announcer(args, out=printed.append), printed

    def test_post_reads_like_a_social_media_post(self):
        ann, printed = self.make()
        post = ann.announce(alert_rec("EMERGENCY", "squawk 7500 (HIJACK)"))
        self.assertTrue(post["text"].startswith("🚨 Hijack code 7500: FZ1073 (A6-FKF, B38M) Dubai (DXB) → TLV"))
        self.assertIn("FL150, 163 nm SE of TLV", post["text"])
        self.assertIn("08:00 IDT", post["text"])  # Israel time
        links = [s for s in post["segments"] if "link" in s]
        self.assertEqual([s["link"] for s in links], ["Live", "Replay", "FR24"])
        self.assertTrue(all(set(s) <= {"text", "link", "url"} for s in post["segments"]))  # TextBuilder-ready
        self.assertEqual(len(printed), 1)

    def test_post_links_our_map_page_first(self):
        t = fw.Track("8965d1", callsign="FDB1073",
                     sched=fw.SchedFlight("FZ1073", "ARR", "DXB", "DUBAI", "2026-09-30 09:24", "FINAL"))
        links = fw.external_links(t, 1790000000, fw.VIEWER_URL)
        self.assertEqual(links["map"], fw.VIEWER_URL + "?hex=8965d1&f=FZ1073&d=A")
        self.assertEqual(fw.external_links(fw.Track("738bed"), 1790000000, fw.VIEWER_URL)["map"],
                         fw.VIEWER_URL + "?hex=738bed")              # not on the board: the aircraft alone
        self.assertNotIn("map", fw.external_links(t, 1790000000, ""))  # --viewer-url ''
        ann, _ = self.make()
        rec = alert_rec("TOWARD_ISRAEL", "not bound for Israel " + "x" * 400)
        rec["links"] = {"map": links["map"], **rec["links"]}
        post = ann.announce(rec)
        self.assertEqual([s["link"] for s in post["segments"] if "link" in s], ["Map", "Live", "Replay", "FR24"])
        self.assertLessEqual(len(post["text"]), fw.Announcer.LIMIT)

    def test_post_names_airline_military_and_unknown_route(self):
        ann, _ = self.make()
        post = ann.announce(alert_rec("EMERGENCY", "squawk 7700 (GENERAL EMERGENCY)", airline="Flydubai"))
        self.assertIn("FZ1073 Flydubai (A6-FKF, B38M) Dubai (DXB) → TLV", post["text"])
        post = ann.announce(alert_rec("POSITION_JUMP", "14.4 nm in 38s (~1373 kt) - possible GPS spoofing",
                                      hexid="738bed", callsign="SHUFL", flight=None, reg="312", type="B762",
                                      route_text=None, military=True))
        self.assertIn("SHUFL (military, 312, B762), route unknown - FL150", post["text"])

    def test_quiet_console_shows_one_row_per_alert(self):
        args = fw.parse_args(["--jsonl", "", "--posts", "", "--turn-zones", ""])
        rows, posts = [], []
        n = fw.Notifier(args, None, fw.Announcer(args, out=posts.append), console=rows.append)
        n.send(alert_rec("EMERGENCY", "squawk 7700 (GENERAL EMERGENCY)", airline="Flydubai",
                         remote=True, traffic="ARR", route=None))
        self.assertEqual(rows, ["08:00:00  FZ1073 Flydubai  Dubai (DXB) → TLV  EMERGENCY"])
        self.assertEqual(fw.Notifier.row(alert_rec("POSITION_JUMP", "x", callsign="SHUFL", flight=None,
                                                    route_text=None, military=True)),
                         "08:00:00  SHUFL (military)  POSITION_JUMP")
        quiet = fw.Announcer(args)  # without -v the feed goes to posts.jsonl only
        self.assertIsNotNone(quiet.announce(alert_rec("EMERGENCY", "squawk 7700 (GENERAL EMERGENCY)")))

    def test_long_details_are_cut_to_the_limit(self):
        ann, _ = self.make()
        post = ann.announce(alert_rec("TOWARD_ISRAEL", "not bound for Israel " + "x" * 400))
        self.assertLessEqual(len(post["text"]), fw.Announcer.LIMIT)
        self.assertIn("…", post["text"])

    def test_one_thread_per_flight_and_hijack_code_starts_a_new_post(self):
        ann, _ = self.make()
        first = ann.announce(alert_rec("VERTICAL_RATE", "DESCENT -19456 ft/min, -26.1 deg at 32325 ft", 22))
        turn = ann.announce(alert_rec("COURSE_CHANGE", "track 298 -> 213 deg (85 deg in 127s) at 15000 ft", 25))
        self.assertIsNone(first["reply_to"])
        self.assertEqual((turn["root"], turn["reply_to"]), (first["id"], first["id"]))
        self.assertNotIn("A6-FKF", turn["text"])  # replies don't repeat the identification
        hijack = ann.announce(alert_rec("EMERGENCY", "squawk 7500 (HIJACK)", 35))
        self.assertIsNone(hijack["reply_to"], "a hijack code is a top-level post, not a reply")
        later = ann.announce(alert_rec("OFF_COURSE", "TLV arrival heading 104 deg, 169 deg away from TLV", 45))
        self.assertEqual(later["root"], hijack["id"])
        other = ann.announce(alert_rec("LOST_CONTACT", "silent 64s; last 9125 ft, track 308 deg", 46, hexid="748051"))
        self.assertIsNone(other["reply_to"])

    def test_contact_restored_only_as_a_reply(self):
        ann, _ = self.make()
        self.assertIsNone(ann.announce(alert_rec("CONTACT_RESTORED", "heard again at 16175 ft")))
        ann.announce(alert_rec("LOST_CONTACT", "silent 64s; last 9125 ft, track 308 deg", 10))
        self.assertIsNotNone(ann.announce(alert_rec("CONTACT_RESTORED", "heard again at 16175 ft", 14)))

    def test_hourly_cap_never_holds_back_priority_5(self):
        ann, _ = self.make("--announce-max-per-hour", "2")
        for i in range(3):
            ann.announce(alert_rec("SHARP_TURN", "8 -> 90 deg in 28s at 356 kt (~53 deg bank)", i, hexid=f"a{i}"))
        self.assertEqual(ann.count, 2)
        self.assertIsNotNone(ann.announce(alert_rec("EMERGENCY", "squawk 7700 (GENERAL EMERGENCY)", 5)))

    def test_posts_are_written_as_json_lines(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "posts.jsonl")
            ann, _ = self.make("--posts", path)
            ann.announce(alert_rec("EMERGENCY", "squawk 7700 (GENERAL EMERGENCY)"))
            with open(path, encoding="utf-8") as f:
                rows = [json.loads(line) for line in f]
        self.assertEqual(rows[0]["kind"], "EMERGENCY")
        self.assertIn("segments", rows[0])

    def test_monitor_alerts_reach_the_feed(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        printed = []
        mon.notifier = fw.Notifier(mon.args, mon.http, fw.Announcer(mon.args, out=printed.append),
                                  console=lambda text: None)
        mon.args.posts = ""
        feed.local = {"738001"}
        feed.aircraft["738001"] = airborne(32.5, 34.5, 30000, squawk="7700")
        step(mon, clock)
        self.assertEqual(len(printed), 1)
        self.assertIn("Emergency code 7700", printed[0])
        self.assertIn("LY315", printed[0])  # callsign ELY315 -> IATA flight number


if __name__ == "__main__":
    unittest.main()
