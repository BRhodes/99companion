"""The component order: what you burned in a stretch of time, scaled to a target weight, each category's floor out
of the budget."""

import unittest

from companion import comporder

COMPONENTS = {1: {"name": "Lead Scarab", "category": 0}, 2: {"name": "Hyssop", "category": 1},
              3: {"name": "Red Taper", "category": 5}, 4: {"name": "Cobalt", "category": 3}}


def casts(*burns):
    return [{"ts": i, "spell_id": 100, "components": list(b)} for i, b in enumerate(burns)]


class OrderTest(unittest.TestCase):
    def test_only_the_casts_in_the_window_count(self):
        history = [{"ts": 0, "spell_id": 1, "components": ["Hyssop"] * 5}, {"ts": 3600, "spell_id": 2, "components": ["Red Taper"] * 5}]
        counts, spells = comporder.recent_burns(history, since=1800)
        self.assertEqual((counts, spells), ({"Red Taper": 5}, {2: 1}))

    def test_scaled_to_the_target_weight_in_proportion(self):
        lv, info = comporder.plan(casts(["Hyssop"] * 300, ["Red Taper"] * 100), COMPONENTS, lambda s: {},
                                  {"restock": {"target_bu": 2000, "min": {"Herbs": 0, "Tapers": 0}}})
        self.assertEqual(lv, {"Hyssop": 300, "Red Taper": 100})       # 400 components x 5 BU
        self.assertEqual(info["total_bu"], 2000)

    def test_floors_come_out_of_the_budget(self):
        need = {100: {"Lead Scarab": 1, "Hyssop": 1}}
        lv, info = comporder.plan(casts(["Hyssop"] * 300, ["Red Taper"] * 100), COMPONENTS, need.get,
                                  {"restock": {"target_bu": 2000, "min": {"Scarabs": 20, "Herbs": 0, "Tapers": 0}}},
                                  known=[100])
        self.assertEqual(lv["Lead Scarab"], 20)                       # never burned, but a spell you know uses it
        self.assertLessEqual(info["total_bu"], 2000)
        self.assertGreater(lv["Hyssop"], 2.5 * lv["Red Taper"])       # the mix kept, a little smaller
        self.assertNotIn("Cobalt", lv)                                # nothing you cast uses it

    def test_counted_from_a_fixed_start(self):
        hour = 3600
        history = [{"ts": 0, "components": ["Hyssop"] * 300}, {"ts": 47 * hour, "components": ["Red Taper"] * 200}]
        _, info = comporder.plan(history, COMPONENTS, lambda s: {}, {"restock": {"since": 24 * hour}}, now=48 * hour)
        self.assertEqual((info["since"], info["since_set"], info["window"]), (24 * hour, True, 200))   # just the tapers
        _, info = comporder.plan(history, COMPONENTS, lambda s: {}, {}, now=48 * hour)
        self.assertEqual((info["since"], info["since_set"], info["window"]), (-120 * hour, False, 500))  # not set: the last week
        self.assertIsNone(comporder.since_of("x"))

    def test_an_override_is_its_own_floor_and_an_adjustment_goes_on_top(self):
        history = casts(["Hyssop"] * 300, ["Red Taper"] * 100)
        cfg = {"target_bu": 2000, "min": {"Herbs": 0, "Tapers": 0}, "override": {"Red Taper": 150, "Cobalt": 5},
               "adjust": {"Hyssop": 25}}
        lv, info = comporder.plan(history, COMPONENTS, lambda s: {}, {"restock": cfg})
        self.assertEqual(lv["Red Taper"], 150)                        # its own minimum, over its share
        self.assertEqual(lv["Cobalt"], 5)                             # not burned, stocked for the override
        self.assertEqual(lv["Hyssop"], info["recommended"]["Hyssop"] + 25)
        self.assertEqual(sum(info["recommended"].values()) * 5, 2000)   # overrides inside the budget, adjustments not

    def test_every_known_spells_components_unless_excluded_with_what_uses_them(self):
        need = {100: {"Lead Scarab": 1, "Hyssop": 1}, 101: {"Cobalt": 1, "Hyssop": 1}, 102: {"Cobalt": 1}}
        names = {100: "Strength Self I", 101: "Heal Self II", 102: "Heal Self III"}
        history = casts(["Hyssop"] * 50) + [{"ts": 99, "spell_id": 101, "components": []}] * 3
        lv, info = comporder.plan(history, COMPONENTS, need.get, {"restock": {"exclude": ["Lead Scarab"]}},
                                  known=[100, 101, 102], spell_name=names.get)
        self.assertIn("Cobalt", lv)                                   # known, never cast: its floor
        self.assertNotIn("Lead Scarab", lv)                           # excluded
        self.assertEqual(info["uses"]["Hyssop"], [["Heal Self", 3], ["Strength Self", 1]])   # most cast first
        self.assertEqual(info["uses"]["Cobalt"], [["Heal Self", 3]])                          # one family for both tiers

    def test_floors_past_the_target_are_just_the_floors(self):
        lv, _ = comporder.plan(casts(["Hyssop"] * 10), COMPONENTS, lambda s: {}, {"restock": {"target_bu": 10}})
        self.assertEqual(lv, {"Hyssop": comporder.DEFAULT_MIN["Herbs"]})


if __name__ == "__main__":
    unittest.main()


class CheckpointBurnsTest(unittest.TestCase):
    def test_burns_within_each_checkpoint(self):
        hour = 3600
        history = [{"ts": 0, "components": ["Hyssop"] * 3}, {"ts": 70 * hour, "components": ["Hyssop"] * 2},
                   {"ts": 99.5 * hour, "components": ["Hyssop"]}]
        got = dict(comporder.checkpoint_burns(history, now=100 * hour))
        self.assertEqual((got[1], got[24], got[72], got[168]), (1, 1, 3, 6))
