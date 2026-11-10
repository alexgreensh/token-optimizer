"""Setup trims: per-component, dated, compounded per request (savings-truth U3).

Synthetic fixtures only. Each test builds a throwaway HOME / data dir / DB.
"""
import json
import os
import time
from datetime import datetime, timedelta

import pytest

from test_usage_counted_once import _load

HOME = "/home/tester"
LOCAL = "-home-tester"                       # a session run in HOME
LOCAL_SUB = "-home-tester-work-app"          # a session in a subdir
STUB = "-home-tester--claude-sync-stubs-x"   # synced from a remote machine
OPUS = "claude-opus-5-5"


@pytest.fixture
def m(tmp_path, monkeypatch):
    return _load(tmp_path, monkeypatch)


def _mods():
    from _measure import (structural_ledger as L, structural_listing as LS,
                          structural_report as R, structural_seed as S, structural_value as V)
    return L, LS, R, S, V


def _card(m, model=OPUS):
    tier = m.PRICING_TIERS["anthropic"]
    return m._counted_exact_card(model, tier, prompt_tokens=0)


def _session(conn, m, uuid, project, start_dt, path=None):
    metrics = {start_dt.strftime("%Y-%m-%d"): {"first": start_dt.timestamp(), "last": start_dt.timestamp() + 60}}
    conn.execute(
        "INSERT INTO session_log (jsonl_path, date, project, session_uuid, api_calls, daily_metrics_json, "
        "first_activity_day) VALUES (?,?,?,?,?,?,?)",
        (path or ("/t/%s.jsonl" % uuid), start_dt.strftime("%Y-%m-%d"), project, uuid, 1,
         json.dumps(metrics), start_dt.strftime("%Y-%m-%d")))


def _req(conn, m, uuid, n, day, cr=0, cc=0, cc_1h=0, cc_5m=0, model=OPUS):
    conn.execute(
        "INSERT INTO usage_request_claims (request_hash, owner_hash, calls, inp, out, cr, cc, cc_1h, cc_5m, model, day) "
        "VALUES (?,?,1,10,10,?,?,?,?,?,?)",
        (n, m._stable_hash63(uuid), cr, cc, cc_1h, cc_5m, model, day))


def _trim(L, ts, tokens=1000, comp="skills", item="old-skill", scope="global", source="audit_action"):
    return L.make_trim(comp, item, scope, tokens, 0, ts, source, evidence="/b/" + item)


def test_owner_hash_matches_the_collector(m):
    _, _, _, _, V = _mods()
    assert V._hash63("abc-123") == m._stable_hash63("abc-123")


def test_fresh_home_reports_zero_with_no_trims_message(m):
    _, _, R, _, _ = _mods()
    out = m._compute_structural_savings(days=30)
    assert out["tokens_saved"] == 0 and out["cost_saved_usd"] == 0.0
    assert out["status"] == "no_trims" and out["message"] == R.NO_TRIMS
    assert out["baseline_source"] is None  # never a baseline delta


def test_record_is_idempotent_and_close_void_resolve(m, tmp_path):
    L, *_ = _mods()
    d = tmp_path / "d"
    t = _trim(L, "2026-01-02T00:00:00+00:00")
    assert L.record_trim(d, t) is True
    assert L.record_trim(d, t) is False
    L.close_trim(d, t["id"], "2026-01-05T00:00:00+00:00", "item restored")
    got = L.load_trims(d)
    assert len(got) == 1 and got[0]["closed_at"].startswith("2026-01-05")
    t2 = dict(_trim(L, "2026-01-03T00:00:00+00:00", item="b"), batch="seed-x")
    L.record_trim(d, t2)
    L.void_batch(d, "seed-x", "2026-01-06T00:00:00+00:00", "reverted")
    assert [x["item"] for x in L.load_trims(d)] == ["old-skill"]
    # append-only: every record is still on disk
    assert len((d / L.TRIMS_FILE).read_text().splitlines()) == 4
    with pytest.raises(ValueError):
        L.make_trim("skills", "x", "global", 10, 20, "2026-01-01", "audit_action")


def _db(m):
    conn = m._init_trends_db()
    return conn


def test_hit_miss_and_small_requests_priced_at_the_exact_card(m):
    _, _, _, _, V = _mods()
    L = _mods()[0]
    conn = _db(m)
    day = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=1)
    ts = (day - timedelta(hours=2)).astimezone().isoformat()
    _session(conn, m, "s1", LOCAL, day)
    d = day.strftime("%Y-%m-%d")
    _req(conn, m, "s1", 1, d, cr=50_000)                       # hit
    _req(conn, m, "s1", 2, d, cr=0, cc=40_000, cc_1h=40_000)   # miss, 1h write
    _req(conn, m, "s1", 3, d, cr=0, cc=40_000, cc_5m=40_000)   # miss, 5m write
    _req(conn, m, "s1", 4, d, cr=100, cc=200)                  # too small to carry it
    conn.commit()
    t = L.load_trims_from([_trim(L, ts, tokens=1000)])
    card = _card(m)
    res = V.value_trims(conn, t, d, d, lambda model, p: _card(m, model), HOME)
    a = res["trims"][t[0]["id"]]
    assert a["requests"] == 4 and a["read_tokens"] == 4000 and a["write_tokens"] == 0
    assert a["read_usd"] == pytest.approx(round(4000 * card["cache_read"] / 1e6, 4))
    assert a["write_1h_usd"] == 0 and a["write_5m_usd"] == 0
    # the exact card, never the family mix: Opus 5.5 reads below the generic opus card
    assert card["cache_read"] < m.PRICING_TIERS["anthropic"]["claude_models"]["opus"]["cache_read"]
    conn.close()


def test_scope_start_time_and_projection(m):
    L, _, _, _, V = _mods()
    conn = _db(m)
    day = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=1)
    d = day.strftime("%Y-%m-%d")
    trim_ts = day.astimezone().isoformat()
    _session(conn, m, "before", LOCAL, day - timedelta(hours=1))   # started before the trim
    _session(conn, m, "after", LOCAL_SUB, day + timedelta(hours=1))
    _session(conn, m, "stub", STUB, day + timedelta(hours=1))      # remote: never loads this setup
    _session(conn, m, "elsewhere", "-opt-other", day + timedelta(hours=1))
    for i, u in enumerate(("before", "after", "stub", "elsewhere")):
        _req(conn, m, u, 10 + i, d, cr=50_000)
    conn.commit()
    g = L.load_trims_from([_trim(L, trim_ts, tokens=500)])
    card = lambda model, p: _card(m, model)
    real = V.value_trims(conn, g, d, d, card, HOME)["trims"][g[0]["id"]]
    proj = V.value_trims(conn, g, d, d, card, HOME, projected=True)["trims"][g[0]["id"]]
    assert real["requests"] == 1      # only "after"
    assert proj["requests"] == 2      # "before" + "after"; stub and elsewhere never
    p = L.load_trims_from([_trim(L, trim_ts, tokens=500, comp="memory_md", item="MEMORY.md",
                                 scope="project:" + LOCAL)])
    assert V.value_trims(conn, p, d, d, card, HOME, projected=True)["trims"][p[0]["id"]]["requests"] == 1
    conn.close()


def test_realized_setup_counts_later_days_of_preexisting_session(m):
    L, _, _, _, V = _mods()
    conn = _db(m)
    day = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=3)
    _session(conn, m, 'ongoing', LOCAL, day - timedelta(hours=2))
    for i in range(3):
        _req(conn, m, 'ongoing', 200 + i, (day + timedelta(days=i)).strftime('%Y-%m-%d'), cr=50000)
    conn.commit()
    trims = L.load_trims_from([_trim(L, day.astimezone().isoformat(), tokens=500)])
    start, end = day.strftime('%Y-%m-%d'), (day + timedelta(days=2)).strftime('%Y-%m-%d')
    result = V.value_trims(conn, trims, start, end, lambda model, p: _card(m, model), HOME)['trims'][trims[0]['id']]
    assert result['requests'] == 2
    assert result['read_tokens'] == 1000
    conn.close()


def test_realized_setup_prices_each_call_at_its_cache_read_rate(m):
    L, _, _, _, V = _mods()
    conn = _db(m)
    day = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=1)
    _session(conn, m, 'after-trim', LOCAL, day)
    _req(conn, m, 'after-trim', 301, day.strftime('%Y-%m-%d'), cr=0, cc=50000, cc_1h=50000)
    conn.commit()
    trims = L.load_trims_from([_trim(L, (day - timedelta(hours=1)).astimezone().isoformat(), tokens=1000)])
    result = V.value_trims(conn, trims, day.strftime('%Y-%m-%d'), day.strftime('%Y-%m-%d'), lambda model, p: _card(m, model), HOME)['trims'][trims[0]['id']]
    assert result['read_tokens'] == 1000 and result['write_tokens'] == 0
    assert result['usd'] == pytest.approx(round(1000 * _card(m)['cache_read'] / 1e6, 4))
    conn.close()


def _snap(L, skills, plugin_tokens=0, memory=None):
    snap = {"skills": {"scope": "global", "items": dict(skills)},
            "plugin_skills": {"scope": "global", "items": {L.UNITEMIZED: plugin_tokens}}}
    if memory is not None:
        snap["memory_md"] = {"scope": "project:" + LOCAL, "items": {"MEMORY.md": memory}}
    return snap


def test_unrelated_growth_never_changes_or_closes_a_trim(m, tmp_path):
    L, *_ = _mods()
    d, cdir = tmp_path / "d", tmp_path / "c"
    (cdir / "_backups" / "skills-archived-20260101" / "gone").mkdir(parents=True)
    L.tick(d, _snap(L, {"keep": 80, "gone": 120}, 1000), cdir, ts="2026-01-01T00:00:00+00:00", force=True)
    r = L.tick(d, _snap(L, {"keep": 80}, 1000), cdir, ts="2026-01-02T00:00:00+00:00", force=True)
    assert len(r["opened"]) == 1
    before = L.load_trims(d)
    # +5,000 tokens of unrelated plugin skills and a brand-new local skill
    r = L.tick(d, _snap(L, {"keep": 80, "new-one": 300}, 6000), cdir, ts="2026-01-03T00:00:00+00:00", force=True)
    assert r["opened"] == [] and r["closed"] == []
    after = L.load_trims(d)
    assert [(t["id"], t["tokens"], t["closed_at"]) for t in after] == \
        [(t["id"], t["tokens"], t["closed_at"]) for t in before]
    assert after[0]["source"] == "audit_action" and after[0]["tier"] == "measured"


def test_restoring_one_item_closes_exactly_that_trim(m, tmp_path):
    L, *_ = _mods()
    d, cdir = tmp_path / "d", tmp_path / "c"
    for n in ("a", "b"):
        (cdir / "_backups" / "skills-archived-20260101" / n).mkdir(parents=True)
    L.tick(d, _snap(L, {"a": 100, "b": 100, "c": 100}), cdir, ts="2026-01-01T00:00:00+00:00", force=True)
    L.tick(d, _snap(L, {"c": 100}), cdir, ts="2026-01-02T00:00:00+00:00", force=True)
    r = L.tick(d, _snap(L, {"a": 100, "c": 100}), cdir, ts="2026-01-03T00:00:00+00:00", force=True)
    trims = {t["item"]: t for t in L.load_trims(d)}
    assert len(r["closed"]) == 1
    assert trims["a"]["closed_at"].startswith("2026-01-03") and trims["b"]["closed_at"] is None


def test_rename_is_not_a_trim_and_unbacked_shrink_is_unattributed(m, tmp_path):
    L, *_ = _mods()
    d, cdir = tmp_path / "d", tmp_path / "c"
    L.tick(d, _snap(L, {"old-name": 100, "x": 200}), cdir, ts="2026-01-01T00:00:00+00:00", force=True)
    r = L.tick(d, _snap(L, {"new-name": 101, "x": 100}), cdir, ts="2026-01-02T00:00:00+00:00", force=True)
    trims = L.load_trims(d)
    assert len(r["opened"]) == 1 and trims[0]["item"] == "x"
    assert trims[0]["source"] == "detected" and trims[0]["tier"] == "unattributed"


def test_project_component_missing_in_another_cwd_is_not_a_trim(m, tmp_path):
    L, *_ = _mods()
    d, cdir = tmp_path / "d", tmp_path / "c"
    L.tick(d, _snap(L, {"x": 100}, memory=3000), cdir, ts="2026-01-01T00:00:00+00:00", force=True)
    r = L.tick(d, _snap(L, {"x": 100}), cdir, ts="2026-01-02T00:00:00+00:00", force=True)
    assert r["opened"] == []


def test_install_baseline_is_captured_once_and_never_recaptured(m, tmp_path):
    L, *_ = _mods()
    d = tmp_path / "d"
    assert L.capture_install_baseline(d, _snap(L, {"a": 100}), "2026-01-01T00:00:00+00:00") is True
    first = (d / L.BASELINE_FILE).read_text()
    assert L.capture_install_baseline(d, _snap(L, {}), "2026-02-01T00:00:00+00:00") is False
    assert (d / L.BASELINE_FILE).read_text() == first


def test_throttle_skips_history_until_forced(m, tmp_path):
    L, *_ = _mods()
    d, cdir = tmp_path / "d", tmp_path / "c"
    L.tick(d, _snap(L, {"a": 100}), cdir, force=True)
    r = L.tick(d, _snap(L, {}), cdir)
    assert r["appended"] is False and r["opened"] == []


def _transcript(path, chars, when):
    rec = {"type": "attachment", "timestamp": when,
           "attachment": {"type": "skill_listing", "isInitial": True, "content": "x" * chars}}
    path.write_text(json.dumps({"type": "user", "timestamp": when}) + "\n" + json.dumps(rec) + "\n")


def test_listing_check_credits_only_the_observed_shrink(m, tmp_path):
    L, LS, *_ = _mods()
    base = time.time() - 86400
    sessions = []
    for i in range(4):
        p = tmp_path / ("pre%d.jsonl" % i); _transcript(p, 30_000, "x"); sessions.append((base - 3600 * (i + 1), str(p)))
        p = tmp_path / ("post%d.jsonl" % i); _transcript(p, 29_600, "x"); sessions.append((base + 3600 * (i + 1), str(p)))
    ts = datetime.fromtimestamp(base).astimezone().isoformat()
    trims = L.load_trims_from([_trim(L, ts, tokens=300, item="a"), _trim(L, ts, tokens=100, item="b")])
    LS.apply_listing_check(trims, sessions, 4.0)
    # 400 chars observed = 100 tokens of the 400 estimated -> each trim at 25%
    assert [t["tokens"] for t in trims] == [75, 25]
    assert all(t["listing_check"] == "measured" for t in trims)
    capped = L.load_trims_from([_trim(L, ts, tokens=300, item="c")])
    flat = [(s, p) for s, p in sessions if "pre" in p] + \
           [(base + 10 + i, str(tmp_path / ("pre%d.jsonl" % i))) for i in range(4)]
    LS.apply_listing_check(capped, flat, 4.0)   # listing at its budget: nothing was freed
    assert capped[0]["tokens"] == 0
    pending = L.load_trims_from([_trim(L, ts, tokens=300, item="d")])
    LS.apply_listing_check(pending, [], 4.0)
    assert pending[0]["listing_check"] == "pending" and pending[0]["tokens"] == 300


def test_pending_listing_trim_moves_to_estimated_band(m, monkeypatch):
    L, _, R, _, _ = _mods()
    conn = _db(m)
    day = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=1)
    _session(conn, m, "s1", LOCAL, day + timedelta(hours=1), path="/nonexistent/s1.jsonl")
    _req(conn, m, "s1", 1, day.strftime("%Y-%m-%d"), cr=50_000)
    conn.commit(); conn.close()
    trims = L.load_trims_from([_trim(L, day.astimezone().isoformat(), tokens=400)])
    out = R.compute(days=30, home=HOME, trims=trims)
    assert out["tokens_saved"] == 0 and out["estimated"]["tokens_saved"] == 400
    assert out["trims"][0]["tier"] == "estimated" and out["trims"][0]["listing_check"] == "pending"


def test_attested_options_price_side_by_side_and_write_nothing(m):
    L, _, R, _, _ = _mods()
    conn = _db(m)
    day = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=2)
    _session(conn, m, "s1", LOCAL, day)
    _req(conn, m, "s1", 1, day.strftime("%Y-%m-%d"), cr=500_000)
    conn.commit(); conn.close()
    rows = R.attested_whatif([100, 200], "2026-01-01T00:00:00+00:00", home=HOME)
    assert [r["tokens_per_call"] for r in rows] == [100, 200]
    assert rows[1]["window_usd"] == pytest.approx(2 * rows[0]["window_usd"])
    assert not (m.SNAPSHOT_DIR / L.TRIMS_FILE).exists()


def test_compound_structural_uses_per_request_pricing(m, monkeypatch):
    conn = _db(m)
    day = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=1)
    _session(conn, m, "s1", LOCAL, day)
    _req(conn, m, "s1", 1, day.strftime("%Y-%m-%d"), cr=500_000)
    _req(conn, m, "s1", 2, day.strftime("%Y-%m-%d"), cr=0, cc=90_000, cc_1h=90_000)
    conn.commit(); conn.close()
    from pathlib import Path
    monkeypatch.setattr(m, "HOME", Path(HOME))
    terms = m._compound_structural(1000, 30, fallback_prefix=0)
    card = _card(m)
    assert terms["events"] == 2
    assert terms["cost_saved_usd"] == pytest.approx(round(2000 * card["cache_read"] / 1e6, 4))
    assert m._compound_structural(0, 30, fallback_prefix=0) is None


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_seed_is_dry_run_by_default_and_reversible(m, tmp_path):
    L, _, _, S, _ = _mods()
    cdir = m.CLAUDE_DIR
    bk = tmp_path / "bk" / "token-optimizer-20260101-120000"
    fm = lambda d: "---\nname: x\ndescription: %s\n---\nbody\n" % ("d" * d)
    _write(tmp_path / "bk" / "skills-archive" / "old" / "SKILL.md", fm(800))
    _write(tmp_path / "bk" / "skills-archive" / "README.txt", "Archived by Token Optimizer audit")
    _write(bk / "skill-descriptions" / "kept.SKILL.md", fm(1200))
    _write(cdir / "skills" / "kept" / "SKILL.md", fm(200))
    _write(tmp_path / "bk" / "commands-archive" / "ns" / "cmd.md", fm(600))
    _write(bk / "MEMORY--home-tester.md", "line\n" * 50)
    _write(cdir / "projects" / "-home-tester" / "memory" / "MEMORY.md", "line\n" * 10)

    class A:
        backup_dir = str(bk); skills_archive = str(tmp_path / "bk" / "skills-archive")
        commands_archive = str(tmp_path / "bk" / "commands-archive"); ts = "2026-01-01T13:00:00+00:00"
        revert = ""; apply = False
    out = S.run(A, m)
    assert out["dry_run"] is True and not (m.SNAPSHOT_DIR / L.TRIMS_FILE).exists()
    items = {(i["component"], i["item"], i["scope"]) for i in out["items"]}
    assert items == {("skills", "old", "global"), ("skills", "kept", "global"),
                     ("commands", "ns/cmd", "global"), ("memory_md", "MEMORY.md", "project:-home-tester")}
    A.apply = True
    out = S.run(A, m)
    assert out["written"] == 4 and len(L.load_trims(m.SNAPSHOT_DIR)) == 4
    assert {t["source"] for t in L.load_trims(m.SNAPSHOT_DIR)} == {"audit_action"}
    A.revert = out["batch"]
    S.run(A, m)
    assert L.load_trims(m.SNAPSHOT_DIR) == []


def test_memory_is_measured_as_loaded(m, tmp_path):
    _, _, _, S, _ = _mods()
    p = tmp_path / "MEMORY.md"
    p.write_text("x" * 99 + "\n" + ("y" * 199 + "\n") * 300, encoding="utf-8")
    n = S.loaded_memory_chars(p)
    assert n <= S.MEMORY_MAX_BYTES and n < len(p.read_text())
    p.write_text("z\n" * 250, encoding="utf-8")
    assert S.loaded_memory_chars(p) == len("z\n" * 200) - 1


def test_itemize_scopes(m, tmp_path):
    L, *_ = _mods()
    comps = {
        "claude_md_global": {"exists": True, "tokens": 500, "path": "/h/.claude/CLAUDE.md"},
        "claude_md_project_app": {"exists": True, "tokens": 300, "path": "/h/work/app/CLAUDE.md"},
        "memory_md": {"exists": True, "tokens": 900, "path": "/h/.claude/projects/-h-work/memory/MEMORY.md"},
        "skills": {"names": ["s1"]}, "skills_detail": {"s1": {"frontmatter_tokens": 70}},
        "commands": {"names": []}, "agents": {"names": []},
        "plugin_skills": {"tokens": 4000}, "mcp_tools": {"tokens": 10},
    }
    snap = L.itemize(comps, tmp_path, lambda p: 0)
    assert snap["claude_md_global"]["scope"] == "global"
    assert snap["claude_md_project_app"]["scope"] == "tree:-h-work-app"
    assert snap["memory_md"] == {"scope": "project:-h-work", "items": {"MEMORY.md": 900}}
    assert snap["skills"]["items"] == {"s1": 70}
    assert snap["plugin_skills"]["items"] == {L.UNITEMIZED: 4000}


def test_cli_status_runs(m, capsys):
    _, _, R, _, _ = _mods()
    assert R.cli(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["message"] == R.NO_TRIMS


def test_structural_script_entry_point(tmp_path):
    import subprocess
    import sys
    from test_usage_counted_once import SCRIPTS
    (tmp_path / "c" / "projects").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "TOKEN_OPTIMIZER"))}
    env.update(TOKEN_OPTIMIZER_SNAPSHOT_DIR=str(tmp_path / "d"), CLAUDE_CONFIG_DIR=str(tmp_path / "c"),
               TOKEN_OPTIMIZER_RUNTIME="claude", PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, str(SCRIPTS / "structural.py"), "status"], env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert json.loads(r.stdout)["status"] == "no_trims"
    r = subprocess.run([sys.executable, str(SCRIPTS / "structural.py"), "attest", "--tokens", "100",
                        "--date", "2026-01-01"], env=env, capture_output=True, text=True, timeout=120)
    assert json.loads(r.stdout)["dry_run"] is True and not (tmp_path / "d" / "structural_trims.jsonl").exists()
