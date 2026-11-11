"""Fail-closed liveness checks for paused Claude sessions."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
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


def interactive_parent(argv, tty, descriptors, ancestors):
    """Require real terminal I/O and reject explicit print or known delegates."""
    if not tty or len(descriptors) != 2:
        return False
    if not all(path.startswith(('/dev/tty', '/dev/pts/')) for path in descriptors):
        return False
    for arg in argv[1:]:
        if arg == '--':
            break
        if arg == '--print' or arg.startswith('--print=') or (arg.startswith('-') and not arg.startswith('--') and 'p' in arg[1:]):
            return False
    return not any('outsourcerer' in command.lower() or 'osrc-' in command.lower() for command in ancestors)


def is_claude_command(command):
    path = Path(str(command))
    return (path.name.lower() == 'claude' or
            (path.parent.name == 'versions' and path.parent.parent.name == 'claude' and
             bool(re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?', path.name))))


def _ps_command(pid):
    proc = subprocess.run(['ps', '-p', str(int(pid)), '-o', 'tty=', '-o', 'args='],
                          capture_output=True, text=True, timeout=1, check=True)
    tty, args = proc.stdout.strip().split(None, 1)
    return tty, shlex.split(args)


def _terminal_descriptors(pid):
    if sys.platform.startswith('linux'):
        return [os.readlink('/proc/%s/fd/%s' % (pid, fd)) for fd in (0, 1)]
    if sys.platform == 'darwin':
        proc = subprocess.run(['/usr/sbin/lsof', '-nP', '-a', '-p', str(int(pid)), '-d', '0,1', '-Ffn'],
                              capture_output=True, text=True, timeout=2, check=True)
        found, descriptor = {}, None
        for line in proc.stdout.splitlines():
            if line.startswith('f'):
                descriptor = line[1:]
            elif line.startswith('n') and descriptor in ('0', '1'):
                found[descriptor] = line[1:]
        return [found.get('0', ''), found.get('1', '')]
    return []


def tmux_parent_allowed(panes, ancestry):
    """Reject identifiable delegate sessions and unknown pane ancestry."""
    names = []
    for line in panes.splitlines():
        try:
            pane_pid, name = line.split('\t', 1)
            if int(pane_pid) in ancestry:
                names.append(name.lower())
        except (ValueError, TypeError):
            continue
    return bool(names) and not any(name.startswith(('outsourcerer-', 'osrc-')) for name in names)


def owner_eligible(pid):
    try:
        tty, argv = _ps_command(pid)
        ancestors, ancestry, has_tmux = [], {pid}, False
        info = process_info(pid)
        for _ in range(16):
            if not info or info['ppid'] <= 1:
                break
            parent = info['ppid']
            _, args = _ps_command(parent)
            ancestors.append(' '.join(args))
            info = process_info(parent)
            if not info:
                return False
            ancestry.add(parent)
            has_tmux = has_tmux or Path(info['command']).name.lower().startswith('tmux')
        if has_tmux:
            binary = shutil.which('tmux') or next((path for path in ('/opt/homebrew/bin/tmux', '/usr/local/bin/tmux', '/usr/bin/tmux') if os.path.isfile(path)), None)
            if not binary:
                return False
            panes = subprocess.run([binary, 'list-panes', '-a', '-F', '#{pane_pid}\t#{session_name}'],
                                   capture_output=True, text=True, timeout=2, check=True).stdout
            if not tmux_parent_allowed(panes, ancestry):
                return False
        return interactive_parent(argv, tty, _terminal_descriptors(pid), ancestors)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def find_owner(pid=None, lookup=process_info, eligibility=owner_eligible):
    pid = os.getppid() if pid is None else pid
    for _ in range(16):
        if pid <= 1:
            break
        info = lookup(pid)
        if not info:
            break
        if is_claude_command(info['command']):
            return {'pid': pid, 'start': info['start']} if eligibility(pid) else None
        pid = info['ppid']
    return None


_NO_OWNER_TTL = 300.0


def _owner_cache_path(directory, session_id):
    name = hashlib.sha256(str(session_id).encode('utf-8')).hexdigest()
    return Path(directory) / ('keepwarm-owner-' + name)


def _pid_exists(pid):
    """Subprocess-free liveness; the tick re-verifies identity with owner_alive."""
    if os.name != 'posix':
        return False
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False
    return True


def session_owner(directory, session_id, now=None):
    """find_owner once per session, not once per Stop hook.

    The owning Claude process is constant for a session, but find_owner walks the
    process table with several `ps`/`lsof` children. A positive answer is reused
    while the pid still exists; a negative one for _NO_OWNER_TTL seconds, so an
    ineligible session does not re-run the walk on every turn. Fail closed: any
    cache problem falls through to a fresh lookup.
    """
    now = time.time() if now is None else now
    path = _owner_cache_path(directory, session_id) if session_id else None
    if path is not None:
        try:
            cached = json.loads(path.read_text(encoding='utf-8'))
            owner = cached.get('owner')
            if isinstance(owner, dict) and owner.get('pid') and owner.get('start'):
                if _pid_exists(owner['pid']):
                    return owner
            elif owner is None and 0 <= now - float(cached.get('ts')) < _NO_OWNER_TTL:
                return None
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    owner = find_owner()
    if path is not None:
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'owner': owner, 'ts': now}, stream)
        except OSError:
            pass
    return owner


def owner_alive(owner, lookup=process_info, eligibility=owner_eligible):
    if not isinstance(owner, dict) or not owner.get('pid') or not owner.get('start'):
        return False
    info = lookup(owner['pid'])
    return bool(info and is_claude_command(info['command']) and info['start'] == owner['start'] and eligibility(owner['pid']))


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
