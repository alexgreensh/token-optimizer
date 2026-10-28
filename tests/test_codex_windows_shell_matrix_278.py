"""Issue #278: the generated Codex hook command must mean the same thing in
every shell Codex may run it in, for tricky Windows install paths.

Codex runs a hook with ``cmd.exe /C "<command>"`` (COMSPEC fallback) or with
its configured session shell, normally ``powershell``/``pwsh -NoProfile
-Command <command>``. tests/_win_shells.py models how each shell turns the
command into the argv python.exe receives. For every path in the matrix the
argv must lead to the right interpreter, launcher, install dir, script and
arguments in cmd.exe (with and without delayed expansion), Windows PowerShell
5.1 and PowerShell 7.

Paths with ``$``, a backtick, ``%`` or ``!`` used to fall back to a cmd-only
command that exits 1 in PowerShell. They now go through 8.3 short names or a
plain ``python -c`` bootstrap that reads the folder from the environment.
"""

from __future__ import annotations

import importlib.util
import json
import ntpath
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path, PureWindowsPath

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _win_shells as shells  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
LAUNCHER_SOURCE = REPO / "hooks" / "windows-launcher.py"


def _load_ci(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("codex_install_matrix_278", SCRIPTS / "codex_install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.sys, "platform", "win32")
    return module


def _load_launcher():
    spec = importlib.util.spec_from_file_location("windows_launcher_matrix_278", LAUNCHER_SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LAUNCHER = _load_launcher()


# ---- 8.3 short-name model ---------------------------------------------------

_83_CHARS = r"A-Za-z0-9_$%'`!(){}^#&@~\-"
_83_RE = re.compile(rf"^[{_83_CHARS}]{{1,8}}(\.[{_83_CHARS}]{{1,3}})?$")


def _short_component(name: str) -> str:
    """What NTFS generates: a name already in 8.3 form keeps itself, else
    spaces and extra dots go, other illegal or non-ASCII characters become
    ``_``, and the base is cut to 6 characters plus ``~1``. ``$ % ! `` and the
    backtick are LEGAL 8.3 characters, so they survive."""
    if _83_RE.match(name):
        return name
    base, dot, ext = name.rpartition(".")
    if not dot:
        base, ext = name, ""

    def clean(s):
        s = s.replace(" ", "").replace(".", "")
        return "".join(c if re.match(rf"[{_83_CHARS}]", c) and ord(c) < 128 else "_" for c in s).upper()

    short = clean(base)[:6] + "~1"
    ext = clean(ext)[:3]
    return f"{short}.{ext}" if ext else short


class ShortNames:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.long_of: dict[str, str] = {}

    def __call__(self, path: str):
        if not self.enabled:
            return None
        drive, rest = ntpath.splitdrive(path)
        parts = [p for p in rest.split("\\") if p]
        short = drive + "\\" + "\\".join(_short_component(p) for p in parts)
        self.long_of[short] = path
        return short

    def expand(self, path: str) -> str:
        for short in sorted(self.long_of, key=len, reverse=True):
            if path == short or path.startswith(short + "\\"):
                return self.long_of[short] + path[len(short):]
        return path


# ---- what python.exe + the launcher do with an argv ---------------------------

def _bootstrap_target(code: str, env: dict[str, str]) -> str:
    """Run the ``-c`` bootstrap with a fake os/runpy and return the path it
    asks runpy to run (never executes the launcher)."""
    seen = {}
    fake_os = types.SimpleNamespace(environ=dict(env), path=ntpath)
    fake_runpy = types.SimpleNamespace(run_path=lambda p, run_name=None: seen.setdefault("path", p))

    def fake_import(name, *a, **k):
        return {"os": fake_os, "runpy": fake_runpy}[name]

    exec(code, {"__builtins__": {"__import__": fake_import}})
    return seen["path"]


def _resolve(argv: list[str], env: dict[str, str], names: ShortNames) -> dict:
    exe = names.expand(argv[0])
    if argv[1] == "-c":
        launcher = _bootstrap_target(argv[2], env)
        rest = argv[3:]
    else:
        launcher = names.expand(argv[1])
        rest = argv[2:]
    baked, env_pairs, script, args = LAUNCHER._parse(rest)
    if not ntpath.isabs(baked):
        baked = ntpath.join(ntpath.dirname(launcher), baked)
    parent, leaf = ntpath.split(baked)
    return {"exe": exe, "launcher": launcher, "baked": ntpath.join(names.expand(parent), leaf),
            "env": env_pairs, "script": script, "args": args}


# ---- matrix -----------------------------------------------------------------

USERS = [
    r"C:\Users\alex",
    r"C:\Users\Jo Smith",
    "C:\\Users\\Jos\u00e9 M\u00fcller",
    "C:\\Users\\\u5c71\u7530",
    r"C:\Users\O'Brien",
    r"C:\Users\R&D (Lab)",
    r"C:\Users\Jo$h",
    r"C:\Users\Jo $mith (R&D)",
    r"C:\Users\100%Sure",
    r"C:\Users\a%PATH%b",
    r"C:\Users\Bang!",
    r"C:\Users\back`tick",
    "C:\\Users\\Smart\u2019Quote",
]

_SAFE_PY = r"C:\Python315\python.exe"
_SPACED_PY = r"C:\Program Files\Python313\python.exe"


def _exes(user):
    return [_SAFE_PY, _SPACED_PY, user + r"\AppData\Local\Programs\Python\Python313\python.exe"]


def _env_for(user):
    return {
        "USERPROFILE": user,
        "LOCALAPPDATA": user + r"\AppData\Local",
        "APPDATA": user + r"\AppData\Roaming",
        "PATH": r"C:\Windows\system32",
        "Sure": "EXPANDED",
        "tick": "EXPANDED",
        "SystemRoot": r"C:\Windows",
    }


def _layout(user):
    base = user + r"\.codex\plugins\cache\token-optimizer\token-optimizer"
    return PureWindowsPath(base + r"\5.13.59"), base + r"\windows-launcher.py"


def _neutral_exe_exists(exe: str, names: ShortNames) -> bool:
    safe = re.compile(r"^[A-Za-z0-9_.:\\/+~-]+$")
    if safe.match(exe):
        return True
    short = names(exe) if names.enabled else None
    return bool(short and safe.match(short))


def _generate(monkeypatch, ci, user, exe, names):
    env = _env_for(user)
    for key in ("USERPROFILE", "LOCALAPPDATA", "APPDATA"):
        monkeypatch.setenv(key, env[key])
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(ci.sys, "executable", exe)
    monkeypatch.setattr(ci, "_short_path", names)
    root, launcher = _layout(user)
    cmd = ci._windows_launcher_command(
        root, "hooks/post_tool_use_runner.py", ["--quiet"],
        {"TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT": "1"}, silent=True)
    return cmd, env, root, launcher


@pytest.mark.parametrize("short_names", [True, False], ids=["8dot3-on", "8dot3-off"])
@pytest.mark.parametrize("user", USERS)
def test_generated_command_means_the_same_in_every_codex_shell(monkeypatch, user, short_names):
    ci = _load_ci(monkeypatch)
    for exe in _exes(user):
        names = ShortNames(short_names)
        cmd, env, root, launcher = _generate(monkeypatch, ci, user, exe, names)
        if not _neutral_exe_exists(exe, ShortNames(short_names)):
            # Only an interpreter path with no PowerShell-safe spelling may
            # fall back, and the doctor must then say exactly what to do.
            assert not ci.is_shell_neutral_command(cmd), cmd
            advice = ci.explain_cmd_only_command(cmd)
            assert advice and "Install Python in a plain folder" in advice, advice
            continue
        assert ci.is_shell_neutral_command(cmd), (user, exe, cmd)
        for shell in shells.SHELLS:
            try:
                argv = shells.run_in(shell, cmd, env)
            except shells.ShellError as exc:  # pragma: no cover - the failure message
                pytest.fail(f"{shell} rejects the command for {user!r} / {exe!r}: {exc}\n{cmd}")
            got = _resolve(argv, env, names)
            assert got == {
                "exe": exe,
                "launcher": launcher,
                "baked": str(root),
                "env": ["TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT=1", "TOKEN_OPTIMIZER_HOOK_SILENT=1"],
                "script": "hooks/post_tool_use_runner.py",
                "args": ["--quiet"],
            }, (shell, user, exe, cmd)


@pytest.mark.parametrize("user", [u for u in USERS if any(c in u for c in "$`%!\u2019")])
def test_expanding_characters_no_longer_force_the_cmd_only_form(monkeypatch, user):
    """The case PR #280 left behind: a plain interpreter with a launcher under
    a user folder holding $ ` % ! must still get a PowerShell-safe command,
    even with 8.3 names turned off."""
    ci = _load_ci(monkeypatch)
    cmd, _, _, _ = _generate(monkeypatch, ci, user, _SAFE_PY, ShortNames(False))
    assert ci.is_shell_neutral_command(cmd), cmd
    assert " -c " in cmd and "os.environ['USERPROFILE']" in cmd
    assert '--baked-root "5.13.59"' in cmd


def test_short_names_are_preferred_over_the_bootstrap(monkeypatch):
    ci = _load_ci(monkeypatch)
    cmd, _, _, _ = _generate(monkeypatch, ci, "C:\\Users\\Jos\u00e9 100%", _SAFE_PY, ShortNames(True))
    assert " -c " not in cmd
    assert '"C:\\Users\\JOS_10~1\\' in cmd and "%" not in cmd


def test_bootstrap_is_plain_source_with_no_encoded_payload(monkeypatch):
    """Issue #183 stays fixed: the bootstrap is readable code, no base64."""
    ci = _load_ci(monkeypatch)
    cmd, _, _, _ = _generate(monkeypatch, ci, r"C:\Users\Jo$h", _SAFE_PY, ShortNames(False))
    assert "b64" not in cmd and "exec(" not in cmd and "decode" not in cmd
    assert ci._LAUNCHER_B64_RE.search(cmd) is None
    assert ci.decode_launcher_command(cmd) is None


def test_bootstrap_folder_outside_every_base_falls_back_with_exact_advice(monkeypatch):
    ci = _load_ci(monkeypatch)
    names = ShortNames(False)
    monkeypatch.setattr(ci, "_short_path", names)
    monkeypatch.setattr(ci.sys, "executable", _SAFE_PY)
    for key in ("USERPROFILE", "LOCALAPPDATA", "APPDATA", "CODEX_HOME"):
        monkeypatch.delenv(key, raising=False)
    root = PureWindowsPath(r"D:\my$tools\token-optimizer\5.13.59")
    cmd = ci._windows_launcher_command(root, "hooks/stop_runner.py", [], {})
    assert not ci.is_shell_neutral_command(cmd)
    advice = ci.explain_cmd_only_command(cmd)
    assert "CODEX_HOME" in advice and "plain folder" in advice
    # Once CODEX_HOME points at the folder, the bootstrap covers it.
    monkeypatch.setenv("CODEX_HOME", r"D:\my$tools")
    assert ci.is_shell_neutral_command(ci._windows_launcher_command(root, "hooks/stop_runner.py", [], {}))


def test_cmd_only_fallback_still_runs_under_cmd(monkeypatch):
    """The fallback keeps working where it can: under cmd.exe."""
    ci = _load_ci(monkeypatch)
    user = r"C:\Users\Jo$h"
    exe = user + r"\AppData\Local\Programs\Python\Python313\python.exe"
    cmd, env, root, launcher = _generate(monkeypatch, ci, user, exe, ShortNames(False))
    assert cmd.startswith(ci._WIN_UNQUOTED_PREFIX)
    first, _, second = cmd.partition(" && ")
    assert first == 'set "TOKEN_OPTIMIZER_RUNTIME=codex"'
    argv = shells.cmd_run(second.removesuffix(" >NUL 2>&1"), env)
    assert argv[:2] == [exe, launcher]
    with pytest.raises(shells.ShellError):
        shells.powershell_run(cmd, "5.1")


def test_heal_uses_short_names_for_old_commands_with_expanding_characters(monkeypatch):
    ci = _load_ci(monkeypatch)
    names = ShortNames(True)
    monkeypatch.setattr(ci, "_short_path", names)
    m = ci._LAUNCHER_MARKER
    old = (r'set "TOKEN_OPTIMIZER_RUNTIME=codex" && "C:\Python315\python.exe" '
           r'"C:\Users\Jo Smith 100%\.codex\t\windows-launcher.py" '
           r'--baked-root "C:\Users\Jo Smith 100%\.codex\t\5.13.57" -- "hooks/stop_runner.py" '
           rf'"{m}" >NUL 2>&1')
    healed = ci.neutralize_launcher_command(old)
    assert healed and ci.is_shell_neutral_command(healed)
    assert "%" not in healed and " -c " not in healed
    env = _env_for(r"C:\Users\Jo Smith 100%")
    for shell in shells.SHELLS:
        got = _resolve(shells.run_in(shell, healed, env), env, names)
        assert got["launcher"] == r"C:\Users\Jo Smith 100%\.codex\t\windows-launcher.py"
        assert got["baked"] == r"C:\Users\Jo Smith 100%\.codex\t\5.13.57"
        assert got["env"] == ["TOKEN_OPTIMIZER_HOOK_SILENT=1"]


def test_heal_never_writes_the_bootstrap_form(monkeypatch):
    """The bootstrap needs this release's launcher, which only codex-install
    copies; the text-only heal must leave such commands to the #276 repair."""
    ci = _load_ci(monkeypatch)
    monkeypatch.setattr(ci, "_short_path", ShortNames(False))
    monkeypatch.setenv("USERPROFILE", r"C:\Users\Jo$h")
    m = ci._LAUNCHER_MARKER
    old = (r'set "TOKEN_OPTIMIZER_RUNTIME=codex" && "C:\Python315\python.exe" '
           r'"C:\Users\Jo$h\.codex\t\windows-launcher.py" --baked-root "C:\Users\Jo$h\.codex\t\5.13.57" '
           rf'-- "hooks/stop_runner.py" "{m}"')
    assert ci.neutralize_launcher_command(old) is None
    assert "codex-install" in ci.explain_cmd_only_command(old)


def test_signature_ignores_the_version_in_a_relative_baked_root(monkeypatch):
    """Upgrades keep an equivalent bootstrap command instead of forcing a
    trust review for a version-only change."""
    ci = _load_ci(monkeypatch)
    names = ShortNames(False)
    a, _, _, _ = _generate(monkeypatch, ci, r"C:\Users\Jo$h", _SAFE_PY, names)
    b = a.replace('"5.13.59"', '"5.13.60"')
    assert a != b and ci._launcher_signature(a) == ci._launcher_signature(b)


# ---- shell models catch the shapes that broke ---------------------------------

def test_models_reject_the_5_13_57_command_in_powershell():
    m = "token-optimizer/scripts/windows-launcher"
    old = (r'set "TOKEN_OPTIMIZER_RUNTIME=codex" && "C:\Python315\python.exe" "C:\u\windows-launcher.py" '
           rf'--baked-root "C:\u\5.13.57" -- "hooks/sessionstart_runner.py" "{m}"')
    for version in ("5.1", "7"):
        with pytest.raises(shells.ShellError):
            shells.powershell_run(old, version)
    quoted_first = rf'"C:\Python315\python.exe" "C:\u\w.py" -- "h.py" "{m}"'
    with pytest.raises(shells.ShellError):
        shells.powershell_run(quoted_first, "7")


@pytest.mark.parametrize("token,shell", [
    ('"C:\\Users\\Jo$h\\w.py"', "pwsh-7"),
    ('"C:\\Users\\a`b\\w.py"', "powershell-5.1"),
    ('"C:\\Users\\Bang!\\w.py"', "cmd-delayed"),
    ("C:\\Users\\R&D\\w.py", "cmd"),
])
def test_models_reject_tokens_a_shell_would_change(token, shell):
    with pytest.raises(shells.ShellError):
        shells.run_in(shell, f"C:\\py.exe {token}", {"PATH": "x"})


def test_cmd_model_expands_defined_percent_variables():
    argv = shells.cmd_run(r'C:\py.exe "C:\Users\a%PATH%b\w.py"', {"PATH": r"C:\Windows"})
    assert argv[1] == r"C:\Users\aC:\Windowsb\w.py"


def test_powershell_51_drops_bare_double_dash_and_the_launcher_copes(tmp_path):
    argv = shells.powershell_run(r'C:\py.exe "C:\l.py" --baked-root "C:\b\5.1.1" -- "h.py" "--quiet"', "5.1")
    assert "--" not in argv
    assert LAUNCHER._parse(argv[2:]) == (r"C:\b\5.1.1", [], "h.py", ["--quiet"])


# ---- the bootstrap form really runs ---------------------------------------------

def _fake_tree(home: Path) -> Path:
    base = home / ".codex" / "plugins" / "cache" / "token-optimizer" / "token-optimizer"
    root = base / "5.13.59"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "run.py").write_text(
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read(),\n"
        " 'runtime': os.environ.get('TOKEN_OPTIMIZER_RUNTIME'),\n"
        " 'root': os.environ.get('TOKEN_OPTIMIZER_RUNTIME_ROOT')}))\n", encoding="utf-8")
    shutil.copyfile(LAUNCHER_SOURCE, base / "windows-launcher.py")
    return root


# Captured before any test patches sys.executable (codex_install shares the
# sys module, so the patched "python" would also be what we spawn).
REAL_PYTHON = sys.executable


def _pinned_env(tmp_path, **extra):
    """Subprocess env with HOME and CLAUDE_CONFIG_DIR inside tmp_path, so no
    spawned hook can reach the real ~/.claude or scheduler folders."""
    home = tmp_path / "h"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
    env.update(extra)
    return env


def test_bootstrap_command_runs_the_hook_end_to_end(monkeypatch, tmp_path):
    """Generate the bootstrap form for a user folder with a $ in it, pass it
    through the PowerShell 5.1 model, and run the resulting argv for real."""
    ci = _load_ci(monkeypatch)
    home = tmp_path / "Jo$h"
    root = _fake_tree(home)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(ci, "_short_path", lambda p: None)
    monkeypatch.setattr(ci.sys, "executable", "python")
    cmd = ci._windows_launcher_command(root, "hooks/stop_runner.py", ["--quiet"], {})
    assert ci.is_shell_neutral_command(cmd) and " -c " in cmd, cmd
    argv = shells.powershell_run(cmd, "5.1")
    argv[0] = REAL_PYTHON
    env = _pinned_env(tmp_path, USERPROFILE=str(home))
    proc = subprocess.run(argv, input=b'{"session_id":"abc123def"}', capture_output=True,
                          timeout=120, env=env, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.decode().strip().splitlines()[-1])
    assert out["argv"] == ["hooks/stop_runner.py", "--quiet"]
    assert json.loads(out["stdin"])["session_id"] == "abc123def"
    assert out["runtime"] == "codex"
    assert Path(out["root"]).resolve() == root.resolve()


def test_launcher_resolves_a_relative_baked_root_next_to_itself(tmp_path):
    """Even when the newest-version scan finds nothing usable, a bare version
    name falls back to the dir beside the launcher, not the hook's cwd."""
    root = _fake_tree(tmp_path / "home")
    launcher = root.parent / "windows-launcher.py"
    (tmp_path / "elsewhere").mkdir()
    # Make the scan fail so only the baked fallback can be used.
    hidden = root.parent / "5.13.59"
    renamed = root.parent / "pinned"
    hidden.rename(renamed)
    proc = subprocess.run(
        [sys.executable, str(launcher), "--baked-root", "pinned", "--", "hooks/x.py"],
        input=b"{}", capture_output=True, timeout=60, cwd=str(tmp_path / "elsewhere"),
        env=_pinned_env(tmp_path))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.decode().strip().splitlines()[-1])
    assert Path(out["root"]).resolve() == renamed.resolve()


def test_dev_checkout_command_runs_the_checkout_in_every_shell(monkeypatch, tmp_path):
    """A development checkout used to get the cmd-only ``set ... &&`` form,
    which exits 1 in PowerShell. It now runs the checkout's own launcher,
    pinned to the checkout even when version folders sit beside it."""
    ci = _load_ci(monkeypatch)
    checkout = tmp_path / "work" / "token-optimizer"
    (checkout / "hooks").mkdir(parents=True)
    (checkout / "hooks" / "run.py").write_text(
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'runtime': os.environ.get('TOKEN_OPTIMIZER_RUNTIME'),\n"
        " 'root': os.environ.get('TOKEN_OPTIMIZER_RUNTIME_ROOT')}))\n", encoding="utf-8")
    shutil.copyfile(LAUNCHER_SOURCE, checkout / "hooks" / "windows-launcher.py")
    decoy = tmp_path / "work" / "9.9.9" / "hooks"
    decoy.mkdir(parents=True)
    (decoy / "run.py").write_text("raise SystemExit('ran the decoy sibling')\n", encoding="utf-8")
    monkeypatch.setattr(ci, "_repo_root", lambda: checkout)
    monkeypatch.setattr(ci.sys, "executable", "python")
    cmd = ci._hook_command("hooks/stop_runner.py", "--quiet")
    assert ci.is_shell_neutral_command(cmd) and "&&" not in cmd, cmd
    env = _pinned_env(tmp_path)
    for shell in ("cmd", "powershell-5.1", "pwsh-7"):
        argv = shells.run_in(shell, cmd, env)
        argv[0] = REAL_PYTHON
        proc = subprocess.run(argv, input=b"{}", capture_output=True, timeout=60, env=env,
                              cwd=str(tmp_path))
        assert proc.returncode == 0, (shell, proc.stderr)
        out = json.loads(proc.stdout.decode().strip().splitlines()[-1])
        assert out["argv"] == ["hooks/stop_runner.py", "--quiet"] and out["runtime"] == "codex"
        assert Path(out["root"]).resolve() == checkout.resolve()
