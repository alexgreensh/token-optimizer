#!/usr/bin/env python3
"""Regression tests for safe delta reads and edit invalidation.

Test 1 (delta_negative_savings_guard): a small file whose unified diff would be
LARGER than the file itself must NOT be served as a delta. Before the fix,
read_cache.py served the diff anyway, producing net-negative savings (e.g.
wait-status.txt: 47-token file, 174-token diff -> -127 tokens). The fix adds a
guard: if delta_tokens >= old_tokens, skip the delta and fall through to a
normal full re-read.

Test 2 (delta_served_when_smaller): a larger file where the diff IS smaller than
the file must still get the delta. This is the positive case, the guard must not
over-fire.

Test 3 (edit_invalidate_refreshes_not_deletes): after an Edit, the cache entry
must be UPDATED with the post-edit file state (mtime, size, content), not
DELETED. Before the fix, handle_invalidate deleted the entire entry, forcing the
next read into the first-read path (full re-read, no savings) and starving delta
mode of its primary trigger (read -> edit -> re-read).

Run: python3 -m pytest tests/test_delta_negative_savings_guard.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
READ_CACHE = SCRIPTS / "read_cache.py"

SESSION_S = "11111111-2222-3333-4444-555555555555"


def _seed_cache_entry(snapshot_dir: Path, session_id: str, file_path: str,
                      content: str, mtime_ns: int, size_bytes: int):
    """Seed both a file_reads row and a cached_content row."""
    sys.path.insert(0, str(SCRIPTS))
    try:
        from session_store import SessionStore
        from delta_diff import content_hash
    finally:
        if str(SCRIPTS) in sys.path:
            sys.path.remove(str(SCRIPTS))

    store = SessionStore(session_id, snapshot_dir=snapshot_dir)
    try:
        store.upsert_file_entry(file_path, {
            "mtime_ns": mtime_ns,
            "size_bytes": size_bytes,
            "ranges_seen": [[0, 0]],
            "tokens_est": max(1, size_bytes // 4),
            "read_count": 1,
            "last_access": time.time(),
            "content_hash": content_hash(content),
            "cached_content": content,
        })
        store.upsert_cached_content(file_path, content, content_hash(content))
    finally:
        store.close()


def _get_file_entry(snapshot_dir: Path, session_id: str, file_path: str):
    sys.path.insert(0, str(SCRIPTS))
    try:
        from session_store import SessionStore
    finally:
        if str(SCRIPTS) in sys.path:
            sys.path.remove(str(SCRIPTS))
    store = SessionStore(session_id, snapshot_dir=snapshot_dir)
    try:
        return store.get_file_entry(file_path)
    finally:
        store.close()


def _get_cached_content(snapshot_dir: Path, session_id: str, file_path: str):
    sys.path.insert(0, str(SCRIPTS))
    try:
        from session_store import SessionStore
    finally:
        if str(SCRIPTS) in sys.path:
            sys.path.remove(str(SCRIPTS))
    store = SessionStore(session_id, snapshot_dir=snapshot_dir)
    try:
        return store.get_cached_content(file_path)
    finally:
        store.close()


def _run_read_cache(snapshot_dir: Path, args: list[str],
                    stdin_payload: dict | None, extra_env: dict | None = None):
    env = dict(os.environ)
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(snapshot_dir)
    env.setdefault("TOKEN_OPTIMIZER_READ_CACHE", "1")
    env.setdefault("TOKEN_OPTIMIZER_READ_CACHE_DELTA", "1")
    # Suppress context-pressure gate so the hook always runs in tests.
    env["TOKEN_OPTIMIZER_CONTEXT_PRESSURE_LEVEL"] = "normal"
    if extra_env:
        env.update(extra_env)
    stdin_data = json.dumps(stdin_payload) if stdin_payload is not None else None
    return subprocess.run(
        [sys.executable, str(READ_CACHE), *args],
        input=stdin_data,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _pretool_read_payload(file_path: str, session_id: str = SESSION_S) -> dict:
    return {
        "hookEventName": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": file_path},
        "session_id": session_id,
        "agent_id": session_id,
    }


# ---------------------------------------------------------------------------
# Test 1: delta guard skips diff larger than file (negative savings fix)
# ---------------------------------------------------------------------------

def test_delta_negative_savings_guard(tmp_path):
    """A small file whose diff is larger than the file must NOT be served as
    a delta. The hook should allow the full re-read instead."""
    # Create a tiny file (10 bytes ~ 3 tokens) and seed old content.
    f = tmp_path / "small.txt"
    f.write_text("hello\n", encoding="utf-8")
    st = os.stat(str(f))

    old_content = "world\n"
    _seed_cache_entry(tmp_path, SESSION_S, str(f), old_content,
                      st.st_mtime_ns - 1_000_000, len(old_content))

    # Now modify the file so the diff (with headers/context) will be larger
    # than the file itself.
    f.write_text("world\nnew line\nanother\n", encoding="utf-8")
    os.utime(str(f), ns=(time.time_ns(), time.time_ns()))
    # Ensure mtime differs from the seeded value.
    time.sleep(0.01)
    os.utime(str(f), ns=(time.time_ns(), time.time_ns()))

    out = _run_read_cache(tmp_path, ["--quiet"], _pretool_read_payload(str(f)))
    assert out.returncode == 0, out.stderr

    # Parse the hook output. A delta serve would be permissionDecision=deny
    # with additionalContext containing diff headers (+/- lines).
    payload = json.loads(out.stdout.strip()) if out.stdout.strip() else {}
    hook_out = payload.get("hookSpecificOutput", {})
    decision = hook_out.get("permissionDecision", "")

    # The guard must prevent the delta from being served. Either:
    # - no output (allow, no deny), or
    # - permissionDecision is not "deny", or
    # - if deny, additionalContext must NOT contain diff markers.
    if decision == "deny":
        ctx = hook_out.get("additionalContext", "")
        assert "---" not in ctx and "+++" not in ctx, (
            f"Delta was served despite diff being larger than file. "
            f"additionalContext={ctx[:200]}"
        )


# ---------------------------------------------------------------------------
# Test 2: delta IS served when the diff is smaller than the file
# ---------------------------------------------------------------------------

def test_delta_served_when_smaller(tmp_path):
    """A larger file with a small change must still get the delta."""
    # Create a file large enough that a 1-line diff is smaller than the file.
    lines = [f"line {i}\n" for i in range(100)]
    f = tmp_path / "large.py"
    f.write_text("".join(lines), encoding="utf-8")
    st = os.stat(str(f))

    old_content = "".join(lines)
    _seed_cache_entry(tmp_path, SESSION_S, str(f), old_content,
                      st.st_mtime_ns - 1_000_000, len(old_content))

    # Make a tiny 1-line change.
    lines[50] = "line 50 MODIFIED\n"
    f.write_text("".join(lines), encoding="utf-8")
    time.sleep(0.01)
    os.utime(str(f), ns=(time.time_ns(), time.time_ns()))

    out = _run_read_cache(tmp_path, ["--quiet"], _pretool_read_payload(str(f)))
    assert out.returncode == 0, out.stderr

    payload = json.loads(out.stdout.strip()) if out.stdout.strip() else {}
    hook_out = payload.get("hookSpecificOutput", {})
    decision = hook_out.get("permissionDecision", "")
    ctx = hook_out.get("additionalContext", "")

    assert decision == "deny", (
        f"Delta should be served for a large file with a small change; "
        f"got decision={decision}, stdout={out.stdout[:300]}"
    )
    assert "MODIFIED" in ctx or "+" in ctx, (
        f"additionalContext should contain the diff; got={ctx[:200]}"
    )


# ---------------------------------------------------------------------------
# Test 3: edit invalidation refreshes the cache instead of deleting it
# ---------------------------------------------------------------------------

def test_edit_invalidate_refreshes_not_deletes(tmp_path):
    """After an Edit, the cache entry must be UPDATED (not deleted) with the
    post-edit file state, so the next read can hit structure-map or delta."""
    f = tmp_path / "code.py"
    f.write_text("def foo():\n    return 1\n", encoding="utf-8")
    st = os.stat(str(f))

    old_content = "def foo():\n    return 1\n"
    _seed_cache_entry(tmp_path, SESSION_S, str(f), old_content,
                      st.st_mtime_ns, len(old_content))

    # Simulate an edit: modify the file, then run --invalidate.
    f.write_text("def foo():\n    return 2\n", encoding="utf-8")
    time.sleep(0.01)
    os.utime(str(f), ns=(time.time_ns(), time.time_ns()))

    invalidate_payload = {
        "hookEventName": "PostToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(f)},
        "session_id": SESSION_S,
        "agent_id": SESSION_S,
    }
    out = _run_read_cache(tmp_path, ["--invalidate", "--quiet"], invalidate_payload)
    assert out.returncode == 0, out.stderr

    # The entry must still exist (not deleted).
    entry = _get_file_entry(tmp_path, SESSION_S, str(f))
    assert entry is not None, (
        "Cache entry was DELETED after edit; it should be REFRESHED instead."
    )

    # The entry's mtime should match the current file (refreshed, not stale).
    new_st = os.stat(str(f))
    assert int(entry.get("mtime_ns", 0)) == new_st.st_mtime_ns, (
        f"Cache entry mtime was not refreshed to post-edit value; "
        f"entry mtime_ns={entry.get('mtime_ns')}, file mtime_ns={new_st.st_mtime_ns}"
    )

    # The cached content should be updated to the post-edit version.
    cached = _get_cached_content(tmp_path, SESSION_S, str(f))
    assert cached is not None, (
        "Cached content was DELETED after edit; it should be REFRESHED."
    )
    assert "return 2" in cached.get("content", ""), (
        f"Cached content was not refreshed to post-edit version; "
        f"got={cached.get('content', '')[:100]}"
    )


def test_edit_then_external_change_serves_delta(tmp_path):
    """The normal read -> Edit -> outside change -> Read flow must reach delta.

    Before edit refresh, the Edit hook deleted the row and the final Read was
    treated as a first read, so no delta was emitted. This is the end-to-end
    regression for the feature's primary trigger.
    """
    lines = [f"line {i}\n" for i in range(100)]
    f = tmp_path / "flow.py"
    initial = "".join(lines)
    f.write_text(initial, encoding="utf-8")
    st = os.stat(str(f))
    _seed_cache_entry(tmp_path, SESSION_S, str(f), initial,
                      st.st_mtime_ns, len(initial))

    edited_lines = list(lines)
    edited_lines[50] = "line 50 edited by agent\n"
    f.write_text("".join(edited_lines), encoding="utf-8")
    time.sleep(0.01)
    os.utime(str(f), ns=(time.time_ns(), time.time_ns()))

    invalidate_payload = {
        "hookEventName": "PostToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(f)},
        "session_id": SESSION_S,
        "agent_id": SESSION_S,
    }
    invalidated = _run_read_cache(
        tmp_path, ["--invalidate", "--quiet"], invalidate_payload
    )
    assert invalidated.returncode == 0, invalidated.stderr

    # Simulate an outside writer after the agent's edit. This is the state in
    # which delta mode should compare the refreshed post-edit cache entry.
    external = "".join(edited_lines) + "line added outside agent\n"
    f.write_text(external, encoding="utf-8")
    time.sleep(0.01)
    os.utime(str(f), ns=(time.time_ns(), time.time_ns()))

    out = _run_read_cache(tmp_path, ["--quiet"], _pretool_read_payload(str(f)))
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout.strip()) if out.stdout.strip() else {}
    hook_out = payload.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") == "deny", (
        "The post-edit external change should be served as a delta; "
        f"stdout={out.stdout[:300]}"
    )
    assert "line added outside agent" in hook_out.get("additionalContext", "")
