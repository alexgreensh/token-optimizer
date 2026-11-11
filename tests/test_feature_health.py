"""Feature-health card: synthetic ledgers only."""
import os
import sqlite3
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/token-optimizer/scripts'))
from _measure import feature_health  # noqa: E402

NOW = datetime(2030, 3, 1, 12, 0, 0)


def _db(tmp_path, savings=(), compression=(), sessions=(), commands=()):
    db = tmp_path / 'trends.db'
    conn = sqlite3.connect(db)
    conn.executescript('''
    CREATE TABLE savings_events(id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT, tokens_saved INTEGER);
    CREATE TABLE compression_events(id INTEGER PRIMARY KEY, timestamp TEXT, feature TEXT,
        original_tokens INTEGER, compressed_tokens INTEGER, command_pattern TEXT);
    CREATE TABLE session_log(id INTEGER PRIMARY KEY, date TEXT);''')
    conn.executemany('INSERT INTO savings_events(timestamp, event_type, tokens_saved) VALUES (?,?,?)', savings)
    conn.executemany('INSERT INTO compression_events(timestamp, feature, original_tokens, compressed_tokens) VALUES (?,?,?,?)', compression)
    conn.executemany('INSERT INTO compression_events(timestamp, feature, original_tokens, compressed_tokens, command_pattern) '
                     'VALUES (?,?,?,?,?)', commands)
    conn.executemany('INSERT INTO session_log(date) VALUES (?)', [(d,) for d in sessions])
    conn.commit()
    conn.close()
    return db


def _day(n):
    return (NOW - timedelta(days=n)).strftime('%Y-%m-%dT10:00:00')


def _rows(result):
    return {r['feature']: r for r in result['features']}


def test_feature_that_used_to_fire_and_stopped_warns(tmp_path):
    # Active every day for 120 days; dedup fired 3x/day until 40 days ago.
    sessions = [_day(n)[:10] for n in range(120)]
    comp = [(_day(n), 'crossturn_dedup', 500, 100) for n in range(40, 120) for _ in range(3)]
    result = feature_health.collect(_db(tmp_path, compression=comp, sessions=sessions), now=NOW)
    row = _rows(result)['crossturn_dedup']
    assert result['available'] is True
    assert row['events'] == 0
    assert row['expected'] > 50
    assert row['status'] == 'quiet' and row['warning'] is True
    assert result['warnings'] >= 1


def test_less_activity_scales_expectation_down(tmp_path):
    # Same per-active-day rate, but the user only worked 2 days this month.
    sessions = [_day(n)[:10] for n in range(40, 120)] + [_day(3)[:10], _day(5)[:10]]
    comp = [(_day(n), 'crossturn_dedup', 500, 100) for n in range(40, 120)]
    comp += [(_day(3), 'crossturn_dedup', 500, 100), (_day(5), 'crossturn_dedup', 500, 100)]
    row = _rows(feature_health.collect(_db(tmp_path, compression=comp, sessions=sessions), now=NOW))['crossturn_dedup']
    assert row['events'] == 2
    assert row['expected'] is not None and row['expected'] < 6
    assert row['warning'] is False and row['status'] == 'ok'


def test_light_user_with_few_events_is_not_warned(tmp_path):
    sessions = [_day(n)[:10] for n in range(120)]
    sav = [(_day(n), 'delta_read', 300) for n in (50, 80, 110)]
    row = _rows(feature_health.collect(_db(tmp_path, savings=sav, sessions=sessions), now=NOW))['delta_read']
    assert row['events'] == 0
    assert row['status'] == 'rare'
    assert row['warning'] is False


def test_fires_but_saves_nothing_warns(tmp_path):
    sessions = [_day(n)[:10] for n in range(60)]
    sav = [(_day(n), 'tool_archive', 0) for n in range(10)]
    row = _rows(feature_health.collect(_db(tmp_path, savings=sav, sessions=sessions), now=NOW))['tool_archive']
    assert row['status'] == 'no_effect' and row['warning'] is True


def test_bash_categories_are_grouped_so_a_rename_is_not_an_outage(tmp_path):
    sessions = [_day(n)[:10] for n in range(120)]
    comp = [(_day(n), 'bash_compress_git', 900, 100) for n in range(40, 120)]
    comp += [(_day(n), 'bash_compress_pipeline', 900, 100) for n in range(0, 30)]
    comp += [(_day(1), 'bashXcompressYgit', 900, 100)]  # LIKE wildcard must not match
    row = _rows(feature_health.collect(_db(tmp_path, compression=comp, sessions=sessions), now=NOW))['bash_compression']
    assert row['events'] == 30
    assert row['status'] == 'ok'


def test_mirrored_delta_rows_are_not_double_counted(tmp_path):
    sav = [(_day(1), 'delta_read', 400)]
    comp = [(_day(1), 'delta_read', 500, 100)]
    row = _rows(feature_health.collect(_db(tmp_path, savings=sav, compression=comp), now=NOW))['delta_read']
    assert row['events'] == 1


def test_unavailable_database_is_not_reported_as_zero(tmp_path):
    result = feature_health.collect(tmp_path / 'missing.db')
    assert result['available'] is False
    assert result['features'] == []
    assert not (tmp_path / 'missing.db').exists()
    assert feature_health.collect(None)['available'] is False


def test_missing_table_reads_unknown_not_zero(tmp_path):
    db = tmp_path / 'trends.db'
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE savings_events(id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT, tokens_saved INTEGER)')
    conn.commit(); conn.close()
    rows = _rows(feature_health.collect(db, now=NOW))
    assert rows['crossturn_dedup']['events'] is None
    assert rows['crossturn_dedup']['status'] == 'unknown'
    assert rows['delta_read']['events'] == 0


def test_collect_never_writes(tmp_path):
    db = _db(tmp_path, savings=[(_day(1), 'delta_read', 1)])
    before = db.read_bytes()
    feature_health.collect(db, now=NOW)
    assert db.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ['trends.db']


def test_renderer_handles_unavailable_and_rows(tmp_path):
    node = shutil.which('node') or ''
    if not node:
        import pytest
        pytest.skip('node not installed')
    js = (ROOT / 'skills/token-optimizer/assets/feature_health.js').read_text(encoding='utf-8')
    script = js + '''
    var a = featureHealthHtml({feature_health: {available: false, features: []}});
    if (a.indexOf('unknown (not zero)') < 0) throw new Error('unavailable copy');
    var b = featureHealthHtml({feature_health: {available: true, warnings: 1, window_days: 30, features: [
      {feature: 'x', label: 'X <b>', status: 'quiet', events: 0, expected: 40, last_event: '2030-01-01', message: 'm'},
      {feature: 'y', label: 'Y', status: 'ok', events: 5, expected: 5, last_event: null}]}});
    if (b.indexOf('Went quiet') < 0 || b.indexOf('X &lt;b&gt;') < 0) throw new Error('rows');
    if (b.indexOf('Went quiet') > b.indexOf('Working')) throw new Error('order');
    if (/\\u2014/.test(a + b)) throw new Error('em dash');
    if (featureHealthHtml({}) !== '') throw new Error('absent');
    '''
    path = tmp_path / 'fh.js'
    path.write_text(script, encoding='utf-8')
    r = subprocess.run([node, str(path)], capture_output=True, text=True, env=dict(os.environ))
    assert r.returncode == 0, r.stderr


def test_template_includes_renderer_and_calls_it():
    html = (ROOT / 'skills/token-optimizer/assets/dashboard.html').read_text(encoding='utf-8')
    assert '/*@include feature_health.js*/' in html
    assert html.count('html += featureHealthHtml(data)') == 2  # with and without live health data
    src = (ROOT / 'skills/token-optimizer/scripts/_measure/dashboard.py').read_text(encoding='utf-8')
    assert src.count('"feature_health": feature_health.collect(') == 2


def _twin_pair(n, i, cmd=None, gap=1.0):
    """A dedup claim and the pipeline event from the other install for one call."""
    cmd = cmd or f'git log --oneline -{n}-{i}'
    base = datetime.strptime(_day(n), '%Y-%m-%dT%H:%M:%S') + timedelta(minutes=i)
    twin = base + timedelta(seconds=gap)
    iso = lambda d: d.isoformat(timespec='microseconds')
    return [(iso(base), 'crossturn_dedup', 500, 100, cmd), (iso(twin), 'bash_compress_pipeline', 500, 300, cmd)]


def test_history_of_duplicate_install_false_positives_does_not_read_as_quiet(tmp_path):
    # 80 days x 3 dedup events, every one with a same-command pipeline twin 1 s apart;
    # nothing in the last 40 days. Counted naively that is "went quiet".
    sessions = [_day(n)[:10] for n in range(120)]
    cmds = [row for n in range(40, 120) for i in range(3) for row in _twin_pair(n, i)]
    result = feature_health.collect(_db(tmp_path, sessions=sessions, commands=cmds), now=NOW)
    row = _rows(result)['crossturn_dedup']
    assert row['earlier_events'] == 0 and row['events'] == 0
    assert row['status'] == 'never' and row['warning'] is False
    # The same history without the twins is a genuine outage and still warns.
    (tmp_path / 'bare').mkdir()
    bare = [r for r in cmds if r[1] == 'crossturn_dedup']
    naive = _rows(feature_health.collect(_db(tmp_path / 'bare', sessions=sessions, commands=bare), now=NOW))['crossturn_dedup']
    assert naive['status'] == 'quiet' and naive['warning'] is True


def test_mostly_false_positive_history_judged_on_the_real_events_only(tmp_path):
    # 9 of 10 earlier events are twinned; the 10th is real. Recent: the user keeps getting real ones.
    sessions = [_day(n)[:10] for n in range(120)]
    cmds = []
    for n in range(40, 120):
        cmds += _twin_pair(n, 0)
        cmds += _twin_pair(n, 1)
    real = [(_day(n), 'crossturn_dedup', 500, 100, f'real {n}') for n in range(50, 120, 7)]
    recent_real = [(_day(n), 'crossturn_dedup', 500, 100, f'recent {n}') for n in range(0, 30, 4)]
    row = _rows(feature_health.collect(_db(tmp_path, sessions=sessions, commands=cmds + real + recent_real), now=NOW))['crossturn_dedup']
    assert row['earlier_events'] == len(real)
    assert row['events'] == len(recent_real)
    assert row['warning'] is False


def test_twin_outside_three_seconds_or_other_command_is_not_a_false_positive(tmp_path):
    sessions = [_day(n)[:10] for n in range(120)]
    far = [row for n in range(40, 120) for i in range(3) for row in _twin_pair(n, i, gap=5.0)]
    other = [(a, b, c, d, 'something else' if b == 'bash_compress_pipeline' else e)
             for n in range(40, 120) for i in range(3) for (a, b, c, d, e) in _twin_pair(n, i + 10)]
    for cmds in (far, other):
        sub = tmp_path / f'case{id(cmds)}'
        sub.mkdir()
        row = _rows(feature_health.collect(_db(sub, sessions=sessions, commands=cmds), now=NOW))['crossturn_dedup']
        assert row['earlier_events'] == 240
        assert row['status'] == 'quiet'


def test_twin_filter_is_skipped_when_the_ledger_has_no_command_column(tmp_path):
    # Older schema: cannot tell commands apart, so nothing is excluded and nothing breaks.
    sessions = [_day(n)[:10] for n in range(120)]
    comp = [(_day(n), 'crossturn_dedup', 500, 100) for n in range(40, 120) for _ in range(3)]
    db = tmp_path / 'old.db'
    conn = sqlite3.connect(db)
    conn.executescript('''CREATE TABLE savings_events(id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT, tokens_saved INTEGER);
        CREATE TABLE compression_events(id INTEGER PRIMARY KEY, timestamp TEXT, feature TEXT, original_tokens INTEGER, compressed_tokens INTEGER);
        CREATE TABLE session_log(id INTEGER PRIMARY KEY, date TEXT);''')
    conn.executemany('INSERT INTO compression_events(timestamp, feature, original_tokens, compressed_tokens) VALUES (?,?,?,?)', comp)
    conn.executemany('INSERT INTO session_log(date) VALUES (?)', [(d,) for d in sessions])
    conn.commit()
    conn.close()
    row = _rows(feature_health.collect(db, now=NOW))['crossturn_dedup']
    assert row['earlier_events'] == 240 and row['status'] == 'quiet'
