#!/usr/bin/env python3
"""Anti-drift tests for the committed self-contained Cowork plugin.

Cowork's marketplace renders a plugin only when its ``source`` points at a
small self-contained dir. The desktop ``token-optimizer`` entry has
``source: "./"`` (the whole ~328MB repo) and does NOT render in Cowork, so a
SLIM sibling is committed at ``cowork/token-optimizer/`` and listed as its own
marketplace plugin ``token-optimizer-cowork``. That committed tree is BUILT by
``cowork_install.py --emit-committed`` from the master runtime set, so it can
silently drift from the source it was generated off. These tests are the
tripwire: they rebuild via the packager API and assert the committed tree still
matches, byte for byte where it must.

Covers:
  (a) committed hooks/hooks.json == build_cowork_hooks(master hooks/hooks.json)
      -- trimmed to the 4 Cowork events, no SessionStart, keepwarm dropped, and
      the run-once features carried on UserPromptSubmit.
  (b) committed .claude-plugin/plugin.json has name token-optimizer-cowork, the
      ./hooks/hooks.json pointer, and version == root plugin.json version.
  (c) skills/token-optimizer/scripts/measure.py exists in the committed dir and
      is byte-identical to the root one.
  (d) rebuilding via ``build_committed_plugin()`` INTO A STAGING ROOT
      reproduces the committed cowork/token-optimizer/ byte for byte (no
      drift). The rebuild never runs against the working tree: a test that
      regenerates in place can destroy uncommitted work, and the clean
      ``git status`` it then asserts hides the loss.
  (e) the marketplace lists exactly 3 plugins with the expected names/sources.

Run: python3 -m pytest tests/test_cowork_committed_plugin.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from _tree_parity import fingerprint, stage_inputs, tree_drift

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
HOOKS_JSON = REPO / "hooks" / "hooks.json"
COMMITTED = REPO / "cowork" / "token-optimizer"
MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"
ROOT_MANIFEST = REPO / ".claude-plugin" / "plugin.json"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cowork_install  # noqa: E402

MASTER_TEMPLATE = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _commands(hooks_by_event, event):
    out = []
    for group in hooks_by_event.get(event, []):
        for hook in group.get("hooks", []):
            out.append(hook.get("command", ""))
    return out


# --------------------------------------------------------------------------- #
# (a) committed hooks.json == build_cowork_hooks(master)
# --------------------------------------------------------------------------- #

def test_committed_hooks_equals_build_cowork_hooks():
    committed = _load(COMMITTED / "hooks" / "hooks.json")
    expected = cowork_install.build_cowork_hooks(MASTER_TEMPLATE)
    assert committed == expected, (
        "committed cowork/token-optimizer/hooks/hooks.json drifted from "
        "build_cowork_hooks(master hooks/hooks.json); rerun "
        "`cowork_install.py --emit-committed`"
    )


def test_committed_hooks_are_the_four_cowork_events_only():
    hooks = _load(COMMITTED / "hooks" / "hooks.json")["hooks"]
    assert set(hooks) == {"UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}
    assert "SessionStart" not in hooks
    for absent in ("PreCompact", "PostCompact", "SessionEnd", "StopFailure", "CwdChanged"):
        assert absent not in hooks, f"{absent} must not survive into the committed plugin"


def test_committed_hooks_drop_keepwarm_and_carry_runonce_on_userpromptsubmit():
    hooks = _load(COMMITTED / "hooks" / "hooks.json")["hooks"]
    all_cmds = [c for evt in hooks for c in _commands(hooks, evt)]
    assert [c for c in all_cmds if "keepwarm" in c] == [], "keepwarm must be dropped"

    # The run-once features (ensure-health, quality-cache --force,
    # compact-restore --new-session-only) and the original UserPromptSubmit
    # subcommands are consolidated into ONE dispatcher entry,
    # hooks/userpromptsubmit_runner.py, which imports measure.py once and runs
    # all six in-process with the --once-per-session marker guard replicated
    # internally. The committed Cowork payload is a pure trim of the master, so
    # its UserPromptSubmit is that single dispatcher.
    ups = _commands(hooks, "UserPromptSubmit")
    assert ups, "committed UserPromptSubmit must have at least one command"
    assert any("userpromptsubmit_runner.py" in c for c in ups), (
        "consolidated UserPromptSubmit dispatcher (userpromptsubmit_runner.py) "
        "missing from committed plugin"
    )
    # The former per-subcommand commands must NOT survive as separate entries.
    for former in (
        "ensure-health",
        "quality-cache --force",
        "compact-restore --new-session-only",
        "--once-per-session",
    ):
        assert not any(former in c for c in ups), (
            f"former UserPromptSubmit subcommand/guard {former!r} should be inside "
            "the runner, not a separate committed command"
        )


def test_every_committed_command_uses_plugin_root_resolver():
    hooks = _load(COMMITTED / "hooks" / "hooks.json")["hooks"]
    cmds = [c for evt in hooks for c in _commands(hooks, evt)]
    assert cmds, "expected at least one committed hook command"
    for command in cmds:
        assert "${CLAUDE_PLUGIN_ROOT}" in command, command


# --------------------------------------------------------------------------- #
# (b) committed plugin.json manifest shape
# --------------------------------------------------------------------------- #

def test_committed_manifest_name_version_and_hooks_pointer():
    manifest = _load(COMMITTED / ".claude-plugin" / "plugin.json")
    assert manifest["name"] == "token-optimizer-cowork"
    assert manifest["hooks"] == "./hooks/hooks.json"
    root_version = _load(ROOT_MANIFEST)["version"]
    assert manifest["version"] == root_version, (
        f"committed plugin version {manifest['version']!r} != root "
        f"plugin.json version {root_version!r}"
    )
    assert "cowork" in manifest["description"].lower()


# --------------------------------------------------------------------------- #
# (c) measure.py byte-identical to root
# --------------------------------------------------------------------------- #

def test_committed_measure_py_is_byte_identical_to_root():
    committed_measure = COMMITTED / "skills" / "token-optimizer" / "scripts" / "measure.py"
    root_measure = REPO / "skills" / "token-optimizer" / "scripts" / "measure.py"
    assert committed_measure.exists(), "measure.py missing from committed plugin"
    assert root_measure.exists(), "root measure.py missing"
    assert committed_measure.read_bytes() == root_measure.read_bytes(), (
        "committed measure.py drifted from the root copy; rerun --emit-committed"
    )


def test_committed_dir_is_self_contained():
    # The four load-bearing surfaces a Cowork install needs.
    assert (COMMITTED / ".claude-plugin" / "plugin.json").exists()
    assert (COMMITTED / "hooks" / "hooks.json").exists()
    assert (COMMITTED / "skills" / "token-optimizer" / "scripts" / "measure.py").exists()
    assert (COMMITTED / "commands").is_dir()
    assert (COMMITTED / "commands" / "apply-safe.md").read_bytes() == (
        REPO / "commands" / "apply-safe.md").read_bytes()


# --------------------------------------------------------------------------- #
# (d) staged rebuild reproduces the committed tree (no drift)
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the generator writes hooks.json with Path.write_text, which newline-"
    "translates to CRLF on Windows and would report spurious byte drift against the "
    "LF-committed tree. Windows users consume the pre-built committed Cowork plugin, "
    "they never regenerate it, so reproducibility is verified on POSIX, not here.",
)
def test_rebuild_emit_committed_reproduces_committed_tree(tmp_path):
    """--emit-committed run against a STAGING root must reproduce
    cowork/token-optimizer/ byte for byte.

    Previously this shelled out to ``cowork_install.py --emit-committed`` at the
    repo root and asserted ``git status`` was clean -- which meant every suite run
    wiped and rewrote 131 tracked files under cowork/token-optimizer/, silently
    discarding any uncommitted edit there. Same guarantee, no writes to the real
    tree: stage the generator's inputs in tmp_path, build there, diff the result
    against the committed tree."""
    before = fingerprint(COMMITTED)

    stage = stage_inputs(tmp_path / "repo", REPO, cowork_install.PAYLOAD_INCLUDE)
    built = cowork_install.build_committed_plugin(stage)
    assert built.is_dir(), "build_committed_plugin produced no tree"
    assert stage in built.parents, "the build escaped the staging root"

    drift = tree_drift(built, COMMITTED)
    assert not drift, (
        "the committed Cowork plugin is not reproducible from --emit-committed:\n"
        + "\n".join(drift)
        + "\n\nRerun `python3 skills/token-optimizer/scripts/cowork_install.py "
        "--emit-committed` and commit the result."
    )

    # Prove the build stayed in tmp_path: inode/mtime too, so a content-identical
    # rewrite (how the old in-place rebuild hid on a clean tree) still trips this.
    assert fingerprint(COMMITTED) == before, (
        "this test wrote to the real cowork/token-optimizer/ tree; the rebuild must "
        "stay inside tmp_path"
    )


# --------------------------------------------------------------------------- #
# (e) marketplace shape
# --------------------------------------------------------------------------- #

def test_marketplace_lists_exactly_three_plugins():
    # The desktop status bar ships inside token-optimizer, not as its own entry.
    plugins = _load(MARKETPLACE)["plugins"]
    by_name = {p["name"]: p for p in plugins}
    assert len(plugins) == 3, f"expected 3 plugins, got {[p['name'] for p in plugins]}"
    assert set(by_name) == {"token-optimizer", "to-hook-probe", "token-optimizer-cowork"}
    assert by_name["token-optimizer"]["source"] == "./"
    assert by_name["to-hook-probe"]["source"] == "./cowork/to-hook-probe"
    assert by_name["token-optimizer-cowork"]["source"] == "./cowork/token-optimizer"


def test_marketplace_cowork_entry_version_matches_root_and_probe_pinned():
    by_name = {p["name"]: p for p in _load(MARKETPLACE)["plugins"]}
    root_version = _load(ROOT_MANIFEST)["version"]
    assert by_name["token-optimizer"]["version"] == root_version
    assert by_name["token-optimizer-cowork"]["version"] == root_version
    assert by_name["to-hook-probe"]["version"] == "0.1.0"
    kw = by_name["token-optimizer-cowork"]["keywords"]
    assert "cowork" in kw and "beta" in kw
    assert by_name["token-optimizer-cowork"]["category"] == "productivity"
