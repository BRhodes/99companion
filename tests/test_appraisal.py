"""An appraised item: the 1999 appraisal decoded (Appraisal.cpp AppraisalProfile::Pack), as a line of text, and the
companion copying it (once, live only, when it's turned on)."""

import struct
import types
import unittest
from unittest import mock

from companion import appraisal
from companion.__main__ import Companion
from companion.messages import decode
from tests.wire import Msg

PLAYER, BOW = 0x500000C9, 0x8000ABCD


def bow_appraisal():
    """Bloodstone Yumi (from a real appraisal): weapon, value / burden, inscription, magic."""
    flags = 0x2 | 0x8 | 0x10 | 0x800000
    m = Msg(0xF7B0).u32(PLAYER).u32(7).u32(0xC9).u32(BOW).u32(flags).i32(1)
    m.u32(0).i32(41).u32(2).i32(0)                                       # weapon: no damage type, speed 41, Bow, 0
    for v in (0.0, 0.0, 27.3, 1.0, 1.0):                                 # variance, length, velocity, defense, offense
        m.f64(v)
    m.u32(0xFFFFFFFF).u32(1109).u32(980)                                 # mass (unset), value, burden
    m.string("Str II").string("Magastar").u16(0)                         # inscription, scribe, (no account: 2 bytes, no align)
    m.u32(3).u32(51).u32(0xFFFFFFFF).u32(139).u32(156).u32(0xFFFFFFFF).u32(0)   # diff, spellcraft, rank, mana, max, -, -
    m.string("Sho").u32(71).u32(2).string("Strength Other II").u16(0)   # heritage, Bow 71+, spells, (no extra line)
    m.b += struct.pack("<d", -1.0)
    return m.bytes()


class DecodeTest(unittest.TestCase):
    def test_an_items_sections(self):
        (ev,) = decode("s2c", 4, bow_appraisal())
        p = ev["profile"]
        self.assertEqual((ev["object"], ev["success"], ev["magic"]), (BOW, True, True))
        self.assertEqual((p["value"], p["burden"], p["weapon"]["speed"], p["weapon"]["skill"]), (1109, 980, 41, 2))
        self.assertEqual((p["inscription"], p["scribe"], p["heritage"], p["spells"]), ("Str II", "Magastar", "Sho", ["Strength Other II"]))
        self.assertEqual((p["difficulty"], p["spellcraft"], p["rank_limit"], p["mana"], p["max_mana"]), (3, 51, None, 139, 156))
        self.assertEqual((p["skill_limit"], p["skill_limit_type"]), (71, 2))

    def test_a_creature_is_only_marked(self):
        m = Msg(0xF7B0).u32(PLAYER).u32(7).u32(0xC9).u32(BOW).u32(0x1).i32(1).u32(0)
        (ev,) = decode("s2c", 4, m.bytes())
        self.assertEqual(ev["profile"], {"creature": True})

    def test_a_short_one_keeps_its_header(self):
        m = Msg(0xF7B0).u32(PLAYER).u32(7).u32(0xC9).u32(BOW).u32(0x8).i32(1)      # value section missing
        (ev,) = decode("s2c", 4, m.bytes())
        self.assertEqual((ev["object"], ev["success"]), (BOW, True))
        self.assertNotIn("profile", ev)


class DescribeTest(unittest.TestCase):
    def test_a_bow(self):
        (ev,) = decode("s2c", 4, bow_appraisal())
        self.assertEqual(appraisal.describe("Bloodstone Yumi", ev["profile"]),
                         "Bloodstone Yumi · 1,109p · 980bu · Bow, Speed 41 · Strength Other II · Diff 3, SC 51, "
                         "Mana 156 · Sho only, Bow 71+")

    def test_a_weapon_and_armor(self):
        knife = {"value": 10, "burden": 38, "weapon": {"damage_type": 3, "speed": 25, "skill": 4, "damage": 3, "variance": 0.75,
                                                       "offense": 1.05, "defense": 1.0}}
        self.assertEqual(appraisal.describe("Practice Knife", knife), "Practice Knife · 10p · 38bu · Dagger, 1-3 Slash/Pierce, Speed 25, +5% attack")
        coat = {"value": 800, "burden": 810, "armor_level": 120, "protection": [1.0, 0.8, 1.0, 0.4, 0.4, 0.6, 0.4]}
        self.assertEqual(appraisal.describe("Coat", coat), "Coat · 800p · 810bu · AL 120 (S1.0, P0.8, B1.0, "
                                                           "C0.4, F0.4, A0.6, L0.4)")


    def test_short_spell_names_and_the_line_without_the_name(self):
        self.assertEqual([appraisal.short_spell(s) for s in ("Impenetrability III", "Impenetrability", "Strength Self II")],
                         ["Impen III", "Impen", "Strength Self II"])
        hauberk = {"value": 5949, "burden": 2988, "armor_level": 120, "protection": [1.3, 1.0, 1.0, 0.4, 0.4, 0.6, 0.4],
                   "spells": ["Impenetrability III"], "difficulty": 105, "spellcraft": 105, "max_mana": 242, "rank_limit": 5}
        self.assertEqual(appraisal.clip_lines(hauberk, {"Impenetrability III": 100}),
                         ["120AL · s1.3, p1.0, b1.0, c0.4, f0.4, a0.6, l0.4",
                          "5,949p 2,988bu Diff 105 Mana 242 (~4h) Rank 5+",
                          "Impen III"])
        self.assertEqual(appraisal.clip_lines({"value": 5, "burden": 10}), ["5p 10bu"])

    def test_how_long_a_full_charge_lasts(self):
        hauberk = {"value": 5949, "burden": 2988, "spells": ["Impenetrability III"], "difficulty": 105, "spellcraft": 105,
                   "max_mana": 242}
        powers = {"Impenetrability III": 100, "Strength Self VI": 300, "Swift Killer V": 250}
        # power 100: 0.01 mana a second, under loot's floor of 1 a minute -> 242 minutes
        self.assertTrue(appraisal.describe("Hauberk", hauberk, powers).endswith("· Impen III · Diff 105, SC 105, Mana 242 (~4h)"))
        self.assertAlmostEqual(appraisal.mana_hours(600, ["Strength Self VI", "Swift Killer V"], powers), 600 / 0.055 / 3600)
        self.assertEqual(appraisal._duration(600 / 0.055 / 3600), "~3h")
        self.assertEqual(appraisal._duration(0.5), "~30m")
        self.assertEqual(appraisal._duration(12.4), "~12h")
        self.assertIsNone(appraisal.mana_hours(242, ["Some Unknown Spell"], powers))      # not every spell known: no guess
        self.assertTrue(appraisal.describe("Hauberk", hauberk).endswith("Mana 242"))          # (no powers given)


class CopyTest(unittest.TestCase):
    def view(self, on=True, live=True):
        tr = types.SimpleNamespace(described=None)
        return types.SimpleNamespace(tracker=tr, live=live, prefs=lambda: {"copy_appraisals": on})

    def test_once_each_live_and_only_when_on(self):
        with mock.patch("companion.clipboard.copy", return_value=True) as copy:
            v = self.view()
            Companion._copy_appraisal(None, v)                     # nothing appraised yet
            v.tracker.described = {"name": "Bow", "text": "Bow · 1p · Bow, Speed 41", "clip": ["Bow, Speed 41", "1p"]}
            Companion._copy_appraisal(None, v)
            Companion._copy_appraisal(None, v)                     # the same one: not again
            self.assertEqual([c.args[0] for c in copy.call_args_list], ["Bow, Speed 41\r\n1p"])     # (its lines, no name)
            off, replay = self.view(on=False), self.view(live=False)
            for w in (off, replay):
                w.tracker.described = {"name": "Bow", "text": "x", "clip": ["x"]}
                Companion._copy_appraisal(None, w)
            self.assertEqual(copy.call_count, 1)


if __name__ == "__main__":
    unittest.main()
