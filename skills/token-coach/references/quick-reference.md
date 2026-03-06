# Quick Reference: Hard Numbers for Token Coach

Reference file for Token Coach. The numbers the coach cites. Updated from research data (March 2026).

---

## Baseline Overhead (Fresh Session)

| Component | Tokens | % of 200K |
|-----------|--------|-----------|
| System prompt | ~3,000 | 1.5% |
| Built-in tools (18+) | ~12,000-15,000 | 6-7.5% |
| Autocompact buffer | ~33,000-45,000 | 16.5-22.5% |
| **Total fixed floor** | **~48,000-63,000** | **24-31.5%** |

Usable context before any user config: ~137,000-152,000 tokens.

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

Tool Search (default since Jan 2026) reduced total MCP overhead by 85-96%.

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
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | Max output tokens (higher = larger autocompact buffer) | 16,384 |
| `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` | Override autocompact trigger percentage | ~80% |
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

## Community Benchmarks

| Metric | Value | Source |
|--------|-------|--------|
| Autocompact buffer | 33K-45K tokens | GitHub issues, claudefa.st |
| Tool Search reduction | 85-96% | Anthropic engineering blog |
| johnlindquist compression | 7,584 to 3,434 tokens (54%) | Community report |
| Boris Cherny workflow | 259 PRs in 30 days | Progressive CLAUDE.md, plan mode first |
| SFEIR three-optimization result | 40% total reduction | Plan mode + PreCompact hooks + multi-sessions |
| ccusage GitHub stars | 11K+ | Community standard for token tracking |

## Competitive Context

| Tool | What It Does | Our Edge |
|------|-------------|----------|
| ccusage (11K stars) | Historical token tracking from JSONL logs | We measure config overhead, they measure usage |
| /context (built-in) | Shows current context fill level | Hides deferred MCP overhead. We show the real picture. |
| GrepAI | Semantic search replacing brute-force grep | Different problem (search efficiency vs config optimization) |
| cozempic | Removes "garbage" Anthropic injects | Risky (modifies system prompt). We optimize your config. |
| token-optimizer-mcp | Caching and compression | Complementary. They cache, we audit and fix. |

## Community Pain Points (Feb-March 2026)

1. No per-request token visibility (GitHub #29600, #30814)
2. Compaction triggers too often / unexpectedly (buffer varies 33K-45K by version)
3. Context fills faster than expected (hidden MCP overhead)
4. MCP overhead invisible until session degrades (/context hides deferred overhead)
5. Auto-memory contributing to bloat (v2.1.53-59 regression confirmed by Anthropic)
6. Plugin cache stale versions accumulating (18+ GitHub issues)
7. Per-turn token regression in v2.1.x (GitHub #24243)
8. Agent Teams burn 7x tokens with unclear ROI
