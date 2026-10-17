"""Your corpses: seen created, kept (per character, in the store) till they rot -- 5 minutes a level, two at most."""

import tempfile
import unittest
from pathlib import Path

from companion.store import Store
from companion.tracker import Tracker


def corpse(guid, name, cell=0xA8BC0018, x=100.0, y=50.0):
    return {"type": "object", "object": guid, "name": name, "wcid": 21, "item_type": 512,
            "position": {"cell": cell, "x": x, "y": y, "z": 0.0}}


class CorpseTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / "s.db")
        self.addCleanup(self.store.close)

    def tracker(self):
        tr = Tracker({})
        tr.character, tr.level = "Brightblade", 8
        tr.remember_corpse, tr.recall_corpses = self.store.save_corpse, self.store.corpses
        return tr

    def test_yours_kept_till_they_rot(self):
        tr = self.tracker()
        tr._on_object(corpse(1, "Corpse of Banderling Scout"), 1000.0)      # not yours
        tr._on_object(corpse(2, "Corpse of Brightblade"), 1000.0)
        (c,) = tr.corpse_list(1000.0)
        self.assertEqual((c["id"], c["expires"], round(c["ns"], 2), round(c["ew"], 2)), (2, 1000.0 + 8 * 300, 48.66, 32.87))
        tr._on_object(corpse(2, "Corpse of Brightblade"), 1500.0)           # seen again: its death stands
        self.assertEqual(tr.corpse_list(1500.0)[0]["died"], 1000.0)
        later = self.tracker()                                               # the next login (or a restart)
        self.assertEqual([c["id"] for c in later.corpse_list(3000.0)], [2])
        self.assertEqual(later.corpse_list(1000.0 + 8 * 300), [])            # rotted

    def test_two_at_most(self):
        tr = self.tracker()
        for guid, t in ((5, 100.0), (6, 200.0), (7, 300.0)):
            tr._on_object(corpse(guid, "Corpse of Brightblade"), t)
        self.assertEqual([c["id"] for c in tr.corpse_list(300.0)], [7, 6])  # a third: the oldest rots

    def test_in_a_dungeon_no_map_spot(self):
        tr = self.tracker()
        tr._on_object(corpse(9, "Corpse of Brightblade", cell=0x01D20123), 10.0)
        self.assertIsNone(tr.corpse_list(10.0)[0]["ns"])


if __name__ == "__main__":
    unittest.main()
