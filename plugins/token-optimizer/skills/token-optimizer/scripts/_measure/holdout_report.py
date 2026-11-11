"""Control-group comparison for the holdout (see holdout.py). Read-only.

Two unpaired arms of sessions: control (hooks ran in shadow) and treatment
(hooks acted). Nothing is shown until each arm has at least ``MIN_SESSIONS``
sessions of at least ``MIN_CALLS`` API calls, because anything smaller is noise
presented as a result. Intervals: a Wilson interval for a share of sessions,
and a plain percentile bootstrap (fixed seed, resampling sessions within each
arm independently) for means and for the treatment-minus-control difference.
The arms are not paired, so a paired bootstrap does not apply.

Metrics per arm: API calls per session; input + output tokens per call (a
dollar figure needs the pricing layer and is a follow-up, labelled as tokens
here); the share of sessions with at least one archive re-expand. Only counts
and aggregates leave this module.
"""
import math
import random
import sqlite3
import statistics
from pathlib import Path

MIN_SESSIONS = 30
MIN_CALLS = 10
BOOTSTRAP_RESAMPLES = 2000
SEED = 1729
CONFIDENCE = 0.95


def wilson(k, n, confidence=CONFIDENCE):
    """Wilson score interval for k of n: (point, low, high). n must be positive."""
    z = statistics.NormalDist().inv_cdf((1.0 + confidence) / 2.0)
    z2, point = z * z, k / n
    denom = 1.0 + z2 / n
    center = (point + z2 / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt(point * (1.0 - point) / n + z2 / (4.0 * n * n))
    # At k == 0 the lower bound is exactly 0 and at k == n the upper bound is exactly 1; the two
    # terms cancel only up to float rounding, which differs by platform (2.8e-17 on Linux and Windows).
    low = 0.0 if k == 0 else max(0.0, center - half)
    high = 1.0 if k == n else min(1.0, center + half)
    return point, low, high


def _quantile(sorted_values, q):
    pos = q * (len(sorted_values) - 1)
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def bootstrap_mean(values, resamples=BOOTSTRAP_RESAMPLES, seed=SEED, confidence=CONFIDENCE):
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples))
    tail = (1.0 - confidence) / 2.0
    return sum(values) / n, _quantile(means, tail), _quantile(means, 1.0 - tail)


def bootstrap_diff(control, treatment, resamples=BOOTSTRAP_RESAMPLES, seed=SEED, confidence=CONFIDENCE):
    """Mean(treatment) - mean(control) with a percentile interval; arms resampled independently."""
    rng = random.Random(seed)
    nc, nt = len(control), len(treatment)
    diffs = sorted(sum(treatment[rng.randrange(nt)] for _ in range(nt)) / nt
                   - sum(control[rng.randrange(nc)] for _ in range(nc)) / nc
                   for _ in range(resamples))
    tail = (1.0 - confidence) / 2.0
    return (sum(treatment) / nt - sum(control) / nc), _quantile(diffs, tail), _quantile(diffs, 1.0 - tail)


def _empty(enabled=False, why=None):
    out = {"available": True, "ready": False, "enabled": enabled,
           "arms": {"control": 0, "treatment": 0}, "needed": MIN_SESSIONS, "min_calls": MIN_CALLS}
    if why:
        out["why"] = why
    return out


def _sessions(db_path):
    """[(arm, api_calls, tokens, reexpanded)] for non-sidechain sessions in an arm."""
    path = Path(db_path)
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "holdout_sessions" not in tables:
            return None
        if "session_log" not in tables:
            return []
        reexpanded = set()
        if "savings_events" in tables:
            reexpanded = {r[0] for r in conn.execute(
                "SELECT DISTINCT session_uuid FROM savings_events "
                "WHERE event_type = 'tool_archive_reexpand' AND session_uuid IS NOT NULL")}
        rows = conn.execute(
            "SELECT h.arm, h.session_uuid, MAX(COALESCE(s.api_calls, 0)), "
            "MAX(COALESCE(s.input_tokens, 0) + COALESCE(s.output_tokens, 0)) "
            "FROM holdout_sessions h JOIN session_log s ON s.session_uuid = h.session_uuid "
            "WHERE COALESCE(s.is_sidechain, 0) = 0 AND h.arm IN ('control', 'treatment') "
            "GROUP BY h.arm, h.session_uuid").fetchall()
    return [(arm, int(calls), int(tokens), uuid in reexpanded) for arm, uuid, calls, tokens in rows]


def collect(db_path, min_sessions=MIN_SESSIONS, min_calls=MIN_CALLS, resamples=BOOTSTRAP_RESAMPLES):
    """Arm comparison, or the not-ready state. A missing table means the holdout never ran."""
    if db_path is None or not Path(db_path).is_file():
        return _empty()
    try:
        rows = _sessions(db_path)
    except (sqlite3.Error, OSError, ValueError):
        return {"available": False, "ready": False, "why": "ledger unreadable"}
    if rows is None:
        return _empty()
    arms = {"control": [], "treatment": []}
    for arm, calls, tokens, reexp in rows:
        if calls >= min_calls:
            arms[arm].append((calls, tokens / calls, 1.0 if reexp else 0.0))
    counts = {k: len(v) for k, v in arms.items()}
    out = _empty(enabled=True)
    out.update(arms=counts, needed=min_sessions, min_calls=min_calls)
    if min(counts.values()) < min_sessions:
        return out
    metrics = {}
    for name, idx in (("calls_per_session", 0), ("tokens_per_call", 1), ("reexpand_rate", 2)):
        per_arm = {a: [r[idx] for r in v] for a, v in arms.items()}
        entry = {a: dict(zip(("mean", "low", "high"),
                             bootstrap_mean(per_arm[a], resamples) if name != "reexpand_rate"
                             else wilson(sum(per_arm[a]), len(per_arm[a]))))
                 for a in arms}
        entry["difference"] = dict(zip(("mean", "low", "high"),
                                       bootstrap_diff(per_arm["control"], per_arm["treatment"], resamples)))
        metrics[name] = entry
    out.update(ready=True, metrics=metrics, interval=CONFIDENCE)
    return out
