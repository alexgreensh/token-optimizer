"""Printed hints name an interpreter and an env form the host's shells can run.

On Windows ``python3`` exists only as a Microsoft Store alias; python.org and
Store installs both provide ``python.exe``. ``VAR=value cmd`` is Bash syntax:
cmd.exe and PowerShell reject it. So on ``os.name == 'nt'`` every printed hint
says ``python`` and carries a runtime as a leading ``--runtime NAME`` flag of
measure.py; on macOS and Linux the output is byte-for-byte what it always was.

The Windows branches are driven through ``refetch_fingerprint._windows_hints``
(the single predicate every hint helper reads), so they run on any OS.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import recovery_output  # noqa: E402
import refetch_fingerprint  # noqa: E402
import runtime_env  # noqa: E402


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(refetch_fingerprint, "_windows_hints", lambda: True)


@pytest.fixture
def posix(monkeypatch):
    monkeypatch.setattr(refetch_fingerprint, "_windows_hints", lambda: False)


# --- the interpreter name ----------------------------------------------------

def test_hint_python_is_python_on_windows_and_python3_elsewhere(monkeypatch):
    monkeypatch.setattr(refetch_fingerprint, "_windows_hints", lambda: True)
    assert refetch_fingerprint.hint_python() == "python"
    monkeypatch.setattr(refetch_fingerprint, "_windows_hints", lambda: False)
    assert refetch_fingerprint.hint_python() == "python3"


def test_windows_predicate_follows_os_name(monkeypatch):
    monkeypatch.setattr(refetch_fingerprint.os, "name", "nt")
    assert refetch_fingerprint._windows_hints() is True
    monkeypatch.setattr(refetch_fingerprint.os, "name", "posix")
    assert refetch_fingerprint._windows_hints() is False


def test_expand_pointer_names_the_host_interpreter(monkeypatch, windows):
    monkeypatch.setattr(refetch_fingerprint, "measure_py_path",
                        lambda: "C:\\Users\\u\\to\\scripts\\measure.py")
    assert refetch_fingerprint.expand_command("original") == \
        "python C:/Users/u/to/scripts/measure.py expand original"


def test_expand_pointer_is_unchanged_on_posix(monkeypatch, posix):
    monkeypatch.setattr(refetch_fingerprint, "measure_py_path", lambda: "/opt/to/scripts/measure.py")
    assert refetch_fingerprint.expand_command("original") == \
        "python3 /opt/to/scripts/measure.py expand original"


# --- the recogniser accepts every interpreter name on every OS --------------

@pytest.mark.parametrize("flavour", ["windows", "posix"])
@pytest.mark.parametrize("interp", ["python", "python3", "py", "python3.12", "python.exe", "py.exe"])
def test_recogniser_accepts_every_interpreter_name(monkeypatch, tmp_path, flavour, interp):
    # A path that is absolute on the host running the test ("/opt/..." has no drive on
    # Windows, and the recogniser only accepts an absolute path to this installation).
    scripts = (tmp_path / "to" / "scripts").resolve()
    script = (scripts / "measure.py").as_posix()
    monkeypatch.setattr(refetch_fingerprint, "_windows_hints", lambda: flavour == "windows")
    monkeypatch.setattr(refetch_fingerprint, "measure_py_path", lambda: script)
    monkeypatch.setattr(recovery_output, "__file__", str(scripts / "recovery_output.py"))
    assert recovery_output.is_expand_command(f"{interp} {script} expand original")
    assert recovery_output.is_expand_command(f"{interp} {script} expand original --session s-1")
    assert not recovery_output.is_expand_command(f"{interp} {script} expand original | cat")
    assert not recovery_output.is_expand_command(f"{interp} {script} expand original; id")


@pytest.mark.parametrize("interp", ["perl", "ruby", "pythonic", "python3 -c", "sh"])
def test_recogniser_still_rejects_other_programs(monkeypatch, interp):
    monkeypatch.setattr(refetch_fingerprint, "measure_py_path", lambda: "/opt/to/scripts/measure.py")
    monkeypatch.setattr(recovery_output, "__file__", "/opt/to/scripts/recovery_output.py")
    assert not recovery_output.is_expand_command(f"{interp} /opt/to/scripts/measure.py expand original")


@pytest.mark.parametrize("interp", ["python", "python3", "py"])
def test_printed_windows_hint_with_a_spaced_path_is_recognised_under_any_name(monkeypatch, windows, interp):
    win = "C:\\Users\\First Last\\.claude\\to\\scripts\\measure.py"
    monkeypatch.setattr(refetch_fingerprint, "measure_py_path", lambda: win)
    hint = refetch_fingerprint.expand_command("original")
    assert hint.startswith('python "C:/Users/First Last/')
    swapped = interp + hint[len("python"):]
    assert recovery_output.is_expand_command(swapped)


# --- runtime_env helpers -----------------------------------------------------

def test_measure_cli_names_python_on_windows(monkeypatch, windows):
    out = runtime_env.measure_cli("doctor")
    assert out.startswith("python ") and not out.startswith("python3")
    assert out.endswith("measure.py doctor")


def test_measure_cli_is_unchanged_on_posix(posix):
    script = (SCRIPTS / "measure.py").resolve()
    assert runtime_env.measure_cli("doctor") == f"python3 {runtime_env.shell_path(script)} doctor"


def test_runtime_command_is_a_flag_on_windows_and_an_env_prefix_on_posix(monkeypatch):
    # The path is quoted by the same predicate, so read it after each switch.
    monkeypatch.setattr(refetch_fingerprint, "_windows_hints", lambda: True)
    script = runtime_env.shell_path((SCRIPTS / "measure.py").resolve())
    win = runtime_env.runtime_cli("codex", "codex-install", "--project", ".")
    assert win == f"python {script} --runtime codex codex-install --project ."
    assert "=" not in win.split("measure.py")[0]  # no VAR=value for cmd.exe or PowerShell
    monkeypatch.setattr(refetch_fingerprint, "_windows_hints", lambda: False)
    script = runtime_env.shell_path((SCRIPTS / "measure.py").resolve())
    assert runtime_env.runtime_cli("codex", "codex-install", "--project", ".") == \
        f"TOKEN_OPTIMIZER_RUNTIME=codex python3 {script} codex-install --project ."


def test_with_measure_cli_rewrites_the_env_prefix_only_on_windows(monkeypatch):
    text = "Run: TOKEN_OPTIMIZER_RUNTIME=codex python3 measure.py codex-install --project ."
    monkeypatch.setattr(refetch_fingerprint, "_windows_hints", lambda: True)
    script = runtime_env.shell_path((SCRIPTS / "measure.py").resolve())
    assert runtime_env.with_measure_cli(text) == \
        f"Run: python {script} --runtime codex codex-install --project ."
    monkeypatch.setattr(refetch_fingerprint, "_windows_hints", lambda: False)
    script = runtime_env.shell_path((SCRIPTS / "measure.py").resolve())
    assert runtime_env.with_measure_cli(text) == \
        f"Run: TOKEN_OPTIMIZER_RUNTIME=codex python3 {script} codex-install --project ."
    assert runtime_env.with_measure_cli("python3 measure.py doctor") == f"python3 {script} doctor"


def test_runtime_flag_is_consumed_before_the_command():
    env: dict = {}
    argv = ["measure.py", "--runtime", "codex", "codex-install", "--project", "."]
    assert runtime_env.consume_runtime_flag(argv, env) == ["measure.py", "codex-install", "--project", "."]
    assert env == {"TOKEN_OPTIMIZER_RUNTIME": "codex"}


@pytest.mark.parametrize("argv", [
    ["measure.py", "doctor"],
    ["measure.py"],
    ["measure.py", "--runtime"],              # no value: leave it for the command to reject
    ["measure.py", "doctor", "--runtime", "codex"],  # only a LEADING flag is a global one
])
def test_runtime_flag_leaves_other_argv_alone(argv):
    env: dict = {}
    assert runtime_env.consume_runtime_flag(list(argv), env) == argv
    assert env == {}


# --- measure.py ------------------------------------------------------------

@pytest.fixture
def measure(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    sys.modules.pop("measure", None)
    import measure as mod
    yield mod
    sys.modules.pop("measure", None)


def test_measure_hint_helpers_follow_the_runtime_env_ones(measure, monkeypatch):
    for flavour in (True, False):
        monkeypatch.setattr(refetch_fingerprint, "_windows_hints", lambda f=flavour: f)
        assert measure._measure_cli("doctor") == runtime_env.measure_cli("doctor")
        text = "x TOKEN_OPTIMIZER_RUNTIME=codex python3 measure.py collect and python3 measure.py doctor"
        assert measure._hint(text) == runtime_env.with_measure_cli(text)


def test_subagent_cache_commands_use_the_hint_builder(measure, monkeypatch):
    """Advice, enable and undo commands are pasted by users; they follow the host."""
    monkeypatch.setattr(refetch_fingerprint, "_windows_hints", lambda: True)
    undo = measure._subagent_cache_cmd("disable")
    enable = measure._subagent_cache_cmd("enable")
    bare = measure._subagent_cache_cmd()
    assert undo == measure._measure_cli("subagent-cache", "disable")
    assert enable == measure._measure_cli("subagent-cache", "enable")
    assert bare == measure._measure_cli("subagent-cache")
    assert undo.startswith("python ") and "python3" not in undo
    monkeypatch.setattr(refetch_fingerprint, "_windows_hints", lambda: False)
    assert measure._subagent_cache_cmd("disable").startswith("python3 ")


def test_no_printed_command_in_measure_py_hardcodes_python3_plus_the_script_path():
    """A `python3 {path}` f-string is a hint that bypasses the builder (and so stays
    python3 and POSIX-quoted on Windows)."""
    src = (SCRIPTS / "measure.py").read_text(encoding="utf-8")
    bad = [f"measure.py:{n}: {line.strip()[:90]}" for n, line in enumerate(src.splitlines(), 1)
           if re.search(r"python3 \{(?:mp_cmd|_shell_script_path\(\)|_display_path)", line)
           or re.search(r"TOKEN_OPTIMIZER_RUNTIME=\w+ python3 \{", line)]
    assert not bad, "\n".join(bad)


def test_fleet_auditor_hint_uses_the_same_builder(monkeypatch):
    fleet_dir = REPO / "skills" / "fleet-auditor" / "scripts"
    monkeypatch.syspath_prepend(str(fleet_dir))
    sys.modules.pop("fleet", None)
    import fleet
    try:
        text = "TOKEN_OPTIMIZER_RUNTIME=codex python3 measure.py codex-install --project ."
        monkeypatch.setattr(refetch_fingerprint, "_windows_hints", lambda: True)
        script = runtime_env.shell_path((SCRIPTS / "measure.py").resolve())
        assert fleet._with_measure_cli(text) == f"python {script} --runtime codex codex-install --project ."
        monkeypatch.setattr(refetch_fingerprint, "_windows_hints", lambda: False)
        script = runtime_env.shell_path((SCRIPTS / "measure.py").resolve())
        assert fleet._with_measure_cli(text) == f"TOKEN_OPTIMIZER_RUNTIME=codex python3 {script} codex-install --project ."
    finally:
        sys.modules.pop("fleet", None)


def test_dashboard_rewrites_the_env_prefix_for_a_windows_cli():
    html = (REPO / "skills" / "token-optimizer" / "assets" / "dashboard.html").read_text(encoding="utf-8")
    fn = html[html.index("function tcli("):html.index("function redactPaths")]
    assert "--runtime" in fn and "TOKEN_OPTIMIZER_RUNTIME" in fn


# --- end to end: the flag selects the runtime exactly like the env var ----------

def _doctor(tmp_path, *prefix, env_runtime=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "CODEX", "TOKEN_OPTIMIZER"))}
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    env.update({"HOME": str(home), "USERPROFILE": str(home), "CODEX_HOME": str(home / ".codex"),
                "CLAUDE_CONFIG_DIR": str(home / ".claude")},
               TOKEN_OPTIMIZER_SNAPSHOT_DIR=str(tmp_path / "snap"),
               TOKEN_OPTIMIZER_NO_PROC_SCAN="1")
    if env_runtime:
        env["TOKEN_OPTIMIZER_RUNTIME"] = env_runtime
    return subprocess.run([sys.executable, str(SCRIPTS / "measure.py"), *prefix, "doctor"],
                          capture_output=True, text=True, env=env, timeout=120, cwd=str(tmp_path))


def test_runtime_flag_behaves_like_the_env_var(tmp_path):
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    via_env = _doctor(tmp_path / "a", env_runtime="codex")
    via_flag = _doctor(tmp_path / "b", "--runtime", "codex")
    assert via_env.returncode == via_flag.returncode, (via_env.stderr, via_flag.stderr)

    def norm(result):
        return re.sub(re.escape(str(tmp_path)) + r"[/\\][ab]", "<tmp>", result.stdout)

    assert norm(via_env) == norm(via_flag)
    assert via_flag.stdout.strip(), via_flag.stderr
