"""Session bookkeeping for spell components: what burned, what the model expected, what's left.

A cast is settled once its outcome is known:
  success / resisted  -> normal burn chance
  fizzled (0x402)     -> burn chance x1.5
  failed otherwise    -> nothing burns (out of range, moved, missing components, ...)
The server reports burned components as text before the outcome arrives, so observed burns are
attached to the open cast and the model's expectation is added when it settles.
"""

from collections import Counter, defaultdict

SUCCESS, FIZZLED, FAILED, REJECTED = "success", "fizzled", "failed", "rejected"


def _spell_stats():   # a named function, not a lambda, so the ledger can be checkpointed (pickled)
    return {"casts": 0, "fizzled": 0, "failed": 0, "observed": Counter(), "expected": Counter()}


class ComponentLedger:
    def __init__(self, model):
        self.model = model
        self.observed = Counter()          # component name -> units burned this session
        self.expected = Counter()          # component name -> units the model expected
        self.by_spell = defaultdict(_spell_stats)

    def burned(self, cast, names):
        """Burn text from the server (one name per unit)."""
        self.observed.update(names)
        if cast is not None:
            cast["components"].extend(names)
            self.by_spell[cast["spell_id"]]["observed"].update(names)

    def settle(self, cast, outcome):
        if cast is None or cast.get("settled"):
            return
        cast["settled"] = True
        if outcome == REJECTED:
            return  # refused before casting: not a cast, nothing burns
        stats = self.by_spell[cast["spell_id"]]
        stats["casts"] += 1
        if outcome == FAILED:
            stats["failed"] += 1
            return
        if outcome == FIZZLED:
            stats["fizzled"] += 1
        expected = self.model.expected_burn(cast["spell_id"], fizzled=outcome == FIZZLED)
        cast["expected_burn"] = round(sum(expected.values()), 3)
        for name, units in expected.items():
            self.expected[name] += units
            stats["expected"][name] += units

    def snapshot(self, inventory, hours, spell_names):
        rate = (lambda n: n / hours) if hours and hours > 0.01 else (lambda n: None)
        names = set(self.observed) | set(self.expected)
        for spell_id in self.by_spell:
            names.update(self.model.required(spell_id))
        components = []
        for name in sorted(names):
            per_hour = rate(self.observed[name]) or rate(self.expected[name])
            carried = inventory.get(name)
            components.append({
                "name": name,
                "carried": carried,
                "used": self.observed[name],
                "expected": round(self.expected[name], 2),
                "per_hour": None if per_hour is None else round(per_hour, 2),
                "per_hour_expected": None if rate(self.expected[name]) is None else round(rate(self.expected[name]), 2),
                "hours_left": None if not per_hour or carried is None else round(carried / per_hour, 1),
            })
        spells = []
        for spell_id, st in self.by_spell.items():
            settled = st["casts"] - st["failed"]
            fizzle_rate = st["fizzled"] / settled if settled else 0.0
            left, limiting = self.model.casts_left(spell_id, inventory, fizzle_rate)
            spells.append({
                "spell": spell_names.get(spell_id, f"Spell {spell_id}"),
                "casts": st["casts"], "fizzled": st["fizzled"], "failed": st["failed"],
                "used": sum(st["observed"].values()),
                "expected": round(sum(st["expected"].values()), 2),
                "per_cast_expected": round(sum(self.model.expected_burn(spell_id).values()), 3),
                "casts_left": None if left is None else (None if left == float("inf") else int(left)),
                "unlimited": left == float("inf"),
                "limiting": limiting,
                "formula": [self.model.components.get(c, {}).get("name", str(c))
                            for c in (self.model.formula(spell_id) or [])],
            })
        spells.sort(key=lambda s: -s["casts"])
        return {"account_known": self.model.account is not None, "components": components, "spells": spells}
