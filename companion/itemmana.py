"""How much mana the items you wear have left, and how long it lasts.

The server never says how much mana a worn item has as it drains (only an appraisal tells, or a mana stone or a cast
from an orb or wand: ITEM_CUR_MANA, int 107), so it's estimated from the last reading. While an item's spells are
on (on you, or on the item: Impenetrability), the server takes 1 mana every 1/rate seconds (WeenieObject::
WieldedTick), and 1 straight away when it's put on; loot's rate is its spells' total power / 10,000 a second, at
least appraisal.MIN_DRAIN. Nothing drains while you're logged out, while it's off, or when putting it on failed
(your Arcane Lore too low...: no spells on, no drain). At 0 the server says "Your X is out of Mana." and takes the
spells off.

Mana given to an item -- a mana stone or charge: "The Greater Mana Stone gives 404 points of mana to the Black Garnet
Staff." -- is kept on it: given (all of it, since the companion first saw the item) and gifts (the latest few: when,
how much, from what). Its new mana comes as ITEM_CUR_MANA too, a reading like an appraisal's.

A record per item (its id stays when it changes hands): {guid, name, mana, max, read (when that mana was read), source
(what read it: "appraisal", "mana stone" -- or "update", a cast from an orb or wand),
spells (names), worn (seconds its spells have been on since the reading, besides the stretch now going), since (when
the stretch now going started, or None), equips (put on since the reading: 1 each), error (why putting it on failed,
the error code, or None), out (it ran out)}.
"""

import re

from . import appraisal

ACTIVATION_ERRORS = range(1221, 1228)   # putting an item on that didn't switch its magic on (1223: your Arcane Lore
                                        # is too low...); the error follows the equip straight away
ERROR_TEXT = {1221: "Allegiance rank too low", 1222: "wrong heritage", 1223: "Arcane Lore too low",     # (GameEnums.h)
              1224: "not enough mana", 1225: "a skill too low", 1226: "not its crafter", 1227: "a skill not specialized"}
EQUIP_WINDOW = 3.0      # seconds: an activation error this soon after an equip is that item's
OUT_OF_MANA = re.compile(r"^Your (.+) is out of Mana\.$")
GIVES = re.compile(r"^The (.+?) gives ([\d,]+) points? of mana to (?:the |your )?(.+?)\.$")
GIFTS_KEPT = 20         # the latest mana stone uses kept on an item
# A stone gives all it holds, or as much as the item has room for (ManaStone.cpp): giving less than it held, it filled
# the item -- the item's mana is its max now. A stone's charge is its last ITEM_CUR_MANA; a charge's, its kind's (they
# come full: 9 WeenieDefaults ManaStone).
CHARGES = {"Tiny Mana Charge": 50, "Small Mana Charge": 100, "Moderate Mana Charge": 250, "High Mana Charge": 500,
           "Great Mana Charge": 1000}
FILL_WINDOW = 5.0       # seconds: the item's new mana and the stone's message come together (either first)


def rate(rec, powers):
    """Mana a second the item uses while its spells are on, or None (spells not known, or not loot's)."""
    return appraisal.mana_rate(rec.get("spells"), powers)


def estimate(rec, powers, now):
    """The item's mana now (estimated from the last reading), or None if it's never been read."""
    if rec.get("mana") is None:
        return None
    r = rate(rec, powers) or 0
    worn = (rec.get("worn") or 0) + (now - rec["since"] if rec.get("since") is not None else 0)
    return max(0.0, rec["mana"] - (rec.get("equips") or 0) - r * worn)


class ItemMana:
    def __init__(self):
        self.items = {}          # guid -> record
        self.last_equip = None   # (guid, when): an activation error just after is that item's

    def record(self, guid, name=None, recall=None):
        """The item's record: this login's, else the one remembered (recall: guid -> record or None), else new."""
        rec = self.items.get(guid)
        if rec is None:
            rec = (recall(guid) if recall else None) or {"guid": guid, "mana": None, "max": None, "read": None,
                                                          "spells": None, "worn": 0.0, "equips": 0, "error": None,
                                                          "out": False}
            rec["since"] = None              # (a stretch is this login's: a remembered one ended with its login)
            self.items[guid] = rec
        if name:
            rec["name"] = name
        return rec

    def reading(self, rec, mana, now, max_mana=None, spells=None, source="update"):
        """The item's mana, read (an appraisal, a mana update): the estimate starts again from it."""
        rec.update(mana=mana, read=now, worn=0.0, equips=0, out=mana == 0)
        if not (source == "update" and rec.get("source") == "mana stone" and now - (rec.get("read_at") or 0) < FILL_WINDOW):
            rec["source"] = source           # (a stone's update, just after its "gives": the stone read it)
        rec["read_at"] = now
        if rec.get("filled") is not None and now - rec["filled"] < FILL_WINDOW:
            rec["max"] = mana                # (a stone just filled it: this is as full as it gets)
        if max_mana is not None:
            rec["max"] = max_mana
        if spells and "Unknown" not in spells:
            rec["spells"] = list(spells)
        if rec.get("since") is not None:
            rec["since"] = now

    def wear(self, rec, now, equip):
        """Its spells came on: put on (equip: it costs 1 mana, and clears an earlier failure), or worn at login."""
        if equip:
            rec["equips"] = (rec.get("equips") or 0) + 1
            rec["error"] = None
            self.last_equip = (rec["guid"], now)
        elif rec.get("error"):
            return                           # (worn at login, but its magic didn't come on when it was put on)
        if rec.get("since") is None:
            rec["since"] = now

    def failed(self, code, now):
        """An activation error: the item just put on didn't switch on -- no spells, no drain. The record, or None."""
        if not self.last_equip or now - self.last_equip[1] > EQUIP_WINDOW or self.last_equip[0] not in self.items:
            return None
        rec = self.items[self.last_equip[0]]
        self.last_equip = None
        rec["error"] = code
        rec["equips"] = max(0, (rec.get("equips") or 0) - 1)
        rec["since"] = None
        return rec

    def off(self, rec, now):
        """Taken off (or logged out): its spells' time on so far kept, the clock stopped."""
        if rec.get("since") is not None:
            rec["worn"] = (rec.get("worn") or 0) + now - rec["since"]
            rec["since"] = None

    def given(self, rec, amount, source, now, held=None):
        """A mana stone (or charge) gave the item mana: kept on it (its new mana comes as an update). held: what the
        stone held (or the least any it could have been held), if known -- more than it gave, and the item's full."""
        rec["given"] = (rec.get("given") or 0) + amount
        rec["gifts"] = ((rec.get("gifts") or []) + [{"ts": now, "amount": amount, "from": source}])[-GIFTS_KEPT:]
        if held is not None and held > amount:
            rec["gifts"][-1]["held"] = held
            rec["filled"] = now
            if rec.get("mana") is not None and now - (rec.get("read_at") or -1e9) < FILL_WINDOW:
                rec["max"] = rec["mana"]     # (its new mana came first)
        rec["source"], rec["read_at"] = "mana stone", now

    def ran_out(self, name, now, worn):
        """"Your X is out of Mana.": the worn item of that name (worn: the guids you wear) is empty. The record or None."""
        rec = next((r for g, r in self.items.items() if g in worn and r.get("name") == name), None)
        if rec:
            self.reading(rec, 0, now)
        return rec
