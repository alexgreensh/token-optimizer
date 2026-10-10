/**
 * Plain opus/sonnet 4.6 ids may be the 1M variant (the runtime never records
 * the `[1m]` suffix). Tokens above the 200K table window prove it.
 */
import { test, expect } from "bun:test";
import { contextWindowForModel, promoteWindowForObservedTokens } from "./quality";

test("table still says plain 4.6 is 200K", () => {
  expect(contextWindowForModel("claude-opus-4-6")).toBe(200_000);
  expect(contextWindowForModel("claude-sonnet-4-6[1m]")).toBe(1_000_000);
});

test("tokens above 200K promote a plain 4.6 id to 1M", () => {
  for (const m of ["claude-opus-4-6", "claude-sonnet-4-6", "anthropic/claude-sonnet-4-6", "claude-opus-4-6-20260301"]) {
    expect(promoteWindowForObservedTokens(m, contextWindowForModel(m), 250_000)).toBe(1_000_000);
  }
});

test("tokens that fit leave the 200K window alone", () => {
  expect(promoteWindowForObservedTokens("claude-opus-4-6", 200_000, 120_000)).toBe(200_000);
  expect(promoteWindowForObservedTokens("claude-opus-4-6", 200_000, 200_000)).toBe(200_000);
});

test("models that cannot be 1M are never promoted", () => {
  expect(promoteWindowForObservedTokens("claude-opus-4-5", 200_000, 250_000)).toBe(200_000);
  expect(promoteWindowForObservedTokens("claude-sonnet-4-5", 200_000, 250_000)).toBe(200_000);
  expect(promoteWindowForObservedTokens("gpt-5", 200_000, 250_000)).toBe(200_000);
});
