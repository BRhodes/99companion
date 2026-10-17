"""Experience: where a character's XP has gone, and goals -- unbuffed values to reach -- with the least XP that
gets there, and what to raise with the XP you have now.

Costs (this server; the ExperienceTable from portal.dat, dat.load_xp_tables):
  attribute: by its raises since the character was made, not its value: n raises cost attribute[n] in all. The 1999
    wire folds the value into its first field and picks the XP-spent field so the client's XP-to-raise works out:
    XP-to-raise = attribute[value + 1] - wire XP (mod 2^32: past the table's end the server uses 0xFFFFFFFF)
    = attribute[raises + 1] - invested. The steps only grow, so the step says how many raises (attribute_raises).
  vital (max health / stamina / mana): its raises, vital[n]; the XP put in is sent as it is.
  skill: its rank and XP (trained / specialized tables; a specialized skill's XP starts with free XP); the next rank
    costs table[rank + 1] - its XP.
Values, unbuffed: an attribute's value; a vital round((w + x a1 + y a2) / z) + init + raises (Attribute2ndTable); a
skill round((w + x a1 + y a2) / z) + rank + init (SkillTable). Raising an attribute raises every skill and vital made
from it, so a goal's cheapest way there can mean raising an attribute rather than the skills.

Buffed goals (goal "buffed"): the targets are with your buffs on -- your buff profile's, at the tier each casts now
(buff_bonus): an attribute buff raises the attribute and every skill made from it, a skill buff the skill.
next_tiers(): for each of the profile's Relax / Max buffs, the next tier up and the buffed school skill that casts it
that reliably (Relax: RELAX_CHANCE; Max: the profile's max chance; the game's 1 - 1 / (1 + e^(0.07 (skill - power)))).

A goal: {name, targets: {"attribute:5": 200, "skill:33": 250, "vital:2": 150}, buffed}. plan() picks the attribute raises
that make the whole cheapest -- each skill and vital then takes just the ranks it's short -- by a local search over the
attributes the goal touches, in halving steps (each raise costs more than the last, so it settles on the least).
"""
import itertools
import math

from . import buffpanel, profilebuffs, research
from . import skills as sk

ATTRIBUTES = {1: "Strength", 2: "Endurance", 3: "Quickness", 4: "Coordination", 5: "Focus", 6: "Self"}
VITALS = {2: "Health", 4: "Stamina", 6: "Mana"}
TRAINED, SPECIALIZED = 2, 3
RELAX_CHANCE = profilebuffs.RELAX_CHANCE
WRAP = 1 << 32
INF = float("inf")


def attribute_raises(value, wire_cp, table):
    """(raises, XP invested) from the wire's attribute, or (None, None) when it doesn't read back."""
    if value is None or wire_cp is None or not table:
        return None, None
    top = table[value + 1] if value + 1 < len(table) else 0xFFFFFFFF
    step = (top - wire_cp) % WRAP
    for n in range(len(table) - 1):
        if table[n + 1] - table[n] == step:
            return n, table[n]
    return None, None


def rank_of(xp, table):
    """The rank an amount of skill XP buys (the most n with table[n] <= it)."""
    n = 0
    while n + 1 < len(table) and table[n + 1] <= xp:
        n += 1
    return n


def formula_value(f, values):
    if not f:
        return 0
    w, x, y, z, a1, a2 = f
    return math.floor((w + x * values.get(a1, 0) + y * values.get(a2, 0)) / z + 0.5) if z else 0


def character(tr, tables):
    """The parts of a character goals work with: {"attributes": {id: {value, raises, invested}}, "vitals": {id: {raises,
    init, invested, formula}}, "skills": {id: {sac, xp, rank, init, formula}}} (skills trained or better)."""
    A = tables.get("attribute") or []
    attrs = {}
    for a in ATTRIBUTES:
        v = tr.attributes.get(a)
        if v is None:
            continue
        raises, invested = attribute_raises(v, tr.attribute_cp.get(a), A)
        attrs[a] = {"value": v, "raises": raises, "invested": invested}
    vitals = {}
    for key in VITALS:
        v = tr.vitals.get(key)
        if v:
            vitals[key] = {"raises": v["raises"], "init": v["init"], "invested": v.get("cp") or 0,
                           "formula": tr.vital_formulas.get(key - 1)}
    skills = {}
    for sid, s in tr.skills.items():
        if s.get("sac", 0) < TRAINED:
            continue
        table = tables.get("specialized" if s["sac"] >= SPECIALIZED else "trained") or []
        rank = rank_of(s.get("pp") or 0, table)
        skills[sid] = {"sac": s["sac"], "xp": s.get("pp") or 0, "rank": rank, "init": (s.get("level") or 0) - rank,
                       "formula": tr.skill_formulas.get(sid)}
    return {"attributes": attrs, "vitals": vitals, "skills": skills}


def _table_for(ch_skill, tables):
    return tables.get("specialized" if ch_skill["sac"] >= SPECIALIZED else "trained") or []


def stat_name(kind, sid):
    return ATTRIBUTES.get(sid) if kind == "attribute" else VITALS.get(sid) if kind == "vital" else sk.skill_name(sid)


def values(ch, raised=None, bonus=None):
    """Values with some attribute raises: {("attribute"|"vital"|"skill", id): value}; unbuffed, or with bonus
    ({"attributes": {id: +n}, "skills": {id: +n}}, buff_bonus) on."""
    raised, bonus = raised or {}, bonus or {}
    battr, bskill = bonus.get("attributes") or {}, bonus.get("skills") or {}
    attr = {a: s["value"] + raised.get(a, 0) + battr.get(a, 0) for a, s in ch["attributes"].items()}
    out = {("attribute", a): v for a, v in attr.items()}
    for k, v in ch["vitals"].items():
        out[("vital", k)] = formula_value(v["formula"], attr) + v["init"] + v["raises"]
    for k, s in ch["skills"].items():
        out[("skill", k)] = formula_value(s["formula"], attr) + s["rank"] + s["init"] + bskill.get(k, 0)
    return out


def _profile(prefs):
    profs = (prefs or {}).get("buff_profiles") or []
    name = (prefs or {}).get("active_profile")
    return next((p for p in profs if p.get("name") == name), profs[0] if profs else None)


RELAX_FIRST = ("Focus", "Self", "Creature Enchantment")   # their Relax tiers go on first; their Max tiers are cast over them


def _school_skills(ch, bonus=None):
    """{spell school: its skill} from a character's values (unbuffed, or with a bonus on)."""
    vals = values(ch, None, bonus)
    return {school: vals[("skill", sid)] for school, sid in research.SCHOOL_SKILL.items() if ("skill", sid) in vals}


def _add(bonus, tr, mods, option):
    mod = mods.get(option["spell"]) if option else None
    if not mod or not mod[0] & research.ADD or mod[2] <= 0:
        return
    if mod[0] & research.ATTRIBUTE_MOD:
        bonus["attributes"][mod[1]] = max(bonus["attributes"].get(mod[1], 0), mod[2])
    elif mod[0] & research.SKILL_MOD:
        bonus["skills"][mod[1]] = max(bonus["skills"].get(mod[1], 0), mod[2])
    else:
        return
    bonus["spells"].append((tr.spells.get(option["spell"]) or {}).get("name"))


def _resolved(tr, prefs, mods, ch=None):
    """[(the profile's buff, its row, its level, the option it casts, what's on while it's cast)] -- known tiers,
    components or not. Focus, Self and Creature Enchantment (RELAX_FIRST) go on at their Relax tier first, from your
    unbuffed skill; a Max one of them is then cast over those (so its tier comes from the skill with them on, and
    what's on for it is its Relax tier). The rest resolve as they would now."""
    prof = _profile(prefs)
    if not prof:
        return None, []
    rows = {r["category"]: r for r in buffpanel.choices(tr, mods)}
    now_skills = tr.magic_skills()
    entries = []
    for pb in prof.get("buffs") or []:
        row = rows.get(pb.get("category"))
        if not row:
            continue
        level = pb.get("level")
        if level is None:
            level = profilebuffs.tier_of((tr.spells.get(pb.get("spell")) or {}).get("name"))
        entries.append((pb, row, level))
    first = {}
    pre = {"attributes": {}, "skills": {}, "spells": []}
    if ch is not None:
        base = _school_skills(ch)
        for pb, row, level in entries:
            if row["name"] in RELAX_FIRST and level in ("relax", "max"):
                first[row["category"]] = profilebuffs.resolve(tr, base, row, "relax", prof.get("max_chance"), ignore_missing=True)
                _add(pre, tr, mods, first[row["category"]])
        over = _school_skills(ch, pre)
    out = []
    for pb, row, level in entries:
        if row["category"] in first:
            relax = first[row["category"]]
            if level == "relax":
                out.append((pb, row, level, relax, relax))
            else:
                out.append((pb, row, level, profilebuffs.resolve(tr, over, row, level, prof.get("max_chance"), ignore_missing=True), relax))
        else:
            o = profilebuffs.resolve(tr, now_skills, row, level, prof.get("max_chance"), ignore_missing=True)
            out.append((pb, row, level, o, o))
    return prof, out


def buff_bonus(tr, prefs, mods, ch=None):
    """What your buff profile has on you while you cast: {"attributes": {id: +n}, "skills": {id: +n}, "spells": [names]}
    (additive attribute and skill buffs; Focus / Self / Creature Enchantment at their Relax tier, cast first)."""
    out = {"attributes": {}, "skills": {}, "spells": []}
    _prof, buffs = _resolved(tr, prefs, mods, ch)
    for _pb, _row, _level, _o, on in buffs:
        _add(out, tr, mods, on)
    return out


def skill_for_chance(power, chance):
    """The least school skill that casts a spell of this power at least this often."""
    chance = min(0.999, max(0.001, chance))
    return math.ceil(power + math.log(chance / (1 - chance)) / 0.07 - 1e-9)


def next_tiers(tr, prefs, mods, ch=None):
    """For each of the profile's Relax / Max buffs, the next tier up and what casts it as reliably:
    {"list": [{buff, mode, now, next, spell, power, school, skill, need}], "targets": {"skill:id": the most any needs}}."""
    prof, buffs = _resolved(tr, prefs, mods, ch)
    out = {"list": [], "targets": {}}
    for pb, row, level, o, _on in buffs:
        if level not in ("relax", "max"):
            continue
        now = o["level"] if o else 0
        nxt = next((x for x in sorted(row["options"], key=lambda x: x["level"]) if x["level"] > now), None)
        sp = tr.spells.get(nxt["spell"]) if nxt else None
        skill = research.SCHOOL_SKILL.get((sp or {}).get("school"))
        if not sp or not skill or not sp.get("power"):
            continue
        floor = RELAX_CHANCE if level == "relax" else (prof.get("max_chance") or 0.3)
        need = skill_for_chance(sp["power"], floor)
        out["list"].append({"buff": row["name"], "mode": level, "now": now, "next": nxt["level"], "spell": sp.get("name"),
                            "known": bool(nxt.get("known")), "power": sp["power"], "school": skill,
                            "skill": sk.skill_name(skill), "chance": floor, "need": need})
        key = f"skill:{skill}"
        out["targets"][key] = max(out["targets"].get(key, 0), need)
    return out


def parse_targets(targets):
    out = []
    for key, target in (targets or {}).items():
        kind, _, sid = str(key).partition(":")
        if kind in ("attribute", "vital", "skill") and sid.isdigit() and isinstance(target, (int, float)):
            out.append((kind, int(sid), int(target)))
    return out


def plan(goal, ch, tables, unassigned=0, bonus=None):
    """The cheapest way to the goal: {name, cost, feasible, problems, targets: [{key, kind, id, name, current, target,
    distance}], raises: [{kind, id, name, from, to (value), raises (how many), cost, steps}], now: [the part of
    raises your unassigned XP pays for, cheapest steps first], still (XP to earn)}."""
    A, V = tables.get("attribute") or [], tables.get("vital") or []
    targets = parse_targets(goal.get("targets"))
    bonus = bonus if goal.get("buffed") else None
    battr = (bonus or {}).get("attributes") or {}
    problems = []
    for kind, sid, _t in targets:
        if kind == "skill" and sid not in ch["skills"]:
            problems.append(f"{stat_name(kind, sid)} isn't trained")
        if kind == "attribute" and ch["attributes"].get(sid, {}).get("raises") is None:
            problems.append(f"{stat_name(kind, sid)}'s raises couldn't be read")
    targets = [t for t in targets if not (t[0] == "skill" and t[1] not in ch["skills"])]
    attrs = ch["attributes"]
    # the attributes that matter: the goal's own, and the ones its skills and vitals are made from
    touched = set()
    for kind, sid, _t in targets:
        if kind == "attribute":
            touched.add(sid)
        else:
            f = (ch["skills"].get(sid) or {}).get("formula") if kind == "skill" else (ch["vitals"].get(sid) or {}).get("formula")
            if f:
                touched |= {a for a, coef in ((f[4], f[1]), (f[5], f[2])) if a and coef}
    touched = {a for a in touched if a in attrs and attrs[a]["raises"] is not None}
    lo = {a: 0 for a in touched}
    for kind, sid, t in targets:
        if kind == "attribute" and sid in touched:
            lo[sid] = max(lo[sid], t - attrs[sid]["value"] - battr.get(sid, 0))
    hi = {a: len(A) - 1 - attrs[a]["raises"] for a in touched}

    def parts(d):
        """(total cost, {(kind, id): (raises / ranks wanted, cost)}) with these attribute raises."""
        total, need = 0, {}
        for a, n in d.items():
            if n:
                r = attrs[a]["raises"]
                c = A[r + n] - A[r]
                need[("attribute", a)] = (n, c)
                total += c
        val = values(ch, d, bonus)
        for kind, sid, t in targets:
            short = t - val.get((kind, sid), 0)
            if short <= 0 or kind == "attribute":
                if kind == "attribute" and short > 0:
                    return INF, None              # an attribute the search can't raise
                continue
            if kind == "skill":
                s = ch["skills"][sid]
                tbl = _table_for(s, tables)
                if s["rank"] + short >= len(tbl):
                    return INF, None
                c = tbl[s["rank"] + short] - s["xp"]
            else:
                v = ch["vitals"].get(sid)
                if not v or v["raises"] + short >= len(V):
                    return INF, None
                c = V[v["raises"] + short] - v["invested"]
            need[(kind, sid)] = (short, c)
            total += c
        return total, need

    d = {a: lo[a] for a in touched}
    if any(lo[a] > hi[a] for a in touched):
        best = INF
    else:
        best, _ = parts(d)
        step = 64
        while step >= 1:
            improved = True
            while improved:
                improved = False
                # one attribute up or down, or two at once (the skill formulas round: trading a raise between
                # Focus and Self can be what's cheaper)
                moves = [{a: s} for a in sorted(touched) for s in (1, -1)]
                moves += [{a: s, b: u} for a, b in itertools.combinations(sorted(touched), 2) for s in (1, -1) for u in (1, -1)]
                for move in moves:
                    trial = {**d, **{a: d[a] + s * step for a, s in move.items()}}
                    if any(trial[a] < lo[a] or trial[a] > hi[a] for a in move):
                        continue
                    c, _ = parts(trial)
                    if c < best:
                        d, best, improved = trial, c, True
            step //= 2
    feasible = best != INF
    _total, need = parts(d) if feasible else (INF, {})
    now_vals, goal_vals = values(ch, None, bonus), values(ch, d, bonus) if feasible else values(ch, None, bonus)
    rows = []
    for (kind, sid), (n, cost) in sorted((need or {}).items(), key=lambda kv: (["attribute", "vital", "skill"].index(kv[0][0]), kv[0][1])):
        steps = _steps(kind, sid, n, ch, tables)
        before = now_vals.get((kind, sid), 0)
        # an attribute: its raises; a skill or vital: what its attributes' raises bring it to, then its own
        after = before + n if kind == "attribute" else goal_vals.get((kind, sid), 0) + n
        rows.append({"kind": kind, "id": sid, "name": stat_name(kind, sid), "from": before, "to": after,
                     "raises": n, "cost": cost, "steps": steps})
    tgt = [{"key": f"{k}:{i}", "kind": k, "id": i, "name": stat_name(k, i), "current": now_vals.get((k, i)),
            "target": t, "distance": max(0, t - (now_vals.get((k, i)) or 0))} for k, i, t in targets]
    cost = best if feasible else None
    return {"name": goal.get("name") or "Goal", "buffed": bool(goal.get("buffed")), "cost": cost, "feasible": feasible, "problems": problems,
            "targets": tgt, "raises": rows, "now": guidance(rows, unassigned if feasible else 0),
            "still": None if cost is None else max(0, cost - (unassigned or 0))}


def _steps(kind, sid, n, ch, tables):
    """Each raise's / rank's own cost, in order."""
    if kind == "attribute":
        A, r = tables["attribute"], ch["attributes"][sid]["raises"]
        return [A[r + k + 1] - A[r + k] for k in range(n)]
    if kind == "vital":
        V, v = tables["vital"], ch["vitals"][sid]
        cum = [V[v["raises"] + k] - v["invested"] for k in range(n + 1)]
        return [cum[k + 1] - max(0, cum[k]) for k in range(n)]
    s = ch["skills"][sid]
    tbl = _table_for(s, tables)
    cum = [max(0, tbl[s["rank"] + k] - s["xp"]) for k in range(n + 1)]
    return [cum[k + 1] - cum[k] for k in range(n)]


def guidance(rows, unassigned):
    """What the XP you have now pays for: the plan's raises, cheapest step first (any of them is part of the
    cheapest way there, so none is wasted), summed per stat: [{kind, id, name, raises, cost}]."""
    steps = sorted(((c, i) for i, r in enumerate(rows) for c in r["steps"]), key=lambda s: s[0])
    left, took = unassigned or 0, {}
    for c, i in steps:
        if c > left:
            break
        left -= c
        n, spent = took.get(i, (0, 0))
        took[i] = (n + 1, spent + c)
    return [{"kind": rows[i]["kind"], "id": rows[i]["id"], "name": rows[i]["name"], "raises": n, "cost": spent}
            for i, (n, spent) in sorted(took.items())]


def summary(tr, tables, goals, prefs=None, mods=None):
    """The Experience page's snapshot: where the XP went, the unbuffed values (and their next raise's cost), what
    your buff profile adds, each goal's plan, and the next tiers of your Relax / Max buffs."""
    if not tables:
        return None
    ch = character(tr, tables)
    val = values(ch)
    A, V = tables["attribute"], tables["vital"]
    attrs = [{"id": a, "name": ATTRIBUTES[a], "value": s["value"], "raises": s["raises"], "invested": s["invested"],
              "next": (A[s["raises"] + 1] - A[s["raises"]]) if s["raises"] is not None and s["raises"] + 1 < len(A) else None}
             for a, s in sorted(ch["attributes"].items())]
    vits = [{"id": k, "name": VITALS[k], "value": val[("vital", k)], "raises": v["raises"], "invested": v["invested"],
             "next": (V[v["raises"] + 1] - v["invested"]) if v["raises"] + 1 < len(V) else None}
            for k, v in sorted(ch["vitals"].items())]
    sks = []
    for k, s in ch["skills"].items():
        tbl = _table_for(s, tables)
        sks.append({"id": k, "name": sk.skill_name(k), "value": val[("skill", k)], "rank": s["rank"],
                    "specialized": s["sac"] >= SPECIALIZED, "xp": s["xp"],
                    "next": (tbl[s["rank"] + 1] - s["xp"]) if s["rank"] + 1 < len(tbl) else None})
    sks.sort(key=lambda s: s["name"])
    invested = {"attributes": sum(a["invested"] or 0 for a in attrs), "vitals": sum(v["invested"] for v in vits),
                "skills": sum(s.get("pp") or 0 for s in tr.skills.values())}
    unassigned = tr.xp_unassigned or 0
    bonus = buff_bonus(tr, prefs, mods, ch) if mods is not None else {"attributes": {}, "skills": {}, "spells": []}
    buffed = values(ch, None, bonus)
    for a in attrs:
        a["buffed"] = buffed.get(("attribute", a["id"]))
    for s in sks:
        s["buffed"] = buffed.get(("skill", s["id"]))
    return {"total": tr.xp_total, "unassigned": tr.xp_unassigned, "invested": invested,
            "gained": sum(invested.values()) + unassigned, "attributes": attrs, "vitals": vits, "skills": sks,
            "buffs": bonus, "next_tiers": next_tiers(tr, prefs, mods, ch) if mods is not None else {"list": [], "targets": {}},
            "goals": [plan(g, ch, tables, unassigned, bonus) for g in goals or []]}
