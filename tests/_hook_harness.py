"""Drive Token Optimizer hooks the way the host does, for outcome tests.

Every helper runs the EXACT hooks/hooks.json command string through /bin/sh
(-> python-launcher.sh -> run.py -> module_runner -> the script) with a
sandboxed HOME, a consented install and a realistic transcript, so a test
checks what a user gets, not what an inner function returns.

``run_hook`` also loads tests/fixtures/raise_monitor/sitecustomize.py, which
logs every exception raised inside the plugin tree (see test_swallowed_errors).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MONITOR_DIR = Path(__file__).resolve().parent / "fixtures" / "raise_monitor"
HOOKS_JSON = REPO / "hooks" / "hooks.json"
SESSION_ID = "0f6e3c1a-5b2d-4e8f-9a1b-246246246246"


def hook_entries():
    data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    out = []
    for event, groups in data["hooks"].items():
        for gi, group in enumerate(groups):
            for hi, hook in enumerate(group["hooks"]):
                if hook.get("type") != "command":
                    continue
                cmd = hook["command"]
                m = re.search(r'run\.py\\?"? ([^;]+?);', cmd)
                label = m.group(1).strip() if m else f"{gi}.{hi}"
                out.append(pytest.param(event, group.get("matcher"), cmd,
                                        id=f"{event}[{group.get('matcher') or '*'}]:{label}"))
    return out


def transcript(project: Path, home: Path) -> Path:
    """A short but realistic Claude Code transcript: usage, a tool round-trip,
    a file read, enough for quality scoring and checkpoint capture to run."""
    read_target = project / "app.py"
    read_target.write_text("def main():\n    return 42\n", encoding="utf-8")
    ts = "2026-10-19T09:00:%02d.000Z"
    usage = {"input_tokens": 1200, "cache_creation_input_tokens": 4000,
             "cache_read_input_tokens": 60000, "output_tokens": 350}
    recs = [
        {"type": "user", "sessionId": SESSION_ID, "cwd": str(project), "timestamp": ts % 0,
         "uuid": "u1", "message": {"role": "user", "content": "Please refactor the parser in app.py and add tests."}},
        {"type": "assistant", "sessionId": SESSION_ID, "cwd": str(project), "timestamp": ts % 1,
         "uuid": "a1", "parentUuid": "u1",
         "message": {"id": "msg_1", "model": "claude-opus-4-6", "role": "assistant", "usage": usage,
                     "content": [{"type": "text", "text": "Reading the file first."},
                                 {"type": "tool_use", "id": "toolu_01", "name": "Read",
                                  "input": {"file_path": str(read_target)}}]}},
        {"type": "user", "sessionId": SESSION_ID, "cwd": str(project), "timestamp": ts % 2,
         "uuid": "u2", "parentUuid": "a1",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_01",
                                                  "content": "1\tdef main():\n2\t    return 42\n"}]}},
        {"type": "assistant", "sessionId": SESSION_ID, "cwd": str(project), "timestamp": ts % 3,
         "uuid": "a2", "parentUuid": "u2",
         "message": {"id": "msg_2", "model": "claude-opus-4-6", "role": "assistant", "usage": usage,
                     "content": [{"type": "tool_use", "id": "toolu_02", "name": "Bash",
                                  "input": {"command": "git status"}}]}},
        {"type": "user", "sessionId": SESSION_ID, "cwd": str(project), "timestamp": ts % 4,
         "uuid": "u3", "parentUuid": "a2",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_02",
                                                  "content": "On branch main\nnothing to commit"}]}},
        {"type": "assistant", "sessionId": SESSION_ID, "cwd": str(project), "timestamp": ts % 5,
         "uuid": "a3", "parentUuid": "u3",
         "message": {"id": "msg_3", "model": "claude-opus-4-6", "role": "assistant", "usage": usage,
                     "content": [{"type": "text", "text": "Decided to split the parser into tokenize() and parse()."}]}},
    ]
    # Claude Code names a project dir by turning every non-alphanumeric of the path into "-"
    # (a Windows path has ":" and "\\", which no directory name can hold).
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(project))
    path = home / ".claude" / "projects" / slug / f"{SESSION_ID}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    return path


def payload(event: str, matcher: str | None, transcript: Path, project: Path) -> dict:
    base = {"session_id": SESSION_ID, "transcript_path": str(transcript), "cwd": str(project),
            "hook_event_name": event, "permission_mode": "default"}
    tool = (matcher or "").split("|")[0]
    if tool.startswith("mcp__"):
        tool = "mcp__github__get_file_contents"
    if event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        tool = tool or "Bash"
        inputs = {
            "Read": {"file_path": str(project / "app.py")},
            "Bash": {"command": "git log --oneline -5"},
            "Agent": {"description": "survey", "prompt": "Survey the parser", "subagent_type": "general-purpose"},
        }
        base["tool_name"] = tool
        base["tool_input"] = inputs.get(tool, {"owner": "a", "repo": "b", "path": "README.md"})
        base["tool_use_id"] = "toolu_09"
        if event == "PostToolUse":
            base["tool_response"] = {"stdout": "abc123 first\n" * 40, "stderr": "", "interrupted": False}
        if event == "PostToolUseFailure":
            base["error"] = "Exit code 1\nfatal: not a git repository"
    elif event == "UserPromptSubmit":
        base["prompt"] = "now add tests for tokenize()"
    elif event == "SessionStart":
        base["source"] = "startup"
        base["model"] = "claude-opus-4-6"
    elif event in ("PreCompact", "PostCompact"):
        base["trigger"] = "auto"
        base["custom_instructions"] = ""
    elif event == "SessionEnd":
        base["reason"] = "other"
    elif event == "StopFailure":
        base["error"] = "rate_limit"
    elif event == "CwdChanged":
        base["old_cwd"] = str(project)
        base["new_cwd"] = str(project)
    elif event == "Stop":
        base["stop_hook_active"] = False
    return base


def consented_home(root: Path) -> Path:
    home = root / "home"
    cfg = home / ".claude" / "token-optimizer" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"v5_welcome_shown": True, "enterprise_consent_shown": True}), encoding="utf-8")
    return home


def run_hook(command: str, plugin_root: Path, payload: dict, home: Path, log: Path,
             timeout: float = 90.0) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE_", "TOKEN_OPTIMIZER_", "CODEX_", "TO_TEST_"))
           or k in ("TOKEN_OPTIMIZER_COLD_COMPILE_WAIT_S", "TOKEN_OPTIMIZER_HOOK_BUDGET_MS")}
    env.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"), "XDG_STATE_HOME": str(home / ".local" / "state"),
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
        "CLAUDE_PROJECT_DIR": payload["cwd"],
        "TOKEN_OPTIMIZER_NO_PROC_SCAN": "1",
        "PYTHONPATH": str(MONITOR_DIR),
        "TO_TEST_RAISE_LOG": str(log),
        "TO_TEST_RAISE_ROOT": str(plugin_root),
    })
    return subprocess.run(["/bin/sh", "-c", command], input=json.dumps(payload), cwd=payload["cwd"],
                          env=env, capture_output=True, text=True, timeout=timeout)


def read_log(log: Path, settle: float = 0.0) -> list[dict]:
    """Records from every process. Async/detached workers may still be writing
    after the hook returns, so wait (up to ``settle`` s) until the log has not
    grown for one second."""
    deadline = time.monotonic() + settle
    last, stable_since = -1, time.monotonic()
    while time.monotonic() < deadline:
        size = log.stat().st_size if log.exists() else 0
        if size != last:
            last, stable_since = size, time.monotonic()
        elif time.monotonic() - stable_since >= 1.0:
            break
        time.sleep(0.2)
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def hook_command(event: str, needle: str = "") -> str:
    """The hooks.json command for ``event`` whose text contains ``needle``."""
    for p in hook_entries():
        if p.values[0] == event and needle in p.values[2]:
            return p.values[2]
    raise LookupError(f"no {event} hook containing {needle!r} in hooks.json")
