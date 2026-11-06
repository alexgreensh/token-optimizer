"""Shared plumbing for split verification layers L4, L5 and L7.

Reuses U0's harness instead of copying it: the synthetic fixture, environment,
pinned capture and raw-byte compare (measure_split_golden), the safe archive
extractor (measure_split_ci) and the Devin source-identity check
(measure_split_artifacts).

Nothing here imports Token Optimizer's measure.py in-process. Every product run
is a subprocess whose HOME is asserted to sit inside this checkout's
.scratch-u0 work tree. The one exception, L5's real service-manager daemon
check, is refused unless it runs on a GitHub Actions runner.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import measure_split_golden as golden  # noqa: E402  (U0 harness)
from measure_split_ci import extract  # noqa: E402  (U0 harness)

ROOT = golden.ROOT
WORK_BASE = ROOT / '.scratch-u0' / 'split-verify'
PROBE_SITE = HERE / 'probe_site'
DEFAULT_BASE = '55da0b1179afa98455fa1b1c6e3dd372afa8ec3f'
PASS, FAIL, CANNOT = 'PASS', 'FAIL', 'CANNOT-MEASURE'
SPLIT_PACKAGE = 'skills/token-optimizer/scripts/_measure'
FACADE = 'skills/token-optimizer/scripts/measure.py'
ERROR_MARKERS = ('Traceback (most recent call last)', 'ModuleNotFoundError', 'ImportError',
                 'SyntaxError', 'NameError', 'AttributeError: module')


# ----------------------------------------------------------------- verdicts

def check(name, status, detail=None, **extra):
    if status not in (PASS, FAIL, CANNOT):
        raise ValueError('unknown status: ' + repr(status))
    return {'name': name, 'status': status, 'detail': detail, **extra}


def combine(statuses):
    """FAIL dominates, then CANNOT-MEASURE. An empty set proves nothing."""
    statuses = list(statuses)
    if FAIL in statuses:
        return FAIL
    if not statuses or CANNOT in statuses:
        return CANNOT
    return PASS


def layer_verdict(checks, control, live):
    """One layer, never blended with another.

    control: {'status': 'caught'|'missed'|'not-run', ...}. A known-bad input
    that does not go red means the gate cannot be trusted: FAIL.
    live: {'proven': bool, ...}. An unproven candidate arm means the run may
    have measured the wrong code: CANNOT-MEASURE, never PASS.
    """
    reasons = [c['name'] + ': ' + c['status'] for c in checks if c['status'] != PASS]
    statuses = [c['status'] for c in checks]
    control_status = (control or {}).get('status', 'not-run')
    if control_status == 'missed':
        statuses.append(FAIL)
        reasons.append('known-bad control did not go red')
    elif control_status != 'caught':
        statuses.append(CANNOT)
        reasons.append('known-bad control did not run')
    if not (live or {}).get('proven'):
        statuses.append(CANNOT)
        reasons.append('candidate arm not proven live')
    return combine(statuses), reasons


def write_result(work, layer, result):
    """Receipt JSON plus a short markdown block for the job summary."""
    work.mkdir(parents=True, exist_ok=True)
    path = work / (layer + '-result.json')
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str) + '\n', encoding='utf-8')
    lines = [f"### {layer} on {result.get('os')}: **{result['verdict']}**", '']
    for reason in result.get('reasons', []):
        lines.append('- ' + reason)
    lines.append('')
    lines.append(f"base `{result.get('base_sha', '')[:12]}` candidate `{result.get('candidate_sha', '')[:12]}`")
    (work / (layer + '-summary.md')).write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('\n'.join(lines), flush=True)
    return path


def guarded(layer, main):
    """Run a layer's main(argv); a crash still writes a CANNOT-MEASURE receipt."""
    def wrapper(argv=None):
        import traceback
        try:
            return main(argv)
        except SystemExit:
            raise
        except Exception as error:
            work = WORK_BASE / layer
            write_result(work, layer, {'layer': layer, 'os': platform.system(), 'verdict': CANNOT,
                                       'reasons': ['harness crashed: ' + repr(error)[:600]],
                                       'trace': traceback.format_exc()[-4000:], 'runner': runner_info()})
            return 1
    return wrapper


def runner_info():
    return {'os': platform.system(), 'release': platform.release(), 'machine': platform.machine(),
            'python': sys.version, 'executable': sys.executable,
            'runner_os': os.environ.get('RUNNER_OS'), 'image': os.environ.get('ImageOS'),
            'image_version': os.environ.get('ImageVersion')}


# ------------------------------------------------------------- source trees

def git(*args, cwd=ROOT):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def resolve_ref(ref):
    for name in (ref, 'origin/' + ref):
        proc = subprocess.run(['git', 'rev-parse', '--verify', '--quiet', name + '^{commit}'],
                              cwd=ROOT, capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    raise ValueError('cannot resolve ref: ' + ref)


def materialize(sha, dest):
    """Exactly the tracked tree of one commit, the way a release ships it."""
    if dest.exists():
        raise ValueError('tree already materialized: ' + str(dest))
    archive = dest.parent / (dest.name + '.tar')
    dest.parent.mkdir(parents=True, exist_ok=True)
    git('archive', '--format=tar', '-o', str(archive), sha)
    extract(archive, dest)
    archive.unlink()
    return dest


def plugin_version(tree):
    return json.loads((Path(tree) / '.claude-plugin/plugin.json').read_text(encoding='utf-8'))['version']


def next_patch(version):
    major, minor, patch = (int(part) for part in version.split('.')[:3])
    return f'{major}.{minor}.{patch + 1}'


def split_sources(tree):
    """Every package source the tree's own commit carries, relative paths."""
    package = Path(tree) / SPLIT_PACKAGE
    if not package.is_dir():
        return []
    return sorted(p.relative_to(tree).as_posix() for p in package.rglob('*.py')
                  if '__pycache__' not in p.parts)


def contents_check(name, installed, tree, *, expect_package=True):
    """Every `_measure` source of the commit is installed byte-for-byte, and no
    stale or foreign `.py` sits in the installed package."""
    installed, tree = Path(installed), Path(tree)
    expected = split_sources(tree)
    if expect_package and not expected:
        return check(name, CANNOT, 'source tree has no _measure package to verify')
    errors = []
    for relative in expected:
        target = installed / relative
        if not target.is_file():
            errors.append('missing: ' + relative)
        elif target.read_bytes() != (tree / relative).read_bytes():
            errors.append('bytes differ: ' + relative)
    present = sorted(p.relative_to(installed).as_posix() for p in (installed / SPLIT_PACKAGE).rglob('*.py')
                     if '__pycache__' not in p.parts) if (installed / SPLIT_PACKAGE).is_dir() else []
    errors += ['unexpected: ' + extra for extra in sorted(set(present) - set(expected))]
    if not (installed / FACADE).is_file():
        errors.append('missing: ' + FACADE)
    return check(name, FAIL if errors else PASS, errors or f'{len(expected)} package files verified',
                 files=len(expected))


def tree_digest(root):
    digest = hashlib.sha256()
    root = Path(root)
    for path in sorted(root.rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            digest.update(path.relative_to(root).as_posix().encode() + b'\0')
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def copy_tree(source, dest, *, ignore_pycache=True):
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc') if ignore_pycache else None
    shutil.copytree(source, dest, ignore=ignore, dirs_exist_ok=True)
    return dest


def remove_tree(path):
    def writable(function, target, _error):
        Path(target).chmod(0o700)
        function(target)
    if Path(path).exists():
        shutil.rmtree(path, onerror=writable)


# ------------------------------------------------------------- environments

def in_git_work_tree(path):
    """run.py's own rule: any ancestor holding `.git` moves bytecode to a
    per-user prefix. A real user install is not inside a repo, so neither
    are ours."""
    current = Path(path).resolve()
    while True:
        if (current / '.git').exists():
            return True
        if current.parent == current:
            return False
        current = current.parent


def real_base():
    """Where installed artifacts and their homes live: outside every git work
    tree. CI uses RUNNER_TEMP; TO_SPLIT_VERIFY_HOMES overrides (tests)."""
    raw = os.environ.get('TO_SPLIT_VERIFY_HOMES') or (
        os.environ.get('RUNNER_TEMP') and str(Path(os.environ['RUNNER_TEMP']) / 'split-verify-homes'))
    if not raw:
        raise RuntimeError('installs need a directory outside any git work tree: '
                           'run in CI (RUNNER_TEMP) or set TO_SPLIT_VERIFY_HOMES')
    base = Path(raw).resolve()
    if in_git_work_tree(base):
        raise RuntimeError('install base is inside a git work tree: ' + str(base))
    base.mkdir(parents=True, exist_ok=True)
    return base


def case_dir(work):
    """The synthetic case for a work dir, mirrored under real_base()."""
    relative = Path(work).resolve().relative_to(WORK_BASE.resolve())
    return real_base() / relative / 'case'


def _sandbox_roots():
    roots = [WORK_BASE.resolve()]
    # real_base() creates the base as a side effect of answering -- and walks
    # parents for .git. A read-only check must not touch paths under the real
    # home at all, so the same location is computed as pure path math: no
    # resolve(), no mkdir, no stat (Path.resolve() calls os.path.realpath,
    # which lstat's every component and tripped the home guard on CI).
    raw = os.environ.get('TO_SPLIT_VERIFY_HOMES') or (
        os.environ.get('RUNNER_TEMP') and str(Path(os.environ['RUNNER_TEMP']) / 'split-verify-homes'))
    if raw:
        roots.append(Path(os.path.normpath(raw)))
    return roots


def fresh_case(case_dir):
    """U0's synthetic home and project, never the owner's."""
    remove_tree(case_dir)
    for key in [k for k in _CASE_ENVS if k[1] == str(case_dir)]:
        del _CASE_ENVS[key]
    case_dir.mkdir(parents=True)
    home, project = golden.fixture(case_dir)
    return home, project


def product_env(home, case_dir, plugin_root, *, runtime='claude', cold_wait='60', liveness=None):
    """U0's pinned environment minus the bytecode overrides: installed hooks
    must compile and reuse bytecode exactly as they do for users.

    cold_wait None keeps the product's own cold-compile wait (L7 cold arm).
    """
    env = dict(_case_env(home, case_dir))
    env['CLAUDE_PLUGIN_ROOT'] = str(plugin_root)
    for key in ('PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX', 'TOKEN_OPTIMIZER_COLD_COMPILE_WAIT_S'):
        env.pop(key, None)
    env['TOKEN_OPTIMIZER_RUNTIME'] = runtime
    interpreter = Path(sys.executable)
    env['TOKEN_OPTIMIZER_PYTHON'] = interpreter.as_posix() if os.name == 'nt' else str(interpreter)
    if cold_wait is not None:
        env['TOKEN_OPTIMIZER_COLD_COMPILE_WAIT_S'] = str(cold_wait)
    if runtime == 'codex':
        env.pop('CLAUDE_PLUGIN_ROOT', None)
    if Path(case_dir).resolve().is_relative_to(WORK_BASE.resolve()) is False:
        # U0's probe refuses reads under the account home to protect a
        # developer machine. Installs outside the repo exist only on
        # disposable runners, whose home contains RUNNER_TEMP.
        env['TO_SPLIT_REAL_HOME'] = str(real_base() / '.no-owner-home')
    if liveness is not None:
        enable_liveness(env, liveness)
    assert_sandboxed(env, case_dir)
    return env


_CASE_ENVS = {}


def _case_env(home, case_dir):
    """golden.environment creates the case's tmp dir, so build it once per case."""
    key = (str(home), str(case_dir))
    if key not in _CASE_ENVS:
        _CASE_ENVS[key] = golden.environment(home, Path(case_dir), case_dir)
    return _CASE_ENVS[key]


def enable_liveness(env, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    env['PYTHONPATH'] = str(PROBE_SITE)
    env['TO_SPLIT_VERIFY_LIVENESS'] = str(directory)
    return env


def assert_sandboxed(env, case_dir):
    """Refuse any product run whose home could be the account's real one."""
    case_dir = Path(case_dir).resolve()
    if not any(case_dir.is_relative_to(root) for root in _sandbox_roots()):
        raise RuntimeError('product work outside the split-verify sandboxes: ' + str(case_dir))
    for key in ('HOME', 'USERPROFILE', 'CLAUDE_CONFIG_DIR', 'TOKEN_OPTIMIZER_SNAPSHOT_DIR', 'CODEX_HOME'):
        value = env.get(key)
        if not value or not Path(value).resolve().is_relative_to(case_dir):
            raise RuntimeError(f'{key} is not inside the synthetic case: {value!r}')


def require_ci_runner():
    """The real-home daemon check touches the account's service manager."""
    if os.environ.get('GITHUB_ACTIONS') != 'true' or not os.environ.get('RUNNER_TEMP'):
        raise RuntimeError('real-home mode runs only on a disposable GitHub Actions runner')
    if (Path.home() / '.claude/plugins/data').exists():
        raise RuntimeError('refusing: this runner home already holds Token Optimizer data')


# ------------------------------------------------------------ hook commands

_RUN_ARGS = re.compile(r'run\.py["\']?\s+([^;>]*?)\s*(?:\d?>[^;]*)?;\s*done')
_ASSIGN = re.compile(r'\b(TOKEN_OPTIMIZER_[A-Z_]+)=(\S+)\s')


def configured_hooks(config, events):
    """Hook entries exactly as an installed host config lists them."""
    data = json.loads(Path(config).read_text(encoding='utf-8'))
    table = data.get('hooks', {}) if isinstance(data, dict) else {}
    found = []
    for event in events:
        for group in table.get(event, []) or []:
            for hook in group.get('hooks', []) or []:
                command = hook.get('command', '')
                match = _RUN_ARGS.search(command)
                found.append({'event': event, 'matcher': group.get('matcher'), 'command': command,
                              'argv': shlex.split(match.group(1)) if match else None,
                              'env': dict(_ASSIGN.findall(command)),
                              'async': bool(hook.get('async')), 'timeout': hook.get('timeout', 30)})
    return found


def hook_stdin(event, home, project, runtime, session='fixture-session'):
    """The same success-variant payload U0's capture builds for hook rows."""
    codex = runtime == 'codex'
    transcript = (next((home / '.codex/sessions').rglob('*.jsonl')) if codex
                  else home / '.claude/projects/-fixture/fixture-session.jsonl')
    return json.dumps({'hook_event_name': event,
                       'session_id': '00000000-0000-4000-8000-000000000002' if codex else session,
                       'cwd': str(project), 'transcript_path': str(transcript),
                       'prompt': 'continue the fixture task', 'source': 'startup',
                       'tool_name': 'Read', 'tool_input': {'file_path': str(project / 'example.py')},
                       'tool_response': 'fixture tool output'}).encode()


def bash():
    if os.name == 'nt':
        for candidate in (r'C:\Program Files\Git\bin\bash.exe', r'C:\Program Files\Git\usr\bin\bash.exe'):
            if Path(candidate).is_file():
                return candidate
    found = shutil.which('bash')
    if not found:
        raise RuntimeError('bash is required to run configured hook commands')
    return found


def run(argv, env, cwd, stdin=b'', timeout=120):
    start = time.perf_counter()
    try:
        proc = subprocess.run(argv, input=stdin, cwd=cwd, env=env, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        return {'argv': [str(a) for a in argv], 'exit': 'timeout', 'stdout': error.stdout or b'',
                'stderr': error.stderr or b'', 'seconds': time.perf_counter() - start}
    return {'argv': [str(a) for a in argv], 'exit': proc.returncode, 'stdout': proc.stdout,
            'stderr': proc.stderr, 'seconds': time.perf_counter() - start}


def run_hook(command, env, cwd, stdin, timeout=120, shell='bash'):
    """A configured hook command string, run by the shell its host uses:
    bash for Claude Code and Cowork, %COMSPEC% /C for Codex on Windows."""
    if shell == 'cmd':
        start = time.perf_counter()
        try:
            proc = subprocess.run(command, input=stdin, cwd=cwd, env=env, capture_output=True,
                                  timeout=timeout, shell=True)
        except subprocess.TimeoutExpired as error:
            return {'argv': ['cmd', '/c', command], 'exit': 'timeout', 'stdout': error.stdout or b'',
                    'stderr': error.stderr or b'', 'seconds': time.perf_counter() - start}
        return {'argv': ['cmd', '/c', command], 'exit': proc.returncode, 'stdout': proc.stdout,
                'stderr': proc.stderr, 'seconds': time.perf_counter() - start}
    return run([bash(), '-c', command], env, cwd, stdin, timeout)


def error_lines(result):
    text = (result['stderr'] or b'').decode('utf-8', 'replace') + (result['stdout'] or b'').decode('utf-8', 'replace')
    return [line for line in text.splitlines() if any(marker in line for marker in ERROR_MARKERS)]


def clean_run(result):
    return result['exit'] == 0 and not error_lines(result)


def brief(result):
    """Receipt-sized record of one run, never the full output."""
    return {'argv': result['argv'][-3:], 'exit': result['exit'], 'seconds': round(result['seconds'], 4),
            'stdout_bytes': len(result['stdout'] or b''),
            'stdout_sha256': hashlib.sha256(result['stdout'] or b'').hexdigest(),
            'stderr_head': (result['stderr'] or b'')[:600].decode('utf-8', 'replace'),
            'errors': error_lines(result)[:5]}


# ----------------------------------------------------------------- liveness

def read_liveness(directory):
    records = []
    for path in sorted(Path(directory).glob('proc-*.json')):
        try:
            records.append(json.loads(path.read_text(encoding='utf-8')))
        except (OSError, ValueError) as error:
            records.append({'unreadable': str(path), 'error': repr(error)})
    return records


def _inside(path, root):
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False


def _from_install(path, root):
    """Inside the install, or bytecode a PYTHONPYCACHEPREFIX mirrors from it
    (the npm cloud kit keeps its bytecode outside the version dir)."""
    if _inside(path, root):
        return True
    mirrored = Path(root).resolve().as_posix().split(':', 1)[-1].lstrip('/')
    return str(path).endswith('.pyc') and mirrored in Path(path).as_posix()


def liveness_summary(records, install_root):
    """Which facade and which split package actually executed, and from where."""
    facades = sorted({f for r in records for f in r.get('facade_reads', [])})
    modules = {}
    for record in records:
        modules.update(record.get('split_modules', {}))
    outside = sorted({f for f in facades + [m for m in modules.values() if m]
                      if not _from_install(f, install_root)})
    return {'processes': len(records), 'facade_reads': facades, 'split_modules': sorted(modules),
            'outside_install': outside, 'probe_errors': [r for r in records if 'error' in r][:3],
            'peak_rss_bytes': max((r.get('peak_rss_bytes') or 0 for r in records), default=0)}


def expected_modules(tree):
    """Every provider the facade imports eagerly must load: all package modules
    except the commit's own DEFERRED_MODULES, which load on first use."""
    deferred = {'_measure.' + name for name in deferred_modules(tree)}
    names = set()
    for relative in split_sources(tree):
        parts = Path(relative).relative_to('skills/token-optimizer/scripts').with_suffix('').parts
        names.add('.'.join(parts[:-1]) if parts[-1] == '__init__' else '.'.join(parts))
    return names - deferred


def deferred_modules(tree):
    """`_measure.DEFERRED_MODULES` as the tree's own commit declares it."""
    init = Path(tree) / SPLIT_PACKAGE / '__init__.py'
    if not init.is_file():
        return ()
    for node in ast.parse(init.read_text(encoding='utf-8')).body:
        if isinstance(node, ast.Assign) and [ast.unparse(t) for t in node.targets] == ['DEFERRED_MODULES']:
            return tuple(ast.literal_eval(node.value))
    return ()


def candidate_live(summary, expected=None):
    """Candidate code ran from its own install; with `expected`, every
    package module of the candidate commit was imported."""
    loaded = set(summary['split_modules'])
    complete = expected is None or (bool(expected) and set(expected) <= loaded)
    return bool(summary['facade_reads']) and bool(loaded) and complete and not summary['outside_install']


def base_is_base(summary):
    """BASE predates the package: its arm must never load `_measure`."""
    return bool(summary['facade_reads']) and not summary['split_modules'] and not summary['outside_install']
