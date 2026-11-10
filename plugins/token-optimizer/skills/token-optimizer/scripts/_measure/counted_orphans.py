"""Locate unjoined ledger events without assigning ambiguous transcripts."""
from collections import defaultdict
from pathlib import Path
from . import counted_subagents as sub, fixture_context


def _parent(path):
    path = Path(path)
    return path.parent.parent.with_suffix('.jsonl') if path.parent.name == 'subagents' else path


def _event_details(conn, events):
    details = {}
    for key, uuid, *_ in events:
        if uuid or not (key.startswith('ce:') or key.startswith('se:')):
            continue
        table = 'compression_events' if key.startswith('ce:') else 'savings_events'
        row = conn.execute('SELECT session_id FROM ' + table + ' WHERE id=?', (int(key[3:]),)).fetchone()
        details[key] = row[0] if row else None
    return details


def group_events(facade, conn, events, resolve=True):
    """Return session groups and independently proven event transcript paths.

    Full transcript search is deliberately restricted to an unbounded backfill;
    normal bounded hook collection must not scan the user's entire history.
    """
    groups, direct = {}, {}
    if not resolve:
        for key, uuid, ts, tok, cost, model in events:
            groups.setdefault(uuid, []).append((key, ts, tok, cost, model))
        return groups, direct
    orphan = [event for event in events if not event[1]]
    ids = _event_details(conn, orphan)
    base = facade.CLAUDE_DIR / 'projects'
    agent_index = defaultdict(list)
    if orphan and base.exists():
        for path in base.glob('*/*/subagents/agent-*.jsonl'):
            if _parent(path).is_file() and fixture_context.allowed_path(_parent(path)):
                agent_index[path.stem[6:]].append(path)
    noid = []
    for key, uuid, ts, tok, cost, model in events:
        ev = (key, ts, tok, cost, model)
        if uuid:
            groups.setdefault(uuid, []).append(ev)
            continue
        sid = ids.get(key)
        hits = agent_index.get(sid, []) if sid else []
        if len(hits) == 1:
            path = hits[0]
            direct[key] = path
            groups.setdefault(_parent(path).stem, []).append(ev)
        elif not sid:
            noid.append(ev)
        else:
            groups.setdefault(None, []).append(ev)
    if noid:
        info = sub.event_info(conn, [ev[0] for ev in noid])
        commands = {key: data[1] for key, data in info.items() if key.startswith('ce:') and data[1]}
        hits = defaultdict(list)
        for path in list(base.glob('*/*.jsonl')) + [p for paths in agent_index.values() for p in paths]:
            if not fixture_context.allowed_path(_parent(path)):
                continue
            for dt, tid, name, command, size in sub.index_results(path, path.parent.name == 'subagents'):
                if name == 'Bash' and command:
                    stored = sub._stored_command(command)
                    if stored in commands.values():
                        hits[stored].append((dt, path, tid))
        candidates = {}
        for ev in noid:
            key, ts = ev[:2]
            when = facade._counted_event_utc(ts)
            candidates[key] = [((when - dt).total_seconds(), path, tid) for dt, path, tid in hits.get(commands.get(key), [])
                               if when is not None and LAG_MIN <= (when - dt).total_seconds() <= LAG_MAX]
        assigned = assign(candidates)
        for ev in noid:
            path = assigned.get(ev[0])
            if path is not None:
                direct[ev[0]] = path
                groups.setdefault(_parent(path).stem, []).append(ev)
            else:
                groups.setdefault(None, []).append(ev)
    return groups, direct


# The hook logs a bash event right after the tool result it compressed. On
# located rows the event is 0 to 1 s after the result's (second-truncated)
# timestamp, occasionally up to 1 s before it; the window allows margin.
LAG_MIN, LAG_MAX = -3.0, 2.0
# Results in different transcripts this close in lag are indistinguishable.
TIE_SECONDS = 1.0


def assign(candidates):
    """One event to one tool result. {event key: transcript path}.

    ``candidates`` maps an event to [(lag seconds, path, tool_use_id)]. An
    event is placed only when its closest free results all sit in one
    transcript; placing it takes that result, which can settle other events.
    Events still split across transcripts stay unlocated.
    """
    taken, placed = set(), {}
    pending = {key: sorted(values, key=lambda c: (abs(c[0]), str(c[1]), str(c[2])))
               for key, values in candidates.items() if values}
    changed = True
    while changed and pending:
        changed = False
        # Events with the fewest free results first, so a forced pairing is
        # never taken by an event that had other choices.
        order = sorted(pending, key=lambda k: (sum((c[1], c[2]) not in taken for c in pending[k]), k))
        for key in order:
            free = [c for c in pending[key] if (c[1], c[2]) not in taken]
            if not free:
                del pending[key]
                changed = True
                continue
            best = abs(free[0][0])
            rivals = [c for c in free if abs(c[0]) <= best + TIE_SECONDS]
            if len({c[1] for c in rivals}) != 1:
                continue
            _lag, path, tid = rivals[0]
            taken.add((path, tid))
            placed[key] = path
            del pending[key]
            changed = True
    return placed


def apply_scope(scope, direct, parent):
    scope.unresolved = set()
    files = {path.resolve(): index for index, path in enumerate(scope.files)}
    for key, path in direct.items():
        if _parent(path).resolve() != parent.resolve():
            scope.unresolved.add(key)
        elif path == _parent(path):
            scope.location.pop(key, None)
        elif path.resolve() in files:
            scope.location[key] = files[path.resolve()]
            scope.stats["located_subagent"] += 1
        else:
            scope.unresolved.add(key)
    return scope


def can_relocate(old, key, source, ts, tokens, proven):
    return (proven and old and old.get("event_key") == key and old.get("source") == source
            and not old.get("session_uuid") and old.get("event_ts") == ts
            and int(old.get("tokens") or 0) == tokens and old.get("transcript_mtime") is None
            and int(old.get("turns_counted") or 0) == 0 and int(old.get("reread_tokens") or 0) == 0
            and float(old.get("reread_usd") or 0) == 0)


def window_events(facade, conn, events):
    """Reuse stored transcript identity only when its old prefix still matches."""
    import json
    rows = {r[0]: r[1:] for r in conn.execute(
        'SELECT event_key, session_uuid, event_ts, tokens, pricing_detail FROM counted_reread')}
    out, paths, proofs = [], {}, {}
    for event in events:
        key, uuid, ts, tokens, cost, model = event
        row = rows.get(key)
        if uuid or not row or not row[0] or row[1] != ts or int(row[2] or 0) != int(tokens or 0):
            out.append(event)
            continue
        try:
            detail = json.loads(row[3] or '{}')
            parent = facade._counted_session_path(conn, row[0])
            agent = detail.get('resolved_agent')
            if not parent or not fixture_context.allowed_path(parent) or detail.get('refresh_incomplete') or not detail.get('file_sha256'):
                raise ValueError('no evidence')
            if detail.get("scope") == "subagent" and not agent:
                raise ValueError("missing agent identity")
            if agent:
                if Path(agent).name != agent or not agent.startswith('agent-') or not agent.endswith('.jsonl'):
                    raise ValueError('invalid agent name')
                path = parent.with_suffix('') / 'subagents' / agent
            else:
                path = parent
            size = int(detail['file_size'])
            proof_key = (path, size)
            if proof_key not in proofs:
                proofs[proof_key] = facade._counted_file_proof(path, [size])
            if proofs[proof_key]['prefixes'].get(size) != detail['file_sha256']:
                raise ValueError('transcript changed')
            paths[key] = path
            out.append((key, row[0], ts, tokens, cost, model))
        except (OSError, ValueError, TypeError, KeyError):
            out.append(event)
    return out, paths
