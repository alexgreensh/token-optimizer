#!/usr/bin/env python3
"""Token Optimizer - Structured first-read preview for huge non-code files.

When a full Read (no offset/limit) targets a large (>40 KB) non-code file
(lockfiles, logs, CSV/TSV, JSON/JSONL data dumps, minified bundles, generated
non-code artifacts), replaces the raw dump with a bounded structured preview:
  - shape/schema summary and exact row/key/package counts
  - head and tail
  - every error/warning/stack-frame line preserved verbatim for logs/SARIF/JSONL
  - exact ranged Read call to inspect more
  - full original archived behind `expand` (via archive_result.archive_original)

Safety invariants (fail-open everywhere):
  - Only when offset == 0 and limit == 0 (never overrides ranged Reads).
  - Never for source code, prose/Markdown, or editable config files.
  - Never for files edited in the current session (store.was_file_edited).
  - Conservative size floor (> 40 KB) and ceiling (<= 10 MB).
  - Escape hatch: a second full Read of the same file in the session returns
    the whole file untouched.
  - Requires >= 40% token reduction; otherwise serves the full file untouched.
  - Any archive or parsing failure fails open (serves the full file untouched).
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional

from plugin_env import is_v5_flag_enabled

try:
    from token_estimate import estimate_tokens
except ImportError:  # pragma: no cover
    def estimate_tokens(text: str) -> int:
        return max(1, len(text.encode("utf-8", errors="replace")) // 4) if text else 0

# Conservative thresholds
MIN_BYTES = 40 * 1024           # 40 KiB floor for lockfiles/logs/csv/jsonl/minified/generated
JSON_DATA_MIN_BYTES = 64 * 1024 # 64 KiB floor for generic .json data dumps (preserves 16-64KB shadow band)
MAX_BYTES = 10 * 1024 * 1024    # 10 MiB ceiling (hook latency / memory guard)
MIN_SAVINGS_RATIO = 0.40        # Must save at least 40% of tokens
MAX_PREVIEW_CHARS = 6000        # Generous cap for log error preservation
STRICT_PREVIEW_CHARS = 2400     # Tighter cap when hook additionalContext is saved
MIN_LINES_NON_MINIFIED = 15     # Leave short multi-line files untouched

FEATURE_NAME = "big_read_preview"
REPLACEMENT_TYPE = "big_noncode_preview"

# Exact lockfile basenames (lowercased)
LOCKFILE_NAMES = frozenset({
    "package-lock.json",
    "npm-shrinkwrap.json",
    "shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "cargo.lock",
    "uv.lock",
    "poetry.lock",
    "composer.lock",
    "gemfile.lock",
    "go.sum",
    "flake.lock",
    "podfile.lock",
    "pipfile.lock",
    "bun.lock",
    "packages.lock.json",
    "mix.lock",
    "pubspec.lock",
    "paket.lock",
    "pdm.lock",
    "conda-lock.yml",
})

# Minified / bundle suffixes (checked before generic source-code exclusions)
MINIFIED_SUFFIXES = (
    ".min.js",
    ".min.css",
    ".min.mjs",
    ".min.cjs",
    ".bundle.js",
    ".bundle.css",
    ".chunk.js",
    ".chunk.css",
    ".js.map",
    ".css.map",
)

# Generated non-code suffixes
GENERATED_SUFFIXES = frozenset({".snap", ".har", ".sarif", ".lcov"})

# Source code and prose extensions: NEVER intercept
SOURCE_AND_PROSE_SUFFIXES = frozenset({
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts",
    ".go", ".rs", ".java", ".kt", ".kts", ".swift", ".c", ".cc", ".cpp", ".cxx",
    ".h", ".hh", ".hpp", ".cs", ".rb", ".php", ".sh", ".bash", ".zsh", ".fish",
    ".ps1", ".sql", ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".vue", ".svelte", ".astro", ".ex", ".exs", ".erl", ".hs", ".lua",
    ".r", ".jl", ".dart", ".scala", ".clj", ".cljs", ".tf", ".hcl",
    ".proto", ".graphql", ".gql", ".zig", ".nim", ".ml", ".mli", ".fs", ".fsx",
    ".groovy", ".pl", ".pm", ".vb", ".asm", ".s", ".cmake", ".gradle",
    ".nix", ".dhall", ".prisma", ".sol",
    # Prose / documentation / config formats
    ".md", ".mdx", ".rst", ".txt", ".adoc", ".org", ".tex",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties", ".xml",
})

# Editable config basenames (never intercept even if .json and > 40 KB)
CONFIG_JSON_NAMES = frozenset({
    "package.json",
    "tsconfig.json",
    "jsconfig.json",
    "settings.json",
    "settings.local.json",
    "config.json",
    "hooks.json",
    "plugin.json",
    "marketplace.json",
    "composer.json",
    "manifest.json",
    "launch.json",
    "tasks.json",
    "extensions.json",
    "vercel.json",
    "netlify.json",
    "firebase.json",
    "angular.json",
    "nx.json",
    "turbo.json",
    "lerna.json",
    "renovate.json",
    "devcontainer.json",
    "deno.json",
    "deno.jsonc",
    "biome.json",
    "biome.jsonc",
    "babel.config.json",
    "jest.config.json",
    "wrangler.json",
    "wrangler.jsonc",
    "mcp.json",
    ".mcp.json",
    "claude.json",
    ".claude.json",
    ".eslintrc.json",
    ".prettierrc.json",
    "project.json",
    "workspace.json",
    "appsettings.json",
})

# Config directories whose JSON files are usually hand-edited config
CONFIG_DIR_PARTS = frozenset({
    ".claude", ".codex", ".cursor", ".vscode", ".github",
    ".config", ".idea", ".husky", ".circleci",
})

_ROTATED_LOG_RE = re.compile(r"\.log\.\d+(?:-\d+)*$", re.IGNORECASE)
_LOG_LEVEL_RE = re.compile(
    r"\b(FATAL|CRITICAL|SEVERE|ALERT|EMERG|ERROR|ERR|WARN|WARNING|INFO|DEBUG|TRACE)\b"
)
_LOG_IMPORTANT_RE = re.compile(
    r"(?:\b(?:FATAL|CRITICAL|SEVERE|ALERT|EMERG|ERROR|WARN|WARNING|FAILED|PANIC)\b"
    r"|Traceback \(most recent call last\):"
    r"|(?:Exception|Error|Panic):\s"
    r"|^\s+File \"[^\"]+\", line \d+"
    r"|^\s+at\s+\S+\s*\([^)]+:\d+"
    r"|^Caused by:)",
    re.IGNORECASE,
)
_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_ERROR_KEY_RE = re.compile(r"^(?:error|errors|err|warning|warnings|warn|failure|failures|diagnostics?)$", re.IGNORECASE)


def is_big_noncode_read_enabled() -> bool:
    """Feature flag: default OFF (DECISION row 9). Opt in via TOKEN_OPTIMIZER_BIG_NONCODE_READ=1."""
    return is_v5_flag_enabled(
        "v5_big_noncode_read",
        "TOKEN_OPTIMIZER_BIG_NONCODE_READ",
        default=False,
    )


def classify_big_noncode_file(file_path: str) -> Optional[str]:
    """Return non-code file kind or None if the file must NOT be intercepted.

    Categories: 'lockfile', 'minified', 'log', 'csv', 'jsonl', 'json_data', 'generated'.
    Returns None for all source code, prose, credentials, and editable config files.
    """
    p = Path(file_path)
    name = p.name.lower()
    ext = p.suffix.lower()

    # Never touch credential / env files
    if name.startswith(".env"):
        return None

    # 1. Explicit lockfiles (before extension exclusions, e.g. pnpm-lock.yaml, Cargo.lock)
    if name in LOCKFILE_NAMES:
        return "lockfile"

    # 2. Minified bundles & source maps (before .js/.css source-code exclusions)
    if any(name.endswith(suf) for suf in MINIFIED_SUFFIXES):
        return "minified"
    if ext == ".map":
        return "minified"

    # 3. Log files (allowed even inside dot-dirs)
    if ext == ".log" or _ROTATED_LOG_RE.search(name):
        return "log"

    # 4. Source code and prose: hard exclude
    if ext in SOURCE_AND_PROSE_SUFFIXES:
        return None

    # 5. CSV / TSV
    if ext in (".csv", ".tsv"):
        return "csv"

    # 6. JSONL / NDJSON
    if ext in (".jsonl", ".ndjson"):
        return "jsonl"

    # 7. Generated non-code artifacts
    if ext in GENERATED_SUFFIXES or name == "lcov.info":
        return "generated"

    # 8. JSON data dumps (strictly exclude editable config files and config dirs)
    if ext == ".json":
        if name in CONFIG_JSON_NAMES:
            return None
        if name.startswith(("tsconfig.", "appsettings.")) or name.endswith(".config.json"):
            return None
        parts_lower = {part.lower() for part in p.parts[:-1]}
        if parts_lower & CONFIG_DIR_PARTS:
            return None
        return "json_data"

    return None


def _clip_line(line: str, max_len: int = 220) -> str:
    if len(line) <= max_len:
        return line
    return line[: max_len - 18] + f"... ({len(line):,} chars)"


def _is_error_line(line: str, *, strict_exception_only: bool = False) -> bool:
    try:
        from visibility_rule import is_error_or_failure_line
        if is_error_or_failure_line(line, strict_exception_only=strict_exception_only):
            return True
    except Exception:
        pass
    if strict_exception_only:
        return False
    return bool(_LOG_IMPORTANT_RE.search(line))


def _format_head_tail(
    lines: list[str],
    head_n: int = 10,
    tail_n: int = 8,
    line_cap: int = 220,
    *,
    place: str = "",
    strict_errors: bool = False,
    skip_middle_errors: bool = False,
) -> list[str]:
    total = len(lines)
    out: list[str] = []
    if total <= head_n + tail_n:
        for idx, ln in enumerate(lines, 1):
            out.append(f"L{idx}: {_clip_line(ln, line_cap)}")
        return out
    out.append(f"Head (lines 1-{head_n}):")
    for idx in range(head_n):
        ln = lines[idx]
        out.append(f"  L{idx + 1}: {ln if _is_error_line(ln) else _clip_line(ln, line_cap)}")
    mid_start = head_n + 1
    mid_end = total - tail_n
    preserved_middle: list[tuple[int, str]] = []
    if not skip_middle_errors:
        for idx in range(head_n, total - tail_n):
            if _is_error_line(lines[idx], strict_exception_only=strict_errors):
                preserved_middle.append((idx + 1, lines[idx]))
    omitted = (total - head_n - tail_n) - len(preserved_middle)
    place_note = f" in {place}" if place else ""
    if preserved_middle:
        out.append(f"  Preserved error/failure lines ({len(preserved_middle)} verbatim):")
        for lno, text in preserved_middle:
            out.append(f"  L{lno}: {text}")
    if omitted > 0:
        out.append(f"  ... ({omitted:,} lines omitted{place_note}, lines {mid_start}-{mid_end}) ...")
    out.append(f"Tail (lines {total - tail_n + 1}-{total}):")
    for idx in range(total - tail_n, total):
        ln = lines[idx]
        out.append(f"  L{idx + 1}: {ln if _is_error_line(ln) else _clip_line(ln, line_cap)}")
    return out


def _preview_lockfile(content: str, file_path: str, lines: list[str]) -> tuple[str, int]:
    name = Path(file_path).name.lower()
    summary_lines: list[str] = []
    pkg_names: list[str] = []
    pkg_count: Optional[int] = None

    if name.endswith(".json"):
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                lf_ver = data.get("lockfileVersion")
                root_name = data.get("name", "")
                packages = data.get("packages")
                deps = data.get("dependencies")
                if isinstance(packages, dict):
                    pkg_count = max(0, len(packages) - (1 if "" in packages else 0))
                    for k in packages:
                        if k:
                            pkg_names.append(k.split("node_modules/")[-1])
                elif isinstance(deps, dict):
                    pkg_count = len(deps)
                    pkg_names = list(deps.keys())
                elif isinstance(data.get("nodes"), dict):  # flake.lock
                    pkg_count = len(data["nodes"])
                    pkg_names = list(data["nodes"].keys())
                meta_parts = [f"format={name}"]
                if root_name:
                    meta_parts.append(f"project={root_name}")
                if lf_ver is not None:
                    meta_parts.append(f"lockfileVersion={lf_ver}")
                if pkg_count is not None:
                    meta_parts.append(f"packages={pkg_count:,}")
                summary_lines.append("Shape: " + ", ".join(meta_parts))
        except Exception:
            pass
    elif name in ("cargo.lock", "uv.lock", "poetry.lock"):
        pkg_re = re.compile(r'^\s*name\s*=\s*"([^"]+)"')
        in_pkg = False
        for ln in lines:
            s = ln.strip()
            if s == "[[package]]":
                in_pkg = True
            elif in_pkg:
                m = pkg_re.match(ln)
                if m:
                    pkg_names.append(m.group(1))
                    in_pkg = False
        pkg_count = len(pkg_names)
        summary_lines.append(f"Shape: format={name}, packages={pkg_count:,}")
    elif name == "go.sum":
        mods = {ln.split()[0] for ln in lines if ln.strip()}
        pkg_count = len(mods)
        pkg_names = sorted(mods)
        summary_lines.append(f"Shape: format=go.sum, entries={len(lines):,}, unique_modules={pkg_count:,}")
    elif name == "yarn.lock":
        entries = [ln.rstrip(":").strip('"') for ln in lines if ln and not ln[0].isspace() and not ln.startswith("#") and ln.endswith(":")]
        pkg_count = len(entries)
        pkg_names = entries
        summary_lines.append(f"Shape: format=yarn.lock, entries={pkg_count:,}")
    elif name == "pnpm-lock.yaml":
        in_pkgs = False
        for ln in lines:
            if ln.startswith("packages:"):
                in_pkgs = True
                continue
            if in_pkgs:
                if ln and not ln[0].isspace():
                    break
                if ln.startswith("  ") and not ln.startswith("   ") and ln.rstrip().endswith(":"):
                    pkg_names.append(ln.strip().rstrip(":").strip("'\""))
        pkg_count = len(pkg_names)
        summary_lines.append(f"Shape: format=pnpm-lock.yaml, packages={pkg_count:,}")
    else:
        summary_lines.append(f"Shape: format={name}, lines={len(lines):,}")

    if pkg_names:
        sample = ", ".join(pkg_names[:15])
        more = f" (+{len(pkg_names) - 15:,} more)" if len(pkg_names) > 15 else ""
        summary_lines.append(f"Sample packages: {sample}{more}")

    summary_lines.extend(_format_head_tail(lines, head_n=10, tail_n=6, place=Path(file_path).name, strict_errors=True))
    return "\n".join(summary_lines), 11


def _preview_log(content: str, file_path: str, lines: list[str]) -> tuple[str, int]:
    level_counts: Counter[str] = Counter()
    important_groups: dict[str, list[int]] = {}
    important_order: list[str] = []
    first_ts: Optional[str] = None
    last_ts: Optional[str] = None

    for idx, ln in enumerate(lines, 1):
        m_lvl = _LOG_LEVEL_RE.search(ln)
        if m_lvl:
            lvl = m_lvl.group(1).upper()
            if lvl == "WARNING":
                lvl = "WARN"
            elif lvl == "ERR":
                lvl = "ERROR"
            level_counts[lvl] += 1
        if first_ts is None:
            m_ts = _TIMESTAMP_RE.search(ln)
            if m_ts:
                first_ts = m_ts.group(0)
        if _is_error_line(ln):
            # Preserve exact text verbatim; deduplicate only exact identical lines
            if ln not in important_groups:
                important_groups[ln] = [idx]
                important_order.append(ln)
            else:
                important_groups[ln].append(idx)

    for ln in reversed(lines):
        m_ts = _TIMESTAMP_RE.search(ln)
        if m_ts:
            last_ts = m_ts.group(0)
            break

    out: list[str] = []
    shape_parts = [f"lines={len(lines):,}"]
    if first_ts and last_ts and first_ts != last_ts:
        shape_parts.append(f"time_range={first_ts} .. {last_ts}")
    elif first_ts:
        shape_parts.append(f"time={first_ts}")
    if level_counts:
        lvl_str = ", ".join(f"{k}={v:,}" for k, v in level_counts.most_common())
        shape_parts.append(f"levels=({lvl_str})")
    out.append("Shape: " + "; ".join(shape_parts))

    total_imp = sum(len(v) for v in important_groups.values())
    if important_order:
        out.append(f"Errors/warnings/stack frames ({total_imp:,} total, {len(important_order):,} unique):")
        for text in important_order:
            lns = important_groups[text]
            if len(lns) == 1:
                out.append(f"  L{lns[0]}: {text}")
            elif len(lns) <= 4:
                refs = ",".join(f"L{n}" for n in lns)
                out.append(f"  {refs} (x{len(lns)}): {text}")
            else:
                out.append(f"  L{lns[0]}..L{lns[-1]} (x{len(lns)}): {text}")
    else:
        out.append("Errors/warnings: 0 detected.")

    out.extend(_format_head_tail(lines, head_n=8, tail_n=12, place=Path(file_path).name, skip_middle_errors=True))
    return "\n".join(out), 9


def _preview_csv(content: str, file_path: str, lines: list[str]) -> tuple[str, int]:
    ext = Path(file_path).suffix.lower()
    delim = "\t" if ext == ".tsv" else ","
    delim_label = "TSV (tab)" if delim == "\t" else "CSV (comma)"

    header: list[str] = []
    sample_vals: list[str] = []
    row_count = 0
    try:
        reader = csv.reader(io.StringIO(content), delimiter=delim)
        header = next(reader, [])
        sample_vals = [""] * len(header)
        for row in reader:
            row_count += 1
            if row_count <= 20:
                for i, val in enumerate(row[: len(sample_vals)]):
                    if not sample_vals[i] and val.strip():
                        sample_vals[i] = val.strip()[:40]
    except Exception:
        row_count = max(0, len(lines) - 1)

    out: list[str] = [
        f"Shape: {delim_label}, rows={row_count:,}, columns={len(header)}"
    ]
    if header:
        cols_desc = []
        for i, col in enumerate(header[:15]):
            ex = sample_vals[i] if i < len(sample_vals) and sample_vals[i] else ""
            cols_desc.append(f"{col} (e.g. {ex!r})" if ex else col)
        more = f" (+{len(header) - 15} more)" if len(header) > 15 else ""
        out.append("Columns: " + ", ".join(cols_desc) + more)

    out.extend(_format_head_tail(lines, head_n=8, tail_n=5, place=Path(file_path).name))
    return "\n".join(out), 9


def _json_type_label(val: Any) -> str:
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "bool"
    if isinstance(val, int):
        return "int"
    if isinstance(val, float):
        return "float"
    if isinstance(val, str):
        return "str"
    if isinstance(val, list):
        return f"list[{len(val):,}]"
    if isinstance(val, dict):
        return f"dict[{len(val):,}]"
    return type(val).__name__


def _preview_jsonl(content: str, file_path: str, lines: list[str]) -> tuple[str, int]:
    total = len(lines)
    valid_objs = 0
    non_json_lines: list[tuple[int, str]] = []
    error_records: list[tuple[int, str]] = []
    key_counts: Counter[str] = Counter()
    key_types: dict[str, set[str]] = {}

    for idx, ln in enumerate(lines, 1):
        s = ln.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except Exception:
            non_json_lines.append((idx, _clip_line(s, 180)))
            continue
        if isinstance(obj, dict):
            valid_objs += 1
            if valid_objs <= 500 or idx > total - 20:
                for k, v in obj.items():
                    key_counts[k] += 1
                    key_types.setdefault(k, set()).add(_json_type_label(v))
            # Preserve any record carrying an error/warning
            has_err = False
            for k, v in obj.items():
                if _ERROR_KEY_RE.match(k) and v:
                    has_err = True
                    break
                if k.lower() in ("level", "severity") and isinstance(v, str) and v.upper() in ("ERROR", "FATAL", "CRITICAL", "WARN", "WARNING"):
                    has_err = True
                    break
            if has_err or _is_error_line(s):
                error_records.append((idx, s))

    out: list[str] = [
        f"Shape: JSONL, lines={total:,}, json_objects={valid_objs:,}, non_json_lines={len(non_json_lines)}"
    ]
    if key_counts:
        schema_parts = []
        for k, _cnt in key_counts.most_common(15):
            types = "/".join(sorted(key_types.get(k, set()))[:3])
            schema_parts.append(f"{k} ({types})")
        more = f" (+{len(key_counts) - 15} more)" if len(key_counts) > 15 else ""
        out.append("Schema keys: " + ", ".join(schema_parts) + more)

    if error_records:
        out.append(f"Error/warning records ({len(error_records):,}):")
        for idx, txt in error_records:
            out.append(f"  L{idx}: {txt}")
    if non_json_lines:
        out.append(f"Non-JSON lines ({len(non_json_lines):,}):")
        for idx, txt in non_json_lines[:20]:
            out.append(f"  L{idx}: {txt}")

    out.extend(_format_head_tail(lines, head_n=5, tail_n=3, line_cap=240, place=Path(file_path).name, skip_middle_errors=True))
    return "\n".join(out), 6


def _preview_json_data(content: str, file_path: str, lines: list[str]) -> tuple[str, int]:
    out: list[str] = []
    try:
        data = json.loads(content)
    except Exception:
        out.append(f"Shape: JSON (unparsed), lines={len(lines):,}, bytes={len(content.encode('utf-8', errors='replace')):,}")
        out.extend(_format_head_tail(lines, head_n=10, tail_n=6))
        return "\n".join(out), 11

    if isinstance(data, list):
        out.append(f"Shape: JSON array, items={len(data):,}, lines={len(lines):,}")
        item_types = Counter(_json_type_label(x).split("[")[0] for x in data[:200])
        out.append("Item types: " + ", ".join(f"{k}={v}" for k, v in item_types.most_common(5)))
        dict_items = [x for x in data[:200] if isinstance(x, dict)]
        if dict_items:
            keys: Counter[str] = Counter()
            ktypes: dict[str, set[str]] = {}
            for item in dict_items:
                for k, v in item.items():
                    keys[k] += 1
                    ktypes.setdefault(k, set()).add(_json_type_label(v).split("[")[0])
            schema = ", ".join(f"{k} ({'/'.join(sorted(ktypes[k])[:2])})" for k, _ in keys.most_common(15))
            more = f" (+{len(keys) - 15} more)" if len(keys) > 15 else ""
            out.append(f"Item schema: {schema}{more}")
        if data:
            out.append("First items:")
            for i, item in enumerate(data[:3]):
                out.append(f"  [{i}]: {_clip_line(json.dumps(item, ensure_ascii=False), 240)}")
            if len(data) > 5:
                out.append(f"  ... ({len(data) - 5:,} items omitted in middle) ...")
                out.append("Last items:")
                for i in range(len(data) - 2, len(data)):
                    out.append(f"  [{i}]: {_clip_line(json.dumps(data[i], ensure_ascii=False), 240)}")
    elif isinstance(data, dict):
        out.append(f"Shape: JSON object, top_level_keys={len(data):,}, lines={len(lines):,}")
        key_summary = [f"{k}: {_json_type_label(v)}" for k, v in list(data.items())[:20]]
        more = f" (+{len(data) - 20:,} more)" if len(data) > 20 else ""
        out.append("Top-level keys: " + ", ".join(key_summary) + more)
        # Preserve any top-level error/warning fields verbatim (bounded)
        for k, v in data.items():
            if _ERROR_KEY_RE.match(k) and v:
                rendered = json.dumps(v, ensure_ascii=False)
                out.append(f"Field {k!r}: {_clip_line(rendered, 600)}")
        if len(lines) > 3:
            out.extend(_format_head_tail(lines, head_n=10, tail_n=6))
        else:
            out.append(f"Head chars: {content[:300]}")
            out.append(f"Tail chars: {content[-200:]}")
    else:
        out.append(f"Shape: JSON {_json_type_label(data)}, lines={len(lines):,}")
        out.extend(_format_head_tail(lines, head_n=10, tail_n=6))

    return "\n".join(out), 11


def _preview_minified(content: str, file_path: str, lines: list[str]) -> tuple[str, int]:
    name = Path(file_path).name.lower()
    max_line_len = max((len(ln) for ln in lines), default=0)
    out: list[str] = []

    if name.endswith(".map"):
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                sources = data.get("sources") or []
                names = data.get("names") or []
                out.append(
                    f"Shape: SourceMap v{data.get('version', '?')}, file={data.get('file', '?')}, "
                    f"sources={len(sources):,}, names={len(names):,}"
                )
                if sources:
                    sample = ", ".join(str(s) for s in sources[:15])
                    more = f" (+{len(sources) - 15:,} more)" if len(sources) > 15 else ""
                    out.append(f"Sources in {Path(file_path).name}: {sample}{more}")
                return "\n".join(out), 1
        except Exception:
            pass

    kind_label = "minified CSS" if name.endswith(".css") else "minified JS/bundle"
    extra = f", css_blocks~={content.count('{'):,}" if name.endswith(".css") else ""
    out.append(
        f"Shape: {kind_label}, lines={len(lines):,}, max_line_chars={max_line_len:,}{extra}"
    )
    out.append(f"Head (first 300 chars): {content[:300]!r}")
    if len(content) > 500:
        out.append(f"Tail (last 200 chars): {content[-200:]!r}")
    return "\n".join(out), 1


def _preview_generated(content: str, file_path: str, lines: list[str]) -> tuple[str, int]:
    name = Path(file_path).name.lower()
    ext = Path(file_path).suffix.lower()
    out: list[str] = []

    if ext == ".snap":
        exports = [
            m.group(1)
            for ln in lines
            for m in (re.match(r'^exports\[`([^`]+)`\]', ln),)
            if m
        ]
        out.append(f"Shape: snapshot file, lines={len(lines):,}, snapshots={len(exports):,}")
        if exports:
            sample = ", ".join(exports[:12])
            more = f" (+{len(exports) - 12:,} more)" if len(exports) > 12 else ""
            out.append(f"Snapshots: {sample}{more}")
    elif ext == ".har":
        try:
            data = json.loads(content)
            entries = (data.get("log") or {}).get("entries") or []
            statuses: Counter[int] = Counter()
            errors: list[str] = []
            for e in entries:
                st = int((e.get("response") or {}).get("status") or 0)
                statuses[st] += 1
                if st >= 400:
                    url = (e.get("request") or {}).get("url", "")
                    errors.append(f"  HTTP {st}: {_clip_line(str(url), 160)}")
            st_str = ", ".join(f"{k}={v}" for k, v in sorted(statuses.items()))
            out.append(f"Shape: HAR archive, entries={len(entries):,}, statuses=({st_str})")
            if errors:
                out.append(f"HTTP 4xx/5xx entries ({len(errors)}):")
                out.extend(errors[:30])
        except Exception:
            out.append(f"Shape: HAR file, lines={len(lines):,}")
    elif ext == ".sarif":
        try:
            data = json.loads(content)
            runs = data.get("runs") or []
            results = [r for run in runs for r in (run.get("results") or [])]
            levels = Counter(str(r.get("level", "warning")) for r in results)
            out.append(f"Shape: SARIF report, runs={len(runs)}, results={len(results):,}, levels={dict(levels)}")
            for r in results:
                lvl = str(r.get("level", "warning")).lower()
                if lvl in ("error", "warning"):
                    rule = r.get("ruleId", "?")
                    msg = ((r.get("message") or {}).get("text") or "").strip()
                    loc = ""
                    locs = r.get("locations") or []
                    if locs:
                        pl = (locs[0].get("physicalLocation") or {})
                        uri = (pl.get("artifactLocation") or {}).get("uri", "")
                        line = (pl.get("region") or {}).get("startLine", "")
                        if uri:
                            loc = f" ({uri}:{line})" if line else f" ({uri})"
                    out.append(f"  [{lvl.upper()}] {rule}{loc}: {_clip_line(msg, 180)}")
        except Exception:
            out.append(f"Shape: SARIF file, lines={len(lines):,}")
    else:
        out.append(f"Shape: generated file ({name}), lines={len(lines):,}")

    out.extend(_format_head_tail(lines, head_n=8, tail_n=6, place=Path(file_path).name))
    return "\n".join(out), 9


_BUILDERS = {
    "lockfile": _preview_lockfile,
    "log": _preview_log,
    "csv": _preview_csv,
    "jsonl": _preview_jsonl,
    "json_data": _preview_json_data,
    "minified": _preview_minified,
    "generated": _preview_generated,
}


def build_big_noncode_preview(
    file_path: str,
    content: str,
    *,
    kind: Optional[str] = None,
    archive_key: Optional[str] = None,
    max_chars: int = MAX_PREVIEW_CHARS,
) -> Optional[dict[str, Any]]:
    """Build a structured preview dict, or return None if compression is not worthwhile.

    Returns dict with keys: kind, reason, context, orig_tokens, preview_tokens, ratio.
    """
    raw_bytes = len(content.encode("utf-8", errors="replace"))
    if raw_bytes < MIN_BYTES or raw_bytes > MAX_BYTES:
        return None

    file_kind = kind or classify_big_noncode_file(file_path)
    if file_kind is None:
        return None
    if file_kind == "json_data" and raw_bytes < JSON_DATA_MIN_BYTES:
        return None

    lines = content.splitlines()
    if file_kind != "minified" and file_kind != "json_data" and len(lines) < MIN_LINES_NON_MINIFIED:
        return None

    builder = _BUILDERS.get(file_kind)
    if builder is None:
        return None

    body, next_offset = builder(content, file_path, lines)
    orig_tokens = estimate_tokens(content)
    if orig_tokens <= 0:
        return None

    size_kb = raw_bytes / 1024.0
    name = Path(file_path).name
    header = (
        f"[Token Optimizer] Structured preview of {name} "
        f"({size_kb:.1f} KB, {len(lines):,} lines, ~{orig_tokens:,} tokens, kind={file_kind})."
    )
    ranged_hint = (
        f'For more lines: Read(file_path="{file_path}", offset={next_offset}, limit=200)\n'
        f'To load the entire file as-is, call Read(file_path="{file_path}") again.'
    )
    preview_text = f"{header}\n{body}\n\n{ranged_hint}"

    # Visibility rule (a): never hide any critical error/exception/traceback line.
    try:
        from visibility_rule import find_missing_critical_lines
        missing_crit = find_missing_critical_lines(content, preview_text)
        if missing_crit:
            crit_block = "\n".join(f"  L{lno}: {txt}" for lno, txt in missing_crit)
            preview_text = f"{header}\n{body}\nPreserved critical lines ({len(missing_crit)}):\n{crit_block}\n\n{ranged_hint}"
    except Exception:
        pass

    if archive_key:
        try:
            from archive_result import build_archive_pointer
            preview_text = build_archive_pointer(preview_text, len(content), archive_key)
        except Exception:
            return None

    if len(preview_text) > max_chars:
        # Never silently truncate away log/SARIF error lines; fail open instead.
        return None
    from emit_safety_gate import gate_or_none as _emit_gate
    if _emit_gate(content, preview_text, feature=FEATURE_NAME, tool_name="Read", require_expand=bool(archive_key)) is None:
        return None

    reason = (
        f"{name} is a large non-code {file_kind} ({size_kb:.1f} KB, ~{orig_tokens:,} tokens); "
        f"served structured preview. Call Read with offset/limit, use expand, or re-Read for full file."
    )
    preview_tokens = estimate_tokens(reason) + estimate_tokens(preview_text)
    ratio = 1.0 - (preview_tokens / orig_tokens)
    if ratio < MIN_SAVINGS_RATIO:
        return None

    return {
        "kind": file_kind,
        "reason": reason,
        "context": preview_text,
        "orig_tokens": orig_tokens,
        "preview_tokens": preview_tokens,
        "ratio": round(ratio, 4),
    }


def compress_for_benchmark(command: str, raw_output: str) -> str:
    """Benchmark adapter compatible with benchmark.run_single_fixture(fixture, compressor).

    Accepts command like 'Read:package-lock.json' or 'Read:/tmp/app.log'.
    Returns the structured preview text if eligible, else raw_output untouched.
    """
    file_path = command.split("Read:", 1)[1].strip() if "Read:" in command else command.strip()
    result = build_big_noncode_preview(file_path, raw_output, archive_key=None)
    if result is None:
        return raw_output
    return result["context"]


def try_big_noncode_read(
    file_path: str,
    stat: os.stat_result,
    offset: int,
    limit: int,
    session_id: str,
    store: Any,
    mode: str,
    entry: dict[str, Any],
    *,
    cached_content: Optional[str] = None,
    reset_epoch: Any = None,
    save_hook_context_enabled: bool = False,
    quiet: bool = True,
    emit_fn: Optional[Callable[..., None]] = None,
    log_decision_fn: Optional[Callable[..., None]] = None,
    log_savings_fn: Optional[Callable[..., Any]] = None,
) -> bool:
    """PreToolUse[Read] entry point called from read_cache.py on first read (entry is None).

    Returns True iff a structured preview was archived and emitted (caller must stop).
    Returns False (fail-open) on any ineligible file, safety exclusion, or error.
    """
    try:
        if not is_big_noncode_read_enabled():
            return False
        if mode not in ("soft_block", "block"):
            return False
        # Never override ranged Reads
        if offset != 0 or limit != 0:
            return False

        size = int(stat.st_size)
        if size < MIN_BYTES or size > MAX_BYTES:
            return False

        kind = classify_big_noncode_file(file_path)
        if kind is None:
            return False
        if kind == "json_data" and size < JSON_DATA_MIN_BYTES:
            return False

        # Never intercept files edited in this session
        if store is not None and store.was_file_edited(file_path):
            return False

        content = (
            cached_content
            if cached_content is not None
            else Path(file_path).read_text(encoding="utf-8", errors="replace")
        )

        # Archive full original BEFORE emitting preview; fail open if archive fails
        from archive_result import (
            archive_entry_exists,
            archive_original,
            derive_archive_key,
        )

        key = derive_archive_key(session_id, file_path, stat.st_mtime_ns)
        if (
            archive_original(
                content,
                session_id,
                key,
                "Read",
                quiet=quiet,
                file_path=file_path,
                language=kind,
            )
            is None
        ):
            return False
        if not archive_entry_exists(session_id, key):
            return False

        cap = STRICT_PREVIEW_CHARS if save_hook_context_enabled else MAX_PREVIEW_CHARS
        result = build_big_noncode_preview(
            file_path, content, kind=kind, archive_key=key, max_chars=cap
        )
        if result is None:
            return False

        # Escape hatch: store a non-empty sentinel range [[-1, -1]] (empty []
        # falls back to [[0, 0]] in read_cache.py) so a second full Read sees
        # range_covered == False and returns the entire file whole.
        entry["ranges_seen"] = [[-1, -1]]
        entry["last_replacement_type"] = REPLACEMENT_TYPE
        entry["tokens_est"] = result["orig_tokens"]
        if store is not None:
            store.upsert_file_entry(file_path, entry, expected_reset_epoch=reset_epoch)

        net_saved = max(0, result["orig_tokens"] - result["preview_tokens"])
        if log_decision_fn is not None:
            log_decision_fn(
                "block",
                file_path,
                "big_noncode_preview",
                session_id,
                mode=mode,
                actual_substitution=True,
                eligible=True,
                language=kind,
                reason_code=REPLACEMENT_TYPE,
                offset=offset,
                limit=limit,
                replacement_type=REPLACEMENT_TYPE,
                file_tokens_est=result["orig_tokens"],
                replacement_tokens_est=result["preview_tokens"],
                net_saved_tokens_est=net_saved,
                replacement_fingerprint=key,
                repeat_replacement_count=1,
                save_hook_additional_context_enabled=save_hook_context_enabled,
            )

        # Log Measured savings to savings_events (primary ledger, owns the key)
        # and compression_events (ratio telemetry, excluded from double-count)
        # per docs/METHODOLOGY.md Section 3.1. Re-expanding the archive debits
        # via tool_archive_reexpand in savings_events (R8).
        if log_savings_fn is not None and net_saved > 0:
            try:
                log_savings_fn(
                    FEATURE_NAME,
                    net_saved,
                    session_id,
                    f"{Path(file_path).name}:{kind}:archive={key}",
                )
            except Exception:
                pass
        try:
            from compression_log import log_compression_event
            log_compression_event(
                feature=FEATURE_NAME,
                session_id=session_id,
                command_pattern=f"Read:{kind}:{Path(file_path).name}",
                tier="measured",
                verified=True,
                quality_preserved=True,
                detail=f"kind={kind} size_bytes={size} archive={key}",
                original_tokens=result["orig_tokens"],
                compressed_tokens=result["preview_tokens"],
            )
        except Exception:
            pass

        if not quiet:
            print(
                f"[Read Cache] Big non-code preview ({kind}): {file_path} "
                f"(~{result['orig_tokens']:,} -> ~{result['preview_tokens']:,} tokens, "
                f"saved ~{net_saved:,})",
                file=sys.stderr,
            )

        if emit_fn is not None:
            emit_fn("deny", result["reason"], result["context"])
        return True
    except Exception:
        return False
