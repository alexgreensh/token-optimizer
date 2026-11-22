#!/usr/bin/env python3
"""Visibility rule tests across EVERY compressor: (a), (b), (c), (d) + grep 'error' distinction."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _run_bash_hook(monkeypatch, tmp_path, command: str, stdout: str, stderr: str = "", exit_code: int = 0, session_id: str = "vis-sess"):
    import archive_result
    import bash_compress_hook

    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)
    monkeypatch.setattr(archive_result, "SNAPSHOT_DIR", tmp_path / "snap")
    monkeypatch.setattr(archive_result, "TRENDS_DB", tmp_path / "snap" / "trends.db")

    payload = {
        "tool_name": "Bash",
        "session_id": session_id,
        "tool_use_id": f"tu_{abs(hash((command, len(stdout)))) % 100000}",
        "tool_input": {"command": command},
        "tool_response": {"stdout": stdout, "stderr": stderr, "exit_code": exit_code},
    }
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    bash_compress_hook._run(payload)
    raw = buf.getvalue().strip()
    if not raw:
        return None
    data = json.loads(raw)
    return data["hookSpecificOutput"]["updatedToolOutput"]["stdout"]


# ---------------------------------------------------------------------------
# 1. bash_search_results (existing search renderer on main + noise folding)
# ---------------------------------------------------------------------------

class TestBashSearchResultsVisibility:
    def test_a_error_and_exception_nonfile_lines_stay_verbatim(self):
        import bash_search_results

        files = {"src/x.py": [f"src/x.py:{i * 10}:val = {i}" for i in range(1, 25)]}
        no_file = [f"info note {i}" for i in range(15)] + [
            "rg: /restricted/secret: Permission denied (os error 13)",
            "ValueError: invalid regex flag",
            "TimeoutException: search worker timed out",
            "Process exited with code 2",
        ]
        out = bash_search_results.render_search_results(files, no_file, command_str="rg val src/")
        assert "rg: /restricted/secret: Permission denied (os error 13)" in out
        assert "ValueError: invalid regex flag" in out
        assert "TimeoutException: search worker timed out" in out
        assert "Process exited with code 2" in out

    def test_b_and_c_exact_count_place_line_range_and_expand_command(self, monkeypatch, tmp_path):
        lines = [f"src/x.py:{i}:match_item_{i} = {i}" for i in range(10, 46)]  # 36 matches: 3 shown, 33 hidden (lines 13-45)
        raw = "\n".join(lines) + "\n"
        out = _run_bash_hook(monkeypatch, tmp_path, "rg match_item src/", raw)
        assert out is not None
        assert "33 more matches in src/x.py (lines 13-45)" in out
        assert "measure.py expand " in out
        # Determinism
        out2 = _run_bash_hook(monkeypatch, tmp_path, "rg match_item src/", raw)
        assert out == out2

    def test_d_archive_write_failure_and_prune_return_original(self, monkeypatch, tmp_path):
        import archive_result

        raw = "\n".join(f"src/x.py:{i}:match_item_{i} = {i}" for i in range(1, 45)) + "\n"
        # Write fails -> archive_original returns None -> original returned
        monkeypatch.setattr(archive_result, "archive_original", lambda *a, **k: None)
        out = _run_bash_hook(monkeypatch, tmp_path, "rg match_item src/", raw)
        assert out is None or out == raw

        # Pruned -> archive_entry_exists returns False -> original returned
        monkeypatch.setattr(archive_result, "archive_original", lambda *a, **k: "deadbeef12345678")
        monkeypatch.setattr(archive_result, "archive_entry_exists", lambda *a, **k: False)
        out_pruned = _run_bash_hook(monkeypatch, tmp_path, "rg match_item src/", raw)
        assert out_pruned is None or out_pruned == raw

    def test_grep_literal_error_in_source_code_still_compresses(self, monkeypatch, tmp_path):
        # Every hit contains literal "error:" / "ValueError" in source code,
        # but the search itself succeeded (exit 0, empty stderr). Must compress!
        lines = [f"src/mod.py:{i * 5}:    logger.error: ValueError('bad input {i}')" for i in range(1, 45)]
        raw = "\n".join(lines) + "\n"
        out = _run_bash_hook(monkeypatch, tmp_path, 'rg "error:" src/', raw)
        assert out is not None
        assert len(out) < len(raw)
        assert "41 more matches in src/mod.py (lines 20-220)" in out
        assert "measure.py expand " in out


# ---------------------------------------------------------------------------
# 2. pipe_compress & bash_reread_unchanged
# ---------------------------------------------------------------------------

class TestPipeCompressVisibility:
    def test_a_exception_and_nonzero_exit_preserved(self):
        import pipe_file_compress

        raw = "\n".join([f"src/a.py:{i}:item_{i}" for i in range(1, 40)] + [
            "TimeoutException: pipeline stage stalled",
            "exit status 2",
        ])
        # Non-zero exit returns None (original verbatim)
        assert pipe_file_compress.compress_pipe("rg item src/ | sort", raw, returncode=2) is None
        # Exit 0 with non-match exception lines: those lines stay visible verbatim
        comp = pipe_file_compress.compress_pipe("rg item src/ | sort", raw, returncode=0)
        assert comp is not None
        assert "TimeoutException: pipeline stage stalled" in comp
        assert "exit status 2" in comp

    def test_b_count_place_and_expand_command_and_literal_error_pipe(self, monkeypatch, tmp_path):
        lines = [f"src/a.py:{i}:    log.error: failed check {i}" for i in range(1, 45)]
        raw = "\n".join(lines) + "\n"
        out = _run_bash_hook(monkeypatch, tmp_path, 'rg "error:" src/ | sort', raw)
        assert out is not None
        assert "41 more matches in src/a.py (lines 4-44)" in out
        assert "measure.py expand " in out

    def test_d_archive_failure_and_prune_return_none(self, monkeypatch, tmp_path):
        import archive_result
        import pipe_file_compress

        raw = "\n".join(f"src/a.py:{i}:item_{i}" for i in range(1, 45))
        monkeypatch.setattr(archive_result, "archive_original", lambda *a, **k: None)
        assert pipe_file_compress.hook_try_pipe_compress("rg item src/ | sort", raw, 0, "") is None

        monkeypatch.setattr(archive_result, "archive_original", lambda *a, **k: "abc1234567890123")
        monkeypatch.setattr(archive_result, "archive_entry_exists", lambda *a, **k: False)
        assert pipe_file_compress.hook_try_pipe_compress("rg item src/ | sort", raw, 0, "") is None


# ---------------------------------------------------------------------------
# 3. command_compress_extra: gh list, gh run log, terraform, kubectl, pkg_install, mypy
# ---------------------------------------------------------------------------

class TestCommandCompressExtraVisibility:
    def test_gh_list_a_b_d(self, monkeypatch, tmp_path):
        import archive_result
        import command_compress_extra as cce

        rows = [f"completed\tsuccess\tCI build #{i}\tmain\tpush\t1000{i}\t2m" for i in range(1, 28)]
        rows[20] = "completed\tunknown\tJob hit ValueError on shard 3\tmain\tpush\t10021\t1m"
        rows[22] = "completed\tunknown\tJob hit TimeoutException exit code 1\tmain\tpush\t10023\t1m"
        raw = "\n".join(rows) + "\n"

        # (a) exception & non-zero exit rows stay visible verbatim
        comp = cce.compress_gh_list(raw)
        assert comp is not None
        assert rows[20] in comp
        assert rows[22] in comp
        # (b) count + place (line range)
        assert "more rows (lines 13-27 of 27 total)" in comp

        # (b)+(d) through hook: expand command present, and archive failure returns original
        out = _run_bash_hook(monkeypatch, tmp_path, "gh run list", raw)
        assert out is not None and "measure.py expand " in out

        monkeypatch.setattr(archive_result, "archive_original", lambda *a, **k: None)
        out_fail = _run_bash_hook(monkeypatch, tmp_path, "gh run list", raw)
        assert out_fail is None or out_fail == raw

        monkeypatch.setattr(archive_result, "archive_original", lambda *a, **k: "k" * 16)
        monkeypatch.setattr(archive_result, "archive_entry_exists", lambda *a, **k: False)
        out_pruned = _run_bash_hook(monkeypatch, tmp_path, "gh run list", raw)
        assert out_pruned is None or out_pruned == raw

    def test_gh_run_log_a_b_d(self, monkeypatch, tmp_path):
        import archive_result
        import command_compress_extra as cce

        lines = [f"job\tSet up job\t2026-11-22T00:00:{i:02d}Z Prepare workflow directory {i}" for i in range(1, 25)]
        lines.append("job\tTest\t2026-11-22T00:01:01Z ValueError: bad config")
        lines.append("job\tTest\t2026-11-22T00:01:02Z Process completed with exit code 1")
        raw = "\n".join(lines) + "\n"

        comp = cce.compress_gh_run_log(raw)
        assert comp is not None
        assert "ValueError: bad config" in comp
        assert "Process completed with exit code 1" in comp
        assert "24 CI setup/boilerplate lines omitted (lines 1-24)" in comp

        monkeypatch.setattr(archive_result, "archive_entry_exists", lambda *a, **k: False)
        out_pruned = _run_bash_hook(monkeypatch, tmp_path, "gh run view 123 --log", raw)
        assert out_pruned is None or out_pruned == raw

    def test_terraform_plan_a_b_d(self, monkeypatch, tmp_path):
        import archive_result
        import command_compress_extra as cce

        lines = [f"aws_instance.web[{i}]: Refreshing state... [id=i-{i:04d}]" for i in range(1, 22)]
        lines += [
            "Terraform will perform the following actions:",
            "  # aws_instance.web[0] will be updated in-place",
            "  ~ resource \"aws_instance\" \"web\" {",
            "      id = (known after apply)",
            "      arn = (known after apply)",
            "      TimeoutException: provider refresh slow",
            "      ~ instance_type = \"t3.micro\" -> \"t3.small\"",
            "    }",
            "Plan: 0 to add, 1 to change, 0 to destroy.",
        ]
        raw = "\n".join(lines) + "\n"
        comp = cce.compress_terraform_plan(raw)
        assert comp is not None
        assert "TimeoutException: provider refresh slow" in comp
        assert "21 state-refresh/unchanged lines collapsed (lines 1-21)" in comp

        monkeypatch.setattr(archive_result, "archive_original", lambda *a, **k: None)
        out_fail = _run_bash_hook(monkeypatch, tmp_path, "terraform plan", raw)
        assert out_fail is None or out_fail == raw

    def test_kubectl_describe_a_b_d(self, monkeypatch, tmp_path):
        import archive_result
        import command_compress_extra as cce

        lines = [
            "Name:         api-pod-1",
            "Namespace:    prod",
            "Status:       Running",
            "Annotations:",
        ] + [f"  ann.example.com/k{i}: value-{i}" for i in range(1, 20)] + [
            "  ann.example.com/boom: ValueError in webhook",
            "  ann.example.com/k21: value-21",
            "Events: <none>",
        ]
        raw = "\n".join(lines) + "\n"
        comp = cce.compress_kubectl_describe(raw)
        assert comp is not None
        assert "ValueError in webhook" in comp
        assert "18 more Annotations lines collapsed, lines 7-25" in comp

        monkeypatch.setattr(archive_result, "archive_original", lambda *a, **k: None)
        out_fail = _run_bash_hook(monkeypatch, tmp_path, "kubectl describe pod api-pod-1", raw)
        assert out_fail is None or out_fail == raw

    def test_pkg_install_a_b_d(self, monkeypatch, tmp_path):
        import archive_result
        import command_compress_extra as cce

        lines = [f"Downloading pkg_{i}-1.0.0-py3-none-any.whl (120 kB)" for i in range(1, 20)]
        lines.append("TimeoutException: mirror retry succeeded on fallback")
        lines.append("Successfully installed pkg_1")
        raw = "\n".join(lines) + "\n"
        comp = cce.compress_package_install(raw)
        assert comp is not None
        assert "TimeoutException: mirror retry succeeded on fallback" in comp
        assert "19 download/progress lines collapsed (lines 1-19)" in comp

        monkeypatch.setattr(archive_result, "archive_entry_exists", lambda *a, **k: False)
        out_pruned = _run_bash_hook(monkeypatch, tmp_path, "pip install foo", raw)
        assert out_pruned is None or out_pruned == raw

    def test_mypy_a_b_d(self, monkeypatch, tmp_path):
        import archive_result
        import command_compress_extra as cce

        lines = [
            'src/a.py:10: error: No overload variant of "foo" matches argument type "int"',
            "src/a.py:10: note: Possible overload variants:",
        ] + [f"src/a.py:10: note:     def foo(x: str, y: int = {i}) -> str" for i in range(1, 16)] + [
            "src/a.py:20: error: ValueError raised in plugin",
            "Found 2 errors in 1 file",
        ]
        raw = "\n".join(lines) + "\n"
        comp = cce.compress_mypy(raw)
        assert comp is not None
        assert "ValueError raised in plugin" in comp
        assert "16 mypy overload variant notes collapsed, lines 2-17" in comp

        monkeypatch.setattr(archive_result, "archive_original", lambda *a, **k: None)
        out_fail = _run_bash_hook(monkeypatch, tmp_path, "mypy src/", raw)
        assert out_fail is None or out_fail == raw


# ---------------------------------------------------------------------------
# 4. big_read_preview
# ---------------------------------------------------------------------------

class TestBigReadPreviewVisibility:
    def test_a_b_c_d_big_read_preview(self, monkeypatch, tmp_path):
        import archive_result
        import big_noncode_read

        monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
        monkeypatch.setattr(archive_result, "SNAPSHOT_DIR", tmp_path / "snap")

        csv_file = tmp_path / "events.csv"
        rows = ["id,msg,payload"] + [f"{i},ok,{'x' * 90}" for i in range(1, 600)]
        rows[250] = "250,TimeoutException: upstream gateway stalled,payload"
        rows[350] = "350,ValueError: corrupt row checksum,payload"
        content = "\n".join(rows) + "\n"
        csv_file.write_text(content, encoding="utf-8")

        # (a) + (b) + (c): error lines preserved verbatim, exact omitted count + place, expand command
        res = big_noncode_read.build_big_noncode_preview(str(csv_file), content, archive_key="deadbeef12345678")
        assert res is not None
        ctx = res["context"]
        assert "TimeoutException: upstream gateway stalled" in ctx
        assert "ValueError: corrupt row checksum" in ctx
        # 600 lines total: head=8, tail=5, 2 preserved middle errors -> 585 omitted
        assert "585 lines omitted in events.csv, lines 9-595" in ctx
        assert "measure.py expand deadbeef12345678" in ctx

        # (d): archive write fails OR pruned -> try_big_noncode_read returns False (original file read stands)
        st = csv_file.stat()
        monkeypatch.setattr(archive_result, "archive_original", lambda *a, **k: None)
        assert big_noncode_read.try_big_noncode_read(str(csv_file), st, 0, 0, "sid", None, "block", {}) is False

        monkeypatch.setattr(archive_result, "archive_original", lambda *a, **k: "deadbeef12345678")
        monkeypatch.setattr(archive_result, "archive_entry_exists", lambda *a, **k: False)
        assert big_noncode_read.try_big_noncode_read(str(csv_file), st, 0, 0, "sid", None, "block", {}) is False


# ---------------------------------------------------------------------------
# 5. subagent_cap
# ---------------------------------------------------------------------------

class TestSubagentCapVisibility:
    def test_a_b_c_d_subagent_cap(self):
        import subagent_result_cap as cap

        body_lines = [f"Regular exploration note line {i}: " + ("a" * 80) for i in range(1, 200)]
        body_lines[90] = "Encountered ValueError while parsing manifest"
        body_lines[110] = "Worker hit TimeoutException after 30s (exit code 1)"
        text = "\n".join(body_lines)

        # (a) + (b) + (c)
        out = cap.cap_subagent_result(text, expand_instruction="python3 measure.py expand toolu_sub1")
        assert out is not None
        assert "Encountered ValueError while parsing manifest" in out
        assert "Worker hit TimeoutException after 30s (exit code 1)" in out
        assert "middle lines archived (lines " in out
        assert "python3 measure.py expand toolu_sub1" in out

        # (d): entry_exists=False (write failed or pruned) -> returns False (original stands)
        emitted = cap.emit_if_capped(
            tool_name="Task",
            raw_response=text,
            safe_response=text,
            original_char_count=len(text),
            tool_use_id="toolu_sub1",
            raw_tool_use_id="toolu_sub1",
            session_id="sid",
            entry_exists=False,
            expand_instruction="python3 measure.py expand toolu_sub1",
            chars_per_token=3.3,
            trends_db=Path("/nonexistent"),
            transcript_path=None,
            log_savings_fn=lambda *a, **k: None,
        )
        assert emitted is False


# ---------------------------------------------------------------------------
# 6. builtin_grep / builtin_glob
# ---------------------------------------------------------------------------

class TestBuiltinToolCompressVisibility:
    def test_a_b_d_builtin_grep_and_glob(self):
        import builtin_tool_compress as btc

        # (a): long line with ValueError/TimeoutException is NOT trimmed
        long_err = "src/a.py:10:" + ("prefix " * 30) + "TimeoutException: keep full line verbatim"
        lines = [long_err] + [
            f"src/file_{i % 6}.py:{i}:" + ("ordinary match text " * 12) for i in range(1, 60)
        ]
        raw = "\n".join(lines)
        comp = btc.compress_grep_content(raw, num_files=6, num_matches=60)
        assert comp is not None
        assert "TimeoutException: keep full line verbatim" in comp
        assert "[trimmed," in comp

        # (b): built updatedToolOutput includes exact runnable expand command
        built = btc.build_builtin_updated_output(
            tool_name="Grep",
            raw_response={"mode": "content", "filenames": [f"src/file_{i}.py" for i in range(6)], "numFiles": 6, "numMatches": 60, "content": raw},
            safe_response=raw,
            tool_input={"pattern": "ordinary"},
            tool_use_id="toolu_grep1",
            expand_instruction="python3 measure.py expand toolu_grep1",
            original_char_count=len(raw),
            char_count=len(raw),
        )
        assert built is not None
        assert "python3 measure.py expand toolu_grep1" in built[0]["content"]


# ---------------------------------------------------------------------------
# 7. crossturn_dedup (bash_rerun_dedupe)
# ---------------------------------------------------------------------------

class TestBashRerunDedupeVisibility:
    def test_a_b_d_rerun_dedupe(self, monkeypatch, tmp_path):
        import archive_result
        import bash_rerun_dedupe

        monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
        monkeypatch.setattr(archive_result, "SNAPSHOT_DIR", tmp_path / "snap")
        raw = "\n".join([f"status line {i}: healthy" for i in range(1, 40)] + ["ValueError: stale cache entry"]) + "\n"

        # First run records, returns None
        first = bash_rerun_dedupe.dedupe_rerun("my-check", raw, session_id="s1", store_id="s1", tool_use_id="t1")
        assert first is None
        # Second identical run preserves ValueError line verbatim + states exact hidden count & place
        second = bash_rerun_dedupe.dedupe_rerun("my-check", raw, session_id="s1", store_id="s1", tool_use_id="t2")
        assert second is not None
        assert "ValueError: stale cache entry" in second
        assert "39 lines (lines 1-40) hidden" in second


# ---------------------------------------------------------------------------
# 8. build_output_compress
# ---------------------------------------------------------------------------

class TestBuildOutputCompressVisibility:
    def test_a_b_d_build_output_compress(self, monkeypatch, tmp_path):
        import archive_result
        import build_output_compress

        lines = (
            [f"   Downloading crate_{i} v0.1.0" for i in range(1, 35)]
            + [f"   Compiling module_{i} v0.1.0" for i in range(1, 120)]
        )
        lines[80] = "ValueError: unexpected linker flag"
        lines[85] = "TimeoutException: build daemon timeout"
        lines.append("Finished dev [unoptimized] target(s) in 3.2s")
        raw = "\n".join(lines) + "\n"

        comp = build_output_compress.compress("cargo build", raw)
        assert comp is not None
        assert "ValueError: unexpected linker flag" in comp
        assert "TimeoutException: build daemon timeout" in comp
        assert "151 progress lines collapsed (lines 1-153)" in comp

        # (d): archive failure -> returns raw (None from hook)
        monkeypatch.setattr(archive_result, "archive_original", lambda *a, **k: None)
        out_fail = _run_bash_hook(monkeypatch, tmp_path, "cargo build", raw)
        assert out_fail is None or out_fail == raw
