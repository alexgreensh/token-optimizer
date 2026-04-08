"""Looping detector: high user-message similarity with low progress."""

import json


def _word_set(text):
    """Extract lowercase word set for rough similarity."""
    return set(text.lower().split())


def _similarity(a, b):
    """Jaccard similarity between two word sets."""
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union else 0.0


def detect_looping(session_data):
    """Detect sessions where user keeps asking similar things with low progress.

    Scans JSONL for sequences of 4+ user messages with >75% word overlap,
    indicating the model is stuck in a loop.
    """
    jsonl_path = session_data.get("jsonl_path")
    if not jsonl_path:
        return []

    user_messages = []

    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if record.get("type") != "user":
                    continue

                msg = record.get("message", {})
                content = msg.get("content") if isinstance(msg, dict) else msg
                if isinstance(content, str) and len(content) > 10:
                    user_messages.append(content[:500])
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            if len(text) > 10:
                                user_messages.append(text[:500])
                                break

    except (OSError, PermissionError):
        return []

    if len(user_messages) < 4:
        return []

    # Find runs of similar messages
    findings = []
    word_sets = [_word_set(m) for m in user_messages]
    streak = 1
    max_streak = 1

    for i in range(1, len(word_sets)):
        if _similarity(word_sets[i], word_sets[i - 1]) > 0.75:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 1

    if max_streak >= 4:
        est_tokens = max_streak * 5000
        findings.append({
            "name": "looping",
            "confidence": 0.6,
            "evidence": f"{max_streak} similar consecutive user messages detected",
            "savings_tokens": est_tokens,
            "suggestion": (
                f"You sent {max_streak} similar messages in a row, suggesting the model was stuck. "
                "Try: restate the problem differently, provide a concrete example, or /clear and start fresh."
            ),
            "occurrence_count": max_streak,
        })

    return findings
