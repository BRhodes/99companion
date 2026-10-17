"""Vendor stock decoding, buy-for-N-hours plans and the buy message."""

import struct
import unittest

from companion.messages import decode
from companion.tracker import Tracker
from tests.wire import Msg

PLAYER, VENDOR = 0x500000C9, 0x77F8C003


def ware(m, iid, name, wcid, value, amount=0xFFFFFF):
    """ItemProfile with an OldPublicWeenieDesc: value (0x8), stack (0x1000), max stack (0x2000)."""
    m.u32(0xFF000000 | amount).u32(iid)
    m.u32(0x8 | 0x1000 | 0x2000).string(name).u16(wcid).u16(0).u32(0x800).u32(0)
    m.u32(value).u16(1).u16(50).align()
    return m


def vendor_info(*wares):
    m = Msg(0xF7B0).u32(PLAYER).u32(3).u32(0x62).u32(VENDOR)
    m.u32(0).i32(0).i32(0).i32(0).b += struct.pack("<ff", 0.9, 1.35)
    m.u32(len(wares))
    for w in wares:
        ware(m, *w)
    return m


class VendorTest(unittest.TestCase):
    def test_base_price_from_the_zaikhal_archmage(self):
        from companion.tracker import BASELINE
        name, value = next(iter(BASELINE["values"].items()))
        self.assertEqual(Tracker.base_price(name), Tracker.vendor_price({"value": value}, BASELINE["sell_price"], 1))
        self.assertIsNone(Tracker.base_price("No Such Thing"))

    def test_wares_in_the_vendors_order_components_only(self):
        tr = Tracker({})
        tr.components = {1: {"name": "Violet Taper", "category": 5}, 2: {"name": "Mugwort", "category": 1}}
        self.assertIsNone(tr._wares())
        tr.comp_vendor = {"id": VENDOR, "ts": 0.0, "sell_price": 1.35, "items": [
            {"iid": 1, "name": "Violet Taper", "value": 25, "amount": 7}, {"iid": 2, "name": "Wand", "value": 99},
            {"iid": 3, "name": "Mugwort", "value": 10, "amount": None}]}
        w = tr._wares()
        self.assertFalse(w["open"])
        self.assertEqual([(i["name"], i["category"], i["price"], i["amount"]) for i in w["items"]],
                         [("Violet Taper", "Tapers", 34, 7), ("Mugwort", "Herbs", 14, None)])

    def test_decode(self):
        (ev,) = decode("s2c", 4, vendor_info((0x9001, "Mugwort", 776, 10), (0x9002, "Violet Taper", 0, 25, 7)).bytes())
        self.assertEqual((ev["vendor"], round(ev["sell_price"], 2), len(ev["items"])), (VENDOR, 1.35, 2))
        self.assertEqual({k: ev["items"][0][k] for k in ("iid", "name", "value", "amount", "max_stack")},
                         {"iid": 0x9001, "name": "Mugwort", "value": 10, "amount": None, "max_stack": 50})
        self.assertEqual(ev["items"][1]["amount"], 7)

    def test_price_matches_server(self):
        item = {"value": 10}
        self.assertEqual(Tracker.vendor_price(item, 1.35, 15), 203)   # ceil(202.5 - 0.1)
        self.assertEqual(Tracker.vendor_price({"value": 0}, 1.35, 3), 1)

    def test_plan_and_message(self):
        tr = Tracker({}, clock=lambda: 1000.0)
        tr.reset("x")
        for ev in decode("s2c", 4, vendor_info((0x9001, "Mugwort", 776, 10), (0x9002, "Violet Taper", 0, 25, 7)).bytes()):
            tr.handle(ev, 990.0)
        comps = [{"name": "Mugwort", "buy": 15}, {"name": "Violet Taper", "buy": 16}, {"name": "Gypsum", "buy": 4},
                 {"name": "Hyssop", "buy": 0}]
        snap = tr._vendor_snapshot(comps, 1000.0)
        self.assertEqual([(p["name"], p["amount"], p["cost"]) for p in snap["plan"]],
                         [("Mugwort", 15, 203), ("Violet Taper", 7, 237)])    # capped at the vendor's 7
        self.assertEqual(snap["missing"], ["Gypsum"])
        tr.handle({"type": "position", "object": None, "source": "client", "cell": 0xA9B4001F, "x": 87.75,
                   "y": 147.72, "z": 66, "qw": 1, "qz": 0}, 1001.0)
        self.assertIsNone(tr._vendor_snapshot(comps, 1001.0))                 # moved, vendor's spot unknown: closed


class WindowTest(unittest.TestCase):
    """A vendor's window: open from its stock list until you move further from it."""
    def setUp(self):
        self.tr = Tracker({}, clock=lambda: 0.0)
        self.tr.reset("x")
        self.tr.player = 0x50000001
        self.tr.objects[VENDOR] = {"name": "Vendor", "wcid": 1, "pos": {"cell": 0xA9B4001F, "x": 90.0, "y": 150.0, "z": 66.0}}
        self.at(87.0, 150.0)
        for ev in decode("s2c", 4, vendor_info((0x9001, "Mugwort", 776, 10)).bytes()):
            self.tr.handle(ev, 1.0)

    def at(self, x, y, ts=2.0):
        self.tr.handle({"type": "position", "object": None, "source": "client", "cell": 0xA9B4001F, "x": x, "y": y,
                        "z": 66.0, "qw": 1, "qz": 0}, ts)

    def test_open_while_standing_or_closer(self):
        self.assertTrue(self.tr.vendor_open(VENDOR))
        self.at(87.0, 150.0)
        self.at(88.5, 150.0)                                    # closer
        self.assertTrue(self.tr.vendor_open(VENDOR))

    def test_any_step_away_closes_it(self):
        self.at(88.5, 150.0)                                    # closer first: away from here counts
        self.at(88.0, 150.0)
        self.assertFalse(self.tr.vendor_open())
        self.assertEqual(self.tr.vendor["closed"], "you moved away from the vendor")
        for ev in decode("s2c", 4, vendor_info((0x9001, "Mugwort", 776, 10)).bytes()):
            self.tr.handle(ev, 3.0)                             # used again: open again
        self.assertTrue(self.tr.vendor_open(VENDOR))


if __name__ == "__main__":
    unittest.main()
