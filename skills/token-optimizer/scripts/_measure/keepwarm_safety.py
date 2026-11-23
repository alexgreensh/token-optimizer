"""Fail-closed liveness checks for paused Claude sessions."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time


def process_info(pid):
    # Terminal owner checks use ps and lsof, which are unavailable on Windows.
    if os.name != 'posix':
        return None
    try:
        proc = subprocess.run(['ps', '-p', str(int(pid)), '-o', 'ppid=', '-o', 'lstart=', '-o', 'comm='],
                              capture_output=True, text=True, timeout=1, check=True,
                              env={**os.environ, 'LC_ALL': 'C'})
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


def _find_claude_process(pid=None, lookup=None):
    pid = os.getppid() if pid is None else pid
    lookup = process_info if lookup is None else lookup
    for _ in range(16):
        if pid <= 1:
            break
        info = lookup(pid)
        if not info:
            break
        if is_claude_command(info['command']):
            return {'pid': pid, 'start': info['start']}
        pid = info['ppid']
    return None


def find_owner(pid=None, lookup=process_info, eligibility=owner_eligible):
    process = _find_claude_process(pid, lookup)
    return process if process and eligibility(process['pid']) else None


_NO_OWNER_TTL = 300.0


def _owner_cache_path(directory, session_id):
    name = hashlib.sha256(str(session_id).encode('utf-8')).hexdigest()
    return Path(directory) / ('keepwarm-owner-' + name)


def _pid_exists(pid):
    """Check whether a process may still be running."""
    if os.name != 'posix':
        return False
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False
    return True


def session_owner(directory, session_id, now=None, *, parent_pid=None):
    """Reuse terminal eligibility only for the current hook's Claude process."""
    process = _find_claude_process(parent_pid)
    if process is None:
        return None
    now = time.time() if now is None else now
    if parent_pid is not None:
        try:
            started = time.mktime(time.strptime(process['start'], '%a %b %d %H:%M:%S %Y'))
        except (ValueError, OverflowError, OSError):
            return None
        # ps reports whole seconds. A start in the hook's own second is
        # ambiguous if the original parent exited and its PID was reused.
        if started >= int(now):
            return None
    path = _owner_cache_path(directory, session_id) if session_id else None
    if path is not None:
        try:
            cached = json.loads(path.read_text(encoding='utf-8'))
            if cached.get('process') == process:
                if cached.get('eligible') is True:
                    return process
                if cached.get('eligible') is False and 0 <= now - float(cached.get('ts')) < _NO_OWNER_TTL:
                    return None
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    eligible = owner_eligible(process['pid'])
    if path is not None:
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'process': process, 'eligible': eligible, 'ts': now}, stream)
        except OSError:
            pass
    return process if eligible else None


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


def _headless_latch(facade):
    return facade.SNAPSHOT_DIR / 'keepwarm-headless-unproved'


def _headless_preference_disabled(facade, env):
    from . import keepwarm_auto

    if env.get(facade._KEEPWARM_PING_ENV):
        return 'ping-process'
    if facade._keepwarm_llm_ping_disabled(env):
        return 'kill-switch'
    if (str(env.get('TOKEN_OPTIMIZER_KEEPWARM_HEADLESS', 'on')).strip().lower() in keepwarm_auto.OFF
            or facade._read_config_flag('keepwarm_auto', None) is False):
        return 'settings-disabled'
    if facade.keepwarm_consent() == 'declined':
        return 'consent-declined'


def _headless_latch_reason(facade):
    try:
        path = _headless_latch(facade)
        if path.is_symlink():
            return 'headless-proof'
        with path.open(encoding='utf-8') as stream:
            state = stream.read(128).split()
        if state and state[0] == 'pending':
            if len(state) == 3 and state[2].isdecimal() and int(state[2]) > 1 and _pid_exists(state[2]):
                return 'headless-busy'
            if 0 <= facade.time.time() - path.stat().st_mtime < 200:
                return 'headless-busy'
        return 'headless-proof'
    except FileNotFoundError:
        return None
    except OSError:
        return 'accounting-unavailable'
    except UnicodeError:
        return 'headless-proof'


def headless_disabled(facade, env=None):
    """Flags, opt-out and install-wide proof latch, without reading transcripts."""
    env = facade.os.environ if env is None else env
    return _headless_preference_disabled(facade, env) or _headless_latch_reason(facade)


def reset_headless_proof(facade):
    """Clear an install-wide proof stop after the active attempt has finished."""
    from .mod_action_booking import _lock

    try:
        with _lock(facade) as held:
            if not held:
                return 'ledger-locked'
            reason = _headless_latch_reason(facade)
            if reason == 'headless-proof':
                _headless_latch(facade).unlink(missing_ok=True)
                return None
            return reason
    except OSError:
        return 'accounting-unavailable'


def scheduler_gate(facade, env=None):
    reason = headless_disabled(facade, env)
    if reason is None and (facade._remote_lean_profile() or facade.detect_runtime() != 'claude'):
        reason = 'platform-gap'
    if reason is None and facade.keepwarm_tripwire_mode() == 'off':
        reason = 'tripwire-off'
    return reason is None, reason or 'headless-enabled'


def _run_ping(facade, runner, command, cwd, env, context, fallback_cost):
    """Return only usage and bounded machine-readable diagnostics from the child."""
    import math
    from .keepwarm_auto import proof_ok

    row = {'status': 'error', 'reason': 'unproved-cache-read', 'proof': False,
           'cost_usd': fallback_cost}
    try:
        if runner is None:
            def runner(args, folder, environment, timeout):
                return subprocess.run(args, cwd=folder, env=environment, timeout=timeout,
                                      stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                      encoding='utf-8', errors='replace')
        output = runner(command, cwd, env, facade._KEEPWARM_PING_TIMEOUT_SECONDS)
        try:
            data = json.loads(output.stdout)
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        reported = data.get('total_cost_usd')
        if type(reported) in (float, int) and math.isfinite(reported) and reported > 0:
            row['cost_usd'] = reported
        usage = data.get('usage')
        row['proof'] = (output.returncode == 0 and data.get('is_error') is not True
                        and proof_ok(usage, context))
        if row['proof']:
            row.update(status='ok', reason='proved',
                       cache_read=usage['cache_read_input_tokens'],
                       cache_write=usage['cache_creation_input_tokens'])
        elif output.returncode != 0 or data.get('is_error') is True:
            cache_used = isinstance(usage, dict) and any(
                type(usage.get(key)) is int and usage[key] > 0
                for key in ('cache_read_input_tokens', 'cache_creation_input_tokens'))
            error_fields = (data.get('errors'), data.get('result'), data.get('error'), getattr(output, 'stderr', ''))
            if not cache_used and any(re.search(
                    r'\b(?:rate[ _-]limit(?:ed|[ _-]error)?|(?:http|api error:?)\s+429|too many requests)\b',
                    str(field)[:4096], re.IGNORECASE) for field in error_fields):
                row['reason'] = 'rate-limited'
    except subprocess.TimeoutExpired:
        row.update(status='timeout', reason='timeout')
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return row


def _pending_attempt(rows, sid, now):
    pending = {r.get('action_id') for r in rows if r.get('session_id') == sid
               and r.get('action_id') and r.get('status') == 'firing'
               and float(r['ts']) > now - 600}
    finished = {r.get('action_id') for r in rows if r.get('session_id') == sid
                and r.get('status') in ('ok', 'error', 'timeout', 'cancelled', 'unproved')}
    return bool(pending - finished)


def fire_headless(facade, record, *, now, env=None, runner=None, live=None, meters=None):
    """Reserve once under the shared lock, then run one bounded fork and prove it."""
    from .mod_action_booking import _append, _lock

    sid, model = record['session_id'], record['model']
    context = record['prefix_proxy']
    if not facade._keepwarm_valid_arg(sid) or not facade._keepwarm_valid_arg(model):
        return {'fired': False, 'reason': 'bad-record'}
    binary, cwd = facade._keepwarm_resolve_claude_bin(), facade._keepwarm_extract_cwd(record['transcript_path'])
    if not binary or not cwd:
        return {'fired': False, 'reason': 'launch-unavailable'}
    if live is None:
        live = lambda: facade.classify_keepwarm_record(record, now=now) == 'still_paused'
    if meters is None:
        meters = _headless_meters(facade, now)
    if env is None:
        env = facade.os.environ
    latch = _headless_latch(facade)
    with _lock(facade) as held:
        if not held:
            return {'fired': False, 'reason': 'ledger-locked'}
        rows = facade._keepwarm_load_ledger_rows()
        decision = _headless_decision(facade, record, now, env, meters, rows)
        if decision['action'] != 'ping':
            return {'fired': False, 'reason': decision['reason']}
        if _pending_attempt(rows, sid, now) or not live():
            return {'fired': False, 'reason': 'session-changed-or-busy'}
        action_id = 'headless-' + os.urandom(16).hex()
        pause_key = facade._keepwarm_pause_key(sid, record['ts'])
        fallback = facade._get_model_cost(model, 1000, 0, 0, context,
                                          cache_create_1h=context, cache_create_5m=0, per_request=True)
        if fallback <= 0:
            return {'fired': False, 'reason': 'unpriced'}
        common = {'source': 'keepwarm-headless', 'action': 'warm', 'auto': True,
                  'session_id': sid, 'action_id': action_id, 'attempt_key': action_id,
                  'pause_key': pause_key, 'model': model, 'prefix_proxy': context}
        command = facade._keepwarm_build_ping_cmd(sid, model, binary,
                   budget_usd=max(facade._KEEPWARM_PING_BUDGET_USD, 2 * decision['ping_cost_usd']))
        # Ask rules preserve the tool definitions while unattended prompts deny tool calls.
        settings = json.dumps({'disableAllHooks': True, 'permissions': {'ask': ['*']}})
        command.extend(['--max-turns', '1', '--permission-mode', 'default',
                        '--permission-prompts', 'none', '--settings', settings,
                        '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}'])
        try:
            fd = os.open(str(latch), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError:
            return {'fired': False, 'reason': 'headless-proof'}
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(f'pending {action_id} {os.getpid()}')
            _append(facade._keepwarm_ledger_path(), {**common, 'status': 'firing',
                    'ts': now, 'est_cost_usd': fallback})
            reason = _headless_preference_disabled(facade, env) if live() else 'session-changed'
            if reason:
                _append(facade._keepwarm_ledger_path(), {**common, 'status': 'cancelled', 'ts': now})
                latch.unlink()
                return {'fired': False, 'reason': reason}
        except OSError:
            latch.unlink(missing_ok=True)
            return {'fired': False, 'reason': 'accounting-unavailable'}
    child_env = dict(env)
    child_env.pop('CLAUDECODE', None)
    child_env[facade._KEEPWARM_PING_ENV] = '1'
    child_env.update(DISABLE_TELEMETRY='1', DISABLE_ERROR_REPORTING='1')
    child_env['PATH'] = os.path.dirname(binary) + os.pathsep + child_env.get('PATH', os.defpath)
    outcome = _run_ping(facade, runner, command, cwd, child_env, context, fallback)
    recorded = False
    try:
        for _ in range(2):
            with _lock(facade) as held:
                if not held:
                    continue
                _append(facade._keepwarm_ledger_path(), {**common, **outcome, 'ts': max(now, facade.time.time())})
                recorded = True
                if outcome['proof']:
                    _track_proven_ping(facade, record, now)
                break
    finally:
        # The pending marker excludes other headless pings until we settle it.
        # If the ledger stays locked, the reservation still counts at fallback cost.
        if outcome['proof'] or outcome['reason'] in ('timeout', 'rate-limited'):
            latch.unlink(missing_ok=True)
        else:
            fd = os.open(str(latch), os.O_WRONLY | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'w') as stream:
                stream.write('failed ' + action_id)
    if not recorded:
        outcome['reason'] = 'ledger-locked'
    return {'fired': True, **outcome}


def _track_proven_ping(facade, record, now):
    """Append a replacement arm under the shared lock, only after proven usage."""
    from .mod_action_booking import _append

    found = next((rec for rec in reversed(facade.load_keepwarm_records())
                  if rec.get('session_id') == record['session_id']
                  and rec.get('byte_size') == record['byte_size']
                  and rec.get('transcript_mtime') == record['transcript_mtime']), None)
    latest = found or record
    updated = dict(latest, auto_warm=True, pings_fired=[*(latest.get('pings_fired') or []), now])
    _append(facade._keepwarm_sidecar_path(), updated)


def _headless_meters(facade, now):
    data = facade._keepwarm_read_meters(now=now)
    age = data['age_s']
    if age is None or not 0 <= age <= 5 * 60:
        return None, None
    return data['five_hour_pct'], data['seven_day_pct']


def _headless_decision(facade, record, now, env, meters, rows):
    from . import keepwarm_auto as policy

    since_turn = now - record['last_turn_ts']
    last_ping = max((float(r['ts']) for r in rows if r.get('session_id') == record['session_id']
                     and r.get('action') == 'warm' and r.get('status') == 'ok'
                     and r.get('proof') is not False and record['last_turn_ts'] <= float(r['ts']) <= now),
                    default=record['last_turn_ts'])
    since_cache = now - last_ping
    if since_cache < policy.BUCKETS[0]:
        return {'action': 'stop', 'reason': 'not-due', 'due_in_s': policy.BUCKETS[0] - since_cache}
    if since_cache >= policy.CACHE_LIFETIME - 15:
        return {'action': 'stop', 'reason': 'cache-expired'}
    return policy.decide(facade, record['session_id'], record['prefix_proxy'], since_turn, record['model'],
                         policy.CACHE_LIFETIME if record['ttl_kind'] == '1h' else 0,
                         *meters, now, headless=True, env=env, rows=rows)


def scheduler_tick(facade, now, dry_run, env, runner):
    """Process the latest arm per session, with the same policy as the band."""
    allowed, reason = scheduler_gate(facade, env)
    summary = {'allowed': allowed, 'reason': reason, 'decisions': [], 'fired': 0, 'skipped': 0, 'errors': 0}
    with facade._keepwarm_tick_singleton() as held:
        if not held:
            return dict(summary, reason='tick-already-running')
        if not dry_run:
            booked = facade.keepwarm_detect_realized(now=now)
            facade.compact_keepwarm_sidecar(now=now)
        if allowed:
            latest = {record['session_id']: record for record in facade.load_keepwarm_records()
                      if record.get('owner')}
            for sid, record in latest.items():
                try:
                    at = now if dry_run or runner is not None else facade.time.time()
                    if facade.classify_keepwarm_record(record, now=at) != 'still_paused':
                        continue
                    decision = _headless_decision(facade, record, at, env, _headless_meters(facade, at),
                                                  facade._keepwarm_load_ledger_rows())
                    entry = {'session_id': sid, 'action': decision['action'], 'reason': decision['reason']}
                    summary['decisions'].append(entry)
                    if decision['action'] == 'ping' and not dry_run:
                        result = fire_headless(facade, record, now=at, env=env, runner=runner)
                        summary['fired' if result['fired'] else 'skipped'] += 1
                        entry['fire_reason'] = result['reason']
                        if result.get('proof') is False:
                            break
                except Exception:
                    summary['errors'] += 1
        if not dry_run:
            facade._keepwarm_emit_tick_observability(
                now=now, allowed=allowed, reason=reason, armed=len(summary['decisions']),
                fired=summary['fired'], skipped=summary['skipped'],
                booked=booked.get('realized', 0), errors=summary['errors'])
    return summary


def desktop_headless_main(facade, args):
    """Desktop fallback uses a fresh transcript snapshot supplied by the live mod."""
    from .keepwarm_auto import _parse_args
    from .mod_action_booking import _snapshot, _snapshot_still_usable

    try:
        event = json.loads(facade.sys.stdin.read(8193))
        values = _parse_args(args)
        sid = values.session_id
        now = facade.time.time()
        path = Path(event['transcript_path']).resolve(strict=True)
        projects = (facade.CLAUDE_DIR / 'projects').resolve(strict=True)
        if projects not in path.parents:
            raise ValueError('unexpected transcript path')
        snapshot = _snapshot(facade, {**event, 'auto': True}, sid)
        if snapshot['model'] != values.model or not snapshot['last_turn']:
            raise ValueError('stale model or transcript')
        matches = next((rec for rec in reversed(facade.load_keepwarm_records())
                        if rec.get('session_id') == sid and rec.get('byte_size') == snapshot['byte_size']
                        and rec.get('transcript_mtime') == snapshot['mtime']), None)
        record = matches or {'session_id': sid, 'ts': now, 'owner': None,
                             'byte_size': snapshot['byte_size'], 'transcript_mtime': snapshot['mtime'],
                             'transcript_path': snapshot['path'], 'prefix_proxy': snapshot['prefix'],
                             'model': snapshot['model'], 'ttl_kind': snapshot['ttl'],
                             'last_turn_ts': snapshot['last_turn']}

        def live():
            return (_snapshot_still_usable(snapshot) and path.stat().st_size == snapshot['byte_size']
                    and not ended_since(facade.SNAPSHOT_DIR, sid, record['ts']))

        result = fire_headless(facade, record, now=now, live=live,
                               meters=(values.five_hour_pct, values.week_pct))
    except (OSError, ValueError, TypeError, KeyError):
        result = {'fired': False, 'reason': 'snapshot-unavailable'}
    print(json.dumps(result))
    return 0


HOOK_PARENT_PID_ENV = 'TOKEN_OPTIMIZER_HOOK_PARENT_PID'


def stop_flush_args(facade, args, hook_input):
    """Forward the hook payload and opt the existing worker into eligible arms."""
    args = list(args)
    sid, transcript = hook_input.get('session_id'), hook_input.get('transcript_path')
    for flag, value in (('--session-id', sid), ('--transcript-path', transcript)):
        if isinstance(value, str) and value and len(value) <= 4096 and '\x00' not in value:
            args += [flag, value]
    try:
        now = time.time()
        if (os.name != 'posix' or '--no-defer' in args or facade._remote_lean_profile()
                or os.environ.get('TOKEN_OPTIMIZER_RUNTIME') != 'claude'
                or 'agent_id' in hook_input or 'agent_type' in hook_input
                or str(hook_input.get('hook_event_name', 'Stop')).lower() != 'stop'
                or not facade._keepwarm_valid_arg(sid)
                or '--transcript-path' not in args
                or headless_disabled(facade) not in (None, 'headless-busy')):
            return args
        parent_pid = int(os.environ.get(HOOK_PARENT_PID_ENV, '0'))
        path = Path(transcript)
        if parent_pid <= 1 or 'subagents' in path.parts or not stat.S_ISREG(path.stat().st_mode):
            return args
        from .cold_guard import _bounded_last_usage
        from .keepwarm_auto import MIN_CONTEXT

        usage, _, _ = _bounded_last_usage(path)
        if usage and facade._keepwarm_prefix_proxy(usage) >= MIN_CONTEXT:
            args += ['--keepwarm-parent-pid', str(parent_pid), '--keepwarm-stop-at', str(now)]
    except Exception:
        pass
    return args


def flush_hook_args(facade, args):
    """Read a real hook's payload once, record session end, or forward the Stop owner."""
    if facade.os.name != 'posix' or not facade.os.environ.get(HOOK_PARENT_PID_ENV):
        return args
    payload = facade._read_stdin_hook_input()
    if str(payload.get('hook_event_name', '')).lower() == 'sessionend':
        mark_ended(facade.SNAPSHOT_DIR, payload.get('session_id'))
    elif args[1:3] == ['--trigger', 'stop']:
        return stop_flush_args(facade, args, payload)
    return args


def run_flush_worker(facade, args):
    """Arm before the refresh throttle and watch after releasing the flush lock."""
    record = None
    try:
        def value(flag):
            return next(v for k, v in reversed(list(zip(args, args[1:]))) if k == flag)

        if (os.name == 'posix' and '--keepwarm-parent-pid' in args
                and value('--trigger') == 'stop' and not facade._remote_lean_profile()):
            parent_pid = int(value('--keepwarm-parent-pid'))
            if parent_pid > 1:
                record = facade.write_keepwarm_arm_record(
                    value('--session-id'), value('--transcript-path'),
                    now=float(value('--keepwarm-stop-at')), parent_pid=parent_pid,
                )
    except Exception:
        pass
    facade._run_session_end_flush_worker(args)
    if (record and not facade._daemon_snapshot_sandboxed() and record['ttl_kind'] == '1h'
            and not headless_disabled(facade) and facade._is_priced_model(record['model'])):
        try:
            watch_armed_terminal(facade, record['session_id'])
        except OSError:
            pass


def watch_armed_terminal(facade, sid):
    """Reuse the flush process, with one watcher per terminal session."""
    import fcntl
    filename = hashlib.sha256(sid.encode('utf-8')).hexdigest()
    path = facade.SNAPSHOT_DIR / ('keepwarm-owner-watch-' + filename)
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.utime(fd)
    except BaseException:
        os.close(fd)
        raise
    watch_terminal(facade, sid, fd)


def watch_terminal(facade, sid, fd):
    """Use current arm and meter evidence at the deadline, then yield at the caps."""
    from . import keepwarm_auto as policy
    if not facade._keepwarm_valid_arg(sid):
        return 1
    try:
        os.fstat(fd)
        while True:
            now = facade.time.time()
            disabled = headless_disabled(facade)
            if disabled == 'headless-busy':
                facade.time.sleep(5)
                continue
            if disabled:
                return 0
            records = [r for r in facade.load_keepwarm_records() if r.get('session_id') == sid and r.get('owner')]
            if not records:
                return 0
            record = records[-1]
            if not owner_alive(record['owner']) or ended_since(facade.SNAPSHOT_DIR, sid, record['ts']):
                return 0
            if now - record['last_turn_ts'] > policy.CACHE_LIFETIME * policy.MAX_PER_PAUSE:
                return 0
            if now - os.fstat(fd).st_mtime > policy.DAY:
                os.utime(fd)
            state = facade.classify_keepwarm_record(record, now=now)
            if state != 'still_paused':
                # A new turn is writing its transcript and will arm on Stop.
                facade.time.sleep(5)
                continue
            rows = facade._keepwarm_load_ledger_rows()
            decision = _headless_decision(facade, record, now, None, _headless_meters(facade, now), rows)
            if decision['reason'] == 'not-due':
                facade.time.sleep(min(60, max(1, decision['due_in_s'])))
                continue
            if decision['action'] != 'ping':
                return 0
            result = fire_headless(facade, record, now=now)
            if not result.get('proof'):
                return 0
    finally:
        os.close(fd)
