"""Build companion/data/dungeons/<landblock>.json: a dungeon's floor plan and its creature spawn points, for the Map
page's dungeon view (and its respawn timers: companion/dungeons.py).

    python -m tools.build_dungeon 019A "Crater Caves" --db <server checkout>/Database/AC1999-World/3-Core

Floor plan: every cell of the landblock's dungeon (cell.dat EnvCells 0xLLLL0100 on), each placed by its frame, with the
upward-facing polygons of its cell structure (portal.dat Environments) as its floor. The 1999 dats pad both to four
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
    """(vertices {id: (x, y, z)}, polygons [[vertex ids]]) of one cell structure of an environment."""
    if (eid, key) in _cache:
        return _cache[(eid, key)]
    b = portal_dat.get(eid)
    r = Reader(b)
    r.take("I")
    for _ in range(r.take("I")):
        k = r.take("I")
        npoly = r.take("I")
        r.take("II")                              # physics polygons, portals
        r.take("i")                               # vertex type
        verts = {}
        for _ in range(r.take("I")):
            vk, nuv = r.take("H"), r.take("H")
            verts[vk] = r.take("3f")
            r.take("3f")                          # normal
            r.p += 8 * nuv
        polys = []
        for _ in range(npoly):
            r.take("h")
            pts, stip = r.take("B"), r.take("B")
            sides = r.take("i")
            r.take("hh")
            polys.append([r.take("h") for _ in range(pts)] if pts > 1 else [r.take("h")] if pts else [])
            if not stip & 4:
                r.p += pts
            if sides == 1 and not stip & 8:
                r.p += pts
            r.align()
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


def floors(portal_dat, c):
    """A cell's floor: its upward-facing polygons in landblock x / y."""
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
        if nz / (math.sqrt(nx * nx + ny * ny + nz * nz) or 1) > 0.7:
            out.append([[round(x, 2), round(y, 2)] for x, y, _z in w])
    return out


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
        cells.append({"id": i, "z": round(c["origin"][2], 1), "floors": floors(portal_dat, c)})
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
                       "x": round(x, 2), "y": round(y, 2), "z": round(z, 2), "delay": delay})
    spawns.sort(key=lambda s: (s["cell"], s["x"], s["y"]))
    exits = [{"name": n, "x": round(x, 2), "y": round(y, 2), "z": round(z, 2), "cell": cid & 0xFFFF}
             for _g, (w, cid, x, y, z, n) in placed.items() if "Surface" in n or "Portal" in n]
    return {"name": name, "landblock": landblock, "cells": cells, "spawns": spawns, "exits": exits}


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
