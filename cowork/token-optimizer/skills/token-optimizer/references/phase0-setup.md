# Phase 0: Setup Details

## Contents

- [Context Window Detection](#context-window-detection)
- [Quick Pre-Check](#quick-pre-check)
- [Backup](#backup)
- [Coordination Folder](#coordination-folder)
- [SessionEnd Hook Check](#sessionend-hook-check)
- [Dashboard Daemon](#dashboard-daemon)
- [Smart Compaction Check](#smart-compaction-check)

---

## Context Window Detection

Check if `TOKEN_OPTIMIZER_CONTEXT_SIZE` env var is already set. If not:
- Most current models have a 1M window by default (Haiku 5.5, Sonnet 5 and later, Opus 4.7 and later, Fable). Sonnet 4.6 and Opus 4.6 have 1M only through the `[1m]` variant and 200K without it.
- Let `measure.py` detect the window from the session's model. Only if the user says it is wrong, ask which window they run and `export TOKEN_OPTIMIZER_CONTEXT_SIZE=<tokens>` (for example `1000000`) for this session.
Keep this quick, one question max.

## Quick Pre-Check

Run `python3 $MEASURE_PY report`.
If estimated controllable tokens < 1,000 and no CLAUDE.md exists, short-circuit:
```
[Token Optimizer] Your setup is already minimal (~X tokens overhead).
Focus on behavioral changes instead: /compact at natural breakpoints, /clear between topics,
route data-gathering agents to a lighter model, batch requests.
```

## Backup

```bash
BACKUP_DIR="$HOME/.claude/_backups/token-optimizer-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
cp ~/.claude/CLAUDE.md "$BACKUP_DIR/" 2>/dev/null || true
cp ~/.claude/settings.json "$BACKUP_DIR/" 2>/dev/null || true
cp -r ~/.claude/commands "$BACKUP_DIR/" 2>/dev/null || true
for memfile in ~/.claude/projects/*/memory/MEMORY.md; do
  if [ -f "$memfile" ]; then
    projname=$(basename "$(dirname "$(dirname "$memfile")")")
    cp "$memfile" "$BACKUP_DIR/MEMORY-${projname}.md" 2>/dev/null || true
  fi
done

if [ -z "$(ls -A "$BACKUP_DIR" 2>/dev/null)" ]; then
  echo "[Warning] Backup directory is empty. No files were backed up."
fi
```

## Coordination Folder

```bash
# Project-local, so the audit files live with the work. Keep it out of git:
# echo '.token-optimizer-audit-*/' >> .git/info/exclude
COORD_PATH=$(mktemp -d "$PWD/.token-optimizer-audit-XXXXXXXXXX")
[ -d "$COORD_PATH" ] || { echo "[Error] Failed to create coordination folder."; exit 1; }
mkdir -p "$COORD_PATH"/{audit,analysis,plan,verification}
```

## SessionEnd Hook Check

```bash
python3 $MEASURE_PY check-hook
```
- Exit 0: already installed, skip to Phase 1.
- Exit 1 (manual/script install users only): explain and offer to install:

```
[Token Optimizer] Want to track your token usage over time?

Right now, the optimizer can audit your setup. But to track *trends* (which
skills you actually use, how your context fills up day to day, model costs),
it needs to save a small log after each Claude Code session.

What this does:
- When you close a Claude Code session, it automatically saves usage stats
- Takes ~2 seconds, runs silently in the background, then stops
- All data stays on your machine (stored in ~/.claude/_backups/token-optimizer/)
- Powers the Trends and Health tabs in your dashboard

Remove anytime: python3 measure.py setup-hook --uninstall
```

Ask user: 1) Install (dry-run first), 2) Show JSON first, 3) Skip

## Dashboard Daemon

Run BOTH probes in one pass:
```bash
python3 "$MEASURE_PY" daemon-status
python3 "$MEASURE_PY" daemon-consent --get
```

`daemon-status` prints `DAEMON_RUNNING`, `DAEMON_FOREIGN`, or `DAEMON_NOT_RUNNING`.
`daemon-consent --get` prints JSON: `{}` (never prompted) or `{"prompted": true, "consent": true|false}`.

| Daemon \ Consent | unrecorded | `consent: true` | `consent: false` |
|---|---|---|---|
| `DAEMON_RUNNING` | skip; lead with URL next time | skip; URL works | offer `setup-daemon --uninstall` once |
| `DAEMON_FOREIGN` | prompt with port conflict warning | note conflict | skip silently |
| `DAEMON_NOT_RUNNING` | first-time install prompt | offer to reinstall | skip silently |

First-time install prompt:
```
[Token Optimizer] Want a bookmarkable dashboard URL?

After you run `setup-daemon`, the dashboard is served at:
  URL:   http://localhost:24842/token-optimizer
Until then, open the file path `measure.py dashboard` prints on its `  Dashboard: ` line. The location is install-dependent, so read it from the output rather than assuming a literal path.

What installing the URL does:
- Runs a tiny web server on your machine (~2MB memory)
- Starts automatically at login, restarts if it ever stops
- Only reachable from this machine (localhost)

Remove anytime: python3 measure.py setup-daemon --uninstall
```

Ask user: 1) Install (write consent first, then install), 2) Skip (set consent no)
On Linux/BSD: skip silently, mention `file://` URL works.

## Smart Compaction Check

```bash
python3 $MEASURE_PY setup-smart-compact --status
```
- All 4 hooks installed: skip entirely.
- Partially or not installed: explain and offer:

```
[Token Optimizer] Smart Compaction captures session state BEFORE compaction fires,
then restores it afterward. Decisions, modified files, errors, agent state.

Remove anytime: python3 measure.py setup-smart-compact --uninstall
```

Ask user: 1) Install (dry-run first), 2) Show JSON first, 3) Skip

Output: `[Token Optimizer Initialized] Backup: $BACKUP_DIR | Coordination: $COORD_PATH`
