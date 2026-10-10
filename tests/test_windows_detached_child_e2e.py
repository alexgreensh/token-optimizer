"""Real end-to-end checks for the Windows DETACHED_PROCESS hook child.

hooks/run.py starts its supervised child with ``DETACHED_PROCESS`` plus the three
std handles passed explicitly (``_windows_stdio_kwargs``), so the child gets no
console and no conhost.exe (issue #215). A detached child has no console to
inherit stdio from: if the explicit handles ever stop reaching it, every hook on
Windows keeps "working" while silently discarding all output. The mocked tests
in test_windows_spawn_no_window.py only check the Popen arguments. These tests
use no mocks and no monkeypatching: they start the real hooks/run.py the way a
host does (a subprocess with PIPEs, never a shell) and drive a throwaway hook
script the test writes itself, inside a sandboxed HOME / config dir.

How a hook is resolved (hooks/run.py ``main``): ``run.py <script-rel-path>
[args...]`` looks the script up under ``CLAUDE_PLUGIN_ROOT``, then spawns
``sys.executable hooks/module_runner.py <script dir> <script stem> [args...]``
with DETACHED_PROCESS on nt. module_runner runs the script in-process via
runpy, so the hook script IS the supervised child process.

What each Windows test proves is in its docstring. Shared scenario functions
(``_scenario_*``) hold the assertions; the Windows tests and the single POSIX
sanity test call the same ones, so a harness bug shows up locally on macOS or
Linux before it can waste a CI cycle on windows-latest.

Contract note: run.py always exits 0 (its docstring: "Always exits 0 so hook
failures never block the user's tool call"). The child's exit code is
therefore NOT propagated by run.py; the exit-code scenario asserts that
contract and that the child really ran to its ``sys.exit(n)``. The utf8_io
re-exec path does propagate the code (``os._exit(rc)``) and is asserted so.

Windows-only tests are skipped elsewhere. Run: python -m pytest
tests/test_windows_detached_child_e2e.py -q
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
REAL_RUN_PY = REPO / "hooks" / "run.py"
REAL_MODULE_RUNNER = REPO / "hooks" / "module_runner.py"

sys.path.insert(0, str(SCRIPTS))

IS_NT = os.name == "nt"
windows_only = pytest.mark.skipif(not IS_NT, reason="DETACHED_PROCESS semantics are Windows-only")

# Child timeout inside the patched run.py copy used by the hang tests.
PATCHED_TIMEOUT_S = 3
HOOK_REL = "hooks/e2e_probe_hook.py"

# Windows creation flags, spelled out so a host flavour is readable in a failing
# test id. Off-Windows these are never passed.
DETACHED_PROCESS = 0x00000008
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NO_WINDOW = 0x08000000
HOST_FLAVOURS = {
    "inherit": 0,
    "detached": DETACHED_PROCESS,
    "no_window": CREATE_NO_WINDOW,
}


# ---------------------------------------------------------------------------
# Deterministic payloads (the hook below embeds make_payload's source, so the
# child and the test can never disagree about the expected bytes)
# ---------------------------------------------------------------------------
def make_payload():
    """~200 KB of JSON with non-ASCII text, as a hook would print on stdout."""
    import json
    items = []
    size = 0
    i = 0
    while size < 200_000:
        item = {"i": i, "msg": "héllo wörld Привет мир 日本語 שלום 🎉 " + "x" * 40}
        items.append(item)
        size += len(json.dumps(item, ensure_ascii=False).encode("utf-8")) + 2
        i += 1
    return (json.dumps({"hookSpecificOutput": {"items": items}}, ensure_ascii=False) + "\n").encode("utf-8")


def make_stdin_payload():
    """~300 KB hook-input JSON with non-ASCII text (a big transcript prompt)."""
    prompt = ("שלום 日本語 Привет héllo 🎉 " * 4000)
    return json.dumps(
        {"session_id": "e2e-session", "hook_event_name": "UserPromptSubmit", "prompt": prompt},
        ensure_ascii=False,
    ).encode("utf-8")


def make_stderr_text():
    """~150 KB of non-ASCII diagnostics text for the child's stderr."""
    return ("warn: dïagnostic 日本語 🎉 line\n" * 5000).encode("utf-8")


# ---------------------------------------------------------------------------
# The throwaway hook the test writes. It runs both as a hooks/ script under
# run.py (as __main__ via runpy) and, for the controls, directly.
# ---------------------------------------------------------------------------
_HOOK_PROLOGUE = '''\
import hashlib
import json
import os
import sys
import time

'''

_HOOK_BODY = '''

REPORT = os.environ["TO_E2E_REPORT"]
FACTS = {}


def _save():
    tmp = REPORT + ".tmp"
    for _ in range(50):
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(FACTS))
            os.replace(tmp, REPORT)
            return
        except OSError:
            time.sleep(0.05)


def _win_facts():
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetConsoleWindow.restype = wintypes.HWND
    k.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    k.GetConsoleProcessList.restype = wintypes.DWORD
    k.GetStdHandle.argtypes = [wintypes.DWORD]
    k.GetStdHandle.restype = wintypes.HANDLE
    k.GetFileType.argtypes = [wintypes.HANDLE]
    k.GetFileType.restype = wintypes.DWORD
    buf = (wintypes.DWORD * 64)()
    # 0 = this process has NO console attached at all (what DETACHED_PROCESS
    # gives). GetConsoleWindow() alone cannot tell "no console" from "hidden
    # console" (CREATE_NO_WINDOW), so it is recorded but not relied on.
    FACTS["console_process_count"] = int(k.GetConsoleProcessList(buf, 64))
    FACTS["console_window"] = bool(k.GetConsoleWindow())
    for name, std in (("stdin", -10), ("stdout", -11), ("stderr", -12)):
        h = k.GetStdHandle(std)
        FACTS["os_" + name + "_null"] = not h
        # GetFileType: 0 unknown/invalid, 1 disk, 2 char (incl. NUL), 3 pipe
        FACTS["os_" + name + "_filetype"] = int(k.GetFileType(h)) if h else 0


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "report"
    FACTS.update(
        mode=mode,
        pid=os.getpid(),
        ppid=os.getppid(),
        nt=(os.name == "nt"),
        utf8_mode=int(sys.flags.utf8_mode),
        reexec_sentinel=os.environ.get("TOKEN_OPTIMIZER_UTF8_REEXEC"),
        py_stdin_none=sys.stdin is None,
        py_stdout_none=sys.stdout is None,
        py_stderr_none=sys.stderr is None,
    )
    if os.name == "nt":
        _win_facts()
    _save()

    if mode == "emit":
        code = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        FACTS["exit_code_intended"] = code
        try:
            sys.stderr.buffer.write("hook-stderr: dïagnostic 日本語 🎉\\n".encode("utf-8"))
            sys.stderr.buffer.flush()
        except Exception as exc:
            FACTS["stderr_error"] = repr(exc)
        try:
            sys.stdout.buffer.write(make_payload())
            sys.stdout.buffer.flush()
            FACTS["stdout_written"] = True
        except Exception as exc:
            FACTS["stdout_error"] = repr(exc)
        _save()
        sys.exit(code)
    elif mode == "echo":
        try:
            data = sys.stdin.buffer.read()
        except Exception as exc:
            FACTS["stdin_error"] = repr(exc)
            data = b""
        FACTS["stdin_len"] = len(data)
        FACTS["stdin_sha256"] = hashlib.sha256(data).hexdigest()
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
            FACTS["stdout_written"] = True
        except Exception as exc:
            FACTS["stdout_error"] = repr(exc)
        _save()
    elif mode == "stderr":
        text = make_stderr_text()
        try:
            sys.stderr.buffer.write(text)
            sys.stderr.buffer.flush()
            FACTS["stderr_written"] = len(text)
        except Exception as exc:
            FACTS["stderr_error"] = repr(exc)
        _save()
    elif mode == "hang":
        FACTS["hanging"] = True
        _save()
        while True:
            time.sleep(0.2)
    elif mode == "linger":
        release = sys.argv[2]
        FACTS["lingering"] = True
        _save()
        deadline = time.time() + 60
        while time.time() < deadline and not os.path.exists(release):
            time.sleep(0.1)
    # mode == "report": facts only
    FACTS["finished"] = True
    _save()


if __name__ == "__main__":
    main()
'''


def hook_source() -> str:
    helpers = "\n\n".join(inspect.getsource(f) for f in (make_payload, make_stderr_text))
    return _HOOK_PROLOGUE + helpers + _HOOK_BODY


UTF8_ENTRY_PROLOGUE = '''\
import sys
sys.path.insert(0, {scripts!r})
import utf8_io
utf8_io.reexec_in_utf8_mode()
utf8_io.enforce_utf8_io()
'''


# ---------------------------------------------------------------------------
# Sandbox + driver
# ---------------------------------------------------------------------------
class Sandbox:
    """A throwaway plugin tree and a fully sandboxed environment."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.plugin_root = tmp / "plugin"
        self.report = tmp / "report.json"
        self.cwd = tmp / "cwd"
        for d in (self.home / ".claude", self.home / "AppData" / "Roaming",
                  self.home / "AppData" / "Local", self.plugin_root / "hooks",
                  self.tmp / "snapshots", self.tmp / "tmpdir", self.cwd):
            d.mkdir(parents=True, exist_ok=True)
        self.hook = self.plugin_root / HOOK_REL
        self.hook.write_text(hook_source(), encoding="utf-8")
        self.env = self._build_env()

    def _build_env(self) -> dict:
        env = dict(os.environ)
        for key in list(env):
            if key.upper().startswith(("TOKEN_OPTIMIZER_", "CLAUDE_", "CODEX_", "TO_E2E_")) or \
                    key.upper() in ("PYTHONUTF8", "PYTHONIOENCODING", "PYTHONPATH", "PYTHONSTARTUP"):
                env.pop(key)
        env.update(
            HOME=str(self.home),
            USERPROFILE=str(self.home),
            APPDATA=str(self.home / "AppData" / "Roaming"),
            LOCALAPPDATA=str(self.home / "AppData" / "Local"),
            CLAUDE_CONFIG_DIR=str(self.home / ".claude"),
            TOKEN_OPTIMIZER_SNAPSHOT_DIR=str(self.tmp / "snapshots"),
            CLAUDE_PLUGIN_ROOT=str(self.plugin_root),
            TEMP=str(self.tmp / "tmpdir"),
            TMP=str(self.tmp / "tmpdir"),
            TMPDIR=str(self.tmp / "tmpdir"),
            TO_E2E_REPORT=str(self.report),
        )
        return env

    # -- targets -----------------------------------------------------------
    def run_py_argv(self, args, run_py: Path = REAL_RUN_PY) -> list:
        return [sys.executable, str(run_py), HOOK_REL, *args]

    def patched_run_py(self, timeout_s: int = PATCHED_TIMEOUT_S) -> Path:
        """A copy of the REAL run.py whose child wait() is shortened.

        run.py waits a hard-coded 120 s for its child. The only change is that
        number; the assertion below fails loudly if run.py's wait line ever
        changes shape, rather than silently testing something else.
        """
        src = REAL_RUN_PY.read_text(encoding="utf-8")
        needle = "proc.wait(timeout=120)"
        assert src.count(needle) == 1, (
            "hooks/run.py no longer contains exactly one %r; update patched_run_py() so the "
            "shortened-timeout hang test still exercises the real wait/reap code" % needle
        )
        d = self.tmp / "patched_runner"
        d.mkdir(exist_ok=True)
        (d / "run.py").write_text(src.replace(needle, "proc.wait(timeout=%d)" % timeout_s), encoding="utf-8")
        # run.py resolves module_runner.py next to itself.
        (d / "module_runner.py").write_text(REAL_MODULE_RUNNER.read_text(encoding="utf-8"), encoding="utf-8")
        return d / "run.py"

    def utf8_entry_argv(self, args) -> list:
        entry = self.tmp / "utf8_entry.py"
        entry.write_text(
            UTF8_ENTRY_PROLOGUE.format(scripts=str(SCRIPTS)) + hook_source(), encoding="utf-8")
        return [sys.executable, str(entry), *args]

    # -- running -----------------------------------------------------------
    def popen(self, argv, *, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
              stderr=subprocess.PIPE, creationflags=0, env=None):
        self.report.unlink(missing_ok=True)  # never let a stale report from an earlier spawn pass a test
        kwargs = dict(stdin=stdin, stdout=stdout, stderr=stderr, cwd=str(self.cwd),
                      env=env if env is not None else self.env)
        if IS_NT and creationflags:
            kwargs["creationflags"] = creationflags
        return subprocess.Popen(argv, **kwargs)  # list argv, never a shell

    def run(self, argv, input_bytes=None, *, stdin="pipe", stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, creationflags=0, timeout=120):
        """Run to completion like a host: write stdin, drain stdout+stderr."""
        stdin_arg = {"pipe": subprocess.PIPE, "devnull": subprocess.DEVNULL}[stdin]
        t0 = time.perf_counter()
        proc = self.popen(argv, stdin=stdin_arg, stdout=stdout, stderr=stderr,
                          creationflags=creationflags)
        try:
            out, err = proc.communicate(input=input_bytes if stdin == "pipe" else None, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            raise AssertionError(
                "host-style run did not finish within %ss (stdout=%d bytes, stderr=%r); a hook "
                "that never exits, or stdio that never reaches EOF, blocks the host"
                % (timeout, len(out or b""), (err or b"")[:300]))
        return RunResult(proc.pid, proc.returncode, out, err, time.perf_counter() - t0, self.read_report(wait=False))

    def read_report(self, *, wait=True, timeout=20.0, key=None):
        """Read the hook's facts file; with wait, poll until it (and ``key``) exists."""
        deadline = time.time() + (timeout if wait else 0)
        last = None
        while True:
            try:
                data = json.loads(self.report.read_text(encoding="utf-8"))
                if key is None or key in data:
                    return data
                last = data
            except (OSError, ValueError):
                pass
            if time.time() >= deadline:
                return last
            time.sleep(0.05)


class RunResult:
    def __init__(self, pid, returncode, stdout, stderr, elapsed, report):
        self.pid, self.returncode, self.stdout, self.stderr = pid, returncode, stdout, stderr
        self.elapsed, self.report = elapsed, report

    def diag(self) -> str:
        return "rc=%r stdout=%d bytes (head %r) stderr=%r report=%r" % (
            self.returncode, len(self.stdout or b""), (self.stdout or b"")[:120],
            (self.stderr or b"")[:400], self.report)


@pytest.fixture
def sandbox(tmp_path):
    return Sandbox(tmp_path)


# ---------------------------------------------------------------------------
# Process helpers (Windows: ctypes only, no psutil on the CI runner)
# ---------------------------------------------------------------------------
def pid_is_alive(pid: int) -> bool:
    if not IS_NT:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False  # ERROR_INVALID_PARAMETER: no process with this pid
    try:
        return k.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
    finally:
        k.CloseHandle(handle)


def kill_pid_best_effort(pid):
    """Cleanup for a pid THIS test started; SIGTERM maps to TerminateProcess on nt."""
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass


def process_snapshot():
    """[(pid, ppid, exe_name_lower)] for every process, via Toolhelp32 (nt only)."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    k.Process32FirstW.restype = wintypes.BOOL
    k.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    k.Process32NextW.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    snap = k.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snap in (None, ctypes.c_void_p(-1).value):
        raise OSError("CreateToolhelp32Snapshot failed: winerror %d" % ctypes.get_last_error())
    out = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile.lower()))
            ok = k.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k.CloseHandle(snap)
    return out


def descendants(snapshot, root_pid):
    """pids below root_pid in the parent/child tree (excludes root_pid)."""
    children = {}
    for pid, ppid, _ in snapshot:
        children.setdefault(ppid, []).append(pid)
    found, stack = set(), [root_pid]
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in found and child != root_pid:
                found.add(child)
                stack.append(child)
    return found


# ---------------------------------------------------------------------------
# Shared scenarios: used by the Windows tests AND the POSIX sanity test
# ---------------------------------------------------------------------------
def _expect_rc(target, code):
    """run.py swallows the child's exit code by design; the utf8_io re-exec passes it on."""
    return 0 if target == "run_py" else code


def _argv(sb, target, args):
    return sb.run_py_argv(args) if target == "run_py" else sb.utf8_entry_argv(args)


def _scenario_stdout_roundtrip(sb, target="run_py", creationflags=0):
    expected = make_payload()
    assert len(expected) > 64 * 1024  # the point is to cross a pipe buffer
    r = sb.run(_argv(sb, target, ["emit", "0"]), b"{}", creationflags=creationflags)
    assert r.returncode == 0, r.diag()
    assert r.report and r.report.get("stdout_written") is True, (
        "the hook never reported a successful stdout write, so it could not even reach the "
        "child's stdout: " + r.diag())
    assert r.stdout == expected, (
        "bytes the child wrote to stdout did not reach run.py's stdout pipe intact: got %d bytes, "
        "expected %d; first difference at offset %s; %s" % (
            len(r.stdout), len(expected),
            next((i for i, (a, b) in enumerate(zip(r.stdout, expected)) if a != b), "n/a (length only)"),
            r.diag()))
    decoded = json.loads(r.stdout.decode("utf-8"))
    assert "日本語" in decoded["hookSpecificOutput"]["items"][0]["msg"]
    return r


def _scenario_stdin_roundtrip(sb, target="run_py", creationflags=0):
    payload = make_stdin_payload()
    assert len(payload) > 64 * 1024
    r = sb.run(_argv(sb, target, ["echo"]), payload, creationflags=creationflags)
    assert r.returncode == 0, r.diag()
    rep = r.report or {}
    assert rep.get("stdin_len") == len(payload), (
        "bytes sent on run.py's stdin did not all reach the child: child read %r of %d bytes; %s"
        % (rep.get("stdin_len"), len(payload), r.diag()))
    assert rep.get("stdin_sha256") == hashlib.sha256(payload).hexdigest(), (
        "child read the right NUMBER of stdin bytes but they were altered; " + r.diag())
    assert r.stdout == payload, "child echoed its stdin back but the stdout bytes differ: " + r.diag()
    return r


def _scenario_stderr(sb, target="run_py", creationflags=0):
    expected = make_stderr_text()
    assert len(expected) > 64 * 1024
    r = sb.run(_argv(sb, target, ["stderr"]), b"{}", creationflags=creationflags)
    assert r.returncode == 0, r.diag()
    assert r.stderr == expected, (
        "the child's stderr did not reach run.py's stderr pipe intact: got %d bytes, expected %d "
        "(head %r); %s" % (len(r.stderr), len(expected), r.stderr[:200], r.diag()))
    return r


def _scenario_exit_code(sb, code, target="run_py", creationflags=0):
    r = sb.run(_argv(sb, target, ["emit", str(code)]), b"{}", creationflags=creationflags)
    rep = r.report or {}
    assert rep.get("exit_code_intended") == code and rep.get("stdout_written") is True, (
        "the hook never reached its sys.exit(%d), so the exit code was never exercised: %s"
        % (code, r.diag()))
    assert r.returncode == _expect_rc(target, code), (
        "%s exited %r after a child that exits %d; expected %r. run.py is documented to ALWAYS exit "
        "0 (a failing hook must never block the user); the utf8_io re-exec must pass the code "
        "through. %s" % (target, r.returncode, code, _expect_rc(target, code), r.diag()))
    assert b"Traceback" not in r.stderr, "run.py/child crashed: " + r.diag()
    return r


def _scenario_hang_is_killed(sb, record=None):
    """A hanging child is killed at run.py's wait timeout and the pid is really gone."""
    run_py = sb.patched_run_py(PATCHED_TIMEOUT_S)
    pid = None
    t0 = time.perf_counter()
    proc = sb.popen(sb.run_py_argv(["hang"], run_py=run_py))
    try:
        try:
            out, err = proc.communicate(input=b"{}", timeout=PATCHED_TIMEOUT_S + 40)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            rep = sb.read_report(wait=False) or {}
            if rep.get("pid"):
                kill_pid_best_effort(rep["pid"])
            raise AssertionError(
                "run.py did not return within %ds although its child wait timeout is %ds: the hung "
                "child was not killed (or still holds the stdout pipe). report=%r"
                % (PATCHED_TIMEOUT_S + 40, PATCHED_TIMEOUT_S, rep))
        elapsed = time.perf_counter() - t0
        rep = sb.read_report(wait=False) or {}
        pid = rep.get("pid")
        assert rep.get("hanging") is True and pid, (
            "the hang hook never started (no pid in report): rc=%r stderr=%r report=%r"
            % (proc.returncode, err[:400], rep))
        assert proc.returncode == 0, "run.py must exit 0 after reaping: rc=%r stderr=%r" % (proc.returncode, err[:400])
        assert elapsed >= PATCHED_TIMEOUT_S - 0.5, (
            "run.py returned after %.2fs, before the %ds timeout, so the child did not hang "
            "(it died on its own) and the kill path was not exercised; report=%r"
            % (elapsed, PATCHED_TIMEOUT_S, rep))
        assert elapsed < PATCHED_TIMEOUT_S + 30, "run.py returned too slowly after the timeout: %.1fs" % elapsed
        # TerminateProcess/SIGKILL is asynchronous from the caller's view; run.py waited for it,
        # but poll briefly so a loaded runner cannot make this flaky.
        gone_deadline = time.time() + 10
        while pid_is_alive(pid) and time.time() < gone_deadline:
            time.sleep(0.1)
        assert not pid_is_alive(pid), (
            "hung child pid %d is STILL ALIVE after run.py returned: it would keep holding "
            "trends.db's SQLite lock and the hook stdout pipe" % pid)
        if record:
            record("hang_run_py_elapsed_s", round(elapsed, 2))
    finally:
        if pid and pid_is_alive(pid):
            kill_pid_best_effort(pid)
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def _scenario_degenerate_parents(sb, target="run_py"):
    """run.py/the entry started with DEVNULL stdout, DEVNULL stdin, or stdin closed."""
    payload = make_stdin_payload()

    # stdout=DEVNULL: the child must still run to completion; it is writing into NUL.
    r = sb.run(_argv(sb, target, ["emit", "0"]), b"{}", stdout=subprocess.DEVNULL)
    rep = r.report or {}
    assert r.returncode == 0, "stdout=DEVNULL parent: " + r.diag()
    assert rep.get("exit_code_intended") == 0, "child did not run with stdout=DEVNULL: " + r.diag()
    assert "stdout_error" not in rep, "child could not write to a DEVNULL stdout: " + r.diag()
    assert b"Traceback" not in r.stderr, r.diag()
    if IS_NT:
        assert rep.get("os_stdout_filetype") == 2, (
            "stdout=DEVNULL should reach the child as the NUL character device (FILE_TYPE_CHAR=2), "
            "not a NULL/invalid handle: " + r.diag())

    # stdin=DEVNULL: child reads EOF immediately and returns.
    r = sb.run(_argv(sb, target, ["echo"]), stdin="devnull")
    rep = r.report or {}
    assert r.returncode == 0 and rep.get("stdin_len") == 0 and "stdin_error" not in rep, (
        "stdin=DEVNULL parent: child should read 0 bytes without raising: " + r.diag())
    assert b"Traceback" not in r.stderr, r.diag()

    # stdin closed by the host straight away (EOF without data).
    proc = sb.popen(_argv(sb, target, ["echo"]))
    proc.stdin.close()
    proc.stdin = None  # communicate() must not flush the already-closed pipe
    out, err = proc.communicate(timeout=120)
    rep = sb.read_report(wait=False) or {}
    assert proc.returncode == 0 and rep.get("stdin_len") == 0 and "stdin_error" not in rep, (
        "closed-stdin parent: rc=%r report=%r stderr=%r" % (proc.returncode, rep, err[:300]))
    assert b"Traceback" not in err, err[:300]

    # Sanity that the harness is not vacuous: the same child DOES get data when stdin is live.
    r = sb.run(_argv(sb, target, ["echo"]), payload)
    assert (r.report or {}).get("stdin_len") == len(payload), r.diag()


# ---------------------------------------------------------------------------
# POSIX sanity: the same driver and scenarios, so harness bugs show up locally
# ---------------------------------------------------------------------------
@pytest.mark.skipif(IS_NT, reason="the Windows tests below run the same scenarios on nt")
def test_driver_scenarios_work_with_pipes_on_posix(sandbox, record_property):
    _scenario_stdout_roundtrip(sandbox)
    _scenario_stdin_roundtrip(sandbox)
    _scenario_stderr(sandbox)
    for code in (0, 2, 7):
        _scenario_exit_code(sandbox, code)
    _scenario_hang_is_killed(sandbox, record_property)
    _scenario_degenerate_parents(sandbox)


# ---------------------------------------------------------------------------
# Windows: run.py -> DETACHED_PROCESS child
# ---------------------------------------------------------------------------
@windows_only
@pytest.mark.parametrize("flavour", sorted(HOST_FLAVOURS))
def test_child_stdout_reaches_host_pipe(sandbox, flavour):
    """The child's stdout (>64 KB, UTF-8 with Hebrew/CJK/Cyrillic/emoji) arrives byte-identical
    on run.py's stdout pipe, for hosts that hand run.py no console, a hidden one, or inherit."""
    _scenario_stdout_roundtrip(sandbox, creationflags=HOST_FLAVOURS[flavour])


@windows_only
def test_host_stdin_reaches_child(sandbox):
    """A >64 KB hook-input JSON written to run.py's stdin is read in full, unaltered, by the child."""
    _scenario_stdin_roundtrip(sandbox)


@windows_only
def test_child_stderr_reaches_host_pipe(sandbox):
    """>64 KB written to the child's stderr arrives intact on run.py's stderr pipe."""
    _scenario_stderr(sandbox)


@windows_only
@pytest.mark.parametrize("code", [0, 2, 7])
def test_exit_codes_through_run_py(sandbox, code):
    """The child reaches its sys.exit(code); run.py survives it and exits 0 (its documented
    contract: a failing hook never blocks the user). The code is NOT propagated by run.py."""
    _scenario_exit_code(sandbox, code)


@windows_only
def test_hung_child_is_killed_and_gone(sandbox, record_property):
    """With run.py's wait shortened to 3 s (only that number differs from the real file), a
    child that never exits is killed at the timeout, its pid is really gone, run.py returns
    promptly with 0 and the stdout pipe reaches EOF (communicate() returned)."""
    _scenario_hang_is_killed(sandbox, record_property)


@windows_only
@pytest.mark.skipif(not os.environ.get("TOKEN_OPTIMIZER_E2E_REAL_TIMEOUT"),
                    reason="takes ~2 minutes; set TOKEN_OPTIMIZER_E2E_REAL_TIMEOUT=1 to run the "
                           "unpatched 120 s wait")
def test_hung_child_is_killed_with_the_real_120s_timeout(sandbox):
    """Same as above against the UNMODIFIED hooks/run.py (opt-in: it waits the full 120 s)."""
    t0 = time.perf_counter()
    proc = sandbox.popen(sandbox.run_py_argv(["hang"]))
    pid = None
    try:
        proc.communicate(input=b"{}", timeout=240)
        pid = (sandbox.read_report(wait=False) or {}).get("pid")
        assert pid and time.perf_counter() - t0 >= 119
        assert not pid_is_alive(pid), "hung child pid %s survived the real 120 s timeout" % pid
    finally:
        if pid and pid_is_alive(pid):
            kill_pid_best_effort(pid)
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


@windows_only
@pytest.mark.parametrize("flavour", sorted(HOST_FLAVOURS))
def test_child_has_no_console(sandbox, flavour):
    """The supervised child has NO console at all (GetConsoleProcessList == 0), whatever
    console state run.py's own host gave it. This is the property DETACHED_PROCESS buys and
    CREATE_NO_WINDOW does not: a hidden console would still count >= 1 here. The child's
    stdout/stderr/stdin are real pipes (FILE_TYPE_PIPE=3), not NULL handles."""
    r = sandbox.run(sandbox.run_py_argv(["report"]), b"{}", creationflags=HOST_FLAVOURS[flavour])
    rep = r.report or {}
    assert r.returncode == 0 and rep.get("finished"), "probe child did not run: " + r.diag()
    assert rep["nt"] is True
    assert rep["console_process_count"] == 0, (
        "the hook child has a console attached (%d processes share it): run.py's spawn is NOT "
        "DETACHED_PROCESS, so a conhost.exe is created per hook (issue #215). %s"
        % (rep["console_process_count"], r.diag()))
    assert rep["console_window"] is False
    for name in ("stdin", "stdout", "stderr"):
        assert rep["os_%s_null" % name] is False and rep["os_%s_filetype" % name] == 3, (
            "child's OS-level %s is not a pipe handle (null=%r filetype=%r): the explicit handle "
            "from _windows_stdio_kwargs() did not arrive. %s"
            % (name, rep["os_%s_null" % name], rep["os_%s_filetype" % name], r.diag()))


@windows_only
def test_console_probe_discriminates(sandbox, record_property):
    """Negative controls for the probe: the same hook DOES report a console when one exists
    (CREATE_NEW_CONSOLE), so console_process_count == 0 above means 'detached', not 'probe
    blind'. CREATE_NO_WINDOW is recorded (not asserted): it documents what the old flag gave."""
    for label, flags, must_have_console in (("new_console", CREATE_NEW_CONSOLE, True),
                                            ("no_window", CREATE_NO_WINDOW, None)):
        proc = sandbox.popen([sys.executable, str(sandbox.hook), "report"], creationflags=flags)
        proc.communicate(timeout=60)
        rep = sandbox.read_report(key="finished") or {}
        count = rep.get("console_process_count")
        record_property("console_process_count_under_" + label, count)
        if must_have_console:
            assert isinstance(count, int) and count >= 1, (
                "probe reported console_process_count=%r under %s; it cannot see consoles, so the "
                "no-console verdicts are meaningless. report=%r" % (count, label, rep))


_CONHOST_CACHE = {}


def _conhost_children_during(sandbox, argv, creationflags):
    """Start argv in 'linger' mode; return (child_pid, conhost pids parented under the tree of
    the started process, system-wide NEW conhost pids). Always releases and reaps."""
    before = {pid for pid, _, exe in process_snapshot() if exe == "conhost.exe"}
    release = sandbox.tmp / ("release-%d" % time.time_ns())
    proc = sandbox.popen([*argv, str(release)], creationflags=creationflags)
    try:
        rep = sandbox.read_report(key="lingering", timeout=60) or {}
        assert rep.get("lingering"), "linger hook never started: %r" % rep
        snap = process_snapshot()
        tree = descendants(snap, proc.pid) | {proc.pid}
        under_tree = sorted(pid for pid, ppid, exe in snap if exe == "conhost.exe" and ppid in tree)
        new_any = sorted({pid for pid, _, exe in snap if exe == "conhost.exe"} - before)
        return rep["pid"], tree, under_tree, new_any
    finally:
        release.write_text("go", encoding="utf-8")
        try:
            proc.communicate(input=b"{}", timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()


def _conhost_attribution_works(sandbox):
    """Control: a CREATE_NO_WINDOW child (the pre-fix behaviour) must show a conhost.exe whose
    parent is in its own process tree. If it does not, ppid-based attribution is unavailable
    on this runner and the real check below would pass vacuously."""
    if "ok" not in _CONHOST_CACHE:
        _, tree, under_tree, _ = _conhost_children_during(
            sandbox, [sys.executable, str(sandbox.hook), "linger"], CREATE_NO_WINDOW)
        _CONHOST_CACHE["ok"] = bool(under_tree)
        _CONHOST_CACHE["detail"] = "tree=%s conhost_under_tree=%s" % (sorted(tree), under_tree)
    return _CONHOST_CACHE["ok"], _CONHOST_CACHE["detail"]


@windows_only
def test_no_conhost_started_for_the_child(sandbox, record_property):
    """While the real run.py's child is alive, no conhost.exe exists anywhere in run.py's
    process tree. Control first: the same probe under CREATE_NO_WINDOW DOES show one, so a
    zero here is a real zero. System-wide new-conhost count is recorded, not asserted (an
    unrelated process on the runner may legitimately start one)."""
    works, detail = _conhost_attribution_works(sandbox)
    if not works:
        pytest.skip("conhost.exe is not parented under its console client on this Windows build, "
                    "so tree attribution cannot prove absence (%s). test_child_has_no_console still "
                    "proves the child has no console, which is what spawns conhost." % detail)
    child_pid, tree, under_tree, new_any = _conhost_children_during(
        sandbox, sandbox.run_py_argv(["linger"]), 0)
    record_property("new_conhost_pids_system_wide", new_any)
    assert child_pid in tree, (
        "the hook (pid %d) is not a descendant of the started run.py tree %s: the harness is not "
        "observing the real spawn" % (child_pid, sorted(tree)))
    assert not under_tree, (
        "conhost.exe pid(s) %s were started under run.py's process tree %s while the hook ran: "
        "the child is NOT console-less, issue #215 is not fixed" % (under_tree, sorted(tree)))


# -- degenerate parents ------------------------------------------------------
@windows_only
def test_run_py_survives_degenerate_host_stdio(sandbox):
    """run.py started with stdout=DEVNULL, with stdin=DEVNULL, and with stdin closed still runs
    its detached child and exits 0 without a traceback; DEVNULL arrives as the NUL device."""
    _scenario_degenerate_parents(sandbox)


@windows_only
def test_run_py_with_no_std_handles_at_all(sandbox):
    """Started by a native parent that gives it NO std handles (no console, no
    STARTF_USESTDHANDLES; run.py's sys.stdin/stdout/stderr are None), run.py still runs the
    child and exits 0, and the child still has no console. `_windows_stdio_kwargs()` skips all
    three streams here and Popen is called with none, so the child has no std handles either;
    the hook's own I/O errors are swallowed. Nobody is listening in this case, so nothing is
    lost, but it must not crash or hang."""
    import ctypes
    from ctypes import wintypes

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
        wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)]
    k.CreateProcessW.restype = wintypes.BOOL
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k.GetExitCodeProcess.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]

    cmdline = subprocess.list2cmdline(sandbox.run_py_argv(["echo"]))
    cmd_buf = ctypes.create_unicode_buffer(cmdline)
    block = "".join("%s=%s\0" % kv for kv in sandbox.env.items()) + "\0"
    env_buf = (ctypes.c_wchar * len(block))(*block)
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    pi = PROCESS_INFORMATION()
    flags = DETACHED_PROCESS | 0x00000400  # CREATE_UNICODE_ENVIRONMENT
    ok = k.CreateProcessW(None, cmd_buf, None, None, False, flags,
                          ctypes.cast(env_buf, ctypes.c_void_p), str(sandbox.cwd),
                          ctypes.byref(si), ctypes.byref(pi))
    assert ok, "CreateProcessW failed, winerror %d" % ctypes.get_last_error()
    try:
        wait = k.WaitForSingleObject(pi.hProcess, 120_000)
        assert wait == 0, "run.py with no std handles did not exit within 120 s (wait=%#x)" % wait
        code = wintypes.DWORD()
        assert k.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        rep = sandbox.read_report(wait=False) or {}
        assert code.value == 0, "run.py with no std handles exited %d; child report %r" % (code.value, rep)
        assert rep.get("mode") == "echo" and rep.get("finished") is True, (
            "the detached child did not run to completion when run.py had no std handles: %r" % rep)
        assert rep.get("console_process_count") == 0, "child has a console: %r" % rep
    finally:
        k.CloseHandle(pi.hProcess)
        k.CloseHandle(pi.hThread)


# -- utf8_io re-exec -----------------------------------------------------------
def _non_utf8_locale_env(sandbox):
    env = dict(sandbox.env)
    env["PYTHONUTF8"] = "0"  # forces the legacy ANSI code page even if the runner exports UTF-8
    return env


def _require_reexec_precondition(sandbox):
    out = subprocess.run(
        [sys.executable, "-c", "import locale; print(locale.getpreferredencoding(False))"],
        env=_non_utf8_locale_env(sandbox), capture_output=True, text=True, timeout=60).stdout
    enc = out.strip().lower().replace("-", "").replace("_", "")
    if enc in ("utf8", "cp65001", "65001", ""):
        pytest.skip("this runner's ANSI code page is UTF-8 (%r), so utf8_io never re-execs" % out.strip())


@windows_only
@pytest.mark.parametrize("flavour", ["detached", "no_window"])
def test_utf8_reexec_child_has_no_console(sandbox, flavour):
    """Under a non-UTF-8 code page the entry re-execs itself via utf8_io; the re-exec'd child
    (the one that does the work) is UTF-8 mode, has NO console even though its parent may
    have one (no_window host) and holds real pipe std handles."""
    _require_reexec_precondition(sandbox)
    sandbox.env = _non_utf8_locale_env(sandbox)
    r = sandbox.run(sandbox.utf8_entry_argv(["report"]), b"{}", creationflags=HOST_FLAVOURS[flavour])
    rep = r.report or {}
    assert rep.get("finished"), "the re-exec'd entry never completed: " + r.diag()
    assert rep.get("reexec_sentinel") == "1" and rep.get("utf8_mode") == 1, (
        "the work did not happen in a re-exec'd UTF-8-mode child (sentinel=%r utf8_mode=%r): the "
        "test is not exercising reexec_in_utf8_mode. %s" % (rep.get("reexec_sentinel"), rep.get("utf8_mode"), r.diag()))
    assert rep["console_process_count"] == 0, (
        "utf8_io's re-exec child has a console attached: DETACHED_PROCESS is not in effect. " + r.diag())
    for name in ("stdin", "stdout", "stderr"):
        assert rep["os_%s_null" % name] is False and rep["os_%s_filetype" % name] == 3, (
            "re-exec child's %s is not a pipe handle: %s" % (name, r.diag()))


@windows_only
def test_utf8_reexec_carries_stdout_stdin_stderr_and_exit_code(sandbox):
    """Checks 1-4 through utf8_io.reexec_in_utf8_mode: the detached re-exec child's stdout
    (>64 KB, non-ASCII), stdin (>64 KB), stderr (>64 KB) and exit codes 0/2/7 all reach the
    host through the outer process; unlike run.py the re-exec passes the exit code on."""
    _require_reexec_precondition(sandbox)
    sandbox.env = _non_utf8_locale_env(sandbox)
    r = _scenario_stdout_roundtrip(sandbox, target="utf8")
    assert r.report["reexec_sentinel"] == "1", "scenario did not go through the re-exec: " + r.diag()
    _scenario_stdin_roundtrip(sandbox, target="utf8")
    _scenario_stderr(sandbox, target="utf8")
    for code in (0, 2, 7):
        _scenario_exit_code(sandbox, code, target="utf8")
    _scenario_degenerate_parents(sandbox, target="utf8")


# -- spawn_utils ---------------------------------------------------------------
@windows_only
def test_spawn_detached_stdio_and_no_console(sandbox, record_property):
    """spawn_utils.spawn_detached (DETACHED_PROCESS | NEW_PROCESS_GROUP | BREAKAWAY | NO_WINDOW):
    every real caller passes DEVNULL stdio, so the DEVNULL child must run with NO console and
    a NUL std handle; and an explicit PIPE stdout/stderr/stdin must still carry >64 KB intact
    (the helper does not take a channel away)."""
    import spawn_utils

    script = [sys.executable, str(sandbox.hook)]

    # Real callers: stdin/stdout/stderr all DEVNULL, fire and forget.
    sandbox.report.unlink(missing_ok=True)
    child = spawn_utils.spawn_detached(
        [*script, "report"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, env=sandbox.env, cwd=str(sandbox.cwd), close_fds=True)
    record_property("spawn_detached_used_breakaway_fallback", spawn_utils.last_spawn_used_fallback)
    assert child is not None, "spawn_detached returned None on a real Windows runner"
    child.wait(timeout=60)
    rep = sandbox.read_report(key="finished") or {}
    assert rep.get("finished") and child.returncode == 0, "DEVNULL child did not run: %r" % rep
    assert rep["console_process_count"] == 0, "spawn_detached child has a console: %r" % rep
    assert rep["os_stdout_filetype"] == 2 and rep["os_stderr_filetype"] == 2 and rep["os_stdin_filetype"] == 2, (
        "DEVNULL stdio should arrive as the NUL device: %r" % rep)

    # Explicit pipes (no caller does this today; guards a future one).
    payload = make_stdin_payload()
    sandbox.report.unlink(missing_ok=True)
    child = spawn_utils.spawn_detached(
        [*script, "echo"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=sandbox.env, cwd=str(sandbox.cwd))
    assert child is not None
    out, err = child.communicate(input=payload, timeout=120)
    assert child.returncode == 0 and out == payload, (
        "spawn_detached lost bytes on an explicit stdout pipe: rc=%r got %d of %d bytes, stderr=%r"
        % (child.returncode, len(out), len(payload), err[:300]))
    assert (sandbox.read_report(key="finished") or {}).get("console_process_count") == 0
