import { expect, test } from "bun:test";
import { claudePricingKey, DEFAULT_PRICING, normalizeModelName, price, price_cw } from "./pricing.js";
import { GENERATED_PRICING } from "./prices.generated.js";
import { modelInputRatePer1M } from "./nudges/fresh-session-nudge.js";
import { contextWindowForModel } from "./util/context-window.js";

// Expected rates come from the auto-refreshed table so a real price change never
// breaks this test; it checks the engine applies those rates correctly.
const MODELS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"];

test("GPT-5.6 aliases and Sol Pro display names normalize to canonical pricing keys", () => {
  expect(normalizeModelName("gpt-5.6")).toBe("gpt-5.6-sol");
  expect(normalizeModelName("openrouter/openai/gpt-5.6-sol-2026-07-09")).toBe("gpt-5.6-sol");
  expect(normalizeModelName("GPT-5.6 Sol Pro")).toBe("gpt-5.6-sol");
  expect(normalizeModelName("openai:gpt-5.6-terra-2026-07-09")).toBe("gpt-5.6-terra");
  expect(normalizeModelName("gpt-5.6_luna")).toBe("gpt-5.6-luna");
  expect(normalizeModelName("gpt-5.5-pro")).toBe("gpt-5.5-pro");
});

test("GPT-5.6 base pricing covers fresh, cached, cache-write, and output tokens", () => {
  for (const model of MODELS) {
    const r = GENERATED_PRICING[model];
    expect(r).toBeDefined();
    expect(DEFAULT_PRICING[model]).toEqual(r);
    expect(price(1_000_000, 1_000_000, 1_000_000, { [model]: 1 })).toBeCloseTo((r.input + r.cacheRead + r.output) * 1e6);
    expect(price_cw(1_000_000, { [model]: 1 })).toBeCloseTo(r.cacheWrite * 1e6);
  }
});

test("GPT-5.6 pricing feeds hook savings and context-window helpers", () => {
  expect(modelInputRatePer1M("GPT-5.6 Sol Pro")).toBeCloseTo(GENERATED_PRICING["gpt-5.6-sol"].input * 1e6);
  expect(modelInputRatePer1M("openai:gpt-5.6-terra-2026-07-09")).toBeCloseTo(GENERATED_PRICING["gpt-5.6-terra"].input * 1e6);
  expect(modelInputRatePer1M("gpt-5.6_luna")).toBeCloseTo(GENERATED_PRICING["gpt-5.6-luna"].input * 1e6);
  expect(contextWindowForModel("gpt-5.6")).toBe(1_050_000);
  expect(contextWindowForModel("openrouter/openai/gpt-5.6-terra-2026-07-09")).toBe(1_050_000);
});

test("Claude 3-era ids price as their own model, not the current family card", () => {
  expect(claudePricingKey("claude-3-5-sonnet-20241022")).toBe("sonnet-legacy");
  expect(claudePricingKey("claude-3-opus-20240229")).toBe("opus-3");
  expect(claudePricingKey(123 as unknown as string)).toBeNull();
});

const CLAUDE_WINDOWS: Array<[string, number]> = [
  ["claude-haiku-5-5", 1_000_000],
  ["anthropic/claude-haiku-5-5", 1_000_000],
  ["claude-haiku-4-5", 200_000],
  ["claude-haiku-4-5-20251001", 200_000],
  ["claude-sonnet-4-6", 200_000],
  ["claude-sonnet-4-6[1m]", 1_000_000],
  ["claude-opus-4-6", 200_000],
  ["claude-opus-4-6[1m]", 1_000_000],
  ["claude-opus-4-7", 1_000_000],
  ["claude-opus-4-8", 1_000_000],
  ["claude-sonnet-5", 1_000_000],
  ["claude-sonnet-5-5", 1_000_000],
  ["claude-opus-5-5", 1_000_000],
  ["claude-fable-5-1", 1_000_000],
  ["claude-3-5-sonnet-20241022", 200_000],
  ["claude-3-haiku-20240307", 200_000],
  ["claude-opus-4-1-20250805", 200_000],
];

test("Claude context windows follow the documented per-model table", () => {
  for (const [model, window] of CLAUDE_WINDOWS) {
    expect([model, contextWindowForModel(model)]).toEqual([model, window]);
  }
});
