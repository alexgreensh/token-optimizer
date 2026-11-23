"""Tests for emit_safety_gate: per-failure-class checks, wired paths, and replay_v2 agreement."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import bash_compress
import bash_compress_hook
import big_noncode_read
import builtin_tool_compress
import command_compress_extra
import emit_safety_gate as esg
import large_output_policy
import pipe_file_compress

EXPAND_PTR = (
    "\n\n[Full result archived (1,200 chars) — saved to disk, not lost.\n"
    "To view the full archived result, run in Bash:\n"
    "    python3 /opt/to/scripts/measure.py expand abcdef0123456789\n]"
)


# ---------------------------------------------------------------------------
# 1. Per-failure-class tests (critical lines, undeclared hiding, expand, raises)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "crit_line,expected_reason",
    [
        ("Traceback (most recent call last):", "hidden_traceback"),
        ('  File "src/app.py", line 42, in run', "hidden_traceback"),
        ("    at handleReq (server.js:18:9)", "hidden_traceback"),
        ("goroutine 7 [running]:", "hidden_traceback"),
        ("Process exited with exit code 2", "hidden_exit_code"),
        ("Error: Exit code 1", "hidden_exit_code"),
        ("FAILED tests/test_auth.py::test_login", "hidden_failing_test"),
        ("--- FAIL: TestConnect (0.04s)", "hidden_failing_test"),
        ("AssertionError: expected 200 got 500", "hidden_failing_test"),
        ("panic: runtime error: index out of range", "hidden_panic_fatal"),
        ("fatal: unable to access remote", "hidden_panic_fatal"),
        ("Segmentation fault (core dumped)", "hidden_panic_fatal"),
        ("ValueError: invalid literal for int()", "hidden_error"),
        ("parse_error: unexpected token at col 4", "hidden_error"),
    ],
)
def test_critical_line_hidden_is_rejected(crit_line, expected_reason):
    raw = "\n".join(
        [f"ok line {i}" for i in range(1, 16)]
        + [crit_line]
        + [f"ok line {i}" for i in range(17, 35)]
    )
    comp = (
        "ok line 1\nok line 2\n"
        "... 30 lines omitted (lines 3-32) ...\n"
        "ok line 33\nok line 34"
        + EXPAND_PTR
    )
    status, reasons = esg.classify_output(raw, comp, "bash_compress_pipeline", tool_name="Bash", command="make")
    assert status == esg.CRITICAL_HIDDEN
    assert expected_reason in reasons
    assert esg.gate_or_raw(raw, comp, feature="bash_compress_pipeline", tool_name="Bash", command="make") == raw
    assert esg.gate_or_none(raw, comp, feature="bash_compress_pipeline", tool_name="Bash", command="make") is None


def test_nonzero_exit_code_arg_hidden_is_rejected():
    raw = "\n".join(f"step {i} done" for i in range(1, 31))
    comp = "step 1 done\n... 28 lines omitted (lines 2-29) ...\nstep 30 done" + EXPAND_PTR
    status, reasons = esg.classify_output(raw, comp, "bash_compress_pipeline", tool_name="Bash", command="run", exit_code=2)
    assert status == esg.CRITICAL_HIDDEN
    assert "hidden_exit_code" in reasons
    assert esg.gate_or_raw(raw, comp, feature="bash_compress_pipeline", tool_name="Bash", command="run", exit_code=2) == raw


def test_undeclared_missing_count_place_and_expand_are_rejected():
    raw = "\n".join(f"item {i}" for i in range(1, 41))
    # Missing count
    no_count = "item 1\n... omitted in middle ...\nitem 40" + EXPAND_PTR
    st, r = esg.classify_output(raw, no_count, "bash_compress_pipeline")
    assert st == esg.UNDECLARED_HIDDEN and "missing_hidden_count" in r

    # Inaccurate count (claims 5 when 38 are hidden)
    bad_count = "item 1\n... 5 lines omitted (lines 2-39) ...\nitem 40" + EXPAND_PTR
    st, r = esg.classify_output(raw, bad_count, "bash_compress_pipeline")
    assert st == esg.UNDECLARED_HIDDEN and "inaccurate_hidden_count" in r

    # Missing place (no location/range text anywhere)
    no_place = "item 1\n... 38 lines omitted ...\nitem 40"
    st, r = esg.classify_output(raw, no_place, "bash_compress_pipeline", require_expand=False)
    assert st == esg.UNDECLARED_HIDDEN and "missing_hidden_place" in r

    # Missing expand command
    no_expand = "item 1\n... 38 lines omitted (lines 2-39) ...\nitem 40"
    st, r = esg.classify_output(raw, no_expand, "bash_compress_pipeline")
    assert st == esg.UNDECLARED_HIDDEN and "missing_expand_cmd" in r

    # Valid declaration + expand passes
    good = "item 1\n... 38 lines omitted (lines 2-39) ...\nitem 40" + EXPAND_PTR
    st, r = esg.classify_output(raw, good, "bash_compress_pipeline")
    assert st == esg.RECOVERABLE_DECLARED and r == []
    assert esg.gate_or_raw(raw, good, feature="bash_compress_pipeline") == good


def test_verifier_exception_fails_closed_to_original(monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("simulated verifier crash")

    monkeypatch.setattr(esg, "classify_output", _boom)
    raw = "line 1\nline 2\nline 3"
    comp = "line 1\n... 1 line omitted (line 2) ...\nline 3" + EXPAND_PTR
    ok, reasons = esg.verify_before_emit(raw, comp)
    assert ok is False
    assert reasons == ["verifier_exception"]
    assert esg.gate_or_raw(raw, comp) == raw
    assert esg.gate_or_none(raw, comp) is None


# ---------------------------------------------------------------------------
# 2. Wired replacement paths: fail-closed when gate rejects or raises
# ---------------------------------------------------------------------------

def test_bash_compress_and_hook_fail_closed_when_gate_rejects(monkeypatch):
    raw = "\n".join(f"item {i}: value_{i}" for i in range(120)) + "\n"
    monkeypatch.setattr(esg, "gate_or_raw", lambda orig, *_a, **_kw: orig)
    assert bash_compress.compress("ls -la", raw) == raw

    # Hook emits nothing on stdout when gate rejects
    payload = {
        "session_id": "sess-gate-hook",
        "tool_name": "Bash",
        "tool_use_id": "tu-gate-1",
        "tool_input": {"command": "ls -la"},
        "tool_response": {"stdout": raw, "stderr": "", "exit_code": 0},
    }
    buf = io.StringIO()
    with redirect_stdout(buf):
        bash_compress_hook._run(payload)
    assert buf.getvalue().strip() == ""


def test_pipe_compress_fails_closed_when_gate_rejects(monkeypatch):
    raw = "\n".join(f"src/file_{i}.py:{i}:match_token_{i}" for i in range(60)) + "\n"
    monkeypatch.setattr(esg, "gate_or_none", lambda *_a, **_kw: None)
    assert pipe_file_compress.compress_pipe("rg match_token | sort", raw) is None


def test_big_read_preview_fails_open_on_hidden_snake_case_error():
    # Lowercase snake_case error key buried in middle of large JSON array
    rows = [{"id": i, "name": f"widget-{i:04d}", "status": "ok", "payload": "x" * 40} for i in range(900)]
    rows[450] = {"id": 450, "parse_error": "unexpected EOF in record 450"}
    raw = json.dumps(rows, indent=2)
    assert len(raw.encode("utf-8")) >= big_noncode_read.JSON_DATA_MIN_BYTES
    # Even if visibility_rule misses snake_case `parse_error`, emit_safety_gate
    # catches it and returns None (original file untouched).
    res = big_noncode_read.build_big_noncode_preview("/data/catalog.json", raw, archive_key="abcdef0123456789")
    if res is not None:
        assert "parse_error" in res["context"]


def test_cmd_extra_fails_closed_when_gate_rejects(monkeypatch):
    raw = "\n".join(f"src/mod_{i}/file_{j}.py" for i in range(10) for j in range(10)) + "\n"
    monkeypatch.setattr(esg, "gate_or_none", lambda *_a, **_kw: None)
    assert command_compress_extra.compress_for_pattern("git_ls", raw, "git ls-files") is None


def test_builtin_grep_and_glob_fail_closed_when_gate_rejects(monkeypatch):
    monkeypatch.setattr(esg, "gate_or_none", lambda *_a, **_kw: None)
    paths = [f"src/pkg_{i}/mod_{j}.py" for i in range(10) for j in range(10)]
    glob_raw = {"durationMs": 5, "numFiles": len(paths), "filenames": paths, "truncated": False}
    assert builtin_tool_compress.build_builtin_updated_output(
        "Glob", glob_raw, "\n".join(paths), {}, "toolu_glob_01", None, 4000, 4000
    ) is None

    grep_lines = [f"src/f{i}.py:{j}:match_val_{j}" for i in range(15) for j in range(1, 8)]
    grep_text = "\n".join(grep_lines)
    grep_raw = {"mode": "content", "numFiles": 15, "filenames": [f"src/f{i}.py" for i in range(15)], "content": grep_text}
    assert builtin_tool_compress.build_builtin_updated_output(
        "Grep", grep_raw, grep_text, {"pattern": "match_val"}, "toolu_grep_01", None, len(grep_text), len(grep_text)
    ) is None


def test_noise_fold_passes_gate_and_fails_closed_on_rejection(monkeypatch):
    raw = "\n".join(
        ["src/app.ts:1:createSession()"]
        + [f"node_modules/pkg-{i}/index.js:1:createSession()" for i in range(35)]
    ) + "\n"
    out = bash_compress.compress("rg -n createSession .", raw)
    assert "[Folded noisy matches:" in out
    st, reasons = esg.classify_output(raw, out, "bash_search_noise_fold", tool_name="Bash", command="rg -n createSession .", require_expand=False)
    assert st == esg.RECOVERABLE_DECLARED and reasons == []

    monkeypatch.setattr(esg, "gate_or_raw", lambda orig, *_a, **_kw: orig)
    assert bash_compress.compress("rg -n createSession .", raw) == raw


def test_large_output_policy_on_gated_and_off_byte_identical(tmp_path, monkeypatch):
    raw = "\n".join(f"row {i:04d}: " + ("x" * 60) for i in range(200)) + "\n"

    # Off mode (default unset and explicit '0'): returns (None, ""), never calls emit_safety_gate
    called = []
    monkeypatch.setattr(esg, "gate_or_none", lambda *a, **kw: (called.append(1), None)[1])
    for val in (None, "0"):
        if val is None:
            monkeypatch.delenv(large_output_policy.ENABLED_ENV, raising=False)
        else:
            monkeypatch.setenv(large_output_policy.ENABLED_ENV, val)
        res, clean = large_output_policy.handle_post_tool_use(
            "echo a && echo b", raw, session_id="s-off", tool_use_id="tu-off"
        )
        assert res is None and clean == ""
        assert called == []

    # On mode ('1'): when gate rejects, returns (None, "")
    monkeypatch.setenv(large_output_policy.ENABLED_ENV, "1")
    monkeypatch.setattr("archive_result.archive_original", lambda *a, **kw: tmp_path / "a.json")
    monkeypatch.setattr("archive_result.archive_entry_exists", lambda *a, **kw: True)
    res_on, clean_on = large_output_policy.handle_post_tool_use(
        "echo a && echo b", raw, session_id="s-on", tool_use_id="tu-on"
    )
    assert res_on is None and clean_on == ""
    assert len(called) == 1


# ---------------------------------------------------------------------------
# 3. Shared-vector agreement test with scripts/replay_v2_metrics.py
# ---------------------------------------------------------------------------

_SHARED_VECTORS = [
    # (original, compressed, feature, tool_name, command, exit_code, expected_status, expected_reasons)
    ("a\nb\nc", "a\nb\nc", "bash_compress_pipeline", "Bash", "ls", 0, "VISIBLE_OK", []),
    (
        'line 1\nTraceback (most recent call last):\n  File "x.py", line 1\nValueError: bad\nline 5',
        "line 1\n... 3 lines omitted (lines 2-4) ...\nline 5" + EXPAND_PTR,
        "bash_compress_pipeline", "Bash", "python3 run.py", 0,
        "CRITICAL_HIDDEN", ["hidden_error", "hidden_traceback"],
    ),
    (
        "\n".join(f"ok {i}" for i in range(1, 31)),
        "ok 1\n... 28 lines omitted (lines 2-29) ...\nok 30" + EXPAND_PTR,
        "bash_compress_pipeline", "Bash", "make build", 2,
        "CRITICAL_HIDDEN", ["hidden_exit_code"],
    ),
    (
        "\n".join(f"ok {i}" for i in range(1, 31)),
        "ok 1\n... 28 lines omitted (lines 2-29) ...\nok 30" + EXPAND_PTR,
        "bash_compress_pipeline", "Bash", "make build", 0,
        "RECOVERABLE_DECLARED", [],
    ),
    (
        "\n".join(f"ok {i}" for i in range(1, 31)),
        "ok 1\n... 28 lines omitted (lines 2-29) ...\nok 30",
        "bash_compress_pipeline", "Bash", "make build", 0,
        "UNDECLARED_HIDDEN", ["missing_expand_cmd"],
    ),
    (
        "\n".join(f"ok {i}" for i in range(1, 31)),
        "ok 1\n... 5 lines omitted (lines 2-29) ...\nok 30" + EXPAND_PTR,
        "bash_compress_pipeline", "Bash", "make build", 0,
        "UNDECLARED_HIDDEN", ["inaccurate_hidden_count"],
    ),
    (
        "\n".join(f"ok {i}" for i in range(1, 31)),
        "ok 1\n... 28 lines omitted ...\nok 30\npython3 measure.py expand abcdef0123456789",
        "bash_compress_pipeline", "Bash", "make build", 0,
        "UNDECLARED_HIDDEN", ["missing_hidden_place"],
    ),
    (
        "src/a.py:1:def handle_error():\nsrc/a.py:2:raise ValueError('x')\nsrc/a.py:3:return error\nsrc/a.py:4:pass",
        "[Search results: 4 matches across 1 files.]\nsrc/a.py:1:def handle_error():\n  ... 3 more matches in src/a.py (lines 2-4) ..." + EXPAND_PTR,
        "bash_compress_pipeline", "Bash", "rg error src/", 0,
        "RECOVERABLE_DECLARED", [],
    ),
    (
        "completed\tsuccess\tFix error handling\tmain\t123\ncompleted\tsuccess\tFix error 2\tmain\t124\ncompleted\tfailure\tBroken build\tmain\t125",
        "completed\tfailure\tBroken build\tmain\t125\n... 2 rows omitted (rows 1-2) ..." + EXPAND_PTR,
        "cmd_gh_list", "Bash", "gh run list", 0,
        "RECOVERABLE_DECLARED", [],
    ),
    (
        "line 1\n0 errors, 0 warnings\nline 3\nline 4\nline 5",
        "line 1\n... 3 lines omitted (lines 2-4) ...\nline 5" + EXPAND_PTR,
        "bash_compress_pipeline", "Bash", "lint", 0,
        "RECOVERABLE_DECLARED", [],
    ),
    (
        "line 1\nparse_error: invalid json\nline 3\nline 4\nline 5",
        "line 1\n... 3 lines omitted (lines 2-4) ...\nline 5" + EXPAND_PTR,
        "big_read_preview", "Read", "", None,
        "CRITICAL_HIDDEN", ["hidden_error"],
    ),
    (
        "line 1\nFAILED tests/t.py::test_a\nline 3",
        "line 1\n... 1 line omitted (line 2) ...\nline 3" + EXPAND_PTR,
        "bash_compress_pipeline", "Bash", "pytest", 0,
        "CRITICAL_HIDDEN", ["hidden_failing_test"],
    ),
    (
        "line 1\npanic: boom\nline 3",
        "line 1\n... 1 line omitted (line 2) ...\nline 3" + EXPAND_PTR,
        "bash_compress_pipeline", "Bash", "go test", 0,
        "CRITICAL_HIDDEN", ["hidden_panic_fatal"],
    ),
    (
        "same output\nline 2",
        "[Token Optimizer: identical to previous run]",
        "rerun_dedupe", "Bash", "git status", 0,
        "VISIBLE_OK", [],
    ),
    (
        "file content\nline 2",
        "[Token Optimizer: foo.py (2 lines) is unchanged since your earlier full read in this session.]",
        "bash_reread_unchanged", "Bash", "cat foo.py", 0,
        "VISIBLE_OK", [],
    ),
    (
        "logs/app.log:1:2026-11-22 INFO ok\nlogs/app.log:222:2026-11-22 FATAL database down: exit code 137\nlogs/app.log:223:2026-11-22 INFO ok",
        "[Search results: 3 matches across 1 files.]\nlogs/app.log:1:2026-11-22 INFO ok\n  ... 2 more matches in logs/app.log (lines 222-223) ..." + EXPAND_PTR,
        "bash_compress_pipeline", "Bash", "grep -rn 2026-11-22 logs", 0,
        "CRITICAL_HIDDEN", ["hidden_exit_code", "hidden_panic_fatal"],
    ),
    (
        "line 1\nbash: line 3: jqq: command not found\nline 3",
        "line 1\n... 1 line omitted (line 2) ...\nline 3" + EXPAND_PTR,
        "large_output_policy", "Bash", "for d in a b; do ./sync.sh; done", 0,
        "CRITICAL_HIDDEN", ["hidden_error"],
    ),
    (
        "line 1\nCONFLICT (content): Merge conflict in src/app.py\nline 3",
        "line 1\n... 1 line omitted (line 2) ...\nline 3" + EXPAND_PTR,
        "large_output_policy", "Bash", "git merge main && ./check.sh", 0,
        "CRITICAL_HIDDEN", ["hidden_error"],
    ),
    (
        "line 1\n ! [rejected] main -> main (non-fast-forward)\nline 3",
        "line 1\n... 1 line omitted (line 2) ...\nline 3" + EXPAND_PTR,
        "large_output_policy", "Bash", "git push && echo done", 0,
        "CRITICAL_HIDDEN", ["hidden_error"],
    ),
    (
        "line 1\nKilled\nline 3",
        "line 1\n... 1 line omitted (line 2) ...\nline 3" + EXPAND_PTR,
        "large_output_policy", "Bash", "for d in a b; do ./sync.sh; done", 0,
        "CRITICAL_HIDDEN", ["hidden_error"],
    ),
]


def _load_replay_v2_metrics():
    import os
    env_path = os.environ.get("TO_REPLAY_V2_METRICS_PATH", "").strip()
    candidates = [Path(env_path)] if env_path else []
    candidates.append(ROOT / "scripts" / "replay_v2_metrics.py")
    for path in candidates:
        if path.is_file():
            sdir = str(path.parent)
            if sdir not in sys.path:
                sys.path.insert(0, sdir)
            spec = importlib.util.spec_from_file_location("_replay_v2_metrics_ref", path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
    return None


def test_agreement_with_replay_v2_metrics_on_shared_vectors():
    ref = _load_replay_v2_metrics()
    for idx, (orig, comp, feat, tname, cmd, rc, exp_status, exp_reasons) in enumerate(_SHARED_VECTORS):
        got = esg.classify_output(orig, comp, feat, tool_name=tname, command=cmd, exit_code=rc)
        assert got == (exp_status, exp_reasons), f"Vector {idx} mismatch: got={got} vs expected={(exp_status, exp_reasons)}"
        if ref is not None:
            ref_got = ref.classify_output(orig, comp, feat, tool_name=tname, command=cmd, exit_code=rc)
            assert got == ref_got, f"Vector {idx} drift vs replay_v2_metrics: emit_safety_gate={got} vs replay={ref_got}"
