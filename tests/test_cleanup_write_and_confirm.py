#!/usr/bin/env python3
"""Symlink/mode preservation on settings writes + confirm gate.

P2-7 os.replace onto SETTINGS_PATH detached a dotfiles-managed symlink
      (settings.json became a regular file, so the user's repo silently
      stopped tracking it) and dropped the mode from 0644 to mkstemp's 0600.
P2-8 `dry = "--dry-run" in args` meant a typo ("--dryrun", "--dry_run")
      silently performed the REAL destructive run.

Run: python3 -m pytest tests/test_cleanup_write_and_confirm.py -q
"""

from __future__ import annotations
from helpers.measure_split import exec_source_module as _split_exec_module

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
MEASURE = SCRIPTS / "measure.py"


@pytest.fixture()
def measure(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", MEASURE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    _split_exec_module(mod)
    monkeypatch.setattr(mod, "_SETTINGS_LOCK_PATH", tmp_path / ".settings.lock")
    yield mod
    sys.modules.pop("measure", None)


# ---------------------------------------------------------------------------
# P2-7: symlinked settings.json + mode
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX symlink + file-mode semantics; Windows symlink_to needs "
    "privilege and cannot represent 0o644",
)
def test_symlinked_settings_stays_a_symlink(measure, tmp_path, monkeypatch):
    real_dir = tmp_path / "dotfiles"
    real_dir.mkdir()
    real = real_dir / "settings.json"
    real.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    real.chmod(0o644)

    home = tmp_path / "claude"
    home.mkdir()
    link = home / "settings.json"
    link.symlink_to(real)
    monkeypatch.setattr(measure, "SETTINGS_PATH", link)

    assert measure._write_settings_atomic({"model": "sonnet"}) is True

    assert link.is_symlink(), "the dotfiles symlink was replaced by a regular file"
    assert link.resolve() == real.resolve()
    assert json.loads(real.read_text(encoding="utf-8")) == {"model": "sonnet"}, (
        "the write did not reach the symlink target"
    )


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX file-mode semantics; Windows cannot represent 0o644 "
    "(os.stat reports 0o666)",
)
def test_file_mode_is_preserved(measure, tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    settings.chmod(0o644)
    monkeypatch.setattr(measure, "SETTINGS_PATH", settings)

    measure._write_settings_atomic({"a": 1})

    mode = stat.S_IMODE(os.stat(settings).st_mode)
    assert mode == 0o644, f"mode changed to {oct(mode)} (mkstemp default leaks 0600)"


def test_missing_file_still_written(measure, tmp_path, monkeypatch):
    """No destination to copy a mode from -- must still write."""
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(measure, "SETTINGS_PATH", settings)

    assert measure._write_settings_atomic({"a": 1}) is True
    assert json.loads(settings.read_text(encoding="utf-8")) == {"a": 1}


# ---------------------------------------------------------------------------
# P2-8: confirm gate on the destructive path
# ---------------------------------------------------------------------------

def _run_cleanup(tmp_path, *flags):
    env = dict(os.environ)
    # HOST SAFETY (incident 2026-07-30): `cleanup` resolves settings.json from
    # CLAUDE_DIR = claude_home(), which is INDEPENDENT of
    # TOKEN_OPTIMIZER_SNAPSHOT_DIR. Pinning only the snapshot dir left these
    # subprocesses pointed at the developer's REAL ~/.claude, and
    # `cleanup --confirm` removed a live statusLine. claude_home() honors
    # CLAUDE_CONFIG_DIR (absolute, existing, non-symlink), so every subprocess
    # here MUST pin it. _assert_isolated below proves the pin actually held
    # rather than trusting it.
    cfg = tmp_path / "claude-home"
    cfg.mkdir(parents=True, exist_ok=True)
    env["CLAUDE_CONFIG_DIR"] = str(cfg)
    env["HOME"] = str(tmp_path)
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(tmp_path / "data")
    proc = subprocess.run(
        [sys.executable, str(MEASURE), "cleanup", *flags],
        capture_output=True, text=True, env=env, timeout=120,
    )
    _assert_isolated(cfg, proc)
    return proc


def _assert_isolated(cfg: Path, proc) -> None:
    """Fail loudly if the subprocess escaped the fixture.

    claude_home() prints a warning to stderr when CLAUDE_CONFIG_DIR is
    rejected (relative, missing, symlinked) and silently falls back to
    ~/.claude -- exactly the silent escape that caused the incident. Treat any
    such warning as a hard test failure, so a future refactor of the resolver
    cannot quietly re-point these destructive runs at a real machine.
    """
    assert "CLAUDE_CONFIG_DIR" not in (proc.stderr or ""), (
        "cleanup subprocess REJECTED the fixture config dir and fell back to "
        f"the real ~/.claude -- refusing to trust this result.\n{proc.stderr}"
    )
    assert cfg.is_dir()


def test_bare_cleanup_refuses_without_confirm(tmp_path):
    p = _run_cleanup(tmp_path)
    assert p.returncode == 1
    assert "Nothing was changed" in p.stdout
    assert "--confirm" in p.stdout
    assert "PRESERVED" in p.stdout, "must state data is preserved"


def test_typo_dry_run_flag_is_rejected_not_executed(tmp_path):
    """The regression: a mistyped --dry-run used to run for real."""
    for typo in ("--dryrun", "--dry_run", "--dry-runn"):
        p = _run_cleanup(tmp_path, typo)
        assert p.returncode == 2, f"{typo} was not rejected"
        assert "Unknown flag" in p.stdout
        assert "Nothing was changed" in p.stdout


def test_dry_run_needs_no_confirm(tmp_path):
    p = _run_cleanup(tmp_path, "--dry-run")
    assert p.returncode == 0, p.stderr
    assert "Unknown flag" not in p.stdout


def test_confirm_proceeds(tmp_path):
    """The one mutating case -- it must operate on the FIXTURE, provably.

    Seeds a statusLine of ours into the fixture settings.json and asserts the
    fixture file is what changed. If CLAUDE_CONFIG_DIR were ever ignored again,
    the fixture would come back untouched and this fails, instead of quietly
    editing someone's real config.
    """
    cfg = tmp_path / "claude-home"
    cfg.mkdir(parents=True, exist_ok=True)
    settings = cfg / "settings.json"
    settings.write_text(json.dumps({
        "model": "opus",
        "statusLine": {"type": "command",
                       "command": "node '/p/token-optimizer/statusline.js'"},
    }, indent=2), encoding="utf-8")

    p = _run_cleanup(tmp_path, "--confirm")

    assert p.returncode == 0, p.stderr
    assert "Nothing was changed" not in p.stdout
    after = json.loads(settings.read_text(encoding="utf-8"))
    assert "statusLine" not in after, (
        "the FIXTURE settings.json was not modified -- the subprocess did not "
        "operate on the fixture (possible escape to the real ~/.claude)"
    )
    assert after.get("model") == "opus", "user key lost in the fixture"


def test_known_flags_combine(tmp_path):
    p = _run_cleanup(tmp_path, "--dry-run", "--this-install-only")
    assert p.returncode == 0, p.stderr
    assert "Unknown flag" not in p.stdout
