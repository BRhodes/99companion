"""Monster info: physical damage by height and type from body-part armor; magic from resistances."""

import struct
import unittest

from companion import monsters
from companion.messages import decode
from companion.tracker import Tracker

MATTEKAR = monsters.load()["2582"]           # Great Mattekar, from the server's data
ME, FOE = 0x50000001, 0x60022EA2


class AnalyzeTest(unittest.TestCase):
    def test_armor_factor(self):
        self.assertAlmostEqual(monsters.armor_factor(0), 1.0)
        self.assertAlmostEqual(monsters.armor_factor(190 / 3), 0.5)       # 63.3 armor halves the damage
        self.assertAlmostEqual(monsters.armor_factor(-190 / 3), 2.0)      # negative armor adds to it

    def test_great_mattekar(self):
        a = monsters.analyze(MATTEKAR)
        low = a["heights"][2]
        self.assertEqual(low["parts"][0], {"name": "Front leg", "chance": 0.8, "armor": 100})
        # low fire: 80% leg (100 x 0.7 armor vs fire) + 20% torso (120 x 0.7)
        expect = 0.8 * monsters.armor_factor(70) + 0.2 * monsters.armor_factor(84)
        self.assertAlmostEqual(low["physical"]["fire"], expect, places=3)
        self.assertEqual((a["best"]["height"], a["best"]["physical"], a["best"]["magic"]), ("Low", "fire", "fire"))
        self.assertEqual(a["magic"]["cold"], 0.25)                        # magic uses resistance only
        self.assertIn("avoid cold, lightning magic", a["advice"])
        self.assertEqual([w["part"] for w in a["weak"]], ["Torso", "Front leg", "Front leg"])

    def test_defenses_and_xp(self):
        # (Quick 135 + Coord 125) / 3 + 70, / 5 + 105; (Self 80 + Focus 60) / 10 + 79; level 18 on the curve
        self.assertEqual(monsters.defenses(MATTEKAR), {"melee": 157, "missile": 157, "magic": 93})
        self.assertEqual(monsters.kill_xp(MATTEKAR), round(7.6356 * 18 ** 1.58006))
        self.assertEqual(monsters.kill_xp(dict(MATTEKAR, xp_override=21490)), 21490)

    def test_attacks_scale_with_strength(self):
        soldier = next(v for v in monsters.load().values() if v["name"] == "Olthoi Soldier")   # Str 300, claw 40
        claw = next(a for a in monsters.attacks(soldier) if a["part"] == "Claw")
        self.assertEqual((claw["type"], claw["base"], claw["mult"]), ("pierce", 40, 3.69))
        self.assertEqual((claw["max"], claw["crit"]), (222, 443))                # 40 x 1.5 power x 3.695, crit doubled


def s16(text):
    raw = struct.pack("<H", len(text)) + text.encode()
    return raw + b"\0" * (-len(raw) % 4)


class FoeTest(unittest.TestCase):
    def setUp(self):
        self.tr = Tracker({})
        self.tr.player = ME
        self.tr.objects[FOE] = {"name": "Great Mattekar", "wcid": 2582}

    def test_an_arrow_at_it(self):
        for ev in decode("c2s", 5, struct.pack("<IIIIIf", 0xF7B1, 9, 0x0A, FOE, 3, 1.0)):
            self.tr.handle(ev, 100.0)
        foe = self.tr.snapshot()["foe"]
        self.assertEqual((foe["name"], foe["guid"]), ("Great Mattekar", FOE))

    def test_by_name_when_it_hits_you(self):
        for ev in decode("s2c", 9, struct.pack("<IIII", 0xF7B0, ME, 5, 0x1B2) + s16("Great Mattekar") + b"\0" * 8):
            self.tr.handle(ev, 100.0)
        self.assertEqual(self.tr.snapshot()["foe"]["level"], 18)

    def test_appraising_a_monster_makes_it_the_foe(self):
        self.tr.handle({"type": "appraise", "object": FOE, "aseq": 1}, 5.0)
        self.assertEqual(self.tr.snapshot()["foe"]["guid"], FOE)

    def test_players_are_not_foes(self):
        self.tr.objects[0x50000002] = {"name": "Krodunk", "wcid": 1}
        for ev in decode("c2s", 5, struct.pack("<IIIIIf", 0xF7B1, 9, 0x0A, 0x50000002, 2, 1.0)):
            self.tr.handle(ev, 100.0)
        self.assertIsNone(self.tr.foe)
        self.assertTrue(self.tr.snapshot()["foe"]["stand_in"])      # still the stand-in, not the player

    def test_a_black_rabbit_until_you_fight(self):
        foe = self.tr.snapshot()["foe"]
        self.assertEqual((foe["name"], foe["level"], foe["stand_in"]), ("Black Rabbit", 1, True))


class RecentFoesTest(unittest.TestCase):
    def test_the_last_five_minutes_of_fighting_alphabetically(self):
        tr = Tracker({}, clock=lambda: 1000.0)
        tr.player = ME
        a, b, c, d = 0x60000001, 0x60000002, 0x60000003, 0x60000004
        tr.objects.update({a: {"name": "Great Mattekar", "wcid": 2582}, b: {"name": "Great Mattekar", "wcid": 2582},
                           c: {"name": "Black Rabbit", "wcid": 19}, d: {"name": "Great Mattekar", "wcid": 2582}})
        for guid, ts in ((a, 100.0), (b, 700.0), (c, 900.0), (d, 950.0), (b, 990.0)):
            tr._set_foe(guid, ts)
        foes = tr._foes_snapshot(1000.0)
        # within 5 minutes of the latest fight (990): b, c, d; a (100) is out. Alphabetical, then by guid
        self.assertEqual([f["guid"] for f in foes], [c, b, d])
        self.assertEqual(foes[1]["xp"], monsters.kill_xp(MATTEKAR))
        # an hour later, the same three: the window is about fighting, not the clock
        self.assertEqual([f["guid"] for f in tr._foes_snapshot(4600.0)], [c, b, d])


if __name__ == "__main__":
    unittest.main()
