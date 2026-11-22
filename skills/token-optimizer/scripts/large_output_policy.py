#!/usr/bin/env python3
"""Large Bash output policy (default SHADOW mode; active opt-in via env).

Evaluates Bash stdout > 8 KB that no existing compressor handled — including
chained commands, loops, subshells, and heredocs — while excluding explicit
narrow reads (sed -n, head -n, tail -n, cut ranges, awk NR ranges),
binary-like output, and already-archived output.

Default mode is SHADOW (`TOKEN_OPTIMIZER_LARGE_OUTPUT_MODE` unset or != 'active'):
  - Never modifies model-visible output (byte-identical to today).
  - Never emits updatedToolOutput, never archives extra content, never credits
    savings to any headline, dashboard total, status bar, or limits number.
  - Records one local counts-only row per eligible output to `large_output_shadow`
    in trends.db with bounded retention. Never stores output text or command text.

Active mode (`TOKEN_OPTIMIZER_LARGE_OUTPUT_MODE=active`):
  - Archives raw stdout FIRST and verifies the archive entry exists; if archiving
    fails, returns None so the caller serves original stdout untouched.
  - Emits first 25 + last 25 lines, every section/echo header, every critical
    line, an exact declaration of hidden lines by count and line range, and the
    exact runnable expand command.
  - Requires >= 15% and >= 200 tokens net saving after the expand pointer.
  - Deterministic and fail-open on any error.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

FEATURE_NAME = "large_output_policy"
MODE_ENV = "TOKEN_OPTIMIZER_LARGE_OUTPUT_MODE"
SHADOW_LABEL = "shadow, not counted"

MIN_BYTES = 8192
HEAD_LINES = 25
TAIL_LINES = 25
MIN_SAVING_RATIO = 0.15
MIN_SAVING_TOKENS = 200

SHADOW_TABLE = "large_output_shadow"
SHADOW_RETENTION_DAYS = 90
SHADOW_MAX_ROWS = 10000
_DB_TIMEOUT_SECONDS = 0.05
_DB_BUSY_TIMEOUT_MS = 50
FOLLOWUP_WINDOW_CALLS = 3

BUCKETS = (
    "heredoc_script",
    "for_while_loop",
    "subshell_or_subst",
    "and_chain",
    "semi_or_newline_chain",
    "python_inline",
    "awk_inline_interp",
    "tmux",
    "curl_http",
    "write_cmd_noisy",
    "other_large_output",
)

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_.:-]+")

# Explicit narrow-read exclusions (word-boundary / shell command aware).
_CMD_PREFIX = r"(?:^|[;&|\n]|\|\||&&)\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
_SED_N_RE = re.compile(_CMD_PREFIX + r"(?:g?sed)\s+(?:[^;&|\n]*?\s)?-[a-zA-Z]*n\b")
_HEAD_N_RE = re.compile(
    _CMD_PREFIX + r"(?:g?head)\s+(?:[^;&|\n]*?\s)?(?:-n\s*\S+|--lines(?:=|\s+)\S+|-\d+\b)"
)
_TAIL_N_RE = re.compile(
    _CMD_PREFIX + r"(?:g?tail)\s+(?:[^;&|\n]*?\s)?(?:-n\s*\S+|--lines(?:=|\s+)\S+|-\d+\b)"
)
_CUT_RANGE_RE = re.compile(
    _CMD_PREFIX + r"(?:g?cut)\s+(?:[^;&|\n]*?\s)?(?:-[fcb]\s*\S+|--(?:fields|characters|bytes)(?:=|\s+)\S+)"
)
_AWK_NR_RE = re.compile(
    _CMD_PREFIX + r"(?:g?awk|mawk|nawk)\b[^;&|\n]*\b(?:F?NR)\s*(?:==|!=|<=|>=|<|>)"
)

# Section / command-boundary header patterns (P1 + P2 header preservation).
_SECTION_HEADER_RE = re.compile(
    r"(?:"
    r"={3,}|"
    r"^-{3,}(?:\s|$|-)|"
    r"\s-{3,}\s|"
    r"^diff --git\s+|"
    r"^\+\+\+\s+[ab]/|"
    r"^---\s+[ab]/|"
    r"^##\[(?:group|endgroup)\]|"
    r"^(?:Step|Stage|Phase)\s+\d+(?:/\d+)?\b|"
    r"^\[(?:Step|Stage|Phase)\s+\d+|"
    r"^#{1,3}\s+\S|"
    r"^(?:\+\s+\S|\$\s+\S|>>>\s+\S|\*\*\*\s+\S)"
    r")"
)

# Echo / printf literal boundary extraction from command text.
_ECHO_STMT_RE = re.compile(
    r"(?:^|[;&\n]|&&|\|\|)\s*(?:echo|printf)\s+(?:-e\s+|-n\s+)?(?:'([^'\n]{2,120})'|\"([^\"\n]{2,120})\"|([A-Za-z0-9_=.:/#-]{3,120}))"
)

# Additional critical-line guards supplementing visibility_rule.
_EXTRA_CRITICAL_RE = re.compile(
    r"(?:"
    r"(?i:\bnon-?zero\s+exit\b)|"
    r"(?i:\bexit\s+(?:code|status)\s+[1-9]\d*\b)|"
    r"^FAILED\s+\S+|"
    r"^\s*FAIL\s+\S+|"
    r"\bAssertionError\b|"
    r"(?i:\bcommand\s+failed\b)"
    r")"
)

_SHADOW_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {SHADOW_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT NOT NULL,
    tool_use_id TEXT NOT NULL,
    bucket TEXT NOT NULL,
    bytes INTEGER NOT NULL DEFAULT 0,
    lines INTEGER NOT NULL DEFAULT 0,
    would_hide_lines INTEGER NOT NULL DEFAULT 0,
    tokens_before INTEGER NOT NULL DEFAULT 0,
    tokens_after INTEGER NOT NULL DEFAULT 0,
    critical_lines INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_large_output_shadow_ts ON {SHADOW_TABLE}(timestamp);
"""


def mode() -> str:
    """Return 'active' only when explicitly opted in via env, else 'shadow'."""
    raw = os.environ.get(MODE_ENV, "").strip().lower()
    return "active" if raw == "active" else "shadow"


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _tokens(text: str) -> int:
    if not text:
        return 0
    try:
        from token_estimate import estimate_tokens
        return int(estimate_tokens(text))
    except Exception:
        return max(1, len(text.encode("utf-8", errors="replace")) // 4)


def is_already_archived(text: str) -> bool:
    """True when output already carries a Token Optimizer archive/policy marker."""
    if not text:
        return False
    return (
        "[Full result archived" in text
        or "[bash_compress]" in text
        or "[large_output_policy:" in text
    )


def is_binary_like(text: str) -> bool:
    """True for NUL-bearing or heavily non-printable/replacement-char outputs."""
    if not text:
        return False
    if "\x00" in text:
        return True
    sample = _strip_ansi(text[:4096])
    if not sample:
        return False
    ctrl = sum(1 for ch in sample if ord(ch) < 32 and ch not in ("\t", "\n", "\r"))
    if ctrl / len(sample) > 0.10:
        return True
    repl = sample.count("\ufffd")
    if repl / len(sample) > 0.15:
        return True
    return False


def is_explicit_narrow_read(command: str) -> bool:
    """True for explicit narrow-read commands (sed -n, head -n, tail -n, cut, awk NR)."""
    if not command or not isinstance(command, str):
        return False
    try:
        from pipe_file_compress import has_explicit_output_limit
        if has_explicit_output_limit(command):
            return True
    except Exception:
        pass
    if _SED_N_RE.search(command):
        return True
    if _HEAD_N_RE.search(command):
        return True
    if _TAIL_N_RE.search(command):
        return True
    if _CUT_RANGE_RE.search(command):
        return True
    if _AWK_NR_RE.search(command):
        return True
    return False


def classify_bucket(command: str) -> str:
    """Classify a Bash command into a fixed-vocabulary bucket (no command text stored)."""
    cmd = (command or "").strip()
    if "<<" in cmd:
        return "heredoc_script"
    if re.search(r"(?:^|[;&|\n])\s*(?:for|while|until)\b", cmd):
        return "for_while_loop"
    if "$(" in cmd or "`" in cmd or re.search(r"(?:^|[;&|\n])\s*\(", cmd):
        return "subshell_or_subst"
    if "&&" in cmd or "||" in cmd:
        return "and_chain"
    if ";" in cmd or "\n" in cmd:
        return "semi_or_newline_chain"
    if re.search(r"(?:^|\s)python3?(?:\.\d+)?\s+.*-c\b", cmd):
        return "python_inline"
    if re.search(r"(?:^|[;&|\s])(?:g?awk|mawk|nawk|perl|node\s+-e|ruby\s+-e)\b", cmd):
        return "awk_inline_interp"
    if re.search(r"(?:^|[;&|\s])tmux\b", cmd):
        return "tmux"
    if re.search(r"(?:^|[;&|\s])(?:curl|wget|http)\b", cmd):
        return "curl_http"
    if re.search(r"(?:>|>>|\btee\b|\bnpm\b|\bpip3?\b|\bcargo\b|\bmake\b|\bdocker\b|\bgit\s+(?:commit|push|pull|fetch)\b)", cmd):
        return "write_cmd_noisy"
    return "other_large_output"


def _extract_echo_markers(command: str) -> frozenset[str]:
    """Extract literal echo/printf section labels from the command string."""
    if not command:
        return frozenset()
    markers: set[str] = set()
    for m in _ECHO_STMT_RE.finditer(command):
        val = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        val = val.replace("\\n", "").strip()
        if len(val) >= 2:
            markers.add(val)
    return frozenset(markers)


def is_section_header(line: str, echo_markers: frozenset[str] = frozenset()) -> bool:
    """True when ``line`` is a section/echo/diff/command boundary header."""
    if not line:
        return False
    stripped = line.strip()
    if not stripped:
        return False
    if echo_markers and stripped in echo_markers:
        return True
    return bool(_SECTION_HEADER_RE.search(stripped))


def is_critical_line(line: str) -> bool:
    """True when ``line`` carries an error, failure, exception, traceback, panic,
    failing test, or non-zero exit that must remain visible verbatim."""
    if not line or not line.strip():
        return False
    try:
        from visibility_rule import is_error_or_failure_line
        if is_error_or_failure_line(line):
            return True
    except Exception:
        pass
    return bool(_EXTRA_CRITICAL_RE.search(line))


def _format_hidden_ranges(runs: list[tuple[int, int]]) -> str:
    """Format 0-based inclusive hidden runs as exact 1-based line ranges."""
    parts: list[str] = []
    for start_0, end_0 in runs:
        lo, hi = start_0 + 1, end_0 + 1
        parts.append(f"line {lo}" if lo == hi else f"lines {lo}-{hi}")
    return ", ".join(parts)


def _attach_pointer(preview_body: str, original_chars: int, archive_key: str) -> str:
    """Attach the exact expand pointer using archive_result.build_archive_pointer."""
    try:
        from archive_result import build_archive_pointer
        return build_archive_pointer(preview_body, original_chars, archive_key)
    except Exception:
        script_path = Path(__file__).resolve().parent / "archive_result.py"
        return (
            f"{preview_body}\n\n"
            f"[Full result archived ({original_chars:,} chars) — saved to disk, not lost.\n"
            f'Run: python3 "{script_path}" expand {archive_key}\n]'
        )


def evaluate(
    command: str,
    stdout: str,
    *,
    archive_key: str | None = None,
) -> dict[str, Any] | None:
    """Evaluate eligibility and build the deterministic would-be output.

    Pure and fail-open: performs zero disk I/O and returns ``None`` if the
    output is ineligible, has no hidden lines, or fails the >=15% and >=200
    token net saving gate after the expand pointer.
    """
    try:
        if not isinstance(stdout, str) or not stdout:
            return None
        raw_bytes = len(stdout.encode("utf-8", errors="replace"))
        if raw_bytes <= MIN_BYTES:
            return None
        if is_already_archived(stdout):
            return None
        if is_binary_like(stdout):
            return None
        if is_explicit_narrow_read(command or ""):
            return None

        cleaned = _strip_ansi(stdout)
        lines = cleaned.splitlines()
        total_lines = len(lines)
        if total_lines <= HEAD_LINES + TAIL_LINES:
            return None

        echo_markers = _extract_echo_markers(command or "")
        kept: set[int] = set(range(min(HEAD_LINES, total_lines)))
        kept.update(range(max(0, total_lines - TAIL_LINES), total_lines))

        critical_count = 0
        for idx, line in enumerate(lines):
            crit = is_critical_line(line)
            if crit:
                critical_count += 1
                kept.add(idx)
            elif idx not in kept and is_section_header(line, echo_markers):
                kept.add(idx)

        hidden_indices = [i for i in range(total_lines) if i not in kept]
        if not hidden_indices:
            return None

        # Group hidden indices into contiguous inclusive 0-based runs.
        hidden_runs: list[tuple[int, int]] = []
        run_start = hidden_indices[0]
        run_prev = hidden_indices[0]
        for idx in hidden_indices[1:]:
            if idx == run_prev + 1:
                run_prev = idx
            else:
                hidden_runs.append((run_start, run_prev))
                run_start = run_prev = idx
        hidden_runs.append((run_start, run_prev))

        run_by_start = {s: e for s, e in hidden_runs}
        out_lines: list[str] = []
        i = 0
        while i < total_lines:
            if i in kept:
                out_lines.append(lines[i])
                i += 1
            else:
                end_0 = run_by_start[i]
                count = end_0 - i + 1
                lo, hi = i + 1, end_0 + 1
                if count == 1:
                    out_lines.append(f"[... 1 line hidden: line {lo} ...]")
                else:
                    out_lines.append(f"[... {count} lines hidden: lines {lo}-{hi} ...]")
                i = end_0 + 1

        would_hide_lines = len(hidden_indices)
        ranges_str = _format_hidden_ranges(hidden_runs)
        out_lines.append(
            f"[large_output_policy: {would_hide_lines} of {total_lines} lines hidden "
            f"({ranges_str}); {critical_count} critical line(s) preserved]"
        )
        preview_body = "\n".join(out_lines)
        key = archive_key or ("0" * 16)
        preview_with_pointer = _attach_pointer(preview_body, len(stdout), key)

        tokens_before = _tokens(cleaned)
        tokens_after = _tokens(preview_with_pointer)
        saved_tokens = tokens_before - tokens_after
        if tokens_before <= 0 or saved_tokens < MIN_SAVING_TOKENS:
            return None
        if (saved_tokens / tokens_before) < MIN_SAVING_RATIO:
            return None

        return {
            "bucket": classify_bucket(command or ""),
            "bytes": raw_bytes,
            "lines": total_lines,
            "would_hide_lines": would_hide_lines,
            "hidden_ranges": ranges_str,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "saved_tokens": saved_tokens,
            "critical_lines": critical_count,
            "preview_body": preview_body,
            "preview_with_pointer": preview_with_pointer,
        }
    except Exception:
        return None


def _sanitize_id(value: str | None) -> str:
    cleaned = _ID_SAFE_RE.sub("", str(value or "").strip())
    return cleaned[:64]


def _snapshot_dir() -> Path:
    from plugin_env import resolve_snapshot_dir
    return resolve_snapshot_dir()


def record_shadow_row(
    eval_result: dict[str, Any],
    *,
    session_id: str = "",
    tool_use_id: str = "",
    snapshot_dir: Path | None = None,
    retention_days: int = SHADOW_RETENTION_DAYS,
    max_rows: int = SHADOW_MAX_ROWS,
    now: datetime | None = None,
) -> bool:
    """Persist one counts-only shadow row with bounded retention. Fail-open.

    Never stores command text or output text. Never raises.
    """
    conn = None
    try:
        bucket = str(eval_result.get("bucket", "other_large_output"))
        if bucket not in BUCKETS:
            bucket = "other_large_output"
        directory = Path(snapshot_dir) if snapshot_dir is not None else _snapshot_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = directory / "trends.db"
        ts_now = now or datetime.now()
        conn = sqlite3.connect(str(db), timeout=_DB_TIMEOUT_SECONDS)
        conn.execute(f"PRAGMA busy_timeout={_DB_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SHADOW_SCHEMA)
        conn.execute(
            f"INSERT INTO {SHADOW_TABLE} "
            "(timestamp, session_id, tool_use_id, bucket, bytes, lines, "
            " would_hide_lines, tokens_before, tokens_after, critical_lines) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts_now.isoformat(),
                _sanitize_id(session_id),
                _sanitize_id(tool_use_id),
                bucket,
                max(0, int(eval_result.get("bytes", 0))),
                max(0, int(eval_result.get("lines", 0))),
                max(0, int(eval_result.get("would_hide_lines", 0))),
                max(0, int(eval_result.get("tokens_before", 0))),
                max(0, int(eval_result.get("tokens_after", 0))),
                max(0, int(eval_result.get("critical_lines", 0))),
            ),
        )
        # Bounded retention: prune by age AND cap row count deterministically.
        if retention_days > 0:
            cutoff = (ts_now - timedelta(days=int(retention_days))).isoformat()
            conn.execute(f"DELETE FROM {SHADOW_TABLE} WHERE timestamp < ?", (cutoff,))
        if max_rows > 0:
            conn.execute(
                f"DELETE FROM {SHADOW_TABLE} WHERE id NOT IN ("
                f"  SELECT id FROM {SHADOW_TABLE} ORDER BY id DESC LIMIT ?"
                f")",
                (int(max_rows),),
            )
        conn.commit()
        try:
            os.chmod(str(db), 0o600)
        except OSError:
            pass
        return True
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def handle_post_tool_use(
    command: str,
    stdout: str,
    *,
    session_id: str = "",
    tool_use_id: str = "",
    snapshot_dir: Path | None = None,
) -> tuple[str | None, str]:
    """PostToolUse entry point wired after existing Bash compressors.

    Returns ``(compressed_output_or_None, cleaned_original)``.
    - In default SHADOW mode: logs a counts-only shadow row if eligible and
      ALWAYS returns ``(None, "")`` (never modifies output, never archives).
    - In ACTIVE mode (`TOKEN_OPTIMIZER_LARGE_OUTPUT_MODE=active`): archives raw
      stdout FIRST, verifies the archive entry exists (failing open to None on
      any archive error), and returns ``(compressed_with_pointer, cleaned)``.
    """
    try:
        if not isinstance(stdout, str) or len(stdout.encode("utf-8", errors="replace")) <= MIN_BYTES:
            return None, ""
        cur_mode = mode()
        if cur_mode != "active":
            ev = evaluate(command, stdout, archive_key="0" * 16)
            if ev is not None:
                record_shadow_row(
                    ev,
                    session_id=session_id,
                    tool_use_id=tool_use_id,
                    snapshot_dir=snapshot_dir,
                )
            return None, ""

        # Active mode: compute deterministic archive key, evaluate, archive first.
        archive_key = hashlib.sha256(
            f"{session_id}|{command}|{stdout}".encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        ev = evaluate(command, stdout, archive_key=archive_key)
        if ev is None:
            return None, ""

        from archive_result import archive_entry_exists, archive_original
        if archive_original(stdout, session_id, archive_key, "Bash") is None:
            return None, ""
        if not archive_entry_exists(session_id, archive_key):
            return None, ""

        compressed = ev["preview_with_pointer"]
        try:
            from bash_compress import _enforce_baseline_invariant
            compressed = _enforce_baseline_invariant(compressed, stdout, archive_key)
        except Exception:
            pass
        return compressed, _strip_ansi(stdout)
    except Exception:
        return None, ""


def compress_benchmark_fixture(fixture: dict[str, Any]) -> str:
    """Benchmark adapter for ``benchmark.py`` external fixtures (no disk I/O)."""
    cmd = str(fixture.get("command", ""))
    raw = str(fixture.get("raw_output", ""))
    ev = evaluate(cmd, raw, archive_key="bench00000000001")
    if ev is None:
        return raw
    return ev["preview_with_pointer"]


# ---------------------------------------------------------------------------
# Shadow report (`measure.py large-output-shadow [--days N] [--json]`)
# ---------------------------------------------------------------------------

_PATH_TOKEN_RE = re.compile(r"(?:^|[\s'\"=])((?:[~/.]|[A-Za-z0-9_.-]+/)[A-Za-z0-9_./-]{2,})")


def _command_signature(command: str) -> tuple[str, set[str]]:
    """Extract normalized command head and path-like targets for follow-up proxy."""
    if not isinstance(command, str) or not command.strip():
        return "", set()
    norm = " ".join(command.strip().split())
    paths: set[str] = set()
    for m in _PATH_TOKEN_RE.finditer(command):
        tok = m.group(1).strip("'\".,;:)(")
        if tok and not tok.startswith("-"):
            paths.add(os.path.normpath(tok))
            base = os.path.basename(tok)
            if len(base) >= 3:
                paths.add(base)
    return norm, paths


def _transcript_calls_by_session(projects_dir: Path | None, wanted_sessions: set[str]) -> dict[str, list[dict[str, Any]]]:
    """Read-only scan of matching session transcripts for follow-up target proxy.

    Only returns lightweight in-memory call metadata (never written anywhere).
    """
    if not projects_dir or not wanted_sessions:
        return {}
    root = Path(projects_dir)
    if not root.is_dir():
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    try:
        for jsonl in root.rglob("*.jsonl"):
            sid = jsonl.stem
            if sid not in wanted_sessions:
                continue
            calls: list[dict[str, Any]] = []
            try:
                with open(jsonl, encoding="utf-8", errors="replace") as stream:
                    for line in stream:
                        if '"tool_use"' not in line:
                            continue
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        msg = row.get("message") if isinstance(row, dict) else None
                        content = msg.get("content") if isinstance(msg, dict) else None
                        if not isinstance(content, list):
                            continue
                        for block in content:
                            if not isinstance(block, dict) or block.get("type") != "tool_use":
                                continue
                            tid = block.get("id")
                            name = block.get("name", "")
                            inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                            if tid:
                                calls.append({"id": str(tid), "name": str(name), "input": inp})
            except OSError:
                continue
            out[sid] = calls
    except OSError:
        return {}
    return out


def _is_immediate_followup(origin: dict[str, Any], later: dict[str, Any]) -> bool:
    """True if ``later`` immediately re-runs or reads the same target as ``origin``."""
    orig_cmd = str((origin.get("input") or {}).get("command", "") or "")
    orig_norm, orig_paths = _command_signature(orig_cmd)
    if not orig_norm:
        return False

    later_name = later.get("name", "")
    later_inp = later.get("input") or {}

    if later_name in ("Read", "Grep", "Glob"):
        fp = str(later_inp.get("file_path") or later_inp.get("path") or "")
        if fp:
            norm_fp = os.path.normpath(fp)
            base_fp = os.path.basename(norm_fp)
            if norm_fp in orig_paths or (len(base_fp) >= 3 and base_fp in orig_paths):
                return True
        return False

    if later_name == "Bash":
        later_cmd = str(later_inp.get("command", "") or "")
        later_norm, later_paths = _command_signature(later_cmd)
        if not later_norm:
            return False
        if later_norm == orig_norm:
            return True
        if orig_paths and (orig_paths & later_paths):
            return True
        try:
            o_toks = shlex.split(orig_cmd)[:2]
            l_toks = shlex.split(later_cmd)[:2]
            if len(o_toks) >= 2 and o_toks == l_toks:
                return True
        except ValueError:
            pass
    return False


def collect_shadow_report(
    db_path: Path | str,
    projects_dir: Path | str | None = None,
    *,
    days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Summarize shadow rows from trends.db plus immediate re-run/read proxy.

    Read-only. All numbers are explicitly labelled ``shadow, not counted``.
    """
    path = Path(db_path)
    base: dict[str, Any] = {
        "available": False,
        "status": SHADOW_LABEL,
        "label": SHADOW_LABEL,
        "counted": False,
        "window_days": int(days),
        "eligible_outputs": 0,
        "total_bytes": 0,
        "total_lines": 0,
        "would_hide_lines": 0,
        "tokens_before": 0,
        "tokens_after": 0,
        "would_save_tokens": 0,
        "critical_lines_preserved": 0,
        "buckets": {},
        "followup_window_calls": FOLLOWUP_WINDOW_CALLS,
        "followup_checked": 0,
        "followup_rerun_or_read": 0,
        "followup_rate": None,
    }
    if not path.is_file():
        base["why"] = "no ledger"
        return base

    cutoff = ((now or datetime.now()) - timedelta(days=int(days))).isoformat()
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0) as conn:
            have = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (SHADOW_TABLE,),
            ).fetchone()
            if not have:
                base["why"] = "no shadow table yet"
                return base
            rows = conn.execute(
                f"SELECT timestamp, session_id, tool_use_id, bucket, bytes, lines, "
                f"would_hide_lines, tokens_before, tokens_after, critical_lines "
                f"FROM {SHADOW_TABLE} WHERE timestamp >= ? ORDER BY id ASC",
                (cutoff,),
            ).fetchall()
    except (sqlite3.Error, OSError, ValueError):
        base["why"] = "ledger unreadable"
        return base

    base["available"] = True
    buckets: dict[str, dict[str, int]] = {}
    wanted_sessions: set[str] = set()
    row_refs: list[tuple[str, str]] = []

    for (_ts, sid, tid, bucket, nbytes, nlines, whide, t_bef, t_aft, crit) in rows:
        nbytes = int(nbytes or 0)
        nlines = int(nlines or 0)
        whide = int(whide or 0)
        t_bef = int(t_bef or 0)
        t_aft = int(t_aft or 0)
        crit = int(crit or 0)
        saved = max(0, t_bef - t_aft)

        base["eligible_outputs"] += 1
        base["total_bytes"] += nbytes
        base["total_lines"] += nlines
        base["would_hide_lines"] += whide
        base["tokens_before"] += t_bef
        base["tokens_after"] += t_aft
        base["would_save_tokens"] += saved
        base["critical_lines_preserved"] += crit

        b = buckets.setdefault(str(bucket or "other_large_output"), {
            "eligible_outputs": 0,
            "would_save_tokens": 0,
            "bytes": 0,
            "lines": 0,
            "would_hide_lines": 0,
            "critical_lines": 0,
        })
        b["eligible_outputs"] += 1
        b["would_save_tokens"] += saved
        b["bytes"] += nbytes
        b["lines"] += nlines
        b["would_hide_lines"] += whide
        b["critical_lines"] += crit

        if sid and tid:
            wanted_sessions.add(str(sid))
            row_refs.append((str(sid), str(tid)))

    base["buckets"] = dict(
        sorted(buckets.items(), key=lambda kv: kv[1]["would_save_tokens"], reverse=True)
    )

    if projects_dir and row_refs:
        sessions = _transcript_calls_by_session(Path(projects_dir), wanted_sessions)
        checked = 0
        hits = 0
        for sid, tid in row_refs:
            seq = sessions.get(sid)
            if not seq:
                continue
            idx = next((i for i, c in enumerate(seq) if c["id"] == tid), None)
            if idx is None:
                continue
            checked += 1
            origin = seq[idx]
            if any(_is_immediate_followup(origin, later)
                   for later in seq[idx + 1: idx + 1 + FOLLOWUP_WINDOW_CALLS]):
                hits += 1
        base["followup_checked"] = checked
        base["followup_rerun_or_read"] = hits
        base["followup_rate"] = round(hits / checked, 4) if checked else None

    return base


def format_shadow_report(summary: dict[str, Any]) -> str:
    """Format a human-readable report with every figure labelled 'shadow, not counted'."""
    label = summary.get("label", SHADOW_LABEL)
    days = summary.get("window_days", 30)
    lines = [
        f"Large-Output Policy Report — last {days}d ({label})",
        f"  Note: All figures below are {label} (zero effect on model output or savings totals).",
    ]
    if not summary.get("available"):
        lines.append(f"  Status: unavailable ({summary.get('why', 'unknown')}) [{label}]")
        return "\n".join(lines)

    elig = summary["eligible_outputs"]
    ws = summary["would_save_tokens"]
    tb = summary["tokens_before"]
    ta = summary["tokens_after"]
    wh = summary["would_hide_lines"]
    tl = summary["total_lines"]
    cr = summary["critical_lines_preserved"]
    lines.extend([
        f"  Eligible outputs ({label}):     {elig:,}",
        f"  Would-save tokens ({label}):    {ws:,} ({tb:,} -> {ta:,})",
        f"  Would-hide lines ({label}):     {wh:,} / {tl:,} ({cr:,} critical line(s) preserved)",
    ])
    checked = summary.get("followup_checked", 0)
    hits = summary.get("followup_rerun_or_read", 0)
    rate = summary.get("followup_rate")
    win = summary.get("followup_window_calls", FOLLOWUP_WINDOW_CALLS)
    rate_str = f"{rate * 100:.1f}%" if rate is not None else "n/a"
    lines.append(
        f"  Immediate re-run/read proxy ({label}, next {win} calls): "
        f"{hits:,} / {checked:,} ({rate_str})"
    )

    buckets = summary.get("buckets") or {}
    if buckets:
        lines.append(f"  Buckets ({label}):")
        for name, b in buckets.items():
            lines.append(
                f"    - {name}: {b['eligible_outputs']:,} outputs, "
                f"{b['would_save_tokens']:,} would-save tokens ({label})"
            )
    return "\n".join(lines)


def shadow_report_cli(
    argv: list[str],
    db_path: Path | str,
    projects_dir: Path | str | None = None,
) -> int:
    """CLI handler for ``measure.py large-output-shadow [--days N] [--json]``."""
    days = 30
    as_json = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--json":
            as_json = True
            i += 1
        elif arg == "--days" and i + 1 < len(argv):
            try:
                days = max(1, int(argv[i + 1]))
            except ValueError:
                print(f"Invalid --days value: {argv[i + 1]}", file=sys.stderr)
                return 2
            i += 2
        elif arg.startswith("--days="):
            try:
                days = max(1, int(arg.split("=", 1)[1]))
            except ValueError:
                print(f"Invalid --days value: {arg}", file=sys.stderr)
                return 2
            i += 1
        else:
            i += 1

    summary = collect_shadow_report(db_path, projects_dir, days=days)
    if as_json:
        print(json.dumps(summary, indent=2))
    else:
        print(format_shadow_report(summary))
    return 0
