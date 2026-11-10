"""Locate unjoined ledger events without assigning ambiguous transcripts."""
from collections import defaultdict
from pathlib import Path
from . import counted_subagents as sub


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
            if _parent(path).is_file():
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
            for dt, tid, name, command, size in sub.index_results(path, path.parent.name == 'subagents'):
                if name == 'Bash' and command:
                    stored = sub._stored_command(command)
                    if stored in commands.values():
                        hits[stored].append((dt, path, tid))
        candidates = {}
        frequency = defaultdict(int)
        for ev in noid:
            key, ts = ev[:2]
            when = facade._counted_event_utc(ts)
            matches = [(path, tid) for dt, path, tid in hits.get(commands.get(key), [])
                       if when is not None and abs((dt - when).total_seconds()) <= sub.MATCH_SECONDS]
            candidates[key] = matches
            for match in matches:
                frequency[match] += 1
        for ev in noid:
            key = ev[0]
            matches = candidates[key]
            if len(matches) == 1 and frequency[matches[0]] == 1:
                path, _tid = matches[0]
                direct[key] = path
                groups.setdefault(_parent(path).stem, []).append(ev)
            else:
                groups.setdefault(None, []).append(ev)
    return groups, direct


def apply_scope(scope, direct):
    for key, path in direct.items():
        if path == _parent(path):
            scope.location.pop(key, None)
        elif path in scope.files:
            scope.location[key] = scope.files.index(path)
            scope.stats["located_subagent"] += 1
    return scope


def can_relocate(old, key, source, ts, tokens, proven):
    return (proven and old and old.get("event_key") == key and old.get("source") == source
            and not old.get("session_uuid") and old.get("event_ts") == ts
            and int(old.get("tokens") or 0) == tokens and old.get("transcript_mtime") is None
            and int(old.get("turns_counted") or 0) == 0 and int(old.get("reread_tokens") or 0) == 0
            and float(old.get("reread_usd") or 0) == 0)
