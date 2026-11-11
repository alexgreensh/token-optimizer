"""statusline.js must resolve Claude's home the same way the Python hooks do (#198).

The hooks resolve every path through runtime_env.claude_home(), which honors
CLAUDE_CONFIG_DIR. statusline.js hard-coded ~/.claude, so with a relocated
config the two sides split: live-fill.json and rate-limits.json landed in
~/.claude while the hooks read $CLAUDE_CONFIG_DIR, the quality score was read
from the wrong dir, and the effort fallback read the wrong settings.json.

The parity test is the load-bearing one: for every CLAUDE_CONFIG_DIR shape it
asks Python where Claude's home is and asserts the statusline wrote there.

Run: python3 -m pytest tests/test_statusline_claude_config_dir.py -v
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "token-optimizer" / "scripts"
SL = SCRIPTS / "statusline.js"
SID = "11111111-2222-3333-4444-555555555555"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def _env(home: Path, config_dir):
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    drive, _, tail = str(home).partition(os.sep)
    if os.name == "nt" and ":" in drive:
        env["HOMEDRIVE"] = drive
        env["HOMEPATH"] = os.sep + tail
    env.pop("CLAUDE_PLUGIN_DATA", None)
    env.pop("CLAUDE_CONFIG_DIR", None)
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    return env


def _run(home: Path, config_dir, payload_extra=None):
    payload = {
        "session_id": SID,
        "model": {"display_name": "Opus"},
        "workspace": {"current_dir": str(home)},
        "transcript_path": "",
        "cost": {"total_duration_ms": 1000},
        "context_window": {"used_percentage": 42},
        "rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": 1900000000}},
    }
    payload.update(payload_extra or {})
    p = subprocess.run(["node", str(SL)], input=json.dumps(payload),
                       env=_env(home, config_dir), capture_output=True,
                       text=True, timeout=60)
    return re.sub(r"\x1b\[[0-9;]*m", "", p.stdout)


def _python_claude_home(home: Path, config_dir) -> Path:
    code = (f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); "
            "import runtime_env; print(runtime_env.claude_home())")
    out = subprocess.run([sys.executable, "-c", code], env=_env(home, config_dir),
                         capture_output=True, text=True, timeout=60, check=True)
    return Path(out.stdout.strip())


def _layout(tmp_path):
    """A fake HOME with ~/.claude, plus a relocated config dir outside it."""
    home = tmp_path / "home"
    cfg = tmp_path / "claude-config"
    for d in (home / ".claude" / "token-optimizer", cfg / "token-optimizer"):
        d.mkdir(parents=True)
    return home, cfg


def _write_quality_cache(dir_: Path):
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / f"quality-cache-{SID}.json").write_text(json.dumps({
        "score": 72.0, "grade": "B", "resource_health": 72.0,
        "resource_health_grade": "B", "session_efficiency": 81.0,
        "session_efficiency_grade": "A", "session_file": f"proj-{SID}.jsonl",
    }))


def test_sidecars_written_to_config_dir_not_home(tmp_path):
    home, cfg = _layout(tmp_path)
    _run(home, str(cfg))
    for name in ("live-fill.json", "rate-limits.json"):
        assert (cfg / "token-optimizer" / name).is_file(), f"{name} not in CLAUDE_CONFIG_DIR"
        assert not (home / ".claude" / "token-optimizer" / name).exists(), \
            f"{name} still leaked into ~/.claude"


def test_quality_cache_read_from_config_dir(tmp_path):
    home, cfg = _layout(tmp_path)
    _write_quality_cache(cfg / "token-optimizer")
    out = _run(home, str(cfg))
    assert re.search(r"Quality [~]?[A-F] \d+", out), f"no Quality score:\n{out}"


def test_quality_cache_read_from_config_dir_plugin_data(tmp_path):
    home, cfg = _layout(tmp_path)
    _write_quality_cache(cfg / "plugins" / "data" / "token-optimizer-x" / "token-optimizer")
    out = _run(home, str(cfg))
    assert re.search(r"Quality [~]?[A-F] \d+", out), f"no Quality score:\n{out}"


def test_effort_fallback_reads_config_dir_settings(tmp_path):
    home, cfg = _layout(tmp_path)
    (home / ".claude" / "settings.json").write_text(json.dumps({"effortLevel": "low"}))
    (cfg / "settings.json").write_text(json.dumps({"effortLevel": "high"}))
    out = _run(home, str(cfg))
    assert re.search(r"\bhi\b", out), f"effort not read from CLAUDE_CONFIG_DIR:\n{out}"
    assert not re.search(r"\blo\b", out), f"effort read from ~/.claude:\n{out}"


def _cases(tmp_path):
    home, cfg = _layout(tmp_path)
    cases = {
        "unset": None,
        "empty": "",
        "valid": str(cfg),
        "padded": f"  {cfg}  ",
        "missing": str(tmp_path / "nope"),
        "relative": "claude-config",
        "file": str(tmp_path / "a-file"),
    }
    (tmp_path / "a-file").write_text("x")
    tilde = home / "tilde-cfg"
    (tilde / "token-optimizer").mkdir(parents=True)
    cases["tilde"] = "~/tilde-cfg"
    cases["trailing_sep"] = str(cfg) + os.sep
    cases["dot_segment"] = str(tmp_path / "." / "claude-config")
    if os.name != "nt":
        link = tmp_path / "cfg-link"
        link.symlink_to(cfg, target_is_directory=True)
        cases["symlink"] = str(link)
        # pathlib drops the trailing slash and still sees the symlink; a raw
        # lstat("link/") follows it. Both sides must reject.
        cases["symlink_trailing_sep"] = str(link) + "/"
        cases["symlink_dot"] = str(link) + "/."
        # '..' after a symlinked parent is physical, not lexical: Python
        # resolves to real/cfg2, a lexical join would give tmp/cfg2.
        (tmp_path / "real" / "sub").mkdir(parents=True)
        (tmp_path / "real" / "cfg2" / "token-optimizer").mkdir(parents=True)
        (tmp_path / "sublink").symlink_to(tmp_path / "real" / "sub", target_is_directory=True)
        cases["dotdot_via_symlink"] = str(tmp_path / "sublink" / ".." / "cfg2")
        # ~\foo is not a tilde path on POSIX.
        cases["tilde_backslash"] = "~\\tilde-cfg"
    return home, cases


CASES = [
    "unset", "empty", "valid", "padded", "missing", "relative", "file", "tilde",
    "trailing_sep", "dot_segment", "symlink", "symlink_trailing_sep", "symlink_dot",
    "dotdot_via_symlink", "tilde_backslash",
]


@pytest.mark.parametrize("case", CASES)
def test_statusline_matches_python_claude_home(tmp_path, case):
    home, cases = _cases(tmp_path)
    if case not in cases:
        pytest.skip("symlink cases are POSIX-only (Windows junction rules differ by design)")
    value = cases[case]
    expected = _python_claude_home(home, value)
    (expected / "token-optimizer").mkdir(parents=True, exist_ok=True)
    _run(home, value)
    written = sorted(p.parent.parent for p in tmp_path.rglob("live-fill.json"))
    assert written == [expected], (
        f"CLAUDE_CONFIG_DIR={value!r}: python claude_home()={expected}, "
        f"statusline wrote live-fill.json under {written}")


# The VS Code companion keeps its own copy of the resolver (vscode-extension/src/
# paths.ts). It has no JS test harness, so run the .ts directly through Node's
# type stripping and hold it to the same Python answers. Skips on a Node too old
# to strip types rather than failing.
PATHS_TS = ROOT / "vscode-extension" / "src" / "paths.ts"


def _ts_claude_dir(tmp_path: Path, home: Path, value):
    runner = tmp_path / "resolve.mjs"
    runner.write_text(
        f"import {{ resolveClaudeDir }} from {PATHS_TS.as_uri()!r};\n"
        "const v = process.argv[3];\n"
        "const env = v === '__UNSET__' ? {} : { CLAUDE_CONFIG_DIR: v };\n"
        "console.log(resolveClaudeDir(process.argv[2], env));\n")
    arg = "__UNSET__" if value is None else value
    p = subprocess.run(["node", "--experimental-strip-types", "--no-warnings",
                        str(runner), str(home), arg],
                       env=_env(home, None), capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        pytest.skip(f"node cannot run paths.ts directly: {p.stderr.strip()[:200]}")
    return Path(p.stdout.strip())


@pytest.mark.parametrize("case", CASES)
def test_vscode_paths_matches_python_claude_home(tmp_path, case):
    home, cases = _cases(tmp_path)
    if case not in cases:
        pytest.skip("symlink cases are POSIX-only (Windows junction rules differ by design)")
    value = cases[case]
    assert _ts_claude_dir(tmp_path, home, value) == _python_claude_home(home, value), \
        f"paths.ts and claude_home() disagree for CLAUDE_CONFIG_DIR={value!r}"
