#!/usr/bin/env python3
"""L4: installed artifacts behave like the BASE install of each artifact.

For every artifact (Claude Code plugin cache, npm tarball, Codex mirror, Cowork
committed plugin, Devin closed set), on this OS:

1. build + install BASE and candidate into separate clean synthetic homes;
2. contents: every `_measure/*.py` of the commit is installed byte-for-byte;
3. real argv: the host's configured hook command strings (SessionStart,
   UserPromptSubmit, PostToolUse, PreCompact, or the artifact's own set) plus
   `measure.py --version | quick | dashboard --quiet` run through bash and
   Python exactly as installed, with no import error or traceback, and with
   the same exit status as BASE;
4. pinned equivalence: the same entries run through U0's deterministic golden
   capture against each installed tree; BASE twice (must be identical, or the
   artifact is CANNOT-MEASURE), then candidate vs BASE byte for byte;
5. liveness: the candidate's processes loaded `_measure` from inside its
   install, BASE's never did.

Known-bad control (TEST-PLAN): a copy of each candidate artifact with one
`_measure` file deleted must FAIL the same contents check, and the runtime
check on the Claude plugin copy is recorded too.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifacts as a  # noqa: E402
import common as c  # noqa: E402
from measure_split_artifacts import compare_candidate  # noqa: E402  (U0)

ARTIFACTS = ('claude-plugin', 'npm', 'codex', 'cowork', 'devin')
# U0's probe fakes webbrowser but not os.startfile, so on Windows a plain
# `dashboard` launches a real Edge into the fixture home. --quiet does not open.
CLI_ROWS = ('cli-quick', 'cli-dashboard---quiet' if os.name == 'nt' else 'cli-dashboard')
# BASE's devin-filter crashes on Windows (signal.SIGPIPE): the adapter is POSIX-only.
DEVIN_ROWS = ('cli-devin-install', 'cli-devin-doctor---json') + (() if os.name == 'nt' else ('cli-devin-filter---help',))
VICTIM = 'pricing.py'


def golden_rows(ids):
    rows = {row['id']: row for row in json.loads(c.golden.CASES.read_text())['cases']}
    return [rows[i] for i in ids]


def pinned_rows(install):
    """Hook rows from the artifact's own canonical hooks.json, run pinned."""
    rows = []
    canonical = install['root'] / 'hooks/hooks.json'
    for index, hook in enumerate(c.configured_hooks(canonical, install['events'])):
        if not hook['argv']:
            continue
        rows.append({'id': f"hook-{index:02d}-{hook['event']}", 'entry': 'hook', 'argv': hook['argv'],
                     'runtime': install['runtime'], 'fixture': 'synthetic-current', 'stdin_base64': '',
                     'hook_event': hook['event'], 'input_variant': 'success', 'host_variant': install['host'],
                     'timeout': 120})
    for row in golden_rows(CLI_ROWS):
        rows.append({**row, 'runtime': install['runtime']})
    return rows


class Arm:
    """One artifact installed from one commit into its own synthetic home."""

    def __init__(self, label, tree, work):
        self.label, self.tree, self.work = label, tree, work
        self.case = c.case_dir(work)
        self.home, self.project = c.fresh_case(self.case)

    def env(self, install, liveness=None, runtime=None):
        env = c.product_env(self.home, self.case, install['root'], runtime=runtime or install['runtime'],
                            liveness=liveness)
        env.update(install.get('extra_env', {}))
        if install['host'] == 'cowork':
            env['TOKEN_OPTIMIZER_REMOTE_HOST'] = 'cowork'
        return env


def install(artifact, arm, sha, npm_cache):
    plain = c.product_env(arm.home, arm.case, arm.case)  # installer env: no plugin root yet
    if artifact in ('claude-plugin', 'devin'):
        result = a.install_claude_plugin(arm.tree, arm.home)
    elif artifact == 'cowork':
        result = a.install_cowork(arm.tree, arm.home)
    elif artifact == 'codex':
        result = a.install_codex(arm.tree, arm.home, c.product_env(arm.home, arm.case, arm.case, runtime='codex'),
                                 arm.project)
    else:
        if sha not in npm_cache:
            npm_cache[sha] = a.build_npm_tarball(sha, arm.work / 'build')
        tarball, package = npm_cache[sha]
        result = a.install_npm(package, arm.home, plain, arm.project)
        result['tarball'] = tarball.name
    if artifact == 'devin':
        # The Devin adapter is POSIX-only (os.geteuid, O_NOFOLLOW); on Windows
        # only the pinned capture compares BASE and candidate behaviour.
        result['devin'] = (None if os.name == 'nt' else
                           a.install_devin(result['root'], arm.home, arm.env(result), arm.project))
    return result


def checkpoints(home):
    return sum(1 for p in Path(home).rglob('*.md') if 'checkpoints' in p.parts)


def real_runs(arm, inst, artifact):
    """Configured commands and the public CLI, exactly as installed."""
    runs, live_dir = [], arm.work / 'liveness'
    if artifact == 'devin':
        return runs, live_dir
    for index, hook in enumerate(c.configured_hooks(inst['config'], inst['events'])):
        env = arm.env(inst, liveness=live_dir)
        env.update(hook['env'])
        stdin = c.hook_stdin(hook['event'], arm.home, arm.project, inst['runtime'])
        shell = 'cmd' if os.name == 'nt' and inst['host'] == 'codex' else 'bash'
        before = checkpoints(arm.home)
        result = c.run_hook(hook['command'], env, arm.project, stdin, timeout=180, shell=shell)
        record = {'key': f"{index:02d}:{hook['event']}:{hook['matcher']}", **c.brief(result),
                  'clean': c.clean_run(result)}
        if 'compact-capture' in (hook['argv'] or []) or 'compact-capture' in hook['command']:
            # A PreCompact save that returns without persisting loses the restore.
            record['checkpoint_written'] = checkpoints(arm.home) > before
        runs.append(record)
    facade = inst['root'] / c.FACADE
    for argv in (['--version'], ['quick'], ['dashboard', '--quiet']):
        result = c.run([sys.executable, str(facade), *argv], arm.env(inst, liveness=live_dir), arm.project,
                       timeout=300)
        runs.append({'key': 'cli:' + ' '.join(argv), **c.brief(result), 'clean': c.clean_run(result)})
    return runs, live_dir


def pinned(arms, insts, artifact, work):
    """U0 golden capture over each installed tree, BASE twice then candidate."""
    rows = golden_rows(DEVIN_ROWS) if artifact == 'devin' else pinned_rows(insts['base'])
    if artifact != 'devin' and rows != pinned_rows(insts['candidate']):
        return c.check('pinned-equivalence', c.FAIL, 'configured hook entries differ from BASE')
    out = work / 'pinned'
    c.remove_tree(out)
    for label, name in (('base', 'base-1'), ('base', 'base-2'), ('candidate', 'candidate')):
        try:
            c.golden.capture(insts[label]['root'], out / 'fixed-path', out / name, rows)
        except Exception as error:
            done = sorted((p for p in (out / name).iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
            last = done[-1] if done else None
            tail = {f: (last / f).read_bytes()[-1500:].decode('utf-8', 'replace')
                    for f in ('stdout', 'stderr', 'exit') if last and (last / f).is_file()}
            return c.check('pinned-equivalence', c.CANNOT, f'{name}: {error!r}'[:1500],
                           last_case=last.name if last else None, last_case_output=tail)
    noise = c.golden.compare(out / 'base-1', out / 'base-2')
    if noise:
        return c.check('pinned-equivalence', c.CANNOT, {'base_self_differences': noise[:20]}, cases=len(rows))
    if artifact == 'devin':
        errors, identities = compare_candidate(out / 'base-1', out / 'candidate', arms['base'].tree,
                                               arms['candidate'].tree)
    else:
        errors, identities = c.golden.compare(out / 'base-1', out / 'candidate'), []
    return c.check('pinned-equivalence', c.FAIL if errors else c.PASS, errors[:40] or f'{len(rows)} cases identical',
                   cases=len(rows), verified_source_artifacts=identities)


def evaluate(artifact, arms, sha_for, npm_cache, work):
    checks, insts, runs, live = [], {}, {}, {}
    for label, arm in arms.items():
        try:
            insts[label] = install(artifact, arm, sha_for[label], npm_cache)
        except a.ArtifactUnavailable as error:
            return {'checks': [c.check('install', c.CANNOT, f'{label}: {error}')], 'live': None}
        except Exception as error:
            return {'checks': [c.check('install', c.FAIL, f'{label}: {error!r}')], 'live': None,
                    'trace': traceback.format_exc()[-3000:]}
    checks.append(c.check('install', c.PASS, {k: v['mode'] if 'mode' in v else 'devin-install'
                                              for k, v in insts.items()}))
    cand_root = insts['candidate']['root']
    if artifact == 'devin':
        if insts['candidate']['devin'] is not None:
            devin = insts['candidate']['devin']['root']
            checks.append(a.devin_closed_set(devin, arms['candidate'].tree, 'contents'))
            checks.append(a.devin_refusal(devin, cand_root, arms['candidate'].env(insts['candidate']),
                                          arms['candidate'].project))
    else:
        checks.append(c.contents_check('contents', cand_root, arms['candidate'].tree))
        for label, arm in arms.items():
            runs[label], live_dir = real_runs(arm, insts[label], artifact)
            live[label] = c.liveness_summary(c.read_liveness(live_dir), insts[label]['root'])
        dirty = [r for label in runs for r in runs[label] if not r['clean']]
        checks.append(c.check('real-argv', c.FAIL if dirty else c.PASS, dirty[:6] or f"{len(runs['candidate'])} runs per arm"))
        parity = [(b['key'], b['exit'], k['exit'], b.get('checkpoint_written'), k.get('checkpoint_written'))
                  for b, k in zip(runs['base'], runs['candidate'])
                  if b['key'] != k['key'] or b['exit'] != k['exit']
                  or b.get('checkpoint_written') != k.get('checkpoint_written')]
        if len(runs['base']) != len(runs['candidate']):
            parity.append(('run count', len(runs['base']), len(runs['candidate'])))
        checks.append(c.check('exit-parity', c.FAIL if parity else c.PASS, parity or 'identical exit codes'))
        alive = (c.candidate_live(live['candidate'], c.expected_modules(arms['candidate'].tree))
                 and c.base_is_base(live['base']))
        checks.append(c.check('liveness', c.PASS if alive else c.CANNOT, live))
    try:
        checks.append(pinned(arms, insts, artifact, work))
    except Exception as error:
        checks.append(c.check('pinned-equivalence', c.CANNOT, repr(error), trace=traceback.format_exc()[-3000:]))
    return {'checks': checks, 'installs': {k: {kk: str(vv) for kk, vv in v.items()} for k, v in insts.items()},
            'runs': runs, 'live': live.get('candidate'), 'alive': artifact != 'devin' and alive,
            'cand_root': cand_root}


def known_bad(results, arms, work):
    """Delete one `_measure` file from each candidate artifact copy: the
    contents check must go red, and the Claude plugin copy is run for real."""
    outcomes = {}
    for artifact, result in results.items():
        root = result.get('cand_root')
        if artifact == 'devin' or root is None:
            continue
        bad = c.case_dir(work / 'known-bad' / artifact).parent / 'install'
        c.remove_tree(bad)
        c.copy_tree(root, bad)
        victim = bad / c.SPLIT_PACKAGE / VICTIM
        if not victim.is_file():
            outcomes[artifact] = {'status': 'not-run', 'detail': 'no ' + VICTIM + ' to delete'}
            continue
        victim.unlink()
        verdict = c.contents_check('contents', bad, arms['candidate'].tree)
        outcomes[artifact] = {'status': 'caught' if verdict['status'] == c.FAIL else 'missed',
                              'detail': verdict['detail']}
        if artifact == 'claude-plugin':
            arm = arms['candidate']
            inst = {'root': bad, 'config': bad / 'hooks/hooks.json', 'events': a.CLAUDE_EVENTS,
                    'runtime': 'claude', 'host': 'claude'}
            env = arm.env(inst)
            result = c.run([sys.executable, str(bad / c.FACADE), 'quick'], env, arm.project, timeout=300)
            outcomes[artifact]['runtime_detected'] = not c.clean_run(result)
            outcomes[artifact]['runtime'] = c.brief(result)
    statuses = [o['status'] for o in outcomes.values()]
    status = 'missed' if 'missed' in statuses else ('caught' if statuses and set(statuses) == {'caught'} else 'not-run')
    return {'status': status, 'victim': VICTIM, 'artifacts': outcomes}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--base-ref', default=c.DEFAULT_BASE)
    parser.add_argument('--candidate-ref', required=True)
    parser.add_argument('--work', type=Path, default=c.WORK_BASE / 'L4')
    parser.add_argument('--artifact', action='append', choices=ARTIFACTS)
    args = parser.parse_args(argv)
    work = args.work.resolve()
    if not work.is_relative_to(c.WORK_BASE.resolve()):
        parser.error('--work must be inside ' + str(c.WORK_BASE))
    c.remove_tree(work)
    sha_for = {'base': c.resolve_ref(args.base_ref), 'candidate': c.resolve_ref(args.candidate_ref)}
    trees = {label: c.materialize(sha, work / 'trees' / label) for label, sha in sha_for.items()}
    results, npm_cache = {}, {}
    for artifact in args.artifact or ARTIFACTS:
        print(f'== {artifact}', flush=True)
        arms = {label: Arm(label, trees[label], work / artifact / label) for label in trees}
        results[artifact] = evaluate(artifact, arms, sha_for, npm_cache, work / artifact)
        for item in results[artifact]['checks']:
            print(f"   {item['name']}: {item['status']}", flush=True)
    control = known_bad(results, {'candidate': Arm('candidate', trees['candidate'], work / 'known-bad-arm')}, work)
    checks = [dict(item, name=f"{artifact}/{item['name']}") for artifact, result in results.items()
              for item in result['checks']]
    lives = [r.get('alive') for k, r in results.items() if k != 'devin']
    live = {'proven': bool(lives) and all(lives),
            'per_artifact': {k: r['live'] for k, r in results.items()}}
    verdict, reasons = c.layer_verdict(checks, control, live)
    result = {'layer': 'L4', 'os': platform.system(), 'verdict': verdict, 'reasons': reasons,
              'base_ref': args.base_ref, 'candidate_ref': args.candidate_ref,
              'base_sha': sha_for['base'], 'candidate_sha': sha_for['candidate'],
              'base_version': c.plugin_version(trees['base']),
              'candidate_version': c.plugin_version(trees['candidate']),
              'runner': c.runner_info(), 'control': control, 'liveness': live, 'checks': checks,
              'artifacts': {k: {kk: vv for kk, vv in v.items() if kk not in ('checks', 'cand_root', 'alive')}
                            for k, v in results.items()}}
    c.write_result(work, 'L4', result)
    return 0 if verdict == c.PASS else 1


if __name__ == '__main__':
    raise SystemExit(c.guarded('L4', main)())
