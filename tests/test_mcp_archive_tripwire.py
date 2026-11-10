import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))

import sqlite3
import json
import os
import subprocess
from _measure.mcp_archive_tripwire import evaluate, structured_preview, decide, record, record_expand, expand_call_overhead


def test_tripwire_uses_net_not_expand_rate():
    assert not evaluate([(100, 1, 1)] * 20)
    assert evaluate([(100, 99, 1)] * 20)
    assert evaluate([(100, 0, 100)] * 20)
    assert not evaluate([(100, 200, 1)] * 19)
    assert not evaluate([(1, 100, 0)] + [(100, 1, 1)] * 20)


def test_preview_preserves_head_tail_and_outline_within_budget():
    text = 'HEAD\n' + '\n'.join('## Section %d\n%s' % (i, 'x' * 120) for i in range(40)) + '\nTAIL'
    preview = structured_preview(text)
    assert 'HEAD' in preview and 'TAIL' in preview and 'Section 20' in preview
    assert len(preview) <= 3500
    assert len(preview) < len(text)


def test_preview_outlines_json_keys_in_the_middle():
    text = '{\n"start": "' + 'x' * 2200 + '",\n"middle_key": 42,\n"end": "' + 'y' * 2200 + '"\n}'
    assert 'middle_key' in structured_preview(text)


def test_legacy_rate_demotion_does_not_activate_net_rule(tmp_path):
    db = tmp_path / 'trends.db'
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE mcp_archive_demotions (tool TEXT PRIMARY KEY, created REAL, samples INTEGER, expanded INTEGER)')
    conn.execute('INSERT INTO mcp_archive_demotions VALUES (?,?,?,?)', ('mcp__one', 1, 20, 20))
    conn.commit()
    conn.close()
    assert not decide(db, 'mcp__one', active=True, now=1400)


def test_high_expand_rate_stays_active_when_net_is_positive(tmp_path):
    from datetime import datetime
    db = tmp_path / 'trends.db'
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE savings_events(timestamp TEXT, event_type TEXT, session_id TEXT, detail TEXT, tokens_saved INTEGER)')
    conn.commit()
    conn.close()
    for i in range(20):
        key = 'id%d' % i
        record(db, 'mcp__one', 'session', key, 1000, now=1000 + i)
        record_expand(db, 'session', key, now=1100 + i)
    conn = sqlite3.connect(db)
    for i in range(20):
        conn.execute('INSERT INTO savings_events VALUES (?,?,?,?,?)',
                     (datetime.fromtimestamp(1100 + i).isoformat(), 'tool_archive_reexpand', 'session', 'id%d' % i, 10))
    conn.commit()
    conn.close()
    assert not decide(db, 'mcp__one', active=True, now=1400)


def test_demotes_per_tool_after_completed_window_and_shadow_is_nonblocking(tmp_path):
    db = tmp_path / 'trends.db'
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE savings_events(timestamp TEXT, event_type TEXT, session_id TEXT, detail TEXT, tokens_saved INTEGER)')
    conn.commit()
    conn.close()
    for i in range(20):
        record(db, 'mcp__one', 'session', 'id%d' % i, 100, now=1000 + i)
    conn = sqlite3.connect(db)
    for i in range(20):
        from datetime import datetime
        stamp = datetime.fromtimestamp(1020 + i).isoformat()
        conn.execute('INSERT INTO savings_events VALUES (?,?,?,?,?)', (stamp, 'tool_archive_reexpand', 'session', 'id%d' % i, 100 - expand_call_overhead('id%d' % i)))
    conn.commit()
    conn.close()
    for i in range(20):
        record_expand(db, 'session', 'id%d' % i, now=1030 + i)
    assert not decide(db, 'mcp__one', active=True, now=1318)
    assert not decide(db, 'mcp__one', active=False, now=1400)
    assert decide(db, 'mcp__one', active=True, now=1400)
    assert not decide(db, 'mcp__two', active=True, now=1400)
    conn = sqlite3.connect(db)
    assert conn.execute('SELECT count(*) FROM mcp_archive_net_demotions').fetchone()[0] == 1
    conn.close()


def test_hook_shadow_and_active_modes(tmp_path):
    scripts = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
    home = tmp_path / 'home'
    snap = tmp_path / 'snap'
    home.mkdir()
    snap.mkdir()
    db = snap / 'trends.db'
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE mcp_archive_net_demotions (tool TEXT PRIMARY KEY, created REAL, samples INTEGER, archived INTEGER, reexpanded INTEGER, overhead INTEGER)')
    conn.execute('INSERT INTO mcp_archive_net_demotions VALUES (?,?,?,?,?,?)', ('mcp__demo__dump', 1, 20, 100, 100, 20))
    conn.commit()
    conn.close()
    env = dict(os.environ, HOME=str(home), TOKEN_OPTIMIZER_SNAPSHOT_DIR=str(snap), TOKEN_OPTIMIZER_RUNTIME='claude')
    payload = {'tool_name': 'mcp__demo__dump', 'session_id': 'session-1', 'tool_use_id': 'toolu_1',
               'tool_input': {}, 'tool_response': 'head\n' + 'x' * 6000 + '\ntail'}
    def run():
        return subprocess.run([sys.executable, str(scripts / 'archive_result.py')], input=json.dumps(payload),
                              env=env, text=True, capture_output=True, check=True).stdout
    out = json.loads(run())['hookSpecificOutput']['updatedMCPToolOutput']
    assert 'head' in out and 'tail' in out and 'expand toolu_1' in out
    for _ in range(2):
        subprocess.run([sys.executable, str(scripts / 'measure.py'), 'expand', 'toolu_1', '--session', 'session-1'],
                       env=env, text=True, capture_output=True, check=True)
    conn = sqlite3.connect(db)
    assert conn.execute('SELECT count(*) FROM mcp_archive_expand_calls').fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM savings_events WHERE event_type='tool_archive_reexpand'").fetchone()[0] == 1
    conn.close()
    env['TOKEN_OPTIMIZER_MCP_ARCHIVE_TRIPWIRE'] = 'active'
    payload['tool_use_id'] = 'toolu_2'
    assert run() == ''
