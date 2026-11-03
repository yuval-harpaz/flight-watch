"""tools/find_go_arounds.py's detector on simple shapes (rows: t, lat, lon, alt or None on the ground, gs, flight)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import find_go_arounds as g  # noqa: E402

CELLS = {(32, 34): [("LLBG", 32.0114, 34.8867, 135)], (31, 35): [("HIGH", 31.9, 35.2, 6447)]}


def seq(alts, lat=32.0, lon=34.85, gs=140):
    return [(i * 5.0, lat, lon, a, gs, "X") for i, a in enumerate(alts)]


class DetectorTests(unittest.TestCase):
    def kinds(self, rows):
        return [h["kind"] for h in g.detect(rows, CELLS)]

    def test_a_landing_and_a_take_off_are_not(self):
        self.assertEqual(self.kinds(seq([3000 - 40 * i for i in range(70)] + [None] * 3)), [])
        self.assertEqual(self.kinds(seq([None] * 4 + [200 + 150 * i for i in range(30)])), [])

    def test_a_go_around_in_the_air(self):
        self.assertEqual(self.kinds(seq([2500 - 40 * i for i in range(58)] + [180 + 100 * i for i in range(30)])), ["air"])

    def test_a_touch_and_go(self):
        self.assertEqual(self.kinds(seq([2000 - 40 * i for i in range(45)] + [None] * 3 + [300 + 120 * i for i in range(20)])),
                         ["touch"])

    def test_a_touch_whose_first_report_after_it_is_low(self):
        """The climb counts from the runway, not from the last report before touching (it found no touch at all before)."""
        self.assertEqual(self.kinds(seq([2000 - 40 * i for i in range(47)] + [None] * 3 + [150 + 120 * i for i in range(20)])),
                         ["touch"])

    def test_a_level_off_far_below_a_listed_airfield_is_not(self):
        """CKS704 levelled at 4,000 ft and climbed on over Jerusalem: 2,447 ft "below" the height listed for LLJR."""
        alts = [5000 - 40 * i for i in range(25)] + [4000 + 100 * i for i in range(15)]
        self.assertEqual(self.kinds(seq(alts, lat=31.9, lon=35.2)), [])

    def test_one_bad_altitude_is_no_climb(self):
        alts = [2500 - 40 * i for i in range(50)] + [1900] + [500 - 40 * i for i in range(10)]
        self.assertEqual(self.kinds(seq(alts)), [])


if __name__ == "__main__":
    unittest.main()
