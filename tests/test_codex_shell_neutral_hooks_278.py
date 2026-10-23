"""Issue #278: Codex Windows hook commands must run under PowerShell AND cmd.exe.

Codex runs a command hook through the session shell (``powershell -NoProfile
-Command <cmd>``, preferring pwsh) and falls back to ``%COMSPEC% /C``. The
5.13.57 command (``set "X=1" && "python" ...`` plus ``>NUL 2>&1``) is cmd-only,
so under PowerShell every hook exited 1 before Token Optimizer started. The
Stop hook also lost the session identity on its way to the deferred worker.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
LAUNCHER_SOURCE = REPO / "hooks" / "windows-launcher.py"
sys.path.insert(0, str(SCRIPTS))


def _load(monkeypatch, platform="win32"):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "codex_install_278", SCRIPTS / "codex_install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.sys, "platform", platform)
    return module


def _fake_install(tmp_path, parent_name="Plugin Space"):
    base = tmp_path / parent_name / "token-optimizer"
    root = base / "5.13.58"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "run.py").write_text(
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read(),\n"
        " 'runtime': os.environ.get('TOKEN_OPTIMIZER_RUNTIME'),\n"
        " 'silent_env': os.environ.get('TOKEN_OPTIMIZER_HOOK_SILENT'),\n"
        " 'extra': os.environ.get('TO_TEST')}))\n"
        "print('noise-on-stderr', file=sys.stderr)\n", encoding="utf-8")
    shutil.copyfile(LAUNCHER_SOURCE, base / "windows-launcher.py")
    return root


def _env(tmp_path):
    """Subprocess environment pinned to the tmp tree (host-safety guard)."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
    return env


def _pwsh_programs():
    names = ["powershell", "pwsh"] if sys.platform == "win32" else ["pwsh"]
    found = [shutil.which(n) for n in names]
    extra = os.environ.get("TO_TEST_PWSH")  # a pwsh binary outside PATH
    if extra and Path(extra).exists():
        found.append(extra)
    return [p for p in found if p]


# ---- generator ------------------------------------------------------------

def test_default_command_is_shell_neutral(monkeypatch, tmp_path):
    ci = _load(monkeypatch)
    root = _fake_install(tmp_path)
    monkeypatch.setattr(ci, "_repo_root", lambda: root)
    cmd = ci._hook_command("hooks/stop_runner.py", redirect_quiet=True)
    assert ci.is_shell_neutral_command(cmd), cmd
    assert not cmd.startswith('"')
    for bad in ("&&", ">NUL", "2>&1", "set "):
        assert bad not in cmd
    assert "TOKEN_OPTIMIZER_HOOK_SILENT=1" in cmd
    assert cmd.rstrip().endswith('"' + ci._LAUNCHER_MARKER + '"')


def test_quiet_is_an_env_pair_not_a_redirect(monkeypatch, tmp_path):
    ci = _load(monkeypatch)
    root = _fake_install(tmp_path)
    loud = ci._windows_launcher_command(root, "hooks/x.py", [], {})
    quiet = ci._windows_launcher_command(root, "hooks/x.py", [], {}, silent=True)
    assert "HOOK_SILENT" not in loud
    assert '--env "TOKEN_OPTIMIZER_HOOK_SILENT=1"' in quiet


@pytest.mark.parametrize("bad", ["a$b", "a`b", "a%b", "a!b"])
def test_unquotable_path_falls_back_to_cmd_only_form(monkeypatch, tmp_path, bad):
    ci = _load(monkeypatch)
    root = _fake_install(tmp_path, parent_name=f"p{bad}")
    cmd = ci._windows_launcher_command(root, "hooks/x.py", [], {}, silent=True)
    assert cmd.startswith(ci._WIN_UNQUOTED_PREFIX)
    assert cmd.endswith(">NUL 2>&1")
    assert not ci.is_shell_neutral_command(cmd)
    assert ci._LAUNCHER_MARKER in cmd


def test_spaced_interpreter_without_short_name_falls_back(monkeypatch, tmp_path):
    ci = _load(monkeypatch)
    root = _fake_install(tmp_path)
    monkeypatch.setattr(ci.sys, "executable", r"C:\Program Files\Python\python.exe")
    monkeypatch.setattr(ci, "_short_path", lambda p: None)
    cmd = ci._windows_launcher_command(root, "hooks/x.py", [], {})
    assert cmd.startswith(ci._WIN_UNQUOTED_PREFIX)
    assert not ci.is_shell_neutral_command(cmd)


def test_spaced_interpreter_uses_short_name_when_present(monkeypatch, tmp_path):
    ci = _load(monkeypatch)
    root = _fake_install(tmp_path)
    monkeypatch.setattr(ci.sys, "executable", r"C:\Program Files\Python\python.exe")
    monkeypatch.setattr(ci, "_short_path", lambda p: r"C:\PROGRA~1\Python\python.exe")
    cmd = ci._windows_launcher_command(root, "hooks/x.py", [], {})
    assert cmd.startswith(r"C:\PROGRA~1\Python\python.exe ")
    assert ci.is_shell_neutral_command(cmd)


def test_short_name_that_is_still_unsafe_is_rejected(monkeypatch):
    ci = _load(monkeypatch)
    monkeypatch.setattr(ci, "_short_path", lambda p: r"C:\a b\python.exe")
    assert ci._shell_neutral_exe(r"C:\x y\python.exe") is None


def test_shell_neutral_arg_rejects_expanding_and_breaking_chars(monkeypatch):
    ci = _load(monkeypatch)
    assert ci._shell_neutral_arg(r"C:\Users\Jo Smith\x.py") == r'"C:\Users\Jo Smith\x.py"'
    for bad in ('a"b', "a$b", "a`b", "a%b", "a!b", "a\nb", "a\\", "a\u201cb", "a\u2019b"):
        assert ci._shell_neutral_arg(bad) is None


def test_is_shell_neutral_command_rejects_old_shapes(monkeypatch):
    ci = _load(monkeypatch)
    m = ci._LAUNCHER_MARKER
    old276 = (r'set "TOKEN_OPTIMIZER_RUNTIME=codex" && C:\P\python.exe "C:\u\w.py" '
              rf'--baked-root "C:\u\5.1.1" -- "h.py" "{m}"')
    old_quoted = rf'"C:\P\python.exe" "C:\u\w.py" --baked-root "C:\u\5.1.1" -- "h.py" "{m}"'
    assert not ci.is_shell_neutral_command(old276)
    assert not ci.is_shell_neutral_command(old_quoted)
    assert not ci.is_shell_neutral_command(old276.replace("set ", "SET "))
    assert not ci.is_shell_neutral_command("python foreign.py")
    # a directory that merely contains the letters "set " is fine
    ok = rf'C:\P\python.exe "C:\u\Asset Dir\w.py" --baked-root "C:\u\5.1.1" -- "h.py" "{m}"'
    assert ci.is_shell_neutral_command(ok)


# ---- heal -----------------------------------------------------------------

def _write_hooks(path, commands):
    hooks = {"hooks": {f"E{i}": [{"hooks": [{"type": "command", "command": c}]}]
                       for i, c in enumerate(commands)}}
    path.write_text(json.dumps(hooks), encoding="utf-8")


def test_neutralize_rewrites_both_old_shapes(monkeypatch):
    ci = _load(monkeypatch)
    m = ci._LAUNCHER_MARKER
    a = (r'set "TOKEN_OPTIMIZER_RUNTIME=codex" && "C:\Python315\python.exe" "C:\u s\windows-launcher.py" '
         r'--baked-root "C:\u s\5.13.57" --env "TO_TEST=1" -- "hooks/stop_runner.py" "--quiet" '
         rf'"{m}" >NUL 2>&1')
    b = (r'"C:\Python315\python.exe" "C:\u\windows-launcher.py" --baked-root "C:\u\5.13.56" '
         rf'-- "hooks/sessionstart_runner.py" "{m}"')
    fa, fb = ci.neutralize_launcher_command(a), ci.neutralize_launcher_command(b)
    assert fa == (r'C:\Python315\python.exe "C:\u s\windows-launcher.py" --baked-root "C:\u s\5.13.57" '
                  r'--env "TO_TEST=1" --env "TOKEN_OPTIMIZER_HOOK_SILENT=1" -- "hooks/stop_runner.py" '
                  rf'"--quiet" "{m}"')
    assert fb == (r'C:\Python315\python.exe "C:\u\windows-launcher.py" --baked-root "C:\u\5.13.56" '
                  rf'-- "hooks/sessionstart_runner.py" "{m}"')
    assert ci.is_shell_neutral_command(fa) and ci.is_shell_neutral_command(fb)
    # idempotent
    assert ci.neutralize_launcher_command(fa) is None
    assert ci.neutralize_launcher_command(fb) is None


@pytest.mark.parametrize("tail", [" & calc", " ; calc", " | more", " > out.txt"])
def test_neutralize_refuses_anything_that_is_not_our_exact_shape(monkeypatch, tail):
    ci = _load(monkeypatch)
    m = ci._LAUNCHER_MARKER
    cmd = (r'set "TOKEN_OPTIMIZER_RUNTIME=codex" && "C:\P\python.exe" "C:\u\w.py" '
           rf'--baked-root "C:\u\5.1.1" -- "h.py" "{m}"{tail}')
    assert ci.neutralize_launcher_command(cmd) is None


def test_neutralize_refuses_spaced_interpreter_and_foreign(monkeypatch):
    ci = _load(monkeypatch)
    monkeypatch.setattr(ci, "_short_path", lambda p: None)
    m = ci._LAUNCHER_MARKER
    spaced = (r'set "TOKEN_OPTIMIZER_RUNTIME=codex" && "C:\Program Files\P\python.exe" "C:\u\w.py" '
              rf'--baked-root "C:\u\5.1.1" -- "h.py" "{m}"')
    assert ci.neutralize_launcher_command(spaced) is None
    assert ci.neutralize_launcher_command('"C:\\t\\python.exe" "C:\\t\\mine.py"') is None
    assert ci.neutralize_launcher_command("") is None


def test_heal_converts_ours_and_leaves_foreign_alone(monkeypatch, tmp_path):
    ci = _load(monkeypatch)
    m = ci._LAUNCHER_MARKER
    ours = (r'"C:\Python315\python.exe" "C:\u\windows-launcher.py" --baked-root "C:\u\5.13.56" '
            rf'-- "hooks/stop_runner.py" "{m}" >NUL 2>&1')
    foreign = r'"C:\tools\python.exe" "C:\tools\my_hook.py"'
    path = tmp_path / "hooks.json"
    _write_hooks(path, [ours, foreign])
    assert ci.heal_quoted_launcher_commands([path]) == 1
    data = json.loads(path.read_text(encoding="utf-8"))["hooks"]
    fixed = data["E0"][0]["hooks"][0]["command"]
    assert ci.is_shell_neutral_command(fixed)
    assert "TOKEN_OPTIMIZER_HOOK_SILENT=1" in fixed
    assert data["E1"][0]["hooks"][0]["command"] == foreign
    assert ci.heal_quoted_launcher_commands([path]) == 0


# ---- real shells ----------------------------------------------------------

@pytest.mark.skipif(not _pwsh_programs(), reason="no PowerShell available")
@pytest.mark.parametrize("silent", [False, True])
def test_generated_command_runs_in_powershell_with_stdin(monkeypatch, tmp_path, silent):
    ci = _load(monkeypatch, sys.platform)
    root = _fake_install(tmp_path)
    cmd = ci._windows_launcher_command(root, "hooks/stop_runner.py", ["--quiet"],
                                       {"TO_TEST": "ok"}, silent=silent)
    if sys.platform == "win32":
        assert ci.is_shell_neutral_command(cmd), cmd
    payload = json.dumps({"session_id": "abcdef123456", "transcript_path": "x"})
    for program in _pwsh_programs():
        proc = subprocess.run([program, "-NoProfile", "-Command", cmd],
                              input=payload.encode(), capture_output=True, timeout=120, env=_env(tmp_path))
        assert proc.returncode == 0, (program, proc.stdout, proc.stderr)
        if silent:
            assert proc.stdout.strip() == b"" and proc.stderr.strip() == b"", (program, proc.stdout)
            continue
        out = json.loads(proc.stdout.decode().strip().splitlines()[-1])
        assert out["argv"] == ["hooks/stop_runner.py", "--quiet"]
        assert json.loads(out["stdin"])["session_id"] == "abcdef123456"
        assert out["runtime"] == "codex" and out["extra"] == "ok"
        assert out["silent_env"] is None


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe /C is Windows-only")
@pytest.mark.parametrize("silent", [False, True])
def test_generated_command_runs_under_cmd_like_codex(monkeypatch, tmp_path, silent):
    """Codex's COMSPEC fallback: ``cmd.exe /C`` plus the whole command wrapped in
    one extra pair of quotes, passed as a raw argument."""
    ci = _load(monkeypatch, sys.platform)
    root = _fake_install(tmp_path)
    cmd = ci._windows_launcher_command(root, "hooks/stop_runner.py", [], {}, silent=silent)
    line = f'{os.environ.get("COMSPEC", "cmd.exe")} /C "{cmd}"'
    proc = subprocess.run(line, input=b'{"session_id":"abcdef123456"}',
                          capture_output=True, timeout=120, env=_env(tmp_path))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    if silent:
        assert proc.stdout.strip() == b""
    else:
        out = json.loads(proc.stdout.decode().strip().splitlines()[-1])
        assert json.loads(out["stdin"])["session_id"] == "abcdef123456"


def test_launcher_silent_env_is_consumed_and_silences_output(tmp_path):
    root = _fake_install(tmp_path)
    base = root.parent
    quiet = subprocess.run(
        [sys.executable, str(base / "windows-launcher.py"), "--baked-root", str(root),
         "--env", "TOKEN_OPTIMIZER_HOOK_SILENT=1", "--", "hooks/x.py"],
        input=b"{}", capture_output=True, timeout=60, env=_env(tmp_path))
    assert quiet.returncode == 0 and quiet.stdout == b"" and quiet.stderr == b""
    loud = subprocess.run(
        [sys.executable, str(base / "windows-launcher.py"), "--baked-root", str(root),
         "--", "hooks/x.py"], input=b"{}", capture_output=True, timeout=60, env=_env(tmp_path))
    out = json.loads(loud.stdout.decode().strip().splitlines()[-1])
    assert out["silent_env"] is None and b"noise-on-stderr" in loud.stderr


def test_older_launcher_still_parses_a_command_with_the_silent_pair(tmp_path):
    """A launcher older than this change treats the pair as plain env, so it
    keeps working (noisily) if install ever leaves it in place."""
    root = _fake_install(tmp_path)
    old = root.parent / "windows-launcher.py"
    src = LAUNCHER_SOURCE.read_text(encoding="utf-8")
    start = src.index("    # Per-hook \"no output\"")
    end = src.index("    runner = root")
    old.write_text(src[:start]
                   + "    for pair in env_pairs:\n"
                     "        key, sep, value = pair.partition('=')\n"
                     "        if sep and key:\n"
                     "            os.environ[key] = value\n"
                   + src[end:], encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(old), "--baked-root", str(root),
         "--env", "TOKEN_OPTIMIZER_HOOK_SILENT=1", "--", "hooks/x.py"],
        input=b"{}", capture_output=True, timeout=60, env=_env(tmp_path))
    assert proc.returncode == 0
    assert json.loads(proc.stdout.decode().strip().splitlines()[-1])["silent_env"] == "1"


# ---- doctor ---------------------------------------------------------------

def test_doctor_flags_cmd_only_commands(monkeypatch, tmp_path):
    import codex_doctor
    monkeypatch.setattr(codex_doctor, "codex_home", lambda: tmp_path)
    ci = codex_doctor.codex_install
    m = ci._LAUNCHER_MARKER
    old = (r'set "TOKEN_OPTIMIZER_RUNTIME=codex" && "C:\P\python.exe" "C:\u\w.py" '
           rf'--baked-root "C:\u\5.1.1" -- "hooks/stop_runner.py" "{m}"')
    new = (rf'C:\P\python.exe "C:\u\w.py" --baked-root "C:\u\5.1.1" -- "hooks/stop_runner.py" "{m}"')
    (tmp_path / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": old}]}]}}), encoding="utf-8")
    checks = codex_doctor._hook_shell_check()
    assert [c["status"] for c in checks] == ["WARN"] and "PowerShell" in checks[0]["detail"]
    (tmp_path / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": new}]}]}}), encoding="utf-8")
    assert [c["status"] for c in codex_doctor._hook_shell_check()] == ["OK"]
    (tmp_path / "hooks.json").write_text("{}", encoding="utf-8")
    assert codex_doctor._hook_shell_check() == []


def test_doctor_flags_generated_dev_checkout_command(monkeypatch, tmp_path):
    """F1: a non-semver (dev checkout) Windows install keeps the cmd-only
    command, which has no launcher marker; the doctor must still warn."""
    import codex_doctor
    ci = codex_doctor.codex_install
    monkeypatch.setattr(codex_doctor, "codex_home", lambda: tmp_path)
    monkeypatch.setattr(ci.sys, "platform", "win32")
    monkeypatch.setattr(ci, "_repo_root", lambda: Path(r"C:\dev\token-optimizer"))
    command = ci._hook_command("stop_runner.py")
    assert ci._LAUNCHER_MARKER not in command
    (tmp_path / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": command}]}]}}), encoding="utf-8")
    checks = codex_doctor._hook_shell_check()
    assert [c["status"] for c in checks] == ["WARN"], (command, checks)
    detail = checks[0]["detail"]
    assert "development checkout" in detail and "Reinstalling does not change this" in detail
    assert "run any measure.py" not in detail


# ---- Stop hook identity ---------------------------------------------------

def _measure(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    (tmp_path / "claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "codex")
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    import measure
    return measure


def _stop_runner(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    (tmp_path / "claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    spec = importlib.util.spec_from_file_location("stop_runner_278", REPO / "hooks" / "stop_runner.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_stop_runner_hands_session_identity_to_the_worker(monkeypatch, tmp_path):
    runner = _stop_runner(monkeypatch, tmp_path)
    seen = []
    monkeypatch.setattr(runner.measure, "_dispatch_session_end_flush",
                        lambda args, budget_seconds=None: seen.append(list(args)))
    monkeypatch.setattr(runner, "_runner_budget", lambda *a, **k: 5.0)
    runner._sub_session_end_flush_stop(
        {"session_id": "abcdef-123456", "transcript_path": "/x/rollout.jsonl"})
    assert seen[0][:5] == ["session-end-flush", "--trigger", "stop", "--quiet", "--defer"]
    assert seen[0][5:] == ["--session-id", "abcdef-123456",
                           "--transcript-path", "/x/rollout.jsonl"]


@pytest.mark.parametrize("payload", [
    {}, {"session_id": 123, "transcript_path": ["a"]},
    {"session_id": "", "transcript_path": None},
    {"session_id": "a\x00b", "transcript_path": "x" * 5000},
])
def test_stop_runner_omits_missing_or_unsafe_identity(monkeypatch, tmp_path, payload):
    runner = _stop_runner(monkeypatch, tmp_path)
    seen = []
    monkeypatch.setattr(runner.measure, "_dispatch_session_end_flush",
                        lambda args, budget_seconds=None: seen.append(list(args)))
    monkeypatch.setattr(runner, "_runner_budget", lambda *a, **k: 5.0)
    runner._sub_session_end_flush_stop(payload)
    assert seen[0] == ["session-end-flush", "--trigger", "stop", "--quiet", "--defer"]


def test_stop_runner_records_field_presence_never_values(monkeypatch, tmp_path):
    runner = _stop_runner(monkeypatch, tmp_path)
    recorded = {}
    monkeypatch.setattr(runner.measure, "_record_hook_status",
                        lambda key, result: recorded.__setitem__(key, result))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    runner._record_payload_shape({"session_id": "SECRET-SESSION-ID", "cwd": "/private/path"})
    text = recorded["stop_payload"]
    assert "session_id=yes" in text and "transcript_path=no" in text
    assert "CODEX_THREAD_ID env=no" in text and "stdin fields: 2" in text
    assert "SECRET" not in text and "/private" not in text


def test_worker_capture_passes_identity_through(monkeypatch, tmp_path):
    m = _measure(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(m, "compact_capture", lambda **kw: calls.append(kw) or "/tmp/cp.md")
    monkeypatch.setattr(m, "_record_hook_status", lambda *a: None)
    out = m._flush_capture_checkpoint(
        ["session-worker", "--trigger", "stop", "--quiet", "--session-id", "abc123def",
         "--transcript-path", "/t/r.jsonl"])
    assert out == "/tmp/cp.md"
    assert calls == [{"trigger": "stop", "backfill_tools": True,
                      "session_id": "abc123def", "transcript_path": "/t/r.jsonl"}]


def test_worker_capture_skips_anonymous_codex_session(monkeypatch, tmp_path):
    m = _measure(monkeypatch, tmp_path)
    calls, status = [], {}
    monkeypatch.setattr(m, "compact_capture", lambda **kw: calls.append(kw))
    monkeypatch.setattr(m, "_find_current_session_jsonl", lambda: None)
    monkeypatch.setattr(m, "_record_hook_status", lambda k, v: status.__setitem__(k, v))
    assert m._flush_capture_checkpoint(["w", "--trigger", "stop"]) is None
    assert calls == []
    assert status["stop_capture"].startswith("skipped")


def test_compact_capture_writes_no_unknown_placeholder_on_codex(monkeypatch, tmp_path):
    m = _measure(monkeypatch, tmp_path)
    monkeypatch.setattr(m, "CHECKPOINT_DIR", tmp_path / "checkpoints")
    monkeypatch.setattr(m, "_find_current_session_jsonl", lambda: None)
    assert m.compact_capture(trigger="stop") is None
    assert not list((tmp_path / "checkpoints").glob("unknown-*"))
    # A named session whose transcript is missing still leaves its own marker.
    named = m.compact_capture(trigger="stop", session_id="abcdef123456")
    assert named and "abcdef123456" in named


def test_reinstall_replaces_the_old_command_without_duplicating(monkeypatch, tmp_path):
    ci = _load(monkeypatch)
    root = _fake_install(tmp_path)
    monkeypatch.setattr(ci, "_repo_root", lambda: root)
    old = ci._cmd_only_launcher_command(root, "hooks/stop_runner.py", [], {}, silent=True)
    existing = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": old, "timeout": 15}]}]}}
    merged = ci._merge_hooks(existing)
    stops = merged["hooks"]["Stop"]
    ours = [g for g in stops if ci._is_token_optimizer_group(g)]
    assert len(ours) == 1
    cmd = ours[0]["hooks"][0]["command"]
    assert ci.is_shell_neutral_command(cmd) and "&&" not in cmd
    for groups in merged["hooks"].values():
        for g in groups:
            for h in g["hooks"]:
                assert ci.is_shell_neutral_command(h["command"]), h["command"]
