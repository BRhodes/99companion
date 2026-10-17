"""Spell research: this account's formula for every researchable spell, and what learning it takes.

In 1999 a spell is learned by casting its formula from the research panel (game action 0x4B, the 8
component ids in slot order plus a target). The server looks the submitted formula up among every
researchable spell's formula personalised for the account (InqSpellFormula + RandomizeVersion1),
then casts the match for real: components are reserved and may burn, mana is spent, the normal skill
roll decides (1 - 1/(1 + e^(0.07 (skill - power)))), and a cast that lands teaches the spell
(ClientEvents.cpp ResearchSpell, SpellcastingManager.cpp BeginResearchCast/ResolveResearchCast).
"""
import math
from collections import Counter

from .burn import personal_formula

NOT_RESEARCHABLE = 0x80
SELF_TARGETED = 0x8          # SpellIndex flags from the spell table
BENEFICIAL = 0x4
# What a spell can be cast on, from the last of its formula's target components (slots 6-8):
# SpellComponentTable::GetTargetTypeFromComponentID. Anything else has no target type.
CREATURE_TARGET_COMPS = {0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38, 0x3A, 0x3C, 0x3D, 0x3E}
ITEM_TARGET_COMP, PORTAL_TARGET_COMP = 0x39, 0x3B
ITEM_ENCHANTABLE = 0xA090F   # ItemType TYPE_ITEM_ENCHANTABLE_TARGET: what an item spell can land on
MAX_REACH = 75.0             # metres: no spell reaches further (the radar's radius)
SCHOOL_SKILL = {1: 34, 2: 33, 3: 32, 4: 31}          # War, Life, Item, Creature (InqSkillForSpell)
DEFAULT_SKILL_FORMULAS = {31: (0, 1, 1, 4, 5, 6), 32: (0, 1, 1, 4, 5, 6), 33: (0, 1, 1, 4, 5, 6), 34: (0, 1, 1, 4, 5, 6)}
VITAE_SPELL = 666
ATTRIBUTE_MOD, VITAL_MOD, SKILL_MOD, MULTIPLY, ADD = 0x1, 0x2, 0x10, 0x4000, 0x8000
SCARAB_LEVELS = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 0x6E: 6, 0x70: 7, 0xC0: 7, 0xC1: 8}  # DeterminePowerLevelOfComponent


def success_chance(skill, power):
    """GetMagicSkillChance (SkillChecks.cpp)."""
    if skill is None:
        return None
    return max(0.0, min(1.0, 1.0 - 1.0 / (1.0 + math.exp(0.07 * (skill - power)))))


def _enchant(value, enchantments):
    for e in enchantments:
        t, v = e["stat"]["type"], e["stat"]["value"]
        if t & ADD:
            value += v
        elif t & MULTIPLY:
            value *= v
    return value


def active_enchantments(enchantments):
    """In each spell category only the strongest enchantment applies (ties: the newest), like the
    server's Duel; vitae always applies."""
    best = {}
    for e in enchantments:
        if e["spell_id"] == VITAE_SPELL:
            continue
        w = best.get(e["category"])
        if w is None or (e["power"], e["started"]) > (w["power"], w["started"]):
            best[e["category"]] = e
    return list(best.values())


def effective_attribute(base, key, active):
    """CEnchantmentRegistry::EnchantAttribute: matching attribute buffs, floored at 10 (1 under 10)."""
    affecting = [e for e in active if e["stat"]["type"] & ATTRIBUTE_MOD and e["stat"]["key"] == key]
    value = _enchant(float(base), affecting)
    return max(value, 1.0 if base < 10 else 10.0)


def vital_enchant(value, max_key, active):
    """CEnchantmentRegistry::EnchantAttribute2nd's spell part: buffs on a max vital (type 0x2, key 1/3/5),
    floored at 5 (1 under 5)."""
    base = value
    value = _enchant(value, [e for e in active if e["stat"]["type"] & VITAL_MOD and e["stat"]["key"] == max_key])
    return max(value, 1.0 if base < 5 else 5.0)


def effective_skill(skill_id, skills, attributes, enchantments, formulas, min_sac=2):
    """CACQualities::InqSkill: round((w + x*a1 + y*a2) / z) on buffed attributes + training + raises,
    then vitae and skill buffs, rounded. None when the skill isn't known (or is trained below min_sac:
    untrained ones, sac 1, only when asked for)."""
    st = skills.get(skill_id)
    f = formulas.get(skill_id)
    if not st or not f or st.get("sac", 0) < min_sac:
        return None
    w, x, y, z, a1, a2 = f
    active = active_enchantments(enchantments)
    v1 = effective_attribute(attributes.get(a1, 0), a1, active) if a1 else 0
    v2 = effective_attribute(attributes.get(a2, 0), a2, active) if a2 else 0
    value = math.floor((w + x * v1 + y * v2) / z + 0.5) if z else 0
    value += st.get("level", 0)
    value = float(value)
    for e in enchantments:
        if e["spell_id"] == VITAE_SPELL:
            value *= e["stat"]["value"]
    value = _enchant(value, [e for e in active if e["stat"]["type"] & SKILL_MOD and e["stat"]["key"] == skill_id])
    return 0 if value <= 0.5 else int(value + 0.5)


def spell_level(spell, comps):
    level = SCARAB_LEVELS.get(comps[0]) if comps else None
    if level:
        return level
    roman = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8}
    words = spell.get("name", "").split()
    return roman.get(words[-1]) if words else None


def target_type(spell):
    """"creature", "item", "portal" or None (no type check): SpellFormula::GetTargetingType on the spell's
    own formula, the last component among slots 6-8."""
    comps = list(spell.get("formula") or [])[:8]
    comps += [0] * (8 - len(comps))
    i = 5
    while i < 8 and comps[i]:
        i += 1
    last = comps[i - 1]
    return ("creature" if last in CREATURE_TARGET_COMPS else "item" if last == ITEM_TARGET_COMP
            else "portal" if last == PORTAL_TARGET_COMP else None)


def reach(spell, skill):
    """How far the spell reaches at this school skill (base + per skill point, capped), or None if unknown."""
    if not spell.get("range"):
        return None
    base, per = spell["range"]
    return min(MAX_REACH, base + per * (skill or 0))


def target_kind(spell):
    """"self" for spells cast on yourself (the SelfTargeted flag: Self buffs, recalls), else "other"."""
    return "self" if spell.get("bitfield", 0) & SELF_TARGETED else "other"


def research_table(spells, components, account, known, carried, skills_by_school):
    """One row per researchable spell: its formula for this account (8 slots), what's missing from the
    pack, the success chance at the current school skill, and whether the dashboard can learn it."""
    rows = []
    for sid, sp in spells.items():
        base = sp.get("formula")
        if not base or sp.get("bitfield", 0) & NOT_RESEARCHABLE or not sp.get("power"):
            continue
        comps = personal_formula(base, account) if account else list(base)
        need = Counter(c for c in comps if c)
        names = {c: components.get(c, {}).get("name", f"#{c}") for c in need}
        missing = {names[c]: n - carried.get(names[c], 0) for c, n in need.items() if carried.get(names[c], 0) < n}
        skill = skills_by_school.get(sp.get("school"))
        target = target_kind(sp)
        is_known = sid in known
        # a spell cast on something else (a bane on armor, a weapon spell, a debuff) is researched at that
        # target, so it needs one picked: the Buffs page knows which item a bane or weapon spell goes on
        reason = ("already known" if is_known else
                  "missing components" if missing else
                  "not trained in this school" if skill is None else None)
        rows.append({
            "id": sid, "name": sp.get("name"), "school": sp.get("school_name"), "level": spell_level(sp, comps),
            "power": sp.get("power"), "mana": sp.get("mana"), "known": is_known, "target": target,
            "icon": f"{sp['icon']:08x}" if sp.get("icon") else None,
            "formula": [names[c] for c in comps if c], "slots": comps, "missing": missing,
            "skill": skill, "chance": success_chance(skill, sp.get("power", 0)),
            "personalised": bool(account), "can_learn": reason is None, "reason": reason,
            "needs_target": target != "self",
            "target_type": target_type(sp), "beneficial": bool(sp.get("bitfield", 0) & BENEFICIAL),
        })
    rows.sort(key=lambda r: (r["school"] or "", r["level"] or 0, r["name"] or ""))
    return rows
