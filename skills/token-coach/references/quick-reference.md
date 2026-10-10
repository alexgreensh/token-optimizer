# Quick Reference: Hard Numbers for Token Coach

Reference file for Token Coach. The numbers the coach cites.

---

## Contents

- [Baseline Overhead (Fresh Session)](#baseline-overhead-fresh-session)
- [Cache and Compaction Controls](#cache-and-compaction-controls)
- [User Config Overhead (Typical Power User)](#user-config-overhead-typical-power-user)
- [Context Quality Degradation](#context-quality-degradation)
- [MCP Tool Costs (Real Examples)](#mcp-tool-costs-real-examples)
- [Token Costs Per Component](#token-costs-per-component)
- [Environment Variables](#environment-variables)
- [Subagent Costs](#subagent-costs)
- [Cache-Expiry Waste — per provider](#cache-expiry-waste--per-provider)

---

## Baseline Overhead (Fresh Session)

Percentages below assume a 1M-token window, the default on current Claude models (Haiku 5.5, Sonnet 5+, Opus 4.7+). On a 200K window the same token counts are 5x the percentage.

| Component | Tokens | % of a 1M window |
|-----------|--------|------------------|
| System prompt | ~3,000 | 0.3% |
| Built-in tools (18+) | ~12,000-15,000 | 1.2-1.5% |
| **Total fixed floor** | **~15,000-18,000** | **1.5-1.8%** |

With auto-compact enabled (the default), Claude Code reserves headroom inside the model's compact window; the reserved size is not documented and varies by build, so treat it as overhead you cannot spend. On 1M models compaction fires at about 967K tokens by default; on 200K models it fires near the context limit.

## Cache and Compaction Controls

What the user can set, from the official Claude Code docs. Cache writes: a 5-minute cache costs 1.25x base input to write, a 1-hour cache 2x; cache reads cost 0.1x.

| Control | What it does | Default |
|----------|-------------|---------|
| `promptCacheTtl` (setting) | Main-conversation cache TTL: "5m" or "1h" | unset (1h on subscription billing, 5m on usage credits/API) |
| `CLAUDE_CODE_PROMPT_CACHE_TTL` (env) | Same as `promptCacheTtl`; env outranks the setting | unset |
| `subagentPromptCacheTtl` (setting) | Subagent cache TTL: "5m" or "1h" (v2.1.243+) | unset (subagents get 5m even on a subscription) |
| `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL` (env) | Same as `subagentPromptCacheTtl`; env outranks the setting | unset |
| `experimental.cacheTtl` (agent frontmatter, v2.1.248+) | Per-subagent cache TTL, inside the `experimental:` map | unset |
| `ENABLE_PROMPT_CACHING_1H=1` (env) | Blanket 1-hour cache | unset |
| `FORCE_PROMPT_CACHING_5M` (env) | Force 5-minute caching; outranks everything above | unset |
| `/autocompact <n>` | Compact window, 100K-1M, saved per model | tuned for the model (~967K on 1M models) |
| `/autocompact auto` | Restore the tuned window | - |
| `autoCompactWindow` (setting) | Compact window, 100000-1000000, capped at the model's limit | unset (tuned per model) |
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (env) | Same range as `autoCompactWindow`; env outranks the setting | unset |

A plugin's own settings.json cannot set the cache TTLs (only `agent` and `subagentStatusLine` take effect from plugin settings).

## User Config Overhead (Typical Power User)

| Component | Tokens | Per-Item Cost |
|-----------|--------|---------------|
| Skills (50 installed) | ~5,000 | ~100/skill |
| Commands (30 installed) | ~1,500 | ~50/command |
| MCP tools (100 deferred) | ~1,500 | ~15/tool |
| MCP server instructions (10 servers) | ~500-1,000 | ~50-100/server |
| CLAUDE.md (global) | ~800-2,000 | Per line |
| MEMORY.md | ~600-1,400 | Per line |
| Rules (5 unscoped) | ~500 | Variable |
| @imports | Variable | Full file cost |

## Context Quality Degradation

| Fill Level | Quality | Recommendation |
|------------|---------|----------------|
| 0-30% | Peak performance | Work freely |
| 30-50% | Good quality | Monitor context |
| 50-70% | Minor degradation | Run /compact soon |
| 70-85% | Noticeable quality loss | Run /compact NOW |
| 85%+ | Hallucinations, corner-cutting | /clear or new session |

## MCP Tool Costs (Real Examples)

| MCP Server | Tools | Tokens (eager) | Tokens (deferred) |
|------------|-------|----------------|-------------------|
| GitHub | 35 | ~26,000 | ~525 |
| Slack | 11 | ~21,000 | ~165 |
| Jira | ~20 | ~17,000 | ~300 |
| Docker | 135 | ~125,000 | ~2,025 |
| Chrome automation | ~30 | ~31,700 | ~450 |

Tool Search (on by default) reduced total MCP overhead by 85-96%.

## Token Costs Per Component

| What | Always-Loaded Cost | On-Demand Cost |
|------|-------------------|----------------|
| Skill (installed) | ~100 tokens (frontmatter) | 2K-5K (full SKILL.md on invoke) |
| Command | ~50 tokens (frontmatter) | Full file on invoke |
| MCP tool (deferred) | ~15 tokens (name only) | Full schema on use |
| MCP tool (eager) | ~300-850 tokens (full schema) | N/A |
| MCP server instruction | ~50-100 tokens | N/A |
| CLAUDE.md line | ~15 tokens | N/A |
| @import file | Full file tokens | N/A |
| Rule (unscoped) | Full file tokens | N/A |
| Rule (path-scoped) | 0 (until path match) | Full file when matched |

## Environment Variables

| Variable | Effect | Default |
|----------|--------|---------|
| `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1` | Remove git workflow instructions (~2K tokens) | Enabled |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` | Disable auto memory creation/loading | Enabled |
| `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` | Disable background tasks | Enabled |
| `ENABLE_CLAUDEAI_MCP_SERVERS=false` | Opt out of claude.ai cloud-synced MCP servers | Enabled |
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | Max output tokens per response | 16,384 |
| `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` | Auto-removed if found (inverted semantics cause premature compaction) | not set (~98%) |
| `includeGitInstructions: false` (setting) | Same as DISABLE_GIT env var, in settings.json | true |
| `effortLevel` (setting) | "high" maximizes quality + cost; "medium" saves 15-25% output tokens | auto |

## Subagent Costs

| Factor | Cost |
|--------|------|
| Native agent overhead (v1.0.60+) | ~13K tokens per agent |
| Config inheritance per agent | Same as main session startup |
| 5 agents x 15K config | 75K tokens just for setup |
| Skill assigned to subagent | FULL SKILL.md at startup (not progressive) |
| Agent Teams vs single agent | ~7x token usage (Anthropic docs) |

## Cache-Expiry Waste — per provider

Cache economics are model-AGNOSTIC: each provider has its own cache profile, so the detector resolves a session's model to a profile, not to Anthropic semantics. Two waste shapes: explicit-TTL re-WRITE (Anthropic) and automatic-discount COLLAPSE (OpenAI/Codex, Gemini, DeepSeek).

| Provider / model family | Cache kind | Cached read | Effective TTL | User TTL knob |
|---|---|---|---|---|
| Anthropic API/SDK | explicit_ttl | 0.1x input | 5 min default | yes (`ttl:"1h"`, 2x write once) |
| Claude Code | explicit_ttl | 0.1x input | 1h on subscription billing, 5m on usage credits/API | yes (see the Cache and Compaction Controls table above) |
| OpenAI / Codex | automatic_discount | 0.1x input | ~5-10 min (max ~1h) | policy only (`prompt_cache_retention="24h"`) |
| Gemini 2.5+ | explicit_storage | ~0.1x input | implicit auto | yes (`cached_content` ttl, default 1h, +storage/hr) |
| DeepSeek | automatic_discount | 0.1x input (1/10) | hours-to-days | no |
| unknown / other | none | n/a | n/a | n/a (no cache economics) |

Claude Code's main conversation requests a 1-hour cache on subscription billing; on usage credits or an API key the default is 5 minutes until a setting above raises it. Empirically the prefix survives sub-hour pauses, so the Claude Code detector counts only pauses LONGER than an hour. Subagents default to a 5-minute cache even on a subscription; `subagentPromptCacheTtl` (or the per-agent `experimental.cacheTtl`) can raise that to 1 hour. Raw Anthropic API/SDK/harness sessions (e.g. Hermes → Anthropic) keep the 5-minute default and the 1h-`cache_control` counterfactual.

Detection: explicit_ttl = gap > effective TTL (Claude Code main conversation: 1 hour; API/SDK: 5min) AND next-turn cache_creation >= 50% of prior cached prefix. automatic_discount/explicit_storage = prior cached ratio >= 0.40, gap > TTL, next ratio < 0.10 with comparable prompt → lost cached tokens re-billed at full input vs cached rate. none = honest skip (counted, never waste).

Verified remedies (per profile, exactly what the provider offers):
- Claude Code: resume inside the cache window or batch related work, so a long pause never strands the prefix. Token Optimizer can also keep it warm automatically (opt-in, API billing only): `keepwarm-enable` (records consent + installs the macOS scheduler; verify with `keepwarm-scheduler status` / `keepwarm-tick --dry-run`). It pings the cache before expiry at ~0.1x of the prefix (vs the 1.25-2x re-write), max 2 pings per pause unless promoted, with a tripwire that auto-disables if pings stop paying for themselves. Off by default; subscription auth stays off (pings would burn quota without saving dollars). To activate on a subscription/off-billing or platform-gap machine: set `ANTHROPIC_API_KEY` and run `keepwarm-enable` (on Linux/Windows, wire `keepwarm-tick` to your own cron/timer until the scheduler ships).
- Anthropic API/SDK/agent harness: `cache_control {"type":"ephemeral","ttl":"1h"}` on stable prefixes.
- OpenAI/Codex: keep prefix exact-match (>=1024 tok), resume within window; `prompt_cache_retention="24h"` for long-lived prefixes.
- Gemini: explicit `cached_content` with user `ttl` (per-hour storage billed).
- DeepSeek: automatic; keep prefixes stable while the on-disk cache is warm.

Coverage gaps (not measurable, rendered explicitly): Hermes (per-session aggregates only, cache_read unreliable), OpenClaw/OpenCode (TS engines, no Python per-turn read path), Copilot (credits-billed, no per-turn cache detail).

Surface: `measure.py cache-report [--days N] [--json]` — per-provider breakdown + coverage gaps. OPPORTUNITY-tier (observed waste, potential recovery); never counts toward realized savings.
