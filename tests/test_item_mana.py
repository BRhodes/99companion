"""The mana of the items you wear (itemmana.py): read from an appraisal or a mana update, estimated as it drains while
its spells are on (1 on putting it on, then its spells' power / 10,000 a second), stopped when it's taken off or you
log out, nothing when putting it on failed, 0 when it ran out. The messages as the server sends them (a capture's)."""

import unittest

from companion import itemmana
from companion.tracker import MAGIC_CHANNEL, Tracker

SPELLS = {1431: {"name": "Focus Other V", "power": 200}, 2: {"name": "Endurance Other VI", "power": 250}}
PLAYER, PACK = 0x50000001, 0x50000001
STAFF, NECKLACE = 0x80000001, 0x80000002
HELD = 16777216          # the wielded-in-hand slot (a staff)
FOCUS_ON_YOU = {"type": "enchantments", "list": [{"id": 1639831, "spell": 1431, "layer": 25, "category": 9, "power": 200,
                "start": 0.0, "duration": -1.0, "caster": STAFF, "stat": {"type": 36865, "key": 5, "value": 30.0}}]}


def tracker(t0=1000.0, store=None):
    clock = {"t": t0}
    tr = Tracker(SPELLS, clock=lambda: clock["t"])
    tr.reset("x")
    tr.player = PLAYER
    if store is not None:
        tr.remember_mana = lambda rec: store.__setitem__(rec["guid"], dict(rec))
        tr.recall_mana = lambda g: dict(store[g]) if g in store else None
    tr.clock_at = clock
    return tr


def obj(tr, guid, name, now, **extra):
    tr.handle({"type": "object", "object": guid, "name": name, "wcid": 1, "item_type": 0x8000, **extra}, now)


def appraise(tr, guid, mana, max_mana, spells, now):
    tr.handle({"type": "appraisal", "object": guid, "success": True, "magic": True,
               "profile": {"value": 2525, "burden": 50, "mana": mana, "max_mana": max_mana, "spells": spells}}, now)


def equip(tr, guid, now):
    tr.handle({"type": "moved", "object": guid, "container": PLAYER, "slot": 0, "pack": False}, now)
    tr.handle({"type": "equipped", "object": guid, "location": HELD}, now)
    if guid == STAFF:
        tr.handle(FOCUS_ON_YOU, now)                  # (its spell on you, straight after)


def row(tr, now, guid=STAFF):
    tr.clock_at["t"] = now
    return next(r for r in tr.snapshot()["item_mana"] if r["guid"] == guid)


class ManaStoneTest(unittest.TestCase):
    """A mana stone's top-up: kept on the item (given, gifts), its new mana read from the update that comes with it."""

    def give(self, tr, amount, item, now, stone="Greater Mana Stone"):
        tr.handle({"type": "text", "channel": 0, "text": f"The {stone} gives {amount:,} points of mana to the {item}."}, now)

    def test_stone_tops_up_and_is_counted(self):
        tr = tracker()
        obj(tr, STAFF, "Black Garnet Staff", 1000.0, container=PACK)
        equip(tr, STAFF, 1000.0)
        appraise(tr, STAFF, 299, 404, ["Focus Other V"], 1010.0)
        self.give(tr, 104, "Black Garnet Staff", 2000.0)
        tr.handle({"type": "int", "object": STAFF, "key": 107, "value": 404}, 2000.0)     # (its new mana)
        r = row(tr, 2000.0)
        self.assertEqual((r["mana"], r["source"], r["given"]), (404, "mana stone", 104))
        self.assertEqual([(g["amount"], g["from"]) for g in r["gifts"]], [(104, "Greater Mana Stone")])
        self.give(tr, 1203, "Black Garnet Staff", 3000.0, stone="Lesser Mana Stone")
        r = row(tr, 3000.0)
        self.assertEqual((r["given"], len(r["gifts"])), (1307, 2))
        self.assertEqual(row(tr, 3500.0)["mana"], 404 - 30)   # (its 404 read at 2000, then 1 every 50 s since)

    def test_an_appraisal_says_so(self):
        tr = tracker()
        obj(tr, STAFF, "Staff", 1000.0, container=PACK)
        appraise(tr, STAFF, 299, 404, ["Focus Other V"], 1000.0)
        tr.clock_at["t"] = 1000.0
        rec = tr.item_mana.items[STAFF]
        self.assertEqual(rec["source"], "appraisal")

    def test_a_stone_for_an_item_in_your_pack(self):
        tr = tracker()
        obj(tr, NECKLACE, "Bronze Heavy Necklace", 1000.0, container=PACK)
        self.give(tr, 203, "Bronze Heavy Necklace", 1000.0)
        self.assertEqual(tr.item_mana.items[NECKLACE]["given"], 203)


class ItemManaTest(unittest.TestCase):
    def test_appraisal_sets_its_mana_and_it_drains(self):
        tr = tracker()
        obj(tr, STAFF, "Staff", 1000.0, container=PACK)
        equip(tr, STAFF, 1000.0)
        appraise(tr, STAFF, 299, 404, ["Focus Other V"], 1010.0)
        r = row(tr, 1010.0)
        self.assertEqual((r["mana"], r["max"], r["rate"], r["active"]), (299, 404, 0.02, True))
        self.assertAlmostEqual(r["left"], 299 / 0.02)
        self.assertEqual(row(tr, 1510.0)["mana"], 289)            # 1 every 50 s

    def test_putting_it_on_costs_one(self):
        tr = tracker()
        obj(tr, STAFF, "Staff", 1000.0, container=PACK)
        appraise(tr, STAFF, 301, 404, ["Focus Other V"], 1000.0)   # in the pack: nothing drains
        equip(tr, STAFF, 1100.0)
        self.assertEqual(row(tr, 1100.0)["mana"], 300)

    def test_an_activation_error_means_no_magic_and_no_drain(self):
        tr = tracker()
        obj(tr, NECKLACE, "Necklace", 1000.0, container=PACK)
        appraise(tr, NECKLACE, 369, 486, ["Endurance Other VI"], 1000.0)
        equip(tr, NECKLACE, 1100.0)
        tr.handle({"type": "error", "code": 1223}, 1100.0)        # your Arcane Lore is too low
        r = row(tr, 5000.0, NECKLACE)
        self.assertEqual((r["mana"], r["active"], r["error"], r["error_text"]), (369, False, 1223, "Arcane Lore too low"))

    def test_out_of_mana_zeroes_it(self):
        tr = tracker()
        obj(tr, STAFF, "Staff", 1000.0, container=PACK)
        equip(tr, STAFF, 1000.0)
        appraise(tr, STAFF, 299, 404, ["Focus Other V"], 1000.0)
        tr.handle({"type": "text", "text": "Your Staff is out of Mana.", "channel": MAGIC_CHANNEL, "sender": None}, 2000.0)
        r = row(tr, 2100.0)
        self.assertEqual((r["mana"], r["out"], r["left"]), (0, True, 0))

    def test_taking_it_off_stops_the_clock(self):
        tr = tracker()
        obj(tr, STAFF, "Staff", 1000.0, container=PACK)
        equip(tr, STAFF, 1000.0)
        appraise(tr, STAFF, 299, 404, ["Focus Other V"], 1000.0)
        tr.handle({"type": "moved", "object": STAFF, "container": PACK, "slot": 0, "pack": False}, 1500.0)
        rec = tr.item_mana.items[STAFF]
        self.assertEqual(itemmana.estimate(rec, tr._spell_powers(), 9000.0), 289)   # 500 s worn, then nothing

    def test_a_mana_update_is_a_reading(self):
        tr = tracker()
        obj(tr, STAFF, "Staff", 1000.0, container=PACK)
        equip(tr, STAFF, 1000.0)
        appraise(tr, STAFF, 299, 404, ["Focus Other V"], 1000.0)
        tr.handle({"type": "int", "object": STAFF, "key": 107, "value": 404}, 1200.0)   # a mana stone
        self.assertEqual((row(tr, 1200.0)["mana"], row(tr, 1700.0)["mana"]), (404, 394))

    def test_kept_across_a_logout_and_worn_at_the_next_login(self):
        store = {}
        tr = tracker(store=store)
        obj(tr, STAFF, "Staff", 1000.0, container=PACK)
        equip(tr, STAFF, 1000.0)
        appraise(tr, STAFF, 299, 404, ["Focus Other V"], 1000.0)
        tr.clock_at["t"] = 1500.0
        tr.reset_character()                                      # logged out after 500 s
        tr2 = tracker(t0=90000.0, store=store)
        tr2.handle(FOCUS_ON_YOU, 90000.0)                         # (the login's enchantments, then what you wear)
        obj(tr2, STAFF, "Staff", 90000.0, wielder=PLAYER, location=HELD)   # worn as you come in: no equip cost
        self.assertEqual((row(tr2, 90000.0)["mana"], row(tr2, 90500.0)["mana"]), (289, 279))

    def test_worn_but_none_of_its_spells_on_you_is_off(self):
        tr = tracker()
        obj(tr, STAFF, "Staff", 1000.0, wielder=PLAYER, location=HELD)    # worn at login, nothing of it on you
        appraise(tr, STAFF, 299, 404, ["Focus Other V"], 1100.0)
        r = row(tr, 5000.0)
        self.assertEqual((r["mana"], r["active"]), (299, False))
        tr.handle(FOCUS_ON_YOU, 5000.0)                           # put on again, and on it comes
        self.assertEqual((row(tr, 5000.0)["active"], row(tr, 5500.0)["mana"]), (True, 289))

    def test_never_appraised_shows_without_mana(self):
        tr = tracker()
        obj(tr, STAFF, "Staff", 1000.0, container=PACK)
        equip(tr, STAFF, 1000.0)
        r = row(tr, 1100.0)
        self.assertEqual((r["mana"], r["rate"], r["left"], r["active"]), (None, None, None, True))


if __name__ == "__main__":
    unittest.main()
