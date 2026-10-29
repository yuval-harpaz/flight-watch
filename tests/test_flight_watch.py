"""Offline simulation tests: a fake feed replaces the network, a fake clock replaces time.

Run with:  python -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import flight_watch as fw  # noqa: E402

for _name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_CHANNEL_ID", "TELEGRAM_SKIP"):
    os.environ.pop(_name, None)  # the owner's shell has them: tests must not use the real bot

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
        self.refused = set()      # path prefixes answered with 403 (adsb.lol's browser check, 18 Oct 2026)
        self.unseen_by = {}       # host -> hexes that feed does not have (coverage differs between feeds)
        self.mil = set()          # hexes in adsb.fi's military list (/v2/mil)
        self.route_status = 200   # standing-data host: 200 serves self.files, else this status
        self.files = {}           # standing-data path ("routes/schema-01/W/WZZ-3.csv") -> CSV text
        self.headers = {}
        self.learned = []         # (json body, Authorization header) of POSTs to the relay's /learn
        self.traces = {}          # hex -> rows of its recent trace, absolute times (see trace_rows)
        self.bsky = []            # AeroSignals' posts (Bluesky getAuthorFeed "feed" items)

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
        if any(path.startswith(p) for p in self.refused):
            return FakeResponse(403, "<title>Verification</title>", url=url)
        now_ms = self.clock() * 1000
        unseen = self.unseen_by.get(u.netloc, set())
        if "/point/" in path or "/v3/lat/" in path:
            return FakeResponse(200, {"now": now_ms, "ac": [self.ac(h) for h in sorted(self.local - unseen)]})
        if path.endswith("/v2/mil"):
            return FakeResponse(200, {"now": now_ms, "ac": [{"hex": h} for h in sorted(self.mil)]})
        if "/hex/" in path:
            ids = path.rsplit("/", 1)[1].split(",")
            return FakeResponse(200, {"now": now_ms, "ac": [self.ac(h) for h in ids if h in self.aircraft]})
        if "/callsign/" in path:
            ids = set(path.rsplit("/", 1)[1].split(","))
            return FakeResponse(200, {"now": now_ms, "ac": [
                self.ac(h) for h, a in self.aircraft.items() if a.get("flight", "").strip() in ids]})
        if path.startswith("/data/traces/"):  # readsb trace_recent_<hex>.json: what the clock has reached
            hexid = path.rsplit("_", 1)[1].split(".")[0]
            rows = [r for r in self.traces.get(hexid, []) if r[0] <= self.clock()]
            if not rows:
                return FakeResponse(404, url=url)
            return FakeResponse(200, {"icao": hexid, "timestamp": rows[0][0],
                                      "trace": [[r[0] - rows[0][0], *r[1:]] for r in rows]}, url=url)
        if path.endswith("/app.bsky.feed.getAuthorFeed"):
            return FakeResponse(200, {"feed": self.bsky}, url=url)
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
    args = fw.parse_args(["--jsonl", "", "--no-schedule", "--turn-zones", "", "--fallback-provider", "",
                          "--remote-providers", "adsb.lol",  # one feed unless a test asks for more
                          "--aerosignals-interval", "0",  # Bluesky: AeroSignalsTests switch it on
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
        t = mon.tracks["738001"]  # silence alone: followed (why is kept for the log), but not hot
        self.assertTrue(t.followed)
        self.assertEqual(t.follow_why, "LOST_CONTACT")
        self.assertFalse(mon.hot(t))
        self.assertEqual((mon.stats["hot"], mon.stats["followed_lost"]), (0, 1))
        t.lost_alerted = False
        t.alerted["VERTICAL_RATE"] = (t.last.t, 1)   # with another anomaly it is a pattern: hot
        self.assertTrue(mon.alert(t, "LOST_CONTACT", "silent again", force=True))
        self.assertIn("[also: VERTICAL_RATE]", alerts[-1]["message"])
        self.assertGreater(t.hot_until, t.last.t)

    def test_kind_telegram_skips_makes_no_flight_hot(self):
        # RJ302 25 Oct 2026: a logged TOWARD_ISRAEL made it hot, and each leg of its hold went out as a reversal
        mon, feed, clock, alerts = make(["--no-routes"])
        feed.local = {"738001"}
        for i in range(3):
            feed.aircraft["738001"] = airborne(32.6, 34.0 + i * 0.03, 20000, flight="RJA302")
            step(mon, clock)
        t = mon.tracks["738001"]
        self.assertTrue(mon.alert(t, "TOWARD_ISRAEL", "turned toward Israel", force=True))
        self.assertTrue(t.followed)
        self.assertFalse(mon.hot(t))
        self.assertTrue(mon.alert(t, "COURSE_CHANGE", "reversal", force=True))
        self.assertTrue(mon.hot(t))

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

    def test_board_alerts_ride_on_the_share(self):
        """The flight board's warnings go with the callsign share (no extra request, no extra KV write): only the
        kinds Telegram gets, sent once; when the POST fails they wait for the next one."""
        from unittest import mock
        with mock.patch.dict(os.environ, {"RELAY_TOKEN": "s3cret"}):
            mon, feed, clock, _ = make(["--share-interval", "60"])
            send = lambda rec: fw.Notifier.send(mon.notifier, rec)          # make() records instead of sending
            send(alert_rec("EMERGENCY", "squawk 7700 (GENERAL EMERGENCY)", remote=False, traffic="ARR", route=None))
            send(alert_rec("LOST_CONTACT", "silent 90s, last track 120 deg", remote=False, traffic="ARR", route=None))
            fly(mon, feed, clock, "738062", path(32.3, 34.5, [180] * 3, [9000] * 3), flight="ELY5064")
            body = feed.learned[0][0]
            self.assertEqual([a[3] for a in body["_alerts"]], ["EMERGENCY"])   # LOST_CONTACT: a Telegram-skip kind
            self.assertEqual(body["_alerts"][0][:3], ["8965d1", "FZ1073", "FDB1073"])
            self.assertIn("ELY5064", body)                                       # the callsigns as before
            self.assertEqual(mon.notifier.board_alerts, [])                      # sent: not again
            feed.limited.add("/learn")
            send(alert_rec("SHARP_TURN", "x", minute=5, remote=False, traffic="ARR", route=None))
            for _ in range(8):
                step(mon, clock)
            self.assertEqual(len(mon.notifier.board_alerts), 1)                  # the relay refused: kept

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


def trace_rows(t0, states, dt=10, squawk=None):
    """path() states as readsb trace rows from time t0, `dt` s apart; the squawk in the details."""
    return [[t0 + i * dt, st["lat"], st["lon"], st["alt_baro"], st["gs"], st["track"], 0, st["baro_rate"],
             {"squawk": squawk} if squawk and i == 0 else None, "adsb_icao"] for i, st in enumerate(states)]


class RecentTraceTests(unittest.TestCase):
    """The feed's recent trace (TRACE_RECENT) fills what our polls missed: 4XDAN, 10 Oct 2026, dives at
    08:55 and 10:17 IDT went unalerted while the API refused half the polls (HTTP 429)."""

    def dive_after_a_gap(self, argv=(), gap=100):
        """Polled on its jump run, then 100 s of refused polls, then one poll mid-dive (-17 deg): no
        report of ours 15-90 s before it to back the rate."""
        mon, feed, clock, alerts = make(["--no-routes", *argv])
        run = path(31.36, 35.387, [185] * 7, [11800 - 40 * i for i in range(7)], gs=55, dt=10)
        lat, lon = fw.destination(run[-1]["lat"], run[-1]["lon"], 185, 55 * 10 / 3600)
        dive = path(lat, lon, [190] * 47, [11500 - 150 * i for i in range(47)], gs=145, dt=2)
        t0 = clock()
        feed.traces["739215"] = trace_rows(t0, run) + trace_rows(t0 + 70, dive, dt=2)
        ident = dict(flight="4XDAN", r="4X-DAN", t="P750", category="A1")
        fly(mon, feed, clock, "739215", run[:6], **ident)   # polls up to t0+50 (clock: t0+60)
        clock.t += gap                                       # refused polls, t0+60 .. t0+150
        fly(mon, feed, clock, "739215", [dive[(gap - 10) // 2]], **ident)  # e.g. t0+160: -4,500 ft/min, 145 kt
        return alerts

    def test_missing_altitude_history_is_read_from_the_trace(self):
        alerts = self.dive_after_a_gap()
        self.assertEqual(kinds(alerts), ["VERTICAL_RATE"])
        self.assertRegex(alerts[0]["message"], r"DESCENT -4500 ft/min, -1\d\.\d deg .*\(history: -4\d{3} ft/min\)")

    def test_without_the_trace_it_waits_for_history(self):
        self.assertEqual(kinds(self.dive_after_a_gap(["--trace-url", ""])), [])

    def test_history_may_be_up_to_90_s_old(self):
        """No trace, last report 60 s old (08:55 IDT: the reports 30-45 s back were missing)."""
        self.assertEqual(kinds(self.dive_after_a_gap(["--trace-url", ""], gap=50)), ["VERTICAL_RATE"])

    def refused_after_the_jump_run(self, argv=()):
        """Polled on its jump run; then the API answers only 429 while it dives (in the trace)."""
        mon, feed, clock, alerts = make(["--no-routes", *argv])
        run = path(31.36, 35.387, [185] * 7, [11800 - 40 * i for i in range(7)], gs=55, dt=10)
        lat, lon = fw.destination(run[-1]["lat"], run[-1]["lon"], 185, 55 * 10 / 3600)
        dive = path(lat, lon, [190] * 30, [11500 - 150 * i for i in range(30)], gs=145, dt=2)
        t0 = clock()
        feed.traces["739215"] = trace_rows(t0, run) + trace_rows(t0 + 70, dive, dt=2)
        fly(mon, feed, clock, "739215", run[:6], flight="4XDAN", r="4X-DAN", t="P750", category="A1")
        feed.limited = {"/v2/point"}
        for _ in range(7):  # t0+60 .. t0+120: refused or skipped (cooling down)
            step(mon, clock)
        return alerts, t0

    def test_fallback_reads_the_watch_list_while_the_api_refuses_us(self):
        alerts, t0 = self.refused_after_the_jump_run()
        self.assertEqual(kinds(alerts), ["VERTICAL_RATE"])
        self.assertTrue(alerts[0]["from_trace"])
        when = datetime.fromisoformat(alerts[0]["time"].replace("Z", "+00:00")).timestamp()
        self.assertLessEqual(when, t0 + 100)  # data time, mid-dive, before the API answers again

    def test_no_fallback_without_the_trace(self):
        alerts, _ = self.refused_after_the_jump_run(["--trace-url", ""])
        self.assertEqual(kinds(alerts), [])

    def test_hot_flight_squawk_during_refusals(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        states = path(32.4, 34.4, [200] * 12, [20000] * 12, gs=300)
        t0 = clock()
        feed.traces["738001"] = trace_rows(t0, states[:6]) + trace_rows(t0 + 60, states[6:], squawk="7700")
        fly(mon, feed, clock, "738001", states[:6], flight="ELY315")
        mon.tracks["738001"].hot_until = clock() + 3600  # it alerted before
        feed.limited = {"/v2/point"}
        for _ in range(4):
            step(mon, clock)
        self.assertEqual(kinds(alerts), ["EMERGENCY"])
        self.assertIn("missed by our polls", alerts[0]["message"])

    def vanish(self, trace_more):
        """Cruising locally, then gone from the API; its trace has 30 s more (squawk 7700) or not."""
        mon, feed, clock, alerts = make(["--no-routes"])
        feed.local.add("999999")
        feed.aircraft["999999"] = airborne(31.0, 34.0, 20000, flight="KEEP1")
        states = path(32.4, 34.4, [200] * 10, [20000] * 10, gs=300)
        t0 = clock()
        feed.traces["738001"] = trace_rows(t0, states[:5]) + (trace_rows(t0 + 50, states[5:8], squawk="7700")
                                                              if trace_more else [])
        fly(mon, feed, clock, "738001", states[:5], flight="ELY315")
        feed.local.discard("738001")
        del feed.aircraft["738001"]
        for _ in range(30):
            step(mon, clock)
        return alerts

    def test_reports_missed_by_our_polls_go_through_the_checks(self):
        alerts = self.vanish(trace_more=True)
        self.assertEqual(kinds(alerts)[:2], ["EMERGENCY", "LOST_CONTACT"])
        self.assertTrue(alerts[0]["from_trace"])
        self.assertIn("missed by our polls", alerts[0]["message"])
        self.assertEqual(alerts[0]["time"], fw.time.strftime("%Y-%m-%dT%H:%M:%SZ", fw.time.gmtime(Clock().t + 50)))
        self.assertIn(f"{alerts[1]['lat']:.4f}", f"{fw.destination(32.4, 34.4, 200, 300 * 70 / 3600)[0]:.4f}")

    def test_silence_confirmed_by_the_trace(self):
        alerts = self.vanish(trace_more=False)
        self.assertEqual(kinds(alerts), ["LOST_CONTACT"])
        self.assertIn("the feed's own trace ends there too", alerts[0]["message"])
        self.assertFalse(alerts[0]["from_trace"])


class SkydiveTests(unittest.TestCase):
    def test_jump_plane_dive_is_described(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        fly(mon, feed, clock, "739215", lift(), flight="4XDAN", r="4X-DAN", t="P750", category="A1")
        self.assertEqual(kinds(alerts), ["VERTICAL_RATE"])
        self.assertIn("skydiving pattern, PAC 750XL: 55 kt at 11800 ft", alerts[0]["message"])
        self.assertEqual(alerts[0]["skydive"]["lift"], 1)

    def test_rides_are_alerted_once_then_only_an_emergency(self):
        """4XDAN, 16 Oct 2026: 3 lifts gave 13 alerts (dives, silences low, heard again)."""
        mon, feed, clock, alerts = make(["--no-routes"])
        ident = dict(flight="4XDAN", r="4X-DAN", t="P750", category="A1")
        end = lift()[-1]
        climb = path(end["lat"], end["lon"], [5] * 60, [8500 + 55 * i for i in range(60)], gs=100)
        second = lift()
        fly(mon, feed, clock, "739215", lift() + climb + second, **ident)
        fly(mon, feed, clock, "739215", [{**second[-1], "squawk": "7700"}] * 2, **ident)
        self.assertEqual(kinds(alerts), ["VERTICAL_RATE", "EMERGENCY"])
        self.assertIn("probably a skydiving ride", alerts[0]["message"])
        self.assertEqual(mon.tracks["739215"].skydive["lift"], 2)

    def test_ride_announced_before_a_restart_is_not_announced_again(self):
        ident = dict(flight="4XDAN", r="4X-DAN", t="P750", category="A1")
        d = tempfile.mkdtemp()
        f = os.path.join(d, "alerts.jsonl")
        mon, feed, clock, alerts = make(["--no-routes", "--jsonl", f])
        fly(mon, feed, clock, "739215", lift(), **ident)
        self.assertEqual(kinds(alerts), ["VERTICAL_RATE"])
        with open(f, "w", encoding="utf-8") as out:  # what Notifier.send appends (replaced in these tests)
            out.write(json.dumps(alerts[0]) + "\n")
        mon, feed, clock2, alerts = make(["--no-routes", "--jsonl", f])  # restarted 20 min later
        clock2.t = clock.t + 1200
        fly(mon, feed, clock2, "739215", lift(), **ident)
        self.assertEqual(kinds(alerts), [])

    def test_faster_jump_run_of_a_jump_plane(self):
        """4XDAN, 17 Oct 2026: a jump run at 87-104 kt, then -4,800 ft/min: a ride, said so on Telegram."""
        mon, feed, clock, alerts = make(["--no-routes"])
        fly(mon, feed, clock, "739215", lift(gs_run=95), flight="4XDAN", r="4X-DAN", t="P750", category="A1")
        self.assertEqual(kinds(alerts), ["VERTICAL_RATE"])
        self.assertIn("probably a skydiving ride", alerts[0]["message"])
        text = mon.notifier.announcer.compose(alerts[0], reply=False)
        self.assertIn("Probably a skydiving flight (sport parachuting, PAC 750XL)", "".join(x.get("text", "") for x in text))

    def test_a_fall_beyond_vrate_on_a_ride_is_alerted(self):
        """A spin, not a jump-plane dive: -12,000 ft/min after the ride's first alert."""
        mon, feed, clock, alerts = make(["--no-routes"])
        ident = dict(flight="4XDAN", r="4X-DAN", t="P750", category="A1")
        first = lift()
        end = first[-1]
        fall = path(end["lat"], end["lon"], [190] * 4, [end["alt_baro"] - 2000 * i for i in range(1, 5)], gs=150)
        fall = [{**st, "baro_rate": -12000} for st in fall]
        fly(mon, feed, clock, "739215", first + fall, **ident)
        self.assertEqual(kinds(alerts), ["VERTICAL_RATE", "VERTICAL_RATE"])
        self.assertIn("-12000", alerts[1]["message"])

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

    def test_speed_its_positions_do_not_back_is_no_sharp_turn(self):
        """IZ98 (A321 LZ-EAM, HER -> TLV, 25 Oct 2026) at FL123 in GPS jamming: "128 -> 59 deg in 33 s at 675 kt
        (~53 deg bank)", NIC-0 positions next. Its positions moved at ~320 kt: ~34 deg, a normal turn."""
        mon, feed, clock, alerts = make(["--no-routes"])
        states = [{**s, "gs": 300} for s in path(32.4, 33.6, [128] * 6 + [105, 82, 59] + [59] * 5, [12350] * 14,
                                                 gs=320)]
        states[8] = {**states[8], "gs": 675}
        fly(mon, feed, clock, "452199", states, flight="AIZ098", r="LZ-EAM", t="A321")
        self.assertNotIn("SHARP_TURN", kinds(alerts))


    def test_turn_rate_its_positions_do_not_back_is_no_sharp_turn(self):
        """LY316 (B789, FL146, 26 Oct 2026): "7 -> 116 deg in 28 s at 316 kt (~48 deg bank)" from its reported tracks;
        ADS-B Exchange's trace has a steady 23-30 deg turn of 60 s. Here the track lags, then catches up; the
        positions turn at 2 deg/s (~30 deg). The same turn at 4 deg/s in both is a sharp turn."""
        got = {}
        for case, step in (("lagging track", 20), ("sharp", 40)):
            mon, feed, clock, alerts = make(["--no-routes"])
            heads = [7] * 4 + [7 + step * (i + 1) for i in range(5)] + [7 + step * 5] * 4
            states = path(32.4, 33.9, heads, [14600] * len(heads), gs=316)
            if case == "lagging track":
                for i in (4, 5, 6):
                    states[i] = {**states[i], "track": 7}
            fly(mon, feed, clock, "7380c4", states, flight="ELY316", r="4X-EDE", t="B789")
            got[case] = "SHARP_TURN" in kinds(alerts)
        self.assertEqual(got, {"lagging track": False, "sharp": True})

    def test_zig_zag_back_onto_its_course_is_no_sharp_turn(self):
        """4X-CZF (H25B, FL240, 27 Oct 2026): straight on 349 deg at 388 kt, it reported 58 deg at 551 kt for 22 s, the
        positions running off ENE, then 349 again where it would have been: "~48 deg bank". A real turn holds."""
        got = {}
        for case in ("glitch", "turned"):
            mon, feed, clock, alerts = make(["--no-routes"])
            if case == "glitch":
                states = path(32.33, 34.42, [349] * 14, [24000] * 14, gs=388)
                for i in (6, 7, 8):
                    lat, lon = fw.destination(states[5]["lat"], states[5]["lon"], 58, 551 * (i - 5) * 10 / 3600)
                    states[i] = {**states[i], "lat": lat, "lon": lon, "track": 58, "gs": 551}
            else:
                states = path(32.33, 34.42, [349] * 6 + [24, 58] + [58] * 6, [24000] * 14, gs=388)
            fly(mon, feed, clock, "73934e", states, flight="4XCZF", r="4X-CZF", t="H25B")
            got[case] = kinds(alerts).count("SHARP_TURN")
        self.assertEqual(got, {"glitch": 0, "turned": 1})

    def test_a_sharp_turn_alone_makes_no_flight_hot(self):
        """VPCMV out of Amman, LY316 toward TLV (26 Oct 2026): hot from a SHARP_TURN, their routine turns went out as
        course reversals too."""
        mon, feed, clock, alerts = make(["--no-routes"])
        fly(mon, feed, clock, "4002c5", path(31.9, 35.9, [250] * 3, [7000] * 3), flight="VPCMV", r="VP-CMV", t="GLEX")
        t = mon.tracks["4002c5"]
        self.assertTrue(mon.alert(t, "SHARP_TURN", "339 -> 250 deg", force=True))
        self.assertFalse(mon.hot(t))

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


class EmergencyStatusTests(unittest.TestCase):
    def test_lifeguard_is_a_medical_flight_not_an_emergency(self):
        """NJE417A set ADS-B status "lifeguard" (a medical / priority flight) over Germany: "Emergency status" on
        Telegram from one report (25 Oct 2026). Now MEDICAL_FLIGHT once, when held 20 s; a lone report only logged.
        The other statuses too, as EMERGENCY: ELY323 and BEL1UG "nordo" from one report each over Bavaria at squawk
        1000, nothing in ADS-B Exchange's trace (26 Oct 2026)."""
        got = {}
        for case, reports in (("held", 8), ("lone", 1), ("nordo", 1), ("nordo held", 8)):
            mon, feed, clock, alerts = make()
            em = "nordo" if case.startswith("nordo") else "lifeguard"
            states = [dict(st, emergency=em if i < reports else "none")
                      for i, st in enumerate(path(32.3, 34.3, [300] * 10, [35000] * 10))]
            fly(mon, feed, clock, "490d0b", states, flight="NJE417A", r="CS-CHK", t="CL35")
            got[case] = kinds(alerts, "490d0b")
        self.assertEqual(got, {"held": ["MEDICAL_FLIGHT"], "lone": [], "nordo": [], "nordo held": ["EMERGENCY"]})


class RestartStateTests(unittest.TestCase):
    """The restart of 24 Oct 2026 21:45 IDT: KL463 on the ground at Bucharest (its stop) climbed out after it and its
    departure turn was a "course reversal"; BZ131 landed at Rome, its origin, and was a DIVERSION (where it came from,
    seen before the restart, was lost). --state keeps each aircraft's leg."""

    def test_leg_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path_ = os.path.join(tmp, "state.json")
            mon, feed, clock, alerts = make(["--state", path_])
            fly(mon, feed, clock, "4d21e6", path(41.0, 13.0, [300] * 6, [20000] * 6),
                flight="BBG131", r="9H-KAR", t="B738")
            t = mon.tracks["4d21e6"]
            t.came_from, t.orig_first, t.followed = True, 120.0, True
            t.alerted["COURSE_CHANGE"] = (clock(), 80)
            mon.save_state()
            mon2, feed2, clock2, alerts2 = make(["--state", path_])
            clock2.t = clock.t + 300
            mon2.load_state()
            t2 = mon2.tracks["4d21e6"]
            self.assertEqual((t2.callsign, t2.came_from, t2.orig_first, t2.followed, t2.lost),
                             ("BBG131", True, 120.0, True, True))
            self.assertEqual(t2.alerted["COURSE_CHANGE"], t.alerted["COURSE_CHANGE"])
            self.assertEqual(t2.last, t.last)
            last = t.last  # heard again: no lost contact nor "restored" for the restart's silence
            fly(mon2, feed2, clock2, "4d21e6", path(last.lat, last.lon, [300] * 6, [20000] * 6),
                flight="BBG131", r="9H-KAR", t="B738")
            self.assertEqual(kinds(alerts2, "4d21e6"), [])
            clock2.t += fw.STATE_MAX_AGE_S + 3600  # too old: not read
            mon3 = make(["--state", path_])[0]
            mon3.clock = mon3.wall = clock2
            mon3.load_state()
            self.assertNotIn("4d21e6", mon3.tracks)

    def test_take_off_after_a_gap_is_its_departure_routing(self):
        """On the ground at Bucharest, then (after a restart or refused polls) first heard again climbing 15 nm out."""
        mon, feed, clock, alerts = make()
        ident = dict(flight="KLM463", r="PH-BXR", t="B739")
        fly(mon, feed, clock, "48418c", [dict(lat=44.5711, lon=26.0850, alt_baro="ground", gs=5, track=90)] * 3,
            **ident)
        clock.t += 1800
        fly(mon, feed, clock, "48418c", path(44.70, 25.80, [150] * 3, [12000, 12400, 12800]), **ident)
        self.assertEqual(mon.tracks["48418c"].takeoff_pos, (44.5711, 26.0850))
        self.assertEqual(mon.tracks["48418c"].takeoff, 0.0)  # when, unknown: no EARLY_DESCENT from it


class LostOnlyFollowTests(unittest.TestCase):
    def test_followed_for_a_lost_contact_only_ends_out_of_the_circle(self):
        """QR1436 (no route) lost contact over Sinai, stayed followed, and its approach turn at Doha 2 h later was a
        "course reversal 979 nm E of TLV" (25 Oct 2026). Followed for another alert: still followed."""
        got = {}
        for kinds_ in (("LOST_CONTACT", "CONTACT_RESTORED"), ("LOST_CONTACT", "COURSE_CHANGE")):
            mon, feed, clock, alerts = make(["--no-routes"])
            ident = dict(flight="QTR1436", r="A7-ALA", t="A359")
            fly(mon, feed, clock, "06a0a1", path(31.0, 33.0, [250] * 3, [37000] * 3, gs=480), **ident)
            t = mon.tracks["06a0a1"]
            t.followed, t.follow_why = True, "LOST_CONTACT"
            t.alerted = {k: (clock() - 600, 0) for k in kinds_}
            last = t.last
            feed.local.discard("06a0a1")
            fly(mon, feed, clock, "06a0a1", path(last.lat, last.lon, [250] * 100, [37000] * 100, gs=480),
                local=False, **ident)
            got[kinds_[1]] = (fw.haversine_nm(t.last.lat, t.last.lon, 32.0114, 34.8867) > 150, t.followed)
        self.assertEqual(got, {"CONTACT_RESTORED": (True, False), "COURSE_CHANGE": (True, True)})


class LifeguardNoteTests(unittest.TestCase):
    def test_lone_lifeguard_report_is_noted_on_a_later_alert(self):
        """The owner: a lone lifeguard report need not alert, but a later alert on the flight (a diversion) must say
        it, with its time. Here a steep descent 2 min after one report of the status; nordo the same."""
        for em, start in (("lifeguard", "⚕️ ADS-B lifeguard (medical) status at "),
                          ("nordo", "ADS-B nordo (radio failure) status, one unconfirmed report at ")):
            mon, feed, clock, alerts = make()
            ident = dict(flight="NJE417A", r="CS-CHK", t="CL35")
            states = [dict(st, emergency=em if i == 0 else "none")
                      for i, st in enumerate(path(32.3, 34.3, [300] * 12, [35000] * 12))]
            fly(mon, feed, clock, "490d0b", states, **ident)
            at = fw.time.strftime("%Y-%m-%dT%H:%M:%SZ", fw.time.gmtime(mon.tracks["490d0b"].status_at))
            last = states[-1]
            fly(mon, feed, clock, "490d0b", path(last["lat"], last["lon"], [300] * 12,
                                                 [35000 - 1500 * i for i in range(12)]), **ident)
            rec = next(r for r in alerts if r["hex"] == "490d0b")
            self.assertEqual(rec["kind"], "VERTICAL_RATE")
            self.assertIn(f"[{em} status at {at}]", rec["message"])
            text = fw.Announcer(mon.args).detail(rec)
            self.assertTrue(text.startswith(start), text)


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
    def test_turn_onto_the_destination_is_not_a_reversal(self):
        """MEA312 Beirut - Amman, 16 Oct 2026: east past Damascus, then south to Amman."""
        tracks = [200] * 12 + [200 + 10 * i for i in range(1, 12)] + [310] * 16  # over Iraq, toward Istanbul
        for callsign, expect in (("UAE121", False), ("UAE122", True)):  # OMDB-LTFM / LTFM-OMDB
            mon, feed, clock, alerts = with_routes()
            fly(mon, feed, clock, "896121", path(33.0, 43.0, tracks, [37000] * len(tracks)), flight=callsign,
                r="A6-EUA")
            self.assertEqual("COURSE_CHANGE" in kinds(alerts), expect, callsign)

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
        """Out at cruise, it turns back. First heard climbing far away: W46404 (9H-WZR, board TLV -> MXP) first heard
        after a restart climbing out near Rome at FL197, then flying on to Romania - two TURNING_BACK (26 Oct 2026):
        another leg on its callsign, not alerted."""
        got = {}
        for case in ("cruise", "climbing far away"):
            mon, feed, clock, alerts = with_board([board_row("LY", "315", "D", local_time(mon_clock(), -0.5), "LHR")])
            if case == "cruise":
                tracks = [255] * 6 + [255 + 15 * i for i in range(1, 13)] + [75] * 24
                states = path(32.45, 32.6, tracks, [33000] * len(tracks))
            else:
                states = path(41.6, 12.3, [110] * 40, [min(19700 + 1000 * i, 37000) for i in range(40)])
            fly(mon, feed, clock, "738315", states, flight="ELY315", r="4X-EDC")
            got[case] = "TURNING_BACK" in kinds(alerts)
        self.assertEqual(got, {"cruise": True, "climbing far away": False})


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

    def test_position_only_arrival_doing_that_is_not_a_diversion(self):
        """IZ598 (D-AUSC, A343, MLAT positions only) silent at 10,225 ft over Amman, then landed at TLV (27 Oct 2026)."""
        mon, feed, clock, alerts = with_board([board_row("IZ", "598", "A", local_time(mon_clock(), 0.5))])
        feed.local.add("999999")
        feed.aircraft["999999"] = airborne(31.0, 34.0, 20000, flight="KEEP1")
        states = path(31.72, 35.70, [270] * 5, [10000 - 250 * i for i in range(5)], gs=220)
        for st in states:
            st.pop("gs", None)
            st.pop("track", None)
        fly(mon, feed, clock, "3c0f0b", states, flight="AIZ598", r="D-AUSC", t="A343")
        feed.local.discard("3c0f0b")
        del feed.aircraft["3c0f0b"]
        for _ in range(12):
            step(mon, clock)
        self.assertNotIn("DIVERSION", kinds(alerts))
        self.assertNotIn("LOST_CONTACT", kinds(alerts))

    def plunge_and_vanish(self, alts, last_rate):
        mon, feed, clock, alerts = make(["--no-routes"])
        feed.local.add("999999")
        feed.aircraft["999999"] = airborne(31.0, 34.0, 20000, flight="KEEP1")
        states = path(31.72, 35.70, [90] * len(alts), alts, gs=300)  # near Amman, as above
        states[-1]["baro_rate"] = last_rate
        fly(mon, feed, clock, "740200", states, flight="RJA200", r="JY-AYC")
        feed.local.discard("740200")
        del feed.aircraft["740200"]
        for _ in range(12):
            step(mon, clock)
        return alerts

    def test_silent_falling_low_is_lost_contact_not_a_landing(self):
        """MU5735 (21 Mar 2022): last heard at 3,225 ft falling 31,000 ft/min - below --lost-min-alt."""
        alerts = self.plunge_and_vanish([12000, 9500, 7000, 4500, 2500], -12000)
        hits = [a for a in alerts if a["kind"] == "LOST_CONTACT"]
        self.assertEqual(len(hits), 1, kinds(alerts))
        self.assertIn("falling -12000 ft/min", hits[0]["message"])

    def test_one_bad_rate_at_the_end_of_a_normal_descent_is_not_a_fall(self):
        alerts = self.plunge_and_vanish([4500, 4300, 4100, 3900, 3700], -12000)
        self.assertNotIn("LOST_CONTACT", kinds(alerts))

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


class CallsignAndLegTests(unittest.TestCase):
    """12 Oct 2026: identity and leg state carried across a callsign change gave 9 false alerts."""
    TLV = (32.0114, 34.8867)
    BOARD = [("6H", "46", "A", 0.5, "ETM"), ("6H", "723", "D", -0.3, "BUD")]

    def board(self):
        return with_board([board_row(op, n, d, local_time(Clock(), h), other) for op, n, d, h, other in self.BOARD])

    def inbound(self, flip, other_aircraft):
        """ISR046 flying north to TLV from 110 nm; for `flip` polls the feed calls it ISR723."""
        mon, feed, clock, alerts = self.board()
        if other_aircraft:  # the real ISR723, climbing out to the north-west
            feed.local.add("738284")
            feed.aircraft["738284"] = {**airborne(32.6, 34.2, 24000, track=320, flight="ISR723"), "r": "4X-ABF"}
        states = path(30.2, 35.0, [5] * 60, [16000] * 60, gs=400)
        fly(mon, feed, clock, "738285", states[:30], flight="ISR046", r="4X-ABG")
        fly(mon, feed, clock, "738285", states[30:30 + flip], flight="ISR723", r="4X-ABG")
        fly(mon, feed, clock, "738285", states[30 + flip:], flight="ISR046", r="4X-ABG")
        return mon, alerts

    def test_callsign_of_another_aircraft_is_refused(self):
        mon, alerts = self.inbound(flip=20, other_aircraft=True)
        self.assertEqual(kinds(alerts, "738285"), [])
        self.assertEqual(mon.tracks["738285"].callsign, "ISR046")

    def test_short_wrong_callsign_is_not_taken_up(self):
        mon, alerts = self.inbound(flip=6, other_aircraft=False)
        self.assertEqual(kinds(alerts, "738285"), [])
        self.assertEqual(mon.tracks["738285"].sched.flight, "6H46")

    def test_lasting_change_is_taken_up_as_a_new_leg(self):
        mon, feed, clock, alerts = self.board()
        fly(mon, feed, clock, "738285", path(31.0, 34.9, [0] * 10, [16000] * 10, gs=300), flight="ISR046", r="4X-ABG")
        t = mon.tracks["738285"]
        self.assertTrue(t.was_airborne)
        fly(mon, feed, clock, "738285", path(31.2, 34.9, [0] * 16, [16000] * 16, gs=300), flight="ISR999", r="4X-ABG")
        self.assertEqual(t.callsign, "ISR999")
        self.assertIsNone(t.sched)  # (no longer the board's 6H46)
        self.assertGreater(t.leg_start, 0)

    def test_unseen_landing_then_the_next_flights_callsign_is_not_returned(self):
        """ELY5120 last heard at 4,025 ft 4 nm out; 2 h 20 min later on the ground as ELY5101."""
        mon, feed, clock, alerts = with_board([board_row("LY", "5120", "A", local_time(Clock(), 0.3), "KRK"),
                                               board_row("LY", "5101", "D", local_time(Clock(), 14), "WAW")])
        start = fw.destination(*self.TLV, 300, 145)
        n = 124
        fly(mon, feed, clock, "738060", path(*start, [120] * n, [30000 - 210 * i for i in range(n)]),
            flight="ELY5120", r="4X-EKM", t="B738")
        feed.local.discard("738060")
        del feed.aircraft["738060"]
        for _ in range(30):
            step(mon, clock, 60)
        for _ in range(20):
            fly(mon, feed, clock, "738060", [dict(lat=self.TLV[0], lon=self.TLV[1], alt_baro="ground", gs=3,
                                                  track=30)], dt=30, flight="ELY5101", r="4X-EKM", t="B738")
        self.assertNotIn("RETURNED", kinds(alerts, "738060"))
        self.assertEqual(mon.tracks["738060"].sched.flight, "LY5101")

    def test_departure_with_the_arrivals_board_row_is_not_off_course(self):
        """TVF3452 landed at TLV and left as TVF55BJ, still bound to the arrival's row."""
        mon, feed, clock, alerts = with_board([board_row("TO", "3452", "A", local_time(Clock(), 0.2), "ORY")])
        mon.schedule.airline_map["TO"] = "TVF"
        mon.schedule.load([board_row("TO", "3452", "A", local_time(Clock(), 0.2), "ORY")],
                          datetime.fromtimestamp(clock(), mon.schedule.tz))
        ground = [dict(lat=self.TLV[0], lon=self.TLV[1], alt_baro="ground", gs=2, track=260)]
        fly(mon, feed, clock, "39dd03", ground * 3, flight="TVF3452", r="F-HXID", t="A20N")
        self.assertEqual(mon.tracks["39dd03"].sched.flight, "TO3452")
        fly(mon, feed, clock, "39dd03", ground * 14, flight="TVF55BJ", r="F-HXID", t="A20N")
        n = 90
        fly(mon, feed, clock, "39dd03", path(*self.TLV, [300] * n, [min(36000, 2000 + 500 * i) for i in range(n)],
                                             gs=400), flight="TVF55BJ", r="F-HXID", t="A20N")
        self.assertNotIn("OFF_COURSE", kinds(alerts, "39dd03"))


class FakeLandingTests(unittest.TestCase):
    """11-12 Oct 2026: four arrivals on approach to TLV with spoofed GPS were DIVERSIONs."""
    TLV = (32.0114, 34.8867)

    def approach(self, argv=()):
        mon, feed, clock, alerts = with_board([board_row("LY", "5462", "A", local_time(Clock(), 0.2), "RHO")], argv)
        start = fw.destination(*self.TLV, 300, 45)
        n = 20
        fly(mon, feed, clock, "452144", path(*start, [120] * n, [10000 - 150 * i for i in range(n)], gs=300),
            flight="ELY5462", r="LZ-EAI")
        return mon, feed, clock, alerts

    def ground_at(self, mon, feed, clock, lat, lon, polls, gs=6, dt=10):
        fly(mon, feed, clock, "452144", [dict(lat=lat, lon=lon, alt_baro="ground", gs=gs, track=298)] * polls,
            dt=dt, flight="ELY5462", r="LZ-EAI")

    def test_ground_report_out_of_reach_is_dropped(self):
        mon, feed, clock, alerts = self.approach()
        self.ground_at(mon, feed, clock, 30.4809, 35.9801, 12)   # 108 nm away, 10 s after 28 nm out
        self.ground_at(mon, feed, clock, *self.TLV, 12)
        self.assertNotIn("DIVERSION", kinds(alerts))
        self.assertTrue(all(fw.haversine_nm(x.lat, x.lon, *self.TLV) < 50 for x in mon.tracks["452144"].samples))

    def test_on_the_ground_at_575_kt_is_dropped(self):
        mon, feed, clock, alerts = self.approach()
        self.ground_at(mon, feed, clock, 32.25, 35.05, 6, gs=575)
        self.assertNotIn("DIVERSION", kinds(alerts))

    def test_a_landing_elsewhere_is_at_an_airport(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "airports", "schema-01", "O"))
            with open(os.path.join(d, "airports", "schema-01", "O", "OJ.csv"), "w") as f:
                f.write(ROUTE_FILES["airports/schema-01/O/OJ.csv"])
            for (lat, lon), expected in (((31.9994, 36.6858), 0), ((31.7226, 35.9932), 1)):  # desert / Amman
                mon, feed, clock, alerts = self.approach(["--standing-data", d])
                mon.standing = fw.StandingData(mon.http, d)
                feed.local.discard("452144")
                del feed.aircraft["452144"]
                for _ in range(15):   # unheard for 15 min: the place is within reach
                    step(mon, clock, 60)
                self.ground_at(mon, feed, clock, lat, lon, 4, dt=30)
                self.assertEqual(kinds(alerts).count("DIVERSION"), expected, (lat, lon, kinds(alerts)))

    def test_landing_at_the_spoofers_place_is_not_a_diversion(self):
        """14 Oct 2026: the fake place was Amman airport; 10 arrivals on final "landed" or went silent there."""
        for low in (False, True):
            mon, feed, clock, alerts = self.approach()
            for i in range(3):  # others still heard: not a feed glitch
                feed.aircraft[f"77777{i}"] = dict(lat=32.3 + i / 10, lon=34.2, alt_baro=30000, gs=450, track=90,
                                                  flight=f"TST00{i}  ", r="SX-ABC", t="A320")
                feed.local.add(f"77777{i}")
            mon.spoofs.append(dict(label="spoof-x", lat=31.7171, lon=35.9995, start=clock.t, last=clock.t + 1500, track=None,
                                   alerted=True, hexes={"aaaaaa", "bbbbbb"}, ended=False))
            feed.local.discard("452144")
            del feed.aircraft["452144"]
            for _ in range(15):
                step(mon, clock, 60)
            if low:   # heard low over the place, then silent
                fly(mon, feed, clock, "452144", [dict(lat=31.7171, lon=35.9995, alt_baro=1450, gs=140, track=298,
                                                      baro_rate=-700)] * 4, dt=30, flight="ELY5462", r="LZ-EAI")
                feed.local.discard("452144")
                del feed.aircraft["452144"]
                for _ in range(48):   # (polls 10 s apart: a longer gap is our own outage)
                    step(mon, clock, 10)
            else:
                self.ground_at(mon, feed, clock, 31.7172, 35.9993, 4, dt=30)
            self.assertNotIn("DIVERSION", kinds(alerts), low)

    def test_fake_position_repeated_for_minutes_stays_out(self):
        """ISR972: NIC-0 reports 73 nm away were dropped for 3 min and then came within reach."""
        mon, feed, clock, alerts = self.approach()
        fake = dict(lat=33.0777, lon=35.5611, alt_baro=1575, gs=474, track=62, baro_rate=-600, nic=0)
        t = mon.tracks["452144"]
        t.nic_last = 8
        fly(mon, feed, clock, "452144", [fake] * 60, flight="ELY5462", r="LZ-EAI")  # 10 min
        self.assertTrue(all(fw.haversine_nm(x.lat, x.lon, *self.TLV) < 50 for x in t.samples))
        feed.local.discard("452144")
        del feed.aircraft["452144"]
        for _ in range(30):
            step(mon, clock)
        self.assertNotIn("DIVERSION", kinds(alerts))


class AmmanAndEilatTests(unittest.TestCase):
    """TOWARD_ISRAEL, 11-12 Oct 2026: 5 Royal Jordanian flights on the daily corridor over Israel
    and 5 flights passing Taba / Aqaba whose straight line clipped Eilat."""
    AMM = (31.7226, 35.9932)

    def run_path(self, states, **ident):
        mon, feed, clock, alerts = make(["--no-routes"])
        fly(mon, feed, clock, "744824", states, **{"flight": "RJA873", "r": "JY-RAD", "t": "A20N", **ident})
        return kinds(alerts)

    def test_inbound_to_amman_from_the_west_is_quiet(self):
        start = (32.33, 33.6)
        trk = fw.bearing(*start, *self.AMM)
        self.assertNotIn("TOWARD_ISRAEL", self.run_path(path(*start, [trk] * 60, [26000] * 60, gs=430)))

    def test_climbing_out_of_amman_westward_is_quiet(self):
        n = 70
        alts = [min(30000, 3500 + 400 * i) for i in range(n)]
        self.assertNotIn("TOWARD_ISRAEL", self.run_path(path(31.74, 35.90, [285] * n, alts, gs=330), flight="RJA872"))

    def test_from_the_east_flying_on_past_amman_alerts(self):
        self.assertIn("TOWARD_ISRAEL", self.run_path(path(31.7226, 37.3, [270] * 110, [35000] * 110, gs=480),
                                                     flight="RJA555"))

    def test_passing_eilat_is_quiet_but_the_negev_is_not(self):
        east = lambda lat: path(lat, 33.6, [90] * 80, [39000] * 80, gs=480)  # noqa: E731
        self.assertNotIn("TOWARD_ISRAEL", self.run_path(east(29.60), flight="VJT536", r="9H-VFN", t="CL60"))
        self.assertIn("TOWARD_ISRAEL", self.run_path(east(30.40), flight="VJT536", r="9H-VFN", t="CL60"))


class MlatSpeedTests(unittest.TestCase):
    def descent(self, mlat_gs, **ident):
        """A 5,300 ft/min descent at 400 kt (7.5 deg), every position MLAT."""
        mon, feed, clock, alerts = make(["--no-routes"])
        n = 30
        states = path(32.65, 34.45, [215] * n, [39000] * 12 + [39000 - 880 * i for i in range(n - 12)], gs=400)
        states = [{**st, "type": "mlat", "mlat": ["lat", "lon", "gs"], "gs": mlat_gs} for st in states]
        fly(mon, feed, clock, "738a4b", states, **ident)
        return alerts

    def test_mlat_speed_estimate_does_not_make_a_descent_steep(self):
        """GLF5 "684", 12 Oct 2026: MLAT said 257 kt on one poll (really 455) -> 11.1 deg."""
        self.assertNotIn("VERTICAL_RATE", kinds(self.descent(250, flight="GLF684", r="684", t="GLF5", dbFlags=1)))

    def test_military_and_mlat_are_said_in_the_alert(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        n = 30
        states = path(32.65, 34.45, [215] * n, [39000] * 12 + [39000 - 1500 * i for i in range(n - 12)], gs=300)
        states = [{**st, "type": "mlat", "mlat": ["lat", "lon", "gs"]} for st in states]
        fly(mon, feed, clock, "738a4b", states, flight="GLF684", r="684", t="GLF5", dbFlags=1)
        hits = [a for a in alerts if a["kind"] == "VERTICAL_RATE"]
        self.assertTrue(hits, kinds(alerts))
        self.assertIn("military aircraft", hits[0]["message"])
        self.assertIn("MLAT position: speed from its positions", hits[0]["message"])


    def test_military_on_mlat_is_judged_by_rate_only(self):
        """USAF tankers into TLV, -3,800 to -4,900 ft/min, were "-15 to -20 deg" from MLAT speeds (24-25 Oct 2026).
        A military aircraft on MLAT positions alerts only at --vrate; others keep the angle."""
        got = {}
        for mil in (1, 0):
            mon, feed, clock, alerts = make(["--no-routes"])
            n = 30
            states = path(32.65, 34.45, [215] * n, [30000] * 12 + [30000 - 850 * i for i in range(n - 12)], gs=250)
            states = [{**st, "type": "mlat", "mlat": ["lat", "lon", "gs"]} for st in states]
            fly(mon, feed, clock, "ae01ce", states, flight="QID61" if mil else "TST001", r="", t="K35R", dbFlags=mil)
            got[mil] = "VERTICAL_RATE" in kinds(alerts)
        self.assertEqual(got, {1: False, 0: True})

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

    def test_descending_toward_an_airport_is_landing_there(self):
        """THY6264, listed LTBA-WSSS, turned from its cruise to descend into Amman (14 Oct 2026)."""
        n = len(self.TRACKS)
        for alts, expected in (([33000] * n, True), ([33000] * 30 + [33000 - 300 * i for i in range(1, n - 28)], False)):
            mon, feed, clock, alerts = with_routes()   # turning back toward Al Jouf (AJF), ~120 nm ahead
            fly(mon, feed, clock, "896121", path(31.0, 39.0, self.TRACKS, alts), flight="UAE121", r="A6-EUA")
            self.assertEqual("OFF_COURSE" in kinds(alerts), expected, kinds(alerts))

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
        self.assertEqual(links["map"], fw.VIEWER_URL + "?hex=8965d1&f=FZ1073&d=A&t=1790000000")
        self.assertEqual(fw.external_links(fw.Track("738bed"), 1790000000, fw.VIEWER_URL)["map"],
                         fw.VIEWER_URL + "?hex=738bed&t=1790000000")  # not on the board: the aircraft alone
        self.assertNotIn("map", fw.external_links(t, 1790000000, ""))  # --viewer-url ''
        # the alert's kind and the flight's earlier alerts (a=KIND~seconds after t), drawn on the map
        t.sent.extend([("VERTICAL_RATE", 1789999440), ("LOST_CONTACT", 1789999500)])
        self.assertEqual(fw.external_links(t, 1790000000, fw.VIEWER_URL, "EMERGENCY")["map"],
                         fw.VIEWER_URL + "?hex=8965d1&f=FZ1073&d=A&t=1790000000"
                         "&a=VERTICAL_RATE~-560.LOST_CONTACT~-500.EMERGENCY~0")
        ann, _ = self.make()
        rec = alert_rec("TOWARD_ISRAEL", "not bound for Israel " + "x" * 400)
        rec["links"] = {"map": links["map"], **rec["links"]}
        post = ann.announce(rec)
        self.assertEqual([s["link"] for s in post["segments"] if "link" in s], ["Map", "Replay", "FR24"])
        self.assertEqual([s["link"] for s in ann.compose(alert_rec("EMERGENCY", "squawk 7700"), False) if "link" in s],
                         ["Live", "Replay", "FR24"])  # no map page (--viewer-url ''): the live link stays
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

    def test_telegram_gets_one_spoofing_start_in_six_hours(self):
        class Sent:
            calls = []

            def post(self, url, **kw):
                self.calls.append(kw["json"])
        args = fw.parse_args(["--jsonl", "", "--posts", "", "--turn-zones", "", "--telegram-posts", ""])
        http = Sent()
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "@c"}):
            n = fw.Notifier(args, http, fw.Announcer(args, out=lambda text: None), console=lambda text: None)
        kw = dict(remote=False, traffic="ARR", route=None)
        n.send(alert_rec("GPS_DEGRADED", "gps-20260930T0500Z: 3 aircraft report ... (NIC 0)", **kw))
        n.send(alert_rec("GPS_SPOOFING", "spoof-20260930T0500Z: 2 aircraft reported motionless", **kw))
        n.send(alert_rec("GPS_SPOOFING", "spoof-20260930T0500Z ended: 2 aircraft reported", minute=20, **kw))
        n.send(alert_rec("GPS_SPOOFING", "spoof-20260930T0540Z: 2 aircraft reported motionless", minute=40, **kw))
        self.assertEqual(len(http.calls), 1)
        n.send(alert_rec("GPS_SPOOFING", "spoof-20260930T1200Z: 2 aircraft reported motionless",
                         time="2026-09-30T12:00:00Z", **kw))
        self.assertEqual(len(http.calls), 2)

    def test_telegram_send_is_tried_again_after_a_connection_reset(self):
        """N143's follow-up was lost to "Connection reset by peer" (25 Oct 2026). Tried once more; the token, which
        is in the URL, never reaches the log."""
        class Flaky:
            def __init__(self, fails):
                self.fails, self.calls = fails, 0

            def post(self, url, **kw):
                self.calls += 1
                if self.calls <= self.fails:
                    raise requests.ConnectionError(f"reset while sending to {url}")
                return FakeResponse(200, {"ok": True, "result": {"message_id": 7}})
        args = fw.parse_args(["--jsonl", "", "--posts", "", "--turn-zones", "", "--telegram-posts", ""])
        got = {}
        for fails in (1, 2):
            http = Flaky(fails)
            with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:SECRET", "TELEGRAM_CHAT_ID": "@c"}):
                n = fw.Notifier(args, http, fw.Announcer(args, out=lambda text: None), console=lambda text: None)
            with mock.patch.object(fw.time, "sleep"), self.assertLogs(fw.log, "WARNING") as logs:
                got[fails] = (n.telegram([{"text": "hello"}]), http.calls)
            self.assertNotIn("SECRET", "\n".join(logs.output))
        self.assertEqual(got, {1: (7, 2), 2: (None, 2)})

    def test_telegram_skips_common_kinds_and_sends_the_post_wording(self):
        class Sent:
            def __init__(self):
                self.calls = []

            def post(self, url, **kw):
                self.calls.append(kw["json"])
        saved = os.path.join(tempfile.mkdtemp(), "posts_telegram.jsonl")
        args = fw.parse_args(["--jsonl", "", "--posts", "", "--turn-zones", "", "--telegram-posts", saved,
                              "--telegram-skip", "LOST_CONTACT,CONTACT_RESTORED,POSITION_JUMP"])
        http = Sent()
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "@c"}):
            n = fw.Notifier(args, http, fw.Announcer(args, out=lambda text: None), console=lambda text: None)
        kw = dict(remote=False, traffic="ARR", route=None)
        n.send(alert_rec("LOST_CONTACT", "silent 124s; last 5875 ft, 56 nm from TLV, track 165 deg", **kw))
        n.send(alert_rec("CONTACT_RESTORED", "heard again after 300s", **kw))
        n.send(alert_rec("POSITION_JUMP", "90.0 nm in 20s (~16200 kt) [also: LOST_CONTACT]", **kw))
        # a course change rests on positions too (GPS jamming: N143, LY844, 25 Oct 2026)
        n.send(alert_rec("LOST_CONTACT", "silent 61s; last 8000 ft, track 310 deg [also: COURSE_CHANGE]", **kw))
        n.send(alert_rec("POSITION_JUMP", "23.8 nm in 19s (~4500 kt) [also: COURSE_CHANGE]", **kw))
        self.assertEqual(http.calls, [])
        n.send(alert_rec("LOST_CONTACT", "silent 124s; last 5875 ft, track 165 deg [also: VERTICAL_RATE]",
                         route_text="A <-> B", **kw))
        n.send(alert_rec("EMERGENCY", "squawk 7700 (GENERAL EMERGENCY)", **kw))
        self.assertEqual(len(http.calls), 2)
        text = http.calls[0]["text"]
        self.assertEqual(http.calls[0]["parse_mode"], "HTML")
        self.assertIn("Lost contact: FZ1073 (A6-FKF, B38M) A &lt;-&gt; B - FL150", text)
        self.assertIn('<a href="https://globe.adsbexchange.com/?icao=8965d1&amp;showTrace=2026-09-30">Replay</a>', text)
        with open(saved, encoding="utf-8") as f:
            lines = [json.loads(line) for line in f]
        self.assertEqual([(p["kind"], p["sent"]) for p in lines], [("LOST_CONTACT", True), ("EMERGENCY", True)])
        self.assertIn("Lost contact: FZ1073 (A6-FKF, B38M) A <-> B - FL150", lines[0]["text"])

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


class BlueskyTests(unittest.TestCase):
    """Announcements posted to Bluesky with a fake atproto client: threads as the Announcer threads them, a failed
    post never stops the next, skipped kinds stay off, and nothing happens without the account in the environment."""

    class FakeClient:
        def __init__(self, fail=()):
            self.posts, self.fail, self.logins = [], set(fail), []

        def login(self, handle, password):
            self.logins.append(handle)

        def send_post(self, segments, reply_to=None):
            n = len(self.posts) + 1
            if n in self.fail:
                self.posts.append(None)
                raise RuntimeError("Bluesky said no")
            text = "".join(s.get("text") or s["link"] for s in segments)
            self.posts.append({"n": n, "text": text, "reply_to": reply_to, "url": f"https://bsky.app/post/{n}"})
            return types.SimpleNamespace(url=f"https://bsky.app/post/{n}", n=n)

    def make(self, fail=(), env=True, *argv):
        args = fw.parse_args(["--jsonl", "", "--posts", "", "--turn-zones", "", "--telegram-posts", "",
                              "--bluesky-posts", "", *argv])
        fake = self.FakeClient(fail)
        envs = {"BLUESKY_HANDLE": "fw.bsky.social", "BLUESKY_APP_PASSWORD": "app-pw"} if env else {}
        with mock.patch.dict(os.environ, envs, clear=False):
            for k in ("BLUESKY_HANDLE", "BLUESKY_APP_PASSWORD", "Bluehandle", "Blueword"):
                if not env:
                    os.environ.pop(k, None)
            bsky = fw.Bluesky(args, client_factory=lambda: fake)
        n = fw.Notifier(args, None, fw.Announcer(args, out=lambda text: None), console=lambda text: None, bluesky=bsky)
        return n, fake

    def test_the_astro_bots_account_is_never_used(self):
        args = fw.parse_args(["--jsonl", "", "--posts", "", "--turn-zones", "", "--bluesky-posts", ""])
        with mock.patch.dict(os.environ, {"Bluehandle": "astro.bsky.social", "Blueword": "pw"}, clear=False):
            for k in ("BLUESKY_HANDLE", "BLUESKY_APP_PASSWORD"):
                os.environ.pop(k, None)
            self.assertFalse(fw.Bluesky(args, client_factory=lambda: None).enabled)

    def test_a_flight_is_one_thread(self):
        n, fake = self.make()
        n.send(alert_rec("EMERGENCY", "squawk 7700 (GENERAL EMERGENCY)", airline="Flydubai", remote=True, traffic="ARR", route=None))
        n.send(alert_rec("VERTICAL_RATE", "DESCENT -8000 ft/min, -14.0 deg", minute=3, remote=True, traffic="ARR", route=None))
        self.assertEqual(len(fake.posts), 2)
        self.assertIsNone(fake.posts[0]["reply_to"])
        self.assertIn("Emergency code 7700: FZ1073 Flydubai", fake.posts[0]["text"])
        first = fake.posts[1]["reply_to"]
        self.assertEqual(first["parent"].n, 1)
        self.assertEqual(first["root"].n, 1)
        self.assertEqual(fake.logins, ["fw.bsky.social"])   # one login for the run

    def test_a_failed_post_does_not_stop_the_next(self):
        n, fake = self.make(fail={1})
        n.send(alert_rec("EMERGENCY", "squawk 7700 (GENERAL EMERGENCY)", remote=True, traffic="ARR", route=None))
        n.send(alert_rec("VERTICAL_RATE", "DESCENT -8000 ft/min, -14.0 deg", minute=3, remote=True, traffic="ARR", route=None))
        self.assertIsNone(fake.posts[0])
        self.assertIsNone(fake.posts[1]["reply_to"])   # its parent never made it: top-level

    def test_skipped_kinds_and_no_account(self):
        n, fake = self.make()
        n.send(alert_rec("LOST_CONTACT", "silent 120s, last track 90 deg", remote=True, traffic="ARR", route=None))
        self.assertEqual(fake.posts, [])
        n, fake = self.make(env=False)
        self.assertFalse(n.bluesky.enabled)
        n.send(alert_rec("EMERGENCY", "squawk 7700 (GENERAL EMERGENCY)", remote=True, traffic="ARR", route=None))
        self.assertEqual(fake.posts, [])


class PlotProbeTests(unittest.TestCase):
    """tools/plot_alert.py runs Monitor.check_vrate offline on every trace report ("first detection")."""
    def test_plot_finds_the_dive_offline(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
        import plot_alert
        states = lift()
        air = [{"t": 1_791_187_200 + 10 * i, "lat": st["lat"], "lon": st["lon"], "alt": st["alt_baro"],
                "track": st["track"], "gs": st["gs"], "vrate": st["baro_rate"], "ground": False}
               for i, st in enumerate(states) if i not in (9, 10)]  # (no history 30-45 s before the dive)
        eps = plot_alert.vrate_episodes(air, {"hex": "739215", "callsign": "4XDAN", "type": "P750"})
        self.assertEqual([e["dir"] for e in eps], ["DESCENT"])


class Run18OctTests(unittest.TestCase):
    """False alarms of the run of 18 Oct 2026 (the API refused most polls for hours)."""

    def test_aircraft_last_heard_before_our_outage_is_not_lost(self):
        """13:48-15:30 IDT no poll answered; 6 aircraft heard before it were "silent 6182s" when polls came back."""
        mon, feed, clock, alerts = make(["--no-routes"])
        feed.local.add("999999")
        feed.aircraft["999999"] = airborne(31.0, 34.0, 20000, flight="KEEP1")
        fly(mon, feed, clock, "4a3122", path(31.9, 35.0, [300] * 6, [9000] * 6, gs=250), flight="AIZ1806")
        feed.local.discard("4a3122")
        del feed.aircraft["4a3122"]  # (landed meanwhile)
        feed.limited.add("/v2/")
        for _ in range(60):  # 10 min of refused polls
            step(mon, clock)
        feed.limited.clear()
        for _ in range(30):
            step(mon, clock)
        self.assertEqual(kinds(alerts), [])
        self.assertTrue(mon.tracks["4a3122"].lost)

    def test_silence_seen_to_begin_while_polls_are_sparse_is_still_lost_contact(self):
        """Answered polls 60 s apart (the rest refused): formerly no lost-contact check at all."""
        mon, feed, clock, alerts = make(["--no-routes"])
        feed.local.add("999999")
        feed.aircraft["999999"] = airborne(31.0, 34.0, 20000, flight="KEEP1")
        fly(mon, feed, clock, "4a3122", path(32.4, 34.0, [300] * 4, [20000] * 4, gs=450, dt=60), dt=60,
            flight="AIZ1806")
        feed.local.discard("4a3122")
        del feed.aircraft["4a3122"]
        for _ in range(5):
            step(mon, clock, 60)
        self.assertEqual(kinds(alerts), ["LOST_CONTACT"])

    def test_refused_trace_files_are_not_asked_again_for_a_while(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        feed.refused.add("/data/traces/")
        t = mon.tracks.setdefault("739215", fw.Track("739215"))
        for _ in range(5):
            mon.trace_left = 3
            self.assertIsNone(mon.recent_trace(t))
            clock.t += 10
        self.assertEqual(sum(p.startswith("/data/traces/") for _, p in feed.calls), 1)
        clock.t += fw.TRACE_REFUSED_S
        mon.trace_left = 3
        mon.recent_trace(t)
        self.assertEqual(sum(p.startswith("/data/traces/") for _, p in feed.calls), 2)
        self.assertEqual(mon.trace_refused[1], 2 * fw.TRACE_REFUSED_S)  # refused again: a longer pause

    def test_departure_heard_again_climbing_out_of_its_destination_is_its_next_flight(self):
        """ELY353 TLV -> MUC, unheard 100 min, then climbing out of Munich still as ELY353, turning toward
        TLV: "TLV departure heading back to TLV, 1427 nm out"."""
        mon, feed, clock, alerts = with_board([board_row("LY", "353", "D", local_time(mon_clock(), -3), "MUC")])
        ident = dict(flight="ELY353", r="4X-EKT", t="B738")
        fly(mon, feed, clock, "738061", path(47.24, 14.32, [306] * 6, [31250] * 6), **ident)
        t = mon.tracks["738061"]
        self.assertEqual(t.sched.direction, "DEP")
        feed.local.discard("738061")
        del feed.aircraft["738061"]
        clock.t += 6000
        out = path(48.35, 11.80, [80] * 6 + [114] * 30, [2750 + 300 * i for i in range(36)], gs=280)
        fly(mon, feed, clock, "738061", out, **ident)
        self.assertNotIn("TURNING_BACK", kinds(alerts))
        self.assertIsNone(t.sched)
        self.assertEqual(mon.classify(t)[0], "?")

    def test_on_the_ground_before_a_gap_is_no_take_off(self):
        """11 EARLY_DESCENTs on arrivals descending into their destinations "1-8 min after take-off", after
        2 h of refused polls."""
        mon, feed, clock, alerts = make(["--no-routes"])
        ident = dict(flight="ELY5115", r="4X-EKB", t="B738", category="A3")
        fly(mon, feed, clock, "738070", [dict(lat=32.0, lon=34.88, alt_baro="ground", gs=10, track=90)] * 3, **ident)
        clock.t += 2 * 3600
        fly(mon, feed, clock, "738070", path(41.5, 41.4, [90] * 30, [22800 - 200 * i for i in range(30)], gs=300),
            **ident)
        self.assertNotIn("EARLY_DESCENT", kinds(alerts))
        self.assertEqual(mon.tracks["738070"].takeoff, 0)

    def test_short_hop_descending_toward_an_airfield_is_no_early_descent(self):
        """AYT253 (Ayeet E145, no route, 25 Oct 2026) climbed to 11,050 ft and descended into Ramon AB 24 nm ahead
        9 min after take-off: EARLY_DESCENT on Telegram. Toward Ovda here; heading west, no airfield ahead, it alerts."""
        got = {}
        for heading in (180, 270):
            mon, feed, clock, alerts = make(["--no-routes"])
            ident = dict(flight="AYT253", r="4X-EMT", t="E145", category="A3")
            fly(mon, feed, clock, "73934a", [dict(lat=30.55, lon=34.94, alt_baro="ground", gs=10, track=heading)] * 3,
                **ident)
            fly(mon, feed, clock, "73934a", path(30.55, 34.94, [heading] * 24,
                                                 [1000 + 600 * i for i in range(18)] + [11200 - 300 * i for i in range(6)],
                                                 gs=250), **ident)
            got[heading] = "EARLY_DESCENT" in kinds(alerts)
        self.assertEqual(got, {180: False, 270: True})

    def test_circuits_near_amman_with_no_route_are_not_holding(self):
        """RJA017 (A321 JY-AYV): took off from Amman and circled east of it at 4,300 ft 2 h later."""
        mon, feed, clock, alerts = make(["--no-routes"])
        fly(mon, feed, clock, "740736", racetrack(10, start=(31.68, 36.20), alt=4300, gs=180), flight="RJA017",
            r="JY-AYV", t="A321")
        self.assertNotIn("HOLDING", kinds(alerts))

    def test_military_trainer_alerts_once(self):
        """CHLE1B (PC21, Jordan): 15 alerts in 31 min of aerobatics."""
        mon, feed, clock, alerts = make(["--no-routes"])
        ident = dict(flight="CHLE1B", r="1178", t="PC21", dbFlags=1)
        for k in range(3):  # three dives 10 min apart (beyond the 5-min cooldown)
            fly(mon, feed, clock, "743593", path(32.4, 36.0 + k / 10, [90] * 10, [16000] * 4 + [16000 - 2500 * i for i in
                                                                                       range(1, 7)], gs=250), **ident)
            fly(mon, feed, clock, "743593", path(32.4, 36.2, [270] * 50, [12000 + 80 * i for i in range(50)], gs=250),
                **ident)
        self.assertEqual(kinds(alerts), ["VERTICAL_RATE"])
        self.assertIn("no further alerts on it", alerts[0]["message"])

    def test_large_military_alerts_once_and_follows_up_when_it_goes_silent(self):
        """IAM4188 (Italian Air Force AT76) patrolling west of Israel: 7 alerts in 70 min (20 Oct 2026)."""
        mon, feed, clock, alerts = make(["--no-routes"])
        ups = []
        mon.notifier.follow_up = lambda h, when, seg: ups.append(seg)
        feed.local = {f"99999{i}" for i in range(6)}  # (one of two aircraft going silent: a feed glitch)
        for i in range(6):
            feed.aircraft[f"99999{i}"] = airborne(32.5 + 0.1 * i, 34.5, 10000, flight=f"LOC{i}")
        ident = dict(flight="IAM4188", r="MM62298", t="AT76", dbFlags=1)
        lap = [85] * 30 + [266] * 30  # (out and back on the same line: a reversal each way, 10 min a lap)
        fly(mon, feed, clock, "33fdd0", path(32.6, 33.2, lap * 4, [15000] * 240, gs=250), **ident)
        at = fw.datetime.fromisoformat(alerts[0]["time"].replace("Z", "+00:00")).timestamp()
        mon.notifier.tg_threads["33fdd0"] = {"id": 3, "t": at, "last": at, "n": 1}
        self.assertEqual(kinds(alerts), ["COURSE_CHANGE"])
        self.assertEqual(ups, [])
        feed.local.discard("33fdd0")
        del feed.aircraft["33fdd0"]
        for _ in range(80):
            step(mon, clock)
        self.assertEqual(kinds(alerts), ["COURSE_CHANGE"])
        self.assertEqual(len(ups), 1)
        text = "".join(x.get("text") or x["link"] for x in ups[0])
        self.assertIn("last heard", text)
        self.assertIn("not alerted (military", text)
        self.assertIn("Alerts: course reversal ", text)
        self.assertRegex(text, r"then not alerted \(military, [0-9:]+-[0-9:]+\): course reversal x[2-9]")


class SecondFeedTests(unittest.TestCase):
    """--fallback-provider (adsb.fi): the local circle when adsb.lol does not answer (18 Oct 2026: ~110 of
    360 polls an hour answered, then none for 2 h twice)."""

    def setUp(self):
        self.mon, self.feed, self.clock, self.alerts = make(["--no-routes", "--fallback-provider", "adsb.fi"])
        self.feed.local = {"999999", "999998", "999997"}
        for i, h in enumerate(sorted(self.feed.local)):
            self.feed.aircraft[h] = airborne(31.5 + i / 10, 34.0, 20000, flight=f"KEEP{i}")

    def test_local_poll_comes_from_the_second_feed_when_the_first_refuses(self):
        mon, feed, clock = self.mon, self.feed, self.clock
        feed.limited.add("/v2/point/")
        fly(mon, feed, clock, "4a3122", path(32.4, 34.0, [300] * 30, [20000] * 30), flight="AIZ1806")
        self.assertEqual(mon.local_feed, "adsb.fi")
        self.assertEqual(len(mon.tracks["4a3122"].samples), 30)
        self.assertTrue(any(p.startswith("/api/v3/lat/") for _, p in feed.calls))
        self.assertEqual(self.alerts, [])

    def test_silence_counts_only_on_the_feed_that_heard_it(self):
        """adsb.fi does not have it: while only adsb.fi answers, its silence is not judged."""
        mon, feed, clock = self.mon, self.feed, self.clock
        fly(mon, feed, clock, "4a3122", path(32.4, 34.0, [300] * 6, [20000] * 6), flight="AIZ1806")
        self.assertEqual(mon.tracks["4a3122"].feed, "adsb.lol")
        feed.unseen_by["opendata.adsb.fi"] = {"4a3122"}
        feed.limited.add("/v2/point/")
        fly(mon, feed, clock, "4a3122", path(32.5, 33.6, [300] * 60, [20000] * 60), flight="AIZ1806")  # 10 min
        self.assertNotIn("LOST_CONTACT", kinds(self.alerts))
        feed.limited.clear()  # adsb.lol back, and it does not have it either: silent now
        feed.local.discard("4a3122")
        for _ in range(30):
            step(mon, clock)
        self.assertNotIn("LOST_CONTACT", kinds(self.alerts))  # (we did not see it go)

    def test_military_list_marks_aircraft_without_db_flags(self):
        mon, feed, clock = self.mon, self.feed, self.clock
        feed.mil.add("ae1234")
        feed.limited.add("/v2/point/")
        fly(mon, feed, clock, "ae1234", path(32.6, 34.6, [90] * 3, [25000] * 3), flight="RCH123", t="C17")
        self.assertTrue(mon.tracks["ae1234"].military)



class SquawkCaseTests(unittest.TestCase):
    """Misses found by replaying the AeroSignals squawk cases (tools/squawk_cases.py, 19 Oct 2026)."""
    TLV = (32.0114, 34.8867)

    def test_departure_landing_short_of_its_destination_is_a_diversion(self):
        """LY357 TLV -> FRA (7700) landed at Belgrade: only EMERGENCY."""
        mon, feed, clock, alerts = with_routes()
        mon.routes["ELY357"] = (clock(), "LLBG-LHBP")
        ident = dict(flight="ELY357", r="4X-EKA", t="B738")
        fly(mon, feed, clock, "738357", [dict(lat=self.TLV[0], lon=self.TLV[1], alt_baro="ground", gs=5, track=270)] * 3,
            **ident)
        fly(mon, feed, clock, "738357", path(*self.TLV, [300] * 6, [1500 + 800 * i for i in range(6)], gs=180), **ident)
        clock.t += 5400
        fly(mon, feed, clock, "738357", path(44.95, 20.10, [130] * 6, [3000 - 400 * i for i in range(6)], gs=150),
            **ident)
        fly(mon, feed, clock, "738357", [dict(lat=44.8206, lon=20.3043, alt_baro="ground", gs=12, track=120)] * 4,
            **ident)
        hits = [a for a in alerts if a["kind"] == "DIVERSION"]
        self.assertEqual(len(hits), 1, kinds(alerts))
        self.assertIn("departure bound for LHBP", hits[0]["message"])

    def test_route_alone_does_not_make_a_departure(self):
        """The same landing by an aircraft never seen leaving TLV (stale route data): not a diversion."""
        mon, feed, clock, alerts = with_routes()
        mon.routes["ELY357"] = (clock(), "LLBG-LHBP")
        fly(mon, feed, clock, "738357", path(44.95, 20.10, [130] * 6, [3000 - 400 * i for i in range(6)], gs=150),
            flight="ELY357", r="4X-EKA", t="B738")
        fly(mon, feed, clock, "738357", [dict(lat=44.8206, lon=20.3043, alt_baro="ground", gs=12, track=120)] * 4,
            flight="ELY357", r="4X-EKA", t="B738")
        self.assertNotIn("DIVERSION", kinds(alerts))

    def test_low_return_under_an_emergency_squawk(self):
        """DL2692 MSP -> JFK squawked 7700 at 3,775 ft and landed back."""
        mon, feed, clock, alerts = with_board([board_row("LY", "5", "D", local_time(Clock(), 0), "ATH")])
        ident = dict(flight="ELY005", r="4X-EKA", t="B738")
        fly(mon, feed, clock, "738005", [dict(lat=self.TLV[0], lon=self.TLV[1], alt_baro="ground", gs=5, track=270)] * 3,
            **ident)
        out = path(*self.TLV, [270] * 12 + [90] * 12, [1000 + 250 * i for i in range(12)] + [3750 - 250 * i for i in range(12)],
                   gs=180, squawk="7700")
        fly(mon, feed, clock, "738005", out, **ident)
        fly(mon, feed, clock, "738005", [dict(lat=self.TLV[0], lon=self.TLV[1], alt_baro="ground", gs=5, track=270,
                                              squawk="7700")] * 3, **ident)
        self.assertIn("RETURNED", kinds(alerts))

    def test_circling_that_ends_on_final_is_not_holding(self):
        """FR6789 (diverting to Venice): "circling for 30 min ... at 275 ft"."""
        mon, feed, clock, alerts = with_routes()
        mon.routes["RYR6789"] = (clock(), "LIPQ-LFOB")
        states = racetrack(8, alt=0, gs=180)
        for i, st in enumerate(states):
            st["alt_baro"] = 9000 - 8000 * i // len(states)
            st["baro_rate"] = -230
        fly(mon, feed, clock, "4d225c", states, flight="RYR6789", r="9H-QDD", t="B738")
        self.assertNotIn("HOLDING", kinds(alerts))


class FarEndTests(unittest.TestCase):
    """False alarms at the far end of TLV flights (19 Oct 2026): the board's airport over the route data, an
    arrival's previous flight landing at its origin under the arrival's callsign, turns out of / into it."""
    AIRPORTS = {
        "airports/schema-01/L/LC.csv": "Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,Longitude,AltitudeFeet\n"
                                       "LCLK,Larnaca,LCLK,LCA,Larnaca,CY,34.8751,33.6249,8\n",
        "airports/schema-01/U/UG.csv": "Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,Longitude,AltitudeFeet\n"
                                       "UGTB,Tbilisi,UGTB,TBS,Tbilisi,GE,41.6692,44.9547,1624\n"
                                       "UGSB,Batumi,UGSB,BUS,Batumi,GE,41.6103,41.5997,105\n",
        "airports/schema-01/L/LE.csv": "Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,Longitude,AltitudeFeet\n"
                                       "LEMD,Madrid,LEMD,MAD,Madrid,ES,40.4719,-3.5626,1998\n"
                                       "LEMG,Malaga,LEMG,AGP,Malaga,ES,36.6749,-4.4991,53\n",
        "airports/schema-01/L/LB.csv": "Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,Longitude,AltitudeFeet\n"
                                       "LBSF,Sofia,LBSF,SOF,Sofia,BG,42.6967,23.4114,1742\n",
    }

    def setup(self, rows):
        mon, feed, clock, alerts = make(["--rate", "600", "--rate-max", "6000"], with_schedule=True)
        feed.files = {**ROUTE_FILES, **self.AIRPORTS, "routes/schema-01/E/ELY-5.csv":
                      "Callsign,Code,Number,AirlineCode,AirportCodes\nELY5417,ELY,5417,ELY,LLBG-LCLK\n"}
        mon.schedule.load(rows, datetime.fromtimestamp(clock(), mon.schedule.tz))
        for icao in ("UGTB", "LEMD", "LBSF"):  # (a local checkout has them all: StandingData.all_fields)
            mon.standing.airport(icao)
            clock.t += 10
        feed.local = {"999999"}
        feed.aircraft["999999"] = airborne(32.5, 34.5, 10000, flight="LOC1")
        return mon, feed, clock, alerts

    def test_board_destination_over_the_route_data(self):
        """ELY5417 TLV -> TBS (board), listed LLBG-LCLK: landed at Tbilisi, "bound for LCLK, 671 nm from it"."""
        mon, feed, clock, alerts = self.setup([board_row("LY", "5417", "D", local_time(Clock(), 0), "TBS")])
        ident = dict(flight="ELY5417", r="LY-LOC", t="B738")
        tlv = (32.0114, 34.8867)
        fly(mon, feed, clock, "503cc0", [dict(lat=tlv[0], lon=tlv[1], alt_baro="ground", gs=5, track=270)] * 3,
            **ident)
        fly(mon, feed, clock, "503cc0", path(*tlv, [10] * 6, [1500 + 800 * i for i in range(6)], gs=180), **ident)
        self.assertEqual(mon.other_end(mon.tracks["503cc0"], "DEP")[0], "UGTB")
        clock.t += 3 * 3600
        fly(mon, feed, clock, "503cc0", path(41.75, 44.70, [110] * 6, [3000 - 400 * i for i in range(6)], gs=150),
            local=False, **ident)
        fly(mon, feed, clock, "503cc0", [dict(lat=41.6692, lon=44.9547, alt_baro="ground", gs=12, track=120)] * 4,
            local=False, **ident)
        self.assertNotIn("DIVERSION", kinds(alerts, "503cc0"))

    def test_flown_leg_outlasts_silence_on_the_ground(self):
        """KL462 TLV -> OTP landed at Bucharest, unheard there for over 30 min (its track dropped, the flown leg with
        it), then found again flying on to Amsterdam as KL462: "bound for LROP on the ground 966 nm from it" (27 Oct)."""
        mon, feed, clock, alerts = self.setup([board_row("LY", "5417", "D", local_time(Clock(), 0), "TBS")])
        ident = dict(flight="ELY5417", r="LY-LOC", t="B738")
        tlv = (32.0114, 34.8867)
        fly(mon, feed, clock, "503cc0", [dict(lat=tlv[0], lon=tlv[1], alt_baro="ground", gs=5, track=270)] * 3,
            **ident)
        fly(mon, feed, clock, "503cc0", path(*tlv, [10] * 6, [1500 + 800 * i for i in range(6)], gs=180), **ident)
        clock.t += 3 * 3600
        fly(mon, feed, clock, "503cc0", [dict(lat=41.6692, lon=44.9547, alt_baro="ground", gs=12, track=120)] * 4,
            local=False, **ident)
        feed.local.discard("503cc0")
        del feed.aircraft["503cc0"]
        for _ in range(45):
            step(mon, clock, 60)
        fly(mon, feed, clock, "503cc0", path(41.61, 41.68, [270] * 6, [3000 - 400 * i for i in range(6)], gs=150),
            local=False, **ident)
        fly(mon, feed, clock, "503cc0", [dict(lat=41.6103, lon=41.5997, alt_baro="ground", gs=12, track=120)] * 4,
            local=False, **ident)
        self.assertNotIn("DIVERSION", kinds(alerts, "503cc0"))

    def test_no_follow_up_after_a_landing_alert(self):
        """LY353 TLV -> MUC landed at Athens (21 Oct 2026): DIVERSION, then a follow-up 2 min later saying the same."""
        mon, feed, clock, alerts = self.setup([board_row("LY", "5417", "D", local_time(Clock(), 0), "TBS")])
        ups = []
        mon.notifier.follow_up = lambda h, when, seg: ups.append(seg)
        ident = dict(flight="ELY5417", r="4X-EKH", t="B738")
        tlv = (32.0114, 34.8867)
        fly(mon, feed, clock, "73806d", [dict(lat=tlv[0], lon=tlv[1], alt_baro="ground", gs=5, track=270)] * 3, **ident)
        fly(mon, feed, clock, "73806d", path(*tlv, [10] * 6, [1500 + 800 * i for i in range(6)], gs=180), **ident)
        clock.t += 3 * 3600
        fly(mon, feed, clock, "73806d", path(41.63, 41.53, [110] * 6, [3000 - 400 * i for i in range(6)], gs=150),
            local=False, **ident)
        fly(mon, feed, clock, "73806d", [dict(lat=41.6103, lon=41.5997, alt_baro="ground", gs=12, track=120)] * 3,
            local=False, **ident)
        self.assertEqual(kinds(alerts, "73806d"), ["DIVERSION"])
        at = fw.datetime.fromisoformat(alerts[0]["time"].replace("Z", "+00:00")).timestamp()
        mon.notifier.tg_threads["73806d"] = {"id": 165, "t": at, "last": at, "n": 1}
        fly(mon, feed, clock, "73806d", [dict(lat=41.6103, lon=41.5997, alt_baro="ground", gs=3, track=120)] * 30,
            local=False, **ident)
        self.assertEqual(ups, [])
        self.assertTrue(mon.notifier.tg_threads["73806d"]["done"])

    def test_positioning_to_its_origin_under_its_callsign(self):
        """4X-ABT flew TLV -> Madrid (6H183), then Madrid -> Malaga as ISR152 (6H152 AGP -> TLV, on time): OFF_COURSE
        ("TLV arrival heading 210 deg, 119 deg away from TLV") and DIVERSION on the ground at Malaga (20 Oct 2026)."""
        got = {}
        for origin in ("AGP", "SOF"):
            mon, feed, clock, alerts = self.setup([board_row("6H", "152", "A", local_time(Clock(), 6), origin)])
            ident = dict(flight="ISR152", r="4X-ABT", t="A320")
            mad, agp = (40.4719, -3.5626), (36.6749, -4.4991)
            fly(mon, feed, clock, "738288", [dict(lat=mad[0], lon=mad[1], alt_baro="ground", gs=5, track=190)] * 3,
                local=False, **ident)
            brg = fw.bearing(*mad, *agp)
            n = 200  # ~233 nm at 420 kt
            alts = [min(1500 + 500 * i, 30000) for i in range(n)]
            alts = [min(a, 1500 + 500 * (n - 1 - i)) for i, a in enumerate(alts)]
            hop = path(*mad, [brg] * n, alts, gs=420)
            hop = hop[:next((i for i, st in enumerate(hop) if fw.haversine_nm(st["lat"], st["lon"], *agp) < 4), n)]
            fly(mon, feed, clock, "738288", hop, local=False, **ident)
            fly(mon, feed, clock, "738288", [dict(lat=agp[0], lon=agp[1], alt_baro="ground", gs=10, track=190)] * 4,
                local=False, **ident)
            got[origin] = kinds(alerts, "738288")
        self.assertEqual(got["AGP"], [])
        self.assertIn("DIVERSION", got["SOF"])  # (bound from Sofia, on the ground at Malaga)

    def test_positioning_flight_climbing_out_away_from_its_origin(self):
        """9A-BTM, 22 Oct 2026: 6H717 TLV -> BUD, then ISR178 (6H178 TIV -> TLV) out of Budapest heading 290 deg
        before turning for Tivat: OFF_COURSE "1,174 nm out and opening" and a course reversal at 12,150 ft."""
        mon, feed, clock, alerts = self.setup([board_row("6H", "152", "A", local_time(Clock(), 6), "AGP")])
        ident = dict(flight="ISR152", r="4X-ABT", t="A320")
        mad, agp = (40.4719, -3.5626), (36.6749, -4.4991)
        fly(mon, feed, clock, "738288", [dict(lat=mad[0], lon=mad[1], alt_baro="ground", gs=5, track=190)] * 3,
            local=False, **ident)
        out = path(*mad, [300] * 30, [1500 + 500 * i for i in range(30)], gs=300)  # departure routing: away
        fly(mon, feed, clock, "738288", out, local=False, **ident)
        last = mon.tracks["738288"].last
        brg = fw.bearing(last.lat, last.lon, *agp)
        fly(mon, feed, clock, "738288", path(last.lat, last.lon, [300 - 10 * i for i in range(1, 11)] + [brg] * 40,
                                             [16000] * 50, gs=300), local=False, **ident)
        self.assertEqual(kinds(alerts, "738288"), [])

    def arrival_at_madrid(self, leaves):
        """An aircraft flying the 6H182 MAD -> TLV callsign coming down at Madrid; `leaves`: first seen
        departing Madrid, flying out 80 nm and back."""
        mon, feed, clock, alerts = self.setup([board_row("6H", "182", "A", local_time(Clock(), 5), "MAD"),
                                               board_row("6H", "193", "D", local_time(Clock(), -4), "MAD")])
        ident = dict(flight="ISR182", r="9A-BTH", t="A320")
        if leaves:
            fly(mon, feed, clock, "501d0a", [dict(lat=40.4719, lon=-3.5626, alt_baro="ground", gs=5, track=90)] * 3,
                local=False, **ident)
            fly(mon, feed, clock, "501d0a", path(40.4719, -3.5626, [90] * 70, [2000 + 300 * i for i in range(70)],
                                                 gs=250), local=False, **ident)
            start = mon.tracks["501d0a"].last
            clock.t += 600
            lat, lon = start.lat, start.lon
        else:
            lat, lon = 40.45, -1.40  # first heard 100 nm out (60 nm or less: its departure may have gone unseen)
        top, rate = (14000, 200) if leaves else (30000, 220)  # 1,500 ft about 5 nm before Madrid
        approach = [st for st in path(lat, lon, [270] * 250, [max(top - rate * i, 1500) for i in range(250)],
                                      gs=250) if st["lon"] > -3.45]
        if not leaves:  # (its previous flight's callsign in the air, taken up after CALLSIGN_HOLD)
            fly(mon, feed, clock, "501d0a", approach[:20], local=False, **{**ident, "flight": "ISR193"})
            approach = approach[20:]
        fly(mon, feed, clock, "501d0a", approach, local=False, **ident)
        del feed.aircraft["501d0a"]
        for _ in range(60):
            step(mon, clock)
        return kinds(alerts, "501d0a")

    def test_return_first_heard_on_its_way_back_is_a_diversion(self):
        """SWA512 (HNL -> SAN, 7700) watched from San Diego: first heard at FL360 362 nm out, flying back to Honolulu.
        Nothing says where it came from: a return, not its previous flight (squawk replay, 21 Oct 2026)."""
        mon, feed, clock, alerts = self.setup([board_row("6H", "182", "A", local_time(Clock(), 5), "MAD")])
        ident = dict(flight="ISR182", r="9A-BTH", t="A320")
        approach = [st for st in path(40.45, -1.40, [270] * 250, [max(30000 - 220 * i, 1500) for i in range(250)],
                                      gs=250) if st["lon"] > -3.45]
        fly(mon, feed, clock, "501d0a", approach, local=False, **ident)
        fly(mon, feed, clock, "501d0a", [dict(lat=40.4719, lon=-3.5626, alt_baro="ground", gs=10, track=270)] * 4,
            local=False, **ident)
        self.assertIn("DIVERSION", kinds(alerts, "501d0a"))

    def test_previous_flight_landing_at_the_arrivals_origin(self):
        """9A-BTH changed ISR193 -> ISR182 in the air and went silent on approach to Madrid: "TLV arrival
        went silent descending through 2000 ft ... probably landing elsewhere" (19 Oct 2026)."""
        self.assertNotIn("DIVERSION", self.arrival_at_madrid(leaves=False))

    def test_return_to_the_arrivals_origin_is_still_a_diversion(self):
        """QS1286 PRG -> TLV (7700) went back to Prague."""
        self.assertIn("DIVERSION", self.arrival_at_madrid(leaves=True))

    def test_listed_stop_on_the_way_is_not_a_diversion(self):
        """KLM461 AMS -> TLV is EHAM-LCLK-LLBG: it lands at Larnaca on the way and was a DIVERSION "probably
        landing at LCA" (20 Oct 2026). The same descent with no stop listed still is one."""
        got = {}
        for route in ("EHAM-LCLK-LLBG", "EHAM-LLBG"):
            mon, feed, clock, alerts = self.setup([board_row("KL", "461", "A", local_time(Clock(), 1), "AMS")])
            feed.files.update({"routes/schema-01/K/KLM-all.csv": "Callsign,Code,Number,AirlineCode,AirportCodes\n"
                                                                  f"KLM461,KLM,461,KLM,{route}\n",
                               "airports/schema-01/E/EH.csv": "Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,"
                                                              "Longitude,AltitudeFeet\nEHAM,Schiphol,EHAM,AMS,"
                                                              "Amsterdam,NL,52.3086,4.7639,-11\n"})
            ident = dict(flight="KLM461", r="PH-BXR", t="B739")
            start = (35.55, 33.15)
            brg = fw.bearing(*start, 34.8751, 33.6249)
            approach = [st for st in path(*start, [brg] * 120, [max(14000 - 150 * i, 1400) for i in range(120)],
                                          gs=220)]
            approach = approach[:next(i for i, st in enumerate(approach)
                                      if fw.haversine_nm(st["lat"], st["lon"], 34.8751, 33.6249) < 5)]
            fly(mon, feed, clock, "48418c", approach, local=False, **ident)
            del feed.aircraft["48418c"]
            for _ in range(60):
                step(mon, clock)
            got[route] = "DIVERSION" in kinds(alerts, "48418c")
        self.assertEqual(got, {"EHAM-LCLK-LLBG": False, "EHAM-LLBG": True})

    def test_board_tag_stop_is_not_a_diversion(self):
        """KL463 AMS -> TLV landed at Bucharest (DIVERSION, 24 Oct 2026): its route data is EHAM-LCLK-LLBG, but the
        board's KL464 leaves TLV for OTP an hour after it is due. Not when KLM flew in from Bucharest too (an ordinary
        rotation, as LY318 from LHR and LY319 to CDG), nor when the board's next number goes off the way. (The route
        here lists no stop and the flight is due too soon to stop on its way: those are stops too, StopOnTheWayTests.)"""
        got = {}
        for case in ("tag", "inbound", "off the way"):
            stop = "LIS" if case == "off the way" else "OTP"
            rows = [board_row("KL", "463", "A", local_time(Clock(), 1), "AMS"),
                    board_row("KL", "464", "D", local_time(Clock(), 4), stop)]
            if case == "inbound":
                rows.append(board_row("KL", "1234", "A", local_time(Clock(), 8), "OTP"))
            mon, feed, clock, alerts = self.setup(rows)
            feed.files.update({"routes/schema-01/K/KLM-all.csv": "Callsign,Code,Number,AirlineCode,AirportCodes\n"
                                                                  "KLM463,KLM,463,KLM,EHAM-LLBG\n",
                               "airports/schema-01/E/EH.csv": "Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,"
                                                              "Longitude,AltitudeFeet\nEHAM,Schiphol,EHAM,AMS,"
                                                              "Amsterdam,NL,52.3086,4.7639,-11\n",
                               "airports/schema-01/L/LR.csv": "Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,"
                                                              "Longitude,AltitudeFeet\nLROP,Henri Coanda,LROP,OTP,"
                                                              "Bucharest,RO,44.5711,26.0850,314\n",
                               "airports/schema-01/L/LP.csv": "Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,"
                                                              "Longitude,AltitudeFeet\nLPPT,Humberto Delgado,LPPT,LIS,"
                                                              "Lisbon,PT,38.7742,-9.1342,374\n"})
            for icao in ("EHAM", "LROP", "LPPT"):
                mon.standing.airport(icao)
            ident = dict(flight="KLM463", r="PH-BXR", t="B739")
            start = (44.95, 25.55)  # ~30 nm NW of Bucharest
            brg = fw.bearing(*start, 44.5711, 26.0850)
            approach = path(*start, [brg] * 120, [max(14000 - 150 * i, 1400) for i in range(120)], gs=220)
            approach = approach[:next(i for i, st in enumerate(approach)
                                      if fw.haversine_nm(st["lat"], st["lon"], 44.5711, 26.0850) < 5)]
            fly(mon, feed, clock, "48418c", approach, local=False, **ident)
            fly(mon, feed, clock, "48418c", [dict(lat=44.5711, lon=26.0850, alt_baro="ground", gs=10, track=90)] * 4,
                local=False, **ident)
            got[case] = (mon.schedule.by_callsign["KLM463"].stop, "DIVERSION" in kinds(alerts, "48418c"))
        self.assertEqual(got, {"tag": ("OTP", False), "inbound": ("", True), "off the way": ("LIS", True)})

    KLM_FILES = {"airports/schema-01/E/EH.csv": "Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,Longitude,AltitudeFeet\n"
                                                "EHAM,Schiphol,EHAM,AMS,Amsterdam,NL,52.3086,4.7639,-11\n",
                 "airports/schema-01/L/LR.csv": "Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,Longitude,AltitudeFeet\n"
                                                "LROP,Henri Coanda,LROP,OTP,Bucharest,RO,44.5711,26.0850,314\n",
                 "airports/schema-01/L/LC.csv": "Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,Longitude,AltitudeFeet\n"
                                                "LCLK,Larnaca,LCLK,LCA,Larnaca,CY,34.8751,33.6249,8\n"}

    def land_at_otp(self, mon, feed, clock, hx, ident):
        start = (44.95, 25.55)  # ~30 nm NW of Bucharest
        approach = path(*start, [fw.bearing(*start, 44.5711, 26.0850)] * 120,
                        [max(14000 - 150 * i, 1400) for i in range(120)], gs=220)
        approach = approach[:next(i for i, st in enumerate(approach)
                                  if fw.haversine_nm(st["lat"], st["lon"], 44.5711, 26.0850) < 5)]
        fly(mon, feed, clock, hx, approach, local=False, **ident)
        fly(mon, feed, clock, hx, [dict(lat=44.5711, lon=26.0850, alt_baro="ground", gs=10, track=90)] * 4,
            local=False, **ident)

    def test_a_route_with_stops_stops_anywhere_on_the_way(self):
        """KLM462 TLV -> AMS, listed LLBG-LCLK-EHAM, landed at Bucharest: KLM's stop moved there (KL463, 24 Oct), the
        route data keeps Larnaca; a DIVERSION on 26 Oct 2026. A route with no stop listed still alerts."""
        got = {}
        for route in ("LLBG-LCLK-EHAM", "LLBG-EHAM"):
            mon, feed, clock, alerts = self.setup([])
            feed.files.update(self.KLM_FILES)
            feed.files["routes/schema-01/K/KLM-all.csv"] = ("Callsign,Code,Number,AirlineCode,AirportCodes\n"
                                                           f"KLM462,KLM,462,KLM,{route}\n")
            for icao in ("EHAM", "LROP", "LCLK"):
                mon.standing.airport(icao)
            ident = dict(flight="KLM462", r="PH-BXT", t="B739")
            tlv = (32.0114, 34.8867)
            fly(mon, feed, clock, "4841a6", [dict(lat=tlv[0], lon=tlv[1], alt_baro="ground", gs=5, track=320)] * 3,
                **ident)
            fly(mon, feed, clock, "4841a6", path(*tlv, [320] * 6, [1500 + 800 * i for i in range(6)], gs=200), **ident)
            clock.t += 2 * 3600
            self.land_at_otp(mon, feed, clock, "4841a6", ident)
            got[route] = "DIVERSION" in kinds(alerts, "4841a6")
        self.assertEqual(got, {"LLBG-LCLK-EHAM": False, "LLBG-EHAM": True})

    def test_an_early_arrival_on_its_way_waits_for_its_board_time(self):
        """IB1873 MAD -> TLV on the ground at Athens 01:41, landed at TLV 04:16, due 04:30 (26 Oct 2026): a stop
        neither the board nor the route data names. A DIVERSION only once it can no longer make its time."""
        got = {}
        for case in ("flies on", "stays", "due soon"):
            due = 1 if case == "due soon" else 4  # Bucharest - TLV ~870 nm: ~2.4 h with the turn-round
            mon, feed, clock, alerts = self.setup([board_row("KL", "463", "A", local_time(Clock(), due), "AMS")])
            feed.files.update(self.KLM_FILES)
            feed.files["routes/schema-01/K/KLM-all.csv"] = ("Callsign,Code,Number,AirlineCode,AirportCodes\n"
                                                           "KLM463,KLM,463,KLM,EHAM-LLBG\n")
            for icao in ("EHAM", "LROP"):
                mon.standing.airport(icao)
            ident = dict(flight="KLM463", r="PH-BXR", t="B739")
            self.land_at_otp(mon, feed, clock, "48418c", ident)
            first = "DIVERSION" in kinds(alerts, "48418c")
            if case == "flies on":
                fly(mon, feed, clock, "48418c", path(44.5711, 26.0850, [150] * 6, [1500 + 800 * i for i in range(6)],
                                                     gs=200), local=False, **ident)
            clock.t += 3 * 3600
            fly(mon, feed, clock, "48418c", [dict(lat=44.5711, lon=26.0850, alt_baro="ground", gs=0, track=90)] * 2
                if case != "flies on" else path(40.0, 30.0, [150] * 3, [30000] * 3, gs=450), local=False, **ident)
            got[case] = (first, "DIVERSION" in kinds(alerts, "48418c"))
        self.assertEqual(got, {"flies on": (False, False), "stays": (False, True), "due soon": (True, True)})

    def test_board_origin_before_the_routes_leg_is_a_stop(self):
        """BZ131 BGY -> TLV (board), BBG131 LIRF-LLBG: a tag flight via Rome, two DIVERSIONs on landing there."""
        got = {}
        for origin in ("LIRF", "LIME"):
            mon, feed, clock, alerts = self.setup([board_row("BZ", "131", "A", local_time(Clock(), 2), "BGY")])
            feed.files.update({"routes/schema-01/B/BBG-all.csv": "Callsign,Code,Number,AirlineCode,AirportCodes\n"
                                                                  f"BBG131,BBG,131,BBG,{origin}-LLBG\n",
                               "airports/schema-01/L/LI.csv": "Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,"
                                                              "Longitude,AltitudeFeet\nLIRF,Fiumicino,LIRF,FCO,Rome,IT,"
                                                              "41.8003,12.2389,13\nLIME,Orio al Serio,LIME,BGY,"
                                                              "Bergamo,IT,45.6739,9.7042,782\n"})
            mon.standing.airport("LIME")
            ident = dict(flight="BBG131", r="9H-SHO", t="B738")
            start = (42.6, 11.0)
            brg = fw.bearing(*start, 41.8003, 12.2389)
            approach = path(*start, [brg] * 120, [max(14000 - 150 * i, 1400) for i in range(120)], gs=220)
            approach = approach[:next(i for i, st in enumerate(approach)
                                      if fw.haversine_nm(st["lat"], st["lon"], 41.8003, 12.2389) < 5)]
            fly(mon, feed, clock, "4d21e6", approach, local=False, **ident)
            fly(mon, feed, clock, "4d21e6", [dict(lat=41.8003, lon=12.2389, alt_baro="ground", gs=10, track=90)] * 4,
                local=False, **ident)
            got[origin] = "DIVERSION" in kinds(alerts, "4d21e6")
        self.assertEqual(got, {"LIRF": False, "LIME": True})

    def ramon(self):
        mon, feed, clock, alerts = self.setup([board_row("IZ", "807", "D", local_time(Clock(), 0.5), "ETM")])
        feed.files.update({"routes/schema-01/A/AIZ-all.csv": "Callsign,Code,Number,AirlineCode,AirportCodes\n"
                                                              "AIZ802,AIZ,802,AIZ,LLER-LLBG\nAIZ807,AIZ,807,AIZ,LLBG-LLER\n",
                           "airports/schema-01/L/LL.csv": ROUTE_FILES["airports/schema-01/L/LL.csv"]
                           + "LLER,Ramon,LLER,ETM,Eilat,IL,29.7236,35.0114,288\n"})
        return mon, feed, clock, alerts

    def test_arrival_callsign_taking_off_from_tlv_is_a_departure(self):
        """4X-AGY landed at TLV as AIZ804, became AIZ802 (LLER-LLBG) on the ground and flew to Ramon with it over
        the Dead Sea: "TLV arrival heading 185 deg, 151 deg away from TLV" (20 Oct 2026)."""
        got = {}
        for seen_leaving in (True, False):
            mon, feed, clock, alerts = self.ramon()
            ident = dict(flight="AIZ802", r="4X-AGY", t="A320")
            tlv = (32.0114, 34.8867)
            if seen_leaving:
                fly(mon, feed, clock, "738447", [dict(lat=tlv[0], lon=tlv[1], alt_baro="ground", gs=5, track=140)] * 3,
                    **ident)
            climb = path(*tlv, [140] * 40 + [180] * 80, [min(1500 + 400 * i, 17000) for i in range(120)], gs=300)
            fly(mon, feed, clock, "738447", climb if seen_leaving else climb[30:], **ident)
            got[seen_leaving] = (mon.classify(mon.tracks["738447"])[0], "OFF_COURSE" in kinds(alerts, "738447"))
        # first heard far out, nothing says where it came from: an arrival flying away (FarEndTests.positioning)
        self.assertEqual(got, {True: ("DEP?", False), False: ("ARR", True)})

    def test_departure_silent_descending_into_its_destination(self):
        """IZ807 TLV -> Ramon went silent at 12,900 ft 37 nm out (coverage near Eilat): LOST_CONTACT."""
        mon, feed, clock, alerts = self.ramon()
        ident = dict(flight="AIZ807", r="4X-AGY", t="A320")
        for i in range(6):  # (one of two aircraft going silent would look like a feed glitch)
            feed.local.add(f"99999{i}")
            feed.aircraft[f"99999{i}"] = airborne(32.5 + 0.1 * i, 34.5, 10000, flight=f"LOC{i}")
        fly(mon, feed, clock, "738447", path(30.95, 35.10, [185] * 40, [20000 - 180 * i for i in range(40)],
                                             gs=300), **ident)
        feed.local.discard("738447")
        del feed.aircraft["738447"]
        for _ in range(30):
            step(mon, clock)
        self.assertNotIn("LOST_CONTACT", kinds(alerts, "738447"))

    def test_turn_climbing_out_of_its_origin_far_away(self):
        """ELY552 SOF -> TLV, 19 Oct 2026: "track 359 -> 126 deg" at 17,825 ft, 10 nm from Sofia."""
        tracks = [0] * 6 + [7.5 * i for i in range(1, 17)] + [126] * 16
        alts = [12000 + 150 * i for i in range(len(tracks))]
        got = {}
        for other in ("SOF", "VIE"):
            mon, feed, clock, alerts = self.setup([board_row("LY", "552", "A", local_time(Clock(), 3), other)])
            fly(mon, feed, clock, "738057", path(42.45, 23.65, tracks, alts), local=False, flight="ELY552",
                r="4X-EHI", t="B739")
            got[other] = "COURSE_CHANGE" in kinds(alerts)
        self.assertEqual(got, {"SOF": False, "VIE": True})


def aero_post(clock, text, n=1, minutes_ago=3):
    created = datetime.fromtimestamp(clock() - 60 * minutes_ago, timezone.utc)
    return {"post": {"uri": f"at://did:plc:x/app.bsky.feed.post/3sq{n}", "author": {"handle": "spotterintel.bsky.social"},
                     "record": {"text": text, "createdAt": created.strftime("%Y-%m-%dT%H:%M:%S.000Z")}}}


OS82 = ("Emergency reported for Austrian flight.\nAustrian Airlines OS82 | A320\nTel-aviv (TLV) \u2192 Vienna (VIE)\n\n"
        "Squawked 7700. Monitoring.")


class LegEndAndLightAircraftTests(unittest.TestCase):
    def test_a_departure_landed_away_is_no_longer_followed(self):
        """A6-AEO flew EY596 TLV -> AUH, then ETD238 to Bangalore still followed: its approach turn there was a
        "course reversal 2,594 nm E of TLV" (22 Oct 2026)."""
        mon, feed, clock, alerts = with_board([board_row("LY", "541", "D", local_time(mon_clock(), 0), "ATH")])
        feed.local = {"999999"}
        feed.aircraft["999999"] = airborne(32.5, 34.5, 10000, flight="LOC1")
        ident = dict(flight="ELY541", r="4X-EKA", t="B738")
        start = fw.destination(37.936, 23.944, 120, 12)  # 12 nm out on final into Athens
        fly(mon, feed, clock, "738003", path(*start, [300] * 18, [4000 - 200 * i for i in range(18)], gs=180),
            local=False, **ident)
        self.assertTrue(mon.tracks["738003"].followed)
        fly(mon, feed, clock, "738003", [dict(lat=37.936, lon=23.944, alt_baro="ground", gs=10, track=0)] * 3,
            local=False, **ident)
        self.assertFalse(mon.tracks["738003"].followed)
        n = len(feed.calls)
        for _ in range(12):
            step(mon, clock)
        self.assertFalse([q for m, q in feed.calls[n:] if "/hex/" in q and "738003" in q])
        self.assertEqual(alerts, [])

    def test_light_jet_alerts_once_whatever_its_callsign(self):
        """BDK12 (survey lines west of Israel, 21-22 Oct 2026): 18 Telegram messages in 3 h. Its callsign looks
        like an airline's, so the pattern-flying rule (one turn alert in 2 h) did not apply. Its wake category is A3
        (large): BDK04, the same aircraft, 19 messages on 28 Oct 2026 - the rule had judged it large."""
        mon, feed, clock, alerts = make(["--no-routes"])
        lap = [115] * 30 + [296] * 30  # (out and back on the same line: a reversal each way, 10 min a lap)
        fly(mon, feed, clock, "738685", path(32.6, 33.2, lap * 4, [25000] * 240, gs=400), flight="BDK12", r="", t="",
            category="A3")
        self.assertEqual(kinds(alerts), ["COURSE_CHANGE"])
        self.assertIn("light / private aircraft with no known route: no further alerts", alerts[0]["message"])
        self.assertTrue(mon.tracks["738685"].mil_held)  # (in its follow-up: "not alerted (light, ...)")

    def test_widebody_types_with_letters_are_not_light(self):
        """UAE46 (A6-ENV, B77W) with no route yet: "light / private aircraft" in its GPS_SPOOFING alert, 28 Oct 2026 -
        the type pattern took only digits after B7 (B77W, B77L, B78X) and A3x (A35K)."""
        for actype in ("B77W", "B77L", "B78X", "B744", "A35K", "A388", "A20N", "B38M", "B3XM"):
            self.assertTrue(fw.LARGE_TYPES.fullmatch(actype), actype)
        for actype in ("GLF5", "H25B", "C172", "P750", "GLEX"):
            self.assertFalse(fw.LARGE_TYPES.fullmatch(actype), actype)
        mon, feed, clock, alerts = make(["--no-routes"])
        fly(mon, feed, clock, "896318", path(31.7, 35.0, [100] * 4, [37000] * 4, gs=480), flight="UAE46", r="A6-ENV",
            t="B77W")
        self.assertFalse(mon.light(mon.tracks["896318"]))

    def test_a_kind_telegram_skips_does_not_use_up_its_one_alert(self):
        mon, feed, clock, alerts = make(["--no-routes"])
        ident = dict(flight="HEZ652", r="4X-CZF", t="H25B")
        states = path(32.6, 33.6, [90] * 8, [19000] * 8, dt=30, gs=400)
        lat, lon = fw.destination(states[4]["lat"], states[4]["lon"], 0, 8)
        states[4] = {**states[4], "lat": lat, "lon": lon}  # one position off the track and back: POSITION_JUMP
        fly(mon, feed, clock, "73934e", states, dt=30, **ident)
        last = states[-1]
        dive = path(*fw.destination(last["lat"], last["lon"], 90, 3.3), [90] * 8,
                    [19000 - 1000 * i for i in range(8)], gs=250)  # -6,000 ft/min at 250 kt: ~13 deg
        fly(mon, feed, clock, "73934e", dive, **ident)
        self.assertEqual(kinds(alerts), ["POSITION_JUMP", "VERTICAL_RATE"])
        self.assertIn("light / private aircraft", alerts[1]["message"])

    def test_a_jump_plane_keeps_its_ride_alert(self):
        """4XDAN: an alert before its first lift must not hold back the ride's one steep-descent alert."""
        mon, feed, clock, alerts = make(["--no-routes"])
        ident = dict(flight="4XDAN", r="4X-DAN", t="P750", category="A1")
        fly(mon, feed, clock, "739215", [dict(airborne(31.5, 35.4, 9000, gs=120), squawk="7600")] * 2, **ident)
        fly(mon, feed, clock, "739215", [dict(airborne(31.5, 35.4, 9000, gs=120), squawk="1200")] * 2, **ident)
        clock.t += 600
        fly(mon, feed, clock, "739215", lift(), **ident)
        self.assertEqual(kinds(alerts), ["EMERGENCY", "VERTICAL_RATE"])
        self.assertIn("probably a skydiving ride", alerts[1]["message"])
        self.assertFalse(mon.light(mon.tracks["739215"]))

    def test_impossible_speed_of_a_bizjet_is_no_sharp_turn(self):
        """HEZ652 (4X-CZF, Hawker 800) "134 -> 50 deg in 30 s at 728 kt (~62 deg bank)" at FL190 in GPS jamming."""
        mon, feed, clock, alerts = make(["--no-routes"])
        fly(mon, feed, clock, "73934e", path(32.87, 33.88, [134] * 6 + [106, 78, 50] + [50] * 6, [19000] * 15,
                                             gs=728), flight="HEZ652", r="4X-CZF", t="H25B")
        self.assertNotIn("SHARP_TURN", kinds(alerts))


class AeroSignalsTests(unittest.TestCase):
    """A second source of emergency squawks: AeroSignals' posts on Bluesky (OS82, 18 Oct 2026: 7700 over Bulgaria
    while adsb.lol refused nearly every poll; nothing alerted)."""

    def setup(self):
        mon, feed, clock, alerts = make(["--aerosignals-interval", "120", "--rate", "600", "--rate-max", "6000"])
        posts = []
        mon.notifier.send_post = posts.append
        feed.local = {"999999"}
        feed.aircraft["999999"] = airborne(32.5, 34.5, 10000, flight="LOC1")
        return mon, feed, clock, alerts, posts

    def test_parses_the_template(self):
        c = fw.aero_case(OS82)
        self.assertEqual((c["squawk"], c["iata"], c["number"], c["from"], c["to"]), ("7700", "OS", "82", "TLV", "VIE"))
        self.assertIsNone(fw.aero_case("Update: Austrian Airlines OS82 has landed safely."))

    def test_squawk_our_polls_missed_is_alerted_once_on_its_aircraft(self):
        mon, feed, clock, alerts, posts = self.setup()
        feed.bsky = [aero_post(clock, OS82)]
        fly(mon, feed, clock, "440604", path(43.8, 24.9, [270] * 30, [37000] * 30), local=False, flight="AUA82",
            r="OE-LZE", t="A320")  # squawk 1000 in our reports: the 7700 fell between them
        hits = [a for a in alerts if a["kind"] == "EMERGENCY"]
        self.assertEqual(len(hits), 1, kinds(alerts))
        self.assertIn("reported by AeroSignals", hits[0]["message"])
        self.assertIn("bsky.app/profile/spotterintel.bsky.social/post/3sq1", hits[0]["message"])
        self.assertEqual(hits[0]["hex"], "440604")
        self.assertEqual(posts, [])

    def test_squawk_we_alerted_ourselves_is_not_repeated(self):
        mon, feed, clock, alerts, posts = self.setup()
        fly(mon, feed, clock, "440604", path(43.8, 24.9, [270] * 3, [37000] * 3, squawk="7700"), local=False,
            flight="AUA82", r="OE-LZE", t="A320")
        clock.t += 60  # (the post a few minutes later)
        feed.bsky = [aero_post(clock, OS82, minutes_ago=0)]
        fly(mon, feed, clock, "440604", path(43.8, 24.6, [270] * 20, [37000] * 20, squawk="7700"), local=False,
            flight="AUA82", r="OE-LZE", t="A320")
        self.assertEqual(kinds(alerts).count("EMERGENCY"), 1)
        self.assertNotIn("AeroSignals", " ".join(a["message"] for a in alerts))

    def test_aircraft_not_in_our_feeds_is_announced_as_the_post(self):
        mon, feed, clock, alerts, posts = self.setup()
        feed.bsky = [aero_post(clock, OS82)]
        for _ in range(80):  # 13 min
            step(mon, clock)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["flight"], "OS82")
        self.assertNotIn("EMERGENCY", kinds(alerts))

    def test_other_flights_and_old_posts_are_left_out(self):
        mon, feed, clock, alerts, posts = self.setup()
        other = OS82.replace("Tel-aviv (TLV)", "Dubai (DXB)")
        feed.bsky = [aero_post(clock, other, n=1), aero_post(clock, OS82, n=2, minutes_ago=4 * 60)]
        for _ in range(80):
            step(mon, clock)
        self.assertEqual((posts, kinds(alerts)), ([], []))
        self.assertEqual(mon.aero_pending, {})


class TelegramThreadTests(unittest.TestCase):
    """A flight's Telegram messages form one thread, closed by a follow-up with the map of the whole flight
    (IZ992, 20 Oct 2026: three messages, each map with the track flown by then)."""

    class Sent:
        def __init__(self):
            self.calls = []

        def post(self, url, **kw):
            self.calls.append(kw["json"])
            return mock.Mock(json=lambda: {"ok": True, "result": {"message_id": len(self.calls)}})

    def notifier(self, saved):
        args = fw.parse_args(["--jsonl", "", "--posts", "", "--turn-zones", "", "--telegram-posts", saved,
                              "--telegram-skip", "LOST_CONTACT"])
        http = self.Sent()
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "@c"}):
            n = fw.Notifier(args, http, fw.Announcer(args, out=lambda text: None), console=lambda text: None)
        return n, http

    def test_later_messages_reply_to_the_first_and_a_restart_keeps_the_thread(self):
        saved = os.path.join(tempfile.mkdtemp(), "posts_telegram.jsonl")
        n, http = self.notifier(saved)
        kw = dict(remote=False, traffic="ARR", route=None)
        n.send(alert_rec("EMERGENCY", "squawk 7700 (GENERAL EMERGENCY)", **kw))
        n.send(alert_rec("OFF_COURSE", "TLV arrival heading 104 deg, 169 deg away from TLV", minute=2, **kw))
        self.assertNotIn("reply_parameters", http.calls[0])
        self.assertEqual(http.calls[1]["reply_parameters"]["message_id"], 1)
        again, _ = self.notifier(saved)
        self.assertEqual((again.tg_threads["8965d1"]["id"], again.tg_threads["8965d1"]["n"]), (1, 2))
        n.follow_up("8965d1", 0, [{"text": "Update"}])
        n.follow_up("8965d1", 0, [{"text": "Update"}])
        self.assertEqual(len(http.calls), 3)
        self.assertEqual(http.calls[2]["reply_parameters"]["message_id"], 1)
        self.assertTrue(self.notifier(saved)[0].tg_threads["8965d1"]["done"])

    def test_follow_up_after_the_last_message_with_the_whole_flight(self):
        mon, feed, clock, alerts = make(["--viewer-url", "https://x/flight_map.html"])
        ups = []
        mon.notifier.follow_up = lambda h, when, seg: ups.append((h, when, seg))
        fly(mon, feed, clock, "4d22a0", path(32.5, 33.0, [90] * 3, [30000] * 3, squawk="7700"), flight="AIZ992")
        self.assertEqual(kinds(alerts), ["EMERGENCY"])
        mon.notifier.tg_threads["4d22a0"] = {"id": 7, "t": clock(), "last": clock(), "n": 1}
        fly(mon, feed, clock, "4d22a0", path(32.5, 33.2, [90] * 60, [30000] * 60, squawk="7700"), flight="AIZ992")
        self.assertEqual(ups, [])  # 10 min: not yet
        for _ in range(240):  # 40 min more
            step(mon, clock)
        self.assertEqual(len(ups), 1)
        text = "".join(s.get("text") or s["link"] for s in ups[0][2])
        self.assertIn("Update: ", text)
        self.assertIn("emergency", text)
        self.assertIn("Map of the flight", text)
        url = next(s["url"] for s in ups[0][2] if s.get("link") == "Map of the flight")
        self.assertIn("a=EMERGENCY~", url)

    def test_aerosignals_then_our_feed_is_one_emergency(self):
        mon, feed, clock, alerts = make()
        fly(mon, feed, clock, "4d22a0", path(32.5, 33.0, [90] * 3, [30000] * 3), flight="AIZ992")
        t = mon.tracks["4d22a0"]
        mon.alert(t, "EMERGENCY", "squawk 7700 (GENERAL EMERGENCY) reported by AeroSignals", key="EMERGENCY:7700",
                  force=True)
        fly(mon, feed, clock, "4d22a0", path(32.5, 33.1, [90] * 3, [30000] * 3, squawk="7700"), flight="AIZ992")
        self.assertEqual(kinds(alerts), ["EMERGENCY"])


class SingleInstanceTests(unittest.TestCase):
    def test_second_monitor_is_refused_until_the_first_ends(self):
        path = os.path.join(tempfile.mkdtemp(), ".flight_watch.lock")
        first = fw.single_instance(path)
        with self.assertRaises(SystemExit) as e:
            fw.single_instance(path)
        self.assertIn(f"pid {os.getpid()}", str(e.exception.code))
        first.close()  # (the process ending)
        fw.single_instance(path).close()
