"""The dashboard buff layout: harmful marking, skill rows, and bane estimates from your own casts."""

import unittest

from companion import buffpanel
from companion.tracker import Tracker

PLAYER, ARMOR = 0x50000001, 0x80001234
SPELLS = {
    1567: {"name": "Mana Conversion Mastery Self II", "duration": 360.0, "school": 4},
    517: {"name": "Acid Vulnerability Self II", "duration": 360.0, "school": 2},
    1482: {"name": "Impenetrability II", "duration": 420.0, "school": 3},
}


def ench(spell, category, power, stat, start=0.0, duration=450.0, caster=PLAYER):
    return {"id": spell, "spell": spell, "layer": 0, "category": category, "power": power, "start": start,
            "duration": duration, "caster": caster, "stat": stat}


class BuffPanelTest(unittest.TestCase):
    def tracker(self):
        tr = Tracker(SPELLS, clock=lambda: 1000.0)
        tr.reset("x")
        tr.player = PLAYER
        tr.attributes = {5: 100, 6: 100}
        tr.skills = {16: {"id": 16, "sac": 2, "level": 18}}          # Mana Conversion: round((100+100)/6) + 18
        tr.skill_formulas[16] = (0, 1, 1, 6, 5, 6)
        tr.objects[ARMOR] = {"name": "Platemail Hauberk", "wcid": 1, "stack": 1, "container": PLAYER}
        return tr

    def test_rows_and_harmful(self):
        tr = self.tracker()
        tr.handle({"type": "enchantments", "list": [
            ench(1567, 47, 50, {"type": 0x9010, "key": 16, "value": 15.0}),
            ench(517, 96, 50, {"type": 0x5008, "key": 69, "value": 1.25})]}, 1000.0)
        bp = buffpanel.build(tr, 1000.0)
        mc = next(s for s in bp["skills"] if s["name"] == "Mana Conversion")
        self.assertEqual((mc["base"], mc["value"]), (51, 66))
        self.assertFalse(mc["spells"][0]["harmful"])
        acid = next(p for p in bp["prots"] if p["name"] == "Acid")
        self.assertTrue(acid["spells"][0]["harmful"])                 # vulnerability: takes 25% more acid
        self.assertAlmostEqual(bp["duration_factor"], 450 / 360)        # learned from your own buffs

    def test_bane_estimate_from_a_landed_item_cast(self):
        tr = self.tracker()
        tr.handle({"type": "enchantments", "list": [ench(1567, 47, 50, {"type": 0x9010, "key": 16, "value": 15.0})]}, 990.0)
        tr.handle({"type": "cast", "spell": 1482, "target": ARMOR, "aseq": 1}, 1000.0)
        tr.handle({"type": "text", "text": "You cast Impenetrability II on Platemail Hauberk", "channel": 7}, 1003.0)
        imp = next(b for b in buffpanel.build(tr, 1003.0)["banes"] if b["name"] == "Impenetrability")
        self.assertEqual((imp["pieces"], imp["items"]), (1, ["Platemail Hauberk"]))
        self.assertAlmostEqual(imp["left"], 420 * 1.25)                # table duration x the factor seen on you
        self.assertEqual(buffpanel.build(tr, 1003.0 + 600)["banes"][0]["pieces"], 0)  # expired

    def test_harmful_rules(self):
        self.assertTrue(buffpanel.harmful({"type": 0x9001, "key": 1, "value": -10}))     # Feeblemind
        self.assertFalse(buffpanel.harmful({"type": 0x5008, "key": 67, "value": 0.5}))   # Fire Protection
        self.assertTrue(buffpanel.harmful({"type": 0x5008, "key": 5, "value": 0.5}))     # mana rate halved



class ChoicesTest(unittest.TestCase):
    """The buff-profile editor's rows: each stat with the spell for every tier, your known ones marked."""

    def test_rows_match_the_right_spells(self):
        from companion.buffpanel import choices
        spells = {
            2: {"name": "Strength Self I", "bitfield": 0x0C, "category": 1},
            3: {"name": "Strength Other I", "bitfield": 0x04, "category": 1},
            24: {"name": "Armor Self I", "bitfield": 0x0E, "category": 109},
            6: {"name": "Heal Self I", "bitfield": 0x0C, "category": 63},
            37: {"name": "Blade Bane I", "bitfield": 0x04, "category": 168},
            781: {"name": "Weapon Expertise Other II", "bitfield": 0x06, "category": 57},
            54: {"name": "Rejuvenation Self I", "bitfield": 0x0C, "category": 89, "duration": 1800},
            211: {"name": "Revitalize Self I", "bitfield": 0x0C, "category": 90},          # instant: not a buff
        }
        mods = {2: [36865, 1, 10], 3: [36865, 1, 10], 24: [41088, 0, 20], 6: [128, 8, 7], 37: [36872, 13, 0.111],
                781: [36880, 28, 15], 54: [20488, 4, 1.1], 211: [128, 4, 10]}

        class Burn:
            def required(self, sid):
                return {"Lead Scarab": 1} if sid == 2 else {}

        class Tr:
            skills, known_spells, burn = {}, {2: 1, 54: 1, 211: 1}, Burn()

            def inventory_counts(self):
                return {}                                   # no scarab: Strength I is missing its component
        Tr.spells = spells
        Tr = Tr()
        rows = {(r["section"], r["name"]): r for r in choices(Tr, mods)}
        self.assertEqual([(o["spell"], o["known"], o["missing"]) for o in rows[("Attributes", "Strength")]["options"]],
                         [(2, True, ["Lead Scarab"])])
        self.assertEqual(rows[("Protections", "Armor")]["options"][0]["spell"], 24)       # not Heal
        self.assertEqual(rows[("Banes", "Blade")]["options"][0]["spell"], 37)
        self.assertNotIn(("Banes", "Impenetrability"), rows)                               # not Weapon Expertise
        self.assertIn(("Regeneration", "Rejuvenation"), rows)
        self.assertEqual(rows[("Regeneration", "Rejuvenation")]["options"][0]["spell"], 54)
        self.assertNotIn(("Other", "Revitalize"), rows)


if __name__ == "__main__":
    unittest.main()
