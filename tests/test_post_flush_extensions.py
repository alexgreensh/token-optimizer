"""Post-flush extension loader: off by default, config-dir-only, fail-open,
budget passed through.

Run: python3 -m pytest tests/test_post_flush_extensions.py -v
"""
import importlib
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def m(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("TOKEN_OPTIMIZER_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "snap").mkdir()
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "CLAUDE_DIR", tmp_path / "claude")
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _install_ext(m, body):
    ext_dir = m.CONFIG_DIR / "extensions"
    ext_dir.mkdir(exist_ok=True)
    p = ext_dir / "post_flush.py"
    p.write_text(body)
    os.chmod(str(p), 0o600)
    return p


def test_off_by_default_no_io(m, monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise AssertionError("should not be called")

    monkeypatch.setattr("builtins.open", boom)
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) is None
    assert not calls


def test_loads_only_from_config_dir(m, tmp_path, monkeypatch):
    _install_ext(m, "def run(ctx):\n    return 'ran'\n")
    # a decoy elsewhere must be ignored (no env override exists)
    decoy = tmp_path / "decoy" / "post_flush.py"
    decoy.parent.mkdir()
    decoy.write_text("def run(ctx):\n    raise AssertionError('decoy ran')")
    monkeypatch.setenv("TO_EXTENSIONS_DIR", str(decoy.parent))  # not honoured
    out = m._run_post_flush_extensions(time_left_fn=lambda: 10)
    assert out == "ran"


def test_context_keys(m):
    body = (
        "def run(ctx):\n"
        "    assert set(ctx) >= {'trends_db', 'snapshot_dir', 'config_dir',"
        " 'runtime', 'version', 'time_left_fn'}\n"
        "    return ctx['time_left_fn']()\n")
    _install_ext(m, body)
    assert m._run_post_flush_extensions(time_left_fn=lambda: 7, version="5.13.4") == 7


def test_fail_open_on_extension_exception(m):
    _install_ext(m, "def run(ctx):\n    raise RuntimeError('exploded')\n")
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) is None


def test_fail_open_on_broken_module(m):
    _install_ext(m, "this is not python (((\n")
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) is None


def test_fail_open_on_missing_run(m):
    _install_ext(m, "x = 1\n")
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) is None


def test_world_writable_extension_ignored(m):
    p = _install_ext(m, "def run(ctx):\n    return 'ran'\n")
    os.chmod(str(p), 0o666)
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) is None


def test_worker_completes_and_releases_lock_with_extension(m, monkeypatch):
    _install_ext(m, "def run(ctx):\n    raise RuntimeError('boom')\n")
    m._run_session_end_flush_worker([])
    assert m._acquire_session_end_flush_lock() is not None  # lock released
    m._release_session_end_flush_lock(m._acquire_session_end_flush_lock())


def test_worker_runs_extension_after_flush(m):
    _install_ext(m, "def run(ctx):\n"
                    "    import json, pathlib\n"
                    "    p = pathlib.Path(ctx['snapshot_dir']) / 'ext-marker'\n"
                    "    p.write_text(ctx['version'])\n"
                    "    return True\n")
    m._run_session_end_flush_worker([])
    assert (m.SNAPSHOT_DIR / "ext-marker").read_text() == m.TOKEN_OPTIMIZER_VERSION
