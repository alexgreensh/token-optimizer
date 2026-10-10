"""The no-flash fix: hooks/, repo-root scripts/ and fleet-auditor must never flash
a console window on Windows.

Companion to ``tests/test_windows_spawn_no_window.py`` (measure.py, hooks/run.py,
utf8_io.py, spawn_utils) and ``tests/test_bridges_no_window.py`` (the runtime
bridges). This file owns the surface neither of those covers:

* ``skills/fleet-auditor/scripts/fleet.py`` -- the dashboard opener, the one
  genuinely user-facing flash in this lane;
* ``scripts/keepwarm-experiment.py`` -- maintainer-only, guarded for uniformity;
* ``hooks/module_runner.py`` -- must stay spawn-free (it runs the target
  IN-PROCESS via ``runpy``, which is why hooks/run.py's Windows reap can be a
  plain ``proc.kill()``);
* ``hooks/hooks.json`` -- the hook command strings must not invoke a console exe
  (no ``cmd.exe``, no ``.cmd``/``.bat`` shim, no ``where``/``which``);
* ``hooks/python-launcher.sh`` -- the pythonw.exe swap must stay at BOTH exec
  sites;
* ``hooks/run.py`` -- an ANTI-REFACTOR guard, see below.

The run.py guard is the subtle one. The sweep convention everywhere else is a
module-level ``_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)``. That
convention is actively WRONG for hooks/run.py: the constant would bind at import
time, and off-Windows the attribute does not exist, so it would freeze at ``0``.
``test_windows_spawn_no_window.py`` simulates Windows by monkeypatching
``mod.subprocess.DETACHED_PROCESS`` AFTER import and then asserts the real
``0x8`` reaches Popen -- a module-level constant would still hold the
stale ``0`` and silently delete that coverage on every non-Windows CI leg. So
run.py must keep resolving the flag at CALL time, and this file fails if someone
"tidies" it into a module constant.

Source scans are AST-based and cross-platform, so a regression is caught on the
macOS/Linux legs rather than only on windows-latest. Genuinely OS-level
assertions are ``nt``-gated.

Run: python3 -m pytest tests/test_hooks_root_no_window.py -v
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"
ROOT_SCRIPTS = REPO / "scripts"
FLEET_SCRIPTS = REPO / "skills" / "fleet-auditor" / "scripts"

_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x8
_CREATE_NEW_PROCESS_GROUP = 0x200

# Files in this lane that create processes and therefore need the guard.
SPAWNING_FILES = {
    "fleet.py": FLEET_SCRIPTS / "fleet.py",
    "keepwarm-experiment.py": ROOT_SCRIPTS / "keepwarm-experiment.py",
}

# Files in this lane that were CLEAN in the no-flash census and must stay clean.
# If one of these grows a spawn, it needs a guard and a move into SPAWNING_FILES.
CLEAN_FILES = {
    "module_runner.py": HOOKS / "module_runner.py",
    "shared.py": FLEET_SCRIPTS / "shared.py",
    "check_docs_claims.py": ROOT_SCRIPTS / "check_docs_claims.py",
    "check_readme_integrity.py": ROOT_SCRIPTS / "check_readme_integrity.py",
    "check-theme-contrast.py": ROOT_SCRIPTS / "check-theme-contrast.py",
}

# Every process-creating API. Anything here without a guard is a flash.
_SPAWN_ATTRS = {"run", "Popen", "call", "check_output", "check_call"}
_OS_SPAWN_ATTRS = {"system", "popen", "startfile"}


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _spawn_calls(tree: ast.Module) -> list[ast.Call]:
    """Every ``subprocess.<spawn>()`` call node in the tree.

    AST-based on purpose: a docstring or comment that merely mentions
    ``subprocess.run`` can neither satisfy nor trip these assertions.
    """
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if (
            isinstance(fn, ast.Attribute)
            and fn.attr in _SPAWN_ATTRS
            and isinstance(fn.value, ast.Name)
            and fn.value.id in ("subprocess", "_subprocess")
        ):
            out.append(node)
    return out


def _kwarg(call: ast.Call, name: str):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


# ---------------------------------------------------------------------------
# 1. Every spawning file declares the guard, in the canonical form.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(SPAWNING_FILES))
def test_spawning_file_defines_no_window_constant(name):
    """``_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)`` at module level.

    The three-argument getattr is the load-bearing part: the attribute only
    exists on Windows builds, so without the ``0`` default this raises
    AttributeError at import on every POSIX host.
    """
    tree = _parse(SPAWNING_FILES[name])
    found = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_NO_WINDOW" for t in node.targets
        ):
            found = node.value
    assert found is not None, f"{name} must define a module-level _NO_WINDOW"
    assert isinstance(found, ast.Call) and isinstance(found.func, ast.Name), (
        f"{name}: _NO_WINDOW must be a getattr(...) call"
    )
    assert found.func.id == "getattr", f"{name}: _NO_WINDOW must use getattr"
    assert len(found.args) == 3, (
        f"{name}: getattr needs a default so POSIX degrades to 0 instead of raising"
    )
    assert found.args[1].value == "CREATE_NO_WINDOW"
    assert found.args[2].value == 0


# ---------------------------------------------------------------------------
# 2. Every spawn site in those files passes creationflags.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(SPAWNING_FILES))
def test_every_spawn_site_passes_creationflags(name):
    tree = _parse(SPAWNING_FILES[name])
    calls = _spawn_calls(tree)
    assert calls, (
        f"{name} was in the no-flash spawn census but the scanner now finds ZERO "
        "spawn sites -- the scanner has stopped matching real code. Fix the "
        "scanner; a silently empty guard is worse than no guard."
    )
    offenders = [
        f"{name}:{c.lineno}" for c in calls if _kwarg(c, "creationflags") is None
    ]
    assert not offenders, (
        "spawn sites missing creationflags=_NO_WINDOW (the no-flash concern: each flashes a "
        f"console window on a Windows Desktop host): {offenders}"
    )


@pytest.mark.parametrize("name", sorted(SPAWNING_FILES))
def test_creationflags_reference_the_guard(name):
    """The flag passed must be ``_NO_WINDOW`` (bare, or OR-ed with others).

    ``creationflags=0`` would satisfy the test above while fixing nothing.
    """
    tree = _parse(SPAWNING_FILES[name])
    for call in _spawn_calls(tree):
        node = _kwarg(call, "creationflags")
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        assert "_NO_WINDOW" in names, (
            f"{name}:{call.lineno} passes creationflags without _NO_WINDOW"
        )


# ---------------------------------------------------------------------------
# 3. No shell=True / os.system / os.popen anywhere in the lane.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(SPAWNING_FILES | CLEAN_FILES))
def test_no_shell_true(name):
    """``shell=True`` spawns cmd.exe on Windows -- always a console host.

    fleet.py used to do exactly this (``["start", path], shell=True``). Same
    prohibition ``test_bridges_no_window.py`` enforces on the bridges.
    """
    path = (SPAWNING_FILES | CLEAN_FILES)[name]
    tree = _parse(path)
    offenders = [
        f"{name}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "shell"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is True
    ]
    assert not offenders, f"shell=True found at {offenders} (cmd.exe console host)"


# ``os.startfile`` exemptions, keyed by (file, ENCLOSING FUNCTION) -- never by
# line number, which shifts on every edit above it. The general ban stands
# because os.startfile does not exist off Windows, so a cross-platform helper
# built on it looks fine on CI and is dead on macOS/Linux. It is allowed where a
# ``platform.system()`` branch reaches it ONLY on Windows and the alternative is
# routing a filesystem path through cmd.exe's parser -- which is command
# injection, not merely a flash. See
# ``test_fleet_opener_windows_branch_uses_os_startfile_not_cmd``. This mirrors
# ``tests/test_measure_no_window.py``, where os.startfile is out of scope for
# the same reason: ShellExecuteW allocates no console and takes no command line.
_OS_STARTFILE_EXEMPT = {("fleet.py", "_open_in_browser")}


def _exempt_startfile_calls(name: str, tree: ast.Module) -> set[int]:
    """ids of the ``os.startfile`` calls covered by ``_OS_STARTFILE_EXEMPT``."""
    allowed: set[int] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        if (name, fn.name) not in _OS_STARTFILE_EXEMPT:
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "startfile"
            ):
                allowed.add(id(node))
    return allowed


@pytest.mark.parametrize("name", sorted(SPAWNING_FILES | CLEAN_FILES))
def test_no_os_system_popen_startfile(name):
    """``os.system``/``os.popen`` go through cmd.exe on Windows and take no
    creationflags at all, so they can never be made windowless. ``os.startfile``
    is windowless but is banned here for a different reason: it silently does
    nothing on POSIX, so a cross-platform helper using it would look fine on CI
    and be dead on macOS/Linux -- except at the reviewed sites in
    ``_OS_STARTFILE_EXEMPT``."""
    path = (SPAWNING_FILES | CLEAN_FILES)[name]
    tree = _parse(path)
    exempt = _exempt_startfile_calls(name, tree)
    offenders = [
        f"{name}:{node.lineno} -- os.{node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _OS_SPAWN_ATTRS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
        and id(node) not in exempt
    ]
    assert not offenders, f"unguardable spawn API: {offenders}"


# ---------------------------------------------------------------------------
# 4. fleet.py opener: the exact site that was broken (pass-on-revert guard).
# ---------------------------------------------------------------------------
def _open_in_browser_node() -> ast.FunctionDef:
    tree = _parse(SPAWNING_FILES["fleet.py"])
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_open_in_browser":
            return node
    pytest.fail("fleet.py no longer defines _open_in_browser")


def test_fleet_opener_windows_branch_uses_os_startfile_not_cmd():
    """The no-flash fix site: the Windows branch must be ``os.startfile``, NOT a cmd hop.

    This assertion used to pin ``["cmd", "/c", "start", "", path]`` -- the first
    no-flash fix. Do not re-pin that shape: it is COMMAND INJECTION. Dropping
    ``shell=True`` removed Python's shell, not cmd.exe's parser, and
    ``subprocess.list2cmdline`` quotes an argument only on space/tab/quote --
    never on ``&``, ``^``, ``|``, ``(``, ``)``. ``&`` is a legal Windows
    account-name character, so a genuine path like
    ``C:\\Users\\R&D\\...\\fleet-dashboard.html`` reached cmd UNQUOTED, cmd split
    at the ``&``, and executed the tail as a second command relative to the CWD.
    ``%VAR%`` expands even inside quotes, and the bare image name ``cmd``
    resolves CWD-FIRST under CreateProcessW. The path is built from
    CLAUDE_CONFIG_DIR (deliberately unconfined) and HOME/USERPROFILE.

    ``os.startfile`` is ShellExecuteW: the path is a real argument, no command
    line is parsed, and no console is allocated -- so it satisfies the no-flash concern with no
    creationflags at all. It is the same helper ``measure.py::_open_in_browser``
    has always used.
    """
    fn = _open_in_browser_node()
    body = ast.Module(body=fn.body, type_ignores=[])

    startfile = [
        node
        for node in ast.walk(body)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "startfile"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
    ]
    assert startfile, (
        "fleet.py's Windows branch must open the dashboard with os.startfile "
        "(ShellExecuteW), not by spawning a command interpreter"
    )
    assert len(startfile[0].args) == 1, (
        "os.startfile must receive the path as its single argument -- never a "
        "string concatenated with anything else"
    )

    # And no cmd/cmd.exe/start argv may come back.
    for call in _spawn_calls(body):
        literals = [
            e.value
            for a in call.args
            if isinstance(a, ast.List)
            for e in a.elts
            if isinstance(e, ast.Constant)
        ]
        lowered = {str(v).lower() for v in literals}
        assert not lowered & {"cmd", "cmd.exe", "start"}, (
            "the opener must not route the dashboard path through cmd's parser "
            f"-- see this test's docstring for why; got {literals}"
        )
        assert _kwarg(call, "shell") is None, "the opener must not pass shell="


def test_fleet_opener_posix_branches_also_guarded():
    """``open``/``xdg-open`` carry the flag too. It is 0 off Windows, so this
    costs nothing and stops the file growing a two-tier convention.

    Two subprocess branches, not three: the Windows branch is ``os.startfile``,
    which spawns no process of ours at all (see the test above)."""
    fn = _open_in_browser_node()
    calls = _spawn_calls(ast.Module(body=fn.body, type_ignores=[]))
    assert len(calls) == 2, f"expected 2 subprocess branches, found {len(calls)}"
    for call in calls:
        assert _kwarg(call, "creationflags") is not None, (
            f"_open_in_browser branch at line {call.lineno} is unguarded"
        )


# ---------------------------------------------------------------------------
# 5. hooks/run.py -- ANTI-REFACTOR guard (see module docstring).
# ---------------------------------------------------------------------------
def test_run_py_resolves_detached_process_at_call_time():
    """run.py must NOT be converted to a module-level flag constant.

    A module-level constant would bind at import, freeze at 0 off Windows,
    and silently void
    ``test_windows_spawn_no_window.py::test_run_py_spawn_nt_uses_detached_process``
    (which monkeypatches ``mod.subprocess.DETACHED_PROCESS`` after import).
    run.py must keep the getattr INSIDE a function body.
    """
    tree = _parse(HOOKS / "run.py")
    for flag_const in ("_NO_WINDOW", "_DETACHED"):
        module_level = [
            n for n in tree.body
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == flag_const for t in n.targets)
        ]
        assert not module_level, (
            f"hooks/run.py must NOT define a module-level {flag_const}: it binds "
            "at import time and freezes at 0 off Windows, voiding the existing "
            "monkeypatch-based nt coverage. Keep the call-time getattr."
        )
    getattrs = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "getattr"
        and len(n.args) == 3
        and isinstance(n.args[1], ast.Constant)
        and n.args[1].value == "DETACHED_PROCESS"
    ]
    assert getattrs, "hooks/run.py must resolve DETACHED_PROCESS via getattr"
    # And that getattr must live inside a function, not at module scope.
    in_function = any(
        any(g is inner for inner in ast.walk(fn))
        for fn in tree.body if isinstance(fn, ast.FunctionDef)
        for g in getattrs
    )
    assert in_function, (
        "the DETACHED_PROCESS getattr must sit inside a function body so it is "
        "evaluated at call time"
    )


def test_run_py_child_stays_stdio_inheriting():
    """run.py's child MUST inherit stdio (hooks inject via stdout). On nt it
    spawns DETACHED_PROCESS -- no console is allocated at all -- and stdio is
    carried by the explicit handles from ``_windows_stdio_kwargs()``
    (STARTF_USESTDHANDLES). Never CREATE_NEW_PROCESS_GROUP (inert for reaping
    here, and it disables the child's Ctrl+C self-terminate) and never
    CREATE_NO_WINDOW (it still allocates a hidden console -- the conhost
    spawn that leaks a kernel token, issue #215)."""
    src = (HOOKS / "run.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    for banned in ("CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        assert banned not in code, (
            f"hooks/run.py must not use {banned}: see this test's docstring"
        )
    assert "DETACHED_PROCESS" in code, (
        "hooks/run.py must spawn the module_runner child DETACHED_PROCESS "
        "(no console allocated -> no conhost spawn -> no kernel-token leak)"
    )
    assert "_windows_stdio_kwargs" in code, (
        "DETACHED_PROCESS only preserves stdio because run.py passes the "
        "three std handles explicitly; _windows_stdio_kwargs must stay"
    )


# ---------------------------------------------------------------------------
# 6. CLEAN files must stay spawn-free.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(CLEAN_FILES))
def test_clean_file_has_no_spawns(name):
    path = CLEAN_FILES[name]
    if not path.is_file():
        pytest.skip(f"{name} not present in this checkout")
    calls = _spawn_calls(_parse(path))
    assert not calls, (
        f"{name} was CLEAN in the no-flash census and has grown a spawn at lines "
        f"{[c.lineno for c in calls]}. Add creationflags=_NO_WINDOW and move it "
        "into SPAWNING_FILES in this file so it is scanned."
    )


def test_module_runner_runs_target_in_process():
    """module_runner.py's spawn-free-ness is load-bearing, not incidental: it
    runs the hook target IN-PROCESS via runpy, which is exactly why the Windows
    reap in run.py can be a plain proc.kill() (the child IS the trends.db lock
    holder) instead of a PPID-tree-walking taskkill."""
    src = (HOOKS / "module_runner.py").read_text(encoding="utf-8")
    assert "runpy.run_module" in src, (
        "module_runner.py must keep running the target in-process via runpy; "
        "spawning it would add a console-flash candidate on the hook hot path "
        "AND break run.py's proc.kill() reap assumption"
    )


# ---------------------------------------------------------------------------
# 7. hooks.json -- the hook command strings must not invoke a console exe.
# ---------------------------------------------------------------------------
def _hook_commands() -> list[str]:
    data = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
    out = []
    for entries in data["hooks"].values():
        for entry in entries:
            for hook in entry.get("hooks", []):
                if hook.get("type") == "command":
                    out.append(hook["command"])
    return out


def test_hooks_json_has_commands():
    assert _hook_commands(), "hooks.json parsed to zero command entries"


def test_hooks_json_never_invokes_cmd_or_a_shim():
    """No ``cmd.exe``, no ``powershell``, no ``.cmd``/``.bat`` shim.

    Any of those is a console-subsystem host on the per-tool-call hot path.
    """
    offenders = []
    for cmd in _hook_commands():
        low = cmd.lower()
        for needle in ("cmd.exe", "cmd /c", "powershell", ".cmd", ".bat"):
            if needle in low:
                offenders.append(f"{needle!r} in {cmd[:70]}...")
    assert not offenders, f"hooks.json invokes a console host: {offenders}"


def test_hooks_json_never_uses_where_or_which():
    """``command -v`` is a shell BUILTIN and forks nothing. ``where`` and
    ``which`` are real console executables, so each would be an extra flash
    candidate on every hook fire. Keep the builtin."""
    offenders = []
    for cmd in _hook_commands():
        for tok in cmd.replace(";", " ").replace("&&", " ").split():
            if tok in ("where", "where.exe", "which", "which.exe"):
                offenders.append(cmd[:70])
    assert not offenders, (
        f"hooks.json uses where/which instead of the `command -v` builtin: {offenders}"
    )
    for cmd in _hook_commands():
        assert "command -v" in cmd, (
            "each hook command must probe for bash with the `command -v` builtin"
        )


def test_hooks_json_routes_every_command_through_the_launcher():
    """No entry may name a Python interpreter directly. The launcher is the only
    thing that performs the Windows pythonw.exe swap; a bare ``python``/``py``
    would bypass it and flash."""
    for cmd in _hook_commands():
        assert "hooks/python-launcher.sh" in cmd, (
            f"hook command bypasses python-launcher.sh: {cmd[:90]}"
        )
        head = cmd.split("python-launcher.sh", 1)[0]
        for tok in head.replace('"', " ").split():
            assert not tok.endswith(("python", "python.exe", "python3", "py", "py.exe")), (
                f"hook command invokes an interpreter directly before the "
                f"launcher: {tok!r} in {cmd[:90]}"
            )


def test_hooks_json_execs_bash_so_no_extra_process_layer():
    """The wrapper must ``exec`` bash, not call it.

    ``exec`` REPLACES the host shell process image, so the chain host-shell ->
    launcher -> interpreter costs no extra process. Dropping ``exec`` would add a
    live parent per hook fire, i.e. another console candidate on the hot path.
    """
    for cmd in _hook_commands():
        assert "exec " in cmd, f"hook command does not exec bash: {cmd[:90]}"


# ---------------------------------------------------------------------------
# 8. python-launcher.sh -- the pythonw swap must stay at BOTH exec sites.
# ---------------------------------------------------------------------------
def test_launcher_swaps_to_pythonw_at_both_exec_sites():
    """The swap is exec-time on purpose: the cache record still names
    python.exe, so a stale record must not freeze a user on the flashing binary.
    That only holds if BOTH exec paths call the swap."""
    src = (HOOKS / "python-launcher.sh").read_text(encoding="utf-8")
    assert src.count("_maybe_swap_to_pythonw") >= 3, (
        "expected the pythonw swap to be defined once and called from BOTH "
        "_exec_cached_interpreter and _exec_discovered_interpreter"
    )
    for fn in ("_exec_cached_interpreter", "_exec_discovered_interpreter"):
        start = src.index(fn + "()")
        body = src[start:start + 2000]
        assert "_maybe_swap_to_pythonw" in body, f"{fn} lost the pythonw swap"


def test_launcher_rejects_cached_pythonw_record():
    """A cache record naming a GUI twin (pythonw.exe, or pyw.exe since the no-flash
    py-launcher swap) is poisoned or a stale artefact of a twin that should have
    been probed out. Rejecting it keeps discovery honest."""
    src = (HOOKS / "python-launcher.sh").read_text(encoding="utf-8")
    assert "*/pythonw.exe|*/pyw.exe) return 1 ;;" in src, (
        "the cached-interpreter reader must reject */pythonw.exe and */pyw.exe records"
    )


# ---------------------------------------------------------------------------
# 9. nt-only runtime checks.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "nt", reason="Windows-only constant check")
def test_no_window_constant_is_real_on_windows():
    """On a real Windows host the guard must resolve to the actual flag, not 0.

    If this is 0 on Windows, every ``creationflags=_NO_WINDOW`` in the lane is a
    no-op and the whole fix is cosmetic.
    """
    sys.path.insert(0, str(FLEET_SCRIPTS))
    sys.path.insert(0, str(REPO / "skills" / "token-optimizer" / "scripts"))
    import fleet
    assert fleet._NO_WINDOW == _CREATE_NO_WINDOW, (
        f"_NO_WINDOW is {fleet._NO_WINDOW!r} on Windows, expected "
        f"{_CREATE_NO_WINDOW:#x} -- the guard is inert"
    )


@pytest.mark.skipif(os.name != "nt", reason="real-spawn check is Windows-only")
def test_create_no_window_child_has_no_console():
    """OS-level proof, not kwargs-level: a child spawned with CREATE_NO_WINDOW
    reports ``GetConsoleWindow() == 0``. Without the flag, a console-subsystem
    child of a console-less parent gets a fresh console -- the console flash the no-flash fix targets."""
    child = (
        "import ctypes, sys;\n"
        "sys.exit(0 if ctypes.windll.kernel32.GetConsoleWindow() == 0 else 1)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", child],
        creationflags=_CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    assert proc.returncode == 0, (
        "child spawned with CREATE_NO_WINDOW still reported a console window"
    )
