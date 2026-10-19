"""Dungeon spawn points and their respawn timers (dungeons.py), and the tracker telling a kill from what it sees."""

import unittest
from unittest import mock

from companion import dungeons
from companion.tracker import Tracker

LB = 0x019A
DATA = {"name": "Crater Caves", "landblock": LB, "cells": [], "exits": [],
        "spawns": [{"wcid": 199, "name": "Magma Golem", "level": 103, "cell": 0x10C, "x": 150, "y": -80, "z": -78, "delay": 420},
                   {"wcid": 199, "name": "Magma Golem", "level": 103, "cell": 0x110, "x": 160, "y": -50, "z": -78, "delay": 420},
                   {"wcid": 195, "name": "Granite Golem", "level": 33, "cell": 0x120, "x": 40, "y": -130, "z": -24, "delay": 420}]}
CELL = (LB << 16) | 0x10C


class SpawnClockTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(dungeons, "load", lambda lb: DATA if lb == LB else None)
        p.start()
        self.addCleanup(p.stop)

    def states(self, c, now):
        return [(p["state"], p.get("back")) for p in c.snapshot(LB, now)["points"]]

    def test_seen_killed_due_and_back(self):
        c = dungeons.SpawnClock()
        c.presence(CELL, 1000)
        c.seen(0xA, 199, CELL, 151, -79, -78, 1000)                    # near the first Magma Golem point
        c.seen(0xB, 195, CELL, 41, -131, -24, 1000)                    # the Granite Golem's
        c.seen(0xC, 199, CELL, 300, 300, -78, 1000)                    # nowhere near a point: nobody's
        self.assertEqual(self.states(c, 1000), [("alive", None), ("unknown", None), ("alive", None)])
        c.died(0xA, 1100)
        self.assertEqual(self.states(c, 1200)[0], ("dead", 1520))       # back 7 minutes after the kill
        self.assertEqual(self.states(c, 1600)[0], ("due", 1520))        # time's up, not seen yet
        c.seen(0xD, 199, CELL, 149, -80, -78, 1600)                     # there it is
        self.assertEqual(self.states(c, 1600)[0], ("alive", None))

    def test_one_back_too_soon_is_anothers(self):
        c = dungeons.SpawnClock()
        c.presence(CELL, 1000)
        c.seen(0xA, 199, CELL, 150, -80, -78, 1000)
        c.died(0xA, 1100)
        c.seen(0xE, 199, CELL, 152, -78, -78, 1110)                     # a golem wandering by seconds later
        self.assertEqual(self.states(c, 1110)[0], ("dead", 1520))

    def test_away_long_enough_it_has_reset(self):
        c = dungeons.SpawnClock()
        c.presence(CELL, 1000)
        c.seen(0xA, 199, CELL, 150, -80, -78, 1000)
        c.died(0xA, 1100)
        c.presence(CELL, 1100 + dungeons.RESET + 60)                    # back after it unloaded
        self.assertEqual(self.states(c, 1100 + dungeons.RESET + 60)[0], ("unknown", None))


class KillTest(unittest.TestCase):
    """A monster's death: no death message -- it's deleted and its corpse appears, in either order."""

    def setUp(self):
        p = mock.patch.object(dungeons, "load", lambda lb: DATA if lb == LB else None)
        p.start()
        self.addCleanup(p.stop)
        self.t = Tracker({}, clock=lambda: 1000.0)
        self.t.reset("x")
        pos = {"cell": CELL, "x": 150, "y": -80, "z": -78, "qw": 1, "qz": 0}
        self.t.handle({"type": "position", "object": None, "source": "client", **pos}, 1000)
        self.t.handle({"type": "object", "object": 0xA, "name": "Magma Golem", "wcid": 199, "item_type": 0x10, "position": pos}, 1000)

    def point(self, now):
        return self.t.dungeon_snapshot(now)["points"][0]["state"]

    def test_deleted_then_corpse(self):
        self.t.handle({"type": "deleted", "object": 0xA}, 1100)
        self.assertEqual(self.point(1100), "alive")                     # (could be anything leaving: not yet)
        self.t.handle({"type": "object", "object": 0xF, "name": "Corpse of Magma Golem", "wcid": 21, "item_type": 0x200,
                       "position": {"cell": CELL, "x": 150, "y": -80, "z": -78, "qw": 1, "qz": 0}}, 1100.03)
        self.assertEqual(self.point(1101), "dead")

    def test_corpse_then_deleted(self):
        self.t.handle({"type": "object", "object": 0xF, "name": "Corpse of Magma Golem", "wcid": 21, "item_type": 0x200,
                       "position": {"cell": CELL, "x": 150, "y": -80, "z": -78, "qw": 1, "qz": 0}}, 1100)
        self.t.handle({"type": "deleted", "object": 0xA}, 1100.03)
        self.assertEqual(self.point(1101), "dead")

    def test_a_deletion_with_no_corpse_isnt_a_death(self):
        self.t.handle({"type": "deleted", "object": 0xA}, 1100)
        self.t.handle({"type": "object", "object": 0xF, "name": "Corpse of Magma Golem", "wcid": 21, "item_type": 0x200,
                       "position": {"cell": CELL, "x": 150, "y": -80, "z": -78, "qw": 1, "qz": 0}}, 1110)   # much later
        self.assertNotEqual(self.point(1111), "dead")


if __name__ == "__main__":
    unittest.main()
