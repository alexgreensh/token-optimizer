/**
 * Portuguese resume cues (OpenClaw port of Python _PT_RESUME_CUE_RE).
 *
 * Mirrors tests/test_relevance_scorer.py PT cases: detection, the bare-"a"
 * false-positive guard ("continua a <infinitive>" = "keeps <verb>ing"), and
 * residual stripping so cue words don't count as topic tokens.
 */
import { test, expect } from "bun:test";
import { isResumeIntent, resumeTopicScore } from "./continuity.js";

const POSITIVES = [
  "continua de onde paramos", "Continuar de onde paramos", "na última sessão",
  "na ultima sessao", "sessão anterior", "da última vez", "da ultima vez",
  "onde paramos?", "onde parei", "continua o trabalho", "continuar com o parser",
  "continua daquilo", "continuar trabalhando no parser", "retoma o parser",
  "retomar de onde parei", "retomando o parser", "o que estávamos fazendo",
  "no que estavamos trabalhando", "ontem a gente mexeu nisso", "a gente estava no parser",
];

const NEGATIVES = [
  "continua", "continuar", "continua!", "escreva uma função de ordenação",
  "adicione testes para o parser", "a sessão expirou",
  "o build continua a falhar", "continua a chover", "ele continua a trabalhar",
];

test("pt resume cues are detected", () => {
  for (const prompt of POSITIVES) {
    expect(isResumeIntent(prompt)).toBe(true);
  }
});

test("pt non-resume prompts are not detected", () => {
  for (const prompt of NEGATIVES) {
    expect(isResumeIntent(prompt)).toBe(false);
  }
});

test("pt cues are stripped from the topic residual", () => {
  expect(resumeTopicScore("continua de onde paramos", "parser module notes")).toBe(0.0);
  expect(resumeTopicScore("continua de onde paramos no parser", "parser module notes")).toBe(1.0);
});
