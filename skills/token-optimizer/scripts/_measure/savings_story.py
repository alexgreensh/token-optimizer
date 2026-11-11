"""Shared savings story totals."""
from contextvars import ContextVar

_SKIP_COMPARISON = ContextVar('skip_since_install_comparison', default=False)


def since_install_summary(builder, days):
    """Skip an expensive comparison that the since-install card never reads."""
    token = _SKIP_COMPARISON.set(True)
    try:
        return builder(days=days)
    finally:
        _SKIP_COMPARISON.reset(token)


def skip_comparison():
    return _SKIP_COMPARISON.get()

def _number(value):
    try:
        return int(float(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def verbosity_logged(summary):
    return sum(float(row.get('cost_saved_usd') or 0) for name, row in
               (summary.get('by_category') or {}).items()
               if str(name).startswith('verbosity') and isinstance(row, dict))


def excluded_since_install(summary):
    routing_row = summary.get('model_routing') or {}
    lean_row = summary.get('resume_lean_estimated') or {}
    estimate = summary.get('verbosity_steer') or {}
    routing = (float(routing_row['realized_cost_usd']) if routing_row.get('realized_cost_usd') is not None
               and routing_row.get('evidence') not in ('pending', 'unavailable') else None)
    lean = float(lean_row['cost_saved_usd']) if lean_row.get('cost_saved_usd') is not None else None
    verbosity = (verbosity_logged(summary) + float(estimate.get('cost_saved_usd') or 0)
                 if estimate or any(str(k).startswith('verbosity') for k in (summary.get('by_category') or {})) else None)
    return {'routing': routing, 'lean_resumes': lean, 'verbosity': verbosity}


def story_token_total(s):
    """Match the token-bearing rows actually shown in the savings story."""
    a, h = s.get('action_savings') or {}, s.get('token_headline') or {}
    d, o = s.get('structural_detail') or {}, s.get('output_reread') or {}
    ledger = d.get('method') == 'trim_ledger'
    mt = _number(d.get('tokens_saved')) if ledger else 0
    mu = float(d.get('cost_saved_usd') or 0) if ledger else 0
    removed = (_number(h.get('context_removed_tokens')) + _number(h.get('output_not_generated_tokens'))
               if h.get('context_removed_tokens') is not None else
               _number(h.get('removed_tokens')) - _number(h.get('reload_avoided_tokens')))
    total = max(0, removed - mt) if float(a.get('logged_usd') or 0) - mu != 0 else 0
    if a.get('reread_available') is not False and a.get('reread_usd') is not None and float(a.get('reread_usd') or 0) != 0:
        total += _number(h.get('reread_tokens'))
    if ledger and mu > 0:
        total += mt
    estimate = d.get('estimated') or {}
    if ledger and float(estimate.get('cost_saved_usd') or 0) > 0:
        total += _number(estimate.get('tokens_saved'))
    if float(o.get('cost_saved_usd') or 0) > 0:
        total += _number(o.get('tokens_saved'))
    if float(a.get('estimated_usd') or 0) - float((a.get('estimates_usd') or {}).get('output_reread') or 0) >= 0.5:
        total += _number(h.get('reload_avoided_tokens'))
    return total


NON_ANTHROPIC_RUNTIMES = {"codex", "hermes", "copilot", "opencode", "cursor", "antigravity", "grok"}


def routing_baseline_kind(routing, runtime):
    """'stated' (the user's own pre-TO share), 'measured_at_install' (the mix
    recorded at install or the earliest history), or None when routing is not
    estimated: no baseline, no Opus in it, a non-Anthropic runtime, or usage
    still being re-read."""
    routing = routing or {}
    if runtime in NON_ANTHROPIC_RUNTIMES or routing.get("evidence") in ("pending", "unavailable"):
        return None
    try:
        share = float(routing.get("baseline_opus_share") or 0)
    except (TypeError, ValueError):
        return None
    if share <= 0:
        return None
    source = routing.get("baseline_source")
    if source == "pretool_baseline":
        return "stated"
    if source in ("snapshot", "history"):
        return "measured_at_install"
    return None


def last_known_windows(meters, mult, live):
    """The stored meter reading per window, kept when every live window was dropped
    for age, so the Savings page still shows the last known used share and what it
    would have been without Token Optimizer. [] while live windows exist or the
    meter never held a value. Pure; never raises."""
    if live:
        return []
    try:
        mult = float(mult)
        out = []
        for key, field, label in (("five_hour", "five_hour_pct", "5h"), ("seven_day", "seven_day_pct", "7d")):
            used = (meters or {}).get(field)
            if not isinstance(used, (int, float)) or used != used:
                continue
            without = min(100.0, used * mult)
            out.append({"key": key, "label": label, "used_pct": round(used, 1),
                        "without_used_pct": round(without, 1), "would_be_capped": without >= 99.5})
        return out
    except (TypeError, ValueError):
        return []


def record_full_bash_fetch(facade, session_id, tool_use_id, response):
    """Record a full fetch separately from savings, including repeat fetches."""
    try:
        conn = facade._init_trends_db()
        try:
            conn.execute('CREATE TABLE IF NOT EXISTS archive_full_fetches '
                         '(timestamp TEXT NOT NULL, session_id TEXT NOT NULL, '
                         'tool_use_id TEXT NOT NULL, tokens INTEGER NOT NULL)')
            conn.execute('INSERT INTO archive_full_fetches VALUES (?, ?, ?, ?)',
                         (facade.datetime.now().isoformat(), session_id, tool_use_id,
                          facade._estimate_tokens(response)))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def full_bash_fetch_totals(conn, cutoff, legacy_rows, is_fixture, snapshot_dir=None):
    """Combine new fetch events with older, provably full archive debits."""
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_full_fetches'").fetchone()
    rows = conn.execute('SELECT timestamp, session_id, tool_use_id, tokens FROM archive_full_fetches WHERE timestamp >= ?', (cutoff,)).fetchall() if exists else []
    rows = [row for row in rows if not is_fixture(row[1])]
    first = {}
    for ts, session, key, _ in rows:
        pair = (session, key)
        first[pair] = min(ts, first.get(pair, ts))
    from datetime import datetime
    def before_new(ts, pair):
        if pair not in first:
            return True
        try:
            return datetime.fromisoformat(ts).timestamp() + 2 < datetime.fromisoformat(first[pair]).timestamp()
        except (TypeError, ValueError, OSError, OverflowError):
            return False
    def verified_full(session, key):
        import json
        from pathlib import Path
        if not snapshot_dir or not session or Path(session).name != session or Path(key).name != key:
            return False
        try:
            item = json.loads((Path(snapshot_dir) / 'tool-archive' / session / (key + '.json')).read_text(encoding='utf-8'))
            return item.get('tool_name') == 'Bash' and item.get('truncated') is False
        except (OSError, ValueError, TypeError):
            return False
    legacy = [int(tokens or 0) for detail, tokens, session, ts in legacy_rows
              if detail and ' (Bash)' in detail and ' chars ' not in detail
              and verified_full(session, detail.split(' ', 1)[0])
              and before_new(ts, (session, detail.split(' ', 1)[0]))]
    return len(rows) + len(legacy), sum(int(row[3]) for row in rows) + sum(legacy)
