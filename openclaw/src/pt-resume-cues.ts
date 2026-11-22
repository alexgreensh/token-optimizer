// Portuguese resume cues for OpenClaw continuity. See Python _measure/pt_resume_cues.py.
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
export const PT_RESUME_CUE_RE =
  /(?<![\p{L}\p{N}_])(?:[úu]ltima sess[ãa]o|sess[ãa]o anterior|da [úu]ltima vez|onde paramos|onde parei|continu(?:a|ar) (?:(?:o|a|com(?: (?:o|a))?|de(?: (?:o|a))?|d[oa]) (?:sess[ãa]o|conversa|tarefa|trabalho|projeto|discuss[ãa]o)|de onde(?: (?:paramos|parei))?|daquilo|aquilo|trabalhando (?:n[oa]s?|em|nisso|naquilo))|retom(?:a|e|ar) (?:(?:o|a) (?:sess[ãa]o|conversa|tarefa|trabalho|projeto|discuss[ãa]o)|aquilo|de onde(?: (?:paramos|parei))?)|retomando|o que est[áa]vamos fazendo|no que est[áa]vamos trabalhando|ontem a gente|a gente estava (?:trabalhando|fazendo|vendo|mexendo|no meio d[oa]|n[oa] (?:sess[ãa]o|conversa|tarefa|trabalho|projeto|discuss[ãa]o)))(?![\p{L}\p{N}_])/iu;

// Global-flagged strip copy, same reasoning as RESUME_INTENT_STRIP_RE.
export const PT_RESUME_CUE_STRIP_RE = new RegExp(PT_RESUME_CUE_RE.source, "giu");
