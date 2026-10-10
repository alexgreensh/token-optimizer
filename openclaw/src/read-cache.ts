/**
 * Token Optimizer - Read Cache for OpenClaw.
 *
 * Intercepts Read tool calls via agent:tool:before events to detect redundant reads.
 * Default ON (warn mode). Opt out via TOKEN_OPTIMIZER_READ_CACHE=0 env var
 * or config.json {"read_cache_enabled": false}.
 *
 * Modes:
 *   warn  (default) - logs redundant read, does NOT block
 *   block           - returns digest instead of re-reading
 *
 * Security:
 *   - Path canonicalization via path.resolve()
 *   - 0o600 permissions on cache files
 *   - mtime re-verification on every cache hit
 *   - Binary file skip
 *   - .contextignore support (hard block)
 */

import * as fs from "fs";
import * as path from "path";
import * as os from "os";
import { isV5Enabled } from "./v5-features";
import { logCompressionEvent } from "./telemetry";
import { computeDelta } from "./delta-diff";
import { estimateTokensFromBytes } from "./token-estimate";
import { redact } from "./redact";

// ---------------------------------------------------------------------------
// Savings event log (U-B + U-G, JSONL append, mirrors Python savings_events)
// ---------------------------------------------------------------------------

const SAVINGS_DIR = path.join(os.homedir(), ".openclaw", "token-optimizer");
const SAVINGS_EVENTS_PATH = path.join(SAVINGS_DIR, "savings-events.jsonl");
const MAX_SAVINGS_DETAIL_LEN = 200;

/**
 * Append a savings event to ~/.openclaw/token-optimizer/savings-events.jsonl.
 * Shape matches Python's _log_savings_event() row (timestamp, event_type,
 * tokens_saved, session_id, detail). Best-effort: never throws.
 *
 * OpenClaw savings events are kept separate from the Python trends.db so each
 * platform owns its own storage; the categories (checkpoint_restore,
 * hint_followed) are the same strings the Python dashboard uses, enabling
 * consistent cross-platform labelling if they are ever merged.
 */
export function logSavingsEvent(
  eventType: string,
  tokensSaved: number,
  sessionId: string,
  detail?: string
): void {
  try {
    // Redact BEFORE cutting: a secret straddling the cut is a prefix no
    // pattern recognises.
    const redactedDetail = detail ? redact(detail) : null;
    const detailTrunc = redactedDetail && redactedDetail.length > MAX_SAVINGS_DETAIL_LEN
      ? redactedDetail.slice(0, MAX_SAVINGS_DETAIL_LEN)
      : redactedDetail;
    const row = JSON.stringify({
      timestamp: new Date().toISOString(),
      event_type: eventType,
      tokens_saved: Math.round(tokensSaved),
      session_id: sessionId || null,
      // Boundary redaction: detail can embed a file name carrying a
      // credential shape — never persist it raw.
      detail: detailTrunc || null,
    });
    try { fs.mkdirSync(SAVINGS_DIR, { recursive: true, mode: 0o700 }); } catch { /* best effort */ }
    fs.appendFileSync(SAVINGS_EVENTS_PATH, row + "\n", { encoding: "utf8", mode: 0o600 });
  } catch {
    // Best-effort: never let savings bookkeeping break a read or inject.
  }
}

// ---------------------------------------------------------------------------
// U-B: working-set token sum (getActiveReadTokens)
// ---------------------------------------------------------------------------

const ACTIVE_READ_MAX_FILES = 25;
const ACTIVE_READ_MIN_COUNT = 2;
const ACTIVE_READ_TOKEN_CAP = 200_000;

/**
 * Sum tokensEst for files read at least MIN_COUNT times this session,
 * capped at 200 000 tokens and limited to 25 most-recently-accessed entries.
 *
 * Session-SCOPED, not agent-scoped: reads are cached under
 * `<agentId>-<session>.json` where agentId is often "unknown"/per-agent, but the
 * compact handler doesn't know that agentId. So we aggregate every
 * `*-<session>.json` cache for this session, which is robust to the agent key
 * and matches the intent ("what did THIS session read repeatedly").
 *
 * Mirrors Python SessionStore.get_active_read_tokens(limit=25, min_read_count=2).
 * Returns 0 on any error (floor fallback will be used instead).
 */
export function getActiveReadTokens(_agentId: string, sessionId: string): number {
  try {
    const safeSession = sessionId.replace(/[^a-zA-Z0-9_-]/g, "") || "unknown";
    const suffix = `-${safeSession}.json`;
    let files: string[] = [];
    try {
      files = fs.readdirSync(CACHE_DIR).filter((f) => f.endsWith(suffix));
    } catch {
      return 0;
    }
    const entries: CacheEntry[] = [];
    for (const f of files) {
      try {
        const cache = JSON.parse(fs.readFileSync(path.join(CACHE_DIR, f), "utf-8")) as ReadCache;
        for (const e of Object.values(cache.files || {})) {
          if (e && e.readCount >= ACTIVE_READ_MIN_COUNT) entries.push(e);
        }
      } catch { /* skip unreadable/corrupt cache */ }
    }
    const total = entries
      .sort((a, b) => b.lastAccess - a.lastAccess)
      .slice(0, ACTIVE_READ_MAX_FILES)
      .reduce((sum, e) => sum + (e.tokensEst ?? 0), 0);
    return Math.min(ACTIVE_READ_TOKEN_CAP, total);
  } catch {
    return 0;
  }
}

// ---------------------------------------------------------------------------
// U-G: per-hint avoided-search (recordHintServe / claimHintFollow)
// ---------------------------------------------------------------------------

const HINT_FOLLOW_MAX_AGE_SECONDS = 4 * 60 * 60; // 4 hours, mirrors Python
const HINT_FOLLOW_TOKEN_CREDIT = 5_000;           // mirrors Python _HINT_FOLLOW_AVOIDED_TOKENS
const HINT_SERVE_MAX_PATHS = 25;

interface HintServeEntry {
  filePath: string;
  servedAt: number; // unix seconds
  credited: boolean;
}

/** Path to the per-session hint-serves sidecar JSON under the read-cache dir. */
function hintServesPath(sessionId: string): string {
  const safeSession = sessionId.replace(/[^a-zA-Z0-9_-]/g, "") || "unknown";
  return path.join(CACHE_DIR, `hint-serves-${safeSession}.json`);
}

function loadHintServes(sessionId: string): Record<string, HintServeEntry> {
  const p = hintServesPath(sessionId);
  if (!fs.existsSync(p)) return {};
  try {
    const data = JSON.parse(fs.readFileSync(p, "utf-8"));
    if (!data || typeof data !== "object") return {};
    return data as Record<string, HintServeEntry>;
  } catch {
    try { fs.unlinkSync(p); } catch { /* ignore */ }
    return {};
  }
}

function saveHintServes(sessionId: string, entries: Record<string, HintServeEntry>): void {
  const p = hintServesPath(sessionId);
  const dir = path.dirname(p);
  try {
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    const tmp = p + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(entries), { mode: 0o600 });
    fs.renameSync(tmp, p);
  } catch {
    // Best-effort: hint-serve bookkeeping must never break the hint injection.
  }
}

/**
 * Record that a continuity hint surfaced these file paths to the session.
 * Idempotent: if a path already has an UNCREDITED entry, do NOT reset its
 * servedAt or credited flag (preserves the original freshness window).
 * Already-credited entries are not re-inserted so a re-serve of the same hint
 * can't resurrect a spent credit.
 *
 * Cap at 25 paths (matches Python record_hint_serve).
 */
export function recordHintServe(sessionId: string, filePaths: string[]): void {
  if (!sessionId || !filePaths || filePaths.length === 0) return;
  try {
    const serves = loadHintServes(sessionId);
    const now = Date.now() / 1000;
    const capped = filePaths.slice(0, HINT_SERVE_MAX_PATHS);
    let changed = false;
    for (const rawPath of capped) {
      const fp = redact(rawPath.trim());
      if (!fp) continue;
      // Only insert if no existing uncredited entry for this path.
      if (serves[fp] && !serves[fp].credited) continue;
      // If previously credited, skip (can't resurrect).
      if (serves[fp] && serves[fp].credited) continue;
      serves[fp] = { filePath: fp, servedAt: now, credited: false };
      changed = true;
    }
    if (changed) saveHintServes(sessionId, serves);
  } catch {
    // Best-effort: never break the hint injection.
  }
}

/**
 * Check if filePath was hinted to this session within the freshness window
 * and not yet credited. If so, mark it credited and return true (caller logs
 * the savings event). Returns false in all other cases. Credit is permanent:
 * once marked, re-reads of the same file can never re-credit.
 *
 * Mirrors Python SessionStore.claim_hint_follow().
 */
export function claimHintFollow(sessionId: string, filePath: string): boolean {
  if (!sessionId || !filePath) return false;
  try {
    const serves = loadHintServes(sessionId);
    // Keys are stored redacted (recordHintServe), so normalize the lookup the
    // same way — a credential-shaped path is never persisted OR queried raw.
    const key = redact(filePath);
    const entry = serves[key];
    if (!entry || entry.credited) return false;
    const ageSecs = Date.now() / 1000 - entry.servedAt;
    if (ageSecs > HINT_FOLLOW_MAX_AGE_SECONDS) return false;
    // Mark credited and persist before returning true.
    serves[key] = { ...entry, credited: true };
    saveHintServes(sessionId, serves);
    return true;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const HOME = os.homedir();
const CACHE_DIR = path.join(HOME, ".openclaw", "token-optimizer", "read-cache");
const MAX_CACHE_ENTRIES = 500;
const MAX_CONTEXTIGNORE_PATTERNS = 200;

// v5 Delta Mode: memory-only content cache keyed by sessionId + filePath.
// Per-entry cap at 50KB. Per-map cap at 100 entries with LRU eviction so a
// long-running gateway that reads hundreds of distinct files cannot leak
// memory. Session-scoped keys prevent cross-session leaks: session B can
// never see content that session A cached, even on the same file. Lost on
// gateway restart by design — the cost of a cold cache is one extra full
// re-read, which is acceptable.
const DELTA_CACHE_MAX_BYTES = 50 * 1024;
const DELTA_CACHE_MAX_ENTRIES = 100;
const _deltaContentCache = new Map<string, { mtime: number; content: string }>();

function deltaCacheKey(sessionId: string, filePath: string): string {
  return `${sessionId}::${filePath}`;
}

function setDeltaCache(
  sessionId: string,
  filePath: string,
  entry: { mtime: number; content: string }
): void {
  const key = deltaCacheKey(sessionId, filePath);
  // Re-insertion moves the key to the end of the Map iteration order, so
  // we delete-then-set to refresh position on an overwrite.
  if (_deltaContentCache.has(key)) {
    _deltaContentCache.delete(key);
  }
  _deltaContentCache.set(key, entry);

  // LRU eviction: drop oldest insertions until the cap holds.
  while (_deltaContentCache.size > DELTA_CACHE_MAX_ENTRIES) {
    const oldestKey = _deltaContentCache.keys().next().value;
    if (oldestKey === undefined) break;
    _deltaContentCache.delete(oldestKey);
  }
}

function getDeltaCache(
  sessionId: string,
  filePath: string
): { mtime: number; content: string } | undefined {
  return _deltaContentCache.get(deltaCacheKey(sessionId, filePath));
}

function deleteDeltaCache(sessionId: string, filePath: string): void {
  _deltaContentCache.delete(deltaCacheKey(sessionId, filePath));
}

/**
 * Drop every delta cache entry for a session. Exported so index.ts can
 * wire this into agent:stop / session:end events if OpenClaw ever exposes
 * them. Safe to call on an unknown sessionId (no-op).
 */
export function clearDeltaCacheForSession(sessionId: string): void {
  const prefix = `${sessionId}::`;
  for (const key of Array.from(_deltaContentCache.keys())) {
    if (key.startsWith(prefix)) {
      _deltaContentCache.delete(key);
    }
  }
}

const BINARY_EXTENSIONS = new Set([
  ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
  ".pdf", ".wasm", ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
  ".exe", ".dll", ".so", ".dylib", ".o", ".a",
  ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv",
  ".ttf", ".otf", ".woff", ".woff2", ".eot",
  ".pyc", ".pyo", ".class", ".jar",
  ".sqlite", ".db", ".sqlite3",
]);

// ---------------------------------------------------------------------------
// .contextignore
// ---------------------------------------------------------------------------

let _contextignorePatterns: string[] | null = null;

function loadContextignorePatterns(): string[] {
  if (_contextignorePatterns !== null) return _contextignorePatterns;

  const patterns: string[] = [];

  // Project-level .contextignore
  const projectIgnore = path.resolve(".contextignore");
  if (fs.existsSync(projectIgnore)) {
    try {
      const lines = fs.readFileSync(projectIgnore, "utf-8").split("\n");
      for (const line of lines) {
        const trimmed = line.trim();
        if (trimmed && !trimmed.startsWith("#")) patterns.push(trimmed);
      }
    } catch { /* ignore */ }
  }

  // Global .contextignore
  const globalIgnore = path.join(HOME, ".openclaw", ".contextignore");
  if (fs.existsSync(globalIgnore)) {
    try {
      const lines = fs.readFileSync(globalIgnore, "utf-8").split("\n");
      for (const line of lines) {
        const trimmed = line.trim();
        if (trimmed && !trimmed.startsWith("#")) patterns.push(trimmed);
      }
    } catch { /* ignore */ }
  }

  _contextignorePatterns = patterns.slice(0, MAX_CONTEXTIGNORE_PATTERNS);
  return _contextignorePatterns;
}

/**
 * Simple glob match using minimatch-style logic (fnmatch equivalent).
 * Supports * and ** patterns. Pre-compiled regex cache avoids ~1,200
 * regex compilations per session.
 */
const _fnmatchCache = new Map<string, RegExp>();

function fnmatch(filepath: string, pattern: string): boolean {
  let re = _fnmatchCache.get(pattern);
  if (!re) {
    const regex = pattern
      .replace(/[.+^${}()|[\]\\]/g, "\\$&")
      .replace(/\*\*/g, "{{GLOBSTAR}}")
      .replace(/\*/g, "[^/]*")
      .replace(/\?/g, "[^/]")
      .replace(/\{\{GLOBSTAR\}\}/g, ".*");
    re = new RegExp(`^${regex}$`);
    _fnmatchCache.set(pattern, re);
  }
  return re.test(filepath);
}

function isContextignored(filePath: string): boolean {
  const patterns = loadContextignorePatterns();
  if (patterns.length === 0) return false;

  const basename = path.basename(filePath);
  for (const pattern of patterns) {
    if (fnmatch(filePath, pattern) || fnmatch(basename, pattern)) return true;
  }
  return false;
}

// ---------------------------------------------------------------------------
// Structural digests
// ---------------------------------------------------------------------------

function digestPython(content: string): string {
  const lines = content.split("\n");
  const parts: string[] = [];
  for (let i = 0; i < lines.length && parts.length < 50; i++) {
    const stripped = lines[i].trim();
    if (stripped.startsWith("class ")) {
      parts.push(`L${i + 1}: ${stripped.split("(")[0].split(":")[0]}`);
    } else if (stripped.startsWith("def ")) {
      parts.push(`L${i + 1}: ${stripped.split("(")[0]}`);
    } else if (stripped.startsWith("import ") || stripped.startsWith("from ")) {
      parts.push(`L${i + 1}: ${stripped}`);
    }
  }
  return parts.length > 0 ? parts.join("\n") : `${lines.length} lines`;
}

function digestJavaScript(content: string): string {
  const lines = content.split("\n");
  const parts: string[] = [];
  for (let i = 0; i < lines.length && parts.length < 50; i++) {
    const stripped = lines[i].trim();
    if (/^(export\s+)?(class|interface|type|enum)\s+/.test(stripped)) {
      parts.push(`L${i + 1}: ${stripped.split("{")[0].trim()}`);
    } else if (/^(export\s+)?(async\s+)?function\s+/.test(stripped)) {
      parts.push(`L${i + 1}: ${stripped.split("{")[0].trim()}`);
    } else if (/^export\s+(default\s+)?(const|let|var)\s+/.test(stripped)) {
      parts.push(`L${i + 1}: ${stripped.split("=")[0].trim()}`);
    }
  }
  return parts.length > 0 ? parts.join("\n") : `${lines.length} lines`;
}

function digestFallback(content: string): string {
  const lines = content.split("\n");
  const n = lines.length;
  if (n <= 6) return `${n} lines`;
  const first = lines.slice(0, 3).join("\n");
  const last = lines.slice(-3).join("\n");
  return `${n} lines\nFirst 3:\n${first}\nLast 3:\n${last}`;
}

function generateDigest(filePath: string, content: string): string {
  const lines = content.split("\n");
  if (lines.length > 10000) return `${lines.length} lines (too large for structural digest)`;
  const ext = path.extname(filePath).toLowerCase();
  if (ext === ".py") return digestPython(content);
  if ([".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"].includes(ext)) return digestJavaScript(content);
  return digestFallback(content);
}

// ---------------------------------------------------------------------------
// Cache types and operations
// ---------------------------------------------------------------------------

interface CacheEntry {
  mtime: number;
  offset: number;
  limit: number;
  tokensEst: number;
  readCount: number;
  lastAccess: number;
  digest: string;
}

interface ReadCache {
  files: Record<string, CacheEntry>;
}

function cachePath(agentId: string, sessionId: string): string {
  const safeAgent = agentId.replace(/[^a-zA-Z0-9_-]/g, "") || "default";
  const safeSession = sessionId.replace(/[^a-zA-Z0-9_-]/g, "") || "unknown";
  return path.join(CACHE_DIR, `${safeAgent}-${safeSession}.json`);
}

function loadCache(agentId: string, sessionId: string): ReadCache {
  const cp = cachePath(agentId, sessionId);
  if (!fs.existsSync(cp)) return { files: {} };
  try {
    const data = JSON.parse(fs.readFileSync(cp, "utf-8"));
    if (!data || !data.files) throw new Error("invalid");
    return data as ReadCache;
  } catch {
    try { fs.unlinkSync(cp); } catch { /* ignore */ }
    return { files: {} };
  }
}

function saveCache(agentId: string, sessionId: string, cache: ReadCache): void {
  const files = cache.files;
  const keys = Object.keys(files);
  if (keys.length > MAX_CACHE_ENTRIES) {
    const sorted = keys.sort((a, b) => (files[a].lastAccess ?? 0) - (files[b].lastAccess ?? 0));
    const toRemove = keys.length - MAX_CACHE_ENTRIES;
    for (let i = 0; i < toRemove; i++) delete files[sorted[i]];
  }

  const cp = cachePath(agentId, sessionId);
  const dir = path.dirname(cp);
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const tmp = cp + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(cache), { mode: 0o600 });
  fs.renameSync(tmp, cp);
}

function logDecision(decision: string, filePath: string, reason: string, sessionId: string): void {
  const safeSession = sessionId.replace(/[^a-zA-Z0-9_-]/g, "") || "unknown";
  const dir = path.join(CACHE_DIR, "decisions");
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const logPath = path.join(dir, `${safeSession}.jsonl`);
  // Boundary redaction: file paths/reasons are tool-derived text — a
  // credential-shaped file name must never reach this log raw.
  const entry = JSON.stringify({ ts: Date.now() / 1000, decision, file: redact(filePath), reason: redact(reason), session: sessionId });
  try {
    fs.appendFileSync(logPath, entry + "\n", { mode: 0o600 });
  } catch { /* ignore */ }
}

// ---------------------------------------------------------------------------
// Exported handlers (called from index.ts plugin events)
// ---------------------------------------------------------------------------

export interface ReadToolInput {
  file_path?: string;
  offset?: number;
  limit?: number;
}

export interface ToolEventData {
  toolName: string;
  toolInput: ReadToolInput;
  agentId: string;
  sessionId: string;
}

/**
 * Handle agent:tool:before for Read events.
 * Returns { block: true, message: string } to block, or null to allow.
 */
/**
 * Read the read-cache config.json once. Used as the fallback source when the
 * corresponding env var is absent (e.g. stripped by the gateway). Returns {} on
 * any error so callers can treat "no config" and "unreadable config" alike.
 */
function readReadCacheConfigFile(): Record<string, unknown> {
  const configPath = path.join(HOME, ".openclaw", "token-optimizer", "read-cache", "config.json");
  try {
    if (fs.existsSync(configPath)) {
      const parsed = JSON.parse(fs.readFileSync(configPath, "utf-8"));
      if (parsed && typeof parsed === "object") return parsed as Record<string, unknown>;
    }
  } catch { /* ignore */ }
  return {};
}

function isReadCacheDisabled(): boolean {
  const envVal = process.env.TOKEN_OPTIMIZER_READ_CACHE;
  if (envVal === "0") return true;
  if (envVal === undefined) {
    // Env var missing (possibly stripped). Check config file.
    if (readReadCacheConfigFile().read_cache_enabled === false) return true;
  }
  return false;
}

/**
 * Resolve read-cache mode (warn/block) with the same two-layer rule as
 * isReadCacheDisabled: env var wins, config file is the fallback when env is
 * absent. Previously env-only, so a user's config-file read_cache_mode was
 * silently ignored and defaulted to "warn".
 */
export function resolveReadCacheMode(): string {
  const envVal = process.env.TOKEN_OPTIMIZER_READ_CACHE_MODE;
  if (envVal !== undefined) return envVal.toLowerCase();
  const cfgVal = readReadCacheConfigFile().read_cache_mode;
  if (typeof cfgVal === "string" && cfgVal.trim()) return cfgVal.toLowerCase();
  return "warn";
}

export function handleReadBefore(event: ToolEventData): { block: boolean; message: string } | null {
  if (isReadCacheDisabled()) return null;

  const mode = resolveReadCacheMode();
  const rawPath = event.toolInput.file_path ?? "";
  if (!rawPath) return null;

  const filePath = path.resolve(rawPath);
  const { agentId, sessionId } = event;
  // Cache keys are persisted to a JSON file — a credential-shaped file name
  // would land on disk verbatim. All cache.files access uses this redacted
  // key; filePath itself stays raw for real fs operations below.
  const persistPath = redact(filePath);

  // .contextignore check (hard block)
  if (isContextignored(filePath)) {
    logDecision("block", filePath, "contextignore", sessionId);
    return {
      block: true,
      message: `[Token Optimizer] File blocked by .contextignore: ${path.basename(filePath)}\nRemove the pattern from .contextignore if you need access.`,
    };
  }

  // Skip binary files
  if (BINARY_EXTENSIONS.has(path.extname(filePath).toLowerCase())) return null;

  const cache = loadCache(agentId, sessionId);
  const entry = cache.files[persistPath];
  const offset = event.toolInput.offset ?? 0;
  const limit = event.toolInput.limit ?? 0;
  // Delta Mode only runs against explicit full-file reads. A read that
  // carries a non-default offset or limit is an intentionally narrow
  // pull (e.g. to avoid a secret section or stay under a window budget),
  // and serving it a whole-file diff on re-read would silently bypass
  // that scope.
  const isFullFileRead = offset === 0 && limit === 0;

  if (!entry) {
    // First read: cache it
    let mtime = 0;
    let tokensEst = 0;
    try {
      const stat = fs.statSync(filePath);
      mtime = stat.mtimeMs / 1000;
      tokensEst = estimateTokensFromBytes(stat.size);
    } catch { return null; }

    cache.files[persistPath] = { mtime, offset, limit, tokensEst, readCount: 1, lastAccess: Date.now() / 1000, digest: "" };
    saveCache(agentId, sessionId, cache);
    logDecision("allow", filePath, "first_read", sessionId);

    // U-G: check if this first read fulfils a prior hint serve.
    try {
      if (claimHintFollow(sessionId, filePath)) {
        logSavingsEvent("hint_followed", HINT_FOLLOW_TOKEN_CREDIT, sessionId,
          `hint->read: ${path.basename(filePath)}`);
      }
    } catch { /* best-effort */ }

    // v5 Delta Mode: seed the memory-only content cache so a follow-up
    // read from the SAME session can be served as a diff. Only activates
    // when the feature is on, the file fits the 50KB budget, and the
    // caller asked for the whole file. Keyed by session so session B
    // never sees session A's cached content.
    if (isV5Enabled("delta_read") && isFullFileRead) {
      try {
        const stat = fs.statSync(filePath);
        if (stat.size <= DELTA_CACHE_MAX_BYTES) {
          const content = fs.readFileSync(filePath, "utf-8");
          setDeltaCache(sessionId, filePath, { mtime, content });
        }
      } catch {
        // File unreadable — skip silently; next read will try again.
      }
    }
    return null;
  }

  // Check staleness: mtime + range
  let currentMtime = 0;
  try {
    currentMtime = fs.statSync(filePath).mtimeMs / 1000;
  } catch {
    delete cache.files[persistPath];
    saveCache(agentId, sessionId, cache);
    logDecision("allow", filePath, "file_changed_or_deleted", sessionId);
    return null;
  }

  const mtimeMatch = Math.abs(currentMtime - entry.mtime) < 0.001;
  const rangeMatch = entry.offset === offset && entry.limit === limit;

  if (!(mtimeMatch && rangeMatch)) {
    // v5 Delta Mode: if we have cached content from the previous read, we
    // can serve the diff instead of the full file. Only fires when the
    // feature is on, the range is unchanged (delta is line-oriented so
    // offset/limit shifts would produce misleading diffs), the mtime
    // changed, and we have content in the memory cache.
    const sessionCached = getDeltaCache(sessionId, filePath);
    const deltaEligible =
      isV5Enabled("delta_read") &&
      isFullFileRead &&
      rangeMatch &&
      !mtimeMatch &&
      sessionCached !== undefined;

    if (deltaEligible && sessionCached) {
      try {
        const stat = fs.statSync(filePath);
        if (stat.size <= DELTA_CACHE_MAX_BYTES) {
          const newContent = fs.readFileSync(filePath, "utf-8");
          const delta = computeDelta(sessionCached.content, newContent);

          // Refresh cache for next delta (only when it still fits the budget)
          setDeltaCache(sessionId, filePath, { mtime: currentMtime, content: newContent });

          // Update the on-disk entry so future calls see the new mtime.
          entry.mtime = currentMtime;
          entry.readCount++;
          entry.lastAccess = Date.now() / 1000;
          entry.digest = "";
          saveCache(agentId, sessionId, cache);

          logCompressionEvent({
            feature: "delta_read",
            sessionId,
            commandPattern: `Read:${path.basename(filePath)}`,
            originalText: newContent,
            compressedText: delta.body,
            qualityPreserved: !delta.fallback,
            verified: false,
            detail: `delta ${delta.summary}${delta.fallback ? " fallback" : ""}`,
          });

          logDecision("block", filePath, `delta_read_${delta.summary}`, sessionId);
          return {
            block: true,
            message: `[Token Optimizer] Delta read: ${path.basename(filePath)} changed ${delta.summary}\n\n${delta.body}\n\n(Re-request with a different offset/limit to force a full re-read.)`,
          };
        }
        // File grew past the budget — drop the memory cache entry and fall through.
        deleteDeltaCache(sessionId, filePath);
      } catch {
        deleteDeltaCache(sessionId, filePath);
        // Fall through to the normal mtime-changed path.
      }
    }

    entry.mtime = currentMtime;
    entry.offset = offset;
    entry.limit = limit;
    entry.readCount++;
    entry.lastAccess = Date.now() / 1000;
    entry.digest = "";
    saveCache(agentId, sessionId, cache);

    // Update the delta cache with the fresh content when delta_read is on
    // AND the caller asked for a full-file read. Narrow reads never feed
    // the delta cache so a follow-up full-file read cannot inherit stale
    // scope from a previous narrow request.
    if (isV5Enabled("delta_read") && isFullFileRead) {
      try {
        const stat = fs.statSync(filePath);
        if (stat.size <= DELTA_CACHE_MAX_BYTES) {
          const content = fs.readFileSync(filePath, "utf-8");
          setDeltaCache(sessionId, filePath, { mtime: currentMtime, content });
        } else {
          deleteDeltaCache(sessionId, filePath);
        }
      } catch {
        deleteDeltaCache(sessionId, filePath);
      }
    } else if (!isFullFileRead) {
      // Narrow reads evict any previously cached delta content so a
      // subsequent full-file read cannot be served a stale diff whose
      // provenance predates the narrow request.
      deleteDeltaCache(sessionId, filePath);
    }

    logDecision("allow", filePath, "file_modified_or_different_range", sessionId);
    return null;
  }

  // Redundant read
  entry.readCount++;
  entry.lastAccess = Date.now() / 1000;

  let contentForTelemetry: string | null = null;
  if (!entry.digest) {
    try {
      const content = fs.readFileSync(filePath, "utf-8");
      entry.digest = generateDigest(filePath, content);
      contentForTelemetry = content;
    } catch {
      entry.digest = "(unable to generate digest)";
    }
  }

  saveCache(agentId, sessionId, cache);

  // v5 Structure Map Beta telemetry: only log when we actually built a
  // fresh digest from file content during this call. On repeat redundant
  // reads the digest is already cached and contentForTelemetry stays null,
  // which would make original_tokens = 0 and tokens_saved flip negative on
  // the v5 dashboard. Skipping these calls keeps the savings card honest.
  if (
    isV5Enabled("structure_map_beta") &&
    contentForTelemetry !== null &&
    entry.digest &&
    entry.digest !== "(unable to generate digest)"
  ) {
    logCompressionEvent({
      feature: "structure_map",
      sessionId,
      commandPattern: `Read:${path.basename(filePath)}`,
      originalText: contentForTelemetry,
      compressedText: entry.digest,
      qualityPreserved: true,
      verified: false,
      detail: `redundant_read ${entry.readCount}`,
    });
  }

  if (mode === "block") {
    logDecision("block", filePath, `redundant_read_${entry.readCount}`, sessionId);
    return {
      block: true,
      message: `[Token Optimizer] File already in context (read #${entry.readCount}, unchanged).\nStructural digest of ${path.basename(filePath)}:\n${entry.digest}\n\nTo re-read, edit the file first or use a different offset/limit.`,
    };
  }

  logDecision("warn", filePath, `redundant_read_${entry.readCount}`, sessionId);
  return null;
}

/**
 * Handle agent:tool:after for Edit/Write events (cache invalidation).
 */
export function handleWriteAfter(event: ToolEventData): void {
  if (!["Edit", "Write", "MultiEdit", "NotebookEdit"].includes(event.toolName)) return;

  const rawPath = event.toolInput.file_path ?? "";
  if (!rawPath) return;

  const filePath = path.resolve(rawPath);
  const cache = loadCache(event.agentId, event.sessionId);
  const persistPath = redact(filePath);
  if (cache.files[persistPath]) {
    delete cache.files[persistPath];
    saveCache(event.agentId, event.sessionId, cache);
  }
  // Keep the v5 delta cache consistent with on-disk truth for this session.
  deleteDeltaCache(event.sessionId, filePath);
}

/**
 * Clear all caches (called on compact).
 */
export function clearCache(agentId: string, sessionId: string): void {
  const cp = cachePath(agentId, sessionId);
  try { fs.unlinkSync(cp); } catch { /* ignore */ }
  // Also remove per-session decisions file
  const safeSession = sessionId.replace(/[^a-zA-Z0-9_-]/g, "") || "unknown";
  const dp = path.join(CACHE_DIR, "decisions", `${safeSession}.jsonl`);
  try { fs.unlinkSync(dp); } catch { /* ignore */ }
}
