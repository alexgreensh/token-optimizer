"""Validate and serialize the recovery state at a transcript boundary."""
import hashlib
import json
import os
from collections import Counter, deque


def _anchor(handle, offset):
    handle.seek(max(0, offset - 4096))
    return hashlib.sha256(handle.read(min(offset, 4096))).hexdigest()


def save(handle, offset, rec, **values):
    stat = os.fstat(handle.fileno())
    position = handle.tell()
    anchor = _anchor(handle, offset)
    handle.seek(position)
    def convert(value):
        if isinstance(value, (deque, set)):
            return list(value)
        raise TypeError(type(value).__name__)
    snapshot = json.loads(json.dumps(rec, default=convert))
    return dict(values, offset=offset, device=stat.st_dev, inode=stat.st_ino,
                anchor=anchor, rec=snapshot)


def restore(handle, checkpoint):
    try:
        stat = os.fstat(handle.fileno())
        offset = int(checkpoint['offset'])
        if (offset < 0 or offset > stat.st_size or stat.st_dev != checkpoint['device']
                or stat.st_ino != checkpoint['inode'] or _anchor(handle, offset) != checkpoint['anchor']):
            handle.seek(0)
            return None
        rec = checkpoint['rec']
        rec['completed'] = deque(rec['completed'], maxlen=25)
        rec['recent_sigs'] = deque(rec['recent_sigs'], maxlen=50)
        rec['versions'] = Counter(rec['versions'])
        rec['hookseen'] = {tuple(item) for item in rec['hookseen']}
        # Calls appear in multiple tracker collections. Re-link them so later
        # tool results and edits update every reference as in a full parse.
        calls = {}
        def link(call):
            if not isinstance(call, dict) or 'id' not in call or 'line' not in call:
                return call
            key = (call['id'], call['line'])
            if key not in calls:
                calls[key] = call
            return calls[key]
        for key, value in rec['pending'].items():
            rec['pending'][key] = link(value)
        rec['completed'] = deque((link(c) for c in rec['completed']), maxlen=25)
        for key, value in rec['latest_sig'].items():
            rec['latest_sig'][key] = link(value)
        opened = rec.get('open')
        if opened:
            for key in ('pre_tail', 'post_calls'):
                opened[key] = [link(c) for c in opened.get(key, [])]
        handle.seek(offset)
        return rec
    except (KeyError, TypeError, ValueError, OSError):
        handle.seek(0)
        return None


def lines(handle, max_line):
    while True:
        offset = handle.tell()
        line = handle.readline(max_line + 1)
        if not line:
            break
        if len(line) > max_line:
            while line and not line.endswith(b'\n'):
                line = handle.readline(65536)
            continue
        yield offset, line.decode('utf-8', errors='replace')


class ParseFailure(dict):
    """Falsey for legacy callers, with a machine-readable failure reason."""
    def __init__(self, reason):
        super().__init__()
        self.reason = reason


def status_telemetry(path, previous=None):
    """Accumulate statusline usage and running agents from appended records."""
    import os
    from datetime import datetime

    state = {'offset': 0, 'total_input': 0, 'cache_read': 0, 'started': None,
             '_pending': {}, '_completed': []}
    try:
        with open(path, 'rb') as handle:
            stat = os.fstat(handle.fileno())
            if isinstance(previous, dict):
                try:
                    offset = int(previous['offset'])
                    valid = (0 <= offset <= stat.st_size and previous['device'] == stat.st_dev
                             and previous['inode'] == stat.st_ino and _anchor(handle, offset) == previous['anchor'])
                    if valid:
                        state = {**previous, '_pending': dict(previous['_pending']), '_completed': list(previous['_completed'])}
                except (KeyError, TypeError, ValueError):
                    pass
            offset = state['offset']
            state['read_from'] = offset
            handle.seek(offset)
            completed = set(state['_completed'])
            while True:
                line_start = handle.tell()
                line = handle.readline(16 * 1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 16 * 1024 * 1024:
                    while line and not line.endswith(b'\n'):
                        line = handle.readline(65536)
                    if line.endswith(b'\n'):
                        continue
                if not line.endswith(b'\n'):
                    handle.seek(line_start)
                    break
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        continue
                    timestamp = row.get('timestamp')
                    if timestamp and state['started'] is None:
                        try:
                            state['started'] = int(datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp())
                        except (ValueError, TypeError):
                            pass
                    message = row.get('message') if isinstance(row.get('message'), dict) else {}
                    usage = message.get('usage') if row.get('type') == 'assistant' else None
                    if isinstance(usage, dict):
                        def n(key):
                            value = usage.get(key, 0)
                            return value if isinstance(value, (int, float)) and value >= 0 else 0
                        state['total_input'] += n('input_tokens') + n('cache_read_input_tokens') + n('cache_creation_input_tokens')
                        state['cache_read'] += n('cache_read_input_tokens')
                    content = message.get('content', [])
                    for block in content if isinstance(content, list) else []:
                        if not isinstance(block, dict):
                            continue
                        if row.get('type') == 'assistant' and block.get('type') == 'tool_use' and block.get('name') in ('Task', 'Agent'):
                            inp = block.get('input') if isinstance(block.get('input'), dict) else {}
                            state['_pending'][block.get('id', '')] = {'model': inp.get('model', ''), 'description': str(inp.get('description') or inp.get('prompt') or '')[:20], 'start_time': timestamp, 'status': 'running'}
                        if block.get('type') == 'tool_result' and block.get('tool_use_id') in state['_pending']:
                            completed.add(block['tool_use_id'])
                except (ValueError, UnicodeError, TypeError, AttributeError):
                    continue
            state['_completed'] = list(completed)
            state['agents'] = [v for k, v in state['_pending'].items() if k not in completed][-5:]
            state['offset'] = handle.tell()
            state.update(device=stat.st_dev, inode=stat.st_ino, anchor=_anchor(handle, state['offset']))
            return state
    except OSError:
        return {**state, 'agents': [], 'read_from': 0}


def restore_codex(handle, checkpoint):
    """Restore Codex state only while the underlying file prefix still matches."""
    try:
        stat = os.fstat(handle.fileno())
        offset = int(checkpoint['offset'])
        if (checkpoint.get('kind') != 'codex' or offset < 0 or offset > stat.st_size
                or stat.st_dev != checkpoint['device'] or stat.st_ino != checkpoint['inode']
                or _anchor(handle, offset) != checkpoint['anchor']):
            handle.seek(0)
            return None
        handle.seek(offset)
        return checkpoint['rec']
    except (KeyError, TypeError, ValueError, OSError):
        handle.seek(0)
        return None
