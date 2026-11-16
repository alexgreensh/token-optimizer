#!/usr/bin/env python3
"""Discovery hint for the code map (SessionStart and SubagentStart).

Tells the agent, in approximately 75 to 81 estimated tokens depending on installation paths, to use the code map before grepping or
reading whole files for structural questions, and warms the index in a
detached background process so the first lookup is fast. Silent (no output,
no spawn) when the feature is off, when the cwd is the home directory or the
filesystem root, or when the project has no Python/JS/TS files.

Run as a hook: ``codemap_hint.py`` reads the hook payload on stdin and prints
SubagentStart JSON (additionalContext). SessionStart uses ``session_text()``
from hooks/sessionstart_runner.py, which wraps plain text itself.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

PROBE_MAX_ENTRIES = 400
PROBE_MAX_S = 0.25
LOCK_FRESH_S = 300.0
OLD_INDEX_AGE_S = 7 * 86400
OLD_INDEX_CLEANUP_LIMIT = 10
OLD_INDEX_CLEANUP_INTERVAL_S = 86400
# Subagents that never explore code: no hint for them.
SKIP_AGENT_TYPES = frozenset({"statusline-setup", "claude-code-guide", "web-fetch"})


def _has_code(root: Path) -> bool:
    import codemap_index as ci
    seen = 0
    deadline = time.monotonic() + PROBE_MAX_S
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ci.SKIP_DIRS and not d.startswith(".")]
        seen += 1 + len(dirnames)
        for fn in filenames:
            seen += 1
            if Path(fn).suffix.lower() in ci.SUPPORTED_SUFFIXES:
                return True
        if seen > PROBE_MAX_ENTRIES or time.monotonic() > deadline:
            return False
    return False


def _project_root(cwd: str) -> Optional[Path]:
    import codemap_index as ci
    if not cwd:
        return None  # hosts always send cwd; never guess (and never index) an unknown dir
    try:
        p = Path(cwd).expanduser().resolve()
    except OSError:
        return None
    if p == Path.home().resolve() or p.parent == p or not p.is_dir():
        return None
    root = ci.find_project_root(p)
    return root if _has_code(root) else None


def warm_index(root: Path) -> bool:
    """Start a detached, niced index build unless one is fresh or running."""
    import codemap_index as ci
    db = ci.index_path(root)
    lock = db.with_suffix(".building")
    try:
        if db.exists():
            import sqlite3
            conn = sqlite3.connect(str(db), timeout=1.0)
            try:
                row = conn.execute("SELECT v FROM meta WHERE k='complete' AND "
                                   "(SELECT v FROM meta WHERE k='schema')=?", (ci.SCHEMA_VERSION,)).fetchone()
            finally:
                conn.close()
            if row and row[0] == "1":
                return True  # an existing complete index needs no warm-up
        if lock.exists() and time.time() - lock.stat().st_mtime < LOCK_FRESH_S:
            return False
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(os.getpid()), encoding="utf-8")
        from spawn_utils import spawn_detached
        script = str(Path(__file__).resolve().parent / "codemap.py")
        import subprocess
        # DEVNULL streams: an inherited hook stdout pipe would hold the host's
        # SessionStart/SubagentStart read open until the build finishes.
        proc = spawn_detached([sys.executable, script, "refresh", "--root", str(root),
                               "--budget", "120", "--quiet", "--background"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return proc is not None
    except Exception:
        return False


def hint_text(root: Path) -> str:
    script = shlex.quote(str(Path(__file__).resolve().parent / "codemap.py"))
    py = shlex.quote(sys.executable or "python3")
    import codemap_index as ci
    import sqlite3
    conn = sqlite3.connect(ci.index_path(root).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT path FROM files ORDER BY path LIMIT 1").fetchone()
    finally:
        conn.close()
    if not row:
        return ""
    return f"[Token Optimizer] Code map: inspect a file with `{py} {script} outline {shlex.quote(row[0])}`."


def _clean_old_indexes(directory: Path) -> None:
    """Remove a few inactive legacy indexes, without following symlinks."""
    import re
    import sqlite3
    import stat

    # Directory-relative operations keep every unlink inside the opened folder.
    if not hasattr(os, "O_NOFOLLOW") or os.unlink not in os.supports_dir_fd:
        return
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        cutoff = time.time() - OLD_INDEX_AGE_S
        removed = 0
        with os.scandir(fd) as entries:
            for entry in entries:
                if removed >= OLD_INDEX_CLEANUP_LIMIT:
                    break
                if not re.fullmatch(r"[0-9a-f]{16}\.db", entry.name):
                    continue
                try:
                    old = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                    if not stat.S_ISREG(old.st_mode) or old.st_mtime > cutoff:
                        continue
                    sidecars = []
                    for suffix in ("-wal", "-shm"):
                        name = entry.name + suffix
                        try:
                            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        if not stat.S_ISREG(info.st_mode) or info.st_mtime > cutoff:
                            break
                        sidecars.append(name)
                    else:
                        # Inspect the database through a no-follow descriptor; an
                        # immutable read cannot create or modify SQLite sidecars.
                        source_fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                        try:
                            current = os.fstat(source_fd)
                            if (current.st_ino, current.st_dev, current.st_mtime_ns, current.st_size) != (
                                    old.st_ino, old.st_dev, old.st_mtime_ns, old.st_size):
                                continue
                            conn = sqlite3.connect(f"file:/dev/fd/{source_fd}?mode=ro&immutable=1", uri=True)
                            try:
                                version = conn.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
                            finally:
                                conn.close()
                            if not version or version[0] != "2":
                                continue
                        finally:
                            os.close(source_fd)
                        os.unlink(entry.name, dir_fd=fd)
                        removed += 1
                        for name in sidecars:
                            try:
                                os.unlink(name, dir_fd=fd)
                            except OSError:
                                pass
                except (OSError, sqlite3.Error):
                    continue
    except OSError:
        pass
    finally:
        os.close(fd)


def cleanup_old_indexes_daily() -> None:
    """Run the legacy cleanup at most once a day when the engine is enabled."""
    try:
        import codemap_index as ci
        import fcntl
        import stat
        if not ci.codemap_enabled() or not hasattr(os, "O_NOFOLLOW"):
            return
        directory = ci.index_dir()
        fd = os.open(directory, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0))
        try:
            name = ".legacy-cleanup-timestamp"
            try:
                marker = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 0o600, dir_fd=fd)
                first = True
            except FileExistsError:
                marker = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                first = False
            try:
                if not stat.S_ISREG(os.fstat(marker).st_mode):
                    return
                fcntl.flock(marker, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if not first and time.time() - os.fstat(marker).st_mtime < OLD_INDEX_CLEANUP_INTERVAL_S:
                    return
                os.utime(marker, None)
                _clean_old_indexes(directory)
            finally:
                os.close(marker)
        finally:
            os.close(fd)
    except (ImportError, OSError):
        pass


def session_text(hook_input: dict) -> str:
    """Plain hint text for SessionStart ('' when off or not a code project)."""
    try:
        import codemap_index as ci
        if not ci.codemap_hint_enabled():
            return ""
        root = _project_root(str(hook_input.get("cwd") or ""))
        if root is None:
            return ""
        if not warm_index(root):
            return ""
        import sqlite3
        db = ci.index_path(root)
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
        try:
            row = conn.execute("SELECT v FROM meta WHERE k='complete'").fetchone()
        finally:
            conn.close()
        if not row or row[0] != "1":
            return ""
        sid = str(hook_input.get("session_id") or "")
        if not sid:
            return ""
        agent_id = str(hook_input.get("agent_id") or "")
        if agent_id:
            sid += ":" + agent_id
        import hashlib
        for old in db.parent.glob("hint-*"):
            try:
                if time.time() - old.stat().st_mtime > 7 * 86400:
                    old.unlink()
            except OSError:
                pass
        marker = db.parent / ("hint-" + hashlib.sha256(sid.encode()).hexdigest())
        try:
            with marker.open("x", encoding="utf-8"):
                pass
        except FileExistsError:
            return ""
        return hint_text(root)
    except Exception:
        return ""


def subagent_payload(hook_input: dict) -> Optional[dict]:
    agent_type = str(hook_input.get("agent_type") or "")
    if agent_type in SKIP_AGENT_TYPES:
        return None
    text = session_text(hook_input)
    if not text:
        return None
    return {"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": text}}


def main() -> int:
    try:
        from hook_io import read_stdin_hook_input
        hook_input = read_stdin_hook_input() or {}
    except Exception:
        try:
            hook_input = json.loads(sys.stdin.read() or "{}")
        except Exception:
            hook_input = {}
    if not isinstance(hook_input, dict):
        return 0
    payload = subagent_payload(hook_input)
    if payload:
        print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
