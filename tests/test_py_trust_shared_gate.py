"""Shared interpreter trust gate: one gate, every adapter.

All hook installers persist an absolute interpreter path into a command the
host agent runs on every tool call. The gate (ownership, writability, null
bytes, Windows semantics) must be identical across adapters, and the
antigravity installer must refuse native Windows rather than persist a hook
command cmd.exe cannot parse.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

ADAPTER_MODULES = ("cursor_install", "copilot_install", "antigravity_install")


def _load(name: str):
    importlib.invalidate_caches()
    return importlib.import_module(name)


@pytest.fixture(scope="module")
def adapters():
    import sys as _sys
    _sys.path.insert(0, str(SCRIPTS))
    return {name: _load(name) for name in ADAPTER_MODULES}


@pytest.mark.parametrize("module", ADAPTER_MODULES)
def test_trust_gate_rejects_world_writable_interpreter(adapters, module, tmp_path):
    mod = adapters[module]
    f = tmp_path / "python3"
    f.write_text("#!/bin/sh\n")
    os.chmod(f, 0o777)
    assert mod._py_path_is_trusted(str(f)) is False


@pytest.mark.parametrize("module", ADAPTER_MODULES)
def test_trust_gate_rejects_null_byte_path(adapters, module):
    # A null-byte path must be rejected (as a reason string), never raise.
    mod = adapters[module]
    reason = mod._py_trust_reason("/usr/bin/python3\x00/tmp/evil")
    assert isinstance(reason, str) and reason


@pytest.mark.parametrize("module", ADAPTER_MODULES)
def test_trust_gate_rejects_missing_interpreter(adapters, module, tmp_path):
    mod = adapters[module]
    assert mod._py_path_is_trusted(str(tmp_path / "no-such-python")) is False


@pytest.mark.parametrize("module", ADAPTER_MODULES)
def test_trust_gate_accepts_owned_non_writable_interpreter(adapters, module, tmp_path):
    mod = adapters[module]
    f = tmp_path / "python3"
    f.write_text("#!/bin/sh\n")
    os.chmod(f, 0o755)
    os.chmod(tmp_path, 0o755)
    assert mod._py_path_is_trusted(str(f)) is True


@pytest.mark.parametrize("module", ADAPTER_MODULES)
def test_trust_gate_rejects_foreign_group_writable_dir(adapters, module, tmp_path, monkeypatch):
    mod = adapters[module]
    if os.name == "nt" or not hasattr(os, "geteuid"):
        pytest.skip("ownership semantics are POSIX-only")
    if os.geteuid() == 0:
        pytest.skip("root bypasses group checks")
    d = tmp_path / "bin"
    d.mkdir()
    f = d / "python3"
    f.write_text("#!/bin/sh\n")
    os.chmod(f, 0o755)
    os.chmod(d, 0o775)
    # Simulate a foreign-owned group-writable dir: chown to another uid needs
    # root, so patch stat to report a dir owned by neither root nor us while
    # keeping the real file stat.
    import py_trust
    real_stat = os.stat
    dir_stat = real_stat(d)
    foreign_uid = 1 if os.geteuid() != 1 else 2

    def fake_stat(path, *a, **k):
        st = real_stat(path, *a, **k)
        if os.path.samestat(st, dir_stat) or str(path) == str(d):
            fields = list(st)[:10]
            fields[4] = foreign_uid  # st_uid
            return os.stat_result(tuple(fields))
        return st

    monkeypatch.setattr(py_trust.os, "stat", fake_stat)
    assert mod._py_path_is_trusted(str(f)) is False


def test_antigravity_install_refuses_native_windows(adapters, monkeypatch):
    """On nt, install must raise before writing anything, mirroring the other
    adapters: hooks that can never fire must never be registered."""
    mod = adapters["antigravity_install"]
    monkeypatch.setattr(mod.os, "name", "nt")
    with pytest.raises(RuntimeError) as exc_info:
        mod.install(dry_run=False, home=None)
    assert "Windows" in str(exc_info.value)


def test_antigravity_windows_refusal_is_module_constant_consistent(adapters):
    """The refusal must live in install() itself (not a wrapper), so a caller
    importing install() directly still gets the gate."""
    import inspect
    mod = adapters["antigravity_install"]
    src = inspect.getsource(mod.install)
    assert 'os.name == "nt"' in src
