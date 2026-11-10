"""Benchmark-harness transcripts are recognised on every platform's temp dir."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"))
from _measure import fixture_context as fc  # noqa: E402

BENCH = [
    "/u/.claude/projects/-private-var-folders-xy-abc-T-to-bench-M1-k2j/s.jsonl",       # macOS
    "/u/.claude/projects/-var-folders-xy-abc-T-to-bench-M2-k2j/s.jsonl",               # macOS, unresolved
    "/home/u/.claude/projects/-tmp-to-bench-M1-k2j/s.jsonl",                           # Linux /tmp
    "/home/u/.claude/projects/-home-u-tmp-to-bench-M3-k2j/s.jsonl",                    # Linux $TMPDIR
    r"C:\Users\u\.claude\projects\C--Users-u-AppData-Local-Temp-to-bench-M1-k2j\s.jsonl",  # Windows
]
REAL = [
    "/u/.claude/projects/-Users-u-code-app/s.jsonl",
    "/home/u/.claude/projects/-home-u-to-bench-M1-notes/s.jsonl",                       # not a temp dir
    r"C:\Users\u\.claude\projects\C--Users-u-code-app\s.jsonl",
]


@pytest.mark.parametrize("path", BENCH)
def test_benchmark_paths_match_on_every_platform(path):
    assert fc.benchmark_path(path)


@pytest.mark.parametrize("path", REAL)
def test_real_projects_never_match(path):
    assert not fc.benchmark_path(path)


def test_sql_rule_agrees_with_python_rule():
    conn = sqlite3.connect(":memory:")
    conn.create_function("to_transcript_has_usage", 1, lambda p: 0)
    conn.execute("CREATE TABLE session_log (id INTEGER PRIMARY KEY, session_uuid TEXT, jsonl_path TEXT)")
    conn.execute("CREATE TABLE e (session_uuid TEXT, session_id TEXT)")
    for i, path in enumerate(BENCH + REAL):
        conn.execute("INSERT INTO session_log (session_uuid, jsonl_path) VALUES (?,?)", ("s%d" % i, path))
        conn.execute("INSERT INTO e VALUES (?, NULL)", ("s%d" % i,))
    kept = [r[0] for r in conn.execute("SELECT session_uuid FROM e WHERE " + fc.excluded_session_sql("e"))]
    assert kept == ["s%d" % i for i in range(len(BENCH), len(BENCH) + len(REAL))]
