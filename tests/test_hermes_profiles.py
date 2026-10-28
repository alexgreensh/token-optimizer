"""Hermes profiles (#291): a multiplexed gateway runs every profile in one process.

Upstream (hermes-agent at 7ee6d99c, read as text): hermes_constants.get_hermes_home()
returns a context-local override (set_hermes_home_override) before the HERMES_HOME
env var; hermes_cli/plugins.get_plugin_manager() keys a manager per resolved home,
and the gateway / cron ticker bind the routed profile's override around its turns,
so a hook runs with get_hermes_home() == the active profile while
os.environ["HERMES_HOME"] still names the launch profile.

These tests reproduce that: process env HERMES_HOME = the default profile, the
hook fired with a stand-in hermes_constants whose override names a secondary
profile, Hermes state for the session only in the secondary profile. Every
engine runs for real in child processes.
"""

from __future__ import annotations

import contextlib
import contextvars
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from test_hermes_parity import (  # noqa: F401  (fixtures are used by name)
    FIND_COMMAND,
    FIND_OUTPUT,
    MCP_RESULT,
    SCRIPTS,
    _checkpoints,
    _clean_env,
    _make_state_db,
    _terminal,
    _wait_for,
    home,
    plugin,
)

_OVERRIDE: contextvars.ContextVar = contextvars.ContextVar("fake_hermes_home_override", default=None)


def _fake_get_hermes_home() -> Path:
    """hermes_constants.get_hermes_home at 7ee6d99c: override, then env, then default."""
    override = _OVERRIDE.get()
    if override:
        return Path(override)
    raw = os.environ.get("HERMES_HOME", "").strip()
    return Path(raw) if raw else Path.home() / ".hermes"


@contextlib.contextmanager
def scope(path: Path):
    """What Hermes's _plugin_home_scope / _profile_cron_scope do around a hook."""
    token = _OVERRIDE.set(str(path))
    try:
        yield
    finally:
        _OVERRIDE.reset(token)


@pytest.fixture()
def profiles(home, monkeypatch, tmp_path):
    """(default, secondary): env names the default, as in a multiplexed gateway."""
    default = home / ".hermes"
    secondary = default / "profiles" / "work"
    secondary.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(default))
    fake = types.ModuleType("hermes_constants")
    fake.get_hermes_home = _fake_get_hermes_home
    monkeypatch.setitem(sys.modules, "hermes_constants", fake)
    return default, secondary


def _seed_state_db(profile: Path, session_id: str, tmp_path: Path, **kw) -> None:
    """Put a realistic state.db for ``session_id`` into ``profile`` only."""
    seed = tmp_path / f"seed-{session_id}"
    (seed / ".hermes").mkdir(parents=True)
    _make_state_db(seed, session_id, **kw)
    shutil.move(str(seed / ".hermes" / "state.db"), str(profile / "state.db"))


def _outside(default: Path, secondary: Path, pattern: str) -> list[Path]:
    """Files matching ``pattern`` in the default profile, not in the secondary."""
    return [p for p in default.rglob(pattern) if secondary not in p.parents]


def _session_rows(db_root: Path, exclude: Path | None, session_id: str) -> int:
    count = 0
    for db in db_root.rglob("trends.db"):
        if exclude is not None and exclude in db.parents:
            continue
        conn = sqlite3.connect(db)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            if "session_log" in tables:
                count += conn.execute("SELECT COUNT(*) FROM session_log WHERE jsonl_path LIKE ?",
                                      (f"%{session_id}%",)).fetchone()[0]
        finally:
            conn.close()
    return count


# ---------------------------------------------------------------------------
# The reported bug: the rollup lands in the profile that ran the session
# ---------------------------------------------------------------------------

def test_secondary_profile_session_rolls_up_into_its_own_trends_db(plugin, profiles, tmp_path):
    default, secondary = profiles
    sid = "20261028_101500_cron01"
    _seed_state_db(secondary, sid, tmp_path)
    with scope(secondary):
        plugin.on_session_finalize(session_id=sid, platform="cron", reason="cron_complete")
    assert _wait_for(lambda: _session_rows(secondary, None, sid), timeout=90), \
        "no session_log row in the secondary profile's trends.db"
    assert (secondary / "_backups" / "token-optimizer" / "trends.db").is_file()
    time.sleep(1.0)  # the dashboard refresh child runs after the rollup child
    assert _session_rows(default, secondary, sid) == 0


def test_secondary_profile_checkpoint_is_written_in_that_profile(plugin, profiles, tmp_path):
    default, secondary = profiles
    sid = "20261028_101500_ckpt01"
    _seed_state_db(secondary, sid, tmp_path)
    with scope(secondary):
        plugin.on_session_end(session_id=sid, task_id="t", turn_id="turn-1", completed=True,
                              failed=False, interrupted=False, turn_exit_reason="done",
                              model="claude-sonnet-4-5", platform="cron")
    files = _wait_for(lambda: sorted(secondary.rglob(f"checkpoints/{sid}-*.md")), timeout=60)
    assert files, "no checkpoint in the secondary profile"
    # Built from the secondary profile's state.db, the only place the transcript is.
    assert "Refactor the parser module" in files[0].read_text(encoding="utf-8")
    assert not _outside(default, secondary, f"checkpoints/{sid}-*.md")


def test_secondary_profile_tool_results_are_archived_in_that_profile(plugin, profiles):
    default, secondary = profiles
    sid = "20261028_101500_tool01"
    with scope(secondary):
        out = _terminal(plugin, sid, FIND_COMMAND, FIND_OUTPUT)
        mcp = plugin.on_transform_tool_result(tool_name="mcp__docs__fetch", args={"q": 1},
                                              result=MCP_RESULT, task_id="t", session_id=sid,
                                              tool_call_id="call_mcp_p1", status="ok")
    assert out and len(out) < len(FIND_OUTPUT) // 3
    assert mcp and "Full result archived" in mcp
    in_secondary = [p for p in secondary.rglob("*") if p.is_file()]
    assert any("archive" in str(p) for p in in_secondary), in_secondary
    assert (secondary / "_backups" / "token-optimizer" / "trends.db").is_file()
    leaked = [p for p in default.rglob("*") if p.is_file() and secondary not in p.parents]
    assert leaked == [], leaked
    # The expand pointer printed to the model resolves in that profile.
    import re

    key = re.search(r"--runtime hermes expand ([A-Za-z0-9_-]+)", out).group(1)
    env = {**_clean_env(secondary.parents[2]), "HERMES_HOME": str(secondary)}
    proc = subprocess.run([sys.executable, str(SCRIPTS / "measure.py"), "--runtime", "hermes",
                           "expand", key], env=env, capture_output=True, text=True, timeout=120)
    assert proc.stdout.strip() == FIND_OUTPUT.strip()


def test_in_process_compression_probe_reads_the_active_profile(plugin, profiles):
    default, secondary = profiles
    sid = "20261028_101500_probe1"
    for profile, until in ((default, 0.0), (secondary, time.time() + 600)):
        conn = sqlite3.connect(profile / "state.db")
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, compression_failure_cooldown_until REAL)")
        conn.execute("INSERT INTO sessions VALUES (?, ?)", (sid, until))
        conn.commit()
        conn.close()
    with scope(secondary):
        assert plugin._native_compression_needs_help(sid) is True
    with scope(default):
        assert plugin._native_compression_needs_help(sid) is False


def test_every_child_gets_the_active_profile_home(profiles):
    import hermes_hook_bridge as bridge

    default, secondary = profiles
    with scope(secondary):
        assert bridge.active_hermes_home() == str(secondary)
        assert bridge._child_env()["HERMES_HOME"] == str(secondary)
        assert bridge._child_env(TOKEN_OPTIMIZER_INTERACTIVE="1")["TOKEN_OPTIMIZER_RUNTIME"] == "hermes"
    assert bridge._child_env()["HERMES_HOME"] == str(default)


def test_without_hermes_the_env_var_still_decides(home, monkeypatch):
    import hermes_hook_bridge as bridge

    monkeypatch.setitem(sys.modules, "hermes_constants", None)  # import fails
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    assert bridge._child_env()["HERMES_HOME"] == str(home / ".hermes")
    monkeypatch.delenv("HERMES_HOME")
    assert "HERMES_HOME" not in bridge._child_env()


# ---------------------------------------------------------------------------
# Doctor: every profile is named with its install state
# ---------------------------------------------------------------------------

def _install_into(home: Path, profile: Path, enable: bool = True) -> None:
    env = {**_clean_env(home), "HERMES_HOME": str(profile)}
    cmd = [sys.executable, str(SCRIPTS / "measure.py"), "hermes-install"] + (["--enable"] if enable else [])
    subprocess.run(cmd, check=True, env=env, capture_output=True, text=True, timeout=120)


def test_doctor_lists_each_profile_and_flags_one_left_behind(home, monkeypatch):
    import hermes_doctor

    default = home / ".hermes"
    work = default / "profiles" / "work"
    idle = default / "profiles" / "idle"
    work.mkdir(parents=True)
    idle.mkdir(parents=True)
    _install_into(home, default)
    _install_into(home, work)
    monkeypatch.setenv("HERMES_HOME", str(work))
    [check] = hermes_doctor._profile_checks()
    assert check["status"] == "OK", check
    assert "default: installed, enabled, current" in check["detail"]
    assert "work: installed, enabled, current" in check["detail"]
    assert "idle: not installed" in check["detail"]

    # A profile whose copy lags the checkout, or that is not allow-listed.
    (work / "plugins" / "token-optimizer" / "plugin.yaml").write_text('name: token-optimizer\nversion: "0.1.0"\n',
                                                                       encoding="utf-8")
    [check] = hermes_doctor._profile_checks()
    assert check["status"] == "WARN"
    assert "work: out of date" in check["detail"]
    assert f"HERMES_HOME={work}" in check["detail"]


def test_doctor_stays_quiet_without_profiles(home, monkeypatch):
    import hermes_doctor

    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    assert hermes_doctor._profile_checks() == []


# ---------------------------------------------------------------------------
# Minor items from #291
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", ["hermes-rollup", "hermes-checkpoint", "hermes-summary",
                                     "hermes-turn-context"])
def test_hermes_subcommand_help_prints_usage_and_runs_nothing(home, tmp_path, command):
    _make_state_db(home, "sess_help_01")
    proc = subprocess.run([sys.executable, str(SCRIPTS / "measure.py"), command, "--help"],
                          env=_clean_env(home), capture_output=True, text=True, timeout=120,
                          stdin=subprocess.DEVNULL)
    assert proc.returncode == 0
    assert proc.stdout.startswith(f"usage: measure.py {command}"), proc.stdout[:300]
    assert "HERMES_HOME" in proc.stdout
    assert not list(home.rglob("trends.db")), "help ran the command"


def test_trends_labels_say_which_figures_include_cache_reads(home, monkeypatch):
    import measure

    trends = {
        "session_count": 2, "avg_duration_minutes": 10, "avg_input_tokens": 900_000,
        "avg_output_tokens": 4_000, "skills": {"used": {}, "installed_count": 0, "never_used": []},
        "subagents": {}, "model_mix": {"claude-sonnet-4-5": 60_000, "claude-haiku-4-5": 20_000},
    }
    monkeypatch.setattr(measure, "_collect_trends_data", lambda days: trends)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            measure.usage_trends(days=7)
        except Exception:
            pass  # later sections need fuller data; the two labels print first
    text = buf.getvalue()
    assert "Avg tokens/session: 900K in (incl. cache reads) / 4K out" in text
    assert "MODEL MIX (share of tokens, excl. cache reads)" in text
