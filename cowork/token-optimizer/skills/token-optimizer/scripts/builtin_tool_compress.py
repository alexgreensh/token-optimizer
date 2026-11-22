#!/usr/bin/env python3
"""PostToolUse compression for Claude Code built-in tools (Grep, Glob).

Compresses oversized built-in tool results after execution via Claude Code's
``hookSpecificOutput.updatedToolOutput`` mechanism, preserving each tool's
exact Zod ``outputSchema`` (verified against Claude Code v2.1.330):

  - ``Grep`` (>4 KB, >=15 lines, multi-file, no context/head_limit/offset):
    LOSSLESS on every ``file:line`` location. Factors out repeated file paths
    and trims only overly long matched line text with a clear marker.
    ``files_with_matches`` and ``count`` modes factor out shared directory
    prefixes losslessly (every file preserved).
  - ``Glob`` (>4 KB, >=15 paths): LOSSLESS directory-prefix grouping (dir
    prefix once, then every basename; ``numFiles`` matches the listed count).
    Passes through untouched if compression saves under 25%.

Safety invariants (all enforced):
  - Fail-open: any unparseable line or exception -> original output untouched.
  - Zero location loss: every ``file:line`` (Grep) and every file path (Glob)
    is preserved; errors, warnings, and binary match lines stay verbatim.
  - Passes through untouched for short outputs, single-file Grep searches, and
    Grep calls with context flags (``-A``/``-B``/``-C``/``context``) or
    ``head_limit`` / ``offset``.
  - Always includes an ``expand`` pointer referencing the full raw archive
    (with ``Full result archived`` marker for ``archive_verify``).
  - Version gate + session self-disable: gates on known-good Claude Code
    versions when available, and if the host rejects a replacement once in a
    session (``archive_verify`` verdict ``full``), logs one warning and
    disables built-in replacements for the rest of the session.
  - Respects ``TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT`` and exempt tools.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any

BUILTIN_COMPRESS_TOOLS = frozenset({"Grep", "Glob"})
BUILTIN_EVENT_TYPES = {"Grep": "builtin_grep", "Glob": "builtin_glob"}

GREP_THRESHOLD = 4096
GLOB_THRESHOLD = 4096
MIN_LINES_LINE_TOOLS = 15
MIN_SAVINGS_RATIO = 0.15
MIN_GLOB_SAVINGS_RATIO = 0.25
MIN_SAVINGS_CHARS = 200

_GREP_MAX_LINE_CHARS = 160
_MIN_CLAUDE_CODE_VERSION = (2, 1, 0)

_GREP_PASSTHROUGH_INPUT_KEYS = (
    "-A", "-B", "-C", "context", "head_limit", "offset",
    "A", "B", "C", "after_context", "before_context",
)

# Strict match-line patterns (colon separator only; never matches `-<digits>-`
# inside dated filenames like `docs/notes/2026-11-22-release-plan.md`).
# Supports optional Windows drive prefix (`C:\...` or `C:/...`) and arbitrary
# colons in the matched line text.
_GREP_CONTENT_LINE_RE = re.compile(r"^((?:[A-Za-z]:)?[^:\n]+):(\d+):(.*)$")
_GREP_COUNT_LINE_RE = re.compile(r"^((?:[A-Za-z]:)?[^:\n]+):(\d+)\s*$")

_ERROR_WARN_RE = re.compile(
    r"(?:^|\b)(?:error|fatal|warning|warn|panic|exception|traceback|"
    r"permission denied|no such file|timed out|timeout|access denied)\b",
    re.IGNORECASE,
)

_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_AI_AGENT_CLAUDE_RE = re.compile(r"claude-code[_-](\d+)\.(\d+)\.(\d+)")


def _updated_tool_output_disabled() -> bool:
    val = (os.environ.get("TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT") or "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _is_error_or_warning(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith(("rg:", "grep:", "Error:", "Warning:", "FATAL:", "ERROR:")):
        return True
    return bool(_ERROR_WARN_RE.search(stripped))


def _trim_match_text(body: str, max_chars: int = _GREP_MAX_LINE_CHARS) -> str:
    """Trim overly long matched line text with a clear marker; never trim errors."""
    if len(body) <= max_chars or _is_error_or_warning(body):
        return body
    extra = len(body) - max_chars
    return f"{body[:max_chars]}... [trimmed, +{extra} chars]"


def _passes_savings_gate(
    original_chars: int,
    replacement_chars: int,
    *,
    min_ratio: float = MIN_SAVINGS_RATIO,
) -> bool:
    if replacement_chars >= original_chars:
        return False
    saved = original_chars - replacement_chars
    if saved < MIN_SAVINGS_CHARS:
        return False
    return (saved / max(original_chars, 1)) >= min_ratio


def _has_grep_passthrough_flags(tool_input: dict[str, Any] | None) -> bool:
    """True when Grep tool_input sets any context flag, head_limit, or offset."""
    if not isinstance(tool_input, dict):
        return False
    for key in _GREP_PASSTHROUGH_INPUT_KEYS:
        val = tool_input.get(key)
        if val not in (None, False, 0, ""):
            return True
    return False


# ---------------------------------------------------------------------------
# Version gate & per-session self-disable on host schema rejection
# ---------------------------------------------------------------------------

def _detect_claude_code_version(hook_input: dict[str, Any] | None) -> tuple[int, int, int] | None:
    """Extract Claude Code semver tuple when exposed to the hook, else None."""
    for env_key in ("CLAUDE_CODE_VERSION", "CLAUDE_VERSION"):
        raw = (os.environ.get(env_key) or "").strip()
        if raw:
            m = _SEMVER_RE.search(raw)
            if m:
                return int(m.group(1)), int(m.group(2)), int(m.group(3))

    ai_agent = (os.environ.get("AI_AGENT") or "").strip()
    if ai_agent:
        m = _AI_AGENT_CLAUDE_RE.search(ai_agent)
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3))

    if isinstance(hook_input, dict):
        for key in ("claude_code_version", "version"):
            raw = hook_input.get(key)
            if isinstance(raw, str) and raw.strip():
                m = _SEMVER_RE.search(raw)
                if m:
                    return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


def _is_version_supported(hook_input: dict[str, Any] | None) -> bool:
    """Gate on known-good Claude Code versions when version is available."""
    ver = _detect_claude_code_version(hook_input)
    if ver is None:
        return True
    return ver >= _MIN_CLAUDE_CODE_VERSION


def _safe_sid(session_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")[:128]
    return cleaned or "unknown"


def _session_disable_marker(trends_db: Path, session_id: str) -> Path:
    return Path(trends_db).parent / "tool-archive" / _safe_sid(session_id) / ".builtin_compress_disabled"


def _check_and_update_session_rejection(trends_db: Path, session_id: str) -> bool:
    """Return True if built-in replacements are disabled for this session.

    Checks the fast marker file first; otherwise runs ``archive_verify.verify``
    and disables the rest of the session on the first ``full`` verdict for a
    built-in tool, logging one warning to stderr.
    """
    if not session_id:
        return False
    marker = _session_disable_marker(trends_db, session_id)
    try:
        if marker.is_file():
            return True
    except OSError:
        pass

    try:
        import archive_verify  # noqa: PLC0415
        archive_verify.verify(trends_db, session_id)
    except Exception:
        return False

    rejected = False
    conn = None
    try:
        if not Path(trends_db).is_file():
            return False
        conn = sqlite3.connect(str(trends_db), timeout=0.2)
        row = conn.execute(
            "SELECT 1 FROM archive_verifications "
            "WHERE session_id=? AND tool_name IN ('Grep', 'Glob') AND verdict='full' LIMIT 1",
            (session_id,),
        ).fetchone()
        rejected = row is not None
    except Exception:
        return False
    finally:
        if conn is not None:
            conn.close()

    if not rejected:
        return False

    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("disabled: host rejected builtin updatedToolOutput\n", encoding="utf-8")
        os.chmod(marker, 0o600)
    except OSError:
        pass
    try:
        sys.stderr.write(
            f"[Token Optimizer] Built-in tool output replacement was rejected by the host "
            f"in session {session_id}; disabling for the rest of this session.\n"
        )
        sys.stderr.flush()
    except (OSError, ValueError):
        pass
    return True


# ---------------------------------------------------------------------------
# Grep compressors (LOSSLESS on every file:line location; fail-open parser)
# ---------------------------------------------------------------------------

def compress_grep_content(
    text: str,
    *,
    num_files: int | None = None,
    num_matches: int | None = None,
    tool_input: dict[str, Any] | None = None,
) -> str | None:
    """Losslessly compress multi-file Grep content output (>4 KB).

    - Passes through untouched when context flags (-A/-B/-C/context),
      head_limit, or offset are set, or for single-file searches.
    - Fails open (returns None) on ANY non-empty line it cannot parse cleanly
      (including context separators ``--``, context lines, single-file
      ``100:text`` lines, or malformed paths).
    - Preserves EVERY ``file:line`` location; factors out repeated file paths
      and trims only overly long matched line text with a clear marker.
    """
    if _has_grep_passthrough_flags(tool_input):
        return None
    if not text or len(text) < GREP_THRESHOLD:
        return None
    if num_files is not None and int(num_files) <= 1:
        return None

    lines = text.splitlines()
    if len(lines) < MIN_LINES_LINE_TOOLS:
        return None

    files: OrderedDict[str, list[tuple[str, str]]] = OrderedDict()
    errors: list[str] = []
    binaries: list[str] = []

    for ln in lines:
        if not ln:
            continue
        if ln == "--":
            # Context separator: context lines are present -> pass through untouched.
            return None
        if ln.startswith("Binary file ") and " matches" in ln:
            binaries.append(ln)
            continue
        if ln.startswith(("rg:", "grep:")):
            errors.append(ln)
            continue
        m = _GREP_CONTENT_LINE_RE.match(ln)
        if not m:
            # Fail open on any line we cannot parse cleanly.
            return None
        fname, lno, body = m.group(1), m.group(2), m.group(3)
        if not fname.strip() or fname.isdigit():
            # Guard against single-file `100:foo:200:bar` turning a line number into a filename.
            return None
        files.setdefault(fname, []).append((lno, body))

    # Single-file search -> pass through untouched.
    if len(files) <= 1:
        return None

    parsed_matches = sum(len(v) for v in files.values())
    total_files = max(len(files), int(num_files)) if num_files is not None else len(files)
    total_matches = max(parsed_matches, int(num_matches)) if num_matches is not None else parsed_matches

    out: list[str] = [
        f"[Grep results: {total_matches:,} matches across {total_files:,} files "
        f"(all file:line locations preserved; long lines trimmed).]"
    ]

    for fname, hits in files.items():
        out.append(f"{fname} ({len(hits)}):")
        for lno, body in hits:
            out.append(f"  {lno}:{_trim_match_text(body)}")

    if binaries:
        out.extend(binaries)
    if errors:
        out.append("Errors/warnings:")
        out.extend(errors)

    result = "\n".join(out)
    if not _passes_savings_gate(len(text), len(result), min_ratio=MIN_SAVINGS_RATIO):
        return None
    return result


def _split_dir_base(path: str) -> tuple[str, str] | None:
    """Split a path into ``(dir_prefix, basename)`` preserving path separators."""
    p = path.strip()
    if not p:
        return None
    sep = "\\" if ("\\" in p and "/" not in p) else "/"
    if sep in p:
        parent, base = p.rsplit(sep, 1)
        if not base:
            return None
        prefix = (parent + sep) if parent else sep
        return prefix, base
    return "", p


def compress_grep_count(
    text: str,
    *,
    tool_input: dict[str, Any] | None = None,
) -> str | None:
    """Losslessly compress Grep count-mode output (dir prefix once, every file:count)."""
    if _has_grep_passthrough_flags(tool_input):
        return None
    if not text or len(text) < GREP_THRESHOLD:
        return None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < MIN_LINES_LINE_TOOLS:
        return None

    groups: OrderedDict[str, list[tuple[str, int]]] = OrderedDict()
    errors: list[str] = []
    total_files = 0
    total_matches = 0

    for ln in lines:
        if ln.startswith(("rg:", "grep:")):
            errors.append(ln)
            continue
        m = _GREP_COUNT_LINE_RE.match(ln)
        if not m:
            return None
        split = _split_dir_base(m.group(1))
        if split is None:
            return None
        prefix, base = split
        cnt = int(m.group(2))
        groups.setdefault(prefix, []).append((base, cnt))
        total_files += 1
        total_matches += cnt

    if total_files < MIN_LINES_LINE_TOOLS:
        return None

    out: list[str] = [
        f"[Grep counts: {total_matches:,} matches across {total_files:,} files "
        f"({len(groups):,} dirs; all files preserved).]"
    ]
    for prefix, items in groups.items():
        rendered = ", ".join(f"{base}:{cnt}" for base, cnt in items)
        out.append(f"{prefix} {rendered}" if prefix else rendered)
    if errors:
        out.append("Errors/warnings:")
        out.extend(errors)

    result = "\n".join(out)
    if not _passes_savings_gate(len(text), len(result), min_ratio=MIN_GLOB_SAVINGS_RATIO):
        return None
    return result


# ---------------------------------------------------------------------------
# Lossless Glob & Grep files_with_matches compressor
# ---------------------------------------------------------------------------

def compress_glob_paths(
    paths: list[str],
    *,
    total_matches: int | None = None,
    truncated: bool = False,
    label: str = "Glob",
) -> list[str] | None:
    """Losslessly group file paths by directory: dir prefix once, then every basename.

    Every single path is preserved. Returns None (pass through untouched) if
    the grouped representation saves less than 25% vs the raw path list.
    """
    if not paths or len(paths) < MIN_LINES_LINE_TOOLS:
        return None
    raw_chars = sum(len(str(p)) + 1 for p in paths)
    if raw_chars < GLOB_THRESHOLD:
        return None

    groups: OrderedDict[str, list[str]] = OrderedDict()
    listed_count = 0

    for raw_p in paths:
        split = _split_dir_base(str(raw_p))
        if split is None:
            # Fail open on empty or malformed path entry.
            return None
        prefix, base = split
        groups.setdefault(prefix, []).append(base)
        listed_count += 1

    if listed_count != len(paths) or not groups:
        return None

    total_note = f"{listed_count:,}"
    if total_matches is not None and total_matches > listed_count:
        total_note = f"{listed_count:,} of {total_matches:,}"
    trunc_note = " (truncated)" if truncated else ""

    out: list[str] = [
        f"[{label}: {total_note} files across {len(groups):,} dirs{trunc_note}; all paths preserved]"
    ]
    for prefix, names in groups.items():
        joined_names = ", ".join(names)
        out.append(f"{prefix} {joined_names}" if prefix else joined_names)

    rep_chars = sum(len(ln) + 1 for ln in out)
    if not _passes_savings_gate(raw_chars, rep_chars, min_ratio=MIN_GLOB_SAVINGS_RATIO):
        return None
    return out


# ---------------------------------------------------------------------------
# Claude Code schema-preserving updatedToolOutput builder
# ---------------------------------------------------------------------------

def _builtin_expand_instruction(tool_use_id: str) -> str:
    """Build an expand pointer explaining how to retrieve the full archived result."""
    try:
        from refetch_fingerprint import expand_command  # noqa: PLC0415
        cmd = expand_command(tool_use_id)
    except Exception:
        cmd = f"expand {tool_use_id}"
    return f"To view the full archived result, run in Bash:\n    {cmd}"


def _archive_footer(
    original_char_count: int,
    char_count: int,
    tool_use_id: str,
    expand_instruction: str | None,
    truncated: bool,
) -> str:
    instr = expand_instruction or _builtin_expand_instruction(tool_use_id)
    if truncated:
        return (
            f"\n\n[Full result archived ({original_char_count:,} chars, truncated to 5MB) — "
            f"saved to disk, not lost.\n{instr}\n]"
        )
    return (
        f"\n\n[Full result archived ({char_count:,} chars) — saved to disk, not lost.\n"
        f"{instr}\n]"
    )


def build_builtin_updated_output(
    tool_name: str,
    raw_response: Any,
    safe_response: str,
    tool_input: dict[str, Any] | None,
    tool_use_id: str,
    expand_instruction: str | None,
    original_char_count: int,
    char_count: int,
    truncated: bool = False,
    redact_fn=None,
) -> tuple[dict[str, Any], int, int] | None:
    """Build a schema-valid ``updatedToolOutput`` dict for Grep or Glob, or None.

    Returns ``(updated_tool_output_dict, original_field_chars, replacement_field_chars)``
    so measured savings are always computed against the exact field that was
    replaced. Returns ``None`` to fail open.
    """
    if tool_name not in BUILTIN_COMPRESS_TOOLS:
        return None
    if not isinstance(raw_response, dict):
        return None

    t_input = tool_input if isinstance(tool_input, dict) else {}
    footer = _archive_footer(
        original_char_count, char_count, tool_use_id, expand_instruction, truncated
    )

    if tool_name == "Grep":
        if _has_grep_passthrough_flags(t_input):
            return None
        mode = raw_response.get("mode")
        filenames = raw_response.get("filenames")
        num_files = raw_response.get("numFiles")
        if not isinstance(filenames, list) or not isinstance(num_files, int):
            return None

        content = raw_response.get("content")
        if isinstance(content, str) and len(content) >= GREP_THRESHOLD:
            safe_content = redact_fn(content) if redact_fn else content
            if mode == "count":
                comp = compress_grep_count(safe_content, tool_input=t_input)
                min_ratio = MIN_GLOB_SAVINGS_RATIO
            else:
                comp = compress_grep_content(
                    safe_content,
                    num_files=num_files,
                    num_matches=raw_response.get("numMatches"),
                    tool_input=t_input,
                )
                min_ratio = MIN_SAVINGS_RATIO
            if not comp:
                return None
            new_content = comp + footer
            orig_field_chars = len(content)
            rep_field_chars = len(new_content)
            if not _passes_savings_gate(orig_field_chars, rep_field_chars, min_ratio=min_ratio):
                return None
            updated = dict(raw_response)
            updated["content"] = new_content
            return updated, orig_field_chars, rep_field_chars

        # files_with_matches mode (filenames list carries the bulk)
        if filenames:
            safe_names = [redact_fn(str(f)) if redact_fn else str(f) for f in filenames]
            summary_lines = compress_glob_paths(
                safe_names,
                total_matches=raw_response.get("totalFiles") or num_files,
                label="Grep files",
            )
            if not summary_lines:
                return None
            new_filenames = summary_lines + [footer.strip()]
            orig_field_chars = sum(len(str(f)) + 1 for f in filenames)
            rep_field_chars = sum(len(ln) + 1 for ln in new_filenames)
            if not _passes_savings_gate(orig_field_chars, rep_field_chars, min_ratio=MIN_GLOB_SAVINGS_RATIO):
                return None
            updated = dict(raw_response)
            updated["filenames"] = new_filenames
            updated["numFiles"] = len(filenames)
            return updated, orig_field_chars, rep_field_chars
        return None

    if tool_name == "Glob":
        filenames = raw_response.get("filenames")
        if (
            not isinstance(filenames, list)
            or not isinstance(raw_response.get("numFiles"), int)
            or not isinstance(raw_response.get("truncated"), bool)
            or not isinstance(raw_response.get("durationMs"), (int, float))
        ):
            return None
        safe_names = [redact_fn(str(f)) if redact_fn else str(f) for f in filenames]
        summary_lines = compress_glob_paths(
            safe_names,
            total_matches=raw_response.get("totalMatches"),
            truncated=bool(raw_response.get("truncated")),
            label="Glob",
        )
        if not summary_lines:
            return None
        new_filenames = summary_lines + [footer.strip()]
        orig_field_chars = sum(len(str(f)) + 1 for f in filenames)
        rep_field_chars = sum(len(ln) + 1 for ln in new_filenames)
        if not _passes_savings_gate(orig_field_chars, rep_field_chars, min_ratio=MIN_GLOB_SAVINGS_RATIO):
            return None
        updated = dict(raw_response)
        updated["filenames"] = new_filenames
        updated["numFiles"] = len(filenames)
        return updated, orig_field_chars, rep_field_chars

    return None


def maybe_emit_builtin_replacement(
    *,
    tool_name: str,
    raw_response: Any,
    safe_response: str,
    hook_input: dict[str, Any],
    tool_use_id: str,
    raw_tool_use_id: str,
    session_id: str,
    original_char_count: int,
    char_count: int,
    truncated: bool,
    expand_instruction: str | None = None,
    code_chars_per_token: float,
    trends_db: Path,
    log_savings_event_fn,
    is_exempt_fn,
    redact_fn=None,
) -> bool:
    """Compress & emit ``updatedToolOutput`` for eligible built-in tools. Fail-open."""
    try:
        if tool_name not in BUILTIN_COMPRESS_TOOLS:
            return False
        if _updated_tool_output_disabled():
            return False
        if not _is_version_supported(hook_input):
            return False
        if is_exempt_fn and is_exempt_fn(tool_name):
            return False
        if _check_and_update_session_rejection(trends_db, session_id):
            return False

        built = build_builtin_updated_output(
            tool_name=tool_name,
            raw_response=raw_response,
            safe_response=safe_response,
            tool_input=hook_input.get("tool_input"),
            tool_use_id=tool_use_id,
            expand_instruction=expand_instruction,
            original_char_count=original_char_count,
            char_count=char_count,
            truncated=truncated,
            redact_fn=redact_fn,
        )
        if built is None:
            return False

        updated_output, orig_field_chars, rep_field_chars = built
        original_tokens = int(orig_field_chars / code_chars_per_token)
        replacement_tokens = int(rep_field_chars / code_chars_per_token)
        tokens_saved = max(0, original_tokens - replacement_tokens)
        detail = f"replaced {tool_name} ({orig_field_chars:,} chars -> {rep_field_chars:,} chars)"
        event_type = BUILTIN_EVENT_TYPES.get(tool_name, "tool_archive")
        if log_savings_event_fn is not None:
            log_savings_event_fn(event_type, tokens_saved, session_id=session_id, detail=detail)

        try:
            import archive_verify  # noqa: PLC0415
            if tokens_saved > 0:
                archive_verify.record(
                    trends_db,
                    session_id,
                    raw_tool_use_id,
                    tool_name,
                    orig_field_chars,
                    rep_field_chars,
                    hook_input.get("transcript_path"),
                    detail,
                )
        except Exception:
            pass

        envelope = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": updated_output,
            }
        }
        print(json.dumps(envelope))
        return True
    except Exception:
        return False


def compress_benchmark_fixture(fixture: dict[str, Any]) -> str:
    """Run a benchmark.py fixture through the matching built-in compressor."""
    tool = fixture.get("tool", "")
    raw = fixture.get("raw_output", "")
    tool_input = fixture.get("tool_input") if isinstance(fixture.get("tool_input"), dict) else None
    if tool == "Grep":
        mode = fixture.get("mode", "content")
        if mode == "count":
            return compress_grep_count(raw, tool_input=tool_input) or raw
        if mode == "files_with_matches":
            lines = compress_glob_paths(raw.splitlines(), label="Grep files")
            return "\n".join(lines) if lines else raw
        return compress_grep_content(raw, tool_input=tool_input) or raw
    if tool == "Glob":
        lines = compress_glob_paths(raw.splitlines(), truncated=bool(fixture.get("truncated", False)))
        return "\n".join(lines) if lines else raw
    return raw
