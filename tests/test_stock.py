"""Component stock levels from cast history: active-time rates, recency, burn model, floors."""

import unittest

from companion import stock


class FakeModel:
    """Spell 1 burns 0.5 Taper a cast (0.75 fizzled) and needs a Scarab on hand; spell 2 needs 9 Herbs."""
    def expected_burn(self, sid, fizzled=False):
        if sid == 1:
            return {"Taper": 0.75 if fizzled else 0.5, "Scarab": 0.0}
        return {"Herb": 0.1}

    def required(self, sid):
        return {"Taper": 1, "Scarab": 1} if sid == 1 else {"Herb": 9}


def casts(spell, start, every, n, result="cast"):
    return [{"ts": start + i * every, "spell_id": spell, "result": result} for i in range(n)]


class StockTest(unittest.TestCase):
    def test_idle_time_does_not_dilute_the_rate(self):
        busy = casts(1, 0, 10, 61)                        # one cast every 10 s for 10 minutes: 360/h
        afk = busy + casts(1, 600 + 7200, 10, 61)         # the same again after two hours AFK
        self.assertAlmostEqual(stock.spell_rates(busy)[0][1][0], 360, delta=40)
        # the AFK gap counts as one idle gap (5 min) of casting, not two hours: ~290/h, not ~55/h
        self.assertGreater(stock.spell_rates(afk)[0][1][0], 250)

    def test_recent_casting_counts_more(self):
        old = casts(1, 0, 10, 3600)                       # 10 active hours of spell 1...
        new = casts(2, 36000, 10, 3600)                   # ...then 10 of spell 2
        rates, _ = stock.spell_rates(old + new)
        self.assertGreater(rates[2][0], rates[1][0] * 1.5)

    def test_rejected_and_failed_casts_do_not_count(self):
        rates, _ = stock.spell_rates(casts(1, 0, 10, 10, "failed (0x401)"))
        self.assertEqual(rates, {})

    def test_targets_use_the_model_fizzles_and_the_floors(self):
        history = casts(1, 0, 36, 75) + casts(1, 2700, 36, 25, "fizzled") + casts(2, 3600, 600, 2)
        plan, _ = stock.plan(history, FakeModel(), {"Taper": 40, "Herb": 3})
        taper = plan["Taper"]
        # ~100 casts/h, a quarter fizzled: 100 x (0.75 x 0.5 + 0.25 x 0.75) = 56 Tapers an hour
        self.assertAlmostEqual(taper["per_hour"], 56, delta=6)
        self.assertEqual(taper["target"], 7)               # just the floor now (the levels are learned: restock.py)
        self.assertEqual(plan["Scarab"]["target"], 7)    # never burns, still kept at the minimum of 7
        self.assertEqual(plan["Herb"]["target"], 10)     # one cast needs 9 on hand: 9 + 1
        self.assertEqual(plan["Herb"]["buy"], 7)



if __name__ == "__main__":
    unittest.main()
