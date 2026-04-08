/**
 * Context Optimization Audit for OpenClaw.
 *
 * Scans all system prompt components and reports per-component token overhead.
 * Includes individual skill breakdown, MCP server scanning, and manage data.
 */

import * as fs from "fs";
import * as path from "path";
import { AgentRun } from "./models";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ContextComponent {
  name: string;
  path: string;
  tokens: number;
  category: "config" | "personality" | "memory" | "skills" | "tools" | "agents" | "system";
  isOptimizable: boolean;
}

export interface SkillDetail {
  name: string;
  path: string;
  tokens: number;         // Startup overhead (~100 tok description loaded per message)
  fullFileTokens: number; // Full SKILL.md token count (loaded only when invoked)
  description: string;
  isArchived: boolean;
}

export interface McpServer {
  name: string;
  command: string;
  toolCount: number;
  isDisabled: boolean;
}

export interface ManageData {
  skills: { active: SkillDetail[]; archived: SkillDetail[] };
  mcpServers: { active: McpServer[]; disabled: McpServer[] };
}

export interface ContextAudit {
  totalOverhead: number;
  components: ContextComponent[];
  skills: SkillDetail[];
  mcpServers: McpServer[];
  recommendations: string[];
  manage: ManageData;
}

// ---------------------------------------------------------------------------
// Token estimation
// ---------------------------------------------------------------------------

function estimateTokens(text: string): number {
  if (!text) return 0;
  return Math.ceil(text.length / 3.8);
}

function readFileTokens(filePath: string): number {
  try {
    if (!fs.existsSync(filePath)) return 0;
    const stat = fs.statSync(filePath);
    if (!stat.isFile() || stat.size > 1_000_000) return 0;
    return estimateTokens(fs.readFileSync(filePath, "utf-8"));
  } catch {
    return 0;
  }
}

function readFileContent(filePath: string): string {
  try {
    if (!fs.existsSync(filePath)) return "";
    const stat = fs.statSync(filePath);
    if (!stat.isFile() || stat.size > 1_000_000) return "";
    return fs.readFileSync(filePath, "utf-8");
  } catch {
    return "";
  }
}

// ---------------------------------------------------------------------------
// Component scanners
// ---------------------------------------------------------------------------

function scanSingleFile(
  openclawDir: string,
  filename: string,
  category: ContextComponent["category"],
  optimizable: boolean
): ContextComponent | null {
  const filePath = path.join(openclawDir, filename);
  const tokens = readFileTokens(filePath);
  if (tokens === 0) return null;
  return { name: filename, path: filePath, tokens, category, isOptimizable: optimizable };
}

/** Extract first line after # heading or first non-empty line as description. */
function extractDescription(content: string): string {
  const lines = content.split("\n");
  let foundHeading = false;
  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed.startsWith("# ")) { foundHeading = true; continue; }
    if (foundHeading && trimmed.length > 0 && !trimmed.startsWith("#")) {
      return trimmed.slice(0, 200);
    }
  }
  // Fallback: first non-heading non-empty line
  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed.length > 0 && !trimmed.startsWith("#") && !trimmed.startsWith("---")) {
      return trimmed.slice(0, 200);
    }
  }
  return "";
}

function scanSkillsIndividual(openclawDir: string): SkillDetail[] {
  const results: SkillDetail[] = [];

  const skillsDirs = [
    path.join(openclawDir, "skills"),
    path.join(openclawDir, "..", "skills"),
  ];

  for (const dir of skillsDirs) {
    if (!fs.existsSync(dir)) continue;
    try {
      const entries = fs.readdirSync(dir, { withFileTypes: true });
      for (const entry of entries) {
        if (!entry.isDirectory()) continue;
        const skillFile = path.join(dir, entry.name, "SKILL.md");
        const content = readFileContent(skillFile);
        if (!content) continue;

        const desc = extractDescription(content);
        results.push({
          name: entry.name,
          path: skillFile,
          tokens: Math.min(estimateTokens(desc), 150) || 100, // ~100 tok description loaded per message
          fullFileTokens: estimateTokens(content),
          description: desc,
          isArchived: false,
        });
      }
    } catch {
      continue;
    }
  }

  // Check archived skills
  const archiveDir = path.join(openclawDir, "skills", "_archived");
  if (fs.existsSync(archiveDir)) {
    try {
      const entries = fs.readdirSync(archiveDir, { withFileTypes: true });
      for (const entry of entries) {
        if (!entry.isDirectory()) continue;
        const skillFile = path.join(archiveDir, entry.name, "SKILL.md");
        const content = readFileContent(skillFile);

        const desc = content ? extractDescription(content) : "";
        results.push({
          name: entry.name,
          path: skillFile,
          tokens: 0, // Archived = not loaded = zero overhead
          fullFileTokens: content ? estimateTokens(content) : 0,
          description: desc,
          isArchived: true,
        });
      }
    } catch {
      // skip
    }
  }

  results.sort((a, b) => b.tokens - a.tokens);
  return results;
}

function scanMcpServers(openclawDir: string): McpServer[] {
  const servers: McpServer[] = [];

  // Check config.json for MCP server definitions
  const configPath = path.join(openclawDir, "config.json");
  try {
    if (!fs.existsSync(configPath)) return [];
    const config = JSON.parse(fs.readFileSync(configPath, "utf-8"));

    // OpenClaw stores MCP servers in various locations
    const mcpConfigs = config.mcpServers ?? config.mcp_servers ?? config.mcp ?? {};

    if (typeof mcpConfigs === "object" && mcpConfigs !== null) {
      for (const [name, srv] of Object.entries(mcpConfigs)) {
        if (typeof srv !== "object" || srv === null) continue;
        const s = srv as Record<string, unknown>;
        servers.push({
          name,
          command: String(s.command ?? s.cmd ?? ""),
          toolCount: Array.isArray(s.tools) ? s.tools.length : 0,
          isDisabled: Boolean(s.disabled),
        });
      }
    }
  } catch {
    // skip
  }

  // Also check openclaw.json
  const openclawJson = path.join(openclawDir, "openclaw.json");
  try {
    if (fs.existsSync(openclawJson)) {
      const config = JSON.parse(fs.readFileSync(openclawJson, "utf-8"));
      const mcpConfigs = config.mcpServers ?? config.mcp_servers ?? {};

      if (typeof mcpConfigs === "object" && mcpConfigs !== null) {
        const existingNames = new Set(servers.map((s) => s.name));
        for (const [name, srv] of Object.entries(mcpConfigs)) {
          if (existingNames.has(name)) continue;
          if (typeof srv !== "object" || srv === null) continue;
          const s = srv as Record<string, unknown>;
          servers.push({
            name,
            command: String(s.command ?? s.cmd ?? ""),
            toolCount: Array.isArray(s.tools) ? s.tools.length : 0,
            isDisabled: Boolean(s.disabled),
          });
        }
      }
    }
  } catch {
    // skip
  }

  // Check TOOLS.md for additional tool definitions
  const toolsMd = path.join(openclawDir, "TOOLS.md");
  if (fs.existsSync(toolsMd) && servers.length === 0) {
    // If no MCP config found, try to parse tool definitions from TOOLS.md
    try {
      const content = fs.readFileSync(toolsMd, "utf-8");
      const serverMatches = content.match(/^#+\s+(.+)/gm);
      if (serverMatches) {
        for (const match of serverMatches) {
          const name = match.replace(/^#+\s+/, "").trim();
          if (name && !servers.some((s) => s.name === name)) {
            servers.push({
              name,
              command: "(defined in TOOLS.md)",
              toolCount: 0,
              isDisabled: false,
            });
          }
        }
      }
    } catch {
      // skip
    }
  }

  return servers;
}

function scanAgentConfigs(openclawDir: string): ContextComponent | null {
  const agentsDir = path.join(openclawDir, "agents");
  if (!fs.existsSync(agentsDir)) return null;

  let totalTokens = 0;
  let agentCount = 0;

  try {
    const entries = fs.readdirSync(agentsDir, { withFileTypes: true });
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      totalTokens += readFileTokens(path.join(agentsDir, entry.name, "config.json"));
      agentCount++;
    }
  } catch {
    return null;
  }

  if (agentCount === 0) return null;
  return {
    name: `Agent configs (${agentCount})`,
    path: agentsDir,
    tokens: totalTokens,
    category: "agents",
    isOptimizable: true,
  };
}

function scanCronConfigs(openclawDir: string): ContextComponent | null {
  const cronDir = path.join(openclawDir, "cron");
  if (!fs.existsSync(cronDir)) return null;

  let totalTokens = 0;
  let cronCount = 0;

  try {
    for (const file of fs.readdirSync(cronDir)) {
      if (!file.endsWith(".json") && !file.endsWith(".yaml")) continue;
      totalTokens += readFileTokens(path.join(cronDir, file));
      cronCount++;
    }
  } catch {
    return null;
  }

  if (cronCount === 0) return null;
  return {
    name: `Cron configs (${cronCount})`,
    path: cronDir,
    tokens: totalTokens,
    category: "config",
    isOptimizable: true,
  };
}

// ---------------------------------------------------------------------------
// Recommendations engine
// ---------------------------------------------------------------------------

function generateRecommendations(
  components: ContextComponent[],
  skills: SkillDetail[],
  mcpServers: McpServer[]
): string[] {
  const recs: string[] = [];

  const soul = components.find((c) => c.name === "SOUL.md");
  if (soul && soul.tokens > 2000) {
    recs.push(
      `SOUL.md is ${soul.tokens.toLocaleString()} tokens. Consider trimming to under 2,000 tokens. Move verbose instructions to skills.`
    );
  }

  const memory = components.find((c) => c.name === "MEMORY.md");
  if (memory && memory.tokens > 1500) {
    recs.push(
      `MEMORY.md is ${memory.tokens.toLocaleString()} tokens. Archive stale entries to reduce per-message overhead.`
    );
  }

  const tools = components.find((c) => c.name === "TOOLS.md");
  if (tools && tools.tokens > 5000) {
    recs.push(
      `Tool definitions consume ${tools.tokens.toLocaleString()} tokens. Consider deferred loading for rarely-used tools.`
    );
  }

  const activeSkills = skills.filter((s) => !s.isArchived);
  if (activeSkills.length > 20) {
    const totalSkillTokens = activeSkills.reduce((s, sk) => s + sk.tokens, 0);
    recs.push(
      `${activeSkills.length} skills loaded (${totalSkillTokens.toLocaleString()} tokens). Archive unused skills to reclaim context.`
    );
  }

  if (mcpServers.length > 10) {
    recs.push(
      `${mcpServers.length} MCP servers configured. Disable unused servers to reduce tool definition overhead.`
    );
  }

  const total = components.reduce((s, c) => s + c.tokens, 0);
  if (total > 30000) {
    recs.push(
      `Total context overhead is ${total.toLocaleString()} tokens per message (${((total / 200000) * 100).toFixed(1)}% of 200K window). Target under 25,000 tokens.`
    );
  }

  if (recs.length === 0) {
    recs.push("Context overhead looks healthy. No immediate optimizations needed.");
  }

  return recs;
}

// ---------------------------------------------------------------------------
// Skill usage history (for never-used skill detection)
// ---------------------------------------------------------------------------

/**
 * Scans tool calls across all provided AgentRun sessions for Skill invocations.
 * Returns a Map of skillName -> invocation count.
 *
 * OpenClaw records tool calls in AgentRun.toolsUsed as an array of tool name strings.
 * Skill invocations appear as the skill name directly (e.g. "token-optimizer") or
 * prefixed with "skill:" or "Skill:" depending on the OpenClaw version.
 * We normalize by stripping known prefixes and lowercasing before matching.
 */
export function getSkillUsageHistory(sessions: AgentRun[]): Map<string, number> {
  const usageMap = new Map<string, number>();

  for (const run of sessions) {
    for (const tool of run.toolsUsed) {
      // Normalize: strip "skill:" / "Skill:" prefix, lowercase for matching
      const normalized = tool
        .replace(/^[Ss]kill:/i, "")
        .trim()
        .toLowerCase();
      if (!normalized) continue;
      usageMap.set(normalized, (usageMap.get(normalized) ?? 0) + 1);
    }
  }

  return usageMap;
}

// ---------------------------------------------------------------------------
// Main audit function
// ---------------------------------------------------------------------------

export function auditContext(openclawDir: string): ContextAudit {
  const components: ContextComponent[] = [];

  // Core system prompt (estimated, not user-editable)
  components.push({
    name: "Core system prompt (est.)",
    path: "(built-in)",
    tokens: 15000,
    category: "system",
    isOptimizable: false,
  });

  // Scan individual files
  const fileScanners: Array<() => ContextComponent | null> = [
    () => scanSingleFile(openclawDir, "SOUL.md", "personality", true),
    () => scanSingleFile(openclawDir, "MEMORY.md", "memory", true),
    () => scanSingleFile(openclawDir, "AGENTS.md", "agents", true),
    () => scanSingleFile(openclawDir, "TOOLS.md", "tools", true),
    () => scanSingleFile(openclawDir, "config.json", "config", false),
    () => scanAgentConfigs(openclawDir),
    () => scanCronConfigs(openclawDir),
  ];

  for (const scanner of fileScanners) {
    const result = scanner();
    if (result) components.push(result);
  }

  // Scan individual skills
  const skills = scanSkillsIndividual(openclawDir);
  const activeSkills = skills.filter((s) => !s.isArchived);
  const archivedSkills = skills.filter((s) => s.isArchived);

  // Add skills as a single aggregated component for the overview bar
  if (activeSkills.length > 0) {
    const totalSkillTokens = activeSkills.reduce((s, sk) => s + sk.tokens, 0);
    components.push({
      name: `Skills (${activeSkills.length} active)`,
      path: "",
      tokens: totalSkillTokens,
      category: "skills",
      isOptimizable: true,
    });
  }

  // Scan MCP servers
  const mcpServers = scanMcpServers(openclawDir);
  const activeMcp = mcpServers.filter((s) => !s.isDisabled);
  const disabledMcp = mcpServers.filter((s) => s.isDisabled);

  // Sort components by tokens (highest first), system prompt stays on top
  components.sort((a, b) => {
    if (a.category === "system") return -1;
    if (b.category === "system") return 1;
    return b.tokens - a.tokens;
  });

  const totalOverhead = components.reduce((s, c) => s + c.tokens, 0);
  const recommendations = generateRecommendations(components, skills, mcpServers);

  const manage: ManageData = {
    skills: { active: activeSkills, archived: archivedSkills },
    mcpServers: { active: activeMcp, disabled: disabledMcp },
  };

  return { totalOverhead, components, skills, mcpServers, recommendations, manage };
}
