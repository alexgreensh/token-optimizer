"""Safety-rule mutations for C7 (SPEC-local-adapt Part B).

Each planted bug breaks one safety invariant; listed tests must go red.
Shape: (name, [test node ids], file_relpath, [(old, new), ...]).
"""
from __future__ import annotations

ESG = "skills/token-optimizer/scripts/emit_safety_gate.py"
RCV = "scripts/replay_critical_vectors.py"

MUTATIONS = [
    ("exempt all file:line search hits including FATAL",
     ["tests/test_emit_safety_gate.py"],
     ESG,
     [("            if not has_search_strict or not _CRIT_SEARCH_STRICT_RE.search(line):\n                return []",
       "            return []")]),
    ("drop context lines check in independent replay gate",
     ["tests/test_replay_critical_vectors.py::test_evidence_hidden_flagged_when_context_lines_dropped"],
     RCV,
     [("        for offset in (-2, -1, 1, 2):",
       "        for offset in ():")]),
    ("ignore non-zero exit status in independent replay gate",
     ["tests/test_local_adapt.py::test_replay_gate_flags_hidden_nonzero_exit_status"],
     RCV,
     [("    if exit_code != 0 and len(emitted_lines) < len(raw_lines):",
       "    if False:")]),
]
