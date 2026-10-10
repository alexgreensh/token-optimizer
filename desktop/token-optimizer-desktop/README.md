# Token Optimizer Desktop

A status bar for the Claude desktop app. It draws above your prompt and shows session quality, context fill, cache countdown, and your 5-hour and weekly limits, with Clawd acting out what the session is doing.

## Install

It ships inside the Token Optimizer plugin (`hooks/hooks.json` names this folder's `hooks/register.tsx` under `modules`), so there is nothing extra to install. This folder keeps its own manifest only so its tests run on their own with `claude plugin test`.

Needs Claude Code 2.1.287 or newer and Anthropic's mods feature. Anthropic can switch mods off remotely; when off, nothing breaks and the bar just does not appear. The terminal keeps its existing status line. VS Code is not supported.

## What you get

- One sentence about the most urgent thing, with at most one button.
- Five marks with hover cards: quality grade, context fill, cache countdown, 5-hour limit, weekly limit.
- The **arrow beside Clawd** unfolds one more line: branch, session time, tool calls, compactions (when any), when the last checkpoint was saved (any kind, or a relevant one from an earlier session), and the tokens Token Optimizer saved you in the last 30 days, the same total the dashboard shows.
- Buttons: **Clean up** (compacts with Token Optimizer's guidance), **Start fresh** (second click saves a checkpoint, clears, and hands it to your first message), **Keep warm** (manual one-click cache refresh, only while the cache is warm, never automatic, uses a small amount of usage).

The cache countdown is an estimate: 1 hour on Claude plans, 5 minutes on the API, measured from the session itself when possible.

## Settings

`TOKEN_OPTIMIZER_STATUS_BAR=0` hides the bar and `TOKEN_OPTIMIZER_STATUS_BAR_ANIMATE=0` keeps Clawd still, set in the `env` block of `~/.claude/settings.json`.

The `–` button beside the arrow shrinks the band to one line and `+` brings it back; the arrow still unfolds the details row under it, and the choice is remembered across sessions and restarts. `TOKEN_OPTIMIZER_STATUS_BAR_SIZE=slim` sets the starting size before a choice is stored, and the terminal status line uses the same variable to print one line.

The context mark measures against your own compact window when you set one: `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, `/autocompact`, or the `autoCompactWindow` setting, within Claude Code's 100K-1M bounds and never above the model's window. A window given only with `--autocompact` for one launch is not visible to it.

## Full docs

See the [Token Optimizer README](../../README.md#desktop-status-bar) and the docs site page "Claude desktop app".
