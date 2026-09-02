"""Wiring: the session-end flush worker emits only when an admin-enabled
fleet.json is present, and the local dashboard shows the telemetry banner.

Run: python3 -m pytest tests/test_fleet_emitter_wiring.py -v
"""
import importlib
import json
import os
import socket
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
HASH_KEY = "c" * 43


@pytest.fixture()
def m(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("TOKEN_OPTIMIZER_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "snap").mkdir()
    (tmp_path / "config").mkdir()
    for var in ("TO_FLEET_ENDPOINT", "TO_FLEET_TOKEN", "TO_FLEET_DISABLE",
                "TO_FLEET_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "CLAUDE_DIR", tmp_path / "claude")
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _seed_trends(m, sid="cafe0000-1111-2222-3333-444455556666"):
    conn = sqlite3.connect(str(m.TRENDS_DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS session_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, jsonl_path TEXT, date TEXT, project TEXT,
        duration_minutes REAL, input_tokens INTEGER, output_tokens INTEGER,
        message_count INTEGER, api_calls INTEGER, cache_create_1h_tokens INTEGER DEFAULT 0,
        cache_create_5m_tokens INTEGER DEFAULT 0, model_usage_json TEXT,
        model_usage_breakdown_json TEXT, collected_at TEXT, quality_score REAL,
        quality_grade TEXT, session_uuid TEXT, is_sidechain INTEGER DEFAULT 0,
        reported_input_tokens INTEGER, reported_output_tokens INTEGER, platform TEXT,
        cost_usd REAL, cost_source TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS counted_reread (
        event_key TEXT PRIMARY KEY, source TEXT, session_uuid TEXT, event_ts TEXT,
        event_month TEXT, tokens INTEGER DEFAULT 0, oneshot_usd REAL DEFAULT 0,
        reread_tokens INTEGER DEFAULT 0, reread_usd REAL DEFAULT 0,
        turns_counted INTEGER DEFAULT 0, transcript_mtime REAL, computed_at TEXT)""")
    conn.execute(
        "INSERT INTO session_log (jsonl_path, date, project, session_uuid, platform,"
        " cost_usd, cost_source) VALUES ('p/x.jsonl', '2026-09-01', 'proj', ?,"
        " 'claude', 1.5, 'copilot_credits')", (sid,))
    conn.execute(
        "INSERT INTO counted_reread (event_key, session_uuid, tokens, oneshot_usd,"
        " reread_usd, computed_at) VALUES ('se:1', ?, 100, 0.5, 0.25,"
        " '2026-09-01T10:00:00')", (sid,))
    conn.commit()
    conn.close()


def _enable(m, endpoint):
    (m.CONFIG_DIR / "fleet.json").write_text(json.dumps(
        {"endpoint": endpoint, "token": "tok", "hash_key": HASH_KEY}))
    os.chmod(str(m.CONFIG_DIR / "fleet.json"), 0o600)


def test_worker_without_config_makes_no_network(m, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network attempted while disabled")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    m._run_session_end_flush_worker([])
    snap = m.SNAPSHOT_DIR
    assert not (snap / "fleet-outbox.jsonl").exists()
    assert not (snap / "fleet-cursor.json").exists()
    assert not (snap / "fleet.log").exists()


def test_worker_posts_when_enabled(m):
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            captured["body"] = json.loads(body)
            captured["auth"] = self.headers.get("Authorization")
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _seed_trends(m)
        _enable(m, f"http://127.0.0.1:{srv.server_port}")
        m._run_session_end_flush_worker([])
        assert captured["auth"] == "Bearer tok"
        assert captured["body"]["schema"] == "to.fleet.v1"
        assert captured["body"]["events"][0]["session_uuid"].startswith("cafe")
        assert (m.SNAPSHOT_DIR / "fleet-cursor.json").exists()
        assert json.loads((m.SNAPSHOT_DIR / "fleet-cursor.json").read_text())["last_id"] == 1
    finally:
        srv.shutdown()


def test_worker_fail_open_when_collector_down(m):
    _seed_trends(m)
    _enable(m, "http://127.0.0.1:1")  # closed port
    m._run_session_end_flush_worker([])  # must not raise
    assert (m.SNAPSHOT_DIR / "fleet-outbox.jsonl").exists()
    assert (m.SNAPSHOT_DIR / "fleet.log").exists()
    outbox = (m.SNAPSHOT_DIR / "fleet-outbox.jsonl").read_text()
    assert "cafe0000" in outbox


def test_worker_survives_emitter_crash_and_releases_lock(m, monkeypatch):
    import fleet_emitter
    def boom(*a, **k):
        raise RuntimeError("emitter exploded")
    monkeypatch.setattr(fleet_emitter, "emit_after_flush", boom)
    _enable(m, "http://127.0.0.1:1")
    m._run_session_end_flush_worker([])
    assert m._acquire_session_end_flush_lock() is not None  # lock was released
    m._release_session_end_flush_lock(m._acquire_session_end_flush_lock())


def test_dashboard_banner_data(m):
    _enable(m, "http://127.0.0.1:8787")
    # load_config through measure's CONFIG_DIR
    import fleet_emitter
    cfg = fleet_emitter.load_config(config_dir=str(m.CONFIG_DIR))
    assert cfg and cfg["endpoint"] == "http://127.0.0.1:8787"
    template = (SCRIPTS.parent / "assets" / "dashboard.html").read_text()
    assert 'id="fleet-telemetry-banner"' in template
    assert "window.__TOKEN_DATA__ = null;" in template


def test_fleet_billing_mode_rungs(m, monkeypatch, tmp_path):
    # Rung 1: env api key -> api
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert m.fleet_billing_mode() == "api"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    # Rung 4: nothing detectable -> unknown (never subscription)
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    assert m.fleet_billing_mode(claude_json_path=empty, settings_path=empty) == "unknown"
    # Rung 2: oauth account marker -> subscription
    oauth = tmp_path / "claude.json"
    oauth.write_text(json.dumps({"oauthAccount": {"emailAddress": "x@y.z"}}))
    assert m.fleet_billing_mode(claude_json_path=oauth, settings_path=empty) == \
        "subscription"
