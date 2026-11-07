#!/usr/bin/env python3
"""Cleanup command orchestrator test (uninstall + cleanup).

The ``cleanup`` command orchestrates three uninstall surfaces:
1. Daemon (identity-sweeping, dry-run-aware).
2. Host config (settings.json): back up first, remove ONLY our entries.
3. Manifests (installed_plugins / known_marketplaces): back up first,
   remove our STALE entries.

Session data, checkpoints, and trends are PRESERVED and disclosed.
``--dry-run`` is side-effect-free and doubles as retained-paths disclosure.

Run directly:  python3 tests/test_cleanup_command.py
Exits non-zero on first failure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _allow_scheduler_code_under_sandbox_home(monkeypatch):
    """The suite runs with a sandbox HOME, which measure treats as a redirected
    home and refuses every per-account scheduler write for (_HOME_REDIRECTED).
    This file drives that installer/restart code with the service-manager
    subprocess layer stubbed, so it opts out of the refusal explicitly."""
    monkeypatch.setattr(measure, "_HOME_REDIRECTED", False)
import plugin_env  # noqa: E402
import install_reconcile  # noqa: E402


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _no_op_run(*a, **k):
    return _Completed(returncode=1, stdout="", stderr="not found")


def _setup_env(tmp_path, monkeypatch):
    """Set up a temp home with two identities, daemon artifacts, settings, manifests."""
    home = tmp_path / "home"
    plugins_dir = home / "plugins"
    data_base = plugins_dir / "data"
    cache = plugins_dir / "cache"

    ident_a = data_base / "token-optimizer-marketplace"
    ident_b = data_base / "token-optimizer-inline"
    snap_a = ident_a / "data"
    snap_b = ident_b / "data"
    for snap in (snap_a, snap_b):
        snap.mkdir(parents=True)
        (snap / "dashboard-server.py").write_text("# daemon\n", encoding="utf-8")
        (snap / "daemon-token").write_text("csrf-secret", encoding="utf-8")
        (snap / "dashboard-host").write_text("127.0.0.1", encoding="utf-8")
        # Retained paths: session data + checkpoints + trends
        (snap / "snap_test.json").write_text("{}", encoding="utf-8")
        (snap / "checkpoints").mkdir()
        (snap / "checkpoints" / "cp1.json").write_text("{}", encoding="utf-8")
        (snap / "trends").mkdir()
        (snap / "trends" / "t1.json").write_text("{}", encoding="utf-8")

    # plugin_env enumeration -> temp base
    monkeypatch.setattr(plugin_env, "_PLUGIN_DATA_BASE", data_base)
    monkeypatch.setattr(plugin_env, "_INSTALLED_PLUGINS", plugins_dir / "installed_plugins.json")
    monkeypatch.setattr(plugin_env, "_PLUGIN_DATA_ENV_VARS", ("CLAUDE_PLUGIN_DATA",))
    plugin_env.resolve_plugin_data_dir.cache_clear()
    monkeypatch.setattr(measure, "_all_plugin_data_dirs", plugin_env._all_plugin_data_dirs)

    # Resolved identity = ident_a
    monkeypatch.setattr(measure, "SNAPSHOT_DIR", snap_a)
    monkeypatch.setattr(measure, "DAEMON_TOKEN_PATH", snap_a / "daemon-token")
    monkeypatch.setattr(measure, "DAEMON_HOST_PATH", snap_a / "dashboard-host")
    monkeypatch.setattr(measure, "DAEMON_THRASH_BREADCRUMB", snap_a / ".daemon-thrash")
    monkeypatch.setattr(measure, "DAEMON_LOG_DIR", snap_a / "logs")
    monkeypatch.setattr(measure, "CLAUDE_DIR", home)
    monkeypatch.setattr(measure, "SETTINGS_PATH", home / "settings.json")
    monkeypatch.setattr(measure, "_SETTINGS_LOCK_PATH", home / ".settings.lock")

    # LaunchAgents -> temp
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    monkeypatch.setattr(measure, "LAUNCH_AGENTS_DIR", launch_agents)
    monkeypatch.setattr(measure, "PLIST_PATH", launch_agents / "com.token-optimizer.dashboard.plist")
    measure.PLIST_PATH.write_text("<plist/>", encoding="utf-8")

    # No real launchctl/schtasks/systemctl
    monkeypatch.setattr(measure.subprocess, "run", _no_op_run)

    # settings.json with our statusLine + a foreign hook
    settings = {
        "statusLine": {"command": f"node '{SCRIPTS / 'statusline.js'}'"},
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": f"python3 '{SCRIPTS / 'measure.py'}' quality-cache --quiet"}]},
                {"hooks": [{"type": "command", "command": "echo foreign-hook"}]},
            ],
            "SessionEnd": [
                {"hooks": [{"type": "command", "command": f"python3 {SCRIPTS / 'measure.py'} collect --quiet"}]},
            ],
        },
        "theme": "dark",
    }
    measure.SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    # Manifests: our stale entry + a foreign active entry
    our_install = cache / "alexgreensh-token-optimizer" / "token-optimizer" / "5.11.67"  # gone
    foreign_install = cache / "claude-plugins-official" / "ralph-loop" / "1.0.0"
    foreign_install.mkdir(parents=True)
    (plugins_dir / "installed_plugins.json").write_text(json.dumps({
        "version": 2,
        "plugins": {
            "token-optimizer@alexgreensh-token-optimizer": [
                {"installPath": str(our_install), "version": "5.11.67"}
            ],
            "ralph-loop@claude-plugins-official": [
                {"installPath": str(foreign_install), "version": "1.0.0"}
            ],
        },
    }, indent=2), encoding="utf-8")
    our_mkt_loc = plugins_dir / "marketplaces" / "alexgreensh-token-optimizer"  # gone
    foreign_mkt_loc = plugins_dir / "marketplaces" / "claude-plugins-official"
    foreign_mkt_loc.mkdir(parents=True)
    (plugins_dir / "known_marketplaces.json").write_text(json.dumps({
        "alexgreensh-token-optimizer": {"installLocation": str(our_mkt_loc)},
        "claude-plugins-official": {"installLocation": str(foreign_mkt_loc)},
    }, indent=2), encoding="utf-8")

    return home, snap_a, snap_b


def test_dry_run_is_side_effect_free(tmp_path, monkeypatch, capsys):
    home, snap_a, snap_b = _setup_env(tmp_path, monkeypatch)
    before_settings = measure.SETTINGS_PATH.read_text(encoding="utf-8")
    before_inst = (home / "plugins" / "installed_plugins.json").read_text(encoding="utf-8")
    before_mkt = (home / "plugins" / "known_marketplaces.json").read_text(encoding="utf-8")

    measure.cleanup(dry_run=True, this_install_only=False)
    out = capsys.readouterr().out

    # Nothing modified.
    assert measure.SETTINGS_PATH.read_text(encoding="utf-8") == before_settings
    assert (home / "plugins" / "installed_plugins.json").read_text(encoding="utf-8") == before_inst
    assert (home / "plugins" / "known_marketplaces.json").read_text(encoding="utf-8") == before_mkt
    # Daemon files survive.
    assert (snap_a / "dashboard-server.py").exists()
    assert (snap_b / "dashboard-server.py").exists()
    assert measure.PLIST_PATH.exists()
    # Retained paths disclosed.
    assert "Retained by design" in out
    assert "session data" in out
    # Would-remove language, not "Removed".
    assert "Would remove" in out or "Would delete" in out


def test_real_cleanup_removes_our_entries_preserves_foreign_and_retained(tmp_path, monkeypatch, capsys):
    home, snap_a, snap_b = _setup_env(tmp_path, monkeypatch)
    measure.cleanup(dry_run=False, this_install_only=False)
    out = capsys.readouterr().out

    # Daemon files gone from both identities.
    assert not (snap_a / "dashboard-server.py").exists()
    assert not (snap_b / "dashboard-server.py").exists()
    assert not (snap_a / "daemon-token").exists()
    assert not (snap_b / "daemon-token").exists()
    assert not measure.PLIST_PATH.exists()

    # settings.json: our statusLine + quality-cache hook + SessionEnd hook gone;
    # foreign hook + theme survive.
    settings = json.loads(measure.SETTINGS_PATH.read_text(encoding="utf-8"))
    assert "statusLine" not in settings
    assert "theme" in settings
    hooks = settings.get("hooks", {})
    # Foreign UserPromptSubmit hook survives.
    ups = hooks.get("UserPromptSubmit", [])
    foreign_survives = any(
        "echo foreign-hook" in (h.get("command", ""))
        for g in ups for h in g.get("hooks", [])
    )
    assert foreign_survives, "foreign UserPromptSubmit hook was removed"
    # Our quality-cache hook gone.
    our_cache_gone = not any(
        "quality-cache" in (h.get("command", ""))
        for g in ups for h in g.get("hooks", [])
    )
    assert our_cache_gone, "quality-cache hook survived cleanup"
    # SessionEnd hooks gone (was only ours).
    assert "SessionEnd" not in hooks

    # Manifests: our stale entries gone, foreign survives.
    inst = json.loads((home / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
    assert "ralph-loop@claude-plugins-official" in inst["plugins"]
    assert "token-optimizer@alexgreensh-token-optimizer" not in inst["plugins"]
    mkt = json.loads((home / "plugins" / "known_marketplaces.json").read_text(encoding="utf-8"))
    assert "claude-plugins-official" in mkt
    assert "alexgreensh-token-optimizer" not in mkt

    # Backups created.
    assert "Backup:" in out
    backup_dirs = list((home / "_backups" / "token-optimizer").iterdir())
    assert len(backup_dirs) >= 1
    # settings.json backup exists.
    assert any((b / "settings.json").exists() for b in backup_dirs)
    # Manifest backups exist.
    assert any((b / "installed_plugins.json").exists() for b in backup_dirs)
    assert any((b / "known_marketplaces.json").exists() for b in backup_dirs)

    # Retained paths survive.
    assert (snap_a / "snap_test.json").exists()
    assert (snap_a / "checkpoints" / "cp1.json").exists()
    assert (snap_a / "trends" / "t1.json").exists()
    assert (snap_b / "snap_test.json").exists()
    assert (snap_b / "checkpoints" / "cp1.json").exists()
    assert (snap_b / "trends" / "t1.json").exists()

    # Retained paths disclosed in output.
    assert "Retained by design" in out


def test_this_install_only_preserves_sibling_daemon(tmp_path, monkeypatch, capsys):
    home, snap_a, snap_b = _setup_env(tmp_path, monkeypatch)
    measure.cleanup(dry_run=False, this_install_only=True)
    # Resolved identity daemon gone.
    assert not (snap_a / "dashboard-server.py").exists()
    # Sibling daemon survives.
    assert (snap_b / "dashboard-server.py").exists(), "sibling daemon removed under --this-install-only"
    # But settings + manifests are still cleaned (those are host-level, not identity-scoped).
    settings = json.loads(measure.SETTINGS_PATH.read_text(encoding="utf-8"))
    assert "statusLine" not in settings


if __name__ == "__main__":
    import tempfile

    class _Monkey:
        def __init__(self):
            self._saved = {}

        def setattr(self, target, name, value):
            if target not in self._saved:
                self._saved[target] = {}
            self._saved[target][name] = getattr(target, name)
            setattr(target, name, value)

        def setenv(self, k, v):
            import os
            self._saved.setdefault(os, {})
            self._saved[os][k] = os.environ.get(k)
            os.environ[k] = v

        def undo(self):
            import os
            for target, attrs in self._saved.items():
                if target is os:
                    for k, v in attrs.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v
                else:
                    for name, val in attrs.items():
                        setattr(target, name, val)

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        mp = _Monkey()
        try:
            with tempfile.TemporaryDirectory() as td:
                import io, contextlib
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    t(Path(td), mp)
                t._last_out = buf.getvalue()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
        finally:
            mp.undo()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
