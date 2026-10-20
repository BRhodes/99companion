"""Build companion/data/monsters.json from the server's creature weenies (ac1999 SQL).

Per creature (keyed by class id): name, level, attributes, the defense skills' init levels (melee, missile,
magic), an XpOverride if it has one, the physical armor modifiers (ArmorModVs*, float 13-19),
the innate resistances that magic damage uses (Resist*, float 64-70), and each body part's base armor
with its chance of being hit at each attack height from the front and from behind, and for a part it
attacks with, its damage (d_Val), variance (d_Var) and damage type.

    python -m tools.build_monsters --sql <server checkout>/Database/AC1999-World/3-Core/9 WeenieDefaults/SQL/Creature
"""
import argparse
import json
import re
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "companion" / "data" / "monsters.json"
DEFENSES = {6, 7, 15}      # melee / missile / magic defense: their init levels (the attribute part is added at use)
TYPES = ["slash", "pierce", "bludgeon", "fire", "cold", "acid", "electric"]
ARMOR_MOD = {13: "slash", 14: "pierce", 15: "bludgeon", 16: "cold", 17: "fire", 18: "acid", 19: "electric"}
DTYPE = {1: "slash", 2: "pierce", 4: "bludgeon", 8: "cold", 16: "fire", 32: "acid", 64: "electric"}
RESIST = {64: "slash", 65: "pierce", 66: "bludgeon", 67: "fire", 68: "cold", 69: "acid", 70: "electric"}
PARTS = {0: "Head", 1: "Chest", 2: "Abdomen", 3: "Upper arm", 4: "Lower arm", 5: "Hand", 6: "Upper leg",
         7: "Lower leg", 8: "Foot", 9: "Horn", 10: "Front leg", 12: "Front foot", 13: "Rear leg", 15: "Rear foot",
         16: "Torso", 17: "Tail", 18: "Arm", 19: "Leg", 20: "Claw", 21: "Wings", 22: "Breath", 23: "Tentacle",
         24: "Upper tentacle", 25: "Lower tentacle", 26: "Cloak"}


def section(text, table):
    """The VALUES rows of one INSERT INTO `table`, as lists of raw fields."""
    m = re.search(r"INSERT INTO `%s` \([^)]*\)\s*VALUES(.*?);\s*$" % table, text, re.S | re.M)
    if not m:
        return []
    rows = []
    for line in m.group(1).splitlines():
        line = re.sub(r"/\*.*?\*/", "", line).strip().lstrip(",").strip()
        if line.startswith("("):
            rows.append([f.strip().strip("'") for f in line.strip("();").split(",")])
    return rows


def parse(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    ints = {int(r[1]): r[2] for r in section(text, "weenie_properties_int")}
    floats = {int(r[1]): float(r[2]) for r in section(text, "weenie_properties_float")}
    strings = {int(r[1]): r[2] for r in section(text, "weenie_properties_string")}
    wcid = int(re.search(r"`class_Id`[^;]*VALUES \((\d+)", text, re.S).group(1))
    parts = []
    for r in section(text, "weenie_properties_body_part"):
        key, armor = int(r[1]), int(float(r[5]))
        hlf, mlf, llf, hrf, mrf, lrf, hlb, mlb, llb, hrb, mrb, lrb = (float(x) for x in r[15:27])
        part = {"name": PARTS.get(key, f"Part {key}"), "armor": armor,
                "front": [hlf + hrf, mlf + mrf, llf + lrf], "back": [hlb + hrb, mlb + mrb, llb + lrb]}
        if int(float(r[3])) > 0:          # it attacks with this part
            part.update(dmg=int(float(r[3])), var=float(r[4]), dtype=DTYPE.get(int(r[2]), str(r[2])))
        parts.append(part)
    if not parts:
        return None
    attributes = {int(r[1]): int(r[2]) for r in section(text, "weenie_properties_attribute")}
    skills = {int(r[1]): int(r[5]) for r in section(text, "weenie_properties_skill") if int(r[1]) in DEFENSES}
    extra = {"attributes": attributes, "skills": skills}
    # its health: the Health vital's own levels plus Endurance / 2 (the vital formula, Attribute2ndTable 1: (0,1,0,2,2,0));
    # the server sets it once, at spawn (SetMaxVitals): every one of a kind has the same
    vitals = {int(r[1]): (int(r[2]), int(r[3])) for r in section(text, "weenie_properties_attribute_2nd")}
    init, raised = vitals.get(1, (0, 0))
    extra["health"] = init + raised + int(attributes.get(2, 0) / 2 + 0.5)
    # who it goes after on sight, and from how far: Tolerance (int 67; 0: anyone in range -- else never, or only once
    # attacked or bothered) and VisualAwarenessRange (float 31; the server's default 40 m)
    extra["tolerance"] = int(ints.get(67, 0))
    if 31 in floats:
        extra["aggro"] = floats[31]
    if 146 in ints:                       # XpOverride: an authored award instead of the level curve
        extra["xp_override"] = int(ints[146])
    return wcid, {**extra, "name": strings.get(1, path.stem), "level": int(ints.get(25, 0)),
                  "armor_mod": {t: floats.get(k, 1.0) for k, t in ARMOR_MOD.items()},
                  "resist": {t: floats.get(k, 1.0) for k, t in RESIST.items()}, "parts": parts}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sql", required=True, help="the server checkout's ...\\9 WeenieDefaults\\SQL\\Creature folder")
    args = p.parse_args()
    out = {}
    for path in sorted(Path(args.sql).rglob("*.sql")):
        got = parse(path)
        if got:
            out[str(got[0])] = got[1]
    OUT.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"{len(out)} creatures -> {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
