"""Tests for session-scoped Bash rerun deduplication (bash_rerun_dedupe.py)."""
from __future__ import annotations

import importlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def env(monkeypatch, tmp_path):
    snap = tmp_path / "snap"
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(snap))
    monkeypatch.setenv("CLAUDE_SESSION_ID", "rerun-dedupe-session")
    monkeypatch.syspath_prepend(str(SCRIPTS))
    names = (
        "bash_rerun_dedupe",
        "bash_compress_hook",
        "session_store",
        "delta_diff",
        "compression_log",
        "credential_patterns",
        "archive_result",
    )
    saved = {n: sys.modules[n] for n in names if n in sys.modules}
    for n in names:
        sys.modules.pop(n, None)
    dedupe = importlib.import_module("bash_rerun_dedupe")
    hook = importlib.import_module("bash_compress_hook")
    yield dedupe, hook, snap
    for n in names:
        sys.modules.pop(n, None)
    sys.modules.update(saved)


def _realistic_git_status(extra_files: tuple[str, ...] = ()) -> str:
    lines = [
        "On branch feat/b5-more-savings",
        "Your branch is up to date with 'origin/main'.",
        "",
        "Changes not staged for commit:",
        '  (use "git add <file>..." to update what will be committed)',
        '  (use "git restore <file>..." to discard changes in working directory)',
    ]
    for i in range(18):
        lines.append(f"\tmodified:   src/services/handler_{i:02d}.py")
    for f in extra_files:
        lines.append(f"\tmodified:   {f}")
    lines.extend(["", "no changes added to commit (use \"git add\" and/or \"git commit -a\")"])
    return "\n".join(lines) + "\n"


def test_normalize_command_collapses_unquoted_whitespace_preserves_quotes(env):
    dedupe, _, _ = env
    assert dedupe.normalize_command("  git   status  \t ") == "git status"
    assert dedupe.normalize_command('grep  "foo   bar"   src/') == 'grep "foo   bar" src/'
    assert dedupe.normalize_command("grep  'a  b'   src/") == "grep 'a  b' src/"
    # Unbalanced quotes fall back safely without mangling.
    assert dedupe.normalize_command('grep "unclosed   quote') == 'grep "unclosed   quote'
    # Heredocs are never collapsed.
    heredoc = "cat <<EOF\n  hello   world\nEOF"
    assert dedupe.normalize_command(heredoc) == heredoc


def test_normalized_command_rerun_identical_includes_time_and_line_count(env):
    dedupe, hook, _ = env
    out = _realistic_git_status()
    n_lines = len(out.splitlines())

    assert hook._crossturn_dedup("git status", out, "tu-1") is None
    ref = hook._crossturn_dedup("  git   status\t", out, "tu-2")
    assert ref is not None
    assert "identical to previous run at " in ref
    assert f"{n_lines} lines" in ref
    assert len(ref) < len(out) * 0.4


def test_nearly_identical_rerun_emits_unified_diff_with_exact_changes(env):
    _, hook, _ = env
    out1 = _realistic_git_status()
    out2 = _realistic_git_status(extra_files=("src/services/new_worker.py",))
    n_lines = len(out2.splitlines())

    assert hook._crossturn_dedup("git status", out1, "tu-1") is None
    ref = hook._crossturn_dedup("git   status", out2, "tu-2")
    assert ref is not None
    assert "same as previous run at " in ref
    assert f"{n_lines} lines; changed lines below" in ref
    assert "+	modified:   src/services/new_worker.py" in ref
    assert "@@" in ref
    assert len(ref) < len(out2) * 0.85


def test_short_output_under_15_lines_passes_through(env):
    _, hook, _ = env
    short = "\n".join(f"line {i}: " + ("x" * 30) for i in range(10)) + "\n"
    assert len(short) > 200
    assert hook._crossturn_dedup("ls -la", short, "tu-1") is None
    assert hook._crossturn_dedup("ls -la", short, "tu-2") is None


def test_mostly_changed_output_passes_through(env):
    _, hook, _ = env
    out1 = "\n".join(f"item_old_{i:03d} value={i}" for i in range(40)) + "\n"
    out2 = "\n".join(f"item_new_{i:03d} value={i * 2}" for i in range(40)) + "\n"
    assert hook._crossturn_dedup("ls -la", out1, "tu-1") is None
    assert hook._crossturn_dedup("ls -la", out2, "tu-2") is None


def test_bounded_storage_prunes_old_commands_and_caps_oversized_text(env, monkeypatch):
    dedupe, hook, _ = env
    monkeypatch.setattr(dedupe, "MAX_STORED_COMMANDS", 5)
    monkeypatch.setattr(dedupe, "MAX_STORED_OUTPUT_CHARS", 1024)

    base = _realistic_git_status()
    for i in range(8):
        assert hook._crossturn_dedup(f"git status --path-{i}", base, f"tu-{i}") is None

    from session_store import SessionStore
    store = SessionStore("rerun-dedupe-session")
    try:
        count = store._connect().execute("SELECT COUNT(*) FROM command_outputs").fetchone()[0]
    finally:
        store.close()
    assert count <= 5

    # Oversized output (> MAX_STORED_OUTPUT_CHARS): identical rerun still
    # matches via SHA-256, but a changed rerun passes through (never diffs
    # truncated text).
    huge1 = "\n".join(f"row {i:04d}: " + ("a" * 60) for i in range(40)) + "\n"
    huge2 = huge1 + "row 9999: changed\n"
    assert len(huge1) > 1024
    assert hook._crossturn_dedup("cat big.log", huge1, "tu-h1") is None
    ref_same = hook._crossturn_dedup("cat big.log", huge1, "tu-h2")
    assert ref_same is not None and "identical" in ref_same.lower()
    # Changed oversized output must pass through (exactness guard).
    assert hook._crossturn_dedup("cat big.log", huge2, "tu-h3") is None


def test_end_to_end_hook_archives_raw_output_behind_expand(env, monkeypatch, capsys):
    _, hook, _ = env
    out1 = _realistic_git_status()
    out2 = _realistic_git_status(extra_files=("src/services/extra.py",))

    def _run_hook(cmd: str, stdout: str, tool_use_id: str):
        payload = {
            "session_id": "rerun-dedupe-session",
            "tool_name": "Bash",
            "tool_use_id": tool_use_id,
            "tool_input": {"command": cmd},
            "tool_response": {"stdout": stdout, "stderr": "", "exit_code": 0},
        }
        hook._run(payload)
        return capsys.readouterr().out

    _run_hook("git status", out1, "tu-e2e-1")
    second_raw = _run_hook("git  status", out2, "tu-e2e-2")
    assert second_raw.strip(), "second run should emit updatedToolOutput"
    parsed = json.loads(second_raw)
    updated_stdout = parsed["hookSpecificOutput"]["updatedToolOutput"]["stdout"]
    assert "same as previous run at " in updated_stdout
    assert "src/services/extra.py" in updated_stdout
    assert "expand " in updated_stdout


def test_fail_open_on_store_error(env, monkeypatch):
    dedupe, _, _ = env
    import session_store

    def _boom(*_a, **_kw):
        raise RuntimeError("sqlite busy")

    monkeypatch.setattr(session_store, "SessionStore", _boom)
    assert dedupe.dedupe_rerun(
        "git status",
        _realistic_git_status(),
        session_id="rerun-dedupe-session",
        store_id="rerun-dedupe-session",
    ) is None
