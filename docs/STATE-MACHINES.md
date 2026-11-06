# Token Optimizer state machines

This is the single source of truth for every explicit and implicit state machine in
Token Optimizer: the states, the invariants, and the transition tables. It lives next to
`docs/METHODOLOGY.md` (which owns every savings number) and follows the same rule:

**If code and this document disagree, one of them is a bug. Change both in the same commit.**

Function names point into `skills/token-optimizer/scripts/` (the canonical tree; the
`plugins/` and `cowork/` mirrors are byte-identical copies enforced by
`scripts/check-mirror-sync.sh`). State machines are listed with the storage that carries
their state, because in this codebase the state usually *is* a file, a marker, or a
database row observed by the next short-lived hook process.

Grounding: every machine below was read at pin `86d9374b` (v5.13.66). Reachability claims
about invalid states are backed by an executable reproduction or an existing test; anything
else is labeled *hypothesis*.

---

## 0. Coverage and limits

Inventoried in full (states, invariants, transitions below):

| # | Machine | Primary code | State storage |
|---|---------|--------------|---------------|
| 1 | Hook process lifetime | `hook_runtime.py` (`HookDeadline`, entry budgets) | process + watchdog thread |
| 2 | LeaseLock pathname machine | `hook_runtime.py` (`LeaseLock`) | `<name>` hard link + `.<name>.candidate-*` + `.<name>.reclaim-*` |
| 3 | Stale-lease sweeper | `hook_runtime.py` (`_sweep_stale_leases`) | quality-cache dir |
| 4 | Tool-result archive | `archive_result.py` | `tool-archive/<sid>/`, `.locks/`, `manifest.jsonl`, `.active` |
| 5 | Session-end flush | `_measure/lifecycle.py` | `.session-end-flush.lock/`, `.last-session-end-refresh`, `.initial-backfill-*` |
| 6 | Dashboard regeneration | `_measure/lifecycle.py`, `_measure/dashboard.py` | `dashboard.hook.backoff`, heal-pid marker, build flock |
| 7 | Checkpoint capture/restore | `_measure/continuity.py`, `checkpoint_state.py` | `checkpoints/<sid>-<ts>[-trigger].md(.json)`, `resumable-<sid>.json`, `once-<tag>-<sid>.json` |
| 8 | Context-reset coordination | `session_store.py`, `read_cache.py`, `refetch_guard.py` | session DB + `.context-reset-<sid>` marker |
| 9 | Session store | `session_store.py` | per-session SQLite (WAL) |
| 10 | Read cache | `read_cache.py` | `file_reads`, `cached_content`, mode config |
| 11 | Re-fetch guard | `refetch_guard.py`, `refetch_fingerprint.py` | archive manifest + reset marker |
| 12 | Thrash guard | `thrash_guard.py` | `command_run_streaks` rows |
| 13 | Quality-cache throttle | `quality_cache_gate.py`, `_measure/quality_archive.py` | throttle marker, `.qlease`, sweep markers |
| 14 | Keep-warm consent | `_measure/cache_policy.py` | `config.json` flag |
| 15 | Keep-warm tripwire | `_measure/cache_policy.py` | tripwire sidecar + lease |
| 16 | Keep-warm arm records | `_measure/cache_policy.py` | keepwarm sidecar JSONL |
| 17 | Settings.json writes | `_measure/lifecycle.py` | `.settings.lease` + file identity |
| 18 | Usage accounting | `_measure/sessions.py` | trends.db request map, rebuild flags |
| 19 | Facade initialization | `_measure/_compat.py`, `measure.py` | `sys.modules['measure']`, provider bindings |
| 20 | Desktop cache clock | `desktop/token-optimizer-desktop/src/clock.ts` | in-memory `ClockState` |

Inventory-only (states listed, transitions not yet tabulated here):

* Per-harness rollup bridges (`cursor_hook_bridge.py`, `grok_hook_bridge.py`,
  `antigravity_hook_bridge.py`, `hermes_hook_bridge.py`): each keeps a per-session state
  DB and a "stop rollup due" check under a stale-swept lock. Same lock family as #2.
* TS-native plugins (`openclaw/`, `opencode/`, `pi/`, `vscode-extension/`): plugin
  lifecycle and rate-window readers. Regression tests for the VS Code companion are owned
  by a separate workstream; this document records their machines but does not change them.
* `desktop/` beyond `clock.ts`: inventoried from public source only. Fixes that would
  overlap the paused desktop diagnosis workstream are out of scope here and held.

---

## 1. Hook process lifetime (`HookDeadline`)

Every hook entry point is a fresh short-lived process. `run.py` arms a `HookDeadline`
per entry from `resolve_entry_budget(module, argv)`; `arm_entry_budget` is the silent
variant. A daemon watchdog thread owns the only guaranteed-exit path.

**States:** `armed -> (completed | timed-out)`

| Event | Guard | Action | Next |
|---|---|---|---|
| `start()` | not started | records previous thread-local deadline, starts watchdog | armed |
| work finishes in budget | - | `cancel()`: sets event, joins watchdog (0.1s), restores previous deadline | completed |
| deadline fires | not cancelled | runs `on_timeout` callbacks on bounded side threads (1.0s total), emits diagnostic on a bounded thread (0.05s join), `os._exit(0)` | timed-out |
| `TOKEN_OPTIMIZER_HOOK_BUDGET_MS <= 0` | env override | no deadline armed | (unbounded) |

**Invariants**

* I1.1 The watchdog always terminates the process after the budget; the diagnostic write
  can never delay it (bounded side thread; losing the message is acceptable).
* I1.2 The "budget exceeded" diagnostic is emitted only on the timeout path.
* I1.3 `os._exit(0)` skips `finally` blocks and `LeaseLock.release()` — every lock used
  under a deadline must be reclaimable by generation (machine #2) and every leaked
  candidate must be sweepable (machine #3).
* I1.4 `remaining()` never exceeds the original budget (Windows clock granularity).
* I1.5 On-timeout callbacks are bounded and abandoned if they hang.

---

## 2. The LeaseLock pathname machine

`LeaseLock` is the one mutual-exclusion primitive: portable exclusive-create, no
platform advisory locking. Used by the settings writer, the quality cache, the tripwire,
the archive store, and the harness bridges.

**Per-pathname states:** `FREE -> HELD -> RELEASED -> (reclaimed) FREE`, plus
`MALFORMED` (unparseable owner file) handled by a parallel grace path.

Owner metadata (`_metadata`): `pid`, `nonce`, `released` (0/1), `reuse_wall`,
`created_wall`, `expires_wall` (deadline wall time, else `now + lease_seconds`).

| From | Event | Guard | Next |
|---|---|---|---|
| FREE | `acquire()` | `O_CREAT|O_EXCL` candidate write + atomic `os.link` publish | HELD (self) |
| any | `acquire()` find existing | `_reclaim_if_expired()` below, else wait with jittered backoff until `acquire_timeout` | unchanged |
| HELD | `release()` | owner nonce matches | RELEASED (single-byte `released:0->1` flip on the shared inode, candidate unlinked; canonical tombstone stays) |
| HELD | `acquire()` by other | `now > expires_wall + reclaim_grace` and claim-by-generation wins | reclaimed -> FREE |
| RELEASED | `acquire()` by other | different pid, `now <= reuse_wall`, `reclaim_released=False` | stays RELEASED (cohort throttle absorbs the herd) |
| RELEASED | `acquire()` by other | `now > reuse_wall` or caller passed `reclaim_released=True` (distinct mutation) | reclaimed -> FREE |
| MALFORMED | `acquire()` | stable across two lstats + older than `max(reclaim_grace, 0.25)` | reclaimed -> FREE |
| any | owner `created_wall > now + grace` | clock rollback: fail open, never steal | unchanged |

Reclaim is `_reclaim_path(generation, validate)`: hard-link the observed generation to a
**deterministic** claim name (`sha256(generation)[:32]`), re-validate under the pin, then
`rename` the pathname aside (a successor linked meanwhile is moved, not deleted, and is
linked back). Exactly one same-generation reclaimer wins; a process killed mid-reclaim
leaves its claim behind and future contenders fail open.

**Invariants**

* I2.1 At most one process believes it holds a given generation.
* I2.2 A lease still within `expires_wall + grace` is never taken.
* I2.3 A released lease stays reserved until `reuse_wall` for waiting-cohort callers
  (`cohort_throttle=True`, the default with `acquire_timeout>0`); writers of distinct
  mutations opt out (`cohort_throttle=False`, e.g. `archive_original`).
* I2.4 `release()` never deletes the canonical pathname (tombstone); only reclaim and
  the sweeper remove pathnames.
* I2.5 Validation failures and OS errors always resolve to "not acquired" (fail open);
  callers skip their mutation.

---

## 3. Stale-lease sweeper

`_sweep_stale_leases(directory)` is the self-healing backstop for I1.3 leaks (one-shot
sessions leave tombstones with no future contender). Throttled to once per 24h by
`_qlease_sweep_due` (`_QLEASE_SWEEP_MARKER`).

| Artifact | Removed when |
|---|---|
| `*.qlease` | parseable and `expires_wall < now - 3600s`; corrupt and `mtime < now - 86400s` |
| `.*.candidate-*`, `.*.reclaim-*`, legacy `*.qlock` | `mtime < now - 86400s` |

Archive-store-local variant (`archive_result._sweep_stale_lock_candidates`): candidates
older than 10.25s (`lease_seconds + reclaim_grace` bound), reclaim claims older than
300s. The asymmetry is load-bearing: reaping a live *candidate* is harmless (acquirer
fails open); reaping a live *claim* can let a second reclaimer win the same generation.

**Invariant** I3.1: the sweeper can never remove anything a live locker still needs;
worse case is an unnecessary failed acquire.

---

## 4. Tool-result archive (`archive_result.py`)

Per-session directory `tool-archive/<sid>/` with `<key>.json` entries, a
`manifest.jsonl` index, and an `.active` heartbeat. Locks live in `tool-archive/.locks/`
as `.{sid}.lease` LeaseLocks (`acquire_timeout=0.075`, `cohort_throttle=False`, no
deadline -> max lease 10.25s).

**Session-dir states:** `active -> inactive -> expired -> pruned`

| Event | Guard | Effect |
|---|---|---|
| any archive write | lock acquired | `.active` touched (0o600) |
| aggregate cleanup (`cleanup_old_archives`) | `.active` mtime older than 24h grace *and* no fresh writes | dir eligible for pruning |
| retention sweep | entry mtime < cutoff, or file/byte caps exceeded | oldest entries unlinked; manifest rewritten under the session lease |
| writer post-write re-check | `archive_entry_exists` | caller verifies its entry survived a concurrent prune |

**Invariants**

* I4.1 Manifest and entry set converge: `_prune_session_entries` rewrites the manifest to
  the retained set in the same lease critical section.
* I4.2 An active session's directory is never removed, only pruned entry-by-entry.
* I4.3 All writes are 0o600, redacted, capped (5MB + overshoot-then-cut after redaction).
* I4.4 Every failure path is fail-open (serve raw); a hook never blocks on the archive.

---

## 5. Session-end flush (`_measure/lifecycle.py`)

`_dispatch_session_end_flush` routes the CLI entry:

| Args / profile | Mode |
|---|---|
| `--no-defer` | inline worker (manual/tests) |
| `_remote_lean_profile()` (cloud VM) | `inline-remote`: worker bounded by the hook deadline's remainder, capped to its fair share |
| default | `_defer_session_end_flush`: detached worker (a legacy fossil hook must never run the flush inline) |

Worker critical section: mkdir lock `.session-end-flush.lock` (stale-steal at 120s) ->
`.last-session-end-refresh` throttle (2 min, touched at *attempt* time) -> initial deep
backfill (`.initial-backfill-attempts` counted *before* the bounded call, marker
`.initial-backfill-done`, max 5 attempts) -> rolling 90-day `collect_sessions` -> bounded
dashboard regen -> `_flush_capture_checkpoint` -> post-flush extensions.

**States:** `idle -> locked -> (ran | skipped-throttle | skipped-lock) -> idle`

**Invariants**

* I5.1 The backfill attempt counter increments even under `_HookTimeout`
  (BaseException), so the deep glob retires unconditionally after 5 attempts.
* I5.2 The inline-remote budget is `min(remaining - margin, cap - margin)`, floor 0.1s:
  a spent share gets a token attempt, never an overrun into the next subcommand's slice.
* I5.3 A second worker can steal a lock older than 120s; concurrent flushes are made
  safe downstream (SQLite WAL + busy timeouts, atomic dashboard writes, per-sid
  checkpoint names), not by the lock alone.
* I5.4 The refresh marker is consumed at attempt time: a timed-out flush still suppresses
  re-runs for 2 minutes (bounded staleness by design).

---

## 6. Dashboard regeneration

Two paths: bounded hook-path regen (20s budget) and detached self-heal
(`_spawn_detached_dashboard_selfheal`, unbounded `TOKEN_OPTIMIZER_INTERACTIVE=1`, fully
detached, quiet).

**Marker states:**

* `dashboard.hook.backoff`: version-stamped; backoff active only while the on-disk
  dashboard is *newer* than the marker and younger than 1h (clock-skew tolerant).
* heal-pid marker (`_DASHBOARD_HEAL_LOCK_NAME`): hook pid -> CAS-replaced by child pid
  (`_replace_dashboard_heal_pid`), so the marker always names the live rebuild owner.
* `_dashboard_selfheal_build_lock`: OS-level flock; `_dashboard_selfheal_inflight()`
  reports a running detached rebuild.

**Invariant** I6.1: a forced heal governs the write throttle only; version precedence
inside `generate_standalone_dashboard` still prevents an older build clobbering a newer
dashboard. I6.2: the detached child never re-spawns (it cannot time out) — no loop.

---

## 7. Checkpoint capture and restore (`_measure/continuity.py`)

`compact_capture` writes `checkpoints/<sid>-<yyyymmdd-hhmmss>[-trigger].md` (+ JSON
sidecar) atomically. Triggers are sanitized to a safe class (`_sanitize_trigger`,
fallback `auto`); filename stamp is local time with offset; selection and age use mtime.

**Capture decision tree:**

| Input | Result |
|---|---|
| `transcript_path` fails confinement | `None` (a forged hook path never falls through to inference) |
| no path + session id | `_current_jsonl_for_hook(session_id)` (identity before inference) |
| Codex, no transcript | no file; `hook_status` records why (#278) |
| non-Codex, no transcript | minimal placeholder ("No transcript data available") |
| redaction config broken | `None` (nothing written) |

**Restore selection:** `_order_checkpoints_for_restore` sorts by recency and keeps only
checkpoints with a restorable body (`## ` sections or a parseable non-empty sidecar) —
placeholders never outrank real state. `_checkpoint_restore_recovery_tokens` floors the
token estimate; older checkpoints merge in via `_merge_earlier_checkpoint_context`.

**Auxiliary per-session flags (quality-cache dir):**

* `resumable-<sid>.json`: a relevance-cleared checkpoint exists; UI nicety, never a
  correctness gate.
* `once-<tag>-<sid>.json`: run-once-per-session guard for SessionStart-equivalent
  features. Unusable/unknown sid -> `None` -> caller fails *open* and runs unguarded
  (a collision would fail *closed* and silently skip a session's work; the sanitizer's
  6-char floor exists so degenerate ids cannot collide).

**Invariants**

* I7.1 Restore never resurrects placeholder content.
* I7.2 A checkpoint's identity (sid + local timestamp + trigger) is unique per capture;
  concurrent captures for one session cannot overwrite each other (second-granularity
  stamp + trigger suffix; atomic write).
* I7.3 `_trusted_transcript` (internal callers only) is the only bypass of hook-path
  confinement.

---

## 8. Context-reset coordination

`/clear` and compaction empty the model's context while the session id lives on. Two
coordinated mechanisms:

1. `SessionStore.reset_context_state()`: one transaction deletes the context-describing
   ledgers (`file_reads`, `cached_content`, `command_outputs`, `command_run_streaks`,
   `hint_serves`, `shadow_fr:*`/`active_fr:*` meta). The tool-output archive, activity
   log, compaction cursor and decision notes survive — they describe the session, not
   the context window.
2. `mark_context_reset(session_id)`: a plain timestamp file read by the re-fetch guard
   on every MCP call, so archived results from before the reset stop counting as data
   the model still holds.

Trigger points: the read cache's proactive compaction scan (transcript marker), and the
explicit `--clear-compacted` path (5s busy-timeout, loud on failure). Subagents
(`agent_id` present) clear only their own read ledger.

**Invariant** I8.1: after a reset, the first read of a file is judged a first read, and
the re-fetch guard's duplicate window restarts. I8.2: a failed reset is loud, never a
silent exit-0.

---

## 9. Session store (`session_store.py`)

Per-session SQLite, WAL mode, `synchronous=NORMAL`, `busy_timeout=50ms` on the hot path
(fail-fast: shadow work accepts dropped writes rather than stalling a hook), 5s on the
explicit clear path. 50MB cap per session DB (`_is_over_size_cap` refuses writes).
PreToolUse hooks read-only; PostToolUse hooks write.

**States:** `absent -> initialized (schema version N) -> migrated (N+1) -> capped -> cleaned`
(`cleanup_old_stores` age sweep). FTS5 mirror `tool_outputs_fts` is kept in sync by
triggers, with a one-time rebuild for legacy rows and a bounded LIKE fallback when FTS5
is unavailable (`_is_real_fts_absence` distinguishes a real absence from corruption).

**Invariant** I9.1: a lock failure leaves old state intact (single transactions).
I9.2: an invalid session id is rejected before a random fallback UUID can silently
disable accumulation (thrash_guard) — sanitize-then-verify, never sanitize-then-assume.

---

## 10. Read cache (`read_cache.py`)

Modes (`READ_CACHE_MODES`): `shadow -> warn -> soft_block (default) -> block`, from
`TOKEN_OPTIMIZER_READ_CACHE` / `config.json read_cache_enabled`.

**Per-file entry states:** `unread -> read(file_reads row) -> (edited: invalidated | compaction: cleared)`

| Event | Effect |
|---|---|
| PreToolUse:Read, no entry | allow; record entry with `tool_use_id` |
| same `tool_use_id` again (double-fired hook) | allow (F1b idempotency: double registration must not deny the user's first actual read) |
| entry present, unchanged, eligible code file | soft_block: deny + inject structure map |
| `record_file_edit` / PostToolUse invalidate | entry removed; next read is fresh |
| compaction scan positive | machine #8 reset |

---

## 11. Re-fetch guard (`refetch_guard.py`)

Self-healing loop-breaker for archived MCP results. Decides on the session's archive
manifest, newest-first.

| Condition | Decision |
|---|---|
| no match for (tool, args fingerprint) | allow (neutral output) |
| newest match outside window (`REFETCH_GUARD_WINDOW_SECONDS` or before `context_reset_at`) | allow — an old identical call is a deliberate re-check, not a loop; every older entry is older still, scan stops |
| match within window, entry file renderable | deny + hand back the exact `expand` command; savings logged once |
| match within window, entry not renderable | keep scanning older entries |
| any error | allow (fail open; the guard can only ever *add* a deny for a proven exact duplicate) |

---

## 12. Thrash guard (`thrash_guard.py`)

Nudge-only; never denies. Three independent signals, priority order, exactly one nudge
per output:

1. **Identical-output streak**: >= 3 consecutive runs of the same command with
   byte-identical output. Any material output change resets the streak to 1.
2. **Burn nudge**: same normalized command fails >= 3 times in a session with different
   output each time.
3. **Inline-script repeat**: normalized heredoc body >= 300 chars, run >= 8 times.

**Streak row states:** `{count, last_output_hash, last_nudge_at_streak, updated_at}`

| Event | Guard | Next |
|---|---|---|
| same command, identical output | - | count+1 |
| same command, different output | - | count=1 |
| nudge fired at streak S | - | next nudge at S+3 |
| streak older than 1800s | staleness | reset (spaced repeats are deliberate re-checks) |

State lives in the per-session store keyed by the agent-scoped id, so streaks never leak
across sessions or between main agent and subagents.

---

## 13. Quality-cache throttle (`quality_cache_gate.py` + `_measure/quality_archive.py`)

The PostToolUse `quality-cache --throttle-only` hook fires on every tool call; the gate
answers the common case with one `stat()` and no measure.py import.

| Marker state | Decision |
|---|---|
| throttle marker fresh (< throttle window) | exit 0, nothing computed |
| marker stale | fall through to the full quality-cache computation |
| marker missing | **not due** (OSError -> False): a cache miss never parses a transcript; UserPromptSubmit creates the first cache and marker |
| force flag | always due |

Long-lived artifacts (`.qlease` tombstones etc.) are reclaimed by machine #3 under the
24h `_QLEASE_SWEEP_MARKER` throttle.

---

## 14. Keep-warm consent (`_measure/cache_policy.py`)

Sticky record in `config.json` (`_KEEPWARM_CONSENT_KEY`), one of
`unasked | asked | declined | enabled`; absent/corrupt reads as `unasked` (fail-safe to
never-pings).

| From | Event | Guard | To |
|---|---|---|---|
| unasked | pitch shown (`keepwarm-consent-asked`) | - | asked |
| unasked/asked | `keepwarm-enable` | billing == api | enabled (+ scheduler install; install failure never rolls back consent) |
| any | `keepwarm-enable`, subscription billing | not limits-lab | unchanged; honest refusal, nothing written |
| any | `keepwarm-enable --limits-lab` (dogfood-only, CLI-only) | explicit double opt-in | enabled + `keepwarm_limits_lab` flag |
| any | `keepwarm-disable` | - | declined (terminal for prompts; explicit re-enable always possible) |

The pitch is shown exactly once (`should_ask` only when api + unasked). `_keepwarm_set_consent`
rejects unknown values so a typo can never read back as `unasked` and re-trigger the ask.

**Pre-ping gate (`keepwarm_gate`) order:** ping-process marker (anti-recursion, first
and unconditional) -> declined -> billing -> consent-state -> tripwire ceiling. Only the
LLM ping is gated; the token-free watcher always runs.

---

## 15. Keep-warm tripwire (`_measure/cache_policy.py`)

Rolling 7d realized/spend ratio, two-strike sticky demotion ladder
`sustain -> probe-only -> off`, sidecar `{mode, strikes, ratio_7d, health, last_strike_ts}`.

| Condition | Transition |
|---|---|
| missing sidecar | reads `sustain` (no breach yet) |
| corrupt/invalid sidecar | reads `off` (fail closed) |
| ratio <= 1.0, first strike (>= 24h since last strike) | sustain -> probe-only |
| second strike, full window (>= MIN_PINGS) | probe-only -> off |
| no breach | unchanged (sticky; never auto-promotes) |
| `keepwarm-enable` (re-)run | sole sanctioned walk-up: strikes zeroed, mode sustain |

Health side-signal: fraction of pings on never-resumed sessions; `error`/`timeout` pings
count as neither waste nor signal (they spent ~$0 and warmed nothing) — they are an infra
signal surfaced separately.

**Invariants** I15.1: zero-spend windows never demote. I15.2: demotion moves only down.
I15.3: recompute+write is serialized by the tripwire lease; a lost race skips the tick.

---

## 16. Keep-warm arm records (`_measure/cache_policy.py`)

The Stop hook (entry 3 of `hooks/stop_runner.py`, every Stop event) calls
`write_keepwarm_arm_record`, which appends one JSON line to the keepwarm sidecar:
`{session_id, transcript_path, model, prefix_proxy, ttl_kind, last_turn_ts,
transcript_mtime, byte_size, ts, state: "armed"}`. The stored `state` field is never
mutated; the live class is *derived* per tick by `classify_keepwarm_record`:

| Class | Condition | Tick behavior |
|---|---|---|
| `gone` | transcript missing/unreadable, or no path | compaction drops the record |
| `resumed` | transcript mtime moved AND size grew past the armed snapshot, or arm older than 14d | compaction drops; realized-savings booking re-reads the real post-arm usage (never books off the verdict alone) |
| `still_paused` | snapshot unchanged (a bare touch is not a resume) | policy decision below |

Legacy records without `byte_size` stay `still_paused` until the 14d expiry (never
falsely booked off a missing field). Arming is suppressed entirely for cloud sessions,
inside a ping subprocess (anti-recursion marker), and for non-Claude runtimes.

Tick decision per still-paused record (`keepwarm_policy_decision`, pure):
prefix >= 10k and known model -> pause < 12h hard cap -> phase (pings 0-1: probe;
ping 3+: needs predictor AND tripwire allow_sustain) -> per-pause break-even
(cumulative ping cost < expected avoided write) -> cadence (5m or 1h by TTL kind).
`keepwarm_tick` serializes: gate -> singleton tick lock -> detect_realized ->
tripwire recompute -> compaction -> per-record decide/fire -> persist `pings_fired`.
Firing is serialized per session by a pidfile lock, gated by an idle re-check
(120s), pre-logged to the spend ledger before the subprocess (a crash cannot hide
spend), and the whole LLM ping ships OFF behind `TOKEN_OPTIMIZER_KEEPWARM_ALLOW_LLM_PING`.

**Invariants**

* I16.1 At most ONE active arm record per (session, transcript snapshot). OBSERVED
  CURRENT: not enforced — `write_keepwarm_arm_record` appends unconditionally
  (finding K1). PROPOSED guarantee: arm-time (session_id, transcript_mtime,
  byte_size) dedup under the keep-warm lock, landing with the pending K1 change
  (not part of this PR).
* I16.2 The per-pause probe cap (2) and the break-even abort are per *pause*; once
  I16.1 lands (pending K1 change, see above) that makes them equivalent to per
  record. Until then, duplicate records for one snapshot each carry their own
  `pings_fired` (finding K1).
* I16.3 A ping never hides its spend: the ledger 'firing' row is written before the
  subprocess exists.
* I16.4 A resume is only ever *detected* by transcript growth; booking always re-reads
  real usage.

---

## 17. Settings.json writes (`_measure/lifecycle.py`)

`_settings_lock` (LeaseLock; user-initiated callers get 2s retries + released-lease
reclaim, hook callers 75ms fail-open) -> fresh read -> `_merge_concurrent_settings` ->
`_write_settings_atomic_locked` with `expect_identity` checked immediately before
`os.replace` (an identity change sets `identity_changed` and nothing is replaced; the
caller re-merges) -> `_settings_write_guard` choke point refuses key-loss writes and
reports the refusal reason. Symlink destinations are written through, never detached;
modes and the file's own line endings are preserved.

**Invariant** I17.1: a hook-context write that loses the lease or the identity check
changes nothing. I17.2: the lock-free body is only ever called with the lease held (the
fossil-reconcile path holds it across read+merge+write as one critical section).

---

## 18. Usage accounting (`_measure/sessions.py`)

Per-request identity (`_usage_request_key`): `requestId`, else the Anthropic `message.id`
for SDK/cloud transcripts, else the record stands alone. Streamed chunks fold to the MAX
usage per request; replayed/duplicated requests fold by key across sessions (`n=0` rows:
"another session already counted this call" — tokens and call count are attributed once).
Requests bucket to their *local* calendar day (#200). `_aggregate_request_usage` is the
single rollup every figure derives from.

Aggregate tables (`daily_stats`, `model_daily`, `skill_daily`, `subagent_daily`) rebuild
behind flags (`_needs_model_daily_rebuild`, `_needs_streaming_dedup_rebuild`) with
migrations (`_migrate_model_daily`, `_migrate_streaming_dedup`).

**Invariant** I18.1: every request counted once (METHODOLOGY R6). I18.2: unparseable
timestamps are excluded from day buckets, never misfiled.

---

## 19. Facade initialization (`_measure/_compat.py`)

Post-split (v5.13.66), `measure.py` is a facade over domain providers. `prepare(module)`
selects the active facade (`sys.modules['measure']`) and either eagerly reloads each
domain module or, only for the UserPromptSubmit runner's import (`HOOK_IMPORT`), defers
the heavy ones.

**Binding states:** `unswept -> pending (PEP 562 __getattr__ installed) -> resolved
(loaded + bound) | caller-assigned (a caller's assignment always wins)`. A deferred
domain that fails `find_spec` fails the facade import, cached or not. A fresh `prepare`
retires the previous initialization's lazy lookups (`__getattr__`/`__dir__` marked
`_measure_lazy` are deleted).

**Invariant** I19.1: ordinary reload creates fresh functions without rebinding names a
previous facade already exported. I19.2: providers carry no business state; all runtime
state lives in the stores above.

---

## 20. Desktop cache clock (`desktop/.../clock.ts`) — inventory only

The one literally-typed FSM in the repo. `ClockState = {anchor, lifetime, contextTokens,
working, warming, lapsed}`; `reduceClock(state, event, now?)` is pure.

| Event | Guard | Effect |
|---|---|---|
| `request-done` | `at >= anchor` | anchor=lifetime=context refreshed, `lapsed=false`; an older request says nothing |
| `warm-start` | `canKeepWarm(state, now)` at press time | `warming=true`, else refused (a button handle can outlive its drawing) |
| `warm-done` | read < 50% of context (or < 1024 tokens when context unknown) | `lapsed=true` — the cache was gone |
| `warm-done` | otherwise | anchor advanced, `lapsed=false` |
| `warm-failed` | - | `warming=false` only |
| `clear` | - | state reset |
| `lifetime-measured` | - | replaces the 300s unmeasured default |
| keep-warm refused | within 15s of expiry (`KEEP_WARM_MARGIN_MS`) | `canKeepWarm` false |

(Recorded from public source; no desktop fixes in this workstream.)

---

## 21. Invalid states and transitions — findings

Status legend: **confirmed** (executable evidence attached, reachable in the shipped host),
**confirmed-storage-race** (executable two-process evidence at the storage layer;
product-level reachability conditional on harness event ordering, unproven per host),
**reproduced-existing-test** (covered), **hypothesis** (not yet evidenced; do not file),
**cleared** (checked, already handled by the code).

### Cleared (checked during this audit)

* C1. Placeholder checkpoints outranking real state on restore — cleared:
  `_checkpoint_has_restorable_body` filters placeholders in every selection path
  (restore, best-checkpoint, dashboard band). Issue #278's Codex side is covered by the
  no-write + hook-status path.
* C2. Concurrent same-generation lease reclaimers both winning — cleared: deterministic
  claim names serialize same-generation reclaim; validated re-stat under the pin;
  successor-linked-during-reclaim is moved aside and put back. Covered by
  `tests/test_reclaim_sweep_serialization.py` and the 5-minute claim-sweep fuse.
* C3. `HookDeadline` kill leaking lock candidates forever — cleared by design: the
  sweeper (machine #3) plus the archive-local 10.25s candidate fuse.
* U1 -> C4. Flush-lock steal cascading — cleared by construction: the facade binds the
  flush worker's hard budget to 20s (`_run_session_end_flush_worker` defaults=(20,)),
  so a live worker cannot reach the 120s steal age; the steal fires only on a lock
  leaked by a deadline-killed worker, which is the designed recovery. Even under a
  hypothetical deadline failure (e.g. a platform where the wall-clock guard cannot
  interrupt), the cascade's downstream writes are atomic: dashboard rebuilds are
  documented racing `os.replace` publishes (`_measure/lifecycle.py:827`), session
  collection is WAL SQLite, and the throttle marker bounds duplicate work. No
  reachable corrupt state.
* C5. Checkpoint same-second filename collision — cleared: `compact_capture` names
  checkpoints `{sid}-{YYYYMMDD-HHMMSS}{trigger_suffix}.md` and writes via
  `_write_checkpoint_atomic` (mkstemp + os.replace). A double-registered Stop firing
  two captures for one session inside the same second overwrites the first with
  byte-equivalent tail content (same session, same second, same trigger) — atomic,
  no torn file, negligible content difference. Different triggers carry different
  suffixes. No reachable harmful state.

### Under investigation

* U0 (environment note, not a product bug): `tests/test_consent_gate_race_window.py::
  test_race_window_pretooluse_bash_hook_still_fires` fails at HEAD 86d9374b in this
  sandbox because an extra `ps` ancestor-scan spawn is counted (expects exactly one
  spawn). Reproduced identically with and without this workstream's changes; flagged
  for the QA-matrix owner as a test-environment sensitivity, not filed as a product bug.

### Confirmed

* **K4 (candidate, triage pending). Escape hatch is cold in hook-per-process
  hosts.** `consecutive_denials` has no `file_reads` column, so the denial
  counter is in-memory only and reloads as 0 in every fresh hook process; the
  escape hatch (`MAX_CONSECUTIVE_DENIALS = 3`) that exists to bound denial loops
  can never fire, so a file misjudged as redundant in `block` mode is denied
  indefinitely with no automatic recourse. Evidence:
  `tests/repro_escape_hatch_cold.py` — three fresh `handle_read` processes
  against one session, all judged with counter=0, 0 escapes. The K2/F3 epoch
  guard on the hatch write is defense-in-depth on this currently-cold path.
  Fix direction (persist the counter; schema migration) deliberately NOT
  implemented in this packet — held for triage after K2 content clears.

* **K1. Duplicate keep-warm arm records multiply the per-pause probe cap.**
  Reachable invalid state: more than one active arm record for the same
  (session, transcript snapshot), produced by a double-registered Stop hook (the
  settings-fossil state the installer reconciler exists to clean up) or by any two
  Stop events without transcript growth between them. `write_keepwarm_arm_record`
  appended unconditionally, `pings_fired` is tracked per record, and the ping command
  is `--no-session-persistence`, so a fired ping never bumps the transcript mtime
  that would make the duplicate classify `resumed` or fail the idle re-check.
  Evidence: `tests/test_keepwarm_arm_dedup.py` (in the pending change) —
  one `keepwarm_tick` fired 2 `claude --resume` pings for one paused session
  (assertion failure showed both `fire_status: ok` decisions). Impact: with the
  LLM-ping escape hatch on, probe spend per pause is multiplied by the registration
  count (default product path is safe: the LLM ping ships disabled, and the
  zero-spend window never trips the tripwire). Diagnosed fix: arm-time dedup on
  (session_id, transcript_mtime, byte_size) under the keep-warm lock, with 6
  regression tests — implemented and reviewed in a SEPARATE pending change that
  is NOT part of this PR and not shipped here; the invariant below is therefore
  stated as observed-current vs proposed, not as enforced.

* **K2. Read-cache post-reset writer race — confirmed-storage-race; product
  reachability conditional, unproven per host.** Storage-layer interleaving
  (executable evidence): a writer process that judged a Read before a context reset
  (`reset_context_state` on /clear or detected compaction, or the subagent-ledger
  `clear_file_entries`) but commits its `file_reads` upsert after the reset's
  transaction leaves a row claiming the file is in context when the model no longer
  holds it; the next Read would then be judged "unchanged and already in context"
  and denied or outline-served (silent-context-loss class). SQLite serializes
  individual statements, so the judge->write window exists for any two processes
  sharing the per-session db. Host reachability: NOT evidenced. In the primary host
  every read_cache and SessionStart hook in `hooks/hooks.json` is `async: false`
  (awaited), and /clear and auto-compact land between turns, so the ordering argues
  the window cannot interleave there; per-bridge event delivery semantics (Cursor,
  Grok, Antigravity, Hermes, Copilot, Codex) are not proven from this repo. The fix
  is therefore defensive hardening at the storage layer, not a confirmed user-facing
  bug in a shipped host. Evidence (storage layer):
  `tests/test_read_cache_reset_epoch_race.py::test_two_process_interleaving`
  (writer/resetter subprocesses over one db; pre-fix shape resurrects the row, pinned
  by `test_unguarded_upsert_still_works`). Fix: `reset_epoch` counter in
  `session_meta`, bumped inside the `reset_context_state`/`clear_file_entries`
  transactions; `handle_read` snapshots the epoch after the compaction scan;
  `upsert_file_entry(..., expected_reset_epoch=...)` checks-and-inserts in one
  IMMEDIATE transaction and drops the write when the epoch moved (failure mode
  parity with the pre-fix unconditional write under the same 50ms busy budget).
  The guard lives in a new sibling module `reset_epoch.py` (a `SessionStore` mixin
  plus `bump_reset_epoch` / `snapshot_reset_epoch` helpers; `SessionStore` public
  method signatures unchanged) so the already-over-limit `session_store.py` and
  `read_cache.py` do not grow under the repo's file-size gate.
  Judge-driven follow-up (F3/F4): the escape-hatch and invalidate writers were
  also unconditional `file_reads` writers that could resurrect a pre-clear row.
  They are now epoch-guarded at the PRODUCTION call sites: both
  `_check_escape_hatch` invocations in `handle_read` (eligible-structure and
  block-mode branches) forward the snapshotted `reset_epoch`, and
  `handle_invalidate` snapshots `_invalidate_epoch` at hook start and passes it
  to its post-edit rewrite — every `file_reads` writer in `read_cache.py` goes
  through the guard. Real-path tests drive `handle_read` / `handle_invalidate`
  with a reset committing inside the judge→write window and pin BOTH hatch
  branches plus the invalidate rewrite (mutation-checked: unwiring any of the
  three call sites fails its test). Corrupt `reset_epoch` values DROP the write
  and return False (never raise, never normalize to 0): "no reset since the
  corruption" and "reset landed, then corruption" are indistinguishable from
  the value alone, and normalizing would accept a stale epoch-0 writer in the
  second case — resurrecting the row this guard exists to drop. Dropping is
  fail-open (the read/edit proceeds; only the ledger write is skipped) and the
  next reset bump CASTs the value to a clean integer, re-engaging the guard.
  Residuals, documented: (a) `cached_content` upserts stay unconditional —
  they cannot flip a Read judgment to redundant on their own (the `file_reads`
  row drives that decision). (b) Corruption ABA: after external corruption of
  the stored epoch, the next reset heals it to '1', which can COLLIDE with a
  stale writer's epoch (1 → 2 → corrupt → heal 1) and accept it — the guard
  bounds resets, not external corruption of `session_meta`, and no product
  writer produces a non-integer value. Regression tests:
  `tests/test_read_cache_reset_epoch_race.py` (13 test functions, 19 pytest cases) +
  `tests/test_read_cache_reset_epoch_integration.py` (6 tests).
---

## 22. Issue grouping

Grouped fix proposals land here once a finding is confirmed. Draft groups are reported
for approval before any public issue is filed.

* **G1 (single-finding group, keep-warm arm integrity) - HELD for trusted-channel
  review, not filed:** K1. Small, self-contained: arm-time dedup + 6 regression
  tests in `tests/test_keepwarm_arm_dedup.py` — implemented in a SEPARATE pending
  change, not this PR. No other confirmed keep-warm findings group with it yet.

* **G2 (single-finding group, read-cache reset integrity) - HELD for trusted-channel
  review, not filed:** K2. Draft title: "Read cache: harden file_reads writes against
  post-reset resurrection (reset epoch)". Body draft must carry the K2 label verbatim:
  confirmed storage-layer race (two-process repro), product reachability conditional
  and NOT evidenced per host (primary-host hooks are all awaited, async: false); the
  change is defensive hardening, not a confirmed user-facing bug fix. Root cause
  (unconditional upsert outside any reset-aware transaction), repro
  (tests/test_read_cache_reset_epoch_race.py, 13 test functions / 19 pytest cases incl. two-process
  interleaving, corrupt-epoch drop and bad-type coverage;
  tests/test_read_cache_reset_epoch_integration.py, 6 real-path tests),
  fix (reset_epoch guard, one IMMEDIATE transaction; every file_reads writer in
  read_cache.py guarded at its production call site; corrupt stored epoch drops
  fail-open).

---

## 23. Test enforcement

| Machine | Existing coverage |
|---|---|
| LeaseLock | `tests/test_leaselock_acquire_free_retry.py`, `tests/test_reclaim_sweep_serialization.py`, `tests/test_concurrency_perf_hardening.py` |
| Sweeper | `tests/test_reclaim_sweep_serialization.py` |
| Flush/backfill | `tests/test_stuck_session_compact_restore_budget.py`, `tests/test_sessionend_fossil_reconcile.py` |
| Consent gate | `tests/test_consent_gate_race_window.py` |
| Keep-warm arm dedup (K1) | `tests/test_keepwarm_arm_dedup.py` (in the pending K1 change, not this PR) |
| Read-cache reset epoch (K2) | `tests/test_read_cache_reset_epoch_race.py`, `tests/test_read_cache_reset_epoch_integration.py` |

New regression tests added by this workstream are listed here as they land.
