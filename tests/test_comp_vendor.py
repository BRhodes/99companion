"""The last component vendor used is remembered: the shop's prices and wares on the next login, and past other vendors."""

import tempfile
import unittest
from pathlib import Path

from companion.store import Store
from companion.tracker import Tracker


def vendor_ev(vid, rate, items):
    return {"type": "vendor", "vendor": vid, "buy_price": 0.9, "sell_price": rate, "items": items}


HYSSOP = {"iid": 11, "name": "Hyssop", "value": 7, "amount": None, "max_stack": 50, "item_type": 0x1000}
SWORD = {"iid": 12, "name": "Long Sword", "value": 300, "amount": None, "max_stack": 1, "item_type": 0x1}


class CompVendorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "s.db")
        self.addCleanup(self.store.close)

    def tracker(self, who):
        tr = Tracker(clock=lambda: 1000.0)
        tr.character = who
        tr.components = {1: {"name": "Hyssop"}}
        tr.objects = {77: {"name": "Shopkeep"}, 78: {"name": "Smith"}}
        tr.remember_vendor, tr.recall_vendor = self.store.save_comp_vendor, self.store.comp_vendor
        return tr

    def test_kept_past_another_vendor_and_the_next_login(self):
        tr = self.tracker("Zorn")
        tr._on_vendor(vendor_ev(77, 1.55, [HYSSOP]), 900.0)
        tr._on_vendor(vendor_ev(78, 1.3, [SWORD]), 950.0)      # a weaponsmith after: the components' prices stay
        w = tr._wares()
        self.assertEqual((w["vendor"], w["items"][0]["unit"]), ("Shopkeep", 7 * 1.55))
        later = self.tracker("Zorn")                          # the next login: nothing seen yet
        self.assertEqual(later._wares()["items"][0]["unit"], 7 * 1.55)
        self.assertFalse(later._wares()["open"])
        other = self.tracker("Brightblade")                   # never used one: the latest any of yours did
        self.assertEqual(other._wares()["vendor"], "Shopkeep")

    def test_an_older_one_does_not_replace_a_newer(self):
        tr = self.tracker("Zorn")
        tr._on_vendor(vendor_ev(77, 1.1, [HYSSOP]), 2000.0)
        tr._on_vendor(vendor_ev(77, 1.8, [HYSSOP]), 1500.0)   # (a journal replayed: an earlier visit)
        self.assertEqual(self.store.comp_vendor("Zorn")["sell_price"], 1.1)


if __name__ == "__main__":
    unittest.main()
