#!/usr/bin/env python3
"""Candidate-payload and deterministic split proof for the existing CI matrix."""
from __future__ import annotations
import argparse
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
from check_measure_split import check
from check_measure_split_payload import verify, smoke


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
    parser.add_argument('--case', action='append')
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
    rows = json.loads(golden.CASES.read_text())['cases']
    if args.case:
        rows = [row for row in rows if row['id'] in args.case]
    shard, total = (int(value) for value in args.shard.split('/'))
    if not 1 <= shard <= total:
        parser.error('invalid shard')
    rows = rows[shard-1::total]
    if not rows:
        parser.error('empty golden selection')
    for source, name in ((baseline, 'base-1'), (baseline, 'base-2'), (split.REPO, 'candidate')):
        print(f'{name}: {golden.capture(source, work / "golden-path", work / name, rows)} cases', flush=True)
    for actual in ('base-2', 'candidate'):
        errors = golden.compare(work / 'base-1', work / actual)
        if errors:
            raise RuntimeError(actual + ':\n' + '\n'.join(errors))
    print(f'zero byte differences in {len(rows)} cases; provisional={manifest["provisional"]}')
    if shard == 1:
        subprocess.run([sys.executable, str(split.REPO / 'scripts/measure_split_performance.py'),
                        '--baseline', str(baseline), '--work', str(work / 'performance')], check=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
