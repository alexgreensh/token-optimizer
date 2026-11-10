"""Re-fetch metric for file content printed through the shell. Measures only.

When the generic fallback cuts the middle out of `sed -n`, `cat`, `head`,
`tail`, `nl` or `git show <rev>:<file>` output, that is file content the model
asked to see. This module answers one question from the user's own transcripts:
after such an elided result, did the same session fetch the same file range
again (a Read, or a shell print of the same file) within the next three tool
calls? It changes nothing about compression.

Decision rule, fixed before any measurement:
  fewer than 200 elided events       CANNOT-MEASURE
  re-fetch rate above 15%            MAKE-VERBATIM  (these commands become lossless)
  re-fetch rate below 5%             MAY-WIDEN      (fallback may reach prefixed forms, in shadow)
  otherwise                          NO-CHANGE

Privacy: only counts leave this module. No path, command or output text is kept
past the comparison, and nothing is written anywhere.
"""
import json
import os
import re
import shlex

WINDOW_CALLS = 3
MIN_EVENTS = 200
VERBATIM_ABOVE = 0.15
WIDEN_BELOW = 0.05

# TO's own elision line from the generic compressor (bash_compress._compress_generic).
_ELIDED = re.compile(r"(?m)^\.\.\. \d+ lines omitted(?: \(kept \d+ error/warning lines\))? \.\.\.$")
_PREFIX = re.compile(r"^\s*cd\s+(\"[^\"]*\"|'[^']*'|[^\s;&|]+)\s*(?:&&|;)\s*")
_FILE_PRINTERS = {"cat", "head", "tail", "nl", "sed"}
_NO_RANGE = None  # an unknown range overlaps everything (conservative: counts as a re-fetch)


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def calls(path):
    """Ordered tool calls of one transcript: [{name, input, cwd, text, is_error}].

    ``text`` is kept for Bash results only; other tools never need it here.
    """
    order, by_id = [], {}
    with open(path, encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if '"tool_use"' not in line and '"tool_result"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            message = row.get("message") if isinstance(row, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            cwd = row.get("cwd") if isinstance(row.get("cwd"), str) else ""
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "tool_use" and block.get("id"):
                    entry = {"name": block.get("name"), "input": block.get("input") or {},
                             "cwd": cwd, "text": "", "is_error": False, "has_result": False}
                    order.append(entry)
                    by_id[block["id"]] = entry
                elif kind == "tool_result":
                    entry = by_id.get(block.get("tool_use_id"))
                    if entry is not None:
                        entry["has_result"] = True
                        entry["is_error"] = bool(block.get("is_error"))
                        if entry["name"] == "Bash":
                            entry["text"] = _text(block.get("content"))
    return order


def _abs(path, cwd, base):
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(base or cwd or "", p)
    return os.path.normpath(p)


def print_targets(command, cwd=""):
    """For a single-stage file print, return [(key, (start, end) or None)], else [].

    key is a normalized absolute path, or ``rev:path`` for `git show`. Pipelines,
    redirections, substitutions and multi-statement commands return [] because
    TO is not the only thing shaping their output.
    """
    if not isinstance(command, str):
        return []
    base = cwd
    m = _PREFIX.match(command)
    if m:
        d = m.group(1).strip("'\"")
        base = _abs(d, cwd, cwd)
        command = command[m.end():]
    if any(c in command for c in "|&;<>`$\n"):
        return []
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    if not tokens:
        return []
    head = tokens[0]
    if head == "git":
        if len(tokens) == 3 and tokens[1] == "show" and ":" in tokens[2] and not tokens[2].startswith("-"):
            rev, _, rel = tokens[2].partition(":")
            return [(f"git:{rev}:{_abs(rel, cwd, base)}", _NO_RANGE)] if rev and rel else []
        return []
    if head not in _FILE_PRINTERS:
        return []
    rest = tokens[1:]
    rng, files, i = _NO_RANGE, [], 0
    if head == "sed":
        script, i = None, 0
        for a in rest:
            if a == "-n":
                continue
            if a.startswith("-") and a != "-":
                return []  # -i, -e, -E ... not the plain print form
            if script is None:
                script = a
            else:
                files.append(a)
        mm = re.fullmatch(r"(\d+)(?:,(\d+))?p", script or "")
        if not mm or not files:
            return []
        start = int(mm.group(1))
        rng = (start, int(mm.group(2) or start))
    else:
        count = None
        while i < len(rest):
            a = rest[i]
            if head in ("head", "tail") and a in ("-n", "-c", "--lines") and i + 1 < len(rest):
                count = rest[i + 1] if a != "-c" else None
                i += 2
            elif head in ("head", "tail") and re.fullmatch(r"-\d+", a):
                count = a[1:]
                i += 1
            elif a.startswith("-") and a != "-":
                i += 1
            else:
                files.append(a)
                i += 1
        if head == "head" and not files:
            return []
        if head == "head":
            n = int(count) if count and count.lstrip("+").isdigit() else 10
            rng = (1, n)
        if head in ("cat", "nl"):
            rng = _NO_RANGE
    if not files:
        return []
    return [(_abs(f, cwd, base), rng) for f in files]


def read_target(entry):
    path = (entry.get("input") or {}).get("file_path")
    if not isinstance(path, str) or not path:
        return None
    inp = entry["input"]
    offset, limit = inp.get("offset"), inp.get("limit")
    rng = _NO_RANGE
    if isinstance(offset, int) and offset >= 1:
        rng = (offset, offset + limit - 1 if isinstance(limit, int) and limit >= 1 else 10 ** 9)
    elif isinstance(limit, int) and limit >= 1:
        rng = (1, limit)
    return _abs(path, entry.get("cwd", ""), entry.get("cwd", "")), rng


def _overlap(a, b):
    if a is _NO_RANGE or b is _NO_RANGE:
        return True
    return a[0] <= b[1] and b[0] <= a[1]


def _follow_up_targets(entry):
    if entry["name"] == "Read":
        t = read_target(entry)
        return [t] if t else []
    if entry["name"] == "Bash":
        cmd = entry["input"].get("command")
        return print_targets(cmd, entry.get("cwd", ""))
    return []


def scan(sequence, acc=None):
    """Count elided file prints and re-fetches in one transcript's call order."""
    acc = acc if acc is not None else new_counts()
    for i, entry in enumerate(sequence):
        if entry["name"] != "Bash" or entry["is_error"] or not _ELIDED.search(entry["text"]):
            continue
        targets = print_targets(entry["input"].get("command"), entry.get("cwd", ""))
        if not targets:
            continue
        acc["events"] += 1
        same_file = same_range = False
        for later in sequence[i + 1:i + 1 + WINDOW_CALLS]:
            for key, rng in _follow_up_targets(later):
                for tkey, trng in targets:
                    if key == tkey:
                        same_file = True
                        if _overlap(rng, trng):
                            same_range = True
        acc["refetched_file"] += int(same_file)
        acc["refetched"] += int(same_range)
    return acc


def new_counts():
    return {"events": 0, "refetched": 0, "refetched_file": 0}


def verdict(events, refetched):
    if events < MIN_EVENTS:
        return "CANNOT-MEASURE"
    rate = refetched / events
    if rate > VERBATIM_ABOVE:
        return "MAKE-VERBATIM"
    if rate < WIDEN_BELOW:
        return "MAY-WIDEN"
    return "NO-CHANGE"


def summarize(acc):
    events = acc["events"]
    return {"events": events, "refetched": acc["refetched"], "refetched_any_range": acc["refetched_file"],
            "rate": (acc["refetched"] / events) if events else None,
            "rate_any_range": (acc["refetched_file"] / events) if events else None,
            "window_calls": WINDOW_CALLS, "min_events": MIN_EVENTS,
            "verdict": verdict(events, acc["refetched"])}


def format_lines(summary):
    rate = "n/a" if summary["rate"] is None else f"{summary['rate'] * 100:.1f}%"
    return [f"File content printed through the shell and cut by the generic fallback: "
            f"{summary['events']:,} events.",
            f"Same file range fetched again within {summary['window_calls']} calls: "
            f"{summary['refetched']:,} ({rate}). Same file, any range: {summary['refetched_any_range']:,}.",
            f"Verdict by the written rule (>15% verbatim, <5% may widen, under "
            f"{summary['min_events']} events cannot measure): {summary['verdict']}."]
