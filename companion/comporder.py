"""The component order: what to keep of each spell component, from what you actually burn.

1. The mix: the components you burned since a fixed date (each cast's observed burns, across logins), yours to set
   (the Comps page: quick buttons for an hour to 30 days back, or a date); until it's set, the last week.
2. Scaled to a target weight: every component's kept amount is its share of that mix times one factor, chosen so
   the whole order weighs the target BU (each component's burden; all of them weigh 5 BU in the 1999 world).
3. Floors per category (scarabs, herbs, powders, potions, talismans, tapers): no component a spell you KNOW
   uses is kept below its category's minimum, even one that seldom burns (scarabs) or one you haven't cast lately.
   The floors come out of the same budget: the factor shrinks to fit them.

Per component, yours to set: excluded (no level at all, whatever uses it), an override (its own minimum, in place of its category's: a floor like the others,
inside the budget; one set on a component you don't burn still stocks it) and an adjustment (added to what's
recommended, after the scaling: outside the budget, and it can be negative).

The level is the recommendation plus the adjustment; buy = level - carried (the Comps page's buy and sell lists).
"""
from collections import Counter

CHECKPOINTS = [1, 4, 12, 24, 72, 168, 336, 720]   # the quick buttons: hours back from now the start can be set to
DEFAULT_HOURS = 168           # with no start set: the last week
BURDEN = 5                    # BU per component (every SpellComponent weenie's EncumbranceVal)
CATEGORIES = ["Scarabs", "Herbs", "Powders", "Potions", "Talismans", "Tapers"]   # the component table's 0-5
DEFAULT_TARGET_BU = 5000
DEFAULT_MIN = {"Scarabs": 10, "Herbs": 10, "Powders": 10, "Potions": 10, "Talismans": 10, "Tapers": 20}


def category(comp):
    c = (comp or {}).get("category")
    return CATEGORIES[c] if isinstance(c, int) and 0 <= c < len(CATEGORIES) else None


def since_of(value):
    """A start time (epoch seconds) from prefs, or None (not set, or not a time)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def settings(prefs):
    """{"target_bu", "min": {category: n}, "since"} from prefs "restock", defaults filled in (since: None, not set)."""
    got = (prefs or {}).get("restock") or {}
    mins = dict(DEFAULT_MIN, **{k: v for k, v in (got.get("min") or {}).items() if k in DEFAULT_MIN})
    return {"target_bu": got.get("target_bu") or DEFAULT_TARGET_BU, "min": mins,
            "since": since_of(got.get("since")),
            "override": dict(got.get("override") or {}), "adjust": dict(got.get("adjust") or {}),
            "exclude": set(got.get("exclude") or [])}


ROMAN = {"I", "II", "III", "IV", "V", "VI", "VII", "VIII"}


def spell_class(name):
    """A spell's family: its name without the tier ("Strength Self III" -> "Strength Self")."""
    words = str(name or "").split(" ")
    return " ".join(words[:-1]) if len(words) > 1 and words[-1] in ROMAN else str(name or "")


def recent_burns(casts, since):
    """(Counter of the components burned by casts from `since` on, Counter of those casts by spell): one walk back
    from the newest. casts are oldest first (the store keeps them so), so only the end of the history is read."""
    counts, spells = Counter(), Counter()
    for c in reversed(casts):
        if (c.get("ts") or 0) < since:
            break
        if c.get("spell_id"):
            spells[c["spell_id"]] += 1
        for name in c.get("components") or []:
            counts[name] += 1
    return counts, spells


def checkpoint_burns(casts, now):
    """[[hours, components burned in the last that many hours]] for each checkpoint (the quick buttons)."""
    out, total, i = [], 0, 0
    for c in reversed(casts):
        age = (now - (c.get("ts") or 0)) / 3600
        while i < len(CHECKPOINTS) and age > CHECKPOINTS[i]:
            out.append([CHECKPOINTS[i], total])
            i += 1
        if i == len(CHECKPOINTS):
            break
        total += len(c.get("components") or [])
    out += [[h, total] for h in CHECKPOINTS[i:]]
    return out


def levels(counts, used, floor_of, target_bu, burden=BURDEN):
    """{name: level} for every component in `used`: max(its floor, round(k x burned)), the largest k that keeps
    the whole order within target_bu."""
    names = sorted(used)
    counts = {n: counts.get(n, 0) for n in names}

    def order(k):
        return {n: max(floor_of(n), round(k * counts.get(n, 0))) for n in names}

    def weight(k):
        return sum(v for v in order(k).values()) * burden

    total = sum(counts.values())
    if not names or not total or weight(0) >= target_bu:
        return order(0)
    lo, hi = 0.0, target_bu / burden / total * 2 + 1
    while weight(hi) <= target_bu:      # (floors only push it up: make sure hi overshoots)
        hi *= 2
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if weight(mid) <= target_bu else (lo, mid)
    return order(lo)


def plan(casts, components, required, prefs, known=(), spell_name=None, now=None):
    """({name: level}, info) for this character: casts are its stored casts ({ts, spell_id, components}),
    components the component table ({id: {name, category}}), required(spell id) -> {component: count} its
    formula, known the spells you know, spell_name(id) their names, now the time. info: {window: burns counted,
    since: when they're counted from, since_set: whether that's yours (else the last week), checkpoints:
    checkpoint_burns, target_bu, min, total_bu, burned, recommended,
    override, adjust, exclude, uses: {component: [[spell family, casts in the window]], most cast first}}."""
    cfg = settings(prefs)
    now = (casts[-1].get("ts") or 0) if now is None and casts else (now or 0)
    since = cfg["since"] if cfg["since"] is not None else now - DEFAULT_HOURS * 3600
    counts, spells = recent_burns(casts, since)
    cat = {c.get("name"): category(c) for c in components.values()}
    # what uses each component: the spells you know (by family), with their casts in the window
    family = Counter()
    uses = {}
    for sid in known:
        name = spell_name(sid) if spell_name else str(sid)
        fam = spell_class(name)
        family[fam] += spells.get(sid, 0)
        for comp in required(sid) or {}:
            uses.setdefault(comp, set()).add(fam)
    uses = {comp: sorted(([f, family[f]] for f in fams), key=lambda u: (-u[1], u[0])) for comp, fams in uses.items()}
    used = (set(uses) | set(counts) | set(cfg["override"])) - cfg["exclude"]
    used = {n for n in used if n in cat}
    over, adjust = cfg["override"], cfg["adjust"]
    rec = levels(counts, used, lambda n: over[n] if n in over else cfg["min"].get(cat.get(n), 0), cfg["target_bu"])
    lv = {n: max(0, v + adjust.get(n, 0)) for n, v in rec.items()}
    return lv, {"window": sum(counts.values()), "since": since, "since_set": cfg["since"] is not None,
                "checkpoints": checkpoint_burns(casts, now),
                "target_bu": cfg["target_bu"], "min": cfg["min"], "recommended": rec,
                "override": over, "adjust": adjust, "exclude": sorted(cfg["exclude"]), "uses": uses,
                "total_bu": sum(lv.values()) * BURDEN, "burned": dict(counts)}
