"""Definition-only implementations; mutable state belongs to measure."""
from __future__ import annotations
from . import _compat


def _classify_jsonl_is_sidechain(filepath, max_lines=None):
    """Return ``(is_sidechain, reason)`` using only authoritative signals."""
    _measure_facade = _compat.facade()
    path_reason = _measure_facade._sidechain_path_reason(filepath)
    if path_reason:
        return (True, path_reason)
    try:
        parsed = 0
        first_user_record_seen = False
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                # Cap on parsed records, not raw lines, so blank/garbage lines
                # do not consume the scan budget before a real record is seen.
                if parsed >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _measure_facade.json.loads(line)
                except _measure_facade.json.JSONDecodeError:
                    continue
                parsed += 1
                if rec.get("isSidechain") is True:
                    return (True, "isSidechain")
                # The OSRC protocol is meaningful only in the first real user
                # prompt. Tool-result transport and later assistant/user echoes
                # are orchestrator activity, not proof of a delegated worker.
                if rec.get("type") == "user" and not first_user_record_seen:
                    prompt = _measure_facade._osrc_prompt_text(rec)
                    if prompt:
                        first_user_record_seen = True
                    if prompt and ("OSRC::PROGRESS" in prompt or "OSRC::DONE" in prompt):
                        return (True, "osrc_prompt")
        return (False, None)
    except (OSError, ValueError):
        return (None, None)


def _scan_jsonl_is_sidechain(filepath, max_lines=None):
    """Return True/False for a readable transcript, otherwise None."""
    _measure_facade = _compat.facade()
    verdict, _reason = _measure_facade._classify_jsonl_is_sidechain(filepath, max_lines=max_lines)
    return verdict


def _backfill_is_sidechain(conn, batch_limit=None):
    """Reclassify legacy rows once with the corrected classifier.

    The persistent gate is required because ``sidechain_reason`` is NULL for a
    legitimate human row after repair, so NULL alone cannot be a durable
    unclassified sentinel. Missing transcripts remain unchanged and are counted
    rather than guessed.

    Batched with a resume cursor: if the process is killed mid-backfill (hook
    timeout), the next _init_trends_db resumes from the last-processed id
    instead of re-reading every row from scratch. The done-marker is only
    written when the cursor reaches the end.
    """
    _measure_facade = _compat.facade()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS token_optimizer_meta "
        "(key TEXT PRIMARY KEY, value TEXT)"
    )
    if conn.execute(
        "SELECT 1 FROM token_optimizer_meta WHERE key = 'sidechain_classifier_v2_done'"
    ).fetchone() is not None:
        return (0, 0)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(session_log)").fetchall()}
    has_reason = "sidechain_reason" in cols
    # Resume from the last-processed id if a prior pass was interrupted.
    cursor_row = conn.execute(
        "SELECT value FROM token_optimizer_meta WHERE key = 'sidechain_classifier_v2_cursor'"
    ).fetchone()
    cursor_id = int(cursor_row[0]) if cursor_row else 0
    rows = conn.execute(
        "SELECT id, jsonl_path FROM session_log "
        "WHERE jsonl_path IS NOT NULL AND id > ? "
        "ORDER BY id LIMIT ?",
        (cursor_id, batch_limit),
    ).fetchall()
    if not rows:
        # No more rows to process: mark done and clear the cursor.
        conn.execute(
            "INSERT OR REPLACE INTO token_optimizer_meta (key, value) "
            "VALUES ('sidechain_classifier_v2_done', datetime('now'))"
        )
        conn.execute(
            "DELETE FROM token_optimizer_meta WHERE key = 'sidechain_classifier_v2_cursor'"
        )
        conn.commit()
        return (0, 0)
    updates = []
    missing = 0
    last_id = cursor_id
    for row_id, jpath in rows:
        verdict, reason = _measure_facade._classify_jsonl_is_sidechain(jpath)
        last_id = row_id
        if verdict is None:
            missing += 1
            continue
        if has_reason:
            updates.append((1 if verdict else 0, reason, row_id))
        else:
            updates.append((1 if verdict else 0, row_id))
    if updates:
        if has_reason:
            conn.executemany(
                "UPDATE session_log SET is_sidechain = ?, sidechain_reason = ? WHERE id = ?",
                updates,
            )
        else:
            conn.executemany(
                "UPDATE session_log SET is_sidechain = ? WHERE id = ?", updates
            )
    # Persist the cursor so a killed process resumes here next time.
    conn.execute(
        "INSERT OR REPLACE INTO token_optimizer_meta (key, value) "
        "VALUES ('sidechain_classifier_v2_cursor', ?)",
        (str(last_id),),
    )
    # If we processed fewer than batch_limit rows, we've reached the end.
    if len(rows) < batch_limit:
        conn.execute(
            "INSERT OR REPLACE INTO token_optimizer_meta (key, value) "
            "VALUES ('sidechain_classifier_v2_done', datetime('now'))"
        )
        conn.execute(
            "DELETE FROM token_optimizer_meta WHERE key = 'sidechain_classifier_v2_cursor'"
        )
    conn.commit()
    return (len(updates), missing)


def _backfill_reported_token_usage(conn, batch_limit=None):
    """Persist the official-compatible, non-deduped usage basis for old rows.

    Batched with a resume cursor (see _backfill_is_sidechain for rationale).
    """
    _measure_facade = _compat.facade()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS token_optimizer_meta "
        "(key TEXT PRIMARY KEY, value TEXT)"
    )
    if conn.execute(
        "SELECT 1 FROM token_optimizer_meta WHERE key = 'reported_token_backfill_done'"
    ).fetchone() is not None:
        return 0

    cursor_row = conn.execute(
        "SELECT value FROM token_optimizer_meta WHERE key = 'reported_token_backfill_cursor'"
    ).fetchone()
    cursor_id = int(cursor_row[0]) if cursor_row else 0
    rows = conn.execute(
        "SELECT id, jsonl_path FROM session_log "
        "WHERE jsonl_path IS NOT NULL AND id > ? "
        "ORDER BY id LIMIT ?",
        (cursor_id, batch_limit),
    ).fetchall()
    if not rows:
        conn.execute(
            "INSERT OR REPLACE INTO token_optimizer_meta (key, value) "
            "VALUES ('reported_token_backfill_done', datetime('now'))"
        )
        conn.execute(
            "DELETE FROM token_optimizer_meta WHERE key = 'reported_token_backfill_cursor'"
        )
        conn.commit()
        return 0
    updates = []
    last_id = cursor_id
    for row_id, jpath in rows:
        parsed = _measure_facade._parse_session_jsonl(jpath)
        last_id = row_id
        if not parsed:
            continue
        updates.append((
            int(parsed.get("reported_input_tokens", 0) or 0),
            int(parsed.get("reported_output_tokens", 0) or 0),
            _measure_facade.json.dumps(parsed.get("reported_model_usage", {})),
            row_id,
        ))
    if updates:
        conn.executemany(
            "UPDATE session_log SET reported_input_tokens = ?, "
            "reported_output_tokens = ?, reported_model_usage_json = ? WHERE id = ?",
            updates,
        )
    conn.execute(
        "INSERT OR REPLACE INTO token_optimizer_meta (key, value) "
        "VALUES ('reported_token_backfill_cursor', ?)",
        (str(last_id),),
    )
    if len(rows) < batch_limit:
        conn.execute(
            "INSERT OR REPLACE INTO token_optimizer_meta (key, value) "
            "VALUES ('reported_token_backfill_done', datetime('now'))"
        )
        conn.execute(
            "DELETE FROM token_optimizer_meta WHERE key = 'reported_token_backfill_cursor'"
        )
    conn.commit()
    return len(updates)


def _backfill_outsourcerer_sidechain(conn):
    """Reclassify existing outsourcerer delegation sessions as is_sidechain=1.

    One-time migration for rows collected before content-based detection existed.
    Scans each already-human (is_sidechain=0) row's JSONL for the OSRC::PROGRESS
    marker injected by the outsourcerer skill and flips true matches to 1.

    Gated by a persistent completion marker so it content-scans transcripts at
    most ONCE. Without the gate the `is_sidechain = 0` predicate matches every
    human session forever (non-matches are never updated, so they never drain),
    which re-opened and re-read up to 200 lines of every human transcript on
    every _init_trends_db() call (~38 call sites; twice per SessionEnd). New
    sessions are classified at collect time, so a single pass over the
    pre-existing rows is sufficient. Returns count of reclassified rows.
    """
    # One-time gate. On any error establishing it, skip the scan entirely rather
    # than risk re-reading the whole history (fail toward NOT rescanning).
    _measure_facade = _compat.facade()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS token_optimizer_meta "
            "(key TEXT PRIMARY KEY, value TEXT)"
        )
        if conn.execute(
            "SELECT 1 FROM token_optimizer_meta WHERE key = 'osrc_backfill_done'"
        ).fetchone() is not None:
            return 0
    except _measure_facade.sqlite3.Error:
        return 0

    rows = conn.execute(
        "SELECT id, jsonl_path FROM session_log "
        "WHERE is_sidechain = 0 AND jsonl_path IS NOT NULL"
    ).fetchall()
    updates = []
    for row_id, jpath in rows:
        verdict = _measure_facade._scan_jsonl_is_sidechain(jpath)
        if verdict is True:
            updates.append((1, row_id))
    if updates:
        conn.executemany(
            "UPDATE session_log SET is_sidechain = ? WHERE id = ?", updates
        )
    # Mark the pass complete regardless of match count so it never re-scans.
    conn.execute(
        "INSERT OR REPLACE INTO token_optimizer_meta (key, value) "
        "VALUES ('osrc_backfill_done', datetime('now'))"
    )
    conn.commit()
    return len(updates)


def _repair_osrc_sidechain_misflag(conn):
    """Undo rows the OLD, over-broad OSRC marker test wrongly flagged.

    Until this fix, both marker tests searched the WHOLE record (and the raw
    line) for OSRC::PROGRESS / OSRC::DONE. The marker therefore matched inside
    tool RESULTS, so a human session that merely *watched* a delegate (running
    `outsourcerer.sh status/watch/result`, whose output echoes the marker)
    classified itself as the delegation it was watching. On the author's own
    machine that mislabelled 234 of 3,312 30-day rows holding 8.87B of 15.5B
    input tokens (57%) -- the longest genuine working sessions -- and removed
    them from the human pool every cost comparison runs on.

    Fixing the classifier is not enough: `_backfill_outsourcerer_sidechain` is
    gated by a persistent `osrc_backfill_done` marker, so the corrected code
    would never revisit the rows the buggy code already wrote. Code fixed, data
    still wrong. This pass re-tests only rows currently flagged is_sidechain=1
    with the corrected scanner and flips back the ones that no longer qualify.

    Direction is deliberately one-way: it can only move 1 -> 0. A row that still
    matches under the corrected rule is left alone, and no row is ever newly
    flagged here, so a bug in this pass cannot manufacture sidechains.

    Gated by its own marker so it runs at most once, matching the discipline of
    `_backfill_outsourcerer_sidechain` (an ungated content-scan re-reads every
    flagged transcript on every _init_trends_db call). Returns count repaired.
    """
    _measure_facade = _compat.facade()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS token_optimizer_meta "
            "(key TEXT PRIMARY KEY, value TEXT)"
        )
        if conn.execute(
            "SELECT 1 FROM token_optimizer_meta WHERE key = 'osrc_misflag_repair_done'"
        ).fetchone() is not None:
            return 0
    except _measure_facade.sqlite3.Error:
        return 0

    try:
        rows = conn.execute(
            "SELECT id, jsonl_path FROM session_log "
            "WHERE is_sidechain = 1 AND jsonl_path IS NOT NULL"
        ).fetchall()
    except _measure_facade.sqlite3.Error:
        return 0

    updates = []
    for row_id, jpath in rows:
        verdict = _measure_facade._scan_jsonl_is_sidechain(jpath)
        # Only an explicit False repairs. None (file gone/unreadable) leaves the
        # row as-is: we cannot prove it was misflagged, so we do not guess.
        if verdict is False:
            updates.append((0, row_id))
    if updates:
        conn.executemany(
            "UPDATE session_log SET is_sidechain = ? WHERE id = ?", updates
        )
    conn.execute(
        "INSERT OR REPLACE INTO token_optimizer_meta (key, value) "
        "VALUES ('osrc_misflag_repair_done', datetime('now'))"
    )
    conn.commit()
    return len(updates)


def _recompute_session_tokens(conn, rel_tol=None, limit=None):
    """Re-parse stored sessions with the CURRENT collector and refresh stale token columns.

    Rows collected by older collector versions (before the streaming-aware
    per-requestId dedup) carry token counts that disagree with a fresh parse —
    some over-counted (summed streaming chunks), some under-counted. Because rows
    are written INSERT OR IGNORE and never updated, that staleness persists and
    skews every per-session cost derived from session_log. This re-parses each
    row whose transcript still exists and UPDATEs input_tokens / output_tokens /
    cache_create_* / cache_hit_rate when they differ by more than `rel_tol`.

    Idempotent: a row already matching the current parse is skipped. Rows whose
    transcript is gone are left untouched (their stored values are the best we
    have). Returns (refreshed, checked, missing). NOT run from _init_trends_db
    (a full re-parse of every transcript is expensive); invoke via the
    `recompute-tokens` command or a maintenance pass.
    """
    _measure_facade = _compat.facade()
    rows = conn.execute(
        "SELECT id, jsonl_path, input_tokens FROM session_log "
        "WHERE jsonl_path IS NOT NULL ORDER BY id DESC"
    ).fetchall()
    if limit:
        rows = rows[:limit]
    refreshed = checked = missing = 0
    updates = []
    for row_id, jpath, stored_input in rows:
        if not jpath or not _measure_facade.os.path.exists(jpath):
            missing += 1
            continue
        parsed = _measure_facade._parse_session_jsonl(jpath)
        if not parsed:
            missing += 1
            continue
        checked += 1
        new_input = int(parsed.get("total_input_tokens", 0) or 0)
        stored = int(stored_input or 0)
        # Only update when the fresh parse has a positive input. The old condition
        # `stored <= 0 or ...` fired for stored==0 even when the new parse was also 0,
        # overwriting valid cache_create_* on legitimately zero-input rows (e.g. the
        # Codex path, where input_tokens can be 0 but cache_create is real). Requiring
        # new_input > 0 keeps those rows intact while still catching every stale row.
        if new_input > 0 and (stored <= 0 or abs(new_input - stored) > stored * rel_tol):
            updates.append((
                new_input,
                int(parsed.get("total_output_tokens", 0) or 0),
                int(parsed.get("total_cache_create_1h", 0) or 0),
                int(parsed.get("total_cache_create_5m", 0) or 0),
                float(parsed.get("cache_hit_rate", 0.0) or 0.0),
                # Refresh the model-usage JSON columns too. Downstream cost calcs
                # read BOTH input_tokens and these breakdowns; updating tokens without
                # the breakdown leaves the row internally inconsistent.
                _measure_facade.json.dumps(parsed.get("model_usage", {})),
                _measure_facade.json.dumps(parsed.get("model_usage", {})),  # all_model_usage = {model:int}
                _measure_facade.json.dumps(parsed.get("model_usage_breakdown", {})),
                row_id,
            ))
            refreshed += 1
    if updates:
        conn.executemany(
            "UPDATE session_log SET input_tokens = ?, output_tokens = ?, "
            "cache_create_1h_tokens = ?, cache_create_5m_tokens = ?, cache_hit_rate = ?, "
            "model_usage_json = ?, all_model_usage_json = ?, model_usage_breakdown_json = ? "
            "WHERE id = ?",
            updates,
        )
        conn.commit()
    return (refreshed, checked, missing)


def _log_migration_error(step, exc):
    """Log a trends-DB migration error, suppressing benign 'duplicate column' /
    'table already exists' cases that are expected under concurrent init.

    The migration blocks in _init_trends_db are guarded by PRAGMA table_info
    checks, so a 'duplicate column name' error means a concurrent process won
    the race — benign and idempotent. Any other sqlite3.Error (disk full,
    corruption, I/O error) would otherwise be silently swallowed, leaving the
    column missing and causing confusing downstream crashes far from the root
    cause. Log those to stderr so they are discoverable.
    """
    _measure_facade = _compat.facade()
    msg = str(exc)
    benign = (
        "duplicate column" in msg
        or "already exists" in msg
    )
    if not benign:
        try:
            _measure_facade.sys.stderr.write(
                "[Token Optimizer] trends DB migration '%s' failed: %s\n"
                % (step, msg)
            )
        except Exception:
            pass


class _SkipMigration(Exception):
    """Nothing to migrate; leave the step without touching the write lock."""


def _origin_insert_parts(conn, table):
    """(column SQL, placeholder SQL, values) that add ``origin`` to an INSERT
    into ``table`` -- or three empties when the column is missing (a migration
    that could not run yet), so the row still lands without it instead of the
    whole event being lost to an "no such column" error."""
    _measure_facade = _compat.facade()
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        cols = set()
    if "origin" in cols:
        return ", origin", ", ?", (_measure_facade.run_origin(),)
    return "", "", ()


def _claude_projects_like_patterns() -> tuple:
    """Prefix LIKE patterns (ESCAPE '!') for transcripts under the resolved
    Claude config's projects/ dir, or () when that is the default ~/.claude
    (the literal pattern already covers it) or unresolvable.

    Anchored at the start of the path, so a "copilot:"/"hermes:" dedup key or
    a path that merely contains the folder name further down never matches.
    Forward slashes only: callers match it against _PLATFORM_PATH_SQL, which
    folds the backslashes of rows written on Windows to forward slashes.
    """
    _measure_facade = _compat.facade()
    try:
        projects = _measure_facade.Path(_measure_facade.CLAUDE_DIR) / "projects"
        if projects == _measure_facade.Path(_measure_facade.HOME) / ".claude" / "projects":
            return ()
        form = str(projects).replace("\\", "/")
        esc = form.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        return (esc + "/%",)
    except Exception:
        return ()


def _sql_quote(value):
    _measure_facade = _compat.facade()
    return "'" + str(value).replace("'", "''") + "'"


def _fixture_session_sql(col=None):
    _measure_facade = _compat.facade()
    globs = " OR ".join(f"{col} GLOB {_measure_facade._sql_quote(g)}" for g in _measure_facade._FIXTURE_SESSION_GLOBS)
    numeric_codex = (f"({col} GLOB 'codex-sess-[0-9]*' "
                     f"AND substr({col}, 12) NOT GLOB '*[^0-9]*')")
    return f"({col} IS NOT NULL AND ({globs} OR {numeric_codex}))"


def _is_fixture_session_id(session_id):
    """Python mirror of _fixture_session_sql, for callers outside SQL."""
    _measure_facade = _compat.facade()
    import fnmatch
    if not session_id:
        return False
    sid = str(session_id)
    if _measure_facade.re.fullmatch(r"codex-sess-[0-9]+", sid):
        return True
    return any(fnmatch.fnmatchcase(sid, g) for g in _measure_facade._FIXTURE_SESSION_GLOBS)


def _sql_variants(template, **fragments):
    """Every statement *template* can become, built once at import.

    Each keyword names a ``{field}`` in the template and maps a key to one of the
    author's own fixed SQL fragments. The result is keyed by one key per field (in
    keyword order), so a call site selects a prebuilt statement and never assembles
    SQL text at runtime."""
    _measure_facade = _compat.facade()
    names = tuple(fragments)
    return {
        keys: template.format(**{n: fragments[n][k] for n, k in zip(names, keys)})
        for keys in _measure_facade.itertools.product(*fragments.values())
    }


def _counted_twin_sql(alias, table, kind_col, same):
    """Twin check: an earlier row of the same shape in the same session within
    the window. Correlated EXISTS (not a window function) so a query with a
    timestamp filter only looks at the rows it reads; the lookup is served by
    the (session_id, kind, timestamp) indexes created in _init_trends_db."""
    _measure_facade = _compat.facade()
    win = _measure_facade._COUNTED_TWIN_WINDOW_SECONDS
    return (
        f"EXISTS (SELECT 1 FROM main.{table} p WHERE p.session_id = {alias}.session_id "
        f"AND p.{kind_col} = {alias}.{kind_col} AND {same} "
        f"AND p.timestamp <= {alias}.timestamp "
        f"AND p.timestamp >= strftime('%Y-%m-%dT%H:%M:%S', julianday({alias}.timestamp) - {win + 1.0} / 86400.0) "
        f"AND (p.timestamp < {alias}.timestamp OR p.id < {alias}.id) "
        f"AND (julianday({alias}.timestamp) - julianday(p.timestamp)) * 86400.0 <= {win})"
    )


def _media_reexpand_pair_sql(alias):
    """A re-expansion whose paired archive row is a media result. Its archive
    credit is left out as media, so its debit is left out too."""
    _measure_facade = _compat.facade()
    media = " OR ".join(f"a.detail GLOB {_measure_facade._sql_quote(g)}" for g in _measure_facade._MEDIA_TOOL_GLOBS)
    win = _measure_facade._REEXPAND_PAIR_WINDOW_SECONDS
    return (
        f"EXISTS (SELECT 1 FROM main.savings_events a WHERE a.session_id = {alias}.session_id "
        f"AND a.event_type = 'tool_archive' AND a.detail IS NOT NULL AND ({media}) "
        f"AND a.timestamp <= {alias}.timestamp "
        f"AND a.timestamp >= strftime('%Y-%m-%dT%H:%M:%S', julianday({alias}.timestamp) - {win}.0 / 86400.0) "
        f"AND {alias}.tokens_saved - a.tokens_saved BETWEEN 0 AND {_measure_facade._REEXPAND_PAIR_STUB_TOKENS} "
        f"AND NOT EXISTS (SELECT 1 FROM main.savings_events b WHERE b.session_id = {alias}.session_id "
        f"AND b.event_type = 'tool_archive' AND b.timestamp <= {alias}.timestamp AND b.timestamp > a.timestamp "
        f"AND {alias}.tokens_saved - b.tokens_saved BETWEEN 0 AND {_measure_facade._REEXPAND_PAIR_STUB_TOKENS}))"
    )


def _counted_view_statements():
    """The two CREATE TEMP VIEW statements, composed once from the fixed rules above."""
    _measure_facade = _compat.facade()
    media = " OR ".join(f"e.detail GLOB {_measure_facade._sql_quote(g)}" for g in _measure_facade._MEDIA_TOOL_GLOBS)
    media_types = ", ".join(_measure_facade._sql_quote(t) for t in _measure_facade._MEDIA_ARCHIVE_EVENT_TYPES)
    media_pair = _measure_facade._media_reexpand_pair_sql("e")
    se_twin = _measure_facade._counted_twin_sql(
        "e", "savings_events", "event_type",
        "p.tokens_saved = e.tokens_saved AND COALESCE(p.detail, '') = COALESCE(e.detail, '')")
    ce_twin = _measure_facade._counted_twin_sql(
        "c", "compression_events", "feature",
        "p.original_tokens = c.original_tokens AND p.compressed_tokens = c.compressed_tokens "
        "AND COALESCE(p.command_pattern, '') = COALESCE(c.command_pattern, '')")
    return (
        "CREATE TEMP VIEW IF NOT EXISTS counted_savings_events AS "
        "SELECT e.* FROM main.savings_events e "
        f"WHERE NOT {_measure_facade._fixture_session_sql('e.session_id')}"
        f" AND NOT (e.event_type IN ({media_types}) AND e.detail IS NOT NULL AND ({media}))"
        f" AND NOT (e.event_type = 'tool_archive_reexpand' AND COALESCE(e.session_id, '') != '' AND {media_pair})"
        f" AND NOT (e.tokens_saved > 0 AND COALESCE(e.session_id, '') != '' AND {se_twin})",
        "CREATE TEMP VIEW IF NOT EXISTS counted_compression_events AS "
        "SELECT c.* FROM main.compression_events c "
        f"WHERE NOT {_measure_facade._fixture_session_sql('c.session_id')}"
        f" AND NOT (c.original_tokens > c.compressed_tokens AND COALESCE(c.session_id, '') != '' AND {ce_twin})",
    )


def _counted_media_candidate_statement():
    """Media archive events that pass every other counted-view rule."""
    _measure_facade = _compat.facade()
    media = " OR ".join(f"e.detail GLOB {_measure_facade._sql_quote(g)}" for g in _measure_facade._MEDIA_TOOL_GLOBS)
    se_twin = _measure_facade._counted_twin_sql(
        "e", "savings_events", "event_type",
        "p.tokens_saved = e.tokens_saved AND COALESCE(p.detail, '') = COALESCE(e.detail, '')")
    return (
        "SELECT 'md:'||e.id, e.session_uuid, e.timestamp, 0, NULL, e.model "
        "FROM main.savings_events e WHERE e.event_type = 'tool_archive' AND e.timestamp >= ? "
        f"AND e.detail IS NOT NULL AND ({media}) "
        f"AND NOT {_measure_facade._fixture_session_sql('e.session_id')} "
        f"AND NOT (e.tokens_saved > 0 AND COALESCE(e.session_id, '') != '' AND {se_twin})"
    )


def _ensure_counted_views(conn):
    """Create the per-connection counted views (idempotent, TEMP, read-only).

    counted_savings_events and counted_compression_events have the same
    columns as the tables they read. Safe to call on any trends.db connection;
    returns False (and callers fall back to the raw table) only if the tables
    are missing or the SQLite build lacks window functions."""
    _measure_facade = _compat.facade()
    try:
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_temp_master WHERE type='view'").fetchall()}
        if {"counted_savings_events", "counted_compression_events"} <= have:
            return True
        for statement in _measure_facade._COUNTED_VIEW_SQL:
            conn.execute(statement)
        # SQLite resolves view columns lazily: a database missing a column the
        # filters use (very old installs, minimal schemas) only fails on first
        # read. Probe now and fall back to the raw tables instead.
        conn.execute("SELECT 1 FROM counted_savings_events LIMIT 0").fetchall()
        conn.execute("SELECT 1 FROM counted_compression_events LIMIT 0").fetchall()
        return True
    except _measure_facade.sqlite3.Error:
        for drop in ("DROP VIEW IF EXISTS temp.counted_savings_events",
                     "DROP VIEW IF EXISTS temp.counted_compression_events"):
            try:
                conn.execute(drop)
            except _measure_facade.sqlite3.Error:
                pass
        return False


def _counted_table(conn, table):
    """Name of the counted view for *table*, or the raw table if views fail."""
    _measure_facade = _compat.facade()
    return _measure_facade._COUNTED_SOURCES[table][0] if _measure_facade._ensure_counted_views(conn) else table


def _counted_sql(table, template):
    """Statement for every source _counted_table(conn, table) can return, keyed by
    that name. The template's ``{table}`` field is the only thing that varies."""
    _measure_facade = _compat.facade()
    variants = _measure_facade._sql_variants(template, table={name: name for name in _measure_facade._COUNTED_SOURCES[table]})
    return {name: sql for (name,), sql in variants.items()}


def _connect_trends_db():
    """Open the trends DB with the standard pragmas (no migrations run)."""
    _measure_facade = _compat.facade()
    conn = _measure_facade.sqlite3.connect(str(_measure_facade.TRENDS_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    # Auto-checkpoint every 100 pages (~400 KiB) instead of 1000 (~4 MiB).
    # A single large transaction (e.g. _rebuild_aggregate_tables) cannot
    # checkpoint mid-flight, so a smaller threshold bounds the WAL growth
    # between transactions to ~400 KiB instead of ~4 MiB, keeping the
    # post-checkpoint WAL closer to the 64 MiB journal_size_limit.
    conn.execute("PRAGMA wal_autocheckpoint=100")
    conn.execute("PRAGMA journal_size_limit=67108864")
    return conn


def _snapshot_activity_dating_prestate(conn, old_rows):
    """Journal pre-migration session dates/calls and copy the aggregate tables
    aside, inside the caller's open transaction. ``old_rows`` holds
    ``(jsonl_path, date, first_activity_day, last_activity_day, daily_calls_json)`` read before
    ``repair_activity_dates`` rewrote the dates, so rollback restores the
    values the migration replaced rather than the repaired ones."""
    _measure_facade = _compat.facade()
    conn.execute(f"DROP TABLE IF EXISTS {_measure_facade._ACTIVITY_DATING_JOURNAL}")
    conn.execute(
        f"CREATE TABLE {_measure_facade._ACTIVITY_DATING_JOURNAL} ("
        "jsonl_path TEXT PRIMARY KEY, old_date TEXT, "
        "old_first_activity_day TEXT, old_last_activity_day TEXT, old_daily_calls_json TEXT)")
    conn.executemany(
        f"INSERT INTO {_measure_facade._ACTIVITY_DATING_JOURNAL} "
        "(jsonl_path, old_date, old_first_activity_day, old_last_activity_day, old_daily_calls_json) "
        "VALUES (?, ?, ?, ?, ?)", old_rows)
    for table in _measure_facade._ACTIVITY_AGGREGATE_TABLES:
        backup = f"{table}_pre_activity_v1"
        conn.execute(f"DROP TABLE IF EXISTS {backup}")
        conn.execute(f"CREATE TABLE {backup} AS SELECT * FROM {table}")


def _archive_pre_activity_state_files():
    """Move every pre-activity derived state file off its live name.

    The first archive keeps the ``.pre-activity-v1`` suffix; when that name is
    already taken (an interrupted earlier run, a restored backup) the active
    copy moves to the next free ``.pre-activity-v1.N`` instead. The stale file
    therefore always leaves its live name — it cannot keep serving as an
    anchor — and no earlier archive is overwritten. Files already stamped
    ``activity_basis == 1`` were rebuilt under activity dating and stay."""
    _measure_facade = _compat.facade()
    for name in ("baseline_state.json", "workload_anchor.json", "workload_anchor_pretool.json",
                 "weekly_full_value.json", "subagent_pool.json", "compact_advice_cache.json"):
        path = _measure_facade.SNAPSHOT_DIR / name
        if not path.exists():
            continue
        try:
            if _measure_facade.json.loads(path.read_text()).get("activity_basis") == 1:
                continue
        except (OSError, ValueError, AttributeError):
            pass
        retained = path.with_name(path.name + ".pre-activity-v1")
        suffix = 2
        while retained.exists():
            retained = path.with_name(f"{path.name}.pre-activity-v1.{suffix}")
            suffix += 1
        path.rename(retained)


def rollback_activity_dating(conn):
    """Developer-only safety net for immediately checking a migration on a copy.

    No CLI is exposed. Use before any recollection or new writes: this restores
    snapshot dates/calls and aggregates, so later changes would be overwritten
    or lost. It is not a downgrade or live-database recovery mechanism.
    Restores the session dates/calls journaled by jsonl_path before the rewrite, copies the
    ``*_pre_activity_v1`` aggregate backups back over the rebuilt tables and
    clears the migration marker, so the next ``_init_trends_db`` re-runs the
    migration. The ``.pre-activity-v1`` JSON archives are left in place: they
    are retained evidence, and derived files regenerate on their own. Returns
    True when pre-migration state was restored, False when there was nothing
    to undo. Caller owns the connection; this commits on success.
    """
    # Only a committed migration is reversible: without the marker the journal
    # and backups (if any) belong to an earlier run or a rolled-back attempt,
    # and restoring them could overwrite newer state with stale snapshots.
    _measure_facade = _compat.facade()
    if not conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name='activity_dating_v1'").fetchone():
        return False
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    journal = _measure_facade._ACTIVITY_DATING_JOURNAL in tables
    backups = [t for t in _measure_facade._ACTIVITY_AGGREGATE_TABLES
               if f"{t}_pre_activity_v1" in tables]
    if not journal and not backups:
        return False
    conn.execute("BEGIN IMMEDIATE")
    try:
        if journal:
            rows = conn.execute(
                f"SELECT jsonl_path, old_date, old_first_activity_day, "
                f"old_last_activity_day, old_daily_calls_json FROM {_measure_facade._ACTIVITY_DATING_JOURNAL}").fetchall()
            conn.executemany(
                "UPDATE session_log SET date=?, first_activity_day=?, "
                "last_activity_day=?, daily_calls_json=? WHERE jsonl_path=?",
                [(old_date, first, last, calls, path)
                 for path, old_date, first, last, calls in rows])
        for table in backups:
            backup = f"{table}_pre_activity_v1"
            live_cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            backup_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({backup})")}
            cols = [c for c in live_cols if c in backup_cols]
            if cols:
                conn.execute(f"DELETE FROM {table}")
                conn.execute(
                    f"INSERT INTO {table} ({', '.join(cols)}) "
                    f"SELECT {', '.join(cols)} FROM {backup}")
            conn.execute(f"DROP TABLE {backup}")
        if journal:
            conn.execute(f"DROP TABLE {_measure_facade._ACTIVITY_DATING_JOURNAL}")
        conn.execute("DELETE FROM schema_migrations WHERE name='activity_dating_v1'")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def _init_trends_db():
    """Initialize the trends SQLite DB. Returns a connection.

    Uses Total Recall's DB-lock pattern: WAL mode + 5s busy_timeout + retry.
    """
    _measure_facade = _compat.facade()
    _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    conn = _measure_facade._connect_trends_db()
    conn.executescript(_measure_facade._SCHEMA)
    # Migrate existing DBs: add slug/topic columns if missing
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(session_log)").fetchall()}
        if "slug" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN slug TEXT")
        if "topic" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN topic TEXT")
        if "cache_create_1h_tokens" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN cache_create_1h_tokens INTEGER DEFAULT 0")
        if "cache_create_5m_tokens" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN cache_create_5m_tokens INTEGER DEFAULT 0")
        if "cache_ttl_scanned" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN cache_ttl_scanned INTEGER DEFAULT 0")
        if "avg_call_gap_seconds" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN avg_call_gap_seconds REAL")
        if "max_call_gap_seconds" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN max_call_gap_seconds REAL")
        if "p95_call_gap_seconds" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN p95_call_gap_seconds REAL")
        if "model_usage_breakdown_json" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN model_usage_breakdown_json TEXT")
        if "all_model_usage_json" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN all_model_usage_json TEXT")
        if "quality_score" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN quality_score REAL")
        if "quality_grade" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN quality_grade TEXT")
        # B1: per-session reclaimable stale-read waste (opportunity tier only,
        # never summed into realized savings). Defaults 0 for pre-existing rows.
        if "stale_waste_tokens" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN stale_waste_tokens INTEGER DEFAULT 0")
        # Stable UUID-keyed join column so savings/compression events can
        # join to their originating session without LIKE-scanning jsonl_path.
        # The session UUID is the JSONL file stem (the Claude session UUID).
        if "session_uuid" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN session_uuid TEXT")
        # Copilot cost is platform-reported (AI credits / premium requests) and
        # CANNOT be re-derived from token counts the way Claude/Codex cost is,
        # so it must be persisted. Also carry the source discriminator + plane
        # so the credits-led dashboard keeps CLI and VS Code sessions distinct.
        if "cost_usd" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN cost_usd REAL")
        if "cost_source" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN cost_source TEXT")
        if "credits" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN credits REAL")
        if "platform" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN platform TEXT")
        # Marks a session whose totals are partial (crashed / still-active at
        # rollup time). Lets a later rollup UPGRADE the row once real shutdown
        # totals arrive, instead of INSERT-OR-IGNORE freezing the partial data.
        if "incomplete" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN incomplete INTEGER DEFAULT 0")
        # Subagent sidechains (isSidechain:true in the JSONL) are not human working
        # sessions; flagging them lets the before/after cost comparison filter to
        # human sessions on both sides instead of diluting "after" with cheap
        # sidechain rows. Added WITHOUT a default so existing rows are NULL =
        # "not yet classified" — the sentinel _backfill_is_sidechain targets. New
        # collection always writes an explicit 0/1. Queries treat NULL as human.
        if "is_sidechain" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN is_sidechain INTEGER")
        if "sidechain_reason" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN sidechain_reason TEXT")
        # Official /stats-compatible usage is captured without streaming dedup
        # and without cache classes. Legacy rows are filled by a one-time scan.
        if "reported_input_tokens" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN reported_input_tokens INTEGER")
        if "reported_output_tokens" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN reported_output_tokens INTEGER")
        if "reported_model_usage_json" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN reported_model_usage_json TEXT")
        # Per-local-day, per-model token buckets for the whole session INCLUDING
        # its subagents. The daily cost rollup prices from this so each day bills
        # its own requests and delegated work is not dropped (#200).
        if "daily_usage_json" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN daily_usage_json TEXT")
        # Where the collecting process ran: 'local', 'cloud' (Claude Code cloud
        # VM) or 'cowork'. NULL on rows collected before the column existed,
        # which were all local. Groundwork for importing a cloud VM's rows or a
        # teleported transcript without counting the session twice: the dedupe
        # key stays session_uuid (the transcript's session id); origin records
        # which side a row came from.
        if "origin" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN origin TEXT")
        # Which usage-attribution version stored the row. NULL = stored before
        # versioning; collect re-parses such rows from their transcript.
        if "usage_parser_version" not in cols:
            conn.execute("ALTER TABLE session_log ADD COLUMN usage_parser_version INTEGER")
        conn.commit()
    except _measure_facade.sqlite3.Error as _e:
        _measure_facade._log_migration_error("session_log columns", _e)
    # Shares already recorded are usage evidence: a claims table from before the
    # model and day columns keeps its counters and gains the columns. Only a
    # table that never held counters (nothing to keep) is recreated.
    try:
        _claim_cols = {r[1] for r in conn.execute("PRAGMA table_info(usage_request_claims)").fetchall()}
        if _claim_cols and "inp" not in _claim_cols:
            conn.execute("DROP TABLE usage_request_claims")
            conn.executescript(_measure_facade._SCHEMA)
        else:
            for _col in ("model", "day"):
                if _claim_cols and _col not in _claim_cols:
                    conn.execute(f"ALTER TABLE usage_request_claims ADD COLUMN {_col} TEXT")
        conn.commit()
    except _measure_facade.sqlite3.Error as _e:
        _measure_facade._log_migration_error("usage_request_claims", _e)
    # Same origin column on the per-event savings tables (NULL = legacy/local).
    for _origin_table in ("savings_events", "compression_events"):
        try:
            _ocols = {r[1] for r in conn.execute(f"PRAGMA table_info({_origin_table})").fetchall()}
            if _ocols and "origin" not in _ocols:
                conn.execute(f"ALTER TABLE {_origin_table} ADD COLUMN origin TEXT")
                conn.commit()
        except _measure_facade.sqlite3.Error as _e:
            _measure_facade._log_migration_error(f"{_origin_table} origin column", _e)
    _measure_facade.ensure_activity_schema(conn)
    # Backfill session_uuid from jsonl_path basename stem for existing rows,
    # then create an index so joins are O(log n) instead of full-table LIKE scans.
    # Done in Python (not a single SQL UPDATE) because SQLite lacks a basename
    # function and the path separator can vary on Windows.
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_log_uuid ON session_log (session_uuid)"
        )
        rows = conn.execute(
            "SELECT id, jsonl_path FROM session_log "
            "WHERE (session_uuid IS NULL OR session_uuid LIKE 'rollout-%') AND jsonl_path IS NOT NULL"
        ).fetchall()
        if rows:
            updates = []
            for row_id, jpath in rows:
                stem = _measure_facade.Path(jpath).stem  # strips directory and .jsonl suffix
                if stem and stem != "unknown":
                    canonical, _ = _measure_facade._extract_session_uuid(stem)
                    updates.append((canonical or stem, row_id))
            if updates:
                conn.executemany(
                    "UPDATE session_log SET session_uuid = ? WHERE id = ?", updates
                )
        conn.commit()
    except (_measure_facade.sqlite3.Error, OSError, ValueError) as _e:
        _measure_facade._log_migration_error("session_uuid backfill", _e)
    # One-time backfill of the corrected sidechain classifier. It revisits old
    # rows, including the 1-valued rows the broad marker test misclassified.
    sidechain_changed = 0
    try:
        sidechain_changed, _sidechain_missing = _measure_facade._backfill_is_sidechain(conn)
    except (_measure_facade.sqlite3.Error, OSError) as _e:
        _measure_facade._log_migration_error("sidechain backfill", _e)
    # One-time backfill of official-compatible usage for existing rows. New
    # collection writes these fields directly from every assistant record.
    try:
        _measure_facade._backfill_reported_token_usage(conn)
    except (_measure_facade.sqlite3.Error, OSError) as _e:
        _measure_facade._log_migration_error("reported token backfill", _e)
    if sidechain_changed:
        try:
            _measure_facade._rebuild_aggregate_tables(conn)
            conn.commit()
        except _measure_facade.sqlite3.Error as _e:
            _measure_facade._log_migration_error("aggregate rebuild", _e)
    # Backfill platform for rows collected before the column was wired into the
    # INSERT paths. The jsonl_path discriminator is definitive: Claude sessions
    # live under ~/.claude/projects/, Codex under ~/.codex/sessions/, Hermes
    # uses "hermes:" dedup keys, and Copilot uses "copilot:" dedup keys. Only
    # infer when the path unambiguously identifies one platform; anything else
    # stays NULL. Idempotent (gated by platform IS NULL). An UPDATE takes the
    # write lock even when it matches nothing, so check with a read first: on
    # every dashboard open this would otherwise wait out busy_timeout behind a
    # running collector.
    _platform_patterns = ("%/.claude/projects/%", "%/.codex/sessions/%", "%/.codex/archived_sessions/%",
                          "hermes:%", "copilot:%", "cursor:%", "antigravity:%")
    # A relocated Claude config (CLAUDE_CONFIG_DIR) keeps its transcripts under
    # <config>/projects/, which the literal ~/.claude pattern above misses.
    _claude_cfg_patterns = _measure_facade._claude_projects_like_patterns()
    # Paths are matched with backslashes folded to '/', so a row written on
    # Windows (C:\Users\me\.claude\projects\...) is tagged like a POSIX one.
    _p = _measure_facade._PLATFORM_PATH_SQL
    _cfg_where = " OR ".join([f"{_p} LIKE ? ESCAPE '!'"] * len(_claude_cfg_patterns))
    try:
        _needs_platform = conn.execute(
            "SELECT 1 FROM session_log WHERE platform IS NULL AND ("
            + " OR ".join([f"{_p} LIKE ?"] * len(_platform_patterns))
            + (" OR " + _cfg_where if _cfg_where else "") + ") LIMIT 1",
            _platform_patterns + _claude_cfg_patterns,
        ).fetchone()
    except _measure_facade.sqlite3.Error:
        _needs_platform = True
    try:
        if not _needs_platform:
            raise _measure_facade._SkipMigration
        for _platform, _pattern in (("claude", "%/.claude/projects/%"),
                                    ("codex", "%/.codex/sessions/%"),
                                    ("codex", "%/.codex/archived_sessions/%"),
                                    ("hermes", "hermes:%"),
                                    ("copilot", "copilot:%"),
                                    ("cursor", "cursor:%"),
                                    ("antigravity", "antigravity:%")):
            conn.execute(
                f"UPDATE session_log SET platform = ? WHERE platform IS NULL AND {_p} LIKE ?",
                (_platform, _pattern),
            )
        # Last, so every other runtime's definitive pattern has claimed its rows
        # first; the Codex path patterns are excluded again so a config dir
        # that happens to sit inside a Codex home can never tag a Codex file.
        if _cfg_where:
            conn.execute(
                "UPDATE session_log SET platform = 'claude' "
                "WHERE platform IS NULL AND (" + _cfg_where + ") "
                f"AND {_p} NOT LIKE '%/.codex/sessions/%' "
                f"AND {_p} NOT LIKE '%/.codex/archived_sessions/%'",
                _claude_cfg_patterns,
            )
        conn.commit()
    except _measure_facade._SkipMigration:
        pass
    except _measure_facade.sqlite3.Error as _e:
        _measure_facade._log_migration_error("platform backfill", _e)
    # Migrate: add quality columns to daily_stats for existing DBs
    try:
        ds_cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_stats)").fetchall()}
        for name, kind in (("session_count", "INTEGER"), ("total_input", "INTEGER"),
                           ("total_output", "INTEGER"), ("total_duration", "REAL"), ("avg_cache_hit", "REAL")):
            if name not in ds_cols:
                conn.execute(f"ALTER TABLE daily_stats ADD COLUMN {name} {kind}")
        if "avg_quality_score" not in ds_cols:
            conn.execute("ALTER TABLE daily_stats ADD COLUMN avg_quality_score REAL")
        if "worst_grade" not in ds_cols:
            conn.execute("ALTER TABLE daily_stats ADD COLUMN worst_grade TEXT")
        conn.commit()
    except _measure_facade.sqlite3.Error as _e:
        _measure_facade._log_migration_error("daily_stats columns", _e)
    # Migrate: add per-event model column to savings_events (v5.9+). Lets the
    # savings view reprice historical events at the rate that was actually in
    # effect when they were logged, instead of today's active-model rate.
    try:
        se_cols = {r[1] for r in conn.execute("PRAGMA table_info(savings_events)").fetchall()}
        if "model" not in se_cols:
            conn.execute("ALTER TABLE savings_events ADD COLUMN model TEXT")
        # Session_uuid + unjoinable columns for direct session joins (no LIKE scan).
        # session_uuid = the Claude session UUID (= JSONL basename stem).
        # unjoinable = 1 when session_id is a short agent_id that cannot be resolved.
        if "session_uuid" not in se_cols:
            conn.execute("ALTER TABLE savings_events ADD COLUMN session_uuid TEXT")
        if "unjoinable" not in se_cols:
            conn.execute("ALTER TABLE savings_events ADD COLUMN unjoinable INTEGER DEFAULT 0")
        # Keep-warm idempotency: pause_key uniquely identifies one (session, pause)
        # realized booking. The UNIQUE index makes SQLite the dedup authority so a
        # lost ledger marker (or a re-detect across ticks) can never double-book the
        # same warm resume. Partial index (WHERE pause_key IS NOT NULL) leaves every
        # legacy/non-keepwarm row -- all NULL here -- unconstrained.
        if "pause_key" not in se_cols:
            conn.execute("ALTER TABLE savings_events ADD COLUMN pause_key TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_savings_events_uuid "
            "ON savings_events (session_uuid)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_savings_events_pause_key "
            "ON savings_events (pause_key) WHERE pause_key IS NOT NULL"
        )
        conn.commit()
    except _measure_facade.sqlite3.Error as _e:
        _measure_facade._log_migration_error("savings_events columns", _e)
    # Backfill session_uuid on savings_events from session_id where it looks
    # like a UUID (8-4-4-4-12 hex pattern). Short agent_ids (<=17 chars without
    # dashes) are flagged unjoinable=1 rather than silently treated as Sonnet.
    try:
        import re as _re
        _UUID_PAT = _re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            _re.IGNORECASE,
        )
        rows = conn.execute(
            "SELECT id, session_id FROM savings_events "
            "WHERE session_uuid IS NULL AND session_id IS NOT NULL"
        ).fetchall()
        if rows:
            uuid_updates = []
            unjoinable_updates = []
            for row_id, sid in rows:
                canonical, _ = _measure_facade._extract_session_uuid(sid)
                if canonical:
                    uuid_updates.append((canonical, row_id))
                elif sid and len(sid) <= 20 and "-" not in sid and sid not in (
                    "unknown", "test-123", "perf_test", "regtest", "demo"
                ):
                    # Short opaque hex agent_id — cannot be resolved to a session_log row
                    unjoinable_updates.append((row_id,))
            if uuid_updates:
                conn.executemany(
                    "UPDATE savings_events SET session_uuid = ? WHERE id = ?",
                    uuid_updates,
                )
            if unjoinable_updates:
                conn.executemany(
                    "UPDATE savings_events SET unjoinable = 1 WHERE id = ?",
                    unjoinable_updates,
                )
        conn.commit()
    except (_measure_facade.sqlite3.Error, OSError) as _e:
        _measure_facade._log_migration_error("savings_events uuid backfill", _e)
    # Migrate: ensure compression_events table exists for upgrades from v4.x
    try:
        conn.execute("SELECT 1 FROM compression_events LIMIT 1")
    except _measure_facade.sqlite3.OperationalError:
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS compression_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    session_id TEXT,
                    session_uuid TEXT,
                    feature TEXT NOT NULL,
                    command_pattern TEXT,
                    original_tokens INTEGER DEFAULT 0,
                    compressed_tokens INTEGER DEFAULT 0,
                    compression_ratio REAL DEFAULT 0.0,
                    quality_preserved INTEGER DEFAULT 1,
                    verified INTEGER DEFAULT 0,
                    detail TEXT,
                    model TEXT
                );
            """)
            conn.commit()
        except _measure_facade.sqlite3.Error as _e:
            _measure_facade._log_migration_error("compression_events table", _e)
    # Idempotent migrations for session_uuid + model on compression_events.
    # These columns enable per-event session joins and correct model attribution.
    try:
        ce_cols = {r[1] for r in conn.execute("PRAGMA table_info(compression_events)").fetchall()}
        if "session_uuid" not in ce_cols:
            conn.execute("ALTER TABLE compression_events ADD COLUMN session_uuid TEXT")
        if "model" not in ce_cols:
            conn.execute("ALTER TABLE compression_events ADD COLUMN model TEXT")
        if "tier" not in ce_cols:
            # Three-tier accounting tag (measured/estimated/
            # opportunity). Nullable: pre-existing rows stay NULL and keep their
            # current headline treatment; only new coverage events set a tier.
            conn.execute("ALTER TABLE compression_events ADD COLUMN tier TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_compression_events_uuid "
            "ON compression_events (session_uuid)"
        )
        # T6: the tripwire edit-rate query filters on (feature, tier); index it so
        # the per-cohort skeleton/follow-up scans stay cheap as the table grows.
        # Idempotent (IF NOT EXISTS) — safe on every init.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_compression_events_feature_tier "
            "ON compression_events (feature, tier)"
        )
        # Serves the counted views' same-session twin lookup.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_compression_events_session_feature_ts "
            "ON compression_events (session_id, feature, timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_savings_events_session_type_ts "
            "ON savings_events (session_id, event_type, timestamp)"
        )
        conn.commit()
    except _measure_facade.sqlite3.Error as _e:
        _measure_facade._log_migration_error("compression_events columns", _e)
    # Backfill session_uuid on compression_events from session_id where UUID pattern.
    try:
        import re as _re
        _UUID_PAT2 = _re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            _re.IGNORECASE,
        )
        ce_rows = conn.execute(
            "SELECT id, session_id FROM compression_events "
            "WHERE session_uuid IS NULL AND session_id IS NOT NULL"
        ).fetchall()
        if ce_rows:
            ce_updates = [
                (_measure_facade._extract_session_uuid(sid)[0], row_id)
                for row_id, sid in ce_rows
                if _measure_facade._extract_session_uuid(sid)[0]
            ]
            if ce_updates:
                conn.executemany(
                    "UPDATE compression_events SET session_uuid = ? WHERE id = ?",
                    ce_updates,
                )
        conn.commit()
    except (_measure_facade.sqlite3.Error, OSError) as _e:
        _measure_facade._log_migration_error("compression_events uuid backfill", _e)
    conn.commit()
    # Journal the pre-rewrite dates before repair_activity_dates opens its
    # transaction; the journal, aggregate backups, rebuild, archives and
    # marker then commit or roll back together.
    pre_activity_rows = []
    if not conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name='activity_dating_v1'").fetchone():
        pre_activity_rows = conn.execute(
            "SELECT jsonl_path,date,first_activity_day,last_activity_day,daily_calls_json FROM session_log").fetchall()
    try:
        if _measure_facade.repair_activity_dates(conn):
            _measure_facade._snapshot_activity_dating_prestate(conn, pre_activity_rows)
            _measure_facade._rebuild_aggregate_tables(conn)
            # Keep original evidence recoverable. These derived comparisons used
            # session-date windows and must be rebuilt against dated activity.
            _measure_facade._archive_pre_activity_state_files()
            conn.execute("INSERT INTO schema_migrations(name) VALUES ('activity_dating_v1')")
            conn.commit()
    except Exception as _e:
        # Includes BEGIN IMMEDIATE lock failures. Roll back any partial work
        # and return a fresh, usable connection; retry migration on next init.
        try:
            conn.rollback()
        finally:
            conn.close()
        _measure_facade._log_migration_error("activity_dating_v1 migration", _e)
        return _measure_facade._connect_trends_db()
    return conn


def _compute_call_gap_stats(api_call_timestamps):
    """Compute avg/max/p95 gaps between assistant API calls within a session."""
    _measure_facade = _compat.facade()
    if len(api_call_timestamps) < 2:
        return {"avg": None, "max": None, "p95": None}

    gaps = []
    prev_ts = None
    for ts in api_call_timestamps:
        if prev_ts is None:
            prev_ts = ts
            continue
        delta = (ts - prev_ts).total_seconds()
        if delta >= 0:
            gaps.append(delta)
        prev_ts = ts

    if not gaps:
        return {"avg": None, "max": None, "p95": None}

    sorted_gaps = sorted(gaps)
    p95_index = max(0, min(len(sorted_gaps) - 1, _measure_facade.math.ceil(len(sorted_gaps) * 0.95) - 1))
    return {
        "avg": sum(gaps) / len(gaps),
        "max": max(gaps),
        "p95": sorted_gaps[p95_index],
    }


def _make_session_key(jsonl_path):
    """Generate a stable opaque session key from a JSONL path."""
    _measure_facade = _compat.facade()
    if not jsonl_path:
        return None
    normalized = str(_measure_facade.Path(jsonl_path).resolve())
    return _measure_facade.hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def _retry_db_operation(conn, operation, max_retries=None, initial_backoff=None):
    """Retry a database operation with exponential backoff on lock errors.

    Mirrors Total Recall's DB-lock pattern: busy_timeout + retry with backoff.
    Fails open on persistent lock errors (returns None, caller handles gracefully).
    Non-lock OperationalErrors are re-raised so real bugs aren't silently swallowed.
    """
    _measure_facade = _compat.facade()
    for attempt in range(max_retries):
        try:
            return operation()
        except _measure_facade.sqlite3.OperationalError as e:
            if "database is locked" in str(e) or "unable to open database" in str(e):
                if attempt < max_retries - 1:
                    backoff = initial_backoff * (2 ** attempt)
                    _measure_facade.time.sleep(backoff)
                    continue
                return None  # exhausted retries on lock error — fail open
            raise  # non-lock error — propagate, don't swallow


def _backfill_session_metrics(conn, days=None, limit=None):
    """Populate derived session metrics for rows collected before fields existed.
    
    Bounded per-flush work (limit=50) to prevent re-parse storms. Batched
    UPDATEs in single transaction to minimize lock duration. Retry with backoff
    on DB lock errors (mirrors Total Recall's pattern).
    """
    _measure_facade = _compat.facade()
    cutoff = (_measure_facade.datetime.now() - _measure_facade.timedelta(days=days)).strftime("%Y-%m-%d")
    
    def fetch_rows():
        _measure_facade = _compat.facade()
        return conn.execute(
            """SELECT jsonl_path
               FROM session_log
               WHERE date >= ?
                 AND (
                       IFNULL(cache_ttl_scanned, 0) = 0
                    OR avg_call_gap_seconds IS NULL
                    OR max_call_gap_seconds IS NULL
                    OR p95_call_gap_seconds IS NULL
                    OR quality_score IS NULL
                 )
               ORDER BY date DESC, collected_at DESC
               LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
    
    rows = _measure_facade._retry_db_operation(conn, fetch_rows)
    if rows is None:
        return 0

    updates = []
    for row in rows:
        jsonl_path = row[0]
        ttl_1h = 0
        ttl_5m = 0
        avg_gap = None
        max_gap = None
        p95_gap = None
        q_score = None
        q_grade = None
        parsed = _measure_facade._parse_session_jsonl(jsonl_path) if jsonl_path and _measure_facade.os.path.exists(jsonl_path) else None
        if not parsed:
            continue
        if parsed:
            ttl_1h = int(parsed.get("total_cache_create_1h", 0) or 0)
            ttl_5m = int(parsed.get("total_cache_create_5m", 0) or 0)
            avg_gap = parsed.get("avg_call_gap_seconds")
            max_gap = parsed.get("max_call_gap_seconds")
            p95_gap = parsed.get("p95_call_gap_seconds")
            sq = _measure_facade.score_session_quality(parsed)
            q_score = sq["score"]
            q_grade = sq["grade"]
        updates.append((ttl_1h, ttl_5m, avg_gap, max_gap, p95_gap, q_score, q_grade, str(jsonl_path)))
    
    if not updates:
        return 0
    
    def batch_update():
        # Batch all UPDATEs into one commit to minimize lock duration (Total
        # Recall's small-chunk pattern). Deliberately NO explicit BEGIN IMMEDIATE:
        # a caller (collect_sessions) may have uncommitted inserts on this same
        # conn — an explicit BEGIN would raise "transaction within a transaction",
        # and rolling back on that error would DISCARD the caller's pending work.
        # Sharing the implicit transaction commits our updates and theirs together.
        _measure_facade = _compat.facade()
        conn.executemany(
            """UPDATE session_log
               SET cache_create_1h_tokens = ?,
                   cache_create_5m_tokens = ?,
                   cache_ttl_scanned = 1,
                   avg_call_gap_seconds = ?,
                   max_call_gap_seconds = ?,
                   p95_call_gap_seconds = ?,
                   quality_score = COALESCE(quality_score, ?),
                   quality_grade = COALESCE(quality_grade, ?)
               WHERE jsonl_path = ?""",
            updates,
        )
        conn.commit()
        return len(updates)
    
    result = _measure_facade._retry_db_operation(conn, batch_update)
    return result if result is not None else 0


def _backfill_daily_usage(conn, days=None, budget_seconds=None):
    """Fill daily_usage_json for rows collected before per-day attribution.

    Time-boxed rather than count-capped: parses are memoized and most rows are
    small, so the window usually converges in one pass, and a huge history
    simply finishes on later flushes. Hook rollups without day evidence stay
    incomplete; missing transcripts can be filled when their files return.
    """
    _measure_facade = _compat.facade()
    rows = _measure_facade._retry_db_operation(conn, lambda: conn.execute(
        """SELECT jsonl_path, date FROM session_log
           WHERE daily_usage_json IS NULL
           ORDER BY collected_at DESC"""
    ).fetchall())
    if not rows:
        return 0
    deadline = _measure_facade.time.monotonic() + budget_seconds
    updates = []
    live_owners = _measure_facade._live_usage_owners(conn)
    for jsonl_path, stored_date in rows:
        if _measure_facade.time.monotonic() > deadline:
            break
        if not (jsonl_path and str(jsonl_path).endswith(".jsonl")):
            # A hook-rollup adapter row has no transcript to split by day:
            # settle it now so it is never rescanned. The other daily fields
            # stay NULL: no evidence was produced, so nothing is claimed.
            updates.append(("{}", None, None, None, "", None, None, None, jsonl_path))
            continue
        if not _measure_facade.os.path.exists(jsonl_path):
            # Transcript missing, maybe only for now (a synced folder not yet
            # downloaded, an unmounted drive): stay NULL so it fills in when the
            # file returns, even with an old mtime that collect would skip.
            continue
        try:
            evidence = _measure_facade._session_activity_evidence(jsonl_path, conn=conn, live_owners=live_owners)
        except Exception:
            evidence = None
        if evidence is None:
            updates.append(("{}", _measure_facade._canonical_session_uuid(jsonl_path) if jsonl_path else None,
                            None, None, "", None, None, None, jsonl_path))
            continue
        bounds = evidence["bounds"]
        updates.append((_measure_facade.json.dumps(evidence["daily_usage"]),
                        _measure_facade._canonical_session_uuid(jsonl_path) if jsonl_path else None,
                        bounds[0] if bounds else None, bounds[-1] if bounds else None,
                        bounds[-1] or "",
                        _measure_facade.json.dumps(evidence["parent_usage"]),
                        _measure_facade.json.dumps(evidence["calls"]),
                        _measure_facade.json.dumps(evidence["metrics"]),
                        jsonl_path))
    if not updates:
        return 0

    def batch_update():
        _measure_facade = _compat.facade()
        conn.executemany(
            """UPDATE session_log
               SET daily_usage_json = ?,
                   session_uuid = COALESCE(session_uuid, ?),
                   first_activity_day = ?, last_activity_day = ?, date = ?,
                   parent_daily_usage_json = ?, daily_calls_json = ?,
                   daily_metrics_json = ?
               WHERE jsonl_path = ?""",
            updates,
        )
        conn.commit()
        return len(updates)

    result = _measure_facade._retry_db_operation(conn, batch_update)
    return result if result is not None else 0


def _extract_session_uuid(session_id):
    """Return (session_uuid, unjoinable) for a session_id string.

    A valid Claude session UUID has the 8-4-4-4-12 hex pattern and maps to a
    JSONL file basename. Short opaque hex agent_ids (<=20 chars, no dashes,
    no UUID pattern) cannot be resolved to a session_log row; they are flagged
    unjoinable=1 so callers never silently price them at the Sonnet fallback.

    Returns:
      (uuid_str, False) — session_id IS a UUID and is safe to join on.
      (None, True)      — short agent_id, cannot join.
      (None, False)     — empty/unknown/test id, not an error, just no UUID.
    """
    _measure_facade = _compat.facade()
    import re as _re
    _UUID_PAT = _re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        _re.IGNORECASE,
    )
    if not session_id or session_id in ("unknown", "test-123", "perf_test", "regtest", "demo"):
        return None, False
    if session_id.startswith('rollout-'):
        match = _measure_facade.re.search(r'([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$', session_id, _measure_facade.re.I)
        if match:
            return match.group(1), False
    if _UUID_PAT.match(session_id):
        return session_id, False
    # Short opaque hex without dashes (17-char agent_ids from Claude Code)
    if len(session_id) <= 20 and "-" not in session_id:
        return None, True
    return None, False


def _log_spawn_failure(msg):
    """Append a durable breadcrumb when a detached spawn returns None.

    spawn_detached swallows OSError and returns None; every caller ignores it,
    so a silent spawn failure leaves no trace. This writes one line to the
    daemon log dir so the failure is discoverable post-hoc. Never raises.
    """
    _measure_facade = _compat.facade()
    try:
        _measure_facade.DAEMON_LOG_DIR.mkdir(parents=True, exist_ok=True)
        _measure_facade._cap_append_log(_measure_facade.DAEMON_LOG_DIR / "spawn-failures.log")
        with open(_measure_facade.DAEMON_LOG_DIR / "spawn-failures.log", "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (_measure_facade.time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
    except OSError:
        pass


def _log_savings_event(event_type, tokens_saved, session_id=None, detail=None, model=None,
                       cost_per_mtok=None):
    """Log a savings event to the trends database.

    Cost is calculated at the session's actual model rate. Resolution order:
    explicit `model` arg → session JSONL (via session_id) → CLAUDE_MODEL env →
    trends DB dominant → Sonnet fallback. See `_resolve_session_model`.

    `cost_per_mtok` overrides the default input-rate pricing for event classes
    whose avoided token is NOT an input-class token (e.g. resume_lean prices at
    the cache-read / 1h cache-write rate of the avoided reload). When given, the
    stored cost is tokens_saved * cost_per_mtok / 1e6 at the caller's rate.

    Also writes session_uuid (the Claude UUID = JSONL stem) so aggregation
    joins directly on session_log.session_uuid rather than LIKE-scanning
    jsonl_path. Agent_ids that cannot be joined are flagged unjoinable=1 so
    the reprice logic skips them instead of silently attributing them to Sonnet.
    """
    _measure_facade = _compat.facade()
    try:
        # Derive the stable join key before opening the DB connection.
        session_uuid, unjoinable = _measure_facade._extract_session_uuid(session_id)

        # Calculate cost saved using input token rate for the active model.
        # Unjoinable rows still get priced via env/recent fallback so the stored
        # cost_saved_usd is a reasonable estimate; the unjoinable flag tells the
        # aggregation layer not to use the stored model for reprice attribution.
        tier = _measure_facade._load_pricing_tier()
        tier_data = _measure_facade.PRICING_TIERS.get(tier, _measure_facade.PRICING_TIERS["anthropic"])
        # Output-class events (a measured verbosity reduction) avoid output tokens,
        # so they are priced at the output rate rather than the input rate.
        output_class = _measure_facade._EVENT_RATE_CLASS.get(event_type) == "output"
        if _measure_facade.detect_runtime() == 'codex':
            normalized = model or _measure_facade._resolve_session_model(session_id)
            rates = _measure_facade._input_and_cached_read_rates(normalized)
            if cost_per_mtok is None:
                if output_class:
                    out_rate = _measure_facade._get_model_cost(normalized, 0, 1) * 1e6 if rates else 0.0
                    cost_per_mtok = out_rate or None
                else:
                    cost_per_mtok = rates[0] if rates else None
        elif model:
            normalized = _measure_facade._claude_price_key(model, tier_data["claude_models"]) or "sonnet"
        else:
            normalized = _measure_facade._resolve_session_model(session_id)
        if _measure_facade.detect_runtime() != 'codex':
            rates = tier_data["claude_models"].get(normalized, tier_data["claude_models"].get("sonnet", {}))
            if cost_per_mtok is None:
                cost_per_mtok = (rates.get("output", 15.0) if output_class
                                 else rates.get("input", 3.0))
        cost_saved = tokens_saved * cost_per_mtok / 1e6 if cost_per_mtok is not None else None

        conn = _measure_facade._init_trends_db()
        try:
            o_col, o_mark, o_val = _measure_facade._origin_insert_parts(conn, "savings_events")
            conn.execute(
                "INSERT INTO savings_events "
                "(timestamp, event_type, tokens_saved, cost_saved_usd, session_id, session_uuid, detail, model, unjoinable"
                + o_col + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?" + o_mark + ")",
                (_measure_facade.datetime.now().isoformat(), event_type, tokens_saved, cost_saved,
                 session_id, session_uuid, detail, normalized, 1 if unjoinable else 0) + o_val,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # Never crash the caller over savings tracking


def _estimate_tokens(text):
    """Estimate token count via the shared calibrated estimator (token_estimate.py).

    Falls back to bytes/4 only if the shared estimator is unavailable.
    """
    _measure_facade = _compat.facade()
    pass
    if not text:
        return 0
    if _measure_facade._cached_estimate_fn is None:
        try:
            from token_estimate import estimate_tokens as _fn
            _measure_facade._cached_estimate_fn = _fn
        except Exception:
            _measure_facade._cached_estimate_fn = lambda t: len(t.encode("utf-8", errors="replace")) // 4
    return _measure_facade._cached_estimate_fn(text)


def _log_compression_event(feature, original_text=None, compressed_text=None,
                           session_id=None, command_pattern=None,
                           quality_preserved=None, verified=None, detail=None,
                           tier=None):
    """Log a compression event to the trends database.

    Token counts come from the shared calibrated estimator (_estimate_tokens ->
    token_estimate.py); it falls back to a bytes/4 proxy only if that module is
    unavailable. Never crashes the caller -- all errors silently caught.

    Writes session_uuid (the stable join key) and resolves + stores model
    at event-time so _get_merged_savings can reprice at the session's real
    model rate instead of the current dashboard session model (anachronistic).
    """
    _measure_facade = _compat.facade()
    try:
        original_tokens = _measure_facade._estimate_tokens(original_text)
        compressed_tokens = _measure_facade._estimate_tokens(compressed_text)
        ratio = 0.0
        if original_tokens > 0:
            ratio = round(1.0 - compressed_tokens / original_tokens, 4)

        # DB boundary: command_pattern/detail can embed a file name or label
        # that carries a credential shape. Redact here so NO caller (present
        # or future) can persist them raw; on a redactor refusal the text
        # columns go NULL while token counts still land. feature/session_id
        # are caller text too — a no-op on real inputs (constant feature
        # names, host-generated ids, so joins still hold), but a tainted
        # value can no longer reach disk verbatim.
        try:
            from credential_patterns import redact_credentials as _ce_redact
            if feature:
                feature = _ce_redact(feature)
            if session_id:
                session_id = _ce_redact(session_id)
            if command_pattern:
                command_pattern = _ce_redact(command_pattern)
            if detail:
                detail = _ce_redact(detail)
        except Exception:
            feature = "unknown"
            session_id = None
            command_pattern = None
            detail = None

        # Derive stable join key and resolve event-time model.
        session_uuid, _ = _measure_facade._extract_session_uuid(session_id)
        resolved_model = _measure_facade._resolve_session_model(session_id)

        conn = _measure_facade._init_trends_db()
        try:
            o_col, o_mark, o_val = _measure_facade._origin_insert_parts(conn, "compression_events")
            conn.execute(
                "INSERT INTO compression_events "
                "(timestamp, session_id, session_uuid, feature, command_pattern, original_tokens, "
                "compressed_tokens, compression_ratio, quality_preserved, verified, detail, model, tier"
                + o_col + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?" + o_mark + ")",
                (_measure_facade.datetime.now().isoformat(), session_id, session_uuid, feature, command_pattern,
                 original_tokens, compressed_tokens, ratio,
                 1 if quality_preserved else 0,
                 1 if verified else 0,
                 detail, resolved_model, tier) + o_val,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _compression_known_model(model):
    """Reject an unknown Claude generation even when its family has a card."""
    _measure_facade = _compat.facade()
    if not isinstance(model, str) or not _measure_facade._is_priced_model(model):
        return False
    value = _measure_facade._strip_provider_prefixes(model)
    if value.startswith(("gpt-", "gemini-")):
        return _measure_facade._foreign_savings_card_exact(model)
    generation = _measure_facade._CLAUDE_GENERATION_RE.search(model.lower())
    legacy = _measure_facade._CLAUDE_LEGACY_ID_RE.search(model.lower())
    if legacy:
        major, minor, family = legacy.groups()
        if major == "3" and family == "sonnet" and minor in (None, "5", "7"):
            return True
    elif generation:
        family, major, minor = generation.groups()
    else:
        return True
    family = "fable" if family == "mythos" else family
    key = f"{family}_{major}" + (f"_{minor}" if minor else "")
    return key in _measure_facade.PRICING_TIERS.get(_measure_facade._load_pricing_tier(), _measure_facade.PRICING_TIERS["anthropic"]).get("claude_models", {})


def _compression_row_rate(model, fallback_rate, tier_data):
    """Input price per million tokens, or zero when no rate card is known."""
    _measure_facade = _compat.facade()
    if not _measure_facade._compression_known_model(model):
        return 0.0
    return _measure_facade._get_model_cost(model, 1, 0) * 1_000_000


def _compression_session_rates(conn, session_uuids, session_ids=None):
    """{session_uuid: [(share, input_rate)]} from each session's own model mix.

    Used for compression rows logged without a usable model stamp: the hook
    often cannot read the live transcript, so it writes "unknown". Each row is
    priced at ITS OWN session's mix (never a window average), the same rule
    _session_model_reprice applies to savings_events."""
    _measure_facade = _compat.facade()
    out = {}
    ids = [u for u in set(session_uuids) if u]
    foreign_ids = [s for s in set(session_ids) if s]
    if not ids and not foreign_ids:
        return out
    try:
        sl_cols = {r[1] for r in conn.execute("PRAGMA table_info(session_log)").fetchall()}
        mu = "model_usage_json" if "model_usage_json" in sl_cols else "NULL"
        amu = "all_model_usage_json" if "all_model_usage_json" in sl_cols else "NULL"
        breakdown = "model_usage_breakdown_json" if "model_usage_breakdown_json" in sl_cols else "NULL"
        for i in range(0, len(ids) if "session_uuid" in sl_cols else 0, 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" for _ in chunk)
            for uuid, detail_json, mu_json, amu_json in conn.execute(
                    f"SELECT session_uuid, {breakdown}, {mu}, {amu} FROM session_log "
                    f"WHERE session_uuid IN ({marks}) ORDER BY id", chunk):
                terms = _measure_facade._compression_mix_input_terms(detail_json, mu_json, amu_json)
                if terms:
                    out[uuid] = terms
        prefixes = ("hermes:", "copilot:", "cursor:", "grok:",
                    "antigravity:antigravity:", "antigravity:antigravity-cli:",
                    "antigravity:antigravity-ide:")
        for i in range(0, len(foreign_ids), 50):
            paths = {prefix + sid: sid for sid in foreign_ids[i:i + 50] for prefix in prefixes}
            marks = ",".join("?" for _ in paths)
            matches = {}
            for path, detail_json, mu_json, amu_json in conn.execute(
                    f"SELECT jsonl_path, {breakdown}, {mu}, {amu} FROM session_log WHERE jsonl_path IN ({marks})", tuple(paths)):
                matches.setdefault(paths[path], []).append((detail_json, mu_json, amu_json))
            for sid, records in matches.items():
                if len(records) == 1:
                    terms = _measure_facade._compression_mix_input_terms(*records[0])
                    if terms:
                        out[("session_id", sid)] = terms
    except (_measure_facade.sqlite3.Error, TypeError, ValueError):
        return {}
    return out


def _compression_mix_input_terms(*blobs):
    """Known input rates weighted by all models in one session, including unknowns."""
    _measure_facade = _compat.facade()
    for blob in blobs:
        try:
            values = _measure_facade.json.loads(blob) if blob else None
        except (TypeError, ValueError):
            continue
        if not isinstance(values, dict) or not values:
            continue
        weights = []
        for name, value in values.items():
            if isinstance(value, dict):
                try:
                    value = sum(float(value.get(field) or 0) for field in
                                ("fresh_input", "cache_create", "output"))
                except (TypeError, ValueError):
                    continue
            try:
                weight = float(value)
            except (TypeError, ValueError):
                continue
            if _measure_facade.math.isfinite(weight) and weight > 0:
                weights.append((name, weight))
        total = sum(weight for _, weight in weights)
        if total:
            return [(weight / total, _measure_facade._get_model_cost(name, 1, 0) * 1_000_000)
                    for name, weight in weights if _measure_facade._compression_known_model(name)]
    return []


def _compression_rate_coverage(model, session_uuid, session_rates, fallback_rate, tier_data):
    """Return the known input price and the fraction of saved tokens priced."""
    _measure_facade = _compat.facade()
    if _measure_facade._compression_known_model(model):
        return _measure_facade._compression_row_rate(model, fallback_rate, tier_data), 1.0
    terms = session_rates.get(session_uuid, [])
    return sum(sh * rate for sh, rate in terms), min(1.0, sum(sh for sh, _ in terms))


def _compression_priced_rate(model, session_uuid, session_rates, fallback_rate, tier_data):
    _measure_facade = _compat.facade()
    return _measure_facade._compression_rate_coverage(model, session_uuid, session_rates, fallback_rate, tier_data)[0]


def _get_compression_summary(days=None, since=None):
    """Query compression events and return a summary dict.

    When *since* is a non-empty ISO-8601 timestamp string, it is used directly
    as the cutoff (``timestamp >= since``) instead of the rolling ``now - days``
    cutoff. When *since* is None / empty, the rolling cutoff is computed from
    *days* as before.

    Price known model stamps or the known share of the event's own session mix.
    Keep tokens with no known rate in the count and disclose them as unpriced.
    """
    _measure_facade = _compat.facade()
    try:
        conn = _measure_facade._init_trends_db()
        try:
            if since:
                cutoff = since
            else:
                cutoff = (_measure_facade.datetime.now() - _measure_facade.timedelta(days=days)).isoformat()
            # Pull model alongside aggregates so we can reprice by event-time model.
            # NULLs land as None in Python; fallback handled below.
            # REALIZED tiers only: opportunity (shadow) and estimated rows are
            # diagnostic and MUST NEVER reach the savings headline (three-tier
            # rule). Legacy rows predate the tier column (NULL) and stay counted.
            rows = conn.execute(
                "SELECT feature, COUNT(*) as cnt, "
                "SUM(original_tokens) as orig, SUM(compressed_tokens) as comp, "
                "AVG(compression_ratio) as avg_ratio, "
                "SUM(CASE WHEN quality_preserved = 1 THEN 1 ELSE 0 END) as quality_ok, "
                "SUM(CASE WHEN verified = 1 THEN 1 ELSE 0 END) as verified_cnt, "
                "model, session_uuid, session_id, "
                "SUM(CASE WHEN original_tokens > compressed_tokens "
                "    THEN original_tokens - compressed_tokens ELSE 0 END) as gross_credit, "
                "SUM(CASE WHEN compressed_tokens > original_tokens "
                "    THEN compressed_tokens - original_tokens ELSE 0 END) as gross_debit "
                "FROM " + _measure_facade._counted_table(conn, "compression_events") + " WHERE timestamp >= ? "
                "AND (tier IS NULL OR tier = 'measured') "
                "GROUP BY feature, model, session_uuid, session_id ORDER BY orig DESC",
                (cutoff,),
            ).fetchall()
            session_rates = _measure_facade._compression_session_rates(
                conn, [r[8] for r in rows
                       if not _measure_facade._compression_known_model(r[7])],
                [r[9] for r in rows if not _measure_facade._compression_known_model(r[7])])
        finally:
            conn.close()

        # Merge rows with the same feature (may appear once per model after the
        # GROUP BY feature, model). Accumulate cost using per-row model rate.
        by_feature: dict = {}
        total_original = 0
        total_compressed = 0
        total_events = 0
        fallback_rate = _measure_facade._estimate_compression_cost_per_mtok()
        tier = _measure_facade._load_pricing_tier()
        tier_data = _measure_facade.PRICING_TIERS.get(tier, _measure_facade.PRICING_TIERS["anthropic"])

        for (feature, cnt, orig, comp, avg_ratio, quality_ok, verified_cnt, model, s_uuid, sid,
             gross_credit, gross_debit) in rows:
            orig = orig or 0
            comp = comp or 0
            cnt = cnt or 0
            key = s_uuid if s_uuid in session_rates else ("session_id", sid)
            rate, priced_share = _measure_facade._compression_rate_coverage(model, key, session_rates, fallback_rate, tier_data)
            tokens_saved = orig - comp
            cost_saved = round(tokens_saved * rate / 1_000_000, 6)
            # Gross credits and debits are kept apart (summed per event in SQL), so
            # an unknown-model expansion is disclosed rather than netted away.
            uncovered = max(0.0, 1.0 - priced_share)
            unpriced_credit = (gross_credit or 0) * uncovered
            unpriced_debit = (gross_debit or 0) * uncovered

            if feature in by_feature:
                # Merge with existing entry for this feature (multi-model rows)
                entry = by_feature[feature]
                entry["events"] += cnt
                entry["original_tokens"] += orig
                entry["compressed_tokens"] += comp
                entry["tokens_saved"] += tokens_saved
                entry["cost_saved_usd"] = round(entry["cost_saved_usd"] + cost_saved, 6)
                entry["unpriced_credit_tokens"] += unpriced_credit
                entry["unpriced_debit_tokens"] += unpriced_debit
                # avg_ratio and quality_pcts are approximate after merging; acceptable.
                entry["quality_preserved_pct"] = round(
                    (entry["quality_preserved_pct"] * (entry["events"] - cnt) + 100 * (quality_ok or 0))
                    / max(entry["events"], 1),
                    1,
                )
            else:
                by_feature[feature] = {
                    "events": cnt,
                    "original_tokens": orig,
                    "compressed_tokens": comp,
                    "avg_ratio": round(avg_ratio or 0.0, 4),
                    "tokens_saved": tokens_saved,
                    "cost_saved_usd": cost_saved,
                    "unpriced_credit_tokens": unpriced_credit,
                    "unpriced_debit_tokens": unpriced_debit,
                    "quality_preserved_pct": round(100 * (quality_ok or 0) / max(cnt, 1), 1),
                    "verified_pct": round(100 * (verified_cnt or 0) / max(cnt, 1), 1),
                }
            total_original += orig
            total_compressed += comp
            total_events += cnt

        overall_ratio = round(1.0 - total_compressed / max(total_original, 1), 4)
        tokens_saved = total_original - total_compressed
        for entry in by_feature.values():
            entry["unpriced_credit_tokens"] = round(entry["unpriced_credit_tokens"])
            entry["unpriced_debit_tokens"] = round(entry["unpriced_debit_tokens"])
            entry["unpriced_tokens"] = entry["unpriced_credit_tokens"] + entry["unpriced_debit_tokens"]

        return {
            "total_events": total_events,
            "total_original_tokens": total_original,
            "total_compressed_tokens": total_compressed,
            "total_tokens_saved": tokens_saved,
            "unpriced_tokens": sum(entry["unpriced_tokens"] for entry in by_feature.values()),
            "unpriced_credit_tokens": sum(e["unpriced_credit_tokens"] for e in by_feature.values()),
            "unpriced_debit_tokens": sum(e["unpriced_debit_tokens"] for e in by_feature.values()),
            "overall_ratio": overall_ratio,
            "by_feature": by_feature,
            "period_days": days,
        }
    except Exception:
        return {"total_events": 0, "total_tokens_saved": 0, "by_feature": {}, "period_days": days}


def _coverage_feature_label(feature):
    """Display label for a coverage feature (falls back through the v5 labels)."""
    _measure_facade = _compat.facade()
    return (
        _measure_facade._COVERAGE_FEATURE_LABELS.get(feature)
        or _measure_facade._V5_COMPRESSION_LABELS.get(feature)
        or feature
    )


def _get_compression_coverage(days=None):
    """Tiered compression-coverage view. Diagnostic only, never summed.

    Groups compression_events by feature into measured / estimated / opportunity
    buckets and reports per-feature events, would-be (shadow) or realized
    (active) avg ratio, and verified %. Tiers are kept strictly separate so a
    shadow opportunity can never be read as realized savings. These numbers do
    NOT feed _get_merged_savings (only _V5_COMPRESSION_CATEGORIES features do).

    Ordinary edits do not establish first-read safety.
    """
    _measure_facade = _compat.facade()
    out = {
        "period_days": days,
        "tiers": {t: [] for t in _measure_facade._COMPRESSION_TIERS},
        "first_read_proxy": None,
        "available": False,
    }
    try:
        conn = _measure_facade._init_trends_db()
        try:
            cutoff = (_measure_facade.datetime.now() - _measure_facade.timedelta(days=days)).isoformat()
            rows = conn.execute(
                "SELECT feature, tier, COUNT(*) as cnt, "
                "SUM(original_tokens) as orig, SUM(compressed_tokens) as comp, "
                "AVG(compression_ratio) as avg_ratio, "
                "SUM(CASE WHEN verified = 1 THEN 1 ELSE 0 END) as vcnt "
                "FROM " + _measure_facade._counted_table(conn, "compression_events") + " WHERE timestamp >= ? "
                "GROUP BY feature, tier",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        # Distinguish a broken query from a legitimately empty DB so the
        # dashboard/CLI can show "unavailable (error)" rather than "no data yet".
        out["error"] = str(exc)
        return out

    for feature, tier, cnt, orig, comp, avg_ratio, vcnt in rows:
        if feature in _measure_facade._NON_SAVINGS_COMPRESSION_FEATURES:
            continue
        cnt = cnt or 0
        orig = orig or 0
        comp = comp or 0
        safe_tier = tier if tier in _measure_facade._COMPRESSION_TIERS else "estimated"
        realized = safe_tier == "measured"
        out["tiers"][safe_tier].append({
            "feature": feature,
            "label": _measure_facade._coverage_feature_label(feature),
            "events": cnt,
            "tokens_saved": max(orig - comp, 0),
            "ratio": round(avg_ratio or 0.0, 4),
            "ratio_kind": "realized" if realized else "would_be",
            "verified_pct": round(100 * (vcnt or 0) / max(cnt, 1), 1),
            "shadow": not realized,
        })
        out["available"] = True

    for bucket in out["tiers"].values():
        bucket.sort(key=lambda e: e["events"], reverse=True)
    return out


def _resolve_active_cohorts():
    """The canonical active-cohort set, preferring read_cache's live constant.

    C2: lazily import read_cache.FIRST_READ_ACTIVE_COHORTS (the hot-path source
    of truth) so this CLI/dashboard module follows the read hook without a hard
    import at module load. Falls back to the local mirror if the import fails,
    and emits a one-line stderr drift warning (once) if the two ever disagree —
    the drift test then fails loudly instead of the tripwire silently judging a
    cohort the read hook no longer serves.
    """
    _measure_facade = _compat.facade()
    pass
    try:
        from read_cache import FIRST_READ_ACTIVE_COHORTS as _live
        live = frozenset(_live)
    except Exception:
        return _measure_facade._FIRST_READ_ACTIVE_COHORTS
    if live != _measure_facade._FIRST_READ_ACTIVE_COHORTS and not _measure_facade._active_cohorts_drift_warned:
        _measure_facade._active_cohorts_drift_warned = True
        try:
            print(
                "[Token Optimizer] WARNING: active-cohort drift between "
                "measure._FIRST_READ_ACTIVE_COHORTS and "
                "read_cache.FIRST_READ_ACTIVE_COHORTS; using the read-hook set.",
                file=_measure_facade.sys.stderr,
            )
        except Exception:
            pass
    return live


def _tripwire_min_samples(cohort) -> int:
    """Sample floor before a cohort can be auto-demoted (T9).

    Interpolated cohorts (thin historical sample) use the tighter floor so a bad
    live signal surfaces faster; full-gate cohorts use the standard floor.
    """
    _measure_facade = _compat.facade()
    return (_measure_facade._TRIPWIRE_MIN_SAMPLES_INTERP if cohort in _measure_facade._INTERPOLATED_COHORTS
            else _measure_facade._TRIPWIRE_MIN_SAMPLES)


def _cohort_basis(cohort) -> str:
    """'interpolated' (adjacent-band graduation) or 'full_gate' (sidecar label)."""
    _measure_facade = _compat.facade()
    return "interpolated" if cohort in _measure_facade._INTERPOLATED_COHORTS else "full_gate"


def _cohort_from_pattern(pattern):
    """Recover a (language, band) cohort from a `lang|band` command_pattern."""
    _measure_facade = _compat.facade()
    if not pattern or "|" not in pattern:
        return None
    lang, band = pattern.split("|", 1)
    lang, band = lang.strip(), band.strip()
    if not lang or not band:
        return None
    return (lang, band)


def _compute_cohort_edit_rates(conn, promoted_at=None):
    """Per-cohort live edit-rate over the last _TRIPWIRE_WINDOW active skeletons.

    Returns {(lang, band): {"skeletons": n, "followups": m, "edit_rate": r}} for
    every active cohort that has at least one measured skeleton. The skeleton
    window is bounded per cohort (most recent N) so an old run of bad edits ages
    out.

    Promote fence (hysteresis hole): for any cohort with a `promoted_at`
    timestamp, BOTH skeleton and follow-up events older than that promotion are
    filtered out, so a re-promoted cohort is judged only on POST-promote history
    and cannot immediately re-demote on the pre-promote edits that demoted it.

    Follow-up window (T6): a global `timestamp >= oldest active-skeleton ts`
    filter is pushed into SQL, then per-cohort follow-ups are bounded to
    oldest(cohort skeleton ts) <= t <= newest(cohort skeleton ts) so an edit
    attributed to an aged-out OR a not-yet-served skeleton can't inflate the rate.
    """
    _measure_facade = _compat.facade()
    promoted_at = promoted_at or {}

    def _fence(cohort):
        _measure_facade = _compat.facade()
        return promoted_at.get(f"{cohort[0]}|{cohort[1]}")

    rates = {}
    # Most-recent measured skeleton events per cohort (id DESC == recency).
    sk_rows = conn.execute(
        "SELECT command_pattern, timestamp FROM compression_events "
        "WHERE feature = ? AND tier = 'measured' AND command_pattern LIKE '%|%' "
        "ORDER BY id DESC",
        (_measure_facade._FEATURE_FIRST_READ_SKELETON,),
    ).fetchall()
    per_cohort_ts = {}
    for pattern, ts in sk_rows:
        cohort = _measure_facade._cohort_from_pattern(pattern)
        if cohort is None:
            continue
        fence = _fence(cohort)
        if fence is not None and ts is not None and ts < fence:
            continue  # pre-promote skeleton: fenced out (T5)
        bucket = per_cohort_ts.setdefault(cohort, [])
        if len(bucket) < _measure_facade._TRIPWIRE_WINDOW:
            bucket.append(ts)
    if not per_cohort_ts:
        return rates

    # T6: oldest in-window skeleton ts across ALL cohorts bounds the follow-up
    # scan, so the query never reads follow-ups that predate every live window.
    all_ts = [t for ts_list in per_cohort_ts.values() for t in ts_list if t is not None]
    global_oldest = min(all_ts) if all_ts else None
    if global_oldest is not None:
        fu_rows = conn.execute(
            "SELECT command_pattern, timestamp FROM compression_events "
            "WHERE feature = ? AND tier = 'measured' AND command_pattern LIKE '%|%' "
            "AND timestamp >= ?",
            (_measure_facade._FEATURE_FIRST_READ_EDIT_FOLLOWUP, global_oldest),
        ).fetchall()
    else:
        fu_rows = conn.execute(
            "SELECT command_pattern, timestamp FROM compression_events "
            "WHERE feature = ? AND tier = 'measured' AND command_pattern LIKE '%|%'",
            (_measure_facade._FEATURE_FIRST_READ_EDIT_FOLLOWUP,),
        ).fetchall()
    followups = {}
    for pattern, ts in fu_rows:
        cohort = _measure_facade._cohort_from_pattern(pattern)
        if cohort is None:
            continue
        fence = _fence(cohort)
        if fence is not None and ts is not None and ts < fence:
            continue  # pre-promote follow-up: fenced out (T5)
        followups.setdefault(cohort, []).append(ts)

    for cohort, ts_list in per_cohort_ts.items():
        skeletons = len(ts_list)
        # Window bounds: oldest skeleton ts (aged-out floor) and newest skeleton
        # ts (a follow-up after the newest served skeleton cannot belong to it).
        valid_ts = [t for t in ts_list if t is not None]
        oldest = min(valid_ts) if valid_ts else None
        newest = max(valid_ts) if valid_ts else None
        fu = followups.get(cohort, [])
        fu_in_window = sum(
            1 for t in fu
            if (oldest is None or t is None or t >= oldest)
            and (newest is None or t is None or t <= newest)
        )
        # Cap follow-ups at the skeleton count (one edit per served skeleton).
        fu_in_window = min(fu_in_window, skeletons)
        edit_rate = (fu_in_window / skeletons) if skeletons else 0.0
        rates[cohort] = {
            "skeletons": skeletons,
            "followups": fu_in_window,
            "edit_rate": round(edit_rate, 4),
            "basis": _measure_facade._cohort_basis(cohort),
        }
    return rates


def _valid_tripwire_payload(data):
    """Schema-check the tripwire sidecar (parity with read_cache).

    `demoted` must be a list of 2-element string pairs; `edit_rates`/`promoted_at`
    (when present) dicts. Used to decide corrupt-vs-valid so a malformed sidecar
    forces a recompute instead of being trusted.
    """
    _measure_facade = _compat.facade()
    if not isinstance(data, dict):
        return False
    demoted = data.get("demoted", [])
    if not isinstance(demoted, list):
        return False
    for pair in demoted:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            return False
        if not all(isinstance(x, str) for x in pair):
            return False
    if not isinstance(data.get("edit_rates", {}), dict):
        return False
    if not isinstance(data.get("promoted_at", {}), dict):
        return False
    return True


def _read_tripwire_sidecar(return_corrupt=None):
    """Load the demoted-cohort sidecar.

    Returns the parsed dict, or {} when missing/unreadable. When the file EXISTS
    but is corrupt/malformed and `return_corrupt=True`, returns the sentinel
    string ``"__corrupt__"`` so the caller (evaluate_cohort_tripwire) can force a
    fresh recompute rather than trust an unparseable verdict (T1). Default
    behavior (return_corrupt=False) keeps the historical empty-dict fail-open for
    read-only consumers (_cohort_tripwire_state / cohorts_promote).
    """
    _measure_facade = _compat.facade()
    path = _measure_facade.SNAPSHOT_DIR / _measure_facade._TRIPWIRE_SIDECAR_NAME
    try:
        if not path.exists():
            return {}
        data = _measure_facade.json.loads(path.read_text(encoding="utf-8"))
    except (_measure_facade.json.JSONDecodeError, OSError, ValueError):
        return "__corrupt__" if return_corrupt else {}
    if not _measure_facade._valid_tripwire_payload(data):
        return "__corrupt__" if return_corrupt else {}
    return data


def _write_tripwire_sidecar(demoted, rates, promoted_at=None):
    """Atomically write the demoted-cohort sidecar (0600).

    `promoted_at` is a {lang|band: epoch_ts} map (hysteresis fence): when a
    cohort is re-promoted, its pre-promote skeleton/follow-up history is fenced
    out of the live edit-rate so it cannot immediately re-demote. Each cohort
    also carries its `basis` (full_gate|interpolated) for the dashboard label.
    """
    _measure_facade = _compat.facade()
    path = _measure_facade.SNAPSHOT_DIR / _measure_facade._TRIPWIRE_SIDECAR_NAME
    promoted_at = promoted_at or {}
    record = {
        "demoted": [list(c) for c in sorted(demoted)],
        "edit_rates": {f"{c[0]}|{c[1]}": rates.get(c, {}) for c in rates},
        "promoted_at": dict(promoted_at),
        "basis": {f"{c[0]}|{c[1]}": _measure_facade._cohort_basis(c) for c in rates},
        "_cached_ts": _measure_facade.time.time(),
    }
    try:
        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = _measure_facade.tempfile.mkstemp(
            prefix=".cohort_tripwire.", suffix=".tmp", dir=str(_measure_facade.SNAPSHOT_DIR))
        try:
            if hasattr(_measure_facade.os, "fchmod"):
                _measure_facade.os.fchmod(fd, 0o600)
            with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as fh:
                _measure_facade.json.dump(record, fh)
            _measure_facade.os.replace(tmp_name, str(path))
        except OSError:
            try:
                _measure_facade.os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        pass


def _tripwire_lock():
    """Bounded portable lease serializing tripwire recompute+write (T4)."""
    _measure_facade = _compat.facade()
    lock_path = _measure_facade.SNAPSHOT_DIR / f"{_measure_facade._TRIPWIRE_LOCK_NAME}.lease"
    with _measure_facade.lease_lock(
        lock_path,
        deadline=_measure_facade.current_deadline(),
        acquire_timeout=0.075,
    ) as acquired:
        yield acquired


def evaluate_cohort_tripwire(force=None):
    """Preserve sticky demotions while outcome-based evidence runs in shadow."""
    # T1: read_corrupt distinguishes a malformed sidecar (force recompute) from a
    # fresh valid one. A corrupt verdict must never satisfy the TTL fast path.
    _measure_facade = _compat.facade()
    sidecar = _measure_facade._read_tripwire_sidecar(return_corrupt=True)
    corrupt = sidecar == "__corrupt__"
    if corrupt:
        sidecar = {}
    if not force and not corrupt:
        try:
            age = _measure_facade.time.time() - float(sidecar.get("_cached_ts", 0))
            if sidecar and age < _measure_facade._TRIPWIRE_CACHE_TTL_SECONDS:
                return {**sidecar, "edit_rates": {}}
        except (TypeError, ValueError):
            pass

    # T4: serialize the recompute+write branch under a portable bounded lease.
    # Contenders fail open by skipping this mutation, never by running unlocked.
    with _measure_facade._tripwire_lock() as acquired:
        if not acquired:
            return sidecar or {}
        # Re-read INSIDE the lock: another process may have just recomputed.
        sidecar = _measure_facade._read_tripwire_sidecar(return_corrupt=True)
        if sidecar == "__corrupt__":
            sidecar = {}
        elif not force:
            try:
                age = _measure_facade.time.time() - float(sidecar.get("_cached_ts", 0))
                if sidecar and age < _measure_facade._TRIPWIRE_CACHE_TTL_SECONDS:
                    return {**sidecar, "edit_rates": {}}
            except (TypeError, ValueError):
                pass

        prev_demoted = set(
            tuple(c) for c in sidecar.get("demoted", [])
            if isinstance(c, (list, tuple)) and len(c) == 2
        )
        promoted_at = sidecar.get("promoted_at", {})
        if not isinstance(promoted_at, dict):
            promoted_at = {}
        # Edits are normal work, not evidence of harm. Keep old demotions
        # until an explicit promotion; shadow outcomes do not change settings.
        rates = {}
        demoted = set(prev_demoted)

        _measure_facade._write_tripwire_sidecar(demoted, rates, promoted_at=promoted_at)

    # T3: build the verdict from the in-memory demoted set + freshly-computed
    # rates rather than re-reading the file. A concurrent writer (or our own
    # not-yet-flushed page cache) could otherwise hand back a stale/ghost verdict.
    return {
        "demoted": [list(c) for c in sorted(demoted)],
        "edit_rates": {f"{c[0]}|{c[1]}": rates.get(c, {}) for c in rates},
        "promoted_at": dict(promoted_at),
        "basis": {f"{c[0]}|{c[1]}": _measure_facade._cohort_basis(c) for c in rates},
        "_cached_ts": _measure_facade.time.time(),
    }


def _cohort_tripwire_state():
    """Read sticky demotions without presenting edits as a harm rate."""
    _measure_facade = _compat.facade()
    out = {"cohorts": [], "demoted": []}
    sidecar = _measure_facade._read_tripwire_sidecar()
    demoted = set(
        tuple(c) for c in sidecar.get("demoted", [])
        if isinstance(c, (list, tuple)) and len(c) == 2
    )
    out["demoted"] = [list(c) for c in sorted(demoted)]
    return out


def cohorts_promote(cohort_args):
    """CLI: clear a tripwire demotion for one or more `lang:band` cohorts.

    The ONLY runtime path that re-promotes a demoted cohort (hysteresis).
    """
    _measure_facade = _compat.facade()
    sidecar = _measure_facade._read_tripwire_sidecar()
    demoted = set(
        tuple(c) for c in sidecar.get("demoted", [])
        if isinstance(c, (list, tuple)) and len(c) == 2
    )
    promoted_at = sidecar.get("promoted_at", {})
    if not isinstance(promoted_at, dict):
        promoted_at = {}
    promoted_at = dict(promoted_at)
    cleared = 0
    now = _measure_facade.time.time()
    for arg in cohort_args:
        if ":" not in arg:
            # T7: a malformed arg (missing the lang:band separator) is almost
            # always a typo; warn rather than silently skip so the operator
            # learns nothing was re-promoted.
            print(f"  [warn] ignoring malformed cohort arg '{arg}' "
                  "(expected lang:band, e.g. python:64-256KB)", file=_measure_facade.sys.stderr)
            continue
        lang, band = arg.split(":", 1)
        cohort = (lang.strip(), band.strip())
        if cohort in demoted:
            demoted.discard(cohort)
            cleared += 1
            # A fresh promotion also authorizes target-outline replacement.
            promoted_at[f"{cohort[0]}|{cohort[1]}"] = now
            promoted_at[f"target_skeleton_v1|{cohort[0]}|{cohort[1]}"] = now
    if cleared:
        rates = {}
        _measure_facade._write_tripwire_sidecar(demoted, rates, promoted_at=promoted_at)
    return cleared
