"""The dashboard's fixed buff layout: attributes, skills, protections and armor banes.

Buffs on you come from the server (0x4E etc., with real durations). Banes and Impenetrability live on
your armor, whose enchantments the server never sends you, so those rows are ESTIMATES from your own
casts: each item spell you cast that lands ("You cast ...") is remembered per item and stat, with an
expiry of spell duration x the duration factor seen on your own buffs (spell economy and per-spell
charge scale durations 0.75-1.25x; SpellPotency.cpp).
"""
import json
from pathlib import Path

from . import research
from . import skills as sk

ATTRIBUTES = [(1, "Strength"), (2, "Endurance"), (3, "Quickness"), (4, "Coordination"), (5, "Focus"), (6, "Self")]
CARD_ATTRIBUTES = [ATTRIBUTES[i] for i in (0, 1, 3, 2, 4, 5)]   # the dashboard card: Coordination before Quickness
# armor, then physical (blade, bludgeon, pierce), then elements (acid, cold, fire, lightning)
PROTS = [(None, "Armor"), (64, "Slashing"), (66, "Bludgeoning"), (65, "Piercing"), (69, "Acid"), (68, "Cold"),
         (67, "Fire"), (70, "Lightning")]
BANES = [(28, "Impenetrability"), (13, "Blade"), (15, "Bludgeon"), (14, "Piercing"), (18, "Acid"), (16, "Frost"),
         (17, "Flame"), (19, "Lightning")]
REGEN = [(3, "Regeneration"), (4, "Rejuvenation"), (5, "Mana Renewal")]   # health / stamina / mana rate
WEAPON = [(44, "Blood Drinker"), (62, "Heart Seeker"), (49, "Swift Killer"), (29, "Defender")]
ITEM_KEYS = {k for k, _ in BANES} | {k for k, _ in WEAPON}   # stats item spells raise
WEAPON_TIME = 49           # Swift Killer lowers it: lower is better
# equipped-slot bits (EquipMask): what kind of item a spell landed on
ARMOR_SLOTS = 0x1 | 0x20 | 0x100 | 0x200 | 0x400 | 0x800 | 0x1000 | 0x2000 | 0x4000
CLOTHES_SLOTS = 0x2 | 0x4 | 0x8 | 0x10 | 0x40 | 0x80
SHIELD_SLOT = 0x200000
WEAPON_SLOTS = 0x100000 | 0x400000 | 0x1000000 | 0x2000000 | 0x4000000


WEAPON_TYPES = 0x1 | 0x100 | 0x8000     # ItemType: melee weapon, missile weapon, caster (wand, orb)


def item_kind(obj):
    """'weapon' or 'shield' for an item you might buff (from its type and where it can be worn), else None."""
    if (obj.get("item_type") or 0) & WEAPON_TYPES:
        return "weapon"
    if ((obj.get("valid_locations") or 0) | (obj.get("location") or 0)) & SHIELD_SLOT:
        return "shield"
    return None


def item_slot(location):
    """'armor', 'underclothes', 'shield', 'weapon' or None, from an item's equipped-slot bits."""
    loc = location or 0
    if loc & SHIELD_SLOT:
        return "shield"
    if loc & WEAPON_SLOTS:
        return "weapon"
    if loc & ARMOR_SLOTS:
        return "armor"
    if loc & CLOTHES_SLOTS:
        return "underclothes"
    return None
BODY_ARMOR = 0x80       # StatMod type bit for the Armor Self family
FLOAT, INT = 0x8, 0x4
_MODS = Path(__file__).parent / "data" / "spell_mods.json"


def load_mods(path=_MODS):
    try:
        return {int(k): v for k, v in json.loads(Path(path).read_text()).items()}
    except (OSError, ValueError):
        return {}


def harmful(stat):
    if stat["key"] == WEAPON_TIME and stat["type"] & INT and not stat["type"] & 0x11:
        return stat["value"] > 0      # Swift Killer: a lower weapon time is faster
    return _harmful(stat)


def _harmful(stat):
    """Whether an enchantment hurts you: a resistance multiplier above 1 (vulnerabilities take more damage),
    any other multiplier below 1, or a negative addition (Ineptitude, Feeblemind, Brittlemail...)."""
    t, k, v = stat["type"], stat["key"], stat["value"]
    if t & 0x4000:
        return v > 1 if 64 <= k <= 70 else v < 1
    return v < 0


def _row_spells(active, match, spells, now):
    """[{name, left, harmful}] of the active enchantments a row is made of, and the earliest expiry."""
    out = []
    for e in active:
        if match(e["stat"]):
            out.append({"name": spells.get(e["spell_id"], {}).get("name", f"Spell {e['spell_id']}"),
                        "left": None if e["expires"] is None else e["expires"] - now,
                        "value": e["stat"]["value"], "harmful": harmful(e["stat"])})
    soonest = min((s["left"] for s in out if s["left"] is not None), default=None)
    return out, soonest


ARMOR_SELF = 0xA080      # StatMod type of the Armor Self family
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7}
SELF_TARGETED = 0x8


def _level(name):
    return ROMAN.get(name.split()[-1]) if name else None


def choices(tr, mods, other=False):
    """The buff-profile editor's rows, laid out like the dashboard panel: each attribute, skill (specialized,
    trained, untrained), protection and bane, then any other buff you know, with the spell for each tier
    (I-VI) that raises it.

    [{section, name, category, options: [{level, spell, name, known}]}]. Your own buffs use the
    self-targeted spell of a tier; banes are item spells (cast on your armor). other=True: for someone else
    (a friend): the Other spell of each tier, and every skill (theirs aren't known)."""
    known = set(tr.known_spells)
    carried = tr.inventory_counts() if hasattr(tr, "inventory_counts") else {}

    def missing(sid):    # components a cast needs on hand that you don't carry
        burn = getattr(tr, "burn", None)
        need = burn.required(sid) if burn else {}
        return sorted(n for n, k in need.items() if carried.get(n, 0) < k)
    found = {}      # row key -> {level: (spell id, spell)}
    rows = [("Attributes", name, lambda m, k=key: m[0] & 0x1 and m[1] == k) for key, name in ATTRIBUTES]
    # skills by how far you've taken them: specialized, trained, then the untrained ones you can still use
    if other:
        for sid in sorted(sk.SKILL_NAMES, key=sk.skill_name):
            rows.append(("Skills", sk.skill_name(sid), lambda m, k=sid: m[0] & 0x10 and m[1] == k))
    for sac, section in (() if other else ((3, "Specialized"), (sk.TRAINED, "Trained"), (1, "Other skills"))):
        for sid, st in sorted(tr.skills.items(), key=lambda kv: sk.skill_name(kv[0])):
            if st.get("sac", 0) == sac:
                rows.append((section, sk.skill_name(sid), lambda m, k=sid: m[0] & 0x10 and m[1] == k))
    for key, name in PROTS:   # Armor Self is a body-armor enchantment (0xA080); Heal shares only the 0x80 bit
        rows.append(("Protections", name, (lambda m: m[0] & ARMOR_SELF == ARMOR_SELF) if key is None
                     else (lambda m, k=key: m[0] & FLOAT and m[1] == k)))
    for key, name in BANES:   # an item stat (float, or int for Impenetrability's armor), never a skill or attribute
        rows.append(("Banes", name, lambda m, k=key: not m[0] & 0x11 and m[0] & (INT if k == 28 else FLOAT)
                     and m[1] == k))
    for key, name in REGEN:   # vital regeneration rates (float multipliers on you)
        rows.append(("Regeneration", name, lambda m, k=key: not m[0] & 0x11 and m[0] & FLOAT and m[1] == k))
    for key, name in WEAPON:  # weapon stats: int damage / weapon time, float offense / defense
        rows.append(("Weapon", name, lambda m, k=key: not m[0] & 0x11 and m[0] & (INT if k in (44, 49) else FLOAT)
                     and m[1] == k))
    matched = set()
    for sid, spell in tr.spells.items():
        mod = mods.get(sid)
        bits = spell.get("bitfield", 0)
        level = _level(spell.get("name"))
        if not mod or not bits & 0x4 or not level or harmful({"type": mod[0], "key": mod[1], "value": mod[2]}):
            continue
        for section, name, match in rows:
            if not match(mod):
                continue
            item = section in ("Banes", "Weapon")
            self_spell = not item and not other     # yourself: the Self version; someone else: the Other one
            if self_spell != bool(bits & SELF_TARGETED):   # (banes, weapon spells: the item spell either way)
                continue
            slot = found.setdefault((section, name), {})
            have = slot.get(level)
            if have is None or (sid in known and have[0] not in known):
                slot[level] = (sid, spell)
            matched.add(spell.get("category"))
            break
    # anything else you know that helps you (regeneration, Sprint, ...): one row per kind (not for someone else)
    extra = {} if other else None
    other = {}
    for sid in (() if extra is not None else known):
        spell = tr.spells.get(sid, {})
        mod, bits, cat = mods.get(sid), spell.get("bitfield", 0), spell.get("category")
        if cat in matched or not bits & 0x4 or not bits & SELF_TARGETED or not _level(spell.get("name"))                 or not spell.get("duration") or spell["duration"] <= 0:     # instant spells aren't buffs to keep up
            continue
        if mod and harmful({"type": mod[0], "key": mod[1], "value": mod[2]}):
            continue
        family = " ".join(w for w in spell["name"].split()[:-1] if w != "Self")
        other.setdefault(family, {})[_level(spell["name"])] = (sid, spell)
    out = []
    for section, name, _ in rows:
        slot = found.get((section, name))
        if slot:
            out.append(_row(section, name, slot, known, missing))
    for family in sorted(other):
        out.append(_row("Other", family, other[family], known, missing))
    return out


def _row(section, name, slot, known, missing):
    options = [{"level": lv, "spell": sid, "name": sp.get("name"), "known": sid in known, "missing": missing(sid)}
               for lv, (sid, sp) in sorted(slot.items())]
    cats = [sp.get("category") for sid, sp in slot.values()]
    return {"section": section, "name": name, "category": max(set(cats), key=cats.count), "options": options}


def build(tr, now):
    enchantments = [e for e in tr.enchantments.values() if e["expires"] is None or e["expires"] > now]
    active = research.active_enchantments(enchantments)
    spells = tr.spells
    attributes = []
    for key, name in CARD_ATTRIBUTES:
        base = tr.attributes.get(key)
        rows, left = _row_spells(active, lambda s: s["type"] & 0x1 and s["key"] == key, spells, now)
        value = research.effective_attribute(base, key, active) if base is not None else None
        attributes.append({"name": name, "base": base, "value": None if value is None else int(value + 0.5),
                           "spells": rows, "left": left})
    skills = []     # every skill you can use: specialized and trained, then untrained (sac 1), by name
    usable = [(sid, st) for sid, st in tr.skills.items() if st.get("sac", 0) >= 1]
    for sid, st in sorted(usable, key=lambda kv: (kv[1].get("sac", 0) < sk.TRAINED, sk.skill_name(kv[0]))):
        base = research.effective_skill(sid, tr.skills, tr.attributes, [], tr.skill_formulas, min_sac=1)
        value = research.effective_skill(sid, tr.skills, tr.attributes, enchantments, tr.skill_formulas, min_sac=1)
        rows, left = _row_spells(active, lambda s: s["type"] & 0x10 and s["key"] == sid, spells, now)
        skills.append({"name": sk.skill_name(sid), "base": base, "value": value, "spells": rows, "left": left,
                       "specialized": st.get("sac") == 3, "trained": st.get("sac", 0) >= sk.TRAINED})
    prots = []
    for key, name in PROTS:
        if key is None:
            match = lambda s: s["type"] & BODY_ARMOR
        else:
            match = lambda s, k=key: s["type"] & FLOAT and s["key"] == k
        rows, left = _row_spells(active, match, spells, now)
        prots.append({"name": name, "spells": rows, "left": left})
    regen = []      # vital regeneration rates on you (float multipliers)
    for key, name in REGEN:
        rows, left = _row_spells(active, lambda s, k=key: not s["type"] & 0x11 and s["type"] & FLOAT and s["key"] == k,
                                 spells, now)
        regen.append({"name": name, "spells": rows, "left": left})
    def item_rows(keys):
        out = []
        for key, name in keys:
            pieces = [b for b in tr.item_enchants.values() if b["key"] == key and b["expires"] > now]
            slots = {}
            for p in pieces:
                slots[p.get("slot") or "other"] = slots.get(p.get("slot") or "other", 0) + 1
            out.append({"name": name, "pieces": len(pieces), "slots": slots, "on": sorted({p.get("target") for p in pieces if p.get("target")}),
                        "items": sorted({p["item"] for p in pieces}),
                        "spells": [{"name": n, "harmful": any(p["value"] < 0 for p in pieces if p["spell"] == n)}
                                   for n in sorted({p["spell"] for p in pieces})],
                        "left": min((p["expires"] - now for p in pieces), default=None)})
        return out
    return {"attributes": attributes, "skills": skills, "prots": prots, "banes": item_rows(BANES),
            "weapon": item_rows(WEAPON), "regen": regen, "duration_factor": tr.duration_factor()}


# war spells by the element they hit with (the Protections rows): bolt (one target), blast, volley
WAR = {"Slashing": ("Whirling Blade", "Blade Blast", "Blade Volley"), "Bludgeoning": ("Shock Wave", "Shock Blast", "Bludgeoning Volley"),
       "Piercing": ("Force Bolt", "Force Blast", "Force Volley"), "Acid": ("Acid Stream", "Acid Blast", "Acid Volley"),
       "Cold": ("Frost Bolt", "Frost Blast", "Frost Volley"), "Fire": ("Flame Bolt", "Flame Blast", "Flame Volley"),
       "Lightning": ("Lightning Bolt", "Lightning Blast", "Lightning Volley")}
WAR_FAMILY = {fam: (element, kind) for element, fams in WAR.items() for fam, kind in zip(fams, ("bolt", "blast", "volley"))}
# the Vitals rows (health, stamina, mana): regeneration up or down (regen / degen, by what the enchantment does to the
# rate), and the families that drain, infuse, restore or deplete each
VITAL_OF_RATE = {3: "Health", 4: "Stamina", 5: "Mana"}
VITAL_FAMILIES = {"Drain Health": ("Health", "drain"), "Infuse Health": ("Health", "infuse"), "Heal": ("Health", "restore"),
                  "Harm": ("Health", "deplete"), "Drain Stamina": ("Stamina", "drain"), "Infuse Stamina": ("Stamina", "infuse"),
                  "Revitalize": ("Stamina", "restore"), "Enfeeble": ("Stamina", "deplete"), "Drain Mana": ("Mana", "drain"),
                  "Infuse Mana": ("Mana", "infuse"), "Mana Boost": ("Mana", "restore"), "Mana Drain": ("Mana", "deplete")}
VITALS = ["Health", "Stamina", "Mana"]
# the rest, a line each: what they do it to, and which of them each family is
UTILITY = {"Hide Value": ("Value", "hide"), "True Value": ("Value", "true"), "Strengthen Lock": ("Locks", "strengthen"),
           "Weaken Lock": ("Locks", "weaken"), "Portal Recall": ("Portals", "recall"), "Lifestone Recall": ("Portals", "lifestone"),
           "Portal Tie": ("Portals", "tie"), "Summon Portal": ("Portals", "summon")}


def research_layout(spells, mods, rows, skills=None):
    """The research list laid out like the dashboard's Buffs card: [{section, name, variants}], each row a kind of
    spell (Strength, Acid Protection, Blood Drinker...) and its variants by direction and target, each the tiers
    there are of it to research: {"up_self" | "up_other" | "down_self" | "down_other" | "up" | "down" | "self" |
    "other": [{level, spell, name, known, chance}], lowest tier first}. Up: a helpful spell (the Beneficial flag),
    down: a harmful one (Resistable); neither (portals, Hide Value): no direction. Self: cast on yourself (the
    SelfTargeted flag), other: on someone or something else; banes and weapon spells go on an item, so they're just
    up or down. A spell that raises or lowers none of the card's stats (war bolts, portals, Heal...) goes under
    Other, one row per family (its name without the tier, Self or Other); a spell with no tier (Portal Recall) is
    its own row. War spells are rows of their own section, "War", one per element (the Protections rows' names), with
    "bolt", "blast" and "volley" for variants. Vitals: a row each for health, stamina and mana, its variants
    regen / degen / drain / infuse / restore / deplete + _self / _other; Conversions (Stamina to Mana...): a row per
    vital converted from, "to_health" / "to_stamina" / "to_mana" its variants; Utility: Value (hide, true), Locks
    (strengthen, weaken) and Portals (recall, lifestone, tie, summon1-3: Summon Portal's tiers each its own).
    rows: research_table's. skills ({skill id: {sac}}, yours): the Skills rows go specialized, trained,
    then the rest, each by name, every one with its "group"."""
    match_rows = [("Attributes", name, lambda m, k=key: m[0] & 0x1 and m[1] == k) for key, name in CARD_ATTRIBUTES]
    sac = {sid: (st or {}).get("sac", 0) for sid, st in (skills or {}).items()}
    group = {}
    for sid in sorted(sk.SKILL_NAMES, key=lambda s: (-min(sac.get(s, 0), 3) if sac.get(s, 0) >= sk.TRAINED else 0,
                                                     sk.skill_name(s))):
        group[sk.skill_name(sid)] = ("Specialized" if sac.get(sid, 0) >= 3 else "Trained" if sac.get(sid, 0) >= sk.TRAINED
                                     else "Other skills")
        match_rows.append(("Skills", sk.skill_name(sid), lambda m, k=sid: m[0] & 0x10 and m[1] == k))
    for key, name in PROTS:
        match_rows.append(("Protections", name, (lambda m: m[0] & ARMOR_SELF == ARMOR_SELF) if key is None
                           else (lambda m, k=key: m[0] & FLOAT and m[1] == k)))
    for key, name in BANES:
        match_rows.append(("Banes", name, lambda m, k=key: not m[0] & 0x11 and m[0] & (INT if k == 28 else FLOAT)
                           and m[1] == k))
    for key, name in WEAPON:
        match_rows.append(("Weapon", name, lambda m, k=key: not m[0] & 0x11 and m[0] & (INT if k in (44, 49) else FLOAT)
                           and m[1] == k))
    for key, name in REGEN:
        match_rows.append(("Vitals", VITAL_OF_RATE[key], lambda m, k=key: not m[0] & 0x11 and m[0] & FLOAT and m[1] == k))
    found, other = {}, {}
    for r in rows:
        spell = spells.get(r["id"]) or {}
        name = r.get("name") or spell.get("name") or f"Spell {r['id']}"
        level = _level(name) or 1
        bits, mod = spell.get("bitfield", 0), mods.get(r["id"])
        tier = {"level": level, "spell": r["id"], "name": name, "known": r.get("known"), "chance": r.get("chance")}
        if spell.get("meta_type") != 1:     # only enchantments raise or lower a stat (others' "mods" are junk)
            mod = None
        place = next(((s, n) for s, n, match in match_rows if mod and match(mod)), None)
        lowers = mod is not None and harmful({"type": mod[0], "key": mod[1], "value": mod[2]})
        direction = "up" if bits & 0x4 else "down" if bits & 0x1 else ("down" if lowers else "up") if mod else ""
        if place is None:   # Other: by family (war spells by element, harms and drains with regeneration)
            words = [w for w in name.split()[:-1 if _level(name) else None] if w not in ("Self", "Other")]
            family = " ".join(words) or name
            target = "self" if bits & SELF_TARGETED else "other"
            if family in WAR_FAMILY:
                element, kind = WAR_FAMILY[family]
                found.setdefault(("War", element), {}).setdefault(kind, {})[level] = tier
                continue
            if family in VITAL_FAMILIES:
                vital, kind = VITAL_FAMILIES[family]
                found.setdefault(("Vitals", vital), {}).setdefault(f"{kind}_{target}", {})[level] = tier
                continue
            if family in UTILITY:
                line, kind = UTILITY[family]
                if kind == "summon":       # each tier its own badge (Summon I, II, III)
                    kind = f"summon{level}"
                found.setdefault(("Utility", line), {}).setdefault(kind, {})[level] = tier
                continue
            source, _, to = family.partition(" to ")
            if source in VITALS and to in VITALS:     # a conversion: from one vital to another
                found.setdefault(("Conversions", source), {}).setdefault(f"to_{to.lower()}", {})[level] = tier
                continue
            place = ("Other", family)
            slots = other
        else:
            slots = found
        item = place[0] in ("Banes", "Weapon")
        if item:          # (item spells' flags don't say: what they do to the stat does)
            direction = "down" if lowers else "up"
        target = "" if item else "self" if bits & SELF_TARGETED else "other"
        if place[0] == "Vitals":       # a regeneration rate: up or down
            direction = "degen" if lowers else "regen"
        variant = "_".join(x for x in (direction, target) if x) or "up"
        slots.setdefault(place, {}).setdefault(variant, {})[level] = tier
    out = []
    for name in VITALS:                # (the conversions' rows, by the vital each converts from)
        match_rows.append(("Conversions", name, lambda m: False))
    for name in ("Value", "Locks", "Portals"):
        match_rows.append(("Utility", name, lambda m: False))
    for _, name in PROTS:
        if name in WAR:
            match_rows.append(("War", name, lambda m: False))
    for section, name, _ in match_rows:
        if (section, name) in found:
            v = found[(section, name)]
            out.append({"section": section, "name": name, "variants": {k: [t[l] for l in sorted(t)] for k, t in v.items()},
                        **({"group": group[name]} if section == "Skills" else {})})
    for (section, name), v in sorted(other.items(), key=lambda kv: kv[0][1]):
        out.append({"section": section, "name": name, "variants": {k: [t[l] for l in sorted(t)] for k, t in v.items()}})
    return out
