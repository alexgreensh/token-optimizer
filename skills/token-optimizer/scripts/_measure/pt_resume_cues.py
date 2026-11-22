"""Portuguese (pt-BR/pt-PT) resume cue matcher shared by _measure/continuity.py."""
from __future__ import annotations

import re

# Single source of truth for Portuguese resume cues in Python continuity.
# Mirrors openclaw/src/pt-resume-cues.ts and opencode/src/continuity/resume-lean.ts.
#
# Guardrails:
# - Bare "continua"/"continuar" does NOT match.
# - "continua/continuar (o|a|com|de)" is restricted to work nouns (sessao, conversa,
#   tarefa, trabalho, projeto, discussao) or "de onde" / "daquilo" / "aquilo" /
#   "trabalhando", so status reports ("continua o mesmo erro", "o build continua com
#   erro", "o teste continua de pe", "continua a falhar") do not trigger resume intent.
# - "retom(a|e|ar)" covers informal, formal imperative ("retome o trabalho"), and
#   infinitive forms.
# - "a gente estava" requires a prior-work verb or locative preposition so
#   "a gente estava errado sobre o cache" does not trigger resume intent.
# - Latin script WITH Unicode \w boundaries, safe to strip from the prompt residual
#   in _resume_topic_score (unlike CJK, which stays detection-only).
PT_RESUME_CUE_RE = re.compile(
    r"(?<!\w)(?:"
    r"[úu]ltima sess[ãa]o|sess[ãa]o anterior|da [úu]ltima vez|"
    r"onde paramos|onde parei|"
    r"continu(?:a|ar) (?:"
    r"(?:o|a|com(?: (?:o|a))?|de(?: (?:o|a))?|d[oa]) "
    r"(?:sess[ãa]o|conversa|tarefa|trabalho|projeto|discuss[ãa]o)|"
    r"de onde(?: (?:paramos|parei))?|daquilo|aquilo|trabalhando)|"
    r"retom(?:a|e|ar) (?:o|a|aquilo|de onde(?: (?:paramos|parei))?)|retomando|"
    r"o que est[áa]vamos fazendo|no que est[áa]vamos trabalhando|"
    r"ontem a gente|a gente estava (?:trabalhando|fazendo|vendo|mexendo|no|na|em)"
    r")(?!\w)",
    re.IGNORECASE,
)


def pt_resume_cue_re():
    """Return the compiled Portuguese resume-cue pattern."""
    return PT_RESUME_CUE_RE
