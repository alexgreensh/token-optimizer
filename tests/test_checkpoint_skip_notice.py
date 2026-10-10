"""A broken custom redaction file used to stop checkpoints with no word to the user.

Only ``doctor`` said so. The Stop/PreCompact/SessionEnd checkpoint hook now
tells the user ONCE per session, as a single ``systemMessage`` line (the same
envelope the archive/read-cache/bash hooks already use), and stays silent on
every later hook in that session and when redaction is healthy.

Run: python3 -m pytest tests/test_checkpoint_skip_notice.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MEASURE = REPO / "skills" / "token-optimizer" / "scripts" / "measure.py"

SID_A = "aaaaaaaa-1111-2222-3333-444444444444"
SID_B = "bbbbbbbb-1111-2222-3333-444444444444"


def _transcript(home: Path) -> Path:
    recs = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "please refactor the parser module"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "done, parser refactored"}]}},
    ]
    p = home / ".claude" / "projects" / "-w" / "s.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")
    return p


def _run(tmp_path: Path, sid: str, patterns_file: Path | None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_DATA", "TOKEN_OPTIMIZER_REDACT_PATTERNS_FILE", "CLAUDE_CONFIG_DIR")}
    env.update({"HOME": str(home), "USERPROFILE": str(home),
                "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(tmp_path / "snap")})
    if patterns_file is not None:
        env["TOKEN_OPTIMIZER_REDACT_PATTERNS_FILE"] = str(patterns_file)
    payload = {"session_id": sid, "transcript_path": str(_transcript(home)), "cwd": str(tmp_path)}
    res = subprocess.run([sys.executable, str(MEASURE), "compact-capture", "--trigger", "stop", "--quiet"],
                         input=json.dumps(payload), capture_output=True, text=True, timeout=120, env=env)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip(), home


def _broken(tmp_path: Path) -> Path:
    p = tmp_path / "bad-patterns.json"
    p.write_text("{ this is not json", encoding="utf-8")
    return p


def _checkpoints(home: Path):
    return list((home / ".claude").rglob("checkpoints/*.md"))


def test_broken_pattern_file_says_so_once_per_session(tmp_path):
    bad = _broken(tmp_path)
    out1, home = _run(tmp_path, SID_A, bad)
    msg = json.loads(out1)  # one JSON object: the hook's own envelope
    assert list(msg) == ["systemMessage"]
    assert "[Token Optimizer]" in msg["systemMessage"]
    assert "\n" not in msg["systemMessage"], "one line"
    assert "redaction" in msg["systemMessage"].lower() and "checkpoint" in msg["systemMessage"].lower()
    assert str(bad) in msg["systemMessage"], "name the file the user has to fix"
    assert not [p for p in _checkpoints(home) if SID_A in p.name], "fail closed: still no checkpoint"

    out2, _ = _run(tmp_path, SID_A, bad)
    assert out2 == "", "a second hook in the same session adds no noise"


def test_a_new_session_is_told_again(tmp_path):
    bad = _broken(tmp_path)
    assert _run(tmp_path, SID_A, bad)[0] != ""
    assert _run(tmp_path, SID_B, bad)[0] != ""


def test_healthy_redaction_is_silent_and_writes_the_checkpoint(tmp_path):
    out, home = _run(tmp_path, SID_A, None)
    assert out == ""
    assert [p for p in _checkpoints(home) if SID_A in p.name], "healthy path still checkpoints"


def test_valid_custom_file_is_silent(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"patterns": [{"regex": "MEDX-[0-9]{6}", "label": "medx id"}]}), encoding="utf-8")
    out, home = _run(tmp_path, SID_A, good)
    assert out == ""
    assert [p for p in _checkpoints(home) if SID_A in p.name]
