"""Regression: hook commands must fail open (exit 0), never 127, when the
launcher path is missing.

The bug: a hook command execs ${CLAUDE_PLUGIN_ROOT}/hooks/python-launcher.sh
with no existence check. When a plugin refresh GCs the old version dir, the
baked/resolved path points at a deleted directory, so `exec bash <deleted>`
prints "No such file or directory" and exits 127 on every tool call (the
trailing `exit 0` never runs because exec already replaced the shell).

Fix: every launcher command guards with `[ -r "$L" ] || exit 0` before exec.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOKS_JSON_FILES = [
    REPO / "hooks" / "hooks.json",
    REPO / "plugins" / "token-optimizer" / "hooks" / "hooks.json",
    REPO / "cowork" / "token-optimizer" / "hooks" / "hooks.json",
]


def _iter_commands(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "command" and isinstance(v, str):
                yield v
            else:
                yield from _iter_commands(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_commands(item)


def _launcher_commands(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return [c for c in _iter_commands(data) if "python-launcher.sh" in c]


@pytest.mark.parametrize("hooks_path", HOOKS_JSON_FILES, ids=lambda p: p.parent.parent.name)
def test_every_launcher_command_is_guarded(hooks_path):
    cmds = _launcher_commands(hooks_path)
    assert cmds, f"no launcher commands found in {hooks_path}"
    for cmd in cmds:
        # Two fail-open shapes are valid: the simple guard (Claude/Cowork, where
        # ${CLAUDE_PLUGIN_ROOT} tracks the live version) and the Codex mirror's
        # runtime version-resolver (prefer the root, else scan for the newest dir).
        guarded = '[ -r "$L" ]' in cmd
        resolver = 'sort -V' in cmd and '[ -r "$D/hooks/python-launcher.sh" ]' in cmd
        assert guarded or resolver, (
            f"unguarded launcher command (would 127 on a stale version dir):\n{cmd}"
        )


def test_every_manifest_uses_runtime_resolver():
    """Every plugin manifest must self-heal mid-session (resolve the newest
    version dir per call): Codex pins ${CLAUDE_PLUGIN_ROOT} to the session's
    plugin version, and a Claude Code update can delete the loaded version dir
    under a running session too (5.13.51)."""
    for tree in ("plugins/token-optimizer/hooks", "hooks", "cowork/token-optimizer/hooks"):
        for cmd in _launcher_commands(REPO / tree / "hooks.json"):
            assert 'sort -V' in cmd and 'ls -d' in cmd, f"{tree} command lost its resolver:\n{cmd}"


def test_resolver_keeps_per_handler_dedup_identity():
    """The resolver's $(...) once collapsed every command to the same dedup
    identity ("run.py:..."), so setup_all_hooks / duplicate cleanup would treat
    all of an event's hooks as one. Each root entry must keep its own handler
    identity, distinct across different handlers."""
    scripts = str(REPO / "skills" / "token-optimizer" / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import measure
    data = json.loads((REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    seen = {}
    for event, groups in data["hooks"].items():
        for group in groups:
            for hook in group["hooks"]:
                cmd = hook["command"]
                ident = measure._hook_command_identity(cmd)
                handler_args = cmd.split('"$D/hooks/run.py"', 1)[1].split(";", 1)[0].strip()
                handler = handler_args.split()[0].rsplit("/", 1)[-1]
                assert handler in ident or ident.startswith("run.py:hooks/" + handler), (event, ident, handler)
                seen.setdefault((event, group.get("matcher")), set()).add(ident)
    assert all(len(v) == sum(1 for _ in v) for v in seen.values())
    pre_compact = seen[("PreCompact", None)]
    assert len(pre_compact) == 3, f"PreCompact's three handlers collapsed: {pre_compact}"


@pytest.mark.parametrize("hooks_path", HOOKS_JSON_FILES, ids=lambda p: p.parent.parent.name)
def test_missing_launcher_exits_zero_not_127(hooks_path, tmp_path):
    """Run each real command with CLAUDE_PLUGIN_ROOT pointing at a gone dir."""
    env_root = "/nonexistent/token-optimizer/9.9.9-gc"
    for cmd in _launcher_commands(hooks_path):
        # Claude substitutes ${CLAUDE_PLUGIN_ROOT} in the command string; emulate
        # by exporting it so the shell expands it identically.
        proc = subprocess.run(
            ["sh", "-c", cmd],
            env={"CLAUDE_PLUGIN_ROOT": env_root, "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, (
            f"stale launcher must exit 0, got {proc.returncode}\n"
            f"cmd: {cmd}\nstderr: {proc.stderr}"
        )
        assert "No such file or directory" not in proc.stderr, (
            f"127-style error leaked to stderr:\n{proc.stderr}"
        )


def test_present_launcher_still_runs(tmp_path):
    """With a real launcher, the command must still exec it (fix is fail-open,
    not fail-closed)."""
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    launcher = hooks_dir / "python-launcher.sh"
    launcher.write_text("#!/bin/bash\necho RAN_OK\n")
    launcher.chmod(0o755)
    (hooks_dir / "run.py").write_text("import sys\n")  # not actually executed by echo stub
    cmd = _launcher_commands(HOOKS_JSON_FILES[0])[0]
    proc = subprocess.run(
        ["sh", "-c", cmd],
        env={"CLAUDE_PLUGIN_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0
    assert "RAN_OK" in proc.stdout


def test_setup_all_hooks_retrofits_guard_on_stable_root():
    """An existing unguarded baked command must be replaced with the guarded
    form even when its root is unchanged (stable-dir script install upgraded in
    place), otherwise that population never gains the fail-open guard.

    Source-guard (mirrors test_setup_all_hooks_containment_is_separator_normalized):
    the `guard_stale` predicate must exist and gate the skip condition.
    """
    src = (REPO / "skills" / "token-optimizer" / "scripts" / "measure.py").read_text(encoding="utf-8")
    assert 'guard_stale = (' in src, "guard-retrofit staleness predicate missing"
    assert '"[ -r " in resolved_cmd and "[ -r " not in existing_cmd' in src, (
        "guard_stale must fire when the new template is guarded and the existing command is not"
    )
    assert "and not guard_stale:" in src, (
        "the skip condition must not short-circuit past guard_stale (would keep the "
        "unguarded command forever on a stable root)"
    )


def test_smart_compact_commands_are_guarded():
    sys.path.insert(0, str(REPO / "skills" / "token-optimizer" / "scripts"))
    import measure

    import shlex

    cmds = measure._smart_compact_hook_commands()
    assert cmds
    # The path is shell-quoted only when it needs it: POSIX always writes
    # '<mp>', Windows writes shlex.quote(<mp with forward slashes>), which is
    # bare for a path with no spaces or quotes (D:/a/.../measure.py on CI).
    # Either way the guard must test exactly the script the command runs.
    for event, cmd in cmds.items():
        guard, sep, rest = cmd.partition(" || exit 0; ")
        assert sep and guard.startswith("[ -r ") and guard.endswith(" ]"), (
            f"{event} smart-compact command is unguarded: {cmd}")
        guarded = shlex.split(guard[len("[ -r "):-2])
        argv = shlex.split(rest)
        assert len(guarded) == 1 and guarded[0] == argv[1], (event, cmd)
        assert guarded[0].endswith("measure.py"), (event, cmd)
