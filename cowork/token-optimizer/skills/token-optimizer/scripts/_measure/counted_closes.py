"""Durable daily savings balances and the reasons for any downward revision."""
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import math
import os
import sqlite3

_HELD_KEY = "counted_held_since"
_OVERALL_EVIDENCE_KEY = "overall_close_evidence"
_RECOUNT = "A recount could not reproduce the earlier figure for this day."
_EMPTY = {"usd": 0.0, "tokens": 0, "since": None, "held_days": 0, "last_correction": None}


def selected_rows(facade, conn):
    """Share the lifetime cohort with the period summary before its date filter."""
    excluded = set()
    if facade._ensure_counted_views(conn):
        for prefix, table in (("se", "savings_events"), ("ce", "compression_events")):
            excluded.update(row[0] for row in conn.execute(
                f"SELECT '{prefix}:'||id FROM main.{table} EXCEPT "
                f"SELECT '{prefix}:'||id FROM counted_{table}"))
    stored = facade._counted_stored_rows(conn)
    candidates = facade._counted_candidate_events(conn)
    orphaned = facade._counted_pricing_v5.orphaned_keys(
        stored.values(), {event[0] for event in candidates}, conn)
    rows = [row for key, row in stored.items() if key not in excluded and key not in orphaned]
    return rows, stored, candidates, orphaned


def awaiting_rescore(facade, rows):
    return [row for row in rows if row.get("session_uuid") and facade._counted_row_awaits_rescore(row)]


def _removal_day(row):
    for value in (str(row["event_ts"] or "")[:10], f"{row['event_month']}-01"):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    # The lifetime rollup counts legacy rows that have no usable timestamp.
    # Keep their value without claiming a start date.
    return ""


def _per_day(facade, conn, rows):
    groups = defaultdict(list)
    for row in rows:
        groups[_removal_day(row)].append(row)
    memo = {}
    totals = {}
    for day, day_rows in groups.items():
        rollup = facade._counted_rollup_rows(day_rows, conn, memo, for_close=True)
        usd = rollup["total_usd"]
        if not math.isfinite(usd):
            raise ValueError("invalid counted amount")
        totals[day] = usd, rollup["removed_tokens"] + rollup["reread_tokens"]
    return totals


def per_day(facade, conn):
    """Count initial removals and repeat reads on the original removal day."""
    rows, *_ = selected_rows(facade, conn)
    return _per_day(facade, conn, rows)


@contextmanager
def _transaction(conn):
    """Close against a single snapshot and preserve the caller's transaction."""
    nested = conn.in_transaction
    conn.execute("SAVEPOINT savings_close" if nested else "BEGIN")
    try:
        yield
        if nested:
            conn.execute("RELEASE savings_close")
        else:
            conn.commit()
    except Exception:
        if nested:
            conn.execute("ROLLBACK TO savings_close")
            conn.execute("RELEASE savings_close")
        else:
            conn.rollback()
        raise


def _create_counted(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS counted_day_close (
        day TEXT PRIMARY KEY, usd REAL NOT NULL, tokens INTEGER NOT NULL,
        pricing_version TEXT, updated_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS counted_corrections (
        id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, applied_at TEXT NOT NULL,
        from_usd REAL NOT NULL, to_usd REAL NOT NULL, reason_code TEXT NOT NULL, reason_text TEXT NOT NULL)""")
    conn.execute("CREATE TABLE IF NOT EXISTS token_optimizer_meta (key TEXT PRIMARY KEY, value TEXT)")


def _decode_held(value):
    held = json.loads(value or "{}")
    if not isinstance(held, dict):
        raise ValueError("invalid counted hold state")
    return held


def _held(conn):
    row = conn.execute("SELECT value FROM token_optimizer_meta WHERE key = ?", (_HELD_KEY,)).fetchone()
    return _decode_held(row[0]) if row else {}


def _decrease(previous, current, old_version, version, pending, held_since, today, recount):
    """Return the hold date and any correction that releases the lower value."""
    if current >= previous - 0.005:
        return None, None
    if old_version != version and not pending:
        return None, ("repricing",
                      f"Prices were updated (version {old_version} to {version}). Earlier days were re-priced.")
    first = held_since or today.isoformat()
    if (today - date.fromisoformat(first)).days >= 7:
        return None, ("recount", recount)
    return first, None


def _write_correction(conn, tier, day, stamp, before, after, reason):
    conn.execute(f"""INSERT INTO {tier}_corrections
        (day, applied_at, from_usd, to_usd, reason_code, reason_text)
        VALUES (?, ?, ?, ?, ?, ?)""", (day, stamp, before, after, *reason))


def _refresh_counted(facade, conn, today, stamp, version):
    rows, source_rows, *_ = selected_rows(facade, conn)
    pending = bool(awaiting_rescore(facade, rows))
    amounts = _per_day(facade, conn, rows)
    _create_counted(conn)
    stored = {day: (usd, tokens, pricing) for day, usd, tokens, pricing in conn.execute(
        "SELECT day, usd, tokens, pricing_version FROM counted_day_close")}
    held = _held(conn)
    prior_held = held.copy()
    # A filtered row can support a correction to zero. An absent row cannot.
    missing = stored.keys() - {_removal_day(row) for row in source_rows.values()}
    for day in missing:
        held.pop(day, None)
    written = 0
    for day in sorted(amounts.keys() | (stored.keys() - missing)):
        current, tokens = amounts.get(day, (0.0, 0))
        saved_version = version
        if day in stored:
            previous, previous_tokens, old_version = stored[day]
            first, correction = _decrease(previous, current, old_version, version, pending,
                                          held.get(day), today, _RECOUNT)
            if first:
                held[day] = first
            else:
                held.pop(day, None)
            if correction:
                _write_correction(conn, "counted", day, stamp, previous, current, correction)
                written += 1
            elif current <= previous:
                tokens = max(tokens, previous_tokens) if current == previous else previous_tokens
                current = previous
            if pending and not correction:
                saved_version = old_version
            if (current, tokens, saved_version) == stored[day]:
                continue
        elif current < 0:
            _write_correction(conn, "counted", day, stamp, 0.0, current,
                              ("net_debit", "Recorded costs exceeded the savings for this day."))
            written += 1
        conn.execute("""INSERT INTO counted_day_close (day, usd, tokens, pricing_version, updated_at)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT(day) DO UPDATE SET
            usd=excluded.usd, tokens=excluded.tokens, pricing_version=excluded.pricing_version,
            updated_at=excluded.updated_at""", (day, current, tokens, saved_version, stamp))
    if held != prior_held:
        conn.execute("INSERT OR REPLACE INTO token_optimizer_meta(key, value) VALUES (?, ?)",
                     (_HELD_KEY, json.dumps(held, sort_keys=True)))
    return written, pending, missing


def _recorded_costs(facade, conn, since, now):
    """Read the cost evidence eligible for the same since-install summary."""
    table = facade._counted_table(conn, "savings_events")
    compaction = conn.execute(f"""SELECT COALESCE(-SUM(cost_saved_usd), 0)
        FROM {table} WHERE event_type = 'autocompact_window_estimated'
        AND timestamp >= ? AND cost_saved_usd < 0""", (since,)).fetchone()[0]
    start = facade._keepwarm_iso_to_epoch(since)
    warm = facade.keepwarm_spend_summary(days=max(0, now - start) / 86400, now=now)["spend_usd"]
    return {"keepwarm": warm, "autocompact": compaction}


def _historical_sources(facade, conn, since, seen, *, recheck):
    """Identify evidence that a fresh recount may no longer be able to read."""
    from . import cold_guard

    sources = set()
    for key, path in conn.execute("""SELECT 'session:' || COALESCE(NULLIF(session_uuid, ''), id), jsonl_path
        FROM session_log WHERE date >= ? AND jsonl_path IS NOT NULL""", (since,)):
        if key not in sources and ((key in seen and not recheck) or os.path.isfile(path)):
            sources.add(key)
    start = facade._keepwarm_iso_to_epoch(since)
    for _, marker in cold_guard._markers(facade):
        for block in cold_guard._blocks(marker):
            if block.get("choice") == "fresh" and block.get("fresh_session_id") and block["told_at"] >= start:
                sources.add(f"cold_guard:{block['fresh_session_id']}:{block['told_at']}")
    return sources


def _overall_evidence(facade, conn, since, now, current, previous):
    row = conn.execute("SELECT value FROM token_optimizer_meta WHERE key = ?", (_OVERALL_EVIDENCE_KEY,)).fetchone()
    prior = _decode_held(row[0]) if row else {}
    costs = _recorded_costs(facade, conn, since, now)
    debit = 0.0
    if prior.get("since") == since:
        new_costs = sum(costs.values()) - sum(prior["costs"].values())
        # A fraction of a cent can move the displayed total by one cent.
        debit_limit = math.ceil(round(new_costs * 100, 6)) / 100
        debit = round(max(0.0, min(debit_limit, prior["usd"] - current, previous - current)), 2)
    seen = set(prior.get("sources", ()))
    sources = _historical_sources(facade, conn, since, seen, recheck=current < previous)
    snapshot = {"since": since, "costs": costs, "usd": current, "sources": sorted(seen | sources)}
    if snapshot != prior:
        conn.execute("INSERT OR REPLACE INTO token_optimizer_meta(key, value) VALUES (?, ?)",
                     (_OVERALL_EVIDENCE_KEY, json.dumps(snapshot, separators=(",", ":"))))
    return debit, bool(seen - sources)


def _refresh_overall(facade, conn, since_install, monthly, now, version, pending, missing):
    # This sum includes lifetime estimates whose model mix spans the whole
    # window. Store the balance observed each day. Adding daily balances (or
    # pricing slices separately) would count the same savings more than once.
    all_in = since_install.get("all_in") or {}
    if all_in.get("total_usd") is None or not since_install.get("install_date"):
        return
    try:
        current = float(all_in["total_usd"])
        since = date.fromisoformat(since_install["install_date"]).isoformat()
    except (TypeError, ValueError):
        return
    if not math.isfinite(current):
        return
    conn.execute("""CREATE TABLE IF NOT EXISTS overall_day_close (
        day TEXT PRIMARY KEY, usd REAL NOT NULL, since TEXT NOT NULL, pricing_version TEXT,
        held_since TEXT, estimated_usd REAL, last_30d_usd REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS overall_corrections (
        id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, applied_at TEXT NOT NULL,
        from_usd REAL NOT NULL, to_usd REAL NOT NULL, reason_code TEXT NOT NULL, reason_text TEXT NOT NULL)""")
    prior = conn.execute("""SELECT day, usd, since, pricing_version, held_since, estimated_usd, last_30d_usd
        FROM overall_day_close ORDER BY day DESC LIMIT 1""").fetchone()
    today, stamp = now.date(), now.isoformat()
    day, held_since = today.isoformat(), None
    estimate, rolling = monthly
    pending = pending or all_in.get("remeasuring") or all_in.get("routing_pending")
    debit, missing_sources = _overall_evidence(
        facade, conn, since, now.timestamp(), current, prior[1] if prior else current)
    if prior:
        old_day, previous, old_since, old_version, first, old_estimate, old_rolling = prior
        estimate = old_estimate if estimate is None else estimate
        rolling = old_rolling if rolling is None else rolling
        # A wall-clock rollback must not make a later close unreachable.
        day = max(day, old_day)
        since = min(since, old_since)
        if debit:
            after_debit = round(previous - debit, 2)
            _write_correction(conn, "overall", day, stamp, previous, after_debit,
                              ("net_debit", "New recorded costs reduced since-install savings."))
            previous = after_debit
        if missing_sources or any(day >= since for day in missing):
            # Aging out of the source window is not evidence for a restatement.
            current = max(previous, current)
            version = old_version
        else:
            held_since, correction = _decrease(
                previous, current, old_version, version, pending, first, today,
                "A recount could not reproduce the earlier since-install savings total.")
            if correction:
                _write_correction(conn, "overall", day, stamp, previous, current, correction)
            else:
                current = max(previous, current)
                if pending:
                    version = old_version
    conn.execute("""INSERT OR REPLACE INTO overall_day_close
        (day, usd, since, pricing_version, held_since, estimated_usd, last_30d_usd)
        VALUES (?, ?, ?, ?, ?, ?, ?)""", (day, current, since, version, held_since, estimate, rolling))


def refresh(facade, conn, now=None, *, since_install=None, monthly=(None, None)):
    """Close the counted days, and the overall balance when a full summary exists."""
    written = 0
    try:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        version = str(facade._COUNTED_PRICING_VERSION)
        with _transaction(conn):
            written, pending, missing = _refresh_counted(facade, conn, now.date(), now.isoformat(), version)
            if since_install:
                _refresh_overall(facade, conn, since_install, monthly, now, version, pending, missing)
    except Exception:
        written = 0
    return {k: v for k, v in total(conn).items() if k != "last_correction"} | {"corrections_written": written}


def _correction(date_applied, before, after, reason):
    if not date_applied:
        return None
    return {"date": date_applied[:10], "delta_usd": round(after - before, 6), "reason": reason}


def total(conn):
    """Read the counted balance and latest correction in one SQLite snapshot."""
    try:
        usd, tokens, since, held, stamp, before, after, reason = conn.execute("""
            WITH totals AS (
                SELECT SUM(usd) AS usd, SUM(tokens) AS tokens, MIN(NULLIF(day, '')) AS since
                FROM counted_day_close
            )
            SELECT usd, tokens, since, (SELECT value FROM token_optimizer_meta WHERE key = ?),
                   applied_at, from_usd, to_usd, reason_text
            FROM totals LEFT JOIN (
                SELECT applied_at, from_usd, to_usd, reason_text
                FROM counted_corrections ORDER BY applied_at DESC, id DESC LIMIT 1
            ) ON 1 = 1""", (_HELD_KEY,)).fetchone()
        try:
            held_days = len(_decode_held(held))
        except ValueError:
            held_days = 0
        return {"usd": round(usd or 0.0, 6), "tokens": tokens or 0, "since": since,
                "held_days": held_days, "last_correction": _correction(stamp, before, after, reason)}
    except (sqlite3.Error, AttributeError):
        return dict(_EMPTY)


def total_overall(conn):
    """Read the newest daily all-in balance, including its rolling context."""
    try:
        row = conn.execute("""
            SELECT usd, since, held_since, estimated_usd, last_30d_usd, applied_at, from_usd, to_usd, reason_text
            FROM (SELECT * FROM overall_day_close ORDER BY day DESC LIMIT 1)
            LEFT JOIN (SELECT applied_at, from_usd, to_usd, reason_text
                       FROM overall_corrections ORDER BY applied_at DESC, id DESC LIMIT 1) ON 1 = 1""").fetchone()
    except (sqlite3.Error, AttributeError):
        return None
    if row is None:
        return None
    usd, since, held, estimated, monthly, stamp, before, after, reason = row
    return {"usd": usd, "tokens": 0, "since": since, "held_days": int(bool(held)),
            "last_correction": _correction(stamp, before, after, reason),
            "estimated_usd": estimated, "last_30d_usd": monthly}


def corrections(conn, now, *, overall=False):
    table = "overall_corrections" if overall else "counted_corrections"
    try:
        rows = conn.execute(f"""SELECT applied_at, from_usd, to_usd, reason_text FROM {table}
            WHERE substr(applied_at, 1, 10) BETWEEN date(?, '-90 days') AND ?
            ORDER BY applied_at DESC, id DESC""", (now.isoformat(), now.isoformat()))
        return [_correction(*row) for row in rows]
    except (sqlite3.Error, AttributeError):
        return []


def register_correction(conn, day, to_usd, reason_code, reason_text):
    """Explicitly restate an existing counted day, atomically with the audit row."""
    stamp = datetime.now(timezone.utc).isoformat()
    to_usd = float(to_usd)
    if not math.isfinite(to_usd) or not reason_code.strip() or not reason_text.strip():
        raise ValueError("a correction needs a finite amount and a reason")
    with _transaction(conn):
        _create_counted(conn)
        row = conn.execute("SELECT usd FROM counted_day_close WHERE day = ?", (day,)).fetchone()
        if row is None or row[0] == to_usd:
            return False
        _write_correction(conn, "counted", day, stamp, row[0], to_usd, (reason_code, reason_text))
        conn.execute("UPDATE counted_day_close SET usd = ?, updated_at = ? WHERE day = ?", (to_usd, stamp, day))
        held = _held(conn)
        if day in held:
            del held[day]
            conn.execute("UPDATE token_optimizer_meta SET value = ? WHERE key = ?",
                         (json.dumps(held, sort_keys=True), _HELD_KEY))
        return True


def on_savings_refresh(facade, summary):
    """Refresh at the scaffold's savings-only entry point, then publish both balances."""
    from . import counted_headline
    try:
        if facade.TRENDS_DB.exists():
            uri = facade.TRENDS_DB.resolve().as_uri() + "?mode=rw"
            conn = facade.sqlite3.connect(uri, uri=True, timeout=0.25)
            try:
                action = summary.get("action_savings") or {}
                since = summary.get("since_install") or {}
                all_in = since.get("all_in") or {}
                window_usd = action.get("total_usd") if summary.get("period_days") == 30 else None
                refresh(facade, conn, since_install=since,
                        monthly=(all_in.get("estimated_usd"), window_usd))
            finally:
                conn.close()
    except (OSError, sqlite3.Error):
        pass
    summary["counted_headline"] = counted_headline.build(facade)
    summary["overall_headline"] = counted_headline.build_overall(facade)
