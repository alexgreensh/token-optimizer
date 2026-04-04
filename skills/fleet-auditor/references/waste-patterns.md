# Waste Patterns: Detection Algorithms and Thresholds

Reference file for Fleet Auditor. Loaded on demand for detector development.

---

## Tier 1: Static Config Analysis

These detectors run against configuration files and don't need session data.

### 1. Heartbeat Model Waste
**Signal**: Cron/heartbeat agent configured with opus or sonnet
**Threshold**: Any heartbeat using non-haiku model with >$0.10/month cost
**False positive check**: Some heartbeats legitimately need reasoning (e.g., triage bots)
**Confidence**: 0.9 (high, easy to verify from config)

### 2. Heartbeat Over-Frequency
**Signal**: 3+ consecutive heartbeat runs with <5 min interval
**Threshold**: Average interval < 300 seconds across 3+ runs
**False positive check**: Burst patterns (3 quick then long gap) are OK
**Confidence**: 0.7 (intervals can be irregular)

### 3. Skill Bloat
**Signal**: >10 skills loaded per agent
**Threshold**: 10+ skills = medium, 20+ = high
**Cost model**: ~100 tokens/skill/API call x 20 calls/session x 30 sessions/month
**False positive check**: Power users with 15 skills they all actively use
**Confidence**: 0.8

### 4. Tool Definition Bloat
**Signal**: MCP tool definitions consuming >15% of 200K context
**Threshold**: Estimated tool tokens > 30K
**Cost model**: Rough (150 tokens/eager tool, 15/deferred, ~10 tools/server)
**False positive check**: All servers could be actively used
**Confidence**: 0.6 (rough estimate)

### 5. Memory/Config Overhead
**Signal**: CLAUDE.md or MEMORY.md exceeding 5,000 tokens
**Threshold**: >5K = medium, >10K = high
**Cost model**: Tokens x 20 calls/session x 30 sessions/month
**False positive check**: Large CLAUDE.md might be legitimately needed
**Confidence**: 0.9

### 6. Stale Cron Configurations
**Signal**: Cron/hook commands referencing non-existent paths
**Threshold**: Any dead path reference
**False positive check**: Paths with variables ($HOME, etc.) that we can't resolve
**Confidence**: 0.5 (can't always determine validity)

---

## Tier 2: Session Log Analysis

These detectors require parsed session data (AgentRun objects from fleet.db).

### 7. Empty Heartbeat Runs (THE #1 WASTE PATTERN)
**Signal**: Input > 5K tokens, output < 100 tokens, messages <= 4
**Confirmation**: Input > 10K OR outcome == "empty"
**Threshold**: 2+ confirmed empty runs in the window
**Cost model**: Actual cost from token data
**False positive check**: Legitimate "nothing to do" checks with small context
**Confidence**: 0.85

### 8. Session History Bloat
**Signal**: Sessions with 30+ messages and 500K+ input tokens
**Interpretation**: Context growing monotonically without compaction
**Savings estimate**: ~40% of bloated input (conservative compaction savings)
**False positive check**: Some sessions legitimately process large codebases
**Confidence**: 0.6

### 9. Loop Detection
**Signal**: High input:output ratio (>20:1) in sessions with 10+ messages
**Interpretation**: Agent reading lots of context but producing little output = stuck
**Threshold**: 2+ suspected loop sessions, >$0.50/month waste
**False positive check**: Exclude "empty" outcome runs (caught by detector 7)
**Confidence**: 0.5 (heuristic, needs JSONL deep-parse for confirmation)

### 10. Abandoned Sessions
**Signal**: 1-2 messages, >3K input tokens, manual run type
**Interpretation**: User started a session, loaded full context, then left
**Threshold**: 3+ abandoned sessions, >$0.20/month waste
**False positive check**: Quick "check something" sessions are normal
**Confidence**: 0.7

---

## Phase 2+ Detectors (Not Yet Implemented)

### 11. Retry Storms
**Signal**: Same tool called 3+ times consecutively with similar inputs
**Implementation**: Requires JSONL deep-parse for tool call sequences

### 12. Model Downgrade Opportunities
**Signal**: Sessions using opus/sonnet with low complexity indicators
**Complexity indicators**: Short messages, few tool calls, simple patterns
**Implementation**: Requires session content analysis

---

## Severity Levels

| Level | Color | Meaning | Threshold |
|-------|-------|---------|-----------|
| critical | Red | Immediate action needed | >$10/month waste |
| high | Orange | Should fix soon | >$2/month waste |
| medium | Cyan | Worth addressing | >$0.50/month waste |
| low | Gray | Nice to have | <$0.50/month waste |

## Confidence Levels

| Range | Meaning | Display |
|-------|---------|---------|
| 0.8-1.0 | High confidence, likely accurate | Show prominently |
| 0.5-0.79 | Medium, heuristic-based | Show with caveat |
| 0.3-0.49 | Low, rough estimate | Show as "possible" |
| <0.3 | Too uncertain | Suppress from report |
