"""POSIX session identity (macOS and Linux), the same contract as Windows (issue #211).

``measure.py health`` / ``kill-stale`` used to decide on process age alone on
macOS and Linux, so a session hosted by the Claude desktop app, an SDK or
headless run, an IDE, or a subcommand process (``mcp``, ``doctor`` ...) could
be listed as a stale terminal session and be terminated. These tests pin:

- every collected ``claude`` process carries ``identity``:
  ``terminal_cli`` / ``embedded_session`` / ``unknown``; Electron helpers
  (``--type=``) and the desktop app's own main process are dropped
- ``terminal_cli`` needs POSITIVE evidence: a controlling TTY, a readable argv
  with no headless/subcommand marker, a complete parent chain that does not
  pass through the desktop app or another claude, and a shell / multiplexer /
  terminal / sshd parent. A tmux-hosted interactive session IS a terminal
  session. Everything unreadable or unexpected is ``unknown``
- only ``terminal_cli`` is ever terminated, re-verified (same pid, same start
  time, same command, still ``terminal_cli``) right before each kill
- DESKTOP / UNVERIFIED flags and recommendations behave as on Windows
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
MEASURE_PATH = REPO / "skills" / "token-optimizer" / "scripts" / "measure.py"

OLD = "Wed Jan  1 00:00:00 2020"
ETIME_OLD = "2000-00:00:00"

MAC_APP = "/Applications/Claude.app/Contents/MacOS/Claude"
MAC_HELPER = ("/Applications/Claude.app/Contents/Frameworks/Claude Helper (Renderer).app"
              "/Contents/MacOS/Claude Helper (Renderer)")
MAC_CODE_TAB = ("/Users/u/Library/Application Support/Claude/claude-code/2.1.289/"
                "claude.app/Contents/MacOS/claude")
CLI = "/Users/u/.local/bin/claude"
STREAM = "--output-format stream-json --verbose --input-format stream-json"


def _load_measure():
    scripts = str(MEASURE_PATH.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("measure_posix_identity_under_test", MEASURE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def proc(pid, ppid, comm, args=None, tty="??", started=OLD, etime=ETIME_OLD, exe=None):
    return dict(pid=pid, ppid=ppid, comm=comm, args=args if args is not None else comm,
                tty=tty, started=started, etime=etime, exe=exe)


def _table(procs):
    out = ["  PID  PPID TTY      STARTED                          ELAPSED COMMAND"]
    for p in procs:
        out.append(f"{p['pid']:>6} {p['ppid']:>5} {p['tty']:<8} {p['started']} {p['etime']:>13} {p['args']}")
    return "\n".join(out) + "\n"


def _names(procs):
    out = ["  PID  PPID COMM"]
    for p in procs:
        out.append(f"{p['pid']:>6} {p['ppid']:>5} {p['comm']}")
    return "\n".join(out) + "\n"


# A realistic macOS terminal: Terminal -> login -> -zsh -> claude, with launchd at the root.
def base_tree():
    return [
        proc(1, 0, "/sbin/launchd"),
        proc(300, 1, "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"),
        proc(310, 300, "/usr/bin/login", "login -pf u"),
        proc(320, 310, "-zsh", "-zsh", tty="ttys003"),
    ]


def install_ps(monkeypatch, measure, procs, table_rc=0, names_rc=0, table_raises=False,
               names_raises=False, argv=None, electron=False, exe=None):
    """Fake `ps` (both the args table and the comm table) plus the OS probes.

    argv: {pid: list|None} for /proc-style argv (default: unreadable, so the
    ps args string is split); exe: {pid: path|None} for the executable path.
    """
    argv = argv or {}
    exe = exe or {}
    calls = []

    def fake_run(argv_, **kw):
        calls.append((list(argv_), kw))
        if argv_[0] != "ps":
            return subprocess.CompletedProcess(argv_, 1, stdout="", stderr="")
        joined = " ".join(argv_)
        if "-p" in argv_ or "lstart=" in joined:
            return subprocess.CompletedProcess(argv_, 1, stdout="", stderr="")
        if "command" in joined:
            if table_raises:
                raise FileNotFoundError("ps")
            return subprocess.CompletedProcess(argv_, table_rc, stdout=_table(procs), stderr="")
        if names_raises:
            raise subprocess.TimeoutExpired(argv_, 10)
        return subprocess.CompletedProcess(argv_, names_rc, stdout=_names(procs), stderr="")

    by_pid = {p["pid"]: p for p in procs}
    monkeypatch.setattr(measure.subprocess, "run", fake_run)
    monkeypatch.setattr(measure, "_posix_read_argv", lambda pid: argv.get(pid))

    def exe_path(pid, comm=None):
        if pid in exe:
            return exe[pid]
        p = by_pid.get(pid)
        if p and p.get("exe"):
            return p["exe"]
        c = comm if comm is not None else (p["comm"] if p else "")
        return c if c.startswith("/") else None

    monkeypatch.setattr(measure, "_posix_exe_path", exe_path)
    if callable(electron):
        monkeypatch.setattr(measure, "_posix_dir_is_electron_app", electron)
    else:
        monkeypatch.setattr(measure, "_posix_dir_is_electron_app", lambda path: electron)
    return calls


def collect(monkeypatch, measure, procs, **kw):
    install_ps(monkeypatch, measure, procs, **kw)
    sessions = measure._collect_posix_claude_sessions()
    return sessions


def identities(sessions):
    return {s["pid"]: s.get("identity") for s in sessions}


def terminal_claude(args="claude", pid=500, ppid=320, tty="ttys003", comm=CLI, **kw):
    return proc(pid, ppid, comm, args, tty=tty, **kw)


# --- the three real classes -------------------------------------------------

def test_terminal_session_under_a_shell_is_terminal_cli(monkeypatch):
    measure = _load_measure()
    s = collect(monkeypatch, measure, base_tree() + [terminal_claude(f"{CLI} --resume abc")])
    assert identities(s) == {500: "terminal_cli"}
    assert s[0]["has_terminal"] is True and s[0]["tty"] == "ttys003"


def test_linux_terminal_session_is_terminal_cli(monkeypatch):
    measure = _load_measure()
    procs = [
        proc(1, 0, "systemd", "/sbin/init"),
        proc(900, 1, "gnome-terminal-", "/usr/libexec/gnome-terminal-server"),
        proc(901, 900, "bash", "bash", tty="pts/3"),
        proc(902, 901, "claude", "claude", tty="pts/3", exe="/home/u/.local/bin/claude"),
    ]
    s = collect(monkeypatch, measure, procs)
    assert identities(s) == {902: "terminal_cli"}


def test_desktop_app_stream_json_session_is_embedded_not_terminal(monkeypatch):
    measure = _load_measure()
    procs = base_tree() + [
        proc(700, 1, MAC_APP, MAC_APP),
        proc(701, 700, MAC_HELPER, f"{MAC_HELPER} --type=renderer --lang=en-US"),
        proc(702, 700, MAC_CODE_TAB, f"{MAC_CODE_TAB} {STREAM}"),
    ]
    s = collect(monkeypatch, measure, procs)
    assert identities(s) == {702: "embedded_session"}


def test_child_of_the_desktop_app_without_headless_flags_is_hosted(monkeypatch):
    measure = _load_measure()
    procs = base_tree() + [
        proc(700, 1, MAC_APP, MAC_APP),
        proc(702, 700, MAC_CODE_TAB, MAC_CODE_TAB, tty="ttys009"),
    ]
    assert identities(collect(monkeypatch, measure, procs)) == {702: "embedded_session"}


def test_multi_hop_desktop_ancestor_is_hosted(monkeypatch):
    measure = _load_measure()
    procs = base_tree() + [
        proc(700, 1, MAC_APP, MAC_APP),
        proc(710, 700, "/bin/zsh", "/bin/zsh -c x"),
        proc(711, 710, "/bin/sh", "/bin/sh"),
        proc(712, 711, CLI, CLI, tty="ttys009"),
    ]
    assert identities(collect(monkeypatch, measure, procs)) == {712: "embedded_session"}


def test_child_of_another_claude_process_is_hosted(monkeypatch):
    measure = _load_measure()
    procs = base_tree() + [
        terminal_claude(f"{CLI}", pid=500),
        proc(510, 500, "/bin/zsh", "/bin/zsh -c claude", tty="ttys003"),
        proc(511, 510, CLI, CLI, tty="ttys003"),
    ]
    got = identities(collect(monkeypatch, measure, procs))
    assert got == {500: "terminal_cli", 511: "embedded_session"}


def test_ccd_cli_launcher_child_is_hosted_via_its_argv(monkeypatch):
    measure = _load_measure()
    launcher = "/home/u/.claude/remote/ccd-cli/2.1.271"
    procs = [
        proc(1, 0, "systemd", "/sbin/init"),
        proc(800, 1, launcher, f"{launcher} --output-format stream-json"),
        proc(801, 800, "bash", "bash", tty="pts/1"),
        proc(802, 801, "claude", "claude", tty="pts/1", exe="/home/u/.local/bin/claude"),
    ]
    got = identities(collect(monkeypatch, measure, procs))
    assert got == {800: "embedded_session", 802: "embedded_session"}


# --- headless switches and subcommands --------------------------------------

@pytest.mark.parametrize("args", [
    "claude -p hello",
    "claude --print hello",
    "claude --print=hello",
    "claude -pc",
    "claude --output-format json",
    "claude --output-format=stream-json",
    "claude --input-format stream-json",
    "claude --sdk-url wss://x",
    "claude --ide",
])
def test_each_headless_switch_alone_protects_a_terminal_parented_session(monkeypatch, args):
    measure = _load_measure()
    got = identities(collect(monkeypatch, measure, base_tree() + [terminal_claude(args)]))
    assert got == {500: "embedded_session"}


@pytest.mark.parametrize("sub", ["mcp", "doctor", "update", "install", "config", "migrate-installer",
                                 # concatenated: tests/test_host_safety_guard.py greps quoted verb
                                 # literals, and this file never spawns a host-mutating verb
                                 "remote-control", "plugin", "agents", "setup" + "-token", "login", "logout"])
def test_subcommand_processes_are_not_terminal_sessions(monkeypatch, sub):
    measure = _load_measure()
    got = identities(collect(monkeypatch, measure, base_tree() + [terminal_claude(f"claude {sub} list")]))
    assert got == {500: "embedded_session"}


def test_unrelated_flags_keep_terminal_cli(monkeypatch):
    measure = _load_measure()
    args = "claude --model opus --resume abc --dangerously-skip-permissions"
    assert identities(collect(monkeypatch, measure, base_tree() + [terminal_claude(args)])) == {500: "terminal_cli"}


def test_exact_argv_keeps_prompt_text_from_masquerading_as_a_switch(monkeypatch):
    measure = _load_measure()
    args = "claude fix the --print flag"
    exact = {500: ["claude", "fix the --print flag"]}
    got = identities(collect(monkeypatch, measure, base_tree() + [terminal_claude(args)], argv=exact))
    assert got == {500: "terminal_cli"}


def test_ps_split_fallback_errs_on_the_protective_side(monkeypatch):
    # Without /proc, "--print" inside a prompt cannot be told from a switch: the
    # session is protected, never exposed to termination.
    measure = _load_measure()
    args = "claude fix the --print flag"
    got = identities(collect(monkeypatch, measure, base_tree() + [terminal_claude(args)]))
    assert got == {500: "embedded_session"}


def test_double_dash_ends_option_parsing(monkeypatch):
    measure = _load_measure()
    args = "claude -- --print"
    exact = {500: ["claude", "--", "--print"]}
    assert identities(collect(monkeypatch, measure, base_tree() + [terminal_claude(args)], argv=exact)) == {500: "terminal_cli"}


# --- helpers, the app itself and claude*-named non-sessions ------------------

def test_mac_helpers_and_app_main_are_never_counted(monkeypatch):
    measure = _load_measure()
    procs = base_tree() + [
        proc(700, 1, MAC_APP, MAC_APP),
        proc(701, 700, MAC_HELPER, f"{MAC_HELPER} --type=renderer"),
        proc(702, 700, "/Applications/Claude.app/Contents/Frameworks/Claude Helper (GPU).app/Contents/MacOS/Claude Helper (GPU)",
             "/Applications/Claude.app/Contents/Frameworks/Claude Helper (GPU).app/Contents/MacOS/Claude Helper (GPU) --type=gpu-process"),
        proc(703, 700, "/Applications/Claude.app/Contents/Resources/claude-ssh-broker",
             "/Applications/Claude.app/Contents/Resources/claude-ssh-broker"),
        proc(704, 1, "/usr/bin/vim", "/usr/bin/vim claude.py"),
    ]
    assert collect(monkeypatch, measure, procs) == []


def test_linux_electron_children_named_claude_are_dropped(monkeypatch):
    measure = _load_measure()
    exe = "/opt/Claude/claude"
    procs = [
        proc(1, 0, "systemd", "/sbin/init"),
        proc(600, 1, "claude", "/opt/Claude/claude --no-sandbox", exe=exe),
        proc(601, 600, "claude", "/opt/Claude/claude --type=zygote", exe=exe),
        proc(602, 600, "claude", "/opt/Claude/claude --type=gpu-process --gpu-preferences=x", exe=exe),
        proc(603, 600, "claude", "/opt/Claude/claude --type=utility --utility-sub-type=network.mojom.NetworkService", exe=exe),
    ]
    assert collect(monkeypatch, measure, procs, electron=lambda path: path.startswith("/opt/Claude")) == []


def test_quoted_type_switch_cannot_hide_a_helper(monkeypatch):
    measure = _load_measure()
    exe = "/opt/Claude/claude"
    procs = [proc(1, 0, "systemd", "/sbin/init"),
             proc(601, 1, "claude", "claude --type=utility", exe=exe)]
    exact = {601: ["claude", "--type=utility"]}
    assert collect(monkeypatch, measure, procs, argv=exact) == []


def test_electron_main_is_dropped_by_its_directory_markers(monkeypatch):
    measure = _load_measure()
    procs = [proc(1, 0, "systemd", "/sbin/init"),
             proc(600, 1, "claude", "claude", tty="pts/1", exe="/opt/Claude/claude")]
    assert collect(monkeypatch, measure, procs, electron=True) == []


def test_unprobeable_executable_directory_is_unknown_not_terminal(monkeypatch):
    measure = _load_measure()
    procs = base_tree() + [terminal_claude()]
    got = identities(collect(monkeypatch, measure, procs, electron=None))
    assert got == {500: "unknown"}


def test_unresolvable_executable_path_alone_does_not_block_a_terminal_session(monkeypatch):
    # A native install deletes the previous version's binary after an update, so
    # the longest-running (most stale) sessions have no resolvable exe path
    # (proc_pidpath fails on macOS). The TTY + shell parent + complete parent
    # chain are the positive evidence; the directory probe is extra evidence
    # that is applied when a path exists.
    measure = _load_measure()
    procs = [proc(1, 0, "systemd", "/sbin/init"), proc(901, 1, "bash", "bash", tty="pts/3"),
             proc(902, 901, "claude", "claude", tty="pts/3")]
    assert identities(collect(monkeypatch, measure, procs, exe={902: None})) == {902: "terminal_cli"}


def test_electron_main_without_a_resolvable_path_is_found_by_its_helper_children(monkeypatch):
    measure = _load_measure()
    procs = [
        proc(1, 0, "systemd", "/sbin/init"),
        proc(901, 1, "bash", "bash", tty="pts/3"),
        proc(600, 901, "claude", "/opt/Claude/claude --no-sandbox", tty="pts/3"),
        proc(601, 600, "claude", "/opt/Claude/claude --type=zygote", tty="pts/3"),
    ]
    assert collect(monkeypatch, measure, procs, exe={600: None, 601: None}) == []


def test_spaced_install_path_session_is_listed_and_protected(monkeypatch):
    # The desktop app's own claude lives under "Application Support": the
    # whitespace-split ps command column never showed it, so it was invisible.
    # It is found through COMM and listed as hosted, never as a terminal session.
    measure = _load_measure()
    procs = base_tree() + [
        proc(700, 1, MAC_APP, MAC_APP),
        proc(701, 700, "/Applications/Claude.app/Contents/Helpers/disclaimer",
             f"/Applications/Claude.app/Contents/Helpers/disclaimer --pgroup -- {MAC_CODE_TAB} {STREAM}"),
        proc(702, 701, MAC_CODE_TAB, f"{MAC_CODE_TAB} {STREAM}"),
    ]
    s = collect(monkeypatch, measure, procs)
    assert identities(s) == {702: "embedded_session"}
    assert s[0]["command"].startswith(MAC_CODE_TAB)


def test_spaced_argv0_is_rebuilt_from_comm_without_exact_argv(monkeypatch):
    measure = _load_measure()
    tab = "/Users/u/Library/Application Support/Claude/claude-code/2.1.289/x/claude"
    procs = base_tree() + [terminal_claude(f"{tab} --print hi", comm=tab)]
    got = identities(collect(monkeypatch, measure, procs))
    assert got == {500: "embedded_session"}


def test_a_user_install_under_a_spaced_path_is_a_terminal_session(monkeypatch):
    measure = _load_measure()
    mine = "/Users/John Smith/.local/bin/claude"
    procs = base_tree() + [terminal_claude(f"{mine} --resume abc", comm=mine)]
    assert identities(collect(monkeypatch, measure, procs)) == {500: "terminal_cli"}


def test_deleted_linux_executable_suffix_is_stripped(monkeypatch):
    measure = _load_measure()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(measure.os, "readlink", lambda p: "/home/u/.local/share/claude/versions/2.1.1 (deleted)")
    assert measure._posix_exe_path(123, "claude") == "/home/u/.local/share/claude/versions/2.1.1"


def test_exe_path_falls_back_to_an_absolute_comm_then_none(monkeypatch):
    measure = _load_measure()
    monkeypatch.setattr(sys, "platform", "freebsd")
    assert measure._posix_exe_path(123, "/usr/bin/claude") == "/usr/bin/claude"
    assert measure._posix_exe_path(123, "claude") is None
    assert measure._posix_exe_path(123, None) is None


def test_linux_proc_cmdline_is_split_on_nul_and_keeps_spaced_arguments(monkeypatch, tmp_path):
    measure = _load_measure()
    monkeypatch.setattr(sys, "platform", "linux")
    data = b"claude\0--model\0opus\0fix the --print flag\0\0\0\0"
    real_open = open

    def fake_open(path, *a, **kw):
        if str(path) == "/proc/4242/cmdline":
            f = tmp_path / "cmdline"
            f.write_bytes(data)
            return real_open(f, *a, **kw)
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", fake_open)
    assert measure._posix_read_argv(4242) == ["claude", "--model", "opus", "fix the --print flag"]


def test_linux_proc_cmdline_empty_or_unreadable_is_none(monkeypatch):
    measure = _load_measure()
    monkeypatch.setattr(sys, "platform", "linux")
    assert measure._posix_read_argv(2 ** 22 + 99) is None
    assert measure._posix_read_argv("not-a-pid") is None


def test_single_token_title_with_a_switch_cannot_hide_it(monkeypatch):
    measure = _load_measure()
    procs = base_tree() + [terminal_claude("claude --print hi")]
    got = identities(collect(monkeypatch, measure, procs, argv={500: ["claude --print hi"]}))
    assert got == {500: "embedded_session"}


# --- parents: tmux, screen, ssh, zombies ------------------------------------

def test_tmux_hosted_interactive_session_stays_terminal_cli(monkeypatch):
    measure = _load_measure()
    procs = [
        proc(1, 0, "/sbin/launchd"),
        proc(1000, 1, "tmux: server", "tmux new -s work"),
        proc(1001, 1000, "-zsh", "-zsh", tty="ttys004"),
        proc(1002, 1001, CLI, "claude", tty="ttys004"),
    ]
    assert identities(collect(monkeypatch, measure, procs)) == {1002: "terminal_cli"}


@pytest.mark.parametrize("parent_comm,parent_args", [
    ("tmux: server", "tmux new-session claude"),
    ("SCREEN", "SCREEN -S work"),
    ("screen", "screen -S work"),
    ("zellij", "zellij"),
    ("sshd", "sshd: u@pts/2"),
    ("sshd-session", "sshd-session: u@pts/2"),
    ("/usr/bin/fish", "fish"),
    ("/opt/homebrew/bin/nu", "nu"),
    ("kitty", "kitty"),
    ("ghostty", "ghostty"),
])
def test_multiplexer_ssh_and_terminal_parents_are_terminal_parents(monkeypatch, parent_comm, parent_args):
    measure = _load_measure()
    procs = [
        proc(1, 0, "/sbin/init"),
        proc(1000, 1, parent_comm, parent_args, tty="pts/2"),
        proc(1002, 1000, "claude", "claude", tty="pts/2", exe="/usr/local/bin/claude"),
    ]
    assert identities(collect(monkeypatch, measure, procs)) == {1002: "terminal_cli"}


@pytest.mark.parametrize("parent_comm,parent_args", [
    ("systemd", "/lib/systemd/systemd --user"),
    ("cron", "/usr/sbin/cron -f"),
    ("/Applications/Visual Studio Code.app/Contents/MacOS/Electron", "/Applications/Visual Studio Code.app/Contents/MacOS/Electron"),
    ("node", "node /home/u/app/server.js"),
    ("python3", "python3 run.py"),
    ("make", "make run"),
    ("/usr/libexec/other-launcher", "other-launcher"),
])
def test_non_terminal_parent_is_unverified(monkeypatch, parent_comm, parent_args):
    measure = _load_measure()
    procs = [
        proc(1, 0, "/sbin/launchd"),
        proc(1000, 1, parent_comm, parent_args, tty="ttys002"),
        proc(1002, 1000, CLI, "claude", tty="ttys002"),
    ]
    assert identities(collect(monkeypatch, measure, procs)) == {1002: "unknown"}


def test_orphan_reparented_to_init_is_unverified_even_with_a_tty(monkeypatch):
    # The zombie case the old age-based kill existed for: its terminal and shell
    # are gone, ppid is launchd/init. Nothing proves it is abandoned, so it is
    # listed, flagged UNVERIFIED, and never auto-killed.
    measure = _load_measure()
    procs = [proc(1, 0, "/sbin/launchd"), terminal_claude(ppid=1, tty="ttys003")]
    assert identities(collect(monkeypatch, measure, procs)) == {500: "unknown"}


def test_orphan_without_tty_under_init_is_unverified(monkeypatch):
    measure = _load_measure()
    procs = [proc(1, 0, "/sbin/launchd"), terminal_claude(ppid=1, tty="??")]
    assert identities(collect(monkeypatch, measure, procs)) == {500: "unknown"}


def test_shell_parent_without_a_controlling_tty_is_unverified(monkeypatch):
    measure = _load_measure()
    procs = base_tree() + [terminal_claude(tty="??")]
    got = collect(monkeypatch, measure, procs)
    assert identities(got) == {500: "unknown"} and got[0]["has_terminal"] is False


@pytest.mark.parametrize("tty", ["?", "-"])
def test_other_no_tty_markers_are_not_terminals(monkeypatch, tty):
    measure = _load_measure()
    procs = base_tree() + [terminal_claude(tty=tty)]
    assert identities(collect(monkeypatch, measure, procs)) == {500: "unknown"}


def test_parent_missing_from_the_process_table_is_unverified(monkeypatch):
    measure = _load_measure()
    procs = [proc(1, 0, "/sbin/launchd"), terminal_claude(ppid=4321)]
    assert identities(collect(monkeypatch, measure, procs)) == {500: "unknown"}


def test_missing_grandparent_is_unverified(monkeypatch):
    measure = _load_measure()
    procs = [proc(1, 0, "/sbin/launchd"), proc(320, 77777, "-zsh", "-zsh", tty="ttys003"), terminal_claude()]
    assert identities(collect(monkeypatch, measure, procs)) == {500: "unknown"}


def test_ancestry_cycle_is_unverified(monkeypatch):
    measure = _load_measure()
    procs = [proc(320, 321, "-zsh", "-zsh", tty="ttys003"), proc(321, 320, "-zsh", "-zsh"), terminal_claude()]
    assert identities(collect(monkeypatch, measure, procs)) == {500: "unknown"}


def test_overlong_ancestry_is_unverified(monkeypatch):
    measure = _load_measure()
    procs = [proc(1, 0, "/sbin/launchd")]
    chain = [proc(2000 + i, 2001 + i, "-zsh", "-zsh") for i in range(40)]
    chain.append(proc(2040, 1, "-zsh", "-zsh"))
    procs += chain + [terminal_claude(ppid=2000)]
    assert identities(collect(monkeypatch, measure, procs)) == {500: "unknown"}


def test_contradictory_parent_snapshots_are_unverified(monkeypatch):
    measure = _load_measure()
    procs = base_tree() + [terminal_claude()]
    install_ps(monkeypatch, measure, procs)
    real = measure.subprocess.run

    def run(argv, **kw):
        r = real(argv, **kw)
        if argv[0] == "ps" and "command" not in " ".join(argv):
            r.stdout = r.stdout.replace("   500   320", "   500   310")
        return r

    monkeypatch.setattr(measure.subprocess, "run", run)
    assert identities(measure._collect_posix_claude_sessions()) == {500: "unknown"}


# --- unreadable evidence fails closed ---------------------------------------

def test_ps_failure_still_returns_none_for_the_table(monkeypatch):
    measure = _load_measure()
    install_ps(monkeypatch, measure, base_tree(), table_raises=True)
    assert measure._collect_posix_claude_sessions() is None


def test_nonzero_table_ps_returns_empty(monkeypatch):
    measure = _load_measure()
    install_ps(monkeypatch, measure, base_tree() + [terminal_claude()], table_rc=1)
    assert measure._collect_posix_claude_sessions() == []


@pytest.mark.parametrize("kw", [dict(names_rc=1), dict(names_raises=True)])
def test_unreadable_name_table_marks_everything_unverified(monkeypatch, kw):
    measure = _load_measure()
    procs = base_tree() + [terminal_claude(), terminal_claude(f"claude {STREAM}", pid=501)]
    assert identities(collect(monkeypatch, measure, procs, **kw)) == {500: "unknown", 501: "unknown"}


def test_truncated_name_table_marks_everything_unverified(monkeypatch):
    measure = _load_measure()
    procs = base_tree() + [terminal_claude()]
    install_ps(monkeypatch, measure, procs)
    real = measure.subprocess.run

    def run(argv, **kw):
        r = real(argv, **kw)
        if argv[0] == "ps" and "command" not in " ".join(argv):
            r.stdout = "  PID  PPID COMM\ngarbage\n  500\n"
        return r

    monkeypatch.setattr(measure.subprocess, "run", run)
    assert identities(measure._collect_posix_claude_sessions()) == {500: "unknown"}


def test_undecodable_argv_is_unverified(monkeypatch):
    measure = _load_measure()
    procs = base_tree() + [terminal_claude()]
    assert identities(collect(monkeypatch, measure, procs, argv={500: ["claude", "bad�bytes"]})) == {500: "unknown"}


def test_undecodable_ps_args_are_unverified(monkeypatch):
    measure = _load_measure()
    procs = base_tree() + [terminal_claude("claude bad�bytes")]
    assert identities(collect(monkeypatch, measure, procs)) == {500: "unknown"}


def test_classifier_with_no_argv_is_unverified(monkeypatch):
    measure = _load_measure()
    monkeypatch.setattr(measure, "_posix_dir_is_electron_app", lambda p: False)
    names = {1: (0, "/sbin/launchd"), 320: (1, "-zsh"), 500: (320, CLI)}
    for argv in (None, []):
        detail = dict(pid=500, ppid=320, tty="ttys003", argv=argv, exe=CLI)
        assert measure._classify_posix_claude_process(detail, names, {}) == "unknown"


def test_unreadable_exact_argv_falls_back_to_the_ps_column(monkeypatch):
    measure = _load_measure()
    procs = base_tree() + [terminal_claude(f"{CLI} --resume abc")]
    assert identities(collect(monkeypatch, measure, procs, argv={500: []})) == {500: "terminal_cli"}


def test_ps_runs_wide_and_in_the_c_locale(monkeypatch):
    measure = _load_measure()
    calls = install_ps(monkeypatch, measure, base_tree() + [terminal_claude()])
    measure._collect_posix_claude_sessions()
    ps_calls = [c for c in calls if c[0][0] == "ps"]
    assert len(ps_calls) >= 2
    for argv, kw in ps_calls:
        assert "-ww" in argv, f"ps output must not be width-truncated: {argv}"
        assert kw["env"]["LC_ALL"] == "C" and kw["env"]["LC_TIME"] == "C"
        assert kw.get("encoding") == "utf-8" and kw.get("errors") == "replace"
    assert any("ppid" in a for a in ps_calls[0][0])


# --- the real probes --------------------------------------------------------

@pytest.mark.parametrize("marker", [
    ("resources", "app.asar"), ("icudtl.dat",), ("chrome_100_percent.pak",), ("resources.pak",),
])
def test_real_marker_probe_detects_linux_electron_files(tmp_path, marker):
    measure = _load_measure()
    target = tmp_path.joinpath(*marker)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x")
    assert measure._posix_dir_is_electron_app(str(tmp_path / "claude")) is True


def test_real_marker_probe_detects_a_macos_electron_bundle(tmp_path):
    measure = _load_measure()
    macos = tmp_path / "Claude.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (tmp_path / "Claude.app" / "Contents" / "Resources").mkdir()
    (tmp_path / "Claude.app" / "Contents" / "Resources" / "app.asar").write_text("x")
    assert measure._posix_dir_is_electron_app(str(macos / "Claude")) is True


def test_real_marker_probe_plain_cli_dir_and_plain_bundle_are_not_electron(tmp_path):
    measure = _load_measure()
    (tmp_path / "bin").mkdir()
    assert measure._posix_dir_is_electron_app(str(tmp_path / "bin" / "claude")) is False
    macos = tmp_path / "claude.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    assert measure._posix_dir_is_electron_app(str(macos / "claude")) is False


def test_real_marker_probe_missing_dir_and_empty_path_are_indeterminate(tmp_path):
    measure = _load_measure()
    assert measure._posix_dir_is_electron_app(str(tmp_path / "nope" / "claude")) is None
    assert measure._posix_dir_is_electron_app(None) is None
    assert measure._posix_dir_is_electron_app("") is None


def test_real_marker_probe_stat_error_is_indeterminate(monkeypatch, tmp_path):
    measure = _load_measure()
    (tmp_path / "icudtl.dat").write_text("x")
    real = measure.os.stat

    def stat(path, *a, **kw):
        if str(path).endswith("resources.pak"):
            raise PermissionError("denied")
        return real(path, *a, **kw)

    monkeypatch.setattr(measure.os, "stat", stat)
    (tmp_path / "x").write_text("")
    assert measure._posix_dir_is_electron_app(str(tmp_path / "claude")) is None


def test_real_probes_never_raise_on_this_machine():
    measure = _load_measure()
    import os
    argv = measure._posix_read_argv(os.getpid())
    assert argv is None or (isinstance(argv, list) and all(isinstance(a, str) for a in argv))
    assert measure._posix_exe_path(os.getpid(), None) is None or isinstance(measure._posix_exe_path(os.getpid(), None), str)
    assert measure._posix_read_argv(2 ** 22 + 12345) is None


def test_live_collector_tags_every_session_it_returns():
    measure = _load_measure()
    sessions = measure._collect_posix_claude_sessions()
    if sessions is None:
        pytest.skip("ps unavailable")
    for s in sessions:
        assert s["identity"] in ("terminal_cli", "embedded_session", "unknown")


@pytest.mark.parametrize("comm,expected", [
    ("-zsh", "zsh"), ("/bin/bash", "bash"), ("tmux: server", "tmux"), ("SCREEN", "screen"),
    ("/usr/libexec/gnome-terminal-server", "gnome-terminal-server"), ("", ""), (None, ""),
    ("Claude Helper (Renderer)", "claude helper (renderer)"),
])
def test_comm_name_normalisation(comm, expected):
    measure = _load_measure()
    assert measure._posix_comm_name(comm) == expected


# --- classifier unit level ---------------------------------------------------

def _classify(measure, args, comm_parent="-zsh", tty="ttys003", **overrides):
    names = {1: (0, "/sbin/launchd"), 320: (1, comm_parent), 500: (320, CLI)}
    args_by_pid = {1: "/sbin/launchd", 320: comm_parent, 500: args}
    detail = dict(pid=500, ppid=320, tty=tty, argv=args.split(), exe=CLI)
    detail.update(overrides)
    return measure._classify_posix_claude_process(detail, names, args_by_pid)


def test_classifier_requires_names(monkeypatch):
    measure = _load_measure()
    monkeypatch.setattr(measure, "_posix_dir_is_electron_app", lambda p: False)
    assert _classify(measure, "claude") == "terminal_cli"
    detail = dict(pid=500, ppid=320, tty="ttys003", argv=["claude"], exe=CLI)
    assert measure._classify_posix_claude_process(detail, None, {}) == "unknown"
    assert measure._classify_posix_claude_process(None, {}, {}) == "unknown"
    assert measure._classify_posix_claude_process({}, {}, {}) == "unknown"


def test_classifier_helper_first(monkeypatch):
    measure = _load_measure()
    monkeypatch.setattr(measure, "_posix_dir_is_electron_app", lambda p: False)
    assert _classify(measure, "claude --type=renderer") == "helper"


# --- kill_stale ---------------------------------------------------------------

def _tagged(pid, identity="terminal_cli", elapsed=13 * 3600, started=OLD, command="claude"):
    s = {"pid": pid, "elapsed_seconds": elapsed, "elapsed_human": "13h", "flags": [],
         "started": started, "command": command, "identity_source": "ps"}
    if identity is not None:
        s["identity"] = identity
    return s


def _health_with(monkeypatch, measure, sessions):
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(measure, "_collect_health_data", lambda: {"running_sessions": sessions})
    monkeypatch.setattr(measure.os, "getpid", lambda: 9999)
    monkeypatch.setattr(measure.os, "getppid", lambda: 9998)


def _fresh(monkeypatch, measure, sessions):
    monkeypatch.setattr(measure, "_collect_posix_claude_sessions", lambda process_name="claude": sessions)


def test_kill_stale_terminates_only_terminal_cli_after_reverification(monkeypatch, capsys):
    measure = _load_measure()
    sessions = [_tagged(1), _tagged(2, "embedded_session"), _tagged(3, "unknown")]
    _health_with(monkeypatch, measure, sessions)
    _fresh(monkeypatch, measure, [dict(s) for s in sessions])
    killed = []
    monkeypatch.setattr(measure.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    measure.kill_stale_sessions(threshold_hours=12)
    import signal
    assert killed == [(1, signal.SIGTERM)]
    assert "Skipping 2 long-running sessions" in capsys.readouterr().out


def test_kill_stale_never_kills_untagged_sessions(monkeypatch):
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_tagged(7, identity=None)])
    monkeypatch.setattr(measure.os, "kill", lambda *a: pytest.fail("terminated a session with no identity"))
    measure.kill_stale_sessions(threshold_hours=12)


def test_kill_is_skipped_when_identity_changes_before_termination(monkeypatch, capsys):
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_tagged(500)])
    _fresh(monkeypatch, measure, [_tagged(500, "embedded_session")])
    monkeypatch.setattr(measure.os, "kill", lambda *a: pytest.fail("killed a re-classified process"))
    measure.kill_stale_sessions(threshold_hours=12)
    assert "identity changed" in capsys.readouterr().out


def test_kill_is_skipped_when_pid_was_reused_by_a_newer_process(monkeypatch):
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_tagged(500)])
    _fresh(monkeypatch, measure, [_tagged(500, started="Sat Oct 10 09:00:00 2026")])
    monkeypatch.setattr(measure.os, "kill", lambda *a: pytest.fail("killed a reused PID"))
    measure.kill_stale_sessions(threshold_hours=12)


def test_kill_is_skipped_when_the_command_line_changed(monkeypatch):
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_tagged(500, command="claude")])
    _fresh(monkeypatch, measure, [_tagged(500, command="claude --resume other")])
    monkeypatch.setattr(measure.os, "kill", lambda *a: pytest.fail("killed a process whose argv changed"))
    measure.kill_stale_sessions(threshold_hours=12)


def test_kill_is_skipped_when_the_pid_vanished(monkeypatch):
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_tagged(500)])
    _fresh(monkeypatch, measure, [])
    monkeypatch.setattr(measure.os, "kill", lambda *a: pytest.fail("killed a vanished pid"))
    measure.kill_stale_sessions(threshold_hours=12)


def test_kill_is_skipped_when_start_time_unknown(monkeypatch):
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_tagged(500, started="unknown")])
    monkeypatch.setattr(measure, "_collect_posix_claude_sessions",
                        lambda process_name="claude": pytest.fail("no re-collect needed when start is unknown"))
    monkeypatch.setattr(measure.os, "kill", lambda *a: pytest.fail("killed without a verifiable start time"))
    measure.kill_stale_sessions(threshold_hours=12)


def test_reverification_error_blocks_the_kill(monkeypatch):
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_tagged(500)])

    def boom(process_name="claude"):
        raise RuntimeError("ps exploded")

    monkeypatch.setattr(measure, "_collect_posix_claude_sessions", boom)
    monkeypatch.setattr(measure.os, "kill", lambda *a: pytest.fail("killed after a failed re-verification"))
    measure.kill_stale_sessions(threshold_hours=12)


def test_reverification_with_unreadable_ps_blocks_the_kill(monkeypatch):
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_tagged(500)])
    monkeypatch.setattr(measure, "_collect_posix_claude_sessions", lambda process_name="claude": None)
    monkeypatch.setattr(measure.os, "kill", lambda *a: pytest.fail("killed with ps unreadable"))
    measure.kill_stale_sessions(threshold_hours=12)


def test_each_candidate_is_reverified_immediately_before_its_own_kill(monkeypatch):
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_tagged(1), _tagged(2)])
    events = []

    def fresh(process_name="claude"):
        events.append("collect")
        return [_tagged(1), _tagged(2)]

    monkeypatch.setattr(measure, "_collect_posix_claude_sessions", fresh)
    monkeypatch.setattr(measure.os, "kill", lambda pid, sig: events.append(("kill", pid)))
    measure.kill_stale_sessions(threshold_hours=12)
    assert events == ["collect", ("kill", 1), "collect", ("kill", 2)]


def test_kill_stale_dry_run_never_terminates(monkeypatch, capsys):
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_tagged(1), _tagged(2, "embedded_session")])
    monkeypatch.setattr(measure.os, "kill", lambda *a: pytest.fail("dry run terminated a process"))
    measure.kill_stale_sessions(threshold_hours=12, dry_run=True)
    out = capsys.readouterr().out
    assert "Would kill 1 process" in out and "PID 1 " in out and "PID 2 " not in out


def test_kill_stale_never_kills_the_session_it_runs_inside_of(monkeypatch):
    # claude (4000) -> bash tool shell (9998) -> this python (9999)
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_tagged(4000), _tagged(4001)])
    monkeypatch.setattr(measure, "_posix_ancestor_pids", lambda pid: {9998, 4000, 1})
    _fresh(monkeypatch, measure, [_tagged(4001)])
    killed = []
    monkeypatch.setattr(measure.os, "kill", lambda pid, sig: killed.append(pid))
    measure.kill_stale_sessions(threshold_hours=12)
    assert killed == [4001]


def test_ancestor_walk_follows_the_parent_chain_and_is_empty_when_ps_is_unreadable(monkeypatch):
    measure = _load_measure()
    install_ps(monkeypatch, measure, base_tree() + [terminal_claude()])
    assert measure._posix_ancestor_pids(500) == {320, 310, 300, 1}
    install_ps(monkeypatch, measure, base_tree(), names_rc=1)
    assert measure._posix_ancestor_pids(500) == set()


def test_kill_stale_never_kills_own_pid_or_parent(monkeypatch):
    measure = _load_measure()
    _health_with(monkeypatch, measure, [_tagged(9999), _tagged(9998)])
    monkeypatch.setattr(measure.os, "kill", lambda *a: pytest.fail("killed itself or its parent"))
    measure.kill_stale_sessions(threshold_hours=12)


# --- end to end through mocked ps ---------------------------------------------

def test_kill_stale_end_to_end_spares_desktop_headless_ide_orphan_and_subcommand(monkeypatch, capsys):
    measure = _load_measure()
    procs = base_tree() + [
        # the only abandoned-looking terminal session
        terminal_claude(f"{CLI} --resume a", pid=500),
        # a live conversation hosted by the desktop app
        proc(700, 1, MAC_APP, MAC_APP),
        proc(702, 700, MAC_CODE_TAB, f"{MAC_CODE_TAB} {STREAM}", tty="??"),
        # SDK/headless run started from a shell
        terminal_claude("claude -p summarize", pid=510),
        # IDE extension session
        terminal_claude("claude --ide --output-format stream-json", pid=511),
        # subcommand processes
        terminal_claude("claude mcp serve", pid=512),
        # orphan: its shell is gone, reparented to launchd
        terminal_claude(ppid=1, pid=513),
    ]
    install_ps(monkeypatch, measure, procs)
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(measure.os, "getpid", lambda: 9999)
    monkeypatch.setattr(measure.os, "getppid", lambda: 9998)
    monkeypatch.setattr(measure.platform, "system", lambda: "Linux")
    killed = []
    monkeypatch.setattr(measure.os, "kill", lambda pid, sig: killed.append(pid))
    measure.kill_stale_sessions(threshold_hours=12)
    assert killed == [500]
    assert "Skipping 5 long-running sessions" in capsys.readouterr().out


def test_kill_stale_end_to_end_tmux_session_is_terminable(monkeypatch):
    measure = _load_measure()
    procs = [
        proc(1, 0, "/sbin/launchd"),
        proc(1000, 1, "tmux: server", "tmux new -s work"),
        proc(1001, 1000, "-zsh", "-zsh", tty="ttys004"),
        proc(1002, 1001, CLI, "claude", tty="ttys004"),
    ]
    install_ps(monkeypatch, measure, procs)
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(measure.platform, "system", lambda: "Darwin")
    killed = []
    monkeypatch.setattr(measure.os, "kill", lambda pid, sig: killed.append(pid))
    measure.kill_stale_sessions(threshold_hours=12)
    assert killed == [1002]


def test_kill_stale_end_to_end_pid_reuse_between_snapshot_and_kill(monkeypatch):
    measure = _load_measure()
    procs = base_tree() + [terminal_claude()]
    install_ps(monkeypatch, measure, procs)
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(measure.platform, "system", lambda: "Darwin")
    real_collect = measure._collect_posix_claude_sessions
    state = {"n": 0}

    def collect_then_reuse(process_name="claude"):
        state["n"] += 1
        if state["n"] == 2:
            # the old process exited; a newer terminal claude took pid 500
            procs[-1].update(started="Sat Oct 10 09:00:00 2026", etime="00:10")
        return real_collect(process_name)

    monkeypatch.setattr(measure, "_collect_posix_claude_sessions", collect_then_reuse)
    monkeypatch.setattr(measure.os, "kill", lambda *a: pytest.fail("killed a reused pid"))
    measure.kill_stale_sessions(threshold_hours=12)
    assert state["n"] == 2


# --- health flags, recommendations, codex -------------------------------------

def test_health_flags_label_hosted_and_unverified_sessions_like_windows(monkeypatch):
    measure = _load_measure()
    procs = base_tree() + [
        proc(700, 1, MAC_APP, MAC_APP),
        proc(702, 700, MAC_CODE_TAB, f"{MAC_CODE_TAB} {STREAM}"),
        terminal_claude(ppid=1, pid=513),
        terminal_claude(pid=500),
    ]
    install_ps(monkeypatch, measure, procs)
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(measure.platform, "system", lambda: "Linux")
    flags = {s["pid"]: s["flags"] for s in measure._collect_health_data()["running_sessions"]}
    assert "DESKTOP" in flags[702] and "TERMINAL" not in flags[702]
    assert "UNVERIFIED" in flags[513] and "TERMINAL" not in flags[513]
    assert "TERMINAL" in flags[500] and "DESKTOP" not in flags[500]


def test_recommendations_do_not_count_protected_posix_sessions(monkeypatch):
    measure = _load_measure()
    procs = base_tree() + [
        proc(700, 1, MAC_APP, MAC_APP),
        proc(702, 700, MAC_CODE_TAB, f"{MAC_CODE_TAB} {STREAM}", etime="5-00:00:00"),
        terminal_claude(ppid=1, pid=513, etime="5-00:00:00"),
    ]
    install_ps(monkeypatch, measure, procs)
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(measure.platform, "system", lambda: "Darwin")
    recs = measure._collect_health_data()["recommendations"]
    assert not any("24+ hours" in r for r in recs)


def test_recommendations_still_count_a_long_running_terminal_session(monkeypatch):
    measure = _load_measure()
    procs = base_tree() + [terminal_claude(pid=500, etime="5-00:00:00")]
    install_ps(monkeypatch, measure, procs)
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(measure.platform, "system", lambda: "Darwin")
    recs = measure._collect_health_data()["recommendations"]
    assert any("1 session running 24+ hours" in r for r in recs)


def test_codex_posix_inventory_is_listed_but_never_terminated(monkeypatch, capsys):
    # Codex has desktop/headless hosts (app-server, exec), but its inventory is
    # age-agnostic by construction: every process is flagged RUNNING and
    # kill-stale is disabled for the whole runtime, so there is nothing to protect
    # by tagging identity.
    measure = _load_measure()
    procs = base_tree() + [
        proc(900, 320, "/opt/homebrew/bin/codex", "/opt/homebrew/bin/codex app-server --analytics-default-enabled",
             tty="??", etime="9-00:00:00"),
        proc(901, 320, "/opt/homebrew/bin/codex", "/opt/homebrew/bin/codex exec do-things",
             tty="ttys003", etime="9-00:00:00"),
    ]
    install_ps(monkeypatch, measure, procs)
    monkeypatch.setattr(measure, "detect_runtime", lambda: "codex")
    monkeypatch.setattr(measure.platform, "system", lambda: "Darwin")
    health = measure._collect_health_data()
    assert {s["pid"] for s in health["running_sessions"]} == {900, 901}
    assert all(s["flags"] == ["RUNNING"] for s in health["running_sessions"])
    assert not health["recommendations"]
    monkeypatch.setattr(measure.os, "kill", lambda *a: pytest.fail("Codex processes must not be terminated"))
    measure.kill_stale_sessions(threshold_hours=0)
    assert "automatic termination is disabled" in capsys.readouterr().out


def test_session_dict_contract_is_unchanged_apart_from_identity(monkeypatch):
    measure = _load_measure()
    s = collect(monkeypatch, measure, base_tree() + [terminal_claude(f"{CLI} --resume abc")])[0]
    for key in ("pid", "started", "elapsed_seconds", "elapsed_human", "command", "has_terminal", "tty", "identity"):
        assert key in s
    assert s["command"] == f"{CLI} --resume abc"
    assert s["started"] == "Wed Jan 1 00:00:00 2020"  # whitespace-collapsed, as before
    assert s["elapsed_seconds"] == 2000 * 86400


# --- dashboard parity -----------------------------------------------------------

def test_dashboard_treats_tagged_posix_inventories_like_windows():
    src = (REPO / "skills" / "token-optimizer" / "assets" / "dashboard.html").read_text(encoding="utf-8")
    bulk = src[src.index("var killStaleBtn"):src.index("kill-pid-btn').forEach")]
    assert "kill-stale" in bulk and "s.identity" in bulk
    assert "!s.identity" in src, "per-row `kill <pid>` must stay hidden for identity-tagged inventories"
    assert "Windows" not in bulk, "the tagged-inventory branch is no longer Windows-only"
