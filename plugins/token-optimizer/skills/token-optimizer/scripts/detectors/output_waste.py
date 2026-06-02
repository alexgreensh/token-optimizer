"""Output token waste detector: flags sessions with excessive output relative to task complexity."""

_OUTPUT_RATIO_THRESHOLD = 3.0  # output/input ratio above this on simple turns
_VERBOSE_RESPONSE_TOKENS = 2000  # assistant message above this after simple ops
_SIMILARITY_THRESHOLD = 0.6  # Jaccard word overlap for repeated explanations
_MIN_TURNS_FOR_DETECTION = 5
_MIN_SAVINGS_TOKENS = 5000  # minimum token savings to report a finding


def _jaccard_words(a: str, b: str) -> float:
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _is_simple_tool_turn(turn: dict) -> bool:
    tools = turn.get("tools_used", [])
    if not tools:
        return False
    simple = {"Read", "Glob", "Grep", "Edit", "Write"}
    return all(t in simple for t in tools) and len(tools) <= 2


def detect_output_waste(session_data):
    """Detect sessions where output tokens are disproportionate to task complexity.

    Three signals:
    1. High output/input ratio on turns with only simple tools
    2. Long assistant messages (>2K tokens) after single-file operations
    3. Repeated similar explanations across turns (Jaccard >0.6)
    """
    turns = session_data.get("turns", [])
    if len(turns) < _MIN_TURNS_FOR_DETECTION:
        return []

    total_output = session_data.get("total_output_tokens", 0)
    if total_output == 0:
        return []

    findings = []

    # Signal 1: Session-level output/input ratio on simple turns
    simple_turn_output = 0
    simple_turn_input = 0
    simple_turn_count = 0
    for turn in turns:
        if _is_simple_tool_turn(turn):
            simple_turn_output += turn.get("output_tokens", 0)
            simple_turn_input += turn.get("input_tokens", 1)
            simple_turn_count += 1

    if simple_turn_count >= 3 and simple_turn_input > 0:
        ratio = simple_turn_output / simple_turn_input
        if ratio > _OUTPUT_RATIO_THRESHOLD:
            excess = simple_turn_output - int(simple_turn_input * 1.5)
            if excess > _MIN_SAVINGS_TOKENS:
                findings.append({
                    "name": "output_waste",
                    "confidence": min(0.5 + (ratio - _OUTPUT_RATIO_THRESHOLD) * 0.1, 0.85),
                    "evidence": (
                        f"{ratio:.1f}x output/input ratio across {simple_turn_count} simple turns "
                        f"({simple_turn_output:,} output vs {simple_turn_input:,} input tokens)"
                    ),
                    "savings_tokens": excess,
                    "suggestion": (
                        f"Output tokens are {ratio:.1f}x higher than input on simple file operations. "
                        f"This session could save ~{excess:,} output tokens by requesting concise responses. "
                        "Add 'Be concise' or 'No explanations' to CLAUDE.md for routine tasks."
                    ),
                    "occurrence_count": simple_turn_count,
                })

    # Signal 2: Verbose responses after simple operations
    verbose_after_simple = 0
    verbose_waste = 0
    for turn in turns:
        if _is_simple_tool_turn(turn):
            output_tok = turn.get("output_tokens", 0)
            if output_tok > _VERBOSE_RESPONSE_TOKENS:
                verbose_after_simple += 1
                verbose_waste += output_tok - _VERBOSE_RESPONSE_TOKENS

    if verbose_after_simple >= 3 and verbose_waste > _MIN_SAVINGS_TOKENS:
        findings.append({
            "name": "output_waste",
            "confidence": 0.65,
            "evidence": (
                f"{verbose_after_simple} turns had >2K output tokens after simple file operations "
                f"(~{verbose_waste:,} excess output tokens)"
            ),
            "savings_tokens": verbose_waste,
            "suggestion": (
                f"Found {verbose_after_simple} cases of verbose responses to simple edits/reads. "
                f"~{verbose_waste:,} output tokens could be saved with more targeted instructions."
            ),
            "occurrence_count": verbose_after_simple,
        })

    # Signal 3: Repeated similar explanations
    assistant_msgs = [
        t.get("assistant_text", "")
        for t in turns
        if len(t.get("assistant_text", "")) > 200
    ][:100]
    repeated_pairs = 0
    repeated_waste = 0
    for i in range(len(assistant_msgs)):
        for j in range(i + 1, min(i + 5, len(assistant_msgs))):
            if _jaccard_words(assistant_msgs[i], assistant_msgs[j]) > _SIMILARITY_THRESHOLD:
                repeated_pairs += 1
                repeated_waste += len(assistant_msgs[j]) // 4

    if repeated_pairs >= 2 and repeated_waste > _MIN_SAVINGS_TOKENS:
        findings.append({
            "name": "output_waste",
            "confidence": 0.55,
            "evidence": (
                f"{repeated_pairs} pairs of similar assistant responses detected "
                f"(>{_SIMILARITY_THRESHOLD:.0%} word overlap, ~{repeated_waste:,} repeated tokens)"
            ),
            "savings_tokens": repeated_waste,
            "suggestion": (
                f"The model repeated similar explanations {repeated_pairs} times. "
                f"~{repeated_waste:,} tokens could be saved. "
                "Consider adding 'Don't repeat previous explanations' to CLAUDE.md."
            ),
            "occurrence_count": repeated_pairs,
        })

    return findings
