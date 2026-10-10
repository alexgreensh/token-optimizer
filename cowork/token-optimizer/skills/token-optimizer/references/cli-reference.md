# CLI Reference

## Core Commands

- `measure.py report` -- Token overhead report with per-component breakdown
- `measure.py snapshot [label]` -- Save a measurement snapshot
- `measure.py compare` -- Compare before/after snapshots
- `measure.py dashboard [--coord-path PATH]` -- Generate interactive HTML dashboard
- `measure.py setup-daemon [--uninstall]` -- Bookmarkable dashboard URL (macOS/Windows)
- `measure.py trends [--days N] [--json]` -- Skill adoption, model mix, overhead over time
- `measure.py health` -- Session hygiene: stale/zombie sessions, version checks

## Quality & Compaction

- `measure.py quality [session-id|current]` -- 7-signal quality score (0-100)
- `measure.py setup-smart-compact [--dry-run|--status|--uninstall]` -- Smart Compaction hooks
- `measure.py compact-capture` -- Internal: checkpoint before compaction
- `measure.py compact-restore` -- Internal: restore after compaction
- `measure.py compact-instructions [--json]` -- Generate project-specific compaction guidance
- `measure.py list-checkpoints [--cwd PATH] [--max-age MINUTES]` -- List session checkpoints

## Active Compression (v5)

- `measure.py v5 status` -- Show all features with current state
- `measure.py v5 enable|disable <feature>` -- Toggle a feature
- `measure.py v5 info <feature>` -- Full details for one feature
- `measure.py compression-stats [--days N]` -- Measured savings from local telemetry

## Context Tools

- `measure.py git-context [--json]` -- Suggest files based on git state
- `measure.py read-cache-stats --session ID` -- Cache stats for a session
- `measure.py read-cache-clear` -- Clear all caches
- `measure.py attention-score` -- Score CLAUDE.md attention placement
- `measure.py attention-optimize --dry-run` -- Preview optimized section order
- `measure.py memory-review [--json|--apply|--stale-days N]` -- MEMORY.md structural audit

## Model Routing

- `measure.py inject-routing [--dry-run]` -- Inject model routing block into CLAUDE.md
- `measure.py setup-coach-injection [--uninstall]` -- Inject coaching block

## Deterministic Candidates

- `measure.py deterministic-candidates [--days N] [--json] [--budget SECONDS] [--max-sessions N] [--no-cache]` -- Parts of your workflow that could be plain code instead of model calls: repeated tool-call sequences (3+ calls in 3+ sessions), templated subagent launches (5+), polling loops (same command 4+ times with no edit between), and passing test/build/lint turns whose reply was short. Local, read-only, no model calls; every figure is "measured on your transcripts" (tokens at real cache weights, API-equivalent dollars), examples are credential-redacted command shapes, top 10 by tokens. Default window 30 days; results are cached in the Token Optimizer data dir keyed by the newest transcript. `measure.py coach --json` carries the same block as `deterministic_candidates` (8 s budget, `"partial": true` if it ran out). Claude Code and Codex are measurable; other runtimes return `not measurable on <runtime>`.

## JSONL Toolkit

- `measure.py jsonl-inspect` -- Stats, record counts, largest records
- `measure.py jsonl-trim --dry-run` -- Preview trimming large tool results
- `measure.py jsonl-dedup --dry-run` -- Preview removing duplicate reminders

## Other

- `measure.py savings` -- Cumulative dollar savings report
- `measure.py expand [--list|<tool-use-id>|--search <query>]` -- Retrieve archived tool results, or search by keyword/lineage
- `measure.py setup-hook [--uninstall]` -- SessionEnd tracking hook
- `measure.py setup-quality-bar [--uninstall]` -- Terminal status line
- `measure.py plugin-cleanup` -- Detect duplicate skills and archive overlaps
- `measure.py check-hook` -- Check if SessionEnd hook is installed

## Codex Commands

- `measure.py codex-install --project PATH [--profile balanced|quiet|telemetry|aggressive]`
- `measure.py codex-doctor --project PATH [--json]`
- `measure.py codex-compact-prompt [--install|--uninstall|--status]`
- `measure.py codex-skill [enable|disable] NAME`
- `measure.py codex-mcp [enable|disable] NAME`

## Cursor Commands

- `measure.py cursor-install` -- Wire Token Optimizer into `~/.cursor/hooks.json` (read-merge-write)
- `measure.py cursor-uninstall` -- Remove only Token Optimizer's hook entries + payload
- `measure.py cursor-doctor [--json]` -- Readiness report (binary, home, hooks, payload, data planes, daemon port 24846)
- `measure.py cursor-doctor --probe` -- Replay each wired event through the installed command
- `TOKEN_OPTIMIZER_RUNTIME=cursor measure.py cursor-summary` -- Token/quality session summary (no cost headline by design)
- `TOKEN_OPTIMIZER_RUNTIME=cursor measure.py cursor-rollup` -- Ingest Cursor sessions into the trends DB

## Feature Controls

### Read-Cache
- Default ON (warn mode). Disable: `TOKEN_OPTIMIZER_READ_CACHE=0`
- Modes: `TOKEN_OPTIMIZER_READ_CACHE_MODE=warn` (default) or `=block`
- Decisions log: `~/.claude/token-optimizer/read-cache/decisions/`

### Efficiency Grading
Grades: S (90-100), A (80-89), B (70-79), C (55-69), D (40-54), F (0-39).
Shown in status line (`ContextQ:A(82)`), dashboard, coach, and CLI.

### .contextignore
Create `.contextignore` in project root or `~/.claude/.contextignore` (global). Uses gitignore-style glob patterns. Hard-blocks regardless of read-cache mode.
