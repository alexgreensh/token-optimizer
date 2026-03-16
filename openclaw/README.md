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
- **Protects** session state during compaction (Smart Compaction hooks)

## CLI

```sh
npx token-optimizer detect               # Is OpenClaw installed?
npx token-optimizer scan --days 30       # Scan sessions, show usage
npx token-optimizer audit --days 30      # Detect waste, show $ savings
npx token-optimizer audit --json         # JSON output for agents
```

## Skill

Inside OpenClaw, run `/token-optimizer` for a guided audit with coaching.

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

## Smart Compaction

The plugin hooks into `session:compact:before` and `session:compact:after` to save and restore session state. Checkpoints are stored as markdown in `~/.openclaw/token-optimizer/checkpoints/`.

## License

AGPL-3.0-only
