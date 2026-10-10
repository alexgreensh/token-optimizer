"""Issue #215 follow-up: PowerShell matcher coverage, pinned exactly.

The Windows shell tool is named ``PowerShell`` and the matcher
``Bash|PowerShell`` covers it. The rule (from the issue brief): add the
matcher ONLY where the handler genuinely works on PowerShell tool input --
output-archiving/activity/intel handlers are tool-name-generic and qualify;
Bash-GRAMMAR handlers (command rewriting/compression, the PreToolUse Bash
hook) must never touch a PowerShell command line, and handlers that cannot
interpret the input must pass through untouched.

What this file pins:

* hooks.json: the consolidated PostToolUse union and PostToolUseFailure gain
  ``PowerShell``; the PreToolUse ``bash_hook.py`` entry stays ``Bash``-only.
* posttooluse_runner._MATCHER_*: archive_result/context_intel/quality-cache
  match ``PowerShell``; bash_compress and read_cache --invalidate do NOT.
* context_intel logs the ``command`` field for PowerShell (same input field
  name as Bash) -- it is activity text, never interpreted as Bash syntax.
* bash_compress_hook, even if it somehow received a PowerShell payload,
  passes through untouched (its own ``tool_name != "Bash"`` self-gate is the
  second line of defence after the matcher).

Run: python3 -m pytest tests/test_powershell_matchers.py -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
HOOKS_JSON = HOOKS / "hooks.json"
RUNNER = HOOKS / "posttooluse_runner.py"
BASH_COMPRESS_HOOK = SCRIPTS / "bash_compress_hook.py"

UNION_MATCHER = (
    "Bash|PowerShell|Read|Glob|Grep|Agent|Edit|Write|MultiEdit|NotebookEdit|mcp__.*"
)


def _load_runner(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    (tmp_path / "claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    spec = importlib.util.spec_from_file_location("ptu_runner_ps", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_context_intel(monkeypatch, tmp_path):
    for name in ("context_intel", "session_store", "plugin_env", "activity_tracker"):
        sys.modules.pop(name, None)
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "context_intel_ps", SCRIPTS / "context_intel.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# hooks.json shape
# --------------------------------------------------------------------------- #


def test_posttooluse_union_matcher_covers_powershell():
    cfg = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    (entry,) = cfg["hooks"]["PostToolUse"]
    assert entry["matcher"] == UNION_MATCHER


def test_posttoolusefailure_matcher_is_bash_and_powershell():
    cfg = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    (entry,) = cfg["hooks"]["PostToolUseFailure"]
    assert entry["matcher"] == "Bash|PowerShell"


def test_pretooluse_bash_hook_stays_bash_only():
    """bash_hook.py advises on/rewrites the Bash command line -- PowerShell
    syntax is not valid input for it, so it must NOT gain the matcher."""
    cfg = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    for entry in cfg["hooks"]["PreToolUse"]:
        for hook in entry["hooks"]:
            if "bash_hook.py" in hook["command"]:
                assert entry["matcher"] == "Bash", (
                    "the PreToolUse command-rewrite hook must stay Bash-only; "
                    "a PowerShell command line is not valid Bash"
                )
                return
    raise AssertionError("no PreToolUse entry dispatches bash_hook.py")


# --------------------------------------------------------------------------- #
# runner-level matcher gates
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "matcher_attr,expected",
    [
        ("_MATCHER_BASH_COMPRESS", False),
        ("_MATCHER_ARCHIVE", True),
        ("_MATCHER_CONTEXT_INTEL", True),
        ("_MATCHER_READ_CACHE_INVALIDATE", False),
    ],
)
def test_runner_matchers_gate_powershell(matcher_attr, expected, monkeypatch, tmp_path):
    runner = _load_runner(monkeypatch, tmp_path)
    pattern = getattr(runner, matcher_attr)
    assert runner._matches(pattern, "PowerShell") is expected, (
        f"{matcher_attr} must {'admit' if expected else 'exclude'} PowerShell"
    )


def test_runner_union_admits_powershell(monkeypatch, tmp_path):
    runner = _load_runner(monkeypatch, tmp_path)
    assert runner._matches(runner._MATCHER_UNION, "PowerShell") is True


# --------------------------------------------------------------------------- #
# context_intel: the `command` field is shared and safe to log
# --------------------------------------------------------------------------- #


def test_context_intel_logs_powershell_command(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    intel = _load_context_intel(monkeypatch, tmp_path)
    intel.read_stdin_hook_input = lambda *_a, **_kw: {
        "session_id": "ps-intel-" + uuid.uuid4().hex[:8],
        "tool_name": "PowerShell",
        "tool_use_id": "toolu_01PS",
        "tool_input": {"command": "Get-ChildItem C:\\src"},
        "tool_response": "ok\n",
    }
    calls = []
    import activity_tracker

    monkeypatch.setattr(
        activity_tracker,
        "log_tool_use",
        lambda store, tool_name, command="", has_error=False: calls.append(
            {"tool_name": tool_name, "command": command, "has_error": has_error}
        ),
    )
    intel.handle_post_tool_use()
    assert calls == [
        {"tool_name": "PowerShell", "command": "Get-ChildItem C:\\src",
         "has_error": False}
    ]


# --------------------------------------------------------------------------- #
# bash_compress_hook: untouched passthrough for PowerShell
# --------------------------------------------------------------------------- #


def test_bash_compress_hook_passes_powershell_through(monkeypatch, tmp_path):
    """Even if a PowerShell payload ever reaches the script (matcher widening,
    a host that ignores matchers), its self-gate must pass it through
    untouched: exit 0, no stdout document, no mutation."""
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    payload = json.dumps({
        "session_id": "ps-passthru-" + uuid.uuid4().hex[:8],
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/Users/test/project",
        "hook_event_name": "PostToolUse",
        "tool_name": "PowerShell",
        "tool_input": {"command": "Get-ChildItem C:\\src"},
        "tool_response": {
            "stdout": "file1\nfile2\n",
            "stderr": "",
            "interrupted": False,
        },
        "tool_use_id": "toolu_01PSPT",
    })
    env = dict(os.environ)
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(tmp_path / "data")
    proc = subprocess.run(
        [sys.executable, str(BASH_COMPRESS_HOOK), "--quiet"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", (
        "a PowerShell payload must produce no updatedToolOutput -- the "
        "compressor rewrites Bash command-line output only"
    )
