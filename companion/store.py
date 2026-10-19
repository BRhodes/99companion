"""SQLite history of tracked events, one row per event."""

import bisect
import json
import sqlite3
import time

KEEP_HOURS = 720          # cast history kept in memory per character: the last 30 days, or back to the component
                          # order's start when that's earlier (the database keeps them all)
TRIM_SLACK = 500          # casts added before it's trimmed again

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    session TEXT,
    kind TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_kind_ts ON events(kind, ts);
-- a restarted companion replays the session from the journal; this keeps those rows from doubling
CREATE UNIQUE INDEX IF NOT EXISTS events_once ON events(session, kind, ts);
-- which account each character is on (chat history is the account's: every character on it)
CREATE TABLE IF NOT EXISTS characters (character TEXT PRIMARY KEY, account TEXT NOT NULL);
-- every item appraised, shared by all your characters (an item keeps its id when it changes hands): who holds it
-- (owner), when it was last appraised, and when it left whoever held it (gone: kept a while, in case another
-- of your characters picks it up)
CREATE TABLE IF NOT EXISTS items (guid INTEGER PRIMARY KEY, name TEXT, owner TEXT, appraised REAL, gone REAL,
    text TEXT, data TEXT);
-- the last spell component vendor each character used: its name, prices (sell rate) and wares (its order, values)
CREATE TABLE IF NOT EXISTS comp_vendors (character TEXT PRIMARY KEY, ts REAL NOT NULL, data TEXT NOT NULL);
-- your own corpses (each character's): where they lie and when they rot
CREATE TABLE IF NOT EXISTS corpses (character TEXT NOT NULL, id INTEGER NOT NULL, expires REAL NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (character, id));
-- the mana of the items your characters wear, by item (itemmana.py): its last reading and its time worn since
CREATE TABLE IF NOT EXISTS item_mana (guid INTEGER PRIMARY KEY, data TEXT NOT NULL);
-- what appraisal training has learned about each item in a character's pack (appraise.py)
CREATE TABLE IF NOT EXISTS appraisal_items (character TEXT NOT NULL, object INTEGER NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (character, object));
"""


def trim(history, since):
    """Drop the casts from before `since` (the history is oldest first), in place."""
    del history[:bisect.bisect_left(history, since, key=lambda c: c.get("ts") or 0)]


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self._casts = {}        # character -> their casts, oldest first: read once, then kept up as casts are added
        self._kept = {}         # character -> its length after the last trim
        self._from = {}         # character -> the time it was last trimmed back to (anything earlier: read again)

    def add(self, kind, data, session=None, ts=None):
        ts = time.time() if ts is None else ts
        cur = self.db.execute("INSERT OR IGNORE INTO events (ts, session, kind, data) VALUES (?, ?, ?, ?)",
                              (ts, session, kind, json.dumps(data)))
        self.db.commit()
        # a cast that's new (not a replay of one already stored) joins its character's history in memory, in
        # time order (a replayed older session's casts slot in where they belong)
        if kind == "cast" and cur.rowcount and isinstance(data, dict) and data.get("character") in self._casts:
            history = self._casts[data["character"]]
            record = json.loads(json.dumps(data))      # (as the database would give it back)
            if not history or (record.get("ts") or 0) >= (history[-1].get("ts") or 0):
                history.append(record)
            else:
                bisect.insort(history, record, key=lambda c: c.get("ts") or 0)

    def recent(self, kind, limit=200):
        rows = self.db.execute("SELECT ts, session, data FROM events WHERE kind = ? ORDER BY id DESC LIMIT ?",
                               (kind, limit)).fetchall()
        return [{"ts": ts, "session": s, **json.loads(d)} for ts, s, d in rows]

    def casts(self, character, now=None, since=None):
        """A character's casts from the last KEEP_HOURS (or from `since`, when that's earlier), oldest first. Read
        from the database the first time (and again when `since` reaches back past what's kept), then kept in memory
        (add() appends new ones, the old trimmed off now and then): the same list each call, so don't change it."""
        start = (time.time() if now is None else now) - KEEP_HOURS * 3600
        if since is not None:
            start = min(start, since)
        if character not in self._casts or start < self._from.get(character, start):
            rows = self.db.execute("SELECT data FROM events WHERE kind = 'cast' ORDER BY ts").fetchall()
            self._casts[character] = [c for c in (json.loads(d) for (d,) in rows) if c.get("character") == character]
            self._kept[character] = None
        history = self._casts[character]
        length = self._kept.get(character)
        if length is None or len(history) > length + TRIM_SLACK:
            trim(history, start)
            self._kept[character] = len(history)
            self._from[character] = start
        return history

    def link(self, character, account):
        """Note the account a character is on."""
        self.db.execute("INSERT OR REPLACE INTO characters (character, account) VALUES (?, ?)", (character, account))
        self.db.commit()

    def same_account(self, character):
        """Every character on the account this one's on (it alone when its account isn't known). "@account":
        that account's characters (character select, before any's logged in)."""
        if character.startswith("@"):
            row = (character[1:],)
        else:
            row = self.db.execute("SELECT account FROM characters WHERE character = ?", (character,)).fetchone()
        if not row:
            return [character]
        names = [n for (n,) in self.db.execute("SELECT character FROM characters WHERE account = ?", (row[0],))]
        return names or [character]

    def chat(self, character, before=None, limit=300, kinds=None):
        """The chat lines from earlier on this character's account (every character on it, every login),
        newest first; kinds: only these (tell, tell_out, say, npc, system)."""
        names = self.same_account(character)
        where, args = "", []
        if kinds:      # (the "Logged in as" / "Logged out from" lines come with any)
            where = f" AND json_extract(data, '$.kind') IN ({','.join('?' * (len(kinds) + 2))})"
            args = list(kinds) + ["login", "logout"]
        rows = self.db.execute("SELECT data FROM events WHERE kind = 'chat' AND ts < ? "
                               f"AND json_extract(data, '$.character') IN ({','.join('?' * len(names))})" + where
                               + " ORDER BY ts DESC LIMIT ?", [before or 1e12, *names, *args, limit]).fetchall()
        return [json.loads(d) for (d,) in rows]

    def appraisal_items(self, character):
        """{item id: what appraisal training learned about it} for a character."""
        rows = self.db.execute("SELECT object, data FROM appraisal_items WHERE character = ?", (character,))
        return {obj: json.loads(d) for obj, d in rows}

    def save_appraisal_item(self, character, obj, data):
        self.db.execute("INSERT OR REPLACE INTO appraisal_items (character, object, data) VALUES (?, ?, ?)",
                        (character, obj, json.dumps(data)))
        self.db.commit()

    def save_comp_vendor(self, character, vendor):
        """Remember the component vendor a character used (vendor["ts"]: when; an older one doesn't replace it)."""
        self.db.execute("INSERT INTO comp_vendors (character, ts, data) VALUES (?, ?, ?) ON CONFLICT(character) DO UPDATE "
                        "SET ts = excluded.ts, data = excluded.data WHERE excluded.ts >= comp_vendors.ts",
                        (character, vendor["ts"], json.dumps(vendor)))
        self.db.commit()

    def comp_vendor(self, character):
        """The component vendor a character last used, else the latest any of them did, or None."""
        row = self.db.execute("SELECT data FROM comp_vendors WHERE character = ?", (character,)).fetchone() \
            or self.db.execute("SELECT data FROM comp_vendors ORDER BY ts DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def save_corpse(self, character, corpse):
        """Remember one of a character's corpses (the first sighting stands: a corpse seen again keeps its death)."""
        self.db.execute("INSERT OR IGNORE INTO corpses (character, id, expires, data) VALUES (?, ?, ?, ?)",
                        (character, corpse["id"], corpse["expires"], json.dumps(corpse)))
        self.db.commit()

    def corpses(self, character, now):
        """A character's corpses that haven't rotted yet (the rotted are dropped)."""
        self.db.execute("DELETE FROM corpses WHERE expires <= ?", (now,))
        self.db.commit()
        return [json.loads(d) for (d,) in self.db.execute("SELECT data FROM corpses WHERE character = ?", (character,))]

    def save_item_mana(self, rec):
        """Remember a worn item's mana record (itemmana.py), by its id; not over a newer one."""
        old = self.item_mana(rec["guid"])
        if old and (old.get("at") or 0) > (rec.get("at") or 0):
            return                        # (a journal replayed: what's kept is newer)
        self.db.execute("INSERT OR REPLACE INTO item_mana (guid, data) VALUES (?, ?)", (rec["guid"], json.dumps(rec)))
        self.db.commit()

    def item_mana(self, guid):
        """A remembered item's mana record, or None."""
        row = self.db.execute("SELECT data FROM item_mana WHERE guid = ?", (guid,)).fetchone()
        return json.loads(row[0]) if row else None

    def items(self):
        """{guid: {guid, name, owner, appraised, gone, text, profile}}: the shared item table."""
        rows = self.db.execute("SELECT guid, name, owner, appraised, gone, text, data FROM items").fetchall()
        return {g: {"guid": g, "name": n, "owner": o, "appraised": a, "gone": gone, "text": t, "profile": json.loads(d or "null")}
                for g, n, o, a, gone, t, d in rows}

    def item_save(self, row):
        self.db.execute("INSERT OR REPLACE INTO items (guid, name, owner, appraised, gone, text, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (row["guid"], row.get("name"), row.get("owner"), row.get("appraised"), row.get("gone"),
                         row.get("text"), json.dumps(row.get("profile"))))
        self.db.commit()

    def item_delete(self, guids):
        self.db.executemany("DELETE FROM items WHERE guid = ?", [(g,) for g in guids])
        self.db.commit()

    def close(self):
        self.db.close()
