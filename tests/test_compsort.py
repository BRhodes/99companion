"""Components sorted into the side packs: the right order, with the fewest moves (longest increasing subsequence)."""

import random
import unittest

from companion import compsort

COMPONENTS = {i: {"name": n, "category": c} for i, (n, c) in enumerate([
    ("Pyreal Scarab", 0), ("Lead Scarab", 0), ("Iron Scarab", 0), ("Hyssop", 1), ("Amaranth", 1),
    ("Powdered Onyx", 2), ("Cobalt", 3), ("Oak Talisman", 4), ("Red Taper", 5), ("Blue Taper", 5)])}
RANK = compsort.component_rank(COMPONENTS)
ORDER = ["Lead Scarab", "Iron Scarab", "Pyreal Scarab", "Amaranth", "Hyssop", "Powdered Onyx", "Cobalt",
         "Oak Talisman", "Blue Taper", "Red Taper"]


def apply(lists, moves):
    """What the server does with each move: out of where it was, in at the slot (clamped)."""
    for g, container, slot in moves:
        for items in lists.values():
            if g in items:
                items.remove(g)
        lists[container].insert(min(slot, len(lists[container])), g)
    return lists


def run(main, packs):
    is_comp = lambda g: g.split("#")[0] in RANK
    key = lambda g: (RANK[g.split("#")[0]], g)
    moves = compsort.plan(("main", main), [(p, cap, items) for p, cap, items in packs], is_comp, key)
    lists = apply({"main": list(main), **{p: list(items) for p, _, items in packs}}, moves)
    return moves, lists


class PlanTest(unittest.TestCase):
    def test_lis(self):
        self.assertEqual(len(compsort.lis([3, 1, 2, 5, 4, 6])), 4)
        self.assertEqual(compsort.lis([]), [])

    def test_the_order(self):
        self.assertEqual(sorted(RANK, key=RANK.get), ORDER)

    def test_already_sorted_moves_nothing(self):
        moves, lists = run(["Sword"], [("A", 24, list(ORDER))])
        self.assertEqual(moves, [])

    def test_one_out_of_place_is_one_move(self):
        shuffled = ORDER[:3] + ["Red Taper"] + ORDER[3:9]          # Red Taper early: one move
        moves, lists = run([], [("A", 24, shuffled)])
        self.assertEqual(len(moves), 1)
        self.assertEqual(lists["A"], ORDER)

    def test_from_the_main_pack_and_others_after(self):
        moves, lists = run(["Hyssop", "Sword", "Lead Scarab"], [("A", 24, ["Bow", "Cobalt"])])
        self.assertEqual(lists["A"], ["Lead Scarab", "Hyssop", "Cobalt", "Bow"])
        self.assertEqual(lists["main"], ["Sword"])

    def test_spills_into_the_next_pack(self):
        comps = list(reversed(ORDER))
        moves, lists = run(comps, [("A", 4, ["Bow"]), ("B", 24, [])])
        self.assertEqual(lists["A"], ORDER[:3] + ["Bow"])           # 3 fit beside the bow
        self.assertEqual(lists["B"], ORDER[3:])

    def test_overflow_stays_in_the_main_pack(self):
        moves, lists = run(list(ORDER), [("A", 3, [])])
        self.assertEqual(lists["A"], ORDER[:3])
        self.assertEqual(lists["main"], ORDER[3:])

    def test_random_layouts_end_sorted_with_minimal_moves_in_one_pack(self):
        rnd = random.Random(7)
        for _ in range(200):
            items = list(ORDER) + ["Bow", "Sword"]
            rnd.shuffle(items)
            moves, lists = run([], [("A", 24, items)])
            self.assertEqual(lists["A"][:10], ORDER)
            target = ORDER + [g for g in items if g in ("Bow", "Sword")]
            pos = {g: n for n, g in enumerate(target)}
            self.assertEqual(len(moves), len(items) - len(compsort.lis([pos[g] for g in items])))

    def test_random_layouts_across_packs(self):
        rnd = random.Random(11)
        for _ in range(200):
            things = [f"{n}#{k}" for k in range(2) for n in ORDER] + ["Bow", "Sword", "Mace"]
            rnd.shuffle(things)
            cut1, cut2 = sorted(rnd.sample(range(len(things)), 2))
            packs = [("A", 12, things[:cut1][:12]), ("B", 12, things[cut1:cut2][:12])]
            used = set(packs[0][2]) | set(packs[1][2])
            main = [g for g in things if g not in used]
            moves, lists = run(main, packs)
            comps_in_packs = [g for p in ("A", "B") for g in lists[p] if "#" in g]
            ordered = sorted((g for g in things if "#" in g), key=lambda g: (RANK[g.split("#")[0]], g))
            self.assertEqual(comps_in_packs, ordered[:len(comps_in_packs)])
            for p in ("A", "B"):
                self.assertLessEqual(len(lists[p]), 12)
                inside = lists[p]
                first_other = next((i for i, g in enumerate(inside) if "#" not in g), len(inside))
                self.assertTrue(all("#" not in g for g in inside[first_other:]))    # components first


if __name__ == "__main__":
    unittest.main()


class SwapTest(unittest.TestCase):
    def test_two_full_packs_swapped_take_one_move_each_plus_one(self):
        comps = [f"{n}#{k}" for k in range(5) for n in ORDER][:48]          # 48 components, sorted
        ordered = sorted(comps, key=lambda g: (RANK[g.split("#")[0]], g))
        a, b = ordered[:24], ordered[24:]                                  # where they belong: A then B
        moves, lists = run([], [("A", 24, list(b)), ("B", 24, list(a))])  # ...but the packs are swapped
        self.assertEqual(lists["A"], a)
        self.assertEqual(lists["B"], b)
        self.assertLessEqual(len(moves), 48 + 1)                           # each once, one of them twice (aside)

    def test_one_free_slot_needs_no_aside(self):
        comps = sorted([f"{n}#{k}" for k in range(5) for n in ORDER][:47], key=lambda g: (RANK[g.split("#")[0]], g))
        a, b = comps[:24], comps[24:]
        moves, lists = run([], [("A", 24, list(b)), ("B", 24, list(a))])   # B has a free slot
        self.assertEqual((lists["A"], lists["B"]), (a, b))
        self.assertEqual(len(moves), 47)
        self.assertNotIn("main", [c for _, c, _ in moves])


class CompStepsTest(unittest.TestCase):
    def tr(self, objects, lists):
        class Tr:
            pass
        tr = Tr()
        tr.player, tr.components = "me", COMPONENTS
        tr.pack_order = ["P1", "P2"]
        tr.objects = {"P1": {"name": "Pack", "items_capacity": 3}, "P2": {"name": "Pack"}, **objects}
        tr.pack_contents = lambda c: list(lists[c])
        return tr

    def test_only_the_wrong_pack_not_the_order_in_it(self):
        tr = self.tr({"a": {"name": "Hyssop", "stack": 12}, "b": {"name": "Lead Scarab"}, "c": {"name": "Iron Scarab"},
                      "sword": {"name": "Sword"}},
                     {"me": ["a", "sword"], "P1": [], "P2": ["c", "b"]})      # Iron before Lead: out of order, same pack
        self.assertEqual(compsort.comp_steps(tr), [{"kind": "move", "name": "Hyssop", "stack": 12, "from": "main pack", "to": "side pack 2", "slot": 1}])

    def test_every_pack_even_the_4th_6th_and_7th(self):
        tr = self.tr({"a": {"name": "Lead Scarab"}}, {"me": ["a"], **{f"P{n}": [] for n in range(1, 8)}})
        tr.pack_order = [f"P{n}" for n in range(1, 8)]
        self.assertEqual([s["to"] for s in compsort.comp_steps(tr)], ["side pack 7"])

    def test_filled_from_the_last_pack_up(self):
        tr = self.tr({"a": {"name": "Lead Scarab"}}, {"me": [], "P1": ["a"], "P2": []})
        self.assertEqual([(s["from"], s["to"]) for s in compsort.comp_steps(tr)], [("side pack 1", "side pack 2")])

    def test_a_move_says_where_and_merging_waits_till_theyre_there(self):
        tr = self.tr({"a": {"name": "Hyssop", "stack": 2, "max_stack": 100}, "b": {"name": "Hyssop", "stack": 1, "max_stack": 100}},
                     {"me": ["a", "b"], "P1": [], "P2": []})
        self.assertEqual(compsort.comp_steps(tr), [
            {"kind": "move", "name": "Hyssop", "stack": 2, "from": "main pack", "to": "side pack 2", "slot": 1},
            {"kind": "move", "name": "Hyssop", "stack": 1, "from": "main pack", "to": "side pack 2", "slot": 2}])   # just moves: not there yet
        # once both are there, they're too many stacks
        tr2 = self.tr({"a": {"name": "Hyssop", "stack": 2, "max_stack": 100}, "b": {"name": "Hyssop", "stack": 1, "max_stack": 100}},
                      {"me": [], "P1": [], "P2": ["a", "b"]})
        self.assertEqual([s["kind"] for s in compsort.comp_steps(tr2)], ["merge"])

    def test_room_counted_as_merged_and_what_still_doesnt_fit_said(self):
        # P1 holds 3: Lead Scarab's 2 stacks merge into one, which leaves room for both herbs; the taper has none
        objs = {"s1": {"name": "Lead Scarab", "stack": 5, "max_stack": 100}, "s2": {"name": "Lead Scarab", "stack": 1, "max_stack": 100},
                "h1": {"name": "Hyssop"}, "h2": {"name": "Amaranth"}, "t": {"name": "Red Taper"}}
        tr = self.tr(objs, {"me": ["t", "h1", "h2"], "P1": ["s1", "s2"], "P2": []})
        tr.pack_order = ["P1"]
        kinds = [(s["kind"], s["name"], s.get("to")) for s in compsort.comp_steps(tr)]
        self.assertEqual(kinds, [("move", "Hyssop", "side pack 1"), ("move", "Amaranth", "side pack 1"),   # in main-pack order
                                 ("merge", "Lead Scarab", "side pack 1"), ("noroom", "Red Taper", None)])

    def test_the_right_number_of_stacks_in_each_pack_is_left_be(self):
        # P2 (filled first) holds 2: Hyssop's three full-ish stacks go two there, one in P1 -- the smaller one sits in
        # P2 and a bigger one in P1, but each pack has the right number of stacks: nothing to do
        objs = {"h1": {"name": "Hyssop", "stack": 40, "max_stack": 50}, "h2": {"name": "Hyssop", "stack": 50, "max_stack": 50},
                "h3": {"name": "Hyssop", "stack": 48, "max_stack": 50}}
        tr = self.tr(objs, {"me": [], "P1": ["h2"], "P2": ["h1", "h3"]})
        tr.objects["P2"]["items_capacity"] = 2
        self.assertEqual(compsort.comp_steps(tr), [])
        # one stack too many in P2, one short in P1: just the one move, whichever it is
        tr = self.tr(objs, {"me": [], "P1": [], "P2": ["h1", "h3", "h2"]})
        tr.objects["P2"]["items_capacity"] = 2
        steps = compsort.comp_steps(tr)
        self.assertEqual([(s["kind"], s["from"], s["to"]) for s in steps], [("move", "side pack 2", "side pack 1")])

    def test_too_many_stacks(self):
        tr = self.tr({"a": {"name": "Hyssop", "stack": 2, "max_stack": 100}, "b": {"name": "Hyssop", "stack": 1, "max_stack": 100}},
                     {"me": [], "P1": [], "P2": ["a", "b"]})
        self.assertEqual(compsort.comp_steps(tr), [{"kind": "merge", "name": "Hyssop", "stacks": [2, 1],
                                                    "where": ["side pack 2", "side pack 2"], "fewer": 1, "to": "side pack 2"}])
