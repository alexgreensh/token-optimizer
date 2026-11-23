#!/usr/bin/env python3
"""Tests for built-in tool output compression (Grep, Glob — lossless)."""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "token-optimizer" / "scripts"
HOOKS_DIR = REPO_ROOT / "hooks"
for _p in (str(SCRIPTS_DIR), str(HOOKS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import archive_result
import archive_verify
import builtin_tool_compress as btc


# ---------------------------------------------------------------------------
# Realistic fixture builders
# ---------------------------------------------------------------------------

def _make_grep_content(num_files: int = 24, hits_per_file: int = 8) -> tuple[str, list[str]]:
    lines = []
    filenames = []
    for f_idx in range(num_files):
        fname = f"src/services/authentication/module_{f_idx:02d}/request_handler.py"
        filenames.append(fname)
        for h_idx in range(hits_per_file):
            lno = 10 + h_idx * 12
            lines.append(
                f"{fname}:{lno}:    result = validate_session_token(request.headers['Authorization'], strict=True)  # check {h_idx}"
            )
    return "\n".join(lines), filenames


def _make_grep_count(num_dirs: int = 10, files_per_dir: int = 12) -> str:
    lines = []
    for d in range(num_dirs):
        for f in range(files_per_dir):
            lines.append(f"packages/core/src/components/domain_{d:02d}/feature_{f:02d}.tsx:{(d + f) % 17 + 1}")
    return "\n".join(lines)


def _make_glob_paths(num_dirs: int = 12, files_per_dir: int = 15) -> list[str]:
    exts = (".py", ".ts", ".tsx", ".json", ".md")
    paths = []
    for d in range(num_dirs):
        for f in range(files_per_dir):
            ext = exts[(d + f) % len(exts)]
            paths.append(f"apps/web/src/features/domain_{d:02d}/component_{f:02d}{ext}")
    return paths


# ---------------------------------------------------------------------------
# Unit tests: Grep (content, count, files_with_matches) & Glob — LOSSLESS
# ---------------------------------------------------------------------------

def test_grep_content_lossless_on_every_file_and_line_and_trims_long_lines():
    raw, filenames = _make_grep_content(num_files=20, hits_per_file=8)
    long_line = f"{filenames[0]}:999:    payload = '" + ("A" * 350) + "'"
    raw_with_long = raw + "\n" + long_line

    comp = btc.compress_grep_content(raw_with_long, num_files=20, num_matches=161)
    assert comp is not None
    assert "161 matches across 20 files" in comp
    # Every single file and every single line number must be preserved.
    for fname in filenames:
        assert f"{fname} (" in comp
    for h_idx in range(8):
        lno = 10 + h_idx * 12
        assert f"  {lno}:" in comp
    assert "  999:" in comp
    assert "[trimmed, +" in comp
    assert "A" * 350 not in comp
    assert len(comp) < len(raw_with_long) * 0.75


def test_grep_count_mode_lossless_every_file_preserved():
    raw = _make_grep_count(num_dirs=10, files_per_dir=12)
    comp = btc.compress_grep_count(raw)
    assert comp is not None
    assert "120 files" in comp
    # Every single basename:count must be present.
    assert "packages/core/src/components/domain_00/" in comp
    assert "feature_00.tsx:1" in comp
    assert "packages/core/src/components/domain_09/" in comp
    assert "feature_11.tsx:" in comp
    assert len(comp) < len(raw) * 0.55


def test_glob_paths_lossless_every_basename_and_numfiles_matches_count():
    paths = _make_glob_paths(num_dirs=12, files_per_dir=15)
    summary = btc.compress_glob_paths(paths, total_matches=250, truncated=True)
    assert summary is not None
    joined = "\n".join(summary)
    assert "180 of 250 files across 12 dirs (truncated)" in joined
    # Every single directory and every single basename must be present (0 paths dropped).
    for d in range(12):
        assert f"apps/web/src/features/domain_{d:02d}/" in joined
        for f in range(15):
            assert f"component_{f:02d}." in joined
    raw_len = sum(len(p) + 1 for p in paths)
    assert len(joined) < raw_len * 0.50


def test_glob_passes_through_when_savings_under_25_percent():
    # 90 files each in its own distinct directory: factoring dir prefix saves ~0%.
    paths = [f"packages/svc_{i:03d}/sub_{i:03d}/unique_handler_{i:03d}.py" for i in range(90)]
    assert sum(len(p) + 1 for p in paths) > btc.GLOB_THRESHOLD
    assert btc.compress_glob_paths(paths) is None


# ---------------------------------------------------------------------------
# Review regression tests (exact repros from REVIEW-B1.md)
# ---------------------------------------------------------------------------

def test_repro_dated_filename_not_dropped_as_context_line():
    """Repro 1a: 30 hits in docs/notes/2026-11-22-release-plan.md + 40 in archive_result.py."""
    lines = [
        f"docs/notes/2026-11-22-release-plan.md:{10 + i}:    release step {i} notes for rollout verification checklist item"
        for i in range(30)
    ] + [
        f"skills/token-optimizer/scripts/archive_result.py:{100 + i}:    archive_step_{i} = verify_entry_exists(session_id, key)"
        for i in range(40)
    ]
    raw = "\n".join(lines)
    assert len(raw) > btc.GREP_THRESHOLD

    comp = btc.compress_grep_content(raw)
    assert comp is not None
    assert "70 matches across 2 files" in comp
    assert "docs/notes/2026-11-22-release-plan.md (30):" in comp
    assert "skills/token-optimizer/scripts/archive_result.py (40):" in comp
    for i in range(30):
        assert f"  {10 + i}:" in comp
    for i in range(40):
        assert f"  {100 + i}:" in comp


def test_repro_single_file_grep_line_number_prefix_fails_open():
    """Repro 1b: single-file Grep `100:text` (and `100:foo:200:bar`) must pass through untouched."""
    raw_plain = "\n".join(
        f"{100 + i}:    some matched line of code with enough text to exceed the 4KB threshold {i}"
        for i in range(70)
    )
    assert len(raw_plain) > btc.GREP_THRESHOLD
    assert btc.compress_grep_content(raw_plain) is None

    # Even if matched text contains `:digits:`, a numeric prefix is a line number, not a filename.
    raw_with_colons = "\n".join(
        f"{100 + i}:    http://localhost:8080/api/v1/resource/{i} status:200:ok payload_padding_here"
        for i in range(70)
    )
    assert len(raw_with_colons) > btc.GREP_THRESHOLD
    assert btc.compress_grep_content(raw_with_colons) is None


def test_repro_single_file_grep_with_filename_passes_through():
    """Single-file search (1 unique file) passes through untouched."""
    raw = "\n".join(
        f"skills/token-optimizer/scripts/archive_result.py:{10 + i}:    matched content line {i} with padding text"
        for i in range(70)
    )
    assert len(raw) > btc.GREP_THRESHOLD
    assert btc.compress_grep_content(raw) is None
    assert btc.compress_grep_content(raw, num_files=1) is None


def test_repro_windows_paths_and_colons_in_text_preserved():
    """Windows drive-letter paths and colons in matched text parse cleanly without loss."""
    lines = [
        f"C:\\Users\\dev\\workspace\\service_a\\config.py:{10 + i}:    url = 'https://api.example.com:8443/v2:item_{i}'"
        for i in range(35)
    ] + [
        f"D:\\Repos\\project\\service_b\\router.py:{50 + i}:    mapping = {{'host:port': '127.0.0.1:9000', 'id': {i}}}"
        for i in range(35)
    ]
    raw = "\n".join(lines)
    assert len(raw) > btc.GREP_THRESHOLD

    comp = btc.compress_grep_content(raw)
    assert comp is not None
    assert "70 matches across 2 files" in comp
    assert "C:\\Users\\dev\\workspace\\service_a\\config.py (35):" in comp
    assert "D:\\Repos\\project\\service_b\\router.py (35):" in comp
    assert "https://api.example.com:8443/v2:item_0" in comp
    assert "'host:port': '127.0.0.1:9000'" in comp


def test_repro_unparseable_line_fails_open():
    """Any unparseable line (or context line without flag) fails open -> returns None."""
    raw, _ = _make_grep_content(num_files=10, hits_per_file=8)
    raw_with_context = raw + "\nsrc/services/authentication/module_00/request_handler.py-11-    # context line"
    assert btc.compress_grep_content(raw_with_context) is None

    raw_with_sep = raw + "\n--\nsrc/services/authentication/module_01/request_handler.py:20:    next match"
    assert btc.compress_grep_content(raw_with_sep) is None


@pytest.mark.parametrize(
    "tool_input",
    [
        {"-A": 3},
        {"-B": 2},
        {"-C": 2},
        {"context": 3},
        {"head_limit": 50},
        {"offset": 10},
    ],
)
def test_repro_grep_context_flags_head_limit_offset_pass_through(tool_input):
    """Repro 2: Grep passes through untouched when any context flag or head_limit/offset is set."""
    raw, filenames = _make_grep_content(num_files=20, hits_per_file=8)
    assert btc.compress_grep_content(raw, tool_input=tool_input) is None

    raw_resp = {"mode": "content", "numFiles": len(filenames), "filenames": filenames, "content": raw}
    assert btc.build_builtin_updated_output(
        "Grep", raw_resp, raw, tool_input, "tu_ctx", None, len(raw), len(raw)
    ) is None


def test_repro_glob_100_paths_in_one_dir_lossless_and_numfiles_matches():
    """Repro 3: 100 paths in one dir -> all 100 basenames kept, numFiles == 100."""
    paths = [f"src/deeply/nested/workspace/module/file_{i:03d}.ts" for i in range(100)]
    glob_raw = {"durationMs": 15, "numFiles": 100, "filenames": paths, "truncated": False}
    res = btc.build_builtin_updated_output(
        "Glob", glob_raw, "\n".join(paths), {}, "tu_100", None,
        sum(len(p) + 1 for p in paths), sum(len(p) + 1 for p in paths)
    )
    assert res is not None
    upd, orig_field, rep_field = res
    assert upd["numFiles"] == 100
    joined = "\n".join(upd["filenames"])
    for i in range(100):
        assert f"file_{i:03d}.ts" in joined
    assert rep_field < orig_field * 0.75


def test_repro_webfetch_and_websearch_completely_out_of_scope():
    """Repro 4 & 6: WebFetch and WebSearch are never compressed or matched."""
    assert "WebFetch" not in btc.BUILTIN_COMPRESS_TOOLS
    assert "WebSearch" not in btc.BUILTIN_COMPRESS_TOOLS

    wf_raw = {
        "bytes": 10000, "code": 200, "codeText": "OK",
        "result": "x" * 10000, "durationMs": 100, "url": "https://example.com",
    }
    assert btc.build_builtin_updated_output("WebFetch", wf_raw, "x" * 10000, {}, "tu_wf", None, 10000, 10000) is None

    ws_raw = {"query": "test", "durationSeconds": 1.0, "results": ["y" * 10000]}
    assert btc.build_builtin_updated_output("WebSearch", ws_raw, "y" * 10000, {}, "tu_ws", None, 10000, 10000) is None

    import posttooluse_runner as runner
    assert not runner._matches(runner._MATCHER_ARCHIVE, "WebFetch")
    assert not runner._matches(runner._MATCHER_ARCHIVE, "WebSearch")
    hooks_json = json.loads((HOOKS_DIR / "hooks.json").read_text(encoding="utf-8"))
    ptu_matcher = hooks_json["hooks"]["PostToolUse"][0]["matcher"]
    assert "WebFetch" not in ptu_matcher and "WebSearch" not in ptu_matcher


# ---------------------------------------------------------------------------
# Safety, version gate, self-disable, expand wording, and measured savings
# ---------------------------------------------------------------------------

def test_safety_short_outputs_untouched():
    short_grep = "\n".join(f"a.py:{i}:match" for i in range(1, 10))
    assert btc.compress_grep_content(short_grep) is None
    short_glob = [f"src/file_{i}.py" for i in range(10)]
    assert btc.compress_glob_paths(short_glob) is None


def test_safety_errors_warnings_and_binaries_never_dropped():
    raw_grep, _ = _make_grep_content(num_files=14, hits_per_file=6)
    raw_grep += "\nrg: /etc/shadow: Permission denied (os error 13)\nBinary file assets/logo.png matches"
    comp_grep = btc.compress_grep_content(raw_grep)
    assert comp_grep is not None
    assert "Permission denied" in comp_grep
    assert "Binary file assets/logo.png matches" in comp_grep


def test_safety_fail_open_on_exception(capsys):
    def _boom(_text, **_kw):
        raise RuntimeError("simulated redactor failure")

    raw_content, filenames = _make_grep_content(num_files=12, hits_per_file=6)
    emitted = btc.maybe_emit_builtin_replacement(
        tool_name="Grep",
        raw_response={"mode": "content", "numFiles": 12, "filenames": filenames, "content": raw_content},
        safe_response=raw_content,
        hook_input={"tool_input": {}},
        tool_use_id="tu_err",
        raw_tool_use_id="tu_err",
        session_id="sess-failopen",
        original_char_count=len(raw_content),
        char_count=len(raw_content),
        truncated=False,
        code_chars_per_token=3.3,
        trends_db=Path("/nonexistent/trends.db"),
        log_savings_event_fn=None,
        is_exempt_fn=lambda _t: False,
        redact_fn=_boom,
    )
    assert emitted is False
    assert capsys.readouterr().out == ""


def test_version_gate_blocks_old_versions_and_allows_known_good(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_VERSION", "2.0.99")
    assert btc._is_version_supported({}) is False
    monkeypatch.setenv("CLAUDE_CODE_VERSION", "2.1.330")
    assert btc._is_version_supported({}) is True
    monkeypatch.delenv("CLAUDE_CODE_VERSION", raising=False)
    monkeypatch.setenv("AI_AGENT", "claude-code_2.0.5_harness")
    assert btc._is_version_supported({}) is False
    monkeypatch.setenv("AI_AGENT", "claude-code_2.1.330_harness")
    assert btc._is_version_supported({}) is True
    monkeypatch.delenv("AI_AGENT", raising=False)
    # Unknown version -> allow (rely on session self-disable)
    assert btc._is_version_supported({}) is True


def test_self_disable_after_first_host_rejection_logs_one_warning(tmp_path, monkeypatch, capsys):
    trends_db = tmp_path / "trends.db"
    # Create savings_events table so archive_verify.record() can link event_id (matches real trends.db).
    conn = sqlite3.connect(str(trends_db))
    conn.execute("CREATE TABLE savings_events (id INTEGER PRIMARY KEY, event_type TEXT, session_id TEXT, detail TEXT)")
    conn.commit()
    conn.close()
    transcript = tmp_path / "session.jsonl"
    raw_content, filenames = _make_grep_content(num_files=14, hits_per_file=6)
    raw_resp = {"mode": "content", "numFiles": len(filenames), "filenames": filenames, "content": raw_content}
    monkeypatch.delenv("TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_VERSION", raising=False)

    # Call 1: succeeds and records pending verification.
    ok1 = btc.maybe_emit_builtin_replacement(
        tool_name="Grep", raw_response=raw_resp, safe_response=raw_content,
        hook_input={"tool_input": {}, "transcript_path": str(transcript)},
        tool_use_id="tu_1", raw_tool_use_id="tu_1", session_id="sess-reject",
        original_char_count=len(raw_content), char_count=len(raw_content), truncated=False,
        code_chars_per_token=3.3, trends_db=trends_db,
        log_savings_event_fn=None, is_exempt_fn=lambda _t: False,
    )
    assert ok1 is True
    capsys.readouterr()

    # Simulate host rejecting tu_1: transcript contains the FULL original content (no marker).
    transcript.write_text(
        json.dumps({
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": raw_content}
                ]
            }
        }) + "\n",
        encoding="utf-8",
    )

    # Call 2: detects rejection, disables rest of session, logs ONE warning, emits nothing on stdout.
    ok2 = btc.maybe_emit_builtin_replacement(
        tool_name="Grep", raw_response=raw_resp, safe_response=raw_content,
        hook_input={"tool_input": {}, "transcript_path": str(transcript)},
        tool_use_id="tu_2", raw_tool_use_id="tu_2", session_id="sess-reject",
        original_char_count=len(raw_content), char_count=len(raw_content), truncated=False,
        code_chars_per_token=3.3, trends_db=trends_db,
        log_savings_event_fn=None, is_exempt_fn=lambda _t: False,
    )
    assert ok2 is False
    cap2 = capsys.readouterr()
    assert cap2.out == ""
    assert "disabling for the rest of this session" in cap2.err

    # Call 3: stays silently disabled (no replacement, no repeated warning).
    ok3 = btc.maybe_emit_builtin_replacement(
        tool_name="Grep", raw_response=raw_resp, safe_response=raw_content,
        hook_input={"tool_input": {}, "transcript_path": str(transcript)},
        tool_use_id="tu_3", raw_tool_use_id="tu_3", session_id="sess-reject",
        original_char_count=len(raw_content), char_count=len(raw_content), truncated=False,
        code_chars_per_token=3.3, trends_db=trends_db,
        log_savings_event_fn=None, is_exempt_fn=lambda _t: False,
    )
    assert ok3 is False
    cap3 = capsys.readouterr()
    assert cap3.out == ""
    assert cap3.err == ""


def test_measured_savings_computed_against_replaced_field_not_whole_dict():
    """Savings for Glob compare raw filenames chars vs replacement filenames chars (not whole dict)."""
    paths = _make_glob_paths(num_dirs=10, files_per_dir=15)
    glob_raw = {
        "durationMs": 42, "numFiles": len(paths), "filenames": paths,
        "truncated": False, "extraMetadata": "M" * 4000,
    }
    whole_dict_len = len(json.dumps(glob_raw))
    field_len = sum(len(p) + 1 for p in paths)
    assert whole_dict_len > field_len + 3500

    res = btc.build_builtin_updated_output(
        "Glob", glob_raw, json.dumps(glob_raw), {}, "tu_gl", None,
        whole_dict_len, whole_dict_len
    )
    assert res is not None
    _upd, orig_field, rep_field = res
    assert orig_field == field_len
    assert rep_field < orig_field


def test_expand_pointer_has_no_do_not_rerun_wording():
    paths = _make_glob_paths(num_dirs=10, files_per_dir=15)
    glob_raw = {"durationMs": 12, "numFiles": len(paths), "filenames": paths, "truncated": False}
    res = btc.build_builtin_updated_output(
        "Glob", glob_raw, "\n".join(paths), {}, "tu_ptr", None,
        sum(len(p) + 1 for p in paths), sum(len(p) + 1 for p in paths)
    )
    assert res is not None
    upd, _, _ = res
    footer = upd["filenames"][-1]
    assert "Full result archived" in footer
    assert "To view the full archived result" in footer
    assert "do not" not in footer.lower()


def test_end_to_end_archive_result_emits_updated_tool_output_and_logs_measured_savings(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_result, "SNAPSHOT_DIR", tmp_path)
    trends_db = tmp_path / "trends.db"
    monkeypatch.setattr(archive_result, "TRENDS_DB", trends_db)
    monkeypatch.delenv("TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_VERSION", raising=False)

    fake_secret = "AKIAIOSFODNN7EXAMPLE"
    grep_text, grep_files = _make_grep_content(16, 6)
    grep_text = f"{grep_files[0]}:5:    AWS_KEY = '{fake_secret}'\n" + grep_text
    payload = {
        "session_id": "sess-e2e-b1",
        "tool_name": "Grep",
        "tool_use_id": "toolu_b1_grep_01",
        "tool_input": {"pattern": "validate_session_token"},
        "tool_response": {
            "mode": "content",
            "numFiles": len(grep_files),
            "filenames": grep_files,
            "content": grep_text,
            "numLines": 97,
        },
    }
    monkeypatch.setattr(archive_result, "read_stdin_hook_input", lambda _max=None: payload)

    buf = io.StringIO()
    with redirect_stdout(buf):
        archive_result.archive_result(quiet=True)

    out_str = buf.getvalue().strip()
    assert out_str, "Expected updatedToolOutput JSON on stdout"
    envelope = json.loads(out_str)
    hso = envelope["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    assert "updatedToolOutput" in hso
    assert "updatedMCPToolOutput" not in hso
    content = hso["updatedToolOutput"]["content"]
    assert fake_secret not in content
    assert "Full result archived" in content
    assert "do not" not in content.lower()
    assert "toolu_b1_grep_01" in content

    # Raw archive file exists and is redacted
    archive_file = tmp_path / "tool-archive" / "sess-e2e-b1" / "toolu_b1_grep_01.json"
    assert archive_file.is_file()
    archived = json.loads(archive_file.read_text(encoding="utf-8"))
    assert fake_secret not in archived["response"]

    # Measured savings logged to trends.db savings_events against the replaced content field
    conn = sqlite3.connect(str(trends_db))
    rows = conn.execute(
        "SELECT event_type, tokens_saved, detail FROM savings_events WHERE session_id='sess-e2e-b1'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "builtin_grep"
    assert rows[0][1] > 0
    assert f"replaced Grep ({len(grep_text):,} chars ->" in rows[0][2]


def test_measured_features_appear_in_savings_json_labels_limits_and_dashboard(tmp_path, monkeypatch):
    """ADDENDUM: builtin_grep & builtin_glob appear in savings JSON with human labels, limits, and dashboard."""
    from datetime import datetime, timezone
    import measure
    from _measure import feature_health, savings

    trends_db = tmp_path / "trends.db"
    monkeypatch.setattr(measure, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(measure, "TRENDS_DB", trends_db)

    conn = measure._init_trends_db()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        conn.execute(
            "INSERT INTO savings_events (timestamp, event_type, tokens_saved, cost_saved_usd, detail, session_id, model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now_iso, "builtin_grep", 1200, 0.0036, "replaced Grep (10,000 chars -> 6,040 chars)", "real-sess-1", "claude-sonnet-4-6"),
        )
        conn.execute(
            "INSERT INTO savings_events (timestamp, event_type, tokens_saved, cost_saved_usd, detail, session_id, model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now_iso, "builtin_glob", 900, 0.0027, "replaced Glob (7,000 chars -> 4,030 chars)", "real-sess-1", "claude-sonnet-4-6"),
        )
        conn.commit()

        # Plan-limits capacity floor (_runway_saved_tokens) includes both measured features.
        runway_tokens = savings._runway_saved_tokens(conn, "2020-01-01T00:00:00")
        assert runway_tokens >= 2100
    finally:
        conn.close()

    # Savings summary JSON (measure.py savings --json) includes both with human labels.
    summary = measure._get_merged_savings(days=30)
    by_cat = summary["by_category"]
    assert "builtin_grep" in by_cat
    assert "builtin_glob" in by_cat
    assert by_cat["builtin_grep"]["label"] == "Built-in Grep compression"
    assert by_cat["builtin_glob"]["label"] == "Built-in Glob compression"
    assert by_cat["builtin_grep"]["tokens_saved"] == 1200
    assert by_cat["builtin_glob"]["tokens_saved"] == 900
    assert summary["total_tokens"] >= 2100

    # Registered in counted event types, feature health, dashboard assets, and METHODOLOGY.md.
    assert "builtin_grep" in measure._COUNTED_SE_EVENT_TYPES
    assert "builtin_glob" in measure._COUNTED_SE_EVENT_TYPES
    assert "builtin_grep" not in measure._RUNWAY_ESTIMATED_EVENT_TYPES
    assert "builtin_glob" not in measure._RUNWAY_ESTIMATED_EVENT_TYPES
    fh_keys = {f[0] for f in feature_health.FEATURES}
    assert {"builtin_grep", "builtin_glob"}.issubset(fh_keys)

    story_js = (REPO_ROOT / "skills" / "token-optimizer" / "assets" / "savings_story.js").read_text(encoding="utf-8")
    dash_html = (REPO_ROOT / "skills" / "token-optimizer" / "assets" / "dashboard.html").read_text(encoding="utf-8")
    methodology = (REPO_ROOT / "docs" / "METHODOLOGY.md").read_text(encoding="utf-8")
    for text in (story_js, dash_html):
        assert "builtin_grep" in text and "Built-in Grep compression" in text
        assert "builtin_glob" in text and "Built-in Glob compression" in text
    assert "builtin_grep" in methodology and "builtin_glob" in methodology


def test_benchmark_builtin_tool_fixtures_all_pass():
    import benchmark

    fixture_path = SCRIPTS_DIR / "fixtures" / "builtin_tools.json"
    fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert len(fixtures) >= 5
    for fx in fixtures:
        assert fx["tool"] in ("Grep", "Glob")
        passed, result = benchmark.run_single_fixture(fx, compressor=lambda _c, r, **_k: r)
        assert passed, f"Benchmark fixture failed: {fx['name']} -> {result}"
