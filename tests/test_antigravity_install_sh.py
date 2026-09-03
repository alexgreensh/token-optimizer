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
import shutil
import subprocess
from pathlib import Path

import pytest

# antigravity_install refuses native Windows by design (the persisted hook
# command is POSIX-shell quoted); install/uninstall/doctor paths are POSIX-only.
pytestmark = pytest.mark.skipif(os.name == "nt", reason="install paths are POSIX-only on this adapter")

REPO = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO / "install.sh"


def _detail(r: subprocess.CompletedProcess) -> str:
    """Render both streams so a Windows runner failure names the real cause
    instead of an empty stderr (install.sh's `fail` writes to stdout)."""
    return "\n--- stdout ---\n" + r.stdout + "\n--- stderr ---\n" + r.stderr


def _install_bash():
    """Resolve the bash install.sh actually targets on each platform. On
    Windows, shutil.which("bash") often resolves WSL's
    C:\\Windows\\System32\\bash.exe first (it exits 1 with "WSL has no
    installed distributions" on GitHub runners), while the supported runtime
    for install.sh is Git Bash — same resolution rule as
    test_windows_hook_launcher._hook_runtime_bash."""
    if os.name != "nt":
        return shutil.which("bash") or "bash"
    for c in (
        os.environ.get("GIT_BASH"),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ):
        if c and Path(c).exists():
            return c
    b = shutil.which("bash")
    if b and "System32" in b:  # WSL launcher — not the install runtime
        return None
    return b


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
    # Windows resolves Path.home() from USERPROFILE (+ HOMEDRIVE/HOMEPATH), not
    # HOME, so pin those too or the measure.py subprocess reads the real
    # runner profile and installs into the wrong ~/.gemini tree.
    if os.name == "nt":
        drive, _, tail = str(home).partition(os.sep)
        env["USERPROFILE"] = str(home)
        if ":" in drive:
            env["HOMEDRIVE"] = drive
            env["HOMEPATH"] = os.sep + tail
    env.pop("TOKEN_OPTIMIZER_ANTIGRAVITY_HOME", None)
    bash = _install_bash()
    if bash is None:
        pytest.skip(
            "Git Bash (the supported Windows runtime for install.sh) not found; "
            "only WSL's System32 bash.exe is available"
        )
    return subprocess.run(
        [bash, str(INSTALL_SH), "--antigravity", *flags],
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
    assert r.returncode == 0, _detail(r)
    assert "Would install" in r.stdout, _detail(r)
    assert not (home / _PLUGIN).exists()
    assert not (home / _CONSENT).exists()


def test_install_then_uninstall_is_idempotent_and_keeps_data(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    r = _run(home)
    assert r.returncode == 0, _detail(r)

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
    assert r2.returncode == 0, _detail(r2)
    assert not pdir.exists()
    # Consent + session/trends data are deliberately left in place: an
    # uninstall must never delete collected data.
    assert consent.is_file()


def test_unknown_flag_degrades_to_install_without_traceback(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    r = _run(home, "--bogus")
    assert r.returncode == 0, _detail(r)
    assert "Traceback" not in r.stderr, _detail(r)
    assert "installed" in r.stdout.lower(), _detail(r)
    assert (home / _PLUGIN).is_dir()


def test_no_unpinned_pull_in_any_sparse_materialization_path():
    """Every sparse-checkout materialization path must pin to the commit the
    checkout is at. An unpinned `git pull` would let a moved upstream deliver
    arbitrary code into the plugin dir, executed on every hook."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "git pull --ff-only" not in text
    # Every sparse-checkout add lives inside the pinned helper.
    adds = [ln for ln in text.splitlines() if "sparse-checkout add" in ln]
    assert adds, "materialization paths vanished from install.sh"
    helper_body = text.split("_materialize_from_pin() {", 1)[1].split("}", 1)[0]
    assert "rev-parse HEAD" in helper_body
    assert "pull" not in helper_body


@pytest.fixture(autouse=True)
def _pin_trusted_python(trusted_python):
    """Pin a gate-trusted interpreter for every install in this file (the
    hosted-CI system interpreter is world-writable and correctly rejected)."""
    return trusted_python
