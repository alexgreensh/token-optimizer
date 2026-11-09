// Pick the Claude Code session that belongs to THIS window.
//
// Claude Code stores each transcript at projects/<encoded-cwd>/<session>.jsonl.
// The naive "globally most-recent JSONL" heuristic shows the wrong session when
// several Claude sessions run at once (a different window's session can be more
// recently written). So when the window has a workspace folder, we scope to the
// transcript dir(s) for that folder (and any subfolder cwd), and only fall back
// to global-most-recent when no folder is open.
//
// Copilot mode: each session lives at <sessionStateDir>/<id>/events.jsonl.
// The active session is the one whose events.jsonl was most recently modified.
import * as fs from 'fs';
import * as path from 'path';
import { sanitizeSessionId, encodeProjectDir } from './paths';

export interface ActiveSession {
  sessionId: string;
  jsonlPath: string;
  mtimeMs: number;
}

export interface ResolveOptions {
  // Absolute path of the window's workspace folder, if any.
  workspaceDir?: string | null;
}

export function findActiveSession(
  projectsDir: string,
  opts: ResolveOptions = {}
): ActiveSession | null {
  let dirs: fs.Dirent[];
  try {
    dirs = fs.readdirSync(projectsDir, { withFileTypes: true });
  } catch {
    return null;
  }

  if (opts.workspaceDir) {
    const prefix = encodeProjectDir(opts.workspaceDir);
    // Prefer an EXACT cwd match (claude run at the workspace root — the common
    // case). Only this avoids the greedy-ancestor trap: a workspace like
    // '/Users/me' would otherwise prefix-match every project beneath it.
    const exact = dirs.filter((d) => d.isDirectory() && d.name === prefix);
    const exactHit = newestSessionIn(projectsDir, exact);
    if (exactHit) return exactHit;

    // No session at the root — fall back to subfolder cwds (claude run in a
    // subdirectory). The '-' boundary stops '/a/proj' matching '/a/proj2'. A
    // hyphenated sibling ('/a/proj-extra') is genuinely ambiguous from '/a/proj'
    // (both '/' and '-' encode to '-'), but the exact-match above covers the
    // common case, so this only over-matches when the workspace has no session
    // of its own AND such a sibling exists — rare and self-correcting.
    const sub = dirs.filter((d) => d.isDirectory() && d.name.startsWith(prefix + '-'));
    // A workspace IS open: only show its session. If none, report no session
    // rather than a different window's — that wrong data is the bug we're fixing.
    return newestSessionIn(projectsDir, sub);
  }

  // No workspace folder open — best effort: globally most-recent.
  return newestSessionIn(projectsDir, dirs.filter((d) => d.isDirectory()));
}

// ---------------------------------------------------------------------------
// Copilot session resolver
// ---------------------------------------------------------------------------
// Copilot Token Optimizer stores session state at:
//   ~/.copilot/session-state/<session-id>/events.jsonl
//
// The "active" session is the directory whose events.jsonl was most recently
// modified. If the sessionStateDir does not exist, or is empty, returns null
// (same contract as findActiveSession). The jsonlPath is set to events.jsonl
// so callers can tail it via JsonlTailer; if the file doesn't exist yet, the
// tailer's graceful stat-failure path handles it.
export function findActiveCopilotSession(sessionStateDir: string): ActiveSession | null {
  let dirs: fs.Dirent[];
  try {
    dirs = fs.readdirSync(sessionStateDir, { withFileTypes: true });
  } catch {
    return null;
  }

  let best: ActiveSession | null = null;
  for (const dir of dirs) {
    if (!dir.isDirectory()) continue;
    const eventsPath = path.join(sessionStateDir, dir.name, 'events.jsonl');
    let mtimeMs: number;
    try {
      mtimeMs = fs.statSync(eventsPath).mtimeMs;
    } catch {
      continue;
    }
    if (!best || mtimeMs > best.mtimeMs) {
      best = {
        sessionId: sanitizeSessionId(dir.name),
        jsonlPath: eventsPath,
        mtimeMs,
      };
    }
  }
  return best;
}

// ---------------------------------------------------------------------------
// Cursor session resolver
// ---------------------------------------------------------------------------
// Cursor Token Optimizer stores one hook tally per conversation at:
//   <cursor home>/token-optimizer/sessions/<conversation_id>.json
//
// The "active" session is the tally with the newest updated_at. When the
// window has a workspace folder, a tally whose cwd or workspace_roots matches
// that folder wins over a globally-newer tally from another window — the same
// wrong-window bug findActiveSession fixes for Claude.

export interface CursorActiveSession {
  sessionId: string;
  tallyPath: string;
  mtimeMs: number;
  tally: any; // parsed tally JSON (already read; saves a second read per refresh)
}

// Cap on tally reads per rescan and per-file bytes: a pathological sessions
// dir must never stall the extension host.
const CURSOR_MAX_CANDIDATES = 8;
const CURSOR_MAX_TALLY_BYTES = 4 * 1024 * 1024;

export function readTallyFile(filePath: string): any | null {
  try {
    const st = fs.statSync(filePath);
    if (st.size > CURSOR_MAX_TALLY_BYTES) return null;
    const parsed = JSON.parse(fs.readFileSync(filePath, 'utf8'));
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function tallyMatchesWorkspace(tally: any, workspaceDir: string): boolean {
  const normalize = (p: any) =>
    typeof p === 'string' && p ? p.replace(/[/\\]+$/, '') : '';
  const want = normalize(workspaceDir);
  if (!want) return false;
  if (normalize(tally.cwd) === want) return true;
  const roots = tally.workspace_roots;
  if (Array.isArray(roots)) {
    for (const root of roots) {
      if (normalize(root) === want) return true;
    }
  }
  return false;
}

export function findActiveCursorSession(
  sessionsDir: string,
  opts: ResolveOptions = {}
): CursorActiveSession | null {
  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(sessionsDir, { withFileTypes: true });
  } catch {
    return null;
  }

  // Candidates: newest few tally files by mtime (reading every tally in a huge
  // sessions dir on every rescan is wasteful; staleness beyond the newest few
  // can never be the active session).
  const files: { path: string; mtimeMs: number }[] = [];
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith('.json')) continue;
    const full = path.join(sessionsDir, entry.name);
    try {
      files.push({ path: full, mtimeMs: fs.statSync(full).mtimeMs });
    } catch {
      // unreadable — skip
    }
  }
  files.sort((a, b) => b.mtimeMs - a.mtimeMs);

  const candidates: CursorActiveSession[] = [];
  for (const file of files.slice(0, CURSOR_MAX_CANDIDATES)) {
    const tally = readTallyFile(file.path);
    if (!tally) continue;
    const id = typeof tally.conversation_id === 'string' && tally.conversation_id
      ? tally.conversation_id
      : path.basename(file.path, '.json');
    candidates.push({
      sessionId: sanitizeSessionId(id),
      tallyPath: file.path,
      mtimeMs: file.mtimeMs,
      tally,
    });
  }
  if (candidates.length === 0) return null;

  const updatedAt = (c: CursorActiveSession): number => {
    const t = Number(c.tally.updated_at);
    return Number.isFinite(t) ? t : 0;
  };
  const newest = (list: CursorActiveSession[]): CursorActiveSession =>
    list.reduce((best, c) => (updatedAt(c) > updatedAt(best) ? c : best));

  if (opts.workspaceDir) {
    const matches = candidates.filter((c) => tallyMatchesWorkspace(c.tally, opts.workspaceDir!));
    // A workspace IS open: only show its session. If none matches, report no
    // session rather than a different window's — that wrong data is the bug.
    return matches.length ? newest(matches) : null;
  }
  return newest(candidates);
}

function newestSessionIn(projectsDir: string, dirs: fs.Dirent[]): ActiveSession | null {
  let best: ActiveSession | null = null;
  for (const dir of dirs) {
    const projectPath = path.join(projectsDir, dir.name);
    let files: string[];
    try {
      files = fs.readdirSync(projectPath);
    } catch {
      continue;
    }
    for (const file of files) {
      if (!file.endsWith('.jsonl')) continue; // skips the subagents/ subdir
      const full = path.join(projectPath, file);
      let mtimeMs: number;
      try {
        mtimeMs = fs.statSync(full).mtimeMs;
      } catch {
        continue;
      }
      if (!best || mtimeMs > best.mtimeMs) {
        best = {
          sessionId: sanitizeSessionId(file.replace(/\.jsonl$/, '')),
          jsonlPath: full,
          mtimeMs,
        };
      }
    }
  }
  return best;
}
