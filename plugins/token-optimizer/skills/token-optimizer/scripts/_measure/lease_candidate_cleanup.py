"""Reap old candidates belonging to the two root-level lease locks."""
import json
import os
import re
import stat
import time

_NAME = re.compile(r"^\.\.(?:archive-cleanup\.lease|cohort_tripwire\.lock\.lease)\.candidate-[0-9a-f]{32}$")
_AGE = 24 * 60 * 60


def owner_is_gone(pid):
    """Treat an unknown liveness result as alive, including on Windows."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            kernel.GetExitCodeProcess.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel.CloseHandle.restype = wintypes.BOOL
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return ctypes.get_last_error() == 87
            try:
                code = wintypes.DWORD()
                if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value != 259
            finally:
                kernel.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        pass
    return False


def sweep(directory, now=None, max_files=2000):
    cutoff = (time.time() if now is None else now) - _AGE
    removed = 0
    try:
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index >= max_files:
                    break
                if not _NAME.fullmatch(entry.name):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_mtime >= cutoff:
                        continue
                    # A long-paused owner still needs its candidate to release.
                    try:
                        with open(entry.path, encoding="utf-8") as f:
                            owner = json.loads(f.read(4096))
                        pid = int(owner.get("pid", 0))
                        if pid > 0 and not owner_is_gone(pid):
                            continue
                    except OSError:
                        continue
                    except (ValueError, TypeError, AttributeError):
                        pass
                    current = entry.stat(follow_symlinks=False)
                    if not os.path.samestat(info, current) or current.st_mtime != info.st_mtime:
                        continue
                    os.unlink(entry.path)
                    removed += 1
                except OSError:
                    continue
    except OSError:
        pass
    return removed
