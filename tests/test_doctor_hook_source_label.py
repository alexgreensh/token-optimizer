#!/usr/bin/env python3
"""doctor names where the SessionEnd hook really comes from.

It printed "SessionEnd hook: active (settings.json)" for a marketplace plugin
install whose hook lives only in the plugin's hooks.json: the check it used
also counts the plugin cache. Driven through the `measure.py doctor` CLI in a
sandboxed HOME.

Run: python3 -m pytest tests/test_doctor_hook_source_label.py -q
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MEASURE = REPO / "skills" / "token-optimizer" / "scripts" / "measure.py"


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    cfg = home / ".claude" / "token-optimizer" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"v5_welcome_shown": True, "enterprise_consent_shown": True}), encoding="utf-8")
    return home


def _doctor_session_end_line(home: Path, cwd: Path) -> str:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE_", "TOKEN_OPTIMIZER_", "CODEX_", "OPENCODE_", "HERMES_"))
           or k == "TOKEN_OPTIMIZER_COLD_COMPILE_WAIT_S"}
    env.update({"HOME": str(home), "USERPROFILE": str(home), "TOKEN_OPTIMIZER_NO_PROC_SCAN": "1",
                "XDG_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_DATA_HOME": str(home / ".local" / "share"), "XDG_STATE_HOME": str(home / ".local" / "state")})
    proc = subprocess.run([sys.executable, str(MEASURE), "doctor"], env=env, cwd=str(cwd),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode in (0, 1), proc.stderr[-2000:]
    m = re.search(r"SessionEnd hook:?\s+(.*)", proc.stdout)
    assert m, proc.stdout
    return m.group(1)


def test_plugin_hooks_json_is_not_called_settings_json(tmp_path):
    home = _home(tmp_path)
    cache = home / ".claude" / "plugins" / "cache" / "alexgreensh-token-optimizer" / "token-optimizer" / "5.13.51" / "hooks"
    cache.mkdir(parents=True)
    (cache / "hooks.json").write_text((REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"), encoding="utf-8")
    line = _doctor_session_end_line(home, tmp_path)
    assert line.startswith("active (plugin hooks.json)"), line


def test_a_settings_json_hook_is_still_called_settings_json(tmp_path):
    home = _home(tmp_path)
    settings = {"hooks": {"SessionEnd": [{"hooks": [
        {"type": "command", "command": f"python3 '{MEASURE}' session-end-flush --trigger end"}]}]}}
    (home / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    line = _doctor_session_end_line(home, tmp_path)
    assert line.startswith("active (settings.json)"), line
