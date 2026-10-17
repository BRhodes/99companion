"""The spell bar: read at login (the player description's PlayerModule tail), kept up from your adds and removes."""

import struct
import unittest

from companion.messages import ACT_SPELLBAR_ADD, ACT_SPELLBAR_REMOVE, OP_GAME_ACTION, decode
from companion.tracker import Tracker


def action(act, *values):
    return struct.pack("<III", OP_GAME_ACTION, 7, act) + b"".join(struct.pack("<i", v) for v in values)


class SpellbarTest(unittest.TestCase):
    def test_adds_and_removes_decode(self):
        (ev,) = decode("c2s", 4, action(ACT_SPELLBAR_ADD, 1328, 2))
        self.assertEqual(ev, {"type": "spellbar_add", "spell": 1328, "index": 2})
        (ev,) = decode("c2s", 4, action(ACT_SPELLBAR_REMOVE, 1328))
        self.assertEqual(ev, {"type": "spellbar_remove", "spell": 1328})

    def test_the_bar_follows_them_as_the_server_does(self):
        tr = Tracker({})
        tr.handle({"type": "player_description", "ints": {}, "strings": {}, "skills": {}, "spellbar": [1, 2, 3]}, 0)
        tr.handle({"type": "spellbar_add", "spell": 3, "index": 0}, 1)       # already there: moves to the slot
        self.assertEqual(tr.spellbar, [3, 1, 2])
        tr.handle({"type": "spellbar_add", "spell": 9, "index": 99}, 2)      # past the end: last
        tr.handle({"type": "spellbar_remove", "spell": 1}, 3)
        self.assertEqual(tr.spellbar, [3, 2, 9])


if __name__ == "__main__":
    unittest.main()
