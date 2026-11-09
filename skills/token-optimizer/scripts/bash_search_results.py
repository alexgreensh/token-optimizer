"""Render parsed search matches with explicit per-file and file-count limits."""


def render_search_results(files, no_file):
    _MAX_PER_FILE = 3
    _MAX_FILES = 20
    result: list[str] = []
    file_count = 0
    file_items = list(files.items())
    for fname, file_lines in file_items:
        if file_count >= _MAX_FILES:
            break
        if len(file_lines) <= _MAX_PER_FILE:
            result.extend(file_lines)
        else:
            result.extend(file_lines[:_MAX_PER_FILE])
            result.append(f"  ... {len(file_lines) - _MAX_PER_FILE} more matches in {fname} (content omitted) ...")
        file_count += 1

    # Keep EVERY matching filename (cheap) so "does file X match?" stays
    # answerable; only per-line CONTENT past the top-N is dropped, never names.
    if file_count < len(file_items):
        result.append(f"... plus {len(file_items) - file_count} more files with matches "
                       f"(filenames + counts, content omitted):")
        for fname, file_lines in file_items[file_count:]:
            result.append(f"  {fname}: {len(file_lines)} matches")

    if no_file:
        result.append("")
        # "Binary file X matches" lines name a matching file; keep ALL of them so
        # the banner's "a file not listed has NO matches" stays true for binaries.
        binary = [ln for ln in no_file if ln.startswith("Binary file ") and " matches" in ln]
        other = [ln for ln in no_file if not (ln.startswith("Binary file ") and " matches" in ln)]
        result.extend(binary)
        result.extend(other[:10])
        if len(other) > 10:
            result.append(f"... {len(other) - 10} more non-file lines omitted ...")

    total_matches = sum(len(v) for v in files.values())
    result.insert(0, f"[Search results: {total_matches} matches across {len(files)} files. "
                     f"ALL matching filenames listed; up to {_MAX_PER_FILE} matches per file "
                     f"shown for the first {_MAX_FILES} files. Other match content omitted.]")
    return "\n".join(result)
