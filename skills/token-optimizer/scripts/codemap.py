#!/usr/bin/env python3
"""Code map lookup: answer structural code questions without reading whole files.

    codemap.py find <symbol>        where a function/class/method/variable is defined
    codemap.py show <symbol>        the source of that one definition (line-numbered)
    codemap.py callers <symbol>     call sites, grouped by file and enclosing function
    codemap.py outline <file>       definitions in a file with line ranges
    codemap.py deps <file>          what a file imports and which files import it
    codemap.py refresh | stats      (maintenance)

Options: --root DIR (default: the project containing the cwd), --in TEXT (only
paths containing TEXT), --max-lines N (show).

Python and JS/TS only. Every answer is hard-capped (~800 tokens) and the index
is synced (mtime+size) before each query, so answers match the files on disk.
On any index problem it prints one line telling the agent to use Grep/Read and
exits 0: lookups fail open, never in the agent's way.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

MAX_OUT_CHARS = 3200
MAX_SHOW_LINES = 120
FALLBACK = "codemap: {why}. Use Grep/Read instead."


def _estimate_tokens(text: str) -> int:
    try:
        from token_estimate import estimate_tokens
        return int(estimate_tokens(text))
    except Exception:
        return max(1, len(text) // 4)


class _Out:
    """Line buffer with a hard character cap."""

    def __init__(self, cap: int = MAX_OUT_CHARS):
        self.cap = cap
        self.lines: List[str] = []
        self.size = 0
        self.dropped = 0

    def add(self, line: str) -> bool:
        if self.size + len(line) + 1 > self.cap:
            self.dropped += 1
            return False
        self.lines.append(line)
        self.size += len(line) + 1
        return True

    def text(self, more_hint: str = "") -> str:
        out = list(self.lines)
        if self.dropped:
            out.append(f"... +{self.dropped} more lines cut at the answer cap{more_hint}")
        return "\n".join(out)


def _filter(rows: Sequence[Tuple], within: str, path_idx: int) -> List[Tuple]:
    if not within:
        return list(rows)
    return [r for r in rows if within in r[path_idx]]


def _span(line: int, end: int) -> str:
    return f"{line}" if not end or end <= line else f"{line}-{end}"


def cmd_find(idx, symbol: str, within: str, out: _Out) -> List[str]:
    rows = _filter(idx.find_defs(symbol), within, 3)
    if not rows:
        out.add(f"no definition of '{symbol}' in the index ({idx.counts()['files']} files)."
                " It may be external, dynamic, or in an unsupported language.")
        return []
    exact = any(r[0] == symbol.rsplit(".", 1)[-1] for r in rows)
    out.add(f"{len(rows)} definition(s) of '{symbol}'" + ("" if exact else " (partial-name matches)") + ":")
    for name, qual, kind, path, line, end, sig in rows:
        out.add(f"{path}:{_span(line, end)}  {kind} {qual}  {sig}")
    paths = list(dict.fromkeys(r[3] for r in rows))
    return paths[:1] if exact and len(rows) <= 6 else []


def cmd_show(idx, symbol: str, within: str, max_lines: int, out: _Out) -> List[str]:
    rows = _filter(idx.find_defs(symbol), within, 3)
    rows = [r for r in rows if r[0] == symbol.rsplit(".", 1)[-1] or r[1] == symbol] or rows
    if not rows:
        out.add(f"no definition of '{symbol}' in the index.")
        return []
    rows.sort(key=lambda r: (r[2] == "variable", r[1] != symbol))
    name, qual, kind, path, line, end, sig = rows[0]
    abs_path = idx.root / path
    try:
        src = abs_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        out.add(f"{path} is no longer readable.")
        return []
    end = max(line, end or line)
    last = min(end, line + max_lines - 1, len(src))
    out.add(f"{path}:{_span(line, end)}  {kind} {qual}  (lines {line}-{last} of {len(src)}; Edit-ready numbering)")
    for n in range(line, last + 1):
        if not out.add(f"{n:>6}\t{src[n - 1]}"):
            break
    if last < end:
        out.add(f"... body continues to line {end}: Read {path} with offset={last + 1} for the rest")
    if len(rows) > 1:
        others = ", ".join(f"{r[3]}:{r[4]}" for r in rows[1:4])
        out.add(f"also defined at: {others}" + (" ..." if len(rows) > 4 else ""))
    return [path]


def cmd_callers(idx, symbol: str, within: str, out: _Out) -> List[str]:
    rows = _filter(idx.callers(symbol), within, 0)
    if not rows:
        out.add(f"no call sites of '{symbol}' found (name-based; dynamic calls are invisible).")
        return []
    by_file: dict = {}
    for path, line, caller, n in rows:
        by_file.setdefault(path, []).append((line, caller, n))
    total = sum(r[3] for r in rows)
    out.add(f"{total} call(s) of '{symbol.rsplit('.', 1)[-1]}' in {len(by_file)} file(s)"
            " (matched by name; same-named symbols are included):")
    ranked = sorted(by_file.items(), key=lambda kv: -sum(x[2] for x in kv[1]))
    for path, sites in ranked:
        parts = [f"{caller}@{line}" + (f" x{n}" if n > 1 else "") for line, caller, n in sites[:8]]
        extra = f" +{len(sites) - 8}" if len(sites) > 8 else ""
        out.add(f"{path}: " + ", ".join(parts) + extra)
    return [p for p, _ in ranked[:2]]


def _resolve_file(idx, file_arg: str) -> Optional[str]:
    rel = idx.rel(file_arg)
    if rel and idx.file_row(rel):
        return rel
    tail = file_arg.replace("\\", "/").lstrip("./")
    row = idx.conn.execute(
        "SELECT path FROM files WHERE path=? OR path LIKE ? ORDER BY length(path) LIMIT 1",
        (tail, "%/" + tail)).fetchone()
    return row[0] if row else None


def cmd_outline(idx, file_arg: str, out: _Out) -> List[str]:
    rel = _resolve_file(idx, file_arg)
    if rel is None:
        out.add(f"{file_arg} is not in the index (Python/JS/TS only).")
        return []
    row = idx.file_row(rel)
    defs = idx.outline(rel)
    out.add(f"{rel}: {row[5]} lines, ~{row[4]:,} tokens to Read in full, {len(defs)} definitions"
            + ("" if row[6] else " (parse failed; outline incomplete)"))
    for name, qual, kind, line, end, sig in defs:
        if kind == "variable":
            continue
        indent = "  " * qual.count(".")
        out.add(f"{indent}{_span(line, end)}  {sig}")
    return [rel]


def cmd_deps(idx, file_arg: str, out: _Out) -> List[str]:
    rel = _resolve_file(idx, file_arg)
    if rel is None:
        out.add(f"{file_arg} is not in the index (Python/JS/TS only).")
        return []
    imports = idx.imports_of(rel)
    internal = [t for _, t in imports if t]
    external = [m for m, t in imports if not t]
    out.add(f"{rel} imports {len(internal)} project file(s): " + (", ".join(internal) or "none"))
    if external:
        out.add("external/unresolved: " + ", ".join(external[:30]) + (" ..." if len(external) > 30 else ""))
    importers = idx.importers_of(rel)
    out.add(f"imported by {len(importers)} file(s): " + (", ".join(importers) or "none"))
    return [rel]


def _root_hash(root: Path) -> str:
    return hashlib.sha256(str(root).encode("utf-8", "replace")).hexdigest()[:16]


def run(argv: Sequence[str]) -> Tuple[int, str]:
    parser = argparse.ArgumentParser(prog="codemap.py", add_help=True)
    parser.add_argument("command", choices=["find", "show", "callers", "outline", "deps", "refresh", "stats"])
    parser.add_argument("arg", nargs="?", default="")
    parser.add_argument("--root", default="")
    parser.add_argument("--in", dest="within", default="")
    parser.add_argument("--max-lines", type=int, default=MAX_SHOW_LINES)
    parser.add_argument("--budget", type=float, default=None)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(list(argv))
    if args.background:
        try:
            os.nice(10)
        except (AttributeError, OSError):
            pass

    import codemap_index as ci
    if not ci.codemap_enabled():
        return 0, "codemap is turned off (TOKEN_OPTIMIZER_CODEMAP=0). Use Grep/Read."
    if args.command in ("find", "show", "callers", "outline", "deps") and not args.arg.strip():
        return 2, f"usage: codemap.py {args.command} <{'file' if args.command in ('outline', 'deps') else 'symbol'}>"

    start = args.root or os.getcwd()
    idx = ci.open_synced(start, budget_s=args.budget)
    try:
        if args.command == "refresh":
            if args.background:
                try:
                    idx.db_path.with_suffix(".building").unlink()
                except OSError:
                    pass
            c = idx.counts()
            return 0, "" if args.quiet else f"codemap index: {c['files']} files, complete={idx.complete}"
        if args.command == "stats":
            c = idx.counts()
            return 0, (f"root {idx.root}\nfiles {c['files']} defs {c['defs']} imports {c['imports']} "
                       f"call-edges {c['refs']} complete={idx.complete} db={idx.db_path.stat().st_size // 1024}KB")
        out = _Out()
        max_lines = max(5, min(int(args.max_lines or MAX_SHOW_LINES), 400))
        symbol = args.arg.strip()
        if args.command == "find":
            covered = cmd_find(idx, symbol, args.within, out)
        elif args.command == "show":
            covered = cmd_show(idx, symbol, args.within, max_lines, out)
        elif args.command == "callers":
            covered = cmd_callers(idx, symbol, args.within, out)
        elif args.command == "outline":
            covered = cmd_outline(idx, symbol, out)
        else:
            covered = cmd_deps(idx, symbol, out)
        if not idx.complete:
            out.lines.append(f"(index still building: {idx.counts()['files']} files so far; rerun or Grep for anything missing)")
        text = out.text(" (narrow with --in <path> or Class.method)")
        _log(idx, args.command, text, covered)
        return 0, text
    finally:
        idx.close()


def _log(idx, command: str, text: str, covered: List[str]) -> None:
    try:
        import codemap_ledger as led
        cov = [(str(idx.root / rel), idx.file_tokens(rel)) for rel in covered]
        led.log_lookup(command, _estimate_tokens(text), cov, root_hash=_root_hash(idx.root))
        led.resolve_pending()
    except Exception:
        pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        code, text = run(argv)
    except SystemExit as exc:  # argparse usage errors
        return int(exc.code or 0)
    except Exception as exc:
        why = type(exc).__name__
        msg = str(exc).splitlines()[0][:120] if str(exc) else ""
        print(FALLBACK.format(why=f"index unavailable ({why}{': ' + msg if msg else ''})"))
        return 0
    if text:
        print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
