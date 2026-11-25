"""Per-transcript facts for the cache TTL waste report, read once per file.

One dashboard build asks for the report over more than one window (30 days and
the dashboard's own history length). Every fact taken from a transcript here
is the same for any window, except the turn timestamps the window filters on,
so each file is parsed once and summarized by (path, mtime_ns, size). A file
that changed is parsed again on its own. Entries keep only token counts,
timestamps and rebuild events, never the parsed turns.
"""
from __future__ import annotations

import os
from datetime import datetime

_MAX = 20000


def _memo(f):
    memo = f.__dict__.get('_ttl_file_memo')
    if memo is None:
        memo = {}
        setattr(f, '_ttl_file_memo', memo)
    return memo


def _stamp(raw):
    """Epoch seconds the explicit-TTL detector derives from a turn timestamp."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return None


def _summarize(f, filepath, parse_fn, runtime, tier_data, candidate_floor):
    try:
        turns = parse_fn(filepath)
    except Exception:
        return None, False
    if not turns:
        return None, True
    model = f._dominant_turn_model(turns)
    prof_key, profile = f._resolve_cache_profile(model, runtime=runtime)
    kind = profile['cache_kind']
    cache_read = full_input = 0
    for turn in turns:
        cr = int(turn.get('cache_read', 0) or 0)
        cc = int(turn.get('cache_creation', 0) or 0)
        cache_read += cr
        full_input += cr + cc + int(turn.get('input_tokens', 0) or 0)
    s = {'prof_key': prof_key, 'profile': profile, 'kind': kind,
         'cache_read': cache_read, 'full_input': full_input}
    if kind == 'explicit_ttl':
        split = []
        for turn in turns:
            h1 = int(turn.get('cache_creation_1h', 0) or 0)
            m5 = int(turn.get('cache_creation_5m', 0) or 0)
            if h1 or m5:
                split.append((f._keepwarm_iso_to_epoch(turn.get('timestamp')), h1, m5))
        events = []
        f._detect_explicit_ttl(
            turns, tier_data, profile, dominant_model=model,
            boundary=min(candidate_floor, profile.get('effective_ttl_seconds') or candidate_floor),
            cutoff_ts=None, events_out=events)
        s['split'] = split
        s['events'] = [(_stamp(e.get('timestamp')), e) for e in events]
    elif kind != 'none':
        detail = {}
        s['collapse'] = f._detect_automatic_collapse(turns, profile, dominant_model=model, detail_out=detail)
        s['max_gap'] = detail.get('max_gap', 0)
    return s, True


def summary(f, filepath, parse_fn, runtime, tier_data, candidate_floor):
    """The file's window-independent facts, or None when it has no turns."""
    try:
        st = os.stat(filepath)
        key = (str(filepath), st.st_mtime_ns, st.st_size, runtime, id(tier_data), candidate_floor,
               id(parse_fn), id(f._resolve_cache_profile), id(f._detect_explicit_ttl),
               id(f._detect_automatic_collapse), id(f._dominant_turn_model))
    except OSError:
        key = None
    memo = _memo(f)
    if key is not None and key in memo:
        return memo[key]
    s, cacheable = _summarize(f, filepath, parse_fn, runtime, tier_data, candidate_floor)
    if key is not None and cacheable:
        if len(memo) >= _MAX:
            memo.clear()
        memo[key] = s
    return s


def split_in_window(s, cutoff_ts):
    """1h and 5m cache-write tokens on turns inside the window."""
    h1 = m5 = 0
    for ts, a, b in s['split']:
        if cutoff_ts is not None and (ts is None or ts < cutoff_ts):
            continue
        h1 += a
        m5 += b
    return h1, m5


def events_in_window(s, cutoff_ts):
    """Fresh copies of the rebuilds whose resume turn is inside the window."""
    return [dict(e) for ts, e in s['events']
            if cutoff_ts is None or (ts is not None and ts >= cutoff_ts)]
