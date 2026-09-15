# Changelog

## [Unreleased]

- Fix: the Hermes context-fill nudge measured the session-CUMULATIVE input tally instead of the live
  prompt, so it reported a context emergency that did not exist. Every host re-sends the whole
  conversation on each turn, so that sum climbs past the model window regardless of real occupancy:
  on a 129-call Hermes session the cumulative figure reached 1,285,803 against a 1,000,000 window
  ("Context ~100% full ... Grade: F", the percentage being capped at 100) while Hermes itself
  reported 278,545 / 1,000,000 = 28% for the same session. The nudge now uses the prompt the last
  call actually sent (fresh + cached prompt tokens, since cached tokens occupy the same window), and
  says so in the message ("last request prompt ~N tokens vs model window M"). The cumulative tally
  is unchanged for cost and usage reporting. Regression tests:
  `tests/test_hermes_context_fill_nudge.py` (3 of the 5 cases fail on the previous code).

## [5.13.14] - 2026-09-14

- Fix: the Codex log-index now self-heals on Windows instead of crashing on a locked or corrupt database. The open path closes the broken connection before rebuilding (Windows refuses to unlink an open file, so the rebuild previously reconnected to the same corrupt DB), retries each unlink briefly to ride out transient share locks, and no longer misdiagnoses a merely-locked database as corruption and deletes it out from under a live process. Transient lock contention (concurrent first-opens racing journal-mode/schema setup, a writer mid-commit, or a lock-upgrade deadlock that returns BUSY without consulting the busy handler) now retries on a deadline at both the connect and write-transaction stages instead of surfacing "database is locked" to callers -- follow-up hardening on #175.
- Fix: the release installable check no longer races the signing workflow. A fresh release missing CHECKSUMS.sha256 is polled through the signing grace window (measured from published_at) instead of failing instantly -- v5.13.13's check ran at publish+2s while the asset landed ~20s later. An old unsigned release still fails immediately.
- Fix: Codex hooks on native Windows failed with "hook exited with code 1" on versioned marketplace installs. The generated cmd.exe command assigned TOKEN_OPTIMIZER_RUNTIME_ROOT inside a `for /f` loop and read it back with `!TOKEN_OPTIMIZER_RUNTIME_ROOT!` on the same line; cmd parses a /C line once and `setlocal EnableDelayedExpansion` only applies from the next line, so Python received the literal placeholder path. The runner path is now built from the FOR variable `%R` inside the do-body, and the version resolver always prints one directory (newest semver install, else the baked install) so the fallback still runs. Regression tests execute the generated command through `%COMSPEC% /D /C` with spaces in the install path. Reinstall and uninstall now also recognize those broken commands: they carry no `token-optimizer/scripts` path marker (backslash install paths and `hooks/<name>_runner.py` args), so `_is_token_optimizer_group` additionally matches the full signature of our generated command -- the quoted `TOKEN_OPTIMIZER_RUNTIME_ROOT=` assignment together with our `hooks\run.py` runner invocation under a token-optimizer path (or via our own FOR/delayed-expansion variable) -- so a user's own hook that merely references the env var is never swept up. When the version resolver falls back to the baked install it now appends a line to `token-optimizer-codex-resolver.log` next to the version dirs if `TOKEN_OPTIMIZER_DEBUG` is set, making the previously silent fallback observable.

- Fix: the token-saving hooks now reach every supported harness. Codex, Cowork, and manual installs get the same savings as Claude Code -- startup diagnostics stay out of the model's context, and the command-failure and long-output nudges reach the model through each host's supported channel.
- Fix: SessionStart no longer adds anything to the model's context. Startup diagnostics (health checks, dashboard setup, daemon status) now write to a local log file instead of stdout/stderr, both of which the host captures into the session context. Sessions begin at their true baseline, so the token savings start on the first turn.
- Add: burn nudge. When the same command fails 3 times in a row with different output, a nudge suggests changing approach instead of re-running. Catches the edit-compile-fail cycle that the existing identical-output streak guard cannot see. Tunable with `TOKEN_OPTIMIZER_FAIL_STREAK_THRESHOLD` (default `3`).
- Add: inline-script repeat nudge. When a command with a heredoc body >= 300 chars has been run 8 times in a session, a nudge suggests saving the script to a file and running that instead, so the body is not re-sent as input tokens every turn. Tunable with `TOKEN_OPTIMIZER_INLINE_SCRIPT_THRESHOLD` (default `8`).

## [5.13.13] - 2026-09-14

- Add: first-class Codex support, extracted and hardened from external PR #175 by @dormancygrace. Codex sessions get real model pricing (gpt-6-astra, gpt-5.6-sol) through a versioned model catalog, delta-based token accounting that stops the over-count on incremental log writes, canonical session-ids, a SQLite log-index for fast session discovery, native Windows process handling, a security-hardened command-compression hook, and a base64 Windows launcher that survives cmd.exe quoting.
- Add: Codex Token-Coach port with full runtime isolation. Context-window detection, model config, and savings accounting now resolve per-runtime across Claude, Codex, and the five other supported harnesses, so a foreign runtime never inherits Claude's model env vars, ~/.claude config, or the 1M default.
- Fix: coach-integrity hardening across the measurement pipeline. Safe-int/type/size guards reject malformed session records, ANSI/VT escape sequences are stripped before terminal output, log-index reads are confined to the runtime home, concurrent writers no longer corrupt the index, and the consent gate narrows its exception handling and logs unexpected errors so a corrupt config is visible, while still failing open by design.
- Docs: new benchmarks category (Overview, Terminal-Bench floor, Controlled A/B, One real month) with a refreshed real-savings.svg built from current measurements.

## [5.13.10] - 2026-09-08

- Prevent sandbox dashboard tests from replacing the real background service. Isolate hook test homes and cached modules, and keep capped transformation percentages and older marker history consistent.

- Include modeled repeat-read savings in the action card for removals made during the selected period. Retain logged setup, output, routing and unmatched-event savings without counting initial removals twice.
- Make Savings easier to scan: compact transformation and action summaries, explicit periods and estimates, matching percentage and dollar comparisons, and expandable methods that stay open during live refresh.
- Compare lifetime context savings with its matching period subtotal. Preserve previously verified history when transcripts rotate, and remove duplicate delta-read entries from the derived ledger.
- Retain other logged savings in the weekly fallback calculation and show the previously omitted concise-output estimate. Supported runtimes without repeat-read evidence keep their logged totals.

## [5.13.9] - 2026-09-08

- Fix growing session logs being skipped after their first collection. Refresh parent and child activity without duplicating totals or overwriting newer collector results.
- Show the full Savings-tab estimate for the actual subscription week, counting overlapping savings once and labeling the amount as estimated savings accrued so far. Include new sessions before background collection catches up.
- Recover the workload comparison after history backfill or rebuild changes its baseline month. Cache weekly results for up to 60 seconds and retain weekly dollars when quota readings are unavailable.
