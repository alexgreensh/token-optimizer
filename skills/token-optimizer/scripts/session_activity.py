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
    columns = {r[1] for r in conn.execute("PRAGMA table_info(session_log)")}
    if "daily_usage_json" not in columns:
        return {"complete": False, "undated_sessions": 0}
    parent_col = "parent_daily_usage_json" if "parent_daily_usage_json" in columns else "NULL"
    incomplete = 0
    for raw, parent in conn.execute(f"SELECT daily_usage_json,{parent_col} FROM session_log"):
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
    if conn.execute("SELECT 1 FROM main.sqlite_master WHERE type='view' AND name='session_activity'").fetchone():
        conn.execute("DROP VIEW main.session_activity")
    ensure_activity_view(conn)


def ensure_activity_view(conn):
    """Connection-local projection, including legacy/read-only database shapes."""
    if conn.execute("SELECT 1 FROM sqlite_temp_master WHERE type='view' AND name='session_activity'").fetchone():
        return
    columns = {r[1] for r in conn.execute("PRAGMA table_info(session_log)")}
    def source(name, default="NULL"):
        return "s." + name if name in columns else default
    def total(field):
        return f"COALESCE((SELECT SUM(COALESCE(json_extract(value, '$.{field}'),0)) FROM json_each(d.value)),0)"
    fresh, read, write, output = [total(k) for k in ("fresh_input", "cache_read", "cache_create", "output")]
    inp = f"({fresh}+{read}+{write})"
    model_mix = "(SELECT json_group_object(key, COALESCE(json_extract(value,'$.fresh_input'),0)+COALESCE(json_extract(value,'$.cache_create'),0)+COALESCE(json_extract(value,'$.output'),0)+COALESCE(json_extract(value,'$.unsplit'),0)) FROM json_each(d.value))"
    overrides = {
        "date": "d.key", "input_tokens": inp, "output_tokens": output,
        "cache_hit_rate": f"CASE WHEN {inp}>0 THEN 1.0*{read}/{inp} ELSE 0 END",
        "cache_create_1h_tokens": total("cache_create_1h"),
        "cache_create_5m_tokens": f"({write}-{total('cache_create_1h')})",
        "model_usage_json": model_mix, "all_model_usage_json": model_mix,
        "model_usage_breakdown_json": "d.value",
        "daily_usage_json": "json_object(d.key, json(d.value))",
        "api_calls": f"json_extract({source('daily_calls_json')}, '$.\"' || d.key || '\"')",
        # 1 only when dated per-day call evidence exists at all; a NULL
        # api_calls then means a measured zero for that day, not missing data.
        "api_calls_known": (f"CASE WHEN {source('daily_calls_json')} IS NOT NULL "
                            f"AND json_valid({source('daily_calls_json')}) THEN 1 ELSE 0 END"),
    }
    cohort_defaults = {
        "message_count": "0", "duration_minutes": "0", "skills_json": "'{}'",
        "subagents_json": "'{}'", "tool_calls_json": "'{}'", "stale_waste_tokens": "0",
        "quality_score": "NULL", "quality_grade": "NULL", "avg_call_gap_seconds": "NULL",
        "max_call_gap_seconds": "NULL", "p95_call_gap_seconds": "NULL",
    }
    for name, default in cohort_defaults.items():
        overrides[name] = f"CASE WHEN d.key={source('date')} THEN {source(name, default)} ELSE {default} END"
    for name in ("message_count", "duration_minutes", "skills_json", "subagents_json", "tool_calls_json"):
        key = name.removesuffix("_json")
        overrides[name] = f"COALESCE(json_extract({source('daily_metrics_json')}, '$.\"' || d.key || '\".{key}'), {overrides[name]})"
    # Optional ingestion/provenance columns are deliberately not dependencies of
    # this view. Their migrations can fail or be retried independently.
    for name in ("id", "jsonl_path", "project", "is_sidechain", "session_uuid", "slug", "topic", "collected_at", "version"):
        overrides[name] = source(name, "s.rowid" if name == "id" else "NULL")
    # Metadata-only activity days (user/tool activity with no billed usage that
    # day) still belong to the projection: they carry message/tool/skill counts
    # and hold the cohort fields on the session's last activity day. Token and
    # model columns stay zero/empty rather than inventing billed volume.
    metric_overrides = dict(overrides)
    metric_overrides.update({
        "input_tokens": "0", "output_tokens": "0", "cache_hit_rate": "0",
        "cache_create_1h_tokens": "0", "cache_create_5m_tokens": "0",
        "model_usage_json": "'{}'", "all_model_usage_json": "'{}'",
        "model_usage_breakdown_json": "'{}'", "daily_usage_json": "'{}'",
    })
    usage_projection = ", ".join(f'{expr} AS "{name}"' for name, expr in overrides.items())
    metric_projection = ", ".join(f'{expr} AS "{name}"' for name, expr in metric_overrides.items())
    usage = f"COALESCE({source('daily_usage_json')},{source('parent_daily_usage_json')})"
    metrics = source("daily_metrics_json")
    day_ok = ("length(d.key)=10 AND d.key GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
              "AND date(d.key)=d.key AND d.type='object'")
    conn.execute(f"""CREATE TEMP VIEW session_activity AS
        SELECT {usage_projection}
        FROM main.session_log s, json_each(CASE WHEN json_valid({usage})
             THEN {usage} ELSE '{{}}' END) d
        WHERE {day_ok}
        UNION ALL
        SELECT {metric_projection}
        FROM main.session_log s, json_each(CASE WHEN json_valid({metrics})
             THEN {metrics} ELSE '{{}}' END) d
        WHERE {day_ok}
          AND CASE WHEN json_valid({usage})
               THEN json_extract({usage}, '$.\"' || d.key || '\"') END IS NULL""")


def repair_activity_dates(conn):
    """Repair even missing-transcript rows from retained daily evidence, once."""
    if conn.execute("SELECT 1 FROM schema_migrations WHERE name='activity_dating_v1'").fetchone():
        return False
    conn.execute("BEGIN IMMEDIATE")
    if conn.execute("SELECT 1 FROM schema_migrations WHERE name='activity_dating_v1'").fetchone():
        conn.commit()
        return False
    columns = {r[1] for r in conn.execute("PRAGMA table_info(session_log)")}
    metrics_col = "daily_metrics_json" if "daily_metrics_json" in columns else "NULL"
    calls_col = "daily_calls_json" if "daily_calls_json" in columns else "NULL"
    api_col = "api_calls" if "api_calls" in columns else "NULL"
    updates = []
    call_updates = []
    for sid, raw, metrics_raw, api_calls, calls_raw in conn.execute(
            f"SELECT id,daily_usage_json,{metrics_col},{api_col},{calls_col} FROM session_log"):
        try:
            usage = json.loads(raw or "{}")
        except (ValueError, TypeError):
            usage = {}
        try:
            metrics = json.loads(metrics_raw or "{}")
        except (ValueError, TypeError):
            metrics = {}
        usage = usage if isinstance(usage, dict) else {}
        metrics = metrics if isinstance(metrics, dict) else {}
        days = sorted(set(activity_days(usage)) | set(activity_days(metrics)))
        updates.append((days[0] if days else None, days[-1] if days else None,
                        days[-1] if days else "", sid))
        # A session with exactly one proven activity day can carry its whole call
        # count on that day (the same allocation the adapter path uses). Sessions
        # with an undated bucket or several activity days keep NULL: per-day call
        # evidence that was never recorded stays unknown, never zero or split.
        if (calls_raw is None and len(days) == 1
                and isinstance(api_calls, (int, float)) and "undated" not in usage):
            call_updates.append((json.dumps({days[0]: int(api_calls)}), sid))
    conn.executemany("UPDATE session_log SET first_activity_day=?,last_activity_day=?,date=? WHERE id=?", updates)
    if call_updates:
        conn.executemany("UPDATE session_log SET daily_calls_json=? WHERE id=?", call_updates)
    return True
