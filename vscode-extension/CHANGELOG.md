# Changelog

## Unreleased

- Fix: the context fill bar no longer reads about double on turns that called the advisor tool. It now uses the real context size, the same number Claude Code shows (#287).

## 0.1.16

- Fix: honor `CLAUDE_CONFIG_DIR`. With a relocated Claude Code config, the companion now reads live fill, usage limits, quality scores, sessions, and effort from that directory instead of a stale `~/.claude` (#198).
- Package: builds now start from a clean `out/`, so compiled files from removed features can no longer ship. 0.1.15 carried two such leftovers (never loaded by the extension); 0.1.16 drops them.

## 0.1.15

- Improve: the install funnel's savings block is now a per-usage tier ladder (Light ~$150/mo, Medium ~$600/mo, Power up to ~$1,900/mo) instead of a single averaged range, so a user can find their own row. The Power row is metered on real 30-day data; lighter rows scale that per-session saving by session volume. Framing stays API-equivalent with the rate-limit-headroom caveat for subscription users.

## 0.1.14

- Add: install funnel. When the extension is running but the Token Optimizer CLI plugin has never written data on this machine, the status bar shows "Save tokens" and the panel presents the three levels of optimization, the savings stat, and one-click platform-specific install (Copy Claude Code command / all-platforms docs) instead of a bare empty state.
- Improve: the empty state now distinguishes "plugin installed but idle" (waiting for a session) from "plugin not installed" (the funnel), based on the presence of a `~/.claude` or `~/.copilot` token-optimizer cache dir. A transient read error never flips an installed user into the funnel.

## 0.1.12

- Add: GitHub Copilot runtime support. Set `tokenOptimizer.runtime` to `copilot` to point the status bar at a Copilot Token Optimizer install (`~/.copilot/token-optimizer/`).
- Improve: data source and session resolver are runtime-aware; switching runtimes rebuilds the data source live.
- Fix: surface read errors to the console instead of silently showing an empty status bar.

## 0.1.11

- Fix: remove the long usage-limit disclaimer from the hover tooltip and panel.
- Improve: usage labels now stay attached to the actual 5h/7d values, with shorter source details.

## 0.1.10

- Fix: replace user-facing "stale" usage-limit wording with clearer cached, estimated, and verified states.
- Improve: manual refresh now uses local, non-terminal paths first and recalculates transcript-based estimates when exact statusline data is not available.
- Package: align extension lockfile metadata with the shipped VSIX version.

## 0.1.8

- Hardening pass: runtime-allowlist the panel's actions, dedupe fill clamping, and make warnings array-based (no join/re-split). No behavior change.

## 0.1.7

- Improve: brighter, higher-contrast grey for secondary info (session time, labels, reset times) in the panel and terminal status line — the old faint grey was hard to read.

## 0.1.6

- Remove: the "Regime" indicator (obscure context-fill-shift signal that confused more than it helped). The underlying detection stays in the plugin; it's just off the status line now.

## 0.1.5

- Fix: context fill is now computed from the transcript's own token count divided by the real context window (from the quality-cache's `model_context_window`). This is the only source that's correct for both 200k and 1M-context sessions, and it ignores a 0% fill the plugin sometimes writes when it can't attribute fill.

## 0.1.4

- Fix: context fill is now read from the per-session quality-cache's authoritative `fill_pct` (computed against the real context window), fixing badly-wrong fill on 1M-context sessions. Priority: matched live-fill → quality-cache → transcript tail.
- Honest: with no folder open, the status bar can't scope to this window's session, so it now says so plainly (in tooltip and panel) instead of showing volatile global data.
- Note: VS Code shows one status bar per window, so multiple Claude tabs in the same window reflect the most-recently-active session — a platform limitation, now documented.

## 0.1.3

- New: clicking the status bar opens an **expanded status panel** — a focused, theme-matched view of context fill, ContextQ, Eff, warnings, compactions, duration, agents, and 5h/7d limits, live-updating. The full browser dashboard is now an explicit "Open full dashboard" button.

## 0.1.2

- Fix: context fill is now read per-session. The shared `live-fill.json` was leaking one session's fill into other windows; the status bar now uses it only when it matches this window's session, falling back to the transcript otherwise.
- Improve: the 7-day usage limit now shows the reset **date and days remaining** (e.g. "Jun 4, 8:00pm · in 4d"), not just a time-of-day.
- Improve: a fresh session with no quality scores yet shows "warming up…" instead of an empty gap.

## 0.1.1

- Fix: the status bar now reflects the Claude Code session for **this** window (scoped to the workspace folder), instead of whichever session was most recently active anywhere. Resolves wrong scores showing in one window when another session is running.

## 0.1.0

- Initial release: Token Optimizer status line in the VS Code status bar.
- Two status-bar items: context fill % + ContextQ, and Eff + 5h usage.
- Rich hover tooltip with the full parity view (warnings, compactions, duration, agents, 5h/7d limits).
- Click to open the Token Optimizer dashboard.
- Optional, off-by-default Live Usage (OAuth) for always-fresh, zero-token usage limits.
- Works in VS Code, Cursor, and Windsurf.
