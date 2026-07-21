# Token Optimizer for OpenCode

Context quality scoring, smart compaction, and session continuity for [OpenCode](https://github.com/anomalyco/opencode). Full parity with the Claude Code Token Optimizer plugin.

## What It Does

Token Optimizer monitors your OpenCode sessions and helps you get the most out of your context window:

- **7-signal quality scoring** with dual ResourceHealth (monotonic) + SessionEfficiency (rolling window) architecture
- **Smart compaction** with mode-aware PRESERVE/DROP guidance (code/debug/review/infra/general)
- **Session continuity** that restores context from prior sessions via keyword matching
- **Quality nudges** that warn when context health drops, fill exceeds thresholds, or retry loops are detected
- **Dashboard** with quality trends, session history, and daily aggregates
- **`token_status` tool** for on-demand quality reports
- **`token_dashboard` tool** to generate and open the visual dashboard

## Install

Add the plugin to your `opencode.json` (or `.opencode/opencode.jsonc`) `plugin`
array. OpenCode resolves and installs it from npm on the next launch:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["token-optimizer-opencode"]
}
```

The `plugin` array holds npm package names as **plain strings** — that is
OpenCode's config schema. No separate install command is needed.

## Where your data goes

By default, nothing is written into your project. Data (session history and
`trends.db`) lives in a per-user location outside any repo:

| OS      | Default location                              |
| ------- | --------------------------------------------- |
| macOS   | `~/Library/Application Support/token-optimizer/` |
| Linux   | `~/.local/share/token-optimizer/` (or `$XDG_DATA_HOME`) |
| Windows | `%LOCALAPPDATA%\token-optimizer\`             |

### Put it somewhere else

Set `dataDir` to the **exact folder** you want. What you type is where data
goes — the full path, including the folder name.

To pass options, OpenCode uses a `[package-name, options]` pair in the `plugin`
array (in place of the plain string):

```jsonc
// opencode.json
{
  "plugin": [
    ["token-optimizer-opencode", { "dataDir": ".opencode/token-optimizer" }]
  ]
}
```

Want a hidden folder in your repo? Type it literally:

```jsonc
{ "plugin": [["token-optimizer-opencode", { "dataDir": ".token-optimizer" }]] }
```

Prefer an environment variable? `TOKEN_OPTIMIZER_DATA_DIR` does the same thing,
needs no tuple, and takes precedence when both are set:

```bash
export TOKEN_OPTIMIZER_DATA_DIR=~/.token-optimizer
```

Existing project-local data from older versions is copied to the new location
automatically on first run; the originals are left in place so you can delete
them yourself once your history looks right.

### Offline / no-npm install

If you can't (or don't want to) install from npm, clone the repo and run the
bundled installer. It builds a single-file plugin and copies it into
`~/.config/opencode/plugins/`, which OpenCode auto-loads at startup:

```bash
git clone https://github.com/alexgreensh/token-optimizer.git
token-optimizer/install.sh --opencode
```

Requires [bun](https://bun.sh) (OpenCode's own runtime). Re-run after a
`git pull` to update.

## Uninstall

```bash
token-optimizer/install.sh --opencode --uninstall
```

Removes `~/.config/opencode/plugins/token-optimizer.js` (the bundle the
offline installer copied) and reverts the `token-optimizer-opencode` entry
from `opencode.json`'s `plugin` array if present. Other plugin entries are
left intact. Add `--dry-run` to preview what would be removed. Idempotent;
running it on a clean install is a no-op.

If you installed via the npm `plugin` array only (no offline bundle), just
remove `"token-optimizer-opencode"` from the `plugin` array in your
`opencode.json` (or `.opencode/opencode.jsonc`) and restart OpenCode.

The `~/.claude/skills/token-optimizer` tree is owned by the standard
installer (`bash install.sh`, no flag); it is NOT touched by
`--opencode --uninstall`. To remove it, follow the Claude Code uninstall
steps in the root [`README.md`](../README.md#uninstall).

OpenCode session/trends data is left in place by design. To purge it too:

```bash
rm -rf ~/.config/opencode/token-optimizer
```

## Update

The npm plugin (`token-optimizer-opencode`) is **runtime-only**: it ships the
hook bridge and tooling OpenCode loads at startup. The **skill content**
(SKILL.md, references, scripts) lives in `~/.claude/skills/token-optimizer/`,
which OpenCode loads directly. Updating the npm plugin does NOT refresh that
tree — they are independent channels.

To refresh the skill content (after a `git pull`, or if SKILL.md references
look stale / `[file not found]`), re-run the **standard** installer — the
Claude-side flow with no `--opencode` flag. It owns the `~/.claude/skills`
tree:

```bash
token-optimizer/install.sh
```

That flow verifies the skill payload is complete and repairs a partial
checkout in place. (`install.sh --opencode` only rebuilds the OpenCode runtime
bundle in `~/.config/opencode/plugins/` — it does not touch the skill tree.)

## Configure

OpenCode's `plugin` array takes **package-name strings only** — it does not
accept an inline options object, and a `["name", { … }]` tuple will fail config
validation and stop OpenCode from starting. Configure Token Optimizer through
environment variables instead (full list below):

```bash
# Example: widen the quality window and disable loop detection
export TOKEN_OPTIMIZER_QUALITY_WINDOW=30
export TOKEN_OPTIMIZER_LOOP_DETECTION=false
```

All settings are optional; defaults are shown in the table below.

## Environment Variables

Override any threshold via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `TOKEN_OPTIMIZER_QUALITY_WINDOW` | 20 | Rolling window size for ratio signals |
| `TOKEN_OPTIMIZER_TOOL_CALL_WARN` | auto | Tool call warning threshold (scales with context window) |
| `TOKEN_OPTIMIZER_TOOL_CALL_CRITICAL` | auto | Tool call critical threshold |
| `TOKEN_OPTIMIZER_CHECKPOINT_RETENTION_DAYS` | 7 | Days to keep checkpoints |
| `TOKEN_OPTIMIZER_CHECKPOINT_RETENTION_MAX` | 50 | Max checkpoints to scan for restore |
| `TOKEN_OPTIMIZER_RELEVANCE_THRESHOLD` | 0.3 | Min relevance score for checkpoint restore |
| `TOKEN_OPTIMIZER_NUDGES` | true | Enable quality nudges |
| `TOKEN_OPTIMIZER_LOOP_DETECTION` | true | Enable retry loop detection |
| `TOKEN_OPTIMIZER_SMART_COMPACTION` | true | Enable compaction context injection |
| `TOKEN_OPTIMIZER_CONTINUITY` | true | Enable session continuity |
| `TOKEN_OPTIMIZER_ACTIVITY` | true | Enable activity tracking |
| `TOKEN_OPTIMIZER_TRENDS` | true | Enable trends collection |

## Quality Scoring

The quality score uses a dual-composite architecture:

**ResourceHealth** (monotonic, can only decrease within a session):
- Context fill degradation (50%) - MRCR-curve-based quality estimate
- Compaction depth (30%) - information loss from repeated compaction
- Absolute waste tokens (20%) - stale reads + bloated results

**SessionEfficiency** (rolling window, can rise or fall):
- Stale reads (30%) - re-reading files after writing them
- Bloated results (30%) - large tool outputs never referenced
- Decision density (20%) - ratio of substantive messages
- Agent efficiency (20%) - agent dispatch result/prompt ratio

Grades: S (90+), A (80+), B (70+), C (55+), D (40+), F (<40)

## Hooks Used

| Hook | Purpose |
|------|---------|
| `chat.message` | Track user messages, trigger quality scoring |
| `tool.execute.before` | Record file reads |
| `tool.execute.after` | Record tool results, file writes, agent dispatches, activity tracking |
| `experimental.chat.system.transform` | Inject warnings, restore session continuity |
| `experimental.session.compacting` | Inject mode-aware compaction guidance, capture checkpoint |
| `experimental.compaction.autocontinue` | Reset signals post-compaction, refresh quality |
| `event` | Handle session lifecycle (created/deleted) |

## Model Support

Context window sizes are mapped for 30+ models across all major providers:
Anthropic (Opus/Sonnet 1M, Haiku 200K), OpenAI (GPT-5.x, GPT-4.1, o3/o4), Google (Gemini 2.x/3.x),
DeepSeek, Qwen, Mistral, xAI Grok, and more.

MRCR quality curves are calibrated per model family for accurate fill-degradation estimates.

## License

PolyForm Noncommercial 1.0.0
