"""U7 — Antigravity readiness doctor.

The doctor must emit per-check ok/warn/fail statuses with fix hints and must
never execute an untrusted binary. These tests drive `antigravity_doctor` with
`antigravity_home` monkeypatched to a scratch dir, so no real ``~/.gemini`` is
touched and no real ``agy`` is ever run.
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


# --- tiny protobuf encoders (test-only, mirrors test_antigravity_state.py) --


def _varint(value):
    out = bytearray()
    value &= (1 << 64) - 1
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _fv(num, value):
    return _varint((num << 3) | 0) + _varint(value)


def _fb(num, payload):
    return _varint((num << 3) | 2) + _varint(len(payload)) + payload


def _usage(inp, out, cache_read=0, thinking=0, response=None):
    m = b""
    if inp:
        m += _fv(2, inp)
    if out:
        m += _fv(3, out)
    if cache_read:
        m += _fv(5, cache_read)
    if thinking:
        m += _fv(9, thinking)
    if response is None:
        response = out - thinking
    if response:
        m += _fv(10, response)
    return m


def _gen_blob(inp, out, cache_read=0, thinking=0, model="Gemini 3.5 Flash (Medium)",
              est_used=0, max_ctx=0):
    gen = _fb(4, _usage(inp, out, cache_read, thinking))
    if model:
        gen += _fb(21, model.encode())
    if est_used or max_ctx:
        cw = b""
        if est_used:
            cw += _fv(1, est_used)
        if max_ctx:
            cw += _fv(4, max_ctx)
        gen += _fb(9, _fb(10, cw))
    return _fb(1, gen)


def _make_conversation_db(db_path, blobs=()):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB, size INTEGER)")
    for i, blob in enumerate(blobs):
        con.execute("INSERT INTO gen_metadata (idx, data, size) VALUES (?,?,?)", (i, blob, len(blob)))
    con.commit()
    con.close()


def _make_summaries_db(db_path, conversation_id="a"):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE conversation_summaries "
                "(conversation_id TEXT, title TEXT, killed numeric, not_fully_idle numeric)")
    con.execute("INSERT INTO conversation_summaries VALUES (?, 'My Title', 0, 0)", (conversation_id,))
    con.commit()
    con.close()


@pytest.fixture()
def doctor(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    for mod in ("antigravity_doctor", "antigravity_install", "antigravity_state"):
        sys.modules.pop(mod, None)
    mod = importlib.import_module("antigravity_doctor")
    monkeypatch.setattr(mod, "antigravity_home", lambda: tmp_path)
    return mod


def _install(doctor, tmp_path):
    importlib.import_module("antigravity_install").install(home=tmp_path)


def _fake_version_run(monkeypatch, doctor, version="agy 1.1.23"):
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/agy")

    class _R:
        stdout = version + "\n"
        stderr = ""
        returncode = 0

    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: _R())
    return _R


# --------------------------------------------------------------------------- #
# complete install: every check ok (daemon is ok either way by contract)
# --------------------------------------------------------------------------- #

def test_complete_install_all_ok(doctor, monkeypatch, tmp_path):
    _install(doctor, tmp_path)
    _make_conversation_db(
        tmp_path / "antigravity-cli" / "conversations" / "a.db",
        blobs=[_gen_blob(10, 10, cache_read=5, thinking=2)],
    )
    _make_summaries_db(tmp_path / "antigravity-cli" / "conversation_summaries.db")
    _fake_version_run(monkeypatch, doctor)

    checks = doctor.run_checks()
    statuses = {c["status"] for c in checks}
    assert "fail" not in statuses, f"unexpected failures: {[c for c in checks if c['status'] == 'fail']}"
    names = {c["name"] for c in checks}
    assert any("conversation store" in n for n in names)
    # binary and consent both report ok on a complete install.
    by_name = {c["name"]: c for c in checks}
    assert by_name["agy binary"]["status"] == "ok"
    assert by_name["consent record"]["status"] == "ok"
    assert by_name["dashboard daemon"]["status"] == "ok"


# --------------------------------------------------------------------------- #
# plugin directory missing / payload missing
# --------------------------------------------------------------------------- #

def test_plugin_dir_missing_warns(doctor, tmp_path):
    checks = doctor.run_checks()
    hook = next(c for c in checks if c["name"] == "plugin directory")
    assert hook["status"] == "warn"
    assert "antigravity-install" in hook["hint"]


def test_missing_payload_module_warns(doctor, monkeypatch, tmp_path):
    _install(doctor, tmp_path)
    pdir = tmp_path / "config" / "plugins" / "token-optimizer"
    (pdir / "bash_compress.py").unlink()
    checks = doctor.run_checks()
    payload = next(c for c in checks if c["name"] == "plugin payload")
    assert payload["status"] == "warn"
    assert "bash_compress.py" in payload["detail"]


# --------------------------------------------------------------------------- #
# plugin enabled = false
# --------------------------------------------------------------------------- #

def test_plugin_disabled_fails_with_enable_hint(doctor, tmp_path):
    _install(doctor, tmp_path)
    conf = tmp_path / "config" / "config.json"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(json.dumps({"plugins": {"token-optimizer": {"enabled": False}}}))
    checks = doctor.run_checks()
    enabled = next(c for c in checks if c["name"] == "plugin enabled")
    assert enabled["status"] == "fail"
    assert "agy plugin enable token-optimizer" in enabled["hint"]


def test_plugin_enabled_default_when_config_absent(doctor, tmp_path):
    _install(doctor, tmp_path)
    checks = doctor.run_checks()
    enabled = next(c for c in checks if c["name"] == "plugin enabled")
    assert enabled["status"] == "ok"


# --------------------------------------------------------------------------- #
# binary trust gate
# --------------------------------------------------------------------------- #

def test_binary_relative_override_fails_no_execution(doctor, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_ANTIGRAVITY_BIN", "agy")
    ran = []
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: ran.append(a))
    checks = doctor.run_checks()
    assert ran == []
    binary = next(c for c in checks if c["name"] == "agy binary")
    assert binary["status"] == "fail"
    assert "absolute path" in binary["hint"]


def test_binary_world_writable_override_fails_no_execution(doctor, monkeypatch, tmp_path):
    if not hasattr(os, "geteuid"):
        pytest.skip("POSIX trust test")
    exe = tmp_path / "agy"
    exe.write_text("#!/bin/sh\n")
    os.chmod(exe, 0o777)
    monkeypatch.setenv("TOKEN_OPTIMIZER_ANTIGRAVITY_BIN", str(exe))
    ran = []
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: ran.append(a))
    checks = doctor.run_checks()
    assert ran == []
    binary = next(c for c in checks if c["name"] == "agy binary")
    assert binary["status"] == "fail"
    assert "trust" in binary["hint"].lower()


def test_binary_absent_from_path_fails_with_install_hint(doctor, monkeypatch):
    monkeypatch.delenv("TOKEN_OPTIMIZER_ANTIGRAVITY_BIN", raising=False)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    checks = doctor.run_checks()
    binary = next(c for c in checks if c["name"] == "agy binary")
    assert binary["status"] == "fail"
    assert "antigravity.google" in binary["hint"]


# --------------------------------------------------------------------------- #
# decoder health
# --------------------------------------------------------------------------- #

def test_newest_db_undecodable_warns_with_decoder_version(doctor, tmp_path):
    _install(doctor, tmp_path)
    _make_conversation_db(
        tmp_path / "antigravity-cli" / "conversations" / "bad.db",
        blobs=[b"\xff\xff\x01"],
    )
    checks = doctor.run_checks()
    store = next(c for c in checks if c["name"] == "conversation store (antigravity-cli)")
    assert store["status"] == "warn"
    assert "undecodable" in store["detail"]
    assert "ag-v1" in store["hint"]


def test_newest_db_decodable_is_ok(doctor, tmp_path):
    _install(doctor, tmp_path)
    _make_conversation_db(
        tmp_path / "antigravity-cli" / "conversations" / "good.db",
        blobs=[_gen_blob(10, 10)],
    )
    checks = doctor.run_checks()
    store = next(c for c in checks if c["name"] == "conversation store (antigravity-cli)")
    assert store["status"] == "ok"
    assert "1 decodable" in store["detail"]


# --------------------------------------------------------------------------- #
# JSON output
# --------------------------------------------------------------------------- #

def test_json_output_parses_and_carries_status(doctor, monkeypatch, tmp_path, capsys):
    _install(doctor, tmp_path)
    _make_conversation_db(
        tmp_path / "antigravity-cli" / "conversations" / "good.db",
        blobs=[_gen_blob(10, 10)],
    )
    _fake_version_run(monkeypatch, doctor)
    rc = doctor.main(["--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list) and data
    for item in data:
        assert item["status"] in {"ok", "warn", "fail"}
        assert item["name"] and item["detail"]


def test_dashboard_toggle_lookup_matches_doctor_check_name(doctor, monkeypatch, tmp_path):
    """The dashboard toggle panel looks its status up by the doctor's check
    name; a typo'd lookup means the toggle shows not-installed forever."""
    import measure

    _install(doctor, tmp_path)
    checks = doctor.run_checks()
    names = {c["name"] for c in checks}
    # measure's collector imports antigravity_doctor locally, resolving to
    # this same module object, so patching here covers both.
    monkeypatch.setattr(doctor, "run_checks", lambda: [
        {**c, "status": "ok"} for c in checks
    ])
    panel = measure._collect_antigravity_hook_status_for_dashboard()
    toggle = panel["antigravity_dashboard_port"]
    # The lookup name must be a name the doctor actually emits.
    assert "dashboard daemon" in names
    assert toggle["installed"] is True


def test_bridge_selftest_ok_after_install(doctor, tmp_path):
    """The self-test must pass on a healthy install: exit 0, valid JSON."""
    _install(doctor, tmp_path)
    check = doctor._bridge_selftest_check()
    assert check["status"] == "ok", check["detail"]


def test_bridge_selftest_fails_on_broken_payload(doctor, tmp_path, monkeypatch):
    """A stale payload (bridge crashes on start) must surface as fail, not ok."""
    _install(doctor, tmp_path)
    bridge = tmp_path / "config" / "plugins" / "token-optimizer" / "antigravity_hook_bridge.py"
    bridge.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    check = doctor._bridge_selftest_check()
    assert check["status"] == "fail"
