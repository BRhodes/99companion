"""Learning a spell cast on others on the monster or player you last appraised, only when the server would take it."""

import unittest
from unittest import mock

from companion import research
from companion.tracker import Tracker

PLAYER, OTHER, MONSTER = 0x50000001, 0x50000002, 0x80000100
CELL = 0xA9B4001F
BOLT, HEAL, BANE = 10, 11, 12
CREATURE, ITEM = 0x31, 0x39


class TargetTypeTest(unittest.TestCase):
    def test_the_last_target_component_decides(self):
        self.assertEqual(research.target_type({"formula": [1, 2, 3, 4, 5, CREATURE, 0, 0]}), "creature")
        self.assertEqual(research.target_type({"formula": [1, 2, 3, 4, 5, CREATURE, ITEM, 0]}), "item")
        self.assertIsNone(research.target_type({"formula": [1, 2, 3, 4, 5, 0, 0, 0]}))

    def test_reach_is_capped(self):
        self.assertEqual(research.reach({"range": (10.0, 0.1)}, 100), 20.0)
        self.assertEqual(research.reach({"range": (50.0, 1.0)}, 100), research.MAX_REACH)


class ExaminedTest(unittest.TestCase):
    def setUp(self):
        self.tr = tr = Tracker({}, clock=lambda: 0.0)
        tr.reset("x")
        tr.player = PLAYER
        tr.position = {"cell": CELL, "x": 70.0, "y": 100.0, "z": 0.0, "qw": 1, "qz": 0, "ts": 0.0}
        here = lambda x: {"cell": CELL, "x": x, "y": 100.0, "z": 0.0}
        tr.objects.update({PLAYER: {"name": "Me", "pos": here(70.0)},
                           OTHER: {"name": "Friend", "pos": here(80.0)},
                           MONSTER: {"name": "Drudge", "wcid": "1", "pos": here(80.0)}})
        tr.spells = {BOLT: {"name": "Frost Bolt I", "range": (20.0, 0.0)},
                     HEAL: {"name": "Heal Other I", "range": (20.0, 0.0)}}
        self.bolt = {"id": BOLT, "needs_target": True, "target_type": "creature", "beneficial": False, "skill": 50}
        self.heal = {"id": HEAL, "needs_target": True, "target_type": "creature", "beneficial": True, "skill": 50}

    def appraise(self, guid):
        with mock.patch("companion.tracker.MONSTERS", {"1": {}}):
            self.tr.handle({"type": "appraise", "object": guid}, 1.0)

    def test_nothing_appraised(self):
        self.assertFalse(self.tr.examined_check(self.bolt)["ok"])
        self.assertIsNone(self.tr.examined_check({"id": BANE, "needs_target": True, "target_type": "item"}))
        self.assertIsNone(self.tr.examined_check({"id": 1, "needs_target": False, "target_type": "creature"}))

    def test_harmful_on_anyone_helpful_on_a_player(self):
        self.appraise(MONSTER)
        self.assertTrue(self.tr.examined_check(self.bolt)["ok"])
        self.assertIn("only on a player", self.tr.examined_check(self.heal)["why"])
        self.appraise(OTHER)
        self.assertTrue(self.tr.examined_check(self.heal)["ok"])
        self.assertTrue(self.tr.examined_check(self.bolt)["ok"])   # harmful at a player: lands, no effect

    def test_yourself_doesnt_count(self):
        self.appraise(MONSTER)
        self.appraise(PLAYER)                                     # (that's you)
        self.assertEqual(self.tr.examined["guid"], MONSTER)

    def test_out_of_reach_or_gone(self):
        self.appraise(MONSTER)
        self.tr.objects[MONSTER]["pos"] = {"cell": CELL, "x": 89.0, "y": 100.0, "z": 0.0}   # 19 m: inside the margin
        self.assertIn("too far", self.tr.examined_check(self.bolt)["why"])
        self.tr.objects[MONSTER]["pos"] = {"cell": CELL, "x": 80.0, "y": 100.0, "z": 0.0}
        self.tr.objects[MONSTER]["dead"] = True
        self.assertIn("gone", self.tr.examined_check(self.bolt)["why"])


class WeaponSpellTest(unittest.TestCase):
    def test_weapon_spells_go_on_your_weapons(self):
        tr = Tracker({}, clock=lambda: 0.0)
        tr.reset("x")
        tr.player = PLAYER
        sword = 0x80000200
        tr.objects[sword] = {"name": "Sword", "wielder": PLAYER, "location": 0x100000, "item_type": 0x1}
        with mock.patch.object(Tracker, "weapon_spells", lambda self: {BOLT}), \
             mock.patch("companion.research.research_table",
                        lambda *a: [{"id": BOLT, "needs_target": True, "target_type": "item"},
                                    {"id": HEAL, "needs_target": True, "target_type": "item"}]):
            rows = {r["id"]: r for r in tr.research()["spells"]}
        self.assertEqual(rows[BOLT]["default_item"], sword)
        self.assertNotIn("default_item", rows[HEAL])               # not a weapon spell (a bane): no default
        self.assertTrue(rows[HEAL]["on_item"])

    def test_the_dropdown_holds_what_the_server_enchants(self):
        tr = Tracker({}, clock=lambda: 0.0)
        tr.reset("x")
        tr.player = PLAYER
        tr.objects.update({
            0x80000201: {"name": "Sword", "wielder": PLAYER, "location": 0x100000, "item_type": 0x1},
            0x80000202: {"name": "Wand", "wielder": PLAYER, "location": 0x1000000, "item_type": 0x8000},   # a caster: no
            0x80000203: {"name": "Ring", "container": PLAYER, "item_type": 0x8},
            0x80000204: {"name": "Gems", "container": PLAYER, "item_type": 0x800, "stack": 5, "max_stack": 100},  # a stack: no
        })
        self.assertEqual(sorted(i["name"] for i in tr.item_targets()), ["Ring", "Sword"])


if __name__ == "__main__":
    unittest.main()
