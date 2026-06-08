#!/usr/bin/env python3
"""Token Optimizer - first-read shadow BACKFILL analyzer.

Replays existing Claude Code (and Codex) transcripts to answer the R9 promotion
question from REAL history instead of waiting weeks for live shadow data:

  For large structure-supported whole-file reads, what would a skeleton have
  saved, and how often did the model edit that same file SOON after reading it
  (the "it needed the full file" signal)?

A transcript already contains both halves: the Read tool_result holds the file
content as it entered context, and later Edit/Write/MultiEdit tool_use blocks
name the file. So `turns_until_edit` -- impossible to know live at read time --
is directly observable in history.

Output: a per-cohort report (language x size-band) of read count, edit-within-N
rate, and average would-be skeleton ratio. With --write-cohorts it persists the
cohorts that pass the gate (edit-rate < threshold AND enough samples) to a gate
file the active first-read path consults. With --write-events it writes
opportunity-tier rows so the dashboard coverage panel reflects history.

This is an offline analysis tool, NOT a hot-path hook: it may import freely.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from structure_map import (  # noqa: E402
    detect_structure_language,
    is_structure_supported_file,
    summarize_code_file,
)

try:  # noqa: E402
    from token_estimate import estimate_tokens
except Exception:  # pragma: no cover
    def estimate_tokens(text: str) -> int:
        return max(1, len(text) // 4) if text else 0

# Gate parameters (mirrors read_cache shadow window + measure promotion gate).
MIN_BYTES = 16 * 1024
MAX_BYTES = 2 * 1024 * 1024
MIN_RATIO = 0.40
DEFAULT_EDIT_WINDOW_TURNS = 5
PROMOTION_EDIT_RATE = 0.15
PROMOTION_MIN_SAMPLES = 20
PROMOTION_MIN_SESSIONS = 5  # spread across >=N distinct sessions, not one loop

_LINE_PREFIX = re.compile(r"^\d+\t")
# Claude Code's Read render is "N\t<line>" (1-indexed, no leading space). Strip
# exactly that; a leading \s* would clip the first column of legit TSV content.


def _strip_line_numbers(text: str) -> str:
    out = []
    for line in text.splitlines():
        out.append(_LINE_PREFIX.sub("", line, count=1))
    return "\n".join(out)


def _norm(path: str) -> str:
    try:
        return os.path.normpath(path)
    except Exception:
        return path


def _result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


def _size_band(n_bytes: int) -> str:
    # MUST match read_cache._first_read_size_band.
    kb = n_bytes / 1024
    if kb < 16:
        return "<16KB"
    if kb < 64:
        return "16-64KB"
    if kb < 256:
        return "64-256KB"
    if kb < 1024:
        return "256KB-1MB"
    return "1-2MB"


def iter_transcripts(projects_dir: Path):
    for p in projects_dir.rglob("*.jsonl"):
        if p.is_file():
            yield p


def replay_session(path: Path):
    """Yield (kind, file_path, turn_index, payload) in transcript order.

    kind is "read" (payload=reconstructed content) or "edit" (payload=None).
    turn_index counts assistant messages so "turns until edit" is well-defined.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return

    tool_name_by_id: dict[str, str] = {}
    read_path_by_id: dict[str, str] = {}
    pending_reads: dict[str, tuple] = {}  # tool_use_id -> (file_path, turn_index)
    turn = 0

    for ln in lines:
        try:
            ev = json.loads(ln)
        except (json.JSONDecodeError, TypeError):
            continue
        msg = ev.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if ev.get("type") == "assistant":
            turn += 1
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "tool_use":
                name = block.get("name")
                tid = block.get("id")
                inp = block.get("input") or {}
                if tid:
                    tool_name_by_id[tid] = name
                if name == "Read":
                    # whole-file reads only (no offset/limit), matching live shadow
                    if inp.get("offset") or inp.get("limit"):
                        continue
                    fp = inp.get("file_path")
                    if fp and tid:
                        read_path_by_id[tid] = _norm(fp)
                        pending_reads[tid] = (_norm(fp), turn)
                elif name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                    fp = inp.get("file_path")
                    if fp:
                        yield ("edit", _norm(fp), turn, None)
            elif bt == "tool_result":
                tuid = block.get("tool_use_id")
                if tuid in pending_reads and tool_name_by_id.get(tuid) == "Read":
                    fp, rturn = pending_reads.pop(tuid)
                    raw = _result_text(block)
                    if raw:
                        yield ("read", fp, rturn, _strip_line_numbers(raw))


def analyze(projects_dir: Path, edit_window: int, limit: int | None):
    """Aggregate first-read->edit cohorts across all transcripts.

    Counts only the FIRST read of each file per session — exactly the occasion
    active mode would fire on. Re-reads (e.g. hooks re-reading CLAUDE.md every
    session) are excluded so they cannot inflate the denominator and understate
    the edit-rate. Also tracks distinct sessions per cohort for a robustness gate.
    """
    cohorts: dict[tuple, dict] = defaultdict(
        lambda: {"reads": 0, "edited_within": 0, "edited_ever": 0,
                 "ratio_sum": 0.0, "would_be_tokens": 0, "sessions": set()}
    )
    totals = {"sessions": 0, "reads_seen": 0, "first_reads": 0, "eligible": 0,
              "skipped_small": 0, "skipped_big": 0, "skipped_unsupported": 0,
              "skipped_ineligible": 0}

    processed = 0
    for path in iter_transcripts(projects_dir):
        if limit and processed >= limit:
            break
        # Collect this session's reads and edits in order.
        reads = []   # (file_path, turn, content)
        edits = []   # (file_path, turn)
        for kind, fp, turn, payload in replay_session(path):
            if kind == "read":
                reads.append((fp, turn, payload))
            else:
                edits.append((fp, turn))
        if not reads:
            continue
        totals["sessions"] += 1
        processed += 1
        sid = path.stem  # session uuid, for distinct-session counting

        edits_by_path: dict[str, list] = defaultdict(list)
        for fp, turn in edits:
            edits_by_path[fp].append(turn)

        # First read of each file in this session only.
        first_read: dict[str, tuple] = {}
        for fp, rturn, content in reads:
            totals["reads_seen"] += 1
            if fp not in first_read:
                first_read[fp] = (rturn, content)

        for fp, (rturn, content) in first_read.items():
            totals["first_reads"] += 1
            nbytes = len(content.encode("utf-8", errors="replace"))
            if nbytes < MIN_BYTES:
                totals["skipped_small"] += 1
                continue
            if nbytes > MAX_BYTES:
                totals["skipped_big"] += 1
                continue
            if not is_structure_supported_file(fp):
                totals["skipped_unsupported"] += 1
                continue
            try:
                result = summarize_code_file(fp, content=content, file_size_bytes=nbytes)
            except Exception:
                continue
            if not result.eligible:
                totals["skipped_ineligible"] += 1
                continue
            orig = int(result.file_tokens_est or estimate_tokens(content))
            skel = int(result.replacement_tokens_est or 0)
            if orig <= 0:
                continue
            ratio = 1.0 - (skel / orig)
            if ratio < MIN_RATIO:
                totals["skipped_ineligible"] += 1
                continue

            totals["eligible"] += 1
            lang = detect_structure_language(fp) or "unknown"
            band = _size_band(nbytes)
            c = cohorts[(lang, band)]
            c["reads"] += 1
            c["sessions"].add(sid)
            c["ratio_sum"] += ratio
            c["would_be_tokens"] += (orig - skel)
            later = [t for t in edits_by_path.get(fp, []) if t > rturn]
            if later:
                c["edited_ever"] += 1
                if min(later) - rturn <= edit_window:
                    c["edited_within"] += 1

    return cohorts, totals


def build_report(cohorts, totals, edit_window):
    rows = []
    for (lang, band), c in sorted(cohorts.items(), key=lambda kv: -kv[1]["reads"]):
        reads = c["reads"]
        n_sessions = len(c["sessions"])
        edit_rate = (c["edited_within"] / reads) if reads else 0.0
        avg_ratio = (c["ratio_sum"] / reads) if reads else 0.0
        passes = (
            reads >= PROMOTION_MIN_SAMPLES
            and n_sessions >= PROMOTION_MIN_SESSIONS
            and edit_rate < PROMOTION_EDIT_RATE
        )
        rows.append({
            "language": lang,
            "size_band": band,
            "first_reads": reads,
            "sessions": n_sessions,
            "edited_within_n": c["edited_within"],
            "edited_ever": c["edited_ever"],
            "edit_rate_pct": round(100 * edit_rate, 1),
            "avg_would_be_ratio_pct": round(100 * avg_ratio, 1),
            "would_be_tokens": c["would_be_tokens"],
            "promotion_ready": passes,
        })
    return {
        "edit_window_turns": edit_window,
        "metric": "first-read-only per session",
        "promotion_gate": {"edit_rate_pct": 100 * PROMOTION_EDIT_RATE,
                           "min_samples": PROMOTION_MIN_SAMPLES,
                           "min_sessions": PROMOTION_MIN_SESSIONS},
        "totals": totals,
        "cohorts": rows,
    }


def print_report(report):
    t = report["totals"]
    print("\n  First-read shadow BACKFILL (from real transcripts)")
    print("  " + "=" * 66)
    print(f"  Sessions scanned: {t['sessions']:,}   reads seen: {t['reads_seen']:,}   "
          f"first-reads: {t['first_reads']:,}")
    print(f"  Eligible (large, structure-supported, >={int(MIN_RATIO*100)}% win): {t['eligible']:,}")
    print(f"  Skipped — small: {t['skipped_small']:,}  too big: {t['skipped_big']:,}  "
          f"unsupported: {t['skipped_unsupported']:,}  low-win/ineligible: {t['skipped_ineligible']:,}")
    g = report["promotion_gate"]
    print(f"\n  Metric: {report['metric']}")
    print(f"  Promotion gate: edit-rate < {g['edit_rate_pct']:.0f}% within "
          f"{report['edit_window_turns']} turns AND >= {g['min_samples']} first-reads "
          f"across >= {g['min_sessions']} sessions\n")
    if not report["cohorts"]:
        print("  No eligible cohorts found.")
        return
    print(f"  {'language':10s} {'size':12s} {'f-reads':>7s} {'sess':>5s} {'edit%':>6s} "
          f"{'ratio%':>7s} {'wouldbe tok':>12s}  gate")
    print("  " + "-" * 72)
    for r in report["cohorts"]:
        flag = ("PROMOTE" if r["promotion_ready"]
                else ("hold" if r["first_reads"] >= PROMOTION_MIN_SAMPLES else "low-n"))
        print(f"  {r['language']:10s} {r['size_band']:12s} {r['first_reads']:>7d} "
              f"{r['sessions']:>5d} {r['edit_rate_pct']:>5.1f}% {r['avg_would_be_ratio_pct']:>6.1f}% "
              f"{r['would_be_tokens']:>12,}  {flag}")
    print()


def main(argv=None):
    ap = argparse.ArgumentParser(description="First-read shadow backfill analyzer")
    ap.add_argument("--projects-dir", default=str(Path.home() / ".claude" / "projects"))
    ap.add_argument("--edit-window", type=int, default=DEFAULT_EDIT_WINDOW_TURNS)
    ap.add_argument("--limit", type=int, default=None,
                    help="max sessions WITH reads to process (for a quick sample)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    projects_dir = Path(args.projects_dir)
    if not projects_dir.exists():
        print(f"[backfill] projects dir not found: {projects_dir}", file=sys.stderr)
        return 1
    cohorts, totals = analyze(projects_dir, args.edit_window, args.limit)
    report = build_report(cohorts, totals, args.edit_window)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
