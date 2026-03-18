#!/usr/bin/env node
// Token Optimizer - Claude Code Status Line
// Shows: model | effort | project | context bar used% | ContextQ:score (duration) | Compacts:N(loss) | Agents
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

    // Cache directory (declared early, used by live-fill write and quality score read)
    const cacheDir = path.join(os.homedir(), '.claude', 'token-optimizer');

    // Context window bar with degradation-aware colors
    // Context fill bands: <50% = green, 50-70% = yellow, 70-80% = orange, 80%+ = red
    let ctx = '';
    const used = usedPct != null
      ? Math.round(usedPct)
      : (remaining != null ? Math.max(0, Math.min(100, 100 - Math.round(remaining))) : null);

    // Sanitize session_id for safe use in filesystem paths
    const safeSessionId = sessionId ? sessionId.replace(/[^a-zA-Z0-9_-]/g, '') : null;

    if (used != null) {
      const clamped = Math.max(0, Math.min(100, used));
      const filled = Math.floor(clamped / 10);
      const bar = '\u2588'.repeat(filled) + '\u2591'.repeat(10 - filled);

      if (clamped < 50) {
        ctx = `${SEP}\x1b[32m${bar} ${clamped}%${RESET}`;
      } else if (clamped < 70) {
        ctx = `${SEP}\x1b[33m${bar} ${clamped}%${RESET}`;
      } else if (clamped < 80) {
        ctx = `${SEP}\x1b[38;5;208m${bar} ${clamped}%${RESET}`;
      } else {
        ctx = `${SEP}\x1b[5;31m${bar} ${clamped}%${RESET}`;
      }

      // Write live fill data for quality score to use (bridges statusline -> quality cache)
      try {
        const liveFillData = JSON.stringify({
          used_percentage: clamped,
          timestamp: Date.now(),
          session_id: sessionId || null
        });
        const tmpPath = path.join(cacheDir, '.live-fill.tmp');
        fs.writeFileSync(tmpPath, liveFillData);
        fs.renameSync(tmpPath, path.join(cacheDir, 'live-fill.json'));
      } catch (e) {}
    }

    // Quality score + compaction info from quality cache
    // ONLY show data from the current session's cache. Never fall back to
    // another session's data (causes stale Compacts/ContextQ on fresh sessions).
    let qScore = '';
    let sessionInfo = '';

    let q = null;
    try {
      // Try per-session cache by session_id (exact match only)
      if (safeSessionId) {
        const sessionCache = path.join(cacheDir, `quality-cache-${safeSessionId}.json`);
        if (fs.existsSync(sessionCache)) {
          q = JSON.parse(fs.readFileSync(sessionCache, 'utf8'));
        }
      }

      if (q) {
        const s = q.score;
        if (s != null) {
          const score = Math.round(s);
          // Color bands: green >=85, yellow 75-84, orange 50-74, red <50
          if (score >= 85) {
            qScore = `${SEP}\x1b[32mContextQ:${score}${RESET}`;
          } else if (score >= 75) {
            qScore = `${SEP}\x1b[33mContextQ:${score}${RESET}`;
          } else if (score >= 50) {
            qScore = `${SEP}\x1b[38;5;208mContextQ:${score}${RESET}`;
          } else {
            qScore = `${SEP}\x1b[31mContextQ:${score}${RESET}`;
          }
        }

        // Compaction count with cumulative loss
        const c = q.compactions;
        if (c != null) {
          if (c > 0) {
            const lossPct = q.breakdown?.compaction_depth?.cumulative_loss_pct;
            const loss = lossPct ? `~${Math.round(lossPct)}%` : (c >= 3 ? '~95%' : c >= 2 ? '~88%' : '~65%');
            const color = c <= 2 ? '\x1b[33m' : '\x1b[31m';
            sessionInfo = `${SEP}${color}Compacts:${c}(${loss} lost)${RESET}`;
          } else {
            sessionInfo = `${SEP}\x1b[32mCompacts:0${RESET}`;
          }
        }
      } else {
        // No cache for this session yet. Show pending (SessionStart hook will create it shortly).
        qScore = `${SEP}${DIM}ContextQ:--${RESET}`;
      }
    } catch (e) {}

    // Session duration - show only when quality < 75 AND cache matches current session
    // Without the session match check, all terminals show the same stale duration
    let duration = '';
    const cacheMatchesSession = q && q.session_file && safeSessionId && q.session_file.includes(safeSessionId);
    if (cacheMatchesSession && q.session_start_ts && q.score != null && q.score < 75) {
      const elapsed = Math.floor((Date.now() / 1000) - q.session_start_ts);
      if (elapsed > 0) {
        const h = Math.floor(elapsed / 3600);
        const m = Math.floor((elapsed % 3600) / 60);
        const dur = h > 0 ? `${h}h${m}m` : `${m}m`;
        duration = ` ${DIM}(${dur})${RESET}`;
      }
    }

    // Active agents - show running agents with model
    // Strip ANSI escape codes from agent data (defense-in-depth against JSONL injection)
    const stripAnsi = s => String(s).replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '').replace(/[\x00-\x1f]/g, '');
    let agents = '';
    if (cacheMatchesSession && q.active_agents && q.active_agents.length > 0) {
      const running = q.active_agents.filter(a => a.status === 'running');
      if (running.length > 0) {
        const agentParts = running.slice(0, 3).map(a => {
          const m = stripAnsi(a.model || '?');
          const desc = stripAnsi(a.description || '');
          let elapsed = '';
          if (a.start_time) {
            try {
              const secs = Math.floor((Date.now() - new Date(a.start_time).getTime()) / 1000);
              elapsed = secs >= 60 ? `${Math.floor(secs / 60)}m${secs % 60}s` : `${secs}s`;
            } catch (e) {}
          }
          return `\x1b[33m${m}\x1b[0m:${desc}${elapsed ? '(' + elapsed + ')' : ''}`;
        });
        agents = `${SEP}Agents: ${agentParts.join(' ')}`;
      }
    }

    const dirname = path.basename(dir);
    process.stdout.write(`${DIM}${model}${RESET}${effort}${SEP}${DIM}${dirname}${RESET}${ctx}${qScore}${duration}${sessionInfo}${agents}`);
  } catch (e) {
    // Silent fail - never break the status line
  }
});
