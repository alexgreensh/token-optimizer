# rel59 unit: hermes-parity (Token Optimizer 5.13.59)

Branch: `claude/rel59-unit-rules-qenjf7`, started from `origin/integ/5.13.59` (see Decisions for why not `rel59/hermes-parity`).

## Outcome

The Hermes plugin now registers 17 hooks (was 4) and gives Hermes users the Claude Code / Codex feature set wherever Hermes exposes a way: terminal output compression, archive with expand, large MCP previews and the re-fetch guard, delta and structure-map re-reads, loop detection, checkpoints (fill bands, pre-compaction, pre-fanout, edit batch, per turn, fatal error, session end), restore after compaction, prior-session hints, lean-output nudge, subagent sprawl note, skill usage telemetry, savings per session, credential patterns shared with Hermes's redactor, and a richer `/token-optimizer` command. Plugin version bumped to 0.2.0.

The parity matrix with every row, the Hermes hook used, and the reason for each gap is in `docs/hermes-parity.md`.

Upstream Hermes was cloned read-only (commit `7ee6d99c`, the commit named in the brief) into a scratch dir outside the repo to read exact hook kwargs. Nothing from it was imported or run.

## What changed

Plugin and adapter
- `hermes/__init__.py`: 13 new hooks (`pre_tool_call`, `post_tool_call`, `transform_terminal_output`, `transform_tool_result`, `pre_auxiliary_call`, `post_compaction`, `api_request_error`, `subagent_start`, `subagent_stop`, `on_skill_lifecycle`, `on_session_start`, `on_session_reset`, `on_session_delete`), one `HOOKS` table used by `register()`, credential pattern registration, per-feature off switches (`TOKEN_OPTIMIZER_HERMES_*`), bounded per-session state, and a `/token-optimizer` command with `summary`, `cost`, `checkpoint`, `expand`, `help`.
- Fixed in the plugin: `on_session_end` fires every turn in Hermes (`agent/turn_finalizer.py`), but the plugin treated it as session end. It rolled the session up after turn one (freezing the dashboard row at turn one) and reset the once-per-session nudge every turn. Now the per-turn shape (has `turn_id`) writes a "stop" checkpoint; finalize or the CLI exit-path shape does the rollup.
- `hermes/plugin.yaml`: 17 hooks, version 0.2.0.
- New `skills/token-optimizer/scripts/hermes_tool_hooks.py`: a child-process dispatcher that feeds a Claude-Code-shaped payload to the existing engines (`bash_compress_hook`, `archive_result`, `context_intel`, `read_cache`, `refetch_guard`, `credential_patterns`). It runs the Claude Code PostToolUse set minus the quality-cache tick, which with no Hermes transcript would fall back to the newest Claude Code transcript and score the wrong session.
- `hermes_hook_bridge.py`: `run_tool_hooks`, `run_measure_context`, `run_turn_context`, `spawn_checkpoint`, `redaction_patterns`, `to_claude_payload`, `run_command_text`; `run_rollup` passes skill counts.
- `hermes_session.py`: Hermes message rows to Claude-Code-shaped transcript records (read_file, write_file, patch incl. multi-file, terminal, todo_list, delegate_task), plus pinned mirrors of the progressive bands and edit-batch thresholds.
- `hermes_state.py`: read-only `session_messages()`.
- `hermes_doctor.py`: expected hooks = all 17; new checks load the installed plugin in a child process with a stand-in Hermes context and list every registered hook by name, check the engine scripts in the checkout, report shared redaction patterns, and warn when the installed copy differs from the checkout.
- `hermes_install.py`: fails early if an engine script is missing; prints and returns the hook list.

Shared code (all behaviour-preserving for other runtimes, covered by their existing tests)
- `measure.py`: `hermes-checkpoint` (checkpoint from Hermes `state.db` via a private, deleted-after temp transcript; never falls back to another tool's transcript), `hermes-turn-context` (new-session pointer + continuity hint + lean-output in one process), `--skills-json` on `hermes-rollup`, a private `_trusted_transcript` path in `compact_capture` (skips the transcript-derived quality block and the process-cwd git line for it), `_verbosity_nudge_text` extracted from `run_verbosity_steer` (same tiers and words), `_VERBOSITY_MAX_NUDGES` / `_VERBOSITY_COOLDOWN_SEC` hoisted to module level.
- `measure.py` bug fix (all runtimes): a leading `--runtime NAME` was applied after `plugin_env` had fixed its data directories at import, so the flag changed printed hints but not where data was read. Now applied before that import.
- `refetch_fingerprint.expand_command`: under the Hermes runtime the printed command is `measure.py --runtime hermes expand <id>`. Hermes's terminal does not pass any runtime marker (it sets `HERMES_HOME` only when overridden), so the bare command looked in the Claude Code archive and found nothing.
- `recovery_output`: recognises the runtime-pinned form, so expanding a saved result is never compressed or archived again.

Tests and docs
- New `tests/test_hermes_parity.py` (89 tests). `skills/token-optimizer/scripts/devin_source.py`: the new module added to the Devin closed-set allow list. Updated `tests/test_bridges_no_window.py` (Hermes bridge now has 4 detached spawn sites) and `tests/test_hermes_context_fill_nudge.py` (stubs the new background work so it stays a unit test).
- `docs/hermes-parity.md` (new), `hermes/README.md`, docs-site `platforms/hermes.mdx`, `install/hermes.mdx`, `reference/capability-matrix.mdx` (Hermes column), `start/feature-map.mdx`, `features/smart-compaction.mdx`, `features/active-compression.mdx`, root `README.md` Hermes install line, `CHANGELOG.md` under `[Unreleased]`.
- Mirrors regenerated with `scripts/sync-codex-marketplace-plugin.sh`; `scripts/check-mirror-sync.sh` passes for `plugins/` and `cowork/`. OpenClaw not touched.

## What I proved

Each fix below was reverted on its own and the named test went red, then green again after restoring.

| Fix | Test that goes red without it |
|---|---|
| Early `--runtime` in measure.py | `test_terminal_output_is_compressed_archived_and_expandable` |
| Runtime-pinned expand pointer | same, plus `test_large_mcp_result_is_trimmed_and_refetch_is_redirected` |
| Recovery recogniser for the pinned form | `test_expand_output_is_returned_whole` |
| Per-turn `on_session_end` no longer ends the session | `test_per_turn_session_end_checkpoints_without_ending_the_session` |
| No fake quality block in Hermes checkpoints | `test_turn_end_writes_a_checkpoint_from_hermes_messages` |
| No git state from the wrong directory | same |
| Never checkpoint from a Claude Code transcript | `test_checkpoint_never_uses_a_claude_code_transcript` |
| Scratch transcript deleted | `test_checkpoint_scratch_transcript_is_removed` |
| Read cache cleared after compaction | `test_compaction_clears_read_cache_and_restores_checkpoint` |
| Hermes default read limit maps to a full read | `test_hermes_messages_translate_to_checkpoint_records` |
| Lean-output reading passed to the engine | `test_lean_output_nudge_reaches_the_model_at_high_fill` |
| Skill counts stored with the session | `test_end_to_end_session_on_the_installed_plugin` |
| Bash compression switch honoured, loop warnings kept | `test_bash_compress_switch_turns_compression_off_but_keeps_loop_detection` |
| Custom patterns loaded under the Hermes runtime | `test_custom_patterns_from_the_hermes_home_reach_hermes` |
| Read cache skips relative paths | `test_relative_paths_never_use_the_read_cache` |

The whole new file was also run against untouched `origin/integ/5.13.59`: 78 of 79 tests failed or errored there (the one pass was a garbage-kwargs test on a hook the old plugin already had).

Other proofs in `tests/test_hermes_parity.py`:
- Registration: every hook in `plugin.yaml` = doctor's expected set = `HOOKS` = what `register()` registers, and each is in upstream `VALID_HOOKS` (`test_plugin_registers_every_declared_hook`); an older Hermes that rejects one hook still gets the rest (`test_register_survives_a_host_that_rejects_one_hook`).
- End to end (`test_end_to_end_session_on_the_installed_plugin`): installs into a throwaway Hermes home, loads the INSTALLED plugin with a fake ctx, runs session start, first-turn context, API usage at 25% fill, a large terminal command, tool hooks, skill loads, turn end and finalize; checks compression happened, progressive-20 / stop / end checkpoints were written from `state.db`, compression savings and the session row (with skill usage) landed in `trends.db`, and nothing raised.
- Real engines through the plugin: terminal compression + archive + expand from a plain shell, savings row per session, failed output untouched, loop nudge on the third identical rerun, MCP preview + re-fetch block, structure map then diff after a patch, partial reads never blocked, pre-compaction checkpoint then restore on the next turn, restore from the old id when compaction rotates the session, prior-session hint on a new session.
- Plugin decisions (recording bridge): fill bands (only the highest new band, never backfilled), edit batch, pre-fanout once, stop-failure only when not retryable, pre-compact once across retries, sprawl note, failed writes not counted, lean-output pacing (3 per session, 5 minutes apart), memory dropped on reset and delete.
- Fail-open: every hook with an engine that raises, every hook with garbage or no kwargs, missing engine scripts, and a hung engine cut off at the budget (`pre_tool_call` returns None, so the tool runs).
- Budget: the `pre_tool_call` child budget stays at or under 40% of Hermes's `hook_callback_timeout` (Hermes blocks the tool when that hook times out).
- Doctor: a clean install reports 17/17 hooks by name; removing one hook from the installed plugin turns the check to FAIL naming it and flags the stale copy.
- Constants mirrored for the in-process plugin are pinned to their sources (`test_mirrored_constants_match_their_sources`).

Full suite: see "Full test run" below.

## What I could not do, and why

| Item | Reason |
|---|---|
| Status line | Hermes has no plugin API for its status bar (no status registration on `PluginContext`); its own bar shows context fill. |
| Keep-warm | Needs an outside command that re-sends a session's cached prefix (`claude --resume -p`); Hermes has none and routes to many providers. |
| Dynamic compact instructions | `pre_auxiliary_call` is observer-only; only `register_context_engine` could change the summary prompt, and it replaces Hermes's compressor. |
| Quality-drop checkpoints (quality-80/70/50/40) | Need a per-turn transcript-derived score; Hermes's score is computed at rollup. Fill-band checkpoints cover the same moments. |
| Gentle lean-output tier | Needs a session-efficiency signal that Hermes does not record; the 75-89% tier works. |
| Claude Code settings features (subagent cache TTL, usage recommendations, model-pin hint, CLAUDE.md routing, smart-compact setup) | They read or change Claude Code's own files. |
| Live Hermes smoke test | No Hermes install in this sandbox, and the rules forbid running upstream code. Tests drive the plugin with payloads shaped like upstream's documented kwargs. |

## Found, not fixed (outside this unit)

- `tests/test_windows_spawn_no_window.py`: 4 tests fail on untouched `origin/integ/5.13.59` too (`test_daemon_revive_*`, `test_ensure_health_auto_update_*`). Not touched.
- `_collect_hermes_sessions` inserts a session row once (`INSERT OR IGNORE`) and later collects only refresh the grade (and now skill counts). If a dashboard refresh or `hermes-summary` collects a session mid-way, its token totals stay at that point. The per-turn rollup bug fixed here made this happen on every session; it can still happen when another collect runs mid-session.
- Codex prints a bare `measure.py expand <id>` too. If Codex hook subprocesses and the model's shell see different runtimes, the same lookup miss could occur there. Not verified; only Hermes was pinned.

## Incident

The commit "Hermes plugin: register tool, compaction, error, subagent and session hooks" first went out with `tr-probe-out/` files written into the working tree by a probe hook in this environment (session transcripts, not project files). I rewrote that commit on this unit's own branch to drop them and force-pushed with lease within minutes; the branch no longer contains them. The remote may still hold the unreferenced objects until GitHub garbage-collects them. I also moved the stray probe folders out of the tree (to my scratch dir, not deleted) because they broke `test_hooks_dual_tree_parity`, and added `tr-probe-out/` to `.git/info/exclude` locally (not to `.gitignore`).

## Decisions

1. Branch: the brief says `rel59/hermes-parity`, but this session's git instructions require developing and pushing on `claude/rel59-unit-rules-qenjf7` and forbid pushing elsewhere without permission. Used that branch, started from `origin/integ/5.13.59` as the brief says.
2. Engines run in child processes, not in the Hermes process. Several engines change `os.environ` and print to stdout while they run, which is unsafe inside a multi-threaded host. A child per call costs about 0.1-0.2 s (bash pipeline) and is how the Claude Code hooks run them anyway. Budgets are enforced by killing the child at the timeout; `measure.py` subcommands also install their own hook budget.
3. One dispatcher (`hermes_tool_hooks.py`) instead of calling `hooks/posttooluse_runner.py`: that runner includes the quality-cache tick, which without a Hermes transcript would read the newest Claude Code transcript. The dispatcher reuses the runner's matchers and its shared-stdin technique, not the engines' logic.
4. Terminal compression through `transform_terminal_output` (compress captured output) rather than rewriting commands in `pre_tool_call`: covers every command, never changes what runs.
5. Re-reads answered with `pre_tool_call` `block`, which is how Claude Code's read cache answers too (a deny with the map or diff). Only absolute or `~` paths: Hermes resolves relative paths against a session directory the plugin cannot see.
6. Checkpoints read Hermes's `state.db` messages (including rows Hermes later compacted away) and build a temporary Claude-Code-shaped transcript in a private dir, deleted right after; no second copy of the conversation is kept. If a session has no messages, no checkpoint is written (never a guess from another transcript).
7. Stop checkpoints per turn are spaced at least 90 s apart (the shared checkpoint cooldown) to keep per-turn cost low; Claude Code writes one per Stop.
8. Restore after compaction is delivered at the next delivery point (next tool result in the same turn, or the next turn), because compaction can happen mid-turn.
9. The fill nudge keeps its existing policy (silent while Hermes's compressor is healthy). The lean-output nudge is about answer length, not compression, so it fires regardless.
10. `on_session_delete` drops in-memory state only; checkpoints and archives follow normal retention (safer: no data loss).
11. Credential patterns are registered with Hermes's redactor (more masking, never less), with an off switch. Patterns are pre-filtered with Hermes's own three acceptance rules so it never logs a warning per refused pattern, and loaded in a child under the Hermes runtime so the custom file is the one the engines use.
12. Expand pointer pinned with `--runtime hermes` for Hermes only, to keep other runtimes' printed commands unchanged.
13. No `register_tool` for expand: a tool schema costs tokens on every request; the printed command and `/token-optimizer expand` cover it.
14. `TOKEN_OPTIMIZER_BASH_COMPRESS=0` (and the dashboard toggle) turns Hermes terminal compression off but keeps loop warnings, using the bash hook's existing nudge-only mode.
15. Values the in-process plugin needs from `measure.py` (fill bands, edit-batch thresholds, checkpoint cooldown, lean-output pacing, sprawl threshold) are mirrored as constants and pinned to their sources by a test, so the Hermes process never imports the 60K-line module.

## Full test run

`python -m pytest tests/ -q` (throwaway HOME) aborts partway with a pytest INTERNALERROR on this branch AND on untouched `origin/integ/5.13.59`: `tests/test_copilot_safe_python.py` and `tests/test_cursor_safe_python.py` patch `os.stat` with an object lacking `st_size`/`st_ino`, and pytest crashes formatting a failure (and the tree guard in `conftest.py` crashes at session end). So I ran every test file in its own pytest process, for this branch and for base, and compared.

- This branch: 7,989 tests passed across 373 files. 19 files have failures; the same 19 files fail on base with the same counts (base also has `test_hook_never_blocks_cross_harness` failing, which passes here).
- After the last fix, `test_devin_closedset` matches base (1 failure, `test_allowed_source_open_checks[owner]`, also on base).
- Files failing on both, not touched by this unit: test_cloud_parity, test_compact_advice, test_consent_gate_race_window, test_copilot_safe_python, test_cowork_adapter, test_cowork_hardening, test_cursor_safe_python, test_daemon_midsession_pulse, test_daemon_snapshot_isolation, test_devin_closedset (1), test_ensure_health_daemon_revive_first, test_heal_hardening, test_measure_no_window, test_posix_session_identity, test_quality_fill_host_sanity, test_sessionend_defer_default, test_sessionstart_context_tax, test_settings_never_clobbered, test_windows_spawn_no_window.

Failures this unit caused and fixed during the run: the verbosity refactor tripped two source guards (`test_every_context_nudge_uses_the_shared_phrase`, `test_nudge_is_suppressed_when_the_window_is_contradicted`), fixed by keeping `_format_context_fill(fill_pct, cached)` in the helper and placing it after the suppression check; the Devin closed-set inventory needed `hermes_tool_hooks`; the Hermes bridge spawn-count pin went from 3 to 4.

Environment note: a probe hook in this sandbox keeps writing `tr-probe-out/` folders into whatever directory a command runs in. Inside `skills/` or `hooks/` they make the mirror tests fail; they are not part of the repo and were moved out before the runs.

UNIT DONE
