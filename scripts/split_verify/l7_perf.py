#!/usr/bin/env python3
"""L7: paired performance of the installed Claude Code plugin, BASE vs candidate.

BASE and candidate are installed side by side in their own synthetic homes and
sampled interleaved (pair i runs back to back, order alternating), n pairs each:

  warm hooks     PostToolUse runner, UserPromptSubmit runner, PreCompact
                 `measure.py dynamic-compact-instructions`, through the real
                 configured bash command. p95 bar +5%, hard fail above +10%.
  CLI quick      `python measure.py quick`, script mode as users run it. Same bar.
  cold start     SessionStart as the first hook after install, every __pycache__
                 removed, the product's own cold-compile wait. Wall time and time
                 until bytecode is ready, p50 bar +20%.
  peak RSS       largest peak RSS of any Python process in the PreCompact hook,
                 p95 bar +10%.

Timed samples never load the liveness probe. Separate liveness runs (the RSS
runs) prove the candidate arm loaded `_measure` from its own install and BASE
never did. Known-bad control: a copy of the candidate with a sleep planted in
`_measure/__init__.py` (half of BASE's warm PreCompact p50, at least 250 ms)
must FAIL the same warm-hook rule.
"""
from __future__ import annotations

import argparse
import importlib.util
import platform
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifacts as a  # noqa: E402
import common as c  # noqa: E402
import stats  # noqa: E402

HOOKS = {
    'posttooluse': ('PostToolUse', 'hooks/posttooluse_runner.py'),
    'userpromptsubmit': ('UserPromptSubmit', 'hooks/userpromptsubmit_runner.py'),
    'precompact-measure': ('PreCompact', 'dynamic-compact-instructions'),
    'sessionstart': ('SessionStart', 'hooks/sessionstart_runner.py'),
}
WARM_BAR, WARM_FAIL, COLD_BAR, RSS_BAR = 0.05, 0.10, 0.20, 0.10
PLANTED_SLEEP = 0.25
COMPILE_LOCK = '.token-optimizer-compile.lock'


class PerfArm:
    def __init__(self, label, tree, work, plant_sleep=0.0):
        self.label, self.work = label, work
        self.case = c.case_dir(work)
        self.home, self.project = c.fresh_case(self.case)
        self.root = a.install_claude_plugin(tree, self.home)['root']
        if plant_sleep:
            init = self.root / c.SPLIT_PACKAGE / '__init__.py'
            init.write_text(init.read_text(encoding='utf-8') +
                            f'\nimport time as _planted_time\n_planted_time.sleep({plant_sleep:.3f})\n',
                            encoding='utf-8')
        found = c.configured_hooks(self.root / 'hooks/hooks.json', {e for e, _ in HOOKS.values()})
        self.hooks = {}
        for name, (event, needle) in HOOKS.items():
            match = [h for h in found if h['event'] == event and h['argv'] and needle in h['argv']]
            if not match:
                raise RuntimeError(f'{label}: no configured {event} hook with {needle}')
            self.hooks[name] = match[0]

    def env(self, cold_wait=None, liveness=None):
        return c.product_env(self.home, self.case, self.root, cold_wait=cold_wait, liveness=liveness)

    def hook(self, name, env):
        hook = self.hooks[name]
        stdin = c.hook_stdin(hook['event'], self.home, self.project, 'claude')
        return c.run_hook(hook['command'], env, self.project, stdin, timeout=180)

    def quick(self, env):
        return c.run([sys.executable, str(self.root / c.FACADE), 'quick'], env, self.project, timeout=300)

    def warm(self):
        """Compile everything once, the way a user's first session does."""
        env = self.env(cold_wait='120')
        results = [self.hook(name, env) for name in HOOKS for _ in range(2)] + [self.quick(env)]
        bad = [c.brief(r) for r in results if not c.clean_run(r)]
        if bad:
            raise RuntimeError(f'{self.label}: warm-up run failed: {bad[:2]}')
        if not wait_ready(self.root, 180):
            raise RuntimeError(f'{self.label}: bytecode never became ready')

    def drop_bytecode(self):
        """A fresh install has no bytecode. The product's detached compiler
        keeps warming flat scripts after it drops its lock, so wait for writes
        to stop before deleting, and retry a racing delete."""
        quiesce(self.root)
        for _ in range(40):
            try:
                for cache in sorted(self.root.rglob('__pycache__'), reverse=True):
                    c.remove_tree(cache)
            except OSError:
                time.sleep(0.5)
                continue
            if not any(self.root.rglob('__pycache__')):
                return
        raise RuntimeError(f'{self.label}: could not clear bytecode for a cold sample')


def quiesce(root, stable=2.0, limit=120.0):
    """Until no bytecode file under root changed for `stable` seconds."""
    def state():
        found = []
        for path in Path(root).rglob('*.pyc'):
            try:
                info = path.stat()
            except OSError:
                continue
            found.append((str(path), info.st_mtime_ns, info.st_size))
        return sorted(found)
    end, last, since = time.monotonic() + limit, None, time.monotonic()
    while time.monotonic() < end:
        current = state()
        if current != last:
            last, since = current, time.monotonic()
        elif time.monotonic() - since >= stable:
            return True
        time.sleep(0.25)
    return False


def runtime_sources(root):
    """The facade and the package entry. The product's compiler owns the exact
    closed set and drops its lock only after warming all of it."""
    scripts = Path(root) / 'skills/token-optimizer/scripts'
    init = scripts / '_measure/__init__.py'
    return [scripts / 'measure.py'] + ([init] if init.is_file() else [])


def bytecode_ready(root):
    sources = runtime_sources(root)
    lock = sources[0].parent / '__pycache__' / COMPILE_LOCK
    return not lock.exists() and all(Path(importlib.util.cache_from_source(str(s))).is_file() for s in sources)


def wait_ready(root, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if bytecode_ready(root):
            return True
        time.sleep(0.05)
    return False


def interleave(arms, n, sample):
    """Pairs run back to back; the order alternates to cancel drift."""
    values = {label: [] for label in arms}
    errors = {label: [] for label in arms}
    for i in range(n):
        order = list(arms) if i % 2 == 0 else list(reversed(list(arms)))
        for label in order:
            value, error = sample(arms[label])
            values[label].append(value)
            if error:
                errors[label].append(error)
    return values, errors


def timed(run):
    def sample(arm):
        result = run(arm)
        return (result['seconds'], None) if c.clean_run(result) else (None, c.brief(result))
    return sample


def cold_sample(arm):
    arm.drop_bytecode()
    start = time.perf_counter()
    result = arm.hook('sessionstart', arm.env())
    if not wait_ready(arm.root, 240):
        return None, {'cold': 'bytecode not ready within 240 s', **c.brief(result)}
    ready = time.perf_counter() - start
    if not c.clean_run(result):
        return None, c.brief(result)
    return {'wall': result['seconds'], 'ready': ready, 'output': bool(result['stdout'])}, None


def rss_sample(work):
    counter = {'n': 0}

    def sample(arm):
        counter['n'] += 1
        directory = work / 'rss' / f"{arm.label}-{counter['n']:03d}"
        result = arm.hook('precompact-measure', arm.env(liveness=directory))
        summary = c.liveness_summary(c.read_liveness(directory), arm.root)
        if not c.clean_run(result) or not summary['peak_rss_bytes']:
            return None, {**c.brief(result), 'liveness': summary}
        return {'rss': summary['peak_rss_bytes'], 'live': summary}, None
    return sample


def metric(name, values, errors, *, key=None, stat, pass_bar, fail_bar=None, min_pairs):
    base = [v if key is None or v is None else v[key] for v in values['base']]
    cand = [v if key is None or v is None else v[key] for v in values['candidate']]
    verdict = stats.bar_verdict(base, cand, stat=stat, pass_bar=pass_bar, fail_bar=fail_bar, min_pairs=min_pairs)
    if errors['candidate'] and not errors['base']:
        verdict.update(verdict=c.FAIL, why='candidate runs failed where BASE ran clean')
    info = {'p50_base': stats.percentile([v for v in base if v is not None], 50) if any(v is not None for v in base) else None,
            'p50_candidate': stats.percentile([v for v in cand if v is not None], 50) if any(v is not None for v in cand) else None}
    return c.check(name, verdict['verdict'], verdict['why'], stats=verdict, info=info,
                   errors={k: v[:3] for k, v in errors.items()}, samples={'base': base, 'candidate': cand})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--base-ref', default=c.DEFAULT_BASE)
    parser.add_argument('--candidate-ref', required=True)
    parser.add_argument('--work', type=Path, default=c.WORK_BASE / 'L7')
    parser.add_argument('--samples', type=int, default=30)
    parser.add_argument('--control-samples', type=int, default=10)
    args = parser.parse_args(argv)
    work = args.work.resolve()
    if not work.is_relative_to(c.WORK_BASE.resolve()):
        parser.error('--work must be inside ' + str(c.WORK_BASE))
    c.remove_tree(work)
    sha = {'base': c.resolve_ref(args.base_ref), 'candidate': c.resolve_ref(args.candidate_ref)}
    trees = {label: c.materialize(value, work / 'trees' / label) for label, value in sha.items()}
    arms = {label: PerfArm(label, trees[label], work / label) for label in ('base', 'candidate')}
    for arm in arms.values():
        arm.warm()
    n, checks = args.samples, []
    for name in ('posttooluse', 'userpromptsubmit', 'precompact-measure'):
        print('== warm ' + name, flush=True)
        values, errors = interleave(arms, n, timed(lambda arm, name=name: arm.hook(name, arm.env())))
        checks.append(metric('warm-' + name, values, errors, stat='p95', pass_bar=WARM_BAR,
                             fail_bar=WARM_FAIL, min_pairs=n))
    print('== cli quick', flush=True)
    values, errors = interleave(arms, n, timed(lambda arm: arm.quick(arm.env())))
    checks.append(metric('cli-quick', values, errors, stat='p95', pass_bar=WARM_BAR, fail_bar=WARM_FAIL,
                         min_pairs=n))
    print('== peak rss + liveness', flush=True)
    values, errors = interleave(arms, n, rss_sample(work))
    checks.append(metric('peak-rss', values, errors, key='rss', stat='p95', pass_bar=RSS_BAR, min_pairs=n))
    lives = {label: [v['live'] for v in values[label] if v] for label in values}
    expected = c.expected_modules(trees['candidate'])
    live = {'proven': len(lives['candidate']) == n and all(c.candidate_live(s, expected) for s in lives['candidate'])
            and len(lives['base']) == n and all(c.base_is_base(s) for s in lives['base']),
            'candidate_example': lives['candidate'][:1], 'base_example': lives['base'][:1]}
    print('== cold first hook', flush=True)
    values, errors = interleave(arms, n, cold_sample)
    checks.append(metric('cold-first-hook', values, errors, key='wall', stat='p50', pass_bar=COLD_BAR, min_pairs=n))
    checks.append(metric('cold-bytecode-ready', values, errors, key='ready', stat='p50', pass_bar=COLD_BAR,
                         min_pairs=n))
    for arm in arms.values():
        wait_ready(arm.root, 240)
        arm.warm()
    print('== known-bad control (planted sleep)', flush=True)
    warm_p50 = next(x for x in checks if x['name'] == 'warm-precompact-measure')['info']['p50_base'] or 0
    sleep_for = max(PLANTED_SLEEP, 0.5 * warm_p50)
    planted = PerfArm('planted', trees['candidate'], work / 'planted', plant_sleep=sleep_for)
    planted.warm()
    pair = {'base': arms['base'], 'candidate': planted}
    values, errors = interleave(pair, args.control_samples,
                                timed(lambda arm: arm.hook('precompact-measure', arm.env())))
    control_check = metric('control-planted-sleep', values, errors, stat='p95', pass_bar=WARM_BAR,
                           fail_bar=WARM_FAIL, min_pairs=args.control_samples)
    control = {'status': 'caught' if control_check['status'] == c.FAIL else 'missed',
               'planted_seconds': round(sleep_for, 3), 'result': control_check}
    verdict, reasons = c.layer_verdict(checks, control, live)
    result = {'layer': 'L7', 'os': platform.system(), 'verdict': verdict, 'reasons': reasons,
              'base_ref': args.base_ref, 'candidate_ref': args.candidate_ref,
              'base_sha': sha['base'], 'candidate_sha': sha['candidate'], 'samples': n,
              'runner': c.runner_info(), 'control': control, 'liveness': live, 'checks': checks,
              'tier': 'measured on this runner; GitHub-hosted runners are shared, so absolute numbers are indicative'}
    c.write_result(work, 'L7', result)
    return 0 if verdict == c.PASS else 1


if __name__ == '__main__':
    raise SystemExit(c.guarded('L7', main)())
