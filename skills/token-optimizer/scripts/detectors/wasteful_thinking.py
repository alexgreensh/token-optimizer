"""Wasteful thinking detector: extended thinking tokens disproportionate to output.

Claude Code only (extended thinking is CC-specific).
"""

import json


def detect_wasteful_thinking(session_data):
    """Detect turns where thinking tokens are disproportionately high vs output.

    Flags when: thinking tokens > 4x output tokens across 4+ turns.
    Indicates overthinking on simple tasks.
    """
    jsonl_path = session_data.get("jsonl_path")
    if not jsonl_path:
        return []

    wasteful_turns = 0
    total_wasted = 0

    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if record.get("type") != "assistant":
                    continue

                msg = record.get("message", {})
                usage = msg.get("usage", {})
                if not usage:
                    continue

                # Extract thinking tokens (extended thinking / reasoning)
                thinking = (
                    usage.get("thinking_tokens", 0)
                    or usage.get("internal_reasoning_tokens", 0)
                    or 0
                )
                output_tokens = usage.get("output_tokens", 0)

                if thinking == 0 or output_tokens == 0:
                    continue

                # Check if thinking is disproportionate
                if thinking <= 4 * output_tokens:
                    continue

                wasteful_turns += 1
                total_wasted += thinking - output_tokens  # excess thinking

    except (OSError, PermissionError):
        return []

    if wasteful_turns >= 4:
        return [{
            "name": "wasteful_thinking",
            "confidence": 0.7,
            "evidence": (
                f"{wasteful_turns} turns with thinking >4x output, "
                f"~{total_wasted:,} excess thinking tokens"
            ),
            "savings_tokens": total_wasted,
            "suggestion": (
                f"{wasteful_turns} turns used extended thinking heavily for small output. "
                "For simple changes, disable extended thinking or use a lighter model."
            ),
            "occurrence_count": wasteful_turns,
        }]

    return []
