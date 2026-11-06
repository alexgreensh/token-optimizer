"""Installed code identity is verified without weakening user-byte comparisons."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import measure_split_artifacts as artifacts


@pytest.fixture
def pair(tmp_path):
    roots = [tmp_path / name for name in ('base-source', 'candidate-source')]
    captures = [tmp_path / name for name in ('expected', 'actual')]
    texts = ["REPO_MODULES = frozenset(('measure',))\nVALUE = 1\n",
             "REPO_MODULES = frozenset(('_measure', 'measure'))\nVALUE = 1\n"]
    for root, capture, text in zip(roots, captures, texts):
        source = root / 'skills/token-optimizer/scripts/devin_source.py'
        source.parent.mkdir(parents=True)
        source.write_text(text)
        (source.parent / 'devin_install.py').write_text(
            'def _render_payload(name):\n    return (SCRIPTS / name).read_bytes()\n')
        case = capture / artifacts.CASE
        copied = case / artifacts.SOURCE
        copied.parent.mkdir(parents=True)
        copied.write_bytes(source.read_bytes())
        other = case / 'files/.config/devin/skills/output/SKILL.md'
        other.parent.mkdir(parents=True)
        other.write_text('fixture skill\n')
        manifest = case / artifacts.MANIFEST
        files = {'/fixture home/' + str(path.relative_to(case / 'files')).replace('\\', '/'):
                 hashlib.sha256(path.read_bytes()).hexdigest() for path in (copied, other)}
        manifest.write_text(json.dumps({'files': files, 'owner': 'devin-output-filter-v1'}))
        (case / 'stdout').write_bytes(b'{"usd":1.23}\n')
    return captures, roots


def compare(pair):
    captures, roots = pair
    return artifacts.compare_candidate(*captures, *roots)


def test_exact_refusal_root_and_integrity_delta_is_verified(pair):
    errors, identities = compare(pair)
    assert errors == []
    assert len(identities) == 2
    assert len(artifacts.golden.compare(*pair[0])) == 2  # raw artifacts remain intact


@pytest.mark.parametrize('kind', ['copy', 'digest', 'other_digest', 'owner', 'extra_field'])
def test_invalid_source_or_manifest_is_never_exempted(pair, kind):
    actual = pair[0][1] / artifacts.CASE
    manifest = actual / artifacts.MANIFEST
    data = json.loads(manifest.read_text())
    if kind == 'copy':
        (actual / artifacts.SOURCE).write_text('foreign source\n')
    elif kind in ('digest', 'other_digest'):
        key = next(key for key in data['files'] if key.endswith('devin_source.py') == (kind == 'digest'))
        data['files'][key] = '0' * 64
    elif kind == 'owner':
        data['owner'] = 'foreign-owner'
    else:
        data['unexpected'] = 'must remain byte-visible'
    manifest.write_text(json.dumps(data))
    errors, identities = compare(pair)
    assert errors and identities == []


def test_other_semantic_source_changes_are_not_exempted(pair):
    captures, roots = pair
    source = roots[1] / 'skills/token-optimizer/scripts/devin_source.py'
    source.write_text(source.read_text().replace('VALUE = 1', 'VALUE = 2'))
    copied = captures[1] / artifacts.CASE / artifacts.SOURCE
    copied.write_bytes(source.read_bytes())
    manifest = captures[1] / artifacts.CASE / artifacts.MANIFEST
    data = json.loads(manifest.read_text())
    key = next(key for key in data['files'] if key.endswith('devin_source.py'))
    data['files'][key] = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(data))
    assert compare(pair)[0]
    assert compare(pair)[1] == []


def test_a_user_dollar_difference_still_fails(pair):
    (pair[0][1] / artifacts.CASE / 'stdout').write_bytes(b'{"usd":1.24}\n')
    errors, identities = compare(pair)
    assert errors == ['bytes differ: ' + artifacts.CASE + '/stdout']
    assert len(identities) == 2


def test_installer_rendered_bytes_and_commitments_are_verified(pair):
    for capture, root in zip(*pair):
        scripts = root / 'skills/token-optimizer/scripts'
        (scripts / 'devin_install.py').write_text(
            'def _render_payload(name):\n    return (SCRIPTS / name).read_bytes() + b"\\n# installed guard\\n"\n')
        copied = capture / artifacts.CASE / artifacts.SOURCE
        copied.write_bytes(copied.read_bytes() + b'\n# installed guard\n')
        manifest = capture / artifacts.CASE / artifacts.MANIFEST
        data = json.loads(manifest.read_text())
        key = next(key for key in data['files'] if key.endswith('devin_source.py'))
        data['files'][key] = hashlib.sha256(copied.read_bytes()).hexdigest()
        manifest.write_text(json.dumps(data))
    errors, identities = compare(pair)
    assert errors == [] and len(identities) == 2


def test_changed_installer_transform_is_not_exempted(pair):
    installer = pair[1][1] / 'skills/token-optimizer/scripts/devin_install.py'
    installer.write_text(installer.read_text().replace('read_bytes()', 'read_bytes() + b"changed"'))
    assert compare(pair)[0] and compare(pair)[1] == []


@pytest.mark.parametrize('platform, ids, identities, refused', [
    ('posix', ['cli-devin-install', 'cli---version'], [], True),
    ('posix', ['cli-devin-install'], ['a', 'b'], False),
    ('posix', ['cli---version'], [], False),
    ('nt', ['cli-devin-install'], [], False),
])
def test_posix_capture_must_prove_the_devin_install_delta(platform, ids, identities, refused):
    """CI 42144651071 was green while every Linux shard verified 0 Devin artifacts: the hosted
    interpreter is world-writable, so devin-install refused in BASE and candidate alike and the
    split's one code change never ran. A POSIX run that selects the case must verify both."""
    error = artifacts.devin_install_proof_error(ids, identities, platform=platform)
    assert (error is not None) is refused
    if refused:
        assert 'trusted' in error


# ---- Generic verbatim-copy faithfulness (declared-module adapters) ----

VERBATIM_INSTALLER = '"""fixture installer"""\n_PAYLOAD_MODULES = (\n    "mod_one.py",\n    "mod_two.py",\n)\n'
VCASE = 'cli-antigravity-install'
VPLUGIN = VCASE + '/files/.gemini/config/plugins/token-optimizer'


@pytest.fixture
def vpair(tmp_path):
    roots = [tmp_path / name for name in ('base-source', 'candidate-source')]
    captures = [tmp_path / name for name in ('expected', 'actual')]
    for root, capture in zip(roots, captures):
        scripts = root / 'skills/token-optimizer/scripts'
        scripts.mkdir(parents=True)
        (scripts / 'antigravity_install.py').write_text(VERBATIM_INSTALLER)
        (scripts / 'mod_one.py').write_text('VALUE = 1\n')
        (scripts / 'mod_two.py').write_text('OTHER = 2\n')
        plugin = capture / VPLUGIN
        plugin.mkdir(parents=True)
        for name in ('mod_one.py', 'mod_two.py'):
            (plugin / name).write_bytes((scripts / name).read_bytes())
        (plugin / 'hooks.json').write_text('{"hooks": true}\n')
        (capture / VCASE / 'stdout').write_bytes(b'installed\n')
    return captures, roots


def _change_module(vpair, name, text):
    captures, roots = vpair
    source = roots[1] / 'skills/token-optimizer/scripts' / name
    source.write_text(text)
    (captures[1] / VPLUGIN / name).write_bytes(source.read_bytes())


def test_verbatim_module_change_is_verified(vpair):
    _change_module(vpair, 'mod_one.py', 'VALUE = 2\n')
    errors, identities = artifacts.compare_candidate(*vpair[0], *vpair[1])
    assert errors == []
    assert identities == ['bytes differ: ' + VPLUGIN + '/mod_one.py']


def test_unrelated_faithful_copies_are_verified(vpair):
    _change_module(vpair, 'mod_one.py', 'VALUE = 2\n')
    _change_module(vpair, 'mod_two.py', 'OTHER = 3\n')
    errors, identities = artifacts.compare_candidate(*vpair[0], *vpair[1])
    assert errors == [] and len(identities) == 2


def test_installer_list_edit_fails_acceptance(vpair):
    _change_module(vpair, 'mod_one.py', 'VALUE = 2\n')
    installer = vpair[1][1] / 'skills/token-optimizer/scripts/antigravity_install.py'
    installer.write_text(installer.read_text().replace('"mod_two.py",', '"mod_two.py",\n    "mod_three.py",'))
    errors, identities = artifacts.compare_candidate(*vpair[0], *vpair[1])
    assert errors and identities == []


def test_corrupt_copy_is_not_exempted(vpair):
    _change_module(vpair, 'mod_one.py', 'VALUE = 2\n')
    (vpair[0][1] / VPLUGIN / 'mod_one.py').write_text('VALUE = 2  # corrupted copy\n')
    errors, identities = artifacts.compare_candidate(*vpair[0], *vpair[1])
    assert errors and identities == []


def test_unmapped_neighbor_difference_stands(vpair):
    _change_module(vpair, 'mod_one.py', 'VALUE = 2\n')
    (vpair[0][1] / VPLUGIN / 'hooks.json').write_text('{"hooks": false}\n')
    errors, identities = artifacts.compare_candidate(*vpair[0], *vpair[1])
    assert errors == ['bytes differ: ' + VPLUGIN + '/hooks.json']
    assert identities == ['bytes differ: ' + VPLUGIN + '/mod_one.py']


def test_observed_difference_without_source_change_fails(vpair):
    (vpair[0][1] / VPLUGIN / 'mod_one.py').write_text('VALUE = 9\n')
    errors, identities = artifacts.compare_candidate(*vpair[0], *vpair[1])
    assert errors and identities == []


def test_user_output_difference_still_fails(vpair):
    _change_module(vpair, 'mod_one.py', 'VALUE = 2\n')
    (vpair[0][1] / VCASE / 'stdout').write_bytes(b'tampered\n')
    errors, identities = artifacts.compare_candidate(*vpair[0], *vpair[1])
    assert errors == ['bytes differ: ' + VCASE + '/stdout']
    assert identities == ['bytes differ: ' + VPLUGIN + '/mod_one.py']


# ---- Per-shard installed-source accounting ----

def _guard(vpair, ids, raw, identities, platform, expected=None):
    return artifacts.installed_source_proof_error(ids, raw, identities, *vpair[1],
                                                  expected or vpair[0][0], platform=platform)


def test_posix_vacuous_install_case_is_an_error(vpair):
    _ = vpair[0]
    source = vpair[1][1] / 'skills/token-optimizer/scripts/mod_one.py'
    source.write_text('VALUE = 2\n')
    error = _guard(vpair, [VCASE], [], [], 'posix')
    assert error and 'trusted' in error and VCASE in error


def test_windows_native_refusal_is_accepted(vpair):
    source = vpair[1][1] / 'skills/token-optimizer/scripts/mod_one.py'
    source.write_text('VALUE = 2\n')
    assert _guard(vpair, [VCASE], [], [], 'nt') is None


def test_verified_delta_satisfies_accounting(vpair):
    _change_module(vpair, 'mod_one.py', 'VALUE = 2\n')
    _, identities = artifacts.compare_candidate(*vpair[0], *vpair[1])
    assert _guard(vpair, [VCASE], identities, identities, 'posix') is None


def test_raw_difference_speaks_for_itself(vpair):
    source = vpair[1][1] / 'skills/token-optimizer/scripts/mod_one.py'
    source.write_text('VALUE = 2\n')
    raw = ['bytes differ: ' + VPLUGIN + '/mod_one.py']
    assert _guard(vpair, [VCASE], raw, [], 'posix') is None


def test_unselected_case_needs_no_accounting(vpair):
    source = vpair[1][1] / 'skills/token-optimizer/scripts/mod_one.py'
    source.write_text('VALUE = 2\n')
    assert _guard(vpair, ['cli---version'], [], [], 'posix') is None


def test_unchanged_sources_need_no_accounting(vpair):
    assert _guard(vpair, [VCASE], [], [], 'posix') is None


def test_changed_installer_still_accounts_for_vacuity(vpair, tmp_path):
    """An installer edit forbids blessing AND makes the delta unpredictable:
    a case that then left no artifacts (POSIX refusal) must error, even when
    no module changed - the edit needs a trusted capture, not a silent pass."""
    installer = vpair[1][1] / 'skills/token-optimizer/scripts/antigravity_install.py'
    installer.write_text(installer.read_text().replace('"mod_two.py",', '"mod_two.py",\n    "mod_three.py",'))
    error = _guard(vpair, [VCASE], [], [], 'posix', expected=tmp_path / 'empty')
    assert error and VCASE in error and 'trusted' in error


def test_changed_installer_with_artifacts_and_no_difference_is_fine(vpair):
    """Equal captures from a changed installer are ordinary frozen behavior."""
    installer = vpair[1][1] / 'skills/token-optimizer/scripts/antigravity_install.py'
    installer.write_text(installer.read_text().replace('"mod_two.py",', '"mod_two.py",\n    "mod_three.py",'))
    assert _guard(vpair, [VCASE], [], [], 'posix') is None


def test_changed_installer_with_observed_difference_speaks(vpair):
    installer = vpair[1][1] / 'skills/token-optimizer/scripts/antigravity_install.py'
    installer.write_text(installer.read_text().replace('"mod_two.py",', '"mod_two.py",\n    "mod_three.py",'))
    source = vpair[1][1] / 'skills/token-optimizer/scripts/mod_one.py'
    source.write_text('VALUE = 2\n')
    raw = ['bytes differ: ' + VPLUGIN + '/mod_one.py']
    assert _guard(vpair, [VCASE], raw, [], 'posix') is None


def test_judge_repro_installer_edit_plus_module_change_plus_refusal(vpair, tmp_path):
    """Exact HOLD repro: candidate edits the installer AND a module, POSIX
    refusal makes both captures identical - this must never be silent green."""
    installer = vpair[1][1] / 'skills/token-optimizer/scripts/antigravity_install.py'
    installer.write_text(installer.read_text().replace('"mod_two.py",', '"mod_two.py",\n    "mod_three.py",'))
    (vpair[1][1] / 'skills/token-optimizer/scripts/mod_one.py').write_text('VALUE = 2\n')
    error = _guard(vpair, [VCASE], [], [], 'posix', expected=tmp_path / 'empty')
    assert error and VCASE in error


def test_changed_devin_renderer_plus_refusal_is_an_error(pair, tmp_path):
    installer = pair[1][1] / 'skills/token-optimizer/scripts/devin_install.py'
    installer.write_text(installer.read_text().replace('read_bytes()', 'read_bytes() + b"x"'))
    error = artifacts.installed_source_proof_error(['cli-devin-install'], [], [], *pair[1],
                                                   tmp_path / 'empty', platform='posix')
    assert error and 'cli-devin-install' in error
