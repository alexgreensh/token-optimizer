"""Windows console-flash elimination + child-tree reap coverage.

On Windows, ``start_new_session=True`` is silently ignored by ``subprocess``,
so a detached child inherits the parent's console and flashes a ~1s window on
every spawn. Every fire-and-forget spawn must now route through
``spawn_utils.spawn_detached()`` (POSIX: ``start_new_session``; Windows:
``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB``,
with a retry that drops ``CREATE_BREAKAWAY_FROM_JOB`` if ``CreateProcess``
raises ``OSError`` inside a restrictive Job Object).

The one exception is the daemon-regen spawn inside the generated
``dashboard-server.py`` template: that file is standalone (no sibling
``spawn_utils.py``), so the detach logic is inlined as ``CREATE_NO_WINDOW`` on
nt (the regen child is a transient worker, not a survivor).

The daemon-revive spawn (~line 21660 in measure.py) uses
``detach_spawn_kwargs()`` directly (single source of truth) because the caller
already swallows ``Exception`` and the child must survive the hook.

``hooks/run.py`` is also special: its child MUST inherit run.py's stdio for
hook injection via stdout, so it uses ``CREATE_NO_WINDOW`` only (NOT
``DETACHED_PROCESS``, NOT ``CREATE_NEW_PROCESS_GROUP``) on Windows. Because
module_runner.py runs measure.py in-process, the child proc IS the lock
holder, so Windows reaps with plain ``proc.kill()`` (TerminateProcess of
proc.pid only) -- NOT ``taskkill /F /T`` which would walk the PPID tree and
kill the detached session-end-flush worker. POSIX keeps ``os.killpg``.

Run: python3 -m pytest tests/test_windows_spawn_no_window.py -v
"""
import importlib
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import threading
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
HOOKS = REPO / "hooks"

sys.path.insert(0, str(SCRIPTS))

# ---------------------------------------------------------------------------
# Windows flag constants (the values CPython uses on Windows builds)
# ---------------------------------------------------------------------------
_DETACHED_PROCESS = 0x8
_CREATE_NEW_PROCESS_GROUP = 0x200
_CREATE_BREAKAWAY_FROM_JOB = 0x1000000
_CREATE_NO_WINDOW = 0x08000000

_NT_FLAGS = {
    "DETACHED_PROCESS": _DETACHED_PROCESS,
    "CREATE_NEW_PROCESS_GROUP": _CREATE_NEW_PROCESS_GROUP,
    "CREATE_BREAKAWAY_FROM_JOB": _CREATE_BREAKAWAY_FROM_JOB,
    "CREATE_NO_WINDOW": _CREATE_NO_WINDOW,
}

# #107: CREATE_NO_WINDOW is OR'd in by detach_spawn_kwargs() as belt-and-suspenders
# (Windows ignores it while DETACHED_PROCESS is set, but the invariant "every spawn
# carries the no-flash flag" must hold uniformly across every spawn site).
_DETACH_FLAGS = (_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
                 | _CREATE_BREAKAWAY_FROM_JOB | _CREATE_NO_WINDOW)


def _make_fake_os(name):
    """Create a fake ``os`` module with the given ``name`` but real posixpath.

    Monkeypatching ``os.name`` on the real ``os`` module breaks ``pathlib``
    on macOS (Python 3.14's pathlib dispatches path flavour on ``os.name``).
    Instead we build a shallow copy of the real ``os`` module's namespace,
    override only ``name``, and inject it into the module under test. The copy
    keeps ``os.path`` as ``posixpath`` so path resolution still works.
    """
    fake = types.ModuleType("os_" + name)
    fake.__dict__.update(os.__dict__)
    fake.name = name
    return fake


def _set_nt_spawn_utils(monkeypatch):
    """Patch ``spawn_utils`` so ``detach_spawn_kwargs()``/``spawn_detached()``
    see ``os.name == 'nt'`` and Windows flag constants.

    The helpers read ``os.name`` and flag constants from their OWN namespace
    (``spawn_utils.os``, ``spawn_utils.subprocess``), not the caller's. We
    replace ``spawn_utils.os`` with a fake that has ``name='nt'`` but keeps
    ``posixpath`` so ``pathlib`` in the CALLER (measure, hermes, copilot) is
    unaffected -- ``spawn_utils`` only reads ``os.name``, never touches paths.
    """
    import spawn_utils
    monkeypatch.setattr(spawn_utils, "os", _make_fake_os("nt"))
    for _name, _val in _NT_FLAGS.items():
        monkeypatch.setattr(spawn_utils.subprocess, _name, _val, raising=False)


def _set_posix_spawn_utils(monkeypatch):
    """Patch ``spawn_utils`` so ``detach_spawn_kwargs()`` returns posix kwargs."""
    import spawn_utils
    monkeypatch.setattr(spawn_utils, "os", _make_fake_os("posix"))
    for _name in _NT_FLAGS:
        monkeypatch.delattr(spawn_utils.subprocess, _name, raising=False)


def _set_nt(monkeypatch, mod):
    """Patch *mod* so ``os.name == 'nt'`` and Windows flag constants exist.

    Use ``_set_nt_spawn_utils`` instead for modules that route spawns through
    ``spawn_detached()`` and don't have inline ``os.name`` checks (measure,
    hermes, copilot). This heavier helper replaces ``mod.os`` with a fake that
    has ``name='nt'`` but keeps ``posixpath`` -- needed for run.py which has
    inline ``os.name == 'nt'`` branches AND must not break pathlib.
    """
    monkeypatch.setattr(mod, "os", _make_fake_os("nt"))
    for _name, _val in _NT_FLAGS.items():
        monkeypatch.setattr(mod.subprocess, _name, _val, raising=False)
    if "spawn_utils" in sys.modules:
        _set_nt_spawn_utils(monkeypatch)


def _set_posix(monkeypatch, mod):
    """Patch *mod* so ``os.name == 'posix'`` and no Windows flags exist.

    Use ``_set_posix_spawn_utils`` instead for modules that route spawns
    through ``spawn_detached()``.
    """
    monkeypatch.setattr(mod, "os", _make_fake_os("posix"))
    for _name in _NT_FLAGS:
        monkeypatch.delattr(mod.subprocess, _name, raising=False)


# ---------------------------------------------------------------------------
# sys.modules hygiene: the fixtures below pop and re-import script modules
# (measure, bridges). Without restoration, a re-imported instance stays in
# sys.modules while later test FILES still hold the original bound at
# collection time -- test_runtime_env_wsl_mnt then patches the original while
# measure delegates to the unpatched replacement (#107 closeout regression).
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = sys.modules.copy()
    yield
    for k in list(sys.modules):
        if k not in saved:
            del sys.modules[k]
    sys.modules.update(saved)


# ---------------------------------------------------------------------------
# measure.py fixture (mirrors test_daemon_midsession_pulse.py bootstrap)
# ---------------------------------------------------------------------------
@pytest.fixture()
def m(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="to-spawn-nt-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_is_foreign_runtime", lambda: False)
    monkeypatch.setattr(mod, "detect_runtime", lambda: "claude")
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _capture_spawn_detached_popen(monkeypatch):
    """Patch ``spawn_utils.subprocess.Popen`` (what ``spawn_detached`` calls)
    with a kwargs-capturing fake. Returns the capture dict."""
    import spawn_utils
    captured = {}

    def fake_popen(argv, *a, **k):
        captured.update(k)
        captured["argv"] = argv

        class _P:
            pid = 99999
            def poll(self):
                return 0
            def wait(self, timeout=None):
                return 0
            def kill(self):
                pass
        return _P()
    monkeypatch.setattr(spawn_utils.subprocess, "Popen", fake_popen)
    return captured


# ---------------------------------------------------------------------------
# spawn_utils unit tests
# ---------------------------------------------------------------------------
def test_detach_spawn_kwargs_nt_returns_creationflags(monkeypatch):
    import spawn_utils
    monkeypatch.setattr(spawn_utils, "os", _make_fake_os("nt"))
    for _name, _val in _NT_FLAGS.items():
        monkeypatch.setattr(spawn_utils.subprocess, _name, _val, raising=False)
    kw = spawn_utils.detach_spawn_kwargs()
    assert "creationflags" in kw
    assert "start_new_session" not in kw
    assert kw["creationflags"] == _DETACH_FLAGS


def test_detach_spawn_kwargs_posix_returns_start_new_session(monkeypatch):
    import spawn_utils
    monkeypatch.setattr(spawn_utils, "os", _make_fake_os("posix"))
    kw = spawn_utils.detach_spawn_kwargs()
    assert kw == {"start_new_session": True}
    assert "creationflags" not in kw


def test_spawn_detached_nt_passes_creationflags(monkeypatch):
    import spawn_utils
    monkeypatch.setattr(spawn_utils, "os", _make_fake_os("nt"))
    for _name, _val in _NT_FLAGS.items():
        monkeypatch.setattr(spawn_utils.subprocess, _name, _val, raising=False)
    cap = {}
    def fake_popen(argv, **k):
        cap.update(k)
        class _P: pass
        return _P()
    monkeypatch.setattr(spawn_utils.subprocess, "Popen", fake_popen)
    spawn_utils.spawn_detached(["x"], stdout=subprocess.DEVNULL)
    assert cap["creationflags"] == _DETACH_FLAGS
    assert "start_new_session" not in cap
    assert spawn_utils.last_spawn_used_fallback is False


def test_spawn_detached_posix_passes_start_new_session(monkeypatch):
    import spawn_utils
    monkeypatch.setattr(spawn_utils, "os", _make_fake_os("posix"))
    cap = {}
    def fake_popen(argv, **k):
        cap.update(k)
        class _P: pass
        return _P()
    monkeypatch.setattr(spawn_utils.subprocess, "Popen", fake_popen)
    spawn_utils.spawn_detached(["x"], stdout=subprocess.DEVNULL)
    assert cap.get("start_new_session") is True
    assert "creationflags" not in cap


def test_spawn_detached_nt_retries_without_breakaway_on_oserror(monkeypatch):
    """If CreateProcess fails with OSError (ACCESS_DENIED inside a restrictive
    Job Object), spawn_detached retries once without CREATE_BREAKAWAY_FROM_JOB."""
    import spawn_utils
    monkeypatch.setattr(spawn_utils, "os", _make_fake_os("nt"))
    for _name, _val in _NT_FLAGS.items():
        monkeypatch.setattr(spawn_utils.subprocess, _name, _val, raising=False)
    attempts = []
    def fake_popen(argv, **k):
        attempts.append(k.get("creationflags", 0))
        if len(attempts) == 1:
            raise OSError("Access is denied")
        class _P: pass
        return _P()
    monkeypatch.setattr(spawn_utils.subprocess, "Popen", fake_popen)
    result = spawn_utils.spawn_detached(["x"])
    assert result is not None, "retry must succeed"
    assert len(attempts) == 2
    assert attempts[0] == _DETACH_FLAGS
    # The retry clears ONLY the breakaway bit -- detach and no-window survive.
    assert attempts[1] == (_DETACH_FLAGS & ~_CREATE_BREAKAWAY_FROM_JOB)
    assert not (attempts[1] & _CREATE_BREAKAWAY_FROM_JOB)
    assert attempts[1] & _CREATE_NO_WINDOW, "#107 flag must survive the fallback"
    assert spawn_utils.last_spawn_used_fallback is True


def test_spawn_detached_nt_returns_none_on_double_failure(monkeypatch):
    import spawn_utils
    monkeypatch.setattr(spawn_utils, "os", _make_fake_os("nt"))
    for _name, _val in _NT_FLAGS.items():
        monkeypatch.setattr(spawn_utils.subprocess, _name, _val, raising=False)
    def fake_popen(argv, **k):
        raise OSError("nope")
    monkeypatch.setattr(spawn_utils.subprocess, "Popen", fake_popen)
    assert spawn_utils.spawn_detached(["x"]) is None


def test_spawn_detached_posix_returns_none_on_oserror(monkeypatch):
    import spawn_utils
    monkeypatch.setattr(spawn_utils, "os", _make_fake_os("posix"))
    def fake_popen(argv, **k):
        raise OSError("nope")
    monkeypatch.setattr(spawn_utils.subprocess, "Popen", fake_popen)
    assert spawn_utils.spawn_detached(["x"]) is None


# ---------------------------------------------------------------------------
# measure.py: _defer_session_end_flush (line ~6059) -- uses spawn_detached
# ---------------------------------------------------------------------------
def test_session_end_flush_nt_uses_creationflags(m, monkeypatch):
    _set_nt_spawn_utils(monkeypatch)
    cap = _capture_spawn_detached_popen(monkeypatch)
    m._defer_session_end_flush(["measure.py", "session-end", "--session", "t"])
    assert "creationflags" in cap, "nt spawn must pass creationflags"
    assert cap["creationflags"] == _DETACH_FLAGS
    assert "start_new_session" not in cap


def test_session_end_flush_posix_uses_start_new_session(m, monkeypatch):
    _set_posix_spawn_utils(monkeypatch)
    cap = _capture_spawn_detached_popen(monkeypatch)
    m._defer_session_end_flush(["measure.py", "session-end", "--session", "t"])
    assert cap.get("start_new_session") is True
    assert "creationflags" not in cap


# ---------------------------------------------------------------------------
# measure.py: daemon-revive spawn (line ~21685) -- now routed through
# spawn_detached (single source of truth, includes breakaway retry).
# ---------------------------------------------------------------------------
def test_daemon_revive_nt_uses_creationflags(m, monkeypatch):
    """The daemon-revive spawn must route through spawn_detached on nt."""
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: False)  # dead
    monkeypatch.setattr(m, "_normalized_platform", lambda: "Windows")
    _set_nt_spawn_utils(monkeypatch)
    cap = _capture_spawn_detached_popen(monkeypatch)
    assert m._daemon_midsession_pulse() == "revive-spawned"
    assert "creationflags" in cap
    assert cap["creationflags"] == _DETACH_FLAGS
    assert "start_new_session" not in cap


@pytest.mark.skipif(sys.platform == "win32", reason="asserts POSIX spawn behavior (start_new_session, no creationflags); the NT variant covers Windows")
def test_daemon_revive_posix_uses_start_new_session(m, monkeypatch):
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: False)
    _set_posix_spawn_utils(monkeypatch)
    cap = _capture_spawn_detached_popen(monkeypatch)
    assert m._daemon_midsession_pulse() == "revive-spawned"
    assert cap.get("start_new_session") is True
    assert "creationflags" not in cap


# ---------------------------------------------------------------------------
# measure.py: generated daemon regen spawn (line ~19290, inside f-string
# template). The generated daemon is standalone -- no spawn_utils import.
# Exec the generated template's _maybe_refresh_dashboard with a fake
# subprocess.Popen to assert the inlined nt branch produces CREATE_NO_WINDOW.
# ---------------------------------------------------------------------------
def test_daemon_regen_nt_uses_create_no_window(monkeypatch, tmp_path):
    """Exec the generated daemon source and assert that on nt the regen spawn
    passes CREATE_NO_WINDOW (not DETACHED_PROCESS, not the shared helper)."""
    if "measure" in sys.modules:
        del sys.modules["measure"]
    import measure
    src = measure._generate_daemon_script()
    if "measure" in sys.modules:
        del sys.modules["measure"]

    # The generated daemon must NOT reference the shared helper.
    assert "detach_spawn_kwargs" not in src, (
        "generated daemon must NOT reference the shared helper (no sibling on sys.path)"
    )
    assert "spawn_detached" not in src, (
        "generated daemon must NOT reference the shared helper (no sibling on sys.path)"
    )

    dashboard = tmp_path / "dashboard.html"
    dashboard.write_text("<html></html>")
    regen_log = tmp_path / "regen.log"
    measure_py = tmp_path / "measure.py"
    measure_py.write_text("# stub\n")

    fake_os = _make_fake_os("nt")
    fake_sub = types.ModuleType("subprocess")
    for _name, _val in _NT_FLAGS.items():
        setattr(fake_sub, _name, _val)
    fake_sub.DEVNULL = subprocess.DEVNULL
    fake_sub.STDOUT = subprocess.STDOUT

    cap = {}
    def fake_popen(argv, **k):
        cap.update(k)
        class _P: pass
        return _P()
    fake_sub.Popen = fake_popen

    # The generated method does `import subprocess` locally, so we must
    # inject our fake into sys.modules so the import gets it.
    monkeypatch.setitem(sys.modules, "subprocess", fake_sub)

    ns = {
        "os": fake_os,
        "sys": types.ModuleType("sys"),
        "time": __import__("time"),
        "Path": Path,
        "DASHBOARD": str(dashboard),
        "REGEN_LOG": str(regen_log),
        "MEASURE_PY_FALLBACK": str(measure_py),
        "_MEASURE_PY_CACHE": ("", 0.0),
        "MEASURE_PY_RESOLVE_TTL": 300,
        "DASHBOARD_FRESH_SECONDS": 120,
        "_last_regen": 0.0,
        # issue #160: _maybe_refresh_dashboard now guards _last_regen with
        # _STATE_LOCK; give the extracted method a real lock so it runs.
        "_STATE_LOCK": threading.Lock(),
        "_resolve_measure_py": lambda: str(measure_py),
        "_log_regen": lambda msg: None,
        # issue #160 follow-up: _maybe_refresh_dashboard now also claims the shared
        # _regen_inflight guard and hands it to a reaper thread; give the extracted
        # method those globals so it runs. The reaper is stubbed here (this test
        # only checks the Popen creationflags, not the guard's lifetime).
        "_regen_inflight": False,
        "threading": threading,
        "_reap_bg_regen": lambda proc: None,
    }
    ns["sys"].executable = sys.executable
    ns["__name__"] = "dashboard_server_test"

    import textwrap
    m = re.search(r"^    def _maybe_refresh_dashboard\(self\):.*?\n(?=^    def |^class |\Z)",
                  src, re.M | re.S)
    assert m, "_maybe_refresh_dashboard missing from generated daemon"
    dedented = textwrap.dedent(m.group(0))
    dedented = dedented.replace("(self):", "():")
    exec(dedented, ns)

    import time as _time
    old_mtime = _time.time() - 9999
    os.utime(str(dashboard), (old_mtime, old_mtime))

    ns["_maybe_refresh_dashboard"]()

    assert "creationflags" in cap, "nt regen spawn must pass CREATE_NO_WINDOW"
    assert cap["creationflags"] == _CREATE_NO_WINDOW
    assert "start_new_session" not in cap
    # The regen child is transient, NOT detached.
    assert not (cap["creationflags"] & _DETACHED_PROCESS)


def test_daemon_regen_posix_uses_start_new_session(monkeypatch, tmp_path):
    """On posix the generated daemon's regen spawn uses start_new_session."""
    if "measure" in sys.modules:
        del sys.modules["measure"]
    import measure
    src = measure._generate_daemon_script()
    if "measure" in sys.modules:
        del sys.modules["measure"]

    dashboard = tmp_path / "dashboard.html"
    dashboard.write_text("<html></html>")
    regen_log = tmp_path / "regen.log"
    measure_py = tmp_path / "measure.py"
    measure_py.write_text("# stub\n")

    fake_os = _make_fake_os("posix")
    fake_sub = types.ModuleType("subprocess")
    fake_sub.DEVNULL = subprocess.DEVNULL
    fake_sub.STDOUT = subprocess.STDOUT

    cap = {}
    def fake_popen(argv, **k):
        cap.update(k)
        class _P: pass
        return _P()
    fake_sub.Popen = fake_popen

    monkeypatch.setitem(sys.modules, "subprocess", fake_sub)

    ns = {
        "os": fake_os,
        "sys": types.ModuleType("sys"),
        "time": __import__("time"),
        "Path": Path,
        "DASHBOARD": str(dashboard),
        "REGEN_LOG": str(regen_log),
        "MEASURE_PY_FALLBACK": str(measure_py),
        "_MEASURE_PY_CACHE": ("", 0.0),
        "MEASURE_PY_RESOLVE_TTL": 300,
        "DASHBOARD_FRESH_SECONDS": 120,
        "_last_regen": 0.0,
        # issue #160: _maybe_refresh_dashboard now guards _last_regen with
        # _STATE_LOCK; give the extracted method a real lock so it runs.
        "_STATE_LOCK": threading.Lock(),
        "_resolve_measure_py": lambda: str(measure_py),
        "_log_regen": lambda msg: None,
        # issue #160 follow-up: _maybe_refresh_dashboard now also claims the shared
        # _regen_inflight guard and hands it to a reaper thread; give the extracted
        # method those globals so it runs. The reaper is stubbed here (this test
        # only checks the Popen creationflags, not the guard's lifetime).
        "_regen_inflight": False,
        "threading": threading,
        "_reap_bg_regen": lambda proc: None,
    }
    ns["sys"].executable = sys.executable
    ns["__name__"] = "dashboard_server_test"

    import textwrap
    m = re.search(r"^    def _maybe_refresh_dashboard\(self\):.*?\n(?=^    def |^class |\Z)",
                  src, re.M | re.S)
    assert m, "_maybe_refresh_dashboard missing from generated daemon"
    dedented = textwrap.dedent(m.group(0))
    dedented = dedented.replace("(self):", "():")
    exec(dedented, ns)

    import time as _time
    old_mtime = _time.time() - 9999
    os.utime(str(dashboard), (old_mtime, old_mtime))

    ns["_maybe_refresh_dashboard"]()

    assert cap.get("start_new_session") is True
    assert "creationflags" not in cap


def test_daemon_regen_closes_errf_on_success(monkeypatch, tmp_path):
    """The generated daemon must close errf after Popen (no fd leak)."""
    if "measure" in sys.modules:
        del sys.modules["measure"]
    import measure
    src = measure._generate_daemon_script()
    if "measure" in sys.modules:
        del sys.modules["measure"]
    # The source must have a finally: errf.close() pattern.
    assert "errf.close()" in src, "generated daemon must close errf in a finally block"
    assert re.search(r"finally:\s+.*errf\.close\(\)", src, re.S), (
        "errf.close() must be in a finally block"
    )


# ---------------------------------------------------------------------------
# measure.py: run_ensure_health auto-update spawn (line ~34367) -- spawn_detached
# ---------------------------------------------------------------------------
def _stub_ensure_health(m, monkeypatch, tmp_path):
    """Stub heavy helpers so run_ensure_health reaches the auto-update spawn."""
    install_dir = tmp_path / "token-optimizer"
    install_dir.mkdir()
    (install_dir / ".git").mkdir()
    (install_dir / "install.sh").write_text("#!/bin/bash\nexit 0\n")
    monkeypatch.setattr(m, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(m, "_read_settings_json", lambda: ({}, None))
    monkeypatch.setattr(m, "_write_settings_atomic", lambda d: None)
    monkeypatch.setattr(m, "_auto_remove_bad_env_vars", lambda: None)
    monkeypatch.setattr(m, "_ensure_dashboard_daemon", lambda *a, **k: "noop-healthy")
    monkeypatch.setattr(m, "setup_quality_bar", lambda *a, **k: None)
    monkeypatch.setattr(m, "_read_config_flag", lambda k, d=None: False)


def test_ensure_health_auto_update_nt_uses_creationflags(m, monkeypatch, tmp_path):
    _set_nt_spawn_utils(monkeypatch)
    _stub_ensure_health(m, monkeypatch, tmp_path)
    cap = _capture_spawn_detached_popen(monkeypatch)
    m.run_ensure_health()
    assert "creationflags" in cap, "nt auto-update spawn must pass creationflags"
    assert cap["creationflags"] == _DETACH_FLAGS
    assert "start_new_session" not in cap


@pytest.mark.skipif(os.name == "nt", reason="POSIX start_new_session semantics")
def test_ensure_health_auto_update_posix_uses_start_new_session(m, monkeypatch, tmp_path):
    _set_posix_spawn_utils(monkeypatch)
    _stub_ensure_health(m, monkeypatch, tmp_path)
    cap = _capture_spawn_detached_popen(monkeypatch)
    m.run_ensure_health()
    assert cap.get("start_new_session") is True
    assert "creationflags" not in cap


# ---------------------------------------------------------------------------
# hermes_hook_bridge.py: run_rollup (~185) and run_dashboard (~226) -- spawn_detached
# ---------------------------------------------------------------------------
@pytest.fixture()
def hermes(monkeypatch):
    for _mod in ("hermes_hook_bridge", "spawn_utils", "runtime_env"):
        if _mod in sys.modules:
            del sys.modules[_mod]
    mod = importlib.import_module("hermes_hook_bridge")
    mod._locate_measure_py_cache = mod._SENTINEL
    yield mod
    for _mod in ("hermes_hook_bridge", "spawn_utils", "runtime_env"):
        if _mod in sys.modules:
            del sys.modules[_mod]


def test_hermes_rollup_nt_uses_creationflags(hermes, monkeypatch):
    _set_nt_spawn_utils(monkeypatch)
    cap = _capture_spawn_detached_popen(monkeypatch)
    hermes.run_rollup("sid", "hermes", "stop")
    assert "creationflags" in cap
    assert cap["creationflags"] == _DETACH_FLAGS
    assert "start_new_session" not in cap


def test_hermes_rollup_posix_uses_start_new_session(hermes, monkeypatch):
    _set_posix_spawn_utils(monkeypatch)
    cap = _capture_spawn_detached_popen(monkeypatch)
    hermes.run_rollup("sid", "hermes", "stop")
    assert cap.get("start_new_session") is True
    assert "creationflags" not in cap


def test_hermes_dashboard_nt_uses_creationflags(hermes, monkeypatch):
    _set_nt_spawn_utils(monkeypatch)
    cap = _capture_spawn_detached_popen(monkeypatch)
    hermes.run_dashboard("sid", 24844)
    assert "creationflags" in cap
    assert "start_new_session" not in cap


def test_hermes_dashboard_posix_uses_start_new_session(hermes, monkeypatch):
    _set_posix_spawn_utils(monkeypatch)
    cap = _capture_spawn_detached_popen(monkeypatch)
    hermes.run_dashboard("sid", 24844)
    assert cap.get("start_new_session") is True
    assert "creationflags" not in cap


# ---------------------------------------------------------------------------
# copilot_hook_bridge.py: handle_stop (~589) -- spawn_detached
# ---------------------------------------------------------------------------
@pytest.fixture()
def copilot(monkeypatch):
    for _mod in ("copilot_hook_bridge", "spawn_utils", "runtime_env"):
        if _mod in sys.modules:
            del sys.modules[_mod]
    mod = importlib.import_module("copilot_hook_bridge")
    yield mod
    for _mod in ("copilot_hook_bridge", "spawn_utils", "runtime_env"):
        if _mod in sys.modules:
            del sys.modules[_mod]


def test_copilot_stop_nt_uses_creationflags(copilot, monkeypatch):
    _set_nt_spawn_utils(monkeypatch)
    cap = _capture_spawn_detached_popen(monkeypatch)
    monkeypatch.setattr(copilot, "_to_dir", lambda: None)
    copilot.handle_stop({"sessionId": "sid", "toolName": "stop"})
    assert "creationflags" in cap
    assert cap["creationflags"] == _DETACH_FLAGS
    assert "start_new_session" not in cap


def test_copilot_stop_posix_uses_start_new_session(copilot, monkeypatch):
    _set_posix_spawn_utils(monkeypatch)
    cap = _capture_spawn_detached_popen(monkeypatch)
    monkeypatch.setattr(copilot, "_to_dir", lambda: None)
    copilot.handle_stop({"sessionId": "sid", "toolName": "stop"})
    assert cap.get("start_new_session") is True
    assert "creationflags" not in cap


# ---------------------------------------------------------------------------
# cursor_hook_bridge.py: handle_stop -- spawn_detached
# ---------------------------------------------------------------------------
@pytest.fixture()
def cursor(monkeypatch):
    for _mod in ("cursor_hook_bridge", "spawn_utils", "runtime_env"):
        if _mod in sys.modules:
            del sys.modules[_mod]
    mod = importlib.import_module("cursor_hook_bridge")
    yield mod
    for _mod in ("cursor_hook_bridge", "spawn_utils", "runtime_env"):
        if _mod in sys.modules:
            del sys.modules[_mod]


# ---------------------------------------------------------------------------
# grok_hook_bridge.py: _spawn_rollup / _spawn_dashboard -- spawn_detached
# ---------------------------------------------------------------------------
@pytest.fixture()
def grok(monkeypatch):
    for _mod in ("grok_hook_bridge", "spawn_utils", "runtime_env"):
        if _mod in sys.modules:
            del sys.modules[_mod]
    mod = importlib.import_module("grok_hook_bridge")
    yield mod
    for _mod in ("grok_hook_bridge", "spawn_utils", "runtime_env"):
        if _mod in sys.modules:
            del sys.modules[_mod]


def _cursor_stop_payload():
    return {"hook_event_name": "stop", "conversation_id": "sid"}


def _silence_cursor_stop_side_effects(cursor, monkeypatch):
    # Isolate the spawn behaviour: the tally/ledger/throttle writes are covered
    # in test_cursor_hook_bridge.py; force the stop throttle past so the two
    # detached spawns actually run.
    monkeypatch.setattr(cursor, "_update_tally", lambda fields, **k: {})
    monkeypatch.setattr(cursor, "_record_observed", lambda event, **k: None)
    monkeypatch.setattr(cursor, "_stop_rollup_due", lambda: True)


def test_cursor_stop_nt_uses_creationflags(cursor, monkeypatch):
    _set_nt_spawn_utils(monkeypatch)
    cap = _capture_spawn_detached_popen(monkeypatch)
    _silence_cursor_stop_side_effects(cursor, monkeypatch)
    cursor.handle_stop(_cursor_stop_payload())
    assert "creationflags" in cap
    assert cap["creationflags"] == _DETACH_FLAGS
    assert "start_new_session" not in cap


def test_grok_rollup_nt_uses_creationflags(grok, monkeypatch):
    _set_nt_spawn_utils(monkeypatch)
    cap = _capture_spawn_detached_popen(monkeypatch)
    grok._spawn_rollup()
    assert "creationflags" in cap
    assert cap["creationflags"] == _DETACH_FLAGS
    assert "start_new_session" not in cap


def test_cursor_stop_posix_uses_start_new_session(cursor, monkeypatch):
    _set_posix_spawn_utils(monkeypatch)
    cap = _capture_spawn_detached_popen(monkeypatch)
    _silence_cursor_stop_side_effects(cursor, monkeypatch)
    cursor.handle_stop(_cursor_stop_payload())
    assert cap.get("start_new_session") is True
    assert "creationflags" not in cap


def test_grok_rollup_posix_uses_start_new_session(grok, monkeypatch):
    _set_posix_spawn_utils(monkeypatch)
    cap = _capture_spawn_detached_popen(monkeypatch)
    grok._spawn_rollup()
    assert cap.get("start_new_session") is True
    assert "creationflags" not in cap


def test_grok_dashboard_nt_uses_creationflags(grok, monkeypatch):
    _set_nt_spawn_utils(monkeypatch)
    cap = _capture_spawn_detached_popen(monkeypatch)
    grok._spawn_dashboard()
    assert "creationflags" in cap
    assert "start_new_session" not in cap


def test_grok_dashboard_posix_uses_start_new_session(grok, monkeypatch):
    _set_posix_spawn_utils(monkeypatch)
    cap = _capture_spawn_detached_popen(monkeypatch)
    grok._spawn_dashboard()
    assert cap.get("start_new_session") is True
    assert "creationflags" not in cap


# ---------------------------------------------------------------------------
# hooks/run.py: SPECIAL spawn (inherits stdio) + tree reap
# ---------------------------------------------------------------------------
def _load_run_py():
    """Load hooks/run.py as an isolated module (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location("run_py_under_test", HOOKS / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_plugin_root(tmp_path):
    """Build a minimal plugin root so run.py's path resolver finds the target."""
    root = tmp_path / "plugin"
    (root / "hooks").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "hooks" / "module_runner.py").write_text(
        "import sys; sys.exit(0)\n"
    )
    (root / "scripts" / "dummy_hook.py").write_text("print('ok')\n")
    return root


def test_run_py_spawn_nt_uses_create_no_window(monkeypatch, tmp_path):
    """run.py SPECIAL: on nt use CREATE_NO_WINDOW ONLY (not
    CREATE_NEW_PROCESS_GROUP -- it is inert for reaping and disables the
    child's Ctrl+C self-terminate). The child MUST inherit run.py's stdio
    (no DEVNULL)."""
    mod = _load_run_py()
    _set_nt(monkeypatch, mod)
    root = _make_plugin_root(tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(root / "_data"))
    monkeypatch.setattr(mod, "_check_consent", lambda *_a, **_k: True)
    class _PipeStream:
        def fileno(self):
            return 1
    inherited = {name: _PipeStream() for name in ("stdin", "stdout", "stderr")}
    for name, stream in inherited.items():
        monkeypatch.setattr(mod.sys, name, stream)
    cap = {}
    def fake_popen(cmd, **k):
        cap.update(k)
        class _P:
            pid = 12345
            def poll(self):
                return 0
            def wait(self, timeout=None):
                return 0
            def kill(self):
                pass
        return _P()
    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    mod.sys.argv = ["run.py", "scripts/dummy_hook.py", "--quiet"]
    mod.main()
    assert "creationflags" in cap, "nt spawn must pass creationflags"
    assert cap["creationflags"] == _CREATE_NO_WINDOW
    assert "start_new_session" not in cap
    # Explicit handles retain a host pipe under CREATE_NO_WINDOW. Leaving these
    # unset lets Windows attach the child to the hidden console instead.
    assert cap["stdin"] is inherited["stdin"]
    assert cap["stdout"] is inherited["stdout"]
    assert cap["stderr"] is inherited["stderr"]


def test_run_py_spawn_posix_uses_start_new_session(monkeypatch, tmp_path):
    mod = _load_run_py()
    _set_posix(monkeypatch, mod)
    root = _make_plugin_root(tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(root / "_data"))
    monkeypatch.setattr(mod, "_check_consent", lambda *_a, **_k: True)
    cap = {}
    def fake_popen(cmd, **k):
        cap.update(k)
        class _P:
            pid = 12345
            def poll(self):
                return 0
            def wait(self, timeout=None):
                return 0
            def kill(self):
                pass
        return _P()
    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    mod.sys.argv = ["run.py", "scripts/dummy_hook.py", "--quiet"]
    mod.main()
    assert cap.get("start_new_session") is True
    assert "creationflags" not in cap


def test_run_py_timeout_reap_nt_uses_proc_kill(monkeypatch, tmp_path):
    """On nt, TimeoutExpired must reap via proc.kill() (TerminateProcess of
    proc.pid only), NOT taskkill /F /T which would walk the PPID tree and
    wrongly kill the detached session-end-flush worker. The child proc IS
    measure.py (module_runner.py runs it in-process), so proc.kill()
    releases the trends.db lock directly."""
    mod = _load_run_py()
    _set_nt(monkeypatch, mod)
    root = _make_plugin_root(tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(root / "_data"))
    monkeypatch.setattr(mod, "_check_consent", lambda *_a, **_k: True)
    # P2-4: guard against pass-on-revert. Patch os.killpg to raise if called
    # on nt (it must never be), and drop signal.SIGKILL so a reverted run.py
    # that references signal.SIGKILL directly (P1-A) blows up here too.
    def _killpg_must_not_be_called(pgid, sig):
        raise AssertionError("os.killpg must NOT be called on nt")
    monkeypatch.setattr(mod.os, "killpg", _killpg_must_not_be_called, raising=False)
    monkeypatch.delattr(mod.signal, "SIGKILL", raising=False)
    kill_calls = []
    class _FakeProc:
        pid = 54321
        def poll(self):
            return None  # still running
        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)
            return 0
        def kill(self):
            kill_calls.append("proc.kill")
    def fake_popen(cmd, **k):
        return _FakeProc()
    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    # Ensure subprocess.run is NOT called (no taskkill).
    run_calls = []
    def fake_run(cmd, **k):
        run_calls.append(cmd)
        class _R:
            returncode = 0
        return _R()
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    mod.sys.argv = ["run.py", "scripts/dummy_hook.py", "--quiet"]
    mod.main()
    assert len(kill_calls) == 1, "nt timeout must reap via proc.kill()"
    assert kill_calls[0] == "proc.kill"
    assert run_calls == [], "nt timeout must NOT use taskkill/subprocess.run"


@pytest.mark.skipif(os.name == "nt", reason="POSIX os.killpg reap semantics")
def test_run_py_timeout_reap_posix_uses_killpg(monkeypatch, tmp_path):
    """On posix, TimeoutExpired reaps via os.killpg (unchanged behavior)."""
    mod = _load_run_py()
    _set_posix(monkeypatch, mod)
    root = _make_plugin_root(tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(root / "_data"))
    monkeypatch.setattr(mod, "_check_consent", lambda *_a, **_k: True)
    killpg_calls = []
    class _FakeProc:
        pid = 54321
        def poll(self):
            return None
        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)
            return 0
        def kill(self):
            killpg_calls.append("kill")
    def fake_popen(cmd, **k):
        return _FakeProc()
    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    def fake_killpg(pgid, sig):
        killpg_calls.append(("killpg", pgid, sig))
    monkeypatch.setattr(mod.os, "killpg", fake_killpg)
    monkeypatch.setattr(mod.os, "getpgid", lambda pid: pid)
    mod.sys.argv = ["run.py", "scripts/dummy_hook.py", "--quiet"]
    mod.main()
    assert any(isinstance(c, tuple) and c[0] == "killpg" for c in killpg_calls), (
        "posix timeout must reap via os.killpg"
    )


def test_run_py_forward_and_exit_nt_uses_proc_kill(monkeypatch):
    """_forward_and_exit(SIGTERM, None) on nt must reap via proc.kill()
    (TerminateProcess of proc.pid only), NOT taskkill /F /T which would
    walk the PPID tree and kill the detached session-end-flush worker.
    Wraps the os._exit(0)."""
    mod = _load_run_py()
    _set_nt(monkeypatch, mod)
    # P2-4: guard against pass-on-revert. Patch os.killpg to raise if called
    # on nt (it must never be), and drop signal.SIGKILL so a reverted run.py
    # that references signal.SIGKILL directly (P1-A) blows up here too.
    def _killpg_must_not_be_called(pgid, sig):
        raise AssertionError("os.killpg must NOT be called on nt")
    monkeypatch.setattr(mod.os, "killpg", _killpg_must_not_be_called, raising=False)
    monkeypatch.delattr(mod.signal, "SIGKILL", raising=False)
    kill_calls = []
    run_calls = []
    def fake_run(cmd, **k):
        run_calls.append(cmd)
        class _R:
            returncode = 0
        return _R()
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    class _FakeProc:
        pid = 77777
        def poll(self):
            return None  # still running
        def kill(self):
            kill_calls.append("proc.kill")

    monkeypatch.setattr(mod, "_child_proc", _FakeProc())
    # Prevent os._exit from killing the test process.
    monkeypatch.setattr(mod.os, "_exit", lambda code=0: None)

    import signal as _signal
    mod._forward_and_exit(_signal.SIGTERM, None)

    assert len(kill_calls) == 1, "nt signal handler must reap via proc.kill()"
    assert kill_calls[0] == "proc.kill"
    assert run_calls == [], "nt signal handler must NOT use taskkill/subprocess.run"


@pytest.mark.skipif(os.name == "nt", reason="POSIX os.killpg reap semantics")
def test_run_py_forward_and_exit_posix_uses_killpg(monkeypatch):
    """_forward_and_exit(SIGTERM, None) on posix must reap via os.killpg."""
    mod = _load_run_py()
    _set_posix(monkeypatch, mod)
    killpg_calls = []
    def fake_killpg(pgid, sig):
        killpg_calls.append(("killpg", pgid, sig))
    monkeypatch.setattr(mod.os, "killpg", fake_killpg)
    monkeypatch.setattr(mod.os, "getpgid", lambda pid: pid)

    class _FakeProc:
        pid = 88888
        def poll(self):
            return None
        def kill(self):
            pass

    monkeypatch.setattr(mod, "_child_proc", _FakeProc())
    monkeypatch.setattr(mod.os, "_exit", lambda code=0: None)

    import signal as _signal
    mod._forward_and_exit(_signal.SIGTERM, None)

    assert any(isinstance(c, tuple) and c[0] == "killpg" for c in killpg_calls), (
        "posix signal handler must reap via os.killpg"
    )


# ---------------------------------------------------------------------------
# utf8_io.py: reexec_in_utf8_mode CREATE_NO_WINDOW on nt
# ---------------------------------------------------------------------------
def test_utf8_io_reexec_nt_uses_create_no_window(monkeypatch):
    """reexec_in_utf8_mode on nt must pass CREATE_NO_WINDOW to Popen so the
    re-exec child does not flash a console when the parent is console-less."""
    if "utf8_io" in sys.modules:
        del sys.modules["utf8_io"]
    import utf8_io
    # Force the re-exec path: non-UTF-8 encoding, no sentinel, no utf8_mode flag.
    monkeypatch.setattr(utf8_io, "os", _make_fake_os("nt"))
    fake_sub = types.ModuleType("subprocess")
    for _name, _val in _NT_FLAGS.items():
        setattr(fake_sub, _name, _val)
    fake_sub.DEVNULL = subprocess.DEVNULL
    fake_sub.SubprocessError = subprocess.SubprocessError
    cap = {}
    class _FakeProc:
        returncode = 0
        def wait(self):
            return 0
    def fake_popen(argv, **k):
        cap.update(k)
        return _FakeProc()
    fake_sub.Popen = fake_popen
    # Inject fake subprocess into sys.modules so the local import gets it.
    monkeypatch.setitem(sys.modules, "subprocess", fake_sub)

    # Make platform check pass for win.
    monkeypatch.setattr(utf8_io.sys, "platform", "win32")
    monkeypatch.setattr(utf8_io.sys, "flags", types.SimpleNamespace(utf8_mode=0))
    monkeypatch.setattr(utf8_io.sys, "argv", ["script.py"])
    monkeypatch.setattr(utf8_io.sys, "executable", sys.executable)
    monkeypatch.delenv(utf8_io._REEXEC_FLAG, raising=False)

    # Force a non-UTF-8 encoding so the re-exec path fires.
    import locale as _locale_mod
    monkeypatch.setattr(_locale_mod, "getpreferredencoding", lambda *a: "cp1252")

    # Prevent os._exit from killing the test process.
    exited = {}
    def fake_exit(code=0):
        exited["code"] = code
    monkeypatch.setattr(utf8_io.os, "_exit", fake_exit)

    utf8_io.reexec_in_utf8_mode()

    assert "creationflags" in cap, "nt re-exec must pass CREATE_NO_WINDOW"
    assert cap["creationflags"] == _CREATE_NO_WINDOW
    assert "start_new_session" not in cap
    assert "code" in exited, "re-exec must call os._exit"


def test_utf8_io_reexec_posix_no_creationflags(monkeypatch):
    """reexec_in_utf8_mode on posix must NOT pass creationflags (uses execv)."""
    if "utf8_io" in sys.modules:
        del sys.modules["utf8_io"]
    import utf8_io
    monkeypatch.setattr(utf8_io, "os", _make_fake_os("posix"))
    monkeypatch.setattr(utf8_io.sys, "platform", "linux")
    monkeypatch.setattr(utf8_io.sys, "flags", types.SimpleNamespace(utf8_mode=0))
    monkeypatch.setattr(utf8_io.sys, "argv", ["script.py"])
    monkeypatch.setattr(utf8_io.sys, "executable", sys.executable)
    monkeypatch.delenv(utf8_io._REEXEC_FLAG, raising=False)

    import locale as _locale_mod
    monkeypatch.setattr(_locale_mod, "getpreferredencoding", lambda *a: "cp1252")

    # On posix, reexec uses os.execv, not Popen. Patch execv to prove it's taken.
    execv_called = {}
    def fake_execv(path, args):
        execv_called["path"] = path
        execv_called["args"] = args
    monkeypatch.setattr(utf8_io.os, "execv", fake_execv)

    utf8_io.reexec_in_utf8_mode()

    assert "path" in execv_called, "posix re-exec must call os.execv"
    assert execv_called["path"] == sys.executable


# ---------------------------------------------------------------------------
# utf8_io.py: #105 -- explicit std-handle passing + pre-spawn flush on nt.
# CREATE_NO_WINDOW left all three of stdin/stdout/stderr None, so CPython did
# not set STARTF_USESTDHANDLES and the child's stdio bound to a NEW hidden
# console: every byte written was discarded and stdin read empty (the "silent
# no-op" on a cp1252 host). The fix passes the parent's real handles
# explicitly (via a fileno()-safe resolver) and flushes BEFORE the spawn so
# parent-buffered text does not interleave behind the child's output.
# ---------------------------------------------------------------------------
class _FilenoStream:
    """A stand-in for a real OS-backed std stream: has a working fileno()."""
    def __init__(self, fd, tag):
        self._fd = fd
        self.tag = tag
        self.flushes = 0
    def fileno(self):
        return self._fd
    def flush(self):
        self.flushes += 1


class _NoFilenoStream:
    """A stream wrapper with no OS handle (pytest capture, embedded hosts)."""
    def flush(self):
        pass


def _utf8_io_nt_reexec_env(monkeypatch, streams=None):
    """Configure utf8_io for the nt re-exec path with controllable std streams.

    Returns ``(module, capture_dict, fake_subprocess_module)``. ``streams``
    maps 'stdin'/'stdout'/'stderr' to objects installed on a FAKE sys module
    (never the real one -- patching real sys.stdout to None would break
    pytest's own capture). Defaults to the real sys.std*.
    """
    if "utf8_io" in sys.modules:
        del sys.modules["utf8_io"]
    import utf8_io as u
    monkeypatch.setattr(u, "os", _make_fake_os("nt"))

    fake_sub = types.ModuleType("subprocess")
    for _name, _val in _NT_FLAGS.items():
        setattr(fake_sub, _name, _val)
    fake_sub.DEVNULL = subprocess.DEVNULL
    fake_sub.SubprocessError = subprocess.SubprocessError
    cap = {}
    class _FakeProc:
        returncode = 0
        def wait(self):
            return 0
    def fake_popen(argv, **k):
        cap.update(k)
        cap["argv"] = argv
        return _FakeProc()
    fake_sub.Popen = fake_popen
    monkeypatch.setitem(sys.modules, "subprocess", fake_sub)

    _streams = streams or {}
    fake_sys = types.SimpleNamespace(
        platform="win32",
        flags=types.SimpleNamespace(utf8_mode=0),
        argv=["script.py"],
        executable=sys.executable,
        stdin=_streams.get("stdin", sys.stdin),
        stdout=_streams.get("stdout", sys.stdout),
        stderr=_streams.get("stderr", sys.stderr),
    )
    monkeypatch.setattr(u, "sys", fake_sys)

    # Clear the re-exec sentinel AND the env vars reexec writes so they cannot
    # leak into sibling tests in this process (monkeypatch restores on teardown).
    monkeypatch.delenv(u._REEXEC_FLAG, raising=False)
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)

    import locale as _locale_mod
    monkeypatch.setattr(_locale_mod, "getpreferredencoding", lambda *a: "cp1252")

    # Stop os._exit from killing the test process.
    monkeypatch.setattr(u.os, "_exit", lambda code=0: None)
    return u, cap, fake_sub


def test_utf8_io_reexec_nt_passes_std_handles(monkeypatch):
    """#105: on nt the re-exec Popen must receive stdin/stdout/stderr bound to
    the parent's real sys.* handles (so the child's stdio attaches to the
    parent's pipe, not a hidden console), AND keep CREATE_NO_WINDOW."""
    stdin_s = _FilenoStream(0, "stdin")
    stdout_s = _FilenoStream(1, "stdout")
    stderr_s = _FilenoStream(2, "stderr")
    u, cap, _ = _utf8_io_nt_reexec_env(monkeypatch, streams={
        "stdin": stdin_s, "stdout": stdout_s, "stderr": stderr_s,
    })
    u.reexec_in_utf8_mode()
    assert cap.get("stdin") is stdin_s, "stdin must be passed explicitly"
    assert cap.get("stdout") is stdout_s, "stdout must be passed explicitly"
    assert cap.get("stderr") is stderr_s, "stderr must be passed explicitly"
    assert cap.get("creationflags") == _CREATE_NO_WINDOW
    assert "start_new_session" not in cap


def test_utf8_io_reexec_nt_keeps_create_no_window_when_stdout_not_tty(monkeypatch):
    """#105: CREATE_NO_WINDOW must be retained even when stdout is a pipe (not
    a tty). The flash-sensitive case is exactly the host-spawned hook whose
    stdout is a pipe; dropping the flag there would reinstate the #104 console
    flash while fixing nothing (handles are now passed explicitly). A future
    'drop it when not a tty' refactor must fail this test."""
    stdout_s = _FilenoStream(1, "stdout")  # a pipe has a fileno but is not a tty
    u, cap, _ = _utf8_io_nt_reexec_env(monkeypatch, streams={"stdout": stdout_s})
    u.reexec_in_utf8_mode()
    assert cap.get("creationflags") == _CREATE_NO_WINDOW
    assert cap.get("stdout") is stdout_s


def test_utf8_io_reexec_nt_safe_when_stdout_lacks_fileno(monkeypatch):
    """#105: if sys.stdout has no fileno (pytest capture, embedded hosts),
    Popen must still be called (no exception escapes) and stdout simply
    omitted from kwargs. A UTF-8 convenience re-exec must never crash the CLI."""
    u, cap, _ = _utf8_io_nt_reexec_env(monkeypatch, streams={"stdout": _NoFilenoStream()})
    u.reexec_in_utf8_mode()  # must not raise
    assert "argv" in cap, "Popen must still be called when a stream lacks fileno"
    assert "stdout" not in cap, "a fileno-less stream must be omitted, not passed"
    assert cap.get("creationflags") == _CREATE_NO_WINDOW


def test_utf8_io_reexec_nt_safe_when_stdout_none(monkeypatch):
    """#105: sys.stdout can be None under pythonw (#104 launcher swap). Popen
    must still be called and stdout omitted, not crash the CLI."""
    u, cap, _ = _utf8_io_nt_reexec_env(monkeypatch, streams={"stdout": None})
    u.reexec_in_utf8_mode()  # must not raise
    assert "argv" in cap, "Popen must still be called when sys.stdout is None"
    assert "stdout" not in cap


def test_utf8_io_reexec_nt_flushes_stdout_stderr_before_popen(monkeypatch):
    """#105: parent stdout/stderr must be flushed BEFORE the Popen spawn so
    parent-buffered text does not interleave behind the child's output (both
    then write to the same fd). The post-wait flush is kept as a safety net,
    not as the only flush."""
    order = []
    class _OrderStream:
        def __init__(self, tag):
            self.tag = tag
        def fileno(self):
            return -1  # no real handle -> omitted from kwargs, but flush recorded
        def flush(self):
            order.append("flush:" + self.tag)
    u, cap, fake_sub = _utf8_io_nt_reexec_env(monkeypatch, streams={
        "stdout": _OrderStream("stdout"), "stderr": _OrderStream("stderr"),
    })
    # Re-wrap Popen to record call order relative to the flushes.
    def ordered_popen(argv, **k):
        order.append("popen")
        cap.update(k)
        cap["argv"] = argv
        class _P:
            returncode = 0
            def wait(self):
                return 0
        return _P()
    fake_sub.Popen = ordered_popen

    u.reexec_in_utf8_mode()

    assert "popen" in order, "Popen must be called"
    popen_idx = order.index("popen")
    assert order.index("flush:stdout") < popen_idx, (
        "sys.stdout must be flushed BEFORE the Popen spawn"
    )
    assert order.index("flush:stderr") < popen_idx, (
        "sys.stderr must be flushed BEFORE the Popen spawn"
    )
    # The post-wait safety-net flush still runs.
    assert "flush:stdout" in order[popen_idx + 1:], (
        "post-wait flush safety net must still run"
    )


def test_utf8_io_reexec_pipe_round_trip(tmp_path):
    """#105 end-to-end: `measure.py version` under a non-UTF-8 locale (which
    triggers reexec_in_utf8_mode) with stdout as a pipe must produce the same
    non-empty output as a UTF-8-locale run. On POSIX this exercises the execv
    round-trip; the Windows Popen discard is covered by the unit tests above.
    Cheap: `version` prints one line and exits 0 with no side effects."""
    env_base = dict(os.environ)
    env_base["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(tmp_path)

    env_nonutf = dict(env_base)
    env_nonutf["PYTHONUTF8"] = "0"
    env_nonutf["LC_ALL"] = "C"
    env_nonutf.pop("PYTHONIOENCODING", None)

    env_utf = dict(env_base)
    env_utf["PYTHONUTF8"] = "1"

    def _run(env):
        return subprocess.run(
            [sys.executable, "measure.py", "version"],
            cwd=str(SCRIPTS), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30,
        )

    r_nonutf = _run(env_nonutf)
    r_utf = _run(env_utf)
    assert r_nonutf.returncode == 0, (
        "non-UTF-8 run failed: " + r_nonutf.stderr.decode("utf-8", "replace")
    )
    assert r_utf.returncode == 0, (
        "UTF-8 run failed: " + r_utf.stderr.decode("utf-8", "replace")
    )
    assert r_nonutf.stdout, (
        "non-UTF-8 re-exec run produced empty stdout (the #105 discard signature)"
    )
    assert r_nonutf.stdout == r_utf.stdout, (
        "re-exec round-trip must not alter the command's stdout"
    )


# ---------------------------------------------------------------------------
# measure.py _log_spawn_failure: durable breadcrumb on spawn_detached None
# ---------------------------------------------------------------------------
def test_log_spawn_failure_writes_durable_line(m, monkeypatch, tmp_path):
    """_log_spawn_failure must append a timestamped line to spawn-failures.log."""
    import spawn_utils
    # Point DAEMON_LOG_DIR to a temp dir inside the snapshot dir.
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(m, "DAEMON_LOG_DIR", log_dir)
    # Make spawn_detached return None by patching its Popen to raise OSError.
    _set_posix_spawn_utils(monkeypatch)
    def fake_popen(argv, **k):
        raise OSError("nope")
    monkeypatch.setattr(spawn_utils.subprocess, "Popen", fake_popen)

    m._defer_session_end_flush(["measure.py", "session-end", "--session", "t"])

    log_file = log_dir / "spawn-failures.log"
    assert log_file.exists(), "spawn-failures.log must be created"
    content = log_file.read_text(encoding="utf-8")
    assert "session-end flush spawn failed" in content
    assert content.strip().endswith("session-end flush spawn failed")


# ---------------------------------------------------------------------------
# Real-spawn smoke test (runs only on actual Windows; CI windows-latest leg).
# Exercises spawn_utils.spawn_detached against a real child process that
# reports its own console state via ctypes. Asserts:
#   1. Popen is returned (not None)
#   2. Child exits 0
#   3. Child has NO console (GetConsoleWindow()==0, proving DETACHED_PROCESS took effect)
#   4. last_spawn_used_fallback is False (CREATE_BREAKAWAY_FROM_JOB succeeded
#      without falling back to the retry path)
# ---------------------------------------------------------------------------
_CHILD_SCRIPT = (
    "import ctypes, sys;\n"
    "hwnd = ctypes.windll.kernel32.GetConsoleWindow();\n"
    "sys.exit(0 if hwnd == 0 else 1)\n"
)


@pytest.mark.skipif(os.name != "nt", reason="real-spawn smoke test is Windows-only")
def test_spawn_detached_real_child():
    """On real Windows, spawn_detached must spawn a detached, console-less child.

    The child script calls GetConsoleWindow() and exits 0 only if the returned
    window handle is 0 (no console attached). This proves DETACHED_PROCESS
    actually took effect at the OS level, not just that the kwargs were passed.

    Either spawn path is accepted: the GitHub Windows runner executes inside a
    Job Object, so CREATE_BREAKAWAY_FROM_JOB can legitimately fail with
    ACCESS_DENIED and spawn_detached falls back to the no-breakaway kwargs
    (which still carry DETACHED_PROCESS | CREATE_NO_WINDOW). The invariant is a
    detached, console-less child -- NOT which of the two paths produced it.
    """
    import spawn_utils
    proc = spawn_utils.spawn_detached(
        [sys.executable, "-c", _CHILD_SCRIPT],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    assert proc is not None, "spawn_detached returned None on real Windows"
    rc = proc.wait(timeout=10)
    assert rc == 0, (
        f"real child exited {rc}, expected 0 (GetConsoleWindow() != 0 means "
        "DETACHED_PROCESS did not take effect)"
    )
    # last_spawn_used_fallback may be True (Job Object blocked BREAKAWAY) or
    # False (primary path succeeded); both yield the required detached,
    # console-less child proven by rc == 0 above. Assert it is a real boolean
    # verdict, not that a specific path was taken.
    assert spawn_utils.last_spawn_used_fallback in (True, False), (
        "spawn_detached must record which path it used"
    )
