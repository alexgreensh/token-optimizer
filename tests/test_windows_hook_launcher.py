"""Regression coverage for Windows hook command generation (#118).

Claude Code runs ``command`` hooks through Git Bash on native Windows, so
generated hook commands must be POSIX-shell safe: ``>/dev/null 2>&1`` (never
``>NUL``, which Git Bash materializes as a literal file named ``NUL`` in the
CWD) and forward-slash or single-quoted paths (never cmd.exe
``list2cmdline`` quoting). Codex is different: it spawns hooks via
``%COMSPEC% /C`` (cmd.exe), so codex_install.py's cmd syntax is correct and
pinned by the tests below.
"""

import importlib.util
import ast
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO / "skills" / "token-optimizer" / "scripts" / "codex_install.py"
MEASURE_PATH = REPO / "skills" / "token-optimizer" / "scripts" / "measure.py"

_CMD_NUL_RE = re.compile(r">\s*NUL\b")

HOOKS_JSON_TEMPLATE = (
    'for b in bash /bin/bash /usr/bin/bash /usr/local/bin/bash /opt/homebrew/bin/bash; '
    'do command -v "$b" >/dev/null 2>&1 && '
    'exec "$b" "${CLAUDE_PLUGIN_ROOT}/hooks/python-launcher.sh" '
    '"${CLAUDE_PLUGIN_ROOT}/hooks/run.py" '
    'skills/token-optimizer/scripts/measure.py ensure-health --quiet; done; exit 0'
)


def _load_measure_hook_resolver(platform):
    tree = ast.parse(MEASURE_PATH.read_text(encoding="utf-8"))
    wanted = {
        "_resolve_hook_command",
        "_windows_hook_command_is_stale",
    }
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {
        "Path": Path,
        "re": re,
        "shlex": shlex,
        "platform": type("Platform", (), {"system": staticmethod(lambda: platform)}),
        "subprocess": subprocess,
        "sys": sys,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(MEASURE_PATH), "exec"), namespace)
    return namespace


def _load_measure_hook_command():
    """Exec the module-level ``if sys.platform == "win32"`` block that assigns
    HOOK_COMMAND, simulating a Windows interpreter."""
    tree = ast.parse(MEASURE_PATH.read_text(encoding="utf-8"))
    node = None
    for candidate in tree.body:
        if not isinstance(candidate, ast.If):
            continue
        for stmt in ast.walk(candidate):
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "HOOK_COMMAND" for t in stmt.targets
            ):
                node = candidate
                break
        if node is not None:
            break
    assert node is not None, "module-level HOOK_COMMAND assignment not found"
    namespace = {
        "sys": SimpleNamespace(
            platform="win32",
            executable="C:\\Python313\\python.exe",
        ),
        "shlex": shlex,
        "subprocess": subprocess,
        "Path": Path,
        "MEASURE_PY_PATH": "C:\\Users\\Test User\\.claude\\token-optimizer\\scripts\\measure.py",
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(MEASURE_PATH), "exec"), namespace)
    return namespace["HOOK_COMMAND"]


def _load_codex_install(monkeypatch, platform):
    scripts = str(MODULE_PATH.parent)
    monkeypatch.syspath_prepend(scripts)
    spec = importlib.util.spec_from_file_location("codex_install_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.sys, "platform", platform)
    return module


def test_windows_hook_command_invokes_python_directly(monkeypatch):
    module = _load_codex_install(monkeypatch, "win32")

    command = module._hook_command("skills/token-optimizer/scripts/read_cache.py", "--quiet")

    assert "hooks/run.py" in command or "hooks\\run.py" in command
    assert "skills/token-optimizer/scripts/read_cache.py" in command
    assert "python-launcher.sh" not in command
    assert "for b in bash" not in command


def test_windows_versioned_marketplace_hook_resolves_newest_install(monkeypatch, tmp_path):
    """The native-CMD launcher must not retain a pruned version directory."""
    module = _load_codex_install(monkeypatch, "win32")
    versioned_root = tmp_path / "cache" / "market" / "token-optimizer" / "5.11.75"
    monkeypatch.setattr(module, "_repo_root", lambda: versioned_root)

    command = module._hook_command("skills/token-optimizer/scripts/read_cache.py", "--quiet")

    assert "TOKEN_OPTIMIZER_RUNTIME_ROOT" in command
    assert "powershell" in command.lower()
    assert str(versioned_root.parent) in command
    assert "hooks" in command and "run.py" in command


def _execute_cmd_runner(command: str, newest: str) -> str:
    """Execute CMD's relevant expansion stages for the generated hook command."""
    parsed = re.sub(r"%TOKEN_OPTIMIZER_RUNTIME_ROOT%", "", command)
    fallback = re.search(
        r'set "TOKEN_OPTIMIZER_RUNTIME_ROOT=([^"]+)"', command
    ).group(1)
    selected = fallback
    if "setlocal enabledelayedexpansion" in command.lower():
        selected = re.search(
            r'do @set "TOKEN_OPTIMIZER_RUNTIME_ROOT=([^"]+)\\%R"', command
        ).group(1) + "\\" + newest
        parsed = parsed.replace("!TOKEN_OPTIMIZER_RUNTIME_ROOT!", selected)
    runner = re.search(r'"([^"]*\\hooks\\run\.py)"', parsed)
    assert runner, f"runner path was not quoted and executable: {parsed}"
    return runner.group(1)


def test_windows_versioned_hook_executes_live_delayed_path_with_spaces(monkeypatch):
    """CMD expands %VAR% before a compound line executes; !VAR! is live."""
    module = _load_codex_install(monkeypatch, "win32")
    root = PureWindowsPath(
        r"C:\Users\Test User\.codex\plugins\market\token-optimizer\5.11.75"
    )
    monkeypatch.setattr(module, "_repo_root", lambda: root)

    command = module._hook_command("skills/token-optimizer/scripts/read_cache.py")
    runner = _execute_cmd_runner(command, "5.11.76")

    assert runner == (
        r"C:\Users\Test User\.codex\plugins\market\token-optimizer"
        r"\5.11.76\hooks\run.py"
    )


def test_posix_hook_command_keeps_bash_resolver(monkeypatch):
    module = _load_codex_install(monkeypatch, "linux")

    command = module._hook_command("skills/token-optimizer/scripts/read_cache.py", "--quiet")

    assert command.startswith(module._BASH_RESOLVER_PREFIX)
    assert "python-launcher.sh" in command
    assert command.endswith(module._BASH_RESOLVER_SUFFIX)


# ---------- #118: Claude Code hooks run under Git Bash on Windows ----------


def test_claude_windows_hook_command_stays_bash_safe():
    """#118: the Windows resolution must keep the Git-Bash launcher form.

    The pre-fix code rewrote it to native cmd.exe syntax (list2cmdline +
    a cmd null redirect), which under Git Bash created a literal NUL file
    and silently broke every hook.
    """
    module = _load_measure_hook_resolver("Windows")
    root = Path(r"C:\Users\Test User\.claude\token-optimizer")

    command = module["_resolve_hook_command"](HOOKS_JSON_TEMPLATE, root)

    assert _CMD_NUL_RE.search(command) is None
    assert ">/dev/null 2>&1" in command
    assert "python-launcher.sh" in command
    assert "for b in bash" in command
    # Root substituted with forward slashes only (backslashes inside bash
    # double quotes invite mangling of \t, \n, ... in user names).
    assert "C:/Users/Test User/.claude/token-optimizer" in command
    assert "\\" not in command
    assert command.endswith("; done; exit 0")


def test_claude_posix_hook_command_is_byte_for_byte_unchanged():
    module = _load_measure_hook_resolver("Linux")
    template = (
        'for b in bash /bin/bash; do command -v "$b" >/dev/null 2>&1 && '
        'exec "$b" "${CLAUDE_PLUGIN_ROOT}/hooks/python-launcher.sh" '
        '"${CLAUDE_PLUGIN_ROOT}/hooks/run.py" scripts/example.py --quiet; done; exit 0'
    )
    root = Path("/opt/token optimizer")

    assert module["_resolve_hook_command"](template, root) == template.replace(
        "${CLAUDE_PLUGIN_ROOT}", str(root)
    )


def _hook_runtime_bash():
    """The bash Claude Code actually runs hooks under on Windows is Git Bash,
    NOT WSL's C:\\Windows\\System32\\bash.exe. They differ where it matters
    here: WSL bash, invoked with a Windows cwd, routes /dev/null through its
    Windows-path translation layer and can leave a literal NUL artifact, while
    Git Bash's MSYS runtime handles /dev/null correctly. shutil.which("bash")
    often resolves the WSL launcher first on GitHub runners, so the killer
    regression must resolve Git Bash explicitly (and skip when only WSL bash
    exists — WSL is not the hook runtime being regression-tested)."""
    for c in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        "/bin/bash",
        "/usr/bin/bash",
    ):
        if Path(c).exists():
            return c
    b = shutil.which("bash")
    if b and "System32" in b:  # WSL launcher — not the hook shell
        return None
    return b


def test_claude_windows_hook_command_executes_under_bash_without_nul_file(tmp_path):
    """Killer regression for #118: run the resolved command under the bash
    Claude Code actually uses for hooks (Git Bash on Windows) in a scratch dir.
    Pre-fix this left a literal file named NUL behind."""
    bash = _hook_runtime_bash()
    if not bash:
        pytest.skip("Git Bash (the Windows hook runtime) unavailable; WSL bash is not the hook shell")
    root = tmp_path / "plugin root"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "python-launcher.sh").write_text("#!/bin/bash\nexit 0\n")
    (root / "hooks" / "run.py").write_text("import sys; sys.exit(0)\n")

    module = _load_measure_hook_resolver("Windows")
    command = module["_resolve_hook_command"](HOOKS_JSON_TEMPLATE, root)

    proc = subprocess.run(
        [bash, "-c", command],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, f"hook command failed under {bash}: {proc.stderr}"
    # Detect a literal file named NUL via the directory LISTING, not
    # Path.exists(): on Windows `NUL` is a reserved device name, so
    # `(tmp_path / "NUL").exists()` is ALWAYS True (it resolves to the null
    # device, not a file) and cannot tell whether the #118 bug fired. A real
    # literal NUL file (created by the pre-fix `>NUL` via MSYS's NT-path bypass)
    # appears as a directory entry; the device never does.
    entries = os.listdir(tmp_path)
    assert "NUL" not in entries, (
        f"literal NUL file created under {bash} (dir listing: {entries})"
    )


def test_claude_windows_hook_command_string_is_bash_parseable():
    """bash -n must parse the resolved command (syntax-level Git Bash safety)."""
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not on PATH")
    module = _load_measure_hook_resolver("Windows")
    root = Path(r"C:\Users\Test User\.claude\token-optimizer")

    command = module["_resolve_hook_command"](HOOKS_JSON_TEMPLATE, root)

    proc = subprocess.run([bash, "-n"], input=command, capture_output=True, text=True)
    assert proc.returncode == 0, f"bash failed to parse hook command: {proc.stderr}"


def test_claude_windows_hook_command_constant_is_bash_safe():
    """The SessionEnd HOOK_COMMAND written to settings.json (win32 branch)."""
    command = _load_measure_hook_command()

    assert _CMD_NUL_RE.search(command) is None
    assert command.endswith(">/dev/null 2>&1")
    assert "\\" not in command, "backslash path leaks into a bash command"
    # Space in the path must be single-quoted (POSIX), not list2cmdline-quoted.
    assert "'C:/Users/Test User/.claude/token-optimizer/scripts/measure.py'" in command
    assert "collect --quiet" in command and "dashboard --quiet" in command


def test_claude_windows_hook_command_constant_parses_under_bash():
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not on PATH")
    command = _load_measure_hook_command()

    proc = subprocess.run([bash, "-n"], input=command, capture_output=True, text=True)
    assert proc.returncode == 0, f"bash failed to parse HOOK_COMMAND: {proc.stderr}"


def test_measure_py_has_no_cmd_null_redirect_anywhere():
    """Source-grep guard: the cmd.exe null redirect must never reappear in
    measure.py. (codex_install.py is exempt: Codex spawns hooks via
    %COMSPEC% /C, so cmd syntax is correct there.)"""
    src = MEASURE_PATH.read_text(encoding="utf-8")

    assert _CMD_NUL_RE.search(src) is None, (
        "cmd.exe null redirect found in measure.py; Claude Code runs hooks "
        "under Git Bash where it creates a literal NUL file (#118)"
    )


def test_claude_windows_session_start_marks_legacy_cmd_form_for_self_heal():
    """Installs holding the pre-fix native cmd.exe form must be flagged stale
    so ensure-health replaces them with the bash launcher form."""
    module = _load_measure_hook_resolver("Windows")
    legacy_cmd_form = (
        "C:\\Python\\python.exe C:\\plugin\\hooks\\run.py"
        " script.py >" + "NUL 2>&1"  # split so the source-grep guard stays meaningful
    )
    resolved = module["_resolve_hook_command"](HOOKS_JSON_TEMPLATE, Path("C:/plugin"))

    assert module["_windows_hook_command_is_stale"](legacy_cmd_form, resolved) is True


def test_claude_windows_current_bash_launcher_is_not_stale():
    module = _load_measure_hook_resolver("Windows")
    root = Path(r"C:\Users\Test User\.claude\token-optimizer")
    current = module["_resolve_hook_command"](HOOKS_JSON_TEMPLATE, root)

    assert module["_windows_hook_command_is_stale"](current, current) is False


def test_claude_posix_does_not_refresh_current_root_launcher():
    module = _load_measure_hook_resolver("Linux")
    old = 'for b in bash; do exec "$b" "/opt/plugin/hooks/python-launcher.sh"; done; exit 0'

    assert module["_windows_hook_command_is_stale"](old, "different") is False


def _load_full_measure():
    """Full importlib load (for functions the AST resolver doesn't extract)."""
    scripts = str(MEASURE_PATH.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("measure_hookcurrent_uut", MEASURE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_legacy_nul_sessionend_hook_reads_as_not_current(monkeypatch):
    """#118 F1: the pre-fix >NUL SessionEnd command matched all three
    substrings and read as 'current', so existing broken Windows installs were
    never healed ('already up to date. Nothing to do.'). It must now read as
    NOT current on win32 so setup_hook's upgrade branch rewrites it."""
    measure = _load_full_measure()
    monkeypatch.setattr(measure.sys, "platform", "win32")
    legacy = (
        "python.exe C:/p/measure.py collect --quiet && "
        "python.exe C:/p/measure.py dashboard --quiet >NUL 2>&1"
    )
    legacy_settings = {"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": legacy}]}]}}
    assert measure._is_hook_current(legacy_settings) is False
    # The bash-safe replacement IS current (no rewrite loop).
    good = legacy.replace(">NUL 2>&1", ">/dev/null 2>&1")
    good_settings = {"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": good}]}]}}
    assert measure._is_hook_current(good_settings) is True


def test_posix_nul_sessionend_hook_is_win32_gated(monkeypatch):
    """The >NUL staleness heuristic is win32-only; POSIX never emits >NUL."""
    measure = _load_full_measure()
    monkeypatch.setattr(measure.sys, "platform", "linux")
    cmd = "python3 /p/measure.py collect --quiet && python3 /p/measure.py dashboard --quiet >NUL 2>&1"
    settings = {"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": cmd}]}]}}
    assert measure._is_hook_current(settings) is True


def test_windows_resolved_command_matches_native_root_after_normalization():
    """#118 F2 (documents WHY): _resolve_hook_command embeds a forward-slash
    root on Windows, so a raw substring test of the native-backslash root
    against the resolved command fails; normalized containment succeeds."""
    module = _load_measure_hook_resolver("Windows")
    native_root = r"C:\Users\Test User\.claude\plugins\token-optimizer"
    resolved = module["_resolve_hook_command"](HOOKS_JSON_TEMPLATE, PureWindowsPath(native_root))
    assert native_root not in resolved
    assert native_root.replace("\\", "/") in resolved.replace("\\", "/")


def test_setup_all_hooks_containment_is_separator_normalized():
    """#118 F2 (reversion guard, non-vacuous): setup_all_hooks' 'already
    present' test must normalize separators on BOTH operands. A revert to the
    raw `plugin_root_str in existing_cmd` reintroduces the perpetual
    settings.json rewrite on Windows (forward-slash resolved root never
    contains the native-backslash plugin_root_str) — this test fails on that
    revert."""
    src = MEASURE_PATH.read_text(encoding="utf-8")
    # The buggy raw predicate must NOT be the active containment test.
    assert "plugin_root_str in existing_cmd" not in src, (
        "raw (un-normalized) containment reintroduces the F2 rewrite loop"
    )
    # Both operands must be separator-normalized before the containment test.
    # (Source contains two literal backslashes: replace("\\", "/").)
    assert r'plugin_root_str.replace("\\", "/")' in src
    assert r'existing_cmd.replace("\\", "/")' in src
