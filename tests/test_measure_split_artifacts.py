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
