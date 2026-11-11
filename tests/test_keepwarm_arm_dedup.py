#!/usr/bin/env python3
"""Regression: duplicate keep-warm arm records must not multiply pings.

Reachable state (evidence below): a double-registered Stop hook -- the
settings-fossil scenario ``install_reconcile`` / ``_reconcile_sessionend_fossils``
exist to clean up -- fires ``keepwarm-arm`` twice for one stop, and any two Stop
events without transcript growth between them do the same. Both calls append an
arm record with an IDENTICAL (session_id, transcript_mtime, byte_size) snapshot,
because ``write_keepwarm_arm_record`` never dedups.

The tick loop then fires one probe ping PER RECORD for the same paused session
in a single tick, because:

  * the per-pause probe cap (``pings_fired``, max 2 probes) is tracked per
    record, not per session;
  * the ping command uses ``--no-session-persistence``, so a fired ping never
    bumps the transcript mtime that the idle re-check and the classifier watch
    -- the second record's ``recently-active`` guard never trips.

That violates the documented economics ("max 2 pings unless the predictor
promotes sustain", keepwarm_enable copy): with the LLM ping escape hatch on,
one pause is probed 2 x (record count) times per cadence window.

These tests pin the fix: arming is idempotent for an unchanged transcript
snapshot, so a duplicate arm is dropped and one tick fires at most one probe
per paused session.

Run: python3 -m pytest tests/test_keepwarm_arm_dedup.py -q
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def m(monkeypatch, tmp_path):
    """Fresh measure module against a fully sandboxed home/snapshot dir."""
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    (tmp_path / "claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "claude")
    from _measure import keepwarm_safety
    monkeypatch.setattr(keepwarm_safety, "find_owner", lambda: {"pid": 123, "start": "fixture"})
    monkeypatch.setattr(keepwarm_safety, "owner_alive", lambda owner: bool(owner))
    # The LLM ping is dogfood-gated; the duplicate-fire path only exists when
    # the escape hatch is explicitly on.
    monkeypatch.setenv("TOKEN_OPTIMIZER_KEEPWARM_ALLOW_LLM_PING", "1")
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    yield mod, tmp_path
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _write_transcript(tmp_path: Path, age_seconds: float) -> Path:
    """A paused session's transcript: one usage-bearing assistant turn, old mtime."""
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    transcript = tmp_path / "session-abc123.jsonl"
    ts = datetime.fromtimestamp(time.time() - age_seconds, timezone.utc).isoformat()
    record = {
        "type": "assistant",
        "timestamp": ts,
        "cwd": str(project),
        "message": {
            "model": "claude-sonnet-4-5",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 50,
                "cache_read_input_tokens": 20000,
                "cache_creation": {"ephemeral_5m_input_tokens": 1000},
            },
        },
    }
    transcript.write_text(json.dumps(record) + "\n", encoding="utf-8")
    old = time.time() - age_seconds
    os.utime(transcript, (old, old))
    return transcript


def _arm(m, tmp_path, age_seconds=7200.0):
    mod, _ = m
    transcript = _write_transcript(tmp_path, age_seconds)
    rec = mod.write_keepwarm_arm_record("abc123", str(transcript))
    assert rec is not None, "arm setup failed (transcript usage not recognized)"
    return transcript


def _sidecar_records(mod):
    return mod.load_keepwarm_records()


def test_arm_is_idempotent_for_an_unchanged_snapshot(m, tmp_path):
    """Two arms of the same unchanged transcript keep ONE record.

    Reachability: hooks.json double-registration (fossil) fires the Stop path
    twice for one stop; both arms see the same transcript mtime/size.
    """
    mod, _ = m
    transcript = _arm(m, tmp_path)
    rec2 = mod.write_keepwarm_arm_record("abc123", str(transcript))
    records = [r for r in _sidecar_records(mod) if r.get("session_id") == "abc123"]
    assert len(records) == 1, (
        f"duplicate arm records for one unchanged transcript snapshot: {len(records)}"
    )


def test_arm_after_real_turn_appends(m, tmp_path):
    """Dedup must not eat genuine re-arms: a grown transcript arms again."""
    mod, _ = m
    transcript = _arm(m, tmp_path)
    # A new turn: transcript grows and mtime advances.
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "timestamp": "x"}) + "\n")
    newer = time.time() - 3600
    os.utime(transcript, (newer, newer))
    rec2 = mod.write_keepwarm_arm_record("abc123", str(transcript))
    assert rec2 is not None
    records = [r for r in _sidecar_records(mod) if r.get("session_id") == "abc123"]
    assert len(records) == 2


def test_one_tick_fires_one_probe_per_paused_session(m, tmp_path):
    """End-to-end: with duplicate records present, one tick fires ONE ping."""
    mod, _ = m
    transcript = _write_transcript(tmp_path, 7200.0)
    # Simulate the fossil double-fire directly at the sidecar level (two
    # identical-snapshot records, as two Stop-hook registrations produce).
    for _ in range(2):
        rec = mod.write_keepwarm_arm_record("abc123", str(transcript))
        assert rec is not None

    fires = []

    class _Proc:
        returncode = 0
        stdout = json.dumps({"cost_usd": 0.001, "cache_read": 20000, "cache_write": 0})
        stderr = ""

    def _fake_runner(cmd, cwd, env, timeout):
        fires.append(list(cmd))
        return _Proc()

    summary = mod.keepwarm_tick(
        now=time.time(),
        env=dict(os.environ),
        runner=_fake_runner,
        gate=lambda **kw: (True, "ok"),
    )
    sid_fires = [c for c in fires if "abc123" in [str(a) for a in c]]
    assert len(sid_fires) <= 1, (
        f"one tick fired {len(sid_fires)} pings for one paused session "
        f"(duplicate arm records multiply the per-pause probe cap); "
        f"decisions={summary.get('decisions')}"
    )


def test_two_sessions_arm_independently(m, tmp_path):
    """Dedup is keyed per session: two legit sessions each keep their record."""
    mod, _ = m
    t1 = _write_transcript(tmp_path, 7200.0)
    t2 = tmp_path / "session-def456.jsonl"
    t2.write_text(t1.read_text(encoding="utf-8"), encoding="utf-8")
    old = time.time() - 7200.0
    os.utime(t2, (old, old))
    assert mod.write_keepwarm_arm_record("abc123", str(t1)) is not None
    assert mod.write_keepwarm_arm_record("def456", str(t2)) is not None
    sids = sorted(r.get("session_id") for r in _sidecar_records(mod))
    assert sids == ["abc123", "def456"]


def _race_two_arms(mod, transcript):
    barrier = threading.Barrier(2)
    results, errors = [], []

    def _arm_again():
        try:
            barrier.wait()
            results.append(mod.write_keepwarm_arm_record("abc123", str(transcript)))
        except Exception as exc:  # the writer must never raise into the hook chain
            errors.append(exc)

    threads = [threading.Thread(target=_arm_again) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def _assert_race_invariants(mod, results, errors):
    records = [r for r in _sidecar_records(mod) if r.get("session_id") == "abc123"]
    assert not errors, errors
    assert len(results) == 2
    assert len(records) == 1, f"racing arms produced {len(records)} records"
    # The lock is a bounded lease (75ms): a contender that cannot get it skips the
    # mutation and returns None BY DESIGN, so a loaded runner may legitimately hand
    # one racer None. What must hold: never an exception, exactly one stored record,
    # at least one racer saw it, and every non-None result is that one record.
    seen = [r for r in results if r is not None]
    assert seen, "neither racer saw the armed record"
    assert all(r.get("transcript_mtime") == records[0]["transcript_mtime"]
               and r.get("byte_size") == records[0]["byte_size"] for r in seen)


def test_concurrent_arms_collapse_to_one_record(m, tmp_path):
    """Two racing Stop-hook processes (fossil double-registration) arm once."""
    mod, _ = m
    transcript = _arm(m, tmp_path)
    results, errors = _race_two_arms(mod, transcript)
    _assert_race_invariants(mod, results, errors)


def test_concurrent_arms_when_the_lock_holder_outlasts_the_lease(m, tmp_path):
    """A contender skips after the 75ms acquisition timeout while the holder scans."""
    mod, _ = m
    transcript = _arm(m, tmp_path)
    original = mod.load_keepwarm_records
    holder_scanning = threading.Event()
    release_holder = threading.Event()
    results, errors = [], []

    def held_load(*args, **kwargs):
        holder_scanning.set()
        release_holder.wait()
        return original(*args, **kwargs)

    def arm_again():
        try:
            results.append(mod.write_keepwarm_arm_record("abc123", str(transcript)))
        except Exception as exc:
            errors.append(exc)

    # A start barrier plus a fixed sleep does not guarantee contention: the
    # scheduler can delay one racer until the other has released the lease.
    # Start the contender only once the holder is inside its critical section,
    # and keep the holder there until the contender has actually returned.
    holder = threading.Thread(target=arm_again, daemon=True)
    contender = threading.Thread(target=arm_again, daemon=True)
    mod.load_keepwarm_records = held_load
    try:
        holder.start()
        assert holder_scanning.wait(timeout=5), "holder never reached the locked sidecar scan"
        contender.start()
        contender.join(timeout=5)
        assert not contender.is_alive(), "contender did not respect the acquisition timeout"
        assert results == [None], "contender must skip while the holder still has the lease"
    finally:
        release_holder.set()
        for thread in (holder, contender):
            if thread.ident is not None:
                thread.join(timeout=5)
        mod.load_keepwarm_records = original
    assert not holder.is_alive(), "holder did not finish after being released"
    _assert_race_invariants(mod, results, errors)


def test_truncated_rotated_transcript_rearms(m, tmp_path):
    """Rotation/truncation is a NEW snapshot: the dedup must not swallow it.

    A rotated transcript shares the session id but not the snapshot; refusing
    to re-arm would leave the fresh pause unwatched.
    """
    mod, _ = m
    transcript = _arm(m, tmp_path)
    # Rotate: replace with a shorter, newer transcript (different size).
    ts = datetime.fromtimestamp(time.time() - 3600, timezone.utc).isoformat()
    transcript.write_text(
        json.dumps({
            "type": "assistant",
            "timestamp": ts,
            "cwd": str(tmp_path / "project"),
            "message": {
                "model": "claude-sonnet-4-5",
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 10,
                    "cache_read_input_tokens": 18000,
                    "cache_creation": {"ephemeral_5m_input_tokens": 500},
                },
            },
        }) + "\n",
        encoding="utf-8",
    )
    newer = time.time() - 3600
    os.utime(transcript, (newer, newer))
    assert mod.write_keepwarm_arm_record("abc123", str(transcript)) is not None
    records = [r for r in _sidecar_records(mod) if r.get("session_id") == "abc123"]
    assert len(records) == 2


def test_keepwarm_tenfold_threshold_and_three_ping_cap(m):
    mod, _ = m
    record = {'ts': 100000, 'last_turn_ts': 100000, 'prefix_proxy': 10000,
              'model': 'claude-sonnet-4-5', 'ttl_kind': '1h', 'pings_fired': []}
    assert mod.keepwarm_policy_decision(record, 104000)['reason'] == 'below-tenfold'
    record['prefix_proxy'] = 200000
    history = {'gap_sessions': [[1000 + 4000 * i for i in range(6)] for _ in range(50)]}
    assert mod.keepwarm_policy_decision(record, 103400, history_stats=history)['action'] == 'ping'
    record['pings_fired'] = [1100, 1200, 1300]
    assert mod.keepwarm_policy_decision(record, 104000)['reason'] == 'hard-cap'
    record['pings_fired'] = []
    assert mod.keepwarm_policy_decision(record, 110800)['reason'] == 'hard-cap'


def test_keepwarm_net_is_estimated_and_deducts_ping_cost(m):
    mod, _ = m
    from _measure import savings_story
    record = {'session_id': 'abc123', 'ts': 1000, 'model': 'claude-sonnet-4-5',
              'prefix_proxy': 200000, 'ttl_kind': '1h', 'pings_fired': [2000, 3000]}
    rows = [{'session_id': 'abc123', 'ts': 2010, 'status': 'ok', 'cost_usd': 0.03},
            {'session_id': 'abc123', 'ts': 3010, 'status': 'ok', 'cost_usd': 0.04}]
    net = savings_story.keepwarm_pause_net(mod, record, 4000, rows)
    key = mod._keepwarm_model_norm(record['model'])
    expected = 200000 * (mod._KEEPWARM_INPUT_USD_PER_TOKEN[key] * 2 - mod._KEEPWARM_CACHE_READ_USD_PER_TOKEN[key]) - 0.07
    assert net == pytest.approx(expected)
    assert savings_story.keepwarm_pause_net(mod, record, 4000, rows[:1]) is None
    _, _, measured, _, popped, _ = mod._net_realized_savings([('keepwarm_estimated', 1, 0, net)], {})
    assert measured == 0 and popped['keepwarm_estimated']['cost_saved_usd'] == pytest.approx(net, abs=0.0001)
    result = mod._action_savings_components({'keepwarm_estimated': popped['keepwarm_estimated']})
    assert result['estimates_usd']['keepwarm_estimated'] == pytest.approx(net, abs=0.0001)


def test_default_gate_subscription_and_kill_switch(m, monkeypatch):
    import sqlite3
    mod, _ = m
    def empty_accounting(timeout):
        conn = sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE savings_events (pause_key TEXT, event_type TEXT)')
        return conn
    monkeypatch.setattr(mod, '_open_trends_db_readonly', empty_accounting)
    monkeypatch.setattr(mod, 'keepwarm_consent', lambda: 'unasked')
    monkeypatch.setattr(mod, '_keepwarm_tripwire_off', lambda: False)
    monkeypatch.setattr(mod, 'keepwarm_billing_mode', lambda **kw: 'subscription')
    assert mod.keepwarm_gate(env={}) == (False, 'live-resume-unverified')
    assert mod.keepwarm_gate(env={'TOKEN_OPTIMIZER_KEEPWARM_DISABLE': '1'}) == (False, 'kill-switch')
    monkeypatch.setattr(mod, 'keepwarm_consent', lambda: 'declined')
    assert mod.keepwarm_gate(env={}) == (False, 'consent-declined')


def test_compacted_or_ended_session_cannot_be_pinged(m, tmp_path):
    from _measure import keepwarm_safety
    mod, _ = m
    transcript = _arm(m, tmp_path)
    record = mod.load_keepwarm_records()[0]
    transcript.write_text('shortened')
    assert mod.classify_keepwarm_record(record) == 'resumed'
    assert mod._keepwarm_fire_ping(record, runner=lambda *a, **kw: None)['reason'] == 'live-resume-unverified'
    keepwarm_safety.mark_ended(mod.SNAPSHOT_DIR, record['session_id'])
    assert mod.classify_keepwarm_record(record) == 'gone'


def test_owner_identity_requires_matching_start_time():
    from _measure import keepwarm_safety
    info = {10: {'ppid': 8, 'start': 'early', 'command': 'bash'},
            8: {'ppid': 1, 'start': 'original', 'command': 'claude'}}
    owner = keepwarm_safety.find_owner(10, lookup=info.get, eligibility=lambda pid: True)
    assert owner == {'pid': 8, 'start': 'original'}
    assert keepwarm_safety.owner_alive(owner, lookup=info.get, eligibility=lambda pid: True)
    info[8]['start'] = 'reused'
    assert not keepwarm_safety.owner_alive(owner, lookup=info.get, eligibility=lambda pid: True)


def test_ping_policy_stops_when_cache_expired_or_previous_ping_failed(m):
    mod, _ = m
    record = {'ts': 1000, 'last_turn_ts': 1000, 'prefix_proxy': 200000,
              'model': 'claude-sonnet-4-5', 'ttl_kind': '1h', 'pings_fired': []}
    assert mod.keepwarm_policy_decision(record, 4600)['reason'] == 'cache-expired'
    record['ping_failed'] = True
    assert mod.keepwarm_policy_decision(record, 4400)['reason'] == 'previous-ping-failed'


def test_compaction_boundary_after_arm_invalidates_booking(tmp_path):
    from _measure import keepwarm_safety
    path = tmp_path / 'transcript.jsonl'
    path.write_bytes(b'old\n')
    record = {'transcript_path': str(path), 'byte_size': path.stat().st_size}
    with path.open('ab') as stream:
        stream.write(b'{"type":"system","subtype":"compact_boundary"}\n')
    assert keepwarm_safety.compacted_since(record)


def test_ledger_append_persists_synthetic_row_without_lease_cleanup(m, monkeypatch):
    from contextlib import nullcontext
    mod, _ = m
    monkeypatch.setattr(mod, '_keepwarm_lock', lambda: nullcontext(True))
    row = {'session_id': 'synthetic', 'status': 'ok', 'cost_usd': 0.05}
    assert mod._keepwarm_ledger_append(row)
    assert mod._keepwarm_load_ledger_rows() == [row]


@pytest.mark.parametrize("parent_gone", [False, True])
def test_warm_resume_books_net_estimate_once(m, tmp_path, monkeypatch, parent_gone):
    import sqlite3
    from contextlib import nullcontext
    mod, _ = m
    monkeypatch.setattr(mod, '_keepwarm_lock', lambda: nullcontext(True))
    transcript = _arm(m, tmp_path)
    rec = mod.load_keepwarm_records()[0]
    rec['prefix_proxy'] = 200000
    rec['ttl_kind'] = '1h'
    rec['pings_fired'] = [rec['ts'] + 1]
    assert mod._keepwarm_ledger_append({'session_id': 'abc123', 'ts': rec['ts'] + 2,
                                       'status': 'ok', 'cost_usd': 0.05})
    with transcript.open('a') as stream:
        stream.write('{"type":"user","message":"resume"}\n')
    if parent_gone:
        monkeypatch.setattr(mod, 'classify_keepwarm_record', lambda *args, **kwargs: 'gone')
    stamp = datetime.fromtimestamp(rec['ts'] + 3, timezone.utc).isoformat()
    parse = lambda path: [{'timestamp': stamp, 'cache_read': 200000, 'cache_creation': 0}]
    result = mod.keepwarm_detect_realized(now=rec['ts'] + 5, records=[rec], parse_fn=parse)
    assert result['realized'] == 1
    conn = sqlite3.connect(mod.TRENDS_DB)
    row = conn.execute("SELECT event_type, tokens_saved, cost_saved_usd FROM savings_events WHERE pause_key IS NOT NULL").fetchone()
    conn.close()
    from _measure import savings_story
    assert row[0:2] == ('keepwarm_estimated', 0)
    assert row[2] == pytest.approx(savings_story.keepwarm_reload_value(mod, rec) - 0.05)
    assert mod.keepwarm_detect_realized(now=rec['ts'] + 6, records=[rec], parse_fn=parse)['realized'] == 0


def test_scheduler_installs_for_new_enabled_user_without_real_launchd(m, monkeypatch):
    mod, _ = m
    monkeypatch.setattr(mod, '_remote_lean_profile', lambda: False)
    monkeypatch.setattr(mod, '_keepwarm_read_scheduler_marker', lambda: None)
    monkeypatch.setattr(mod, '_normalized_platform', lambda: 'Darwin')
    calls = []
    monkeypatch.setattr(mod, '_keepwarm_scheduler_install_macos', lambda quiet: (calls.append(quiet) is None, 'ok'))
    assert mod.keepwarm_scheduler_repair(gate=lambda: (True, 'ok')) == 'repaired'
    assert calls == [True]


def test_attempt_count_recovers_from_ledger_after_crash():
    from _measure.keepwarm_safety import reconcile_attempts
    record = {'session_id': 's', 'ts': 100, 'prefix_proxy': 10000, 'pings_fired': []}
    rows = [{'session_id': 's', 'ts': 101, 'status': 'firing'},
            {'session_id': 's', 'ts': 102, 'status': 'ok', 'cache_read': 10000},
            {'session_id': 's', 'ts': 103, 'status': 'firing'}]
    assert reconcile_attempts(record, rows)
    assert record['pings_fired'] == [101, 103]
    assert record['ping_failed']


def test_real_ping_fails_closed_until_live_resume_is_proven_safe(m, monkeypatch):
    mod, _ = m
    monkeypatch.setattr(mod, '_keepwarm_llm_ping_disabled', lambda: False)
    result = mod._keepwarm_fire_ping({'session_id': 's', 'transcript_path': '/unused',
                                     'model': 'claude-sonnet-4-5'})
    assert result['fired'] is False
    assert result['reason'] == 'live-resume-unverified'


def test_keepwarm_requires_history_and_positive_expected_value(m):
    mod, _ = m
    record = {'ts': 100000, 'last_turn_ts': 100000, 'prefix_proxy': 200000,
              'model': 'claude-sonnet-4-5', 'ttl_kind': '1h', 'pings_fired': []}
    assert mod.keepwarm_policy_decision(record, 103400)['reason'] == 'insufficient-return-value'
    histories = [[1000 + 4000 * i for i in range(6)] for _ in range(50)]
    decision = mod.keepwarm_policy_decision(record, 103400, history_stats={'gap_sessions': histories})
    assert decision['action'] == 'ping'
    abandoned = [[1000] for _ in range(50)]
    assert mod.keepwarm_policy_decision(record, 103400, history_stats={'gap_sessions': abandoned})['reason'] == 'insufficient-return-value'


def test_replay_charges_abandoned_tail_and_stops_at_cutoff(m):
    mod, _ = m
    pause = {'pause_start_ts': 100000, 'prefix_proxy': 200000,
             'model': 'claude-sonnet-4-5', 'ttl_kind': '1h'}
    history = [[1000 + 4000 * i for i in range(6)] for _ in range(50)]
    count, cost, gross = mod._keepwarm_replay_pause(
        dict(pause, gap_sessions=history), 'probe', 103500, is_tail=True)
    assert count == 1
    assert cost > 0
    assert gross == 0


def test_gate_reports_negative_trailing_net(m, monkeypatch):
    mod, _ = m
    from _measure import keepwarm_economics
    monkeypatch.setattr(mod, 'keepwarm_consent', lambda: 'enabled')
    monkeypatch.setattr(mod, '_keepwarm_tripwire_off', lambda: False)
    monkeypatch.setattr(keepwarm_economics, 'verified_summary', lambda facade, days, now: {'pings': 1, 'net_usd': -0.12})
    assert mod.keepwarm_gate(env={}) == (False, 'negative-net')


def test_spend_ledger_counts_each_attempt_including_unresolved(m):
    mod, _ = m
    rows = [
        {'session_id': 's', 'ts': 100, 'status': 'firing', 'est_cost_usd': 0.05},
        {'session_id': 's', 'ts': 101, 'status': 'firing', 'est_cost_usd': 0.06},
        {'session_id': 's', 'ts': 102, 'status': 'ok', 'cost_usd': 0.04},
    ]
    result = mod.keepwarm_spend_summary(days=None, now=103, rows=rows)
    assert result['pings'] == 2
    assert result['spend_usd'] == 0.10
    assert result['net_usd'] == -0.10


def test_paused_fire_never_invokes_injected_runner(m):
    mod, _ = m
    calls = []
    for _ in range(2):
        result = mod._keepwarm_fire_ping({'session_id': 'synthetic'}, runner=lambda *a, **kw: calls.append(a))
        assert not result['fired'] and result['reason'] == 'live-resume-unverified'
    assert not calls


def test_guarded_fork_is_disconnected_from_hooks_and_scheduler():
    scripts = REPO / 'skills' / 'token-optimizer' / 'scripts'
    references = [p for p in scripts.rglob('*.py') if 'run_guarded' in p.read_text(encoding='utf-8')]
    assert references == []


def test_linux_scheduler_dispatches_only_after_gate(m, monkeypatch):
    from contextlib import nullcontext
    from _measure import keepwarm_linux
    mod, _ = m
    calls = []
    monkeypatch.setattr(mod, '_normalized_platform', lambda: 'Linux')
    monkeypatch.setattr(mod, '_keepwarm_scheduler_install_lock', lambda: nullcontext(True))
    monkeypatch.setattr(keepwarm_linux, 'install', lambda facade: (calls.append('install') or True, 'ok'))
    assert not mod.keepwarm_scheduler_install(gate=lambda: (False, 'disabled'))[0]
    assert calls == []
    assert mod.keepwarm_scheduler_install(gate=lambda: (True, 'enabled'))[0]
    assert calls == ['install']


def test_safety_pause_is_exposed_even_when_accounting_is_unavailable(m, monkeypatch):
    mod, _ = m
    monkeypatch.setattr(mod, 'detect_runtime', lambda: 'claude')
    monkeypatch.setattr(mod, '_normalized_platform', lambda: 'Darwin')
    def unavailable(**kwargs):
        raise OSError('synthetic unavailable accounting')
    monkeypatch.setattr(mod, 'keepwarm_report', unavailable)
    health = mod.keepwarm_cache_health_block()
    assert health['state'] == 'paused'
    assert health['note'] == 'Keep-warm: paused while we prove it is safe and saves you money'
    feature = mod._get_v5_feature_status()['keepwarm']
    assert feature['enabled'] is False and feature['source'] == 'paused'


def test_windows_health_reports_keepwarm_unavailable(m, monkeypatch):
    mod, _ = m
    monkeypatch.setattr(mod, 'keepwarm_report', lambda **kwargs: {})
    monkeypatch.setattr(mod, 'detect_runtime', lambda: 'claude')
    monkeypatch.setattr(mod, '_normalized_platform', lambda: 'Windows')
    health = mod.keepwarm_cache_health_block(days=30)
    assert health['state'] == 'platform-gap'
    assert 'Windows' in health['note']


def test_keepwarm_settings_toggle_is_default_on_and_can_pause(m, monkeypatch):
    mod, _ = m
    assert mod.V5_FEATURES['keepwarm']['default'] is True
    monkeypatch.setattr(mod, 'keepwarm_consent', lambda: 'enabled')
    monkeypatch.setattr(mod, '_keepwarm_tripwire_off', lambda: False)
    monkeypatch.setattr(mod, '_is_v5_feature_enabled', lambda name: name != 'keepwarm')
    assert mod.keepwarm_gate(env={}) == (False, 'settings-disabled')


def test_keepwarm_environment_kill_switch_stops_gate_and_ping(m):
    mod, _ = m
    env = {'TOKEN_OPTIMIZER_KEEPWARM': '0'}
    assert mod.keepwarm_gate(env=env) == (False, 'kill-switch')
    assert mod._keepwarm_llm_ping_disabled(env)


@pytest.mark.parametrize('enabled', [True, False])
def test_dashboard_cannot_toggle_while_safety_paused(m, enabled):
    mod, _ = m
    assert not mod._set_v5_feature('keepwarm', enabled)
    assert mod._read_config_flag('v5_keepwarm', None) is None


def test_keepwarm_legacy_opt_out_stays_off(m, monkeypatch):
    mod, _ = m
    monkeypatch.setattr(mod, '_resolve_feature_env', lambda name: None)
    monkeypatch.setattr(mod, '_read_config_flag', lambda key, default=None: 'declined' if key == 'keepwarm_consent' else default)
    assert not mod._is_v5_feature_enabled('keepwarm')
    assert mod.keepwarm_consent() == 'declined'


def test_keepwarm_toggle_hidden_on_unsupported_hosts(m, monkeypatch):
    mod, _ = m
    monkeypatch.setattr(mod, 'detect_runtime', lambda: 'claude')
    monkeypatch.setattr(mod, '_normalized_platform', lambda: 'Windows')
    assert 'keepwarm' not in mod._get_v5_feature_status()


def test_paused_negative_net_still_books_a_return(m, monkeypatch):
    mod, _ = m
    calls = []
    monkeypatch.setattr(mod, 'keepwarm_detect_realized', lambda now: calls.append(now) or {'realized': 1})
    monkeypatch.setattr(mod, '_keepwarm_emit_tick_observability', lambda **kwargs: None)
    result = mod.keepwarm_tick(now=12345, dry_run=False, gate=lambda env: (False, 'negative-net'))
    assert result['fired'] == 0
    assert calls == [12345]


def test_finished_parent_return_is_still_detected_for_accounting(m, monkeypatch, tmp_path):
    from _measure import keepwarm_safety
    from _measure import keepwarm_economics
    mod, _ = m
    transcript = _write_transcript(tmp_path, 7200)
    stat = transcript.stat()
    record = {'session_id': 'abc123', 'ts': time.time() - 100,
              'transcript_path': str(transcript), 'transcript_mtime': stat.st_mtime,
              'byte_size': stat.st_size, 'owner': {'pid': 123, 'start': 'fixture'}}
    monkeypatch.setattr(keepwarm_safety, 'owner_alive', lambda owner: False)
    assert mod.classify_keepwarm_record(record) == 'gone'
    with transcript.open('a') as stream:
        stream.write('{"type":"user"}\n')
    assert mod.classify_keepwarm_record(record) == 'gone'
    assert keepwarm_economics.transcript_changed(record)
