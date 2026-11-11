"""Fail-closed liveness checks for paused Claude sessions."""
import hashlib
import os
from pathlib import Path
import subprocess
import time


def process_info(pid):
    try:
        proc = subprocess.run(['ps', '-p', str(int(pid)), '-o', 'ppid=', '-o', 'lstart=', '-o', 'comm='],
                              capture_output=True, text=True, timeout=1, check=True)
        parts = proc.stdout.strip().split(None, 6)
        if len(parts) != 7:
            return None
        return {'ppid': int(parts[0]), 'start': ' '.join(parts[1:6]), 'command': parts[6]}
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def find_owner(pid=None, lookup=process_info):
    pid = os.getppid() if pid is None else pid
    for _ in range(16):
        if pid <= 1:
            break
        info = lookup(pid)
        if not info:
            break
        if Path(info['command']).name.lower() == 'claude':
            return {'pid': pid, 'start': info['start']}
        pid = info['ppid']
    return None


def owner_alive(owner, lookup=process_info):
    if not isinstance(owner, dict) or not owner.get('pid') or not owner.get('start'):
        return False
    info = lookup(owner['pid'])
    return bool(info and Path(info['command']).name.lower() == 'claude' and info['start'] == owner['start'])


def _end_path(directory, session_id):
    name = hashlib.sha256(str(session_id).encode('utf-8')).hexdigest()
    return Path(directory) / ('keepwarm-ended-' + name)


def mark_ended(directory, session_id):
    if not session_id:
        return False
    try:
        Path(directory).mkdir(parents=True, exist_ok=True)
        path = _end_path(directory, session_id)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(str(path), flags, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            if hasattr(os, 'fchmod'):
                os.fchmod(stream.fileno(), 0o600)
            stream.write(str(time.time()))
        return True
    except OSError:
        return False


def ended_since(directory, session_id, armed_at):
    try:
        path = _end_path(directory, session_id)
        if path.is_symlink():
            return True
        return float(path.read_text(encoding='utf-8')) >= float(armed_at)
    except FileNotFoundError:
        return False
    except (OSError, ValueError, TypeError):
        return True


def compacted_since(record):
    """Fail closed if the post-arm transcript is missing, truncated or too large."""
    try:
        path = Path(record['transcript_path'])
        offset = int(record['byte_size'])
        if offset < 0 or path.stat().st_size < offset or path.stat().st_size - offset > 5 * 1024 * 1024:
            return True
        with path.open('rb') as stream:
            stream.seek(offset)
            tail = stream.read()
        return b'compact_boundary' in tail or b'isCompactSummary' in tail
    except (KeyError, OSError, TypeError, ValueError):
        return True


def reconcile_attempts(record, rows):
    """Recover attempt caps from the append-only ledger after a tick crash."""
    session = str(record.get('session_id') or '')
    start = float(record.get('ts') or 0)
    relevant = [row for row in rows if str(row.get('session_id') or '') == session
                and float(row.get('ts') or 0) >= start]
    attempts = [float(row['ts']) for row in relevant if row.get('status') == 'firing']
    outcomes = [row for row in relevant if row.get('status') in ('ok', 'error', 'timeout')]
    changed = False
    if len(attempts) > len(record.get('pings_fired') or []):
        record['pings_fired'] = sorted(attempts)
        changed = True
    prefix = int(record.get('prefix_proxy') or 0)
    unsafe = any(row.get('status') != 'ok' or int(row.get('cache_read') or 0) < prefix * 0.5
                 or int(row.get('cache_write') or 0) >= prefix * 0.5 for row in outcomes)
    if attempts and (len(outcomes) < len(attempts) or unsafe):
        if not record.get('ping_failed'):
            record['ping_failed'] = True
            changed = True
    return changed
