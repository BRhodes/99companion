"""Updates from GitHub: the companion checks the project's repository now and then and, when it's asked to (or the
"apply automatically" setting is on), brings this install up to the newest commit.

    REPO          "owner/name" of the GitHub repository (update.local.json's "repo" overrides it)
    BRANCH        the branch followed

Checking: GitHub's public API, GET /repos/<repo>/commits/<branch> -- its sha, message and date -- at start and every
CHECK_EVERY seconds (well inside the 60-an-hour limit for unauthenticated calls). This install's version: the git
checkout's HEAD when there is one, else the VERSION file an earlier update wrote.

Updating: a git checkout pulls (fast-forward only: local changes stop it rather than be touched). Otherwise the
commit's zip is downloaded and its files copied over this install -- never what's yours (KEEP: journals, the
database, caches, captures, checkpoints, the runtime\ Python, *.local.json, settings files), and nothing is deleted. The companion's
reloader then restarts it on its own, and the relay too when a file it runs changed (a relay of your own, from
relay.cmd: the dashboard says to restart it). start.cmd's window itself only runs new code once it's opened again.
"""
import io
import json
import logging
import os
import subprocess
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

REPO = "BRhodes/99companion"   # the GitHub repository updates come from
BRANCH = "main"
CHECK_EVERY = 1800.0      # seconds between checks
ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / "update.local.json"
VERSION_FILE = ROOT / "VERSION"
KEEP = ("journals/", "cache/", "captures/", "checkpoints/", ".git/", "runtime/", "runtime.partial/", "companion.sqlite3", "spellbook-layouts.json",
        "VERSION")
RELAY_FILES = {"companion/relay.py", "companion/proxy.py", "companion/capture.py", "companion/storetint.py",
               "companion/netcrc.py", "companion/framing.py", "companion/binreader.py", "companion/messages.py",
               "relay.cmd", "relay-tint.cmd", "relay-stop.cmd"}
WINDOW_FILES = {"companion/reloader.py"}   # what start.cmd's window itself runs: new only once it's opened again
log = logging.getLogger(__name__)


def kept(path):
    """True for files an update never writes: your data and settings."""
    p = path.replace("\\", "/")
    return p.endswith(".local.json") or any(p == k or p.startswith(k) for k in KEEP)


def settings(path=SETTINGS):
    try:
        got = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        got = {}
    return {"repo": str(got.get("repo") or REPO), "branch": str(got.get("branch") or BRANCH),
            "auto": got.get("auto") is True}


def save_settings(values, path=SETTINGS):
    now = settings(path)
    now.update({k: v for k, v in values.items() if k in ("repo", "branch", "auto")})
    Path(path).write_text(json.dumps(now, indent=1), encoding="utf-8")
    return now


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=120)


def current_version(root=ROOT):
    """This install's commit: the git checkout's HEAD, else the VERSION file, else None."""
    if (root / ".git").exists():
        try:
            r = _git(root, "rev-parse", "HEAD")
            if r.returncode == 0:
                return r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        return (root / "VERSION").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "99companion-updater", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def latest(repo, branch, get=_get):
    """The newest commit on the branch: {sha, message, date}."""
    d = json.loads(get(f"https://api.github.com/repos/{repo}/commits/{branch}"))
    c = d.get("commit") or {}
    return {"sha": d["sha"], "message": (c.get("message") or "").split("\n")[0][:200],
            "date": (c.get("committer") or {}).get("date")}


def unpack(blob, root):
    """Copy a GitHub zip's files over root (its single top folder stripped), but never what's kept.
    -> [the paths written, relative, with /]."""
    written = []
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        top = names[0].split("/", 1)[0] + "/" if names else ""
        for n in names:
            rel = n[len(top):] if n.startswith(top) else n
            if not rel or kept(rel) or ".." in Path(rel).parts:
                continue
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            data = z.read(n)
            try:
                if dest.read_bytes() == data:
                    continue
            except OSError:
                pass
            dest.write_bytes(data)
            written.append(rel)
    return written


class Updater:
    """Checks in a thread and applies on request; state() is what the dashboard shows."""

    def __init__(self, root=ROOT, on_change=None, get=_get):
        self.root, self.on_change, self.get = root, on_change, get
        self.info = {"latest": None, "checked": None, "error": None, "busy": False, "applied": None, "relay": False,
                     "window": False}
        self.next_check = time.time() + 20
        self.lock = threading.Lock()
        self._current = (0.0, None)          # (when read, this install's version): read again every 30 s

    def state(self):
        cfg = settings(self.root / "update.local.json")
        if time.time() - self._current[0] > 30 or self.info["busy"]:
            self._current = (time.time(), current_version(self.root))
        cur = self._current[1]
        lat = self.info["latest"]
        return dict(self.info, repo=cfg["repo"], branch=cfg["branch"], auto=cfg["auto"], current=cur,
                    supervised=bool(os.environ.get("COMPANION_SUPERVISED")),
                    git=(self.root / ".git").exists(),
                    available=bool(lat and cur != lat["sha"] and not (cur and lat["sha"].startswith(cur))))

    def _changed(self):
        if self.on_change:
            self.on_change()

    def tick(self, now=None):
        now = time.time() if now is None else now
        if now >= self.next_check and not self.info["busy"] and settings(self.root / "update.local.json")["repo"]:
            self.next_check = now + CHECK_EVERY
            threading.Thread(target=self.check, name="update-check", daemon=True).start()

    def check(self):
        cfg = settings(self.root / "update.local.json")
        if not cfg["repo"]:
            self.info["error"] = "No repository set"
            return self._changed()
        try:
            self.info.update(latest=latest(cfg["repo"], cfg["branch"], self.get), error=None, checked=time.time())
        except Exception as e:          # offline, a typo in the repo, the rate limit: say so, try again later
            self.info.update(error=f"Couldn't check: {e}", checked=time.time())
        self._changed()
        if cfg["auto"] and self.state()["available"]:
            self.apply()

    def apply(self):
        """Bring this install to the newest commit (a thread; state() follows it)."""
        with self.lock:
            if self.info["busy"]:
                return False
            self.info["busy"] = True
        threading.Thread(target=self._apply, name="update-apply", daemon=True).start()
        self._changed()
        return True

    def _apply(self):
        cfg = settings(self.root / "update.local.json")
        try:
            lat = self.info["latest"] or latest(cfg["repo"], cfg["branch"], self.get)
            if (self.root / ".git").exists():
                before = current_version(self.root)
                r = _git(self.root, "pull", "--ff-only", f"https://github.com/{cfg['repo']}.git", cfg["branch"])
                if r.returncode != 0:
                    raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout) else "git pull failed")
                d = _git(self.root, "diff", "--name-only", before, "HEAD") if before else None
                written = d.stdout.split() if d is not None and d.returncode == 0 else []
            else:
                written = unpack(self.get(f"https://codeload.github.com/{cfg['repo']}/zip/{lat['sha']}", timeout=120), self.root)
                (self.root / "VERSION").write_text(lat["sha"], encoding="utf-8")
            self.info.update(applied={"sha": lat["sha"], "files": len(written), "ts": time.time()},
                             relay=self.info["relay"] or any(w in RELAY_FILES for w in written),
                             window=self.info.get("window") or any(w in WINDOW_FILES for w in written), error=None)
            log.info("updated to %s (%d files)", lat["sha"][:7], len(written))
        except Exception as e:
            self.info["error"] = f"Update failed: {e}"
            log.warning("update failed: %s", e)
        finally:
            self.info["busy"] = False
            self._current = (0.0, None)
            self._changed()
