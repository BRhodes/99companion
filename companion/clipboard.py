"""Text onto the Windows clipboard, kept out of clipboard history (Win+V) and cloud clipboard: the companion's
copies replace each other rather than piling up there. Nothing anywhere else (the copy just doesn't happen)."""

import ctypes
import logging
import sys

log = logging.getLogger("companion")
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
# the registered formats Windows looks for to leave a copy out of its history and cloud clipboard
EXCLUDE = "ExcludeClipboardContentFromMonitorProcessing"
NO_HISTORY = "CanIncludeInClipboardHistory"
NO_CLOUD = "CanUploadToCloud"


def _setup():
    if sys.platform != "win32":
        return None
    from ctypes import wintypes
    user32, kernel32 = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes, user32.OpenClipboard.restype = [wintypes.HWND], wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes, user32.SetClipboardData.restype = [wintypes.UINT, wintypes.HANDLE], wintypes.HANDLE
    user32.RegisterClipboardFormatW.argtypes, user32.RegisterClipboardFormatW.restype = [wintypes.LPCWSTR], wintypes.UINT
    kernel32.GlobalAlloc.argtypes, kernel32.GlobalAlloc.restype = [wintypes.UINT, ctypes.c_size_t], wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes, kernel32.GlobalLock.restype = [wintypes.HGLOBAL], wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes, kernel32.GlobalUnlock.restype = [wintypes.HGLOBAL], wintypes.BOOL
    kernel32.GlobalFree.argtypes, kernel32.GlobalFree.restype = [wintypes.HGLOBAL], wintypes.HGLOBAL
    return user32, kernel32


_API = None


def _global(kernel32, data):
    """The bytes in a moveable global block (the clipboard takes it over once set)."""
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, max(1, len(data)))
    if not handle:
        raise OSError("GlobalAlloc failed")
    ptr = kernel32.GlobalLock(handle)
    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(handle)
    return handle


def copy(text):
    """Put `text` on the clipboard (out of its history). True if it went on."""
    global _API
    if _API is None:
        _API = _setup() or False
    if not _API:
        return False
    user32, kernel32 = _API
    for _ in range(5):                      # another program may hold it for a moment
        if user32.OpenClipboard(None):
            break
        ctypes.windll.kernel32.Sleep(20)
    else:
        log.warning("clipboard busy: the appraisal wasn't copied")
        return False
    try:
        user32.EmptyClipboard()
        data = (text + "\0").encode("utf-16-le")
        if not user32.SetClipboardData(CF_UNICODETEXT, _global(kernel32, data)):
            return False
        user32.SetClipboardData(user32.RegisterClipboardFormatW(EXCLUDE), _global(kernel32, b""))
        zero = (0).to_bytes(4, "little")     # a DWORD 0: no
        user32.SetClipboardData(user32.RegisterClipboardFormatW(NO_HISTORY), _global(kernel32, zero))
        user32.SetClipboardData(user32.RegisterClipboardFormatW(NO_CLOUD), _global(kernel32, zero))
        return True
    except OSError as e:
        log.warning("clipboard: %s", e)
        return False
    finally:
        user32.CloseClipboard()
