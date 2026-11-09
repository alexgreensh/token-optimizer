"""Cursor installer: read-merge-write of ~/.cursor/hooks.json + Windows refusal.

Unlike Copilot (which owns a dedicated per-plugin hook file), Cursor has ONE
shared ``~/.cursor/hooks.json``. The installer must therefore merge, never
clobber: foreign entries are preserved verbatim, only entries whose ``command``
points at our bridge path are replaced/removed. These tests pin that contract,
the six-event hook map, and the fail-closed Windows refusal.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cursor_install as ci  # noqa: E402
import cursor_hook_bridge as bridge_mod  # noqa: E402

# These tests assert POSIX file modes and POSIX-quoted commands. The Windows
# command form is covered by test_windows_install_writes_shell_neutral_commands
# (runs everywhere) and tests/test_adapter_hook_command.py.
needs_posix = pytest.mark.skipif(
    os.name == "nt",
    reason="asserts POSIX file modes and the POSIX command form",
)



@pytest.fixture(autouse=True)
def _trusted_python_env(tmp_path, monkeypatch):
    """A CONTROLLED trusted interpreter for resolver/install tests.

    The host's real interpreter is not guaranteed to pass the trust gate --
    hosted-CI tool caches extract python world-writable (measured on runner
    33618210157), so falling back to sys.executable/$PATH makes these tests
    environment-dependent. Point TOKEN_OPTIMIZER_PYTHON at a tmp interpreter
    with clean modes (0755 file in a 0755 euid-owned dir) instead. Tests that
    need a specific resolution override the env or mock the gate themselves.
    """
    d = tmp_path / "trusted-bin"
    d.mkdir(mode=0o755)
    f = d / "python3"
    f.write_bytes(b"#!/bin/sh\n")
    os.chmod(f, 0o755)
    os.chmod(d, 0o755)
    monkeypatch.setenv("TOKEN_OPTIMIZER_PYTHON", str(f))


@pytest.fixture()
def home(tmp_path):
    return tmp_path / "cursor-home"


def _bridge_path(home):
    return ci._plugin_dir(home) / "cursor_hook_bridge.py"


def _foreign_entry():
    return {"command": "echo other-tool", "type": "command", "timeout": 5}


@needs_posix
def test_install_writes_merged_hooks_and_payload(home):
    home.mkdir()
    # A foreign entry in sessionStart must survive; a stale "ours" entry is replaced.
    hooks_path = ci._host_hooks_path(home)
    hooks_path.write_text(json.dumps({
        "version": 1,
        "hooks": {
            "sessionStart": [
                _foreign_entry(),
                {"command": f"TOKEN_OPTIMIZER_RUNTIME=cursor python3 {_bridge_path(home)} sessionStart"},
            ],
        },
    }), encoding="utf-8")

    result = ci.install(home=home)

    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert result["hook_file"] == str(hooks_path)
    # every wired event is present
    assert set(data["hooks"]) == {
        "sessionStart", "preToolUse", "postToolUse", "preCompact", "stop", "sessionEnd",
        "afterAgentResponse", "postToolUseFailure", "afterShellExecution",
        "subagentStart", "subagentStop"
    }
    # foreign entry preserved, stale ours replaced by exactly one fresh ours
    starts = data["hooks"]["sessionStart"]
    assert _foreign_entry() in starts
    ours = [e for e in starts if str(_bridge_path(home)) in e["command"]]
    assert len(ours) == 1
    assert " python3 " not in f" {ours[0]['command']} "

    # payload landed next to the bridge path the command points at
    assert _bridge_path(home).exists()


@needs_posix
def test_install_never_drops_foreign_entries(home):
    home.mkdir()
    ci._host_hooks_path(home).write_text(json.dumps({
        "version": 1,
        "hooks": {"stop": [_foreign_entry()]},
    }), encoding="utf-8")

    ci.install(home=home)
    ci.install(home=home)  # idempotent

    data = json.loads(ci._host_hooks_path(home).read_text(encoding="utf-8"))
    stops = data["hooks"]["stop"]
    assert _foreign_entry() in stops
    assert len([e for e in stops if str(_bridge_path(home)) in e["command"]]) == 1


@needs_posix
def test_uninstall_removes_only_our_entries_and_payload(home):
    home.mkdir()
    ci._host_hooks_path(home).write_text(json.dumps({
        "version": 1,
        "hooks": {"sessionStart": [_foreign_entry()]},
    }), encoding="utf-8")
    ci.install(home=home)

    result = ci.uninstall(home=home)

    data = json.loads(ci._host_hooks_path(home).read_text(encoding="utf-8"))
    assert data["hooks"]["sessionStart"] == [_foreign_entry()]
    assert ci._plugin_dir(home).exists() is False
    assert any("sessionStart" in (r or "") for r in result["removed"]) or len(result["removed"]) >= 1


@needs_posix
def test_dry_run_writes_nothing(home):
    home.mkdir()
    result = ci.install(dry_run=True, home=home)
    assert result["dry_run"] is True
    assert ci._host_hooks_path(home).exists() is False
    assert ci._plugin_dir(home).exists() is False


def test_windows_install_writes_shell_neutral_commands(monkeypatch, home):
    """#296 class: native Windows installs instead of refusing, and every
    command is one PowerShell and cmd.exe both parse (no VAR=value prefix,
    bare interpreter, double-quoted bridge)."""
    home.mkdir()
    monkeypatch.setattr(ci.os, "name", "nt")
    ci.install(home=home)
    config = json.loads(ci._host_hooks_path(home).read_text(encoding="utf-8"))
    commands = [e["command"] for entries in config["hooks"].values() for e in entries]
    assert commands
    bridge = str(_bridge_path(home))
    for cmd in commands:
        assert "TOKEN_OPTIMIZER_RUNTIME=" not in cmd
        assert not cmd.startswith(('"', "'", "&"))
        assert f'"{bridge}"' in cmd
    # Re-install replaces our entries instead of adding a second set.
    ci.install(home=home)
    again = json.loads(ci._host_hooks_path(home).read_text(encoding="utf-8"))
    assert sum(len(v) for v in again["hooks"].values()) == len(commands)


def test_is_ours_matches_bridge_path(home):
    bridge_path = _bridge_path(home)
    assert ci._is_ours({"command": f"python3 {bridge_path} stop"}, bridge_path) is True
    assert ci._is_ours(_foreign_entry(), bridge_path) is False
    assert ci._is_ours("not-a-dict", bridge_path) is False


@needs_posix
def test_install_writes_measure_locator(home):
    home.mkdir()
    ci.install(home=home)

    locator = ci._plugin_dir(home) / ci._MEASURE_LOCATOR_NAME
    assert locator.exists()
    target = Path(locator.read_text(encoding="utf-8").strip())
    assert target.is_file() and target.name == "measure.py"


@needs_posix
def test_install_aborts_on_corrupt_hooks_json(home):
    home.mkdir()
    hooks_path = ci._host_hooks_path(home)
    hooks_path.write_text("{ not valid json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not valid JSON"):
        ci.install(home=home)
    # The shared file must be left byte-for-byte intact, never clobbered.
    assert hooks_path.read_text(encoding="utf-8") == "{ not valid json"


@needs_posix
def test_install_aborts_on_non_dict_hooks_member(home):
    """G4: a malformed 'hooks' member must fail closed, never be replaced."""
    home.mkdir()
    hooks_path = ci._host_hooks_path(home)
    original = json.dumps({"version": 1, "hooks": ["foreign-tool-entry"]})
    hooks_path.write_text(original, encoding="utf-8")

    with pytest.raises(RuntimeError, match="non-object 'hooks' member"):
        ci.install(home=home)
    assert hooks_path.read_text(encoding="utf-8") == original


@needs_posix
def test_install_aborts_on_non_list_event(home):
    """G4: a malformed event entry must fail closed, never be replaced."""
    home.mkdir()
    hooks_path = ci._host_hooks_path(home)
    original = json.dumps({"version": 1, "hooks": {"preToolUse": {"foreign": True}}})
    hooks_path.write_text(original, encoding="utf-8")

    with pytest.raises(RuntimeError, match="not a list"):
        ci.install(home=home)
    assert hooks_path.read_text(encoding="utf-8") == original


@needs_posix
def test_install_aborts_on_non_object_hooks_root(home):
    home.mkdir()
    hooks_path = ci._host_hooks_path(home)
    hooks_path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(RuntimeError, match="non-object root"):
        ci.install(home=home)
    assert hooks_path.read_text(encoding="utf-8") == "[1, 2, 3]"


@needs_posix
def test_install_aborts_on_copy_failure_before_touching_hooks(home, monkeypatch):
    home.mkdir()
    hooks_path = ci._host_hooks_path(home)
    hooks_path.write_text(json.dumps({"version": 1, "hooks": {"stop": [_foreign_entry()]}}),
                          encoding="utf-8")

    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(ci.shutil, "copyfileobj", _boom)
    with pytest.raises(RuntimeError, match="failed copying"):
        ci.install(home=home)
    # Abort happened before the read-merge-write: foreign entries untouched.
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert data["hooks"]["stop"] == [_foreign_entry()]


@needs_posix
def test_install_aborts_on_missing_payload_module(home, monkeypatch):
    home.mkdir()
    monkeypatch.setattr(ci, "_PAYLOAD_MODULES", ("cursor_hook_bridge.py", "nonexistent.py"))
    with pytest.raises(RuntimeError, match="missing payload modules"):
        ci.install(home=home)
    assert ci._host_hooks_path(home).exists() is False


@needs_posix
def test_install_refuses_symlinked_hooks_json(tmp_path):
    """P0-3: a pre-existing hooks.json symlink must never be written through."""
    import json as _json
    target = tmp_path / "target.json"
    target.write_text(_json.dumps({"hooks": {}}), encoding="utf-8")
    cur = tmp_path / ".cursor"
    cur.mkdir()
    hooks = cur / "hooks.json"
    hooks.symlink_to(target)

    with pytest.raises(RuntimeError, match="symlink"):
        ci.install(home=cur)
    # the symlink target is untouched
    assert _json.loads(target.read_text(encoding="utf-8")) == {"hooks": {}}


def test_atomic_write_replace_symlink_replaces_link_not_target(tmp_path):
    """codex_io.atomic_write(replace_symlink=True) swaps the symlink itself."""
    from codex_io import atomic_write
    target = tmp_path / "target.json"
    target.write_text("old", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    atomic_write(link, "new", replace_symlink=True)
    assert link.is_symlink() is False
    assert target.read_text(encoding="utf-8") == "old"


def test_atomic_write_default_still_follows_symlink(tmp_path):
    """Default behavior unchanged for dotfile-managed config callers."""
    from codex_io import atomic_write
    target = tmp_path / "target.json"
    target.write_text("old", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    atomic_write(link, "new")
    assert link.is_symlink() is True
    assert target.read_text(encoding="utf-8") == "new"


@needs_posix
def test_concurrent_installs_both_survive(tmp_path, monkeypatch):
    """P0-3: two racing installs must not silently drop each other's entries.
    With the lease, the loser aborts loudly instead of clobbering."""
    import threading
    cur = tmp_path / ".cursor"
    cur.mkdir()
    monkeypatch.setattr(ci, "cursor_home", lambda: cur)
    # minimal payload so install() gets to the hooks RMW
    plugin = cur / "token-optimizer" / "plugin"
    plugin.mkdir(parents=True)
    for name in ci._PAYLOAD_MODULES:
        (plugin / name).write_text("# stub\n", encoding="utf-8")

    errors, done = [], []
    def run():
        try:
            ci.install(home=cur)
            done.append(1)
        except RuntimeError as exc:
            errors.append(exc)

    t1, t2 = threading.Thread(target=run), threading.Thread(target=run)
    t1.start(); t2.start(); t1.join(); t2.join()

    # Every racer either completed (serialized by the lease) or aborted loudly;
    # nobody vanished. hooks.json is valid JSON with our six events intact.
    assert len(done) + len(errors) == 2
    import json as _json
    data = _json.loads((cur / "hooks.json").read_text(encoding="utf-8"))
    assert set(data["hooks"]) == {"sessionStart", "preToolUse", "postToolUse",
                                  "preCompact", "stop", "sessionEnd",
                                  "afterAgentResponse", "postToolUseFailure",
                                  "afterShellExecution", "subagentStart", "subagentStop"}


@needs_posix
def test_install_aborts_loudly_when_lease_held(home, monkeypatch):
    """P0-3 (deterministic): with the install lease held by a concurrent
    installer, install() raises the loud RuntimeError instead of racing the
    read-merge-write."""
    from hook_runtime import lease_lock
    home.mkdir(parents=True)
    monkeypatch.setattr(ci, "cursor_home", lambda: home)
    lock_path = home / ".hooks.json.to-install.lock"
    with lease_lock(lock_path, acquire_timeout=0.0, lease_seconds=120.0) as acquired:
        assert acquired
        with pytest.raises(RuntimeError, match="another Cursor install"):
            ci.install(home=home)


def test_payload_modules_ship_the_bridges_runtime_deps():
    """P1-1: the bridge imports spawn_utils (spawn_detached), hook_runtime
    (lease_lock) and utf8_io; every one absent from the payload means a silent
    degraded fallback in EVERY installed hook."""
    for required in ("spawn_utils.py", "hook_runtime.py", "utf8_io.py",
                     "codex_io.py", "hermes_session.py", "cursor_quality.py"):
        assert required in ci._PAYLOAD_MODULES, required
        assert (SCRIPTS / required).is_file(), f"{required} missing from checkout"


@needs_posix
def test_install_copies_full_payload(home, monkeypatch):
    monkeypatch.setattr(ci, "cursor_home", lambda: home)
    ci.install(home=home)
    plugin = home / "token-optimizer" / "plugin"
    for name in ci._PAYLOAD_MODULES:
        assert (plugin / name).is_file(), name


@needs_posix
def test_install_replaces_symlinked_payload_dest_without_following(home, monkeypatch):
    """P1-5: a pre-planted symlink at a payload destination is swapped by
    os.replace, never written through to the link's target."""
    plugin = home / "token-optimizer" / "plugin"
    plugin.mkdir(parents=True)
    victim = home / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    (plugin / "codex_io.py").symlink_to(victim)
    monkeypatch.setattr(ci, "cursor_home", lambda: home)
    ci.install(home=home)
    assert victim.read_text(encoding="utf-8") == "keep"
    assert (plugin / "codex_io.py").is_symlink() is False
    assert (plugin / "codex_io.py").is_file()


@needs_posix
def test_install_replaces_symlinked_measure_locator_without_following(home, monkeypatch):
    plugin = home / "token-optimizer" / "plugin"
    plugin.mkdir(parents=True)
    victim = home / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    (plugin / ci._MEASURE_LOCATOR_NAME).symlink_to(victim)
    monkeypatch.setattr(ci, "cursor_home", lambda: home)
    ci.install(home=home)
    assert victim.read_text(encoding="utf-8") == "keep"
    assert (plugin / ci._MEASURE_LOCATOR_NAME).is_symlink() is False


def test_locate_measure_py_rejects_crafted_locator(tmp_path, monkeypatch):
    """P1-5: the bridge must not spawn an arbitrary file named by a crafted
    measure-path locator; only a regular, non-symlink measure.py qualifies."""
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    evil = tmp_path / "evil.py"
    evil.write_text("pwn", encoding="utf-8")
    (plugin / "measure-path").write_text(f"{evil}\n", encoding="utf-8")
    monkeypatch.setattr(bridge_mod, "_SCRIPT_DIR", plugin)
    monkeypatch.setattr(bridge_mod, "_MEASURE_LOCATOR", plugin / "measure-path")
    assert bridge_mod._locate_measure_py() is None
    # a locator naming a real measure.py still resolves
    good = tmp_path / "checkout" / "measure.py"
    good.parent.mkdir()
    good.write_text("ok", encoding="utf-8")
    (plugin / "measure-path").write_text(f"{good}\n", encoding="utf-8")
    assert bridge_mod._locate_measure_py() == good


def test_install_sh_cursor_materializes_from_pinned_commit():
    """P1-7: install_cursor must not `git pull` (moving branch => arbitrary
    code into the plugin dir); it materializes skills/ from the pinned HEAD
    via the shared pinned-materialization helper."""
    sh = (REPO / "install.sh").read_text(encoding="utf-8")
    cursor_fn = sh[sh.index("install_cursor() {"):]
    cursor_fn = cursor_fn[:cursor_fn.index("\n}\n")]
    assert "pull --ff-only" not in cursor_fn
    # The pin lives in the shared helper; the adapter path must call it.
    assert "_materialize_from_pin" in cursor_fn
    helper = sh[sh.index("_materialize_from_pin() {"):]
    helper = helper[:helper.index("\n}\n")]
    assert 'rev-parse HEAD' in helper
    assert 'checkout "$pin_sha"' in helper


@needs_posix
def test_uninstall_rmtree_failure_is_runtime_error(home, monkeypatch):
    """P1-12: an OSError from shutil.rmtree becomes a user-facing RuntimeError,
    not a raw traceback (main() only catches RuntimeError)."""
    import json as _json
    plugin = home / "token-optimizer" / "plugin"
    plugin.mkdir(parents=True)
    (home / "hooks.json").write_text(_json.dumps({"hooks": {}}), encoding="utf-8")
    for name in ci._PAYLOAD_MODULES:
        (plugin / name).write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(ci, "cursor_home", lambda: home)

    def boom(_p, *a, **k):
        raise OSError(13, "Permission denied")
    monkeypatch.setattr(ci.shutil, "rmtree", boom)
    with pytest.raises(RuntimeError, match="failed removing"):
        ci.uninstall(home=home)


_FILTERS_PAYLOAD_PROBE = r'''
import os, sys, importlib.abc
class _NoStdlibToml(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "tomllib":
            raise ImportError("simulated Python 3.9/3.10")
sys.meta_path.insert(0, _NoStdlibToml())
sys.path.insert(0, sys.argv[1])
os.environ["TOKEN_OPTIMIZER_COMMAND_FILTERS"] = sys.argv[2]
import command_filters
print(command_filters.load_filters().excludes)
'''


@needs_posix
def test_payload_parses_command_filters_without_stdlib_tomllib(home, tmp_path):
    """command_filters + _tomli must ship in the payload: on Python 3.9/3.10
    there is no tomllib, so the vendored _tomli module is its only TOML parser.
    Without them the lazy import fails and user filters silently never apply;
    a malformed filters file must still fail open with an empty config."""
    import subprocess
    ci.install(home=home)
    pdir = ci._plugin_dir(home)
    good = tmp_path / "command-filters.toml"
    good.write_text('[filters.exclude]\ncommands = ["git log"]\n')
    bad = tmp_path / "bad-filters.toml"
    bad.write_text("[[broken\n")
    py = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
    for path, want in ((good, "('git log',)"), (bad, "()")):
        out = subprocess.run(
            [py, "-c", _FILTERS_PAYLOAD_PROBE, str(pdir), str(path)],
            capture_output=True, text=True, cwd=str(tmp_path), timeout=60,
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == want, out.stdout + out.stderr


def test_uninstall_removes_derived_caches_but_keeps_history(home):
    for name in ("read-fingerprints", "checkpoints", "restore-context", "snapshot/checkpoints", "sessions"):
        directory = home / "token-optimizer" / name
        directory.mkdir(parents=True)
        (directory / "data").write_text("data")
    result = ci.uninstall(home=home)
    for name in ("read-fingerprints", "checkpoints", "restore-context", "snapshot/checkpoints"):
        assert not (home / "token-optimizer" / name).exists()
    assert (home / "token-optimizer" / "sessions" / "data").is_file()
    assert result["kept"]


def test_payload_imports_and_read_interception_are_self_contained(tmp_path):
    import shutil
    import subprocess
    payload = tmp_path / "payload"
    payload.mkdir()
    for name in ci._PAYLOAD_MODULES:
        shutil.copy2(SCRIPTS / name, payload / name)
    target = tmp_path / "sample.py"
    target.write_text("def sample():\n    return 1\n")
    program = '''import importlib, pathlib, sys
payload, target, home, names = sys.argv[1:]
sys.path.insert(0, payload)
for name in names.split(","):
    importlib.import_module(name[:-3])
import cursor_read_cache as cache
cache._home = lambda: pathlib.Path(home)
result = cache.evaluate({"tool_name": "Read", "tool_args": {"file_path": target},
                         "conversation_id": "session-123", "agent_id": "parent", "tool_use_id": "call-1", "workspace_roots": []})
assert result and result["observed"]["decision"] == "first_read", result
fields = {"tool_name": "Read", "tool_args": {"file_path": target},
          "conversation_id": "session-123", "agent_id": "parent", "tool_use_id": "call-1", "workspace_roots": []}
cache.confirm_read(fields)
assert cache.evaluate(fields)["observed"]["decision"] == "shadow_redundant"
'''
    result = subprocess.run([sys.executable, "-I", "-c", program, str(payload), str(target),
                             str(tmp_path), ",".join(ci._PAYLOAD_MODULES)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_explicit_relative_home_becomes_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = ci.install(dry_run=True, home=Path('relative-cursor'))
    assert Path(result['hook_file']).is_absolute()
