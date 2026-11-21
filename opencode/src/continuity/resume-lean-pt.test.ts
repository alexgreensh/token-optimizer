/**
 * Portuguese resume cues (OpenCode port of Python _PT_RESUME_CUE_RE).
 * Same case set as the OpenClaw port and tests/test_relevance_scorer.py.
 */
import { test, expect } from "bun:test";
import { resumeIntent, resumeTopicScore } from "./resume-lean.js";

const POSITIVES = [
  "continua de onde paramos", "Continuar de onde paramos", "na última sessão",
  "na ultima sessao", "sessão anterior", "da última vez", "da ultima vez",
  "onde paramos?", "onde parei", "continua o trabalho", "continuar com o parser",
  "continua daquilo", "continuar trabalhando no parser", "retoma o parser",
  "retomar de onde parei", "retomando o parser", "o que estávamos fazendo",
  "no que estavamos trabalhando", "ontem a gente mexeu nisso", "a gente estava no parser",
  // Pinned intended semantics, matching the English production regex: "continue with"
  // fires, so "continua com" fires even in ongoing-status sentences; past-work
  // recollection ("we were working on") fires, so "a gente estava ..." fires; and
  // dropping bare "a" does not lose "continua a última sessão" (matches via "última sessão").
  "continua com o erro no parser", "a gente estava no meio do refactor",
  "continua a última sessão",
];

const NEGATIVES = [
  "continua", "continuar", "continua!", "escreva uma função de ordenação",
  "adicione testes para o parser", "a sessão expirou",
  "o build continua a falhar", "continua a chover", "ele continua a trabalhar",
  "continua a implementar o parser",
  // Boundary parity probe: Python \w is Unicode-aware, so an accented letter glued
  // to the cue must block the match here too (\p{L} boundaries with the u flag).
  "éúltima sessão",
];

test("pt resume cues are detected", () => {
  for (const prompt of POSITIVES) {
    expect(resumeIntent(prompt)).toBe(true);
  }
});

test("pt non-resume prompts are not detected", () => {
  for (const prompt of NEGATIVES) {
    expect(resumeIntent(prompt)).toBe(false);
  }
});

test("pt cues are stripped from the topic residual", () => {
  expect(resumeTopicScore("continua de onde paramos", "parser module notes")).toBe(0.0);
  expect(resumeTopicScore("continua de onde paramos no parser", "parser module notes")).toBe(1.0);
});
