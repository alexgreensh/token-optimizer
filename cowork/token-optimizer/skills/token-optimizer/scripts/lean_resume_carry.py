"""Shadow diagnostic for lean-resume carry (savings-truth U6).

For every ``resume_lean`` savings event this walks the resumed session's
transcript and credits the avoided context once per real API turn up to the
first real compaction, capped when the session's own context growth passes the
avoided size or the counterfactual reaches its auto-compaction threshold. The
result is a SHADOW field only: it is never added to
``total_tokens``, ``total_cost_usd``, counted ledger summaries, action savings,
or any dashboard headline total.

The event row records the TARGET session that was lean-resumed, not the session
that received the injected lean block, so the resumed transcript is proxied by
the target session's own ``.jsonl``. That linkage is the method's largest known
weakness and is disclosed in every payload.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from _measure import lean_resume_threshold

_EVENT_TYPE = "resume_lean"
_TURN_CAP = 20000


def _m():
    import measure  # noqa: PLC0415 - lazy: this module is imported by measure providers
    return measure


def _parse_ts(value):
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return None


def _event_dt(m, ts):
    try:
        dt = m._counted_event_utc(ts)
    except Exception:
        dt = None
    return dt or _parse_ts(ts)


def _cutoff(days, since, m):
    if since:
        return str(since)
    return (m.datetime.now() - m.timedelta(days=days or 30)).isoformat()


def _connect(db_path, m):
    if db_path is not None:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=1.5)
    return m._init_trends_db()


def _events(days=None, since=None, db_path=None):
    m = _m()
    conn = _connect(db_path, m)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, timestamp, tokens_saved, cost_saved_usd, session_id, "
            "session_uuid, model, detail FROM savings_events "
            "WHERE event_type = ? AND timestamp >= ? ORDER BY timestamp",
            (_EVENT_TYPE, _cutoff(days, since, m)),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _sid_for(row, m):
    raw = row.get("session_uuid") or row.get("session_id")
    if not raw:
        return None
    try:
        canonical, _unjoinable = m._extract_session_uuid(str(raw))
        raw = canonical or str(raw)
    except Exception:
        pass
    try:
        safe = m.sanitize_session_id(raw)
    except Exception:
        safe = None
    return safe or str(raw)


def _transcript_for(sid, m):
    try:
        conn = m._open_trends_db_readonly(timeout=1.0)
        try:
            row = conn.execute('SELECT jsonl_path FROM session_log WHERE session_uuid=? AND jsonl_path IS NOT NULL ORDER BY id DESC LIMIT 1', (sid,)).fetchone()
        finally:
            conn.close()
        if row and Path(row[0]).is_file():
            return Path(row[0])
    except (OSError, sqlite3.Error, AttributeError):
        pass
    try:
        base = m.CLAUDE_DIR / "projects"
        candidates = list(base.glob("*/" + sid + ".jsonl")) if base.exists() else []
        return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None
    except (OSError, PermissionError):
        return None


def _prompt_tokens(m, usage):
    try:
        selected = m._context_usage(usage)
        return int(m._context_token_count(selected) or 0)
    except Exception:
        return 0


def _walk(path, ev_floor, avoided, m, threshold):
    """Post-event turns until real or counterfactual compaction or the growth cap.

    Returns (turns, stopped_by) where turns is a list of
    (utc-naive datetime, raw model, context tokens). Never raises; returns
    (None, 'unreadable') when the file cannot be opened.
    """
    turns = []
    seen = {}
    first_prompt = None
    stopped_by = "eof"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"assistant"' not in line and "compact_boundary" not in line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                rtype = record.get("type")
                dt = _parse_ts(record.get("timestamp"))
                if rtype == "system" and record.get("subtype") == "compact_boundary":
                    if dt is not None and dt >= ev_floor:
                        stopped_by = "compaction"
                        break
                    continue
                if (rtype != "assistant" or record.get("isSidechain") or dt is None
                        or dt < ev_floor):
                    continue
                message = record.get("message") or {}
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict) or not usage:
                    continue
                ctx = _prompt_tokens(m, usage)
                if ctx <= 0:
                    continue
                model = message.get("model")
                rid = None
                try:
                    rid = m._usage_request_key(record, message)
                except Exception:
                    rid = None
                if rid is not None and rid in seen:
                    index = seen[rid]
                    if ctx > turns[index][2]:
                        if ctx + avoided >= threshold:
                            del turns[index:]
                            stopped_by = "counterfactual_compaction"
                            break
                        turns[index] = (dt, model or turns[index][1], ctx)
                    continue
                if ctx + avoided >= threshold:
                    stopped_by = "counterfactual_compaction"
                    break
                if first_prompt is None:
                    first_prompt = ctx
                elif ctx - first_prompt > avoided:
                    stopped_by = "growth_cap"
                    break
                turns.append((dt, model, ctx))
                if rid is not None:
                    seen[rid] = len(turns) - 1
                if len(turns) >= _TURN_CAP:
                    stopped_by = "turn_cap"
                    break
    except (OSError, PermissionError):
        return None, "unreadable"
    return turns, stopped_by


def _resolver(m):
    """resolve(model, largest) -> the user's effective compaction settings for a model.

    Transcripts name the model without the [1m] tag, so a session seen past
    200K is resolved as the 1M variant of its model.
    """
    def resolve(model, largest):
        model = str(model or "") or None
        out = m._resolve_compact_window(model)
        if largest > 200000 and int(out.get("model_window") or 0) < 1_000_000 and model and "[1m]" not in model:
            out = m._resolve_compact_window(model + "[1m]")
        if largest > int(out.get("model_window") or 0):
            # The session itself proves a larger window than the model table knows.
            out = dict(out, model_window=1_000_000,
                       tokens=out["tokens"] if out.get("user_override") else m._COMPACT_WINDOW_1M_DEFAULT)
        return out
    return resolve


def _card_for(m, model, prompt_tokens, tier_data, fallback_key):
    card = None
    try:
        if model:
            card = m._counted_exact_card(model, tier_data, prompt_tokens)
    except Exception:
        card = None
    if card is None and fallback_key:
        card = (tier_data.get("claude_models") or {}).get(fallback_key)
    if card is None:
        card = (tier_data.get("claude_models") or {}).get("sonnet") or {}
    return card


def _cache_read_rate(card):
    rate = card.get("cache_read") if isinstance(card, dict) else None
    if not isinstance(rate, (int, float)):
        rate = card.get("input") if isinstance(card, dict) else None
    return float(rate or 0.0)


def _score_event(row, m, tier_data):
    sid = _sid_for(row, m)
    avoided = int(row.get("tokens_saved") or 0)
    out = {
        "event_id": row.get("id"),
        "session": sid,
        "event_ts": row.get("timestamp"),
        "avoided_tokens": avoided,
        "logged_cost_usd": row.get("cost_saved_usd"),
        "turns": 0,
        "carry_tokens": 0,
        "carry_cost_usd": 0.0,
        "stopped_by": "no_transcript",
        "priced": False,
        "threshold_source": None,
    }
    if not sid or avoided <= 0:
        out["stopped_by"] = "no_avoided_context"
        return out
    path = _transcript_for(sid, m)
    if path is None:
        return out
    ev_dt = _event_dt(m, row.get("timestamp"))
    if ev_dt is None:
        out["stopped_by"] = "bad_timestamp"
        return out
    ev_floor = ev_dt.replace(microsecond=0)
    threshold, source = lean_resume_threshold.threshold(path, lambda usage: _prompt_tokens(m, usage), _resolver(m))
    if threshold is None:
        out["stopped_by"] = "unreadable"
        return out
    out["threshold_source"] = source
    turns, stopped_by = _walk(path, ev_floor, avoided, m, threshold)
    if turns is None:
        out["stopped_by"] = "unreadable"
        return out
    out["stopped_by"] = stopped_by or "eof"
    fallback_key = row.get("model")
    cost = 0.0
    for _dt, model, ctx in turns:
        card = _card_for(m, model, ctx, tier_data, fallback_key)
        cost += avoided * _cache_read_rate(card) / 1_000_000.0
    out["turns"] = len(turns)
    out["carry_tokens"] = avoided * len(turns)
    out["carry_cost_usd"] = round(cost, 6)
    out["priced"] = bool(turns)
    return out


def summary(days=30, since=None, db_path=None, include_events=True):
    """Shadow lean-resume carry diagnostic. Never feeds savings totals."""
    m = _m()
    empty = {
        "available": False,
        "evidence": "shadow",
        "included_in_totals": False,
        "days": days,
        "events": 0,
        "events_with_transcript": 0,
        "events_priced": 0,
        "missing_transcript": 0,
        "no_post_event_turns": 0,
        "carry_tokens": 0,
        "carry_cost_usd": 0.0,
        "threshold_source_counts": {"observed_auto": 0, "fallback": 0},
        "method": "avoided context x real parent API turns to first "
                  "compact_boundary, capped when context growth exceeds the "
                  "avoided size or counterfactual context reaches the session's "
                  "auto-compaction threshold; priced per turn at that turn's exact "
                  "cache-read card",
        "weaknesses": [
            "resume_lean rows name the target session, not the session that "
            "received the lean block; the target transcript is the resumed-"
            "session proxy",
            "the resume trigger is counterfactual, so the carry is shadow "
            "evidence only",
            "turns are deduped on requestId/message.id and stop at the first "
            "compact_boundary; a request split across the event boundary can "
            "be missed",
            "the growth cap uses first-post-event context as the baseline, "
            "so sessions that compact without a compact_boundary line are "
            "truncated only by the caps or EOF",
            "the auto-compaction threshold is inferred from this session's "
            "observed auto compactions or a context-window fallback",
        ],
    }
    try:
        rows = _events(days=days, since=since, db_path=db_path)
    except Exception:
        return empty
    try:
        tier_data = m.PRICING_TIERS.get(
            m._load_pricing_tier(), m.PRICING_TIERS.get("anthropic", {}))
    except Exception:
        tier_data = {}
    details = []
    carry_tokens = 0
    carry_cost = 0.0
    priced = with_transcript = missing = no_turns = 0
    for row in rows:
        try:
            item = _score_event(row, m, tier_data)
        except Exception:
            continue
        details.append(item)
        source = item.get("threshold_source")
        if source in empty["threshold_source_counts"]:
            empty["threshold_source_counts"][source] += 1
        carry_tokens += int(item.get("carry_tokens") or 0)
        carry_cost += float(item.get("carry_cost_usd") or 0.0)
        stopped = item.get("stopped_by")
        if stopped in ("no_transcript", "unreadable", "bad_timestamp"):
            missing += 1
        elif stopped not in ("no_avoided_context",):
            with_transcript += 1
        if item.get("priced"):
            priced += 1
        if int(item.get("turns") or 0) <= 0:
            no_turns += 1
    if include_events:
        empty["event_details"] = details
    empty.update({
        "available": True,
        "events": len(rows),
        "events_with_transcript": with_transcript,
        "events_priced": priced,
        "missing_transcript": missing,
        "no_post_event_turns": no_turns,
        "carry_tokens": int(carry_tokens),
        "carry_cost_usd": round(carry_cost, 4),
    })
    return empty


def main(argv=None):
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--since", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--no-events", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(summary(days=args.days, since=args.since, db_path=args.db,
                             include_events=not args.no_events), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def _score_verified_event(row, m, tier_data, compactions=None):
    """Score a delivered lean block only if the old session stayed stopped.

    The counterfactual old session's compaction point comes from the user's own
    compaction sizes (``compaction_survival``); with too little history the
    fixed 500K rule stays.
    """
    import re
    result = {'eligible': False, 'turns': 0, 'carry_tokens': 0, 'carry_cost_usd': 0.0}
    target = _sid_for(row, m)
    match = re.search(r'(?:^|\s)receiver=([0-9a-fA-F-]{36})(?:\s|$)', row.get('detail') or '')
    ev = _event_dt(m, row.get('timestamp'))
    if not target or not match or not ev or match.group(1) == target:
        return result
    receiver = match.group(1)
    old_path, new_path = _transcript_for(target, m), _transcript_for(receiver, m)
    if not old_path or not new_path:
        return result
    old_seen = False
    old_latest = None
    delivered = False
    try:
        old_stat, new_stat = old_path.stat(), new_path.stat()
        with old_path.open(encoding='utf-8', errors='replace') as f:
            for line in f:
                if '"assistant"' not in line or '"usage"' not in line:
                    continue
                try: obj = json.loads(line)
                except ValueError: continue
                stamp = _parse_ts(obj.get('timestamp'))
                if obj.get('type') != 'assistant' or obj.get('isSidechain') or not (obj.get('message') or {}).get('usage') or not stamp:
                    continue
                if stamp > ev:
                    return result
                old_seen = True
                if old_latest is None or stamp > old_latest[0]:
                    msg = obj['message']
                    old_latest = (stamp, msg.get('model'), _prompt_tokens(m, msg['usage']))
        marker = 'Cold-resume lean (session ' + target[:8]
        with new_path.open(encoding='utf-8', errors='replace') as f:
            for line in f:
                if marker not in line:
                    continue
                try: obj = json.loads(line)
                except ValueError: continue
                stamp = _parse_ts(obj.get('timestamp'))
                attachment = obj.get('attachment') or {}
                if obj.get('type') == 'attachment' and attachment.get('type') == 'hook_additional_context' and stamp and abs((stamp - ev).total_seconds()) <= 180 and re.search(re.escape(marker) + r'(?=[, )])', json.dumps(attachment)):
                    delivered = True
                    break
    except OSError:
        return result
    if not old_seen or not delivered:
        return result
    avoided = int(row.get('tokens_saved') or 0)
    if avoided <= 0:
        return result
    from _measure import compaction_survival
    old_model, old_largest = (old_latest[1], old_latest[2]) if old_latest else (None, 0)
    if compactions is None:
        compactions = compaction_survival.user_history(m, ev)
    survival = compaction_survival.for_event(compactions, ev, avoided)
    if survival.usable:
        # The counterfactual is the old session continuing: its context window
        # is 1M when either session (or the avoided reload itself) passed 200K.
        sizes = [lean_resume_threshold.window_limit(p, lambda usage: _prompt_tokens(m, usage), _resolver(m)) for p in (new_path, old_path)]
        threshold = None if None in sizes else max(sizes + [lean_resume_threshold.window_for(avoided, old_model, _resolver(m))])
        source = 'user_compaction_survival'
    else:
        # Too little history: the old session would have compacted at the
        # user's effective compaction window for its model (env, then
        # modelSettings, then autoCompactWindow, then the model default).
        threshold = lean_resume_threshold.compaction_window(max(avoided, old_largest), old_model, _resolver(m))
        source = 'effective_compact_window'
    if threshold is None:
        return result
    turns, stopped = _walk(new_path, ev.replace(microsecond=0), avoided, m, threshold)
    if not turns and (stopped != 'counterfactual_compaction' or not (_walk(new_path, ev.replace(microsecond=0), 0, m, 2**63)[0] or [])):
        return result
    from _measure import counted_pricing_v5
    key = counted_pricing_v5.resume_rate_key(row.get('detail'))
    old_card = m._counted_exact_card(old_latest[1], tier_data, old_latest[2]) if old_latest and old_latest[1] else None
    if not key or not old_card or not isinstance(old_card.get(key), (int, float)):
        return result
    cards = [m._counted_exact_card(model, tier_data, ctx) if model else None for _dt, model, ctx in turns]
    if any(not card or not isinstance(card.get('cache_read'), (int, float)) for card in cards):
        return result
    try:
        if (old_path.stat().st_mtime_ns, old_path.stat().st_size) != (old_stat.st_mtime_ns, old_stat.st_size) or (new_path.stat().st_mtime_ns, new_path.stat().st_size) != (new_stat.st_mtime_ns, new_stat.st_size):
            return result
    except OSError:
        return result
    weights = [survival.weight(ctx) if survival.usable else 1.0 for _dt, _model, ctx in turns]
    result.update(eligible=True, receiver=receiver, oneshot_usd=round(avoided * old_card[key] / 1e6, 6), turns=len(turns),
                  carry_tokens=int(round(sum(avoided * w for w in weights))),
                  carry_cost_usd=round(sum(avoided * w * card['cache_read'] / 1e6 for w, card in zip(weights, cards)), 6),
                  weighted_turns=round(sum(weights), 4), compactions_above=survival.above,
                  stopped_by=stopped, threshold_source=source)
    return result


def verified_summary(days=30, since=None, db_path=None):
    """Estimated reload and carry only for receiver-linked, stopped sessions."""
    m = _m()
    tier_data = m.PRICING_TIERS.get(m._load_pricing_tier(), m.PRICING_TIERS['anthropic'])
    out = {'events': 0, 'tokens_saved': 0, 'cost_saved_usd': 0.0, 'evidence': 'estimated'}
    try:
        from _measure import lean_resume_receivers
        conn = _connect(db_path, m)
        try: links = lean_resume_receivers.resolved(conn)
        finally: conn.close()
    except Exception:
        links = {}
    from _measure import compaction_survival
    compactions = None
    for row in _events(days=days, since=since, db_path=db_path):
        if compactions is None:
            compactions = compaction_survival.user_history(m, _event_dt(m, row.get('timestamp')))
        if 'receiver=' not in (row.get('detail') or ''):
            receiver = links.get(row.get('id'))
            if not receiver:
                continue
            row = dict(row, detail=(row.get('detail') or '') + ' receiver=' + receiver)
        score = _score_verified_event(row, m, tier_data, compactions)
        if not score['eligible']:
            continue
        out['events'] += 1
        out['tokens_saved'] += max(0, int(row.get('tokens_saved') or 0)) + score['carry_tokens']
        out['cost_saved_usd'] += score['oneshot_usd'] + score['carry_cost_usd']
    out['cost_saved_usd'] = round(out['cost_saved_usd'], 6)
    return out
