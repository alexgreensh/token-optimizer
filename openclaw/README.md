# Token Optimizer for OpenClaw

Version: `1.3.1`

**Your AI is getting dumber and you can't see it.**

*Find the ghost tokens. Survive compaction. Track the quality decay.*

Opus 4.6 drops from 93% to 76% accuracy across a 1M context window. Compaction loses 60-70% of your conversation. Ghost tokens burn through your plan limits on every single message. Token Optimizer tracks the degradation, cuts the waste, checkpoints your decisions before compaction fires, and tells you what to fix.

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

## What It Does

- **Scans** all agent sessions for token usage and cost
- **Detects** 7 waste patterns with monthly $ savings and fix snippets
- **Dashboard** with 8-tab HTML visualization
- **Context audit** with per-skill and per-MCP-server token breakdown
- **Quality scoring** with 5 signals and model-aware context windows (Claude 1M, GPT-5 400K, Gemini 2M)
- **Manage tab** to toggle skills and MCP servers on/off (accumulated clipboard commands)
- **Smart Compaction v2** preserves decisions, errors, and user instructions during compaction
- **Combined checkpoint policy** fires on fill bands `20/35/50/65/80`, quality drops `80/70/50/40`, and milestones like `pre-fanout` / `edit-batch`
- **Local checkpoint telemetry** is opt-in with `TOKEN_OPTIMIZER_CHECKPOINT_TELEMETRY=1` and shows whether the new policy is firing without sending any external analytics
- **Drift detection** snapshots config and diffs to catch creep

## CLI

```sh
npx token-optimizer detect                # Is OpenClaw installed?
npx token-optimizer scan --days 30        # Scan sessions, show usage
npx token-optimizer audit --days 30       # Detect waste, show $ savings
npx token-optimizer audit --json          # JSON output for agents
npx token-optimizer dashboard             # Generate HTML dashboard, open in browser
npx token-optimizer context               # Show context overhead breakdown
npx token-optimizer context --json        # Context audit as JSON
npx token-optimizer quality               # Show quality score (0-100)
npx token-optimizer drift                 # Check for config drift
npx token-optimizer drift --snapshot      # Capture current config snapshot
npx token-optimizer doctor --json         # Check checkpoint health, recent events, last trigger
TOKEN_OPTIMIZER_CHECKPOINT_TELEMETRY=1 npx token-optimizer checkpoint-stats
```

## Dashboard

The interactive dashboard has 8 tabs:

| Tab | What It Shows |
|-----|--------------|
| Overview | Stat cards (runs, cost, quality score, savings), agent cards, context overhead bar |
| Context | Per-component token breakdown, individual skill bars, MCP server list, recommendations |
| Quality | 5-signal quality score (0-100) with per-signal breakdown and recommendations |
| Waste | Waste cards with severity, confidence, fix snippets with Copy Fix button |
| Agents | Per-agent cost, model mix stacked bars (multi-model only), top agents table |
| Sessions | Individual session history grouped by date with outcome, cost, and model |
| Daily | Daily cost/token and run count charts with Y-axis labels and custom tooltips |
| Manage | Toggle skills and MCP servers on/off. Changes accumulate, copy all at once |

Dashboard auto-regenerates on session end. Open manually with `npx token-optimizer dashboard`.

## Waste Patterns Detected

| Pattern | What It Means | Typical Savings |
|---------|--------------|-----------------|
| Heartbeat Model Waste | Cron agent using opus/sonnet instead of haiku | $2-50/month |
| Heartbeat Over-Frequency | Checking more often than every 5 minutes | $1-10/month |
| Empty Heartbeat Runs | Loading 50K+ tokens, finding nothing to do | $2-30/month |
| Stale Cron Config | Hooks pointing to non-existent paths | Varies |
| Session History Bloat | 500K+ tokens without compaction | 40% of bloated input |
| Loop Detection | 20+ messages with near-zero output | $1-20/month |
| Abandoned Sessions | Started, loaded context, then left | $0.20-5/month |

## Quality Signals

| Signal | Weight | What It Measures |
|--------|--------|-----------------|
| Context Fill | 25% | Token usage relative to model context window (per-model: Claude 1M, GPT-5 400K, Gemini 2M) |
| Session Length Risk | 20% | Message count vs compaction threshold |
| Model Routing | 20% | Expensive models used for cheap tasks |
| Empty Run Ratio | 20% | Runs that load context but produce nothing |
| Outcome Health | 15% | Success vs abandoned/empty/failure ratio |

## Context Audit

Scans every component OpenClaw injects into context:

| Component | Source | Optimizable |
|-----------|--------|-------------|
| Core system prompt | Built-in | No |
| SOUL.md | Personality/instructions | Yes |
| MEMORY.md | Persistent memory | Yes |
| AGENTS.md | Agent definitions | Yes |
| TOOLS.md | MCP tool definitions | Yes |
| Skills | Individual SKILL.md files | Yes (archive unused) |
| Agent configs | Per-agent config.json | Yes |
| Cron configs | cron/*.json | Yes |
| MCP Servers | config.json mcpServers | Yes (disable unused) |

## Smart Compaction v2

Hooks into `session:compact:before` and `session:compact:after`. Instead of saving the last 20 raw messages (v1), v2 extracts:

- **User instructions**: "always", "never", "make sure" directives
- **Decisions**: "decided to", "going with", "switching to"
- **Errors**: stack traces, error messages, failure patterns
- **File changes**: write, edit, create operations

Result: more relevant context in fewer tokens after compaction.

Checkpointing is no longer just “wait until the window is almost full.” The runtime now captures:

- Fill bands at `20%`, `35%`, `50%`, `65%`, and `80%`
- First quality drops below `80`, `70`, `50`, and `40`
- Milestones before agent fan-out and after a meaningful edit batch
- Optional local telemetry in `checkpoint-stats` so you can see whether the policy is firing in real sessions

## Drift Detection

```sh
npx token-optimizer drift --snapshot      # Save current state
# ... time passes, skills added, configs changed ...
npx token-optimizer drift                 # See what changed
```

Tracks: skill count, agent count, SOUL.md/MEMORY.md size changes, model config changes, cron configs.

## Pricing

Covers 30+ models with verified March 2026 rates: Claude (Opus/Sonnet/Haiku), GPT-5 family, GPT-4.1 family, o3/o4, Gemini 2.0-3.1, DeepSeek, Qwen, Mistral, Grok, and more. User-configured pricing overrides via openclaw.json.

## License

AGPL-3.0-only
