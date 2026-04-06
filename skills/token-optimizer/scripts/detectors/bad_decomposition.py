"""Bad decomposition detector: monolithic prompts that should be split."""

import json
import re

# Imperative verbs commonly starting task instructions
_IMPERATIVE_PATTERN = re.compile(
    r'\b(add|create|update|fix|remove|delete|change|modify|implement|refactor|'
    r'write|build|set up|configure|migrate|deploy|test|check|verify|ensure|'
    r'move|rename|replace|install|run|convert|merge|split|extract)\b',
    re.IGNORECASE,
)


def detect_bad_decomposition(session_data):
    """Detect user prompts that try to do too much in one turn.

    Scans JSONL for user messages with >500 words and 3+ imperative verbs,
    suggesting the task should be decomposed into smaller steps.
    """
    jsonl_path = session_data.get("jsonl_path")
    if not jsonl_path:
        return []

    findings = []
    monolith_count = 0

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
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            break

                if not text:
                    continue

                word_count = len(text.split())
                if word_count < 500:
                    continue

                imperative_matches = set(_IMPERATIVE_PATTERN.findall(text))
                if len(imperative_matches) >= 3:
                    monolith_count += 1

    except (OSError, PermissionError):
        return []

    if monolith_count > 0:
        est_tokens = monolith_count * 8000  # monolith prompts waste context on confusion
        findings.append({
            "name": "bad_decomposition",
            "confidence": 0.6,
            "evidence": f"{monolith_count} prompts with 500+ words and 3+ task verbs",
            "savings_tokens": est_tokens,
            "suggestion": (
                f"Found {monolith_count} monolithic prompts. Break large requests into "
                "sequential steps: one task per message improves accuracy and reduces retries."
            ),
        })

    return findings
