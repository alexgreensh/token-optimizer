"""No console-window flash from anything measure.py launches on Windows.

Three separate mechanisms, all pinned here because each one alone leaves a
hole:

1. OUR OWN SPAWNS. Every ``subprocess.*`` call site in measure.py (and in the
   generated ``dashboard-server.py`` template) must pass ``creationflags``
   carrying ``CREATE_NO_WINDOW``. Console-subsystem children (git, gh, schtasks,
   tasklist, wmic, netstat, powershell, ``code.cmd``, python.exe) otherwise get
   a window from Windows whenever the host has no console to inherit -- which is
   exactly the GitHub Desktop app case in the report. ``_NO_WINDOW`` is
   ``getattr(subprocess, "CREATE_NO_WINDOW", 0)`` so it is a literal no-op on
   POSIX; the source scan below therefore runs on every platform.

2. THE SCHEDULED TASK ACTION. This is the one CREATE_NO_WINDOW cannot fix: when
   Task Scheduler runs the action, WE are not the parent, so no creationflags of
   ours apply. The action used to be ``dashboard-launcher.cmd``, and a .cmd runs
   through console-subsystem cmd.exe -- a visible window on every fire (logon,
   boot, and every ``schtasks /Run`` the revive/restart self-heal issues, which
   is what pops windows on an otherwise idle machine). ``<Hidden>true</Hidden>``
   does NOT suppress it; it only hides the task from the Task Scheduler UI's
   default filter. The action must therefore be GUI-subsystem pythonw.exe, with
   the .cmd kept only as the fallback when no usable pythonw twin exists.

3. STICKY INSTALL-FAILED. A daemon install that fails structurally (no schtasks,
   locked state dir, un-spawnable interpreter) used to be retried by
   ``_ensure_dashboard_daemon`` every SessionStart and by
   ``_daemon_midsession_pulse`` every ~5min, forever -- the throttles bound the
   RATE, never the lifetime. On Windows each retry is another flash. A sticky
   ``.daemon-install-failed`` breadcrumb (same state dir and dot-file convention
   as the ``.daemon-thrash`` tombstone) must stop every revive/self-heal/ensure
   path, and must be cleared ONLY by an explicit successful ``setup-daemon`` --
   never by time, never by a fresh session.

Run: python3 -m pytest tests/test_measure_no_window.py -q
"""
from __future__ import annotations
from helpers.measure_split import measure_source as _split_measure_source
import ast
import importlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
MEASURE_PY = SCRIPTS / "measure.py"

sys.path.insert(0, str(SCRIPTS))

_CREATE_NO_WINDOW = 0x08000000

# Every spawn primitive that can allocate a console on Windows.
_SPAWN_FUNCS = {
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_output",
    "subprocess.check_call",
    "os.system",
    "os.popen",
}


def _measure_source() -> str:
    return _split_measure_source(MEASURE_PY)


def _spawn_sites(source: str):
    """Yield ``(lineno, dotted_name, node)`` for every spawn call in *source*."""
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func)
        if name in _SPAWN_FUNCS:
            yield node.lineno, name, node


def _creationflags_expr(node: ast.Call) -> str | None:
    for kw in node.keywords:
        if kw.arg == "creationflags":
            return ast.unparse(kw.value)
    return None


# ---------------------------------------------------------------------------
# 1. Source scan: measure.py itself
# ---------------------------------------------------------------------------
def test_measure_defines_no_window_helper():
    """The single module-level constant every call site references.

    ``getattr`` (not a bare attribute) so a POSIX build -- where
    ``subprocess.CREATE_NO_WINDOW`` does not exist -- yields 0 instead of
    raising at import time.
    """
    src = _measure_source()
    assert re.search(
        r'^_NO_WINDOW = getattr\(subprocess, "CREATE_NO_WINDOW", 0\)$',
        src, re.M,
    ), "measure.py must define _NO_WINDOW via getattr at module level"

    import measure
    assert measure._NO_WINDOW == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if os.name != "nt":
        assert measure._NO_WINDOW == 0, "_NO_WINDOW must be inert on POSIX"


# ---------------------------------------------------------------------------
# CLASS GUARD. Not "are the fixed sites still fixed" but "is EVERY site in
# the file hidden", so a future edit that adds an un-hidden spawn goes red in
# CI instead of in a user's face.
#
# Exemptions are keyed by (enclosing function, argv marker) -- never by line
# number, which shifts on every edit above it.
#
# The list is EMPTY on purpose. ``_NO_WINDOW`` is ``getattr(subprocess,
# "CREATE_NO_WINDOW", 0)``, i.e. literally 0 off Windows, so even a
# provably-POSIX-only site (``open``, ``xdg-open``, ``launchctl``, ``systemctl``)
# costs nothing to tag. Universal application means the rule is "all of them",
# which is checkable at a glance and has no judgement call at review time.
# Adding an entry here is a deliberate, reviewable act -- and
# ``test_posix_allowlist_stays_empty`` makes it visible.
#
# ``os.startfile`` is not in scope at all (it is ShellExecuteW: nt-only, opens a
# document with the shell's own handler, allocates no console, and accepts no
# creationflags). ``test_measure_has_no_shell_true_or_os_system`` covers the
# genuinely dangerous shell primitives.
_POSIX_ONLY_ALLOWLIST: set[tuple[str, str]] = set()


def _enclosing_functions(tree: ast.AST) -> dict[int, str]:
    """Map every line inside a function body to the name of the TIGHTEST
    function enclosing it, so a nested helper wins over its parent."""
    spans = [
        (node.lineno, node.end_lineno or node.lineno, node.name)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    # Widest first, so tighter spans overwrite them.
    spans.sort(key=lambda s: s[1] - s[0], reverse=True)
    owner: dict[int, str] = {}
    for start, end, name in spans:
        for line in range(start, end + 1):
            owner[line] = name
    return owner


def _argv_marker(node: ast.Call) -> str:
    """A stable, human-meaningful handle for a spawn site: the first argv
    element when it is a literal (``git``, ``schtasks``, …), else the unparsed
    first argument. Never a line number."""
    if not node.args:
        return "<no-args>"
    first = node.args[0]
    if isinstance(first, (ast.List, ast.Tuple)) and first.elts:
        head = first.elts[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return head.value
    return ast.unparse(first)[:60]


def test_every_measure_spawn_site_carries_no_window():
    """THE class guard: any ``subprocess.*`` site in measure.py without
    ``CREATE_NO_WINDOW`` is a cmd-window flash on a console-less Windows host.

    Accepts any creationflags expression that MENTIONS ``_NO_WINDOW``, so a
    future detached site may OR it into ``DETACHED_PROCESS`` et al. rather than
    replace them -- the brief's rule is "add, never remove".
    """
    src = _measure_source()
    tree = ast.parse(src)
    owner = _enclosing_functions(tree)

    offenders = []
    total = 0
    for lineno, name, node in _spawn_sites(src):
        total += 1
        func = owner.get(lineno, "<module>")
        marker = _argv_marker(node)
        if (func, marker) in _POSIX_ONLY_ALLOWLIST:
            continue
        expr = _creationflags_expr(node)
        if expr is None or "_NO_WINDOW" not in expr:
            offenders.append(
                f"{func}() spawning {marker!r} (measure.py:{lineno}) -> "
                f"creationflags={expr!r}"
            )

    # Anti-vacuous guard: a scan that silently matches nothing passes forever.
    assert total > 40, (
        f"spawn scan found only {total} sites in a {len(src.splitlines())}-line "
        "file -- the SCAN is broken, not the code"
    )
    assert not offenders, (
        f"{len(offenders)} spawn site(s) missing CREATE_NO_WINDOW -- each one "
        "flashes a console window on Windows:\n  " + "\n  ".join(offenders)
    )


def test_posix_allowlist_stays_empty():
    """Tagging a POSIX-only site costs nothing (``_NO_WINDOW`` is 0 there), so
    there is no legitimate reason to exempt one. If this fails, someone widened
    the exemption surface -- read why before accepting it."""
    assert _POSIX_ONLY_ALLOWLIST == set(), (
        f"unexpected no-flash-fix exemptions: {_POSIX_ONLY_ALLOWLIST}"
    )


def test_class_guard_would_catch_a_new_unhidden_spawn():
    """Negative test for the guard itself: prove the scan actually fails on a
    bad site, so a future refactor cannot quietly neuter it into a pass."""
    bad = "import subprocess\ndef f():\n    subprocess.run(['git', 'status'])\n"
    sites = list(_spawn_sites(bad))
    assert len(sites) == 1
    assert _creationflags_expr(sites[0][2]) is None
    good = ("import subprocess\ndef f():\n"
            "    subprocess.run(['git', 'status'], creationflags=_NO_WINDOW)\n")
    sites = list(_spawn_sites(good))
    assert "_NO_WINDOW" in _creationflags_expr(sites[0][2])
    # And an OR-ed detached form must still count as hidden.
    ored = ("import subprocess\ndef f():\n"
            "    subprocess.Popen(['x'], creationflags=DETACHED | _NO_WINDOW)\n")
    sites = list(_spawn_sites(ored))
    assert "_NO_WINDOW" in _creationflags_expr(sites[0][2])


def test_argv_marker_is_stable_against_line_shifts():
    """The exemption key must not be a line number."""
    src = "import subprocess\nsubprocess.run(['schtasks', '/Query'])\n"
    node = list(_spawn_sites(src))[0][2]
    assert _argv_marker(node) == "schtasks"


def test_measure_has_no_shell_true_or_os_system():
    """``shell=True``/``os.system``/``os.popen`` route through cmd.exe, which
    flashes a window that no creationflags on OUR call can suppress. There are
    none today; this keeps it that way."""
    src = _measure_source()
    assert "shell=True" not in src, "shell=True spawns cmd.exe -- a guaranteed flash"
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call):
            assert ast.unparse(node.func) not in ("os.system", "os.popen"), (
                f"os.system/os.popen at measure.py:{node.lineno} routes through cmd.exe"
            )


def test_detached_spawns_still_use_spawn_utils():
    """The three fire-and-forget spawns must keep routing through
    ``spawn_detached`` (DETACHED_PROCESS + the CREATE_BREAKAWAY_FROM_JOB retry).
    The no-flash fix must not have replaced detach semantics with a bare CREATE_NO_WINDOW --
    the children have to OUTLIVE the hook."""
    src = _measure_source()
    assert "from spawn_utils import spawn_detached" in src
    assert src.count("spawn_detached(") >= 3, (
        "a detached spawn was downgraded to a plain subprocess call"
    )


# ---------------------------------------------------------------------------
# 2. Source scan: the GENERATED dashboard-server.py template
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def daemon_src():
    if "measure" in sys.modules:
        del sys.modules["measure"]
    import measure
    src = measure._generate_daemon_script()
    if "measure" in sys.modules:
        del sys.modules["measure"]
    return src


def test_generated_daemon_parses(daemon_src):
    ast.parse(daemon_src)  # must not raise


def test_generated_daemon_spawns_carry_no_window(daemon_src):
    """The daemon shells out to ``python measure.py …`` for the v5-toggle,
    skill/MCP-manage and manual-regenerate endpoints. Each is a console child.

    The regen ``Popen`` is the documented exception: it keeps its INLINE
    nt-branch (``CREATE_NO_WINDOW`` on nt, ``start_new_session`` on POSIX)
    because the regen child is transient and must NOT be detached. Assert the
    inline form is still there rather than demanding the shared constant.
    """
    runs = []
    popens = []
    for lineno, name, node in _spawn_sites(daemon_src):
        (popens if name == "subprocess.Popen" else runs).append((lineno, node))

    assert len(runs) >= 3, "expected the 3 measure.py-shelling endpoints"
    for lineno, node in runs:
        expr = _creationflags_expr(node)
        assert expr is not None and "_NO_WINDOW" in expr, (
            f"generated daemon line {lineno}: subprocess.run without _NO_WINDOW"
        )

    assert len(popens) == 1, "expected exactly the regen Popen"
    assert re.search(
        r'_flags = getattr\(subprocess, "CREATE_NO_WINDOW", 0\)', daemon_src
    ), "the regen spawn's inline nt CREATE_NO_WINDOW branch was removed"

    assert re.search(
        r'^_NO_WINDOW = getattr\(subprocess, "CREATE_NO_WINDOW", 0\)$',
        daemon_src, re.M,
    ), "generated daemon must define its own _NO_WINDOW (it is standalone)"
    # Standalone: no sibling helper module on its sys.path.
    assert "spawn_detached" not in daemon_src
    assert "detach_spawn_kwargs" not in daemon_src


def test_generated_daemon_reopens_stdio_under_pythonw(daemon_src):
    """pythonw.exe gives the process NO std handles, so ``sys.stdout`` is None
    and the stdout.log/stderr.log trail the .cmd launcher used to provide would
    silently vanish -- the exact failure mode that once let a dead regeneration
    look healthy for two days. The daemon must reopen them onto the same files."""
    assert "if sys.stdout is None or sys.stderr is None:" in daemon_src, (
        "generated daemon must detect pythonw's absent std handles"
    )
    assert 'os.path.join(LOG_DIR, "stderr.log")' in daemon_src, (
        "generated daemon must reopen stderr onto the daemon log dir"
    )


# ---------------------------------------------------------------------------
# 3. The Scheduled Task action (the idle-pop source)
# ---------------------------------------------------------------------------
@pytest.fixture()
def m(monkeypatch):
    """measure.py imported with its state dir pinned into a temp dir."""
    tmp = tempfile.mkdtemp(prefix="to-107-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    # This module replaces daemon OS actions before invoking lifecycle code.
    monkeypatch.setattr(mod, "_daemon_snapshot_sandboxed", lambda: False)
    monkeypatch.setattr(mod, "_is_foreign_runtime", lambda: False)
    monkeypatch.setattr(mod, "detect_runtime", lambda: "claude")
    # The legacy daemon dir derives from RUNTIME_DIR (the REAL ~/.claude, not
    # covered by TOKEN_OPTIMIZER_SNAPSHOT_DIR); pin it into the sandbox so no
    # test can ever read or heal a real machine's legacy shim. Built EAGERLY:
    # under the fake-nt monkeypatch, a lazy Path() would flavour as
    # WindowsPath and blow up on POSIX.
    _legacy_sandbox = Path(tmp) / "legacy"
    monkeypatch.setattr(mod, "_legacy_daemon_dir",
                        lambda: _legacy_sandbox, raising=False)
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


def test_task_action_prefers_pythonw(m, monkeypatch, tmp_path):
    """With a usable pythonw twin, the ``<Exec>`` action is pythonw.exe + the
    daemon script -- NOT the .cmd launcher."""
    pyw = tmp_path / "pythonw.exe"
    pyw.write_text("")
    monkeypatch.setattr(m, "_windows_gui_python", lambda: str(pyw))
    command, arguments = m._windows_task_exec_action(
        r"C:\state\dashboard-server.py", r"C:\state\dashboard-launcher.cmd")
    assert command == str(pyw)
    assert arguments == '"C:\\state\\dashboard-server.py"'
    assert not command.lower().endswith(".cmd"), (
        "a .cmd action runs through cmd.exe -- console window on every task fire"
    )


def test_task_action_falls_back_to_cmd_without_pythonw(m, monkeypatch):
    """No usable pythonw (MS Store alias, or no twin) -> the old .cmd launcher.
    It still works, it just shows a window; a visible window beats a daemon that
    never starts. The installer prints this so the user is not surprised."""
    monkeypatch.setattr(m, "_windows_gui_python", lambda: None)
    command, arguments = m._windows_task_exec_action(
        r"C:\state\dashboard-server.py", r"C:\state\dashboard-launcher.cmd")
    assert command == r"C:\state\dashboard-launcher.cmd"
    assert arguments == ""


def test_windows_gui_python_is_posix_noop(m):
    if os.name == "nt":
        pytest.skip("POSIX-only assertion")
    assert m._windows_gui_python() is None
    assert m._detached_python_exe() == (sys.executable or "python3"), (
        "POSIX interpreter selection must be byte-identical to the pre-fix form"
    )


def test_detached_python_exe_prefers_pythonw(m, monkeypatch, tmp_path):
    pyw = tmp_path / "pythonw.exe"
    pyw.write_text("")
    monkeypatch.setattr(m, "_windows_gui_python", lambda: str(pyw))
    assert m._detached_python_exe() == str(pyw)
    monkeypatch.setattr(m, "_windows_gui_python", lambda: None)
    assert m._detached_python_exe() == (sys.executable or "python3")


def test_schtasks_xml_emits_pythonw_command_and_arguments(m):
    xml = m._generate_schtasks_xml(
        task_name="TokenOptimizerDashboard",
        user_id="WORKGROUP\\bob",
        command=r"C:\Python\pythonw.exe",
        arguments=r'"C:\state\dashboard-server.py"',
    )
    assert "<Command>C:\\Python\\pythonw.exe</Command>" in xml
    assert "<Arguments>&quot;C:\\state\\dashboard-server.py&quot;</Arguments>" in xml
    # The escaper must still run over both fields.
    assert "WORKGROUP\\bob" in xml


def test_schtasks_xml_omits_arguments_for_cmd_fallback(m):
    """The fallback must produce the exact pre-fix XML shape (no empty
    <Arguments/>, which some Task Scheduler builds reject)."""
    xml = m._generate_schtasks_xml(
        task_name="TokenOptimizerDashboard",
        user_id="bob",
        command=r"C:\state\dashboard-launcher.cmd",
        arguments="",
    )
    assert "<Arguments>" not in xml
    assert "<Command>C:\\state\\dashboard-launcher.cmd</Command>" in xml


def test_schtasks_xml_escapes_command(m):
    xml = m._generate_schtasks_xml(
        task_name="T", user_id="bob",
        command=r"C:\O'Brien & Co\pythonw.exe",
        arguments=r'"C:\a&b\srv.py"',
    )
    assert "&amp;" in xml and "&apos;" in xml
    assert " & " not in xml.replace("&amp;", "")


def test_installer_wires_the_pythonw_action_into_the_xml(m):
    """The installer must feed ``_windows_task_exec_action``'s output into
    ``_generate_schtasks_xml`` -- not the launcher path directly. Source-scanned
    because driving the real installer needs schtasks."""
    src = _measure_source()
    assert "_task_command, _task_arguments = _windows_task_exec_action(" in src
    assert re.search(
        r"_generate_schtasks_xml\(\s*task_name=WINDOWS_TASK_NAME,\s*"
        r"user_id=user_id,\s*command=_task_command,\s*arguments=_task_arguments,",
        src,
    ), "installer must pass the resolved action, not launcher_path"


# ---------------------------------------------------------------------------
# 3b. The generated .cmd shim (fallback path; invisible to AST scans)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def shim_src():
    if "measure" in sys.modules:
        del sys.modules["measure"]
    import measure
    src = measure._generate_windows_launcher_cmd(
        r"C:\state\dashboard-server.py", r"C:\state\logs")
    if "measure" in sys.modules:
        del sys.modules["measure"]
    return src


def _rung_order(src: str) -> list[str]:
    """Order in which the shim tries each interpreter."""
    order = []
    for line in src.splitlines():
        stripped = line.strip()
        for exe in ("pythonw.exe", "pyw.exe", "py.exe", "python.exe"):
            if stripped.startswith(exe + " ") and exe not in order:
                order.append(exe)
    return order


def test_shim_tries_gui_interpreters_before_console_ones(shim_src):
    """THE Lane-D finding: the ladder used to try console ``py.exe -3`` FIRST,
    so on any host with the Python Launcher installed (the common case) the
    long-lived daemon itself ran as a console process. GUI-subsystem
    interpreters must come first.

    The no-flash fix updated the pythonw rung: it is now PATH-resolved into
    ``PYW_EXE`` and guarded against Microsoft Store aliases (see
    ``test_shim_pythonw_rung_guards_windows_store_alias`` in
    test_heal_hardening.py), so ``_rung_order`` sees the remaining bare
    rungs. The guarded pythonw block must still come before every other rung.
    """
    order = _rung_order(shim_src)
    assert order == ["pyw.exe", "py.exe", "python.exe"], (
        f"unexpected bare-rung order: {order}"
    )
    # The guarded pythonw invocation precedes the pyw.exe rung.
    pythonw_idx = shim_src.index('if defined PYW_EXE "%PYW_EXE%"')
    pyw_idx = shim_src.index("pyw.exe -3")
    assert pythonw_idx < pyw_idx, (
        "the (guarded) pythonw.exe rung must still be tried first"
    )
    # And GUI interpreters still come before console ones.
    assert pyw_idx < shim_src.index("py.exe -3") < shim_src.index("python.exe ")


def test_shim_does_not_spawn_where_probes(shim_src):
    """``where.exe`` is itself a console binary; the old shim ran up to three of
    them per fire. Detection is cmd's own 9009 'command not found' code now."""
    assert "where " not in shim_src.lower(), (
        "the shim must not shell out to where.exe to probe interpreters"
    )
    assert '"%ERRORLEVEL%"=="9009"' in shim_src, (
        "interpreter detection must use cmd's 9009 not-found code"
    )


def test_shim_errorlevel_checks_are_not_inside_blocks(shim_src):
    """``%ERRORLEVEL%`` inside a ``( ... )`` block expands once at block-parse
    time, so a block form silently tests a stale value. The ladder must be flat."""
    for line in shim_src.splitlines():
        if "%ERRORLEVEL%" in line and "9009" in line:
            assert not line.startswith(" "), (
                f"errorlevel check appears indented (inside a block): {line!r}"
            )


def test_shim_still_captures_daemon_logs(shim_src):
    """The shim exists to keep a stderr trail; a silent daemon death is the
    failure mode that once looked healthy for two days."""
    assert '2>>"%STDERR_LOG%"' in shim_src
    assert '1>>"%STDOUT_LOG%"' in shim_src


def test_shim_is_only_the_fallback_action(m, monkeypatch):
    """A .cmd action still gets a cmd.exe console from Task Scheduler -- nothing
    inside the shim can prevent its own host. So it must never be chosen while a
    usable pythonw twin exists."""
    monkeypatch.setattr(m, "_windows_gui_python", lambda: r"C:\Py\pythonw.exe")
    command, _ = m._windows_task_exec_action(r"C:\s\d.py", r"C:\s\l.cmd")
    assert not command.lower().endswith(".cmd")


# ---------------------------------------------------------------------------
# 3c. Migration wiring inside the restart path
# ---------------------------------------------------------------------------
def test_restart_runs_the_heal_between_end_and_run(m):
    """Ordering is load-bearing: the re-registration's port-owner pre-check
    fails while the daemon still holds DAEMON_PORT, so the heal must sit AFTER
    /End and BEFORE /Run."""
    src = _measure_source()
    body = src[src.index("def _restart_dashboard_daemon("):]
    body = body[:body.index("\ndef ", 10)]
    end_idx = body.index('"schtasks", "/End"')
    heal_idx = body.index("_heal_windows_task_action()")
    run_idx = body.index('"schtasks", "/Run"')
    assert end_idx < heal_idx < run_idx, (
        "the no-flash task-action heal must run between schtasks /End and /Run"
    )


def test_restart_windows_path_is_gated_by_the_sticky_marker(m, monkeypatch):
    """The heal lives inside the restart path, so the sticky marker must still
    stop it -- otherwise a broken install re-registers a task every prompt."""
    monkeypatch.setattr(m, "_read_config_flag", lambda k, d=None: False)
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: False)
    monkeypatch.setattr(m, "_restart_dashboard_daemon",
                        _explode("_restart_dashboard_daemon"))
    monkeypatch.setattr(m, "_daemon_service_installed",
                        _explode("_daemon_service_installed"))
    _arm_marker(m)
    assert m._ensure_dashboard_daemon(force=True) == "noop-install-failed"


# ---------------------------------------------------------------------------
# 3d. Runtime self-heal of ALREADY-INSTALLED flashers (_heal_windows_console_flash)
#
# A generator fix only helps the next install. The reporter already has the
# flashing task and shim on disk, and nothing in the update path rewrites either.
# ---------------------------------------------------------------------------
def _fake_nt(m, monkeypatch):
    """Make the heal believe it is on Windows without touching the real os
    module's ``name`` (which breaks pathlib on macOS)."""
    monkeypatch.setattr(m.os, "name", "nt", raising=False)


class _SchtasksRecorder:
    """Stands in for subprocess.run, recording schtasks verbs and answering
    /Query with a controllable task XML."""

    def __init__(self, action_command, query_rc=0):
        self.action_command = action_command
        self.query_rc = query_rc
        self.verbs = []

    def __call__(self, argv, *a, **k):
        self.verbs.append(list(argv))

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        if "/Query" in argv:
            _R.returncode = self.query_rc
            _R.stdout = (
                "<Task><Actions><Exec>"
                f"<Command>{self.action_command}</Command>"
                "</Exec></Actions></Task>"
            )
        return _R()


def _heal_env(m, monkeypatch, action_command, query_rc=0,
              pythonw=r"C:\Py\pythonw.exe", install_ok=True):
    _fake_nt(m, monkeypatch)
    monkeypatch.setattr(m, "_windows_gui_python", lambda: pythonw)
    monkeypatch.setattr(m, "_persist_dashboard_host", lambda persist=True: "127.0.0.1")
    monkeypatch.setattr(m, "_read_config_flag", lambda k, d=None: False)
    # The heal throttle stamps a config flag; CONFIG_PATH is NOT sandboxed by
    # TOKEN_OPTIMIZER_SNAPSHOT_DIR, so a real write would touch the dev
    # machine's config.json.
    monkeypatch.setattr(m, "_write_config_flag", lambda k, v: None)
    # Windows-shaped action paths cannot exist on a POSIX test host; default to
    # "alive" so only tests that WANT a dead path (T5-H1) simulate one.
    monkeypatch.setattr(m, "_windows_action_path_exists", lambda p: True)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    rec = _SchtasksRecorder(action_command, query_rc=query_rc)
    monkeypatch.setattr(m.subprocess, "run", rec)
    installs = []
    monkeypatch.setattr(
        m, "_install_task_scheduler_daemon",
        lambda **k: (installs.append(k), install_ok)[1])
    return rec, installs


def test_heal_repairs_a_cmd_action_task(m, monkeypatch):
    """THE installed-base gap: the auto-update path only /End + /Run's the
    EXISTING task, so without this every current Windows user keeps the .cmd
    action -- and its console window -- forever."""
    rec, installs = _heal_env(m, monkeypatch, r"C:\s\dashboard-launcher.cmd")
    assert m._heal_windows_console_flash() is True
    assert len(installs) == 1
    assert installs[0]["soft_fail"] is True
    # It stopped the task before re-registering (the installer refuses while
    # something holds DAEMON_PORT).
    assert any("/End" in v for v in rec.verbs)


def test_heal_repairs_a_bare_console_python_action(m, monkeypatch):
    """Not only .cmd: a task action pointing straight at console python.exe or
    py.exe flashes just the same. (Bare ``cmd.exe`` was REMOVED from
    this set -- our installers never bake it, so matching it meant rewriting a
    user's own wrapper arrangement; see test_heal_hardening.py.)"""
    for exe in (r"C:\Python\python.exe", r"C:\Windows\py.exe", "python3.exe"):
        _, installs = _heal_env(m, monkeypatch, exe)
        assert m._heal_windows_console_flash() is True, exe
        assert len(installs) == 1, exe


def test_heal_is_idempotent(m, monkeypatch):
    """Second run must be a no-op -- the pythonw action is not a flasher, so the
    same check that triggered the repair now reports 'not broken'."""
    _, installs = _heal_env(m, monkeypatch, r"C:\Py\pythonw.exe")
    assert m._heal_windows_console_flash() is False
    assert installs == []


def test_heal_never_creates_an_absent_task(m, monkeypatch):
    """Healing an absent task would resurrect a daemon the user declined or
    uninstalled. A failed /Query means 'no task', never 'install one'."""
    _, installs = _heal_env(m, monkeypatch, "irrelevant", query_rc=1)
    assert m._heal_windows_console_flash() is False
    assert installs == []


def test_heal_refuses_while_tombstoned(m, monkeypatch):
    """An uninstall/thrash tombstone means the daemon is meant to be dead."""
    _, installs = _heal_env(m, monkeypatch, r"C:\s\dashboard-launcher.cmd")
    m.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    m.DAEMON_THRASH_BREADCRUMB.write_text("uninstalled", encoding="utf-8")
    assert m._heal_windows_console_flash() is False
    assert installs == []


def test_heal_refuses_while_daemon_disabled(m, monkeypatch):
    _, installs = _heal_env(m, monkeypatch, r"C:\s\dashboard-launcher.cmd")
    monkeypatch.setattr(m, "_read_config_flag",
                        lambda k, d=None: k == "daemon_disabled")
    assert m._heal_windows_console_flash() is False
    assert installs == []


def test_heal_refuses_while_sticky_install_failed(m, monkeypatch):
    """The no-flash-fix marker exists to stop exactly this kind of per-session poking."""
    _, installs = _heal_env(m, monkeypatch, r"C:\s\dashboard-launcher.cmd")
    _arm_marker(m)
    assert m._heal_windows_console_flash() is False
    assert installs == []


def test_heal_is_a_strict_noop_off_windows(m, monkeypatch):
    if os.name == "nt":
        pytest.skip("POSIX-only assertion")
    called = []
    monkeypatch.setattr(m.subprocess, "run",
                        lambda *a, **k: called.append(a) or None)
    assert m._heal_windows_console_flash() is False
    assert called == [], "the heal must not touch schtasks off Windows"


def test_heal_failure_never_runs_a_still_flasher_action(m, monkeypatch):
    """The no-flash regression: this test USED to pin the opposite
    -- a compensating /Run after a failed re-registration. But when the
    install fails, the registered action is still the .cmd flasher, so that
    /Run IS one extra console flash per session, on exactly the hosts the heal
    cannot fix. The restore must refuse to fire a flasher (or dead) action;
    the LogonTrigger revives the daemon at next logon instead. The
    safe-restore direction (action already migrated when the installer raised)
    is covered in test_heal_hardening.py."""
    rec, _ = _heal_env(m, monkeypatch, r"C:\s\dashboard-launcher.cmd", install_ok=False)
    assert m._heal_windows_console_flash() is False
    assert not any("/Run" in v for v in rec.verbs), (
        "a failed heal must not /Run a still-.cmd action -- that is the flash "
        "the no-flash fix exists to remove"
    )


def test_heal_preserves_the_persisted_bind_host(m, monkeypatch):
    """A migration must not silently move a deliberately network-exposed daemon
    back to loopback."""
    _fake_nt(m, monkeypatch)
    monkeypatch.setattr(m, "_windows_gui_python", lambda: r"C:\Py\pythonw.exe")
    monkeypatch.setattr(m, "_persist_dashboard_host", lambda persist=True: "0.0.0.0")
    monkeypatch.setattr(m, "_read_config_flag", lambda k, d=None: False)
    monkeypatch.setattr(m, "_write_config_flag", lambda k, v: None)
    monkeypatch.setattr(m, "_windows_action_path_exists", lambda p: True)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    monkeypatch.setattr(m.subprocess, "run",
                        _SchtasksRecorder(r"C:\s\dashboard-launcher.cmd"))
    seen = {}
    monkeypatch.setattr(m, "_install_task_scheduler_daemon",
                        lambda **k: (seen.update(k), True)[1])
    m._heal_windows_console_flash()
    assert seen["effective_host"] == "0.0.0.0"


def test_heal_does_nothing_without_a_pythonw_twin(m, monkeypatch):
    """Nothing better to migrate TO -- do not churn the task registration."""
    _, installs = _heal_env(m, monkeypatch, r"C:\s\dashboard-launcher.cmd", pythonw=None)
    assert m._heal_windows_console_flash() is False
    assert installs == []


def test_action_flasher_classifier(m):
    """Update: this test USED to pin ``launcher.BAT`` and bare
    ``cmd.exe`` as "ours" -- i.e. ANY batch file in our task slot was
    'positively identified' and rewritten, wiping a user's own wrapper.
    A .cmd/.bat now matches only OUR generated launcher name."""
    for bad in (r"C:\s\dashboard-launcher.cmd", "DASHBOARD-LAUNCHER.CMD",
                "py.exe", r"C:\Python\python.exe", "python3.exe"):
        assert m._windows_action_is_console_flasher(bad) is True, bad
    for good in (r"C:\Py\pythonw.exe", "pyw.exe", "PYTHONW.EXE", "", None,
                 "launcher.BAT", "cmd.exe", "conhost.exe",
                 r"C:\x\my-daemon-wrapper.cmd"):
        assert m._windows_action_is_console_flasher(good) is False, good
    # Unrecognised commands are left alone: the repair re-registers the whole
    # task, so we only touch actions we can positively identify as ours.
    assert m._windows_action_is_console_flasher(r"C:\venv\Scripts\myshim.exe") is False


def test_shim_heal_rewrites_a_stale_on_disk_launcher(m, monkeypatch):
    """(b) An installed .cmd from an older build keeps its console-first ladder
    and where.exe probes until something rewrites it."""
    _fake_nt(m, monkeypatch)
    m.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    launcher = m.SNAPSHOT_DIR / m.WINDOWS_LAUNCHER_NAME
    launcher.write_text(
        "@echo off\r\nwhere py.exe >nul 2>&1\r\npy.exe -3 x\r\n", encoding="utf-8")
    assert m._heal_windows_launcher_shim() is True
    healed = launcher.read_text(encoding="utf-8")
    assert "where " not in healed.lower()
    # The no-flash fix: the pythonw rung is now PATH-resolved + Store-alias
    # guarded, so the first BARE rung is pyw.exe; the guarded pythonw
    # invocation still precedes it.
    assert _rung_order(healed)[0] == "pyw.exe"
    assert healed.index('if defined PYW_EXE "%PYW_EXE%"') < healed.index("pyw.exe -3")
    # Idempotent.
    assert m._heal_windows_launcher_shim() is False


def test_shim_heal_never_creates_a_missing_launcher(m, monkeypatch):
    """Writing a launcher for a daemon that was never installed is half an
    install -- and on a machine where the user declined it, unwanted."""
    _fake_nt(m, monkeypatch)
    m.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    launcher = m.SNAPSHOT_DIR / m.WINDOWS_LAUNCHER_NAME
    assert not launcher.exists()
    assert m._heal_windows_launcher_shim() is False
    assert not launcher.exists()


def test_ensure_health_calls_the_console_flash_heal():
    """Wiring guard: the heal must actually be invoked from ensure-health, next
    to the other _heal_* migrations, inside a fail-open try/except."""
    src = _measure_source()
    assert "if _heal_windows_console_flash():" in src, (
        "the no-flash heal is never called from ensure-health"
    )
    idx = src.index("if _heal_windows_console_flash():")
    window = src[idx - 1500:idx + 500]
    assert "if _heal_keepwarm_plist_path():" in window, (
        "the heal must sit with the other ensure-health _heal_* calls"
    )
    assert "except Exception as _e:" in window, (
        "the call site must be fail-open like its siblings"
    )
    # And it must be inside run_ensure_health, not some other function.
    tree = ast.parse(src)
    owner = _enclosing_functions(tree)
    lineno = src[:idx].count("\n") + 1
    assert owner.get(lineno) == "run_ensure_health", (
        f"the heal is called from {owner.get(lineno)!r}, not run_ensure_health"
    )


# ---------------------------------------------------------------------------
# 4. Sticky install-failed marker
# ---------------------------------------------------------------------------
def _arm_marker(m):
    m._write_daemon_install_failed_marker("test")
    assert m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists()


def _explode(name):
    def _boom(*a, **k):
        raise AssertionError(f"{name} must not run while the marker is armed")
    return _boom


def test_marker_lives_beside_the_thrash_tombstone(m):
    """Same state dir, same dot-file convention -- so an operator who knows the
    tombstone knows where to look for this one."""
    assert m.DAEMON_INSTALL_FAILED_BREADCRUMB.name == ".daemon-install-failed"
    assert (m.DAEMON_INSTALL_FAILED_BREADCRUMB.parent
            == m.DAEMON_THRASH_BREADCRUMB.parent)


def test_ensure_daemon_refuses_while_marker_armed(m, monkeypatch):
    """THE no-flash regression: a permanently-failed install must not be retried,
    so the costly (window-flashing) path never runs again.

    The gate now runs ONE cheap identity-checked
    port probe (no subprocess, cannot flash) so a live daemon can disprove and
    clear a stale marker -- hence ``_verify_daemon_port`` is stubbed dead
    rather than exploding. Everything costly must still never run."""
    monkeypatch.setattr(m, "_read_config_flag", lambda k, d=None: False)
    monkeypatch.setattr(m, "_daemon_service_installed", _explode("_daemon_service_installed"))
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: False)
    _arm_marker(m)
    assert m._ensure_dashboard_daemon() == "noop-install-failed"


def test_ensure_daemon_refuses_even_with_force(m, monkeypatch):
    """``force=True`` is the ``daemon-revive`` subcommand -- the per-turn retry
    the marker exists to stop. It must NOT bypass the marker."""
    monkeypatch.setattr(m, "_read_config_flag", lambda k, d=None: False)
    monkeypatch.setattr(m, "_daemon_service_installed", _explode("_daemon_service_installed"))
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: False)
    _arm_marker(m)
    assert m._ensure_dashboard_daemon(force=True) == "noop-install-failed"


def test_midsession_pulse_refuses_while_marker_armed(m, monkeypatch):
    monkeypatch.setattr(m, "_read_config_flag", lambda k, d=None: False)
    monkeypatch.setattr(m, "_verify_daemon_port", _explode("_verify_daemon_port"))
    monkeypatch.setattr(m, "spawn_detached", _explode("spawn_detached"))
    _arm_marker(m)
    assert m._daemon_midsession_pulse() == "noop-install-failed"


def test_pulse_still_revives_when_marker_absent(m, monkeypatch):
    """Guard against pass-on-overreach: with NO marker the pulse must still do
    its job. A test that only proves refusal would pass on a hard-disabled
    daemon."""
    monkeypatch.setattr(m, "_read_config_flag", lambda k, d=None: 0)
    monkeypatch.setattr(m, "_write_config_flag", lambda k, v: None)
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: False)
    monkeypatch.setattr(m, "_normalized_platform", lambda: "Darwin")
    spawned = {}

    def _fake_spawn(argv, **k):
        spawned["argv"] = argv
        return object()
    monkeypatch.setattr(m, "spawn_detached", _fake_spawn)
    assert not m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists()
    assert m._daemon_midsession_pulse() == "revive-spawned"
    assert "daemon-revive" in spawned["argv"]


def test_transient_install_failure_does_not_arm_the_marker(m, monkeypatch):
    """The no-flash regression: this test USED to pin the opposite -- ANY
    installer failure armed the permanent marker. A missing dashboard file is
    a transient class (disk full once, a regen hiccup); it must stay
    retryable under the 24h throttle, never become a permanent kill switch."""
    monkeypatch.setattr(m, "_read_config_flag", lambda k, d=None: False)
    monkeypatch.setattr(m, "_write_config_flag", lambda k, v: None)
    monkeypatch.setattr(m, "_normalized_platform", lambda: "Darwin")
    monkeypatch.setattr(m, "_daemon_service_installed", lambda s=None: False)
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: False)
    monkeypatch.setattr(m, "_ensure_dashboard_file", lambda **kw: False)
    assert not m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists()
    assert m._ensure_dashboard_daemon(force=True) == "install-failed"
    assert not m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists(), (
        "a transient install failure must not permanently disable the daemon"
    )
    # And the next attempt is allowed to try again (bounded by the throttle,
    # which force=True legitimately bypasses).
    assert m._ensure_dashboard_daemon(force=True) == "install-failed"


def test_installer_exception_does_not_arm_the_marker(m, monkeypatch):
    """An UNCLASSIFIED exception (OSError from a full disk, an AV lock, a
    gui-domain bootstrap over SSH) is not evidence of a structural failure.
    Definitive classes are armed inside the installer where they can actually
    be identified (see test_heal_hardening.py)."""
    monkeypatch.setattr(m, "_read_config_flag", lambda k, d=None: False)
    monkeypatch.setattr(m, "_write_config_flag", lambda k, v: None)
    monkeypatch.setattr(m, "_normalized_platform", lambda: "Windows")
    monkeypatch.setattr(m, "_daemon_service_installed", lambda s=None: False)
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: False)
    monkeypatch.setattr(m, "_ensure_dashboard_file", lambda **kw: True)
    monkeypatch.setattr(m, "_get_or_create_daemon_token", lambda: "t")

    def _boom(**k):
        raise OSError("disk full")
    monkeypatch.setattr(m, "_install_task_scheduler_daemon", _boom)
    assert m._ensure_dashboard_daemon(force=True) == "install-failed"
    assert not m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists()


def test_failed_restart_does_not_arm_the_marker(m, monkeypatch):
    """'restart-failed' is reached through a blanket ``except Exception`` whose
    common inhabitants are transient (a 5s launchctl kickstart TimeoutExpired
    on a busy Mac -- T3-H3/T7-H2). An INSTALLED daemon proves the structure
    works; one slow service-manager call must not kill it forever, on any
    platform."""
    monkeypatch.setattr(m, "_read_config_flag", lambda k, d=None: False)
    monkeypatch.setattr(m, "_write_config_flag", lambda k, v: None)
    monkeypatch.setattr(m, "_normalized_platform", lambda: "Windows")
    monkeypatch.setattr(m, "_daemon_service_installed", lambda s=None: True)
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: False)
    monkeypatch.setattr(m, "_restart_dashboard_daemon", lambda s: "restart-failed")
    assert m._ensure_dashboard_daemon(force=True) == "restart-failed"
    assert not m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists()


def test_restart_stale_does_not_arm_the_marker(m, monkeypatch):
    """'restart-stale' means the service DID restart and only the landing probe
    was inconclusive. Treating it as a failure would permanently disable the
    self-heal for a daemon that is fine."""
    monkeypatch.setattr(m, "_read_config_flag", lambda k, d=None: False)
    monkeypatch.setattr(m, "_write_config_flag", lambda k, v: None)
    monkeypatch.setattr(m, "_normalized_platform", lambda: "Darwin")
    monkeypatch.setattr(m, "_daemon_service_installed", lambda s=None: True)
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: False)
    monkeypatch.setattr(m, "_restart_dashboard_daemon", lambda s: "restart-stale")
    assert m._ensure_dashboard_daemon(force=True) == "restart-stale"
    assert not m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists()


def test_revive_spawn_failure_does_not_arm_the_marker(m, monkeypatch):
    """The no-flash regression: a spawn returning None is the
    LEAST structural failure in the set (fork EAGAIN under load, AV
    transiently locking the exe, a replaceable corrupt pythonw twin). The
    300s revive throttle bounds retries; one hiccup must not permanently
    disable a healthy installed daemon. This test USED to pin the opposite."""
    monkeypatch.setattr(m, "_read_config_flag", lambda k, d=None: 0)
    monkeypatch.setattr(m, "_write_config_flag", lambda k, v: None)
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: False)
    monkeypatch.setattr(m, "_normalized_platform", lambda: "Windows")
    monkeypatch.setattr(m, "spawn_detached", lambda argv, **k: None)
    monkeypatch.setattr(m, "_log_spawn_failure", lambda msg: None)
    assert m._daemon_midsession_pulse() == "revive-spawn-failed"
    assert not m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists()


def _stub_setup_daemon(m, monkeypatch, installer_result):
    monkeypatch.setattr(m, "_normalized_platform", lambda: "Darwin")
    monkeypatch.setattr(m, "_persist_dashboard_host", lambda persist=True: "127.0.0.1")
    monkeypatch.setattr(m, "_set_daemon_disabled", lambda v: None)
    monkeypatch.setattr(
        m, "_install_launchd_daemon", lambda **k: installer_result)


def test_successful_explicit_install_clears_the_marker(m, monkeypatch):
    """The ONE thing that clears it: the user re-running ``setup-daemon`` and
    that install succeeding."""
    _arm_marker(m)
    _stub_setup_daemon(m, monkeypatch, True)
    m.setup_daemon()
    assert not m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists()


def test_failed_explicit_install_does_not_clear_the_marker(m, monkeypatch):
    _arm_marker(m)
    _stub_setup_daemon(m, monkeypatch, False)
    m.setup_daemon()
    assert m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists()


def test_dry_run_install_does_not_clear_the_marker(m, monkeypatch):
    """A preview must stay side-effect-free, or the user 'previews' their way
    back into the retry loop."""
    _arm_marker(m)
    _stub_setup_daemon(m, monkeypatch, True)
    m.setup_daemon(dry_run=True)
    assert m.DAEMON_INSTALL_FAILED_BREADCRUMB.exists()


def test_marker_survives_a_fresh_session(m, monkeypatch):
    """Never self-heals on TIME or on a reload (a new Claude session). The one
    self-clearing path is a VERIFIED live daemon, stubbed dead
    here."""
    _arm_marker(m)
    marker_path = m.DAEMON_INSTALL_FAILED_BREADCRUMB
    reloaded = importlib.reload(m)
    monkeypatch.setattr(reloaded, "_daemon_snapshot_sandboxed", lambda: False)
    monkeypatch.setattr(reloaded, "_is_foreign_runtime", lambda: False)
    monkeypatch.setattr(reloaded, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(reloaded, "_read_config_flag", lambda k, d=None: False)
    monkeypatch.setattr(reloaded, "_verify_daemon_port", lambda **k: False)
    assert marker_path.exists()
    assert reloaded._ensure_dashboard_daemon(force=True) == "noop-install-failed"


def test_marker_clear_sites_are_bounded():
    """Structural guard, updated for the no-flash fix: the clear helper
    may be called ONLY from ``setup_daemon`` (explicit install success) and
    ``_ensure_dashboard_daemon`` (verified success / live-daemon disproof).
    Anything else -- a timer, a session hook, a heal that didn't verify --
    would restore the retry loop."""
    src = _measure_source()
    tree = ast.parse(src)
    owner = _enclosing_functions(tree)
    call_owners = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and ast.unparse(node.func) == "_clear_daemon_install_failed_marker"):
            call_owners.add(owner.get(node.lineno, "<module>"))
    assert call_owners == {"setup_daemon", "_ensure_dashboard_daemon"}, (
        f"unexpected clear-marker call sites: {sorted(call_owners)}"
    )


def test_marker_write_is_best_effort(m, monkeypatch):
    """An unwritable state dir must not raise out of a hook."""
    monkeypatch.setattr(
        m.SNAPSHOT_DIR.__class__, "mkdir",
        lambda self, **k: (_ for _ in ()).throw(OSError("read-only")))
    m._write_daemon_install_failed_marker("boom")  # must not raise


# ---------------------------------------------------------------------------
# 5. Real-Windows runtime assertions
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "nt", reason="Windows-only runtime assertion")
def test_no_window_constant_is_real_on_windows():
    import measure
    assert measure._NO_WINDOW == _CREATE_NO_WINDOW


@pytest.mark.skipif(os.name != "nt", reason="Windows-only runtime assertion")
def test_real_child_spawned_with_no_window_has_no_console():
    """A real console-subsystem child launched with CREATE_NO_WINDOW must report
    GetConsoleWindow() == 0. Proves the flag takes effect at the OS level, not
    just that the kwarg was passed."""
    import measure
    child = (
        "import ctypes, sys;\n"
        "sys.exit(0 if ctypes.windll.kernel32.GetConsoleWindow() == 0 else 1)\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True, timeout=30, creationflags=measure._NO_WINDOW,
    )
    assert r.returncode == 0, "child still had a console despite CREATE_NO_WINDOW"


@pytest.mark.skipif(os.name != "nt", reason="Windows-only runtime assertion")
def test_resolve_windows_pythonw_finds_a_real_twin():
    """On a real Windows runner there should be a pythonw.exe next to the
    interpreter (unless the runner is an MS Store install, which we refuse on
    purpose). Skips rather than fails in that case -- the .cmd fallback is a
    supported configuration."""
    import measure
    pyw = measure._windows_gui_python()
    if pyw is None:
        pytest.skip("no usable pythonw twin on this runner (.cmd fallback path)")
    assert Path(pyw).name.lower() == "pythonw.exe"
    assert Path(pyw).exists()
