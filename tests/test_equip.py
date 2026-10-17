"""Putting an item on (game event 0x23) makes it worn; moving it into a pack takes it off."""

import struct
import unittest

from companion.messages import decode
from companion.tracker import Tracker

ME, GIRTH, PACK = 0x500000C9, 0x90053DB4, 0x90054A89


class EquipTest(unittest.TestCase):
    def test_on_and_off(self):
        tr = Tracker({})
        tr.player = ME
        tr.objects[GIRTH] = {"name": "Chainmail Girth", "wcid": 415, "stack": 1, "container": ME}
        for ev in decode("s2c", 9, struct.pack("<IIIIII", 0xF7B0, ME, 7, 0x23, GIRTH, 0x400)):
            tr.handle(ev, 100.0)
        girth = next(t for t in tr._targets() if t["id"] == GIRTH)
        self.assertEqual((girth["worn"], girth["slot"]), (True, "armor"))
        for ev in decode("s2c", 9, struct.pack("<IIIIIII", 0xF7B0, ME, 8, 0x22, GIRTH, PACK, 0)):
            tr.handle(ev, 101.0)
        girth = next((t for t in tr._targets() if t["id"] == GIRTH), None)
        self.assertFalse(girth and girth["worn"])


if __name__ == "__main__":
    unittest.main()


class GoneTest(unittest.TestCase):
    def test_the_server_saying_an_item_is_gone_forgets_it(self):
        tr = Tracker({})
        tr.player = ME
        tr.objects[GIRTH] = {"name": "Pyreal", "wcid": 273, "stack": 476, "container": ME}
        tr.order[ME] = [GIRTH]
        for ev in decode("s2c", 9, struct.pack("<IIIIII", 0xF7B0, ME, 7, 0xA0, GIRTH, 0x37)):
            tr.handle(ev, 100.0)
        self.assertNotIn(GIRTH, tr.objects)
        self.assertEqual(tr.order[ME], [])
