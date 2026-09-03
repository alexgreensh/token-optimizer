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
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

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
