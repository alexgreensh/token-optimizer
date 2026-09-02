#!/usr/bin/env python3
"""Cross-platform behavioral contract for hook deadlines and lease locks."""

from __future__ import annotations

import importlib
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
MEASURE = SCRIPTS / "measure.py"
sys.path.insert(0, str(SCRIPTS))

from hook_runtime import HookDeadline, LeaseLock  # noqa: E402


def _python(code, *, timeout=3):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SCRIPTS)
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    return result, time.monotonic() - started


@pytest.mark.parametrize("blocker", ["pipe", "socket", "subprocess"])
def test_deadline_hard_exits_blocking_operations_without_platform_guards(blocker):
    setup = {
        "pipe": "r, w = os.pipe(); os.read(r, 1)",
        "socket": "a, b = socket.socketpair(); a.recv(1)",
        "subprocess": (
            "subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(1.5)'], stdout=subprocess.DEVNULL, "
            "stderr=subprocess.DEVNULL).wait()"
        ),
    }[blocker]
    code = f"""
import os, signal, socket, subprocess, sys
sys.modules["fcntl"] = None
if hasattr(signal, "SIGALRM"):
    del signal.SIGALRM
from hook_runtime import HookDeadline
HookDeadline(0.15).start()
{setup}
"""
    result, elapsed = _python(code)
    assert (
        result.returncode == 0
        and 0.08 <= elapsed < 0.9
        and "hook budget exceeded" in result.stderr
    )


def test_normal_completion_cancels_watchdog_without_late_exit():
    code = """
import time
from hook_runtime import HookDeadline
deadline = HookDeadline(0.1).start()
deadline.cancel()
time.sleep(0.2)
print("survived")
"""
    result, _elapsed = _python(code)
    assert result.returncode == 0 and result.stdout.strip() == "survived"
    # A hook that finishes inside its budget must say NOTHING. Emitting the
    # timeout diagnostic at arm time made every normal completion print
    # "budget exceeded; skipping" on stderr - four gauntlet reviewers caught it
    # in the field before any test did.
    assert "budget exceeded" not in result.stderr
    assert "skipping" not in result.stderr


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only nonblocking anonymous-pipe semantics",
)
def test_deadline_exit_is_not_blocked_by_full_stderr_pipe():
    code = """
import os, time
read_fd, write_fd = os.pipe()
os.dup2(write_fd, 2)
os.close(write_fd)
os.set_blocking(2, False)
while True:
    try:
        os.write(2, b"x" * 65536)
    except BlockingIOError:
        break
os.set_blocking(2, True)
from hook_runtime import HookDeadline
HookDeadline(0.15).start()
time.sleep(2)
"""
    result, elapsed = _python(code)
    assert result.returncode == 0 and 0.08 <= elapsed < 0.9


def test_live_pid_does_not_prevent_expired_lease_recovery(tmp_path):
    path = tmp_path / "state.lease"
    now = time.time()
    path.write_text(json.dumps({
        "pid": os.getpid(),
        "nonce": "expired-owner",
        "created_wall": now - 2,
        "expires_wall": now - 1,
    }))
    contender = LeaseLock(path, acquire_timeout=0, reclaim_grace=0)
    acquired = contender.acquire()
    contender.release()
    successor = LeaseLock(path, acquire_timeout=0, reclaim_grace=0)
    succeeded_again = successor.acquire()
    successor.release()
    assert acquired and succeeded_again


def test_dead_pid_does_not_make_an_unexpired_lease_reclaimable(tmp_path):
    path = tmp_path / "state.lease"
    now = time.time()
    record = {
        "pid": 999_999_999,
        "nonce": "unexpired-owner",
        "created_wall": now,
        "expires_wall": now + 30,
    }
    path.write_text(json.dumps(record))
    acquired = LeaseLock(path, acquire_timeout=0, reclaim_grace=0).acquire()
    assert not acquired and json.loads(path.read_text())["nonce"] == record["nonce"]


@pytest.mark.parametrize(
    "record_factory",
    [
        pytest.param(lambda: "{not-json", id="malformed-json"),
        pytest.param(
            lambda: json.dumps({
                "pid": 1,
                "nonce": "future-owner",
                "created_wall": time.time() + 60,
                "expires_wall": time.time() + 61,
            }),
            id="future-owner",
        ),
    ],
)
def test_unassessable_lease_metadata_fails_open_without_reclamation(
    tmp_path, record_factory
):
    record = record_factory()
    path = tmp_path / "state.lease"
    path.write_text(record)
    acquired = LeaseLock(path, acquire_timeout=0, reclaim_grace=0).acquire()
    assert not acquired and path.exists() and path.read_text() == record


@pytest.mark.parametrize("malformed", [b"", b"{partial", b'{"nonce":'])
def test_stable_malformed_lease_is_reclaimable_after_grace(
    tmp_path, malformed
):
    path = tmp_path / "state.lease"
    path.write_bytes(malformed)
    old = time.time() - 2
    os.utime(path, (old, old))
    contender = LeaseLock(path, acquire_timeout=0, reclaim_grace=0.25)

    acquired = contender.acquire()
    contender.release()

    successor = LeaseLock(path, acquire_timeout=0)
    succeeded_again = successor.acquire()
    successor.release()
    assert acquired and succeeded_again


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="adversarial 12-process reclaim of an expired/malformed lease: under "
    "Windows file-locking semantics no contender reliably wins the reclaim "
    "(same known Windows lease-reclaim gap skipped in test_defeat_exit_cleanup.py). "
    "POSIX rename-based reclaim is the guarded invariant.",
)
def test_many_contenders_reclaim_one_malformed_generation_once(tmp_path):
    lock_path = tmp_path / "malformed.lease"
    wins_path = tmp_path / "wins.txt"
    lock_path.write_bytes(b"")
    old = time.time() - 2
    os.utime(lock_path, (old, old))
    code = """
import sys, time
from hook_runtime import LeaseLock
lock = LeaseLock(
    sys.argv[1],
    acquire_timeout=0.075,
    reclaim_grace=0.25,
)
if lock.acquire():
    with open(sys.argv[2], "a", encoding="utf-8") as out:
        out.write("won\\n")
    time.sleep(0.2)
    lock.release()
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SCRIPTS)
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(lock_path), str(wins_path)],
            env=env,
        )
        for _ in range(12)
    ]
    codes = [process.wait(timeout=2) for process in processes]

    assert codes == [0] * len(processes)
    assert wins_path.read_text().splitlines() == ["won"]


def test_lease_metadata_is_complete_before_final_path_is_published(
    tmp_path, monkeypatch
):
    path = tmp_path / "state.lease"
    real_write = os.write

    def checked_write(fd, data):
        if data != b"1":
            assert not path.exists()
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", checked_write)
    lock = LeaseLock(path, acquire_timeout=0)
    acquired = lock.acquire()
    lock.release()

    assert (
        acquired
        and path.exists()
        and json.loads(path.read_text())["released"] == 1
    )


def test_release_never_unlinks_a_different_nonce(tmp_path):
    path = tmp_path / "state.lease"
    owner = LeaseLock(path, acquire_timeout=0)
    acquired = owner.acquire()
    replacement = {
        "pid": os.getpid(),
        "nonce": "replacement-owner",
        "created_wall": time.time(),
        "expires_wall": time.time() + 30,
    }
    path.write_text(json.dumps(replacement))
    owner.release()
    assert (
        acquired
        and path.exists()
        and json.loads(path.read_text())["nonce"] == "replacement-owner"
    )


def test_expired_owner_release_cannot_delete_a_successor_lease(
    tmp_path, monkeypatch
):
    path = tmp_path / "state.lease"
    owner = LeaseLock(
        path, acquire_timeout=0, lease_seconds=0.1, reclaim_grace=0
    )
    assert owner.acquire()
    time.sleep(0.12)
    reclaimer = LeaseLock(path, acquire_timeout=0, reclaim_grace=0)
    successor = LeaseLock(path, acquire_timeout=0, reclaim_grace=0)
    anchor = getattr(owner, "_owner_path", path)
    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes
    raced = False

    def trigger_race(raw):
        nonlocal raced
        if not raced:
            raced = True
            assert reclaimer._reclaim_if_expired()
            assert successor.acquire()
        return raw

    def raced_read_text(self, *args, **kwargs):
        raw = real_read_text(self, *args, **kwargs)
        return trigger_race(raw) if self in (anchor, path) else raw

    def raced_read_bytes(self):
        raw = real_read_bytes(self)
        return trigger_race(raw) if self in (anchor, path) else raw

    monkeypatch.setattr(Path, "read_text", raced_read_text)
    monkeypatch.setattr(Path, "read_bytes", raced_read_bytes)
    owner.release()

    assert raced
    assert json.loads(path.read_text())["nonce"] == successor.nonce
    assert not reclaimer.acquire()
    successor.release()


def test_stale_reclaimer_cannot_move_a_successor_lease(tmp_path, monkeypatch):
    path = tmp_path / "state.lease"
    owner = LeaseLock(
        path, acquire_timeout=0, lease_seconds=0.1, reclaim_grace=0
    )
    assert owner.acquire()
    time.sleep(0.12)

    first = LeaseLock(path, acquire_timeout=0, reclaim_grace=0)
    stale = LeaseLock(path, acquire_timeout=0, reclaim_grace=0)
    stale_observation = stale._read_owner()
    assert stale_observation is not None
    assert first._reclaim_if_expired()

    successor = LeaseLock(path, acquire_timeout=0)
    assert successor.acquire()
    late = LeaseLock(path, acquire_timeout=0)
    real_read_text = Path.read_text
    late_acquired = None

    def raced_read_text(self, *args, **kwargs):
        nonlocal late_acquired
        raw = real_read_text(self, *args, **kwargs)
        if ".reclaim-" in self.name and late_acquired is None:
            late_acquired = late.acquire()
        return raw

    monkeypatch.setattr(stale, "_read_owner", lambda: stale_observation)
    monkeypatch.setattr(Path, "read_text", raced_read_text)

    assert not stale._reclaim_if_expired()
    assert late_acquired is False
    assert json.loads(path.read_text())["nonce"] == successor.nonce

    late.release()
    successor.release()
    owner.release()


def test_acquire_never_retries_after_timeout(tmp_path, monkeypatch):
    lock = LeaseLock(tmp_path / "state.lease", acquire_timeout=0.075)
    now = 0.0
    attempts = 0

    def try_create():
        nonlocal attempts
        attempts += 1
        return attempts > 1

    def advance_past_timeout(_seconds):
        nonlocal now
        now = 0.08

    monkeypatch.setattr(lock, "_try_create", try_create)
    monkeypatch.setattr(lock, "_reclaim_if_expired", lambda: False)
    monkeypatch.setattr(time, "monotonic", lambda: now)
    monkeypatch.setattr(time, "sleep", advance_past_timeout)

    assert not lock.acquire()
    assert attempts == 1


def test_many_contenders_have_one_winner_and_bounded_losers(tmp_path):
    lock_path = tmp_path / "race.lease"
    wins_path = tmp_path / "wins.txt"
    code = """
import sys, time
from hook_runtime import LeaseLock
lock = LeaseLock(sys.argv[1], acquire_timeout=0.075)
if lock.acquire():
    with open(sys.argv[2], "a", encoding="utf-8") as out:
        out.write("won\\n")
    time.sleep(0.2)
    lock.release()
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SCRIPTS)
    started = time.monotonic()
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(lock_path), str(wins_path)],
            env=env,
        )
        for _ in range(12)
    ]
    codes = [process.wait(timeout=2) for process in processes]
    elapsed = time.monotonic() - started
    wins = wins_path.read_text().splitlines()
    assert codes == [0] * 12 and wins == ["won"] and elapsed < 1.2


def test_owner_hard_exit_recovers_only_after_lease_expiry(tmp_path):
    path = tmp_path / "orphan.lease"
    code = """
import os, sys
from hook_runtime import LeaseLock
lock = LeaseLock(sys.argv[1], acquire_timeout=0, lease_seconds=0.25, reclaim_grace=0)
if not lock.acquire():
    raise SystemExit(2)
os._exit(0)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SCRIPTS)
    subprocess.run([sys.executable, "-c", code, str(path)], env=env, check=True)
    immediate = LeaseLock(path, acquire_timeout=0, reclaim_grace=0).acquire()
    time.sleep(0.3)
    recovered_lock = LeaseLock(path, acquire_timeout=0, reclaim_grace=0)
    recovered = recovered_lock.acquire()
    recovered_lock.release()
    assert immediate is False and recovered is True


@pytest.fixture()
def measure_module(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    sys.modules.pop("measure", None)
    module = importlib.import_module("measure")
    module = importlib.reload(module)
    yield module
    sys.modules.pop("measure", None)


def test_windows_simulation_uses_portable_config_and_tripwire_leases(
    monkeypatch, tmp_path
):
    """Every lock site acquires under Windows simulation (fcntl/SIGALRM removed).

    Proves cross-platform parity on this macOS host: no lock silently no-ops
    when fcntl is unavailable. Each ``is True`` assertion fails if its site
    reverts to the old _HAS_FCNTL no-op guard (which yielded None, not True).
    """
    # Simulate Windows: fcntl unavailable, SIGALRM absent.
    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.delattr(signal, "SIGALRM", raising=False)

    # Redirect snapshot dir via env before import.
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snapshots"))

    # Fresh import with fcntl removed — pop cached modules so they re-import
    # under the Windows simulation. Pop hook_runtime too so the bridge
    # re-imports it without fcntl.
    for mod_name in ("measure", "copilot_hook_bridge", "hook_runtime"):
        sys.modules.pop(mod_name, None)

    measure = importlib.import_module("measure")
    copilot_bridge = importlib.import_module("copilot_hook_bridge")

    # Redirect module-level path constants to isolated tmp_path locations.
    config_dir = tmp_path / "config"
    monkeypatch.setattr(measure, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(measure, "_CONFIG_LOCK_PATH", config_dir / ".config.lock")
    monkeypatch.setattr(measure, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(measure, "_CODEX_CONFIG_LOCK_PATH", tmp_path / ".codex-config.lock")
    monkeypatch.setattr(measure, "CLAUDE_DIR", tmp_path / "claude")
    monkeypatch.setattr(measure, "_SETTINGS_LOCK_PATH", tmp_path / ".settings.lock")

    try:
        # 1. _config_lock (already portable)
        with measure._config_lock() as acquired:
            pass
        assert acquired is True

        # 2. _tripwire_lock (already portable)
        with measure._tripwire_lock() as acquired:
            pass
        assert acquired is True

        # 3. _acquire_quality_lock (already portable)
        quality_lock = measure._acquire_quality_lock(tmp_path / "test.qcache")
        assert quality_lock is not False
        assert quality_lock.acquired is True
        quality_lock.release()

        # 4. _codex_config_lock (migrated from fcntl)
        with measure._codex_config_lock() as acquired:
            pass
        assert acquired is True

        # 5. _skill_mgmt_lock (migrated from fcntl)
        with measure._skill_mgmt_lock() as acquired:
            pass
        assert acquired is True

        # 6. _keepwarm_lock (migrated from fcntl)
        with measure._keepwarm_lock() as acquired:
            pass
        assert acquired is True

        # 7. _keepwarm_tripwire_lock (migrated from fcntl)
        with measure._keepwarm_tripwire_lock() as acquired:
            pass
        assert acquired is True

        # 8. _settings_lock (migrated from fcntl)
        with measure._settings_lock() as acquired:
            pass
        assert acquired is True

        # 9. copilot_hook_bridge _session_lock (migrated from fcntl)
        copilot_dir = tmp_path / "copilot"
        with copilot_bridge._session_lock(copilot_dir, "test-sid") as acquired:
            pass
        assert acquired is True

        # Verify lease artifacts exist (guards against yield-True-without-lock
        # mutants). The `is True` assertions above are the primary proof; these
        # artifact checks add a second layer for a few representative sites.
        assert (config_dir / ".config.lease").exists() or any(
            p.name.startswith(".config.lease.candidate-") for p in config_dir.iterdir()
        )
        assert (tmp_path / ".codex-config.lease").exists() or any(
            p.name.startswith(".codex-config.lease.candidate-")
            for p in tmp_path.iterdir()
        )
        # copilot _session_lock publishes "inflight-<sid>.lock" (not .lease)
        # because the lock path is built directly from to_dir, not via
        # .with_suffix(".lease").
        assert (tmp_path / "copilot" / "inflight-test-sid.lock").exists() or any(
            p.name.startswith(".inflight-test-sid.lock.candidate-")
            for p in (tmp_path / "copilot").iterdir()
        )
    finally:
        for mod_name in ("measure", "copilot_hook_bridge", "hook_runtime"):
            sys.modules.pop(mod_name, None)


def test_config_contention_is_bounded_and_skips_mutation(
    measure_module, monkeypatch, tmp_path
):
    module = measure_module
    config_dir = tmp_path / "config"
    config_path = config_dir / "config.json"
    lock_path = config_dir / ".config.lock"
    monkeypatch.setattr(module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(module, "_CONFIG_LOCK_PATH", lock_path)
    holder = LeaseLock(lock_path.with_suffix(".lease"), acquire_timeout=0)
    holder_acquired = holder.acquire()
    started = time.monotonic()
    module._write_config_flag("must_not_write", True)
    elapsed = time.monotonic() - started
    holder.release()
    # Behavioral guarantee (the point of the test): while the lock is held
    # elsewhere, the write is SKIPPED — no mutation, no indefinite block.
    assert holder_acquired and not config_path.exists()
    # Timing is a generous ceiling that only catches a genuine hang: the config
    # lock's acquire_timeout is 75ms, so a correct skip returns fast. The bound is
    # deliberately loose (26x the timeout) so process/FS jitter on a loaded CI
    # runner can't flake it — measuring performance is not this test's job.
    assert elapsed < 2.0, f"config write blocked for {elapsed:.2f}s (should skip fast)"


def test_throttle_only_cache_miss_never_parses_transcript(
    measure_module, monkeypatch, tmp_path
):
    module = measure_module
    transcript = tmp_path / "session-1.jsonl"
    transcript.write_text('{"type":"user"}\n')
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(module, "QUALITY_CACHE_DIR", cache_dir)
    monkeypatch.setattr(
        module,
        "_parse_jsonl_for_quality",
        lambda _path: pytest.fail("cache miss fell through to transcript parsing"),
    )
    result = module.quality_cache(
        session_jsonl=str(transcript),
        pure_time_throttle=True,
        quiet=True,
    )
    assert result is None and not cache_dir.exists()


def test_quality_cache_throttle_markers_are_session_specific(
    measure_module, monkeypatch, tmp_path
):
    module = measure_module
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(module, "QUALITY_CACHE_DIR", cache_dir)
    cache_dir.mkdir()
    session_a = tmp_path / "session-a.jsonl"
    session_b = tmp_path / "session-b.jsonl"
    for session in (session_a, session_b):
        session.write_text("{}\n", encoding="utf-8")
        cache_path = module._quality_cache_path_for(session)
        assert module._write_quality_cache(cache_path, {"score": 100})
    old = time.time() - 300
    os.utime(
        module._quality_cache_throttle_marker(filepath=session_b),
        (old, old),
    )

    assert not module._quality_cache_tick_due(120, session_a)
    assert module._quality_cache_tick_due(120, session_b)


def test_throttle_only_windows_does_not_read_an_open_stdin_pipe(
    measure_module, monkeypatch
):
    """Exercise the native-Windows fast path without needing a Windows runner."""
    module = measure_module

    class _WindowsOS:
        name = "nt"

        def __getattr__(self, name):
            return getattr(os, name)

    monkeypatch.setattr(module, "os", _WindowsOS())
    monkeypatch.setattr(
        module,
        "_read_stdin_hook_input",
        lambda *_args, **_kwargs: pytest.fail("throttle-only read an open Windows stdin pipe"),
    )

    assert module._read_throttle_only_hook_input() == {}


def test_throttle_only_windows_preserves_available_session_identity(
    measure_module, monkeypatch, tmp_path
):
    """The Windows fast path must be prompt and still choose its session marker."""
    module = measure_module

    class _WindowsOS:
        name = "nt"

        def __getattr__(self, name):
            return getattr(os, name)

    session = tmp_path / "session.jsonl"
    monkeypatch.setattr(module, "os", _WindowsOS())
    # Build the fake stdin payload with json.dumps so a Windows session path
    # (with backslashes) is escaped into valid JSON, not raw-concatenated.
    stdin_bytes = json.dumps(
        {"session_id": "windows-session", "transcript_path": str(session)}
    ).encode("utf-8")
    monkeypatch.setattr(
        module,
        "_read_windows_stdin_bytes",
        lambda _max_bytes: stdin_bytes,
    )
    seen = {}
    monkeypatch.setattr(
        module,
        "_quality_cache_tick_due",
        lambda _throttle, path, session_id: seen.update(
            path=path, session_id=session_id
        ) or True,
    )

    payload, due = module._quality_cache_throttle_only_state(120, force=False)
    assert payload["session_id"] == "windows-session"
    assert due is True
    assert seen == {
        "path": str(session),
        "session_id": "windows-session",
    }


def test_all_three_user_prompt_handlers_install_and_clear_budgets():
    source = MEASURE.read_text(encoding="utf-8")
    names = ["prompt-continuity", "verbosity-steer", "quality-cache"]
    blocks = []
    for index, name in enumerate(names):
        start = source.index(f'elif args[0] == "{name}":')
        if index + 1 < len(names):
            end = source.index(f'elif args[0] == "{names[index + 1]}":', start)
        else:
            end = source.index('elif args[0] == "v5":', start)
        blocks.append(source[start:end])
    assert all(
        "_install_hook_budget(" in block and "_clear_hook_budget(" in block
        for block in blocks
    )
