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
    """Evaluate `expr` with FB (flightboard.js) and DATA (json) in node; returns its JSON value
    (awaited when it is a promise)."""
    script = (f"const FB = require({json.dumps(os.path.abspath(JS))});"
              f"const DATA = {json.dumps(data)};"
              f"Promise.resolve((() => {expr})()).then(v => process.stdout.write(JSON.stringify(v)));")
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

    def test_current_leg_starts_after_a_stop_even_one_not_seen(self):
        t = 1791400000
        out_leg = [{"t": t + i * 60, "lat": 32.0 + i * 0.1, "lon": 34.8 - i * 0.1, "alt": 5000 + 1000 * i} for i in range(5)]
        # unseen stop: 3.6 h later, 330 nm away (~92 kt on average), then the way back
        back = [{"t": t + 4 * 60 + 13000 + i * 60, "lat": 34.8 - i * 0.1, "lon": 29.7 + i * 0.1, "alt": 39000} for i in range(3)]
        self.assertEqual(node("FB.currentLeg(DATA).length", out_leg + back), 3)
        # a 30 min coverage gap at cruise speed is the same leg
        gap = [{"t": t + 4 * 60 + 1800, "lat": 32.4 - 3.5, "lon": 34.4 + 3.5, "alt": 37000}]  # ~240 nm in 30 min
        self.assertEqual(node("FB.currentLeg(DATA).length", out_leg + gap), 6)
        ground = [dict(out_leg[0], alt="ground")]
        self.assertEqual(node("FB.currentLeg(DATA).length", ground + out_leg), 5)

    def test_leg_of_a_landed_flight(self):
        t = 1791400000
        def p(i, lat, lon, alt):
            return {"t": t + i * 60, "lat": lat, "lon": lon, "alt": alt}
        out = [p(i, 32.0 + i * 0.2, 34.85 - i * 0.2, 3000 + 3000 * i) for i in range(6)]        # TLV departure
        back = [p(300 + i, 33.0 - i * 0.2, 33.85 + i * 0.2, 30000 - 5000 * i) for i in range(6)]  # arrival 5 h later
        ground = [p(306, 32.0, 34.88, "ground")]
        pts = out + back + ground
        legs = node("FB.legs(DATA).map(l => l.length)", pts)
        self.assertEqual(legs, [6, 6])                       # split by the unseen stop; ground ends the arrival
        seen = t + 306 * 60                                  # the callsign last heard taxiing in
        arr = node(f"FB.legAt(DATA, {seen}, 'ARR')[0].t", pts)
        self.assertEqual(arr, back[0]["t"])
        dep = node(f"FB.legAt(DATA, {t}, 'DEP')[0].t", pts)
        self.assertEqual(dep, out[0]["t"])

    def test_geometry_matches(self):
        self.assertAlmostEqual(node("FB.haversineNm(32.0114, 34.8867, 25.2528, 55.3644)"),
                               fw.haversine_nm(32.0114, 34.8867, 25.2528, 55.3644), places=6)
        self.assertAlmostEqual(node("FB.bearing(32.0114, 34.8867, 41.2753, 28.7519)"),
                               fw.bearing(32.0114, 34.8867, 41.2753, 28.7519), places=6)


@unittest.skipUnless(NODE, "node not installed")
class BoardArchive(unittest.TestCase):
    """A past board is rebuilt from over.org.il's archive: the latest state of each row seen by then."""
    def test_snapshot_query(self):
        t = datetime(2026, 9, 30, 8, 45, tzinfo=fw.ZoneInfo("Asia/Jerusalem")).timestamp()
        sql = node(f"FB.snapshotSQL({t}, 1000, 2000)")
        self.assertIn('SELECT DISTINCT ON ("CHOPER","CHFLTN","CHAORD","CHSTOL")', sql)
        self.assertIn("first_seen <= '2026-09-30T05:45:00.000Z'", sql)        # the moment, in UTC
        self.assertIn(""""CHSTOL" >= '2026-09-29T08:45:00'""", sql)           # rows for a day before ...
        self.assertIn(""""CHSTOL" < '2026-10-03T08:45:00'""", sql)            # ... to 3 days after, Israel time
        self.assertTrue(sql.endswith("ORDER BY \"CHOPER\",\"CHFLTN\",\"CHAORD\",\"CHSTOL\",first_seen DESC "
                                     "LIMIT 1000 OFFSET 2000"), sql)
        for f in fw.FLYDATA_FIELDS.split(","):  # every column the monitor reads
            self.assertIn(f'"{f}"', sql)

    def test_pages_until_a_short_page_and_retries_once(self):
        out = node("""(async () => {
            const urls = []; let failed = false;
            const fake = async u => {
              urls.push(decodeURIComponent(u.split("?sql=")[1]));
              if (urls.length === 2 && !failed) { failed = true; throw new Error("Failed to fetch"); }
              const n = urls.length <= 3 ? 1000 : 7;   // two full pages (one retried), then a short one
              return {ok: true, json: async () => ({result: {records: Array(n).fill(DATA)}})};
            };
            const rows = await FB.fetchBoardAt(FB.HISTORY_START + 86400, fake);
            let early = null;
            try { await FB.fetchBoardAt(FB.HISTORY_START - 60, fake); } catch (e) { early = e.message; }
            return {n: rows.length, offsets: urls.map(u => +u.split("OFFSET ")[1]), early};
        })()""", ROWS[0])
        self.assertEqual(out["n"], 2007)
        self.assertEqual(out["offsets"], [0, 1000, 1000, 2000])
        self.assertIn("10 Apr 2026", out["early"])
        self.assertEqual(len(node("FB.flights(DATA)", [ROWS[0]] * 3)), 1)  # repeated states of a row: one flight


@unittest.skipUnless(NODE, "node not installed")
class Situations(unittest.TestCase):
    def label(self, d, status, sched, est, now):
        f = {"dir": d, "status": status, "sched": fw_epoch(sched), "est": fw_epoch(est)}
        return node(f"FB.situation(DATA.f, {fw_epoch(now)})", {"f": f})

    def test_live_lookup_only_when_it_can_help(self):
        def useful(d, status, est, now):
            return node(f"FB.liveUseful(DATA, {fw_epoch(now)})", {"dir": d, "status": status, "est": fw_epoch(est)})
        self.assertFalse(useful("ARR", "LANDED", "07:15", "09:20"))   # the W64603 case: landed 2 h ago
        self.assertTrue(useful("ARR", "LANDED", "09:00", "09:20"))    # just landed: still taxiing
        self.assertTrue(useful("ARR", "ON TIME", "13:00", "09:00"))   # airborne on a long flight
        self.assertFalse(useful("ARR", "CANCELED", "09:30", "09:00"))
        self.assertFalse(useful("DEP", "ON TIME", "18:00", "09:00"))  # still hours from its time
        self.assertTrue(useful("DEP", "DEPARTED", "07:00", "09:00"))  # may still be on its way
        self.assertTrue(node("FB.blocked(new TypeError('Failed to fetch'))"))     # CORS / offline
        self.assertFalse(node("FB.blocked(new Error('x: HTTP 429'))"))

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


@unittest.skipUnless(NODE, "node not installed")
class CorsWorker(unittest.TestCase):
    """tools/cors_worker.js (Cloudflare Worker): relays only the feed paths the pages use, adds CORS,
    serves a recent answer when adsb.lol says 429, and keeps the callsign -> hex map (/hexof)."""
    def run_worker(self, body):
        """Run `body` (async JS) with the worker `w`, `req(path, origin, method)`, fake `fetch`
        (`UP.status`, `UP.point` aircraft, `calls`), cache (`caches.default`), KV `env.CALLSIGNS`
        and clock (`clock.now`, ms). Returns what the body returns."""
        src = os.path.abspath(os.path.join(ROOT, "tools", "cors_worker.js"))
        script = f"""
          const src = require("fs").readFileSync({json.dumps(src)}, "utf8");
          const clock = {{now: 1791400000000}}; Date.now = () => clock.now;
          console.log = () => {{}};   // the worker's log lines (Cloudflare Logs) would mix with the result
          const calls = [], UP = {{status: 200, point: []}};
          globalThis.fetch = async (u) => {{ calls.push(u);
            if (u.includes("429") || UP.status !== 200) return new Response("busy", {{status: UP.status === 200 ? 429 : UP.status}});
            const body = u.includes("/v2/point/") ? {{ac: UP.point}} : {{ac: [], n: calls.length}};
            return new Response(JSON.stringify(body), {{headers: {{"Content-Type": "application/json"}}}}); }};
          const store = new Map();
          globalThis.caches = {{default: {{
            match: async k => store.has(k.url) ? store.get(k.url).clone() : undefined,
            put: async (k, r) => {{ store.set(k.url, r); }} }}}};
          const kv = new Map();
          const env = {{CALLSIGNS: {{get: async (k, o) => kv.has(k) ? JSON.parse(kv.get(k)) : null,
                                     put: async (k, v) => {{ kv.set(k, v); }} }}}};
          const ctx = {{waitUntil: p => p}};
          (async () => {{
            const w = (await import("data:text/javascript," + encodeURIComponent(src))).default;
            const req = async (path, origin, method) => {{
              const r = await w.fetch(new Request("https://relay.example" + path,
                {{method: method || "GET", headers: origin ? {{Origin: origin}} : {{}}}}), env, ctx);
              await new Promise(ok => setTimeout(ok, 0));   // let cache.put finish
              return [r.status, r.headers.get("Access-Control-Allow-Origin"), r.headers.get("X-Stale"), await r.text()];
            }};
            const out = await (async () => {{ {body} }})();
            process.stdout.write(JSON.stringify(out));
          }})();"""
        r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
        if r.returncode:
            raise AssertionError(r.stderr)
        return json.loads(r.stdout)

    def test_relays_feed_paths_with_cors_only(self):
        pages = "https://yuval-harpaz.github.io"
        got = self.run_worker("""
            const P = "https://yuval-harpaz.github.io", out = [];
            for (const [path, origin, method] of [
                ["/v2/hex/738071,8965d1", P], ["/v2/callsign/WZZ4603", "http://localhost:8765"],
                ["/data/traces/71/trace_recent_738071.json", P], ["/v2/hex/738071", P, "OPTIONS"],
                ["/v2/hex/738071", "https://evil.example"],   // another site: refused
                ["/v2/point/32/34/250", P],                   // not a path the pages use: not relayed
                ["/https://example.com/", P], ["/v2/hex/738071", P, "POST"],
                ["/v2/callsign/X429", P],                     // upstream 429 passed on (nothing cached)
                ["/", null]])                                 // someone opening the address: a help text
              out.push((await req(path, origin, method)).slice(0, 2));
            return {out, calls};""")
        self.assertEqual(got["out"], [[200, pages], [200, "http://localhost:8765"], [200, pages], [204, pages],
                                      [403, None], [404, pages], [404, pages], [405, pages], [429, pages], [200, None]])
        self.assertEqual(got["calls"], ["https://api.adsb.lol/v2/hex/738071,8965d1",
                                        "https://api.adsb.lol/v2/callsign/WZZ4603",
                                        "https://adsb.lol/data/traces/71/trace_recent_738071.json",
                                        "https://api.adsb.lol/v2/callsign/X429"])

    def test_cached_answer_and_a_recent_one_when_adsb_lol_says_429(self):
        got = self.run_worker("""
            const P = "https://yuval-harpaz.github.io", path = "/v2/callsign/CFG4308", out = [];
            out.push(await req(path, P));                       // fetched
            clock.now += 3000; out.push(await req(path, P));    // fresh: from the cache
            clock.now += 10000; UP.status = 429;
            out.push(await req(path, P));                       // 429 upstream: the 13 s old answer
            clock.now += 200000; out.push(await req(path, P));  // too old by now: the 429 itself
            return {out: out.map(r => [r[0], r[2], r[3]]), calls: calls.length};""")
        self.assertEqual([o[0] for o in got["out"]], [200, 200, 200, 429])
        self.assertEqual(got["out"][1][2], got["out"][0][2])     # same answer, not asked again
        self.assertEqual(got["out"][2][1], "13")                 # X-Stale: its age in seconds
        self.assertEqual(got["calls"], 3)

    def test_callsign_map_from_the_scheduled_run(self):
        got = self.run_worker("""
            UP.point = [{hex: "4b1805", flight: "CFG4308 ", seen: 2}, {hex: "738062", flight: "ELY5064"},
                        {hex: "abc123"}];                                      // no callsign: skipped
            await w.scheduled({}, env, ctx);
            const first = JSON.parse((await req("/hexof/CFG4308,ELY5064,NONE", "https://yuval-harpaz.github.io"))[3]);
            clock.now += 37 * 3600e3; UP.point = [{hex: "738062", flight: "ELY5064"}];
            await w.scheduled({}, env, ctx);                                   // CFG4308 too old now
            const later = JSON.parse((await req("/hexof/CFG4308,ELY5064", null))[3]);
            UP.status = 429; await w.scheduled({}, env, ctx);                  // a busy feed changes nothing
            const busy = JSON.parse((await req("/hexof/ELY5064", null))[3]);
            const status = JSON.parse((await req("/status", null))[3]);
            return {first, later, busy, status, point: calls.filter(u => u.includes("/v2/point/")).length};""")
        self.assertEqual(got["status"]["kv"], True)          # /status: what the owner checks in a browser
        self.assertEqual(got["status"]["callsigns"], 1)
        self.assertEqual(got["status"]["lastRun"]["upstreamHTTP"], 429)
        self.assertEqual(got["status"]["lastRun"]["aircraft"], 0)
        self.assertEqual(got["first"], {"CFG4308": {"hex": "4b1805", "seen": 1791399998},
                                        "ELY5064": {"hex": "738062", "seen": 1791400000}})
        self.assertEqual(list(got["later"]), ["ELY5064"])
        self.assertEqual(got["busy"]["ELY5064"]["hex"], "738062")
        self.assertEqual(got["point"], 3)


    def test_monitor_fills_the_map_through_learn(self):
        got = self.run_worker("""
            const post = async (body, token) => {
              const r = await w.fetch(new Request("https://relay.example/learn", {method: "POST", body,
                headers: token ? {Authorization: "Bearer " + token} : {}}), env, ctx);
              return [r.status, await r.text()];
            };
            const none = await post("{}", "s3cret");                  // no secret set in the worker yet
            env.LEARN_TOKEN = "s3cret";
            const wrong = await post("{}", "guess");
            const bad = await post("not json", "s3cret");
            const now = clock.now / 1000;
            const ok = await post(JSON.stringify({"ELY5064": ["738062", now - 30], "cfg4308 ": ["4B1805", now],
                                                  "BAD!": ["738062", now], "OLD1": ["abcdef", now - 40 * 3600],
                                                  "NOHEX": ["zz", now]}), "s3cret");
            await post(JSON.stringify({"ELY5064": ["111111", now - 600]}), "s3cret");   // older: kept as was
            const map = JSON.parse((await req("/hexof/ELY5064,CFG4308,OLD1", null))[3]);
            const status = JSON.parse((await req("/status", null))[3]);
            return {codes: [none[0], wrong[0], bad[0], ok[0]], stored: JSON.parse(ok[1]).stored, map, status};""")
        self.assertEqual(got["codes"], [503, 403, 400, 200])
        self.assertEqual(got["stored"], 2)
        self.assertEqual({k: v["hex"] for k, v in got["map"].items()}, {"ELY5064": "738062", "CFG4308": "4b1805"})
        self.assertEqual(got["status"]["callsigns"], 2)
        self.assertTrue(got["status"]["learnToken"])
        self.assertEqual(got["status"]["lastPush"]["callsigns"], 1)   # received in the last push (an older hearing: map kept)


class ShareCallsignsScript(unittest.TestCase):
    """tools/share_callsigns.py (run by the GitHub workflow): adsb.lol near TLV -> the relay's /learn."""
    def run_main(self, answers, token="s3cret"):
        import io
        import urllib.error
        from unittest import mock
        import share_callsigns as sc
        sent, sleeps = [], []

        def opener(req, timeout=None):
            sent.append(req)
            code, body = answers.pop(0)
            if code != 200:
                raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b"busy"))
            return io.BytesIO(json.dumps(body).encode())

        with mock.patch.dict(os.environ, {"RELAY_TOKEN": token}):
            return sc.main(opener, sleeps.append), sent, sleeps

    FEED = {"now": 1791400000000, "ac": [{"hex": "738062", "flight": "ELY5064 ", "seen": 3.4},
                                         {"hex": "4b1805", "flight": "CFG4308"}, {"hex": "abc123"}]}

    def test_sends_what_is_near_tlv(self):
        code, sent, _ = self.run_main([(200, self.FEED), (200, {"stored": 2})])
        self.assertEqual(code, 0)
        self.assertIn("/v2/point/32.0114/34.8867/250", sent[0].full_url)
        post = sent[1]
        self.assertEqual(post.get_method(), "POST")
        self.assertEqual(post.get_header("Authorization"), "Bearer s3cret")
        self.assertEqual(json.loads(post.data), {"ELY5064": ["738062", 1791399997], "CFG4308": ["4b1805", 1791400000]})

    def test_busy_feed_is_a_warning_and_a_refusing_relay_an_error(self):
        code, sent, sleeps = self.run_main([(429, None), (429, None)])
        self.assertEqual((code, len(sent), sleeps), (0, 2, [30]))     # one retry, nothing sent
        code, _, _ = self.run_main([(429, None), (200, self.FEED), (200, {"stored": 2})])
        self.assertEqual(code, 0)
        code, _, _ = self.run_main([(200, self.FEED), (403, None)])
        self.assertEqual(code, 1)                                     # wrong token: a red run
        self.assertEqual(self.run_main([], token="")[0], 1)            # no secret set


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

    def test_pages_ask_for_the_current_flightboard_js(self):
        # GitHub Pages lets browsers cache files for 10 min: a new page with the old flightboard.js
        # broke ("FB.blocked is not a function"). The ?v= tag must follow the file's content.
        import hashlib
        with open(JS, "rb") as f:
            v = hashlib.sha256(f.read()).hexdigest()[:8]
        for page in ("flights.html", "flight_map.html"):
            with open(os.path.join(ROOT, "docs", page), encoding="utf-8") as f:
                self.assertIn(f'src="flightboard.js?v={v}"', f.read(),
                              f"{page}: set flightboard.js?v={v} (sha256 of docs/flightboard.js)")

    def test_serves_the_pages(self):
        for path in ("/", "/flights.html", "/flight_map.html", "/flightboard.js"):
            code, _, body = self.get(path)
            self.assertEqual(code, 200, path)
        self.assertIn(b"flightboard.js", self.get("/")[2])


if __name__ == "__main__":
    unittest.main()
