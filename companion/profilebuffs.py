"""Your buff profile's tiers: which tier of a kept buff you'd cast, and what an item buff goes on.

The profile (prefs "buff_profiles": {name, buffs: [{section, name, category, level, spell}], max_chance,
bane_targets}) names each buff to keep and the level to keep it at: a tier, "relax" (the highest tier you
know that lands RELAX_CHANCE of the time) or "max" (the highest at the profile's max_chance).
"""

RELAX_CHANCE = 0.9
ROMAN = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII"]


def tier_of(name):
    word = str(name or "").split(" ")[-1]
    return ROMAN.index(word) if word in ROMAN[1:] else None


def _castable(o, ignore_missing=False):
    return o.get("known") and (ignore_missing or not o.get("missing"))


def _chance(tr, skills, spell_id):
    from . import research
    sp = tr.spells.get(spell_id, {})
    skill = skills.get(sp.get("school"))
    return None if skill is None else research.success_chance(skill, sp.get("power", 0))


def resolve(tr, skills, row, level, max_chance, ignore_missing=False):
    """The option a kept buff resolves to (the best castable one at or below its level), or None. skills: {spell
    school: skill} to judge Relax / Max by. ignore_missing: count tiers whose components you're out of."""
    options = row["options"]
    if level in ("relax", "max"):
        known = [o for o in options if _castable(o, ignore_missing)]
        floor = RELAX_CHANCE if level == "relax" else (max_chance or 0.3)
        ok = [o for o in known if (_chance(tr, skills, o["spell"]) or 0) >= floor]
        return (ok or known[:1] or [None])[-1]
    if not isinstance(level, int):
        return None
    return ([o for o in options if _castable(o, ignore_missing) and o["level"] <= level] or [None])[-1]


def gear_for(tr, prefs, slot, targets):
    """The items a bane or weapon spell of this slot goes on: the weapons / shields picked on the config page
    you still carry, else what you wear in that slot."""
    picked = ((prefs or {}).get("items") or {}).get("weapons" if slot == "weapon" else "shields") or []
    by_id = {t["id"]: t for t in targets}
    if slot in ("weapon", "shield") and picked:
        have = [by_id[x["id"]] for x in picked if x.get("id") in by_id]
        if have:
            return have
    return [t for t in targets if t["worn"] and t["slot"] == slot]
