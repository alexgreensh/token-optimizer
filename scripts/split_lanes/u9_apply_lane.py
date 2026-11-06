#!/usr/bin/env python3
"""U9: apply one extraction lane to the aggregate tree with that lane's own mover.

Each lane script (scripts/split_lanes/uN_move.py) has two layers:

* an inner move that finds the lane's symbols BY NAME in measure.py, moves them
  with U0's scope-aware extractor, replaces each in place with its binding,
  requalifies the lane's mutation fragments and validates the result against
  the frozen BASE (bodies, bindings, positions);
* an outer completed-state guard that requires measure.py to be byte-identical
  to the untouched U0 tree the lane was cut from (or to a replay on it).

On the aggregate, measure.py also carries the lanes applied before this one,
so the outer guard refuses by construction. This driver calls each lane's
inner move directly, on the aggregate tree. It never copies or edits lane code.
U1 and U2 run through their CLI (U1's tree still equals its U0 inputs; U2's
CLI moves first and only then runs its single-lane byte check).

The replacement for the outer guard is outside this file: after each lane the
integrator byte-compares every lane-written file with a 3-way git merge of the
lanes' own reviewed output commits (see the U9 build script and
decisions-U9.md).

Usage: python3.14 scripts/split_lanes/u9_apply_lane.py N
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LANES = REPO / 'scripts/split_lanes'
SCRIPTS = REPO / 'skills/token-optimizer/scripts'


def load(lane):
    path = LANES / f'u{lane}_move.py'
    spec = importlib.util.spec_from_file_location(f'u{lane}_move', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def cli(lane):
    proc = subprocess.run([sys.executable, str(LANES / f'u{lane}_move.py')], cwd=REPO,
                          capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode, proc.stdout + proc.stderr


def lane1():
    code, _ = cli(1)
    if code:
        raise SystemExit(f'U9: lane 1 CLI exited {code}')
    return {'mode': 'cli'}


def lane2():
    code, text = cli(2)
    moved = 'U2: moved 100 definitions and the CLI dispatch; bodies, bindings, positions and the dispatch match the frozen BASE'
    if code == 0:
        return {'mode': 'cli'}
    # The only accepted refusal: the single-lane byte check, after the move and its finer checks passed,
    # naming exactly the two files that must differ on an aggregate (facade, and the record of its hash).
    differing = [line.split(' != ')[0].strip() for line in text.splitlines() if ' != ' in line]
    expected = [str(SCRIPTS / 'measure.py'), str(REPO / 'scripts/split_lanes/u2_frozen.json')]
    if moved in text and 'lane output differs from the foundation re-run' in text and differing == expected:
        return {'mode': 'cli', 'single_lane_check': 'refused as expected on the aggregate', 'differing': differing}
    raise SystemExit(f'U9: lane 2 CLI exited {code} with an unexpected refusal')


def lane3():
    u3 = load(3)
    names, manifest = u3.lane_symbols()
    _, pending = u3.classify(SCRIPTS, names, manifest)
    if len(pending) != len(names):
        raise SystemExit(f'U9: lane 3 is not fully pending ({len(pending)} of {len(names)})')
    u3.move_fresh(SCRIPTS)
    return {'mode': 'inner', 'entry': 'move_fresh', 'moved': len(pending)}


def lane4():
    u4 = load(4)
    code = u4.run(REPO, rebuilding=True)
    if code:
        raise SystemExit(f'U9: lane 4 run(rebuilding=True) returned {code}')
    return {'mode': 'inner', 'entry': 'run(rebuilding=True)'}


def lane5():
    u5 = load(5)
    lane, _ = u5.load_packet(REPO)
    lock = u5.load_lock(REPO, lane)
    frozen = u5.frozen_source_text(REPO, lock, None)
    report = u5.run(REPO, _frozen=frozen, _oracle=True)
    return {'mode': 'inner', 'entry': 'run(_oracle=True)', **report}


def lane6():
    u6 = load(6)
    manifest = json.loads((REPO / 'scripts/measure_split_manifest.json').read_text(encoding='utf-8'))
    owners = u6.lane_symbols(REPO / 'scripts/measure_split_ownership.json')
    rows = {row['name']: row for row in manifest['symbols'] if row['name'] in owners}
    frozen = u6.load_pinned_frozen(manifest, None)
    inputs = u6.lane_inputs()
    unmoved, moved = u6.state(SCRIPTS, owners)
    if moved:
        raise SystemExit(f'U9: lane 6 is not fully pending ({len(moved)} already moved)')
    stubs = {m: inputs['providers'][m] for m in u6.MODULES}
    report = u6._move(SCRIPTS, owners, rows, frozen, REPO / 'scripts/mutations/_measure_lane6.json', stubs)
    return {'mode': 'inner', 'entry': '_move', **report}


def lane7():
    u7 = load(7)
    found = u7.examine(REPO)
    if found['state'] != 'pending':
        raise SystemExit(f'U9: lane 7 state is {found["state"]}, not pending')
    u7.apply(REPO, found['result'])
    return {'mode': 'inner', 'entry': 'examine+apply', 'moved': len(found['result']['moved'])}


def lane8():
    u8 = load(8)
    rows = u8.lane_rows(json.loads(u8.OWNERSHIP.read_text(encoding='utf-8')))
    u8.frozen_inputs()  # pinned BASE + lane packet slices still verified; the inputs themselves are single-lane
    report = u8._apply(SCRIPTS, u8.FRAGMENTS, rows, write=True)
    return {'mode': 'inner', 'entry': '_apply(write=True)', **report}


def main(argv):
    if len(argv) != 2 or argv[1] not in '12345678' or len(argv[1]) != 1:
        raise SystemExit(__doc__)
    report = globals()[f'lane{argv[1]}']()
    print('U9 lane ' + argv[1] + ': ' + json.dumps(report, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
