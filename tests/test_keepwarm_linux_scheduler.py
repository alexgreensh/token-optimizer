"""Linux keep-warm timers use a user-scoped systemd unit."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))


def test_units_run_one_quiet_tick_on_each_minute():
    from _measure.keepwarm_linux import unit_texts
    service, timer = unit_texts('/usr/bin/python3', '/home/a/project files/measure.py', '/home/a/to state', 60)
    assert 'ExecStart="/usr/bin/python3" "/home/a/project files/measure.py" keepwarm-tick --quiet' in service
    assert 'TOKEN_OPTIMIZER_SNAPSHOT_DIR=/home/a/to state' in service
    assert 'OnUnitActiveSec=60s' in timer
    assert 'WantedBy=timers.target' in timer


def test_unit_rejects_newlines_in_paths():
    import pytest
    from _measure.keepwarm_linux import unit_texts
    with pytest.raises(ValueError):
        unit_texts('/bin/python', '/bad\nExecStart=/bin/false', '/safe', 60)


def test_install_writes_and_enables_user_timer(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace
    from _measure.keepwarm_linux import install, status, uninstall
    calls = []
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    class Facade:
        os = __import__("os")
        Path = __import__("pathlib").Path
        SNAPSHOT_DIR = tmp_path / 'state'
        _KEEPWARM_SCHEDULER_START_INTERVAL = 60
        _NO_WINDOW = 0
        _scheduler_install_refused = staticmethod(lambda: False)
        _keepwarm_scheduler_last_tick_ok = staticmethod(lambda: False)
        _keepwarm_scheduler_last_tick_age = staticmethod(lambda: None)
        keepwarm_consent = staticmethod(lambda: 'enabled')
        keepwarm_billing_mode = staticmethod(lambda: 'api')
        _keepwarm_python3_path = staticmethod(lambda: '/usr/bin/python3')
        _keepwarm_measure_script_path = staticmethod(lambda: '/opt/to/measure.py')
        subprocess = SimpleNamespace(run=lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0, stderr=''))
    ok, message = install(Facade)
    assert ok, message
    assert (tmp_path / 'config/systemd/user/token-optimizer-keepwarm.timer').is_file()
    assert calls == [['systemctl', '--user', 'daemon-reload'],
                     ['systemctl', '--user', 'enable', '--now', 'token-optimizer-keepwarm.timer']]
    assert status(Facade, lambda: (False, 'live-resume-unverified'))['installed']
    assert uninstall(Facade)[0]
    assert not status(Facade, lambda: (False, 'live-resume-unverified'))['installed']
    assert calls[-1] == ['systemctl', '--user', 'disable', '--now', 'token-optimizer-keepwarm.timer']


def test_install_refuses_unowned_unit(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from _measure.keepwarm_linux import install
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    user = tmp_path / 'systemd/user'
    user.mkdir(parents=True)
    (user / 'token-optimizer-keepwarm.timer').write_text('someone else')
    facade = SimpleNamespace(os=__import__('os'), Path=Path, SNAPSHOT_DIR=tmp_path / 'state',
                             _scheduler_install_refused=lambda: False)
    assert install(facade)[0] is False
    assert (user / 'token-optimizer-keepwarm.timer').read_text() == 'someone else'
