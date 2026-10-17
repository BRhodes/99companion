"""A character's run speed, the server's way (MovementSystem::GetRunRate): Run, then burden against 150 x Strength."""

import unittest

from companion.tracker import Tracker


def tracker(quickness, strength, burden):
    tr = Tracker({}, skill_formulas={24: (0, 1, 0, 1, 3, 0)})            # Run = Quickness
    tr.player = 1
    tr.attributes = {1: strength, 3: quickness}
    tr.skills = {24: {"sac": 2, "level": 0}}
    tr.objects = {1: {"name": "Me"}}
    tr.burden = burden                                                     # the server's total (ENCUMB_VAL)
    return tr


class RunSpeedTest(unittest.TestCase):
    def test_skill_and_burden(self):
        self.assertAlmostEqual(tracker(200, 100, 0).run_speed(), 4 + 11 * 0.5)          # Run 200: 9.5 m/s
        self.assertAlmostEqual(tracker(16, 50, 0).run_speed(), 4 + 11 * 16 / 216)       # 4.8: hardly a run
        self.assertAlmostEqual(tracker(200, 100, 22500).run_speed(), 4 + 11 * 0.5 * 0.5)   # 150% loaded: half the run
        self.assertAlmostEqual(tracker(200, 100, 40000).run_speed(), 4.0)               # 2x and more: no run at all


if __name__ == "__main__":
    unittest.main()
