#!/usr/bin/env python3
"""/clear and compaction empty the model's context; "already in context" must follow.

Claude Code keeps the session id across /clear in some versions, and always
across a compaction. Every record that lets a hook say "you already have this"
(read ledger, delta baselines, cross-turn command outputs, repeat-command
streaks, re-fetch guard, first-read markers) has to be dropped for THAT session
when SessionStart reports source=clear or source=compact, and only that session.

The Read and SessionStart steps run the real hook scripts in an isolated
snapshot dir; nothing here calls an inner function the host would not.

Run: python3 -m pytest tests/test_context_reset_on_clear.py -q
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
READ_CACHE = SCRIPTS / "read_cache.py"
REFETCH_GUARD = SCRIPTS / "refetch_guard.py"
SESSIONSTART_RUNNER = REPO / "hooks" / "sessionstart_runner.py"

SESSION = "11111111-2222-3333-4444-555555555555"

# In-context tables a clear must empty, each with its own fixed count query.
_CLEARED_TABLE_COUNTS = {
    "file_reads": "SELECT COUNT(*) FROM file_reads",
    "cached_content": "SELECT COUNT(*) FROM cached_content",
    "command_outputs": "SELECT COUNT(*) FROM command_outputs",
    "command_run_streaks": "SELECT COUNT(*) FROM command_run_streaks",
    "hint_serves": "SELECT COUNT(*) FROM hint_serves",
}
OTHER_SESSION = "99999999-2222-3333-4444-555555555555"


def _env(root: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE_", "TOKEN_OPTIMIZER_", "CODEX_"))}
    home = root / "home"
    home.mkdir(exist_ok=True)
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(root / "snap"),
        "TOKEN_OPTIMIZER_READ_CACHE": "1",
        "TOKEN_OPTIMIZER_NO_PROC_SCAN": "1",
    })
    env.update(extra)
    return env


def _run(script: Path, root: Path, payload: dict, *args: str, **env_extra):
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        input=json.dumps(payload), capture_output=True, text=True,
        env=_env(root, **env_extra), timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc


def _markdown(root: Path) -> Path:
    path = root / "notes.md"
    path.write_text(
        "# Notes\n\n" + "\n".join(
            f"## Section {i}\n\n" + "lorem ipsum dolor sit amet " * 40 for i in range(12)),
        encoding="utf-8")
    return path


_tool_use = iter(range(1, 10_000))


def _read(root: Path, path: Path, session: str = SESSION, agent: str | None = None):
    """One PreToolUse Read through the real read_cache.py. Returns the decision."""
    payload = {
        "session_id": session, "hook_event_name": "PreToolUse", "tool_name": "Read",
        "tool_input": {"file_path": str(path)}, "tool_use_id": f"toolu_{next(_tool_use)}",
    }
    if agent:
        payload["agent_id"] = agent
    out = _run(READ_CACHE, root, payload, "--quiet").stdout.strip()
    if not out:
        return "allow", ""
    hook = json.loads(out)["hookSpecificOutput"]
    return hook.get("permissionDecision", "allow"), hook.get("permissionDecisionReason", "")


def _session_start(root: Path, source: str, session: str = SESSION):
    started = time.monotonic()
    proc = _run(SESSIONSTART_RUNNER, root, {
        "session_id": session, "hook_event_name": "SessionStart",
        "source": source, "transcript_path": "",
    })
    proc.elapsed_s = round(time.monotonic() - started, 2)  # diagnostics only
    return proc


def _store(root: Path, session: str = SESSION):
    sys.path.insert(0, str(SCRIPTS))
    try:
        from session_store import SessionStore
    finally:
        sys.path.remove(str(SCRIPTS))
    return SessionStore(session, snapshot_dir=root / "snap")


# --- the reported failure ----------------------------------------------------

@pytest.mark.parametrize("source", ["clear", "compact"])
def test_read_after_reset_returns_the_full_file(tmp_path, source):
    md = _markdown(tmp_path)
    assert _read(tmp_path, md)[0] == "allow"
    decision, reason = _read(tmp_path, md)
    assert decision == "deny" and "already in context" in reason, "precondition: dedup is live"

    _session_start(tmp_path, source)

    assert _read(tmp_path, md)[0] == "allow", (
        f"SessionStart source={source} must forget the earlier read; the model no longer holds the file")


@pytest.mark.parametrize("source", ["startup", "resume"])
def test_plain_start_keeps_the_read_ledger(tmp_path, source):
    md = _markdown(tmp_path)
    _read(tmp_path, md)
    _session_start(tmp_path, source)
    assert _read(tmp_path, md)[0] == "deny"


def test_reset_is_scoped_to_the_cleared_session_and_leaves_subagents(tmp_path):
    md = _markdown(tmp_path)
    for kwargs in ({}, {"session": OTHER_SESSION}, {"agent": "agent-A"}):
        _read(tmp_path, md, **kwargs)
        assert _read(tmp_path, md, **kwargs)[0] == "deny"

    _session_start(tmp_path, "clear")

    assert _read(tmp_path, md)[0] == "allow"
    assert _read(tmp_path, md, session=OTHER_SESSION)[0] == "deny", "another session was wiped"
    assert _read(tmp_path, md, agent="agent-A")[0] == "deny", "a subagent's ledger was wiped"


def test_edited_file_is_not_served_as_a_delta_after_reset(tmp_path):
    md = _markdown(tmp_path)
    _read(tmp_path, md)
    md.write_text(md.read_text(encoding="utf-8") + "\n## Added\n\nnew text\n", encoding="utf-8")
    _session_start(tmp_path, "clear")
    decision, reason = _read(tmp_path, md)
    assert decision == "allow", reason
    store = _store(tmp_path)
    try:
        # The baseline now matches the file the model just read, not the pre-clear copy.
        cached = store.get_cached_content(str(md.resolve()))
        assert cached is None or "new text" in cached["content"]
    finally:
        store.close()


# --- the rest of the class ---------------------------------------------------

def test_reset_drops_every_in_context_record_and_keeps_session_records(tmp_path):
    store = _store(tmp_path)
    try:
        store.upsert_file_entry("/p/a.md", {"mtime_ns": 1, "size_bytes": 2, "tokens_est": 1})
        store.upsert_cached_content("/p/a.md", "old", "h")
        store.insert_command_output("c1", "git status", "o1", 10, "text")
        store.upsert_command_streak("c1", "git status", "o1", 3, None, time.time())
        store.record_hint_serve(["/p/a.md"])
        store.set_meta("shadow_fr:/p/a.md", "{}")
        store.set_meta("active_fr:/p/a.md", "{}")
        store.set_meta("compact_scan_cursor", "{}")
        store.set_meta("session_decisions", "[]")
        store.insert_tool_output("t1", "Bash", "bash", "ls", "h", 3, 1, "x")
        store.insert_intel_event("Bash", "t1", "ran ls", 3)
    finally:
        store.close()

    _session_start(tmp_path, "clear")

    db = sqlite3.connect(str(tmp_path / "snap" / "session-store" / f"{SESSION}.db"))
    try:
        for table, count_sql in _CLEARED_TABLE_COUNTS.items():
            assert db.execute(count_sql).fetchone()[0] == 0, table
        keys = {r[0] for r in db.execute("SELECT key FROM session_meta")}
        assert "shadow_fr:/p/a.md" not in keys and "active_fr:/p/a.md" not in keys
        assert {"compact_scan_cursor", "session_decisions"} <= keys
        assert db.execute("SELECT COUNT(*) FROM tool_outputs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM context_intel_events").fetchone()[0] == 1
    finally:
        db.close()


_DEDUP_PROBE = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, {scripts!r})
    import bash_compress_hook as h
    out = "build step output line\\n" * 40
    print(repr(h._crossturn_dedup("make build", out)))
""")


def _dedup_probe(root: Path) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", _DEDUP_PROBE.format(scripts=str(SCRIPTS))],
        capture_output=True, text=True, timeout=120,
        env=_env(root, CLAUDE_SESSION_ID=SESSION))
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout.strip().splitlines()[-1]


def test_cross_turn_bash_dedup_does_not_point_at_cleared_output(tmp_path):
    assert _dedup_probe(tmp_path) == "None"
    assert "identical to your previous" in _dedup_probe(tmp_path), "precondition: dedup is live"

    _session_start(tmp_path, "clear")

    assert _dedup_probe(tmp_path) == "None", "a reference to output the model no longer holds"


def _seed_mcp_archive(root: Path, session: str, tool: str, tool_input: dict) -> None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        from refetch_fingerprint import ARGS_HASH_KEY, tool_fingerprint
    finally:
        sys.path.remove(str(SCRIPTS))
    archive = root / "snap" / "tool-archive" / session
    archive.mkdir(parents=True)
    meta = {"tool_name": tool, "tool_use_id": "toolu_arch1", "tokens_est": 5000,
            ARGS_HASH_KEY: tool_fingerprint(tool, tool_input),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 5))}
    (archive / "toolu_arch1.json").write_text(json.dumps({**meta, "response": "big result"}), encoding="utf-8")
    (archive / "manifest.jsonl").write_text(json.dumps(meta) + "\n", encoding="utf-8")


def _refetch(root: Path, tool: str, tool_input: dict) -> str:
    out = _run(REFETCH_GUARD, root, {
        "session_id": SESSION, "hook_event_name": "PreToolUse", "tool_name": tool,
        "tool_input": tool_input, "tool_use_id": "toolu_new"}).stdout
    return json.loads(out)["hookSpecificOutput"].get("permissionDecision", "allow")


def test_refetch_guard_allows_a_call_whose_result_was_cleared(tmp_path):
    tool, args = "mcp__github__get_file_contents", {"owner": "a", "repo": "b", "path": "README.md"}
    _seed_mcp_archive(tmp_path, SESSION, tool, args)
    assert _refetch(tmp_path, tool, args) == "deny", "precondition: guard is live"

    time.sleep(1.1)  # manifest timestamps have one-second resolution
    start = _session_start(tmp_path, "clear")

    decision = _refetch(tmp_path, tool, args)
    if decision != "allow":
        # Failure-only context (does not change the pass condition): was the clear marker written, how
        # long did the SessionStart runner take against its shared 18s deadline, and what did it say.
        # Built lazily and guarded so a diagnostics problem can never fail a passing run or mask this one.
        try:
            marker = tmp_path / "snap" / "tool-archive" / SESSION / "context_reset_at"
            seen = marker.read_text(encoding="utf-8").strip() if marker.exists() else None
        except Exception as exc:  # noqa: BLE001
            seen = "unreadable: %s" % type(exc).__name__
        detail = ("SessionStart clear diagnostics: rc=%s elapsed_s=%s (runner budget 18s) reset_marker_value=%s "
                  "stderr=%r stdout=%r" % (start.returncode, start.elapsed_s, seen,
                                           start.stderr[-600:], start.stdout[-300:]))
    else:
        detail = ""
    assert decision == "allow", detail


def test_reset_waits_out_a_short_write_lock(tmp_path):
    """A sibling hook holding the session db's write lock must not turn the reset into a no-op."""
    import threading

    md = _markdown(tmp_path)
    _read(tmp_path, md)
    db = sqlite3.connect(str(tmp_path / "snap" / "session-store" / f"{SESSION}.db"),
                         check_same_thread=False, isolation_level=None)
    db.execute("BEGIN IMMEDIATE")
    release = threading.Timer(1.0, lambda: db.execute("ROLLBACK"))
    release.start()
    try:
        _session_start(tmp_path, "clear")
    finally:
        release.join()
        db.close()
    assert _read(tmp_path, md)[0] == "allow"


# --- the hooks.json wiring ---------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="drives the hooks.json command through /bin/sh")
def test_hooks_json_session_start_resets_on_clear(tmp_path, monkeypatch):
    # A fresh HOME has no bytecode cache; wait for the one-time compile instead
    # of letting the first hook skip itself.
    monkeypatch.setenv("TOKEN_OPTIMIZER_COLD_COMPILE_WAIT_S", "300")
    # A cold interpreter on a loaded CI box can outlast the PreToolUse budget,
    # which exits silently; this test checks the decision, not the latency.
    monkeypatch.setenv("TOKEN_OPTIMIZER_HOOK_BUDGET_MS", "60000")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _hook_harness import REPO as HREPO, consented_home, hook_command, run_hook

    home = consented_home(tmp_path)
    md = _markdown(tmp_path)

    def read():
        payload = {"session_id": SESSION, "hook_event_name": "PreToolUse", "tool_name": "Read",
                   "tool_input": {"file_path": str(md)}, "tool_use_id": f"toolu_{next(_tool_use)}",
                   "cwd": str(tmp_path)}
        proc = run_hook(hook_command("PreToolUse", "read_cache.py --quiet"), HREPO, payload, home,
                        tmp_path / "raises.jsonl")
        assert proc.returncode == 0, proc.stderr[-2000:]
        out = proc.stdout.strip()
        return json.loads(out)["hookSpecificOutput"].get("permissionDecision", "allow") if out else "allow"

    assert read() == "allow"
    assert read() == "deny"
    start = {"session_id": SESSION, "hook_event_name": "SessionStart", "source": "clear",
             "transcript_path": "", "cwd": str(tmp_path)}
    proc = run_hook(hook_command("SessionStart", "sessionstart_runner.py"), HREPO, start, home,
                    tmp_path / "raises.jsonl")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert read() == "allow"
