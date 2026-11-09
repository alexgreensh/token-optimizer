"""Typical-session effect, evidence-keyed coverage, fleet share and TO overhead.

Synthetic fixtures only: a tiny trends.db plus hand-written transcripts.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))

from _measure import session_effect as se  # noqa: E402
from _measure.headline import token_headline  # noqa: E402

START, END = '2026-10-01', '2026-10-31T23:59:59'


def _usage(day, fresh=0, cache_read=0, output=0):
    return json.dumps({day: {'opus': {'fresh_input': fresh, 'cache_create': 0,
                                      'cache_read': cache_read, 'output': output}}})


def _line(obj):
    return json.dumps(obj, separators=(',', ':'))


def _assistant(req, day='2026-10-05'):
    return _line({'type': 'assistant', 'requestId': req, 'timestamp': day + 'T10:00:00Z',
                  'message': {'id': 'm' + req, 'content': []}})


def _inject(text, kind='hook_additional_context', day='2026-10-05'):
    return _line({'type': 'attachment', 'timestamp': day + 'T10:00:00Z',
                  'attachment': {'type': kind, 'content': [text], 'hookName': 'UserPromptSubmit'}})


def _db(tmp_path):
    db = sqlite3.connect(str(tmp_path / 'trends.db'))
    db.executescript('''
        CREATE TABLE counted_reread(event_key TEXT PRIMARY KEY, source TEXT, session_uuid TEXT,
            event_ts TEXT, tokens INTEGER, oneshot_usd REAL, reread_tokens INTEGER, reread_usd REAL);
        CREATE TABLE session_log(session_uuid TEXT, jsonl_path TEXT, input_tokens INTEGER,
            parent_daily_usage_json TEXT, daily_usage_json TEXT, is_sidechain INTEGER);
        CREATE TABLE savings_events(timestamp TEXT, session_uuid TEXT);
        CREATE TABLE compression_events(timestamp TEXT, session_uuid TEXT);
    ''')
    return db


def _session(db, su, path, inp, traffic, side=0):
    db.execute('INSERT INTO session_log VALUES (?,?,?,?,NULL,?)',
               (su, str(path) if path else None, inp, _usage('2026-10-05', cache_read=traffic), side))


def _counted(db, su, tokens, reread, usd=0.0, key=None):
    db.execute('INSERT INTO counted_reread VALUES (?,?,?,?,?,?,?,?)',
               (key or su + str(tokens), 'ce', su, '2026-10-05T10:00:00', tokens, usd / 2, reread, usd / 2))


def _write(path, lines):
    path.write_text('\n'.join(lines) + '\n')
    return path


def test_per_session_percentiles_include_sidechains_and_skip_zero_input(tmp_path):
    db = _db(tmp_path)
    shares = []
    for i in range(10):
        su = 's%d' % i
        saved = (i + 1) * 10
        _session(db, su, None, 1000 - saved, 1000, side=int(i == 9))
        _counted(db, su, saved // 2, saved - saved // 2)
        shares.append(saved / 1000.0)
    _session(db, 'zero', None, 0, 0)
    _counted(db, 'zero', 5, 5)
    r = se.compute(db, START, END, scan_budget_s=None)
    ps = r['per_session']
    assert ps['sessions'] == 10
    assert ps['median'] == pytest.approx(shares[5])
    assert ps['p75'] == pytest.approx(shares[7])
    assert ps['p90'] == pytest.approx(shares[9])
    assert ps['max'] == pytest.approx(shares[-1])


def test_rows_outside_window_are_ignored(tmp_path):
    db = _db(tmp_path)
    _session(db, 'a', None, 900, 1000)
    db.execute("INSERT INTO counted_reread VALUES ('old','ce','a','2026-09-01T00:00:00',500,0,500,0)")
    _counted(db, 'a', 50, 50)
    assert se.compute(db, START, END)['per_session']['median'] == pytest.approx(0.1)


def test_coverage_is_keyed_on_evidence_not_directory(tmp_path):
    db = _db(tmp_path)
    stubs = tmp_path / 'cloud-stubs'
    stubs.mkdir()
    ran = _write(tmp_path / 'ran.jsonl', [_inject('[Token Optimizer] Context 40% of 1M'), _assistant('r1')])
    quiet_stub = _write(stubs / 'quiet.jsonl', [_assistant('q1')])
    quoted = _write(tmp_path / 'quoted.jsonl', [
        _line({'type': 'user', 'message': {'content': 'why does [Token Optimizer] say that?'}}),
        _inject('Other hook said [Token Optimizer] once'), _assistant('x1')])
    _session(db, 'ledger', None, 900, 1000)
    _counted(db, 'ledger', 50, 50)
    _session(db, 'logged', None, 0, 2000)
    db.execute("INSERT INTO savings_events VALUES ('2026-10-05T10:00:00','logged')")
    _session(db, 'ran', ran, 0, 3000)
    _session(db, 'stub', quiet_stub, 0, 4000)
    _session(db, 'quoted', quoted, 0, 5000)
    _session(db, 'missing', tmp_path / 'gone.jsonl', 0, 6000)
    _session(db, 'idle', None, 0, 0)
    cov = se.compute(db, START, END, scan_budget_s=None)['coverage']
    c = cov['classes']
    assert c['ledger']['sessions'] == 1 and c['logged_no_ledger']['sessions'] == 1
    assert c['ran_unlogged']['sessions'] == 1 and c['ran_unlogged']['traffic_tokens'] == 3000
    assert c['not_ran']['sessions'] == 2  # the cloud-stubs folder alone proves nothing
    assert c['unknown']['sessions'] == 1  # missing transcript is never "not ran"
    assert cov['sessions'] == 6  # no traffic and no rows: outside the window
    assert cov['ran_no_ledger_sessions'] == 2
    assert cov['ran_traffic_share'] == pytest.approx(6000 / 21000.0, abs=1e-6)


def test_overhead_rereads_until_compaction_and_restores_not_netted(tmp_path):
    big = '[Token Optimizer] ' + 'x' * 400
    path = _write(tmp_path / 't.jsonl', [
        _inject(big), _assistant('a'), _assistant('a'), _assistant('b'), _assistant('c'),
        _line({'type': 'system', 'subtype': 'compact_boundary'}), _assistant('d'),
        _inject('[Token Optimizer] Post-compact recovery (auto checkpoint): ' + 'y' * 400),
        _assistant('e'),
        _inject('[Token Optimizer] WARNING: 202+ tool calls', kind='hook_system_message'),
        _assistant('f')])
    scan = se.scan_transcript(str(path))
    t = se._tokens(big)
    assert scan['injections'] == 1 and scan['markers'] == 3
    assert scan['days']['2026-10-05'] == [t, 2 * t]  # a is the first read; b, c re-read; d after compaction
    assert scan['restore_tokens'] > 0


def test_fleet_shares_and_net_of_overhead(tmp_path):
    db = _db(tmp_path)
    path = _write(tmp_path / 'l.jsonl', [_inject('[Token Optimizer] ' + 'z' * 400),
                                         _assistant('a'), _assistant('b')])
    _session(db, 'l', path, 9000, 9000)
    _counted(db, 'l', 400, 600, usd=1.0)
    _session(db, 'r', _write(tmp_path / 'r.jsonl', [_inject('[Token Optimizer] hi'), _assistant('a')]), 0, 1000)
    _session(db, 'n', _write(tmp_path / 'n.jsonl', [_assistant('a')]), 0, 50000)
    r = se.compute(db, START, END, scan_budget_s=None, price=lambda models: 0.001 * sum(
        b['cache_read'] for b in models.values()))
    f = r['fleet']
    assert f['sessions'] == 2 and f['traffic_tokens'] == 10000  # 'n' never ran TO
    assert f['token_share'] == pytest.approx(1000 / 11000.0, abs=1e-6)
    assert f['cost_share'] == pytest.approx(1.0 / 11.0, abs=1e-6)
    assert f['token_share_net'] < f['token_share'] and f['cost_share_net'] < f['cost_share']
    assert f['cost_tier'] == 'estimated'
    oh = r['overhead']
    assert oh['injected_tokens'] > 0 and oh['reread_tokens'] > 0 and oh['usd'] > 0
    assert r['per_session']['net_median'] < r['per_session']['median']


def test_scan_cache_and_budget(tmp_path):
    db = _db(tmp_path)
    p1 = _write(tmp_path / 'a.jsonl', [_inject('[Token Optimizer] hi'), _assistant('a')])
    _session(db, 'a', p1, 0, 100)
    assert se.compute(db, START, END, scan_budget_s=None)['coverage']['classes']['ran_unlogged']['sessions'] == 1
    assert db.execute('SELECT COUNT(*) FROM to_session_evidence').fetchone()[0] == 1
    p2 = _write(tmp_path / 'b.jsonl', [_inject('[Token Optimizer] hi'), _assistant('a')])
    _session(db, 'b', p2, 0, 100)
    r = se.compute(db, START, END, scan_budget_s=-1)  # budget spent: cached a, unscanned b
    assert r['coverage']['classes']['ran_unlogged']['sessions'] == 1
    assert r['coverage']['unknown_sessions'] == 1


def test_missing_tables_unavailable(tmp_path):
    db = sqlite3.connect(str(tmp_path / 'empty.db'))
    assert se.compute(db, START, END)['available'] is False


def test_cli_lines_render(tmp_path):
    db = _db(tmp_path)
    _session(db, 'a', None, 900, 1000)
    _counted(db, 'a', 50, 50)
    text = '\n'.join(se.cli_lines(se.compute(db, START, END)))
    assert 'In a typical session, 10.0% of your context never gets sent' in text
    assert 'Token Optimizer ran in 100.0% of your traffic' in text and 'Your limits (estimated' in text
    assert se.cli_lines({'available': False}) == []


def test_headline_splits_the_removed_pool_by_what_it_is():
    h = token_headline({'total_tokens': 1000,
                        'by_category': {'verbosity_steer_measured': {'tokens_saved': 300},
                                        'bash_compress_pipeline': {'tokens_saved': 700}},
                        'resume_lean_estimated': {'tokens_saved': 200},
                        'checkpoint_restore_estimated': {'tokens_saved': 50},
                        'mcp_cap_estimated': {'tokens_saved': 25}})
    assert h['removed_tokens'] == 1275
    assert h['output_not_generated_tokens'] == 300
    assert h['reload_avoided_tokens'] == 250
    assert h['context_removed_tokens'] == 725
    assert h['context_removed_tokens'] + h['output_not_generated_tokens'] + h['reload_avoided_tokens'] == h['removed_tokens']


def test_no_surface_renders_the_fresh_label_or_fresh_only_multiplier():
    root = SCRIPTS.parents[2]
    for rel in ('skills/token-optimizer/assets/dashboard.html',
                'plugins/token-optimizer/skills/token-optimizer/assets/dashboard.html',
                'cowork/token-optimizer/skills/token-optimizer/assets/dashboard.html'):
        html = (root / rel).read_text(encoding='utf-8')
        assert "' fresh &middot; '" not in html, rel
        assert 'tokenSplitText(' in html, rel
    source = (SCRIPTS / '_measure/savings.py').read_text(encoding='utf-8')
    # The context multiplier divides by dated traffic that includes cache reads and writes.
    assert 'consumed = traffic["tokens"]' in source
    assert "('fresh_input', 'cache_create', 'cache_read', 'output')" in (
        SCRIPTS / '_measure/headline.py').read_text(encoding='utf-8')
