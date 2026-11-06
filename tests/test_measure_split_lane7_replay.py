"""Lane U7 proof: the deterministic mover script (scripts/split_lanes/u7_move.py).

A fresh move of the pinned foundation reproduces the applied lane byte for byte, damaged and half-applied
trees are refused before anything is written, and the pinned foundation, LF bytes and CRLF refusal hold.
Provider and facade behaviour is proved in test_measure_split_lane7.py.
"""
import ast
import hashlib
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from helpers.measure_split import isolated_measure
from _lane7_helpers import (  # noqa: F401,F811  (fixtures are used by name)
    FACADE_REL,
    FRAGMENTS_REL,
    lf_fixtures,
    mover,
    PROVIDER,
    PROVIDER_REL,
    REPO,
    SCRIPTS,
    _fixture_env,
)


# ---- the mover script ----------------------------------------------------------------------------

def test_mover_reports_the_applied_tree_as_a_verified_no_op(mover, tmp_path):
    """A fresh move of the frozen foundation reproduces all three lane files byte for byte.

    The foundation is the commit recorded in the script (fetched by SHA in a shallow checkout), never a
    merge-base guess, so this runs the same way locally and in CI.
    """
    assert re.fullmatch('[0-9a-f]{40}', mover.FOUNDATION)
    result = mover.plan(REPO, foundation=mover.FOUNDATION, scratch_parent=tmp_path)
    assert result['moved'] == [] and result['already'] == 126 and len(result['fragments']) == 13
    assert result['verified'] == 126
    assert result['rebuilt'] == [mover.FACADE, mover.PROVIDER, mover.FRAGMENTS]
    assert result['provider'] == PROVIDER.read_text(encoding='utf-8')


def _git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), '-c', 'user.name=u7', '-c', 'user.email=u7@example.invalid',
                           '-c', 'commit.gpgsign=false', *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _present(repo, rev):
    return subprocess.run(['git', '-C', str(repo), 'cat-file', '-e', rev + '^{commit}'],
                          capture_output=True).returncode == 0


def test_ensure_commit_fetches_a_commit_that_a_shallow_clone_lacks(mover, tmp_path):
    """The CI situation: a one-commit checkout, the lane's foundation further back, fetched by SHA."""
    source = tmp_path / 'source'
    source.mkdir()
    _git(source, 'init', '-q', '-b', 'main')
    (source / 'a.txt').write_text('one\n', encoding='utf-8')
    _git(source, 'add', 'a.txt')
    _git(source, 'commit', '-q', '-m', 'one')
    first = _git(source, 'rev-parse', 'HEAD')
    (source / 'a.txt').write_text('two\n', encoding='utf-8')
    _git(source, 'commit', '-q', '-am', 'two')
    _git(source, 'config', 'uploadpack.allowReachableSHA1InWant', 'true')
    clone = tmp_path / 'clone'
    subprocess.run(['git', 'clone', '-q', '--depth=1', '--no-tags', source.as_uri(), str(clone)],
                   check=True, capture_output=True)
    assert not _present(clone, first), 'the shallow clone must really lack the commit for this test to mean anything'
    mover.ensure_commit(clone, first)
    assert _present(clone, first)
    assert _git(clone, 'show', f'{first}:a.txt').replace('\r', '') == 'one'
    mover.ensure_commit(clone, first)                       # already there: nothing to fetch, no error


def test_ensure_commit_says_why_a_commit_cannot_be_had(mover, tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    _git(source, 'init', '-q', '-b', 'main')
    (source / 'a.txt').write_text('one\n', encoding='utf-8')
    _git(source, 'add', 'a.txt')
    _git(source, 'commit', '-q', '-m', 'one')
    clone = tmp_path / 'clone'
    subprocess.run(['git', 'clone', '-q', source.as_uri(), str(clone)], check=True, capture_output=True)
    with pytest.raises(mover.MoveRefused, match='cannot read the foundation 0{40}'):
        mover.ensure_commit(clone, '0' * 40)


def test_the_cli_uses_the_recorded_foundation_for_this_checkout_only(mover, tmp_path, monkeypatch):
    seen = []

    def fake_plan(root, tool=None, foundation=None, scratch_parent=None):
        seen.append(foundation)
        return {'moved': [], 'already': 0, 'rebuilt': [], 'verified': 0, 'fragments': []}
    monkeypatch.setattr(mover, 'plan', fake_plan)
    monkeypatch.delenv('U7_FOUNDATION', raising=False)
    assert mover.main([]) == 0
    assert mover.main(['--root', str(tmp_path)]) == 0
    assert mover.main(['--foundation', 'abc123']) == 0
    monkeypatch.setenv('U7_FOUNDATION', 'def456')
    assert mover.main([]) == 0
    assert seen == [mover.FOUNDATION, None, 'abc123', 'def456']

def mini_tree(root):
    """A miniature facade with the same shapes the real packet has."""
    (root / 'scripts').mkdir(parents=True)
    (root / 'scripts/mutations').mkdir()
    scripts = root / 'skills/token-optimizer/scripts'
    shutil.copytree(SCRIPTS / '_measure', scripts / '_measure', ignore=shutil.ignore_patterns('__pycache__'))
    (scripts / '_measure/daemon.py').write_text(
        '"""Definition-only implementations; mutable state belongs to measure."""\n'
        'from __future__ import annotations\nfrom . import _compat\n', encoding='utf-8')
    shutil.copy(REPO / 'scripts/measure_split.py', root / 'scripts/measure_split.py')
    source = textwrap.dedent('''\
        import os
        import sys
        from pathlib import Path
        from _measure import _compat as _measure_compat
        _measure_providers = _measure_compat.prepare(sys.modules[__name__])
        LIMIT = 3
        CACHE = None


        def other(value):
            """Not part of the packet."""
            return value * 2


        def helper(value, limit=LIMIT):
            """Same-domain callee."""
            return min(value, limit)  # trailing note


        AFTER_HELPER = 'between'


        def lookup(value):
            """Reads the shared cache, calls both kinds of helper."""
            global CACHE
            local = os.getcwd()
            CACHE = (helper(value), other(value), Path(local).name, __file__)
            return CACHE


        if __name__ == "__main__":
            print(lookup(1))
        ''')
    (scripts / 'measure.py').write_text(source, encoding='utf-8')
    lines = source.splitlines()
    tree = ast.parse(source)
    rows = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in ('helper', 'lookup'):
            body = '\n'.join(lines[node.lineno - 1:node.end_lineno])
            rows.append({'name': node.name, 'module': 'daemon', 'lane': 7, 'start': node.lineno,
                         'end': node.end_lineno, 'kind': 'FunctionDef',
                         'signature': '\n'.join(lines[node.lineno - 1:node.body[0].lineno - 1]),
                         'global_declarations': [{'line': d.lineno - node.lineno, 'text': ast.get_source_segment(source, d)}
                                                 for d in ast.walk(node) if isinstance(d, ast.Global)],
                         'sha256': hashlib.sha256(body.encode()).hexdigest()})
    (root / 'tests/helpers').mkdir(parents=True)
    shutil.copy(REPO / 'tests/helpers/measure_split.py', root / 'tests/helpers/measure_split.py')
    other = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'other')
    foreign = {'name': 'other', 'module': 'runtime', 'lane': 1, 'start': other.lineno, 'end': other.end_lineno,
               'kind': 'FunctionDef', 'signature': 'def other(value):', 'global_declarations': [],
               'sha256': hashlib.sha256('\n'.join(lines[other.lineno - 1:other.end_lineno]).encode()).hexdigest()}
    guard = next(n for n in tree.body if isinstance(n, ast.If))
    manifest = {'symbols': rows + [foreign],
                'statements': [{'start': n.lineno, 'end': n.end_lineno, 'kind': type(n).__name__,
                                'sha256': hashlib.sha256('\n'.join(lines[n.lineno - 1:n.end_lineno]).encode()).hexdigest()}
                               for n in tree.body],
                'dispatch': {'module': 'cli', 'lane': 2, 'start': guard.lineno + 1, 'end': guard.end_lineno}}
    (root / 'scripts/measure_split_manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    (root / 'scripts/measure_split_ownership.json').write_text(json.dumps(
        {'lanes': {'7': {'modules': ['daemon'], 'symbols': rows}}}), encoding='utf-8')
    (root / 'scripts/mutations/_measure_lane7.json').write_text(json.dumps([
        {'mutation': 'mini', 'edit': 0, 'file': 'skills/token-optimizer/scripts/measure.py', 'symbol': 'lookup',
         'old': '    local = os.getcwd()\n    CACHE = (helper(value),', 'new': '    local = os.getcwd()\n    CACHE = (other(value),'},
        {'mutation': 'mini', 'edit': 1, 'file': 'skills/token-optimizer/scripts/measure.py', 'symbol': 'helper',
         'old': 'min(value, limit)', 'new': 'max(value, limit)'}]), encoding='utf-8')
    return scripts


def applied_mini(mover, tmp_path):
    """The miniature facade, a snapshot of the frozen foundation it came from, and the applied lane."""
    root = tmp_path / 'mini'
    scripts = mini_tree(root)
    foundation = tmp_path / 'foundation'
    shutil.copytree(root, foundation)
    mover.apply(root, plan_pending(mover, root, foundation, tmp_path))
    return root, scripts, foundation


def plan_applied(mover, root, foundation, tmp_path, tool=None):
    """Plan against a frozen miniature foundation, pinned by the hashes of that foundation's own files."""
    return mover.plan(root, tool, foundation=foundation, scratch_parent=tmp_path / 'scratch',
                      pins=mover.pins_for(foundation))


plan_pending = plan_applied      # the same call: the pinned fresh-move gate treats both states alike


def test_mover_moves_qualifies_and_binds_a_miniature_facade(mover, tmp_path):
    root = tmp_path / 'mini'
    scripts = mini_tree(root)
    foundation = tmp_path / 'foundation'
    shutil.copytree(root, foundation)
    result = plan_pending(mover, root, foundation, tmp_path)
    assert result['moved'] == ['helper', 'lookup'] and result['removed_lines'] > 0
    mover.apply(root, result)
    provider = (scripts / '_measure/daemon.py').read_text(encoding='utf-8')
    facade = (scripts / 'measure.py').read_text(encoding='utf-8')
    assert '_measure_facade.os.getcwd()' in provider and '_measure_facade.helper(value)' in provider
    assert '_measure_facade.CACHE = (' in provider and 'global CACHE' not in provider
    assert 'def other' in facade and 'def helper' not in facade and 'def lookup' not in facade
    assert "helper = _measure_compat.bind(_measure_providers['daemon'].helper, __name__, defaults=(LIMIT,))" in facade
    fragments = json.loads((root / 'scripts/mutations/_measure_lane7.json').read_text(encoding='utf-8'))
    assert all(f['old'] in provider for f in fragments)
    assert '_measure_facade.helper(value)' in fragments[0]['old']
    assert '_measure_facade.other(value)' in fragments[0]['new']
    again = plan_applied(mover, root, foundation, tmp_path)
    assert again['moved'] == [] and again['provider'] == provider and again['facade'] == facade
    assert again['verified'] == 2 and len(again['rebuilt']) == 3
    assert provider.count('# trailing note') == 1
    with isolated_measure(path=scripts / 'measure.py', name='measure_u7_mini', env=_fixture_env(tmp_path / 'home')) as mini:
        assert mini.helper(10) == 3
        assert mini.lookup(2)[0] == 2
        assert mini.CACHE[:2] == (2, 4) and mini.CACHE[3] == str(scripts / 'measure.py')
        mini.LIMIT = 1
        assert mini.helper(10, mini.LIMIT) == 1 and mini.helper(10) == 3   # default captured at bind time


@pytest.mark.parametrize('damage,message', [
    ('drop_binding', 'symbols not in a clean state'),
    ('duplicate_provider_body', 'symbols not in a clean state'),
    ('stray_provider_body', 'outside the lane packet'),
    ('stale_fragment', 'does not land'),
    ('foreign_fragment', 'outside lane 7'),
])
def test_mover_refuses_damaged_trees_before_writing(mover, tmp_path, damage, message):
    root, scripts, foundation = applied_mini(mover, tmp_path)
    facade, provider = scripts / 'measure.py', scripts / '_measure/daemon.py'
    fragments = root / 'scripts/mutations/_measure_lane7.json'
    if damage == 'drop_binding':
        text = facade.read_text(encoding='utf-8')
        facade.write_text('\n'.join(l for l in text.splitlines() if not l.startswith('lookup = ')) + '\n', encoding='utf-8')
    elif damage == 'duplicate_provider_body':
        provider.write_text(provider.read_text(encoding='utf-8') + '\n\ndef helper(value, limit=None):\n    pass\n', encoding='utf-8')
    elif damage == 'stray_provider_body':
        provider.write_text(provider.read_text(encoding='utf-8') + '\n\ndef stranger():\n    pass\n', encoding='utf-8')
    else:
        rows = json.loads(fragments.read_text(encoding='utf-8'))
        if damage == 'stale_fragment':
            rows[0]['old'] = 'not present anywhere'
        else:
            rows[0]['symbol'] = 'other'
        fragments.write_text(json.dumps(rows), encoding='utf-8')
    snapshot = {p: p.read_bytes() for p in (facade, provider, fragments)}
    with pytest.raises(mover.MoveRefused, match=message):
        plan_applied(mover, root, foundation, tmp_path)
    assert {p: p.read_bytes() for p in snapshot} == snapshot


def test_mover_refuses_a_half_applied_packet(mover, tmp_path):
    root = tmp_path / 'mini'
    scripts = mini_tree(root)
    foundation = tmp_path / 'foundation'
    shutil.copytree(root, foundation)
    mover.apply(root, plan_pending(mover, root, foundation, tmp_path))
    source = (scripts / 'measure.py').read_text(encoding='utf-8')
    original = textwrap.dedent('''\
        def lookup(value):
            return value
        ''')
    # A body for one packet symbol reappears in the facade while the other stays bound.
    (scripts / 'measure.py').write_text(source.replace(
        "lookup = _measure_compat.bind(_measure_providers['daemon'].lookup, __name__)", original), encoding='utf-8')
    provider = scripts / '_measure/daemon.py'
    tree = ast.parse(provider.read_text(encoding='utf-8'))
    keep = [n for n in tree.body if not (isinstance(n, ast.FunctionDef) and n.name == 'lookup')]
    provider.write_text(ast.unparse(ast.Module(body=keep, type_ignores=[])) + '\n', encoding='utf-8')
    with pytest.raises(mover.MoveRefused, match='half-applied'):
        mover.plan(root)


def test_mover_refuses_source_that_drifted_from_the_frozen_hash(mover, tmp_path):
    root = tmp_path / 'mini'
    scripts = mini_tree(root)
    path = scripts / 'measure.py'
    path.write_text(path.read_text(encoding='utf-8').replace('min(value, limit)', 'min(limit, value)'), encoding='utf-8')
    with pytest.raises(mover.MoveRefused, match='differs from the frozen hash'):
        mover.plan(root)


def _move_binding(text, name, before=None, after=None):
    lines = text.splitlines()
    binding = next(i for i, line in enumerate(lines) if line.startswith(name + ' = _measure_compat.bind('))
    moved = lines.pop(binding)
    anchor = next(i for i, line in enumerate(lines) if line.startswith(before or after))
    lines.insert(anchor if before else anchor + 1, moved)
    return '\n'.join(lines) + '\n'


@pytest.mark.parametrize('damage,path,diagnostic', [
    # one per class the reviews found, each caught by the fresh-move comparison
    ('body_edit', PROVIDER_REL, 'helper: body differs from the frozen source'),
    ('default_edit', FACADE_REL, 'helper: facade binding differs from the frozen signature'),
    ('binding_moved_across_init', FACADE_REL, 'helper: binding moved across'),
    ('binding_moved_below_later_statement', FACADE_REL, 'helper: binding moved across'),
    ('extra_provider_statement', PROVIDER_REL, None),
    # and the neighbours of those classes
    ('signature_edit', PROVIDER_REL, 'helper: provider signature differs from the frozen signature'),
    ('extra_binding_argument', FACADE_REL, 'lookup: facade binding differs from the frozen signature'),
    ('fragment_edit', FRAGMENTS_REL, None),
    ('constant_outside_bindings', FACADE_REL, None),
])
def test_mover_refuses_applied_trees_that_differ_from_a_fresh_move(mover, tmp_path, capsys, monkeypatch, damage, path, diagnostic):
    root, scripts, foundation = applied_mini(mover, tmp_path)
    control = plan_applied(mover, root, foundation, tmp_path)            # the clean applied tree passes
    assert control['verified'] == 2 and control['rebuilt'] == [FACADE_REL, PROVIDER_REL, FRAGMENTS_REL]
    target = root / path
    text = target.read_text(encoding='utf-8')

    def replaced(old, new):
        assert text.count(old) == 1, old
        return text.replace(old, new)
    damaged = {
        # the fragment text is still present, so only the body check or the comparison can see this
        'body_edit': lambda: replaced('return min(value, limit)', 'return min(value, limit) + 100'),
        'default_edit': lambda: replaced('defaults=(LIMIT,)', 'defaults=(999,)'),
        # lane order (helper before lookup) is preserved; only the position among other statements changes
        'binding_moved_across_init': lambda: _move_binding(text, 'helper', before='LIMIT = 3'),
        'binding_moved_below_later_statement': lambda: _move_binding(text, 'helper', after="AFTER_HELPER = 'between'"),
        'extra_provider_statement': lambda: text + "\nSIDE_EFFECT = print('runs at import')\n",
        'signature_edit': lambda: replaced('def helper(value, limit=None):', 'def helper(value, limit=None, extra=1):'),
        'extra_binding_argument': lambda: replaced('lookup, __name__)', 'lookup, __name__, decorators=(staticmethod,))'),
        'fragment_edit': lambda: replaced('max(value, limit)', 'max(limit, value)'),
        'constant_outside_bindings': lambda: replaced('LIMIT = 3\n', 'LIMIT = 999\n'),
    }[damage]()
    target.write_text(damaged, encoding='utf-8')
    tracked = [root / rel for rel in (FACADE_REL, PROVIDER_REL, FRAGMENTS_REL)]
    snapshot = {p: p.read_bytes() for p in tracked}
    with pytest.raises(mover.MoveRefused) as caught:
        plan_applied(mover, root, foundation, tmp_path)
    assert f'{path}: differs from a fresh move of the frozen inputs' in str(caught.value)
    if diagnostic:
        assert diagnostic in str(caught.value)
    monkeypatch.setattr(mover, 'PINS', mover.pins_for(foundation))     # the CLI reads the script's own pins
    assert mover.main(['--root', str(root), '--foundation', str(foundation), '--scratch', str(tmp_path / 'scratch')]) == 2
    assert f'{path}: differs from a fresh move' in capsys.readouterr().err
    assert {p: p.read_bytes() for p in tracked} == snapshot


def test_applied_mini_fails_to_run_after_its_binding_is_moved_across_initialization(mover, tmp_path):
    """The danger the position class guards: the captured default no longer sees its initialization."""
    root, scripts, foundation = applied_mini(mover, tmp_path)
    facade = scripts / 'measure.py'
    facade.write_text(_move_binding(facade.read_text(encoding='utf-8'), 'helper', before='LIMIT = 3'), encoding='utf-8')
    with pytest.raises(NameError, match='LIMIT'):
        with isolated_measure(path=facade, name='measure_u7_moved', env=_fixture_env(tmp_path / 'home')):
            pass


def test_position_check_ignores_other_lanes_and_added_statements_but_not_crossings(mover, tmp_path):
    root, scripts, foundation = applied_mini(mover, tmp_path)
    tool = mover.load_tool(root)
    names, _frozen = mover.lane_packet(root, tool)
    source = (scripts / 'measure.py').read_text(encoding='utf-8')
    other = 'def other(value):\n    """Not part of the packet."""\n    return value * 2'
    assert source.count(other) == 1
    # another lane replaced its definition in place, and U0 added a statement of its own
    tolerated = source.replace(other, "other = _measure_compat.bind(_measure_providers['runtime'].other, __name__)")
    tolerated = tolerated.replace('LIMIT = 3\n', "EXTRA_FROM_U0 = 'prologue'\nLIMIT = 3\n")
    assert mover.position_errors(tool, root, tolerated, ast.parse(tolerated), names) == []
    crossed = _move_binding(tolerated, 'helper', before='LIMIT = 3')
    errors = mover.position_errors(tool, root, crossed, ast.parse(crossed), names)
    assert len(errors) == 1 and errors[0].startswith('helper: binding moved across')


def test_mover_refuses_to_call_a_tree_applied_without_a_findable_foundation(mover, tmp_path, monkeypatch):
    root, scripts, foundation = applied_mini(mover, tmp_path)
    monkeypatch.delenv('U7_FOUNDATION', raising=False)
    with pytest.raises(mover.MoveRefused, match='cannot find the frozen foundation'):
        mover.plan(root, scratch_parent=tmp_path / 'scratch')


def test_apply_rolls_back_a_written_tree_that_fails_verification(mover, tmp_path, monkeypatch):
    root = tmp_path / 'mini'
    scripts = mini_tree(root)
    foundation = tmp_path / 'foundation'
    shutil.copytree(root, foundation)
    result = plan_pending(mover, root, foundation, tmp_path)
    tracked = (scripts / 'measure.py', scripts / '_measure/daemon.py', root / 'scripts/mutations/_measure_lane7.json')
    before = {p: p.read_bytes() for p in tracked}

    def refuse(*args, **kwargs):
        raise mover.MoveRefused('simulated post-write drift')
    monkeypatch.setattr(mover, 'verify_applied', refuse)
    with pytest.raises(mover.MoveRefused, match='simulated post-write drift'):
        mover.apply(root, result)
    assert {p: p.read_bytes() for p in tracked} == before


def test_trailing_comment_is_attached_once_even_if_the_extractor_keeps_it(mover, tmp_path, monkeypatch):
    root = tmp_path / 'mini'
    mini_tree(root)
    foundation = tmp_path / 'foundation'
    shutil.copytree(root, foundation)
    tool = mover.load_tool(root)
    assert plan_pending(mover, root, foundation, tmp_path, tool)['provider'].count('# trailing note') == 1
    original = tool.extract_definition

    def fixed(source, name, module, *, tree=None, table=None):
        moved, binding = original(source, name, module, tree=tree, table=table)
        return (moved[:-1] + '  # trailing note\n' if name == 'helper' else moved), binding
    monkeypatch.setattr(tool, 'extract_definition', fixed)
    assert plan_pending(mover, root, foundation, tmp_path, tool)['provider'].count('# trailing note') == 1


# ---- the frozen inputs are pinned, immutable and independent of the checkout ----------------------
# (orchestrator correction, REVIEW-astra-U4 finding 1: a rebuild from the current facade blesses any
# change outside the bindings; these tests use no git history at all)

def _mini_with_foundation(mover, tmp_path):
    root = tmp_path / 'mini'
    scripts = mini_tree(root)
    foundation = tmp_path / 'foundation'
    shutil.copytree(root, foundation)
    return root, scripts, foundation


def _edit(path, old, new):
    text = path.read_text(encoding='utf-8')
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new), encoding='utf-8')


@pytest.mark.parametrize('state', ['pending', 'applied'])
def test_a_changed_constant_outside_the_bindings_is_refused_with_no_history(mover, tmp_path, capsys, monkeypatch, state):
    root, scripts, foundation = _mini_with_foundation(mover, tmp_path)
    if state == 'applied':
        mover.apply(root, plan_pending(mover, root, foundation, tmp_path))
    _edit(scripts / 'measure.py', 'LIMIT = 3\n', 'LIMIT = 999\n')        # outside every lane binding
    tracked = [root / rel for rel in (FACADE_REL, PROVIDER_REL, FRAGMENTS_REL)]
    snapshot = {p: p.read_bytes() for p in tracked}
    assert not (root / '.git').exists()
    with pytest.raises(mover.MoveRefused) as caught:
        plan_applied(mover, root, foundation, tmp_path)
    assert f'{FACADE_REL}: differs from a fresh move of the frozen inputs' in str(caught.value)
    monkeypatch.setattr(mover, 'PINS', mover.pins_for(foundation))
    assert mover.main(['--root', str(root), '--foundation', str(foundation), '--scratch', str(tmp_path / 'scratch')]) == 2
    assert f'{FACADE_REL}: differs from a fresh move' in capsys.readouterr().err
    assert {p: p.read_bytes() for p in tracked} == snapshot


@pytest.mark.parametrize('tamper', ['constant_in_facade', 'edited_provider_module', 'extra_provider_module',
                                    'missing_provider_module', 'edited_tooling'])
def test_a_foundation_that_is_not_the_pinned_one_is_refused(mover, tmp_path, tamper):
    """The Astra scenario: change the foundation and the working tree alike, so a rebuild matches itself."""
    root, scripts, foundation = _mini_with_foundation(mover, tmp_path)
    pinned = mover.pins_for(foundation)                                 # what the script would have recorded
    inputs = foundation / 'skills/token-optimizer/scripts'
    message = {
        'constant_in_facade': 'is not the pinned',
        'edited_provider_module': 'is not the pinned',
        'extra_provider_module': 'is not one of the pinned',
        'missing_provider_module': 'missing from the frozen inputs',
        'edited_tooling': 'is not the pinned',
    }[tamper]
    if tamper == 'constant_in_facade':
        for base in (root, foundation):                                 # working tree and foundation agree
            _edit(base / 'skills/token-optimizer/scripts/measure.py', 'LIMIT = 3\n', 'LIMIT = 999\n')
    elif tamper == 'edited_provider_module':
        (inputs / '_measure/runtime.py').write_text('X = 1\n', encoding='utf-8')
    elif tamper == 'extra_provider_module':
        (inputs / '_measure/extra.py').write_text('X = 1\n', encoding='utf-8')
    elif tamper == 'missing_provider_module':
        (inputs / '_measure/runtime.py').unlink()
    else:
        with (foundation / 'scripts/measure_split.py').open('a', encoding='utf-8') as handle:
            handle.write('\n# tampered\n')
    before = {p: p.read_bytes() for p in (root / FACADE_REL, root / PROVIDER_REL, root / FRAGMENTS_REL)}
    with pytest.raises(mover.MoveRefused, match='not the pinned ones; nothing was written') as caught:
        mover.plan(root, foundation=foundation, scratch_parent=tmp_path / 'scratch', pins=pinned)
    assert message in str(caught.value)
    assert {p: p.read_bytes() for p in before} == before
    # Control: pins derived from the tampered inputs themselves bless them. That is why the pins are recorded
    # in the script, ahead of time, and never computed from whatever is on disk.
    if tamper == 'constant_in_facade':
        blessed = mover.plan(root, foundation=foundation, scratch_parent=tmp_path / 'scratch',
                             pins=mover.pins_for(foundation))
        assert blessed['moved'] == ['helper', 'lookup']


@pytest.mark.parametrize('state', ['pending', 'applied'])
def test_a_foundation_that_cannot_be_obtained_refuses_and_writes_nothing(mover, tmp_path, capsys, state):
    root, scripts, foundation = _mini_with_foundation(mover, tmp_path)
    if state == 'applied':
        mover.apply(root, plan_pending(mover, root, foundation, tmp_path))
    tracked = [root / rel for rel in (FACADE_REL, PROVIDER_REL, FRAGMENTS_REL)]
    snapshot = {p: p.read_bytes() for p in tracked}
    with pytest.raises(mover.MoveRefused, match='cannot read the foundation 0{40}'):
        mover.plan(root, foundation='0' * 40, scratch_parent=tmp_path / 'scratch', pins=mover.pins_for(foundation))
    assert mover.main(['--root', str(root), '--foundation', '0' * 40, '--scratch', str(tmp_path / 'scratch')]) == 2
    assert 'cannot read the foundation' in capsys.readouterr().err
    assert {p: p.read_bytes() for p in tracked} == snapshot
    assert not list((tmp_path / 'scratch').glob('u7-rebuild-*')), 'the throwaway rebuild must be cleaned up'


def test_an_empty_pin_table_refuses_everything(mover, tmp_path):
    root, scripts, foundation = _mini_with_foundation(mover, tmp_path)
    with pytest.raises(mover.MoveRefused, match='not the pinned ones'):
        mover.plan(root, foundation=foundation, scratch_parent=tmp_path / 'scratch', pins={})


def test_the_recorded_pins_are_the_real_foundations_and_a_changed_constant_cannot_pass_as_it(mover, tmp_path):
    mover.ensure_commit(REPO, mover.FOUNDATION)                           # fetched by SHA in a shallow checkout
    frozen = tmp_path / 'frozen'
    frozen.mkdir()
    mover.materialize(REPO, mover.FOUNDATION, frozen, mover.PINS)         # verified, or it raises
    blob = subprocess.run(['git', '-C', str(REPO), 'cat-file', 'blob', f'{mover.FOUNDATION}:{FACADE_REL}'],
                          capture_output=True, check=True).stdout
    assert hashlib.sha256(blob).hexdigest() == mover.PINS[FACADE_REL]
    assert set(mover.PINS) == set(mover.pins_for(frozen))
    facade = frozen / FACADE_REL
    text = facade.read_text(encoding='utf-8')
    constant = re.search(r'^([A-Z][A-Z_]+ = )(\d+)$', text, re.M)        # a numeric constant outside any binding
    assert constant
    facade.write_text(text[:constant.start(2)] + str(int(constant.group(2)) + 999) + text[constant.end(2):],
                      encoding='utf-8')
    with pytest.raises(mover.MoveRefused, match=re.escape(FACADE_REL) + r'.*is not the pinned'):
        mover.verify_pins(frozen, mover.PINS)


# ---- line endings: the repository is LF and replays compare raw bytes -----------------------------

def test_the_script_writes_lf_bytes_whatever_the_platform_default(mover, tmp_path, monkeypatch):
    root, scripts, foundation = _mini_with_foundation(mover, tmp_path)
    original = Path.write_text

    def windows_write_text(self, data, encoding=None, errors=None, newline=None):
        return original(self, data, encoding=encoding, errors=errors, newline='\r\n')   # what Windows would do
    monkeypatch.setattr(Path, 'write_text', windows_write_text)
    mover.apply(root, plan_pending(mover, root, foundation, tmp_path))
    for rel in (FACADE_REL, PROVIDER_REL, FRAGMENTS_REL):
        data = (root / rel).read_bytes()
        assert data.endswith(b'\n') and b'\r' not in data, rel


def test_crlf_is_refused_not_normalised(mover, tmp_path):
    root, scripts, foundation = applied_mini(mover, tmp_path)
    pinned = mover.pins_for(foundation)
    facade = root / FACADE_REL
    facade.write_bytes(facade.read_bytes().replace(b'\n', b'\r\n'))
    snapshot = facade.read_bytes()
    with pytest.raises(mover.MoveRefused, match='line endings only'):
        mover.plan(root, foundation=foundation, scratch_parent=tmp_path / 'scratch', pins=pinned)
    assert facade.read_bytes() == snapshot
    # a foundation whose files were checked out with CRLF is not the pinned one either
    frozen = foundation / FACADE_REL
    frozen.write_bytes(frozen.read_bytes().replace(b'\n', b'\r\n'))
    with pytest.raises(mover.MoveRefused, match=re.escape(FACADE_REL) + '.*is not the pinned'):
        mover.verify_pins(foundation, pinned)
