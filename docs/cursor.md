# Token Optimizer for Cursor (beta)

Token Optimizer's Cursor adapter. One adapter, two surfaces:

- **Cursor IDE** — the Composer and its per-request token fields in `state.vscdb`
- **Cursor CLI (`agent`)** — the same eleven wired hook events plus a transcript path

The value prop is **token accounting and continuity without a second dashboard
to watch**: Cursor shows context usage natively but does not persist per-request
token totals in one place, and it ships no hook-surfaced cost figure. Token
Optimizer reads Cursor's own stored token fields read-only, never a re-derived
pricing table — and where Cursor exposes no cost number, it says so instead of
inventing one.

## Install

Run from any folder. The clone creates a `token-optimizer/` folder; `cd` into it before running the installer:

```bash
git clone --depth 1 https://github.com/alexgreensh/token-optimizer.git
cd token-optimizer
bash install.sh --cursor
# verify (from inside the token-optimizer folder)
TOKEN_OPTIMIZER_RUNTIME=cursor python3 skills/token-optimizer/scripts/measure.py cursor-doctor
```

**Already have Token Optimizer installed** (Claude Code plugin, script install,
or a checkout under `~/.claude/skills/token-optimizer`)? You don't need a fresh
clone, and you won't find `install.sh` inside the skill folder — it only exists
at the repo root. Run the installer module you already have, directly:

```bash
TOKEN_OPTIMIZER_RUNTIME=cursor python3 ~/.claude/skills/token-optimizer/scripts/measure.py cursor-install
TOKEN_OPTIMIZER_RUNTIME=cursor python3 ~/.claude/skills/token-optimizer/scripts/measure.py cursor-doctor
```

Adjust the path if your `measure.py` lives elsewhere — any up-to-date copy
works. Script installs can equivalently run
`bash ~/.claude/token-optimizer/install.sh --cursor`.

This read-merges user-level hooks into `~/.cursor/hooks.json` and copies the
adapter into `~/.cursor/token-optimizer/plugin/`. Cursor has **one shared**
`hooks.json` (unlike Copilot's per-plugin file), so the installer merges rather
than overwrites: entries owned by other tools are preserved verbatim, and only
the entries whose `command` points at our bridge are replaced on re-install or
removed on uninstall.

### Windows

On native Windows the installer writes hook commands that PowerShell and cmd.exe both run (a bare interpreter path and double-quoted arguments, the same form the Codex hooks use). A folder name holding `$`, a backtick, `%` or `!` goes through its short 8.3 name; if there is none, the installer says what to move. Use `python`, not `python3`, in the commands below.

## No capability map — hooks are observed, not version-gated

Copilot's CLI ships weekly and its hook fields regress, so that adapter gates
features on a per-version capability map. Cursor's hooks are structured
differently: every handler appends one line to
`~/.cursor/token-optimizer/observed-events.jsonl`, and each event records whether
the Shell rewrite was `attempted`, `honoured`, or `ignored` — the latter two
only when the executed command pairs byte-for-byte with the rewrite staged
under the same `tool_use_id`; an unpaired execution records nothing rather
than guessing. `cursor-doctor
--probe` replays the documented payloads through the installed `command`, so on
any given build you can prove exactly which of the eleven events fire — no version
table to rot.

If a hook field upstream changes, the evidence is on disk, and
`cursor-doctor` reports the toggles directly rather than trusting a stale map.

## What Cursor hands a companion, and what it keeps

| Event | TO uses it for | Output shape |
|---|---|---|
| `sessionStart` | per-workspace continuity restore (`additional_context`) | `{"additional_context": "..."}` |
| `preToolUse` (Shell\|Read matcher) | Shell: bash output compression (`updated_input`); Read: read-cache interception (structure map / unchanged note; shadow by default, `soft_block`/`block` opt-in) | Shell: `{"permission": "allow", "updated_input": {...}}`; Read: `{"permission": "deny", "user_message": "..."}` under an armed mode, else nothing |
| `postToolUse` | tally update + Shell rewrite-honoured evidence + context nudge + thrash loop nudge (identical-output streak, burn streak, inline-script repeat — same thresholds as Claude) | `{"additional_context": "..."}` |
| `preCompact` | record real `context_tokens` / `context_window_size` / `context_usage_percent` + capture a redacted compact checkpoint (one bounded restore rides the next `postToolUse`) | `{"user_message": "..."}` when a checkpoint is captured, else none |
| `stop` | per-turn tally + throttled rollup/dashboard refresh | none |
| `sessionEnd` | finalise tally + unthrottled rollup/dashboard | none |
| `afterAgentResponse` | records hook-reported token fields for offline correlation; these figures are not counted in session totals | none |
| `postToolUseFailure` | per-tool failure counts for the burn signal | none |
| `afterShellExecution` | records shell completion; rewrite evidence comes from paired `postToolUse` | none |
| `subagentStart` / `subagentStop` | subagent fan-out counts for fleet rows | none |

The bridge always exits 0 and never blocks a Cursor session. The `Shell`
rewrite is limited to inspection commands that cannot write, delete or run
other programs (`ls`, `git status`, `git log`, `rg`, `grep`, `wc`, `du`, `df`,
`tail`, `tree`), because Cursor approves a rewritten command. It **fails
closed** (emits nothing) on any uncertainty.

### Honest gaps

| Gap | Why | Tracking |
|---|---|---|
| Per-session cost | Cursor exposes no billing figure to hooks or state files; there is no number to pass through, so cost-source is `cursor_no_cost_data` and no savings headline is shown rather than a wrong one | marked red in the capability matrix |
| Compaction steering | Capture + restore shipped: `preCompact` captures a redacted checkpoint (U1 disk-transcript adapter -> `compact_capture`) and the next `postToolUse` offers exactly one bounded restore. Steering the compaction itself remains observational — Cursor compacts server-side/model-side, and the restore's delivery into the rebuilt context is claimed only with positive native evidence (ledgered `emitted` otherwise) | native validation open |
| Database parity surfaces | Cursor moves token fields across `state.vscdb` releases; the reader maps the known shapes and falls back instead of guessing | read-only, never writes |
| Native IDE Memories | Settings > Memories have no verified local file surface and the IDE state DB schema is unverified; `cursor-audit` performs no IDE DB reads and does not audit them. User Rules and Team Rules are also not file-backed and not audited | marked not-covered in audit output |
| Read interception (structure maps) | Shipped shadow-first: `preToolUse` records redundant-read decisions by default and emits nothing. Changed files are allowed; fingerprints are confirmed by matching successful Read events. `soft_block`/`block` are manual per-install opt-ins (`read-cache.json`); `cursor-doctor` reports `unknown`/`shadow`/`armed-unverified`, and `armed-verified` requires positive native evidence. `soft_block` degrades to `warn` after 3 denies for a session and file; `block` never degrades | native delivery validation open |

Consequences and mitigations:

- **Crash-killed / killed-composer sessions** never fire a clean `sessionEnd`.
  The `postToolUse` hook maintains an in-flight tally and the rollup recovers
  partial data flagged as `~est.` — never silently dropped, never silently exact.
- **Token figures have a strict ordering**, highest trust first: Cursor's own
  `state.vscdb` token fields (`cursor_state_vscdb`), then a transcript-derived
  estimate (`cursor_transcript_estimate`), then the in-flight tally alone
  (`cursor_tally_only`). The doctor and summary name which source was active for
  a session; sources are never summed.
- **Chat-only sessions** (no tool calls) still get continuity restore at
  session start; the postToolUse context nudge needs tool activity to fire.
- **Continuity restore is per workspace**, keyed by `sha1(workspace_root)`, so a
  busy IDE with many concurrent chats never seeds a new chat with another
  repo's rollup. Restore is skipped while a sibling chat in the same workspace
  is still active.

## Data sources, one active per session

| Source | What it gives | Role |
|---|---|---|
| `state.vscdb` → `cursorDiskKV` | per-request token fields Cursor persists | authoritative when present |
| `transcript_path` | transcript file for the conversation | estimate fallback |
| `<cursor_home>/token-optimizer/sessions/<id>.json` | our durable in-flight tally | crash recovery |

`cursor-doctor` names which source is active on this install.

## Commands

```bash
measure.py cursor-install     # read-merge hooks.json + copy the payload
measure.py cursor-doctor      # readiness + hook-firing probe (--probe)
measure.py cursor-summary     # token/quality session summary (reads tallies)
measure.py cursor-coach       # native coach data: sessions, quality, patterns (--json)
measure.py cursor-rollup      # ingest sessions into trends.db (auto on stop/sessionEnd)
measure.py cursor-audit       # rules + hooks setup audit (read-only, never fatal)
measure.py cursor-uninstall   # remove only what we installed
```

## Quality score signals

The Cursor quality score uses every signal the Cursor data planes actually
feed, each with named provenance, and omits the rest with named reasons —
unavailable signals are never filled with a neutral-healthy default and the
composite renormalizes:

- **Context fill** — always omitted on Cursor: there is no current
  occupancy source. The host's preCompact numbers (`context_tokens` /
  `context_window_size`) are HISTORICAL (taken at compaction time), so
  they are reported as labeled metadata only (`fill_ratio`,
  `fill_is_current: false`, `fill_measured_at`) and feed compaction
  depth's event heat - they never grade the current session, and no
  lifetime token sum ever stands in for occupancy.
- **Message count** — tally turns.
- **Output/input ratio** — only with verified state.vscdb token pairs.
- **Compaction depth** — preCompact event frequency and heat.
- **Agent efficiency** — recorded tool failures plus terminal `turn_ended`
  status/error (the disk transcript's only failure evidence).
- **Decision density** — from the disk transcript (tool calls + substantive
  text).

`stale reads`, `bloated results` and `duplicate reminders` stay omitted:
Cursor disk transcripts never persist tool outputs (bundle-proven), no read
ledger exists yet, and the writer strips system reminders. The grade scale
(S/A/B/C/D/F) is identical to every other runtime.

`.cursor/rules/*.mdc`, AGENTS.md and legacy `.cursorrules` get the same
attention-curve audit CLAUDE.md gets, plus `~/.cursor/hooks.json` health
(`cursor-audit`).

Run them with `TOKEN_OPTIMIZER_RUNTIME=cursor` set (the installer and hooks set
it for you). `cursor-rollup` and `cursor-summary` refuse with a hint unless the
runtime is pinned to `cursor` — the bridge always pins it, so the refusal only
catches a stray manual run.

## Uninstall

```bash
TOKEN_OPTIMIZER_RUNTIME=cursor python3 skills/token-optimizer/scripts/measure.py cursor-uninstall
```

Removes the Token Optimizer hook entries from `~/.cursor/hooks.json`, the
payload, read fingerprints, checkpoints and restore-context caches. Other tools' hooks
are left intact. The uninstall is idempotent; running it on a clean config is a
no-op.

**Cursor session data is left in place by design.** Token Optimizer reads
Cursor's state and transcript files read-only and never moves or owns them. To
purge Token Optimizer's remaining data (tallies, the observed-events
ledger, and the trends rows) too:

```bash
rm -rf ~/.cursor/token-optimizer
```

## Live-smoke runbook (first run on a machine with Cursor)

1. Install Cursor (`agent` CLI or the IDE) and open a project.
2. `bash install.sh --cursor`
3. `measure.py cursor-doctor --probe` — expect the eleven wired events to replay
   green, and the data-source check to name what this install can read.
4. Run one short Cursor session that executes a whitelisted shell command
   (e.g. `git status`).
5. Confirm: `observed-events.jsonl` gained `sessionStart`, `preToolUse`,
   `postToolUse`, and either `stop` or `sessionEnd` lines; a
   `sessions/<id>.json` tally appeared and (on a clean exit) is marked final;
   `cursor-summary` shows the session with its active token source; the trends
   DB gained a row after the stop/sessionEnd rollup.
6. If the whitelisted command ran bare (not through `bash_compress.py`), the
   `postToolUse` line records `rewrite: ignored` — file it against the hook
   contract, and `cursor-doctor` will show the observed toggle for it.

### Compaction recovery (U3)

1. Run a session long enough to compact (or force one).
2. `observed-events.jsonl` should gain `compactCapture` at `preCompact` and
   `compactRestore` at the next `postToolUse`; the tally's
   `restore_pending` flips true then false, and `compact_checkpoint` names a
   0o600 file under `token-optimizer/checkpoints/`.
3. The checkpoint body must contain no credentials (redaction is applied at
   translation and again at capture).
4. The restore rides `additional_context` exactly once, byte-bounded;
   consecutive compactions coalesce to a single restore. Delivery into the
   rebuilt context is claimed only when a follow-on session demonstrably
   uses it — the ledger says `emitted`, never `delivered`, without that
   evidence.

Cursor read suppression respects the workspace `.contextignore` and the active runtime home’s global `.contextignore` (`~/.cursor/.contextignore` by default).
