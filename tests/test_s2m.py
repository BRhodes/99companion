"""The S2M table: net mana from casting Revitalize Self then Stamina to Mana Self, over and over."""

import unittest

from companion import s2m
from companion.tracker import Tracker

ROMAN = ["I", "II", "III", "IV", "V", "VI"]
SPELLS = {}
for n, r in enumerate(ROMAN, 1):
    SPELLS[1176 + n] = {"name": f"Revitalize Self {r}", "school": 2, "power": 50 * n - 49 if n > 1 else 1, "mana": 10 * n}
    SPELLS[1675 + n] = {"name": f"Stamina to Mana Self {r}", "school": 2, "power": 50 * n - 49 if n > 1 else 1, "mana": 10 * n}


class S2MTest(unittest.TestCase):
    def tracker(self, life=400, stamina_max=1000):
        tr = Tracker(SPELLS, clock=lambda: 1000.0)
        tr.reset("x")
        tr.vitals = {4: {"current": 1, "init": 0, "raises": 0}}
        tr.magic_skills = lambda: {2: life}
        tr._vital_snapshot = lambda: [{"name": "Stamina", "current": 1, "max": stamina_max}]
        return tr

    def test_no_mana_conversion(self):
        t = s2m.table(self.tracker())
        self.assertEqual(len(t["cells"]), 6)
        self.assertTrue(all(len(row) == 6 for row in t["cells"]))
        # plenty of room: each Stamina to Mana takes back what Revitalize added (15.5 on average for I),
        # 25% lost at I, less 10 + 10 cost
        self.assertAlmostEqual(t["cells"][0][0], 15.5 * 0.75 - 20, delta=1.5)
        self.assertAlmostEqual(t["cells"][5][5], 113 * 1.5 - 120, delta=2)
        self.assertIsNone(t["inputs"]["mana_conversion"])

    def test_capped_by_max_stamina(self):
        t = s2m.table(self.tracker(stamina_max=100))
        # Revitalize VI overflows a 100 bar: each Stamina to Mana takes half of it, not 113
        self.assertAlmostEqual(t["cells"][5][5], 50 * 1.5 - 120, delta=1)

    def test_a_level_you_cant_land(self):
        t = s2m.table(self.tracker(life=150))
        r, c = t["rows"][0], t["cols"][5]
        self.assertLess(c["chance"], 0.01)
        self.assertAlmostEqual(c["cost"], 5, places=0)       # mostly fizzles at 5 mana
        # Stamina to Mana VI never lands, so the pair only costs: no mana from the stamina piling up
        self.assertLess(t["cells"][0][5], 0)
        self.assertAlmostEqual(t["cells"][0][5], -(r["cost"] + c["cost"]), delta=2)

    def test_mana_conversion_cuts_cost(self):
        self.assertEqual(s2m.expected_cost(60, 250, None), 60)
        cut = s2m.expected_cost(60, 250, 300)
        self.assertLess(cut, 60)
        self.assertGreaterEqual(cut, 1)
        self.assertGreaterEqual(s2m.expected_cost(1, 1, 1000), 1)

    def test_nothing_without_a_character(self):
        self.assertIsNone(s2m.table(Tracker({}, clock=lambda: 1000.0)))
        self.assertIsNone(s2m.table(self.tracker(stamina_max=0)))


if __name__ == "__main__":
    unittest.main()
