#!/usr/bin/env python3
"""PostToolUse compression for Claude Code built-in tools (Grep, Glob, WebFetch, WebSearch).

Compresses oversized built-in tool results after execution via Claude Code's
``hookSpecificOutput.updatedToolOutput`` mechanism, preserving each tool's
exact Zod ``outputSchema`` (verified against Claude Code v2.1.330):

  - ``Grep`` (>4 KB): grouped by file, per-file counts, top hits with line
    numbers, total matches/files, plus ``files_with_matches`` and ``count`` modes.
  - ``Glob`` (>4 KB): directory-grouped summary, extension counts, total files.
  - ``WebFetch`` / ``WebSearch`` (>6 KB): strips repeated nav/footer/cookie
    boilerplate, keeps headings, query-relevant links/citations, and dense body.

Safety invariants (all enforced):
  - Fail-open: any exception returns None / False -> original output untouched.
  - Never drops errors, warnings, HTTP status codes, file:line locations, or
    credential redaction (operates on already-redacted text).
  - Leaves short outputs (< threshold or < 15 lines for line tools) untouched.
  - Always includes an ``expand`` pointer referencing the full raw archive
    (with ``Full result archived`` marker for ``archive_verify``).
  - Respects ``TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT`` (Codex / hosts without
    ``updatedToolOutput``) and ``TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_TOOLS``.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any

BUILTIN_COMPRESS_TOOLS = frozenset({"Grep", "Glob", "WebFetch", "WebSearch"})

GREP_THRESHOLD = 4096
GLOB_THRESHOLD = 4096
WEB_THRESHOLD = 6144
MIN_LINES_LINE_TOOLS = 15
MIN_SAVINGS_RATIO = 0.15
MIN_SAVINGS_CHARS = 200

_GREP_MAX_FILES = 15
_GREP_MAX_HITS_PER_FILE = 3
_GREP_MAX_LISTED_REMAINING = 50
_GREP_MAX_LINE_CHARS = 200
_GREP_COUNT_TOP_FILES = 30
_GLOB_MAX_DIRS = 25
_GLOB_INLINE_MAX_FILES = 4
_WEB_MAX_BODY_CHARS = 2800
_WEB_MAX_HEADINGS = 35
_WEB_MAX_LINKS = 15

# Patterns for Grep output lines:
#   path/to/file.py:42:matched text   (standard rg -n / Grep content mode)
#   path/to/file.py-41-context text   (rg context lines)
#   C:\path\file.py:42:matched text   (Windows paths)
_GREP_LINE_RE = re.compile(
    r"^((?:[A-Za-z]:)?[^:\n]+?)(?:([:-])(\d+)\2|:)(.*)$"
)
_GREP_COUNT_RE = re.compile(r"^((?:[A-Za-z]:)?[^:\n]+):(\d+)\s*$")

_ERROR_WARN_RE = re.compile(
    r"(?:^|\b)(?:error|fatal|warning|warn|panic|exception|traceback|"
    r"permission denied|no such file|timed out|timeout|rate limit|"
    r"403 forbidden|404 not found|429 too many|500 internal|502 bad gateway|"
    r"503 service unavailable|504 gateway|http/\d(?:\.\d)?\s+[45]\d\d|"
    r"status(?:\s+code)?:\s*[45]\d\d|captcha|access denied)\b",
    re.IGNORECASE,
)

_BOILERPLATE_LINE_RE = re.compile(
    r"^(?:"
    r"skip to (?:main )?content|skip navigation|toggle navigation|open menu|close menu|"
    r"sign in|log in|sign up|create account|my account|forgot password|"
    r"accept all cookies|accept cookies|reject all|cookie settings|cookie policy|"
    r"manage cookie preferences|we use cookies.*|"
    r"privacy policy|terms of service|terms of use|terms & conditions|"
    r"all rights reserved\.?|copyright\s*(?:©|\(c\))?.*|"
    r"subscribe to (?:our )?newsletter|follow us on.*|share this.*|"
    r"back to top|scroll to top|loading\.\.\.|menu|search|home|about|contact|pricing|blog|careers|help|faq"
    r")$",
    re.IGNORECASE,
)

_MD_HEADING_RE = re.compile(r"^#{1,6}\s+\S")
_MD_LINK_RE = re.compile(r"\[([^\]]{1,120})\]\((https?://[^)\s]+)\)")
_BARE_URL_RE = re.compile(r"https?://[^\s<>\"')]+")
_NAV_LINK_ONLY_RE = re.compile(
    r"^\s*(?:[-*+•|]\s*)?(?:\[[^\]]{1,50}\]\([^)]+\)\s*(?:[|·•/]\s*)?)+$"
)

_STOP_WORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
    "have", "has", "had", "not", "but", "what", "when", "where", "which", "who",
    "how", "why", "can", "will", "should", "could", "would", "about", "into",
    "your", "our", "their", "they", "them", "its", "all", "any", "some", "more",
    "most", "other", "than", "then", "also", "just", "only", "very", "here",
    "there", "page", "docs", "documentation", "http", "https", "www", "com",
    "org", "html", "summarize", "explain", "extract", "find", "get", "read",
})


def _updated_tool_output_disabled() -> bool:
    val = (os.environ.get("TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT") or "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _threshold_for(tool_name: str) -> int:
    if tool_name in ("WebFetch", "WebSearch"):
        return WEB_THRESHOLD
    return GREP_THRESHOLD


def _is_error_or_warning(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith(("rg:", "grep:", "Error:", "Warning:", "FATAL:", "ERROR:")):
        return True
    return bool(_ERROR_WARN_RE.search(stripped))


def _truncate_line(line: str, max_chars: int = _GREP_MAX_LINE_CHARS) -> str:
    if len(line) <= max_chars:
        return line
    return line[: max_chars - 3] + "..."


def _ext_label(filename: str) -> str:
    base = filename.rsplit("/", 1)[-1]
    if "." in base and not base.startswith("."):
        return "." + base.rsplit(".", 1)[-1].lower()
    if base.startswith(".") and base.count(".") >= 2:
        return "." + base.rsplit(".", 1)[-1].lower()
    return "(no ext)"


def _format_ext_counts(exts: Counter[str], limit: int = 6) -> str:
    parts = [f"{ext}: {cnt}" for ext, cnt in exts.most_common(limit)]
    if len(exts) > limit:
        parts.append(f"+{len(exts) - limit} more")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Grep compressor
# ---------------------------------------------------------------------------

def compress_grep_content(
    text: str,
    *,
    num_files: int | None = None,
    num_matches: int | None = None,
) -> str | None:
    """Compress Grep content-mode output (>4 KB) grouped by file with line numbers."""
    if not text or len(text) < GREP_THRESHOLD:
        return None
    lines = text.splitlines()
    if len(lines) < MIN_LINES_LINE_TOOLS:
        return None

    files: OrderedDict[str, list[str]] = OrderedDict()
    errors: list[str] = []
    binaries: list[str] = []
    other: list[str] = []

    for ln in lines:
        if not ln or ln == "--":
            continue
        if ln.startswith("Binary file ") and " matches" in ln:
            binaries.append(ln)
            continue
        if ln.startswith(("rg:", "grep:")):
            errors.append(ln)
            continue
        m = _GREP_LINE_RE.match(ln)
        if m:
            fname = m.group(1)
            sep = m.group(2)
            lno = m.group(3)
            # Context lines use '-' separator; keep real match lines (':' or no sep) first,
            # but still count and preserve file grouping.
            if sep == "-" and lno is not None:
                continue
            files.setdefault(fname, []).append(_truncate_line(ln))
        else:
            if _is_error_or_warning(ln):
                errors.append(_truncate_line(ln))
            else:
                other.append(_truncate_line(ln))

    if not files:
        return None

    parsed_matches = sum(len(v) for v in files.values())
    total_files = max(len(files), int(num_files)) if num_files is not None else len(files)
    total_matches = max(parsed_matches, int(num_matches)) if num_matches is not None else parsed_matches

    out: list[str] = [
        f"[Grep results: {total_matches:,} matches across {total_files:,} files "
        f"(grouped by file; showing up to {_GREP_MAX_HITS_PER_FILE} hits/file "
        f"for first {_GREP_MAX_FILES} files).]"
    ]

    file_items = list(files.items())
    for fname, hits in file_items[:_GREP_MAX_FILES]:
        out.append(f"{fname} ({len(hits)} matches):")
        for h in hits[:_GREP_MAX_HITS_PER_FILE]:
            out.append(f"  {h}")
        if len(hits) > _GREP_MAX_HITS_PER_FILE:
            out.append(f"  ... +{len(hits) - _GREP_MAX_HITS_PER_FILE} more matches in {fname}")

    remaining = file_items[_GREP_MAX_FILES:]
    if remaining:
        out.append(f"Other matching files ({len(remaining)} more files, counts):")
        for fname, hits in remaining[:_GREP_MAX_LISTED_REMAINING]:
            out.append(f"  {fname}: {len(hits)} matches")
        if len(remaining) > _GREP_MAX_LISTED_REMAINING:
            extra = remaining[_GREP_MAX_LISTED_REMAINING:]
            extra_matches = sum(len(h) for _, h in extra)
            out.append(f"  ... +{len(extra)} more files ({extra_matches:,} matches)")

    if binaries:
        out.extend(binaries[:20])
        if len(binaries) > 20:
            out.append(f"... +{len(binaries) - 20} more binary matches")

    if errors:
        out.append("Errors/warnings:")
        out.extend(errors[:20])

    if other and len(other) <= 10:
        out.extend(other)

    return "\n".join(out)


def compress_grep_count(text: str) -> str | None:
    """Compress Grep count-mode output (file:count lines)."""
    if not text or len(text) < GREP_THRESHOLD:
        return None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < MIN_LINES_LINE_TOOLS:
        return None

    counts: list[tuple[str, int]] = []
    errors: list[str] = []
    for ln in lines:
        m = _GREP_COUNT_RE.match(ln)
        if m:
            counts.append((m.group(1), int(m.group(2))))
        elif _is_error_or_warning(ln):
            errors.append(_truncate_line(ln))
    if not counts:
        return None

    counts.sort(key=lambda item: (-item[1], item[0]))
    total_matches = sum(c for _, c in counts)
    out = [
        f"[Grep counts: {total_matches:,} total matches across {len(counts):,} files "
        f"(showing top {_GREP_COUNT_TOP_FILES} by count).]"
    ]
    for fname, cnt in counts[:_GREP_COUNT_TOP_FILES]:
        out.append(f"  {fname}: {cnt}")
    if len(counts) > _GREP_COUNT_TOP_FILES:
        rem = counts[_GREP_COUNT_TOP_FILES:]
        out.append(f"  ... +{len(rem)} more files ({sum(c for _, c in rem):,} matches)")
    if errors:
        out.append("Errors/warnings:")
        out.extend(errors[:20])
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Glob (and Grep files_with_matches) compressor
# ---------------------------------------------------------------------------

def compress_glob_paths(
    paths: list[str],
    *,
    total_matches: int | None = None,
    truncated: bool = False,
    label: str = "Glob",
) -> list[str] | None:
    """Compress a list of file paths into directory-grouped summary + extension counts."""
    if not paths or len(paths) < MIN_LINES_LINE_TOOLS:
        return None
    raw_chars = sum(len(p) + 1 for p in paths)
    if raw_chars < GLOB_THRESHOLD:
        return None

    groups: dict[str, list[str]] = {}
    ext_counts: Counter[str] = Counter()
    errors: list[str] = []

    for raw_p in paths:
        p = str(raw_p).strip()
        if not p:
            continue
        if _is_error_or_warning(p) and "/" not in p and "\\" not in p:
            errors.append(_truncate_line(p))
            continue
        norm = p.replace("\\", "/")
        if "/" in norm:
            parent, base = norm.rsplit("/", 1)
            parent = parent or "/"
        else:
            parent, base = ".", norm
        groups.setdefault(parent, []).append(base)
        ext_counts[_ext_label(base)] += 1

    if not groups:
        return None

    total_files = sum(len(v) for v in groups.values())
    total_note = f"{total_files:,}"
    if total_matches is not None and total_matches > total_files:
        total_note = f"{total_files:,} shown of {total_matches:,} total"
    trunc_note = " (truncated)" if truncated else ""

    out: list[str] = [
        f"[{label} results: {total_note} files across {len(groups):,} directories{trunc_note} "
        f"| extensions: {_format_ext_counts(ext_counts)}]"
    ]

    sorted_dirs = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for dpath, names in sorted_dirs[:_GLOB_MAX_DIRS]:
        prefix = dpath if dpath.endswith("/") else f"{dpath}/"
        if len(names) <= _GLOB_INLINE_MAX_FILES:
            out.append(f"  {prefix} ({len(names)}): {', '.join(names)}")
        else:
            dir_exts = Counter(_ext_label(n) for n in names)
            sample = ", ".join(names[:3])
            out.append(
                f"  {prefix} ({len(names)} files: {_format_ext_counts(dir_exts, limit=4)}) "
                f"e.g. {sample}"
            )

    if len(sorted_dirs) > _GLOB_MAX_DIRS:
        rem_dirs = sorted_dirs[_GLOB_MAX_DIRS:]
        rem_files = sum(len(v) for _, v in rem_dirs)
        out.append(f"  ... +{len(rem_dirs)} more directories ({rem_files:,} files)")

    if errors:
        out.append("Errors/warnings:")
        out.extend(errors[:15])

    return out


# ---------------------------------------------------------------------------
# WebFetch / WebSearch compressor
# ---------------------------------------------------------------------------

def _extract_query_terms(query: str, url: str = "") -> set[str]:
    terms: set[str] = set()
    for src in (query or "", url or ""):
        for tok in re.findall(r"[A-Za-z0-9_-]{3,}", src.lower()):
            if tok not in _STOP_WORDS and not tok.isdigit():
                terms.add(tok)
    return terms


def _is_boilerplate_line(stripped: str, line_counts: Counter[str]) -> bool:
    if not stripped:
        return False
    if _MD_HEADING_RE.match(stripped) or _is_error_or_warning(stripped):
        return False
    if len(stripped) <= 90 and _BOILERPLATE_LINE_RE.match(stripped):
        return True
    norm = re.sub(r"\s+", " ", stripped).lower()
    if len(norm) <= 80 and line_counts.get(norm, 0) >= 3:
        return True
    return False


def compress_web_text(
    text: str,
    *,
    query: str = "",
    url: str = "",
) -> str | None:
    """Compress WebFetch/WebSearch text (>6 KB): drop boilerplate, keep headings, relevant links, dense body."""
    if not text or len(text) < WEB_THRESHOLD:
        return None

    raw_lines = text.splitlines()
    query_terms = _extract_query_terms(query, url)

    # Count normalized short lines to detect repeated nav/footer/sidebar chrome.
    line_counts: Counter[str] = Counter()
    for ln in raw_lines:
        s = re.sub(r"\s+", " ", ln.strip()).lower()
        if s and len(s) <= 80:
            line_counts[s] += 1

    # Pass 1: filter boilerplate & collapse long nav-link runs.
    cleaned_lines: list[str] = []
    nav_run: list[str] = []
    errors: list[str] = []
    headings: list[str] = []
    links: OrderedDict[str, str] = OrderedDict()

    def _flush_nav_run() -> None:
        nonlocal nav_run
        if not nav_run:
            return
        if len(nav_run) >= 4:
            # Keep any link in the nav run that matches query terms.
            kept = [
                ln for ln in nav_run
                if query_terms and any(t in ln.lower() for t in query_terms)
            ]
            if kept:
                cleaned_lines.extend(kept[:4])
        else:
            cleaned_lines.extend(nav_run)
        nav_run = []

    for ln in raw_lines:
        stripped = ln.strip()
        if not stripped:
            _flush_nav_run()
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue

        if _is_error_or_warning(stripped):
            _flush_nav_run()
            errors.append(_truncate_line(stripped, 240))
            cleaned_lines.append(stripped)
            continue

        if _MD_HEADING_RE.match(stripped):
            _flush_nav_run()
            if len(headings) < _WEB_MAX_HEADINGS:
                headings.append(_truncate_line(stripped, 140))
            cleaned_lines.append(stripped)
            continue

        # Collect markdown links before filtering nav chrome so query-relevant links survive.
        for label, href in _MD_LINK_RE.findall(stripped):
            if href not in links:
                links[href] = label.strip()

        if _is_boilerplate_line(stripped, line_counts):
            continue

        if _NAV_LINK_ONLY_RE.match(stripped) and len(stripped) <= 160:
            nav_run.append(stripped)
            continue

        _flush_nav_run()
        cleaned_lines.append(stripped)

    _flush_nav_run()

    # Split cleaned text into paragraphs and score by query relevance + density.
    paragraphs: list[str] = []
    buf: list[str] = []
    for ln in cleaned_lines:
        if not ln:
            if buf:
                paragraphs.append("\n".join(buf))
                buf = []
        else:
            buf.append(ln)
    if buf:
        paragraphs.append("\n".join(buf))

    if not paragraphs and not headings and not errors:
        return None

    scored: list[tuple[int, float, str]] = []
    for idx, para in enumerate(paragraphs):
        low = para.lower()
        q_hits = sum(1 for t in query_terms if t in low) if query_terms else 0
        is_err = 1 if _is_error_or_warning(para) else 0
        is_head = 1 if _MD_HEADING_RE.match(para) else 0
        # Prefer errors, query-matching paragraphs, headings, and early dense paragraphs.
        lead_bonus = 2.0 if idx < 3 else (0.5 if idx < 8 else 0.0)
        density = min(len(para), 400) / 400.0
        score = is_err * 100.0 + q_hits * 10.0 + is_head * 3.0 + lead_bonus + density
        scored.append((idx, score, para))

    # Select top-scoring paragraphs within budget, then restore document order.
    selected_indices: set[int] = set()
    budget = _WEB_MAX_BODY_CHARS
    for idx, _score, para in sorted(scored, key=lambda item: (-item[1], item[0])):
        if len(para) <= budget or not selected_indices:
            selected_indices.add(idx)
            budget -= len(para) + 2
        if budget <= 0:
            break

    body_parts: list[str] = []
    last_idx = -1
    for idx in sorted(selected_indices):
        if last_idx != -1 and idx > last_idx + 1:
            body_parts.append("...")
        para = paragraphs[idx]
        if len(para) > 900 and not _is_error_or_warning(para):
            para = para[:897] + "..."
        body_parts.append(para)
        last_idx = idx

    # Pick relevant links (query-matching first, then top non-boilerplate links).
    relevant_links: list[tuple[str, str]] = []
    fallback_links: list[tuple[str, str]] = []
    for href, label in links.items():
        hay = f"{label} {href}".lower()
        if query_terms and any(t in hay for t in query_terms):
            relevant_links.append((label, href))
        elif len(label) >= 4 and not _BOILERPLATE_LINE_RE.match(label):
            fallback_links.append((label, href))
    chosen_links = (relevant_links + fallback_links)[:_WEB_MAX_LINKS]

    out: list[str] = []
    source_note = f" from {url}" if url else ""
    out.append(
        f"[Web content compressed{source_note}: {len(text):,} chars -> preview "
        f"(repeated nav/footer stripped; headings, relevant links & dense body kept).]"
    )
    if errors:
        out.append("Errors/status:")
        for e in list(OrderedDict.fromkeys(errors))[:10]:
            out.append(f"  {e}")
    if headings:
        out.append("Headings:")
        for h in headings[:20]:
            out.append(f"  {h}")
    if chosen_links:
        out.append("Links:")
        for label, href in chosen_links:
            out.append(f"  - [{label}]({href})")
    if body_parts:
        out.append("")
        out.extend(body_parts)

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Claude Code schema-preserving updatedToolOutput builder
# ---------------------------------------------------------------------------

def _archive_footer(
    original_char_count: int,
    char_count: int,
    tool_use_id: str,
    tool_name: str,
    expand_instruction: str,
    truncated: bool,
) -> str:
    if truncated:
        return (
            f"\n\n[Full result archived ({original_char_count:,} chars, truncated to 5MB) — "
            f"saved to disk, not lost.\n{expand_instruction}\n]"
        )
    return (
        f"\n\n[Full result archived ({char_count:,} chars) — saved to disk, not lost.\n"
        f"{expand_instruction}\n]"
    )


def _passes_savings_gate(original_chars: int, replacement_chars: int) -> bool:
    if replacement_chars >= original_chars:
        return False
    saved = original_chars - replacement_chars
    if saved < MIN_SAVINGS_CHARS:
        return False
    return (saved / max(original_chars, 1)) >= MIN_SAVINGS_RATIO


def _compress_websearch_results(
    raw_response: dict[str, Any],
    query: str,
    footer: str,
    redact_fn,
) -> tuple[dict[str, Any], int] | None:
    """Compress WebSearch ``results`` while preserving Zod schema ``sl`` and citations."""
    results = raw_response.get("results")
    if not isinstance(results, list) or not results:
        return None

    new_results: list[Any] = []
    changed = False
    for item in results:
        if isinstance(item, str):
            safe_item = redact_fn(item) if redact_fn else item
            if len(safe_item) >= WEB_THRESHOLD:
                comp = compress_web_text(safe_item, query=query)
                if comp and len(comp) < len(safe_item):
                    new_results.append(comp)
                    changed = True
                    continue
            new_results.append(safe_item)
        elif isinstance(item, dict) and isinstance(item.get("content"), list):
            # Preserve {tool_use_id, content: [{title, url, ...}]} so citations stay valid,
            # while trimming oversized per-hit snippet/text fields if present.
            new_hits = []
            for hit in item["content"]:
                if isinstance(hit, dict):
                    hit_copy = dict(hit)
                    for text_key in ("snippet", "text", "content", "page_text"):
                        val = hit_copy.get(text_key)
                        if isinstance(val, str) and len(val) > 600:
                            safe_val = redact_fn(val) if redact_fn else val
                            comp_val = compress_web_text(safe_val, query=query) if len(safe_val) >= WEB_THRESHOLD else None
                            hit_copy[text_key] = comp_val if (comp_val and len(comp_val) < len(safe_val)) else (safe_val[:500] + "...")
                            changed = True
                    new_hits.append(hit_copy)
                else:
                    new_hits.append(hit)
            item_copy = dict(item)
            item_copy["content"] = new_hits
            new_results.append(item_copy)
        else:
            new_results.append(item)

    if not changed:
        return None

    new_results.append(footer.strip())
    updated = dict(raw_response)
    updated["results"] = new_results
    rep_chars = len(json.dumps(updated, ensure_ascii=False))
    return updated, rep_chars


def build_builtin_updated_output(
    tool_name: str,
    raw_response: Any,
    safe_response: str,
    tool_input: dict[str, Any] | None,
    tool_use_id: str,
    expand_instruction: str,
    original_char_count: int,
    char_count: int,
    truncated: bool = False,
    redact_fn=None,
) -> tuple[dict[str, Any], int] | None:
    """Build a schema-valid ``updatedToolOutput`` dict for a built-in tool, or None.

    Returns ``(updated_tool_output_dict, replacement_char_count)`` when eligible
    and meaningfully smaller; returns ``None`` to fail open.
    """
    if tool_name not in BUILTIN_COMPRESS_TOOLS:
        return None
    if not isinstance(raw_response, dict):
        return None
    if original_char_count < _threshold_for(tool_name):
        return None

    t_input = tool_input if isinstance(tool_input, dict) else {}
    footer = _archive_footer(
        original_char_count, char_count, tool_use_id, tool_name, expand_instruction, truncated
    )

    if tool_name == "Grep":
        mode = raw_response.get("mode")
        if not isinstance(raw_response.get("filenames"), list) or not isinstance(raw_response.get("numFiles"), int):
            return None
        content = raw_response.get("content")
        if isinstance(content, str) and len(content) >= GREP_THRESHOLD:
            safe_content = redact_fn(content) if redact_fn else content
            if mode == "count":
                comp = compress_grep_count(safe_content)
            else:
                comp = compress_grep_content(
                    safe_content,
                    num_files=raw_response.get("numFiles"),
                    num_matches=raw_response.get("numMatches"),
                )
            if not comp:
                return None
            new_content = comp + footer
            if not _passes_savings_gate(len(content), len(new_content)):
                return None
            updated = dict(raw_response)
            updated["content"] = new_content
            if len(updated["filenames"]) > 50:
                updated["filenames"] = list(updated["filenames"][:50])
            return updated, len(new_content)
        # files_with_matches mode (filenames list carries the bulk)
        filenames = raw_response.get("filenames") or []
        if filenames:
            safe_names = [redact_fn(str(f)) if redact_fn else str(f) for f in filenames]
            summary_lines = compress_glob_paths(
                safe_names,
                total_matches=raw_response.get("totalFiles") or raw_response.get("numFiles"),
                label="Grep files",
            )
            if not summary_lines:
                return None
            new_filenames = summary_lines + [footer.strip()]
            orig_len = sum(len(str(f)) + 1 for f in filenames)
            rep_len = sum(len(ln) + 1 for ln in new_filenames)
            if not _passes_savings_gate(orig_len, rep_len):
                return None
            updated = dict(raw_response)
            updated["filenames"] = new_filenames
            return updated, rep_len
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
        orig_len = sum(len(str(f)) + 1 for f in filenames)
        rep_len = sum(len(ln) + 1 for ln in new_filenames)
        if not _passes_savings_gate(orig_len, rep_len):
            return None
        updated = dict(raw_response)
        updated["filenames"] = new_filenames
        return updated, rep_len

    if tool_name == "WebFetch":
        result_text = raw_response.get("result")
        if (
            not isinstance(result_text, str)
            or len(result_text) < WEB_THRESHOLD
            or not isinstance(raw_response.get("bytes"), (int, float))
            or not isinstance(raw_response.get("code"), int)
            or not isinstance(raw_response.get("codeText"), str)
            or not isinstance(raw_response.get("url"), str)
            or not isinstance(raw_response.get("durationMs"), (int, float))
        ):
            return None
        safe_text = redact_fn(result_text) if redact_fn else result_text
        query = str(t_input.get("prompt") or "")
        url = str(raw_response.get("url") or t_input.get("url") or "")
        comp = compress_web_text(safe_text, query=query, url=url)
        if not comp:
            return None
        new_result = comp + footer
        if not _passes_savings_gate(len(result_text), len(new_result)):
            return None
        updated = dict(raw_response)
        updated["result"] = new_result
        return updated, len(new_result)

    if tool_name == "WebSearch":
        if (
            not isinstance(raw_response.get("query"), str)
            or not isinstance(raw_response.get("results"), list)
            or not isinstance(raw_response.get("durationSeconds"), (int, float))
        ):
            return None
        query = str(raw_response.get("query") or t_input.get("query") or "")
        res = _compress_websearch_results(raw_response, query, footer, redact_fn)
        if res is None:
            return None
        updated, rep_chars = res
        if not _passes_savings_gate(original_char_count, rep_chars):
            return None
        return updated, rep_chars

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
    expand_instruction: str,
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
        if is_exempt_fn and is_exempt_fn(tool_name):
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

        updated_output, replacement_chars = built
        original_tokens = int(original_char_count / code_chars_per_token)
        replacement_tokens = int(replacement_chars / code_chars_per_token)
        tokens_saved = max(0, original_tokens - replacement_tokens)
        detail = f"replaced {tool_name} ({original_char_count:,} chars -> {replacement_chars:,} chars)"
        if log_savings_event_fn is not None:
            log_savings_event_fn("tool_archive", tokens_saved, session_id=session_id, detail=detail)

        try:
            import archive_verify  # noqa: PLC0415
            archive_verify.verify(trends_db, session_id)
            if tokens_saved > 0:
                archive_verify.record(
                    trends_db,
                    session_id,
                    raw_tool_use_id,
                    tool_name,
                    original_char_count,
                    replacement_chars,
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
    if tool == "Grep":
        mode = fixture.get("mode", "content")
        if mode == "count":
            return compress_grep_count(raw) or raw
        if mode == "files_with_matches":
            lines = compress_glob_paths(raw.splitlines(), label="Grep files")
            return "\n".join(lines) if lines else raw
        return compress_grep_content(raw) or raw
    if tool == "Glob":
        lines = compress_glob_paths(raw.splitlines(), truncated=bool(fixture.get("truncated", False)))
        return "\n".join(lines) if lines else raw
    if tool in ("WebFetch", "WebSearch"):
        return compress_web_text(
            raw,
            query=str(fixture.get("query", "")),
            url=str(fixture.get("url", "")),
        ) or raw
    return raw
