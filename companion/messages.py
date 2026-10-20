"""Decode reassembled messages into plain dict events.

Strings are AcString1999: u16 length (incl. NUL), bytes, then padding to 4 relative to the message
start (the opcode is offset 0). Server events wrapped in 0xF7B0 [guid][seq][inner...] are unwrapped.
"""

import logging

from .binreader import ReadError, Reader

log = logging.getLogger(__name__)

# server -> client
OP_UPDATE_INT = 0x000E
OP_PLAYER_DESCRIPTION = 0x0013
OP_WEENIE_ERROR = 0x001C
OP_WEENIE_ERROR_STRING = 0x028B    # (in 0xF7B0) an error with a name in it: [i32 code][string] -- "You must be %s to use that
                                   # item's magic." (the 1999 client shows nothing for it)
OP_REMOVE_FROM_CONTAINER = 0x0024
OP_UPDATE_SKILL = 0x0028
OP_UPDATE_ATTRIBUTE = 0x0026   # [obj][key][attribute 12]
OP_UPDATE_VITAL = 0x0029       # [obj][key 1-6: MaxHealth Health MaxStamina Stamina MaxMana Mana][vital 16]
OP_STACK_SIZE = 0x0197
OP_USE_DONE = 0x01C7
OP_HEALTH = 0x01C0      # (in 0xF7B0) [object][health fraction]
OP_DEATH = 0x019E       # [text][victim][killer] (WeenieObject.cpp NotifyDeathMessage)
OP_APPRAISAL = 0x00C9         # [obj][flags][i32 success]...  (in 0xF7B0)
OP_TEXT = 0xF62C
OP_SPEECH = 0x0037        # heard nearby: [string text][string sender name][u32 sender] (players, NPCs, spell words)
OP_CHANNEL = 0x014A       # group chat heard: [u32 channel][string sender][string text] (0x800 fellowship)
ACT_CHANNEL_SEND = 0x0147  # you saying something on one (client action): [u32 channel][string text]
CHANNELS = {0x800: "Fellowship", 0x1000: "Vassals", 0x2000: "Patron", 0x4000: "Monarch", 0x8000: "Covassals",
            0x10000: "Allegiance"}
ACT_MELEE_ATTACK, ACT_MISSILE_ATTACK = 0x0008, 0x000A   # [target][height 1-3][power / accuracy]
OP_YOU_HIT, OP_HIT_YOU, OP_YOU_MISSED = 0x01B1, 0x01B2, 0x01B3   # (in 0xF7B0) start with the creature's name
OP_TELL = 0x0038          # a tell to you (in 0xF7B0): [string text][string sender name][u32 sender][u32 you]
OP_CREATE_OBJECT = 0xF745
OP_PLAYER_CREATE = 0xF746
OP_DELETE_OBJECT = 0xF747
OP_EVENT = 0xF7B0
OP_INVENTORY_PUT = 0x0022
OP_INVENTORY_FAILED = 0x00A0    # (in 0xF7B0) an inventory action on an item failed: [u32 item][u32 error]
OP_VIEW_CONTENTS = 0x0196       # (in 0xF7B0) a pack's contents in order (sent at login and when it's opened)
OP_INVENTORY_EQUIP = 0x0023     # (in 0xF7B0) you put an item on: [u32 item][u32 equipped slots (EquipMask)]
OP_CHARACTER_LIST = 0xF658
OP_SPELL_LEARNED = 0x004C   # Net_UpdateSpell [u32 spell]
OP_SPELL_REMOVED = 0x004D   # Net_RemoveSpell [u32 spell]
OP_VENDOR_INFO = 0x0062       # [vendor][profile 24][u32 n][ItemProfile]*n
OP_ENCHANTMENT = 0x004E       # add/refresh one enchantment [Enchantment]
OP_ENCHANTMENT_REMOVED = 0x004F  # [u32 id]
OP_ENCHANTMENTS = 0x01A5      # add/refresh a list [u32 n][Enchantment]*n
OP_ENCHANTMENTS_EXPIRED = 0x01A6  # [u32 n][u32 id]*n (the client prints "... has expired")
OP_ENCHANTMENTS_DISPELLED = 0x01AE  # same, silently
CHARLIST_TAIL = 28  # holds the account name: the key the client and server hash for spell tapers

# client -> server
OP_GAME_ACTION = 0xF7B1
OP_AUTONOMOUS_POSITION = 0xF753  # client game action: [Position 32][timestamps]... its own position
OP_UPDATE_POSITION = 0xF748      # server: [guid][flags][cell][x y z][quaternion parts unless flagged]...
OP_LOGOFF = 0xF653
ACT_CAST_UNTARGETED = 0x48
ACT_CAST_TARGETED = 0x4A
ACT_USE_WITH_TARGET = 0x35
ACT_USE = 0x36
ACT_APPRAISE = 0xC8
ACT_SPELLBAR_ADD, ACT_SPELLBAR_REMOVE = 0x1E3, 0x1E4   # [spell][slot] / [spell]: the one 1999 spell bar
ACT_CHANGE_STANCE = 0x53     # [u32 mode]: 1 peace, 2 melee, 3 missile, 4 magic (ClientEvents.cpp, CombatModeFromWire)

# 0x13 PlayerDescription base-table flags, in wire order
_BASE_TABLES = ((0x1, "int"), (0x80, "int64"), (0x2, "bool"), (0x4, "float"),
                (0x10, "string"), (0x8, "did"), (0x40, "iid"), (0x20, "position"))

# PhysicsDesc optional fields after position, in wire order: (bit, fixed size or "list")
_PHYS_FIELDS = ((0x2, 4), (0x800, 4), (0x1000, 4), (0x1, 4), (0x20, 8), (0x40, "list"), (0x80, 4),
                (0x100, 4), (0x200, 4), (0x40000, 4), (0x4, 12), (0x8, 12), (0x10, 12), (0x2000, 4), (0x4000, 4))

# PublicWeenieDesc optional fields, in wire order: (bit, name or None, size or "str")
_PWD_FIELDS = ((0x1, "plural", "str"), (0x2, "items_capacity", 1), (0x4, None, 1), (0x8, "value", 4), (0x10, None, 4),
               (0x20, None, "f32"), (0x80000, None, 4), (0x80, None, 4), (0x100, None, 1), (0x200, None, 1),
               (0x400, None, 2), (0x800, None, 2), (0x1000, "stack", 2), (0x2000, "max_stack", 2),
               (0x4000, "container", 4), (0x8000, "wielder", 4), (0x10000, "valid_locations", 4), (0x20000, "location", 4),
               (0x40000, None, 4), (0x100000, None, 1), (0x800000, None, 1), (0x200000, None, 2),
               (0x400000, "spell", 2))


UNSET = 0xFFFFFFFF   # an appraisal section's "not set" member


def _acstring(r):
    """The 1999 AcString: u16 length counting its NUL (0: empty, two bytes, no align), the bytes, then the cursor
    aligned to 4 from the message's start (PacketCodec::WriteAcString)."""
    n = r.u16()
    if not n:
        return ""
    raw = r.bytes(n)
    r.align()
    return raw.split(b"\x00", 1)[0].decode("latin-1")


def _appraisal_profile(r, flags):
    """An item's appraisal: the 1999 AppraisalProfile's sections, in the order the server writes them
    (Appraisal.cpp AppraisalProfile::Pack). A creature or player (0x1) is only marked, not read."""
    p = {}
    if flags & 0x1:
        p["creature"] = True
        return p
    opt = lambda v: None if v == UNSET else v
    if flags & 0x2:     # weapon: damage type bits, speed, skill, max damage, then variance, length, velocity, defense, offense
        dtype, speed, skill, damage = r.u32(), r.i32(), r.u32(), r.i32()
        variance, _length, velocity, defense, offense = (r.f64() for _ in range(5))
        p["weapon"] = {"damage_type": dtype, "speed": speed, "skill": skill, "damage": damage, "variance": variance,
                       "velocity": velocity, "defense": defense, "offense": offense}
    if flags & 0x4:     # armor: its protection against slash, pierce, bludgeon, cold, fire, acid, electric
        p["protection"] = [r.f32() for _ in range(7)]
    if flags & 0x8:     # [mass (never set)] [value] [burden]
        r.u32()
        p["value"], p["burden"] = opt(r.u32()), opt(r.u32())
    if flags & 0x200:
        p["lockpick_percent"] = r.i32()
    if flags & 0x800:
        p["locked"] = bool(r.u32())
    if flags & 0x2000:
        p["boost_type"] = r.u32()   # 2 health, 4 stamina, 6 mana
    if flags & 0x1000:
        p["boost"] = r.i32()
    if flags & 0x8000:
        p["armor_level"] = r.i32()
    if flags & 0x40:
        p["resist_lockpick"] = r.i32()
    if flags & 0x4000000:
        p["crafter"] = _acstring(r)
    if flags & 0x10:
        p["inscription"], p["scribe"], _account = _acstring(r), _acstring(r), _acstring(r)
    if flags & 0x400:
        p["use"] = _acstring(r)
    if flags & 0x20000:
        p["description"] = _acstring(r)
    if flags & 0x40000:
        p["short_description"] = _acstring(r)
    if flags & 0x10000:
        lo, hi = r.u32(), r.u32()
        p["min_level"], p["max_level"] = opt(lo), opt(hi)
        p["destination"] = _acstring(r).strip()
    if flags & 0x80000:
        p["uses"], p["max_uses"] = opt(r.u32()), opt(r.u32())
    for bit in (0x100000, 0x200000, 0x400000):    # which lines are enchanted (the panel's colours)
        if flags & bit:
            r.u32()
    if flags & 0x800000:   # magic: difficulty, spellcraft, rank limit, mana, max mana, 2 unused, heritage, skill limit, spells
        p["difficulty"], p["spellcraft"], p["rank_limit"] = opt(r.u32()), opt(r.u32()), opt(r.u32())
        p["mana"], p["max_mana"] = opt(r.u32()), opt(r.u32())
        r.u32(), r.u32()
        p["heritage"] = _acstring(r)
        p["skill_limit"], p["skill_limit_type"] = opt(r.u32()), r.u32()
        p["spells"] = [s for s in _acstring(r).split(", ") if s]
        _acstring(r)
        r.f64()
    return p


def decode(direction, group, data):
    """Return a list of event dicts for one reassembled message; unknown opcodes give []."""
    r = Reader(data)
    try:
        op = r.u32()
        if direction == "c2s":
            return _client(op, r)
        return _server(op, r, wrapped=False)
    except ReadError as e:
        log.debug("short message %s op=0x%X: %s", direction, data[:4].hex(), e)
        return [{"type": "decode_error", "dir": direction, "op": int.from_bytes(data[:4], "little"), "error": str(e)}]


def _position(r):
    """Position: [cell][x y z][w x y z]."""
    cell = r.u32()
    x, y, z = r.f32(), r.f32(), r.f32()
    qw, qx, qy, qz = r.f32(), r.f32(), r.f32(), r.f32()
    return {"cell": cell, "x": x, "y": y, "z": z, "qw": qw, "qz": qz}


def _client(op, r):
    if op == OP_LOGOFF:
        return [{"type": "logoff"}]  # back to character select (the client repeats it many times)
    if op != OP_GAME_ACTION:
        return []
    aseq = r.u32()  # the client's action counter; the server only logs it
    act = r.u32()
    if act == OP_AUTONOMOUS_POSITION:  # the client reporting where it is, several times a second while moving
        return [dict(_position(r), type="position", object=None, source="client")]
    if act == ACT_CAST_UNTARGETED:
        return [{"type": "cast", "spell": r.u32(), "target": None, "aseq": aseq}]
    if act == ACT_CHANGE_STANCE:
        return [{"type": "stance_request", "mode": r.u32(), "aseq": aseq}]
    if act == ACT_CAST_TARGETED:
        target = r.u32()
        return [{"type": "cast", "spell": r.u32(), "target": target, "aseq": aseq}]
    if act == ACT_USE_WITH_TARGET:
        return [{"type": "use_with", "source": r.u32(), "target": r.u32(), "aseq": aseq}]
    if act == ACT_USE:
        return [{"type": "use", "object": r.u32(), "aseq": aseq}]
    if act == ACT_APPRAISE:
        return [{"type": "appraise", "object": r.u32(), "aseq": aseq}]
    if act == ACT_SPELLBAR_ADD:
        spell = r.u32()
        return [{"type": "spellbar_add", "spell": spell, "index": r.i32()}]
    if act == ACT_SPELLBAR_REMOVE:
        return [{"type": "spellbar_remove", "spell": r.u32()}]
    if act in (ACT_MELEE_ATTACK, ACT_MISSILE_ATTACK):
        return [{"type": "attack", "target": r.u32(), "height": r.u32(), "missile": act == ACT_MISSILE_ATTACK}]
    if act == ACT_CHANNEL_SEND:   # the server doesn't echo your own group chat back: log it as it goes out
        channel = r.u32()
        return [{"type": "channel", "channel": CHANNELS.get(channel, f"channel {channel:#x}"), "sender": None,
                 "text": r.string16(), "mine": True}]
    return []


def _server(op, r, wrapped):
    if op == OP_EVENT and not wrapped:
        _guid = r.u32()
        _seq = r.u32()
        return _server(r.u32(), r, wrapped=True)
    if op == OP_UPDATE_INT:
        return [{"type": "int", "object": r.u32(), "key": r.u32(), "value": r.i32()}]
    if op == OP_UPDATE_ATTRIBUTE:
        obj, key = r.u32(), r.u32()
        value, cp = _attribute(r)
        return [{"type": "attribute", "object": obj, "key": key, "value": value, "cp": cp}]
    if op == OP_UPDATE_VITAL:
        obj, key = r.u32(), r.u32()
        return [dict(type="vital", object=obj, key=key, **_vital(r))]
    if op == OP_UPDATE_SKILL:
        obj = r.u32()
        skill = r.u32()
        return [dict(type="skill", object=obj, skill=skill, **_skill_record(r))]
    if op == OP_TEXT:
        text = r.string16()
        return [{"type": "text", "text": text, "channel": r.i32()}]
    if op == OP_SPEECH and not wrapped:
        text, sender = r.string16(), r.string16()
        return [{"type": "speech", "text": text, "sender": sender, "sender_id": r.u32()}]
    if op == OP_CHANNEL:
        channel = r.u32()
        sender = r.string16()
        return [{"type": "channel", "channel": CHANNELS.get(channel, f"channel {channel:#x}"), "sender": sender,
                 "text": r.string16(), "mine": False}]
    if op == OP_YOU_HIT and wrapped:   # [name][damage type][f64 the share of its max health it took][damage][crit][conditions]
        foe = r.string16()
        ev = {"type": "combat", "foe": foe, "you_hit": True}
        try:
            r.u32()
            ev["share"], ev["damage"] = r.f64(), r.u32()
        except ReadError:
            pass
        return [ev]
    if op in (OP_HIT_YOU, OP_YOU_MISSED) and wrapped:
        return [{"type": "combat", "foe": r.string16(), "you_hit": op != OP_HIT_YOU}]
    if op == OP_TELL and wrapped:
        text, sender = r.string16(), r.string16()
        return [{"type": "tell", "text": text, "sender": sender, "sender_id": r.u32()}]
    if op == OP_WEENIE_ERROR:
        return [{"type": "error", "code": r.i32()}]
    if op == OP_WEENIE_ERROR_STRING and wrapped:
        return [{"type": "error", "code": r.i32(), "text": r.string16()}]
    if op == OP_APPRAISAL:
        obj = r.u32()
        flags = r.u32()   # AppraisalProfile's sections: 0x800000 = the magic block, sent for an item with spells
        # (failed or not: a failure draws "Casts the following spells: Unknown"; ObjectMsgs.cpp IdentifyObjectFail)
        ev = {"type": "appraisal", "object": obj, "success": r.i32() != 0, "magic": bool(flags & 0x800000)}
        try:
            ev["profile"] = _appraisal_profile(r, flags)
        except ReadError:
            pass                      # the header's still good: what's below it just isn't described
        return [ev]
    if op == OP_USE_DONE:
        return [{"type": "use_done", "code": r.i32() if r.remaining() >= 4 else 0}]
    if op == OP_HEALTH and wrapped:   # your selected / attacked target's health (ObjectMsgs.cpp HealthUpdate)
        return [{"type": "health", "object": r.u32(), "fraction": r.f32()}]
    if op == OP_DEATH:                # a creature or player died nearby: [text][victim][killer]
        text = r.string16()
        return [{"type": "death", "text": text, "object": r.u32(), "killer": r.u32()}]
    if op == OP_PLAYER_CREATE:
        return [{"type": "player", "object": r.u32()}]
    if op == OP_STACK_SIZE:
        return [{"type": "stack", "object": r.u32(), "stack": r.u32(), "value": r.u32()}]
    if op == OP_REMOVE_FROM_CONTAINER:
        return [{"type": "removed", "object": r.u32()}]
    if op == OP_DELETE_OBJECT:
        return [{"type": "deleted", "object": r.u32()}]
    if op == OP_INVENTORY_PUT:      # [item][container][slot][1 = a pack, 0 = an item] (older captures: the first two)
        ev = {"type": "moved", "object": r.u32(), "container": r.u32()}
        if r.remaining() >= 8:
            ev["slot"], ev["pack"] = r.u32(), bool(r.u32())
        return [ev]
    if op == OP_INVENTORY_FAILED:
        return [{"type": "inventory_failed", "object": r.u32(), "code": r.u32()}]
    if op == OP_VIEW_CONTENTS:      # a container's contents, in order: [container][n][iid, 0 item / 1 pack]*n
        container, n = r.u32(), r.u32()
        entries = [(r.u32(), r.u32()) for _ in range(n)]
        return [{"type": "contents", "container": container, "items": [i for i, kind in entries if kind == 0],
                 "packs": [i for i, kind in entries if kind != 0]}]
    if op == OP_INVENTORY_EQUIP:
        return [{"type": "equipped", "object": r.u32(), "location": r.u32()}]
    if op == OP_SPELL_LEARNED:
        return [{"type": "spell_learned", "spell": r.u32()}]
    if op == OP_SPELL_REMOVED:
        return [{"type": "spell_removed", "spell": r.u32()}]
    if op == OP_ENCHANTMENT:
        return [{"type": "enchantments", "list": [_enchantment(r)]}]
    if op == OP_ENCHANTMENTS:
        return [{"type": "enchantments", "list": [_enchantment(r) for _ in range(r.u32())]}]
    if op == OP_ENCHANTMENT_REMOVED:
        return [{"type": "enchantments_removed", "ids": [r.u32()], "why": "removed"}]
    if op in (OP_ENCHANTMENTS_EXPIRED, OP_ENCHANTMENTS_DISPELLED):
        ids = [r.u32() for _ in range(r.u32())]
        return [{"type": "enchantments_removed", "ids": ids,
                 "why": "expired" if op == OP_ENCHANTMENTS_EXPIRED else "dispelled"}]
    if op == OP_UPDATE_POSITION:
        guid, flags, cell = r.u32(), r.u32(), r.u32()
        x, y, z = r.f32(), r.f32(), r.f32()
        q = {}
        for bit, name in ((0x08, "qw"), (0x10, "qx"), (0x20, "qy"), (0x40, "qz")):  # a set bit means "zero, not sent"
            q[name] = 0.0 if flags & bit else r.f32()
        return [{"type": "position", "object": guid, "source": "server", "cell": cell, "x": x, "y": y, "z": z,
                 "qw": q["qw"], "qz": q["qz"]}]
    if op == OP_CREATE_OBJECT:
        return [_create_object(r)]
    if op == OP_VENDOR_INFO:
        return [_vendor_info(r)]
    if op == OP_CHARACTER_LIST:
        if r.end - r.pos < CHARLIST_TAIL:
            return []
        # [u32 0][20-byte NUL-padded account name][u32 0] (PacketCodec::WriteCharListTail); the
        # server hashes the same 19-char-max copy for tapers (CClient::GetCharListAccountName).
        tail = r.data[r.end - CHARLIST_TAIL:r.end]
        return [{"type": "account", "account": tail[4:24].split(b"\x00", 1)[0].decode("latin-1")}]
    if op == OP_PLAYER_DESCRIPTION:
        return [_player_description(r)]
    return []


def _enchantment(r):
    """Enchantment::Pack. Times are relative to when the server sent it: start is how long ago it began
    (negative), duration -1 means permanent. The id is spell | layer << 16."""
    eid = r.u32()
    out = {"id": eid, "spell": eid & 0xFFFF, "layer": eid >> 16, "category": r.u32(), "power": r.i32(),
           "start": r.f64(), "duration": r.f64(), "caster": r.u32()}
    r.f32()  # degrade modifier
    r.f32()  # degrade limit
    r.f64()  # last degraded
    out["stat"] = {"type": r.u32(), "key": r.u32(), "value": r.f32()}
    return out


def _attribute(r):
    """1999 wire attribute: field0 is the displayed (current) value; init is folded into it. (value, the wire's XP
    spent: the server picks it so the client's XP-to-raise comes out right -- goals.attribute_raises reads the
    raises and real XP back from it)."""
    value = r.u32()
    r.u32()  # init (0 on the wire)
    return value, r.u32()


def _vital(r):
    """Secondary attribute: raises and init add to the attribute-derived base; then the current value."""
    raises, init, cp = r.u32(), r.u32(), r.u32()
    return {"raises": raises, "init": init, "cp": cp, "current": r.u32()}


def _skill_record(r):
    level = r.u32() & 0xFFFF
    sac = r.i32()
    pp = r.u32()
    init_level = r.u32()
    resistance = r.i32()
    since = r.f64()
    return {"level_from_pp": level, "sac": sac, "pp": pp, "init_level": init_level,
            "resistance": resistance, "since_last_grant": since}


def _create_object(r):
    guid = r.u32()
    # ObjDesc
    r.u8()  # 0x11
    n_sub, n_tex, n_anim = r.u8(), r.u8(), r.u8()
    if n_sub:
        r.skip(2 + n_sub * 4)
    r.skip(n_tex * 5 + n_anim * 3)
    r.align()
    # PhysicsDesc
    bits = r.u32()
    r.u32()  # state
    if bits & 0x10000:
        r.skip(r.u32())
        r.u32()
    elif bits & 0x20000:
        r.u32()
    position = _position(r) if bits & 0x8000 else None
    for bit, size in _PHYS_FIELDS:
        if bits & bit:
            r.skip(r.u32() * 8 if size == "list" else size)
    r.skip(18)  # 9 x u16 timestamps
    r.align()
    obj = dict(_weenie_desc(r), type="object", object=guid)
    if position:
        obj["position"] = position
    return obj


def _weenie_desc(r):
    """OldPublicWeenieDesc: the leading header dword says which optional fields follow; the dword after
    the item type is the object-description bitfield (inscribable, stuck, ...), not a presence mask."""
    bits = r.u32()
    obj = {"name": r.string16(), "wcid": r.u16()}
    r.u16()  # icon
    obj["item_type"] = r.u32()
    r.u32()  # object description flags
    for bit, name, size in _PWD_FIELDS:
        if not bits & bit:
            continue
        if size == "str":
            value = r.string16()
        elif size == "f32":
            value = r.f32()
        else:
            value = {1: r.u8, 2: r.u16, 4: r.u32}[size]()
        if name:
            obj[name] = value
    r.align()
    return obj


def _vendor_info(r):
    """The vendor's stock, sent when its window opens and after every sale (Vendor.cpp SendVendorInventory)."""
    vendor = r.u32()
    r.u32()  # item types it buys
    r.i32()  # min value
    r.i32()  # max value
    r.i32()  # magic
    buy_price, sell_price = r.f32(), r.f32()  # what it pays you / what it charges, as fractions of value
    items = []
    for _ in range(r.u32()):
        packed, iid = r.u32(), r.u32()
        amount = packed & 0xFFFFFF
        item = {"iid": iid, "amount": None if amount == 0xFFFFFF else amount}  # 0xFFFFFF: unlimited (-1)
        if packed >> 24:
            item.update(_weenie_desc(r))
        items.append(item)
    return {"type": "vendor", "vendor": vendor, "buy_price": buy_price, "sell_price": sell_price, "items": items}


def _player_description(r):
    out = {"type": "player_description", "ints": {}, "strings": {}, "skills": {}}
    base_flags = r.u32()
    r.i32()  # weenie type
    for bit, kind in _BASE_TABLES:
        if not base_flags & bit:
            continue
        count = r.u16()
        r.u16()  # buckets
        for _ in range(count):
            key = r.u32()
            if kind == "int":
                out["ints"][key] = r.i32()
            elif kind == "int64":
                r.u64()
            elif kind == "bool":
                r.u32()
            elif kind == "float":
                r.f64()
            elif kind == "string":
                out["strings"][key] = r.string16()
            elif kind in ("did", "iid"):
                r.u32()
            else:
                r.skip(32)
    ac_flags = r.u32()
    r.u32()  # wcid
    if ac_flags & 0x1:
        mask = r.u32()
        out["attributes"], out["attribute_cp"], out["vitals"] = {}, {}, {}
        for key, bit in enumerate((0x1, 0x2, 0x4, 0x8, 0x10, 0x20), start=1):  # Str End Quick Coord Focus Self
            if mask & bit:
                out["attributes"][key], out["attribute_cp"][key] = _attribute(r)
        for key, bit in ((2, 0x40), (4, 0x80), (6, 0x100)):  # Health Stamina Mana
            if mask & bit:
                out["vitals"][key] = _vital(r)
    if ac_flags & 0x2:
        count = r.u16()
        r.u16()
        for _ in range(count):
            skill = r.u32()
            out["skills"][skill] = _skill_record(r)
    # 0x4 (body) is stripped from the player's own description
    if ac_flags & 0x100:
        out["spells"] = {}
        count = r.u16()
        r.u16()
        for _ in range(count):
            spell = r.u32()
            r.f32()  # charge
            r.f32()
            out["spells"][spell] = r.f32()  # casting likelihood
    if ac_flags & 0x200:  # enchantment registry: header 0x1 multiplicative, 0x2 additive, 0x4 vitae
        reg = r.u32()
        enchantments = []
        for bit in (0x1, 0x2):
            if reg & bit:
                enchantments += [_enchantment(r) for _ in range(r.u32())]
        if reg & 0x4:
            enchantments.append(_enchantment(r))
        out["enchantments"] = enchantments
    # then the PlayerModule (options, hotbar, spell bar) and the main pack's contents in order: items, then
    # side packs. Read when it's all there (ObjectMsgs.cpp LoginCharacter); anything short leaves it out.
    try:
        selector, _options = r.u32(), r.u32()
        if selector & 1:                         # hotbar: [n][index, object, spell]*n
            for _ in range(r.u32() * 3):
                r.u32()
        out["spellbar"] = [r.u32() for _ in range(r.u32())]   # spell bar: [n][spell]*n, in bar order
        entries = [(r.u32(), r.u32()) for _ in range(r.u32())]
        out["inventory"] = {"items": [i for i, kind in entries if kind == 0], "packs": [i for i, kind in entries if kind != 0]}
    except Exception:
        pass
    return out
