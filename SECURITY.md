# Security Policy

Token Optimizer is a local-first developer tool that optimizes AI coding assistant context windows. This document describes its security architecture, data handling practices, and known limitations.

## Architecture Overview

Token Optimizer is **100% local**. It runs as a set of Python hook scripts invoked by the host platform (Claude Code, Codex, OpenCode, Hermes) during normal tool call lifecycle events.

- **Zero network calls.** No telemetry, no analytics, no crash reporting, no phone-home.
- **Zero runtime dependencies.** Pure Python stdlib. No pip packages, no node_modules, no native binaries.
- **Single-user.** Designed for individual developer workstations, not multi-tenant servers.

Data flow: `Host platform tool call -> Hook event -> Token Optimizer script -> Local file write (SQLite / JSON / markdown)`

No data leaves the machine at any point in this flow.

---

## Data Handling

### What Token Optimizer Reads

- Host platform session transcripts (JSONL files) for trend analysis
- `settings.json`, `CLAUDE.md`, `MEMORY.md` for audit measurements
- Skill and MCP server directories for token inventory
- Tool call inputs/outputs passed via hook stdin

### What Token Optimizer Writes

| Store | Contents | Retention | Path Pattern |
|-------|----------|-----------|--------------|
| trends.db (SQLite) | Per-session aggregates: token counts, model usage, cost estimates, session UUIDs | Configurable (`TOKEN_OPTIMIZER_TRENDS_RETENTION_DAYS`, default: unlimited) | `<plugin-data>/data/trends.db` |
| Session stores (SQLite) | File read records, content hashes, cached file content (up to 50KB/file, credential-redacted), activity log | Auto-deleted after 48 hours | `<plugin-data>/data/session-store/<id>.db` |
| Checkpoints (markdown) | Truncated conversation context (up to 300 chars of last user/assistant message), extracted decisions, error snippets | Configurable (`TOKEN_OPTIMIZER_CHECKPOINT_RETENTION_DAYS`, default: 7 days, max 50 files) | `~/.claude/token-optimizer/checkpoints/` |
| Tool archives (JSON) | Credential-redacted tool output for retrieval | Configurable (`TOKEN_OPTIMIZER_ARCHIVE_RETENTION_HOURS`, default: 24 hours) | `<plugin-data>/data/tool-archive/` |
| Quality cache (JSON) | Per-session quality score snapshots | Configurable (`TOKEN_OPTIMIZER_QUALITY_CACHE_RETENTION_DAYS`, default: 7 days) | `~/.claude/token-optimizer/quality-cache-*.json` |
| Config (JSON) | Feature flags, consent status, pricing tier | Persistent until purge | `~/.claude/token-optimizer/config.json` |
| Dashboard (HTML) | Generated visualization | Regenerated on demand | `<plugin-data>/data/dashboard.html` |

### What Token Optimizer Never Does

- Transmit data over the network (no HTTP clients, no sockets, no DNS lookups)
- Access browser data, SSH keys, Keychain, or credentials outside the host platform directory
- Modify source code files or project content
- Execute user code or eval any content from transcripts

---

## Access Controls

### File Permissions

All directories created with mode `0o700` (owner read/write/execute only). All files created with mode `0o600` (owner read/write only). Enforced at creation time via `os.makedirs(mode=0o700)` and `os.open(..., 0o600)`.

### Dashboard API Security

The optional dashboard HTTP server:

- Binds to `127.0.0.1` / `[::1]` only (loopback, never externally accessible)
- Requires `X-TO-Token` header with a per-install 32-byte cryptographically random secret (`secrets.token_urlsafe(32)`) for all state-mutating POST endpoints
- Token comparison uses `hmac.compare_digest()` (constant-time, prevents timing attacks)
- Token file stored at `<plugin-data>/data/daemon-token` with `0600` permissions
- Origin header must be a localhost prefix (DNS rebinding guard)
- Host header must be a localhost form
- POST body capped at 64KB
- Response headers: `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Content-Security-Policy` set
- JSONL path access restricted to `~/.claude/projects/` subtree

### Consent Gate

First-run data collection requires acknowledgment. Hooks exit early (fail-open, no data collection) until the consent notice has been shown during initial session startup. Existing users who already saw the v5 welcome are automatically backfilled. Consent status can be checked and reset via `measure.py consent --show` / `--reset`.

---

## Input Validation

- **No `shell=True`** in any subprocess call across the entire codebase
- **Shell metacharacters rejected:** Commands containing `;|&$(){}><\n\r\x00` are categorically excluded from processing
- **Session IDs sanitized** to `[a-zA-Z0-9_-]` only with UUID fallback for invalid inputs
- **Path traversal guards:** All plugin data directory paths are verified to resolve inside the runtime home. Symlinks are rejected.
- **Environment variable whitelist:** Only a predefined set of safe env vars can be passed through command rewrites
- **Marketplace name validation:** Must match `[A-Za-z0-9._-]+`

---

## Credential Protection

Token Optimizer scans for and redacts 23 credential patterns before writing content to disk:

AWS keys (`AKIA...`), OpenAI/Anthropic keys (`sk-...`), GitHub PATs (all 5 prefix types + fine-grained), npm tokens, Slack tokens (bot/user/app), Stripe keys (live/restricted), HuggingFace tokens, Bearer headers, Google API keys, Google OAuth tokens, JWTs, PEM private key headers, database URIs with embedded passwords, HTTP basic auth URLs, and credentials passed as URL query/fragment parameters (`?token=`, `?api_key=`, `#access_token=`, and similar — value redacted, parameter name preserved).

Credentials are replaced with `[CREDENTIAL REDACTED: <type>]` in:
- Read cache (session store SQLite)
- Tool archives (JSON)

Bash compression output preserves credential-containing lines verbatim (not redacted) to ensure compressed output doesn't mangle secrets that appear in command output.

---

## Encryption

**At rest:** Token Optimizer does not encrypt local data stores. Data protection relies on filesystem permissions (`0o700` directories, `0o600` files). For environments requiring encryption at rest, enable FileVault (macOS) or LUKS (Linux) at the OS level.

**In transit:** No data is transmitted. The dashboard server uses plain HTTP but is bound to loopback only (`127.0.0.1`), so traffic never leaves the network stack.

---

## Data Retention and Deletion

Each data store has a configurable retention period (see Data Handling table above). Retention enforcement runs at session end and during smart compaction.

To delete all Token Optimizer data across all platforms:
```
python3 measure.py purge          # dry-run: shows what would be deleted
python3 measure.py purge --confirm  # actually delete
```

The purge command does NOT delete host platform data (transcripts, settings.json).

---

## Change Management

- All releases are git-tagged with semantic versioning
- Plugin distributed via Claude Code marketplace with automatic updates
- npm package published with Sigstore provenance attestation where available
- No hot-patching: all changes require a version bump and release

---

## Incident Response

**Email:** me@alexgreenshpun.com

- Acknowledgment within 48 hours
- Fix or mitigation within 7 days for critical issues
- Only the latest release is supported with security updates

---

## Supply Chain

- **Zero runtime dependencies** across all components (Python core, TypeScript OpenClaw plugin, VS Code extension). No `requirements.txt`, no runtime npm packages.
- **No `eval()` or `exec()`** on any content derived from user transcripts or tool output
- **No dynamic code loading** from external sources

---

## Known Scanner Findings

Security scanners (including repo-forensics) will flag the following patterns. These are documented here for transparency:

### "Config write request: modify auto-executed file" (HIGH)

Token Optimizer's skills instruct the AI assistant to modify `CLAUDE.md` and `settings.json` as part of optimization workflows (e.g., adding model routing instructions, configuring compaction settings). This is the tool's intended behavior. All such modifications go through the host platform's permission system, which requires user approval before any file write.

### "Tainted data reaches sink" (CRITICAL, false positive)

The dataflow scanner flags `os.environ.get()` values flowing into `subprocess.run()` calls. All subprocess calls use **list arguments** (never `shell=True`), and the env values set `cwd`, `capture_output`, or other kwargs, not command strings. No user-controlled input reaches a shell interpreter.

### "Potential data exfiltration" (CRITICAL, false positive)

The correlation engine flags files that read environment variables AND contain network-related code. The "network code" is the localhost-only dashboard HTTP server bound to `127.0.0.1`. No outbound connections exist.

### "Hook installation directive" (HIGH)

Token Optimizer installs hooks into the host platform's `settings.json` via the `ensure-health` command. This is part of the standard plugin installation lifecycle. Hooks are registered in the JSON hooks array, not as executable files.

---

## Limitations and Transparency

| Limitation | Mitigation |
|-----------|------------|
| No encryption at rest | Filesystem permissions (0o700/0o600). Use OS-level encryption for additional protection. |
| No admin policy enforcement / MDM | Single-user local tool. Config is per-user, no org-wide lockdown. |
| No structured audit log | Functional telemetry (checkpoint events, compression events) provides partial coverage. |
| No TLS on dashboard | Loopback-only binding. Traffic never leaves the machine. |
| settings.json is user-writable | Hook registrations have no integrity verification. A local attacker with filesystem access could modify hook configuration. |
| Transcript preservation | Token Optimizer sets `cleanupPeriodDays=99999` in settings.json to preserve transcripts for trend analysis. This is intentional and documented. Users can override this setting. Transcripts are the host platform's data, not Token Optimizer's. |

---

## Security Self-Assessment

Enterprise IT can generate a machine-readable security report:
```
python3 measure.py security-report        # human-readable
python3 measure.py security-report --json  # machine-readable
```

The report covers: runtime environment, data stores inventory with permissions, consent status, retention configuration, hook configuration, credential scanning coverage, dashboard security, and version information.
