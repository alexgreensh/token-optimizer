# Torture-review log — K1 + K2 patch families

Scope (provenance, standalone recomposition on base 2c3412ca): K2 =
`pr-k2-nb` (read-cache reset-epoch guard: `reset_epoch.py`, `session_store.py`,
`read_cache.py` x3 mirrors + race/integration/closure tests), K3 = `pr-k3-nb`
(antigravity bridge) on K2, K1 = `pr-k1-nb` (keep-warm arm dedup,
`_measure/cache_policy.py` x3 mirrors + `tests/test_keepwarm_arm_dedup.py`) on
K3. K1 is code-reviewed (rounds in the K1 section); its doc wording is
implemented-in-this-change but not released until merge. Each round is a distinct adversarial
perspective with the check actually run and its evidence. Verdicts: PASS / PASS-NOTE
(passes with an observation) / FAIL (blocks).

## K2 (reset-epoch guard)

1. **Concurrency / atomicity** — PASS. Epoch check and insert run inside one
   `BEGIN IMMEDIATE` transaction (`reset_epoch.py::ResetEpochMixin.upsert_file_entry`, mixed into `SessionStore`); the reset
   bump is inside the reset's own `with conn:` transaction, so no interleaving can
   pass the check yet lose the row. Evidence: `test_two_process_interleaving`
   (subprocess writer sleeps 1.0s in the judge->write window; resetter commits at
   0.3s; write dropped, row absent).
2. **Failure modes / fail-open parity** — PASS. Under a sibling write lock,
   `BEGIN IMMEDIATE` honors the same 50ms `busy_timeout` the legacy deferred INSERT
   honored, raising the same `OperationalError` at the same call sites (call sites
   unchanged, so surrounding try/except parity is exact). On any exception the
   guarded path rolls back before re-raising; a dangling open transaction from a
   prior failed write is healed by that rollback (strictly better than pre-fix,
   which left it open on the cached connection).
3. **Schema / migration on old DBs** — PASS. `session_meta(key TEXT PRIMARY KEY)`
   (schema line 119) supports the `ON CONFLICT(key)` bump; a pre-upgrade db has no
   `reset_epoch` row, so `get_reset_epoch()` returns 0 and the guarded upsert's
   absent-row read compares 0 == 0 and writes. First reset on an old db inserts
   epoch 1. Evidence: `test_reset_bumps_epoch`, `test_guarded_upsert_succeeds_when_epoch_unchanged`.
4. **Legacy caller contract** — PASS. Signature is additive
   (`expected_reset_epoch=None`); all pre-existing callers (`_check_escape_hatch`
   line 635, `handle_invalidate` line 2325) are unconditional by design and cannot
   flip a Read judgment to redundant without a `file_reads` row, which only the
   guarded writers create post-reset. Return type changed None -> bool; no caller
   consumed the old return. Verified by grep: exactly 8 upsert_file_entry call
   sites repo-wide (6 guarded in handle_read, 2 intentionally legacy).
5. **Multiple resets / epoch monotonicity** — PASS. Epoch bumps per reset
   (0->1->2 pinned by `test_reset_bumps_epoch`); a writer holding any stale epoch
   is dropped. `clear_file_entries` (subagent path) bumps too
   (`test_clear_file_entries_bumps_epoch`).
6. **Performance / hot path** — PASS. One extra `session_meta` SELECT per
   `handle_read` (indexed PRIMARY KEY lookup) and one IMMEDIATE transaction per
   recorded read (the legacy path already took the same write lock at INSERT).
   No measurable addition to the 10s hook budget.
7. **Reachability honesty** — PASS. Doc and G2 draft label K2
   confirmed-storage-race with conditional, unproven per-host product reachability;
   primary-host evidence recorded (all read_cache and SessionStart hooks in
   `hooks/hooks.json` are `async: false`, and /clear and auto-compact land between
   turns). Not filed as a confirmed user-facing bug.

## K1 (keep-warm arm dedup)

8. **Dedup identity contract** — PASS-NOTE. Dedup keys on
   (session_id, transcript_mtime, byte_size) and only requires that a byte-identical
   snapshot is the same pause — true for append-only JSONL; no global
   size+mtime uniqueness assumed. Rotation/truncation changes the snapshot and
   re-arms (`test_truncated_rotated_transcript_rearms`). Note: coarse-mtime
   filesystems (FAT, some NFS) widen the identical-snapshot window but cannot
   create a false positive without a same-size non-append rewrite, which JSONL
   transcripts never do.
9. **Lock coverage / tick-arm interleave** — PASS. Dedup check-and-append runs
   under `_keepwarm_lock()` with the append body inlined in
   `write_keepwarm_arm_record` (no new module-level symbol — lane-4 ownership
   contract intact); `compact_keepwarm_sidecar` takes the same lock, so a tick
   cannot compact between check and append. Racing arms collapse to one record
   (`test_concurrent_arms_collapse_to_one_record`, threaded barrier).
10. **Blast radius / regression sweep** — PASS. 6/6 new dedup tests; 136 passed
    across arm-dedup + lane4 ownership + stop_runner + plist_heal + cloud_parity;
    64 passed across read-cache/session-store/reset suites after K2; both mirrors
    byte-identical per `scripts/check-mirror-sync.sh` (run after every commit,
    last run green at the recomposed PR heads).


## K3 (Antigravity rollup-lease fail-closed) — delta rounds

11. **Failure modes / fail-closed direction** — PASS. Any OSError from the lease
    touch skips the spawn pair. Evidence: real permission repro
    (`test_stop_fails_closed_when_lease_touch_fails`, 0o500 dir — 4 spawns from 2
    stops pre-fix, 0 after) plus a portable seam test
    (`test_stop_fails_closed_when_lease_touch_raises`, Path.touch patched to
    raise) for platforms where directory permissions do not block file creation.
    `_to_dir` never returns None (unconditional `home / "token-optimizer"`), so
    no TypeError path exists between the held-check and the touch.
12. **API / caller contract** — PASS. `_touch_rollup_lease` return None -> bool;
    sole caller is `handle_stop` (grep-verified across all three mirror trees and
    tests). Pre-existing tests monkeypatch `_rollup_lease_held`, not the touch,
    and stay green.
13. **Concurrency honesty** — PASS-NOTE. No lock serializes check+touch: two
    concurrent stops can both observe a free lease and both spawn. Impact is one
    duplicate idempotent rollup+dashboard spawn (atomic os.replace dashboard
    publish, WAL trends.db), not a storm; the Cursor bridge closed this same
    class with a lease lock. Recorded as a documented residual in the K3 finding
    and G3 draft; deliberately not fixed in this minimal change.
14. **Healthy-path parity + self-healing** — PASS. Free-lease spawn and
    lease-held skip pinned by pre-existing tests plus
    `test_stop_still_spawns_when_lease_writeable`; 21/21 bridge tests green. When
    the data dir recovers, the next stop's touch succeeds and normal throttled
    behavior resumes (30s lease TTL) — the fail-closed state is not sticky.
15. **Blast radius / scope discipline** — PASS. Behavior changes only on the
    write-failure path (previously a spawn storm); healthy installs are
    byte-identical in behavior. No `_measure` changes (lane-4 ownership
    untouched); mirrors byte-identical (`check-mirror-sync.sh` green after each
    commit). Zero-token local spawns only; no paid inference anywhere in the
    repro (spawn_detached stubbed capture).


## K3 artifact torture rounds (10 distinct perspectives on the final PR-3 artifact)

Rounds 11-15 above were preliminary (pre-artifact). The rounds below are the
countable set, executed against the final `pr-k3-antigravity-lease` head.

16. **Adversarial storm variants** — PASS. Data path as a regular FILE (mkdir
    FileExistsError) → 0 spawns; parent-dir writable but lease create denied
    (0o500) → 0 spawns from 2 stops (pre-fix: 4). Executable probes, this run.
17. **TTL boundary** — PASS. Lease at 29.9s → held (skip); at 30.1s → free
    (spawn). `_ROLLUP_LEASE_SECONDS = 30` honored exactly.
18. **Clock skew** — PASS. Lease mtime 10min in the future → negative age →
    held → 0 spawns, no crash; throttling resumes as real time passes.
19. **Concurrent stops (residual verification)** — PASS-NOTE, corrected after
    independent review. Two threads barrier-synced at the held-check produced a
    single winner in my run, but the reviewer's 10-concurrent-stop run observed
    6 spawns (3 duplicate pairs): the check→touch window is NOT atomic, so
    concurrent stops CAN duplicate-spawn — there is no single-winner guarantee.
    Bounded by stop-event count, spawns are idempotent, documented in the
    finding and G3 as a candidate lease-lock follow-up, deliberately out of
    this minimal fix.
20. **Full-suite regression at PR-3 head** — PASS. 63 passed across bridge,
    install, install-atomicity, state, and session antigravity suites.
21. **Mirror parity at PR-3 head** — PASS. `check-mirror-sync.sh` green
    (plugins + cowork byte-identical).
22. **Caller contract on final artifact** — PASS. `_touch_rollup_lease` has
    exactly one caller per mirror tree (`handle_stop`); no test or module
    consumes the old None return.
23. **Doc coherence on final artifact** — PASS. K3 finding states the
    fail-closed rule, the 30s TTL, the red-repro numbers (4 spawns from 2
    stops pre-fix), and the residual; G3 marked HELD for trusted-channel
    review; no filing language.
24. **Consent semantics parity** — PASS. `_consent_ok` gates only the
    pre-invocation/preToolUse rewrite path (line 495); `handle_stop` rollups
    were never consent-gated and are unchanged — the fix alters behavior only
    on the lease-write-failure path.
25. **Self-healing / non-stickiness** — PASS. After a degraded-dir window (0
    spawns), restoring writability makes the very next stop spawn the pair
    again (2 spawns) — fail-closed is not a latched outage.

## Known gaps handed to the independent judge

- ~~No end-to-end `handle_read` integration test~~ CLOSED (delta):
  `tests/test_read_cache_reset_epoch_integration.py` drives the real
  `handle_read` PreToolUse path with a simulated concurrent resetter committing
  inside the judge->write window; the write is dropped (row absent, epoch 1)
  and the read decision stays fail-open. Healthy-path recording pinned by the
  companion test.
- Per-harness (non-Claude) event-delivery ordering for K2 remains unproven from
  this repo; the fix's value there is inferred, not demonstrated.
- Consent-test environment sensitivity (U0) is unresolved but out of scope.

## K2 F3/F4 v2 artifact torture rounds (after judge NOT-CLEAR on v1)

v1 failed review on two real blockers: the escape-hatch/invalidate guards were
never wired into the production call sites (tests injected the argument into
the helper directly), and corrupt-current normalization to 0 accepted a stale
epoch-0 writer. v2 rewires the callers, changes corrupt handling to
drop-and-return-False, and replaces helper-level tests with real-path ones.
Rounds below run against the final v2 artifact.

26. **Race suite (store-level)** — PASS. 19 pytest cases across 13 test functions: epoch bumps on both reset
    paths, drop-after-reset, unchanged-epoch accept, unguarded legacy accept,
    two-process interleaving repro, 0/positive matching-epoch controls,
    corrupt-current drop + reset heal, stale-real-vs-corrupt drop,
    7-value bad-stored-type sweep (5.7, "", 1e3, NaN, inf, 0x10, " "),
    unparseable-expected drop (no raise), whitespace-padded integer accept.
27. **Integration suite (real path)** — PASS. 6 passed: handle_read
    first-read drop + no-reset control, block-mode hatch branch drop,
    eligible-structure hatch branch drop, handle_invalidate mid-hook drop +
    no-reset control. Hatch tests patch only the MAX_CONSECUTIVE_DENIALS
    constant (the counter is in-memory; see R34) and assert the raced write
    carries consecutive_denials == 0, pinning it to the hatch, not the deny
    path.
28. **Mutation battery** — PASS, 4/4 killed. Unwiring the eligible-branch
    call, the block-branch call, or the handle_invalidate rewrite each fails
    its pinned test; reverting corrupt handling to normalize-to-0 fails the
    corrupt tests. (First M2 attempt silently no-op'd the pattern replace;
    re-ran with an asserted mutation — killed.)
29. **Adjacent read-cache suites** — PASS. 53 passed across race, integration,
    clear-compacted, first-read-retarget, marker-key-redaction at PR-2 head.
30. **Per-commit history hygiene** — PASS. All 5 commits of the rewritten
    series carry a docs/STATE-MACHINES.md with zero conflict markers
    (scanned each commit's blob).
31. **Production caller audit** — PASS. grep-verified: every
    `upsert_file_entry(` call in read_cache.py passes `expected_reset_epoch=`
    (6 handle_read sites, the hatch internal write, the invalidate rewrite);
    no unguarded call remains.
32. **Mirror parity** — PASS. check-mirror-sync.sh green at PR-2 head
    (plugins + cowork byte-identical).
33. **Full stack regression** — PASS (historical artifact evidence, NOT re-executed
    as a K1+K2+K3 stack on the standalone branches). 63 passed across K1 keepwarm,
    K2 race+integration, K3 antigravity suites at the old stacked PR-3 head
    `0c705b8f` (K1 code present there). The standalone branches contain no K1
    code; their own executed suites are recorded in rounds 36-45 below.
34. **Escape-hatch reachability (execution)** — CONFIRMED COLD, repro
    preserved. `consecutive_denials` has no `file_reads` column, so it is
    in-memory only and resets every hook process. `tests/repro_escape_hatch_cold.py`
    (not pytest-collected) seeds a redundant-judged file and runs THREE fresh
    python processes through the real `handle_read`: all 3 judged with
    counter=0, 0 escapes — the hatch that exists to bound denial loops cannot
    fire in hook-per-process hosts. The F3 guard on the hatch is therefore
    defense-in-depth on a currently-cold path. Persisting the counter is a
    candidate follow-up (K4, would make the hatch live); NOT fixed in this
    packet — held for triage after K2 content clears.
35. **Heal-direction check (analysis)** — PARTIAL, universal claim RETRACTED.
    The reset bump is `CAST(CAST(value AS INTEGER) + 1 AS TEXT)`: a corrupt
    value heals to '1' on the next reset, possibly LOWER than the
    pre-corruption epoch. A stale writer holding a HIGHER epoch mismatches and
    drops. But the ABA interleaving old-writer(1) → reset(2) → corrupt(junk) →
    reset(heals to '1') leaves current == the stale writer's epoch: accepted,
    and the pre-reset row resurrects. The guard does NOT bound stale writers
    across external corruption; the residual requires a non-integer value
    landing in `session_meta` (no product writer produces one — external/manual
    db tampering or an unrelated bug) PLUS a reset PLUS a colliding stale
    writer. Recorded as an explicit corruption residual in the doc, not a
    universal safety claim; widening the design (e.g., monotonic tokens) is out
    of scope for K2.

## New-base (2c3412ca) standalone variant rounds

Executed after rebasing onto main 2c3412ca (#316: tests.yml-only delta) and the
file-size refactor. Runtime clearance of the previous K2/K3 code is retained only
for the unchanged K3 files; K2 runtime CHANGED (guard moved into `reset_epoch.py`),
so rounds 38-41 re-test it fresh.

36. **File-size gate (CI "File size")** — FOUND A REAL FAILURE, FIXED.
    `scripts/check_file_size.py --base 86d9374b` failed on the pre-refactor K2 tree:
    `read_cache.py` 2514 -> 2533 and `session_store.py` 1097 -> 1175 (legacy files
    over 1000 lines may only shrink; 6 failures across 3 mirrors). The prior judge
    clearance did not run this gate. Fix: new `reset_epoch.py`; after the fix
    `read_cache.py` 2514 -> 2513, `session_store.py` 1097 -> 1060 (both shrink),
    `reset_epoch.py` 163 lines, and the gate reports OK against base 2c3412ca
    (17 files checked at K2, 26 at K1 head). No exemption written.
37. **Refactor defect caught by tests** — PASS after fix. The first extraction
    omitted `import time` in `reset_epoch.py` (NameError in the legacy insert);
    26 tests failed immediately (`53 passed` after the fix). AST undefined-name
    scan of all three touched modules afterwards: clean (only `__file__`).
38. **Mutation re-check at the new location** — PASS. Four asserted mutations of
    `reset_epoch.py` (drop the epoch compare; normalize corrupt to 0; no bump; snapshot
    always None) each killed by the race/integration suites (M1-M4 KILLED).
39. **Install closure** — PASS for what is enforced. New
    `tests/test_reset_epoch_module_closure.py` (3 tests): `reset_epoch.py` byte-identical
    in skills/, plugins/ and cowork/ trees; only `read_cache.py` and `session_store.py`
    import it; Devin's closed-set loader lists it in `REPO_MODULES` (Devin PAYLOAD does
    not ship read_cache/session_store, so the module is correctly not added there).
    Hermes runs `read_cache.py` from the checkout (not copied). Mirror sync green;
    `tests/test_devin_closedset.py` 226 passed.
40. **Adjacent suites at the K2 refactor** — PASS. 53 passed (race, integration,
    clear-compacted, first-read-retarget, marker-key-redaction); 140 passed / 5 skipped
    across release-installable, measure-split-packaging, desktop packaging,
    no-window, session-store FTS, double-fire, hook budgets, antigravity.
41. **Legacy-write parity** — PASS. `expected_reset_epoch=None` still takes the
    unconditional insert + commit; the moved `_insert_file_entry` body is verbatim
    (the K2 doc/code diff vs the cleared tree is the move only).
42. **Split/doc gates** — PASS. `check_measure_split` 1258 defs / 0 remaining;
    `check_docs_claims` OK; `check_readme_integrity` OK; measure-split
    artifacts/compat/deferred/golden/packaging 99 passed at the K1 head.
43. **Lane replays (frozen lane 4)** — K1 HEAD ONLY. `test_measure_split_lane4_replay.py`:
    26 passed, 15 failed at the K1 head (the known frozen-body proof, F1). Per #316
    those jobs run only for cloud/split-* branches or by hand, and normal CI pytest
    ignores `tests/test_measure_split_lane*.py` (tests.yml line 101). K2/K3 heads: 41
    passed. No proof file touched.
44. **Regression-guards (check_regression_guards.py)** — PARTIAL. Shards 1-3 of 4 at
    the K1 head: failures only in `tests/test_hook_wire_contract.py` (sandbox
    `PermissionError: /root/.cargo/bin/schtasks`); the same test file's shim test
    fails identically on a pristine 2c3412ca worktree (3 failed), so it is
    environmental, not caused by this change. Shard 4 and a full base comparison of
    `test_hooks_run_when_path_has_no_bash` were NOT completed (run timed out):
    UNVERIFIED, left for CI.
45. **Provenance / identity** — PASS. K1 `_measure/cache_policy.py` (x3) and
    `tests/test_keepwarm_arm_dedup.py` byte-identical to the cleared K1 branch
    `93e78486`; K3 antigravity bridge + tests byte-identical to `955fa4aa`'s;
    docs pending/implemented wording flips only at the K1 head.

## K2 extraction (`reset_epoch.py`) fresh torture rounds, executed

Script: `torture_reset_epoch.py` (attached to the packet, not in the repo), run against all
three trees (skills/, plugins/, cowork/) at the K2 refactor. Interpreter: CPython 3.12
only. NOT tested: a 3.9 or 3.10 interpreter (grammar parse only, round 53), PyPy,
Windows, macOS, hooks driven by a real host harness, or the per-host installer scripts.

46. **MRO and signature** — PASS x3. `SessionStore.__mro__[1]` is `ResetEpochMixin`;
    `upsert_file_entry` resolves to the mixin function and is not shadowed in the class
    dict; parameters are `(self, file_path, entry, expected_reset_epoch)`.
47. **Import isolation and cycles** — PASS x3. Fresh interpreter importing only
    `reset_epoch`, only `session_store`, only `read_cache` each succeeds.
48. **Partial install** — PASS (loud failure). A scripts copy with `reset_epoch.py`
    deleted: `import read_cache` and `import session_store` both fail with
    `ModuleNotFoundError: reset_epoch`. This differs from the old optional-import
    behavior for session_store; closure is guarded by round 39 and the closure test.
49. **Legacy DB, epoch 0 semantics** — PASS x3. Fresh store reports 0; guarded write with
    expected 0 succeeds; after `reset_context_state` a stale expected-0 write returns False
    and leaves no row.
50. **Corrupt stored epoch through the mixin** — PASS x3. `'junk'` stored: both expected 0
    and expected 1 drop (False); the next reset CAST-heals to 1; a write with expected 1
    then lands. Unguarded (None) write still lands.
51. **Subclass and override compatibility** — PASS x3. A `SessionStore` subclass overriding
    `upsert_file_entry` and calling `super()` works; `clear_file_entries` bumps the epoch.
52. **Concurrency, separate processes** — PASS x3. Six writer processes snapshot the epoch
    then sleep 1.0s inside the judge-to-write window while a resetter process commits at
    0.4s: all six writes dropped (False), reset completed.
53. **Python 3.9 grammar** — PASS x3 (`ast.parse(..., feature_version=(3, 9))`; grammar
    only, no 3.9 interpreter was run).
54. **Real hook entry points per tree** — PASS x3. `read_cache.py --quiet`, `--invalidate`,
    `--clear`, `--clear-compacted` run as subprocesses from each tree with isolated HOME:
    exit 0, no traceback.
55. **Byte identity** — PASS. `reset_epoch.py` sha256 prefix `a71a08bc3cecafff` identical
    in all three trees, LF only, single trailing newline.

Harness note: the first run of round 49-51 failed for plugins/cowork because all three
trees reused one session id and shared one on-disk DB (test artifact, not a code
defect); rerun with a unique session id per tree passed.
