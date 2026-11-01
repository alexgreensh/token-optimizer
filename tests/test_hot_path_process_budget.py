#!/usr/bin/env python3
"""Hot-path process budget: how many processes does one hook event start?

Escapes this guards (each shipped while the suite was green):
  * 5.13.50 (78670f91): SessionStart ran ``schtasks /Query`` on EVERY Windows
    desktop session (the scheduler job check sat ahead of the cheap gates).
    Only the Windows CI saw it; macOS/Linux read a plist/unit file there.
  * #139: UserPromptSubmit started 18 processes per prompt (six hook entries,
    each its own launcher -> run.py -> module_runner chain).
  * #117: ``tasklist /v`` on a hook path, seconds per call on busy Windows boxes.

Two layers, both through the real hook code, never an inner helper:

1. ``test_real_dispatch_*``: every run.py entry of the shipped ``hooks.json``,
   run as a real process (``python hooks/run.py <script> <args>``, the argv the
   launcher execs) on whatever OS this is. A ``sitecustomize`` on PYTHONPATH
   records every process start in every Python process of the hook tree.
   run.py's own dispatch child (module_runner) runs for real; any other start
   is recorded and replaced by an empty ``python -c ""`` so nothing escapes the
   sandbox. On the Windows CI legs this is real Windows (real ``os.name``,
   ``str(Path)``, ``creationflags``). The test HOME is a sandbox, so the
   product's own guard keeps the scheduler writers off here; layer 2 covers
   that branch.

2. ``test_healthy_*``: the consolidated runners exactly as module_runner runs
   them (``runpy`` as ``__main__``), in-process, with this process placed at
   the account's real home (the branch the frozen sandbox flag hides from every
   subprocess test) and a healthy install (daemon installed, answering, current
   version). Every process-start primitive is replaced by a recorder that starts
   NOTHING, so the run is safe on a developer machine. Shapes: ``native`` (the
   host OS, so Linux on the Linux CI and real Windows on the Windows CI) and
   ``windows`` (``platform.system() == "Windows"``, the input the scheduler code
   branches on). On a non-Windows host the windows shape is a simulation:
   ``os.name`` and the path flavour stay native.
"""

from __future__ import annotations

import io
import json
import os
import platform
import re
import runpy
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402

# Processes no hook event may start, whatever its count budget: scheduler and
# process-table tools. Each costs 0.1-10 s on a busy machine and flashes a
# console on Windows. schtasks (5.13.50) and tasklist (#117) both shipped.
FORBIDDEN_ON_HOT_PATH = {
    "schtasks", "tasklist", "wmic", "powershell", "pwsh", "taskkill",
    "launchctl", "systemctl", "loginctl", "crontab", "reg", "where",
}

# Extra processes (beyond run.py's one module_runner child) a steady-state hook
# may start. Hand-set from what each event is FOR, not copied from a run:
#   Stop / SessionEnd: one detached session-end-flush worker (the flush must
#     outlive the hook); SessionStart: one detached daemon-revive child (the
#     daemon repair is kept off the SessionStart budget). Nothing else needs a
#     process: the runners call measure in-process (the #139 consolidation).
EXTRA_PROCESS_BUDGET = {"Stop": 1, "SessionEnd": 1, "SessionStart": 1}

# Python processes per hook event, all entries for one tool together: run.py +
# its module_runner child (the bash launcher in front is not counted here; its
# own budget lives in test_python_launcher_cache_hit_processes.py). #139 was
# six entries on UserPromptSubmit = 12 Python processes (18 with the shells).
PER_EVENT_PYTHON_BUDGET = 2

_TOOLS_BY_EVENT = {
    "PreToolUse": ["Read", "Bash", "Agent", "mcp__srv__tool", "Edit"],
    "PostToolUse": ["Read", "Bash", "Agent", "mcp__srv__tool", "Edit"],
    "PostToolUseFailure": ["Bash", "PowerShell"],
}


def _exe_name(argv) -> str:
    first = str(argv[0]) if argv else ""
    base = first.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".com"):
        if base.endswith(ext):
            base = base[: -len(ext)]
    return base


# Every hooks.json a host loads: the plugin, its Codex-marketplace mirror and
# the Cowork copy (Cowork runs a different event set, so it is checked as is).
SHIPPED_HOOKS_JSON = [
    HOOKS / "hooks.json",
    REPO / "plugins" / "token-optimizer" / "hooks" / "hooks.json",
    REPO / "cowork" / "token-optimizer" / "hooks" / "hooks.json",
]


def _hooks_json_entries(path=HOOKS / "hooks.json"):
    """(event, matcher, run.py argv tail, async) for every run.py entry in a
    shipped hooks.json, parsed from the real command strings."""
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for event, groups in cfg["hooks"].items():
        for group in groups:
            for hook in group.get("hooks", []):
                m = re.search(r'hooks/run\.py"? (.*?); done', hook.get("command", ""))
                if m:
                    out.append((event, group.get("matcher"), m.group(1).split(),
                                bool(hook.get("async"))))
    return out


def _matches(matcher, tool):
    return matcher in (None, "", "*") or re.fullmatch(matcher, tool) is not None


# --------------------------------------------------------------------------
# Layer 1: the real dispatch, real processes, real OS
# --------------------------------------------------------------------------

_RECORDER_SITECUSTOMIZE = textwrap.dedent(r'''
    import json as _json, os as _os, sys as _sys
    _LOG = _os.environ.get("TO_SPAWN_LOG")
    if _LOG:
        import subprocess as _sp

        def _w(kind, argv, **extra):
            try:
                row = {"pid": _os.getpid(), "kind": kind, "argv": argv}
                row.update(extra)
                with open(_LOG, "a", encoding="utf-8") as _fh:
                    _fh.write(_json.dumps(row) + "\n")
            except Exception:
                pass

        def _norm(args):
            if isinstance(args, (list, tuple)):
                return [a.decode("utf-8", "replace") if isinstance(a, bytes)
                        else _os.fspath(a) for a in args]
            return str(args).split()

        _w("boot", [])
        _Real = _sp.Popen
        # Popen starts its child through an os primitive (os.posix_spawn on
        # Linux 3.13+, fork/exec elsewhere). Inside a recorded Popen those calls
        # are the same process: they pass through to the real primitive and are
        # not recorded again. Per thread, so concurrent spawns stay separate.
        import threading as _threading
        _tls = _threading.local()

        class _RecPopen(_Real):
            def __init__(self, args, *a, **kw):
                argv = _norm(args)
                dispatch = any(str(x).replace("\\", "/").endswith("hooks/module_runner.py")
                               for x in argv[:2])
                flags = kw.get("creationflags", 0) or 0
                if not dispatch:
                    # Recorded, never run: nothing escapes the sandbox. The
                    # stand-in is started with the caller's own kwargs
                    # (creationflags included), so CreateProcess accepts or
                    # refuses it exactly as it would the real child.
                    args = [_sys.executable, "-c", ""]
                    kw.pop("executable", None)
                    kw["shell"] = False
                # A process exists only once Popen returns. spawn_detached on
                # Windows retries ONCE without CREATE_BREAKAWAY_FROM_JOB when
                # the parent's job object refuses breakaway (the GitHub runner
                # does); the refused attempt starts nothing and must not count.
                _tls.inside = getattr(_tls, "inside", 0) + 1
                try:
                    super().__init__(args, *a, **kw)
                except BaseException as exc:
                    _w("failed", argv, dispatch=dispatch, flags=flags,
                       error=type(exc).__name__)
                    raise
                finally:
                    _tls.inside -= 1
                _w("dispatch" if dispatch else "popen", argv, flags=flags,
                   child=self.pid)

        _sp.Popen = _RecPopen

        def _rec(name, real):
            def _fn(*a, **k):
                if getattr(_tls, "inside", 0):
                    return real(*a, **k)  # Popen's own start: already recorded
                _w(name, [str(x) for x in a[:3]])
                return 0
            return _fn

        for _n in ("system", "startfile", "spawnv", "spawnve", "spawnvp", "spawnvpe",
                   "spawnl", "spawnle", "spawnlp", "spawnlpe", "posix_spawn",
                   "posix_spawnp"):
            if hasattr(_os, _n):
                setattr(_os, _n, _rec(_n, getattr(_os, _n)))
''')


def _real_dispatch(tmp_path, tail, payload, log):
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir(exist_ok=True)
    (rec_dir / "sitecustomize.py").write_text(_RECORDER_SITECUSTOMIZE, encoding="utf-8")
    env = dict(os.environ)
    env["TO_SPAWN_LOG"] = str(log)
    env["PYTHONPATH"] = str(rec_dir) + (os.pathsep + env["PYTHONPATH"]
                                        if env.get("PYTHONPATH") else "")
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO)
    # The recorder replaces every child with a stand-in, including the #264
    # background compiler, so a cold round would wait for a compile that
    # never happens (Windows: hung to the 120 s timeout; Linux: hooks failed
    # open and a planted Stop double-spawn went unseen). Gate off = round 1
    # compiles inline and writes the .pyc; the asserted round is the real
    # warm steady state, gate check included. The gate has its own tests.
    env["TOKEN_OPTIMIZER_COLD_COMPILE_WAIT_S"] = "-1"
    env.pop("TOKEN_OPTIMIZER_RUNTIME", None)
    proc = subprocess.run([sys.executable, str(HOOKS / "run.py"), *tail],
                          input=json.dumps(payload), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env,
                          cwd=str(tmp_path), timeout=120)
    rows = []
    if log.exists():
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
                if line.strip()]
    return proc, rows


def _payload(event, sid, cwd, tool="Read"):
    return {"hook_event_name": event, "session_id": sid, "cwd": str(cwd),
            "transcript_path": str(Path(cwd) / f"{sid}.jsonl"),
            "tool_name": tool, "tool_input": {"file_path": str(Path(cwd) / "x.txt"),
                                              "command": "ls"},
            "tool_response": {"content": "x", "stdout": "x"},
            "prompt": "hello", "source": "startup", "trigger": "auto"}


@pytest.fixture(scope="module")
def real_dispatch_runs(tmp_path_factory):
    """Every run.py entry of hooks.json, twice (round 1 warms first-run work
    such as the consent bootstrap and the runtime-detection cache; round 2 is
    the steady state every later hook pays). Returns the round-2 records."""
    tmp_path = tmp_path_factory.mktemp("real-dispatch")
    results = {}
    for rnd in (1, 2):
        for i, (event, matcher, tail, _async) in enumerate(_hooks_json_entries()):
            log = tmp_path / f"spawns-{rnd}-{i}.jsonl"
            proc, rows = _real_dispatch(tmp_path, tail, _payload(event, f"real-{rnd}", tmp_path),
                                        log)
            if rnd == 2:
                results[i] = (event, matcher, tail, proc, rows)
    return results


def test_real_dispatch_recorder_is_live(real_dispatch_runs):
    """Non-vacuity: the recorder loaded in run.py AND in the hook child, and
    run.py dispatched every entry. Without this a dead recorder (another
    sitecustomize earlier on the path, an -I/-E interpreter flag) would make
    every budget below pass on zero recorded processes."""
    assert real_dispatch_runs, "hooks.json has no run.py entries"
    for event, _m, tail, proc, rows in real_dispatch_runs.values():
        boot_pids = {r["pid"] for r in rows if r["kind"] == "boot"}
        dispatches = [r for r in rows if r["kind"] == "dispatch"]
        assert proc.returncode == 0, (event, tail, proc.stderr[-2000:])
        assert len(dispatches) == 1, (
            f"{event} {tail}: run.py must start exactly one module_runner child, "
            f"recorded {len(dispatches)}; stderr: {proc.stderr[-1500:]}")
        assert len(boot_pids) >= 2, (
            f"{event} {tail}: recorder did not load in both run.py and its child "
            f"(boots in pids {sorted(boot_pids)})")


def test_real_dispatch_starts_no_scheduler_or_process_table_tool(real_dispatch_runs):
    """No hook event starts schtasks / tasklist / powershell / launchctl / ...
    in steady state, on the real OS this suite runs on."""
    bad = []
    for event, _m, tail, _proc, rows in real_dispatch_runs.values():
        for r in rows:
            # Attempts count here even when CreateProcess refused them.
            if r["kind"] not in ("boot", "dispatch") and _exe_name(r["argv"]) in FORBIDDEN_ON_HOT_PATH:
                bad.append((event, tail[0], r["argv"][:4]))
    assert not bad, f"hook events started scheduler/process-table tools: {bad}"


def test_real_dispatch_extra_processes_within_budget(real_dispatch_runs):
    """Per entry: processes started beyond run.py's dispatch child."""
    over = []
    for event, _m, tail, _proc, rows in real_dispatch_runs.values():
        # Processes that actually started (a refused CreateProcess is not one).
        extra = [r["argv"][:4] for r in rows if r["kind"] not in ("boot", "dispatch", "failed")]
        if len(extra) > EXTRA_PROCESS_BUDGET.get(event, 0):
            refused = [(r["argv"][:3], r.get("error"), r.get("flags")) for r in rows
                       if r["kind"] == "failed"]
            over.append((event, tail[0], len(extra), extra, {"refused_attempts": refused}))
    assert not over, f"hook entries over their process budget: {over}"


@pytest.mark.parametrize("spawn_path", ["default", "posix_spawn"])
def test_recorder_counts_started_processes_not_refused_attempts(tmp_path, spawn_path):
    """Windows CI 5.13.51: Stop/SessionEnd showed two identical flush-worker
    records. _defer_session_end_flush calls spawn_detached once, and
    spawn_detached's only second Popen is its retry without
    CREATE_BREAKAWAY_FROM_JOB after the runner's job object refuses breakaway:
    one process, two attempts. This pins the recorder's rule on every OS with
    a real refusal (a missing cwd makes CreateProcess/exec fail): a refused
    attempt then a successful retry is ONE started process, while two
    successful starts stay two.

    ``posix_spawn``: Linux CI 3.13/3.14 starts children through os.posix_spawn
    inside Popen, which the recorder also hooks, so one process was recorded
    twice (and the hooked primitive faked the start). Forced here on every
    POSIX host (``_PYTHON_SUBPROCESS_USE_POSIX_SPAWN=1``, close_fds=False since
    macOS lacks POSIX_SPAWN_CLOSEFROM) so the path is proven off Linux too."""
    if spawn_path == "posix_spawn" and not hasattr(os, "posix_spawn"):
        pytest.skip("no os.posix_spawn on this platform (Windows)")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    (rec_dir / "sitecustomize.py").write_text(_RECORDER_SITECUSTOMIZE, encoding="utf-8")
    log = tmp_path / "spawns.jsonl"
    kw = "close_fds=False" if spawn_path == "posix_spawn" else ""
    script = textwrap.dedent(f'''
        import subprocess, sys
        try:
            subprocess.Popen(["schtasks-stand-in", "/x"], cwd={str(tmp_path / "missing")!r}).wait()
        except OSError:
            p = subprocess.Popen(["schtasks-stand-in", "/x"], {kw})   # the retry
            assert p.wait() == 0 and p.pid > 0, (p.pid, p.returncode)
        p = subprocess.Popen(["second-real-start"], {kw})
        assert p.wait() == 0 and p.pid > 0, (p.pid, p.returncode)
    ''')
    env = dict(os.environ, TO_SPAWN_LOG=str(log), PYTHONPATH=str(rec_dir))
    env.pop("_PYTHON_SUBPROCESS_USE_POSIX_SPAWN", None)
    if spawn_path == "posix_spawn":
        env["_PYTHON_SUBPROCESS_USE_POSIX_SPAWN"] = "1"
    proc = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    kinds = [(r["kind"], Path(r["argv"][0]).name if r["argv"] else "") for r in rows
             if r["kind"] != "boot"]
    assert kinds == [("failed", "schtasks-stand-in"), ("popen", "schtasks-stand-in"),
                     ("popen", "second-real-start")], kinds


@pytest.mark.parametrize("hooks_json", SHIPPED_HOOKS_JSON,
                         ids=lambda p: str(p.relative_to(REPO)))
def test_hooks_json_python_processes_per_event_within_budget(hooks_json):
    """#139 shape: an event (for one tool) fans out to many hooks.json entries,
    each a launcher -> run.py -> module_runner chain. Two Python processes per
    event is the consolidated design; an async entry still costs processes."""
    entries = _hooks_json_entries(hooks_json)
    assert entries, f"could not parse any run.py entry from {hooks_json}"
    counts = {}
    for event, matcher, _tail, _async in entries:
        for tool in _TOOLS_BY_EVENT.get(event, [None]):
            if tool is not None and not _matches(matcher, tool):
                continue
            counts[(event, tool)] = counts.get((event, tool), 0) + 2
    over = {k: v for k, v in counts.items() if v > PER_EVENT_PYTHON_BUDGET
            # PreCompact runs once per compaction, not per prompt or tool call;
            # its three entries (instructions, capture, cache clear) are off the
            # per-turn hot path. Every per-turn event stays at the budget.
            and k[0] != "PreCompact"}
    assert not over, f"events over {PER_EVENT_PYTHON_BUDGET} Python processes: {over}"
    assert counts.get(("UserPromptSubmit", None)) == 2, counts


# --------------------------------------------------------------------------
# Layer 2: healthy install at the account's real home, in-process
# --------------------------------------------------------------------------


class _Recorder:
    def __init__(self):
        self.calls = []

    def names(self):
        return [_exe_name(c) for c in self.calls]


def _install_recorder(monkeypatch) -> _Recorder:
    """Replace every process-start primitive with a recorder that starts
    nothing. A healthy-install run at the account home must never reach a real
    scheduler from a test, so this is the safety net as much as the probe."""
    rec = _Recorder()

    class _FakePopen:
        def __init__(self, args, *a, **kw):
            rec.calls.append([os.fspath(x) if not isinstance(x, bytes) else x.decode()
                              for x in args] if isinstance(args, (list, tuple))
                             else str(args).split())
            self.args = args
            self.pid = 0
            self.returncode = 0
            self._text = bool(kw.get("text") or kw.get("universal_newlines")
                              or kw.get("encoding") or kw.get("errors"))
            self.stdin = self.stdout = self.stderr = None

        def _empty(self):
            return "" if self._text else b""

        def communicate(self, input=None, timeout=None):
            return self._empty(), self._empty()

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            pass

        terminate = kill

        def send_signal(self, sig):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _record(name):
        def _fn(*a, **k):
            rec.calls.append([name] + [str(x) for x in a[:2]])
            if name.startswith("exec"):
                raise OSError("exec blocked by the process-budget test")
            return 0
        return _fn

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    for name in ("system", "startfile", "posix_spawn", "posix_spawnp", "spawnv",
                 "spawnve", "spawnvp", "spawnvpe", "spawnl", "spawnle", "spawnlp",
                 "spawnlpe", "execv", "execve", "execvp", "execvpe", "execl",
                 "execle", "execlp", "execlpe"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, _record(name))
    return rec


@pytest.fixture
def healthy_install(tmp_path, monkeypatch):
    """This process at the account's real home with a healthy daemon, so every
    hot path takes its steady-state branch and the scheduler guard's job check
    is reachable (a sandboxed HOME answers 'refused' before it)."""
    home = tmp_path / "home"
    home.mkdir()
    for var in ("TOKEN_OPTIMIZER_ALLOW_FOREIGN_HOME_SCHEDULER", "CLAUDE_CODE_REMOTE",
                "CLAUDE_CODE_REMOTE_SESSION_ID", "TOKEN_OPTIMIZER_REMOTE_HOST",
                "TOKEN_OPTIMIZER_CLOUD_KIT_VERSION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(measure, "_real_account_home", lambda: home)
    monkeypatch.setattr(measure, "HOME", home)
    monkeypatch.setattr(measure, "_SNAPSHOT_DIR_OVERRIDE", "")
    monkeypatch.setattr(measure, "_HOME_REDIRECTED", measure._compute_home_redirected())
    assert measure._daemon_snapshot_sandboxed() is False
    # The healthy install: service registered, answering, current version.
    monkeypatch.setattr(measure, "_daemon_service_installed", lambda *a, **k: True)
    monkeypatch.setattr(measure, "_verify_daemon_port", lambda *a, **k: True)
    monkeypatch.setattr(measure, "_daemon_served_version",
                        lambda *a, **k: measure.TOKEN_OPTIMIZER_VERSION)

    # The runners' watchdog calls os._exit on expiry: never arm it in pytest.
    class _NoDeadline:
        def __init__(self, *a, **k):
            pass

        def start(self):
            return self

        def cancel(self):
            pass

        def remaining(self):
            return 18.0

    monkeypatch.setattr(measure, "HookDeadline", _NoDeadline)
    measure._reset_job_guard_cache()
    yield {"home": home, "tmp": tmp_path}
    measure._reset_job_guard_cache()


def _run_runner(name, payload, monkeypatch):
    """hooks/<name>.py run exactly as module_runner runs it (as __main__)."""
    raw = json.dumps(payload).encode("utf-8")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8"))
    monkeypatch.setattr(sys, "argv", [name])
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    try:
        runpy.run_path(str(HOOKS / f"{name}.py"), run_name="__main__")
    except SystemExit:
        pass
    return out.getvalue()


def _follow_detached_children(rec, monkeypatch):
    """A detached ``measure.py daemon-revive`` child is part of what a
    SessionStart costs: run its dispatch (``_ensure_dashboard_daemon(force=True)``,
    measure.py's daemon-revive branch) in-process so ITS starts are recorded too."""
    followed = 0
    for argv in list(rec.calls):
        if len(argv) >= 3 and Path(str(argv[1])).name == "measure.py" and argv[2] == "daemon-revive":
            measure._ensure_dashboard_daemon(force=True)
            followed += 1
    return followed


_SHAPES = ["native", "windows"]


def _apply_shape(shape, monkeypatch):
    if shape == "windows":
        monkeypatch.setattr(platform, "system", lambda: "Windows")
    measure._reset_job_guard_cache()


@pytest.mark.parametrize("shape", _SHAPES)
def test_healthy_session_start_starts_no_scheduler_query(shape, healthy_install, monkeypatch):
    """5.13.50: every healthy Windows SessionStart ran ``schtasks /Query``.
    A healthy SessionStart (and the daemon-revive child it fires) must start
    no scheduler or process-table tool, and at most its one detached child."""
    _apply_shape(shape, monkeypatch)
    rec = _install_recorder(monkeypatch)
    cwd = healthy_install["tmp"]
    # Session 1 bootstraps consent and first-run state; session 2 is the
    # steady state every later session start pays.
    _run_runner("sessionstart_runner", _payload("SessionStart", "warm-1", cwd), monkeypatch)
    rec.calls.clear()
    measure._reset_job_guard_cache()
    _run_runner("sessionstart_runner", _payload("SessionStart", "steady-2", cwd), monkeypatch)
    started = list(rec.calls)
    assert len(started) <= EXTRA_PROCESS_BUDGET["SessionStart"], started
    followed = _follow_detached_children(rec, monkeypatch)
    forbidden = [c for c in rec.calls if _exe_name(c) in FORBIDDEN_ON_HOT_PATH]
    assert not forbidden, (
        f"[{shape}] healthy SessionStart started {forbidden} "
        f"(followed {followed} daemon-revive child)")


@pytest.mark.parametrize("shape", _SHAPES)
def test_healthy_prompt_and_tool_hooks_start_nothing(shape, healthy_install, monkeypatch):
    """#139 / #117 shape: per-prompt and per-tool hooks of a healthy session
    start no process at all; they run in the hook process."""
    _apply_shape(shape, monkeypatch)
    rec = _install_recorder(monkeypatch)
    cwd = healthy_install["tmp"]
    _run_runner("sessionstart_runner", _payload("SessionStart", "s-1", cwd), monkeypatch)
    _run_runner("userpromptsubmit_runner", _payload("UserPromptSubmit", "s-1", cwd), monkeypatch)
    rec.calls.clear()
    measure._reset_job_guard_cache()
    per_hook = {}
    for name, event, tool in (("userpromptsubmit_runner", "UserPromptSubmit", None),
                              ("posttooluse_runner", "PostToolUse", "Bash"),
                              ("posttooluse_runner", "PostToolUse", "Read")):
        before = len(rec.calls)
        _run_runner(name, _payload(event, "s-1", cwd, tool=tool or "Read"), monkeypatch)
        per_hook[f"{event}:{tool}"] = rec.calls[before:]
    over = {k: v for k, v in per_hook.items() if v}
    assert not over, f"[{shape}] steady-state hooks started processes: {over}"


@pytest.mark.parametrize("shape", _SHAPES)
def test_healthy_stop_starts_only_its_flush_worker(shape, healthy_install, monkeypatch):
    _apply_shape(shape, monkeypatch)
    rec = _install_recorder(monkeypatch)
    cwd = healthy_install["tmp"]
    _run_runner("sessionstart_runner", _payload("SessionStart", "st-1", cwd), monkeypatch)
    rec.calls.clear()
    _run_runner("stop_runner", _payload("Stop", "st-1", cwd), monkeypatch)
    forbidden = [c for c in rec.calls if _exe_name(c) in FORBIDDEN_ON_HOT_PATH]
    assert not forbidden, forbidden
    assert len(rec.calls) <= EXTRA_PROCESS_BUDGET["Stop"], rec.calls
