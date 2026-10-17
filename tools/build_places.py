"""Build companion/data/places.json (portals, lifestones, towns) from the AC1999 server's world database.

    python tools/build_places.py <path to the ac1999 server checkout>

Portals: every placed instance (3-Core/6 LandBlockExtendedData) of a portal weenie (type 7) that has a
destination (weenie_properties_position type 2), with its level limits. Lifestones: placed instances of
wcid 509. Towns: the destinations of the "Portal to <Town>" portals, named after them; then every other town
with a name sign (a Generic/Misc weenie whose class is its name and "sign": linsign, sign-eastham, glendensign for
Glenden Wood; a shop's sign is named for its owner, linarchmagesign) at the middle of where its signs stand. Outposts
are left out. Then the settlements with neither (SETTLEMENTS): where an NPC who lives there stands.
Positions are kept as [cell, x, y] with map coordinates (NS, EW) for outdoor ones (indoor = dungeon).
"""
import glob
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from companion.worldmap import coords  # noqa: E402

LIFESTONE = 509
# Settlements with no town portal or town sign, found as clusters of buildings (cell.dat) with people in them: (name,
# an NPC placed there, roughly where (NS, EW) -- the placement of that NPC nearest it is the town's spot)
SETTLEMENTS = (
    ("Kara", "Shopkeep Kuro of Kara", (-83.2, 47.2)),
    ("Stonehold", "Vivaina, Shopkeep of Stonehold", (68.7, -21.8)),
    ("Plateau Village", "Scribe Finaver", (43.6, -42.8)),
    ("Wai Jhou", "Dou Ran", (-62.1, -51.5)),
    ("Crater", "Geldeb Finol", (64.8, 13.7)),
    ("Beachside", "Healer", (-9.6, -72.0)),
    ("Settlement 76N 49W", "Weaponsmith", (76.0, -48.8)),
    ("Settlement 60N 4W", "Wandering Bowyer", (60.0, -4.0)),
    ("Settlement 23N 8W", "Merchant", (23.2, -8.0)),
    ("Settlement 67S 28E", "Peddler", (-67.2, 28.1)),
    ("Settlement 82S 35W", "Merchant", (-82.3, -34.8)),
)
INSTANCE = re.compile(r"\((0x[0-9A-Fa-f]+),\s*(\d+),\s*(0x[0-9A-Fa-f]+),\s*([-\d.]+),\s*([-\d.]+),\s*([-\d.]+),")
DESTINATION = re.compile(r"\(\d+,\s*2,\s*(0x[0-9A-Fa-f]+),\s*([-\d.]+),\s*([-\d.]+),\s*([-\d.]+),[^)]*\)\s*/\* Destination \*/")


def place(cell, x, y):
    c = coords(cell, x, y)
    return {"cell": cell, "x": round(x, 2), "y": round(y, 2), "ns": round(c[0], 2) if c else None,
            "ew": round(c[1], 2) if c else None}


def town_signs(root):
    """{wcid: town name} of the town name signs (see the module's doc): a sign class (sign-<x> or <x>sign) whose <x>
    is the name squashed (or starts it: glenden, Glenden Wood); of names sharing an <x>, the exact one."""
    found = {}       # stem -> [(exact?, wcid, name)]
    for f in glob.glob(str(root / "9 WeenieDefaults/SQL/Generic/Misc/*.sql")):
        name = os.path.basename(f)[6:-4].strip()
        if re.search(r"mile|outpost|sign|lifestone|armorer|blacksmith|smithy|bowyer|fletcher|grocer|healer|tailor|"
                     r"jewel|scribe|tavern|supplies|provision", name, re.I) or "_" in name:    # a shop's, not a town's
            continue
        head = re.search(r"VALUES \((\d+), '([^']*)'", open(f, encoding="utf-8", errors="replace").read(400))
        if not head or not re.match(r"^sign-[a-z]+$|^[a-z]+sign$", head.group(2)):
            continue
        stem = re.sub(r"^sign-|sign$", "", head.group(2))
        flat = re.sub(r"[^a-z]", "", name.lower())
        if len(stem) >= 3 and flat.startswith(stem):
            found.setdefault(stem, []).append((flat == stem, int(head.group(1)), name))
    out = {}
    for options in found.values():
        exact = [o for o in options if o[0]]
        for _e, wcid, name in (exact or options if len(options) == 1 or exact else []):
            out[wcid] = name
    return out


def main():
    root = Path(sys.argv[1]) / "Server/Database/AC1999-World/3-Core"
    portals = {}
    for f in glob.glob(str(root / "9 WeenieDefaults/SQL/**/*.sql"), recursive=True):
        t = open(f, encoding="utf-8", errors="replace").read()
        head = re.search(r"VALUES \((\d+), '[^']*', (\d+),", t)
        if not head or head.group(2) != "7":
            continue
        dest = DESTINATION.search(t)
        if not dest:
            continue
        name = re.search(r"\(\d+,\s*1, '((?:[^']|'')*)'\) /\* Name \*/", t)
        level = lambda label: (lambda m: int(m.group(1)) if m else None)(
            re.search(r"\(\d+,\s*\d+,\s*(-?\d+)\) /\* " + label + r" \*/", t))
        portals[int(head.group(1))] = {
            "name": name.group(1).replace("''", "'") if name else "Portal",
            "to": place(int(dest.group(1), 16), float(dest.group(2)), float(dest.group(3))),
            "min_level": level("MinLevel"), "max_level": level("MaxLevel"),
            "quest": bool(re.search(r"/\* QuestRestriction \*/", t)),
        }
    signs = town_signs(root)
    anchors = {name for _t, name, _at in SETTLEMENTS}
    anchor_wcids = {int(os.path.basename(f)[:5]): os.path.basename(f)[6:-4].strip()
                    for f in glob.glob(str(root / "9 WeenieDefaults/SQL/**/*.sql"), recursive=True)
                    if os.path.basename(f)[6:-4].strip() in anchors}
    anchor_at = {}     # NPC name -> where each of its placements stands
    sign_at = {}       # town name -> where its signs stand
    placed, lifestones = [], []
    for f in sorted(glob.glob(str(root / "6 LandBlockExtendedData/SQL/*.sql"))):
        for m in INSTANCE.finditer(open(f, encoding="utf-8", errors="replace").read()):
            wcid, cell = int(m.group(2)), int(m.group(3), 16)
            at = place(cell, float(m.group(4)), float(m.group(5)))
            if wcid in anchor_wcids:     # (most stand indoors: a building's cells sit on its landblock, so its spot holds)
                x, y = float(m.group(4)), float(m.group(5))
                outdoors = cell & 0xFFFF0000 | 0x0001 + int(y // 24) % 8 + 8 * (int(x // 24) % 8)
                anchor_at.setdefault(anchor_wcids[wcid], []).append(place(outdoors, x, y))
            if wcid in signs:
                at = place(cell, float(m.group(4)), float(m.group(5)))
                if at["ns"] is not None:
                    sign_at.setdefault(signs[wcid], []).append(at)
            if wcid == LIFESTONE:
                if at["ns"] is not None:
                    lifestones.append(at)
            elif wcid in portals:
                placed.append(dict(portals[wcid], wcid=wcid, at=at))
    towns = {}
    for p in placed:
        m = re.match(r"Portal to (.+)$", p["name"])
        if m and p["to"]["ns"] is not None and not p["min_level"]:
            towns.setdefault(m.group(1), p["to"])
    known = {n.lower() for n in towns}
    for name, ats in sign_at.items():
        if name.lower() not in known:       # (the portals' Hebian-To is the sign's Hebian-to)
            mid = min(ats, key=lambda a: sum((a["ns"] - b["ns"]) ** 2 + (a["ew"] - b["ew"]) ** 2 for b in ats))
            towns[name] = dict(mid, ns=round(sum(a["ns"] for a in ats) / len(ats), 2),
                               ew=round(sum(a["ew"] for a in ats) / len(ats), 2))
    for name, npc, (ns, ew) in SETTLEMENTS:
        ats = anchor_at.get(npc) or []
        if ats and name.lower() not in known:
            at = min(ats, key=lambda a: (a["ns"] - ns) ** 2 + (a["ew"] - ew) ** 2)
            if (at["ns"] - ns) ** 2 + (at["ew"] - ew) ** 2 < 4:
                towns[name] = at
    out = {"source": "AC1999 server world database", "portals": placed, "lifestones": lifestones,
           "towns": [dict(at, name=n) for n, at in sorted(towns.items())]}
    dest = Path(__file__).resolve().parent.parent / "companion/data/places.json"
    dest.write_text(json.dumps(out, separators=(",", ":")))
    outdoor = sum(1 for p in placed if p["at"]["ns"] is not None and p["to"]["ns"] is not None)
    print(f"{len(placed)} placed portals ({outdoor} outdoor to outdoor), {len(lifestones)} lifestones, "
          f"{len(towns)} towns -> {dest}")


if __name__ == "__main__":
    main()
