"""Regression coverage for Windows hook command generation.

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
import json
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


def test_windows_versioned_hook_avoids_same_line_expansion(monkeypatch):
    """cmd.exe parses a /C command line ONCE, before anything on it runs:
    %VAR% expands to the pre-line value, and `setlocal EnableDelayedExpansion`
    only takes effect from the NEXT line, so !VAR! on the same line reaches
    python as the literal text "!TOKEN_OPTIMIZER_RUNTIME_ROOT!" (issue #180).
    The runner path must be built from the FOR variable %R inside the
    do-body, with no expansion form of TOKEN_OPTIMIZER_RUNTIME_ROOT."""
    module = _load_codex_install(monkeypatch, "win32")
    root = PureWindowsPath(
        r"C:\Users\Test User\.codex\plugins\market\token-optimizer\5.11.75"
    )
    monkeypatch.setattr(module, "_repo_root", lambda: root)

    command = module._hook_command("skills/token-optimizer/scripts/read_cache.py")

    assert "!TOKEN_OPTIMIZER_RUNTIME_ROOT!" not in command
    assert "setlocal" not in command.lower()
    assert r"%R\hooks\run.py" in command
    # Fail-open: the resolver prints the baked install directory when the
    # version scan finds nothing, so the do-body still runs the baked path.
    # The else-branch may carry debug-gated logging before the baked name.
    assert re.search(r"else \{ .* '5\.11\.75' \}", command), command


def test_legacy_markerless_windows_groups_are_replaced_on_reinstall(monkeypatch):
    """Pre-fix versioned Windows commands carry NO "token-optimizer/scripts"
    path marker: the baked paths use backslashes (..\\token-optimizer\\X.Y.Z)
    and consolidated-runner args are "hooks/<name>_runner.py", so the old
    marker-only _is_token_optimizer_group missed them entirely. Reinstall
    then kept the broken issue-#180 command AND appended the fixed one, and
    uninstall left the broken one behind. The widened marker -- the full
    generated-command signature (quoted `set "TOKEN_OPTIMIZER_RUNTIME_ROOT=`
    assignment AND a hooks\\run.py runner invocation under a token-optimizer
    path, or via the FOR variable) -- must evict them while never touching a
    foreign group."""
    module = _load_codex_install(monkeypatch, "win32")
    legacy = {
        "hooks": [{
            "type": "command",
            "command": (
                'setlocal EnableDelayedExpansion && '
                'set "TOKEN_OPTIMIZER_RUNTIME=codex" && '
                'set "TOKEN_OPTIMIZER_RUNTIME_ROOT=C:\\market\\token-optimizer\\5.13.2" && '
                "for /f \"delims=\" %R in ('powershell -NoProfile -Command Get-ChildItem') "
                'do @set "TOKEN_OPTIMIZER_RUNTIME_ROOT=C:\\market\\token-optimizer\\%R" && '
                'python.exe "!TOKEN_OPTIMIZER_RUNTIME_ROOT!\\hooks\\run.py" '
                'hooks/stop_runner.py >NUL 2>&1'
            ),
        }],
    }
    foreign = {"hooks": [{"type": "command", "command": "echo not ours"}]}
    # The guard the old marker-only check failed: no forward-slash path marker.
    assert "token-optimizer/scripts" not in json.dumps(legacy)
    assert module._is_token_optimizer_group(legacy)
    assert not module._is_token_optimizer_group(foreign)

    fixed = {"hooks": [{"type": "command", "command": "fixed"}]}
    monkeypatch.setattr(module, "_managed_hooks", lambda **kw: {"Stop": [fixed]})
    merged = module._merge_hooks({"hooks": {"Stop": [legacy, foreign]}})
    assert merged["hooks"]["Stop"] == [foreign, fixed]
    removed = module._remove_hooks({"hooks": {"Stop": [legacy, foreign]}})
    assert removed == {"hooks": {"Stop": [foreign]}}


def test_foreign_hook_referencing_runtime_root_env_is_never_touched(monkeypatch):
    """The widened marker must not become a new footgun: a user's OWN hook
    that merely contains the env var -- a bare reference
    (%TOKEN_OPTIMIZER_RUNTIME_ROOT%), a POSIX-style VAR=x prefix assignment,
    an UNQUOTED set, or even the full QUOTED `set "TOKEN_OPTIMIZER_RUNTIME_ROOT=`
    assignment our generator emits -- is not a generated Token Optimizer
    command and must survive both reinstall-merge and uninstall-remove. The
    assignment alone matched the pre-fix marker, so the quoted-set cases are
    the regression this test exists to pin."""
    module = _load_codex_install(monkeypatch, "win32")
    user_hooks = [
        {"hooks": [{"type": "command",
                    "command": "echo %TOKEN_OPTIMIZER_RUNTIME_ROOT% && python mine.py"}]},
        {"hooks": [{"type": "command",
                    "command": "TOKEN_OPTIMIZER_RUNTIME_ROOT=/x python3 mine.py"}]},
        {"hooks": [{"type": "command",
                    "command": "set TOKEN_OPTIMIZER_RUNTIME_ROOT=C:\\x && python mine.py"}]},
        # The confirmed false-positive: quoted set-assignment, no runner.
        {"hooks": [{"type": "command",
                    "command": 'set "TOKEN_OPTIMIZER_RUNTIME_ROOT=C:\\x" && python mine.py'}]},
        # Quoted assignment AND a hooks\run.py call, but under the user's own
        # directory -- not a token-optimizer path and not our FOR/delayed-var
        # invocation shape.
        {"hooks": [{"type": "command",
                    "command": (
                        'set "TOKEN_OPTIMIZER_RUNTIME_ROOT=D:\\tools\\mine" && '
                        "python \"D:\\tools\\mine\\hooks\\run.py\" mine.py"
                    )}]},
    ]
    for user_hook in user_hooks:
        assert not module._is_token_optimizer_group(user_hook), user_hook

    fixed = {"hooks": [{"type": "command", "command": "fixed"}]}
    monkeypatch.setattr(module, "_managed_hooks", lambda **kw: {"Stop": [fixed]})
    merged = module._merge_hooks({"hooks": {"Stop": list(user_hooks)}})
    assert merged["hooks"]["Stop"] == [*user_hooks, fixed]
    removed = module._remove_hooks({"hooks": {"Stop": list(user_hooks)}})
    assert removed == {"hooks": {"Stop": user_hooks}}


def test_generated_command_shape_satisfies_matcher_contract(monkeypatch, tmp_path):
    """Generator-to-matcher contract: every Windows command shape
    _hook_command can emit must be claimed by _is_token_optimizer_group, or
    reinstall/uninstall silently keeps stale copies. If the generator's
    command shape changes, this fails until the matcher's anchors are updated
    in the same commit."""
    module = _load_codex_install(monkeypatch, "win32")

    # Versioned marketplace root: the cmd resolver shape (the only shape that
    # relies on the signature anchors rather than the path marker).
    versioned_root = tmp_path / "plugin cache" / "token-optimizer" / "5.13.12"
    monkeypatch.setattr(module, "_repo_root", lambda: versioned_root)
    for script in (
        "hooks/stop_runner.py",
        "hooks/sessionstart_runner.py",
        "skills/token-optimizer/scripts/codex_hook_bridge.py",
    ):
        command = module._hook_command(script, redirect_quiet=True)
        group = {"hooks": [{"type": "command", "command": command}]}
        assert "token-optimizer/scripts" not in json.dumps(group) or script.startswith("skills/")
        assert module._is_token_optimizer_group(group), command

    # Non-versioned root: runner path is literal; claimed via the path marker
    # for marker-bearing script args (consolidated-runner args on a
    # non-token-optimizer root remain a known gap -- see PR follow-ups).
    plain_root = tmp_path / "checkout" / "token-optimizer"
    monkeypatch.setattr(module, "_repo_root", lambda: plain_root)
    command = module._hook_command("skills/token-optimizer/scripts/read_cache.py", "--quiet")
    assert module._is_token_optimizer_group(
        {"hooks": [{"type": "command", "command": command}]}
    ), command


def test_version_resolver_fallback_debug_log_is_debug_gated(monkeypatch):
    """The baked-install fallback is silent by design; the only observable
    channel is a TOKEN_OPTIMIZER_DEBUG-gated line appended to
    token-optimizer-codex-resolver.log next to the version dirs. Assert the
    generated command carries that instrumentation, inside the else-branch,
    with no cmd-hostile metacharacters in the added syntax."""
    module = _load_codex_install(monkeypatch, "win32")
    root = PureWindowsPath(
        r"C:\Users\Test User\.codex\plugins\market\token-optimizer\5.11.75"
    )
    monkeypatch.setattr(module, "_repo_root", lambda: root)

    command = module._hook_command("skills/token-optimizer/scripts/read_cache.py")

    assert "$env:TOKEN_OPTIMIZER_DEBUG" in command
    assert "token-optimizer-codex-resolver.log" in command
    # The log write must sit inside the else-branch so a healthy resolve
    # stays quiet, and the baked name must still be the branch's last word.
    assert re.search(
        r"else \{ if \(\$env:TOKEN_OPTIMIZER_DEBUG\) .* \}; '5\.11\.75' \}",
        command,
    ), command
    # for /f runs the in-clause via cmd /c: parens, redirects, %, and & in
    # the added syntax would break the command line. The single-quoted
    # -Command payload may legitimately contain | and > inside PowerShell
    # operators we already rely on, so scope the check to the new fragment.
    fragment = re.search(
        r"if \(\$env:TOKEN_OPTIMIZER_DEBUG\) \{ (.*?) \}; '", command
    ).group(1)
    assert not re.search(r"[()<>%&]", fragment), fragment


def test_version_resolver_fallback_writes_debug_log_when_enabled(monkeypatch, tmp_path):
    """Live proof (where PowerShell exists): with TOKEN_OPTIMIZER_DEBUG set,
    the fallback appends a line to the resolver log; without it, nothing is
    written. Skips where no PowerShell runtime is available."""
    pwsh = _pwsh()
    if not pwsh:
        pytest.skip("PowerShell (the Windows version-resolver runtime) unavailable")
    base = tmp_path / "plugin cache" / "token-optimizer"
    (base / "latest").mkdir(parents=True)
    argv = _generated_resolver_argv(monkeypatch, base / "5.11.75")
    # On Windows the log is a real child of the version-dirs parent; under
    # pwsh-on-POSIX the backslash separator lands in the file NAME, so match
    # by suffix in the directory listing instead of a fixed child path.
    def _resolver_logs():
        return [
            p for p in base.iterdir()
            if p.name.endswith("token-optimizer-codex-resolver.log")
        ]

    env = {**os.environ, "TOKEN_OPTIMIZER_DEBUG": "1"}
    proc = subprocess.run(
        [pwsh, *argv[1:]], capture_output=True, text=True, timeout=60, env=env
    )
    assert proc.returncode == 0, f"resolver failed: {proc.stderr}"
    assert proc.stdout.strip() == "5.11.75"
    logs = _resolver_logs()
    assert logs, "debug-enabled fallback did not write the resolver log"
    assert "5.11.75" in logs[0].read_text(encoding="utf-8")

    for p in logs:
        p.unlink()
    env_off = {k: v for k, v in os.environ.items() if k != "TOKEN_OPTIMIZER_DEBUG"}
    proc = subprocess.run(
        [pwsh, *argv[1:]], capture_output=True, text=True, timeout=60, env=env_off
    )
    assert proc.returncode == 0, f"resolver failed: {proc.stderr}"
    assert proc.stdout.strip() == "5.11.75"
    assert not _resolver_logs(), "resolver log written without TOKEN_OPTIMIZER_DEBUG"


def _generated_resolver_argv(monkeypatch, root):
    """Capture the exact argv vector the generator hands to cmd for the
    version resolver (the `powershell -NoProfile -Command ...` inside the
    for /f in-clause)."""
    module = _load_codex_install(monkeypatch, "win32")
    monkeypatch.setattr(module, "_repo_root", lambda: root)
    recorded = []
    real = subprocess.list2cmdline
    monkeypatch.setattr(
        module.subprocess,
        "list2cmdline",
        lambda argv: (recorded.append(list(argv)), real(argv))[1],
    )
    module._hook_command("skills/token-optimizer/scripts/read_cache.py")
    return next(a for a in recorded if a[0] == "powershell")


def _pwsh():
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _run_version_resolver(monkeypatch, base: Path, baked: str) -> str:
    pwsh = _pwsh()
    if not pwsh:
        pytest.skip("PowerShell (the Windows version-resolver runtime) unavailable")
    argv = _generated_resolver_argv(monkeypatch, base / baked)
    proc = subprocess.run(
        [pwsh, *argv[1:]], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"version resolver failed: {proc.stderr}"
    return proc.stdout.strip()


def test_version_resolver_picks_newest_semver(monkeypatch, tmp_path):
    """Live resolver proof: numeric [version] sort (5.11.76 > 5.11.9, which
    lexicographic order would get backwards) and non-semver siblings ignored,
    under a base path with spaces."""
    base = tmp_path / "plugin cache" / "token-optimizer"
    for version in ("5.11.9", "5.11.75", "5.11.76", "latest"):
        (base / version).mkdir(parents=True)

    assert _run_version_resolver(monkeypatch, base, "5.11.75") == "5.11.76"


def test_version_resolver_falls_back_to_baked_install(monkeypatch, tmp_path):
    """When the scan finds no semver sibling (e.g. the marketplace cache is
    unreadable), the resolver prints the baked install directory so the hook
    still runs the install it was generated from."""
    base = tmp_path / "plugin cache" / "token-optimizer"
    (base / "latest").mkdir(parents=True)

    assert _run_version_resolver(monkeypatch, base, "5.11.75") == "5.11.75"


def _make_fake_runner(version_dir: Path) -> None:
    """A stand-in hooks/run.py that records how it was invoked, in a marker
    file next to itself. Per-version markers make a stale version executing
    (or a double execution) detectable."""
    hooks = version_dir / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "run.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(__file__).with_name(\'invoked.json\').write_text(json.dumps({\n"
        "    \'root\': os.environ.get(\'TOKEN_OPTIMIZER_RUNTIME_ROOT\'),\n"
        "    \'runtime\': os.environ.get(\'TOKEN_OPTIMIZER_RUNTIME\'),\n"
        "    \'argv\': sys.argv[1:],\n"
        "    \'stdin\': sys.stdin.read(),\n"
        "}))\n",
        encoding="utf-8",
    )


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe semantics are Windows-only")
def test_windows_versioned_hook_executes_through_comspec_with_spaces(monkeypatch, tmp_path):
    """Killer regression for issue #180: execute the generated command through
    %COMSPEC% /D /C exactly as Codex does, from a versioned marketplace layout
    under a path WITH SPACES, and assert the newest version's run.py actually
    executes with TOKEN_OPTIMIZER_RUNTIME_ROOT pointing at it.

    Sibling versions 5.11.9 and 5.11.76 guard the numeric [version] sort
    (lexicographic order would pick 5.11.9 over 5.11.76), and a non-semver
    "latest" sibling must never be picked. redirect_quiet=True exercises the
    production-shaped command, trailing >NUL 2>&1 and all.
    """
    base = tmp_path / "plugin cache" / "token-optimizer"
    for version in ("5.11.9", "5.11.75", "5.11.76", "latest"):
        _make_fake_runner(base / version)

    module = _load_codex_install(monkeypatch, "win32")
    monkeypatch.setattr(module, "_repo_root", lambda: base / "5.11.75")

    command = module._hook_command(
        "skills/token-optimizer/scripts/read_cache.py", "--quiet",
        redirect_quiet=True,
    )

    # Codex hands cmd.exe the command as ONE raw /C string. A Python argv
    # LIST would go through list2cmdline (backslash-double-quote escaping),
    # which cmd.exe does not parse: it chokes on \"delims=\" before the
    # for-loop ever runs. Reproduce the production invocation exactly.
    proc = subprocess.run(
        f'{os.environ.get("COMSPEC", "cmd.exe")} /d /s /c "{command}"',
        input="{}",
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, f"hook command failed: {proc.stderr}\n{command}"
    newest = base / "5.11.76" / "hooks" / "invoked.json"
    assert newest.exists(), f"newest version's run.py did not execute: {command}"
    payload = json.loads(newest.read_text(encoding="utf-8"))
    assert payload["root"] == str(base / "5.11.76")
    assert payload["runtime"] == "codex"
    assert payload["argv"] == ["skills/token-optimizer/scripts/read_cache.py", "--quiet"]
    assert payload["stdin"] == "{}"
    for version in ("5.11.9", "5.11.75", "latest"):
        stale = base / version / "hooks" / "invoked.json"
        assert not stale.exists(), f"{version} executed instead of or before 5.11.76"


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe semantics are Windows-only")
def test_windows_versioned_hook_falls_back_to_baked_install(monkeypatch, tmp_path):
    """When the marketplace cache holds only the baked install, the generated
    command still runs it (through %COMSPEC% /D /C, spaces in path)."""
    base = tmp_path / "plugin cache" / "token-optimizer"
    _make_fake_runner(base / "5.11.75")

    module = _load_codex_install(monkeypatch, "win32")
    monkeypatch.setattr(module, "_repo_root", lambda: base / "5.11.75")

    command = module._hook_command("skills/token-optimizer/scripts/read_cache.py")

    # Raw /C string, not an argv list -- see the sibling test for why a list
    # (CRT backslash-quote escaping) cannot express this command to cmd.exe.
    proc = subprocess.run(
        f'{os.environ.get("COMSPEC", "cmd.exe")} /d /s /c "{command}"',
        input="{}",
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, f"hook command failed: {proc.stderr}\n{command}"
    payload = json.loads((base / "5.11.75" / "hooks" / "invoked.json").read_text(encoding="utf-8"))
    assert payload["root"] == str(base / "5.11.75")


def test_posix_hook_command_keeps_bash_resolver(monkeypatch):
    module = _load_codex_install(monkeypatch, "linux")

    command = module._hook_command("skills/token-optimizer/scripts/read_cache.py", "--quiet")

    assert command.startswith(module._BASH_RESOLVER_PREFIX)
    assert "python-launcher.sh" in command
    assert command.endswith(module._BASH_RESOLVER_SUFFIX)


# ---------- Claude Code hooks run under Git Bash on Windows ----------


def test_claude_windows_hook_command_stays_bash_safe():
    """The Windows resolution must keep the Git-Bash launcher form.

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
    """Killer regression: run the resolved command under the bash
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
    # device, not a file) and cannot tell whether the bug fired. A real
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
    assert "session-end-flush --trigger end" in command
    assert "collect --quiet" not in command and "dashboard --quiet" not in command


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
        "under Git Bash where it creates a literal NUL file"
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
    """The pre-fix >NUL SessionEnd command matched all three
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
    # The collect/dashboard shape is never current, even with bash-safe redirect.
    good_fossil = legacy.replace(">NUL 2>&1", ">/dev/null 2>&1")
    good_fossil_settings = {"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": good_fossil}]}]}}
    assert measure._is_hook_current(good_fossil_settings) is False
    flush = "python.exe C:/p/measure.py session-end-flush --trigger end >/dev/null 2>&1"
    flush_settings = {"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": flush}]}]}}
    assert measure._is_hook_current(flush_settings) is True


def test_posix_nul_sessionend_hook_is_win32_gated(monkeypatch):
    """The >NUL staleness heuristic is win32-only; POSIX never emits >NUL."""
    measure = _load_full_measure()
    monkeypatch.setattr(measure.sys, "platform", "linux")
    fossil = "python3 /p/measure.py collect --quiet && python3 /p/measure.py dashboard --quiet >NUL 2>&1"
    fossil_settings = {"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": fossil}]}]}}
    assert measure._is_hook_current(fossil_settings) is False
    flush = "python3 /p/measure.py session-end-flush --trigger end >NUL 2>&1"
    flush_settings = {"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": flush}]}]}}
    assert measure._is_hook_current(flush_settings) is True


def test_windows_resolved_command_matches_native_root_after_normalization():
    """_resolve_hook_command embeds a forward-slash
    root on Windows, so a raw substring test of the native-backslash root
    against the resolved command fails; normalized containment succeeds."""
    module = _load_measure_hook_resolver("Windows")
    native_root = r"C:\Users\Test User\.claude\plugins\token-optimizer"
    resolved = module["_resolve_hook_command"](HOOKS_JSON_TEMPLATE, PureWindowsPath(native_root))
    assert native_root not in resolved
    assert native_root.replace("\\", "/") in resolved.replace("\\", "/")


def test_setup_all_hooks_containment_is_separator_normalized():
    """setup_all_hooks' 'already
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
