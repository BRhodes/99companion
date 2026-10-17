"""A restart restores a session from its checkpoint and skips the records it already holds."""

import struct
import tempfile
import unittest

from companion.__main__ import SessionView
from companion.checkpoint import Checkpoints

SPELLS = {1: {"name": "Strength Self I", "school": 4, "power": 1}}
INFO = {"id": "20261010-000000-1", "client": ["127.0.0.1", 9000], "server": "x"}


def cast(aseq, spell):
    return struct.pack("<IIII", 0xF7B1, aseq, 0x48, spell)     # an untargeted cast, as the client sends it


def packet(seq, message):
    """A client datagram carrying one message: header, then one fragment (framing.py)."""
    frag = struct.pack("<IIHHHH", seq, 0, 1, 16 + len(message), 0, 5) + message
    return struct.pack("<IIIIHBB", seq, 0x800, 0, 0, len(frag), 0, 0) + frag


def stream(n):
    return [(1000.0 + i, "c2s", 0, packet(i + 1, cast(i + 1, 1))) for i in range(n)]


def view():
    return SessionView(1, INFO, SPELLS, None, {}, lambda: None)


class CheckpointTest(unittest.TestCase):
    def test_restore_and_skip(self):
        records = stream(40)
        whole = view()
        for rec in records:
            whole.feed(*rec)
        with tempfile.TemporaryDirectory() as d:
            store = Checkpoints(d)
            first = view()
            for rec in records[:25]:
                first.feed(*rec)
            first.tracker.book_burden = lambda g: None         # (what the companion hands a tracker)
            store.save(first)
            again = view()                                     # the restarted companion
            self.assertTrue(store.restore(again))
            self.assertEqual((again.skip, again.seen), (25, 0))
            fed = []
            again.tracker.handle = (lambda orig: lambda ev, ts: (fed.append(ts), orig(ev, ts)))(again.tracker.handle)
            for rec in records:                                # the relay streams the journal from the start
                again.feed(*rec)
        self.assertEqual(fed, [r[0] for r in records[25:]])    # only what came after the checkpoint was decoded
        self.assertEqual(again.seen, 40)
        self.assertEqual(len(again.tracker.casts), len(whole.tracker.casts))
        self.assertEqual(again.tracker.last_aseq, whole.tracker.last_aseq)
        self.assertIs(again.tracker.ledger.model, again.tracker.burn)   # shared parts are the new process's

    def test_no_checkpoint_or_wrong_version(self):
        with tempfile.TemporaryDirectory() as d:
            store = Checkpoints(d)
            self.assertFalse(store.restore(view()))
            v = view()
            store.save(v)
            import companion.checkpoint as ck
            saved, ck.VERSION = ck.VERSION, ck.VERSION + 1
            try:
                self.assertFalse(store.restore(view()))
            finally:
                ck.VERSION = saved


if __name__ == "__main__":
    unittest.main()
