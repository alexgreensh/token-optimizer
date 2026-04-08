"""Retry churn detector: same tool+input repeated 3+ times with errors."""

import json


def detect_retry_churn(session_data):
    """Detect repeated tool calls that keep failing.

    Scans JSONL for tool_use blocks where the same (tool, input_hash) appears
    3+ times, indicating the model is retrying a failing approach.
    """
    jsonl_path = session_data.get("jsonl_path")
    if not jsonl_path:
        return []

    # Track (tool_name, input_prefix) -> count of error-preceded repeats
    tool_attempts = {}
    last_tool_key = None
    last_had_error = False

    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                rec_type = record.get("type")

                # Check tool results for errors
                if rec_type == "tool_result":
                    content = record.get("content", "")
                    text = content if isinstance(content, str) else str(content)[:500]
                    if "error" in text.lower() or "failed" in text.lower():
                        last_had_error = True
                    continue

                if rec_type != "assistant":
                    continue

                msg = record.get("message", {})
                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue

                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    tool_name = block.get("name", "")
                    inp = block.get("input", {})
                    # Hash first 200 chars of input for grouping
                    inp_key = str(inp)[:200]
                    key = (tool_name, inp_key)

                    if key == last_tool_key and last_had_error:
                        tool_attempts[key] = tool_attempts.get(key, 1) + 1
                    elif key not in tool_attempts:
                        tool_attempts[key] = 1

                    last_tool_key = key
                    last_had_error = False

    except (OSError, PermissionError):
        return []

    findings = []
    for (tool_name, _), count in tool_attempts.items():
        if count >= 3:
            est_tokens = count * 3000  # ~3K tokens per retry attempt
            findings.append({
                "name": "retry_churn",
                "confidence": 0.8,
                "evidence": f"{tool_name} retried {count} times with errors",
                "savings_tokens": est_tokens,
                "suggestion": (
                    f"Stop and diagnose after 2 failures instead of retrying. "
                    f"{count} retries of {tool_name} wasted ~{est_tokens:,} tokens."
                ),
                "occurrence_count": count,
            })

    return findings
