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
