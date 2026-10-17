"""A character's cast history: read from the database once, then kept up in memory as casts are added."""

import os
import tempfile
import unittest

from companion.store import Store


class CastHistoryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(os.path.join(tmp.name, "c.sqlite3"))
        self.addCleanup(self.store.db.close)

    def cast(self, ts, who="Me", session="s1"):
        self.store.add("cast", {"ts": ts, "character": who, "spell_id": 1, "components": ["Hyssop"]}, session, ts=ts)

    def test_read_once_then_kept_up(self):
        self.cast(1.0)
        history = self.store.casts("Me", now=10.0)
        self.assertEqual([c["ts"] for c in history], [1.0])
        self.cast(2.0)
        self.cast(3.0, who="Other")
        self.assertIs(self.store.casts("Me", now=10.0), history)                 # the same list, not a new read
        self.assertEqual([c["ts"] for c in history], [1.0, 2.0])

    def test_a_replay_isnt_counted_twice_and_an_older_one_slots_in(self):
        self.cast(5.0)
        history = self.store.casts("Me", now=10.0)
        self.cast(5.0)                                                 # the companion replaying the journal
        self.cast(3.0, session="s0")                                   # an older session's, replayed late
        self.assertEqual([c["ts"] for c in history], [3.0, 5.0])

    def test_the_last_thirty_days_kept(self):
        from companion.store import KEEP_HOURS, trim
        history = [{"ts": float(i)} for i in range(10)]
        trim(history, since=4.0)
        self.assertEqual([c["ts"] for c in history], [4.0, 5.0, 6.0, 7.0, 8.0, 9.0])
        day = 86400.0
        for ts in (0.0, 20 * day, 40 * day):
            self.cast(ts)
        self.assertEqual([c["ts"] for c in self.store.casts("Me", now=40 * day)], [20 * day, 40 * day])
        self.assertEqual(KEEP_HOURS, 720)
        # a start further back: read again, from the database, back to it
        self.assertEqual([c["ts"] for c in self.store.casts("Me", now=40 * day, since=0.0)], [0.0, 20 * day, 40 * day])


if __name__ == "__main__":
    unittest.main()
