"""Build companion/data/spell_mods.json: what each enchantment changes, from the server's spell table.

    python tools/build_spellmods.py <path to the ac1999 server checkout>

The 1999 portal.dat spell table carries no stat modifier; the server reads it from
3-Core/2 SpellTableExtendedData. Output: {spell id: [type, key, value]} (StatMod: type flags such as
0x8 Float / 0x4000 Multiplicative / 0x8000 Additive, the stat key, and the amount).
"""
import glob
import json
import re
import sys
from pathlib import Path

ROW = re.compile(r"VALUES \((\d+), '(?:[^']|'')*', (\d+) /\*[^*]*\*/, (\d+)(?: /\*[^*]*\*/)?, ([-\d.]+),")


def main():
    root = Path(sys.argv[1]) / "Server/Database/AC1999-World/3-Core/2 SpellTableExtendedData/SQL"
    mods = {}
    for f in glob.glob(str(root / "*.sql")):
        m = ROW.search(open(f, encoding="utf-8", errors="replace").read())
        if m:
            value = float(m.group(4))
            mods[int(m.group(1))] = [int(m.group(2)), int(m.group(3)), int(value) if value.is_integer() else value]
    dest = Path(__file__).resolve().parent.parent / "companion/data/spell_mods.json"
    dest.write_text(json.dumps(mods, separators=(",", ":"), sort_keys=True))
    print(f"{len(mods)} spells -> {dest}")


if __name__ == "__main__":
    main()
