"""Regression guards for the concurrency/performance hardening pass.

Each test pins one fix so a future edit that reintroduces the defect fails here:

- Dashboard HTML is written atomically (temp + os.replace), so three
  uncoordinated regen writers cannot leave a torn/mixed file.
- The transcript preload skips oversized session files, so a 100MB+ JSONL can't
  turn a cold dashboard gen into a timeout that also kills the flush worker.
- The daemon reject-throttle dict is bounded, so a flood of distinct bad-token
  paths cannot grow it without limit (memory-leak / DoS in network mode).
- The daemon log sanitizer strips ESC and every control byte, not just CR/LF.
- The MCP state writer uses a unique temp name, so two concurrent measure runs
  cannot clobber a shared ".tmp".
- The hook disable-check fails open on an oversized config file rather than
  reading it unboundedly on every hook event.
"""
from helpers.measure_split import measure_source as _split_measure_source

import os
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _generated_src() -> str:
    import measure

    return measure._generate_daemon_script()


def _extract(src, names, extra_ns=None):
    ns = {"time": time, "os": os, "_STATE_LOCK": threading.Lock()}
    ns.update(extra_ns or {})
    for const in ("_REJECT_LOG_LAST_TS", "_REJECT_LOG_MIN_GAP", "_REJECT_LOG_MAX_KEYS", "LOG_CAP_BYTES"):
        m = re.search(r"^%s(?::[^=]+)? = .*$" % re.escape(const), src, re.M)
        if m:
            exec(m.group(0), ns)
    for fn in names:
        m = re.search(r"^def %s\(.*?\n(?=^\S)" % re.escape(fn), src, re.M | re.S)
        assert m, f"{fn} missing from generated daemon"
        exec(m.group(0), ns)
    return ns


# --- atomic dashboard write ------------------------------------------

def test_dashboard_write_is_atomic():
    """The dashboard write loop must use a temp file + os.replace, never a bare
    O_TRUNC write onto the live path (which lets two regens interleave)."""
    src = _split_measure_source(SCRIPTS / "measure.py")
    m = re.search(r"for wp in write_paths:(.*?)\n    if not wrote_any:", src, re.S)
    assert m, "dashboard write loop not found"
    body = m.group(1)
    assert "tempfile.mkstemp(" in body, "dashboard write is not using a temp file"
    assert "os.replace(" in body, "dashboard write is not atomically replaced"
    # the truncating open must target the temp fd, never the live path directly
    assert "os.open(str(wp)" not in body, "dashboard still opens the live path with O_TRUNC (non-atomic)"
    # the temp-cleanup must catch BaseException, not just OSError: this write runs
    # under the flush worker's hook budget, which raises _HookTimeout (a
    # BaseException). A bare `except OSError` would leak the temp on timeout.
    assert "except BaseException" in body, "temp cleanup must catch BaseException so a _HookTimeout mid-write doesn't leak the temp"


# --- bounded transcript preload --------------------------------------

def test_transcript_preload_has_size_guard():
    src = _split_measure_source(SCRIPTS / "measure.py")
    assert "_turn_preload_maxbytes" in src, "preload size cap variable missing"
    assert "TOKEN_OPTIMIZER_TURN_PRELOAD_MAX_BYTES" in src, "preload size env override missing"
    # the guard must gate the parse call
    m = re.search(r"os\.path\.getsize\(jsonl_resolved\) > _turn_preload_maxbytes", src)
    assert m, "preload does not skip oversized transcripts before parse_session_turns"


# --- bounded reject-throttle dict ------------------------------------

def test_reject_throttle_dict_is_bounded():
    src = _generated_src()
    ns = _extract(src, ["_cap_log", "_sanitize_log_path", "_log_reject_regen"])
    import tempfile

    ns["REGEN_LOG"] = tempfile.mktemp()
    cap = ns["_REJECT_LOG_MAX_KEYS"]
    d = ns["_REJECT_LOG_LAST_TS"]
    for i in range(cap + 300):
        ns["_log_reject_regen"]("/api/p%d" % i)
    assert len(d) <= cap, f"reject dict grew past cap: {len(d)} > {cap}"


# --- full control-char stripping in the log sanitizer -----------------

def test_sanitize_log_path_strips_esc_and_control():
    src = _generated_src()
    ns = _extract(src, ["_sanitize_log_path"])
    san = ns["_sanitize_log_path"]
    assert san("a\r\nb") == "ab"
    # a bare ESC + ANSI clear-screen must not survive into the log
    dirty = "/api/x\x1b[2J\x1b[1;1Hgone"
    out = san(dirty)
    assert "\x1b" not in out
    assert out == "/api/x[2J[1;1Hgone"
    # other control bytes gone too; printable ASCII preserved
    assert san("/ok/PATH_09-az~") == "/ok/PATH_09-az~"
    assert san("\x00\x07\x1f\x7f") == ""


# --- unique temp for MCP state write ----------------------------------

def test_mcp_state_write_uses_unique_temp(tmp_path):
    import detectors.cache_instability as ci

    state_path = tmp_path / "mcp_state.json"
    ci._save_mcp_state(state_path, {"a": 1})
    import json

    assert json.loads(state_path.read_text()) == {"a": 1}
    # the old fixed-name sibling must NOT be how it writes (no leftover, and the
    # source must use mkstemp so concurrent cwds get distinct temps)
    assert not (tmp_path / "mcp_state.json.tmp").exists(), "fixed .tmp sibling left behind"
    src = (SCRIPTS / "detectors" / "cache_instability.py").read_text(encoding="utf-8")
    m = re.search(r"def _save_mcp_state\(.*?\n(?=^\S)", src, re.M | re.S)
    assert "tempfile.mkstemp(" in m.group(0), "MCP state write not using a unique temp name"


# --- hook disable-check fails open on oversized config ----------------

def test_hook_disable_check_fails_open_on_huge_settings(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "hooks"))
    import importlib

    run = importlib.import_module("run")
    # a plugin root with valid meta but a giant settings.json
    meta = tmp_path / ".claude-plugin"
    meta.mkdir()
    (meta / "plugin.json").write_text('{"name": "token-optimizer"}', encoding="utf-8")
    (meta / "marketplace.json").write_text('{"name": "mkt"}', encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    settings = tmp_path / "settings.json"
    settings.write_text("{" + '"x":"' + ("a" * 5_000_000) + '"}', encoding="utf-8")
    monkeypatch.setattr(run, "_claude_settings_path", lambda: settings)
    # oversized settings -> can't determine disabled -> fail open (not disabled)
    assert run._plugin_disabled_by_host() is False
