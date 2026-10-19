"""Build companion/data/dungeons/<landblock>.json: a dungeon's floor plan and its creature spawn points, for the Map
page's dungeon view (and its respawn timers: companion/dungeons.py).

    python -m tools.build_dungeon 019A "Crater Caves" --db <server checkout>/Database/AC1999-World/3-Core

A floor is 12 m: two 6 m halves (paired the way that stacks the fewest rooms over each other) (a cave's floor rolls across the line
between them, a short ramp or a few steps apart) are one place, drawn as one.

Floor plan: every cell of the landblock's dungeon (cell.dat EnvCells 0xLLLL0100 on), each placed by its frame, with the
walkable upward-facing polygons of its cell structure (portal.dat Environments) as its floor -- each on the floor its own
height is nearest (floors are FLOOR_STEP apart), not its cell's: a ramp's pieces go to the floors it joins, a ledge to
the floor it's at. The 1999 dats pad both to four
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
    """A dungeon cell: {id, env, struct, origin, rot (w x y z)}, or None past the last one."""
    b = cell_dat.get(cid)
    if b is None:
        return None
    r = Reader(b)
    r.take("I")                                   # flags
    out = {"id": r.take("I")}
    surfaces, _portals = r.take("B"), r.take("B")
    r.take("H")                                   # cells seen from it
    r.p += 2 * surfaces
    r.align()
    out["env"], out["struct"] = 0x0D000000 | r.take("H"), r.take("H")
    out["origin"], out["rot"] = r.take("3f"), r.take("4f")
    return out


def cell_structure(portal_dat, eid, key, _cache={}):
    """(vertices {id: (x, y, z)}, polygons [[vertex ids]]) of one cell structure of an environment -- not its portals
    (the openings into the cells beside it: one under a tall cave's upper cell would read as its floor)."""
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
        polys = [v for pid, v in polys.items() if pid not in portals]
        _cache[(eid, k)] = (verts, polys)
        if k == key:
            return verts, polys
        q = r.p
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


FLOOR_STEP = 6.0       # metres between the heights a dungeon is built on (half a floor)
FLOOR = 12.0           # a floor: two of them
WALKABLE = 0.664       # a polygon's normal z at least this: the game's own (PhysicsGlobals::floor_z, ~48 degrees)
WAY = 10               # metres from a way to another floor that a slope is its stairs (further: a half step)
STAIRS = 0.93          # steeper than this (~22 degrees) is drawn as stairs / a ramp; gentler is a cave's uneven floor


def level(z):
    """The half floor a height belongs to: the nearest multiple of FLOOR_STEP."""
    return round(z / FLOOR_STEP) * FLOOR_STEP


def floors(portal_dat, c):
    """A cell's floor: its walkable upward-facing polygons, each [floor it's on (by its own height), [[x, y]...], sloped
    (1: a ramp or stairs, 0: level)]."""
    got = cell_structure(portal_dat, c["env"], c["struct"])
    if not got:
        return []
    verts, polys = got
    out = []
    for ids in polys:
        w = [tuple(a + b for a, b in zip(rotate(c["rot"], verts[i]), c["origin"])) for i in ids if i in verts]
        if len(w) < 3:
            continue
        (ax, ay, az), (bx, by, bz), (cx, cy, cz) = w[:3]
        nz = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        nx = (by - ay) * (cz - az) - (bz - az) * (cy - ay)
        ny = (bz - az) * (cx - ax) - (bx - ax) * (cz - az)
        up = nz / (math.sqrt(nx * nx + ny * ny + nz * nz) or 1)
        if up > WALKABLE:
            out.append([level(sum(p[2] for p in w) / len(w)), [[round(x, 2), round(y, 2)] for x, y, _z in w], int(up < STAIRS)])
    return out


def connections(cells):
    """Where the floors join: a cell with floor on two floors (a ramp, stairs) joins them; neighbouring ones joining
    the same pair are one way through. [{n: its number, a: the upper floor, b: the lower, x, y}], top down."""
    joins = []
    for c in cells:
        levels = sorted({f[0] for f in c["floors"]})
        if len(levels) < 2:
            continue
        pts = [pt for f in c["floors"] for pt in f[1]]
        x, y = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
        for a, b in zip(levels[1:], levels[:-1]):
            joins.append((a, b, x, y))
    groups = []
    for a, b, x, y in joins:
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


def floor_of(z, top):
    """The floor a half floor is on: the top of its FLOOR, counted from top (a floor's top half)."""
    return top - FLOOR * math.floor((top - z) / FLOOR)


def overlap(cells, a, b):
    """How much of half floor a lies over half floor b, in m^2 (drawn at 1 px per metre)."""
    from PIL import Image, ImageDraw
    import numpy as np
    pts = [pt for c in cells for _z, poly, _s in c["floors"] for pt in poly]
    x0, y1 = min(p[0] for p in pts), max(p[1] for p in pts)
    w, h = int(max(p[0] for p in pts) - x0) + 2, int(y1 - min(p[1] for p in pts)) + 2

    def mask(z):
        im = Image.new("1", (w, h))
        g = ImageDraw.Draw(im)
        for c in cells:
            for zz, poly, _s in c["floors"]:
                if zz == z:
                    g.polygon([(x - x0, y1 - y) for x, y in poly], fill=1)
        return np.array(im)
    return int((mask(a) & mask(b)).sum())


def top_half(cells, arrival):
    """Which halves make a floor: the pairing (the one you arrive on on top, or below) whose halves lie over each other
    least -- two rooms stacked are two floors, not one."""
    halves = sorted({z for c in cells for z, _p, _s in c["floors"]})
    stacked = lambda top: sum(overlap(cells, z, z - FLOOR_STEP) for z in halves
                              if floor_of(z, top) == floor_of(z - FLOOR_STEP, top))
    return min((arrival, arrival - FLOOR_STEP), key=stacked)


def merged(data):
    """Each floor's two halves drawn as one, by its top -- polygons, spawn points, exits, the arrival -- and the ways
    between them dropped (they're inside it now), and the slopes between them (a half step) drawn flat: only the
    stairs and ramps of a way to another floor are hatched."""
    arrival = (data.get("entrance") or {}).get("arrival", {}).get("floor")
    if arrival is None:
        arrival = max(z for c in data["cells"] for z, _p, _s in c["floors"])
    top = top_half(data["cells"], arrival)
    to = lambda z: floor_of(z, top)
    hs = [z for c in data["cells"] for z, _p, _s in c["floors"]] + [s["floor"] for s in data["spawns"] + data["exits"]]
    halves = [min(hs) - FLOOR_STEP + FLOOR_STEP * i for i in range(int((max(hs) - min(hs)) / FLOOR_STEP) + 3)]
    ways = []                      # (the middles of the cells a way to another floor runs through)
    for c in data["cells"]:
        if len({to(z) for z, _p, _s in c["floors"]}) > 1:
            pts = [pt for _z, poly, _s in c["floors"] for pt in poly]
            ways.append((sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)))
    near_way = lambda poly: any(math.dist(w, (sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly))) < WAY
                                for w in ways)
    for c in data["cells"]:
        c["floors"] = [[to(z), poly, int(sloped and near_way(poly))] for z, poly, sloped in c["floors"]]
    for s in data["spawns"] + data["exits"]:
        s["floor"] = to(s["floor"])
    if data.get("entrance"):
        data["entrance"]["arrival"]["floor"] = to(data["entrance"]["arrival"]["floor"])
    joins = [c for c in data["connections"] if to(c["a"]) != to(c["b"])]
    for n, c in enumerate(joins, 1):
        c.update(n=n, a=to(c["a"]), b=to(c["b"]))
    data["connections"] = joins
    data["merged"] = {f"{z:g}": to(z) for z in halves if to(z) != z}       # (where you stand: which floor)
    return data


def build(landblock, name, core):
    game = Path(game_dir())
    cell_dat, portal_dat = DatFile(str(game / "cell.dat")), DatFile(str(game / "portal.dat"))
    monsters = json.loads((ROOT / "companion" / "data" / "monsters.json").read_text(encoding="utf-8"))
    cells = []
    for i in range(0x100, 0x1000):
        c = env_cell(cell_dat, (landblock << 16) | i)
        if c is None:
            break
        cells.append({"id": i, "z": level(c["origin"][2]), "floors": floors(portal_dat, c)})
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
    if door:                                  # (the floor you arrive on: your cell's)
        cell = next((c for c in cells if c["id"] == door["arrival"]["cell"]), None)
        levels = [f[0] for f in cell["floors"]] if cell else []
        door["arrival"]["floor"] = max(set(levels), key=levels.count) if levels else (cell or {}).get("z")
    return merged({"name": name, "landblock": landblock, "cells": cells, "spawns": spawns, "exits": exits,
                   "connections": connections(cells), "entrance": door})


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
