"""The restartable half: decoder, trackers and dashboard, one tracker per character session.

    python -m companion [--tap host:port] [--web PORT]

Connects to the relay's tap (python -m companion.relay), which replays every active session from its
start and then streams live traffic, so this process can be stopped and restarted at any time
without affecting the game or losing state. Reconnects automatically if the relay isn't up yet.
"""

import argparse
import asyncio
import itertools
import struct
import json
import re
import logging
import os
import sys
import threading
import time
from concurrent.futures.process import BrokenProcessPool
from urllib.parse import unquote

from .capture import TapDecoder
from .dat import DatFile, icon_png, load_components, load_level_xp, load_skill_formulas, load_spells, load_vital_formulas, load_xp_tables
from . import buffpanel, walkmap, worldmap
from .routing import Router
from pathlib import Path
from .checkpoint import Checkpoints
from . import clipboard, research, routing
from .itembook import ItemBook
from .updater import Updater, save_settings as save_update_settings
from . import launcher
from .layouts import LayoutStore
from .messages import decode
from .pipeline import Pipeline
from .store import Store
from .tracker import SPELL_MODS, Tracker
from .web import WebServer

RESTART = 3            # the exit code that asks start.cmd's window (reloader.RESTART) to start the companion again
KEEP_ENDED = 900.0
STALE_AFTER = 60.0     # a duplicate session counts as stale once its client is this far behind the newest
BACKLOG = "backlog"    # the "positions" asked on connecting: its answer means the replay is through
POSITIONS_WAIT = 5.0   # a relay that hasn't answered "positions" by now is an older one: checkpoint without offsets
BOOT_ID = f"{time.time():.6f}"  # changes on every restart; open dashboards reload when they see a new one
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7}
ROUTE_BUDGET = 20.0      # seconds the worker may spend walking a route out; past that, straight lines
log = logging.getLogger("companion")
PUBLISH_GATHER = 0.03   # seconds to gather a burst of changes into one dashboard update
PUBLISH_EVERY = 0.1     # at most this often (10 a second)


def _json(status, obj):
    return status, "application/json", json.dumps(obj).encode()


def spell_catalog(spells):
    """Static spell info for the spellbook page (sent once, not with every state update)."""
    out = {}
    for sid, s in spells.items():
        words = s["name"].split()
        out[sid] = {
            "name": s["name"], "school": s.get("school_name"), "power": s.get("power"), "mana": s.get("mana"),
            "category": s.get("category"), "beneficial": bool(s.get("bitfield", 0) & 0x4),
            "level": ROMAN.get(words[-1]) if words else None,
            "target": "self" if "Self" in words else ("other" if "Other" in words else "any"),
            "target_type": research.target_type(s),     # "creature", "item", ...: what it's cast on
            "icon": f"{s['icon']:08x}" if s.get("icon") else None,
        }
    return out


def hostport(text, default_port):
    host, _, port = text.rpartition(":")
    return (host, int(port)) if host else (text, default_port)


class SessionView:
    """One character session: its tracker, decoder pipeline and journal clock."""

    def __init__(self, num, info, spells, store, components, on_change, vital_formulas=None, skill_formulas=None):
        self.num = num
        self.info = info
        self.ended = None
        self.tracker = Tracker(spells, store=store, components=components, vital_formulas=vital_formulas,
                               skill_formulas=skill_formulas)
        self.tracker.reset(info.get("server"))
        self.tracker.session = info.get("id")
        self.tracker.on_change = on_change
        self.pipeline = Pipeline(self.tracker)
        self.last_ts = None
        self.last_ts_wall = 0.0
        self.seen = 0           # records fed since the session's header (a checkpoint's position)
        self.last_character = None   # the last character in the world here
        self.last_client_ts = None   # when the client last sent anything
        self.live = False       # streamed up to now (the relay's answer after the backlog came in)
        self.skip = 0           # records already in a restored checkpoint: stream past without decoding

    def event_time(self):
        """Time on the journal's clock: replayed history runs behind the wall clock."""
        if self.last_ts is None:
            return time.time()
        return self.last_ts + (time.time() - self.last_ts_wall)

    def feed(self, ts, kind, index, payload):
        self.last_ts, self.last_ts_wall = ts, time.time()
        if kind == "c2s":
            self.last_client_ts = ts          # the client itself still talking (the server keeps sending to a dead one)
        self.seen += 1
        if self.skip:
            self.skip -= 1
            return
        if kind == "control":
            if payload.get("event") == "session_end":
                self.ended = ts
            return
        self.pipeline.feed(kind, payload, ts)

    def snapshot(self):
        snap = self.tracker.snapshot()
        snap["session"] = {"num": self.num, "id": self.info.get("id"), "client": self.info.get("client"),
                           "ended": self.ended}
        return snap


class Companion:
    # A change (dirty) pushes the dashboard an update straight away (on_dirty, set by run(): soon after, at
    # most PUBLISH_EVERY apart), not on the next half-second tick.
    _dirty = False
    on_dirty = None

    @property
    def dirty(self):
        return self._dirty

    @dirty.setter
    def dirty(self, value):
        self._dirty = value
        if value and self.on_dirty:
            self.on_dirty()

    def __init__(self, spells, store, components=None, dat=None, layouts=None, checkpoints=None):
        self.spells = spells
        self.checkpoints = checkpoints
        self.backlog_done = False      # the relay's said everything it had queued has been sent
        self.positions_asked = None    # when the relay was last asked for journal positions (no reply yet)
        self.store = store
        self.components = components
        self.dat = dat
        self.layouts = layouts
        self.vital_formulas = load_vital_formulas(dat)
        self.skill_formulas = load_skill_formulas(dat) if dat else {}
        Tracker.level_xp = load_level_xp(dat) if dat else []     # (the same for everyone: the XP bar's levels)
        Tracker.xp_tables = load_xp_tables(dat) if dat else {}   # (the raises' costs: the Experience page's goals)
        self.icons = {}
        self.router = Router()
        self.cell_dat = Path(dat_path).with_name("cell.dat") if (dat_path := getattr(dat, "path", None)) else None
        self.walk_state = None         # the walk map (routing over terrain): None, "loading", "ready" or "failed"
        self.route_pool = None         # the routing worker process (_route_pool)
        self.route_jobs = {}           # session id -> (token, future): its newest walked route
        self.route_tokens = itertools.count(1)
        self.catalog = json.dumps(spell_catalog(spells)).encode()
        self.sessions = {}
        self.items = ItemBook(store)   # every appraised item, shared by your characters (itembook.py)
        self.last_world = {}     # account -> its character's last snapshot in the world (shown while it's out of it)
        self.dirty = False
        self.relay_up = False
        self.writer = None      # tap connection, also used to send commands to the relay
        self.shoplists = {}     # session id -> (its character, the dashboard's shopping list {name: how many}), for the relay (--tint-store)
        self.updater = Updater(on_change=lambda: setattr(self, "dirty", True))   # new versions from GitHub
        self.launcher = launcher.Launcher()   # the game folder, accounts and launching (Settings > Game / Accounts)
        self.dat_path = Path(dat.path) if getattr(dat, "path", None) else None
        self.restart = False    # a new game folder: run() ends and the window starts the companion again

    # ---- dashboard API -------------------------------------------------------------------------
    def _active_view(self, session_id):
        view = next((v for v in self.sessions.values() if v.info.get("id") == session_id), None)
        return None if view is None or view.ended else view

    def route(self, method, path, body):
        parts = [unquote(p) for p in path.strip("/").split("/")]
        if method == "GET" and path == "/api/spells":
            return 200, "application/json", self.catalog
        if method == "GET" and len(parts) == 2 and parts[0] == "icon" and parts[1].endswith(".png"):
            return self._icon(parts[1][:-4])
        if len(parts) == 3 and parts[:2] == ["api", "layout"] and self.layouts:
            if method == "GET":
                return _json(200, self.layouts.get(parts[2]))
            if method == "PUT":
                try:
                    return _json(200, self.layouts.put(parts[2], json.loads(body or b"{}")))
                except ValueError:
                    return _json(400, {"error": "bad layout JSON"})
        if len(parts) == 3 and parts[:2] == ["api", "prefs"] and self.layouts:
            if method == "GET":
                return _json(200, self.layouts.get_prefs(parts[2]))
            if method == "PUT":
                try:
                    saved = self.layouts.put_prefs(parts[2], json.loads(body or b"{}"))
                    for sid, (who, _names) in self.shoplists.items():
                        if who == parts[2]:
                            self._send_shoplist(sid)         # its backgrounds may have changed
                    self.dirty = True                  # levels, profiles... show in the snapshot
                    return _json(200, saved)
                except ValueError:
                    return _json(400, {"error": "bad prefs JSON"})
        if path.startswith(("/api/launcher", "/api/accounts")):
            return self._launcher_route(method, path, body)
        if path.startswith("/api/update"):          # the updater: its state, check now, update now, its settings
            if method == "GET" and path == "/api/update":
                return _json(200, self.updater.state())
            if method == "POST" and path == "/api/update/check":
                threading.Thread(target=self.updater.check, daemon=True).start()
                return _json(200, {"ok": True})
            if method == "POST" and path == "/api/update/apply":
                return _json(200 if self.updater.apply() else 409, self.updater.state())
            if method == "PUT" and path == "/api/update/settings":
                try:
                    got = json.loads(body or b"{}")
                except ValueError:
                    return _json(400, {"error": "bad JSON"})
                repo = str(got.get("repo", "")).strip()
                if repo and not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
                    return _json(400, {"error": "the repository is owner/name"})
                save_update_settings({k: v for k, v in (("repo", repo if "repo" in got else None), ("auto", got.get("auto")))
                                      if v is not None})
                self.updater.next_check = 0
                self.dirty = True
                return _json(200, self.updater.state())
        if method == "GET" and path == "/api/items":
            return _json(200, {"items": self.items.listing(), "keep": self.items.keep})
        if method == "GET" and path == "/map.png":
            return self._map_png()
        if method == "GET" and path == "/api/places":
            return _json(200, self.router.places)
        if method == "GET" and len(parts) == 5 and parts[:2] == ["api", "sessions"] and parts[3] == "routed":
            return self._routed(parts[2], parts[4])
        if method == "GET" and len(parts) == 6 and parts[:2] == ["api", "sessions"] and parts[3] == "route":
            return self._route(parts[2], parts[4], parts[5])
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "route":
            try:
                got = json.loads(body or b"{}")
                return self._route(parts[2], got.get("ns"), got.get("ew"), skip=[str(s) for s in got.get("skip") or []][:500],
                                   start=got.get("from"))
            except (ValueError, AttributeError):
                return _json(400, {"error": "want {ns, ew, skip, from: {ns, ew} (optional)}"})
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "shoplist":
            # the components the dashboard's buy list has: in a vendor's window the relay shows them, and the rest, on
            # the backgrounds picked on the Order tab (--tint-store)
            try:
                got = json.loads(body or b"{}").get("amounts") or {}
                amounts = {str(n): int(a) for n, a in list(got.items())[:500] if int(a) > 0}
            except (ValueError, AttributeError, TypeError):
                return _json(400, {"error": "want {amounts: {name: how many}}"})
            view = self._active_view(parts[2])
            self.shoplists[parts[2]] = (view.tracker.character if view else None, amounts)
            self._send_shoplist(parts[2])
            return _json(200, {"ok": True})
        if method == "GET" and len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "research":
            view = self._active_view(parts[2])
            if view is None:
                return _json(404, {"error": "no such active session"})
            return _json(200, view.tracker.research())
        if method == "GET" and len(parts) in (4, 5) and parts[:2] == ["api", "sessions"] and parts[3] == "buffchoices":
            view = self._active_view(parts[2])      # .../buffchoices/other: for a friend (the Other spells)
            if view is None:
                return _json(404, {"error": "no such active session"})
            return _json(200, buffpanel.choices(view.tracker, SPELL_MODS, other=parts[4:] == ["other"]))
        if method == "GET" and len(parts) in (3, 4, 5) and parts[:2] == ["api", "chat"]:
            # /api/chat/<character>[/<before ts>[/<kind,kind...>]]: earlier chat, from every login
            try:
                before = float(parts[3]) if len(parts) >= 4 else None
            except ValueError:
                return _json(400, {"error": "bad time"})
            kinds = [k for k in parts[4].split(",") if k] if len(parts) == 5 else None
            return _json(200, self.store.chat(parts[2], before, kinds=kinds))
        return None

    def _send_shoplist(self, session_id):
        """The shopping list to the relay, with the backgrounds its character's picked."""
        who, amounts = self.shoplists[session_id]
        got = ((self.layouts.get_prefs(who) if self.layouts and who else None) or {}).get("restock") or {}
        if self.writer is not None and not self.writer.is_closing():
            self.writer.write(json.dumps({"cmd": "shoplist", "session": session_id, "amounts": amounts,
                                          "buy": got.get("tint_buy"), "other": got.get("tint_other")}).encode() + b"\n")

    def _launcher_route(self, method, path, body):
        """Settings > Game and Accounts, and the header's launch buttons (launcher.py)."""
        try:
            values = json.loads(body or b"{}")
        except ValueError:
            return _json(400, {"error": "bad JSON"})
        if not isinstance(values, dict):
            return _json(400, {"error": "bad JSON"})
        parts = path.strip("/").split("/")
        changed = lambda: (self.launcher.invalidate(), setattr(self, "dirty", True))
        if method == "GET" and path == "/api/launcher":
            return _json(200, self.launcher.state())
        if method == "PUT" and path == "/api/launcher/game":
            return self._use_game(values.get("dir"))
        if method == "POST" and path == "/api/launcher/game/find":
            found = launcher.find_game()
            if not found:
                return _json(404, {"error": "The game wasn't found in the usual places: pick its folder"})
            return self._use_game(found, picked=False)
        if method == "POST" and path == "/api/launcher/game/browse":
            async def browse():
                picked = await launcher.browse()
                return self._use_game(picked) if picked else _json(200, {"cancelled": True})
            return browse()
        if method == "POST" and path == "/api/launcher/pick-folder":      # (an account's own folder: just picked, not kept)
            async def pick():
                start = values.get("start") if isinstance(values.get("start"), str) else None
                picked = await launcher.browse(start, "Pick the Asheron's Call folder this account starts from (the one "
                                                      "with client-windows-3d-accel.exe in it)")
                if not picked:
                    return _json(200, {"cancelled": True})
                try:
                    return _json(200, {"dir": launcher.check_account_dir(picked)})
                except ValueError as e:
                    return _json(400, {"error": str(e)})
            return pick()
        if method == "POST" and path == "/api/launcher/game/background":
            async def background():
                started = await launcher.make_background_copy()
                changed()
                made = launcher.check_game(launcher.game_dir())["background"]
                return _json(200 if made else 409, {"made": made} if made else
                             {"error": "The background copy wasn't made" + ("" if started else " (Windows' prompt was declined)")})
            return background()
        if method == "PUT" and path == "/api/launcher/settings":
            for k in ("tint", "experimental"):
                if k in values:
                    launcher.save_settings({k: values[k] is True})
            changed()
            return _json(200, self.launcher.state())
        if parts[:2] == ["api", "accounts"]:
            key = parts[2] if len(parts) > 2 else None
            try:
                if method == "POST" and len(parts) == 2:
                    key = launcher.save_account(values)
                elif method == "PUT" and len(parts) == 3:
                    launcher.save_account(values, key)
                elif method == "DELETE" and len(parts) == 3:
                    launcher.delete_account(key)
                elif method == "POST" and len(parts) == 4 and parts[3] == "launch":
                    async def launch():
                        ok, said = await launcher.launch(key)
                        return _json(200 if ok else 409, {"ok": ok, "message": said})
                    return launch()
                else:
                    return None
            except ValueError as e:
                return _json(400, {"error": str(e)})
            changed()
            return _json(200, {"key": key, "accounts": launcher.accounts()})
        return None

    def _use_game(self, folder, picked=True):
        """Use this game folder (picked: remembered; else found, and found again next time). A new one restarts
        the companion so it reads that folder's portal.dat and cell.dat."""
        got = launcher.check_game(folder) if isinstance(folder, str) and folder.strip() else None
        if not got or not (got["portal"] and got["cell"]):
            return _json(400, {"error": "That folder doesn't have portal.dat and cell.dat in it", "game": got})
        launcher.save_settings({"game_dir": got["dir"] if picked else None})
        self.launcher.invalidate()
        self.dirty = True
        restart = Path(got["dir"]) / "portal.dat" != self.dat_path
        if restart and os.environ.get("COMPANION_SUPERVISED"):
            self.restart = True
        return _json(200, {"game": got, "restart": restart})

    def _map_png(self):
        if not self.cell_dat or not self.cell_dat.exists():
            return _json(404, {"error": "cell.dat not found next to portal.dat"})
        png = worldmap.map_png(self.cell_dat, Path(__file__).resolve().parent.parent / "cache" / "worldmap.png")
        return 200, "image/png", png

    def _load_walk(self):
        """Build (or read back) the walk map off the event loop; routes run straight lines until it's in."""
        if self.walk_state is not None or not self.cell_dat or not self.cell_dat.exists() or not self.dat:
            return
        self.walk_state = "loading"

        def load():
            try:
                self.router.walk = walkmap.load(self.cell_dat, self.dat, Path(__file__).resolve().parent.parent / "cache")
                self.walk_state = "ready"
                log.info("walk map ready")
            except Exception:
                log.exception("walk map failed")
                self.walk_state = "failed"
        threading.Thread(target=load, name="walkmap", daemon=True).start()

    async def _route(self, session_id, ns, ew, skip=(), start=None):
        """GET /api/sessions/<id>/route/<ns>/<ew> (or POST .../route {ns, ew, skip: [portal ids], from: {ns, ew}}):
        to that map coordinate (decimal, north and east positive), from where the character is, or from
        the `from` spot (outdoors there)."""
        view = self._active_view(session_id)
        if view is None:
            return _json(404, {"error": "no such active session"})
        try:
            dest = (float(ns), float(ew))
        except (TypeError, ValueError):
            return _json(400, {"error": "bad coordinates"})
        if start is not None:
            try:
                pos = routing.outdoor_position(float(start["ns"]), float(start["ew"]))
            except (TypeError, ValueError, KeyError):
                return _json(400, {"error": "bad starting point"})
            if pos is None:
                return _json(400, {"error": "that starting point is off the map"})
        else:
            pos = view.tracker.position
        if not pos:
            return _json(409, {"error": "your position isn't known yet: move a step in game"})
        self._load_walk()
        level = view.tracker.level
        # this character's pace: a map unit (240 m) at their run speed (Run, burden: Tracker.run_speed)
        speed = view.tracker.run_speed()
        run_seconds = 240.0 / speed if speed and speed > 0.5 else routing.RUN_SECONDS
        pool = self._route_pool()
        if pool is None:            # no worker (no game data): walked here, off the event loop
            result = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self.router.route(pos, dest, level=level, skip=skip, run_seconds=run_seconds))
            if result:
                result["terrain_state"] = self.walk_state
            return _json(200, result) if result else _json(404, {"error": "no route found"})
        # the straight-line route now (a fraction of a second), the walked one from the worker after
        estimate = (self.router.route(pos, dest, level=level, skip=skip, walk=False, run_seconds=run_seconds)
                    # no straight lines around the sea: the plain ones meanwhile; the walk goes around
                    or self.router.route(pos, dest, level=level, skip=skip, walk=False, check=False, run_seconds=run_seconds))
        if not estimate:
            return _json(404, {"error": "no route found"})
        old = self.route_jobs.get(session_id)
        if old:
            old[1].cancel()         # still queued behind another: never started (the newest one wins)
        token = f"{session_id}:{next(self.route_tokens)}"
        future = pool.submit(routing.worker_route, dict(pos), dest, level, list(skip), ROUTE_BUDGET, run_seconds)
        self.route_jobs[session_id] = (token, future)
        estimate.update(estimate=True, refine=token, terrain_state=self.walk_state, run_speed=speed)
        return _json(200, estimate)

    def _route_pool(self):
        """The routing worker process (started on first use), or None without the game data."""
        if self.route_pool is None and self.dat is not None and getattr(self.dat, "path", None):
            from concurrent.futures import ProcessPoolExecutor
            self.route_pool = ProcessPoolExecutor(
                max_workers=1, initializer=routing.worker_init,
                initargs=(str(self.dat.path), str(Path(__file__).resolve().parent.parent / "cache"), os.getpid()))
        return self.route_pool

    def _routed(self, session_id, token):
        """GET /api/sessions/<id>/routed/<token>: the walked route for a request, once it's done."""
        job = self.route_jobs.get(session_id)
        if not job or job[0] != token:
            return _json(410, {"error": "a newer route replaced this one"})
        future = job[1]
        if not future.done():
            return _json(202, {"pending": True})
        try:
            result = future.result()
        except Exception as e:      # the worker died, or the job was cancelled: the estimate stands
            log.warning("route worker: %s", e)
            self.route_pool = None if isinstance(e, BrokenProcessPool) else self.route_pool
            return _json(500, {"error": "couldn't walk the route out: the estimate stands"})
        if not result:
            return _json(404, {"error": "no walkable route found"})
        result["terrain_state"] = self.walk_state
        return _json(200, result)

    def _icon(self, hex_id):
        try:
            icon_id = int(hex_id, 16)
        except ValueError:
            return None
        if icon_id not in self.icons:
            self.icons[icon_id] = icon_png(self.dat, icon_id) if self.dat and icon_id >> 24 == 0x06 else None
        png = self.icons[icon_id]
        return (200, "image/png", png) if png else None

    def _mark(self):
        self.dirty = True

    def clear(self):
        self.sessions.clear()
        self.dirty = True

    def handle(self, num, ts, kind, index, payload):
        if kind == "control" and payload.get("event") == "positions":
            if payload.get("id") == BACKLOG:     # everything that was waiting has been streamed: live now
                self.backlog_done = True
                for view in self.sessions.values():
                    view.live = True
                self.dirty = True
                return
            self._save_checkpoints(payload.get("sessions") or {})
            return
        if kind == "control" and payload.get("event") == "resumed":
            view = self.sessions.get(num)     # the relay started this session at its checkpoint: no skipping
            if view is not None:
                view.skip, view.seen = 0, getattr(view, "resume_seen", 0)
            return
        if kind == "control" and payload.get("event") == "session":
            log.info("session %d: %s from %s", num, payload.get("id"), payload.get("client"))
            self.sessions[num] = SessionView(num, payload, self.spells, self.store, self.components, self._mark,
                                             self.vital_formulas, self.skill_formulas)
            view = self.sessions[num]
            view.live = self.backlog_done      # a session that starts once the backlog's through is live from its first record
            # this character's dashboard settings (the component order...), read fresh each time
            view.prefs = lambda v=view: self.layouts.get_prefs(v.tracker.character) if self.layouts and v.tracker.character else None
            view.tracker.prefs = view.prefs
            # an item's burden from its appraisal in the shared item book (the 1999 item description has none)
            view.tracker.book_burden = lambda g: ((self.items.rows.get(g) or {}).get("profile") or {}).get("burden")
            if self.store:   # the last component vendor used, kept per character: the shop's prices on the next login
                view.tracker.remember_vendor = self.store.save_comp_vendor
                view.tracker.recall_vendor = self.store.comp_vendor
                view.tracker.remember_corpse = self.store.save_corpse      # your corpses, till they rot
                view.tracker.recall_corpses = self.store.corpses
            if self.checkpoints:
                self.checkpoints.restore(self.sessions[num])
            self.dirty = True
            return
        view = self.sessions.get(num)
        if view is None:  # joined mid-record stream without the session header; can't happen on a fresh tap
            return
        view.feed(ts, kind, index, payload)
        if kind == "control":
            self.dirty = True

    def _end_stale_duplicates(self):
        """A character in two live sessions (an old link a relay restart left behind): only the one its client
        spoke on last is live; the others are ended (and drop off after KEEP_ENDED)."""
        if not self.backlog_done:                       # mid-replay, who spoke last isn't known yet
            return
        live = {}
        for view in self.sessions.values():
            who = view.tracker.character
            if who and not view.ended:
                live.setdefault(who, []).append(view)
        for views in live.values():
            if len(views) < 2:
                continue
            views.sort(key=lambda v: v.last_client_ts or 0)
            newest = views[-1].last_client_ts
            if newest is None:                          # not heard from any client yet (just restarted): wait
                continue
            for old in views[:-1]:
                if old.last_client_ts is None or newest - old.last_client_ts < STALE_AFTER:
                    continue                            # not clearly behind
                old.ended = old.last_ts or time.time()
                self.dirty = True

    def _copy_appraisal(self, view):
        """An item you've just appraised, onto the clipboard as up to three lines without its name (appraisal.clip_lines:
        when the character has it turned on, prefs copy_appraisals): each replaces the last, out of clipboard history.
        Never one from a replay. (The dashboard shows and copies the one line, name and all.)"""
        d = view.tracker.described
        if d is getattr(view, "copied", None):
            return
        view.copied = d
        prefs = view.prefs() if callable(getattr(view, "prefs", None)) else None
        text = "\r\n".join(d.get("clip") or []) if d else ""
        if text and getattr(view, "live", False) and (prefs or {}).get("copy_appraisals"):
            if clipboard.copy(text):
                log.info("copied the appraisal of %s", d["name"])

    def _track_items(self, view, now):
        """The shared item book (itembook.py): an item this character's just appraised goes in it (theirs if they hold
        it), and while they play, what they hold is theirs and what leaves them (sold, dropped, handed over) is gone
        from then, kept a while. Ownership only from live play, never a replay; a logout leaves nothing gone."""
        tr = view.tracker
        d = tr.described
        if d is not getattr(view, "booked", None):
            view.booked = d
            if d:
                holder = tr.character if d["object"] in tr.held() else None
                self.items.appraised(d["object"], d["name"], d["text"], d.get("profile"), d["ts"], holder)
        who = tr.character if getattr(view, "live", False) and tr.in_world else None
        if not who or getattr(view, "held_by", None) != who:
            view.held_by, view.held_now = who, None
        if not who:
            return
        held = set(tr.held())
        self.items.held(who, held)
        if view.held_now is not None:
            self.items.left(who, view.held_now - held, now)
        view.held_now = held

    def _held_items(self, view):
        """What this character holds, each with its appraisal from the item book (the Items page)."""
        tr = view.tracker
        out = []
        for guid, where in tr.held().items():
            o, row = tr.objects.get(guid, {}), self.items.rows.get(guid) or {}
            out.append({"id": guid, "name": o.get("name", "?"), "stack": o.get("stack") or 1, "where": where,
                        "text": row.get("text"), "appraised": row.get("appraised")})
        return out

    def _superseded(self, view):
        """An ended session with a newer one on its account (or no account at all): the one to drop. An account's
        newest stays, ended or not, so its tab keeps its character."""
        account = view.tracker.account
        return not account or any(v is not view and v.tracker.account == account and v.num > view.num
                                  for v in self.sessions.values())

    def tick(self):
        now = time.time()
        self.updater.tick(now)
        if self.launcher.poll():            # a game client started or closed: the header's play buttons
            self.dirty = True
        self._end_stale_duplicates()
        for num, view in list(self.sessions.items()):
            if view.skip:
                continue          # still streaming past its checkpoint
            view.tracker.tick(view.event_time())
            view.last_character = view.tracker.character or view.last_character
            self._copy_appraisal(view)
            self._track_items(view, now)
            if view.ended and now - view.event_time() > KEEP_ENDED and self._superseded(view):
                del self.sessions[num]
                if self.checkpoints:
                    self.checkpoints.forget(view.info.get("id"))
                self.dirty = True
        self.items.prune(now)
        self._checkpoint_tick(now)

    # ---- checkpoints ---------------------------------------------------------------------------------
    def _due_checkpoints(self, now):
        return [v for v in self.sessions.values() if not v.skip and not v.ended and v.last_ts
                and now - v.last_ts < 10 and self.checkpoints.due(v, now)]

    def _checkpoint_tick(self, now):
        """Ask the relay where the journals end; the reply ("positions") saves the checkpoints. A relay
        that never answers (an older one) gets checkpoints without an offset instead."""
        if not self.checkpoints or not self.writer or not self._due_checkpoints(now):
            return
        if self.positions_asked is None:
            self.positions_asked = now
            self.writer.write(json.dumps({"cmd": "positions", "id": "ckpt"}).encode() + b"\n")
        elif now - self.positions_asked > POSITIONS_WAIT:
            self.positions_asked = None
            for view in self._due_checkpoints(now):
                self._save(view, None)

    def _save_checkpoints(self, sessions):
        self.positions_asked = None
        for num, info in sessions.items():
            view = self.sessions.get(int(num))
            if view is not None and not view.skip and not view.ended and view.info.get("id") == info.get("id"):
                self._save(view, info.get("offset"))

    def _save(self, view, offset):
        try:
            self.checkpoints.save(view, offset)
        except Exception as e:   # never let a checkpoint break the companion
            log.warning("checkpoint for %s failed: %s", view.info.get("id"), e)

    def snapshot(self):
        sessions = []
        for n in sorted(self.sessions):
            snap = self.sessions[n].snapshot()
            snap["items"] = self._held_items(self.sessions[n])
            sessions.append(self._last_seen(snap))
        # your other characters in the world, as cast targets (helpful spells can go on them)
        chars = [(v.tracker.player, v.tracker.character) for v in self.sessions.values()
                 if not v.ended and v.tracker.in_world and v.tracker.player]
        for s in sessions:
            s["characters"] = [{"id": guid, "name": name} for guid, name in chars if name != s["status"].get("character")]
        return {"now": time.time(), "boot": BOOT_ID, "relay": self.relay_up, "sessions": sessions,
                "update": self.updater.state(), "launcher": self.launcher.state()}

    def _last_seen(self, snap):
        """A session with its character in the world: remembered for its account. One without (character select,
        or the client gone): its account's character as it was last seen in the world, marked logged out
        (status.logged_out, the time in status.last_seen), with this session's own chat and links."""
        st = snap["status"]
        account = st.get("account")
        if st.get("in_world") and st.get("character") and not snap["session"].get("ended"):
            if account:
                self.last_world[account] = snap
            return snap
        last = self.last_world.get(account) if account else None
        if last is None:
            return snap
        out = dict(last)
        out["status"] = dict(last["status"], in_world=False, logged_out=True, last_seen=last.get("now"),
                             connected=st.get("connected"))
        for key in ("chat", "session", "busy", "vendor", "vendor_closed"):
            if key in snap:
                out[key] = snap[key]
        return out

    async def follow(self, host, port):
        warned = False
        while True:
            try:
                reader, writer = await asyncio.open_connection(host, port)
            except OSError:
                if not warned:
                    log.info("waiting for the relay at %s:%d (start it with: python -m companion.relay)", host, port)
                    warned = True
                await asyncio.sleep(2)
                continue
            warned = False
            self.relay_up = True
            self.writer = writer
            self.positions_asked = None
            if self.checkpoints:   # where each checkpointed session can resume (an older relay ignores this)
                writer.write(json.dumps({"cmd": "resume", "from": self.checkpoints.offsets()}).encode() + b"\n")
            # the relay answers this after everything it had queued: the moment the backlog is through
            self.backlog_done = False
            writer.write(json.dumps({"cmd": "positions", "id": BACKLOG}).encode() + b"\n")
            for sid in self.shoplists:                   # a restarted relay has none
                self._send_shoplist(sid)
            log.info("connected to relay; replaying sessions")
            self.clear()  # rebuilt from each session's start; the relay replays everything again
            decoder = TapDecoder()
            try:
                while True:
                    chunk = await reader.read(256 * 1024)
                    if not chunk:
                        break
                    for rec in decoder.feed(chunk):
                        self.handle(*rec)
            except (ConnectionError, OSError):
                pass
            finally:
                writer.close()
                self.writer = None
            self.relay_up = False
            self.dirty = True
            log.info("relay connection closed; reconnecting")
            await asyncio.sleep(1)


async def run(args):
    args.dat = args.dat or str(Path(launcher.game_dir()) / "portal.dat")
    try:
        dat = DatFile(args.dat)
        spells, components = load_spells(dat), load_components(dat)
        log.info("loaded %d spells and %d components from %s", len(spells), len(components), args.dat)
    except OSError as e:
        dat, spells, components = None, {}, {}
        log.warning("no spell data (could not read %s: %s)", args.dat, e)

    store = Store(args.db)
    app = Companion(spells, store, components, dat=dat, layouts=LayoutStore(args.layouts),
                    checkpoints=Checkpoints(args.checkpoints))
    app._load_walk()                  # the map's terrain routing, built in the background
    # both loopbacks: Windows tries "localhost" as IPv6 (::1) first, and only on IPv4 every request waited
    # out that attempt (0.2-2 s a click)
    web = WebServer(["127.0.0.1", "::1"], args.web, app.snapshot)
    web.router = app.route
    await web.start()
    tap_host, tap_port = hostport(args.tap, 9119)
    follower = asyncio.create_task(app.follow(tap_host, tap_port))
    loop = asyncio.get_running_loop()
    push = {"handle": None, "last": 0.0}

    def publish_now():
        push["handle"] = None
        if app.dirty:
            app.dirty = False
            push["last"] = loop.time()
            web.publish()

    def publish_soon():                 # bundle a burst of changes; no closer together than PUBLISH_EVERY
        if push["handle"] is None:
            wait = max(PUBLISH_GATHER, push["last"] + PUBLISH_EVERY - loop.time())
            push["handle"] = loop.call_later(wait, publish_now)
    app.on_dirty = publish_soon
    try:
        while not app.restart:
            await asyncio.sleep(0.5)
            app.tick()
            if app.dirty and push["handle"] is None:     # (the timer's a safety net; changes push themselves)
                publish_now()
    finally:
        follower.cancel()
        web.close()
        store.close()
    return app.restart


def main():
    p = argparse.ArgumentParser(prog="companion", description="AC1999 companion dashboard (reads the relay tap)")
    p.add_argument("--tap", default="127.0.0.1:9119", help="relay tap address (default %(default)s)")
    p.add_argument("--web", type=int, default=8099, help="dashboard port (default %(default)s)")
    p.add_argument("--dat", help="portal.dat for spell data (default: the game folder, Settings > Game)")
    p.add_argument("--db", default="companion.sqlite3", help="event history database (default %(default)s)")
    p.add_argument("--checkpoints", default="checkpoints",
                   help="session checkpoints, for quick restarts (default %(default)s)")
    p.add_argument("--layouts", default="spellbook-layouts.json",
                   help="per-character spellbook layouts (default %(default)s)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        if asyncio.run(run(args)):
            sys.exit(RESTART)       # start.cmd's window starts it again (reloader)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
