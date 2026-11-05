#!/usr/bin/env python3
"""Verify the candidate's actual installed Python payload, including mirrors."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from measure_split import REPO, DOMAINS

SOURCES = ('__init__.py', '_compat.py') + tuple(name + '.py' for name in DOMAINS)


def verify(payload, canonical=REPO):
    relative = Path('skills/token-optimizer/scripts/_measure')
    errors = []
    for name in SOURCES:
        actual = payload / relative / name
        expected = canonical / relative / name
        if not actual.is_file():
            errors.append('missing installed provider: ' + str(actual))
        elif actual.read_bytes() != expected.read_bytes():
            errors.append('provider bytes differ: ' + str(actual))
    extra = {path.name for path in (payload / relative).glob('*.py')} - set(SOURCES)
    if extra:
        errors.append('unmanifested installed providers: ' + repr(sorted(extra)))
    return errors


def smoke(payload, work):
    home = work / 'home é with spaces'
    home.mkdir(parents=True, exist_ok=True)
    cwd = work / 'outside source'
    cwd.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('CLAUDE', 'CODEX', 'TOKEN_OPTIMIZER', 'HERMES', 'COPILOT', 'CURSOR', 'GROK', 'ANTIGRAVITY', 'DEVIN'))}
    env.update({'HOME': str(home), 'USERPROFILE': str(home), 'CLAUDE_CONFIG_DIR': str(home),
                'XDG_CACHE_HOME': str(home / '.cache'), 'PYTHONDONTWRITEBYTECODE': '1',
                'TOKEN_OPTIMIZER_REMOTE_LEAN': '1', 'TOKEN_OPTIMIZER_NO_PROC_SCAN': '1',
                'TOKEN_OPTIMIZER_COLD_COMPILE_WAIT_S': '-1', 'PYTHONUTF8': '1'})
    measure = payload / 'skills/token-optimizer/scripts/measure.py'
    commands = ([sys.executable, str(measure), '--version'],
                [sys.executable, str(payload / 'hooks/run.py'), 'skills/token-optimizer/scripts/measure.py', '--version'])
    outputs = []
    for argv in commands:
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=30)
        if proc.returncode or not proc.stdout.strip() or proc.stderr:
            raise RuntimeError(f'{argv}: exit={proc.returncode}, stdout={proc.stdout!r}, stderr={proc.stderr!r}')
        outputs.append(proc.stdout)
    if outputs[0] != outputs[1]:
        raise RuntimeError('direct CLI and real hook launcher version bytes differ')
    return outputs[0].decode().strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload', type=Path)
    parser.add_argument('--work', type=Path)
    parser.add_argument('--mirrors', action='store_true')
    args = parser.parse_args()
    payloads = [args.payload.resolve()] if args.payload else []
    if args.mirrors:
        payloads += [REPO / 'plugins/token-optimizer', REPO / 'cowork/token-optimizer']
    errors = [error for payload in payloads for error in verify(payload)]
    if not payloads:
        parser.error('select --payload or --mirrors')
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    if args.payload and args.work:
        print('installed CLI/hook smoke: ' + smoke(args.payload.resolve(), args.work.resolve()))
    print(f'{len(payloads)} complete payload(s), {len(SOURCES)} provider files each')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
