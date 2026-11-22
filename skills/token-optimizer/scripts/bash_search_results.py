"""Render parsed search matches with explicit per-file, file-count, and noise-folding limits."""

from __future__ import annotations

import os
import re
import shlex

NOISY_FOLDERS = frozenset({"node_modules", "dist", "build", ".git", "vendor"})

LOCKFILE_NAMES = frozenset({
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "cargo.lock",
    "poetry.lock",
    "gemfile.lock",
    "composer.lock",
    "go.sum",
    "podfile.lock",
    "pipfile.lock",
    "uv.lock",
    "bun.lock",
    "bun.lockb",
    "shrinkwrap.json",
    "npm-shrinkwrap.json",
    "flake.lock",
    "mix.lock",
    "pubspec.lock",
})

_MINIFIED_SUFFIXES = (".min.js", ".min.mjs", ".min.cjs", ".min.css")
_MINIFIED_RE = re.compile(r"(?:\.min\.(?:m|c)?js|\.min\.css)$", re.IGNORECASE)


def _command_tokens(command_str: str) -> list[str]:
    if not command_str:
        return []
    try:
        return shlex.split(command_str)
    except ValueError:
        return command_str.split()


def _explicitly_targeted(command_str: str) -> tuple[set[str], bool, bool]:
    """Return (targeted_folders, targets_lockfiles, targets_minified) from command_str."""
    targeted_folders: set[str] = set()
    targets_lockfiles = False
    targets_minified = False
    tokens = _command_tokens(command_str)
    # Skip the command binary itself (tokens[0]) when scanning patterns/paths.
    for tok in tokens[1:]:
        low = tok.lower().replace("\\", "/")
        # Split on path separators and common flag value delimiters (=, :)
        parts = [p for p in re.split(r"[/=:,]", low) if p]
        for folder in NOISY_FOLDERS:
            if folder in parts or f"/{folder}" in low or low.startswith(f"{folder}/"):
                targeted_folders.add(folder)
        if any(name in low for name in LOCKFILE_NAMES) or ".lock" in low or "lockfile" in low:
            targets_lockfiles = True
        if any(suf in low for suf in _MINIFIED_SUFFIXES) or "min.js" in low or "minified" in low:
            targets_minified = True
    return targeted_folders, targets_lockfiles, targets_minified


def classify_noisy_path(
    filepath: str,
    targeted_folders: set[str],
    targets_lockfiles: bool,
    targets_minified: bool,
) -> str | None:
    """Return the folded folder bucket label if filepath is noisy and untargeted, else None."""
    norm = filepath.replace("\\", "/")
    stripped = norm[2:] if norm.startswith("./") else norm
    parts = [p for p in stripped.split("/") if p]
    if not parts:
        return None

    # 1. Noisy folder segment (matches earliest noisy segment in path, preserving its folder path).
    for idx, seg in enumerate(parts[:-1]):
        if seg in NOISY_FOLDERS:
            if seg in targeted_folders:
                return None
            folder_path = "/".join(parts[: idx + 1]) + "/"
            return folder_path

    basename = parts[-1]
    base_low = basename.lower()
    parent_dir = ("/".join(parts[:-1]) + "/") if len(parts) > 1 else "./"

    # 2. Lockfiles.
    if base_low in LOCKFILE_NAMES:
        if targets_lockfiles:
            return None
        return f"{parent_dir} (lockfiles)"

    # 3. Minified JS/CSS files (*.min.js, *.min.mjs, *.min.cjs, *.min.css).
    if _MINIFIED_RE.search(base_low):
        if targets_minified:
            return None
        return f"{parent_dir} (*.min.js)"

    return None


def _split_noisy_files(files, command_str: str = ""):
    """Partition files into (kept_files, folded_buckets).

    folded_buckets maps folder_label -> [file_count, match_count, set_of_lockfile_names].
    """
    targeted_folders, targets_lockfiles, targets_minified = _explicitly_targeted(command_str)
    kept: dict[str, list[str]] = {}
    folded: dict[str, list] = {}
    for fname, file_lines in files.items():
        bucket = classify_noisy_path(fname, targeted_folders, targets_lockfiles, targets_minified)
        if bucket is None:
            kept[fname] = file_lines
        else:
            entry = folded.get(bucket)
            if entry is None:
                entry = [0, 0, []]
                folded[bucket] = entry
            entry[0] += 1
            entry[1] += len(file_lines)
            if bucket.endswith("(lockfiles)"):
                bname = os.path.basename(fname.replace("\\", "/"))
                if bname not in entry[2]:
                    entry[2].append(bname)
    return kept, folded


def _line_range_suffix(file_lines: list[str]) -> str:
    """Return 'lines A-B' / 'line N' for search hits, or '' if none parse."""
    try:
        from visibility_rule import extract_search_line_number, format_line_range
    except Exception:
        return ""
    nums = [n for ln in file_lines if (n := extract_search_line_number(ln)) is not None]
    return format_line_range(nums)


def _is_error_nonfile(line: str) -> bool:
    try:
        from visibility_rule import is_error_or_failure_line
        return is_error_or_failure_line(line)
    except Exception:
        return False


def render_search_results(files, no_file, command_str: str = ""):
    _MAX_PER_FILE = 3
    _MAX_FILES = 20

    kept_files, folded = _split_noisy_files(files, command_str=command_str)

    result: list[str] = []
    file_count = 0
    file_items = list(kept_files.items())
    for fname, file_lines in file_items:
        if file_count >= _MAX_FILES:
            break
        if len(file_lines) <= _MAX_PER_FILE:
            result.extend(file_lines)
        else:
            result.extend(file_lines[:_MAX_PER_FILE])
            hidden = file_lines[_MAX_PER_FILE:]
            loc = _line_range_suffix(hidden)
            loc_note = f"({loc})" if loc else "(content omitted)"
            result.append(f"  ... {len(hidden)} more matches in {fname} {loc_note} ...")
        file_count += 1

    # Keep EVERY matching non-noisy filename (cheap) so "does file X match?" stays
    # answerable; only per-line CONTENT past the top-N is dropped, never names.
    if file_count < len(file_items):
        result.append(f"... plus {len(file_items) - file_count} more files with matches "
                       f"(filenames + counts, content omitted):")
        for fname, file_lines in file_items[file_count:]:
            loc = _line_range_suffix(file_lines)
            loc_suffix = f" ({loc})" if loc else ""
            result.append(f"  {fname}: {len(file_lines)} matches{loc_suffix}")

    if folded:
        folded_total_matches = sum(v[1] for v in folded.values())
        folded_total_files = sum(v[0] for v in folded.values())
        if result:
            result.append("")
        result.append(
            f"[Folded noisy matches: {folded_total_matches} matches across "
            f"{folded_total_files} files in {len(folded)} folder(s) "
            f"(not explicitly targeted):]"
        )
        for bucket, (f_cnt, m_cnt, lock_names) in folded.items():
            if lock_names:
                label = f"{bucket[:-1]}: {', '.join(lock_names)})"
            else:
                label = bucket
            file_word = "file" if f_cnt == 1 else "files"
            match_word = "match" if m_cnt == 1 else "matches"
            result.append(f"  {label}: {m_cnt} {match_word} across {f_cnt} {file_word}")

    if no_file:
        result.append("")
        # "Binary file X matches" lines name a matching file; keep ALL of them so
        # the banner's "a file not listed has NO matches" stays true for binaries.
        binary = [ln for ln in no_file if ln.startswith("Binary file ") and " matches" in ln]
        other = [ln for ln in no_file if not (ln.startswith("Binary file ") and " matches" in ln)]
        # Rule (a): error/failure/exception/traceback/panic/non-zero-exit lines stay visible verbatim.
        error_other = [ln for ln in other if _is_error_nonfile(ln)]
        plain_other = [ln for ln in other if not _is_error_nonfile(ln)]
        result.extend(binary)
        result.extend(error_other)
        result.extend(plain_other[:10])
        if len(plain_other) > 10:
            result.append(
                f"... {len(plain_other) - 10} more non-file lines "
                f"(lines 11-{len(plain_other)} of non-file output) omitted ..."
            )

    total_matches = sum(len(v) for v in files.values())
    result.insert(0, f"[Search results: {total_matches} matches across {len(files)} files. "
                     f"ALL matching filenames listed; up to {_MAX_PER_FILE} matches per file "
                     f"shown for the first {_MAX_FILES} files. Other match content omitted.]")
    return "\n".join(result)
