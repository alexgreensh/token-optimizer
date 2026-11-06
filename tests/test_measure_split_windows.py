"""Windows behavior of the golden harness, provable from any host.

CI run 42124039503 (8aa1464d) failed the Windows golden and pytest legs on
four harness gaps: text-mode stdout turned the seed marker into CRLF, a
dashboard case reached the real ``os.startfile`` (Edge opened inside the
fixture home), comparator names used backslashes so the two verified Devin
artifacts were not recognized, and BASE's own SIGPIPE crash in
``devin_compress`` aborted capture. Each test below reproduces one of these
without Windows.
"""
import importlib.util
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
spec = importlib.util.spec_from_file_location('measure_split_golden', REPO / 'scripts/measure_split_golden.py')
golden = importlib.util.module_from_spec(spec)
spec.loader.exec_module(golden)
import measure_split_crashes as crashes  # noqa: E402

SIGPIPE_STDERR = (
    b'Traceback (most recent call last):\r\n'
    b'  File "D:\\a\\x\\scripts\\measure_split_probe.py", line 374, in <module>\r\n'
    b"    runpy.run_path(str(path), run_name='__main__')\r\n"
    b'  File "D:\\a\\x\\payload\\skills\\token-optimizer\\scripts\\measure.py", line %d, in <module>\r\n'
    b'    sys.exit(devin_compress.main(args[1:]))\r\n'
    b'  File "D:\\a\\x\\payload\\skills\\token-optimizer\\scripts\\devin_compress.py", line 707, in main\r\n'
    b'    signal.signal(signal.SIGPIPE, signal.SIG_IGN)\r\n'
    b'                  ^^^^^^^^^^^^^^\r\n'
    b"AttributeError: module 'signal' has no attribute 'SIGPIPE'. Did you mean: 'SIGFPE'?\r\n")


O_NONBLOCK_STDERR = (
    b'Traceback (most recent call last):\r\n'
    b'  File "D:\\a\\x\\payload\\skills\\token-optimizer\\scripts\\measure.py", line %d, in <module>\r\n'
    b'    sys.exit(devin_doctor.main(args[1:]))\r\n'
    b'  File "D:\\a\\x\\payload\\skills\\token-optimizer\\scripts\\devin_doctor.py", line 107, in local_agent_surfaces\r\n'
    b'    fd = os.open(product, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)\r\n'
    b'                                        ^^^^^^^^^^^^^\r\n'
    b"AttributeError: module 'os' has no attribute 'O_NONBLOCK'\r\n")


def test_devin_doctor_crash_is_listed_only_for_its_cases_and_platform():
    stderr = O_NONBLOCK_STDERR % 64088
    assert crashes.known_crash('cli-devin-doctor---json', stderr, platform='nt')['name'] == 'devin-doctor-o-nonblock-windows'
    assert crashes.known_crash('cli-devin-doctor---json', stderr, platform='posix') is None
    assert crashes.known_crash('cli-devin-filter---quiet', stderr, platform='nt') is None
    assert crashes.known_crash('cli-devin-doctor', stderr.replace(b'O_NONBLOCK\'', b'O_BINARY\''), platform='nt') is None
    assert [entry['name'] for entry in crashes.KNOWN_BASE_CRASHES] == ['devin-sigpipe-windows', 'devin-doctor-o-nonblock-windows']

def test_seed_marker_survives_text_mode_newline_translation(monkeypatch):
    """Windows stdout writes '\\r\\n' for '\\n'; the seed handshake must not care."""
    site = REPO / '.scratch-u0' / ('crlf-site-' + uuid.uuid4().hex)
    site.mkdir(parents=True)
    (site / 'sitecustomize.py').write_text(
        'import io, sys\n'
        'sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\\r\\n", line_buffering=True)\n',
        encoding='utf-8')
    environment = golden.environment
    monkeypatch.setattr(golden, 'environment', lambda *args: {**environment(*args), 'PYTHONPATH': str(site)})
    row = next(row for row in json.loads(golden.CASES.read_text())['cases']
               if row['id'] == 'api-_get_savings_summary-seeded-current')
    work = REPO / '.scratch-u0' / ('crlf-seed-test-' + uuid.uuid4().hex)
    assert golden.capture(REPO, work / 'fixed-path', work / 'capture', [row]) == 1
    assert (work / 'capture' / row['id'] / 'exit').read_text().strip() == '0'


def test_startfile_is_a_recorded_fake_never_the_real_launcher(tmp_path):
    """Windows opens the dashboard with os.startfile, not a subprocess."""
    work = REPO / '.scratch-u0' / ('startfile-test-' + uuid.uuid4().hex)
    (work / 'case').mkdir(parents=True)
    home, project = golden.fixture(work / 'case', 'synthetic-current')
    env = golden.environment(home, tmp_path / 'payload', work / 'case')
    marker = work / 'case' / 'REAL-STARTFILE'
    site = work / 'site'
    site.mkdir()
    (site / 'sitecustomize.py').write_text(
        'import os\n'
        'def startfile(target, *args, **kwargs):\n'
        f'    open({str(marker)!r}, "w").write(str(target))\n'
        'os.startfile = startfile\n', encoding='utf-8')
    script = work / 'case' / 'open_dashboard.py'
    script.write_text('import os\nos.startfile("http://localhost:24842/token-optimizer")\n', encoding='utf-8')
    effects = work / 'case' / 'effects.json'
    proc = subprocess.run([sys.executable, str(REPO / 'scripts/measure_split_probe.py'), str(tmp_path / 'payload'),
                           str(effects), 'script', str(script)],
                          cwd=project, env={**env, 'PYTHONPATH': str(site)}, capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert not marker.exists(), 'the real os.startfile ran'
    assert ['startfile', ['http://localhost:24842/token-optimizer'], {}] in json.loads(effects.read_text())


def test_comparator_names_are_portable(tmp_path):
    for side in ('a', 'b'):
        path = tmp_path / side / 'cli-case' / 'files' / 'nested' / 'value'
        path.parent.mkdir(parents=True)
        path.write_bytes(side.encode())
    assert golden.compare(tmp_path / 'a', tmp_path / 'b') == ['bytes differ: cli-case/files/nested/value']


def test_known_base_crash_matches_only_the_reviewed_signature():
    stderr = SIGPIPE_STDERR % 64099
    assert crashes.known_crash('cli-devin-filter---quiet', stderr, platform='nt')['name'] == 'devin-sigpipe-windows'
    assert crashes.known_crash('cli-devin-filter---quiet', stderr, platform='posix') is None
    assert crashes.known_crash('cli-dashboard', stderr, platform='nt') is None
    other_final = stderr.replace(b"no attribute 'SIGPIPE'", b"no attribute 'SIGHUP'")
    assert crashes.known_crash('cli-devin-filter---quiet', other_final, platform='nt') is None
    other_frame = stderr.replace(b'signal.signal(signal.SIGPIPE, signal.SIG_IGN)', b'signal.alarm(1)')
    assert crashes.known_crash('cli-devin-filter---quiet', other_frame, platform='nt') is None


def _case(root, name, stderr, stdout=b'', exit_code=b'1\n'):
    case = root / name
    case.mkdir(parents=True)
    (case / 'stderr').write_bytes(stderr)
    (case / 'stdout').write_bytes(stdout)
    (case / 'exit').write_bytes(exit_code)


def test_known_crash_normalizes_measure_line_numbers(tmp_path):
    name = 'cli-devin-filter---quiet'
    _case(tmp_path / 'base', name, SIGPIPE_STDERR % 64099)
    _case(tmp_path / 'candidate', name, SIGPIPE_STDERR % 63813)
    raw = golden.compare(tmp_path / 'base', tmp_path / 'candidate')
    assert raw == ['bytes differ: ' + name + '/stderr']
    remaining, normalized = crashes.normalize_known_crashes(raw, tmp_path / 'base', tmp_path / 'candidate', platform='nt')
    assert remaining == [] and normalized == [name + '/stderr: devin-sigpipe-windows']
    assert crashes.normalize_known_crashes(raw, tmp_path / 'base', tmp_path / 'candidate', platform='posix') == (raw, [])


@pytest.mark.parametrize('change', ['stdout', 'exit', 'frame'])
def test_known_crash_never_hides_other_differences(tmp_path, change):
    name = 'cli-devin-filter---quiet'
    _case(tmp_path / 'base', name, SIGPIPE_STDERR % 64099)
    stderr = SIGPIPE_STDERR % 63813
    if change == 'frame':
        stderr = stderr.replace(b'signal.signal(signal.SIGPIPE, signal.SIG_IGN)', b'signal.signal(signal.SIGTERM, signal.SIG_IGN)')
    _case(tmp_path / 'candidate', name, stderr, stdout=b'x' if change == 'stdout' else b'',
          exit_code=b'2\n' if change == 'exit' else b'1\n')
    raw = golden.compare(tmp_path / 'base', tmp_path / 'candidate')
    remaining, normalized = crashes.normalize_known_crashes(raw, tmp_path / 'base', tmp_path / 'candidate', platform='nt')
    if change == 'frame':
        assert remaining == raw and normalized == []
    else:
        assert remaining == ['bytes differ: ' + name + '/' + change] and normalized == [name + '/stderr: devin-sigpipe-windows']


@pytest.mark.parametrize('platform', ['nt', 'posix'])
def test_capture_accepts_the_listed_crash_only_on_its_platform(platform, monkeypatch):
    source = REPO / '.scratch-u0' / ('crash-source-' + uuid.uuid4().hex)
    scripts = source / 'skills/token-optimizer/scripts'
    scripts.mkdir(parents=True)
    (scripts / 'devin_compress.py').write_text(
        'import types\n'
        'signal = types.ModuleType("signal")\n'
        'signal.signal = lambda *args: None\n'
        'signal.SIG_IGN = 1\n'
        'def main(argv=None):\n'
        '    signal.signal(signal.SIGPIPE, signal.SIG_IGN)\n', encoding='utf-8')
    (scripts / 'measure.py').write_text('import sys\nimport devin_compress\nsys.exit(devin_compress.main([]))\n',
                                        encoding='utf-8')
    monkeypatch.setattr(crashes, 'PLATFORM', platform)
    row = {'id': 'cli-devin-filter-synthetic', 'entry': 'cli', 'argv': [], 'runtime': 'claude',
           'fixture': 'synthetic-current', 'stdin_base64': ''}
    work = REPO / '.scratch-u0' / ('crash-capture-' + uuid.uuid4().hex)
    if platform == 'nt':
        assert golden.capture(source, work / 'fixed-path', work / 'capture', [row]) == 1
        assert b"no attribute 'SIGPIPE'" in (work / 'capture' / row['id'] / 'stderr').read_bytes()
    else:
        with pytest.raises(RuntimeError, match='unexpected traceback'):
            golden.capture(source, work / 'fixed-path', work / 'capture', [row])


@pytest.mark.parametrize('audit', ['posix', 'windows'])
def test_internal_hook_spawn_is_approved_for_a_windows_command_line(tmp_path, monkeypatch, audit):
    """Windows audits subprocess.Popen with the list2cmdline STRING; the approved
    run.py -> module_runner re-entry must still run (CI: every Windows hook empty)."""
    import shutil
    source = tmp_path / 'source'
    scripts = source / 'skills/token-optimizer/scripts'
    shutil.copytree(REPO / 'skills/token-optimizer/scripts', scripts,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copytree(REPO / 'hooks', source / 'hooks', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    facade, marker = scripts / 'measure.py', 'if __name__ == "__main__":'
    facade.write_text(facade.read_text(encoding='utf-8').replace(
        marker, 'def _continuity_prompt_hint(**kwargs):\n    return "fixture continuity hint"\n\n' + marker),
        encoding='utf-8')
    site = REPO / '.scratch-u0' / ('cmdline-site-' + uuid.uuid4().hex)
    site.mkdir(parents=True)
    (site / 'sitecustomize.py').write_text(
        'import subprocess, sys\n'
        '_child = subprocess.Popen._execute_child\n'
        'def _execute_child(self, args, executable, *rest):\n'
        '    if not isinstance(args, str):\n'
        '        sys.audit("subprocess.Popen", executable, subprocess.list2cmdline(args), None, None)\n'
        '    return _child(self, args, executable, *rest)\n'
        'subprocess.Popen._execute_child = _execute_child\n', encoding='utf-8')
    if audit == 'windows':
        environment = golden.environment
        monkeypatch.setattr(golden, 'environment', lambda *args: {**environment(*args), 'PYTHONPATH': str(site)})
    row = next(row for row in json.loads(golden.CASES.read_text())['cases']
               if row['id'] == 'hook-hooks_userpromptsubmit_runner.py-UserPromptSubmit-success-claude')
    work = REPO / '.scratch-u0' / ('cmdline-hook-test-' + uuid.uuid4().hex)
    golden.capture(source, work / 'fixed-path', work / 'capture', [row])
    stdout = (work / 'capture' / row['id'] / 'stdout').read_bytes()
    assert b'fixture continuity hint' in stdout and b'hookSpecificOutput' in stdout


@pytest.mark.parametrize('audit', ['posix', 'windows'])
def test_precompact_checkpoint_persists_under_windows_command_line_audit(monkeypatch, audit):
    """Lane V shakedown: on Windows BASE capture raised 'PreCompact returned without
    persisting its checkpoint'. Same cause as the empty hooks: the approved
    run.py -> module_runner re-entry was blocked when audited as a string."""
    site = REPO / '.scratch-u0' / ('cmdline-site-' + uuid.uuid4().hex)
    site.mkdir(parents=True)
    (site / 'sitecustomize.py').write_text(
        'import subprocess, sys\n'
        '_child = subprocess.Popen._execute_child\n'
        'def _execute_child(self, args, executable, *rest):\n'
        '    if not isinstance(args, str):\n'
        '        sys.audit("subprocess.Popen", executable, subprocess.list2cmdline(args), None, None)\n'
        '    return _child(self, args, executable, *rest)\n'
        'subprocess.Popen._execute_child = _execute_child\n', encoding='utf-8')
    if audit == 'windows':
        environment = golden.environment
        monkeypatch.setattr(golden, 'environment', lambda *args: {**environment(*args), 'PYTHONPATH': str(site)})
    case = 'hook-skills_token-optimizer_scripts_measure.py-compact-capture---trigger-auto---quiet-PreCompact-success-claude'
    row = next(row for row in json.loads(golden.CASES.read_text())['cases'] if row['id'] == case)
    work = REPO / '.scratch-u0' / ('precompact-test-' + uuid.uuid4().hex)
    # capture() itself raises 'PreCompact returned without persisting its checkpoint'.
    assert golden.capture(REPO, work / 'fixed-path', work / 'capture', [row]) == 1
    files = work / 'capture' / case / 'files'
    assert any('checkpoints' in path.parts for path in files.rglob('*.md'))


def test_blocked_socket_records_never_carry_the_descriptor(tmp_path):
    """CI 42138301440: BASE doctor --json effects differed from BASE on Windows because the
    blocked socket.connect record printed the socket repr, whose fd is a fresh kernel handle
    each run. Shift the descriptor between two runs; the record must not change."""
    records = []
    for extra in (0, 3):
        work = REPO / '.scratch-u0' / ('socket-test-' + uuid.uuid4().hex)
        (work / 'case').mkdir(parents=True)
        home, project = golden.fixture(work / 'case', 'synthetic-current')
        env = golden.environment(home, tmp_path / 'payload', work / 'case')
        script = work / 'case' / 'connect.py'
        script.write_text(
            'import socket\n'
            f'held = [socket.socket() for _ in range({extra})]\n'
            'probe = socket.socket()\n'
            'try:\n'
            '    probe.connect(("127.0.0.1", 24848))\n'
            'except PermissionError:\n'
            '    pass\n', encoding='utf-8')
        effects = work / 'case' / 'effects.json'
        proc = subprocess.run([sys.executable, str(REPO / 'scripts/measure_split_probe.py'), str(tmp_path / 'payload'),
                               str(effects), 'script', str(script)],
                              cwd=project, env=env, capture_output=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        records.append([row for row in json.loads(effects.read_text()) if row[:2] == ['blocked', 'socket.connect']])
    assert records[0] and records[0] == records[1], records
    assert 'fd=' not in json.dumps(records[0])


_PREFIX_DIAGNOSTIC = b'diagnostic: File "D:\\payload\\measure.py", line %d\r\n'


@pytest.mark.parametrize('variant', ['diagnostic', 'message'])
def test_known_crash_normalizes_only_traceback_frame_headers(tmp_path, variant):
    """Astra U0-final2 #1: frame-shaped text outside the traceback frames (a diagnostic line
    before the block, the final exception line) must stay raw. Every frame is identical."""
    name = 'cli-devin-filter---quiet'
    frames = SIGPIPE_STDERR % 64099

    def side(number):
        if variant == 'diagnostic':
            return _PREFIX_DIAGNOSTIC % number + frames
        if variant == 'source':
            return frames.replace(b'    sys.exit(devin_compress.main(args[1:]))\r\n',
                                  b'    sys.exit(devin_compress.main(args[1:]))  # File "D:\\x\\measure.py", line %d\r\n' % number)
        return frames.replace(b"Did you mean: 'SIGFPE'?", b'File "D:\\x\\measure.py", line %d' % number)

    before, after = side(100), side(101)
    assert crashes.known_crash(name, before, platform='nt') is crashes.known_crash(name, after, platform='nt') is not None
    _case(tmp_path / 'base', name, before)
    _case(tmp_path / 'candidate', name, after)
    raw = golden.compare(tmp_path / 'base', tmp_path / 'candidate')
    assert raw == ['bytes differ: ' + name + '/stderr']
    remaining, normalized = crashes.normalize_known_crashes(raw, tmp_path / 'base', tmp_path / 'candidate', platform='nt')
    assert remaining == raw and normalized == []


def test_known_crash_normalizes_every_measure_frame_in_chained_tracebacks(tmp_path):
    """Frames normalize in both blocks of a chained traceback; its exception lines stay exact."""
    name = 'cli-devin-filter---quiet'
    chained = (b'Traceback (most recent call last):\r\n'
               b'  File "D:\\a\\x\\payload\\skills\\token-optimizer\\scripts\\measure.py", line %d, in helper\r\n'
               b'    raise KeyError(1)\r\n'
               b'KeyError: 1\r\n\r\n'
               b'During handling of the above exception, another exception occurred:\r\n\r\n')
    _case(tmp_path / 'base', name, chained % 10 + SIGPIPE_STDERR % 64099)
    _case(tmp_path / 'candidate', name, chained % 12 + SIGPIPE_STDERR % 63813)
    raw = golden.compare(tmp_path / 'base', tmp_path / 'candidate')
    remaining, normalized = crashes.normalize_known_crashes(raw, tmp_path / 'base', tmp_path / 'candidate', platform='nt')
    assert remaining == [] and normalized == [name + '/stderr: devin-sigpipe-windows']
    # The intermediate exception line of a chained traceback is outside every frame: exact.
    _case(tmp_path / 'other', name, (chained % 10).replace(b'KeyError: 1', b'KeyError: 2') + SIGPIPE_STDERR % 64099)
    raw = golden.compare(tmp_path / 'base', tmp_path / 'other')
    assert crashes.normalize_known_crashes(raw, tmp_path / 'base', tmp_path / 'other', platform='nt') == (raw, [])


_CLI_FRAME = (b'  File "D:\\a\\x\\payload\\skills\\token-optimizer\\scripts\\_measure\\cli.py", line 412, in main\r\n'
              b'    return devin_compress.main(args[1:])\r\n'
              b'           ~~~~~~~~~~~~~~~~~~~^^^^^^^^^^\r\n')
_DEVIN_FRAME = b'  File "D:\\a\\x\\payload\\skills\\token-optimizer\\scripts\\devin_compress.py"'


def _with_cli_frame(stderr):
    """Candidate shape after the CLI move: one more frame (cli.py) before devin_compress."""
    assert stderr.count(_DEVIN_FRAME) == 1
    return stderr.replace(_DEVIN_FRAME, _CLI_FRAME + _DEVIN_FRAME)


def _normalize(tmp_path, name, before, after, platform='nt'):
    _case(tmp_path / 'base', name, before)
    _case(tmp_path / 'candidate', name, after)
    raw = golden.compare(tmp_path / 'base', tmp_path / 'candidate')
    assert raw == ['bytes differ: ' + name + '/stderr']
    return raw, crashes.normalize_known_crashes(raw, tmp_path / 'base', tmp_path / 'candidate', platform=platform)


def test_listed_crash_ignores_an_extra_traceback_frame(tmp_path):
    """decisions-U9 F2: the CLI move adds a cli.py frame to the listed SIGPIPE traceback."""
    name = 'cli-devin-filter---quiet'
    after = _with_cli_frame(SIGPIPE_STDERR % 63813)
    raw, result = _normalize(tmp_path, name, SIGPIPE_STDERR % 64099, after)
    assert result == ([], [name + '/stderr: devin-sigpipe-windows'])


def test_listed_crash_ignores_changed_frame_source_lines(tmp_path):
    """Source lines belong to the ignored frame pairs (formerly a negative under the
    line-number-only policy; the F2 decision ignores whole frames)."""
    name = 'cli-devin-filter---quiet'
    stderr = SIGPIPE_STDERR % 64099
    after = stderr.replace(b'    sys.exit(devin_compress.main(args[1:]))\r\n',
                           b'    sys.exit(_measure_cli.main(args))\r\n')
    raw, result = _normalize(tmp_path, name, stderr, after)
    assert result == ([], [name + '/stderr: devin-sigpipe-windows'])


def test_listed_crash_with_a_changed_exception_message_stays_a_difference(tmp_path):
    name = 'cli-devin-filter---quiet'
    after = _with_cli_frame(SIGPIPE_STDERR % 63813).replace(b"Did you mean: 'SIGFPE'?", b"Did you mean: 'SIGINT'?")
    assert crashes.known_crash(name, after, platform='nt') is not None
    raw, result = _normalize(tmp_path, name, SIGPIPE_STDERR % 64099, after)
    assert result == (raw, [])


@pytest.mark.parametrize('name, platform', [('cli-dashboard', 'nt'), ('cli-devin-filter---quiet', 'posix')])
def test_unlisted_crash_with_an_extra_frame_stays_a_difference(tmp_path, name, platform):
    """Frames are ignored only for a listed crash on its platform."""
    stderr = SIGPIPE_STDERR % 64099
    raw, result = _normalize(tmp_path, name, stderr, _with_cli_frame(stderr), platform=platform)
    assert result == (raw, [])


def test_listed_crash_bytes_outside_the_traceback_block_stay_exact(tmp_path):
    name = 'cli-devin-filter---quiet'
    before = b'warning: fixture\r\n' + SIGPIPE_STDERR % 64099
    after = b'warning: fixturE\r\n' + _with_cli_frame(SIGPIPE_STDERR % 63813)
    raw, result = _normalize(tmp_path, name, before, after)
    assert result == (raw, [])
