#!/usr/bin/env python3
"""Regression tests for the WSL-root /mnt/ opt-in in runtime_env (issue #78).

Asaf's live bug: under WSL-root, ``$HOME=/root``, so a legit
``COPILOT_HOME=/mnt/c/Users/<you>/.copilot`` (and the CODEX_HOME /
HERMES_HOME / OPENCODE_* equivalents) is REJECTED by the strict
under-``$HOME`` safe-home guard. Token Optimizer then falls back to
``/root/.copilot`` and reads/writes the wrong home, measuring nothing.

The fix (A1) adds a ``/mnt/`` opt-in inside ``_safe_home_from_env`` that is
gated on ACTUAL WSL detection (``/proc/version`` / ``/proc/sys/kernel/osrelease``
contain ``microsoft`` / ``WSL``). The opt-in is STRICTER than the prior
shipped ``measure.py::_resolve_copilot_home_wsl_aware`` version: native-Linux
``/mnt`` mounts stay on the strict path and behavior there is byte-identical.

These cases are covered:

- WSL-root + ``/mnt`` COPILOT_HOME accepted (no "rejected" warning).
- non-WSL ``/mnt`` rejected (strict guard, warning) — byte-identical to before.
- ``/mnt`` symlink rejected.
- ``/etc`` rejected.
- normal under-``$HOME`` value still accepted (strict path unchanged).
- ``codex_home`` / ``hermes_home`` / ``opencode_config_home`` /
  ``opencode_data_home`` share the opt-in (single source of truth).
- ``measure.py::_resolve_copilot_home_wsl_aware`` delegates to
  ``runtime_env.copilot_home`` (no divergent logic).
- ``copilot-doctor`` resolution path: ``copilot_home`` resolves to the
  ``/mnt`` home with no warning (simulated via mnt_root injection + env).

Run directly:  python3 tests/test_runtime_env_wsl_mnt.py
Exits non-zero on first failure.
"""

import io
import os
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import runtime_env  # noqa: E402

# Every env var the safe-home resolvers consult — cleared before each case.
_CONTROLLED_ENV = (
    "COPILOT_HOME",
    "CODEX_HOME",
    "HERMES_HOME",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_DATA_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
)


def _save_env():
    return {k: os.environ.get(k) for k in _CONTROLLED_ENV}


def _restore_env(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _with_env(env=None, wsl=False, mnt_root=None, fn=None):
    """Run ``fn`` under a controlled env + faked WSL context.

    ``wsl`` stands in for the live ``/proc`` WSL detection so the tests are
    deterministic on any host (macOS can't read /proc and can't create
    ``/mnt/c/...``). ``mnt_root`` is the temp dir substituted for ``/mnt``.
    """
    env = env or {}
    saved_env = _save_env()
    saved_wsl = runtime_env._is_wsl_context
    try:
        for k in _CONTROLLED_ENV:
            os.environ.pop(k, None)
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        runtime_env._is_wsl_context = lambda: bool(wsl)
        return fn(mnt_root)
    finally:
        runtime_env._is_wsl_context = saved_wsl
        _restore_env(saved_env)


def _make_mnt_tree(base: Path, rel: str) -> Path:
    """Create base/<rel> as a real directory and return its path."""
    p = base / rel
    p.mkdir(parents=True, exist_ok=True)
    return p


# --- accepted: WSL-root + /mnt COPILOT_HOME --------------------------------

def test_wsl_mnt_copilot_home_accepted_no_warning(tmp_path=None):
    tmp = tmp_path or Path(tempfile.mkdtemp(prefix="t_wsl_mnt_"))
    mnt = _make_mnt_tree(tmp, "mnt")
    copilot_dir = _make_mnt_tree(mnt, "c/Users/asaf/.copilot")

    def body(mnt_root):
        os.environ["COPILOT_HOME"] = str(copilot_dir)
        buf = io.StringIO()
        with redirect_stderr(buf):
            result = runtime_env.copilot_home(mnt_root=mnt_root)
        assert result == copilot_dir.resolve(strict=False), \
            f"expected {copilot_dir} resolved, got {result}"
        assert "rejected" not in buf.getvalue().lower(), \
            f"should NOT print a rejected warning under WSL /mnt opt-in, got: {buf.getvalue()!r}"

    _with_env(wsl=True, mnt_root=mnt, fn=body)


def test_wsl_mnt_opt_in_shared_helper_accepts_codex_hermes_opencode():
    """The opt-in lives in _safe_home_from_env, so all runtimes share it."""
    tmp = Path(tempfile.mkdtemp(prefix="t_wsl_shared_"))
    mnt = _make_mnt_tree(tmp, "mnt")

    cases = [
        ("CODEX_HOME", ".codex"),
        ("HERMES_HOME", ".hermes"),
        ("OPENCODE_CONFIG_DIR", ".config/opencode"),
        ("OPENCODE_DATA_DIR", ".local/share/opencode"),
    ]
    for env_var, sub in cases:
        target = _make_mnt_tree(mnt, f"c/Users/asaf/{sub}")

        def body(mnt_root, env_var=env_var, target=target):
            os.environ[env_var] = str(target)
            buf = io.StringIO()
            with redirect_stderr(buf):
                result = runtime_env._safe_home_from_env(
                    env_var, Path.home() / sub, mnt_root=mnt_root
                )
            assert result == target.resolve(strict=False), \
                f"{env_var}: expected {target} resolved, got {result}"
            assert "rejected" not in buf.getvalue().lower(), \
                f"{env_var}: should NOT warn under WSL /mnt opt-in, got: {buf.getvalue()!r}"

        _with_env(wsl=True, mnt_root=mnt, fn=body)


# --- rejected: non-WSL /mnt stays strict (byte-identical) ------------------

def test_non_wsl_mnt_rejected_with_warning():
    tmp = Path(tempfile.mkdtemp(prefix="t_nonwsl_mnt_"))
    mnt = _make_mnt_tree(tmp, "mnt")
    copilot_dir = _make_mnt_tree(mnt, "c/Users/asaf/.copilot")

    def body(mnt_root):
        os.environ["COPILOT_HOME"] = str(copilot_dir)
        buf = io.StringIO()
        with redirect_stderr(buf):
            result = runtime_env.copilot_home(mnt_root=mnt_root)
        # Non-WSL: /mnt opt-in is gated off -> strict guard rejects -> fallback.
        assert result == Path.home() / ".copilot", \
            f"non-WSL /mnt must fall back to default, got {result}"
        assert "rejected" in buf.getvalue().lower(), \
            f"non-WSL /mnt must warn, got: {buf.getvalue()!r}"

    _with_env(wsl=False, mnt_root=mnt, fn=body)


def test_non_wsl_mnt_helper_returns_none():
    tmp = Path(tempfile.mkdtemp(prefix="t_nonwsl_helper_"))
    mnt = _make_mnt_tree(tmp, "mnt")
    target = _make_mnt_tree(mnt, "c/Users/asaf/.copilot")

    def body(mnt_root):
        result = runtime_env._wsl_mnt_safe_home(target, mnt_root=mnt_root)
        assert result is None, f"non-WSL helper must return None, got {result}"

    _with_env(wsl=False, mnt_root=mnt, fn=body)


# --- rejected: /mnt symlink ------------------------------------------------

def test_wsl_mnt_symlink_rejected():
    tmp = Path(tempfile.mkdtemp(prefix="t_wsl_symlink_"))
    mnt = _make_mnt_tree(tmp, "mnt")
    real = _make_mnt_tree(mnt, "c/Users/asaf/.copilot")
    link = mnt / "c" / "Users" / "asaf" / ".copilot-link"
    if link.exists():
        link.unlink()
    os.symlink(real, link)

    def body(mnt_root):
        os.environ["COPILOT_HOME"] = str(link)
        buf = io.StringIO()
        with redirect_stderr(buf):
            result = runtime_env.copilot_home(mnt_root=mnt_root)
        assert result == Path.home() / ".copilot", \
            f"symlink under /mnt must be rejected, got {result}"
        assert "rejected" in buf.getvalue().lower(), \
            f"symlink under /mnt must warn, got: {buf.getvalue()!r}"

    _with_env(wsl=True, mnt_root=mnt, fn=body)


# --- rejected: /etc (outside /mnt entirely) --------------------------------

def test_wsl_etc_rejected():
    def body(mnt_root):
        os.environ["COPILOT_HOME"] = "/etc"
        buf = io.StringIO()
        with redirect_stderr(buf):
            result = runtime_env.copilot_home(mnt_root=mnt_root)
        assert result == Path.home() / ".copilot", \
            f"/etc must be rejected, got {result}"
        assert "rejected" in buf.getvalue().lower(), \
            f"/etc must warn, got: {buf.getvalue()!r}"

    _with_env(wsl=True, mnt_root=Path("/mnt"), fn=body)


# --- accepted: normal under-$HOME still works (strict path unchanged) ------

def test_under_home_copilot_home_accepted():
    home = Path.home()
    target = home / ".copilot-test-under-home"
    target.mkdir(parents=True, exist_ok=True)
    try:
        def body(mnt_root):
            os.environ["COPILOT_HOME"] = str(target)
            buf = io.StringIO()
            with redirect_stderr(buf):
                result = runtime_env.copilot_home(mnt_root=mnt_root)
            assert result == target.resolve(strict=False), \
                f"under-$HOME value must be accepted, got {result}"
            assert "rejected" not in buf.getvalue().lower(), \
                f"under-$HOME value must not warn, got: {buf.getvalue()!r}"

        _with_env(wsl=False, mnt_root=Path("/mnt"), fn=body)
    finally:
        try:
            target.rmdir()
        except OSError:
            pass


# --- measure.py delegation -------------------------------------------------

def test_measure_resolve_copilot_home_wsl_aware_delegates():
    """_resolve_copilot_home_wsl_aware must delegate to runtime_env.copilot_home."""
    import measure  # noqa: E402

    tmp = Path(tempfile.mkdtemp(prefix="t_measure_deleg_"))
    mnt = _make_mnt_tree(tmp, "mnt")
    copilot_dir = _make_mnt_tree(mnt, "c/Users/asaf/.copilot")

    saved_wsl = runtime_env._is_wsl_context
    saved_env = _save_env()
    try:
        for k in _CONTROLLED_ENV:
            os.environ.pop(k, None)
        os.environ["COPILOT_HOME"] = str(copilot_dir)
        runtime_env._is_wsl_context = lambda: True
        result = measure._resolve_copilot_home_wsl_aware(mnt_root=mnt)
        assert result == copilot_dir.resolve(strict=False), \
            f"delegated resolver must accept WSL /mnt, got {result}"
    finally:
        runtime_env._is_wsl_context = saved_wsl
        _restore_env(saved_env)


# --- copilot-doctor resolution path (simulated) ----------------------------

def test_copilot_doctor_resolution_uses_mnt_home():
    """copilot_doctor reads copilot_home(); under WSL /mnt it must resolve there."""
    import copilot_doctor  # noqa: E402

    tmp = Path(tempfile.mkdtemp(prefix="t_doctor_"))
    mnt = _make_mnt_tree(tmp, "mnt")
    copilot_dir = _make_mnt_tree(mnt, "c/Users/asaf/.copilot")

    saved_wsl = runtime_env._is_wsl_context
    saved_env = _save_env()
    try:
        for k in _CONTROLLED_ENV:
            os.environ.pop(k, None)
        os.environ["COPILOT_HOME"] = str(copilot_dir)
        runtime_env._is_wsl_context = lambda: True
        # copilot_doctor calls copilot_home() with no args -> uses real /mnt.
        # Under the faked WSL context on a non-Linux host, real /mnt may not
        # exist, so we instead assert the resolution path the doctor uses:
        # copilot_home(mnt_root=mnt) returns the /mnt dir (the doctor's
        # root would then be <that>/token-optimizer/plugin).
        root = runtime_env.copilot_home(mnt_root=mnt)
        assert root == copilot_dir.resolve(strict=False), \
            f"copilot-doctor resolution must use the /mnt home, got {root}"
        assert root.name == ".copilot"
    finally:
        runtime_env._is_wsl_context = saved_wsl
        _restore_env(saved_env)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
