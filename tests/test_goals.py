"""Goals: reading a character's raises back, and the cheapest raises to reach a goal."""

import itertools
import unittest
from types import SimpleNamespace

from companion import goals

# small cost tables that grow like the game's (cumulative XP for n raises / rank n)
A = [0] + list(itertools.accumulate(100 + 10 * n for n in range(60)))
V = [0] + list(itertools.accumulate(50 + 5 * n for n in range(60)))
T = [0] + list(itertools.accumulate(60 + 8 * n for n in range(80)))
S = [0] + list(itertools.accumulate(30 + 4 * n for n in range(90)))
TABLES = {"attribute": A, "vital": V, "trained": T, "specialized": S, "level": [0, 0, 1000]}
MAGIC = (0, 1, 1, 4, 5, 6)          # (Focus + Self) / 4


def wire_cp(value, raises, table=A):
    """What the server sends as an attribute's XP spent (WeenieObject.cpp Remap1999AttributeForWire)."""
    top = table[value + 1] if value + 1 < len(table) else 0xFFFFFFFF
    return (top - table[raises + 1] + table[raises]) % (1 << 32)


def char(focus=50, self_=50, raises=10, war_rank=20):
    tr = SimpleNamespace(
        attributes={5: focus, 6: self_, 2: 40}, attribute_cp={5: wire_cp(focus, raises), 6: wire_cp(self_, raises), 2: wire_cp(40, 0)},
        vitals={2: {"raises": 5, "init": 0, "cp": V[5], "current": 30}},
        skills={34: {"sac": 2, "pp": T[war_rank], "level": war_rank}},
        vital_formulas={1: (0, 1, 0, 2, 2, 0)}, skill_formulas={34: MAGIC}, xp_unassigned=5000, xp_total=100000)
    return tr


class ReadBackTest(unittest.TestCase):
    def test_attribute_raises_from_the_wire(self):
        for value, raises in ((50, 10), (40, 0), (58, 3)):
            self.assertEqual(goals.attribute_raises(value, wire_cp(value, raises), A), (raises, A[raises]))

    def test_past_the_tables_end(self):
        self.assertEqual(goals.attribute_raises(200, wire_cp(200, 7), A), (7, A[7]))

    def test_rank_of(self):
        self.assertEqual(goals.rank_of(T[12], T), 12)
        self.assertEqual(goals.rank_of(T[12] + 5, T), 12)


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.ch = goals.character(char(), TABLES)

    def brute(self, target):
        """The cheapest by trying every Focus / Self raise."""
        best = None
        for f in range(0, 45):
            for s in range(0, 45):
                if 10 + f >= len(A) or 10 + s >= len(A):
                    continue
                cost = A[10 + f] - A[10] + A[10 + s] - A[10]
                have = goals.formula_value(MAGIC, {5: 50 + f, 6: 50 + s}) + 20
                need = max(0, target - have)
                if 20 + need >= len(T):
                    continue
                cost += T[20 + need] - T[20]
                best = cost if best is None else min(best, cost)
        return best

    def test_the_least_xp(self):
        for target in range(46, 101):
            p = goals.plan({"name": "War", "targets": {"skill:34": target}}, self.ch, TABLES)
            self.assertTrue(p["feasible"])
            self.assertEqual(p["cost"], self.brute(target), target)
            self.assertEqual(p["targets"][0]["distance"], max(0, target - 45))

    def test_an_attribute_goal_and_a_vital_from_it(self):
        p = goals.plan({"targets": {"attribute:2": 46, "vital:2": 40}}, self.ch, TABLES)
        end = next(r for r in p["raises"] if r["name"] == "Endurance")
        self.assertGreaterEqual(end["to"], 46)
        health = next((r for r in p["raises"] if r["name"] == "Health"), None)
        self.assertEqual((health["to"] if health else goals.values(self.ch, {2: end["raises"]})[("vital", 2)]) >= 40, True)

    def test_out_of_reach_and_untrained(self):
        self.assertFalse(goals.plan({"targets": {"skill:34": 5000}}, self.ch, TABLES)["feasible"])
        p = goals.plan({"targets": {"skill:33": 100}}, self.ch, TABLES)
        self.assertIn("isn't trained", p["problems"][0])

    def test_now_spends_only_what_you_have_cheapest_first(self):
        p = goals.plan({"targets": {"skill:34": 80}}, self.ch, TABLES, unassigned=5000)
        self.assertLessEqual(sum(n["cost"] for n in p["now"]), 5000)
        self.assertTrue(p["now"])
        self.assertEqual(p["still"], p["cost"] - 5000)

    def test_buffed_targets_count_the_buffs(self):
        bonus = {"attributes": {5: 10, 6: 10}, "skills": {34: 15}}
        plain = goals.plan({"targets": {"skill:34": 80}}, self.ch, TABLES)
        buffed = goals.plan({"targets": {"skill:34": 80}, "buffed": True}, self.ch, TABLES, bonus=bonus)
        self.assertEqual(buffed["targets"][0]["current"], 45 + 5 + 15)      # Focus / Self +10 each: +5; the skill +15
        self.assertLess(buffed["cost"], plain["cost"])
        self.assertEqual(goals.plan({"targets": {"skill:34": 80}}, self.ch, TABLES, bonus=bonus)["cost"], plain["cost"])

    def test_buff_levels(self):
        # the skill with its attributes at a level II buff (+15) and itself at level III (+20): as if those were on
        by_levels = goals.plan({"targets": {"skill:34": 80}, "buffs": {"skill:34": {"attr": 2, "skill": 3}}}, self.ch, TABLES)
        same = goals.plan({"targets": {"skill:34": 80}, "buffed": True}, self.ch, TABLES,
                          bonus={"attributes": {5: 15, 6: 15, 2: 15}, "skills": {34: 20}})
        self.assertEqual((by_levels["targets"][0]["current"], by_levels["cost"]), (same["targets"][0]["current"], same["cost"]))
        # set, they take the place of the profile's buffs
        profile = goals.plan({"targets": {"skill:34": 80}, "buffed": True, "buffs": {"skill:34": {"attr": 2, "skill": 3}}},
                             self.ch, TABLES, bonus={"attributes": {5: 1}, "skills": {}})
        self.assertEqual(profile["cost"], by_levels["cost"])
        self.assertFalse(goals.has_levels({"buffs": {"skill:34": {"attr": 0}}}))

    def test_saved_goals_keep_their_buff_levels(self):
        from companion import layouts
        got = layouts._goals([{"name": "MD", "targets": {"skill:6": 280, "attribute:2": 100},
                               "buffs": {"skill:6": {"attr": 6, "skill": 6}, "attribute:2": {"attr": 9}, "skill:99": {"skill": 3}}}])
        self.assertEqual(got[0]["buffs"], {"skill:6": {"attr": 6, "skill": 6}})   # (a bad level and a key not a target: gone)

    def test_each_target_its_own_buffs(self):
        # an attribute target buffed, a skill target not: each measured with its own
        p = goals.plan({"targets": {"attribute:5": 70, "skill:34": 60}, "buffs": {"attribute:5": {"attr": 6}}}, self.ch, TABLES)
        cur = {t["key"]: t["current"] for t in p["targets"]}
        self.assertEqual(cur["attribute:5"], 50 + 35)
        self.assertEqual(cur["skill:34"], goals.plan({"targets": {"skill:34": 60}}, self.ch, TABLES)["targets"][0]["current"])

    def test_skill_for_a_cast_chance(self):
        from companion import research
        for power, chance in ((100, 0.9), (150, 0.3), (200, 0.75)):
            need = goals.skill_for_chance(power, chance)
            self.assertGreaterEqual(research.success_chance(need, power), chance)
            self.assertLess(research.success_chance(need - 1, power), chance)
        self.assertEqual(goals.skill_for_chance(100, 0.9), 132)

    def test_max_focus_is_cast_over_its_relax_tier(self):
        """Focus on Max: its Relax tier goes on first (from the unbuffed skill), the Max tier's cast with that on --
        not with whatever's on now -- and while you cast, it's the Relax tier that's on."""
        from companion import buffpanel
        tr = char(focus=50, self_=50)
        tr.skills[31] = {"sac": 2, "pp": T[20], "level": 20}       # Creature: (50 + 50) / 4 + 20 = 45 unbuffed
        tr.skill_formulas[31] = MAGIC
        tr.spells = {2: {"name": "Focus Self II", "school": 4, "power": 10}, 3: {"name": "Focus Self III", "school": 4, "power": 50},
                     4: {"name": "Focus Self IV", "school": 4, "power": 100}}
        tr.magic_skills = lambda: {4: 500}                           # (live buffs: ignored for these)
        rows = [{"category": 77, "name": "Focus", "options": [{"level": n, "spell": n, "known": True} for n in (2, 3, 4)]}]
        mods = {2: (0x9001, 5, 40), 3: (0x9001, 5, 60), 4: (0x9001, 5, 80)}
        prefs = {"buff_profiles": [{"name": "p", "max_chance": 0.3, "buffs": [{"category": 77, "level": "max"}]}]}
        real = buffpanel.choices
        buffpanel.choices = lambda tr_, mods_: rows
        try:
            ch = goals.character(tr, TABLES)
            _prof, ((_pb, _row, _lvl, cast, on),) = goals._resolved(tr, prefs, mods, ch)
            # Relax from 45: II (power 10) lands 90% at 42; III needs 82 -> II, +40 Focus: Creature 45 + 10 = 55
            # Max (30%) from 55: III (power 50) needs 38, IV (100) needs 88 -> III
            self.assertEqual((cast["level"], on["level"]), (3, 2))
            self.assertEqual(goals.buff_bonus(tr, prefs, mods, ch)["attributes"], {5: 40})
        finally:
            buffpanel.choices = real

    def test_summary(self):
        s = goals.summary(char(), TABLES, [{"name": "War", "targets": {"skill:34": 70}}])
        self.assertEqual(s["invested"]["attributes"], 2 * A[10])
        self.assertEqual(s["gained"], 2 * A[10] + V[5] + T[20] + 5000)
        self.assertEqual(s["goals"][0]["name"], "War")


if __name__ == "__main__":
    unittest.main()
