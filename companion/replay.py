"""Replay a capture through the decoder and print what the tracker saw.

    python -m companion.replay captures/session-....cap [--dat PATH] [--events]
"""

import argparse
import json
from pathlib import Path

from .capture import read_capture
from .dat import DatFile, load_components, load_spells
from .pipeline import Pipeline
from .tracker import Tracker
from .launcher import game_dir


def main():
    p = argparse.ArgumentParser(prog="companion.replay")
    p.add_argument("capture")
    p.add_argument("--dat", help="portal.dat (default: the game folder, Settings > Game)")
    p.add_argument("--events", action="store_true", help="print every decoded event")
    args = p.parse_args()

    try:
        dat = DatFile(args.dat or str(Path(game_dir()) / "portal.dat"))
        spells, components = load_spells(dat), load_components(dat)
    except OSError:
        spells, components = {}, {}
    clock = {"t": 0.0}
    tracker = Tracker(spells, clock=lambda: clock["t"], components=components)
    pipeline = Pipeline(tracker)
    if args.events:
        real_handle = tracker.handle

        def handle(ev, now=None):
            print(f"{now:.3f}", json.dumps(ev, default=str)[:300])
            real_handle(ev, now)

        tracker.handle = handle

    for ts, kind, _index, payload in read_capture(args.capture):
        clock["t"] = ts
        if kind == "control":
            print(f"{ts:.3f} control {payload}")
            if payload.get("event") == "session":
                tracker.reset(payload.get("server"))
                pipeline.reset()
            continue
        pipeline.feed(kind, payload, ts)
        tracker.tick(ts)

    print("message counts:", dict(pipeline.stats))
    print(json.dumps(tracker.snapshot(), indent=1, default=str))


if __name__ == "__main__":
    main()
