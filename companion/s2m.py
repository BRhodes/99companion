"""Stamina-to-mana cycling: the mana you net from casting Revitalize Self then Stamina to Mana Self, over and
over, for every pair of levels.

From the server (SpellcastingManager.cpp, SkillChecks.cpp) and its spell table:
  Revitalize Self N  adds a roll of boost .. boost + variance stamina (so boost + variance / 2 on average),
                     times your effect multiplier (spell charge x economy, the same factor that stretches
                     your buffs' durations); stamina can't go past its maximum.
  Stamina to Mana N  takes half your current stamina (proportion 0.5) and gives that x (1 - loss) x the
                     effect multiplier as mana; loss runs 25% at I down to -50% at VI.
  A cast lands with chance 1 - 1/(1 + e^(0.07 (skill - power))) at your (buffed) Life Magic; a fizzle costs a
  flat 5 mana. A landed cast costs its base mana cut by Mana Conversion: two checks, each passing with
  1 - 1/(1 + e^(0.03 (MC - d))) against d = (50 + power) / 2, then 50 + power; a pass multiplies the cost by
  1 - (p - roll x luck) with roll uniform up to p and luck uniform 0-1, i.e. 1 - 3p^2/4 on average.

The pair is simulated cast by cast from a full stamina bar, the way the server does it: each landed
Revitalize rolls its stamina and anything past your maximum is lost; each landed Stamina to Mana takes half
of what you have. A level you can't land mostly fizzles, so it mostly costs 5 mana and does nothing.
Stamina regeneration between casts and a full mana bar (the excess is lost) aren't counted.
"""
import functools
import math
import random

from . import research

REVITALIZE = "Revitalize Self"
S2M = "Stamina to Mana Self"
LOSS = {1: 0.25, 2: 0.1, 3: -0.05, 4: -0.2, 5: -0.35, 6: -0.5}                          # spell table
BOOST = {1: (11, 9), 2: (16, 14), 3: (26, 24), 4: (38, 37), 5: (51, 49), 6: (76, 74)}  # boost, variance
MANA_CONVERSION = 16      # skill id
LIFE = 2                  # spell school


FIZZLE_MANA = 5


def _mc_check(skill, difficulty):
    return min(1.0, max(0.0, 1.0 - 1.0 / (1.0 + math.exp(0.03 * (skill - difficulty)))))


def expected_cost(base, power, mc):
    """Average mana a landed cast costs after Mana Conversion's two cuts (GenerateManaCost: difficulty 50 + power)."""
    if not mc:
        return float(base)
    difficulty = 50 + power
    cost = base * (1 - 0.75 * _mc_check(mc, difficulty // 2) ** 2)
    if cost > 1:
        cost *= 1 - 0.75 * _mc_check(mc, difficulty) ** 2
    return max(1.0, cost)


CYCLES, WARMUP = 1500, 50


def _converted(rev, s2m, max_stamina, mult):
    return _simulate(rev["level"], rev["chance"], s2m["chance"], s2m["loss"], max_stamina, round(mult, 3))


@functools.lru_cache(maxsize=512)
def _simulate(level, rev_chance, s2m_chance, loss, max_stamina, mult):
    """Average mana one Stamina to Mana gives, cast after a Revitalize over and over (seeded, so the same
    inputs always give the same number)."""
    rng = random.Random(f"{level}:{rev_chance}:{s2m_chance}:{loss}:{max_stamina}:{mult}")
    lo, var = BOOST[level]
    stamina, mana = max_stamina, 0
    for i in range(CYCLES + WARMUP):
        if rng.random() < rev_chance:
            stamina = min(max_stamina, stamina + int(rng.uniform(lo, lo + var) * mult))
        if rng.random() < s2m_chance:
            take = int(stamina * 0.5)
            stamina -= take
            if i >= WARMUP:
                mana += int(take * (1 - loss) * mult)
    return mana / CYCLES


def table(tr):
    """{rows: Revitalize I-VI [{level, spell, name, known, chance, cost, stamina}], cols: Stamina to Mana I-VI
    [{level, spell, name, known, chance, cost, loss}], cells: [[net mana per pair]], inputs} for the
    character, or None without the spell data or a character."""
    by_name = {sp.get("name"): sid for sid, sp in tr.spells.items()}
    roman = ["", "I", "II", "III", "IV", "V", "VI"]
    rev = {n: by_name.get(f"{REVITALIZE} {roman[n]}") for n in range(1, 7)}
    s2m = {n: by_name.get(f"{S2M} {roman[n]}") for n in range(1, 7)}
    if not all(rev.values()) or not all(s2m.values()) or not tr.vitals:     # no spells, or not in the world
        return None
    life = tr.magic_skills().get(LIFE)
    max_stamina = next((v["max"] for v in tr._vital_snapshot() if v["name"] == "Stamina"), 0)
    if life is None or not max_stamina:        # Life Magic untrained or not seen yet: nothing to work out
        return None
    mc = research.effective_skill(MANA_CONVERSION, tr.skills, tr.attributes, list(tr.enchantments.values()),
                                  tr.skill_formulas)
    mult = tr.duration_factor()
    known = set(tr.known_spells)

    def info(sid, n):
        sp = tr.spells[sid]
        chance = research.success_chance(life, sp.get("power", 0))
        cost = chance * expected_cost(sp.get("mana", 0), sp.get("power", 0), mc) + (1 - chance) * FIZZLE_MANA
        return {"level": n, "spell": sid, "name": sp.get("name"), "known": sid in known,
                "chance": round(chance, 3), "cost": cost}
    rows = [dict(info(rev[n], n), stamina=(BOOST[n][0] + BOOST[n][1] / 2) * mult) for n in range(1, 7)]
    cols = [dict(info(s2m[n], n), loss=LOSS[n]) for n in range(1, 7)]
    cells = [[round(_converted(r, c, max_stamina, mult) - r["cost"] - c["cost"], 1) for c in cols] for r in rows]
    for x in rows + cols:
        x["cost"] = round(x["cost"], 1)
    for r in rows:
        r["stamina"] = round(r["stamina"], 1)
    return {"rows": rows, "cols": cols, "cells": cells,
            "inputs": {"life_magic": life, "mana_conversion": mc, "max_stamina": max_stamina, "multiplier": round(mult, 2)}}
