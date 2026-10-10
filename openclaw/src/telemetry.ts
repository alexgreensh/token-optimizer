/**
 * v5 compression event telemetry for OpenClaw.
 *
 * Schema matches the Python `compression_events` SQLite table shape so the
 * two plugins can be reasoned about side-by-side, but OpenClaw writes to an
 * append-only JSONL file under `~/.openclaw/token-optimizer/` to avoid
 * pulling in a native SQLite dependency. Each plugin owns its own data file;
 * there is no cross-plugin read path.
 */

import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { estimateTokens as sharedEstimateTokens } from "./token-estimate";
import { redact } from "./redact";

export interface CompressionEvent {
  timestamp: string;
  session_id: string | null;
  feature: string;
  command_pattern: string | null;
  original_tokens: number;
  compressed_tokens: number;
  compression_ratio: number;
  quality_preserved: 0 | 1;
  verified: 0 | 1;
  detail: string | null;
}

export interface CompressionFeatureSummary {
  events: number;
  original_tokens: number;
  compressed_tokens: number;
  tokens_saved: number;
  avg_ratio: number;
}

export interface CompressionSummary {
  total_events: number;
  total_original_tokens: number;
  total_compressed_tokens: number;
  total_tokens_saved: number;
  overall_ratio: number;
  by_feature: Record<string, CompressionFeatureSummary>;
  period_days: number;
}

const HOME = process.env.HOME ?? process.env.USERPROFILE ?? os.homedir();
const TELEMETRY_DIR = path.join(HOME, ".openclaw", "token-optimizer");
const TELEMETRY_PATH = path.join(TELEMETRY_DIR, "compression-events.jsonl");
const MAX_DETAIL_LEN = 200;

function ensureDir(): void {
  try {
    // 0o700 so the telemetry directory is readable only by the owner on
    // shared hosts. The JSONL file itself is already created 0o600 on
    // first append.
    fs.mkdirSync(TELEMETRY_DIR, { recursive: true, mode: 0o700 });
  } catch {
    // Ignore — the next write attempt will surface the real error via its
    // own try/catch. Never crash the gateway over telemetry setup.
  }
}

function estimateTokens(text: string): number {
  // Calibrated estimator shared with the Python side (3.3 chars/tok + CJK),
  // replacing the old bytes/4 proxy that undercounts ~15-20%.
  return sharedEstimateTokens(text);
}

export interface LogCompressionEventInput {
  feature: string;
  sessionId?: string | null;
  commandPattern?: string | null;
  originalText?: string;
  compressedText?: string;
  originalTokens?: number;
  compressedTokens?: number;
  qualityPreserved?: boolean;
  verified?: boolean;
  detail?: string | null;
}

/**
 * Append one compression event to the telemetry log. Never throws —
 * returns true on success, false on any failure. Callers should ignore
 * the return value unless they genuinely need to branch on it.
 */
export function logCompressionEvent(input: LogCompressionEventInput): boolean {
  try {
    const originalTokens =
      input.originalTokens ?? estimateTokens(input.originalText ?? "");
    const compressedTokens =
      input.compressedTokens ?? estimateTokens(input.compressedText ?? "");
    const ratio =
      originalTokens > 0
        ? Math.round((1 - compressedTokens / originalTokens) * 10000) / 10000
        : 0;

    const detailRaw = input.detail ?? null;
    const detail =
      detailRaw && detailRaw.length > MAX_DETAIL_LEN
        ? detailRaw.slice(0, MAX_DETAIL_LEN)
        : detailRaw;

    const event: CompressionEvent = {
      timestamp: new Date().toISOString(),
      session_id: input.sessionId ?? null,
      feature: input.feature,
      // Boundary redaction: command_pattern/detail embed tool labels and file
      // names that can carry a credential shape. Redact here so no caller can
      // persist them raw. Fail-closed is not applicable (pure function, no
      // I/O dependency); the pattern set itself is in ./redact.
      command_pattern: input.commandPattern ? redact(input.commandPattern) : null,
      original_tokens: originalTokens,
      compressed_tokens: compressedTokens,
      compression_ratio: ratio,
      quality_preserved: input.qualityPreserved === false ? 0 : 1,
      verified: input.verified ? 1 : 0,
      detail: detail ? redact(detail) : null,
    };

    ensureDir();
    fs.appendFileSync(TELEMETRY_PATH, JSON.stringify(event) + "\n", {
      encoding: "utf8",
      mode: 0o600,
    });
    return true;
  } catch {
    return false;
  }
}

function parseLine(line: string): CompressionEvent | null {
  try {
    const parsed = JSON.parse(line) as unknown;
    if (!parsed || typeof parsed !== "object") return null;
    return parsed as CompressionEvent;
  } catch {
    return null;
  }
}

/** Read events from the telemetry log within the given window. */
export function readRecentEvents(days: number): CompressionEvent[] {
  if (!fs.existsSync(TELEMETRY_PATH)) return [];
  const cutoff = Date.now() - days * 24 * 60 * 60 * 1000;
  const out: CompressionEvent[] = [];
  try {
    const content = fs.readFileSync(TELEMETRY_PATH, "utf8");
    for (const line of content.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const event = parseLine(trimmed);
      if (!event) continue;
      const ts = Date.parse(event.timestamp);
      if (Number.isNaN(ts) || ts < cutoff) continue;
      out.push(event);
    }
  } catch {
    // Silent: a corrupt telemetry file returns whatever parsed so far.
  }
  return out;
}

/** Summary equivalent of Python's `_get_compression_summary`. */
export function getCompressionSummary(days = 30): CompressionSummary {
  const events = readRecentEvents(days);
  const byFeature: Record<string, CompressionFeatureSummary> = {};
  let totalOriginal = 0;
  let totalCompressed = 0;

  for (const event of events) {
    const key = event.feature;
    if (!byFeature[key]) {
      byFeature[key] = {
        events: 0,
        original_tokens: 0,
        compressed_tokens: 0,
        tokens_saved: 0,
        avg_ratio: 0,
      };
    }
    const slot = byFeature[key];
    slot.events += 1;
    slot.original_tokens += event.original_tokens || 0;
    slot.compressed_tokens += event.compressed_tokens || 0;
    slot.avg_ratio += event.compression_ratio || 0;
    totalOriginal += event.original_tokens || 0;
    totalCompressed += event.compressed_tokens || 0;
  }

  for (const slot of Object.values(byFeature)) {
    slot.tokens_saved = slot.original_tokens - slot.compressed_tokens;
    slot.avg_ratio = slot.events > 0 ? slot.avg_ratio / slot.events : 0;
    slot.avg_ratio = Math.round(slot.avg_ratio * 10000) / 10000;
  }

  const overallRatio =
    totalOriginal > 0
      ? Math.round((1 - totalCompressed / totalOriginal) * 10000) / 10000
      : 0;

  return {
    total_events: events.length,
    total_original_tokens: totalOriginal,
    total_compressed_tokens: totalCompressed,
    total_tokens_saved: totalOriginal - totalCompressed,
    overall_ratio: overallRatio,
    by_feature: byFeature,
    period_days: days,
  };
}

/** Prune events older than `days`. Used by a background maintenance task.
 *
 * Atomic replace via tmp-file + rename so a concurrent `logCompressionEvent`
 * append during the prune window lands either in the pre-prune file (and
 * gets replaced) or against the post-prune file (and survives). Without
 * the atomic swap, an append that races with the read-filter-writeFileSync
 * sequence would be silently dropped.
 */
export function pruneOldEvents(days = 90): number {
  if (!fs.existsSync(TELEMETRY_PATH)) return 0;
  try {
    const cutoff = Date.now() - days * 24 * 60 * 60 * 1000;
    const kept: string[] = [];
    let dropped = 0;
    const content = fs.readFileSync(TELEMETRY_PATH, "utf8");
    for (const line of content.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const event = parseLine(trimmed);
      if (!event) {
        dropped += 1;
        continue;
      }
      const ts = Date.parse(event.timestamp);
      if (Number.isNaN(ts) || ts < cutoff) {
        dropped += 1;
        continue;
      }
      kept.push(trimmed);
    }
    const tmp = TELEMETRY_PATH + ".tmp";
    fs.writeFileSync(tmp, kept.join("\n") + (kept.length ? "\n" : ""), {
      encoding: "utf8",
      mode: 0o600,
    });
    fs.renameSync(tmp, TELEMETRY_PATH);
    return dropped;
  } catch {
    return 0;
  }
}
