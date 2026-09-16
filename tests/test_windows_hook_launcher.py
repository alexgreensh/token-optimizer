"""Regression coverage for Windows hook command generation.

Claude Code runs ``command`` hooks through Git Bash on native Windows, so
those commands must be POSIX-shell safe. Codex is different: it spawns hooks
via ``%COMSPEC% /C`` (cmd.exe), so codex_install.py uses native Windows
quoting and an auditable Python launcher file at a stable path next to the
versioned marketplace installs.
"""

import importlib.util
import ast
import base64
import inspect
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
LAUNCHER_SOURCE = REPO / "hooks" / "windows-launcher.py"

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
    wanted = {"_resolve_hook_command", "_windows_hook_command_is_stale"}
    nodes = [node for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {
        "Path": Path, "re": re, "shlex": shlex, "subprocess": subprocess,
        "sys": sys,
        "platform": type("Platform", (), {"system": staticmethod(lambda: platform)}),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(MEASURE_PATH), "exec"), namespace)
    return namespace


def _load_measure_hook_command():
    """Exec the module-level win32 HOOK_COMMAND assignment."""
    tree = ast.parse(MEASURE_PATH.read_text(encoding="utf-8"))
    node = None
    for candidate in tree.body:
        if not isinstance(candidate, ast.If):
            continue
        if any(isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "HOOK_COMMAND"
                for t in stmt.targets) for stmt in ast.walk(candidate)):
            node = candidate
            break
    assert node is not None
    namespace = {
        "sys": SimpleNamespace(platform="win32", executable=r"C:\Python313\python.exe"),
        "shlex": shlex, "subprocess": subprocess, "Path": Path,
        "MEASURE_PY_PATH": r"C:\Users\Test User\.claude\token-optimizer\scripts\measure.py",
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(MEASURE_PATH), "exec"), namespace)
    return namespace["HOOK_COMMAND"]


def _load_codex_install(monkeypatch, platform):
    monkeypatch.syspath_prepend(str(MODULE_PATH.parent))
    spec = importlib.util.spec_from_file_location("codex_install_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.sys, "platform", platform)
    return module


def _make_fake_runner(version_dir: Path) -> None:
    hooks = version_dir / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "run.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(__file__).with_name('invoked.json').write_text(json.dumps({\n"
        "    'root': os.environ.get('TOKEN_OPTIMIZER_RUNTIME_ROOT'),\n"
        "    'runtime': os.environ.get('TOKEN_OPTIMIZER_RUNTIME'),\n"
        "    'extra': os.environ.get('TO_TEST'),\n"
        "    'argv': sys.argv[1:],\n"
        "    'stdin': sys.stdin.read(),\n"
        "}))\n", encoding="utf-8")


def _install_test_launcher(base: Path) -> Path:
    target = base / "windows-launcher.py"
    target.write_bytes(LAUNCHER_SOURCE.read_bytes())
    return target


def test_windows_hook_command_invokes_stable_plain_launcher(monkeypatch, tmp_path):
    module = _load_codex_install(monkeypatch, "win32")
    root = tmp_path / "market" / "token-optimizer" / "5.13.14"
    monkeypatch.setattr(module, "_repo_root", lambda: root)

    command = module._hook_command(
        "skills/token-optimizer/scripts/read_cache.py", "--quiet")

    assert str(root.parent / "windows-launcher.py") in command
    assert str(root) in command
    assert "hooks/run.py" not in command
    assert "python-launcher.sh" not in command
    assert " -c " not in command
    assert "b64decode" not in command
    assert "base64" not in command
    assert module._LAUNCHER_MARKER in command
    assert module._is_token_optimizer_group({"hooks": [{"command": command}]})


def test_installer_generator_source_cannot_regress_to_encoded_exec(monkeypatch, tmp_path):
    """AV-safe source-string guard: codex_install's command generator must
    neither contain nor emit the encoded ``-c`` loader shape. Legacy decode
    support elsewhere in the module is intentionally retained so existing
    commands remain auditable and recognizable during upgrade."""
    module = _load_codex_install(monkeypatch, "win32")
    source = inspect.getsource(module._windows_launcher_command).lower()
    compact = re.sub(r"\s+", "", source)
    forbidden = ("b64encode", "b64decode", "__import__('base64')", 'exec(')
    for pattern in forbidden:
        assert pattern not in compact

    root = tmp_path / "plugin space" / "token-optimizer" / "5.13.14"
    for script, args, env in (
        ("hooks/stop_runner.py", [], {}),
        ("hooks/posttooluse_runner.py", ["--quiet"],
         {"TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT": "1"}),
    ):
        command = module._windows_launcher_command(root, script, args, env)
        low = command.lower()
        assert " -c " not in low
        assert "base64" not in low
        assert "b64decode" not in low
        assert "exec(" not in low
        assert str(root.parent / "windows-launcher.py") in command


def test_plain_launcher_source_is_auditable_and_has_no_encoded_exec():
    source = LAUNCHER_SOURCE.read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", source.lower())
    assert "b64decode" not in compact
    assert "b64encode" not in compact
    assert "__import__('base64')" not in compact
    assert "exec(" not in compact
    assert "runpy.run_path" in source
    assert "root.parent.iterdir" in source


def test_launcher_signature_normalizes_version_only(monkeypatch, tmp_path):
    module = _load_codex_install(monkeypatch, "win32")
    base = tmp_path / "plugin space" / "token-optimizer"
    cmd_a = module._windows_launcher_command(base / "5.9.0", "hooks/run.py", ["a"], {"K": "1"})
    cmd_b = module._windows_launcher_command(base / "5.10.0", "hooks/run.py", ["a"], {"K": "1"})
    assert cmd_a != cmd_b
    assert module._launcher_signature(cmd_a) == module._launcher_signature(cmd_b)
    for other in (
        module._windows_launcher_command(base / "5.9.0", "hooks/run.py", ["a"], {"K": "2"}),
        module._windows_launcher_command(base / "5.9.0", "hooks/other.py", ["a"], {"K": "1"}),
        module._windows_launcher_command(tmp_path / "elsewhere" / "5.9.0", "hooks/run.py", ["a"], {"K": "1"}),
        cmd_a + " & echo tampered",
    ):
        assert module._launcher_signature(other) != module._launcher_signature(cmd_a)


def test_legacy_base64_launcher_is_recognized_decodable_and_replaced(monkeypatch, tmp_path):
    """Upgrade compatibility: the retired command shape remains owned and
    auditable, but never signature-equivalent to the new launcher-file shape,
    so install replaces it once (the trust review needed to ship #183)."""
    module = _load_codex_install(monkeypatch, "win32")
    code = "print('legacy audit')\n"
    blob = base64.b64encode(code.encode()).decode()
    legacy = (
        f'{sys.executable} -c "'
        + "exec(__import__('base64').b64decode('" + blob + "'))"
        + f'" {module._LAUNCHER_MARKER}'
    )
    current = module._windows_launcher_command(
        tmp_path / "token-optimizer" / "5.13.14", "hooks/stop_runner.py", [], {})
    assert module.decode_launcher_command(legacy) == code
    assert module._is_token_optimizer_group({"hooks": [{"command": legacy}]})
    assert module._launcher_signature(legacy) == legacy
    assert module._launcher_signature(legacy) != module._launcher_signature(current)
    assert module.decode_launcher_command(current) is None


def test_launcher_install_is_atomic_idempotent_and_stable(monkeypatch, tmp_path):
    module = _load_codex_install(monkeypatch, "win32")
    root = tmp_path / "plugin space" / "token-optimizer" / "5.13.14"
    root.mkdir(parents=True)
    monkeypatch.setattr(module, "_repo_root", lambda: root)
    source = root / "hooks" / "windows-launcher.py"
    source.parent.mkdir()
    source.write_bytes(LAUNCHER_SOURCE.read_bytes())

    first = module._install_windows_launcher(root)
    target = root.parent / "windows-launcher.py"
    before = target.stat().st_mtime_ns
    second = module._install_windows_launcher(root)

    assert first.startswith("installed:")
    assert second.startswith("already current:")
    assert target.read_bytes() == LAUNCHER_SOURCE.read_bytes()
    assert target.stat().st_mtime_ns == before
    # No temp residue of any name next to the installed launcher.
    assert list(root.parent.glob("windows-launcher.py.*")) == []
    # Stable across versions: every version dir under the same parent maps
    # to the same launcher path, so an upgrade never orphans the command.
    assert module._windows_launcher_install_path(root.parent / "9.9.9") == target


def test_launcher_install_cleans_unique_temp_on_replace_failure(monkeypatch, tmp_path):
    """Failure path: os.replace denied (AV lock, ACL race) must surface the
    loud ValueError, leave NO target behind, and leave no temp file -- the
    temp is unique per attempt (no fixed-name contention between concurrent
    installers) and always cleaned up."""
    module = _load_codex_install(monkeypatch, "win32")
    root = tmp_path / "plugin" / "token-optimizer" / "5.13.14"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "windows-launcher.py").write_bytes(b"new launcher")
    monkeypatch.setattr(module, "_repo_root", lambda: root)

    def denied(src, dst):
        raise PermissionError("locked")

    monkeypatch.setattr(module.os, "replace", denied)
    with pytest.raises(ValueError, match="cannot install Windows launcher"):
        module._install_windows_launcher(root)

    target = root.parent / "windows-launcher.py"
    assert not target.exists()
    assert list(root.parent.glob("windows-launcher.py.*")) == []
    assert sorted(p.name for p in root.parent.iterdir()) == ["5.13.14"]


def test_launcher_install_temps_are_unique_per_attempt(monkeypatch, tmp_path):
    """Concurrent installers must not contend on one fixed temp name: the
    temp is a fresh unique sibling per call, in the target's own directory
    (same filesystem, so os.replace stays atomic)."""
    module = _load_codex_install(monkeypatch, "win32")
    root = tmp_path / "plugin" / "token-optimizer" / "5.13.14"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "windows-launcher.py").write_bytes(b"launcher v1")
    monkeypatch.setattr(module, "_repo_root", lambda: root)
    names = []
    real_replace = module.os.replace

    def recording_replace(src, dst):
        names.append(str(src))
        return real_replace(src, dst)

    monkeypatch.setattr(module.os, "replace", recording_replace)
    module._install_windows_launcher(root)
    (root / "hooks" / "windows-launcher.py").write_bytes(b"launcher v2")
    module._install_windows_launcher(root)

    assert len(names) == 2 and names[0] != names[1]
    for name in names:
        assert Path(name).parent == root.parent
        assert Path(name).name.startswith("windows-launcher.py.")


def test_install_writes_launcher_before_hooks_config(monkeypatch, tmp_path):
    module = _load_codex_install(monkeypatch, "win32")
    root = tmp_path / "plugin" / "token-optimizer" / "5.13.14"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "windows-launcher.py").write_bytes(LAUNCHER_SOURCE.read_bytes())
    monkeypatch.setattr(module, "_repo_root", lambda: root)
    monkeypatch.setattr(module.codex_compact_prompt, "install", lambda **kw: "ok")
    project = tmp_path / "proj"
    project.mkdir()

    path, action, details = module.install(project, is_global=False)

    assert action == "installed"
    assert path == project / ".codex" / "hooks.json"
    assert path.exists()
    assert (root.parent / "windows-launcher.py").exists()
    assert details["windows_launcher"].startswith("installed:")
    commands = [h["command"] for groups in json.loads(path.read_text())["hooks"].values()
                for group in groups for h in group["hooks"]]
    assert commands
    assert all(str(root.parent / "windows-launcher.py") in c for c in commands)


def test_windows_version_resolver_picks_newest_with_stdio_argv_env(monkeypatch, tmp_path):
    module = _load_codex_install(monkeypatch, "win32")
    base = tmp_path / "plugin space & (test) ! apostrophe'" / "token-optimizer"
    for version in ("5.11.9", "5.11.75", "5.11.76", "latest"):
        _make_fake_runner(base / version)
    launcher = _install_test_launcher(base)
    argument = 'space & pipe| quote" percent% bang!'

    proc = subprocess.run(
        [sys.executable, str(launcher), "--baked-root", str(base / "5.11.75"),
         "--env", f"TO_TEST={argument}", "--", "hooks/test.py", argument,
         module._LAUNCHER_MARKER],
        input='{"test": true}', capture_output=True, text=True, timeout=30)

    assert proc.returncode == 0, proc.stderr
    payload = json.loads((base / "5.11.76" / "hooks" / "invoked.json").read_text())
    assert payload == {
        "root": str(base / "5.11.76"), "runtime": "codex", "extra": argument,
        "argv": ["hooks/test.py", argument], "stdin": '{"test": true}',
    }
    for stale in ("5.11.9", "5.11.75", "latest"):
        assert not (base / stale / "hooks" / "invoked.json").exists()


def test_windows_version_resolver_falls_back_and_logs_only_under_debug(monkeypatch, tmp_path):
    module = _load_codex_install(monkeypatch, "win32")
    base = tmp_path / "plugin cache" / "token-optimizer"
    _make_fake_runner(base / "5.11.75")
    launcher = _install_test_launcher(base)
    log = base / "token-optimizer-codex-resolver.log"
    argv = [sys.executable, str(launcher), "--baked-root", str(base / "5.11.75"),
            "--", "hooks/test.py", module._LAUNCHER_MARKER]

    env = {k: v for k, v in os.environ.items() if k != "TOKEN_OPTIMIZER_DEBUG"}
    proc = subprocess.run(argv, input="", capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert not log.exists()
    (base / "5.11.75" / "hooks" / "invoked.json").unlink()
    proc = subprocess.run(argv, input="", capture_output=True, text=True,
                          env={**env, "TOKEN_OPTIMIZER_DEBUG": "1"}, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert str(base / "5.11.75") in log.read_text()


def test_legacy_markerless_windows_groups_are_replaced_on_reinstall(monkeypatch):
    module = _load_codex_install(monkeypatch, "win32")
    legacy = {"hooks": [{"command":
        'set "TOKEN_OPTIMIZER_RUNTIME_ROOT=C:\\old\\token-optimizer\\5.13.2" && '
        'python "C:\\old\\token-optimizer\\%R\\hooks\\run.py" hooks/stop_runner.py'}]}
    foreign = {"hooks": [{"command": "echo not ours"}]}
    assert module._is_token_optimizer_group(legacy)
    assert not module._is_token_optimizer_group(foreign)
    fixed = {"hooks": [{"command": "fixed"}]}
    monkeypatch.setattr(module, "_managed_hooks", lambda **kw: {"Stop": [fixed]})
    merged = module._merge_hooks({"hooks": {"Stop": [legacy, foreign]}})
    assert merged["hooks"]["Stop"] == [foreign, fixed]


def test_foreign_hook_referencing_runtime_root_is_never_touched(monkeypatch):
    module = _load_codex_install(monkeypatch, "win32")
    foreign = {"hooks": [{"command":
        'set "TOKEN_OPTIMIZER_RUNTIME_ROOT=C:\\mine" && python mine.py'}]}
    assert not module._is_token_optimizer_group(foreign)
    fixed = {"hooks": [{"command": "fixed"}]}
    monkeypatch.setattr(module, "_managed_hooks", lambda **kw: {"Stop": [fixed]})
    assert module._merge_hooks({"hooks": {"Stop": [foreign]}})["hooks"]["Stop"] == [foreign, fixed]
    assert module._remove_hooks({"hooks": {"Stop": [foreign]}}) == {"hooks": {"Stop": [foreign]}}


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe semantics are Windows-only")
def test_windows_versioned_hook_executes_through_comspec(monkeypatch, tmp_path):
    """Windows execution proof: run the emitted raw /C command under the
    actual cmd.exe, with path/argv/env metacharacters and an upgrade."""
    module = _load_codex_install(monkeypatch, "win32")
    base = tmp_path / "plugin space & (test) ! apostrophe'" / "token-optimizer"
    for version in ("5.11.75", "5.11.76"):
        _make_fake_runner(base / version)
    _install_test_launcher(base)
    monkeypatch.setattr(module, "_repo_root", lambda: base / "5.11.75")
    # Metacharacters that legitimately occur in a real install path / hook arg
    # and MUST survive %COMSPEC% /C. All are literal inside cmd's double quotes.
    # Excluded on purpose (documented, production-impossible under this command
    # shape): an embedded double-quote (illegal in a Windows path, and cmd has
    # no \"-escape so it desyncs quote parity) and a percent (cmd expands %..%
    # even inside quotes). The generated argv/env values are fixed and quote-
    # free, so neither can appear in practice -- see _windows_launcher_command's
    # compatibility notes.
    argument = "space & pipe| paren() bang! caret^ semi;"
    command = module._hook_command("hooks/test.py", argument,
                                   extra_env={"TO_TEST": argument})
    proc = subprocess.run(
        f'{os.environ.get("COMSPEC", "cmd.exe")} /d /s /c "{command}"',
        input='{"test": true}', capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"hook command failed: {proc.stderr}\n{command}"
    payload = json.loads((base / "5.11.76" / "hooks" / "invoked.json").read_text())
    assert payload["root"] == str(base / "5.11.76")
    assert payload["runtime"] == "codex"
    assert payload["extra"] == argument
    assert payload["argv"] == ["hooks/test.py", argument]
    assert payload["stdin"] == '{"test": true}'


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe semantics are Windows-only")
def test_windows_hook_executes_with_spaceless_metachar_path(monkeypatch, tmp_path):
    """Regression: an install path carrying a cmd metacharacter (& ( ) ^) but NO
    space must still run. list2cmdline only wraps tokens that contain whitespace,
    so a space-free metachar path was emitted bare and cmd parsed the `&` as a
    command separator ('is not recognized as an internal or external command').
    _cmd_quote force-quotes every value token; cmd treats these as literal inside
    quotes. Distinct from the spacey-path proof above, which quotes by accident of
    the space."""
    module = _load_codex_install(monkeypatch, "win32")
    base = tmp_path / "plugin&tools(x)^y" / "token-optimizer"  # legal, no spaces
    for version in ("5.11.75", "5.11.76"):
        _make_fake_runner(base / version)
    _install_test_launcher(base)
    monkeypatch.setattr(module, "_repo_root", lambda: base / "5.11.75")
    command = module._hook_command("hooks/test.py", "--flag")
    proc = subprocess.run(
        f'{os.environ.get("COMSPEC", "cmd.exe")} /d /s /c "{command}"',
        input='{"ok": true}', capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"hook command failed: {proc.stderr}\n{command}"
    payload = json.loads((base / "5.11.76" / "hooks" / "invoked.json").read_text())
    assert payload["root"] == str(base / "5.11.76")
    assert payload["argv"] == ["hooks/test.py", "--flag"]


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
