"""Game-state tracker fed by decoded events.

Skill-usage XP grants (Player.cpp MaybeGiveSkillUsageXP) arrive as:
    0x28 skill update   pp += S, carrying the stored (difficulty, seconds since grant) from BEFORE this grant
    0x0E unassigned     += S          (the double-count bug)
    0x0E total          += S
    0x0E unassigned     += D // 10    (only when D >= 10)
    0x0E total          += D // 10
so a grant's difficulty D is pinned to [10k, 10k + 9] where k is the second unassigned step, then
narrowed to an exact value from the action that caused it (spell power, recipe difficulty). The next
0x28 for that skill reports the stored difficulty exactly, which confirms or corrects it.
"""

import json
import logging
import math
import time
from collections import Counter, deque
from pathlib import Path

from . import profilebuffs
from . import skills as sk
from .burn import BurnModel
from .dat import DEFAULT_VITAL_FORMULAS
from .ledger import FAILED, FIZZLED, REJECTED, SUCCESS, ComponentLedger
from . import research
from . import buffpanel
from . import stock
from . import monsters
from . import s2m
from . import compsort
from .worldmap import coords
from . import goals
from . import comporder
from . import appraisal

BANE_KEYS = {k for k, _ in buffpanel.BANES}
FOE_RECENT = 300.0               # the Monster card's < >: what you fought within this long of your latest fight
FOE_HISTORY = 40              # remembered at most (a busy 5 minutes fits)
STAND_IN_FOE = "Black Rabbit"
COMBAT_MODE = 0x28              # the combat-mode int (UpdateInt / login), as the client's wire value
STANCES = {1: "peace", 2: "melee", 3: "missile", 4: "magic"}
MAGIC_STANCE = 4
CASTER_TYPE = 0x8000            # ItemType: caster (wand, orb, staff)
OBJECT_GONE = 0x37              # WERROR_OBJECT_GONE: "Unable to move to object!" (it isn't there)
INTERRUPTED = 0x36              # WERROR_INTERRUPTED: what the server answers a stance change it won't make
STANCE_REFUSE_WINDOW = 2.0   # the Monster card's creature until you fight something

SPELL_MODS = buffpanel.load_mods()

XP_TOTAL = 0x15
XP_UNASSIGNED = 0x16
TEXT_COMPONENTS = "The spell consumed the following components: "
MAGIC_CHANNEL = 7
SYSTEM_CHANNEL, TELL_CHANNEL = 0, 4   # text channels: broadcasts ("... has been saved"), your own tells
EXPIRED_KEEP = 3600.0   # a buff of yours that ran out is reported this long (unless recast)
CHAT_KEEP = 500         # chat lines kept per login (all of them go to the database)
PLAYER_IDS = range(0x50000000, 0x60000000)
FIZZLE = 0x402
# errors the server returns when it refuses a cast/use request outright (TryBeginCast); nothing burns
REJECT_CODES = {0x1D: "rejected: busy", 0x3FC: "rejected: untrained", 0x3FE: "rejected: unlearned",
                0x400: "rejected: missing components", 0x401: "rejected: not enough mana",
                0x409: "rejected", 0x40A: "rejected: unprepared"}
COIN_VALUE = 20         # int stat: pyreals carried
ENCUMB_VAL = 5          # int stat: the burden of everything you carry (the 1999 item description has no burden)
LEVEL = 25              # int stat: character level
RUN_SKILL = 24          # Run (skills.py)
CORPSE_SECONDS_PER_LEVEL = 300.0   # a player's corpse lasts 5 minutes a level (Player.cpp; no floor in 1999)
MAX_CORPSES = 2         # the server keeps two of yours; a third makes the oldest rot (max_corpses_per_player)
WORN_WINDOW = 3600.0    # seconds of equipped gear's burden kept (the order's capacity target: the heaviest in it)
BUY_HOURS = stock.BUY_HOURS   # "buy for N hours" of component burn (active casting hours)
MAIN_PACK = 102         # items the main pack holds beside its side packs, when the server hasn't said
VENDOR_MOVE = 0.25      # metres further from the vendor that count as walking away (its window: closed)
NO_OBJECT = 0x38        # WERROR_NO_OBJECT: every refused vendor buy or sell ends with it
ACTION_TIMEOUT = 20.0  # an outstanding request with no answer by now is dropped
GRANT_WINDOW = 1.5   # seconds to collect a grant's XP updates
CAUSE_WINDOW = 15.0  # how far back an action can explain a grant
RECENT = 60

_DATA = Path(__file__).parent / "data"


def _load_prices():
    """The Zaikhal archmage's markup and item values (tools/build_comp_prices.py), to price Fillcomps
    when no vendor is open."""
    try:
        return json.loads((_DATA / "comp_prices.json").read_text())
    except (OSError, ValueError):
        return None


BASELINE = _load_prices()
try:                                    # {component name: icon id (hex)}: tools/build_component_icons.py
    COMPONENT_ICONS = json.loads((_DATA / "component_icons.json").read_text())
except (OSError, ValueError):
    COMPONENT_ICONS = {}
MONSTERS = monsters.load()
REACH_MARGIN = 2       # metres short of a spell's reach that a target has to be (it may step back)
PLAYERS_NEAR = 300     # metres: another player further than this isn't counted as in the area


def _load_recipes():
    try:
        rows = json.loads((_DATA / "recipes.json").read_text())
    except (OSError, ValueError):
        return {}
    table = {}
    for row in rows:
        table[(row["tool"], row["target"])] = row
        table.setdefault((row["target"], row["tool"]), row)  # the server retries with the pair swapped
    return table


class Tracker:
    def __init__(self, spells=None, store=None, on_change=None, clock=time.time, components=None, vital_formulas=None,
                 skill_formulas=None):
        self.spells = spells or {}
        self.components = components or {}
        self.skill_formulas = {**research.DEFAULT_SKILL_FORMULAS, **(skill_formulas or {})}
        self.vital_formulas = vital_formulas or DEFAULT_VITAL_FORMULAS
        self.burn = BurnModel(self.spells, components or {})
        self.recipes = _load_recipes()
        self.recipes_by_tool = {}
        for (tool, target), row in self.recipes.items():
            if row["tool"] == tool:  # the table also maps the swapped pair; keep the canonical direction
                self.recipes_by_tool.setdefault(tool, []).append(row)
        self.store = store
        self.on_change = on_change
        self.clock = clock
        self.prefs = None       # () -> this character's dashboard settings (buff profile, hunting stock), from the companion
        self.reset()

    # ---- session -------------------------------------------------------------------------------
    def reset(self, server=None):
        """A new client connection: forget everything."""
        self.server = server
        self.session = time.strftime("%Y%m%d-%H%M%S")
        self.account = None
        self.burn.set_account(None)
        self.last_packet = None
        self.reset_character()

    def reset_character(self):
        """A character left the world (logoff to character select, or another character entered on
        the same connection): forget everything about the character, keep the connection's account."""
        logout = None
        if getattr(self, "in_world", False):
            self.logouts = getattr(self, "logouts", 0) + 1
            if getattr(self, "character", None):            # the chat's "Logged out from ..." line, while its name's known
                self._chat(self.clock(), "logout", None, f"Logged out from {self.character}")
                logout = self.chat[-1]
        self.session_start = None
        self.player = None
        self.character = None
        self.login_noted = False      # the chat's "Logged in as" line for this character (_note_login)
        self.in_world = False
        self.xp_total = None
        self.xp_unassigned = None
        self.xp_start_total = None
        self.grant_gain = 0
        self.skills = {}          # id -> state dict
        self.attributes = {}      # 1 Str .. 6 Self -> current value
        self.attribute_cp = {}    # 1 Str .. 6 Self -> the wire's XP spent (goals.attribute_raises reads it)
        self.vitals = {}          # 2 health, 4 stamina, 6 mana -> {raises, init, current}
        self.objects = {}         # guid -> {name, wcid, stack, container}
        self.pending_container = {}  # guid -> container from a "put" that arrived before the object
        self.grants = deque(maxlen=RECENT)
        self.chat = deque(maxlen=CHAT_KEEP)
        if logout is not None:
            self.chat.append(logout)          # (kept: the last line at character select says who logged out)
        self.expired = {}         # category -> {name, spell_id, ts}: your own buffs that ran out
        self.foe = None           # the creature you fought last: {guid, name, wcid, ts}
        self.casts = deque(maxlen=RECENT)
        self.known_spells = {}    # spell id -> casting likelihood (from the spellbook)
        self.spellbar = None      # [spell id] on the client's spell bar, in order (None: not seen yet)
        self.enchantments = {}    # enchantment id (spell | layer << 16) -> active buff/debuff on you
        self.item_enchants = {}   # (item, stat key) -> estimated bane/impen you cast on your armor
        self.duration_ratios = deque(maxlen=20)  # actual / table duration of your own buffs
        self.vendor = None        # the last vendor whose window opened: {id, items, sell_price, ts}
        self.comp_vendor = None   # the last one of them that sold spell components: {id, name, items, sell_price, ts}
        self.corpses = {}         # your corpses: id -> {id, name, ns, ew (None in a dungeon), cell, x, y, died, expires}
        self._corpses_of = None   # whose corpses those are (read back from the store once per character)
        self.position = None      # {cell, x, y, z, qw, qz, ts} from the client's reports and the server's updates
        self.level = None
        self.coins = None
        self.actions = deque()    # outstanding cast/use requests, oldest first
        self.skip_dones = 0       # UseDones already accounted for by an error or result
        self.last_aseq = 0        # the client's latest game-action counter
        self.appraisals = deque(maxlen=20)
        self.described = None    # the latest item appraisal's line (appraisal.py): what the companion copies
        self.burden = None       # everything carried, the server's total (ENCUMB_VAL)
        self.appraised_burden = {}   # item -> its burden, from an appraisal this login (the item book has older ones)
        self.ledger = ComponentLedger(self.burn)
        self._order = None      # (what it was worked out from, the component order): comporder.plan, cached
        self._stock_cache = {}  # stock.plan's work on that history (stretches, 90% stock levels)
        self.stance = None      # combat stance, the wire value: 1 peace, 2 melee, 3 missile, 4 magic
        self.stance_asked = None  # (asked at, the stance before) while a change is waiting on the server
        self.last_inventory_failed = None   # (item, error, when) from the server's 0xA0
        self.order = {}         # container -> [item guids] in the order they sit (0x196, 0x0013, 0x22's slot)
        self.pack_order = []    # the side packs in the main pack, in order
        self.last_action = None   # {"kind", "ts", ...}
        self.pending = None       # grant being assembled

    def handle(self, ev, now=None):
        now = self.clock() if now is None else now
        self.last_packet = now
        self._expire_pending(now)
        fn = getattr(self, "_on_" + ev["type"], None)
        if fn:
            fn(ev, now)
            if self.on_change:
                self.on_change()

    def tick(self, now=None):
        """Call periodically so a grant without a second XP step is finalised."""
        now = self.clock() if now is None else now
        self._note_worn(now)
        if self._expire_pending(now) and self.on_change:
            self.on_change()

    # ---- client actions ------------------------------------------------------------------------
    def _note_aseq(self, ev):
        self.last_aseq = max(self.last_aseq, ev.get("aseq", 0))

    def _on_spellbar_add(self, ev, now):
        """A spell put on the bar at a slot (one already there moves); the server keeps the same list."""
        bar = [s for s in (self.spellbar or []) if s != ev["spell"]]
        bar.insert(max(0, min(ev["index"], len(bar))), ev["spell"])
        self.spellbar = bar

    def _on_spellbar_remove(self, ev, now):
        if self.spellbar is not None:
            self.spellbar = [s for s in self.spellbar if s != ev["spell"]]

    def _on_appraise(self, ev, now):
        self._note_aseq(ev)
        if not ev["object"]:
            return  # the client clearing its selection
        obj = self.objects.get(ev["object"], {})
        self.appraisals.appendleft({"ts": now, "object": ev["object"], "name": obj.get("name", "?"), "success": None})
        self._set_foe(ev["object"], now)         # a monster you appraise: the Monster card shows it
        kind = self._creature_kind(ev["object"])
        if kind:                                 # a spell cast on others can be researched at it (Spells page)
            self.examined = {"guid": ev["object"], "name": obj.get("name", "?"), "kind": kind, "ts": now}

    def _spell_powers(self):
        """{spell name: power}, for how long an item's mana lasts (appraisal.mana_hours)."""
        return {s["name"]: s["power"] for s in self.spells.values() if s.get("name") and s.get("power") is not None}

    def _on_appraisal(self, ev, now):
        if ev["object"] in self.objects and "magic" in ev:
            self.objects[ev["object"]]["magic"] = ev["magic"]     # carries spells
        obj = self.objects.get(ev["object"], {})
        # an item you appraised (not a creature or player), and it worked: a line to paste (appraisal.py)
        prof = ev.get("profile")
        item = ev["success"] and prof and not prof.get("creature")
        powers = self._spell_powers() if item else None
        text = appraisal.describe(obj.get("name"), prof, powers) if item else None
        if ev["success"] and prof and prof.get("burden") is not None:
            self.appraised_burden[ev["object"]] = prof["burden"]
        if text:
            self.described = {"ts": now, "object": ev["object"], "name": obj.get("name", "?"), "text": text,
                              "clip": appraisal.clip_lines(prof, powers), "profile": prof}   # (a new one each time)
        for a in self.appraisals:
            if a["object"] == ev["object"] and a["success"] is None:
                a["success"], a["text"] = ev["success"], text
                return
        self.appraisals.appendleft({"ts": now, "object": ev["object"], "name": obj.get("name", "?"),
                                    "success": ev["success"], "text": text})

    def _on_cast(self, ev, now):
        self._note_aseq(ev)
        spell = self.spells.get(ev["spell"], {})
        cast = {"ts": now, "spell_id": ev["spell"], "target": ev.get("target"),
                "spell": spell.get("name", f"Spell {ev['spell']}"),
                "school": spell.get("school"), "power": spell.get("power"), "result": "casting", "components": []}
        self.casts.appendleft(cast)
        self.actions.append({"ts": now, "cast": cast})
        self.last_action = {"kind": "cast", "ts": now, "cast": cast}
        if ev.get("target"):
            self._set_foe(ev["target"], now)     # a war spell at a creature counts as fighting it

    def _on_use_with(self, ev, now):
        self._note_aseq(ev)
        src, dst = self.objects.get(ev["source"], {}), self.objects.get(ev["target"], {})
        recipe = self.recipes.get((src.get("wcid"), dst.get("wcid")))
        self.actions.append({"ts": now, "cast": None})
        self.last_action = {"kind": "use_with", "ts": now, "recipe": recipe,
                            "label": f"{src.get('name', '?')} on {dst.get('name', '?')}"}

    def _on_use(self, ev, now):
        self._note_aseq(ev)
        self.actions.append({"ts": now, "cast": None})
        self.last_action = {"kind": "use", "ts": now, "label": self.objects.get(ev["object"], {}).get("name", "?")}

    # ---- server state --------------------------------------------------------------------------
    def _on_account(self, ev, now):
        self.account = ev["account"]
        self.burn.set_account(self.account)

    def _on_logoff(self, ev, now):
        self.close_vendor("you logged off")
        if self.in_world:
            self.reset_character()

    def _on_player(self, ev, now):
        if self.player is not None:
            self.reset_character()  # another character entered the world on this connection
        self.player = ev["object"]
        self.in_world = True
        if self.session_start is None:
            self.session_start = now
        me = self.objects.get(self.player)
        if me and not self.character:
            self.character = me["name"]
        self._note_login(now)

    def _note_login(self, now):
        """A line in the chat when a character enters the world ("Logged in as ..."), once its name is known:
        the chat history is the account's, so it marks where one character's begins."""
        if self.in_world and self.character and not getattr(self, "login_noted", False):
            self.login_noted = True
            self._chat(now, "login", None, f"Logged in as {self.character}")

    def _on_player_description(self, ev, now):
        if self.session_start is None:
            self.session_start = now
        self.character = ev["strings"].get(1, self.character)
        self._note_login(now)
        if COMBAT_MODE in ev["ints"]:
            self.stance = ev["ints"][COMBAT_MODE]
        if "inventory" in ev and self.player is not None:      # the main pack, in order
            self.order[self.player] = list(ev["inventory"]["items"])
            self.pack_order = list(ev["inventory"]["packs"])
        if XP_TOTAL in ev["ints"]:
            self.xp_total = ev["ints"][XP_TOTAL]
            if self.xp_start_total is None:
                self.xp_start_total = self.xp_total
        if XP_UNASSIGNED in ev["ints"]:
            self.xp_unassigned = ev["ints"][XP_UNASSIGNED]
        if COIN_VALUE in ev["ints"]:
            self.coins = ev["ints"][COIN_VALUE]
        if ENCUMB_VAL in ev["ints"]:
            self.burden = ev["ints"][ENCUMB_VAL]
        if LEVEL in ev["ints"]:
            self.level = ev["ints"][LEVEL]
        for skill_id, rec in ev["skills"].items():
            self.skills[skill_id] = self._skill_from_record(skill_id, rec, now, self.skills.get(skill_id))
        if "spells" in ev:
            self.known_spells = dict(ev["spells"])
        if "spellbar" in ev:
            self.spellbar = list(ev["spellbar"])
        if "enchantments" in ev:  # the full registry at login replaces whatever we had
            self.enchantments = {}
            self._on_enchantments({"list": ev["enchantments"]}, now)
        self.attributes.update(ev.get("attributes", {}))
        self.attribute_cp.update(ev.get("attribute_cp", {}))
        self.vitals.update(ev.get("vitals", {}))

    def _on_attribute(self, ev, now):
        if self.player is None or ev["object"] == self.player:
            self.attributes[ev["key"]] = ev["value"]
            if ev.get("cp") is not None:
                self.attribute_cp[ev["key"]] = ev["cp"]

    def _on_vital(self, ev, now):
        if self.player is not None and ev["object"] != self.player:
            return
        key = ev["key"] + (ev["key"] % 2)  # max and current keys share one record: 1->2, 3->4, 5->6
        self.vitals[key] = {"raises": ev["raises"], "init": ev["init"], "cp": ev.get("cp"), "current": ev["current"]}

    def _on_spell_learned(self, ev, now):
        self.known_spells.setdefault(ev["spell"], 1.0)

    def _on_spell_removed(self, ev, now):
        self.known_spells.pop(ev["spell"], None)

    # ---- position ------------------------------------------------------------------------------------
    def _on_position(self, ev, now):
        if ev["object"] is not None and ev["object"] != self.player:
            if ev["object"] in self.objects:     # where things stand (a vendor's window closes as you walk off)
                self.objects[ev["object"]]["pos"] = {k: ev[k] for k in ("cell", "x", "y", "z")}
            return  # someone else moving
        self.position = {k: ev[k] for k in ("cell", "x", "y", "z", "qw", "qz")}
        self.position["ts"] = now
        self._vendor_moved()

    def _position_snapshot(self):
        p = self.position
        if not p:
            return None
        bx, by = p["cell"] >> 24, (p["cell"] >> 16) & 0xFF
        # heading: rotation about z; facing = (0, 1) turned by it, as (east, north)
        angle = 2 * math.atan2(p["qz"], p["qw"])
        return {"cell": p["cell"], "x": p["x"], "y": p["y"], "ts": p["ts"],
                "ns": (by * 192 + p["y"]) / 240 - 101.95, "ew": (bx * 192 + p["x"]) / 240 - 101.95,
                "indoors": (p["cell"] & 0xFFFF) >= 0x100,
                "facing": [-math.sin(angle), math.cos(angle)]}

    # ---- vendors -------------------------------------------------------------------------------------
    # A vendor's window counts as open from its stock list (sent when you use it, and after every sale) until
    # you move at all further from it, another opens, or you leave. The client never says it closed the
    # window, so anything short of that is taken as closed: better a refused click than a refused purchase.
    def _on_vendor(self, ev, now):
        self.vendor = {"id": ev["vendor"], "items": ev["items"], "sell_price": ev["sell_price"], "ts": now,
                       "open": True, "dist": self._vendor_distance(ev["vendor"]),
                       "from": dict(self.position) if self.position else None}
        names = {c.get("name") for c in self.components.values()}
        if any(i.get("name") in names for i in ev["items"]):
            # a component vendor: its prices and wares are the shop's until another's used -- this login or the next
            # (Companion sets remember_vendor: kept per character)
            self.comp_vendor = {"id": ev["vendor"], "name": self.objects.get(ev["vendor"], {}).get("name", "Vendor"),
                                "sell_price": ev["sell_price"], "ts": now,
                                "items": [{k: i.get(k) for k in ("iid", "name", "value", "amount", "max_stack")}
                                          for i in ev["items"] if i.get("name") in names]}
            if callable(getattr(self, "remember_vendor", None)) and self.character:
                self.remember_vendor(self.character, self.comp_vendor)

    @staticmethod
    def _global(p):
        """(x, y, z) metres in one frame for a position anywhere: its landblock's corner plus the local spot."""
        return ((p["cell"] >> 24) * 192 + p["x"], ((p["cell"] >> 16) & 0xFF) * 192 + p["y"], p.get("z") or 0.0)

    def _vendor_distance(self, vendor):
        v, me = (self.objects.get(vendor) or {}).get("pos"), self.position
        if not v or not me:
            return None
        return math.dist(self._global(v), self._global(me))

    def _vendor_moved(self):
        v = self.vendor
        if not v or not v.get("open"):
            return
        d = self._vendor_distance(v["id"])
        if d is None:                            # the vendor's spot isn't known: any move at all closes it
            start = v.get("from")
            if start is None or math.dist(self._global(start), self._global(self.position)) > VENDOR_MOVE:
                self.close_vendor("you moved")
            return
        if v.get("dist") is not None and d > v["dist"] + VENDOR_MOVE:
            self.close_vendor("you moved away from the vendor")
        elif v.get("dist") is None or d < v["dist"]:
            v["dist"] = d                        # closer: moving off from here closes it

    def close_vendor(self, why):
        if self.vendor and self.vendor.get("open"):
            self.vendor["open"], self.vendor["closed"] = False, why

    def vendor_open(self, vendor=None):
        """True while a vendor's window is taken as open (that vendor's, if given)."""
        v = self.vendor
        return bool(v and v.get("open") and (vendor is None or v["id"] == vendor))

    @staticmethod
    def vendor_price(item, sell_price, amount):
        """Vendor.cpp VendorSellPrice: ceil(value * sell multiplier * amount - 0.1), at least 1."""
        return max(1, math.ceil((item.get("value") or 0) * sell_price * amount - 0.1))

    def main_pack_free(self):
        """Free slots in the main pack (side packs don't count: a purchase lands in the main pack), or None."""
        if self.player is None:
            return None
        cap = (self.objects.get(self.player) or {}).get("items_capacity") or MAIN_PACK
        return max(0, cap - sum(1 for g in self.pack_contents(self.player) if g not in self.pack_order))

    @staticmethod
    def slots_for(item, amount):
        """Main-pack slots buying `amount` of a vendor item takes (Vendor.cpp: a slot per stack, rounded up)."""
        per = max(1, item.get("max_stack") or 1)
        return -(-amount // per)

    @staticmethod
    def base_price(name):
        """One of a component at the Zaikhal archmage's prices (comp_prices.json), or None: what to price with when
        no vendor's been seen."""
        value = ((BASELINE or {}).get("values") or {}).get(name)
        return Tracker.vendor_price({"value": value}, BASELINE["sell_price"], 1) if value else None

    @staticmethod
    def fit_slots(item, amount, free):
        """How much of `amount` fits in `free` main-pack slots: whole stacks, the last one may be partial."""
        if Tracker.slots_for(item, amount) <= free:
            return amount
        return max(0, free) * max(1, item.get("max_stack") or 1)

    def _vendor_snapshot(self, components, now):
        """The current vendor and what it can sell toward each component's buy-for-N-hours amount."""
        v = self.vendor
        if not v or not v.get("open"):
            return None                          # no window open: nothing to buy from
        stock = {i.get("name"): i for i in v["items"] if i.get("name")}
        plan, missing = [], []
        for c in components:
            if not c.get("buy"):
                continue
            item = stock.get(c["name"])
            if item is None:
                missing.append(c["name"])
                continue
            amount = c["buy"] if item["amount"] is None else min(c["buy"], item["amount"])
            plan.append({"name": c["name"], "iid": item["iid"], "amount": amount, "max_stack": item.get("max_stack"),
                         "slots": self.slots_for(item, amount), "carried": c.get("carried") or 0, "level": c.get("level"),
                         "cost": self.vendor_price(item, v["sell_price"], amount)})
        return {"id": v["id"], "name": self.objects.get(v["id"], {}).get("name", "Vendor"), "seen": v["ts"],
                "items": len(v["items"]), "stock": sorted({i["name"] for i in v["items"] if i.get("name")}),
                "plan": plan, "missing": missing,
                "total": sum(p["cost"] for p in plan), "coins": self.coins,
                "slots": sum(p["slots"] for p in plan), "free_slots": self.main_pack_free()}

    def _wares(self):
        """The spell components the last vendor sold, in the order its window lists them (its window open or
        not): {vendor, open, items: [{name, icon, category, price (one), unit (value x the vendor's rate: a purchase of
        n costs ceil(unit x n - 0.1)), amount (None: unlimited), max_stack}]}, or None
        when no vendor's been seen or it sold none. The last one that sold components, this login, else the one this
        character (or, never having used one, any of yours) last used (Companion sets recall_vendor)."""
        v = self.comp_vendor
        if not v and callable(getattr(self, "recall_vendor", None)) and self.character:
            got = getattr(self, "_recalled", None)          # (read again a minute on: another login may have used one)
            if not got or got[0] != self.character or self.clock() - got[1] > 60:
                got = self._recalled = (self.character, self.clock(), self.recall_vendor(self.character))
            v = got[2]
        if not v:
            return None
        comps = {c.get("name"): c for c in self.components.values()}
        items = [{"name": i["name"], "icon": COMPONENT_ICONS.get(i["name"]), "category": comporder.category(comps[i["name"]]),
                  "price": self.vendor_price(i, v["sell_price"], 1), "unit": (i.get("value") or 0) * v["sell_price"],
                  "amount": i.get("amount"), "max_stack": i.get("max_stack")}
                 for i in v["items"] if i.get("name") in comps]
        if not items:
            return None
        return {"vendor": v.get("name") or self.objects.get(v["id"], {}).get("name", "Vendor"), "open": self.vendor_open(v["id"]),
                "items": items}

    # ---- spell research ------------------------------------------------------------------------------
    def burden_of(self, guid):
        """An item's burden, if it's been appraised: this login, else the item book's (Companion sets book_burden).
        The 1999 item description doesn't carry it."""
        known = self.appraised_burden.get(guid)
        if known is None and callable(getattr(self, "book_burden", None)):
            known = self.book_burden(guid)
        return known

    def worn_burden(self):
        """(The burden of everything worn or wielded that's been appraised, how many worn items haven't been)."""
        total, unknown = 0, 0
        for g, o in self.objects.items():
            if self.player is not None and o.get("wielder") == self.player:
                b = self.burden_of(g)
                if b is None:
                    unknown += 1
                else:
                    total += b
        return total, unknown

    def _note_worn(self, now):
        """What's equipped weighs, kept for WORN_WINDOW (a sample when it changes, and every minute)."""
        if not getattr(self, "in_world", False) or self.player is None:
            return
        h = self.__dict__.setdefault("worn_history", deque())
        w = self.worn_burden()[0]
        if not h or h[-1][1] != w or now - h[-1][0] >= 60:
            h.append((now, w))
        while h and now - h[0][0] > WORN_WINDOW:
            h.popleft()

    def capacity_target(self, prefs):
        """The component order's weight when it's set as a share of what you can carry (prefs restock.target_mode
        "capacity"): target_pct% of 150 x Strength (unbuffed: the order should fit when buffs drop), and out of that
        the most your equipped gear has weighed in the last hour -- what's left is the components'.
        {strength, capacity, pct, allowed, worn, target_bu}, or None."""
        got = (prefs or {}).get("restock") or {}
        if got.get("target_mode") != "capacity":
            return None
        strength = self.attributes.get(1) or 0
        if not strength:
            return None
        now_worn, unknown = self.worn_burden()
        worn = max([w for _, w in getattr(self, "worn_history", ())] + [now_worn])
        pct = max(1, min(100, int(got.get("target_pct") or 50)))
        allowed = 150 * strength * pct / 100
        return {"strength": strength, "capacity": 150 * strength, "pct": pct, "allowed": round(allowed), "worn": round(worn),
                "unknown": unknown, "target_bu": max(0, int(allowed - worn))}

    def run_speed(self):
        """Metres a second on foot, as the server has it (MovementSystem::GetRunRate x the run animation's 4 m/s):
        4 + 11 x Run / (Run + 200) x the load factor -- 1 up to your carrying capacity (150 x Strength), falling
        to 0 at twice it. Run and Strength buffed; the burden the server says you carry. None until Run is known."""
        enchantments = list(self.enchantments.values())
        run = research.effective_skill(RUN_SKILL, self.skills, self.attributes, enchantments, self.skill_formulas, min_sac=0)
        if run is None:
            return None
        if run >= 800:
            return 18.0                      # (the server's special case: the fastest)
        strength = research.effective_attribute(self.attributes.get(1, 0), 1, research.active_enchantments(enchantments))
        burden = self.burden or 0
        load = burden / (150 * strength) if strength else 0
        factor = 1.0 if load < 1 else max(0.0, 2.0 - load)
        return 4.0 + 11.0 * run / (run + 200.0) * factor

    level_xp = []   # total XP to reach each level (dat.load_level_xp; Companion sets it)
    xp_tables = {}  # the ExperienceTable's cost tables (dat.load_xp_tables; Companion sets it)

    def experience(self):
        """The Experience page's snapshot (goals.summary), worked out again only when something it uses changed."""
        prefs = self.prefs() if callable(getattr(self, "prefs", None)) else None
        mine = (prefs or {}).get("goals") or []
        key = json.dumps([mine, self.attributes, self.attribute_cp, self.vitals, self.xp_unassigned, self.xp_total,
                          {k: (s.get("sac"), s.get("pp"), s.get("level")) for k, s in self.skills.items()},
                          (prefs or {}).get("buff_profiles"), (prefs or {}).get("active_profile"),
                          sorted(self.known_spells), self.magic_skills()], sort_keys=True, default=str)
        cached = getattr(self, "_experience", None)
        if not cached or cached[0] != key:
            cached = self._experience = (key, goals.summary(self, self.xp_tables, mine, prefs, SPELL_MODS))
        return cached[1]

    def _level_progress(self):
        """{level, level_from (the XP it took), level_to (the next level's), or {} when not known."""
        table, level = self.level_xp, self.level
        if not table or not level or self.xp_total is None:
            return {}
        if level + 1 >= len(table):
            return {"level": level, "level_from": table[min(level, len(table) - 1)], "level_to": None}
        return {"level": level, "level_from": table[level], "level_to": table[level + 1]}

    def magic_skills(self):
        """{spell school: effective school skill (buffs included)} for the trained magic schools."""
        out = {}
        enchantments = list(self.enchantments.values())
        for school, skill_id in research.SCHOOL_SKILL.items():
            value = research.effective_skill(skill_id, self.skills, self.attributes, enchantments, self.skill_formulas)
            if value is not None:
                out[school] = value
        return out

    def research(self):
        rows = research.research_table(self.spells, self.components, self.account, self.known_spells,
                                       self.inventory_counts(), self.magic_skills())
        weapon_spells = self.weapon_spells()
        items = self.item_targets()
        weapons = None
        for r in rows:
            r["on_examined"] = self.examined_check(r)
            if r["target_type"] == "item" and r["needs_target"]:
                r["on_item"] = True           # learned on an item you pick (the page's dropdown: `items`)
                if r["id"] in weapon_spells:  # Blood Drinker, Heart Seeker, Swift Killer, Defender: your weapon first
                    weapons = self.weapons() if weapons is None else weapons
                    r["default_item"] = next((w["id"] for w in weapons if any(i["id"] == w["id"] for i in items)), None)
        return {"account": self.account, "skills": {sk.skill_name(research.SCHOOL_SKILL[s]): v
                                                    for s, v in self.magic_skills().items()},
                "examined": self.examined_snapshot(), "items": items, "spells": rows,
                "layout": buffpanel.research_layout(self.spells, SPELL_MODS, rows, self.skills),
                "components": {cid: {"name": c.get("name"), "icon": COMPONENT_ICONS.get(c.get("name"))}
                               for cid, c in self.components.items()}}

    def item_targets(self):
        """What an item spell can be cast on: your worn, wielded and single pack items of a type the server
        enchants (ITEM_ENCHANTABLE: weapons, armor, clothing, jewelry, gems, casters...), worn first."""
        return [{"id": t["id"], "name": t["name"], "worn": t["worn"], "kind": t["kind"]} for t in self._targets()
                if (self.objects.get(t["id"], {}).get("item_type") or 0) & research.ITEM_ENCHANTABLE]

    def weapon_spells(self):
        """The ids of every tier of the weapon spells (the Buffs page's Weapon rows)."""
        return {o["spell"] for row in buffpanel.choices(self, SPELL_MODS) if row["section"] == "Weapon"
                for o in row["options"]}

    def weapons(self):
        """The weapons a weapon spell goes on: the ones picked on the config page you still carry, else what
        you wield (profilebuffs.gear_for, as the Buffs page picks them)."""
        prefs = self.prefs() if callable(getattr(self, "prefs", None)) else None
        return profilebuffs.gear_for(self, prefs, "weapon", self._targets())

    def research_row(self, spell_id):
        return next((r for r in self.research()["spells"] if r["id"] == spell_id), None)

    # ---- enchantments (buffs and debuffs on you) -------------------------------------------------
    def _on_enchantments(self, ev, now):
        for e in ev["list"]:
            self.expired.pop(e["category"], None)   # that kind of buff is back
            started = now + e["start"]  # sent relative to the moment the server wrote it
            self.enchantments[e["id"]] = {
                "id": e["id"], "spell_id": e["spell"], "layer": e["layer"], "category": e["category"],
                "power": e["power"], "caster": e["caster"], "started": started, "duration": e["duration"],
                "expires": None if e["duration"] < 0 else started + e["duration"], "stat": e["stat"]}
            table = self.spells.get(e["spell"], {}).get("duration")
            if e["caster"] == self.player and table and e["duration"] > 0 and e["start"] > -1:
                self.duration_ratios.append(e["duration"] / table)  # a fresh cast, not one restored at login

    def _note_item_spell(self, cast, now):
        """A bane/Impenetrability (on armor, underclothes or a shield) or a weapon spell (Blood Drinker, Heart
        Seeker, Swift Killer, Defender) that landed on one of your items: remember it with an estimated expiry
        (the server never sends an item's enchantments to its owner), and what kind of item it went on."""
        target = cast.get("target")
        mod = SPELL_MODS.get(cast["spell_id"])
        if not target or target == self.player or not mod or mod[1] not in buffpanel.ITEM_KEYS:
            return
        if self.objects.get(target, {}).get("wielder") not in (None, self.player):
            return                  # someone else's
        spell = self.spells.get(cast["spell_id"], {})
        if not spell.get("duration"):
            return
        entry = self.item_enchants.setdefault((target, mod[1]), {"key": mod[1], "target": target})
        # every version stays on the item (Acid Bane II and III both): each kept with its own expiry; recasting
        # one refreshes just that one
        versions = entry.setdefault("versions", {})
        if not versions and entry.get("spell_id") is not None:      # remembered before versions were kept
            versions[entry["spell_id"]] = {k: entry[k] for k in ("spell", "spell_id", "value", "started", "expires") if k in entry}
        versions[cast["spell_id"]] = {"spell": spell.get("name"), "spell_id": cast["spell_id"], "value": mod[2], "started": now,
                                      "expires": now + spell["duration"] * self.duration_factor()}
        entry.update(item=self.objects.get(target, {}).get("name", "item"),
                     slot=buffpanel.item_slot(self.objects.get(target, {}).get("location")))
        self._item_effective(entry, now)

    @staticmethod
    def _item_effective(entry, now):
        """What an item's versions add up to now: the strongest one still on it (spell, value, its start), and
        on until the LAST of them runs out (one expiring doesn't take the buff off)."""
        versions = entry.get("versions") or {}
        live = [v for v in versions.values() if v["expires"] > now] or list(versions.values())
        if not live:
            return
        best = max(live, key=lambda v: (abs(v["value"]), v["expires"]))
        entry.update(spell=best["spell"], spell_id=best["spell_id"], value=best["value"], started=best["started"],
                     expires=max(v["expires"] for v in live))

    def duration_factor(self):
        """How much longer than the spell table your buffs actually last (spell economy x charge), from
        the buffs you cast on yourself; 1.0 until one has been seen."""
        if not self.duration_ratios:
            return 1.0
        ordered = sorted(self.duration_ratios)
        return ordered[len(ordered) // 2]

    def _on_enchantments_removed(self, ev, now):
        for eid in ev["ids"]:
            e = self.enchantments.pop(eid, None)
            # one of your own buffs ran out and nothing else of its kind is still on you
            if (e and e["caster"] == self.player and self.spells.get(e["spell_id"], {}).get("bitfield", 0) & 0x4
                    and not any(o["category"] == e["category"] for o in self.enchantments.values())):
                self.expired[e["category"]] = {"name": self.spells[e["spell_id"]].get("name"),
                                               "spell_id": e["spell_id"], "ts": now}

    def _recast_spell(self, ench):
        """A spell from your spellbook that renews this enchantment on yourself: the same spell when you
        know it, else the strongest self-castable spell of its category. (spell id, its power) or None."""
        def self_castable(sid):
            return sid in self.known_spells and "Other" not in self.spells.get(sid, {}).get("name", "").split()
        if self_castable(ench["spell_id"]):
            return ench["spell_id"], ench["power"]
        best = None
        for sid in self.known_spells:
            sp = self.spells.get(sid, {})
            if sp.get("category") == ench["category"] and self_castable(sid):
                if best is None or sp.get("power", 0) > best[1]:
                    best = (sid, sp.get("power", 0))
        return best

    def _buffs(self, now):
        """Active enchantments, soonest to expire first. In each category only the strongest applies
        (ties: the newest), so the rest are marked as surpassed."""
        live = [e for e in self.enchantments.values() if e["expires"] is None or e["expires"] > now - 2]
        winners = {}
        for e in live:
            w = winners.get(e["category"])
            if w is None or (e["power"], e["started"]) > (w["power"], w["started"]):
                winners[e["category"]] = e
        out = []
        for e in live:
            spell = self.spells.get(e["spell_id"], {})
            recast = self._recast_spell(e)
            out.append({
                "id": e["id"], "spell_id": e["spell_id"], "name": spell.get("name", f"Spell {e['spell_id']}"),
                "school": spell.get("school_name"), "icon": f"{spell['icon']:08x}" if spell.get("icon") else None,
                "power": e["power"], "expires": e["expires"], "duration": e["duration"],
                "self_cast": e["caster"] == self.player, "active": winners[e["category"]] is e,
                "beneficial": bool(spell.get("bitfield", 0) & 0x4),
                "recast": recast[0] if recast else None,
                "recast_name": self.spells.get(recast[0], {}).get("name") if recast else None,
                "recast_weaker": bool(recast) and recast[1] < e["power"],
            })
        out += self._bane_buffs(now)
        return sorted(out, key=lambda b: (b["expires"] is None, b["expires"] or 0, b["name"]))

    def _bane_buffs(self, now):
        """Banes and Impenetrability on your items, one entry per item, at their estimated expiry (the server
        never says): recast on that same item."""
        by_name = None
        out = []
        for (target, key), b in self.item_enchants.items():
            if key not in BANE_KEYS or b["expires"] <= now - 2 or b["value"] < 0:
                continue
            sid = b.get("spell_id")
            if sid is None:          # remembered before spell ids were kept: find it by name
                by_name = by_name or {sp.get("name"): i for i, sp in self.spells.items()}
                sid = by_name.get(b["spell"])
            out.append({
                "id": f"item:{target}:{key}", "spell_id": sid, "name": f"{b['spell']} ({b['item']})",
                "school": self.spells.get(sid, {}).get("school_name"), "icon": None, "power": None,
                "expires": b["expires"], "duration": b["expires"] - b["started"], "self_cast": True, "active": True,
                "beneficial": True, "estimate": True, "target": target,
                "recast": sid if sid in self.known_spells else None, "recast_name": b["spell"], "recast_weaker": False})
        return out

    def _skill_from_record(self, skill_id, rec, now, old=None):
        st = old or {"id": skill_id, "grants": 0, "skill_xp": 0}
        st.update(sac=rec["sac"], pp=rec["pp"], level=rec["level_from_pp"] + rec["init_level"])
        st["stored_difficulty"] = rec["resistance"]
        st["stored_confirmed"] = True
        st["last_grant_ts"] = now - rec["since_last_grant"]
        return st

    def _on_int(self, ev, now):
        if self.player is not None and ev["object"] != self.player:
            return
        key, value = ev["key"], ev["value"]
        if key == COIN_VALUE:
            self.coins = value
        elif key == ENCUMB_VAL:
            self.burden = value
        elif key == LEVEL:
            self.level = value
        elif key == COMBAT_MODE:          # the server setting your stance (it sends the wire value)
            self.stance, self.stance_asked = value, None
        elif key == XP_TOTAL:
            if self.xp_start_total is None:
                self.xp_start_total = value
            self.xp_total = value
        elif key == XP_UNASSIGNED:
            delta = None if self.xp_unassigned is None else value - self.xp_unassigned
            self.xp_unassigned = value
            if delta is not None:
                self._unassigned_step(delta, now)

    def _on_skill(self, ev, now):
        if self.player is not None and ev["object"] != self.player:
            return
        sid = ev["skill"]
        old = self.skills.get(sid)
        delta = ev["pp"] - old["pp"] if old else 0
        if self.pending:
            self._finish_pending(now)
        if old and old.get("last_grant") and old["last_grant"].get("difficulty") != ev["resistance"]:
            # This record carries the stored difficulty from the previous grant: correct our estimate.
            g = old["last_grant"]
            g["difficulty"] = ev["resistance"]
            g["confirmed"] = True
        if old and delta > 0:
            # Either a usage grant (unassigned rises next) or XP spent raising the skill (it falls).
            old.update(sac=ev["sac"], pp=ev["pp"], level=ev["level_from_pp"] + ev["init_level"])
            self.pending = {"skill": sid, "skill_xp": delta, "ts": now, "steps": [], "cause": self._cause(now),
                            "record": ev}
            return
        self.skills[sid] = self._skill_from_record(sid, ev, now, old)

    def _unassigned_step(self, delta, now):
        p = self.pending
        if not p:
            return
        if delta < 0:
            self.pending = None  # XP spent raising the skill, not a grant
            self._apply_record(p, now)
            return
        p["steps"].append(delta)
        if len(p["steps"]) >= 2:
            self._finish_pending(now)

    def _apply_record(self, p, now):
        sid = p["skill"]
        self.skills[sid] = self._skill_from_record(sid, p["record"], p["ts"], self.skills.get(sid))

    def _expire_pending(self, now):
        if self.pending and now - self.pending["ts"] > GRANT_WINDOW:
            self._finish_pending(now)
            return True
        return False

    def _finish_pending(self, now):
        p, self.pending = self.pending, None
        if not p:
            return
        if not p["steps"]:
            self._apply_record(p, now)  # no matching unassigned increase: not a usage grant
            return
        sid = p["skill"]
        tenth = p["steps"][1] if len(p["steps"]) > 1 else 0
        low, high = tenth * 10, tenth * 10 + 9
        exact, label = self._difficulty_from_cause(sid, p["cause"], low, high)
        unassigned = sum(p["steps"])
        grant = {"ts": p["ts"], "skill_id": sid, "skill": sk.skill_name(sid), "skill_xp": p["skill_xp"],
                 "unassigned_xp": unassigned, "difficulty": exact, "difficulty_low": low,
                 "difficulty_high": high, "confirmed": False, "cause": label}
        st = self.skills.setdefault(sid, {"id": sid, "grants": 0, "skill_xp": 0, "sac": sk.TRAINED})
        st["grants"] += 1
        st["skill_xp"] += p["skill_xp"]
        st["stored_difficulty"] = exact if exact is not None else low
        st["stored_confirmed"] = exact is not None
        st["last_grant_ts"] = p["ts"]
        st["last_grant"] = grant
        self.grant_gain += unassigned
        self.grants.appendleft(grant)
        if self.store:
            self.store.add("grant", grant, self.session, ts=p["ts"])

    def _cause(self, now):
        a = self.last_action
        return a if a and now - a["ts"] <= CAUSE_WINDOW else None

    def _difficulty_from_cause(self, sid, cause, low, high):
        candidates = []
        label = None
        if cause and cause["kind"] == "cast":
            c = cause["cast"]
            label = c["spell"]
            if c.get("power") is not None:
                if sid == sk.MANA_CONVERSION:
                    candidates.append(sk.mana_conversion_difficulty(c["power"]))
                else:
                    candidates.append(c["power"])
        elif cause and cause["kind"] == "use_with":
            label = cause["label"]
            if cause.get("recipe") and cause["recipe"]["skill"] == sid:
                candidates.append(cause["recipe"]["difficulty"])
        elif cause:
            label = cause.get("label")
        for d in candidates:
            if low <= d <= high:
                return d, label
        return None, label

    # ---- casts and components ------------------------------------------------------------------
    # The server answers every cast/use request with exactly one UseDone (0x1C7). A request it refuses
    # up front (busy, no mana, missing components, ...) gets a 0x1C error and its UseDone within the
    # same tick, while an accepted cast resolves after its windup: burn text, then a fizzle error or
    # "You cast ..." text, then its UseDone. So refusals belong to the NEWEST outstanding request and
    # outcomes to the OLDEST, and each error/result consumes that request's UseDone.
    def _settle(self, cast, outcome, result):
        cast["result"] = result
        self.ledger.settle(cast, outcome)
        if self.store and outcome != REJECTED:
            record = {k: v for k, v in cast.items() if k != "settled"}
            self.store.add("cast", dict(record, character=self.character), self.session, ts=cast["ts"])

    def _expire_actions(self, now):
        while self.actions and now - self.actions[0]["ts"] > ACTION_TIMEOUT:
            stale = self.actions.popleft()
            if stale["cast"] is not None:
                stale["cast"]["result"] = "unknown"

    def _resolve(self, action, outcome=None, result=None):
        self.actions.remove(action)
        if action["cast"] is not None and outcome is not None:
            self._settle(action["cast"], outcome, result)

    def _oldest_cast(self):
        return next((a for a in self.actions if a["cast"] is not None), None)

    def _on_text(self, ev, now):
        self._expire_actions(now)
        self._chat_text(ev, now)
        text = ev["text"]
        action = self._oldest_cast()
        if text.startswith(TEXT_COMPONENTS):
            names = [n.strip() for n in text[len(TEXT_COMPONENTS):].split(",") if n.strip()]
            self.ledger.burned(action["cast"] if action else None, names)
        elif action and ev["channel"] == MAGIC_CHANNEL:
            if text.startswith("You cast"):
                self._resolve(action, SUCCESS, "cast")
                self.skip_dones += 1
                self._note_item_spell(action["cast"], now)
            elif "resists your spell" in text:
                self._resolve(action, SUCCESS, "resisted")
                self.skip_dones += 1

    # ---- chat ---------------------------------------------------------------------------------------
    def _incantation(self, text):
        """True for spell words ("Malar Easith"): every word is made of components' spoken words (the
        second word of an incantation joins several: Ea + sith)."""
        if not hasattr(self, "_words"):
            self._words = {c.get("word", "").lower() for c in self.components.values() if c.get("word")}
        if not self._words:
            return False

        def joined(w):
            ok = [True] + [False] * len(w)          # ok[i]: w[:i] splits into component words
            for i in range(1, len(w) + 1):
                ok[i] = any(ok[j] and w[j:i] in self._words for j in range(max(0, i - 8), i))
            return ok[-1]
        words = text.lower().split()
        return bool(words) and len(words) <= 4 and all(joined(w) for w in words)

    def _chat(self, now, kind, sender, text, **extra):
        line = {"ts": now, "kind": kind, "sender": sender, "text": text, "character": self.character, **extra}
        if self.chat and self.chat[-1]["ts"] >= now:      # one packet can carry several lines: keep them apart
            line["ts"] = self.chat[-1]["ts"] + 1e-6
        self.chat.append(line)
        if self.store:
            self.store.add("chat", line, self.session, ts=line["ts"])
            self._link_account()

    def _link_account(self):
        """Tell the store which account this character is on (once per pair): chat history is the account's."""
        pair = (self.character, self.account)
        if self.store and all(pair) and pair != getattr(self, "_linked", None) and hasattr(self.store, "link"):
            self.store.link(*pair)
            self._linked = pair

    def _on_speech(self, ev, now):
        if self._incantation(ev["text"]):
            return
        player = ev["sender_id"] in PLAYER_IDS
        self._chat(now, "say" if player else "npc", ev["sender"], ev["text"], me=ev["sender_id"] == self.player)

    def _on_channel(self, ev, now):
        """Fellowship (and other group) chat; your own lines come from what you sent."""
        sender = self.character if ev["mine"] else ev["sender"]
        self._chat(now, "channel", sender, ev["text"], channel=ev["channel"], me=ev["mine"])

    # ---- what you're fighting ---------------------------------------------------------------------------
    def _set_foe(self, guid, now):
        o = self.objects.get(guid)
        if o and guid != self.player and guid not in PLAYER_IDS and str(o.get("wcid")) in MONSTERS:
            self.foe = {"guid": guid, "name": o.get("name"), "wcid": o["wcid"], "ts": now}
            # the ones fought lately, newest last (the Monster card's < >): this one moves to the end
            self.foes = [f for f in getattr(self, "foes", []) if f["guid"] != guid][-(FOE_HISTORY - 1):] + [self.foe]

    def _on_attack(self, ev, now):
        self._set_foe(ev["target"], now)

    def _creature_kind(self, guid):
        """"player" (someone else), "monster", or None (you, an item, an NPC, something not seen)."""
        o = self.objects.get(guid)
        if not o or guid == self.player:
            return None
        return "player" if guid in PLAYER_IDS else "monster" if str(o.get("wcid")) in MONSTERS else None

    def examined_snapshot(self):
        """The monster or player you last appraised in game: {guid, name, kind, alive, distance}, or None."""
        e = getattr(self, "examined", None)
        if not e:
            return None
        d = self.distance_to(e["guid"])
        return dict(e, alive=self._alive(e["guid"]), distance=None if d is None else round(d, 1))

    def examined_check(self, row):
        """Whether a research row's spell can be cast at the creature you last appraised, the way the server
        checks it (CheckTargetValidity): None when it isn't a spell cast on a creature, else {ok, why}. A
        helpful spell goes on players only (no buffing monsters); a harmful one on either (it lands on a player,
        with no effect); either has to reach it."""
        if not row.get("needs_target") or row.get("target_type") not in ("creature", None):
            return None
        e = self.examined_snapshot()
        if not e:
            return {"ok": False, "why": "appraise a monster or player in game to learn it on them"}
        if not e["alive"]:
            return {"ok": False, "why": f"{e['name']} is gone (dead or out of view)"}
        if row.get("beneficial") and e["kind"] != "player":
            return {"ok": False, "why": f"a helpful spell: only on a player, not {e['name']}"}
        far = research.reach(self.spells.get(row["id"], {}), row.get("skill"))
        if far is not None and e["distance"] is not None and e["distance"] > far - REACH_MARGIN:
            return {"ok": False, "why": f"{e['name']} is too far: {e['distance']:.0f} m, it reaches {far:.0f} m"}
        return {"ok": True, "why": None}

    def confirm_target(self, guid):
        """Make the candidate the target (it must be the current candidate). True when it was."""
        p = getattr(self, "picked", None)
        if not p or p["guid"] != guid:
            return False
        self.target = dict(p, confirmed=self.clock())
        return True

    def clear_target(self):
        self.target = None

    def _alive(self, guid):
        o = self.objects.get(guid)
        return bool(o) and not o.get("dead") and o.get("health") != 0

    def picked_target(self):
        """The confirmed target's id while it's alive and still in the world, else None."""
        t = getattr(self, "target", None)
        return t["guid"] if t and self._alive(t["guid"]) else None

    def distance_to(self, guid):
        """Metres from you to an object (its last known spot), or None when either isn't known."""
        o, me = (self.objects.get(guid) or {}).get("pos"), self.position
        return math.dist(self._global(o), self._global(me)) if o and me else None

    def _on_health(self, ev, now):
        if ev["object"] in self.objects:
            self.objects[ev["object"]]["health"] = ev["fraction"]

    def _on_death(self, ev, now):
        if ev["object"] in self.objects:
            self.objects[ev["object"]]["dead"] = True

    def players_near(self):
        """Other players in view: [{guid, name, distance, since}], nearest first. One last seen further than
        PLAYERS_NEAR away is left out (the client forgets what's behind a portal; the server needn't say)."""
        seen = {s["guid"]: s["ts"] for s in getattr(self, "player_sightings", None) or []}
        out = []
        for guid, o in self.objects.items():
            if guid not in PLAYER_IDS or guid == self.player or not o.get("name"):
                continue
            d = self.distance_to(guid)
            if d is not None and d > PLAYERS_NEAR:
                continue
            out.append({"guid": guid, "name": o["name"], "distance": None if d is None else round(d),
                        "since": seen.get(guid)})
        return sorted(out, key=lambda p: (p["distance"] is None, p["distance"] or 0))

    def _picked_snapshot(self):
        """{candidate, target}: what you picked in game (to confirm), and the confirmed target, each with
        alive / health / distance; None when there's neither."""
        def info(p):
            if not p:
                return None
            o = self.objects.get(p["guid"]) or {}
            d = self.distance_to(p["guid"])
            return dict(p, alive=self._alive(p["guid"]), health=o.get("health"), distance=None if d is None else round(d, 1))
        cand, target = getattr(self, "picked", None), getattr(self, "target", None)
        if not cand and not target:
            return None
        return {"candidate": info(cand) if cand and (not target or cand["guid"] != target["guid"]) else None,
                "target": info(target)}

    def _on_combat(self, ev, now):
        """'You hit X' / 'X hits you' name the creature: the newest object of that name we know."""
        if self.foe and self.foe["name"] == ev["foe"]:
            self.foe["ts"] = now          # (the same dict as the history's newest)
            return
        guid = max((g for g, o in self.objects.items() if o.get("name") == ev["foe"]), default=None)
        if guid is not None:
            self._set_foe(guid, now)

    def _foe_card(self, m, **extra):
        return dict(monsters.analyze(m), defenses=monsters.defenses(m, self.skill_formulas), xp=monsters.kill_xp(m), **extra)

    def _foe_snapshot(self):
        if not self.foe:      # nothing fought yet: a Black Rabbit's real numbers, so the card isn't empty
            m = next((m for m in MONSTERS.values() if m.get("name") == STAND_IN_FOE), None)
            return self._foe_card(m, guid=None, ts=None, stand_in=True) if m else None
        m = MONSTERS.get(str(self.foe["wcid"]))
        return self._foe_card(m, guid=self.foe["guid"], ts=self.foe["ts"]) if m else None

    def _foes_snapshot(self, now):
        """The creatures fought within FOE_RECENT seconds of your latest fight (however long ago that was), in
        alphabetical order."""
        foes = getattr(self, "foes", []) or ([self.foe] if self.foe else [])
        if not foes:
            return []
        latest = max(f["ts"] for f in foes)
        out = [self._foe_card(MONSTERS[str(f["wcid"])], guid=f["guid"], ts=f["ts"]) for f in foes
               if str(f["wcid"]) in MONSTERS and latest - f["ts"] <= FOE_RECENT]
        return sorted(out, key=lambda f: (f["name"], f["guid"]))

    def _on_tell(self, ev, now):
        player = ev["sender_id"] in PLAYER_IDS
        self._chat(now, "tell" if player else "npc", ev["sender"], ev["text"])

    def _chat_text(self, ev, now):
        text, channel = ev["text"], ev["channel"]
        if channel == TELL_CHANNEL and text.startswith("You tell "):
            to, _, said = text[len("You tell "):].partition(", ")
            self._chat(now, "tell_out", "You", said.strip().strip('"'), to=to)
        elif channel == SYSTEM_CHANNEL:
            self._chat(now, "system", None, text)

    def _on_stance_request(self, ev, now):
        """You asked for a stance: taken as yours unless the server refuses it straight away (INTERRUPTED)."""
        self.stance_asked = (now, self.stance)
        self.stance = ev["mode"]

    def stance_snapshot(self):
        """{mode, name, caster, ready}: your stance, the caster (wand, orb, staff) you're holding if any, and
        whether you can cast from it (magic stance with a caster in hand)."""
        caster = next((o for g, o in self.objects.items() if o.get("wielder") == self.player and self.player is not None
                       and (o.get("item_type") or 0) & CASTER_TYPE), None)
        return {"mode": self.stance, "name": STANCES.get(self.stance), "caster": caster and caster.get("name"),
                "ready": self.stance == MAGIC_STANCE and caster is not None}

    def _on_error(self, ev, now):
        self._expire_actions(now)
        code = ev["code"]
        if code == INTERRUPTED and self.stance_asked and now - self.stance_asked[0] < STANCE_REFUSE_WINDOW:
            self.stance, self.stance_asked = self.stance_asked[1], None      # the stance change was refused
        if code in REJECT_CODES:
            if self.actions:
                self._resolve(self.actions[-1], REJECTED, REJECT_CODES[code])
                self.skip_dones += 1
            return
        action = self._oldest_cast() if code == FIZZLE else (self.actions[0] if self.actions else None)
        if action:
            if code == FIZZLE:
                self._resolve(action, FIZZLED, "fizzled")
            else:
                self._resolve(action, FAILED, f"failed (0x{code:X})")
            self.skip_dones += 1

    def _on_use_done(self, ev, now):
        self._expire_actions(now)
        if self.skip_dones:
            self.skip_dones -= 1
            return
        if self.actions:
            self._resolve(self.actions[0], SUCCESS, "done")

    # ---- inventory -----------------------------------------------------------------------------
    def _on_object(self, ev, now):
        guid = ev["object"]
        if self.character and ev.get("position") and ev.get("name") == f"Corpse of {self.character}":
            self._note_corpse(ev, now)
        was_here = guid in self.objects
        container = ev.get("container")
        if container is None:
            # A vendor purchase arrives as "put in pack" (0x22) BEFORE the item's create message, which then
            # leaves out the container; a re-sent item also keeps the container we already know.
            container = self.pending_container.pop(guid, None) or self.objects.get(guid, {}).get("container")
        else:
            self.pending_container.pop(guid, None)
        self.objects[guid] = {"name": ev["name"], "wcid": ev["wcid"], "stack": ev.get("stack", 1),
                              "max_stack": ev.get("max_stack"), "container": container,
                              "wielder": ev.get("wielder"), "item_type": ev.get("item_type"),
                              "location": ev.get("location"), "valid_locations": ev.get("valid_locations"),
                              "items_capacity": ev.get("items_capacity"), "spell": ev.get("spell"),
                              "magic": self.objects.get(guid, {}).get("magic")}
        if ev.get("position"):
            self.objects[guid]["pos"] = {k: ev["position"][k] for k in ("cell", "x", "y", "z")}
        if ev["object"] == self.player and not self.character:
            self.character = ev["name"]  # the description's string table doesn't carry the name
            self._note_login(now)
        if guid in PLAYER_IDS and guid != self.player and not was_here:
            # another player came into view (the dashboard's players near you)
            sightings = getattr(self, "player_sightings", None)
            if sightings is None:
                sightings = self.player_sightings = []
            sightings.append({"guid": guid, "name": ev["name"], "ts": now})
            del sightings[:-50]
        if ev["object"] == self.player and ev.get("position"):
            self._on_position(dict(ev["position"], object=None), now)

    def _on_stack(self, ev, now):
        if ev["object"] in self.objects:
            self.objects[ev["object"]]["stack"] = ev["stack"]

    def _on_moved(self, ev, now):
        if "slot" in ev:
            self._place(ev["object"], ev["container"], ev["slot"], ev.get("pack"))
        if ev["object"] in self.objects:     # into a pack: not worn any more (if it was)
            self.objects[ev["object"]].update(container=ev["container"], wielder=None, location=None)
        else:
            self.pending_container[ev["object"]] = ev["container"]  # its create message follows

    def _on_equipped(self, ev, now):
        """You put an item on (armor, clothes, a shield, a weapon): worn by you, in those slots."""
        obj = self.objects.get(ev["object"])
        if obj is not None and self.player is not None:
            obj.update(wielder=self.player, location=ev["location"], container=None)

    def _on_removed(self, ev, now):
        if ev["object"] in self.objects:
            self.objects[ev["object"]]["container"] = None
        self._unorder(ev["object"])

    def _note_corpse(self, ev, now):
        """One of your corpses, seen created (it is where you died, then): kept till it rots -- 5 minutes a level."""
        guid, p = ev["object"], ev["position"]
        self._load_corpses(now)
        if guid in self.corpses:
            return                          # seen again (back in view, a relogin): its death's when it was first
        c = coords(p["cell"], p["x"], p["y"])
        self.corpses[guid] = {"id": guid, "name": ev.get("name"), "ns": c[0] if c else None, "ew": c[1] if c else None,
                              "cell": p["cell"], "x": p["x"], "y": p["y"], "died": now,
                              "expires": now + CORPSE_SECONDS_PER_LEVEL * max(1, self.level or 1)}
        for old in sorted(self.corpses.values(), key=lambda c: c["died"])[:-MAX_CORPSES]:
            self.corpses.pop(old["id"], None)    # a third makes the oldest rot
        if callable(getattr(self, "remember_corpse", None)):
            self.remember_corpse(self.character, self.corpses[guid])

    def _load_corpses(self, now):
        if self._corpses_of != self.character and self.character:
            self._corpses_of = self.character
            self.corpses = {}
            if callable(getattr(self, "recall_corpses", None)):
                self.corpses = {c["id"]: c for c in self.recall_corpses(self.character, now)}

    def corpse_list(self, now):
        """Your corpses not yet rotted, the newest first, with how long each has left."""
        self._load_corpses(now)
        for guid in [g for g, c in self.corpses.items() if c["expires"] <= now]:
            del self.corpses[guid]
        return [dict(c, left=c["expires"] - now) for c in sorted(self.corpses.values(), key=lambda c: -c["died"])]

    def _on_deleted(self, ev, now):
        if self.vendor and self.vendor["id"] == ev["object"]:
            self.close_vendor("the vendor went out of view")
        self.objects.pop(ev["object"], None)
        self._unorder(ev["object"])

    # ---- the order of things in each pack (for sorting components into packs) ---------------------------
    def _on_inventory_failed(self, ev, now):
        """The server says an item you tried to use isn't there (OBJECT_GONE): forget it, so nothing keeps
        trying it (a coin stack it dropped without telling the client, say)."""
        self.last_inventory_failed = (ev["object"], ev["code"], now)
        if ev["code"] == OBJECT_GONE and ev["object"] in self.objects:
            self.objects.pop(ev["object"], None)
            self._unorder(ev["object"])

    def _on_contents(self, ev, now):
        self.order[ev["container"]] = list(ev["items"])
        if ev["container"] == self.player:
            self.pack_order = list(ev["packs"])

    def _unorder(self, guid):
        for items in self.order.values():
            if guid in items:
                items.remove(guid)
        if guid in self.pack_order:
            self.pack_order.remove(guid)

    def _place(self, guid, container, slot, pack):
        """A move to slot N of a container: out of wherever it was, then in at N (Container_InsertInventoryItem)."""
        self._unorder(guid)
        if pack:
            if container == self.player:
                self.pack_order.insert(min(slot, len(self.pack_order)), guid)
            return
        items = self.order.setdefault(container, [])
        items.insert(min(slot, len(items)), guid)

    def pack_contents(self, container):
        """Items in a container in their order: the known order, then any it holds that the order missed."""
        known = [g for g in self.order.get(container, []) if self.objects.get(g, {}).get("container") == container]
        rest = sorted(g for g, o in self.objects.items() if o.get("container") == container and g not in known
                      and g not in self.pack_order)
        return known + rest

    def _carried(self, obj):
        c = obj.get("container")
        if c is None or self.player is None:
            return False
        if c == self.player:
            return True
        pack = self.objects.get(c)
        return bool(pack and pack.get("container") == self.player)

    def inventory_counts(self):
        counts = Counter()
        for obj in self.objects.values():
            if self._carried(obj):
                counts[obj["name"]] += obj.get("stack") or 1
        return counts

    # ---- dashboard -----------------------------------------------------------------------------
    def snapshot(self):
        now = self.clock()
        self._link_account()
        for entry in self.item_enchants.values():     # a version running out may leave a weaker one on
            self._item_effective(entry, now)
        inv = self.inventory_counts()
        skills = []
        for st in self.skills.values():
            if st.get("sac", 0) < sk.TRAINED:
                continue
            g = st.get("last_grant")
            skills.append({
                "id": st["id"], "name": sk.skill_name(st["id"]), "sac": st.get("sac"), "level": st.get("level"),
                "stored_difficulty": st.get("stored_difficulty"), "stored_confirmed": st.get("stored_confirmed"),
                "last_grant_ts": st.get("last_grant_ts"), "grants": st["grants"], "skill_xp": st["skill_xp"],
                "climb": bool(g and now - g["ts"] < 20),
            })
        skills.sort(key=lambda s: s["name"])
        burn = self.ledger.snapshot(inv, self._session_hours(now), {sid: s["name"] for sid, s in self.spells.items()})
        self._apply_stock_plan(burn, inv, now)
        return {
            "now": now,
            "status": {"connected": self.last_packet is not None and now - self.last_packet < 10,
                       "server": self.server, "character": self.character, "session_start": self.session_start,
                       "in_world": self.in_world, "account": self.account},
            "xp": {"total": self.xp_total, "unassigned": self.xp_unassigned, **self._level_progress(),
                   "session_gain": None if self.xp_start_total is None or self.xp_total is None
                   else self.xp_total - self.xp_start_total,
                   "grant_gain": self.grant_gain},
            "skills": skills,
            "grants": list(self.grants)[:25],
            "chat": list(self.chat)[-200:],
            "comp_sort": [dict(s, icon=COMPONENT_ICONS.get(s["name"])) for s in compsort.comp_steps(self)],
            "stance": self.stance_snapshot(),
            "foe": self._foe_snapshot(),
            "foes": self._foes_snapshot(now),
            "expired_buffs": sorted((dict(x) for x in self.expired.values() if now - x["ts"] < EXPIRED_KEEP),
                                    key=lambda x: -x["ts"]),
            "casts": [{k: v for k, v in c.items() if k != "settled"} for c in list(self.casts)[:25]],
            "burn": burn,
            "vendor": self._vendor_snapshot(burn["components"], now),
            "vendor_closed": self.vendor.get("closed") if self.vendor and not self.vendor.get("open") else None,
            "wares": self._wares(),
            "corpses": self.corpse_list(now),
            "experience": self.experience(),
            "coins": self.coins,
            "spellbook": self._spellbook(inv),
            "spellbar": self.spellbar,
            "vitals": self._vital_snapshot(),
            "buffs": self._buffs(now),
            "buff_panel": buffpanel.build(self, now),
            "s2m": s2m.table(self),
            "components": self._components_carried(inv),
            "targets": self._targets(),
            "picked": self._picked_snapshot(),
            "players_near": self.players_near(),
            "players_seen": [dict(s) for s in (getattr(self, "player_sightings", None) or [])[-25:]],
            "item_targets": self.item_targets(),
            "position": self._position_snapshot(),
            "level": self.level,
            "inventory": self._carried_items(),
            "crafts": self._craftable(),
            "appraisals": list(self.appraisals)[:10],
            "busy": any(now - a["ts"] < ACTION_TIMEOUT for a in self.actions),
        }

    def _targets(self):
        """What a cast can be aimed at: worn or wielded gear and single (unstacked) pack items. Stacks
        can't be enchanted (the server refuses them)."""
        out = []
        for guid, o in self.objects.items():
            if guid == self.player:
                continue
            worn = o.get("wielder") == self.player
            single = (o.get("stack") or 1) == 1 and (o.get("max_stack") or 1) <= 1
            if single and (worn or self._carried(o)):
                out.append({"id": guid, "name": o["name"], "worn": worn, "slot": buffpanel.item_slot(o.get("location")) if worn else None,
                            "kind": buffpanel.item_kind(o)})
        return sorted(out, key=lambda t: (not t["worn"], t["name"]))

    def _stock_history(self, now):
        """This character's cast history (the last 30 days, or back to the component order's start), oldest first:
        the store's, read once and kept up in memory."""
        if not (self.store and self.character):
            return []
        prefs = self.prefs() if callable(self.prefs) else None
        try:
            return self.store.casts(self.character, since=comporder.settings(prefs)["since"])
        except Exception:
            return []

    def _apply_stock_plan(self, burn, inv, now):
        """Rates, hours left and how many to buy, from this character's whole cast history (stock.py)
        rather than this login."""
        history = self._stock_history(now)
        plan, hours = stock.plan(history, self.burn, inv, self._stock_cache) if history else ({}, 0.0)
        rows = {c["name"]: c for c in burn["components"]}
        for name in plan:
            if name not in rows:   # used before, not yet this login
                rows[name] = {"name": name, "carried": inv.get(name), "used": 0, "expected": 0,
                              "per_hour": None, "per_hour_expected": None, "hours_left": None}
        for name, c in rows.items():
            p = plan.get(name)
            if p:
                c.update(per_hour=p["per_hour"], hours_left=p["hours_left"] if c["carried"] is not None else None,
                         buy=p["buy"], stock_target=p["target"], stock_floor=p["floor"],
                         why=[dict(w, name=self.spells.get(w["spell"], {}).get("name")) for w in p["why"]],
                         basis="history")
            else:  # no history yet: this login's rate, as before
                c["buy"] = None if c["per_hour"] is None else max(0, math.ceil(c["per_hour"] * BUY_HOURS - (c["carried"] or 0)))
                c["basis"] = "session"
        # what to keep: what you burned since the order's start, scaled to the target BU, with each category's
        # floor (comporder.py)
        prefs = self.prefs() if callable(self.prefs) else None
        cap = self.capacity_target(prefs)
        if cap:       # the weight as a share of what you can carry: the target it comes to, for the order
            prefs = dict(prefs, restock=dict(prefs.get("restock") or {}, target_bu=cap["target_bu"]))
        # worked out again when a cast's been added, your settings or spells changed, or a minute's gone by
        key = (self.character, len(history), history[-1].get("ts") if history else None, int(now // 60),
               json.dumps((prefs or {}).get("restock"), sort_keys=True), tuple(sorted(self.known_spells)))
        if self._order is None or self._order[0] != key:
            self._order = (key, comporder.plan(history, self.components, self.burn.required, prefs,
                                               known=list(self.known_spells),
                                               spell_name=lambda sid: self.spells.get(sid, {}).get("name"), now=now))
        order, info = self._order[1]
        for name in list(order) + [n for n in info["exclude"] if n in info["uses"]]:   # (excluded ones stay listed)
            if name not in rows:   # a component your spells use that hasn't burned lately
                rows[name] = {"name": name, "carried": inv.get(name), "used": 0, "expected": 0,
                              "per_hour": None, "per_hour_expected": None, "hours_left": None}
        cats = {c.get("name"): comporder.category(c) for c in self.components.values()}
        ids = {c.get("name"): cid for cid, c in self.components.items()}
        for name, c in rows.items():
            c["stock_target"] = c["level"] = order.get(name, 0)
            c["recommended"] = info["recommended"].get(name, 0)
            c["override"], c["adjust"] = info["override"].get(name), info["adjust"].get(name)
            c["excluded"] = name in info["exclude"]
            c["uses"] = info["uses"].get(name) or []
            c["buy"] = max(0, c["level"] - (c.get("carried") or 0))
            c["window_burned"] = info["burned"].get(name, 0)
            c["category"] = cats.get(name)
            c["icon"] = COMPONENT_ICONS.get(name)
            c["base_price"] = Tracker.base_price(name)
            value = ((BASELINE or {}).get("values") or {}).get(name)
            c["base_unit"] = value * BASELINE["sell_price"] if value else None     # (as a ware's unit)
            c["component_id"] = ids.get(name)
            c["basis"] = "order"
        burn["order"] = {k: v for k, v in info.items() if k not in ("burned", "recommended", "uses")}
        burn["order"]["capacity"] = cap
        burn["components"] = sorted(rows.values(), key=lambda c: c["name"])
        burn["buy_hours"] = BUY_HOURS
        burn["stock"] = {"active_hours": round(hours, 1), "min_stock": stock.MIN_STOCK, "half_life": stock.HALF_LIFE}
        burn["fill_estimate"] = self._fill_estimate(burn["components"])

    @staticmethod
    def _fill_estimate(components):
        """What buying every component's recommended amount costs at the Zaikhal archmage's prices:
        {total, units, unpriced: [names she doesn't sell]}."""
        if not BASELINE:
            return None
        total = units = 0
        unpriced = []
        for c in components:
            amount = c.get("buy") or 0
            if amount <= 0:
                continue
            value = BASELINE["values"].get(c["name"])
            if value is None:
                unpriced.append(c["name"])
                continue
            total += Tracker.vendor_price({"value": value}, BASELINE["sell_price"], amount)
            units += amount
        return {"total": total, "units": units, "unpriced": unpriced, "vendor": BASELINE["vendor"]}

    def _components_carried(self, inv):
        """[{name, count, id, icon, category}] of every spell component in the pack, by category (scarabs, herbs,
        powders, potions, talismans, tapers), alphabetical within each."""
        ids = {c.get("name"): cid for cid, c in self.components.items()}
        return sorted(({"name": n, "count": c, "id": ids[n], "icon": COMPONENT_ICONS.get(n),
                        "category": self.components[ids[n]].get("category")}
                       for n, c in inv.items() if n in ids), key=lambda c: (c["category"] if c["category"] is not None else 9, c["name"]))

    def held(self):
        """{guid: where} of everything this character holds: worn or wielded ("worn"), in the main pack ("main pack"),
        or in a side pack ("pack N"); the side packs themselves too."""
        out = {}
        if self.player is None:
            return out
        for guid, o in self.objects.items():
            if guid == self.player:
                continue
            if o.get("wielder") == self.player:
                out[guid] = "worn"
            elif o.get("container") == self.player:
                out[guid] = f"pack {self.pack_order.index(guid) + 1}" if guid in self.pack_order else "main pack"
            elif self._carried(o):
                c = o.get("container")
                out[guid] = f"pack {self.pack_order.index(c) + 1}" if c in self.pack_order else "pack"
        return out

    def _carried_items(self):
        items = [{"id": guid, "name": o["name"], "stack": o.get("stack") or 1, "wcid": o["wcid"]}
                 for guid, o in self.objects.items() if guid != self.player and self._carried(o)]
        return sorted(items, key=lambda i: (i["name"], i["id"]))

    def _craftable(self):
        """Recipes whose tool is carried, one entry per recipe, with carried totals. A target you don't
        carry yet (count 0) still lists, so a chain can plan for it: steaks before they're carved."""
        totals, names = {}, {}
        for item in self._carried_items():
            totals[item["wcid"]] = totals.get(item["wcid"], 0) + item["stack"]
            names[item["wcid"]] = item["name"]
        out = []
        for tool_wcid in totals:
            for recipe in self.recipes_by_tool.get(tool_wcid, []):
                out.append({"tool_wcid": tool_wcid, "tool_name": names[tool_wcid], "tool_count": totals[tool_wcid],
                            "target_wcid": recipe["target"],
                            "target_name": names.get(recipe["target"], recipe["target_name"]),
                            "target_count": totals.get(recipe["target"], 0), "result": recipe["result"],
                            "skill": sk.skill_name(recipe["skill"]) if recipe["skill"] else None,
                            "difficulty": recipe["difficulty"]})
        return sorted(out, key=lambda c: (not c["target_count"], c["result"], c["tool_name"]))

    def carried_named(self, name):
        """[(guid, stack)] of carried items with this name."""
        return [(i["id"], i["stack"]) for i in self._carried_items() if i["name"] == name]

    def carried_count(self, wcid):
        return sum(i["stack"] for i in self._carried_items() if i["wcid"] == wcid)

    def is_busy(self, now):
        return any(now - a["ts"] < ACTION_TIMEOUT for a in self.actions)

    def carried_guids(self):
        return {i["id"] for i in self._carried_items()}

    def merge_plan(self, guid):
        """(target, amount) to fold this carried stack into another of its kind, or None: the fullest
        stack that still has room, so stacks fill up one at a time."""
        src = self.objects.get(guid)
        if not src or not self._carried(src):
            return None
        best = None
        for g, o in self.objects.items():
            if g == guid or g == self.player or o["wcid"] != src["wcid"] or not self._carried(o):
                continue
            room = (o.get("max_stack") or 0) - (o.get("stack") or 1) if o.get("max_stack") else None
            if room is not None and room <= 0:
                continue
            if best is None or (o.get("stack") or 1) > best[1]:
                best = (g, o.get("stack") or 1, room)
        if best is None:
            return None
        amount = src.get("stack") or 1
        return best[0], amount if best[2] is None else min(amount, best[2])

    def find_carried(self, wcid, exclude=None):
        """Guid of a carried item of this type (smallest stack first, so partial stacks get used up)."""
        items = [i for i in self._carried_items() if i["wcid"] == wcid and i["id"] != exclude]
        return min(items, key=lambda i: i["stack"])["id"] if items else None

    def _vital_snapshot(self):
        """Current and maximum vitals, the way CACQualities::InqAttribute2nd works the max out: the
        Attribute2ndTable formula round((w + x*attr1 + y*attr2) / z) on your BUFFED attributes (Endurance
        Self raises max stamina), plus init + raises, then vitae and any max-vital buffs, rounded
        (EnchantAttribute2nd). A current value above that is taken as the max and flagged."""
        out = []
        enchantments = list(self.enchantments.values())
        active = research.active_enchantments(enchantments)
        for key, name in ((2, "Health"), (4, "Stamina"), (6, "Mana")):
            v = self.vitals.get(key)
            if v is None:
                continue
            w, x, y, z, a1, a2 = self.vital_formulas[key - 1]
            v1 = research.effective_attribute(self.attributes.get(a1, 0), a1, active) if a1 else 0
            v2 = research.effective_attribute(self.attributes.get(a2, 0), a2, active) if a2 else 0
            base = int((w + x * v1 + y * v2) / z + 0.5) if z else 0
            maximum = float(base + v["init"] + v["raises"])
            for e in enchantments:
                if e["spell_id"] == research.VITAE_SPELL:
                    maximum *= e["stat"]["value"]
            maximum = research.vital_enchant(maximum, key - 1, active)
            maximum = int(maximum + 0.5)
            out.append({"name": name, "current": v["current"], "max": max(maximum, v["current"]),
                        "above_base": v["current"] > maximum})
        return out

    def _spellbook(self, inv):
        """Known spell ids, with casts left from carried components (None = unknown formula)."""
        status = {}
        for sid in self.known_spells:
            left, limiting = self.burn.casts_left(sid, inv)
            if left is None:
                continue
            status[sid] = {"casts_left": None if left == float("inf") else int(left), "limiting": limiting}
        return {"known": sorted(self.known_spells), "status": status, "inventory_known": bool(inv)}

    def _session_hours(self, now):
        return (now - self.session_start) / 3600.0 if self.session_start else 0.0
