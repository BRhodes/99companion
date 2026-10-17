"""An appraised item as one line of text, to paste into chat, a trade post or a note (messages.py decodes the
appraisal: _appraisal_profile)."""

import json
import re
from pathlib import Path

from . import skills as sk

DAMAGE_TYPES = [(0x1, "Slash"), (0x2, "Pierce"), (0x4, "Bludge"), (0x8, "Cold"), (0x10, "Fire"), (0x20, "Acid"),
                (0x40, "Lightning")]
PROTECTIONS = ["Slash", "Pierce", "Bludge", "Cold", "Fire", "Acid", "Lightning"]
HERITAGE_SHORT = {"Aluvian": "Aluv", "Gharu'ndim": "Gharu", "Sho": "Sho"}   # the clipboard's (HeritageIndexName)
PROT_LETTERS = "spbcfal"      # the same, as the clipboard has them (it's short of room)
BOOSTS = {2: "Health", 4: "Stamina", 6: "Mana"}
# spells' short words on the clipboard (it's short of room; the dashboard keeps the whole names), anywhere in a name:
# {"Impenetrability": "Impen", "Protection Other": "Prot"} (add to it freely)
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
    """A spell's name as the clipboard gives it: SPELL_SHORT's words shortened wherever they are, whole words only,
    longest first ("Impenetrability III" -> "Impen III", "Fire Protection Other III" -> "Fire Prot III")."""
    for long in sorted(SPELL_SHORT, key=len, reverse=True):
        name = re.sub(rf"(?<!\w){re.escape(long)}(?!\w)", SPELL_SHORT[long], name)
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
        stats.append(f"AL {p['armor_level']}" + (f" ({', '.join(f'{k} {v:.1f}' for k, v in zip(PROTECTIONS, prot))})" if prot else ""))
    if p.get("boost_type") in BOOSTS and p.get("boost"):
        stats.append(f"Restores {p['boost']} {BOOSTS[p['boost_type']]}")
    if p.get("max_uses"):
        stats.append(f"{p.get('uses') if p.get('uses') is not None else '?'}/{p['max_uses']} uses")
    got["stats"] = " · ".join(s for s in stats if s)
    got["spells"] = ", ".join(p["spells"]) if p.get("spells") else ""
    magic = [f"Diff {p['difficulty']}" if p.get("difficulty") is not None else "",
             f"Spellcraft {p['spellcraft']}" if p.get("spellcraft") is not None else "",
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
    """What the clipboard gets (no name: the game's chat already says it), short, as up to three lines -- a number
    first, then what it is, in lower case and against it (2,988bu, 105diff, 25speed):
        what it does:      weapon (skill, 1-3slash/pierce, 25speed, +5%attack), armor (120al · 1.3s, 1.0p, ...),
                           50health restored, 3/10uses
        value and needs:   5,949p · 2,988bu · lore 105, rank 5, bow 200, lvl 20, Aluv · crafter
                           (no spellcraft or mana)
        its spells, short (Impen III)
    Lines with nothing on them are left out."""
    first = []
    w = p.get("weapon")
    if w:
        bits = [sk.skill_name(w["skill"])] if w.get("skill") else []
        if w.get("damage", 0) > 0:
            lo = int(w["damage"] * (1 - (w.get("variance") or 0)) + 0.5)
            kinds = "/".join(n.lower() for bit, n in DAMAGE_TYPES if w.get("damage_type", 0) & bit)
            bits.append(f"{lo}-{w['damage']}{kinds}" if lo != w["damage"] else f"{w['damage']}{kinds}")
        if w.get("speed"):
            bits.append(f"{w['speed']}speed")
        bits += [s.replace(" ", "") for s in (_pct(w.get("offense", 1), "attack"), _pct(w.get("defense", 1), "defense")) if s]
        first.append(", ".join(bits))
    if p.get("armor_level"):
        prot = p.get("protection")
        first.append(f"{p['armor_level']}al" + (f" · {', '.join(f'{v:.1f}{k}' for k, v in zip(PROT_LETTERS, prot))}" if prot else ""))
    if p.get("boost_type") in BOOSTS and p.get("boost"):
        first.append(f"{p['boost']}{BOOSTS[p['boost_type']].lower()}")
    if p.get("max_uses"):
        first.append(f"{p.get('uses') if p.get('uses') is not None else '?'}/{p['max_uses']}uses")
    needs = [f"lore {p['difficulty']}" if p.get("difficulty") is not None else ""]    # (the arcane lore it takes)
    if p.get("rank_limit") is not None:
        needs.append(f"rank {p['rank_limit']}")
    if p.get("skill_limit") is not None and p.get("skill_limit_type"):
        needs.append(f"{sk.skill_name(p['skill_limit_type']).lower()} {p['skill_limit']}")
    if p.get("min_level") is not None:
        needs.append(f"lvl {p['min_level']}")
    if p.get("max_level") is not None:
        needs.append(f"lvl <={p['max_level']}")
    if p.get("heritage"):
        needs.append(HERITAGE_SHORT.get(p["heritage"], p["heritage"]))
    second = [f"{_n(p['value'])}p" if p.get("value") is not None else "",
              f"{_n(p['burden'])}bu" if p.get("burden") is not None else "",
              ", ".join(s for s in needs if s), f"made by {p['crafter']}" if p.get("crafter") else ""]
    third = ", ".join(short_spell(s) for s in p["spells"]) if p.get("spells") else ""
    lines = [" · ".join(s for s in first if s), " · ".join(s for s in second if s), third]
    return [line for line in lines if line]
