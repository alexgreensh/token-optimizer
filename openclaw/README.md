# Token Optimizer for OpenClaw

Version: `2.2.0`

**Your AI is getting dumber and you can't see it.**

*Find the ghost tokens. Survive compaction. Track the quality decay.*

Opus 4.6 drops from 93% to 76% accuracy across a 1M context window. Compaction loses 60-70% of your conversation. Ghost tokens burn through your plan limits on every single message. Token Optimizer tracks the degradation, cuts the waste, checkpoints your decisions before compaction fires, and tells you what to fix.

Native TypeScript plugin for OpenClaw agent systems. Zero Python dependency. Works with any model (Claude, GPT-5, Gemini, DeepSeek, Qwen, Mistral, Grok, local via Ollama). Reads your OpenClaw pricing config for accurate cost tracking, falls back to built-in rates for 30+ models.

## Install

```sh
# From GitHub (recommended)
openclaw plugins install github:alexgreensh/token-optimizer

# From ClawHub
openclaw plugins install token-optimizer
```

Or from source:

```sh
git clone https://github.com/alexgreensh/token-optimizer
cd token-optimizer/openclaw && npm install && npm run build
openclaw plugins install ./
```

Inside OpenClaw, run `/token-optimizer` for a guided audit with coaching.

## What It Does

- **Scans** all agent sessions for token usage, cost, and topic extraction
- **Detects** 15 waste patterns across 3 tiers with monthly $ savings and fix snippets
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

```sh
npx token-optimizer detect                                            # Is OpenClaw installed?
npx token-optimizer scan --days 30                                    # Scan sessions, show usage
npx token-optimizer audit --days 30                                   # Detect waste, show $ savings
npx token-optimizer audit --json                                      # JSON output for agents
npx token-optimizer dashboard                                         # Generate HTML dashboard, open in browser
npx token-optimizer context                                           # Show context overhead breakdown
npx token-optimizer context --json                                    # Context audit as JSON
npx token-optimizer quality                                           # Show quality score (0-100)
npx token-optimizer quality --json                                    # Quality report as JSON
npx token-optimizer git-context                                       # Suggest files based on git state
npx token-optimizer git-context --json                                # Git context as JSON
npx token-optimizer drift                                             # Check for config drift
npx token-optimizer drift --snapshot                                  # Capture current config snapshot
npx token-optimizer validate                                          # Before/after impact comparison
npx token-optimizer validate --strategy auto --json                   # Auto-split strategy, JSON output
npx token-optimizer doctor --json                                     # Check checkpoint health, plugin status
TOKEN_OPTIMIZER_CHECKPOINT_TELEMETRY=1 npx token-optimizer checkpoint-stats  # Checkpoint telemetry summary
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

Dashboard auto-regenerates on session end. Open manually with `npx token-optimizer dashboard`.

## Waste Patterns Detected

15 detectors across 3 tiers:

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
npx token-optimizer validate                     # Auto-detect split point
npx token-optimizer validate --strategy halves   # Chronological midpoint
npx token-optimizer validate --json              # JSON output
```

Measures: average tokens, cost, messages, and cache hit rate. Reports percentage deltas and a verdict (improved / regressed / no_change).

## Drift Detection

```sh
npx token-optimizer drift --snapshot      # Save current state
# ... time passes, skills added, configs changed ...
npx token-optimizer drift                 # See what changed
```

Tracks: skill count, agent count, SOUL.md/MEMORY.md/AGENTS.md/TOOLS.md size changes, model config changes, cron configs.

## Git-Aware Context

```sh
npx token-optimizer git-context           # Suggest files based on git state
npx token-optimizer git-context --json    # JSON output
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
Compression (requires a tool-result mutation hook that the OpenClaw plugin
API does not yet provide), Live Quality Bar (status line), Memory Health
audit, Attention Optimizer, JSONL trim/dedup tools, routing injection,
tool result archive.

## License

PolyForm Noncommercial 1.0.0
