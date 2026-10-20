"""Chat recap: speech and tells are logged, spell words aren't, and lines go to the database."""

import struct
import tempfile
import unittest
from pathlib import Path

from companion.messages import decode
from companion.store import Store
from companion.tracker import Tracker

ME, FRIEND, NPC = 0x500000C9, 0x50000014, 0x77F90003
COMPONENTS = {1: {"name": "Lead Scarab", "word": "Malar"}, 2: {"name": "Hyssop", "word": "Ea"},
              3: {"name": "Gypsum", "word": "Sith"}, 4: {"name": "Mandrake", "word": "Equin"}}


def s16(text):
    raw = struct.pack("<H", len(text)) + text.encode("latin-1")
    return raw + b"\0" * (-len(raw) % 4)


class ChatTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.dir.name) / "db.sqlite3"))
        self.tr = Tracker({}, store=self.store, components=COMPONENTS)
        self.tr.player, self.tr.character, self.tr.session = ME, "Zorn", "s1"

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def feed(self, data, ts=100.0):
        for ev in decode("s2c", 9, data):
            self.tr.handle(ev, ts)

    def test_decodes_speech_and_tells(self):
        self.feed(struct.pack("<I", 0x37) + s16("hello there") + s16("Krodunk") + struct.pack("<I", FRIEND))
        self.feed(struct.pack("<III", 0xF7B0, ME, 5) + struct.pack("<I", 0x38) + s16("psst") + s16("Krodunk")
                  + struct.pack("<II", FRIEND, ME), ts=101.0)
        self.feed(struct.pack("<I", 0xF62C) + s16('You tell Krodunk, "hi back"') + struct.pack("<i", 4), ts=102.0)
        got = [(c["kind"], c["sender"], c["text"], c.get("to")) for c in self.tr.chat]
        self.assertEqual(got, [("say", "Krodunk", "hello there", None), ("tell", "Krodunk", "psst", None),
                               ("tell_out", "You", "hi back", "Krodunk")])

    def test_fellowship_chat_both_ways(self):
        self.feed(struct.pack("<II", 0x14A, 0x800) + s16("Krodunk") + s16("13 creature"))
        for ev in decode("c2s", 5, struct.pack("<III", 0xF7B1, 7, 0x147) + struct.pack("<I", 0x800) + s16("what you need")):
            self.tr.handle(ev, 101.0)
        got = [(c["kind"], c["channel"], c["sender"], c["text"], c["me"]) for c in self.tr.chat]
        self.assertEqual(got, [("channel", "Fellowship", "Krodunk", "13 creature", False),
                               ("channel", "Fellowship", "Zorn", "what you need", True)])

    def test_global_channels(self):
        # the server's channels come as text on channel 8, already "[General] Name: ..." (its length counting a NUL)
        line = "[General] Mira: this is wiggity whack\0"
        self.feed(struct.pack("<I", 0xF62C) + s16(line) + struct.pack("<i", 8))
        self.feed(struct.pack("<I", 0xF62C) + s16("[Trade] Zorn: wts pyreals") + struct.pack("<i", 8), ts=101.0)
        got = [(c["kind"], c["channel"], c["sender"], c["text"], c["me"]) for c in self.tr.chat]
        self.assertEqual(got, [("channel", "General", "Mira", "this is wiggity whack", False),
                               ("channel", "Trade", "Zorn", "wts pyreals", True)])

    def test_spell_words_and_npcs(self):
        self.feed(struct.pack("<I", 0x37) + s16("Malar Easith") + s16("Zorn") + struct.pack("<I", ME))
        self.feed(struct.pack("<I", 0x37) + s16("Equin Eamalar") + s16("Krodunk") + struct.pack("<I", FRIEND))
        self.assertEqual(list(self.tr.chat), [])                      # incantations aren't chat
        self.feed(struct.pack("<III", 0xF7B0, ME, 5) + struct.pack("<I", 0x38) + s16("An excellent purchase.")
                  + s16("Asaina") + struct.pack("<II", NPC, ME))
        self.assertEqual(self.tr.chat[-1]["kind"], "npc")            # a vendor's "tell" is NPC chatter

    def test_lines_are_kept_apart_and_saved(self):
        for text in ("one", "two", "three"):                          # one packet, same timestamp
            self.feed(struct.pack("<III", 0xF7B0, ME, 5) + struct.pack("<I", 0x38) + s16(text) + s16("Krodunk")
                      + struct.pack("<II", FRIEND, ME))
        self.assertEqual([c["text"] for c in self.store.chat("Zorn")], ["three", "two", "one"])
        self.assertEqual(self.store.chat("Zorn Alt"), [])


if __name__ == "__main__":
    unittest.main()
