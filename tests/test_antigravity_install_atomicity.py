"""Antigravity plugin-dir lifecycle: atomic install swap, safe uninstall.

Concurrent installs (or an install racing a hook reader) must never surface a
half-copied plugin dir, and uninstall must convert filesystem errors into the
installer's RuntimeError contract instead of leaking a raw traceback.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

# antigravity_install refuses native Windows by design (the persisted hook
# command is POSIX-shell quoted); install/uninstall/doctor paths are POSIX-only.
pytestmark = pytest.mark.skipif(os.name == "nt", reason="install paths are POSIX-only on this adapter")

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


def _load():
    importlib.invalidate_caches()
    sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("antigravity_install")


@pytest.fixture()
def mod():
    return _load()


def test_install_is_atomic_and_complete(mod, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    result = mod.install(home=home)
    pdir = Path(result["plugin_dir"])
    # Every payload module present, manifests valid, nothing left staged.
    for name in mod._PAYLOAD_MODULES:
        assert (pdir / name).is_file(), f"missing payload module {name}"
    assert json.loads((pdir / "hooks.json").read_text(encoding="utf-8"))
    assert json.loads((pdir / "plugin.json").read_text(encoding="utf-8"))
    leftovers = [p for p in pdir.parent.iterdir() if p.name.startswith(f".{pdir.name}.")]
    assert leftovers == [], f"staging/trash dirs leaked: {leftovers}"


def test_reinstall_swaps_atomically_no_partial_state(mod, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    mod.install(home=home)
    result = mod.install(home=home)
    pdir = Path(result["plugin_dir"])
    for name in mod._PAYLOAD_MODULES:
        assert (pdir / name).is_file(), f"missing payload module {name}"
    leftovers = [p for p in pdir.parent.iterdir() if p.name.startswith(f".{pdir.name}.")]
    assert leftovers == [], f"staging/trash dirs leaked: {leftovers}"


def test_concurrent_installs_leave_a_complete_plugin_dir(mod, tmp_path):
    import multiprocessing as mp

    def _install(home_str):
        import sys as _sys
        _sys.path.insert(0, str(SCRIPTS))
        import antigravity_install as ai
        ai.install(home=Path(home_str))

    home = tmp_path / "home"
    home.mkdir()
    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=_install, args=(str(home),)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
        assert p.exitcode == 0

    pdir = home / "config" / "plugins" / mod.HOOK_NAME
    assert pdir.is_dir()
    for name in mod._PAYLOAD_MODULES:
        assert (pdir / name).is_file(), f"missing payload module {name}"
    assert json.loads((pdir / "hooks.json").read_text(encoding="utf-8"))
    assert json.loads((pdir / "plugin.json").read_text(encoding="utf-8"))
    leftovers = [p for p in pdir.parent.iterdir() if p.name.startswith(f".{pdir.name}.")]
    assert leftovers == [], f"staging/trash dirs leaked: {leftovers}"


def test_uninstall_converts_oserror_to_runtime_error(mod, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    mod.install(home=home)
    monkeypatch.setattr(mod.shutil, "rmtree", lambda *a, **k: (_ for _ in ()).throw(OSError("disk gone")))
    with pytest.raises(RuntimeError):
        mod.uninstall(home=home)


@pytest.fixture(autouse=True)
def _pin_trusted_python(trusted_python):
    """Pin a gate-trusted interpreter for every install in this file (the
    hosted-CI system interpreter is world-writable and correctly rejected)."""
    return trusted_python
