"""Combat stance: from your requests (0x53), the server's combat-mode int, and its refusals."""

import struct
import unittest

from companion.messages import decode
from companion.tracker import Tracker

ME, WAND = 0x50000001, 0x80000010


def request(tr, mode, ts):
    for ev in decode("c2s", 5, struct.pack("<IIII", 0xF7B1, 9, 0x53, mode)):
        tr.handle(ev, ts)


class StanceTest(unittest.TestCase):
    def tracker(self):
        tr = Tracker({})
        tr.player = ME
        tr.objects[WAND] = {"name": "Aluvian Wand", "wcid": 1, "item_type": 0x8000, "wielder": ME}
        return tr

    def test_magic_with_a_wand_is_ready(self):
        tr = self.tracker()
        request(tr, 4, 100.0)
        self.assertEqual(tr.stance_snapshot(), {"mode": 4, "name": "magic", "caster": "Aluvian Wand", "ready": True})
        request(tr, 1, 110.0)
        self.assertFalse(tr.stance_snapshot()["ready"])

    def test_no_wand_not_ready(self):
        tr = self.tracker()
        tr.objects[WAND]["wielder"] = None
        request(tr, 4, 100.0)
        self.assertFalse(tr.stance_snapshot()["ready"])

    def test_a_refused_change_goes_back(self):
        tr = self.tracker()
        request(tr, 1, 100.0)
        request(tr, 4, 200.0)
        tr.handle({"type": "error", "code": 0x36}, 200.5)          # WERROR_INTERRUPTED: not changed
        self.assertEqual(tr.stance, 1)

    def test_the_server_sets_it(self):
        tr = self.tracker()
        tr.handle({"type": "int", "object": ME, "key": 0x28, "value": 4}, 100.0)
        self.assertTrue(tr.stance_snapshot()["ready"])


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
