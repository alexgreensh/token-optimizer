"""Tool cascade detector: 3+ consecutive tool errors forming an error chain."""

import json


def detect_tool_cascade(session_data):
    """Detect chains of consecutive tool errors.

    Scans JSONL for sequences where 4+ tool results are errors in a row,
    indicating an error propagation cascade.
    """
    jsonl_path = session_data.get("jsonl_path")
    if not jsonl_path:
        return []

    consecutive_errors = 0
    streaks = []

    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if record.get("type") != "tool_result":
                    continue

                content = record.get("content", "")
                text = content if isinstance(content, str) else str(content)[:500]
                is_error = record.get("is_error", False) or "error" in text.lower()

                if is_error:
                    consecutive_errors += 1
                else:
                    if consecutive_errors >= 4:
                        streaks.append(consecutive_errors)
                    consecutive_errors = 0

    except (OSError, PermissionError):
        return []

    if consecutive_errors >= 4:
        streaks.append(consecutive_errors)

    findings = []
    for streak_len in streaks:
        est_tokens = streak_len * 2500
        findings.append({
            "name": "tool_cascade",
            "confidence": 0.7,
            "evidence": f"{streak_len} consecutive tool errors in a row",
            "savings_tokens": est_tokens,
            "suggestion": (
                f"A cascade of {streak_len} tool errors burned ~{est_tokens:,} tokens. "
                "Break error chains early: diagnose the root cause after 2 failures."
            ),
            "occurrence_count": len(streaks),
        })

    return findings
