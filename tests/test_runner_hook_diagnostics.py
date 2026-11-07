"""Hook diagnostics recognize shipped runner and legacy direct command shapes."""
import json
from pathlib import Path
import shutil
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))
import measure


@pytest.fixture
def isolated_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(measure, 'CLAUDE_DIR', tmp_path)
    monkeypatch.setattr(measure, 'SETTINGS_PATH', tmp_path / 'settings.json')
    return tmp_path


def test_shipped_marketplace_hooks_are_installed(isolated_hooks):
    root = Path(__file__).resolve().parents[1]
    cache = isolated_hooks / 'plugins/cache/test/token-optimizer/5.13.37/hooks'
    cache.mkdir(parents=True)
    shutil.copy(root / 'hooks/hooks.json', cache / 'hooks.json')
    assert measure._is_smart_compact_installed({}) == dict.fromkeys(
        ('PreCompact', 'SessionStart', 'Stop', 'SessionEnd'), True)
    assert measure._is_hook_installed({}) is True


def test_legacy_direct_commands_stay_installed(isolated_hooks):
    # SessionEnd counts only our own hook: the direct command setup_hook wrote
    # for this install's measure.py (a bare 'measure.py ...' could be anyone's).
    commands = {'PreCompact': 'measure.py compact-capture',
                'SessionStart': 'measure.py compact-restore',
                'Stop': 'measure.py compact-capture',
                'SessionEnd': "python3 '%s' session-end-flush --trigger end" % measure.MEASURE_PY_PATH}
    settings = {'hooks': {event: [{'hooks': [{'command': command}]}]
                          for event, command in commands.items()}}
    assert all(measure._is_smart_compact_installed(settings).values())
    assert measure._is_hook_installed(settings) is True


@pytest.mark.parametrize('event,command,expected', [
    ('SessionStart', 'python hooks/sessionstart_runner.py', True),
    ('Stop', 'python "C:\\plugin\\hooks\\stop_runner.py"', True),
    ('SessionEnd', 'python /plugin/hooks/stop_runner.py', True),
    ('PreCompact', 'python hooks/stop_runner.py', False),
    ('Stop', 'python hooks/sessionstart_runner.py', False),
    ('SessionStart', 'python hooks/stop_runner.py', False),
    ('SessionEnd', 'python hooks/not_stop_runner.py', False),
    ('SessionEnd', 'python hooks/stop_runner.py.backup', False),
    ('SessionEnd', None, False),
])
def test_runner_detection_is_event_and_filename_scoped(isolated_hooks, event, command, expected):
    settings = {'hooks': {event: [{'hooks': [{'command': command}]}]}}
    assert measure._is_smart_compact_installed(settings)[event] is expected
    if event == 'SessionEnd':
        # The SessionEnd label counts only our own hook: a shipped hooks.json
        # entry resolved for an install. None of these bare runner paths is.
        assert measure._is_hook_installed(settings) is False
