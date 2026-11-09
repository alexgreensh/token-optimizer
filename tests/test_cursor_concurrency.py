#!/usr/bin/env python3
"""G2: real multi-process hook-shape concurrency contracts.

Every Cursor hook event is its own OS process. Single-process tests cannot
see the failure classes this file pins:

1. P1 (executed repro R-T7 in review): the session-tally lease must not
   suppress distinct-PID acquirers after a clean release. Three sequential
   hook processes for one conversation must each persist their update.
2. P2: with suppression gone, concurrent postToolUse processes race the
   compact restore - the claim pops restore_pending inside ONE locked RMW,
   so exactly one process emits the restore.

Each subprocess is a real interpreter with TOKEN_OPTIMIZER_CURSOR_HOME
pointed at tmp_path, the same env shim production uses.

Run: python3 -m pytest tests/test_cursor_concurrency.py -v
"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

_CHILD = """
import sys
sys.path.insert(0, {scripts!r})
import cursor_hook_bridge as b
b.handle_post_tool_use({{
    "hook_event_name": "postToolUse",
    "conversation_id": {sid!r},
    "tool_name": "Read",
    "tool_input": {{"file_path": {fp!r}}},
}})
"""


def _run_hook(home: Path, sid: str, fp: str = "/tmp/some-file.py") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # runtime_env only honors TOKEN_OPTIMIZER_CURSOR_HOME under $HOME.
    env["HOME"] = str(home.parent)
    env["USERPROFILE"] = str(home.parent)
    env["TOKEN_OPTIMIZER_CURSOR_HOME"] = str(home)
    env["PYTHONPATH"] = str(SCRIPTS)
    return subprocess.run(
        [sys.executable, "-c", _CHILD.format(scripts=str(SCRIPTS), sid=sid, fp=fp)],
        env=env, capture_output=True, text=True, timeout=60,
    )


def _tally(home: Path, sid: str) -> dict:
    return json.loads((home / "token-optimizer" / "sessions" / f"{sid}.json").read_text())


def test_sequential_distinct_pid_updates_all_persist(tmp_path):
    """G2 P1: 3 sequential hook processes each persist their tool call."""
    home = tmp_path / ".cursor"
    home.mkdir()
    sid = "conv-mp-seq"
    for i in range(3):
        proc = _run_hook(home, sid, fp=f"/tmp/file-{i}.py")
        assert proc.returncode == 0, proc.stderr[-500:]
    tally = _tally(home, sid)
    assert tally["tool_calls"] == 3, (
        f"distinct-PID hook updates were suppressed: tool_calls={tally['tool_calls']}"
    )


def test_concurrent_post_tool_use_emits_exactly_one_restore(tmp_path):
    """G2 P2: the atomic claim gives the restore to exactly one process."""
    home = tmp_path / ".cursor"
    sid = "conv-mp-restore"
    to_dir = home / "token-optimizer" / "sessions"
    to_dir.mkdir(parents=True)
    cp_dir = home / "token-optimizer" / "snapshot" / "checkpoints"
    cp_dir.mkdir(parents=True)
    checkpoint = cp_dir / "cp.md"
    checkpoint.write_text("decision: rerun the migration\n" * 10, encoding="utf-8")
    (to_dir / f"{sid}.json").write_text(json.dumps({
        "conversation_id": sid,
        "tool_calls": 5,
        "restore_pending": True,
        "compact_checkpoint": str(checkpoint),
    }), encoding="utf-8")

    with ThreadPoolExecutor(max_workers=4) as pool:
        procs = list(pool.map(lambda _: _run_hook(home, sid), range(4)))
    for proc in procs:
        assert proc.returncode == 0, proc.stderr[-500:]
    emitters = [p for p in procs if "checkpoint captured before compaction" in p.stdout]
    assert len(emitters) == 1, (
        f"restore emitted by {len(emitters)} processes (want exactly 1)"
    )
    assert _tally(home, sid).get("restore_pending") is False
