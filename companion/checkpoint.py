"""Session checkpoints, so a companion restart doesn't replay every journal from the start.

With a relay that supports it, a checkpoint also records the journal offset it covers (asked of the relay
with "positions" and saved when the reply arrives, so the state matches it exactly); the companion then
opens its tap connection with "resume" and the relay streams each session from that offset instead of the
start. With an older relay it falls back to streaming everything and skipping the counted records.

The relay always streams a session's journal from its first record, and rebuilding the tracker from
millions of records is what makes a restart slow. So the companion saves, every CHECKPOINT_EVERY seconds,
each live session's tracker and packet-reassembly state together with how many records it had fed. A
restarted companion restores that and skips that many records (they still stream past, but aren't
decoded), processing only what came after.

Shared, read-only data (spells, components, recipes, formulas, the burn model) and live handles (the
database, callbacks) aren't saved; the restored tracker gets the new process's. A tracker field added
since the checkpoint keeps its fresh default. Bump VERSION when the meaning of saved state changes, and
older checkpoints are ignored (a full replay rebuilds them).
"""
import logging
import os
import pickle
import time
from pathlib import Path

log = logging.getLogger("companion")

VERSION = 3       # 2: worn items tracked from the equip event (0x23); 3: the order of things in packs
CHECKPOINT_EVERY = 60.0
KEEP_DAYS = 2
SHARED = {"spells", "components", "skill_formulas", "vital_formulas", "recipes", "recipes_by_tool", "burn",
          "store", "on_change", "clock", "_stock", "_stock_cache", "prefs"}


class Checkpoints:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.saved_at = {}
        cutoff = time.time() - KEEP_DAYS * 86400
        for f in self.folder.glob("*.ckpt"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)

    def _path(self, session_id):
        return self.folder / f"{session_id}.ckpt"

    def offsets(self):
        """{session id: journal offset} of the checkpoints that know one: where the relay can resume."""
        out = {}
        for f in self.folder.glob("*.ckpt"):
            try:
                data = pickle.loads(f.read_bytes())
            except Exception:
                continue
            if data.get("version") == VERSION and isinstance(data.get("offset"), int):
                out[f.stem] = data["offset"]
        return out

    def save(self, view, offset=None):
        tr = view.tracker
        model, tr.ledger.model = tr.ledger.model, None      # the ledger points at the shared burn model
        try:
            data = pickle.dumps({
                "version": VERSION, "seen": view.seen, "last_ts": view.last_ts, "ended": view.ended, "offset": offset,
                "tracker": {k: v for k, v in tr.__dict__.items() if k not in SHARED},
                "reassemblers": view.pipeline.reassemblers, "stats": view.pipeline.stats,
            }, protocol=pickle.HIGHEST_PROTOCOL)
        finally:
            tr.ledger.model = model
        path = self._path(view.info.get("id"))
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        self.saved_at[view.info.get("id")] = time.time()

    def restore(self, view):
        """Load a session's checkpoint into a fresh view; True when it did (view.skip records follow)."""
        path = self._path(view.info.get("id"))
        try:
            data = pickle.loads(path.read_bytes())
        except FileNotFoundError:
            return False
        except Exception as e:  # unreadable or from an incompatible version of the code: replay instead
            log.info("checkpoint for %s unusable (%s); replaying the whole journal", view.info.get("id"), e)
            return False
        if data.get("version") != VERSION:
            return False
        tr = view.tracker
        tr.__dict__.update(data["tracker"])
        tr.ledger.model = tr.burn
        tr.burn.set_account(tr.account)
        view.pipeline.reassemblers = data["reassemblers"]
        view.pipeline.stats = data["stats"]
        # the stream starts over (count back up through the skipped records) unless the relay says it resumed
        # at the checkpoint's offset ("resumed", handled by the companion), when nothing needs skipping
        view.skip = data["seen"]
        view.resume_seen = data["seen"]
        view.last_ts, view.ended = data["last_ts"], data["ended"]
        self.saved_at[view.info.get("id")] = time.time()
        log.info("restored %s from its checkpoint: skipping %d records", view.info.get("id"), data["seen"])
        return True

    def due(self, view, now):
        return now - self.saved_at.get(view.info.get("id"), 0) >= CHECKPOINT_EVERY

    def forget(self, session_id):
        self._path(session_id).unlink(missing_ok=True)
        self.saved_at.pop(session_id, None)
