"""Prepare an isolated interactive Claude fork for guarded cache refresh."""
import re
from pathlib import Path
from urllib.parse import urlsplit

_ARG = re.compile(r'^[A-Za-z0-9._][A-Za-z0-9._-]{0,127}$')


def fork_command(executable, session_id, model, prompt):
    if (not Path(executable).is_absolute() or not _ARG.fullmatch(str(session_id)) or
            not _ARG.fullmatch(str(model)) or not isinstance(prompt, str) or not prompt):
        raise ValueError('invalid fork arguments')
    return [str(executable), '--resume', str(session_id), '--fork-session',
            '--settings', '{"disableAllHooks":true}', '--model', str(model), prompt]


def fork_environment(source, api_key, local_url):
    parsed = urlsplit(local_url)
    if (not isinstance(api_key, str) or not api_key or parsed.scheme != 'http' or
            parsed.hostname != '127.0.0.1' or parsed.username or parsed.password or
            not parsed.port or parsed.path or parsed.query or parsed.fragment):
        raise ValueError('invalid guarded endpoint or API key')
    allowed = ('PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'CLAUDE_CONFIG_DIR',
               'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME')
    env = {key: str(source[key]) for key in allowed if source.get(key) is not None}
    env.update({'TERM': 'xterm-256color', 'ANTHROPIC_API_KEY': api_key,
                'ANTHROPIC_BASE_URL': local_url, 'CLAUDE_CODE_SKIP_PROMPT_HISTORY': '1',
                'TOKEN_OPTIMIZER_KEEPWARM_PING': '1'})
    return env


def _recorded_parent(record):
    import json
    import os
    import stat
    from .keepwarm_prefix import reconstruct
    path = str(record['transcript_path'])
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_size > 128 * 1024 * 1024 or
                before.st_size != int(record['byte_size']) or before.st_mtime != float(record['transcript_mtime'])):
            raise ValueError('parent transcript changed or is too large')
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            def rows():
                for line in stream:
                    if len(line) > 16 * 1024 * 1024:
                        raise ValueError('parent record is too large')
                    yield json.loads(line)
            result = reconstruct(rows())
            after = os.fstat(stream.fileno())
        current = os.stat(path, follow_symlinks=False)
        identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        if identity(before) != identity(after) or identity(before) != identity(current):
            raise ValueError('parent transcript changed')
        return result
    finally:
        if fd is not None:
            os.close(fd)


def _posix_ptys():
    import os
    return os.name == 'posix'


def run_guarded(record, cwd, executable, api_key, source_env, is_paused, timeout=60, forward=None):
    """Run a one-request fork; no model content reaches the CLI.

    This entry point is intentionally not enabled by the production gate until
    the cache hit and all safety checks are proven against a real API session.
    """
    if not isinstance(api_key, str) or not api_key:
        return {'ok': False, 'reason': 'api-key-unavailable'}
    if not _posix_ptys():
        # Before any POSIX-only import: `pty` needs `termios`, which Windows lacks, so importing it
        # first made this refusal an ImportError instead of a reason.
        return {'ok': False, 'reason': 'platform-unsupported'}
    import os
    import pty
    import select
    import signal
    import subprocess
    import time
    from .keepwarm_guard_server import official_forward, server

    if str(source_env.get('TOKEN_OPTIMIZER_KEEPWARM', '1')).strip().lower() in ('0', 'false', 'no', 'off'):
        return {'ok': False, 'reason': 'kill-switch'}
    base = source_env.get('ANTHROPIC_BASE_URL', '')
    if base and str(base).rstrip('/') != 'https://api.anthropic.com':
        return {'ok': False, 'reason': 'custom-api-endpoint'}
    master = slave = None
    child = None
    try:
        if not is_paused():
            return {'ok': False, 'reason': 'parent-not-paused'}
        parent = _recorded_parent(record)
        command = fork_command(executable, record['session_id'], record['model'], 'Reply with exactly: ok')
        send = forward or official_forward
        def send_if_paused(body, headers):
            if not is_paused():
                raise ValueError('parent is no longer paused')
            return send(body, headers)
        with server(parent, api_key, send_if_paused) as live:
            env = fork_environment(source_env, api_key, live.url)
            master, slave = pty.openpty()
            child = subprocess.Popen(command, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave,
                                     start_new_session=True, close_fds=True)
            os.close(slave)
            slave = None
            deadline = time.monotonic() + max(1, min(float(timeout), 180))
            while time.monotonic() < deadline:
                if live.usage is not None:
                    return {'ok': True, 'reason': 'ok', 'usage': live.usage}
                if child.poll() is not None:
                    break
                ready, _, _ = select.select([master], [], [], min(0.2, max(0, deadline - time.monotonic())))
                if ready:
                    try:
                        if not os.read(master, 65536):
                            break
                    except OSError:
                        break
            if live.usage is not None:
                return {'ok': True, 'reason': 'ok', 'usage': live.usage}
            return {'ok': False, 'reason': 'guard-not-completed', 'exit_code': child.poll()}
    except Exception:
        return {'ok': False, 'reason': 'fork-unavailable'}
    finally:
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        for fd in (slave, master):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
