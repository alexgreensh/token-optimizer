# Token Optimizer for Google Antigravity (beta)

Token Optimizer's 7th platform. Three surfaces, one adapter:

- **`agy` CLI** — `~/.gemini/antigravity-cli/`
- **Antigravity 2.0 desktop app** — `~/.gemini/antigravity/`
- **Antigravity IDE** — `~/.gemini/antigravity-ide/`

The value prop leads with **tokens**: Antigravity's own UI shows context fill but
does not surface per-session token totals or a cost figure. Token Optimizer
reads Antigravity's own per-conversation SQLite store (read-only), decodes the
`gen_metadata` protobuf blobs with a zero-dependency decoder, and reports token
totals and — where the model has a known Gemini rate card — a clearly-labelled
list-price estimate.

## Install

Run from any folder:

```bash
git clone --depth 1 https://github.com/alexgreensh/token-optimizer.git
cd token-optimizer
bash install.sh --antigravity
# verify (from inside the token-optimizer folder)
TOKEN_OPTIMIZER_RUNTIME=antigravity python3 skills/token-optimizer/scripts/measure.py antigravity-doctor
```

**Already have Token Optimizer installed**? You don't need a fresh clone;
`install.sh` only lives at the repo root. Run the installer module directly:

```bash
TOKEN_OPTIMIZER_RUNTIME=antigravity python3 ~/.claude/skills/token-optimizer/scripts/measure.py antigravity-install
```

This writes a **user-level plugin directory** at
`~/.gemini/config/plugins/token-optimizer/`. It never touches your own
`~/.gemini/config/hooks.json`, `config.json`, or `settings.json`.

### Consent (R20)

Collection is **opt-in**. The installer records consent in
`~/.gemini/token-optimizer/config.json`; until that record exists the bridge and
rollup short-circuit to `{}` / zero. This matches the consent promise in
`HOOKS.md` and `PRIVACY.md`.

## Where the data comes from

| Signal | Source | Notes |
|---|---|---|
| Token totals | `gen_metadata.data` (one protobuf per generation) | input/output/cache-read/thinking, summed over generations |
| Model | `model_display_name` (field 21) | `.3.7`-style names map to a Gemini rate-card id; unknown names stay unpriced |
| Credit cost | `credit_cost` / `consumed_credits` (fields 13/18) | shown when Antigravity reports a non-zero figure; never mixed with the USD estimate |
| Context fill | `estimated_tokens_used` / `max_context_tokens` | last decoded generation |
| Tool calls / user inputs | `steps` step_type + decoded tool name | step_type 14 = user input |
| Title / workspace | `conversation_summaries.db` | `title`, `workspace_uris`, `killed`, `not_fully_idle`, `last_modified_time`, `nesting_depth` |

The decoder is version-tagged (`ag-v1`, pinned to the Antigravity field mapping
verified in the research snapshot). Every field is sanity-gated; a database row
the decoder cannot parse is counted as `undecodable` and surfaced by
`antigravity-doctor` and `antigravity-summary` — never guessed.

### What is never read (R21)

Prompt text (`history.jsonl` `display`, summaries `preview`), tool arguments in
step metadata, and `trajectory_metadata_blob` are never selected, read, or
persisted. See `PRIVACY.md`.

## Parity

| Surface | Status | Notes |
|---|---|---|
| Per-session tokens (in / out / cache-read / thinking) | 🟢 green | decoded from `gen_metadata` |
| Model + duration + tool-call counts | 🟢 green | |
| Context fill + quality grade | 🟢 green | 3-signal subset, like Hermes/Copilot |
| Trends DB + dashboard (port 24847) | 🟢 green | `platform=antigravity`, keyed per surface |
| Continuity restore | 🟢 green | `PreInvocation #1` injects a capped, R22-filtered summary |
| Bash output compression | 🟢 green | `PreToolUse` `run_command` rewrite, wrapper re-validates its argv (R13a) |
| Stop-time rollup | 🟢 green | detached, 60s budget, lease-debounced |
| Doctor / install / uninstall | 🟢 green | |
| Context-growth nudges | 🟡 partial | derived from the live conversation's last decoded generation, not hook payload |
| Crash recovery | 🟡 partial | killed/incomplete sessions flagged honestly; no in-flight tally needed (gen rows persist) |
| Compaction steering | 🔴 red | no `PreCompact` hook event |
| Read-interception (Delta / Structure Map) | 🔴 red | no PostToolUse output rewrite; `PreToolUse` overwrite can only change what is read |
| Status line | 🔴 red | Antigravity owns its status-line data model; no hook surface |
| Keep-warm | 🔴 red | no cache-TTL ping surface |

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TOKEN_OPTIMIZER_ANTIGRAVITY_HOME` | `~/.gemini` | Relocated Antigravity home (accepted only under `$HOME`). |
| `TOKEN_OPTIMIZER_ANTIGRAVITY_BIN` | first `agy` on `PATH` | Override the `agy` binary the doctor probes. |
| `TOKEN_OPTIMIZER_BASH_COMPRESS` | `1` (on) | Set `0` to disable command-output compression. |

The approval prompt for a rewritten command shows the **wrapped** command
(`bash_compress.py …`), because that is what the permission system will actually
see. The wrapper re-checks its argv against the same whitelist and
dangerous-character check before executing anything, so a cached "always allow"
on the wrapper prefix never becomes an unguarded command runner (R13 + R13a).

## Uninstall

```bash
TOKEN_OPTIMIZER_RUNTIME=antigravity python3 skills/token-optimizer/scripts/measure.py antigravity-uninstall
```

Removes only `~/.gemini/config/plugins/token-optimizer/`. Your own hooks and
Antigravity conversation data are untouched. Collected data and trends stay in
place by design; to purge Token Optimizer's own data too:

```bash
rm -rf ~/.gemini/token-optimizer
```

## Live-smoke runbook (first run on a machine with Antigravity)

1. Install and authenticate the CLI (`agy`, then sign in).
2. `bash install.sh --antigravity`
3. `agy plugin validate ~/.gemini/config/plugins/token-optimizer` — expect `hooks: 1 processed`.
4. `measure.py antigravity-doctor` — expect zero `fail`.
5. `measure.py antigravity-summary` — expect per-surface sections with real token totals.
6. `measure.py antigravity-rollup` — expect "Collected N new Antigravity sessions".
7. Run one short `agy -p "run <a whitelisted command like git status>"` session and confirm the recorded step wraps the command through `bash_compress.py`; if the non-interactive turn cannot complete (auth/print-mode), record that and mark the live-injection parity rows partial.
