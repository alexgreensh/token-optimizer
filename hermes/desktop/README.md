# Experimental Hermes Desktop bar

A desktop composer extension for the Hermes integration. It shows a context-quality score, context occupancy, Hermes's prompt-cache clock, cache-hit rate and optional subscription-window data; it includes a Hermes mascot that animates while the focused session is busy and a dashboard link. This is an **experimental** port, not the Claude Desktop plugin.

Each metric is an accessible, compact chip that opens a popover with its explanation; the quality chip contains **Clean up**, and the `⋯` menu contains session details and the dashboard link. The single-row strip scrolls horizontally on narrow windows.

When the focused session's reported cache window is cold, the cache chip explains the expected re-read and offers **Clean up first**. The context count is approximate, savings depend on the provider, and no keep-warm request is sent automatically.

The details menu shows session age, Git branch, tool calls, and the runtime's compaction count when available. **Clean up** requires a second click to confirm and invokes Hermes's native session compression, which may use model quota and summarize away details. It refuses if the focused session, profile, gateway route or idle state changes before confirmation. **Start fresh**, checkpoint recovery and manual cache-warming are not implemented.

## Installation (manual; not wired into `install.sh`)

Requires Hermes Desktop with the plugin SDK `COMPOSER_AREAS.top`, `useQuery`, `focusedUsage`, `focusedSessionProfile` and `session.context_breakdown`. The Token Optimizer Hermes plugin and its engine scripts must already be installed. These commands target the current `HERMES_HOME`, defaulting to `~/.hermes`:

```sh
home="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$home/desktop-plugins/token-optimizer-bar" "$home/plugins/token-optimizer-bar/dashboard"
cp desktop-plugins/token-optimizer-bar/plugin.js "$home/desktop-plugins/token-optimizer-bar/"
cp plugins/token-optimizer-bar/plugin.yaml "$home/plugins/token-optimizer-bar/"
cp plugins/token-optimizer-bar/dashboard/{manifest.json,plugin_api.py} "$home/plugins/token-optimizer-bar/dashboard/"
hermes plugins enable token-optimizer-bar --no-allow-tool-override
```

Run those commands from this directory. Reload desktop plugins in the Desktop command palette. Gateway API discovery depends on the Hermes version; a running backend may need a rescan or restart. Enabling the backend does not guarantee the renderer has loaded it.

## Data and limitations

- Quality uses Token Optimizer's existing `session_quality` function and the context reported by the focused session. Unavailable data displays `—`.
- The cache clock reads Hermes's `session.context_breakdown` result. It distinguishes proven/declared, estimated and uncertain windows; it does **not** send manual or automatic keep-warm pings. Cache hit percentage is separate from the clock.
- Optional account windows come from `$HERMES_HOME/limit-watches/account-limits.json`, a **third-party local collector not shipped by this project**. Without it, or if data is over 15 minutes old, the limits display `—`. The endpoint exposes only percentages, reset/check timestamps, freshness and a recognized provider, not credentials, emails, or raw collector records. They are account/subscription windows, **not per-model quotas**. Auto-selection requires an exact session/model/provider match in collector activity; the selector also allows manual choice.
- The Dashboard button opens `http://127.0.0.1:24844/` on the Desktop client and assumes the Token Optimizer dashboard is already running on that same machine. It does not start the dashboard and is not suitable for a remote backend.
- Multi-profile and remote deployment behavior has not been end-to-end tested. The current Hermes context-breakdown request relies on the host's focused session routing; validate before use across profiles.
- The Hermes animation frames originate from `apps/desktop/public/hermes-frames` in NousResearch/hermes-agent (commit `51c68d4ab1a9e3c62fb1048fccb84144c409f0e7`). Their MIT license and attribution are reproduced in `HERMES-ASSET-LICENSE.txt`.
