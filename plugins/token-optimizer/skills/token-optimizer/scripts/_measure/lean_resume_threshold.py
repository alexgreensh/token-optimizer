"""Find a session-specific counterfactual auto-compaction threshold."""
import json


def threshold(path, prompt_tokens):
    observed = []
    largest = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(record, dict) or record.get("isSidechain"):
                    continue
                meta = record.get("compactMetadata")
                if (record.get("type") == "system" and record.get("subtype") == "compact_boundary"
                        and isinstance(meta, dict) and meta.get("trigger") == "auto"):
                    value = meta.get("preTokens")
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                        observed.append(value)
                elif record.get("type") == "assistant":
                    message = record.get("message")
                    usage = message.get("usage") if isinstance(message, dict) else None
                    if isinstance(usage, dict):
                        largest = max(largest, prompt_tokens(usage))
    except OSError:
        return None, None
    if observed:
        return min(observed), "observed_auto"
    return (967000 if largest > 200000 else 167000), "fallback"
