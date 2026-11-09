// Watches Token Optimizer's cache dir and produces a fresh Snapshot on change
// or on a slow timer (so session duration ticks and JSONL-fallback fill stays
// current even when nothing else writes). This is the only data module that
// imports `vscode`; all parsing is delegated to the vscode-free cacheReader.
import * as fs from 'fs';
import * as path from 'path';
import * as vscode from 'vscode';
import { ClaudePaths, detectPluginInstalled } from './paths';
import {
  findActiveSession,
  findActiveCopilotSession,
  findActiveCursorSession,
  readTallyFile,
  ActiveSession,
  CursorActiveSession,
} from './sessionResolver';
import { JsonlTailer } from './jsonlTail';
import { buildSnapshot, parseRateLimitsSidecar } from './cacheReader';
import { buildCursorSnapshot } from './cursorReader';
import { estimateRateLimitsFromTranscripts } from './usageEstimator';
import { RateLimits, Snapshot, emptySnapshot } from './types';

const DEBOUNCE_MS = 400;
const TICK_MS = 5000;
const RESCAN_EVERY_TICKS = 6; // ~30s safety net for sessions that write no cache yet
const EFFORT_MAP: Record<string, string> = { low: 'lo', medium: 'med', high: 'hi' };

export class DataSource {
  private watcher: vscode.FileSystemWatcher | undefined;
  private focusSub: vscode.Disposable | undefined;
  private workspaceSub: vscode.Disposable | undefined;
  private timer: NodeJS.Timeout | undefined;
  private debounce: NodeJS.Timeout | undefined;
  private tailer: JsonlTailer | undefined;
  private disposed = false;
  // Session resolution and effort rarely change, so we don't re-scan every
  // project dir or re-read settings on each 5s tick — only when the cache dir
  // changes (a new session writes there) or on a periodic safety rescan.
  private cachedSession: ActiveSession | null = null;
  private cachedCursorSession: CursorActiveSession | null = null;
  private cachedEffort: string | null = null;
  private cachedUsageEstimate: RateLimits | null = null;
  private needsRescan = true;
  private tick = 0;

  constructor(
    private paths: ClaudePaths,
    private getStaleAfterSeconds: () => number,
    private onSnapshot: (snap: Snapshot) => void
  ) {}

  start(): void {
    try {
      this.watcher = vscode.workspace.createFileSystemWatcher(
        // Recursive: Cursor keeps its per-session tallies one level down in
        // cacheDir/sessions/, while Claude/Copilot caches are flat.
        new vscode.RelativePattern(vscode.Uri.file(this.paths.cacheDir), '**/*.json')
      );
      // Content changes (live-fill / rate-limits / quality-cache updates) belong
      // to the SAME session, so they must NOT trigger a session re-scan — those
      // writes fire several times a second and re-scanning stat-walks every
      // project dir. Only a new or removed file can mean a session change —
      // plus, in Cursor mode, a tally update for a DIFFERENT conversation,
      // which means the user switched chats inside the same window.
      this.watcher.onDidChange((uri) => this.scheduleRefresh(this.cursorSessionSwitch(uri)));
      this.watcher.onDidCreate(() => this.scheduleRefresh(true));
      this.watcher.onDidDelete(() => this.scheduleRefresh(true));
    } catch {
      // Watching is best-effort; the timer below still keeps us current.
    }
    this.timer = setInterval(() => {
      // Don't poll while the window is unfocused — nobody is looking, and the
      // watcher still catches real changes. Focus regain forces a refresh below.
      if (!this.isFocused()) return;
      this.tick++;
      if (this.tick % RESCAN_EVERY_TICKS === 0) this.needsRescan = true;
      this.refresh(false);
    }, TICK_MS);
    try {
      this.focusSub = vscode.window.onDidChangeWindowState((s) => {
        if (s.focused) this.refresh(false);
      });
    } catch {
      // window state API unavailable — timer + watcher still cover us.
    }
    try {
      this.workspaceSub = vscode.workspace.onDidChangeWorkspaceFolders(() => {
        this.needsRescan = true;
        this.refresh(false);
      });
    } catch {
      // ignore — folder rarely changes mid-session.
    }
    // Defer the first (synchronous, fs-walking) refresh off the activation path
    // so activate() returns immediately and never trips VS Code's >500ms watchdog.
    setImmediate(() => this.refresh(false));
  }

  // The window's workspace folder, used to scope session resolution to this
  // window. First folder when a multi-root workspace; undefined if none open.
  private workspaceDir(): string | null {
    try {
      return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? null;
    } catch {
      return null;
    }
  }

  private isFocused(): boolean {
    try {
      return vscode.window.state.focused;
    } catch {
      return true; // if the API is unavailable, behave as before (always poll)
    }
  }

  private scheduleRefresh(rescan: boolean): void {
    if (rescan) this.needsRescan = true;
    if (this.debounce) clearTimeout(this.debounce);
    this.debounce = setTimeout(() => this.refresh(false), DEBOUNCE_MS);
  }

  // True when a changed file is a Cursor tally for a DIFFERENT conversation
  // than the cached active session — i.e. the user switched chats and the
  // cached identity is stale. The bridge rewrites the SAME tally on every
  // hook event, so same-file changes stay on the cheap path; only Cursor mode
  // has per-session tally files under the watched dir, so other runtimes
  // always return false here.
  private cursorSessionSwitch(uri: vscode.Uri): boolean {
    const sessionsDir = this.paths.cursorSessionsDir;
    if (!sessionsDir || !this.cachedCursorSession) return false;
    try {
      const changed = path.resolve(uri.fsPath);
      const dir = path.resolve(sessionsDir);
      if (!changed.startsWith(dir + path.sep)) return false;
      return changed !== path.resolve(this.cachedCursorSession.tallyPath);
    } catch {
      return false;
    }
  }

  refresh(recomputeUsageEstimate = false): void {
    if (this.disposed) return;
    let snap: Snapshot;
    try {
      snap = this.buildFromDisk(recomputeUsageEstimate);
    } catch (e) {
      // Log the error instead of swallowing it silently.
      console.error('[Token Optimizer] DataSource.refresh error:', e);
      snap = emptySnapshot();
      // Keep the empty state's runtime honest even on a transient read error.
      snap.runtimeName = this.paths.cursorSessionsDir
        ? 'cursor'
        : this.paths.sessionStateDir
          ? 'copilot'
          : 'claude';
    }
    // Overlay independently of the disk read so a transient parse error can't
    // flip an installed user into the funnel: detection is pure fs.statSync on
    // the well-known cache dirs.
    try {
      snap.pluginDetected = detectPluginInstalled();
    } catch {
      snap.pluginDetected = true; // never nag on a detection failure
    }
    this.onSnapshot(snap);
  }

  private buildFromDisk(recomputeUsageEstimate: boolean): Snapshot {
    const isCursorMode = !!this.paths.cursorSessionsDir;
    if (isCursorMode) {
      // Cursor mode: resolve and render from hook tallies only. There are no
      // Claude-shape transcripts to tail, no effort setting, and no rate-limit
      // sidecars, so the generic readers are skipped entirely. Session IDENTITY
      // is resolved on rescan, but the tally CONTENT is re-read on every
      // refresh — the bridge rewrites it on every hook event, and a cached
      // parse would freeze fill/grade at rescan time.
      if (this.needsRescan) {
        this.cachedCursorSession = findActiveCursorSession(this.paths.cursorSessionsDir!, {
          workspaceDir: this.workspaceDir(),
        });
        this.needsRescan = false;
      }
      const tally = this.cachedCursorSession
        ? readTallyFile(this.cachedCursorSession.tallyPath)
        : null;
      return buildCursorSnapshot(tally, {
        nowMs: Date.now(),
        scoped: this.workspaceDir() != null,
      });
    }

    if (this.needsRescan) {
      // Copilot mode: resolve via session-state dirs (by events.jsonl mtime).
      // Claude mode: resolve via projects transcript dirs (by .jsonl mtime).
      this.cachedSession = this.paths.sessionStateDir
        ? findActiveCopilotSession(this.paths.sessionStateDir)
        : findActiveSession(this.paths.projectsDir, {
            workspaceDir: this.workspaceDir(),
          });
      this.cachedEffort = this.readEffort();
      this.needsRescan = false;
    }
    const session = this.cachedSession;

    // In copilot mode (sessionStateDir is set), the copilot events.jsonl is
    // not in Claude JSONL format — the usage parser always returns null, so tailer
    // reads are wasted.  Skip them and return null cleanly.
    const isCopilotMode = !!this.paths.sessionStateDir;

    let jsonlTokens: number | null = null;
    let jsonlModel: string | null = null;
    if (session && !isCopilotMode) {
      if (!this.tailer) this.tailer = new JsonlTailer(session.jsonlPath);
      else this.tailer.setPath(session.jsonlPath);
      const tail = this.tailer.read();
      jsonlTokens = tail.tokens;
      jsonlModel = tail.model;
    } else if (!session) {
      // No session: drop the tailer so a future session starts from offset 0.
      this.tailer = undefined;
    }

    const nowMs = Date.now();
    const rateLimitsJson = readIfExists(this.paths.rateLimits);
    if (recomputeUsageEstimate) {
      // In copilot mode there is no ~/.copilot/projects transcript dir,
      // so estimateRateLimitsFromTranscripts would stat a non-existent path on
      // every explicit refresh.  Skip it and leave the estimate as null.
      if (isCopilotMode) {
        this.cachedUsageEstimate = null;
      } else {
        try {
          this.cachedUsageEstimate = estimateRateLimitsFromTranscripts(this.paths.projectsDir, {
            nowMs,
            baseline: parseRateLimitsSidecar(rateLimitsJson, nowMs, this.getStaleAfterSeconds()),
          });
        } catch {
          this.cachedUsageEstimate = null;
        }
      }
    }

    const snap = buildSnapshot({
      qualityJson: session ? readIfExists(this.paths.qualityCache(session.sessionId)) : null,
      liveFillJson: readIfExists(this.paths.liveFill),
      rateLimitsJson,
      estimatedRateLimits: this.cachedUsageEstimate,
      jsonlTokens,
      jsonlModel,
      effort: this.cachedEffort,
      sessionId: session ? session.sessionId : null,
      scoped: this.workspaceDir() != null,
      nowMs,
      staleAfterSeconds: this.getStaleAfterSeconds(),
    });
    snap.runtimeName = isCopilotMode ? 'copilot' : 'claude';
    return snap;
  }

  private readEffort(): string | null {
    const raw = readIfExists(path.join(this.paths.claudeDir, 'settings.json'));
    if (!raw) return null;
    try {
      const level = JSON.parse(raw).effortLevel;
      return level ? EFFORT_MAP[level] || level : null;
    } catch {
      return null;
    }
  }

  dispose(): void {
    this.disposed = true;
    if (this.debounce) clearTimeout(this.debounce);
    if (this.timer) clearInterval(this.timer);
    this.watcher?.dispose();
    this.focusSub?.dispose();
    this.workspaceSub?.dispose();
  }
}

function readIfExists(filePath: string): string | null {
  try {
    return fs.readFileSync(filePath, 'utf8');
  } catch {
    return null;
  }
}
