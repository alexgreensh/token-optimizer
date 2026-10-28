---
title: Claude Code mods for the desktop status bar, verified behaviour
category: integrations
component: token-optimizer-desktop
runtime: claude-code
severity: medium
verified: true
---

# Claude Code mods for the desktop status bar: verified behaviour

Observed on Claude Code 2.1.288 (CLI, Claude Max, Haiku 4.5), 2026-10-03, with a throwaway mod loaded through `--plugin-dir`. Desktop-only behaviour (animation paint, Client modules, hover cards, reduced motion) still needs a desktop check.

## Problem

The mod API declarations leave several behaviours the status bar depends on unstated: whether a fork warms the main cache, whether a plugin may run `/clear`, which events fire around compaction and clear, and what usage figures a mod sees.

## What we observed

- **`$.model.fork` reads the whole cached conversation and adds no message rows.** The fork's `cache_read_input_tokens` (42,887) covered the main thread's last request (42,240 read plus 584 written), with zero cache writes. The transcript gained only the command's own system rows. A fork is therefore a valid Keep warm: a cache read refreshes that entry's lifetime.
- **`turn.complete.usage` is summed over every request in the turn.** One turn with a tool call reported 67,607 read and 17,457 written while each request carried about 42,000. Anything that needs the context size or the cache anchor must use per-request usage (the transcript's assistant rows, or `turn.step` stop chunks), never the turn sum.
- **The cache lifetime is visible in the transcript.** Assistant rows carry `cache_creation.ephemeral_1h_input_tokens` / `ephemeral_5m_input_tokens`. On Claude Max every write was one-hour. `turn.complete.usage` does not carry this split.
- **`$.session.compact()` is refused in a headless session, and the desktop app runs its sessions headless** ("not available in a headless (-p / SDK) session yet"). `$.command.run({ command: 'compact' })` works in both (scheduled with `$.clock.after`, outside the calling hook): it is the same as typing `/compact`, fires the `session.compact` event with trigger `manual`, and Token Optimizer's classic `PreCompact` saves its checkpoint. A plugin's `$.prompt.submit` of a text starting with `/` is refused.
- **Claude Code writes the `compact_boundary` transcript row after the PostCompact hooks run.** A PostCompact refresh that counts compactions from the transcript sees one too few; it has to count the compaction itself.
- **`$.command.run({ command: 'clear' })` is allowed for a plugin** when run outside the calling hook. It fires `session.end` with reason `clear`, then the classic `SessionStart` with source `clear`, whose `additionalContext` carries Token Optimizer's "Cross-session checkpoint" pointer. No `session.start` fires afterwards.
- **`$.session.usage()`** returns `{ startedAt, context: { tokens, window, percent }, rateLimits: [{ kind: 'five_hour' | 'seven_day', percentUsed, resetsAt }], cost }`.
- **Validator rule:** `$` may be passed only to functions declared at the top of the module (a function declaration or a const bound to one). Helpers defined inside `register` that take `$` fail `claude plugin validate`.
- **One hooks module per plugin, and `$` never crosses an import.** A two-entry `modules` list is refused. `$.env.get` takes only literal variable names, and `atom()` needs a literal `{ plugin, key }` in the hooks module itself. Shared logic therefore takes a small adapter built from `$` inside the hooks module, which also makes it testable under plain Node.
- **`PluginState` is keyed by plugin, then by atom key** (`PluginState['token-optimizer-desktop'].session`).
- **Plugin tests:** `claude plugin test` runs every `*.test.ts(x)` inside the engine, where `node:test` cannot be imported. Pure Node tests therefore use `*.spec.ts`.
- **Rollout switch:** a stale "switched off" state cached by an earlier session blocks `claude plugin test` until any `claude` run refreshes it with network access.

## Observed on the desktop app (2026-10-03)

- **Every band redraw restarts a picture's SMIL animation, and an `isInteractive` Svg blanks for a frame on each redraw.** A plain `Svg` keeps animating (SMIL plays in it) and does not blank. Keep redraws rare: at most once a minute, never per second, and only when something shown changed.
- **A hover group (`hover.scope`) on an ancestor of a picture rebuilds that picture on every redraw.** Hover-revealing one part of the band from another part through a shared scope did not work on the desktop; revealing a hidden Box inside the hovered keyed Box does. The engine reports no pointer position to a mod.
- **`Box` sizes and offsets count text cells, not pixels.** Size a stack of pictures by its first, in-flow picture; place later ones absolutely over it.
- **Colours with alpha (`rgba()`) in an Svg did not render;** use solid colours. Svg `<style>` with `@media (prefers-color-scheme: dark)` lets a picture follow the app's own appearance; the config's `theme` row is the terminal's theme, not the app's.
- **`$.session.usage()` can omit a rate limit for one refresh** (seen right after a compact). Keep the last known value.
- **Only a `Button` can be pressed, and its label is text:** a picture cannot be made clickable.
- **Hot reload (`CLAUDE_CODE_PLUGIN_DIR_WATCH`) prints a "reloaded" line into the session per reload;** installed plugins do not.

## Managed machines: the built-in guard skips `classic.*` handlers (2026-10-27, #281)

- **On a machine with managed settings, or a Team or Enterprise login, Claude Code seats a built-in guard, `cc-plugin-sec-default`, ahead of installed plugins.** Its public source passes every `classic.*` event on past a person's plugin (`on('classic.*', ($, e, next) => next.to(e, 'append'))`), and Claude Code shows a notice once per session and event: `its <event> hook was skipped. "cc-plugin-sec-default" runs first and sent the event on past it.`
- **A plugin cannot move ahead of the guard or register hooks only when it is absent,** because hooks register while the module loads. The only lever is registering fewer `classic.*` handlers.
- **Keep only `classic.*` handlers that carry a signal nothing else gives.** The status bar keeps `classic.SessionStart` (the only source of the transcript path and the /clear reset) and `classic.PermissionRequest`. `classic.UserPromptSubmit` and `classic.Stop` were dropped in 5.13.59: without a transcript path, the status and checkpoint commands look the session up by id. A source test pins the registered set.
- Not yet seen live on a guarded machine; the behaviour comes from the guard's source and `claude plugin validate`'s list of registered hooks.
