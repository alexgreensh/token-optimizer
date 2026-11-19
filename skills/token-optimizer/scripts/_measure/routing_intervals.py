"""Append-only history of routing guidance enabled and disabled dates."""
import json
from pathlib import Path

HISTORY = 'routing_intervals.jsonl'


def intervals(directory, injected=None, removed=None):
    path = Path(directory) / HISTORY
    try:
        events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]
    except FileNotFoundError:
        return [(injected, removed)] if injected else []
    result = []
    for event in events:
        if event.get('injected_at') and (not result or result[-1][1]):
            result.append((event['injected_at'], None))
        elif event.get('removed_at') and result and result[-1][1] is None:
            result[-1] = (result[-1][0], event['removed_at'])
    return result


def append(directory, event, injected=None, removed=None):
    path = Path(directory) / HISTORY
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() and injected:
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'injected_at': injected}) + '\n')
            if removed:
                stream.write(json.dumps({'removed_at': removed}) + '\n')
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(event) + '\n')
