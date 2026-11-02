"""Regression replays of real incidents away from TLV (fixtures in tests/data/, see tests/replay.py).

Each is watched from a point near it (`airport_coords` in the fixture), so every check runs on it as
on local traffic. Israel-only checks (TOWARD_ISRAEL) are off there. As in test_replay_fz1073.py:
the must-detect alerts stay green whatever the thresholds; a noise budget counts the alerts on the
other aircraft today - lower it as tuning improves, never raise it casually.

- FR4978 (RYR1TZ, SP-RSM, Athens -> Vilnius), 23 May 2021, ADS-B Exchange history, 56 aircraft:
  squawk 7700 at 09:47 over Belarus 35 nm from Vilnius, turned toward Minsk, silent 9 min in the
  descent, landed at Minsk. Today's route data has RYR1TZ as Brussels-Rome; the fixture keeps the
  route of the day (`route_overrides`).
- AS1282 (ASA1282, N704AL, Portland -> Ontario), 6 Jan 2024, ADS-B Exchange history, 76 aircraft:
  door plug blown out at 16,000 ft at 01:14 UTC, back on the ground at Portland at 01:27. No
  emergency squawk in ADS-B and never more than 29 nm from Portland: EARLY_DESCENT (turned down 7 min
  after take-off) at 01:14, RETURNED at the landing.
- MU5735 (CES5735, B-1791, Kunming -> Guangzhou), 21 Mar 2022, Flightradar24's granular data
  (ADS-B Exchange had no coverage there), imported with tools/import_track.py: dive from FL291 at
  06:20 UTC, last report 3,225 ft falling 31,000 ft/min.
- ET302 (ETH302, ET-AVJ, Addis Ababa -> Nairobi), 10 Mar 2019, Flightradar24's granular data: the
  data ends at 05:41:02 UTC, 8,600 ft, ~3 min before the crash: ALTITUDE_WOBBLE at 05:40, LOST_CONTACT.
- JT610 (LNI610, PK-LQP, Jakarta -> Pangkal Pinang), 28 Oct 2018 UTC, Flightradar24's granular data: the
  altitude went up and down for 8 min after take-off (MCAS); ALTITUDE_WOBBLE from 23:25, the dive at 23:31.
- UA2005 (UAL2005, N69818, Chicago -> Minneapolis), 30 May 2026 UTC, ADS-B Exchange history, 122
  aircraft around Madison: a passenger tried to breach the cockpit, squawk 7500 at 01:52:25, U-turn,
  landed at Madison 02:11.
- Horizon 2059 (QXE2059, N660QX, Everett -> San Francisco), 23 Oct 2023, ADS-B Exchange history, 246
  aircraft around Portland: an off-duty pilot pulled the engine fire handles; no squawk; turned back
  at FL310 at 01:05, landed at Portland 01:26. Most noise is today's route data on 2023 callsigns
  (ASA1383 taken as a Portland arrival landed at Seattle: OFF_COURSE + DIVERSION).
- Frontier 3345 (FFT3345, N660FR, San Juan -> Chicago), 1 Jun 2026 UTC, ADS-B Exchange history, 181
  aircraft around Miami: a passenger tried the exit door and the cockpit, diverted to Miami. Nothing
  in ADS-B tells (no squawk, a 40 min gap over the Bahamas, Miami lies within ~30 deg of Chicago's
  direction): no alert, recorded as a limit.
- N722BD (Learjet 60, Austin -> North Eleuthera), 1 Nov 2026, ADS-B Exchange's trace_full (live, ~24 h back),
  the aircraft alone: dive from 35,450 ft at 17:48:17 UTC, last report 17:49:32. The barometric altitude stayed
  8,750 ft from 17:49:23 while the GPS altitude fell to 325 ft: the last 12 rows carry the GPS altitude (the
  fixture's `altitude_note`). The reported rate is capped at -32,640 ft/min, ADS-B's limit.
- AA77 and UA93, 11 Sep 2001, from 911maps.kmz (11-septembre.com) with tools/import_911.py: the flight
  recorder path (1 point a second, with altitude), timed by FAA / NEADS radar returns, as if the
  transponder had stayed on (it was switched off: AA77 08:55, UA93 09:40 EDT). Watched from Dulles and
  Pittsburgh. AA77's final 330 deg descending turn into the Pentagon gives nothing (circling, 2,000-
  4,700 ft/min), and a cut at the switch-off gives nothing either: silent 242 nm from Dulles, at the
  circle's edge.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import replay  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "data")


class Incident:
    """Mixin: replay FIXTURE once; `first` finds the focus aircraft's alert of a kind in a window."""
    FIXTURE = DATE = ""
    NOISE_BUDGET = 0
    HIGH_NOISE_BUDGET = 0  # high-priority alerts allowed on other aircraft (stale routes of an old day)

    @classmethod
    def setUpClass(cls):
        cls.fixture = replay.load(os.path.join(DATA, cls.FIXTURE))
        cls.alerts, cls.monitor, cls.feed = replay.run(cls.fixture)
        focus = set(cls.fixture["meta"]["focus"])
        cls.focus = [a for a in cls.alerts if a["hex"] in focus]
        cls.others = [a for a in cls.alerts if a["hex"] not in focus]

    def first(self, kind, start, end, text=""):
        lo, hi = f"{self.DATE}T{start}Z", f"{self.DATE}T{end}Z"
        hits = [a for a in self.focus if a["kind"] == kind and lo <= a["time"] <= hi and text in a["message"]]
        self.assertTrue(hits, f"no {kind} {text!r} between {start} and {end}\n" + replay.timeline(self.focus))
        return hits[0]

    def test_noise_budget(self):
        self.assertLessEqual(len(self.others), self.NOISE_BUDGET, "\n" + replay.timeline(self.others))

    def test_no_high_priority_false_alarms_on_other_flights(self):
        bad = [a for a in self.others if a["kind"] in ("DIVERSION", "RETURNED", "EMERGENCY", "OFF_COURSE",
                                                       "TURNING_BACK", "TOWARD_ISRAEL")]
        self.assertLessEqual(len(bad), self.HIGH_NOISE_BUDGET, "\n" + replay.timeline(bad))


class FR4978Replay(Incident, unittest.TestCase):
    FIXTURE, DATE = "fr4978_2021-05-23.json.gz", "2021-05-23"
    NOISE_BUDGET = 4  # Belavia course changes around Minsk (3) and a loss at the circle's edge (1)

    def test_vilnius_arrival_by_the_route_of_the_day(self):
        t = self.monitor.tracks["48c22c"]
        self.assertEqual(self.monitor.classify(t), ("ARR", "LGAV-EYVI"))

    def test_emergency_7700(self):
        self.first("EMERGENCY", "09:47:00", "09:48:30", "7700")

    def test_turn_toward_minsk(self):
        self.first("COURSE_CHANGE", "09:47:00", "09:50:00")

    def test_off_course(self):
        a = self.first("OFF_COURSE", "09:50:00", "09:55:00")
        self.assertEqual(a["priority"], 5)

    def test_silent_in_the_descent_then_heard_again(self):
        self.first("LOST_CONTACT", "09:53:00", "09:56:00")
        self.first("CONTACT_RESTORED", "10:01:00", "10:04:00")

    def test_diversion_near_minsk(self):
        a = self.first("DIVERSION", "10:10:00", "10:20:00")
        self.assertGreater(a["dist_nm"], 100)


class AS1282Replay(Incident, unittest.TestCase):
    FIXTURE, DATE = "as1282_2024-01-06.json.gz", "2024-01-06"
    NOISE_BUDGET = 1  # a regional jet climbing out at 12 deg

    def test_returned_although_never_far(self):
        a = self.first("RETURNED", "01:26:00", "01:30:00")
        self.assertIn("16275 ft", a["message"])

    def test_descending_soon_after_take_off(self):
        """No squawk, a moderate descent (~2,000 ft/min) and a return inside --off-course-min-dist: only
        turning down 7 min after take-off tells, 13 min before the landing back."""
        a = self.first("EARLY_DESCENT", "01:14:00", "01:15:00")
        early = [x for x in self.focus if x["time"] < a["time"]]
        self.assertFalse(early, "\n" + replay.timeline(early))


class MU5735Replay(Incident, unittest.TestCase):
    FIXTURE, DATE = "mu5735_2022-03-21.json.gz", "2022-03-21"

    def test_dive(self):
        self.first("VERTICAL_RATE", "06:20:30", "06:21:30", "DESCENT")

    def test_lost_falling_below_lost_min_alt(self):
        a = self.first("LOST_CONTACT", "06:22:00", "06:24:00", "falling")
        self.assertLess(a["alt"], 5000)



class AA77Replay(Incident, unittest.TestCase):
    FIXTURE, DATE = "aa77_2001-09-11.json.gz", "2001-09-11"

    def test_turning_back_to_dulles(self):
        self.first("COURSE_CHANGE", "12:58:00", "13:02:00")
        self.first("TURNING_BACK", "12:58:00", "13:02:00")


class UA93Replay(Incident, unittest.TestCase):
    FIXTURE, DATE = "ua93_2001-09-11.json.gz", "2001-09-11"

    def test_u_turn_near_cleveland(self):
        self.first("COURSE_CHANGE", "13:38:00", "13:42:00")

    def test_final_dive(self):
        self.first("VERTICAL_RATE", "14:02:30", "14:03:11", "DESCENT")
        self.first("LOST_CONTACT", "14:03:00", "14:05:00", "falling")



class ET302Replay(Incident, unittest.TestCase):
    FIXTURE, DATE = "et302_2019-03-10.json.gz", "2019-03-10"

    def test_lost_after_take_off(self):
        self.first("LOST_CONTACT", "05:41:00", "05:45:00")

    def test_altitude_wobble_before_the_data_ends(self):
        self.first("ALTITUDE_WOBBLE", "05:39:30", "05:41:02")


class JT610Replay(Incident, unittest.TestCase):
    FIXTURE, DATE = "jt610_2018-10-29.json.gz", "2018-10-28"  # UTC (06:20 local, 29 Oct)

    def test_altitude_wobble_minutes_before_the_crash(self):
        self.first("ALTITUDE_WOBBLE", "23:24:00", "23:26:00")

    def test_final_dive(self):
        self.first("VERTICAL_RATE", "23:31:00", "23:32:00", "DESCENT")


class UA2005Replay(Incident, unittest.TestCase):
    FIXTURE, DATE = "ua2005_2026-05-30.json.gz", "2026-05-30"
    NOISE_BUDGET = HIGH_NOISE_BUDGET = 1  # one route-based OFF_COURSE

    def test_hijack_squawk_7500(self):
        self.first("EMERGENCY", "01:52:00", "01:53:00", "7500")

    def test_turn_and_off_course(self):
        self.first("COURSE_CHANGE", "01:53:00", "01:57:00")
        self.first("OFF_COURSE", "01:56:00", "02:00:00", "KMSP")


class QX2059Replay(Incident, unittest.TestCase):
    FIXTURE, DATE = "qx2059_2023-10-23.json.gz", "2023-10-23"
    NOISE_BUDGET = 15
    HIGH_NOISE_BUDGET = 7  # today's routes on 2023 callsigns (ASA1383 "PDX arrival" landed at Seattle)

    def test_turned_back_toward_portland(self):
        self.first("COURSE_CHANGE", "01:06:00", "01:12:00")
        self.first("OFF_COURSE", "01:07:00", "01:13:00", "KSFO")


class N722BDReplay(Incident, unittest.TestCase):
    FIXTURE, DATE = "n722bd_2026-11-01.json.gz", "2026-11-01"

    def test_dive(self):
        self.first("VERTICAL_RATE", "17:48:30", "17:48:50", "DESCENT")

    def test_lost_contact_falling(self):
        self.first("LOST_CONTACT", "17:49:00", "17:51:00", "falling")


class F93345Replay(Incident, unittest.TestCase):
    FIXTURE, DATE = "f93345_2026-06-01.json.gz", "2026-06-01"
    NOISE_BUDGET = HIGH_NOISE_BUDGET = 5  # route-based OFF_COURSE on Miami traffic

    def test_diversion_not_visible(self):
        """No squawk and Miami within ~30 deg of Chicago: the monitor cannot tell (a limit)."""
        self.assertFalse(self.focus, "\n" + replay.timeline(self.focus))


if __name__ == "__main__":
    unittest.main()
