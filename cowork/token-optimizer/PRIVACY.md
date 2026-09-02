# Privacy Policy

**Token Optimizer** is a source-available developer tool that runs entirely on your local machine. It optimizes AI coding assistant context windows across Claude Code, Codex, OpenClaw, OpenCode, Hermes, and Google Antigravity.

## No External Data Transmission

- **No telemetry**: No usage data, analytics, or metrics are sent anywhere.
- **No runtime network calls**: Zero outbound network requests during normal operation. The only network activity at runtime is a loopback-only (127.0.0.1) dashboard server for local visualization. Install and update checks contact GitHub (`api.github.com`) to verify release integrity; no product data is sent.
- **No third-party services**: No external APIs, tracking pixels, or data processors.
- **No accounts required**: No sign-up, login, or registration.

## What Token Optimizer Reads

To perform its analysis, Token Optimizer reads files that already exist on your machine:

- Host platform session transcripts (JSONL files under `~/.claude/projects/`)
- `~/.claude/settings.json` and project-level `.claude/settings.json`
- `~/.claude/CLAUDE.md`, project-level `CLAUDE.md`, and `~/.claude/MEMORY.md`
- Skill and command directories under `~/.claude/`
- MCP server configurations
- Google Antigravity's `~/.gemini/{antigravity-cli,antigravity,antigravity-ide}/`
  conversation stores: only `gen_metadata.data`, `steps.step_type` and step
  timestamps, and a fixed set of summaries columns (`title`, `workspace_uris`,
  `killed`, `not_fully_idle`, `last_modified_time`, `nesting_depth`). Prompt
  text (`history.jsonl` `display`, summaries `preview`), tool arguments in step
  metadata, and `trajectory_metadata_blob` are never read or stored.

These files are read locally and never transmitted.

## What Token Optimizer Stores

Token Optimizer writes several local data stores. All are stored with restrictive file permissions (`0o700` directories, `0o600` files).

### Session Metrics Database (trends.db)

SQLite database containing per-session aggregates: token counts, model usage, cost estimates, session UUIDs, JSONL file paths. Used for the dashboard and savings tracking.

- **Path:** `<plugin-data>/data/trends.db`
- **Retention:** Configurable via `TOKEN_OPTIMIZER_TRENDS_RETENTION_DAYS` (default: unlimited)
- **Sensitive content:** Contains JSONL file paths which embed the local username as a path component

### Per-Session File Cache (session stores)

SQLite databases with file read records, content hashes, token estimates, and cached file content (up to 50KB per file). **Cached content is credential-redacted before storage** using 23 pattern types (AWS keys, API tokens, URL query/fragment auth params, etc.).

- **Path:** `<plugin-data>/data/session-store/<session-id>.db`
- **Retention:** Auto-deleted after 48 hours
- **Sensitive content:** Cached source code content (credential-redacted). Activity log with tool call summaries.

### Checkpoints

Markdown files containing truncated conversation context for session continuity. Each checkpoint includes up to 300 characters of the last user message and last assistant message, extracted file paths, decision text, error snippets (up to 150 characters), and todo items.

- **Path:** `~/.claude/token-optimizer/checkpoints/`
- **Retention:** Configurable via `TOKEN_OPTIMIZER_CHECKPOINT_RETENTION_DAYS` (default: 7 days) and `TOKEN_OPTIMIZER_CHECKPOINT_RETENTION_MAX` (default: 50 files)
- **Sensitive content:** Truncated conversation snippets may contain PII or sensitive information the user typed into the coding assistant

### Tool Archives

JSON files containing the full output of large tool calls (over 4KB), used for retrieval during the same session. **Content is credential-redacted before storage.**

- **Path:** `<plugin-data>/data/tool-archive/<session-id>/`
- **Retention:** Configurable via `TOKEN_OPTIMIZER_ARCHIVE_RETENTION_HOURS` (default: 24 hours), with aggregate caps of 1,000 files and 100 MiB
- **Sensitive content:** Tool output (credential-redacted) which may include file contents, command output, or API responses

### Quality Cache

JSON files with per-session quality score snapshots (6-signal metric).

- **Path:** `~/.claude/token-optimizer/quality-cache-*.json`
- **Retention:** Configurable via `TOKEN_OPTIMIZER_QUALITY_CACHE_RETENTION_DAYS` (default: 7 days)
- **Sensitive content:** None

### Configuration and Auxiliary Files

- **Config:** `~/.claude/token-optimizer/config.json` (feature flags, consent status, timestamps)
- **Checkpoint event log:** `~/.claude/token-optimizer/checkpoint-events.jsonl` (rotated at 1000 entries)
- **Live fill cache:** `~/.claude/token-optimizer/live-fill.json`
- **Dashboard:** `<plugin-data>/data/dashboard.html` (generated visualization)
- **Daemon token:** `<plugin-data>/data/daemon-token` (32-byte random secret, 0600 permissions)
- **Daemon logs:** `<plugin-data>/data/logs/` (stdout/stderr from dashboard server)

### Antigravity adapter data

The Google Antigravity adapter keeps its own minimal state under
`~/.gemini/token-optimizer/`:

- **Consent record:** `config.json` with the `antigravity_consent` flag (R20)
- **Continuity restore:** `restore-context.md`, a capped summary of the most
  recent session (model, token totals, and — title/workspace permitting — a
  200-character topic and workspace path). Titles and workspace paths originate
  from Antigravity's own summaries and are filtered to printable characters
  before this file is written.

The installed plugin payload lives at `~/.gemini/config/plugins/token-optimizer/`
and is removed by `antigravity-uninstall`.

## Credential Handling

Token Optimizer scans for 23 credential patterns (AWS keys, API tokens, GitHub PATs, database URIs, JWTs, PEM keys, URL query/fragment auth params, and more) and replaces them with `[CREDENTIAL REDACTED: <type>]` before writing to the session store and tool archive. This redaction is one-way and permanent in stored content.

**Known tradeoff:** Credential redaction in the read cache means delta reads against files containing credentials will produce non-empty diffs on every re-read (the stored version has the redacted placeholder, the live file has the actual credential). This is a deliberate security-over-efficiency tradeoff.

Bash compression output preserves credential-containing lines verbatim (not redacted) to ensure compressed output returned to the coding assistant doesn't mangle secrets.

## Consent

On first activation, Token Optimizer shows a data notice describing what is stored locally and requires acknowledgment before data collection begins. Hooks exit early (no data collection, no blocking tool calls) until consent is granted.

- Check consent status: `python3 measure.py consent --show`
- Reset consent: `python3 measure.py consent --reset`
- Grant consent: `python3 measure.py consent --grant`

Existing users who already saw the v5 welcome notice are automatically considered consented (backward compatible).

## Transcript Preservation

Token Optimizer sets `cleanupPeriodDays=99999` in the host platform's `settings.json` to preserve session transcripts for trend analysis. This is the host platform's cleanup setting, not Token Optimizer's data.

- Transcripts are the host platform's data (JSONL files), not Token Optimizer's
- Users can override this setting in their `settings.json`
- The `purge` command does NOT delete transcripts (they belong to the host platform)

## Data Deletion

To delete all Token Optimizer data across all platforms:

```
python3 measure.py purge            # dry-run: shows what would be deleted
python3 measure.py purge --confirm  # actually delete
```

Manual deletion: remove the following directories:
- `~/.claude/token-optimizer/`
- `~/.claude/plugins/data/token-optimizer-*/`
- `~/.claude/_backups/token-optimizer/`
- For Codex: `~/.codex/token-optimizer/`
- For OpenCode: `~/.local/share/opencode/token-optimizer/`
- For Cursor: `~/.cursor/token-optimizer/`
- For Antigravity: `~/.gemini/token-optimizer/` (and, to also remove the hook wiring, `~/.gemini/config/plugins/token-optimizer/`)

## Cross-Platform

Locally installed extensions (see `docs/local-extensions.md`) may run inside the session-end flush worker; Token Optimizer ships none by default.

The same privacy guarantees apply across all supported platforms (Claude Code, Codex, OpenCode, Hermes, Copilot, Cursor, Google Antigravity). Data paths vary by platform but the architecture is identical: local-only, zero network, credential-redacted storage.

Consent is tracked per-runtime. A user running both Claude Code and Codex must acknowledge consent separately in each runtime. Antigravity collection is additionally gated by the bridge itself (R20): the Antigravity hooks and rollup no-op until `~/.gemini/token-optimizer/config.json` records `antigravity_consent: true`.

## Source Available

Token Optimizer is licensed under [PolyForm Noncommercial 1.0.0](LICENSE). The full source code is published at [github.com/alexgreensh/token-optimizer](https://github.com/alexgreensh/token-optimizer) and can be audited by anyone. Non-commercial use (personal, research, education) requires no license purchase. Commercial use requires a separate license.

## Contact

For privacy-related questions, reach out to [Alex Greenshpun](https://linkedin.com/in/alexgreensh) or open an issue on the [GitHub repository](https://github.com/alexgreensh/token-optimizer/issues).
