"""Recognize transcripts made by Token Optimizer's benchmark harness."""
import fnmatch
import json
import os
from functools import lru_cache

# The harness runs each benchmark session in tempfile.mkdtemp(prefix="to-bench-M<n>-"),
# so its project key is the temp dir path with separators turned into dashes:
#   macOS   /private/var/folders/xx/yy/T/to-bench-M1-abc -> -private-var-folders-xx-yy-T-to-bench-M1-abc
#   Linux   /tmp/to-bench-M1-abc (or $TMPDIR)            -> -tmp-to-bench-M1-abc
#   Windows C:\Users\u\AppData\Local\Temp\to-bench-M1-abc -> C--Users-u-AppData-Local-Temp-to-bench-M1-abc
_PATTERNS = (
    '*/-private-var-folders-*-T-to-bench-M[0-9]-*/*.jsonl',
    '*/-var-folders-*-T-to-bench-M[0-9]-*/*.jsonl',
    '*/*-tmp-to-bench-M[0-9]-*/*.jsonl',
    '*/*-temp-to-bench-M[0-9]-*/*.jsonl',
    '*/*-Temp-to-bench-M[0-9]-*/*.jsonl',
    '*/*-TEMP-to-bench-M[0-9]-*/*.jsonl',
)
_PATTERN = _PATTERNS[0]  # kept for callers that import the macOS pattern


def benchmark_path(path):
    text = str(path or '').replace('\\', '/')
    return bool(text) and any(fnmatch.fnmatchcase(text, pattern) for pattern in _PATTERNS)


@lru_cache(maxsize=4096)
def _usage_in_file(path, size, mtime):
    try:
        with open(path, encoding='utf-8', errors='replace') as stream:
            for line in stream:
                if '"usage"' not in line or '"assistant"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict) or not isinstance(obj.get('message'), dict):
                    continue
                usage = obj['message'].get('usage')
                if obj.get('type') == 'assistant' and isinstance(usage, dict) and any(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in usage.values()):
                    return True
    except (OSError, AttributeError):
        pass
    return False


def has_usage(path):
    try:
        stat = os.stat(path)
        return _usage_in_file(str(path), stat.st_size, stat.st_mtime_ns)
    except (OSError, TypeError, ValueError):
        return False


def allowed_path(path):
    return not benchmark_path(path) or has_usage(path)


def excluded_session_sql(alias):
    glob = " OR ".join("replace(s.jsonl_path, char(92), '/') GLOB '" + p + "'" for p in _PATTERNS)
    return ("COALESCE((SELECT CASE WHEN " + glob + " "
            "THEN to_transcript_has_usage(s.jsonl_path) ELSE 1 END FROM session_log s "
            "WHERE s.session_uuid=COALESCE(NULLIF(" + alias + ".session_uuid,''),NULLIF(" + alias + ".session_id,'')) ORDER BY s.id DESC LIMIT 1), 1)=1")
