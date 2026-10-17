"""The dashboard's red light: your own buffs that ran out, until they're cast again."""

import unittest

from companion.tracker import Tracker

ME, OTHER = 0x500000C9, 0x50000014
SPELLS = {1: {"name": "Strength Self I", "bitfield": 0x0E, "category": 1},
          2: {"name": "Strength Self II", "bitfield": 0x0E, "category": 1},
          3: {"name": "Weakness Self I", "bitfield": 0x0A, "category": 2}}   # not beneficial


def ench(eid, spell, caster=ME):
    return {"id": eid, "spell": spell, "layer": 1, "category": SPELLS[spell]["category"], "power": 10,
            "caster": caster, "start": 0, "duration": 600, "stat": {}}


class ExpiredBuffsTest(unittest.TestCase):
    def setUp(self):
        self.tr = Tracker(SPELLS)
        self.tr.player = ME

    def expired(self):
        return [x["name"] for x in self.tr.expired.values()]

    def test_a_buff_that_runs_out_is_reported_until_recast(self):
        tr = self.tr
        tr.handle({"type": "enchantments", "list": [ench(10, 1)]}, 100)
        tr.handle({"type": "enchantments_removed", "ids": [10], "why": "expired"}, 700)
        self.assertEqual(self.expired(), ["Strength Self I"])
        tr.handle({"type": "enchantments", "list": [ench(11, 2)]}, 710)
        self.assertEqual(self.expired(), [])

    def test_replaced_by_a_stronger_one_is_not_lost(self):
        tr = self.tr
        tr.handle({"type": "enchantments", "list": [ench(10, 1)]}, 100)
        tr.handle({"type": "enchantments", "list": [ench(11, 2)]}, 200)      # the new one lands first
        tr.handle({"type": "enchantments_removed", "ids": [10], "why": "removed"}, 200)
        self.assertEqual(self.expired(), [])

    def test_only_your_own_beneficial_buffs(self):
        tr = self.tr
        tr.handle({"type": "enchantments", "list": [ench(10, 1, caster=OTHER), ench(12, 3)]}, 100)
        tr.handle({"type": "enchantments_removed", "ids": [10, 12], "why": "expired"}, 700)
        self.assertEqual(self.expired(), [])


if __name__ == "__main__":
    unittest.main()
