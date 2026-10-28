# Hermes parity matrix

What Token Optimizer does on Claude Code and Codex, and how each feature reaches Hermes (NousResearch hermes-agent) through its plugin hooks. Hermes hook names and payloads come from upstream `hermes_cli/plugins.py` and `website/docs/user-guide/features/hooks.md` at commit `7ee6d99c`.

Sources for the feature list: README feature sections, `docs/codex.md` parity tables, `HOOKS.md`, `hooks/hooks.json`, `skills/token-optimizer/SKILL.md`, `measure.py` subcommands, and the Codex, Copilot and Devin adapters.

How Hermes runs it: the plugin (`hermes/__init__.py`, version 0.2.1) lives inside the Hermes process. Every engine call happens in a short child process running the same scripts the Claude Code hooks run (`hermes_tool_hooks.py` routes a Claude-Code-shaped payload to them; `measure.py hermes-checkpoint` and `hermes-turn-context` cover checkpoints and per-turn context). Every hook fails open: on any error the tool output, the turn and the session continue unchanged.

Status key: **Yes** works, **Part** partly works (reason given), **No** not available (reason given).

## Compression and archive

| Capability | Claude Code | Codex | Hermes before 0.2.0 | Hermes hook | Hermes now |
|---|---|---|---|---|---|
| Terminal output compression (git, find, ls, logs, lint, JSON, CSV, docker, k8s, cloud CLIs, stack traces) | Yes (PreToolUse rewrite + PostToolUse) | Experimental opt-in | No | `transform_terminal_output` | Yes. The captured output goes through the Claude Code PostToolUse pipeline (`bash_compress_hook`). |
| Build and test output compression (pytest, jest, cargo, npm, make, gcc, go, mvn) | Yes (PostToolUse) | Experimental opt-in | No | `transform_terminal_output` | Yes, same engine. |
| Search result compression (grep, rg run in a shell) | Yes | Experimental opt-in | No | `transform_terminal_output` | Yes for searches run in the terminal. Hermes's own `search_files` tool results are archived, not trimmed, matching Claude Code's Grep tool. |
| Cross-turn dedup of a repeated command's output | Yes | Experimental opt-in | No | `transform_terminal_output` | Yes |
| Failed commands and error output kept whole | Yes | Yes | n/a | `transform_terminal_output` | Yes |
| Custom command filters (TOML) | Yes | Yes | No | engine setting | Yes: `~/.hermes/token-optimizer/command-filters.toml` |
| Full original archived, `expand` pointer | Yes | Stop-time backfill | No | `transform_terminal_output`, `transform_tool_result` | Yes. The pointer carries `--runtime hermes` so it works from Hermes's terminal. |
| Large MCP result replaced by a preview | Yes | Part | No | `transform_tool_result` | Yes. Hermes names MCP tools `mcp__server__tool`, the same as Claude Code. |
| MCP re-fetch guard | Yes (PreToolUse) | No | No | `pre_tool_call` (block) | Yes |
| Read, search and agent results archived for recovery | Yes | Stop-time backfill | No | `transform_tool_result` | Yes |
| Savings accounting per session | Yes | Yes | No | engines write `compression_events` | Yes, attributed to the Hermes session id. |
| Bash command rewrite before it runs | Yes (PreToolUse `bash_hook`) | Experimental opt-in | No | `pre_tool_call` (modify) | Not needed. Compressing the captured output covers every command, read-only or not, without changing what runs. |

## Reads

| Capability | Claude Code | Codex | Hermes before 0.2.0 | Hermes hook | Hermes now |
|---|---|---|---|---|---|
| Delta reads (re-read after an edit returns only the diff) | Yes | No (no `updatedInput` on Read) | No | `pre_tool_call` (block with the diff) | Yes |
| Structure map for an unchanged re-read | Yes | No | No | `pre_tool_call` | Yes. Hermes also has its own "unchanged" stub; Token Optimizer answers first, with the same engine as Claude Code. |
| First-read periphery skeletons | Promoted cohorts | No | No | `pre_tool_call` + `transform_tool_result` | Yes. Extra context from the read engine is attached to that read's result. |
| `.contextignore` and binary-ingestion warnings | Yes | No | No | `pre_tool_call` | Yes |
| Read cache refreshed after edits | Yes | n/a | No | `transform_tool_result` (`write_file`, `patch`) | Yes |
| Read cache cleared after compaction | Yes | n/a | No | `post_compaction` | Yes |

## Checkpoints and continuity

| Capability | Claude Code | Codex | Hermes before 0.2.0 | Hermes hook | Hermes now |
|---|---|---|---|---|---|
| Progressive checkpoints at 20/35/50/65/80% fill | Yes | Yes | No | `post_api_request` (live prompt size) | Yes |
| Quality-drop checkpoints (score below 80/70/50/40) | Yes | Yes | No | none | No. Those thresholds read Claude Code's transcript-derived score. Hermes's score is computed at rollup from fill, length and output ratio; fill is already covered by the fill bands. |
| Checkpoint before compaction | Yes (PreCompact) | Compact prompt + Stop | No | `pre_auxiliary_call` with `aux_task="compression"` | Yes, once per compaction (retries do not repeat it). |
| Checkpoint at turn end | Yes (Stop) | Yes (Stop) | No | `on_session_end` (fires every turn) | Yes, at most every 90 seconds. |
| Checkpoint on a fatal API error | Yes (StopFailure) | Approx. | No | `api_request_error` with `retryable=False` | Yes |
| Checkpoint at session end, rollup, dashboard refresh | Yes (SessionEnd) | Yes (Stop) | Rollup only, and it fired after the first turn | `on_session_finalize` | Yes. Fixed: the rollup now runs once at session end, not after turn one. |
| Checkpoint before subagent fan-out | Yes | Yes | No | `pre_tool_call` (`delegate_task`) | Yes, once per session. |
| Checkpoint after an edit batch (4 writes or 3 files) | Yes | Yes | No | `post_tool_call` (`write_file`, `patch`) | Yes |
| Manual checkpoint | `checkpoint-trigger` | Same | No | `register_command` | Yes: `/token-optimizer checkpoint` |
| Restore after compaction | Yes (SessionStart compact) | Yes (resume) | No | `post_compaction`, delivered by `pre_llm_call` or the next tool result | Yes. When compaction rotates the session id, the old session's checkpoint is restored. |
| New-session checkpoint pointer | Yes | Yes | No | `pre_llm_call` (`is_first_turn`) | Yes |
| Prior-session hints matched to the prompt (multilingual topics, resume intent) | Yes (UserPromptSubmit) | Yes | No | `pre_llm_call` | Yes, same engine. |
| Checkpoint content source | Claude Code transcript | Codex rollout | n/a | Hermes `state.db` messages, read-only | Yes. Never falls back to another tool's transcript. |
| Context intel (activity mode, decision extraction) | Yes | Part | No | `transform_terminal_output`, `transform_tool_result` | Yes |
| Dynamic compact instructions (decisions the summary must keep) | Yes (PreCompact) | Compact prompt | No | none | No host support. `pre_auxiliary_call` is observer-only. Only `register_context_engine` could change the summary prompt, and that replaces Hermes's own compressor. |

## Nudges and quality

| Capability | Claude Code | Codex | Hermes before 0.2.0 | Hermes hook | Hermes now |
|---|---|---|---|---|---|
| Quality score and grades | 7 signals | 7 signals | 3 signals | rollup | Yes, unchanged (3 signals; see `hermes/README.md`). |
| Context fill nudge | Yes | Yes | Yes, only when Hermes's compressor needs help | `pre_llm_call` | Yes, unchanged by design: Hermes owns compression. Fixed: the once-per-session gate no longer resets every turn. |
| Lean-output nudge | Yes | Yes | No | `pre_llm_call` | Part. The 75-89% fill tier fires, paced like Claude Code (3 per session, 5 minutes apart). The gentle tier needs a session-efficiency signal Hermes does not record. |
| Loop detection (identical reruns, fail streaks, inline-script repeats) | Yes | Part | No | `transform_terminal_output` | Yes |
| Subagent sprawl nudge | n/a | Yes | No | `subagent_start`, `subagent_stop` | Yes, delivered with the next tool result or turn. |

## Tokens, cost, dashboard, tools

| Capability | Claude Code | Codex | Hermes before 0.2.0 | Hermes hook | Hermes now |
|---|---|---|---|---|---|
| Per-turn token capture | Transcript | Rollout | Yes | `post_api_request` | Yes |
| Cost per session, dashboard, trends | Yes | Yes | Yes | rollup | Yes |
| Skill usage telemetry | Yes | Part | No | `on_skill_lifecycle` | Yes. Skill loads are counted and stored with the session. |
| Per-session cost lookup | `session-cost` | Same | Summary only | `register_command` | Yes: `/token-optimizer cost [words]` |
| Read an archived result | `measure.py expand` | Same | No | terminal, `register_command` | Yes: the printed command, or `/token-optimizer expand <id>` |
| Credential redaction in archives, checkpoints and logs | Yes | Yes | n/a | shared engines | Yes |
| Credential patterns shared with the host | n/a | n/a | No | `register_redaction_patterns` | Yes. Token Optimizer's patterns (and your custom file) also mask secrets in Hermes's own output and logs. |
| Doctor | `doctor` | `codex-doctor` | Checked 4 hooks | n/a | `hermes-doctor` loads the installed plugin and reports all 17 hooks by name, the engine scripts, redaction, and stale installed copies. |
| Install, uninstall | Plugin | `codex-install` | Yes | n/a | Yes; install now fails early if an engine script is missing. |
| Session state cleanup | n/a | n/a | n/a | `on_session_reset`, `on_session_delete` | Yes (in-memory state only; files follow normal retention). |

## Not available on Hermes

| Capability | Why |
|---|---|
| Status line | No host support. Hermes has a built-in status bar (which already shows context fill) but no plugin API to add to it: `PluginContext` has no status registration. `/token-optimizer` and the dashboard cover the same numbers. |
| Keep-warm | No host support. Keep-warm re-sends a Claude Code session's cached prefix with `claude --resume -p`; Hermes has no entry point to ping a session from outside, and it routes to many providers. |
| Subagent prompt cache TTL, usage recommendations, model-pin hint, CLAUDE.md routing injection, smart-compact setup | These audit or change Claude Code's own settings (`settings.json`, `CLAUDE.md`); Hermes has no equivalent files for them to read. Session cost per model is still tracked for Hermes. |
| Quality-drop checkpoints | See above: no per-turn transcript score on Hermes. Fill-band checkpoints cover the same moments. |
| Dynamic compact instructions | See above: observer-only compression hooks. |

## Hermes hooks not used

| Hook | Why not |
|---|---|
| `post_llm_call`, `transform_llm_output`, streaming hooks, `on_interim_message` | No Token Optimizer feature acts on the final reply. |
| `pre_api_request`, `post_auxiliary_call` | `post_api_request` already carries usage; auxiliary usage is billed by Hermes and already in `state.db`. |
| `pre_verify` | No Token Optimizer feature gates finishing a turn. |
| `transform_api_error_classification` | Error routing is Hermes's job; Token Optimizer only observes failures. |
| `agent_loop_stopped` | Gateway-only interrupt; `on_session_end` covers the turn end. |
| Gateway, approval, human-input, kanban, room, transcription, `pre_command` hooks | No Token Optimizer feature maps to them. |

## Switches

Every Hermes feature can be turned off on its own with an environment variable set to `0`: `TOKEN_OPTIMIZER_HERMES_TERMINAL`, `_TOOL_RESULTS`, `_READ_CACHE`, `_REFETCH_GUARD`, `_CHECKPOINTS`, `_RESTORE`, `_CONTINUITY`, `_VERBOSITY`, `_REDACTION` (each prefixed `TOKEN_OPTIMIZER_HERMES`). The shared engine switches apply too, for example `TOKEN_OPTIMIZER_BASH_COMPRESS=0`, `TOKEN_OPTIMIZER_READ_CACHE=0` and `TOKEN_OPTIMIZER_READ_CACHE_DELTA=0`.
