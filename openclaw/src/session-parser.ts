/**
 * OpenClaw session JSONL parser.
 *
 * Reads ~/.openclaw/agents/{agentId}/sessions/{sessionId}.jsonl
 * and normalizes into AgentRun objects.
 *
 * OpenClaw JSONL differences from Claude Code:
 * - Token fields: inputTokens, outputTokens, totalTokens (no cache breakdown)
 * - Agent-scoped: sessions live under agent directories
 * - No subagent nesting (agents are top-level)
 */

import * as fs from "fs";
import * as path from "path";
import { AgentRun, TokenBreakdown, Outcome } from "./models";
import { normalizeModelName } from "./pricing";
import { calculateCost } from "./pricing";

const HOME = process.env.HOME ?? process.env.USERPROFILE ?? "";
const OPENCLAW_DIRS = [
  path.join(HOME, ".openclaw"),
  path.join(HOME, ".clawdbot"),
  path.join(HOME, ".moltbot"),
];

/**
 * Find the first existing OpenClaw data directory.
 */
export function findOpenClawDir(): string | null {
  for (const dir of OPENCLAW_DIRS) {
    if (fs.existsSync(dir)) return dir;
  }
  return null;
}

/**
 * Discover all agent directories under the OpenClaw data root.
 */
export function listAgents(openclawDir: string): string[] {
  const agentsDir = path.join(openclawDir, "agents");
  if (!fs.existsSync(agentsDir)) return [];

  try {
    return fs
      .readdirSync(agentsDir, { withFileTypes: true })
      .filter((d) => d.isDirectory())
      .map((d) => d.name);
  } catch {
    return [];
  }
}

/**
 * Find all session JSONL files for a given agent, optionally filtered by age.
 *
 * Returns array of { filePath, agentName, sessionId, mtime } sorted newest-first.
 */
export function findSessionFiles(
  openclawDir: string,
  agentName: string,
  days: number = 30
): Array<{
  filePath: string;
  agentName: string;
  sessionId: string;
  mtime: number;
}> {
  const sessionsDir = path.join(openclawDir, "agents", agentName, "sessions");
  if (!fs.existsSync(sessionsDir)) return [];

  const cutoff = Date.now() - days * 86400 * 1000;
  const results: Array<{
    filePath: string;
    agentName: string;
    sessionId: string;
    mtime: number;
  }> = [];

  try {
    const files = fs.readdirSync(sessionsDir);
    for (const file of files) {
      if (!file.endsWith(".jsonl")) continue;

      const filePath = path.join(sessionsDir, file);
      try {
        const stat = fs.statSync(filePath);
        if (stat.mtimeMs >= cutoff) {
          results.push({
            filePath,
            agentName,
            sessionId: path.basename(file, ".jsonl"),
            mtime: stat.mtimeMs,
          });
        }
      } catch {
        continue;
      }
    }
  } catch {
    return [];
  }

  results.sort((a, b) => b.mtime - a.mtime);
  return results;
}

/**
 * Parse a line of JSON safely. Returns null on any error.
 */
function parseLine(line: string): Record<string, unknown> | null {
  try {
    const trimmed = line.trim();
    if (!trimmed) return null;
    return JSON.parse(trimmed) as Record<string, unknown>;
  } catch {
    return null;
  }
}

/**
 * Parse a single OpenClaw session JSONL file into an AgentRun.
 *
 * OpenClaw JSONL format:
 * - Each line is a JSON object with at minimum a "type" field
 * - Token data in assistant messages under "usage" or top-level fields
 * - Model ID in "model" field of assistant messages
 */
export function parseSession(
  filePath: string,
  agentName: string,
  openclawDir?: string
): AgentRun | null {
  let content: string;
  try {
    const stat = fs.statSync(filePath);
    if (stat.size > 50 * 1024 * 1024) return null; // Skip files >50MB to prevent OOM
    content = fs.readFileSync(filePath, "utf-8");
  } catch {
    return null;
  }

  const lines = content.split("\n");

  let totalInput = 0;
  let totalOutput = 0;
  let messageCount = 0;
  const modelUsage: Map<string, number> = new Map();
  const toolsUsed: Set<string> = new Set();
  let firstTs: Date | null = null;
  let lastTs: Date | null = null;

  for (const line of lines) {
    const record = parseLine(line);
    if (!record) continue;

    // Timestamp
    const tsRaw = record.timestamp as string | undefined;
    if (tsRaw) {
      try {
        const ts = new Date(tsRaw);
        if (!isNaN(ts.getTime())) {
          if (!firstTs) firstTs = ts;
          lastTs = ts;
        }
      } catch {
        // skip bad timestamps
      }
    }

    const recType = record.type as string | undefined;

    // Count messages
    if (recType === "user" || recType === "assistant") {
      messageCount++;
    }

    // Extract token data from assistant messages
    if (recType === "assistant") {
      const msg = record.message as Record<string, unknown> | undefined;
      const usage =
        (msg?.usage as Record<string, unknown>) ??
        (record.usage as Record<string, unknown>);

      if (usage) {
        // OpenClaw uses inputTokens (total input, no cache split)
        const inp =
          (usage.inputTokens as number) ??
          (usage.input_tokens as number) ??
          0;
        const out =
          (usage.outputTokens as number) ??
          (usage.output_tokens as number) ??
          0;

        totalInput += inp;
        totalOutput += out;

        // Track model usage
        const modelId =
          (msg?.model as string) ?? (record.model as string) ?? "unknown";
        const current = modelUsage.get(modelId) ?? 0;
        modelUsage.set(modelId, current + inp + out);
      }

      // Extract tool usage
      const msgContent = msg?.content;
      if (Array.isArray(msgContent)) {
        for (const block of msgContent) {
          if (
            typeof block === "object" &&
            block !== null &&
            (block as Record<string, unknown>).type === "tool_use"
          ) {
            const name = (block as Record<string, unknown>).name;
            if (typeof name === "string") toolsUsed.add(name);
          }
        }
      }
    }
  }

  if (messageCount === 0) return null;

  // Duration
  let durationSeconds = 0;
  if (firstTs && lastTs) {
    durationSeconds = Math.max(
      0,
      (lastTs.getTime() - firstTs.getTime()) / 1000
    );
  }

  // Dominant model
  let dominantModelRaw = "unknown";
  let maxUsage = 0;
  for (const [model, usage] of modelUsage) {
    if (usage > maxUsage) {
      maxUsage = usage;
      dominantModelRaw = model;
    }
  }
  const model = normalizeModelName(dominantModelRaw) ?? dominantModelRaw;

  const tokens: TokenBreakdown = {
    input: totalInput,
    output: totalOutput,
    cacheRead: 0,
    cacheWrite: 0,
  };

  // Determine outcome
  let outcome: Outcome = "success";
  if (messageCount <= 2 && totalOutput < 200) {
    outcome = "abandoned";
  } else if (totalOutput < 100 && totalInput > 50_000) {
    outcome = "empty";
  }

  const costUsd = calculateCost(tokens, model, openclawDir);

  return {
    system: "openclaw",
    sessionId: path.basename(filePath, ".jsonl"),
    agentName,
    project: agentName, // OpenClaw scopes by agent, not project
    timestamp: firstTs ?? new Date(),
    durationSeconds,
    tokens,
    costUsd,
    model,
    runType: "manual",
    outcome,
    messageCount,
    toolsUsed: Array.from(toolsUsed).sort(),
    sourcePath: filePath,
  };
}

/**
 * Load session-level token aggregates from sessions.json.
 * OpenClaw stores authoritative totals here (inputTokens, outputTokens, contextTokens).
 * Returns a map of sessionId -> { inputTokens, outputTokens, contextTokens }.
 */
function loadSessionIndex(
  openclawDir: string,
  agentName: string
): Map<string, { inputTokens: number; outputTokens: number; contextTokens: number }> {
  const result = new Map<string, { inputTokens: number; outputTokens: number; contextTokens: number }>();
  const indexPath = path.join(openclawDir, "agents", agentName, "sessions", "sessions.json");

  try {
    if (!fs.existsSync(indexPath)) return result;
    const stat = fs.statSync(indexPath);
    if (stat.size > 10_000_000) return result; // Skip huge index files
    const data = JSON.parse(fs.readFileSync(indexPath, "utf-8"));

    // sessions.json can be an array or object of session entries
    const entries = Array.isArray(data) ? data : Object.values(data);
    for (const entry of entries) {
      if (typeof entry !== "object" || entry === null) continue;
      const e = entry as Record<string, unknown>;
      const id = e.id as string ?? e.sessionId as string;
      if (!id) continue;
      result.set(id, {
        inputTokens: Number(e.inputTokens ?? 0),
        outputTokens: Number(e.outputTokens ?? 0),
        contextTokens: Number(e.contextTokens ?? 0),
      });
    }
  } catch {
    // sessions.json not available or malformed
  }

  return result;
}

/**
 * Scan all agents and sessions within the given day window.
 *
 * Returns all parsed AgentRuns sorted by timestamp (newest first).
 */
export function scanAllSessions(
  openclawDir: string,
  days: number = 30
): AgentRun[] {
  const agents = listAgents(openclawDir);
  const allRuns: AgentRun[] = [];

  for (const agent of agents) {
    const sessionIndex = loadSessionIndex(openclawDir, agent);
    const files = findSessionFiles(openclawDir, agent, days);
    for (const { filePath, agentName, sessionId } of files) {
      const run = parseSession(filePath, agentName, openclawDir);
      if (!run) continue;

      // If JSONL parsing yielded zero tokens, fall back to sessions.json
      if (run.tokens.input === 0 && run.tokens.output === 0) {
        const indexed = sessionIndex.get(sessionId);
        if (indexed) {
          run.tokens.input = indexed.inputTokens;
          run.tokens.output = indexed.outputTokens;
          run.costUsd = calculateCost(run.tokens, run.model, openclawDir);
        }
      }

      allRuns.push(run);
    }
  }

  allRuns.sort(
    (a, b) => b.timestamp.getTime() - a.timestamp.getTime()
  );
  return allRuns;
}

/**
 * Classify runs as heartbeat/cron based on OpenClaw cron config.
 *
 * Reads ~/.openclaw/cron/ for heartbeat configurations and marks
 * matching agent runs accordingly.
 */
export function classifyCronRuns(
  openclawDir: string,
  runs: AgentRun[]
): void {
  const cronDir = path.join(openclawDir, "cron");
  if (!fs.existsSync(cronDir)) return;

  // Read cron config to identify heartbeat agents
  const cronAgents: Set<string> = new Set();
  try {
    const configPath = path.join(openclawDir, "config.json");
    if (fs.existsSync(configPath)) {
      const config = JSON.parse(fs.readFileSync(configPath, "utf-8"));
      const crons = config.cron ?? config.heartbeat ?? {};
      for (const key of Object.keys(crons)) {
        cronAgents.add(key);
      }
    }
  } catch {
    // No cron config, skip
  }

  // Also check cron directory for agent-named configs
  try {
    const cronFiles = fs.readdirSync(cronDir);
    for (const file of cronFiles) {
      if (file.endsWith(".json") || file.endsWith(".yaml")) {
        cronAgents.add(path.basename(file, path.extname(file)));
      }
    }
  } catch {
    // skip
  }

  // Mark matching runs
  for (const run of runs) {
    if (cronAgents.has(run.agentName)) {
      (run as { runType: string }).runType = "heartbeat";
    }
  }
}
