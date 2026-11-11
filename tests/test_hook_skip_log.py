"""U2: a Bash compression skipped by the PostToolUse budget is logged as an
opportunity, and that row can never count as a saving.

Run: python3 -m pytest tests/test_hook_skip_log.py -q
"""
import json
import os
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from test_counted_cumulative import m, _db, _transcript, _add_event, _local_iso, SID  # noqa: F401
from test_posttooluse_runner import _load_runner, _no_real_deadline, REPO, SCRIPTS

sys.path.insert(0, str(SCRIPTS))
import hook_skip_log  # noqa: E402

BIG = "\n".join(f"line {i} of a long listing" for i in range(400))


def _bash(stdout=BIG, command="grep -rn foo src", **extra):
    payload = {"tool_name": "Bash", "session_id": SID, "tool_use_id": "toolu_x",
               "tool_input": {"command": command},
               "tool_response": {"stdout": stdout, "stderr": "", "interrupted": False}}
    payload.update(extra)
    return payload


def test_row_is_opportunity_with_zero_saved_and_the_would_be_size():
    row = hook_skip_log.skip_row(_bash(), "bash_compress_hook", 0.625, "hook_budget")
    assert row["tier"] == "opportunity"
    assert row["feature"] == "bash_compress_skipped"
    assert row["original_tokens"] == row["compressed_tokens"] > 0
    detail = json.loads(row["detail"])
    assert detail["saved_tokens"] == 0
    assert detail["output_chars"] == len(BIG)
    assert detail["output_tokens"] == row["original_tokens"]
    assert detail["budget_s"] == 0.625 and detail["reason"] == "hook_budget"
    assert row["command_pattern"] == "grep"


@pytest.mark.parametrize("payload", [
    {"tool_name": "Read", "tool_response": {"stdout": BIG}},
    _bash(stdout="short"),
    _bash(stdout="x" * 99),
    _bash(tool_response={"stdout": BIG, "interrupted": True}),
    _bash(tool_response={"stdout": BIG, "isImage": True}),
    _bash(tool_response=None),
    "not a dict",
])
def test_nothing_to_skip_writes_nothing(payload):
    assert hook_skip_log.skip_row(payload, "bash_compress_hook", 0.6, "hook_budget") is None
    assert hook_skip_log.log_skipped_compression(payload, "bash_compress_hook", 0.6) is False


def test_plain_string_response_is_sized():
    row = hook_skip_log.skip_row(_bash(tool_response=BIG), "bash_compress_hook", 0.6, "hook_budget")
    assert row["original_tokens"] > 0


@pytest.mark.parametrize("command,word", [
    ("cd /repo && FOO=1 cargo test -q 2>&1 | tail -40", "cargo"),
    ("sudo /usr/bin/journalctl -n 50", "journalctl"),
    ("cd x; cd y", "unknown"),
    ("", "unknown"),
])
def test_command_word_never_keeps_the_full_line(command, word):
    assert hook_skip_log._command_word(command) == word


def test_row_is_written_and_reads_back(tmp_path, monkeypatch):
    import compression_log
    monkeypatch.setattr(compression_log, "SNAPSHOT_DIR", tmp_path / "snap")
    monkeypatch.setattr(compression_log, "TRENDS_DB", tmp_path / "snap" / "trends.db")
    assert hook_skip_log.log_skipped_compression(_bash(), "bash_compress_hook", 0.625) is True
    with sqlite3.connect(tmp_path / "snap" / "trends.db") as conn:
        rows = conn.execute("SELECT feature, tier, original_tokens, compressed_tokens, verified, "
                            "session_uuid FROM compression_events").fetchall()
    assert len(rows) == 1
    feature, tier, orig, comp, verified, uuid = rows[0]
    assert (feature, tier, verified, uuid) == ("bash_compress_skipped", "opportunity", 0, SID)
    assert orig == comp > 0


def test_skip_rows_never_enter_the_counted_ledger(m, tmp_path, monkeypatch):
    """Negative case next to a positive control of the same size and session."""
    dbp = _db(m, tmp_path, monkeypatch)
    _, start = _transcript(m, tmp_path, monkeypatch, n_turns=5)
    import compression_log
    monkeypatch.setattr(compression_log, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(compression_log, "TRENDS_DB", dbp)
    for _ in range(3):
        assert hook_skip_log.log_skipped_compression(_bash(), "bash_compress_hook", 0.625)
    # Stamp the skip rows inside the transcript window, like a real overrun.
    with sqlite3.connect(str(dbp)) as conn:
        conn.execute("UPDATE compression_events SET timestamp=?",
                     (_local_iso(start + timedelta(seconds=90)),))
        conn.commit()
        m._update_counted_cumulative(conn, quiet=True)
        assert conn.execute("SELECT COUNT(*) FROM counted_reread").fetchone()[0] == 0
    summary = m._counted_cumulative_summary()
    assert summary["removed_tokens"] == 0 and summary["total_usd"] == 0
    coverage = m._get_compression_coverage(days=3650)
    opp = [r for r in coverage["tiers"]["opportunity"] if r["feature"] == "bash_compress_skipped"]
    assert opp and opp[0]["tokens_saved"] == 0 and opp[0]["events"] == 3
    assert all(r["feature"] != "bash_compress_skipped"
               for t in ("measured", "estimated") for r in coverage["tiers"][t])
    compression = m._get_compression_summary(days=3650)
    assert "bash_compress_skipped" not in compression.get("by_feature", {}) or \
        compression["by_feature"]["bash_compress_skipped"].get("tokens_saved", 0) == 0
    assert "bash_compress_skipped" not in m._V5_COMPRESSION_CATEGORIES

    # Positive control: a measured removal of the same session IS counted.
    _add_event(dbp, "bash_compress_pipeline", 40_000,
               _local_iso(start + timedelta(seconds=90)), table="compression_events")
    with sqlite3.connect(str(dbp)) as conn:
        m._update_counted_cumulative(conn, quiet=True)
        keys = [k for (k,) in conn.execute("SELECT event_key FROM counted_reread")]
    assert len(keys) == 1


def _runner_with_overrun(monkeypatch, tmp_path, overrun_name):
    runner = _load_runner(monkeypatch, tmp_path)
    _no_real_deadline(monkeypatch, runner)

    def _over(_payload):
        raise runner._HandlerBudgetExceeded(0.625)

    noop = lambda _p: None  # noqa: E731
    for attr in ("_sub_bash_compress", "_sub_archive_result", "_sub_context_intel",
                 "_sub_read_cache_invalidate", "_sub_quality_cache"):
        monkeypatch.setattr(runner, attr, noop)
    monkeypatch.setattr(runner, overrun_name, _over)
    logged = []
    monkeypatch.setattr(hook_skip_log, "log_skipped_compression",
                        lambda *a, **k: logged.append((a, k)) or True)
    monkeypatch.setitem(sys.modules, "hook_skip_log", hook_skip_log)
    monkeypatch.setattr(runner, "_read_hook_input", lambda: _bash())
    return runner, logged


def test_runner_logs_a_bash_compress_overrun(monkeypatch, tmp_path, capsys):
    runner, logged = _runner_with_overrun(monkeypatch, tmp_path, "_sub_bash_compress")
    assert runner.main() == 0
    assert "bash_compress_hook budget exceeded 0.625s; continuing" in capsys.readouterr().err
    assert len(logged) == 1
    args, _ = logged[0]
    assert args[1:] == ("bash_compress_hook", 0.625, "hook_budget")


def test_runner_does_not_log_other_handlers(monkeypatch, tmp_path, capsys):
    runner, logged = _runner_with_overrun(monkeypatch, tmp_path, "_sub_archive_result")
    assert runner.main() == 0
    assert "archive_result budget exceeded" in capsys.readouterr().err
    assert logged == []


def test_runner_logs_a_skip_before_dispatch(monkeypatch, tmp_path):
    runner, logged = _runner_with_overrun(monkeypatch, tmp_path, "_sub_bash_compress")
    monkeypatch.setattr(runner, "_runner_budget", lambda *a, **k: 0.0)
    assert runner.main() == 0
    assert [a[1:] for a, _ in logged] == [("bash_compress_hook", 0.0, "budget_exhausted")]


@pytest.mark.skipif(os.name == "nt" or sys.platform == "win32", reason="POSIX interval timer path")
def test_real_runner_overrun_writes_the_row_end_to_end(tmp_path):
    """The real hook chain, forced over budget (1 ms entry budget), writes one
    opportunity row to an isolated trends.db and still exits 0 with the raw
    output untouched."""
    home = tmp_path / "home"
    snap = tmp_path / "snap"
    home.mkdir()
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
           "CLAUDE_PLUGIN_ROOT": str(REPO), "CLAUDE_CONFIG_DIR": str(home),
           "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(snap), "TOKEN_OPTIMIZER_HOOK_BUDGET_MS": "1",
           "PYTHONPYCACHEPREFIX": str(tmp_path / "pyc")}
    payload = dict(_bash(), hook_event_name="PostToolUse")

    def run(environment):
        return subprocess.run([sys.executable, str(REPO / "hooks" / "posttooluse_runner.py")],
                              input=json.dumps(payload), capture_output=True, text=True,
                              env=environment, timeout=60)

    # The skip row is written under a 0.2 s deadline (best effort, fail-open). A cold bytecode cache
    # spends that on compiling the hook's modules, which a starved CI core (Python 3.9, 5.13.70)
    # cannot do in time. Warm the cache in a throwaway run so this run measures the write, not the compiler.
    run(dict(env, TOKEN_OPTIMIZER_SNAPSHOT_DIR=str(tmp_path / "warm-snap")))
    proc = run(env)
    assert proc.returncode == 0
    assert "bash_compress_hook budget exceeded" in proc.stderr
    assert "updatedToolOutput" not in proc.stdout
    with sqlite3.connect(snap / "trends.db") as conn:
        rows = conn.execute("SELECT tier, original_tokens, compressed_tokens, detail "
                            "FROM compression_events WHERE feature='bash_compress_skipped'").fetchall()
    assert len(rows) == 1
    tier, orig, comp, detail = rows[0]
    assert tier == "opportunity" and orig == comp > 0
    assert json.loads(detail)["budget_s"] == 0.001
