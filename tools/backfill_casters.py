"""One-off: label stored casts with the character who cast them, by replaying the session journals.

Casts recorded before the tracker stored "character" on them have only a session id. This replays
each journal through a tracker, notes which character was logged in at each cast's time, and writes
that into the cast records. Safe to run again: records that already have a character are left alone.

    python -m tools.backfill_casters [--db companion.sqlite3] [--journals journals]
"""
import argparse
import bisect
import json
import sqlite3
from pathlib import Path

from companion.capture import read_capture
from companion.messages import decode
from companion.pipeline import Pipeline
from companion.tracker import Tracker


def characters_over_time(path):
    """[(ts, character)] each time the logged-in character changes in a journal."""
    tr = Tracker({})
    pipe = Pipeline(tr)
    changes, last = [], None
    for ts, kind, _index, payload in read_capture(path):
        if kind == "control":
            continue
        pipe.feed(kind, payload, ts)
        if tr.character != last:
            last = tr.character
            changes.append((ts, last))
    return changes


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="companion.sqlite3")
    p.add_argument("--journals", default="journals")
    args = p.parse_args()
    db = sqlite3.connect(args.db)
    rows = db.execute("SELECT id, ts, session, data FROM events WHERE kind = 'cast'").fetchall()
    sessions = {s for _, _, s, d in rows if "character" not in json.loads(d)}
    timelines = {}
    for s in sorted(x for x in sessions if x):
        path = Path(args.journals) / f"session-{s}.cap"
        if path.exists():
            timelines[s] = characters_over_time(path)
            print(s, [c for _, c in timelines[s] if c])
        else:
            print(s, "journal gone: left unlabelled")
    labelled = 0
    for rid, ts, s, d in rows:
        data = json.loads(d)
        if "character" in data or s not in timelines:
            continue
        line = timelines[s]
        i = bisect.bisect_right([t for t, _ in line], ts) - 1
        if i >= 0 and line[i][1]:
            data["character"] = line[i][1]
            db.execute("UPDATE events SET data = ? WHERE id = ?", (json.dumps(data), rid))
            labelled += 1
    db.commit()
    print(f"labelled {labelled} of {len(rows)} casts")


if __name__ == "__main__":
    main()
