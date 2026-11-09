// Unit tests for Cursor path resolution and the Cursor session resolver.
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import {
  resolveCursorDir,
  resolveCursorPaths,
  resolvePathsForRuntime,
  resolveAutoPaths,
  detectPluginInstalled,
} from '../src/paths';
import { findActiveCursorSession } from '../src/sessionResolver';

let tmp: string;

beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'to-cursor-test-'));
});

afterEach(() => {
  fs.rmSync(tmp, { recursive: true, force: true });
});

function home(): string {
  return tmp;
}

function writeTally(sessionsDir: string, name: string, tally: any, mtimeMs?: number): void {
  fs.mkdirSync(sessionsDir, { recursive: true });
  const p = path.join(sessionsDir, `${name}.json`);
  fs.writeFileSync(p, JSON.stringify(tally));
  if (mtimeMs != null) {
    const d = new Date(mtimeMs);
    fs.utimesSync(p, d, d);
  }
}

describe('resolveCursorDir', () => {
  it('falls back to ~/.cursor with no env', () => {
    expect(resolveCursorDir(home(), {})).toBe(path.join(tmp, '.cursor'));
  });

  it('honors TOKEN_OPTIMIZER_CURSOR_HOME when absolute, existing, non-symlink, under home', () => {
    const custom = path.join(tmp, 'cursor-custom');
    fs.mkdirSync(custom);
    expect(resolveCursorDir(home(), { TOKEN_OPTIMIZER_CURSOR_HOME: custom })).toBe(
      fs.realpathSync(custom)
    );
  });

  it('rejects an override outside home', () => {
    const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'to-outside-'));
    expect(resolveCursorDir(home(), { TOKEN_OPTIMIZER_CURSOR_HOME: outside })).toBe(
      path.join(tmp, '.cursor')
    );
    fs.rmSync(outside, { recursive: true, force: true });
  });

  it('rejects a symlinked override', () => {
    const real = path.join(tmp, 'cursor-real');
    const link = path.join(tmp, 'cursor-link');
    fs.mkdirSync(real);
    fs.symlinkSync(real, link);
    expect(resolveCursorDir(home(), { TOKEN_OPTIMIZER_CURSOR_HOME: link })).toBe(
      path.join(tmp, '.cursor')
    );
  });

  it('rejects a missing directory override', () => {
    expect(
      resolveCursorDir(home(), { TOKEN_OPTIMIZER_CURSOR_HOME: path.join(tmp, 'nope') })
    ).toBe(path.join(tmp, '.cursor'));
  });
});

describe('resolveCursorPaths', () => {
  it('points the sessions dir under <cursor>/token-optimizer/sessions', () => {
    const paths = resolveCursorPaths(home());
    expect(paths.cursorSessionsDir).toBe(
      path.join(tmp, '.cursor', 'token-optimizer', 'sessions')
    );
    expect(paths.cacheDir).toBe(path.join(tmp, '.cursor', 'token-optimizer'));
  });
});

describe('resolvePathsForRuntime', () => {
  it("returns cursor paths for 'cursor'", () => {
    expect(resolvePathsForRuntime('cursor', home()).cursorSessionsDir).toBeDefined();
  });
  it("returns no cursorSessionsDir for 'claude' or 'copilot'", () => {
    expect(resolvePathsForRuntime('claude', home()).cursorSessionsDir).toBeUndefined();
    expect(resolvePathsForRuntime('copilot', home()).cursorSessionsDir).toBeUndefined();
  });
});

describe('resolveAutoPaths', () => {
  it('picks cursor only when its cache is strictly the newest', () => {
    const claudeCache = path.join(tmp, '.claude', 'token-optimizer');
    const cursorCache = path.join(tmp, '.cursor', 'token-optimizer');
    fs.mkdirSync(claudeCache, { recursive: true });
    fs.mkdirSync(cursorCache, { recursive: true });
    // Claude newer -> claude wins even though cursor has data.
    fs.writeFileSync(path.join(cursorCache, 'a.json'), '{}');
    fs.writeFileSync(path.join(claudeCache, 'b.json'), '{}');
    const future = new Date(Date.now() + 60_000);
    fs.utimesSync(path.join(claudeCache, 'b.json'), future, future);
    expect(resolveAutoPaths(home()).cursorSessionsDir).toBeUndefined();

    // Cursor strictly newest -> cursor wins.
    const later = new Date(Date.now() + 120_000);
    fs.utimesSync(path.join(cursorCache, 'a.json'), later, later);
    expect(resolveAutoPaths(home()).cursorSessionsDir).toBeDefined();
  });

  it('falls back to claude with no caches at all', () => {
    const paths = resolveAutoPaths(home());
    expect(paths.cursorSessionsDir).toBeUndefined();
    expect(paths.sessionStateDir).toBeUndefined();
  });
});

describe('detectPluginInstalled', () => {
  it('detects a cursor-only install', () => {
    expect(detectPluginInstalled(home())).toBe(false);
    fs.mkdirSync(path.join(tmp, '.cursor', 'token-optimizer'), { recursive: true });
    expect(detectPluginInstalled(home())).toBe(true);
  });
});

describe('findActiveCursorSession', () => {
  const dir = () => path.join(tmp, '.cursor', 'token-optimizer', 'sessions');
  const base = { conversation_id: 'conv-a', first_ts: 1000, tool_calls: 1 };

  it('returns null for a missing or empty dir', () => {
    expect(findActiveCursorSession(dir())).toBeNull();
    fs.mkdirSync(dir(), { recursive: true });
    expect(findActiveCursorSession(dir())).toBeNull();
  });

  it('picks the tally with the newest updated_at', () => {
    writeTally(dir(), 'conv-a', { ...base, updated_at: 2000 });
    writeTally(dir(), 'conv-b', { ...base, conversation_id: 'conv-b', updated_at: 3000 });
    const found = findActiveCursorSession(dir());
    expect(found?.sessionId).toBe('conv-b');
  });

  it('scopes to the workspace folder when one is open', () => {
    writeTally(dir(), 'conv-a', { ...base, updated_at: 3000, cwd: '/other/place' });
    writeTally(dir(), 'conv-b', {
      ...base,
      conversation_id: 'conv-b',
      updated_at: 2000,
      workspace_roots: ['/repo/demo'],
    });
    const found = findActiveCursorSession(dir(), { workspaceDir: '/repo/demo' });
    expect(found?.sessionId).toBe('conv-b');
  });

  it('matches workspace_roots with a trailing slash difference', () => {
    writeTally(dir(), 'conv-b', { ...base, conversation_id: 'conv-b', updated_at: 1, cwd: '/repo/demo/' });
    const found = findActiveCursorSession(dir(), { workspaceDir: '/repo/demo' });
    expect(found?.sessionId).toBe('conv-b');
  });

  it('reports no session when the open workspace has none', () => {
    writeTally(dir(), 'conv-a', { ...base, updated_at: 3000, cwd: '/other/place' });
    expect(findActiveCursorSession(dir(), { workspaceDir: '/repo/demo' })).toBeNull();
  });

  it('skips corrupt and oversized tallies', () => {
    fs.mkdirSync(dir(), { recursive: true });
    fs.writeFileSync(path.join(dir(), 'bad.json'), '{not json');
    writeTally(dir(), 'conv-a', { ...base, updated_at: 1000 });
    const found = findActiveCursorSession(dir());
    expect(found?.sessionId).toBe('conv-a');
  });

  it('sanitizes the session id it reports', () => {
    writeTally(dir(), 'conv-a', { ...base, conversation_id: '../../evil', updated_at: 1000 });
    const found = findActiveCursorSession(dir());
    expect(found?.sessionId).toBe('evil');
  });

  it('matches a tally cwd nested under the workspace (subfolder agent run)', () => {
    writeTally(dir(), 'conv-b', {
      ...base,
      conversation_id: 'conv-b',
      updated_at: 2000,
      cwd: '/repo/demo/src/deep',
    });
    const found = findActiveCursorSession(dir(), { workspaceDir: '/repo/demo' });
    expect(found?.sessionId).toBe('conv-b');
  });

  it('does not match a sibling path that only shares a prefix', () => {
    writeTally(dir(), 'conv-a', { ...base, updated_at: 3000, cwd: '/repo/demo-other' });
    expect(findActiveCursorSession(dir(), { workspaceDir: '/repo/demo' })).toBeNull();
  });

  it('does not match when the workspace is nested inside the tally root', () => {
    // workspace=/repo/demo/sub but the agent worked at /repo/demo — that tally
    // may belong to a different window, so it must not match.
    writeTally(dir(), 'conv-a', { ...base, updated_at: 3000, cwd: '/repo/demo' });
    expect(findActiveCursorSession(dir(), { workspaceDir: '/repo/demo/sub' })).toBeNull();
  });

  it('normalizes separator direction between tally and workspace', () => {
    writeTally(dir(), 'conv-b', {
      ...base,
      conversation_id: 'conv-b',
      updated_at: 2000,
      cwd: 'C:\\repo\\demo',
    });
    const found = findActiveCursorSession(dir(), { workspaceDir: 'C:/repo/demo' });
    expect(found?.sessionId).toBe('conv-b');
  });

  it('compares case-insensitively on Windows (drive-letter case)', () => {
    const original = process.platform;
    Object.defineProperty(process, 'platform', { value: 'win32', configurable: true });
    try {
      writeTally(dir(), 'conv-b', {
        ...base,
        conversation_id: 'conv-b',
        updated_at: 2000,
        cwd: 'c:\\Repo\\Demo',
      });
      const found = findActiveCursorSession(dir(), { workspaceDir: 'C:/Repo/Demo' });
      expect(found?.sessionId).toBe('conv-b');
    } finally {
      Object.defineProperty(process, 'platform', { value: original, configurable: true });
    }
  });

  it('finds the workspace session even when 8+ newer foreign tallies exist', () => {
    // Regression: the candidate cap used to apply BEFORE the workspace filter,
    // so eight newer other-window tallies starved this window's session out.
    const now = Date.now();
    for (let i = 0; i < 9; i++) {
      writeTally(dir(), `foreign-${i}`, {
        ...base,
        conversation_id: `foreign-${i}`,
        updated_at: now / 1000 + i,
        cwd: '/other/window',
      }, now + i);
    }
    writeTally(dir(), 'conv-mine', {
      ...base,
      conversation_id: 'conv-mine',
      updated_at: now / 1000 - 100,
      cwd: '/repo/demo',
    }, now - 60_000);
    const found = findActiveCursorSession(dir(), { workspaceDir: '/repo/demo' });
    expect(found?.sessionId).toBe('conv-mine');
  });
});
