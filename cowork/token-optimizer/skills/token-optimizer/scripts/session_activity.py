"""Dated usage projections. Ingestion timestamps never supply activity dates."""
import json
import sqlite3
from datetime import date


def activity_days(usage):
    days = []
    for day, models in usage.items():
        try:
            if date.fromisoformat(day).isoformat() == day and isinstance(models, dict):
                days.append(day)
        except (TypeError, ValueError):
            pass
    return sorted(days)


def activity_coverage(conn):
    incomplete = 0
    for raw, parent in conn.execute("SELECT daily_usage_json,parent_daily_usage_json FROM session_log"):
        try:
            usage = json.loads(raw) if raw is not None else None
        except (TypeError, ValueError):
            usage = None
        if not isinstance(usage, dict) or (not usage and parent is None) or len(activity_days(usage)) != len(usage):
            incomplete += 1
    return {"complete": incomplete == 0, "undated_sessions": incomplete}


def ensure_activity_schema(conn):
    columns = {r[1] for r in conn.execute("PRAGMA table_info(session_log)")}
    for name, kind in (("first_activity_day", "TEXT"), ("last_activity_day", "TEXT"),
                       ("transcript_mtime", "REAL"), ("parent_daily_usage_json", "TEXT"),
                       ("daily_calls_json", "TEXT"), ("daily_metrics_json", "TEXT")):
        if name not in columns:
            try:
                conn.execute(f"ALTER TABLE session_log ADD COLUMN {name} {kind}")
            except sqlite3.OperationalError:
                if name not in {r[1] for r in conn.execute("PRAGMA table_info(session_log)")}:
                    raise
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='view' AND name='session_activity'").fetchone():
        return
    # Each projection has the session_log interface, but token classes and models
    # describe just this day. Historical metadata is a last-active-day cohort,
    # never apportioned in proportion to tokens or copied onto every day.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(session_log)")]
    def total(field):
        return f"COALESCE((SELECT SUM(COALESCE(json_extract(value, '$.{field}'),0)) FROM json_each(d.value)),0)"
    fresh, read, write, output = [total(k) for k in ("fresh_input", "cache_read", "cache_create", "output")]
    inp = f"({fresh}+{read}+{write})"
    model_mix = "(SELECT json_group_object(key, COALESCE(json_extract(value,'$.fresh_input'),0)+COALESCE(json_extract(value,'$.cache_create'),0)+COALESCE(json_extract(value,'$.output'),0)) FROM json_each(d.value))"
    overrides = {
        "date": "d.key", "input_tokens": inp, "output_tokens": output,
        "cache_hit_rate": f"CASE WHEN {inp}>0 THEN 1.0*{read}/{inp} ELSE 0 END",
        "cache_create_1h_tokens": total("cache_create_1h"),
        # Older buckets may have unsplit cache writes, retained at the 5m rate.
        "cache_create_5m_tokens": f"({write}-{total('cache_create_1h')})",
        "model_usage_json": model_mix, "all_model_usage_json": model_mix,
        "model_usage_breakdown_json": "d.value",
        "daily_usage_json": "json_object(d.key, json(d.value))",
        "api_calls": "json_extract(s.daily_calls_json, '$.\"' || d.key || '\"')",
    }
    cohort_defaults = {
        "message_count": "0", "duration_minutes": "0", "skills_json": "'{}'",
        "subagents_json": "'{}'", "tool_calls_json": "'{}'", "stale_waste_tokens": "0",
        "quality_score": "NULL", "quality_grade": "NULL", "avg_call_gap_seconds": "NULL",
        "max_call_gap_seconds": "NULL", "p95_call_gap_seconds": "NULL",
    }
    for name, default in cohort_defaults.items():
        overrides[name] = f"CASE WHEN d.key=s.date THEN s.{name} ELSE {default} END"
    # New transcripts carry dated messages/tools/duration. Legacy metadata keeps
    # the explicitly documented session-cohort semantics above.
    for name in ("message_count", "duration_minutes", "skills_json", "subagents_json", "tool_calls_json"):
        key = name.removesuffix("_json")
        overrides[name] = f"COALESCE(json_extract(s.daily_metrics_json, '$.\"' || d.key || '\".{key}'), {overrides[name]})"
    projection = ", ".join(f'{overrides.get(c, "s."+c)} AS "{c}"' for c in cols)
    conn.execute(f"""CREATE VIEW IF NOT EXISTS session_activity AS SELECT {projection}
        FROM session_log s, json_each(CASE WHEN json_valid(s.daily_usage_json)
             THEN s.daily_usage_json ELSE '{{}}' END) d
        WHERE length(d.key)=10 AND d.key GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
          AND date(d.key)=d.key AND d.type='object'""")


def repair_activity_dates(conn):
    """Repair even missing-transcript rows from retained daily evidence, once."""
    if conn.execute("SELECT 1 FROM schema_migrations WHERE name='activity_dating_v1'").fetchone():
        return False
    conn.execute("BEGIN IMMEDIATE")
    if conn.execute("SELECT 1 FROM schema_migrations WHERE name='activity_dating_v1'").fetchone():
        conn.commit()
        return False
    updates = []
    for sid, raw in conn.execute("SELECT id,daily_usage_json FROM session_log"):
        try:
            usage = json.loads(raw or "{}")
        except (ValueError, TypeError):
            usage = {}
        days = activity_days(usage) if isinstance(usage, dict) else []
        updates.append((days[0] if days else None, days[-1] if days else None,
                        days[-1] if days else "", sid))
    conn.executemany("UPDATE session_log SET first_activity_day=?,last_activity_day=?,date=? WHERE id=?", updates)
    return True
