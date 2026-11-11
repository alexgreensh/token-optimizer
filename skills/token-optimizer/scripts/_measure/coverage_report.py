"""Bash coverage funnel: summary for the dashboard and the read-only replay.

``collect`` reads the counter rows the PostToolUse hook writes (coverage_funnel)
and returns the share of command-output tokens that ended in each gate.
``replay`` re-runs the same gates over the user's own transcripts, strictly
read-only, and feeds the same ``summarize``. Neither stores, prints or returns
command text or output text: only gate names, fixed-vocabulary reasons,
compressor pattern names and token counts leave this module.

Shares are fractions of command-output tokens and sum to 1 across the five
gates by construction (every result lands in exactly one).
"""
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

WINDOW_DAYS = 30
DEFAULT_MAX_RESULTS = 20000


def _gates():
    import coverage_funnel
    return coverage_funnel.GATES, coverage_funnel.GATE_LABELS


def summarize(rows, window_days=None):
    """rows: iterable of (gate, reason, pattern, tokens, removed_tokens)."""
    gates, labels = _gates()
    acc = {g: {"results": 0, "tokens": 0, "removed": 0} for g in gates}
    rejects, patterns, shorts = Counter(), Counter(), Counter()
    for gate, reason, pattern, tokens, removed in rows:
        if gate not in acc:
            continue
        tokens, removed = int(tokens or 0), int(removed or 0)
        a = acc[gate]
        a["results"] += 1
        a["tokens"] += tokens
        a["removed"] += removed
        if gate == "not_read_only":
            rejects[reason or "unknown"] += tokens
        elif gate == "compressed":
            patterns[pattern or "unlabelled"] += removed
        elif gate == "error_or_short":
            shorts[reason or "unknown"] += tokens
    total = sum(a["tokens"] for a in acc.values())
    results = sum(a["results"] for a in acc.values())
    out_gates = []
    for g in gates:
        a = acc[g]
        out_gates.append({"gate": g, "label": labels[g], "results": a["results"],
                          "tokens": a["tokens"], "removed_tokens": a["removed"],
                          "share": (a["tokens"] / total) if total else 0.0})
    top = rejects.most_common(1)
    return {
        "available": True, "empty": results == 0, "window_days": window_days,
        "results": results, "tokens": total,
        "gates": out_gates,
        "acted_share": (acc["compressed"]["tokens"] / total) if total else 0.0,
        "removed_tokens": acc["compressed"]["removed"],
        "not_eligible_share": (acc["not_read_only"]["tokens"] / total) if total else 0.0,
        "top_reject": ({"reason": top[0][0], "tokens": top[0][1],
                        "share": top[0][1] / total} if top and total else None),
        "rejects": [{"reason": r, "tokens": t, "share": (t / total) if total else 0.0}
                    for r, t in rejects.most_common(8)],
        "short_reasons": [{"reason": r, "tokens": t} for r, t in shorts.most_common()],
        "patterns": [{"pattern": p, "removed_tokens": t} for p, t in patterns.most_common(8)],
    }


def unavailable(why):
    return {"available": False, "why": why}


def collect(db_path, days=WINDOW_DAYS, now=None):
    """Summary of the last ``days`` days from the counter table. Read-only.

    A missing database or table is "unavailable" (unknown), never a row of zeros.
    A table with no rows in the window is ``empty`` (nothing counted yet).
    """
    path = Path(db_path)
    if not path.is_file():
        return unavailable("no ledger")
    cutoff = ((now or datetime.now()) - timedelta(days=days)).isoformat()
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0) as conn:
            have = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bash_coverage'").fetchone()
            if not have:
                return unavailable("no counter table yet")
            rows = conn.execute(
                "SELECT gate, reason, pattern, tokens, removed_tokens FROM bash_coverage WHERE timestamp >= ?",
                (cutoff,)).fetchall()
    except (sqlite3.Error, OSError, ValueError):
        return unavailable("ledger unreadable")
    return summarize(rows, days)


def _fmt_tokens(n):
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}K"
    return f"{n:,}"


def headline(summary):
    """One honest sentence: what TO saw, what it acted on, what was not eligible."""
    if not summary or not summary.get("available"):
        return "Command-output coverage is unknown: the counter ledger could not be read."
    if summary.get("empty"):
        return "No command output has been counted yet."
    line = (f"TO saw {_fmt_tokens(summary['tokens'])} tokens of command output, "
            f"acted on {summary['acted_share'] * 100:.1f}%. "
            f"Not eligible: {summary['not_eligible_share'] * 100:.1f}%")
    top = summary.get("top_reject")
    if top:
        line += f" (top reason: {top['reason']}, {top['share'] * 100:.1f}%)"
    return line + "."


def format_table(summary, source_note=""):
    if not summary.get("available"):
        return headline(summary)
    out = [headline(summary), ""]
    if source_note:
        out += [source_note, ""]
    out.append(f"{'gate':50s} {'results':>8s} {'tokens':>10s} {'share':>7s} {'removed':>9s}")
    for g in summary["gates"]:
        out.append(f"{g['label'][:50]:50s} {g['results']:8d} {g['tokens']:10d} "
                   f"{g['share'] * 100:6.1f}% {g['removed_tokens']:9d}")
    if summary["rejects"]:
        out += ["", "Not eligible, by reason (share of all command-output tokens):"]
        out += [f"  {r['reason']:40s} {r['share'] * 100:5.1f}%" for r in summary["rejects"]]
    if summary["short_reasons"]:
        out += ["", "Left raw by design:"]
        out += [f"  {r['reason']:40s} {r['tokens']:>9d} tokens" for r in summary["short_reasons"]]
    if summary["patterns"]:
        out += ["", "Compressed, by compressor (tokens removed):"]
        out += [f"  {r['pattern']:40s} {r['removed_tokens']:>9d}" for r in summary["patterns"]]
    if summary.get("file_refetch"):
        from . import file_refetch
        out += [""] + file_refetch.format_lines(summary["file_refetch"])
    return "\n".join(out)


def _cutoff(now, days):
    """Oldest modification time the window admits. A window reaching before the epoch (``--days 36500``)
    admits every file: Windows rejects pre-epoch local timestamps (OSError) and an absurd ``days``
    overflows the date arithmetic on every platform."""
    try:
        return max(0.0, ((now or datetime.now()) - timedelta(days=days)).timestamp())
    except (OverflowError, OSError, ValueError):
        return 0.0


def _transcripts(projects_dir, days, now=None):
    """Transcript files modified within ``days``, newest first, or None if the folder is missing."""
    root = Path(projects_dir)
    if not root.is_dir():
        return None
    cutoff = _cutoff(now, days)
    files = []
    for p in root.rglob("*.jsonl"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            files.append((mtime, p))
    files.sort(reverse=True)
    return [p for _, p in files]


def replay(projects_dir, days=WINDOW_DAYS, max_results=DEFAULT_MAX_RESULTS, now=None, deadline=None):
    """Replay the hook's gates over transcripts under ``projects_dir``. Read-only.

    Newest files first, files older than ``days`` skipped by modification time.
    Stops at ``max_results`` Bash results (reported in ``truncated``). Nothing is
    written; no command or output text is kept past the gate decision. The same
    walk feeds the file-content re-fetch metric (see file_refetch).
    """
    here = str(Path(__file__).resolve().parent.parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    from coverage_funnel import classify_result
    from . import file_refetch
    files = _transcripts(projects_dir, days, now)
    if files is None:
        return unavailable("no transcripts folder")
    rows, truncated, unreadable = [], False, 0
    refetch = file_refetch.new_counts()
    for path in files:
        if truncated or (deadline and time.monotonic() > deadline):
            truncated = True
            break
        try:
            sequence = file_refetch.calls(path)
        except OSError:
            unreadable += 1
            continue
        file_refetch.scan(sequence, refetch)
        for entry in sequence:
            cmd = entry["input"].get("command") if entry["name"] == "Bash" else None
            if not entry["has_result"] or not isinstance(cmd, str):
                continue
            r = classify_result(cmd, entry["text"], entry["is_error"])
            rows.append((r["gate"], r["reason"], r["pattern"], r["tokens"], r["removed"]))
            if len(rows) >= max_results:
                truncated = True
                break
    result = summarize(rows, days)
    result.update(files=len(files), unreadable=unreadable, truncated=truncated,
                  file_refetch=file_refetch.summarize(refetch))
    return result


def refetch_only(projects_dir, days=WINDOW_DAYS, now=None):
    """Just the file-content re-fetch metric over the same transcripts. Read-only."""
    from . import file_refetch
    files = _transcripts(projects_dir, days, now)
    if files is None:
        return unavailable("no transcripts folder")
    acc, unreadable = file_refetch.new_counts(), 0
    for path in files:
        try:
            file_refetch.scan(file_refetch.calls(path), acc)
        except OSError:
            unreadable += 1
    out = file_refetch.summarize(acc)
    out.update(available=True, files=len(files), unreadable=unreadable, window_days=days)
    return out


def _parse_args(args, projects_dir):
    days, max_results, as_json = WINDOW_DAYS, DEFAULT_MAX_RESULTS, False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--json":
            as_json = True
        elif a in ("--days", "--max", "--projects-dir") and i + 1 < len(args):
            try:
                if a == "--days":
                    days = max(1, int(args[i + 1]))
                elif a == "--max":
                    max_results = max(1, int(args[i + 1]))
                else:
                    projects_dir = Path(args[i + 1]).expanduser()
            except ValueError:
                print(f"[Error] Invalid {a} value: {args[i + 1]}")
                return None
            i += 1
        i += 1
    return days, max_results, as_json, projects_dir


def replay_cli(args, projects_dir, now=None):
    """`measure.py coverage-replay [--days N] [--max N] [--projects-dir PATH] [--json]`."""
    parsed = _parse_args(args, projects_dir)
    if parsed is None:
        return 1
    days, max_results, as_json, projects_dir = parsed
    summary = replay(projects_dir, days=days, max_results=max_results, now=now)
    if as_json:
        print(json.dumps(summary, indent=2))
        return 0 if summary.get("available") else 1
    note = ""
    if summary.get("available"):
        note = (f"Replayed {summary['results']:,} Bash results from {summary['files']:,} transcripts "
                f"in the last {days} days (read-only; nothing stored)."
                + (" Stopped at the --max limit; newest transcripts first." if summary["truncated"] else ""))
    print(format_table(summary, note))
    return 0 if summary.get("available") else 1


def refetch_cli(args, projects_dir, now=None):
    """`measure.py file-refetch [--days N] [--projects-dir PATH] [--json]`: re-fetch metric only."""
    parsed = _parse_args(args, projects_dir)
    if parsed is None:
        return 1
    days, _max, as_json, projects_dir = parsed
    out = refetch_only(projects_dir, days=days, now=now)
    if as_json:
        print(json.dumps(out, indent=2))
    elif not out.get("available"):
        print(headline(out))
    else:
        from . import file_refetch
        print("\n".join([f"Read {out['files']:,} transcripts from the last {days} days (read-only; nothing stored)."]
                        + file_refetch.format_lines(out)))
    return 0 if out.get("available") else 1
