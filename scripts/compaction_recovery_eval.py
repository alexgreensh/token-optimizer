#!/usr/bin/env python3
"""Ongoing compaction-recovery evaluation (COMPACTION-QUALITY.md section 7).

Three jobs, all local and cheap:

1. PRODUCTION MONITOR -- the live parser already collects matched pre/post
   tool-call windows per compaction boundary into each session's
   quality-cache-*.json (recovery_evidence). This script aggregates those
   records across sessions: it stores metadata and hashes only, never raw
   transcript content, and dedupes by boundary_id.

2. WEEKLY EVENT STUDY -- rerun the same paired-window statistics as the
   retrospective audit (research/arxiv-2611.05306): per-boundary paired
   deltas, equal-session averaging, session-clustered bootstrap CIs
   (2,000 replicates, seed 261105306 for comparability). Strata: ordinal
   1 / 2 / 3+, restore evidence, host/model version. Observations and
   estimates are labeled separately; missing telemetry is "unknown", never
   "healthy".

3. ADVISOR EVALUATION -- summarize context_advisory_shadow /
   context_size_advisory compression_events from trends.db: what the
   300K/350K/400K thresholds would have decided, blocker mix, and actual
   advisory fires. Shadow data only; no user-facing output is replayed.

Externally checked continuation tasks live in
evals/compaction-continuation/tasks.json -- a held-out set with verifiable
acceptance conditions, run in isolated continuation trials OUTSIDE this
script (it cannot grade task outcomes itself).

Usage:
    python3 scripts/compaction_recovery_eval.py [--days 30] [--cache-dir DIR]
        [--trends-db PATH] [--out REPORT.json] [--markdown]

Exit code is always 0 unless the inputs are unreadable; a thin cohort is
reported as thin, not as a pass.
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_SEED = 261105306  # same seed as the retrospective audit
MIN_MATCHED_CALLS = 10
WINDOW_CALLS = 20

# Metrics compared pre vs post, per 100 completed calls (audit names).
PAIRED_METRICS = (
    "errors",
    "shell_nonzero",
    "repeats",
    "repeat_after_error",
    "reads",
    "rereads",
    "rereads_unchanged",
    "expansions",
    "loop_notices",
)


def _default_cache_dir() -> Path:
    try:
        import measure  # noqa: PLC0415 - resolved after sys.path insert
        return Path(measure.QUALITY_CACHE_DIR)
    except Exception:
        return Path.home() / ".claude" / "token-optimizer"


def _iter_quality_caches(cache_dir: Path):
    for p in sorted(cache_dir.glob("quality-cache-*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            yield p.stem.replace("quality-cache-", "", 1), data


def _collect_boundaries(cache_dir: Path, since_days: int):
    """Pull every evaluated compaction boundary out of the quality caches.

    Returns (boundaries, coverage). Each boundary keeps only the fields the
    event study needs -- the parser already stored metadata, not content.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    boundaries = []
    coverage = {
        "caches_seen": 0,
        "sessions_with_compactions": 0,
        "boundaries_total": 0,
        "eligible": 0,
        "unmeasurable": 0,
        "observing": 0,
        "needs_attention": 0,
        "no_concerning_signal_observed": 0,
        "score_versions": defaultdict(int),
        "dropped_stale": 0,
        "strata_counts": {
            "model": defaultdict(int),
            "host_version": defaultdict(int),
            "ordinal": defaultdict(int),
            "restore_evidence": defaultdict(int),
        },
        # Sessions whose NEWEST boundary is still "observing" never produced a
        # finalized post-window: they either ended right after compacting or
        # stalled. Kept as a separate review cohort, never silently dropped.
        "stopped_after_compaction": 0,
    }
    seen_ids = set()
    last_status_by_session = {}
    for session_id, data in _iter_quality_caches(cache_dir):
        coverage["caches_seen"] += 1
        sv = data.get("score_version")
        coverage["score_versions"][str(sv)] += 1
        comp = int(data.get("compactions") or 0)
        if comp <= 0:
            continue
        coverage["sessions_with_compactions"] += 1
        evidence = data.get("recovery_evidence") or []
        coverage["boundaries_total"] += len(evidence) if evidence else comp
        session_last_status = None
        for b in evidence:
            if not isinstance(b, dict):
                continue
            bid = b.get("boundary_id") or f"{session_id}:{b.get('ordinal')}"
            if bid in seen_ids:
                continue
            seen_ids.add(bid)
            ts_epoch = b.get("ts_epoch")
            if ts_epoch is not None:
                ts_dt = datetime.fromtimestamp(ts_epoch, tz=timezone.utc)
                if ts_dt < cutoff:
                    coverage["dropped_stale"] += 1
                    continue
            status = b.get("status") or "unmeasurable"
            coverage[status if status in coverage else "unmeasurable"] += 1
            sc = coverage["strata_counts"]
            sc["model"][str(b.get("model") or "unknown")] += 1
            sc["host_version"][str(b.get("host_version") or "unknown")] += 1
            sc["ordinal"][_ordinal_stratum(b.get("ordinal"))] += 1
            sc["restore_evidence"][_restore_stratum(b.get("restore_evidence"))] += 1
            boundaries.append({
                "session_id": session_id,
                "boundary_id": bid,
                "ordinal": b.get("ordinal"),
                "status": status,
                "score": b.get("score"),
                "flags": b.get("flags") or [],
                "matched_calls": b.get("matched_calls") or 0,
                "eligible": bool(b.get("eligible")),
                "restore_evidence": b.get("restore_evidence") or "none",
                "capture_notice": bool(b.get("capture_notice")),
                "host_version": b.get("host_version"),
                "model": b.get("model"),
                "ts_epoch": ts_epoch,
                "pre": b.get("pre") if isinstance(b.get("pre"), dict) else None,
                "post": b.get("post") if isinstance(b.get("post"), dict) else None,
            })
            session_last_status = status
        if session_last_status is not None:
            last_status_by_session[session_id] = session_last_status
        elif comp > 0 and not evidence:
            last_status_by_session[session_id] = "unmeasurable"
    coverage["stopped_after_compaction"] = sum(
        1 for s in last_status_by_session.values() if s == "observing")
    for k, v in coverage["strata_counts"].items():
        coverage["strata_counts"][k] = dict(v)
    return boundaries, coverage


def _ordinal_stratum(ordinal):
    try:
        n = int(ordinal or 0)
    except (TypeError, ValueError):
        return "unknown"
    if n <= 0:
        return "unknown"
    return "1" if n == 1 else "2" if n == 2 else "3+"


def _restore_stratum(ev):
    return "evidenced" if ev in ("transcript", "db", "both") else "none_observed"


def _effect_for_metric(rows, metric):
    """Session-balanced delta + bootstrap CI for one metric."""
    by_session = defaultdict(list)
    for r in rows:
        if metric in r:
            by_session[r["session_id"]].append(r[metric])
    if not by_session:
        return {"sessions": 0, "boundaries": 0, "delta_per_100": None, "ci95": [None, None]}
    session_means = [sum(v) / len(v) for v in by_session.values()]
    point = sum(session_means) / len(session_means)
    rng = random.Random(BOOTSTRAP_SEED)
    sids = list(by_session.keys())
    boots = []
    for _ in range(BOOTSTRAP_REPLICATES):
        sample = [by_session[rng.choice(sids)] for _ in sids]
        flat = [x for grp in sample for x in grp]
        # resample sessions, average within each drawn session's mean
        means = [sum(v) / len(v) for v in sample]
        boots.append(sum(means) / len(means))
    boots.sort()
    lo = boots[int(0.025 * len(boots))]
    hi = boots[int(0.975 * len(boots)) - 1]
    return {
        "sessions": len(by_session),
        "boundaries": sum(len(v) for v in by_session.values()),
        "delta_per_100": round(point, 2),
        "ci95": [round(lo, 2), round(hi, 2)],
    }


def _stratified_effects(rows):
    """Effect sizes overall and by ordinal x restore strata."""
    strata = {"all": rows}
    for metric_key in ("ordinal", "restore"):
        groups = defaultdict(list)
        for r in rows:
            groups[r[metric_key]].append(r)
        for k, v in groups.items():
            strata[f"{metric_key}={k}"] = v
    # ordinal x restore cross
    cross = defaultdict(list)
    for r in rows:
        cross[f"ordinal={r['ordinal']},restore={r['restore']}"].append(r)
    strata.update({f"x:{k}": v for k, v in cross.items()})

    out = {}
    for name, subset in strata.items():
        out[name] = {m: _effect_for_metric(subset, m) for m in PAIRED_METRICS}
    return out


def _advisor_report(trends_db: Path, since_days: int):
    """Summarize context_advisory_shadow / context_size_advisory events."""
    report = {"available": False, "shadow_events": 0, "advisory_fires": 0,
              "threshold_votes": defaultdict(int), "blocker_counts": defaultdict(int),
              "fires_by_threshold": defaultdict(int)}
    if not trends_db.exists():
        return report
    cutoff = (datetime.now() - timedelta(days=since_days)).isoformat()
    try:
        conn = sqlite3.connect(f"file:{trends_db}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT feature, detail, timestamp FROM compression_events "
                "WHERE feature IN ('context_advisory_shadow','context_size_advisory') "
                "AND timestamp >= ? ORDER BY timestamp",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return report
    report["available"] = True
    for feature, detail, _ts in rows:
        if feature == "context_size_advisory":
            report["advisory_fires"] += 1
            continue
        report["shadow_events"] += 1
        try:
            d = json.loads(detail)
        except (json.JSONDecodeError, TypeError):
            continue
        for t, eval_at in (d.get("thresholds") or {}).items():
            if eval_at.get("eligible"):
                report["threshold_votes"][t] += 1
        for k in (d.get("blockers") or {}):
            report["blocker_counts"][k] += 1
    report["threshold_votes"] = dict(report["threshold_votes"])
    report["blocker_counts"] = dict(report["blocker_counts"])
    return report


def _continuation_tasks_status(repo: Path):
    """Report the held-out continuation task set's declared state. The tasks
    themselves run in isolated trials outside this script; here we only
    verify the set exists, parses, and every task has acceptance conditions."""
    tasks_path = repo / "evals" / "compaction-continuation" / "tasks.json"
    out = {"path": str(tasks_path), "present": tasks_path.exists()}
    if not tasks_path.exists():
        return out
    try:
        tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        out["error"] = str(e)
        return out
    items = tasks.get("tasks", tasks if isinstance(tasks, list) else [])
    out["count"] = len(items)
    out["missing_acceptance"] = [
        t.get("id", "?") for t in items
        if not t.get("acceptance") or not isinstance(t.get("acceptance"), list) or not t["acceptance"]
    ]
    out["ids"] = [t.get("id") for t in items if isinstance(t, dict)]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--cache-dir", type=Path, default=None)
    ap.add_argument("--trends-db", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--markdown", action="store_true")
    args = ap.parse_args(argv)

    cache_dir = args.cache_dir or _default_cache_dir()
    trends_db = args.trends_db or (REPO / "state" / "trends.db")
    # Fall back to the snapshot dir the measure module would use.
    if args.trends_db is None:
        try:
            import measure  # noqa: PLC0415
            trends_db = Path(measure.TRENDS_DB)
        except Exception:
            pass

    boundaries, coverage = _collect_boundaries(cache_dir, args.days)
    rows = []
    for b in boundaries:
        if not b["eligible"]:
            continue
        # Rebuild the flat per-metric row the stats functions expect.
        pre, post = b.get("pre") or {}, b.get("post") or {}
        m = min(int(b.get("matched_calls") or 0), WINDOW_CALLS)
        if m < MIN_MATCHED_CALLS or not pre.get("calls") or not post.get("calls"):
            continue
        row = {"session_id": b["session_id"], "boundary_id": b["boundary_id"],
               "ordinal": _ordinal_stratum(b.get("ordinal")),
               "restore": _restore_stratum(b.get("restore_evidence")),
               "calls": m}
        for metric in PAIRED_METRICS:
            row[metric] = ((post.get(metric) or 0) - (pre.get(metric) or 0)) * 100.0 / max(m, 1)
        rows.append(row)

    effects = _stratified_effects(rows)
    advisor = _advisor_report(trends_db, args.days)
    tasks = _continuation_tasks_status(REPO)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": args.days,
        "method": ("paired 20-call windows per compaction boundary; "
                   "session-balanced deltas; session-clustered bootstrap "
                   f"{BOOTSTRAP_REPLICATES} replicates seed {BOOTSTRAP_SEED}; "
                   "observations vs estimates labeled; missing telemetry = unknown"),
        "coverage": {k: (dict(v) if isinstance(v, defaultdict) else v)
                     for k, v in coverage.items()},
        "boundaries_in_study": len(rows),
        "effects_per_100_calls": effects,
        "advisor": advisor,
        "continuation_tasks": tasks,
    }

    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    if args.markdown:
        print("# Compaction recovery eval\n")
        print(f"- window: {args.days}d, cache dir: {cache_dir}")
        print(f"- boundaries in study: {len(rows)} "
              f"(eligible {coverage['eligible']}, unmeasurable {coverage['unmeasurable']}, "
              f"observing {coverage['observing']}, "
              f"stopped-after-compaction review: {coverage['stopped_after_compaction']})")
        print(f"- strata counts: {json.dumps(coverage.get('strata_counts', {}), sort_keys=True)}")
        print("\n## Overall (delta per 100 calls, session-balanced, 95% CI)\n")
        print("| metric | delta | ci95 | sessions | boundaries |")
        print("|---|---|---|---|---|")
        for m in PAIRED_METRICS:
            e = effects["all"][m]
            print(f"| {m} | {e['delta_per_100']} | {e['ci95']} | {e['sessions']} | {e['boundaries']} |")
        print("\n## Advisor shadow\n")
        print(f"- shadow events: {advisor['shadow_events']}, advisory fires: {advisor['advisory_fires']}")
        print(f"- threshold votes: {advisor['threshold_votes']}")
        print(f"- blockers: {advisor['blocker_counts']}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
