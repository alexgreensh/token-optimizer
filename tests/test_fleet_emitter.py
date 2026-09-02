"""Fleet emitter (Teams edition): off by default, file-only enablement,
allowlisted payload, fail-open delivery, ack-advanced cursor.

Run: python3 -m pytest tests/test_fleet_emitter.py -v
"""
import importlib.util
import json
import os
import socket
import sqlite3
import stat
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
EMITTER = SCRIPTS / "fleet_emitter.py"
HASH_KEY = "a" * 43


def _load(name="fleet_emitter_test"):
    spec = importlib.util.spec_from_file_location(name, str(EMITTER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def fe(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    (tmp_path / "snap").mkdir()
    (tmp_path / "config").mkdir()
    for var in ("TO_FLEET_ENDPOINT", "TO_FLEET_TOKEN", "TO_FLEET_DISABLE",
                "TO_FLEET_CONFIG", "TO_FLEET_USER", "TO_FLEET_HASH_KEY"):
        monkeypatch.delenv(var, raising=False)
    mod = _load()
    yield mod
    _load("fleet_emitter_test_reload")  # fresh module globals for the next test


def _write_config(tmp_path, fe, mode=0o600, **fields):
    p = tmp_path / "config" / "fleet.json"
    body = {"endpoint": "http://127.0.0.1:8787", "token": "tok", "hash_key": HASH_KEY}
    body.update(fields)
    p.write_text(json.dumps(body))
    p.chmod(mode)
    return p


def _db(tmp_path, rows=(), counted=(), name="trends.db"):
    conn = sqlite3.connect(str(tmp_path / name))
    conn.execute("""CREATE TABLE session_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, jsonl_path TEXT, date TEXT, project TEXT,
        duration_minutes REAL, input_tokens INTEGER, output_tokens INTEGER,
        message_count INTEGER, api_calls INTEGER, cache_create_1h_tokens INTEGER DEFAULT 0,
        cache_create_5m_tokens INTEGER DEFAULT 0, model_usage_json TEXT,
        model_usage_breakdown_json TEXT, collected_at TEXT, quality_score REAL,
        quality_grade TEXT, session_uuid TEXT, is_sidechain INTEGER DEFAULT 0,
        reported_input_tokens INTEGER, reported_output_tokens INTEGER, platform TEXT,
        cost_usd REAL, cost_source TEXT)""")
    conn.execute("""CREATE TABLE counted_reread (
        event_key TEXT PRIMARY KEY, source TEXT, session_uuid TEXT, event_ts TEXT,
        event_month TEXT, tokens INTEGER DEFAULT 0, oneshot_usd REAL DEFAULT 0,
        reread_tokens INTEGER DEFAULT 0, reread_usd REAL DEFAULT 0,
        turns_counted INTEGER DEFAULT 0, transcript_mtime REAL, computed_at TEXT)""")
    for r in rows:
        conn.execute(
            "INSERT INTO session_log (jsonl_path, date, project, session_uuid, platform,"
            " cost_usd, cost_source, model_usage_breakdown_json, is_sidechain)"
            " VALUES (?,?,?,?,?,?,?,?,?)", r)
    for c in counted:
        conn.execute("INSERT INTO counted_reread (event_key, session_uuid, tokens,"
                     " oneshot_usd, reread_usd, computed_at) VALUES (?,?,?,?,?,?)", c)
    conn.commit()
    return conn


_ROW = ("p/x.jsonl", "2026-09-01", "proj", "cafe0000-1111-2222-3333-444455556666",
        "claude", 1.5, "copilot_credits", '{"claude-opus-5":{"fresh_input":10,"output":5,'
        '"cache_read":100,"cache_create":3}}', 0)


# --------------------------------------------------------------------------- #
# Enablement (R1, R2, R2a, R2b)
# --------------------------------------------------------------------------- #

def test_disabled_by_default(fe, tmp_path):
    assert fe.load_config(config_dir=str(tmp_path / "config")) is None
    assert fe.emit_after_flush(trends_db=str(tmp_path / "trends.db"),
                               snapshot_dir=str(tmp_path / "snap"),
                               config_dir=str(tmp_path / "config")) is None
    assert not (tmp_path / "snap" / "fleet-outbox.jsonl").exists()
    assert not (tmp_path / "snap" / "fleet-cursor.json").exists()


def test_env_alone_never_enables(fe, tmp_path, monkeypatch):
    monkeypatch.setenv("TO_FLEET_ENDPOINT", "http://127.0.0.1:8787")
    monkeypatch.setenv("TO_FLEET_TOKEN", "tok")
    assert fe.load_config(config_dir=str(tmp_path / "config")) is None


def test_disable_overrides_file(fe, tmp_path, monkeypatch):
    _write_config(tmp_path, fe)
    monkeypatch.setenv("TO_FLEET_DISABLE", "1")
    assert fe.load_config(config_dir=str(tmp_path / "config")) is None


def test_token_env_and_hash_key_env(fe, tmp_path, monkeypatch):
    _write_config(tmp_path, fe, token="", token_env="MY_TOK", hash_key="",
                  hash_key_env="MY_KEY")
    monkeypatch.setenv("MY_TOK", "tok")
    monkeypatch.setenv("MY_KEY", HASH_KEY)
    cfg = fe.load_config(config_dir=str(tmp_path / "config"))
    assert cfg and cfg["token"] == "tok" and cfg["hash_key"] == HASH_KEY


def test_incomplete_file_disabled(fe, tmp_path):
    _write_config(tmp_path, fe, token="")
    assert fe.load_config(config_dir=str(tmp_path / "config")) is None
    _write_config(tmp_path, fe, hash_key="")
    assert fe.load_config(config_dir=str(tmp_path / "config")) is None


def test_non_loopback_http_disabled(fe, tmp_path):
    _write_config(tmp_path, fe, endpoint="http://collector.example.com")
    assert fe.load_config(config_dir=str(tmp_path / "config")) is None


def test_loopback_http_and_https_enabled(fe, tmp_path):
    _write_config(tmp_path, fe, endpoint="http://127.0.0.1:8787")
    assert fe.load_config(config_dir=str(tmp_path / "config"))
    _write_config(tmp_path, fe, endpoint="https://x.example")
    assert fe.load_config(config_dir=str(tmp_path / "config"))


def test_group_readable_file_ignored(fe, tmp_path):
    _write_config(tmp_path, fe, mode=0o644)
    assert fe.load_config(config_dir=str(tmp_path / "config")) is None


def test_config_override_outside_dir_ignored(fe, tmp_path, monkeypatch):
    evil = tmp_path / "evil.json"
    evil.write_text(json.dumps({"endpoint": "http://127.0.0.1:9", "token": "t",
                                "hash_key": HASH_KEY}))
    evil.chmod(0o600)
    monkeypatch.setenv("TO_FLEET_CONFIG", str(evil))
    assert fe.load_config(config_dir=str(tmp_path / "config")) is None


def test_enable_writes_0600(fe, tmp_path):
    ok, where = fe.enable("http://127.0.0.1:8787", "tok", HASH_KEY,
                          config_dir=str(tmp_path / "config"))
    assert ok
    p = Path(where)
    assert p.stat().st_mode & 0o777 == 0o600
    assert fe.load_config(config_dir=str(tmp_path / "config"))


def test_no_socket_when_disabled(fe, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network attempted while disabled")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert fe.emit_after_flush(trends_db=str(tmp_path / "trends.db"),
                               snapshot_dir=str(tmp_path / "snap"),
                               config_dir=str(tmp_path / "config")) is None


# --------------------------------------------------------------------------- #
# Identity (R3, R3a, KTD7)
# --------------------------------------------------------------------------- #

def test_identity_precedence(fe, tmp_path, monkeypatch):
    env = {"TO_FLEET_USER": "env-user"}
    cfg = {"hash_key": HASH_KEY, "user": ""}
    assert fe.canonical_identity(cfg, env) == "env-user"
    cfg["user"] = "file-user"
    assert fe.canonical_identity(cfg, env) == "file-user"
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[core]\nx=1\n[user]\nemail=git@x.io\n")
    cfg["user"] = ""
    assert fe.canonical_identity(cfg, {}, home=home) == "git@x.io"
    assert len(fe.user_hash(cfg, env, home=home)) == 32


def test_hash_key_changes_identity(fe, monkeypatch):
    env = {}
    cfg = {"hash_key": HASH_KEY, "user": "u"}
    h1 = fe.user_hash(cfg, env)
    h2 = fe.user_hash({"hash_key": "b" * 43, "user": "u"}, env)
    assert h1 != h2


# --------------------------------------------------------------------------- #
# Payload allowlist + canary (R3, R4)
# --------------------------------------------------------------------------- #

def test_sanitize_strips_unknown_keys_and_canary(fe):
    cfg = {"hash_key": HASH_KEY}
    ev = {"type": "session", "session_uuid": "s1", "prompt": "CANARY-prompt",
          "topic": "CANARY-topic", "slug": "CANARY-slug", "cost_usd": 2.5}
    out = fe.sanitize_event(cfg, ev)
    assert "prompt" not in out and "topic" not in out and "slug" not in out
    assert set(out) <= fe.EVENT_KEYS | {"models_redacted"}
    assert out["cost_usd"] == 2.5


def test_sanitize_requires_type_and_uuid(fe):
    cfg = {"hash_key": HASH_KEY}
    assert fe.sanitize_event(cfg, {"type": "session"}) is None
    assert fe.sanitize_event(cfg, {"session_uuid": "s"}) is None


def test_model_name_redaction(fe):
    cfg = {"hash_key": HASH_KEY}
    out = fe.sanitize_event(cfg, {"type": "session", "session_uuid": "s",
                                  "models": {"anthropic/claude-opus-5": {"output": 3},
                                             "CANARY-model!": {"output": 1}}})
    assert "claude-opus-5" in out["models"]
    assert list(out["models"])[1].startswith("custom-")
    assert len(out["models"][list(out["models"])[1]]) == 4
    assert "CANARY" not in json.dumps(out)


def test_envelope_keys_frozen(fe):
    cfg = {"hash_key": HASH_KEY, "user": "u", "send_user_label": True}
    payload = fe.build_payload(cfg, [{"type": "session", "session_uuid": "s"}],
                               "5.13.4", env={},
                               meters={"available": True, "five_hour_pct": 10,
                                       "seven_day_pct": 20, "ts": 1},
                               billing_mode="api", savings_method="counted_reread")
    assert set(payload) <= fe.ENVELOPE_KEYS
    assert payload["limit"]["five_hour_pct"] == 10
    assert payload["user_label"] == "u"


def test_limit_omitted_when_meter_unavailable(fe):
    cfg = {"hash_key": HASH_KEY, "user": "u"}
    payload = fe.build_payload(cfg, [], "v", env={}, meters=None)
    assert "limit" not in payload
    assert "user_label" not in payload


def test_canary_never_reaches_post_bytes(fe, tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            captured["body"] = body.decode("utf-8", "replace")
            captured["auth"] = self.headers.get("Authorization")
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cfg = {"hash_key": HASH_KEY, "endpoint": f"http://127.0.0.1:{srv.server_port}",
               "token": "tok", "user": "u"}
        ev = {"type": "session", "session_uuid": "s1", "project_hash": "CANARY-proj",
              "models": {"m": {"output": 1}}, "quality_grade": "A"}
        ok, status, _ = fe.post_payload(cfg, [{"id": 1, "event": ev}], "v", env={})
        assert ok and status == 200
        assert captured["auth"] == "Bearer tok"
        assert "CANARY" not in captured["body"]
        payload = json.loads(captured["body"])
        assert payload["schema"] == "to.fleet.v1"
        assert set(payload["events"][0]) <= fe.EVENT_KEYS
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- #
# Event building (R4a-R4c)
# --------------------------------------------------------------------------- #

def test_session_uuid_fallbacks(fe, tmp_path):
    cfg = {"hash_key": HASH_KEY}
    assert fe.session_uuid_for(cfg, "cafe0000-1111-2222-3333-444455556666", None) == \
        "cafe0000-1111-2222-3333-444455556666"
    m = fe.session_uuid_for(cfg, None, "/x/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl")
    assert m == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    h = fe.session_uuid_for(cfg, None, "/x/opaque-path.jsonl")
    assert h.startswith("p-") and len(h) == 32
    assert fe.session_uuid_for(cfg, None, None) is None


def test_cost_precedence(fe, tmp_path):
    conn = _db(tmp_path, rows=[_ROW])
    cfg = {"hash_key": HASH_KEY}
    events, _mid, _w = fe.build_session_events(conn, cfg, runtime="claude")
    assert events[0]["cost_usd"] == 1.5
    assert events[0]["cost_source"] == "copilot_credits"
    # no stored cost -> breakdown cost_fn
    conn2 = _db(tmp_path, rows=[_ROW[:5] + (None, None) + _ROW[7:]], name="trends2.db")
    events2, _m, _w = fe.build_session_events(
        conn2, cfg, runtime="claude",
        cost_fn=lambda b, **k: 4.25)
    assert events2[0]["cost_usd"] == 4.25
    conn.close()
    conn2.close()


def test_savings_from_counted_ledger(fe, tmp_path):
    sid = "cafe0000-1111-2222-3333-444455556666"
    conn = _db(tmp_path, rows=[_ROW],
               counted=[("se:1", sid, 100, 0.5, 0.25, "2026-09-01T10:00:00")])
    cfg = {"hash_key": HASH_KEY}
    events, _mid, wm = fe.build_session_events(conn, cfg, runtime="claude")
    assert events[0]["savings"] == {"tokens": 100, "cost_usd": 0.75}
    assert events[0]["savings_coverage"] == "counted"
    assert wm == "2026-09-01T10:00:00"
    # non-Claude runtime: unsupported, zero savings
    events2, _m2, _w2 = fe.build_session_events(conn, cfg, runtime="hermes")
    assert events2[0]["savings_coverage"] == "unsupported_platform"
    assert events2[0]["savings"]["tokens"] == 0
    conn.close()


def test_sidechain_and_platform_unknown(fe, tmp_path):
    side = _ROW[:8] + (1,)
    noplat = _ROW[:4] + (None,) + _ROW[5:]
    conn = _db(tmp_path, rows=[side, noplat], name="trends3.db")
    cfg = {"hash_key": HASH_KEY}
    events, _m, _w = fe.build_session_events(conn, cfg, runtime="claude")
    assert len(events) == 1
    assert events[0]["platform"] == "unknown"
    conn.close()


def test_cursor_and_watermark_requeue(fe, tmp_path):
    sid = "cafe0000-1111-2222-3333-444455556666"
    conn = _db(tmp_path, rows=[_ROW],
               counted=[("se:1", sid, 100, 0.5, 0.25, "2026-09-01T10:00:00")])
    cfg = {"hash_key": HASH_KEY}
    _e, mid, wm = fe.build_session_events(conn, cfg, cursor_id=0, runtime="claude")
    # old row, past the 2-day window, but ledger moved past the watermark -> requeued
    old = _ROW[:3] + _ROW[3:]
    conn.execute("UPDATE session_log SET date='2026-08-01'")
    conn.execute("UPDATE counted_reread SET computed_at='2026-09-02T00:00:00'")
    conn.commit()
    events, _m, _w = fe.build_session_events(conn, cfg, cursor_id=mid, watermark=wm,
                                             runtime="claude",
                                             now=datetime(2026, 9, 2))
    assert any(e["session_uuid"] == sid for e in events)
    conn.close()


# --------------------------------------------------------------------------- #
# Outbox + cursor + drain (R6, R6a, R6c)
# --------------------------------------------------------------------------- #

def test_enqueue_dedup_and_caps(fe, tmp_path):
    snap = str(tmp_path / "snap")
    now = datetime.utcnow()
    old = (now - timedelta(days=8)).isoformat()
    fe.enqueue(snap, [{"type": "session", "session_uuid": "s1", "cost_usd": 1}], now=now)
    fe.enqueue(snap, [{"type": "session", "session_uuid": "s1", "cost_usd": 2}], now=now)
    items = fe.read_outbox(snap)
    assert len(items) == 1 and items[0]["event"]["cost_usd"] == 2
    rec = {"queued_at": old, "id": 1,
           "event": {"type": "session", "session_uuid": "old"}}
    fe.write_outbox(snap, items + [rec])
    fe.enqueue(snap, [], now=now)
    assert all(r["event"]["session_uuid"] != "old" for r in fe.read_outbox(snap))


def test_cursor_advances_only_on_ack(fe, tmp_path):
    snap = str(tmp_path / "snap")
    cfg = {"hash_key": HASH_KEY, "endpoint": "http://127.0.0.1:9", "token": "t"}
    events = [{"type": "session", "session_uuid": f"s{i}", "_src_id": i}
              for i in range(1, 4)]
    ids = {e["session_uuid"]: e.pop("_src_id") for e in events}
    fe.enqueue(snap, events, source_ids=ids)
    calls = {"n": 0}

    def flaky_post(c, records, version, **kw):
        calls["n"] += 1
        return (calls["n"] > 1), (200 if calls["n"] > 1 else 500), ""

    stats = fe.drain_outbox(snap, cfg, "v", post=flaky_post, max_posts=2)
    assert stats["failed"] == 1
    last_id, _ = fe.read_state(snap)
    assert last_id == 0  # nothing acked yet
    stats = fe.drain_outbox(snap, cfg, "v", post=flaky_post, max_posts=2)
    assert stats["sent"] == 3 and stats["remaining"] == 0
    last_id, _ = fe.read_state(snap)
    assert last_id == 3


def test_two_posts_per_flush_max(fe, tmp_path):
    snap = str(tmp_path / "snap")
    cfg = {"hash_key": HASH_KEY, "endpoint": "http://127.0.0.1:9", "token": "t"}
    events = [{"type": "session", "session_uuid": f"s{i}"} for i in range(500)]
    fe.enqueue(snap, events)
    calls = {"n": 0}

    def post(c, records, version, **kw):
        calls["n"] += 1
        return True, 200, ""

    stats = fe.drain_outbox(snap, cfg, "v", post=post)
    assert calls["n"] == 2 and stats["remaining"] == 100


def test_redirect_is_failure(fe, tmp_path):
    snap = str(tmp_path / "snap")
    cfg = {"hash_key": HASH_KEY, "endpoint": "http://127.0.0.1:9", "token": "t"}
    fe.enqueue(snap, [{"type": "session", "session_uuid": "s1"}])

    def redirect_post(c, records, version, **kw):
        return False, 302, "HTTPError"

    stats = fe.drain_outbox(snap, cfg, "v", post=redirect_post)
    assert stats["sent"] == 0 and len(fe.read_outbox(snap)) == 1


def test_post_to_closed_port_fails_fast(fe):
    cfg = {"hash_key": HASH_KEY, "endpoint": "http://127.0.0.1:1", "token": "t"}
    ok, status, detail = fe.post_payload(
        cfg, [{"event": {"type": "session", "session_uuid": "s"}}], "v")
    assert ok is False and status == 0


def test_atomic_writes_and_dir_mode_gate(fe, tmp_path):
    snap = str(tmp_path / "snap")
    fe._write_private(Path(snap) / "f.json", "x")
    assert (Path(snap) / "f.json").stat().st_mode & 0o777 == 0o600
    (tmp_path / "ro").mkdir()
    os.chmod(str(tmp_path / "ro"), 0o777)
    assert fe.snapshot_dir_ok(str(tmp_path / "ro")) is False
    os.chmod(str(tmp_path / "ro"), 0o755)
    assert fe.snapshot_dir_ok(str(tmp_path / "ro")) is True


def test_stale_tmp_reaped(fe, tmp_path):
    snap = tmp_path / "snap"
    stale = snap / "fleet-outbox.jsonl.1.tmp"
    stale.write_text("junk")
    old = (datetime.now().timestamp()) - 3600
    os.utime(str(stale), (old, old))
    fe.reap_stale_tmp(str(snap))
    assert not stale.exists()


def test_time_left_below_threshold_skips_network(fe, tmp_path):
    snap = str(tmp_path / "snap")
    cfg = {"hash_key": HASH_KEY, "endpoint": "http://127.0.0.1:9", "token": "t"}
    fe.enqueue(snap, [{"type": "session", "session_uuid": "s1"}])
    stats = fe.drain_outbox(snap, cfg, "v", post=lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no POST under budget")), max_posts=0)
    assert stats["posts"] == 0 and len(fe.read_outbox(snap)) == 1


def test_emit_fail_open_on_db_error(fe, tmp_path):
    _write_config(tmp_path, fe)
    stats = fe.emit_after_flush(trends_db=str(tmp_path / "nonexistent-dir" / "x.db"),
                                snapshot_dir=str(tmp_path / "snap"),
                                config_dir=str(tmp_path / "config"))
    assert stats is None  # fail-open, never raises
