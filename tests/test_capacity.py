"""The component order's weight as a share of what you can carry: pct% of 150 x Strength, less the most your equipped
gear has weighed in the last hour."""

import unittest

from companion.tracker import Tracker


class CapacityTest(unittest.TestCase):
    def setUp(self):
        tr = Tracker({})
        tr.player, tr.in_world = 1, True
        tr.attributes = {1: 100}                                                    # 15,000 BU
        tr.objects = {1: {"name": "Me"}, 2: {"name": "Plate", "wielder": 1, "stack": 1}, 3: {"name": "Ring", "wielder": 1}}
        tr.appraised_burden = {2: 3000}                                             # the ring's never been appraised
        self.tr = tr

    def test_the_heaviest_kit_in_the_last_hour(self):
        prefs = {"restock": {"target_mode": "capacity", "target_pct": 40}}
        self.tr.tick(0.0)
        self.assertEqual(self.tr.capacity_target(prefs)["target_bu"], 3000)         # 40% of 15,000 (6,000) - 3,000
        self.tr.objects[2]["wielder"] = None                                        # took the plate off
        self.tr.tick(1800.0)
        self.assertEqual(self.tr.capacity_target(prefs)["worn"], 3000)              # still the most this hour
        self.tr.tick(3700.0)
        self.assertEqual(self.tr.capacity_target(prefs)["target_bu"], 6000)         # an hour on: 40% of 15,000

    def test_unappraised_gear_is_counted_as_unknown(self):
        cap = self.tr.capacity_target({"restock": {"target_mode": "capacity", "target_pct": 40}})
        self.assertEqual((cap["worn"], cap["unknown"]), (3000, 1))
        self.tr.book_burden = lambda g: 50 if g == 3 else None                      # the item book knows the ring
        self.assertEqual(self.tr.worn_burden(), (3050, 0))

    def test_off_unless_asked(self):
        self.assertIsNone(self.tr.capacity_target({"restock": {"target_bu": 5000}}))


if __name__ == "__main__":
    unittest.main()
