"""Statusline must find the quality-cache the HOOKS wrote, not just its own dir.

The hooks write quality-cache under _STATE_BASE = ${CLAUDE_PLUGIN_DATA}/token-
optimizer (~/.claude/plugins/data/{id}/...) in the desktop plugin hook context.
The statusline runs WITHOUT CLAUDE_PLUGIN_DATA and used to read only
~/.claude/token-optimizer, so ContextQ/Eff showed "--" for every desktop plugin
user even though a valid score existed under plugins/data. The read now searches
plugin-data dirs too. This test builds that exact layout and asserts the score
renders (not "--").

Run: python3 -m pytest tests/test_statusline_quality_cache_dir.py -v
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SL = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts" / "statusline.js"
SID = "11111111-2222-3333-4444-555555555555"


def _has_node():
    return shutil.which("node") is not None


def _run(home: Path, extra_env=None):
    payload = json.dumps({
        "session_id": SID,
        "model": {"display_name": "Opus"},
        "workspace": {"current_dir": str(home)},
        "transcript_path": "",
        "cost": {"total_duration_ms": 1000},
    })
    # Node's os.homedir() reads $HOME on POSIX but %USERPROFILE% on Windows, so
    # override both (plus HOMEDRIVE/HOMEPATH) or the Windows runner ignores HOME
    # and reads the real profile dir -> no cache -> false failure.
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    drive, _, tail = str(home).partition(os.sep)
    if os.name == "nt" and ":" in drive:
        env["HOMEDRIVE"] = drive
        env["HOMEPATH"] = os.sep + tail
    env.pop("CLAUDE_PLUGIN_DATA", None)
    # A real CLAUDE_CONFIG_DIR would outrank the pinned HOME (#198).
    env.pop("CLAUDE_CONFIG_DIR", None)
    if extra_env:
        env.update(extra_env)
    p = subprocess.run(["node", str(SL)], input=payload, env=env,
                       capture_output=True, text=True, timeout=60)
    # strip ANSI
    return re.sub(r"\x1b\[[0-9;]*m", "", p.stdout)


def _write_cache(dir_: Path):
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / f"quality-cache-{SID}.json").write_text(json.dumps({
        "score": 72.0, "grade": "B", "resource_health": 72.0,
        "resource_health_grade": "B", "session_efficiency": 81.0,
        "session_efficiency_grade": "A", "session_file": f"proj-{SID}.jsonl",
    }))


@pytest.mark.skipif(not _has_node(), reason="node not available")
def test_reads_quality_cache_from_plugin_data_dir(tmp_path):
    """Cache ONLY in the plugin-data dir (hook context) -> statusline must find it."""
    home = tmp_path
    plugin_data = home / ".claude" / "plugins" / "data" / "token-optimizer-alexgreensh-token-optimizer" / "token-optimizer"
    _write_cache(plugin_data)
    out = _run(home)
    assert "ContextQ:--" not in out, f"score not read from plugin-data dir:\n{out}"
    assert re.search(r"ContextQ:[~]?[A-F]\(\d+\)", out), f"no ContextQ score:\n{out}"
    assert re.search(r"Eff:[A-F]\(\d+\)", out), f"no Eff score:\n{out}"


@pytest.mark.skipif(not _has_node(), reason="node not available")
def test_still_reads_from_runtime_fallback_dir(tmp_path):
    """Cache in the classic ~/.claude/token-optimizer dir still works (non-hook)."""
    home = tmp_path
    _write_cache(home / ".claude" / "token-optimizer")
    out = _run(home)
    assert re.search(r"ContextQ:[~]?[A-F]\(\d+\)", out), f"fallback dir not read:\n{out}"


@pytest.mark.skipif(not _has_node(), reason="node not available")
def test_no_cache_anywhere_degrades_to_placeholder(tmp_path):
    """No cache at all -> the honest '--' placeholder, never a crash."""
    out = _run(tmp_path)
    assert "ContextQ:--" in out, f"expected -- placeholder when no cache:\n{out}"


@pytest.mark.skipif(not _has_node(), reason="node not available")
@pytest.mark.parametrize("status", ["needs_attention", "no_concerning_signal_observed", "unmeasurable", "observing", None])
def test_recovery_flags_always_render_as_observing(tmp_path, status):
    cache_dir = tmp_path / ".claude" / "token-optimizer"
    _write_cache(cache_dir)
    path = cache_dir / f"quality-cache-{SID}.json"
    cache = json.loads(path.read_text())
    cache.update(score_version=2, compactions=3, recovery_status=status)
    path.write_text(json.dumps(cache))
    out = _run(tmp_path)
    assert "Compacts:3(observing)" in out
    assert "needs attention" not in out and "recovery ok" not in out
