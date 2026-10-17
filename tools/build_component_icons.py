"""Build companion/data/component_icons.json ({component name: icon id, hex}) from the AC1999 server's world database.

    python tools/build_component_icons.py <path to the ac1999 server checkout>

Every spell component weenie (3-Core/9 WeenieDefaults/SQL/SpellComponent): its Name and Icon (data id 8). The
Spells page's research panel draws them (/icon/<id>.png, from portal.dat).
"""
import glob
import json
import os
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "companion" / "data" / "component_icons.json"


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    pattern = os.path.join(root, "**", "9 WeenieDefaults", "SQL", "SpellComponent", "**", "*.sql")
    icons = {}
    for path in glob.glob(pattern, recursive=True):
        text = open(path, encoding="utf-8", errors="ignore").read()
        name = re.search(r"'([^']+)'\) /\* Name \*/", text)
        icon = re.search(r"\(\s*\d+,\s*8,\s*(0x[0-9A-Fa-f]+)\) /\* Icon \*/", text)
        if name and icon:
            icons.setdefault(name.group(1), f"{int(icon.group(1), 16):08x}")
    OUT.write_text(json.dumps(dict(sorted(icons.items())), indent=1) + "\n", encoding="utf-8")
    print(f"{len(icons)} component icons -> {OUT}")


if __name__ == "__main__":
    main()
