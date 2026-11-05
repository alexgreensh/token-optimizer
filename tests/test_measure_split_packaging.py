"""An intact facade cannot conceal a missing runtime package in a payload."""
import importlib.util
import importlib
from pathlib import Path
import shutil
import sys
import zipfile

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))


def test_cloud_payload_refuses_missing_domain(tmp_path):
    import cloud_kit
    from _measure import RUNTIME_SOURCES
    for relative in cloud_kit._REQUIRED:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    cloud_kit._validate_tree(tmp_path)
    package = tmp_path / 'skills/token-optimizer/scripts/_measure'
    assert all((package / name).is_file() for name in RUNTIME_SOURCES)
    (package / 'pricing.py').unlink()
    with pytest.raises(cloud_kit.KitError, match='pricing.py'):
        cloud_kit._validate_tree(tmp_path)


def test_runtime_manifest_and_hook_warmer_match():
    from _measure import RUNTIME_SOURCES
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
    'daemon.py', 'quality_archive.py', 'continuity.py', 'savings.py', 'cli.py'])
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
    (plugin / 'measure-path').write_text(str(scripts / 'measure.py'))
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
        shutil.copy2(SCRIPTS / name, plugin / name)
    payload = tmp_path / 'canonical engine é'
    scripts = payload / 'skills/token-optimizer/scripts'
    scripts.mkdir(parents=True)
    (scripts / 'measure.py').touch()
    shutil.copytree(SCRIPTS / '_measure', scripts / '_measure',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (plugin / 'measure-path').write_text(str(scripts / 'measure.py'))
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
