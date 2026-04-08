"""Overpowered model detector: expensive model used for simple tasks."""

import json

_TOP_TIER_MODELS = ("opus", "claude-opus")
_SIMPLE_TOOLS = frozenset({"Read", "Glob", "Grep", "Edit", "Write", "Bash"})


def detect_overpowered(session_data):
    """Detect sessions where Opus was used for tasks that Sonnet could handle.

    Flags when: short output (<5K tokens per turn avg) + mostly simple tools
    + Opus is the dominant model.
    """
    model_usage = session_data.get("model_usage", {})
    if not model_usage:
        return []

    total_tokens = sum(model_usage.values())
    if total_tokens == 0:
        return []

    # Check Opus dominance
    opus_tokens = sum(v for k, v in model_usage.items()
                      if any(t in k.lower() for t in _TOP_TIER_MODELS))
    opus_pct = opus_tokens / total_tokens

    if opus_pct < 0.5:
        return []

    # Check if work was simple: low output, mostly basic tools
    total_output = session_data.get("total_output_tokens", 0)
    api_calls = session_data.get("api_calls", 1)
    avg_output_per_turn = total_output / max(api_calls, 1)

    tool_calls = session_data.get("tool_calls", {})
    total_tool_count = sum(tool_calls.values())
    simple_tool_count = sum(tool_calls.get(t, 0) for t in _SIMPLE_TOOLS)
    simple_pct = simple_tool_count / max(total_tool_count, 1)

    # Only flag if output is light AND tools are simple
    if avg_output_per_turn > 5000 or simple_pct < 0.7:
        return []

    sonnet_savings = int(opus_tokens * 0.6)  # Sonnet is ~60% cheaper
    return [{
        "name": "overpowered",
        "confidence": 0.6,
        "evidence": (
            f"{opus_pct:.0%} Opus usage, {avg_output_per_turn:.0f} avg output tokens/turn, "
            f"{simple_pct:.0%} simple tool calls"
        ),
        "savings_tokens": sonnet_savings,
        "suggestion": (
            f"This session used Opus for mostly simple edits and file reads. "
            f"Sonnet would save ~{sonnet_savings:,} tokens (~60% cost reduction) "
            "for equivalent quality on these tasks."
        ),
        "occurrence_count": 1,
    }]
