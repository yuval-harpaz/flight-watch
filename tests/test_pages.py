"""The flight pages (docs/flights.html, docs/flight_map.html) and their local helper.

The pages' board logic (docs/flightboard.js) must match the monitor's (flight_watch.Schedule):
the same rows give the same flights, operating carriers, callsigns and times. These tests run
the JavaScript under node (skipped when node is not installed) and compare.

Run with:  python -m unittest discover -s tests
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
import urllib.request
from datetime import datetime

import requests

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import flight_watch as fw  # noqa: E402

JS = os.path.join(ROOT, "docs", "flightboard.js")
NODE = shutil.which("node")


def node(expr: str, data=None):
    """Evaluate `expr` with FB (flightboard.js) and DATA (json) in node; returns its JSON value."""
    script = (f"const FB = require({json.dumps(os.path.abspath(JS))});"
              f"const DATA = {json.dumps(data)};"
              f"process.stdout.write(JSON.stringify((() => {expr})()));")
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "TZ": "UTC"})
    if out.returncode:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


def row(op, num, d, sched, loc, status="ON TIME", est=None, name=""):
    return {"CHOPER": op, "CHFLTN": num, "CHAORD": d, "CHSTOL": sched, "CHPTOL": est or sched,
            "CHLOC1": loc, "CHLOC1D": loc, "CHLOC1T": loc, "CHRMINE": status, "CHOPERD": name}


ROWS = [
    row("LY", "025", "D", "2026-10-08T10:00:00", "EWR"),           # LY25 + DL7441 codeshare
    row("DL", "7441", "D", "2026-10-08T10:00:00", "EWR"),
    row("EY", "595", "A", "2026-10-08T05:50:00", "AUH", "LANDED"),
    row("LY", "9604", "A", "2026-10-08T05:50:00", "AUH", "LANDED"),
    row("6H", "889", "D", "2026-10-08T08:00:00", "BUS", "DEPARTED", "2026-10-08T08:39:00"),
    row("U8", "216", "D", "2026-10-08T07:50:00", "SKG", "DEPARTED"),  # unmapped airline code
    row("IZ", "598", "A", "2026-10-08T06:50:00", "HKT", "CANCELED"),
    row("LY", "001", "A", "2026-10-08T23:30:00", "JFK", "NOT FINAL", "2026-10-09T00:10:00"),
    row("W6", "2328", "A", "2026-10-08T12:00:00", "BUD"),
    row("FZ", "1073", "A", "2026-10-25T09:05:00", "DXB"),           # after the change back to winter time
]


@unittest.skipUnless(NODE, "node not installed")
class BoardLogicMatchesPython(unittest.TestCase):
    def test_airline_table_is_the_monitors(self):
        self.assertEqual(node("FB.AIRLINE_ICAO"), fw.AIRLINE_ICAO)

    def test_same_flights_callsigns_and_times(self):
        js = {f["id"]: f for f in node("FB.flights(DATA)", ROWS)}
        sched = fw.Schedule(None, dict(fw.AIRLINE_ICAO), 1e9, 24 * 30)
        sched.load(ROWS, datetime(2026, 10, 8, 9, 0, tzinfo=sched.tz))
        self.assertTrue(sched.flights)
        for sf in sched.flights:  # every flight the monitor keeps, the page has the same way
            d = "A" if sf.direction == "ARR" else "D"
            f = next(f for f in js.values() if f["dir"] == sf.direction and f["flight"] == sf.flight)
            self.assertEqual(f["id"].split("|")[0], d)
            self.assertEqual(tuple(f["callsigns"]), sf.callsigns, sf.flight)
            self.assertEqual(tuple(f["altCallsigns"]), sf.alt_callsigns, sf.flight)
            self.assertEqual(tuple(f["codeshares"]), sf.codeshares, sf.flight)
            self.assertEqual(f["est"], sf.ts, sf.flight)          # epoch seconds, Israel time read alike
        # the page also lists what the monitor skips: cancelled, landed, unmapped airlines
        self.assertEqual(len(js), 8)  # 10 rows, two of them codeshares
        self.assertEqual(js["D|2026-10-08T07:50:00|SKG"]["callsigns"], [])
        self.assertEqual(js["D|2026-10-08T10:00:00|EWR"]["flight"], "LY25")
        self.assertEqual(js["A|2026-10-08T05:50:00|AUH"]["codeshares"], ["LY9604"])

    def test_israel_time_across_dst_changes(self):
        # 25 Oct 01:30 happens twice (summer time ends); 27 Mar 02:30 never happens (it starts)
        for s in ("2026-10-08T10:30:00", "2026-10-25T01:30:00", "2026-10-25T09:05:00", "2026-03-27T02:30:00",
                  "2026-03-27T03:00:00", "2026-01-15T00:00:00"):
            py = datetime.fromisoformat(s).replace(tzinfo=fw.ZoneInfo("Asia/Jerusalem")).timestamp()
            self.assertEqual(node(f"FB.israelEpoch({json.dumps(s)})"), py, s)

    def test_geometry_matches(self):
        self.assertAlmostEqual(node("FB.haversineNm(32.0114, 34.8867, 25.2528, 55.3644)"),
                               fw.haversine_nm(32.0114, 34.8867, 25.2528, 55.3644), places=6)
        self.assertAlmostEqual(node("FB.bearing(32.0114, 34.8867, 41.2753, 28.7519)"),
                               fw.bearing(32.0114, 34.8867, 41.2753, 28.7519), places=6)


@unittest.skipUnless(NODE, "node not installed")
class Situations(unittest.TestCase):
    def label(self, d, status, sched, est, now):
        f = {"dir": d, "status": status, "sched": fw_epoch(sched), "est": fw_epoch(est)}
        return node(f"FB.situation(DATA.f, {fw_epoch(now)})", {"f": f})

    def test_statuses(self):
        s = self.label("ARR", "ON TIME", "10:00", "10:40", "09:00")
        self.assertEqual((s["label"], s["cls"], s["delay"]), ("Delayed landing +40 min", "late", 40))
        self.assertEqual(self.label("ARR", "ON TIME", "10:00", "10:00", "10:30")["cls"], "overdue")
        self.assertEqual(self.label("ARR", "LANDED", "10:00", "10:30", "10:40")["label"], "Landed late +30 min")
        self.assertEqual(self.label("ARR", "ON TIME", "10:00", "09:45", "09:00")["label"], "Early -15 min")
        self.assertIn("Late to depart", self.label("DEP", "ON TIME", "10:00", "10:00", "10:25")["label"])
        self.assertEqual(self.label("DEP", "DELAYED", "10:00", "10:50", "09:00")["label"], "Late departure +50 min")
        self.assertEqual(self.label("DEP", "DEPARTED", "10:00", "10:05", "10:10")["label"], "Departed")
        self.assertEqual(self.label("DEP", "CANCELED", "10:00", "10:00", "09:00")["cls"], "cancel")


def fw_epoch(hhmm: str) -> float:
    return datetime.fromisoformat(f"2026-10-08T{hhmm}:00").replace(tzinfo=fw.ZoneInfo("Asia/Jerusalem")).timestamp()


class FakeUpstream:
    """Stands in for requests.Session inside serve.Relay's Http."""
    def __init__(self):
        self.calls, self.status = [], 200

    def request(self, method, url, timeout=None, **kw):
        self.calls.append(url)
        r = requests.Response()
        r.status_code, r._content, r.url = self.status, b'{"ac": [], "now": 1}', url
        r.headers["Content-Type"] = "application/json"
        return r


class LocalHelper(unittest.TestCase):
    def setUp(self):
        import http.server
        import serve
        self.relay = serve.Relay(rate=600, rate_max=600)
        self.relay.http.min_gap = 0
        self.up = FakeUpstream()
        self.relay.http.s = self.up
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.handler(self.relay))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback, no proxy

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path):
        try:
            r = self.opener.open(self.base + path, timeout=10)
            return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_relays_the_feed_with_cors_and_caches(self):
        code, headers, body = self.get("/v2/callsign/ELY315")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(self.up.calls, ["https://api.adsb.lol/v2/callsign/ELY315"])
        self.get("/v2/callsign/ELY315")  # within the cache time: no second upstream request
        self.assertEqual(len(self.up.calls), 1)
        self.get("/data/traces/71/trace_recent_738071.json")
        self.assertEqual(self.up.calls[-1], "https://adsb.lol/data/traces/71/trace_recent_738071.json")

    def test_429_cools_down_instead_of_hammering(self):
        self.up.status = 429
        self.assertEqual(self.get("/v2/hex/abc")[0], 429)
        self.up.status = 200
        code, _, body = self.get("/v2/hex/def")  # still cooling down: answered without asking upstream
        self.assertEqual(code, 429)
        self.assertIn(b"cooling down", body)
        self.assertEqual(len(self.up.calls), 1)

    def test_serves_the_pages(self):
        for path in ("/", "/flights.html", "/flight_map.html", "/flightboard.js"):
            code, _, body = self.get(path)
            self.assertEqual(code, 200, path)
        self.assertIn(b"flightboard.js", self.get("/")[2])


if __name__ == "__main__":
    unittest.main()
