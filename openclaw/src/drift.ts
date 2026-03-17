/**
 * Drift Detection for OpenClaw.
 *
 * Snapshots the OpenClaw config at a point in time.
 * Later, diffs against current state to catch config creep.
 */

import * as fs from "fs";
import * as path from "path";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ConfigSnapshot {
  capturedAt: string;
  skillCount: number;
  agentCount: number;
  cronCount: number;
  soulMdSize: number;
  memoryMdSize: number;
  agentsMdSize: number;
  toolsMdSize: number;
  modelConfig: string;
  skills: string[];
  agents: string[];
}

export interface DriftReport {
  hasDrift: boolean;
  snapshotDate: string;
  changes: DriftChange[];
}

export interface DriftChange {
  component: string;
  type: "added" | "removed" | "changed";
  details: string;
}

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------

const HOME = process.env.HOME ?? process.env.USERPROFILE ?? "";
const SNAPSHOT_DIR = path.join(HOME, ".openclaw", "token-optimizer", "snapshots");

// ---------------------------------------------------------------------------
// Snapshot capture
// ---------------------------------------------------------------------------

function countDir(dir: string, ext?: string): { count: number; names: string[] } {
  if (!fs.existsSync(dir)) return { count: 0, names: [] };
  try {
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    const filtered = ext
      ? entries.filter((e) => e.name.endsWith(ext))
      : entries.filter((e) => e.isDirectory());
    return { count: filtered.length, names: filtered.map((e) => e.name).sort() };
  } catch {
    return { count: 0, names: [] };
  }
}

function fileSize(filePath: string): number {
  try {
    if (!fs.existsSync(filePath)) return 0;
    return fs.statSync(filePath).size;
  } catch {
    return 0;
  }
}

function readModelConfig(openclawDir: string): string {
  const configPath = path.join(openclawDir, "config.json");
  try {
    if (!fs.existsSync(configPath)) return "";
    const config = JSON.parse(fs.readFileSync(configPath, "utf-8"));
    const models = config.models ?? config.model ?? {};
    return JSON.stringify(models);
  } catch {
    return "";
  }
}

export function captureSnapshot(openclawDir: string): string {
  const skills = countDir(path.join(openclawDir, "skills"));
  const agents = countDir(path.join(openclawDir, "agents"));
  const crons = countDir(path.join(openclawDir, "cron"), ".json");

  const snapshot: ConfigSnapshot = {
    capturedAt: new Date().toISOString(),
    skillCount: skills.count,
    agentCount: agents.count,
    cronCount: crons.count,
    soulMdSize: fileSize(path.join(openclawDir, "SOUL.md")),
    memoryMdSize: fileSize(path.join(openclawDir, "MEMORY.md")),
    agentsMdSize: fileSize(path.join(openclawDir, "AGENTS.md")),
    toolsMdSize: fileSize(path.join(openclawDir, "TOOLS.md")),
    modelConfig: readModelConfig(openclawDir),
    skills: skills.names,
    agents: agents.names,
  };

  fs.mkdirSync(SNAPSHOT_DIR, { recursive: true, mode: 0o700 });
  const filename = `snapshot-${snapshot.capturedAt.slice(0, 10)}.json`;
  const filePath = path.join(SNAPSHOT_DIR, filename);
  fs.writeFileSync(filePath, JSON.stringify(snapshot, null, 2), { encoding: "utf-8", mode: 0o600 });
  return filePath;
}

// ---------------------------------------------------------------------------
// Drift detection
// ---------------------------------------------------------------------------

function loadLatestSnapshot(): ConfigSnapshot | null {
  if (!fs.existsSync(SNAPSHOT_DIR)) return null;

  try {
    const files = fs.readdirSync(SNAPSHOT_DIR)
      .filter((f) => f.startsWith("snapshot-") && f.endsWith(".json"))
      .sort()
      .reverse();

    if (files.length === 0) return null;

    const content = fs.readFileSync(path.join(SNAPSHOT_DIR, files[0]), "utf-8");
    return JSON.parse(content) as ConfigSnapshot;
  } catch {
    return null;
  }
}

function buildCurrentSnapshot(openclawDir: string): ConfigSnapshot {
  const skills = countDir(path.join(openclawDir, "skills"));
  const agents = countDir(path.join(openclawDir, "agents"));
  const crons = countDir(path.join(openclawDir, "cron"), ".json");

  return {
    capturedAt: new Date().toISOString(),
    skillCount: skills.count,
    agentCount: agents.count,
    cronCount: crons.count,
    soulMdSize: fileSize(path.join(openclawDir, "SOUL.md")),
    memoryMdSize: fileSize(path.join(openclawDir, "MEMORY.md")),
    agentsMdSize: fileSize(path.join(openclawDir, "AGENTS.md")),
    toolsMdSize: fileSize(path.join(openclawDir, "TOOLS.md")),
    modelConfig: readModelConfig(openclawDir),
    skills: skills.names,
    agents: agents.names,
  };
}

function diffArrays(
  label: string,
  prev: string[],
  curr: string[]
): DriftChange[] {
  const changes: DriftChange[] = [];
  const prevSet = new Set(prev);
  const currSet = new Set(curr);

  for (const name of curr) {
    if (!prevSet.has(name)) {
      changes.push({ component: label, type: "added", details: name });
    }
  }
  for (const name of prev) {
    if (!currSet.has(name)) {
      changes.push({ component: label, type: "removed", details: name });
    }
  }

  return changes;
}

function diffSize(
  component: string,
  prevSize: number,
  currSize: number,
  threshold: number = 100
): DriftChange | null {
  const diff = currSize - prevSize;
  if (Math.abs(diff) < threshold) return null;

  const direction = diff > 0 ? "grew" : "shrank";
  const absDiff = Math.abs(diff);
  return {
    component,
    type: "changed",
    details: `${direction} by ${absDiff} bytes (${prevSize} -> ${currSize})`,
  };
}

export function detectDrift(openclawDir: string): DriftReport {
  const prev = loadLatestSnapshot();

  if (!prev) {
    return {
      hasDrift: false,
      snapshotDate: "none",
      changes: [{
        component: "snapshot",
        type: "added",
        details: "No previous snapshot found. Run with --snapshot first.",
      }],
    };
  }

  const curr = buildCurrentSnapshot(openclawDir);
  const changes: DriftChange[] = [];

  // Skills diff
  changes.push(...diffArrays("Skills", prev.skills, curr.skills));

  // Agents diff
  changes.push(...diffArrays("Agents", prev.agents, curr.agents));

  // Cron count
  if (curr.cronCount !== prev.cronCount) {
    const diff = curr.cronCount - prev.cronCount;
    changes.push({
      component: "Cron configs",
      type: diff > 0 ? "added" : "removed",
      details: `${Math.abs(diff)} config(s) ${diff > 0 ? "added" : "removed"} (${prev.cronCount} -> ${curr.cronCount})`,
    });
  }

  // File size diffs
  const sizeDiffs = [
    diffSize("SOUL.md", prev.soulMdSize, curr.soulMdSize),
    diffSize("MEMORY.md", prev.memoryMdSize, curr.memoryMdSize),
    diffSize("AGENTS.md", prev.agentsMdSize, curr.agentsMdSize),
    diffSize("TOOLS.md", prev.toolsMdSize, curr.toolsMdSize),
  ];
  for (const d of sizeDiffs) {
    if (d) changes.push(d);
  }

  // Model config diff
  if (prev.modelConfig !== curr.modelConfig) {
    changes.push({
      component: "Model config",
      type: "changed",
      details: "Model configuration has changed since last snapshot.",
    });
  }

  return {
    hasDrift: changes.length > 0,
    snapshotDate: prev.capturedAt.slice(0, 10),
    changes,
  };
}
