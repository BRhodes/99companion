"""The character planner: a character that isn't one of yours -- its starting attributes, the skills it trains or
specializes, a buff tier and stat goals -- and the least XP that gets it there (goals.plan), the level by which it has
earned that much, the skill credits its skills take, and how often it casts each school's spells.

A build: {name, attributes: {"1".."6": 10..100} (330 in all at creation), skills: {"33": "trained" | "specialized"},
chargen: the skills are trained at creation (+5 ranks trained, +10 specialized: free XP, Client.cpp), buff: 0..6 (a
level I..VI buff on every attribute and skill: goals.BUFF_BY_LEVEL), targets: {"skill:33": 300, ...}, rate: XP an hour}.
A benchmark: a goal set to hold every build against -- {name, targets, buff} (against()).

The targets are buffed values when buff is set (as a goal's buff levels), else unbuffed. The XP a level has earned is
the level table's (dat.load_level_xp); skill credits: CREATION_CREDITS, then one at each level the table gives one.
"""
import math

from . import goals
from . import skills as sk

CREATION_CREDITS = 50          # Client.cpp kCreationSkillCredits
CREATION_ATTRIBUTES = 330      # what the six attributes add up to at creation
ATTRIBUTE_MIN, ATTRIBUTE_MAX = 10, 100
CHARGEN_RANKS = {goals.TRAINED: 5, goals.SPECIALIZED: 10}     # AC1999::TRAINED_SKILL_BONUS / SPECIALIZED_SKILL_BONUS
SCHOOLS = {31: "Creature", 32: "Item", 33: "Life", 34: "War"}
# Spell power by level: buffs and bolts on the first track, spells cast on others (and war's streaks) 25 above
POWERS = {"base": [1, 50, 100, 150, 200, 250], "other": [25, 75, 125, 175, 225, 275]}


def cast_chance(skill, power):
    return 1 - 1 / (1 + math.exp(0.07 * (skill - power)))


def _sac(v):
    return goals.SPECIALIZED if v == "specialized" else goals.TRAINED if v == "trained" else None


def character(build, tables, bases, vital_formulas):
    """The planner's character as goals works with it (goals.character's shape): nothing raised yet."""
    attrs = {}
    for a in goals.ATTRIBUTES:
        try:
            v = int((build.get("attributes") or {}).get(str(a)) or ATTRIBUTE_MIN)
        except (TypeError, ValueError):
            v = ATTRIBUTE_MIN
        attrs[a] = {"value": max(ATTRIBUTE_MIN, min(ATTRIBUTE_MAX, v)), "raises": 0, "invested": 0}
    vitals = {k: {"raises": 0, "init": 0, "invested": 0, "formula": vital_formulas.get(k - 1)} for k in goals.VITALS}
    chargen = build.get("chargen", True) is not False
    out_skills = {}
    for sid, b in bases.items():
        sac = _sac((build.get("skills") or {}).get(str(sid)))
        if sac is None and b["trained_cost"] == 0:
            sac = goals.TRAINED                  # (it comes trained)
        if sac is None:
            continue
        table = tables.get("specialized" if sac == goals.SPECIALIZED else "trained") or []
        ranks = CHARGEN_RANKS[sac] if chargen else 0
        xp = table[ranks] if ranks < len(table) else 0
        out_skills[sid] = {"sac": sac, "xp": xp, "rank": ranks, "init": 0, "formula": b["formula"]}
    return {"attributes": attrs, "vitals": vitals, "skills": out_skills}


def credits_used(build, bases):
    used = 0
    for sid, v in (build.get("skills") or {}).items():
        b = bases.get(int(sid)) if str(sid).isdigit() else None
        if b and _sac(v):
            used += b["specialized_cost"] if _sac(v) == goals.SPECIALIZED else b["trained_cost"]
    return used


def credits_at(level, level_credits):
    return CREATION_CREDITS + sum(level_credits[1:level + 1])


def level_for(xp, level_xp):
    """The first level whose total XP covers this much, or None (more than the last level's)."""
    return next((lv for lv in range(1, len(level_xp)) if level_xp[lv] >= xp), None)


def _goal(build):
    tier = goals._level(build.get("buff"))
    targets = {k: v for k, v in (build.get("targets") or {}).items()}
    buffs = {k: {"attr": tier, "skill": tier} for k in targets} if tier else {}
    return {"name": build.get("name") or "Build", "targets": targets, "buffs": buffs}


def plan(build, tables, bases, vital_formulas, level_xp, level_credits):
    ch = character(build, tables, bases, vital_formulas)
    tier = goals._level(build.get("buff"))
    p = goals.plan(_goal(build), ch, tables)
    # where it ends up: the plan's raises on top of the start
    end = {"attributes": {a: dict(s) for a, s in ch["attributes"].items()},
           "vitals": {k: dict(v) for k, v in ch["vitals"].items()},
           "skills": {k: dict(s) for k, s in ch["skills"].items()}}
    for r in p["raises"] if p["feasible"] else []:
        if r["kind"] == "attribute":
            end["attributes"][r["id"]]["value"] += r["raises"]
        elif r["kind"] == "vital":
            end["vitals"][r["id"]]["raises"] += r["raises"]
        else:
            end["skills"][r["id"]]["rank"] += r["raises"]
    add = goals.BUFF_BY_LEVEL[tier]
    bonus = {"attributes": {a: add for a in goals.ATTRIBUTES}, "skills": {s: add for s in end["skills"]}} if add else None
    start_v, end_v, end_b = goals.values(ch), goals.values(end), goals.values(end, None, bonus)
    stats = [{"key": f"{k}:{i}", "kind": k, "id": i, "name": goals.stat_name(k, i), "start": start_v[(k, i)],
              "end": end_v[(k, i)], "buffed": end_b[(k, i)],
              "specialized": k == "skill" and ch["skills"][i]["sac"] == goals.SPECIALIZED}
             for k, i in sorted(end_v, key=lambda ki: (["attribute", "vital", "skill"].index(ki[0]),
                                                       goals.stat_name(*ki) if ki[0] == "skill" else "", ki[1]))]   # (skills by name)
    cost = p["cost"]
    used = credits_used(build, bases)
    xp_level = level_for(cost, level_xp) if cost is not None else None
    credit_level = next((lv for lv in range(1, len(level_xp)) if credits_at(lv, level_credits) >= used), None)
    try:
        rate = float(build.get("rate") or 0)
    except (TypeError, ValueError):
        rate = 0
    magic = []
    for sid, school in SCHOOLS.items():
        if sid not in end["skills"]:
            magic.append({"school": school, "skill": None})
            continue
        s = end_b[("skill", sid)]
        magic.append({"school": school, "skill": s, "unbuffed": end_v[("skill", sid)],
                      "chances": {track: [round(cast_chance(s, pw), 4) for pw in pws] for track, pws in POWERS.items()}})
    return {"name": build.get("name") or "Build", "cost": cost, "feasible": p["feasible"], "problems": p["problems"],
            "targets": p["targets"], "raises": p["raises"], "stats": stats, "buff": tier,
            "level": max(xp_level, credit_level or 0) if xp_level is not None else None,   # (None: past the last level)
            "xp_level": xp_level, "credits": {"used": used, "creation": CREATION_CREDITS, "level": credit_level,
                                              "over_creation": max(0, used - CREATION_CREDITS)},
            "hours": round(cost / rate, 1) if cost is not None and rate > 0 else None, "magic": magic}


def against(build, bench, tables, bases, vital_formulas, level_xp, level_credits):
    """A build against a benchmark (a goal set of its own: {name, targets, buff}): the build's setup, the benchmark's
    goals and buff tier. {cost, level, xp_level, hours, feasible, missing: [skills it needs trained]}."""
    p = plan({**build, "targets": bench.get("targets") or {}, "buff": bench.get("buff") or 0},
             tables, bases, vital_formulas, level_xp, level_credits)
    missing = [goals.stat_name("skill", int(k.split(":")[1])) for k in (bench.get("targets") or {})
               if k.startswith("skill:") and k.split(":")[1].isdigit()
               and not any(s["key"] == k for s in p["stats"])]
    return {"cost": p["cost"] if not missing else None, "level": p["level"] if not missing else None,
            "xp_level": p["xp_level"], "hours": p["hours"] if not missing else None,
            "feasible": p["feasible"] and not missing, "missing": missing}


def catalog(bases):
    """What a build can pick: the skills (with their credit costs), the attributes and vitals, and the spell powers."""
    return {"skills": sorted(({"id": sid, "name": sk.skill_name(sid), "trained": b["trained_cost"],
                               "specialized": b["specialized_cost"], "free": b["trained_cost"] == 0}
                              for sid, b in bases.items()), key=lambda s: s["name"]),
            "attributes": [{"id": a, "name": n} for a, n in goals.ATTRIBUTES.items()],
            "vitals": [{"id": k, "name": n} for k, n in goals.VITALS.items()],
            "creation": {"credits": CREATION_CREDITS, "attributes": CREATION_ATTRIBUTES, "min": ATTRIBUTE_MIN, "max": ATTRIBUTE_MAX},
            "powers": POWERS, "schools": SCHOOLS}
