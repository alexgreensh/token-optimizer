#!/usr/bin/env python3
"""Tests for built-in tool output compression (Grep, Glob, WebFetch, WebSearch)."""

from __future__ import annotations

import io
import json
import os
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
import builtin_tool_compress as btc


# ---------------------------------------------------------------------------
# Realistic fixture builders
# ---------------------------------------------------------------------------

def _make_grep_content(num_files: int = 24, hits_per_file: int = 8) -> tuple[str, list[str]]:
    lines = []
    filenames = []
    for f_idx in range(num_files):
        fname = f"src/services/module_{f_idx:02d}/handler.py"
        filenames.append(fname)
        for h_idx in range(hits_per_file):
            lno = 10 + h_idx * 12
            lines.append(
                f"{fname}:{lno}:    result = validate_session_token(request.headers['Authorization'], strict=True)  # check {h_idx}"
            )
    return "\n".join(lines), filenames


def _make_grep_count(num_files: int = 120) -> str:
    return "\n".join(
        f"packages/core/src/components/feature_{i:03d}/index.tsx:{(i % 17) + 1}"
        for i in range(num_files)
    )


def _make_glob_paths(num_dirs: int = 12, files_per_dir: int = 15) -> list[str]:
    exts = (".py", ".ts", ".tsx", ".json", ".md")
    paths = []
    for d in range(num_dirs):
        for f in range(files_per_dir):
            ext = exts[(d + f) % len(exts)]
            paths.append(f"apps/web/src/features/domain_{d:02d}/component_{f:02d}{ext}")
    return paths


def _make_webfetch_doc() -> str:
    nav_block = "\n".join([
        "Skip to main content",
        "[Home](https://docs.example.com/)",
        "[Pricing](https://docs.example.com/pricing)",
        "[Blog](https://docs.example.com/blog)",
        "[Careers](https://docs.example.com/careers)",
        "[Contact](https://docs.example.com/contact)",
        "Sign in",
        "Accept all cookies",
        "Privacy Policy",
        "Terms of Service",
        "All rights reserved.",
    ])
    repeated_sidebar = "\n".join([
        "Sidebar Item Alpha",
        "Sidebar Item Beta",
        "Sidebar Item Gamma",
    ] * 5)
    body_sections = [
        "# Claude Code Hooks Reference",
        "Hooks let you run custom shell commands at lifecycle events in Claude Code.",
        "## PostToolUse Decision Control",
        "Use `hookSpecificOutput.updatedToolOutput` to replace built-in tool results before the model sees them. "
        "The value must match the tool's Zod outputSchema, otherwise Claude Code logs a warning and falls back to the original output.",
        "See the [PostToolUse Schema Guide](https://docs.example.com/hooks/posttooluse-schema) and "
        "[Migration Notes](https://docs.example.com/hooks/updated-tool-output) for full field definitions.",
        "## Security and Fail-Open Behavior",
        "Every hook should fail open on unexpected input, never drop error lines, and redact credentials before writing archives to disk.",
    ]
    filler = "\n\n".join(
        f"### Appendix Section {i}\n"
        + f"General background paragraph {i} describing historical webhook delivery retries, "
        + "batching intervals, queue backoff tuning, and unrelated legacy CLI flags. " * 6
        for i in range(1, 18)
    )
    return "\n\n".join([nav_block, repeated_sidebar, "\n\n".join(body_sections), filler, nav_block, repeated_sidebar])


# ---------------------------------------------------------------------------
# Unit tests: Grep, Glob, WebFetch, WebSearch compressors
# ---------------------------------------------------------------------------

def test_grep_content_groups_by_file_with_counts_and_line_numbers():
    raw, _ = _make_grep_content(num_files=24, hits_per_file=8)
    comp = btc.compress_grep_content(raw, num_files=24, num_matches=192)
    assert comp is not None
    assert "192 matches across 24 files" in comp
    assert "src/services/module_00/handler.py (8 matches):" in comp
    assert "src/services/module_00/handler.py:10:" in comp
    assert "+5 more matches in src/services/module_00/handler.py" in comp
    assert "Other matching files (9 more files, counts):" in comp
    assert "src/services/module_23/handler.py: 8 matches" in comp
    assert len(comp) < len(raw) * 0.45


def test_grep_count_mode_summarizes_totals_and_top_files():
    raw = _make_grep_count(120)
    comp = btc.compress_grep_count(raw)
    assert comp is not None
    assert "120 files" in comp
    assert "+90 more files" in comp
    assert len(comp) < len(raw) * 0.40


def test_glob_paths_groups_by_directory_with_extension_counts():
    paths = _make_glob_paths(num_dirs=12, files_per_dir=15)
    summary = btc.compress_glob_paths(paths, total_matches=250, truncated=True)
    assert summary is not None
    joined = "\n".join(summary)
    assert "180 shown of 250 total files across 12 directories (truncated)" in joined
    assert ".py:" in joined and ".ts:" in joined and ".tsx:" in joined
    assert "apps/web/src/features/domain_00/" in joined
    raw_len = sum(len(p) + 1 for p in paths)
    assert len(joined) < raw_len * 0.30


def test_webfetch_strips_boilerplate_keeps_headings_links_and_query_body():
    raw = _make_webfetch_doc()
    assert len(raw) > 6144
    comp = btc.compress_web_text(
        raw,
        query="How does updatedToolOutput work in PostToolUse schema?",
        url="https://docs.example.com/hooks",
    )
    assert comp is not None
    assert "# Claude Code Hooks Reference" in comp
    assert "## PostToolUse Decision Control" in comp
    assert "updatedToolOutput" in comp
    assert "https://docs.example.com/hooks/posttooluse-schema" in comp
    assert "Accept all cookies" not in comp
    assert "Sidebar Item Alpha" not in comp
    assert "All rights reserved." not in comp
    assert len(comp) < len(raw) * 0.50


# ---------------------------------------------------------------------------
# Safety regression tests (one per RULES-BUILD safety rule)
# ---------------------------------------------------------------------------

def test_safety_short_outputs_untouched():
    short_grep = "\n".join(f"a.py:{i}:match" for i in range(1, 10))
    assert btc.compress_grep_content(short_grep) is None
    short_glob = [f"src/file_{i}.py" for i in range(10)]
    assert btc.compress_glob_paths(short_glob) is None
    short_web = "# Title\nShort page body under 6KB.\n" * 20
    assert len(short_web) < 6144
    assert btc.compress_web_text(short_web, query="Title") is None


def test_safety_errors_warnings_and_http_status_never_dropped():
    raw_grep, _ = _make_grep_content(num_files=18, hits_per_file=6)
    raw_grep += "\nrg: /etc/shadow: Permission denied (os error 13)\nBinary file assets/logo.png matches"
    comp_grep = btc.compress_grep_content(raw_grep)
    assert comp_grep is not None
    assert "Permission denied" in comp_grep
    assert "Binary file assets/logo.png matches" in comp_grep

    raw_web = _make_webfetch_doc() + "\n\nHTTP/1.1 429 Too Many Requests\nError: rate limit exceeded for token\n"
    comp_web = btc.compress_web_text(raw_web, query="updatedToolOutput")
    assert comp_web is not None
    assert "429 Too Many Requests" in comp_web
    assert "Error: rate limit exceeded" in comp_web


def test_safety_fail_open_on_exception(capsys):
    def _boom(_text, **_kw):
        raise RuntimeError("simulated redactor failure")

    emitted = btc.maybe_emit_builtin_replacement(
        tool_name="Grep",
        raw_response={"mode": "content", "numFiles": 20, "filenames": ["a.py"], "content": "x" * 9000},
        safe_response="x" * 9000,
        hook_input={"tool_input": {}},
        tool_use_id="tu_err",
        raw_tool_use_id="tu_err",
        session_id="sess-failopen",
        original_char_count=9000,
        char_count=9000,
        truncated=False,
        expand_instruction="expand tu_err",
        code_chars_per_token=3.3,
        trends_db=Path("/nonexistent/trends.db"),
        log_savings_event_fn=None,
        is_exempt_fn=lambda _t: False,
        redact_fn=_boom,
    )
    assert emitted is False
    assert capsys.readouterr().out == ""


def test_safety_respects_no_updated_tool_output_and_exempt(monkeypatch, capsys):
    raw_content, filenames = _make_grep_content(num_files=20, hits_per_file=6)
    raw_resp = {"mode": "content", "numFiles": len(filenames), "filenames": filenames, "content": raw_content}

    monkeypatch.setenv("TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT", "1")
    assert btc.maybe_emit_builtin_replacement(
        tool_name="Grep", raw_response=raw_resp, safe_response=raw_content,
        hook_input={}, tool_use_id="tu_1", raw_tool_use_id="tu_1", session_id="s1",
        original_char_count=len(raw_content), char_count=len(raw_content), truncated=False,
        expand_instruction="expand tu_1", code_chars_per_token=3.3, trends_db=Path("/tmp/x.db"),
        log_savings_event_fn=None, is_exempt_fn=lambda _t: False,
    ) is False
    monkeypatch.delenv("TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT", raising=False)

    assert btc.maybe_emit_builtin_replacement(
        tool_name="Grep", raw_response=raw_resp, safe_response=raw_content,
        hook_input={}, tool_use_id="tu_2", raw_tool_use_id="tu_2", session_id="s1",
        original_char_count=len(raw_content), char_count=len(raw_content), truncated=False,
        expand_instruction="expand tu_2", code_chars_per_token=3.3, trends_db=Path("/tmp/x.db"),
        log_savings_event_fn=None, is_exempt_fn=lambda t: t == "Grep",
    ) is False
    assert capsys.readouterr().out == ""


def test_safety_schema_shapes_match_claude_code_zod_schemas():
    # 1. Grep (W8r): numFiles int, filenames list[str], content str
    grep_text, grep_files = _make_grep_content(20, 6)
    grep_raw = {"mode": "content", "numFiles": 20, "filenames": grep_files, "content": grep_text, "numLines": 120}
    res = btc.build_builtin_updated_output(
        "Grep", grep_raw, grep_text, {}, "tu_g", "expand tu_g", len(grep_text), len(grep_text)
    )
    assert res is not None
    upd, _ = res
    assert isinstance(upd["numFiles"], int) and upd["numFiles"] == 20
    assert isinstance(upd["filenames"], list) and all(isinstance(x, str) for x in upd["filenames"])
    assert isinstance(upd["content"], str) and "Full result archived" in upd["content"] and "expand tu_g" in upd["content"]

    # 2. Glob (L8r): durationMs number, numFiles int, filenames list[str], truncated bool
    glob_paths = _make_glob_paths(10, 15)
    glob_raw = {"durationMs": 42, "numFiles": len(glob_paths), "filenames": glob_paths, "truncated": False}
    res = btc.build_builtin_updated_output(
        "Glob", glob_raw, "\n".join(glob_paths), {}, "tu_gl", "expand tu_gl",
        sum(len(p) + 1 for p in glob_paths), sum(len(p) + 1 for p in glob_paths)
    )
    assert res is not None
    upd, _ = res
    assert upd["durationMs"] == 42 and upd["numFiles"] == len(glob_paths) and upd["truncated"] is False
    assert isinstance(upd["filenames"], list) and all(isinstance(x, str) for x in upd["filenames"])
    assert any("Full result archived" in ln for ln in upd["filenames"])

    # 3. WebFetch (q_s): bytes, code, codeText, result, durationMs, url
    web_doc = _make_webfetch_doc()
    wf_raw = {
        "bytes": len(web_doc), "code": 200, "codeText": "OK",
        "result": web_doc, "durationMs": 310, "url": "https://docs.example.com/hooks",
    }
    res = btc.build_builtin_updated_output(
        "WebFetch", wf_raw, web_doc, {"prompt": "PostToolUse updatedToolOutput"},
        "tu_wf", "expand tu_wf", len(web_doc), len(web_doc)
    )
    assert res is not None
    upd, _ = res
    assert upd["code"] == 200 and upd["codeText"] == "OK" and upd["url"] == wf_raw["url"]
    assert "Full result archived" in upd["result"] and "expand tu_wf" in upd["result"]

    # 4. WebSearch (sl): query str, results list[dict|str], durationSeconds number
    ws_raw = {
        "query": "Claude Code PostToolUse updatedToolOutput",
        "durationSeconds": 1.8,
        "results": [
            {
                "tool_use_id": "srvtoolu_1",
                "content": [
                    {"title": "Hooks Reference", "url": "https://code.claude.com/docs/en/hooks"},
                ],
            },
            web_doc,
        ],
    }
    ws_text = json.dumps(ws_raw)
    res = btc.build_builtin_updated_output(
        "WebSearch", ws_raw, ws_text, {"query": ws_raw["query"]},
        "tu_ws", "expand tu_ws", len(ws_text), len(ws_text)
    )
    assert res is not None
    upd, _ = res
    assert upd["query"] == ws_raw["query"] and upd["durationSeconds"] == 1.8
    assert isinstance(upd["results"], list)
    # Citation object preserved intact
    assert upd["results"][0]["content"][0]["url"] == "https://code.claude.com/docs/en/hooks"
    assert any(isinstance(x, str) and "Full result archived" in x for x in upd["results"])


# ---------------------------------------------------------------------------
# End-to-end PostToolUse integration (archive_result + savings_events + redaction)
# ---------------------------------------------------------------------------

def test_end_to_end_archive_result_emits_updated_tool_output_and_logs_measured_savings(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_result, "SNAPSHOT_DIR", tmp_path)
    trends_db = tmp_path / "trends.db"
    monkeypatch.setattr(archive_result, "TRENDS_DB", trends_db)
    monkeypatch.delenv("TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT", raising=False)

    # Include a fake AWS key to verify credential redaction runs before emit & archive.
    fake_secret = "AKIAIOSFODNN7EXAMPLE"
    grep_text, grep_files = _make_grep_content(20, 6)
    grep_text = f"src/services/module_00/handler.py:5:    AWS_KEY = '{fake_secret}'\n" + grep_text
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
            "numLines": 121,
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
    assert fake_secret not in hso["updatedToolOutput"]["content"]
    assert "Full result archived" in hso["updatedToolOutput"]["content"]
    assert "toolu_b1_grep_01" in hso["updatedToolOutput"]["content"]

    # Raw archive file exists and is redacted
    archive_file = tmp_path / "tool-archive" / "sess-e2e-b1" / "toolu_b1_grep_01.json"
    assert archive_file.is_file()
    archived = json.loads(archive_file.read_text(encoding="utf-8"))
    assert fake_secret not in archived["response"]

    # Measured savings logged to trends.db savings_events as tool_archive
    conn = sqlite3.connect(str(trends_db))
    rows = conn.execute(
        "SELECT event_type, tokens_saved, detail FROM savings_events WHERE session_id='sess-e2e-b1'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "tool_archive"
    assert rows[0][1] > 0
    assert "replaced Grep" in rows[0][2]


def test_posttooluse_runner_matches_webfetch_and_websearch():
    import posttooluse_runner as runner

    assert runner._matches(runner._MATCHER_ARCHIVE, "WebFetch")
    assert runner._matches(runner._MATCHER_ARCHIVE, "WebSearch")
    assert runner._matches(runner._MATCHER_QUALITY_CACHE, "WebFetch")
    assert runner._matches(runner._MATCHER_QUALITY_CACHE, "WebSearch")

    hooks_json = json.loads((HOOKS_DIR / "hooks.json").read_text(encoding="utf-8"))
    ptu_matcher = hooks_json["hooks"]["PostToolUse"][0]["matcher"]
    assert "WebFetch" in ptu_matcher and "WebSearch" in ptu_matcher


def test_benchmark_builtin_tool_fixtures_all_pass():
    import benchmark

    fixture_path = SCRIPTS_DIR / "fixtures" / "builtin_tools.json"
    fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert len(fixtures) >= 5
    for fx in fixtures:
        passed, result = benchmark.run_single_fixture(fx, compressor=lambda _c, r, **_k: r)
        assert passed, f"Benchmark fixture failed: {fx['name']} -> {result}"
