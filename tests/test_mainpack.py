"""Main-pack room for a purchase: the server wants a free main-pack slot per stack bought (Vendor.cpp)."""

import unittest

from companion.tracker import MAIN_PACK, Tracker

PLAYER = 0x50000001
PACK = 0x80000100


class MainPackTest(unittest.TestCase):
    def tracker(self, items, capacity=None):
        tr = Tracker({}, clock=lambda: 0.0)
        tr.reset("x")
        tr.player = PLAYER
        tr.objects[PLAYER] = {"name": "Me", "wcid": 1, "items_capacity": capacity}
        tr.objects[PACK] = {"name": "Pack", "wcid": 2, "container": PLAYER}
        tr.pack_order = [PACK]
        for k in range(items):
            tr.objects[0x80001000 + k] = {"name": "Thing", "wcid": 3, "container": PLAYER}
        tr.objects[0x80002000] = {"name": "In a pack", "wcid": 3, "container": PACK}   # side packs don't count
        return tr

    def test_free_slots(self):
        self.assertEqual(self.tracker(10).main_pack_free(), MAIN_PACK - 10)
        self.assertEqual(self.tracker(10, capacity=12).main_pack_free(), 2)

    def test_slots_and_fit(self):
        item = {"max_stack": 100}
        self.assertEqual(Tracker.slots_for(item, 250), 3)
        self.assertEqual(Tracker.slots_for({}, 3), 3)                 # no max stack known: one each
        self.assertEqual(Tracker.fit_slots(item, 250, 3), 250)
        self.assertEqual(Tracker.fit_slots(item, 250, 2), 200)        # whole stacks into what's free
        self.assertEqual(Tracker.fit_slots(item, 250, 0), 0)

    def test_vendor_plan_counts_slots(self):
        tr = self.tracker(100)                                         # 2 free
        tr.vendor = {"id": 7, "ts": 0.0, "sell_price": 1.0, "open": True,
                     "items": [{"iid": 1, "name": "Hyssop", "max_stack": 100, "amount": None, "value": 5}]}
        v = tr._vendor_snapshot([{"name": "Hyssop", "buy": 250, "carried": 0, "level": 250}], 0.0)
        self.assertEqual((v["slots"], v["free_slots"]), (3, 2))


if __name__ == "__main__":
    unittest.main()
