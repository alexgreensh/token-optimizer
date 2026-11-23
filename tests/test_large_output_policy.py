"""Tests for SPEC-large-output-narrow: 16 KB+ repetitive-only rule, default OFF."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import large_output_policy as lop  # noqa: E402


def _repetitive_output(n_lines: int = 320) -> str:
    return "\n".join(
        f"2026-11-22T10:00:{i % 60:02d}Z INFO worker_{i % 4} processed item_{i:05d} status=200 latency=12ms payload=ok"
        for i in range(n_lines)
    )


def _diverse_output(n_lines: int = 260) -> str:
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]
    return "\n".join(
        f"{words[i % len(words)]}_{words[(i // 3) % len(words)]} {' '.join(words[(i + j) % len(words)] * ((j % 3) + 1) for j in range(5))} key_{i}"
        for i in range(n_lines)
    )


def test_default_off_creates_no_files_and_does_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("TOKEN_OPTIMIZER_LARGE_OUTPUT", raising=False)
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path))
    out, _ = lop.handle_post_tool_use("./run_sync.sh", _repetitive_output(400), session_id="s-off")
    assert out is None
    assert list(tmp_path.iterdir()) == []


def test_condition2_under_16kb_or_under_200_lines_passes_raw():
    # 10 KB repetitive (would have been caught by old 8 KB rule) -> raw
    small = _repetitive_output(120)
    assert 8192 < len(small.encode()) < 16384
    assert lop.evaluate("./run_sync.sh", small) is None
    # >16 KB but <200 lines -> raw
    wide_short = "\n".join(("x" * 120) for _ in range(180))
    assert len(wide_short.encode()) >= 16384
    assert lop.evaluate("./run_sync.sh", wide_short) is None


def test_condition3_exit_nonzero_interrupted_or_stderr_passes_raw():
    rep = _repetitive_output(320)
    assert lop.evaluate("./run_sync.sh", rep, exit_code=1) is None
    assert lop.evaluate("./run_sync.sh", rep, interrupted=True) is None
    assert lop.evaluate("./run_sync.sh", rep, stderr="warning: something") is None


@pytest.mark.parametrize("cmd", [
    "cat src/handlers.py",
    "cd /repo && cat a.py && cat b.py",
    "sed -n '1,300p' file.txt",
    "head -n 300 file.txt",
    "tail -n 300 file.txt",
    "awk 'NR<=300' file.txt",
    "cut -f1-3 file.tsv",
    "curl -s https://api.example | jq .",
    "sqlite3 app.db 'SELECT * FROM events LIMIT 500'",
    "grep -m 300 foo bar.log",
    "rg -m 300 foo src/",
    "git log -n 250",
    "git show HEAD",
    "git diff HEAD~1",
    "python3 -c 'print(1)'",
    "node -e 'console.log(1)'",
    "python3 - <<'EOF'\nprint(1)\nEOF",
    "unterminated 'quote",
])
def test_condition4_excluded_and_unparseable_commands_pass_raw(cmd):
    assert lop.evaluate(cmd, _repetitive_output(320)) is None


@pytest.mark.parametrize("failure_line", [
    "bash: line 3: jqq: command not found",
    "cat: config/prod.yaml: No such file or directory",
    "CONFLICT (content): Merge conflict in src/app.py",
    " ! [rejected] main -> main (non-fast-forward)",
    "Killed",
    "  Expected: 42",
    "  --> src/main.rs:57:18",
    "deployment web exceeded its progress deadline",
    "logs/app.log:222: FATAL database down: exit code 137",
])
def test_condition5_any_failure_vector_passes_whole_output_raw(failure_line):
    lines = _repetitive_output(320).splitlines()
    lines[150] = failure_line
    assert lop.evaluate("./run_sync.sh", "\n".join(lines)) is None


def test_condition6_non_repetitive_output_passes_raw():
    assert lop.evaluate("./dump_report.sh", _diverse_output(260)) is None


def test_condition7_and_8_repetitive_16kb_compresses_deterministically_with_exact_expand(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_LARGE_OUTPUT", "1")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path))
    raw = _repetitive_output(360)
    assert len(raw.encode()) >= 16384

    c1, orig1 = lop.handle_post_tool_use("./run_sync.sh", raw, session_id="s-on", exit_code=0)
    c2, orig2 = lop.handle_post_tool_use("./run_sync.sh", raw, session_id="s-on", exit_code=0)
    assert c1 is not None
    assert c1 == c2  # deterministic
    assert orig1 == raw
    assert "hidden:" in c1 and "matching \"" in c1 and "expand " in c1
    # First 40 and last 40 lines preserved
    raw_lines = raw.splitlines()
    assert raw_lines[0] in c1 and raw_lines[39] in c1
    assert raw_lines[-40] in c1 and raw_lines[-1] in c1

    # Byte-identical expand from archive
    import json, re
    m = re.search(r"expand ([0-9a-f]{16})", c1)
    assert m is not None
    arch_file = tmp_path / "tool-archive" / "s-on" / f"{m.group(1)}.json"
    assert arch_file.is_file()
    restored = json.loads(arch_file.read_text(encoding="utf-8"))["response"]
    assert restored == raw
