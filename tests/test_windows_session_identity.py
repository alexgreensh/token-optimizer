"""Windows session identity (issue #211).

The Claude desktop app is Electron: its main process, GPU/renderer/utility/
crashpad children and the SSH broker are all image-named ``claude*.exe``, and
the Code tab hosts real sessions as ``claude.exe ... stream-json`` children.
``kill_stale_sessions`` consumes the same inventory, so image name alone must
never be enough to terminate a process. These tests pin:

- Electron children (``--type=``), the Electron main process and
  ``claude-ssh-broker`` are not counted as sessions
- desktop/SDK/IDE-hosted and headless sessions are counted but never terminated
- identity needs POSITIVE terminal evidence (a shell/terminal parent); every
  uncertain case (unreadable command line, unreadable executable directory,
  undecodable text, missing parent info) is "unknown" and never terminated
- prompt text in arguments is never mistaken for a switch
- a session is re-verified right before it is terminated
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
MEASURE_PATH = REPO / "skills" / "token-optimizer" / "scripts" / "measure.py"

APP = r"C:\Users\u\AppData\Local\AnthropicClaude\app-2.26454.0\claude.exe"
CODE_TAB = r"C:\Users\u\AppData\Roaming\Claude\claude-code\2.1.289\abc123\claude.exe"
CLI = r"C:\Users\u\.local\bin\claude.exe"

OLD = "2020-01-01T00:00:00Z"
SHELL_PID = 400


def _load_measure():
    scripts = str(MEASURE_PATH.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("measure_identity_under_test", MEASURE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _csv(rows, header):
    def esc(v):
        return '"' + str(v).replace('"', '""') + '"'
    out = [",".join(esc(h) for h in header)]
    for row in rows:
        out.append(",".join(esc(row[h]) for h in header))
    return "\r\n".join(out) + "\r\n"


def _install_fake_powershell(monkeypatch, measure, procs, cim=True, names=True,
                             parents=((SHELL_PID, 1, "pwsh.exe"), (1, 0, "explorer.exe")), electron_dir=None):
    """procs: dicts with pid, ppid, name, session, path, cmdline.

    electron_dir: None keeps the REAL marker probe (callers use tmp_path);
    a callable stubs it.
    """
    calls = []
    gp = _csv(
        [{"Id": p["pid"], "ProcessName": p["name"], "SessionId": p.get("session", 1), "StartTime": OLD}
         for p in procs],
        ["Id", "ProcessName", "SessionId", "StartTime"],
    )
    cm = _csv(
        [{"ProcessId": p["pid"], "ParentProcessId": p["ppid"],
          "ExecutablePath": p.get("path", ""), "CommandLine": p.get("cmdline", ""),
         "CreationDate": p.get("creation", OLD)}
         for p in procs],
        ["ProcessId", "ParentProcessId", "ExecutablePath", "CommandLine", "CreationDate"],
    )
    nm_rows = [{"ProcessId": p["pid"], "ParentProcessId": p["ppid"], "Name": p["name"] + ".exe"
                if not p["name"].endswith(".exe") else p["name"]} for p in procs]
    nm_rows += [{"ProcessId": a, "ParentProcessId": b, "Name": c} for a, b, c in parents]
    nm = _csv(nm_rows, ["ProcessId", "ParentProcessId", "Name"])

    def fake_run(argv, **kwargs):
        cmd = argv[-1]
        calls.append((cmd, kwargs))
        if "Select-Object ProcessId, ParentProcessId, Name" in cmd:
            if not names:
                raise FileNotFoundError("names unavailable")
            return subprocess.CompletedProcess(argv, 0, stdout=nm, stderr="")
        if "Win32_Process -Filter 'Name LIKE" in cmd:
            if not cim:
                raise FileNotFoundError("cim unavailable")
            return subprocess.CompletedProcess(argv, 0, stdout=cm, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout=gp, stderr="")

    monkeypatch.setattr(measure.subprocess, "run", fake_run)
    monkeypatch.setattr(measure, "_windows_process_creation", lambda pid: {})
    if electron_dir is not None:
        monkeypatch.setattr(measure, "_windows_dir_is_electron_app", electron_dir)
    else:
        monkeypatch.setattr(measure, "_windows_dir_is_electron_app", lambda path: "AnthropicClaude" in (path or ""))
    return calls


def desktop_fleet():
    procs = [
        dict(pid=864, ppid=1, name="claude", path=APP, cmdline=f'"{APP}"'),
        dict(pid=11848, ppid=864, name="claude", path=APP, cmdline=f'"{APP}" --type=gpu-process --field-trial-handle=1'),
        dict(pid=12472, ppid=864, name="claude", path=APP, cmdline=f'"{APP}" --type=renderer'),
        dict(pid=16936, ppid=864, name="claude", path=APP, cmdline=f'"{APP}" --type=utility --utility-sub-type=node.mojom.NodeService'),
        dict(pid=28504, ppid=864, name="claude", path=APP, cmdline=f'"{APP}" --type=crashpad-handler'),
        dict(pid=17828, ppid=864, name="claude-ssh-broker", path=r"C:\x\claude-ssh-broker.exe",
             cmdline=r'"C:\x\claude-ssh-broker.exe"'),
    ]
    for pid in (17948, 31280, 21948):
        procs.append(dict(pid=pid, ppid=864, name="claude", path=CODE_TAB,
                          cmdline=f'"{CODE_TAB}" --output-format stream-json --verbose --input-format stream-json'))
    return procs


def _one(monkeypatch, measure, cmdline, path=CLI, ppid=SHELL_PID, parents=((SHELL_PID, 1, "pwsh.exe"), (1, 0, "explorer.exe")), **kw):
    procs = [dict(pid=500, ppid=ppid, name="claude", path=path, cmdline=cmdline)]
    _install_fake_powershell(monkeypatch, measure, procs, parents=parents, **kw)
    return measure._collect_windows_claude_sessions()


def test_desktop_app_collapses_to_the_three_real_sessions(monkeypatch):
    measure = _load_measure()
    _install_fake_powershell(monkeypatch, measure, desktop_fleet())

    sessions = measure._collect_windows_claude_sessions()

    assert sorted(s["pid"] for s in sessions) == [17948, 21948, 31280]
    assert {s["identity"] for s in sessions} == {"embedded_session"}


def test_electron_main_without_visible_children_is_still_not_a_session(monkeypatch):
    measure = _load_measure()
    assert _one(monkeypatch, measure, f'"{APP}"', path=APP, ppid=1, parents=()) == []


def test_terminal_cli_needs_a_terminal_parent(monkeypatch):
    measure = _load_measure()
    sessions = _one(monkeypatch, measure, f'"{CLI}" --resume')
    assert [(s["pid"], s["identity"]) for s in sessions] == [(500, "terminal_cli")]


@pytest.mark.parametrize("parent", ["explorer.exe", "sihost.exe", "code.exe", "taskeng.exe", "svchost.exe"])
def test_non_terminal_parent_is_unverified(monkeypatch, parent):
    measure = _load_measure()
    sessions = _one(monkeypatch, measure, f'"{CLI}"', parents=((SHELL_PID, 1, parent), (1, 0, "explorer.exe")))
    assert [(s["pid"], s["identity"]) for s in sessions] == [(500, "unknown")]


def test_dead_or_unreadable_parent_is_unverified(monkeypatch):
    measure = _load_measure()
    sessions = _one(monkeypatch, measure, f'"{CLI}"', parents=())
    assert sessions[0]["identity"] == "unknown"


def test_names_unavailable_is_unverified(monkeypatch):
    measure = _load_measure()
    sessions = _one(monkeypatch, measure, f'"{CLI}"', names=False)
    assert sessions[0]["identity"] == "unknown"


def test_unreadable_command_line_is_unverified_not_terminal(monkeypatch):
    measure = _load_measure()
    assert _one(monkeypatch, measure, "")[0]["identity"] == "unknown"


def test_cim_unavailable_marks_everything_unverified(monkeypatch):
    measure = _load_measure()
    _install_fake_powershell(monkeypatch, measure, desktop_fleet(), cim=False)

    sessions = measure._collect_windows_claude_sessions()

    assert sessions, "Get-Process inventory is still reported"
    assert {s["identity"] for s in sessions} == {"unknown"}
    assert 17828 not in {s["pid"] for s in sessions}  # broker rejected by image name


def test_missing_executable_path_is_unverified(monkeypatch):
    measure = _load_measure()
    assert _one(monkeypatch, measure, "claude --resume", path="")[0]["identity"] == "unknown"


def test_pid_absent_from_cim_is_unverified(monkeypatch):
    measure = _load_measure()
    procs = [dict(pid=500, ppid=SHELL_PID, name="claude", path=CLI, cmdline=f'"{CLI}"')]
    _install_fake_powershell(monkeypatch, measure, procs)
    original = measure._windows_cim_process_details
    monkeypatch.setattr(measure, "_windows_cim_process_details",
                        lambda: {k: v for k, v in (original() or {}).items() if k != 500} or {1: {}})
    assert measure._collect_windows_claude_sessions()[0]["identity"] == "unknown"


def test_undecodable_text_is_unverified(monkeypatch):
    measure = _load_measure()
    sessions = _one(monkeypatch, measure, f'"{CLI}"', path=CLI.replace("u", "Ren\ufffd"))
    assert sessions[0]["identity"] == "unknown"


# --- fail closed on the Electron marker probe ------------------------------

def test_marker_probe_error_never_yields_terminal_cli(monkeypatch):
    measure = _load_measure()
    sessions = _one(monkeypatch, measure, f'"{APP}"', path=APP, ppid=SHELL_PID,
                    electron_dir=lambda path: None)
    assert [(s["pid"], s["identity"]) for s in sessions] == [(500, "unknown")]


def test_real_marker_probe_stat_error_is_indeterminate(monkeypatch):
    measure = _load_measure()

    def boom(path, *a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(measure.os, "stat", boom)
    assert measure._windows_dir_is_electron_app(CLI) is None


@pytest.mark.skipif(sys.platform == "win32", reason="chmod 0 semantics differ on Windows")
def test_real_unreadable_electron_dir_is_indeterminate_not_absent(monkeypatch, tmp_path):
    import os
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions")
    measure = _load_measure()
    exe = tmp_path / "claude.exe"
    res = tmp_path / "resources"
    res.mkdir()
    (res / "app.asar").write_text("x")
    res.chmod(0)
    tmp_path.chmod(0o111)  # searchable but not listable
    try:
        # Probe a marker that lives inside the locked directory.
        assert measure._windows_dir_is_electron_app(str(exe)) in (None, True)
        res.chmod(0o700)
        tmp_path.chmod(0o700)
        locked = tmp_path / "locked"
        locked.mkdir()
        (locked / "resources").mkdir()
        (locked / "resources" / "app.asar").write_text("x")
        (locked / "resources").chmod(0)
        assert measure._windows_dir_is_electron_app(str(locked / "claude.exe")) is None
    finally:
        (tmp_path / "locked" / "resources").chmod(0o700)
        res.chmod(0o700)
        tmp_path.chmod(0o700)


def test_real_marker_probe_missing_dir_is_indeterminate(tmp_path):
    measure = _load_measure()
    assert measure._windows_dir_is_electron_app(str(tmp_path / "nope" / "claude.exe")) is None


@pytest.mark.parametrize("marker", [("resources", "app.asar"), ("icudtl.dat",), ("chrome_100_percent.pak",), ("resources.pak",)])
def test_real_marker_probe_detects_electron_files(tmp_path, marker):
    measure = _load_measure()
    exe = tmp_path / "claude.exe"
    target = tmp_path.joinpath(*marker)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x")
    assert measure._windows_dir_is_electron_app(str(exe)) is True


def test_real_marker_probe_plain_cli_dir_is_not_electron(tmp_path):
    measure = _load_measure()
    (tmp_path / "claude.exe").write_text("x")
    assert measure._windows_dir_is_electron_app(str(tmp_path / "claude.exe")) is False
    assert measure._windows_dir_is_electron_app("") is False


def test_desktop_main_found_by_real_marker_probe_end_to_end(monkeypatch, tmp_path):
    measure = _load_measure()
    exe = tmp_path / "claude.exe"
    (tmp_path / "resources").mkdir()
    (tmp_path / "resources" / "app.asar").write_text("x")
    sessions = _one(monkeypatch, measure, f'"{exe}"', path=str(exe), electron_dir=measure._windows_dir_is_electron_app)
    assert sessions == []


def test_electron_main_identified_only_by_child_parentage(monkeypatch):
    measure = _load_measure()
    procs = [
        dict(pid=864, ppid=1, name="claude", path=r"C:\opaque\claude.exe", cmdline=r'"C:\opaque\claude.exe"'),
        dict(pid=865, ppid=864, name="claude", path=r"C:\opaque\claude.exe", cmdline=r'"C:\opaque\claude.exe" --type=gpu-process'),
    ]
    _install_fake_powershell(monkeypatch, measure, procs, electron_dir=lambda p: False)
    assert measure._collect_windows_claude_sessions() == []


def test_type_flag_child_is_dropped_even_without_markers_or_parent_link(monkeypatch):
    measure = _load_measure()
    assert _one(monkeypatch, measure, f'"{CLI}" --type=renderer', electron_dir=lambda p: False) == []


def test_child_of_electron_main_without_headless_flags_is_hosted(monkeypatch):
    measure = _load_measure()
    procs = [
        dict(pid=864, ppid=1, name="claude", path=APP, cmdline=f'"{APP}"'),
        dict(pid=865, ppid=864, name="claude", path=APP, cmdline=f'"{APP}" --type=gpu-process'),
        dict(pid=900, ppid=864, name="claude", path=CODE_TAB, cmdline=f'"{CODE_TAB}" --resume'),
    ]
    _install_fake_powershell(monkeypatch, measure, procs)
    sessions = measure._collect_windows_claude_sessions()
    assert [(s["pid"], s["identity"]) for s in sessions] == [(900, "embedded_session")]


def test_child_of_another_claude_process_is_hosted(monkeypatch):
    measure = _load_measure()
    procs = [
        dict(pid=500, ppid=SHELL_PID, name="claude", path=CLI, cmdline=f'"{CLI}"'),
        dict(pid=501, ppid=500, name="claude", path=CLI, cmdline=f'"{CLI}" --resume'),
    ]
    _install_fake_powershell(monkeypatch, measure, procs)
    got = {s["pid"]: s["identity"] for s in measure._collect_windows_claude_sessions()}
    assert got == {500: "terminal_cli", 501: "embedded_session"}


# --- argv parsing: prompt text is data, not switches -----------------------

def test_prompt_text_never_hides_a_session(monkeypatch):
    measure = _load_measure()
    sessions = _one(monkeypatch, measure, f'"{CLI}" "Explain --type=renderer to me"')
    assert [(s["pid"], s["identity"]) for s in sessions] == [(500, "terminal_cli")]


def test_prompt_text_never_flips_label_to_hosted(monkeypatch):
    measure = _load_measure()
    sessions = _one(monkeypatch, measure, f'"{CLI}" "use --output-format json and --sdk-url"')
    assert sessions[0]["identity"] == "terminal_cli"


@pytest.mark.parametrize("args", [
    "--output-format stream-json", "--input-format stream-json", "--sdk-url ws://x",
    "-p hi", "--print hi", "--ide", "mcp serve", "--output-format=json",
    '--"print" hi', '--debug mcp serve', '--model opus mcp serve', 'remote-control', 'plugin list',
    'agents', 'setup-token', 'login', 'doctor', 'update', 'install', 'config get', 'migrate-installer',
])
def test_each_headless_switch_alone_protects_the_session(monkeypatch, args):
    measure = _load_measure()
    sessions = _one(monkeypatch, measure, f'"{CLI}" {args}')
    assert sessions[0]["identity"] == "embedded_session"


def test_unrelated_flags_keep_terminal_cli(monkeypatch):
    measure = _load_measure()
    assert _one(monkeypatch, measure, f'"{CLI}" --resume --model opus')[0]["identity"] == "terminal_cli"


# --- transport -------------------------------------------------------------

def test_powershell_queries_force_utf8_transport(monkeypatch):
    measure = _load_measure()
    calls = _install_fake_powershell(monkeypatch, measure, desktop_fleet())
    measure._collect_windows_claude_sessions()
    cim_calls = [c for c in calls if "Win32_Process" in c[0]]
    assert len(cim_calls) == 2
    for cmd, kwargs in cim_calls:
        assert "OutputEncoding" in cmd and "UTF8" in cmd
        assert kwargs.get("encoding") == "utf-8"
        assert "-Filter 'Name LIKE ''claude%'''" in cmd or "Select-Object ProcessId, ParentProcessId, Name" in cmd
        assert "ExecutablePath" in cmd or "Name" in cmd


def test_cim_property_names_match_the_parser(monkeypatch):
    measure = _load_measure()
    calls = _install_fake_powershell(monkeypatch, measure, desktop_fleet())
    measure._collect_windows_claude_sessions()
    detail_cmd = next(c[0] for c in calls if "Name LIKE" in c[0])
    for prop in ("ProcessId", "ParentProcessId", "ExecutablePath", "CommandLine", "CreationDate"):
        assert prop in detail_cmd


# --- kill_stale ------------------------------------------------------------

def _health_with(monkeypatch, measure, sessions):
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(measure, "_collect_health_data", lambda: {"running_sessions": sessions})


def _session(pid, identity="terminal_cli", elapsed=13 * 3600, started="Wed Jan 01 00:00:00 2020"):
    s = {"pid": pid, "elapsed_seconds": elapsed, "elapsed_human": "13h", "flags": [], "started": started}
    if identity is not None:
        s["identity"] = identity
    return s


def _revalidates(monkeypatch, measure, ok=True):
    monkeypatch.setattr(measure, "_windows_revalidate_terminal_cli", lambda s: ok)


def test_kill_stale_terminates_only_identified_terminal_cli(monkeypatch, capsys):
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_session(1, "terminal_cli"), _session(2, "embedded_session"), _session(3, "unknown")])
    _revalidates(monkeypatch, measure)
    killed = []
    monkeypatch.setattr(measure.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(measure.os, "getpid", lambda: 9999)
    monkeypatch.setattr(measure.os, "getppid", lambda: 9998)

    measure.kill_stale_sessions(threshold_hours=12)

    import signal
    assert killed == [(1, signal.SIGTERM)]
    assert "Skipping 2 long-running sessions" in capsys.readouterr().out


def test_kill_stale_dry_run_with_mixed_identities_kills_nothing(monkeypatch, capsys):
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_session(1), _session(2, "embedded_session")])
    monkeypatch.setattr(measure.os, "kill", lambda *a: pytest.fail("dry run terminated a process"))
    monkeypatch.setattr(measure.os, "getpid", lambda: 9999)
    monkeypatch.setattr(measure.os, "getppid", lambda: 9998)
    measure.kill_stale_sessions(threshold_hours=12, dry_run=True)
    out = capsys.readouterr().out
    assert "Would kill 1 process" in out and "PID 1 " in out and "PID 2 " not in out


def test_kill_stale_never_kills_when_nothing_is_identified(monkeypatch, capsys):
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_session(2, "embedded_session"), _session(3, "unknown")])
    monkeypatch.setattr(measure.os, "kill", lambda *a: pytest.fail("terminated an unverified process"))

    measure.kill_stale_sessions(threshold_hours=12)

    out = capsys.readouterr().out
    assert "No terminable stale sessions" in out
    assert "all within threshold" not in out


def test_kill_stale_never_kills_sessions_without_an_identity_tag(monkeypatch):
    # Every collector that feeds kill_stale_sessions tags identity (Windows via
    # CIM, POSIX via ps argv + parent chain); an untagged session is not
    # positively a terminal CLI, so it is protected.
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_session(7, identity=None)])
    monkeypatch.setattr(measure.os, "kill", lambda *a: pytest.fail("terminated a session with no identity"))
    monkeypatch.setattr(measure, "_windows_revalidate_terminal_cli",
                        lambda s: pytest.fail("untagged sessions are never candidates"))

    measure.kill_stale_sessions(threshold_hours=12)


def test_kill_is_skipped_when_identity_changes_before_termination(monkeypatch, capsys):
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_session(500)])
    # Fresh inventory: the PID is now a desktop-hosted session.
    monkeypatch.setattr(measure, "_collect_windows_claude_sessions",
                        lambda **kw: [{"pid": 500, "identity": "embedded_session", "started": "Wed Jan 01 00:00:00 2020"}])
    monkeypatch.setattr(measure.os, "kill", lambda *a: pytest.fail("killed a re-classified process"))
    measure.kill_stale_sessions(threshold_hours=12)
    assert "identity changed" in capsys.readouterr().out


def test_kill_is_skipped_when_pid_was_reused_by_a_newer_process(monkeypatch):
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_session(500)])
    monkeypatch.setattr(measure, "_collect_windows_claude_sessions",
                        lambda **kw: [{"pid": 500, "identity": "terminal_cli", "started": "Thu Oct 08 09:00:00 2026"}])
    monkeypatch.setattr(measure.os, "kill", lambda *a: pytest.fail("killed a reused PID"))
    measure.kill_stale_sessions(threshold_hours=12)


def test_kill_is_skipped_when_start_time_unknown(monkeypatch):
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_session(500, started="unknown")])
    monkeypatch.setattr(measure, "_collect_windows_claude_sessions",
                        lambda **kw: pytest.fail("no re-collect needed when start is unknown"))
    monkeypatch.setattr(measure.os, "kill", lambda *a: pytest.fail("killed without a verifiable start time"))
    measure.kill_stale_sessions(threshold_hours=12)


def test_kill_proceeds_when_reverification_matches(monkeypatch):
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_session(500)])
    monkeypatch.setattr(measure, "_collect_windows_claude_sessions",
                        lambda **kw: [{"pid": 500, "identity": "terminal_cli", "started": "Wed Jan 01 00:00:00 2020"}])
    killed = []
    monkeypatch.setattr(measure.os, "kill", lambda pid, sig: killed.append(pid))
    measure.kill_stale_sessions(threshold_hours=12)
    assert killed == [500]


# --- health flags and dashboard -------------------------------------------

def test_health_flags_do_not_label_hosted_or_unverified_sessions_terminal(monkeypatch):
    measure = _load_measure()
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(measure.platform, "system", lambda: "Windows")
    monkeypatch.setattr(measure.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr=""))
    monkeypatch.setattr(measure, "_collect_windows_claude_sessions", lambda **kw: [
        {"pid": 1, "elapsed_seconds": 60, "has_terminal": True, "identity": "embedded_session"},
        {"pid": 2, "elapsed_seconds": 60, "has_terminal": True, "identity": "unknown"},
        {"pid": 3, "elapsed_seconds": 60, "has_terminal": True, "identity": "terminal_cli"},
    ])
    flags = {s["pid"]: s["flags"] for s in measure._collect_health_data()["running_sessions"]}
    assert "DESKTOP" in flags[1] and "TERMINAL" not in flags[1]
    assert "UNVERIFIED" in flags[2] and "TERMINAL" not in flags[2]
    assert "TERMINAL" in flags[3]


def test_dashboard_bulk_kill_banner_and_row_button_exclude_protected_sessions():
    src = (REPO / "skills" / "token-optimizer" / "assets" / "dashboard.html").read_text(encoding="utf-8")
    bulk = src[src.index("var killStaleBtn"):src.index("kill-pid-btn').forEach")]
    assert "DESKTOP" in bulk and "UNVERIFIED" in bulk
    banner = src[src.index("var staleCount = sessions.filter"):src.index("if (sessions.length === 0)")]
    assert "DESKTOP" in banner and "UNVERIFIED" in banner
    assert "!isProtected" in src


# --- argv parser -----------------------------------------------------------

@pytest.mark.parametrize("cmdline,expected", [
    ('"C:\\a b\\claude.exe" --print hi', ["C:\\a b\\claude.exe", "--print", "hi"]),
    ('claude.exe --"print" hi', ["claude.exe", "--print", "hi"]),
    ('claude.exe --"type"=utility', ["claude.exe", "--type=utility"]),
    ('claude.exe --opt="a b" c', ["claude.exe", "--opt=a b", "c"]),
    ('claude.exe "a \\"quoted\\" b"', ["claude.exe", 'a "quoted" b']),
    ('claude.exe "x ""y"" z"', ["claude.exe", 'x "y" z']),
    ('claude.exe C:\\dir\\ --x', ["claude.exe", "C:\\dir\\", "--x"]),
    ('  claude.exe   a   b  ', ["claude.exe", "a", "b"]),
])
def test_cmdline_tokenizer_follows_msvc_rules(cmdline, expected):
    measure = _load_measure()
    assert measure._windows_cmdline_tokens(cmdline) == expected


def test_quote_obfuscated_type_switch_is_still_a_helper(monkeypatch):
    measure = _load_measure()
    assert _one(monkeypatch, measure, f'"{CLI}" --"type"=utility') == []


def test_quoted_option_value_cannot_hide_or_fake_a_switch(monkeypatch):
    measure = _load_measure()
    sessions = _one(monkeypatch, measure, f'"{CLI}" --append-system-prompt="x --type=gpu y"')
    assert [(s["pid"], s["identity"]) for s in sessions] == [(500, "terminal_cli")]


def test_double_dash_ends_option_parsing(monkeypatch):
    measure = _load_measure()
    sessions = _one(monkeypatch, measure, f'"{CLI}" -- --type=renderer --print')
    assert [(s["pid"], s["identity"]) for s in sessions] == [(500, "terminal_cli")]


# --- ancestry must be complete ---------------------------------------------

def test_missing_grandparent_is_unverified(monkeypatch):
    measure = _load_measure()
    sessions = _one(monkeypatch, measure, f'"{CLI}"', parents=((SHELL_PID, 864, "pwsh.exe"),))
    assert sessions[0]["identity"] == "unknown"


def test_ancestry_cycle_is_unverified(monkeypatch):
    measure = _load_measure()
    sessions = _one(monkeypatch, measure, f'"{CLI}"', ppid=SHELL_PID, parents=((SHELL_PID, 500, "pwsh.exe"),))
    assert sessions[0]["identity"] == "unknown"


def test_overlong_ancestry_is_unverified(monkeypatch):
    measure = _load_measure()
    chain = [(SHELL_PID, 401, "pwsh.exe")] + [(401 + i, 402 + i, "pwsh.exe") for i in range(30)]
    sessions = _one(monkeypatch, measure, f'"{CLI}"', parents=tuple(chain))
    assert sessions[0]["identity"] == "unknown"


def test_multi_hop_electron_ancestor_is_hosted(monkeypatch):
    measure = _load_measure()
    procs = [
        dict(pid=864, ppid=1, name="claude", path=APP, cmdline=f'"{APP}"'),
        dict(pid=865, ppid=864, name="claude", path=APP, cmdline=f'"{APP}" --type=gpu-process'),
        dict(pid=500, ppid=450, name="claude", path=CLI, cmdline=f'"{CLI}"'),
    ]
    parents = ((450, 440, "cmd.exe"), (440, 864, "cmd.exe"), (1, 0, "explorer.exe"))
    _install_fake_powershell(monkeypatch, measure, procs, parents=parents)
    got = {s["pid"]: s["identity"] for s in measure._collect_windows_claude_sessions()}
    assert got == {500: "embedded_session"}


def test_multi_hop_claude_ancestor_is_hosted(monkeypatch):
    measure = _load_measure()
    procs = [
        dict(pid=600, ppid=1, name="claude", path=CLI, cmdline=f'"{CLI}"'),
        dict(pid=500, ppid=450, name="claude", path=CLI, cmdline=f'"{CLI}"'),
    ]
    parents = ((450, 440, "cmd.exe"), (440, 600, "cmd.exe"), (1, 0, "explorer.exe"))
    _install_fake_powershell(monkeypatch, measure, procs, parents=parents)
    got = {s["pid"]: s["identity"] for s in measure._collect_windows_claude_sessions()}
    assert got[500] == "embedded_session"


# --- CSV hygiene -----------------------------------------------------------

def test_utf8_bom_in_powershell_output_does_not_break_parsing(monkeypatch):
    measure = _load_measure()
    text = "\ufeff" + _csv([{"ProcessId": 500, "ParentProcessId": 400, "ExecutablePath": CLI, "CommandLine": "x", "CreationDate": OLD}],
                          ["ProcessId", "ParentProcessId", "ExecutablePath", "CommandLine", "CreationDate"])
    monkeypatch.setattr(measure.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=text, stderr=""))
    assert 500 in measure._windows_cim_process_details()


@pytest.mark.parametrize("text", [
    '"ProcessId","ParentProcessId","ExecutablePath","CommandLine","CreationDate"\r\n"500","400","c","unterminated',
    '"ProcessId","ParentProcessId","ExecutablePath","CommandLine","CreationDate"\r\n"500","400","c"\r\n',
    '"Foo","Bar"\r\n"1","2"\r\n',
])
def test_truncated_or_malformed_cim_output_is_not_evidence(monkeypatch, text):
    measure = _load_measure()
    monkeypatch.setattr(measure.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=text, stderr=""))
    assert measure._windows_cim_process_details() is None


def test_truncated_names_output_marks_everything_unverified(monkeypatch):
    measure = _load_measure()
    procs = [dict(pid=500, ppid=SHELL_PID, name="claude", path=CLI, cmdline=f'"{CLI}"')]
    _install_fake_powershell(monkeypatch, measure, procs)
    monkeypatch.setattr(measure, "_windows_process_names", lambda: None)
    assert measure._collect_windows_claude_sessions()[0]["identity"] == "unknown"


# --- kill-time failure paths ---------------------------------------------

def test_reverification_error_blocks_the_kill(monkeypatch):
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_session(500)])

    def boom(**kw):
        raise OSError("powershell hung")

    monkeypatch.setattr(measure, "_collect_windows_claude_sessions", boom)
    monkeypatch.setattr(measure.os, "kill", lambda *a: pytest.fail("killed after re-verification failed"))
    measure.kill_stale_sessions(threshold_hours=12)


def test_each_candidate_is_reverified_immediately_before_its_own_kill(monkeypatch):
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_session(1), _session(2)])
    order = []
    monkeypatch.setattr(measure, "_collect_windows_claude_sessions", lambda **kw: order.append("collect") or [
        {"pid": 1, "identity": "terminal_cli", "started": "Wed Jan 01 00:00:00 2020"},
        {"pid": 2, "identity": "terminal_cli", "started": "Wed Jan 01 00:00:00 2020"}])
    monkeypatch.setattr(measure.os, "kill", lambda pid, sig: order.append(f"kill{pid}"))
    monkeypatch.setattr(measure.os, "getpid", lambda: 9999)
    monkeypatch.setattr(measure.os, "getppid", lambda: 9998)
    measure.kill_stale_sessions(threshold_hours=12)
    assert order == ["collect", "kill1", "collect", "kill2"]


def test_recommendations_do_not_count_protected_sessions(monkeypatch):
    measure = _load_measure()
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(measure.platform, "system", lambda: "Windows")
    monkeypatch.setattr(measure.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr=""))
    monkeypatch.setattr(measure, "_collect_windows_claude_sessions", lambda **kw: [
        {"pid": 1, "elapsed_seconds": 200000, "has_terminal": True, "identity": "embedded_session"},
        {"pid": 2, "elapsed_seconds": 200000, "has_terminal": True, "identity": "unknown"},
    ])
    recs = measure._collect_health_data()["recommendations"]
    assert not any("24+ hours" in r for r in recs)


def test_dashboard_routes_tagged_inventories_through_the_cli():
    src = (REPO / "skills" / "token-optimizer" / "assets" / "dashboard.html").read_text(encoding="utf-8")
    bulk = src[src.index("var killStaleBtn"):src.index("kill-pid-btn').forEach")]
    assert "kill-stale" in bulk and "s.identity" in bulk
    assert "!s.identity" in src


# --- v4: argv[0] fragments, short flags, service hosts, coherence ----------

@pytest.mark.parametrize("cmdline,expected", [
    ('C:\\Pro"gram Files"\\claude.exe --print hi', ['C:\\Program Files\\claude.exe', '--print', 'hi']),
    ('"C:\\Program Files"\\claude.exe --type=utility', ['C:\\Program Files\\claude.exe', '--type=utility']),
    ('claude.exe "a\\"--print"', ['claude.exe', 'a"--print']),
    ('claude.exe a\\\\"b c"', ['claude.exe', 'a\\b c']),
    ('claude.exe a\\\\\\"b', ['claude.exe', 'a\\"b']),
])
def test_argv_zero_fragments_and_backslash_rules(cmdline, expected):
    measure = _load_measure()
    assert measure._windows_cmdline_tokens(cmdline) == expected


def test_quoted_fragment_program_name_cannot_hide_print(monkeypatch):
    measure = _load_measure()
    sessions = _one(monkeypatch, measure, 'C:\\Pro"gram Files"\\claude.exe --print hi', path=CLI)
    assert all(s["identity"] != "terminal_cli" for s in sessions)


@pytest.mark.parametrize("flag", ["-cp", "-pc", "-p", "-dp"])
def test_combined_short_flags_containing_p_are_headless(monkeypatch, flag):
    measure = _load_measure()
    sessions = _one(monkeypatch, measure, f'"{CLI}" {flag} hi')
    assert [s["identity"] for s in sessions] == ["embedded_session"]


@pytest.mark.parametrize("host", ["svchost.exe", "services.exe", "taskeng.exe", "taskhostw.exe"])
def test_service_or_scheduler_ancestry_is_never_terminal_cli(monkeypatch, host):
    measure = _load_measure()
    parents = ((SHELL_PID, 300, "cmd.exe"), (300, 200, host), (200, 1, "wininit.exe"), (1, 0, "explorer.exe"))
    sessions = _one(monkeypatch, measure, f'"{CLI}"', parents=parents)
    assert sessions[0]["identity"] == "unknown"


def test_ancestry_positive_paths():
    measure = _load_measure()
    st = measure._windows_ancestry_state
    assert st(5, {5: (0, "claude.exe")}, set()) == "complete"
    assert st(5, {5: (6, "claude.exe"), 6: (7, "cmd.exe"), 7: (8, "explorer.exe")}, set()) == "complete"
    assert st(5, {5: (6, "claude.exe"), 6: (7, "cmd.exe"), 7: (9, "cmd.exe"), 9: (0, "cmd.exe")}, set()) == "complete"
    assert st(5, {5: (6, "claude.exe"), 6: (7, "cmd.exe"), 7: (1, "cmd.exe"), 1: (0, "x")}, {7}) == "hosted"
    assert st(5, {5: (6, "claude.exe"), 6: (7, "claude.exe")}, set()) == "hosted"
    assert st(5, {5: (6, "claude.exe"), 6: (7, "cmd.exe")}, set()) == "incomplete"


def test_contradictory_parent_snapshots_are_unverified(monkeypatch):
    measure = _load_measure()
    procs = [dict(pid=500, ppid=SHELL_PID, name="claude", path=CLI, cmdline=f'"{CLI}"')]
    _install_fake_powershell(monkeypatch, measure, procs)
    real = measure._windows_cim_process_details
    def skewed():
        d = real()
        d[500] = dict(d[500], ppid=77)
        return d
    monkeypatch.setattr(measure, "_windows_cim_process_details", skewed)
    assert measure._collect_windows_claude_sessions()[0]["identity"] == "unknown"


def test_revalidation_does_not_probe_unrelated_processes(monkeypatch):
    measure = _load_measure()
    procs = [dict(pid=500, ppid=SHELL_PID, name="claude", path=CLI, cmdline=f'"{CLI}"')]
    _install_fake_powershell(monkeypatch, measure, procs)
    monkeypatch.setattr(measure, "_parse_iso_process_datetime", lambda text: None)
    monkeypatch.setattr(measure, "_windows_process_creation",
                        lambda pid: pytest.fail("per-PID fallback probe during re-verification"))
    assert measure._windows_revalidate_terminal_cli({"pid": 500, "started": "x"}) is False


def test_pid_reused_between_queries_is_unverified(monkeypatch):
    measure = _load_measure()
    procs = [dict(pid=500, ppid=SHELL_PID, name="claude", path=CLI, cmdline=f'"{CLI}"',
                  creation="2026-10-08T09:00:00Z")]
    _install_fake_powershell(monkeypatch, measure, procs)
    assert measure._collect_windows_claude_sessions()[0]["identity"] == "unknown"


def test_reused_pid_between_snapshot_and_cim_blocks_the_kill(monkeypatch):
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_session(500)])
    procs = [dict(pid=500, ppid=SHELL_PID, name="claude", path=CLI, cmdline=f'"{CLI}"',
                  creation="2026-10-08T09:00:00Z")]
    _install_fake_powershell(monkeypatch, measure, procs)
    monkeypatch.setattr(measure.os, "getpid", lambda: 9999)
    monkeypatch.setattr(measure.os, "getppid", lambda: 9998)
    monkeypatch.setattr(measure.os, "kill", lambda *a: pytest.fail("killed a reused PID"))
    measure.kill_stale_sessions(threshold_hours=12)


@pytest.mark.parametrize("a,b,ok", [
    (OLD, OLD, True), (OLD, "2020-01-01T00:00:01Z", True), (OLD, "2020-01-01T00:00:02Z", True), (OLD, "2020-01-01T00:00:03Z", False), (OLD, "2020-01-01T00:00:09Z", False),
    (OLD, "", False), ("", OLD, False), (OLD, "garbage", False), (OLD, None, False),
])
def test_start_time_agreement(a, b, ok):
    measure = _load_measure()
    assert measure._windows_start_times_agree(a, b) is ok
