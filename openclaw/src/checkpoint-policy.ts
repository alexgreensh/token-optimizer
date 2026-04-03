import * as fs from "fs";
import * as path from "path";
import { AgentRun } from "./models";
import { contextWindowForModel, scoreSessionQuality } from "./quality";
import { ContextAudit } from "./context-audit";

export interface SessionCheckpointState {
  capturedFillBands: Set<number>;
  capturedQualityThresholds: Set<number>;
  capturedMilestones: Set<string>;
  lastCheckpointAt: number;
  lastEvaluatedAt: number;
  editWriteCount: number;
  editedFiles: Set<string>;
  editBatchMarkerWrites: number;
  editBatchMarkerFiles: number;
}

export interface RuntimeSnapshot {
  fillPct: number;
  qualityScore: number;
}

export interface CheckpointDecision {
  trigger: string;
  fillPct?: number;
  qualityScore?: number;
}

export interface CheckpointTelemetry {
  sessionId: string;
  messages?: Array<{ role: string; content: string; timestamp?: string }>;
  fillPct?: number;
  qualityScore?: number;
  toolName?: string;
  eventKind: "session-patch" | "tool-before" | "tool-after" | "session-end";
  activeAgents?: number;
  writeCount?: number;
  writeBurstCount?: number;
  contextWindow?: number;
  model?: string;
  timestamp?: number;
  agentId?: string;
  sessionFile?: string;
}

export interface CheckpointHealth {
  checkpointRoot: string;
  sessionCount: number;
  checkpointCount: number;
  policyCount: number;
  pendingCount: number;
  checkpointBytes: number;
  recentEventCount: number;
  lastTrigger?: string;
  issues: string[];
}

export interface CheckpointTelemetrySummary {
  enabled: boolean;
  eventLog: string;
  days: number;
  totalEvents: number;
  recentEvents: number;
  byTrigger: Record<string, number>;
  lastEvent: {
    timestamp?: string;
    sessionId?: string;
    trigger?: string;
    fillPct?: number;
    qualityScore?: number;
  } | null;
}

export type CheckpointTrigger =
  | "compact"
  | "stop"
  | "stop-failure"
  | "end"
  | "milestone-pre-fanout"
  | "milestone-edit-batch"
  | `progressive-${number}`
  | `quality-${number}`;

export const FILL_BANDS = [20, 35, 50, 65, 80];
export const QUALITY_THRESHOLDS = [80, 70, 50, 40];
const HOME = process.env.HOME ?? process.env.USERPROFILE ?? "";
const CHECKPOINT_ROOT = path.join(HOME, ".openclaw", "token-optimizer", "checkpoints");
const CHECKPOINT_COOLDOWN_MS = Number.parseInt(
  process.env.TOKEN_OPTIMIZER_CHECKPOINT_COOLDOWN_MS ?? "90000",
  10
);
const EVALUATION_COOLDOWN_MS = Number.parseInt(
  process.env.TOKEN_OPTIMIZER_RUNTIME_EVALUATION_COOLDOWN_MS ?? "30000",
  10
);
const EDIT_BATCH_WRITE_THRESHOLD = Number.parseInt(
  process.env.TOKEN_OPTIMIZER_EDIT_BATCH_WRITE_THRESHOLD ?? "4",
  10
);
const EDIT_BATCH_FILE_THRESHOLD = Number.parseInt(
  process.env.TOKEN_OPTIMIZER_EDIT_BATCH_FILE_THRESHOLD ?? "3",
  10
);
const CHECKPOINT_TELEMETRY_ENABLED = ["1", "true", "yes", "on"].includes(
  (process.env.TOKEN_OPTIMIZER_CHECKPOINT_TELEMETRY ?? "0").toLowerCase()
);
const STATE_FILENAME = "policy-state.json";
const EVENTS_FILENAME = "checkpoint-events.jsonl";

const states = new Map<string, SessionCheckpointState>();

function sanitizeSessionId(id: string): string {
  const clean = id.replace(/[^a-zA-Z0-9_-]/g, "_");
  if (!clean || clean === "." || clean === "..") return "invalid-session";
  return clean;
}

function isWithinDir(root: string, candidate: string): boolean {
  const relative = path.relative(root, candidate);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

function resolvedCheckpointRoot(): string | null {
  if (!HOME || !fs.existsSync(CHECKPOINT_ROOT)) return null;
  try {
    const stat = fs.lstatSync(CHECKPOINT_ROOT);
    if (stat.isSymbolicLink()) return null;
    return fs.realpathSync(CHECKPOINT_ROOT);
  } catch {
    return null;
  }
}

function resolveSafeExistingCheckpointDir(dirPath: string): string | null {
  const root = resolvedCheckpointRoot();
  if (!root || !fs.existsSync(dirPath)) return null;
  try {
    const stat = fs.lstatSync(dirPath);
    if (stat.isSymbolicLink() || !stat.isDirectory()) return null;
    const realDir = fs.realpathSync(dirPath);
    return isWithinDir(root, realDir) ? realDir : null;
  } catch {
    return null;
  }
}

function resolveSafeExistingCheckpointFile(filePath: string, allowedDir: string): string | null {
  if (!fs.existsSync(filePath)) return null;
  try {
    const stat = fs.lstatSync(filePath);
    if (stat.isSymbolicLink() || !stat.isFile()) return null;
    const realFile = fs.realpathSync(filePath);
    return isWithinDir(allowedDir, realFile) ? realFile : null;
  } catch {
    return null;
  }
}

function ensureSafeCheckpointRootForWrites(): string {
  if (!HOME) {
    throw new Error("Home directory is not set");
  }
  if (!fs.existsSync(CHECKPOINT_ROOT)) {
    fs.mkdirSync(CHECKPOINT_ROOT, { recursive: true, mode: 0o700 });
  }
  const stat = fs.lstatSync(CHECKPOINT_ROOT);
  if (stat.isSymbolicLink() || !stat.isDirectory()) {
    throw new Error("Checkpoint root is unsafe");
  }
  return fs.realpathSync(CHECKPOINT_ROOT);
}

function ensureSafeCheckpointDirForWrites(sessionId: string): string {
  const root = ensureSafeCheckpointRootForWrites();
  const safeSession = sanitizeSessionId(sessionId);
  const dir = checkpointSessionDir(safeSession);
  if (fs.existsSync(dir)) {
    const stat = fs.lstatSync(dir);
    if (stat.isSymbolicLink() || !stat.isDirectory()) {
      throw new Error("Checkpoint session directory is unsafe");
    }
  } else {
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  }
  const realDir = fs.existsSync(dir)
    ? fs.realpathSync(dir)
    : path.join(root, safeSession);
  if (!isWithinDir(root, realDir)) {
    throw new Error("Checkpoint session directory escapes checkpoint root");
  }
  return realDir;
}

function safeCheckpointFilePathForWrite(dir: string, filename: string): string {
  const target = path.join(dir, filename);
  if (fs.existsSync(target)) {
    const stat = fs.lstatSync(target);
    if (stat.isSymbolicLink() || !stat.isFile()) {
      throw new Error("Checkpoint file is unsafe");
    }
    const realFile = fs.realpathSync(target);
    if (!isWithinDir(dir, realFile)) {
      throw new Error("Checkpoint file escapes checkpoint session directory");
    }
    return realFile;
  }

  const resolved = path.resolve(target);
  if (!isWithinDir(dir, resolved)) {
    throw new Error("Checkpoint file escapes checkpoint session directory");
  }
  return resolved;
}

function safeCheckpointRootFilePathForWrite(filename: string): string {
  const root = ensureSafeCheckpointRootForWrites();
  const target = path.join(root, filename);
  if (fs.existsSync(target)) {
    const stat = fs.lstatSync(target);
    if (stat.isSymbolicLink() || !stat.isFile()) {
      throw new Error("Checkpoint root file is unsafe");
    }
    const realFile = fs.realpathSync(target);
    if (!isWithinDir(root, realFile)) {
      throw new Error("Checkpoint root file escapes checkpoint root");
    }
    return realFile;
  }

  const resolved = path.resolve(target);
  if (!isWithinDir(root, resolved)) {
    throw new Error("Checkpoint root file escapes checkpoint root");
  }
  return resolved;
}

export function checkpointSessionDir(sessionId: string): string {
  return path.join(CHECKPOINT_ROOT, sanitizeSessionId(sessionId));
}

export function checkpointManifestPath(sessionId: string): string {
  return path.join(checkpointSessionDir(sessionId), "manifest.jsonl");
}

function checkpointStatePath(sessionId: string): string {
  return path.join(checkpointSessionDir(sessionId), STATE_FILENAME);
}

function persistState(sessionId: string, state: SessionCheckpointState): void {
  const dir = ensureSafeCheckpointDirForWrites(sessionId);
  const statePath = safeCheckpointFilePathForWrite(dir, STATE_FILENAME);
  fs.writeFileSync(
    statePath,
    JSON.stringify(
      {
        capturedFillBands: [...state.capturedFillBands],
        capturedQualityThresholds: [...state.capturedQualityThresholds],
        capturedMilestones: [...state.capturedMilestones],
        lastCheckpointAt: state.lastCheckpointAt,
        lastEvaluatedAt: state.lastEvaluatedAt,
        editWriteCount: state.editWriteCount,
        editedFiles: [...state.editedFiles],
        editBatchMarkerWrites: state.editBatchMarkerWrites,
        editBatchMarkerFiles: state.editBatchMarkerFiles,
      },
      null,
      2
    ),
    { encoding: "utf-8", mode: 0o600 }
  );
}

function loadPersistedState(sessionId: string): SessionCheckpointState | null {
  const sessionDir = resolveSafeExistingCheckpointDir(checkpointSessionDir(sessionId));
  if (!sessionDir) return null;
  const statePath = resolveSafeExistingCheckpointFile(path.join(sessionDir, STATE_FILENAME), sessionDir);
  if (!statePath) return null;

  try {
    const raw = JSON.parse(fs.readFileSync(statePath, "utf-8")) as {
      capturedFillBands?: number[];
      capturedQualityThresholds?: number[];
      capturedMilestones?: string[];
      lastCheckpointAt?: number;
      lastEvaluatedAt?: number;
      editWriteCount?: number;
      editedFiles?: string[];
      editBatchMarkerWrites?: number;
      editBatchMarkerFiles?: number;
    };

    return {
      capturedFillBands: new Set(raw.capturedFillBands ?? []),
      capturedQualityThresholds: new Set(raw.capturedQualityThresholds ?? []),
      capturedMilestones: new Set(raw.capturedMilestones ?? []),
      lastCheckpointAt: raw.lastCheckpointAt ?? 0,
      lastEvaluatedAt: raw.lastEvaluatedAt ?? 0,
      editWriteCount: raw.editWriteCount ?? 0,
      editedFiles: new Set(raw.editedFiles ?? []),
      editBatchMarkerWrites: raw.editBatchMarkerWrites ?? 0,
      editBatchMarkerFiles: raw.editBatchMarkerFiles ?? 0,
    };
  } catch {
    return null;
  }
}

export function getCheckpointState(sessionId: string): SessionCheckpointState {
  const existing = states.get(sessionId);
  if (existing) return existing;

  const persisted = loadPersistedState(sessionId);
  if (persisted) {
    states.set(sessionId, persisted);
    return persisted;
  }

  const created: SessionCheckpointState = {
    capturedFillBands: new Set<number>(),
    capturedQualityThresholds: new Set<number>(),
    capturedMilestones: new Set<string>(),
    lastCheckpointAt: 0,
    lastEvaluatedAt: 0,
    editWriteCount: 0,
    editedFiles: new Set<string>(),
    editBatchMarkerWrites: 0,
    editBatchMarkerFiles: 0,
  };
  states.set(sessionId, created);
  return created;
}

export function clearCheckpointState(sessionId: string): void {
  states.delete(sessionId);
}

export function registerWriteEvent(sessionId: string, filePath?: string): SessionCheckpointState {
  const state = getCheckpointState(sessionId);
  state.editWriteCount += 1;
  if (filePath) state.editedFiles.add(filePath);
  return state;
}

export function shouldEvaluateRuntimeState(sessionId: string, nowMs: number = Date.now()): boolean {
  const state = getCheckpointState(sessionId);
  return nowMs - state.lastEvaluatedAt >= EVALUATION_COOLDOWN_MS;
}

export function markEvaluated(sessionId: string, nowMs: number = Date.now()): void {
  const state = getCheckpointState(sessionId);
  state.lastEvaluatedAt = nowMs;
}

function checkpointCooldownActive(state: SessionCheckpointState, nowMs: number): boolean {
  return nowMs - state.lastCheckpointAt < CHECKPOINT_COOLDOWN_MS;
}

export function buildRuntimeSnapshot(
  run: AgentRun,
  contextAudit?: ContextAudit | null
): RuntimeSnapshot {
  const ctxWindow = contextWindowForModel(run.model);
  const dynamicFill = ctxWindow > 0 ? (run.tokens.input / ctxWindow) * 100 : 0;
  const overheadFill = contextAudit ? (contextAudit.totalOverhead / ctxWindow) * 100 : 0;
  const fillPct = Math.max(dynamicFill, overheadFill);
  const qualityScore = scoreSessionQuality(run).score;
  return {
    fillPct,
    qualityScore,
  };
}

export function maybeDecideSnapshotCheckpoint(
  sessionId: string,
  snapshot: RuntimeSnapshot,
  nowMs: number = Date.now()
): CheckpointDecision | null {
  const state = getCheckpointState(sessionId);
  if (checkpointCooldownActive(state, nowMs)) return null;

  let targetBand: number | null = null;
  for (const band of FILL_BANDS) {
    if (snapshot.fillPct >= band && !state.capturedFillBands.has(band)) {
      targetBand = band;
    }
  }
  if (targetBand !== null) {
    return {
      trigger: `progressive-${targetBand}`,
      fillPct: snapshot.fillPct,
      qualityScore: snapshot.qualityScore,
    };
  }

  for (const threshold of QUALITY_THRESHOLDS) {
    if (snapshot.qualityScore < threshold && !state.capturedQualityThresholds.has(threshold)) {
      return {
        trigger: `quality-${threshold}`,
        fillPct: snapshot.fillPct,
        qualityScore: snapshot.qualityScore,
      };
    }
  }

  return null;
}

export function maybeDecidePreFanoutCheckpoint(
  sessionId: string,
  snapshot?: RuntimeSnapshot,
  nowMs: number = Date.now()
): CheckpointDecision | null {
  const state = getCheckpointState(sessionId);
  if (checkpointCooldownActive(state, nowMs) || state.capturedMilestones.has("pre-fanout")) {
    return null;
  }
  return {
    trigger: "milestone-pre-fanout",
    fillPct: snapshot?.fillPct,
    qualityScore: snapshot?.qualityScore,
  };
}

export function maybeDecideEditBatchCheckpoint(
  sessionId: string,
  snapshot?: RuntimeSnapshot,
  nowMs: number = Date.now()
): CheckpointDecision | null {
  const state = getCheckpointState(sessionId);
  if (checkpointCooldownActive(state, nowMs)) return null;

  const writeDelta = state.editWriteCount - state.editBatchMarkerWrites;
  const fileDelta = state.editedFiles.size - state.editBatchMarkerFiles;
  if (
    writeDelta < EDIT_BATCH_WRITE_THRESHOLD &&
    fileDelta < EDIT_BATCH_FILE_THRESHOLD
  ) {
    return null;
  }

  return {
    trigger: "milestone-edit-batch",
    fillPct: snapshot?.fillPct,
    qualityScore: snapshot?.qualityScore,
  };
}

export function recordCheckpointDecision(sessionId: string, trigger: string, nowMs: number = Date.now()): void {
  const state = getCheckpointState(sessionId);
  state.lastCheckpointAt = nowMs;

  if (trigger.startsWith("progressive-")) {
    const band = Number.parseInt(trigger.split("-")[1] ?? "", 10);
    if (!Number.isNaN(band)) state.capturedFillBands.add(band);
    persistState(sessionId, state);
    return;
  }

  if (trigger.startsWith("quality-")) {
    const threshold = Number.parseInt(trigger.split("-")[1] ?? "", 10);
    if (!Number.isNaN(threshold)) state.capturedQualityThresholds.add(threshold);
    persistState(sessionId, state);
    return;
  }

  if (trigger === "milestone-pre-fanout") {
    state.capturedMilestones.add("pre-fanout");
    persistState(sessionId, state);
    return;
  }

  if (trigger === "milestone-edit-batch") {
    state.editBatchMarkerWrites = state.editWriteCount;
    state.editBatchMarkerFiles = state.editedFiles.size;
  }

  persistState(sessionId, state);
}

export function registerCheckpointCapture(
  sessionId: string,
  trigger: CheckpointTrigger,
  telemetry: Partial<CheckpointTelemetry>
): void {
  appendCheckpointEvent(sessionId, trigger, telemetry);
  recordCheckpointDecision(sessionId, trigger);
}

function appendCheckpointEvent(
  sessionId: string,
  trigger: CheckpointTrigger,
  telemetry: Partial<CheckpointTelemetry>
): void {
  if (!CHECKPOINT_TELEMETRY_ENABLED) return;
  try {
    const eventPath = safeCheckpointRootFilePathForWrite(EVENTS_FILENAME);
    const event = {
      timestamp: new Date().toISOString(),
      platform: "openclaw",
      sessionId: sanitizeSessionId(sessionId),
      trigger,
      fillPct: telemetry.fillPct,
      qualityScore: telemetry.qualityScore,
      toolName: telemetry.toolName,
      eventKind: telemetry.eventKind,
      model: telemetry.model,
    };
    const fd = fs.openSync(eventPath, "a", 0o600);
    try {
      fs.writeSync(fd, JSON.stringify(event) + "\n");
    } finally {
      fs.closeSync(fd);
    }
  } catch {
    // Telemetry is best-effort only.
  }
}

export function getCheckpointTelemetrySummary(days: number = 7): CheckpointTelemetrySummary {
  const root = resolvedCheckpointRoot();
  const eventLog = root ? resolveSafeExistingCheckpointFile(path.join(root, EVENTS_FILENAME), root) : null;
  const cutoff = Date.now() - days * 86400 * 1000;
  const events: Array<Record<string, unknown> & { _ts?: number }> = [];

  if (eventLog) {
    try {
      const lines = fs.readFileSync(eventLog, "utf-8").split("\n").filter(Boolean);
      for (const line of lines) {
        try {
          const event = JSON.parse(line) as Record<string, unknown>;
          const ts = typeof event.timestamp === "string" ? Date.parse(event.timestamp) : NaN;
          if (Number.isNaN(ts)) continue;
          events.push({ ...event, _ts: ts });
        } catch {
          continue;
        }
      }
    } catch {
      // ignore
    }
  }

  const recent = events.filter((event) => (event._ts ?? 0) >= cutoff);
  const byTrigger: Record<string, number> = {};
  for (const event of recent) {
    const trigger = typeof event.trigger === "string" ? event.trigger : "unknown";
    byTrigger[trigger] = (byTrigger[trigger] ?? 0) + 1;
  }

  recent.sort((a, b) => (b._ts ?? 0) - (a._ts ?? 0));
  const last = recent[0];
  return {
    enabled: CHECKPOINT_TELEMETRY_ENABLED,
    eventLog: root ? path.join(root, EVENTS_FILENAME) : path.join(CHECKPOINT_ROOT, EVENTS_FILENAME),
    days,
    totalEvents: events.length,
    recentEvents: recent.length,
    byTrigger: Object.fromEntries(Object.entries(byTrigger).sort(([a], [b]) => a.localeCompare(b))),
    lastEvent: last
      ? {
          timestamp: typeof last.timestamp === "string" ? last.timestamp : undefined,
          sessionId: typeof last.sessionId === "string" ? last.sessionId : undefined,
          trigger: typeof last.trigger === "string" ? last.trigger : undefined,
          fillPct: typeof last.fillPct === "number" ? last.fillPct : undefined,
          qualityScore: typeof last.qualityScore === "number" ? last.qualityScore : undefined,
        }
      : null,
  };
}

export function getCheckpointFiles(sessionId: string): Array<{ path: string; trigger: string; createdAt: number }> {
  const sessionDir = resolveSafeExistingCheckpointDir(checkpointSessionDir(sessionId));
  if (!sessionDir) return [];

  const manifestPath = resolveSafeExistingCheckpointFile(path.join(sessionDir, "manifest.jsonl"), sessionDir);
  if (!manifestPath) return [];

  const entries: Array<{ path: string; trigger: string; createdAt: number }> = [];
  try {
    const lines = fs.readFileSync(manifestPath, "utf-8").split("\n").filter(Boolean);
    for (const line of lines) {
      try {
        const entry = JSON.parse(line) as { file?: string; trigger?: string; createdAt?: string };
        if (!entry.file || !entry.trigger || !entry.createdAt) continue;
        const createdAt = Date.parse(entry.createdAt);
        if (Number.isNaN(createdAt)) continue;
        const safeFile = resolveSafeExistingCheckpointFile(entry.file, sessionDir);
        if (!safeFile) continue;
        entries.push({ path: safeFile, trigger: entry.trigger, createdAt });
      } catch {
        continue;
      }
    }
  } catch {
    return [];
  }
  return entries;
}

export function cleanupPolicyArtifacts(maxAgeDays: number = 7): number {
  const root = resolvedCheckpointRoot();
  if (!root) return 0;
  const cutoff = Date.now() - maxAgeDays * 86400 * 1000;
  let cleaned = 0;

  for (const dir of fs.readdirSync(root)) {
    const fullDir = path.join(root, dir);
    const safeDir = resolveSafeExistingCheckpointDir(fullDir);
    if (!safeDir) continue;

    for (const file of fs.readdirSync(safeDir)) {
      const filePath = path.join(safeDir, file);
      const safeFile = resolveSafeExistingCheckpointFile(filePath, safeDir);
      if (!safeFile) continue;
      try {
        const fileStat = fs.statSync(safeFile);
        if (fileStat.mtimeMs < cutoff) {
          fs.rmSync(safeFile, { force: true });
          cleaned += 1;
        }
      } catch {
        continue;
      }
    }

    try {
      if (fs.readdirSync(safeDir).length === 0) fs.rmdirSync(safeDir);
    } catch {
      // ignore
    }
  }

  return cleaned;
}

export function getCheckpointHealth(): CheckpointHealth {
  const issues: string[] = [];
  let sessionCount = 0;
  let checkpointCount = 0;
  let policyCount = 0;
  let checkpointBytes = 0;
  const telemetry = getCheckpointTelemetrySummary(7);

  if (!HOME) issues.push("Home directory is not set.");
  const root = resolvedCheckpointRoot();
  if (!fs.existsSync(CHECKPOINT_ROOT)) {
    issues.push("Checkpoint root does not exist yet.");
  } else if (!root) {
    issues.push("Checkpoint root is invalid or symlinked.");
  } else {
    for (const dir of fs.readdirSync(root)) {
      const fullDir = path.join(root, dir);
      const safeDir = resolveSafeExistingCheckpointDir(fullDir);
      if (!safeDir) {
        issues.push(`Skipped unsafe checkpoint directory: ${fullDir}`);
        continue;
      }
      sessionCount += 1;
      for (const file of fs.readdirSync(safeDir)) {
        const filePath = path.join(safeDir, file);
        const safeFile = resolveSafeExistingCheckpointFile(filePath, safeDir);
        if (!safeFile) {
          issues.push(`Skipped unsafe checkpoint file: ${filePath}`);
          continue;
        }
        try {
          const fileStat = fs.statSync(safeFile);
          checkpointBytes += fileStat.size;
        } catch {
          continue;
        }
        if (file.endsWith(".md")) checkpointCount += 1;
        if (file === STATE_FILENAME) policyCount += 1;
      }
    }
  }

  return {
    checkpointRoot: CHECKPOINT_ROOT,
    sessionCount,
    checkpointCount,
    policyCount,
    pendingCount: 0,
    checkpointBytes,
    recentEventCount: telemetry.recentEvents,
    lastTrigger: telemetry.lastEvent?.trigger,
    issues,
  };
}
