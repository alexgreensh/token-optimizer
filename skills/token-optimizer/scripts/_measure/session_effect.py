"""Typical-session effect, coverage and fleet share of the counted savings.

Three questions, all over one dated window and all recomputed from stored rows
on every call (nothing here is a constant):

1. Per-session effect. For each session with counted_reread rows in the window,
   saved / (input + saved) where saved = removed + rereads (the counted ledger)
   and input is the session's own input tokens (session_log, cache included).
   Median, p75, p90, max and the session count. Nearest-rank, lower index.
2. Coverage. Every parent session with traffic in the window is put in exactly
   one class, keyed on evidence, never on a directory name:
     ledger            counted_reread rows in the window;
     logged_no_ledger  savings or compression events, no counted rows;
     ran_unlogged      TO's own "[Token Optimizer" hook injection in the
                       transcript, no rows anywhere (TO ran, ledger empty);
     not_ran           transcript scanned, no evidence, no rows;
     unknown           no rows and the transcript was not scanned (missing or
                       over the scan budget). Never folded into not_ran.
3. Fleet share over the sessions where TO ran (the first three classes):
   tokens  saved / (dated traffic + saved), traffic = fresh input, cache
           writes, cache reads and output on the window's days;
   cost    saved $ / (actual $ + saved $), the limit proxy. Estimated: the
           provider does not publish how it weights tokens inside a limit.

Net of TO's own injections: context TO adds through hook additionalContext
(nudges, warnings, pointers, prior-session hints) is read once and then re-read
on every later call until the next compaction. Both are subtracted for the
"net" figures. Restore payloads (post-compact recovery, cross-session
checkpoint, cold-resume lean) are reported separately and not netted here:
they belong to the estimated restore and resume pools, not the counted ledger.
"""
from __future__ import annotations

import json
import os
import re
import time

from . import _compat, transcript_stream

SCANNER_VERSION = 4
_MARKER = b'[Token Optimizer'
_EVIDENCE_TYPES = ('hook_additional_context', 'hook_system_message')
_RESTORE_PREFIXES = ('[Token Optimizer] Post-compact recovery',
                     '[Token Optimizer] Cross-session checkpoint',
                     '[Token Optimizer] Cold-resume lean')
_REQUEST_RE = re.compile(rb'"requestId"\s*:\s*"([^"]+)"')
_MESSAGE_RE = re.compile(rb'"message"\s*:\s*\{[^{}]*?"id"\s*:\s*"([^"]+)"')
_TS_RE = re.compile(rb'"timestamp"\s*:\s*"(\d{4}-\d{2}-\d{2})')
_ASSISTANT_RE = re.compile(rb'"type"\s*:\s*"assistant"')
DEFAULT_SCAN_BUDGET_S = 20.0
_PARALLEL_EVIDENCE_MIN = 256


def _int(value):
    try:
        return int(float(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _tokens(text):
    try:
        from token_estimate import estimate_tokens_from_bytes
        return int(estimate_tokens_from_bytes(len(text.encode('utf-8', 'replace'))))
    except Exception:
        return (len(text) + 3) // 4


def _percentile(values, num, den):
    """Nearest rank, lower index: sorted[(num * n) // den]."""
    if not values:
        return None
    return values[min(len(values) - 1, (num * len(values)) // den)]


def _injection_text(attachment):
    """TO's own injected text, or None. Only content that starts with TO's
    exact marker counts; a quote of the marker elsewhere never does."""
    if not isinstance(attachment, dict) or attachment.get('type') not in _EVIDENCE_TYPES:
        return None
    content = attachment.get('content')
    parts = content if isinstance(content, list) else [content]
    hits = [p for p in parts if isinstance(p, str) and p.lstrip().startswith('[Token Optimizer')]
    return '\n'.join(hits) if hits else None


def scan_transcript(path):
    """One pass over a transcript for TO evidence and injected-context cost.

    Returns {'markers', 'injections', 'restore_tokens', 'days': {day: [injected,
    reread]}} or None when unreadable. A parent API call is one new requestId
    (or message id) on an assistant line; every such call re-reads the context
    injections made since the last compaction, except the first call after an
    injection, which is its first read (counted as injected, not reread).
    """
    try:
        data = transcript_stream.binary_lines(path)
    except OSError:
        return None
    out = {'markers': 0, 'injections': 0, 'restore_tokens': 0, 'days': {}}
    active = unseen = active_expand = unseen_expand = 0
    seen = set()
    expand_seen = set()
    day = ''
    for line in data:
        m = _TS_RE.search(line)
        if m:
            day = m.group(1).decode()
        if b'"compact_boundary"' in line:
            active = unseen = active_expand = unseen_expand = 0
            continue
        if _MARKER in line and b'"attachment"' in line:
            try:
                text = _injection_text(json.loads(line).get('attachment'))
            except (ValueError, AttributeError):
                text = None
            if text is None:
                continue
            out['markers'] += 1
            obj_type = 'hook_additional_context'
            if b'"hook_additional_context"' not in line:
                obj_type = 'hook_system_message'
            if obj_type != 'hook_additional_context':
                continue  # shown to the user, not sent to the model
            tokens = _tokens(text)
            if text.lstrip().startswith(_RESTORE_PREFIXES):
                out['restore_tokens'] += tokens
                continue
            out['injections'] += 1
            active += tokens
            unseen += tokens
            out['days'].setdefault(day, [0, 0, 0, 0])[0] += tokens
            continue
        if _ASSISTANT_RE.search(line) and active:
            m = _REQUEST_RE.search(line) or _MESSAGE_RE.search(line)
            key = m.group(1) if m else None
            if key is not None and key not in seen:
                seen.add(key)
                reread = active - unseen
                expand_reread = active_expand - unseen_expand
                unseen = unseen_expand = 0
                if reread > 0:
                    out['days'].setdefault(day, [0, 0, 0, 0])[1] += reread
                if expand_reread > 0:
                    out['days'].setdefault(day, [0, 0, 0, 0])[3] += expand_reread
        if b'measure.py' in line and _ASSISTANT_RE.search(line):
            try:
                content = (json.loads(line).get('message') or {}).get('content') or []
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict) or block.get('type') != 'tool_use' or block.get('name') != 'Bash':
                        continue
                    ident = block.get('id')
                    command = (block.get('input') or {}).get('command') or ''
                    if not ident or ident in expand_seen or not re.search(r'''(?:^|[ /])measure\.py["']?\s+(?:--runtime\s+[A-Za-z0-9_-]+\s+)?expand\s+[A-Za-z0-9_-]+(?:\s|$)''', command):
                        continue
                    expand_seen.add(ident)
                    out['markers'] += 1
                    tokens = _tokens(command)
                    out['injections'] += 1
                    active += tokens
                    unseen += tokens
                    active_expand += tokens
                    unseen_expand += tokens
                    out['days'].setdefault(day, [0, 0, 0, 0])[0] += tokens
                    out['days'].setdefault(day, [0, 0, 0, 0])[2] += tokens
            except (ValueError, TypeError, AttributeError):
                pass
            continue
    return out


def _ensure_cache(conn):
    try:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS to_session_evidence ('
            'jsonl_path TEXT PRIMARY KEY, size INTEGER, mtime REAL, '
            'scanner_version INTEGER, markers INTEGER, injections INTEGER, '
            'restore_tokens INTEGER, days_json TEXT, scanned_at TEXT)')
        return True
    except Exception:
        return False


def _evidence(conn, paths, budget_s):
    """{path: scan or None}. Cached per (size, mtime, scanner version); new or
    changed transcripts are scanned until the budget runs out, the rest are
    left out (the caller reports them as unknown, never as not_ran)."""
    cached = {}
    writable = _ensure_cache(conn)
    if writable:
        try:
            for row in conn.execute('SELECT jsonl_path, size, mtime, scanner_version, markers, '
                                    'injections, restore_tokens, days_json FROM to_session_evidence'):
                cached[row[0]] = row[1:]
        except Exception:
            cached = {}
    out, pending = {}, []
    deadline = time.monotonic() + budget_s if budget_s is not None else None
    for path in paths:
        try:
            st = os.stat(path)
        except (OSError, TypeError, ValueError):
            continue
        row = cached.get(path)
        if row and row[0] == st.st_size and row[1] == st.st_mtime and row[2] == SCANNER_VERSION and not transcript_stream.needs_resume(path):
            try:
                out[path] = {'markers': row[3], 'injections': row[4], 'restore_tokens': row[5],
                             'days': json.loads(row[6] or '{}')}
                continue
            except ValueError:
                pass
        pending.append((path, st))
    if len(pending) >= _PARALLEL_EVIDENCE_MIN and getattr(scan_transcript, '__module__', None) == __name__:
        from . import evidence_parallel
        batches = evidence_parallel.batches(pending, deadline, transcript_stream.LARGE_BYTES) if evidence_parallel.safe_entrypoint() else None
    else:
        batches = None
    if batches is None:
        def serial():
            for path, st in pending:
                if deadline is not None and time.monotonic() > deadline:
                    break
                yield [((path, st), scan_transcript(path))]
        batches = serial()
    for batch in batches:
        for (path, st), scan in batch:
            if scan is None:
                continue
            out[path] = scan
            if writable and not transcript_stream.needs_resume(path):
                try:
                    conn.execute('INSERT OR REPLACE INTO to_session_evidence VALUES (?,?,?,?,?,?,?,?,?)',
                                 (path, st.st_size, st.st_mtime, SCANNER_VERSION, scan['markers'],
                                  scan['injections'], scan['restore_tokens'], json.dumps(scan['days']),
                                  time.strftime('%Y-%m-%dT%H:%M:%S')))
                except Exception:
                    writable = False
    if writable:
        try:
            conn.commit()
        except Exception:
            pass
    return out


def _day_traffic(usage, start_day, end_day, price):
    """(tokens, usd) for parent usage on the window's days."""
    tokens = 0
    usd = 0.0
    if not isinstance(usage, dict):
        return 0, 0.0
    for day, models in usage.items():
        if not isinstance(day, str) or not (start_day <= day <= end_day) or not isinstance(models, dict):
            continue
        for bucket in models.values():
            if isinstance(bucket, dict):
                tokens += sum(max(0, _int(bucket.get(k))) for k in
                              ('fresh_input', 'cache_create', 'cache_read', 'output'))
        if price is not None:
            try:
                usd += float(price(models) or 0.0)
            except Exception:
                pass
    return tokens, usd


def _counted_excluded(conn, facade):
    excluded = set()
    try:
        if facade is not None and facade._ensure_counted_views(conn):
            excluded.update(r[0] for r in conn.execute(
                "SELECT 'se:'||id FROM main.savings_events EXCEPT SELECT 'se:'||id FROM counted_savings_events"))
            excluded.update(r[0] for r in conn.execute(
                "SELECT 'ce:'||id FROM main.compression_events EXCEPT SELECT 'ce:'||id FROM counted_compression_events"))
    except Exception:
        pass
    return excluded


def _share(num, den):
    return round(num / den, 6) if den > 0 else None


def compute(conn, start_ts, end_ts, scan_budget_s=DEFAULT_SCAN_BUDGET_S, price=None, facade=None):
    """Session effect, coverage and fleet share for [start_ts, end_ts].

    start_ts/end_ts are local naive ISO strings (the ledger's own clock).
    ``price`` maps one day's {model: bucket} dict to dollars; None leaves the
    cost basis unavailable. Never raises on a missing table: returns
    {'available': False, 'reason': ...}.
    """
    start_day, end_day = start_ts[:10], end_ts[:10]
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'counted_reread', 'session_log'} <= names:
        return {'available': False, 'reason': 'counted ledger or session log missing'}
    excluded = _counted_excluded(conn, facade)
    saved = {}
    rate = [0, 0.0, 0, 0.0]  # oneshot tokens, oneshot $, reread tokens, reread $
    for key, su, tok, one, rr, rru in conn.execute(
            'SELECT event_key, session_uuid, tokens, oneshot_usd, reread_tokens, reread_usd '
            'FROM counted_reread WHERE event_ts >= ? AND event_ts <= ? AND session_uuid IS NOT NULL',
            (start_ts, end_ts)):
        if key in excluded:
            continue
        s = saved.setdefault(su, [0, 0, 0.0])
        s[0] += _int(tok)
        s[1] += _int(rr)
        s[2] += float(one or 0.0) + float(rru or 0.0)
        if _int(tok) > 0:
            rate[0] += _int(tok)
            rate[1] += float(one or 0.0)
        rate[2] += _int(rr)
        rate[3] += float(rru or 0.0)
    logged = set()
    for table in ('savings_events', 'compression_events'):
        if table in names:
            logged.update(r[0] for r in conn.execute(
                'SELECT DISTINCT session_uuid FROM {} WHERE timestamp >= ? AND timestamp <= ? '
                'AND session_uuid IS NOT NULL'.format(table), (start_ts, end_ts)))
    # Where a session ran: cloud when its ledger rows were rebuilt from the
    # transcript (transcript_backfill only writes for sessions whose hooks ran
    # elsewhere and logged nothing here); local otherwise. Evidence, never a
    # directory name. Absent origin column: every session counts as local.
    cloud = set()
    for table in ('savings_events', 'compression_events'):
        if table in names and 'origin' in {r[1] for r in conn.execute('PRAGMA table_info({})'.format(table))}:
            cloud.update(r[0] for r in conn.execute(
                "SELECT DISTINCT session_uuid FROM {} WHERE origin = 'transcript_backfill' "
                'AND session_uuid IS NOT NULL'.format(table)))
    cols = {row[1] for row in conn.execute('PRAGMA table_info(session_log)')}
    parent = 'parent_daily_usage_json' if 'parent_daily_usage_json' in cols else 'NULL'
    daily = 'daily_usage_json' if 'daily_usage_json' in cols else 'NULL'
    side = 'COALESCE(is_sidechain,0)' if 'is_sidechain' in cols else '0'
    sessions = {}
    for su, path, inp, own, merged, is_side in conn.execute(
            'SELECT session_uuid, jsonl_path, input_tokens, {}, {}, {} FROM session_log'.format(parent, daily, side)):
        if not su:
            continue
        cur = sessions.setdefault(su, {'path': None, 'input': 0, 'traffic': 0, 'usd': 0.0, 'side': True})
        cur['input'] = max(cur['input'], _int(inp))
        if is_side:
            continue
        try:
            usage = json.loads(own if own is not None else merged or 'null')
        except (TypeError, ValueError):
            usage = None
        tokens, usd = _day_traffic(usage, start_day, end_day, price)
        cur['traffic'] += tokens
        cur['usd'] += usd
        cur['side'] = False
        cur['path'] = cur['path'] or path
    window = {su: v for su, v in sessions.items()
              if not v['side'] and (v['traffic'] > 0 or su in saved)}
    evidence = _evidence(conn, [v['path'] for v in window.values() if v['path']], scan_budget_s)

    def overhead(path):
        ev = evidence.get(path) or {}
        inj = rr = exp = exp_rr = 0
        for day, pair in (ev.get('days') or {}).items():
            if start_day <= day <= end_day and isinstance(pair, list) and len(pair) >= 2:
                inj += _int(pair[0])
                rr += _int(pair[1])
                if len(pair) >= 4:
                    exp += _int(pair[2])
                    exp_rr += _int(pair[3])
        return inj, rr, exp, exp_rr

    classes = ('ledger', 'logged_no_ledger', 'ran_unlogged', 'not_ran', 'unknown')
    cov = {c: {'sessions': 0, 'traffic_tokens': 0} for c in classes}
    fleet = {'sessions': 0, 'traffic_tokens': 0, 'actual_usd': 0.0, 'saved_tokens': 0,
             'saved_usd': 0.0, 'overhead_tokens': 0, 'overhead_reread_tokens': 0}
    oh = {'injections': 0, 'injected_tokens': 0, 'reread_tokens': 0, 'restore_tokens': 0,
          'sessions_scanned': 0, 'expand_tokens': 0, 'expand_reread_tokens': 0}
    gross, net = [], []
    session_overhead = {}
    for su, v in window.items():
        ev = evidence.get(v['path']) if v['path'] else None
        if su in saved:
            cls = 'ledger'
        elif su in logged:
            cls = 'logged_no_ledger'
        elif ev is None:
            cls = 'unknown'
        elif ev.get('markers'):
            cls = 'ran_unlogged'
        else:
            cls = 'not_ran'
        cov[cls]['sessions'] += 1
        cov[cls]['traffic_tokens'] += v['traffic']
        inj, rr, exp, exp_rr = overhead(v['path']) if ev is not None else (0, 0, 0, 0)
        if ev is not None:
            oh['sessions_scanned'] += 1
            oh['injections'] += _int(ev.get('injections'))
            oh['restore_tokens'] += _int(ev.get('restore_tokens'))
        if cls in ('ledger', 'logged_no_ledger', 'ran_unlogged'):
            s = saved.get(su, [0, 0, 0.0])
            fleet['sessions'] += 1
            fleet['traffic_tokens'] += v['traffic']
            fleet['actual_usd'] += v['usd']
            fleet['saved_tokens'] += s[0] + s[1]
            fleet['saved_usd'] += s[2]
            fleet['overhead_tokens'] += inj
            fleet['overhead_reread_tokens'] += rr
            oh['injected_tokens'] += inj
            oh['reread_tokens'] += rr
            oh['expand_tokens'] += exp
            oh['expand_reread_tokens'] += exp_rr
        session_overhead[su] = inj + rr
    # Per session: every session with counted rows and input, sidechains included
    # (a sub-agent session is a session); overhead nets only where scanned.
    weighted = [0, 0]
    place = {'local': ([], []), 'cloud': ([], [])}
    local_ranked = []
    for su, (tok, rr, _usd) in saved.items():
        inp = sessions.get(su, {}).get('input', 0)
        if inp <= 0:
            continue
        s = tok + rr
        gross.append(s / float(inp + s))
        net.append((s - session_overhead.get(su, 0)) / float(inp + s))
        bucket = place['cloud' if su in cloud else 'local']
        bucket[0].append(gross[-1])
        bucket[1].append(net[-1])
        if su not in cloud:
            local_ranked.append((net[-1], su))
        weighted[0] += s
        weighted[1] += inp + s
    gross.sort()
    net.sort()
    by_place = {}
    for key, (g, n) in place.items():
        g.sort()
        n.sort()
        by_place[key] = {'sessions': len(g), 'median': _percentile(g, 1, 2), 'p75': _percentile(g, 3, 4),
                         'net_median': _percentile(n, 1, 2), 'net_p75': _percentile(n, 3, 4)}
    from . import session_best
    by_place['local']['best'] = session_best.verified_best(conn, facade, local_ranked)
    by_place['basis'] = ("cloud = the session's ledger rows were rebuilt from its transcript "
                         "(origin transcript_backfill: hooks ran elsewhere); local = logged on this machine")
    one_rate = rate[1] / rate[0] if rate[0] > 0 else 0.0
    rr_rate = rate[3] / rate[2] if rate[2] > 0 else 0.0
    oh_usd = oh['injected_tokens'] * one_rate + oh['reread_tokens'] * rr_rate
    fleet_oh_usd = fleet['overhead_tokens'] * one_rate + fleet['overhead_reread_tokens'] * rr_rate
    fleet_oh = fleet['overhead_tokens'] + fleet['overhead_reread_tokens']
    total_traffic = sum(c['traffic_tokens'] for c in cov.values())
    total_sessions = sum(c['sessions'] for c in cov.values())
    for c in cov.values():
        c['traffic_share'] = _share(c['traffic_tokens'], total_traffic)
    ran = ('ledger', 'logged_no_ledger', 'ran_unlogged')
    ledger_traffic = sum(window[su]['traffic'] for su in saved if su in window)
    ledger_saved = sum(saved[su][0] + saved[su][1] for su in saved if su in window)
    have_cost = price is not None and fleet['actual_usd'] > 0
    return {
        'available': True,
        'window': {'start': start_ts, 'end': end_ts},
        'per_session': {
            'sessions': len(gross),
            'median': _percentile(gross, 1, 2), 'p75': _percentile(gross, 3, 4),
            'p90': _percentile(gross, 9, 10), 'max': gross[-1] if gross else None,
            'net_median': _percentile(net, 1, 2), 'net_p75': _percentile(net, 3, 4),
            'net_p90': _percentile(net, 9, 10),
            'net_max': net[-1] if net else None,
            'by_place': by_place,
            'basis': 'per session: (removed + rereads) / (session input incl. cache + removed + rereads), '
                     'counted ledger rows in the window; net subtracts TO injections and their rereads',
            'tier': 'measured',
        },
        'coverage': {
            'sessions': total_sessions, 'traffic_tokens': total_traffic, 'classes': cov,
            'ran_sessions': sum(cov[c]['sessions'] for c in ran),
            'ran_traffic_share': _share(sum(cov[c]['traffic_tokens'] for c in ran), total_traffic),
            'ledger_traffic_share': cov['ledger']['traffic_share'],
            'ran_no_ledger_sessions': cov['logged_no_ledger']['sessions'] + cov['ran_unlogged']['sessions'],
            'unknown_sessions': cov['unknown']['sessions'],
            'evidence': 'ledger rows, logged events, or a "[Token Optimizer" hook injection in the transcript',
        },
        'fleet': {
            'sessions': fleet['sessions'], 'traffic_tokens': fleet['traffic_tokens'],
            'saved_tokens': fleet['saved_tokens'], 'saved_usd': round(fleet['saved_usd'], 4),
            'actual_usd': round(fleet['actual_usd'], 4) if have_cost else None,
            'token_share': _share(fleet['saved_tokens'], fleet['traffic_tokens'] + fleet['saved_tokens']),
            'token_share_net': _share(fleet['saved_tokens'] - fleet_oh,
                                      fleet['traffic_tokens'] + fleet['saved_tokens']),
            'cost_share': _share(fleet['saved_usd'], fleet['actual_usd'] + fleet['saved_usd']) if have_cost else None,
            'cost_share_net': _share(fleet['saved_usd'] - fleet_oh_usd,
                                     fleet['actual_usd'] + fleet['saved_usd']) if have_cost else None,
            'ledger_token_share': _share(ledger_saved, ledger_traffic + ledger_saved),
            'ledger_token_share_session_input': _share(weighted[0], weighted[1]),
            'token_tier': 'measured', 'cost_tier': 'estimated',
            'basis': 'sessions where TO ran; traffic = fresh input, cache writes, cache reads and output '
                     'on the window days; cost = API-equivalent dollars, a proxy for limit weighting',
        },
        'overhead': {
            'injections': oh['injections'], 'injected_tokens': oh['injected_tokens'],
            'reread_tokens': oh['reread_tokens'], 'usd': round(oh_usd, 4),
            'expand_tokens': oh['expand_tokens'], 'expand_reread_tokens': oh['expand_reread_tokens'],
            'expand_usd': round(oh['expand_tokens'] * one_rate + oh['expand_reread_tokens'] * rr_rate, 6),
            'restore_tokens_not_netted': oh['restore_tokens'],
            'sessions_scanned': oh['sessions_scanned'], 'tier': 'estimated',
            'basis': 'hook additionalContext starting with "[Token Optimizer", priced at the counted '
                     "ledger's own average one-shot and reread rates",
        },
    }


def summary(days):
    """Facade entry: the window ending now, priced on the exact model cards."""
    facade = _compat.facade()
    if facade.detect_runtime() != 'claude' or not facade.TRENDS_DB.exists():
        return {'available': False, 'reason': 'counted ledger is Claude Code only'}
    now = facade.datetime.now()
    start = (now - facade.timedelta(days=max(1, int(days or 30)))).isoformat()
    try:
        budget = float(os.environ.get('TOKEN_OPTIMIZER_EFFECT_SCAN_BUDGET_S', DEFAULT_SCAN_BUDGET_S))
    except ValueError:
        budget = DEFAULT_SCAN_BUDGET_S

    def price(models):
        return facade._cost_from_model_breakdown(models)
    conn = facade._init_trends_db()
    try:
        return compute(conn, start, now.isoformat(), scan_budget_s=budget, price=price, facade=facade)
    finally:
        conn.close()


def _pct(x):
    return '{:.1f}%'.format(100 * x) if isinstance(x, (int, float)) else 'n/a'


def cli_lines(effect):
    """Plain CLI lines; empty when unavailable."""
    if not isinstance(effect, dict) or not effect.get('available'):
        return []
    ps, cov = effect['per_session'], effect['coverage']
    lines = []
    # Same order as the dashboard's savings story (assets/savings_story.js):
    # absolute counts first; percentages only after them, each named with its
    # base (that session's own context), never a share of all traffic.
    lines.append('  Token Optimizer ran in {:,} of your {:,} sessions.'.format(cov['ran_sessions'], cov['sessions']))
    if cov['ran_no_ledger_sessions']:
        lines.append('  {:,} sessions ran Token Optimizer but are missing from the ledger; the total understates.'.format(
            cov['ran_no_ledger_sessions']))
    if cov['unknown_sessions']:
        lines.append('  {:,} sessions not scanned yet (transcript missing).'.format(cov['unknown_sessions']))
    places = ps.get('by_place') or {}
    rows = [(label, places.get(key) or {}) for key, label in (('local', 'this computer'), ('cloud', 'cloud'))]
    rows = [(label, v) for label, v in rows if v.get('sessions')]
    if not rows and ps.get('sessions'):
        rows = [('all', ps)]
    if rows:
        lines.append("  Per session, share of that session's own context never sent (net of Token Optimizer's own notes):")
        for label, v in rows:
            lines.append('    {}: typical {}, top quarter {}+ ({:,} sessions)'.format(
                label, _pct(v['net_median']), _pct(v['net_p75']), v['sessions']))
    return lines
