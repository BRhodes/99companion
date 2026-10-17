"""Spell component burn model, mirroring the server.

Which components a cast uses (SpellcastingManager::ReserveSpellComponents):
  the spell's base formula, personalised by SpellFormula::RandomizeVersion1(account name), which
  replaces taper slots 1/3/6 for spells with more than 5/6/7 components.
What burns (BurnUsedComponents + SpellComponentBurn.h):
  each distinct component stack independently, with chance  cdm * spell.component_loss * (1.5 if fizzled);
  a hit destroys 1..n units where n is how many of that component the formula lists (almost always 1).
Casts that fail before the skill roll (out of range, moved, missing components) burn nothing.
"""

from collections import Counter

from .dat import compute_hash

LOWEST_TAPER_ID = 63
FIZZLE_MULT = 1.5


def _int32(x):
    return ((x + 0x80000000) & 0xFFFFFFFF) - 0x80000000


def _cdiv(a, b):
    """C integer division (truncates toward zero)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def _cmod(a, b):
    """C % (sign follows the dividend)."""
    return a - _cdiv(a, b) * b


def personal_formula(base, account):
    """SpellFormula::RandomizeVersion1, line for line (Server/Source/PhatSDK/SpellTable.cpp:47)."""
    comps = list(base)
    n = sum(1 for c in comps if c)
    seed = compute_hash(account) % 0x13D573
    accent1 = accent2 = accent3 = 0

    v8 = comps[0]
    v9 = 1
    if n > 5:
        v9 = 2
        accent1 = 2
    v10 = comps[v9]
    v11 = v9 + 1
    a = v10
    if n > 6:
        v11 += 1
        accent2 = 1
    v16 = comps[v11] if 0 <= v11 < 8 else 0
    v12 = v11 + 1
    v3 = comps[v12] if 0 <= v12 < 8 else 0
    v13 = v12 + 1
    x = v3
    if n > 7:
        v13 += 1
        accent3 = 1
    v14 = comps[v13] if 0 <= v13 < 8 else 0

    if accent1:
        if not (v8 + v10):
            v8 = 1
        comps[1] = _cmod(_int32(v8 + v16 + v3 + v14 + 2 * v10), 0xC) + LOWEST_TAPER_ID
    if accent2:
        v15 = v16 + v3
        if not (v8 + v15):
            v8 = 1
        comps[3] = _cmod(_int32((v8 + a + v14 + 2 * v15) * _cdiv(seed, v8 + v15)), 0xC) + LOWEST_TAPER_ID
    if accent3:
        if not (v14 + v8):
            v8 = 1
        comps[6] = _cmod(_int32((v8 + a + v16 + x + 2 * v14) * _cdiv(seed, v14 + v8)), 0xC) + LOWEST_TAPER_ID
    return comps


def _mean_units(n):
    """Units destroyed when a stack burns: 1..n, favouring the middle (mean of the range)."""
    return (1 + n) / 2.0


class BurnModel:
    def __init__(self, spells, components):
        self.spells = spells
        self.components = components
        self.account = None
        self._cache = {}

    def set_account(self, account):
        if account != self.account:
            self.account = account
            self._cache.clear()

    def formula(self, spell_id):
        """Component ids (with repeats) this account's cast of the spell reserves, or None if unknown."""
        if spell_id in self._cache:
            return self._cache[spell_id]
        spell = self.spells.get(spell_id)
        base = spell and spell.get("formula")
        if not base:
            result = None
        else:
            comps = personal_formula(base, self.account) if self.account else list(base)
            result = [c for c in comps if c]
        self._cache[spell_id] = result
        return result

    def expected_burn(self, spell_id, fizzled=False):
        """{component name: expected units burned} for one cast."""
        comps = self.formula(spell_id)
        spell = self.spells.get(spell_id)
        if not comps or not spell:
            return {}
        loss = spell.get("component_loss", 0.0)
        out = {}
        for cid, n in Counter(comps).items():
            base = self.components.get(cid)
            if not base:
                continue
            chance = base["cdm"] * loss * (FIZZLE_MULT if fizzled else 1.0)
            out[base["name"]] = out.get(base["name"], 0.0) + chance * _mean_units(n)
        return out

    def required(self, spell_id):
        """{component name: units that must be carried to cast at all}."""
        comps = self.formula(spell_id) or []
        out = Counter()
        for cid in comps:
            base = self.components.get(cid)
            if base:
                out[base["name"]] += 1
        return dict(out)

    def casts_left(self, spell_id, inventory, fizzle_rate=0.0):
        """Expected casts before some required component runs out (inf if nothing burns), or 0 if
        a required component is already missing. Returns (casts, limiting component)."""
        need = self.required(spell_id)
        if not need:
            return None, None
        for name, units in need.items():
            if inventory.get(name, 0) < units:
                return 0, name
        per_cast = Counter()
        for fizzled, weight in ((False, 1.0 - fizzle_rate), (True, fizzle_rate)):
            for name, units in self.expected_burn(spell_id, fizzled).items():
                per_cast[name] += units * weight
        best, limiting = float("inf"), None
        for name, units in per_cast.items():
            if units <= 0:
                continue
            # usable units are what's carried beyond the minimum the formula needs on hand
            casts = (inventory.get(name, 0) - need[name] + 1) / units
            if casts < best:
                best, limiting = casts, name
        return best, limiting
