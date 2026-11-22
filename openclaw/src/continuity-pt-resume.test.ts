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
  "onde paramos?", "onde parei", "continua o trabalho", "continuar com o projeto",
  "continua daquilo", "continuar trabalhando no parser", "retoma o parser",
  "retome o trabalho", "retomar de onde parei", "retomando o parser",
  "o que estávamos fazendo", "no que estavamos trabalhando",
  "ontem a gente mexeu nisso", "a gente estava no parser",
  "a gente estava no meio do refactor", "continua a última sessão",
  // Article/preposition + work noun is a real resume ask, mirroring English
  // "resume the (work|session|project|task|conversation|thread|discussion)":
  "continua a sessão", "continuar a tarefa", "continua a conversa sobre o parser",
  "continua a discussão", "continua com a tarefa", "retoma a tarefa",
];

const NEGATIVES = [
  "continua", "continuar", "continua!", "escreva uma função de ordenação",
  "adicione testes para o parser", "a sessão expirou",
  "o build continua a falhar", "continua a chover", "ele continua a trabalhar",
  "continua a implementar o parser", "continua a trabalhar no parser",
  // Ordinary status reports and non-work recollections must not trigger resume:
  "continua com o erro no parser", "continua o mesmo erro",
  "o build continua com erro", "o teste continua de pé",
  "a gente estava errado sobre o cache",
  // Boundary parity probe: Python \w is Unicode-aware, so an accented letter glued
  // to the cue must block the match here too (\p{L} boundaries with the u flag).
  "éúltima sessão",
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
