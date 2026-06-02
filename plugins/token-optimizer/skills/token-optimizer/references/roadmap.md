# Token Optimizer Roadmap

## v2.0 (Current)

**Theme: Active Session Intelligence**

From static audit to active protection. Three new capabilities:

1. **Smart Compaction System**: PreCompact state capture + SessionStart restoration + Compact Instructions generation. Protects decisions, error sequences, and agent state across compaction events.

2. **Context Quality Analyzer**: JSONL-based quality scoring (stale reads, bloated results, duplicates, compaction depth, decision density, agent efficiency). Measures content quality, not just quantity.

3. **Session Continuity Engine**: Extends checkpoints to Stop/SessionEnd events. Keyword relevance matching for cross-session context restoration.

## v2.1 (Planned)

**Theme: Cross-Session Intelligence**

Move from single-session analysis to accumulated wisdom across all sessions.

- **Usage pattern mining**: Analyze session logs for personalized optimization recommendations. Which projects compact most? Which skills are truly high-value? Where do sessions die?
- **Session benchmarks**: Average compaction count per project, model routing efficiency, session duration distributions. Compare against your own history and community baselines.
- **Weekly/monthly digest**: "Spotify Wrapped for Claude Code." Token spend by project, most-used skills, efficiency trends, waste reduction over time.
- **Personalized recommendations**: Based on YOUR usage history, not generic advice. "You compact 3x more in project-X than project-Y. Here's why."

## v2.1: Lost-in-the-Middle Optimizer

- Score CLAUDE.md and MEMORY.md structure against the U-shaped attention curve (models attend more to beginning and end of context).
- Flag critical rules sitting in the low-attention zone (30-70% of file).
- Generate optimized version with cache-aware ordering for better prompt cache hit rates.
- Provide before/after attention heatmaps showing where Claude is likely paying attention.

## v2.2 (Planned)

**Theme: Active Context Management**

- **Lightweight tool result summarization**: PostToolUse hook that summarizes large tool results before they fill context. Configurable threshold (e.g., >4KB auto-summarize).
- **JSONL bloat detection and cleanup**: Identify sessions with unusually large JSONL files. Surface patterns (e.g., "80% of this session's tokens were tool results from a single Bash command").
- **Agent Teams state protection**: Specialized checkpoint handling for Agent Teams dispatches, which are catastrophically lost on compaction.
- **Background quality guard**: Optional daemon that monitors active session quality and sends notifications when quality drops below threshold. Suggests `/compact` or `/clear` at the right moment.

## v2.3 (Planned)

**Theme: Structure-Aware Compression**

Move from measuring context waste to actively replacing expensive context with smaller, task-shaped representations.

### ELI5 Product Definition

Token Optimizer is not just a token meter. It is becoming a **backpack manager** for the model.

Today, the model often reads the whole book when it only needs the table of contents.
This track adds a **code map layer** that gives the model the shape of a file before it pays for the full file:

- What classes are here?
- What functions exist?
- What gets exported?
- What imports does this file depend on?
- What changed since the last read?

The goal is not to clone a code-structure tool. The goal is to make Token Optimizer better at handing the model the **smallest useful representation** for the job.

### Why This Matters

Current read-cache and digest features are good at saying:

- "You already read this"
- "This file did not change"
- "Here is a rough digest"

But they are still weak at saying:

- "Here is the exact compressed view you need instead"

That is the gap this roadmap item closes.

### User-Facing Behavior

When a file is large, already read, or likely redundant, Token Optimizer should be able to offer:

- **Signatures only**: function, method, class, interface, type names
- **Skeleton view**: file outline with bodies removed
- **Focused view**: just one class, function, export, or section
- **Top-level map**: imports, exports, globals, entry points
- **Diff-aware view**: what structurally changed since the last read

In plain language: instead of "don't reread this file," the tool should say "here's the compact version that will still let you keep working."

### Integration Points

This work should plug into existing Token Optimizer features rather than living as a separate product:

- **Read cache**: replace heuristic digests with richer structural summaries
- **Archive previews**: show compact structure before asking the user to expand a large archived result
- **Tool result summarization**: use structure-aware summaries for code-heavy outputs
- **Quality scoring**: reward effective use of compressed structure vs repeated full reads
- **Savings tracking**: estimate tokens saved by structure-first flows

### Design Constraints

- Must be **inspiration-driven**, not copied from any external implementation
- Must preserve Token Optimizer's role as an **optimization layer**, not turn into a general code exploration product
- Must degrade gracefully:
  - AST/semantic parsing when available
  - cheap structural heuristics when not
- Must support the languages users hit most often first:
  - Python
  - TypeScript / JavaScript
  - then expand carefully

### Success Criteria

- Redundant read blocking feels helpful, not restrictive
- Large code reads are replaced by compact structure views when possible
- Users can stay oriented in code without paying for full-file rereads
- Structure-aware compression becomes a reusable primitive across read-cache, archives, and future summarization work

### Current Status (2026-04-02)

Implemented locally in a conservative v1 shape:

- Python-only structure-aware reread substitution inside `read_cache.py`
- Supported Claude `PreToolUse` hook contract using `permissionDecision` + `additionalContext`
- One-time structure map injection with repeat degradation:
  - full structure map
  - reminder
  - reason-only
- Local savings tracking category: `structure_map`
- Offline proof runner: `structure_replay.py`
- `measure.py structure-proof` wrapper

Still intentionally deferred:

- JavaScript / TypeScript substitution
- symbol-targeted focused views
- archive-preview integration
- quality-score integration
- broader heuristic tuning from real-world samples

## Roadmap Reality Check

This section exists so future work does not confuse "written down" with "shipped."

### Already Shipped (Broadly)

- Smart compaction and checkpoint capture/restore
- Quality scoring and degradation tracking
- Trends, dashboard, and coaching flows
- Tool result archiving with `expand`
- Read-cache with redundant read detection
- `.contextignore`
- Git-aware context suggestions
- Attention scoring / optimization

### Partially Shipped / Early Form Exists

- Cross-session intelligence exists in pieces through trends and coaching, but not yet as deeply personalized recommendations
- Tool result compression exists as archiving and retrieval, but not yet as true preemptive summarization
- Agent efficiency is measured, but agent-team-specific state protection is not fully productized
- Attention optimization exists, but richer visual heatmaps and stronger before/after storytelling are still incomplete

### Still Not Fully Implemented

- Personalized recommendations based on long-term usage history
- Project-to-project benchmarks and stronger self-comparison over time
- Weekly/monthly digest ("Spotify Wrapped for Claude Code")
- Lightweight summarization before large tool results bloat context
- Agent Teams-specific continuity handling
- Background quality guard with proactive nudges
- Structure-aware compression / code map layer

### Strategic Direction

The next major step is:

**Move from finding waste to replacing waste with better compressed context.**

That means the product should increasingly answer:

- not just "where did your tokens go?"
- but "what smaller thing could the model have used instead?"

## Design Principles

All features follow these constraints:

- **Plugin-native**: No external dependencies beyond Python 3.8 stdlib. No MCP servers, no databases (except SQLite for trends), no shell scripts as primary mechanism.
- **Cross-platform**: All hook logic in Python. Works on macOS, Linux, Windows.
- **Composable**: Hooks run alongside any existing user hooks without conflict.
- **Transparent**: All data stays local. No API calls. Checkpoints are plain markdown (readable, editable).
- **Non-destructive**: Always backup before changes. Archive, never delete. User approves every modification.
- **Configurable**: All thresholds via environment variables. Sensible defaults that work for most users.

---

## Internal Cleanup Backlog (captured 2026-04-05)

Source: rejected drive-by PRs #10 and #11 from @mangodxd (LLM-automated spam campaign, 56 PRs in one day across unrelated repos). The underlying ideas are legitimate code-quality improvements worth doing ourselves, sweeping the whole fleet-auditor properly instead of accepting a partial cleanup with errors.

### Encoding sweep — `encoding="utf-8"` on all `open()` calls
**fleet-auditor is already complete** per Kieran's full call-site walk (2026-04-05):
- `fleet.py` has exactly 3 `open()` calls total: lines 85, 583 (both fixed by the rejected PR #10 approach) and line 1578 (already had encoding on main)
- `shared.py` open() calls already had encoding on main
- So a one-liner in fleet.py `pricing.json` loader + `settings.json` loader is the entire fix

**Match the existing project convention**: `shared.py:63` and `shared.py:199` already pair `encoding="utf-8"` with `errors="replace"`. That's the house style for text reads — it keeps the scanner robust against malformed UTF-8 from corrupted JSONL/log files instead of crashing. The rejected drive-by PR copied only half the pattern (encoding without errors). When we sweep, use the full pair at every read site:
```python
with open(path, "r", encoding="utf-8", errors="replace") as f:
```

Real sweep work lives elsewhere:
- `skills/token-optimizer/scripts/measure.py` — needs full audit
- Any other script that reads user-provided text (`scripts/structure_replay.py`, dashboard generators, archive tools)
- Matches the torture-room finding PL-F2 we addressed in repo-forensics PR #7

### Return type annotations — proper hints, not blanket `-> None`
PR #11 blanket-added `-> None` to 8 functions, including `iter_jsonl` which is a generator (`yield json.loads(line)` at `shared.py:64`). Correct version:
- `cmd_detect, cmd_scan, cmd_audit, cmd_report, cmd_dashboard` → `-> None` (void commands) ✓ PR had this right
- `fleet_cli(args: list[str] | None = None)` → `-> None` (void) ✓ PR had this right
- `migrate_add_columns` → `-> None` ✓ PR had this right
- `iter_jsonl(filepath: Path)` → `-> Iterator[dict[str, Any]]` (NOT `-> None`, it's a generator at `shared.py:57` with `yield json.loads(line)` at line 66, and the caller at `fleet.py:436` iterates the result). Requires adding `from collections.abc import Iterator` to `shared.py` imports. ✗ The rejected PR was actively wrong here, not just cosmetic.
- Reference pattern: `skills/token-optimizer/scripts/structure_replay.py:271` already has a correctly-typed `_iter_jsonl_records` — model the fix after that.
- While we're at it, sweep all public functions in `fleet.py` and `shared.py` for missing return type hints and add them (`detect`, `_serve_dashboard`, `parse_config`, `_load_pricing` already has one, etc.)

### Mypy CI check (optional follow-up)
If we're going to bother with type hints at all, add a mypy CI step that runs against `skills/fleet-auditor/scripts/` with strict-ish settings. That would have caught the `iter_jsonl -> None` bug automatically and would prevent the next drive-by PR from landing the same error if we ever accept one by mistake.
