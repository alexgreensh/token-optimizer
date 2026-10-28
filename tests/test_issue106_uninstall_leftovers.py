"""Issue #106 guard: count what is really left on disk after cleanup.

#106 reported ~850 files and dangling config left after an uninstall. The older
guards (test_cleanup_command, test_uninstall_manifest_reconcile,
test_setup_hook_uninstall) check named entries one by one; none of them walks
the disk, so a new artifact the remover does not know about slipped through.

Two guards:

1. End to end, real CLI: a script install (the skill tree install.sh puts in
   ~/.claude/skills) in a sandbox HOME, every opt-in component installed with
   its own setup command, every installed hook run once, then
   ``measure.py cleanup --confirm``. settings.json must come back to exactly
   the user's own content, cleanup must add nothing but its settings backup,
   and every Token Optimizer file still on disk must sit under a root that
   docs/uninstall.md names (and the dry-run disclosure lists the data roots).

2. In process, plugin layout: every per-identity daemon artifact any platform
   writes, in two identities, plus the LaunchAgent. After cleanup the whole
   plugin-data tree must hold exactly the retained data plus the uninstall
   tombstone, on whatever OS runs the test.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402
import plugin_env  # noqa: E402

DOCS = (REPO / "docs" / "uninstall.md").read_text(encoding="utf-8")

USER_SETTINGS = {
    "theme": "dark",
    "env": {"MY_KEY": "keep-me"},
    "permissions": {"allow": ["Bash(ls:*)"]},
    "compactInstructions": "Keep the migration plan and the open questions.",
    "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "echo user-stop"}]}],
        "SessionEnd": [{"hooks": [{"type": "command", "command": "echo user-end"}]}],
    },
}

SID = "11111111-2222-3333-4444-555555555555"

# Roots Token Optimizer may leave behind after cleanup, relative to HOME. Each
# must be named in docs/uninstall.md (as ~/<root>) so nothing stays silently.
DOCUMENTED_ROOTS = (
    ".claude/skills/token-optimizer",
    ".claude/token-optimizer",
    ".claude/_backups/token-optimizer",
    ".cache/token-optimizer",
    ".claude/.settings.lease",
)
USER_OWNED = (".claude/settings.json", ".claude/projects/", "tmp/")


def _env(home: Path) -> dict:
    py_dir = str(Path(sys.executable).parent)
    env = {
        "HOME": str(home),
        # Pinned explicitly (host-safety guard): settings resolve inside the sandbox.
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "PATH": os.pathsep.join([py_dir, "/usr/bin", "/bin"]),
        "TMPDIR": str(home / "tmp"),
        "LANG": "en_US.UTF-8",
        "PYTHONUTF8": "1",
    }
    return env


def _files(home: Path) -> set[str]:
    out = set()
    for p in home.rglob("*"):
        if p.is_dir() and not p.is_symlink():
            continue
        rel = p.relative_to(home).as_posix()
        if rel.startswith(".claude/skills/token-optimizer/"):
            continue  # the copied skill tree itself; checked as a root below
        out.add(rel)
    return out


def _run(home: Path, args, stdin=None, shell=False):
    cmd = args if shell else [sys.executable, str(home / ".claude/skills/token-optimizer/scripts/measure.py"), *args]
    return subprocess.run(
        ["bash", "-c", cmd] if shell else cmd,
        input=stdin, capture_output=True, text=True, env=_env(home), timeout=240,
        cwd=str(home),
    )


def _write_transcript(home: Path) -> Path:
    proj = home / ".claude" / "projects" / "-work-proj"
    proj.mkdir(parents=True)
    lines = []
    for i in range(6):
        lines.append({"type": "user", "sessionId": SID, "cwd": "/work/proj",
                      "timestamp": f"2026-10-28T01:0{i}:00Z",
                      "message": {"role": "user", "content": f"do thing {i}"}})
        lines.append({"type": "assistant", "sessionId": SID, "cwd": "/work/proj",
                      "timestamp": f"2026-10-28T01:0{i}:30Z", "requestId": f"r{i}",
                      "message": {"id": f"m{i}", "model": "claude-sonnet-4-5",
                                  "usage": {"input_tokens": 100, "output_tokens": 50,
                                            "cache_read_input_tokens": 2000,
                                            "cache_creation_input_tokens": 100},
                                  "content": [{"type": "text", "text": "ok"}]}})
    path = proj / f"{SID}.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


@pytest.mark.skipif(sys.platform == "win32", reason="installed hook commands are POSIX shell")
def test_script_install_cleanup_leaves_only_documented_files(tmp_path):
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True)
    shutil.copytree(REPO / "skills" / "token-optimizer",
                    home / ".claude" / "skills" / "token-optimizer",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    settings_path = home / ".claude" / "settings.json"
    settings_path.write_text(json.dumps(USER_SETTINGS, indent=2), encoding="utf-8")
    transcript = _write_transcript(home)

    # Install every opt-in component through its own command, back to back.
    for verb in ("setup-quality-bar", "setup-smart-compact", "setup-hook"):
        proc = _run(home, [verb])
        assert proc.returncode == 0, (verb, proc.stdout, proc.stderr)
        assert "refused" not in proc.stderr, (verb, proc.stderr)
    installed = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "statusLine" in installed
    assert "# Token Optimizer additions:" in installed["compactInstructions"]
    events = {e for e, groups in installed["hooks"].items()
              for g in groups for h in g["hooks"] if "measure.py" in h["command"]}
    assert {"UserPromptSubmit", "PreCompact", "SessionStart", "Stop", "SessionEnd"} <= events

    # Run every installed Token Optimizer hook once, so the usual data files exist.
    for event, groups in installed["hooks"].items():
        for group in groups:
            for hook in group["hooks"]:
                if "measure.py" not in hook["command"]:
                    continue
                payload = json.dumps({
                    "session_id": SID, "transcript_path": str(transcript),
                    "cwd": "/work/proj", "hook_event_name": event, "prompt": "hi",
                    "source": "startup", "trigger": "auto", "reason": "other",
                })
                _run(home, hook["command"], stdin=payload, shell=True)
    data_files = _files(home)
    assert any("/checkpoints/" in f for f in data_files), sorted(data_files)
    assert any(f.endswith("trends.db") for f in data_files), sorted(data_files)

    # Re-running compact-instructions must refresh our part and keep the user's.
    proc = _run(home, ["compact-instructions", "--install"])
    assert proc.returncode == 0, proc.stderr
    refreshed = json.loads(settings_path.read_text(encoding="utf-8"))["compactInstructions"]
    assert refreshed.startswith(USER_SETTINGS["compactInstructions"])
    assert refreshed.count("# Token Optimizer additions:") == 1

    # Then a hook-style write from another process (not user-initiated), which
    # leaves the ~10s herd reservation on the lease, exactly as a SessionStart
    # heal does. Cleanup runs straight after it.
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import measure; "
        "s, ok = measure._read_settings_for_write(); assert ok; "
        "sys.exit(0 if measure._write_settings_atomic(s) else 3)"
    )
    hook_write = subprocess.run(
        [sys.executable, "-c", code, str(home / ".claude/skills/token-optimizer/scripts")],
        capture_output=True, text=True, env=_env(home), cwd=str(home), timeout=120)
    assert hook_write.returncode == 0, hook_write.stderr

    before = _files(home)
    proc = _run(home, ["cleanup", "--confirm"])
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    after = _files(home)

    # settings.json is exactly the user's own again: nothing of ours left.
    final_text = settings_path.read_text(encoding="utf-8")
    assert json.loads(final_text) == USER_SETTINGS, final_text
    for marker in ("measure.py", "statusline.js", "Token Optimizer", "token-optimizer"):
        assert marker not in final_text, marker

    # Cleanup adds only its settings backup, and deletes no retained data.
    added = after - before
    assert len(added) == 1, sorted(added)
    (backup,) = added
    assert backup.startswith(".claude/_backups/token-optimizer/") and backup.endswith("/settings.json")
    assert before - after == set(), sorted(before - after)

    # Every Token Optimizer file still on disk is under a root the docs name.
    for rel in after:
        if rel.startswith(USER_OWNED):
            continue
        assert rel.startswith(DOCUMENTED_ROOTS), f"undocumented leftover: {rel}"
    for root in DOCUMENTED_ROOTS:
        assert f"~/{root}" in DOCS, f"docs/uninstall.md does not name ~/{root}"

    # The disclosure names the data roots that stay (session data + checkpoints).
    out = proc.stdout
    assert "Retained by design" in out
    assert str(home / ".claude" / "_backups" / "token-optimizer") in out
    assert str(home / ".claude" / "token-optimizer" / "checkpoints") in out


# ---------------------------------------------------------------------------
# Plugin layout: daemon artifacts of every platform, exact leftover set
# ---------------------------------------------------------------------------

class _Completed:
    returncode = 1
    stdout = ""
    stderr = "not found"


RETAINED = ("trends.db", "dashboard.html", "checkpoints/s1.md", "checkpoints/s1.json",
            "quality-cache-s1.json")


def test_plugin_cleanup_leaves_exactly_retained_data(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(measure, "_HOME_REDIRECTED", False)
    home = tmp_path / "home"
    plugins_dir = home / "plugins"
    data_base = plugins_dir / "data"
    snaps = [data_base / name / "data" for name in
             ("token-optimizer-alexgreensh-token-optimizer", "token-optimizer-inline")]
    planted = {}
    for snap in snaps:
        snap.mkdir(parents=True)
        for rel in RETAINED:
            (snap / rel).parent.mkdir(parents=True, exist_ok=True)
            (snap / rel).write_text("x", encoding="utf-8")
        # Every per-identity daemon artifact any platform's installer writes.
        artifacts = list(measure._daemon_per_identity_files(snap).values())
        artifacts.append(snap / ".schtasks-daemon.xml")
        for path in artifacts:
            path.write_text("daemon", encoding="utf-8")
        planted[snap] = artifacts

    monkeypatch.setattr(plugin_env, "_PLUGIN_DATA_BASE", data_base)
    monkeypatch.setattr(plugin_env, "_INSTALLED_PLUGINS", plugins_dir / "installed_plugins.json")
    monkeypatch.setattr(plugin_env, "_PLUGIN_DATA_ENV_VARS", ("CLAUDE_PLUGIN_DATA",))
    plugin_env.resolve_plugin_data_dir.cache_clear()
    monkeypatch.setattr(measure, "_all_plugin_data_dirs", plugin_env._all_plugin_data_dirs)
    snap_a = snaps[0]
    monkeypatch.setattr(measure, "SNAPSHOT_DIR", snap_a)
    monkeypatch.setattr(measure, "CHECKPOINT_DIR", snap_a / "checkpoints")
    monkeypatch.setattr(measure, "DAEMON_TOKEN_PATH", snap_a / "daemon-token")
    monkeypatch.setattr(measure, "DAEMON_HOST_PATH", snap_a / "dashboard-host")
    monkeypatch.setattr(measure, "DAEMON_THRASH_BREADCRUMB", snap_a / ".daemon-thrash")
    monkeypatch.setattr(measure, "DAEMON_LOG_DIR", snap_a / "logs")
    monkeypatch.setattr(measure, "CLAUDE_DIR", home)
    monkeypatch.setattr(measure, "SETTINGS_PATH", home / "settings.json")
    monkeypatch.setattr(measure, "_SETTINGS_LOCK_PATH", home / ".settings.lock")
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    monkeypatch.setattr(measure, "LAUNCH_AGENTS_DIR", launch_agents)
    monkeypatch.setattr(measure, "PLIST_PATH", launch_agents / "com.token-optimizer.dashboard.plist")
    measure.PLIST_PATH.write_text("<plist/>", encoding="utf-8")
    monkeypatch.setattr(measure.subprocess, "run", lambda *a, **k: _Completed())

    settings = dict(USER_SETTINGS)
    settings["statusLine"] = {"command": f"node '{SCRIPTS / 'statusline.js'}'"}
    settings["hooks"] = dict(USER_SETTINGS["hooks"], PreCompact=[{"hooks": [{
        "type": "command",
        "command": f"python3 '{SCRIPTS / 'measure.py'}' compact-capture --trigger auto"}]}])
    measure.SETTINGS_PATH.write_text(json.dumps(settings), encoding="utf-8")

    measure.cleanup(dry_run=False, this_install_only=False)
    capsys.readouterr()

    expected = set()
    for snap in snaps:
        rel_base = snap.relative_to(data_base).as_posix()
        expected |= {f"{rel_base}/{r}" for r in RETAINED}
        expected.add(f"{rel_base}/.daemon-thrash")  # uninstall tombstone, kept by design
    left = {p.relative_to(data_base).as_posix() for p in data_base.rglob("*") if p.is_file()}
    assert left == expected, f"leftovers: {sorted(left - expected)}; lost: {sorted(expected - left)}"
    assert not measure.PLIST_PATH.exists()
    assert list(launch_agents.iterdir()) == []
    assert json.loads(measure.SETTINGS_PATH.read_text(encoding="utf-8")) == USER_SETTINGS


# ---------------------------------------------------------------------------
# compactInstructions ownership (setup-smart-compact writes it)
# ---------------------------------------------------------------------------

GENERATED = "\n".join([
    measure._COMPACT_INSTR_HEAD,
    "- Modified file paths with line ranges",
    "- Open questions and unresolved TODOs",
    measure._COMPACT_INSTR_TAIL,
])


def test_generated_compact_instructions_are_removed_whole():
    s = {"compactInstructions": GENERATED, "theme": "dark"}
    assert measure._remove_our_compact_instructions(s) is True
    assert s == {"theme": "dark"}


def test_appended_section_is_removed_and_user_text_kept():
    s = {"compactInstructions": "Mine." + measure._COMPACT_INSTR_MARKER + GENERATED}
    assert measure._remove_our_compact_instructions(s) is True
    assert s == {"compactInstructions": "Mine."}


@pytest.mark.parametrize("text", [
    "keep the plan",
    GENERATED + "\n- my own extra rule",
    measure._COMPACT_INSTR_HEAD + "\n- something I wrote\n" + measure._COMPACT_INSTR_TAIL,
])
def test_user_compact_instructions_are_never_touched(text):
    s = {"compactInstructions": text}
    assert measure._remove_our_compact_instructions(s) is False
    assert s == {"compactInstructions": text}
