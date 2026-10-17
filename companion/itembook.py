"""The items your characters have appraised, shared between them: an item keeps its id when it changes hands, so
its appraisal follows it from one character to another.

Each item has an owner (the character holding it) or none, and a "gone" time: when it left whoever held it (sold,
dropped, handed over), or, for one appraised where none of yours holds it (a vendor's, the ground), when it was
appraised. A gone item is kept GONE_KEEP, long enough to log another character in and pick it up there; past that
it's dropped. Kept in memory, written through to the store's items table."""

GONE_KEEP = 3600.0       # seconds an item with no owner is kept


class ItemBook:
    def __init__(self, store=None, keep=GONE_KEEP):
        self.store = store
        self.keep = keep
        self.rows = store.items() if store else {}

    def _save(self, row):
        self.rows[row["guid"]] = row
        if self.store:
            self.store.item_save(row)

    def appraised(self, guid, name, text, profile, ts, holder=None):
        """An item appraised at `ts` (an older appraisal than the one kept changes nothing); holder: the character
        holding it, if one of yours is."""
        row = self.rows.get(guid)
        if row and (row.get("appraised") or 0) >= ts:
            return
        row = dict(row or {"guid": guid, "owner": None, "gone": ts}, name=name, text=text, profile=profile, appraised=ts)
        if holder:
            row.update(owner=holder, gone=None)
        self._save(row)

    def held(self, character, guids):
        """The items `character` holds now: theirs, and not gone."""
        for g in guids:
            row = self.rows.get(g)
            if row and (row["owner"] != character or row["gone"] is not None):
                self._save(dict(row, owner=character, gone=None))

    def left(self, character, guids, now):
        """Items that left `character` (sold, dropped, handed over): gone from now, kept a while."""
        for g in guids:
            row = self.rows.get(g)
            if row and row["owner"] == character and row["gone"] is None:
                self._save(dict(row, gone=now))

    def prune(self, now):
        """Drop the items gone longer than the keep."""
        old = [g for g, r in self.rows.items() if r["gone"] is not None and now - r["gone"] > self.keep]
        for g in old:
            del self.rows[g]
        if old and self.store:
            self.store.item_delete(old)

    def listing(self):
        """Every item, newest appraisal first, with when a gone one will be dropped (keep_until)."""
        out = [{k: v for k, v in r.items() if k != "profile"} | {"keep_until": r["gone"] + self.keep if r["gone"] is not None else None}
               for r in self.rows.values()]
        return sorted(out, key=lambda r: -(r.get("appraised") or 0))
