# Token Optimizer for OpenClaw

Version: `2.4.21`

**Your AI is getting dumber and you can't see it.**

*Find the ghost tokens. Survive compaction. Track the quality decay.*

Opus 4.6 drops from 93% to 76% accuracy across a 1M context window. Compaction drops detail. Ghost tokens burn through your plan limits on every single message. Token Optimizer tracks the degradation, cuts the waste, checkpoints your decisions before compaction fires, and tracks recovery after.

Native TypeScript plugin for OpenClaw agent systems. Zero Python dependency. Works with any model (Claude, GPT-5, Gemini, DeepSeek, Qwen, Mistral, Grok, local via Ollama). Reads your OpenClaw pricing config for accurate cost tracking, falls back to built-in rates for 30+ models.

## Install

From source (the supported path):

```sh
git clone https://github.com/alexgreensh/token-optimizer
cd token-optimizer/openclaw
npm install
npm run build
```

This plugin's `package.json` lives inside the `openclaw/` subdirectory, not the
repo root, so clone, install, and build from there. After `npm run build`, the
CLI entrypoint is `openclaw/dist/cli.js`, which is what the `bin` field in
`openclaw/package.json` points to.

To register the built checkout with OpenClaw's plugin manager, run from the
`openclaw/` directory:

```sh
openclaw plugins install ./
```

Then grant conversation access in `openclaw.json`. The in-session continuity
hints, cold-resume and nudges run on OpenClaw's `agent_turn_prepare` hook, which
a non-bundled plugin may use only with this explicit grant:

```json
{
  "plugins": {
    "entries": {
      "token-optimizer-openclaw": {
        "enabled": true,
        "hooks": { "allowConversationAccess": true }
      }
    }
  }
}
```

Merge the entry into your existing config. Without the grant the plugin still
installs and the CLI, detectors and dashboard work, but the in-session
contributions stay silent. See the [OpenClaw hook
permissions](https://docs.openclaw.ai/plugins/hooks#permissions-and-scope) for
the upstream rule.

Inside OpenClaw, run `/token-optimizer` for a guided audit with coaching.

> Note: do not use `npx token-optimizer` or `openclaw plugins install
> token-optimizer`. The npm package name `token-optimizer` is an unrelated
> project, and `token-optimizer-openclaw` is not published to the npm registry.
> Run the CLI from this checkout as `node dist/cli.js` (see CLI below).

## Uninstall

Token Optimizer for OpenClaw is installed and removed through OpenClaw's
native plugin manager; there is no repo-side removal script. The plugin id
is `token-optimizer-openclaw`.

```sh
# Preview what would be removed (no changes written)
openclaw plugins uninstall token-optimizer-openclaw --dry-run

# Remove the plugin: deletes the managed install directory under
# $OPENCLAW_STATE_DIR/extensions/, removes the plugin record from
# plugins.entries, and clears allow/deny + load-path entries.
openclaw plugins uninstall token-optimizer-openclaw

# Keep the on-disk files (only removes the config records):
openclaw plugins uninstall token-optimizer-openclaw --keep-files
```

`openclaw plugins uninstall` is the exact inverse of `openclaw plugins
install` for a managed install: it removes everything the install created.
For a from-source install (`openclaw plugins install ./`), the same command
removes the plugin record; the source checkout under `token-optimizer/openclaw/`
is yours and is never touched by the uninstaller.

### What is left behind by design

Session data and trends are NOT removed by the plugin uninstaller (they live
outside the plugin directory, under OpenClaw's state dir). To purge them too:

```sh
# Token Optimizer's OpenClaw session/trends data (optional, full wipe):
rm -rf "${OPENCLAW_STATE_DIR:-$HOME/.local/state/openclaw}/token-optimizer"
```

You can also temporarily disable the plugin without removing it:

```sh
openclaw plugins disable token-optimizer-openclaw   # stop loading
openclaw plugins enable  token-optimizer-openclaw   # re-enable
```

## What It Does

- **Scans** all agent sessions for token usage, cost, and topic extraction
- **Detects** 16 waste patterns across 3 tiers with monthly $ savings and fix snippets
- **Dashboard** with 8-tab interactive HTML visualization
- **Context audit** with per-skill and per-MCP-server token breakdown
- **Quality scoring** with 7 signals (2-stage) and model-aware context windows (Claude 1M, GPT-5 400K, Gemini 2M)
- **Coach mode** with health scoring, pattern detection, costly prompt ranking, and agent cost breakdown
- **Read-cache** intercepts redundant file reads with warn/block modes and `.contextignore` support
- **Smart Compaction v2** preserves decisions, errors, and user instructions during compaction
- **Progressive checkpoints** fire on fill bands `20/35/50/65/80`, quality drops `80/70/50/40`, and milestones like `pre-fanout` / `edit-batch`
- **Local checkpoint telemetry** is opt-in with `TOKEN_OPTIMIZER_CHECKPOINT_TELEMETRY=1`
- **Impact validation** compares before/after session metrics to measure optimization effectiveness
- **Drift detection** snapshots config and diffs to catch creep
- **Git-aware context** suggests files based on git state (test companions, co-changed files)
- **Per-turn token breakdown** with cache analysis across all providers (Claude, GPT-5, Gemini)
- **Costly prompt ranking** identifies your most expensive user prompts by cost
- **Agent cost analysis** breaks down orchestrator vs worker spending
- **Model switch simulation** estimates savings from routing to cheaper models
- **Distortion bounds analysis** estimates quality ceiling using context capacity heuristics
- **Manage tab** to toggle skills and MCP servers on/off (accumulated clipboard commands)

## CLI

Run from the `openclaw/` directory after `npm run build`. The CLI entrypoint is
`dist/cli.js` (the `bin` field in `openclaw/package.json`):

```sh
node dist/cli.js detect                                            # Is OpenClaw installed?
node dist/cli.js scan --days 30                                    # Scan sessions, show usage
node dist/cli.js audit --days 30                                   # Detect waste, show $ savings
node dist/cli.js audit --json                                      # JSON output for agents
node dist/cli.js dashboard                                         # Generate HTML dashboard, open in browser
node dist/cli.js context                                           # Show context overhead breakdown
node dist/cli.js context --json                                    # Context audit as JSON
node dist/cli.js quality                                           # Show quality score (0-100)
node dist/cli.js quality --json                                    # Quality report as JSON
node dist/cli.js git-context                                       # Suggest files based on git state
node dist/cli.js git-context --json                                # Git context as JSON
node dist/cli.js drift                                             # Check for config drift
node dist/cli.js drift --snapshot                                  # Capture current config snapshot
node dist/cli.js validate                                          # Before/after impact comparison
node dist/cli.js validate --strategy auto --json                  # Auto-split strategy, JSON output
node dist/cli.js doctor --json                                     # Check checkpoint health, plugin status
TOKEN_OPTIMIZER_CHECKPOINT_TELEMETRY=1 node dist/cli.js checkpoint-stats  # Checkpoint telemetry summary
```

## Dashboard

The interactive dashboard has 8 tabs:

| Tab | What It Shows |
|-----|--------------|
| Overview | Stat cards (runs, cost, quality score, savings), agent cards, context overhead bar |
| Context | Per-component token breakdown, individual skill bars, MCP server list, recommendations |
| Quality | 7-signal quality score (0-100) with per-signal breakdown, distortion bounds, recommendations |
| Waste | Waste cards with severity, confidence, fix snippets with Copy Fix button |
| Agents | Per-agent cost, model mix stacked bars (multi-model only), top agents table |
| Sessions | Individual session history grouped by date with outcome, cost, model, and per-session quality |
| Daily | Daily cost/token and run count charts with Y-axis labels and custom tooltips |
| Manage | Toggle skills and MCP servers on/off. Changes accumulate, copy all at once |

Dashboard auto-regenerates on session end. Open manually with `node dist/cli.js dashboard` (run from the `openclaw/` directory).

## Waste Patterns Detected

16 detectors across 3 tiers:

### Tier 1: Config & Heartbeat Analysis

| Pattern | What It Means | Typical Savings |
|---------|--------------|-----------------|
| Heartbeat Model Waste | Cron agent using opus/sonnet instead of haiku | $2-50/month |
| Heartbeat Over-Frequency | Checking more often than every 5 minutes | $1-10/month |
| Stale Cron Config | Hooks pointing to non-existent paths | Varies |

### Tier 2: Session Log Analysis

| Pattern | What It Means | Typical Savings |
|---------|--------------|-----------------|
| Empty Runs | Loading 50K+ tokens, finding nothing to do | $2-30/month |
| Session History Bloat | 500K+ tokens without compaction | 40% of bloated input |
| Loop Detection | 20+ messages with near-zero output | $1-20/month |
| Abandoned Sessions | Started, loaded context, then left | $0.20-5/month |
| Ghost Tokens (QJL) | Near-duplicate context loaded with <100 token output | $1-50/month |
| Retry Churn | 10+ messages ending in failure (retry storms) | $0.50-10/month |
| Tool Cascade | 5+ tool calls ending in failure (error chains) | $0.50-10/month |
| Overpowered Model | Expensive model for simple tasks (low output, few tools) | $0.50-20/month |
| Weak Model | Cheap model for complex tasks (causes errors/retries) | Quality impact |
| Output Waste | Sessions with >40% output tokens vs total (verbose responses) | $0.50-15/month |
| Bad Decomposition | 30+ messages with <2% output ratio (monolithic prompts) | $1-15/month |

### Tier 3: Context Composition

| Pattern | What It Means | Typical Savings |
|---------|--------------|-----------------|
| Tool Loading Overhead | Sessions loading 15+ tools without compact view | Token overhead |
| Unused Skills | Installed skills never invoked across sessions | ~100 tok/skill/msg |

## Quality Signals

7-signal two-stage quality metric adapted for OpenClaw's architecture.

### Stage 1: Coarse Signals (80% weight)

| Signal | Weight | What It Measures |
|--------|--------|-----------------|
| Context Fill | 20% | Token usage relative to model context window (per-model: Claude 1M, GPT-5 400K, Gemini 2M) |
| Session Length Risk | 16% | Message count vs compaction threshold (50 messages) |
| Model Routing | 16% | Expensive models used for heartbeat/cron tasks |
| Empty Run Ratio | 16% | Runs that load context but produce nothing |
| Outcome Health | 12% | Success vs abandoned/empty/failure ratio |

### Stage 2: Semantic Signals (20% weight)

| Signal | Weight | What It Measures |
|--------|--------|-----------------|
| Message Efficiency | 10% | Output-to-total token ratio (signal-to-noise) |
| Compression Opportunity | 10% | Input redundancy across runs (near-duplicate detection) |

Scores map to grades: S (90-100), A (80-89), B (70-79), C (55-69), D (40-54), F (0-39).

Includes **distortion bounds analysis**: estimates theoretical quality ceiling based on context capacity, showing how close you are to optimal.

## Coach Mode

Health scoring and anti-pattern detection for your entire OpenClaw setup:

- **SOUL.md analysis**: flags oversized personality files (>3% of context)
- **Usage tracking**: confirms session data collection is active
- **Unused skill detection**: ratio-based severity (>80% unused = high)
- **MCP server overhead**: flags >20 servers consuming >3% of context
- **Model routing**: detects >85% expensive model usage across sessions
- **Costly prompt ranking**: top 5 most expensive user prompts by cost
- **Agent cost breakdown**: orchestrator vs worker spending analysis

Generates a health score (0-100) with actionable patterns (good and bad).

## Read-Cache

Intercepts redundant file reads to prevent re-reading the same file within a session:

- **Warn mode**: logs a warning when a cached file is re-read
- **Block mode**: returns cached content instead of re-reading
- **`.contextignore` support**: exclude files from being read into context
- **Cache persistence**: maintains cache across the session lifecycle

## Context Audit

Scans every component OpenClaw injects into context:

| Component | Source | Optimizable |
|-----------|--------|-------------|
| Core system prompt | Built-in (~15K tokens est.) | No |
| SOUL.md | Personality/instructions | Yes |
| MEMORY.md | Persistent memory | Yes |
| AGENTS.md | Agent definitions | Yes |
| TOOLS.md | MCP tool definitions | Yes |
| Skills | Individual SKILL.md files (~100 tok each) | Yes (archive unused) |
| Agent configs | Per-agent config.json | Yes |
| Cron configs | cron/*.json | Yes |
| MCP Servers | config.json mcpServers | Yes (disable unused) |

Generates per-component token counts, individual skill bars, and actionable recommendations.

## Smart Compaction v2

Hooks into `session:compact:before` and `session:compact:after`. Instead of saving the last 20 raw messages (v1), v2 extracts:

- **User instructions**: "always", "never", "make sure" directives
- **Decisions**: "decided to", "going with", "switching to"
- **Errors**: stack traces, error messages, failure patterns
- **File changes**: write, edit, create operations

Result: more relevant context in fewer tokens after compaction.

Includes deduplication via semantic digest fingerprinting — identical checkpoint content is not written twice.

### Progressive Checkpoints

Checkpointing is no longer just "wait until the window is almost full." The runtime captures:

- Fill bands at `20%`, `35%`, `50%`, `65%`, and `80%`
- First quality drops below `80`, `70`, `50`, and `40`
- Milestones before agent fan-out and after a meaningful edit batch
- Cooldown periods prevent checkpoint spam
- Optional local telemetry in `checkpoint-stats` so you can see whether the policy is firing in real sessions

Checkpoint priority ranking ensures the most important checkpoints (milestones > quality drops > progressive fills) are restored first.

## Impact Validation

Compare session metrics before and after an optimization:

```sh
node dist/cli.js validate                     # Auto-detect split point
node dist/cli.js validate --strategy halves   # Chronological midpoint
node dist/cli.js validate --json              # JSON output
```

Measures: average tokens, cost, messages, and cache hit rate. Reports percentage deltas and a verdict (improved / regressed / no_change).

## Drift Detection

```sh
node dist/cli.js drift --snapshot      # Save current state
# ... time passes, skills added, configs changed ...
node dist/cli.js drift                 # See what changed
```

Tracks: skill count, agent count, SOUL.md/MEMORY.md/AGENTS.md/TOOLS.md size changes, model config changes, cron configs.

## Git-Aware Context

```sh
node dist/cli.js git-context           # Suggest files based on git state
node dist/cli.js git-context --json    # JSON output
```

Suggests relevant files based on your current git state: test companions, co-changed files, and import chain analysis.

## Pricing

Covers 30+ models with verified March 2026 rates:

- **Anthropic**: Opus, Sonnet, Haiku (1M context)
- **OpenAI**: GPT-5.4/5.2/5.1/5/5-mini/5-nano, GPT-4.1/4.1-mini/4.1-nano, GPT-4o/4o-mini, o3/o3-pro/o3-mini/o4-mini
- **Google**: Gemini 3-pro/3-flash/3.1-pro, 2.5-pro/2.5-flash, 2.0-flash/2.0-flash-lite
- **DeepSeek**: v3, R1
- **Alibaba**: Qwen3, Qwen3-mini, Qwen-Coder
- **Moonshot**: Kimi K2.5
- **MiniMax**: MiniMax-2
- **Zhipu**: GLM-4.7, GLM-4.7-flash
- **Xiaomi**: MiMo-flash
- **Mistral**: Large 3, Small
- **xAI**: Grok-4
- **Local**: Ollama, LM Studio (free, token tracking only)

4 pricing tiers: Anthropic API (direct), Google Vertex AI (global), Vertex AI (regional, +10%), AWS Bedrock.

User-configured pricing overrides via `openclaw.json`. Model switch simulation estimates savings from routing to cheaper models.

## What's Different from Claude Code

The OpenClaw plugin includes its own 7-signal ContextQ with signals native to OpenClaw's architecture (Message Efficiency, Compression Opportunity, Model Routing, etc.) rather than a direct port of Claude Code's signals. The Coach tab adapts scoring to OpenClaw concepts (SOUL.md instead of CLAUDE.md, agent configs instead of hooks).

**Ported from Claude Code in v2.3.0:** Delta Mode (line-diff on re-reads),
Structure Map Beta (structural digests on redundant reads), v5 feature
registry + telemetry JSONL, dashboard Overview card, first-run welcome
prompt, and the `v5 status | info | enable | disable | welcome`
CLI subcommand.

**Still not ported from Claude Code:** Quality Nudges and Loop Detection
(require a session-visible notification surface that the current OpenClaw
plugin API does not expose; revisit when that lands), Bash Output
Compression (requires a tool-input mutation hook that the OpenClaw plugin
API does not yet provide), Context Intel Promotion (post-compaction tool
output digest), MCP Tool Introspection (Claude Code-specific server
config), Live Quality Bar (status line), Memory Health audit, Attention
Optimizer, JSONL trim/dedup tools, routing injection, tool result archive.

## License

PolyForm Noncommercial 1.0.0
