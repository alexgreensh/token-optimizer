"""The post-BASE exemption list may only admit commands that BASE does not have."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import measure_split_inventory as inventory  # noqa: E402
import measure_split_postbase as postbase  # noqa: E402

MANIFEST = json.loads((ROOT / 'tests/fixtures/measure_split/cases.json').read_text())


def base_commands():
    sha = MANIFEST['base']
    run = subprocess.run(['git', '-C', str(ROOT), 'show', f'{sha}:skills/token-optimizer/scripts/measure.py'],
                         capture_output=True, text=True)
    if run.returncode != 0:
        pytest.skip('BASE commit is not available in this checkout')
    commands, _ = inventory.dispatch_inventory(run.stdout)
    return set(commands)


def test_the_real_list_names_only_commands_absent_from_base():
    base = base_commands()
    listed = MANIFEST['post_base_commands']
    assert listed and not (set(listed) & base)
    assert postbase.validate(MANIFEST, base) == []


def test_a_baseline_command_in_the_list_fails():
    base = base_commands()
    assert 'drift' in base
    bad = dict(MANIFEST, post_base_commands=['coverage-replay', 'drift'])
    errors = postbase.validate(bad, base)
    assert len(errors) == 1 and errors[0].startswith('drift: exists at BASE')


def test_an_unknown_or_duplicate_entry_fails():
    base = base_commands()
    assert any('not a current command' in e for e in postbase.validate(dict(MANIFEST, post_base_commands=['no-such-command']), base))
    assert any('duplicates' in e for e in postbase.validate(dict(MANIFEST, post_base_commands=['file-refetch'] * 2), base))
    assert postbase.validate(dict(MANIFEST, post_base_commands='file-refetch'), base)


def test_exempt_cases_are_exactly_the_listed_commands_rows():
    ids = postbase.exempt_case_ids(MANIFEST)
    assert ids == {'cli-coverage-replay', 'cli-coverage-replay---json', 'cli-file-refetch', 'cli-file-refetch---json',
                   'cli-mod-action', 'cli-mod-action---json', 'cli-mod-action---limits-lab'}
    assert postbase.exempt_case_ids(dict(MANIFEST, post_base_commands=[])) == set()


def test_only_exempt_case_differences_are_dropped():
    ids = postbase.exempt_case_ids(MANIFEST)
    errors = ['bytes differ: cli-coverage-replay/stdout', 'bytes differ: cli-file-refetch---json/stderr',
              'bytes differ: cli-drift/stdout', 'missing: cli-drift---json/effects.json',
              'bytes differ: cli-coverage-replay-lookalike/stdout', 'unexpected: cli-coverage-replay', 'free text']
    kept, dropped = postbase.split_errors(errors, ids)
    assert dropped == ['bytes differ: cli-coverage-replay/stdout', 'bytes differ: cli-file-refetch---json/stderr']
    assert kept == errors[2:]
    assert postbase.split_errors(errors, set()) == (errors, [])


def test_every_exempt_case_still_exists_in_the_manifest():
    ids = {r['id'] for r in MANIFEST['cases']}
    assert postbase.exempt_case_ids(MANIFEST) <= ids
