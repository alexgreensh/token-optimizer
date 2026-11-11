"""W5: setup savings measured as a step in the first-call series. Synthetic transcripts only."""
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/token-optimizer/scripts'))
from _measure import setup_step, structural_ledger as L  # noqa: E402

T0 = datetime(2030, 1, 1, tzinfo=timezone.utc)
RATE = 0.30 / 1e6          # a synthetic cache-read price per token
JITTER = (0, 150, -120, 80, -60, 200, -180, 40, -20, 110)


def sess(i, prefix, first_user=100, calls=50, model='m1', components=True):
    total = prefix + first_user + JITTER[i % len(JITTER)]
    s = {'ts_epoch': (T0 + timedelta(hours=i)).timestamp(), 'total': total, 'api_calls': calls, 'model': model,
         'components': {'first_user': first_user} if components else {}}
    return s


def series(before, after, n_before=25, n_after=25, **kw):
    """Sessions 0..n_before-1 at one prefix, then n_after at another; the trim sits between them."""
    rows = [sess(i, before, **kw) for i in range(n_before)]
    rows += [sess(n_before + i, after, **kw) for i in range(n_after)]
    trim = (T0 + timedelta(hours=n_before, minutes=-30)).timestamp()
    return rows, trim


def rate(model):
    return RATE


# ---- the written gate -------------------------------------------------------

def test_known_60k_to_45k_step_is_reported_within_two_percent():
    rows, trim = series(60_000, 45_000)
    r = setup_step.measure_step(rows, trim, rate_fn=rate)
    assert r['status'] == 'measured' and r['tier'] == 'measured'
    assert abs(r['drop_tokens'] - 15_000) <= 0.02 * 15_000
    assert abs(r['before_median'] - 60_000) <= 300 and abs(r['after_median'] - 45_000) <= 300


def test_no_step_reports_zero():
    rows, trim = series(60_000, 60_000)
    r = setup_step.measure_step(rows, trim, rate_fn=rate)
    assert r['status'] == 'no_step' and r['tier'] == 'estimated'
    assert (r['drop_tokens'], r['calls_since'], r['tokens_saved'], r['usd']) == (0, 0, 0, 0.0)


def test_a_shorter_first_message_alone_is_not_credited():
    # The first call got 15K smaller only because the first user message did; the prefix is unchanged.
    rows = [sess(i, 50_000, first_user=15_000) for i in range(25)] + [sess(25 + i, 50_000, first_user=100) for i in range(25)]
    assert abs(rows[0]['total'] - rows[-1]['total']) > 14_000      # the naive series does show a drop
    r = setup_step.measure_step(rows, (T0 + timedelta(hours=24, minutes=30)).timestamp(), rate_fn=rate)
    assert r['status'] == 'no_step' and r['usd'] == 0.0 and r['tokens_saved'] == 0


def test_a_longer_first_message_does_not_hide_a_real_step():
    rows = [sess(i, 60_000, first_user=100) for i in range(25)] + [sess(25 + i, 45_000, first_user=15_000) for i in range(25)]
    assert abs(rows[0]['total'] - rows[-1]['total']) < 300         # the naive series shows nothing
    r = setup_step.measure_step(rows, (T0 + timedelta(hours=24, minutes=30)).timestamp(), rate_fn=rate)
    assert r['status'] == 'measured' and abs(r['drop_tokens'] - 15_000) <= 300


def test_unsizable_first_messages_cannot_be_measured():
    rows, trim = series(60_000, 45_000, components=False)
    r = setup_step.measure_step(rows, trim, rate_fn=rate)
    assert r['status'] == 'cannot_measure' and r['usd'] == 0.0 and r['drop_tokens'] == 0
    assert 'first message' in r['reason']


# ---- pricing and attribution ------------------------------------------------

def test_value_is_drop_times_calls_since_at_each_calls_cache_read_rate():
    rows, trim = series(60_000, 45_000, n_after=30)
    for s in rows[25:]:
        s['api_calls'] = 40
    for s in rows[:25]:
        s['api_calls'] = 999                       # earlier sessions never count: no install-time credit
    rows[30]['model'] = 'pricey'
    prices = {'m1': 0.30 / 1e6, 'pricey': 1.50 / 1e6}
    r = setup_step.measure_step(rows, trim, rate_fn=lambda m: prices[m])
    drop = r['drop_tokens']
    assert r['calls_since'] == 30 * 40 and r['tokens_saved'] == drop * 30 * 40
    expect = drop * (29 * 40 * prices['m1'] + 40 * prices['pricey'])
    assert abs(r['usd'] - round(expect, 4)) < 1e-3
    assert r['unpriced_calls'] == 0


def test_unpriced_calls_are_counted_not_guessed():
    rows, trim = series(60_000, 45_000)
    r = setup_step.measure_step(rows, trim, rate_fn=lambda m: None)
    assert r['status'] == 'measured' and r['usd'] == 0.0 and r['unpriced_calls'] == r['calls_since'] > 0


def test_credit_stops_when_the_trim_is_closed():
    rows, trim = series(60_000, 45_000)
    closed = (T0 + timedelta(hours=25 + 5, minutes=-1)).timestamp()      # five after-sessions carried it
    r = setup_step.measure_step(rows, trim, closed_epoch=closed, rate_fn=rate)
    assert r['calls_since'] == 5 * 50 and r['status'] == 'measured'


def test_only_the_twenty_sessions_nearest_the_trim_are_compared():
    old = [sess(i, 80_000) for i in range(30)]                    # long before: must not leak in
    near = [sess(30 + i, 60_000) for i in range(20)]
    later = [sess(50 + i, 45_000) for i in range(20)]
    trim = (T0 + timedelta(hours=49, minutes=30)).timestamp()
    r = setup_step.measure_step(old + near + later, trim, rate_fn=rate)
    assert abs(r['drop_tokens'] - 15_000) <= 300 and (r['n_before'], r['n_after']) == (20, 20)


def test_too_few_sessions_since_is_pending_not_zero_evidence():
    rows, trim = series(60_000, 45_000, n_after=10)
    r = setup_step.measure_step(rows, trim, rate_fn=rate)
    assert r['status'] == 'pending' and r['usd'] == 0.0 and r['n_after'] == 10


def test_another_trim_inside_the_span_cannot_be_attributed():
    rows, trim = series(60_000, 45_000)
    other = (T0 + timedelta(hours=35)).timestamp()
    r = setup_step.measure_step(rows, trim, other_trim_epochs=[other], rate_fn=rate)
    assert r['status'] == 'overlap' and r['usd'] == 0.0
    far = (T0 + timedelta(hours=500)).timestamp()
    assert setup_step.measure_step(rows, trim, other_trim_epochs=[far], rate_fn=rate)['status'] == 'measured'


def test_noise_larger_than_the_drop_is_not_a_step():
    # A 2.5% drop (passes the 1% floor) inside +-6K of spread: not distinguishable from chance.
    wide = (6_000, -6_000, 5_000, -5_000, 4_000, -4_000, 6_500, -6_500, 3_000, -3_000)
    rows = [dict(sess(i, 60_000), total=60_000 + wide[i % 10]) for i in range(25)]
    rows += [dict(sess(25 + i, 58_500), total=58_500 + wide[(i + 3) % 10]) for i in range(25)]
    r = setup_step.measure_step(rows, (T0 + timedelta(hours=24, minutes=30)).timestamp(), rate_fn=rate)
    assert r['status'] == 'no_step' and r['usd'] == 0.0 and r['before_median'] - r['after_median'] >= 600


def test_an_increase_is_never_a_saving():
    rows, trim = series(45_000, 60_000)
    r = setup_step.measure_step(rows, trim, rate_fn=rate)
    assert r['status'] == 'no_step' and r['usd'] == 0.0 and r['tokens_saved'] == 0


def test_garbage_input_never_raises():
    assert setup_step.measure_step([], 0)['status'] == 'cannot_measure'
    assert setup_step.measure_step([{'ts_epoch': None}], 5)['status'] == 'cannot_measure'
    assert setup_step.measure_step([{'ts_epoch': 1, 'total': 'x'}] * 3, 5)['status'] == 'cannot_measure'


def test_trims_at_the_same_instant_are_one_event_and_summary_adds_only_measured():
    rows, trim = series(60_000, 45_000)
    ts = (T0 + timedelta(hours=25, minutes=-30)).timestamp()
    trims = [{'ts_epoch': ts, 'tokens': 4000}, {'ts_epoch': ts, 'tokens': 3000},
             {'ts_epoch': (T0 + timedelta(hours=900)).timestamp(), 'tokens': 500}]
    steps = setup_step.measure_all(rows, trims, rate)
    assert [s['trims'] for s in steps] == [2, 1]
    assert steps[1]['status'] in ('pending', 'cannot_measure')
    total = setup_step.summarize(steps)
    assert total['measured_trims'] == 1 and total['measured_usd'] == steps[0]['usd'] > 0
    assert steps[0]['estimated_tokens'] == 7000


# ---- end to end on transcripts ----------------------------------------------

def _transcript(path, ts, total, first_user_chars, model='m1'):
    rows = [{'type': 'user', 'message': {'role': 'user', 'content': 'x' * first_user_chars}},
            {'type': 'assistant', 'timestamp': ts, 'message': {'model': model, 'usage': {
                'input_tokens': 10, 'cache_read_input_tokens': total - 10, 'cache_creation_input_tokens': 0}}},
            {'type': 'assistant', 'timestamp': ts, 'message': {'usage': {'input_tokens': 999999}}}]
    path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')


def test_collect_reads_ledger_and_transcripts_and_prices_them(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    db = data / 'trends.db'
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE session_log(jsonl_path TEXT, api_calls INTEGER, is_sidechain INTEGER DEFAULT 0)')
    trim_ts = (T0 + timedelta(hours=24, minutes=30)).isoformat()
    L.record_trim(data, L.make_trim('skills', 'old-skill', 'global', 20_000, 5_000, trim_ts, 'audit_action', evidence='e'))
    for i in range(50):
        p = tmp_path / f't{i}.jsonl'
        _transcript(p, (T0 + timedelta(hours=i)).isoformat(), (60_000 if i < 25 else 45_000) + 400, 400)
        conn.execute('INSERT INTO session_log VALUES (?,?,0)', (str(p), 20))
    conn.commit()
    conn.close()
    out = setup_step.collect(db, data, card_fn=lambda model, tokens: {'cache_read': 0.30})
    assert out['available'] and out['measured_trims'] == 1
    step = out['steps'][0]
    assert step['status'] == 'measured' and abs(step['drop_tokens'] - 15_000) <= 0.02 * 15_000
    assert step['calls_since'] == 25 * 20
    assert abs(step['usd'] - step['drop_tokens'] * 25 * 20 * 0.30 / 1e6) < 1e-3
    assert str(tmp_path) not in json.dumps(out)                         # no paths leave the module


def test_collect_without_a_ledger_or_trims_is_honest(tmp_path):
    assert setup_step.collect(tmp_path / 'none.db', tmp_path)['available'] is False
    db = tmp_path / 'trends.db'
    sqlite3.connect(db).close()
    r = setup_step.collect(db, tmp_path)
    assert r['available'] and r['steps'] == [] and r['measured_usd'] == 0.0


def test_observer_stops_at_the_first_call_and_keeps_time_and_model():
    rows = [{'type': 'user', 'message': {'role': 'user', 'content': 'hello'}},
            {'type': 'assistant', 'timestamp': '2030-01-01T00:00:00Z',
             'message': {'model': 'm9', 'usage': {'input_tokens': 5, 'cache_read_input_tokens': 20}}}]

    def endless():
        yield from rows
        raise AssertionError('read past the first call')

    obs = setup_step.observe_step(endless(), len)
    assert obs['total'] == 25 and obs['ts'] == '2030-01-01T00:00:00Z' and obs['model'] == 'm9'
    assert setup_step.prefix_of(obs) == 20
    assert setup_step.observe_step(iter([{'type': 'assistant', 'message': {'usage': {}}}]), len) is None


# ---- card and wiring --------------------------------------------------------

def test_card_words_each_status_plainly(tmp_path):
    import os
    import subprocess
    node = shutil.which('node') or ''
    if not node:
        pytest.skip('node not installed')
    js = (ROOT / 'skills/token-optimizer/assets/first_call_context.js').read_text(encoding='utf-8')
    script = js + '''
    var month = {month: '2030-01', median: 60000, p25: 59000, p75: 61000, n: 9, components: {}};
    var base = {months: [month], trim_dates: []};
    var none = firstCallContextHtml(base);
    if (none.indexOf('Measured against') >= 0) throw new Error('no steps must add nothing');
    var steps = [
      {status: 'measured', trim_epoch: 1893456000, before_median: 60000, after_median: 45000, drop_tokens: 15000, calls_since: 1250, usd: 5.625, window: 20, n_after: 20},
      {status: 'no_step', trim_epoch: 1893456000, window: 20, n_after: 20},
      {status: 'pending', trim_epoch: 1893456000, window: 20, n_after: 12},
      {status: 'overlap', trim_epoch: 1893456000, window: 20, n_after: 20},
      {status: 'cannot_measure', trim_epoch: 1893456000, reason: '<b>x</b>', window: 20, n_after: 0}];
    var h = firstCallContextHtml(Object.assign({}, base, {setup_steps: {available: true, steps: steps}}));
    var need = ['fell from 60,000 to 45,000 tokens', 'step of 15,000', '1,250 main-thread calls since the trim', '$5.63', '(measured)',
                'st-mega', '60K', "What's in it", 'Components overlap and do not add up',
                'no step down is visible', 'waiting for 8 more sessions', 'too close to tell them apart', '&lt;b>x&lt;/b>'];
    need.forEach(function(t) { if (h.indexOf(t) < 0) throw new Error('missing: ' + t); });
    if (h.indexOf('<b>x</b>') >= 0) throw new Error('unescaped reason');
    if (/\\u2014/.test(h)) throw new Error('em dash');
    var off = firstCallContextHtml(Object.assign({}, base, {setup_steps: {available: false}}));
    if (off.indexOf('Measured against') >= 0) throw new Error('unavailable must add nothing');
    '''
    path = tmp_path / 'fc.js'
    path.write_text(script, encoding='utf-8')
    r = subprocess.run([node, str(path)], capture_output=True, text=True, env=dict(os.environ))
    assert r.returncode == 0, r.stderr


def test_payload_wiring_is_fail_open_and_copies_the_cached_result():
    src = (ROOT / 'skills/token-optimizer/scripts/_measure/dashboard.py').read_text(encoding='utf-8')
    assert 'setup_step.collect(' in src and '{**result["first_call_context"], "setup_steps"' in src
    block = src[src.index('setup_step.collect('):]
    assert 'except Exception:' in block[:600]


def test_a_step_larger_than_the_trim_is_capped_at_the_trim_size():
    # The first call dropped 15K, but the dated trim only removed 800 tokens per call:
    # credit stops at what the trim itself removed; the observed drop stays visible.
    rows, trim = series(60_000, 45_000)
    r = setup_step.measure_step(rows, trim, rate_fn=rate, cap_tokens=800)
    assert r['status'] == 'measured' and r['drop_tokens'] == 800
    assert abs(r['observed_drop_tokens'] - 15_000) <= 300
    assert r['tokens_saved'] == 800 * r['calls_since']
    assert abs(r['usd'] - round(800 * r['calls_since'] * RATE, 4)) < 1e-6
    steps = setup_step.measure_all(rows, [{'ts_epoch': trim, 'tokens': 500}, {'ts_epoch': trim, 'tokens': 300}], rate)
    assert steps[0]['drop_tokens'] == 800 and steps[0]['estimated_tokens'] == 800
    small = setup_step.measure_step(rows, trim, rate_fn=rate, cap_tokens=50_000)
    assert small['drop_tokens'] == small['observed_drop_tokens']


def test_first_call_card_leads_with_the_latest_median_and_change_since_the_first_month(tmp_path):
    import os
    import subprocess
    node = shutil.which('node') or ''
    if not node:
        pytest.skip('node not installed')
    js = (ROOT / 'skills/token-optimizer/assets/first_call_context.js').read_text(encoding='utf-8')
    script = js + """
    var months = [{month: '2026-03', median: 39000, p25: 30000, p75: 45000, n: 9, components: {}},
                  {month: '2026-11', median: 82000, p25: 70000, p75: 90000, n: 9,
                   components: {skills: {median: 7000, observed: 9}, agents: {median: 1400, observed: 9}}}];
    var h = firstCallContextHtml({months: months, trim_dates: []});
    var hero = h.slice(0, h.indexOf('<svg'));
    ['82K', '+43K since March', 'st-mega'].forEach(function(t) { if (hero.indexOf(t) < 0) throw new Error('hero missing ' + t); });
    if (h.indexOf('>90K<') < 0 || h.indexOf('>45K<') < 0) throw new Error('y ticks in K');
    var fold = h.slice(h.indexOf('<details'));
    if (fold.indexOf('Skills listing') < 0 || fold.indexOf('Skills listing') > fold.indexOf('Agents')) throw new Error('components sorted by size in fold');
    if (hero.indexOf('Skills listing') >= 0) throw new Error('components belong in the fold');
    """
    path = tmp_path / 'fc2.js'
    path.write_text(script, encoding='utf-8')
    r = subprocess.run([node, str(path)], capture_output=True, text=True, env=dict(os.environ))
    assert r.returncode == 0, r.stderr


def test_a_stepped_trim_is_priced_on_main_thread_calls_since_the_trim_in_the_window():
    from _measure import structural_report
    t = {"tokens": 400, "step": {"share": 1.0, "drop": 800, "size": 800,
                                  "carried": [(datetime(2030, 1, 5, 12).timestamp(), 10, 2e-6),
                                              (datetime(2030, 1, 6, 12).timestamp(), 5, None),
                                              (datetime(2029, 12, 1, 12).timestamp(), 99, 2e-6)]}}
    r0 = {"requests": 777, "read_tokens": 1, "write_tokens": 2, "usd": 9.0, "read_usd": 9.0, "write_1h_usd": 1.0,
          "write_5m_usd": 0.0, "unpriced_tokens": 0}
    r, p = structural_report._step_value(t, r0, dict(r0), "2030-01-01", "2030-01-30")
    assert r["requests"] == 15 and r["read_tokens"] == 400 * 15 and r["write_tokens"] == 0
    assert abs(r["usd"] - 400 * 10 * 2e-6) < 1e-12 and r["write_1h_usd"] == 0


def test_large_transcripts_counts_only_in_window_files_over_the_cap(tmp_path):
    import os
    import time
    import large_transcripts
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    for name, size, age in (("big.jsonl", 50, 1), ("small.jsonl", 5, 1), ("old.jsonl", 50, 90)):
        f = proj / name
        f.write_bytes(b"x" * size)
        then = time.time() - age * 86400
        os.utime(f, (then, then))
    assert large_transcripts.count(tmp_path, 30, limit=10) == {"unscanned_large_sessions": 1}
    assert large_transcripts.count(tmp_path / "missing", 30, limit=10) == {"unscanned_large_sessions": 0}


def test_setup_rows_get_the_pricing_cards_the_step_uses(tmp_path, monkeypatch):
    # A stepped trim priced without cards has calls but $0; the row path must pass them.
    from _measure import setup_step, structural_report
    seen = {}
    monkeypatch.setattr(setup_step, "_MEMO", {})
    monkeypatch.setattr(setup_step, "collect", lambda *a, **k: seen.setdefault("card_fn", a[3]) and {"steps": []})
    db = tmp_path / "t.db"
    db.write_bytes(b"")
    setup_step.measured_epochs(db, tmp_path, None, card_fn=lambda m, t: {"cache_read": 0.3})
    assert callable(seen["card_fn"])
    src = Path(structural_report.__file__).read_text()
    assert "measured_epochs(f.TRENDS_DB, f.SNAPSHOT_DIR, f.CLAUDE_DIR, value.card_resolver(f))" in src
