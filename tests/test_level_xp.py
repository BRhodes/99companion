"""The XP bar: the level table from portal.dat, and how far into its level a character is."""

import os
import unittest

from companion.dat import DatFile, load_level_xp
from companion.tracker import Tracker
from tests.test_spellbook import PORTAL


class LevelXpTest(unittest.TestCase):
    @unittest.skipUnless(os.path.exists(PORTAL), "needs the AC install's portal.dat")
    def test_the_games_table(self):
        table = load_level_xp(DatFile(PORTAL))
        self.assertEqual(table[:4], [0, 0, 1000, 2777])
        self.assertEqual(len(table), 127)
        self.assertEqual(table, sorted(table))

    def test_progress(self):
        tr = Tracker({})
        tr.level_xp = [0, 0, 1000, 2777, 5697]
        self.assertEqual(tr._level_progress(), {})                      # nothing known yet
        tr.level, tr.xp_total = 3, 4000
        self.assertEqual(tr._level_progress(), {"level": 3, "level_from": 2777, "level_to": 5697})
        tr.level = 4
        self.assertEqual(tr._level_progress()["level_to"], None)        # the last level


if __name__ == "__main__":
    unittest.main()
