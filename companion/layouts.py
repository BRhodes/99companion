"""Per-character spellbook layouts (pages of spell slots), stored in one JSON file."""

import json
import threading
from pathlib import Path

from . import comporder, storetint

SLOTS_PER_PAGE = 24
MAX_PAGES = 12
DEFAULT = {"pages": [{"name": "Spells", "slots": [None] * SLOTS_PER_PAGE}]}


def _clean(layout):
    """Validate a layout from the browser: bounded pages, names and slot values."""
    pages = []
    for page in (layout or {}).get("pages", [])[:MAX_PAGES]:
        name = str(page.get("name") or "Page")[:40]
        slots = list(page.get("slots") or [])[:SLOTS_PER_PAGE]
        slots = [s if isinstance(s, int) and 0 < s < 0x100000 else None for s in slots]
        slots += [None] * (SLOTS_PER_PAGE - len(slots))
        pages.append({"name": name, "slots": slots})
    return {"pages": pages or json.loads(json.dumps(DEFAULT["pages"]))}




def _goals(got):
    """The Experience page's goals: [{name, targets: {"attribute:5" / "vital:2" / "skill:33": value}, start (the XP
    it took when it was set: the dashboard's progress)}], at most 20, 40 targets each."""
    out = []
    for g in (got if isinstance(got, list) else [])[:20]:
        if not isinstance(g, dict):
            continue
        targets = {}
        for key, value in list((g.get("targets") or {}).items())[:40]:
            kind, _, sid = str(key).partition(":")
            if kind in ("attribute", "vital", "skill") and sid.isdigit() and isinstance(value, (int, float)) and 0 < value < 100000:
                targets[f"{kind}:{int(sid)}"] = int(value)
        start = g.get("start") if isinstance(g.get("start"), (int, float)) and g.get("start") >= 0 else None
        out.append({"name": str(g.get("name") or "Goal")[:40], "targets": targets, "start": start,
                    "buffed": g.get("buffed") is True, "kind": "next" if g.get("kind") == "next" else None})
    return out


def _per_component(got, low):
    """{component name: whole number} (an override or adjustment each), at most 200, dropping the unset ones."""
    out = {}
    for name, v in list((got if isinstance(got, dict) else {}).items())[:200]:
        try:
            v = max(low, min(99999, int(v)))
        except (TypeError, ValueError):
            continue
        if v or low == 0:
            out[str(name)[:40]] = v
    return out


def _clean_profile(p):
    """One buff profile, cleaned: {name, buffs: [{category, spell, name, section, critical, level}], max_chance,
    crit_memory, bane_targets}, or None without a name."""
    if not isinstance(p, dict) or not str(p.get("name") or "").strip():
        return None
    buffs, seen = [], set()
    for b in (p.get("buffs") or [])[:80]:
        try:
            category, spell = int(b["category"]), int(b["spell"])
        except (KeyError, TypeError, ValueError):
            continue
        if category in seen:
            continue
        seen.add(category)
        level = b.get("level")       # a tier 1-7, or "relax" / "max" (worked out from your skill when cast)
        if level not in ("relax", "max"):
            level = level if isinstance(level, int) and 1 <= level <= 7 else None
        buffs.append({"category": category, "spell": spell, "name": str(b.get("name") or "")[:60],
                      "section": str(b.get("section") or "")[:20], "critical": bool(b.get("critical")),
                      "level": level})
    try:
        max_chance = min(0.99, max(0.05, float(p.get("max_chance", 0.3))))
    except (TypeError, ValueError):
        max_chance = 0.3
    # buffs taken out of the profile while critical: the mark comes back if they're kept again
    remembered = sorted({c for c in (p.get("crit_memory") or []) if isinstance(c, int) and c not in seen})[:200]
    # which items the banes go on (all of them count): armor by default
    targets = [s for s in ("armor", "underclothes", "shield") if s in (p.get("bane_targets") or ["armor"])]
    return {"name": str(p["name"]).strip()[:40], "buffs": buffs, "max_chance": max_chance,
            "crit_memory": remembered, "bane_targets": targets or ["armor"]}


def clean_prefs(prefs):
    """Per-character dashboard preferences: skills hidden from the XP timers, and buff profiles (named lists
    of buffs to keep up, by spell category, each optionally critical) with the one in use."""
    prefs = prefs or {}
    hidden = prefs.get("hidden_skills") or []
    profiles = [_clean_profile(p) for p in (prefs.get("buff_profiles") or [])[:20]]
    profiles = [p for p in profiles if p]
    # the items buffs go on: weapons (Blood Drinker...) and shields (banes), picked on the config page
    items = {}
    for kind in ("weapons", "shields"):
        picked = []
        for it in ((prefs.get("items") or {}).get(kind) or [])[:20]:
            try:
                picked.append({"id": int(it["id"]), "name": str(it.get("name") or "")[:60]})
            except (KeyError, TypeError, ValueError):
                continue
        items[kind] = picked
    # the component order (comporder.py): its target weight and each category's floor
    got = prefs.get("restock") if isinstance(prefs.get("restock"), dict) else {}
    try:
        target_bu = max(100, min(200000, int(got.get("target_bu", comporder.DEFAULT_TARGET_BU))))
    except (TypeError, ValueError):
        target_bu = comporder.DEFAULT_TARGET_BU
    mins = {}
    for cat, default in comporder.DEFAULT_MIN.items():
        try:
            mins[cat] = max(0, min(9999, int((got.get("min") or {}).get(cat, default))))
        except (TypeError, ValueError):
            mins[cat] = default
    names = [p["name"] for p in profiles]
    active = prefs.get("active_profile") if prefs.get("active_profile") in names else None
    return {"hidden_skills": sorted({s for s in hidden if isinstance(s, int) and 0 < s < 1000}),
            "buff_profiles": profiles, "active_profile": active, "items": items,
            "copy_appraisals": prefs.get("copy_appraisals") is True,     # an item you appraise onto the clipboard
            "goals": _goals(prefs.get("goals")),
            "goal_tracked": prefs.get("goal_tracked") if isinstance(prefs.get("goal_tracked"), int) else None,
            "restock": {"target_bu": target_bu, "min": mins, "since": comporder.since_of(got.get("since")),
                        "target_mode": "capacity" if got.get("target_mode") == "capacity" else "bu",
                        "target_pct": max(1, min(100, int(got.get("target_pct") or 50))) if str(got.get("target_pct") or 50).isdigit() else 50,
                        # a vendor's window (the relay's --tint-store): the item type, so background, for the list's and the rest
                        "tint_buy": got.get("tint_buy") if got.get("tint_buy") in storetint.BACKGROUNDS else storetint.COMPONENT,
                        "tint_other": got.get("tint_other") if got.get("tint_other") in storetint.BACKGROUNDS else storetint.DEFAULT_OTHER,
                        "override": _per_component(got.get("override"), 0), "adjust": _per_component(got.get("adjust"), -99999),
                        "exclude": sorted({str(n)[:40] for n in (got.get("exclude") or [])[:200] if isinstance(n, str)})}}


class LayoutStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def get(self, character):
        return self.data.get(character) or _clean(DEFAULT)

    def put(self, character, layout):
        clean = _clean(layout)
        self._save(character, clean)
        return clean

    def get_prefs(self, character):
        return clean_prefs(self.data.get("prefs:" + character))

    def put_prefs(self, character, prefs):
        clean = clean_prefs(prefs)
        self._save("prefs:" + character, clean)
        return clean

    def _save(self, key, value):
        with self.lock:
            self.data[key] = value
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            tmp.replace(self.path)
