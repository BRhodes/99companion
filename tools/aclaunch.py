"""Launch an AC1999 client for a saved account through the relay, after removing the single-instance lock.

    python tools/aclaunch.py 1            # launch account "1" from accounts.local.json
    python tools/aclaunch.py --unlock     # only close the lock in running clients
    python tools/aclaunch.py --unlock --dry-run

The client creates a named semaphore "Empyrean Client" and refuses to start if it already exists
("There is a client already running on this machine."). Closing that handle inside the running
client(s) removes the object, so another client can start. The running client only uses it for that
check. Your own processes allow this without administrator rights, so no Sysinternals handle.exe.

Accounts live in accounts.local.json next to this repo's README (git-ignored, plain text):
    {"1": {"account": "name", "password": "..."},
     "2": {"account": "...", "password": "...", "dir": "C:\\...\\Asheron's Call2"}}
"exe" (the game program to start, a path: else the game folder's, Settings > Game) is optional per account. "background": true uses
the copy made by tools/bgpatch.py (Settings > Game, or tools/bgpatch.cmd), which keeps drawing when its window loses focus.
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import csv
import io
import json
import socket
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from companion.launcher import ACCOUNTS, account_exe, background_of, game_dir  # noqa: E402

AC_DIR = Path(game_dir())     # Settings > Game (launcher.local.json), else the folder the game's found in
LOCK_NAME = "Empyrean Client"
RELAY = ("127.0.0.1", 9110)
RELAY_TAP = ("127.0.0.1", 9119)

ntdll = ctypes.WinDLL("ntdll")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.restype = wt.HANDLE
k32.GetCurrentProcess.restype = wt.HANDLE
k32.DuplicateHandle.argtypes = [wt.HANDLE, wt.HANDLE, wt.HANDLE, ctypes.POINTER(wt.HANDLE), wt.DWORD, wt.BOOL, wt.DWORD]
k32.CloseHandle.argtypes = [wt.HANDLE]
ntdll.NtQuerySystemInformation.argtypes = [wt.ULONG, ctypes.c_void_p, wt.ULONG, ctypes.POINTER(wt.ULONG)]
ntdll.NtQueryObject.argtypes = [wt.HANDLE, wt.ULONG, ctypes.c_void_p, wt.ULONG, ctypes.POINTER(wt.ULONG)]

SYSTEM_EXTENDED_HANDLE_INFORMATION = 64
OBJECT_NAME_INFORMATION, OBJECT_TYPE_INFORMATION = 1, 2
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
PROCESS_DUP_HANDLE = 0x0040
DUPLICATE_CLOSE_SOURCE, DUPLICATE_SAME_ACCESS = 0x1, 0x2


class HandleEntry(ctypes.Structure):
    _fields_ = [("Object", ctypes.c_void_p), ("UniqueProcessId", ctypes.c_size_t), ("HandleValue", ctypes.c_size_t),
                ("GrantedAccess", wt.ULONG), ("CreatorBackTraceIndex", wt.USHORT), ("ObjectTypeIndex", wt.USHORT),
                ("HandleAttributes", wt.ULONG), ("Reserved", wt.ULONG)]


class UnicodeString(ctypes.Structure):
    _fields_ = [("Length", wt.USHORT), ("MaximumLength", wt.USHORT), ("Buffer", ctypes.c_wchar_p)]


def client_pids():
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    return {int(row[1]): row[0] for row in csv.reader(io.StringIO(out))
            if len(row) > 1 and row[0].lower().startswith("client") and row[0].lower().endswith(".exe")}


def system_handles():
    size = 1 << 20
    while True:
        buf = ctypes.create_string_buffer(size)
        needed = wt.ULONG()
        status = ntdll.NtQuerySystemInformation(SYSTEM_EXTENDED_HANDLE_INFORMATION, buf, size, ctypes.byref(needed))
        if status & 0xFFFFFFFF == STATUS_INFO_LENGTH_MISMATCH:
            size = max(size * 2, needed.value + (1 << 16))
            continue
        if status:
            raise OSError(f"NtQuerySystemInformation failed: 0x{status & 0xFFFFFFFF:08X}")
        count = ctypes.c_size_t.from_buffer(buf).value
        entries = (HandleEntry * count).from_buffer(buf, 2 * ctypes.sizeof(ctypes.c_size_t))
        return list(entries)


def _query(handle, info_class):
    buf = ctypes.create_string_buffer(0x1000)
    if ntdll.NtQueryObject(handle, info_class, buf, len(buf), None):
        return None
    return UnicodeString.from_buffer(buf).Buffer or ""


def find_locks(pids):
    """[(pid, handle value, object name)] for the lock semaphore in the given processes."""
    found = []
    me = k32.GetCurrentProcess()
    procs = {pid: k32.OpenProcess(PROCESS_DUP_HANDLE, False, pid) for pid in pids}
    try:
        for entry in system_handles():
            proc = procs.get(entry.UniqueProcessId)
            if not proc:
                continue
            dup = wt.HANDLE()
            if not k32.DuplicateHandle(proc, entry.HandleValue, me, ctypes.byref(dup), 0, False, DUPLICATE_SAME_ACCESS):
                continue
            try:
                # Only semaphores get a name query: name queries on some other types (pipes) can block.
                if _query(dup, OBJECT_TYPE_INFORMATION) == "Semaphore":
                    name = _query(dup, OBJECT_NAME_INFORMATION) or ""
                    if name.endswith("\\" + LOCK_NAME):
                        found.append((entry.UniqueProcessId, entry.HandleValue, name))
            finally:
                k32.CloseHandle(dup)
    finally:
        for proc in procs.values():
            if proc:
                k32.CloseHandle(proc)
    return found


def close_lock(pid, handle):
    proc = k32.OpenProcess(PROCESS_DUP_HANDLE, False, pid)
    if not proc:
        return False
    try:
        return bool(k32.DuplicateHandle(proc, handle, None, None, 0, False, DUPLICATE_CLOSE_SOURCE))
    finally:
        k32.CloseHandle(proc)


def unlock(dry_run=False):
    pids = client_pids()
    if not pids:
        print("No client running: nothing to unlock.")
        return True
    locks = find_locks(pids)
    if not locks:
        print(f"No '{LOCK_NAME}' lock held by the running client(s) {sorted(pids)}: already unlocked.")
        return True
    ok = True
    for pid, handle, name in locks:
        if dry_run:
            print(f"Would close {name} (handle 0x{handle:X}) in {pids[pid]} PID {pid}")
        elif close_lock(pid, handle):
            print(f"Closed the instance lock in {pids[pid]} PID {pid}")
        else:
            ok = False
            print(f"Couldn't close the lock in PID {pid} (error {ctypes.get_last_error()}). If that client runs as "
                  f"administrator, run this as administrator too, or use close-instance-lock.ps1.")
    return ok


def load_account(key):
    if not ACCOUNTS.exists():
        ACCOUNTS.write_text(json.dumps({"1": {"account": "", "password": ""}, "2": {"account": "", "password": ""}},
                                       indent=2), encoding="utf-8")
        print(f"Created {ACCOUNTS} - fill in your account names and passwords, then run this again.")
        return None
    acct = json.loads(ACCOUNTS.read_text(encoding="utf-8")).get(key) or {}
    if not acct.get("account") or not acct.get("password"):
        print(f"Account '{key}' isn't filled in yet in {ACCOUNTS}.")
        return None
    return acct


def relay_running():
    try:
        socket.create_connection(RELAY_TAP, timeout=1).close()
        return True
    except OSError:
        return False


def launch(acct):
    # The client reads its own command line to find its install, and a quoted "C:\Program Files (x86)\..."
    # first token makes it report missing critical files. So the command line starts with the same
    # relative ".\client-....exe" the launcher uses, while `executable` tells Windows which file to run.
    exe = account_exe(acct, str(AC_DIR))
    if exe is None or not exe.exists():
        print(f"No game program to start ({exe or 'none found in ' + str(AC_DIR)}): pick it in Settings > Accounts.")
        return False
    if acct.get("background"):
        if background_of(exe).exists():
            exe = background_of(exe)
        else:
            print(f"No {background_of(exe).name} yet (Settings > Game > Make the background copy); starting {exe.name}.")
    folder, exe_name = exe.parent, exe.name
    args = [".\\" + exe_name, "-h", RELAY[0], "-p", str(RELAY[1]), "-a", f"{acct['account']}:{acct['password']}", "-db"]
    subprocess.Popen(args, executable=str(exe), cwd=folder,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    print(f"Started {exe_name} from {folder} for {acct['account']} via the relay at {RELAY[0]}:{RELAY[1]}.")
    return True


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("account", nargs="?", help="key in accounts.local.json, e.g. 1 or 2")
    p.add_argument("--unlock", action="store_true", help="only close the instance lock in running clients")
    p.add_argument("--dry-run", action="store_true", help="show which locks would be closed")
    p.add_argument("--no-relay-check", action="store_true", help="launch even if the relay isn't running")
    args = p.parse_args()
    if not args.unlock and not args.account:
        p.error("give an account key (e.g. 1) or --unlock")
    if args.unlock:
        sys.exit(0 if unlock(args.dry_run) else 1)
    acct = load_account(args.account)
    if not acct:
        sys.exit(1)
    if not args.no_relay_check and not relay_running():
        print("The relay isn't running - run start.cmd first (or pass --no-relay-check).")
        sys.exit(1)
    if not unlock():
        sys.exit(1)
    if not launch(acct):
        sys.exit(1)


if __name__ == "__main__":
    main()
