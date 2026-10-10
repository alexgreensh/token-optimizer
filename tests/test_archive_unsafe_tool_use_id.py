"""Unsafe tool_use_id values must still archive — under a safe filename.

The ``^[a-zA-Z0-9_-]+$`` whitelist silently skipped archiving for ids with
``+``, space, ``/``, ``.`` or ``'`` — the full result passed through to context
uncompressed and nothing was retrievable later. Unsafe ids now map to a
deterministic digest filename: archiving always happens, traversal is
impossible, and the printed expand hint uses the safe key so recovery works.

Run: python3 -m pytest tests/test_archive_unsafe_tool_use_id.py -q
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

BODY = "payload\n" * 1000  # > _ARCHIVE_THRESHOLD
SID = "11111111-1111-1111-1111-111111111111"


def _env(tmp_path):
    env = dict(os.environ)
    env.update({"HOME": str(tmp_path / "home"),
                "CLAUDE_CONFIG_DIR": str(tmp_path / "home" / ".claude"),
                "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(tmp_path / "snap"),
                "TOKEN_OPTIMIZER_RUNTIME": "claude"})
    return env


def _archive(tmp_path, tool_use_id, tool_name="mcp__demo__dump"):
    payload = {"tool_name": tool_name, "session_id": SID,
               "tool_use_id": tool_use_id, "tool_input": {},
               "tool_response": BODY}
    p = subprocess.run([sys.executable, str(SCRIPTS / "archive_result.py")],
                       input=json.dumps(payload), text=True, capture_output=True,
                       env=_env(tmp_path), timeout=60)
    assert p.returncode == 0, p.stderr
    return p


def _archive_dir(tmp_path):
    return tmp_path / "snap" / "tool-archive" / SID


@pytest.mark.parametrize("tid", ["tu_COMB+FLAG", "tu id space", "tu/slash",
                                 "tu..dots", "tu'*q", "..\\win\\path"])
def test_unsafe_tool_use_id_still_archives(tmp_path, tid):
    p = _archive(tmp_path, tid)
    arch = _archive_dir(tmp_path)
    entries = [f.name for f in arch.glob("*.json")]
    assert entries, f"{tid!r}: nothing archived"
    assert all(re.fullmatch(r"[a-zA-Z0-9_-]+\.json", n) for n in entries)
    # The result is retrievable through the printed hint key.
    replacement = json.loads(p.stdout)["hookSpecificOutput"]["updatedMCPToolOutput"]
    m = re.search(r"expand ([a-zA-Z0-9_-]+)", replacement)
    assert m, replacement
    key = m.group(1)
    assert (arch / f"{key}.json").is_file()
    data = json.loads((arch / f"{key}.json").read_text())
    assert data["response"] != ""


@pytest.mark.parametrize("tid", ["../escape", "..\\escape", "/abs", "a/b/../../x"])
def test_no_path_can_escape_the_archive_dir(tmp_path, tid):
    _archive(tmp_path, tid)
    # Nothing may be written outside the session archive dir.
    for p in (tmp_path / "snap" / "tool-archive").rglob("*"):
        assert p.is_file() or p.is_dir()
        assert SID in str(p.relative_to(tmp_path / "snap" / "tool-archive")) or \
            p.name in (".locks", SID), p


def test_safe_id_is_unchanged(tmp_path):
    _archive(tmp_path, "tuid0001")
    assert (_archive_dir(tmp_path) / "tuid0001.json").is_file()


def test_same_unsafe_id_maps_to_the_same_key(tmp_path):
    """Deterministic: re-archiving one unsafe id replaces, never duplicates."""
    _archive(tmp_path, "tu id space")
    first = {f.name for f in _archive_dir(tmp_path).glob("*.json")}
    _archive(tmp_path, "tu id space")
    second = {f.name for f in _archive_dir(tmp_path).glob("*.json")}
    assert first == second


def test_expand_recovers_hashed_entry(tmp_path):
    p = _archive(tmp_path, "tu/slash")
    replacement = json.loads(p.stdout)["hookSpecificOutput"]["updatedMCPToolOutput"]
    key = re.search(r"expand ([a-zA-Z0-9_-]+)", replacement).group(1)
    argv = [sys.executable, str(SCRIPTS / "measure.py"), "expand", key]
    result = subprocess.run(argv, env=_env(tmp_path), capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert BODY.splitlines()[0] in result.stdout
