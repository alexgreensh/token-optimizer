# Token Optimizer for Grok Build (beta — contract-only)

Token Optimizer's Grok Build adapter ships in **NO-INSTALL / contract-only
mode**: built and unit-tested against Grok Build's documented hook + session
contract (the cloned `github.com/xai-org/grok-build` source), but **not yet
verified on a live Grok host** (the maintainer has no Grok account). Every
assumed shape is cited to its source file and re-listed under
[Needs live verification](#needs-live-verification) so a future tester can
close the whole list in one pass.

The value prop leads with **authoritative token + cost**: Grok Build persists
per-turn usage (`updates.jsonl`) that already carries both token counts and a
USD cost field (`costUsdTicks`), so Token Optimizer reports Grok's own numbers
— never a re-derived pricing table.

## Install

Run from any folder. The clone creates a `token-optimizer/` folder; `cd` into
it before running the installer:

```bash
git clone --depth 1 https://github.com/alexgreensh/token-optimizer.git
cd token-optimizer
bash install.sh --grok
# verify (from inside the token-optimizer folder)
TOKEN_OPTIMIZER_RUNTIME=grok python3 skills/token-optimizer/scripts/measure.py grok-doctor
TOKEN_OPTIMIZER_RUNTIME=grok python3 skills/token-optimizer/scripts/measure.py grok-doctor --probe
```

**Already have Token Optimizer installed** (Claude Code plugin, script install,
or a checkout under `~/.claude/skills/token-optimizer`)? The installer module
already ride along:

```bash
TOKEN_OPTIMIZER_RUNTIME=grok python3 ~/.claude/skills/token-optimizer/scripts/measure.py grok-install
```

Adjust the path if your `measure.py` lives elsewhere.

This writes Token Optimizer's OWN hook file to
`$GROK_HOME/hooks/token-optimizer.json` only (Grok scans `~/.grok/hooks/*.json`
as a directory of files, so TO owns its file outright and never merges with, or
clobbers, another tool's hooks). The adapter payload is copied to
`$GROK_HOME/token-optimizer/plugin/`. Nothing outside `$GROK_HOME` is touched.

### WSL / non-default home: set `TOKEN_OPTIMIZER_GROK_HOME`, never `GROK_HOME`

`GROK_HOME` is Grok Build's **own** configuration variable. Setting it to a WSL
`/mnt/...` path (meaningless to the native-Windows CLI) breaks Grok's own
session persistence. Point Token Optimizer with its collision-free override:

```bash
TOKEN_OPTIMIZER_GROK_HOME=/mnt/c/Users/<you>/.grok bash install.sh --grok
```

`TOKEN_OPTIMIZER_GROK_HOME` steers only Token Optimizer; Grok never reads it.

## The documented hook contract (static capability gate)

Grok Build's hook contract is documented in one place — the cloned repo's
`crates/codegen/xai-grok-pager/docs/user-guide/10-hooks.md` — and the adapter
gates every feature on that documented contract. The matrix is **static** here
(no `grok --version` is probed, because there is no live host), so it is the
documented contract, not an observed version matrix.

| Hook power | Documented | Source | TO feature |
|---|---|---|---|
| PreToolUse `updatedInput` | yes | 10-hooks.md "Output (Blocking Hooks)" | bash output compression |
| PostToolUse `additionalContext` | yes | 10-hooks.md "PostToolUse Output" | context-growth nudges |
| SessionStart context | no (stdout ignored) | 10-hooks.md "Passive Hooks" | not used — setup only |
| UserPromptSubmit context | no (allowing stdout discarded) | 10-hooks.md "UserPromptSubmit Decision Control" | not used — observe only |

TO never emits `deny`/`block`: every handler is fail-open, and a timed-out or
crashed hook never blocks a tool call (10-hooks.md "How a Hook Resolves",
step 4).

## What Grok exposes to companions

| Surface | Authoritative | Never |
|---|---|---|
| Per-turn token + cost totals | `sessions/<group>/<uuid>/updates.jsonl` `turn_completed` usage | mixing with signals |
| Session identity/summary | `sessions/<group>/<uuid>/summary.json` | — |
| Signals (turns, tools, context, compaction) | `sessions/<group>/<uuid>/signals.json` | — |

Token resolution: `inputTokens` in `turn_completed` is the full prompt sum
including cache reads, so `cache_read` is a subset of `input` and is never
re-added. When a session has no `turn_completed` usage (crash before first turn
end), the reader falls back to `signals.json` `contextTokensUsed` as an input
estimate and flags it `~est.` — never dropping a session.

## What Grok does not expose (honest gaps)

| Gap | Why | Tracking |
|---|---|---|
| Cache-TTL / collapse waste | updates.jsonl has per-turn cache tokens but no wired per-turn cache+timestamp read path in the Python engine | `_CACHE_COVERAGE_GAP_REASONS` (grok entry) |
| Continuity restore at SessionStart | Grok ignores SessionStart stdout (no `additionalContext`) | 10-hooks.md "Passive Hooks" |
| Per-prompt quality steering | allowing UserPromptSubmit stdout is discarded | 10-hooks.md |

## Needs live verification

Every claim below is derived from the cloned source and **has not been observed
on a live Grok host**. A future tester should confirm each one with a single
short Grok session; `grok-doctor --probe` covers the hook-firing items offline.

1. `updates.jsonl` `turn_completed` lines actually carry the `SessionNotification` camelCase envelope with a snake_case-tagged `update.sessionUpdate == "turn_completed"`.
2. `summary.json` matches the `Summary` struct in `persistence.rs` (`info.id`, `info.cwd`, session summary, created/updated timestamps, message counts, current model id, agent name). Upstream currently serializes it with plain `serde_json`, so the field names are snake_case; the adapter also accepts the camelCase spelling older builds wrote.
3. `signals.json` matches `SessionSignals` camelCase (`turnCount`, `toolCallCount`, `toolsUsed`, `modelsUsed`, `primaryModelId`, `contextTokensUsed`, `contextWindowTokens`, `contextWindowUsage`, `compactionCount`, `sessionDurationSeconds`).
4. Hook stdin actually uses the camelCase envelope (`hookEventName` snake_case value, `hook_event_name` PascalCase value, `sessionId`, `cwd`, `workspaceRoot`, `timestamp`, `permissionMode`, `promptId`; tool events add `toolName`/`toolInput`/`toolUseId`).
5. PreToolUse `updatedInput` and PostToolUse `additionalContext` actually reach the model (the `--probe` proves the wire exits 0, not that Grok applies the fields).
6. There is no clean SessionEnd marker in the session store, so `incomplete` is always False in contract-only reads (updates.jsonl only marks per-turn `usageIsIncomplete`).
7. `costUsdTicks` is 1e10 ticks per USD and is scrubbed (absent) when `usageIsIncomplete` or `costIsPartial`; absence of cost means unknown, not free.

## Commands

```bash
measure.py grok-install     # wire hooks into $GROK_HOME/hooks/ + copy payload
measure.py grok-doctor      # readiness report + --probe (replays documented payloads)
measure.py grok-summary     # token/cost session summary
measure.py grok-rollup      # ingest sessions into trends.db (auto on stop hook)
measure.py grok-uninstall   # remove only what we installed
measure.py grok-home        # print the resolved Grok home (honors GROK_HOME)
```

## Uninstall

```bash
TOKEN_OPTIMIZER_RUNTIME=grok python3 skills/token-optimizer/scripts/measure.py grok-uninstall
```

Removes only `$GROK_HOME/hooks/token-optimizer.json` and the
`$GROK_HOME/token-optimizer/plugin/` payload. Grok's own session data and other
tools' hooks are left intact. Idempotent.

To purge Token Optimizer's own data too: `rm -rf $GROK_HOME/token-optimizer`.

## Live-smoke runbook (first run on a machine with Grok Build)

1. Install Grok Build and authenticate.
2. `bash install.sh --grok`.
3. `measure.py grok-doctor` — the binary check should turn green; `--probe`
   should show 5/5 wired events firing (exit 0).
4. Run one short Grok session that executes a shell command and then ends.
5. Confirm each item under [Needs live verification](#needs-live-verification)
   against the produced `sessions/<group>/<uuid>/` files and the hook
   `observed-events.jsonl` ledger.
6. If a whitelisted command (e.g. `git status`) ran, check Grok's transcript:
   the command should have been wrapped through `bash_compress.py`. If it ran
   unwrapped, `updatedInput` is not being applied on this build — file it.
