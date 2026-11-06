#!/usr/bin/env python3
"""Lane U5 golden proof: BASE twice, then this tree, raw-byte comparison.

Uses U0's capture/compare unchanged (scripts/measure_split_golden.py). Only the
orchestration differs from measure_split_ci.py: no npm packing, and a case
selection. BASE is a fresh `git archive` of the manifest's base commit.

    python3 scripts/split_lanes/u5_golden.py --select all
    python3 scripts/split_lanes/u5_golden.py --select audit --work DIR

The capture tool only accepts a workspace under <checkout>/.scratch-u0.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import measure_split as split  # noqa: E402
import measure_split_ci as ci  # noqa: E402
import measure_split_golden as golden  # noqa: E402

# Commands whose handlers run the audit domain directly.
AUDIT_COMMANDS = {
    'quick', 'report', 'doctor', 'snapshot', 'compare', 'coach', 'memory-review', 'attention-score',
    'attention-optimize', 'recommendations', 'git-context', 'drift', 'deterministic-candidates',
    'inject-coach', 'health', 'benchmark', 'dashboard', 'trends', 'check-staleness',
    'setup-coach-injection', 'structure-map', 'structure-proof', 'pin-baseline', 'set-baseline',
}


def select(rows, mode):
    if mode == 'all':
        return rows
    return [row for row in rows
            if row['entry'] != 'cli' or (row['argv'][:1] or [''])[0] in AUDIT_COMMANDS]


def summarize(total, errors):
    """Case counts and file counts, kept apart: one case can differ in several files.

    `errors` are U0's compare() lines ("bytes differ: <case>/<file>", "missing: ...",
    "unexpected: ..."); the first path component is the case id.
    """
    cases = sorted({error.split(': ', 1)[1].split('/', 1)[0] for error in errors})
    return {'cases': total, 'unchanged_cases': total - len(cases), 'changed_cases': len(cases),
            'file_differences': len(errors), 'changed_case_ids': cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--work', type=Path, default=split.REPO / '.scratch-u0/u5-golden')
    parser.add_argument('--select', choices=('all', 'audit'), default='all')
    parser.add_argument('--case', action='append')
    args = parser.parse_args()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    manifest = split.read_manifest()
    archive, baseline = work / 'base.tar', work / 'base-source'
    if baseline.exists():
        shutil.rmtree(baseline)
    subprocess.run(['git', 'archive', '--format=tar', '-o', str(archive), manifest['base']],
                   cwd=split.REPO, check=True)
    ci.extract(archive, baseline)
    if (baseline / 'skills/token-optimizer/scripts/_measure').exists():
        raise RuntimeError('BASE archive must be the unsplit tree')
    rows = select(json.loads(golden.CASES.read_text())['cases'], args.select)
    if args.case:
        rows = [row for row in rows if row['id'] in args.case]
    if not rows:
        parser.error('empty case selection')
    print(f'{len(rows)} cases, base={manifest["base"]}, provisional={manifest["provisional"]}', flush=True)
    for source, name in ((baseline, 'base-1'), (baseline, 'base-2'), (split.REPO, 'candidate')):
        out = work / name
        if out.exists():
            shutil.rmtree(out)
        started = time.monotonic()
        count = golden.capture(source, work / 'golden-path', out, rows)
        print(f'{name}: {count} cases in {time.monotonic() - started:.0f}s', flush=True)
    failed, summaries = False, {}
    for actual in ('base-2', 'candidate'):
        errors = golden.compare(work / 'base-1', work / actual)
        summaries[actual] = summarize(len(rows), errors)
        report = summaries[actual]
        print(f'compare base-1 vs {actual}: {report["unchanged_cases"]}/{report["cases"]} cases byte-identical; '
              f'{report["file_differences"]} file differences in {report["changed_cases"]} cases', flush=True)
        for error in errors[:50]:
            print('  ' + error, flush=True)
        failed = failed or bool(errors)
    candidate = summaries['candidate']
    print('RESULT ' + (f'FAIL: {candidate["unchanged_cases"]}/{candidate["cases"]} cases unchanged, '
                       f'{candidate["file_differences"]} file differences in {candidate["changed_cases"]} cases'
                       if failed else f'zero byte differences in {len(rows)} cases'), flush=True)
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
