"""tools/health_check.py: the owner's private chat is told only when something is badly wrong (25 Nov 2026)."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import health_check as hc  # noqa: E402

NOW = datetime(2026, 11, 25, 12, 0, 0)
START = "INFO    watching TLV (LLBG) r=150nm every 10s via adsb.lol, code 95a3062 (flight_watch.py 1234abcd)"


def log(*items):
    """[(minutes before NOW, text)] -> [(datetime, line)] as read_log gives them."""
    out = []
    for ago, text in sorted(items, key=lambda x: -x[0]):
        t = NOW - timedelta(minutes=ago)
        out.append((t, f"{t:%Y-%m-%d %H:%M:%S} {text}"))
    return out


def healthy(minutes=180, local=30):
    items = [(minutes, START)]
    items += [(m, f"INFO    {local} local | 40 followed globally (0 hot, 0 only for lost contact) | 300 scheduled flights | "
                  "budget 3.0 req/min") for m in range(minutes - 1, -1, -1)]
    items += [(m, "INFO    flight board: 300 active flights (100 with codeshares, 400 callsign variants)")
              for m in range(minutes - 2, -1, -10)]
    return items


class Problems(unittest.TestCase):
    def check(self, items, alive=True, free=50.0):
        return hc.problems(NOW, log(*items), alive, free)

    def test_all_well(self):
        self.assertEqual(self.check(healthy()), {})

    def test_usual_noise_is_not_a_problem(self):
        """429s, a failed board fetch, a relay reset, a short spell on adsb.fi: every day (20-25 Nov 2026)."""
        items = healthy() + [(m, "WARNING HTTP 429 from api.adsb.lol - cooling down 40s, budget now 3.0 req/min")
                             for m in range(0, 120, 2)]
        items += [(50, "WARNING flight board fetch failed (Read timed out.) - keeping 300 known flights"),
                  (40, "WARNING could not share callsigns with the relay: Connection reset by peer"),
                  (30, "INFO    local poll from adsb.fi (adsb.lol: 429 Client Error)"),
                  (25, "INFO    local poll from adsb.lol again")]
        self.assertEqual(self.check(items), {})

    def test_monitor_stopped(self):
        self.assertIn("down", self.check(healthy(), alive=False))
        stale = [(m + 30, text) for m, text in healthy()]
        self.assertEqual(list(self.check(stale)), ["down"])  # (the log silent: the rest is not repeated)

    def test_no_poll_answered(self):
        items = [x for x in healthy() if not (" local | " in x[1] and x[0] < 25)]
        items += [(m, "INFO    local poll: adsb.fi failed too (Read timed out)") for m in range(24, -1, -1)]
        self.assertIn("no_polls", self.check(items))

    def test_empty_answers(self):
        self.assertIn("empty", self.check(healthy(local=0)))

    def test_board_not_read(self):
        items = [x for x in healthy() if not ("flight board:" in x[1] and x[0] < 100)]
        self.assertIn("board", self.check(items))

    def test_adsblol_gone_from_the_local_poll(self):
        items = healthy(minutes=500) + [(420, "INFO    local poll from adsb.fi (adsb.lol: 403 Forbidden)")]
        self.assertIn("adsblol_local", self.check(items))

    def test_refused(self):
        items = healthy() + [(m, "ERROR   adsb.fi refused access (HTTP 403) - no lookups on it for 30 min") for m in (90, 30)]
        self.assertIn("refused_adsb.fi", self.check(items))
        self.assertNotIn("refused_adsb.fi", self.check(healthy() + [items[-1]]))

    def test_squawk_reports_unread(self):
        items = healthy() + [(m, "WARNING Squawk Alert (Mastodon) not read: 503") for m in range(0, 110, 2)]
        self.assertIn("squawkalert", self.check(items))

    def test_crashing(self):
        items = healthy() + [(m, START) for m in (50, 35, 20, 5)]
        self.assertIn("restarts", self.check(items))


class Telling(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.sent = []
        self.found = {}
        self.saved = (hc.STATE, hc.tell_owner, hc.problems, hc.read_log, hc.running, hc.datetime)
        hc.STATE = os.path.join(self.dir.name, "state.json")
        hc.tell_owner = lambda text: self.sent.append(text) or True
        hc.problems = lambda now, lines, alive, free: dict(self.found)
        hc.read_log = lambda now, hours=8: []
        hc.running = lambda: True
        self.now = NOW

        test = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return test.now

        hc.datetime = Clock

    def tearDown(self):
        hc.STATE, hc.tell_owner, hc.problems, hc.read_log, hc.running, hc.datetime = self.saved
        self.dir.cleanup()

    def test_told_once_reminded_and_over(self):
        self.found = {"down": "The monitor is not running."}
        hc.main([])
        hc.main([])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("The monitor is not running.", self.sent[0])
        self.now = NOW + timedelta(hours=13)
        hc.main([])
        self.assertEqual(len(self.sent), 2)
        self.assertIn("still:", self.sent[1])
        self.found = {}
        hc.main([])
        self.assertIn("over", self.sent[2])
        hc.main([])
        self.assertEqual(len(self.sent), 3)
        with open(hc.STATE) as f:
            self.assertEqual(json.load(f), {})

    def test_nothing_wrong_sends_nothing(self):
        hc.main([])
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
