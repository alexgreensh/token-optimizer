"""Golden comparisons must detect even one changed user-visible byte."""
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('split_golden', REPO / 'scripts/measure_split_golden.py')
golden = importlib.util.module_from_spec(spec)
spec.loader.exec_module(golden)


@pytest.mark.parametrize('fail_snapshot', [False, True])
def test_fixture_and_snapshot_close_sqlite_connections(tmp_path, monkeypatch, fail_snapshot):
    import sqlite3
    connections = []
    connect = sqlite3.connect
    class TrackedConnection(sqlite3.Connection):
        closed = False
        def close(self):
            self.closed = True
            super().close()
        def execute(self, sql, *args):
            if fail_snapshot and sql.startswith('SELECT name FROM sqlite_master'):
                raise sqlite3.OperationalError('snapshot read failed')
            return super().execute(sql, *args)
    def tracked(*args, **kwargs):
        connection = connect(*args, **kwargs, factory=TrackedConnection)
        connections.append(connection)
        return connection
    monkeypatch.setattr(golden.sqlite3, 'connect', tracked)
    home, _ = golden.fixture(tmp_path / 'case', 'legacy')
    output = tmp_path / 'record'
    output.mkdir()
    if fail_snapshot:
        with pytest.raises(sqlite3.OperationalError, match='snapshot read failed'):
            golden.snapshot(home, output)
    else:
        golden.snapshot(home, output)
    assert connections and all(connection.closed for connection in connections)


def test_daemon_authorized_capture_allows_approved_posix_spawn(tmp_path, monkeypatch):
    """glibc can implement the approved Popen through a second audit event."""
    import os
    import uuid
    if not hasattr(os, 'posix_spawn'):
        pytest.skip('POSIX spawn backend')
    site = tmp_path / 'spawn-site'
    site.mkdir()
    (site / 'sitecustomize.py').write_text(
        'import subprocess\n'
        'subprocess._USE_POSIX_SPAWN = True\n'
        'class Spawn(subprocess.Popen):\n'
        '    def __init__(self, *args, **kwargs):\n'
        '        kwargs["close_fds"] = False\n'
        '        super().__init__(*args, **kwargs)\n'
        'subprocess.Popen = Spawn\n', encoding='utf-8')
    environment = golden.environment
    def env(*args):
        return {**environment(*args), 'PYTHONPATH': str(site)}
    monkeypatch.setattr(golden, 'environment', env)
    row = next(row for row in json.loads(golden.CASES.read_text())['cases']
               if row['id'] == 'http-daemon-authorized-seeded-current')
    work = REPO / '.scratch-u0' / ('posix-spawn-test-' + uuid.uuid4().hex)
    golden.capture(REPO, work / 'fixed-path', work / 'capture', [row])
    wire = (work / 'capture' / row['id'] / 'stdout').read_bytes()
    assert wire.startswith(b'HTTP/1.0 200')
    payload = json.loads(wire.split(b'\r\n\r\n', 1)[1])
    assert payload['ok'] is True
    settings = work / 'fixed-path/case/home é with spaces/.claude/settings.json'
    assert 'fixture' in json.loads(settings.read_text())['_disabledMcpServers']


def test_unapproved_posix_spawn_remains_blocked(tmp_path):
    import os
    import uuid
    if not hasattr(os, 'posix_spawn'):
        pytest.skip('POSIX spawn backend')
    source = tmp_path / 'source'
    scripts = source / 'skills/token-optimizer/scripts'
    scripts.mkdir(parents=True)
    (scripts / 'measure.py').write_text(
        'import os, sys\nos.posix_spawn(sys.executable, '
        '[sys.executable, "-c", "print(123)"], dict(os.environ))\n', encoding='utf-8')
    work = REPO / '.scratch-u0' / ('blocked-spawn-test-' + uuid.uuid4().hex)
    row = {'id': 'blocked', 'entry': 'cli', 'argv': [], 'runtime': 'claude',
           'fixture': 'synthetic-current', 'stdin_base64': ''}
    with pytest.raises(RuntimeError, match='golden host boundary: os.posix_spawn'):
        golden.capture(source, work / 'fixed-path', work / 'capture', [row])


@pytest.mark.parametrize('name,before,after', [
    ('json', b'{"field":1}\n', b'{"field":2}\n'),
    ('dollar', b'$1.23456789\n', b'$1.23456788\n'),
    ('statusline', b'cost 1.23', b'cost 1.24'),
    ('hook', b'{"hookSpecificOutput":{}}', b'{"hookSpecificOutput":null}'),
    ('exit', b'0\n', b'1\n'),
])
def test_negative_controls_detect_each_kind_of_drift(tmp_path, name, before, after):
    left, right = tmp_path / 'expected', tmp_path / 'actual'
    left.mkdir()
    right.mkdir()
    (left / name).write_bytes(before)
    (right / name).write_bytes(before)
    assert golden.compare(left, right) == []
    (right / name).write_bytes(after)
    assert golden.compare(left, right) == ['bytes differ: ' + name]


def test_missing_and_extra_artifacts_are_differences(tmp_path):
    left, right = tmp_path / 'expected', tmp_path / 'actual'
    left.mkdir()
    right.mkdir()
    (left / 'missing').touch()
    (right / 'extra').touch()
    assert golden.compare(left, right) == ['missing: missing', 'unexpected: extra']


def test_compiled_fixture_cache_preserves_runpy_and_annotation_semantics(tmp_path):
    import uuid
    source = tmp_path / 'source'
    scripts = source / 'skills/token-optimizer/scripts'
    scripts.mkdir(parents=True)
    (scripts / 'measure.py').write_text('VALUE = 1\ndef f(value: int = 3) -> int:\n    return value\n'
        'assert f.__annotations__["value"] is int\n'
        'if __name__ == "__main__":\n    print(__file__, f.__annotations__, f())\n')
    work = REPO / '.scratch-u0' / ('compiled-cache-test-' + uuid.uuid4().hex)
    rows = [{'id': name, 'entry': name, 'argv': [], 'runtime': 'claude',
             'fixture': 'synthetic-current', 'stdin_base64': ''} for name in ('exports', 'cli')]
    golden.capture(source, work / 'fixed-path', work / 'uncached', rows, compiled_cache=False)
    golden.capture(source, work / 'fixed-path', work / 'cached', rows)
    assert (work / 'uncached/cli/exit').read_text().strip() == '0'
    assert golden.compare(work / 'uncached', work / 'cached') == []


@pytest.mark.parametrize('variant', ['success', 'deadline'])
def test_hook_has_deterministic_watchdog_order(variant):
    import uuid
    row = next(row for row in json.loads(golden.CASES.read_text())['cases']
               if row['id'] == 'hook-hooks_userpromptsubmit_runner.py-UserPromptSubmit-' + variant + '-codex')
    work = REPO / '.scratch-u0' / ('deadline-order-test-' + uuid.uuid4().hex)
    for name in ('first', 'second'):
        golden.capture(REPO, work / 'fixed-path', work / name, [row])
        result = work / name / row['id']
        assert (result / 'exit').read_text().strip() == '0'
        if variant == 'deadline':
            assert (result / 'stdout').read_bytes() == b''
            assert not (result / 'files/data/userpromptsubmit_diagnostics.log').exists()
    assert golden.compare(work / 'first', work / 'second') == []


@pytest.mark.parametrize('argv', [['devin-doctor', '--json'], ['subagent-cache', 'enable', '--json']])
def test_normal_cli_fixture_has_required_markers_and_version(argv):
    import uuid
    work = REPO / '.scratch-u0' / ('normal-cli-test-' + uuid.uuid4().hex)
    row = {'id': 'normal', 'entry': 'cli', 'argv': argv, 'runtime': 'claude',
           'fixture': 'synthetic-current', 'stdin_base64': ''}
    golden.capture(REPO, work / 'fixed-path', work / 'capture', [row])
    assert (work / 'fixed-path/payload/README.md').is_file()
    stderr = (work / 'capture/normal/stderr').read_bytes()
    assert b'Traceback' not in stderr
    assert b'bootstrap manifest gate refused' not in stderr


def test_unexpected_cli_traceback_rejects_a_fixture(tmp_path):
    import uuid
    source = tmp_path / 'source'
    scripts = source / 'skills/token-optimizer/scripts'
    scripts.mkdir(parents=True)
    (scripts / 'measure.py').write_text('raise RuntimeError("invalid fixture")\n')
    work = REPO / '.scratch-u0' / ('bad-cli-test-' + uuid.uuid4().hex)
    row = {'id': 'bad', 'entry': 'cli', 'argv': [], 'runtime': 'claude',
           'fixture': 'synthetic-current', 'stdin_base64': ''}
    with pytest.raises(RuntimeError, match='unexpected traceback'):
        golden.capture(source, work / 'fixed-path', work / 'capture', [row])


def test_every_dispatch_spelling_has_manifest_rows():
    import sys
    sys.path.insert(0, str(REPO / 'scripts'))
    import measure_split_inventory as inventory
    from helpers.measure_split import measure_source
    commands, branches = inventory.dispatch_inventory(measure_source())
    manifest = json.loads(golden.CASES.read_text())
    rows = manifest['cases']
    assert set(commands) == set(manifest['spellings'])
    assert len(commands) >= 146
    for command in commands:
        assert any(row['entry'] == 'cli' and row['argv'] == [command] for row in rows), command
        assert any(row['entry'] == 'cli' and row['argv'] == [command, '--json'] for row in rows), command
    assert any(row['argv'] == [] for row in rows)
    assert any(row['argv'] == ['unknown-split-command'] for row in rows)
    assert {b['expression'] for b in branches} == {b['expression'] for b in manifest['dispatch_branches']}
    requirements = inventory.required_dispatch_cases(measure_source())
    assert {(r['expression'], tuple(r['argv'])) for r in requirements} == {
        (r['expression'], tuple(r['argv'])) for r in manifest['required_dispatch_cases']}
    for requirement in requirements:
        assert any(row['entry'] == 'cli' and row['argv'] == requirement['argv'] for row in rows), requirement
