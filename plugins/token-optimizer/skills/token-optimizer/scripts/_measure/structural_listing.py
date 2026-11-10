"""Transcript check for trims of items that live in the skill listing.

Claude Code injects skills and commands as one `skill_listing` attachment and
holds it to a character budget: past the budget it shortens descriptions
instead of growing. Archiving a skill then frees no tokens; the remaining
descriptions just get longer. The audit's per-file estimate cannot see that,
so every trim of a listing item is checked against what was actually sent:
the median initial listing size in local sessions that started before the
trim versus those that started after it. The credited share of the trim is
observed shrink / estimated shrink, clamped to [0, 1].
"""
from __future__ import annotations

import json
import os
import statistics

LISTING_COMPONENTS = ("skills", "commands", "plugin_skills", "plugin_commands")
MIN_SESSIONS = 3
LOOKBACK_S = 7 * 86400
MAX_LINES = 200
CACHE_FILE = "structural_listing_checks.json"


def first_listing_chars(path):
    """Characters of the first initial skill_listing attachment, or None."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > MAX_LINES:
                    return None
                if '"skill_listing"' not in line:
                    continue
                try:
                    a = (json.loads(line).get("attachment") or {})
                except ValueError:
                    continue
                if a.get("type") == "skill_listing" and a.get("isInitial"):
                    return len(a.get("content") or "")
    except OSError:
        return None
    return None


def observe(sessions, ts_epoch, chars_per_token):
    """sessions: iterable of (start_epoch, transcript_path) for local sessions.

    Returns {"before": n, "after": n, "median_before", "median_after",
    "observed_tokens"} or None without enough evidence on both sides."""
    before, after = [], []
    for start, path in sessions:
        if start is None or not path:
            continue
        if ts_epoch - LOOKBACK_S <= start < ts_epoch:
            side = before
        elif start >= ts_epoch:
            side = after
        else:
            continue
        n = first_listing_chars(path)
        if n:
            side.append(n)
    if len(before) < MIN_SESSIONS or len(after) < MIN_SESSIONS:
        return {"before": len(before), "after": len(after), "observed_tokens": None}
    mb, ma = statistics.median(before), statistics.median(after)
    return {"before": len(before), "after": len(after), "median_before_chars": mb,
            "median_after_chars": ma,
            "observed_tokens": max(0, int((mb - ma) / float(chars_per_token or 4.0)))}


def apply_listing_check(trims, sessions, chars_per_token, data_dir=None):
    """Scale listing trims by observed/estimated, grouped by trim timestamp.

    Mutates each trim: "tokens" becomes the credited per-call size,
    "tokens_estimated" keeps the audit's figure, "listing_check" records the
    evidence ("measured", or "pending" when transcripts cannot show it yet).
    A frozen result (both sides have evidence) is cached in the data dir.
    """
    cache = {}
    cpath = os.path.join(str(data_dir), CACHE_FILE) if data_dir else None
    if cpath:
        try:
            with open(cpath, encoding="utf-8") as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
    groups = {}
    for t in trims:
        if t.get("component") in LISTING_COMPONENTS and t.get("scope") == "global":
            groups.setdefault(str(t["ts"]), []).append(t)
    sessions = list(sessions)
    changed = False
    for ts, group in groups.items():
        estimated = sum(int(t["tokens_before"]) - int(t["tokens_after"]) for t in group)
        obs = cache.get(ts)
        if obs is None or obs.get("observed_tokens") is None:
            obs = observe(sessions, group[0]["ts_epoch"], chars_per_token)
            if obs.get("observed_tokens") is not None and obs.get("after", 0) >= 20:
                cache[ts] = obs
                changed = True
        for t in group:
            t["tokens_estimated"] = int(t["tokens_before"]) - int(t["tokens_after"])
            t["listing_observation"] = obs
            if obs.get("observed_tokens") is None:
                t["listing_check"] = "pending"
                t["tokens"] = 0
                continue
            factor = min(1.0, obs["observed_tokens"] / float(estimated)) if estimated > 0 else 0.0
            t["listing_check"] = "measured"
            t["listing_factor"] = round(factor, 4)
            t["tokens"] = int(round(t["tokens_estimated"] * factor))
    if changed and cpath:
        try:
            with open(cpath, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=1, sort_keys=True)
        except OSError:
            pass
    return trims


GROWTH_RECENT_S = 7 * 86400
GROWTH_MIN_MONTH = 20


def listing_growth(sessions, now_epoch, chars_per_token, read=None):
    """How much the initial skill listing grew: today's size versus the
    smallest monthly median in the owner's own history.

    sessions: (start_epoch, transcript_path) for local sessions. Current is the
    median of sessions that started in the last 7 days (at least MIN_SESSIONS);
    the baseline is the smallest median among months with at least
    GROWTH_MIN_MONTH sessions. Returns None without enough evidence."""
    from datetime import datetime
    read = read or first_listing_chars
    recent, months = [], {}
    for start, path in sessions:
        if start is None or not path:
            continue
        n = read(path)
        if not n:
            continue
        months.setdefault(datetime.fromtimestamp(start).strftime("%Y-%m"), []).append(n)
        if now_epoch - GROWTH_RECENT_S <= start <= now_epoch:
            recent.append(n)
    medians = {m: statistics.median(v) for m, v in months.items() if len(v) >= GROWTH_MIN_MONTH}
    if len(recent) < MIN_SESSIONS or not medians:
        return None
    base_month = min(medians, key=lambda m: (medians[m], m))
    current, base = statistics.median(recent), medians[base_month]
    return {"current_chars": int(current), "baseline_chars": int(base), "baseline_month": base_month,
            "recent_sessions": len(recent), "baseline_sessions": len(months[base_month]),
            "growth_tokens": max(0, int((current - base) / float(chars_per_token or 4.0)))}
