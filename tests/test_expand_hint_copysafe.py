"""The printed expand hint must be copy-safe, and a failed copy never archives.

The archive footer put the closing ``]`` on the same line as the expand command,
so a naive line-copy produced ``expand <id>]`` — an invalid id — and because
``is_expand_command`` did not recognise the bracketed form, the failed attempt's
output could be archived as a brand-new junk entry. The ``]`` now sits on its
own line, the recogniser tolerates the stale trailing bracket (old pointers are
still in live transcripts), and ``expand`` itself tolerates one stray ``]``.

Run: python3 -m pytest tests/test_expand_hint_copysafe.py -q
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

BODY = "line\n" * 3000  # > _ARCHIVE_THRESHOLD so the archive path engages


def _env(tmp_path):
    env = dict(os.environ)
    env.update({"HOME": str(tmp_path / "home"),
                "CLAUDE_CONFIG_DIR": str(tmp_path / "home" / ".claude"),
                "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(tmp_path / "snap"),
                "TOKEN_OPTIMIZER_RUNTIME": "claude"})
    return env


def _run_hook(tmp_path, script, payload):
    p = subprocess.run([sys.executable, str(SCRIPTS / script), "--quiet"],
                       input=json.dumps(payload), text=True, capture_output=True,
                       env=_env(tmp_path), timeout=30)
    assert p.returncode == 0, p.stderr
    return p.stdout


def test_pointer_bracket_is_on_its_own_line():
    from archive_result import build_archive_pointer
    pointer = build_archive_pointer("PREVIEW", 5000, "tuid0001")
    lines = pointer.splitlines()
    assert lines[-1] == "]", f"closing bracket must sit alone: {lines[-1]!r}"
    cmd_line = lines[-2].strip()
    assert cmd_line.endswith(" expand tuid0001"), cmd_line
    assert not cmd_line.endswith("]"), cmd_line


def test_mcp_replacement_hint_bracket_is_on_its_own_line(tmp_path):
    out = _run_hook(tmp_path, "archive_result.py", {
        "tool_name": "mcp__demo__dump", "session_id": "s1",
        "tool_use_id": "tuid0001", "tool_input": {}, "tool_response": BODY})
    replacement = json.loads(out)["hookSpecificOutput"]["updatedMCPToolOutput"]
    lines = replacement.splitlines()
    assert lines[-1] == "]"
    assert lines[-2].strip().endswith("expand tuid0001")


@pytest.mark.parametrize("suffix", ["]", " --session s1]"])
def test_stale_bracketed_hint_is_still_an_expand_command(suffix):
    """Old pointers in live transcripts still end with ']' — a Bash call made by
    copying one is a recovery attempt and must keep the recovery exemption."""
    from recovery_output import is_expand_command
    from refetch_fingerprint import expand_command
    assert is_expand_command(expand_command("tuid0001") + suffix)


@pytest.mark.parametrize("suffix", [";;", "]; echo another", "] | cat"])
def test_bracket_tolerance_does_not_admit_operators(suffix):
    from recovery_output import is_expand_command
    from refetch_fingerprint import expand_command
    assert not is_expand_command(expand_command("tuid0001") + suffix)


def test_expand_cli_strips_a_stray_bracket(tmp_path):
    """A verbatim copy of an old-format pointer still retrieves the entry."""
    _run_hook(tmp_path, "archive_result.py", {
        "tool_name": "mcp__demo__dump", "session_id": "s1",
        "tool_use_id": "tuid0001", "tool_input": {}, "tool_response": BODY})
    argv = [sys.executable, str(SCRIPTS / "measure.py"), "expand", "tuid0001]"]
    result = subprocess.run(argv, env=_env(tmp_path), capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert BODY.splitlines()[0] in result.stdout


def test_failed_bracketed_expand_is_never_archived(tmp_path):
    """Replaying a stale-bracket expand call through PostToolUse writes nothing."""
    import shlex
    command = (
        f"{shlex.quote(sys.executable)} "
        f"{shlex.quote(str(SCRIPTS / 'measure.py'))} expand tuid0001]"
    )
    out = _run_hook(tmp_path, "archive_result.py", {
        "tool_name": "Bash", "session_id": "s1", "tool_use_id": "tuid0002",
        "tool_input": {"command": command},
        "tool_response": {"stdout": BODY, "stderr": "", "exit_code": 1}})
    assert not out.strip()
    assert not list(tmp_path.rglob("manifest.jsonl"))
