"""Monster info: where and with what to hit the creature you're fighting.

From the server's creature data (data/monsters.json, tools/build_monsters.py) and its damage rules
(WeenieObject.cpp TakeDamage):

  physical (arrows, swords; elemental arrows too): only the struck body part's armor counts,
      armor = base armor x ArmorModVs<type>, damage x 1 / (1 + armor / 63.33) (negative armor adds damage);
      the creature's innate resistance is ignored for physical hits
  magic (war spells): only the innate resistance multiplier counts, Resist<type>

Its own melee hits (combat/MeleeAttackEventData.cpp, CombatFormulas.cpp): a body part's d_Val, less up to d_Var
of it at random, x (0.5 + a random attack power 0..1), x its Strength (+1.1% a point over 55); one hit in ten
crits, skipping the variance and doubling. All before your armor and protections.

The struck part depends on the attack height: each part has a weight at high/medium/low, from the front
and from behind. Front is assumed (you usually face what you shoot).
"""
import json
from pathlib import Path

TYPES = ["slash", "pierce", "bludgeon", "acid", "cold", "fire", "electric"]
LABEL = {"slash": "Slash", "pierce": "Pierce", "bludgeon": "Bludgeon", "acid": "Acid", "cold": "Cold",
         "fire": "Fire", "electric": "Lightning"}
HEIGHTS = ["High", "Medium", "Low"]
ARMOR_SCALE = 190.0 / 3.0
STRENGTH_BASELINE, STRENGTH_PER_POINT = 55, 0.011
CRIT_CHANCE = 0.1
WEAK = 0.85      # a part at or below this share of the creature's average armor is a weak spot

_DATA = Path(__file__).parent / "data" / "monsters.json"


def load(path=_DATA):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# Defense skills: round((w + x*attr1 + y*attr2) / z) + the creature's init level (the dat's SkillTable when
# the companion has it, else the same: melee (Quick + Coord) / 3, missile (Quick + Coord) / 5, magic
# (Self + Focus) / 10). Kill XP: an authored XpOverride, else the AC1999 level curve (Monster.cpp OnDeath).
DEFENSES = {"melee": 6, "missile": 7, "magic": 15}
DEFENSE_FORMULAS = {6: (0, 1, 1, 3, 3, 4), 7: (0, 1, 1, 5, 3, 4), 15: (0, 1, 1, 10, 6, 5)}   # the 1999 SkillTable's
KILL_XP_COEFFICIENT, KILL_XP_EXPONENT = 7.6356, 1.58006


def defenses(m, formulas=None):
    """{"melee", "missile", "magic"}: the creature's defense skills, None where its data doesn't say."""
    attrs = {int(k): v for k, v in (m.get("attributes") or {}).items()}
    inits = {int(k): v for k, v in (m.get("skills") or {}).items()}
    out = {}
    for name, sid in DEFENSES.items():
        f = (formulas or {}).get(sid) or DEFENSE_FORMULAS[sid]
        w, x, y, z, a1, a2 = f
        if not attrs or not z:
            out[name] = None
            continue
        out[name] = int((w + x * attrs.get(a1, 0) + y * attrs.get(a2, 0)) / z + 0.5) + inits.get(sid, 0)
    return out


def kill_xp(m):
    if m.get("xp_override") is not None:
        return m["xp_override"]
    level = m.get("level") or 0
    return int(KILL_XP_COEFFICIENT * level ** KILL_XP_EXPONENT + 0.5) if level > 0 else 0


def armor_factor(armor):
    return 1.0 / (1.0 + armor / ARMOR_SCALE) if armor >= 0 else 1.0 + (-armor / ARMOR_SCALE)


def strength_mult(m):
    strength = (m.get("attributes") or {}).get("1", (m.get("attributes") or {}).get(1, 0)) or 0
    return max(1.0, 1.0 + (strength - STRENGTH_BASELINE) * STRENGTH_PER_POINT)


def attacks(m):
    """Its body-part attacks, hardest first: {part, type, base, mult, avg, max, crit} (before your armor)."""
    mult = strength_mult(m)
    out = []
    for p in m.get("parts") or []:
        if p.get("dmg"):
            base, var = p["dmg"], p.get("var") or 0.0
            out.append({"part": p["name"], "type": p.get("dtype"), "base": base, "mult": round(mult, 2),
                        "avg": round(base * (1 - var / 2) * mult), "max": round(base * 1.5 * mult),
                        "crit": round(base * 1.5 * mult * 2)})
    return sorted(out, key=lambda a: -a["max"])


def analyze(m, side="front"):
    """Everything the panel shows for one creature record."""
    parts = m["parts"]
    heights = []
    for h, name in enumerate(HEIGHTS):
        weights = [(p, p[side][h]) for p in parts if p[side][h] > 0]
        total = sum(w for _, w in weights)
        if not total:
            heights.append({"height": name, "parts": [], "physical": None})
            continue
        hits = [{"name": p["name"], "chance": round(w / total, 2), "armor": p["armor"]} for p, w in weights]
        physical = {t: round(sum(w / total * armor_factor(p["armor"] * m["armor_mod"][t]) for p, w in weights), 3)
                    for t in TYPES}
        heights.append({"height": name, "parts": sorted(hits, key=lambda x: -x["chance"]), "physical": physical})
    avg = sum(p["armor"] for p in parts) / len(parts)
    # each height's softest part, flagged when it's well below the rest
    weak = []
    for h in heights:
        if h["parts"]:
            x = min(h["parts"], key=lambda x: x["armor"])
            weak.append({"height": h["height"], "part": x["name"], "armor": x["armor"], "chance": x["chance"],
                         "weak": x["armor"] <= avg * WEAK})
    magic = {t: round(m["resist"][t], 2) for t in TYPES}
    reachable = [h for h in heights if h["physical"]]
    best_h = max(reachable, key=lambda h: max(h["physical"].values())) if reachable else None
    best_p = max(TYPES, key=lambda t: best_h["physical"][t]) if best_h else None
    best_m = max(TYPES, key=lambda t: magic[t])
    by_type = {t: max((h["physical"][t] for h in reachable), default=0) for t in TYPES}
    advice = []
    if best_h:
        advice.append(f"Attack {best_h['height'].lower()} with {LABEL[best_p].lower()}"
                      f" (x{best_h['physical'][best_p]:.2f} of full damage)")
    advice.append(f"cast {LABEL[best_m].lower()} (x{magic[best_m]:.2f})")
    worst = [LABEL[t].lower() for t in TYPES if magic[t] <= 0.5]
    if worst:
        advice.append(f"avoid {', '.join(worst)} magic")
    return {"name": m["name"], "level": m["level"], "side": side, "attacks": attacks(m), "heights": heights, "weak": weak,
            "magic": magic, "physical_best": by_type, "best": {"height": best_h and best_h["height"],
            "physical": best_p, "magic": best_m}, "advice": "; ".join(advice) + "."}
