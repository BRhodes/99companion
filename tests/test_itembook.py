"""The shared item book: appraisals follow an item between characters; one that leaves a character is kept a while,
then dropped; ownership only from live play."""

import os
import tempfile
import types
import unittest

from companion.__main__ import Companion
from companion.itembook import ItemBook
from companion.store import Store

SWORD, RING = 0x80000001, 0x80000002


class BookTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "c.sqlite3")
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.db.close())
        self.book = ItemBook(self.store, keep=600)

    def test_an_item_moves_between_characters_and_is_dropped_once_gone_long_enough(self):
        self.book.appraised(SWORD, "Sword", "Sword · 10p", {"value": 10}, ts=100, holder="Zorn")
        self.book.left("Zorn", {SWORD}, now=200)                     # handed over
        self.assertEqual((self.book.rows[SWORD]["owner"], self.book.rows[SWORD]["gone"]), ("Zorn", 200))
        self.book.held("Helper", {SWORD})                           # picked up by the other one
        self.assertEqual((self.book.rows[SWORD]["owner"], self.book.rows[SWORD]["gone"]), ("Helper", None))
        self.book.left("Helper", {SWORD}, now=300)                  # sold
        self.book.prune(now=850)
        self.assertIn(SWORD, self.book.rows)                         # 550 s gone: still kept
        self.book.prune(now=950)
        self.assertNotIn(SWORD, self.book.rows)                      # 650 s: dropped
        again = Store(self.path)
        self.addCleanup(again.db.close)
        self.assertEqual(again.items(), {})                          # from the database too

    def test_appraised_where_none_holds_it_is_kept_a_while_and_a_newer_appraisal_wins(self):
        self.book.appraised(RING, "Ring", "Ring · 5p", None, ts=100)
        self.assertEqual((self.book.rows[RING]["owner"], self.book.rows[RING]["gone"]), (None, 100))
        self.book.appraised(RING, "Ring", "older", None, ts=50)
        self.assertEqual(self.book.rows[RING]["text"], "Ring · 5p")
        self.assertEqual(ItemBook(self.store).rows[RING]["text"], "Ring · 5p")   # read back from the database
        self.assertEqual(self.book.listing()[0]["keep_until"], 700)


class TrackTest(unittest.TestCase):
    def view(self, live=True):
        held = {}
        tr = types.SimpleNamespace(described=None, character="Zorn", in_world=True, held=lambda: dict(held), objects={})
        return types.SimpleNamespace(tracker=tr, live=live), held

    def test_appraised_held_left(self):
        app = types.SimpleNamespace(items=ItemBook(keep=600))
        v, held = self.view()
        held[SWORD] = "main pack"
        v.tracker.described = {"object": SWORD, "name": "Sword", "text": "Sword · 10p", "ts": 10.0}
        Companion._track_items(app, v, 10.0)
        self.assertEqual(app.items.rows[SWORD]["owner"], "Zorn")
        del held[SWORD]                                               # dropped
        Companion._track_items(app, v, 20.0)
        self.assertEqual(app.items.rows[SWORD]["gone"], 20.0)

    def test_a_replay_or_logout_leaves_nothing_gone(self):
        app = types.SimpleNamespace(items=ItemBook(keep=600))
        app.items.appraised(SWORD, "Sword", "x", None, ts=1.0, holder="Zorn")
        v, held = self.view(live=False)
        Companion._track_items(app, v, 5.0)                            # replaying: nothing held is watched
        self.assertIsNone(app.items.rows[SWORD]["gone"])
        v.live = True
        held[SWORD] = "worn"
        Companion._track_items(app, v, 6.0)
        v.tracker.in_world = False                                    # logged out: everything disappears
        held.clear()
        Companion._track_items(app, v, 7.0)
        self.assertIsNone(app.items.rows[SWORD]["gone"])


if __name__ == "__main__":
    unittest.main()
