"""Privacy-safe first-call context observations from parent transcripts."""
import json
import statistics
from collections import defaultdict


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(item) for item in value)
    if isinstance(value, dict):
        return _text(value.get("text", value.get("content", "")))
    return ""


def observe(rows, estimate):
    """Return estimates and measured API input for the first parent call only."""
    parts = defaultdict(int)
    first_user = False
    hook_owners = {}
    for row in rows:
        if row.get("isSidechain"):
            continue
        kind = row.get("type")
        if kind == "assistant":
            message = row.get("message") or {}
            usage = message.get("usage") or {}
            if not isinstance(usage, dict) or not usage:
                continue
            try:
                total = sum(int(usage.get(key) or 0) for key in
                            ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            except (TypeError, ValueError):
                return None
            timestamp = row.get("timestamp")
            if total <= 0 or not isinstance(timestamp, str) or len(timestamp) < 7:
                return None
            return {"month": timestamp[:7], "total": total, "components": dict(parts)}
        if kind == "user" and not first_user and not row.get("isMeta"):
            message = row.get("message") or {}
            if isinstance(message, dict) and message.get("role") == "user":
                parts["first_user"] += estimate(_text(message.get("content")))
                first_user = True
        if kind != "attachment":
            continue
        a = row.get("attachment") or {}
        if not isinstance(a, dict):
            continue
        typ = a.get("type")
        if typ == "hook_success":
            command = str(a.get("command") or "").lower()
            hook_owners[a.get("toolUseID")] = "token_optimizer" if "token-optimizer" in command else "other_or_unknown"
        mapping = {"skill_listing": ("skills", "content"),
                   "deferred_tools_delta": ("mcp_tools", "addedLines"),
                   "mcp_instructions_delta": ("mcp_instructions", "addedBlocks"),
                   "agent_listing_delta": ("agents", "addedLines")}
        if typ in mapping:
            name, field = mapping[typ]
            parts[name] += estimate(_text(a.get(field)))
            if typ == "deferred_tools_delta":
                parts[name] += sum(estimate(_text(item.get("definition"))) for item in a.get("surfacedDefinitions") or [] if isinstance(item, dict))
        elif typ == "instructions":
            for item in a.get("files") or []:
                if isinstance(item, dict):
                    parts["instructions"] += estimate(_text(item.get("content")))
        elif typ == "hook_additional_context":
            event = a.get("hookEvent")
            if event in ("SessionStart", "UserPromptSubmit"):
                # The hook name is evidence, not a path or content to expose.
                owner = hook_owners.get(a.get("toolUseID"), "other_or_unknown")
                parts[event + "_" + owner] += estimate(_text(a.get("content")))
    return None


def _quantile(values, fraction):
    values = sorted(values)
    index = (len(values) - 1) * fraction
    lo = int(index)
    hi = min(lo + 1, len(values) - 1)
    return round(values[lo] + (values[hi] - values[lo]) * (index - lo))


def summarize(observations):
    groups = defaultdict(list)
    for row in observations:
        if row:
            groups[row["month"]].append(row)
    result = []
    for month, rows in sorted(groups.items()):
        totals = [row["total"] for row in rows]
        names = set().union(*(row["components"] for row in rows))
        components = {}
        for name in sorted(names):
            found = [row["components"][name] for row in rows if name in row["components"]]
            components[name] = {"median": round(statistics.median(found)), "observed": len(found)}
        result.append({"month": month, "n": len(rows), "median": round(statistics.median(totals)),
                       "p25": _quantile(totals, .25), "p75": _quantile(totals, .75),
                       "components": components})
    return result


def transcript(path):
    with open(path, encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row
