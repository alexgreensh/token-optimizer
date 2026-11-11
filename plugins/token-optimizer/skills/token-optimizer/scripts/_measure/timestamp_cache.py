"""Reuse local-to-UTC event conversions across dashboard summaries."""
from functools import lru_cache


@lru_cache(maxsize=65536)
def parse(timestamp, datetime_class, utc, tz_env, tz_names):
    try:
        value = datetime_class.fromisoformat(timestamp.replace('Z', '+00:00'))
        return value.astimezone(utc).replace(tzinfo=None)
    except (ValueError, TypeError, OSError):
        return None
