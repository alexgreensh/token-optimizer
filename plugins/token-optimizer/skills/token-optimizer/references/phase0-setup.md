# Phase 0: Setup Details

## Context Window Detection

Check if `TOKEN_OPTIMIZER_CONTEXT_SIZE` env var is already set. If not:
- Check for `ANTHROPIC_API_KEY` env var (indicates API usage, possibly 1M context)
- If API key found, ask the user: "You appear to be using the API. Do you have 1M token context (e.g. Opus)? If so I'll calibrate for 1M instead of 200K."
- If they confirm 1M, `export TOKEN_OPTIMIZER_CONTEXT_SIZE=1000000` for this session
- If no API key or they say no, default is 200K (no action needed)
Keep this quick, one question max.

## Quick Pre-Check

Run `python3 $MEASURE_PY report`.
If estimated controllable tokens < 1,000 and no CLAUDE.md exists, short-circuit:
```
[Token Optimizer] Your setup is already minimal (~X tokens overhead).
Focus on behavioral changes instead: /compact at 70%, /clear between topics,
default agents to haiku, batch requests.
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
COORD_PATH=$(mktemp -d /tmp/token-optimizer-XXXXXXXXXX)
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

URL:  http://localhost:24842/token-optimizer
File: ~/.claude/_backups/token-optimizer/dashboard.html  (always works)

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
