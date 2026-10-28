"""Suite-wide guard: running the tests must never write to the working tree.

WHY THIS EXISTS
---------------
Twice in one day a pytest run destroyed real work:

  1. ``plugins/token-optimizer/skills/token-optimizer/scripts/benchmark.py``
     vanished from the working tree during a suite run (twice), and was restored
     by hand in commit "fix(parity): restore benchmark.py to plugins mirror".
  2. Worse because it was silent: an engineer edited
     ``plugins/token-optimizer/hooks/userpromptsubmit_runner.py``, ran the suite,
     and the suite regenerated that mirror from the canonical
     ``hooks/userpromptsubmit_runner.py``, discarding the edit. ``git status`` was
     clean afterwards, so nothing pointed at the loss.

The cause was anti-drift tests that proved "the committed mirror is reproducible"
by REGENERATING it in place and asserting ``git status`` was clean, plus README
tests that mutated ``README.md`` and restored it in a ``finally``. A suite that
rewrites the tree can eat uncommitted work with no error and no diff, and it
makes results order-dependent: a later parity test passes only because an earlier
test rewrote the tree under it.

WHAT THIS DOES
--------------
Fingerprints every git-TRACKED file at session start and again at session end,
then fails the run if any of them changed. The fingerprint is
``(sha256, inode, mtime_ns)``, not content alone, because the dangerous case is
the CONTENT-IDENTICAL rewrite: regenerating a mirror on an already-clean tree
changes nothing a hash can see, and only bites the engineer who happened to have
an uncommitted edit. Inode/mtime catch the write itself.

Tracked files only, so untracked scratch files, ``.pytest_cache``, build staging
dirs and briefs in progress are ignored -- the guard is about destroying work
under version control.

Set ``TOKEN_OPTIMIZER_TREE_GUARD_PER_TEST=1`` to fingerprint around EVERY test
instead of once per session. That attributes a mutation to the exact test that
caused it (it costs a few minutes on the full suite, so it is opt-in).

Set ``TOKEN_OPTIMIZER_TREE_GUARD=0`` to disable the guard entirely -- for the
one legitimate case, a deliberate negative test of the guard itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Run as CI does, even inside an agent session. A Claude Code, Codex or
# Copilot session exports its own identity (CLAUDE_CODE_REMOTE, CLAUDECODE,
# CLAUDE_PLUGIN_ROOT, CODEX_THREAD_ID, ...). Product code reads those to pick a
# runtime or a lean cloud path, so with them set some guards stopped testing
# anything: the #117 process-budget guard passed with the bug planted back.
# Set TO_TEST_KEEP_HOST_ENV=1 to keep them on purpose.
_HOST_AGENT_ENV_PREFIXES = ("CLAUDE", "CODEX_", "COPILOT_", "CURSOR_", "AI_AGENT",
                            "OPENCODE", "HERMES_", "GROK_", "ANTIGRAVITY_", "DEVIN_")
if os.environ.get("TO_TEST_KEEP_HOST_ENV", "") not in ("1", "true", "yes"):
    for _key in [k for k in os.environ if k.upper().startswith(_HOST_AGENT_ENV_PREFIXES)]:
        os.environ.pop(_key, None)

# Real-home protection (see tests/_home_guard.py). This MUST run at conftest
# import, before any test module imports a product script that caches
# HOME-derived paths at import time.
import _home_guard  # noqa: E402

_home_guard.install_sandbox()
_home_guard.install_guard()

# A hook that finds measure.py's bytecode cold waits for the background compile
# only as long as the host's hook timeout allows, then exits without output
# (#264, hooks/module_runner.py). Test sandboxes start cold, and the compile
# takes 10-30 s on Windows runners and 5 s+ on a loaded Mac, past the 4 s most
# measure.py hooks wait. A test hook would then get the host-deadline fail-open
# instead of the behaviour it checks. Tests have no host deadline: let them wait
# for the compile, as an inline compile did. The hook harnesses pass this one
# variable through. The gate itself is pinned by tests/test_cold_compile_gate.py,
# which strips it and sets explicit waits.
os.environ.setdefault("TOKEN_OPTIMIZER_COLD_COMPILE_WAIT_S", "600")

_ENV_DISABLE = "TOKEN_OPTIMIZER_TREE_GUARD"
_ENV_PER_TEST = "TOKEN_OPTIMIZER_TREE_GUARD_PER_TEST"

# Filled at session start; read by the guard test and by the session-end check.
BASELINE: dict[str, tuple] | None = None
UNAVAILABLE_REASON: str | None = None
# Mutations attributed to individual tests when per-test mode is on.
PER_TEST_MUTATIONS: list[tuple[str, list[str]]] = []


def guard_enabled() -> bool:
    return os.environ.get(_ENV_DISABLE, "1") not in ("0", "false", "no")


def tracked_files() -> list[str] | None:
    """Every git-tracked path, or None when this is not a usable git work tree."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(REPO),
            capture_output=True,
            check=False,
        )
    except (OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return [p.decode("utf-8", "surrogateescape") for p in proc.stdout.split(b"\0") if p]


def fingerprint(paths) -> dict[str, tuple]:
    """path -> (sha256, inode, mtime_ns), or ("<absent>",) for a deleted file.

    Content AND identity: a regenerate-in-place that reproduces byte-identical
    content still changes the inode and mtime, and that is precisely the write
    that silently ate an engineer's uncommitted edit.
    """
    out: dict[str, tuple] = {}
    for rel in paths:
        p = REPO / rel
        try:
            st = p.lstat()
        except OSError:
            out[rel] = ("<absent>",)
            continue
        if p.is_symlink():
            out[rel] = ("symlink:" + os.readlink(p), st.st_ino, st.st_mtime_ns)
            continue
        try:
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError as exc:
            out[rel] = ("<unreadable:%s>" % exc.errno,)
            continue
        out[rel] = (digest, st.st_ino, st.st_mtime_ns)
    return out


def diff_fingerprints(before: dict, after: dict) -> list[str]:
    """Human-readable lines describing every tracked-file mutation."""
    lines: list[str] = []
    for rel in sorted(set(before) | set(after)):
        was, now = before.get(rel), after.get(rel)
        if was == now:
            continue
        if now is None or now[0] == "<absent>":
            lines.append(f"  DELETED    {rel}")
        elif was is None or was[0] == "<absent>":
            lines.append(f"  CREATED    {rel}")
        elif was[0] != now[0]:
            lines.append(f"  MODIFIED   {rel}")
        else:
            lines.append(
                f"  REWRITTEN  {rel}  (same bytes, new inode/mtime -- a "
                f"regenerate-in-place)"
            )
    return lines


def snapshot_now() -> dict[str, tuple] | None:
    paths = tracked_files()
    if paths is None:
        return None
    return fingerprint(paths)


def mutations_since_session_start() -> list[str]:
    if BASELINE is None:
        return []
    return diff_fingerprints(BASELINE, fingerprint(list(BASELINE)))


FAILURE_ADVICE = (
    "\nA test wrote to a git-TRACKED file. That is never acceptable: a suite that\n"
    "rewrites the working tree can destroy uncommitted work with no error and no\n"
    "diff, and it makes results order-dependent. Fix the test to operate on a\n"
    "copy under pytest's tmp_path (see tests/_tree_parity.py for the staging +\n"
    "compare helpers the mirror parity tests use).\n"
    "\nRun with TOKEN_OPTIMIZER_TREE_GUARD_PER_TEST=1 to attribute the write to a\n"
    "specific test. If you were editing these files by hand while the suite ran,\n"
    "that is the other explanation -- rerun on a quiet tree to tell them apart.\n"
)


# Windows caps one environment variable at 32767 characters, and pytest exports
# the running test's id as PYTEST_CURRENT_TEST. An id derived from a huge
# parameter value errors there and stalls the run while it is reported, so an
# oversized id is refused on every platform at collection time.
MAX_TEST_ID_CHARS = 32500


def _shard_spec():
    """TO_TEST_SHARD="K/N" (1-based) runs slice K of N; unset runs everything."""
    raw = os.environ.get("TO_TEST_SHARD", "").strip()
    if not raw:
        return None
    try:
        k, n = (int(x) for x in raw.split("/"))
    except ValueError:
        raise pytest.UsageError(f"TO_TEST_SHARD must look like 2/4, got {raw!r}")
    if not (n >= 1 and 1 <= k <= n):
        raise pytest.UsageError(f"TO_TEST_SHARD out of range: {raw!r}")
    return k, n


def shard_files(weights, n):
    """Split test files into n slices of similar total weight.

    Whole files stay together, so module fixtures run once. Heaviest first onto
    the lightest slice; ties break on name, so every machine computes the same
    split. test_zz_* files (end-of-session guards) belong to every slice.
    """
    slices = [[] for _ in range(n)]
    loads = [0.0] * n
    for name in sorted(weights, key=lambda f: (-weights[f], f)):
        if Path(name).name.startswith("test_zz_"):
            for s in slices:
                s.append(name)
            continue
        i = min(range(n), key=lambda j: (loads[j], j))
        slices[i].append(name)
        loads[i] += weights[name]
    return [set(s) for s in slices]


def _file_weights(items):
    """Seconds per file from tests/shard_weights.json when known, else a
    per-test estimate, so a new file still lands somewhere sensible."""
    known = {}
    try:
        known = json.loads((Path(__file__).parent / "shard_weights.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    counts = {}
    for item in items:
        f = item.nodeid.split("::", 1)[0]
        counts[f] = counts.get(f, 0) + 1
    return {f: float(known.get(f, 0.15 * c)) for f, c in counts.items()}


def pytest_collection_modifyitems(config, items):
    oversized = [item.nodeid for item in items if len(item.nodeid) > MAX_TEST_ID_CHARS]
    if oversized:
        raise pytest.UsageError(
            'test id longer than %d characters; pass explicit ids= to parametrize: %s'
            % (MAX_TEST_ID_CHARS, ', '.join(nodeid[:120] + '...' for nodeid in oversized)))
    spec = _shard_spec()
    if spec is None:
        return
    k, n = spec
    mine = shard_files(_file_weights(items), n)[k - 1]
    keep = [it for it in items if it.nodeid.split("::", 1)[0] in mine]
    dropped = [it for it in items if it.nodeid.split("::", 1)[0] not in mine]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = keep


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: timing-sensitive test (startup budgets)")


def pytest_sessionstart(session):
    global BASELINE, UNAVAILABLE_REASON
    if not guard_enabled():
        UNAVAILABLE_REASON = f"{_ENV_DISABLE} disables the working-tree guard"
        return
    BASELINE = snapshot_now()
    if BASELINE is None:
        UNAVAILABLE_REASON = "not a git work tree (git ls-files unavailable)"


@pytest.fixture(autouse=True)
def _tree_guard_per_test(request):
    """Opt-in per-test attribution of tracked-file writes."""
    if BASELINE is None or os.environ.get(_ENV_PER_TEST, "0") in ("0", "", "false"):
        yield
        return
    keys = list(BASELINE)
    before = fingerprint(keys)
    yield
    changed = diff_fingerprints(before, fingerprint(keys))
    if changed:
        PER_TEST_MUTATIONS.append((request.node.nodeid, changed))


# Env vars that decide which runtime Token Optimizer thinks it is in. Hook
# bridges set these on purpose (os.environ.setdefault in cursor_hook_bridge.main,
# for example, because in production that process IS Cursor), so a test that
# drives a bridge's main() leaks them into every later test: the whole savings
# suite then ran as "cursor" and reported unsupported_billing. Restore them after
# every test so no test can change the runtime or data dir of the ones after it.
_RUNTIME_ENV_KEYS = (
    "TOKEN_OPTIMIZER_RUNTIME",
    "TOKEN_OPTIMIZER_COPILOT_HOME",
    "TOKEN_OPTIMIZER_CURSOR_HOME",
    "COPILOT_HOME",
    "CODEX_HOME",
    "HERMES_HOME",
    # Data-dir override: a leaked temp dir silently redirects later tests'
    # settings and snapshot reads (test_parity_5138_behavioral leaked it).
    "TOKEN_OPTIMIZER_SNAPSHOT_DIR",
)


# Env vars that change hook DECISIONS (not just which runtime/data-dir). The
# helpers that build hook subprocess envs copy os.environ, so an ambient value
# — exported by the dev or baked into a CI image — silently un-guards tools and
# fails assertions through no code fault. Removed for the test, restored after.
_SANITIZED_ENV_KEYS = (
    "TOKEN_OPTIMIZER_LIVE_STATE_TOOLS",
)


@pytest.fixture(autouse=True)
def _scrub_user_session_env(monkeypatch):
    """Drop the developer's own session settings so they cannot steer a test.

    Running the suite from inside a Claude Code session inherits that session's
    switches: the Bash compression off switch disables every bridge rewrite, and
    a custom compact window rescales context fill. A test that needs either one
    sets it itself.
    """
    for name in ("TOKEN_OPTIMIZER_BASH_COMPRESS", "CLAUDE_CODE_AUTO_COMPACT_WINDOW"):
        monkeypatch.delenv(name, raising=False)


def _clear_runtime_cache():
    """detect_runtime() is cached for the life of the process. Restoring the env
    vars is not enough: a test that forced "codex" leaves the cached answer
    behind, and every later test in that process runs as Codex."""
    mod = sys.modules.get("runtime_env")
    if mod is not None and hasattr(getattr(mod, "detect_runtime", None), "cache_clear"):
        mod.detect_runtime.cache_clear()


@pytest.fixture(autouse=True)
def _restore_runtime_env():
    saved = {k: os.environ.get(k) for k in _RUNTIME_ENV_KEYS + _SANITIZED_ENV_KEYS}
    for key in _SANITIZED_ENV_KEYS:
        os.environ.pop(key, None)
    _clear_runtime_cache()
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _clear_runtime_cache()


@pytest.fixture(autouse=True)
def _real_home_guard(request):
    """Fail any test that touches the developer's REAL home directory.

    Hits are recorded by an audit hook (tests/_home_guard.py) and attributed to
    the running test by node id; a test that is allowed to touch real home
    read-only needs an entry in ``_home_guard.ALLOWLIST``, with a reason.
    """
    nodeid = request.node.nodeid
    _home_guard.set_current(nodeid)
    # hooks/run.py main() sets sys.pycache_prefix for its own process; a test
    # that calls it in-process must not move bytecode for the rest of the run.
    pycache_prefix = sys.pycache_prefix
    yield
    sys.pycache_prefix = pycache_prefix
    _home_guard.set_current("")
    hits = _home_guard.take_hits(nodeid)
    if hits:
        pytest.fail(
            "test touched the REAL home directory (%s):\n%s\n"
            "Product modules cache HOME-derived paths at import; use tmp_path "
            "and monkeypatch the module constants (see tests/_home_guard.py)."
            % (_home_guard.REAL_HOME, _home_guard.format_hits(hits)),
            pytrace=False,
        )


def _report_lines() -> list[str]:
    lines = mutations_since_session_start()
    if not lines:
        return []
    out = ["", "TRACKED FILES MUTATED BY THE TEST SUITE:", *lines]
    if PER_TEST_MUTATIONS:
        out.append("")
        out.append("attributed to:")
        for nodeid, changed in PER_TEST_MUTATIONS:
            out.append(f"  {nodeid}")
            out.extend("    " + c.strip() for c in changed)
    out.append(FAILURE_ADVICE)
    return out


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    for line in _report_lines():
        terminalreporter.write_line(line)


def pytest_sessionfinish(session, exitstatus):
    """Fail the run for a tree mutation even if every test itself passed.

    The guard test in tests/test_zz_worktree_immutable.py reports the same thing
    as a named failure, but only when it is collected and only if it runs last.
    This hook is the backstop: any tracked-file write fails the session, whatever
    was selected and in whatever order it ran.
    """
    if mutations_since_session_start() and exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    # Real-home touches outside any test (import/collection time) have no test
    # to fail, so they fail the session here.
    stray = _home_guard.take_hits("")
    if stray:
        sys.stderr.write(
            "\nREAL HOME TOUCHED OUTSIDE A TEST (import/collection):\n%s\n"
            % _home_guard.format_hits(stray))
        if exitstatus == 0:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture()
def trusted_python(tmp_path_factory, monkeypatch):
    """A gate-trusted interpreter for installer tests.

    Hosted-CI tool caches ship the real interpreter world-writable, which the
    shared trust gate (correctly) rejects as a swap vector. Installer tests
    pin TOKEN_OPTIMIZER_PYTHON to a trusted scratch interpreter instead,
    exactly as the cursor/copilot suites do."""
    import os as _os
    d = tmp_path_factory.mktemp("trusted-bin")
    f = d / "python3"
    f.write_text("#!/bin/sh\n")
    _os.chmod(f, 0o755)
    _os.chmod(d, 0o755)
    monkeypatch.setenv("TOKEN_OPTIMIZER_PYTHON", str(f))
    return Path(f)


_DEVIN_TEST_FILES = {
    'test_devin_closedset.py', 'test_devin_compress.py', 'test_devin_install.py',
    'test_devin_lifecycle_matrix.py', 'test_devin_review_regressions.py',
    'test_devin_state.py', 'test_devin_hooks.py',
}


def _devin_isolated_env(env, home):
    """Keep inherited host configuration out of a synthetic Devin home."""
    result = dict(env)
    for key in ('XDG_CONFIG_HOME', 'TOKEN_OPTIMIZER_DEVIN_HOME',
                'TOKEN_OPTIMIZER_RUNTIME', 'DEVIN_CONFIG_DIR'):
        result.pop(key, None)
    result.update({'HOME': str(home), 'USERPROFILE': str(home),
                   'CLAUDE_CONFIG_DIR': str(home)})
    return result


@pytest.fixture(scope='session')
def devin_real_python(tmp_path_factory):
    """Copy executable bytes, never mock or bypass the production trust gate."""
    import shutil
    import sys
    folder = tmp_path_factory.mktemp('devin-real-python')
    folder.chmod(0o700)
    import subprocess
    subprocess.run([sys.executable, '-m', 'venv', '--copies', '--without-pip',
                    str(folder)], check=True, capture_output=True)
    binary = folder / 'bin' / 'python3'
    assert not binary.is_symlink()
    binary.chmod(0o700)
    binary.parent.chmod(0o700)
    # TOKEN_OPTIMIZER_DEVIN_SCRIPTS_ROOT points the suite at a scratch copy of
    # the product scripts; mutation runs use it, the default stays the tree.
    override = os.environ.get('TOKEN_OPTIMIZER_DEVIN_SCRIPTS_ROOT')
    scripts = Path(override).resolve() if override else REPO / 'skills/token-optimizer/scripts'
    sys.path.insert(0, str(scripts))
    from py_trust import py_trust_reason
    reason = py_trust_reason(str(binary))
    assert reason is None, f'copied real interpreter refused: {reason}'
    return binary, sys.base_prefix


@pytest.fixture(autouse=True)
def devin_isolation(request, monkeypatch):
    """Scoped to Devin tests; other runtime suites keep their own fixtures."""
    if request.node.path.name not in _DEVIN_TEST_FILES:
        return
    import sys
    if os.name != 'posix':
        pytest.skip('Devin adapter explicitly requires a POSIX environment')
    tmp_path = request.getfixturevalue('tmp_path')
    binary, prefix = request.getfixturevalue('devin_real_python')
    tmp_path.chmod(0o700)
    env = _devin_isolated_env(os.environ, tmp_path)
    for key in ('XDG_CONFIG_HOME', 'TOKEN_OPTIMIZER_DEVIN_HOME',
                'TOKEN_OPTIMIZER_RUNTIME', 'DEVIN_CONFIG_DIR'):
        monkeypatch.delenv(key, raising=False)
    for key in ('HOME', 'USERPROFILE', 'CLAUDE_CONFIG_DIR'):
        monkeypatch.setenv(key, env[key])
    # A copied binary still needs its real stdlib and shared-library prefix.
    monkeypatch.setenv('PYTHONHOME', prefix)
    monkeypatch.setenv('LD_LIBRARY_PATH', str(Path(prefix) / 'lib'))
    monkeypatch.setenv('PATH', str(binary.parent) + os.pathsep + os.environ.get('PATH', ''))
    monkeypatch.setattr(sys, 'executable', str(binary))
    return env
