#!/usr/bin/env python3
"""Regression tests for Codex uninstall residue cleanup (issue #78, B2).

Before the fix, ``codex-install --uninstall`` only stripped Token Optimizer
groups from ``hooks.json``. It left the ``compact_prompt`` /
``experimental_compact_prompt_file`` keys and the ``[tui]`` status-line block
in ``~/.codex/config.toml`` — so uninstall lied to users about being clean.

The fix makes ``codex_install.uninstall`` also call
``codex_compact_prompt.uninstall`` and ``codex_statusline.uninstall``, which
reverse EXACTLY what the corresponding installs write (managed block + prompt
file + commented-out originals), idempotently, never clobbering user keys.

Run directly:  python3 tests/test_codex_uninstall_residue.py
Exits non-zero on first failure.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import codex_compact_prompt  # noqa: E402
import codex_install  # noqa: E402
import codex_statusline  # noqa: E402
import runtime_env  # noqa: E402


def _setup_temp_codex_home():
    """Point codex_home at a temp dir for every module that imported it.

    The codex_* modules bind ``codex_home`` at import time
    (``from runtime_env import codex_home``), so patching
    ``runtime_env.codex_home`` alone is not enough — each module's own
    binding must be repointed too.
    """
    tmp = Path(tempfile.mkdtemp(prefix=".t_codex_uninstall_", dir=str(Path.home())))
    (tmp / "token-optimizer").mkdir(parents=True, exist_ok=True)
    saved = {
        "runtime_env": runtime_env.codex_home,
        "codex_compact_prompt": codex_compact_prompt.codex_home,
        "codex_statusline": codex_statusline.codex_home,
        "codex_install": codex_install.codex_home,
    }
    runtime_env.codex_home = lambda: tmp
    codex_compact_prompt.codex_home = lambda: tmp
    codex_statusline.codex_home = lambda: tmp
    codex_install.codex_home = lambda: tmp
    return tmp, {"_tmp": tmp, "mods": saved}


def _restore_codex_home(saved):
    shutil.rmtree(saved["_tmp"], ignore_errors=True)
    runtime_env.codex_home = saved["mods"]["runtime_env"]
    codex_compact_prompt.codex_home = saved["mods"]["codex_compact_prompt"]
    codex_statusline.codex_home = saved["mods"]["codex_statusline"]
    codex_install.codex_home = saved["mods"]["codex_install"]


def test_compact_prompt_uninstall_removes_managed_block_and_prompt():
    tmp, saved = _setup_temp_codex_home()
    try:
        config = tmp / "config.toml"
        # Install first (writes prompt + managed block).
        action = codex_compact_prompt.install()
        assert action in ("installed", "updated")
        assert (tmp / "token-optimizer" / "codex-compact-prompt.md").exists()
        text = config.read_text(encoding="utf-8")
        assert codex_compact_prompt.MANAGED_BEGIN in text
        # Now uninstall.
        action = codex_compact_prompt.uninstall()
        assert action == "removed"
        text = config.read_text(encoding="utf-8")
        assert codex_compact_prompt.MANAGED_BEGIN not in text
        assert codex_compact_prompt.MANAGED_END not in text
        assert "experimental_compact_prompt_file" not in text
        assert not (tmp / "token-optimizer" / "codex-compact-prompt.md").exists()
    finally:
        _restore_codex_home(saved)


def test_compact_prompt_uninstall_restores_commented_originals():
    """A --force install comments out user keys; uninstall restores them."""
    tmp, saved = _setup_temp_codex_home()
    try:
        config = tmp / "config.toml"
        config.write_text(
            'experimental_compact_prompt_file = "/user/orig.md"\n',
            encoding="utf-8",
        )
        codex_compact_prompt.install(force=True)
        text = config.read_text(encoding="utf-8")
        assert "# replaced by Token Optimizer:" in text
        assert "/user/orig.md" in text  # preserved as a comment
        # Uninstall restores the user's original line.
        codex_compact_prompt.uninstall()
        text = config.read_text(encoding="utf-8")
        assert 'experimental_compact_prompt_file = "/user/orig.md"' in text
        assert codex_compact_prompt.MANAGED_BEGIN not in text
    finally:
        _restore_codex_home(saved)


def test_compact_prompt_uninstall_idempotent():
    tmp, saved = _setup_temp_codex_home()
    try:
        # Uninstall on a clean config is a no-op.
        (tmp / "config.toml").write_text("# user config\n", encoding="utf-8")
        action = codex_compact_prompt.uninstall()
        assert action == "noop"
    finally:
        _restore_codex_home(saved)


def test_compact_prompt_uninstall_never_touches_user_keys():
    tmp, saved = _setup_temp_codex_home()
    try:
        config = tmp / "config.toml"
        config.write_text(
            'model = "gpt-5.4"\n'
            'model_reasoning_effort = "high"\n'
            'experimental_compact_prompt_file = "/user/orig.md"\n',
            encoding="utf-8",
        )
        codex_compact_prompt.install(force=True)
        codex_compact_prompt.uninstall()
        text = config.read_text(encoding="utf-8")
        assert 'model = "gpt-5.4"' in text
        assert 'model_reasoning_effort = "high"' in text
        assert 'experimental_compact_prompt_file = "/user/orig.md"' in text
    finally:
        _restore_codex_home(saved)


def test_statusline_uninstall_removes_managed_block():
    tmp, saved = _setup_temp_codex_home()
    try:
        config = tmp / "config.toml"
        codex_statusline.install()
        text = config.read_text(encoding="utf-8")
        assert codex_statusline.MANAGED_BEGIN in text
        assert "[tui]" in text
        codex_statusline.uninstall()
        text = config.read_text(encoding="utf-8")
        assert codex_statusline.MANAGED_BEGIN not in text
        assert codex_statusline.MANAGED_END not in text
        # The [tui] header TO added (table now empty) is dropped.
        assert "[tui]" not in text
    finally:
        _restore_codex_home(saved)


def test_statusline_uninstall_keeps_user_tui_content():
    tmp, saved = _setup_temp_codex_home()
    try:
        config = tmp / "config.toml"
        config.write_text(
            "[tui]\nstatus_line = [\"model\"]\n",
            encoding="utf-8",
        )
        codex_statusline.install(force=True)
        codex_statusline.uninstall()
        text = config.read_text(encoding="utf-8")
        # User's [tui] table + status_line restored.
        assert "[tui]" in text
        assert 'status_line = ["model"]' in text
        assert codex_statusline.MANAGED_BEGIN not in text
    finally:
        _restore_codex_home(saved)


def test_statusline_uninstall_idempotent():
    tmp, saved = _setup_temp_codex_home()
    try:
        (tmp / "config.toml").write_text("# clean\n", encoding="utf-8")
        assert codex_statusline.uninstall() == "noop"
    finally:
        _restore_codex_home(saved)


def test_codex_install_uninstall_invokes_both_residue_cleanups():
    """codex_install.uninstall must call both compact_prompt and statusline uninstall."""
    tmp, saved = _setup_temp_codex_home()
    try:
        config = tmp / "config.toml"
        # Simulate a prior install: hooks + compact prompt + statusline.
        codex_compact_prompt.install()
        codex_statusline.install()
        hooks_path = tmp / "hooks.json"
        hooks_path.write_text(
            '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "token-optimizer/scripts/measure.py session-end-flush"}]}]}}',
            encoding="utf-8",
        )
        # Run uninstall via the public API (global, not dry-run).
        path, action, details = codex_install.uninstall(
            Path("."), is_global=True, dry_run=False
        )
        assert action == "removed"
        assert details["compact_prompt"] == "removed"
        assert details["status_line"] == "removed"
        text = config.read_text(encoding="utf-8")
        assert codex_compact_prompt.MANAGED_BEGIN not in text
        assert codex_statusline.MANAGED_BEGIN not in text
        # hooks.json stripped of TO groups.
        import json
        hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        assert "Stop" not in hooks.get("hooks", {})
    finally:
        _restore_codex_home(saved)


def test_codex_install_uninstall_dry_run_does_not_write():
    tmp, saved = _setup_temp_codex_home()
    try:
        config = tmp / "config.toml"
        codex_compact_prompt.install()
        before = config.read_text(encoding="utf-8")
        hooks_path = tmp / "hooks.json"
        hooks_path.write_text(
            '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "token-optimizer/scripts/measure.py session-end-flush"}]}]}}',
            encoding="utf-8",
        )
        hooks_before = hooks_path.read_text(encoding="utf-8")
        path, action, details = codex_install.uninstall(
            Path("."), is_global=True, dry_run=True
        )
        assert action == "removed"
        # Dry-run plan keys present.
        assert "would_remove_config_block" in details["compact_prompt"]
        assert "would_remove_config_block" in details["status_line"]
        # Nothing written.
        assert config.read_text(encoding="utf-8") == before
        assert hooks_path.read_text(encoding="utf-8") == hooks_before
    finally:
        _restore_codex_home(saved)


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
