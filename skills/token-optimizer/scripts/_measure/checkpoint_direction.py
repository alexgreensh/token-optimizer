"""Bounded recovery of recent user direction and checkpoint constraints."""
import json
import re

WINDOW_BYTES = 8 * 1024 * 1024
BLOCK_BYTES = 64 * 1024
_RULE = re.compile(r"(?:\b(?:never|always|must|don['’]t|do not|only|cap)\b|^no\s+(?!no\b)\w+)", re.I)
_PING = re.compile(r"^(?:ok(?:ay)?|yes|yep|no|nope|thanks|thank you|continue|proceed|go on|remove|status(?:\s+(?:plz|please))?|/\S+)[.!?\s]*$", re.I)


def _reverse_lines(path):
    """Yield complete JSONL records from the last bounded window, newest first."""
    with open(path, 'rb') as stream:
        end = stream.seek(0, 2)
        floor = max(0, end - WINDOW_BYTES)
        pending = b''
        while end > floor:
            start = max(floor, end - BLOCK_BYTES)
            stream.seek(start)
            data = stream.read(end - start) + pending
            pieces = data.split(b'\n')
            pending = pieces[0]
            for line in reversed(pieces[1:]):
                if line:
                    yield line
            end = start
        if floor == 0 and pending:
            yield pending


def merge_constraints(recent, cached):
    """Preserve older cached rules, with recent duplicates taking priority."""
    result = []
    seen = set()
    for item in reversed(list(cached or []) + list(recent or [])):
        if not isinstance(item, dict) or not isinstance(item.get('text'), str):
            continue
        key = ' '.join(item['text'].lower().split())
        if key and key not in seen:
            seen.add(key)
            result.append(item)
            if len(result) == 8:
                break
    return list(reversed(result))


def scan(path, user_text, redact, checkpoint_state, max_line):
    recent, constraints, seen = [], [], set()
    try:
        for line in _reverse_lines(path):
            if len(line) > max_line or b'"user"' not in line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(record, dict) or record.get('type') != 'user' or record.get('isSidechain'):
                continue
            raw = user_text(record)
            if not raw.strip():
                continue
            text = redact(raw).strip()
            ident = record.get('uuid')
            if len(recent) < 3 and not _PING.fullmatch(text) and len(text.split()) >= 2:
                recent.append({'text': text[:400], 'source': {'kind': 'user_message', 'id': ident}})
            for sentence in reversed(checkpoint_state._sentences(text)):
                cue = _RULE.search(sentence)
                if not cue or sentence.rstrip().endswith('?'):
                    continue
                start = max(0, cue.start() - 60) if cue.end() > 300 else 0
                excerpt = checkpoint_state._clip(('… ' if start else '') + sentence[start:], 300)
                key = checkpoint_state._norm(excerpt)
                if key not in seen and len(constraints) < 8:
                    seen.add(key)
                    constraints.append({'text': excerpt, 'source': {'kind': 'user_message', 'id': ident}})
            if len(recent) == 3 and len(constraints) == 8:
                break
    except OSError:
        pass
    return None, list(reversed(recent)), merge_constraints(list(reversed(constraints)), [])


def previous_state(directory, session_id):
    """Read the latest bounded sidecar belonging to this session only."""
    if not session_id or session_id == 'unknown':
        return {}
    try:
        paths = sorted(directory.glob(session_id + '-*.json'), key=lambda p: p.stat().st_mtime_ns, reverse=True)
        for path in paths:
            if path.stat().st_size > 512 * 1024:
                continue
            data = json.loads(path.read_text(encoding='utf-8'))
            if data.get('session_id') == session_id and isinstance(data.get('task_state'), dict):
                return data['task_state']
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {}
