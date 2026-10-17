"""Travel routes across Dereth: running plus portals, cheapest total time.

Places come from data/places.json (tools/build_places.py: every placed portal with its destination,
lifestones, towns). The route is a shortest path over:
  * run: between any two outdoor points, RUN_SECONDS per map unit (1 unit = 240 m). With a walk map
    (walkmap.py: sea, whole-water landblocks and too-steep ground) a run is the walking path around
    them; without one, a straight line.
  * portal: from a portal to its destination, PORTAL_SECONDS. Portals above or below your level,
    or locked behind a quest, are left out.
  * dungeon: between two points inside the same dungeon (indoor cells of one landblock),
    DUNGEON_SECONDS. That covers the dungeon shortcuts: in through one portal, out through another.

Walking paths are worked out as the search goes, not for every pair up front: a run first costs its
straight line (never more than the walk), and only when the search is about to settle a point
reached by a run is that run walked out; if the walk is longer it goes back in the queue at its real
cost. A point is settled only at a cost that's real, so the route is the best one by walking time.
Portals can be skipped (their ids: portal_id). With a deadline, once it's passed the runs still to walk
keep their straight lines (a far-off or unreachable spot can't search forever).

The companion runs the walking in a worker process of its own (worker_init / worker_route), so a slow
route never holds up the dashboard; it answers with the straight-line route at once meanwhile.
"""
import heapq
import itertools
import json
import os
import threading
import time
from pathlib import Path

from .worldmap import coords, distance

RUN_SECONDS = 20.0
PORTAL_SECONDS = 12.0
DUNGEON_SECONDS = 90.0
_PLACES = Path(__file__).parent / "data" / "places.json"


def load_places(path=_PLACES):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {"portals": [], "lifestones": [], "towns": []}


def portal_id(p):
    """A placed portal's id: its weenie and where it stands."""
    a = p["at"]
    return f"{p.get('wcid', 0)}@{a['cell']:08X}:{a['x']:.0f},{a['y']:.0f}"


def _indoor(at):
    return (at["cell"] & 0xFFFF) >= 0x100


class Router:
    def __init__(self, places=None, walk=None):
        self.places = places if places is not None else load_places()
        for p in self.places.get("portals", []):
            p.setdefault("id", portal_id(p))
        self.walk = walk           # a walkmap.WalkMap, or None: runs are straight lines

    def route_nearest(self, start, dest, level=None, skip=(), deadline=None, run_seconds=RUN_SECONDS):
        """route(), but when there's no walking route to `dest` (an island, a cliff top, open water), the route
        to the nearest spot you can get to instead: in the walkable area you start in, or one a usable portal
        lets you out into. That route carries partial: {ns, ew, short (map units)}."""
        found = self.route(start, dest, level=level, skip=skip, deadline=deadline, run_seconds=run_seconds)
        if found is not None or self.walk is None:
            return found
        skip = set(skip or ())
        areas = {self.walk.area(*(coords(start["cell"], start["x"], start["y"]) or _approx(start)))}
        for p in self.places.get("portals", []):
            if self._usable(p, level) and p["id"] not in skip and not _indoor(p["to"]):
                areas.add(self.walk.area(*_approx(p["to"])))
        areas.discard(None)
        near = self.walk.nearest_in(dest[0], dest[1], areas) if areas else None
        if near is None:
            return None
        found = self.route(start, near, level=level, skip=skip, deadline=deadline, run_seconds=run_seconds)
        if found is not None:
            found["partial"] = {"ns": near[0], "ew": near[1], "short": distance(near, dest)}
        return found

    def _usable(self, portal, level):
        if portal.get("quest"):
            return False
        if level is not None:
            if portal.get("min_level") and level < portal["min_level"]:
                return False
            if portal.get("max_level") and portal["max_level"] > 0 and level > portal["max_level"]:
                return False
        return True

    def route(self, start, dest, level=None, run_seconds=RUN_SECONDS, skip=(), deadline=None, walk=True, check=True):
        """start: {cell, x, y} (where you are; indoors counts as both the building's spot on the map and
        the dungeon it may be in). dest: (ns, ew). skip: portal ids to leave out. deadline: a time.monotonic()
        after which runs aren't walked out any more. walk=False: straight lines only (the quick estimate).
        check=False: straight lines may cross open sea too (the estimate's fallback when none can go around).
        Returns {seconds, steps, path, walked, terrain, cut_short} or None."""
        skip = set(skip or ())
        nodes = []          # (ns, ew, indoor landblock or None, label)
        edges = {}          # node -> [(node, seconds, step)]

        def add(ns, ew, block, label):
            nodes.append((ns, ew, block, label))
            return len(nodes) - 1

        def link(a, b, seconds, step):
            edges.setdefault(a, []).append((b, seconds, step))

        sc = coords(start["cell"], start["x"], start["y"]) or _approx(start)
        s = add(sc[0], sc[1], None, "you")
        start_block = start["cell"] >> 16 if _indoor(start) else None
        s_in = add(sc[0], sc[1], start_block, "you") if start_block is not None else None
        d = add(dest[0], dest[1], None, "destination")
        for p in self.places.get("portals", []):
            if not self._usable(p, level) or p["id"] in skip:
                continue
            a, b = p["at"], p["to"]
            ia = add(*(_approx(a)), a["cell"] >> 16 if _indoor(a) else None, p["name"])
            ib = add(*(_approx(b)), b["cell"] >> 16 if _indoor(b) else None, p["name"])
            link(ia, ib, PORTAL_SECONDS, {"kind": "portal", "name": p["name"], "id": p["id"], "at": a, "to": b})
        outdoor = [i for i, n in enumerate(nodes) if n[2] is None]
        by_block = {}
        for i, n in enumerate(nodes):
            if n[2] is not None:
                by_block.setdefault(n[2], []).append(i)

        def neighbours(i):
            n = nodes[i]
            yield from edges.get(i, [])
            if n[2] is None:
                for j in outdoor:
                    if j != i:
                        dist = distance(n[:2], nodes[j][:2])
                        yield j, dist * run_seconds, {"kind": "run", "distance": dist}
            else:
                for j in by_block.get(n[2], []):
                    if j != i:
                        yield j, DUNGEON_SECONDS, {"kind": "dungeon"}

        walk, check = (self.walk if walk else None), (self.walk if check else None)   # check: no straight run over open sea
        cut_short = False
        settled = {}                    # node -> its real cost
        back = {}
        # (cost, tie, node, from, step, real): a run's cost is its straight line until it's walked out
        tie = itertools.count()
        heap = [(0.0, next(tie), s, None, None, True)] + ([(0.0, next(tie), s_in, None, None, True)] if s_in is not None else [])
        walked = 0
        while heap:
            cost, _, i, prev, step, real = heapq.heappop(heap)
            if i in settled:
                continue
            if not real and (walk is None or (deadline is not None and time.monotonic() > deadline)):
                # not walked out (the estimate, or out of time): the straight line, unless it crosses open sea
                cut_short = cut_short or walk is not None
                if check is not None and check.crosses_blocked(nodes[prev][:2], nodes[i][:2]):
                    continue
                real = True
            if not real:                # walk it out now; back in the queue at what it really costs
                path = walk.walk(nodes[prev][:2], nodes[i][:2])
                walked += 1
                if path is None:
                    continue            # can't walk there from that point
                step = dict(step, distance=path["distance"], points=path["points"])
                heapq.heappush(heap, (settled[prev] + path["distance"] * run_seconds, next(tie), i, prev, step, True))
                continue
            settled[i] = cost
            if prev is not None:
                back[i] = (prev, step)
            if i == d:
                break
            for j, sec, st in neighbours(i):
                if j not in settled:
                    rough = (walk is not None or check is not None) and st["kind"] == "run"
                    heapq.heappush(heap, (cost + sec, next(tie), j, i, st, not rough))
        best = settled
        if d not in best:
            return None
        steps, path, i = [], [], d
        while i in back:
            prev, step = back[i]
            a, b = nodes[prev], nodes[i]
            entry = dict(step, from_ns=a[0], from_ew=a[1], to_ns=b[0], to_ew=b[1])
            if step["kind"] == "run":
                entry["seconds"] = step["distance"] * run_seconds
            else:
                entry["seconds"] = PORTAL_SECONDS if step["kind"] == "portal" else DUNGEON_SECONDS
            steps.append(entry)
            i = prev
        steps.reverse()
        # Merge consecutive runs (can't happen in a shortest path, but keep the output tidy) and drop zero runs.
        steps = [st for st in steps if not (st["kind"] == "run" and st["distance"] < 0.01)]
        for st in steps:
            path.append([st["from_ns"], st["from_ew"], st["kind"]])
        path.append([dest[0], dest[1], "end"])
        return {"seconds": best[d], "steps": steps, "path": path, "walked": walked, "terrain": walk is not None,
                "cut_short": cut_short}


# ---- the routing worker process -----------------------------------------------------------------------
_WORKER = None


def worker_init(dat_path, cache_dir, parent_pid=None):
    """In the worker process: the places and the walk map (from its cache; built if it isn't there). It
    exits when the companion does (the reloader kills it outright, so nothing would shut the worker down)."""
    global _WORKER
    if parent_pid:
        threading.Thread(target=_exit_with, args=(parent_pid,), name="parent-watch", daemon=True).start()
    from . import walkmap
    from .dat import DatFile
    dat = DatFile(dat_path)
    _WORKER = Router(walk=walkmap.load(Path(dat_path).with_name("cell.dat"), dat, Path(cache_dir)))


def _exit_with(pid):
    """Wait for process `pid` to end, then end this one."""
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        handle = k.OpenProcess(0x00100000, False, pid)          # SYNCHRONIZE
        if handle:
            k.WaitForSingleObject(handle, 0xFFFFFFFF)            # INFINITE
    else:
        while os.getppid() == pid:
            time.sleep(2)
    os._exit(0)


def worker_route(start, dest, level, skip, budget, run_seconds=RUN_SECONDS):
    """In the worker process: the walked route, walking for at most `budget` seconds (or, when the destination
    can't be walked to, to the nearest spot that can: route_nearest)."""
    return _WORKER.route_nearest(start, dest, level=level, skip=skip, deadline=time.monotonic() + budget,
                                 run_seconds=run_seconds)


def outdoor_position(ns, ew):
    """{cell, x, y} for a map coordinate, outdoors (a route's starting point picked on the map), or None
    when it's off the map."""
    gx, gy = (ew + 101.95) * 240, (ns + 101.95) * 240
    bx, by = int(gx // 192), int(gy // 192)
    if not (0 <= bx <= 0xFE and 0 <= by <= 0xFE):
        return None
    x, y = gx - bx * 192, gy - by * 192
    cell = int(x // 24) * 8 + int(y // 24) + 1          # outdoor cells 1-64, column by column
    return {"cell": (bx << 24) | (by << 16) | cell, "x": x, "y": y}


def _approx(at):
    """Map coordinates for any position, indoors included (a building sits in its town's landblock;
    a dungeon's landblock usually isn't where its entrance is, so this is only a placeholder there)."""
    bx, by = at["cell"] >> 24, (at["cell"] >> 16) & 0xFF
    return (by * 192 + at["y"]) / 240 - 101.95, (bx * 192 + at["x"]) / 240 - 101.95
