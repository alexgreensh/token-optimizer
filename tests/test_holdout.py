"""W4: control-group scaffold. Built and tested, never on. Synthetic data only."""
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))
import holdout  # noqa: E402
from _measure import holdout_report  # noqa: E402

HOOK = SCRIPTS / 'bash_compress_hook.py'
LONG_LS = '\n'.join(
    f'-rw-r--r--  1 user  staff  {1000 + i:>6} Aug {10 + (i % 20):>2} 10:{i % 60:02d} file_{i:04d}.py'
    for i in range(1, 201))


def _sid(i):
    return str(uuid.UUID(int=i * 7919 + 13))


# ---- parsing and assignment -------------------------------------------------

@pytest.mark.parametrize('raw', [None, '', ' ', '0', '0.0', '-0.1', '-1', 'abc', 'nan', 'inf', '1.5', '2', '10%', '0,1'])
def test_every_unusable_value_means_off(raw):
    env = {} if raw is None else {holdout.ENV: raw}
    assert holdout.share(env) == 0.0
    assert holdout.arm(_sid(1), snapshot_dir='/nonexistent/never-created', env=env) == holdout.OFF


@pytest.mark.parametrize('raw,expected', [('0.1', 0.1), (' 0.25 ', 0.25), ('1', 1.0), ('1.0', 1.0), ('0.000001', 1e-06)])
def test_valid_shares(raw, expected):
    assert holdout.share({holdout.ENV: raw}) == expected


def test_bucket_is_stable_uniform_and_share_scales(tmp_path):
    ids = [_sid(i) for i in range(1, 10001)]
    assert [holdout.bucket(i) for i in ids[:50]] == [holdout.bucket(i) for i in ids[:50]]
    assert all(0.0 <= holdout.bucket(i) < 1.0 for i in ids)
    ten = {i for i in ids if holdout.decide(i, 0.10) == holdout.CONTROL}
    thirty = {i for i in ids if holdout.decide(i, 0.30) == holdout.CONTROL}
    assert 0.085 * len(ids) <= len(ten) <= 0.115 * len(ids)
    assert 0.27 * len(ids) <= len(thirty) <= 0.33 * len(ids)
    assert ten <= thirty  # raising the share only ever adds control sessions
    assert holdout.decide(_sid(1), 1.0) == holdout.CONTROL
    assert holdout.decide(_sid(1), 0.0) == holdout.OFF


def test_unusable_session_ids_are_never_control():
    for sid in ('', '   ', 'unknown', None):
        assert holdout.decide(sid, 1.0) == holdout.TREATMENT


def test_off_touches_no_disk(tmp_path):
    target = tmp_path / 'never'
    assert holdout.arm(_sid(2), snapshot_dir=target, env={}) == holdout.OFF
    assert not target.exists()


def test_first_assignment_is_stored_and_wins_over_a_changed_share(tmp_path):
    sid = _sid(3)
    assert holdout.arm(sid, tmp_path, {holdout.ENV: '1'}) == holdout.CONTROL
    # The share drops to almost nothing; the session stays where it was put.
    assert holdout.arm(sid, tmp_path, {holdout.ENV: '0.000001'}) == holdout.CONTROL
    with sqlite3.connect(tmp_path / 'trends.db') as conn:
        assert conn.execute('SELECT arm, share FROM holdout_sessions WHERE session_uuid = ?', (sid,)).fetchall() == [('control', 1.0)]
    assert holdout.is_control(sid, tmp_path, {holdout.ENV: '0.000001'}) is True


def test_unwritable_ledger_falls_back_to_the_pure_decision(tmp_path):
    blocker = tmp_path / 'file'
    blocker.write_text('x')
    assert holdout.arm(_sid(4), blocker / 'sub', {holdout.ENV: '1'}) == holdout.CONTROL


def test_default_is_off_everywhere_it_could_be_set():
    for rel in ('hooks/hooks.json', 'install.sh', 'package.json', '.claude-plugin/plugin.json', 'skills/token-optimizer/SKILL.md'):
        p = ROOT / rel
        if p.exists():
            assert 'TOKEN_OPTIMIZER_HOLDOUT' not in p.read_text(encoding='utf-8'), rel
    assert holdout.share({}) == 0.0


# ---- the Bash hook in each arm ----------------------------------------------

def _hook(tmp_path, holdout_value, session, command='ls -la', stdout=LONG_LS):
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ('CLAUDE_PLUGIN_ROOT', holdout.ENV)}
    env.update(HOME=str(home), CLAUDE_CONFIG_DIR=str(home / '.claude'), CLAUDE_SESSION_ID=session,
               TOKEN_OPTIMIZER_SNAPSHOT_DIR=str(tmp_path / 'snap'), PYTHONDONTWRITEBYTECODE='1')
    if holdout_value is not None:
        env[holdout.ENV] = holdout_value
    payload = {'session_id': session, 'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
               'tool_input': {'command': command},
               'tool_response': {'stdout': stdout, 'stderr': '', 'interrupted': False, 'isImage': False},
               'tool_use_id': 'toolu_synthetic'}
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True,
                          text=True, env=env, timeout=60)


def _q(tmp_path, sql):
    db = tmp_path / 'snap' / 'trends.db'
    if not db.exists():
        return []
    with sqlite3.connect(db) as conn:
        try:
            return conn.execute(sql).fetchall()
        except sqlite3.OperationalError:
            return []


@pytest.mark.parametrize('value', [None, '', '0', 'banana', '-1', '7'])
def test_off_behaves_exactly_like_before(tmp_path, value):
    proc = _hook(tmp_path, value, _sid(5))
    assert 'updatedToolOutput' in proc.stdout
    assert _q(tmp_path, "SELECT name FROM sqlite_master WHERE name = 'holdout_sessions'") == []
    assert _q(tmp_path, "SELECT COUNT(*) FROM bash_coverage")[0][0] == 1


def test_control_session_replaces_nothing_and_logs_the_opportunity(tmp_path):
    proc = _hook(tmp_path, '1', _sid(6))
    assert proc.returncode == 0 and proc.stdout.strip() == ''
    rows = _q(tmp_path, "SELECT feature, tier, detail, original_tokens, compressed_tokens, verified "
                        "FROM compression_events")
    assert len(rows) == 1
    feature, tier, detail, orig, comp, verified = rows[0]
    assert (feature, tier, detail, verified) == ('bash_compress_pipeline', 'opportunity', 'holdout_control', 0)
    assert 0 < comp < orig
    assert _q(tmp_path, "SELECT arm FROM holdout_sessions") == [('control',)]
    assert _q(tmp_path, "SELECT * FROM bash_coverage") == []  # the funnel counts only sessions TO acted on


def test_control_session_archives_nothing(tmp_path):
    sid = _sid(7)
    _hook(tmp_path, '1', sid)
    names = sorted(p.name for p in (tmp_path / 'snap').rglob('*') if p.is_file())
    # The ledger, plus the existing per-session thrash-guard state. No archived original.
    assert all(n.startswith('trends.db') or n == f'{sid}.db' for n in names), names
    assert not any('archive' in str(p.relative_to(tmp_path / 'snap')) for p in (tmp_path / 'snap').rglob('*'))


def test_treatment_session_compresses_normally(tmp_path):
    sid = next(_sid(i) for i in range(1000) if holdout.decide(_sid(i), 0.0001) == holdout.TREATMENT)
    proc = _hook(tmp_path, '0.0001', sid)
    assert 'updatedToolOutput' in proc.stdout
    assert _q(tmp_path, "SELECT arm FROM holdout_sessions") == [('treatment',)]
    assert _q(tmp_path, "SELECT COUNT(*) FROM bash_coverage")[0][0] == 1


def test_control_session_gets_no_nudge(tmp_path):
    sid = _sid(8)
    outputs = [_hook(tmp_path, '1', sid, command='cat notes.txt', stdout='the same output\n' * 20).stdout for _ in range(4)]
    assert all(o.strip() == '' for o in outputs)


def test_control_shadow_uses_the_build_compressor_too(tmp_path):
    build = '\n'.join(f'   Compiling crate{i} v0.{i}.0' for i in range(80)) + '\n    Finished dev target(s) in 12.3s\n'
    _hook(tmp_path, '1', _sid(9), command='make all', stdout=build)
    assert _q(tmp_path, "SELECT feature, tier FROM compression_events") == [('build_output_compress', 'opportunity')]


# ---- report -----------------------------------------------------------------

def _ledger(tmp_path, control, treatment, calls=20, extra=()):
    db = tmp_path / 'trends.db'
    conn = sqlite3.connect(db)
    conn.executescript('''
    CREATE TABLE holdout_sessions(session_uuid TEXT PRIMARY KEY, arm TEXT, share REAL, assigned_at TEXT);
    CREATE TABLE session_log(id INTEGER PRIMARY KEY, session_uuid TEXT, api_calls INTEGER, input_tokens INTEGER,
        output_tokens INTEGER, is_sidechain INTEGER DEFAULT 0);
    CREATE TABLE savings_events(id INTEGER PRIMARY KEY, event_type TEXT, session_uuid TEXT);''')
    n = 0
    for arm, count, per_call in (('control', control, 1000), ('treatment', treatment, 800)):
        for i in range(count):
            n += 1
            sid = _sid(10000 + n)
            conn.execute('INSERT INTO holdout_sessions VALUES (?,?,?,?)', (sid, arm, 0.5, 't'))
            conn.execute('INSERT INTO session_log(session_uuid, api_calls, input_tokens, output_tokens) VALUES (?,?,?,?)',
                         (sid, calls + (i % 5), per_call * (calls + (i % 5)) - 10, 10))
            if arm == 'treatment' and i % 10 == 0:
                conn.execute("INSERT INTO savings_events(event_type, session_uuid) VALUES ('tool_archive_reexpand', ?)", (sid,))
    for sql in extra:
        conn.execute(sql)
    conn.commit()
    conn.close()
    return db


def test_not_ready_below_thirty_per_arm(tmp_path):
    r = holdout_report.collect(_ledger(tmp_path, 29, 60))
    assert r['ready'] is False and r['arms'] == {'control': 29, 'treatment': 60} and 'metrics' not in r


def test_ready_at_thirty_per_arm_with_intervals(tmp_path):
    r = holdout_report.collect(_ledger(tmp_path, 30, 30), resamples=300)
    assert r['ready'] is True
    m = r['metrics']['tokens_per_call']
    assert abs(m['control']['mean'] - 1000) < 1 and abs(m['treatment']['mean'] - 800) < 1
    assert m['difference']['mean'] < -190 and m['difference']['high'] < 0 < 1  # treatment is cheaper per call
    for arm in ('control', 'treatment'):
        e = m[arm]
        assert e['low'] <= e['mean'] <= e['high']
    rx = r['metrics']['reexpand_rate']
    assert rx['control']['mean'] == 0.0 and abs(rx['treatment']['mean'] - 0.1) < 1e-9
    assert rx['treatment']['low'] < 0.1 < rx['treatment']['high']
    assert r['interval'] == 0.95


def test_short_and_sidechain_sessions_are_excluded(tmp_path):
    db = _ledger(tmp_path, 30, 30, calls=9)  # 9..13 calls: some sessions under 10 calls
    r = holdout_report.collect(db, resamples=50)
    assert r['arms']['control'] < 30 and r['ready'] is False
    (tmp_path / 'b').mkdir()
    db2 = _ledger(tmp_path / 'b', 30, 30, extra=["UPDATE session_log SET is_sidechain = 1 WHERE id <= 5"])
    assert holdout_report.collect(db2, resamples=50)['arms']['control'] == 25


def test_nothing_without_the_holdout_and_never_writes(tmp_path):
    assert holdout_report.collect(None)['ready'] is False
    assert holdout_report.collect(tmp_path / 'none.db') == holdout_report.collect(None)
    db = tmp_path / 'trends.db'
    sqlite3.connect(db).close()
    off = holdout_report.collect(db)
    assert off['ready'] is False and off['enabled'] is False
    (tmp_path / 'x').mkdir()
    full = _ledger(tmp_path / 'x', 30, 30)
    before = full.read_bytes()
    holdout_report.collect(full, resamples=50)
    assert full.read_bytes() == before


def test_wilson_and_bootstrap_are_correct_and_deterministic():
    p, lo, hi = holdout_report.wilson(0, 10)
    assert p == 0.0 and lo == 0.0 and abs(hi - 0.2775) < 1e-3
    p, lo, hi = holdout_report.wilson(10, 10)
    assert hi == 1.0 and abs(lo - 0.7225) < 1e-3
    p, lo, hi = holdout_report.wilson(5, 10)
    assert abs((lo + hi) / 2 - 0.5) < 1e-9 and 0.23 < lo < 0.24
    a, b = [1.0, 2.0, 3.0, 4.0] * 10, [3.0, 4.0, 5.0, 6.0] * 10
    d1 = holdout_report.bootstrap_diff(a, b, 500)
    assert d1 == holdout_report.bootstrap_diff(a, b, 500)
    assert abs(d1[0] - 2.0) < 1e-9 and d1[1] < 2.0 < d1[2]
    assert holdout_report.bootstrap_diff([5.0] * 40, [5.0] * 40, 200) == (0.0, 0.0, 0.0)
    m = holdout_report.bootstrap_mean(a, 500)
    assert m[1] <= m[0] <= m[2]


# ---- dashboard --------------------------------------------------------------

def test_renderer_is_silent_unless_ready(tmp_path):
    node = subprocess.run(['which', 'node'], capture_output=True, text=True).stdout.strip()
    if not node:
        pytest.skip('node not installed')
    js = (ROOT / 'skills/token-optimizer/assets/holdout_card.js').read_text(encoding='utf-8')
    script = js + '''
    var m = {mean: 10, low: 8, high: 12};
    if (holdoutHtml({}) !== '') throw new Error('absent');
    if (holdoutHtml({holdout: {ready: false, arms: {control: 3, treatment: 4}}}) !== '') throw new Error('not ready must be silent');
    var h = holdoutHtml({holdout: {ready: true, interval: 0.95, min_calls: 10, arms: {control: 31, treatment: 33}, metrics: {
      calls_per_session: {control: m, treatment: m, difference: m},
      tokens_per_call: {control: m, treatment: m, difference: m},
      reexpand_rate: {control: {mean: 0.1, low: 0.05, high: 0.2}, treatment: {mean: 0.1, low: 0.05, high: 0.2}, difference: {mean: 0, low: -0.1, high: 0.1}}}}});
    if (h.indexOf('With and without Token Optimizer') < 0 || h.indexOf('(31)') < 0 || h.indexOf('10.0%') < 0) throw new Error('rows');
    if (/\\u2014/.test(h)) throw new Error('em dash');
    '''
    path = tmp_path / 'ho.js'
    path.write_text(script, encoding='utf-8')
    r = subprocess.run([node, str(path)], capture_output=True, text=True, env=dict(os.environ))
    assert r.returncode == 0, r.stderr


def test_template_and_payload_wiring():
    html = (ROOT / 'skills/token-optimizer/assets/dashboard.html').read_text(encoding='utf-8')
    assert '/*@include holdout_card.js*/' in html and html.count('html += holdoutHtml(data)') == 2
    src = (SCRIPTS / '_measure/dashboard.py').read_text(encoding='utf-8')
    assert src.count('"holdout": holdout_report.collect(') == 2
