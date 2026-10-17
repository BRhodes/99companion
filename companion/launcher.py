"""The launcher: what start.cmd's window and the dashboard's Settings > Game / Accounts set up, and starting a
game client for an account.

launcher.local.json (beside the README):
    {"game_dir": "C:\\...\\Asheron's Call",   the game folder (portal.dat, cell.dat, the client); found on its own
                                              (game_dir()) until one is picked
     "tint": false}                           the relay runs with --tint-store (vendor backgrounds); start.cmd's
                                              window restarts it when this changes
accounts.local.json: the accounts (tools/aclaunch.py's format, which starts the client):
    {"1": {"account": "name", "password": "...", "background": true, ...}, "2": ...}
A password is written here and read by aclaunch, and never goes back out: accounts() says only whether
there is one. Launching takes an account's key and nothing else -- the client is always the one in the game
folder -- so nothing that reaches the dashboard can choose a program to run.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / "launcher.local.json"
ACCOUNTS = ROOT / "accounts.local.json"
CLIENT_EXE = "client-windows-3d-accel.exe"
BACKGROUND_EXE = "client-windows-3d-accel-bg.exe"
DEFAULT_GAME_DIR = r"C:\Program Files (x86)\Microsoft Games\Asheron's Call"
CANDIDATES = (DEFAULT_GAME_DIR, r"C:\Program Files\Microsoft Games\Asheron's Call", r"C:\Turbine\Asheron's Call",
              r"C:\Games\Asheron's Call", r"C:\AC\Asheron's Call", r"C:\Asheron's Call")
REGISTRY = (r"SOFTWARE\WOW6432Node\Microsoft\Microsoft Games\Asheron's Call\1.00",
            r"SOFTWARE\Microsoft\Microsoft Games\Asheron's Call\1.00")
MAX_NAME, MAX_PASSWORD = 64, 128


def settings(path=None):
    try:
        got = json.loads(Path(path or SETTINGS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        got = {}
    return got if isinstance(got, dict) else {}


def save_settings(values, path=None):
    now = settings(path)
    now.update(values)
    Path(path or SETTINGS).write_text(json.dumps(now, indent=1), encoding="utf-8")
    return now


# ---- the game folder --------------------------------------------------------------------------------------------

def check_game(folder):
    """What's in a folder: {dir, portal, cell, client, background, ok} (ok: everything the companion needs)."""
    f = Path(folder) if folder else None
    has = lambda name: bool(f and (f / name).is_file())
    got = {"dir": str(f) if f else "", "portal": has("portal.dat"), "cell": has("cell.dat"),
           "client": has(CLIENT_EXE), "background": has(BACKGROUND_EXE)}
    got["ok"] = got["portal"] and got["cell"] and got["client"]
    return got


def registry_dirs():
    """The install folders the game's setup wrote to the registry."""
    try:
        import winreg
    except ImportError:
        return []
    found = []
    for key in REGISTRY:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                for name in ("InstallationDirectory", "Portal Dat", "path"):
                    try:
                        found.append(str(winreg.QueryValueEx(k, name)[0]))
                    except OSError:
                        pass
        except OSError:
            pass
    return found


def find_game():
    """The first folder with the game in it: the registry's, then the usual places. None if there's none."""
    for d in [*registry_dirs(), *CANDIDATES]:
        if check_game(d)["ok"]:
            return d
    return None


def game_dir(values=None):
    """The game folder: the one picked in Settings > Game, else the one found, else the usual place."""
    values = settings() if values is None else values
    return values.get("game_dir") or find_game() or DEFAULT_GAME_DIR


# ---- accounts ---------------------------------------------------------------------------------------------------

def _accounts_raw(path=None):
    try:
        got = json.loads(Path(path or ACCOUNTS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in got.items() if isinstance(v, dict)} if isinstance(got, dict) else {}


def _key_order(k):
    return (0, int(k)) if k.isdigit() else (1, k)


def accounts(path=None):
    """[{key, account, password: there is one, background, dir}] -- never a password itself."""
    out = []
    for k, a in sorted(_accounts_raw(path).items(), key=lambda kv: _key_order(kv[0])):
        if a.get("account"):
            out.append({"key": k, "account": str(a["account"]), "password": bool(a.get("password")),
                        "background": a.get("background") is True, "dir": str(a.get("dir") or "")})
    return out


def save_account(values, key=None, path=None):
    """Add an account (key None: the first free number) or change one: account, password (blank or missing:
    keep the saved one), background. Anything else an account has (dir, exe) is kept. -> its key.
    ValueError for a missing name or an unknown key."""
    raw = _accounts_raw(path)
    name = str(values.get("account") if values.get("account") is not None else (raw.get(key) or {}).get("account") or "").strip()
    if not name or len(name) > MAX_NAME or ":" in name:
        raise ValueError("an account name (no ':')")
    password = values.get("password")
    if password is not None and (not isinstance(password, str) or len(password) > MAX_PASSWORD):
        raise ValueError("a password of up to %d characters" % MAX_PASSWORD)
    if key is None:
        key = next(str(n) for n in range(1, len(raw) + 2) if not (raw.get(str(n)) or {}).get("account"))
    elif key not in raw:
        raise ValueError("no such account")
    entry = dict(raw.get(key) or {}, account=name)
    if password:
        entry["password"] = password
    if "background" in values:
        entry["background"] = values["background"] is True
    raw[key] = entry
    _write_accounts(raw, path)
    return key


def delete_account(key, path=None):
    raw = _accounts_raw(path)
    if key not in raw:
        raise ValueError("no such account")
    del raw[key]
    _write_accounts(raw, path)


def _write_accounts(raw, path=None):
    p = Path(path or ACCOUNTS)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(dict(sorted(raw.items(), key=lambda kv: _key_order(kv[0]))), indent=2), encoding="utf-8")
    os.replace(tmp, p)


# ---- doing things (off the event loop) -------------------------------------------------------------------------

async def _run(*argv, timeout=60):
    proc = await asyncio.create_subprocess_exec(*argv, cwd=ROOT, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, "timed out"
    return proc.returncode, out.decode("utf-8", "replace").strip()


async def launch(key):
    """Start the client for an account (tools/aclaunch.py: the instance lock, then the client through the relay).
    -> (ok, what it said)."""
    if key not in {a["key"] for a in accounts()}:
        return False, "no such account"
    code, said = await _run(sys.executable, str(ROOT / "tools" / "aclaunch.py"), key)
    return code == 0, said.splitlines()[-1] if said else ("started" if code == 0 else "failed")


BROWSE = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = "Pick your Asheron's Call folder (the one with portal.dat and cell.dat in it)"
$d.SelectedPath = $env:COMPANION_GAME_DIR
$owner = New-Object System.Windows.Forms.Form -Property @{TopMost = $true; ShowInTaskbar = $false}
if ($d.ShowDialog($owner) -eq 'OK') { $d.SelectedPath }
"""


async def browse():
    """Windows' folder picker, on top of everything. -> the folder picked, or None."""
    env = dict(os.environ, COMPANION_GAME_DIR=game_dir())
    proc = await asyncio.create_subprocess_exec("powershell", "-NoProfile", "-STA", "-Command", BROWSE, env=env,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    picked = out.decode("utf-8", "replace").strip()
    return picked or None


async def make_background_copy(folder):
    """tools/bgpatch.py on the game folder, as administrator (it writes into Program Files): Windows asks first.
    -> False when it couldn't be started (the prompt declined)."""
    script = ("Start-Process -Verb RunAs -WindowStyle Hidden -Wait -FilePath $env:PY -ArgumentList "
              "('\"' + $env:SCRIPT + '\"'), ('\"' + $env:GAME + '\"')")
    env = dict(os.environ, PY=sys.executable, SCRIPT=str(ROOT / "tools" / "bgpatch.py"), GAME=str(folder))
    proc = await asyncio.create_subprocess_exec("powershell", "-NoProfile", "-Command", script, env=env,
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    return await proc.wait() == 0


class Launcher:
    """What the dashboard shows (state(), read again at most every few seconds) and the game folder it uses."""

    def __init__(self):
        self._state = (0.0, None)

    def invalidate(self):
        self._state = (0.0, None)

    def state(self):
        if time.time() - self._state[0] > 5:
            values = settings()
            game = check_game(game_dir(values))
            game["picked"] = bool(values.get("game_dir"))
            self._state = (time.time(), {"game": game, "tint": values.get("tint") is True, "accounts": accounts(),
                                         "supervised": bool(os.environ.get("COMPANION_SUPERVISED"))})
        return self._state[1]
