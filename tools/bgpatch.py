"""Make a copy of the AC1999 client that keeps drawing when its window loses focus.

    python tools/bgpatch.py                         # the game folder (Settings > Game) and accounts' own folders
    python tools/bgpatch.py "C:\\...\\Asheron's Call2"

The client's main loop runs networking and physics every frame but only draws while its "app active"
flag (0x62fdd8) is set. WM_ACTIVATE with WA_INACTIVE queues a deactivate (state 1 in 0x62fdfc), which
the message pump turns into a call that clears the flag, so an unfocused window stops drawing. The
patch removes that one store (mov dword [0x62fdfc], 1 at 0x40859a), so losing focus no longer
deactivates the game. Everything else is unchanged:
  * WM_KILLFOCUS still releases held keys and mouse buttons (no stuck movement keys);
  * minimizing still pauses, as before;
  * keyboard input still goes only to the focused window (Windows' rule, not the client's).

The original exe is never modified; the patched copy is written next to it. Point an account at it
with "exe" in accounts.local.json. The bytes are checked first, so a different client build is
refused rather than corrupted.
"""

import struct
import sys
from pathlib import Path

SOURCE = "client-windows-3d-accel.exe"
TARGET = "client-windows-3d-accel-bg.exe"
PATCH_VA = 0x0040859A
ORIGINAL = bytes.fromhex("c705fcfd620001000000")   # mov dword ptr [0x62fdfc], 1
PATCHED = b"\x90" * len(ORIGINAL)                    # nop x 10


def file_offset(data, va):
    """Map a virtual address to a file offset using the PE section table."""
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("not a PE file")
    sections = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    image_base = struct.unpack_from("<I", data, pe + 24 + 28)[0]
    rva = va - image_base
    table = pe + 24 + opt_size
    for i in range(sections):
        vsize, vaddr, rsize, raddr = struct.unpack_from("<IIII", data, table + i * 40 + 8)
        if vaddr <= rva < vaddr + max(vsize, rsize):
            return raddr + rva - vaddr
    raise ValueError(f"address {va:#x} isn't in any section")


def default_folders():
    """The game folder (Settings > Game) and each account's own "dir", once each."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from companion.launcher import accounts, game_dir
    seen = []
    for f in [game_dir(), *(a["dir"] for a in accounts(running={}) if a["dir"])]:
        if Path(f) not in seen and (Path(f) / SOURCE).exists():
            seen.append(Path(f))
    return seen


def main():
    folders = [Path(a) for a in sys.argv[1:]] or default_folders()
    if not folders:
        print(f"No {SOURCE} found: pick the game folder in Settings > Game, or pass the folder.")
        return 1
    return max(patch(f) for f in folders)


def patch(folder):
    src, dst = folder / SOURCE, folder / TARGET
    data = bytearray(src.read_bytes())
    off = file_offset(data, PATCH_VA)
    current = bytes(data[off:off + len(ORIGINAL)])
    if current == PATCHED:
        print(f"{src} is already patched; copying it as {TARGET}.")
    elif current != ORIGINAL:
        print(f"{src}: unexpected bytes {current.hex()} at {PATCH_VA:#x} (a different client build?). Nothing written.")
        return 1
    data[off:off + len(ORIGINAL)] = PATCHED
    try:
        dst.write_bytes(bytes(data))
    except PermissionError:
        # The copy has to sit next to the original: the client loads dgVoodoo's DDraw.dll from its own folder.
        print(f"Windows won't let this write into {folder} without administrator rights. Run this once from an\n"
              f"administrator terminal (right-click Terminal > Run as administrator):\n"
              f"    python \"{Path(__file__).resolve()}\" \"{folder}\"")
        return 1
    print(f"Wrote {dst} (keeps drawing in the background). The original {SOURCE} is unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
