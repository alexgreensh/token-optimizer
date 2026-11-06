#!/usr/bin/env python3
"""L5: upgrading a live BASE install to the candidate, without cleanup.

Scenarios, each in a fresh synthetic home on this OS:

  side-by-side   Claude Code style: BASE stays in its cache/<version> dir with
                 its __pycache__, the candidate lands in a new version dir.
                 Then the old dir is removed while the session still points at
                 it, so the hook command's newest-version fallback must find
                 the candidate.
  overlay        manual / git-pull style: candidate files are copied over the
                 BASE install; stale __pycache__ and files are left in place.

In both, a BASE process stays running across the upgrade (BASE's own
generated dashboard daemon script, served from the synthetic home), BASE's
dashboard numbers are read twice before the upgrade and the candidate's right
after it, from the same history, and must be equal (numeric leaves, pinned
clock, U0's deterministic probe). Every hook runs clean before and after, and
the candidate run must load the whole `_measure` package from the new code.

--real-home-daemon (GitHub runners only) adds the service-manager check:
BASE's `setup-daemon`, upgrade, candidate SessionStart, and the daemon must
come back serving the candidate version from the candidate's code.

Known-bad control (TEST-PLAN lists none for L5; added here): the overlay
upgrade with one `_measure` provider deleted must be caught by the same
after-upgrade checks.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifacts as a  # noqa: E402
import common as c  # noqa: E402

SESSION_EVENTS = ('SessionStart', 'UserPromptSubmit', 'PostToolUse', 'PreCompact')
NUMBER_APIS = ('_get_savings_summary', '_collect_trends_data')
DAEMON_PORT = 24842
VICTIM = 'pricing.py'


def session(root, home, project, case, live_dir, session_id):
    """One host session's hooks, exactly as configured in that install."""
    runs = []
    for hook in c.configured_hooks(Path(root) / 'hooks/hooks.json', SESSION_EVENTS):
        env = c.product_env(home, case, root, liveness=live_dir)
        stdin = c.hook_stdin(hook['event'], home, project, 'claude', session=session_id)
        result = c.run_hook(hook['command'], env, project, stdin, timeout=180)
        runs.append({'event': hook['event'], **c.brief(result), 'clean': c.clean_run(result)})
    return runs


def numbers(root, home, case, tag):
    """Dashboard data through U0's pinned probe; only numeric leaves count."""
    env = c.product_env(home, case, root)
    out = {}
    for name in NUMBER_APIS:
        effects = case / f'effects-{tag}-{name}.json'
        result = c.run([sys.executable, str(c.SCRIPTS / 'measure_split_probe.py'), str(root), str(effects),
                        'api', name], env, case, timeout=300)
        if result['exit'] != 0:
            raise RuntimeError(f'{name} failed: {c.brief(result)}')
        out[name] = flatten(json.loads(result['stdout']))
    return out


def flatten(value, prefix=''):
    if isinstance(value, dict):
        return {k: v for key in sorted(value) for k, v in flatten(value[key], f'{prefix}/{key}').items()}
    if isinstance(value, list):
        return {k: v for i, item in enumerate(value) for k, v in flatten(item, f'{prefix}[{i}]').items()}
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return {}
    return {prefix: value}


def diff_numbers(before, after):
    errors = []
    for api in NUMBER_APIS:
        left, right = before[api], after[api]
        errors += [f'{api}{k}: {left.get(k)!r} -> {right.get(k)!r}'
                   for k in sorted(set(left) | set(right)) if left.get(k) != right.get(k)]
    return errors


def served_version(timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{DAEMON_PORT}/api/health', timeout=2) as response:
                data = json.loads(response.read(4096))
            if data.get('server') == 'token-optimizer-daemon':
                return data.get('version')
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    return None


_DAEMON_SCRIPT = '''
import sys
sys.path.insert(0, sys.argv[1])
import measure
measure._get_or_create_daemon_token()
open(sys.argv[2], 'w', encoding='utf-8').write(measure._generate_daemon_script())
'''


def start_old_daemon(root, home, case, project):
    """BASE's own generated daemon script, running across the upgrade."""
    env = c.product_env(home, case, root)
    script = home / 'data/dashboard-server.py'
    script.parent.mkdir(parents=True, exist_ok=True)
    rendered = c.run([sys.executable, str(Path(root) / c.FACADE), 'dashboard', '--quiet'], env, project, timeout=300)
    if rendered['exit'] != 0:
        return None, {'dashboard': c.brief(rendered)}
    made = c.run([sys.executable, '-c', _DAEMON_SCRIPT, str(Path(root) / 'skills/token-optimizer/scripts'),
                  str(script)], env, project)
    if made['exit'] != 0:
        return None, {'generate': c.brief(made)}
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    proc = subprocess.Popen([sys.executable, str(script)], cwd=project, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    return proc, {'version': served_version(30)}


def scenario(name, trees, work, *, damage=False):
    case = c.case_dir(work / name)
    home, project = c.fresh_case(case)
    base_version, cand_version = c.plugin_version(trees['base']), c.plugin_version(trees['candidate'])
    base_root = a.install_claude_plugin(trees['base'], home)['root']
    checks, record = [], {'scenario': name}
    before = session(base_root, home, project, case, work / name / 'live-before', 'fixture-session')
    live_before = c.liveness_summary(c.read_liveness(work / name / 'live-before'), base_root)
    checks.append(c.check(f'{name}/base-session', c.PASS if all(r['clean'] for r in before) else c.CANNOT,
                          [r for r in before if not r['clean']][:4] or f'{len(before)} hooks clean'))
    stale = sorted(p.relative_to(base_root).as_posix() for p in base_root.rglob('*.pyc'))
    record['stale_bytecode_files'] = len(stale)
    record['settle_seconds'] = settle(base_root)
    daemon, daemon_info = start_old_daemon(base_root, home, case, project)
    record['old_daemon'] = daemon_info
    try:
        first, second = numbers(base_root, home, case, 'base-1'), numbers(base_root, home, case, 'base-2')
        if name == 'side-by-side':
            directory = cand_version if cand_version != base_version else c.next_patch(base_version)
            record['candidate_dir'] = directory
            record['candidate_dir_synthetic'] = directory != cand_version
            new_root = a.install_claude_plugin(trees['candidate'], home, version=directory)['root']
        else:
            c.copy_tree(trees['candidate'], base_root, ignore_pycache=True)
            new_root = base_root
        if damage:
            (new_root / c.SPLIT_PACKAGE / VICTIM).unlink()
        try:
            after_numbers, numbers_error = numbers(new_root, home, case, 'candidate'), None
        except RuntimeError as error:
            after_numbers, numbers_error = None, str(error)[:2000]
        after = session(new_root, home, project, case, work / name / 'live-after', 'fixture-session-upgraded')
        live_after = c.liveness_summary(c.read_liveness(work / name / 'live-after'), new_root)
        alive = c.candidate_live(live_after, c.expected_modules(trees['candidate']))
        noise = diff_numbers(first, second)
        drift = [numbers_error] if numbers_error else diff_numbers(second, after_numbers)
        checks.append(c.check(f'{name}/numbers', c.CANNOT if noise else (c.FAIL if drift else c.PASS),
                              {'base_self': noise[:10]} if noise else (drift[:20] or
                              f"{sum(len(v) for v in second.values())} numbers equal")))
        checks.append(c.check(f'{name}/upgraded-session', c.PASS if all(r['clean'] for r in after) else c.FAIL,
                              [r for r in after if not r['clean']][:4] or f'{len(after)} hooks clean'))
        checks.append(c.check(f'{name}/candidate-live', c.PASS if alive else c.FAIL,
                              {'before': live_before, 'after': live_after}))
        if name == 'side-by-side' and not damage:
            moved = base_root.with_name(base_root.name + '.removed-by-host')
            try:
                base_root.rename(moved)
            except OSError as error:
                checks.append(c.check(f'{name}/removed-old-root-fallback', c.CANNOT,
                                      'old dir could not be removed (open handles): ' + repr(error)))
                return finish(checks, record, daemon, daemon_info, name)
            stale_dir = work / name / 'live-stale-root'
            orphan = session_with_root(base_root, new_root, home, project, case, stale_dir)
            live_orphan = c.liveness_summary(c.read_liveness(stale_dir), new_root)
            ok = all(r['clean'] for r in orphan) and c.candidate_live(live_orphan, c.expected_modules(trees['candidate']))
            detail = {'runs': [r for r in orphan if not r['clean']][:4], 'liveness': live_orphan}
            status = c.PASS if ok else c.FAIL
            if not ok and all(r['clean'] for r in orphan) and not live_orphan['processes']:
                # Nothing ran at all: the shell fallback found no version dir.
                # The hook text is unchanged from BASE, so judge against BASE.
                detail['base_control'] = fallback_at_base(trees, work)
                if detail['base_control'].get('processes') == 0:
                    status = c.PASS
                    detail['finding'] = ('pre-existing, identical at BASE: with the old version dir '
                                         'removed, the hook command exits 0 without running Python')
            checks.append(c.check(f'{name}/removed-old-root-fallback', status, detail))
        return finish(checks, record, daemon, daemon_info, name)
    finally:
        if daemon is not None and daemon.poll() is None:
            daemon.terminate()
            daemon.wait(timeout=30)


def finish(checks, record, daemon, daemon_info, name):
    """The BASE process that was running before the upgrade still serves."""
    if daemon is None or not daemon_info.get('version'):
        checks.append(c.check(f'{name}/running-base-daemon', c.CANNOT, daemon_info))
    else:
        survived = daemon.poll() is None and served_version(10) == daemon_info['version']
        checks.append(c.check(f'{name}/running-base-daemon', c.PASS if survived else c.FAIL,
                              {'served_before': daemon_info['version'], 'alive_after': daemon.poll() is None}))
    return checks, record


def settle(root, quiet=10.0, limit=120.0):
    """Hooks leave detached work behind (bytecode warm-up, regen). Wait for the
    compile lock to clear, then a fixed quiet window, before reading numbers."""
    start = time.monotonic()
    lock = Path(root) / 'skills/token-optimizer/scripts/__pycache__/.token-optimizer-compile.lock'
    while lock.exists() and time.monotonic() - start < limit:
        time.sleep(0.2)
    time.sleep(quiet)
    return round(time.monotonic() - start, 2)


def fallback_at_base(trees, work):
    """The removed-old-root scenario with BASE as both versions."""
    case = c.case_dir(work / 'fallback-base')
    home, project = c.fresh_case(case)
    version = c.plugin_version(trees['base'])
    old = a.install_claude_plugin(trees['base'], home)['root']
    new = a.install_claude_plugin(trees['base'], home, version=c.next_patch(version))['root']
    old.rename(old.with_name(old.name + '.removed-by-host'))
    live_dir = work / 'fallback-base' / 'live'
    runs = session_with_root(old, new, home, project, case, live_dir)
    summary = c.liveness_summary(c.read_liveness(live_dir), new)
    return {'clean': all(r['clean'] for r in runs), 'processes': summary['processes'],
            'reached_new_dir': bool(summary['facade_reads']) and not summary['outside_install']}


def session_with_root(stale_root, new_root, home, project, case, live_dir):
    """The session's env still names the removed dir; commands come from the
    new install's hooks.json (identical text), so only the fallback differs."""
    runs = []
    for hook in c.configured_hooks(Path(new_root) / 'hooks/hooks.json', SESSION_EVENTS):
        env = c.product_env(home, case, stale_root, liveness=live_dir)
        stdin = c.hook_stdin(hook['event'], home, project, 'claude', session='fixture-session-orphan')
        result = c.run_hook(hook['command'], env, project, stdin, timeout=180)
        runs.append({'event': hook['event'], **c.brief(result), 'clean': c.clean_run(result)})
    return runs


_WHERE = '''
import json, sys
sys.path.insert(0, sys.argv[1])
import measure
print(json.dumps([str(measure.SNAPSHOT_DIR / "dashboard-server.py"),
                  str(measure.RUNTIME_DIR / "_backups" / "token-optimizer" / "dashboard-server.py")]))
'''


def real_home_daemon(trees, work, bump):
    """Service-manager restart on new code. Runner's real home, CI only."""
    c.require_ci_runner()
    home = Path.home()
    base_version, cand_version = c.plugin_version(trees['base']), c.plugin_version(trees['candidate'])
    base_root = a.install_claude_plugin(trees['base'], home)['root']
    env = {k: v for k, v in os.environ.items() if k not in ('TOKEN_OPTIMIZER_SNAPSHOT_DIR', 'CLAUDE_CONFIG_DIR')}
    env.update({'TOKEN_OPTIMIZER_RUNTIME': 'claude', 'TOKEN_OPTIMIZER_REMOTE_LEAN': '0',
                'TOKEN_OPTIMIZER_PYTHON': Path(sys.executable).as_posix() if os.name == 'nt' else sys.executable,
                'TOKEN_OPTIMIZER_COLD_COMPILE_WAIT_S': '60', 'PYTHONUTF8': '1'})
    record = {'home': str(home)}
    project = work / 'real-project'
    project.mkdir(parents=True, exist_ok=True)
    setup = c.run([sys.executable, str(base_root / c.FACADE), 'setup-daemon'], {**env, 'CLAUDE_PLUGIN_ROOT': str(base_root)},
                  project, timeout=300)
    record['setup'] = c.brief(setup)
    record['served_before'] = served_version(90)
    try:
        if record['served_before'] != base_version:
            return c.check('real-daemon-restart', c.CANNOT, record), record
        target = cand_version
        if cand_version == base_version:
            if not bump:
                record['why'] = 'candidate version equals BASE; restart is version-keyed'
                return c.check('real-daemon-restart', c.CANNOT, record), record
            target = c.next_patch(base_version)
        new_root = a.install_claude_plugin(trees['candidate'], home, version=target)['root']
        if target != cand_version:
            manifest = new_root / '.claude-plugin/plugin.json'
            data = json.loads(manifest.read_text(encoding='utf-8'))
            data['version'] = target
            manifest.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
            record['simulated_release_bump'] = f'{cand_version} -> {target} (plugin.json only)'
        hook = next(h for h in c.configured_hooks(new_root / 'hooks/hooks.json', ('SessionStart',)))
        stdin = c.hook_stdin('SessionStart', home, project, 'claude', session='real-upgraded-session')
        run = c.run_hook(hook['command'], {**env, 'CLAUDE_PLUGIN_ROOT': str(new_root)}, project, stdin, timeout=180)
        record['sessionstart'] = c.brief(run)
        served = None
        end = time.monotonic() + 180
        while time.monotonic() < end and served != target:
            served = served_version(5)
        record['served_after'] = served
        where = c.run([sys.executable, '-c', _WHERE, str(new_root / 'skills/token-optimizer/scripts')],
                      {**env, 'CLAUDE_PLUGIN_ROOT': str(new_root)}, project, timeout=120)
        try:
            candidates = [Path(x) for x in json.loads(where['stdout'].strip().splitlines()[-1])]
        except ValueError:
            candidates, record['where'] = [], c.brief(where)
        scripts = [p for p in candidates if p.is_file()]
        texts = [p.read_text(encoding='utf-8', errors='replace') for p in scripts]
        record['daemon_script_candidates'] = [str(p) for p in candidates]
        record['daemon_scripts'] = [str(p) for p in scripts]
        facade = str((new_root / c.FACADE).resolve())
        new_code = any(f'TOKEN_OPTIMIZER_DAEMON_VERSION = "{target}"' in t and (repr(facade) in t or facade in t)
                       for t in texts)
        record['script_points_at_candidate'] = new_code
        ok = served == target and new_code and c.clean_run(run)
        return c.check('real-daemon-restart', c.PASS if ok else c.FAIL, record), record
    finally:
        latest = sorted((home / '.claude/plugins/cache' / a.MARKETPLACE / 'token-optimizer').glob('*/' + c.FACADE))
        if latest:
            record['uninstall'] = c.brief(c.run([sys.executable, str(latest[-1]), 'setup-daemon', '--uninstall'],
                                                env, project, timeout=300))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--base-ref', default=c.DEFAULT_BASE)
    parser.add_argument('--candidate-ref', required=True)
    parser.add_argument('--work', type=Path, default=c.WORK_BASE / 'L5')
    parser.add_argument('--real-home-daemon', action='store_true')
    parser.add_argument('--no-bump', action='store_true', help='never simulate a release bump')
    args = parser.parse_args(argv)
    work = args.work.resolve()
    if not work.is_relative_to(c.WORK_BASE.resolve()):
        parser.error('--work must be inside ' + str(c.WORK_BASE))
    if args.real_home_daemon:
        c.require_ci_runner()
    c.remove_tree(work)
    sha = {'base': c.resolve_ref(args.base_ref), 'candidate': c.resolve_ref(args.candidate_ref)}
    trees = {label: c.materialize(value, work / 'trees' / label) for label, value in sha.items()}
    checks, records = [], {}
    for name in ('side-by-side', 'overlay'):
        print('== ' + name, flush=True)
        try:
            found, records[name] = scenario(name, trees, work)
        except Exception as error:
            found, records[name] = [c.check(name, c.CANNOT, repr(error))], {'trace': traceback.format_exc()[-3000:]}
        checks += found
        for item in found:
            print(f"   {item['name']}: {item['status']}", flush=True)
    if args.real_home_daemon:
        found, records['real-daemon'] = real_home_daemon(trees, work, bump=not args.no_bump)
        checks.append(found)
    else:
        checks.append(c.check('real-daemon-restart', c.CANNOT, 'not requested (--real-home-daemon, CI only)'))
    print('== known-bad control', flush=True)
    try:
        bad_checks, records['known-bad'] = scenario('overlay', trees, work / 'known-bad', damage=True)
    except Exception as error:
        bad_checks, records['known-bad'] = [], {'error': repr(error), 'trace': traceback.format_exc()[-3000:]}
    after = [x for x in bad_checks if x['name'].endswith(('upgraded-session', 'candidate-live'))]
    control = {'status': 'not-run' if not after else ('caught' if any(x['status'] == c.FAIL for x in after)
                                                       else 'missed'),
               'deleted': VICTIM, 'checks': after}
    lives = [x for x in checks if x['name'].endswith('candidate-live')]
    live = {'proven': bool(lives) and all(x['status'] == c.PASS for x in lives)}
    verdict, reasons = c.layer_verdict(checks, control, live)
    result = {'layer': 'L5', 'os': platform.system(), 'verdict': verdict, 'reasons': reasons,
              'base_ref': args.base_ref, 'candidate_ref': args.candidate_ref,
              'base_sha': sha['base'], 'candidate_sha': sha['candidate'],
              'base_version': c.plugin_version(trees['base']),
              'candidate_version': c.plugin_version(trees['candidate']),
              'runner': c.runner_info(), 'control': control, 'liveness': live, 'checks': checks,
              'scenarios': records}
    c.write_result(work, 'L5', result)
    return 0 if verdict == c.PASS else 1


if __name__ == '__main__':
    raise SystemExit(c.guarded('L5', main)())
