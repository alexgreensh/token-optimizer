"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.PT_RESUME_CUE_STRIP_RE = exports.PT_RESUME_CUE_RE = void 0;
// Portuguese resume cues for OpenClaw continuity. See Python _measure/continuity.py.
/**
 * Portuguese (pt-BR/pt-PT) resume cues. Mirrors the inline PT cue regex in
 * _measure/continuity.py:_resume_intent (same fix: "continua a <infinitive>" excluded -
 * "keeps <verb>ing", not resume; "a" allowed only before a work noun,
 * mirroring English "resume the (work|session|...)"). Accent-tolerant (ultima/última, sessao/sessão).
 * A bare "continua"/"continuar" does NOT match; it needs a following o/com/de/...
 * word. Boundaries use Unicode property classes (\p{L}/\p{N}/_) with the u flag
 * because Python's \w is Unicode-aware: an accented letter glued to a cue
 * ("éúltima") must block the match identically in both runtimes. Latin script
 * with word boundaries, so it is safe to strip from the
 * prompt residual in resumeTopicScore (unlike CJK, which stays detection-only).
 */
exports.PT_RESUME_CUE_RE = /(?<![\p{L}\p{N}_])(?:[úu]ltima sess[ãa]o|sess[ãa]o anterior|da [úu]ltima vez|onde paramos|onde parei|continu(?:a|ar) (?:o|com|de|daquilo|aquilo|trabalhando|a (?:sess[ãa]o|conversa|tarefa|trabalho|projeto|discuss[ãa]o))|retom(?:a|ar) (?:o|a|aquilo|de onde)|retomando|o que est[áa]vamos fazendo|no que est[áa]vamos trabalhando|ontem a gente|a gente estava)(?![\p{L}\p{N}_])/iu;
// Global-flagged strip copy, same reasoning as RESUME_INTENT_STRIP_RE.
exports.PT_RESUME_CUE_STRIP_RE = new RegExp(exports.PT_RESUME_CUE_RE.source, "giu");
/**
 * True when the prompt asks to continue or recall prior work.
 * Exported for tests.
 */
//# sourceMappingURL=pt-resume-cues.js.map