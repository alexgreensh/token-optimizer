"""C8 regression guards.

1. The PostToolUse fast path may skip only a provable no-op. Edit-family tools
   always run context_intel (activity row) and read_cache --invalidate; Bash
   always runs context_intel. None of them may early-exit.
2. bash_compress_hook's "deliberate content read" guard is narrower than the
   large-output Condition 4 exclusion list: `git log | head -20` must stay
   eligible for ordinary compression while bare cat / jq / inline scripts pass.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import hook_fastpath as hf  # noqa: E402
import large_output_policy as lop  # noqa: E402


def test_fastpath_never_skips_tools_with_real_handlers(tmp_path):
    for payload in (
        {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/a.py"}, "session_id": "s"},
        {"tool_name": "Write", "tool_input": {"file_path": "/tmp/a.py"}, "session_id": "s"},
        {"tool_name": "Bash", "tool_input": {"command": "git status"},
         "tool_response": {"stdout": "M a.py\n", "stderr": "", "exit_code": 0}, "session_id": "s"},
    ):
        assert hf.can_early_exit_posttooluse(payload, snapshot_dir=tmp_path) is False


def test_fastpath_skips_only_a_throttle_only_call_with_no_tick_due(tmp_path):
    payload = {"tool_name": "TodoWrite", "session_id": "s"}
    assert hf.can_early_exit_posttooluse(payload, snapshot_dir=tmp_path, only_throttle_matches=True) is True
    failure = {**payload, "hook_event_name": "PostToolUseFailure"}
    assert hf.can_early_exit_posttooluse(failure, snapshot_dir=tmp_path, only_throttle_matches=True) is False


def test_runner_has_no_in_process_dispatch():
    run_py = (Path(__file__).resolve().parent.parent / "hooks" / "run.py").read_text(encoding="utf-8")
    assert "runpy.run_path" not in run_py, "handlers must run in a child interpreter (wire contract)"


def test_deliberate_read_guard_is_narrower_than_condition4():
    for cmd in ("git log | head -20", "du -sh * | sort -rh | head -20"):
        assert lop.is_deliberate_content_read(cmd) is False, cmd
        assert lop.is_condition4_excluded(cmd) is True, cmd
    for cmd in ("cat a.py", "cd repo && cat a.py b.py", "jq . f.json", 'python3 -c "print(1)"'):
        assert lop.is_deliberate_content_read(cmd) is True, cmd
        assert lop.is_condition4_excluded(cmd) is True, cmd
