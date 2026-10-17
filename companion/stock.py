"""How many of each spell component to stock, from how you actually cast.

1. Casts per hour of each spell, from every stored cast of this character (across logins), per hour of
   ACTIVE casting: the gap between two casts counts in full up to IDLE_GAP, so time spent AFK doesn't
   water the rate down. Recent play weighs more: a cast's weight halves for every HALF_LIFE active hours
   of casting since it.
2. Components burned per cast from the burn model (each component's burn chance for this account's
   formula; a fizzle burns x1.5), using each spell's own fizzle rate. That's smooth even for components
   that rarely burn (scarabs), where a session's observed burns are all or nothing.
3. What a restock buys up to is learned from your restocks (restock.py); here, just the floor: never below
   MIN_STOCK of any component a spell you cast uses, nor below what one cast needs on hand plus one.
"""
import math
from collections import defaultdict

IDLE_GAP = 300.0       # seconds: a longer gap between casts counts as only this much casting
HALF_LIFE = 10.0       # active hours: older casts count half as much per this much casting since
BUY_HOURS = 2.0        # (the dashboard's "for N hours" wording)
MIN_STOCK = 7          # never stock fewer of a component your spells use
IN_USE = 0.1           # casts per active hour for a spell to count as one you cast
BURNS = {"cast", "done", "fizzled"}   # outcomes that burn components (refusals and failures don't)


def spell_rates(casts):
    """{spell id: (casts per active hour, fizzle rate)} from cast records ({ts, spell_id, result}), and
    the weighted active hours they cover."""
    casts = sorted((c for c in casts if c.get("result") in BURNS and c.get("spell_id")), key=lambda c: c["ts"])
    if not casts:
        return {}, 0.0
    # active hours of casting from each cast to the newest one, for its weight
    after = [0.0] * len(casts)
    for i in range(len(casts) - 2, -1, -1):
        after[i] = after[i + 1] + min(casts[i + 1]["ts"] - casts[i]["ts"], IDLE_GAP) / 3600.0
    weight = [0.5 ** (a / HALF_LIFE) for a in after]
    hours = sum(min(casts[i + 1]["ts"] - casts[i]["ts"], IDLE_GAP) / 3600.0 * weight[i + 1]
                for i in range(len(casts) - 1))
    hours = max(hours, IDLE_GAP / 3600.0)    # a handful of casts is at least one idle-gap of casting
    count, fizzled = defaultdict(float), defaultdict(float)
    for c, w in zip(casts, weight):
        count[c["spell_id"]] += w
        if c["result"] == "fizzled":
            fizzled[c["spell_id"]] += w
    return {sid: (n / hours, fizzled[sid] / n) for sid, n in count.items()}, hours


def plan(casts, model, carried, cache=None):
    """{component name: {per_hour, target, buy, hours_left, why}} for every component the spells you
    cast use. model: the BurnModel (expected_burn, required). carried: {name: count}."""
    # depends only on the history: worked out once per read of it (cache), not on every update
    if cache is not None and cache.get("casts") is casts:
        rates, hours = cache["rates"], cache["hours"]
    else:
        rates, hours = spell_rates(casts)
        if cache is not None:
            cache.clear()
            cache.update(casts=casts, rates=rates, hours=hours)
    per_hour, floor, why = defaultdict(float), {}, defaultdict(list)
    for sid, (rate, fizzle) in rates.items():
        ok, bad = model.expected_burn(sid), model.expected_burn(sid, fizzled=True)
        for name in set(ok) | set(bad):
            burn = rate * ((1 - fizzle) * ok.get(name, 0) + fizzle * bad.get(name, 0))
            per_hour[name] += burn
            if burn > 0:
                why[name].append((burn, sid, rate))
        if rate >= IN_USE:
            for name, n in model.required(sid).items():
                floor[name] = max(floor.get(name, MIN_STOCK), n + 1)
    out = {}
    for name in set(per_hour) | set(floor):
        rate = per_hour.get(name, 0.0)
        target = floor.get(name, 0)
        have = carried.get(name, 0)
        out[name] = {"per_hour": round(rate, 2), "target": target, "buy": max(0, target - have),
                     "floor": floor.get(name), "hours_left": round(have / rate, 1) if rate > 0 else None,
                     "why": [{"spell": sid, "casts_per_hour": round(r, 1), "burn_per_hour": round(b, 2)}
                             for b, sid, r in sorted(why[name], reverse=True)[:3]]}
    return out, hours
