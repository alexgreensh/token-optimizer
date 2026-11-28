# Token Optimizer for Google Antigravity (beta)

Token Optimizer's 7th platform. Three surfaces, one adapter:

- **`agy` CLI** — `~/.gemini/antigravity-cli/`
- **Antigravity 2.0 desktop app** — `~/.gemini/antigravity/`
- **Antigravity IDE** — `~/.gemini/antigravity-ide/`

The value prop leads with **tokens**: Antigravity's own UI shows context fill but
does not surface per-session token totals or a cost figure. Token Optimizer
reads Antigravity's own per-conversation SQLite store (read-only), decodes the
`gen_metadata` protobuf blobs with a zero-dependency decoder, and reports token
totals and — where the model has a known Gemini rate card — a clearly-labelled
list-price estimate.

## Install

Run from any folder:

```bash
git clone --depth 1 https://github.com/alexgreensh/token-optimizer.git
cd token-optimizer
bash install.sh --antigravity
# verify (from inside the token-optimizer folder)
TOKEN_OPTIMIZER_RUNTIME=antigravity python3 skills/token-optimizer/scripts/measure.py antigravity-doctor
```

**Already have Token Optimizer installed**? You don't need a fresh clone;
`install.sh` only lives at the repo root. Run the installer module directly:

```bash
TOKEN_OPTIMIZER_RUNTIME=antigravity python3 ~/.claude/skills/token-optimizer/scripts/measure.py antigravity-install
```

This writes a **user-level plugin directory** at
`~/.gemini/config/plugins/token-optimizer/`. It never touches your own
`~/.gemini/config/hooks.json`, `config.json`, or `settings.json`.

### Consent (R20)

Collection is **opt-in**. The installer records consent in
`~/.gemini/token-optimizer/config.json`; until that record exists the bridge and
rollup short-circuit to `{}` / zero. This matches the consent promise in
`HOOKS.md` and `PRIVACY.md`.

## Where the data comes from

| Signal | Source | Notes |
|---|---|---|
| Token totals | `gen_metadata.data` (one protobuf per generation) | input/output/cache-read/thinking, summed over generations |
| Model | `model_display_name` (field 21) | `.3.7`-style names map to a Gemini rate-card id; unknown names stay unpriced |
| Credit cost | `credit_cost` / `consumed_credits` (fields 13/18) | shown when Antigravity reports a non-zero figure; never mixed with the USD estimate |
| Context fill | `estimated_tokens_used` / `max_context_tokens` | last decoded generation |
| Tool calls / user inputs | `steps` step_type + decoded tool name | step_type 14 = user input |
| Title / workspace | `conversation_summaries.db` | `title`, `workspace_uris`, `killed`, `not_fully_idle`, `last_modified_time`, `nesting_depth` |

The decoder is version-tagged (`ag-v1`, pinned to the Antigravity field mapping
verified in the research snapshot). Every field is sanity-gated; a database row
the decoder cannot parse is counted as `undecodable` and surfaced by
`antigravity-doctor` and `antigravity-summary` — never guessed.

### What is never read (R21)

Prompt text (`history.jsonl` `display`, summaries `preview`), tool arguments in
step metadata, and `trajectory_metadata_blob` are never selected, read, or
persisted. See `PRIVACY.md`.

## Parity

| Surface | Status | Notes |
|---|---|---|
| Per-session tokens (in / out / cache-read / thinking) | 🟢 green | decoded from `gen_metadata` |
| Model + duration + tool-call counts | 🟢 green | |
| Context fill + quality grade | 🟢 green | 3-signal subset, like Hermes/Copilot |
| Trends DB + dashboard (port 24847) | 🟢 green | `platform=antigravity`, keyed per surface |
| Continuity restore | 🟡 partial | `SessionStart` injection was observed in CLI `agy 1.5.18`; desktop app and IDE are not live-validated |
| Bash output compression | 🟡 partial | Wired for the supported shell path; the current Windows bridge deliberately no-ops for `run_command`. No Windows live A/B was completed |
| Stop-time rollup | 🟢 green | detached, 60s budget, lease-debounced |
| Doctor / install / uninstall | 🟢 green | |
| Context-growth nudges | 🟡 partial | derived from the live conversation's last decoded generation, not hook payload |
| Crash recovery | 🟡 partial | killed/incomplete sessions flagged honestly; no in-flight tally needed (gen rows persist) |
| Compaction steering | 🔴 red | no `PreCompact` hook event |
| Read-interception (Delta / Structure Map) | 🟡 experimental | CLI `PreToolUse` redirected `read_file.Path` to a labeled summary in a disposable probe; no production read hook or automatic fallback is implemented, and `PostToolUse` cannot rewrite output |
| Status line | 🔴 red | Antigravity owns its status-line data model; no hook surface |
| Keep-warm | 🔴 red | no cache-TTL ping surface |

## Live validation and experiment report (2026-11-27)

This report records a Windows workstation experiment. It does **not** establish
parity with Claude Code, and the synthetic results below are not sufficient to
approve a general-purpose optimization. The CLI was the only surface exercised
live.

### Environment and scope

| Surface | Version observed | Live status | Notes |
|---|---|---|---|
| `agy` CLI | `1.5.18` | **Confirmed** | Full hook lifecycle and 5-rep balanced multi-file experiments run |
| Antigravity desktop app | `2.32.2.0` (`44.9.1` wrapper) | **Blocked** | Installed, no headless CLI or non-disruptive targetable window; hooks not exercised live |
| Antigravity VS Code / IDE | `google.google-antigravity 1.13.0` / IDE `2.5.5` | **Blocked** | Installed, no headless automation hook probe without disrupting active user IDE session |

The Token Optimizer plugin was absent from the global Antigravity plugin
directory and was **not installed** during these experiments. No global
permissions or settings were changed. Headless command-permission failures
were left in place; `--dangerously-skip-permissions` was not used.

### CLI hook contract observed

The probes persisted event names, payload field names, tool names, argument
field names, invocation indices, and error presence only. They did not persist
prompt text, tool argument values, or tool output. Common lifecycle payload
fields observed: `hookEventName`, `cwd`, `modelName`, `lastUserInput`,
`transcriptPath`, `workspacePaths`, `artifactDirectoryPath`, and
`conversationId`.

Source reference: Antigravity built-in customization guide (`agy-customizations/docs/hooks.md`), `agy changelog` (`1.5.18`), and live CLI probes.

| Event | Additional observed fields | Response/result observed | Status & limits |
|---|---|---|---|
| `SessionStart` | None beyond common fields | `injectSteps.systemMessage` reached the model in live CLI runs | **Confirmed** on CLI; desktop/IDE **not confirmed**. Bridge now gates restore by matching `- Working dir:` against `cwd`/`workspacePaths` |
| `PreInvocation` | `invocationNum` (`0`, `1`, ...), `initialNumSteps` | Single or multiple plain `injectSteps.systemMessage` entries reached the model on `invocationNum=0` (alone and combined with `SessionStart`) | **Confirmed** on CLI; **doc caveat:** adding `metadata.user_markdown` inside `systemMessage` suppressed the `systemMessage` text from reaching the model on `agy 1.5.18` (contrary to `hooks.md`) |
| `PostInvocation` | `invocationNum`, `initialNumSteps` | `{}` accepted for invocations `0` and `1` | **Confirmed** on CLI; no post-result rewrite observed |
| `PreToolUse` | `stepIdx`, `toolCall` (`name`, `args`) | `{"decision":"ask","overwrite":{"Path":"..."}}` redirected reads | **Confirmed** on CLI. Tool names in **historical** DBs differ (`antigravity-cli`/`antigravity` use `read_file`/`edit_file`/`write_file`/`run_command`; legacy `antigravity-ide` rows show `view_file`/`replace_file_content`/`grep_search`/`list_dir`, which do not confirm current IDE extension behavior) |
| `PostToolUse` | `stepIdx`, `toolCall`, `error` | `{}` accepted after reads (including redirected reads) | **Confirmed** on CLI; result rewriting **unsupported** |
| `Stop` | `continuationActive`, `executionNum`, `fullyIdle`, `terminationReason`, `error` | `{}` accepted (`NO_TOOL_CALL`, `fullyIdle=true`, `executionNum=0`) | **Confirmed** on CLI; post-completion only |

The adapter currently registers `PreToolUse` only for `run_command`; read-file
redirection was **not** added to the shipped bridge.

### Multi-file balanced experiment (ABBA / Latin square, 5 blocks = 20 runs)

To move beyond the initial synthetic 1-file pilot, all four strategies were
evaluated on a disposable 5-module Python mini-repo (688 lines with cross-file
imports, pricing/validation rules, and a subtle off-by-one bug) using
`gemini-4-neon-low`, fixed files, and alternating block order (5 repetitions per
condition). Input total = `fresh input + cache-read`; `thinking` is a subset of
`output` (`thinking + response == output`). Transient API failures (`exit=3`,
`status=ERROR`) are separated from `SUCCESS` completions below.

| Condition | `SUCCESS` / 5 | Task pass | Mean total input (`fresh` / `cache`) | Delta vs control | Mean output (`thinking`) | API / Tool calls | Mean wall |
|---|---:|---:|---:|---:|---:|---:|---:|
| `C0_control` (no hooks) | 3/5 | 3/3 | `74,839` (`23,500` / `51,339`) | `0.0%` | `1,737` (`1,424`) | `3.0` / `5.0` | `27.1s` |
| `C1_session_handoff` (`SessionStart` map) | 4/5 | 4/4 | **`44,950`** (`5,194` / `39,756`) | **`-39.9%`** | `1,734` (`1,361`) | **`2.0`** / `5.0` | **`21.5s`** |
| `C2_preinv_nudge` (`PreInvocation` generic nudge) | 3/5 | 3/3 | `75,271` (`11,223` / `64,047`) | `+0.6%` | `1,711` (`1,398`) | `3.0` / `5.0` | `27.7s` |
| `C3_pretool_ast_map` (`PreToolUse` AST redirect) | 2/5 | 2/2 | `73,910` (`29,833` / `44,077`) | `-1.2%` | `2,332` (`1,854`) | `3.0` / `7.0` | `33.5s` |

*(In an additional 1-rep pilot across all 4 conditions, `C1` cut total input by
`39.6%` with `2` API calls, whereas `C3` caused an extra 4th API turn and used
`+37.6%` more input tokens due to re-reading cached context across 10 tool calls.)*

Key findings:
- **`SessionStart` structural handoff is the primary optimization candidate** (`-39.9%`
  total input, `-5.6s` wall time, `4/4` task pass among `SUCCESS` runs, `1` fewer API round-trip). Because transient API errors occurred (`2/5` in `C0`, `1/5` in `C1`), larger sample sizes comparing all-attempt success, tokens, time, and rework are still needed before declaring a general-purpose win.
- **Generic `PreInvocation` nudges are neutral** (`+0.6%` input; identical tool
  and API call counts).
- **`PreToolUse` AST/summary redirection is not net-beneficial** on multi-file
  tasks: it raises output tokens (`+34.2%`), tool calls (`7.0` vs `5.0`), and
  latency (`+6.4s`), and regresses input tokens (`+37.6%`) whenever it triggers
  an extra API round-trip.

### Read-redirection stress-test findings

Tested separately on a multi-dependency module (`ledger.py` + `rates.py`):
- **Incomplete summaries trigger costly re-reads:** omitting helper/callee
  implementations caused a follow-up `StartLine`/`EndLine` read and consumed
  `65,973` vs `44,337` input tokens (**`+48.8%` worse**).
- **Missing redirect targets break tasks:** when `overwrite.Path` pointed to a
  nonexistent temp path, the CLI reported `file does not exist` and the model
  concluded the **original** file was missing, failing the task. Automatic
  fallback only happens when `PreToolUse` validates the temp summary before
  returning `overwrite` and omits `overwrite` on error.
- **Outside-repo cleanup & permissions:** `PostToolUse` deleted temp files
  outside the repo with `decision: "ask"` (no permission widening), but the
  overhead and failure risks do not justify shipping a production read matcher.

### Upstream Antigravity API gaps & observability updates

| Area | Claude Code | Antigravity | Status / Upstream gap |
|---|---|---|---|
| `PreToolUse` output rewrite | `PostToolUse` replaces command output | Only `PreToolUse` `overwrite` on args; `PostToolUse` returns `{}` | Upstream gap; Windows `run_command` compression remains **blocked** |
| Read substitution | Delta / Structure Map via `updatedInput` | Tested via `overwrite.Path`, not net-beneficial | Rejected after stress-test & ABBA benchmark |
| `user_markdown` UI-only note | N/A | Suppressed `systemMessage` model visibility on `agy 1.5.18` | Avoid `metadata.user_markdown` when model must read the message |
| Rate-card coverage | Full Anthropic/OpenAI cards | `gemini-3.6-flash` and earlier priced; `Gemini 3.8 Flash` / `Gemini 4 *` unpriced | Rate cards fall back cleanly to `antigravity_no_cost_data` |
| Cross-workspace restore | Per-project / session-scoped | Single global `restore-context.md` | **Fixed in adapter:** `handle_session_start` now checks `- Working dir:` against `cwd`/`workspacePaths` |
| Step/tool observability | Per-tool counts, errors, timings | Previously only `tool_call_count` | **Fixed in adapter:** `antigravity_state.py` & `antigravity_session.py` now aggregate per-tool counts, metadata-only `tool_error_count` (`steps.status` / `error_details IS NOT NULL`), `response_tokens`, and `avg`/`max`/`p95` `call_gap_seconds` (R21 preserved) |

### Synthetic A/B results

All conditions used `gemini-4-neon-high`, a disposable 727-line synthetic
Python file, and two repetitions per condition. “Input total” below means
`fresh input + cache-read`; output includes thinking, so those two figures must
not be added together. Each answer was checked against the expected result
`34.4`; each condition passed both repetitions.

| Comparison | Input total, mean | Tool calls per session | Outcome |
|---|---:|---:|---|
| Full read (`49,986`, `49,962`) vs. requested line ranges (`64,153`, `63,707`) | `49,974` vs. `63,930` | `1` vs. `2` `read_file` calls | Both correct; the interval workflow used about 28% more input tokens in this pilot |
| Full read (`50,309`, `50,200`) vs. short `SessionStart` handoff (`20,706`, `20,708`) | `50,254.5` vs. `20,707` | `1` vs. `0` | Both correct; handoff used about 58.8% fewer input tokens. Promising, but only one synthetic task |
| Full read (`50,003`, `50,082`) vs. `PreToolUse` generated function excerpt (`41,688`, `41,826`) | `50,042.5` vs. `41,757` | `1` vs. `1` | Both correct and accurately labeled `FULL FILE` / `SUMMARY ONLY`; summary condition used about 16.6% fewer input tokens |

For the summary-redirection comparison, mean fresh/cache-read input was
`21,210 / 28,832.5` tokens for the full-read control and
`13,656.5 / 28,100.5` for the summary condition. Mean output/thinking was
`498 / 432` versus `398 / 332`.

The redirect probe generated a 326-byte excerpt for the synthetic function,
clearly labeled it as a summary, and deleted the temporary file in
`PostToolUse`. The original remained present and readable. A separate
missing-destination test recovered by having the model request the original
again; this was **not** an automatic fallback. The probe and generated summary
were outside the repository, and the summary file was removed. The A/B
workspace hook configuration was reset to `{}`.
The disposable test directories remain in `%TEMP%`; do not remove them
recursively without the user's approval.

The interval test's two reads are counted as part of the strategy, not
discounted as an experimental artifact. A single-function excerpt is not proof
that a summary preserves imports, callers, invariants, or enough context for a
real code change.

### Recent locally stored usage (before the synthetic runs)

The read-only aggregation used the local conversation databases' generation
metadata and tool names for the preceding seven days, ending before the
synthetic runs began. It did not select prompts, tool arguments, or titles. Tool
categories are based on tool names; the reader does not expose command duration,
so “long commands” cannot be counted from this data.

| Store/surface | Sessions / generations | Fresh / cache-read input | Output / thinking | Tool calls by category |
|---|---:|---:|---:|---|
| `antigravity-cli` | `6 / 15` | `153,596 / 178,079` | `2,858 / 1,909` | `9`: 8 read, 1 command |
| `antigravity` desktop | `18 / 15,618` | `126,909,667 / 2,439,441,458` | `11,749,601 / 4,849,699` | `18,450`: 5,627 read, 182 search, 6,819 command, 5,822 other |
| `antigravity-ide` | `0 / 0` | `0 / 0` | `0 / 0` | `0` in the queried store/window |

Zero IDE-store sessions does not prove that the extension is unused; it only
describes the local store queried for this time window. The aggregation logic
and privacy boundary live in `antigravity_state.py` and
`antigravity_session.py`.

### Adapter changes already in the worktree

The preceding implementation pass changed the bridge and its mirrors to:

- restore context from `SessionStart` using `systemMessage`;
- handle the observed first `invocationNum` value of `0`;
- resolve nested transcript paths for the three Antigravity stores; and
- update bridge/installer tests and Antigravity documentation.

That pass reported 36 passing tests and 17 POSIX-specific skips, plus
documentation/payload-mirror checks. Those checks were not rerun as part of
this report-only update. The changes remain uninstalled and uncommitted in the
worktree. `PreToolUse` for read tools is still only a temporary experiment.

## Action plan for deeper validation

Use fresh disposable workspaces and keep each finding labeled **confirmed**,
**not confirmed**, or **blocked**. Do not infer platform support from source
code alone.

1. **Finish the hook matrix per surface.** On CLI, desktop app, and VS Code
   extension, record product/version, event names, payload field names,
   response shape accepted, and observed limits. Recheck `SessionStart`,
   `PreInvocation` (including `systemMessage` and deduplication),
   `PreToolUse`, `PostToolUse`, `PostInvocation`, and `Stop`. Do not log prompt
   contents or argument values.
2. **Establish reproducible baselines.** For each surface, fix model, task,
   repository commit, and permissions. Alternate conditions (for example
   ABBA), run at least five repetitions per condition, and record fresh input,
   cache-read, output, thinking, tool counts, elapsed time, task tests, and
   rework. Keep thinking as a subset of output.
3. **Repeat focused-reading tests on realistic tasks.** The current range
   workflow caused duplicate reads and lost to a full read. Compare symbol
   search, smaller ranges, diffs, and full reads while verifying the task with
   tests; do not approve a token reduction if correctness or rework worsens.
4. **Stress-test read redirection separately.** Require a clear partial-summary
   label, access to the original, recovery to the original when summary
   generation/read fails, cleanup outside the repository, and no permission
   widening. Test files with dependencies and multiple relevant symbols, not
   just one function. Do not add a production read matcher until these pass.
5. **Test command compression only with an interactive permission path.**
   Confirm the actual Windows `run_command` behavior and wrapper argv without
   auto-approving tools. The previous headless attempt was blocked; no
   compression A/B result exists.
6. **Expand the handoff experiment.** Repeat on realistic continuation tasks
   with verifiable completion, not just a one-line calculation. Confirm the
   restored context is sufficient and that reduced tokens do not increase
   tool calls, errors, or rework.
7. **Implement only a repeatable winner.** Update the canonical bridge,
   mirrors, tests, and documentation together; rerun focused/full checks; then
   install the user-level plugin only with explicit approval and repeat the
   surface-specific smoke tests.

Until these steps are complete, classify handoff and read redirection as
**promising CLI experiments**, interval reading as **not beneficial in the
current pilot**, command compression as **unvalidated on this Windows setup**,
and PostToolUse result rewriting as **unsupported by the observed contract**.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TOKEN_OPTIMIZER_ANTIGRAVITY_HOME` | `~/.gemini` | Relocated Antigravity home (accepted only under `$HOME`). |
| `TOKEN_OPTIMIZER_ANTIGRAVITY_BIN` | first `agy` on `PATH` | Override the `agy` binary the doctor probes. |
| `TOKEN_OPTIMIZER_BASH_COMPRESS` | `1` (on) | Set `0` to disable command-output compression. |

The approval prompt for a rewritten command shows the **wrapped** command
(`bash_compress.py …`), because that is what the permission system will actually
see. The wrapper re-checks its argv against the same whitelist and
dangerous-character check before executing anything, so a cached "always allow"
on the wrapper prefix never becomes an unguarded command runner (R13 + R13a).

## Uninstall

```bash
TOKEN_OPTIMIZER_RUNTIME=antigravity python3 skills/token-optimizer/scripts/measure.py antigravity-uninstall
```

Removes only `~/.gemini/config/plugins/token-optimizer/`. Your own hooks and
Antigravity conversation data are untouched. Collected data and trends stay in
place by design; to purge Token Optimizer's own data too:

```bash
rm -rf ~/.gemini/token-optimizer
```

## Live-smoke runbook (first run on a machine with Antigravity)

1. Install and authenticate the CLI (`agy`, then sign in).
2. `bash install.sh --antigravity`
3. `agy plugin validate ~/.gemini/config/plugins/token-optimizer` — expect `hooks: 1 processed`.
4. `measure.py antigravity-doctor` — expect zero `fail`.
5. `measure.py antigravity-summary` — expect per-surface sections with real token totals.
6. `measure.py antigravity-rollup` — expect "Collected N new Antigravity sessions".
7. Run one short `agy -p "run <a whitelisted command like git status>"` session and confirm the recorded step wraps the command through `bash_compress.py`; if the non-interactive turn cannot complete (auth/print-mode), record that and mark the live-injection parity rows partial.
