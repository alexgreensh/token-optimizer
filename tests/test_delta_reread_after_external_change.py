#!/usr/bin/env python3
"""Regression tests for #361: delta mode must not trap the host in a read/edit loop.

When a file changes outside Edit/Write (shell, formatter, the user's editor),
Claude Code refuses the next Edit until a Read of that file actually succeeds.
A denied Read never counts. Delta mode answers the first re-read with a diff
(a deny), which is fine as information, but the follow-up Read must go through
to the host as a real read instead of being answered with another deny.

Run: python3 -m pytest tests/test_delta_reread_after_external_change.py -v
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from test_delta_negative_savings_guard import (
    _get_file_entry,
    _run_read_cache,
)

SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _read(tmp_path: Path, path: Path, n: int, **tool_input) -> dict:
    payload = {
        "hookEventName": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": str(path), **tool_input},
        "tool_use_id": f"toolu_{n}",
        "session_id": SESSION,
        "agent_id": SESSION,
    }
    out = _run_read_cache(tmp_path, ["--quiet"], payload)
    assert out.returncode == 0, out.stderr
    if not out.stdout.strip():
        return {}
    return json.loads(out.stdout.strip()).get("hookSpecificOutput", {})


def _bump(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    time.sleep(0.01)
    now = time.time_ns()
    os.utime(str(path), ns=(now, now))


def _make(tmp_path: Path) -> tuple[Path, list[str]]:
    lines = []
    for i in range(60):
        lines += [f"def func_{i}(a, b):\n", f"    value_{i} = a + b + {i}\n", f"    return value_{i}\n", "\n"]
    f = tmp_path / "mod.py"
    f.write_text("".join(lines), encoding="utf-8")
    return f, lines


def test_reread_after_delta_is_allowed(tmp_path):
    f, lines = _make(tmp_path)
    assert _read(tmp_path, f, 1).get("permissionDecision") != "deny"

    lines[101] = "    value_25 = 'changed by sed'\n"
    _bump(f, "".join(lines))

    delta = _read(tmp_path, f, 2)
    assert delta.get("permissionDecision") == "deny"
    assert "changed by sed" in delta.get("additionalContext", "")

    # The host refused the Edit and the agent reads again: this must reach the host.
    again = _read(tmp_path, f, 3)
    assert again.get("permissionDecision") != "deny", again
    # Every later unchanged re-read may be substituted again.
    third = _read(tmp_path, f, 4)
    assert "unchanged" in json.dumps(third) or third.get("permissionDecision") == "deny"


def test_ranged_reread_after_delta_is_allowed(tmp_path):
    f, lines = _make(tmp_path)
    _read(tmp_path, f, 1)
    lines[5] = "    value_1 = 'x'\n"
    _bump(f, "".join(lines))
    assert _read(tmp_path, f, 2).get("permissionDecision") == "deny"
    again = _read(tmp_path, f, 3, offset=1, limit=20)
    assert again.get("permissionDecision") != "deny", again


def test_second_external_change_gets_a_fresh_delta(tmp_path):
    f, lines = _make(tmp_path)
    _read(tmp_path, f, 1)
    lines[1] = "    value_0 = 'a'\n"
    _bump(f, "".join(lines))
    assert _read(tmp_path, f, 2).get("permissionDecision") == "deny"
    assert _read(tmp_path, f, 3).get("permissionDecision") != "deny"
    lines[2] = "    return 'b'\n"
    _bump(f, "".join(lines))
    again = _read(tmp_path, f, 4)
    assert again.get("permissionDecision") == "deny"
    assert "return 'b'" in again.get("additionalContext", "")


def test_duplicate_hook_call_for_same_tool_use_does_not_consume_marker(tmp_path):
    f, lines = _make(tmp_path)
    _read(tmp_path, f, 1)
    lines[9] = "    value_2 = 'z'\n"
    _bump(f, "".join(lines))
    assert _read(tmp_path, f, 2).get("permissionDecision") == "deny"
    first = _read(tmp_path, f, 3)
    assert first.get("permissionDecision") != "deny"
    # A repeated hook invocation of the same tool use is still just an allow.
    assert _read(tmp_path, f, 3).get("permissionDecision") != "deny"


def test_edit_after_delta_clears_marker(tmp_path):
    f, lines = _make(tmp_path)
    _read(tmp_path, f, 1)
    lines[9] = "    value_2 = 'q'\n"
    _bump(f, "".join(lines))
    assert _read(tmp_path, f, 2).get("permissionDecision") == "deny"
    lines[9] = "    value_2 = 'edited'\n"
    _bump(f, "".join(lines))
    inv = {
        "hookEventName": "PostToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(f)},
        "session_id": SESSION,
        "agent_id": SESSION,
    }
    assert _run_read_cache(tmp_path, ["--invalidate", "--quiet"], inv).returncode == 0
    entry = _get_file_entry(tmp_path, SESSION, str(f))
    assert entry["last_replacement_type"] != "delta_served"
