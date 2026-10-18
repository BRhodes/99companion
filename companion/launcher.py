"""The launcher: what start.cmd's window and the dashboard's Settings > Game / Accounts set up, and starting a
game client for an account.

launcher.local.json (beside the README):
    {"game_dir": "C:\\...\\Asheron's Call",   the game folder (portal.dat, cell.dat, the client); found on its own
                                              (game_dir()) until one is picked
     "copy_appraisals": true,                 each item you appraise onto the clipboard (Settings > Items), for every
                                              character
     "experimental": false,                   experimental features: the ones that change what the game sends you
                                              (off: the relay only ever forwards traffic as it is)
     "tint": false}                           with experimental on, the relay runs with --tint-store (vendor
                                              backgrounds); start.cmd's window restarts it when either changes
accounts.local.json: the accounts (tools/aclaunch.py's format, which starts the client):
    {"1": {"account": "name", "password": "...", "exe": "C:\\...\\client-windows-3d-accel-bg.exe"}, "2": ...}
(an older "background": true starts the program's background copy, as picking it does now)
A password is written here and read by aclaunch, and never goes back out: accounts() says only whether
there is one. Launching takes an account's key and nothing else -- the client is always the one in the game
folder -- so nothing that reaches the dashboard can choose a program to run.
"""
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / "launcher.local.json"
ACCOUNTS = ROOT / "accounts.local.json"
CLIENTS = ("client-windows-3d-accel.exe", "client.exe")     # the game's program in its folder, first found (dgVoodoo's, else the plain one)
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


def copy_appraisals(values=None):
    """Whether an item you appraise goes onto the clipboard (any character). Until it's set: on if any character
    had it on, as it used to be kept per character (spellbook-layouts.json's prefs)."""
    values = settings() if values is None else values
    if "copy_appraisals" in values:
        return values["copy_appraisals"] is True
    try:
        layouts = json.loads((ROOT / "spellbook-layouts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return any(isinstance(v, dict) and v.get("copy_appraisals") is True for k, v in layouts.items() if k.startswith("prefs:"))


def save_settings(values, path=None):
    now = settings(path)
    now.update(values)
    Path(path or SETTINGS).write_text(json.dumps(now, indent=1), encoding="utf-8")
    return now


# ---- the game folder --------------------------------------------------------------------------------------------

def background_of(exe):
    """The background copy of a game program (tools/bgpatch.py makes it): client.exe -> client-bg.exe."""
    exe = Path(exe)
    return exe.with_name(exe.stem + "-bg" + exe.suffix)


def default_exe(folder):
    """The game program in a folder (CLIENTS, first there), or None."""
    return next((Path(folder) / n for n in CLIENTS if folder and (Path(folder) / n).is_file()), None)


def check_game(folder):
    """What's in a folder: {dir, portal, cell, client: its game program's name ("" if none), background: that
    program's background copy is there, ok: everything the companion needs}."""
    f = Path(folder) if folder else None
    has = lambda name: bool(f and (f / name).is_file())
    exe = default_exe(f)
    got = {"dir": str(f) if f else "", "portal": has("portal.dat"), "cell": has("cell.dat"),
           "client": exe.name if exe else "", "background": bool(exe and background_of(exe).is_file())}
    got["ok"] = got["portal"] and got["cell"] and bool(got["client"])
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


def account_exe(a, game=None):
    """The game program an account starts: its own "exe" (a path; or a name in its "dir", as older settings
    have it), else its "dir"'s or the game folder's (default_exe) -- the background copy instead when an older
    setting asks ("background": true) and it's there. None if there's none."""
    folder = a.get("dir") or game or game_dir()
    if a.get("exe"):
        exe = Path(a["exe"])
        exe = exe if exe.is_absolute() else Path(folder) / exe
    else:
        exe = default_exe(folder)
    if exe is not None and a.get("background") is True and background_of(exe).is_file():
        exe = background_of(exe)
    return exe


def accounts(path=None, running=None):
    """[{key, account, password: there is one, exe: its own game program ("": the game folder's), dir: its folder,
    starts: the program it starts, running: its client is running here}] -- never a password itself. (running: from the clients' own command lines; running=None
    reads them now.)"""
    out = []
    running = running_accounts() if running is None else running
    game = None
    for k, a in sorted(_accounts_raw(path).items(), key=lambda kv: _key_order(kv[0])):
        if a.get("account"):
            game = game or game_dir()
            exe = account_exe(a, game)
            own = bool(a.get("exe") or a.get("dir") or a.get("background"))
            out.append({"key": k, "account": str(a["account"]), "password": bool(a.get("password")),
                        "exe": str(exe) if own and exe else "",
                        "dir": str(exe.parent) if own and exe else "",       # (its folder: tools/bgpatch.py's)
                        "starts": str(exe or ""),
                        "running": str(a["account"]).lower() in running})
    return out


def check_exe(exe):
    """An account's own game program, as it's kept: an .exe beside the game's portal.dat and cell.dat ("" for
    the game folder's). ValueError for anything else -- so nothing can make the companion run another program."""
    exe = str(exe or "").strip().strip('"')
    if not exe:
        return ""
    p = Path(exe)
    if p.suffix.lower() != ".exe" or not p.is_file():
        raise ValueError("Pick the game's program (an .exe)")
    if not ((p.parent / "portal.dat").is_file() and (p.parent / "cell.dat").is_file()):
        raise ValueError("That isn't in a game folder (no portal.dat and cell.dat beside it)")
    return str(p)


def save_account(values, key=None, path=None):
    """Add an account (key None: the first free number) or change one: account, password (blank or missing:
    keep the saved one), exe (its own game program, check_exe; "" for the game folder's: either replaces an older
    folder or background setting). -> its key.
    ValueError for a missing name, a program that isn't the game's, or an unknown key."""
    raw = _accounts_raw(path)
    name = str(values.get("account") if values.get("account") is not None else (raw.get(key) or {}).get("account") or "").strip()
    if not name or len(name) > MAX_NAME or ":" in name:
        raise ValueError("an account name (no ':')")
    password = values.get("password")
    if password is not None and (not isinstance(password, str) or len(password) > MAX_PASSWORD):
        raise ValueError("a password of up to %d characters" % MAX_PASSWORD)
    exe = check_exe(values["exe"]) if isinstance(values.get("exe"), str) else None
    if key is None:
        key = next(str(n) for n in range(1, len(raw) + 2) if not (raw.get(str(n)) or {}).get("account"))
    elif key not in raw:
        raise ValueError("no such account")
    entry = dict(raw.get(key) or {}, account=name)
    if password:
        entry["password"] = password
    if exe is not None:
        entry.pop("dir", None)          # (a program picked replaces an older folder or background setting)
        entry.pop("background", None)
        if exe:
            entry["exe"] = exe
        else:
            entry.pop("exe", None)
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


# ---- which accounts have a client running on this computer ------------------------------------------------------

LOGIN = re.compile(r'(?:^|\s)-a\s+"?([^:"\s]+):')


def account_in(cmdline):
    """The account a game client was started for, from its command line (-a name:password), or None. Only
    the name is kept: the password is never read out of it."""
    m = LOGIN.search(cmdline or "")
    return m.group(1) if m else None


def _client_command_lines():
    """[(pid, command line)] of the game clients running (client*.exe), read straight from Windows."""
    if sys.platform != "win32":
        return []
    import ctypes
    import ctypes.wintypes as wt

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", wt.LONG), ("dwFlags", wt.DWORD),
                    ("szExeFile", wt.WCHAR * 260)]

    class UnicodeString(ctypes.Structure):
        _fields_ = [("Length", wt.USHORT), ("MaximumLength", wt.USHORT), ("Buffer", ctypes.c_void_p)]

    k32, ntdll = ctypes.WinDLL("kernel32"), ctypes.WinDLL("ntdll")
    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k32.OpenProcess.restype = wt.HANDLE
    k32.CloseHandle.argtypes = [wt.HANDLE]
    ntdll.NtQueryInformationProcess.argtypes = [wt.HANDLE, wt.ULONG, ctypes.c_void_p, wt.ULONG, ctypes.POINTER(wt.ULONG)]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)            # TH32CS_SNAPPROCESS
    if not snap or snap == wt.HANDLE(-1).value:
        return []
    out = []
    try:
        e = Entry()
        e.dwSize = ctypes.sizeof(Entry)
        more = k32.Process32FirstW(snap, ctypes.byref(e))
        while more:
            name = e.szExeFile.lower()
            if name.startswith("client") and name.endswith(".exe"):
                h = k32.OpenProcess(0x1000, False, e.th32ProcessID)   # PROCESS_QUERY_LIMITED_INFORMATION
                if h:
                    try:
                        buf = ctypes.create_string_buffer(8192)
                        got = wt.ULONG()
                        # ProcessCommandLineInformation (60): a UNICODE_STRING pointing into the buffer
                        if ntdll.NtQueryInformationProcess(h, 60, buf, len(buf), ctypes.byref(got)) == 0:
                            us = UnicodeString.from_buffer(buf)
                            if us.Buffer and us.Length:
                                out.append((e.th32ProcessID, ctypes.wstring_at(us.Buffer, us.Length // 2)))
                    finally:
                        k32.CloseHandle(h)
            more = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def running_accounts():
    """{account name, lower case: [pid]} for the game clients running on this computer."""
    found = {}
    try:
        lines = _client_command_lines()
    except OSError:
        return found
    for pid, line in lines:
        name = account_in(line)
        if name:
            found.setdefault(name.lower(), []).append(pid)
    return found


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


PICK_EXE = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.OpenFileDialog
$d.Title = $env:COMPANION_BROWSE_TITLE
$d.Filter = "Programs (*.exe)|*.exe"
$d.InitialDirectory = $env:COMPANION_GAME_DIR
$owner = New-Object System.Windows.Forms.Form -Property @{TopMost = $true; ShowInTaskbar = $false}
if ($d.ShowDialog($owner) -eq 'OK') { $d.FileName }
"""


async def pick_exe(start=None):
    """Windows' file picker for a game program, on top of everything, starting in `start`'s folder (else the
    game folder). -> the file picked, or None."""
    folder = str(Path(start).parent) if start else game_dir()
    env = dict(os.environ, COMPANION_GAME_DIR=folder,
               COMPANION_BROWSE_TITLE="Pick the game program this account starts (client.exe, or the one you use)")
    proc = await asyncio.create_subprocess_exec("powershell", "-NoProfile", "-STA", "-Command", PICK_EXE, env=env,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return out.decode("utf-8", "replace").strip() or None


BROWSE = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = $env:COMPANION_BROWSE_TITLE
$d.SelectedPath = $env:COMPANION_GAME_DIR
$owner = New-Object System.Windows.Forms.Form -Property @{TopMost = $true; ShowInTaskbar = $false}
if ($d.ShowDialog($owner) -eq 'OK') { $d.SelectedPath }
"""


async def browse(start=None, title="Pick your Asheron's Call folder (the one with portal.dat and cell.dat in it)"):
    """Windows' folder picker, on top of everything, starting at `start` (else the game folder). -> the folder
    picked, or None."""
    env = dict(os.environ, COMPANION_GAME_DIR=start or game_dir(), COMPANION_BROWSE_TITLE=title)
    proc = await asyncio.create_subprocess_exec("powershell", "-NoProfile", "-STA", "-Command", BROWSE, env=env,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    picked = out.decode("utf-8", "replace").strip()
    return picked or None


async def make_background_copy():
    """tools/bgpatch.py, as administrator (it writes into Program Files: Windows asks first), on the game folder's
    program and every account's own. -> False when it couldn't be started (the prompt declined)."""
    script = ("Start-Process -Verb RunAs -WindowStyle Hidden -Wait -FilePath $env:PY -ArgumentList "
              "('\"' + $env:SCRIPT + '\"')")
    env = dict(os.environ, PY=sys.executable, SCRIPT=str(ROOT / "tools" / "bgpatch.py"))
    proc = await asyncio.create_subprocess_exec("powershell", "-NoProfile", "-Command", script, env=env,
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    return await proc.wait() == 0


class Launcher:
    """What the dashboard shows (state(), read again at most every few seconds) and the game folder it uses."""

    def __init__(self):
        self._state = (0.0, None)
        self._running = (0.0, None)

    def invalidate(self):
        self._state = (0.0, None)

    def poll(self, every=2.0):
        """Look at the game clients running (at most every `every` seconds). -> True when which accounts have one
        changed: state() has been read again, for the dashboard."""
        if time.time() - self._running[0] < every:
            return False
        now = set(running_accounts())
        changed = self._running[1] is not None and now != self._running[1]
        self._running = (time.time(), now)
        if changed:
            self.invalidate()
        return changed

    def state(self):
        if time.time() - self._state[0] > 5:
            values = settings()
            game = check_game(game_dir(values))
            game["picked"] = bool(values.get("game_dir"))
            self._state = (time.time(), {"game": game, "experimental": values.get("experimental") is True,
                                         "copy_appraisals": copy_appraisals(values),
                                         "tint": values.get("tint") is True, "accounts": accounts(),
                                         "supervised": bool(os.environ.get("COMPANION_SUPERVISED"))})
        return self._state[1]
