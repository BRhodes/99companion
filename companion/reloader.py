"""The window start.cmd leaves open: runs the relay and the companion dashboard, and keeps both going.

    python -m companion.reloader [--no-relay] [companion args...]

- The companion restarts whenever its code changes (an edit, or an update), or when it asks to (exit code
  RESTART: a setting that needs a fresh start, such as the game folder).
- The relay starts with the flags launcher.local.json asks for (relay_flags), after any relay
  already running is stopped (tools/relay-stop.cmd). It restarts when a file it runs changes, when those flags
  change, and if it exits on its own. A restart drops about a second of game traffic; sessions carry on.
  A relay that someone else started meanwhile (tools/relay.cmd) is left alone until it goes away.
- --no-relay leaves the relay to tools/relay.cmd.
Closing the window stops both; game clients already started keep running.
"""

import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

PACKAGE = Path(__file__).parent
ROOT = PACKAGE.parent
WATCH_SUFFIXES = {".py", ".html", ".json"}
RELAY_FILES = {"relay.py", "proxy.py", "capture.py", "storetint.py", "netcrc.py", "framing.py", "binreader.py",
               "messages.py"}  # modules the long-lived relay process runs
RESTART = 3          # the companion's exit code for "start me again"
SETTINGS = ROOT / "launcher.local.json"   # (companion/launcher.py writes it: Settings > Game)
TAP = ("127.0.0.1", 9119)
POLL = 0.5
SETTLE = 0.4  # editors write files in several steps; wait for quiet before restarting
log = logging.getLogger("reloader")


def snapshot():
    files = {}
    for path in PACKAGE.rglob("*"):
        if path.suffix in WATCH_SUFFIXES and "__pycache__" not in path.parts:
            try:
                files[path] = path.stat().st_mtime_ns
            except OSError:
                pass
    return files


def changed(before, after):
    return sorted({p for p in before.keys() | after.keys() if before.get(p) != after.get(p)})


def start(args):
    log.info("starting companion")
    return subprocess.Popen([sys.executable, "-m", "companion", *args], env=dict(os.environ, COMPANION_SUPERVISED="1"))


def stop(proc):
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def relay_flags():
    """The relay's command-line flags launcher.local.json asks for: "tint" is --tint-store, and only with
    "experimental" on (it changes what the server sends the client; everything else is read-only)."""
    try:
        values = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        values = {}
    if not isinstance(values, dict) or values.get("experimental") is not True:
        return []
    return ["--tint-store"] if values.get("tint") is True else []


def tap_open():
    try:
        socket.create_connection(TAP, timeout=0.5).close()
        return True
    except OSError:
        return False


class Relay:
    """The relay process this window runs, and what to do when it isn't running."""

    def __init__(self):
        self.proc = None
        self.flags = None
        self.started = 0.0
        self.backoff = 3.0
        self.retry_at = 0.0
        self.outside = False        # another relay holds the ports: leave it be

    def start(self, take_over=False):
        if take_over:
            subprocess.run(["cmd", "/c", str(ROOT / "tools" / "relay-stop.cmd")], cwd=ROOT)
        self.flags = relay_flags()
        log.info("starting relay%s", " " + " ".join(self.flags) if self.flags else "")
        self.proc = subprocess.Popen([sys.executable, "-m", "companion.relay", *self.flags], cwd=ROOT)
        self.started = time.monotonic()
        self.outside = False

    def restart(self, why):
        log.info("restarting relay: %s", why)
        stop(self.proc)
        self.start()

    def tick(self):
        if self.proc is not None and self.proc.poll() is None:
            if relay_flags() != self.flags:
                self.restart("its settings changed")
            return
        now = time.monotonic()
        if self.proc is not None:              # it exited: say so once, then try again (longer if it keeps dying)
            log.warning("relay exited (code %s)", self.proc.returncode)
            self.backoff = 3.0 if now - self.started > 30 else min(self.backoff * 2, 60.0)
            self.retry_at = now + self.backoff
            self.proc = None
        if now < self.retry_at:
            return
        if tap_open():
            if not self.outside:
                log.info("another relay is running (tools\\relay.cmd?): leaving it be until it stops")
                self.outside = True
            self.retry_at = now + 5
            return
        self.start()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    args = sys.argv[1:]
    relay = None
    if "--no-relay" in args:
        args.remove("--no-relay")
    else:
        relay = Relay()
        relay.start(take_over=True)
    state = snapshot()
    proc = start(args)
    exited_reported = False
    try:
        while True:
            time.sleep(POLL)
            now = snapshot()
            diff = changed(state, now)
            if diff:
                time.sleep(SETTLE)
                now = snapshot()
                diff = changed(state, now)
                state = now
                names = [p.relative_to(PACKAGE).as_posix() for p in diff]
                log.info("changed: %s", ", ".join(names))
                if any(p.name in RELAY_FILES for p in diff):
                    if relay is not None and not relay.outside:
                        relay.restart("its code changed")
                    else:
                        log.warning("relay code changed: restart tools\\relay.cmd when convenient (it drops ~1s of game "
                                    "traffic); the dashboard is reloading now")
                stop(proc)
                proc = start(args)
                exited_reported = False
            elif proc.poll() is not None:
                if proc.returncode == RESTART:
                    proc = start(args)
                elif not exited_reported:
                    log.warning("companion exited (code %s); waiting for a code change to restart it", proc.returncode)
                    exited_reported = True
            if relay is not None:
                relay.tick()
    except KeyboardInterrupt:
        pass
    finally:
        stop(proc)
        if relay is not None:
            stop(relay.proc)


if __name__ == "__main__":
    main()
