# Token Optimizer for Hermes

**Beta (v0.2.0).** The Token Optimizer feature set for [NousResearch Hermes](https://github.com/NousResearch/hermes-agent): smaller terminal and tool output, cheaper re-reads, loop warnings, checkpoints that survive compaction, prior-session hints, cost and quality tracking, and the shared dashboard.

Native Python plugin. Reads Hermes's own `~/.hermes/state.db` read-only. Model-agnostic. No telemetry, no network calls, no Python dependencies.

## What It Does

**Less context spent on output**
- **Terminal output compression** (`transform_terminal_output`): git, find, ls, test runners, builds, logs, lint, JSON, docker, kubectl and cloud CLI output is condensed to what matters. Failed commands and error output always come through whole. The full original is saved, and the output ends with a command that prints it again.
- **Large MCP results** (`transform_tool_result`): a preview plus a pointer to the saved copy. Repeating the exact same MCP call is answered from the saved copy (`pre_tool_call`).
- **Cheaper re-reads** (`pre_tool_call`): re-reading an unchanged file returns a short structure map; re-reading after an edit returns only the diff. Partial reads (offset/limit) are never blocked.
- **Loop warnings**: running the same command with the same output three times, a run of failures, or re-sending the same long inline script gets a one-line note asking for a different approach.
- **Repeated output dedup**: a command whose output repeats a recent run returns a short reference instead.

**Work that survives compaction and new sessions**
- **Checkpoints** of the active task, decisions, files, todos and next step, built from Hermes's own session messages: at 20/35/50/65/80% context fill, right before Hermes compacts, before the first subagent fan-out, after a batch of edits, at the end of each turn, on a fatal API error, and at session end. `/token-optimizer checkpoint` saves one on demand.
- **Restore after compaction** (`post_compaction`): the checkpoint comes back with the next turn or tool result, and files read before the compaction are served in full again.
- **Prior-session hints** (`pre_llm_call`): a new session gets a pointer to the last checkpoint, and every prompt is matched against recent checkpoints by topic (several languages), so "continue the parser work" brings back the right session.

**Nudges**
- **Context fill nudge**: one line at about 70% fill, only when Hermes's own compressor is off or failing. Hermes owns compression, so Token Optimizer stays quiet while it is healthy.
- **Lean-output nudge**: at 75-89% fill, a short note asks for focused answers. At most 3 per session, 5 minutes apart.
- **Subagent sprawl**: a note when 8 or more subagents run at once.

**Tracking**
- **Per-turn usage** (`post_api_request`) and a **session rollup** into the shared `trends.db` at session end, with skill usage (`on_skill_lifecycle`) and every compression saving attributed to the session.
- **Credential safety**: archives, checkpoints and logs are redacted, and Token Optimizer's credential patterns (plus your custom pattern file) are registered with Hermes's own redactor.
- **`/token-optimizer`** slash command and **`hermes token-optimizer`** CLI subcommand (dashboard).

The full feature-by-feature comparison with Claude Code and Codex is in [`docs/hermes-parity.md`](../docs/hermes-parity.md).

## Install

Clone the repo and run the bundled installer:

```bash
git clone https://github.com/alexgreensh/token-optimizer.git
token-optimizer/install.sh --hermes
```

Preview without writing anything: `token-optimizer/install.sh --hermes --dry-run`. (Direct equivalent: `python3 token-optimizer/skills/token-optimizer/scripts/measure.py hermes-install`.)

The installer copies the plugin into `~/.hermes/plugins/token-optimizer/` and writes a small `measure-path` file pointing back at your checkout, which is where the compression and checkpoint engines run from. Keep the checkout; `git pull` in it updates the engines, and re-running the installer refreshes the plugin files.

Hermes also needs the plugin allow-listed in its config:

```yaml
plugins:
  enabled:
    - token-optimizer
```

Add that to `~/.hermes/config.yaml`, or let the installer do it with `--enable`:

```bash
python3 token-optimizer/skills/token-optimizer/scripts/measure.py hermes-install --enable
```

`--enable` is idempotent and backs up your config before touching it. If your config layout is unusual, the installer leaves it alone and prints the snippet to add.

**HERMES_HOME override**: if your Hermes home is not at `~/.hermes`, set `HERMES_HOME=/path/to/.hermes` before installing.

**Upgrading from 0.1.0**: re-run the installer, then restart Hermes. `hermes-doctor` warns when the installed copy is older than your checkout.

**Verify the install:**

```bash
python3 token-optimizer/skills/token-optimizer/scripts/measure.py hermes-doctor
```

The doctor loads the installed plugin and lists every hook it registers (17), checks the engine scripts in your checkout, the credential patterns shared with Hermes, whether the installed copy is current, the `plugins.enabled` entry, `state.db` readability, and the dashboard port.

**Uninstall:**

```bash
token-optimizer/install.sh --hermes --uninstall
```

Then remove `- token-optimizer` from `plugins.enabled` in your Hermes config.

## Usage Inside Hermes

Everything above runs on its own once the plugin is installed and allow-listed.

| Surface | How to use |
|---------|-----------|
| `/token-optimizer` | Token and cost summary for recent sessions. |
| `/token-optimizer cost [words]` | What a past session cost, e.g. `/token-optimizer cost yesterday parser`. |
| `/token-optimizer checkpoint` | Save a checkpoint of this session now. |
| `/token-optimizer expand <id>` | Print a saved tool result in full. |
| `hermes token-optimizer` | Opens the dashboard at `http://localhost:24844`. `--port <n>` to change it. |

Saved output can also be read with the command printed under it, for example:

```
python3 /path/to/token-optimizer/skills/token-optimizer/scripts/measure.py --runtime hermes expand 8616734fdb056ab2
```

## Turning Features Off

Each feature has its own switch. Set it to `0` in the environment Hermes runs in:

| Variable | Turns off |
|---|---|
| `TOKEN_OPTIMIZER_HERMES_TERMINAL` | terminal output compression and loop warnings |
| `TOKEN_OPTIMIZER_HERMES_TOOL_RESULTS` | tool result archive and MCP previews |
| `TOKEN_OPTIMIZER_HERMES_READ_CACHE` | structure-map and diff re-reads |
| `TOKEN_OPTIMIZER_HERMES_REFETCH_GUARD` | answering repeated MCP calls from the saved copy |
| `TOKEN_OPTIMIZER_HERMES_CHECKPOINTS` | all checkpoints |
| `TOKEN_OPTIMIZER_HERMES_RESTORE` | restore after compaction |
| `TOKEN_OPTIMIZER_HERMES_CONTINUITY` | prior-session hints |
| `TOKEN_OPTIMIZER_HERMES_VERBOSITY` | lean-output nudge |
| `TOKEN_OPTIMIZER_HERMES_REDACTION` | sharing credential patterns with Hermes |

The shared switches work too: `TOKEN_OPTIMIZER_BASH_COMPRESS=0` stops terminal compression but keeps loop warnings, and `TOKEN_OPTIMIZER_READ_CACHE=0` stops re-read savings.

## Context-Quality Score

Each session is scored 0-100 from the signals Hermes's session rows carry.

| Signal | Weight | What it measures |
|--------|--------|-----------------|
| Context usage | 40% | Latest provider-reported prompt tokens over the model's context window. Omitted when unavailable; cumulative totals are never used as occupancy. |
| Message count risk | 35% | Session length against a risk curve (20 messages or fewer scores 100; over 100 scores 10). |
| Output / input ratio | 25% | Output tokens over input tokens. |

Cache hit rate, compaction depth and API calls per turn are left out of the score: Hermes documents `cache_read_tokens` as unreliable, the sessions row has no successful-compaction count, and call counts do not compare with Claude Code turns.

Grades: **S** (90-100), **A** (80-89), **B** (70-79), **C** (55-69), **D** (40-54), **F** (0-39), the same on every platform.

## Savings

Compression savings are measured per call and stored with the Hermes session. Session cost comes from Hermes's own `estimated_cost_usd`, falling back to Token Optimizer's pricing for the stored token counts. Before/after savings compare your average cost per session before and after you started using Token Optimizer; it is a trend, not a per-action guarantee. Two small imprecisions (cache writes priced as input, under-reported cache reads) both make the saving look smaller, not larger.

## Privacy and Safety

- `state.db` is opened read-only (`mode=ro`, `PRAGMA query_only = ON`). Token Optimizer never writes to it.
- Engines run in short child processes, never inside Hermes. Every hook catches its own errors; a failure leaves Hermes's output and turn exactly as they were. Hooks that run before a tool stay well inside Hermes's hook timeout, because Hermes blocks a tool when that hook times out.
- Archives and checkpoints are redacted before they are written, and stored with private permissions under `~/.hermes/`. A checkpoint is built from a temporary private file that is deleted right after.
- No telemetry. No network calls.

## Model Support

Model-agnostic: Hermes routes to Nous Portal, OpenRouter, Anthropic, OpenAI, Gemini or a local model. Context windows are mapped for common Claude, GPT and Gemini models; others fall back to 200,000 tokens.

## Requirements

- Python 3 (the interpreter Hermes runs on)
- A working Hermes install
- The Token Optimizer repo cloned locally (the plugin runs its engines from there)

**Beta status:** fully tested against Hermes's documented hook contract (upstream commit `7ee6d99c`), not yet validated against a live Hermes install. File issues on the [Token Optimizer repo](https://github.com/alexgreensh/token-optimizer).

## How It Works

```
Hermes turn
  pre_llm_call ............ restore / prior-session hints / lean-output / fill nudge
  post_api_request ........ token tally, fill-band checkpoints
  pre_tool_call ........... read_file: structure map or diff; mcp__*: saved copy; delegate_task: checkpoint
  transform_terminal_output  compression, dedup, loop warnings, archive + expand pointer
  transform_tool_result ... MCP preview, archive, read-cache refresh after edits, pending notes
  post_tool_call .......... edit-batch checkpoint
  pre_auxiliary_call ...... checkpoint right before Hermes compacts
  post_compaction ......... forget pre-compaction reads, queue the restore
  api_request_error ....... checkpoint on a fatal error
  subagent_start/stop ..... sprawl note
  on_skill_lifecycle ...... skill usage counts
  on_session_end .......... (every turn) checkpoint
  on_session_finalize ..... end checkpoint, rollup into trends.db, dashboard refresh
  on_session_start/reset/delete  per-session state

Engines (child processes, from your checkout)
  hermes_tool_hooks.py .... bash_compress_hook, archive_result, context_intel, read_cache, refetch_guard
  measure.py hermes-checkpoint ... state.db messages -> checkpoint
  measure.py hermes-turn-context . compact_restore, continuity hints, lean-output tiers
  measure.py hermes-rollup ....... state.db sessions -> trends.db
```

Data flows one way: from Hermes's `state.db` into Token Optimizer's own files. The plugin never writes back.

## License

PolyForm Noncommercial 1.0.0
