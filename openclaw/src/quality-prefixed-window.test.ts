/**
 * Provider-prefixed Claude ids (gateways, Bedrock inference profiles like
 * "us.anthropic.claude-haiku-4-5") resolve to the same window as the bare id.
 */
import { test, expect } from "bun:test";
import { claudeContextWindow, contextWindowForModel } from "./quality";

const cases: Array<[string, number]> = [
  ["anthropic/claude-haiku-4-5", 200_000],
  ["bedrock/claude-sonnet-4-5", 200_000],
  ["anthropic/claude-sonnet-4-6", 200_000],
  ["us.anthropic.claude-haiku-4-5", 200_000],
  ["eu.anthropic.claude-opus-4-7", 1_000_000],
  ["global.anthropic.claude-sonnet-5", 1_000_000],
  ["anthropic.claude-haiku-4-5", 200_000],
  ["bedrock:claude-haiku-4-5", 200_000],
  ["openrouter/anthropic/claude-sonnet-4-6[1m]", 1_000_000],
];

test("provider-prefixed ids resolve like their bare form", () => {
  for (const [model, expected] of cases) {
    expect(contextWindowForModel(model)).toBe(expected);
  }
});

test("a family word inside an unstripped prefix is not the family", () => {
  expect(claudeContextWindow("us.sonnet.claude-haiku-4-5")).toBe(
    claudeContextWindow("sonnet.claude-haiku-4-5"),
  );
});
