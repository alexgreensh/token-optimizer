# review-b-fixes report (Token Optimizer 5.13.59)

Branch: `claude/rel59-unit-fixes-ey1hw5` (based on `origin/integ/5.13.59`).
Scope: every should-fix and nit in Review B of PR #290 (#287, advisor context
fill) and PR #289 (#288, harness text in Active Task / topic).

## Summary

All 17 findings were checked against the code. 15 needed a change and got one.
Finding 8 (numbers the host owns) needed no code change, and finding 6 got a
code comment only, as the review suggested. No finding turned out to be wrong.
One fact changed the plan for finding 4: the top-level usage holds only the
executor iterations, so advisor tokens were missing from the billed total, not
just priced at the wrong model. The fix adds them.

## What changed, per finding

| # | Fix | Where |
|---|-----|-------|
| 1 | VS Code fill fallback uses the final `type:"message"` iteration (port of `_context_usage`, `_valid_context_usage`, `_context_token_count`). | `vscode-extension/src/jsonlTail.ts` |
| 2 | `parse_session_turns` stores `ctx_cache` (final executor cache read + write) on advisor turns. Keep-warm replay uses it for `prefix_proxy`, `prev_cached` and the tail, and uses `resume_cache_creation` for the rewrite test. Shared helpers `_turn_prefix_cached` and `_turn_resume_write`. | `measure.py` |
| 3 | Cache-rewrite waste detector uses the same two helpers for its threshold. The dollars charged stay on the billed aggregate `cc`. | `measure.py` `_detect_explicit_ttl` |
| 4 | New shared helper `_billed_usage_parts` splits one response into an executor part and one part per `advisor_message` iteration. Each advisor part is priced at the iteration's `model` when the rate card knows it, else at the executor model. `_price_usage_parts` prices the parts. Used by `_parse_session_jsonl` (per-request map, so per-model breakdown, daily usage and cross-file merge all see the advisor model), `parse_session_turns` and the costly-prompts parser. | `measure.py` |
| 5 | `get_session_baselines` uses `_context_token_count(_context_usage(u))`. It also stops raising on `None` values. | `measure.py` |
| 6 | Comment only: the statusline cache hit rate is a billing ratio and reads the aggregate on purpose. | `measure.py` |
| 7 | Docstring note in `_advice_session_data`: `cache_reads` is the billed aggregate, `turns` is executor occupancy, so `avoided_share_pct` can only understate on advisor sessions. | `measure.py` |
| 8 | No code change (see "Could not do"). | |
| 9 | `[Request interrupted by user` added to `_HARNESS_TASK_PREFIXES` (covers the "for tool use" form). | `measure.py` |
| 10 | Narrow prefix `Your response above was cut off mid-stream` added. The human sentence in the existing test ("...cut off. Please explain why?") is still kept. | `measure.py` |
| 11 | A bare built-in local command (`/model`, `/context`, `/exit`, `/init`, ... list in `_LOCAL_SLASH_COMMANDS`) is transport unless an installed skill owns the name. With arguments it is kept (`/model opus`). | `measure.py` `_command_task_text` |
| 12 | Codex: `_task_user_text` skips `# AGENTS.md instructions`, `<environment_context>`, `<user_instructions>`, `<turn_aborted>`, `<user_action>`, `<INSTRUCTIONS>`. Applied to topic (both parsers) and `current_step.last_user`; injected text also no longer feeds open questions. Topic prefers the `event_msg/user_message` over the `response_item` duplicate (`_pick_topic`). | `codex_session.py` |
| 13 | Costly prompts take their text from `_task_user_text(record)`, skipping the record when it is empty. | `measure.py` `_extract_costly_prompts` |
| 14 | A command wrapper without a `<command-args>` tag is read as empty args (`/review` is kept). Skill-usage counting still requires the args tag, unchanged. | `measure.py` |
| 15 | A leading `<system-reminder>...</system-reminder>` block is dropped and the text after it is kept; an unclosed block is all harness. | `measure.py` `_task_user_text` |
| 16 | `message_count` skips `isMeta` and `isCompactSummary` user records. | `measure.py` `_parse_session_jsonl` |
| 17 | OpenClaw `topicUserText` ports the same rule for legacy Claude Code logs (meta, compact summary, prefixes, system reminder, command wrappers). `dist/` rebuilt. | `openclaw/src/session-parser.ts` |

Advisor parts in the per-request map are keyed `<requestId>#advisor<n>` and
marked `advisor: True`. They do not add to `api_calls` or call timestamps, and
the subagent cache-payoff scan skips them (the advisor has its own cache).

## What I proved

New file `tests/test_review_b_advisor_harness.py` (28 tests). With the three
source files (`measure.py`, `codex_session.py`, `jsonlTail.ts`) reverted to
`origin/integ/5.13.59`: 25 fail, 3 pass. The 3 that pass are guards that real
input is still kept (`/model opus`, the human "cut off" sentence, an installed
skill named `context`). With the fixes: 28 pass.

- Finding 4: `test_advisor_iteration_is_priced_at_the_advisor_model`,
  `test_unknown_advisor_model_falls_back_to_executor_price`,
  `test_aggregate_that_already_holds_advisor_tokens_is_split_not_doubled`,
  `test_streamed_advisor_records_count_once`.
- Findings 2, 3: `test_keepwarm_replay_uses_executor_prefix_and_sees_the_rewrite`
  (the review's exact case: prev aggregate 980,924, resume writes 489,000 in
  iteration 0; before the fix `rewrote` was False),
  `test_rewrite_detector_threshold_uses_executor_prefix`,
  `test_plain_turns_keep_aggregate_prefix`.
- Finding 5: `test_session_baseline_is_context_size_not_advisor_sum`.
- Findings 9, 10, 11, 15: `test_harness_text_never_becomes_active_task[*]`
  (end to end through `_extract_session_state`).
- Findings 11, 14, 15 keep side: `test_real_tasks_are_kept[*]`,
  `test_installed_skill_named_like_a_local_command_is_kept`.
- Finding 13: `test_costly_prompts_skip_harness_text`.
- Finding 16: `test_meta_records_are_not_counted_as_human_messages`.
- Finding 12: `test_codex_injected_context_never_becomes_topic_or_task`,
  `test_codex_topic_prefers_user_message_event`.
- Finding 1: `test_vscode_tail_reads_executor_context_on_advisor_turns` runs the
  real `jsonlTail.ts` through node (skipped when node or the extension's dev
  dependencies are missing). Also a vitest file
  `vscode-extension/src/__tests__/jsonlTail.test.ts` (12 tests): 3 fail on the
  old `jsonlTail.ts`, all pass with the fix. That folder is gitignored by repo
  rule ("Tests are local-only"), so it was run but not pushed.
- Finding 17: `openclaw/src/topic-harness.test.ts` (bun, 2 tests). Fails on
  the old parser. Full OpenClaw suite: 462 pass.

Updated existing tests that pinned the old "advisor billing unchanged" rule,
which this unit changes on purpose:
`test_advisor_context_uses_final_executor_and_bills_advisor_on_top` and
`test_advisor_keepwarm_books_executor_resume_once` in
`tests/test_quality_fill_host_sanity.py`. They now assert executor billing is
unchanged and the advisor iteration is added on top.

Full suite: see "Full test run" below.

Mirrors: `scripts/sync-codex-marketplace-plugin.sh` run, cowork mirror
regenerated, `scripts/check-mirror-sync.sh` passes for both.

## Could not do, and why

- Finding 8 (statusline.js live fill, desktop `requestContextTokens`): these
  show numbers Claude Code or the SDK computes. No code here reads
  `iterations`, so nothing to change unless the host itself sums. The review
  asks for one live check on an advisor session. This sandbox has no live
  Claude Code advisor session, so it was not done. Suggested check: on an
  advisor session, compare the statusline percent with the quality cache
  `context_tokens`.
- Real transcript shape for advisor iterations (finding 4) and for the
  "cut off mid-stream" record (finding 10): no real transcript is available
  offline. Finding 4 follows the API docs and the repo's own fixture (the
  top-level usage equals the executor sum; advisor iterations carry `model`).
  The helper handles both shapes: if a future aggregate already includes the
  advisor tokens, they are moved out of the executor part instead of added.
  Finding 10 uses a narrow prefix, which is safe whether or not the record
  carries `isMeta`.
- `deterministic_candidates.py` still prices each turn at one model. It ranks
  turns, it is not the billed total, and its turn struct holds one model.
  Splitting it is a larger change; left as a follow-up.
- The VS Code extension is a separate vsix. Its CHANGELOG has an
  `## Unreleased` line; publishing is out of scope (no version bumps).

## Decisions

1. Branch name: the brief says `rel59/review-b`, but this session may only push
   to `claude/rel59-unit-fixes-ey1hw5`. Used that branch.
2. Finding 4 direction: the documented shape has advisor tokens outside the
   top-level usage, so they are added to the bill. When the aggregate already
   covers executor + advisor in every bucket, they are split out instead. Either
   way no token leaves the billed total.
3. Advisor parts are not counted as API calls and do not add call timestamps;
   they belong to their request. The subagent cache-payoff analysis skips them.
4. Costly prompts keep pricing cache writes without the TTL split (as before),
   so only advisor turns change there.
5. Finding 11: the review's rule ("keep only with args or an installed skill")
   would also drop bare custom commands and prompt commands such as `/review`,
   which are real tasks. Safer choice: drop only a fixed list of built-in local
   commands when bare, unless an installed skill owns the name. `/init` is on
   the list because the review names it.
6. Finding 15: strip `<system-reminder>` only. A teammate message can carry the
   real assignment for a teammate agent, so it is left as is.
7. Finding 16 changes `message_count` slightly for sessions with meta records.
   No existing test depended on the old count.
8. VS Code test: the extension's test folder is gitignored by repo rule, so a
   pushed Python test runs the same TypeScript through node, and the vitest file
   stays local.
9. Tests run with `env -i` and a throwaway HOME. This sandbox sets
   `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=80` and other host variables, which make 5
   tests fail on the untouched base branch. With a clean environment the base is
   green.

## Full test run

PENDING

UNIT DONE
