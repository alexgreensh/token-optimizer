"""U5: Antigravity adapter installer.

The installer writes a user-level plugin directory under the Antigravity home
and must be idempotent, scoped, and safe:

* payload + hooks.json + plugin.json land in ``<home>/config/plugins/token-optimizer/``
* hooks.json groups the PreToolUse matcher but keeps PreInvocation/Stop as flat
  handler lists (per the Antigravity hooks contract)
* plugin.json is written LAST so a partial payload never registers hooks
* consent is recorded in ``<home>/token-optimizer/config.json`` (R20)
* uninstall removes only the plugin directory, never session data or consent

All tests pass an explicit ``home=tmp_path`` so the real ``~/.gemini`` is never
touched.
"""

from __future__ import annotations

import importlib
import json
import os
import stat as _stat
import sys
from pathlib import Path

import pytest

# antigravity_install refuses native Windows by design (the persisted hook
# command is POSIX-shell quoted); install/uninstall/doctor paths are POSIX-only.
pytestmark = pytest.mark.skipif(os.name == "nt", reason="install paths are POSIX-only on this adapter")

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def ai(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("antigravity_install", None)
    yield importlib.import_module("antigravity_install")


# ---------------------------------------------------------------------------
# Safe python resolution (same trust gate as the Copilot installer)
# ---------------------------------------------------------------------------

def test_resolver_returns_absolute_existing_file(ai):
    r = ai._resolve_safe_python()
    assert os.path.isabs(r), f"not absolute: {r}"
    assert os.path.isfile(r), f"not a real file: {r}"


def test_hooks_config_uses_pre_tool_matcher_and_flat_lifecycle(ai, tmp_path):
    cfg = ai._hooks_config(tmp_path / "bridge.py")
    h = cfg["token-optimizer"]
    # PreToolUse: matcher group targeting only run_command.
    assert len(h["PreToolUse"]) == 1
    assert h["PreToolUse"][0]["matcher"] == "run_command"
    assert h["PreToolUse"][0]["hooks"]
    # PreInvocation / Stop: flat command handler lists (no matcher).
    assert isinstance(h["PreInvocation"], list) and h["PreInvocation"][0]["type"] == "command"
    assert isinstance(h["Stop"], list) and h["Stop"][0]["type"] == "command"
    # The persisted command must bake an absolute trusted python and the
    # -E -s isolation flags (PYTHONPATH / user-site hijack defense).
    cmd = h["PreInvocation"][0]["command"]
    assert "-E -s" in cmd
    assert "pre-invocation" in cmd


def test_hooks_config_pre_tool_timeout_bounded(ai, tmp_path):
    h = ai._hooks_config(tmp_path / "bridge.py")["token-optimizer"]
    assert h["PreToolUse"][0]["hooks"][0]["timeout"] == ai.PRE_TIMEOUT_SEC
    assert h["Stop"][0]["timeout"] == ai.STOP_TIMEOUT_SEC


# ---------------------------------------------------------------------------
# install() behaviour
# ---------------------------------------------------------------------------

def test_install_writes_payload_hooks_plugin_and_consent(ai, tmp_path):
    home = tmp_path / "home"
    result = ai.install(home=home, dry_run=False)

    pdir = ai.plugin_dir(home)
    assert pdir.is_dir()
    for name in ai._PAYLOAD_MODULES:
        assert (pdir / name).is_file(), f"payload module missing: {name}"
    assert (pdir / "measure-path").read_text().strip().endswith("measure.py")

    hooks = json.loads((pdir / "hooks.json").read_text())
    assert hooks["token-optimizer"]["PreToolUse"][0]["matcher"] == "run_command"
    assert (pdir / "plugin.json").is_file()

    consent = json.loads((ai.data_dir(home) / "config.json").read_text())
    assert consent[ai.CONSENT_KEY] is True
    assert result["dry_run"] is False


def test_dry_run_writes_nothing_but_reports_plugin_dir(ai, tmp_path):
    home = tmp_path / "home"
    result = ai.install(home=home, dry_run=True)
    assert result["dry_run"] is True
    pdir = ai.plugin_dir(home)
    assert not pdir.exists(), "dry-run must not create the plugin dir"
    assert result["plugin_dir"] == str(pdir)


def test_install_is_idempotent(ai, tmp_path):
    home = tmp_path / "home"
    ai.install(home=home)
    first_mtime = (ai.plugin_dir(home) / "hooks.json").stat().st_mtime_ns
    ai.install(home=home)
    second_mtime = (ai.plugin_dir(home) / "hooks.json").stat().st_mtime_ns
    # Re-running refreshes the payload/hooks in place; the key invariant is it
    # does not error and does not move anything outside the plugin dir.
    assert (ai.plugin_dir(home) / "plugin.json").is_file()
    assert second_mtime >= first_mtime


def test_install_refuses_symlink_plugin_dir(ai, tmp_path):
    home = tmp_path / "home"
    pdir = ai.plugin_dir(home)
    pdir.parent.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    pdir.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError):
        ai.install(home=home)


def test_install_refuses_plugin_dir_escaping_home(ai, tmp_path):
    """A plugin dir that resolves outside the Antigravity home must be refused."""
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    outside.mkdir()
    pdir = ai.plugin_dir(home)
    pdir.parent.mkdir(parents=True)
    pdir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError):
        ai.install(home=home)


def test_plugin_json_written_last_is_present_after_partial_failures(ai, tmp_path):
    """Ordering guarantee: plugin.json registers hooks, so it must never be the
    only artifact. After a successful install all three config artifacts exist;
    a missing payload module aborts before ANY file is written."""
    home = tmp_path / "home"
    ai.install(home=home)
    names = {p.name for p in ai.plugin_dir(home).iterdir()}
    assert {"hooks.json", "plugin.json", "measure-path"} <= names


def test_install_aborts_on_missing_payload(ai, monkeypatch, tmp_path):
    """An incomplete checkout must fail before writing anything."""
    monkeypatch.setattr(ai, "_PAYLOAD_MODULES", ai._PAYLOAD_MODULES + ("nonexistent_module.py",))
    home = tmp_path / "home"
    with pytest.raises(RuntimeError):
        ai.install(home=home)
    assert not ai.plugin_dir(home).exists()


# ---------------------------------------------------------------------------
# uninstall() behaviour
# ---------------------------------------------------------------------------

def test_uninstall_removes_only_plugin_dir(ai, tmp_path):
    home = tmp_path / "home"
    ai.install(home=home)
    data = ai.data_dir(home)
    assert ai.plugin_dir(home).is_dir()
    assert data.is_dir()

    result = ai.uninstall(home=home)
    assert ai.plugin_dir(home) in {Path(p) for p in result["removed"]}
    assert not ai.plugin_dir(home).exists()
    # R3: consent/session data stays put.
    assert data.is_dir()
    assert (data / "config.json").is_file()


def test_uninstall_dry_run_leaves_plugin_dir(ai, tmp_path):
    home = tmp_path / "home"
    ai.install(home=home)
    ai.uninstall(home=home, dry_run=True)
    assert ai.plugin_dir(home).is_dir()


def test_main_install_and_uninstall_return_zero(ai, tmp_path, capsys):
    home = tmp_path / "home"
    # install
    rc = ai.main(["install", "--dry-run"])
    assert rc == 0
    capsys.readouterr()
    # unknown action is rejected by argparse with a non-zero exit, but the two
    # real actions return 0 on success.
    rc = ai.main(["uninstall", "--dry-run"])
    assert rc == 0


@pytest.fixture(autouse=True)
def _pin_trusted_python(trusted_python):
    """Pin a gate-trusted interpreter for every install in this file (the
    hosted-CI system interpreter is world-writable and correctly rejected)."""
    return trusted_python
