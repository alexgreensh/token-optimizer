# Error Handling & Recovery

This file covers the Claude Code audit phases (0-5). For Codex error handling, see `codex-workflow.md`.

## Error Handling

- **Agent timeout/failure**: Note the gap and continue. Do not retry. Synthesis handles missing files.
- **No CLAUDE.md found**: Report 0 tokens, skip to skills audit.
- **No skills directory**: Report 0 tokens, note as "fresh setup."
- **measure.py not found**: Fall back to manual estimation (line count x 15 for prose, x 8 for YAML).
- **Coordination folder write failure**: Abort and report. Do not proceed without audit storage.
- **Backup write failure**: Warn user and ask whether to proceed without backup.
- **mktemp failure**: Print error and abort. Check /tmp permissions.
- **Synthesis agent failure**: Present raw audit files to user. Do not proceed to Phase 4 blindly.
- **Verification agent failure**: Fall back to `measure.py snapshot after` + `measure.py compare`.
- **Snapshot file corrupt**: Re-run `measure.py snapshot [label]` to regenerate.
- **Stale snapshot warning**: If "before" snapshot is >24h old, warn. Consider re-taking.

## Restoring Backups

```bash
ls -ltd ~/.claude/_backups/token-optimizer-* | head -5

BACKUP="$HOME/.claude/_backups/token-optimizer-TIMESTAMP"
cp "$BACKUP/CLAUDE.md" ~/.claude/CLAUDE.md
cp "$BACKUP/settings.json" ~/.claude/settings.json
cp -r "$BACKUP/commands" ~/.claude/commands
for f in "$BACKUP"/MEMORY-*.md; do
  [ -f "$f" ] || continue
  projname="${f##*/MEMORY-}"; projname="${projname%.md}"
  case "$projname" in *..* | */* ) echo "[Warning] Skipping suspicious backup: $f"; continue ;; esac
  [ -d "$HOME/.claude/projects/${projname}/memory" ] || continue
  cp "$f" "$HOME/.claude/projects/${projname}/memory/MEMORY.md"
done
```

Backups are never automatically deleted. They accumulate in `~/.claude/_backups/`.
