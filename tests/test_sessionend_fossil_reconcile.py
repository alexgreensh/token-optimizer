"""Layer 2: rename-aware settings.json fossil reconcile.

A collect && dashboard SessionEnd hook fossilized in ~/.claude/settings.json
is never rewritten by exact-identity dedup (measure.py:collect !=
measure.py:session-end-flush). The reconcile must rewrite or remove that
shape and leave unrelated hooks alone.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# The fossil reconcile touches only Token Optimizer's own entries, so these
# carry a real install path (this checkout's measure.py), exactly as
# setup_hook wrote them. A user's own measure.py elsewhere is left alone
# (tests/test_hook_provenance_r4.py).
MP = str((SCRIPTS / "measure.py").resolve())
FOSSIL = (
    f"python3 '{MP}' collect --quiet && "
    f"python3 '{MP}' dashboard --quiet"
)
CURRENT = f"python3 '{MP}' session-end-flush --trigger end"
UNRELATED = "echo user-own-keep-me"


def _settings(session_end_hooks, stop_hooks=None):
    hooks = {"SessionEnd": [{"hooks": session_end_hooks}]}
    if stop_hooks is not None:
        hooks["Stop"] = [{"hooks": stop_hooks}]
    hooks["PreCompact"] = [{"hooks": [{"type": "command", "command": UNRELATED}]}]
    return {"hooks": hooks}


def _hook(cmd, **extra):
    entry = {"type": "command", "command": cmd}
    entry.update(extra)
    return entry


@pytest.fixture()
def measure_mod():
    import measure
    return measure


def test_reconcile_function_exists(measure_mod):
    assert hasattr(measure_mod, "_reconcile_sessionend_fossils"), (
        "missing _reconcile_sessionend_fossils — Layer 2 was never implemented"
    )


def test_rewrite_fossil_without_async(measure_mod, tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    payload = _settings([_hook(FOSSIL)])
    settings_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(measure_mod, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(measure_mod, "CLAUDE_DIR", tmp_path)

    result = measure_mod._reconcile_sessionend_fossils()
    assert result.get("rewritten", 0) >= 1 or result.get("removed", 0) >= 1

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    se_cmds = [
        h.get("command", "")
        for g in data["hooks"]["SessionEnd"]
        for h in g.get("hooks", [])
        if isinstance(h, dict)
    ]
    assert any("session-end-flush" in c and "--trigger" in c and "end" in c for c in se_cmds)
    assert not any("collect --quiet &&" in c for c in se_cmds)
    rewritten = next(h for g in data["hooks"]["SessionEnd"] for h in g["hooks"] if "session-end-flush" in h["command"])
    assert rewritten.get("async") is True
    assert data["hooks"]["PreCompact"][0]["hooks"][0]["command"] == UNRELATED


def test_rewrite_fossil_preserves_existing_async_and_timeout(measure_mod, tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    payload = _settings([_hook(FOSSIL, **{"async": True, "timeout": 45})])
    settings_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(measure_mod, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(measure_mod, "CLAUDE_DIR", tmp_path)

    measure_mod._reconcile_sessionend_fossils()
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    rewritten = next(
        h
        for g in data["hooks"]["SessionEnd"]
        for h in g["hooks"]
        if "session-end-flush" in h["command"]
    )
    assert rewritten.get("async") is True
    assert rewritten.get("timeout") == 45


def test_remove_fossil_when_current_shape_already_present(measure_mod, tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    payload = _settings([_hook(FOSSIL), _hook(CURRENT, **{"async": True})])
    settings_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(measure_mod, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(measure_mod, "CLAUDE_DIR", tmp_path)

    result = measure_mod._reconcile_sessionend_fossils()
    assert result.get("removed", 0) >= 1

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    se_cmds = [
        h.get("command", "")
        for g in data["hooks"]["SessionEnd"]
        for h in g.get("hooks", [])
        if isinstance(h, dict)
    ]
    assert se_cmds == [CURRENT]
    assert data["hooks"]["PreCompact"][0]["hooks"][0]["command"] == UNRELATED


def test_remove_legacy_stop_duplicate_when_plugin_provides_it(measure_mod, tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    stop_fossil = f"python3 '{MP}' collect --quiet"
    stop_flush = f"python3 '{MP}' session-end-flush --trigger stop --quiet"
    payload = _settings(
        [_hook(CURRENT, **{"async": True})],
        stop_hooks=[_hook(stop_fossil), _hook(stop_flush), _hook(UNRELATED)],
    )
    settings_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(measure_mod, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(measure_mod, "CLAUDE_DIR", tmp_path)
    monkeypatch.setattr(measure_mod, "_is_plugin_installed", lambda: True)

    result = measure_mod._reconcile_sessionend_fossils()
    assert result.get("stop_removed", 0) >= 1 or result.get("removed", 0) >= 1

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    stop_cmds = [
        h.get("command", "")
        for g in data["hooks"].get("Stop", [])
        for h in g.get("hooks", [])
        if isinstance(h, dict)
    ]
    assert UNRELATED in stop_cmds
    assert not any("collect --quiet" in c and "session-end-flush" not in c for c in stop_cmds)


def test_is_hook_current_rejects_collect_shape(measure_mod):
    fossil_settings = {"hooks": {"SessionEnd": [{"hooks": [_hook(FOSSIL)]}]}}
    current_settings = {"hooks": {"SessionEnd": [{"hooks": [_hook(CURRENT)]}]}}
    assert measure_mod._is_hook_current(fossil_settings) is False
    assert measure_mod._is_hook_current(current_settings) is True


# --- Safety branches of _reconcile_sessionend_fossils ---------------------
# The reconcile runs unattended from a SessionStart hook, so its failure modes
# must be fail-open and non-destructive. These cover the branches the original
# test suite never exercised: unreadable settings, top-level exception, no-op,
# and the false-positive guard for a non-token-optimizer hook sharing a
# SessionEnd group with a fossil.


def _backup_dir(tmp_path):
    return tmp_path / "_backups" / "token-optimizer"


def test_reconcile_malformed_settings_is_unreadable_and_does_not_write(measure_mod, tmp_path, monkeypatch):
    """(a) A malformed settings.json -> reason 'settings_unreadable', and the
    file is left byte-unchanged. Round-tripping an unknown-state {} would
    destroy every key the user has, so the reconcile must refuse to write.
    """
    settings_path = tmp_path / "settings.json"
    malformed = b"{ this is not ,,, valid json )))"
    settings_path.write_bytes(malformed)
    monkeypatch.setattr(measure_mod, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(measure_mod, "CLAUDE_DIR", tmp_path)

    result = measure_mod._reconcile_sessionend_fossils()

    assert result["reason"] == "settings_unreadable", result
    assert settings_path.read_bytes() == malformed, "malformed settings.json was mutated"
    assert not _backup_dir(tmp_path).exists(), "no backup should be written for an unreadable file"


def test_reconcile_top_level_exception_is_fail_open_and_never_raises(measure_mod, tmp_path, monkeypatch):
    """(b) A top-level exception -> reason 'fail_open', and the call never
    raises. The reconcile runs inside a hook; an unhandled exception would
    break the SessionStart budget. Fail-open leaves the fossil in place
    (recoverable) instead of risking a destructive write.
    """
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(_settings([_hook(FOSSIL)])), encoding="utf-8")
    monkeypatch.setattr(measure_mod, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(measure_mod, "CLAUDE_DIR", tmp_path)

    def _boom(*_a, **_k):
        raise RuntimeError("simulated read failure")

    monkeypatch.setattr(measure_mod, "_read_settings_json_checked", _boom)

    # Must not raise.
    result = measure_mod._reconcile_sessionend_fossils()

    assert result["reason"] == "fail_open", result
    # The fossil file is untouched (the exception fired before any write).
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    se_cmds = [
        h.get("command", "")
        for g in data["hooks"]["SessionEnd"]
        for h in g.get("hooks", [])
        if isinstance(h, dict)
    ]
    assert FOSSIL in se_cmds


def test_reconcile_noop_when_only_current_flush_present(measure_mod, tmp_path, monkeypatch):
    """(c) When only the current flush is present (no fossil), the reconcile is
    a no-op: reason 'nothing_to_do', the file is byte-unchanged, and NO backup
    is written. This avoids lease/backup churn on every healthy SessionStart.
    """
    settings_path = tmp_path / "settings.json"
    payload = _settings([_hook(CURRENT, **{"async": True})])
    settings_path.write_text(json.dumps(payload), encoding="utf-8")
    before = settings_path.read_bytes()
    monkeypatch.setattr(measure_mod, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(measure_mod, "CLAUDE_DIR", tmp_path)

    result = measure_mod._reconcile_sessionend_fossils()

    assert result["reason"] == "nothing_to_do", result
    assert result["rewritten"] == 0 and result["removed"] == 0 and result["stop_removed"] == 0
    assert settings_path.read_bytes() == before, "no-op reconcile mutated the file"
    assert not _backup_dir(tmp_path).exists(), "no backup should be written for a no-op"


def test_reconcile_preserves_unrelated_hook_in_same_sessionend_group(measure_mod, tmp_path, monkeypatch):
    """(d) False-positive guard inside a group: a non-token-optimizer hook in
    the SAME SessionEnd group as a fossil must survive the rewrite untouched.

    The reconcile rewrites the fossil in place and leaves every other hook in
    the group byte-identical. A regression that matched too broadly (e.g. a
    ``measure.py`` substring hit on an unrelated command, or a whole-group
    wipe) would drop the user's own hook.
    """
    settings_path = tmp_path / "settings.json"
    # FOSSIL and UNRELATED share the SAME SessionEnd group.
    payload = _settings([_hook(FOSSIL), _hook(UNRELATED)])
    settings_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(measure_mod, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(measure_mod, "CLAUDE_DIR", tmp_path)

    result = measure_mod._reconcile_sessionend_fossils()
    assert result.get("rewritten", 0) >= 1 or result.get("removed", 0) >= 1

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    group = data["hooks"]["SessionEnd"][0]
    cmds = [h.get("command", "") for h in group.get("hooks", []) if isinstance(h, dict)]

    # The fossil is gone (rewritten to the flush shape).
    assert not any("collect --quiet &&" in c for c in cmds), cmds
    # The unrelated hook in the SAME group survives, unchanged.
    assert UNRELATED in cmds, f"unrelated same-group hook was dropped: {cmds}"
    # A current flush shape is now present.
    assert any("session-end-flush" in c and "--trigger" in c and "end" in c for c in cmds), cmds


def test_reconcile_preserves_unrelated_hook_when_removing_fossil(measure_mod, tmp_path, monkeypatch):
    """(d, remove path) The false-positive guard also holds on the REMOVE path:
    when a current flush already exists so the fossil is removed (not
    rewritten), an unrelated hook in the same group still survives.
    """
    settings_path = tmp_path / "settings.json"
    payload = _settings([_hook(FOSSIL), _hook(UNRELATED), _hook(CURRENT, **{"async": True})])
    settings_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(measure_mod, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(measure_mod, "CLAUDE_DIR", tmp_path)

    result = measure_mod._reconcile_sessionend_fossils()
    assert result.get("removed", 0) >= 1

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    group = data["hooks"]["SessionEnd"][0]
    cmds = [h.get("command", "") for h in group.get("hooks", []) if isinstance(h, dict)]
    assert UNRELATED in cmds, f"unrelated same-group hook was dropped on remove: {cmds}"
    assert not any("collect --quiet &&" in c for c in cmds), cmds
    assert any(c == CURRENT for c in cmds), cmds


# ---------------------------------------------------------------------------
# Follow-up: the two gaps found were STILL-OPEN.
# ---------------------------------------------------------------------------


def test_existing_script_install_self_heals_on_hook_collect(measure_mod, tmp_path, monkeypatch):
    """An EXISTING script install (fossil in settings.json, NO
    ensure-health hook) heals itself when its own fossil runs collect on the
    hook path. This is the population the SessionStart ensure-health hook
    cannot reach; the SessionStart hook is deliberately absent from the fixture.
    """
    settings_path = tmp_path / "settings.json"
    # An old script install: only the fossil SessionEnd hook, no SessionStart
    # ensure-health hook anywhere.
    payload = _settings([_hook(FOSSIL)])
    assert "SessionStart" not in payload["hooks"]  # fixture sanity: no ensure-health
    settings_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(measure_mod, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(measure_mod, "CLAUDE_DIR", tmp_path)
    # Force the 24h throttle open (pretend it never ran) and swallow the flag write.
    monkeypatch.setattr(measure_mod, "_read_config_flag", lambda key, default=0: 0)
    written = {}
    monkeypatch.setattr(measure_mod, "_write_config_flag", lambda key, value: written.__setitem__(key, value))

    measure_mod._maybe_self_heal_sessionend_fossils_on_hook()

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    se_cmds = [
        h.get("command", "")
        for g in data["hooks"]["SessionEnd"]
        for h in g.get("hooks", [])
        if isinstance(h, dict)
    ]
    assert not any("collect --quiet &&" in c for c in se_cmds), (
        f"self-heal did not remove the fossil for an existing script install: {se_cmds}"
    )
    assert any("session-end-flush" in c for c in se_cmds), se_cmds
    # throttle flag advanced so it does not re-run every hook fire
    assert written.get("last_hook_heal_check")


def test_self_heal_throttled_within_24h(measure_mod, tmp_path, monkeypatch):
    """The self-heal must NOT reconcile when the 24h throttle is fresh."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(_settings([_hook(FOSSIL)])), encoding="utf-8")
    monkeypatch.setattr(measure_mod, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(measure_mod, "CLAUDE_DIR", tmp_path)
    import time as _t
    monkeypatch.setattr(measure_mod, "_read_config_flag", lambda key, default=0: int(_t.time()))
    called = {"n": 0}
    orig = measure_mod._reconcile_sessionend_fossils
    monkeypatch.setattr(measure_mod, "_reconcile_sessionend_fossils",
                        lambda: (called.__setitem__("n", called["n"] + 1), orig())[1])
    measure_mod._maybe_self_heal_sessionend_fossils_on_hook()
    assert called["n"] == 0, "self-heal ran despite a fresh 24h throttle"


def test_reconcile_reads_and_writes_under_the_lease(measure_mod, tmp_path, monkeypatch):
    """The fresh read + re-apply + atomic write must all happen INSIDE
    the held _settings_lock() (one critical section), so a concurrent writer
    cannot land between the fresh read and the write. Proven by recording
    whether the lease is held at the moment of the fresh read and the write
    (safe: no nested acquire, which could block on the non-reentrant lease).
    """
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(_settings([_hook(FOSSIL)])), encoding="utf-8")
    monkeypatch.setattr(measure_mod, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(measure_mod, "CLAUDE_DIR", tmp_path)

    import contextlib
    state = {"held": False, "read_held": [], "write_held": None}

    orig_lock = measure_mod._settings_lock

    @contextlib.contextmanager
    def spy_lock():
        with orig_lock() as acquired:
            state["held"] = True
            try:
                yield acquired
            finally:
                state["held"] = False

    orig_read = measure_mod._read_settings_json_checked
    def spy_read():
        state["read_held"].append(state["held"])
        return orig_read()

    orig_write = measure_mod._write_settings_atomic_locked
    def spy_write(data):
        state["write_held"] = state["held"]
        return orig_write(data)

    monkeypatch.setattr(measure_mod, "_settings_lock", spy_lock)
    monkeypatch.setattr(measure_mod, "_read_settings_json_checked", spy_read)
    monkeypatch.setattr(measure_mod, "_write_settings_atomic_locked", spy_write)

    result = measure_mod._reconcile_sessionend_fossils()

    # The unlocked probe read is first (held=False); the authoritative fresh
    # read is the LAST read and must be under the lease; the write too.
    assert state["read_held"], "no reads happened"
    assert state["read_held"][0] is False, "probe read should be the unlocked cheap check"
    assert state["read_held"][-1] is True, (
        f"fresh read was NOT under the lease (lost-update window still open): {state['read_held']}"
    )
    assert state["write_held"] is True, "write was not under the lease"
    # and the reconcile still did its job
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    se_cmds = [h.get("command", "") for g in data["hooks"]["SessionEnd"] for h in g.get("hooks", []) if isinstance(h, dict)]
    assert not any("collect --quiet &&" in c for c in se_cmds), se_cmds


# ---------------------------------------------------------------------------
# Follow-up: the self-heal must be reached via the real
# dispatch AND be time-bounded so a stalled settings write can't hold the pipe.
# ---------------------------------------------------------------------------


def test_dispatch_collect_on_hook_path_self_heals_fossil(measure_mod, tmp_path, monkeypatch):
    """Integration: the REAL _dispatch_collect hook path (not the private
    helper) reconciles an existing script install's fossil. Proves the fix is
    wired into the dispatch, not just callable in isolation."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(_settings([_hook(FOSSIL)])), encoding="utf-8")
    monkeypatch.setattr(measure_mod, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(measure_mod, "CLAUDE_DIR", tmp_path)
    monkeypatch.setattr(measure_mod, "_running_under_hook", lambda: True)
    monkeypatch.setattr(measure_mod, "collect_sessions", lambda **kw: None)
    monkeypatch.setattr(measure_mod, "_read_config_flag", lambda key, default=0: 0)
    monkeypatch.setattr(measure_mod, "_write_config_flag", lambda key, value: None)

    measure_mod._dispatch_collect(["collect", "--quiet"])

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    se_cmds = [h.get("command", "") for g in data["hooks"]["SessionEnd"] for h in g.get("hooks", []) if isinstance(h, dict)]
    assert not any("collect --quiet &&" in c for c in se_cmds), (
        f"_dispatch_collect hook path did not self-heal the fossil: {se_cmds}"
    )
    assert any("session-end-flush" in c for c in se_cmds), se_cmds


def test_self_heal_whole_tail_runs_under_a_deadline(measure_mod, tmp_path, monkeypatch):
    """Boundedness: the deadline must cover the ENTIRE self-heal tail -- the
    throttle read, the reconcile, AND the throttle write -- because each does
    synchronous filesystem I/O that could stall and hold the hook pipe open
    (the re-wedge flagged, incl. _write_config_flag's dir-create/lease/
    temp-write/os.replace). Records the exact order and asserts install is
    FIRST, clear is LAST, and every I/O step happens while armed."""
    events = []
    sentinel = object()
    monkeypatch.setattr(measure_mod, "_install_hook_budget", lambda n: events.append(("install", n)) or sentinel)
    monkeypatch.setattr(measure_mod, "_clear_hook_budget", lambda d: events.append(("clear", d is sentinel)))
    monkeypatch.setattr(measure_mod, "_read_config_flag", lambda key, default=0: events.append(("read_flag", key)) or 0)
    monkeypatch.setattr(measure_mod, "_write_config_flag", lambda key, value: events.append(("write_flag", key)))
    monkeypatch.setattr(measure_mod, "_reconcile_sessionend_fossils",
                        lambda: events.append(("reconcile",)) or {"reason": "ok", "rewritten": 1})

    measure_mod._maybe_self_heal_sessionend_fossils_on_hook()

    names = [e[0] for e in events]
    assert names[0] == "install" and events[0][1] == 10, f"budget not armed first: {events}"
    assert names[-1] == "clear" and events[-1][1] is True, f"budget not cleared last: {events}"
    # every filesystem step is bracketed by install ... clear
    for step in ("read_flag", "reconcile", "write_flag"):
        assert step in names, f"{step} did not run: {events}"
        assert 0 < names.index(step) < len(names) - 1, (
            f"{step} ran OUTSIDE the deadline bracket -> unbounded, can re-wedge: {events}"
        )
