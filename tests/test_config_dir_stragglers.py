"""CLAUDE_CONFIG_DIR / TOKEN_OPTIMIZER_COPILOT_HOME stragglers (#198, #265).

Every place Token Optimizer reads Claude Code's config folder must honor
CLAUDE_CONFIG_DIR, and the VS Code companion must find Copilot data where the
plugin writes it (TOKEN_OPTIMIZER_COPILOT_HOME). Each test here pins one site
that used to hardcode the default home.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
FLEET_SCRIPTS = REPO / "skills" / "fleet-auditor" / "scripts"
VSCODE = REPO / "vscode-extension"

_STRIP = ("CLAUDE_CONFIG_DIR", "TO_EXTERNAL_MEMORY", "TO_EXTERNAL_MEMORY_MARKER",
          "TOKEN_OPTIMIZER_SNAPSHOT_DIR", "CLAUDE_PLUGIN_DATA",
          "TOKEN_OPTIMIZER_PLUGIN_DATA", "TOKEN_OPTIMIZER_RUNTIME",
          "TOKEN_OPTIMIZER_COPILOT_HOME", "COPILOT_HOME", "PYTHONPATH")


@pytest.fixture
def homes(tmp_path):
    """A fake HOME plus a Claude config dir OUTSIDE it."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    config = tmp_path / "relocated-claude"
    config.mkdir()
    return home, config


def _env(home: Path, config: Path | None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP}
    env.update(HOME=str(home), USERPROFILE=str(home), TOKEN_OPTIMIZER_RUNTIME="claude")
    if config is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config)
    return env


def _run_py(code: str, env: dict, path: Path = SCRIPTS) -> str:
    out = subprocess.run([sys.executable, "-c", code], cwd=str(path), env=env,
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()[-1]


# --------------------------------------------------------- cache_instability

def test_cache_instability_state_dir_honors_claude_config_dir(homes):
    home, config = homes
    got = _run_py(
        "import sys; sys.path.insert(0, '.');"
        "from detectors import cache_instability as ci; print(ci._state_dir())",
        _env(home, config))
    assert Path(got) == config.resolve() / "token-optimizer" / "data"


def test_cache_instability_state_dir_default_unchanged(homes):
    home, _ = homes
    got = _run_py(
        "import sys; sys.path.insert(0, '.');"
        "from detectors import cache_instability as ci; print(ci._state_dir())",
        _env(home, None))
    assert Path(got).resolve() == (home / ".claude" / "token-optimizer" / "data").resolve()


def test_cache_instability_scans_relocated_processes_dir(homes, tmp_path):
    home, config = homes
    got = _run_py(
        "import sys, json; sys.path.insert(0, '.');"
        "from detectors import cache_instability as ci;"
        f"print(json.dumps([str(p) for p in ci._process_prefix_dirs({str(tmp_path)!r})]))",
        _env(home, config))
    dirs = [Path(p) for p in json.loads(got)]
    assert config.resolve() / "processes" in dirs
    assert home / ".claude" / "processes" in dirs  # default still scanned


# ----------------------------------------------------------------- measure.py

def test_durable_memory_marker_read_from_claude_config_dir(homes):
    home, config = homes
    (config / "durable-memory.json").write_text("{}", encoding="utf-8")
    got = _run_py("import measure; print(measure._external_memory_present())",
                  _env(home, config))
    assert got == "True"


def test_durable_memory_marker_in_stale_default_is_ignored(homes):
    home, config = homes
    (home / ".claude" / "durable-memory.json").write_text("{}", encoding="utf-8")
    got = _run_py("import measure; print(measure._external_memory_present())",
                  _env(home, config))
    assert got == "False"


def test_claude_bin_resolvers_check_relocated_local_install(homes):
    home, config = homes
    binary = config / "local" / "claude"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    got = _run_py(
        "import measure, shutil, json; shutil.which = lambda *a, **k: None;"
        "print(json.dumps([measure._keepwarm_resolve_claude_bin(),"
        " measure._resolve_runtime_bin('claude')]))",
        _env(home, config))
    expected = str(config.resolve() / "local" / "claude")
    assert json.loads(got) == [expected, expected]


def test_statusline_snippet_points_at_relocated_runtime_dir(homes):
    home, config = homes
    got = _run_py(
        "import measure, json;"
        "measure._read_settings_for_write = lambda **k: ({'statusLine': {'command': 'mine'}}, True);"
        "measure._is_quality_bar_installed = lambda s: {'hook': True, 'statusline': False};"
        "measure._is_plugin_installed = lambda: False;"
        "measure._is_running_from_plugin_cache = lambda: False;"
        "import io, contextlib; buf = io.StringIO();"
        "ctx = contextlib.redirect_stdout(buf); ctx.__enter__();"
        "measure.setup_quality_bar(dry_run=True); ctx.__exit__(None, None, None);"
        "print(json.dumps(buf.getvalue()))",
        _env(home, config))
    text = json.loads(got)
    qline = next(line for line in text.splitlines() if "const qFile" in line)
    assert json.dumps(config.resolve().as_posix()) in qline, qline
    assert "'.claude'" not in qline, qline


# ---------------------------------------------------------------- fleet-auditor

def test_fleet_shared_claude_dir_honors_claude_config_dir(homes):
    home, config = homes
    # Standalone (no Token Optimizer runtime_env on the path) and plugin layout.
    standalone = _run_py("import sys; sys.path.insert(0, '.'); import shared; print(shared.CLAUDE_DIR)",
                         _env(home, config), path=FLEET_SCRIPTS)
    assert Path(standalone) == config.resolve()
    plugin = _run_py(f"import sys; sys.path[:0] = ['.', {str(SCRIPTS)!r}]; import shared;"
                     " print(shared.CLAUDE_DIR)", _env(home, config), path=FLEET_SCRIPTS)
    assert Path(plugin) == config.resolve()


def test_fleet_shared_claude_dir_default_unchanged(homes):
    home, _ = homes
    got = _run_py("import sys; sys.path.insert(0, '.'); import shared; print(shared.CLAUDE_DIR)",
                  _env(home, None), path=FLEET_SCRIPTS)
    assert Path(got) == home / ".claude"


# ------------------------------------------------- vscode-extension/src/paths.ts

_TS_RUNNER = r"""
const fs = require('fs'), path = require('path'), Module = require('module');
const ts = require(path.join(process.argv[1], 'node_modules', 'typescript'));
const src = fs.readFileSync(path.join(process.argv[1], 'src', 'paths.ts'), 'utf8');
const js = ts.transpileModule(src, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020}}).outputText;
const mod = new Module('paths'); mod.paths = module.paths; mod._compile(js, 'paths.js');
const home = process.argv[2];
const p = mod.exports.resolvePathsForRuntime('copilot', home);
const auto = mod.exports.resolvePathsForRuntime('auto', home);
process.stdout.write(JSON.stringify({cacheDir: p.cacheDir, sessionStateDir: p.sessionStateDir,
  autoCacheDir: auto.cacheDir, installed: mod.exports.detectPluginInstalled(home)}));
"""

_needs_ts = pytest.mark.skipif(
    shutil.which("node") is None or not (VSCODE / "node_modules" / "typescript").is_dir(),
    reason="needs node and vscode-extension dev dependencies")


def _run_ts(home: Path, extra_env: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP}
    env.update(HOME=str(home), USERPROFILE=str(home), **extra_env)
    out = subprocess.run(["node", "-e", _TS_RUNNER, str(VSCODE), str(home)], env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@_needs_ts
def test_vscode_copilot_paths_honor_token_optimizer_copilot_home(tmp_path):
    home = tmp_path / "home"
    relocated = home / "profiles" / "work-copilot"
    (relocated / "token-optimizer").mkdir(parents=True)
    (relocated / "token-optimizer" / "live-fill.json").write_text("{}", encoding="utf-8")
    got = _run_ts(home, {"TOKEN_OPTIMIZER_COPILOT_HOME": str(relocated)})
    real = Path(os.path.realpath(relocated))
    assert Path(got["cacheDir"]) == real / "token-optimizer"
    assert Path(got["sessionStateDir"]) == real / "session-state"
    # Auto mode finds the fresh Copilot cache (there is no Claude cache) and the
    # install probe sees the plugin, both only through the override.
    assert Path(got["autoCacheDir"]) == real / "token-optimizer"
    assert got["installed"] is True


@_needs_ts
def test_vscode_copilot_home_rejects_unsafe_override(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = home / "link-copilot"
    link.symlink_to(outside)
    for bad in (str(outside), str(link), "relative/dir", str(home / "missing")):
        got = _run_ts(home, {"TOKEN_OPTIMIZER_COPILOT_HOME": bad})
        assert Path(got["cacheDir"]) == home / ".copilot" / "token-optimizer", bad


@_needs_ts
def test_vscode_copilot_home_matches_python_resolver(tmp_path):
    """Same answer as runtime_env.copilot_home() for an accepted override."""
    home = tmp_path / "home"
    relocated = home / "alt-copilot"
    relocated.mkdir(parents=True)
    env = _env(home, None)
    env["TOKEN_OPTIMIZER_COPILOT_HOME"] = str(relocated)
    py = _run_py("import runtime_env; print(runtime_env.copilot_home())", env)
    ts = _run_ts(home, {"TOKEN_OPTIMIZER_COPILOT_HOME": str(relocated)})
    assert Path(ts["cacheDir"]).parent == Path(py)
