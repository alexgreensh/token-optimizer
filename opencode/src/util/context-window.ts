const MODEL_CONTEXT_WINDOWS: Record<string, number> = {
  // Anthropic (Opus/Sonnet 1M GA since March 13, 2026)
  opus: 1_000_000,
  sonnet: 1_000_000,
  haiku: 200_000,
  "claude-opus-4-7": 1_000_000,
  "claude-opus-4-6": 1_000_000,
  "claude-sonnet-4-6": 1_000_000,
  "claude-haiku-4-5": 200_000,

  // OpenAI GPT-5 family
  "gpt-5.5": 1_100_000,
  "gpt-5.4": 1_100_000,
  "gpt-5.2": 400_000,
  "gpt-5.1": 400_000,
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
  "gemini-3-pro": 1_000_000,
  "gemini-3-flash": 1_000_000,
  "gemini-3.1-pro": 1_000_000,
  "gemini-2.5-pro": 2_000_000,
  "gemini-2.5-flash": 1_000_000,
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

export function contextWindowForModel(model: string): number {
  if (!model) return DEFAULT_CONTEXT_WINDOW;

  const lower = model.toLowerCase();

  const direct = MODEL_CONTEXT_WINDOWS[lower];
  if (direct !== undefined) return direct;

  for (const [key, value] of Object.entries(MODEL_CONTEXT_WINDOWS)) {
    if (lower.includes(key)) return value;
  }

  return DEFAULT_CONTEXT_WINDOW;
}
