"""Explicit day evidence for synthetic SQL fixtures whose date means activity.

Only call this from a fixture that intentionally creates single-day work. Never
use it on production data or fixtures exercising missing/undated coverage.
"""
import json


def stamp_activity(conn, platforms=None):
    cols = {row[1] for row in conn.execute('PRAGMA table_info(session_log)')}
    if not cols:
        return
    for name in ('daily_usage_json', 'parent_daily_usage_json', 'first_activity_day', 'last_activity_day', 'daily_calls_json'):
        if name not in cols:
            conn.execute(f'ALTER TABLE session_log ADD COLUMN {name} TEXT')
    names = [row[1] for row in conn.execute('PRAGMA table_info(session_log)')]
    for raw in conn.execute('SELECT rowid,* FROM session_log').fetchall():
        rowid, row = raw[0], dict(zip(names, raw[1:]))
        if platforms is not None and row.get('platform') not in platforms:
            continue
        if row.get('daily_usage_json') is not None:
            continue
        day = row.get('date')
        if not day:
            continue
        def decoded(name):
            return json.loads(row.get(name) or '{}')
        models = decoded('model_usage_json') or decoded('all_model_usage_json')
        breakdown = decoded('model_usage_breakdown_json')
        if not breakdown:
            inp, out = row.get('input_tokens') or 0, row.get('output_tokens') or 0
            read = inp * (row.get('cache_hit_rate') or 0)
            h1, m5 = row.get('cache_create_1h_tokens') or 0, row.get('cache_create_5m_tokens') or 0
            read = min(read, max(0, inp-h1-m5))
            classes = {'fresh_input': max(0, inp-read-h1-m5), 'cache_read': read,
                       'cache_create': h1+m5, 'cache_create_1h': h1, 'cache_create_5m': m5, 'output': out}
            models = models or {'unknown': inp+out or 1}
            total = sum(models.values()) or 1
            breakdown = {model: {kind: value * weight / total for kind,value in classes.items()}
                         for model,weight in models.items()}
        usage = json.dumps({day: breakdown})
        conn.execute('UPDATE session_log SET daily_usage_json=?,parent_daily_usage_json=?,first_activity_day=?,last_activity_day=?,daily_calls_json=? WHERE rowid=?',
                     (usage,usage,day,day,json.dumps({day: row.get('api_calls') or 0}),rowid))
