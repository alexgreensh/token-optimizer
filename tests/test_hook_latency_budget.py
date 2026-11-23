"""Hook latency budget tests (SPEC-hook-fastpath).

Runs the real launcher command from hooks/hooks.json against 8 fixture payloads
in a temp HOME, 7 runs each (first discarded), and asserts the latency budget
table plus linear scaling (time(1 MB) <= 4.5 * time(300 KB)) and time-box fail-open.
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import hook_fastpath as hf  # noqa: E402
import emit_safety_gate as esg  # noqa: E402

SLACK = float(os.environ.get("TOKEN_OPTIMIZER_LATENCY_SLACK", "2.0"))


def _make_payloads() -> dict[str, dict]:
    grep_line = "src/module_{:04d}.py:42:    value = compute_something(arg_a, arg_b)\n"
    grep_30k = "".join(grep_line.format(i) for i in range(460))
    grep_300k = "".join(grep_line.format(i) for i in range(4600))
    grep_1m = "".join(grep_line.format(i) for i in range(15500))
    find_300k = "".join(f"./packages/svc-{i % 20}/src/file_{i:05d}.ts\n" for i in range(7200))
    chain_8k = "".join(f"item_{i:04d}: ok status=200 duration=12ms\n" for i in range(220))
    read_100 = "".join(f"line {i:03d}: def fn_{i}(): return {i}\n" for i in range(100))

    return {
        "edit": {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/tmp/sample.py", "old_string": "a", "new_string": "b"},
            "tool_response": {"success": True},
            "session_id": "lat-sess",
        },
        "bash_34b": {
            "tool_name": "Bash",
            "tool_input": {"command": "git status --short"},
            "tool_response": {"stdout": "M README.md\n?? out/tmp.txt\n", "stderr": "", "exit_code": 0},
            "session_id": "lat-sess",
        },
        "read_100": {
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/sample.py"},
            "tool_response": {"content": read_100},
            "session_id": "lat-sess",
        },
        "chain_8k": {
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la && echo done"},
            "tool_response": {"stdout": chain_8k, "stderr": "", "exit_code": 0},
            "session_id": "lat-sess",
        },
        "grep_30k": {
            "tool_name": "Bash",
            "tool_input": {"command": "grep -rn compute_something src"},
            "tool_response": {"stdout": grep_30k, "stderr": "", "exit_code": 0},
            "session_id": "lat-sess",
        },
        "grep_300k": {
            "tool_name": "Bash",
            "tool_input": {"command": "grep -rn compute_something src"},
            "tool_response": {"stdout": grep_300k, "stderr": "", "exit_code": 0},
            "session_id": "lat-sess",
        },
        "grep_1m": {
            "tool_name": "Bash",
            "tool_input": {"command": "grep -rn compute_something src"},
            "tool_response": {"stdout": grep_1m, "stderr": "", "exit_code": 0},
            "session_id": "lat-sess",
        },
        "find_300k": {
            "tool_name": "Bash",
            "tool_input": {"command": "find . -type f"},
            "tool_response": {"stdout": find_300k, "stderr": "", "exit_code": 0},
            "session_id": "lat-sess",
        },
    }


def _run_hook_once(payload: dict, home: Path) -> tuple[float, int, str]:
    snap = home / "snap"
    snap.mkdir(parents=True, exist_ok=True)
    # Pre-seed consent so hook executes real path
    cfg_dir = home / ".claude" / "token-optimizer"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "config.json"
    if not cfg.exists():
        cfg.write_text(json.dumps({"v5_welcome_shown": True, "enterprise_consent_shown": True}))
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "CLAUDE_PLUGIN_ROOT": str(REPO),
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(snap),
        "TOKEN_OPTIMIZER_NO_PROC_SCAN": "1",
    }
    raw_in = json.dumps(payload)
    t0 = time.monotonic()
    p = subprocess.run(
        [sys.executable, str(REPO / "hooks" / "run.py"), "hooks/posttooluse_runner.py"],
        input=raw_in, text=True, capture_output=True, env=env, timeout=15,
    )
    dt = time.monotonic() - t0
    return dt, p.returncode, p.stdout


def test_early_exit_and_monotonic_budget(tmp_path):
    assert hf.can_early_exit_posttooluse({
        "tool_name": "Edit",
        "tool_input": {"file_path": "/tmp/nope.py"},
        "session_id": "s1",
    }, snapshot_dir=tmp_path) is True

    assert hf.can_early_exit_posttooluse({
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "tool_response": {"stdout": "M a.py\n", "stderr": "", "exit_code": 0},
        "session_id": "s1",
    }, snapshot_dir=tmp_path) is True

    # Non-zero exit must NOT early-exit
    assert hf.can_early_exit_posttooluse({
        "tool_name": "Bash",
        "tool_input": {"command": "false"},
        "tool_response": {"stdout": "", "stderr": "err", "exit_code": 1},
        "session_id": "s1",
    }, snapshot_dir=tmp_path) is False

    # BudgetExceeded triggers on expired deadline
    b = hf.MonotonicBudget(0.01)
    b.deadline = time.monotonic() - 1.0
    with pytest.raises(hf.BudgetExceeded):
        b.check(0, stride=1)


def test_hook_latency_budget_and_linear_scaling(tmp_path):
    payloads = _make_payloads()
    timings: dict[str, list[float]] = {}
    for name, payload in payloads.items():
        runs = []
        for i in range(7):
            dt, rc, _ = _run_hook_once(payload, tmp_path / name)
            assert rc == 0, f"{name} run {i} exited {rc}"
            if i > 0:  # discard warmup run 0
                runs.append(dt)
        timings[name] = runs

    def p50(xs):
        return statistics.median(xs)

    def p95(xs):
        return sorted(xs)[max(0, int(len(xs) * 0.95) - 1)]

    # Pass-through payloads: p50 <= 120 ms * SLACK, p95 <= 250 ms * SLACK
    for name in ("edit", "bash_34b"):
        assert p50(timings[name]) <= 0.120 * SLACK, f"{name} p50={p50(timings[name]):.3f}s"
        assert p95(timings[name]) <= 0.250 * SLACK, f"{name} p95={p95(timings[name]):.3f}s"

    # Up to 30 KB replacement: p95 <= 400 ms * SLACK
    assert p95(timings["grep_30k"]) <= 0.400 * SLACK, f"grep_30k p95={p95(timings['grep_30k']):.3f}s"

    # Hard ceiling: <= 2.0 s * SLACK on all payloads
    for name, xs in timings.items():
        assert max(xs) <= 2.0 * SLACK, f"{name} max={max(xs):.3f}s"

    # Linear scaling guard: time(1 MB) <= 4.5 * time(300 KB)
    t_300k = max(0.05, p50(timings["grep_300k"]))
    t_1m = p50(timings["grep_1m"])
    assert t_1m <= 4.5 * t_300k, f"Non-linear scaling: 1MB={t_1m:.3f}s vs 300KB={t_300k:.3f}s"
