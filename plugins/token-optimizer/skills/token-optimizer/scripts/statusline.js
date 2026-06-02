#!/usr/bin/env node
// Token Optimizer - Claude Code Status Line (two-row layout)
//
// Row 1: model | effort | project | context bar used% | ContextQ:grade(score)
// Row 2: Eff:grade(score) | warnings | Compacts:N(loss) | duration | Agents
//
// Install: python3 measure.py setup-quality-bar
// The quality score is updated by a UserPromptSubmit hook every ~2 minutes.
// Reads from the most recent per-session quality-cache-*.json for accuracy.
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
    // Account-global usage limits (Pro/Max only, populated after first API
    // response; each window may be independently absent). Handed only to the
    // status line, persisted nowhere else, so we bridge it to a sidecar file
    // for the VS Code companion (rate limits are account-wide, not per-session).
    const rateLimits = data.rate_limits || null;
    // A readable medium grey instead of ANSI faint (\x1b[2m), which renders too
    // low-contrast for secondary info like session time on most terminals.
    const DIM = '\x1b[38;5;245m';
    const RESET = '\x1b[0m';
    const SEP = ` ${DIM}|${RESET} `;
    const gradeFor = (s) => s >= 90 ? 'S' : s >= 80 ? 'A' : s >= 70 ? 'B' : s >= 55 ? 'C' : s >= 40 ? 'D' : 'F';

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
    // Fill bands: <50% green, 50-70% yellow, 70-80% orange, 80%+ red (blinking)
    let ctx = '';
    const used = usedPct != null
      ? Math.round(usedPct)
      : (remaining != null ? Math.max(0, Math.min(100, 100 - Math.round(remaining))) : null);

    // Sanitize session_id for safe use in filesystem paths
    const safeSessionId = sessionId ? sessionId.replace(/[^a-zA-Z0-9_-]/g, '') : null;

    if (used != null) {
      const clamped = Math.max(0, Math.min(100, used));
      const filled = Math.floor(clamped / 10);
      const bar = '█'.repeat(filled) + '░'.repeat(10 - filled);

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
        // PID-scoped tmp so two concurrent terminals never write the same
        // temp file (which would corrupt one another's rename).
        const tmpPath = path.join(cacheDir, `.live-fill.${process.pid}.tmp`);
        fs.writeFileSync(tmpPath, liveFillData);
        fs.renameSync(tmpPath, path.join(cacheDir, 'live-fill.json'));
      } catch (e) {}
    }

    // ---- Write authoritative rate limits to a global sidecar ----
    // Bridges status-line-only data (used %, reset time) to disk so the VS Code
    // companion can show authoritative usage limits with no terminal of its own.
    // Account-global file (not per-session): rate limits are account-wide.
    const pickWindow = (w) => {
      if (!w || typeof w.used_percentage !== 'number' || !isFinite(w.used_percentage)) return null;
      return {
        used_percentage: Math.max(0, Math.min(100, w.used_percentage)),
        resets_at: typeof w.resets_at === 'number'
          ? w.resets_at
          : (typeof w.resets_at === 'string'
              ? (Math.floor(Date.parse(w.resets_at) / 1000) || null)
              : null)
      };
    };
    const fiveHour = rateLimits ? pickWindow(rateLimits.five_hour) : null;
    const sevenDay = rateLimits ? pickWindow(rateLimits.seven_day) : null;
    if (fiveHour || sevenDay) {
      try {
        const payload = JSON.stringify({
          five_hour: fiveHour,
          seven_day: sevenDay,
          timestamp: Date.now(),
          source: 'statusline'
        });
        const rlTmp = path.join(cacheDir, `.rate-limits.${process.pid}.tmp`);
        fs.writeFileSync(rlTmp, payload);
        fs.renameSync(rlTmp, path.join(cacheDir, 'rate-limits.json'));
      } catch (e) {}
    }

    // ---- Read quality cache ----
    let q = null;
    try {
      if (safeSessionId) {
        const sessionCache = path.join(cacheDir, `quality-cache-${safeSessionId}.json`);
        if (fs.existsSync(sessionCache)) {
          q = JSON.parse(fs.readFileSync(sessionCache, 'utf8'));
        }
      }
    } catch (e) {}

    const cacheMatchesSession = q && typeof q.session_file === 'string' && safeSessionId && q.session_file.includes(safeSessionId);

    // ---- ROW 1: Core identity + context health ----
    // Staleness guard: the score is recomputed by hooks (PostToolUse is
    // throttled to ~2min). If the cache is older than 5 min the displayed score
    // may not reflect recent activity, so mark it (~ prefix + dim) rather than
    // showing a frozen score as if it were live.
    let stale = false;
    if (q) {
      // Absent or unparseable timestamp => unknown age => treat as stale, so a
      // cache written by an older plugin version can't show as live.
      const ts = q.timestamp ? new Date(q.timestamp).getTime() : NaN;
      if (isNaN(ts) || (Date.now() - ts) / 1000 > 300) stale = true;
    }
    let qScore = '';
    if (q) {
      const rh = q.resource_health != null ? q.resource_health : q.score;
      if (rh != null) {
        const score = Math.round(rh);
        const grade = q.resource_health_grade || q.grade || gradeFor(score);
        // Keep the score's value-color (green/yellow/orange/red) regardless of
        // staleness; the `~` prefix is the only stale indicator so the number
        // stays readable. (Dimming the whole score made it barely visible.)
        const tag = `ContextQ:${stale ? '~' : ''}${grade}(${score})`;
        if (score >= 85) {
          qScore = `${SEP}\x1b[32m${tag}${RESET}`;
        } else if (score >= 75) {
          qScore = `${SEP}\x1b[33m${tag}${RESET}`;
        } else if (score >= 50) {
          qScore = `${SEP}\x1b[38;5;208m${tag}${RESET}`;
        } else {
          qScore = `${SEP}\x1b[31m${tag}${RESET}`;
        }
      }
    } else {
      qScore = `${SEP}${DIM}ContextQ:--${RESET}`;
    }

    const dirname = path.basename(dir);
    const row1 = `${DIM}${model}${RESET}${effort}${SEP}${DIM}${dirname}${RESET}${ctx}${qScore}`;

    // ---- ROW 2: Session details ----
    const row2Parts = [];

    // SessionEfficiency
    if (q) {
      const se = q.session_efficiency;
      if (se != null) {
        const seScore = Math.round(se);
        const seGrade = q.session_efficiency_grade || gradeFor(seScore);
        row2Parts.push(`${DIM}Eff:${seGrade}(${seScore})${RESET}`);
      }
    } else {
      row2Parts.push(`${DIM}Eff:--${RESET}`);
    }

    // Fill warning
    if (q) {
      const fw = q.fill_warning;
      if (fw && fw.level) {
        if (fw.level === 'CRITICAL') {
          row2Parts.push(`\x1b[5;31mFill:${Math.round(fw.fill_pct)}%!${RESET}`);
        } else if (fw.level === 'WARNING') {
          row2Parts.push(`\x1b[33mFill:${Math.round(fw.fill_pct)}%${RESET}`);
        }
      }

      // Tool call fatigue warning
      const tcw = q.tool_call_warning;
      if (tcw && tcw.level === 'CRITICAL') {
        row2Parts.push(`\x1b[31mTools:${q.tool_calls}!${RESET}`);
      } else if (tcw && tcw.level === 'WARNING') {
        row2Parts.push(`\x1b[33mTools:${q.tool_calls}${RESET}`);
      }
    }

    // Compaction count
    if (q) {
      const c = q.compactions;
      if (c != null) {
        if (c > 0) {
          const lossPct = q.breakdown?.compaction_depth?.cumulative_loss_pct;
          const loss = lossPct ? `~${Math.round(lossPct)}%` : (c >= 3 ? '~95%' : c >= 2 ? '~88%' : '~65%');
          const color = c <= 2 ? '\x1b[33m' : '\x1b[31m';
          row2Parts.push(`${color}Compacts:${c}(${loss} lost)${RESET}`);
        } else {
          row2Parts.push(`\x1b[32mCompacts:0${RESET}`);
        }
      }
    }

    // Session duration - ALWAYS shown when cache matches session
    if (cacheMatchesSession && q.session_start_ts > 0) {
      const elapsed = Math.floor((Date.now() / 1000) - q.session_start_ts);
      if (elapsed > 0 && elapsed < 604800) {
        const h = Math.floor(elapsed / 3600);
        const m = Math.floor((elapsed % 3600) / 60);
        const dur = h > 0 ? `${h}h${m}m` : `${m}m`;
        row2Parts.push(`${DIM}${dur}${RESET}`);
      }
    }

    // Active agents
    const stripAnsi = s => String(s).replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '').replace(/[\x00-\x1f]/g, '');
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
        row2Parts.push(`Agents: ${agentParts.join(' ')}`);
      }
    }

    // Usage limits row fragment (5h primary, 7d compact). Colored by pressure.
    const fmtReset = (epochSec) => {
      if (typeof epochSec !== 'number' || epochSec <= 0) return '';
      try {
        const d = new Date(epochSec * 1000);
        let h = d.getHours();
        const m = d.getMinutes();
        const ap = h >= 12 ? 'p' : 'a';
        h = h % 12; if (h === 0) h = 12;
        return ` ↺${h}:${String(m).padStart(2, '0')}${ap}`;
      } catch (e) { return ''; }
    };
    const limitColor = (pct) =>
      pct >= 90 ? '\x1b[5;31m' : pct >= 75 ? '\x1b[38;5;208m' : pct >= 50 ? '\x1b[33m' : '\x1b[32m';
    if (fiveHour) {
      const p = Math.round(fiveHour.used_percentage);
      row2Parts.push(`${limitColor(p)}5h:${p}%${fmtReset(fiveHour.resets_at)}${RESET}`);
    }
    if (sevenDay) {
      const p = Math.round(sevenDay.used_percentage);
      row2Parts.push(`${DIM}7d:${p}%${RESET}`);
    }

    const row2 = row2Parts.join(SEP);
    process.stdout.write(`${row1}\n${row2}`);
  } catch (e) {
    // Silent fail - never break the status line
  }
});
