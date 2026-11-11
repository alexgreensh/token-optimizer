"""Self-tests for tests/_home_guard.py: the sandbox is active and the audit
guard flags real-home access. Uses nonexistent paths under the real home, so no
real data is read or written."""
from helpers.measure_split import exec_source_module as _split_exec_module

import os
import sys
from pathlib import Path

import pytest

import _home_guard

pytestmark = pytest.mark.skipif(
    not (_home_guard.sandbox_enabled() and _home_guard.guard_enabled()),
    reason="real-home sandbox/guard disabled by env",
)

SYNTH = "synthetic::home-guard-selftest"


def _hits_for_synth():
    return _home_guard.take_hits(SYNTH)


def test_home_is_sandboxed():
    real = _home_guard.REAL_HOME
    sandbox = str(_home_guard.SANDBOX) + os.sep
    # On Windows the sandbox sits in AppData/Local/Temp, under the real home,
    # so assert "inside the sandbox" rather than "outside the real home".
    assert os.path.realpath(Path.home()) != real
    assert os.path.realpath(Path.home()).startswith(sandbox)
    assert os.path.realpath(os.path.expanduser("~")).startswith(sandbox)
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "HERMES_HOME",
                "TOKEN_OPTIMIZER_SNAPSHOT_DIR", "CLAUDE_PLUGIN_DATA"):
        assert var not in os.environ or not os.environ[var].startswith(real + os.sep)


def test_import_time_paths_in_measure_are_not_under_real_home():
    scripts = str(Path(__file__).resolve().parent.parent / "skills/token-optimizer/scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    # Import a fresh copy: an earlier test may have left a measure module in
    # sys.modules that it loaded against its own temp folders.
    original = sys.modules.pop("measure", None)
    try:
        import measure

        sandbox = str(_home_guard.SANDBOX) + os.sep
        for name in ("SNAPSHOT_DIR", "TRENDS_DB", "CLAUDE_DIR", "RUNTIME_DIR", "SETTINGS_PATH"):
            value = os.path.realpath(str(getattr(measure, name)))
            assert value.startswith(sandbox), f"measure.{name} cached outside the sandbox: {value}"
    finally:
        sys.modules.pop("measure", None)
        if original is not None:
            sys.modules["measure"] = original


def test_guard_flags_read_write_and_sqlite_under_real_home():
    real = Path(_home_guard.REAL_HOME)
    ghost = real / ".claude" / "__home_guard_selftest_nonexistent__"
    _home_guard.set_current(SYNTH)
    try:
        with pytest.raises(OSError):
            open(ghost / "trends.db", "rb")
        with pytest.raises(OSError):
            open(ghost / "out.json", "w")
        with pytest.raises(OSError):
            os.listdir(ghost)
        with pytest.raises(OSError):
            os.mkdir(ghost / "a" / "b")
    finally:
        _home_guard.set_current("")
    kinds = {(h[1], h[2]) for h in _hits_for_synth()}
    assert ("open", "R") in kinds
    assert ("open", "W") in kinds
    assert ("os.listdir", "LIST") in kinds
    assert ("os.mkdir", "W") in kinds


def test_guard_ignores_sandbox_repo_and_tmp(tmp_path):
    _home_guard.set_current(SYNTH)
    try:
        (tmp_path / "x.txt").write_text("ok")
        (_home_guard.SANDBOX / "home" / "y.txt").write_text("ok")
        open(Path(__file__), "rb").close()
    finally:
        _home_guard.set_current("")
    assert _hits_for_synth() == []


def test_allowlist_only_exempts_matching_kind_and_path(monkeypatch):
    real = Path(_home_guard.REAL_HOME)
    # Parent dir does not exist, so even an un-blocked "w" open fails and
    # nothing can ever be created under the real home.
    probe = real / ".claude" / "__home_guard_absent_dir__" / "probe"
    monkeypatch.setattr(_home_guard, "ALLOWLIST",
                        [("R", r"^\.claude/__home_guard_absent_dir__/probe$", "selftest")])
    _home_guard.set_current(SYNTH)
    try:
        with pytest.raises(OSError):
            open(probe, "rb")
        with pytest.raises(OSError):
            open(probe, "w")
    finally:
        _home_guard.set_current("")
    kinds = {(h[1], h[2]) for h in _hits_for_synth()}
    assert kinds == {("open", "W")}


def test_guard_flags_exists_and_stat_probes_under_real_home():
    # Path.exists() on a cached path steered the old trends.db leak and raises
    # no audit event, so the guard wraps os.stat/lstat/access.
    ghost = Path(_home_guard.REAL_HOME) / ".claude" / "__home_guard_absent_dir__" / "trends.db"
    _home_guard.set_current(SYNTH)
    try:
        assert ghost.exists() is False
        assert os.path.lexists(ghost) is False
        assert os.access(ghost, os.R_OK) is False
    finally:
        _home_guard.set_current("")
    events = {h[1] for h in _hits_for_synth()}
    # Windows (and Path.exists() from 3.14) take os.path's C fast paths, which
    # never call os.stat/os.lstat, so either the os or os.path wrapper must fire.
    assert "os.access" in events
    assert events & {"os.stat", "os.path.exists"}
    assert events & {"os.lstat", "os.path.lexists"}


def test_desktop_bundled_claude_probe_is_stat_only():
    """The dashboard's live process scan stats the .app bundle of a running,
    desktop-bundled Claude CLI. That read-only probe is allowed; any read,
    listing or write there is still flagged."""
    rel = ("Library/Application Support/Claude/claude-code/2.1.299/4fa9694b7975/"
           "claude.app/Contents/Resources/app.asar")
    assert _home_guard._allowed("STAT", rel)
    for kind in ("R", "W", "LIST", "SQLITE"):
        assert not _home_guard._allowed(kind, rel), kind
    assert not _home_guard._allowed("STAT", "Library/Application Support/Claude/claude_desktop_config.json")


def _load_run_py():
    import importlib.util

    run_py = Path(__file__).resolve().parent.parent / "hooks" / "run.py"
    spec = importlib.util.spec_from_file_location("to_run_py_home_guard", run_py)
    mod = importlib.util.module_from_spec(spec)
    _split_exec_module(mod)
    return mod


def test_bytecode_cache_dir_is_sandboxed():
    """run.py's bytecode cache comes from %LOCALAPPDATA% on Windows and
    XDG_CACHE_HOME elsewhere. Both must point into the sandbox: an in-process
    run.main() sets sys.pycache_prefix there, and a real one sent every later
    import's .pyc to the runner's real profile (Windows CI, 5.13.50)."""
    sandbox = str(_home_guard.SANDBOX) + os.sep
    vars_ = ["XDG_CACHE_HOME"] + (["LOCALAPPDATA", "APPDATA"] if os.name == "nt" else [])
    for var in vars_:
        assert os.path.realpath(os.environ[var]).startswith(sandbox), var
    assert os.path.realpath(str(_load_run_py()._bytecode_cache_dir())).startswith(sandbox)


def test_temp_ancestor_git_probe_is_stat_exempt_only_for_dot_git(monkeypatch):
    """On Windows tmp_path sits under ~/AppData/Local/Temp, so run.py's upward
    ``.git`` walk from a tmp plugin root stats ``~/.git``. That exact probe is
    exempt; any other child of a temp ancestor (``~/.claude``) still counts."""
    real = _home_guard.REAL_HOME
    # Isolate the rule: a dev checkout under ~ makes ~ a repo ancestor too.
    monkeypatch.setattr(_home_guard, "_REPO_ANCESTORS", ())
    monkeypatch.setattr(_home_guard, "_PATH_DIRS", ())
    monkeypatch.setattr(_home_guard, "_TEMP_ANCESTORS", (real,))
    assert _home_guard._stat_exempt(os.path.join(real, ".git"))
    assert not _home_guard._stat_exempt(os.path.join(real, ".claude"))
    assert not _home_guard._stat_exempt(os.path.join(real, ".claude", ".git"))
    # The code map's project-root walk probes the same ancestors for these names.
    for marker in ("pyproject.toml", "package.json", "setup.py", "setup.cfg", "go.mod", "Cargo.toml"):
        assert _home_guard._stat_exempt(os.path.join(real, marker)), marker
    assert not _home_guard._stat_exempt(os.path.join(real, "notes.txt"))



def test_account_home_identity_exception_is_metadata_only():
    assert _home_guard._allowed("STAT", ".")
    for kind in ("R", "W", "LIST", "SQLITE"):
        assert not _home_guard._allowed(kind, "."), kind
    for child in (".claude", ".claude/settings.json", "config.json", "private.txt"):
        assert not _home_guard._allowed("STAT", child), child


def test_exact_root_stat_rule_still_detects_decoy_content_access(tmp_path):
    """Use a disposable account home, never actual user files, for negatives."""
    import json
    import subprocess
    decoy = tmp_path / "decoy account"
    decoy.mkdir()
    (decoy / "private.json").write_text("fixture", encoding="utf-8")
    code = '''import json, os, sqlite3, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import _home_guard as guard
guard.install_sandbox(); guard.install_guard()
root = Path(guard.REAL_HOME)
root.stat(); root.lstat()
assert guard.HITS == [], guard.HITS
ghost = root / "missing-dir" / "config.json"
for operation in (ghost.stat, ghost.lstat):
    try:
        operation()
    except OSError:
        pass
    else:
        raise AssertionError("missing decoy unexpectedly exists")
assert any(hit[2] == "STAT" for hit in guard.HITS), guard.HITS
guard.HITS.clear()
ghost.exists()
assert any(hit[2] == "STAT" for hit in guard.HITS), guard.HITS
open(root / "private.json").close()
open(root / "written.json", "w").close()
os.listdir(root)
connection = sqlite3.connect(root / "test.db"); connection.close()
print(json.dumps(sorted({hit[2] for hit in guard.HITS})))
'''
    env = dict(os.environ, TOKEN_OPTIMIZER_TEST_REAL_HOME_OVERRIDE=str(decoy))
    proc = subprocess.run([sys.executable, "-c", code, str(Path(__file__).parent)],
                          env=env, capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, proc.stderr
    assert {"STAT", "R", "W", "LIST", "SQLITE"} <= set(json.loads(proc.stdout))
