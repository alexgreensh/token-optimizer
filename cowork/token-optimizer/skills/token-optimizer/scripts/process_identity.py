"""Read a process creation stamp without launching a command."""

import sys
from pathlib import Path


def process_start(pid):
    """Return a comparable stamp for this PID's current process, or None."""
    try:
        pid = int(pid)
        if not 0 < pid < 2**31:
            return None
        if sys.platform.startswith("linux"):
            stat = Path(f"/proc/{pid}/stat").read_text()
            # The executable name can itself contain spaces and parentheses.
            return stat.rsplit(")", 1)[1].split()[19]
        if sys.platform == "darwin":
            info = _darwin_info(pid)
            if info is not None:
                return f"{info.start_seconds}:{info.start_microseconds}"
        if sys.platform == "win32":
            return _windows_start(pid)
    except (OSError, ValueError, TypeError, IndexError):
        return None
    return None


def process_parent(pid):
    """Read the kernel's parent PID without starting ps in a Stop hook."""
    try:
        if sys.platform.startswith("linux"):
            stat = Path(f"/proc/{int(pid)}/stat").read_text()
            return int(stat.rsplit(")", 1)[1].split()[1])
        if sys.platform == "darwin":
            info = _darwin_info(int(pid))
            return info.ppid if info is not None else None
    except (OSError, ValueError, TypeError, IndexError):
        pass
    return None


def _darwin_info(pid):
    import ctypes

    class ProcBSDInfo(ctypes.Structure):
        # sys/proc_info.h: the fixed-width fields preceding the start timeval.
        _fields_ = [("prefix", ctypes.c_byte * 16), ("ppid", ctypes.c_uint32),
                    ("remaining", ctypes.c_byte * 100),
                    ("start_seconds", ctypes.c_uint64), ("start_microseconds", ctypes.c_uint64)]

    proc_pidinfo = ctypes.CDLL("/usr/lib/libproc.dylib").proc_pidinfo
    proc_pidinfo.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
    info = ProcBSDInfo()
    size = ctypes.sizeof(info)
    if proc_pidinfo(pid, 3, 0, ctypes.byref(info), size) == size:  # PROC_PIDTBSDINFO
        return info
    return None


def _windows_start(pid):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32")
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    # PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if (kernel.GetProcessTimes(handle, *(ctypes.byref(stamp) for stamp in times))
                and kernel.WaitForSingleObject(handle, 0) == 258):  # WAIT_TIMEOUT: still running
            created = times[0]
            return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)
    return None
