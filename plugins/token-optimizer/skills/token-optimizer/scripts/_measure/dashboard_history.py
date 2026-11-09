"""Persist the dashboard's embedded history window across rebuilds."""
import json
import os
import sqlite3
import tempfile
from datetime import date
from pathlib import Path

DEFAULT_DAYS = 90


def valid(value):
    return value == 'all' or (type(value) is int and 1 <= value <= 365000)


def load(runtime_dir):
    try:
        path = Path(runtime_dir) / 'token-optimizer' / 'dashboard-history.json'
        if path.is_symlink() or path.stat().st_size > 1024:
            return DEFAULT_DAYS
        value = json.loads(path.read_text(encoding='utf-8')).get('days')
        return value if valid(value) else DEFAULT_DAYS
    except (OSError, ValueError, AttributeError):
        return DEFAULT_DAYS


def save(runtime_dir, value):
    if not valid(value):
        raise ValueError('invalid dashboard history window')
    path = Path(runtime_dir) / 'token-optimizer' / 'dashboard-history.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.history-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump({'days': value}, handle)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def resolve_days(value, db_path, today=None):
    if value != 'all':
        return value if valid(value) else DEFAULT_DAYS
    today = today or date.today()
    earliest = None
    try:
        # URI mode=ro prevents an absent DB from being created as a side effect.
        uri = Path(db_path).resolve().as_uri() + '?mode=ro'
        with sqlite3.connect(uri, uri=True) as conn:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(session_log)')}
            for column in ('date', 'first_activity_day'):
                if column in columns:
                    value = conn.execute('SELECT MIN(' + column + ') FROM session_log').fetchone()[0]
                    if value:
                        day = date.fromisoformat(value[:10])
                        earliest = day if earliest is None else min(earliest, day)
            # Older databases may not yet have activity bounds. Inspect their
            # daily maps so an older day is not hidden by the session date.
            for column in ('daily_usage_json', 'parent_daily_usage_json', 'daily_calls_json'):
                if column not in columns:
                    continue
                condition = ' WHERE first_activity_day IS NULL' if 'first_activity_day' in columns else ''
                for (raw,) in conn.execute('SELECT ' + column + ' FROM session_log' + condition):
                    try:
                        for key in json.loads(raw or '{}'):
                            day = date.fromisoformat(key)
                            earliest = day if earliest is None else min(earliest, day)
                    except (ValueError, TypeError):
                        continue
    except (OSError, sqlite3.Error, ValueError, TypeError):
        pass
    # A missing or old DB falls back to a broad window for JSONL discovery.
    earliest = earliest or date(1900, 1, 1)
    return max(DEFAULT_DAYS, (today - earliest).days + 1)


def selected_days(runtime_dir, db_path):
    return resolve_days(load(runtime_dir), db_path)
