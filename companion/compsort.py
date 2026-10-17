"""Where spell components belong in the side packs (the Shop's Sort panel: the steps to do it by hand).

Order: scarabs by tier (Lead, Iron, Copper, Silver, Gold, Pyreal), then herbs, powders, potions,
talismans, tapers, each of those alphabetical (the component table's categories 0-5); stacks of the same
component keep their order. The side packs fill in their order in the main pack: each takes as many
components as it has room for beside the other things already in it (its capacity, else PACK_CAPACITY),
components first, the other things after them in the order they were. Components beyond what the packs
hold stay in the main pack.

plan() works out where everything ends up as the fewest moves: moving an item in game takes it out of wherever
it is and inserts it at a slot, so within a pack everything off the longest increasing subsequence (of where each
item should end up) moves once and the rest stay; a component that belongs in another pack goes straight there
when that pack has room, and only when every such move waits on a full pack does one step aside into the main pack.
"""
import bisect


PACK_CAPACITY = 24
SCARABS = ["Lead Scarab", "Iron Scarab", "Copper Scarab", "Silver Scarab", "Gold Scarab", "Pyreal Scarab"]


def component_rank(components):
    """{component name: sort key} from the component table ({id: {name, category}})."""
    out = {}
    for c in components.values():
        name, cat = c["name"], c.get("category", 9)
        out[name] = (cat, SCARABS.index(name) if name in SCARABS else 0, name)
    return out


def lis(seq):
    """Indexes of a longest strictly increasing subsequence of seq."""
    tails, tails_at, prev = [], [], [None] * len(seq)
    for i, v in enumerate(seq):
        k = bisect.bisect_left(tails, v)
        if k == len(tails):
            tails.append(v)
            tails_at.append(i)
        else:
            tails[k], tails_at[k] = v, i
        prev[i] = tails_at[k - 1] if k else None
    out, i = [], tails_at[-1] if tails_at else None
    while i is not None:
        out.append(i)
        i = prev[i]
    return out[::-1]


def plan(main, packs, is_comp, key=None, groups=None):
    """The moves, in order: [(item, container, slot)].

    main: (container id, [items]); packs: [(container id, capacity, [items])], in their order. groups: the kinds
    of things sorted, each [(is_member(item), key(item), [the packs it fills, in order])] (key: its place among
    them, with the item for ties); or is_comp / key for a single kind filling every pack."""
    if groups is None:
        groups = [(is_comp, key, [p for p, _, _ in packs])]
    member = lambda g: any(m(g) for m, _, _ in groups)
    lists = {main[0]: list(main[1]), **{p: list(items) for p, _, items in packs}}
    cap = {p: c or PACK_CAPACITY for p, c, _ in packs}
    home, target = {}, {p: [] for p, _, _ in packs}
    room = {p: cap[p] - sum(1 for g in items if not member(g)) for p, _, items in packs}   # beside the rest
    for in_group, gkey, gpacks in groups:  # each kind fills its packs in order, as many as fit
        things = sorted((g for items in lists.values() for g in items if in_group(g)), key=gkey)
        fits = sum(max(0, room[p] - len(target[p])) for p in gpacks)
        if len(things) > fits:             # more than fit: the ones already in its packs stay, then the rest in order
            there = [g for g in things if any(g in lists[p] for p in gpacks)][:fits]
            chosen = set(there) | set([g for g in things if g not in there][:fits - len(there)])
            things = [g for g in things if g in chosen]
        i = 0
        for p in gpacks:
            take = things[i:i + max(0, room[p] - len(target[p]))]
            target[p] += take
            for g in take:
                home[g] = p
            i += len(take)
    is_comp = member
    # more than their packs hold: stay where they are when that pack has a slot to spare (beside what's coming
    # to it), else the main pack; no moving them about for nothing
    spare = {p: room[p] - len(target[p]) for p in room}
    for c, items in lists.items():
        for g in items:
            if member(g) and g not in home and c in spare and spare[c] > 0:
                home[g] = c
                spare[c] -= 1
    where = {g: c for c, items in lists.items() for g in items}
    moves = []

    def move(g, container, slot_of):
        """Out of where it is, then in at slot_of() (worked out without it, as the server does)."""
        lists[where[g]].remove(g)
        slot = min(slot_of(), len(lists[container]))
        lists[container].insert(slot, g)
        where[g] = container
        moves.append((g, container, slot))

    def rank(p, g):
        """Where g belongs in pack p: its place among p's components; anything else after them, as it was."""
        return order_pos[p].get(g, len(target[p]) + (lists[p].index(g) if g in lists[p] else 0))

    def slot_in(p, g):
        """Just after the last thing in p that comes before g (so it lands in order: no second move)."""
        r = rank(p, g)
        before = [i for i, x in enumerate(lists[p]) if x != g and rank(p, x) < r]
        return before[-1] + 1 if before else 0

    order_pos = {p: {g: n for n, g in enumerate(target[p])} for p, _, _ in packs}
    main_id = main[0]

    # 1. components in the wrong place go straight home, each into its slot. A component with no pack (more
    #    than the packs hold) goes to the main pack. Only when every one left is waiting on a full pack does
    #    one step aside into the main pack: that frees a slot, and the swaps run on from there.
    for _ in range(4 * len(where) + 8):
        wrong = [g for c, items in lists.items() for g in items
                 if is_comp(g) and home.get(g, main_id) != c]
        if not wrong:
            break
        # pack to pack first; one waiting in the main pack goes home last (it'd take the slot the swaps run on)
        can = [g for g in wrong if home.get(g, main_id) == main_id or len(lists[home[g]]) < cap[home[g]]]
        ready = next((g for g in can if where[g] != main_id), None) or next(iter(can), None)
        if ready is not None:
            dest = home.get(ready, main_id)
            move(ready, dest, lambda d=dest, g=ready: len(lists[d]) if d == main_id else slot_in(d, g))
            continue
        wanted = {home[g] for g in wrong if g in home}            # all waiting: one steps aside, from a pack
        aside = next((g for g in wrong if where[g] in wanted), None) or next(g for g in wrong if where[g] != main_id)
        move(aside, main_id, lambda: len(lists[main_id]))

    # 2. each pack in order: its components, then the rest as they were; the longest run already in order
    #    stays, everything else moves once
    for p, _, _ in packs:
        order = target[p] + [g for g in lists[p] if not is_comp(g)]
        pos = {g: n for n, g in enumerate(order)}
        present = [g for g in lists[p] if g in pos]
        stay = {present[n] for n in lis([pos[g] for g in present])}
        before = None
        for g in order:
            if g not in stay:
                move(g, p, lambda b=before: lists[p].index(b) + 1 if b is not None else 0)
            before = g
    return moves


def comp_steps(tr):
    """Sorting the components by hand into the side packs -- every one, filled from the last one up (the 8th first)
    -- the parts that matter (not their order inside a pack), worked out as if
    each component were already in as few stacks as it fits in (the packs' room counted that way: the stacks that
    merge away free their slots). In order:
      move: a stack in another container than it belongs in -- where plan() ends it up; one that'll merge
        away goes where its component's biggest kept stack goes;
      merge: a component already where it belongs, in more stacks there than it needs: drop one on another;
      noroom: a component the packs can't hold even so: it stays in the main pack.
    [{kind: "move", name, stack, from, to, slot} | {kind: "merge", name, stacks: [sizes], where: [places now], fewer,
    to: the pack they end up in, or None} | {kind: "noroom", name, stack, slot}]. from / to / where: "main pack" or
    "side pack N" (N: its place, from 1); slot: where it sits in its pack now, from 1 (the side packs in the main pack
    don't count: the game shows them apart from its grid). Worked out from where things are now, so it shrinks as
    you go."""
    if not getattr(tr, "components", None) or not getattr(tr, "pack_order", None) or tr.player is None:
        return []
    rank = component_rank(tr.components)
    names = {g: o.get("name") for g, o in tr.objects.items()}
    stack = lambda g: (tr.objects.get(g) or {}).get("stack") or 1
    packs = [(p, tr.objects.get(p, {}).get("items_capacity"), tr.pack_contents(p)) for p in tr.pack_order]
    main = (tr.player, tr.pack_contents(tr.player))
    lists = {main[0]: list(main[1]), **{p: list(items) for p, _, items in packs}}
    start = {g: c for c, items in lists.items() for g in items}
    slot = {g: n for c, items in lists.items() for n, g in enumerate((x for x in items if x not in tr.pack_order), 1)}
    label = lambda c: "main pack" if c == main[0] else f"side pack {tr.pack_order.index(c) + 1}"
    order = sorted((g for g in start if names.get(g) in rank), key=lambda g: (rank[names[g]], g))
    by_name = {}
    for g in order:
        by_name.setdefault(names[g], []).append(g)
    # the stacks each component keeps (as few as it fits in, the biggest), and the ones that merge into them
    keep, extra = {}, set()
    for name, gs in by_name.items():
        per = max((tr.objects.get(g) or {}).get("max_stack") or 0 for g in gs)
        fewer = -(-sum(stack(g) for g in gs) // per) if per > 1 else len(gs)
        keep[name] = sorted(gs, key=lambda g: -stack(g))[:fewer]
        extra |= set(gs) - set(keep[name])
    # plan as if merged: the extra stacks gone, their slots free
    packs = [(p, cap, [g for g in items if g not in extra]) for p, cap, items in packs]
    main = (main[0], [g for g in main[1] if g not in extra])
    homes = list(tr.pack_order)[::-1]   # every side pack, filled from the last one up (8, 7, 6 ... 1)
    groups = [(lambda g: names.get(g) in rank and g not in extra, lambda g: (rank[names[g]], g), homes)]
    end = dict(start)
    for g, container, _slot in plan(main, packs, None, groups=groups):
        end[g] = container
    kept = [g for g in order if g not in extra]
    # which stacks of a component go where doesn't matter, only how many in each container: the ones already in a
    # container fill its share first, and only the rest move (a pack with the right number of stacks is left be,
    # whatever their sizes). Each takes a slot either way, so the packs' room holds.
    for name in by_name:
        share = {}
        for g in keep[name]:
            share[end[g]] = share.get(end[g], 0) + 1
        moving = []
        for g in sorted(keep[name], key=lambda g: -stack(g)):
            if share.get(start[g], 0) > 0:
                end[g] = start[g]
                share[start[g]] -= 1
            else:
                moving.append(g)
        spots = [c for c, k in share.items() for _ in range(k)]
        for g, c in zip(moving, spots):
            end[g] = c
    for name, gs in by_name.items():     # a stack that merges away: into one of its component's kept ones in the
        homes_of = {end[g] for g in keep[name]}  # container it's in, else where the biggest kept one goes
        for g in gs:
            if g in extra:
                end[g] = start[g] if start[g] in homes_of else end[keep[name][0]]
    # anything in another container than it belongs in: a move there, whatever happens to it after
    steps = sorted(({"kind": "move", "name": names[g], "stack": stack(g), "from": label(start[g]), "to": label(end[g]),
                     "slot": slot.get(g)} for g in order if end[g] != start[g] and end[g] != main[0]),
                   key=lambda s: (s["from"] != "main pack", s["from"], s["slot"] or 0))
    # too many stacks: only of what's already where it belongs, more stacks of it there than it needs there
    for name, gs in by_name.items():
        for pack in sorted({end[g] for g in gs}, key=label):
            here = [g for g in gs if start[g] == pack == end[g]]
            need = sum(1 for g in keep[name] if end[g] == pack)
            if len(here) > max(1, need):
                steps.append({"kind": "merge", "name": name, "stacks": [stack(g) for g in here], "where": [label(pack)] * len(here),
                              "fewer": max(1, need), "to": label(pack)})
    # what the packs can't hold: in the main pack, staying there
    steps += [{"kind": "noroom", "name": names[g], "stack": stack(g), "slot": slot.get(g)}
              for g in sorted(kept, key=lambda g: slot.get(g) or 0) if end[g] == main[0]]
    return steps
