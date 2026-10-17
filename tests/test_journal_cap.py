"""The relay keeps the journal folder under its size cap without touching journals still in use."""

import os
import tempfile
import time
import unittest
from pathlib import Path

from companion.relay import KEEP_ENDED, Journal


class JournalCapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def old_journal(self, name, size, age):
        f = self.dir / f"session-{name}.cap"
        f.write_bytes(b"x" * size)
        t = time.time() - age
        os.utime(f, (t, t))
        return f

    def test_deletes_oldest_finished_first(self):
        oldest = self.old_journal("a", 400, age=3000)
        middle = self.old_journal("b", 400, age=2000)
        newer = self.old_journal("c", 400, age=1000)
        journal = Journal(self.dir, "x", cap_bytes=900)
        self.assertEqual(journal.enforce_cap(), [oldest])            # 1200 -> 800: one is enough
        self.assertTrue(middle.exists() and newer.exists())
        journal.close()

    def test_never_deletes_journals_in_use(self):
        self.old_journal("old", 500, age=5000)
        journal = Journal(self.dir, "x", cap_bytes=10)
        live = journal.new_session(("127.0.0.1", 9000))              # a session that's running
        os.utime(live.path, (time.time() - 9000, time.time() - 9000))  # even if its file looks oldest
        ended = journal.new_session(("127.0.0.1", 9001))
        journal.end_session(ended, "idle")                           # ended just now: still replayed
        deleted = journal.enforce_cap()
        self.assertEqual([f.name for f in deleted], ["session-old.cap"])
        self.assertTrue(Path(live.path).exists() and Path(ended.path).exists())
        # once the ended session is forgotten, its journal can go
        ended.ended -= KEEP_ENDED + 1
        journal.last_cap_check = 0
        journal.reap()
        self.assertFalse(Path(ended.path).exists())
        self.assertTrue(Path(live.path).exists())
        journal.close()

    def test_zero_cap_keeps_everything(self):
        f = self.old_journal("a", 1000, age=1000)
        journal = Journal(self.dir, "x", cap_bytes=0)
        journal.reap()
        self.assertTrue(f.exists())
        journal.close()


if __name__ == "__main__":
    unittest.main()
