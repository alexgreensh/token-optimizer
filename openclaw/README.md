# Token Optimizer for OpenClaw

Find the ghost tokens. Audit your OpenClaw setup, see where 25-38% of your context goes, fix it.

## Install

```sh
openclaw plugins install token-optimizer-openclaw
```

Or from source:

```sh
cd openclaw && npm install && npm run build
openclaw plugins install ./openclaw
```

## What It Does

- **Scans** all agent sessions for token usage and cost
- **Detects** 7 waste patterns (heartbeat model waste, empty runs, stuck loops, bloated sessions, stale configs, over-frequency, abandoned sessions)
- **Reports** monthly $ savings with actionable fix snippets
- **Dashboard** with 6-tab HTML visualization (Overview, Context, Quality, Waste, Agents, Daily)
- **Context audit** showing per-component token overhead (SOUL.md, skills, tools, agents)
- **Quality scoring** with 5 signals adapted for OpenClaw
- **Smart Compaction v2** with intelligent extraction (decisions, errors, instructions)
- **Drift detection** to catch config creep over time

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
```

## Dashboard

The interactive dashboard shows 6 tabs:

| Tab | What It Shows |
|-----|--------------|
| Overview | Stat cards (runs, cost, quality score, savings), agent cards, waste summary |
| Context | Per-component token breakdown (SOUL.md, skills, tools, memory, agents) |
| Quality | 5-signal quality score with per-signal breakdown and recommendations |
| Waste | Waste cards with severity, confidence, fix snippets with copy button |
| Agents | Per-agent cost, model mix stacked bars, top agents table |
| Daily | Daily cost/token and run count charts with Y-axis labels |

Dashboard auto-regenerates on session end. Open manually: `npx token-optimizer dashboard`.

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
| Context Fill | 25% | Token usage relative to 200K context window |
| Session Length Risk | 20% | Message count vs compaction threshold |
| Model Routing | 20% | Expensive models used for cheap tasks |
| Empty Run Ratio | 20% | Runs that load context but produce nothing |
| Outcome Health | 15% | Success vs abandoned/empty/failure ratio |

## Smart Compaction

The plugin hooks into `session:compact:before` and `session:compact:after` to save and restore session state. v2 uses intelligent extraction to preserve decisions, errors, file changes, and user instructions in fewer tokens than v1's raw message dump.

## Drift Detection

Snapshot your config with `npx token-optimizer drift --snapshot`. Later, run `npx token-optimizer drift` to see what changed (skills added/removed, file sizes, model config changes).

## License

AGPL-3.0-only
