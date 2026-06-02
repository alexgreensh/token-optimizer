# Fleet Systems: Data Format Specifications

Reference file for Fleet Auditor. Loaded on demand for adapter development.

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
**Format**: JSON per-message + SQLite (v1.2+)

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
- `storage.db` - SQLite database (v1.2+, replaces JSON)

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
- Phase 1: detect only, no scan
- Phase 2+: support exported data files or direct DB connection
