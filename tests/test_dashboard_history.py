"""Dashboard history choices survive rebuilds and remain bounded by default."""
import importlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))


def test_history_preference_default_save_and_invalid(tmp_path):
    history = importlib.import_module('_measure.dashboard_history')
    assert history.load(tmp_path) == 30
    history.save(tmp_path, 365)
    assert history.load(tmp_path) == 365
    history.save(tmp_path, 'all')
    assert history.load(tmp_path) == 'all'
    (tmp_path / 'token-optimizer/dashboard-history.json').write_text('{"days": -2}')
    assert history.load(tmp_path) == 30


def test_full_history_window_uses_oldest_activity(tmp_path):
    history = importlib.import_module('_measure.dashboard_history')
    db = tmp_path / 'trends.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE session_log (date TEXT, first_activity_day TEXT)')
        conn.execute("INSERT INTO session_log VALUES ('2026-05-01', '2026-03-01')")
    assert history.resolve_days('all', db, today=history.date(2026, 11, 1)) == 246
    assert history.resolve_days(180, db) == 180


def test_dispatch_keeps_explicit_window_for_future_rebuilds(tmp_path, monkeypatch):
    import measure
    monkeypatch.setattr(measure, 'RUNTIME_DIR', tmp_path)
    monkeypatch.setattr(measure, '_running_under_hook', lambda: False)
    monkeypatch.setattr(measure, '_open_dashboard', lambda **kwargs: None)
    calls = []
    monkeypatch.setattr(measure, 'generate_standalone_dashboard', lambda **kw: calls.append(kw['days']) or str(tmp_path / 'dash.html'))
    for args in (['dashboard', '--days', '365', '--quiet'], ['dashboard', '--quiet']):
        with pytest.raises(SystemExit) as ex:
            measure._dispatch_dashboard(args)
        assert ex.value.code == 0
    assert calls == [365, 365]


def test_daily_controls_offer_long_ranges_and_full_history():
    html = (SCRIPTS.parent / 'assets/dashboard.html').read_text()
    for value in ('90', '180', 'all'):
        assert 'data-range="' + value + '"' in html
    assert 'Load full history' in html


def test_range_filters_calendar_days_and_all(tmp_path):
    import subprocess
    html = (SCRIPTS.parent / 'assets/dashboard.html').read_text()
    start = html.index('  function selectDailyRange(')
    end = html.index('  function buildDailyTable(', start)
    script = html[start:end] + """
const rows = [{date:'2026-11-09'}, {date:'2026-11-02'}, {date:'2026-05-01'}];
function dates(x) { return x.map(r => r.date); }
if (JSON.stringify(dates(selectDailyRange(rows, '7', null))) !== JSON.stringify(['2026-11-09'])) throw Error('calendar window');
if (dates(selectDailyRange(rows, 'all', null)).length !== 3) throw Error('all embedded');
if (JSON.stringify(dates(selectDailyRange(rows, 'all', '2026-11-02'))) !== JSON.stringify(['2026-11-02','2026-05-01'])) throw Error('all with date picker');
"""
    subprocess.run(['node', '-e', script], check=True)


def test_older_daily_usage_is_counted_when_bounds_are_missing(tmp_path):
    history = importlib.import_module('_measure.dashboard_history')
    db = tmp_path / 'trends.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE session_log (date TEXT, first_activity_day TEXT, daily_usage_json TEXT)')
        conn.execute('INSERT INTO session_log VALUES (?, ?, ?)', ('2026-10-01', None, json.dumps({'2026-01-01': {}})))
    assert history.resolve_days('all', db, today=history.date(2026, 11, 1)) == 305


def test_failed_build_does_not_save_explicit_window(tmp_path, monkeypatch):
    import measure
    history = importlib.import_module('_measure.dashboard_history')
    monkeypatch.setattr(measure, 'RUNTIME_DIR', tmp_path)
    monkeypatch.setattr(measure, '_running_under_hook', lambda: False)
    monkeypatch.setattr(measure, 'generate_standalone_dashboard', lambda **kw: None)
    with pytest.raises(SystemExit) as ex:
        measure._dispatch_dashboard(['dashboard', '--days', '365', '--quiet'])
    assert ex.value.code == 1
    assert history.load(tmp_path) == 30


def test_default_trends_payload_excludes_older_sessions(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    from _activity_fixtures import stamp_activity
    import measure
    db = tmp_path / 'trends.db'
    monkeypatch.setattr(measure, 'TRENDS_DB', db)
    conn = measure._init_trends_db()
    now = datetime.now()
    for i in range(101):
        day = (now - timedelta(days=5 if i == 0 else 60)).date().isoformat()
        conn.execute('INSERT INTO session_log (jsonl_path, date, input_tokens, output_tokens, message_count) VALUES (?, ?, ?, ?, ?)',
                     ('session-%d' % i, day, 100, 20, 1))
    stamp_activity(conn)
    conn.commit(); conn.close()
    recent = measure._collect_trends_from_db(days=30)
    expanded = measure._collect_trends_from_db(days=90)
    assert recent['session_count'] == 1
    assert expanded['session_count'] == 101
    assert len(json.dumps(recent)) < len(json.dumps(expanded)) / 3
