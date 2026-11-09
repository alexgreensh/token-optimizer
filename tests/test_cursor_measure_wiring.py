"""Cursor wiring in measure.py: runtime routing, JSONL guard, the upscert
helper, and the end-to-end collector.

The heavy lifting (tally readers, transcript estimate, vscdb token reader) is
unit-tested in test_cursor_state.py; the normalizer in test_cursor_session.py.
This file pins the measure.py seams that integrate those two into the trends
pipeline:

  - `_use_cursor_session_adapter()` routes collect_sessions() to the Cursor
    collector (never the ~/.claude JSONL scanner).
  - `_find_all_jsonl_files()` returns [] under Cursor (defense-in-depth for the
    dashboard exemption) without touching ~/.claude.
  - `measure_components()` returns the Cursor component dict without scanning.
  - `_insert_normalized_session()` writes platform='cursor', the
    `<platform>:<slug>` dedup key, and upgrades a row in place when the stored
    `incomplete` flag changes (idle rows were previously frozen).
  - `_collect_cursor_sessions()` reads tallies end-to-end and does a per-
    workspace restore-context write.
  - the `cursor-rollup` / `cursor-summary` dispatches refuse (with a hint)
    when the detected runtime is not cursor — the bridge always pins
    TOKEN_OPTIMIZER_RUNTIME=cursor.

All fixtures live under tmp_path; no network, and no writes outside tmp_path.
"""
from __future__ import annotations

import hashlib
import importlib
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"

_MODULES = ("measure", "runtime_env", "plugin_env", "cursor_session", "cursor_state")


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Undo the _purge()/_import_measure() re-imports after each test.

    _import_measure pops and re-imports ``measure`` (and its ``runtime_env``
    sibling) so it can pin module-level paths to tmp_path. Without this
    fixture the fresh instances stay in sys.modules and later test FILES that
    imported the originals at collection time patch one instance while the
    code under test runs the other (closeout regression, same shape as
    test_windows_spawn_no_window.py).
    """
    saved = sys.modules.copy()
    yield
    for k in list(sys.modules):
        if k not in saved:
            del sys.modules[k]
    sys.modules.update(saved)


def _purge():
    for name in _MODULES:
        sys.modules.pop(name, None)


def _import_measure(monkeypatch, tmp_path, runtime="cursor"):
    snap = tmp_path / "snap"
    snap.mkdir(parents=True, exist_ok=True)
    # runtime_env confines TOKEN_OPTIMIZER_CURSOR_HOME under $HOME, so HOME is
    # repointed at tmp_path and the cursor home lives as <HOME>/.cursor.
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    claude_home = tmp_path / "claude-home"
    claude_home.mkdir(parents=True, exist_ok=True)
    cursor_home = home / ".cursor"
    cursor_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Path.home() on Windows
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(snap))
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", runtime)
    monkeypatch.setenv("TOKEN_OPTIMIZER_CURSOR_HOME", str(cursor_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setenv("TOKEN_OPTIMIZER_NO_PROC_SCAN", "1")
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    _purge()
    mod = importlib.import_module("measure")
    return mod, snap, claude_home, cursor_home


# ---------------------------------------------------------------------------
# Routing / guards
# ---------------------------------------------------------------------------

def test_use_cursor_session_adapter_routes_collect(monkeypatch, tmp_path):
    mod, _snap, _ch, _cuh = _import_measure(monkeypatch, tmp_path, "cursor")
    assert mod._use_cursor_session_adapter() is True
    assert mod.detect_runtime() == "cursor"
    assert "cursor" in mod._FOREIGN_RUNTIMES
    assert mod._FOREIGN_RUNTIME_EXEMPTIONS.get("cursor") == frozenset({"dashboard"})


def test_find_all_jsonl_files_empty_and_no_scan_under_cursor(monkeypatch, tmp_path):
    """The dashboard exemption must not fall back into ~/.claude/projects JSONL."""
    mod, _snap, claude_home, _cuh = _import_measure(monkeypatch, tmp_path, "cursor")
    # A real Claude JSONL lives next door; if the guard is gone the scanner
    # would find it under the cursor runtime.
    proj = claude_home / "projects" / "-Users-x-y"
    proj.mkdir(parents=True)
    (proj / "abc.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
    assert mod._find_all_jsonl_files(days=30) == []


def test_measure_components_cursor_shape(monkeypatch, tmp_path):
    mod, _snap, _ch, cursor_home = _import_measure(monkeypatch, tmp_path, "cursor")
    comps = mod._measure_cursor_components()
    assert "cursor_hooks" in comps
    assert comps["cursor_hooks"]["tokens"] == 0
    assert str(cursor_home) in comps["cursor_hooks"]["path"]


# ---------------------------------------------------------------------------
# _insert_normalized_session: platform, dedup, incomplete-flag upgrade
# ---------------------------------------------------------------------------

def _parsed(slug="composer-abc-123", incomplete=False):
    return {
        "slug": slug,
        "first_ts": "2026-09-01T00:00:00+00:00",
        "duration_minutes": 12.5,
        "total_input_tokens": 1000,
        "total_output_tokens": 400,
        "message_count": 2,
        "api_calls": 2,
        "cache_hit_rate": 0.0,
        "incomplete": incomplete,
        "cwd": "/Users/alex/repos/demo",
        "version": "3.18.9",
        "cost_source": "cursor_no_cost_data",
        "cost_usd": 0.0,
        "credits": None,
    }


def test_insert_normalized_session_writes_platform_and_dedup_key(monkeypatch, tmp_path):
    mod, _snap, _ch, _cuh = _import_measure(monkeypatch, tmp_path, "cursor")
    conn = mod._init_trends_db()
    assert mod._insert_normalized_session(
        conn, "cursor:composer-abc-123", _parsed(), "cursor", "cursor"
    ) == 1
    conn.commit()
    row = conn.execute(
        "SELECT jsonl_path, platform, incomplete, project FROM session_log"
    ).fetchone()
    conn.close()
    assert row[0] == "cursor:composer-abc-123"
    assert row[1] == "cursor"
    assert row[2] == 0
    assert row[3] == "/Users/alex/repos/demo"


def test_insert_normalized_session_upgrades_incomplete_flag(monkeypatch, tmp_path):
    """An idle-finalized row (incomplete=0) flips back when the chat resumes,
    and a second identical call is idempotent (returns 0)."""
    mod, _snap, _ch, _cuh = _import_measure(monkeypatch, tmp_path, "cursor")
    conn = mod._init_trends_db()
    # First: finalized (complete) session.
    assert mod._insert_normalized_session(
        conn, "cursor:composer-abc-123", _parsed(incomplete=False), "cursor", "cursor"
    ) == 1
    # Same completeness again -> idempotent no-op.
    assert mod._insert_normalized_session(
        conn, "cursor:composer-abc-123", _parsed(incomplete=False), "cursor", "cursor"
    ) == 0
    # Chat resumes -> incompleteness flips, row upgraded.
    assert mod._insert_normalized_session(
        conn, "cursor:composer-abc-123", _parsed(incomplete=True), "cursor", "cursor"
    ) == 1
    conn.commit()
    row = conn.execute(
        "SELECT incomplete FROM session_log WHERE jsonl_path = 'cursor:composer-abc-123'"
    ).fetchone()
    conn.close()
    assert row == (1,)


# ---------------------------------------------------------------------------
# End-to-end collector
# ---------------------------------------------------------------------------

def _tally():
    now = time.time()
    return {
        "conversation_id": "composer-abc-123",
        "turns": 2,
        "tool_calls": 2,
        "models": {"gpt-4o": 2},
        "tool_names": {"Shell": 2},
        "compactions": [],
        "first_ts": now - 600.0,
        "updated_at": now - 60.0,
        "final": True,
        "end_reason": "sessionEnd",
        "cursor_version": "3.18.9",
        "cwd": "/Users/alex/repos/demo",
    }


def _monkeypatch_cursor_state_readers(monkeypatch, *, bubble_tokens=None):
    import cursor_state as cst

    monkeypatch.setattr(cst, "find_tallies", lambda home: [Path("tally.json")])
    monkeypatch.setattr(cst, "read_tally", lambda p: _tally())
    monkeypatch.setattr(cst, "idle_finalise", lambda t: t)
    monkeypatch.setattr(
        cst, "read_state_vscdb_tokens", lambda ids, **kwargs: bubble_tokens or {}
    )
    monkeypatch.setattr(cst, "transcript_estimate", lambda p, home: None)
    return cst


def test_collect_cursor_sessions_end_to_end(monkeypatch, tmp_path):
    """One tally becomes one session_log row (platform=cursor) and a per-
    workspace restore-context file keyed by sha1(workspace_root)."""
    mod, snap, _claude_home, cursor_home = _import_measure(monkeypatch, tmp_path, "cursor")
    _monkeypatch_cursor_state_readers(monkeypatch)
    new = mod._collect_cursor_sessions(days=90, quiet=True)
    assert new == 1
    conn = sqlite3.connect(str(mod.TRENDS_DB))
    rows = conn.execute(
        "SELECT jsonl_path, platform, incomplete, input_tokens, output_tokens FROM session_log"
    ).fetchall()
    conn.close()
    # No bubble tokens and no transcript estimate -> a tally-only row that is
    # still ingested (never dropped for lacking tokens), complete (sessionEnd).
    assert rows == [("cursor:composer-abc-123", "cursor", 0, 0, 0)]

    restore_dir = cursor_home / "token-optimizer" / "restore-context"
    files = list(restore_dir.glob("*.md"))
    assert len(files) == 1
    digest = hashlib.sha1(b"/Users/alex/repos/demo").hexdigest()
    assert files[0].name == f"{digest}.md"
    assert "previous Cursor session" in files[0].read_text(encoding="utf-8")


def test_collect_cursor_sessions_idempotent_second_run(monkeypatch, tmp_path):
    mod, _snap, _claude_home, _cursor_home = _import_measure(monkeypatch, tmp_path, "cursor")
    _monkeypatch_cursor_state_readers(monkeypatch)
    assert mod._collect_cursor_sessions(days=90, quiet=True) == 1
    assert mod._collect_cursor_sessions(days=90, quiet=True) == 0


def test_collect_cursor_sessions_skips_incomplete_restore(monkeypatch, tmp_path):
    """An incomplete (crash/kill) session is ingested as a row but never seeds
    continuity restore."""
    mod, _snap, _claude_home, cursor_home = _import_measure(monkeypatch, tmp_path, "cursor")
    cst = _monkeypatch_cursor_state_readers(monkeypatch)
    tally = _tally()
    tally["final"] = False
    tally["end_reason"] = ""
    monkeypatch.setattr(cst, "read_tally", lambda p: tally)

    assert mod._collect_cursor_sessions(days=90, quiet=True) == 1
    restore_dir = cursor_home / "token-optimizer" / "restore-context"
    assert not restore_dir.exists() or not list(restore_dir.glob("*.md"))


def test_collect_cursor_sessions_keys_restore_by_workspace_root(monkeypatch, tmp_path):
    """The restore file must be keyed by workspace_roots[0] (repo root), not by
    a tool's working_directory (often a subdirectory), so the sessionStart hook
    (which looks up workspace_roots[0]) actually finds it."""
    mod, _snap, _claude_home, cursor_home = _import_measure(monkeypatch, tmp_path, "cursor")
    cst = _monkeypatch_cursor_state_readers(monkeypatch)
    tally = _tally()
    tally["cwd"] = "/Users/alex/repos/demo/src"          # tool working dir
    tally["workspace_roots"] = ["/Users/alex/repos/demo"]  # repo root
    monkeypatch.setattr(cst, "read_tally", lambda p: tally)

    mod._collect_cursor_sessions(days=90, quiet=True)

    restore_dir = cursor_home / "token-optimizer" / "restore-context"
    files = list(restore_dir.glob("*.md"))
    assert len(files) == 1
    digest = hashlib.sha1(b"/Users/alex/repos/demo").hexdigest()
    assert files[0].name == f"{digest}.md"


def test_dashboard_cursor_toggles_read_real_doctor_names(monkeypatch, tmp_path):
    """The dashboard collector must map to the check names cursor_doctor
    actually emits (IDE/CLI token planes), not stale names."""
    mod, _snap, _ch, _cuh = _import_measure(monkeypatch, tmp_path, "cursor")
    import cursor_doctor as cd

    monkeypatch.setattr(cd, "run_checks", lambda: [
        {"name": "TO hook config", "status": "ok"},
        {"name": "hook payload", "status": "ok"},
        {"name": "measure-path locator", "status": "warn"},
        {"name": "persisted python", "status": "ok"},
        {"name": "IDE token plane", "status": "ok"},
        {"name": "CLI transcript plane", "status": "warn"},
    ])
    status = mod._collect_cursor_hook_status_for_dashboard()
    assert status["cursor_data"]["installed"] is True
    assert status["cursor_data"]["partial"] is True
    assert status["cursor_payload"]["partial"] is True


# ---------------------------------------------------------------------------
# Dispatch refusals (R20): no cursor runtime pin -> hint, no DB write.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("subcommand", ["cursor-rollup", "cursor-summary"])
def test_cursor_subcommand_refuses_without_runtime_pin(tmp_path, subcommand):
    snap = tmp_path / "snap"
    snap.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(snap),
        "TOKEN_OPTIMIZER_NO_PROC_SCAN": "1",
    })
    env.pop("TOKEN_OPTIMIZER_RUNTIME", None)
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "measure.py"), subcommand],
        capture_output=True, text=True, env=env, timeout=180,
    )
    assert out.returncode == 0
    assert "set TOKEN_OPTIMIZER_RUNTIME=cursor" in out.stderr
    assert not (snap / "trends.db").exists()


def test_dashboard_management_data_does_not_fall_through_to_claude(monkeypatch):
    """P1-8: under a Cursor runtime pin, _collect_management_data must return a
    minimal cursor-mode dict, never scan CLAUDE_DIR/_backups for Claude data."""
    import measure as m
    monkeypatch.setattr(m, "detect_runtime", lambda: "cursor")
    data = m._collect_management_data(components={})
    assert data["mode"] == "cursor"
    assert data["skills"] == {"active": [], "archived": []}


def test_dashboard_health_data_does_not_probe_claude_under_cursor(monkeypatch):
    import measure as m
    monkeypatch.setattr(m, "detect_runtime", lambda: "cursor")
    health = m._collect_health_data()
    assert health["runtime"] == "cursor"
    assert health["running_sessions"] == []


def test_restore_stamps_source_session_end(monkeypatch, tmp_path):
    mod, _, _, cursor_home = _import_measure(monkeypatch, tmp_path, "cursor")
    _monkeypatch_cursor_state_readers(monkeypatch)
    assert mod._collect_cursor_sessions(days=90, quiet=True) == 1
    path = next((cursor_home / "token-optimizer" / "restore-context").glob("*.md"))
    marker = path.read_text().splitlines()[0]
    assert marker.startswith("<!-- source-end: ")
    assert abs(float(marker[len("<!-- source-end: "):-4]) - (time.time() - 60)) < 5


# ---------------------------------------------------------------------------
# cursor-summary token resolution (F10)
# ---------------------------------------------------------------------------

def test_cursor_summary_resolves_token_sources(monkeypatch, tmp_path, capsys):
    """cursor-summary must resolve state.vscdb/transcript tokens like the
    rollup collector. Raw tally JSON never carries bubble_tokens or
    transcript_tokens, so without this step the summary always prints
    '0 in / 0 out' and mislabels every session 'estimated'."""
    mod, _snap, _ch, _cuh = _import_measure(monkeypatch, tmp_path, "cursor")
    _monkeypatch_cursor_state_readers(
        monkeypatch,
        bubble_tokens={"composer-abc-123": {
            "input_tokens": 5000, "output_tokens": 900, "model": "gpt-4o",
        }},
    )
    mod._cursor_summary()
    out = capsys.readouterr().out
    assert "5,000 in / 900 out" in out
    assert "cursor_state_vscdb" in out
    assert "use estimated token counts" not in out


def test_cursor_summary_reports_unknown_tokens_honestly(monkeypatch, tmp_path, capsys):
    """With no token source at all, the summary must say the figures are
    unknown (tally only) — not claim every session is 'estimated'."""
    mod, _snap, _ch, _cuh = _import_measure(monkeypatch, tmp_path, "cursor")
    _monkeypatch_cursor_state_readers(monkeypatch)  # bubble {}, transcript None
    mod._cursor_summary()
    out = capsys.readouterr().out
    assert "no token figures" in out
    assert "use estimated token counts" not in out


def test_summary_marks_estimated_headline(monkeypatch, tmp_path, capsys):
    mod, _, _, _ = _import_measure(monkeypatch, tmp_path, "cursor")
    cst = _monkeypatch_cursor_state_readers(monkeypatch)
    monkeypatch.setattr(cst, "transcript_estimate", lambda p, home: 200)
    mod._cursor_summary()
    headline = next(line for line in capsys.readouterr().out.splitlines() if "Tokens:" in line)
    assert "includes 1 estimated" in headline
