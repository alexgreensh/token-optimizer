#!/usr/bin/env python3
"""Regression tests for hook I/O and runtime resource hardening.

Each test reproduces a bug that was demonstrated live on main before the
hardening landed:

  1. read_stdin_hook_input blocked past its own _STDIN_TIMEOUT on a partial
     payload with a held pipe (select guarantees one byte; the old
     read(max_bytes) blocked until max_bytes or EOF).
  2. A planted reader-less FIFO at module_runner's once-a-day ro-pyc marker
     path hung the hook indefinitely (open(marker, "w") on a FIFO blocks;
     the except cannot interrupt a blocked syscall).
  3. _write_dashboard_meta_atomic leaked tmp_fd when os.fchmod raised between
     mkstemp and fdopen (50 leaked fds in 50 forced-failure calls).
  4. _handler_deadline restored an already-expired outer timer via
     setitimer(0.0), which DISARMS it, silently dropping the outer budget for
     every later handler in the runner.
"""

from __future__ import annotations
from helpers.measure_split import exec_source_module as _split_exec_module

import hashlib
import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
HOOKS = REPO / "hooks"


def _load_module(path: Path, name: str):
    # measure.py imports hook_io from hooks/; other measure-loading tests do
    # the same path prepends.
    for p in (str(SCRIPTS), str(HOOKS)):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    _split_exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# 1. stdin partial payload must not hang past the deadline
# ---------------------------------------------------------------------------

def test_stdin_partial_payload_returns_within_deadline(tmp_path):
    """A host that writes a partial JSON payload and holds the pipe open must
    not hang the hook: the read is bounded by _STDIN_TIMEOUT end to end."""
    code = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import hook_io\n"
        "t0 = time.time()\n"
        "hook_io.read_stdin_hook_input(max_bytes=1_048_576)\n"
        "print('returned after %.2fs' % (time.time() - t0), flush=True)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        proc.stdin.write('{"partial": tru')  # valid-ish start, never completed
        proc.stdin.flush()
        try:
            out, _err = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                pass
            pytest.fail("read_stdin_hook_input hung past 60s on a partial payload "
                        "with the pipe held open (deadline not enforced end to end)")
        assert "returned" in out, f"child crashed instead of timing out: {out!r}"
        # Pin the deadline itself, not just "did not hang": the read must be
        # bounded near _STDIN_TIMEOUT (0.5s), with slack for interpreter
        # startup on slow runners. A reverted blocking read hangs instead.
        # H-7: the old code set the deadline AFTER the first select, so the
        # loop got a full additional _STDIN_TIMEOUT — measured 1098ms peak
        # (2.2x). Assert against the imported constant so a regression that
        # re-introduces the double-budget is a red test.
        sys.path.insert(0, str(SCRIPTS))
        from hook_io import _STDIN_TIMEOUT
        elapsed = float(out.split("after ")[1].split("s")[0])
        assert elapsed < _STDIN_TIMEOUT * 2, (
            f"stdin read took {elapsed:.2f}s, exceeding {_STDIN_TIMEOUT * 2:.2f}s "
            f"(2x the {_STDIN_TIMEOUT}s deadline — the deadline was likely set "
            f"after the first select, giving the loop a full second budget)"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                pass


# --------------------------------------------------------------------------- #
# 2. planted FIFO at the ro-pyc marker path must not hang the hook
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    sys.platform == "win32",
    reason="os.mkfifo is POSIX-only; the planted-FIFO attack shape requires a "
    "filesystem with FIFO semantics.",
)
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root passes os.access(W_OK) on a 0o555 dir, so the read-only "
    "branch (and the marker logic under test) is never reached.",
)
def test_planted_fifo_marker_does_not_hang_the_hook(tmp_path):
    """A reader-less FIFO planted at the marker path (backdated to defeat the
    once-a-day freshness check) must be refused, not block the hook."""
    module_runner = _load_module(HOOKS / "module_runner.py", "module_runner_regr")
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    scripts_dir.chmod(0o555)  # force the read-only-scripts-dir branch
    tag = hashlib.sha1(str(scripts_dir).encode("utf-8", "replace")).hexdigest()[:12]
    marker = Path(tempfile.gettempdir()) / f".token-optimizer-ro-pyc-{tag}"
    try:
        os.mkfifo(marker)
        old = time.time() - 172800  # backdate 2 days: defeat the freshness check
        os.utime(marker, (old, old))
        done = threading.Event()
        thread = threading.Thread(
            target=lambda: (module_runner._warn_readonly_scripts_dir_once(str(scripts_dir)),
                            done.set()),
            daemon=True,
        )
        thread.start()
        thread.join(timeout=60)
        assert done.is_set(), ("hook hung >=5s on a planted FIFO marker; the marker write "
                               "must be exclusive and non-blocking (fail-open)")
    finally:
        scripts_dir.chmod(0o755)
        if thread.is_alive() and marker.exists():
            # Rescue reader: release a worker still blocked opening the FIFO
            # for write, so nothing outlives the test.
            try:
                os.close(os.open(marker, os.O_RDONLY | os.O_NONBLOCK))
            except OSError:
                pass
            thread.join(timeout=60)
        try:
            marker.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 3. interrupted atomic write must not leak the mkstemp fd
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    sys.platform == "win32",
    reason="os.fchmod is absent on Windows, so the leak window between mkstemp "
    "and fdopen cannot be forced there.",
)
def test_dashboard_meta_write_does_not_leak_fd_on_fchmod_failure(tmp_path, monkeypatch):
    """Forcing os.fchmod to raise between mkstemp and fdopen must not leak the
    descriptor: 50 failed writes used to leak 50 fds."""
    measure = _load_module(SCRIPTS / "measure.py", "measure_regr")
    meta_path = tmp_path / "meta.json"

    def fd_count():
        with os.scandir("/dev/fd") as entries:
            return sum(1 for _ in entries)

    def forced_fchmod_failure(*_args, **_kwargs):
        raise OSError("forced fchmod failure")

    monkeypatch.setattr(os, "fchmod", forced_fchmod_failure)
    before = fd_count()
    for _ in range(50):
        measure._write_dashboard_meta_atomic(meta_path)  # fail-soft, never raises
    after = fd_count()
    assert after - before == 0, (
        f"interrupted atomic writes leaked {after - before} fds "
        "(tmp_fd not closed when fchmod raises before fdopen owns it)"
    )


# --------------------------------------------------------------------------- #
# 4. an already-expired outer timer must be delivered, not disarmed
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    sys.platform == "win32",
    reason="setitimer/SIGALRM are POSIX-only; the nested-budget timer restore "
    "under test does not exist on Windows.",
)
def test_expired_outer_budget_is_delivered_not_disarmed():
    """When the inner budget context exits after the outer deadline already
    elapsed, the outer budget must fire (via the restored handler), not be
    silently disarmed by setitimer(0.0)."""
    ptr = _load_module(HOOKS / "posttooluse_runner.py", "ptr_regr")

    events = []

    def outer_handler(_signum, _frame):
        events.append("outer-fired")
        raise ptr._HandlerBudgetExceeded(1.0)

    # _handler_deadline short-circuits to a bare yield while _RUNNER_DEADLINE
    # is None (it is only armed by main()); a dummy object forces the real
    # save/restore branch this test pins.
    ptr._RUNNER_DEADLINE = object()
    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, outer_handler)
    signal.setitimer(signal.ITIMER_REAL, 1.0)
    try:
        try:
            with ptr._handler_deadline(5.0):
                time.sleep(2.0)  # outer expires mid-inner; inner masks the signal
            events.append("inner-exited-cleanly")
        except ptr._HandlerBudgetExceeded:
            events.append("budget-exceeded")
        remaining = signal.getitimer(signal.ITIMER_REAL)[0]
        # The outer budget must have been DELIVERED (its handler ran via the
        # fix's os.kill re-delivery, raising its own budget exception), not
        # silently disarmed for later handlers. Pre-fix, the inner handler
        # masks the outer SIGALRM and the finally disarms it, so "outer-fired"
        # never appears.
        assert "outer-fired" in events and "budget-exceeded" in events, (
            f"expired outer budget was disarmed instead of delivered: {events}; "
            f"outer timer remaining after inner exit: {remaining:.3f}s"
        )
    finally:
        ptr._RUNNER_DEADLINE = None
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_stdin_payload_still_streaming_after_the_first_half_second_is_read_whole():
    """A big tool result can take longer than _STDIN_TIMEOUT to arrive on a
    loaded machine. As long as bytes keep coming, the read must wait for all of
    them; a payload cut at 0.5 s is invalid JSON and the hook silently did
    nothing (archive_result lost 3-5 MB results this way)."""
    import json as _json
    import time as _time
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import hook_io\n"
        "got = hook_io.read_stdin_hook_input(max_bytes=5_242_880)\n"
        "print(json.dumps({'keys': sorted(got), 'n': len(got.get('tool_response', ''))}), flush=True)\n"
    )
    payload = _json.dumps({"tool_name": "Bash", "tool_response": "x" * 600_000}).encode()
    proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        step = len(payload) // 12 + 1
        try:
            for i in range(0, len(payload), step):  # ~1.2 s of steady streaming
                proc.stdin.write(payload[i:i + step])
                proc.stdin.flush()
                _time.sleep(0.1)
            proc.stdin.close()
        except BrokenPipeError:
            pass  # the reader gave up mid-stream; the assert below says so
        # Not communicate(): it flushes the stdin we already closed, which
        # raises on Python 3.11/3.12.
        proc.wait(timeout=60)
        out, err = proc.stdout.read(), proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
    result = _json.loads(out.decode())
    assert result == {"keys": ["tool_name", "tool_response"], "n": 600_000}, err.decode()[-1500:]
