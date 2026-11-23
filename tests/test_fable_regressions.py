"""Regression tests for all 7 Fable review items (out/REVIEW-FINAL.md)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import bash_compress as bc  # noqa: E402
import emit_safety_gate as esg  # noqa: E402
import large_output_policy as lop  # noqa: E402
import visibility_rule as vr  # noqa: E402


def _run_hook(
    command: str,
    stdout: str,
    *,
    home: Path,
    exit_code: int = 0,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["CLAUDE_SESSION_ID"] = "12345678-1234-1234-1234-123456789abc"
    env.pop("TOKEN_OPTIMIZER_LARGE_OUTPUT", None)
    env.pop("TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT", None)
    env.pop("TOKEN_OPTIMIZER_HOLDOUT", None)
    if extra_env:
        env.update(extra_env)
    payload = {
        "session_id": env["CLAUDE_SESSION_ID"],
        "tool_name": "Bash",
        "tool_use_id": "tu_fable_1",
        "tool_input": {"command": command},
        "tool_response": {
            "stdout": stdout,
            "stderr": "",
            "exit_code": exit_code,
            "interrupted": False,
            "isImage": False,
        },
    }
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "bash_compress_hook.py")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
    )


def _updated_stdout(proc: subprocess.CompletedProcess) -> str | None:
    if not proc.stdout.strip():
        return None
    return (
        json.loads(proc.stdout)
        .get("hookSpecificOutput", {})
        .get("updatedToolOutput", {})
        .get("stdout")
    )


def test_fable1_search_hit_with_fatal_traceback_exit_stays_visible_while_error_word_compresses():
    """Fable #1: file:line search hits carrying FATAL/Traceback/exit code stay visible;
    ordinary source hits matching the word 'error' still compress."""
    # Repro 1: 400 log hits with FATAL + exit code 137 + Traceback at hit 222.
    log_lines = [
        f"logs/app.log:{i}:2026-11-22 10:00:{i % 60:02d} INFO request handled id={i:04d}"
        for i in range(1, 401)
    ]
    fatal_line = (
        "logs/app.log:222:2026-11-22 10:03:42 FATAL database down: "
        "exit code 137, Traceback (most recent call last)"
    )
    log_lines[221] = fatal_line
    raw_logs = "\n".join(log_lines)

    out_logs = bc.compress("grep -rn 2026-11-22 logs", raw_logs)
    assert fatal_line in out_logs
    assert len(out_logs) < len(raw_logs) * 0.25

    # Repro 2: ordinary source hits for "error" still compress cleanly.
    src_lines = [
        f"src/mod_{i % 5}.py:{10 + i}:    def handle_error_{i}(err): return error_code"
        for i in range(1, 201)
    ]
    raw_src = "\n".join(src_lines)
    out_src = bc.compress("rg -n error src/", raw_src)
    assert len(out_src) < len(raw_src) * 0.50


def test_fable2_verifier_linear_time_on_1mb_grep_and_find():
    """Fable #2: emit_safety_gate and visibility_rule run in linear time on ~1 MB grep/find."""
    # ~1 MB grep output (~14,000 lines)
    grep_raw = "\n".join(
        f"src/pkg_{i % 200}/module_{i % 50}.py:{i}:    result = compute_value_for_item_{i:05d}(ctx)"
        for i in range(1, 14001)
    )
    assert len(grep_raw) > 900_000
    t0 = time.monotonic()
    grep_comp = bc.compress("grep -rn compute_value src/", grep_raw)
    dt_grep = time.monotonic() - t0
    assert len(grep_comp) < len(grep_raw)
    assert dt_grep < 5.0, f"1MB grep compression + verification too slow: {dt_grep:.2f}s"

    t_v0 = time.monotonic()
    esg.verify_before_emit(
        grep_raw, grep_comp, feature="bash_compress_pipeline",
        tool_name="Bash", command="grep -rn compute_value src/", require_expand=False,
    )
    dt_vgrep = time.monotonic() - t_v0
    assert dt_vgrep < 1.5, f"1MB grep verifier alone too slow: {dt_vgrep:.2f}s"

    # ~1 MB find output (~18,000 paths)
    find_raw = "\n".join(
        f"./packages/service_{i % 150}/src/components/widget_{i:05d}.tsx"
        for i in range(1, 18001)
    )
    assert len(find_raw) > 900_000
    t1 = time.monotonic()
    find_comp = bc.compress("find . -type f", find_raw)
    dt_find = time.monotonic() - t1
    assert len(find_comp) < len(find_raw)
    assert dt_find < 5.0, f"1MB find compression + verification too slow: {dt_find:.2f}s"

    t_v1 = time.monotonic()
    esg.verify_before_emit(
        find_raw, find_comp, feature="bash_compress_pipeline",
        tool_name="Bash", command="find . -type f", require_expand=False,
    )
    dt_vfind = time.monotonic() - t_v1
    assert dt_vfind < 1.5, f"1MB find verifier alone too slow: {dt_vfind:.2f}s"


def test_fable3_non_error_word_failures_preserved_and_gated():
    """Fable #3: failure lines with no 'error' word stay visible in large_output_policy and gate."""
    failures = {
        60: "bash: line 3: jqq: command not found",
        90: "cat: config/prod.yaml: No such file or directory",
        120: "CONFLICT (content): Merge conflict in src/app.py",
        150: " ! [rejected] main -> main (non-fast-forward)",
        180: "Killed",
        210: "deployment \"web\" exceeded its progress deadline",
    }
    lines = [
        failures.get(i, f"syncing directory item={i:04d} status=ok payload=abcdef{i:06d}")
        for i in range(1, 307)
    ]
    raw = "\n".join(lines)
    ev = lop.evaluate("for d in a b; do (cd $d && ./sync.sh) 2>&1; done", raw, archive_key="fable30000000001")
    assert ev is not None
    out = ev["preview_with_pointer"]
    for msg in failures.values():
        assert msg in out, f"Hidden non-error-word failure: {msg}"
        assert vr.is_error_or_failure_line(msg)
        st, _ = esg.classify_output(
            f"ok1\n{msg}\nok2",
            "ok1\n... 1 line omitted (line 2) ...\nok2\npython3 measure.py expand fable30000000001",
            "large_output_policy",
            tool_name="Bash",
            command="for d in a b; do ./sync.sh; done",
        )
        assert st == esg.CRITICAL_HIDDEN


def test_fable4_critical_context_keeps_expected_received_source_and_test_name():
    """Fable #4: keep context around each critical line (Expected/Received, code frame, test name)."""
    lines = [f"step {i:03d}: building workspace package module_{i:03d} ok" for i in range(1, 240)]
    # Jest failure block around line 80
    lines[78] = "  ● BillingCalculator › computes prorated invoice"
    lines[79] = "    expect(received).toBe(expected) // Object.is equality"
    lines[80] = "    AssertionError: value mismatch"
    lines[81] = "    Expected: 42"
    lines[82] = "    Received: 41"
    lines[83] = "      > 13 | expect(t).toBe(42);"
    # Cargo/rustc diagnostic around line 140
    lines[139] = "error[E0308]: mismatched types"
    lines[140] = " --> src/main.rs:57:18"
    lines[141] = "  |"
    lines[142] = "57 |     let x: u32 = \"hello\";"
    raw = "\n".join(lines)

    ev = lop.evaluate("npm run build && npm test 2>&1; echo done", raw, archive_key="fable40000000001")
    assert ev is not None
    out = ev["preview_with_pointer"]
    for must_keep in (
        "BillingCalculator",
        "Expected: 42",
        "Received: 41",
        "> 13 | expect(t).toBe(42);",
        "error[E0308]: mismatched types",
        "--> src/main.rs:57:18",
        '57 |     let x: u32 = "hello";',
    ):
        assert must_keep in out, f"Missing failure context: {must_keep}"


@pytest.mark.parametrize("cmd", [
    "cat src/handlers.py",
    "cd /repo && cat a.py && cat b.py",
    "curl -s https://api.example.com/items | jq .",
    "python3 - <<'EOF'\nfor i in range(200): print(f'row {i}')\nEOF",
    "python3 -c 'for i in range(200): print(i)'",
])
def test_fable5_deliberate_reads_excluded_cat_jq_inline_script(tmp_path, cmd):
    """Fable #5: bare cat, cat chains, jq output, and inline-script dumps are never chopped."""
    raw = "\n".join(f"line {i:04d}: def handler_{i}(req): return response_ok_{i}" for i in range(1, 361))
    assert len(raw.encode("utf-8")) > lop.MIN_BYTES
    assert lop.evaluate(cmd, raw) is None

    proc = _run_hook(cmd, raw, home=tmp_path, extra_env={"TOKEN_OPTIMIZER_LARGE_OUTPUT": "1"})
    assert proc.returncode == 0
    assert _updated_stdout(proc) is None


def test_fable6_large_output_passes_exit_code_to_gate(tmp_path):
    """Fable #6: failed command (non-zero exit_code) passes exit_code into emit_safety_gate."""
    # Output has >8KB of ordinary lines and NO 'exit code 2' text -> gate must reject and pass raw.
    raw = "\n".join(
        f"worker batch item={i:04d} status=running payload=abcdef{i:06d}"
        for i in range(1, 220)
    )
    proc = _run_hook(
        "echo start && ./run_job.sh",
        raw,
        home=tmp_path,
        exit_code=2,
        extra_env={"TOKEN_OPTIMIZER_LARGE_OUTPUT": "1"},
    )
    assert proc.returncode == 0
    assert _updated_stdout(proc) is None


def test_fable7_noise_fold_logs_under_bash_search_noise_fold_once(tmp_path):
    """Fable #7: grep with node_modules hits logs once under bash_search_noise_fold."""
    lines = [
        f"src/app_{i}.py:{10 + i}:    return handle_error(ctx)" for i in range(1, 15)
    ] + [
        f"node_modules/pkg-{i}/index.js:{i}:    const error = new TypeError();"
        for i in range(1, 301)
    ]
    raw = "\n".join(lines)
    proc = _run_hook("grep -rn error .", raw, home=tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = _updated_stdout(proc)
    assert out is not None
    assert "[Folded noisy matches:" in out

    db = tmp_path / ".claude" / "_backups" / "token-optimizer" / "trends.db"
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            "SELECT feature, tier, verified FROM compression_events"
        ).fetchall()
    assert rows == [("bash_search_noise_fold", "measured", 1)]
