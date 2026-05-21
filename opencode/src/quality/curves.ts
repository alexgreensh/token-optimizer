type CurvePoint = [number, number];

// Anthropic default (fill fraction 0-1)
const ANTHROPIC_MRCR: CurvePoint[] = [
  [0.0, 98],
  [0.10, 96],
  [0.25, 93],
  [0.50, 88],
  [0.60, 84],
  [0.70, 80],
  [0.80, 78],
  [0.90, 77],
  [1.0, 76],
];

// OpenAI GPT-5.5 (absolute tokens)
const OPENAI_GPT55_MRCR: CurvePoint[] = [
  [0, 98],
  [8_000, 98],
  [16_000, 96],
  [32_000, 94],
  [64_000, 90],
  [128_000, 86],
  [256_000, 84],
  [512_000, 81],
  [1_000_000, 74],
];

// OpenAI GPT-5 family fallback (absolute tokens)
const OPENAI_GPT5_MRCR: CurvePoint[] = [
  [0, 98],
  [32_000, 94],
  [64_000, 90],
  [128_000, 85],
  [256_000, 80],
  [512_000, 72],
  [1_000_000, 64],
];

// GPT-4.1 family (1M window, extrapolated from GPT-5 shape)
const OPENAI_GPT41_MRCR: CurvePoint[] = [
  [0, 98],
  [32_000, 95],
  [64_000, 92],
  [128_000, 88],
  [256_000, 82],
  [512_000, 74],
  [1_000_000, 66],
];

// Gemini (steepest degradation among frontier models)
const GEMINI_MRCR: CurvePoint[] = [
  [0, 98],
  [8_000, 97],
  [32_000, 95],
  [64_000, 92],
  [128_000, 85],
  [256_000, 72],
  [512_000, 50],
  [1_000_000, 26],
  [2_000_000, 15],
];

function interpolate(curve: CurvePoint[], x: number): number {
  if (curve.length === 0) return 76;
  if (x <= curve[0][0]) return curve[0][1];
  if (x >= curve[curve.length - 1][0]) return curve[curve.length - 1][1];

  for (let i = 1; i < curve.length; i++) {
    if (x <= curve[i][0]) {
      const [x0, y0] = curve[i - 1];
      const [x1, y1] = curve[i];
      const t = (x - x0) / (x1 - x0);
      return y0 + t * (y1 - y0);
    }
  }
  return curve[curve.length - 1][1];
}

type CurveInfo = {
  family: string;
  curve: CurvePoint[];
  mode: "fill_fraction" | "absolute_tokens";
};

function selectCurve(model: string | undefined): CurveInfo {
  const m = (model ?? "").toLowerCase();
  if (m.includes("gemini")) {
    return { family: "google-gemini", curve: GEMINI_MRCR, mode: "absolute_tokens" };
  }
  if (m.includes("gpt-5.5") || m.includes("gpt-5.4")) {
    return { family: "openai-gpt-5.5", curve: OPENAI_GPT55_MRCR, mode: "absolute_tokens" };
  }
  if (m.includes("gpt-4.1")) {
    return { family: "openai-gpt-4.1", curve: OPENAI_GPT41_MRCR, mode: "absolute_tokens" };
  }
  if (m.includes("gpt-5") || m.includes("gpt-4")) {
    return { family: "openai-gpt-5", curve: OPENAI_GPT5_MRCR, mode: "absolute_tokens" };
  }
  return { family: "anthropic-default", curve: ANTHROPIC_MRCR, mode: "fill_fraction" };
}

export function estimateQualityFromFill(
  fillPct: number,
  model?: string,
  contextWindow?: number,
): { quality: number; curveName: string } {
  const fill = Math.max(0, Math.min(1, fillPct));
  const { family, curve, mode } = selectCurve(model);

  let quality: number;
  if (mode === "absolute_tokens" && contextWindow) {
    quality = interpolate(curve, fill * contextWindow);
  } else {
    quality = interpolate(curve, fill);
  }

  return { quality: Math.round(quality), curveName: family };
}
