/**
 * Coach module for OpenClaw Token Optimizer.
 *
 * Generates a holistic "health score" for an OpenClaw setup by analyzing
 * context overhead, skill usage, model routing, and session patterns.
 * Produces structured data consumed by the Coach tab in the dashboard.
 */

import { AgentRun, CostlyPrompt, WasteFinding, EXPENSIVE_MODELS } from "./models";
import { ContextAudit } from "./context-audit";
import { AgentCostBreakdown } from "./dashboard";
import { scoreToGrade, contextWindowForModel } from "./quality";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface CoachPattern {
  name: string;
  detail: string;
  severity?: "high" | "medium" | "low"; // only for bad patterns
  fix?: string;
  savings?: string;
  earned?: boolean; // only for good patterns
}

export interface CoachData {
  health_score: number;
  grade: string;
  patterns_good: CoachPattern[];
  patterns_bad: CoachPattern[];
  costly_prompts: CostlyPrompt[];
  agent_costs: AgentCostBreakdown[];
  snapshot: {
    total_overhead: number;
    context_window: number;
    overhead_pct: number;
    usable_tokens: number;
    skill_count: number;
    mcp_server_count: number;
  };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Determine the dominant model's context window from session data. */
function dominantContextWindow(sessions: AgentRun[]): number {
  if (sessions.length === 0) return 200_000;
  const counts = new Map<string, number>();
  for (const r of sessions) {
    counts.set(r.model, (counts.get(r.model) ?? 0) + 1);
  }
  let best = sessions[0].model;
  let max = 0;
  for (const [model, count] of counts) {
    if (count > max) {
      max = count;
      best = model;
    }
  }
  return contextWindowForModel(best);
}

// ---------------------------------------------------------------------------
// Main generator
// ---------------------------------------------------------------------------

/**
 * Generate structured coach data for the dashboard Coach tab.
 *
 * Scoring logic:
 * - Base score: 75
 * - Earned bonuses (+5 each):
 *   - Lean SOUL.md (under 2% of context window)
 *   - Usage tracking active (sessions exist)
 * - Penalties:
 *   - Unused skills >80% ratio AND count >20: -7 (high severity)
 *   - Unused skills >60% ratio AND count >15: -3 (low severity)
 *   - Oversized SOUL.md (>3% of context): -5
 *   - Too many MCP servers (>20 AND >3% of context): -5
 *   - Poor model routing (>85% expensive model): -5
 * - Clamped to 0-100, graded via scoreToGrade()
 */
export function generateCoachData(
  audit: ContextAudit,
  sessions: AgentRun[],
  costlyPrompts: CostlyPrompt[],
  agentCosts: AgentCostBreakdown[],
  unusedSkillFindings: WasteFinding[]
): CoachData {
  const ctxWindow = sessions.length > 0
    ? dominantContextWindow(sessions)
    : 200_000;

  const totalOverhead = audit.totalOverhead;
  const overheadPct = ctxWindow > 0 ? (totalOverhead / ctxWindow) * 100 : 0;
  const usableTokens = Math.max(0, ctxWindow - totalOverhead);
  const activeSkills = audit.skills.filter((s) => !s.isArchived);
  const activeMcp = audit.mcpServers.filter((s) => !s.isDisabled);

  // -------------------------------------------------------------------------
  // Scoring
  // -------------------------------------------------------------------------

  let score = 75;
  const patternsGood: CoachPattern[] = [];
  const patternsBad: CoachPattern[] = [];

  // --- SOUL.md analysis ---
  const soulComponent = audit.components.find(
    (c) => c.name === "SOUL.md"
  );
  const soulTokens = soulComponent?.tokens ?? 0;
  const soulPct = ctxWindow > 0 ? (soulTokens / ctxWindow) * 100 : 0;

  if (soulTokens > 0 && soulPct < 2) {
    score += 5;
    patternsGood.push({
      name: "Lean SOUL.md",
      detail: `SOUL.md is ${soulTokens.toLocaleString()} tokens (${soulPct.toFixed(1)}% of context). Well under the 2% threshold.`,
      earned: true,
    });
  } else if (soulPct > 3) {
    score -= 5;
    patternsBad.push({
      name: "Oversized SOUL.md",
      detail: `SOUL.md is ${soulTokens.toLocaleString()} tokens (${soulPct.toFixed(1)}% of context). Exceeds the 3% threshold.`,
      severity: "medium",
      fix: "Move verbose instructions to skills. Keep SOUL.md under 2% of your context window.",
      savings: `~${Math.round(soulTokens * 0.4).toLocaleString()} tokens per message if trimmed to 2%`,
    });
  }

  // --- Usage tracking ---
  if (sessions.length > 0) {
    score += 5;
    patternsGood.push({
      name: "Usage tracking active",
      detail: `${sessions.length} session(s) recorded. Token usage data is being collected.`,
      earned: true,
    });
  }

  // --- Unused skills ---
  // unusedSkillFindings come from detectUnusedSkills(), which already has
  // severity computed based on ratio thresholds.
  if (unusedSkillFindings.length > 0) {
    const finding = unusedSkillFindings[0];
    const unusedCount = (finding.evidence?.unusedCount as number) ?? 0;
    const ratio = ((finding.evidence?.unusedRatioPct as number) ?? 0) / 100;

    if (ratio > 0.8 && unusedCount > 20) {
      score -= 7;
      patternsBad.push({
        name: "High unused skill ratio",
        detail: `${unusedCount} of ${activeSkills.length} installed skills have never been invoked (${(ratio * 100).toFixed(0)}%).`,
        severity: "high",
        fix: "Archive or remove unused skills to reclaim context. Run `npx token-optimizer context` to identify them.",
        savings: `~${(unusedCount * 100).toLocaleString()} tokens per message`,
      });
    } else if (ratio > 0.6 && unusedCount > 15) {
      score -= 3;
      patternsBad.push({
        name: "Moderate unused skill ratio",
        detail: `${unusedCount} of ${activeSkills.length} installed skills are unused (${(ratio * 100).toFixed(0)}%).`,
        severity: "low",
        fix: "Consider archiving skills you no longer use.",
        savings: `~${(unusedCount * 100).toLocaleString()} tokens per message`,
      });
    }
  }

  // --- MCP server overhead ---
  const mcpTokenEstimate = activeMcp.length * 150; // ~150 tokens per MCP server for tool definitions
  const mcpPct = ctxWindow > 0 ? (mcpTokenEstimate / ctxWindow) * 100 : 0;

  if (activeMcp.length > 20 && mcpPct > 3) {
    score -= 5;
    patternsBad.push({
      name: "Too many MCP servers",
      detail: `${activeMcp.length} active MCP servers consuming ~${mcpTokenEstimate.toLocaleString()} tokens (${mcpPct.toFixed(1)}% of context).`,
      severity: "medium",
      fix: "Disable MCP servers you rarely use. Each server adds tool definitions to every API call.",
      savings: `~${mcpTokenEstimate.toLocaleString()} tokens per message`,
    });
  }

  // --- Model routing ---
  if (sessions.length >= 5) {
    const expensiveCount = sessions.filter(
      (r) => EXPENSIVE_MODELS.has(r.model)
    ).length;
    const expensivePct = (expensiveCount / sessions.length) * 100;

    if (expensivePct > 85) {
      score -= 5;
      patternsBad.push({
        name: "Poor model routing",
        detail: `${expensivePct.toFixed(0)}% of sessions use expensive models (opus/sonnet/gpt-5). Consider routing routine tasks to lighter models.`,
        severity: "medium",
        fix: "Route heartbeat, cron, and simple lookup tasks to Haiku or flash-tier models.",
        savings: "Up to 60x cost reduction per routed task",
      });
    }
  }

  // --- Neutral facts (no score impact) ---
  patternsGood.push({
    name: "Skill count",
    detail: `${activeSkills.length} active skill(s) installed.`,
  });

  patternsGood.push({
    name: "MCP servers",
    detail: `${activeMcp.length} active MCP server(s) configured.`,
  });

  if (sessions.length >= 5) {
    const models = new Set(sessions.map((r) => r.model));
    patternsGood.push({
      name: "Model usage",
      detail: `Using ${models.size} model(s): ${Array.from(models).join(", ")}.`,
    });
  }

  // Clamp
  score = Math.round(Math.min(100, Math.max(0, score)));
  const grade = scoreToGrade(score);

  return {
    health_score: score,
    grade,
    patterns_good: patternsGood,
    patterns_bad: patternsBad,
    costly_prompts: costlyPrompts,
    agent_costs: agentCosts,
    snapshot: {
      total_overhead: totalOverhead,
      context_window: ctxWindow,
      overhead_pct: Math.round(overheadPct * 10) / 10,
      usable_tokens: usableTokens,
      skill_count: activeSkills.length,
      mcp_server_count: activeMcp.length,
    },
  };
}
