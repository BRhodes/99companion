"""Build companion/data/comp_prices.json: a baseline for pricing components without a vendor open.

Takes the Zaikhal archmage's stock message (Asaina al-Arqis) from the session journals: her markup and
each item's base value. The companion prices Fillcomps with it whenever no vendor window is open.

    python -m tools.build_comp_prices [--journals journals]
"""
import argparse
import glob
import json
from pathlib import Path

from companion.capture import read_capture
from companion.pipeline import Pipeline
from companion.tracker import Tracker

VENDOR = 0x77F90003          # Asaina al-Arqis, archmage, Zaikhal (landblock 0x7F90)
OUT = Path(__file__).resolve().parent.parent / "companion" / "data" / "comp_prices.json"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--journals", default="journals")
    args = p.parse_args()
    found = None
    for path in sorted(glob.glob(str(Path(args.journals) / "session-*.cap"))):
        tr = Tracker({})
        pipe = Pipeline(tr)
        for ts, kind, _index, payload in read_capture(path):
            if kind in ("c2s", "s2c"):
                pipe.feed(kind, payload, ts)
                if tr.vendor and tr.vendor["id"] == VENDOR:
                    found = tr.vendor            # the newest one wins
    if not found:
        raise SystemExit("no stock message from the Zaikhal archmage in the journals")
    values = {i["name"]: i["value"] for i in found["items"] if i.get("name") and i.get("value")}
    OUT.write_text(json.dumps({"vendor": "Asaina al-Arqis, archmage, Zaikhal", "sell_price": found["sell_price"],
                               "values": dict(sorted(values.items()))}, indent=1), encoding="utf-8")
    print(f"{len(values)} items at x{found['sell_price']} -> {OUT}")


if __name__ == "__main__":
    main()
