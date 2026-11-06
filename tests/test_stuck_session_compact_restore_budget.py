#!/usr/bin/env python3
"""SessionStart `compact-restore` must be wall-clock budgeted.

Root cause of the ~36s first-prompt stall when multiple concurrent sessions share
one project dir: `compact-restore --new-session-only` was the ONLY synchronous
first-prompt hook with no internal HookDeadline budget -- just the harness 20s
timeout, 2.5x the 8s cap on every other hook -- so under disk/CPU contention it
dominated the stall. This wraps it in the same `_install_hook_budget(...)` guard
the other eight hooks already use (env-overridable via
TOKEN_OPTIMIZER_COMPACT_RESTORE_BUDGET). Its output is a best-effort re-orientation
hint, so a budget-exit degrades gracefully to no hint.

We do NOT unit-test the budget actually firing: HookDeadline's backstop is
`os._exit(0)`, which would terminate the pytest process itself. The firing
mechanism is shared with eight already-shipped hooks. Here we prove the wrapper
integrates cleanly (fast case + a tiny budget both exit 0, no traceback) and an
edit-time guard keeps the budget from silently regressing in both source trees.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MEASURE = REPO / "skills" / "token-optimizer" / "scripts" / "measure.py"
MEASURE_MIRROR = (
    REPO / "plugins" / "token-optimizer" / "skills" / "token-optimizer"
    / "scripts" / "measure.py"
)


def _run_compact_restore(tmp_path, env_extra=None):
    # A bare env has no HOME, so Python falls back to the real account home and
    # the child reads and writes the developer's own ~/.claude. Give it a temp one.
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(tmp_path / "snap"),
    }
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(MEASURE), "compact-restore", "--new-session-only"],
        input='{"session_id":"budget-smoketest","source":"startup"}',
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_compact_restore_exits_clean_default_budget(tmp_path):
    p = _run_compact_restore(tmp_path)
    assert p.returncode == 0, p.stderr
    assert "Traceback" not in p.stderr


def test_compact_restore_exits_clean_tiny_budget(tmp_path):
    # A 1s budget must not break the fast path (real op completes well under 1s).
    p = _run_compact_restore(tmp_path, {"TOKEN_OPTIMIZER_COMPACT_RESTORE_BUDGET": "1"})
    assert p.returncode == 0, p.stderr
    assert "Traceback" not in p.stderr


@pytest.mark.parametrize(
    "src", [MEASURE, MEASURE_MIRROR], ids=["canonical", "plugin-mirror"]
)
def test_compact_restore_dispatch_installs_a_budget(src):
    """Guard: the compact-restore CLI branch must install a hook budget, in both
    the canonical and the codex-mirror source trees."""
    from helpers.measure_split import measure_source
    text = measure_source(src)
    idx = text.find('args[0] == "compact-restore"')
    assert idx != -1, "compact-restore dispatch not found"
    # Bound to exactly the compact-restore branch: slice up to the NEXT dispatch
    # handler (`elif args[0]`), not a fixed byte window. The branch grew when the
    # Cowork additionalContext-wrap landed, so a magic offset would false-
    # fail; slicing to the real boundary keeps the guard precise regardless of
    # branch size and still catches a removed budget install/clear.
    nxt = text.find("elif args[0]", idx + 10)
    branch = text[idx:nxt] if nxt != -1 else text[idx: idx + 3600]
    assert "_install_hook_budget(" in branch, "compact-restore is not budgeted"
    assert "TOKEN_OPTIMIZER_COMPACT_RESTORE_BUDGET" in branch
    assert "_clear_hook_budget(" in branch
