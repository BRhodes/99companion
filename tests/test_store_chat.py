"""Chat history is the account's: every character on it."""

import tempfile
import unittest
from pathlib import Path

from companion.store import Store


class AccountChatTest(unittest.TestCase):
    def test_every_character_on_the_account(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(str(Path(d) / "h.sqlite3"))
            for i, (who, text) in enumerate([("Main", "hi"), ("Alt", "yo"), ("Other", "hey"), ("Main", "bye")]):
                s.add("chat", {"ts": 100.0 + i, "kind": "say", "sender": "x", "text": text, "character": who}, "S", ts=100.0 + i)
            self.assertEqual([r["text"] for r in s.chat("Main")], ["bye", "hi"])          # account not known yet: its own
            s.link("Main", "acct1")
            s.link("Alt", "acct1")
            s.link("Other", "acct2")
            self.assertEqual([r["text"] for r in s.chat("Main")], ["bye", "yo", "hi"])   # Main and Alt, newest first
            self.assertEqual([r["text"] for r in s.chat("Alt", before=103)], ["yo", "hi"])
            self.assertEqual([r["text"] for r in s.chat("Other")], ["hey"])
            self.assertEqual([r["text"] for r in s.chat("@acct1")], ["bye", "yo", "hi"])   # character select: the account
            s.close()


class LoginLineTest(unittest.TestCase):
    def test_a_line_when_a_character_enters_the_world(self):
        from companion.tracker import Tracker
        me = lambda guid, name: {"type": "object", "object": guid, "name": name, "wcid": 1}
        with tempfile.TemporaryDirectory() as d:
            s = Store(str(Path(d) / "h.sqlite3"))
            try:
                tr = Tracker({}, store=s, clock=lambda: 0.0)
                tr.reset("x")
                tr.handle({"type": "account", "account": "acct1"}, 10.0)
                tr.handle({"type": "player", "object": 0x50000001}, 11.0)
                tr.handle(me(0x50000001, "Main"), 12.0)
                tr.handle(me(0x50000001, "Main"), 13.0)                             # once per login
                logins = [c for c in tr.chat if c["kind"] == "login"]
                self.assertEqual([(c["text"], c["character"]) for c in logins], [("Logged in as Main", "Main")])
                self.assertEqual([r["text"] for r in s.chat("Main", kinds=["tell"])], ["Logged in as Main"])   # any kinds
                self.assertEqual(s.same_account("Main"), ["Main"])                  # and the account's known
                tr.handle({"type": "logoff"}, 20.0)
                out = [c for c in tr.chat if c["kind"] == "logout"]
                self.assertEqual([(c["text"], c["character"]) for c in out], [("Logged out from Main", "Main")])
                self.assertIn("Logged out from Main", [r["text"] for r in s.chat("Main", kinds=["tell"])])
                tr.handle({"type": "player", "object": 0x50000002}, 21.0)
                tr.handle(me(0x50000002, "Alt"), 22.0)
                self.assertEqual([c["text"] for c in tr.chat if c["kind"] == "login"][-1], "Logged in as Alt")
                self.assertEqual(s.same_account("Main"), ["Main", "Alt"])           # the same account, both
            finally:
                s.close()

if __name__ == "__main__":
    unittest.main()
