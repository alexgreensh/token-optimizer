"""The expand command a Codex archive prints must work from Codex's own shell.

Codex runs our hooks with TOKEN_OPTIMIZER_RUNTIME=codex (codex_install puts it
on the hook command), so archives land under the Codex home. The model's shell
gets no such marker, so a bare `measure.py expand <id>` copied from the pointer
resolved the Claude Code runtime and answered "No archive directory found".
The pointer now pins `--runtime codex`, like Hermes, and the pinned form is
still recognised as a recovery command so an expand is never re-archived.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

SID = "sess-codex-expand"
TOOL_USE_ID = "call_codexexpand01"
BODY = "row of tool output that is long enough to archive\n" * 200


def _env(home: Path, **extra) -> dict:
    keep = ("PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP", "LANG")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONIOENCODING="utf-8", **extra)
    return env


def _run(script: Path, args, env, stdin=None):
    return subprocess.run([sys.executable, str(script), *args], input=stdin, env=env,
                          capture_output=True, text=True, timeout=180)


@pytest.fixture
def archived(tmp_path):
    """Archive one MCP result exactly as the Codex PostToolUse hook does."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    payload = {"tool_name": "mcp__demo__dump", "session_id": SID,
               "tool_use_id": TOOL_USE_ID, "tool_input": {},
               "tool_response": BODY, "hook_event_name": "PostToolUse"}
    hook = _run(SCRIPTS / "archive_result.py", [], _env(home, TOKEN_OPTIMIZER_RUNTIME="codex"),
                stdin=json.dumps(payload))
    assert hook.returncode == 0, hook.stderr
    assert list((home / ".codex").rglob(f"{TOOL_USE_ID}.json")), "archive not under the Codex home"
    text = json.loads(hook.stdout)["hookSpecificOutput"]["updatedMCPToolOutput"]
    pointer = next(line.strip() for line in text.splitlines()
                   if re.search(rf"measure\.py.* expand {TOOL_USE_ID}$", line.strip()))
    return home, pointer


def test_codex_pointer_pins_the_runtime(archived):
    _, pointer = archived
    assert f" --runtime codex expand {TOOL_USE_ID}" in pointer, pointer


def test_codex_pointer_retrieves_from_a_bare_codex_shell(archived):
    """Run the printed command the way the model's Codex shell runs it: no
    TOKEN_OPTIMIZER_RUNTIME, no CODEX_HOME, nothing but the pointer's argv."""
    home, pointer = archived
    argv = pointer.split()
    assert Path(argv[1]).name == "measure.py"
    out = _run(Path(argv[1]), argv[2:], _env(home))
    assert out.returncode == 0, out.stderr
    assert out.stdout.rstrip("\n") == BODY.rstrip("\n")


def test_bare_expand_from_codex_shell_misses(archived):
    """Documents why the pin is needed: without it the shell resolves Claude Code."""
    home, _ = archived
    out = _run(SCRIPTS / "measure.py", ["expand", TOOL_USE_ID], _env(home))
    assert out.returncode != 0 or BODY.strip() not in out.stdout


def test_pinned_codex_pointer_is_a_recovery_command(archived):
    home, pointer = archived
    sys.path.insert(0, str(SCRIPTS))
    import recovery_output

    assert recovery_output.is_expand_command(pointer)
    assert recovery_output.is_recovery_output("Bash", {"command": pointer})
    # Running the pinned expand through the Codex hook again must not re-archive it.
    payload = {"tool_name": "Bash", "session_id": SID, "tool_use_id": "call_expandrun",
               "tool_input": {"command": pointer}, "tool_response": BODY,
               "hook_event_name": "PostToolUse"}
    hook = _run(SCRIPTS / "archive_result.py", [], _env(home, TOKEN_OPTIMIZER_RUNTIME="codex"),
                stdin=json.dumps(payload))
    assert hook.returncode == 0, hook.stderr
    assert not list((home / ".codex").rglob("call_expandrun.json"))
