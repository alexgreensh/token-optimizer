"""Tests for B6: safe read-only pipeline compression (pipe_compress) and
proof-backed unchanged Bash file re-read detection (bash_reread_unchanged).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
HOOK = SCRIPTS / "bash_compress_hook.py"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipe_file_compress as pfc  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures (real-shaped CLI outputs)
# ---------------------------------------------------------------------------

def _grep_output(n: int = 80, extra_line: str = "") -> str:
    lines = [
        f"src/service_{i % 5:02d}.py:{10 + i}:    logger.info('handling request_id={i:04d} status=ok')"
        for i in range(n)
    ]
    if extra_line:
        lines.append(extra_line)
    return "\n".join(lines) + "\n"


def _git_diff_output(n_files: int = 16) -> str:
    chunks = []
    for i in range(n_files):
        chunks.append(
            f"diff --git a/src/mod_{i:02d}.py b/src/mod_{i:02d}.py\n"
            f"index 1111111..2222222 100644\n"
            f"--- a/src/mod_{i:02d}.py\n"
            f"+++ b/src/mod_{i:02d}.py\n"
            f"@@ -10,4 +10,4 @@ def handler_{i}():\n"
            f"     x = {i}\n"
            f"-    return x + 1\n"
            f"+    return x + 2\n"
        )
    return "".join(chunks)


def _python_source_file(n_lines: int = 60) -> str:
    lines = ["#!/usr/bin/env python3", '"""Sample module for cat reread tests."""', ""]
    for i in range(n_lines):
        lines.append(f"def compute_{i:03d}(x: int) -> int:")
        lines.append(f"    return (x * {i + 1}) + {i}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _run_hook(
    command: str,
    stdout: str,
    *,
    home: Path,
    session_id: str = "11111111-2222-3333-4444-555555555555",
    transcript_path: str = "",
    cwd: str = "",
    tool_use_id: str = "toolu_01",
    exit_code: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess:
    payload = {
        "session_id": session_id,
        "tool_name": "Bash",
        "tool_use_id": tool_use_id,
        "tool_input": {"command": command},
        "tool_response": {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "interrupted": False,
            "isImage": False,
        },
    }
    if transcript_path:
        payload["transcript_path"] = transcript_path
    if cwd:
        payload["cwd"] = cwd
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["CLAUDE_SESSION_ID"] = session_id
    env.pop("TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT", None)
    env.pop("TOKEN_OPTIMIZER_HOLDOUT", None)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
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
# Step 2: Safe pipeline classification & compression (pipe_compress)
# ---------------------------------------------------------------------------

class TestPipeCompressSafe:
    @pytest.mark.parametrize("cmd,expected_pattern", [
        ("grep -rnE 'ERROR|WARN' src/ | sort", "search_results"),
        ("rg 'foo|bar' src/ | sort | uniq", "search_results"),
        ("cd /repo && grep -rnE 'foo|bar' src/ | sort", "search_results"),
        ("cd /repo; grep 'a;b' src/", "search_results"),
        ("export LC_ALL=C && rg 'TODO' src/ | head", "search_results"),
        ("cd /repo && git diff", "git_diff"),
        ("cat app.log | grep -rnE 'timeout|retry'", "search_results"),
        ("git log | grep feat", "search_results"),
        ("cd /repo && git -C /other grep -n 'needle'", "search_results"),
        ("grep -rn 'foo|bar' src/ 2>/dev/null | sort", "search_results"),
    ])
    def test_safe_pipelines_classified(self, cmd, expected_pattern):
        res = pfc.classify_safe_pipeline(cmd)
        assert res is not None, f"Expected safe classification for: {cmd}"
        _producer, pattern = res
        assert pattern == expected_pattern

    def test_grep_alternation_sort_compresses_and_preserves_signal(self):
        raw = _grep_output(
            80,
            extra_line="src/auth.py:99:    raise ValueError('key AKIAIOSFODNN7EXAMPLE rejected')",
        )
        comp = pfc.compress_pipe("grep -rnE 'ERROR|WARN' src/ | sort", raw)
        assert comp is not None
        assert len(comp) < len(raw) * 0.85
        assert "src/auth.py:99" in comp
        assert "AKIAIOSFODNN7EXAMPLE" in comp

    def test_cd_git_diff_compresses_and_preserves_files(self):
        raw = _git_diff_output(18)
        comp = pfc.compress_pipe("cd /repo && git diff", raw)
        assert comp is not None
        assert len(comp) < len(raw) * 0.85
        assert "src/mod_00.py" in comp
        assert "src/mod_17.py" in comp

    def test_deterministic_output(self):
        raw = _grep_output(60)
        cmd = "cd /repo && grep -rnE 'foo|bar' src/ | sort"
        c1 = pfc.compress_pipe(cmd, raw)
        c2 = pfc.compress_pipe(cmd, raw)
        assert c1 is not None
        assert c1 == c2


class TestPipeCompressSafetyRefusals:
    """One regression test per safety rule: unsafe commands always return None."""

    @pytest.mark.parametrize("cmd", [
        "rg 'TODO' src/ | head -n 50",
        "grep foo src/ | tail -n 30",
        "rg 'TODO' src/ | head -50",
        "grep foo src/ | tail -30",
        "sed -n '10,40p' src/app.py",
        "sed -n 10,40p src/app.py",
        "head -n 100 src/app.py",
        "tail -n 100 src/app.py",
        "cat f.py | sed -n '1,50p'",
    ])
    def test_explicit_output_limits_refused(self, cmd):
        assert pfc.classify_safe_pipeline(cmd) is None
        assert pfc.compress_pipe(cmd, _grep_output(60)) is None

    @pytest.mark.parametrize("cmd", [
        "grep foo a.py && grep bar b.py",
        "echo start; grep foo a.py; echo end",
        "ls src/; git status",
        "cd /repo && grep foo a.py && git diff",
    ])
    def test_mixed_multi_producer_refused(self, cmd):
        assert pfc.classify_safe_pipeline(cmd) is None

    @pytest.mark.parametrize("cmd", [
        "for f in *.py; do grep foo \"$f\"; done",
        "while read line; do echo \"$line\"; done",
        "if true; then grep foo a.py; fi",
        "grep foo a.py || true",
        "(cd /repo && git diff)",
        "{ grep foo a.py; }",
    ])
    def test_loops_conditionals_subshells_refused(self, cmd):
        assert pfc.classify_safe_pipeline(cmd) is None

    @pytest.mark.parametrize("cmd", [
        "grep $(whoami) /etc/passwd",
        "grep `whoami` /etc/passwd",
        "grep \"$(cat pat)\" src/",
        "diff <(cat a) <(cat b)",
        "cat <<EOF",
    ])
    def test_substitutions_and_heredocs_refused(self, cmd):
        assert pfc.classify_safe_pipeline(cmd) is None

    @pytest.mark.parametrize("cmd", [
        "grep foo src/ > out.txt",
        "grep foo src/ >> out.txt",
        "grep foo src/ | tee out.txt",
        "grep foo src/ | xargs rm",
        "cat a.py;rm -rf /",
        "grep foo src/|rm -rf /",
        "cd /repo && sed -i 's/a/b/' f.txt",
        "grep foo src/ &",
        "cd /repo\ngrep foo src/",
    ])
    def test_redirects_writes_and_glued_attacks_refused(self, cmd):
        assert pfc.classify_safe_pipeline(cmd) is None

    @pytest.mark.parametrize("cmd", [
        "grep foo src/ | cut -d: -f2",
        "cd /repo && git diff | sort",
        "cat src/app.py | sort",
        "git show HEAD:src/app.py",
    ])
    def test_shape_destroying_or_file_content_refused(self, cmd):
        assert pfc.classify_safe_pipeline(cmd) is None

    def test_short_output_untouched(self):
        short = "\n".join(f"a.py:{i}:match" for i in range(10)) + "\n"
        assert pfc.compress_pipe("grep -rnE 'a|b' src/ | sort", short) is None

    def test_failure_exit_code_and_stderr_untouched(self):
        raw = _grep_output(60)
        assert pfc.compress_pipe("grep -rnE 'a|b' src/ | sort", raw, returncode=1) is None
        assert pfc.compress_pipe(
            "grep -rnE 'a|b' src/ | sort", raw, returncode=0, stderr="grep: src/: Permission denied\n"
        ) is None

    def test_unclosed_quote_fails_open(self):
        assert pfc.classify_safe_pipeline("grep -E 'unclosed src/ | sort") is None
        assert pfc.compress_pipe("grep -E 'unclosed src/ | sort", _grep_output(60)) is None


# ---------------------------------------------------------------------------
# Step 3: Unchanged Bash `cat` file re-read detection (bash_reread_unchanged)
# ---------------------------------------------------------------------------

class TestBashRereadUnchanged:
    def test_parse_unranged_cat_accepts_bare_and_cd(self):
        assert pfc.parse_unranged_cat("cat /tmp/foo.py") == "/tmp/foo.py"
        assert pfc.parse_unranged_cat("cat src/app.py", cwd="/repo") == "/repo/src/app.py"
        assert pfc.parse_unranged_cat("cd /repo && cat src/app.py") == "/repo/src/app.py"
        assert pfc.parse_unranged_cat("cat -- /tmp/foo.py") == "/tmp/foo.py"

    @pytest.mark.parametrize("cmd", [
        "cat -n /tmp/foo.py",
        "cat /tmp/a.py /tmp/b.py",
        "cat /tmp/*.py",
        "cat /tmp/foo.py | head",
        "cat /tmp/foo.py > /tmp/out.py",
        "cat -",
        "cat /dev/stdin",
        "cat relative.py",  # no cwd
    ])
    def test_parse_unranged_cat_rejects_unsafe_or_flagged(self, cmd):
        assert pfc.parse_unranged_cat(cmd, cwd="") is None

    def test_reread_lifecycle_and_compaction_invalidation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        # Re-point SessionStore dir to tmp_path
        import session_store as ss
        monkeypatch.setattr(ss, "SESSION_STORE_DIR", tmp_path / "session-store")

        target = tmp_path / "sample.py"
        body = _python_source_file(50)
        target.write_text(body, encoding="utf-8")
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n', encoding="utf-8")

        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        # 1. First cat -> returns None (full output verbatim), records full read
        first = pfc.try_bash_reread_unchanged(
            f"cat {target}", body,
            session_id=sid, store_id=sid,
            transcript_path=str(transcript), tool_use_id="tu_1",
        )
        assert first is None

        # 2. Same tool_use_id (duplicate hook fire) -> returns None
        dup = pfc.try_bash_reread_unchanged(
            f"cat {target}", body,
            session_id=sid, store_id=sid,
            transcript_path=str(transcript), tool_use_id="tu_1",
        )
        assert dup is None

        # 3. Second cat of same unchanged file -> returns short unchanged note
        second = pfc.try_bash_reread_unchanged(
            f"cat {target}", body,
            session_id=sid, store_id=sid,
            transcript_path=str(transcript), tool_use_id="tu_2",
        )
        assert second is not None
        assert "unchanged" in second.lower()
        assert "sample.py" in second
        # Deterministic: repeating with tu_3 gives byte-identical note
        third = pfc.try_bash_reread_unchanged(
            f"cat {target}", body,
            session_id=sid, store_id=sid,
            transcript_path=str(transcript), tool_use_id="tu_3",
        )
        assert third == second

        # 4. Missing transcript_path -> cannot prove still in context -> None
        no_tr = pfc.try_bash_reread_unchanged(
            f"cat {target}", body,
            session_id=sid, store_id=sid,
            transcript_path="", tool_use_id="tu_4",
        )
        assert no_tr is None

        # 5. Microcompact boundary appended -> invalidates -> returns None
        with open(transcript, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "system", "subtype": "microcompact_boundary", "uuid": "mc-1"}) + "\n")
        after_mc = pfc.try_bash_reread_unchanged(
            f"cat {target}", body,
            session_id=sid, store_id=sid,
            transcript_path=str(transcript), tool_use_id="tu_5",
        )
        assert after_mc is None

    def test_ranged_or_edited_or_skeletonized_read_always_verbatim(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        import session_store as ss
        monkeypatch.setattr(ss, "SESSION_STORE_DIR", tmp_path / "session-store")

        target = tmp_path / "edit_me.py"
        body = _python_source_file(50)
        target.write_text(body, encoding="utf-8")
        st = os.stat(target)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"type":"user"}\n', encoding="utf-8")
        sid = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"

        store = ss.SessionStore(sid)
        try:
            # Case A: prior read was ranged [10, 50], NOT full [0, 0]
            store.upsert_file_entry(str(target), {
                "mtime_ns": st.st_mtime_ns, "size_bytes": st.st_size,
                "ranges_seen": [[10, 50]], "tokens_est": 500, "read_count": 1,
                "last_access": time.time(), "last_replacement_type": "",
                "last_tool_use_id": "tu_ranged",
            })
        finally:
            store.close()
        assert pfc.try_bash_reread_unchanged(
            f"cat {target}", body, session_id=sid, store_id=sid,
            transcript_path=str(transcript), tool_use_id="tu_cat_a",
        ) is None

        # Case B: file is being edited (ranges_seen cleared to [] by Edit hook)
        store = ss.SessionStore(sid)
        try:
            store.upsert_file_entry(str(target), {
                "mtime_ns": st.st_mtime_ns, "size_bytes": st.st_size,
                "ranges_seen": [], "tokens_est": 500, "read_count": 1,
                "last_access": time.time(), "last_replacement_type": "",
                "last_tool_use_id": "tu_edit",
            })
        finally:
            store.close()
        assert pfc.try_bash_reread_unchanged(
            f"cat {target}", body, session_id=sid, store_id=sid,
            transcript_path=str(transcript), tool_use_id="tu_cat_b",
        ) is None

        # Case C: prior read was replaced by a skeleton (full file not in context)
        store = ss.SessionStore(sid)
        try:
            store.upsert_file_entry(str(target), {
                "mtime_ns": st.st_mtime_ns, "size_bytes": st.st_size,
                "ranges_seen": [[0, 0]], "tokens_est": 500, "read_count": 1,
                "last_access": time.time(), "last_replacement_type": "first_read_skeleton",
                "last_tool_use_id": "tu_skel",
            })
        finally:
            store.close()
        assert pfc.try_bash_reread_unchanged(
            f"cat {target}", body, session_id=sid, store_id=sid,
            transcript_path=str(transcript), tool_use_id="tu_cat_c",
        ) is None

        # Case D: file modified on disk after full read
        store = ss.SessionStore(sid)
        try:
            store.upsert_file_entry(str(target), {
                "mtime_ns": st.st_mtime_ns, "size_bytes": st.st_size,
                "ranges_seen": [[0, 0]], "tokens_est": 500, "read_count": 1,
                "last_access": time.time(), "last_replacement_type": "",
                "last_tool_use_id": "tu_full",
            })
        finally:
            store.close()
        body2 = body + "# edited\n"
        target.write_text(body2, encoding="utf-8")
        assert pfc.try_bash_reread_unchanged(
            f"cat {target}", body2, session_id=sid, store_id=sid,
            transcript_path=str(transcript), tool_use_id="tu_cat_d",
        ) is None


# ---------------------------------------------------------------------------
# End-to-end hook subprocess tests (pipe_compress + bash_reread_unchanged)
# ---------------------------------------------------------------------------

class TestHookEndToEnd:
    def test_hook_pipe_compress_and_logs_measured_feature(self, tmp_path):
        raw = _grep_output(80, extra_line="src/auth.py:99:    # secret AKIAIOSFODNN7EXAMPLE")
        cmd = "cd /repo && grep -rnE 'ERROR|WARN' src/ | sort"
        proc = _run_hook(cmd, raw, home=tmp_path, tool_use_id="tu_pipe_1")
        assert proc.returncode == 0, proc.stderr
        out = _updated_stdout(proc)
        assert out is not None
        assert len(out) < len(raw)
        assert "AKIAIOSFODNN7EXAMPLE" in out
        assert "expand" in out.lower()

        db = tmp_path / ".claude" / "_backups" / "token-optimizer" / "trends.db"
        assert db.exists()
        with sqlite3.connect(str(db)) as conn:
            rows = conn.execute(
                "SELECT feature, tier, verified FROM compression_events WHERE feature = 'pipe_compress'"
            ).fetchall()
        assert rows == [("pipe_compress", "measured", 1)]

    def test_hook_explicit_limit_passes_through_verbatim(self, tmp_path):
        raw = _grep_output(60)
        for cmd in ("rg 'TODO' src/ | head -n 50", "sed -n '10,40p' src/app.py", "tail -n 40 app.log"):
            proc = _run_hook(cmd, raw, home=tmp_path, tool_use_id=f"tu_lim_{hash(cmd)}")
            assert proc.returncode == 0
            assert _updated_stdout(proc) is None, f"Explicit limit should not be shortened: {cmd}"

    def test_hook_cat_first_read_verbatim_then_second_reread_collapsed(self, tmp_path):
        target = tmp_path / "module.py"
        body = _python_source_file(60)
        target.write_text(body, encoding="utf-8")
        transcript = tmp_path / "session.jsonl"
        transcript.write_text('{"type":"user","message":{"role":"user","content":"go"}}\n', encoding="utf-8")

        # First cat: never skeletonized, never chopped -> None (verbatim)
        p1 = _run_hook(
            f"cat {target}", body, home=tmp_path,
            transcript_path=str(transcript), tool_use_id="tu_cat_1",
        )
        assert p1.returncode == 0, p1.stderr
        assert _updated_stdout(p1) is None

        # Second cat of same unchanged file -> short note + expand pointer + measured event
        p2 = _run_hook(
            f"cat {target}", body, home=tmp_path,
            transcript_path=str(transcript), tool_use_id="tu_cat_2",
        )
        assert p2.returncode == 0, p2.stderr
        out2 = _updated_stdout(p2)
        assert out2 is not None
        assert "unchanged" in out2.lower()
        assert "module.py" in out2
        assert "expand" in out2.lower()

        db = tmp_path / ".claude" / "_backups" / "token-optimizer" / "trends.db"
        with sqlite3.connect(str(db)) as conn:
            rows = conn.execute(
                "SELECT feature, tier, verified FROM compression_events WHERE feature = 'bash_reread_unchanged'"
            ).fetchall()
        assert rows == [("bash_reread_unchanged", "measured", 1)]


# ---------------------------------------------------------------------------
# ADDENDUM: savings summary JSON + human labels + benchmark fixtures
# ---------------------------------------------------------------------------

class TestSavingsSummaryAndLabels:
    def test_fake_events_appear_in_savings_summary_json_with_human_labels(self, tmp_path, monkeypatch):
        import measure
        from datetime import datetime

        monkeypatch.setattr(measure, "SNAPSHOT_DIR", tmp_path)
        db_path = tmp_path / "trends.db"
        monkeypatch.setattr(measure, "TRENDS_DB", db_path)

        # Ensure both features are headline-eligible with human labels.
        assert "pipe_compress" in measure._V5_COMPRESSION_CATEGORIES
        assert "bash_reread_unchanged" in measure._V5_COMPRESSION_CATEGORIES
        assert measure._V5_COMPRESSION_LABELS["pipe_compress"] == "Bash pipelines"
        assert measure._V5_COMPRESSION_LABELS["bash_reread_unchanged"] == "Bash unchanged file re-reads"

        # Insert realistic measured events into a fresh trends.db
        conn = measure._init_trends_db()
        now_iso = datetime.now().isoformat()
        real_uuid = "12345678-1234-1234-1234-123456789abc"
        for feat, orig, comp in (
            ("pipe_compress", 5000, 1200),
            ("bash_reread_unchanged", 4000, 80),
        ):
            conn.execute(
                "INSERT INTO compression_events "
                "(timestamp, session_id, session_uuid, feature, command_pattern, "
                " original_tokens, compressed_tokens, compression_ratio, "
                " quality_preserved, verified, detail, model, tier) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1, '', 'claude-sonnet-4-6', 'measured')",
                (now_iso, real_uuid, real_uuid, feat, feat, orig, comp, round(comp / orig, 4)),
            )
        conn.commit()
        conn.close()

        summary = measure._get_merged_savings(days=30)
        by_cat = summary.get("by_category", {})
        assert "pipe_compress" in by_cat
        assert measure._SAVINGS_CATEGORY_LABELS["pipe_compress"] == "Bash pipelines"
        assert by_cat["pipe_compress"]["tokens_saved"] == 3800
        assert by_cat["pipe_compress"]["cost_saved_usd"] > 0

        assert "bash_reread_unchanged" in by_cat
        assert measure._SAVINGS_CATEGORY_LABELS["bash_reread_unchanged"] == "Bash unchanged file re-reads"
        assert by_cat["bash_reread_unchanged"]["tokens_saved"] == 3920
        assert by_cat["bash_reread_unchanged"]["cost_saved_usd"] > 0

        # Also verify the rendered JSON summary (as emitted by `measure.py savings --json`)
        # includes both features with their human labels.
        rendered = json.dumps({
            "by_category": {
                k: {**v, "label": measure._SAVINGS_CATEGORY_LABELS.get(k, k)}
                for k, v in by_cat.items()
            }
        })
        parsed = json.loads(rendered)
        assert parsed["by_category"]["pipe_compress"]["label"] == "Bash pipelines"
        assert parsed["by_category"]["bash_reread_unchanged"]["label"] == "Bash unchanged file re-reads"

    def test_feature_health_and_savings_story_include_new_features(self):
        from _measure import feature_health
        keys = {row[0] for row in feature_health.FEATURES}
        assert "pipe_compress" in keys
        assert "bash_reread_unchanged" in keys

        story_js = (REPO / "skills" / "token-optimizer" / "assets" / "savings_story.js").read_text(encoding="utf-8")
        assert "pipe_compress" in story_js

    def test_benchmark_pipe_compress_fixtures_pass(self):
        import benchmark
        pipe_fixtures = [f for f in benchmark.FIXTURES if f.get("category") == "pipe_compress"]
        assert len(pipe_fixtures) >= 3

        def _comp(cmd, raw, **kw):
            res = pfc.compress_pipe(cmd, raw, returncode=kw.get("returncode", 0), stderr=kw.get("stderr", ""))
            return res if res is not None else raw

        for fx in pipe_fixtures:
            passed, res = benchmark.run_single_fixture(fx, compressor=_comp)
            assert passed, f"Benchmark fixture failed: {fx['name']} -> {res}"
