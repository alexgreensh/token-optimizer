"""Tests for large_output_policy.py (two states: on/off via TOKEN_OPTIMIZER_LARGE_OUTPUT=1/0, default OFF)."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import large_output_policy as lop  # noqa: E402


def _make_large_output(
    n_lines: int = 200,
    *,
    inject: dict[int, str] | None = None,
    prefix: str = "Processing batch record",
) -> str:
    """Generate a realistic >8KB multi-line output with optional injected lines."""
    inject = inject or {}
    lines = []
    for i in range(1, n_lines + 1):
        if i in inject:
            lines.append(inject[i])
        else:
            lines.append(
                f"{prefix} item={i:04d} status=ok duration_ms={10 + (i % 40)} "
                f"worker=pool-{i % 4} checksum=abcdef{i:06d}"
            )
    return "\n".join(lines)


def _run_hook(
    command: str,
    stdout: str,
    *,
    home: Path,
    session_id: str = "12345678-1234-1234-1234-123456789abc",
    tool_use_id: str = "tu_lop_1",
    exit_code: int = 0,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["CLAUDE_SESSION_ID"] = session_id
    env.pop("TOKEN_OPTIMIZER_LARGE_OUTPUT", None)
    env.pop("TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT", None)
    env.pop("TOKEN_OPTIMIZER_HOLDOUT", None)
    if extra_env:
        env.update(extra_env)
    payload = {
        "session_id": session_id,
        "tool_name": "Bash",
        "tool_use_id": tool_use_id,
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
        timeout=15,
    )


def _updated_stdout(proc: subprocess.CompletedProcess) -> str | None:
    if not proc.stdout.strip():
        return None
    data = json.loads(proc.stdout)
    return (
        data.get("hookSpecificOutput", {})
        .get("updatedToolOutput", {})
        .get("stdout")
    )


# ---------------------------------------------------------------------------
# Eligibility, exclusions, headers, critical lines, exact counts, determinism
# ---------------------------------------------------------------------------


class TestEligibilityAndExclusions:
    def test_small_output_below_8kb_ineligible(self):
        small = _make_large_output(40)
        assert len(small.encode("utf-8")) <= lop.MIN_BYTES
        assert lop.evaluate("echo a && echo b", small) is None

    @pytest.mark.parametrize("cmd", [
        "echo start && ./run_a.sh && ./run_b.sh",
        "echo start; ./run_a.sh; ./run_b.sh",
        "for f in a b c; do ./check \"$f\"; done",
        "while read line; do echo \"$line\"; done < list.txt",
        "(cd sub && ./emit_logs.sh)",
        "res=$(./emit_logs.sh); echo \"$res\"",
    ])
    def test_eligible_compound_shapes(self, cmd):
        raw = _make_large_output(180)
        assert len(raw.encode("utf-8")) > lop.MIN_BYTES
        ev = lop.evaluate(cmd, raw)
        assert ev is not None
        assert ev["saved_tokens"] >= lop.MIN_SAVING_TOKENS
        assert (ev["saved_tokens"] / ev["tokens_before"]) >= lop.MIN_SAVING_RATIO

    @pytest.mark.parametrize("cmd", [
        "sed -n '1,200p' src/app.py",
        "cd /repo && sed -n '50,250p' src/app.py",
        "head -n 200 big.log",
        "cat big.log | head -150",
        "tail -n 200 big.log",
        "cat big.log | tail -200",
        "cut -d, -f1-3 data.csv",
        "cat data.txt | cut -c1-80",
        "cut -b 1-64 blob.txt",
        "awk 'NR>=10 && NR<=210' src/app.py",
        "awk 'FNR>5 && FNR<180 {print}' src/app.py",
    ])
    def test_explicit_narrow_reads_excluded(self, cmd):
        raw = _make_large_output(200)
        assert lop.is_explicit_narrow_read(cmd)
        assert lop.evaluate(cmd, raw) is None

    def test_binary_like_output_excluded(self):
        base = _make_large_output(180)
        assert lop.evaluate("cat bin.dat && echo done", base + "\x00binary") is None
        ctrl_blob = ("\x01\x02\x03\x04\x05\x06abc\n" * 1200)
        assert lop.evaluate("cat bin.dat && echo done", ctrl_blob) is None

    @pytest.mark.parametrize("marker", [
        "[Full result archived (12,000 chars) — saved to disk, not lost.]",
        "[bash_compress] 180 lines -> 30 lines",
        "[large_output_policy: 100 of 200 lines hidden (lines 26-125); 0 critical line(s) preserved]",
    ])
    def test_already_archived_excluded(self, marker):
        raw = _make_large_output(180) + "\n" + marker
        assert lop.evaluate("echo a && ./run.sh", raw) is None

    def test_under_threshold_saving_rejected(self):
        # 55 lines (>50 so 5 hidden) -> < 15% and < 200 tokens saved -> rejected
        raw = "\n".join(f"L{i:03d}: " + ("x" * 180) for i in range(55))
        assert len(raw.encode("utf-8")) > lop.MIN_BYTES
        assert lop.evaluate("echo a && ./run.sh", raw) is None


class TestPreservationAndExactCounts:
    def test_every_critical_class_and_section_headers_visible(self):
        injected = {
            40: "=== SECTION ALPHA ===",
            55: "--- subsection beta ---",
            60: "diff --git a/src/foo.py b/src/foo.py",
            65: "MY_ECHO_BOUNDARY",
            75: "error: database connection refused on port 5432",
            85: "Build failure in target web-bundle",
            95: "TimeoutException: upstream gateway exceeded 30s",
            105: "Traceback (most recent call last):",
            106: '  File "src/worker.py", line 42, in run_job',
            107: "    raise ValueError('bad state')",
            115: "thread 'main' panicked at src/main.rs:19:5: index out of bounds",
            125: "FAILED tests/test_core.py::test_invariant",
            126: "--- FAIL: TestScheduler (0.04s)",
            127: "not ok 7 - expected 200 got 500",
            135: "Command exited with exit code 2",
            136: "Process returned non-zero exit status 1",
        }
        raw = _make_large_output(200, inject=injected)
        cmd = "echo MY_ECHO_BOUNDARY && ./run_all.sh"
        ev = lop.evaluate(cmd, raw, archive_key="deadbeef01234567")
        assert ev is not None
        out = ev["preview_with_pointer"]

        # First 25 + last 25 lines present
        lines = raw.splitlines()
        for line in lines[:25] + lines[-25:]:
            assert line in out

        # Every header and every critical class present verbatim
        for line in injected.values():
            assert line in out, f"Missing preserved line: {line}"

        # Expand pointer present with exact key
        assert "expand deadbeef01234567" in out
        assert ev["critical_lines"] >= 12

    def test_exact_hidden_line_count_and_ranges(self):
        injected = {
            60: "=== MID HEADER ===",
            120: "ValueError: something broke",
        }
        raw = _make_large_output(180, inject=injected)
        ev = lop.evaluate("echo a && ./run.sh", raw, archive_key="1111222233334444")
        assert ev is not None
        body = ev["preview_body"]

        # Kept lines: 1..25 (25), 60 (1), 117..124 (8: line 120 + context -3/+4),
        # 156..180 (25) => 59 kept, 121 hidden.
        # Hidden runs: 26-59 (34), 61-116 (56), 125-155 (31) => 34 + 56 + 31 = 121.
        assert ev["lines"] == 180
        assert ev["hidden_lines"] == 121
        assert ev["hidden_ranges"] == "lines 26-59, lines 61-116, lines 125-155"
        assert "[... 34 lines hidden: lines 26-59 ...]" in body
        assert "[... 56 lines hidden: lines 61-116 ...]" in body
        assert "[... 31 lines hidden: lines 125-155 ...]" in body
        assert "121 of 180 lines hidden (lines 26-59, lines 61-116, lines 125-155)" in body

        # Sum of all inline hidden markers equals hidden_lines
        inline_counts = [
            int(m.group(1))
            for m in re.finditer(r"\[\.\.\. (\d+) lines? hidden:", body)
        ]
        assert sum(inline_counts) == ev["hidden_lines"]

    def test_deterministic_output(self):
        raw = _make_large_output(190, inject={80: "RuntimeError: deterministic check"})
        ev1 = lop.evaluate("echo a && ./run.sh", raw, archive_key="abcd1234abcd1234")
        ev2 = lop.evaluate("echo a && ./run.sh", raw, archive_key="abcd1234abcd1234")
        assert ev1 == ev2


# ---------------------------------------------------------------------------
# Two states only (on/off), no shadow log or schema created, archive failure
# ---------------------------------------------------------------------------


class TestOnOffStatesAndNoLocalLog:
    @pytest.mark.parametrize("env_val", [None, "0", "", "shadow", "active", "true"])
    def test_is_enabled_strictly_one_or_off(self, monkeypatch, env_val):
        if env_val is None:
            monkeypatch.delenv(lop.ENABLED_ENV, raising=False)
        else:
            monkeypatch.setenv(lop.ENABLED_ENV, env_val)
        assert lop.is_enabled() is False

    def test_is_enabled_when_one(self, monkeypatch):
        monkeypatch.setenv(lop.ENABLED_ENV, "1")
        assert lop.is_enabled() is True

    def test_off_does_no_work_beyond_env_check(self, monkeypatch):
        monkeypatch.delenv(lop.ENABLED_ENV, raising=False)
        called = []
        monkeypatch.setattr(lop, "evaluate", lambda *a, **kw: called.append(1))
        assert lop.handle_post_tool_use("echo a && ./run.sh", _make_large_output(200)) == (None, "")
        assert called == []

        monkeypatch.setenv(lop.ENABLED_ENV, "0")
        assert lop.handle_post_tool_use("echo a && ./run.sh", _make_large_output(200)) == (None, "")
        assert called == []

    @pytest.mark.parametrize("extra_env", [None, {"TOKEN_OPTIMIZER_LARGE_OUTPUT": "0"}])
    def test_hook_off_is_byte_identical_and_creates_no_log_or_schema(self, tmp_path, extra_env):
        raw = _make_large_output(200, inject={90: "ValueError: off check"})
        cmd = "echo step1 && ./run_job.sh"
        proc = _run_hook(cmd, raw, home=tmp_path, tool_use_id="tu_off_1", extra_env=extra_env)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""
        assert _updated_stdout(proc) is None

        # Must NOT archive anything, must NOT create large_output_shadow table,
        # and must NOT log any compression_events / savings_events rows.
        archive_dir = tmp_path / ".claude" / "_backups" / "token-optimizer" / "tool-archive"
        assert not archive_dir.exists() or list(archive_dir.rglob("*.json")) == []
        db = tmp_path / ".claude" / "_backups" / "token-optimizer" / "trends.db"
        if db.exists():
            with sqlite3.connect(str(db)) as conn:
                tables = {
                    r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                assert "large_output_shadow" not in tables
                if "compression_events" in tables:
                    assert conn.execute("SELECT COUNT(*) FROM compression_events").fetchone()[0] == 0
                if "savings_events" in tables:
                    assert conn.execute("SELECT COUNT(*) FROM savings_events").fetchone()[0] == 0

    def test_direct_off_call_creates_no_files(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv(lop.ENABLED_ENV, raising=False)
        res = lop.handle_post_tool_use("echo a && ./run.sh", _make_large_output(200), session_id="s1")
        assert res == (None, "")
        assert list(tmp_path.rglob("*")) == []

    def test_hook_on_archives_first_logs_measured_and_creates_no_shadow_table(self, tmp_path):
        raw = _make_large_output(200, inject={90: "ValueError: on check"})
        cmd = "echo step1 && ./run_job.sh"
        proc = _run_hook(
            cmd, raw, home=tmp_path, tool_use_id="tu_on_1",
            extra_env={lop.ENABLED_ENV: "1"},
        )
        assert proc.returncode == 0, proc.stderr
        out = _updated_stdout(proc)
        assert out is not None
        assert len(out) < len(raw)
        assert "ValueError: on check" in out
        assert "142 of 200 lines hidden" in out
        assert "expand " in out

        # Archive file exists on disk
        archive_dir = tmp_path / ".claude" / "_backups" / "token-optimizer" / "tool-archive"
        assert len(list(archive_dir.rglob("*.json"))) >= 1

        # Logged as measured in compression_events under feature=large_output_policy;
        # no large_output_shadow table created.
        db = tmp_path / ".claude" / "_backups" / "token-optimizer" / "trends.db"
        with sqlite3.connect(str(db)) as conn:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "large_output_shadow" not in tables
            rows = conn.execute(
                "SELECT feature, tier, verified FROM compression_events WHERE feature = 'large_output_policy'"
            ).fetchall()
        assert rows == [("large_output_policy", "measured", 1)]

    def test_on_mode_archive_failure_returns_original(self, monkeypatch):
        import archive_result
        monkeypatch.setenv(lop.ENABLED_ENV, "1")
        monkeypatch.setattr(archive_result, "archive_original", lambda *a, **kw: None)
        res = lop.handle_post_tool_use(
            "echo a && ./run.sh", _make_large_output(200), session_id="s1"
        )
        assert res == (None, "")

        # Also test archive_entry_exists returning False
        monkeypatch.setattr(archive_result, "archive_original", lambda *a, **kw: "k1")
        monkeypatch.setattr(archive_result, "archive_entry_exists", lambda *a, **kw: False)
        res2 = lop.handle_post_tool_use(
            "echo a && ./run.sh", _make_large_output(200), session_id="s1"
        )
        assert res2 == (None, "")

    def test_module_contains_no_shadow_log_or_sqlite_schema(self):
        src = (SCRIPTS / "large_output_policy.py").read_text(encoding="utf-8")
        assert "sqlite3" not in src
        assert "CREATE TABLE" not in src
        assert "large_output_shadow" not in src
        assert "record_shadow_row" not in src
        assert "shadow_report_cli" not in src
        assert not hasattr(lop, "record_shadow_row")
        assert not hasattr(lop, "collect_shadow_report")
        assert not hasattr(lop, "shadow_report_cli")
        assert not hasattr(lop, "SHADOW_TABLE")


# ---------------------------------------------------------------------------
# ADDENDUM: savings summary JSON, labels, benchmark, registry
# ---------------------------------------------------------------------------


class TestAddendumAndRegistrations:
    def test_on_fake_event_appears_in_savings_summary_with_human_label(self, tmp_path, monkeypatch):
        import measure
        monkeypatch.setattr(measure, "SNAPSHOT_DIR", tmp_path)
        monkeypatch.setattr(measure, "TRENDS_DB", tmp_path / "trends.db")

        assert "large_output_policy" in measure._V5_COMPRESSION_CATEGORIES
        assert measure._V5_COMPRESSION_LABELS["large_output_policy"] == "Large Bash output policy"

        conn = measure._init_trends_db()
        now_iso = datetime.now().isoformat()
        real_uuid = "12345678-1234-1234-1234-123456789abc"
        conn.execute(
            "INSERT INTO compression_events "
            "(timestamp, session_id, session_uuid, feature, command_pattern, "
            " original_tokens, compressed_tokens, compression_ratio, "
            " quality_preserved, verified, detail, model, tier) "
            "VALUES (?, ?, ?, 'large_output_policy', 'large_output_policy', 6000, 1500, 0.25, 1, 1, '', 'claude-sonnet-4-6', 'measured')",
            (now_iso, real_uuid, real_uuid),
        )
        conn.commit()
        conn.close()

        summary = measure._get_merged_savings(days=30)
        by_cat = summary.get("by_category", {})
        assert "large_output_policy" in by_cat
        assert by_cat["large_output_policy"]["tokens_saved"] == 4500
        assert by_cat["large_output_policy"]["cost_saved_usd"] > 0
        assert measure._SAVINGS_CATEGORY_LABELS["large_output_policy"] == "Large Bash output policy"

    def test_registrations_and_benchmark_fixtures(self):
        import benchmark
        import devin_source
        import hermes_install
        from _measure import feature_health

        assert "large_output_policy" in devin_source.REPO_MODULES
        assert "large_output_policy.py" not in devin_source.PAYLOAD
        assert "large_output_policy.py" in hermes_install._ENGINE_SCRIPTS
        assert "large_output_policy" in {row[0] for row in feature_health.FEATURES}

        story_js = (REPO / "skills" / "token-optimizer" / "assets" / "savings_story.js").read_text(encoding="utf-8")
        assert "large_output_policy" in story_js

        fp = SCRIPTS / "fixtures" / "large_output_policy.json"
        fixtures = json.loads(fp.read_text(encoding="utf-8"))
        assert len(fixtures) >= 3
        for fx in fixtures:
            passed, res = benchmark.run_single_fixture(fx, compressor=lambda c, r, **kw: r)
            assert passed, f"Benchmark fixture failed: {fx['name']} -> {res}"
