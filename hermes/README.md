# Token Optimizer for Hermes

**Beta (v0.1.0).** Per-turn token capture, context-quality scoring, proactive context nudges, before/after savings measurement, and a dashboard for [NousResearch Hermes](https://github.com/NousResearch/hermes-agent), the autonomous agent that runs in your terminal and messaging apps.

Native Python plugin. Reads Hermes's own `~/.hermes/state.db` read-only. Model-agnostic. No telemetry. No Python dependency conflicts.

## What It Does

- **Per-turn usage capture** via `post_api_request`: accumulates `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, and `reasoning_tokens` into a thread-safe in-process tally for each session.
- **Proactive context nudge** via `pre_llm_call`: injects a one-line warning into the next turn when estimated context usage reaches ~70% of the model's window. Fires at most once per session to avoid spam; the gate never re-arms inside a session.
- **Session rollup** via `on_session_finalize` and `on_session_end`: when a session ends, the plugin reads the final sessions row from `state.db` and writes it into Token Optimizer's shared `trends.db`. A double-rollup guard ensures only one subprocess fires even when both hooks arrive for the same session.
- **`/token-optimizer` slash command**: shows a token and cost summary for the most recent Hermes sessions, available inside Hermes at any time.
- **`hermes token-optimizer` CLI subcommand**: opens the dashboard at `http://localhost:24844`.
- **Dashboard**: the same shared Token Optimizer dashboard (Overview, Quality, Waste, Sessions, Daily tabs) served from the shared engine, populated with Hermes session data.

## Install

Clone the repo and run the bundled installer:

```bash
git clone https://github.com/alexgreensh/token-optimizer.git
token-optimizer/install.sh --hermes
```

Preview without writing anything: `token-optimizer/install.sh --hermes --dry-run`. (Direct equivalent if you prefer: `python3 token-optimizer/skills/token-optimizer/scripts/measure.py hermes-install`.)

The installer copies the plugin payload (manifest plus the runtime modules it needs) into `~/.hermes/plugins/token-optimizer/`. Hermes discovers plugins from that directory, but user plugins must also be allow-listed in your Hermes config before they activate:

```yaml
plugins:
  enabled:
    - token-optimizer
```

Add that to `~/.hermes/config.yaml`, or let the installer do it for you with `--enable`:

```bash
python3 token-optimizer/skills/token-optimizer/scripts/measure.py hermes-install --enable
```

`--enable` is idempotent and backs up your config before touching it. If your config layout is unusual, the installer leaves it alone and prints the exact snippet to add manually.

The install is idempotent: re-running replaces files in place without touching other Hermes plugins or state.

**HERMES_HOME override**: if your Hermes home is not at `~/.hermes`, set `HERMES_HOME=/path/to/.hermes` before installing. The installer validates that the path is a real directory under your home before accepting it.

**Verify the install:**

```bash
python3 token-optimizer/skills/token-optimizer/scripts/measure.py hermes-doctor
```

The doctor checks: HERMES_HOME resolution, plugin directory presence, required files (manifest plus all runtime modules), declared hooks, a bridge smoke test (can the plugin reach `measure.py`?), the `plugins.enabled` activation entry, `state.db` readability, and dashboard port availability.

**Uninstall:**

```bash
token-optimizer/install.sh --hermes --uninstall
```

After uninstalling, remove `- token-optimizer` from `plugins.enabled` in your Hermes config so Hermes does not log a missing-plugin warning on the next start.

## Usage Inside Hermes

Once the plugin is installed and allow-listed in `plugins.enabled`, the hooks activate automatically on the next Hermes start. No further configuration is required.

| Surface | How to use |
|---------|-----------|
| `/token-optimizer` | Slash command inside any Hermes session. Prints a token and cost summary for recent sessions. |
| `hermes token-optimizer` | CLI subcommand run from your shell. Opens the dashboard at `http://localhost:24844`. Pass `--port <n>` to override the port. |
| Dashboard | Visit `http://localhost:24844` after launching via the CLI subcommand, or open it from the main Token Optimizer dashboard if you run Claude Code or Codex alongside Hermes. |

## The Context Nudge

Before each turn, the `pre_llm_call` hook checks live context usage. Hermes owns compression, so Token Optimizer stays silent while the native compressor is enabled and healthy; it warns only when compression is disabled, failed, or ineffective. If the estimated fill reaches 70% (the threshold is inclusive), the plugin appends a one-line notice to the user message:

```
[Token Optimizer] Context ~73% full (~146,000 input tokens vs assumed 200,000 window) Grade: C. Avoid adding large files; prefer targeted reads.
```

At 85% or above, the tip escalates to suggest `/compact`.

**Important caveats:** Hermes does not expose the live context window size to plugins, so the fill percentage is an estimate against an assumed window (200,000 tokens by default, or the mapped window for known models). The displayed percentage is capped at 100 to avoid absurd figures on large-window models. The nudge fires at most once per session, and the gate never re-arms within that session: once it has fired, a later drop back under the threshold and a second crossing stay silent. It never raises an exception into the Hermes host.

## Context-Quality Score

Each session is scored on a 0-100 scale using the signals available in Hermes's session rows.

### Active signals

| Signal | Weight | What it measures |
|--------|--------|-----------------|
| Context usage | 40% | Latest provider-reported prompt tokens divided by the mapped model context window. Omitted when live prompt usage is unavailable; cumulative session totals are never used as occupancy. |
| Message count risk | 35% | Session length relative to a risk curve (<=20 messages scores 100; >100 scores 10). |
| Output / input ratio | 25% | Productivity signal: output tokens divided by input tokens. Low ratios indicate context-heavy sessions producing little output. |

### Omitted signals (and why)

| Signal | Why omitted |
|--------|------------|
| Cache hit rate | `cache_read_tokens` is present in the Hermes schema but documented as unreliable; wiring it into the score would introduce noise rather than signal. It is included as informational metadata but not weighted. |
| Compaction events | The sessions row carries no successful-compaction count. It does carry compression-health fields (failure cooldown, fallback streak, ineffective count, recovery deadline), which the plugin uses for compression-health gating, not for the weighted score. |
| API calls per turn | `api_call_count` is available but not directly comparable to Claude Code's turn-based counts. It is carried as metadata but excluded from the weighted score. |

Grades: **S** (90-100), **A** (80-89), **B** (70-79), **C** (55-69), **D** (40-54), **F** (0-39). Thresholds are the same across all Token Optimizer platforms.

## Savings

Token Optimizer estimates "before" vs "after" savings by comparing session cost trajectories in `trends.db`. The Hermes adapter feeds sessions into the same shared engine used for Claude Code and Codex sessions.

**What "savings" means here:** the before/after engine compares your average cost-per-session before and after you began using Token Optimizer. It is a correlation of cost trends, not a per-action guarantee that any individual nudge saved money.

**Model pricing:** each session is priced at its real model (Claude, GPT-4.1, Gemini, Nous Portal, OpenRouter). Hermes pre-computes `estimated_cost_usd` in its sessions table; the adapter uses that directly. If the column is NULL or `cost_status` is `"unknown"`, the adapter falls back to Token Optimizer's own pricing engine using the stored token counts. Free or local models are proxy-priced at the runtime default rate rather than zero so they appear in the cost signal.

**Conservative by design:** two imprecisions in the current adapter both understate savings rather than inflate them:

1. `cache_write_tokens` is rolled into the `input_tokens` total rather than split into its cheaper cache-write tier, making the "after" cost slightly higher than the true billed amount.
2. If Hermes under-reports `cache_read_tokens`, the inferred cache hit rate is lower, fresh input is higher, and the "after" cost again comes out slightly high.

A smaller "after" cost means a smaller savings number. The math leans conservative.

## Privacy and Safety

- `hermes_state.py` opens `~/.hermes/state.db` with the `file:...?mode=ro&immutable=1` URI and sets `PRAGMA query_only = ON` as defence-in-depth. It never writes to the file and never opens a long-lived connection.
- The plugin runs in-process inside Hermes. All hooks are wrapped in `try/except`; no exception is ever raised into the Hermes host.
- No data is sent to any external service. No telemetry. No network calls.
- `trends.db` (Token Optimizer's own SQLite database, separate from Hermes's `state.db`) is written by the rollup subprocess, not by the plugin's hooks directly.

## Model Support

The plugin is model-agnostic. Hermes routes sessions to whichever model the user configures (Nous Portal, OpenRouter, Anthropic, OpenAI, Gemini, local). Context window sizes are mapped for common Claude, GPT-4.1, and Gemini model IDs; unrecognised models fall back to a conservative 200,000-token assumed window.

## Requirements

- Python 3.x
- A working Hermes install (the plugin copies into Hermes's plugin directory and relies on Hermes to load and invoke it)
- The Token Optimizer repo cloned locally (the installer reads from `hermes/` in the repo root)

**Beta status:** the plugin has been through full internal testing but has not yet been validated against a live Hermes install. Live smoke-testing is the remaining gate before a stable release. Expect rough edges; file issues on the [Token Optimizer repo](https://github.com/alexgreensh/token-optimizer).

## How It Works

```
Hermes turn lifecycle
    pre_llm_call  ──► nudge check (fill > 70%?)  ──► inject {"context": "..."} or None
    post_api_request  ──► accumulate usage into in-process tally (input/output/cache)
    on_session_finalize / on_session_end
        ──► hermes_hook_bridge.run_rollup()
            ──► measure.py hermes-rollup
                ──► hermes_state.recent_sessions()  (reads state.db read-only)
                ──► hermes_session.normalize_session()  (token rollup, cost, quality score)
                ──► INSERT OR IGNORE into trends.db
                ──► _rebuild_aggregate_tables()  (daily_stats, model_daily)

/token-optimizer command
    ──► hermes_hook_bridge.run_summary()
        ──► measure.py hermes-summary  ──► usage_trends()

hermes token-optimizer CLI
    ──► hermes_hook_bridge.run_dashboard(port=24844)
        ──► dashboard served at http://localhost:24844
```

Session data flows one way: from Hermes's `state.db` into Token Optimizer's `trends.db`. The plugin never writes back to `state.db`.

For the full Token Optimizer feature set, quality signal details, waste pattern detection, and the shared savings methodology, see the [main README](../README.md).

## License

PolyForm Noncommercial 1.0.0
