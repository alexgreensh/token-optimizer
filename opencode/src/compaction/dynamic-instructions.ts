import type { SessionMode } from "../activity/tracker.js";

const MODE_INSTRUCTIONS: Record<SessionMode, string> = {
  code: [
    "PRESERVE: All file paths edited or created in this session.",
    "PRESERVE: Recent Edit/Write tool calls with their file paths and the intent behind each change.",
    "PRESERVE: Build/test outcomes and any error patterns being investigated.",
    "DROP: Full file contents already persisted to disk (keep paths, drop bodies).",
    "DROP: Intermediate Read results for files that were subsequently edited.",
  ].join("\n"),

  debug: [
    "PRESERVE: Error messages, stack traces, and exception types.",
    "PRESERVE: Hypotheses tested and their outcomes (confirmed/rejected).",
    "PRESERVE: Root cause analysis progress and remaining candidates.",
    "DROP: Verbose log output already analyzed.",
    "DROP: Read results for files confirmed not involved.",
  ].join("\n"),

  review: [
    "PRESERVE: Files reviewed and findings per file.",
    "PRESERVE: Code patterns flagged and severity assessments.",
    "PRESERVE: Coverage notes and areas not yet reviewed.",
    "DROP: Full file contents (keep paths and line references).",
    "DROP: Grep/Glob results that were scanned but yielded no findings.",
  ].join("\n"),

  infra: [
    "PRESERVE: Infrastructure commands executed and their outcomes.",
    "PRESERVE: Service states, deployment steps completed.",
    "PRESERVE: Configuration changes made and their locations.",
    "DROP: Verbose command output already summarized.",
    "DROP: Repeated status checks with identical output.",
  ].join("\n"),

  general: [
    "PRESERVE: Key decisions and the reasoning behind them.",
    "PRESERVE: Action items and commitments made.",
    "PRESERVE: File paths mentioned as relevant to ongoing work.",
    "DROP: Exploratory reads that did not inform decisions.",
    "DROP: Verbose tool output already summarized.",
  ].join("\n"),
};

export function generateCompactionContext(
  mode: SessionMode,
  activeFiles: string[],
  qualityScore: number | null,
  fillPct: number | null,
): string[] {
  const context: string[] = [];

  context.push(`[Token Optimizer] Session mode: ${mode}`);
  context.push(MODE_INSTRUCTIONS[mode]);

  if (activeFiles.length > 0) {
    const sanitized = activeFiles.slice(0, 15).map((f) => f.replace(/[\r\n]/g, " ").slice(0, 256));
    context.push(`Active files (PRESERVE paths): ${sanitized.join(", ")}`);
  }

  if (qualityScore !== null) {
    context.push(`Context quality before compaction: ${Math.round(qualityScore)}/100`);
  }

  if (fillPct !== null && fillPct > 0.7) {
    context.push("HIGH FILL WARNING: Aggressively drop verbose output and intermediate results.");
  }

  return context;
}
