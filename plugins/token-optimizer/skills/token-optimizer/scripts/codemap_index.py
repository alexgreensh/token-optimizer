#!/usr/bin/env python3
"""Serverless per-project code map index (stdlib only).

The index answers structural questions ("where is X defined", "what calls Y",
"what does this file import") from a small SQLite file in the plugin data dir,
so an agent can ask instead of grepping and reading whole files. There is no
resident server or file watcher: every query first runs a bounded mtime+size
sweep and re-parses only the files that changed, so answers are never staler
than the files on disk.

Parsing reuses structure_map (Python ``ast`` signatures, the JS/TS line
scanner and its comment/string stripper, the import resolvers). Everything
here is bounded: file count, per-file bytes, total build time and DB size.
Any failure raises ``CodemapError`` so callers can fail open to normal reads.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import structure_map as sm

SCHEMA_VERSION = "2"
MAX_FILES = 6000
MAX_PY_BYTES = sm.MAX_AST_BYTES
MAX_JS_BYTES = sm.MAX_JS_TS_BYTES
MAX_DB_BYTES = 96 * 1024 * 1024
BUILD_BUDGET_S = 8.0
SWEEP_BUDGET_S = 2.0
MAX_REFS_PER_FILE = 4000
MAX_DEFS_PER_FILE = 2000
SUPPORTED_SUFFIXES = frozenset({sm.PYTHON_SUFFIX}) | frozenset(sm.JS_TS_SUFFIXES)
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    "out", "target", ".next", ".nuxt", "coverage", "site-packages", "vendor",
    "bower_components", ".cache", ".idea", ".vscode", ".gradle", "Pods",
})
ROOT_MARKERS = (".git", "pyproject.toml", "package.json", "setup.py", "setup.cfg", "go.mod", "Cargo.toml")
_JS_CALL_RE = re.compile(r"(?<![\w$.])(?:new\s+)?([A-Za-z_$][\w$]*)\s*\(|\.([A-Za-z_$][\w$]*)\s*\(")
_JS_KEYWORDS = frozenset({
    "if", "for", "while", "switch", "catch", "function", "return", "typeof", "await",
    "super", "import", "require", "with", "do", "else", "new", "delete", "void", "in",
    "of", "yield", "case", "throw", "constructor", "async",
})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS files (
    fid INTEGER PRIMARY KEY, path TEXT UNIQUE, mtime_ns INTEGER, size INTEGER,
    lang TEXT, tokens INTEGER, lines INTEGER, parse_ok INTEGER
);
CREATE TABLE IF NOT EXISTS defs (
    name TEXT, qual TEXT, kind TEXT, fid INTEGER, line INTEGER, end_line INTEGER, sig TEXT
);
CREATE TABLE IF NOT EXISTS imports (fid INTEGER, module TEXT, target TEXT);
CREATE TABLE IF NOT EXISTS refs (fid INTEGER, line INTEGER, caller TEXT, callee TEXT, n INTEGER);
CREATE INDEX IF NOT EXISTS defs_name ON defs(name);
CREATE INDEX IF NOT EXISTS defs_fid ON defs(fid);
CREATE INDEX IF NOT EXISTS refs_callee ON refs(callee);
CREATE INDEX IF NOT EXISTS refs_fid ON refs(fid);
CREATE INDEX IF NOT EXISTS imports_fid ON imports(fid);
CREATE INDEX IF NOT EXISTS imports_target ON imports(target);
"""


def _tokens_from_bytes(n: int) -> int:
    try:
        from token_estimate import estimate_tokens_from_bytes
        return int(estimate_tokens_from_bytes(n))
    except Exception:
        return max(1, (int(n) + 3) // 4)


def _discard_index_file(db: Path) -> None:
    """Remove a corrupt index (our own cache file and its WAL sidecars only)."""
    if db.suffix != ".db" or db.parent.name != "codemap":
        return
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(db) + suffix).unlink()
        except OSError:
            pass


class CodemapError(Exception):
    """Any index failure. Callers fail open (serve normal reads)."""


# --------------------------------------------------------------------------- #
# Flags. ON by default; TOKEN_OPTIMIZER_CODEMAP=0 (or config v5_codemap=false)
# turns off lookups, hints and re-read outlines together.
# --------------------------------------------------------------------------- #

def codemap_enabled() -> bool:
    try:
        from plugin_env import is_v5_flag_enabled
        return is_v5_flag_enabled("v5_codemap", "TOKEN_OPTIMIZER_CODEMAP", default=True)
    except Exception:
        return False


def reread_outline_enabled() -> bool:
    if not codemap_enabled():
        return False
    try:
        from plugin_env import is_v5_flag_enabled
        return is_v5_flag_enabled("v5_codemap_reread", "TOKEN_OPTIMIZER_CODEMAP_REREAD", default=True)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Locations
# --------------------------------------------------------------------------- #

def find_project_root(start: str | os.PathLike[str]) -> Path:
    """Nearest ancestor with a project marker; the start dir when none is found.

    The home directory and filesystem root are never treated as a project, so a
    stray ``~/.git`` cannot make the index crawl a whole home folder.
    """
    p = Path(str(start)).expanduser().resolve()
    if p.is_file():
        p = p.parent
    home = Path.home().resolve()
    cur = p
    for _ in range(40):
        if cur == home or cur.parent == cur:
            break
        if any((cur / m).exists() for m in ROOT_MARKERS):
            return cur
        cur = cur.parent
    return p


def index_dir() -> Path:
    from plugin_env import resolve_snapshot_dir
    return resolve_snapshot_dir() / "codemap"


def index_path(root: Path) -> Path:
    digest = hashlib.sha256(str(root).encode("utf-8", "replace")).hexdigest()[:16]
    return index_dir() / f"{digest}.db"


# --------------------------------------------------------------------------- #
# Parsing (reuses structure_map)
# --------------------------------------------------------------------------- #

class FileFacts:
    __slots__ = ("defs", "imports", "refs", "lines", "parse_ok")

    def __init__(self) -> None:
        self.defs: List[Tuple[str, str, str, int, int, str]] = []
        self.imports: List[Tuple[str, Optional[str]]] = []
        self.refs: List[Tuple[int, str, str]] = []
        self.lines = 0
        self.parse_ok = True


def _rel(root: Path, p: Path) -> Optional[str]:
    try:
        return p.resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return None


def _first_existing(cands: Iterable[Path], root: Path) -> Optional[str]:
    for c in cands:
        try:
            if c.is_file():
                rel = _rel(root, c)
                if rel is not None:
                    return rel
        except OSError:
            continue
    return None


def _py_import_targets(node: ast.AST, path: Path, root: Path) -> List[Tuple[str, Optional[str]]]:
    out: List[Tuple[str, Optional[str]]] = []
    roots = [path.parent, root] + [a for a in path.parents if a.name == "src"]
    if isinstance(node, ast.Import):
        for alias in node.names:
            cands = [c for r in roots for c in sm._module_to_file_candidates(alias.name, r)]
            out.append((alias.name, _first_existing(cands, root)))
    elif isinstance(node, ast.ImportFrom):
        mod = node.module or ""
        label = "." * (node.level or 0) + mod
        if node.level:
            base = path.parent
            for _ in range(max(0, node.level - 1)):
                base = base.parent
            bases = [base]
        else:
            bases = roots
        cands: List[Path] = []
        for b in bases:
            if mod:
                cands.extend(sm._module_to_file_candidates(mod, b))
            for alias in node.names:
                sub = f"{mod}.{alias.name}" if mod else alias.name
                cands.extend(sm._module_to_file_candidates(sub, b))
        out.append((label or ".", _first_existing(cands, root)))
    return out


def _parse_python(source: str, path: Path, root: Path, facts: FileFacts) -> None:
    try:
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        facts.parse_ok = False
        return

    def visit(node: ast.AST, scope: List[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = scope[-1] if scope and scope[-1][:1].isupper() else None
                try:
                    sig = sm._summarize_function(child, owner=owner).signature
                except Exception:
                    sig = f"def {child.name}(...)"
                kind = "method" if owner else "function"
                qual = ".".join(scope + [child.name])
                if len(facts.defs) < MAX_DEFS_PER_FILE:
                    facts.defs.append((child.name, qual, kind, child.lineno,
                                       getattr(child, "end_lineno", child.lineno) or child.lineno, sig))
                _calls(child, qual)
                visit(child, scope + [child.name])
            elif isinstance(child, ast.ClassDef):
                try:
                    sig = sm._summarize_class(child).signature
                except Exception:
                    sig = f"class {child.name}"
                qual = ".".join(scope + [child.name])
                if len(facts.defs) < MAX_DEFS_PER_FILE:
                    facts.defs.append((child.name, qual, "class", child.lineno,
                                       getattr(child, "end_lineno", child.lineno) or child.lineno, sig))
                visit(child, scope + [child.name])
            elif isinstance(child, (ast.Import, ast.ImportFrom)) and not scope:
                facts.imports.extend(_py_import_targets(child, path, root))
            elif isinstance(child, (ast.Assign, ast.AnnAssign)) and not scope:
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                for t in targets:
                    for name in sm._flatten_targets(t):
                        if name and len(facts.defs) < MAX_DEFS_PER_FILE:
                            facts.defs.append((name, name, "variable", child.lineno,
                                               getattr(child, "end_lineno", child.lineno) or child.lineno,
                                               name))

    def _calls(fn: ast.AST, caller: str) -> None:
        stack = list(ast.iter_child_nodes(fn))
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(n, ast.Call) and len(facts.refs) < MAX_REFS_PER_FILE:
                name = sm._call_target_name(n.func)
                if name:
                    facts.refs.append((getattr(n, "lineno", 0), caller, name))
            stack.extend(ast.iter_child_nodes(n))

    try:
        visit(tree, [])
        # Module-level calls (scripts, decorators, registrations).
        for top in tree.body:
            if not isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for n in ast.walk(top):
                    if isinstance(n, ast.Call) and len(facts.refs) < MAX_REFS_PER_FILE:
                        name = sm._call_target_name(n.func)
                        if name:
                            facts.refs.append((getattr(n, "lineno", 0), "<module>", name))
    except RecursionError:
        facts.parse_ok = False


def _parse_js_ts(source: str, path: Path, root: Path, facts: FileFacts) -> None:
    try:
        imports, classes, functions, symbols = sm._collect_js_ts_structure(source)
        clean = sm._strip_js_ts_comments_and_strings(source).splitlines()
    except Exception:
        facts.parse_ok = False
        return
    starts: List[Tuple[int, str]] = []
    for c in classes:
        facts.defs.append((c.name, c.name, "class", c.lineno, c.lineno, c.signature))
        starts.append((c.lineno, c.name))
        for m in c.methods:
            q = f"{c.name}.{m.name}"
            facts.defs.append((m.name, q, "method", m.lineno, m.lineno, m.signature))
            starts.append((m.lineno, q))
    for f in functions:
        facts.defs.append((f.name, f.name, "function", f.lineno, f.lineno, f.signature))
        starts.append((f.lineno, f.name))
    for s in symbols:
        facts.defs.append((s.name, s.name, s.kind, s.lineno, s.lineno, s.signature))
        if s.kind in ("const", "let", "var"):
            starts.append((s.lineno, s.name))
    del facts.defs[MAX_DEFS_PER_FILE:]
    starts.sort()
    # End lines: the line before the next top-level start (approximate span).
    ends: Dict[str, int] = {}
    for i, (ln, q) in enumerate(starts):
        ends[q] = (starts[i + 1][0] - 1) if i + 1 < len(starts) else len(clean)
    facts.defs = [
        (n, q, k, ln, max(ln, ends.get(q, ln)) if k in ("class", "function", "method") else e, sig)
        for (n, q, k, ln, e, sig) in facts.defs
    ]
    seen_specs = set()
    raw_lines = source.splitlines()
    for raw, cl in zip(raw_lines, clean):
        if not sm.JS_TS_IMPORT_RE.match(cl.strip()):
            continue
        for rx in (sm._JS_TS_FROM_SPEC_RE, sm._JS_TS_SIDE_EFFECT_IMPORT_RE):
            for m in rx.finditer(raw):
                spec = m.group(1)
                if spec in seen_specs:
                    continue
                seen_specs.add(spec)
                target = None
                if spec.startswith("."):
                    target = _first_existing(sm._resolve_js_ts_spec(spec, path.parent), root)
                facts.imports.append((spec, target))
    import bisect
    keys = [s[0] for s in starts]
    for lineno, line in enumerate(clean, 1):
        if "(" not in line:
            continue
        for m in _JS_CALL_RE.finditer(line):
            name = m.group(1) or m.group(2)
            if not name or name in _JS_KEYWORDS:
                continue
            idx = bisect.bisect_right(keys, lineno) - 1
            caller = starts[idx][1] if idx >= 0 else "<module>"
            if idx >= 0 and starts[idx][0] == lineno and caller.split(".")[-1] == name:
                continue  # the definition line itself
            facts.refs.append((lineno, caller, name))
            if len(facts.refs) >= MAX_REFS_PER_FILE:
                return


def parse_file(path: Path, root: Path, source: Optional[str] = None) -> FileFacts:
    facts = FileFacts()
    if source is None:
        source = path.read_text(encoding="utf-8", errors="replace")
    facts.lines = source.count("\n") + (1 if source and not source.endswith("\n") else 0)
    if sm.is_python_file(path):
        _parse_python(source, path, root, facts)
    elif sm.is_js_ts_file(path):
        _parse_js_ts(source, path, root, facts)
    else:
        facts.parse_ok = False
    return facts


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #

def _iter_source_files(root: Path, deadline: float) -> Iterator[Path]:
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            if Path(fn).suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            if fn.endswith((".min.js", ".bundle.js", ".d.ts")):
                continue
            count += 1
            if count > MAX_FILES or time.monotonic() > deadline:
                return
            yield Path(dirpath) / fn


class CodeIndex:
    """One project's index. Use ``CodeIndex.open(root)`` then ``sync()``."""

    def __init__(self, root: Path, conn: sqlite3.Connection, db_path: Path):
        self.root = root
        self.conn = conn
        self.db_path = db_path
        self.complete = False

    @classmethod
    def open(cls, root: str | os.PathLike[str], db_path: Optional[Path] = None) -> "CodeIndex":
        root_p = Path(str(root)).resolve()
        db = db_path or index_path(root_p)
        try:
            db.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CodemapError(f"index dir unavailable: {exc}") from exc
        for attempt in (1, 2):
            conn = None
            try:
                conn = sqlite3.connect(str(db), timeout=3.0)
                conn.execute("PRAGMA busy_timeout=3000")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
                ver = conn.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
                if ver is None or ver[0] != SCHEMA_VERSION:
                    for t in ("files", "defs", "imports", "refs"):
                        conn.execute(f"DROP TABLE IF EXISTS {t}")
                    conn.executescript(_SCHEMA)
                    conn.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (SCHEMA_VERSION,))
                    conn.execute("INSERT OR REPLACE INTO meta VALUES('root', ?)", (str(root_p),))
                    conn.commit()
                return cls(root_p, conn, db)
            except sqlite3.DatabaseError as exc:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                # A corrupt index is only a cache: discard our own file once and rebuild.
                if attempt == 1 and isinstance(exc, sqlite3.DatabaseError) and not isinstance(
                        exc, sqlite3.OperationalError):
                    _discard_index_file(db)
                    continue
                raise CodemapError(f"index open failed: {exc}") from exc
        raise CodemapError("index open failed")

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    # -- writes ------------------------------------------------------------ #
    def _drop(self, rel: str) -> None:
        row = self.conn.execute("SELECT fid FROM files WHERE path=?", (rel,)).fetchone()
        if row is None:
            return
        for t in ("defs", "imports", "refs"):
            self.conn.execute(f"DELETE FROM {t} WHERE fid=?", (row[0],))
        self.conn.execute("DELETE FROM files WHERE fid=?", (row[0],))

    def _store(self, rel: str, st: os.stat_result, facts: FileFacts) -> None:
        self._drop(rel)
        lang = sm.detect_structure_language(rel)
        cur = self.conn.execute(
            "INSERT INTO files (path, mtime_ns, size, lang, tokens, lines, parse_ok) "
            "VALUES (?,?,?,?,?,?,?)",
            (rel, st.st_mtime_ns, st.st_size, lang, _tokens_from_bytes(st.st_size),
             facts.lines, 1 if facts.parse_ok else 0),
        )
        fid = cur.lastrowid
        self.conn.executemany(
            "INSERT INTO defs VALUES (?,?,?,?,?,?,?)",
            [(n, q, k, fid, ln, e, sig[:240]) for (n, q, k, ln, e, sig) in facts.defs],
        )
        self.conn.executemany("INSERT INTO imports VALUES (?,?,?)",
                              [(fid, m[:200], t) for (m, t) in facts.imports])
        # One row per (caller, callee): first call line plus a count. Callers
        # are answered per call site group, which keeps the DB small.
        grouped: Dict[Tuple[str, str], List[int]] = {}
        for ln, c, n in facts.refs:
            if len(n) < 2:
                continue
            g = grouped.get((c, n))
            if g is None:
                grouped[(c, n)] = [ln, 1]
            else:
                g[1] += 1
        self.conn.executemany("INSERT INTO refs VALUES (?,?,?,?,?)",
                              [(fid, v[0], c, n, v[1]) for (c, n), v in grouped.items()])

    def index_file(self, path: Path, st: Optional[os.stat_result] = None) -> bool:
        rel = _rel(self.root, path)
        if rel is None:
            return False
        try:
            st = st or path.stat()
            cap = MAX_PY_BYTES if sm.is_python_file(path) else MAX_JS_BYTES
            if st.st_size > cap:
                facts = FileFacts()
                facts.parse_ok = False
            else:
                facts = parse_file(path, self.root)
        except (OSError, ValueError):
            self._drop(rel)
            return False
        self._store(rel, st, facts)
        return True

    def refresh_file(self, path: str | os.PathLike[str]) -> bool:
        """Re-index one file if its mtime/size changed. Returns True if fresh."""
        p = Path(str(path)).resolve()
        rel = _rel(self.root, p)
        if rel is None or p.suffix.lower() not in SUPPORTED_SUFFIXES:
            return False
        try:
            st = p.stat()
        except OSError:
            self._drop(rel)
            self.conn.commit()
            return False
        row = self.conn.execute("SELECT mtime_ns, size FROM files WHERE path=?", (rel,)).fetchone()
        if row is None or row[0] != st.st_mtime_ns or row[1] != st.st_size:
            self.index_file(p, st)
            self.conn.commit()
        return True

    def sync(self, budget_s: Optional[float] = None) -> Dict[str, int]:
        """Bring the index up to date: new/changed files re-parsed, deleted dropped.

        Bounded by ``budget_s``; a sweep that runs out of time marks the index
        incomplete and the next query continues where it stopped.
        """
        start = time.monotonic()
        done = self.conn.execute("SELECT v FROM meta WHERE k='complete'").fetchone()
        first = done is None or done[0] != "1"
        budget = budget_s if budget_s is not None else (BUILD_BUDGET_S if first else SWEEP_BUDGET_S)
        deadline = start + budget
        known = {r[0]: (r[1], r[2]) for r in self.conn.execute("SELECT path, mtime_ns, size FROM files")}
        seen = set()
        stats = {"parsed": 0, "unchanged": 0, "removed": 0}
        timed_out = False
        try:
            for p in _iter_source_files(self.root, deadline):
                rel = _rel(self.root, p)
                if rel is None:
                    continue
                seen.add(rel)
                try:
                    st = p.stat()
                except OSError:
                    continue
                if known.get(rel) == (st.st_mtime_ns, st.st_size):
                    stats["unchanged"] += 1
                    continue
                if time.monotonic() > deadline:
                    timed_out = True
                    break
                self.index_file(p, st)
                stats["parsed"] += 1
                if stats["parsed"] % 200 == 0:
                    self.conn.commit()
                    if self.db_path.exists() and self.db_path.stat().st_size > MAX_DB_BYTES:
                        timed_out = True
                        break
            else:
                timed_out = time.monotonic() > deadline
            if not timed_out:
                for rel in set(known) - seen:
                    self._drop(rel)
                    stats["removed"] += 1
            self.complete = not timed_out
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES('complete', ?)",
                              ("1" if self.complete else "0",))
            self.conn.commit()
        except sqlite3.Error as exc:
            raise CodemapError(f"index sync failed: {exc}") from exc
        stats["ms"] = int((time.monotonic() - start) * 1000)
        return stats

    # -- reads ------------------------------------------------------------- #
    _DEF_COLS = "d.name, d.qual, d.kind, f.path, d.line, d.end_line, d.sig"
    _DEF_FROM = "FROM defs d JOIN files f ON f.fid=d.fid"

    def file_row(self, rel: str) -> Optional[Tuple]:
        return self.conn.execute(
            "SELECT path, mtime_ns, size, lang, tokens, lines, parse_ok FROM files WHERE path=?",
            (rel,)).fetchone()

    def find_defs(self, symbol: str, limit: int = 40) -> List[Tuple]:
        c, f = self._DEF_COLS, self._DEF_FROM
        if "." in symbol:
            rows = self.conn.execute(
                f"SELECT {c} {f} WHERE d.qual=? OR d.qual LIKE ? ORDER BY f.path, d.line LIMIT ?",
                (symbol, "%." + symbol, limit)).fetchall()
            if rows:
                return rows
            symbol = symbol.rsplit(".", 1)[-1]
        rows = self.conn.execute(
            f"SELECT {c} {f} WHERE d.name=? ORDER BY d.kind='variable', f.path, d.line LIMIT ?",
            (symbol, limit)).fetchall()
        if rows:
            return rows
        pat = "%" + symbol.replace("\\", "").replace("%", "\\%").replace("_", "\\_") + "%"
        return self.conn.execute(
            f"SELECT {c} {f} WHERE d.name LIKE ? ESCAPE '\\' AND d.kind!='variable' "
            "ORDER BY length(d.name), f.path, d.line LIMIT ?",
            (pat, min(limit, 15))).fetchall()

    def callers(self, symbol: str, limit: int = 300) -> List[Tuple]:
        """(path, first_line, caller, count) for every caller of ``symbol``'s name."""
        name = symbol.rsplit(".", 1)[-1]
        return self.conn.execute(
            "SELECT f.path, r.line, r.caller, r.n FROM refs r JOIN files f ON f.fid=r.fid "
            "WHERE r.callee=? ORDER BY f.path, r.line LIMIT ?",
            (name, limit)).fetchall()

    def outline(self, rel: str) -> List[Tuple]:
        return self.conn.execute(
            "SELECT d.name, d.qual, d.kind, d.line, d.end_line, d.sig FROM defs d "
            "JOIN files f ON f.fid=d.fid WHERE f.path=? ORDER BY d.line", (rel,)).fetchall()

    def imports_of(self, rel: str) -> List[Tuple]:
        return self.conn.execute(
            "SELECT i.module, i.target FROM imports i JOIN files f ON f.fid=i.fid WHERE f.path=?",
            (rel,)).fetchall()

    def importers_of(self, rel: str, limit: int = 60) -> List[str]:
        """Files importing ``rel``: resolved targets, plus unresolved imports whose
        module name ends with the file's stem (sys.path-style imports)."""
        stem = Path(rel).stem
        if stem in ("__init__", "index"):
            stem = Path(rel).parent.name or stem
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT f.path FROM imports i JOIN files f ON f.fid=i.fid "
            "WHERE f.path!=? AND (i.target=? OR (i.target IS NULL AND (i.module=? OR i.module LIKE ? OR i.module LIKE ?))) "
            "ORDER BY f.path LIMIT ?", (rel, rel, stem, "%." + stem, "%/" + stem, limit))]

    def file_tokens(self, rel: str) -> int:
        row = self.conn.execute("SELECT tokens FROM files WHERE path=?", (rel,)).fetchone()
        return int(row[0]) if row else 0

    def counts(self) -> Dict[str, int]:
        out = {}
        for t in ("files", "defs", "imports", "refs"):
            out[t] = int(self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        return out

    def rel(self, path: str | os.PathLike[str]) -> Optional[str]:
        p = Path(str(path))
        if not p.is_absolute():
            p = Path.cwd() / p
        return _rel(self.root, p)


def open_synced(start: str | os.PathLike[str], budget_s: Optional[float] = None) -> CodeIndex:
    """Open (building lazily) and sync the index for the project containing ``start``."""
    root = find_project_root(start)
    idx = CodeIndex.open(root)
    try:
        idx.sync(budget_s)
    except CodemapError:
        idx.close()
        raise
    except Exception as exc:  # parsing bugs must never escape to the agent
        idx.close()
        raise CodemapError(f"index sync failed: {exc}") from exc
    return idx


def symbols_in(text: str) -> Sequence[str]:
    """Identifiers in ``text`` (used by the replay helper)."""
    return re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text or "")
