// Filesystem locations the companion reads. All derived from the Claude home
// dir so tests can point at a fixture by passing an explicit base.
import * as os from 'os';
import * as path from 'path';
import * as fs from 'fs';

export interface ClaudePaths {
  claudeDir: string;
  cacheDir: string; // <claude dir>/token-optimizer  (or ~/.copilot/token-optimizer)
  projectsDir: string; // ~/.claude/projects  (unused/empty for Copilot mode)
  liveFill: string;
  rateLimits: string;
  dashboardFile: string;
  qualityCache(sessionId: string): string;
  // Copilot-only: directory that holds per-session state dirs.
  // Undefined for Claude mode; used by findActiveCopilotSession.
  sessionStateDir?: string;
}

// Claude Code's config dir, resolved exactly like the plugin's statusline.js and
// runtime_env.claude_home(): CLAUDE_CONFIG_DIR when it is an absolute, existing,
// non-symlink directory, else <home>/.claude. The plugin writes every file this
// companion reads under that dir, so reading a hard-coded ~/.claude showed stale
// or missing data for relocated configs (#198). Keep the three copies in step.
export function resolveClaudeDir(
  homeDir: string = os.homedir(),
  env: NodeJS.ProcessEnv = process.env
): string {
  const fallback = path.join(homeDir, '.claude');
  const raw = (env.CLAUDE_CONFIG_DIR || '').trim();
  if (!raw) return fallback;
  try {
    const win = process.platform === 'win32';
    const tilde = win ? /^~(?=$|[\\/])/ : /^~(?=$|\/)/;
    let p = tilde.test(raw) ? homeDir + raw.slice(1) : raw;
    // pathlib parity: drop empty and '.' segments, keep '..' (see statusline.js).
    const root = path.parse(p).root;
    const segs = p.slice(root.length).split(win ? /[\\/]+/ : /\/+/);
    p = root + segs.filter((s) => s && s !== '.').join(path.sep);
    const absolute = win ? /^([a-zA-Z]:[\\/]|[\\/]{2})/.test(p) : path.isAbsolute(p);
    if (absolute) {
      const st = fs.lstatSync(p);
      // .native: the JS realpathSync collapses '..' lexically before resolving.
      const real = (): string => {
        try { return fs.realpathSync.native(p); } catch { return path.resolve(p); }
      };
      if (st.isDirectory()) return real();
      // Windows junctions show up as symlinks in Node; the plugin accepts them.
      if (win && st.isSymbolicLink() && fs.statSync(p).isDirectory()) return real();
    }
  } catch {
    // unusable override: fall back like the plugin does
  }
  return fallback;
}

export function resolvePaths(homeDir: string = os.homedir()): ClaudePaths {
  const claudeDir = resolveClaudeDir(homeDir);
  const cacheDir = path.join(claudeDir, 'token-optimizer');
  return {
    claudeDir,
    cacheDir,
    projectsDir: path.join(claudeDir, 'projects'),
    liveFill: path.join(cacheDir, 'live-fill.json'),
    rateLimits: path.join(cacheDir, 'rate-limits.json'),
    dashboardFile: path.join(claudeDir, '_backups', 'token-optimizer', 'dashboard.html'),
    qualityCache: (sessionId: string) =>
      path.join(cacheDir, `quality-cache-${sanitizeSessionId(sessionId)}.json`),
  };
}

function realOrResolved(p: string): string {
  try { return fs.realpathSync.native(p); } catch { return path.resolve(p); }
}

function isUnder(child: string, parent: string): boolean {
  const rel = path.relative(parent, child);
  return rel !== '' && !rel.startsWith('..') && !path.isAbsolute(rel);
}

function isWsl(): boolean {
  if (process.platform !== 'linux') return false;
  for (const probe of ['/proc/version', '/proc/sys/kernel/osrelease']) {
    try {
      const low = fs.readFileSync(probe, 'utf8').toLowerCase();
      if (low.includes('microsoft') || low.includes('wsl')) return true;
    } catch {
      // probe unreadable: try the next one
    }
  }
  return false;
}

// Copilot's home for Token Optimizer data, resolved like the plugin's
// runtime_env.copilot_home(): TOKEN_OPTIMIZER_COPILOT_HOME wins, then
// COPILOT_HOME. Either must be an absolute, existing, non-symlink directory
// strictly under the user's home, or (inside WSL only) under /mnt/ for a
// Windows-profile install. A set-but-rejected value falls back to ~/.copilot,
// as the plugin does. Ignoring the override made the companion read an empty
// ~/.copilot while the plugin wrote elsewhere. Keep both copies in step.
export function resolveCopilotDir(
  homeDir: string = os.homedir(),
  env: NodeJS.ProcessEnv = process.env
): string {
  const fallback = path.join(homeDir, '.copilot');
  const ours = (env.TOKEN_OPTIMIZER_COPILOT_HOME || '').trim();
  const raw = ours || (env.COPILOT_HOME || '').trim();
  if (!raw) return fallback;
  try {
    const win = process.platform === 'win32';
    const tilde = win ? /^~(?=$|[\\/])/ : /^~(?=$|\/)/;
    const p = tilde.test(raw) ? homeDir + raw.slice(1) : raw;
    if (!path.isAbsolute(p)) return fallback;
    const st = fs.lstatSync(p);
    if (st.isSymbolicLink() || !st.isDirectory()) return fallback;
    const resolved = realOrResolved(p);
    if (isUnder(resolved, realOrResolved(homeDir))) return resolved;
    if (isWsl() && isUnder(resolved, realOrResolved('/mnt'))) return resolved;
  } catch {
    // missing or unreadable override: fall back like the plugin does
  }
  return fallback;
}

// Paths for a GitHub Copilot Token Optimizer install at <copilot home>/token-optimizer/
// (~/.copilot unless TOKEN_OPTIMIZER_COPILOT_HOME / COPILOT_HOME relocates it).
// The cache-file names (live-fill.json, rate-limits.json, quality-cache-*.json) are
// intentionally identical to the Claude layout so cacheReader / dataSource are
// runtime-agnostic — only the base directory changes.
export function resolveCopilotPaths(homeDir: string = os.homedir()): ClaudePaths {
  const copilotDir = resolveCopilotDir(homeDir);
  const cacheDir = path.join(copilotDir, 'token-optimizer');
  return {
    // claudeDir is used only by DataSource.readEffort (reads ~/.claude/settings.json).
    // For Copilot mode there is no equivalent settings file, so we point at the
    // copilot dir; readEffort will return null gracefully when the file is absent.
    claudeDir: copilotDir,
    cacheDir,
    // Copilot does not use the Claude projects transcript layout.  DataSource's
    // findActiveSession call is skipped when sessionStateDir is set (see dataSource.ts).
    projectsDir: path.join(copilotDir, 'projects'),
    liveFill: path.join(cacheDir, 'live-fill.json'),
    rateLimits: path.join(cacheDir, 'rate-limits.json'),
    dashboardFile: path.join(copilotDir, 'token-optimizer', 'dashboard.html'),
    qualityCache: (sessionId: string) =>
      path.join(cacheDir, `quality-cache-${sanitizeSessionId(sessionId)}.json`),
    sessionStateDir: path.join(copilotDir, 'session-state'),
  };
}

// The runtime setting value.  Matches tokenOptimizer.runtime in package.json.
export type Runtime = 'auto' | 'claude' | 'copilot';

// Most-recent mtime (ms) of any file directly in `dir`, or 0 if the dir is
// missing/empty/unreadable. Used as a cheap "was this runtime active recently"
// signal. The token-optimizer cache dir is a small flat dir, so a single
// readdir + stat pass is inexpensive and only runs at activation / config change.
function mostRecentMtimeMs(dir: string): number {
  try {
    let max = 0;
    for (const entry of fs.readdirSync(dir)) {
      try {
        const m = fs.statSync(path.join(dir, entry)).mtimeMs;
        if (m > max) max = m;
      } catch {
        // unreadable entry — skip
      }
    }
    return max;
  } catch {
    return 0; // dir absent or unreadable
  }
}

// Auto-detect the active runtime so Copilot users don't have to flip a setting.
// Compares the most recent write to each runtime's token-optimizer cache and
// prefers Copilot ONLY when its cache exists and is strictly more recent than
// Claude's. A pure-Copilot user (no Claude cache) gets Copilot; a pure-Claude
// user or the no-data case falls back to Claude (the historical default); a
// mixed user follows whichever runtime they used most recently.
export function resolveAutoPaths(homeDir: string = os.homedir()): ClaudePaths {
  const claudeCache = path.join(resolveClaudeDir(homeDir), 'token-optimizer');
  const copilotCache = path.join(resolveCopilotDir(homeDir), 'token-optimizer');
  const copilotMs = mostRecentMtimeMs(copilotCache);
  const claudeMs = mostRecentMtimeMs(claudeCache);
  if (copilotMs > 0 && copilotMs > claudeMs) return resolveCopilotPaths(homeDir);
  return resolvePaths(homeDir);
}

// Has the Token Optimizer CLI plugin ever run on this machine? True when a
// token-optimizer cache dir exists under any known runtime home (~/.claude or
// ~/.copilot) — the plugin creates it on first run. Used to tell "installed but
// idle" (show the normal empty state) from "extension present, plugin missing"
// (show the install funnel). Directory presence, not freshness: a plugin the
// user hasn't run today is still installed.
export function detectPluginInstalled(homeDir: string = os.homedir()): boolean {
  const candidates = [
    path.join(resolveClaudeDir(homeDir), 'token-optimizer'),
    path.join(resolveCopilotDir(homeDir), 'token-optimizer'),
  ];
  for (const dir of candidates) {
    try {
      if (fs.statSync(dir).isDirectory()) return true;
    } catch {
      // absent/unreadable — try the next candidate
    }
  }
  return false;
}

// Factory: pick the right paths object based on the runtime setting string.
// 'claude' / 'copilot' are explicit overrides; 'auto' (the default) and any
// unrecognized value resolve by detecting the most recently active runtime.
export function resolvePathsForRuntime(
  runtime: string | undefined,
  homeDir: string = os.homedir()
): ClaudePaths {
  if (runtime === 'copilot') return resolveCopilotPaths(homeDir);
  if (runtime === 'claude') return resolvePaths(homeDir);
  return resolveAutoPaths(homeDir);
}

// Mirror measure.py's sanitize: strip anything outside [A-Za-z0-9_-] so a
// crafted session id can never escape the cache dir.
export function sanitizeSessionId(sessionId: string): string {
  return (sessionId || '').replace(/[^a-zA-Z0-9_-]/g, '');
}

// Claude Code names each transcript directory after the session's cwd, with
// every non-alphanumeric character replaced by '-' (so '/Users/x/.claude' →
// '-Users-x--claude'). Used to scope session resolution to the window's folder.
export function encodeProjectDir(cwd: string): string {
  return (cwd || '').replace(/[^a-zA-Z0-9]/g, '-');
}
