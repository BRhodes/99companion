"""Max vitals the way the server works them out: buffed attributes, then vitae and max-vital buffs."""

import unittest

from companion.tracker import Tracker


def ench(spell, category, stat_type, key, value, power=100):
    return {"id": spell, "spell": spell, "layer": 0, "category": category, "power": power, "start": 0.0,
            "duration": 900.0, "caster": 1, "stat": {"type": stat_type, "key": key, "value": value}}


class VitalsTest(unittest.TestCase):
    def tracker(self):
        tr = Tracker({}, clock=lambda: 1000.0)
        tr.reset("x")
        tr.attributes = {2: 25, 6: 121}                          # Endurance, Self
        tr.vitals = {4: {"current": 10, "init": 0, "raises": 21}, 6: {"current": 10, "init": 0, "raises": 6}}
        return tr

    def max_of(self, tr, name):
        return next(v["max"] for v in tr._vital_snapshot() if v["name"] == name)

    def test_unbuffed(self):
        tr = self.tracker()
        self.assertEqual((self.max_of(tr, "Stamina"), self.max_of(tr, "Mana")), (46, 127))

    def test_attribute_buffs_raise_the_max(self):
        tr = self.tracker()
        tr.handle({"type": "enchantments", "list": [ench(1, 10, 0x8001, 2, 20.0), ench(2, 11, 0x8001, 6, 20.0)]}, 1000.0)
        self.assertEqual((self.max_of(tr, "Stamina"), self.max_of(tr, "Mana")), (66, 147))

    def test_vitae_lowers_it(self):
        tr = self.tracker()
        tr.handle({"type": "enchantments", "list": [ench(666, 204, 0x4002, 0, 0.9)]}, 1000.0)
        self.assertEqual(self.max_of(tr, "Stamina"), 41)        # 46 x 0.9 = 41.4


if __name__ == "__main__":
    unittest.main()
