"""#107: Codex / Hermes / Copilot bridge scripts must never flash a console.

Companion to ``tests/test_windows_spawn_no_window.py``, which covers measure.py,
hooks/run.py, utf8_io.py and the ``spawn_utils`` helper itself. This file covers
the 19 runtime-bridge scripts (codex_*, hermes_*, copilot_*).

Two spawn shapes exist in that surface and they need different guards:

* **Fire-and-forget** spawns go through ``spawn_utils.spawn_detached()``, whose
  ``DETACHED_PROCESS`` already means the child neither inherits nor creates a
  console. Those sites are already flash-free and are asserted to still route
  through the helper (converting one back to a raw ``Popen`` is caught by the
  scan below).
* **Blocking** spawns (``subprocess.run`` for ``copilot --version``, the hermes
  measure.py shim, the hermes-doctor bridge smoke test) must carry
  ``CREATE_NO_WINDOW`` explicitly. Without it, a console-subsystem child
  allocates a console window: ``python.exe`` directly, and ``copilot`` worse
  still because it is an npm-installed **``copilot.cmd``** on Windows, so
  ``CreateProcess`` routes it through ``cmd.exe`` -- itself a console binary.
  Same hazard for any future ``npm`` / ``npx`` / ``code`` spawn: all resolve to
  ``.cmd`` shims hosted by cmd.exe.

The guard is always the module-level ``_NO_WINDOW = getattr(subprocess,
"CREATE_NO_WINDOW", 0)``, which degrades to ``0`` on POSIX where
``creationflags=0`` is an accepted no-op (CPython only rejects a *non-zero*
``creationflags`` off-Windows).

The source scan is cross-platform (AST, no import needed) so a regression is
caught on the macOS/Linux CI legs, not only on windows-latest. Runtime checks
force ``_NO_WINDOW`` to the real Windows constant so the wiring is proven on
every platform; the genuinely OS-level assertions are ``nt``-gated.

Run: python3 -m pytest tests/test_107_bridges_no_window.py -v
"""
import ast
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

sys.path.insert(0, str(SCRIPTS))

_CREATE_NO_WINDOW = 0x08000000

# The full Lane C surface. Every one of these is scanned, including the ones
# with zero spawns, so "clean" is an assertion rather than an omission.
BRIDGE_FILES = (
    "codex_io.py",
    "codex_install.py",
    "codex_statusline.py",
    "codex_hook_bridge.py",
    "codex_compact_prompt.py",
    "codex_session.py",
    "codex_doctor.py",
    "codex_state.py",
    "hermes_session.py",
    "hermes_hook_bridge.py",
    "hermes_doctor.py",
    "hermes_state.py",
    "hermes_install.py",
    "copilot_install.py",
    "copilot_doctor.py",
    "copilot_session.py",
    "copilot_hook_bridge.py",
    "copilot_vscode.py",
    "copilot_state.py",
    "cursor_install.py",
    "cursor_doctor.py",
    "cursor_session.py",
    "cursor_hook_bridge.py",
    "cursor_state.py",
    "grok_install.py",
    "grok_doctor.py",
    "grok_session.py",
    "grok_hook_bridge.py",
    "grok_state.py",
)

# Files known to contain at least one process-creating call. Anything NOT in
# this set must stay spawn-free; anything in it must carry the _NO_WINDOW
# guard. Both directions are asserted, so adding a spawn to a "clean" file
# fails loudly instead of slipping through.
SPAWNING_FILES = frozenset({
    "hermes_hook_bridge.py",
    "hermes_doctor.py",
    "copilot_hook_bridge.py",
    "cursor_hook_bridge.py",
    "cursor_doctor.py",
    "grok_hook_bridge.py",
    "grok_doctor.py",
})

# Callables that actually create a process. `subprocess.list2cmdline` is
# deliberately absent: it only formats an argv into a string (codex_install.py
# uses it to build the hooks.json command text) and creates nothing.
_SUBPROCESS_SPAWNS = {"run", "Popen", "call", "check_output", "check_call"}
_OS_SPAWNS = {"system", "popen", "startfile", "execv", "execvp", "execve",
              "spawnl", "spawnv", "spawnve"}


def _parse(name):
    return ast.parse((SCRIPTS / name).read_text(encoding="utf-8"), filename=name)


def _module_aliases(tree):
    """Names bound to the `subprocess` / `os` modules in this file.

    Catches ``import subprocess as _sp`` (the copilot degraded fallback) as well
    as a plain ``import subprocess``, so an aliased import cannot dodge the scan.
    """
    subs, oses = {"subprocess"}, {"os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "subprocess":
                    subs.add(a.asname or a.name)
                elif a.name == "os":
                    oses.add(a.asname or a.name)
    return subs, oses


def _spawn_calls(tree):
    """Yield ``(call_node, label)`` for every process-creating call in *tree*."""
    subs, oses = _module_aliases(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute) or not isinstance(fn.value, ast.Name):
            continue
        mod, attr = fn.value.id, fn.attr
        if mod in subs and attr in _SUBPROCESS_SPAWNS:
            yield node, f"{mod}.{attr}"
        elif mod in oses and attr in _OS_SPAWNS:
            yield node, f"{mod}.{attr}"


def _enclosing_funcs(tree):
    """Map each AST node to the innermost FunctionDef containing it."""
    owner = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(fn):
                owner[child] = fn  # inner walk wins for nested defs
    return owner


def _mentions_no_window(node):
    return any(
        isinstance(n, ast.Name) and n.id == "_NO_WINDOW" for n in ast.walk(node)
    )


def _guarded(call, tree, owner):
    """True if *call* provably carries the _NO_WINDOW guard.

    Two accepted forms:

    1. An explicit ``creationflags=`` keyword whose value mentions ``_NO_WINDOW``
       (``creationflags=_NO_WINDOW`` or an OR with other flags).
    2. The call forwards ``**kwargs`` and its enclosing function assigns
       ``kwargs["creationflags"]`` from an expression mentioning ``_NO_WINDOW``.
       This is the copilot degraded-fallback shape, where the flag is merged
       into a caller-supplied dict so an existing DETACHED/breakaway flag is
       preserved rather than clobbered.
    """
    for kw in call.keywords:
        if kw.arg == "creationflags":
            return _mentions_no_window(kw.value)

    forwards = any(kw.arg is None for kw in call.keywords)
    if not forwards:
        return False
    fn = owner.get(call)
    if fn is None:
        return False
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (isinstance(tgt, ast.Subscript)
                        and isinstance(tgt.slice, ast.Constant)
                        and tgt.slice.value == "creationflags"
                        and _mentions_no_window(node.value)):
                    return True
    return False


# ---------------------------------------------------------------------------
# Source scan -- runs on every platform
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", BRIDGE_FILES)
def test_every_spawn_site_carries_no_window(name):
    """Every process-creating call in the bridge surface must be guarded."""
    tree = _parse(name)
    owner = _enclosing_funcs(tree)
    unguarded = [
        f"{name}:{call.lineno} {label}"
        for call, label in _spawn_calls(tree)
        if not _guarded(call, tree, owner)
    ]
    assert not unguarded, (
        "spawn site(s) missing the CREATE_NO_WINDOW guard (#107 console flash): "
        + ", ".join(unguarded)
    )


@pytest.mark.parametrize("name", BRIDGE_FILES)
def test_spawn_free_files_stay_spawn_free(name):
    """A file not in SPAWNING_FILES must not grow a spawn unnoticed.

    Pairs with the scan above: that one proves new spawns are guarded, this one
    proves the census in findings/107-sweep/C-bridges.md still describes reality.
    """
    found = [f"{call.lineno} {label}" for call, label in _spawn_calls(_parse(name))]
    if name in SPAWNING_FILES:
        assert found, (
            f"{name} is listed as a spawning file but has no spawn calls; "
            "update SPAWNING_FILES and the #107 census"
        )
    else:
        assert not found, (
            f"{name} was spawn-free at the #107 sweep but now spawns at {found}; "
            "add the _NO_WINDOW guard and update SPAWNING_FILES"
        )


@pytest.mark.parametrize("name", sorted(SPAWNING_FILES))
def test_spawning_files_define_guarded_module_constant(name):
    """_NO_WINDOW must be the guarded getattr form, not a bare attribute access.

    ``subprocess.CREATE_NO_WINDOW`` raises AttributeError on POSIX, which would
    turn a console-flash fix into an import-time crash on macOS/Linux.
    """
    tree = _parse(name)
    found = None
    for node in tree.body:  # module level only
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
        f"{name}: getattr must pass a default so POSIX degrades to 0, not raises"
    )
    assert found.args[1].value == "CREATE_NO_WINDOW"
    assert found.args[2].value == 0


@pytest.mark.parametrize("name", BRIDGE_FILES)
def test_no_shell_true(name):
    """shell=True spawns cmd.exe on Windows -- always a console host."""
    tree = _parse(name)
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


def test_detached_sites_still_route_through_spawn_utils():
    """The fire-and-forget spawns must keep using spawn_detached().

    They are flash-free via DETACHED_PROCESS, so they need no CREATE_NO_WINDOW.
    Open-coding one as a raw Popen would silently reintroduce the flash; this
    asserts the helper is still the path (the scan above catches the raw Popen).
    """
    # +1 each for the Gap-2 session-end dashboard regen (also via spawn_detached).
    # Cursor: one rollup + one dashboard spawn (stop shares both throttle paths).
    # Grok: one rollup + one dashboard spawn (same pattern as Cursor).
    expected = {"hermes_hook_bridge.py": 3, "copilot_hook_bridge.py": 2,
                "cursor_hook_bridge.py": 2, "grok_hook_bridge.py": 2}
    for name, count in expected.items():
        tree = _parse(name)
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "spawn_detached"
        ]
        assert len(calls) == count, (
            f"{name}: expected {count} spawn_detached call(s), found {len(calls)}"
        )


# ---------------------------------------------------------------------------
# Runtime wiring -- forces _NO_WINDOW to the real Windows value so the flag is
# proven to reach Popen on any platform (on POSIX the real constant is 0, which
# would make a broken wiring indistinguishable from a working one).
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Fresh-import tests below pop and re-import script modules. Without
    restoration, a re-imported instance (e.g. runtime_env) stays in
    sys.modules while later test FILES still hold the original bound at
    collection time -- test_runtime_env_wsl_mnt then patches the original
    while measure delegates to the unpatched replacement. Snapshot and
    restore the exact mapping so no duplicate module instance leaks out."""
    saved = sys.modules.copy()
    yield
    for k in list(sys.modules):
        if k not in saved:
            del sys.modules[k]
    sys.modules.update(saved)


def _fresh(name):
    for mod in (name, "spawn_utils", "runtime_env"):
        sys.modules.pop(mod, None)
    return importlib.import_module(name)


def test_hermes_run_measure_passes_no_window(monkeypatch, tmp_path):
    """hermes_hook_bridge._run_measure (blocking) must pass creationflags."""
    mod = _fresh("hermes_hook_bridge")
    monkeypatch.setattr(mod, "_NO_WINDOW", _CREATE_NO_WINDOW)
    measure = tmp_path / "measure.py"
    measure.write_text("# stub\n")
    monkeypatch.setattr(mod, "_locate_measure_py", lambda: measure)
    cap = {}

    def fake_run(cmd, **k):
        cap.update(k)

        class _R:
            returncode = 0
            stdout = "out"
            stderr = ""
        return _R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    mod._run_measure(["hermes-summary"])
    assert cap.get("creationflags") == _CREATE_NO_WINDOW


def test_hermes_doctor_smoke_check_passes_no_window(monkeypatch, tmp_path):
    """hermes_doctor._bridge_smoke_check must pass creationflags."""
    mod = _fresh("hermes_doctor")
    monkeypatch.setattr(mod, "_NO_WINDOW", _CREATE_NO_WINDOW)
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "hermes_hook_bridge.py").write_text("# stub\n")
    monkeypatch.setattr(mod, "hermes_home", lambda: tmp_path)
    monkeypatch.setattr(mod, "_plugin_install_dir", lambda root: plugin_dir)
    cap = {}

    def fake_run(cmd, **k):
        cap.update(k)

        class _R:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return _R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    mod._bridge_smoke_check()
    assert cap.get("creationflags") == _CREATE_NO_WINDOW


def test_copilot_cli_version_passes_no_window(monkeypatch):
    """`copilot --version` resolves to copilot.cmd on Windows (npm shim), which
    CreateProcess hands to cmd.exe -- a console host. It MUST be suppressed."""
    mod = _fresh("copilot_hook_bridge")
    monkeypatch.setattr(mod, "_NO_WINDOW", _CREATE_NO_WINDOW)
    cap = {}

    def fake_run(cmd, **k):
        cap.update(k)
        cap["argv"] = cmd

        class _R:
            returncode = 0
            stdout = "1.2.3"
            stderr = ""
        return _R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    version, raw = mod._copilot_cli_version()
    assert cap["argv"][1] == "--version"
    assert cap.get("creationflags") == _CREATE_NO_WINDOW
    assert version == (1, 2, 3)


def _load_copilot_degraded(monkeypatch):
    """Import copilot_hook_bridge with `spawn_utils` unimportable.

    Setting ``sys.modules["spawn_utils"] = None`` makes the ``from spawn_utils
    import spawn_detached`` raise ImportError, which is exactly the broken /
    partial-install path that defines the inline fallback.
    """
    for mod in ("copilot_hook_bridge", "spawn_utils", "runtime_env"):
        sys.modules.pop(mod, None)
    monkeypatch.setitem(sys.modules, "spawn_utils", None)
    mod = importlib.import_module("copilot_hook_bridge")
    sys.modules.pop("copilot_hook_bridge", None)
    return mod


def test_copilot_degraded_fallback_adds_no_window(monkeypatch):
    """The broken-install fallback must still suppress the console.

    It cannot detach (no spawn_utils), but a partial install must not mean a
    console window flashing on every stop hook.
    """
    mod = _load_copilot_degraded(monkeypatch)
    monkeypatch.setattr(mod, "_NO_WINDOW", _CREATE_NO_WINDOW)
    cap = {}
    real_popen = subprocess.Popen

    def fake_popen(argv, **k):
        cap.update(k)

        class _P:
            pid = 4242
        return _P()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    try:
        mod.spawn_detached(["x"], stdout=subprocess.DEVNULL)
    finally:
        monkeypatch.setattr(subprocess, "Popen", real_popen)
    assert cap.get("creationflags", 0) & _CREATE_NO_WINDOW, (
        "degraded fallback must OR in CREATE_NO_WINDOW"
    )


def test_copilot_degraded_fallback_preserves_caller_flags(monkeypatch):
    """OR-in, never clobber: a caller-supplied DETACHED flag must survive."""
    _DETACHED = 0x8
    mod = _load_copilot_degraded(monkeypatch)
    monkeypatch.setattr(mod, "_NO_WINDOW", _CREATE_NO_WINDOW)
    cap = {}
    real_popen = subprocess.Popen

    def fake_popen(argv, **k):
        cap.update(k)

        class _P:
            pid = 4242
        return _P()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    try:
        mod.spawn_detached(["x"], creationflags=_DETACHED)
    finally:
        monkeypatch.setattr(subprocess, "Popen", real_popen)
    flags = cap.get("creationflags", 0)
    assert flags & _DETACHED, "caller's DETACHED_PROCESS was clobbered"
    assert flags & _CREATE_NO_WINDOW, "CREATE_NO_WINDOW was not added"


def test_no_window_is_zero_on_posix():
    """On POSIX the guard must collapse to 0 so creationflags stays a no-op."""
    if os.name == "nt":
        pytest.skip("POSIX-only assertion")
    for name in sorted(SPAWNING_FILES):
        mod = _fresh(name[:-3])
        assert mod._NO_WINDOW == 0, f"{name}: _NO_WINDOW must be 0 on POSIX"


# ---------------------------------------------------------------------------
# Real-Windows checks (windows-latest CI leg only)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "nt", reason="Windows-only constant check")
def test_no_window_is_real_constant_on_windows():
    """On Windows the guard must resolve to the real CREATE_NO_WINDOW, not 0."""
    for name in sorted(SPAWNING_FILES):
        mod = _fresh(name[:-3])
        assert mod._NO_WINDOW == _CREATE_NO_WINDOW, (
            f"{name}: _NO_WINDOW resolved to {mod._NO_WINDOW!r}, expected "
            "subprocess.CREATE_NO_WINDOW"
        )


_CHILD_SCRIPT = (
    "import ctypes, sys;\n"
    "hwnd = ctypes.windll.kernel32.GetConsoleWindow();\n"
    "sys.exit(0 if hwnd == 0 else 1)\n"
)


@pytest.mark.skipif(os.name != "nt", reason="real-spawn check is Windows-only")
def test_blocking_run_with_no_window_has_no_console():
    """OS-level proof: a blocking subprocess.run with the guard gets no console.

    The child asks Windows for its own console window handle and exits non-zero
    if it has one. This is the shape used by _run_measure / _bridge_smoke_check
    / _copilot_cli_version, so a zero handle proves those spawns are silent.
    """
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT],
        capture_output=True,
        timeout=30,
        creationflags=_CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, (
        f"child exited {result.returncode}; GetConsoleWindow() != 0 means "
        "CREATE_NO_WINDOW did not take effect"
    )
