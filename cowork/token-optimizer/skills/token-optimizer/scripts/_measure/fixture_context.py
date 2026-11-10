"""Recognize transcripts made by Token Optimizer's benchmark harness."""
import fnmatch

_PATTERN = '*/-private-var-folders-*-T-to-bench-M[0-9]-*/*.jsonl'


def benchmark_path(path):
    return bool(path) and fnmatch.fnmatchcase(str(path).replace('\\', '/'), _PATTERN)


def excluded_session_sql(alias):
    return ("COALESCE(" + alias + ".session_uuid, '') NOT IN "
            "(SELECT session_uuid FROM session_log WHERE session_uuid IS NOT NULL "
            "AND jsonl_path GLOB '" + _PATTERN + "')")
