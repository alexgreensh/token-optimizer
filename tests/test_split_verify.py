"""Lane V harness (scripts/split_verify): the decision logic, never the heavy runs.

Installs, golden captures and timing loops run only in .github/workflows/
split-verify.yml. These tests never import measure.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts/split_verify'))

import common as c  # noqa: E402
import stats  # noqa: E402
import artifacts as a  # noqa: E402
import report  # noqa: E402
import l5_upgrade  # noqa: E402
import l4_installed  # noqa: E402


# ------------------------------------------------------------------ stats

def test_percentile_matches_linear_interpolation():
    values = list(range(1, 11))
    assert stats.percentile(values, 50) == 5.5
    assert stats.percentile(values, 95) == pytest.approx(9.55)
    assert stats.percentile([3.0], 95) == 3.0


def test_equal_arms_pass_and_slow_candidate_fails():
    base = [0.100 + 0.001 * (i % 7) for i in range(30)]
    same = list(base)
    assert stats.bar_verdict(base, same, pass_bar=0.05, fail_bar=0.10)['verdict'] == stats.PASS
    slow = [v * 1.30 for v in base]
    verdict = stats.bar_verdict(base, slow, pass_bar=0.05, fail_bar=0.10)
    assert verdict['verdict'] == stats.FAIL and verdict['interval_95'][0] > 1.05


def test_interval_crossing_the_bar_is_not_established():
    base = [0.1] * 30
    candidate = [0.1] * 26 + [0.2] * 4  # noisy tail straddles the p95 bar
    verdict = stats.bar_verdict(base, candidate, pass_bar=0.05, fail_bar=10.0)
    assert verdict['verdict'] == stats.CANNOT
    assert 'not established' in verdict['why']


def test_hard_fail_bar_uses_the_point_estimate():
    base = [0.1] * 29 + [0.3]
    candidate = [0.1] * 28 + [0.35, 0.36]
    verdict = stats.bar_verdict(base, candidate, pass_bar=0.05, fail_bar=0.10)
    assert verdict['ratio'] > 1.10
    assert verdict['verdict'] == stats.FAIL


def test_missing_or_short_samples_cannot_measure():
    assert stats.bar_verdict([0.1] * 29, [0.1] * 29, pass_bar=0.05)['verdict'] == stats.CANNOT
    broken = [0.1] * 29 + [None]
    assert stats.bar_verdict([0.1] * 30, broken, pass_bar=0.05)['verdict'] == stats.CANNOT


def test_bootstrap_is_deterministic():
    base = [0.1 + 0.01 * (i % 5) for i in range(30)]
    candidate = [v * 1.02 for v in reversed(base)]
    fn = stats.statistic('p95')
    assert stats.paired_ratio_interval(base, candidate, fn) == stats.paired_ratio_interval(base, candidate, fn)


# --------------------------------------------------------------- verdicts

def test_layer_verdict_rules():
    ok = [c.check('x', c.PASS)]
    live = {'proven': True}
    assert c.layer_verdict(ok, {'status': 'caught'}, live)[0] == c.PASS
    assert c.layer_verdict(ok, {'status': 'missed'}, live)[0] == c.FAIL
    assert c.layer_verdict(ok, {'status': 'not-run'}, live)[0] == c.CANNOT
    assert c.layer_verdict(ok, {'status': 'caught'}, {'proven': False})[0] == c.CANNOT
    assert c.layer_verdict([], {'status': 'caught'}, live)[0] == c.CANNOT
    failing = ok + [c.check('y', c.FAIL)]
    assert c.layer_verdict(failing, {'status': 'caught'}, {'proven': False})[0] == c.FAIL
    with pytest.raises(ValueError):
        c.check('z', 'WARN')


# --------------------------------------------------------------- contents

def _tree(root, files):
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    return root


PACKAGE = {c.FACADE: 'from _measure import _compat\n',
           c.SPLIT_PACKAGE + '/__init__.py': 'x = 1\n',
           c.SPLIT_PACKAGE + '/_compat.py': 'y = 2\n',
           c.SPLIT_PACKAGE + '/pricing.py': 'z = 3\n'}


def test_contents_check_and_deleted_file_control(tmp_path):
    tree = _tree(tmp_path / 'tree', PACKAGE)
    installed = _tree(tmp_path / 'installed', PACKAGE)
    assert c.contents_check('contents', installed, tree)['status'] == c.PASS
    (installed / c.SPLIT_PACKAGE / 'pricing.py').unlink()
    result = c.contents_check('contents', installed, tree)
    assert result['status'] == c.FAIL and 'missing: ' + c.SPLIT_PACKAGE + '/pricing.py' in result['detail']


def test_contents_check_rejects_changed_and_stale_files(tmp_path):
    tree = _tree(tmp_path / 'tree', PACKAGE)
    installed = _tree(tmp_path / 'installed', {**PACKAGE, c.SPLIT_PACKAGE + '/old_domain.py': 'stale\n'})
    (installed / c.SPLIT_PACKAGE / '_compat.py').write_text('y = 3\n', encoding='utf-8')
    detail = c.contents_check('contents', installed, tree)['detail']
    assert 'bytes differ: ' + c.SPLIT_PACKAGE + '/_compat.py' in detail
    assert 'unexpected: ' + c.SPLIT_PACKAGE + '/old_domain.py' in detail


def test_contents_check_without_a_package_cannot_measure(tmp_path):
    tree = _tree(tmp_path / 'tree', {c.FACADE: 'pass\n'})
    assert c.contents_check('contents', tree, tree)['status'] == c.CANNOT


def test_expected_modules_and_complete_liveness(tmp_path):
    tree = _tree(tmp_path / 'tree', PACKAGE)
    expected = c.expected_modules(tree)
    assert expected == {'_measure', '_measure._compat', '_measure.pricing'}
    root = tmp_path / 'install'
    summary = {'facade_reads': [str(root / c.FACADE)], 'outside_install': [],
               'split_modules': ['_measure', '_measure._compat', '_measure.pricing']}
    assert c.candidate_live(summary, expected)
    partial = dict(summary, split_modules=['_measure', '_measure._compat'])
    assert not c.candidate_live(partial, expected)
    assert not c.candidate_live(dict(summary, outside_install=['/elsewhere/measure.py']), expected)
    assert c.base_is_base(dict(summary, split_modules=[]))
    assert not c.base_is_base(summary)


def test_deferred_providers_are_not_required_for_liveness(tmp_path):
    files = dict(PACKAGE)
    files[c.SPLIT_PACKAGE + '/__init__.py'] = "DEFERRED_MODULES = ('pricing',)\n"
    tree = _tree(tmp_path / 'tree', files)
    assert c.deferred_modules(tree) == ('pricing',)
    assert c.expected_modules(tree) == {'_measure', '_measure._compat'}


# ----------------------------------------------------------- hook configs

def test_configured_hooks_reads_the_real_canonical_config():
    hooks = c.configured_hooks(REPO / 'hooks/hooks.json', ('SessionStart', 'PreCompact', 'PostToolUse'))
    argvs = [h['argv'] for h in hooks]
    assert ['hooks/sessionstart_runner.py'] in argvs
    assert ['hooks/posttooluse_runner.py'] in argvs
    assert ['skills/token-optimizer/scripts/measure.py', 'compact-capture', '--trigger', 'auto', '--quiet'] in argvs
    assert any(h['async'] for h in hooks)


def test_configured_hooks_reads_codex_style_commands(tmp_path):
    command = ('for b in bash /bin/bash; do command -v "$b" >/dev/null 2>&1 || continue; '
               '[ -r /p/hooks/run.py ] || exit 0; TOKEN_OPTIMIZER_RUNTIME=codex TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT=1 '
               'exec "$b" /p/hooks/python-launcher.sh /p/hooks/run.py hooks/posttooluse_runner.py >/dev/null 2>&1; done; exit 0')
    config = tmp_path / 'hooks.json'
    config.write_text(json.dumps({'hooks': {'PostToolUse': [{'matcher': 'Bash', 'hooks': [
        {'type': 'command', 'command': command}]}]}}), encoding='utf-8')
    [hook] = c.configured_hooks(config, ('PostToolUse', 'PreCompact'))
    assert hook['argv'] == ['hooks/posttooluse_runner.py']
    assert hook['env'] == {'TOKEN_OPTIMIZER_RUNTIME': 'codex', 'TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT': '1'}


def test_pinned_rows_come_from_the_installed_config():
    install = {'root': REPO, 'events': a.CLAUDE_EVENTS, 'runtime': 'claude', 'host': 'claude'}
    rows = l4_installed.pinned_rows(install)
    assert len({row['id'] for row in rows}) == len(rows)
    assert set(l4_installed.CLI_ROWS) <= {row['id'] for row in rows}  # OS-specific dashboard row
    assert all(row['input_variant'] == 'success' for row in rows if row['entry'] == 'hook')


def test_error_markers_flag_import_failures():
    result = {'exit': 0, 'stdout': b'', 'stderr': b"ModuleNotFoundError: No module named '_measure.pricing'\n"}
    assert not c.clean_run(result)
    assert c.clean_run({'exit': 0, 'stdout': b'ok', 'stderr': b''})


# ---------------------------------------------------------------- devin

def test_devin_closed_set(tmp_path):
    payload = a.devin_payload(REPO)
    assert 'devin_source.py' in payload and 'measure.py' not in payload
    installed = tmp_path / 'plugin'
    installed.mkdir()
    for name in payload:
        (installed / name).write_text('', encoding='utf-8')
    assert a.devin_closed_set(installed, REPO, 'contents')['status'] == c.PASS
    (installed / 'measure.py').write_text('', encoding='utf-8')
    assert a.devin_closed_set(installed, REPO, 'contents')['status'] == c.FAIL


# --------------------------------------------------------------- liveness

def test_liveness_probe_records_package_facade_and_rss(tmp_path):
    scripts = _tree(tmp_path / 'scripts', {'measure.py': 'import _measure.pricing\n',
                                           '_measure/__init__.py': '', '_measure/pricing.py': 'X = 1\n'})
    records = tmp_path / 'records'
    env = c.enable_liveness({**os.environ}, records)
    proc = subprocess.run([sys.executable, str(scripts / 'measure.py')], cwd=scripts, env=env,
                          capture_output=True, timeout=60)
    assert proc.returncode == 0 and proc.stdout == b'' and proc.stderr == b''
    summary = c.liveness_summary(c.read_liveness(records), scripts)
    assert summary['split_modules'] == ['_measure', '_measure.pricing']
    assert summary['facade_reads'] == [str((scripts / 'measure.py').resolve())]
    assert summary['peak_rss_bytes'] > 1_000_000
    assert summary['outside_install'] == []


def test_liveness_probe_is_silent_without_its_variable(tmp_path):
    env = {**os.environ, 'PYTHONPATH': str(c.PROBE_SITE)}
    env.pop('TO_SPLIT_VERIFY_LIVENESS', None)
    proc = subprocess.run([sys.executable, '-c', 'print(1)'], cwd=tmp_path, env=env, capture_output=True, timeout=60)
    assert proc.stdout.replace(b'\r\n', b'\n') == b'1\n' and proc.stderr == b''
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------- sandbox

def test_product_runs_refuse_a_home_outside_the_work_tree(tmp_path):
    with pytest.raises(RuntimeError):
        c.assert_sandboxed({'HOME': str(tmp_path)}, tmp_path)
    case = c.WORK_BASE / ('unit-' + uuid.uuid4().hex)
    try:
        inside = {key: str(case / 'home') for key in
                  ('HOME', 'USERPROFILE', 'CLAUDE_CONFIG_DIR', 'TOKEN_OPTIMIZER_SNAPSHOT_DIR', 'CODEX_HOME')}
        c.assert_sandboxed(inside, case)
        with pytest.raises(RuntimeError):
            c.assert_sandboxed(dict(inside, CLAUDE_CONFIG_DIR=str(Path.home() / '.claude')), case)
    finally:
        c.remove_tree(case)


def test_real_home_mode_refuses_off_ci(monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    with pytest.raises(RuntimeError):
        c.require_ci_runner()


# ----------------------------------------------------------- L5 numbers

def test_numbers_compare_numeric_leaves_only():
    before = {'_get_savings_summary': l5_upgrade.flatten({'total': 3, 'usd': 0.25, 'path': '/old', 'ok': True}),
              '_collect_trends_data': l5_upgrade.flatten({'days': [{'sessions': 2}], 'version': '5.13.64'})}
    after = {'_get_savings_summary': l5_upgrade.flatten({'total': 3, 'usd': 0.25, 'path': '/new', 'ok': True}),
             '_collect_trends_data': l5_upgrade.flatten({'days': [{'sessions': 2}], 'version': '5.13.65'})}
    assert l5_upgrade.diff_numbers(before, after) == []
    after['_get_savings_summary']['/usd'] = 0.26
    assert l5_upgrade.diff_numbers(before, after) == ['_get_savings_summary/usd: 0.25 -> 0.26']


def test_next_patch():
    assert c.next_patch('5.13.64') == '5.13.65'


def test_restamp_edits_exact_version_fields_only(tmp_path):
    """Exact-field restamp (Sol audit F1 rev3): JSON "version" fields and
    *VERSION* assignment literals are rewritten; release-shaped text anywhere
    else -- a description, an id, a longer token -- is byte-preserved."""
    import measure_split_golden as golden  # noqa: E402
    cand = tmp_path / 'cand'
    (cand / '.claude-plugin').mkdir(parents=True)
    (cand / '.claude-plugin' / 'plugin.json').write_text('{\n  "version": "5.13.66"\n}\n')
    (cand / 'package.json').write_text(
        '{\n  "name": "x",\n  "version": "5.13.66",\n'
        '  "description": "job-5.13.66-x",\n  "other": "5.13.660"\n}\n')
    (cand / 'code.ts').write_text(
        "export const CORE_VERSION = '5.13.66';\nconst NOTE = 'uses 5.13.66 inline';\n")
    (cand / 'CHANGELOG.md').write_text('## [5.13.66] - today\n')

    changed = golden.restamp(cand, '5.13.64',
                             surfaces=['.claude-plugin/plugin.json', 'package.json', 'code.ts'])
    assert sorted(changed) == ['.claude-plugin/plugin.json', 'code.ts', 'package.json']
    text = (cand / 'package.json').read_text()
    assert '"version": "5.13.64"' in text
    assert 'job-5.13.66-x' in text, 'a description is not a version field'
    assert '5.13.660' in text, 'a longer token is not an exact match'
    code = (cand / 'code.ts').read_text()
    assert "CORE_VERSION = '5.13.64'" in code
    assert "uses 5.13.66 inline" in code, 'a non-assignment literal is untouched'
    assert '[5.13.66]' in (cand / 'CHANGELOG.md').read_text(), 'markdown is never restamped'


def test_restamp_keeps_a_planted_non_version_change_failing(tmp_path):
    """A planted description survives restamp and still fails the byte
    compare: job-5.13.64-x -> job-5.13.66-x is a real product change."""
    import measure_split_golden as golden  # noqa: E402
    base, cand = tmp_path / 'base', tmp_path / 'cand'
    for side, version, desc in ((base, '5.13.64', 'job-5.13.64-x'),
                                (cand, '5.13.66', 'job-5.13.66-x')):
        (side / '.claude-plugin').mkdir(parents=True)
        (side / 'package.json').parent.mkdir(parents=True, exist_ok=True)
        (side / '.claude-plugin' / 'plugin.json').write_text(f'{{\n  "version": "{version}"\n}}\n')
        (side / 'package.json').write_text(
            f'{{\n  "version": "{version}",\n  "description": "{desc}"\n}}\n')
    golden.restamp(cand, '5.13.64', surfaces=['package.json'])
    assert '"version": "5.13.64"' in (cand / 'package.json').read_text()
    assert 'job-5.13.66-x' in (cand / 'package.json').read_text()
    assert golden.compare(base, cand) != [], 'the planted non-version change must still fail'


def test_real_tree_restamp_plan_equals_the_bump_fields():
    """Actual default discovery on THIS tree: every planned edit equals exactly
    the version-field multiset the release bumps changed (counts included)."""
    import measure_split_golden as golden  # noqa: E402
    fields = golden.bump_version_fields(REPO)
    assert len(fields) >= 9, fields
    assert fields[('.claude-plugin/marketplace.json', 'version')] == 2  # :16 and :58
    cand = json.loads((REPO / '.claude-plugin' / 'plugin.json').read_text())['version']
    planned = golden._plan(REPO, fields, cand)
    assert planned == fields, (dict(planned), dict(fields))


def test_restamp_counts_each_field_occurrence(tmp_path):
    """Two version fields in one file are two items (Sol A3 finding 2): damage
    only ONE of marketplace.json's pair and the validated plan must diverge."""
    import measure_split_golden as golden  # noqa: E402
    cand = tmp_path / 'cand'
    fields = golden.bump_version_fields(REPO)
    version = json.loads((REPO / '.claude-plugin' / 'plugin.json').read_text())['version']
    for rel in sorted({r for r, _ in fields}):
        dst = cand / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes((REPO / rel).read_bytes())
    assert golden._plan(cand, fields, version) == fields, 'clean copy must plan equal'

    market = cand / '.claude-plugin/marketplace.json'
    market.write_text(market.read_text().replace(
        f'"version": "{version}"', f'"version": "{version}-x"', 1))
    assert golden._plan(cand, fields, version) != fields
    with pytest.raises(RuntimeError, match='release-bump fields'):
        golden.restamp(cand, '5.13.64')


def test_restamp_writes_only_the_matched_span(tmp_path):
    """Only the matched quoted value span is rewritten (Sol A3 finding 1): a
    same-line literal carrying the same release bytes keeps them, and the
    resulting difference still fails the byte compare."""
    import measure_split_golden as golden  # noqa: E402
    base, cand = tmp_path / 'base', tmp_path / 'cand'
    for side, version, alias in ((base, '5.13.64', '5.13.64'), (cand, '5.13.66', '5.13.66')):
        (side / '.claude-plugin').mkdir(parents=True)
        (side / '.claude-plugin' / 'plugin.json').write_text(f'{{\n  "version": "{version}"\n}}\n')
        (side / 'code.ts').write_text(
            f'const CORE_VERSION_FALLBACK = "{version}"; const ALIAS = "{alias}";\n')
    golden.restamp(cand, '5.13.64', surfaces=['code.ts'])
    assert (cand / 'code.ts').read_text() == (
        'const CORE_VERSION_FALLBACK = "5.13.64"; const ALIAS = "5.13.66";\n')
    assert golden.compare(base, cand) != [], 'the untouched literal must still differ'


def test_sandbox_roots_are_lexical_only(monkeypatch):
    """_sandbox_roots must not touch the filesystem: Path.resolve, os.stat and
    os.lstat are patched to raise for the whole call (Sol A2 finding 3)."""
    def boom(*args, **kwargs):
        raise AssertionError('filesystem access in _sandbox_roots')
    monkeypatch.setattr(Path, 'resolve', boom)
    monkeypatch.setattr(os, 'stat', boom)
    monkeypatch.setattr(os, 'lstat', boom)
    monkeypatch.setenv('RUNNER_TEMP', '/home/runner/work/_temp')
    roots = c._sandbox_roots()
    assert roots == [c.WORK_BASE, Path('/home/runner/work/_temp/split-verify-homes')]


# ----------------------------------------------------------------- report

def _receipt(directory, layer, system, verdict, sha=('b' * 40, 'c' * 40)):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f'{layer}-result.json').write_text(json.dumps(
        {'layer': layer, 'os': system, 'verdict': verdict, 'reasons': [],
         'base_sha': sha[0], 'candidate_sha': sha[1]}), encoding='utf-8')


def test_report_requires_every_cell(tmp_path):
    for system in report.SYSTEMS:
        _receipt(tmp_path / system, 'L4', system, 'PASS')
    assert report.main([str(tmp_path), '--layers', 'L4']) == 0
    assert report.main([str(tmp_path), '--layers', 'L4,L5']) == 1


def test_report_rejects_mixed_shas_and_non_pass(tmp_path):
    _receipt(tmp_path / 'a', 'L4', 'Linux', 'PASS')
    _receipt(tmp_path / 'b', 'L4', 'Windows', 'PASS', sha=('b' * 40, 'd' * 40))
    _receipt(tmp_path / 'c', 'L4', 'Darwin', 'PASS')
    assert report.main([str(tmp_path), '--layers', 'L4']) == 1
    _receipt(tmp_path / 'b', 'L4', 'Windows', 'CANNOT-MEASURE')
    assert report.main([str(tmp_path), '--layers', 'L4']) == 1


def test_prefixed_bytecode_counts_as_the_install(tmp_path):
    root = tmp_path / 'cache' / '5.13.64'
    mirrored = tmp_path / 'cache/pycache' / root.resolve().as_posix().split(':', 1)[-1].lstrip('/')
    pyc = str(mirrored / 'skills/token-optimizer/scripts/__pycache__/measure.cpython-314.pyc')
    summary = c.liveness_summary([{'facade_reads': [pyc], 'split_modules': {}}], root)
    assert summary['outside_install'] == []
    other = c.liveness_summary([{'facade_reads': [str(tmp_path / 'elsewhere/measure.py')], 'split_modules': {}}], root)
    assert other['outside_install']


def test_installs_live_outside_any_git_work_tree(tmp_path, monkeypatch):
    assert c.in_git_work_tree(REPO / 'scripts')
    monkeypatch.setenv('TO_SPLIT_VERIFY_HOMES', str(REPO / '.scratch-u0' / 'homes'))
    with pytest.raises(RuntimeError):
        c.real_base()
    outside = tmp_path / 'homes'
    monkeypatch.setenv('TO_SPLIT_VERIFY_HOMES', str(outside))
    assert not c.in_git_work_tree(outside)
    case = c.case_dir(c.WORK_BASE / 'L4' / 'npm' / 'base')
    assert case == outside.resolve() / 'L4/npm/base/case'
    env = {key: str(case / 'home') for key in
           ('HOME', 'USERPROFILE', 'CLAUDE_CONFIG_DIR', 'TOKEN_OPTIMIZER_SNAPSHOT_DIR', 'CODEX_HOME')}
    c.assert_sandboxed(env, case)
    monkeypatch.delenv('TO_SPLIT_VERIFY_HOMES')
    monkeypatch.delenv('RUNNER_TEMP', raising=False)
    with pytest.raises(RuntimeError):
        c.real_base()
