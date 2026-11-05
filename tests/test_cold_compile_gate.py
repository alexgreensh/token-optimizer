#!/usr/bin/env python3
"""#264: a hook must never compile measure.py on the host's hook deadline.

Claude desktop on Windows starts hooks with PYTHONDONTWRITEBYTECODE=1, and
compiling the 2.7 MB measure.py there took 20-28 s against host timeouts of
15 s (Stop) and 20 s (UserPromptSubmit, SessionStart). Python writes the .pyc
only after the compile finishes, so a hook killed mid-compile never warmed the
cache and every call timed out again. hooks/module_runner.py now hands the cold
compile to ONE detached process and waits for it only as long as the host
deadline allows.

Everything here runs the real entry point (``python hooks/run.py <runner>``,
the argv the launcher execs) as real processes on whatever OS this is. A slow
machine is simulated with a ``sitecustomize`` that makes compiling measure.py
sleep, in the hook AND in the background compiler (both compile through
``SourceFileLoader.source_to_code``; py_compile does too). The same
sitecustomize logs every process start and, at exit, whether the process
loaded measure and whether it compiled it.
"""

from __future__ import annotations
from helpers.measure_split import exec_source_module as _split_exec_module

import importlib.util
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
MEASURE = SCRIPTS / "measure.py"
COMPILER_TAG = "token-optimizer-bytecode-warmup"


def _gate_module():
    spec = importlib.util.spec_from_file_location('split_gate_under_test', HOOKS / 'module_runner.py')
    module = importlib.util.module_from_spec(spec)
    _split_exec_module(module)
    return module


def test_warm_facade_does_not_hide_a_cold_domain(tmp_path, monkeypatch):
    gate = _gate_module()
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    (scripts / 'measure.py').write_text('VALUE = 1\n')
    package = scripts / '_measure'
    package.mkdir()
    for name in ('__init__', '_compat', 'runtime', 'pricing', 'audit', 'dashboard',
                 'lifecycle', 'sessions', 'trends', 'cache_policy', 'daemon',
                 'quality_archive', 'continuity', 'savings', 'cli'):
        (package / (name + '.py')).write_text('VALUE = 1\n')
    monkeypatch.setattr(gate, '_bytecode_path', lambda src: str(tmp_path / 'cache' / (Path(src).name + '.pyc')))
    monkeypatch.setattr(gate, '_bytecode_fresh', lambda src, cache: Path(src).name != 'runtime.py')
    monkeypatch.setattr(gate, '_start_background_compile', lambda *args: type('Child', (), {'poll': lambda self: None})())
    assert gate._measure_bytecode_ready(str(scripts), 0) is False


def test_failure_marker_is_invalidated_by_a_package_only_source_change(tmp_path):
    gate = _gate_module()
    scripts = tmp_path / 'scripts'
    package = scripts / '_measure'
    package.mkdir(parents=True)
    (scripts / 'measure.py').write_text('VALUE = 1\n')
    for name in gate._MEASURE_PACKAGE_SOURCES:
        (package / name).write_text('VALUE = 1\n')
    marker = tmp_path / 'failed'
    source = str(scripts / 'measure.py')
    marker.write_text(gate._source_set_key(source))
    assert gate._compile_failed_for(str(marker), source)
    (package / 'runtime.py').write_text('VALUE = 12345\n')
    assert not gate._compile_failed_for(str(marker), source)

# Simulated compile time for measure.py, and the hook's wait for the
# background compile. A hook that compiled inline would take >= SLOW_S.
SLOW_S = 8.0
WAIT_S = 1.0
# Fail-open budget for one hook: the wait plus interpreter start-up and
# run.py's own imports (slower on the Windows CI runners). Well under SLOW_S.
FAST_EXIT_S = 6.0
# Upper bound for "did not hang" checks, where startup cost on a loaded runner varies.
HANG_S = 30.0

_SITECUSTOMIZE = textwrap.dedent(r'''
    import json as _json, os as _os, sys as _sys, time as _time
    _LOG = _os.environ.get("TO_COLD_LOG")
    if _LOG:
        _compiler = len(_sys.argv) > 1 and _sys.argv[1] == "%(tag)s"
        _state = {"compiled_measure": False}

        def _w(kind, **extra):
            try:
                row = {"pid": _os.getpid(), "kind": kind, "compiler": _compiler,
                       "argv": [str(a) for a in _sys.argv[:3]] if not _compiler else []}
                row.update(extra)
                # One file per process: concurrent appends to a shared file are
                # not atomic on Windows and dropped rows there (CI, 5.13.51).
                with open("%%s.%%d" %% (_LOG, _os.getpid()), "a", encoding="utf-8") as fh:
                    fh.write(_json.dumps(row) + "\n")
            except Exception:
                pass

        _w("boot")
        from importlib import machinery as _machinery
        _real = _machinery.SourceFileLoader.source_to_code

        def _source_to_code(self, data, path, *a, **k):
            if str(path).replace("\\", "/").endswith(_os.environ.get("TO_COLD_TARGET", "/measure.py")):
                _state["compiled_measure"] = True
                _w("compile-measure")
                slow = float(_os.environ.get("TO_COLD_SLOW_S") or 0)
                if slow:
                    _time.sleep(slow)
                if _compiler and _os.environ.get("TO_COLD_COMPILER_FAILS"):
                    raise SyntaxError("planted compile failure")
            return _real(self, data, path, *a, **k)

        _machinery.SourceFileLoader.source_to_code = _source_to_code

        import atexit as _atexit

        def _at_exit():
            _w("exit", measure_loaded="measure" in _sys.modules,
               compiled_measure=_state["compiled_measure"])

        _atexit.register(_at_exit)
''' % {"tag": COMPILER_TAG})


class Bench:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        site = tmp_path / "site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
        self.prefix = tmp_path / "pycache-prefix"
        self.home = tmp_path / "home"
        cfg = self.home / ".claude" / "token-optimizer" / "config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({"v5_welcome_shown": True, "enterprise_consent_shown": True}),
                       encoding="utf-8")
        self.proj = tmp_path / "proj"
        self.proj.mkdir()
        self.log = tmp_path / "procs.jsonl"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("CLAUDE", "CODEX", "TOKEN_OPTIMIZER", "TO_COLD_"))
                    and k not in ("PYTHONPYCACHEPREFIX", "PYTHONDONTWRITEBYTECODE")}
        self.env.update({
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "LOCALAPPDATA": str(self.home / "AppData" / "Local"),
            # The runners bootstrap config (ensure-health): pin it to the fixture.
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "CLAUDE_PLUGIN_ROOT": str(REPO),
            "TOKEN_OPTIMIZER_NO_PROC_SCAN": "1",
            # What the Claude desktop app sets (#264).
            "PYTHONDONTWRITEBYTECODE": "1",
            # A private bytecode cache, so every test starts cold and the
            # repo's own __pycache__ is never touched. run.py leaves a user's
            # PYTHONPYCACHEPREFIX alone and passes it to the hook.
            "PYTHONPYCACHEPREFIX": str(self.prefix),
            "PYTHONPATH": str(site),
            "TO_COLD_LOG": str(self.log),
        })

    def measure_pyc(self) -> Path:
        saved = sys.pycache_prefix
        sys.pycache_prefix = str(self.prefix)
        try:
            return Path(importlib.util.cache_from_source(str(MEASURE)))
        finally:
            sys.pycache_prefix = saved

    def pyc_fresh(self) -> bool:
        try:
            head = self.measure_pyc().read_bytes()[:16]
        except OSError:
            return False
        st = MEASURE.stat()
        return (len(head) == 16 and head[:4] == importlib.util.MAGIC_NUMBER
                and int.from_bytes(head[8:12], "little") == int(st.st_mtime) & 0xFFFFFFFF
                and int.from_bytes(head[12:16], "little") == st.st_size & 0xFFFFFFFF)

    def payload(self, event="UserPromptSubmit"):
        return {"hook_event_name": event, "session_id": "cold-compile-264",
                "cwd": str(self.proj), "transcript_path": str(self.proj / "t.jsonl"),
                "prompt": "add tests for the parser", "source": "startup",
                "stop_hook_active": False, "permission_mode": "default"}

    def start(self, runner="hooks/userpromptsubmit_runner.py", event="UserPromptSubmit", args=(), **env):
        proc = subprocess.Popen([sys.executable, str(HOOKS / "run.py"), runner, *args],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=str(self.proj),
                                env={**self.env, **env})
        return proc, json.dumps(self.payload(event)), time.monotonic()

    @staticmethod
    def finish(started, timeout=120):
        proc, payload, t0 = started
        out, err = proc.communicate(payload.encode("utf-8"), timeout=timeout)
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), \
            time.monotonic() - t0

    def fire(self, **kw):
        return self.finish(self.start(**kw))

    def rows(self):
        rows = []
        for f in sorted(self.log.parent.glob(self.log.name + ".*")):
            rows += [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()
                     if line.strip()]
        return rows

    def compiler_boots(self):
        return [r for r in self.rows() if r["kind"] == "boot" and r["compiler"]]

    def hook_exits(self):
        """Exit records of the hook processes (module_runner children)."""
        hook_pids = {r["pid"] for r in self.rows() if r["kind"] == "boot"
                     and any(a.replace("\\", "/").endswith("hooks/module_runner.py")
                             for a in r["argv"][:2])}
        return [r for r in self.rows() if r["kind"] == "exit" and r["pid"] in hook_pids]

    def wait_for_lock_release(self, timeout=30.0):
        lock = self.measure_pyc().parent / ".token-optimizer-compile.lock"
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not lock.exists():
                return True
            time.sleep(0.1)
        return False

    def wait_for_pyc(self, timeout=150.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.pyc_fresh():
                return True
            time.sleep(0.2)
        return False

    def clear_log(self):
        for f in self.log.parent.glob(self.log.name + ".*"):
            try:
                f.unlink()
            except OSError:
                pass


def _valid_envelope(out: str) -> bool:
    """Claude Code accepts no output, or one JSON object (or plain context text
    for UserPromptSubmit / SessionStart)."""
    text = out.strip()
    if not text or not text.startswith("{"):
        return True
    try:
        return isinstance(json.loads(text), dict)
    except ValueError:
        return False


@pytest.fixture
def bench(tmp_path):
    return Bench(tmp_path)


def test_slow_cold_compile_runs_once_in_the_background_and_hooks_fail_open(bench):
    """The #264 machine: compiling measure.py takes longer than the host allows.
    Concurrent hooks finish fast with a valid (empty) envelope, exactly one
    background compile starts, the .pyc lands in the active pycache prefix,
    and the next hook runs the normal path from it."""
    slow = {"TO_COLD_SLOW_S": str(SLOW_S), "TOKEN_OPTIMIZER_COLD_COMPILE_WAIT_S": str(WAIT_S)}
    started = [bench.start(**slow) for _ in range(4)]
    results = [bench.finish(s) for s in started]
    for rc, out, err, elapsed in results:
        assert rc == 0, err[-2000:]
        assert elapsed < FAST_EXIT_S, (
            f"hook took {elapsed:.1f}s: it compiled measure.py on its own deadline "
            f"(or waited on the compiler's pipe); stderr={err[-1500:]!r}")
        assert _valid_envelope(out), out[:500]
    exits = bench.hook_exits()
    assert len(exits) == 4, bench.rows()
    assert not any(r["compiled_measure"] or r["measure_loaded"] for r in exits), exits

    assert len(bench.compiler_boots()) == 1, (
        f"{len(bench.compiler_boots())} background compiles for 4 concurrent cold hooks")
    assert bench.wait_for_pyc(), f"no fresh measure .pyc under the prefix: {bench.measure_pyc()}"
    # The compiler writes the .pyc, then releases its lock: wait for the
    # release instead of racing it (a slow runner lost that race in CI).
    assert bench.wait_for_lock_release(), "the compile lock was never released"

    bench.clear_log()
    rc, out, err, elapsed = bench.fire(**slow)
    assert rc == 0, err[-2000:]
    # No wall-clock bound here: a warm hook runs the full normal path, which a
    # loaded Windows runner can take 7s for. What matters is checked directly:
    # no new compile, and measure loaded from the cache instead of compiled.
    assert _valid_envelope(out), out[:500]
    assert not bench.compiler_boots(), "a warm cache started another compile"
    [warm] = bench.hook_exits()
    assert warm["measure_loaded"] and not warm["compiled_measure"], warm


def test_fast_cold_compile_inside_the_wait_runs_the_hook_as_before(bench):
    """A machine that compiles within the wait (any Mac or Linux box) sees the
    old behaviour: the hook waits for the one background compile, then loads
    measure from the fresh .pyc and does its work. (The wait is explicit: a
    Windows CI runner may compile slower than the shipped wait.)"""
    rc, out, err, _elapsed = bench.fire(TOKEN_OPTIMIZER_COLD_COMPILE_WAIT_S="300")
    assert rc == 0, err[-2000:]
    assert _valid_envelope(out), out[:500]
    assert len(bench.compiler_boots()) == 1
    [hook] = bench.hook_exits()
    assert hook["measure_loaded"], "the hook failed open although the compile finished in time"
    assert not hook["compiled_measure"], "the hook compiled measure.py itself"
    assert bench.pyc_fresh()


def test_cold_domain_with_warm_facade_compiles_in_the_background(bench):
    import py_compile
    destination = bench.measure_pyc()
    destination.parent.mkdir(parents=True, exist_ok=True)
    py_compile.compile(str(MEASURE), cfile=str(destination), doraise=True,
                       invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
    assert bench.pyc_fresh()
    slow = {'TO_COLD_TARGET': '/_measure/runtime.py', 'TO_COLD_SLOW_S': str(SLOW_S),
            'TOKEN_OPTIMIZER_COLD_COMPILE_WAIT_S': str(WAIT_S)}
    rc, out, err, elapsed = bench.fire(**slow)
    assert rc == 0 and _valid_envelope(out), err[-2000:]
    assert elapsed < FAST_EXIT_S, 'the cold domain compiled on the hook deadline'
    [cold] = bench.hook_exits()
    assert not cold['compiled_measure'] and not cold['measure_loaded']
    assert len(bench.compiler_boots()) == 1
    assert bench.wait_for_lock_release(), 'package compile did not release the shared lock'
    bench.clear_log()
    rc, out, err, _ = bench.fire(**slow)
    assert rc == 0 and _valid_envelope(out), err[-2000:]
    [warm] = bench.hook_exits()
    assert warm['measure_loaded'] and not warm['compiled_measure']
    assert not bench.compiler_boots()


def test_failed_background_compile_falls_back_to_the_inline_compile(bench):
    """A compile that cannot succeed must not leave every later hook failing
    open: the hook compiles inline as it did before the gate (and that inline
    import writes the cache)."""
    rc, _out, err, _elapsed = bench.fire(TO_COLD_COMPILER_FAILS="1",
                                        TOKEN_OPTIMIZER_COLD_COMPILE_WAIT_S="20")
    assert rc == 0, err[-2000:]
    assert len(bench.compiler_boots()) == 1
    [hook] = bench.hook_exits()
    assert hook["measure_loaded"] and hook["compiled_measure"], hook
    assert bench.pyc_fresh()


def test_live_lock_defers_to_the_running_compile_and_stale_lock_is_taken_over(bench):
    """A fresh lock means another hook's compile is running: no second compile,
    fail open after the wait. A lock past its stale age belongs to a compile
    that died: the next hook starts a new one."""
    lock = bench.measure_pyc().parent / ".token-optimizer-compile.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("4242 0\n", encoding="utf-8")
    rc, out, err, elapsed = bench.fire(TOKEN_OPTIMIZER_COLD_COMPILE_WAIT_S=str(WAIT_S))
    assert rc == 0 and _valid_envelope(out), err[-2000:]
    # No compile is running behind this fake lock, so the one timing failure is a
    # hook that waits on it for good. A cold Windows runner needs 12s just to
    # start the hook, so the bound is generous; the no-second-compile rule below
    # is what this test is about.
    assert elapsed < HANG_S, f"hook waited {elapsed:.1f}s on a lock nobody will release"
    assert not bench.compiler_boots(), "started a compile while another one holds the lock"
    assert not bench.pyc_fresh()

    old = time.time() - 600
    os.utime(lock, (old, old))
    bench.clear_log()
    # The compiler is detached, so the hook need not wait for it. Checking the
    # lock right after the hook exits keeps a broken takeover a fast failure
    # instead of a 2-minute timeout.
    rc, out, err, _elapsed = bench.fire(TOKEN_OPTIMIZER_COLD_COMPILE_WAIT_S=str(WAIT_S))
    assert rc == 0 and _valid_envelope(out), err[-2000:]
    try:
        still_stale = lock.read_text(encoding="utf-8") == "4242 0\n"
    except FileNotFoundError:
        still_stale = False  # taken over and already released
    assert not still_stale, "the dead compile's stale lock was never taken over"
    assert bench.wait_for_pyc()
    assert len(bench.compiler_boots()) == 1


# --------------------------------------------------------------------------- #
# Every gated entry's wait fits under the host timeout of every manifest that
# dispatches it.
# --------------------------------------------------------------------------- #

# Interpreter start-up, run.py, loading the fresh .pyc and the hook's own work
# (1.0-1.5 s warm on the #264 machine), with room to spare. The Codex subagent
# bridge has a 6 s host timeout and a 2.5 s budget of its own.
_RESERVE_S = 8.0
_RESERVE_OVERRIDES_S = {"codex_hook_bridge": 4.5}


def _module_runner():
    spec = importlib.util.spec_from_file_location("_module_runner_264", HOOKS / "module_runner.py")
    mod = importlib.util.module_from_spec(spec)
    _split_exec_module(mod)
    return mod


def _gated_waits():
    return _module_runner()._COLD_COMPILE_WAIT_S


def _hook_args(command: str, target: str) -> list[str]:
    """The arguments a manifest command passes after <target>.py."""
    m = re.search(r"(?:hooks|scripts)/" + re.escape(target) + r"\.py([^;\"]*)", command)
    return m.group(1).split() if m else []


def _manifest_entries():
    out = []
    for rel in ("hooks/hooks.json", "plugins/token-optimizer/hooks/hooks.json",
                "cowork/token-optimizer/hooks/hooks.json"):
        data = json.loads((REPO / rel).read_text(encoding="utf-8"))
        for event, groups in data["hooks"].items():
            for group in groups:
                for hook in group.get("hooks", []):
                    out.append((rel, event, hook.get("command", ""), hook.get("timeout")))
    code = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import codex_install as ci\n"
        "print(json.dumps(ci._managed_hooks(enable_bash_compression=True,"
        " enable_hot_path_hooks=True, enable_prompt_hooks=True, enable_subagent_hooks=True)))\n")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    for event, groups in json.loads(proc.stdout).items():
        for group in groups:
            for hook in group.get("hooks", []):
                out.append(("codex-install", event, hook.get("command", ""), hook.get("timeout")))
    return out


# Codex clamps every SessionEnd hook to 3 s (scripts/sync-codex-marketplace-
# plugin.sh), less than any cold compile of measure.py, so a cold SessionEnd
# was killed there before the gate as well. The gate makes it no worse: the
# kill lands during the wait and the detached compile carries on warming the
# cache. stop_runner cannot tell SessionEnd from Stop before reading stdin.
_HOST_KILL_EXPECTED = {("plugins/token-optimizer/hooks/hooks.json", "SessionEnd")}


def test_every_gated_wait_leaves_room_under_the_host_timeout(monkeypatch):
    monkeypatch.delenv("TOKEN_OPTIMIZER_COLD_COMPILE_WAIT_S", raising=False)
    runner = _module_runner()
    waits = runner._COLD_COMPILE_WAIT_S
    sys.path.insert(0, str(SCRIPTS))
    try:
        import hook_runtime
    finally:
        sys.path.remove(str(SCRIPTS))
    checked, over = 0, []
    for origin, event, command, timeout in _manifest_entries():
        if (origin, event) in _HOST_KILL_EXPECTED:
            continue
        targets = [t for t in re.findall(r"(?:hooks|scripts)/([A-Za-z0-9_]+)\.py", command)
                   if t != "run"]
        if not targets or targets[0] not in waits:
            continue
        checked += 1
        host = float(timeout or 60)
        args = _hook_args(command, targets[0])
        wait = runner._cold_compile_wait(targets[0], args)
        reserve = _RESERVE_OVERRIDES_S.get(targets[0], _RESERVE_S)
        # A hook with its own work budget needs that budget, plus start-up, after the wait.
        budget, _label = hook_runtime.resolve_entry_budget(targets[0], args)
        if budget and budget + 2.0 > reserve:
            reserve = budget + 3.0
        if wait + reserve > host:
            over.append((origin, event, targets[0], args, wait, reserve, host))
    assert checked >= 8, f"found only {checked} gated manifest entries"
    assert not over, f"cold-compile wait + reserve exceeds the host timeout: {over}"


@pytest.mark.skipif(os.name == "nt", reason="the real compile alone takes 10-30 s on Windows runners, "
                    "past any wait the save's 30 s hook timeout leaves room for")
def test_precompact_save_waits_out_a_slow_cold_compile(bench):
    """The PreCompact save is the measure.py hook whose skip loses data: the
    restore after the compaction falls back to an older checkpoint. With the
    shipped waits (no override) a 5 s cold compile outlasts the 4 s other
    measure.py hooks get, but the save waits for it and writes its checkpoint.
    5 s, not SLOW_S: the real compile adds 2-5 s on a loaded runner, and the
    total must stay inside the save's 12 s wait."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _hook_harness import SESSION_ID, transcript
    tpath = transcript(bench.proj, bench.home)
    base = bench.payload

    def payload(event="PreCompact"):
        return {**base(event), "session_id": SESSION_ID, "transcript_path": str(tpath), "trigger": "auto"}

    bench.payload = payload
    rc, _out, err, _elapsed = bench.fire(
        runner="skills/token-optimizer/scripts/measure.py", event="PreCompact",
        args=("compact-capture", "--trigger", "auto", "--quiet"), TO_COLD_SLOW_S="5")
    assert rc == 0, err[-2000:]
    assert len(bench.compiler_boots()) == 1
    [hook] = bench.hook_exits()
    assert not hook["compiled_measure"], "the hook compiled measure.py itself"
    ckpts = bench.home / ".claude" / "token-optimizer" / "checkpoints"
    saved = sorted(ckpts.glob(f"{SESSION_ID}-*.md")) if ckpts.is_dir() else []
    assert saved, "the PreCompact save failed open on a compile it had time to wait for"


def test_only_the_precompact_save_gets_the_longer_wait(monkeypatch):
    monkeypatch.delenv("TOKEN_OPTIMIZER_COLD_COMPILE_WAIT_S", raising=False)
    runner = _module_runner()
    short = runner._COLD_COMPILE_WAIT_S["measure"]
    assert runner._cold_compile_wait("measure", ["compact-capture", "--trigger", "auto", "--quiet"]) > short
    for args in (["compact-capture", "--trigger", "stop-failure", "--quiet"],
                 ["compact-capture", "--trigger", "stop"],
                 ["dynamic-compact-instructions", "--quiet"],
                 ["compact-capture"], []):
        assert runner._cold_compile_wait("measure", args) == short, args
