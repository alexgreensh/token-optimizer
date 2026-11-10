"""Counterfactual compaction weight from the user's own compaction sizes.

A lean resume avoided reloading the old session's context. The carry credits
that avoided context on later receiver turns only while the old session would
still have been alive. Instead of a fixed compaction size, each receiver turn is
weighted by the share of this user's own compactions that happened above the
counterfactual size, conditional on having reached the old session's size:

    w(ctx) = S(ctx + avoided) / S(avoided)

where S(x) is the fraction of compaction ``preTokens`` (any trigger, main
transcripts, the WINDOW_DAYS before the event) that are >= x. With fewer than
MIN_ABOVE compactions at or above ``avoided`` the history is too thin and the
caller keeps its fixed rule.
"""
import json
import os
from bisect import bisect_left
from datetime import timedelta
from pathlib import Path

from . import fixture_context

WINDOW_DAYS = 60
MIN_ABOVE = 10
CACHE_NAME = "compaction_sizes.json"
_CACHE_VERSION = 1


def scan(path):
    """[(utc 'YYYY-MM-DDTHH:MM:SS', preTokens)] for main-thread compactions in one file."""
    out = []
    with open(path, "rb") as stream:
        for line in stream:
            if b"compact_boundary" not in line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if (not isinstance(record, dict) or record.get("isSidechain")
                    or record.get("type") != "system" or record.get("subtype") != "compact_boundary"):
                continue
            meta = record.get("compactMetadata")
            value = meta.get("preTokens") if isinstance(meta, dict) else None
            stamp = str(record.get("timestamp") or "")[:19]
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 and len(stamp) == 19:
                out.append((stamp, int(value)))
    return out


def _load_cache(cache_path):
    try:
        with open(cache_path, encoding="utf-8") as stream:
            data = json.load(stream)
        if isinstance(data, dict) and data.get("version") == _CACHE_VERSION and isinstance(data.get("files"), dict):
            return data["files"]
    except (OSError, ValueError, TypeError):
        pass
    return {}


def _save_cache(cache_path, files):
    try:
        cache_path = Path(cache_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_name(cache_path.name + ".%d.tmp" % os.getpid())
        with open(tmp, "w", encoding="utf-8") as stream:
            json.dump({"version": _CACHE_VERSION, "files": files}, stream)
        os.replace(tmp, cache_path)
    except (OSError, TypeError, ValueError):
        pass


def history(projects_dir, floor_mtime=None, cache_path=None):
    """Every main-transcript compaction under ``projects_dir`` as sorted (ts, size).

    Files last written before ``floor_mtime`` cannot hold a compaction inside
    the window and are skipped. Benchmark-harness transcripts are not the
    user's own sessions and are skipped. Results are cached per (size, mtime).
    """
    projects_dir = Path(projects_dir)
    if not projects_dir.is_dir():
        return []
    cached = _load_cache(cache_path) if cache_path else {}
    files, changed, items = {}, False, []
    for path in projects_dir.glob("*/*.jsonl"):
        key = str(path)
        if fixture_context.benchmark_path(key):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if floor_mtime is not None and stat.st_mtime < floor_mtime:
            continue
        entry = cached.get(key)
        if not (isinstance(entry, dict) and entry.get("size") == stat.st_size
                and entry.get("mtime_ns") == stat.st_mtime_ns):
            try:
                entry = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "items": scan(path)}
            except OSError:
                continue
            changed = True
        files[key] = entry
        items.extend(tuple(item) for item in entry.get("items") or [])
    if cache_path and (changed or set(files) != set(cached)):
        merged = dict(cached)
        merged.update(files)
        _save_cache(cache_path, merged)
    return sorted(items)


class Survival:
    """Weight for one event from the compactions in the window before it."""

    def __init__(self, sizes, avoided):
        self.sizes = sorted(sizes)
        self.avoided = int(avoided)
        self.above = self._at_least(self.avoided)

    def _at_least(self, value):
        return len(self.sizes) - bisect_left(self.sizes, value)

    @property
    def usable(self):
        return self.avoided > 0 and self.above >= MIN_ABOVE

    def weight(self, ctx):
        if not self.usable:
            return None
        return self._at_least(int(ctx) + self.avoided) / float(self.above)


def for_event(compactions, event_utc, avoided):
    """Survival for an event at ``event_utc`` (UTC-naive datetime)."""
    end = event_utc.strftime("%Y-%m-%dT%H:%M:%S")
    start = (event_utc - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    return Survival([size for stamp, size in compactions if start <= stamp < end], avoided)


def user_history(m, earliest_event_utc):
    """Cached compaction history for the user's Claude Code projects dir."""
    try:
        projects = Path(m.CLAUDE_DIR) / "projects"
    except (AttributeError, TypeError):
        return []
    floor = None
    if earliest_event_utc is not None:
        try:
            floor = (earliest_event_utc - timedelta(days=WINDOW_DAYS + 1)).timestamp()
        except (OverflowError, ValueError, OSError):
            floor = None
    try:
        cache = Path(m.TRENDS_DB).parent / CACHE_NAME
    except (AttributeError, TypeError):
        cache = None
    return history(projects, floor, cache)
