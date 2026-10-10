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

// --- Self-disabling guard ---
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

// --- Clone-path uninstall guard ---
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

// Claude Code's config dir. Mirrors runtime_env.claude_home(): honor
// CLAUDE_CONFIG_DIR when it is an ABSOLUTE, EXISTING, NON-SYMLINK directory,
// else ~/.claude. Hard-coding ~/.claude here split state with the Python hooks
// for relocated configs (#198): live-fill/rate-limits landed in ~/.claude while
// the hooks read $CLAUDE_CONFIG_DIR, and the quality cache was read from the
// wrong side. Any divergence from the Python rules reintroduces that split.
function _claudeHome() {
  const fallback = path.join(os.homedir(), '.claude');
  const raw = (process.env.CLAUDE_CONFIG_DIR || '').trim();
  if (!raw) return fallback;
  try {
    const win = process.platform === 'win32';
    // Path.expanduser() parity: only a bare ~ or ~/ (also ~\ on Windows).
    const tilde = win ? /^~(?=$|[\\/])/ : /^~(?=$|\/)/;
    let p = tilde.test(raw) ? os.homedir() + raw.slice(1) : raw;
    // pathlib parity: drop empty and '.' segments, keep '..'. A trailing slash
    // would make lstat follow a symlink pathlib rejects, and collapsing '..'
    // lexically lands somewhere else under a symlinked parent.
    const root = path.parse(p).root;
    const segs = p.slice(root.length).split(win ? /[\\/]+/ : /\/+/);
    p = root + segs.filter(s => s && s !== '.').join(path.sep);
    // pathlib on Windows needs a drive or UNC root: a bare \foo is relative
    // there, while Node's isAbsolute() would accept it.
    const absolute = win ? /^([a-zA-Z]:[\\/]|[\\/]{2})/.test(p) : path.isAbsolute(p);
    if (absolute) {
      const st = fs.lstatSync(p);  // lstat: a symlink is never isDirectory()
      // realpath, like Python's resolve(), so '..' after a symlinked parent
      // names the same physical dir on both sides. .native on purpose: the JS
      // realpathSync collapses '..' lexically before resolving.
      const real = () => { try { return fs.realpathSync.native(p); } catch (e) { return path.resolve(p); } };
      if (st.isDirectory()) return real();
      // Python's is_symlink() is False for a Windows junction (the usual way to
      // relocate a dir without admin rights), so it accepts one. Node's lstat
      // reports junctions as symlinks and cannot tell them apart, so accept any
      // reparse point that resolves to a directory on Windows. The one residual
      // mismatch is a true directory symlink there, which needs admin rights or
      // Developer Mode and is far rarer than a junction.
      if (win && st.isSymbolicLink() && fs.statSync(p).isDirectory()) return real();
    }
  } catch (e) {}
  return fallback;
}
const CLAUDE_HOME = _claudeHome();

// ---- Effective compact window (JS twin of measure.py effective_compact_window) ----
// Where THIS session auto-compacts, so the fill bar divides by the window the
// user will actually hit. Precedence (code.claude.com/docs/en/model-config):
// env CLAUDE_CODE_AUTO_COMPACT_WINDOW > per-model modelSettings (/autocompact)
// > top-level autoCompactWindow. Explicit values clamp to 100000..1000000 and
// cap at the model window; CLAUDE_AUTOCOMPACT_PCT_OVERRIDE (1-99) then scales it
// down (it can never raise it). Returns the window in tokens ONLY when a real
// user override shrank it below the model window (the tuned ~967K default is
// not an override of the host's own number); otherwise null.
function _parseCompactWindow(v) {
  if (v === null || v === undefined || typeof v === 'boolean') return null;
  if (typeof v === 'number') return Number.isFinite(v) && v > 0 ? Math.floor(v) : null;
  const m = /^\s*(\d+)/.exec(String(v));
  return m ? parseInt(m[1], 10) : null;
}
function _canonModelId(model) {
  let m = String(model || '').trim().toLowerCase();
  if (!m) return '';
  m = m.split('/').pop().replace('[1m]', '').trim().replace(/[-@]\d{8}$/, '');
  if (m && !m.startsWith('claude-')) m = 'claude-' + m;
  return m;
}
function _reducedCompactWindow(modelId, modelWindow, settings) {
  try {
    if (!(modelWindow > 0)) return null;
    const envBlock = (settings && typeof settings.env === 'object' && settings.env) || {};
    const envVal = (name) => (process.env[name] !== undefined ? process.env[name] : envBlock[name]);
    const clamp = (n) => Math.max(100000, Math.min(1000000, n));
    let tokens = null;
    const fromEnv = _parseCompactWindow(envVal('CLAUDE_CODE_AUTO_COMPACT_WINDOW'));
    if (fromEnv !== null) {
      tokens = clamp(fromEnv);
    } else {
      const ms = settings && settings.modelSettings;
      if (ms && typeof ms === 'object') {
        const raw = String(modelId || '').trim().toLowerCase();
        const canon = _canonModelId(modelId);
        const fam = /^(?:claude[-_])?(fable|mythos|opus|sonnet|haiku)/.exec(canon);
        const family = fam ? fam[1] : '';
        for (const key of Object.keys(ms)) {
          const entry = ms[key];
          if (!entry || typeof entry !== 'object' || !('autoCompactWindow' in entry)) continue;
          const k = String(key).trim().toLowerCase();
          if (k === raw || k === family || _canonModelId(k) === canon) {
            const w = _parseCompactWindow(entry.autoCompactWindow);
            if (w !== null) { tokens = clamp(w); break; }
          }
        }
      }
      if (tokens === null && settings) {
        const top = _parseCompactWindow(settings.autoCompactWindow);
        if (top !== null) tokens = clamp(top);
      }
    }
    if (tokens !== null && tokens > modelWindow) tokens = modelWindow;
    const pct = parseInt(String(envVal('CLAUDE_AUTOCOMPACT_PCT_OVERRIDE') ?? '').trim(), 10);
    if (Number.isFinite(pct) && pct >= 1 && pct < 100) {
      tokens = Math.floor((tokens === null ? Math.min(modelWindow, 967000) : tokens) * pct / 100);
    }
    return tokens !== null && tokens < modelWindow ? tokens : null;
  } catch (e) {
    return null;
  }
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
        const settingsPath = path.join(CLAUDE_HOME, 'settings.json');
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
    const cacheDir = path.join(CLAUDE_HOME, 'token-optimizer');

    // Context window bar with degradation-aware colors
    // Fill bands: <50% green, 50-70% yellow, 70-80% orange, 80%+ red (blinking)
    let ctx = '';
    const hostUsed = usedPct != null
      ? Math.round(usedPct)
      : (remaining != null ? Math.max(0, Math.min(100, 100 - Math.round(remaining))) : null);

    // Fill on the window the session will actually compact at. When the user
    // shrank it (env / /autocompact / autoCompactWindow / PCT override), divide
    // the live token count by that window instead of the model window.
    const cw = data.context_window || {};
    const modelWindow = typeof cw.context_window_size === 'number' ? cw.context_window_size : null;
    const cu = cw.current_usage || null;
    const liveTokens = cu
      ? (Number(cu.input_tokens) || 0) + (Number(cu.cache_creation_input_tokens) || 0)
        + (Number(cu.cache_read_input_tokens) || 0)
      : (hostUsed != null && modelWindow ? Math.round(hostUsed / 100 * modelWindow) : null);
    let used = hostUsed;
    try {
      let settingsForWindow = null;
      const sp = path.join(CLAUDE_HOME, 'settings.json');
      if (fs.existsSync(sp)) settingsForWindow = JSON.parse(fs.readFileSync(sp, 'utf8'));
      const reduced = modelWindow
        ? _reducedCompactWindow(data.model && data.model.id, modelWindow, settingsForWindow)
        : null;
      if (reduced && liveTokens !== null && liveTokens >= 0) {
        used = Math.max(0, Math.min(100, Math.round(liveTokens / reduced * 100)));
      }
    } catch (e) {}

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
        // used_percentage stays the HOST's model-window fill (measure.py scales
        // it onto the effective compact window); context_tokens + window carry
        // the numerator so it can recompute exactly.
        const liveFillData = JSON.stringify({
          used_percentage: hostUsed != null ? Math.max(0, Math.min(100, hostUsed)) : clamped,
          context_tokens: liveTokens !== null && liveTokens > 0 ? liveTokens : undefined,
          context_window: modelWindow || undefined,
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

    // ---- Bridge the native prompt_cache object to a per-session sidecar ----
    // Claude Code v2.1.251+ includes prompt_cache (ttl, expires_at, warm, ...)
    // in the status-line input for MAIN conversations. measure.py status-bar
    // reads prompt-cache-<sid>.json and prefers it over the transcript guess
    // for the cache countdown and cache-cold warning. Older Claude Code sends
    // no such field and we simply write nothing.
    if (data.prompt_cache && typeof data.prompt_cache === 'object' && safeSessionId) {
      try {
        const pcTmp = path.join(cacheDir, `.prompt-cache.${process.pid}.tmp`);
        fs.writeFileSync(pcTmp, JSON.stringify({ ...data.prompt_cache, timestamp: Date.now() }));
        fs.renameSync(pcTmp, path.join(cacheDir, `prompt-cache-${safeSessionId}.json`));
      } catch (e) {}
    }

    // ---- Read quality cache ----
    // The hooks write the quality-cache under _STATE_BASE, which is
    // ${CLAUDE_PLUGIN_DATA}/token-optimizer (<claude home>/plugins/data/{id}/...)
    // when that env is set (the desktop plugin hook context), else <claude home>/
    // token-optimizer. The statusline runs WITHOUT CLAUDE_PLUGIN_DATA, so reading only
    // `cacheDir` (the claude-home fallback) missed the per-session cache the hooks
    // wrote under plugins/data -> ContextQ/Eff showed "--" for every desktop
    // plugin user. Search every candidate dir and take the freshest matching file.
    let q = null;
    try {
      if (safeSessionId) {
        const _candidates = [];
        if (process.env.CLAUDE_PLUGIN_DATA) {
          _candidates.push(path.join(process.env.CLAUDE_PLUGIN_DATA, 'token-optimizer'));
        }
        try {
          const _dataRoot = path.join(CLAUDE_HOME, 'plugins', 'data');
          for (const d of fs.readdirSync(_dataRoot)) {
            if (d.includes('token-optimizer')) {
              _candidates.push(path.join(_dataRoot, d, 'token-optimizer'));
            }
          }
        } catch (e) {}
        _candidates.push(cacheDir); // <claude home>/token-optimizer fallback (non-hook context)
        let _best = null, _bestMtime = -1;
        for (const c of _candidates) {
          try {
            const f = path.join(c, `quality-cache-${safeSessionId}.json`);
            const st = fs.statSync(f);
            if (st.mtimeMs > _bestMtime) { _bestMtime = st.mtimeMs; _best = f; }
          } catch (e) {}
        }
        if (_best) {
          q = JSON.parse(fs.readFileSync(_best, 'utf8'));
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

    // U3: near-zero-cost "resumable checkpoint" existence signal.
    // When the SessionStart pointer fired (a relevance-cleared checkpoint
    // exists for this session), compact_restore wrote a per-session flag file
    // beside the quality cache. Show a compact ⤸resumable token in the
    // statusline UI ONLY -- this is never injected as additionalContext, so it
    // costs zero billed tokens (R2). Stale flags (>30 min) are ignored so the
    // signal does not outlive the resumable window.
    try {
      if (safeSessionId) {
        const flagPath = path.join(cacheDir, `resumable-${safeSessionId}.json`);
        if (fs.existsSync(flagPath)) {
          const flag = JSON.parse(fs.readFileSync(flagPath, 'utf8'));
          const ts = typeof flag.ts === 'number' ? flag.ts : NaN;
          if (isFinite(ts) && (Date.now() - ts) < (30 * 60 * 1000)) {
            row2Parts.push(`\x1b[36m⤸resumable${RESET}`);
          }
        }
      }
    } catch (e) {}

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

    // Biggest drag: the actual signal pulling the score down — context fill,
    // compactions, or the dominant waste cause — computed upstream by weighted
    // deficit, not guessed from waste keys here.
    if (q && q.top_drag && q.top_drag.label) {
      row2Parts.push(`${DIM}Drag:${q.top_drag.label}${RESET}`);
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
    // TOKEN_OPTIMIZER_STATUS_BAR_SIZE=slim: one line with the most important
    // fields (row 1), never wrapped, whatever COLUMNS says.
    const _slim = (process.env.TOKEN_OPTIMIZER_STATUS_BAR_SIZE || '').trim().toLowerCase() === 'slim';
    if (_slim) {
      process.stdout.write(row1Segs.join(SEP));
    } else if (_width) {
      const rows = [...packRows(row1Segs, _width), ...packRows(row2Parts, _width)];
      process.stdout.write(rows.join('\n'));
    } else {
      process.stdout.write(`${row1Segs.join(SEP)}\n${row2Parts.join(SEP)}`);
    }
  } catch (e) {
    // Silent fail - never break the status line
  }
});
