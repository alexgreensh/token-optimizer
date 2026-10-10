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

Token Optimizer writes several local data stores. On macOS and Linux, all are stored with restrictive file permissions (`0o700` directories, `0o600` files). On Windows, Python cannot apply these POSIX modes (`os.chmod` only toggles the read-only flag), so the stores inherit the access rules of your user profile folder, which by default grants access only to your account, Administrators, and SYSTEM.

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
- **Retention:** Configurable via `TOKEN_OPTIMIZER_CHECKPOINT_RETENTION_DAYS` (default: 7 days) `TOKEN_OPTIMIZER_CHECKPOINT_PER_SESSION` (default: 5 per session) and `TOKEN_OPTIMIZER_CHECKPOINT_RETENTION_MAX` (default: 400 files in total)
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
- **Daemon token:** `<plugin-data>/data/daemon-token` (32-byte random secret, 0600 permissions on macOS and Linux)
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

Token Optimizer scans for 28 credential patterns (AWS keys, API tokens, GitHub PATs, database URIs, JWTs, PEM keys, URL query/fragment auth params, and more) and replaces them with `[CREDENTIAL REDACTED: <type>]` before writing to the session store and tool archive. This redaction is one-way and permanent in stored content.

**Known tradeoff:** Credential redaction in the read cache means delta reads against files containing credentials will produce non-empty diffs on every re-read (the stored version has the redacted placeholder, the live file has the actual credential). This is a deliberate security-over-efficiency tradeoff.

Bash compression output preserves credential-containing lines verbatim (not redacted) to ensure compressed output returned to the coding assistant doesn't mangle secrets.

### Custom redaction patterns

You can add your own secret shapes (internal API keys, service tokens, record identifiers) to the built-in list without editing code. Put them in `~/.claude/token-optimizer/redact-patterns.json` (on other platforms, `token-optimizer/redact-patterns.json` under that platform's home, e.g. `~/.codex/`), or set `TOKEN_OPTIMIZER_REDACT_PATTERNS_FILE` to the path of the file, either in your environment or in the `env` block of your global `settings.json` (`~/.claude/settings.json`; hooks do not read project-level settings):

```json
{
  "patterns": [
    "acme_[A-Za-z0-9]{32}",
    {"label": "Acme service token", "regex": "(?P<keep>ACME_TOKEN=)\\S+", "ignore_case": true}
  ]
}
```

- Each entry is a Python regular expression, either as a plain string or as an object with `regex`, an optional `label` (shown in the placeholder, default `custom pattern`), and an optional `ignore_case`. JSON strings need doubled backslashes (`\\d` for `\d`).
- Text matched by a named group `keep` stays in place and only the rest of the match is replaced, the same way the built-in `?token=` pattern keeps the parameter name.
- `TOKEN_OPTIMIZER_REDACT_PATTERNS_FILE` must be an absolute path after `~` and environment-variable expansion (surrounding quotes are stripped first). A relative value would resolve against whatever directory the hook launched in, so it is treated as a configuration error — see fail-closed behavior below.
- Custom patterns are additive and run BEFORE the built-ins, so an org pattern can claim a composite secret (e.g. `MEDX-123456-<jwt>`) whole instead of leaving a readable prefix beside a built-in placeholder. Neither custom nor built-in patterns touch `[CREDENTIAL REDACTED: ...]` placeholders already in the text, so re-running redaction is idempotent. Custom patterns apply everywhere the built-in redaction applies; when scanning rather than rewriting, they are matched per line. They do not change which lines Bash compression keeps verbatim.
- The file is read once per process. Invalid entries (bad regex, empty regex, a regex that matches empty text, wrong types, unsafe or over-broad shapes) are skipped with a warning on stderr; the rest still load. A missing file at the default location is ignored. Limits: 200 entries, 1,000 characters per regex, 1 MB per file.
- **Fail closed:** if the configured file — or a file present at the default location — cannot be trusted (unreadable, invalid JSON, wrong shape, or a relative env path), custom redaction is considered broken rather than absent. Every write that redacts (archives, caches, checkpoints) is then skipped so content your own patterns were meant to cover never reaches disk unredacted, and a one-time warning is shown through the hook's normal output channel. Fix or remove the file to resume writes.
- Unsafe patterns are rejected before they can run: the loader statically rejects nested unbounded quantifiers and ambiguous repeated alternations (the classic "regex hangs forever" shapes), then runs each remaining pattern once against a battery of adversarial strings in a separate process with a hard time limit — a pattern that cannot finish in time is rejected. Verdicts are cached per file content, so this costs nothing on steady-state hook runs.
- `measure.py security-report` shows whether custom redaction is active, how many patterns loaded, from which file, and any rejections; the same status is persisted under the runtime's `token-optimizer/` directory.
- This is pattern-based redaction of known shapes. It is not a general PII or PHI scrubber, and it cannot catch values that have no recognizable shape.

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

## Subagent Prompt-Cache Setting

On Claude Code 2.1.243+, Token Optimizer turns on `subagentPromptCacheTtl: "1h"` when your own history shows it pays, setting it once in the user `settings.json` so subagents keep their prompt cache for an hour instead of five minutes. This is the host platform's billing setting, not Token Optimizer data.

- Never overrides a value you set yourself, an env override, or a managed/project/local settings file
- Undone by `python3 measure.py subagent-cache disable`, by `TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H=0`, or automatically if a 14-day tripwire shows the change costs more than it saves
- `TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H=1` forces it on without the evidence check; `=0` forces it off
- The payoff check reads your own session transcripts locally, in a background process, and keeps one small verdict file and a marker in Token Optimizer's data directory; nothing leaves the machine

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
