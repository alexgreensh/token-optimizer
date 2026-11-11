"""User-scoped systemd timer for keep-warm ticks."""
import subprocess


def _quote(value, exec_arg=False):
    value = str(value)
    if any(char in value for char in ('\n', '\r', '\x00')):
        raise ValueError('invalid systemd unit value')
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if exec_arg:
        value = value.replace('$', '$$')
    return '"' + value + '"'


def unit_texts(python, script, snapshot, interval):
    interval = int(interval)
    if interval < 1:
        raise ValueError('timer interval must be positive')
    service = ('[Unit]\nDescription=Token Optimizer keep-warm tick\n\n'
               '[Service]\nType=oneshot\nExecStart=' + _quote(python, True) + ' ' +
               _quote(script, True) + ' keepwarm-tick --quiet\nEnvironment=' +
               _quote('TOKEN_OPTIMIZER_SNAPSHOT_DIR=' + str(snapshot)) + '\n')
    timer = ('[Unit]\nDescription=Token Optimizer keep-warm timer\n\n'
             '[Timer]\nOnBootSec={0}s\nOnUnitActiveSec={0}s\n'
             'Unit=token-optimizer-keepwarm.service\n\n'
             '[Install]\nWantedBy=timers.target\n').format(interval)
    return service, timer


def _paths(facade):
    xdg = facade.os.environ.get('XDG_CONFIG_HOME', '').strip()
    base = facade.Path(xdg) if xdg else facade.Path.home() / '.config'
    if not base.is_absolute():
        raise ValueError('XDG_CONFIG_HOME must be absolute')
    user = base / 'systemd' / 'user'
    return (user / 'token-optimizer-keepwarm.service',
            user / 'token-optimizer-keepwarm.timer',
            facade.SNAPSHOT_DIR / 'keepwarm-linux-installed')


def install(facade):
    if facade._scheduler_install_refused():
        return False, facade._SCHEDULER_REFUSAL
    try:
        service, timer, marker = _paths(facade)
        if any(path.is_symlink() for path in (service, timer, marker)):
            return False, 'Refusing a symlinked scheduler file.'
        if (service.exists() or timer.exists()) and not marker.is_file():
            return False, 'A keep-warm systemd unit exists without this installation’s marker.'
        content = unit_texts(facade._keepwarm_python3_path(),
                             facade._keepwarm_measure_script_path(),
                             facade.SNAPSHOT_DIR, facade._KEEPWARM_SCHEDULER_START_INTERVAL)
        service.parent.mkdir(parents=True, exist_ok=True)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('pending\n', encoding='utf-8')
        service.write_text(content[0], encoding='utf-8')
        timer.write_text(content[1], encoding='utf-8')
        for command in (['systemctl', '--user', 'daemon-reload'],
                        ['systemctl', '--user', 'enable', '--now', timer.name]):
            proc = facade.subprocess.run(command, capture_output=True, text=True,
                                         timeout=10, creationflags=facade._NO_WINDOW)
            if proc.returncode:
                return False, 'Could not enable the systemd user timer: ' + (proc.stderr or '').strip()
        marker.write_text('enabled\n', encoding='utf-8')
        return True, 'Keep-warm systemd user timer enabled.'
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
        return False, 'Could not install the systemd user timer: ' + str(exc)


def status(facade, gate):
    service, timer, marker = _paths(facade)
    try:
        owned = marker.is_file() and marker.read_text(encoding='utf-8').strip() == 'enabled'
    except OSError:
        owned = False
    allowed, reason = gate()
    result = {'installed': bool(owned and service.is_file() and timer.is_file()),
              'label': timer.name, 'plist_path': str(timer),
              'last_tick_ok': facade._keepwarm_scheduler_last_tick_ok(),
              'last_tick_age': facade._keepwarm_scheduler_last_tick_age(),
              'gate_allowed': bool(allowed), 'consent': facade.keepwarm_consent(),
              'billing_mode': facade.keepwarm_billing_mode()}
    if not allowed:
        result['reason'] = reason
    return result


def uninstall(facade):
    try:
        _, timer, marker = _paths(facade)
        if not marker.is_file() or marker.is_symlink():
            return True, 'Keep-warm systemd timer is not owned by this installation.'
        proc = facade.subprocess.run(['systemctl', '--user', 'disable', '--now', timer.name],
                                     capture_output=True, text=True, timeout=10,
                                     creationflags=facade._NO_WINDOW)
        if proc.returncode:
            return False, 'Could not disable keep-warm timer: ' + (proc.stderr or '').strip()
        marker.write_text('disabled\n', encoding='utf-8')
        return True, 'Keep-warm systemd timer disabled.'
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return False, 'Could not disable keep-warm timer: ' + str(exc)


def repair(facade, gate):
    allowed, _ = gate()
    if not allowed:
        return 'noop-gate'
    try:
        service, timer, marker = _paths(facade)
        if not marker.is_file() or marker.read_text(encoding='utf-8').strip() != 'enabled':
            return 'noop-no-marker'
        expected = unit_texts(facade._keepwarm_python3_path(), facade._keepwarm_measure_script_path(),
                              facade.SNAPSHOT_DIR, facade._KEEPWARM_SCHEDULER_START_INTERVAL)
        if service.read_text(encoding='utf-8') == expected[0] and timer.read_text(encoding='utf-8') == expected[1]:
            return 'noop-fresh'
    except (OSError, ValueError):
        pass
    with facade._keepwarm_scheduler_install_lock(soft_fail=True) as acquired:
        if not acquired:
            return 'repair-failed'
        return 'repaired' if install(facade)[0] else 'repair-failed'
