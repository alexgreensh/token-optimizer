# Token Optimizer for VS Code

See your Claude Code **context health, efficiency, and usage limits** right in the
VS Code status bar — no integrated terminal required.

Most people who live in the Claude Code extension panel never see the terminal status
line, so they fly blind on context pressure and usage limits. This companion brings the
full Token Optimizer status line into VS Code's own UI.

## What it shows

Two status-bar items, plus a rich hover tooltip:

- **Context fill %** with a bar
- **ContextQ** — resource health grade + score
- **Eff** — session efficiency grade + score
- **Warnings** — fill / tool-fatigue / regime-change
- **Compactions** + measured recovery status (never a fixed loss estimate)
- **Session duration** and **active subagents**
- **5-hour and 7-day usage limits** with reset times

Click either item to open the Token Optimizer dashboard.

## How it gets the data

It reads the files Token Optimizer already writes to `~/.claude/token-optimizer/`
(quality scores from the `UserPromptSubmit` hook, context fill and rate limits from the
status line) and tails the session transcript as a fallback. Pure file-watching, zero
network — with one optional exception below.

When you click to open the dashboard, the extension first probes a local Token Optimizer
daemon at `http://localhost:24842` and falls back to opening the generated HTML file. That
localhost probe is the only non-OAuth network activity, and it fires only on an explicit click.

## Usage Limits

Usage limits are exact when fresh Claude statusline data is available. If not, Token
Optimizer falls back to a clearly labeled local estimate or the last captured value:

- **verified** — captured from Claude Code's statusline cache
- **estimated** — computed locally from recent transcript usage
- **cached** — last captured value, with update age shown

Use **Token Optimizer: Refresh Now** or the panel's **Refresh** button to re-read the
cache and recompute the local transcript estimate. No terminal workflow required.

## Install

**From VSIX (GitHub release):**

```
code --install-extension token-optimizer-statusline-<version>.vsix
```

or in VS Code: Extensions → `...` → **Install from VSIX**.

Works in VS Code, Cursor, and Windsurf.

## GitHub Copilot mode

If you run Token Optimizer with GitHub Copilot instead of (or alongside) Claude Code, set:

```json
"tokenOptimizer.runtime": "copilot"
```

The extension will read cache files from `~/.copilot/token-optimizer/` and resolve the
active session from `~/.copilot/session-state/<id>/events.jsonl` (most-recently-modified
directory wins). All status-bar items, quality scores, and usage-limit display work the
same way — only the source directory changes.

The setting takes effect immediately without reloading VS Code.

| Setting | Values | Default |
|---|---|---|
| `tokenOptimizer.runtime` | `"claude"` · `"copilot"` · `"cursor"` | `"auto"` |

## Cursor mode

If you run Token Optimizer's Cursor hooks, set:

```json
"tokenOptimizer.runtime": "cursor"
```

The extension reads the per-conversation hook tallies from
`~/.cursor/token-optimizer/sessions/` and resolves the active session by the tally's
`updated_at`, scoped to this window's workspace folder when one is open (a tally whose
`cwd` or `workspace_roots` matches wins over a globally newer one).

What Cursor data can and cannot show today:

- **Context fill** comes from the last `preCompact` occupancy snapshot the hooks recorded
  (labeled _preCompact_ in the tooltip and panel) — never from a lifetime token sum.
- **ContextQ** on Cursor is derived from that fill alone; the full multi-signal quality
  score is Claude-only for now. The tooltip says so.
- **Compactions, session duration, and tool-call pressure** come from the tally.
- **Usage limits, efficiency, and cost are absent on Cursor**: Cursor exposes no
  per-session limit or billing data, and Token Optimizer does not invent either.

A separate, strictly opt-in Cursor CLI `statusLine` command
(`skills/token-optimizer/scripts/cursor_statusline.py`) renders the same grade/fill in the
Cursor CLI footer. It **replaces the native footer** (Cursor offers no append mode), so the
installer does not wire it: enabling it is a manual, deliberate step. To opt in, add the
`statusLine` command to your Cursor CLI config pointing at that script; remove the key to
restore the native footer.

The setting takes effect immediately without reloading VS Code.

## Requires

[Token Optimizer](https://github.com/alexgreensh/token-optimizer) installed in Claude Code
(it writes the data this extension reads).

For Copilot mode, a GitHub Copilot Token Optimizer installation at `~/.copilot/token-optimizer/`
is required.

For Cursor mode, a Cursor Token Optimizer installation at `~/.cursor/token-optimizer/`
is required. Auto mode picks whichever runtime you used most recently.
