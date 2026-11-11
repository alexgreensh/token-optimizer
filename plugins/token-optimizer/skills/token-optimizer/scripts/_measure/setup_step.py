"""Setup savings measured as a step in the first-call series. Read-only.

A dated trim (the trim ledger, or a dated archive folder) says WHEN setup text
left the context that rides on every call. This module checks whether the
first-call series shows it: the median of the 20 sessions before the trim
against the median of the 20 after. The drop, times the calls that carried it
since, priced at each call's cache-read rate, is the measured saving.

Rules, fixed before any measurement:
  * The series is the PREFIX of the first call: measured first-call input minus
    the estimated size of the first user message. A longer or shorter first
    message therefore cannot masquerade as a setup change. Sessions whose first
    user message cannot be sized are left out; without 20 usable sessions on a
    side the result is CANNOT-MEASURE, never a guess.
  * A step is visible only when the drop is positive, at least 1% of the
    earlier median, and at least 3 robust standard errors (MAD-based) of the
    difference of the two medians. Otherwise the credit is zero and the
    existing estimate stays; nothing is invented.
  * Another dated trim inside the before/after span means the step cannot be
    attributed to one trim: credit zero, status ``overlap``.
  * No install-time credit: only calls from sessions that began after the trim
    count, and only until the trim was closed (reverted), if it was.
  * Fewer than 20 sessions after the trim yet: status ``pending``, credit zero.

Tier: ``measured`` when the step is visible, else the existing estimate stands.
Only counts, medians and token figures leave this module: no paths, no text.
"""
import math
import sqlite3
import statistics
from pathlib import Path

WINDOW = 20
MIN_Z = 3.0
MIN_RELATIVE = 0.01
MAD_SCALE = 1.4826


def _se(values):
    if len(values) < 2:
        return 0.0
    med = statistics.median(values)
    return MAD_SCALE * statistics.median(abs(v - med) for v in values) / math.sqrt(len(values))


def prefix_of(obs):
    """First-call input minus the estimated first user message, or None if it cannot be sized."""
    first_user = (obs.get("components") or {}).get("first_user")
    total = obs.get("total")
    if first_user is None or total is None:
        return None
    return max(0, int(total) - int(first_user))


def measure_step(sessions, trim_epoch, closed_epoch=None, other_trim_epochs=(), rate_fn=None, window=WINDOW,
                 cap_tokens=None):
    """One trim's step. ``sessions``: dicts with ts_epoch, total, components, api_calls, model.

    Returns a dict with ``status`` in measured | no_step | pending | overlap |
    cannot_measure, the two medians, the drop, the calls that carried it and
    its priced value. Never raises on odd input.
    """
    base = {"status": "cannot_measure", "tier": "estimated", "reason": "", "window": window,
            "before_median": None, "after_median": None, "drop_tokens": 0,
            "n_before": 0, "n_after": 0, "calls_since": 0, "tokens_saved": 0, "usd": 0.0,
            "unpriced_calls": 0}
    rows = sorted((s for s in sessions if s.get("ts_epoch") is not None), key=lambda s: s["ts_epoch"])
    before_all = [s for s in rows if s["ts_epoch"] < trim_epoch]
    after_all = [s for s in rows if s["ts_epoch"] >= trim_epoch]
    before = [(s, prefix_of(s)) for s in before_all]
    after = [(s, prefix_of(s)) for s in after_all]
    before = [(s, p) for s, p in before if p is not None][-window:]
    after = [(s, p) for s, p in after if p is not None][:window]
    base.update(n_before=len(before), n_after=len(after))
    if len(before) < window:
        return {**base, "reason": "fewer than %d earlier sessions with a sizable first message" % window}
    if len(after) < window:
        return {**base, "status": "pending", "reason": "fewer than %d sessions since the trim" % window}
    span = (before[0][0]["ts_epoch"], after[-1][0]["ts_epoch"])
    if any(span[0] <= t <= span[1] and t != trim_epoch for t in other_trim_epochs):
        return {**base, "status": "overlap", "reason": "another dated trim falls inside the comparison span"}
    b_vals, a_vals = [p for _, p in before], [p for _, p in after]
    b_med, a_med = statistics.median(b_vals), statistics.median(a_vals)
    drop = b_med - a_med
    se = math.sqrt(_se(b_vals) ** 2 + _se(a_vals) ** 2)
    base.update(before_median=round(b_med), after_median=round(a_med))
    visible = drop > 0 and drop >= MIN_RELATIVE * b_med and (drop >= MIN_Z * se if se > 0 else True)
    if not visible:
        return {**base, "status": "no_step", "reason": "no step visible in the first-call prefix"}
    observed_drop = int(round(drop))
    # A step can never be credited above what the trim itself removed per call.
    drop_tokens = min(observed_drop, int(cap_tokens)) if cap_tokens else observed_drop
    calls, usd, unpriced, carried = 0, 0.0, 0, []
    for s in after_all:
        if closed_epoch is not None and s["ts_epoch"] >= closed_epoch:
            continue
        n = int(s.get("api_calls") or 0)
        if n <= 0:
            continue
        rate = None
        if rate_fn is not None:
            try:
                rate = rate_fn(s.get("model"))
            except Exception:
                rate = None
        calls += n
        carried.append((s["ts_epoch"], n, rate))
        if rate is None:
            unpriced += n
        else:
            usd += drop_tokens * n * rate
    return {**base, "status": "measured", "tier": "measured", "reason": "", "drop_tokens": drop_tokens,
            "calls_since": calls, "tokens_saved": drop_tokens * calls, "usd": round(usd, 4),
            "unpriced_calls": unpriced, "observed_drop_tokens": observed_drop, "carried": carried}


def measure_all(sessions, trims, rate_fn=None, window=WINDOW):
    """Step per dated trim; trims at the same instant are one event."""
    epochs = sorted({t["ts_epoch"] for t in trims if t.get("ts_epoch") is not None})
    out = []
    for epoch in epochs:
        group = [t for t in trims if t.get("ts_epoch") == epoch]
        closes = [t["closed_epoch"] for t in group if t.get("closed_epoch") is not None]
        closed = min(closes) if len(closes) == len(group) else None
        size = sum(int(t.get("tokens") or 0) for t in group)
        result = measure_step(sessions, epoch, closed, [e for e in epochs if e != epoch], rate_fn, window, size)
        result["trim_epoch"] = epoch
        result["trims"] = len(group)
        result["estimated_tokens"] = size
        out.append(result)
    return out


def summarize(steps, keep_carried=False):
    if not keep_carried:
        steps = [{k: v for k, v in s.items() if k != "carried"} for s in steps]
    measured = [s for s in steps if s["status"] == "measured"]
    return {"available": True, "steps": steps,
            "measured_usd": round(sum(s["usd"] for s in measured), 4),
            "measured_tokens": sum(s["tokens_saved"] for s in measured),
            "measured_trims": len(measured)}


def observe_step(rows, estimate):
    """First-call observation plus the timestamp and model needed to place it in time."""
    from . import first_call_context
    seen = []
    for row in rows:
        seen.append(row)
        if row.get("type") == "assistant" and not row.get("isSidechain"):
            usage = (row.get("message") or {}).get("usage")
            if isinstance(usage, dict) and usage:
                break
    obs = first_call_context.observe(seen, estimate)
    if not obs:
        return None
    last = seen[-1]
    message = last.get("message") or {}
    model = message.get("model") if isinstance(message.get("model"), str) else None
    return {**obs, "ts": last.get("timestamp"), "model": model}


_MEMO = {}


def measured_epochs(db_path, data_dir, claude_dir=None, card_fn=None):
    """{trim epoch: {"share", "drop", "size", "carried"}} for each visible step: the capped drop,
    the trim group's size and the main-thread (ts_epoch, calls, rate) it was carried by.
    One scan per process and DB state."""
    try:
        key = (str(db_path), str(data_dir), str(claude_dir), Path(db_path).stat().st_mtime_ns)
    except OSError:
        return {}
    if key not in _MEMO:
        found = collect(db_path, data_dir, claude_dir, card_fn, keep_carried=True)
        _MEMO[key] = {s["trim_epoch"]: {"share": min(1.0, s["drop_tokens"] / s["estimated_tokens"]),
                                        "drop": s["drop_tokens"], "size": s["estimated_tokens"],
                                        "carried": s.get("carried", [])}
                      for s in found.get("steps", [])
                      if s.get("status") == "measured" and s.get("estimated_tokens")}
    return _MEMO[key]


def collect(db_path, data_dir, claude_dir=None, card_fn=None, keep_carried=False):
    """Steps for every dated trim, from the user's own ledger and transcripts. Read-only."""
    from . import first_call_context, structural_ledger as ledger
    path = Path(db_path) if db_path else None
    if path is None or not path.is_file():
        return {"available": False, "why": "no ledger"}
    try:
        trims = [t for t in ledger.load_trims(data_dir) if int(t.get("tokens") or 0) > 0]
        if claude_dir:
            from token_estimate import estimate_tokens
            from . import structural_archives
            trims += [t for t in structural_archives.discover(claude_dir, trims, estimate_tokens)
                      if int(t.get("tokens") or 0) > 0 and t.get("ts_epoch") is not None]
        if not trims:
            return {"available": True, "steps": [], "measured_usd": 0.0, "measured_tokens": 0,
                    "measured_trims": 0, "why": "no dated trims"}
        sessions = []
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0) as conn:
            rows = conn.execute("SELECT DISTINCT jsonl_path, api_calls FROM session_log "
                                "WHERE jsonl_path IS NOT NULL AND COALESCE(is_sidechain,0)=0").fetchall()
        for name, api_calls in rows:
            try:
                obs = observe_step(first_call_context.transcript(Path(name)), lambda v: round(len(v) / 4))
            except (OSError, ValueError, TypeError):
                continue
            epoch = ledger.to_epoch(obs.get("ts")) if obs else None
            if obs and epoch is not None:
                sessions.append({"ts_epoch": epoch, "total": obs["total"], "components": obs["components"],
                                 "api_calls": api_calls, "model": obs.get("model")})
        rate_fn = None
        if card_fn is not None:
            def rate_fn(model):
                card = card_fn(model, 0)
                return float(card["cache_read"]) / 1e6 if card else None
        return summarize(measure_all(sessions, trims, rate_fn), keep_carried)
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
        return {"available": False, "why": "ledger unreadable"}
