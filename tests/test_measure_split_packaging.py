"""An intact facade cannot conceal a missing runtime package in a payload."""
import importlib.util
import importlib
import json
from pathlib import Path
import shutil
import sys
import time
import zipfile

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'skills/token-optimizer/scripts'


def _tree_version():
    """The release this checkout pins; literals here rot on every version bump."""
    return json.loads((REPO / '.claude-plugin' / 'plugin.json').read_text(encoding='utf-8'))['version']
sys.path.insert(0, str(SCRIPTS))


@pytest.mark.parametrize('missing', [
    'skills/token-optimizer/scripts/_measure/pricing.py',
    'skills/token-optimizer/scripts/_measure/transcript_stream.py',
    'skills/token-optimizer/scripts/_measure/checkpoint_direction.py',
    'skills/token-optimizer/scripts/_measure/checkpoint_retention.py',
    'skills/token-optimizer/scripts/session_store_guard.py',
])
def test_cloud_payload_refuses_missing_domain(tmp_path, missing):
    import cloud_kit
    from _measure import RUNTIME_SOURCES
    for relative in cloud_kit._REQUIRED:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    cloud_kit._validate_tree(tmp_path)
    package = tmp_path / 'skills/token-optimizer/scripts/_measure'
    assert all((package / name).is_file() for name in RUNTIME_SOURCES)
    (tmp_path / missing).unlink()
    with pytest.raises(cloud_kit.KitError, match=Path(missing).name):
        cloud_kit._validate_tree(tmp_path)


def test_runtime_manifest_and_hook_warmer_match():
    from _measure import RUNTIME_SOURCES
    assert {'transcript_stream.py', 'checkpoint_direction.py', 'checkpoint_retention.py'} <= set(RUNTIME_SOURCES)
    spec = importlib.util.spec_from_file_location('split_gate_manifest', REPO / 'hooks/module_runner.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert tuple(module._MEASURE_PACKAGE_SOURCES) == RUNTIME_SOURCES


def test_devin_closed_set_refuses_package_and_every_provider():
    import devin_source
    from _measure import RUNTIME_SOURCES
    expected = {'_measure'} | {'_measure.' + Path(name).stem for name in RUNTIME_SOURCES if name != '__init__.py'}
    assert '_measure' in devin_source.REPO_MODULES
    assert not any(name.startswith('_measure.') for name in devin_source.REPO_MODULES)
    for name in expected:
        with pytest.raises(ImportError, match='outside Devin payload'):
            devin_source.SourceOnly(SCRIPTS).find_spec(name)


@pytest.mark.parametrize('missing', ['__init__.py', '_compat.py', 'runtime.py', 'pricing.py',
    'audit.py', 'dashboard.py', 'lifecycle.py', 'sessions.py', 'trends.py', 'cache_policy.py',
    'daemon.py', 'quality_archive.py', 'continuity.py', 'savings.py', 'cli.py', 'dashboard_history.py', 'weekly_activity.py',
    'transcript_stream.py', 'checkpoint_direction.py', 'checkpoint_retention.py'])
def test_native_completeness_refuses_each_missing_provider(tmp_path, monkeypatch, missing):
    import codex_doctor
    import hermes_doctor
    import hermes_install
    from _measure import RUNTIME_SOURCES
    expected = tuple('_measure/' + name for name in RUNTIME_SOURCES)
    for module in (codex_doctor, hermes_doctor, hermes_install):
        assert module._MEASURE_PACKAGE_FILES == expected
    roots = {'repo': tmp_path / 'repo', 'skill': tmp_path / 'skill'}
    for relative in codex_doctor.REQUIRED_FILES:
        path = roots['repo'] / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    for relative in set(codex_doctor.SKILL_INSTALL_FILES + hermes_doctor.SKILL_INSTALL_FILES):
        path = roots['skill'] / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    for directory in (roots['repo'] / 'skills/token-optimizer/scripts', roots['skill'] / 'scripts'):
        for relative in expected:
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
    monkeypatch.setattr(codex_doctor, '_skill_root', lambda: roots['skill'])
    monkeypatch.setattr(hermes_doctor, '_skill_root', lambda: roots['skill'])
    assert codex_doctor._required_file_checks(roots['repo'])[0]['detail'] == f'{len(codex_doctor.REQUIRED_FILES)} present'
    assert codex_doctor._skill_install_checks()[-1]['status'] == 'OK'
    assert hermes_doctor._skill_install_checks()[-1]['status'] == 'OK'
    for directory in (roots['repo'] / 'skills/token-optimizer/scripts', roots['skill'] / 'scripts'):
        (directory / '_measure' / missing).unlink()
    for checks in (codex_doctor._required_file_checks(roots['repo']), codex_doctor._skill_install_checks(),
                   hermes_doctor._skill_install_checks()):
        assert checks[0]['status'] == 'FAIL'
        assert '_measure/' + missing in checks[0]['detail']
    plugin = tmp_path / 'hermes-plugin'
    plugin.mkdir()
    scripts = roots['skill'] / 'scripts'
    for name in hermes_doctor.REQUIRED_ENGINE_SCRIPTS:
        (scripts / name).touch()
    (plugin / 'measure-path').write_text(str(scripts / 'measure.py'), encoding='utf-8')
    monkeypatch.setattr(hermes_doctor, '_plugin_install_dir', lambda _: plugin)
    check = hermes_doctor._engine_checks()[0]
    assert check['status'] == 'FAIL' and '_measure/' + missing in check['detail']
    for name in hermes_install._RUNTIME_MODULES:
        (scripts / name).touch()
    monkeypatch.setattr(hermes_install, '_scripts_dir', lambda: scripts)
    monkeypatch.setattr(hermes_install, '_payload_dir', lambda: plugin)
    with pytest.raises(ValueError, match='_measure/' + missing):
        hermes_install.install()


@pytest.mark.parametrize('runtime', ['copilot', 'cursor', 'grok', 'antigravity'])
def test_installed_bridge_locator_requires_complete_canonical_payload(tmp_path, runtime):
    sys.path.insert(0, str(REPO / 'scripts'))
    from check_measure_split_payload import verify
    installer = importlib.import_module(runtime + '_install')
    plugin = tmp_path / 'installed bridge'
    plugin.mkdir()
    for name in installer._PAYLOAD_MODULES:
        (plugin / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SCRIPTS / name, plugin / name)
    payload = tmp_path / 'canonical engine é'
    scripts = payload / 'skills/token-optimizer/scripts'
    scripts.mkdir(parents=True)
    (scripts / 'measure.py').touch()
    shutil.copytree(SCRIPTS / '_measure', scripts / '_measure',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (plugin / 'measure-path').write_text(str(scripts / 'measure.py'), encoding='utf-8')
    spec = importlib.util.spec_from_file_location('_split_' + runtime + '_bridge', plugin / (runtime + '_hook_bridge.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = module._locate_measure_py()
    assert target == scripts / 'measure.py'
    assert verify(target.parents[3]) == []
    (scripts / '_measure/pricing.py').unlink()
    assert any('pricing.py' in error for error in verify(target.parents[3]))


def test_cowork_distributable_zip_contains_every_provider(tmp_path):
    import cowork_install
    from _measure import RUNTIME_SOURCES
    payload = cowork_install.build_plugin_payload(REPO, tmp_path)
    archive = cowork_install._zip(payload)
    with zipfile.ZipFile(archive) as stream:
        for name in RUNTIME_SOURCES:
            relative = 'skills/token-optimizer/scripts/_measure/' + name
            matches = [member for member in stream.namelist() if member.endswith('/' + relative) or member == relative]
            assert len(matches) == 1
            assert stream.read(matches[0]) == (SCRIPTS / '_measure' / name).read_bytes()


def test_installed_smoke_exercises_normal_json_and_accounting(tmp_path):
    sys.path.insert(0, str(REPO / 'scripts'))
    from check_measure_split_payload import smoke
    assert _tree_version() in smoke(REPO, tmp_path)
    # Windows 3.14 CI timed out recompiling the facade on every call; it compiles once, outside the payload.
    assert list((tmp_path / 'pycache').rglob('measure.*.pyc'))
    for name in ('direct', 'hook'):
        settings = json.loads((tmp_path / name / 'home é/settings.json').read_text(encoding='utf-8'))
        assert settings['unrelated'] is True
        assert 'fixture' in settings['_disabledMcpServers']


@pytest.mark.parametrize('origin', ['.', 'plugins/token-optimizer', 'cowork/token-optimizer'])
def test_original_manifest_shell_launcher_recovers_root_and_runs_guidance(tmp_path, origin):
    import os
    import subprocess
    from test_windows_hook_launcher import _hook_runtime_bash
    bash = _hook_runtime_bash()
    if not bash:
        pytest.skip('the hook runtime requires Bash or Git Bash')
    source = REPO / origin
    version = tmp_path / 'versions/5.13.64'
    for relative in ('hooks', 'skills/token-optimizer', '.claude-plugin'):
        if (source / relative).is_dir():
            shutil.copytree(source / relative, version / relative,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    manifest = json.loads((version / 'hooks/hooks.json').read_text(encoding='utf-8'))
    commands = [hook['command'] for groups in manifest['hooks'].values() for group in groups for hook in group['hooks']]
    guidance = next((command for command in commands if 'dynamic-compact-instructions' in command), None)
    command = guidance or next(command for command in commands if 'read_cache.py' in command)
    home = tmp_path / 'home é'
    home.mkdir()
    (home / '.claude').mkdir()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('CLAUDE', 'CODEX', 'TOKEN_OPTIMIZER'))}
    env.update({'HOME': str(home), 'USERPROFILE': str(home), 'CLAUDE_CONFIG_DIR': str(home / '.claude'),
                'TOKEN_OPTIMIZER_SNAPSHOT_DIR': str(home / 'data'), 'TOKEN_OPTIMIZER_RUNTIME': 'claude',
                'TOKEN_OPTIMIZER_NO_PROC_SCAN': '1', 'TOKEN_OPTIMIZER_PYTHON': sys.executable,
                'TOKEN_OPTIMIZER_COLD_COMPILE_WAIT_S': '-1', 'PYTHONUTF8': '1',
                'PYTHONPYCACHEPREFIX': str(tmp_path / 'pycache'), 'XDG_CACHE_HOME': str(home / '.cache')})
    env.pop('PYTHONDONTWRITEBYTECODE', None)

    timing = {}

    def launch(root, cache):
        run_env = {**env, 'CLAUDE_PLUGIN_ROOT': root, 'PYTHONPYCACHEPREFIX': str(cache)}
        warm = 'import sys; sys.path.insert(0, sys.argv[1]); import measure, read_cache'
        warm_started = time.monotonic()
        proc = subprocess.run([sys.executable, '-c', warm, str(version / 'skills/token-optimizer/scripts')],
                              cwd=home, env=run_env, capture_output=True, timeout=45)
        timing['warm_s'] = round(time.monotonic() - warm_started, 2)
        assert proc.returncode == 0 and not proc.stderr, proc.stderr
        # The warm-up compiles read_cache too; drop it so only the real entry can recreate it.
        for compiled in cache.rglob('read_cache.*.pyc'):
            compiled.unlink()
        started = time.monotonic()
        result = subprocess.run([bash, '-c', command], cwd=home, env=run_env,
                                input=b'{}', capture_output=True, timeout=45)
        timing['launch_s'] = round(time.monotonic() - started, 2)
        return result

    def diagnostics(root, index, proc):
        # Failure-only context for flaky Windows launcher runs. No env dump, no secrets: the
        # launcher inputs are the test's own tmp paths. Does not change any pass condition.
        probe = ''
        try:
            listing = subprocess.run(
                [bash, '-c', 'for b in bash /bin/bash /usr/bin/bash /usr/local/bin/bash '
                             '/opt/homebrew/bin/bash; do printf "%s=" "$b"; command -v "$b" || echo -; done'],
                capture_output=True, timeout=20)
            probe = listing.stdout.decode('utf-8', 'replace')[-600:]
        except Exception as exc:  # noqa: BLE001 - diagnostics must never mask the real failure
            probe = 'probe failed: %s' % type(exc).__name__
        return ('launcher diagnostics: root_index=%d root=%s returncode=%s launch_s=%s warm_s=%s '
                'selected_bash=%s stdout=%r stderr=%r candidates=[%s]' % (
                    index, root, proc.returncode, timing.get('launch_s'), timing.get('warm_s'),
                    bash, proc.stdout[-400:], proc.stderr[-400:], probe.strip().replace('\n', ' ')))

    stale = version.with_name('5.13.63-missing')
    # Pinned BASE 5.13.64 behavior on Windows, unchanged by the split (diag runs
    # 42132891199 and 42136205975 traced BASE and candidate under bash -x): Git Bash
    # leaves "${D%[/\\]*}" unstripped for backslash AND forward-slash roots, so a
    # stale root never recovers and the hook fails open silently. Product bug,
    # reported in U0-FINAL2-REPORT.md; a hooks.json fix must flip this branch.
    recovers = os.name != 'nt'
    roots = [str(version), str(stale)]
    if not recovers:
        roots += [str(version).replace('\\', '/'), str(stale).replace('\\', '/')]
    outputs = []
    for index, root in enumerate(roots):
        cache = tmp_path / ('entry-cache-' + str(index))
        proc = launch(root, cache)
        assert proc.returncode == 0 and not proc.stderr, (proc.stderr, diagnostics(root, index, proc))
        if root.endswith('5.13.63-missing') and not recovers:
            assert proc.stdout == b'', diagnostics(root, index, proc)
            assert not list(cache.rglob('read_cache.*.pyc'))
            continue
        if guidance:
            assert b'COMPACTION GUIDANCE:' in proc.stdout, diagnostics(root, index, proc)
        else:
            assert b'Traceback' not in proc.stderr
            assert list(cache.rglob('read_cache.*.pyc')), 'the Cowork entry never loaded its real target'
        outputs.append(proc.stdout)
    assert len(outputs) == 2 and outputs[0] == outputs[1]
