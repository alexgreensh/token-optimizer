const MODEL_CONTEXT_WINDOWS: Record<string, number> = {
  // Anthropic ids resolve through claudeContextWindow() below, not this table.

  // OpenAI GPT-5 family
  "gpt-5.6": 1_050_000,
  "gpt-5.6-sol": 1_050_000,
  "gpt-5.6-terra": 1_050_000,
  "gpt-5.6-luna": 1_050_000,
  "gpt-5.5-pro": 1_000_000,
  "gpt-5.5": 1_000_000,
  "gpt-5.4": 1_000_000,
  "gpt-5.4-mini": 400_000,
  "gpt-5.4-nano": 400_000,
  "gpt-5.3-codex": 400_000,
  "gpt-5.2-codex": 400_000,
  "gpt-5.2": 400_000,
  "gpt-5.1-codex-mini": 400_000,
  "gpt-5.1-codex": 400_000,
  "gpt-5.1": 400_000,
  "gpt-5-codex": 400_000,
  "gpt-5": 400_000,
  "gpt-5-mini": 400_000,
  "gpt-5-nano": 400_000,
  // OpenAI GPT-4 family
  "gpt-4.1": 1_000_000,
  "gpt-4.1-mini": 1_000_000,
  "gpt-4.1-nano": 1_000_000,
  "gpt-4o": 128_000,
  "gpt-4o-mini": 128_000,
  // OpenAI reasoning
  o3: 200_000,
  "o3-mini": 200_000,
  "o3-pro": 200_000,
  "o4-mini": 200_000,

  // Google Gemini
  "gemini-3.5-flash": 1_000_000,
  "gemini-3.1-pro-preview": 2_000_000,
  "gemini-3.1-flash-lite": 1_000_000,
  "gemini-3-pro": 1_000_000,
  "gemini-3-flash": 1_000_000,
  "gemini-3.1-pro": 1_000_000,
  "gemini-2.5-pro": 2_000_000,
  "gemini-2.5-flash": 1_000_000,
  "gemini-2.5-flash-lite": 1_000_000,
  "gemini-2.0-flash": 1_000_000,
  "gemini-2.0-flash-lite": 1_000_000,

  // DeepSeek
  "deepseek-v3": 128_000,
  "deepseek-r1": 128_000,

  // Qwen
  qwen3: 128_000,
  "qwen3-mini": 128_000,
  "qwen-coder": 128_000,

  // Mistral
  "mistral-large": 262_000,
  "mistral-small": 128_000,

  // xAI
  "grok-4": 131_000,

  // Other
  "kimi-k2.5": 128_000,
  "minimax-2": 128_000,
  "glm-4.7": 128_000,
  "glm-4.7-flash": 128_000,
  "mimo-flash": 128_000,
  local: 128_000,
};

const DEFAULT_CONTEXT_WINDOW = 200_000;

/**
 * Context window for a Claude model id, per Claude Code's model-config docs
 * (checked 2026-10-10): Fable, Sonnet 5+, Opus 4.7+ and Haiku 5.5 are 1M with
 * no suffix; Sonnet 4.6 / Opus 4.6 are 1M only as the `[1m]` variant (200K
 * without); every older Claude is 200K. Returns null when the id carries no
 * Claude family so callers fall through to their other rules.
 */
export function claudeContextWindow(model: string): number | null {
  const raw = (model ?? "").toLowerCase().trim();
  const oneM = raw.includes("[1m]") || raw.includes("1000k");
  const id = raw
    .replace("[1m]", "")
    .replace(/^.*\//, "")
    .replace(/[-@]\d{8}$/, "");
  if (/claude[-_]?[0-3]\b/.test(id) || /claude-\d(?:[-.]\d)?-(opus|sonnet|haiku)/.test(id)) return 200_000;
  const m = /(fable|mythos|opus|sonnet|haiku)(?:[-_.](\d+))?(?:[-_.](\d{1,2}))?(?!\d)/.exec(id);
  if (!m) return null;
  const [, family, majorRaw, minorRaw] = m;
  if (family === "fable" || family === "mythos") return 1_000_000;
  const major = majorRaw === undefined ? null : parseInt(majorRaw, 10);
  const minor = minorRaw === undefined ? 0 : parseInt(minorRaw, 10);
  if (family === "haiku") {
    if (major === null) return 200_000; // bare alias: conservative
    return major > 5 || (major === 5 && minor >= 5) ? 1_000_000 : 200_000;
  }
  // opus / sonnet: a bare alias resolves to the current 5.x line (1M).
  if (major === null || major >= 5) return 1_000_000;
  if (major === 4 && family === "opus" && minor >= 7) return 1_000_000;
  if (major === 4 && minor === 6) return oneM ? 1_000_000 : 200_000;
  return 200_000;
}

export function contextWindowForModel(model: string): number {
  if (!model) return DEFAULT_CONTEXT_WINDOW;

  const lower = model.toLowerCase();

  const direct = MODEL_CONTEXT_WINDOWS[lower];
  if (direct !== undefined) return direct;

  // Claude (incl. legacy 2.x/3.x, which are genuinely 200K): one table-driven rule.
  if (/claude|fable|mythos|opus|sonnet|haiku/.test(lower)) {
    const claude = claudeContextWindow(lower);
    if (claude !== null) return claude;
  }

  for (const [key, value] of Object.entries(MODEL_CONTEXT_WINDOWS)) {
    if (lower.includes(key)) return value;
  }

  return DEFAULT_CONTEXT_WINDOW;
}
