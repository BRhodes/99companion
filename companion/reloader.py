"""Run the companion dashboard and restart it whenever its code changes.

    python -m companion.reloader [companion args...]

Only the restartable half is reloaded. The relay keeps the game connected and is never touched; if a
file the relay uses changes, this prints a reminder to restart relay.cmd at a convenient moment.
"""

import logging
import subprocess
import sys
import time
from pathlib import Path

PACKAGE = Path(__file__).parent
WATCH_SUFFIXES = {".py", ".html", ".json"}
RELAY_FILES = {"relay.py", "proxy.py", "capture.py", "storetint.py", "netcrc.py", "framing.py", "binreader.py",
               "messages.py"}  # modules the long-lived relay process runs
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
    return subprocess.Popen([sys.executable, "-m", "companion", *args])


def stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    args = sys.argv[1:]
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
                    log.warning("relay code changed: restart relay.cmd when convenient (it drops ~1s of game "
                                "traffic); the dashboard is reloading now")
                stop(proc)
                proc = start(args)
                exited_reported = False
            elif proc.poll() is not None and not exited_reported:
                log.warning("companion exited (code %s); waiting for a code change to restart it", proc.returncode)
                exited_reported = True
    except KeyboardInterrupt:
        pass
    finally:
        stop(proc)


if __name__ == "__main__":
    main()
