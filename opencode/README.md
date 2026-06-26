# Token Optimizer for OpenCode

Context quality scoring, smart compaction, and session continuity for [OpenCode](https://github.com/anomalyco/opencode). Full parity with the Claude Code Token Optimizer plugin.

## What It Does

Token Optimizer monitors your OpenCode sessions and helps you get the most out of your context window:

- **7-signal quality scoring** with dual ResourceHealth (monotonic) + SessionEfficiency (rolling window) architecture
- **Smart compaction** with mode-aware PRESERVE/DROP guidance (code/debug/review/infra/general)
- **Session continuity** that restores context from prior sessions via keyword matching
- **Quality nudges** that warn when context health drops, fill exceeds thresholds, or retry loops are detected
- **Dashboard** with quality trends, session history, and daily aggregates
- **`token_status` tool** for on-demand quality reports
- **`token_dashboard` tool** to generate and open the visual dashboard

## Install

Add the plugin to your `opencode.json` (or `.opencode/opencode.jsonc`) `plugin`
array. OpenCode resolves and installs it from npm on the next launch:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["token-optimizer-opencode"]
}
```

The `plugin` array holds npm package names as **plain strings** — that is
OpenCode's config schema. No separate install command is needed.

### Offline / no-npm install

If you can't (or don't want to) install from npm, clone the repo and run the
bundled installer. It builds a single-file plugin and copies it into
`~/.config/opencode/plugins/`, which OpenCode auto-loads at startup:

```bash
git clone https://github.com/alexgreensh/token-optimizer.git
token-optimizer/install.sh --opencode
```

Requires [bun](https://bun.sh) (OpenCode's own runtime). Re-run after a
`git pull` to update.

## Configure

OpenCode's `plugin` array takes **package-name strings only** — it does not
accept an inline options object, and a `["name", { … }]` tuple will fail config
validation and stop OpenCode from starting. Configure Token Optimizer through
environment variables instead (full list below):

```bash
# Example: widen the quality window and disable loop detection
export TOKEN_OPTIMIZER_QUALITY_WINDOW=30
export TOKEN_OPTIMIZER_LOOP_DETECTION=false
```

All settings are optional; defaults are shown in the table below.

## Environment Variables

Override any threshold via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `TOKEN_OPTIMIZER_QUALITY_WINDOW` | 20 | Rolling window size for ratio signals |
| `TOKEN_OPTIMIZER_TOOL_CALL_WARN` | auto | Tool call warning threshold (scales with context window) |
| `TOKEN_OPTIMIZER_TOOL_CALL_CRITICAL` | auto | Tool call critical threshold |
| `TOKEN_OPTIMIZER_CHECKPOINT_RETENTION_DAYS` | 7 | Days to keep checkpoints |
| `TOKEN_OPTIMIZER_CHECKPOINT_RETENTION_MAX` | 50 | Max checkpoints to scan for restore |
| `TOKEN_OPTIMIZER_RELEVANCE_THRESHOLD` | 0.3 | Min relevance score for checkpoint restore |
| `TOKEN_OPTIMIZER_NUDGES` | true | Enable quality nudges |
| `TOKEN_OPTIMIZER_LOOP_DETECTION` | true | Enable retry loop detection |
| `TOKEN_OPTIMIZER_SMART_COMPACTION` | true | Enable compaction context injection |
| `TOKEN_OPTIMIZER_CONTINUITY` | true | Enable session continuity |
| `TOKEN_OPTIMIZER_ACTIVITY` | true | Enable activity tracking |
| `TOKEN_OPTIMIZER_TRENDS` | true | Enable trends collection |

## Quality Scoring

The quality score uses a dual-composite architecture:

**ResourceHealth** (monotonic, can only decrease within a session):
- Context fill degradation (50%) - MRCR-curve-based quality estimate
- Compaction depth (30%) - information loss from repeated compaction
- Absolute waste tokens (20%) - stale reads + bloated results

**SessionEfficiency** (rolling window, can rise or fall):
- Stale reads (30%) - re-reading files after writing them
- Bloated results (30%) - large tool outputs never referenced
- Decision density (20%) - ratio of substantive messages
- Agent efficiency (20%) - agent dispatch result/prompt ratio

Grades: S (90+), A (80+), B (70+), C (55+), D (40+), F (<40)

## Hooks Used

| Hook | Purpose |
|------|---------|
| `chat.message` | Track user messages, trigger quality scoring |
| `tool.execute.before` | Record file reads |
| `tool.execute.after` | Record tool results, file writes, agent dispatches, activity tracking |
| `experimental.chat.system.transform` | Inject warnings, restore session continuity |
| `experimental.session.compacting` | Inject mode-aware compaction guidance, capture checkpoint |
| `experimental.compaction.autocontinue` | Reset signals post-compaction, refresh quality |
| `event` | Handle session lifecycle (created/deleted) |

## Model Support

Context window sizes are mapped for 30+ models across all major providers:
Anthropic (Opus/Sonnet 1M, Haiku 200K), OpenAI (GPT-5.x, GPT-4.1, o3/o4), Google (Gemini 2.x/3.x),
DeepSeek, Qwen, Mistral, xAI Grok, and more.

MRCR quality curves are calibrated per model family for accurate fill-degradation estimates.

## License

PolyForm Noncommercial 1.0.0
