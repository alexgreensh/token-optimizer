#!/usr/bin/env python3
"""Collect every layer x OS receipt into one verdict table.

Layers are reported separately, never blended. A job that crashed or never
wrote its receipt counts as CANNOT-MEASURE, never PASS.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LAYERS = ('L4', 'L5', 'L7')
SYSTEMS = ('Linux', 'Windows', 'Darwin')


def collect(directory):
    found = {}
    for path in sorted(Path(directory).rglob('L*-result.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        found[(data['layer'], data['os'])] = data
    return found


def table(found, layers, systems):
    rows = ['| Layer | ' + ' | '.join(systems) + ' |', '|---|' + '---|' * len(systems)]
    verdicts = {}
    for layer in layers:
        cells = []
        for system in systems:
            data = found.get((layer, system))
            verdict = data['verdict'] if data else 'CANNOT-MEASURE (no receipt)'
            verdicts[(layer, system)] = verdict
            cells.append(f'**{verdict}**')
        rows.append(f'| {layer} | ' + ' | '.join(cells) + ' |')
    return rows, verdicts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('directory', type=Path)
    parser.add_argument('--layers', default=','.join(LAYERS))
    parser.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    layers = [x.strip() for x in args.layers.split(',') if x.strip()]
    found = collect(args.directory)
    rows, verdicts = table(found, layers, SYSTEMS)
    shas = sorted({(d['base_sha'], d['candidate_sha']) for d in found.values() if d.get('base_sha')})
    lines = ['## Split verification (L4 installed, L5 upgrade, L7 performance)', '']
    lines += [f'base `{b}` candidate `{k}`' for b, k in shas]
    if len(shas) > 1:
        lines.append('**Receipts disagree on the SHAs under test: CANNOT-MEASURE.**')
    lines += [''] + rows + ['']
    for (layer, system), data in sorted(found.items()):
        if data['verdict'] != 'PASS':
            lines.append(f"- {layer} {system}: " + '; '.join(data.get('reasons', []))[:1500])
    text = '\n'.join(lines) + '\n'
    print(text)
    if args.out:
        args.out.write_text(text, encoding='utf-8')
    passed = len(shas) == 1 and all(v == 'PASS' for v in verdicts.values())
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
