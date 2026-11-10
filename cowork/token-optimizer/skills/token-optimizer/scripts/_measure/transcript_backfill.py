"""Rebuild ledger events from Token Optimizer's own markers in transcripts.

Hooks that run somewhere else (a cloud machine, any future remote runtime) log
their events to a database on that machine. The transcript comes home; the
events do not. When a session's transcript shows Token Optimizer ran but this
machine's ledger holds no event for it, the compressions it made are rebuilt
here from the pointer Token Optimizer itself left in each tool result.

Rules:
  * Only Token Optimizer's exact pointer counts (see POINTER_RX). Text that only
    mentions the marker (source code, docs, escaped JSON) does not match,
    because the real pointer is always the tail of the tool result.
  * Qualify sessions by Token Optimizer evidence, never by directory name.
    In mixed sessions, skip markers matching a nearby native event.
  * original tokens come from the size the pointer states (measured);
    compressed tokens from the text actually in the transcript. Both go through
    the shared calibrated estimator, as the hooks themselves log them.
  * Markers with no stated original size are counted, never sized or written.
  * Every row carries origin='transcript_backfill' and a unique backfill_key
    (session uuid + tool_use_id), so a second run inserts nothing.

The facade is passed in (helpers and constants), so this module holds no
import-time state and imports nothing from measure.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

ORIGIN = "transcript_backfill"
META_CURSOR = "transcript_backfill_cursor_mixed_v2"
BASH_FEATURE = "bash_compress_pipeline"

# archive_result.build_archive_pointer and the MCP replacement in
# archive_result.archive_result: a header line stating the original size, the
# retrieval instruction, then "expand <key>" and the closing bracket, always at
# the very end of the tool result.
POINTER_RX = re.compile(
    r"\[Full result archived \((?P<chars>\d{1,3}(?:,\d{3})*|\d+) chars"
    r"(?: \((?P<kind>[a-z]+)\))?(?P<truncated>, truncated to 5MB)?\)"
    r"(?:\.\]| — saved to disk, not lost\.\n(?:[^\n]*\n){0,3}?[^\n]*\bexpand [\w\-]+(?:\]|\n\]))\s*\Z")

# Token Optimizer output that states no original size. Counted, never sized.
UNSIZED_RX = {
    "lines_omitted": re.compile(r"(?m)^\.\.\. \d+ lines omitted(?: \(kept \d+ error/warning lines\))? \.\.\.$"),
    "crossturn_identical": re.compile(r"\A\[Token Optimizer: identical to your previous `"),
    "crossturn_delta": re.compile(r"\A\[Token Optimizer: same as your previous `"),
    "search_results": re.compile(r"\A\[Search results: \d+ matches across \d+ files\. "),
}

# Evidence that Token Optimizer's hooks ran in a session, read only from hook
# attachments (never from tool output, which can quote anything).
_HOOK_ATTACHMENTS = ("hook_system_message", "hook_additional_context", "hook_success",
                     "hook_non_blocking_error", "hook_cancelled")
_HOOK_TRACES = ("[Token Optimizer]", "bash_compress_hook budget exceeded")


def _texts(content):
    if isinstance(content, str):
        return [content]
    out = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                out.append(block["text"])
    return out


def _hook_trace(attachment):
    if not isinstance(attachment, dict) or attachment.get("type") not in _HOOK_ATTACHMENTS:
        return None
    for field in ("content", "stdout", "stderr"):
        value = attachment.get(field)
        if isinstance(value, str):
            for trace in _HOOK_TRACES:
                if trace in value:
                    return trace
    return None


def scan_transcript(path):
    """One pass over a transcript. Never raises.

    Returns {"traces": {...}, "sized": [event...], "unsized": {name: n},
             "pointer_results": n}. Each sized event: tool_use_id, ts (UTC
    ISO string), tool, model, original_chars, kind, text (the tool result text).
    """
    out = {"traces": {}, "sized": [], "unsized": {}, "pointer_results": 0, "error": None}
    tool_uses = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                is_assistant = '"tool_use"' in line and '"assistant"' in line
                is_result = '"tool_result"' in line
                is_hook = '"attachment"' in line and any(t in line for t in _HOOK_TRACES)
                if not (is_assistant or is_result or is_hook):
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict) or record.get("isSidechain"):
                    continue
                if is_hook and record.get("type") == "attachment":
                    trace = _hook_trace(record.get("attachment"))
                    if trace:
                        out["traces"][trace] = out["traces"].get(trace, 0) + 1
                    continue
                message = record.get("message")
                if not isinstance(message, dict) or not isinstance(message.get("content"), list):
                    continue
                if record.get("type") == "assistant":
                    model = message.get("model")
                    model = model if isinstance(model, str) and model and not model.startswith("<") else None
                    for block in message["content"]:
                        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                            args = block.get("input")
                            tool_uses[block["id"]] = (block.get("name"), model, args.get("command") if isinstance(args, dict) else None)
                    continue
                if record.get("type") != "user":
                    continue
                for block in message["content"]:
                    if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                        continue
                    text = "\n".join(_texts(block.get("content")))
                    if not text:
                        continue
                    match = POINTER_RX.search(text)
                    if match:
                        out["pointer_results"] += 1
                        tool, model, command = tool_uses.get(block.get("tool_use_id"), (None, None, None))
                        out["sized"].append({
                            "tool_use_id": block.get("tool_use_id"), "ts": record.get("timestamp"),
                            "tool": tool, "model": model, "command": command, "kind": match.group("kind"),
                            "original_chars": int(match.group("chars").replace(",", "")),
                            "truncated": bool(match.group("truncated")), "text": text})
                        continue
                    for name, rx in UNSIZED_RX.items():
                        if rx.search(text):
                            out["unsized"][name] = out["unsized"].get(name, 0) + 1
                            break
    except OSError as exc:
        out["error"] = type(exc).__name__
    return out


def token_optimizer_ran(scan):
    """True when the transcript carries Token Optimizer's own evidence."""
    return bool(scan["pointer_results"] or scan["traces"])


def local_naive(ts_utc):
    """Ledger timestamps are naive local time (datetime.now()); transcripts are UTC."""
    try:
        value = datetime.fromisoformat(str(ts_utc).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().replace(tzinfo=None).isoformat()


def ensure_schema(conn):
    """Additive: origin + backfill_key columns and a unique guard on each table."""
    for table in ("compression_events", "savings_events"):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        if "origin" not in cols:
            conn.execute("ALTER TABLE %s ADD COLUMN origin TEXT" % table)
        if "backfill_key" not in cols:
            conn.execute("ALTER TABLE %s ADD COLUMN backfill_key TEXT" % table)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_%s_backfill_key ON %s (backfill_key) "
                     "WHERE backfill_key IS NOT NULL" % (table, table))


def local_event_sessions(conn):
    """Sessions with any ledger row this machine logged itself (any time)."""
    found = set()
    for table in ("savings_events", "compression_events"):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        where = "WHERE origin IS NULL OR origin != ?" if "origin" in cols else ""
        params = (ORIGIN,) if where else ()
        for uuid, sid in conn.execute("SELECT session_uuid, session_id FROM %s %s" % (table, where), params):
            found.update(v for v in (uuid, sid) if v)
    return found


def unmatched_markers(conn, session_uuid, scan, estimate_tokens, tokens_from_chars):
    """Skip markers with a plausible native row, keeping ambiguous cases conservative."""
    ce = conn.execute("SELECT timestamp, original_tokens, command_pattern FROM compression_events WHERE "
                      "(session_uuid=? OR session_id=?) AND (origin IS NULL OR origin!=?) "
                      "AND (feature LIKE 'bash_%' OR feature IN ('build_output_compress','crossturn_dedup'))", (session_uuid, session_uuid, ORIGIN)).fetchall()
    se = conn.execute("SELECT timestamp, tokens_saved, detail FROM savings_events WHERE "
                      "(session_uuid=? OR session_id=?) AND (origin IS NULL OR origin!=?) "
                      "AND event_type='tool_archive'", (session_uuid, session_uuid, ORIGIN)).fetchall()
    def near(stamp, other):
        try:
            return abs((datetime.fromisoformat(stamp) - datetime.fromisoformat(other)).total_seconds()) <= 20
        except (ValueError, TypeError):
            return False
    def same_command(a, b):
        if not a or not b or a == b:
            return True
        try:
            import shlex
            return shlex.split(a) == shlex.split(b)
        except ValueError:
            return False
    missing = []
    for event in scan['sized']:
        stamp = local_naive(event.get('ts'))
        if not stamp or not event.get('tool'):
            continue
        if event['tool'] == 'Bash':
            from .counted_subagents import _stored_command
            command = _stored_command(event['command']) if isinstance(event.get('command'), str) else None
            match = any(near(stamp, ts) and same_command(command, pattern)
                        for ts, _orig, pattern in ce)
        else:
            match = any(near(stamp, ts) and (not detail or detail.startswith('replaced ' + event['tool'] + ' ('))
                        for ts, _tokens, detail in se)
        if not match:
            missing.append(event)
    return dict(scan, sized=missing)


def build_rows(session_uuid, scan, estimate_tokens, tokens_from_chars, input_rate):
    """(compression rows, savings rows, skipped counts) for one session's sized markers."""
    ce_rows, se_rows = [], []
    skipped = {"no_tool_use_id": 0, "no_timestamp": 0, "no_reduction": 0}
    for event in scan["sized"]:
        if not event["tool_use_id"]:
            skipped["no_tool_use_id"] += 1
            continue
        ts = local_naive(event["ts"])
        if not ts:
            skipped["no_timestamp"] += 1
            continue
        original = int(tokens_from_chars(event["original_chars"]))
        compressed = int(estimate_tokens(event["text"]))
        if original <= compressed:
            skipped["no_reduction"] += 1
            continue
        key = "%s:%s" % (session_uuid, event["tool_use_id"])
        if event["tool"] == "Bash":
            ce_rows.append((ts, session_uuid, BASH_FEATURE, original, compressed,
                            round(compressed / original, 4), 1, 1,
                            "rebuilt from transcript marker (%s chars)" % format(event["original_chars"], ","),
                            session_uuid, event["model"], "measured", ORIGIN, key))
        else:
            saved = original - compressed
            rate = input_rate(event["model"])
            se_rows.append((ts, "tool_archive", saved,
                            None if rate is None else saved * rate / 1e6, session_uuid,
                            "replaced %s (%s chars -> %s chars)" % (
                                event["tool"] or "unknown tool", format(event["original_chars"], ","),
                                format(len(event["text"]), ",")),
                            event["model"], session_uuid, 0, ORIGIN, key))
    return ce_rows, se_rows, skipped


_CE_INSERT = ("INSERT OR IGNORE INTO compression_events (timestamp, session_id, feature, original_tokens, "
              "compressed_tokens, compression_ratio, quality_preserved, verified, detail, session_uuid, model, "
              "tier, origin, backfill_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
_SE_INSERT = ("INSERT OR IGNORE INTO savings_events (timestamp, event_type, tokens_saved, cost_saved_usd, "
              "session_id, detail, model, session_uuid, unjoinable, origin, backfill_key) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?)")


def write_rows(conn, ce_rows, se_rows):
    """Insert, then read back by key: returns the number of rows that now exist and were new."""
    keys = [r[-1] for r in ce_rows] + [r[-1] for r in se_rows]
    before = _existing(conn, keys)
    conn.executemany(_CE_INSERT, ce_rows)
    conn.executemany(_SE_INSERT, se_rows)
    after = _existing(conn, keys)
    return len(after - before)


def _existing(conn, keys):
    found = set()
    for i in range(0, len(keys), 400):
        part = keys[i:i + 400]
        marks = ",".join("?" * len(part))
        for table in ("compression_events", "savings_events"):
            found.update(r[0] for r in conn.execute(
                "SELECT backfill_key FROM %s WHERE backfill_key IN (%s)" % (table, marks), part))
    return found


def _cursor(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS token_optimizer_meta (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM token_optimizer_meta WHERE key = ?", (META_CURSOR,)).fetchone()
    try:
        value = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


from . import fixture_context


def candidate_sessions(conn, since_date, is_fixture):
    """Distinct session_log sessions on or after since_date, newest row per session."""
    rows = conn.execute(
        "SELECT session_uuid, jsonl_path FROM session_log WHERE session_uuid IS NOT NULL AND date >= ? "
        "ORDER BY id", (since_date,)).fetchall()
    latest = {}
    for uuid, path in rows:
        if uuid and not is_fixture(uuid) and fixture_context.allowed_path(path):
            latest[uuid] = path or latest.get(uuid)
    return latest


def run(conn, m, since_date, max_files=None, deadline_seconds=None, report=None):
    """Backfill every qualifying session once per transcript change. Fail-open.

    ``m`` is the measure facade (path resolver, fixture test, estimators, cards).
    ``report`` (a dict) collects per-session detail for audits; leave None in
    the collect pass.
    """
    import time
    stats = {"sessions_considered": 0, "files_scanned": 0, "to_ran_ledger_empty": 0,
             "mixed_sessions": 0, "rows_written": 0, "sized_markers": 0, "unsized_markers": 0,
             "no_transcript": 0, "pending": 0}
    try:
        from token_estimate import estimate_tokens, estimate_tokens_from_bytes
        ensure_schema(conn)
        tier = m.PRICING_TIERS.get(m._load_pricing_tier(), m.PRICING_TIERS["anthropic"])

        def input_rate(model):
            card = m._counted_exact_card(model, tier) if model else None
            rate = (card or {}).get("input")
            return rate if isinstance(rate, (int, float)) else None

        cursor = _cursor(conn)
        local = local_event_sessions(conn)
        started = time.monotonic()
        for uuid, logged_path in candidate_sessions(conn, since_date, m._is_fixture_session_id).items():
            stats["sessions_considered"] += 1
            path = Path(logged_path) if logged_path else None
            if path is None or not path.is_file():
                path = m._counted_session_path(conn, uuid)
            try:
                st = path.stat()
            except (AttributeError, OSError):
                stats["no_transcript"] += 1
                continue
            stamp = [st.st_size, st.st_mtime_ns]
            if report is None and cursor.get(uuid) == stamp:
                continue  # unchanged since the last scan
            if (max_files is not None and stats["files_scanned"] >= max_files) or (
                    deadline_seconds is not None and time.monotonic() - started > deadline_seconds):
                stats["pending"] += 1
                continue
            stats["files_scanned"] += 1
            scan = scan_transcript(path)
            if scan["error"]:
                continue
            ran = token_optimizer_ran(scan)
            entry = {"path": str(path), "to_ran": ran, "local_events": uuid in local,
                     "sized": len(scan["sized"]), "unsized": scan["unsized"], "traces": scan["traces"]}
            if ran and uuid in local:
                stats["mixed_sessions"] += 1
            if ran:
                if uuid not in local: stats["to_ran_ledger_empty"] += 1
                stats["sized_markers"] += len(scan["sized"])
                stats["unsized_markers"] += sum(scan["unsized"].values())
                if uuid in local: scan = unmatched_markers(conn, uuid, scan, estimate_tokens, estimate_tokens_from_bytes)
                ce_rows, se_rows, skipped = build_rows(uuid, scan, estimate_tokens,
                                                       estimate_tokens_from_bytes, input_rate)
                written = write_rows(conn, ce_rows, se_rows)
                stats["rows_written"] += written
                entry.update(rows=len(ce_rows) + len(se_rows), written=written, skipped=skipped)
            cursor[uuid] = stamp
            if report is not None:
                report[uuid] = entry
        conn.execute("INSERT OR REPLACE INTO token_optimizer_meta (key, value) VALUES (?, ?)",
                     (META_CURSOR, json.dumps(cursor, separators=(",", ":"))))
        conn.commit()
    except Exception as exc:  # fail-open: never break the collect pass
        stats["error"] = type(exc).__name__
    return stats


def collect_pass(conn, m, drain, budgets, quiet=True):
    """The collect-pass hook: same caps and deadline as the marker backfill."""
    if drain == "full":
        max_files, deadline = None, None
    elif drain == "inline":
        max_files, deadline = None, budgets.get("counted_seconds")
    else:
        max_files, deadline = m._COUNTED_MARKER_FILES_PER_PASS, m._COUNTED_PASS_DEADLINE_SECONDS
    import time
    started = time.monotonic()
    stats = run(conn, m, m._COUNTED_LEDGER_ERA_START[:10], max_files=max_files, deadline_seconds=deadline)
    from . import archive_void_evidence
    remaining = None if deadline is None else max(0.0, deadline - (time.monotonic() - started))
    try: stats["archive_void"] = archive_void_evidence.collect(conn, max_sessions=None if drain == "full" else 8, deadline_seconds=remaining)
    except Exception as exc: stats["archive_void_error"] = type(exc).__name__
    from . import compression_fixture_void
    remaining = None if deadline is None else max(0.0, deadline - (time.monotonic() - started))
    try: stats["fixture_void"] = compression_fixture_void.collect(conn, m._counted_event_utc, m.CLAUDE_DIR / "projects", deadline_seconds=remaining)
    except Exception as exc: stats["fixture_void_error"] = type(exc).__name__
    from . import lean_resume_receivers
    remaining = None if deadline is None else max(0.0, deadline - (time.monotonic() - started))
    try: stats["lean_receivers"] = lean_resume_receivers.collect(conn, m._counted_event_utc, max_files=None if drain == "full" else 8, deadline_seconds=remaining)
    except Exception as exc: stats["lean_receivers_error"] = type(exc).__name__
    if not quiet and stats.get("rows_written"):
        print("[Token Optimizer] transcript backfill: %d session(s), %d row(s)."
              % (stats["to_ran_ledger_empty"], stats["rows_written"]))
    return stats
