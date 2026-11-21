/**
 * Prompt-side function words (EN + PT) must not count as topic: overlap on
 * them alone scores 0.0, while genuine content overlap is unchanged.
 *
 * Ports the Python test_keyword_relevance_ignores_function_word_overlap
 * (tests/test_i18n_continuity.py) so the OpenClaw runtime pins the same
 * keywordRelevanceScore semantics as the Python continuity provider.
 */
import { test, expect } from "bun:test";
import { keywordRelevanceScore } from "./continuity.js";

const EN_CHECKPOINT =
  "Active task: fix the docker build cache for payments, we would check this later, " +
  "our pipeline is slow and that should change.";
const PT_CHECKPOINT =
  "Tarefa: corrigir o cache do docker, esse pipeline é lento e nossos " +
  "testes também falham, isso precisa mudar.";

test("keywordRelevanceScore ignores function-word overlap", () => {
  expect(keywordRelevanceScore("would this should your have", "unused-path", EN_CHECKPOINT)).toBe(0);
  expect(keywordRelevanceScore("would this benchmark spend our tokens", "unused-path", EN_CHECKPOINT)).toBe(0);
  expect(keywordRelevanceScore("esse isso nossos também para como", "unused-path", PT_CHECKPOINT)).toBe(0);
  expect(keywordRelevanceScore("esse benchmark gastaria nossos tokens", "unused-path", PT_CHECKPOINT)).toBe(0);
  expect(keywordRelevanceScore("fix the docker build cache", "unused-path", EN_CHECKPOINT)).toBe(1);
  expect(keywordRelevanceScore("corrigir o cache do docker", "unused-path", PT_CHECKPOINT)).toBe(1);
  expect(keywordRelevanceScore("continue", "unused-path", EN_CHECKPOINT)).toBe(1);
  // A resume cue followed only by function words is still a vague resume.
  expect(keywordRelevanceScore("continue where we left off with these", "unused-path", EN_CHECKPOINT)).toBe(1);
  expect(keywordRelevanceScore("continue isso aqui", "unused-path", PT_CHECKPOINT)).toBe(1);
  // "todo" stays a topic word (TODO lists), not the Portuguese determiner.
  expect(
    keywordRelevanceScore("update the todo", "unused-path", "Active task: update the todo file.")
  ).toBe(1);
});
