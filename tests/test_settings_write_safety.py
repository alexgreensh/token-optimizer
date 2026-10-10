#!/usr/bin/env python3
"""Settings write safety: concurrent edits, non-regular files, undecodable bytes,
OS errors on the replace, refusal reasons, new-file modes, stale-lock reclaim.

Run: python3 -m pytest tests/test_settings_write_safety.py -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

BASE = {
    "model": "opus",
    "effortLevel": "high",
    "voice": "alloy",
    "env": {"MY_KEY": "keep-me", "OTHER": "1"},
    "permissions": {"allow": ["Bash(ls:*)"]},
}


@pytest.fixture()
def measure(tmp_path, monkeypatch):
    """Load measure.py against a throwaway CLAUDE_DIR. Never touches ~/.claude."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    spec.loader.exec_module(mod)

    home = tmp_path / "claude"
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    settings = home / "settings.json"
    settings.write_text(json.dumps(BASE, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setattr(mod, "SETTINGS_PATH", settings)
    monkeypatch.setattr(mod, "_SETTINGS_LOCK_PATH", home / ".settings.lock")
    monkeypatch.setattr(mod, "CLAUDE_DIR", home)
    yield mod, settings
    sys.modules.pop("measure", None)


def _read(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _inject_after_guard(mod, monkeypatch, edits):
    """Run each callable in ``edits`` right after one guard call returns.

    The guard runs after the merge's read and before os.replace, which is the
    narrowest window a real editor can hit (the merge-read-to-replace gap).
    """
    real = mod._settings_write_guard
    pending = list(edits)

    def wrapped(*args, **kwargs):
        result = real(*args, **kwargs)
        if pending:
            pending.pop(0)()
        return result

    monkeypatch.setattr(mod, "_settings_write_guard", wrapped)


# ---------------------------------------------------------------------------
# F1: an edit that lands between the merge read and os.replace is re-merged
# ---------------------------------------------------------------------------

def _human_save(settings, mutate):
    def go():
        data = _read(settings)
        mutate(data)
        settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return go


@pytest.mark.parametrize("name,mutate,check", [
    ("value_edit",
     lambda d: d.update(model="claude-human-picked"),
     lambda d: d["model"] == "claude-human-picked"),
    ("key_delete",
     lambda d: d.pop("voice"),
     lambda d: "voice" not in d),
    ("env_delete",
     lambda d: d["env"].pop("OTHER"),
     lambda d: "OTHER" not in d["env"]),
    ("env_to_string",
     lambda d: d.update(env="oops"),
     lambda d: d["env"] == "oops" or d["env"].get("TO_VAR") == "ours"),
])
def test_edit_between_merge_read_and_replace_is_remerged(measure, monkeypatch, name, mutate, check):
    mod, settings = measure
    stale, ok = mod._read_settings_for_write()
    assert ok
    stale["effortLevel"] = "low"  # our change
    # A concurrent edit that lands in the file between the unrelated edit the
    # merge already saw and the replace. Force a first merge by making an
    # earlier edit, so the injected one is NOT visible to the merge read.
    settings.write_text(json.dumps({**BASE, "cleanupPeriodDays": 5}, indent=2) + "\n", encoding="utf-8")
    _inject_after_guard(mod, monkeypatch, [_human_save(settings, mutate)])

    assert mod._write_settings_atomic(stale) is True
    on_disk = _read(settings)
    assert check(on_disk), f"{name}: the concurrent edit was reverted: {on_disk}"
    assert on_disk["effortLevel"] == "low", "our own change was lost"
    assert on_disk["cleanupPeriodDays"] == 5


def test_edit_in_window_when_merge_saw_an_unchanged_file(measure, monkeypatch):
    mod, settings = measure
    stale, ok = mod._read_settings_for_write()
    assert ok
    stale["effortLevel"] = "low"
    _inject_after_guard(mod, monkeypatch, [_human_save(settings, lambda d: d.update(model="human"))])

    assert mod._write_settings_atomic(stale) is True
    on_disk = _read(settings)
    assert on_disk["model"] == "human"
    assert on_disk["effortLevel"] == "low"


def test_two_changes_in_a_row_refuse_with_a_reason_and_keep_the_last_edit(measure, monkeypatch):
    mod, settings = measure
    stale, ok = mod._read_settings_for_write()
    assert ok
    stale["effortLevel"] = "low"
    _inject_after_guard(mod, monkeypatch, [
        _human_save(settings, lambda d: d.update(model="human-1")),
        _human_save(settings, lambda d: d.update(model="human-22")),
    ])

    assert mod._write_settings_atomic(stale) is False
    assert _read(settings)["model"] == "human-22", "the last human edit was reverted"
    reason = getattr(mod._SETTINGS_WRITE_READ_STATE, "last_refusal", "")
    assert "changed" in reason and "settings.json" in reason, reason
    leftovers = [p.name for p in settings.parent.iterdir() if p.name.startswith(".settings-")]
    assert leftovers == [], "temp file left behind"


# ---------------------------------------------------------------------------
# F2: a non-regular file at settings.json (or any JSON these readers open)
# must never block a reader. Real FIFOs, a daemon thread, and a join timeout,
# so a regression fails the test instead of hanging the suite.
# ---------------------------------------------------------------------------

needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")


def _call_with_fifo(fifo, fn, timeout=6):
    """Run ``fn()`` on a thread. Fail (and free the thread) if it blocks."""
    out = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as exc:  # SystemExit included
            out["exc"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        try:  # unblock the reader so the daemon thread can exit
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.write(fd, b"{}")
            os.close(fd)
        except OSError:
            pass
        t.join(2)
        pytest.fail("a reader blocked on a FIFO")
    return out


@pytest.fixture()
def fifo_settings(measure):
    mod, settings = measure
    if not hasattr(os, "mkfifo"):
        pytest.skip("needs POSIX FIFOs")
    settings.unlink()
    os.mkfifo(settings)
    return mod, settings


@needs_fifo
def test_keepwarm_reader_skips_a_fifo(fifo_settings):
    mod, settings = fifo_settings
    out = _call_with_fifo(settings, lambda: mod._keepwarm_json_says_api(settings))
    assert out.get("value", "missing") is None, out


@needs_fifo
def test_mcp_reader_skips_a_fifo_and_records_it(fifo_settings):
    mod, settings = fifo_settings
    skipped = []
    out = _call_with_fifo(settings, lambda: mod._mcp_read_json(settings, skipped))
    assert out.get("value", "missing") is None, out
    assert skipped and skipped[0]["path"] == str(settings)


@needs_fifo
def test_keepwarm_reader_skips_a_directory(measure):
    mod, settings = measure
    settings.unlink()
    settings.mkdir()
    assert mod._keepwarm_json_says_api(settings) is None
    skipped = []
    assert mod._mcp_read_json(settings, skipped) is None


@needs_fifo
def test_hook_state_readers_skip_a_fifo(fifo_settings):
    mod, settings = fifo_settings
    out = _call_with_fifo(settings, lambda: (mod._is_hook_installed(), mod._is_hook_current()))
    assert out.get("value") == (False, False), out


@needs_fifo
def test_setup_hook_refuses_a_fifo_without_blocking(fifo_settings, capsys):
    mod, settings = fifo_settings
    out = _call_with_fifo(settings, lambda: mod.setup_hook(dry_run=True))
    assert isinstance(out.get("exc"), SystemExit), out
    assert "regular file" in capsys.readouterr().out


@needs_fifo
def test_plugin_cleanup_skips_a_fifo(fifo_settings):
    mod, settings = fifo_settings
    (settings.parent / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {}}), encoding="utf-8")
    out = _call_with_fifo(settings, lambda: mod.plugin_cleanup(dry_run=True, quiet=True))
    assert "exc" not in out, out


def _sandbox_env(tmp_path, claude_dir):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE", "TOKEN_OPTIMIZER", "CODEX", "HERMES"))}
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(tmp_path / "data")
    return env


@needs_fifo
@pytest.mark.parametrize("command", [["quality-cache", "--quiet"], ["ensure-health", "--quiet"]])
def test_hook_commands_do_not_hang_on_a_fifo(tmp_path, command):
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    fifo = claude_dir / "settings.json"
    os.mkfifo(fifo)
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPTS / "measure.py"), *command],
        env=_sandbox_env(tmp_path, claude_dir), cwd=str(tmp_path),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        out, _ = proc.communicate(timeout=45)
        # The hook budget kills a stuck reader at ~8 s and prints this line;
        # a reader that never blocks finishes well before that.
        assert b"hook budget exceeded" not in out, out.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        try:  # free the blocked reader, then kill only the pid we started
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.write(fd, b"{}")
            os.close(fd)
        except OSError:
            pass
        proc.kill()
        proc.communicate()
        pytest.fail(f"measure.py {' '.join(command)} hung on a FIFO settings.json")


@needs_fifo
def test_quality_cache_self_heal_in_the_gate_and_the_runner_skip_a_fifo(fifo_settings, tmp_path, monkeypatch):
    mod, settings = fifo_settings
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(mod, "CONFIG_PATH", cfg)
    monkeypatch.setattr(mod, "_is_running_from_plugin_cache", lambda: False)
    monkeypatch.setattr(mod, "_is_plugin_installed", lambda: False)

    spec = importlib.util.spec_from_file_location("qc_gate_safety", SCRIPTS / "quality_cache_gate.py")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    out = _call_with_fifo(settings, lambda: gate._quality_cache_self_heal(mod))
    assert "exc" not in out, out

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    spec = importlib.util.spec_from_file_location(
        "ptu_runner_safety", REPO / "hooks" / "posttooluse_runner.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setattr(runner, "_measure", lambda: mod)
    out = _call_with_fifo(settings, runner._quality_cache_self_heal)
    assert "exc" not in out, out


# ---------------------------------------------------------------------------
# F4: undecodable bytes in settings.json are "unknown", not a traceback
# ---------------------------------------------------------------------------

def test_non_utf8_settings_reads_as_unknown(measure, monkeypatch):
    mod, settings = measure
    settings.write_bytes(b'{"note": "caf\xe9"}')  # cp1252, what an ANSI editor saves
    data, path, ok = mod._read_settings_json_checked()
    assert ok is False and data == {}
    assert mod._read_settings_for_write() == ({}, False)

    monkeypatch.setattr(mod, "_subagent_cache_claude_only", lambda: True)
    result = mod.subagent_cache_enable(automatic=False)
    assert result["state"] == "unknown-settings"
    assert settings.read_bytes() == b'{"note": "caf\xe9"}', "the undecodable file was changed"


# ---------------------------------------------------------------------------
# F6: an OSError from the temp write or os.replace is a refusal with a reason
# ---------------------------------------------------------------------------

def _enable_ready(mod, monkeypatch, tmp_path):
    """Make an explicit `subagent-cache enable` reach the write without history."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mod, "_subagent_cache_claude_only", lambda: True)
    monkeypatch.setattr(mod, "_subagent_cache_claude_code_version", lambda: (2, 1, 300))
    monkeypatch.setattr(mod, "_subagent_cache_external_key_holder", lambda: None)
    monkeypatch.setattr(mod, "subagent_cache_payoff", lambda days=30, **kw: mod._subagent_cache_payoff_zero(days))
    monkeypatch.setattr(mod, "keepwarm_billing_mode", lambda: "subscription")


def _fail_replace_for_settings(mod, monkeypatch, exc):
    real = os.replace

    def replace(src, dst, *a, **kw):
        if Path(dst).name == "settings.json":
            raise exc
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", replace)


def test_replace_failure_is_a_refusal_with_a_reason(measure, monkeypatch):
    mod, settings = measure
    before = settings.read_bytes()
    _fail_replace_for_settings(mod, monkeypatch, PermissionError(13, "sharing violation"))
    payload = _read(settings)
    payload["effortLevel"] = "low"

    assert mod._write_settings_atomic(payload) is False
    reason = mod._SETTINGS_WRITE_READ_STATE.last_refusal
    assert "PermissionError" in reason and "settings.json" in reason, reason
    assert settings.read_bytes() == before
    assert [p.name for p in settings.parent.iterdir() if p.name.startswith(".settings-")] == []


def test_temp_write_failure_is_a_refusal_with_a_reason(measure, monkeypatch):
    mod, settings = measure
    monkeypatch.setattr(mod.tempfile, "mkstemp",
                        lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    assert mod._write_settings_atomic({**BASE, "effortLevel": "low"}) is False
    assert "No space left" in mod._SETTINGS_WRITE_READ_STATE.last_refusal


def test_cli_enable_prints_write_refused_with_the_reason(measure, monkeypatch, tmp_path, capsys):
    mod, settings = measure
    _enable_ready(mod, monkeypatch, tmp_path)
    _fail_replace_for_settings(mod, monkeypatch, PermissionError(13, "sharing violation"))

    mod._subagent_cache_cli(["subagent-cache", "enable"])  # must not raise
    out = capsys.readouterr().out
    line = [l for l in out.splitlines() if "write-refused" in l]
    assert line, out
    assert "PermissionError" in line[0], line[0]
    assert "Traceback" not in out


def test_cli_reports_the_real_refusal_not_a_fixed_string(measure, monkeypatch, tmp_path, capsys):
    """A guard refusal (read-only file) names itself, in enable and in disable."""
    mod, settings = measure
    _enable_ready(mod, monkeypatch, tmp_path)
    settings.chmod(0o444)
    try:
        mod._subagent_cache_cli(["subagent-cache", "enable"])
    finally:
        settings.chmod(0o644)
    out = capsys.readouterr().out
    line = [l for l in out.splitlines() if "write-refused" in l]
    assert line, out
    assert "read-only" in line[0] or "no write bits" in line[0], line[0]
    assert "locked or guard refused" not in out


def test_lease_denied_reason_is_reported(measure, monkeypatch, tmp_path):
    mod, settings = measure
    _enable_ready(mod, monkeypatch, tmp_path)
    from contextlib import contextmanager

    @contextmanager
    def denied(user_initiated=False):
        yield False

    monkeypatch.setattr(mod, "_settings_lock", denied)
    result = mod.subagent_cache_enable(automatic=False)
    assert result["state"] == "write-refused"
    assert "lease denied" in result["reason"], result


# ---------------------------------------------------------------------------
# F10c: a settings.json created from nothing follows the umask, not mkstemp's 0600
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.parametrize("umask,expected", [(0o022, 0o644), (0o077, 0o600), (0o002, 0o664)])
def test_new_settings_file_gets_the_umask_mode(measure, umask, expected):
    mod, settings = measure
    settings.unlink()
    old = os.umask(umask)
    try:
        assert mod._write_settings_atomic({"effortLevel": "low"}) is True
    finally:
        os.umask(old)
    assert stat.S_IMODE(settings.stat().st_mode) == expected


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_existing_settings_keep_their_mode(measure):
    mod, settings = measure
    settings.chmod(0o640)
    payload = _read(settings)
    payload["effortLevel"] = "low"
    assert mod._write_settings_atomic(payload) is True
    assert stat.S_IMODE(settings.stat().st_mode) == 0o640


# ---------------------------------------------------------------------------
# F10e: reclaiming a stale lock must never delete a live successor's lock
# ---------------------------------------------------------------------------

def _stale_lock_race(mod, monkeypatch, lock_path, stale_seconds, acquire):
    """Another process reclaims and re-creates the lock right after our age check."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("crashed-holder", encoding="ascii")
    old = __import__("time").time() - stale_seconds - 60
    os.utime(lock_path, (old, old))
    real_stat = Path.stat
    state = {"raced": False}

    def stat(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        if self == lock_path and not state["raced"]:
            state["raced"] = True
            os.unlink(lock_path)
            lock_path.write_text("live-holder-A", encoding="ascii")
        return result

    monkeypatch.setattr(Path, "stat", stat)
    try:
        token = acquire()
    finally:
        monkeypatch.setattr(Path, "stat", real_stat)
    assert state["raced"], "the race was never injected"
    assert token is None, "a second process took a lock that was live"
    assert lock_path.read_text(encoding="ascii") == "live-holder-A", "the live lock was deleted"
    assert [p.name for p in lock_path.parent.iterdir() if ".stale-" in p.name] == []


def _stale_lock_plain(lock_path, stale_seconds, acquire):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("crashed-holder", encoding="ascii")
    old = __import__("time").time() - stale_seconds - 60
    os.utime(lock_path, (old, old))
    token = acquire()
    assert token, "a genuinely stale lock was not reclaimed"
    assert lock_path.read_text(encoding="ascii") == token
    assert [p.name for p in lock_path.parent.iterdir() if ".stale-" in p.name] == []


def test_recs_lock_reclaim_does_not_delete_a_live_successor(measure, monkeypatch):
    mod, _ = measure
    _stale_lock_race(mod, monkeypatch, mod._recs_lock_path(), mod._RECS_LOCK_STALE,
                     mod._recs_lock_acquire)


def test_recs_lock_reclaims_a_genuinely_stale_lock(measure):
    mod, _ = measure
    _stale_lock_plain(mod._recs_lock_path(), mod._RECS_LOCK_STALE, mod._recs_lock_acquire)


def test_scan_lock_reclaim_does_not_delete_a_live_successor(measure, monkeypatch):
    mod, _ = measure
    _stale_lock_race(mod, monkeypatch, mod._subagent_cache_scan_lock_path(),
                     mod._SUBAGENT_CACHE_SCAN_LOCK_STALE, mod._subagent_cache_scan_acquire_lock)


def test_scan_lock_reclaims_a_genuinely_stale_lock(measure):
    mod, _ = measure
    _stale_lock_plain(mod._subagent_cache_scan_lock_path(), mod._SUBAGENT_CACHE_SCAN_LOCK_STALE,
                      mod._subagent_cache_scan_acquire_lock)


def test_lease_reclaim_does_not_unlink_a_successor(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import hook_runtime

    path = tmp_path / "lease"
    first = hook_runtime.LeaseLock(path, acquire_timeout=0, lease_seconds=0.1, reclaim_grace=0.0)
    assert first.acquire() is True
    __import__("time").sleep(0.25)  # the lease expires; its holder is gone

    real_unlink, real_rename = os.unlink, os.rename
    state = {"raced": False}

    def inject_successor():
        state["raced"] = True
        real_unlink(path)
        path.write_text("successor", encoding="utf-8")

    def unlink(target, *a, **k):
        if Path(target) == path and not state["raced"]:
            inject_successor()
        return real_unlink(target, *a, **k)

    def rename(src, dst, *a, **k):
        if Path(src) == path and not state["raced"]:
            inject_successor()
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(os, "rename", rename)
    second = hook_runtime.LeaseLock(path, acquire_timeout=0, reclaim_grace=0.0)
    got = second.acquire()
    monkeypatch.setattr(os, "unlink", real_unlink)
    monkeypatch.setattr(os, "rename", real_rename)
    assert state["raced"], "the race was never injected"
    assert got is False
    assert path.read_text(encoding="utf-8") == "successor", "the successor's lease was deleted"
    assert [p.name for p in tmp_path.iterdir() if ".stale-" in p.name] == []
    first.release()


# ---------------------------------------------------------------------------
# Line endings: a write keeps the file's dominant ending; a new file uses LF
# ---------------------------------------------------------------------------

def _fixture_bytes(eol: bytes, lines=None) -> bytes:
    body = [b"{", b'  "model": "opus",', b'  "effortLevel": "high"', b"}"]
    if lines is not None:
        body = lines
    return eol.join(body) + eol


def _write_low_effort(mod, settings):
    data = _read(settings)
    data["effortLevel"] = "low"
    assert mod._write_settings_atomic(data) is True
    return settings.read_bytes()


def test_crlf_file_stays_crlf(measure):
    mod, settings = measure
    settings.write_bytes(_fixture_bytes(b"\r\n"))
    raw = _write_low_effort(mod, settings)
    assert raw.count(b"\n") == raw.count(b"\r\n") > 0, raw
    assert raw.endswith(b"}\r\n")
    assert json.loads(raw)["effortLevel"] == "low"


def test_lf_file_stays_lf(measure):
    mod, settings = measure
    settings.write_bytes(_fixture_bytes(b"\n"))
    raw = _write_low_effort(mod, settings)
    assert b"\r" not in raw and raw.endswith(b"}\n"), raw


def test_mixed_file_follows_the_dominant_ending(measure):
    mod, settings = measure
    settings.write_bytes(b'{\r\n  "model": "opus",\r\n  "voice": "alloy",\n  "effortLevel": "high"\r\n}\r\n')
    raw = _write_low_effort(mod, settings)
    assert raw.count(b"\n") == raw.count(b"\r\n"), raw
    settings.write_bytes(b'{\n  "model": "opus",\n  "voice": "alloy",\r\n  "effortLevel": "high"\n}\n')
    raw = _write_low_effort(mod, settings)
    assert b"\r" not in raw, raw


def test_new_file_uses_lf(measure):
    mod, settings = measure
    settings.unlink()
    assert mod._write_settings_atomic({"effortLevel": "low"}) is True
    raw = settings.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")


def test_single_line_file_has_no_ending_to_copy(measure):
    mod, settings = measure
    settings.write_bytes(b'{"model": "opus", "effortLevel": "high"}')
    raw = _write_low_effort(mod, settings)
    assert b"\r" not in raw


def test_temp_file_is_opened_without_newline_translation(measure, monkeypatch):
    """On Windows text mode turns every \\n into \\r\\n; newline='' turns that off.

    POSIX never translates, so assert the open call itself (the only way to pin
    the Windows behavior from a POSIX runner).
    """
    mod, settings = measure
    seen = []
    real = os.fdopen

    def spy(fd, *args, **kwargs):
        seen.append(kwargs.get("newline", "<default>"))
        return real(fd, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", spy)
    settings.write_bytes(_fixture_bytes(b"\n"))
    _write_low_effort(mod, settings)
    assert seen == [""], seen
