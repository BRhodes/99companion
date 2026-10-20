"""The Map page's creatures: the ones in view outdoors, where they stand, and how far off they come for you."""

import unittest

from companion.tracker import AGGRO_DEFAULT, MONSTERS, Tracker

ME = 0x500000C9
OUTSIDE = 0xA9B4001C          # a landblock's outdoor cell (low word under 0x100)
INSIDE = 0x019A0150           # a dungeon cell


def creature(guid, wcid, cell, x=100.0, y=50.0):
    return {"type": "object", "object": guid, "name": MONSTERS[str(wcid)]["name"], "wcid": wcid, "item_type": 0x10,
            "position": {"cell": cell, "x": x, "y": y, "z": 0.0, "qw": 1, "qz": 0}}


class CreaturesTest(unittest.TestCase):
    def setUp(self):
        self.t = Tracker({}, clock=lambda: 100.0)
        self.t.reset("x")
        self.t.player = ME

    def guids(self):
        return {c["guid"]: c for c in self.t.snapshot()["creatures"]}

    def test_outdoors_with_their_range(self):
        self.t.handle(creature(0xA1, 199, OUTSIDE), 100)            # a Magma Golem: sees you from 17 m
        self.t.handle(creature(0xA2, 199, INSIDE), 100)             # one in a dungeon: not on the overworld map
        got = self.guids()
        self.assertEqual(list(got), [0xA1])
        g = got[0xA1]
        self.assertEqual(g["aggro"], 17.0)
        self.assertAlmostEqual(g["ns"], ((OUTSIDE >> 16 & 0xFF) * 192 + 50) / 240 - 101.95, places=3)
        self.assertAlmostEqual(g["ew"], ((OUTSIDE >> 24) * 192 + 100) / 240 - 101.95, places=3)

    def test_ones_that_dont_come_for_you_have_no_range(self):
        self.t.handle(creature(0xB1, 2566, OUTSIDE), 100)           # a Black Rabbit: only once attacked
        self.assertIsNone(self.guids()[0xB1]["aggro"])

    def test_moves_and_goes(self):
        self.t.handle(creature(0xA1, 199, OUTSIDE), 100)
        self.t.handle({"type": "position", "object": 0xA1, "cell": OUTSIDE, "x": 120.0, "y": 50.0, "z": 0.0,
                       "qw": 1, "qz": 0, "source": "server"}, 101)
        self.assertAlmostEqual(self.guids()[0xA1]["ew"], ((OUTSIDE >> 24) * 192 + 120) / 240 - 101.95, places=3)
        self.t.handle({"type": "deleted", "object": 0xA1}, 102)
        self.assertEqual(self.guids(), {})

    def test_default_range(self):
        wcid = next(int(k) for k, v in MONSTERS.items() if "aggro" not in v and not v["tolerance"])
        self.t.handle(creature(0xC1, wcid, OUTSIDE), 100)
        self.assertEqual(self.guids()[0xC1]["aggro"], AGGRO_DEFAULT)


if __name__ == "__main__":
    unittest.main()
