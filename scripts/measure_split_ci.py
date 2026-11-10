#!/usr/bin/env python3
"""Candidate-payload and deterministic split proof for the existing CI matrix."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import measure_split as split
import measure_split_golden as golden
import measure_split_inventory as inventory
import measure_split_postbase as postbase
from check_measure_split import check
from check_measure_split_payload import verify, smoke
from measure_split_artifacts import compare_candidate, devin_install_proof_error
from measure_split_crashes import normalize_known_crashes


def extract(archive, target):
    """Only regular files/directories from our local git/npm artifacts."""
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as stream:
        for member in stream:
            relative = Path(member.name)
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('unsafe archive member: ' + member.name)
            path = target / relative
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
                path.chmod(member.mode & 0o777)
            elif member.isfile():
                path.parent.mkdir(parents=True, exist_ok=True)
                with stream.extractfile(member) as source, path.open('wb') as destination:
                    shutil.copyfileobj(source, destination)
                path.chmod(member.mode & 0o777)
            else:
                raise ValueError('unexpected archive link/device: ' + member.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--golden-only', action='store_true')
    parser.add_argument('--case', action='append', help='bounded case selection; skips the full performance workload')
    parser.add_argument('--shard', default='1/1')
    args = parser.parse_args()
    work = args.work.resolve()
    if not work.is_relative_to((split.REPO / '.scratch-u0').resolve()):
        parser.error('--work must be inside this checkout\'s .scratch-u0')
    work.mkdir(parents=True, exist_ok=True)
    errors, counts = check()
    if errors:
        raise RuntimeError('\n'.join(errors))
    print(counts, flush=True)
    if not args.golden_only:
        spec = importlib.util.spec_from_file_location('_split_npm_build', split.REPO / 'packaging/npm/build.py')
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        package = build.stage(work / 'npm-stage', include_untracked=False)
        npm_env = {**os.environ, 'npm_config_cache': str(work / 'npm-cache')}
        npm = shutil.which('npm')
        if not npm:
            raise RuntimeError('npm is required for the local candidate tarball')
        result = subprocess.run([npm, 'pack', '--json', '--offline', '--ignore-scripts',
                                 '--pack-destination', str(work)], cwd=package, env=npm_env,
                                check=True, capture_output=True, text=True)
        info = json.loads(result.stdout)[0]
        extract(work / info['filename'], work / 'installed')
        payload = work / 'installed/package'
        errors = verify(payload)
        if errors:
            raise RuntimeError('\n'.join(errors))
        if os.name != 'nt':
            for path in sorted(payload.rglob('*'), reverse=True):
                path.chmod(0o555 if path.is_dir() else 0o444)
            payload.chmod(0o555)
        print('read-only installed payload: ' + smoke(payload, work / 'installed-smoke'), flush=True)
    manifest = split.read_manifest()
    archive = work / 'base.tar'
    subprocess.run(['git', 'archive', '--format=tar', '-o', str(archive), manifest['base']],
                   cwd=split.REPO, check=True)
    baseline = work / 'base-source'
    extract(archive, baseline)
    candidate_archive = work / 'candidate.tar'
    subprocess.run(['git', 'archive', '--format=tar', '-o', str(candidate_archive), 'HEAD'],
                   cwd=split.REPO, check=True)
    candidate = work / 'candidate-source'
    extract(candidate_archive, candidate)
    # A version bump is not a behaviour change: re-stamp the candidate's release
    # surfaces to BASE's version, then compare byte-exact (Sol audit F1 rev2).
    golden.restamp(candidate, json.loads(
        (baseline / '.claude-plugin' / 'plugin.json').read_text(encoding='utf-8'))['version'])
    case_manifest = json.loads(golden.CASES.read_text())
    base_commands, _ = inventory.dispatch_inventory(
        (baseline / 'skills/token-optimizer/scripts/measure.py').read_text(encoding='utf-8'))
    bad = postbase.validate(case_manifest, base_commands)
    if bad:
        raise RuntimeError('post-BASE exemption list is invalid:\n' + '\n'.join(bad))
    exempt_ids = postbase.exempt_case_ids(case_manifest)
    rows = case_manifest['cases']
    if args.case:
        rows = [row for row in rows if row['id'] in args.case]
    shard, total = (int(value) for value in args.shard.split('/'))
    if not 1 <= shard <= total:
        parser.error('invalid shard')
    rows = rows[shard-1::total]
    if not rows:
        parser.error('empty golden selection')
    receipt = {'base': manifest['base'], 'candidate': subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=split.REPO, text=True).strip(),
        'provisional': manifest['provisional'], 'shard': args.shard,
        'case_manifest_sha256': hashlib.sha256(golden.CASES.read_bytes()).hexdigest(),
        'case_ids': [row['id'] for row in rows], 'captures': {}, 'comparisons': {}}
    def save_receipt():
        (work / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    save_receipt()
    for source, name in ((baseline, 'base-1'), (baseline, 'base-2'), (candidate, 'candidate')):
        receipt['captures'][name] = {'status': 'started'}
        save_receipt()
        try:
            count = golden.capture(source, work / 'golden-path', work / name, rows)
        except Exception as error:
            receipt['captures'][name] = {'status': 'failed', 'error': repr(error)}
            save_receipt()
            raise
        receipt['captures'][name] = {'status': 'complete', 'cases': count}
        save_receipt()
        print(f'{name}: {count} cases', flush=True)
    for actual in ('base-2', 'candidate'):
        identities = []
        if actual == 'candidate':
            errors, identities = compare_candidate(work / 'base-1', work / actual, baseline, candidate)
            print(f'{len(identities)} installed-source identity artifacts independently verified')
            proof = devin_install_proof_error([row['id'] for row in rows], identities)
            if proof:
                raise RuntimeError(proof)
            raw = errors + identities
            errors, normalized = normalize_known_crashes(errors, work / 'base-1', work / actual)
            print(f'{len(normalized)} listed BASE-crash stderr line-number differences: {normalized}')
            errors, exempted = postbase.split_errors(errors, exempt_ids)
            print(f'{len(exempted)} byte differences exempt as post-BASE commands: {exempted}')
        else:
            errors = golden.compare(work / 'base-1', work / actual)
            raw, normalized, exempted = list(errors), [], []
        receipt['comparisons'][actual] = {'differences': errors, 'verified_source_artifacts': identities,
                                          'normalized_known_crashes': normalized,
                                          'exempt_post_base': exempted,
                                          'raw_differences': sorted(raw)}
        save_receipt()
        if errors:
            raise RuntimeError(actual + ':\n' + '\n'.join(errors))
    print(f'zero user-output byte differences in {len(rows)} cases; provisional={manifest["provisional"]}')
    print('golden receipt: ' + str(work / 'receipt.json'), flush=True)
    if shard == 1 and not args.case:
        subprocess.run([sys.executable, str(split.REPO / 'scripts/measure_split_performance.py'),
                        '--baseline', str(baseline), '--work', str(work / 'performance')], check=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
