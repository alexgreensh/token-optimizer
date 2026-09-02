"""R13a: bash_compress.main() self-validates its own argv.

bash_compress.py is dispatched as a shell-visible wrapper (``... exec python
bash_compress.py <original command tokens>``). If a host ever caches an
"always allow" on that wrapper prefix, the wrapper itself must refuse to run
anything its bridge would not have rewritten: dangerous shell characters are
rejected and the command must be on the compression whitelist. These tests
prove the gate fires BEFORE subprocess.run, so nothing is ever spawned.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


def _load(name: str):
    importlib.invalidate_caches()
    return importlib.import_module(name)


@pytest.fixture()
def bc(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    bc = _load("bash_compress")
    # Redirect any archive side effects into a scratch dir so the gate tests
    # stay hermetic if a command ever slips through.
    ar = _load("archive_result")
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True)
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(snapshot_dir))
    monkeypatch.setattr(ar, "SNAPSHOT_DIR", snapshot_dir)
    monkeypatch.setattr(ar, "TRENDS_DB", snapshot_dir / "trends.db")
    return bc


def _assert_refused(bc, monkeypatch, capsys, argv):
    """main() must exit 1 and never call subprocess.run for a refused argv."""
    calls = []
    monkeypatch.setattr(bc.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(bc.sys, "argv", argv)
    with pytest.raises(SystemExit) as exc_info:
        bc.main()
    assert exc_info.value.code == 1
    assert calls == [], "self-check let a refused command reach subprocess.run"
    err = capsys.readouterr().err
    assert "not eligible for compression" in err


def test_dangerous_command_refused_before_spawn(bc, monkeypatch, capsys):
    """`rm -rf /` has no metacharacters fingerprint risk but must be refused;
    the categorical dangerous-char gate plus whitelist both block it."""
    _assert_refused(bc, monkeypatch, capsys, ["bash_compress.py", "rm", "-rf", "/"])


def test_injection_command_refused_before_spawn(bc, monkeypatch, capsys):
    """`curl x | sh` is the canonical injection shape; refused before spawn."""
    _assert_refused(bc, monkeypatch, capsys, ["bash_compress.py", "curl", "x", "|", "sh"])


def test_unknown_non_whitelisted_command_refused(bc, monkeypatch, capsys):
    """An innocent-looking but non-whitelisted command is still refused: the
    wrapper exists only to run the compression whitelist."""
    _assert_refused(bc, monkeypatch, capsys, ["bash_compress.py", "someunknowncmd"])


def test_whitelisted_command_passes_gate_and_spawns(bc, monkeypatch, capsys, tmp_path):
    """A whitelisted read-only command reaches subprocess.run (with shell=False)."""
    calls = []

    class _FakeResult:
        stdout = "short output\n"
        stderr = ""
        returncode = 0

    monkeypatch.setattr(bc.subprocess, "run", lambda *a, **k: calls.append((a, k)) or _FakeResult())
    monkeypatch.setattr(bc.sys, "argv", ["bash_compress.py", "ls", "-la", "/usr/bin"])
    with pytest.raises(SystemExit) as exc_info:
        bc.main()
    assert exc_info.value.code == 0
    assert len(calls) == 1, "whitelisted command did not reach subprocess.run exactly once"
    args, kwargs = calls[0]
    assert args[0] == ["ls", "-la", "/usr/bin"]
    assert kwargs["shell"] is False, "capopt path must keep shell=False"
