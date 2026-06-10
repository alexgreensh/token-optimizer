<p align="center">
  <img src="skills/token-optimizer/assets/logo.svg" alt="Token Optimizer" width="780">
</p>

<p align="center">
  <a href="https://github.com/alexgreensh/token-optimizer/releases/latest"><img src="https://img.shields.io/github/v/release/alexgreensh/token-optimizer?label=version&color=green" alt="Latest stable version"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/releases"><img src="https://img.shields.io/github/release-date/alexgreensh/token-optimizer?label=last%20release&color=blue" alt="Last Release"></a>
  <a href="https://github.com/alexgreensh/token-optimizer"><img src="https://img.shields.io/badge/Claude_Code-Plugin-blueviolet" alt="Claude Code Plugin"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/tree/main/openclaw"><img src="https://img.shields.io/badge/OpenClaw-v2.4.7-brightgreen" alt="OpenClaw v2.4.7"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/tree/main/opencode"><img src="https://img.shields.io/badge/OpenCode-v1.0.8-58a6ff" alt="OpenCode v1.0.8"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/blob/main/docs/codex.md"><img src="https://img.shields.io/badge/Codex-supported-orange" alt="Codex supported"></a>
  <a href="https://marketplace.visualstudio.com/items?itemName=alexgreensh.token-optimizer-statusline"><img src="https://img.shields.io/visual-studio-marketplace/v/alexgreensh.token-optimizer-statusline?label=VS%20Code%20Marketplace&color=0078d7" alt="VS Code Marketplace"></a>
</p>
<p align="center">
  <img src="https://img.shields.io/badge/cuts%20context%20waste-3fb950" alt="Cuts context waste">
  <img src="https://img.shields.io/badge/survives%20compaction-checkpoint%20%2B%20restore-58a6ff" alt="Survives compaction">
  <img src="https://img.shields.io/badge/saves%20real%20%24-every%20session-2ea043" alt="Saves real dollars every session">
  <img src="https://img.shields.io/badge/live%20dashboard-tokens%20%2B%20%24%20%2B%20turns-8B5CF6?logo=chartdotjs&logoColor=white" alt="Live dashboard">
  <img src="https://img.shields.io/badge/quality%20score-v6%20dual--score-blue" alt="Quality score v6 dual-score">
  <img src="https://img.shields.io/badge/tests-572-brightgreen" alt="572 Tests">
</p>
<p align="center">
  <img src="https://img.shields.io/badge/dependencies-zero-brightgreen" alt="Zero Dependencies">
  <img src="https://img.shields.io/badge/telemetry-none-brightgreen" alt="Zero Telemetry">
  <img src="https://img.shields.io/badge/python-3.9+-blue" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey" alt="Platform">
  <a href="https://github.com/alexgreensh/token-optimizer/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-PolyForm%20Noncommercial-blue.svg" alt="License: PolyForm Noncommercial"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/stargazers"><img src="https://img.shields.io/github/stars/alexgreensh/token-optimizer" alt="GitHub Stars"></a>
  <a href="https://github.com/alexgreensh/token-optimizer/commits/main"><img src="https://img.shields.io/github/commit-activity/m/alexgreensh/token-optimizer" alt="Commit Activity"></a>
  <a href="https://linkedin.com/in/alexgreensh"><img src="https://img.shields.io/badge/LinkedIn-Connect-0A66C2?logo=linkedin&logoColor=white" alt="Connect on LinkedIn"></a>
  <a href="https://github.com/sponsors/alexgreensh"><img src="https://img.shields.io/badge/sponsor-keep%20it%20free-%23ea4aaa?logo=githubsponsors" alt="Sponsor - Keep It Free"></a>
</p>

<h2 align="center">Cut the tokens you waste. Keep the work you'd lose.</h2>

<p align="center"><em>Optimize every session, then watch the dollars add up on the dashboard.</em></p>

<p align="center">
<strong>Most token tools only touch one slice of the problem.</strong>
</p>
<p align="center">
They compress command output, which covers 15-25% of your context on a good day. The other 75-85% (bloated configs, unused skills, duplicate system prompts, stale memory, plus the 60-70% you lose on every compaction) goes untouched.
</p>
<p align="center">
Token Optimizer covers all of it, keeps your work alive across compactions, measures whether the optimization actually helped, and gives you a <strong>live dashboard</strong> that shows every token, every dollar, and every turn, auto-updated after every session. Runs fully local. Zero baseline context overhead. Zero runtime dependencies.
</p>
<p align="center">
Works on <strong>Claude Code</strong> (CLI and VS Code), <strong>OpenCode</strong>, <strong>OpenClaw</strong>, <strong>Codex</strong>, <strong>Hermes</strong>, and <strong>GitHub Copilot</strong> (CLI and VS Code, beta) today. Windsurf, Cursor, and more on the way.
</p>

<p align="center">
  <img src="skills/token-optimizer/assets/hero-terminal.svg" alt="Token Optimizer Quick Scan" width="800">
</p>

## TL;DR

Install the plugin. Most of it runs automatically from that point:

| | What | How |
|---|---|---|
| **Once** | Run `/token-optimizer` after install | Scans your setup, finds waste, fixes it for you |
| **Automatic** | Everything else | Smart Compaction (checkpoints before, restores after), active compression, quality scoring, loop detection, read deduplication, model routing nudges, and more. Runs in the background every session |
| **Automatic** | Dashboard | Updates after every session with tokens, dollars, quality grades, session history |
| **When you want** | `/token-coach` | Analyzes 30 days of your session history. Shows where you're efficient and where you're not, helps plan new projects for minimal waste |
| **When you want** | `/token-optimizer quick` | 10-second health check: context fill, quality score, top issues |

Install, run the audit once, everything else just works.

## Install

**Recommended on every platform (macOS, Linux, Windows):**

```
/plugin marketplace add alexgreensh/token-optimizer
/plugin install token-optimizer@alexgreensh-token-optimizer
```

Then in Claude Code: `/token-optimizer`

> **Please enable auto-update after installing.** Claude Code ships third-party marketplaces with auto-update **off by default**, and plugin authors cannot change that default. So you won't get bug fixes automatically unless you turn it on. In Claude Code: `/plugin` → **Marketplaces** tab → select `alexgreensh-token-optimizer` → **Enable auto-update**. One-time, 10 seconds, and you'll never miss a fix again. Token Optimizer also prints a one-time reminder on your first SessionStart so you don't forget.

<details>
<summary><h3>Windows users: read this first</h3></summary>

The plugin install above is the **only** path you should use on Windows. Do **not** also run the `install.sh` script described below — that's a bash installer for macOS/Linux/WSL, and combining the two creates an `EBUSY: resource busy or locked` error because Git Bash holds Windows file handles open while the plugin system is trying to clone.

**Repo size note**: our repo is ~3 MB (218 files, ~2,700 git objects). If your `/plugin marketplace add` attempt seems to be downloading gigabytes, it's not us — cancel and check whether Claude Code is cloning a different URL or network state. You can verify by cloning manually: `git clone --bare https://github.com/alexgreensh/token-optimizer.git` should finish in under a second and produce a ~2.6 MB directory.

If you've already hit the EBUSY error:

1. Close every Claude Code window and Git Bash terminal.
2. Open Task Manager and end any lingering `git.exe` processes.
3. Delete both folders if they exist:
   - `C:\Users\<you>\.claude\token-optimizer`
   - `C:\Users\<you>\.claude\plugins\marketplaces\alexgreensh-token-optimizer`
4. If Windows still refuses to delete (file in use), reboot, then delete.
5. Open a fresh Claude Code window and run the two `/plugin` commands above.

**Manual ZIP fallback** (advanced, if plugin install repeatedly fails): download a versioned source ZIP from the [latest GitHub Release](https://github.com/alexgreensh/token-optimizer/releases/latest), download that release's `CHECKSUMS.sha256`, verify the extracted scripts, then run `python measure.py setup-quality-bar` from `C:\Users\<you>\.claude\token-optimizer\`. Note: on Windows the command is `python`, not `python3`.

</details>

<details>
<summary><h3>macOS / Linux only: script install (alternative)</h3></summary>

If you prefer a script-managed install on macOS or Linux, this works too and auto-updates daily by re-running the verified installer against the latest release tag. **Do not run this on Windows, and do not run it alongside the plugin install above on any platform.** Pick one method.

```bash
tmp="$(mktemp -d)"
release_json="$(curl -fsSL https://api.github.com/repos/alexgreensh/token-optimizer/releases/latest)"
tag="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])' <<<"$release_json")"
checksums="$(python3 -c 'import json,sys; data=json.load(sys.stdin); print(next(a["browser_download_url"] for a in data["assets"] if a["name"]=="CHECKSUMS.sha256"))' <<<"$release_json")"
git clone --branch "$tag" --depth 1 https://github.com/alexgreensh/token-optimizer.git ~/.claude/token-optimizer
curl -fsSL -o "$tmp/CHECKSUMS.sha256" "$checksums"
install_sum="$(grep '  install.sh$' "$tmp/CHECKSUMS.sha256")"
(cd ~/.claude/token-optimizer && (printf '%s\n' "$install_sum" | sha256sum -c - --quiet 2>/dev/null || printf '%s\n' "$install_sum" | shasum -a 256 -c - --quiet))
bash ~/.claude/token-optimizer/install.sh
rm -rf "$tmp"
```

This verifies `install.sh` before executing it. The installer then resolves the latest GitHub release tag, checks out that tag, and verifies every installed runtime file against that release's checksums. If you're offline or behind a restrictive proxy, set `TOKEN_OPTIMIZER_SKIP_VERIFY=1` before running.

Works on Claude Code (CLI and VS Code), [OpenCode](#opencode), [OpenClaw](#openclaw), [Codex](#codex), [Hermes](#hermes), and GitHub Copilot (CLI and VS Code, beta — see `docs/copilot.md`). Each platform has its own native plugin. No bridging, no shared runtime, zero cross-platform dependencies.

</details>

<details>
<summary><h3>Codex</h3></summary>

Token Optimizer works on OpenAI Codex (CLI and Desktop). Same core engine, adapted for AGENTS.md, GPT-5.x models, intelligence levels, and Codex's hook surface. Some Claude Code mechanisms have Codex-native equivalents rather than identical hooks; the Codex docs call out the few upstream hook gaps honestly.

```bash
codex plugin marketplace add alexgreensh/token-optimizer
```

Then in the Codex TUI: `/plugins` and install Token Optimizer. Ask for it conversationally: "Run Token Optimizer".

After install, set up hooks and the bookmarkable dashboard:

```bash
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py codex-install --project "$PWD"
TOKEN_OPTIMIZER_RUNTIME=codex python3 skills/token-optimizer/scripts/measure.py setup-daemon
```

Dashboard: `http://localhost:24843/token-optimizer` (separate port from Claude Code's 24842, both can run side by side).

Auto-updates on startup via `git ls-remote`. Manual: `codex plugin marketplace upgrade`.

See [`docs/codex.md`](docs/codex.md) for the full feature parity table, hook profiles, and Codex model pricing.

</details>

<details>
<summary><h3>OpenCode</h3></summary>

Native TypeScript plugin for [OpenCode](https://github.com/anomalyco/opencode) with full Claude Code feature parity. v6 dual-score quality engine, smart compaction with mode-aware context injection, session continuity, quality nudges, loop detection, and a built-in dashboard.

```bash
opencode plugin token-optimizer-opencode
```

Or add it to your `opencode.json` (or `.opencode/opencode.jsonc`) plugin array:

```jsonc
{
  "plugin": ["token-optimizer-opencode"]
}
```

**No global npm? Local build install:** clone this repo and run the bundled installer. It builds the plugin and drops it into `~/.config/opencode/plugins/`, which OpenCode auto-loads. Requires Bun and registry or cache access unless dependencies are already present:

```bash
git clone https://github.com/alexgreensh/token-optimizer.git
token-optimizer/install.sh --opencode
```

Two custom tools are available inside OpenCode:
- `token_status` for on-demand quality reports
- `token_dashboard` to generate and open the visual dashboard

Works with every model OpenCode supports: Anthropic, OpenAI, Google, DeepSeek, Qwen, Mistral, xAI, and local models. MRCR quality curves are calibrated per model family.

See [`opencode/README.md`](opencode/README.md) for full docs, configuration options, and environment variable overrides.

</details>

<details>
<summary><h3>OpenClaw</h3></summary>

Native TypeScript plugin for OpenClaw agent systems. Zero Python dependency, zero runtime dependencies, zero telemetry. Works with any model your gateway is configured against: Claude, GPT-5, Gemini, DeepSeek, local via Ollama.

```bash
# From GitHub (recommended)
openclaw plugins install github:alexgreensh/token-optimizer

# From ClawHub
openclaw plugins install token-optimizer
```

Inside OpenClaw, run `/token-optimizer` for a guided audit with coaching.

See [`openclaw/README.md`](openclaw/README.md) for full docs.

</details>

<details>
<summary><h3>Hermes</h3></summary>

> **Beta (v0.1.0).** Token Optimizer for [NousResearch Hermes](https://github.com/NousResearch/hermes-agent) — the autonomous agent that lives in your terminal and messaging apps. Per-turn usage capture, cost, context-quality scoring, before/after savings, a proactive pre-turn context nudge, and a dashboard — all read from Hermes's own `~/.hermes/state.db`. Model-agnostic: each session is priced at its real model (Nous Portal, OpenRouter, OpenAI, Anthropic, local), so free/cheap models never show inflated savings.

Native Python plugin. Installs into `~/.hermes/plugins/token-optimizer/`, which Hermes auto-loads. Read-only access to your Hermes data, no telemetry, no Python dependency conflicts.

```bash
git clone https://github.com/alexgreensh/token-optimizer.git
token-optimizer/install.sh --hermes
```

Verify the install:

```bash
token-optimizer/install.sh --hermes --dry-run   # preview
python3 token-optimizer/skills/token-optimizer/scripts/measure.py hermes-doctor
```

Inside Hermes:
- `/token-optimizer` — usage + context-quality summary for recent sessions
- `hermes token-optimizer` — open the dashboard (http://localhost:24844)
- A short context nudge appears automatically before a turn once context fills past ~70%

See [`hermes/README.md`](hermes/README.md) for full docs.

</details>

<details>
<summary><h3>GitHub Copilot</h3></summary>

> **Beta.** Token Optimizer for **GitHub Copilot** — both the **CLI** and **VS Code**. Copilot now bills in AI Credits and nothing in the product answers "what is this session costing me." This adapter does, using Copilot's own cost figures (per-request `copilotUsageNanoAiu` in VS Code, premium-request totals on the CLI) — never a re-derived pricing table. Per-session cost and tokens, context-quality scoring, capability-gated context savers, before/after savings, and the shared dashboard.

Native Python, read-only access to Copilot's own data, no telemetry, no dependencies. The CLI and VS Code surfaces are separate session populations — never merged, never summed.

Run these three lines from **any folder** — the first downloads the repo into a new `token-optimizer/` folder, the second moves into it, the third installs:

```bash
git clone --depth 1 https://github.com/alexgreensh/token-optimizer.git
cd token-optimizer
bash install.sh --copilot
```

Verify the install (run these from **inside** the `token-optimizer` folder you just `cd`'d into):

```bash
bash install.sh --copilot --dry-run   # preview without writing anything
TOKEN_OPTIMIZER_RUNTIME=copilot python3 skills/token-optimizer/scripts/measure.py copilot-doctor
```

**Already have Token Optimizer installed** (plugin or script install)? Skip the clone — and don't look for `install.sh` inside `~/.claude/skills/token-optimizer`, it only exists at the repo root. The installer module ships with the skill, so run it directly:

```bash
TOKEN_OPTIMIZER_RUNTIME=copilot python3 ~/.claude/skills/token-optimizer/scripts/measure.py copilot-install
```

Adjust the path if your `measure.py` lives elsewhere — any up-to-date copy works. Script installs can equivalently run `bash ~/.claude/token-optimizer/install.sh --copilot`.

Using Copilot:
- `copilot-summary` — credits-led cost + token summary for recent sessions
- `copilot-doctor` — per-source readiness + hook capability check
- User-level CLI hooks (`~/.copilot/hooks/`) add bash output compression and session-start continuity restore, each gated on what your installed Copilot CLI version actually supports
- For VS Code per-request credit costs, enable both `github.copilot.chat.agentDebugLog` settings (they log full prompt text to disk, so the switch is yours)

Copilot CLI ships weekly and its hook fields break and regress between releases, so every engine feature is gated on a per-version capability map and auto-activates when upstream support lands. See [`copilot/README.md`](copilot/README.md) and [`docs/copilot.md`](docs/copilot.md) for full docs and the honest feature-by-feature status.

</details>

---

## Full Visibility: See Every Token, Every Dollar, Every Turn

Most tools tell you your context is full. Token Optimizer shows you exactly where every token went, how much each turn cost, which skills and MCP servers actually fired, and which ones are just sitting there eating your budget.

![Token Optimizer Dashboard](skills/token-optimizer/assets/dashboard-demo.gif)

One single-file HTML dashboard. Auto-regenerates after every session via the SessionEnd hook. Bookmark `http://localhost:24842/token-optimizer` and it's always current. Zero baseline context overhead, no telemetry, zero setup after install.

### What the dashboard tracks

- **Per-turn token breakdown** for every API call: input, output, cache-read, cache-write, with spike detection highlighting context jumps
- **Cache analysis**: stacked bars showing input vs output vs cache-read vs cache-write split, with TTL mix (`1h` vs `5m`) and hit rate alongside
- **Pacing metrics** between calls so you can see whether a thread was steady or stop-start
- **Cost across 4 pricing tiers**: Anthropic API, Vertex Global, Vertex Regional, AWS Bedrock. Set your tier once and every session updates
- **Color-coded quality scores** overlaid on every session: green healthy, yellow degrading, red trouble
- **Subagent cost breakdown**: orchestrator vs worker spend, top offenders ranked by cost, flags when subagents consume over 30%
- **Top 5 costliest prompts** per session, pairing each user message with the cost of the response
- **Skill adoption trends**: which skills you actually invoke vs just having installed
- **Model mix over time**: Opus, Sonnet, Haiku breakdown across every session
- **CLAUDE.md and MEMORY.md health cards** on the Overview tab with line count, orphan count, and status at a glance
- **Drift detection**: config snapshots compared across time so you catch creep before it costs you
- **Savings tracker**: cumulative dollars saved from optimizations, checkpoint restores, and tool-output replacements

`/context` shows a capacity bar. Proxy compressors print a terminal report. Token Optimizer shows the receipts, auto-updated, with zero baseline context overhead.

### Launch it

```bash
python3 measure.py setup-daemon           # Bookmarkable URL at http://localhost:24842/token-optimizer
python3 measure.py dashboard --serve      # One-time serve over HTTP
```

By default the dashboard binds to localhost, reachable only from your own machine. On a headless or shared box where you want LAN access, set `TOKEN_OPTIMIZER_DASHBOARD_HOST=0.0.0.0` before running `setup-daemon`:

```bash
TOKEN_OPTIMIZER_DASHBOARD_HOST=0.0.0.0 python3 measure.py setup-daemon
```

`setup-daemon` now honors this variable (the older `dashboard --serve` path already did). Because the daemon runs under launchd / systemd / Task Scheduler with an empty environment, the host you choose is persisted to a small `dashboard-host` file next to the daemon, so the background service binds it correctly. This setting is **per-runtime**: each daemon (Claude, Codex, Hermes, Copilot) persists its own host, so run `setup-daemon` under each runtime you want to expose. The setting is **sticky**: re-running `setup-daemon` without the variable (for example during an upgrade) keeps your last choice. It is cleared only when you change it or run `setup-daemon --uninstall`. Allowed values are `127.0.0.1`, `localhost`, and `0.0.0.0`; anything else is rejected with a warning and falls back to localhost. In network mode the dashboard is **view-only for LAN visitors**: the token endpoint is loopback-locked, so the per-install token that gates every toggle can only be fetched from the machine itself. LAN visitors can view the dashboard; skill and MCP toggles work only from the machine running the daemon.

Throughout this README, whenever a feature mentions it's also visible on the dashboard, that means it lives inside this same HTML page. One place, everything tracked.

---

## What Makes This Different

### Three kinds of token waste, and most tools fix one

**Structural waste**: bloated CLAUDE.md, unused skills, duplicate system reminders, stale MEMORY.md, invisible entries past line 200, dead MCP servers. Often the biggest share in high-waste setups, and it compounds, since a leaner prefix means a smaller cache-read bill on every turn that follows. Almost nobody touches this.

**Runtime waste**: verbose command output, oversized MCP results, and re-read files that flood your context mid-session. The slice proxy compressors handle.

**Behavioral waste**: the habits that quietly burn tokens. Letting the cache expire, compacting too late, looping on a failing approach, running Opus where Haiku would do, switching models mid-session and killing your cache. Token Coach analyzes 30 days of your session history to surface patterns no single session reveals: quality trending down, sessions creeping longer, cache hit rates falling, cost per session climbing. It even distinguishes when a cache drop is caused by a model switch (expected) vs. a config change (fixable).

Token Optimizer covers all three. And because it checkpoints your session before compaction fires and restores what the summary dropped, the savings stick instead of vanishing the moment auto-compact kicks in.

### Fully local, zero dependencies, zero telemetry

Pure Python stdlib on Claude Code and Codex. TypeScript with zero runtime deps on OpenCode and OpenClaw. Nothing to `pip install`, no analytics endpoint, no phone-home. Every measurement is a local SQLite write to a file you own under your runtime home. You can inspect it, export it, or delete it.

### Zero baseline context overhead

Token Optimizer runs as an external process. It doesn't inject always-on instructions into your context and it doesn't add MCP overhead. Optional quality nudges and checkpoint restore hints are short, event-triggered messages when they are useful; idle overhead stays zero.

### `/context` shows the dashboard light. Token Optimizer opens the hood.

`/context` tells you that your context is 73% full. Token Optimizer tells you which 12K are wasted on skills you never use, flags 47 orphaned MEMORY.md topic files Claude can't see, checkpoints your decisions before compaction destroys them, and gives you a quality score that tracks how much dumber your AI is getting as the session wears on.

### How it compares

|  | Token Optimizer | Headroom | RTK | context-mode | `/context` |
|---|---|---|---|---|---|
| **Tool output compression** | 🟢 30+ CLI families, credential-safe, toggleable | 🟢 6 algorithms incl. model-based | 🟢 100+ command filters | 🟢 Sandbox + summary | 🔴 |
| **First-read file skeletons** | 🟢 Shadow-validated, fail-open, full original retrievable | 🔴 | 🔴 | 🔴 | 🔴 |
| **Tabular/JSON compression** | 🟢 Value-preserving columnar | 🟢 SmartCrusher | 🔴 | 🟡 Generic summary | 🔴 |
| **Read dedup and delta diffs** | 🟢 Re-reads serve diff only | 🔴 | 🔴 | 🔴 | 🔴 |
| **Compaction survival** | 🟢 Progressive checkpoints, restore, tool output digest | 🔴 | 🔴 | 🟡 Session guide only | 🔴 |
| **Conversation history** | 🟢 Progressive checkpoints + compaction restore | 🔴 | 🔴 | 🟡 Session guide | 🔴 |
| **Model routing and behavioral coaching** | 🟢 11 detectors, subagent cost breakdown, anti-patterns | 🔴 | 🔴 | 🔴 | 🟡 Basic suggestions |
| **Historical trend analysis** | 🟢 30-day trends, quality/cost/cache/duration correlation, model-switch detection | 🔴 | 🔴 | 🔴 | 🔴 |
| **Loop and spin detection** | 🟢 Catches behavioral loops before they burn | 🔴 | 🔴 | 🔴 | 🔴 |
| **Context quality scoring** | 🟢 7-signal dual-score with grades | 🔴 | 🔴 | 🔴 | 🟡 Capacity % only |
| **Structural waste audit** | 🟢 Deep per-component (CLAUDE.md, skills, MCP, memory) | 🔴 | 🔴 | 🔴 | 🟡 Summary only |
| **CLAUDE.md and MEMORY.md health** | 🟢 8 auditors + attention-curve scoring | 🔴 | 🔴 | 🔴 | 🔴 |
| **Measures if compression helped** | 🟢 Local telemetry, before/after tokens, dollar savings | 🔴 | 🟡 `rtk gain` (token counts only) | 🔴 | 🔴 |
| **Fleet-level cross-agent analysis** | 🟢 | 🔴 | 🔴 | 🔴 | 🔴 |
| **Cache-safe** | 🟢 Never modifies existing context prefix | 🟡 Proxy mode rewrites in-flight | 🟢 Pre-shell only | 🟡 MCP overhead | 🟢 |
| **Zero baseline context overhead** | 🟢 External process, no context injection | 🔴 Injects instructions | 🟢 Shell-level only | 🔴 MCP server overhead | 🟢 Native |
| **Zero runtime dependencies** | 🟢 Pure stdlib (Python/TypeScript) | 🟡 Python + Rust + optional model | 🟢 Single Rust binary | 🟡 SQLite adapter required | 🟢 N/A |
| **Zero telemetry** | 🟢 | 🟢 | 🟡 Opt-in | 🟡 Varies | 🟢 |
| **Multi-platform** | 🟢 Claude Code, VS Code, Codex, OpenClaw, OpenCode, Hermes | 🟢 Claude Code, Cursor, Codex, Aider, Copilot | 🟢 14 integrations | 🟢 15 integrations | 🔴 Claude Code only |

Every claim in this table is tested against real sessions and a 57-fixture compression suite you can run yourself. **[See the full benchmark methodology and results.](BENCHMARK.md)**

---

## What It Saves

Savings come from all three waste types. Some are **measured** from real token deltas, some are **estimated** from your baselines and current pricing, and some are **enabled** (a setting Token Optimizer recommends, like an MCP output cap, that does the saving once you apply it). The dollar figures are **API-equivalent**: literal cash if you pay per token (API, Bedrock, Vertex), and capacity plus quality if you are on a flat subscription (more work per session, fewer compaction wipeouts, less drift before you hit a limit).

Pricing uses the current published Anthropic rates used by the modeled Opus profile: $5 input, $25 output, $0.50 cache-read, $6.25 5m cache-write, and $10 1h cache-write per million tokens.

### What different users can expect (modeled estimates)

| Profile | Monthly input mix | Structural waste trimmed | Est. monthly value (API-equivalent) |
|---|---|---|---|
| Light | ~0.3B | ~5K/session | ~$80-150 |
| Typical heavy | ~2B | ~10-20K/session | ~$300-600 |
| Heavy + high-waste | ~6B+ | ~35K/session | ~$1,500-2,500 |

These are modeled from the volume, structural waste trimmed, cache-hit rate, and model mix, not guarantees. Your number is your own.

### Estimated savings for a heavy user

Modeled on a real heavy-Opus usage profile from local trends data: 428 sessions, 12,430 API calls, and 2.23B total input mix over 30 days. The measured split is 8.7M fresh input, 2.13B cache-read input, 88.5M cache-write input, and 9.65M output. The structural line uses a historical starting-context baseline of ~45K tokens before the first user message, with ~15K-20K prefix tokens trimmed by Token Optimizer. Estimated potential value across the three pillars:

<p align="center">
  <img src="skills/token-optimizer/assets/real-savings.svg" alt="Estimated monthly savings for a heavy user, broken down by the three waste types" width="800">
</p>

In that historical-baseline scenario, structural is the giant: the diagram models how a leaner prefix can reduce future input, cache-write, and cache-read work. Runtime compression and behavioral coaching add on top. Estimated potential, not a guarantee. On a flat subscription it shows up as capacity and quality rather than cash.

---

## Trust & Safety FAQ

<details>
<summary>🎯 <strong>Can Token Optimizer degrade my context quality?</strong></summary>

No. Structural optimization only removes genuinely unused components (skills you never invoke, duplicate configs, orphaned memory entries). Active Compression controls can be disabled with a single command or env var. The v6 dual-score quality system actively tracks degradation, so if anything ever hurt quality, the score would show it.
</details>

<details>
<summary>💾 <strong>Does it break the prompt cache?</strong></summary>

No, and this matters. The prompt cache depends on a stable prefix. Any tool that edits or removes blocks already in your conversation invalidates the cache and costs you **more**, not less.

Token Optimizer never touches content that's already in your context. It works on new content entering your window (compression), and on what happens before and after compaction (checkpoints and restore). Your cache prefix stays intact, which means Token Optimizer actually saves you money twice:

1. **Less input per turn.** Fewer structural tokens means a smaller context, so every message processes faster and cheaper.
2. **Cheaper cache reads on every turn forward.** A smaller stable prefix means a smaller cache-read bill on every subsequent message. This compounds across the session.

Be careful with tools that claim to "clean up" your context mid-session. If they modify or remove existing conversation blocks, they break your cache. The cost of re-sending a full prefix at uncached rates on the next 50 messages easily wipes out whatever they saved you.
</details>

<details>
<summary>🔒 <strong>Does it send any data anywhere?</strong></summary>

No analytics, no telemetry endpoint, no product data leaves your machine. Measurement events are local SQLite rows you own. Install and update paths may call GitHub to fetch releases, checksums, or marketplace metadata; set `TOKEN_OPTIMIZER_SKIP_VERIFY=1` only if you explicitly accept skipping release verification.
</details>

<details>
<summary>🛟 <strong>Can it hurt my session?</strong></summary>

No. All hooks are non-blocking with fail-open design. If a Token Optimizer script ever errors, your command runs normally. Compression features are all individually toggleable. Checkpoints are additive. Quality scoring is read-only measurement.
</details>

<details>
<summary>📦 <strong>Does it have any runtime dependencies?</strong></summary>

No. Pure Python stdlib on Claude Code and Codex. TypeScript with zero runtime deps on OpenCode and OpenClaw. What you clone (or `opencode plugin token-optimizer-opencode`) is everything it needs.
</details>

<details>
<summary>🧰 <strong>Which platforms does it support?</strong></summary>

Claude Code (CLI and VS Code), OpenCode, OpenClaw, Codex, Hermes, and GitHub Copilot (beta) today, with native support for each.

**What each platform gets:**

| Capability | Claude Code / Codex | OpenClaw | OpenCode | Hermes | Copilot (beta) |
|---|---|---|---|---|---|
| Quality scoring | 7 signals (dual composite) | 7 signals (two-stage) | 7 signals (MRCR curves) | 3 signals (delegated) | 3 signals (session-level) |
| Output compression | 🟢 Full | 🟢 Native TS | Platform-native | 🟢 Via Python delegation | 🟡 Capability-gated per CLI version |
| Continuity + checkpoints | 🟢 | 🟢 | 🟢 | 🟢 | 🟡 Session-start restore |
| Dashboard + savings | 🟢 | 🟢 | 🟢 | 🟢 (via bridge) | 🟢 Credits-led (cost pass-through) |

Quality signal counts differ because each platform targets a different measurement context: Claude Code/Codex measures session-level telemetry, OpenClaw measures run-level outcomes, OpenCode does file-level analysis with per-model degradation curves. The grade scale (S/A/B/C/D/F) is identical everywhere.

Copilot runs in beta with honesty built in: several Copilot CLI hook powers change between weekly upstream releases, so every engine feature is gated on a per-version capability map and auto-activates when upstream support lands. The full feature-by-feature status, including what Copilot does not yet expose to companions (per-request CLI token data, compaction steering), lives in `docs/copilot.md`.

Windsurf and Cursor are next on the roadmap.
</details>

<details>
<summary>🔐 <strong>How does install.sh verify file integrity?</strong></summary>

The installer resolves the latest GitHub Release tag, checks out that tag, fetches `CHECKSUMS.sha256` from the same release (not from the repo tree), then verifies every script file against those checksums. This out-of-band verification means a compromised commit cannot swap both the code and the checksums simultaneously. If the checksum fetch fails or any file fails verification, the installer exits with a non-zero status and rolls back an updated install. You can also verify manually:

```bash
# Download checksums from the latest release
curl -sL $(gh release view --json assets -q '.assets[] | select(.name=="CHECKSUMS.sha256") | .url') -o /tmp/checksums.sha256

# Verify your install
cd ~/.claude/token-optimizer && shasum -a 256 -c /tmp/checksums.sha256
```
</details>

---

## Why install this first

Every Claude Code session starts with invisible overhead: system prompt, tool definitions, skills, MCP servers, CLAUDE.md, MEMORY.md. A typical power user burns 50-70K tokens before typing a word.

With Opus 4.6 and Sonnet 4.6 now at 1M context, that feels like breathing room. The problems still compound:

- **Quality degrades as context fills.** MRCR drops from 93% to 76% between 256K and 1M. Your AI gets measurably dumber as the window fills.
- **Rate limits hit faster.** Ghost tokens count toward your plan's usage caps on every message, cached or not. 50K overhead times 100 messages is 5M tokens burned on nothing.
- **Compaction is catastrophic.** 60-70% of your conversation gone per compaction. After 2-3 compactions, you've lost 88-95%. And each compaction means re-sending all that overhead again.
- **Higher effort means faster burn.** More thinking tokens per response means you hit compaction sooner, which means more total tokens across the session.
- **You can't fix what you can't see.** Without per-turn visibility into cache hits, model mix, and subagent spend, every "it feels slow" guess costs money. The dashboard shows exactly which turn was the expensive one.

Token Optimizer tracks all of this. Quality score, degradation bands, compaction loss, drift detection, per-turn cost across four pricing tiers, and skill-and-MCP attribution for every session. Zero baseline context overhead.

![What happens inside a 1M session](skills/token-optimizer/assets/user-profiles.svg)

> **"But doesn't removing tokens hurt the model?"** No. Token Optimizer only touches what's safe to touch. Structural optimization removes genuinely unused components (duplicate configs, unused skill frontmatter, orphaned memory entries), never the conversation itself. Active Compression works on new content entering your window (smart re-reads, credential-safe command summaries) and on the compaction boundary (checkpoints before auto-compact, restore after). Nothing already in your context gets edited or removed, which means your prompt cache stays intact. The v6 dual-score quality system tracks degradation in real time, and most users see scores improve after optimization because the model has more room for real work.

---

## Smart Compaction and Session Continuity

When auto-compact fires, 60-70% of your conversation vanishes. Decisions, error-fix sequences, agent state, all gone.

Smart Compaction catches all of it as checkpoints before compaction fires, then restores what the summary dropped. It also injects a digest of large tool outputs the model previously processed, so after compaction the model knows what it already saw without re-reading everything from scratch. Sessions pick up where you left off, even after a crash or /clear. Checkpoint history and compaction loss per session are also visible on the dashboard.

Compression savings only stick if your session survives the compaction. Saving tokens on `git status` doesn't help if the next auto-compact wipes out the decision that made you run `git status` in the first place. Smart Compaction closes that loop: checkpoint your decisions, restore them after compaction, and remind the model what outputs it already processed so it doesn't waste tokens re-reading them.

```bash
python3 measure.py setup-smart-compact    # checkpoint + restore hooks
```

### Progressive Checkpoints

Instead of waiting for emergency compaction, Token Optimizer captures session state at multiple thresholds: `20%`, `35%`, `50%`, `65%`, and `80%` context fill, plus quality drops below `80`, `70`, `50`, and `40`. It also snapshots before agent fan-out and after large edit batches. On restore, it picks the richest eligible checkpoint, not just the most recent one.

Background guards handle one-shot threshold capture, cooldown suppression, and deterministic extraction. No LLM calls in the checkpoint path.

### Tool Result Archive (model-aware, no manual lookups)

Large MCP tool results (>4KB) get archived to disk automatically and replaced with a short preview plus an inline hint like `[Full result archived (12,400 chars). Use 'expand abc123' to retrieve.]`

That hint is visible to Claude, not just you. So after a compaction (when the original tool result has been summarized away), if the model needs the full output again to answer your next question, it invokes `expand abc123` itself and the archived content comes back through the CLI. No command re-run, no lost output, no context cost in the meantime.

Native Bash/Read/Grep-style outputs are archived for continuity too, but Claude's current PostToolUse API cannot replace those already-returned results. Those durability archives are not counted as token savings.

You can run `expand` yourself too when you want to see a specific archived result, but the primary flow is automatic: the model sees the hint, the model asks for the bytes, the bytes come back.

```bash
python3 measure.py expand --list                 # List all archived tool results
python3 measure.py expand <tool-use-id>          # Retrieve a specific archived result manually
```

### Session Continuity

Sessions auto-checkpoint on end, /clear, and crashes. On a fresh session, Token Optimizer drops a short in-context pointer to the most recent relevant checkpoint, so Claude can pull the right prior state on its own if the new conversation needs it. No auto-replay of stale context, no user action required, just a breadcrumb the model can follow when it matters.

Enable optional local-only checkpoint telemetry to see whether checkpoints are firing and which triggers are active:

```bash
TOKEN_OPTIMIZER_CHECKPOINT_TELEMETRY=1 python3 measure.py checkpoint-stats --days 7
```

---

## Quality Scoring

v6 reports two composites: **Resource Health** for monotonic session risk, and **Session Efficiency** for behavior that can improve or regress during the session.

| Score | Signals | What It Means For You |
|--------|--------|----------------|
| **Resource Health** | Context fill, compaction depth, absolute waste tokens | How close are you to the degradation cliff, and how much hard capacity has already been lost. |
| **Session Efficiency** | Stale reads, bloated results, decision density, agent efficiency | Whether the session is using its tokens well right now. |
| **Detail signals** | Duplicate reminders and per-category waste estimates | Extra diagnostics for explaining why a score moved. |

### Efficiency Grades

Every quality score includes a letter grade for quick triage. The status line shows something like `ContextQ:A(82)`, and the same grade appears in the dashboard, coach tab, and CLI output.

| Grade | Range | Meaning |
|-------|-------|---------|
| **S** | 90-100 | Peak efficiency. Everything is clean. |
| **A** | 80-89 | Healthy. Minor optimization possible. |
| **B** | 70-79 | Degradation starting. Worth investigating. |
| **C** | 55-69 | Significant waste. Coach mode will help. |
| **D** | 40-54 | Serious problems. Multiple anti-patterns likely. |
| **F** | 0-39 | Context is rotting. Immediate action needed. |

### Degradation Bands

The status bar shifts color as your context fills:

- Green (<50% fill): peak quality zone
- Yellow (50-70%): degradation starting
- Orange (70-80%): quality dropping
- Red (80%+): severe, consider /clear

### What Degradation Actually Looks Like

Real session. 708 messages, 2 compactions, 88% of the original context gone. Without the quality score, you'd have no idea.

![Real session quality breakdown](skills/token-optimizer/assets/quality-example.svg)

---

## Active Compression (v5)

Token Optimizer no longer just measures context bloat. It actively reduces it. Seven features target specific waste patterns, each with honest risk assessment and dashboard toggles.

![v5 Active Compression overview](skills/token-optimizer/assets/v5-hero.svg)

**On by default**: Quality Nudges, Loop Detection, Delta Mode, Structure Map, Bash Compression (16 handlers), Activity Mode Detection, Decision Extraction.

Five compression controls are toggleable from the Manage tab in the dashboard, via CLI (`measure.py v5 enable|disable <feature>`), or with environment variables. Activity Mode and Decision Extraction are hook-managed continuity features.

| Feature | Default | Potential Savings | Risk |
|---|---|---|---|
| Quality Nudges | ON | Measured per-compact (fill% recovery) | None |
| Loop Detection | ON | Measured per-loop (actual turn content) | None |
| Delta Mode | ON | ~20% (smart re-reads) | Low |
| Structure Map | ON (soft-block) | ~30% (large file re-reads, up to 99% per file) | Low |
| Bash Compression | ON | ~10% (CLI output) | Low |
| Activity Mode | ON | Adapts compaction to session phase | None |
| Decision Extraction | ON | Preserves decisions across compactions | None |

> **Privacy note**: Every feature runs on your machine. No analytics endpoint, no phone-home, no cloud sync. "Measurement" and "beta telemetry" always mean local-only SQLite writes to a file you own, and you can inspect, export, or delete that file at any time. Network use is limited to install/update checks against GitHub or package registries for the install method you chose.

![Quality Nudges and Loop Detection in action](skills/token-optimizer/assets/v5-nudges-loops.svg)

### Quality Nudges (ON by default, fully automatic)

Watches your context quality in real time. When the score drops 15+ points or crosses below 60, an inline system note enters the context that reads something like `[Token Optimizer] Quality dropped to 58. Consider /compact to protect context.`

Claude sees that note on the next turn and surfaces the warning to you naturally, or adjusts behavior on its own. You don't have to watch a dashboard or remember thresholds. The nudge shows up right where decisions get made, with zero setup after install.

**Value**: catches context rot early so /compact lands at the right moment, before you lose decisions to compaction.

**How it works**: runs inside the existing quality-cache hook on every UserPromptSubmit. Cooldown of 5 minutes between nudges, max 3 per session. Suppressed on the first check after a compaction, so you don't get warned about quality you just fixed.

**Risk**: none. Only adds a short note to context, never removes anything.

### Loop Detection (ON by default, fully automatic)

Catches the AI getting stuck on a retry loop before it burns through tokens. When similarity crosses the threshold, a short inline note lands in the context flagging the loop so the model breaks out of it, with no user action needed. Savings are measured from the actual content of the looping turns, not estimated.

**Value**: post-hoc detectors found that loop sessions average 47K wasted tokens. Real-time detection prevents this. Every caught loop logs the measured token cost of the loop turns to your local telemetry.

**How it works**: compares the last 4 user messages and last 5 tool results for similarity. Fires at confidence ≥0.7 with a session cap of 2 notes. Uses fixed message templates and never echoes user content back.

**Risk**: none. Only adds a short note.

![Delta Mode: smart re-reads](skills/token-optimizer/assets/v5-delta-mode.svg)

### Delta Mode (ON by default, your biggest single win)

When the AI re-reads a file after editing it, the Read call returns only what changed instead of the whole file. Fully automatic, no configuration, no user action. 65%+ of Read calls in real sessions are re-reads, which makes this the highest-impact v5 feature.

**Value**: typical sessions re-read the same file 2-5 times. Delta mode sends only the diff. A 2,000-token file re-read becomes a 50-token diff, for 97% savings on that specific read.

**How it works**: stores file content (up to 50KB per file) in a local cache on first read. On re-read with changed mtime, computes a unified diff via Python's `difflib` (stdlib, no git dependency). Falls back to full re-read if the diff exceeds 1,500 chars or either file exceeds 2,000 lines. Scoped to explicit full-file reads so narrow `offset`/`limit` requests are never served a whole-file diff. `.env` and credential files are excluded from caching.

**Risk**: low. If the AI needed the full file to understand the change in context, the diff alone might not be enough. Fails open on large changes and big files. Set `TOKEN_OPTIMIZER_READ_CACHE_DELTA=0` to disable.

### Structure Map (ON in soft-block mode, your biggest win on large files)

When Claude re-reads a code file it already saw this session, the Read call is blocked and replaced with a compact structural summary: function signatures, class hierarchies, imports, and module docstrings. A 720KB Python file (180,000 tokens) becomes a 250-token skeleton. Works on Python files up to 800KB/20K lines and JS/TS files up to 400KB/5K lines.

**Value**: code-heavy sessions re-read the same large files 3-17 times. Structure Map compresses every re-read after the first by 95-99%. On a 180K-token file re-read 5 times, that's ~900K tokens saved in a single session.

**How it works**: on first read, caches the file content and generates an AST-based summary (Python) or regex-based summary (JS/TS). On subsequent reads of the same unchanged file, returns the summary via `additionalContext` and blocks the full re-read. Falls back to full read on files below 1,000 tokens, generated/minified files, partial-range reads, or if the AST parse fails.

**Measurement**: enable `measure.py v5 enable structure_map_beta` or `TOKEN_OPTIMIZER_STRUCTURE_MAP=beta` to log compression events to your local SQLite for `compression-stats`. Nothing sent anywhere.

**Risk**: low. The model works from the summary instead of full source. For files where implementation details matter (not just structure), the model can request a full read. Disable with `TOKEN_OPTIMIZER_READ_CACHE_MODE=shadow`.

![Bash Output Compression: git status and pytest before/after](skills/token-optimizer/assets/v5-bash-compression.svg)

### Bash Output Compression (ON by default, lossy)

Rewrites common CLI commands to return compressed summaries instead of verbose output. v5.1.0 ships seven new handlers covering the command families that eat the most context: lint (rule-code grouping for eslint, ruff, flake8, shellcheck, rubocop, golangci-lint), log tails (adjacent-duplicate collapse), tree (depth-2 truncation), docker pull (progress filtering), long listings (pip list, npm ls, docker ps, with top-N plus tail marker), JS/TS/Go build output (error-and-summary view), and test runner routing (cypress, playwright, mocha, karma all route through the unified pytest compressor).

Together with the existing git and pytest handlers, that's full coverage for ~90% of the verbose CLI output real sessions produce.

**Value**: strips hundreds of lines of test/build/git output down to just the essentials. A 564-token pytest output becomes 115 tokens. A 60-file `ls -la` truncates to 50. Best for sessions with lots of CLI commands.

**How it works**: a PreToolUse hook (`bash_hook.py`) intercepts safe read-only commands, tokenizes them with `shlex.split()`, checks against a whitelist, and rewrites them via `updatedInput` to route through a compression wrapper (`bash_compress.py`). Categorically excludes compound commands (anything with `;`, `&&`, `||`, `|`, `$()`, backticks, `>`, `>>`), sudo, and interactive flags.

**Security**: `shell=True` is never used. Credentials (AWS keys, GitHub PATs, Slack tokens, Stripe keys, OpenAI keys, HTTP basic-auth URLs) are scanned pre-compression and preserved verbatim. Multilingual error lines survive the preservation path. Partial output on timeout is returned raw, never compressed.

**How to disable**: `measure.py v5 disable bash_compress` or `TOKEN_OPTIMIZER_BASH_COMPRESS=0`

**Risk**: low. Compression is lossy by design. For routine checks this is fine. For careful diff review or debugging specific test failures, disable temporarily with the command above.

### Activity Mode Detection (ON by default, v5.6)

Classifies your session into one of five modes (code, debug, review, infra, general) using a sliding window of the last 10 tool calls. The mode label feeds into compaction guidance so PRESERVE/DROP priorities adapt to what you're actually doing: debug mode preserves error signals and stack traces, code mode preserves edited files and their tests, review mode keeps findings and decisions while dropping full file contents.

**How it works**: the PostToolUse hook classifies each tool call into a bucket (edit, read, bash_infra, bash_git, web, etc.) and stores it in the per-session SQLite. Mode classification runs on every tool call with zero latency impact (single INSERT + bounded SELECT). The activity log auto-prunes at 30 rows.

**Risk**: none. Mode detection is read-only context, never modifies or blocks anything.

### Decision Extraction (ON by default, v5.6)

Detects decision statements ("chose X because Y", "going with Z over W", "switched to") in real-time from tool outputs and stores them incrementally in the session database. At compaction time, these decisions are injected as CRITICAL DECISIONS that the compaction summary must preserve verbatim. Combined with the new anchored compact state (which persists intent, changes, decisions, and errors across compaction cycles), this prevents the decision drift that makes post-compaction sessions lose context.

**How it works**: regex-based extraction on the PostToolUse path (runs only on outputs >500 chars). Uses atomic read-modify-write (SQLite BEGIN IMMEDIATE) to prevent lost updates under concurrent hooks. Capped at 10 decisions per session.

**Risk**: none. Only adds structured data to the compaction guidance, never removes anything.

### Managing v5 features

Three ways to control these features:

```bash
# CLI
python3 measure.py v5 status                    # show all features with current state
python3 measure.py v5 enable delta_mode         # turn a feature on
python3 measure.py v5 disable bash_compress     # turn a feature off
python3 measure.py v5 info delta_mode           # show full details for one feature
python3 measure.py v5 welcome                   # show the first-run welcome screen
python3 measure.py compression-stats            # see actual measured savings from local telemetry
```

```bash
# Environment variables (override config.json, for CI/scripts)
TOKEN_OPTIMIZER_QUALITY_NUDGES=0        # kill switch for nudges
TOKEN_OPTIMIZER_LOOP_DETECTION=0        # kill switch for loop detection
TOKEN_OPTIMIZER_READ_CACHE_DELTA=1      # enable delta mode
TOKEN_OPTIMIZER_BASH_COMPRESS=0         # disable bash compression
TOKEN_OPTIMIZER_STRUCTURE_MAP=beta      # enable beta telemetry
```

**Dashboard**: Open `token-dashboard` and the Manage tab. Active Compression (v5) is the first section. Toggles apply instantly to new tool calls, no Claude Code restart needed. Each feature shows what it does, its value, how it works, its risk level, and its impact estimate.

**First-run welcome**: on your first session after installing v5, you'll see a one-time welcome screen explaining each feature, its default state, and how to toggle it. Stored in `config.json` so it only shows once.

### Measuring real savings (all local)

All v5 features log to a `compression_events` SQLite table stored locally on your machine at `~/.claude/_backups/token-optimizer/trends.db`. Nothing leaves your system.

```bash
python3 measure.py compression-stats --days 30
```

Output shows total events per feature, tokens saved, compression ratio, and quality preservation rate. The `verified` flag distinguishes exact measurements (delta mode knows the precise before/after) from estimates (structure map is heuristic).

---

## Live Quality Bar

A glance at your terminal tells you if you're in trouble. Colors shift from green to red as quality degrades. When quality drops below 75, session duration appears as a warning. Running subagents show with their model and elapsed time so you can spot misrouted models.

![Status Bar Degradation](skills/token-optimizer/assets/status-bar.svg)

```bash
python3 measure.py setup-quality-bar      # one-time install
```

**My quality bar disappeared, how do I get it back?** Running Claude Code's built-in `/statusline` rewrites the `statusLine` key in `~/.claude/settings.json` and silently overwrites Token Optimizer's entry. SessionStart detects this and **auto-restores** the quality bar. Just start a new session and it's back. You'll see a one-line notice explaining what happened.

**I really don't want the quality bar anymore, how do I turn it off for good?**

```bash
python3 measure.py setup-quality-bar --uninstall
```

This removes the components and writes `quality_bar_disabled: true` to `~/.claude/token-optimizer/config.json`. The opt-out is sticky across sessions. SessionStart will not auto-restore it. You can also just tell Claude Code in natural language: _"remove the Token Optimizer statusline"_, and Claude will run the uninstall command for you.

**I changed my mind, bring it back.** Run `python3 measure.py setup-quality-bar`. Explicit install clears the opt-out flag automatically.

**I want to keep my own custom statusline and also see the quality score.** The custom-statusline path is still respected when you run `setup-quality-bar` directly. You'll get integration instructions for reading `~/.claude/token-optimizer/quality-cache.json` from your own script instead.

---

## Coach Mode and Fleet Auditor

Token Optimizer is not just reactive. It's also proactive.

### Coach Mode

```
> /token-coach
```

Tell it your goal. Get back specific, prioritized fixes with exact token savings.

Token Coach works at two levels. **Static analysis** audits your current setup: 8 named anti-patterns (The Kitchen Sink, The Hoarder, The Monolith, and more), multi-agent design patterns, structural bloat. **Historical analysis** reads 30 days of your session data and surfaces what no single session can show:

| Pattern | What it detects |
|---|---|
| Quality drift | Average quality dropping week over week |
| Session duration creep | Sessions getting longer, filling context faster |
| Cache degradation | Cache hit rate falling (and whether model switches are the cause) |
| Grade distribution | Too many D-grade sessions piling up |
| Cost awareness | Cost per session climbing, with dollar-grounded routing advice |
| Duration-quality correlation | Short sessions scoring higher, suggesting you should break up long ones |
| Compression gap | Shadow-only savings far exceeding active compression (untapped opportunity) |
| Model switching | Frequent mid-session model switches invalidating the prompt cache |

Every insight is grounded in your actual numbers, not generic advice. "Your short sessions score 68 vs 60 for long ones" hits differently than "consider shorter sessions."

**Building a new project?** Run `/token-coach` before writing your first `CLAUDE.md` or Codex `AGENTS.md`. Start clean instead of accumulating waste for months and fixing it later.

### Waste Detectors

11 automated detectors analyze your session patterns and surface actionable findings:

| Detector | What it catches |
|---|---|
| PDF/binary ingestion | Large files consuming context (warns with token estimate) |
| Web search overhead | Too many web results dumped into context |
| Retry churn | Same tool retried 3+ times with errors |
| Tool cascade | 3+ consecutive tool errors in a chain |
| Looping | Repeated similar messages (stuck model) |
| Overpowered model | Opus used for simple edits (with "if Sonnet: $X saved") |
| Weak model | Haiku on complex tasks needing a stronger model |
| Bad decomposition | Monolithic 500+ word prompts doing too much |
| Wasteful thinking | Extended thinking >2x output for small edits |
| Output waste | Verbose responses to simple operations, repeated explanations |
| Cache instability | CLAUDE.md patterns that break Anthropic's prompt cache prefix |

### Fleet Auditor

Managing multiple agent systems? Fleet Auditor scans across Claude Code, Codex, and custom transcript setups to find idle burns, model misrouting, and config bloat with dollar savings per finding. Use the OpenClaw dashboard for OpenClaw runs.

### Subagent Cost Breakdown

See exactly how much your subagents cost: total spend, % of combined budget, and top offenders ranked by cost. Flags when subagents consume >30% of total. Also visible per session on the dashboard, with orchestrator-vs-worker split.

### Costly Prompt Ranking

See which prompts cost the most: pairs each user message with the cost of the response, ranks top 5. Shows what you asked, not just totals.

### CLAUDE.md Routing Injection

Generate model routing instructions from your actual usage data and inject them into CLAUDE.md. Claude reads these every session and routes accordingly. A 48-hour staleness guard auto-removes stale advice.

```bash
python3 measure.py inject-routing --dry-run   # Preview what would be injected
python3 measure.py inject-routing              # Inject (with approval)
```

---

## Dashboard: Post-Audit Walkthrough

The Full Visibility dashboard up top auto-tracks every session. After you run `/token-optimizer` and the 6-agent audit finishes, the same dashboard opens on an audit-focused view where every component is clickable. Expand any item to see why it matters, the trade-offs, and what would change. Toggle the fixes you want, copy a ready-to-paste optimization prompt, and apply with approval.

Hover help on every column explains `Cache`, `TTL`, `Pacing`, `Cache R`, and `Cache W` without jargon. Session drill-downs key off stable session identity for consistent expansion across refreshes.

---

## What questions can you ask?

| Command | What You Get |
|---------|-------------|
| `quick` | **"Am I in trouble?"** 10-second answer: context health, degradation risk, biggest token offenders, which model to use. |
| `doctor` | **"Is everything installed correctly?"** Score out of 10. Broken hooks, missing components, exact fix commands. |
| `drift` | **"Has my setup grown?"** Side-by-side comparison vs your last snapshot. Catches config creep before it costs you. |
| `quality` | **"How healthy is this session?"** v6 dual-score analysis of your live conversation. Stale reads, wasted tokens, compaction damage. |
| `report` | **"Where are my tokens going?"** Full per-component breakdown. Every skill, every MCP server, every config file. |
| `conversation` | **"What happened each turn?"** Per-message token and cost breakdown with spike detection. |
| `pricing-tier` | **"What am I paying?"** View or switch between Anthropic, Vertex, and Bedrock pricing tiers. |
| `kill-stale` | **"Clean up zombies."** Terminate headless sessions running 12+ hours. |
| `git-context` | **"What files matter right now?"** Test companions, co-changed files, import chains for your current git diff. |
| `trends` | **"What's actually being used?"** Skill adoption, model mix, overhead trajectory over time. |
| `coach` | **"Where do I start?"** Health score, 8 named anti-patterns, and 30-day historical trend analysis: quality drift, cost per session, cache degradation, model-switch impact, session duration correlation. |
| `memory-review` | **"Is my MEMORY.md broken?"** Structural audit: orphaned files, broken links, invisible entries past line 200, duplicate rules. |
| `dashboard` | **"Show me everything."** Interactive HTML dashboard with all analytics and health cards. |
| `savings` | **"How much have I saved?"** Cumulative dollar savings from optimizations, checkpoint restores, and tool-output replacements. |
| `attention-score` | **"Is my CLAUDE.md well-structured?"** Scores sections against the attention curve, flags critical rules in low-attention zones. |
| `jsonl-inspect` | **"What's in this session?"** Record counts, token distribution, top 10 largest records, compaction markers. |
| `expand` | **"Get that result back."** Retrieves a tool result the model archived automatically. Usually the model calls this itself when it needs the full output again, but you can also run it manually. |
| `/token-optimizer` | **"Fix it for me."** Interactive audit with 6 parallel agents. Guided fixes with diffs and backups. |

---

---

## Memory Health: Your MEMORY.md Is Probably Broken

Claude auto-loads the first 200 lines of MEMORY.md every session. Everything after line 200 is silently truncated. The tokens still count against your window, but Claude never sees the content. Most power users don't know this is happening.

`memory-review` scans your MEMORY.md structurally and tells you what's wrong:

- **Orphaned topic files**: files in your memory directory that nothing links to
- **Broken links**: index entries pointing to files that don't exist
- **Invisible entries**: content below line 200 that Claude can't see
- **Inline content**: notes that should be in topic files, wasting index budget
- **Duplicate rules**: rules already in CLAUDE.md (which loads in full regardless)
- **Stale entries**: resolved/superseded content still taking up space
- **Task leakage**: TODO lists and checklists that belong in a task tracker

```bash
python3 measure.py memory-review                        # Full structural audit
python3 measure.py memory-review --json                 # Machine-readable for dashboards
python3 measure.py memory-review --apply                # Show actionable fixes
python3 measure.py memory-review --stale-days 90        # Custom staleness threshold
```

The dashboard shows CLAUDE.md Health and MEMORY.md Health cards on the Overview tab, with line count, orphan count, and status at a glance.

For contradiction detection (two rules saying opposite things), run the audit in a Claude session. The tool extracts all NEVER/ALWAYS/MUST rules from both files. Claude reviews them semantically in context, no extra LLM call needed.

---

## Read-Cache and Context Tools

### PreToolUse Read-Cache (automatic deduplication)

Detects redundant file reads automatically. On the first re-read of an unchanged file, returns a structural code summary (function signatures, class hierarchy, imports) instead of the full source. A 180,000-token file re-read becomes a 250-token skeleton. Works on Python files up to 800KB and JS/TS files up to 400KB. Default ON in soft-block mode. Saves 8-30% tokens from read deduplication across a typical session, with 95%+ compression on large code files.

```bash
# Read-cache is ON by default (warn mode). To disable:
export TOKEN_OPTIMIZER_READ_CACHE=0               # Disable
export TOKEN_OPTIMIZER_READ_CACHE_MODE=block       # Upgrade to block mode

# Read-cache management
python3 measure.py read-cache-stats --session ID   # Cache stats for a session
python3 measure.py read-cache-clear                # Clear all caches
```

Opt out entirely with `TOKEN_OPTIMIZER_READ_CACHE=0` or config `{"read_cache_enabled": false}`. Upgrade to `TOKEN_OPTIMIZER_READ_CACHE_MODE=block` after gaining confidence.

### Git-Aware Context

Analyzes your working tree to suggest files that should be in context: test companions, frequently co-changed files from the last 50 commits, and import chains for Python/JS/TS.

```bash
python3 measure.py git-context                     # Suggest files for current changes
python3 measure.py git-context --json              # Machine-readable output
```

### .contextignore

Block files from being read with gitignore-style patterns. Supports project root `.contextignore` and global `~/.claude/.contextignore`. Hard block regardless of read-cache mode. This is provided by Token Optimizer, not a built-in Claude Code feature.

```
# Block build artifacts and lockfiles
dist/**
node_modules/**
package-lock.json
yarn.lock
*.min.js
*.min.css
```

### Attention Optimizer

Scores CLAUDE.md against the U-shaped attention curve. Flags critical rules (NEVER/ALWAYS/MUST) sitting in the low-attention zone (30-70% position). Generates a reordered version that moves critical rules to high-attention zones.

```bash
python3 measure.py attention-score               # Score CLAUDE.md attention placement
python3 measure.py attention-optimize --dry-run  # Preview optimized section order
```

### JSONL Toolkit

Three utilities for session JSONL files: `jsonl-inspect` (stats, record counts, largest records), `jsonl-trim` (replace large tool results with placeholders), `jsonl-dedup` (detect and remove duplicate system reminders). All use streaming I/O and atomic writes.

```bash
python3 measure.py jsonl-inspect                 # Stats on current session JSONL
python3 measure.py jsonl-trim --dry-run          # Preview trimming large tool results
python3 measure.py jsonl-dedup --dry-run         # Preview removing duplicate reminders
```

### Savings Tracking

Tracks cumulative dollar savings from setup optimization, checkpoint restores, and tool archiving. Also surfaced on the dashboard's savings tile so you can watch the number climb over weeks.

```bash
python3 measure.py savings                      # Dollar savings report (last 30 days)
```

---

## Usage Analytics

**Trends**: Which skills do you actually invoke vs just having installed? Which models are you using? How has your overhead changed over time?

**Session Health**: Catches stale sessions (24h+), zombie sessions (48h+), and outdated configurations before they cause problems.

```bash
python3 measure.py setup-hook       # Enable session tracking (one-time)
python3 measure.py trends           # Usage patterns over time
python3 measure.py health           # Session hygiene check
python3 measure.py plugin-cleanup   # Detect duplicate skills and archive local/plugin overlaps
```

---

## VS Code Users

Using Claude Code in the VS Code extension? Token Optimizer works with full feature parity:

| Feature | CLI | VS Code Extension |
|---------|-----|-------------------|
| Smart Compaction (checkpoint + restore) | 🟢 | 🟢 |
| Quality tracking + session data | 🟢 | 🟢 |
| All hooks (SessionEnd, PreCompact, etc.) | 🟢 | 🟢 |
| Dashboard (localhost:24842/token-optimizer) | 🟢 | 🟢 |
| Active Compression (v5) | 🟢 | 🟢 |
| Status line (quality bar) | 🟢 | 🟢 |

Install the same way (plugin marketplace), and everything works. Bookmark `http://localhost:24842/token-optimizer` for the dashboard, or run `python3 measure.py setup-daemon` to enable auto-refresh after every session.

> **Note on `--bare` mode**: Running Claude Code with the `--bare` flag (for scripted/CI usage) skips all hooks and plugin sync. Token Optimizer's Smart Compaction, quality tracking, and session data collection require hooks and won't activate in `--bare` mode. This is expected. `--bare` is designed for lightweight scripted calls.

---

## Other Platforms

### OpenClaw

Native TypeScript plugin with session audits, 10 waste detectors, coach mode, Smart Compaction, and interactive dashboard adapted for OpenClaw's architecture. Works with any model (Claude, GPT-5, Gemini, DeepSeek, local via Ollama). Install instructions in the [Install section above](#openclaw). Full docs: [`openclaw/README.md`](openclaw/README.md).

### Codex

Python adapter for OpenAI Codex (CLI and Desktop). Same core engine, adapted for AGENTS.md, GPT-5.x models, intelligence levels, and Codex's hook surface. Install instructions in the [Install section above](#codex). Full docs with feature parity table, hook profiles, and model pricing: [`docs/codex.md`](docs/codex.md).

---

## License

**PolyForm Noncommercial 1.0.0**. Source-available. Personal, research, educational, and non-commercial use requires no license purchase.

_This FAQ is informational guidance, not a modification of the license terms. Last updated: April 2026._

### 🧑‍💻 Personal / hobby / research / education?
Go for it. Full source, runs locally, no license purchase needed. That's the whole point.

### 🏢 Small team (under 5 people OR under $20k/month revenue)?
Small teams get a no-cost commercial license automatically. Just use it.
If you want to [sponsor the project](https://github.com/sponsors/alexgreensh) or buy me a coffee, not required, but always appreciated ☕

### 🔄 Started personal, now it's turning into a business?
Your past use is totally fine. The license has a built-in 32-day grace period after any written notice, so there's plenty of runway.
When you're ready, just reach out for a commercial license. Terms are reasonable and size-appropriate.

### 🏗️ Larger company / commercial use?
Let's talk. Contact [Alex Greenshpun](https://linkedin.com/in/alexgreensh) or me@alexgreenshpun.com.

---

Created by [Alex Greenshpun](https://linkedin.com/in/alexgreensh).
