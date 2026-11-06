# Torture-review log — K1 + K2 patch families

Scope (provenance, standalone recomposition): the reviewed units are the
standalone per-family branches on base 86d9374b. K1 (keep-warm arm dedup,
`_measure/cache_policy.py` x3 mirrors + `tests/test_keepwarm_arm_dedup.py`) is
code-CLEAR but held on the frozen lane-4 proof contract (F1) — it ships as its
own change once that contract lands, and is NOT in this PR. K2 =
`pr-k2-standalone` (read-cache reset-epoch guard, `session_store.py` +
`read_cache.py` x3 mirrors + `tests/test_read_cache_reset_epoch_race.py` /
`_integration.py`). K3 = `pr-k3-standalone` (antigravity bridge), stacked on K2.
The doc presents the K1 finding as diagnosed-pending: observed-current vs
proposed guarantees are kept separate. Each round is a distinct adversarial
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
