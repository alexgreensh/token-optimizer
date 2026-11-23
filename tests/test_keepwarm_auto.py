"""Bounded policy, receipts, and scheduler tests. All model calls are fakes."""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_keepwarm_arm_dedup import m  # isolated measure facade fixture

from _measure import keepwarm_auto as auto, keepwarm_economics as economics, keepwarm_safety as safety


NOW = 100 * auto.DAY
MODEL = 'claude-sonnet-4-5'
SID = 'current-session'
TOKENS = 120_000
IDLE = 3300


def history(facade, *, stamp=NOW, at_risk=20, returned=10):
    payload = {'built_at': stamp, 'buckets': {
        str(bucket): {'at_risk': at_risk, 'returned': returned} for bucket in auto.BUCKETS}}
    auto._history_path(facade).write_text(json.dumps(payload), encoding='utf-8')


@pytest.fixture
def policy(tmp_path, monkeypatch):
    settings = {'keepwarm_auto': True}
    facade = SimpleNamespace(
        SNAPSHOT_DIR=tmp_path, os=SimpleNamespace(environ={}), time=SimpleNamespace(time=lambda: NOW),
        _read_config_flag=lambda key, default: settings.get(key, default),
        _write_config_flag=lambda key, value: settings.update({key: value}),
        _keepwarm_llm_ping_disabled=lambda env: env.get('TOKEN_OPTIMIZER_KEEPWARM') in auto.OFF,
        _KEEPWARM_PING_ENV='TOKEN_OPTIMIZER_KEEPWARM_PING',
        keepwarm_consent=lambda: 'unasked',
        keepwarm_tripwire_mode=lambda: 'probe-only',
        keepwarm_billing_mode=lambda **kw: 'subscription',
        _is_priced_model=lambda model: model == MODEL,
        _keepwarm_load_ledger_rows=lambda: [],
        _keepwarm_ping_cost_est=lambda tokens, model: 1.0,
        _get_model_cost=lambda model, input, output, cache_read, write, **kw: 10.0 if write else 1.0,
        lease_lock=lambda path, **kw: nullcontext(True),
    )
    monkeypatch.setattr(economics, 'verified_summary', lambda facade, days, now: {'pings': 0, 'net_usd': 0})
    history(facade)
    return facade


def decide(facade, **overrides):
    values = dict(session_id=SID, context_tokens=TOKENS, idle_seconds=IDLE,
                  model=MODEL, lifetime=3600, five_hour_pct=20, week_pct=20, now=NOW)
    return auto.decide(facade, **{**values, **overrides})


def attempt(n, *, sid=SID, ts=NOW - 60, source='claude-code-mod', auto=True, status='ok', **extra):
    return dict(session_id=sid, action_id=f'attempt-{n}', ts=ts, source=source, action='warm',
                auto=auto, status=status, **extra)


@pytest.mark.parametrize('reason,options', [
    ('off', {'lifetime': 300}),
    ('tripwire', {'lifetime': 300}),
    ('lifetime', {'lifetime': 300, 'context_tokens': 1}),
    ('small', {'context_tokens': 99_999, 'model': 'unpriced'}),
    ('unpriced', {'model': 'unpriced', 'five_hour_pct': 80}),
    ('limits', {'five_hour_pct': 80, 'week_pct': None}),
    ('meters-unknown', {'week_pct': None}),
    ('cap-pause', {}),
    ('cap-session', {}),
    ('cap-day', {}),
    ('failed-proof', {}),
    ('no-history', {}),
    ('not-worth-it', {}),
    ('due', {}),
    ('error', {}),
])
def test_ordered_decision_reasons(policy, reason, options):
    rows = []
    if reason == 'off':
        policy._write_config_flag('keepwarm_auto', False)
        policy.keepwarm_tripwire_mode = lambda: 'off'
    elif reason == 'tripwire':
        policy.keepwarm_tripwire_mode = lambda: 'off'
    elif reason == 'cap-pause':
        rows = [attempt(n, proof=False) for n in range(2)]
    elif reason == 'cap-session':
        rows = [attempt(n, ts=NOW - IDLE - 1) for n in range(4)]
        rows += [attempt(n + 4, sid='other', ts=NOW - IDLE - 1) for n in range(4)]
    elif reason == 'cap-day':
        rows = [attempt(n, sid=f'other-{n}') for n in range(8)]
    elif reason == 'failed-proof':
        rows = [attempt(0, proof=False)]
        auto._history_path(policy).unlink()
    elif reason == 'no-history':
        history(policy, at_risk=19, returned=19)
    elif reason == 'not-worth-it':
        history(policy, returned=9)
    elif reason == 'error':
        def fail():
            raise OSError('synthetic')
        policy.keepwarm_tripwire_mode = fail
    result = decide(policy, rows=rows, **options)
    assert result['reason'] == reason
    assert result['action'] == ('ping' if reason == 'due' else 'stop')


def test_caps_are_shared_rolling_and_ignore_cancelled_or_manual_attempts(policy):
    rows = [
        attempt(1, status='firing'), attempt(1, status='ok', ts=NOW - 59),
        attempt(2, source='keepwarm-headless', sid='other'),
        attempt(3, status='firing'), attempt(3, status='cancelled', ts=NOW - 59),
        attempt(4, auto=False), attempt(5, ts=NOW - auto.DAY - 1),
    ]
    assert decide(policy, rows=rows)['caps'] == {'pause_left': 1, 'session_day_left': 3, 'day_left': 6}
    rows.append(attempt(6, source='keepwarm-headless'))
    assert decide(policy, rows=rows)['reason'] == 'cap-pause'


def test_limits_and_kill_switch(policy):
    assert decide(policy, week_pct=90)['reason'] == 'limits'
    assert decide(policy, five_hour_pct=None)['reason'] == 'meters-unknown'
    policy.keepwarm_billing_mode = lambda **kw: 'api'
    assert decide(policy, five_hour_pct=None, week_pct=None)['action'] == 'ping'
    policy.os.environ['TOKEN_OPTIMIZER_KEEPWARM'] = 'OFF'.lower()
    assert decide(policy)['reason'] == 'off'


def test_net_tripwire_starts_at_five_pings(policy, monkeypatch):
    summary = {'pings': 4, 'net_usd': -0.01}
    monkeypatch.setattr(economics, 'verified_summary', lambda facade, days, at: summary)
    assert decide(policy)['action'] == 'ping'
    summary['pings'] = 5
    assert decide(policy)['reason'] == 'tripwire'


def test_margin_boundary_and_no_transcript_access(policy):
    real_open = Path.open

    def guard(path, *args, **kwargs):
        assert path.suffix != '.jsonl'
        return real_open(path, *args, **kwargs)

    with patch.object(Path, 'open', guard):
        result = decide(policy)
        assert result['p_return'] * result['avoided_usd'] == 3 * result['ping_cost_usd']
        assert result['action'] == 'ping'
        policy._keepwarm_ping_cost_est = lambda tokens, model: 1.000001
        assert decide(policy)['reason'] == 'not-worth-it'


@pytest.mark.parametrize('read,write,expected', [
    (108_000, 6_000, True), (107_999, 6_000, False), (108_000, 6_001, False),
    (108_000, None, False), (108_000, -1, False), (108_000, True, False),
])
def test_proof_usage_boundaries(read, write, expected):
    assert auto.proof_ok({'cache_read_input_tokens': read, 'cache_creation_input_tokens': write}, TOKENS) is expected


def test_history_refresh_writes_only_counts_once_daily(policy, monkeypatch):
    calls = []
    timings = [[NOW - 10_000, NOW - 6_000] for _ in range(20)]
    monkeypatch.setattr(economics, 'load_history', lambda *args: calls.append(True) or {
        'gap_sessions': economics.GapHistory(timings)})
    auto._history_path(policy).unlink()
    auto.on_savings_refresh(policy, {})
    auto.on_savings_refresh(policy, {})
    assert len(calls) == 1
    data = json.loads(auto._history_path(policy).read_text())
    assert data == {'built_at': NOW, 'buckets': {'3300': {'at_risk': 20, 'returned': 20},
                    '6900': {'at_risk': 0, 'returned': 0}, '10500': {'at_risk': 0, 'returned': 0}}}
    assert auto._history_path(policy).stat().st_mode & 0o777 == 0o600
    history(policy, stamp=NOW - 7 * auto.DAY - 1)
    assert decide(policy)['reason'] == 'no-history'


def test_prune_at_most_200_old_markers(policy):
    for index in range(207):
        prefix = 'keepwarm-owner-' if index % 2 else 'keepwarm-ended-'
        path = policy.SNAPSHOT_DIR / f'{prefix}{index}'
        path.touch()
        os.utime(path, (NOW - 15 * auto.DAY,) * 2)
    recent = policy.SNAPSHOT_DIR / 'keepwarm-ended-recent'
    recent.touch()
    os.utime(recent, (NOW - 13 * auto.DAY,) * 2)
    preserve = policy.SNAPSHOT_DIR / 'keepwarm-ended-symlink'
    preserve.symlink_to(recent)
    assert auto.prune_markers(policy, NOW) == 200
    assert len(list(policy.SNAPSHOT_DIR.glob('keepwarm-*'))) == 9
    assert recent.exists() and preserve.is_symlink()


def test_approval_requires_mod_activity_and_positive_capped_replay(policy, monkeypatch):
    policy._write_config_flag('keepwarm_auto', False)
    forecast = {'positive': False, 'net_usd': -1, 'ping_cost_usd': 2, 'avoided_writes': 1, 'pings': 3}

    def replay(*, days, automatic):
        assert days == 30 and automatic
        return forecast

    policy.keepwarm_forecast = replay
    monkeypatch.setattr(auto, '_mod_active', lambda facade, now: False)
    assert auto.plan_item(policy) is None
    monkeypatch.setattr(auto, '_mod_active', lambda facade, now: True)
    assert auto.plan_item(policy) is None
    forecast.update(positive=True, net_usd=1)
    item = auto.plan_item(policy)
    assert item['basis'].endswith('1 returns within the hour, 3 pings')
    assert item['net_usd_month'] == 1 and item['own_cost_usd_month'] == 2
    assert auto.apply_item(policy, item) == {'ok': True, 'before': False, 'after': True}
    assert auto.plan_item(policy) is None


def test_default_headless_and_explicit_kill_switches(policy):
    assert safety.headless_disabled(policy) is None
    assert decide(policy, headless=True)['action'] == 'ping'
    assert safety.headless_disabled(policy, {'TOKEN_OPTIMIZER_KEEPWARM_HEADLESS': 'NO'}) == 'settings-disabled'
    assert safety.headless_disabled(policy, {'TOKEN_OPTIMIZER_KEEPWARM': '0'}) == 'kill-switch'
    assert safety.headless_disabled(policy, {'TOKEN_OPTIMIZER_KEEPWARM_PING': '1'}) == 'ping-process'
    policy.keepwarm_consent = lambda: 'declined'
    assert safety.headless_disabled(policy) == 'consent-declined'


def ready_install(m, monkeypatch):
    facade, tmp = m
    now = time.time()
    facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    conn = facade._init_trends_db()
    conn.close()
    history(facade, stamp=now, at_risk=30, returned=30)
    monkeypatch.setattr(facade, '_keepwarm_resolve_claude_bin', lambda: '/fixture/claude')
    monkeypatch.setattr(facade, '_keepwarm_extract_cwd', lambda path: str(tmp))
    monkeypatch.setattr(facade, 'classify_keepwarm_record', lambda *a, **kw: 'still_paused')
    monkeypatch.setattr(facade, '_keepwarm_read_meters', lambda **kw: {
        'age_s': 3300, 'five_hour_pct': 20, 'seven_day_pct': 30})
    record = {'session_id': SID, 'model': MODEL, 'prefix_proxy': TOKENS, 'ttl_kind': '1h',
              'last_turn_ts': now - IDLE - 10, 'ts': now - IDLE, 'owner': {'pid': 123},
              'transcript_path': str(tmp / (SID + '.jsonl')), 'transcript_mtime': now - IDLE,
              'byte_size': 100}
    assert facade._keepwarm_append_line(facade._keepwarm_sidecar_path(), json.dumps(record) + '\n')
    return facade, now, record


@pytest.mark.parametrize('read,write,proof', [(119_000, 1_000, True), (70, 119_930, False)])
def test_scheduler_proves_receipt_charges_once_and_latches_failure(m, monkeypatch, read, write, proof):
    facade, now, record = ready_install(m, monkeypatch)
    calls = []

    def runner(argv, cwd, env, timeout):
        calls.append(argv)
        assert argv[:5] == ['/fixture/claude', '--resume', SID, '--fork-session', '-p']
        assert '--no-session-persistence' in argv and env['TOKEN_OPTIMIZER_KEEPWARM_PING'] == '1'
        assert json.loads(argv[argv.index('--settings') + 1])['disableAllHooks']
        assert facade._keepwarm_load_ledger_rows()[0]['status'] == 'firing'
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            'total_cost_usd': .12, 'usage': {'cache_read_input_tokens': read,
                                             'cache_creation_input_tokens': write}}))

    dry = facade.keepwarm_tick(now=now, dry_run=True, runner=runner)
    assert dry['decisions'][0]['action'] == 'ping' and not calls
    tick = facade.keepwarm_tick(now=now, runner=runner)
    assert tick['fired'] == 1 and len(calls) == 1
    rows = facade._keepwarm_load_ledger_rows()
    assert [row['status'] for row in rows] == ['firing', 'ok' if proof else 'error']
    assert rows[-1]['proof'] is proof and rows[-1]['cost_usd'] == .12
    assert bool(facade.load_keepwarm_records()[-1].get('pings_fired')) is proof
    assert facade.keepwarm_spend_summary(rows=rows)['net_usd'] == -.12
    assert (safety.headless_disabled(facade) == 'headless-proof') is (not proof)
    assert auto.health_block(facade)['proof_failed_7d'] == int(not proof)
    estimate = facade._get_merged_savings(days=7)['keepwarm_estimated']
    assert estimate['label'] == 'Keep-warm net'
    assert estimate['cost_saved_usd'] == -.12
    if not proof:
        assert not safety.fire_headless(facade, record, now=now, runner=runner)['fired']
        assert len(calls) == 1


def test_headless_missing_usage_is_a_priced_failure(m, monkeypatch):
    facade, now, record = ready_install(m, monkeypatch)
    result = safety.fire_headless(facade, record, now=now, runner=lambda *a: SimpleNamespace(
        returncode=0, stdout='{"usage":{"cache_read_input_tokens":120000}}'))
    assert result['fired'] and result['proof'] is False
    assert result['cost_usd'] > 0
    assert safety.headless_disabled(facade) == 'headless-proof'


def test_status_bar_keeps_signed_amount_even_below_headline_threshold(policy):
    headline = {'display': None}
    assert auto.status_headline(policy, headline, {'events': 1, 'cost_saved_usd': -.12}) is None
    assert headline['display']['short'] == 'Keep-warm -$0.12 est.'
    safety._headless_latch(policy).touch()
    assert auto.status_headline(policy, {'display': None}, None) == auto.HEADLESS_STOP


def test_forecast_replays_global_day_cap(policy):
    # Small historical turns teach the return odds but cannot trigger a ping.
    start = NOW - 2 * auto.DAY
    small = [{'timestamp': t, 'cache_creation_1h': 10, 'model': MODEL, 'cache_read': 500}
             for t in (start, start + 4000)]
    big = [
        {'timestamp': NOW - 4200, 'cache_creation_1h': 10, 'cache_read': TOKENS, 'model': MODEL},
        {'timestamp': NOW - 200, 'cache_creation_1h': 10, 'cache_read': 0, 'model': MODEL},
    ]
    files = [f'train-{n}' for n in range(30)] + [f'candidate-{n}' for n in range(12)]
    policy._keepwarm_iso_to_epoch = lambda value: value
    policy._turn_prefix_cached = lambda turn: turn['cache_read']
    result = auto.capped_forecast(policy, days=30, now=NOW, files=files,
                                  parse_fn=lambda fp: small if fp.startswith('train-') else big)
    assert result['pings'] == result['avoided_writes'] == 8
    assert result['net_usd'] == 8 * (9 - 1)


def test_resume_during_headless_reservation_cancels_without_spend(m, monkeypatch):
    facade, now, record = ready_install(m, monkeypatch)
    live = iter((True, False))

    def must_not_run(*args):
        pytest.fail('A resumed session reached the model runner')

    result = safety.fire_headless(facade, record, now=now, live=lambda: next(live), runner=must_not_run)
    assert result == {'fired': False, 'reason': 'session-changed'}
    rows = facade._keepwarm_load_ledger_rows()
    assert [row['status'] for row in rows] == ['firing', 'cancelled']
    assert facade.keepwarm_spend_summary(rows=rows)['pings'] == 0
    assert safety.headless_disabled(facade) is None


def test_dashboard_and_limits_expose_same_signed_net(policy):
    import subprocess
    root = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/assets'
    data = {'runway': {}, 'cache_health': {'keepwarm': {}, 'keepwarm_auto': {
        'state': 'on', 'headless_state': 'stopped', 'headless_reason': 'headless-proof',
        'pings_7d': 1, 'proof_failed_7d': 1, 'net_usd_7d': -.12}}}
    js = '\n'.join([(root / 'keepwarm_health.js').read_text(), (root / 'savings_story.js').read_text(),
                    'const data = ' + json.dumps(data),
                    'console.log(JSON.stringify([keepwarmHealthHtml(data), savingsLimitsHtml(data)]))'])
    health, limits = json.loads(subprocess.run(['node', '-e', js], check=True, capture_output=True, text=True).stdout)
    assert 'Automatic keep-warm is on, inside open sessions only.' in health
    assert 'warm-up failed the cache proof' in health
    assert all('&minus;$0.12 estimated at API rates' in panel for panel in (health, limits))


def test_scheduler_keeps_proven_desktop_arm_until_the_return(m):
    facade, tmp = m
    facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = tmp / (SID + '.jsonl')
    path.write_text('{}\n', encoding='utf-8')
    original = path.stat()
    now = time.time()
    record = {'session_id': SID, 'transcript_path': str(path), 'ts': now - 15, 'owner': None,
              'transcript_mtime': original.st_mtime, 'byte_size': original.st_size,
              'auto_warm': True, 'pings_fired': [now - 10]}
    assert facade._keepwarm_append_line(facade._keepwarm_sidecar_path(), json.dumps(record) + '\n')
    assert facade.compact_keepwarm_sidecar(now=now) == (1, 0)
    with path.open('a') as stream:
        stream.write('{}\n')
    assert facade.classify_keepwarm_record(record, now=now) == 'resumed'


@pytest.mark.parametrize('passed', [True, False])
def test_in_session_reservation_checks_proof_before_tracking_a_return(m, monkeypatch, passed):
    from _measure import mod_action_booking

    facade, tmp = m
    now = time.time()
    facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    history(facade, stamp=now, at_risk=30, returned=30)
    facade._write_config_flag('keepwarm_auto', True)
    conn = facade._init_trends_db()
    conn.close()
    path = tmp / (SID + '.jsonl')
    assistant = {'type': 'assistant', 'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now - IDLE)),
                 'message': {'model': MODEL, 'usage': {
                     'input_tokens': 100, 'output_tokens': 20, 'cache_read_input_tokens': TOKENS - 1000,
                     'cache_creation_input_tokens': 1000, 'cache_creation': {'ephemeral_1h_input_tokens': 1000}}}}
    path.write_text(json.dumps(assistant) + '\n', encoding='utf-8')
    snapshot = path.stat()
    event = {'session_id': SID, 'action_id': 'band-ping-1', 'timestamp': now,
             'transcript_path': str(path), 'byte_size': snapshot.st_size,
             'transcript_mtime': snapshot.st_mtime, 'context_tokens': TOKENS,
             'five_hour_pct': 20, 'week_pct': 30, 'auto': True}
    invalid = mod_action_booking.record(facade, {**event, 'action': 'warm-prepare', 'proof': 1})
    assert invalid['reason'] == 'invalid-auto-proof'
    assert mod_action_booking.record(facade, {**event, 'action': 'warm-prepare'})['ok']
    usage = {'cache_read_input_tokens': 119_000 if passed else 5,
             'cache_creation_input_tokens': 1000 if passed else 119_995,
             'input_tokens': 100, 'output_tokens': 31}
    outcome = mod_action_booking.record(facade, {**event, 'action': 'warm', 'proof': True, 'usage': usage})
    assert outcome['ok']
    assert bool(facade.load_keepwarm_records()) is passed
    assert facade._keepwarm_load_ledger_rows()[-1]['proof'] is passed
    assert facade.keepwarm_spend_summary()['pings'] == 1
