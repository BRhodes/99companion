"""An appraised item as one line of text, to paste into chat, a trade post or a note (messages.py decodes the
appraisal: _appraisal_profile)."""

import json
from pathlib import Path

from . import skills as sk

DAMAGE_TYPES = [(0x1, "Slash"), (0x2, "Pierce"), (0x4, "Bludge"), (0x8, "Cold"), (0x10, "Fire"), (0x20, "Acid"),
                (0x40, "Lightning")]
PROTECTIONS = ["S", "P", "B", "C", "F", "A", "L"]   # slash, pierce, bludgeon, cold, fire, acid, lightning (a long line)
BOOSTS = {2: "Health", 4: "Stamina", 6: "Mana"}
# spells' short names on the line: {"Impenetrability": "Impen"} makes "Impenetrability III" "Impen III" (add to it freely)
SPELL_SHORT = json.loads((Path(__file__).parent / "data" / "spell_short.json").read_text(encoding="utf-8"))
MIN_DRAIN = 0.0166      # mana a second: loot never drains slower (TreasureFactory.cpp, MANA_RATE_FLOAT)


def mana_hours(mana, spells, powers):
    """About how long `mana` lasts worn, in hours, or None. The server takes 1 mana every 1/rate seconds while the
    item's spells are on, and loot's rate is its spells' total power / 10,000 a second, at least MIN_DRAIN
    (TreasureFactory.cpp). Quest and other fixed items have their own rates, which the game never says."""
    if not mana or not spells or any(s not in powers for s in spells):
        return None
    rate = max(sum(powers[s] for s in spells) / 10000.0, MIN_DRAIN)
    return mana / rate / 3600


def short_spell(name):
    """A spell's name as the line gives it: its start shortened by SPELL_SHORT ("Impenetrability III" -> "Impen III")."""
    for long, short in SPELL_SHORT.items():
        if name == long or name.startswith(long + " "):
            return short + name[len(long):]
    return name


def _duration(hours):
    if hours * 60 < 90:
        return f"~{max(1, round(hours * 60))}m"
    return f"~{round(hours)}h" if hours >= 9.5 else f"~{hours:.1f}".rstrip("0").rstrip(".") + "h"


def _n(v):
    return f"{v:,}"


def _pct(mod, what):
    """A weapon's attack / defense modifier as "+5% attack" (nothing when it's even)."""
    d = round((mod - 1) * 100)
    return f"{'+' if d > 0 else ''}{d}% {what}" if d else ""


def _parts(p, powers=None):
    """The line's parts, by what they are: value / burden, stats (weapon, armor, restores, uses), spells, magic
    (difficulty, spellcraft, mana, and about how long a full charge lasts worn: powers, {spell name: power}),
    needs (who can use it), crafter. Each a string, "" when the item hasn't got it."""
    got = {"value": f"{_n(p['value'])}p" if p.get("value") is not None else "",
           "burden": f"{_n(p['burden'])} BU" if p.get("burden") is not None else ""}
    stats = []
    w = p.get("weapon")
    if w:
        bits = [sk.skill_name(w["skill"])] if w.get("skill") else []
        if w.get("damage", 0) > 0:
            lo = int(w["damage"] * (1 - (w.get("variance") or 0)) + 0.5)
            kinds = "/".join(n for bit, n in DAMAGE_TYPES if w.get("damage_type", 0) & bit)
            bits.append(f"{lo}-{w['damage']}{' ' + kinds if kinds else ''}" if lo != w["damage"] else f"{w['damage']}{' ' + kinds if kinds else ''}")
        if w.get("speed"):
            bits.append(f"Speed {w['speed']}")
        bits += [s for s in (_pct(w.get("offense", 1), "attack"), _pct(w.get("defense", 1), "defense")) if s]
        stats.append(", ".join(bits))
    if p.get("armor_level"):
        prot = p.get("protection")
        stats.append(f"AL {p['armor_level']}" + (f" ({', '.join(f'{k}{v:.1f}' for k, v in zip(PROTECTIONS, prot))})" if prot else ""))
    if p.get("boost_type") in BOOSTS and p.get("boost"):
        stats.append(f"Restores {p['boost']} {BOOSTS[p['boost_type']]}")
    if p.get("max_uses"):
        stats.append(f"{p.get('uses') if p.get('uses') is not None else '?'}/{p['max_uses']} uses")
    got["stats"] = " · ".join(s for s in stats if s)
    got["spells"] = ", ".join(short_spell(s) for s in p["spells"]) if p.get("spells") else ""
    magic = [f"Diff {p['difficulty']}" if p.get("difficulty") is not None else "",
             f"SC {p['spellcraft']}" if p.get("spellcraft") is not None else "",
             f"Mana {p['max_mana']}" if p.get("max_mana") is not None else ""]     # its capacity (what's in it now changes)
    hours = mana_hours(p.get("max_mana"), p.get("spells"), powers or {}) if magic[2] else None
    if hours is not None:
        magic[2] += f" ({_duration(hours)})"
    got["magic"] = ", ".join(m for m in magic if m)
    needs = []
    if p.get("heritage"):
        needs.append(f"{p['heritage']} only")
    if p.get("rank_limit") is not None:
        needs.append(f"Rank {p['rank_limit']}+")
    if p.get("skill_limit") is not None and p.get("skill_limit_type"):
        needs.append(f"{sk.skill_name(p['skill_limit_type'])} {p['skill_limit']}+")
    if p.get("min_level") is not None:
        needs.append(f"Level {p['min_level']}+")
    if p.get("max_level") is not None:
        needs.append(f"Level {p['max_level']} or below")
    got["needs"] = ", ".join(needs)
    got["crafter"] = f"made by {p['crafter']}" if p.get("crafter") else ""
    return got


def describe(name, p, powers=None):
    """One line: name · value · burden · what it does (weapon, armor, restores, uses) · spells · its magic (difficulty,
    spellcraft, mana, and about how long a full charge lasts worn) · who can use it. Parts it doesn't have are left
    out."""
    g = _parts(p, powers)
    parts = [name or "Item", g["value"], g["burden"], g["stats"], g["spells"], g["magic"], g["needs"], g["crafter"]]
    return " · ".join(s for s in parts if s)


def clip_lines(p, powers=None):
    """What the clipboard gets (no name: the game's chat already says it), as up to three lines: what it does
    (weapon, armor, restores, uses); value, burden and what it takes to use (difficulty, spellcraft, mana, limits,
    crafter: only spaces between, no commas, numbers' too); its spells. Lines with nothing on them are left out."""
    g = _parts(p, powers)
    lines = [g["stats"], " ".join(s for s in (g["value"], g["burden"], g["magic"], g["needs"], g["crafter"]) if s).replace(",", ""),
             g["spells"]]
    return [line for line in lines if line]
