"""Antigravity hook interpreter resolution: trust-gated, symlink-safe.

The persisted hook command bakes in ONE interpreter path at install time.
That path must come from the shared trust gate (an untrusted sys.executable,
e.g. a writable venv, must never be persisted) and must be the RESOLVED
realpath (persisting a symlink leaves a swap window between install and
hook fire).
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


def _load():
    import importlib as _il
    _il.invalidate_caches()
    sys.path.insert(0, str(SCRIPTS))
    return _il.import_module("antigravity_install")


@pytest.fixture()
def mod():
    return _load()


def test_untrusted_sys_executable_is_not_persisted(mod, monkeypatch, tmp_path):
    """A writable venv interpreter as sys.executable must be skipped in favor
    of a trusted PATH candidate."""
    venv_py = tmp_path / "venv" / "bin" / "python3"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("#!/bin/sh\n")
    os.chmod(venv_py, 0o777)  # world-writable: never trusted
    trusted_py = tmp_path / "trusted" / "python3"
    trusted_py.parent.mkdir(parents=True)
    trusted_py.write_text("#!/bin/sh\n")
    os.chmod(trusted_py, 0o755)
    os.chmod(trusted_py.parent, 0o755)

    monkeypatch.setattr(mod.sys, "executable", str(venv_py))
    monkeypatch.setattr(mod.shutil, "which", lambda name: str(trusted_py))
    resolved = mod._resolve_safe_python()
    assert resolved != str(venv_py)
    assert resolved == os.path.realpath(str(trusted_py))


def test_persists_realpath_not_symlink(mod, monkeypatch, tmp_path):
    """A symlinked trusted interpreter persists as its realpath."""
    target = tmp_path / "real" / "python3"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n")
    os.chmod(target, 0o755)
    os.chmod(target.parent, 0o755)
    link = tmp_path / "link/python3"
    link.parent.mkdir(parents=True)
    os.symlink(target, link)

    monkeypatch.setattr(mod.sys, "executable", "")
    monkeypatch.setattr(mod.shutil, "which", lambda name: str(link))
    resolved = mod._resolve_safe_python()
    assert resolved == os.path.realpath(str(target))
    assert not resolved.startswith(str(link.parent))


def test_no_trusted_candidate_lists_reasons(mod, monkeypatch, tmp_path):
    """When every candidate fails the gate, the error names each candidate."""
    bad = tmp_path / "bad" / "python3"
    bad.parent.mkdir(parents=True)
    bad.write_text("#!/bin/sh\n")
    os.chmod(bad, 0o777)
    monkeypatch.setattr(mod.sys, "executable", str(bad))
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    monkeypatch.delenv("TOKEN_OPTIMIZER_PYTHON", raising=False)
    with pytest.raises(RuntimeError) as exc_info:
        mod._resolve_safe_python()
    assert "sys.executable" in str(exc_info.value)
    assert str(bad) in str(exc_info.value)
