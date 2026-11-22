/**
 * Portuguese (pt-BR/pt-PT) resume cues. Mirrors _measure/pt_resume_cues.py:
 * - Bare "continua"/"continuar" does NOT match.
 * - "continua/continuar (o|a|com|de)" is restricted to work nouns or "de onde" /
 *   "daquilo" / "aquilo" / "trabalhando (no|na|nos|nas|em|nisso|naquilo)", so status
 *   reports ("continua o mesmo erro", "o build continua com erro", "o teste continua
 *   de pé", "continua a falhar", "o worker continua trabalhando depois do timeout") do
 *   not trigger resume intent.
 * - "retom(a|e|ar)" requires a work noun after o/a, "aquilo", or "de onde", so
 *   "o servidor retoma a conexão sozinho" does not trigger resume intent.
 * - "a gente estava" requires a prior-work verb, "no meio do/da", or "no/na <work noun>",
 *   so "a gente estava errado sobre o cache", "a gente estava no escritório ontem", and
 *   "a gente estava em dúvida sobre o nome" do not trigger resume intent.
 * - Boundaries use Unicode property classes (\p{L}/\p{N}/_) with the u flag
 *   because Python's \w is Unicode-aware ("éúltima" must block the match identically
 *   in both runtimes). Safe to strip from the prompt residual in resumeTopicScore.
 */
export declare const PT_RESUME_CUE_RE: RegExp;
export declare const PT_RESUME_CUE_STRIP_RE: RegExp;
//# sourceMappingURL=pt-resume-cues.d.ts.map