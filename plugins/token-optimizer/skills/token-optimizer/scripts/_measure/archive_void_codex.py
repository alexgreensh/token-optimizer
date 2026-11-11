"""Archive rows on Codex sessions: did the rollout ever show the pointer?

The archive hook logs "replaced <tool> (N chars -> M chars)". On a Codex
session the source of truth for what the model saw is the session rollout
(``$CODEX_HOME/sessions/**/rollout-*-<thread id>.jsonl[.zst]``). A row earns
its saving only when a tool output of about M chars sits next to the event in
that rollout. When the rollout covers the event and no such pointer is there
(the full output stayed, or nothing was replaced), the row is void evidence.
Rollouts that end before the event are still being written: no verdict yet.
"""
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from . import transcript_stream

_ROW = re.compile(r'^replaced (\S+) \(([\d,]+) chars -> ([\d,]+) chars\)')
_UUID = r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'
# A continued thread keeps its id and writes rollout-<ts>-<id>_<child id>.jsonl.
_ID = re.compile(r'^rollout-.*?-(' + _UUID + r')(?:_' + _UUID + r')*\.jsonl(?:\.zst)?$')
_OUTPUT_TYPES = ('function_call_output', 'custom_tool_call_output', 'mcp_tool_call_output')
WINDOW_SECONDS = 20


def _roots():
    try:
        from runtime_env import codex_home
        home = Path(codex_home())
    except Exception:
        return ()
    return (home / 'sessions', home / 'archived_sessions')


def rollout_index(roots=None):
    """{thread id: [rollout paths]} for every rollout file under the Codex roots."""
    found = {}
    for root in (_roots() if roots is None else roots):
        root = Path(root)
        if not root.is_dir():
            continue
        for pattern in ('*/*/*/rollout-*.jsonl*', 'rollout-*.jsonl*'):
            for path in root.glob(pattern):
                match = _ID.match(path.name)
                if not match or path.is_symlink() or not path.is_file():
                    continue
                found.setdefault(match.group(1).lower(), []).append(path)
    for paths in found.values():
        paths.sort(key=lambda p: (p.stat().st_mtime, str(p)))
    return found


def _lines(path):
    """Raw lines of a rollout, decompressing .zst; None when it cannot be read."""
    path = Path(path)
    if path.suffix != '.zst':
        try:
            return transcript_stream.binary_lines(path)
        except OSError:
            return None
    try:
        from compression import zstd  # Python 3.14+
        with zstd.open(path, 'rb') as stream:
            return stream.read().splitlines()
    except ImportError:
        pass
    except (OSError, ValueError, EOFError):
        return None
    try:
        import zstandard
        with path.open('rb') as raw, zstandard.ZstdDecompressor().stream_reader(raw) as stream:
            return stream.read().splitlines()
    except ImportError:
        pass
    except (OSError, ValueError, EOFError):
        return None
    tool = shutil.which('zstd')
    if not tool:
        return None
    try:
        done = subprocess.run([tool, '-dc', '--', str(path)], capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.splitlines() if done.returncode == 0 else None


def _local(stamp):
    return datetime.fromisoformat(str(stamp).replace('Z', '+00:00')).astimezone().replace(tzinfo=None)


def outputs(path):
    """([(local dt, text chars, serialized chars)], last record local dt) or None."""
    lines = _lines(path)
    if lines is None:
        return None
    out, last = [], None
    for line in lines:
        if b'"timestamp"' not in line:
            continue
        try:
            record = json.loads(line)
            stamp = _local(record.get('timestamp'))
        except (ValueError, TypeError, AttributeError, OverflowError):
            continue
        last = stamp if last is None or stamp > last else last
        if b'call_output' not in line:
            continue
        payload = record.get('payload') if isinstance(record.get('payload'), dict) else {}
        if payload.get('type') not in _OUTPUT_TYPES:
            continue
        compact = payload.get('_optimizer_archive_chars')
        if isinstance(compact, list) and len(compact) == 2:
            out.append((stamp, *compact))
            continue
        value = payload.get('output')
        if isinstance(value, str):
            text = full = len(value)
        else:
            items = value if isinstance(value, list) else []
            text = sum(len(item.get('text') or '') for item in items if isinstance(item, dict))
            full = len(json.dumps(value))
        out.append((stamp, text, full))
    return None if Path(path).suffix != '.zst' and transcript_stream.needs_resume(path) else (out, last)


def _shows_pointer(near, pointer_chars):
    return any(abs(text - pointer_chars) <= max(100, pointer_chars * 0.1)
               or abs(full - pointer_chars) <= max(150, pointer_chars * 0.1)
               for _stamp, text, full in near)


def void_ids(paths, rows):
    """Row ids the thread's rollouts contradict; None when unreadable or still being written.

    ``rows`` are (id, local ISO timestamp, detail). A row is void when the
    rollouts run past the event and show no pointer-sized output within
    WINDOW_SECONDS of it.
    """
    found, last = [], None
    for path in ([paths] if isinstance(paths, (str, Path)) else paths):
        parsed = outputs(path)
        if parsed is None:
            return None
        found.extend(parsed[0])
        if parsed[1] is not None and (last is None or parsed[1] > last):
            last = parsed[1]
    void = set()
    for ident, timestamp, detail in rows:
        match = _ROW.match(detail or '')
        try:
            when = datetime.fromisoformat(timestamp)
        except (ValueError, TypeError):
            continue
        if not match:
            continue
        if last is None or last < when + timedelta(seconds=WINDOW_SECONDS):
            return None
        pointer = int(match.group(3).replace(',', ''))
        near = [item for item in found if abs((item[0] - when).total_seconds()) <= WINDOW_SECONDS]
        if not _shows_pointer(near, pointer):
            void.add(ident)
    return void
