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
