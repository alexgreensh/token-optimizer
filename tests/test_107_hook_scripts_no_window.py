"""#107: no Windows console flash from the hook-script surface.

These scripts run as Claude Code hooks on every prompt. On Windows, spawning a
console executable (``git``, ``where``, ``tasklist``, ``wmic``, ``powershell``,
``cmd``, or the user's own Bash-tool command) from a console-less parent makes
the OS allocate a console for the child, which the Desktop app user sees as a
cmd window flashing on screen. ``CREATE_NO_WINDOW`` suppresses that allocation.

The load-bearing test here is a cross-platform SOURCE SCAN (AST, so a match
cannot be faked by a comment or a string): every ``subprocess.run`` /
``Popen`` / ``call`` / ``check_output`` / ``check_call`` in the surface must
pass ``creationflags`` whose expression mentions the guarded
``CREATE_NO_WINDOW`` constant -- either directly, via the module-level
``_NO_WINDOW`` alias, or by spreading kwargs that came from
``spawn_utils.detach_spawn_kwargs()``. ``os.system`` / ``os.popen`` /
``os.startfile`` are banned outright: they cannot carry creation flags at all.

The scan is guarded against passing vacuously (a broken matcher finding zero
sites) by ``test_scan_finds_the_known_spawn_sites``, which asserts the known
census is still visible to the matcher.

Runtime behaviour on real Windows is covered by the ``os.name == "nt"`` tests
at the bottom (skipped elsewhere), which spawn a real child and ask it via
``GetConsoleWindow()`` whether a console was allocated.

Run: python3 -m pytest tests/test_107_hook_scripts_no_window.py -v
"""
import ast
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

sys.path.insert(0, str(SCRIPTS))

_CREATE_NO_WINDOW = 0x08000000

# The full Lane-B surface. Every one of these files is scanned; the ones with
# no spawn at all are covered too, so a spawn added later cannot slip in
# without creation flags.
SURFACE = [
    "bash_hook.py", "read_cache.py", "archive_result.py", "context_intel.py",
    "injection.py", "context_pressure.py", "activity_tracker.py",
    "session_store.py", "hook_runtime.py", "hook_io.py", "bash_compress.py",
    "compression_backfill.py", "compression_log.py", "structure_map.py",
    "structure_replay.py", "delta_diff.py", "outline.py", "token_estimate.py",
    "refetch_fingerprint.py", "refetch_guard.py", "credential_patterns.py",
    "routing_advisor.py", "runtime_env.py", "plugin_env.py", "utf8_io.py",
    "spawn_utils.py", "benchmark.py", "install_reconcile.py",
]

_SPAWN_ATTRS = {"run", "Popen", "call", "check_output", "check_call"}
_BANNED_OS_ATTRS = {"system", "popen", "startfile"}

# Minimum spawn-site count per file, from the manual census. Purely an
# anti-vacuous-pass guard: if the AST matcher stops matching, these fail loudly
# instead of the suite going green on zero findings.
KNOWN_SITES = {
    "bash_compress.py": 1,   # the Bash-tool wrapper's subprocess.run
    "runtime_env.py": 1,     # the shared `ps` process snapshot (one spawn serves
                             # both ancestor scans)
    "utf8_io.py": 1,         # the UTF-8 re-exec Popen
    "spawn_utils.py": 2,     # primary Popen + the ACCESS_DENIED retry Popen
}


def _subprocess_aliases(tree):
    """Names bound to the ``subprocess`` module in this file.

    Covers ``import subprocess`` and ``import subprocess as _sp`` at module or
    function scope (utf8_io imports it lazily inside the function).
    """
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    aliases.add(alias.asname or "subprocess")
    return aliases


def _spawn_calls(tree, aliases):
    """Yield (call_node, dotted_name) for every subprocess spawn call."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            continue
        if func.value.id in aliases and func.attr in _SPAWN_ATTRS:
            yield node, f"{func.value.id}.{func.attr}"


def _flag_source(call, src):
    """Return the source text of the call's ``creationflags`` value, or None.

    A ``**mapping`` spread is returned as ``"**" + <mapping source>`` so the
    caller can follow the spread to where the flag was actually set.
    """
    for kw in call.keywords:
        if kw.arg == "creationflags":
            return ast.get_source_segment(src, kw.value) or ""
        if kw.arg is None:
            return "**" + (ast.get_source_segment(src, kw.value) or "")
    return None


def _name_assignments(tree, src, name):
    """Source text of every ``name = <expr>`` value in the tree."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    out.append(ast.get_source_segment(src, node.value) or "")
    return out


def _spread_sets_no_window(tree, src, var):
    """True when ``var["creationflags"]`` is assigned a CREATE_NO_WINDOW value.

    Follows one level of indirection (``d["creationflags"] = _flags`` where
    ``_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)``), which is the shape
    utf8_io.py uses so it can omit the key entirely on builds without the flag.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if not (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                    and t.value.id == var):
                continue
            key = ast.get_source_segment(src, t.slice) or ""
            if "creationflags" not in key:
                continue
            val = ast.get_source_segment(src, node.value) or ""
            if "CREATE_NO_WINDOW" in val:
                return True
            if isinstance(node.value, ast.Name):
                if any("CREATE_NO_WINDOW" in a
                       for a in _name_assignments(tree, src, node.value.id)):
                    return True
    return False


def _iter_surface():
    for name in SURFACE:
        path = SCRIPTS / name
        assert path.is_file(), f"surface file missing from the repo: {name}"
        src = path.read_text(encoding="utf-8")
        yield name, path, src, ast.parse(src)


# ---------------------------------------------------------------------------
# Source scan: the cross-platform contract
# ---------------------------------------------------------------------------
def test_every_spawn_site_carries_create_no_window():
    """Every subprocess spawn in the hook-script surface passes creationflags
    containing the guarded CREATE_NO_WINDOW constant."""
    offenders = []
    for name, _path, src, tree in _iter_surface():
        aliases = _subprocess_aliases(tree)
        for call, dotted in _spawn_calls(tree, aliases):
            flags = _flag_source(call, src)
            if flags is None:
                offenders.append(
                    f"{name}:{call.lineno} {dotted}(...) has no creationflags "
                    "-- a console exe spawned here flashes a cmd window on Windows"
                )
                continue
            if flags.startswith("**"):
                var = flags[2:].strip()
                # spawn_utils spreads the kwargs it just merged with
                # detach_spawn_kwargs() -- covered by its own tests below.
                if name == "spawn_utils.py" and "detach_spawn_kwargs" in src:
                    continue
                if not _spread_sets_no_window(tree, src, var):
                    offenders.append(
                        f"{name}:{call.lineno} {dotted}(**{var}) -- nothing sets "
                        f"{var}['creationflags'] to a CREATE_NO_WINDOW value"
                    )
                continue
            if "CREATE_NO_WINDOW" not in flags and "_NO_WINDOW" not in flags:
                offenders.append(
                    f"{name}:{call.lineno} {dotted}(...) creationflags={flags!r} "
                    "does not include CREATE_NO_WINDOW"
                )
    assert not offenders, "unguarded spawn sites (#107):\n  " + "\n  ".join(offenders)


def test_no_banned_shell_spawns():
    """os.system / os.popen / os.startfile cannot carry creation flags, so they
    always flash a console on Windows. None may appear in the surface."""
    offenders = []
    for name, _path, src, tree in _iter_surface():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                    and func.attr in _BANNED_OS_ATTRS):
                offenders.append(f"{name}:{node.lineno} os.{func.attr}(...)")
    assert not offenders, (
        "os.system/os.popen/os.startfile cannot suppress the Windows console "
        "window; use subprocess with creationflags instead:\n  " + "\n  ".join(offenders)
    )


def test_no_window_constant_is_getattr_guarded():
    """The ``_NO_WINDOW`` module constants must be getattr-guarded so they are 0
    on POSIX (where subprocess has no such attribute) instead of raising at
    import time and taking the hook down with them."""
    for name, _path, src, tree in _iter_surface():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "_NO_WINDOW" not in targets:
                continue
            seg = ast.get_source_segment(src, node.value) or ""
            assert seg.startswith("getattr("), (
                f"{name}:{node.lineno} _NO_WINDOW must be getattr-guarded, got {seg!r}"
            )
            assert "CREATE_NO_WINDOW" in seg and seg.rstrip().endswith("0)"), (
                f"{name}:{node.lineno} _NO_WINDOW must default to 0, got {seg!r}"
            )


def test_scan_finds_the_known_spawn_sites():
    """Anti-vacuous-pass guard: the AST matcher must still SEE the censused
    spawn sites. If a refactor breaks the matcher, the contract tests above
    would go green on zero findings; this fails instead."""
    found = {}
    for name, _path, _src, tree in _iter_surface():
        aliases = _subprocess_aliases(tree)
        n = sum(1 for _ in _spawn_calls(tree, aliases))
        if n:
            found[name] = n
    for name, minimum in KNOWN_SITES.items():
        assert found.get(name, 0) >= minimum, (
            f"expected at least {minimum} spawn site(s) in {name}, matcher saw "
            f"{found.get(name, 0)} -- the AST matcher is broken"
        )


# ---------------------------------------------------------------------------
# spawn_utils: the shared helper every detached spawn inherits from
# ---------------------------------------------------------------------------
def test_detach_spawn_kwargs_nt_includes_create_no_window(monkeypatch):
    """On nt, detach_spawn_kwargs() must OR CREATE_NO_WINDOW in ON TOP OF the
    existing detach flags -- never instead of them."""
    import spawn_utils
    fake_os = types.ModuleType("os_nt")
    fake_os.__dict__.update(os.__dict__)
    fake_os.name = "nt"
    monkeypatch.setattr(spawn_utils, "os", fake_os)
    for _name, _val in (("DETACHED_PROCESS", 0x8),
                        ("CREATE_NEW_PROCESS_GROUP", 0x200),
                        ("CREATE_BREAKAWAY_FROM_JOB", 0x1000000),
                        ("CREATE_NO_WINDOW", _CREATE_NO_WINDOW)):
        monkeypatch.setattr(spawn_utils.subprocess, _name, _val, raising=False)
    flags = spawn_utils.detach_spawn_kwargs()["creationflags"]
    assert flags & _CREATE_NO_WINDOW, "CREATE_NO_WINDOW must be present (#107)"
    assert flags & 0x8, "DETACHED_PROCESS must be preserved"
    assert flags & 0x200, "CREATE_NEW_PROCESS_GROUP must be preserved"
    assert flags & 0x1000000, "CREATE_BREAKAWAY_FROM_JOB must be preserved"


def test_detach_spawn_kwargs_posix_unchanged(monkeypatch):
    """POSIX still gets start_new_session and no creationflags at all."""
    import spawn_utils
    fake_os = types.ModuleType("os_posix")
    fake_os.__dict__.update(os.__dict__)
    fake_os.name = "posix"
    monkeypatch.setattr(spawn_utils, "os", fake_os)
    assert spawn_utils.detach_spawn_kwargs() == {"start_new_session": True}


# ---------------------------------------------------------------------------
# bash_compress: the hot one -- wraps every Bash-tool command the model runs
# ---------------------------------------------------------------------------
def test_bash_compress_no_window_constant_matches_subprocess():
    import bash_compress
    assert bash_compress._NO_WINDOW == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if os.name != "nt":
        assert bash_compress._NO_WINDOW == 0, "must be a no-op on POSIX"


def test_bash_compress_main_passes_creationflags(monkeypatch, tmp_path):
    """Functional proof: main() forwards the no-window flag to subprocess.run
    for the user's command (the site that would flash on every Bash call)."""
    import bash_compress
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setattr(bash_compress, "_NO_WINDOW", _CREATE_NO_WINDOW)
    cap = {}

    class _R:
        stdout = "hi\n"
        stderr = ""
        returncode = 0

    def fake_run(argv, **kwargs):
        cap.update(kwargs)
        cap["argv"] = argv
        return _R()

    monkeypatch.setattr(bash_compress.subprocess, "run", fake_run)
    monkeypatch.setattr(bash_compress.sys, "argv", ["bash_compress.py", "git", "status"])
    with pytest.raises(SystemExit):
        bash_compress.main()
    assert cap.get("argv") == ["git", "status"], "the user's command must still run"
    assert cap.get("creationflags") == _CREATE_NO_WINDOW, (
        "the Bash-tool wrapper must spawn with CREATE_NO_WINDOW (#107)"
    )


# ---------------------------------------------------------------------------
# Real Windows runtime checks (skipped everywhere else)
# ---------------------------------------------------------------------------
_CONSOLE_PROBE = (
    "import ctypes, sys;\n"
    "sys.exit(0 if ctypes.windll.kernel32.GetConsoleWindow() == 0 else 1)\n"
)


@pytest.mark.skipif(os.name != "nt", reason="console allocation is Windows-only")
def test_create_no_window_child_has_no_console():
    """A child spawned with CREATE_NO_WINDOW must have no console window.

    Exit 0 means GetConsoleWindow() returned 0, i.e. Windows allocated no
    console -- the thing the user was seeing flash.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _CONSOLE_PROBE],
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, (
        "child reported a console window despite CREATE_NO_WINDOW"
    )


@pytest.mark.skipif(os.name != "nt", reason="console allocation is Windows-only")
def test_bash_compress_wrapper_child_has_no_console(tmp_path):
    """End-to-end on Windows: the real bash_compress wrapper must forward the
    exit code and output of a whitelisted console child.

    The direct GetConsoleWindow() probe can no longer ride through the wrapper:
    R13a makes bash_compress re-validate its own argv, and ``python -c`` is a
    non-whitelisted interpreter, so the wrapper correctly refuses it. The
    no-console guarantee for the wrapper's own child is proven elsewhere:
    ``test_create_no_window_child_has_no_console`` proves CREATE_NO_WINDOW
    suppresses a console, and the source scan proves bash_compress passes
    ``creationflags=_NO_WINDOW`` to that child. This test keeps proving the
    wrapper's end-to-end execution contract (spawn, forward output, forward the
    exit code) using a whitelisted console binary the wrapper will accept.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", "to@example.com"], cwd=str(repo), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Token Optimizer"], cwd=str(repo), check=True
    )
    (repo / "file.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True)

    env = dict(os.environ)
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "bash_compress.py"), "git", "status", "--short"],
        capture_output=True,
        timeout=60,
        env=env,
        cwd=str(repo),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, (
        "bash_compress child failed (rc=%s, stderr=%r)"
        % (proc.returncode, proc.stderr)
    )


# The probe reports through a file, not its exit code: git treats any
# non-zero exit from GIT_EXTERNAL_DIFF as fatal and returns 128 itself,
# which would make a "console attached" verdict indistinguishable from a
# broken probe.
_CONSOLE_PROBE_PY = r"""
import ctypes, os, sys
kernel32 = ctypes.windll.kernel32
verdict = "0" if kernel32.GetConsoleWindow() == 0 else "1"
with open(os.environ["TO_CONSOLE_PROBE_OUT"], "w", encoding="utf-8") as fh:
    fh.write(verdict)
sys.exit(0)
"""


@pytest.mark.skipif(os.name != "nt", reason="console allocation is Windows-only")
def test_bash_compress_wrapper_child_has_no_live_console(tmp_path):
    """Live console verdict through the full wrapper chain.

    A whitelisted `git diff` runs inside the wrapper; GIT_EXTERNAL_DIFF points
    at a probe whose exit code reports GetConsoleWindow() for the process tree
    the wrapper spawned (0 = no console, 1 = console attached). This proves
    the no-console guarantee end-to-end: if the wrapper ever dropped
    CREATE_NO_WINDOW, git (and the probe under it) would gain a console and
    this test would fail with a live verdict, not a source-composition claim.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", "to@example.com"], cwd=str(repo), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Token Optimizer"], cwd=str(repo), check=True
    )
    (repo / "file.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True)
    (repo / "file.txt").write_text("hello world\n", encoding="utf-8")

    probe_py = tmp_path / "console_probe.py"
    probe_py.write_text(_CONSOLE_PROBE_PY, encoding="utf-8")
    # Git for Windows runs GIT_EXTERNAL_DIFF through sh.exe, so the probe
    # launcher is a POSIX shell script with forward-slash paths, not a .cmd.
    probe_cmd = tmp_path / "console_probe.sh"
    probe_cmd.write_text(
        '#!/bin/sh\nexec "{}" "{}"\n'.format(
            sys.executable.replace("\\", "/"), str(probe_py).replace("\\", "/")
        ),
        encoding="utf-8",
        newline="\n",
    )

    verdict_file = tmp_path / "console_verdict.txt"
    env = dict(os.environ)
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(tmp_path)
    env["GIT_EXTERNAL_DIFF"] = str(probe_cmd).replace("\\", "/")
    env["TO_CONSOLE_PROBE_OUT"] = str(verdict_file)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "bash_compress.py"), "git", "diff"],
        capture_output=True,
        timeout=60,
        env=env,
        cwd=str(repo),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, (
        "wrapper's git-diff child failed (rc=%s, stderr=%r)" % (proc.returncode, proc.stderr)
    )
    assert verdict_file.is_file(), "the external-diff probe never ran under the wrapper"
    assert verdict_file.read_text(encoding="utf-8") == "0", (
        "wrapper's git-diff child reported a live console"
    )


@pytest.mark.skipif(os.name != "nt", reason="console allocation is Windows-only")
def test_console_probe_detects_a_real_console(tmp_path):
    """Negative control for the live probe: the same probe MUST report a
    console when one is allocated (CREATE_NEW_CONSOLE), proving a 0 exit in
    the wrapper test means 'no console' and not 'probe cannot tell'."""
    probe_py = tmp_path / "console_probe.py"
    probe_py.write_text(_CONSOLE_PROBE_PY, encoding="utf-8")
    verdict_file = tmp_path / "console_verdict.txt"
    env = dict(os.environ, TO_CONSOLE_PROBE_OUT=str(verdict_file))
    proc = subprocess.run(
        [sys.executable, str(probe_py)],
        capture_output=True,
        timeout=30,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    )
    assert proc.returncode == 0 and verdict_file.read_text(encoding="utf-8") == "1", (
        "probe did not report a console under CREATE_NEW_CONSOLE; it cannot detect "
        "consoles and the wrapper no-console verdict is meaningless"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows flag constants only exist on nt")
def test_real_detach_spawn_kwargs_has_no_window():
    """On a real Windows runner, detach_spawn_kwargs() carries CREATE_NO_WINDOW."""
    import spawn_utils
    flags = spawn_utils.detach_spawn_kwargs().get("creationflags", 0)
    assert flags & subprocess.CREATE_NO_WINDOW
    assert flags & subprocess.DETACHED_PROCESS
