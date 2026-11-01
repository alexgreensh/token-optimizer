"""Counted views: which ledger rows count as savings, on every total.

The ledgers keep every row ever written. The counted views leave out rows that
are not savings: image-bearing tool results (images bill by pixels, so an
archived screenshot saved nothing), fixture sessions written by test suites and
benchmarks, and twin rows written by two installs firing on the same call.
Real sessions from every harness (Claude UUIDs, short agent ids, Codex rollout
ids) must always count.

Run: python3 -m pytest tests/test_counted_views.py -v
"""
import ast
import importlib
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "token-optimizer" / "scripts"

REAL_UUID = "11111111-2222-3333-4444-555555555555"
AGENT_ID = "a0ce18efb2d1c3e4f"
CODEX_ID = "rollout-2026-10-01T10-00-00-0199aaaa-bbbb-cccc-dddd-eeeeffff0000"


@pytest.fixture()
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    conn = mod._init_trends_db()
    conn.close()
    yield mod
    sys.modules.pop("measure", None)


def _ts(seconds_ago=0.0, base=None):
    base = base or (datetime.now() - timedelta(days=1))
    return (base - timedelta(seconds=seconds_ago)).isoformat()


def _se(conn, event_type, tokens, sid, detail=None, ts=None, cost=None):
    conn.execute(
        "INSERT INTO savings_events (timestamp, event_type, tokens_saved, cost_saved_usd, "
        "session_id, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (ts or _ts(), event_type, tokens, tokens * 3e-6 if cost is None else cost, sid, detail))


def _ce(conn, feature, orig, comp, sid, pattern="cmd", ts=None, tier="measured", model=None):
    conn.execute(
        "INSERT INTO compression_events (timestamp, session_id, feature, command_pattern, "
        "original_tokens, compressed_tokens, verified, tier, model) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (ts or _ts(), sid, feature, pattern, orig, comp, tier, model))


def _seed(m):
    conn = sqlite3.connect(str(m.TRENDS_DB))
    base = datetime.now() - timedelta(days=1)
    # Real savings from three harnesses.
    _se(conn, "tool_archive", 1000, REAL_UUID, "replaced mcp__github__search (9,000 chars -> 400 chars)")
    _se(conn, "tool_archive", 500, AGENT_ID, "replaced Read (4,000 chars -> 300 chars)")
    _ce(conn, "bash_compress_pipeline", 900, 100, CODEX_ID)
    # Screenshots: not savings.
    _se(conn, "tool_archive", 200000, REAL_UUID,
        "replaced mcp__claude-in-chrome__computer (672,175 chars -> 1,540 chars)")
    _se(conn, "tool_archive_refetch_block", 25000, REAL_UUID,
        "blocked re-fetch of mcp__claude-in-chrome__computer (archived toolu_01X)")
    # Fixtures: not savings.
    _se(conn, "tool_archive", 7000, "test-session-0002dafd725145ea82a59bd70cac4331")
    _ce(conn, "build_output_compress", 5000, 100, "failwire-77086ecc", pattern="gcc -c main.c")
    _ce(conn, "bash_compress_pipeline", 4000, 100, "f3bench-session-0001")
    # Twins: the same row twice within a second counts once.
    _ce(conn, "bash_compress_git", 300, 100, REAL_UUID, pattern="git log", ts=_ts(0.0, base))
    _ce(conn, "bash_compress_git", 300, 100, REAL_UUID, pattern="git log", ts=_ts(-0.4, base))
    # Same command, same sizes, minutes apart: two real savings.
    _ce(conn, "bash_compress_git", 300, 100, REAL_UUID, pattern="git log", ts=_ts(-600, base))
    conn.commit()
    conn.close()


def test_views_leave_raw_rows_untouched(m):
    _seed(m)
    conn = m._init_trends_db()
    try:
        assert m._ensure_counted_views(conn)
        assert conn.execute("SELECT COUNT(*) FROM savings_events").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM compression_events").fetchone()[0] == 6
        assert conn.execute("SELECT COUNT(*) FROM counted_savings_events").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM counted_compression_events").fetchone()[0] == 3
    finally:
        conn.close()


def test_savings_summary_counts_only_real_rows(m):
    _seed(m)
    s = m._get_savings_summary(days=30)
    assert s["by_category"]["tool_archive"]["tokens_saved"] == 1500
    assert "tool_archive_refetch_block" not in s["by_category"]


def test_platform_exclusion_vectors_preserve_unattributed_actions(m):
    conn = m._init_trends_db()
    base = datetime.now() - timedelta(days=1)
    _se(conn, "checkpoint_restore", 100, REAL_UUID, "same", base.isoformat(), 1)
    _se(conn, "checkpoint_restore", 100, REAL_UUID, "same",
        (base + timedelta(seconds=1)).isoformat(), 2)
    _se(conn, "tool_archive", 100, REAL_UUID,
        "replaced mcp__playwright__browser_take_screenshot", base.isoformat(), 1)
    for _ in range(2):
        _se(conn, "checkpoint_restore", 100, "", "same", base.isoformat(), 1)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM savings_events").fetchone()[0] == 5
    view = m._counted_table(conn, "savings_events")
    assert conn.execute(f"SELECT COUNT(*), SUM(tokens_saved) FROM {view}").fetchone() == (3, 300)
    conn.close()


def test_compression_summary_counts_every_harness_and_dedups_twins(m):
    _seed(m)
    c = m._get_compression_summary(days=30)
    assert c["by_feature"]["bash_compress_pipeline"]["tokens_saved"] == 800  # Codex row only
    assert c["by_feature"]["bash_compress_git"]["tokens_saved"] == 400       # twin counted once
    assert "build_output_compress" not in c["by_feature"]                   # fixture only


def test_status_bar_buckets_match_the_summary(m):
    _seed(m)
    conn = m._init_trends_db()
    try:
        cutoff = (datetime.now() - timedelta(days=30)).isoformat()
        buckets = m._realized_savings_buckets(conn, cutoff)
    finally:
        conn.close()
    se = m._get_savings_summary(days=30)
    ce = m._get_compression_summary(days=30)
    assert sum(b["tokens"] for b in buckets.values()) == se["total_tokens"] + ce["total_tokens_saved"]


@pytest.mark.parametrize("sid,fixture", [
    ("test-session-abc", True), ("failwire-1", True), ("f3bench-session-0001", True),
    ("codex-sess-1", True), ("torture-session", True), ("unit-c-never-1", True),
    ("torture_test_001", False), ("s", False), ("demo", False),
    ("test-project", False), ("test_real_work", False), ("unit-c-real", False),
    ("f3bench-production", False), ("codex-sess-live", False),
    ("codex-sess-1-live", False), ("codex-sess-", False),
    (REAL_UUID, False), (AGENT_ID, False), (CODEX_ID, False), ("unknown", False), (None, False),
])
def test_fixture_ids_match_in_python_and_sql(m, sid, fixture):
    assert m._is_fixture_session_id(sid) is fixture
    conn = sqlite3.connect(":memory:")
    query = f"SELECT {m._fixture_session_sql('x')} FROM (SELECT ? AS x)"
    got = conn.execute(query, (sid,)).fetchone()[0]
    assert bool(got) is fixture


def test_unstamped_compression_rows_use_their_session_mix(m):
    conn = sqlite3.connect(str(m.TRENDS_DB))
    conn.execute(
        "INSERT INTO session_log (jsonl_path, date, session_uuid, model_usage_json) "
        "VALUES (?, ?, ?, ?)",
        ("/x.jsonl", datetime.now().date().isoformat(), REAL_UUID,
         '{"claude-opus-4-6": 1000000}'))
    _ce(conn, "bash_compress_pipeline", 1_000_100, 100, REAL_UUID, model="unknown")
    conn.commit()
    conn.close()
    c = m._get_compression_summary(days=30)
    tier = m.PRICING_TIERS[m._load_pricing_tier()]
    opus_rate = tier["claude_models"][m._claude_price_key("claude-opus-4-6", tier["claude_models"])]["input"]
    assert c["by_feature"]["bash_compress_pipeline"]["cost_saved_usd"] == pytest.approx(opus_rate, rel=1e-6)


def test_counted_reread_rows_for_excluded_events_are_dropped(m):
    _seed(m)
    conn = m._init_trends_db()
    try:
        shot_id = conn.execute(
            "SELECT id FROM savings_events WHERE detail GLOB '*claude-in-chrome__computer*' "
            "AND event_type='tool_archive'").fetchone()[0]
        real_id = conn.execute(
            "SELECT id FROM savings_events WHERE session_id=?", (AGENT_ID,)).fetchone()[0]
        for key in (f"se:{shot_id}", f"se:{real_id}"):
            conn.execute(
                "INSERT INTO counted_reread (event_key, source, event_ts, event_month, tokens, "
                "oneshot_usd, reread_usd) VALUES (?, 'se', ?, ?, 1, 1.0, 10.0)",
                (key, _ts(), datetime.now().strftime("%Y-%m")))
        conn.commit()
    finally:
        conn.close()
    summary = m._counted_cumulative_summary()
    assert summary.get("available") is not False
    conn = m._init_trends_db()
    try:
        m._update_counted_cumulative(conn, max_sessions=0)
        keys = {r[0] for r in conn.execute("SELECT event_key FROM counted_reread")}
    finally:
        conn.close()
    assert f"se:{shot_id}" not in keys and f"se:{real_id}" in keys


# Every feature string a hook writes must be either a labelled saving (so it
# reaches the totals) or explicitly marked as bookkeeping.
_WRITERS = {"log_compression_event", "_log_compression_event", "_log_event"}
_FEATURE_VARS = {"feature", "_feature", "_log_feature"}


def _logged_features():
    found = set()
    for path in SCRIPTS.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        consts = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        consts[t.id] = node.value.value
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "_COMPRESS_FEATURE_MAP" for t in node.targets):
                found.update(v.value for v in node.value.values if isinstance(v, ast.Constant))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str) and any(
                        isinstance(t, ast.Name) and t.id in _FEATURE_VARS for t in node.targets):
                found.add(node.value.value)
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in _WRITERS:
                continue
            for kw in node.keywords:
                if kw.arg == "feature":
                    v = kw.value
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        found.add(v.value)
                    elif isinstance(v, ast.Name) and v.id in consts:
                        found.add(consts[v.id])
    return found


def test_every_logged_feature_is_totalled_or_marked_bookkeeping(m):
    feats = _logged_features()
    assert {"build_output_compress", "codex_command_compress", "bash_compress_pipeline"} <= feats
    known = set(m._V5_COMPRESSION_LABELS) | set(m._NON_SAVINGS_COMPRESSION_FEATURES)
    missing = sorted(f for f in feats if f not in known and f not in m._LEGACY_SAVINGS_LABELS)
    assert missing == [], f"logged but never totalled: {missing}"


def _counted_se_ids(m):
    conn = m._init_trends_db()
    try:
        assert m._ensure_counted_views(conn)
        return {r[0]: r[1] for r in conn.execute(
            "SELECT id, detail FROM counted_savings_events WHERE event_type='tool_archive_reexpand'")}
    finally:
        conn.close()


def test_screenshot_reexpansion_is_left_out_with_its_archive(m):
    """An archived screenshot is not text savings, so re-expanding it is not a
    text debit either. Older rows carry only the tool_use_id and pair with
    their archive row by session, time and size; text re-expansions still count."""
    conn = sqlite3.connect(str(m.TRENDS_DB))
    base = datetime.now() - timedelta(days=1)
    _se(conn, "tool_archive", 76185, REAL_UUID,
        "replaced mcp__claude-in-chrome__computer (253,279 chars -> 1,869 chars)", ts=_ts(20, base))
    _se(conn, "tool_archive_reexpand", 76752, REAL_UUID, "toolu_01Shot", ts=_ts(10, base))
    _se(conn, "tool_archive", 9000, REAL_UUID,
        "replaced mcp__github__search (30,000 chars -> 1,869 chars)", ts=_ts(8, base))
    _se(conn, "tool_archive_reexpand", 9567, REAL_UUID, "toolu_01Text", ts=_ts(5, base))
    _se(conn, "tool_archive_reexpand", 40000, REAL_UUID,
        "toolu_01New (mcp__claude-in-chrome__computer)", ts=_ts(1, base))
    conn.commit()
    conn.close()
    kept = set(_counted_se_ids(m).values())
    assert kept == {"toolu_01Text"}


def test_screenshot_pairing_uses_the_nearest_archive(m):
    """A text archive written after the screenshot is the one being re-expanded."""
    conn = sqlite3.connect(str(m.TRENDS_DB))
    base = datetime.now() - timedelta(days=1)
    _se(conn, "tool_archive", 9000, REAL_UUID,
        "replaced mcp__claude-in-chrome__computer (30,000 chars -> 1,869 chars)", ts=_ts(30, base))
    _se(conn, "tool_archive", 9100, REAL_UUID,
        "replaced Read (30,300 chars -> 1,869 chars)", ts=_ts(20, base))
    _se(conn, "tool_archive_reexpand", 9600, REAL_UUID, "toolu_01Read", ts=_ts(10, base))
    conn.commit()
    conn.close()
    assert set(_counted_se_ids(m).values()) == {"toolu_01Read"}


def test_reexpand_debit_records_the_tool_and_debits_once(m):
    m._log_reexpand_debit(REAL_UUID, "toolu_01Shot", "x" * 4000, "mcp__claude-in-chrome__computer")
    m._log_reexpand_debit(REAL_UUID, "toolu_01Shot", "x" * 4000, "mcp__claude-in-chrome__computer")
    m._log_reexpand_debit(REAL_UUID, "toolu_01Old", "x" * 4000)
    conn = sqlite3.connect(str(m.TRENDS_DB))
    conn.execute(
        "INSERT INTO savings_events (timestamp, event_type, tokens_saved, cost_saved_usd, session_id, detail) "
        "VALUES (?, 'tool_archive_reexpand', 10, 0, ?, 'toolu_01Legacy')", (_ts(), REAL_UUID))
    conn.commit()
    conn.close()
    m._log_reexpand_debit(REAL_UUID, "toolu_01Legacy", "x" * 4000, "Read")
    conn = sqlite3.connect(str(m.TRENDS_DB))
    rows = sorted(r[0] for r in conn.execute(
        "SELECT detail FROM savings_events WHERE event_type='tool_archive_reexpand'"))
    conn.close()
    assert rows == ["toolu_01Legacy", "toolu_01Old", "toolu_01Shot (mcp__claude-in-chrome__computer)"]
    assert set(_counted_se_ids(m).values()) == {"toolu_01Old", "toolu_01Legacy"}
