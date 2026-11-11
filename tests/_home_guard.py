"""Keep the test suite away from the developer's REAL home directory.

THE BUG CLASS
-------------
Product modules bind paths at IMPORT time from HOME / CLAUDE_CONFIG_DIR / XDG_*
(``measure.SNAPSHOT_DIR``, ``measure.TRENDS_DB``, ``plugin_env._USER_CONFIG_DIR``,
...). The first test that imports one of them freezes those paths. A later test
that does ``monkeypatch.setenv("HOME", tmp)`` then changes nothing for the cached
constants, so the code under test reads and WRITES the developer's real
``~/.claude`` (trends.db, checkpoints, settings, plugin data, session stores).
``test_measure_variant_redacts_feature`` read the real ``trends.db`` this way and
passed or failed depending on test order and on what was on the dev's machine.

TWO LAYERS
----------
1. SANDBOX (prevention). Before any product module is imported, HOME, USERPROFILE
   and the XDG_* dirs are pointed at a throwaway directory and every runtime-home
   override variable is dropped. Whatever a module caches at import time is then
   a sandbox path, never the real home, and child processes inherit it too.
2. AUDIT GUARD (detection). A ``sys.addaudithook`` watches ``open``,
   ``sqlite3.connect``, ``os.mkdir/rename/remove/listdir/scandir`` and friends.
   Any touch of the REAL home (outside a small justified allowlist) is attributed
   to the running test, which then fails at teardown with the path and the event.

LIMITS: audit events are per-process. A subprocess that is handed the real HOME
explicitly is not seen, and ``os.stat``/``lstat``/``access`` raise no audit event, so the guard also
wraps those three and the cached pathlib accessor aliases on Python 3.9/3.10. C-level stats
(``os.scandir`` entries, ``shutil`` fast paths) are not seen.

``TOKEN_OPTIMIZER_TEST_REAL_HOME_OVERRIDE=<dir>`` makes the guard treat a decoy
directory as the real home (used to prove the guard goes red without touching
real data).

Set ``TOKEN_OPTIMIZER_TEST_HOME_SANDBOX=0`` to skip the sandbox (the guard then
reports every real-home touch, which is how the guard itself is proven) and
``TOKEN_OPTIMIZER_TEST_HOME_GUARD=0`` to switch the guard off.
"""

from __future__ import annotations

import atexit
import os
import pathlib
import re
import shutil
import sys
import tempfile
import threading
from types import BuiltinFunctionType
from pathlib import Path
from sys import addaudithook as _register_audit_hook

REPO = Path(__file__).resolve().parent.parent

_ENV_SANDBOX = "TOKEN_OPTIMIZER_TEST_HOME_SANDBOX"
_ENV_GUARD = "TOKEN_OPTIMIZER_TEST_HOME_GUARD"


def _real_home() -> str:
    # Self-proof hook: treat a decoy directory as "the real home" so the guard
    # can be shown catching a leak without touching the developer's actual data.
    override = os.environ.get("TOKEN_OPTIMIZER_TEST_REAL_HOME_OVERRIDE")
    if override:
        return os.path.realpath(override)
    try:
        import pwd

        return os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:  # Windows, or no passwd entry
        return os.path.realpath(os.path.expanduser("~"))


# Captured at import, before the sandbox rewrites HOME.
REAL_HOME = _real_home()
_REAL_HOME_FORMS = tuple({REAL_HOME, os.path.abspath(os.path.expanduser("~"))})

# Variables that redirect a runtime's home/data dir. Dropped for the session so
# an ambient developer value cannot steer a cached path back to real data.
_HOME_OVERRIDE_VARS = (
    "CLAUDE_CONFIG_DIR", "CLAUDE_PLUGIN_DATA", "TOKEN_OPTIMIZER_PLUGIN_DATA",
    "CODEX_HOME", "HERMES_HOME", "COPILOT_HOME", "GROK_HOME",
    "TOKEN_OPTIMIZER_COPILOT_HOME", "TOKEN_OPTIMIZER_CURSOR_HOME",
    "TOKEN_OPTIMIZER_ANTIGRAVITY_HOME", "TOKEN_OPTIMIZER_GROK_HOME",
    "TOKEN_OPTIMIZER_DEVIN_HOME", "DEVIN_CONFIG_DIR",
    "TOKEN_OPTIMIZER_SNAPSHOT_DIR", "OPENCODE_DATA_DIR", "OPENCODE_CONFIG_DIR",
    "HOMEDRIVE", "HOMEPATH",
    # Hosted-VM markers (runtime_env.remote_host): a suite run INSIDE a Claude
    # Code cloud or Cowork session would otherwise flip every test into the
    # cloud lean profile / Cowork state routing. Tests that need them set them.
    "CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
    "TOKEN_OPTIMIZER_REMOTE_HOST", "TOKEN_OPTIMIZER_REMOTE_LEAN",
    "TOKEN_OPTIMIZER_CLOUD_KIT_VERSION", "TOKEN_OPTIMIZER_ALLOW_FOREIGN_HOME_SCHEDULER",
    "CLAUDE_PROJECT_DIR", "PYTHONPYCACHEPREFIX",
)

SANDBOX: Path | None = None

# (event kind, regex on the real-home-relative path, reason). Read-only and
# justified cases only; a write never belongs here.
ALLOWLIST: list[tuple[str, str, str]] = [
    # _compute_home_redirected compares the sandbox home with the account home
    # before touching schedulers. Resolving/samefile on that directory is a
    # metadata-only identity check; no child, content read or listing is exempt.
    ("STAT", r"^\.$", "account-home directory identity for scheduler isolation"),
    # Host CLI discovery: PATH shims (~/.local/bin/claude, devin) are symlinks
    # into these version dirs, and the code under test stats/access-checks the
    # resolved binary to find the host CLI. Existence probe of an executable,
    # not user data; STAT only, so any open/list/write there still fails.
    ("STAT", r"^\.local/share/(claude|devin)(/|$)", "host CLI binary discovery"),
    # npm discovery for the cloud-kit packaging test (shutil.which("npm") stats
    # the node version manager's bin dir). STAT only, same reasoning as above.
    ("STAT", r"^\.(nvm|volta|fnm|asdf)(/|$)", "npm binary discovery"),
    # Live claude-process classification (dashboard health scan): when a
    # Claude CLI bundled by the desktop app is running on the dev machine, the
    # scan stats its .app bundle (MacOS/, Resources/app.asar, Electron
    # Framework) to tell the desktop app from a terminal CLI. Read-only probe
    # of an installed binary, not user data, and only reachable while such a
    # process happens to run, which made two dashboard tests fail in one full
    # run and pass in the next (2026-10-19). STAT only.
    ("STAT", r"^Library/Application Support/Claude/claude-code(/|$)", "desktop-bundled claude binary probe"),
]


def sandbox_enabled() -> bool:
    return os.environ.get(_ENV_SANDBOX, "1") not in ("0", "false", "no")


def guard_enabled() -> bool:
    return os.environ.get(_ENV_GUARD, "1") not in ("0", "false", "no")


def install_sandbox() -> Path | None:
    """Point HOME and friends at a throwaway dir. Idempotent; call at import."""
    global SANDBOX
    if SANDBOX is not None or not sandbox_enabled():
        return SANDBOX
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="to-test-home-")))
    home = root / "home"
    home.mkdir()
    for var in _HOME_OVERRIDE_VARS:
        os.environ.pop(var, None)
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    os.environ["XDG_CONFIG_HOME"] = str(home / ".config")
    os.environ["XDG_CACHE_HOME"] = str(home / ".cache")
    os.environ["XDG_DATA_HOME"] = str(home / ".local" / "share")
    os.environ["XDG_STATE_HOME"] = str(home / ".local" / "state")
    if os.name == "nt":
        # Cursor, Copilot and Cowork read %APPDATA% before falling back to HOME.
        os.environ["APPDATA"] = str(home / "AppData" / "Roaming")
        # hooks/run.py and measure.py put bytecode under %LOCALAPPDATA% (the
        # Windows twin of XDG_CACHE_HOME). Left real, an in-process run.main()
        # set sys.pycache_prefix to the runner's real profile and every later
        # import read/wrote .pyc there (~3800 guard errors, Windows CI 5.13.50).
        os.environ["LOCALAPPDATA"] = str(home / "AppData" / "Local")
    _install_scheduler_shims(root)
    SANDBOX = root
    atexit.register(shutil.rmtree, str(root), True)
    return SANDBOX


# Service managers are per ACCOUNT, not per HOME: a test that reaches a real
# ``launchctl bootstrap`` re-points the developer's live job at the sandbox
# (2026-10-19: an e2e run re-bootstrapped the real dashboard agent). The
# product guard refuses those calls from a redirected HOME, but a guard is not
# a reason to let the real binaries be reachable. Each tool that exists on this
# machine gets a no-op shim first on PATH (so ``shutil.which`` answers exactly
# as before), which logs its argv to SCHEDULER_LOG and exits 0. Child processes
# inherit the PATH. POSIX only: Windows CreateProcess resolves ``schtasks`` to
# ``schtasks.exe`` and would skip a script shim, and the Windows CI runner has
# no live user jobs to hijack.
SCHEDULER_TOOLS = ("launchctl", "systemctl", "schtasks", "crontab", "loginctl")
SCHEDULER_LOG: Path | None = None


def _install_scheduler_shims(root: Path) -> None:
    global SCHEDULER_LOG
    if os.name == "nt":
        return
    shim_dir = root / "scheduler-shims"
    shim_dir.mkdir(exist_ok=True)
    log = root / "scheduler-calls.log"
    for tool in SCHEDULER_TOOLS:
        if shutil.which(tool) is None:
            continue
        shim = shim_dir / tool
        shim.write_text(
            "#!/bin/sh\n"
            f'printf "%s %s\\n" "{tool}" "$*" >> "{log}" 2>/dev/null\n'
            "exit 0\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)
    os.environ["PATH"] = str(shim_dir) + os.pathsep + os.environ.get("PATH", "")
    os.environ["TOKEN_OPTIMIZER_TEST_SCHEDULER_LOG"] = str(log)
    SCHEDULER_LOG = log


# --- audit guard -----------------------------------------------------------

_EXEMPT_PREFIXES: tuple[str, ...] = ()
_PATH_DIRS: tuple[str, ...] = ()
_REPO_ANCESTORS: tuple[str, ...] = tuple(
    str(p) for p in REPO.parents if str(p).startswith(REAL_HOME))
# Ancestors of the system temp dir under the real home (Windows only:
# AppData\Local\Temp). pytest's tmp_path lives there, so a product walk up
# from a tmp_path (run.py's ``.git`` work-tree probe) stats
# ``~/AppData/Local/.git``, ``~/AppData/.git``, ``~/.git``: existence probes
# of the temp dir's parents, the same thing the repo-ancestor rule allows.
_TEMP_ANCESTORS: tuple[str, ...] = tuple(
    str(p) for p in Path(os.path.realpath(tempfile.gettempdir())).parents
    if str(p) == REAL_HOME or str(p).startswith(REAL_HOME + os.sep))
# The names an upward project-root walk (run.py's work-tree probe, the code map's root finder)
# stats at every ancestor. Existence probes of these names only; ``~/.claude`` and any other
# real directory still count as a touch.
_PROJECT_ROOT_MARKERS = (".git", "pyproject.toml", "package.json", "setup.py", "setup.cfg",
                         "go.mod", "Cargo.toml")
HITS: list[tuple[str, str, str, str]] = []  # (nodeid, event, kind, path)
_current: dict[str, str] = {"id": ""}
_installed = False
_busy = threading.local()

_WRITE_FLAGS = (
    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
)
_PATH_EVENTS = {
    "os.mkdir": ("W", 0), "os.remove": ("W", 0), "os.rmdir": ("W", 0),
    "os.chmod": ("W", 0), "os.truncate": ("W", 0), "os.utime": ("W", 0),
    "os.rename": ("W", 1), "os.listdir": ("LIST", 0), "os.scandir": ("LIST", 0),
}


def _compute_exempt() -> tuple[str, ...]:
    """Real-home subtrees that are not user data: the repo, the interpreter, and
    the system temp dir (on Windows it lives under the home, in AppData\\Local\\Temp,
    and pytest's tmp_path is inside it)."""
    out = {str(REPO), os.path.realpath(tempfile.gettempdir())}
    for p in (sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix):
        out.add(os.path.realpath(p))
    for p in sys.path:
        if p:
            out.add(os.path.realpath(p))
    return tuple(sorted(x for x in out if x.startswith(REAL_HOME + os.sep)))


def _compute_path_dirs() -> tuple[str, ...]:
    """PATH entries under the real home (~/.bun/bin, ~/.nvm/...): executable
    lookups (``shutil.which``) stat every candidate, which is not user data."""
    out = set()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d:
            out.add(os.path.realpath(d))
    return tuple(sorted(x for x in out if x.startswith(REAL_HOME + os.sep)))


def _stat_exempt(full: str) -> bool:
    """Existence probes that are not reads of user data (kind STAT only):
    executable lookup along PATH, and the cwd-upward walk for CLAUDE.md that
    probes ``<ancestor-of-repo>/CLAUDE.md`` and ``<ancestor>/.claude/CLAUDE.md``,
    and ``<temp-dir ancestor>/<project root marker>`` (an upward work-tree or project-root walk
    from a tmp_path; only those names, so ``~/.claude`` and other real dirs still count)."""
    for ex in _PATH_DIRS:
        if full == ex or full.startswith(ex + os.sep) or ex.startswith(full + os.sep):
            return True
    parent = os.path.dirname(full)
    if parent in _TEMP_ANCESTORS and os.path.basename(full) in _PROJECT_ROOT_MARKERS:
        return True
    for anc in _REPO_ANCESTORS:
        if parent == anc or (os.path.basename(parent) == ".claude"
                             and os.path.dirname(parent) == anc):
            return True
    return False


def _under_real_home(path: str, kind: str | None = None) -> str | None:
    """Real-home-relative path if ``path`` is user data under REAL_HOME."""
    for home in _REAL_HOME_FORMS:
        if path == home or path.startswith(home + os.sep):
            break
    else:
        return None
    full = os.path.realpath(path)
    if full != REAL_HOME and not full.startswith(REAL_HOME + os.sep):
        return None
    for ex in _EXEMPT_PREFIXES:
        # Inside an exempt subtree, or an ancestor of one (tools stat the parents
        # of the repo and the interpreter to resolve them).
        if full == ex or full.startswith(ex + os.sep) or ex.startswith(full + os.sep):
            return None
    if kind == "STAT" and _stat_exempt(full):
        return None
    return full[len(REAL_HOME):].lstrip(os.sep) or "."


def _allowed(kind: str, rel: str) -> bool:
    # Allowlist patterns use "/"; Windows paths arrive with "\\".
    rel = rel.replace(os.sep, "/")
    return any(k == kind and re.search(rx, rel) for k, rx, _ in ALLOWLIST)


def _to_path(arg) -> str | None:
    if isinstance(arg, int) or arg is None:
        return None
    try:
        s = os.fsdecode(arg)
    except Exception:
        return None
    return os.path.abspath(s) if s else None


def _hook(event: str, args: tuple) -> None:
    if event == "open":
        path = _to_path(args[0])
        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
        kind = "W" if flags & _WRITE_FLAGS else "R"
    elif event == "sqlite3.connect":
        path, kind = _to_path(args[0]), "SQLITE"
    elif event in _PATH_EVENTS:
        kind, idx = _PATH_EVENTS[event]
        path = _to_path(args[idx]) if len(args) > idx else None
    else:
        return
    if not path:
        return
    _busy.on = True  # realpath() below stats; do not record our own probing
    try:
        rel = _under_real_home(path)
    finally:
        _busy.on = False
    if rel is None or _allowed(kind, rel):
        return
    HITS.append((_current["id"], event, kind, "~/" + rel))


def _wrap_stat(name: str, real, kind: str):
    """``os.stat``/``lstat``/``access`` raise no audit event, yet
    ``Path.exists()`` on a cached path is how the trends.db leak steered
    behaviour. Wrap them to record real-home probes."""

    def wrapper(path, *args, **kwargs):
        # realpath() itself calls os.lstat, so guard against re-entry.
        if not getattr(_busy, "on", False):
            _busy.on = True
            try:
                p = _to_path(path)
                if p is not None:
                    rel = _under_real_home(p, kind)
                    if rel is not None and not _allowed(kind, rel):
                        HITS.append((_current["id"], "os." + name, kind, "~/" + rel))
            finally:
                _busy.on = False
        return real(path, *args, **kwargs)

    wrapper.__wrapped__ = real
    return wrapper


def install_guard() -> None:
    global _installed, _EXEMPT_PREFIXES, _PATH_DIRS
    if _installed or not guard_enabled():
        return
    _EXEMPT_PREFIXES = _compute_exempt()
    _PATH_DIRS = _compute_path_dirs()
    _register_audit_hook(_hook)
    for name in ("stat", "lstat", "access"):
        setattr(os, name, _wrap_stat(name, getattr(os, name), "STAT"))
    # Python 3.9/3.10 pathlib caches os.stat (and 3.9 os.lstat) on its
    # accessor before this guard replaces os functions. Wrap those cached
    # callables too. A staticmethod preserves the unbound builtin signature.
    accessor = getattr(pathlib, "_NormalAccessor", None)
    for name in ("stat", "lstat"):
        cached = vars(accessor).get(name) if accessor is not None else None
        if cached is not None and cached is not getattr(os, name):
            # Only builtin aliases need wrapping; 3.10's lstat is a Python
            # method that delegates to the now-wrapped accessor.stat.
            if isinstance(cached, BuiltinFunctionType):
                setattr(accessor, name, staticmethod(_wrap_stat(name, cached, "STAT")))
    # On Windows these use C fast paths (nt._path_exists etc.) that never call
    # os.stat, and from 3.14 Path.exists() delegates to os.path.exists.
    for name in ("exists", "lexists", "isfile", "isdir", "islink"):
        setattr(os.path, name, _wrap_stat("path." + name, getattr(os.path, name), "STAT"))
    _installed = True


# --- child processes --------------------------------------------------------
#
# The audit hook only sees this process. A child started with an explicit env
# that has no HOME resolves Path.home() from the password database, i.e. the
# REAL home, and a child handed the real HOME (or a data-dir override under it)
# writes the real ledger. In August 2026 a hook harness wrote 532 rows into a
# developer's live trends.db that way. Every child env is therefore rewritten
# before spawn: a missing HOME becomes the sandbox, and any home/data-dir
# variable that points into the real home is replaced by its sandbox
# equivalent and reported as a hit, so the test fails without the child ever
# seeing the real data dir.

_CHILD_HOME_VARS = ("HOME", "USERPROFILE")
_CHILD_DIR_VARS = _CHILD_HOME_VARS + tuple(v for v in _HOME_OVERRIDE_VARS if v not in ("HOMEDRIVE", "HOMEPATH"))
_subprocess_installed = False


def _child_real_home_rel(value: str) -> str | None:
    """Real-home-relative path when ``value`` is the real home or user data in it.

    Unlike the stat rule, an ANCESTOR of the repo or interpreter (the real home
    itself) is not exempt here: a child given it as HOME would write there.
    """
    _busy.on = True
    try:
        full = os.path.realpath(os.path.abspath(os.path.expanduser(value)))
    finally:
        _busy.on = False
    if full == REAL_HOME:
        return "."
    if not full.startswith(REAL_HOME + os.sep):
        return None
    if any(full == ex or full.startswith(ex + os.sep) for ex in _EXEMPT_PREFIXES):
        return None
    return full[len(REAL_HOME):].lstrip(os.sep)


def sanitize_child_env(env):
    """The env a child should get: never the real home, never without HOME."""
    if env is None or SANDBOX is None:
        return env
    home = str(SANDBOX / "home")
    fixed = dict(env)
    # A test that names one home variable means THAT home. Windows reads USERPROFILE and POSIX
    # reads HOME, so the missing sibling takes the named value; only an env naming neither gets the
    # sandbox. (Defaulting USERPROFILE to the sandbox sent a Windows child to the wrong home
    # whenever a test passed only HOME: CI 5.13.70, ~15 Windows failures.)
    named = next((fixed[v] for v in _CHILD_HOME_VARS if isinstance(fixed.get(v), str) and fixed[v]), home)
    for var in _CHILD_HOME_VARS:
        fixed.setdefault(var, named)
    for var in _CHILD_DIR_VARS:
        value = fixed.get(var)
        if not isinstance(value, str) or not value:
            continue
        rel = _child_real_home_rel(value)
        if rel is None:
            continue
        HITS.append((_current["id"], "subprocess.env", "W", "%s=~/%s" % (var, rel)))
        fixed[var] = home if var in _CHILD_HOME_VARS or rel == "." else os.path.join(home, rel)
    if os.name == "nt":
        windows_home_alias(fixed, home)
    return fixed


def windows_home_alias(env: dict, sandbox_home: str) -> dict:
    """Make a test's HOME mean the home on Windows, where Python reads USERPROFILE.

    Tests are written for POSIX, where ``HOME`` is the home. A test that copies ``os.environ`` and sets
    only ``HOME`` would, on Windows, leave the sandbox ``USERPROFILE`` in place and its child would
    resolve the wrong home (CI 5.13.70: ~25 failures). When USERPROFILE is still the sandbox default
    and HOME names something else, USERPROFILE follows HOME. A test that sets USERPROFILE itself is left alone.
    """
    named = env.get("HOME")
    if isinstance(named, str) and named and env.get("USERPROFILE") == sandbox_home and named != sandbox_home:
        env["USERPROFILE"] = named
    return env


def install_windows_home_alias() -> None:
    """``monkeypatch.setenv("HOME", x)`` also moves USERPROFILE on Windows (see windows_home_alias)."""
    if os.name != "nt" or SANDBOX is None or not sandbox_enabled():
        return
    import pytest

    original = pytest.MonkeyPatch.setenv
    if getattr(original, "_home_alias", False):
        return
    sandbox_home = str(SANDBOX / "home")

    def setenv(self, name, value, prepend=None):
        before = os.environ.get("HOME")
        original(self, name, value, prepend)
        if name == "HOME" and os.environ.get("USERPROFILE") in (sandbox_home, before):
            original(self, "USERPROFILE", str(value), None)

    setenv._home_alias = True
    pytest.MonkeyPatch.setenv = setenv


def install_subprocess_guard() -> None:
    global _subprocess_installed
    if _subprocess_installed or not sandbox_enabled():
        return
    import subprocess

    real_init = subprocess.Popen.__init__

    def guarded_init(self, *args, **kwargs):
        if "env" in kwargs:
            kwargs["env"] = sanitize_child_env(kwargs["env"])
        elif len(args) > 9:  # env is Popen's 10th positional parameter
            args = args[:9] + (sanitize_child_env(args[9]),) + args[10:]
        return real_init(self, *args, **kwargs)

    guarded_init.__wrapped__ = real_init
    subprocess.Popen.__init__ = guarded_init
    _subprocess_installed = True


def set_current(nodeid: str) -> None:
    _current["id"] = nodeid


def take_hits(nodeid: str) -> list[tuple[str, str, str, str]]:
    mine = [h for h in HITS if h[0] == nodeid]
    HITS[:] = [h for h in HITS if h[0] != nodeid]
    return mine


def format_hits(hits) -> str:
    seen, lines = set(), []
    for _, event, kind, path in hits:
        key = (event, kind, path)
        if key not in seen:
            seen.add(key)
            lines.append(f"  {kind:6} {event:16} {path}")
    return "\n".join(lines)
