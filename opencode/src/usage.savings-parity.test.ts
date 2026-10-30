import { test, expect } from "bun:test";
import { aggregateModelUsage } from "./usage.js";

test("message replacements count once and retain model classes", () => {
  const messages = new Map();
  messages.set("first", { model: "sonnet", input: 10, output: 1, cacheRead: 2, cacheWrite: 3, cost: 0 });
  messages.set("first", { model: "sonnet", input: 10, output: 8, cacheRead: 2, cacheWrite: 3, cost: 0 });
  messages.set("second", { model: "opus-5-5", input: 20, output: 5, cacheRead: 4, cacheWrite: 6, cost: 0 });
  const buckets = aggregateModelUsage(messages.values());
  expect(buckets.sonnet.output).toBe(8);
  expect(buckets["opus-5-5"].input).toBe(20);
});

test("unknown explicit Claude generations have no fallback price", async () => {
  const { knownModelRate } = await import("./pricing.js");
  expect(knownModelRate("claude-opus-5-99", "input")).toBeNull();
  expect(knownModelRate("anthropic/claude-opus-5-99", "output")).toBeNull();
  expect(knownModelRate("opus", "input")).toBeGreaterThan(0);
  expect(knownModelRate("claude-opus-5-5", "input")).toBeGreaterThan(0);
});

test("fresh-session estimate leaves unknown Claude generations unpriced", async () => {
  const { modelInputRatePer1M } = await import("./nudges/fresh-session-nudge.js");
  const { knownModelRate } = await import("./pricing.js");
  expect(modelInputRatePer1M("claude-opus-5-99")).toBe(0);
  expect(knownModelRate("claude-3-5-sonnet-20241022", "input")).toBeGreaterThan(0);
});
