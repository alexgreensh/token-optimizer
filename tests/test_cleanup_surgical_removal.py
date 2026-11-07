#!/usr/bin/env python3
"""Remove ONLY what we own.

P2-5 foreign marketplace claimed by a bare "token-optimizer" substring and
      deleted from the user's known_marketplaces.json.
P2-6 user-authored hook groups with no hook of ours were deleted (and
      miscounted as our quality-cache hook) whenever filtering left them empty.
P2-9 our OWN smart-compact family (PreCompact / SessionStart / Stop) was left
      dangling in settings.json -- commands pointing into a tree cleanup is
      about to delete.

Run: python3 -m pytest tests/test_cleanup_surgical_removal.py -q
"""

from __future__ import annotations
from helpers.measure_split import exec_source_module as _split_exec_module

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
# Our hooks run an install's measure.py (here: this checkout's). A command
# running a measure.py outside every install is the user's own tool.
MP = str(SCRIPTS / "measure.py")


@pytest.fixture()
def measure(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    _split_exec_module(mod)
    yield mod
    sys.modules.pop("measure", None)


@pytest.fixture()
def reconcile(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("install_reconcile", None)
    spec = importlib.util.spec_from_file_location(
        "install_reconcile", SCRIPTS / "install_reconcile.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["install_reconcile"] = mod
    _split_exec_module(mod)
    yield mod
    sys.modules.pop("install_reconcile", None)


# ---------------------------------------------------------------------------
# P2-6: user hook groups are never collateral
# ---------------------------------------------------------------------------

def test_empty_user_group_is_not_deleted_or_miscounted(measure):
    settings = {"hooks": {"UserPromptSubmit": [
        {"matcher": "mine", "hooks": []},                      # user's, empty
        {"hooks": [{"command": f"python3 '{MP}' quality-cache --quiet"}]},  # ours
    ]}}

    removed = measure._remove_our_settings_entries(settings)

    groups = settings["hooks"]["UserPromptSubmit"]
    assert len(groups) == 1 and groups[0].get("matcher") == "mine", (
        "the user's empty group was deleted as collateral"
    )
    assert removed == ["1 UserPromptSubmit hook(s)"], removed


def test_foreign_group_in_a_shared_event_survives(measure):
    settings = {"hooks": {"SessionStart": [
        {"hooks": [{"command": "node /home/u/my-own-tool.js"}]},
        {"hooks": [{"command": f"python3 '{MP}' quality-cache --quiet"}]},
    ]}}

    measure._remove_our_settings_entries(settings)

    remaining = settings["hooks"]["SessionStart"]
    assert len(remaining) == 1
    assert remaining[0]["hooks"][0]["command"] == "node /home/u/my-own-tool.js"


def test_user_hook_merely_mentioning_us_is_kept(measure):
    """A user script whose path contains our name is still theirs."""
    settings = {"hooks": {"Stop": [
        {"hooks": [{"command": "bash /home/u/notes/token-optimizer-reminder.sh"}]},
    ]}}

    removed = measure._remove_our_settings_entries(settings)

    assert removed == []
    assert len(settings["hooks"]["Stop"]) == 1


def test_hook_entry_ownership_predicate(measure):
    ours = [
        {"command": f"python3 '{MP}' compact-capture --trigger auto"},
        {"command": f"python3 '{MP}' quality-cache --quiet"},
    ]
    theirs = [  # naming our subcommands or scripts proves nothing
        {"command": f"python3 {MP} quality-cache --force"},
        {"command": "bash launcher.sh read_cache.py --clear-compacted --quiet"},
        {"command": "curl -fsS http://localhost:8080/ensure-health"},
        {"command": "node /home/u/statusline.js"},          # their own statusline
        {"command": "echo token-optimizer is great"},        # mentions us only
        {"command": ""},
        {},
        None,
    ]
    for e in ours:
        assert measure._is_our_hook_entry(e) is True, e
    for e in theirs:
        assert measure._is_our_hook_entry(e) is False, e


# ---------------------------------------------------------------------------
# P2-9: our own smart-compact family is removed too
# ---------------------------------------------------------------------------

def test_smart_compact_family_is_removed(measure):
    mp = MP
    settings = {"hooks": {
        "PreCompact": [{"hooks": [{"command": f"python3 '{mp}' compact-capture --trigger auto"}]}],
        "SessionStart": [{"hooks": [{"command": f"python3 '{mp}' compact-restore"}]}],
        "Stop": [{"hooks": [{"command": f"python3 '{mp}' compact-capture --trigger stop"}]}],
        "UserPromptSubmit": [{"hooks": [{"command": f"python3 '{mp}' quality-cache --quiet"}]}],
    }}

    measure._remove_our_settings_entries(settings)

    assert settings["hooks"] == {}, (
        f"our own hooks were left dangling: {settings['hooks']}"
    )


def test_unrelated_top_level_keys_untouched(measure):
    settings = {
        "model": "opus", "theme": "dark", "env": {"K": "V"},
        "statusLine": {"type": "command", "command": "node '/p/token-optimizer/statusline.js'"},
        "hooks": {"PreToolUse": [{"hooks": [{"command": "python3 /home/u/guard.py"}]}]},
    }

    measure._remove_our_settings_entries(settings)

    assert settings["model"] == "opus" and settings["theme"] == "dark"
    assert settings["env"] == {"K": "V"}
    assert "statusLine" not in settings
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "python3 /home/u/guard.py"


# ---------------------------------------------------------------------------
# P2-5: marketplace ownership needs evidence
# ---------------------------------------------------------------------------

def test_foreign_marketplace_is_not_claimed_by_substring(reconcile):
    """A user's own registry whose NAME contains our string is not ours."""
    entry = {"installLocation": "/home/u/dev/my-token-optimizer-tweaks"}
    assert reconcile._is_our_marketplace(
        "my-token-optimizer-tweaks", entry, our_marketplaces=set()) is False


def test_marketplace_is_ours_when_the_registry_says_we_installed_from_it(reconcile):
    ours = {"alexgreensh-token-optimizer"}
    assert reconcile._is_our_marketplace(
        "alexgreensh-token-optimizer", {}, our_marketplaces=ours) is True


def test_marketplace_names_derived_from_our_plugin_keys(reconcile):
    registry = {"plugins": {
        "token-optimizer@alexgreensh-token-optimizer": [{"installPath": "/x"}],
        "other-plugin@someone-else": [{"installPath": "/y"}],
    }}
    assert reconcile._our_marketplace_names(registry) == {"alexgreensh-token-optimizer"}


def test_exact_plugin_name_path_segment_still_counts(reconcile):
    entry = {"installLocation": "/home/u/.claude/plugins/marketplaces/token-optimizer"}
    assert reconcile._is_our_marketplace("whatever", entry, our_marketplaces=set()) is True


def test_lookalike_path_segment_is_rejected(reconcile):
    entry = {"installLocation": "/home/u/dev/token-optimizer-fork/marketplace"}
    assert reconcile._is_our_marketplace(
        "token-optimizer-fork", entry, our_marketplaces=set()) is False
