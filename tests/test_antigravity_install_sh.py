"""U9 — ``install.sh --antigravity`` end-to-end.

The installer wires Token Optimizer into Antigravity as a user-level plugin
directory (``~/.gemini/config/plugins/token-optimizer/``) and records the R20
data-consent flag in ``~/.gemini/token-optimizer/config.json``. These tests run
the real ``install.sh`` against a temp ``HOME`` so nothing touches the host's
``~/.gemini``, and exercise the three behaviours the plan pins: dry-run writes
nothing, install-then-uninstall is idempotent and leaves data behind, and an
unknown flag degrades to a normal install instead of a traceback.

Run: python3 -m pytest tests/test_antigravity_install_sh.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO / "install.sh"


def _run(home: Path, *flags: str) -> subprocess.CompletedProcess:
    tmp = home / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        # Pin the Claude config root as well. The antigravity installer never
        # reads ~/.claude, but measure.py's runtime_env resolves a Claude home
        # as a shared fallback, so pinning both keeps the subprocess fully
        # isolated from this machine (host-safety guard, incident 2026-07-30).
        "CLAUDE_CONFIG_DIR": str(home),
        "TMPDIR": str(tmp),
        # Deterministic runtime for the measure.py subprocess; no host process
        # scan, no chance of auto-detecting the real ~/.gemini on this box.
        "TOKEN_OPTIMIZER_RUNTIME": "antigravity",
        "TOKEN_OPTIMIZER_NO_PROC_SCAN": "1",
    })
    env.pop("TOKEN_OPTIMIZER_ANTIGRAVITY_HOME", None)
    return subprocess.run(
        ["bash", str(INSTALL_SH), "--antigravity", *flags],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


_PLUGIN = ".gemini/config/plugins/token-optimizer"
_CONSENT = ".gemini/token-optimizer/config.json"


def test_dry_run_prints_would_install_and_writes_nothing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    r = _run(home, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "Would install" in r.stdout
    assert not (home / _PLUGIN).exists()
    assert not (home / _CONSENT).exists()


def test_install_then_uninstall_is_idempotent_and_keeps_data(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    r = _run(home)
    assert r.returncode == 0, r.stderr

    pdir = home / _PLUGIN
    assert pdir.is_dir()
    assert (pdir / "plugin.json").is_file()
    assert (pdir / "hooks.json").is_file()
    assert (pdir / "measure-path").is_file()
    assert (pdir / "antigravity_hook_bridge.py").is_file()

    consent = home / _CONSENT
    assert consent.is_file()
    cfg = json.loads(consent.read_text(encoding="utf-8"))
    assert cfg["antigravity_consent"] is True

    r2 = _run(home, "--uninstall")
    assert r2.returncode == 0, r2.stderr
    assert not pdir.exists()
    # Consent + session/trends data are deliberately left in place: an
    # uninstall must never delete collected data.
    assert consent.is_file()


def test_unknown_flag_degrades_to_install_without_traceback(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    r = _run(home, "--bogus")
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr
    assert "installed" in r.stdout.lower()
    assert (home / _PLUGIN).is_dir()
