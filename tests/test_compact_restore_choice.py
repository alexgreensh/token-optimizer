#!/usr/bin/env python3
"""After a compaction, the restore brings back the work from BEFORE it.

SessionStart(source=compact) runs quality-cache before compact-restore. With the
post-compact transcript that refresh can capture a fresh `progressive-20`
checkpoint whose "Active Task" is the compaction summary. Restore therefore
prefers the LATEST VALID save (gap-checkpoint) and keeps the PreCompact
eligibility anchor so post-compact captures cannot restore the summary back
as the task.

Every step is the real hooks.json command (PreCompact compact-capture,
quality-cache, SessionStart) in a sandboxed HOME; the expected strings are
hand-checked against the transcript _hook_harness.transcript() writes.

Run: python3 -m pytest tests/test_compact_restore_choice.py -q
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _hook_harness import (  # noqa: E402
    REPO, SESSION_ID, consented_home, hook_command, payload, run_hook, transcript)

posix_only = pytest.mark.skipif(os.name == "nt", reason="drives the hooks.json command through /bin/sh, which native Windows lacks")

TASK = "Please refactor the parser in app.py and add tests."
SUMMARY = "This session is being continued from a previous conversation that ran out of context. Summary: the user is working on app.py."


def _session(tmp_path):
    home = consented_home(tmp_path)
    # A fixed 200K window so the fills below are hand-checkable: the harness
    # transcript's last request reads 65,200 tokens (32.6%); the post-compact one 46,000 (23%).
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"TOKEN_OPTIMIZER_CONTEXT_SIZE": "200000"}}), encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()
    return home, project, transcript(project, home), tmp_path / "raises.jsonl"


def _compact(tpath: Path, project: Path) -> None:
    """What Claude Code appends when it compacts: the boundary, the summary as the
    new first user message, then a first request over the much smaller context."""
    rows = [
        {"type": "system", "subtype": "compact_boundary", "sessionId": SESSION_ID, "cwd": str(project),
         "timestamp": "2026-10-19T09:05:00.000Z", "uuid": "b1", "content": "Conversation compacted",
         "compactMetadata": {"trigger": "auto", "preTokens": 190000}},
        {"type": "user", "isCompactSummary": True, "sessionId": SESSION_ID, "cwd": str(project),
         "timestamp": "2026-10-19T09:05:01.000Z", "uuid": "s1", "parentUuid": "b1",
         "message": {"role": "user", "content": SUMMARY}},
        {"type": "assistant", "sessionId": SESSION_ID, "cwd": str(project), "timestamp": "2026-10-19T09:05:02.000Z",
         "uuid": "a9", "parentUuid": "s1",
         "message": {"id": "msg_9", "model": "claude-opus-4-6", "role": "assistant",
                     "usage": {"input_tokens": 1000, "cache_creation_input_tokens": 45000,
                               "cache_read_input_tokens": 0, "output_tokens": 50},
                     "content": [{"type": "text", "text": "Continuing."}]}},
    ]
    with tpath.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _precompact(home, project, tpath, log) -> Path:
    proc = run_hook(hook_command("PreCompact", "compact-capture --trigger auto"), REPO,
                    payload("PreCompact", None, tpath, project), home, log)
    assert proc.returncode == 0, proc.stderr[-2000:]
    ckpt_dir = home / ".claude" / "token-optimizer" / "checkpoints"
    autos = sorted(ckpt_dir.glob(f"{SESSION_ID}-*-??????.md"))
    assert autos, sorted(p.name for p in ckpt_dir.glob("*"))
    return autos[-1]


def _restore(home, project, tpath, log) -> str:
    pl = payload("SessionStart", None, tpath, project)
    pl["source"] = "compact"
    proc = run_hook(hook_command("SessionStart"), REPO, pl, home, log)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


def _active_task(ctx: str) -> str:
    lines = ctx.splitlines()
    heading = "## Current direction" if "## Current direction" in lines else "## Active Task"
    i = lines.index(heading)
    return lines[i + 1].removeprefix("- ").strip()


@posix_only
def test_compact_restore_brings_back_the_task_from_before_the_compaction(tmp_path):
    home, project, tpath, log = _session(tmp_path)
    _precompact(home, project, tpath, log)
    _compact(tpath, project)
    ctx = _restore(home, project, tpath, log)

    assert "Post-compact recovery (auto checkpoint)" in ctx, ctx[:800]
    assert _active_task(ctx) == TASK, ctx[:800]
    assert "Decided to split the parser into tokenize() and parse()" in ctx


@posix_only
def test_the_latest_valid_checkpoint_wins_over_an_older_richer_one(tmp_path):
    """gap-checkpoint: restore prefers the NEWEST save that can actually
    restore state. The PreCompact `auto` capture postdates the mid-session
    progressive-20 capture, so the newer (correct) state wins even though the
    older one was captured at a "richer" fill band."""
    home, project, tpath, log = _session(tmp_path)
    # A mid-session refresh (the quality-cache command hooks.json runs) at 32.6% fill
    # captures band 20 from the full transcript.
    proc = run_hook(hook_command("PostCompact", "quality-cache --force"), REPO,
                    payload("UserPromptSubmit", None, tpath, project), home, log)
    assert proc.returncode == 0, proc.stderr[-2000:]
    ckpt_dir = home / ".claude" / "token-optimizer" / "checkpoints"
    assert list(ckpt_dir.glob(f"{SESSION_ID}-*-progressive-20.md")), sorted(p.name for p in ckpt_dir.glob("*"))
    time.sleep(1.1)  # distinct mtimes: the PreCompact capture comes after it
    _precompact(home, project, tpath, log)
    _compact(tpath, project)
    ctx = _restore(home, project, tpath, log)

    assert "Post-compact recovery (auto checkpoint)" in ctx, ctx[:800]
    assert _active_task(ctx) == TASK, ctx[:800]


@posix_only
def test_an_old_precompact_capture_does_not_hide_later_saves(tmp_path):
    """A PreCompact capture older than a compaction can take belongs to an earlier
    compaction (this one's capture failed, #253): it does not fence off the saves
    made since, so the newest valid progressive save wins over the stale auto."""
    home, project, tpath, log = _session(tmp_path)
    auto = _precompact(home, project, tpath, log)
    hours_ago = time.time() - 2 * 3600
    for p in (auto, auto.with_suffix(".json")):
        if p.exists():
            os.utime(p, (hours_ago, hours_ago))
    _compact(tpath, project)
    ctx = _restore(home, project, tpath, log)

    assert "Post-compact recovery (progressive-20 checkpoint)" in ctx, ctx[:800]
