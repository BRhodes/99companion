"""Banes in the Expiring soon list: one entry per item, at the estimated expiry, recast on that item."""

import unittest
import unittest.mock

from companion.tracker import Tracker

SPELLS = {1325: {"name": "Acid Bane III", "duration": 1800.0, "school": 3},
          1614: {"name": "Blood Drinker IV", "duration": 1800.0, "school": 3}}
ARMOR, SHIELD, SWORD = 0x80000001, 0x80000002, 0x80000003


def piece(target, key, spell, sid, expires, item, value=1.0):
    return {"key": key, "spell": spell, "spell_id": sid, "value": value, "started": expires - 1800, "expires": expires,
            "item": item, "target": target, "slot": "armor"}


class BaneBuffsTest(unittest.TestCase):
    def test_one_row_per_item(self):
        tr = Tracker(SPELLS, clock=lambda: 1000.0)
        tr.reset("x")
        tr.known_spells = {1325: 1.0}
        tr.item_enchants = {(ARMOR, 18): piece(ARMOR, 18, "Acid Bane III", 1325, 1300.0, "Chainmail Hauberk"),
                            (SHIELD, 18): piece(SHIELD, 18, "Acid Bane III", None, 1500.0, "Kite Shield"),   # no id kept
                            (SWORD, 44): piece(SWORD, 44, "Blood Drinker IV", 1614, 1200.0, "Sword"),       # not a bane
                            (ARMOR, 13): piece(ARMOR, 13, "Blade Bane I", 1, 900.0, "Chainmail Hauberk")}    # ran out
        banes = [b for b in tr._buffs(1000.0) if b.get("estimate")]
        self.assertEqual([(b["name"], b["target"], b["recast"], b["expires"]) for b in banes],
                         [("Acid Bane III (Chainmail Hauberk)", ARMOR, 1325, 1300.0),
                          ("Acid Bane III (Kite Shield)", SHIELD, 1325, 1500.0)])


if __name__ == "__main__":
    unittest.main()


class VersionsTest(unittest.TestCase):
    def test_one_version_running_out_leaves_the_other(self):
        spells = {1324: {"name": "Acid Bane II", "duration": 600.0, "school": 3},
                  1325: {"name": "Acid Bane III", "duration": 1800.0, "school": 3}}
        clock = {"t": 1000.0}
        tr = Tracker(spells, clock=lambda: clock["t"])
        tr.reset("x")
        tr.player = 0x50000001
        tr.objects[ARMOR] = {"name": "Chainmail Hauberk", "wcid": 1, "location": 0x200}
        from companion import tracker as T
        mods = {1324: [0x8008, 18, 1.2], 1325: [0x8008, 18, 1.3]}
        with unittest.mock.patch.dict(T.SPELL_MODS, mods):
            tr._note_item_spell({"target": ARMOR, "spell_id": 1325}, 1000.0)      # III, until 2800
            tr._note_item_spell({"target": ARMOR, "spell_id": 1324}, 2500.0)      # II later, until 3100
        entry = tr.item_enchants[(ARMOR, 18)]
        tr._item_effective(entry, 2600.0)
        self.assertEqual((entry["spell"], entry["expires"]), ("Acid Bane III", 3100.0))   # the stronger shows; on until II ends
        tr._item_effective(entry, 2900.0)                                    # III ran out: II still on
        self.assertEqual((entry["spell"], entry["expires"]), ("Acid Bane II", 3100.0))
