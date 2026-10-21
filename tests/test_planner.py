"""The character planner: a made-up character's start, its plan, credits and cast chances."""

import unittest

from companion import goals, planner
from tests.test_goals import MAGIC, TABLES, T, S

BASES = {34: {"trained_cost": 16, "specialized_cost": 28, "formula": MAGIC},
         33: {"trained_cost": 12, "specialized_cost": 20, "formula": MAGIC},
         15: {"trained_cost": 0, "specialized_cost": 12, "formula": (0, 1, 1, 10, 6, 5)}}
VITALS = {1: (0, 1, 0, 2, 2, 0), 3: (0, 1, 0, 1, 2, 0), 5: (0, 1, 0, 1, 6, 0)}
LEVEL_XP = [0, 0, 1000, 5000, 20000, 60000, 150000]
LEVEL_CREDITS = [0, 0, 1, 1, 1, 1, 1]


def build(**kw):
    b = {"name": "Mage", "attributes": {"1": 10, "2": 40, "3": 10, "4": 70, "5": 100, "6": 100},
         "skills": {"34": "specialized"}, "targets": {}}
    b.update(kw)
    return b


class CharacterTest(unittest.TestCase):
    def test_creation_bonus_ranks(self):
        ch = planner.character(build(skills={"34": "specialized", "33": "trained"}), TABLES, BASES, VITALS)
        self.assertEqual((ch["skills"][34]["rank"], ch["skills"][34]["xp"]), (10, S[10]))
        self.assertEqual((ch["skills"][33]["rank"], ch["skills"][33]["xp"]), (5, T[5]))
        self.assertIn(15, ch["skills"])                      # (free: it comes trained)
        later = planner.character(build(chargen=False), TABLES, BASES, VITALS)
        self.assertEqual(later["skills"][34]["rank"], 0)

    def test_attributes_kept_in_range(self):
        ch = planner.character(build(attributes={"5": 250, "6": 3}), TABLES, BASES, VITALS)
        self.assertEqual((ch["attributes"][5]["value"], ch["attributes"][6]["value"]), (100, 10))

    def test_credits(self):
        self.assertEqual(planner.credits_used(build(skills={"34": "specialized", "33": "trained"}), BASES), 28 + 12)
        self.assertEqual(planner.credits_at(4, LEVEL_CREDITS), 50 + 3)


class PlanTest(unittest.TestCase):
    def test_the_plan_matches_goals(self):
        b = build(targets={"skill:34": 120})
        got = planner.plan(b, TABLES, BASES, VITALS, LEVEL_XP, LEVEL_CREDITS)
        ch = planner.character(b, TABLES, BASES, VITALS)
        self.assertEqual(got["cost"], goals.plan({"targets": {"skill:34": 120}}, ch, TABLES)["cost"])
        war = next(s for s in got["stats"] if s["key"] == "skill:34")
        self.assertGreaterEqual(war["end"], 120)
        self.assertEqual(got["xp_level"], planner.level_for(got["cost"], LEVEL_XP))

    def test_a_buff_tier_counts_toward_the_targets(self):
        plain = planner.plan(build(targets={"skill:34": 120}), TABLES, BASES, VITALS, LEVEL_XP, LEVEL_CREDITS)
        buffed = planner.plan(build(targets={"skill:34": 120}, buff=6), TABLES, BASES, VITALS, LEVEL_XP, LEVEL_CREDITS)
        self.assertLess(buffed["cost"], plain["cost"])
        war = next(s for s in buffed["stats"] if s["key"] == "skill:34")
        self.assertGreaterEqual(war["buffed"], 120)
        self.assertIn(war["buffed"] - war["end"], (35 + 17, 35 + 18))   # (its own +35, and Focus and Self's +35 / 4, rounded)

    def test_credits_past_creation_need_levels(self):
        b = build(skills={"34": "specialized", "33": "specialized", "15": "specialized"})   # 28 + 20 + 12 = 60
        got = planner.plan(b, TABLES, BASES, VITALS, LEVEL_XP, LEVEL_CREDITS)
        self.assertEqual(got["credits"]["over_creation"], 10)
        self.assertIsNone(got["credits"]["level"])              # (these tables only give 5 more)

    def test_cast_chances(self):
        got = planner.plan(build(targets={"skill:34": 250}), TABLES, BASES, VITALS, LEVEL_XP, LEVEL_CREDITS)
        war = next(m for m in got["magic"] if m["school"] == "War")
        self.assertAlmostEqual(war["chances"]["base"][5], planner.cast_chance(war["skill"], 250), places=3)
        self.assertIsNone(next(m for m in got["magic"] if m["school"] == "Item")["skill"])     # (not trained)
        self.assertAlmostEqual(planner.cast_chance(250, 250), 0.5)

    def test_hours(self):
        got = planner.plan(build(targets={"skill:34": 120}, rate=1000), TABLES, BASES, VITALS, LEVEL_XP, LEVEL_CREDITS)
        self.assertEqual(got["hours"], round(got["cost"] / 1000, 1))


if __name__ == "__main__":
    unittest.main()
