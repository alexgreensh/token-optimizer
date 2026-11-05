#!/usr/bin/env python3
"""Keep-warm launchd plist stable-path self-heal.

An agent installed by an older build embeds a version-pinned
``/plugins/cache/.../<VERSION>/measure.py`` path that dies on the next plugin
update; launchd then fires an old (or missing) script every ~5min and the
dashboard's savings freeze. `_heal_keepwarm_plist_path` lifts an existing plist
onto the update-surviving marketplace clone path at ensure-health.

POSIX-only: the plist lives under ~/Library/LaunchAgents and the reload uses
launchctl + os.getuid, none of which Windows represents.

Run: python3 -m pytest tests/test_keepwarm_plist_heal.py -q
"""

from __future__ import annotations
from helpers.measure_split import exec_source_module as _split_exec_module

import importlib.util
import os
import plistlib
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX launchd plist semantics (launchctl/os.getuid/LaunchAgents)",
)

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


def _load_measure(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    _split_exec_module(mod)
    return mod


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """measure.py with the heal wired to a synthetic plist + stable path, and
    launchctl neutralized (no real bootout/bootstrap during tests)."""
    mod = _load_measure(monkeypatch)
    # The sandbox HOME reads as a redirected home, which refuses scheduler
    # writes; launchctl is neutralized here, so opt out explicitly.
    monkeypatch.setattr(mod, "_HOME_REDIRECTED", False)

    stable = tmp_path / "marketplaces" / "stable" / "measure.py"
    stable.parent.mkdir(parents=True)
    stable.write_text("# stable clone\n")
    plist = tmp_path / "com.token-optimizer.keepwarm.plist"

    monkeypatch.setattr(mod, "_is_running_from_plugin_cache", lambda: True)
    monkeypatch.setattr(mod, "_stable_marketplace_script_path", lambda name: str(stable))
    monkeypatch.setattr(mod, "_keepwarm_scheduler_plist_path", lambda: plist)

    calls = []

    class _FakeSub:
        TimeoutExpired = mod.subprocess.TimeoutExpired

        @staticmethod
        def run(argv, *a, **k):
            calls.append(argv)
            return None

    monkeypatch.setattr(mod, "subprocess", _FakeSub)

    def _write_plist(script_path):
        data = {
            "Label": "com.token-optimizer.keepwarm",
            "ProgramArguments": ["/usr/bin/python3", script_path, "keepwarm-tick", "--quiet"],
        }
        with open(plist, "wb") as f:
            plistlib.dump(data, f)

    def _script_arg():
        data = plistlib.load(open(plist, "rb"))
        return [a for a in data["ProgramArguments"] if a.endswith("measure.py")][0]

    yield mod, plist, str(stable), _write_plist, _script_arg, calls
    sys.modules.pop("measure", None)


def test_rewrites_stale_path(wired):
    mod, plist, stable, write_plist, script_arg, calls = wired
    write_plist("/Users/x/OLD/cache/5.11.0/measure.py")
    assert mod._heal_keepwarm_plist_path() is True
    assert script_arg() == stable
    # reloaded so the corrected path takes effect immediately
    assert any("bootout" in c for c in calls)
    assert any("bootstrap" in c for c in calls)


def test_idempotent_when_already_stable(wired):
    mod, plist, stable, write_plist, script_arg, calls = wired
    write_plist(stable)
    assert mod._heal_keepwarm_plist_path() is False
    assert not calls  # no reload when nothing changed


def test_noop_when_plist_absent(wired):
    mod, plist, stable, write_plist, script_arg, calls = wired
    assert not plist.exists()
    assert mod._heal_keepwarm_plist_path() is False


def test_noop_when_not_cache_install(wired, monkeypatch):
    mod, plist, stable, write_plist, script_arg, calls = wired
    write_plist("/Users/x/OLD/cache/5.11.0/measure.py")
    monkeypatch.setattr(mod, "_is_running_from_plugin_cache", lambda: False)
    assert mod._heal_keepwarm_plist_path() is False
    assert script_arg().endswith("5.11.0/measure.py")  # untouched


def test_noop_when_no_stable_path(wired, monkeypatch):
    mod, plist, stable, write_plist, script_arg, calls = wired
    write_plist("/Users/x/OLD/cache/5.11.0/measure.py")
    monkeypatch.setattr(mod, "_stable_marketplace_script_path", lambda name: None)
    assert mod._heal_keepwarm_plist_path() is False


def test_atomic_write_leaves_no_tmp(wired):
    mod, plist, stable, write_plist, script_arg, calls = wired
    write_plist("/Users/x/OLD/cache/5.11.0/measure.py")
    mod._heal_keepwarm_plist_path()
    leftovers = list(plist.parent.glob(".keepwarm-plist.*"))
    assert leftovers == []
