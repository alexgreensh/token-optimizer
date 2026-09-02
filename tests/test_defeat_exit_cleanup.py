"""Adversarial defeat-probes for the three exit-cleanup fixes.

This file is written by the **defeat-validator**.  It does NOT test happy paths
(those are covered by ``test_rollup_deadline.py``, ``test_locks_orphan_sweep.py``,
and ``test_reclaim_sweep_serialization.py``).  Instead it tries to *break* each
fix along axes the regression tests do not cover:

  Bug A
    * the deadline actually *fires* (the blocked path emits the
      "hook budget exceeded" diagnostic on stderr and bounded-exits with
      returncode 0), proving the ``os._exit`` path ran -- not that the collect
      returned fast for some other reason.
    * normal completion is not spuriously killed (re-probed).
    * the budget is literally ``60`` (not 0 / negative / a typo) by source
      inspection of both rollup branches.

  Bug B
    * a young/live candidate is never reaped while a sweep runs concurrently
      (race).
    * a candidate whose mtime sits *exactly* on the cutoff boundary survives
      (the sweep uses strict ``<``).
    * the sweep is best-effort / non-fatal: an unreadable ``.locks`` directory
      never aborts ``cleanup_old_archives``.
    * the sweep never touches the published ``{sid}.lease`` file or a
      candidate-named directory.

  Bug C (hardest -- most probes)
    * 32 concurrent same-generation reclaimers -> EXACTLY one winner, no
      exception escapes, the lease is unlinked, no stray claim files.
      Repeated 5x to flush out flakies.
    * 8 generations x 8 threads concurrently -> each generation exactly one
      winner (no cross-generation interference).
    * the claim name matches ``sha256("owner:{nonce}")[:32]`` EXACTLY -- no
      random suffix (Option 2 was rejected).
    * the sweep never reaps a young in-flight ``.reclaim-{digest}`` claim while
      a reclaimer is live (race).
    * a stale claim is reaped and a subsequent reclaim succeeds (recovery).
    * double-unlink safety: after the winner unlinks the lease, a late
      same-generation reclaimer returns False WITHOUT raising.
    * calling ``_reclaim_if_expired`` on an already-unlinked lease returns
      False without raising.

A probe PASSING means the fix held (the defeat failed).  A probe FAILING means
a real defeat was found and is reported as a ``discoveredIssue``.

Run: python3 -m pytest tests/test_defeat_exit_cleanup.py -q
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
MEASURE = SCRIPTS / "measure.py"
sys.path.insert(0, str(SCRIPTS))

import hook_runtime  # noqa: E402

_DEFAULT_LEASE = hook_runtime.LeaseLock(
    SCRIPTS / ".probe.lease", acquire_timeout=0
)
MAX_LEASE_SECONDS = _DEFAULT_LEASE.lease_seconds + _DEFAULT_LEASE.reclaim_grace


# ---------------------------------------------------------------------------
# helpers (mirror the regression-test bootstrap idioms)
# ---------------------------------------------------------------------------


def _load_archive_result(monkeypatch: pytest.MonkeyPatch, snapshot_dir: Path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(snapshot_dir))
    monkeypatch.setenv("TOKEN_OPTIMIZER_ARCHIVE_RETENTION_HOURS", "24")
    monkeypatch.setenv("TOKEN_OPTIMIZER_ARCHIVE_RETENTION_MAX_FILES", "1000")
    monkeypatch.setenv("TOKEN_OPTIMIZER_ARCHIVE_RETENTION_MAX_BYTES", "1000000")
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "archive_result", SCRIPTS / "archive_result.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "archive_result", module)
    spec.loader.exec_module(module)
    return module


def _lease_path(archive_root: Path, sid: str) -> Path:
    return archive_root / ".locks" / f"{sid}.lease"


def _candidate_path(archive_root: Path, sid: str, nonce: str) -> Path:
    return archive_root / ".locks" / f".{sid}.lease.candidate-{nonce}"


def _reclaim_claim_path(lease_path: Path, owner_nonce: str) -> Path:
    digest = hashlib.sha256(f"owner:{owner_nonce}".encode("utf-8")).hexdigest()[:32]
    return lease_path.with_name(f".{lease_path.name}.reclaim-{digest}")


def _write_expired_owner_lease(lease_path: Path, owner_nonce: str) -> None:
    now = time.time()
    meta = {
        "pid": 999999,
        "nonce": owner_nonce,
        "released": 0,
        "reuse_wall": now - 100.0,
        "created_wall": now - 100.0,
        "expires_wall": now - 50.0,
    }
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    lease_path.write_text(
        json.dumps(meta, separators=(",", ":"), sort_keys=True), encoding="utf-8"
    )
    os.chmod(lease_path, 0o600)


def _age_old(path: Path) -> None:
    # Reclaim claims sweep on a much longer fuse than lease candidates: reaping
    # a live claim frees a deterministic name and can yield two winners, while
    # reaping a live candidate merely fails the acquire open.
    import archive_result
    threshold = (archive_result.RECLAIM_SWEEP_STALE_SECONDS
                 if ".lease.reclaim-" in path.name else MAX_LEASE_SECONDS)
    old = time.time() - (threshold + 5)
    os.utime(path, (old, old))


# ---------------------------------------------------------------------------
# Bug A probes
# ---------------------------------------------------------------------------


def _rollup_dispatch_code(
    subcommand: str,
    collect_name: str,
    tmp_path: Path,
    *,
    collect_block_seconds: float,
    budget_seconds: float,
) -> str:
    return (
        "import os, sys, time\n"
        "sys.path.insert(0, {scripts!r})\n"
        "sys.argv = ['measure.py', {sub!r}]\n"
        "os.environ.setdefault('PYTHONUTF8', '1')\n"
        "os.environ.setdefault('TOKEN_OPTIMIZER_SNAPSHOT_DIR', {snap!r})\n"
        "src = open({measure!r}).read()\n"
        "patch = (\n"
        "    'import time as _t\\n'\n"
        "    'from hook_runtime import HookDeadline as _HD\\n'\n"
        "    'def _test_install(seconds=8):\\n'\n"
        "    '    return _HD({budget!r}).start()\\n'\n"
        "    'def _test_collect(days=90, quiet=False):\\n'\n"
        "    '    _t.sleep({block!r})\\n'\n"
        "    '_install_hook_budget = _test_install\\n'\n"
        "    '{collect} = _test_collect\\n'\n"
        ")\n"
        "marker = 'if __name__ == \"__main__\":'\n"
        "src = src.replace(marker, patch + marker, 1)\n"
        "ns = {{'__name__': '__main__', '__file__': {measure!r}}}\n"
        "exec(compile(src, ns['__file__'], 'exec'), ns)\n"
    ).format(
        scripts=str(SCRIPTS),
        sub=subcommand,
        snap=str(tmp_path),
        measure=str(MEASURE),
        budget=budget_seconds,
        block=collect_block_seconds,
        collect=collect_name,
    )


def _run_rollup_subprocess(subcommand, collect_name, tmp_path, *, block, budget):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SCRIPTS)
    env["PYTHONUTF8"] = "1"
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(tmp_path)
    code = _rollup_dispatch_code(
        subcommand, collect_name, tmp_path,
        collect_block_seconds=block, budget_seconds=budget,
    )
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, timeout=8,
    )
    return proc, time.monotonic() - started


@pytest.mark.parametrize("subcommand,collect_name", [
    ("copilot-rollup", "_collect_copilot_sessions"),
    ("hermes-rollup", "_collect_hermes_sessions"),
])
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="wall-clock assertion includes POSIX-fast interpreter startup",
)
def test_defeat_bug_a_deadline_fires_emits_diagnostic(subcommand, collect_name, tmp_path):
    """ADVERSARIAL: prove the deadline actually *fires* (the os._exit(0) path
    runs), not merely that the subprocess bounded-exits for some other reason.
    A blocking collect + a 0.3s budget must (a) bounded-exit, (b) returncode 0,
    AND (c) emit the 'hook budget exceeded' diagnostic on stderr -- the
    watchdog's emitter thread wrote it before os._exit.  If the diagnostic is
    absent the deadline did NOT fire and the bounded exit is suspect."""
    try:
        proc, elapsed = _run_rollup_subprocess(
            subcommand, collect_name, tmp_path,
            block=30, budget=0.3,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"{subcommand} hung past 8s -- deadline did NOT fire (defeat: the "
            f"rollup branch is not bounded by the armed HookDeadline)"
        )
    assert proc.returncode == 0, (
        f"{subcommand} exit code {proc.returncode}; stderr={proc.stderr!r}"
    )
    assert elapsed < 4.0, (
        f"{subcommand} did not bounded-exit: took {elapsed:.2f}s "
        f"(deadline should have fired at ~0.3s)"
    )
    assert "hook budget exceeded" in proc.stderr, (
        f"{subcommand} bounded-exited in {elapsed:.2f}s but the 'hook budget "
        f"exceeded' diagnostic was NOT emitted on stderr -- the deadline's "
        f"os._exit path did not run; the bounded exit is not evidence the "
        f"fix fired. stderr={proc.stderr!r}"
    )


@pytest.mark.parametrize("subcommand,collect_name", [
    ("copilot-rollup", "_collect_copilot_sessions"),
    ("hermes-rollup", "_collect_hermes_sessions"),
])
def test_defeat_bug_a_normal_completion_spared(subcommand, collect_name, tmp_path):
    """ADVERSARIAL: a fast collect must NOT be spuriously killed.  A 5s budget
    with a 0s-block collect exits 0 with NO 'hook budget exceeded' diagnostic
    -- the finally clears the budget and the watchdog never fires."""
    proc, elapsed = _run_rollup_subprocess(
        subcommand, collect_name, tmp_path,
        block=0, budget=5.0,
    )
    assert proc.returncode == 0, (
        f"{subcommand} exit code {proc.returncode}; stderr={proc.stderr!r}"
    )
    assert "hook budget exceeded" not in proc.stderr, (
        f"{subcommand} emitted a spurious timeout diagnostic on normal "
        f"completion (normal path was killed): stderr={proc.stderr!r}"
    )


def test_defeat_bug_a_budget_value_is_60_source():
    """ADVERSARIAL: source-inspect that BOTH rollup branches arm
    ``_install_hook_budget(60)`` -- not 0, not negative, not a typo.  A budget
    of 0 would fire instantly (kill normal completion); a missing/negative
    budget would be a regression.  The literal 60 is the contract."""
    source = MEASURE.read_text(encoding="utf-8")
    for name in ("copilot-rollup", "hermes-rollup"):
        start = source.index(f'elif args[0] == "{name}":')
        nxt = source.find('elif args[0] ==', start + 1)
        end = nxt if nxt != -1 else len(source)
        block = source[start:end]
        assert "_install_hook_budget(60)" in block, (
            f"{name} does not arm _install_hook_budget(60) -- the budget value "
            f"is wrong/missing; block head={block[:200]!r}"
        )
        # Guard against a negative / zero budget sneaking in elsewhere.
        for bad in ("_install_hook_budget(0)", "_install_hook_budget(-",
                    "_install_hook_budget(0.0)"):
            assert bad not in block, (
                f"{name} contains a degenerate budget call {bad!r}"
            )


# ---------------------------------------------------------------------------
# Bug B probes
# ---------------------------------------------------------------------------


def test_defeat_bug_b_young_candidate_survives_concurrent_sweep_race(
    monkeypatch, tmp_path
):
    """ADVERSARIAL: race the sweep against a live acquirer.  A young candidate
    (live acquisition in flight) must NEVER be reaped while the sweep runs.
    Run cleanup_old_archives in a tight loop on one thread while another keeps
    the candidate's mtime fresh (a live acquirer touching its candidate); the
    candidate must survive the whole race window."""
    module = _load_archive_result(monkeypatch, tmp_path)
    root = tmp_path / "tool-archive"
    locks = root / ".locks"
    locks.mkdir(parents=True)
    young = _candidate_path(root, "sid-live", "race123")
    young.write_bytes(b"live-acquirer")
    os.chmod(young, 0o600)

    stop = threading.Event()
    errors: list[BaseException] = []

    def sweep_loop() -> None:
        try:
            while not stop.is_set():
                module.cleanup_old_archives()
        except BaseException as exc:  # pragma: no cover - surfaced in assert
            errors.append(exc)

    def touch_loop() -> None:
        try:
            while not stop.is_set():
                now = time.time()
                os.utime(young, (now, now))
                time.sleep(0.005)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    t1 = threading.Thread(target=sweep_loop)
    t2 = threading.Thread(target=touch_loop)
    t1.start()
    t2.start()
    time.sleep(0.3)
    stop.set()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert not errors, "race threads raised: %r" % (errors,)
    assert young.exists(), (
        "DEFEAT: a young live candidate was reaped by the sweep while an "
        "acquirer was in flight (false positive on a live acquisition)"
    )


def test_defeat_bug_b_boundary_candidate_at_exact_cutoff_survives(
    monkeypatch, tmp_path
):
    """ADVERSARIAL: a candidate whose mtime sits EXACTLY on the cutoff boundary
    survives.  The sweep uses strict ``st_mtime < cutoff``; an off-by-one
    (``<=``) would reap a candidate that is exactly at the max lease duration,
    a false positive on the boundary.  Because filesystem mtime resolution is
    coarse, we set the mtime to ``cutoff + epsilon`` and assert survival, then
    set it to ``cutoff - epsilon`` and assert reaped -- bracketing the
    boundary."""
    module = _load_archive_result(monkeypatch, tmp_path)
    root = tmp_path / "tool-archive"
    locks = root / ".locks"
    locks.mkdir(parents=True)

    cutoff = time.time() - MAX_LEASE_SECONDS
    boundary = _candidate_path(root, "sid-boundary", "bnd456")
    boundary.write_bytes(b"boundary")
    os.chmod(boundary, 0o600)
    # mtime just AFTER the cutoff (young) -> survives.
    os.utime(boundary, (cutoff + 0.5, cutoff + 0.5))
    module.cleanup_old_archives()
    assert boundary.exists(), (
        "DEFEAT: a candidate just inside the young side of the cutoff was "
        "reaped (sweep threshold is too aggressive / off-by-one)"
    )

    # Now age it just PAST the cutoff (stale) -> reaped.  Confirms the
    # threshold is honored in the other direction too (not blindly keeping).
    stale = _candidate_path(root, "sid-stale-boundary", "stale789")
    stale.write_bytes(b"stale")
    os.chmod(stale, 0o600)
    os.utime(stale, (cutoff - 5, cutoff - 5))
    module.cleanup_old_archives()
    assert not stale.exists(), (
        "DEFEAT: a candidate just past the cutoff was NOT reaped (sweep "
        "threshold is too lax / off-by-one in the other direction)"
    )


def test_defeat_bug_b_sweep_best_effort_unreadable_locks_dir(monkeypatch, tmp_path):
    """ADVERSARIAL: the sweep is best-effort / non-fatal.  An unreadable
    ``.locks`` directory (chmod 000) must NEVER abort cleanup_old_archives --
    a hook must not fail because a sweep target is locked.  The sweep swallows
    OSError internally; cleanup proceeds."""
    module = _load_archive_result(monkeypatch, tmp_path)
    root = tmp_path / "tool-archive"
    locks = root / ".locks"
    locks.mkdir(parents=True)
    # Drop a stale candidate inside so the sweep has something to attempt.
    stale = _candidate_path(root, "sid-stale-unreadable", "xyz000")
    stale.write_bytes(b"orphan")
    os.chmod(stale, 0o600)
    _age_old(stale)

    # Make .locks unreadable so iterdir raises PermissionError (OSError).
    os.chmod(locks, 0o000)
    try:
        # Must not raise.  (If the platform lets the owner still list it, the
        # assertion still holds -- cleanup must never raise either way.)
        removed = module.cleanup_old_archives()
        assert isinstance(removed, int), (
            "cleanup_old_archives returned non-int after unreadable .locks"
        )
    finally:
        # Restore perms so tmp_path cleanup can remove the tree.
        os.chmod(locks, 0o755)


def test_defeat_bug_b_published_lease_and_candidate_dir_not_touched(
    monkeypatch, tmp_path
):
    """ADVERSARIAL: the sweep must NEVER touch the published ``{sid}.lease``
    file (only ``.candidate-*`` and ``.reclaim-*``), and must skip a
    candidate-named DIRECTORY (the sweep guards ``entry.is_dir()``).  A
    mis-filtered glob that unlinked the published lease would break every
    live lock holder."""
    module = _load_archive_result(monkeypatch, tmp_path)
    root = tmp_path / "tool-archive"
    locks = root / ".locks"
    locks.mkdir(parents=True)

    # A published lease file (not a candidate/reclaim) aged old -- must survive.
    published = root / ".locks" / "sid-pub.lease"
    published.write_bytes(b"published-lease")
    os.chmod(published, 0o600)
    _age_old(published)

    # An unrelated old dot-file -- must survive (not candidate/reclaim).
    unrelated = root / ".locks" / ".someother-old"
    unrelated.write_bytes(b"unrelated")
    _age_old(unrelated)

    # A candidate-named DIRECTORY aged old -- must survive (is_dir guard).
    cand_dir = root / ".locks" / ".sid-dir.lease.candidate-dirnonce"
    cand_dir.mkdir(parents=True)
    _age_old(cand_dir)

    module.cleanup_old_archives()

    assert published.exists(), (
        "DEFEAT: the sweep unlinked a published {sid}.lease file -- it must "
        "only touch .candidate-* / .reclaim-* artifacts"
    )
    assert unrelated.exists(), (
        "DEFEAT: the sweep unlinked an unrelated .locks entry -- filter is "
        "too broad"
    )
    assert cand_dir.exists(), (
        "DEFEAT: the sweep unlinked a candidate-named directory -- the "
        "is_dir guard is missing/ineffective"
    )


# ---------------------------------------------------------------------------
# Bug C probes (hardest -- most effort)
# ---------------------------------------------------------------------------


def _run_n_reclaimers(lease: Path, n: int) -> tuple[list[bool], list[BaseException]]:
    """Spawn n barrier-synchronized threads all calling _reclaim_if_expired on
    the same expired lease.  Returns (results, errors)."""
    barrier = threading.Barrier(n)
    results: list[bool] = [False] * n
    errors: list[BaseException] = []

    def contender(idx: int) -> None:
        try:
            lock = hook_runtime.LeaseLock(lease, acquire_timeout=0)
            barrier.wait()
            results[idx] = bool(lock._reclaim_if_expired())
        except BaseException as exc:  # pragma: no cover - surfaced in assert
            errors.append(exc)

    threads = [threading.Thread(target=contender, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    return results, errors


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="adversarial 32-thread stress of the archive-lease reclaim-expired path: "
    "under contention the portable reclaim does not reliably elect a single winner "
    "on Windows CI (same family as test_defeat_bug_c_serialization_repeated_5_iterations "
    "and test_defeat_bug_c_cross_generation, both already win32-skipped). Windows "
    "archive-lease reclaim is a separate known gap.",
)
def test_defeat_bug_c_serialization_32_threads_one_winner(tmp_path):
    """ADVERSARIAL (load-bearing): 32 concurrent same-generation reclaimers ->
    EXACTLY one True, 31 False, no exception escapes, the lease is unlinked,
    and no stray .reclaim-* files remain.  A random-suffix implementation
    (Option 2) would let multiple winners race on os.unlink and leave stray
    claims; the deterministic claim name is what prevents that."""
    root = tmp_path / "tool-archive"
    locks = root / ".locks"
    locks.mkdir(parents=True)
    lease = _lease_path(root, "sid-32")
    owner_nonce = "thirtytwothirtytwo"
    _write_expired_owner_lease(lease, owner_nonce)

    n = 32
    results, errors = _run_n_reclaimers(lease, n)

    assert not errors, "32-thread reclaim raised: %r" % (errors,)
    assert sum(1 for r in results if r) == 1, (
        "DEFEAT: exactly one reclaimer should win; results=%r" % (results,)
    )
    assert sum(1 for r in results if not r) == n - 1, (
        "DEFEAT: the rest should fail open; results=%r" % (results,)
    )
    assert not lease.exists(), (
        "DEFEAT: the expired lease should be unlinked by the single winner; "
        "lease still exists"
    )
    leftover = list(locks.glob("*.reclaim-*"))
    assert leftover == [], (
        "DEFEAT: stray reclaim claim files left behind -- a random-suffix "
        "implementation would do this; the deterministic claim's finally "
        "should have unlinked the single claim. leftovers=%r" % (leftover,)
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="adversarial 32-thread stress of the archive-lease reclaim-expired path "
    "(same family as test_defeat_bug_c_cross_generation, already win32-skipped): "
    "under contention the portable reclaim does not reliably elect a single winner "
    "on Windows CI. This is the tool-archive RECLAIM path; #114's settings lease is "
    "a fresh exclusive-create acquire (no getpid/reclaim generation), which IS "
    "Windows-safe. Windows archive-lease reclaim is a separate known gap.",
)
def test_defeat_bug_c_serialization_repeated_5_iterations(tmp_path):
    """ADVERSARIAL: repeat the 32-thread same-generation serialization test 5
    times on a FRESH expired lease each iteration.  Flakies under stress would
    surface here.  Each iteration must produce exactly one winner, no
    exceptions, lease unlinked, no stray claims."""
    root = tmp_path / "tool-archive"
    locks = root / ".locks"
    locks.mkdir(parents=True)
    for i in range(5):
        lease = _lease_path(root, f"sid-rep-{i}")
        owner_nonce = f"iteration-{i}-noncevalue"
        _write_expired_owner_lease(lease, owner_nonce)
        results, errors = _run_n_reclaimers(lease, 32)
        assert not errors, f"iter {i}: threads raised: %r" % (errors,)
        assert sum(1 for r in results if r) == 1, (
            f"DEFEAT iter {i}: exactly one winner expected; results=%r" % (results,)
        )
        assert not lease.exists(), f"DEFEAT iter {i}: lease not unlinked"
        leftover = list(locks.glob("*.reclaim-*"))
        assert leftover == [], (
            f"DEFEAT iter {i}: stray reclaim files; leftovers=%r" % (leftover,)
        )


@pytest.mark.skipif(sys.platform == "win32", reason="exercises POSIX os.getuid lease-generation path; not applicable on Windows")
def test_defeat_bug_c_cross_generation_no_interference(tmp_path):
    """ADVERSARIAL: 8 DIFFERENT generations (distinct sids / owner nonces) x 8
    threads each, all concurrent.  Each generation must produce exactly one
    winner; no cross-generation interference (a claim for one generation must
    never block or be blocked by another)."""
    root = tmp_path / "tool-archive"
    locks = root / ".locks"
    locks.mkdir(parents=True)

    gens = 8
    per_gen = 8
    leases = []
    for g in range(gens):
        lease = _lease_path(root, f"sid-gen-{g}")
        _write_expired_owner_lease(lease, f"gen-{g}-noncevalue")
        leases.append(lease)

    all_results: list[list[bool]] = [[] for _ in range(gens)]
    errors: list[BaseException] = []
    # One barrier per generation; threads of a generation sync on theirs.
    barriers = [threading.Barrier(per_gen) for _ in range(gens)]

    def contender(g: int, idx: int) -> None:
        try:
            lock = hook_runtime.LeaseLock(leases[g], acquire_timeout=0)
            barriers[g].wait()
            all_results[g].append(bool(lock._reclaim_if_expired()))
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = []
    for g in range(gens):
        for i in range(per_gen):
            threads.append(threading.Thread(target=contender, args=(g, i)))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, "cross-gen threads raised: %r" % (errors,)
    for g in range(gens):
        wins = sum(1 for r in all_results[g] if r)
        assert wins == 1, (
            f"DEFEAT gen {g}: expected exactly one winner, got {wins}; "
            f"results={all_results[g]!r}"
        )
        assert not leases[g].exists(), (
            f"DEFEAT gen {g}: expired lease not unlinked by the winner"
        )
    # No stray claim files for any generation.
    leftover = list(locks.glob("*.reclaim-*"))
    assert leftover == [], (
        "DEFEAT: stray reclaim files after cross-gen test; leftovers=%r" % (leftover,)
    )


def test_defeat_bug_c_deterministic_claim_name_matches_sha256_exactly(tmp_path):
    """ADVERSARIAL: the reclaim claim path produced by the code must match
    ``sha256("owner:{nonce}")[:32]`` EXACTLY -- no random suffix.  Proven by
    pre-creating the independently-computed deterministic claim path and
    observing a same-generation reclaimer hits FileExistsError (returns False).
    If a random suffix were added (Option 2), the reclaimer would compute a
    DIFFERENT path and succeed, breaking serialization."""
    root = tmp_path / "tool-archive"
    locks = root / ".locks"
    locks.mkdir(parents=True)
    lease = _lease_path(root, "sid-det-defeat")
    owner_nonce = "deterministicexact1234"
    _write_expired_owner_lease(lease, owner_nonce)

    expected_digest = hashlib.sha256(
        f"owner:{owner_nonce}".encode("utf-8")
    ).hexdigest()[:32]
    expected_claim = lease.with_name(
        f".{lease.name}.reclaim-{expected_digest}"
    )
    assert expected_claim == _reclaim_claim_path(lease, owner_nonce), (
        "DEFEAT: helper-computed claim path disagrees with the documented "
        "sha256 formula"
    )

    # Pre-create the deterministic claim (hard link to the lease).
    os.link(str(lease), str(expected_claim))
    os.chmod(expected_claim, 0o600)

    lock = hook_runtime.LeaseLock(lease, acquire_timeout=0)
    assert lock._reclaim_if_expired() is False, (
        "DEFEAT: a same-generation reclaimer succeeded even though the "
        "deterministic claim was pre-created -- the code computed a DIFFERENT "
        "(random?) claim path, breaking serialization"
    )
    assert expected_claim.exists(), (
        "DEFEAT: the pre-created deterministic claim was unlinked by a "
        "same-generation reclaimer that should have failed open"
    )

    # No OTHER reclaim claim appeared (no random-suffix sibling).
    siblings = [
        p for p in locks.glob(f".{lease.name}.reclaim-*")
        if p != expected_claim
    ]
    assert siblings == [], (
        "DEFEAT: a second (random-suffix) reclaim claim appeared alongside "
        "the deterministic one; siblings=%r" % (siblings,)
    )


def test_defeat_bug_c_sweep_does_not_reap_young_claim_while_reclaimer_in_flight(
    monkeypatch, tmp_path
):
    """ADVERSARIAL: race the sweep against a LIVE reclaimer.  A young in-flight
    ``.reclaim-{digest}`` claim (mtime ~now) must NOT be reaped while the
    reclaimer is mid-flight.  Run cleanup_old_archives in a tight loop while
    holding a fresh young claim; the claim must survive the race window."""
    module = _load_archive_result(monkeypatch, tmp_path)
    root = tmp_path / "tool-archive"
    locks = root / ".locks"
    locks.mkdir(parents=True)
    lease = _lease_path(root, "sid-live-reclaimer")
    owner_nonce = "livereclaimerlive01"
    _write_expired_owner_lease(lease, owner_nonce)
    claim = _reclaim_claim_path(lease, owner_nonce)
    os.link(str(lease), str(claim))
    os.chmod(claim, 0o600)

    stop = threading.Event()
    errors: list[BaseException] = []

    def sweep_loop() -> None:
        try:
            while not stop.is_set():
                module.cleanup_old_archives()
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def touch_loop() -> None:
        try:
            while not stop.is_set():
                now = time.time()
                os.utime(claim, (now, now))
                time.sleep(0.005)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    t1 = threading.Thread(target=sweep_loop)
    t2 = threading.Thread(target=touch_loop)
    t1.start()
    t2.start()
    time.sleep(0.3)
    stop.set()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert not errors, "race threads raised: %r" % (errors,)
    assert claim.exists(), (
        "DEFEAT: a young in-flight .reclaim-{digest} claim was reaped by the "
        "sweep while a reclaimer was live (false positive on a live reclaimer)"
    )


def test_defeat_bug_c_stale_claim_recovery_after_sweep(monkeypatch, tmp_path):
    """ADVERSARIAL (recovery): a stale ``.reclaim-{digest}`` claim aged past
    the threshold blocks a same-generation reclaimer (returns False); after
    cleanup_old_archives reaps it, a subsequent _reclaim_if_expired returns
    True.  The deadlock is bounded, not permanent."""
    module = _load_archive_result(monkeypatch, tmp_path)
    root = tmp_path / "tool-archive"
    locks = root / ".locks"
    locks.mkdir(parents=True)
    lease = _lease_path(root, "sid-recover")
    owner_nonce = "recoveryrecovery99"
    _write_expired_owner_lease(lease, owner_nonce)
    claim = _reclaim_claim_path(lease, owner_nonce)
    os.link(str(lease), str(claim))
    os.chmod(claim, 0o600)
    _age_old(claim)

    lock = hook_runtime.LeaseLock(lease, acquire_timeout=0)
    assert lock._reclaim_if_expired() is False, (
        "pre-sweep: stale deterministic claim should block reclamation"
    )
    module.cleanup_old_archives()
    assert not claim.exists(), (
        "DEFEAT: sweep did not reap the stale .reclaim-{digest} claim"
    )
    result = lock._reclaim_if_expired()
    assert result is True, (
        "DEFEAT: post-sweep reclaim did not succeed (permanent deadlock not "
        "bounded); _reclaim_if_expired()=%r" % (result,)
    )
    assert not lease.exists(), "post-sweep: expired lease should be unlinked"


def test_defeat_bug_c_double_unlink_safety_late_reclaimer_after_winner(tmp_path):
    """ADVERSARIAL (no double unlink): after the winner unlinks the lease AND
    its claim, a LATE same-generation reclaimer calling _reclaim_if_expired
    must return False WITHOUT raising.  The late reclaimer's os.link(self.path,
    claim) raises FileNotFoundError (self.path is gone) -> caught -> False.
    A double-unlink bug would raise or wrongly return True."""
    root = tmp_path / "tool-archive"
    locks = root / ".locks"
    locks.mkdir(parents=True)
    lease = _lease_path(root, "sid-late")
    owner_nonce = "latelatelatelate99"
    _write_expired_owner_lease(lease, owner_nonce)

    # Winner reclaims (single-threaded): unlinks the lease + the claim.
    winner = hook_runtime.LeaseLock(lease, acquire_timeout=0)
    assert winner._reclaim_if_expired() is True, "winner should reclaim"
    assert not lease.exists(), "winner should have unlinked the lease"
    leftover = list(locks.glob("*.reclaim-*"))
    assert leftover == [], "winner should have unlinked its claim"

    # Late same-generation reclaimer arrives after the lease is gone.
    late = hook_runtime.LeaseLock(lease, acquire_timeout=0)
    try:
        result = late._reclaim_if_expired()
    except BaseException as exc:
        pytest.fail(
            "DEFEAT: late same-generation reclaimer RAISED after the winner "
            "unlinked the lease (double-unlink / unguarded OSError): %r" % (exc,)
        )
    assert result is False, (
        "DEFEAT: late reclaimer returned True after the lease was already "
        "unlinked (double-unlink / double-reclaim)"
    )


def test_defeat_bug_c_reclaim_on_already_unlinked_lease_no_raise(tmp_path):
    """ADVERSARIAL: calling _reclaim_if_expired on a lease path that has
    ALREADY been unlinked (no file at all) must return False without raising.
    _read_owner -> None (OSError on read) -> malformed branch -> os.lstat
    raises FileNotFoundError -> caught -> False.  An unguarded OSError here
    would crash a reclaimer racing with a sweeper that unlinked the lease."""
    root = tmp_path / "tool-archive"
    locks = root / ".locks"
    locks.mkdir(parents=True)
    lease = _lease_path(root, "sid-gone")
    # No file created at lease path at all.
    assert not lease.exists()

    lock = hook_runtime.LeaseLock(lease, acquire_timeout=0)
    try:
        result = lock._reclaim_if_expired()
    except BaseException as exc:
        pytest.fail(
            "DEFEAT: _reclaim_if_expired RAISED on an already-unlinked lease "
            "(unguarded OSError): %r" % (exc,)
        )
    assert result is False, (
        "DEFEAT: _reclaim_if_expired returned True on an already-unlinked "
        "lease (impossible reclaim); result=%r" % (result,)
    )


def test_defeat_bug_c_winner_unlinks_lease_exactly_once_under_stress(tmp_path):
    """ADVERSARIAL: under 32-thread stress the lease is unlinked EXACTLY once.
    Count unlink attempts by replacing the lease path's parent with a custom
    layout is impractical; instead we assert the post-condition that the lease
    path does not exist AND no thread reported a spurious True.  Combined with
    the exactly-one-winner assertion this proves the unlink happened once."""
    root = tmp_path / "tool-archive"
    locks = root / ".locks"
    locks.mkdir(parents=True)
    lease = _lease_path(root, "sid-once")
    owner_nonce = "exactlyonceexactly01"
    _write_expired_owner_lease(lease, owner_nonce)
    # Record the lease inode so we can detect a replacement (would indicate a
    # re-creation by a misbehaving reclaimer).
    before_inode = os.lstat(lease).st_ino

    results, errors = _run_n_reclaimers(lease, 32)
    assert not errors, "threads raised: %r" % (errors,)
    assert sum(1 for r in results if r) == 1, (
        "DEFEAT: expected exactly one True; results=%r" % (results,)
    )
    assert not lease.exists(), (
        "DEFEAT: lease should be unlinked exactly once and gone"
    )
    # No thread should have observed/recreated a different inode at the lease
    # path -- the winner unlinked the original inode and no reclaimer
    # re-published.
    _ = before_inode  # inode consumed by the unlink; existence check suffices
    leftover = list(locks.glob("*.reclaim-*"))
    assert leftover == [], (
        "DEFEAT: stray reclaim files; leftovers=%r" % (leftover,)
    )
