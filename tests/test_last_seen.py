"""An account's tab keeps its character when it's out of the world: as last seen, marked logged out."""

import types
import unittest

from companion.__main__ import Companion


def snap(character, in_world, ended=None, chat=(), now=100.0):
    return {"now": now, "status": {"character": character, "in_world": in_world, "account": "acct", "connected": True},
            "session": {"id": "s", "ended": ended}, "chat": list(chat), "level": 19 if character else None}


class LastSeenTest(unittest.TestCase):
    def setUp(self):
        self.c = types.SimpleNamespace(last_world={})
        self.seen = lambda s: Companion._last_seen(self.c, s)

    def test_character_select_shows_the_last_character(self):
        self.seen(snap("Hero", True, now=50.0))
        out = self.seen(snap(None, False, chat=["Logged out from Hero"]))
        self.assertEqual((out["status"]["character"], out["status"]["logged_out"], out["status"]["last_seen"], out["level"]),
                         ("Hero", True, 50.0, 19))
        self.assertEqual(out["chat"], ["Logged out from Hero"])          # this session's own chat
        self.assertFalse(out["status"]["in_world"])

    def test_nothing_remembered_shows_as_it_is(self):
        s = snap(None, False)
        self.assertIs(self.seen(s), s)

    def test_an_ended_session_shows_the_last_character(self):
        self.seen(snap("Hero", True))
        out = self.seen(snap("Hero", True, ended=120.0))
        self.assertTrue(out["status"]["logged_out"])
        self.assertEqual(out["session"]["ended"], 120.0)


class SupersededTest(unittest.TestCase):
    def test_an_accounts_newest_stays(self):
        view = lambda num, account: types.SimpleNamespace(num=num, tracker=types.SimpleNamespace(account=account))
        old, new, other = view(1, "a"), view(2, "a"), view(3, "b")
        c = types.SimpleNamespace(sessions={1: old, 2: new, 3: other})
        self.assertTrue(Companion._superseded(c, old))
        self.assertFalse(Companion._superseded(c, new))
        self.assertFalse(Companion._superseded(c, other))
        self.assertTrue(Companion._superseded(c, view(4, None)))


if __name__ == "__main__":
    unittest.main()
