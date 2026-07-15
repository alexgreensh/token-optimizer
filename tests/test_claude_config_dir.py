#!/usr/bin/env python3
"""Regression tests for CLAUDE_CONFIG_DIR resolution in runtime_env.claude_home().

Claude Code lets CLAUDE_CONFIG_DIR relocate its config (projects/, settings.json,
the trends DB, the dashboard) anywhere -- including OUTSIDE $HOME (containers, CI
runners, relocated volumes). claude_home() must honor a valid override so those
users are not silently pinned to a stale ~/.claude, while still rejecting unsafe
inputs (symlinks, relative paths).

Run directly:  python3 tests/test_claude_config_dir.py
Exits non-zero on first failure.
"""

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import runtime_env  # noqa: E402

ENV = "CLAUDE_CONFIG_DIR"


def _claude_home_with(value):
    """Call claude_home() with CLAUDE_CONFIG_DIR set to `value` (None = unset)."""
    saved = os.environ.get(ENV)
    try:
        if value is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = value
        return runtime_env.claude_home()
    finally:
        if saved is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = saved


def test_unset_falls_back_to_default():
    assert _claude_home_with(None) == Path.home() / ".claude"


def test_honors_valid_out_of_home_dir():
    # A real absolute dir that is NOT under $HOME (the case the old under-$HOME
    # confinement broke). Use the system temp root, which on macOS/Linux is
    # outside the user home.
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        resolved = _claude_home_with(d)
        assert resolved == Path(d).resolve(), f"expected {d}, got {resolved}"
        # And it must NOT have silently fallen back to ~/.claude.
        assert resolved != Path.home() / ".claude"


def test_honors_valid_in_home_dir():
    with tempfile.TemporaryDirectory(dir=str(Path.home())) as d:
        assert _claude_home_with(d) == Path(d).resolve()


def test_rejects_symlink():
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        real = Path(d) / "real"
        real.mkdir()
        link = Path(d) / "link"
        link.symlink_to(real)
        # A symlinked CLAUDE_CONFIG_DIR is rejected -> fall back to default.
        assert _claude_home_with(str(link)) == Path.home() / ".claude"


def test_rejects_relative_path():
    assert _claude_home_with("relative/not/absolute") == Path.home() / ".claude"


def test_rejects_nonexistent_dir():
    assert _claude_home_with("/tmp/does-not-exist-token-optimizer-xyz") == Path.home() / ".claude"


def test_empty_string_falls_back():
    assert _claude_home_with("   ") == Path.home() / ".claude"


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
