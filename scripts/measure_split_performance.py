#!/usr/bin/env python3
"""Compare repeated warm fixture calls; keep cold imports separate."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

import measure_split as split
import measure_split_golden as golden


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    names = ('quality-cache', 'compact-capture', 'status-bar', 'collection', 'dashboard')
    rows = [{'id': name, 'argv': [name], 'entry': 'perf', 'runtime': 'claude',
             'fixture': 'synthetic-current', 'stdin_base64': '', 'timeout': 120} for name in names]
    results = {}
    for source, label in ((args.baseline.resolve(), 'base'), (split.REPO, 'candidate')):
        golden.capture(source, work / 'fixed-path', work / label, rows)
        results[label] = {}
        for name in names:
            path = work / label / name
            if (path / 'exit').read_text().strip() != '0':
                raise RuntimeError(name + ': ' + (path / 'stderr').read_text())
            results[label][name] = json.loads((path / 'stdout').read_text())
    regressions = []
    for name in names:
        before = results['base'][name]['warm_median_seconds']
        after = results['candidate'][name]['warm_median_seconds']
        if after > before * 1.1 and after - before > 0.020:
            regressions.append(name)
    results['regressions_requiring_investigation'] = regressions
    (work / 'performance.json').write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps({'warm_medians': {label: {name: round(results[label][name]['warm_median_seconds'], 6)
                                              for name in names} for label in ('base', 'candidate')},
                      'regressions_requiring_investigation': regressions}, indent=2))
    return bool(regressions)


if __name__ == '__main__':
    raise SystemExit(main())
