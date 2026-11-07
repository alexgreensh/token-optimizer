"""Installer/healer self-conflict: the malformed-hook detector must not
remove hooks that the installer itself wrote.

PROVENANCE RULE (supersedes the text-ownership proofs below it in history):
a hook is removable only when its exact command bytes are recorded in the
hook provenance ledger for this settings file, or are byte-identical to a
shipped hooks.json template instantiated for this install, AND it is
malformed. Every foreign/unprovable case here must stay preserved; the
positive controls are proven entries (an older shipped template, or a ledger
record), not Claude-written look-alikes, which are now preserved and logged.

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
import os
import shlex
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def m(monkeypatch, tmp_path):
    """Load measure.py fresh with a tmp snapshot dir and a private ledger dir."""
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tempfile.mkdtemp(prefix="to-md-"))
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    import measure
    import _measure.lifecycle as _lc
    measure._lc = _lc
    monkeypatch.setattr(measure, "CONFIG_DIR", tmp_path / "to-config")
    monkeypatch.setattr(measure, "_find_plugin_hooks_json", lambda: REPO / "hooks" / "hooks.json")
    return measure


def _hp():
    sys.path.insert(0, str(SCRIPTS))
    import hook_provenance
    return hook_provenance


def _old_template(m, needle="read_cache.py --quiet", root=REPO):
    """A command an OLDER setup_all_hooks wrote for this install that main's
    detector heals: a shipped template, resolved for `root`, that is malformed
    under the current resolver (the pre-backslash `${D%/*}` resolver)."""
    hp = _hp()
    for template in sorted(hp.SHIPPED_HOOK_TEMPLATES):
        cmd = m._resolve_hook_command(template, root)
        if needle in template and "${D%/*}" in template and hp.looks_malformed(cmd, False):
            return cmd
    raise AssertionError("no historical malformed template for " + needle)


def _ledger(m, *commands, event="PreToolUse", matcher="Read"):
    """Record commands as Token Optimizer-written for the current settings, in
    the (event, matcher) the end-to-end tests below place them in."""
    hp = _hp()
    m._settings_heal_ensure_dir()
    assert hp.record_contexts(hp.ledger_path_for(m), m.SETTINGS_PATH,
                              [(event, matcher, "command", c) for c in commands], "test")


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


CLAUDE_WRITTEN = [
    # Claude-written interactive subshell shapes (the old detector's target).
    f"python3 $(which python3) {REPO}/skills/token-optimizer/scripts/read_cache.py --quiet",
    f"python3 `which python3` {REPO}/skills/token-optimizer/scripts/measure.py ensure-health",
    f"python3 {REPO}/skills/token-optimizer/scripts/bash_hook.py $(echo --quiet)",
]


@pytest.mark.parametrize("evil", CLAUDE_WRITTEN)
def test_claude_written_subshell_shapes_are_unproven_and_preserved(m, evil):
    """Malformed, but nothing proves Token Optimizer wrote them: preserved.
    The same bytes become removable once the ledger records them."""
    assert _hp().looks_malformed(evil, False) is True
    assert m._is_malformed_to_hook(evil) is False
    _ledger(m, evil)
    assert m._is_malformed_to_hook(evil) is True


def test_resolver_with_injected_second_subshell_still_flagged(m, installed):
    """Exempting the known resolver must not become a blanket subshell
    exemption: an installer-shaped command carrying any second subshell is
    still malformed."""
    base = next(c for c in _all_commands(installed) if 'D="$(ls -d ' in c)
    poisoned = base.replace("; done; exit 0", "; done; $(touch /tmp/pwn); exit 0")
    assert poisoned != base
    assert _hp().looks_malformed(poisoned, False) is True
    # One byte off the recorded installer command is no longer ours.
    assert m._is_malformed_to_hook(poisoned) is False
    _ledger(m, poisoned)
    assert m._is_malformed_to_hook(poisoned) is True


def test_healer_removes_only_proven_entries(m, installed):
    """End to end: installer hooks + one older installer-written malformed
    hook + one Claude-written subshell hook. Only the proven one goes."""
    old = _old_template(m)
    claude = CLAUDE_WRITTEN[0]
    data = json.loads(installed.read_text())
    data["hooks"].setdefault("PreToolUse", []).extend([
        {"matcher": "Read", "hooks": [{"type": "command", "command": old}]},
        {"matcher": "Read", "hooks": [{"type": "command", "command": claude}]},
    ])
    installed.write_text(json.dumps(data), encoding="utf-8")
    removed = m._fix_malformed_hook_commands()
    assert removed == 1
    remaining = list(_all_commands(installed))
    assert old not in remaining
    assert claude in remaining
    assert not any(m._is_malformed_to_hook(c) for c in remaining)


def test_double_home_unproven_preserved_proven_removed_on_darwin(m, monkeypatch, tmp_path):
    monkeypatch.setattr(m.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    evil = f"python3 $HOME/Users/u{REPO}/skills/token-optimizer/scripts/measure.py ensure-health"
    assert m._is_malformed_to_hook(evil) is False
    _ledger(m, evil)
    assert m._is_malformed_to_hook(evil) is True
    monkeypatch.setattr(m.platform, "system", lambda: "Linux")
    assert m._is_malformed_to_hook(evil) is False  # doubled $HOME is a macOS shape


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
    """The exempted resolver template must stay byte-identical to what the
    installer writes; if hooks.json changes shape, this test forces a
    conscious re-decision instead of silent drift. Asserted behaviourally:
    an own-install hook carrying the hooks.json resolver verbatim must NOT
    be flagged - if the exempted template drifted, its leftover ``$(``
    would flag the command."""
    resolver = _hooks_json_resolver()
    assert _hp().CURRENT_RESOLVER == resolver
    cmd = (f'D="${{CLAUDE_PLUGIN_ROOT}}"; [ -r "$D/hooks/python-launcher.sh" ] || {resolver}; '
           f'D="${{D%/}}"; exec "$b" "$D/hooks/python-launcher.sh" '
           f'"$D/hooks/run.py" {REPO}/skills/token-optimizer/scripts/read_cache.py --quiet')
    # Even recorded as ours, the resolver alone is plumbing, not malformation.
    _ledger(m, cmd)
    assert m._is_malformed_to_hook(cmd) is False


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
    """The malformation gate stays exact: each shape is malformed, so it is
    removed when (and only when) it is proven ours."""
    assert _hp().looks_malformed(evil, False) is True
    assert m._is_malformed_to_hook(evil) is False
    _ledger(m, evil)
    assert m._is_malformed_to_hook(evil) is True


def test_exact_template_single_occurrence_passes(m):
    """The true resolver on its own, exactly as installed, stays clean."""
    cmd = ('for b in bash; do ' + _READ_CMD_TAIL + RESOLVER + '; D="${D%/}"; exec "$b" "$D/hooks/run.py" skills/token-optimizer/scripts/read_cache.py --quiet; done; exit 0')
    _ledger(m, cmd)
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
    ours = CLAUDE_WRITTEN[0]
    _ledger(m, ours)
    data = json.loads(installed.read_text())
    data["hooks"].setdefault("PreToolUse", []).extend([
        {"matcher": "Read", "hooks": [{"type": "command", "command": ours}]},
        {"matcher": "Bash", "hooks": [{"type": "command",
            "command": "python3 $(which python3) /home/u/vendor/measure.py --quiet"}]},
    ])
    installed.write_text(json.dumps(data), encoding="utf-8")
    removed = m._fix_malformed_hook_commands()
    assert removed == 1, "only the provably-ours malformed hook may be removed"
    remaining = [c for c in _all_commands(installed)]
    assert any("vendor/measure.py" in c for c in remaining), "foreign hook deleted"
    assert ours not in remaining


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
        {"matcher": "Read", "hooks": [{"type": "command", "command": _old_template(m)}]},
        {"matcher": "Bash", "hooks": [{"type": "command",
            "command": "python3 $(which python3) /home/u/vendor/token-optimizer/scripts/measure.py --quiet"}]},
    ])
    installed.write_text(json.dumps(data), encoding="utf-8")
    removed = m._fix_malformed_hook_commands()
    assert removed == 1
    remaining = [c for c in _all_commands(installed)]
    assert any("vendor/token-optimizer" in c for c in remaining), "lookalike foreign hook deleted"


def test_root_prefix_collision_against_proven_root_survives(m, monkeypatch, tmp_path):
    """A sibling dir sharing a string prefix with a PROVEN root is not under
    that root and must survive. tmp_path-based: resolving a real
    repo-adjacent path trips the suite's real-home guard on CI, where the
    repo lives under the runner home. The proven root here is the running
    install root (REPO); $CLAUDE_PLUGIN_ROOT set to it changes nothing."""
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    evil = tmp_path / "plugin-evil"
    cmd = f"python3 $(which python3) {evil}/skills/token-optimizer/scripts/measure.py --quiet"
    assert m._is_malformed_to_hook(cmd) is False
    # The same shipped template at the sibling root is not ours; at the
    # running install root it is (positive control).
    assert m._is_malformed_to_hook(_old_template(m, root=evil)) is False
    assert m._is_malformed_to_hook(_old_template(m)) is True


def test_owned_paths_under_each_root_are_proven(m, installed, monkeypatch):
    """Positive controls: the same malformed shape is flagged only when the
    script token resolves under a runtime-proven root - the running install
    root or the resolved plugin-root env. The shared Claude dir is NOT a
    root: our own skill location under it is unprovable (and preserved)
    when the healer is not running from that install."""
    shape = "python3 $(which python3) {root}/skills/token-optimizer/scripts/read_cache.py --quiet"
    # Claude-written shapes are unproven at every root.
    assert m._is_malformed_to_hook(shape.format(root=REPO)) is False
    assert m._is_malformed_to_hook(shape.format(root=installed.parent)) is False
    # Shipped templates prove only at the running install root.
    assert m._is_malformed_to_hook(_old_template(m)) is True
    assert m._is_malformed_to_hook(_old_template(m, root=installed.parent)) is False
    # $CLAUDE_PLUGIN_ROOT never adds a root: a foreign env root proves nothing.
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/srv/plugin-root")
    assert m._is_malformed_to_hook(_old_template(m, root="/srv/plugin-root")) is False
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    assert m._is_malformed_to_hook(_old_template(m)) is True


def test_owned_cache_via_real_plugin_root(m, installed, monkeypatch, tmp_path):
    """A plugin-cache path is provable only when the running install IS that
    cache version dir - i.e. the healer's own __file__ lives under it, which
    makes the install root and the resolved $CLAUDE_PLUGIN_ROOT agree. A
    same-named plugin from another marketplace, a stale version, or a
    foreign env root is not provably ours and survives."""
    own_cache = tmp_path / "plugins" / "cache" / "mkt" / "token-optimizer" / "5.13.66"
    # Env root that is NOT this running install proves nothing.
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(own_cache))
    shape = "python3 $(id) {root}/skills/token-optimizer/scripts/measure.py ensure-health"
    assert m._is_malformed_to_hook(shape.format(root=own_cache)) is False
    literal = ("python3 $(id) ${CLAUDE_PLUGIN_ROOT}/skills/token-optimizer/"
               "scripts/measure.py ensure-health")
    assert m._is_malformed_to_hook(literal) is False
    foreign_same_name = m.CLAUDE_DIR / "plugins" / "cache" / "evil-mkt" / "token-optimizer" / "9.9.9"
    assert m._is_malformed_to_hook(shape.format(root=foreign_same_name)) is False
    # Even the same marketplace dir, a different plugin slot is foreign.
    other_slot = m.CLAUDE_DIR / "plugins" / "cache" / "mkt" / "token-optimizer" / "9.9.9"
    assert m._is_malformed_to_hook(shape.format(root=other_slot)) is False
    for root in (own_cache, foreign_same_name, other_slot):
        assert m._is_malformed_to_hook(_old_template(m, root=root)) is False


def test_quoted_own_script_path_is_preserved(m):
    """Quoted own spellings are not proven by the coarse basename gate, so
    they are preserved rather than repaired (fail-safe)."""
    own = f'{REPO}/skills/token-optimizer/scripts/measure.py'
    assert m._is_malformed_to_hook(f'python3 "{own}" $(id)') is False
    assert m._is_malformed_to_hook(f"python3 '{own}' `id`") is False


def test_backslash_own_script_path_is_preserved(m):
    """A backslash spelling of our path is not bytes Token Optimizer wrote:
    preserved on every platform (no separator rewriting on the removal path).
    Foreign backslash spellings stay preserved too."""
    own = f'{REPO}/skills/token-optimizer/scripts/measure.py'
    bs = own.replace('/', '\\')
    assert m._is_malformed_to_hook(f'python3 {bs} $(id)') is False
    foreign_bs = '/opt/vendor/measure.py'.replace('/', '\\')
    assert m._is_malformed_to_hook(f'python3 {foreign_bs} $(id)') is False
    foreign_drive = 'D:\\opt\\vendor\\skills\\token-optimizer\\scripts\\measure.py'
    assert m._is_malformed_to_hook(f'python3 {foreign_drive} $(id)') is False


def test_foreign_plugin_root_env_never_proves_ownership(m, monkeypatch, tmp_path):
    """$CLAUDE_PLUGIN_ROOT is environment input, not proof: pointing it at a
    foreign same-named plugin (another marketplace's cache entry) must not
    make that plugin's hooks ours."""
    foreign = tmp_path / "plugins" / "cache" / "evil-mkt" / "token-optimizer" / "9.9.9"
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(foreign))
    cmd = f"python3 $(id) {foreign}/skills/token-optimizer/scripts/measure.py ensure-health"
    assert m._is_malformed_to_hook(cmd) is False
    literal = ("python3 $(id) ${CLAUDE_PLUGIN_ROOT}/skills/token-optimizer/"
               "scripts/measure.py ensure-health")
    assert m._is_malformed_to_hook(literal) is False


def test_sol_v8_foreign_spellings_are_preserved(m, installed, tmp_path):
    """Six foreign executions must survive: spellings that only mention
    our path (quoted, partially quoted, literal apostrophe/backslash, or as a
    driver argument) are not ownership proof, and the healer removes none."""
    own = f'{REPO}/skills/token-optimizer/scripts/measure.py'
    prefix = str(tmp_path / 'vendor ')
    case = tmp_path / 'foreign-execution'

    def _write(argument):
        # The predicate never reads these files; they only make the foreign
        # execution real on POSIX. On Windows several names are illegal
        # (WinError 123), and the backslash spelling IS our own measure.py,
        # so writing it would overwrite the repo's real file.
        if os.name == "nt":
            return
        path = Path(argument)
        if not path.is_absolute():
            path = case / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('print("FOREIGN_HOOK_EXECUTED")\n', encoding='utf-8')

    commands = {}

    def _add(name, argument, spelling):
        _write(argument)
        commands[name] = 'python3 ' + spelling + ' $(printf harmless)'

    _add('foreign-double-quoted-space', prefix + own, '"' + prefix + own + '"')
    _add('foreign-single-quoted-space', prefix + own, "'" + prefix + own + "'")
    _add('foreign-partial-quoted-space', prefix + own, '"' + prefix + '"' + own)
    _add('foreign-literal-apostrophe', "'" + own, '"' + "'" + own + '"')
    if os.name != "nt":
        # POSIX only: a backslash is a literal filename character there. On
        # Windows this spelling is our own path and is rightly ours.
        _add('foreign-literal-backslash-name', own.replace('/', '\\'),
             shlex.quote(own.replace('/', '\\')))
    driver = case / 'foreign_driver_do_not_remove.py'
    driver.parent.mkdir(parents=True, exist_ok=True)
    driver.write_text('print("FOREIGN_HOOK_EXECUTED")\n', encoding='utf-8')
    commands['foreign-executable-own-path-only-in-argument'] = (
        'python3 ' + str(driver) + ' --source "' + own + '" $(printf harmless)')

    for name, cmd in commands.items():
        assert m._is_malformed_to_hook(cmd) is False, name

    data = {
        'permissions': {'deny': ['Bash(rm:*)']},
        'env': {'USER_DATA': 'keep'},
        'hooks': {'SessionStart': [{'matcher': 'startup', 'hooks': [
            {'type': 'command', 'command': cmd, 'timeout': 17}
            for cmd in commands.values()
        ]}]},
    }
    installed.write_text(json.dumps(data), encoding='utf-8')
    assert m._fix_malformed_hook_commands() == 0
    after = json.loads(installed.read_text())
    assert after['permissions'] == data['permissions']
    assert after['env'] == data['env']
    assert sorted(_all_commands(installed)) == sorted(commands.values())


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
    assert m._is_malformed_to_hook(shape) is False
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT")
    assert m._is_malformed_to_hook(shape) is False
    # Raw (unresolved) shipped templates are never proof: main never removes
    # them, and settings.json hooks get no plugin-root expansion.
    hp = _hp()
    raw = next(t for t in sorted(hp.SHIPPED_HOOK_TEMPLATES)
               if "${D%/*}" in t and hp.looks_malformed(t, False))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    assert m._is_malformed_to_hook(raw) is False


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
        {"matcher": "Read", "hooks": [{"type": "command", "command": _old_template(m)}]},
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


def test_drive_letter_foreign_path_is_unprovable_preserved(m):
    """A drive-letter token naming a FOREIGN root is preserved on every
    platform: on POSIX the form is unprovable (fail-safe); on Windows it is
    absolute-classified but does not resolve under either runtime-proven
    root. (Must not name a real CI checkout root - the first version of
    this test used D:\\a\\token-optimizer\\token-optimizer, which IS
    the GitHub Windows runner checkout, so on Windows it proved ours.)"""
    assert m._is_malformed_to_hook(
        "python3 $(which python3) C:\\ci-foreign\\token-optimizer"
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


def _windows_spelling(path):
    """POSIX stand-in for a Windows path: keeps the leading slash (so it is
    absolute here) and spells every other separator as a backslash."""
    text = str(path)
    return text[:1] + text[1:].replace("/", "\\")


class _NtOs:
    """The real os module, reporting os.name == "nt"."""
    name = "nt"

    def __init__(self, real):
        self._real = real

    def __getattr__(self, attr):
        return getattr(self._real, attr)


def test_windows_backslash_hooks_keep_ownership_proof(m, installed, monkeypatch):
    """Windows spellings prove nothing by shape. Claude-written mixed or
    backslashed spellings of our path are unproven and preserved; the same
    bytes recorded by an installer are removable. (Byte-exact Windows
    template instances: tests/test_hook_provenance.py.)"""
    monkeypatch.setattr(m, "os", _NtOs(m.os))
    root = _windows_spelling(REPO)
    script = "skills/token-optimizer/scripts/read_cache.py"
    mixed = f"python3 $(which python3) {root}/{script} --quiet"
    backslashed = f"python3 $(which python3) {root}\\{script.replace('/', chr(92))} --quiet"
    assert m._is_malformed_to_hook(mixed) is False
    assert m._is_malformed_to_hook(backslashed) is False
    _ledger(m, backslashed)
    assert m._is_malformed_to_hook(backslashed) is True
    assert m._is_malformed_to_hook(mixed) is False
    # Partially quoted foreign path: bash reads ONE word ending in our path.
    # posix=False tokenizing would expose our path as its own token.
    foreign = f'python3 "C:\\vendor "{root}\\{script.replace("/", chr(92))} $(printf x)'
    assert m._is_malformed_to_hook(foreign) is False
    elsewhere = f"python3 $(which python3) D:\\elsewhere\\{script.replace('/', chr(92))} --quiet"
    assert m._is_malformed_to_hook(elsewhere) is False


@pytest.mark.skipif(os.name == "nt", reason="a backslash path IS ours on Windows")
def test_posix_keeps_backslash_as_literal_filename_character(m, installed):
    """Outside Windows nothing changes: a backslash-spelled path is a different
    file (or an escape), never proof that the hook is ours."""
    root = _windows_spelling(REPO)
    script = "skills/token-optimizer/scripts/read_cache.py".replace("/", chr(92))
    assert m._is_malformed_to_hook(f"python3 $(which python3) {root}\\{script} --quiet") is False
