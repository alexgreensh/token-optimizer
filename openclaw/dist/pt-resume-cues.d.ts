/**
 * Portuguese (pt-BR/pt-PT) resume cues. Mirrors _measure/pt_resume_cues.py:
 * - Bare "continua"/"continuar" does NOT match.
 * - "continua/continuar (o|a|com|de)" is restricted to work nouns or "de onde" /
 *   "daquilo" / "aquilo" / "trabalhando", so status reports ("continua o mesmo erro",
 *   "o build continua com erro", "o teste continua de pé", "continua a falhar") do
 *   not trigger resume intent.
 * - "retom(a|e|ar)" covers informal, formal imperative ("retome o trabalho"), and
 *   infinitive forms.
 * - "a gente estava" requires a prior-work verb or locative preposition so
 *   "a gente estava errado sobre o cache" does not trigger resume intent.
 * - Boundaries use Unicode property classes (\p{L}/\p{N}/_) with the u flag
 *   because Python's \w is Unicode-aware ("éúltima" must block the match identically
 *   in both runtimes). Safe to strip from the prompt residual in resumeTopicScore.
 */
export declare const PT_RESUME_CUE_RE: RegExp;
export declare const PT_RESUME_CUE_STRIP_RE: RegExp;
//# sourceMappingURL=pt-resume-cues.d.ts.map