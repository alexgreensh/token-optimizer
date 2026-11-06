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

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
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
    f"python3 $(which python3) {REPO}/skills/token-optimizer/scripts/read_cache.py --quiet",
    f"python3 `which python3` {REPO}/skills/token-optimizer/scripts/measure.py ensure-health",
    f"python3 {REPO}/skills/token-optimizer/scripts/bash_hook.py $(echo --quiet)",
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
                   "command": f"python3 $(which python3) {REPO}/skills/token-optimizer/scripts/read_cache.py --quiet"}],
    })
    installed.write_text(json.dumps(data), encoding="utf-8")
    removed = m._fix_malformed_hook_commands()
    assert removed == 1
    remaining = list(_all_commands(installed))
    assert not any("$(which python3)" in c for c in remaining)
    assert not any(m._is_malformed_to_hook(c) for c in remaining)


def test_double_home_still_flagged_on_darwin(m, monkeypatch, tmp_path):
    monkeypatch.setattr(m.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    evil = f"python3 $HOME/Users/u{REPO}/skills/token-optimizer/scripts/measure.py ensure-health"
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
_READ_CMD_TAIL = f'D="{REPO}"; [ -r "$D/hooks/python-launcher.sh" ] || '


@pytest.mark.parametrize("evil", [
    # command substitution with backtick inside a resolver-shaped wrapper
    _READ_CMD_TAIL + 'D="$(ls -d `whoami`)"; exec "$b" "$D/hooks/run.py" skills/token-optimizer/scripts/read_cache.py --quiet',
    # extra command chained inside the substitution
    _READ_CMD_TAIL + 'D="$(ls -d /tmp; id)"; exec "$b" "$D/hooks/run.py" skills/token-optimizer/scripts/read_cache.py --quiet',
    # newline inside the substitution
    _READ_CMD_TAIL + 'D="$(ls -d /tmp\nid)"; exec "$b" "$D/hooks/run.py" skills/token-optimizer/scripts/read_cache.py --quiet',
    # near-miss template: one character different from the real resolver
    _READ_CMD_TAIL + RESOLVER.replace("tail -n1", "tail -n2") + '; exec "$b" "$D/hooks/run.py" skills/token-optimizer/scripts/read_cache.py --quiet',
    # real template followed by a second resolver-shaped subshell
    _READ_CMD_TAIL + RESOLVER + "; " + RESOLVER + '; exec "$b" "$D/hooks/run.py" skills/token-optimizer/scripts/read_cache.py --quiet',
    # real template plus an appended substitution after it
    _READ_CMD_TAIL + RESOLVER + '; X=$(id); exec "$b" "$D/hooks/run.py" skills/token-optimizer/scripts/read_cache.py --quiet',
])
def test_evasion_shapes_still_flagged(m, evil):
    assert m._is_malformed_to_hook(evil) is True


def test_exact_template_single_occurrence_passes(m):
    """The true resolver on its own, exactly as installed, stays clean."""
    cmd = ('for b in bash; do ' + _READ_CMD_TAIL + RESOLVER + '; D="${D%/}"; exec "$b" "$D/hooks/run.py" skills/token-optimizer/scripts/read_cache.py --quiet; done; exit 0')
    assert m._is_malformed_to_hook(cmd) is False


# --- Ownership: positive proof required before any removal ----------------
# Regression for the basename-ownership bug: the detector used to treat any
# command naming one of our script BASENAMES as ours, so a user's own hook
# running some other vendor's measure.py (or read_cache.py, ...) with a
# subshell was permanently deleted from settings.json. Removal now requires
# the actual script path token (the path the hook will execute) to resolve
# to this install's payload: skills/token-optimizer/scripts/<name> under a
# runtime-proven root - the running install root (the healer's own __file__)
# or the resolved $CLAUDE_PLUGIN_ROOT env. The shared Claude dir is never a
# root: a same-named skill dir or plugin-cache entry under it, whatever its
# layout, is not provably ours and survives. A root string anywhere else in
# the command (argument, comment, redirect, env) proves nothing; unknown or
# ambiguous ownership is always preserved.


@pytest.mark.parametrize("foreign", [
    "python3 $(which python3) /home/u/vendor/measure.py --quiet",
    "python3 $(which python3) /opt/tools/read_cache.py --quiet",
    "python3 $(id -u > /dev/null && echo python3) /home/u/bin/bash_hook.py --quiet",
    "python3 `which python3` /home/u/lib/archive_result.py --out /tmp/o",
    "python3 $(which python3) /srv/ctx/context_intel.py --quiet",
])
def test_foreign_same_basename_hooks_survive(m, foreign):
    """A subshell hook naming our script's basename under a foreign path is
    not ours and must never be flagged for removal."""
    assert m._is_malformed_to_hook(foreign) is False


def test_unprovable_ownership_survives(m):
    """No path anchor at all: ownership cannot be proven, and deletion is
    irreversible for the user's own hook - fail safe, leave it alone."""
    assert m._is_malformed_to_hook("$(cat /tmp/x)/measure.py ensure-health") is False


def test_healer_preserves_foreign_measure_hook(m, installed):
    """The exact reported bug: settings.json holds one of our malformed
    hooks plus the user's own hook running vendor/measure.py with a
    subshell. The heal pass must remove ours and leave the foreign one
    untouched."""
    data = json.loads(installed.read_text())
    data["hooks"].setdefault("PreToolUse", []).extend([
        {"matcher": "Read", "hooks": [{"type": "command",
            "command": f"python3 $(which python3) {REPO}/skills/token-optimizer/scripts/read_cache.py --quiet"}]},
        {"matcher": "Bash", "hooks": [{"type": "command",
            "command": "python3 $(which python3) /home/u/vendor/measure.py --quiet"}]},
    ])
    installed.write_text(json.dumps(data), encoding="utf-8")
    removed = m._fix_malformed_hook_commands()
    assert removed == 1, "only the provably-ours malformed hook may be removed"
    remaining = [c for c in _all_commands(installed)]
    assert any("vendor/measure.py" in c for c in remaining), "foreign hook deleted"
    assert not any("$(which python3)" in c and "token-optimizer" in c for c in remaining)


# --- Ownership evasions: marker-shaped foreign commands must survive ------
# Review findings: a bare "token-optimizer" substring is not ownership proof.
# Each of these names our basename and carries the product string somewhere,
# but none references a path under this install's actual roots.


@pytest.mark.parametrize("foreign", [
    # foreign install whose directory happens to be named token-optimizer
    "python3 $(which python3) /home/u/vendor/token-optimizer/measure.py --quiet",
    # foreign install mimicking the canonical layout exactly
    "python3 $(which python3) /home/u/vendor/token-optimizer/scripts/measure.py --quiet",
    # product string in arguments, not in any install path
    "python3 $(which python3) /home/u/bin/measure.py --config token-optimizer",
    # product string in a trailing comment
    "python3 $(which python3) /home/u/bin/measure.py --quiet  # token-optimizer",
])
def test_ownership_evasions_survive(m, foreign):
    assert m._is_malformed_to_hook(foreign) is False


def test_healer_preserves_marker_shaped_foreign_hook(m, installed):
    """End to end: a foreign hook at vendor/token-optimizer/scripts survives
    the heal pass while our malformed hook is removed."""
    data = json.loads(installed.read_text())
    data["hooks"].setdefault("PreToolUse", []).extend([
        {"matcher": "Read", "hooks": [{"type": "command",
            "command": f"python3 $(which python3) {REPO}/skills/token-optimizer/scripts/read_cache.py --quiet"}]},
        {"matcher": "Bash", "hooks": [{"type": "command",
            "command": "python3 $(which python3) /home/u/vendor/token-optimizer/scripts/measure.py --quiet"}]},
    ])
    installed.write_text(json.dumps(data), encoding="utf-8")
    removed = m._fix_malformed_hook_commands()
    assert removed == 1
    remaining = [c for c in _all_commands(installed)]
    assert any("vendor/token-optimizer" in c for c in remaining), "lookalike foreign hook deleted"


def test_root_prefix_collision_against_proven_root_survives(m, monkeypatch, tmp_path):
    """A sibling dir sharing a string prefix with a PROVEN root
    (plugin-evil vs plugin) is not under that root and must survive.
    tmp_path-based: resolving a real repo-adjacent path trips the suite's
    real-home guard on CI, where the repo lives under the runner home."""
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "plugin"))
    evil = tmp_path / "plugin-evil"
    cmd = f"python3 $(which python3) {evil}/skills/token-optimizer/scripts/measure.py --quiet"
    assert m._is_malformed_to_hook(cmd) is False
    # positive control: the proven root itself is flagged
    own = f"python3 $(which python3) {tmp_path}/plugin/skills/token-optimizer/scripts/measure.py --quiet"
    assert m._is_malformed_to_hook(own) is True


def test_owned_paths_under_each_root_are_proven(m, installed, monkeypatch):
    """Positive controls: the same malformed shape is flagged only when the
    script token resolves under a runtime-proven root - the running install
    root or the resolved plugin-root env. The shared Claude dir is NOT a
    root: our own skill location under it is unprovable (and preserved)
    when the healer is not running from that install."""
    shape = "python3 $(which python3) {root}/skills/token-optimizer/scripts/read_cache.py --quiet"
    assert m._is_malformed_to_hook(shape.format(root=REPO)) is True
    assert m._is_malformed_to_hook(shape.format(root=installed.parent)) is False
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/srv/plugin-root")
    assert m._is_malformed_to_hook(shape.format(root="/srv/plugin-root")) is True


def test_owned_cache_via_real_plugin_root(m, installed, monkeypatch, tmp_path):
    """A plugin-cache path IS provable when the resolved plugin-root env (or
    the running install root) IS that cache version dir - both the expanded
    absolute spelling and the literal ${CLAUDE_PLUGIN_ROOT} form. A
    same-named plugin from another marketplace with an identical layout is
    foreign and survives (judge residual class)."""
    own_cache = tmp_path / "plugins" / "cache" / "mkt" / "token-optimizer" / "5.13.66"
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(own_cache))
    shape = "python3 $(id) {root}/skills/token-optimizer/scripts/measure.py ensure-health"
    assert m._is_malformed_to_hook(shape.format(root=own_cache)) is True
    literal = ("python3 $(id) ${CLAUDE_PLUGIN_ROOT}/skills/token-optimizer/"
               "scripts/measure.py ensure-health")
    assert m._is_malformed_to_hook(literal) is True
    foreign_same_name = m.CLAUDE_DIR / "plugins" / "cache" / "evil-mkt" / "token-optimizer" / "9.9.9"
    assert m._is_malformed_to_hook(shape.format(root=foreign_same_name)) is False
    # Even the same marketplace dir, a different plugin slot is foreign.
    other_slot = m.CLAUDE_DIR / "plugins" / "cache" / "mkt" / "token-optimizer" / "9.9.9"
    assert m._is_malformed_to_hook(shape.format(root=other_slot)) is False


def test_subshell_glued_path_is_unprovable_and_preserved(m):
    """`$(cat f)<our path>` executes a path that depends on the subshell's
    output; ownership cannot be proven, so the hook is preserved (the
    fail-safe direction: a malformed own hook stays, never a foreign one
    deleted)."""
    assert m._is_malformed_to_hook(
        f"$(cat /tmp/x){REPO}/skills/token-optimizer/scripts/measure.py ensure-health"
    ) is False


@pytest.mark.parametrize("foreign", [
    # our install root appears only inside an ARGUMENT (judge e2e class)
    "python3 $(which python3) /opt/vendor/measure.py --out {root}/logs/$(date +%s).log",
    # our install root appears only in a trailing COMMENT
    "python3 $(which python3) /opt/vendor/measure.py --quiet  # see {root}/skills/token-optimizer/scripts/measure.py",
    # our script path appears only in a comment; executed script is foreign
    "python3 $(date +%s) /opt/vendor/other.py  # {root}/skills/token-optimizer/scripts/measure.py",
])
def test_root_in_argument_or_comment_is_not_proof(m, foreign, installed):
    """A grounded root outside the executed script token is not ownership."""
    assert m._is_malformed_to_hook(foreign.format(root=installed.parent)) is False
    assert m._is_malformed_to_hook(foreign.format(root=REPO)) is False


def test_mixed_own_and_foreign_anchors_are_ambiguous_and_preserved(m, installed):
    """A command carrying BOTH our script and a foreign one is ambiguous;
    ambiguity must preserve."""
    ours = f"{REPO}/skills/token-optimizer/scripts/measure.py"
    cmd = f"python3 $(which python3) {ours} --quiet && python3 /opt/vendor/measure.py"
    assert m._is_malformed_to_hook(cmd) is False


def test_literal_plugin_root_env_resolution(m, monkeypatch):
    """A literal $CLAUDE_PLUGIN_ROOT resolves through the environment when
    set (the shell would expand it at run time); when unset, the hook is
    unprovable and preserved - fail-safe, disclosed."""
    shape = ("python3 $(which python3) "
             "${CLAUDE_PLUGIN_ROOT}/skills/token-optimizer/scripts/measure.py ensure-health")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    assert m._is_malformed_to_hook(shape) is True
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT")
    assert m._is_malformed_to_hook(shape) is False


def test_double_home_foreign_still_preserved_on_darwin(m, monkeypatch, tmp_path):
    monkeypatch.setattr(m.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    foreign = "python3 $HOME/Users/u/home/u/vendor/measure.py --quiet"
    assert m._is_malformed_to_hook(foreign) is False


def test_healer_preserves_other_tools_sharing_claude_dir(m, installed):
    """End to end over the real heal path (judge e2e classes): hooks from
    OTHER skills/plugins under the shared Claude dir survive - including
    the plugin cache - while our malformed hook is removed."""
    claude = installed.parent
    data = json.loads(installed.read_text())
    data["hooks"].setdefault("PreToolUse", []).extend([
        {"matcher": "Read", "hooks": [{"type": "command",
            "command": f"python3 $(which python3) {REPO}/skills/token-optimizer/scripts/read_cache.py --quiet"}]},
        {"matcher": "Bash", "hooks": [{"type": "command",
            "command": f"python3 $(which python3) {claude}/skills/othertool/measure.py --quiet"}]},
        {"matcher": "Bash", "hooks": [{"type": "command",
            "command": f"python3 $(which python3) {claude}/plugins/cache/mkt/othertool/1.0/scripts/measure.py --quiet"}]},
        {"matcher": "Bash", "hooks": [{"type": "command",
            "command": f"python3 $(which python3) {claude}/plugins/cache/mkt/token-optimizer/9.9/scripts/measure.py --quiet"}]},
        {"matcher": "Bash", "hooks": [{"type": "command",
            "command": f"python3 $(id) {claude}/plugins/cache/evil-mkt/token-optimizer/9.9.9/skills/token-optimizer/scripts/measure.py --quiet"}]},
    ])
    installed.write_text(json.dumps(data), encoding="utf-8")
    removed = m._fix_malformed_hook_commands()
    assert removed == 1, "only our own malformed hook may be removed"
    remaining = list(_all_commands(installed))
    assert any("skills/othertool" in c for c in remaining), "other skill's hook deleted"
    assert any("cache/mkt/othertool" in c for c in remaining), "other plugin's cache hook deleted"
    assert any("cache/mkt/token-optimizer/9.9" in c for c in remaining), "non-payload cache layout deleted"
    assert any("evil-mkt/token-optimizer/9.9.9" in c for c in remaining), \
        "same-name foreign cache plugin with identical layout deleted"


def test_healer_ignores_root_in_argument_e2e(m, installed):
    """End to end: our root referenced only as a foreign hook's argument
    (judge's verbatim class) does not get the hook removed."""
    claude = installed.parent
    data = json.loads(installed.read_text())
    data["hooks"].setdefault("PreToolUse", []).append({
        "matcher": "Bash", "hooks": [{"type": "command",
            "command": f"python3 $(which python3) /opt/vendor/measure.py --out {claude}/logs/$(date +%s).log"}],
    })
    installed.write_text(json.dumps(data), encoding="utf-8")
    removed = m._fix_malformed_hook_commands()
    assert removed == 0
    remaining = list(_all_commands(installed))
    assert any("/opt/vendor/measure.py" in c for c in remaining), "root-in-argument foreign hook deleted"


def test_windows_style_path_on_posix_is_unprovable_preserved(m):
    """A drive-letter token on a POSIX healer names no install this process
    runs from: unprovable, preserved (fail-safe). The reverse direction
    (Windows healer, its own drive-letter install paths) is covered by the
    platform-aware absolute/separator handling and verified by the Windows
    CI shard."""
    assert m._is_malformed_to_hook(
        "python3 $(which python3) D:\\a\\token-optimizer\\token-optimizer"
        "/skills/token-optimizer/scripts/read_cache.py --quiet"
    ) is False


def test_unc_absolute_token_on_posix_is_unprovable_preserved(m):
    """UNC absolute (\\\\server\\share\\...) is recognized as an absolute
    form by the classifier (so it is not misread as relative on Windows), but
    on a POSIX healer it names no install this process runs from: preserved."""
    assert m._is_malformed_to_hook(
        "python3 $(which python3) \\\\evil\\share\\skills\\token-optimizer"
        "\\scripts\\measure.py --quiet"
    ) is False


def test_drive_relative_token_is_not_absolute_preserved(m):
    """C:foo (drive-relative, no separator after the colon) is NOT an absolute
    path under Windows semantics; the classifier must not treat it as one."""
    assert m._is_malformed_to_hook(
        "python3 $(which python3) C:skills\\token-optimizer\\scripts\\measure.py --quiet"
    ) is False


def test_backslash_foreign_posix_path_preserved(m):
    """A foreign POSIX install whose token carries literal backslashes cannot
    be canon-collapsed into our install root on Linux: preserved."""
    assert m._is_malformed_to_hook(
        "python3 $(which python3) /home/u/.claude\\skills\\token-optimizer"
        "\\scripts\\measure.py --quiet"
    ) is False
