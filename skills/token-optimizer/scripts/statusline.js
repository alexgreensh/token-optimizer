#!/usr/bin/env node
// Token Optimizer - Claude Code Status Line
// Shows: model | effort | project | context bar used% | ContextQ:score | Compacts:N(loss)
//
// Install: python3 measure.py setup-quality-bar
// The quality score is updated by a UserPromptSubmit hook every ~2 minutes.
// Reads from the most recent per-session quality-cache-*.json for accuracy.
// Falls back to quality-cache.json (global) if no per-session cache found.
// Reads effortLevel from settings.json (not available in stdin data).

const fs = require('fs');
const path = require('path');
const os = require('os');

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', () => {
  try {
    const data = JSON.parse(input);
    const model = data.model?.display_name || 'Claude';
    const dir = data.workspace?.current_dir || process.cwd();
    const remaining = data.context_window?.remaining_percentage;
    const usedPct = data.context_window?.used_percentage;
    const sessionId = data.session_id;
    const DIM = '\x1b[2m';
    const RESET = '\x1b[0m';
    const SEP = ` ${DIM}|${RESET} `;

    // Effort level (read from settings.json, not in stdin data)
    let effort = '';
    try {
      const settingsPath = path.join(os.homedir(), '.claude', 'settings.json');
      if (fs.existsSync(settingsPath)) {
        const settings = JSON.parse(fs.readFileSync(settingsPath, 'utf8'));
        const level = settings.effortLevel;
        if (level) {
          const effortMap = { low: 'lo', medium: 'med', high: 'hi' };
          const effortLabel = effortMap[level] || level;
          effort = `${SEP}${DIM}${effortLabel}${RESET}`;
        }
      }
    } catch (e) {}

    // Context window bar with degradation-aware colors
    // Context fill bands: <50% = green, 50-70% = yellow, 70-80% = orange, 80%+ = red
    let ctx = '';
    const used = usedPct != null
      ? Math.round(usedPct)
      : (remaining != null ? Math.max(0, Math.min(100, 100 - Math.round(remaining))) : null);

    if (used != null) {
      const filled = Math.floor(used / 10);
      const bar = '\u2588'.repeat(filled) + '\u2591'.repeat(10 - filled);

      if (used < 50) {
        ctx = `${SEP}\x1b[32m${bar} ${used}%${RESET}`;
      } else if (used < 70) {
        ctx = `${SEP}\x1b[33m${bar} ${used}%${RESET}`;
      } else if (used < 80) {
        ctx = `${SEP}\x1b[38;5;208m${bar} ${used}%${RESET}`;
      } else {
        ctx = `${SEP}\x1b[5;31m${bar} ${used}%${RESET}`;
      }
    }

    // Quality score + compaction info from quality cache
    // Priority: per-session cache (by session_id) > most recent per-session > global fallback
    let qScore = '';
    let sessionInfo = '';
    const cacheDir = path.join(os.homedir(), '.claude', 'token-optimizer');

    let q = null;
    try {
      // Try per-session cache by session_id first
      if (sessionId) {
        const sessionCache = path.join(cacheDir, `quality-cache-${sessionId}.json`);
        if (fs.existsSync(sessionCache)) {
          q = JSON.parse(fs.readFileSync(sessionCache, 'utf8'));
        }
      }

      // Fall back to most recently modified per-session cache
      if (!q) {
        try {
          const files = fs.readdirSync(cacheDir)
            .filter(f => f.startsWith('quality-cache-') && f.endsWith('.json'))
            .map(f => ({ name: f, mtime: fs.statSync(path.join(cacheDir, f)).mtimeMs }))
            .sort((a, b) => b.mtime - a.mtime);
          if (files.length > 0) {
            q = JSON.parse(fs.readFileSync(path.join(cacheDir, files[0].name), 'utf8'));
          }
        } catch (e) {}
      }

      // Final fallback: global cache
      if (!q) {
        const qFile = path.join(cacheDir, 'quality-cache.json');
        if (fs.existsSync(qFile)) {
          q = JSON.parse(fs.readFileSync(qFile, 'utf8'));
        }
      }

      if (q) {
        const s = q.score;
        if (s != null) {
          const score = Math.round(s);
          if (score >= 85) {
            qScore = `${SEP}\x1b[32mContextQ:${score}${RESET}`;
          } else if (score >= 70) {
            qScore = `${SEP}${DIM}ContextQ:${score}${RESET}`;
          } else if (score >= 50) {
            qScore = `${SEP}\x1b[33mContextQ:${score}${RESET}`;
          } else {
            qScore = `${SEP}\x1b[31mContextQ:${score}${RESET}`;
          }
        }

        // Compaction count with cumulative loss (read from quality cache, single source of truth)
        const c = q.compactions;
        if (c != null && c > 0) {
          const lossPct = q.breakdown?.compaction_depth?.cumulative_loss_pct;
          const loss = lossPct ? `~${Math.round(lossPct)}%` : (c >= 3 ? '~95%' : c >= 2 ? '~88%' : '~65%');
          const color = c <= 2 ? '\x1b[33m' : '\x1b[31m';
          sessionInfo = `${SEP}${color}Compacts:${c}(${loss} lost)${RESET}`;
        }
      }
    } catch (e) {}

    const dirname = path.basename(dir);
    process.stdout.write(`${DIM}${model}${RESET}${effort}${SEP}${DIM}${dirname}${RESET}${ctx}${qScore}${sessionInfo}`);
  } catch (e) {
    // Silent fail - never break the status line
  }
});
