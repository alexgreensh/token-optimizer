"""The terminal status line reads the shared savings cache the Python side writes.

measure.py writes the 30-day savings to status-bar/global.json (one file for all
sessions). statusline.js used to read only status-bar/<session>.json, so after an
upgrade the headline froze, and never appeared for a new session. These tests
let the real Python code write the files, then run statusline.js on them.

Run: python3 -m pytest tests/test_statusline_savings_global.py -q
"""
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

import pytest

from test_status_bar_command import SCRIPTS, SID_A, SID_B, _day, _sandbox_env, _seed

SL = SCRIPTS / "statusline.js"
SID_NEW = "cccccccc-1111-2222-3333-444444444444"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


@pytest.fixture()
def sb(tmp_path, monkeypatch):
    env, snap, claude = _sandbox_env(tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    m = importlib.import_module("measure")
    m._sb_env, m._sb_snap = env, snap
    monkeypatch.setattr(m, "spawn_detached", lambda argv, **kw: object())
    yield m
    sys.modules.pop("measure", None)


def _three_day_fixture(m):
    # Above the 1M-token / $60 display thresholds, so the headline has a display.
    _seed(m, rows=[(_day(0), "tool_archive", 2_000_000, 75.0, SID_A),
                   (_day(1), "tool_archive", 500_000, 10.0, SID_B)])


def _run_statusline(m, session_id):
    payload = json.dumps({"session_id": session_id, "model": {"display_name": "Opus"},
                          "workspace": {"current_dir": str(m._sb_snap)}, "transcript_path": "",
                          "cost": {"total_duration_ms": 1000}})
    env = {**os.environ, **m._sb_env}
    env.pop("CLAUDE_PLUGIN_DATA", None)
    p = subprocess.run(["node", str(SL)], input=payload, env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stderr
    return re.sub(r"\x1b\][^\x07]*\x07|\x1b\[[0-9;]*m", "", p.stdout)


def _short(data):
    return data["savings"]["savings_headline"]["display"]["short"]


def test_new_session_shows_headline_from_shared_global_cache(sb):
    _three_day_fixture(sb)
    sb.status_bar_payload(SID_A, sync=True)
    status_dir = sb._sb_snap / "status-bar"
    data = json.loads((status_dir / "global.json").read_text(encoding="utf-8"))
    assert not (status_dir / (SID_A + ".json")).exists()
    short = _short(data)
    assert short
    # A session that has never refreshed reads the same shared number.
    assert short + " ↗" in _run_statusline(sb, SID_NEW)
    assert short + " ↗" in _run_statusline(sb, SID_A)


def test_headline_follows_newer_global_cache_over_legacy_session_file(sb):
    _three_day_fixture(sb)
    sb.status_bar_payload(SID_A, sync=True)
    status_dir = sb._sb_snap / "status-bar"
    current = json.loads((status_dir / "global.json").read_text(encoding="utf-8"))
    legacy = json.loads(json.dumps(current))
    legacy["computed_at"] = current["computed_at"] - 3600
    legacy["savings"]["savings_headline"]["display"]["short"] = "OLD-LEGACY-NUMBER"
    (status_dir / (SID_B + ".json")).write_text(json.dumps(legacy), encoding="utf-8")
    out = _run_statusline(sb, SID_B)
    assert _short(current) + " ↗" in out
    assert "OLD-LEGACY-NUMBER" not in out


def test_pending_file_written_when_global_is_locked_is_read(sb, monkeypatch):
    _three_day_fixture(sb)
    sb.status_bar_payload(SID_A, sync=True)
    status_dir = sb._sb_snap / "status-bar"
    stale = json.loads((status_dir / "global.json").read_text(encoding="utf-8"))
    stale["computed_at"] -= 3600
    stale["savings"]["savings_headline"]["display"]["short"] = "OLDER-GLOBAL"
    (status_dir / "global.json").write_text(json.dumps(stale), encoding="utf-8")
    from _measure import status_bar_refresh
    real_replace = os.replace

    def locked(src, dst):
        if str(dst).endswith("global.json"):
            raise PermissionError("locked")
        return real_replace(src, dst)

    monkeypatch.setattr(sb.os, "replace", locked)
    monkeypatch.setattr(status_bar_refresh.time, "sleep", lambda s: None)
    sb.status_bar_payload(SID_A, sync=True)
    monkeypatch.setattr(sb.os, "replace", real_replace)
    pending = list(status_dir.glob("global-pending-*.json"))
    assert len(pending) == 1
    fresh = json.loads(pending[0].read_text(encoding="utf-8"))
    out = _run_statusline(sb, SID_NEW)
    assert _short(fresh) + " ↗" in out
    assert "OLDER-GLOBAL" not in out


def test_pre_upgrade_session_file_still_shows_until_first_refresh(sb):
    _three_day_fixture(sb)
    sb.status_bar_payload(SID_A, sync=True)
    status_dir = sb._sb_snap / "status-bar"
    data = json.loads((status_dir / "global.json").read_text(encoding="utf-8"))
    (status_dir / "global.json").unlink()
    (status_dir / (SID_B + ".json")).write_text(json.dumps(data), encoding="utf-8")
    assert _short(data) + " ↗" in _run_statusline(sb, SID_B)


def test_statusline_never_spawns_python():
    code = re.sub(r"//.*", "", SL.read_text(encoding="utf-8"))
    assert "python" not in code.lower()
    calls = re.findall(r"(?<![.\w])(?:execFileSync|execSync|spawnSync|spawn|exec|execFile|fork)\(\s*([^,)]*)", code)
    assert calls == ["'git'"]
