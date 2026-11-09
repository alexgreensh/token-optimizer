# Fleet Systems: Data Format Specifications

Reference file for Fleet Auditor. Loaded on demand for adapter development.

---

## Contents

- [Claude Code](#claude-code)
- [OpenClaw](#openclaw)
- [NanoClaw](#nanoclaw)
- [Hermes](#hermes)
- [OpenCode](#opencode)
- [IronClaw](#ironclaw)
- [Cursor](#cursor)

---

## Claude Code

**Data Location**: `~/.claude/projects/`
**Format**: JSONL (one JSON object per line)
**Structure**: Each project gets a directory named with a path-encoded slug (e.g., `-Users-alex-myproject/`). Session files are `{uuid}.jsonl`. Subagent files live in `{uuid}/subagents/{sub-uuid}.jsonl`.

### JSONL Record Types
- `type: "user"` - User messages with `message.content` (string or array of blocks)
- `type: "assistant"` - Assistant messages with `message.content` (array of tool_use and text blocks), `message.usage` (token data), `message.model`
- `type: "result"` - Tool results

### Token Fields (in `message.usage`)
```json
{
  "input_tokens": 12345,
  "output_tokens": 678,
  "cache_read_input_tokens": 9000,
  "cache_creation_input_tokens": 3000
}
```

### Key Extraction Points
- **Version**: First record with `version` field
- **Slug**: First record with `slug` field
- **Timestamp**: `timestamp` field (ISO-8601 with Z suffix)
- **Model**: `message.model` in assistant records
- **Tools**: `tool_use` blocks in `message.content` array

---

## OpenClaw

**Data Location**: `~/.openclaw/agents/` (also `~/.clawdbot/`, `~/.moltbot/`)
**Format**: JSON index + JSONL transcripts

### Key Files
- `sessions.json` - Index of all sessions with metadata
- `agents/{agent-name}/sessions/{session-id}.jsonl` - Individual session transcripts
- `cron/` - Heartbeat/cron configuration and logs
- `config.json` - Global config including model settings, pricing overrides

### Token Fields
```json
{
  "inputTokens": 12345,
  "outputTokens": 678,
  "totalTokens": 13023
}
```

Note: OpenClaw does NOT expose cache read/write breakdown. Use `inputTokens` for total input.

---

## NanoClaw

**Data Location**: `~/.nanoclaw/` or container volume mounts
**Format**: SQLite (`messages` table) + Claude Agent SDK response objects
**Built on**: Anthropic Claude Agent SDK

### Token Fields (from SDK response)
```python
response.usage.input_tokens
response.usage.output_tokens
response.usage.cache_creation_input_tokens
response.usage.cache_read_input_tokens
```

### SQLite Schema
```sql
-- messages table stores conversations
-- Token data in SDK response metadata, not always in DB
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    role TEXT,
    content TEXT,
    created_at TIMESTAMP
);
```

### Container Considerations
- Data may be inside Docker/container volumes
- Check for exported/mounted data directories
- Container isolation means direct DB access may not be possible

---

## Hermes

**Data Location**: `~/.hermes/`
**Format**: SQLite (`state.db`) + JSONL session logs

### Key Files
- `state.db` - Primary state database
- `sessions/{date}/{session-id}.jsonl` - Session transcripts

### Token Fields
```json
{
  "tokens": {
    "input": 12345,
    "output": 678
  }
}
```

---

## OpenCode

**Data Location**: `~/.local/share/opencode/` or `$OPENCODE_DATA_DIR`
**Format**: JSON per-message, or SQLite on OpenCode builds that use `storage.db`

### Token Fields
```json
{
  "usage": {
    "input": 12345,
    "output": 678
  },
  "cacheRead": 9000,
  "cacheWrite": 3000
}
```

### File Structure
- `sessions/{session-id}/` - Per-session directories
- `sessions/{session-id}/messages.json` - Array of messages
- `storage.db` - SQLite database, present on builds that replace the JSON files

---

## IronClaw

**Data Location**: `~/.ironclaw/`
**Format**: PostgreSQL or libSQL (requires connection config)

### Token Fields
```json
{
  "max_tokens": 4096,
  "total_tokens_used": 12345
}
```

### Access Pattern
- Requires database connection string from config
- Detect only today, no scan
- Scanning would need exported data files or a direct DB connection

---

## Cursor

**Data Location**: `~/.cursor/token-optimizer/` plus `~/.cursor/_backups/token-optimizer/` (override: `TOKEN_OPTIMIZER_CURSOR_HOME`; snapshot override: `TOKEN_OPTIMIZER_SNAPSHOT_DIR`)
**Format**: JSON hook tallies + SQLite trends rollup (both written by Token Optimizer's own Cursor adapter — the collector adds no new write paths)

### Key Files
- `token-optimizer/sessions/{conversation-id}.json` — hook tally, the authoritative session record (turns, tool calls, models, compactions, cwd, timestamps). Written by `cursor_hook_bridge.py`'s `_update_tally`.
- `_backups/token-optimizer/trends.db` — rollup output (`measure.py cursor-rollup`); `session_log` rows with `platform='cursor'` carry the resolved token figures. Read-only for consumers (`mode=ro`).

### Tally Fields (JSON)
```json
{
  "conversation_id": "...",
  "first_ts": 1762684800.0,
  "updated_at": 1762686600.0,
  "turns": 14,
  "tool_calls": 22,
  "models": {"gpt-5.1": 9},
  "tool_names": {"Shell": 15},
  "compactions": [],
  "cursor_version": "2026.11.05-abc1234",
  "cwd": "/work/project",
  "final": true,
  "end_reason": "sessionEnd"
}
```

### Token Fields
The tally carries **no token totals**. Tokens come from the rollup's `session_log` (`input_tokens`, `output_tokens`), resolved by the rollup from `state.vscdb` bubble `tokenCount` (best-effort per Cursor staff) or a chars-over-four transcript estimate. Cache read/write breakdown does not exist for Cursor — leave it 0, do not estimate.

A tallied session with no rollup row yet has **unknown** token figures: collect it with `token_source = "cursor_tally_only"` and zero tokens, and say the figures are unknown (run `measure.py cursor-rollup`). A silent zero is a defect (#149 class).

### Cost
`cost_source` is `cursor_no_cost_data`: Cursor exposes no local billing data and Token Optimizer never derives dollar figures for Cursor — no pricing-table lookup, no $0.00-as-free. Fleet rows carry `cost_usd = 0` with an explicit "no cost data" surface; waste columns show token deltas only.
