"""Weak model detector: cheap model used for complex tasks."""

_CHEAP_MODELS = ("haiku", "claude-haiku")


def detect_weak_model(session_data):
    """Detect sessions where Haiku was used for tasks that need a stronger model.

    Flags when: high input tokens (>100K) + Haiku dominant + many tool calls,
    suggesting complex work that would benefit from Sonnet/Opus.
    """
    model_usage = session_data.get("model_usage", {})
    if not model_usage:
        return []

    total_tokens = sum(model_usage.values())
    if total_tokens == 0:
        return []

    haiku_tokens = sum(v for k, v in model_usage.items()
                       if any(t in k.lower() for t in _CHEAP_MODELS))
    haiku_pct = haiku_tokens / total_tokens

    if haiku_pct < 0.5:
        return []

    total_input = session_data.get("total_input_tokens", 0)
    tool_calls = session_data.get("tool_calls", {})
    total_tool_count = sum(tool_calls.values())

    # Flag only if the session is complex: high input + many tool calls
    if total_input < 100_000 or total_tool_count < 10:
        return []

    return [{
        "name": "weak_model",
        "confidence": 0.5,
        "evidence": (
            f"{haiku_pct:.0%} Haiku usage with {total_input:,} input tokens "
            f"and {total_tool_count} tool calls"
        ),
        "savings_tokens": 0,  # No token savings, quality improvement
        "suggestion": (
            "This complex session used Haiku as the primary model. "
            "Consider Sonnet for sessions with >100K context and many tool calls "
            "to reduce errors and retries."
        ),
    }]
