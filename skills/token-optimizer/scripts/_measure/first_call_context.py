"""Privacy-safe first-call context observations from parent transcripts."""
import json
import re
import sqlite3
import statistics
import threading
import time
from collections import defaultdict, OrderedDict
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _optimizer_commands():
    """Use registered hook commands as identity evidence, not runner names."""
    try:
        root = json.loads((Path(__file__).resolve().parents[4] / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, IndexError):
        return frozenset()
    commands, pending = set(), [root]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if isinstance(item.get("command"), str):
                commands.add(item["command"])
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return frozenset(commands)


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
    hook_outputs = []
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
            command = str(a.get("command") or "")
            if command in _optimizer_commands() or any(name in command.lower() for name in ("token-optimizer", "token_optimizer")):
                try:
                    output = json.loads(a.get("stdout") or "{}")
                    context = (output.get("hookSpecificOutput") or {}).get("additionalContext")
                    if isinstance(context, str) and context:
                        hook_outputs.append((a.get("hookEvent"), context))
                except (ValueError, TypeError, AttributeError):
                    pass
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
                    path = str(item.get("path") or "").replace("\\", "/").lower()
                    kind = str(item.get("type") or "").lower()
                    if path.endswith("/claude.md") or path == "claude.md":
                        name = "claude_md"
                    elif "/memory/" in path or path.endswith("/memory.md") or kind == "memory":
                        name = "memory_files"
                    else:
                        name = "other_instructions"
                    parts[name] += estimate(_text(item.get("content")))
        elif typ == "hook_additional_context":
            event = a.get("hookEvent")
            if event in ("SessionStart", "UserPromptSubmit"):
                content = _text(a.get("content"))
                for item in hook_outputs[:]:
                    if item[0] == event and item[1] in content:
                        parts[event + "_token_optimizer"] += estimate(item[1])
                        content = content.replace(item[1], "", 1)
                        hook_outputs.remove(item)
                parts[event + "_other_or_unknown"] += estimate(content)
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


_RELEVANT_TYPE = re.compile(r'"type"\s*:\s*"(?:assistant|user|attachment)"')


def transcript(path):
    with open(path, encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if '\\u' not in line and not _RELEVANT_TYPE.search(line):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


_cache = OrderedDict()
_cache_lock = threading.Lock()


def collect(db_path, data_dir):
    """Share a scan for one minute after it finishes, even if it ran slowly."""
    key = (str(db_path), str(data_dir))
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.monotonic() - entry[0] < 60:
            _cache.move_to_end(key)
            return entry[1]
        value = _collect_uncached(*key)
        _cache[key] = (time.monotonic(), value)
        _cache.move_to_end(key)
        while len(_cache) > 4:
            _cache.popitem(last=False)
        return value


def _collect_uncached(db_path, data_dir):
    from . import structural_ledger
    db_path = Path(db_path)
    if not db_path.is_file():
        return {"months": [], "trim_dates": [], "missing": 0}
    observations, missing, seen = [], 0, set()
    try:
        with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            paths = conn.execute("SELECT DISTINCT jsonl_path FROM session_log WHERE jsonl_path IS NOT NULL AND COALESCE(is_sidechain,0)=0").fetchall()
        files = []
        for (name,) in paths:
            path = Path(name)
            if path in seen:
                continue
            seen.add(path)
            try:
                files.append((str(path), path.stat()))
            except (OSError, ValueError, TypeError):
                missing += 1
        from . import evidence_parallel
        if len(files) >= 256 and evidence_parallel.safe_entrypoint():
            batches = evidence_parallel.batches(files, None, 8 * 1024 * 1024, evidence_parallel.first_call)
        else:
            batches = ([((path, stat), evidence_parallel.first_call(path))] for path, stat in files)
        for batch in batches:
            for _, (item, failed) in batch:
                missing += int(failed)
                if item:
                    observations.append(item)
        dates = sorted({str(t.get("ts"))[:10] for t in structural_ledger.load_trims(data_dir)
                        if t.get("ts") and int(t.get("tokens") or 0) > 0})
        return {"months": summarize(observations), "trim_dates": dates, "missing": missing}
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return {"months": [], "trim_dates": [], "missing": missing}
