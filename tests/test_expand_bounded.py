#!/usr/bin/env python3
"""Bounded ``expand`` windows: --lines/--start/--chars/--match, hit metadata,
whole-body search indexing, and range-scoped re-expansion debits.

Run: python3 -m pytest tests/test_expand_bounded.py -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from token_estimate import estimate_tokens  # noqa: E402

SESSION = "bounded-sess-1"
KEY = "tu-bounded-1"

# 200 lines, ~70 chars each, two MATCHME markers far enough apart to be
# separate match groups.
BODY = "".join(
    f"line {i:03d} " + ("x" * 55) + (" MATCHME" if i in (50, 120) else "") + "\n"
    for i in range(1, 201)
)


def _env(tmp_path, session=SESSION):
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / ".claude")
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(tmp_path / "snap")
    env["TOKEN_OPTIMIZER_RUNTIME"] = "claude"
    env["CLAUDE_SESSION_ID"] = session
    (tmp_path / "home").mkdir(exist_ok=True)
    return env


def _seed_archive(tmp_path, body=BODY, key=KEY, session=SESSION, **meta):
    archive_dir = tmp_path / "snap" / "tool-archive" / session
    archive_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "tool_name": "Bash",
        "tool_use_id": key,
        "chars": len(body),
        "original_chars": len(body),
        "tokens_est": len(body) // 3,
        "truncated": False,
        "timestamp": "2026-11-20T10:00:00+00:00",
        "archived_from": "PostToolUse",
        "command_or_path": "cat big.log",
        "response": body,
    }
    entry.update(meta)
    (archive_dir / f"{key}.json").write_text(json.dumps(entry))
    return entry


def _run_measure(args, env, timeout=30):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "measure.py")] + args,
        capture_output=True, text=True, env=env, timeout=timeout,
    )


def _reexpand_rows(tmp_path):
    db = tmp_path / "snap" / "trends.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT tokens_saved, detail FROM savings_events "
            "WHERE event_type='tool_archive_reexpand' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _progressive_summary(tmp_path):
    code = (
        "import json, sys; "
        f"sys.path.insert(0, {str(SCRIPTS)!r}); "
        "import measure; "
        "print(json.dumps(measure._progressive_disclosure_summary(days=30)))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=_env(tmp_path), timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_expand_lines_returns_only_that_range(tmp_path):
    _seed_archive(tmp_path)
    out = _run_measure(["expand", KEY, "--lines", "50:52"], _env(tmp_path))
    assert out.returncode == 0, out.stderr
    assert "line 050" in out.stdout and "line 051" in out.stdout and "line 052" in out.stdout
    assert "line 049" not in out.stdout and "line 053" not in out.stdout
    assert "line 001" not in out.stdout and "line 200" not in out.stdout


def test_expand_bounded_header_reports_hit_metadata(tmp_path):
    _seed_archive(tmp_path, truncated=True, original_chars=len(BODY) + 9999)
    out = _run_measure(["expand", KEY, "--lines", "1:2"], _env(tmp_path))
    assert out.returncode == 0, out.stderr
    header = out.stdout.splitlines()[0]
    for bit in (f"expand {KEY}", "session=", "tool=Bash", "origin=PostToolUse",
                "captured=2026-11-20T10:00:00+00:00", "sha256:",
                f"total={len(BODY)} chars", "stored_truncated=yes",
                "original="):
        assert bit in header, header


def test_expand_chars_window_and_next_command(tmp_path):
    _seed_archive(tmp_path)
    out = _run_measure(["expand", KEY, "--start", "100", "--chars", "80"],
                       _env(tmp_path))
    assert out.returncode == 0, out.stderr
    assert BODY[100:180] in out.stdout
    assert "next:" in out.stdout and "--start 180 --chars 80" in out.stdout
    assert "prev:" in out.stdout and "--start 20 --chars 80" in out.stdout
    assert "full:" in out.stdout and " expand " in out.stdout


def test_expand_lines_next_window_command_round_trips(tmp_path):
    """The printed next command is itself a valid bounded expand."""
    _seed_archive(tmp_path)
    env = _env(tmp_path)
    out = _run_measure(["expand", KEY, "--lines", "10:12"], env)
    assert out.returncode == 0, out.stderr
    footer = out.stdout.strip().splitlines()[-1]
    assert "next:" in footer and "--lines 13:15" in footer
    # Feed the printed command's flag tail back through the CLI.
    out2 = _run_measure(["expand", KEY, "--lines", "13:15"], env)
    assert out2.returncode == 0, out2.stderr
    assert "line 013" in out2.stdout and "line 015" in out2.stdout
    assert "line 012" not in out2.stdout and "line 016" not in out2.stdout


def test_expand_match_windows_with_context(tmp_path):
    _seed_archive(tmp_path)
    out = _run_measure(["expand", KEY, "--match", "MATCHME", "--context", "1"],
                       _env(tmp_path))
    assert out.returncode == 0, out.stderr
    assert "match 1/2" in out.stdout and "match 2/2" in out.stdout
    assert "matched lines: 50" in out.stdout and "matched lines: 120" in out.stdout
    # context=1 → one line either side of each hit
    assert "line 049" in out.stdout and "line 051" in out.stdout
    assert "line 048" not in out.stdout and "line 052" not in out.stdout
    assert "[exact:" in out.stdout and "--lines 49:51" in out.stdout
    assert "--lines 119:121" in out.stdout


def test_expand_match_no_results_prints_header_and_full_command(tmp_path):
    _seed_archive(tmp_path)
    out = _run_measure(["expand", KEY, "--match", "zzz_absent_marker"],
                       _env(tmp_path))
    assert out.returncode == 0, out.stderr
    assert "no matches" in out.stdout
    assert "full:" in out.stdout
    assert _reexpand_rows(tmp_path) == []


def test_expand_match_pattern_is_literal_not_regex(tmp_path):
    from archive_window import match_windows
    # A regex-looking pattern must not match the regex it would compile to.
    assert match_windows("MATCHME\n", "MATCH.*") == ([], 0)
    windows, total = match_windows("xx MATCH.* yy\n", "MATCH.*")
    assert total == 1 and windows and "MATCH.*" in windows[0].text


def test_expand_lines_count_only_lf_breaks(tmp_path):
    body = "alpha\x0cbeta\nMATCHME gamma\nomega\x85delta\nlast"
    _seed_archive(tmp_path, body=body)
    env = _env(tmp_path)
    out = _run_measure(["expand", KEY, "--lines", "2:2"], env)
    assert out.returncode == 0, out.stderr
    assert "MATCHME gamma" in out.stdout
    assert "alpha" not in out.stdout and "omega" not in out.stdout
    out = _run_measure(["expand", KEY, "--match", "MATCHME", "--context", "0"], env)
    assert out.returncode == 0, out.stderr
    assert "matched lines: 2" in out.stdout


def test_expand_full_default_is_byte_for_byte(tmp_path):
    _seed_archive(tmp_path)
    out = _run_measure(["expand", KEY], _env(tmp_path))
    assert out.returncode == 0, out.stderr
    assert out.stdout == BODY


@pytest.mark.parametrize("extra", [
    ["--lines", "1:2", "--chars", "5"],
    ["--match", "x", "--lines", "1:2"],
    ["--context", "2"],
    ["--skip", "1"],
    ["--lines", "500:600"],
    ["--lines", "bad"],
    ["--start", "-1"],
    ["--chars", "0"],
])
def test_expand_invalid_specs_exit_1(tmp_path, extra):
    _seed_archive(tmp_path)
    out = _run_measure(["expand", KEY] + extra, _env(tmp_path))
    assert out.returncode != 0
    assert out.stdout == ""


def test_expand_debits_only_previously_uncovered_chars(tmp_path):
    _seed_archive(tmp_path)
    env = _env(tmp_path)
    start = BODY.index("line 050")
    end = BODY.index("line 053")
    window = BODY[start:end]
    out = _run_measure(["expand", KEY, "--lines", "50:52"], env)
    assert out.returncode == 0, out.stderr
    rows = _reexpand_rows(tmp_path)
    assert len(rows) == 1
    tokens, detail = rows[0]
    assert tokens == estimate_tokens(window)
    assert "chars " in detail and KEY in detail
    # A full expand debits only the chars the bounded window did not cover.
    _run_measure(["expand", KEY], env)
    rows = _reexpand_rows(tmp_path)
    assert len(rows) == 2
    uncovered = BODY[:start] + BODY[end:]
    assert rows[1][0] == estimate_tokens(uncovered)
    assert f"chars 0-{start},{end}-{len(BODY)}" in rows[1][1]


def test_expand_full_then_bounded_adds_no_debit(tmp_path):
    _seed_archive(tmp_path)
    env = _env(tmp_path)
    assert _run_measure(["expand", KEY], env).returncode == 0
    before = _reexpand_rows(tmp_path)
    assert len(before) == 1
    out = _run_measure(["expand", KEY, "--lines", "50:52"], env)
    assert out.returncode == 0, out.stderr
    assert _reexpand_rows(tmp_path) == before
    # A repeated full expand is still covered by the same union.
    assert _run_measure(["expand", KEY], env).returncode == 0
    assert _reexpand_rows(tmp_path) == before


def test_expand_overlapping_windows_debit_their_union(tmp_path):
    _seed_archive(tmp_path)
    env = _env(tmp_path)
    _run_measure(["expand", KEY, "--lines", "50:52"], env)
    # Identical range via different flags → same char range → deduped.
    first_window_start = BODY.index("line 050")
    first_window_end = BODY.index("line 053")
    _run_measure(["expand", KEY, "--start", str(first_window_start),
                  "--chars", str(first_window_end - first_window_start)], env)
    assert len(_reexpand_rows(tmp_path)) == 1
    # The second window overlaps, so only its uncovered tail (line 053) debits.
    uncovered_start = BODY.index("line 053")
    uncovered_end = BODY.index("line 054")
    _run_measure(["expand", KEY, "--lines", "51:53"], env)
    rows = _reexpand_rows(tmp_path)
    assert len(rows) == 2
    assert rows[1][0] == estimate_tokens(BODY[uncovered_start:uncovered_end])
    assert f"chars {uncovered_start}-{uncovered_end}" in rows[1][1]
    # A second identical bounded expand stays deduped.
    _run_measure(["expand", KEY, "--lines", "51:53"], env)
    assert len(_reexpand_rows(tmp_path)) == 2


def test_progressive_disclosure_counts_distinct_reexpanded_ids(tmp_path):
    _seed_archive(tmp_path)
    _seed_archive(tmp_path, key="tu-bounded-2")
    env = _env(tmp_path)
    # Two debit events for KEY (overlapping windows) plus one for KEY-2.
    _run_measure(["expand", KEY, "--lines", "50:52"], env)
    _run_measure(["expand", KEY, "--lines", "51:53"], env)
    _run_measure(["expand", "tu-bounded-2", "--lines", "1:2"], env)
    assert len(_reexpand_rows(tmp_path)) == 3
    summary = _progressive_summary(tmp_path)
    assert summary["reexpanded"] == 2


def test_expand_bounded_session_scoped(tmp_path):
    _seed_archive(tmp_path)
    out = _run_measure(["expand", KEY, "--lines", "7:9", "--session", SESSION],
                       _env(tmp_path))
    assert out.returncode == 0, out.stderr
    assert "line 007" in out.stdout and "line 009" in out.stdout
    assert "session=" + SESSION in out.stdout


def _run_archive_hook(tmp_path, payload):
    env = _env(tmp_path)
    p = subprocess.run(
        [sys.executable, str(SCRIPTS / "archive_result.py"), "--quiet"],
        input=json.dumps(payload), text=True, capture_output=True,
        env=env, timeout=60)
    assert p.returncode == 0, p.stderr
    return p.stdout


def test_search_indexes_beyond_former_50k_cap(tmp_path):
    """The SessionStore row must carry the whole stored response, so a term at
    char ~55k (past the old [:50000] index cap) is searchable, and the hit
    reports source/time/hash/length/truncation."""
    marker = "deepbodymarkerqqq"
    big = "x" * 55_000 + f"\n{marker} here\n" + "y" * 5_000
    assert len(big) > 50_000
    out = _run_archive_hook(tmp_path, {
        "tool_name": "mcp__ordinary__list_items",
        "session_id": SESSION, "tool_use_id": "tu-deep-search",
        "tool_input": {"handle": "h"}, "tool_response": big})
    assert out.strip() == "" or "updatedMCPToolOutput" in out
    env = _env(tmp_path)
    env.pop("CLAUDE_SESSION_ID", None)
    out = _run_measure(["expand", "--search", marker, "--session", SESSION], env)
    assert out.returncode == 0, out.stderr
    assert "tu-deep-search" in out.stdout
    assert "captured:" in out.stdout and "hash:" in out.stdout
    assert "chars:" in out.stdout and "stored_truncated:" in out.stdout


def test_large_archive_index_is_bounded_and_marked(tmp_path):
    """Thirty 5MB archived bodies must not fill the 50MB session store: the FTS
    index keeps a bounded head+tail copy and later cache writes still land."""
    from session_store import SessionStore
    store = SessionStore(SESSION, snapshot_dir=tmp_path / "snap")
    try:
        body = ("headmarkerzed " + "x" * 2_500_000 +
                " midmarkerqqq " + "y" * 2_499_000 +
                " tailmarkeryd")
        for i in range(30):
            store.insert_tool_output(
                tool_use_id=f"tu-big-{i}",
                tool_name="mcp__big__fetch",
                tool_type="mcp",
                command_or_path="big.fetch",
                output_hash=f"hash-{i}",
                output_chars=len(body),
                output_tokens_est=len(body) // 4,
                output_text=body,
                archived_from="test",
                truncated=0,
            )
        store.upsert_cached_content("late.txt", "late content", "late-hash")
        assert store.get_cached_content("late.txt") is not None
        assert store._connect().execute("SELECT COUNT(*) FROM tool_outputs").fetchone()[0] == 30
        row = store._connect().execute(
            "SELECT length(output_text) AS n, index_truncated "
            "FROM tool_outputs WHERE tool_use_id = 'tu-big-0'"
        ).fetchone()
        assert int(row["index_truncated"]) == 1
        assert int(row["n"]) < 2_000_000
        assert int(row["n"]) <= 256 * 1024
        head_hits = store.search_tool_outputs("headmarkerzed")
        tail_hits = store.search_tool_outputs("tailmarkeryd")
        assert head_hits and tail_hits
        assert all(int(h.get("index_truncated") or 0) == 1 for h in head_hits)
        assert store.search_tool_outputs("midmarkerqqq") == []
    finally:
        store.close()


def test_bounded_expand_output_is_recovery_not_rearchived(tmp_path):
    """A bounded expand's output passed back through the archive hook as a
    Bash result must be recognized as recovery output and not re-archived."""
    _seed_archive(tmp_path)
    env = _env(tmp_path)
    # A window big enough (>4KB) that, were it not recognized as recovery
    # output, the archive hook would happily store it as a new result.
    out = _run_measure(["expand", KEY, "--lines", "40:140"], env)
    assert out.returncode == 0, out.stderr
    assert len(out.stdout) > 4096
    # The exact printed 'next:' command is what the model would paste.
    footer = out.stdout.strip().splitlines()[-1]
    next_cmd = footer.split("next: ", 1)[1].split(" | ", 1)[0]
    hook_out = _run_archive_hook(tmp_path, {
        "tool_name": "Bash", "session_id": SESSION,
        "tool_use_id": "tu-recovered-window",
        "tool_input": {"command": next_cmd},
        "tool_response": {"stdout": out.stdout, "stderr": "", "exit_code": 0}})
    assert hook_out.strip() == "", hook_out
    assert not (tmp_path / "snap" / "tool-archive" / SESSION
                / "tu-recovered-window.json").exists()


def test_printed_next_command_is_expand_command_recognized(tmp_path):
    from recovery_output import is_expand_command
    _seed_archive(tmp_path)
    out = _run_measure(["expand", KEY, "--start", "0", "--chars", "100"],
                       _env(tmp_path))
    assert out.returncode == 0, out.stderr
    footer = out.stdout.strip().splitlines()[-1]
    for label in ("prev:", "next:", "full:"):
        if label in footer:
            cmd = footer.split(label + " ", 1)[1].split(" | ", 1)[0].rstrip("]")
            assert is_expand_command(cmd), cmd


def test_expand_recognizer_rejects_glued_shell_operators(tmp_path):
    """`--match a;b` is a compound command to the shell even though shlex keeps
    it in one word: the recognizer must split on unquoted operators."""
    import shlex
    from recovery_output import is_expand_command
    mp = shlex.quote(str(SCRIPTS / "measure.py"))
    base = f"python3 {mp} expand {KEY}"
    assert is_expand_command(base + " --match 'a;b'")
    assert is_expand_command(base + " --match 'R&D' --context 2")
    for bad in ("--match a;b", "--match a&&b", "--match a|b",
                "--match $(id)", "--lines 1:2;id", "--lines 1:2>out"):
        assert not is_expand_command(base + " " + bad), bad


def test_expand_recognizer_rejects_expansion_values_and_bad_session(tmp_path):
    import shlex
    from recovery_output import is_expand_command
    mp = shlex.quote(str(SCRIPTS / "measure.py"))
    base = f"python3 {mp} expand {KEY}"
    for bad in ("--match \"$(id)\"", "--match '$(id)'",
                "--match 'a`id`b'", "--lines \"$(id)\"",
                "--session sess.ion", "--session sess:ion"):
        assert not is_expand_command(base + " " + bad), bad
    assert is_expand_command(base + " --session sess-ion_1 --lines 1:2")
