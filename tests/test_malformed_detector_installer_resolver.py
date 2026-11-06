"""Installer/healer self-conflict: the malformed-hook detector must not
remove hooks that the installer itself wrote.

ROOT CAUSE: setup_all_hooks writes hook commands whose bash wrapper carries
the version-resolver subshell (D="$(ls -d ... | sort -V | tail -n1)").
_is_malformed_to_hook flagged ANY command that names a TO script and
contains "$(", so on every session the healer removed up to 9
installer-written entries, reprovisioned them via setup_all_hooks, reset
last_hook_heal_check, and logged a false "Removed N malformed hook(s)"
diagnostic - settings.json churn on every session, forever.

_hook_command_identity already treats that one resolver subshell as
plumbing (not identity). The malformed predicate must exempt the same
recognized plumbing while still rejecting every other subshell or
backtick shape (Claude-written interactive hooks, the documented target
of the detector).

Run: python3 -m pytest tests/test_malformed_detector_installer_resolver.py -v
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def m(monkeypatch):
    """Load measure.py fresh with a tmp snapshot dir."""
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tempfile.mkdtemp(prefix="to-md-"))
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    import measure
    import _measure.lifecycle as _lc
    measure._lc = _lc
    return measure


@pytest.fixture()
def installed(m, monkeypatch, tmp_path):
    """A real script-install: setup_all_hooks writes settings.json."""
    claude_dir = tmp_path / "claude"
    plugin_dir = tmp_path / "plugin"
    settings_path = claude_dir / "settings.json"
    claude_dir.mkdir(parents=True)
    settings_path.write_text(json.dumps({"_test_marker": True}), encoding="utf-8")
    # Source the hook list from the repo's real hooks.json so the test
    # exercises the exact commands the installer writes.
    hooks_json = REPO / "hooks" / "hooks.json"

    monkeypatch.setattr(m, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(m, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(m, "_is_plugin_installed", lambda: False)
    monkeypatch.setattr(m, "_running_under_hook", lambda: False)
    monkeypatch.setattr(m, "_find_plugin_hooks_json", lambda: hooks_json)
    monkeypatch.setattr(
        m, "_read_config_flag",
        lambda key, default=0: 0 if key == "last_hook_heal_check" else default,
    )
    result = m.setup_all_hooks(dry_run=False, verbose=False)
    assert result["added"] > 0, "install must register hooks"
    return settings_path


def _all_commands(settings_path: Path):
    data = json.loads(settings_path.read_text())
    for groups in data.get("hooks", {}).values():
        for group in groups:
            for hook in group.get("hooks", []):
                yield hook.get("command", "")


def test_installer_written_hooks_are_not_flagged_malformed(m, installed):
    """Every command setup_all_hooks writes must survive the malformed
    detector. Before the fix, 9 of 16 were flagged and removed each session."""
    commands = list(_all_commands(installed))
    assert commands, "install wrote no hook commands"
    flagged = [c for c in commands if m._is_malformed_to_hook(c)]
    assert flagged == [], (
        f"{len(flagged)} installer-written hook command(s) flagged malformed: "
        + "; ".join(c[:80] for c in flagged)
    )


def test_heal_cycle_is_stable_after_install(m, installed):
    """The full install -> heal loop must converge: zero removals, and the
    settings file untouched, on the first and every subsequent heal pass."""
    before = installed.read_bytes()
    first = m._fix_malformed_hook_commands()
    assert first == 0, f"healer removed {first} installer-written hook(s)"
    assert installed.read_bytes() == before, "settings.json churned by heal pass"
    second = m._fix_malformed_hook_commands()
    assert second == 0
    assert installed.read_bytes() == before


@pytest.mark.parametrize("evil", [
    # Claude-written interactive subshell shapes (the detector's documented target).
    "python3 $(which python3) ~/.claude/skills/token-optimizer/scripts/read_cache.py --quiet",
    "python3 `which python3` ~/.claude/skills/token-optimizer/scripts/measure.py ensure-health",
    "$(cat /tmp/x)/measure.py ensure-health",
    "python3 ~/.claude/skills/token-optimizer/scripts/bash_hook.py $(echo --quiet)",
])
def test_claude_written_subshell_shapes_still_flagged(m, evil):
    assert m._is_malformed_to_hook(evil) is True


def test_resolver_with_injected_second_subshell_still_flagged(m, installed):
    """Exempting the known resolver must not become a blanket subshell
    exemption: an installer-shaped command carrying any second subshell is
    still malformed."""
    base = next(c for c in _all_commands(installed) if 'D="$(ls -d ' in c)
    poisoned = base.replace("; done; exit 0", "; done; $(touch /tmp/pwn); exit 0")
    assert poisoned != base
    assert m._is_malformed_to_hook(poisoned) is True


def test_healer_removes_only_claude_written_entries(m, installed):
    """End to end: a settings file with installer hooks plus one
    Claude-written subshell hook heals to exactly the installer set."""
    data = json.loads(installed.read_text())
    data["hooks"].setdefault("PreToolUse", []).append({
        "matcher": "Read",
        "hooks": [{"type": "command",
                   "command": "python3 $(which python3) /x/scripts/read_cache.py --quiet"}],
    })
    installed.write_text(json.dumps(data), encoding="utf-8")
    removed = m._fix_malformed_hook_commands()
    assert removed == 1
    remaining = list(_all_commands(installed))
    assert not any("$(which python3)" in c for c in remaining)
    assert not any(m._is_malformed_to_hook(c) for c in remaining)


def test_double_home_still_flagged_on_darwin(m, monkeypatch):
    monkeypatch.setattr(m.platform, "system", lambda: "Darwin")
    evil = "python3 $HOME/Users/u/.claude/skills/token-optimizer/scripts/measure.py ensure-health"
    assert m._is_malformed_to_hook(evil) is True


def test_unrelated_hooks_never_touched(m, installed):
    """Hooks from other plugins/tools stay outside the detector's reach,
    subshells or not."""
    foreign = "python3 $(which python3) /x/other-plugin/do_thing.py --flag"
    assert m._is_malformed_to_hook(foreign) is False


def _hooks_json_resolver():
    """Extract the resolver template from hooks/hooks.json (the source the
    installer writes into settings.json, JSON-decoded)."""
    hooks_json = json.loads((REPO / "hooks" / "hooks.json").read_text())
    seen = set()
    for groups in hooks_json["hooks"].values():
        for group in groups:
            for hook in group.get("hooks", []):
                cmd = hook.get("command", "")
                if 'D="$(ls -d ' in cmd:
                    start = cmd.index('D="$(ls -d ')
                    end = cmd.index(')"', start) + 2
                    seen.add(cmd[start:end])
    assert len(seen) == 1, f"expected one resolver shape, got {seen}"
    return seen.pop()


RESOLVER = _hooks_json_resolver()


def test_template_constant_matches_hooks_json(m):
    """The exemption template must stay byte-identical to what the installer
    writes; if hooks.json changes shape, this test forces a conscious
    re-decision instead of silent drift."""
    assert m._lc._TO_VERSION_RESOLVER == RESOLVER, (
        "hooks.json resolver shape differs from the exempted template: "
        f"{RESOLVER!r} vs {m._lc._TO_VERSION_RESOLVER!r}"
    )


# Adversarial evasion shapes: each LOOKS resolver-flavored but deviates from
# the exact template, so every one must still be rejected. These guard the
# predicate's role as a rejection gate, not a pattern approximation.
_READ_CMD_TAIL = '"$D/hooks/run.py" skills/token-optimizer/scripts/read_cache.py --quiet'


@pytest.mark.parametrize("evil", [
    # command substitution with backtick inside a resolver-shaped wrapper
    'D="$(ls -d `whoami`)"; ' + _READ_CMD_TAIL,
    # extra command chained inside the substitution
    'D="$(ls -d /tmp; id)"; ' + _READ_CMD_TAIL,
    # newline inside the substitution
    'D="$(ls -d /tmp\nid)"; ' + _READ_CMD_TAIL,
    # near-miss template: one character different from the real resolver
    RESOLVER.replace("tail -n1", "tail -n2") + "; " + _READ_CMD_TAIL,
    # real template followed by a second resolver-shaped subshell
    RESOLVER + "; " + RESOLVER + "; " + _READ_CMD_TAIL,
    # real template plus an appended substitution after it
    RESOLVER + "; X=$(id); " + _READ_CMD_TAIL,
])
def test_evasion_shapes_still_flagged(m, evil):
    assert m._is_malformed_to_hook(evil) is True


def test_exact_template_single_occurrence_passes(m):
    """The true resolver on its own, exactly as installed, stays clean."""
    cmd = 'for b in bash; do D="/x"; [ -r "$D/hooks/python-launcher.sh" ] || ' + RESOLVER + '; exec "$b" ' + _READ_CMD_TAIL + "; done; exit 0"
    assert m._is_malformed_to_hook(cmd) is False
