"""Build each real Token Optimizer artifact from one commit and install it
into a synthetic home the way its host does.

Every installer here is the product's own code (packaging/npm/build.py,
install-cloud.sh, `measure.py codex-install`, `measure.py devin-install`) run
as a subprocess. The two plain-copy installs (Claude Code plugin cache, Cowork
plugin) mirror what the host does: copy the plugin source into a versioned
cache directory.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import common as c

MARKETPLACE = 'alexgreensh-token-optimizer'
CLAUDE_EVENTS = ('SessionStart', 'UserPromptSubmit', 'PostToolUse', 'PreCompact')
CODEX_EVENTS = ('SessionStart', 'UserPromptSubmit', 'PostToolUse', 'Stop')
DEVIN_ROOT = '.config/devin/token-optimizer/plugin'


class ArtifactUnavailable(Exception):
    """The host tool this artifact needs is absent: CANNOT-MEASURE, not FAIL."""


def claude_cache(home, plugin, version):
    return home / '.claude/plugins/cache' / MARKETPLACE / plugin / version


def install_claude_plugin(tree, home, version=None):
    """Claude Code copies the marketplace source ('./') into its cache."""
    target = claude_cache(home, 'token-optimizer', version or c.plugin_version(tree))
    c.copy_tree(tree, target)
    return {'root': target, 'config': target / 'hooks/hooks.json', 'events': CLAUDE_EVENTS,
            'runtime': 'claude', 'host': 'claude', 'mode': 'plugin-cache copy'}


def install_cowork(tree, home):
    source = Path(tree) / 'cowork/token-optimizer'
    target = claude_cache(home, 'token-optimizer-cowork', c.plugin_version(source))
    c.copy_tree(source, target)
    return {'root': target, 'config': target / 'hooks/hooks.json', 'events': CLAUDE_EVENTS,
            'runtime': 'claude', 'host': 'cowork', 'mode': 'committed cowork plugin copy'}


def install_codex(tree, home, env, cwd):
    """The committed Codex mirror, then the mirror's own codex-install."""
    source = Path(tree) / 'plugins/token-optimizer'
    version = json.loads((source / '.codex-plugin/plugin.json').read_text(encoding='utf-8'))['version']
    target = home / '.codex/plugins/cache' / MARKETPLACE / 'token-optimizer' / version
    c.copy_tree(source, target)
    result = c.run([sys.executable, str(target / c.FACADE), 'codex-install'], env, cwd, timeout=180)
    config = home / '.codex/hooks.json'
    if result['exit'] != 0 or not config.is_file():
        raise RuntimeError('codex-install failed: ' + json.dumps(c.brief(result)))
    return {'root': target, 'config': config, 'events': CODEX_EVENTS, 'runtime': 'codex',
            'host': 'codex', 'mode': 'committed mirror + codex-install', 'install': c.brief(result)}


def build_npm_tarball(sha, work, restamp_to=None):
    """The subject commit's own packaging/npm/build.py and `npm pack`, from a
    real checkout so build.py's tracked-file filter sees that commit."""
    npm = shutil.which('npm')
    if not npm:
        raise ArtifactUnavailable('npm is not on PATH')
    checkout = work / 'npm-checkout'
    c.git('worktree', 'add', '--detach', '--force', str(checkout), sha)
    try:
        if restamp_to:
            c.golden.restamp(checkout, restamp_to)
        stage = work / 'npm-stage'
        subprocess.run([sys.executable, str(checkout / 'packaging/npm/build.py'), '--out', str(stage)],
                       cwd=checkout, check=True, capture_output=True)
        env = {**os.environ, 'npm_config_cache': str(work / 'npm-cache')}
        packed = subprocess.run([npm, 'pack', '--json', '--offline', '--ignore-scripts',
                                 '--pack-destination', str(work)], cwd=stage / 'package', env=env,
                                check=True, capture_output=True, text=True)
        tarball = work / json.loads(packed.stdout)[0]['filename']
    finally:
        subprocess.run(['git', 'worktree', 'remove', '--force', str(checkout)], cwd=c.ROOT,
                       capture_output=True)
    extracted = c.case_dir(work).parent / 'npm-extracted'  # outside any git work tree
    c.extract(tarball, extracted)
    return tarball, extracted / 'package'


def install_npm(package, home, env, cwd):
    """install-cloud.sh is the package's only installer. It targets Linux VMs
    and needs `python3`; elsewhere the extracted package is the plugin root."""
    python3 = shutil.which('python3', path=env.get('PATH'))
    if os.name == 'nt' or not python3:
        return {'root': package, 'config': package / 'hooks/hooks.json', 'events': CLAUDE_EVENTS,
                'runtime': 'claude', 'host': 'claude',
                'mode': 'extracted npm package as plugin root (install-cloud.sh is Linux-VM only)'}
    result = c.run([c.bash(), str(package / 'install-cloud.sh')], env, cwd, timeout=300)
    cache = home / '.cache/token-optimizer-cloud'
    roots = sorted(p for p in cache.iterdir() if (p / 'hooks/run.py').is_file()) if cache.is_dir() else []
    log = (cache / 'install.log').read_text(encoding='utf-8', errors='replace') if (cache / 'install.log').is_file() else ''
    if result['exit'] != 0 or len(roots) != 1 or 'exit 0' not in log:
        raise RuntimeError('install-cloud.sh failed: ' + log[-1500:])
    return {'root': roots[0], 'config': home / '.claude/settings.json', 'events': CLAUDE_EVENTS,
            'runtime': 'claude', 'host': 'claude', 'mode': 'install-cloud.sh (cloud_kit)',
            'extra_env': {'TOKEN_OPTIMIZER_CLOUD_KIT': 'force'}, 'install_log_tail': log[-400:]}


_TRUST_PROBE = ('import sys; sys.path.insert(0, sys.argv[1]); from py_trust import py_trust_reason; '
                'print(py_trust_reason(sys.executable) or "trusted" if sys.version_info >= (3, 11) else "too old")')


def trusted_python(plugin_root, env, cwd):
    """The installer refuses an interpreter whose file or directory another
    account could swap (py_trust). Hosted runners' framework Pythons can fail
    that check, so look for one the product itself would accept."""
    scripts = str(Path(plugin_root) / 'skills/token-optimizer/scripts')
    seen, reasons = set(), {}
    for candidate in (sys.executable, shutil.which('python3'), '/opt/homebrew/bin/python3',
                      '/usr/local/bin/python3', '/usr/bin/python3'):
        if not candidate or candidate in seen or not Path(candidate).is_file():
            continue
        seen.add(candidate)
        probe = c.run([candidate, '-I', '-c', _TRUST_PROBE, scripts], env, cwd, timeout=60)
        verdict = (probe['stdout'] or b'').decode('utf-8', 'replace').strip()
        if verdict == 'trusted':
            return candidate, reasons
        reasons[candidate] = verdict or c.brief(probe)['stderr_head']
    raise ArtifactUnavailable('no interpreter the Devin installer trusts: ' + json.dumps(reasons)[:800])


def install_devin(plugin_root, home, env, cwd):
    """`measure.py devin-install` from an installed Claude plugin, run by an
    interpreter the installer trusts (the same one for both arms)."""
    python, rejected = trusted_python(plugin_root, env, cwd)
    result = c.run([python, str(Path(plugin_root) / c.FACADE), 'devin-install'], env, cwd, timeout=180)
    target = home / DEVIN_ROOT
    if result['exit'] != 0 or not target.is_dir():
        raise RuntimeError('devin-install failed: ' + json.dumps(c.brief(result)))
    return {'root': target, 'install': c.brief(result), 'python': python, 'rejected_interpreters': rejected}


def devin_payload(tree):
    """PAYLOAD as the commit's devin_source.py declares it."""
    source = (Path(tree) / 'skills/token-optimizer/scripts/devin_source.py').read_text(encoding='utf-8')
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == 'PAYLOAD' for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    raise ValueError('devin_source.py has no literal PAYLOAD')


def devin_closed_set(installed, tree, name):
    """Exactly the declared payload; no facade and no split package."""
    expected = set(devin_payload(tree))
    present = {p.name for p in Path(installed).iterdir() if p.is_file()}
    errors = ['missing: ' + n for n in sorted(expected - present)]
    errors += ['unexpected: ' + n for n in sorted(present - expected)]
    errors += ['measurement engine shipped: ' + p.name for p in Path(installed).iterdir()
               if p.name in ('measure.py', '_measure')]
    return c.check(name, c.FAIL if errors else c.PASS, errors or f'{len(expected)} payload files, closed')


_REFUSAL_PROBE = '''
import json, sys
plugin, scripts = sys.argv[1], sys.argv[2]
sys.path.insert(0, plugin)
import devin_source
devin_source.activate(plugin)
sys.path.append(scripts)
out = {}
for name in ('_measure', '_measure.pricing', 'measure'):
    try:
        __import__(name)
        out[name] = 'imported'
    except ImportError as error:
        out[name] = type(error).__name__ + ': ' + str(error)
print(json.dumps(out))
'''


def devin_refusal(installed, plugin_root, env, cwd):
    """Inside the activated Devin environment, with the canonical scripts dir
    reachable, every measurement root must be refused by name."""
    scripts = Path(plugin_root) / 'skills/token-optimizer/scripts'
    result = c.run([sys.executable, '-I', '-c', _REFUSAL_PROBE, str(installed), str(scripts)], env, cwd)
    try:
        outcome = json.loads(result['stdout'])
    except ValueError:
        return c.check('devin-refusal', c.FAIL, c.brief(result))
    refused = all(value == 'ImportError: repo module outside Devin payload' for value in outcome.values())
    return c.check('devin-refusal', c.PASS if refused else c.FAIL, outcome)
