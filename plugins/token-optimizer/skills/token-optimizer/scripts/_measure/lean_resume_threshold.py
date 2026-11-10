"""Find a session-specific counterfactual auto-compaction threshold.

``resolve(model, largest)`` (optional) returns the user's effective compaction
settings for a model as ``{"tokens", "model_window"}`` (env >
modelSettings.autoCompactWindow > autoCompactWindow > model default, see
_measure/audit._resolve_compact_window). Without it, a context-size heuristic
stands in: 1M window when the session passed 200K, else 200K.
"""
import json


def _scan(path, prompt_tokens):
    observed = []
    largest = 0
    model = None
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
                    model = message.get("model") or model
    return observed, largest, model


def _resolved(largest, model, resolve, key):
    if resolve is not None:
        try:
            value = (resolve(model, largest) or {}).get(key)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
        except Exception:
            pass
    return 967000 if largest > 200000 else 167000


def threshold(path, prompt_tokens, resolve=None):
    try:
        observed, largest, model = _scan(path, prompt_tokens)
    except OSError:
        return None, None
    if observed:
        return min(observed), "observed_auto"
    return _resolved(largest, model, resolve, "tokens"), "fallback"


def window_limit(path, prompt_tokens, resolve=None):
    """Hard ceiling: the session model's context window, ignoring observed compactions."""
    try:
        _observed, largest, model = _scan(path, prompt_tokens)
    except OSError:
        return None
    return _resolved(largest, model, resolve, "model_window")


def window_for(context_tokens, model=None, resolve=None):
    """Context-window ceiling for a session that reached ``context_tokens``."""
    return _resolved(int(context_tokens or 0), model, resolve, "model_window")


def compaction_window(context_tokens, model=None, resolve=None):
    """The user's effective auto-compaction point for a session's model."""
    return _resolved(int(context_tokens or 0), model, resolve, "tokens")
