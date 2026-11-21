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
export declare const PT_RESUME_CUE_RE: RegExp;
export declare const PT_RESUME_CUE_STRIP_RE: RegExp;
/**
 * True when the prompt asks to continue or recall prior work.
 * Exported for tests.
 */
//# sourceMappingURL=pt-resume-cues.d.ts.map