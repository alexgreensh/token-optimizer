#!/usr/bin/env node
// Token Optimizer - Claude Code Status Line (two-row layout)
//
// Row 1: model | effort | project | context bar used% | ContextQ:grade(score)
// Row 2: Eff:grade(score) | warnings | Compacts:N(loss) | duration | Agents
//
// Install: python3 measure.py setup-quality-bar
// The quality score is updated by a UserPromptSubmit hook every ~2 minutes.
// Reads from the most recent per-session quality-cache-*.json for accuracy.
// Effort level comes from the stdin payload (data.effort.level) when Claude
// Code provides it, falling back to settings.json effortLevel on older versions.

const fs = require('fs');
const path = require('path');
const os = require('os');
const { execFileSync } = require('child_process');

// --- Self-disabling guard (issue #106 / F1) ---
// Claude Code has NO plugin uninstall/teardown hook, so a `/plugin uninstall`
// (or a manual `rm -rf` of the plugin tree) can leave this statusLine command
// pointing at a script whose plugin tree has been removed. A missing command
// makes the status line go silently blank with no error surfaced to the user
// (visible only under `claude --debug`). Rather than emit a broken-command
// state, we self-disable: if our own plugin tree is gone (this script's
// directory no longer holds the sibling files a real install always ships),
// exit 0 with no output. A blank status line is exactly what a missing command
// already produces, so this changes nothing while installed and removes the
// dangling-reference state after removal. The guard runs BEFORE reading stdin
// so a deleted tree never pays the parse cost.
//
// We do NOT change the command string shape for existing installs (per the
// locked design decision): the command stays `node '<path>/statusline.js'`.
// When the script file itself is deleted, node fails to load it before this
// guard runs, and the host already renders that as a blank line, so the guard
// targets the partial-removal case (script present, tree gutted).
function _pluginTreeGone() {
  // __dirname is this script's directory: <tree>/skills/token-optimizer/scripts
  // (plugin cache) or <repo>/skills/token-optimizer/scripts (dev/script install).
  // measure.py is the canonical sibling every real install ships alongside
  // statusline.js. If it is gone while statusline.js remains, the plugin tree
  // is being or has been removed, and a working status line would be a lie.
  try {
    if (!fs.existsSync(path.join(__dirname, 'measure.py'))) return true;
  } catch (e) { return true; }
  return false;
}

// --- Clone-path uninstall guard (issue #106 / F1, G3 C-P2-2) ---
// For plugin-cache installs the statusLine command now points at the marketplace
// CLONE (<claude>/plugins/marketplaces/<mkt>/skills/token-optimizer/scripts/
// statusline.js) so it survives version bumps. But a native `/plugin uninstall
// token-optimizer` removes only the plugin's CACHE tree
// (<claude>/plugins/cache/<mkt>/token-optimizer/<ver>) and leaves the shared
// marketplace clone in place. The clone still ships measure.py next to
// statusline.js, so _pluginTreeGone() stays false and we would render a working
// "ghost" status line for an uninstalled plugin. Detect that: when we are
// running FROM a marketplace clone and no token-optimizer cache install remains
// under the same <plugins> root, the plugin has been uninstalled -> self-disable.
function _pluginUninstalledFromClone() {
  try {
    const parts = __dirname.split(path.sep);
    const mi = parts.lastIndexOf('marketplaces');
    if (mi <= 0) return false;               // not running from a clone
    const marketplace = parts[mi + 1];
    if (!marketplace) return false;
    const pluginsDir = parts.slice(0, mi).join(path.sep);  // <claude>/plugins
    const cachePluginDir = path.join(pluginsDir, 'cache', marketplace, 'token-optimizer');
    // A live plugin-cache install has at least one <version> dir here. The dir
    // gone (or emptied) means `/plugin uninstall` removed the cache tree.
    if (!fs.existsSync(cachePluginDir)) return true;
    return fs.readdirSync(cachePluginDir).length === 0;
  } catch (e) {
    // Never blank a healthy line on an unexpected error: a lingering ghost is
    // strictly less harmful than blanking a working status line.
    return false;
  }
}

if (_pluginTreeGone() || _pluginUninstalledFromClone()) {
  process.exit(0);
}

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

    let branch = '';
    try {
      const b = execFileSync('git', ['branch', '--show-current'], {
        cwd: dir,
        encoding: 'utf8',
        timeout: 200,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'ignore'],
      }).trim();
      if (b) branch = `${SEP}${DIM}(${b})${RESET}`;
    } catch (e) {}
    const gradeFor = (s) => s >= 90 ? 'S' : s >= 80 ? 'A' : s >= 70 ? 'B' : s >= 55 ? 'C' : s >= 40 ? 'D' : 'F';

    // Effort level: prefer the LIVE session value Claude Code now passes in the
    // statusline stdin (data.effort.level — reflects mid-session /effort changes,
    // values low/medium/high/xhigh/max). Fall back to settings.json effortLevel
    // for older Claude Code versions that don't send it (a session-only /effort
    // there won't show, but it's the best available). 'max' is short enough to
    // wear its own name.
    let effort = '';
    try {
      let level = data.effort?.level;
      if (!level) {
        const settingsPath = path.join(os.homedir(), '.claude', 'settings.json');
        if (fs.existsSync(settingsPath)) {
          const settings = JSON.parse(fs.readFileSync(settingsPath, 'utf8'));
          level = settings.effortLevel;
        }
      }
      if (level) {
        const effortMap = { low: 'lo', medium: 'med', high: 'hi', xhigh: 'xhi' };
        const effortLabel = effortMap[level] || level;
        effort = `${SEP}${DIM}${effortLabel}${RESET}`;
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
    // Row 1 as BARE segments (no baked-in SEP) so a narrow terminal can reflow them
    // across physical rows instead of the host clipping the overflow. The effort/
    // branch/ctx/qScore builders each prepend SEP, so strip that leading separator;
    // packRows re-inserts it between whatever segments land on the same row.
    const _stripLeadSep = s => (s && s.startsWith(SEP) ? s.slice(SEP.length) : s);
    const row1Segs = [
      `${DIM}${model}${RESET}`,
      _stripLeadSep(effort),
      `${DIM}${dirname}${RESET}`,
      _stripLeadSep(branch),
      _stripLeadSep(ctx),
      _stripLeadSep(qScore),
    ].filter(Boolean);

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
      const p = Math.ceil(fiveHour.used_percentage);
      row2Parts.push(`${limitColor(p)}5h:${p}%${fmtReset(fiveHour.resets_at)}${RESET}`);
    }
    if (sevenDay) {
      const p = Math.ceil(sevenDay.used_percentage);
      row2Parts.push(`${DIM}7d:${p}%${RESET}`);
    }

    // Responsive layout: when Claude Code exports COLUMNS (v2.1.153+), greedily pack
    // each row's segments into physical rows no wider than the terminal, so narrow
    // windows drop segments to the next line instead of the host clipping them.
    // COLUMNS unset (older Claude Code) -> the exact prior two-row output, byte-for-byte.
    const vlen = s => stripAnsi(s).length;   // visible width; SEP (" | ") is 3 cols
    const packRows = (segs, width) => {
      const rows = [];
      let cur = '';
      for (const seg of segs) {
        if (cur === '') { cur = seg; continue; }
        if (vlen(cur) + 3 + vlen(seg) <= width) cur += SEP + seg;
        else { rows.push(cur); cur = seg; }   // a lone over-wide segment still clips (unsplittable)
      }
      if (cur !== '') rows.push(cur);
      return rows;
    };
    const _cols = parseInt(process.env.COLUMNS, 10);
    const _width = Number.isFinite(_cols) && _cols > 4 ? _cols : null;
    if (_width) {
      const rows = [...packRows(row1Segs, _width), ...packRows(row2Parts, _width)];
      process.stdout.write(rows.join('\n'));
    } else {
      process.stdout.write(`${row1Segs.join(SEP)}\n${row2Parts.join(SEP)}`);
    }
  } catch (e) {
    // Silent fail - never break the status line
  }
});
