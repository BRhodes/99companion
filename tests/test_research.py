"""Spell research: effective magic skill, success chance, and the per-account learn table."""

import struct
import unittest

from companion import research

FORMULAS = dict(research.DEFAULT_SKILL_FORMULAS)
LIFE = 33


def ench(spell, category, power, stat_type, key, value, started=0.0):
    return {"spell_id": spell, "category": category, "power": power, "started": started,
            "stat": {"type": stat_type, "key": key, "value": value}}


class SkillTest(unittest.TestCase):
    def test_unbuffed(self):
        skills = {LIFE: {"sac": 2, "level": 15}}            # training bonus + raises
        attrs = {5: 100, 6: 110}                              # Focus, Self: round(210 / 4) = 53
        self.assertEqual(research.effective_skill(LIFE, skills, attrs, [], FORMULAS), 68)

    def test_buffs_strongest_per_category_and_vitae(self):
        skills = {LIFE: {"sac": 2, "level": 15}}
        attrs = {5: 100, 6: 110}
        buffs = [ench(1, 50, 50, 0x9010, LIFE, 15.0), ench(2, 50, 100, 0x9010, LIFE, 20.0),  # same category: +20 wins
                 ench(3, 60, 50, 0x9001, 5, 10.0),                                            # Focus +10 -> round(220/4)=55
                 ench(666, 204, 0, 0x5001, 0, 0.9)]                                           # vitae 10%
        # (55 + 15) * 0.9 = 63, + 20 = 83
        self.assertEqual(research.effective_skill(LIFE, skills, attrs, buffs, FORMULAS), 83)

    def test_untrained_is_none(self):
        self.assertIsNone(research.effective_skill(LIFE, {LIFE: {"sac": 1, "level": 0}}, {5: 100, 6: 100}, [], FORMULAS))

    def test_chance(self):
        self.assertAlmostEqual(research.success_chance(100, 100), 0.5)
        self.assertGreater(research.success_chance(132, 100), 0.9)


class TableTest(unittest.TestCase):
    # Mana Renewal Self III's base formula from the spell table (8 slots), and the components it names
    BASE = [3, 73, 11, 64, 26, 42, 61, 0]
    COMPONENTS = {3: {"name": "Copper Scarab"}, 73: {"name": "White Taper"}, 11: {"name": "Mugwort"},
                  70: {"name": "Indigo Taper"}, 64: {"name": "Pink Taper"}, 26: {"name": "Powdered Amber"},
                  42: {"name": "Gypsum"}, 61: {"name": "Willow Talisman"}}
    SPELLS = {214: {"name": "Mana Renewal Self III", "school": 2, "school_name": "Life", "power": 100, "bitfield": 0x0E,
                    "formula": BASE},
              215: {"name": "Mana Renewal Other III", "school": 2, "school_name": "Life", "power": 100, "bitfield": 0x06,
                    "formula": BASE},
              900: {"name": "Monster Thing", "school": 2, "school_name": "Life", "power": 100, "bitfield": 0x80,
                    "formula": BASE}}
    HAVE = {"Copper Scarab": 1, "White Taper": 2, "Mugwort": 5, "Indigo Taper": 1, "Powdered Amber": 3,
            "Gypsum": 3, "Willow Talisman": 1}

    def table(self, carried, known=()):
        rows = research.research_table(self.SPELLS, self.COMPONENTS, "zorn", set(known), carried, {2: 120})
        return {r["id"]: r for r in rows}

    def test_personal_formula_and_learnable(self):
        rows = self.table(self.HAVE)
        self.assertNotIn(900, rows)                                   # NotResearchable
        r = rows[214]
        self.assertEqual(r["formula"], ["Copper Scarab", "White Taper", "Mugwort", "Indigo Taper", "Powdered Amber",
                                        "Gypsum", "Willow Talisman"])  # zorn's slot 3 is an Indigo Taper
        self.assertEqual((r["level"], r["target"], r["can_learn"]), (3, "self", True))
        self.assertGreater(r["chance"], 0.75)

    def test_reasons(self):
        rows = self.table({**self.HAVE, "Indigo Taper": 0})
        self.assertEqual((rows[214]["can_learn"], rows[214]["missing"]), (False, {"Indigo Taper": 1}))
        self.assertTrue(rows[215]["needs_target"])                    # not self-targeted: researched at a target
        self.assertNotIn("target", rows[215]["reason"] or "")         # which no longer stops it being learned
        self.assertEqual(self.table(self.HAVE, known={214})[214]["reason"], "already known")



if __name__ == "__main__":
    unittest.main()
