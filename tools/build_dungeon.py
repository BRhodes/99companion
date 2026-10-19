"""Build companion/data/dungeons/<landblock>.json: a dungeon's floor plan and its creature spawn points, for the Map
page's dungeon view (and its respawn timers: companion/dungeons.py).

    python -m tools.build_dungeon 019A "Crater Caves" --db <server checkout>/Database/AC1999-World/3-Core

Floors are where a dungeon is flat: each height with FLAT_MIN of level ground. A slope (a ramp, stairs, a cave's floor
rolling from one floor to the next) is drawn on both floors it joins -- each shows the SHARED of it nearest itself, so
its middle is on both -- hatched, and numbered as a way between them; a slope that touches only one floor (a mound, the
foot of a wall) is that floor's, drawn as floor.

Floor plan: every cell of the landblock's dungeon (cell.dat EnvCells 0xLLLL0100 on), each placed by its frame, with the
walkable upward-facing polygons of its cell structure (portal.dat Environments) as its floor, on the floors their
heights are (not their cell's). The 1999 dats pad both to four
bytes: an EnvCell after its surface list, an Environment after each polygon. The BSP trees after a cell structure
aren't read: the next structure is found by its header (its key, plausible counts, vertex type 1).

Spawn points: the creatures placed in the landblock (6 LandBlockExtendedData), each with its respawn delay -- a
linked spawn's from its spawner's first generator entry (the placeholder that times the links, as the server's
EnsureLink copies it), else the creature's own spawner entry.
"""
import argparse
import json
import math
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from companion.dat import DatFile          # noqa: E402
from companion.launcher import game_dir   # noqa: E402

OUT = ROOT / "companion" / "data" / "dungeons"


class Reader:
    def __init__(self, b):
        self.b, self.p = b, 0

    def take(self, fmt):
        v = struct.unpack_from("<" + fmt, self.b, self.p)
        self.p += struct.calcsize("<" + fmt)
        return v if len(v) > 1 else v[0]

    def align(self):
        self.p = (self.p + 3) & ~3


def env_cell(cell_dat, cid):
    """A dungeon cell: {id, env, struct, origin, rot (w x y z), portals: {its structure's polygon: the cell through
    it}}, or None past the last one."""
    b = cell_dat.get(cid)
    if b is None:
        return None
    r = Reader(b)
    r.take("I")                                   # flags
    out = {"id": r.take("I")}
    surfaces, portals = r.take("B"), r.take("B")
    r.take("H")                                   # cells seen from it
    r.p += 2 * surfaces
    r.align()
    out["env"], out["struct"] = 0x0D000000 | r.take("H"), r.take("H")
    out["origin"], out["rot"] = r.take("3f"), r.take("4f")
    out["portals"] = {}
    for _ in range(portals):
        _flags, pid, other, _their = r.take("HHHH")
        out["portals"][pid] = other
    return out


def cell_structure(portal_dat, eid, key, _cache={}):
    """(vertices {id: (x, y, z)}, polygons [[vertex ids]], portals {polygon: [vertex ids]}) of one cell structure of an
    environment -- its portals (the openings into the cells beside it) apart: one under a tall cave's upper cell would
    read as its floor."""
    if (eid, key) in _cache:
        return _cache[(eid, key)]
    b = portal_dat.get(eid)
    r = Reader(b)
    r.take("I")
    for _ in range(r.take("I")):
        k = r.take("I")
        npoly = r.take("I")
        r.take("I")                               # physics polygons
        nport = r.take("I")
        r.take("i")                               # vertex type
        verts = {}
        for _ in range(r.take("I")):
            vk, nuv = r.take("H"), r.take("H")
            verts[vk] = r.take("3f")
            r.take("3f")                          # normal
            r.p += 8 * nuv
        polys = {}
        for _ in range(npoly):
            pid = r.take("h")
            pts, stip = r.take("B"), r.take("B")
            sides = r.take("i")
            r.take("hh")
            polys[pid] = [r.take("h") for _ in range(pts)] if pts > 1 else [r.take("h")] if pts else []
            if not stip & 4:
                r.p += pts
            if sides == 1 and not stip & 8:
                r.p += pts
            r.align()
        portals = {r.take("h") for _ in range(nport)}
        _cache[(eid, k)] = (verts, [v for pid, v in polys.items() if pid not in portals],
                            {pid: v for pid, v in polys.items() if pid in portals})
        if k == key:
            return _cache[(eid, k)]
        q = (r.p + 3) & ~3                        # (aligned: an odd number of portals leaves it half a word in)
        while q + 24 <= len(b):
            nk, a1, a2, a3, vt, nv = struct.unpack_from("<6I", b, q)
            if nk == k + 1 and vt == 1 and 0 < a1 < 2000 and a2 < 2000 and a3 < 64 and 0 < nv < 5000:
                break
            q += 4
        else:
            return None
        r.p = q
    return None


def rotate(q, v):
    w, x, y, z = q
    vx, vy, vz = v
    ix, iy, iz = w * vx + y * vz - z * vy, w * vy + z * vx - x * vz, w * vz + x * vy - y * vx
    iw = -x * vx - y * vy - z * vz
    return (ix * w - iw * x - iy * z + iz * y, iy * w - iw * y - iz * x + ix * z, iz * w - iw * z - ix * y + iy * x)


WALKABLE = 0.664       # a polygon's normal z at least this: the game's own (PhysicsGlobals::floor_z, ~48 degrees)
STAIRS = 0.93          # steeper than this (~22 degrees) is drawn as stairs / a ramp; gentler is a cave's uneven floor
FLAT_MIN = 50          # m^2 of level ground at one height that make it a floor
SHARED = 0.75          # how much of a slope between two floors each of them shows
NEAR = 0.15            # metres: a height this close to a floor's is on it
LOW_WALL = 1.5         # metres: a "wall" this short with floor on both sides is a step's riser or a lip
THRESHOLD = 1.2        # metres: a strip of no floor this narrow between two level pieces at one height is a doorway's


def walkable(portal_dat, c):
    """A cell's walkable upward-facing polygons, each (its [(x, y, z)...] in the dungeon, its normal's z), its level
    openings (portals in its floor or ceiling: ([(x, y, z)...], the cell through it)) and the cells beside it (through
    its other portals: {cell: where the portal is (x, y)}), and its walls' feet ([x1, y1, x2, y2, z, height]: each wall's
    bottom edge, where it meets the floor, and how tall it stands)."""
    got = cell_structure(portal_dat, c["env"], c["struct"])
    if not got:
        return [], [], {}, []
    verts, polys, portals = got
    place = lambda ids: [tuple(a + b for a, b in zip(rotate(c["rot"], verts[i]), c["origin"])) for i in ids if i in verts]
    holes, beside = [], {}
    for pid, ids in portals.items():
        w, other = place(ids), c["portals"].get(pid, 0xFFFF)
        if len(w) >= 3 and level_ground(w):
            holes.append((w, other))
        elif w:
            beside[other] = (sum(p[0] for p in w) / len(w), sum(p[1] for p in w) / len(w))
    out, walls = [], []
    # (and a doorway lying in a ramp's slope -- its foot: the cells' floors stop at it, you walk across it; a level one,
    # a tall cave's opening, isn't ground)
    sloped_doors = [ids for ids in portals.values() if len(place(ids)) >= 3 and not level_ground(place(ids))]
    for ids in polys + sloped_doors:
        w = place(ids)
        if len(w) < 3:
            continue
        (ax, ay, az), (bx, by, bz), (cx, cy, cz) = w[:3]
        nz = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        nx = (by - ay) * (cz - az) - (bz - az) * (cy - ay)
        ny = (bz - az) * (cx - ax) - (bx - ax) * (cz - az)
        up = nz / (math.sqrt(nx * nx + ny * ny + nz * nz) or 1)
        if ids in sloped_doors and up < 0:      # (a doorway faces either way)
            w, up = w[::-1], -up
        if up >= WALKABLE:
            out.append((w, up))
        elif up > -0.3 and ids not in sloped_doors:      # a wall (not a ceiling): its foot -- an edge with the rest of
            for k, (a, b) in enumerate(zip(w, w[1:] + w[:1])):   # it above (level, or along a ramp), at its middle's height
                rest = [p[2] for j, p in enumerate(w) if j not in (k, (k + 1) % len(w))]
                if rest and min(rest) >= max(a[2], b[2]) - 0.05 and math.dist(a[:2], b[:2]) > 0.2:
                    walls.append([round(a[0], 2), round(a[1], 2), round(b[0], 2), round(b[1], 2), round((a[2] + b[2]) / 2, 2),
                                  round(max(rest) - (a[2] + b[2]) / 2, 2)])          # (and how tall it stands)
    return out, holes, beside, walls


def area(w):
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1, _a), (x2, y2, _b) in zip(w, w[1:] + w[:1]))) / 2


def level_ground(w):
    return max(p[2] for p in w) - min(p[2] for p in w) < 0.05


def clip(w, lo, hi):
    """The part of a (flat-faced) polygon between two heights."""
    for keep, at in ((lambda z: z >= lo - 1e-6, lo), (lambda z: z <= hi + 1e-6, hi)):
        out = []
        for a, b in zip(w, w[1:] + w[:1]):
            if keep(a[2]):
                out.append(a)
            if keep(a[2]) != keep(b[2]):
                f = (at - a[2]) / (b[2] - a[2])
                out.append(tuple(p + f * (q - p) for p, q in zip(a, b)))
        w = out
        if len(w) < 3:
            return []
    return w


def crosses(a, b, poly):
    """Whether a segment touches a polygon: an end (or its middle) in it, or across one of its sides."""
    if any(inside(poly, *pt) for pt in (a, b, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))):
        return True
    side = lambda o, p, q: (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])
    return any(side(a, b, c) * side(a, b, d) < 0 and side(c, d, a) * side(c, d, b) < 0 for c, d in zip(poly, poly[1:] + poly[:1]))


def along(f, a, ux, uy, nx, ny, lo, hi, gap):
    """Whether a wall's foot runs along one of a strip's long sides (its edge at 0 or at gap out from a, between lo and
    hi along u) for half a metre or more."""
    s1, s2 = ((f[0] - a[0]) * ux + (f[1] - a[1]) * uy), ((f[2] - a[0]) * ux + (f[3] - a[1]) * uy)
    g1, g2 = ((f[0] - a[0]) * nx + (f[1] - a[1]) * ny), ((f[2] - a[0]) * nx + (f[3] - a[1]) * ny)
    on_side = any(abs(g1 - g) < 0.1 and abs(g2 - g) < 0.1 for g in (0, gap))
    return on_side and min(hi, max(s1, s2)) - max(lo, min(s1, s2)) >= 0.5


def fill_thresholds(cells):
    """The bare strip at a doorway: two pieces of level floor at one height, edge facing edge under THRESHOLD apart with
    no wall between, have the strip between them filled in (added to the first one's cell). How many were filled."""
    edges = []
    for c in cells:
        for w, _up in c["raw"]:
            if level_ground(w):
                for a, b in zip(w, w[1:] + w[:1]):
                    if math.dist(a[:2], b[:2]) > 0.5:
                        edges.append((c, a, b))
    grid = {}
    for k, (_c, a, b) in enumerate(edges):
        grid.setdefault((math.floor((a[0] + b[0]) / 4), math.floor((a[1] + b[1]) / 4)), []).append(k)
    feet = [(c, w) for c in cells for w in c["walls"]]
    added = 0
    for k, (c, a, b) in enumerate(edges):
        ux, uy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(ux, uy)
        ux, uy = ux / length, uy / length
        nx, ny = uy, -ux                          # (the edge's outward side: a walkable piece winds counter-clockwise)
        gx, gy = math.floor((a[0] + b[0]) / 4), math.floor((a[1] + b[1]) / 4)
        for j in [j for dx in (-1, 0, 1) for dy in (-1, 0, 1) for j in grid.get((gx + dx, gy + dy), []) if j > k]:
            _d, p, q = edges[j]
            if abs(p[2] - a[2]) > 0.05 or (p[0] - q[0]) * ux + (p[1] - q[1]) * uy < 0.99 * math.dist(p[:2], q[:2]):
                continue                          # (another height, or not facing this edge)
            gap = (p[0] - a[0]) * nx + (p[1] - a[1]) * ny
            off = abs((q[0] - a[0]) * nx + (q[1] - a[1]) * ny - gap)
            if not 0.05 < gap < THRESHOLD or off > 0.05:
                continue
            lo = max(0, min((p[0] - a[0]) * ux + (p[1] - a[1]) * uy, (q[0] - a[0]) * ux + (q[1] - a[1]) * uy))
            hi = min(length, max((p[0] - a[0]) * ux + (p[1] - a[1]) * uy, (q[0] - a[0]) * ux + (q[1] - a[1]) * uy))
            if hi - lo < 0.5:
                continue
            strip = [(a[0] + ux * s + nx * g, a[1] + uy * s + ny * g, a[2]) for s, g in ((lo, 0), (hi, 0), (hi, gap), (lo, gap))]
            inner = [(a[0] + ux * s + nx * g, a[1] + uy * s + ny * g) for s, g in ((lo + .3, 0), (hi - .3, 0), (hi - .3, gap), (lo + .3, gap))]
            if any(abs(f[4] - a[2]) < 2 and crosses((f[0], f[1]), (f[2], f[3]), inner) for _c, f in feet):
                continue                          # (a wall in it, or across it -- not just touching its ends: a door frame)
            if any(abs(f[4] - a[2]) < 2 and along(f, a, ux, uy, nx, ny, lo, hi, gap) for _c, f in feet):
                continue                          # (a wall along it: the gap is a wall's thickness, between two rooms)
            c["raw"].append((strip[::-1], 1.0))   # (wound counter-clockwise, as the rest)
            added += 1
    return added


def lay(cells):
    """Each cell's floor pieces ([floor, [[x, y]...], hatched]) from its walkable polygons (cells' "raw"), the floors'
    heights, and the ways between them ([(upper, lower, x, y)])."""
    ground = {}
    for c in cells:
        for w, _up in c["raw"]:
            if level_ground(w):
                z = round(w[0][2], 1)
                ground[z] = ground.get(z, 0) + area(w)
    heights = sorted(z for z, a in ground.items() if a >= FLAT_MIN)
    on = lambda z: next((f for f in heights if abs(z - f) < NEAR), None)
    xy = lambda w: [[round(x, 2), round(y, 2)] for x, y, _z in w]
    # the rest -- slopes, and level bits too small to be a floor -- in runs joined edge to edge
    rest = [(c, w, up) for c in cells for w, up in c["raw"] if not (level_ground(w) and on(w[0][2]) is not None)]
    key = lambda p: (round(p[0], 1), round(p[1], 1), round(p[2], 1))
    parent = list(range(len(rest)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    edges = {}
    for i, (_c, w, _up) in enumerate(rest):
        for a, b in zip(w, w[1:] + w[:1]):
            e = frozenset((key(a), key(b)))
            if e in edges:
                parent[find(i)] = find(edges[e])
            else:
                edges[e] = i
    runs = {}
    for i in range(len(rest)):
        runs.setdefault(find(i), []).append(rest[i])
    for c in cells:
        c["floors"] = [[on(w[0][2]), xy(w), 0] for w, _up in c["raw"] if level_ground(w) and on(w[0][2]) is not None]
    ways = []
    for run in runs.values():
        zs = [p[2] for _c, w, _u in run for p in w]
        touch = sorted({f for z in zs for f in [on(z)] if f is not None}, reverse=True)
        if not touch:                            # (touching no floor: the one under it, else the nearest)
            under = [f for f in heights if f <= min(zs) + NEAR]
            touch = [max(under) if under else min(heights, key=lambda f: abs(f - min(zs)))]
        for k, f in enumerate(touch):
            above, below = (touch[k - 1] if k else None), (touch[k + 1] if k + 1 < len(touch) else None)
            lo = f - SHARED * (f - below) if below is not None else min(zs)
            hi = f + SHARED * (above - f) if above is not None else max(zs)
            for c, w, up in run:
                part = clip(w, lo, hi)
                if part:
                    c["floors"].append([f, xy(part), int(len(touch) > 1 and up < STAIRS)])
        for a, b in zip(touch, touch[1:]):
            mid = [p for _c, w, _u in run for p in w if b < p[2] < a] or [p for _c, w, _u in run for p in w]
            ways.append((a, b, sum(p[0] for p in mid) / len(mid), sum(p[1] for p in mid) / len(mid)))
    by_id = {c["id"]: c for c in cells}
    has_floor = lambda c, f: any(level_ground(w) and on(w[0][2]) == f for w, _u in c["raw"])
    ground = lambda c: [on(w[0][2]) for w, _u in c["raw"] if level_ground(w) and on(w[0][2]) is not None]
    # ledges: a doorway to a cell whose floor is all lower than this one's (stepped off, not climbed back)
    for c in cells:
        for o, (x, y) in c["beside"].items():
            if o in by_id and ground(c) and ground(by_id[o]) and max(ground(by_id[o])) < min(ground(c)) - 3:
                ways.append((min(ground(c)), max(ground(by_id[o])), x, y))
    # drops: an opening in a floor, down to the floor under it -- a pit (floor of its height around it), or a shaft
    # (two or more cells with no floor, one over the other, walked into from floor beside its top); not a tall cave's
    # open middle (its upper cells have no floor beside them, or one above floor)
    edge = {}
    for c in cells:
        for w, _up in c["raw"]:
            if level_ground(w) and on(w[0][2]) is not None:
                for a, b in zip(w, w[1:] + w[:1]):
                    edge[frozenset((key(a), key(b)))] = on(w[0][2])
    seen = set()
    for c in cells:
        for w, below in c["holes"]:
            f = on(w[0][2])
            if f is None or below not in by_id or by_id[below]["origin"] >= c["origin"]:
                continue                         # (only openings down)
            chain, d = 0 if ground(c) else 1, by_id[below]
            while not ground(d):                 # (down the shaft to the first cell with floor)
                chain += 1
                nxt = [o for v, o in d["holes"] if o in by_id and by_id[o]["origin"] < d["origin"]]
                if not nxt:
                    break
                d = by_id[nxt[0]]
            if not ground(d):
                continue
            land = max(ground(d))
            pit = any(edge.get(frozenset((key(a), key(b)))) == f for a, b in zip(w, w[1:] + w[:1]))
            shaft = chain >= 2 and any(o in by_id and has_floor(by_id[o], f) for o in c["beside"])
            x, y = sum(p[0] for p in w) / len(w), sum(p[1] for p in w) / len(w)
            if land < f and (pit or shaft) and (round(x), round(y)) not in seen:
                seen.add((round(x), round(y)))
                ways.append((f, land, x, y))
    return heights, ways


def inside(poly, x, y):
    hit = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def two_sided(wall, cells, _index={}):
    """A wall's foot with ground on both sides of it (a step's riser, a lip between two pieces of floor, a door frame's
    side): not a wall. Looked for half a metre out (a doorway's bare threshold is filled in first), at about its
    height."""
    key = id(cells)
    if key not in _index:
        _index.clear()
        grid = _index[key] = {}
        for c in cells:
            for w, _up in c["raw"]:
                (ax, ay, az), (bx, by, bz), (cx, cy, cz) = w[:3]
                nx = (by - ay) * (cz - az) - (bz - az) * (cy - ay)
                ny = (bz - az) * (cx - ax) - (bx - ax) * (cz - az)
                nz = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
                piece = ([p[:2] for p in w], lambda x, y, a=w[0], n=(nx, ny, nz): a[2] - (n[0] * (x - a[0]) + n[1] * (y - a[1])) / n[2] if n[2] else a[2])
                xs, ys = [p[0] for p in w], [p[1] for p in w]
                for gx in range(math.floor(min(xs)), math.floor(max(xs)) + 1):
                    for gy in range(math.floor(min(ys)), math.floor(max(ys)) + 1):
                        grid.setdefault((gx, gy), []).append(piece)
    grid = _index[key]
    x1, y1, x2, y2, z, tall = wall
    if tall >= LOW_WALL and math.dist((x1, y1), (x2, y2)) >= 2:
        return False                              # (a long wall you can't step over is a wall, however thin; a short tall
                                                  # one with floor both sides is a door frame's side)
    length = math.dist((x1, y1), (x2, y2)) or 1
    nx, ny = (y2 - y1) / length, -(x2 - x1) / length
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2

    def ground(x, y):
        return any(inside(poly, x, y) and -0.5 < z_at(x, y) - z < 1.5 for poly, z_at in grid.get((math.floor(x), math.floor(y)), []))
    sides = [any(ground(mx + s * d * nx, my + s * d * ny) for d in (0.5,)) for s in (1, -1)]
    return all(sides)


def connections(ways):
    """The ways between floors, neighbouring ones joining the same pair as one: [{n: its number, a: the upper floor,
    b: the lower, x, y}], top down."""
    groups = []
    for a, b, x, y in ways:
        g = next((g for g in groups if g["a"] == a and g["b"] == b and math.dist((g["x"], g["y"]), (x, y)) < 15), None)
        if g:
            g["pts"].append((x, y))
            g["x"], g["y"] = sum(p[0] for p in g["pts"]) / len(g["pts"]), sum(p[1] for p in g["pts"]) / len(g["pts"])
        else:
            groups.append({"a": a, "b": b, "x": x, "y": y, "pts": [(x, y)]})
    groups.sort(key=lambda g: (-g["a"], g["x"], -g["y"]))
    return [{"n": n, "a": g["a"], "b": g["b"], "x": round(g["x"], 1), "y": round(g["y"], 1)} for n, g in enumerate(groups, 1)]


def entrance(landblock):
    """Where the dungeon's entered from (companion/data/places.json): {ns, ew, min_level, lifestone: {ns, ew, away},
    town: {name, away}}, or None."""
    places = json.loads((ROOT / "companion" / "data" / "places.json").read_text(encoding="utf-8"))
    ins = [p for p in places["portals"] if p["to"]["cell"] >> 16 == landblock and p["at"].get("ns") is not None]
    if not ins:
        return None
    a = ins[0]["at"]
    away = lambda o: round(math.hypot(o["ns"] - a["ns"], o["ew"] - a["ew"]), 1)
    ls = min(places["lifestones"], key=away)
    town = min(places["towns"], key=away)
    to = ins[0]["to"]
    return {"ns": round(a["ns"], 1), "ew": round(a["ew"], 1), "min_level": ins[0].get("min_level"),
            "arrival": {"x": round(to["x"], 1), "y": round(to["y"], 1), "cell": to["cell"] & 0xFFFF},
            "lifestone": {"ns": round(ls["ns"], 1), "ew": round(ls["ew"], 1), "away": away(ls)},
            "town": {"name": town["name"], "away": away(town)}}


def weenie_files(core):
    files = {}
    for f in (core / "9 WeenieDefaults" / "SQL").rglob("*.sql"):
        m = re.match(r"(\d{5}) ", f.name)
        if m:
            files[int(m.group(1))] = f
    return files


def first_delay(files, wcid):
    """A spawner's first generator entry's delay (seconds), or None."""
    f = files.get(wcid)
    t = f.read_text(encoding="utf-8", errors="ignore") if f else ""
    m = re.search(rf"\({wcid},\s*-?[\d.]+,\s*\d+,\s*([\d.]+),", t[t.find("weenie_properties_generator"):]) \
        if "weenie_properties_generator" in t else None
    return float(m.group(1)) if m else None


def build(landblock, name, core):
    game = Path(game_dir())
    cell_dat, portal_dat = DatFile(str(game / "cell.dat")), DatFile(str(game / "portal.dat"))
    monsters = json.loads((ROOT / "companion" / "data" / "monsters.json").read_text(encoding="utf-8"))
    cells = []
    for i in range(0x100, 0x1000):
        c = env_cell(cell_dat, (landblock << 16) | i)
        if c is None:
            break
        raw, holes, beside, walls = walkable(portal_dat, c)
        cells.append({"id": i, "origin": c["origin"][2], "raw": raw, "holes": holes, "beside": beside, "walls": walls})
    filled = fill_thresholds(cells)
    heights, ways = lay(cells)
    level = lambda z: min(heights, key=lambda f: abs(f - z))        # (the floor a height is nearest)
    for c in cells:
        c["z"] = level(c.pop("origin"))
    text = (core / "6 LandBlockExtendedData" / "SQL" / f"{landblock:04X}.sql").read_text(encoding="utf-8")
    placed = {g: (int(w), int(c, 16), float(x), float(y), float(z), n) for g, w, c, x, y, z, n in re.findall(
        r"VALUES \((0x[0-9A-F]+),\s*(\d+),\s*(0x[0-9A-F]+),\s*([-\d.e]+),\s*([-\d.e]+),\s*([-\d.e]+),[^;]*;\s*/\* (.*?) \*/", text)}
    parent = {child: p for p, child in re.findall(r"(?:VALUES|     ,) \((0x[0-9A-F]+), (0x[0-9A-F]+), '", text)}
    files = weenie_files(core)
    spawns = []
    for g, (wcid, cid, x, y, z, label) in placed.items():
        if str(wcid) not in monsters or (cid & 0xFFFF) < 0x100:
            continue
        m = monsters[str(wcid)]
        delay = first_delay(files, placed[parent[g]][0]) if g in parent and parent[g] in placed else None
        spawns.append({"wcid": wcid, "name": m["name"].replace("''", "'"), "level": m["level"], "cell": cid & 0xFFFF,
                       "x": round(x, 2), "y": round(y, 2), "z": round(z, 2), "floor": level(z), "delay": delay})
    spawns.sort(key=lambda s: (s["cell"], s["x"], s["y"]))
    exits = [{"name": n, "x": round(x, 2), "y": round(y, 2), "z": round(z, 2), "floor": level(z), "cell": cid & 0xFFFF}
             for _g, (w, cid, x, y, z, n) in placed.items() if "Surface" in n or "Portal" in n]
    door = entrance(landblock)
    if door:                                  # (the floor you arrive on: where your cell's ground is)
        cell = next((c for c in cells if c["id"] == door["arrival"]["cell"]), None)
        zs = [p[2] for w, _up in cell["raw"] for p in w] if cell else []
        door["arrival"]["floor"] = level(sum(zs) / len(zs)) if zs else (cell or {}).get("z")
    # the ground itself, in 3D (each walkable polygon once, [[x, y, z]...]): what the page's walking view spreads over
    surface = [[[round(x, 2), round(y, 2), round(z, 2)] for x, y, z in w] for c in cells for w, _up in c["raw"]]
    walls = [w for c in cells for w in c["walls"] if not two_sided(w, cells)]
    for c in cells:
        del c["raw"], c["holes"], c["beside"], c["walls"]
    return {"name": name, "landblock": landblock, "floors": heights, "cells": cells, "spawns": spawns, "exits": exits,
            "connections": connections(ways), "entrance": door, "surface": surface, "walls": walls}


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("landblock", help="the dungeon's landblock, hex (019A)")
    p.add_argument("name", help="what it's called (Crater Caves)")
    p.add_argument("--db", required=True, help="the server checkout's Database/AC1999-World/3-Core")
    a = p.parse_args()
    lb = int(a.landblock, 16)
    data = build(lb, a.name, Path(a.db))
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{lb:04X}.json"
    out.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    timed = sum(1 for s in data["spawns"] if s["delay"])
    print(f"{a.name}: {len(data['cells'])} cells, {len(data['spawns'])} spawns ({timed} with a respawn time) -> {out} "
          f"({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
