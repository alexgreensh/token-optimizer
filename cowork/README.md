# Token Optimizer for Claude Cowork

> **⚠️ BETA.** This Cowork adapter is experimental and unverified on a live
> Cowork build. Install the `to-hook-probe` first to confirm which hooks fire
> before relying on any automatic feature. Cloud-only Cowork accounts require
> account/marketplace-level install (not local); see the personal + org paths below.

First-cut Cowork adapter. Core insight: TO is **already a plugin in the
shared Claude Code/Cowork format**, so Cowork parity is packaging +
distribution + the hook subset that fires in Cowork + a domain allowlist
entry + a probe to prove it — not a rewrite. The engine (`measure.py` et
al.) ships as-is; this directory adds the delivery mechanics, mirroring
the `codex_*` / `opencode/` / `openclaw/` per-host adapter pattern.

**Why the install path is different from every other host:** Cowork does
NOT read `~/.claude`, and injecting settings into a local VM session dir
does not make hooks fire (proven dead). The working path (v4 correction,
first-hand from a shipped Cowork plugin) is the **Anthropic org admin
plugin console**: push a plugin as *available to install* / *installed by
default* / *required* and it account-syncs into every org user's Cowork
sessions — **cloud and local**. Org-pushed plugin hooks fire; a hook that
phones home additionally needs its domain on Cowork's **domain allowlist**.

Since October 6, 2026, new Cowork tasks on Pro and Max plans run in the
cloud. A cloud session leaves no local transcript on your machine, which
is why per-session cloud token figures need org telemetry (see
`MISSING.md`).

## What's here

| Piece | What it does |
|---|---|
| `to-hook-probe/` | Diagnostic plugin: 13-event fire matrix, env dump, marker files, phone-home. Push this FIRST. |
| `collector/to_collector.py` | Stub HTTP collector: probe phone-home + Cowork OTel (OTLP/HTTP) capture + `api_request` token summarizer. |
| `../skills/token-optimizer/scripts/cowork_install.py` | Packager: builds the org-console payloads under `dist/cowork/`. Never touches `~/.claude`. |
| `../skills/token-optimizer/scripts/cowork_doctor.py` | Readiness report: desktop build, allowlist keys, session tree, payload, probe matrix, OTel — with an explicit NEEDS-LIVE checklist. |
| `MISSING.md` | What this cut does not cover + every claim that still needs a live org console / Cowork session. |

## The hook set

The packaged `hooks/hooks.json` is the Claude Code one **trimmed to the
events verified to fire in Cowork** (UserPromptSubmit, PreToolUse,
PostToolUse, Stop), with keepwarm dropped (its keep-a-local-CLI-warm
premise doesn't transfer). Cowork does **not** fire SessionStart, so the
run-once work that SessionStart handles on Claude Code (ensure-health
bootstrap, quality-cache --force, compact-restore) rides UserPromptSubmit
instead, gated by the same per-session markers. Commands are byte-identical
to the Claude Code plugin — same `${CLAUDE_PLUGIN_ROOT}` bash-resolver
pattern, same `measure.py` entrypoints — and every hook is additive and
fail-open, so a non-firing event degrades nothing. If the probe shows more
events firing, widen `COWORK_EVENTS` in `cowork_install.py` and repackage.

What rides in per event:

- **UserPromptSubmit** — `quality-cache --warn`, `prompt-continuity`, `verbosity-steer`, plus the run-once bootstrap (`ensure-health`, `quality-cache --force`, `compact-restore`) gated by per-session markers
- **PreToolUse** — `read_cache` (Read), `bash_hook` (Bash), `checkpoint-trigger` (Agent|Task), `refetch_guard` (mcp__.*)
- **PostToolUse** — `posttooluse_runner` (consolidated: bash_compress, archive_result, context_intel, read_cache --invalidate, quality-cache --throttle-only)
- **Stop** — `compact-capture`, `session-end-flush`

## Rollout (org admin)

1. **Package** (from the repo root):
   ```bash
   bash install.sh --cowork
   ```
   Builds `dist/cowork/token-optimizer-cowork-<ver>.zip` and
   `dist/cowork/to-hook-probe-<ver>.zip`. For cloud coverage, set
   `TO_PROBE_URL` in `to-hook-probe/probe.env` (copy the `.example`)
   **before** packaging.

2. **Probe first.** In the Anthropic admin console → Settings → Plugins,
   register `to-hook-probe` as *installed by default* for a test group.
   Run one Cowork session (local, then cloud). Read the result per
   `to-hook-probe/README.md`. This is the gate: don't push the main
   plugin until the probe proves hooks fire on your build.

3. **Allowlist.** Add the collector's domain to Cowork's domain allowlist
   (org security settings). Without it, phone-home and OTel POSTs are
   blocked. Locally you can see whether enforcement is on via
   `cowork_doctor.py` (it reads the live `dxt:allowlistEnabled` /
   `dxt:allowlistLastUpdated` keys in the desktop config); the allowlist
   *contents* are only visible in the console.

4. **Push the main plugin** the same way (start *available to install*,
   promote to *installed by default* once the probe matrix is green).

5. **Telemetry (Team/Enterprise).** Point Cowork's OTel export
   (Organization settings → Cowork; http/protobuf, no gRPC) at a running
   collector:
   ```bash
   python3 cowork/collector/to_collector.py --host 127.0.0.1 --port 4318
   python3 cowork/collector/to_collector.py --summarize   # after a session
   ```
   Put a protected HTTPS ingress in front of the loopback collector so cloud
   VMs can reach it. Restrict that ingress to expected senders and enforce
   request limits; the collector itself does not authenticate POSTs. Do not
   expose its raw port. Its domain is the one you allowlisted.

6. **Verify:**
   ```bash
   python3 skills/token-optimizer/scripts/cowork_doctor.py
   ```
   Zero FAIL and an empty NEEDS-LIVE tail = parity confirmed for the
   pushed event set.

## Testing today without an org (personal upload)

Cowork also accepts a **custom plugin uploaded by an individual user** —
Claude desktop → Settings → Plugins → upload custom plugin (exact menu
wording varies by build). The upload account-syncs into your own sessions
only, which is enough to run the whole to-hook-probe experiment **today**
with zero org-admin involvement: same zips from `install.sh --cowork`,
same verification steps, with the console pushes in steps 2 and 4 replaced
by a personal upload. Two caveats: availability tiers
(available/default/required) are org-console-only, and the domain
allowlist — when enforcement is on — is still org-controlled, so the
phone-home leg of a personal test rides on enforcement being off (check
with `cowork_doctor.py`).

## License note

Token Optimizer is **PolyForm-Noncommercial-1.0.0**. An org-wide rollout
at a company (IV, Nostik, any customer) is commercial use, so that step is
a **terms decision** — commercial license or separate agreement — before
any org-console push, not just a technical one. Personal-upload testing
and evaluation are what the noncommercial license covers.

## What this cut deliberately is not

- No MCP server / companion menubar / statusline replacement — the v3
  three-plane design remains the fallback if the probe disproves the v4
  hook claim, but hooks-as-primary makes those optional, not required.
- No trends.db ingestion of OTel yet (collector is capture + summarize;
  see `MISSING.md`).
- No cloud `cse_*` token accounting — provably closed (v3 §7): no local
  transcript, no token fields in the Compliance API, OTel is the only
  window and it's org-gated.
