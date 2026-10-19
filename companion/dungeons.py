"""Dungeon spawn points and their respawn timers: the Map page's dungeon view.

A dungeon's floor plan and creature spawn points come from companion/data/dungeons/<landblock>.json
(tools/build_dungeon.py). SpawnClock follows each spawn point from what the server shows you there:

  * a creature that comes into view (its create message, with where it is) within MATCH metres of a free spawn
    point of its kind is that point's -- alive;
  * its death (the server's death message, naming it) starts the point's clock: it's due back `delay` seconds later
    (the spawner's own respawn time, 7 minutes in Crater Caves);
  * a point whose creature you never saw is unknown, and so is everything once you've been away from the dungeon
    longer than RESET: the server has unloaded it by then, and it comes back fully stocked (a landblock goes dormant
    10 minutes after the last player leaves, and is freed 5 minutes later).

A respawn you don't see (out of view) shows as "due"; the creature that comes into view there makes it alive again.
"""
import json
import math
from pathlib import Path

DATA = Path(__file__).parent / "data" / "dungeons"
MATCH = 20.0             # metres between a creature first seen and a spawn point of its kind for it to be that point's
EARLY = 30.0             # seconds before its time a point can't be back: a creature of its kind there then is another's
RESET = 15 * 60.0        # seconds away after which the dungeon has reset (dormant 10 min, then freed 5 min later)
CREATURE = 0x10          # item type
_data = {}


def load(landblock):
    """A dungeon's data, or None when there's none for that landblock."""
    if landblock not in _data:
        f = DATA / f"{landblock:04X}.json"
        try:
            _data[landblock] = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
        except (OSError, ValueError):
            _data[landblock] = None
    return _data[landblock]


def available():
    """[{landblock: "019A", name}] of the dungeons there's a map of."""
    out = []
    for f in sorted(DATA.glob("*.json")):
        try:
            out.append({"landblock": f.stem, "name": json.loads(f.read_text(encoding="utf-8")).get("name") or f.stem})
        except (OSError, ValueError):
            pass
    return out


def raw(landblock):
    """The data file's bytes (the dashboard draws the plan from it), or None."""
    f = DATA / f"{landblock:04X}.json"
    return f.read_bytes() if f.exists() else None


class SpawnClock:
    """{landblock: {"points": [{guid, seen, died}], "last": when you were last there}}."""

    def __init__(self):
        self.state = {}

    def _points(self, lb, now):
        data = load(lb)
        if not data:
            return None, None
        st = self.state.get(lb)
        if st is None or len(st["points"]) != len(data["spawns"]):
            st = self.state[lb] = {"points": [{} for _ in data["spawns"]], "last": now}
        return data, st

    def presence(self, cell, now):
        """You're at `cell`: a dungeon with data keeps its clocks (or starts afresh after RESET away)."""
        lb = cell >> 16
        if (cell & 0xFFFF) < 0x100 or not load(lb):
            return
        data, st = self._points(lb, now)
        if st["last"] is not None and now - st["last"] > RESET:
            st["points"] = [{} for _ in data["spawns"]]          # it's been unloaded and restocked since
        st["last"] = now

    def seen(self, guid, wcid, cell, x, y, z, now, alive=lambda g: True):
        """A creature came into view. alive(guid): whether a guid is still in view (a point held by one that isn't
        can be taken over: its death may have happened out of view)."""
        lb = cell >> 16
        if (cell & 0xFFFF) < 0x100:
            return
        data, st = self._points(lb, now)
        if not data:
            return
        points = st["points"]
        if any(p.get("guid") == guid for p in points):
            return
        near = sorted((math.dist((x, y, z), (s["x"], s["y"], s["z"])), i) for i, s in enumerate(data["spawns"])
                      if s["wcid"] == wcid)
        early = lambda p, s: p.get("died") is not None and s.get("delay") and now < p["died"] + s["delay"] - EARLY
        for free in (lambda p: not p.get("guid"), lambda p: p.get("guid") and not alive(p["guid"])):
            for d, i in near:
                if d > MATCH:
                    break
                if free(points[i]) and not early(points[i], data["spawns"][i]):     # (one back too soon is another's)
                    points[i] = {"guid": guid, "seen": now}
                    return

    def died(self, guid, now):
        for st in self.state.values():
            for p in st["points"]:
                if p.get("guid") == guid:
                    p.clear()
                    p["died"] = now
                    return

    def snapshot(self, lb, now):
        """{landblock, name, points: [{state: alive | dead | due | unknown, back: when it's due (dead / due)}]}"""
        data, st = self._points(lb, now)
        if not data:
            return None
        out = []
        for s, p in zip(data["spawns"], st["points"]):
            if p.get("guid"):
                out.append({"state": "alive"})
            elif p.get("died") is not None and s.get("delay"):
                back = p["died"] + s["delay"]
                out.append({"state": "dead" if back > now else "due", "back": back})
            else:
                out.append({"state": "unknown"})
        return {"landblock": f"{lb:04X}", "name": data["name"], "points": out, "last": st["last"]}
