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

from runtime_env import claude_home  # noqa: E402
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

# Interpolated cohorts (F5 / T9): cohorts that graduate to active on a thin
# sample because the SAME language already passed the full gate in an ADJACENT
# size band. Mirrors read_cache._INTERPOLATED_COHORTS + measure._INTERPOLATED_
# COHORTS. The backfill ASSERTS (assert_interpolated_cohorts_supported) that each
# one's language really does have a passing adjacent band in the live report, so
# an interpolated promotion can never rest on a language that itself never passed.
_INTERPOLATED_COHORTS = frozenset({
    ("typescript", "64-256KB"),
})
# Adjacency in band space (ordered): a cohort's adjacent bands are its neighbors.
_SIZE_BANDS_ORDERED = ("<16KB", "16-64KB", "64-256KB", "256KB-1MB", "1-2MB")


def _adjacent_bands(band: str):
    """Return the immediately-smaller and immediately-larger band names."""
    try:
        i = _SIZE_BANDS_ORDERED.index(band)
    except ValueError:
        return ()
    out = []
    if i > 0:
        out.append(_SIZE_BANDS_ORDERED[i - 1])
    if i < len(_SIZE_BANDS_ORDERED) - 1:
        out.append(_SIZE_BANDS_ORDERED[i + 1])
    return tuple(out)


def assert_interpolated_cohorts_supported(report) -> list:
    """F5: every interpolated cohort's language must pass in an ADJACENT band.

    Scans the backfill report's cohort rows; for each interpolated cohort
    (lang, band) verifies that (lang, adjacent_band) appears with
    promotion_ready=True for at least one adjacent band. Returns the list of
    UNSUPPORTED interpolated cohorts (empty == all supported). Callers may assert
    on the empty-ness; the report carries it as `interpolated_unsupported` so the
    CLI surfaces a drift without crashing a long backfill run.
    """
    by_cohort = {
        (r["language"], r["size_band"]): r for r in report.get("cohorts", [])
    }
    unsupported = []
    for lang, band in sorted(_INTERPOLATED_COHORTS):
        ok = any(
            by_cohort.get((lang, adj), {}).get("promotion_ready")
            for adj in _adjacent_bands(band)
        )
        if not ok:
            unsupported.append([lang, band])
    return unsupported

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
    report = {
        "edit_window_turns": edit_window,
        "metric": "first-read-only per session",
        "promotion_gate": {"edit_rate_pct": 100 * PROMOTION_EDIT_RATE,
                           "min_samples": PROMOTION_MIN_SAMPLES,
                           "min_sessions": PROMOTION_MIN_SESSIONS},
        "totals": totals,
        "cohorts": rows,
    }
    # F5: surface any interpolated cohort whose language lacks a passing adjacent
    # band so the CLI/test catches an unsupported interpolation (arch invariant).
    report["interpolated_unsupported"] = assert_interpolated_cohorts_supported(report)
    return report


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


# ---------------------------------------------------------------------------
# Agent/Task result compression backfill (WS4).
#
# Agent (sub-agent) results enter the parent context whole and are large
# (a typical 30d sample: ~1.3M tokens of Agent results). The proposed active treatment is
# a progressive-disclosure replacement: keep HEAD (first _AGENT_HEAD_CHARS) +
# TAIL (last _AGENT_TAIL_CHARS) inline plus an expand pointer, archiving the full
# result. The MIDDLE is elided. Because sub-agents often put findings in the
# middle (head = preamble, tail = usage/continuation block), the HARM PROXY is
# mandatory: how often does the parent's NEXT assistant turn quote text that
# lives in the would-be-elided middle (>=_HARM_MIN_SPAN-char verbatim span)? If
# that rate is >=15% we ship measure-only; below 15% we ship active. The data
# decides, per the configured exception rule.
# ---------------------------------------------------------------------------

# C1: single source of truth — import the runtime gate constants from
# archive_result (the hot-path hook that actually performs the head+tail+pointer
# treatment) so the backfill measures EXACTLY what production would gate on.
# Both now use CHAR semantics (the runtime gates on char count, not byte count),
# so the size floor name carries CHARS. Fail-open to local literals if the
# import is unavailable (e.g. a partial checkout running the backfill alone).
try:
    from archive_result import (
        _AGENT_RESULT_MIN_CHARS,
        _AGENT_RESULT_HEAD_CHARS as _AGENT_HEAD_CHARS,
        _AGENT_RESULT_TAIL_CHARS as _AGENT_TAIL_CHARS,
    )
except Exception:  # pragma: no cover - standalone fallback
    _AGENT_RESULT_MIN_CHARS = 8 * 1024
    _AGENT_HEAD_CHARS = 2000
    _AGENT_TAIL_CHARS = 2000
_HARM_MIN_SPAN = 20                 # min verbatim span length to count as a "quote"
_HARM_MAX_SPANS_CHECK = 400         # cap spans scanned per result (perf)


def _verbatim_overlap_in_middle(middle: str, next_text: str) -> bool:
    """True iff next_text contains a >=_HARM_MIN_SPAN verbatim span from middle.

    Scans candidate spans from the middle (the would-be-elided region) and checks
    membership in the parent's next assistant turn. Whitespace-run starts are used
    as span anchors so we test meaningful token sequences, not arbitrary offsets.
    Bounded by _HARM_MAX_SPANS_CHECK for performance on large middles.
    """
    if not middle or not next_text or len(middle) < _HARM_MIN_SPAN:
        return False
    # Anchor candidate spans at word boundaries in the middle.
    anchors = [0]
    for m in re.finditer(r"\S{4,}", middle):
        anchors.append(m.start())
        if len(anchors) >= _HARM_MAX_SPANS_CHECK:
            break
    for start in anchors:
        span = middle[start:start + _HARM_MIN_SPAN]
        if len(span) < _HARM_MIN_SPAN:
            continue
        if span.strip() and span in next_text:
            return True
    return False


def replay_agent_results(path: Path):
    """Yield (result_text, next_assistant_text) for EVERY Agent/Task result.

    next_assistant_text is the concatenation of text blocks in the parent's NEXT
    assistant message after the tool_result (the turn that acts on the result).

    C3: yields every agent result regardless of size; the caller applies the
    >=_AGENT_RESULT_MIN_CHARS gate so it can count the full pre-gate pool in
    totals["results"]. The gate uses CHAR length (C1 — same semantics the
    runtime hook gates on), not byte length.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return

    events = []
    for ln in lines:
        try:
            events.append(json.loads(ln))
        except (json.JSONDecodeError, TypeError):
            continue

    agent_ids: set[str] = set()
    for idx, ev in enumerate(events):
        msg = ev.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") in ("Task", "Agent"):
                if block.get("id"):
                    agent_ids.add(block["id"])
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in agent_ids:
                txt = _result_text(block)
                if not txt:
                    continue
                # Find the NEXT assistant message after this event.
                next_text = ""
                for j in range(idx + 1, len(events)):
                    nmsg = events[j].get("message")
                    if events[j].get("type") != "assistant" or not isinstance(nmsg, dict):
                        continue
                    ncontent = nmsg.get("content")
                    if isinstance(ncontent, list):
                        next_text = "".join(
                            b.get("text", "") for b in ncontent
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    break
                yield (txt, next_text)


def analyze_agent_results(projects_dir: Path, limit: int | None):
    """Aggregate the Agent-result pool + harm proxy across all transcripts."""
    from token_estimate import estimate_tokens as _est
    totals = {
        "sessions": 0, "results": 0, "results_over_8kb": 0,
        "orig_tokens": 0, "would_be_tokens": 0,
        "harm_hits": 0, "harm_checked": 0,
        "no_next_turn": 0,
    }
    processed = 0
    for path in iter_transcripts(projects_dir):
        if limit and processed >= limit:
            break
        saw = False
        for result_text, next_text in replay_agent_results(path):
            # C3: count EVERY agent result in the pool, pre-gate, so the JSON's
            # "results" total is truthful (it was previously initialized but
            # never incremented). The >=8KB gate decides the candidate subset.
            totals["results"] += 1
            if len(result_text) < _AGENT_RESULT_MIN_CHARS:
                continue
            saw = True
            totals["results_over_8kb"] += 1
            orig_tok = _est(result_text)
            head = result_text[:_AGENT_HEAD_CHARS]
            tail = result_text[-_AGENT_TAIL_CHARS:] if len(result_text) > _AGENT_TAIL_CHARS else ""
            middle = result_text[_AGENT_HEAD_CHARS: len(result_text) - _AGENT_TAIL_CHARS]
            would_be = _est(head + tail)
            totals["orig_tokens"] += orig_tok
            totals["would_be_tokens"] += max(0, orig_tok - would_be)
            # Harm proxy: only meaningful when there IS a next turn and a middle.
            if not next_text:
                totals["no_next_turn"] += 1
                continue
            if not middle.strip():
                continue
            totals["harm_checked"] += 1
            if _verbatim_overlap_in_middle(middle, next_text):
                totals["harm_hits"] += 1
        if saw:
            totals["sessions"] += 1
            processed += 1
    return totals


def build_agent_report(totals):
    checked = totals["harm_checked"]
    harm_rate = (totals["harm_hits"] / checked) if checked else 0.0
    return {
        "metric": "agent/task result compression (head+tail+pointer)",
        "head_chars": _AGENT_HEAD_CHARS,
        "tail_chars": _AGENT_TAIL_CHARS,
        "min_chars": _AGENT_RESULT_MIN_CHARS,  # C1: char-semantics gate (shared)
        "harm_span_min_chars": _HARM_MIN_SPAN,
        "totals": totals,
        "harm_rate_pct": round(100 * harm_rate, 1),
        "harm_gate_pct": 100 * PROMOTION_EDIT_RATE,
        "ship_active": harm_rate < PROMOTION_EDIT_RATE,
    }


def print_agent_report(report):
    t = report["totals"]
    print("\n  Agent/Task result compression BACKFILL (from real transcripts)")
    print("  " + "=" * 66)
    print(f"  Sessions with >8KB agent results: {t['sessions']:,}   "
          f"results: {t['results_over_8kb']:,}")
    print(f"  Original tokens: {t['orig_tokens']:,}   "
          f"would-be saved (head+tail): {t['would_be_tokens']:,}")
    print(f"  Harm proxy: next-turn verbatim quote (>= {report['harm_span_min_chars']} chars) "
          f"from the elided MIDDLE")
    print(f"    checked: {t['harm_checked']:,}   hits: {t['harm_hits']:,}   "
          f"no-next-turn: {t['no_next_turn']:,}")
    verdict = "SHIP ACTIVE" if report["ship_active"] else "MEASURE-ONLY"
    print(f"    harm rate: {report['harm_rate_pct']:.1f}%  "
          f"(gate <{report['harm_gate_pct']:.0f}% — {verdict})")
    print()


def main(argv=None):
    ap = argparse.ArgumentParser(description="First-read shadow backfill analyzer")
    ap.add_argument("--projects-dir", default=str(claude_home() / "projects"))
    ap.add_argument("--edit-window", type=int, default=DEFAULT_EDIT_WINDOW_TURNS)
    ap.add_argument("--limit", type=int, default=None,
                    help="max sessions WITH reads to process (for a quick sample)")
    ap.add_argument("--agent-results", action="store_true",
                    help="run the WS4 Agent/Task result compression + harm-proxy backfill")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    projects_dir = Path(args.projects_dir)
    if not projects_dir.exists():
        print(f"[backfill] projects dir not found: {projects_dir}", file=sys.stderr)
        return 1
    if args.agent_results:
        totals = analyze_agent_results(projects_dir, args.limit)
        report = build_agent_report(totals)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print_agent_report(report)
        return 0
    cohorts, totals = analyze(projects_dir, args.edit_window, args.limit)
    report = build_report(cohorts, totals, args.edit_window)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
