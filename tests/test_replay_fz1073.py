"""Regression replay: FZ1073 (A6-FKF, Dubai -> TLV), 30 Sep 2026, 05:00-06:15 UTC.

Over Jordan the flight descended suddenly from FL340 (05:21), went silent for 8.5 min, squawked
7700 (05:31) then 7500 (05:35), made a U-turn 130 nm from TLV (05:42-05:44) and was lost heading
south-east (05:53). 161 other aircraft in the 150 nm circle or flying to/from TLV are replayed
with it; all of them landed where they were going.

The must-detect list should stay green whatever the thresholds; the noise budget is the number
of alerts on the other aircraft today - lower it as tuning improves, never raise it casually.
See the full timeline with:  python tests/replay.py
"""
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import replay  # noqa: E402

FZ1073 = "8965d1"
NOISE_BUDGET = 60  # alerts on the other 161 aircraft (57 at capture time with default thresholds)


def utc(hhmmss: str) -> str:
    return f"2026-09-30T{hhmmss}Z"


class FZ1073Replay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = replay.load()
        cls.alerts, cls.monitor, cls.feed = replay.run(cls.fixture)
        cls.focus = [a for a in cls.alerts if a["hex"] == FZ1073]
        cls.others = [a for a in cls.alerts if a["hex"] != FZ1073]

    def first(self, kind, start, end, text=""):
        hits = [a for a in self.focus if a["kind"] == kind and utc(start) <= a["time"] <= utc(end)
                and text in a["message"]]
        self.assertTrue(hits, f"no {kind} {text!r} for FZ1073 between {start} and {end}\n"
                              + replay.timeline(self.focus))
        return hits[0]

    def test_followed_as_tlv_arrival_before_the_incident(self):
        t = self.monitor.tracks[FZ1073]
        self.assertTrue(t.followed)
        self.assertEqual(t.sched.flight, "FZ1073")
        self.assertEqual(t.sched.direction, "ARR")

    def test_sudden_descent(self):
        a = self.first("VERTICAL_RATE", "05:21:00", "05:23:00", "DESCENT")
        self.assertTrue(a["remote"], "seen while still outside the local circle")

    def test_emergency_7700_before_entering_the_circle(self):
        a = self.first("EMERGENCY", "05:31:00", "05:33:00", "7700")
        self.assertTrue(a["remote"])

    def test_hijack_squawk_7500(self):
        self.first("EMERGENCY", "05:35:00", "05:37:00", "7500")

    def test_u_turn(self):
        self.first("COURSE_CHANGE", "05:41:30", "05:46:00")

    def test_lost_heading_away(self):
        a = self.first("LOST_CONTACT", "05:53:00", "06:15:00")
        self.assertEqual(a["traffic"], "ARR")

    def test_no_false_diversions_or_emergencies_on_other_flights(self):
        bad = [a for a in self.others if a["kind"] in ("DIVERSION", "RETURNED", "EMERGENCY")]
        self.assertFalse(bad, "\n" + replay.timeline(bad))

    def test_noise_budget(self):
        self.assertLessEqual(len(self.others), NOISE_BUDGET, "\n" + replay.timeline(self.others))

    def test_fixture_is_the_incident(self):
        meta = self.fixture["meta"]
        self.assertEqual(datetime.fromtimestamp(meta["start"], timezone.utc).isoformat(),
                         "2026-09-30T05:00:00+00:00")
        self.assertIn(FZ1073, self.fixture["aircraft"])


if __name__ == "__main__":
    unittest.main()
