#!/usr/bin/env python3
"""Suite-wide guard: no test may run a host-mutating verb against the real HOME.

Incident 2026-07-30. ``tests/test_cleanup_write_and_confirm.py`` spawned
``measure.py cleanup --confirm`` with only ``TOKEN_OPTIMIZER_SNAPSHOT_DIR``
pinned. That env var controls the DATA dir only; ``settings.json`` resolves
through ``CLAUDE_DIR = claude_home()``, which is independent of it. The
subprocess therefore operated on the developer's real ``~/.claude`` and
removed a live ``statusLine``. Running the full suite re-fired it every time.

The class of bug: a destructive CLI verb invoked as a subprocess whose config
root was never pinned. This test makes that class fail at collection time
instead of on someone's machine.

Rule enforced: any test file that spawns a subprocess with a destructive verb
(``cleanup``, ``setup-*``, ``*-uninstall``, ``*-install`` for codex/copilot/
hermes, ``keepwarm-scheduler``, ``--uninstall``) must set BOTH
``CLAUDE_CONFIG_DIR`` and ``HOME`` somewhere in the file. ``claude_home()``
honors ``CLAUDE_CONFIG_DIR`` for any absolute, existing, non-symlink dir, so
pinning it redirects settings.json into the fixture. ``HOME`` matters
independently: ``LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"``
(measure.py) and the systemd/schtasks unit paths derive from ``HOME``, NOT
from ``CLAUDE_CONFIG_DIR``. A scheduler-installing verb (``setup-daemon``,
``keepwarm-scheduler install``) writes a launchd/systemd/schtasks unit under
``HOME``; a codex/copilot/hermes ``*-install`` writes that runtime's host
config under ``HOME``. Pinning ``CLAUDE_CONFIG_DIR`` alone leaves the real
``~/Library/LaunchAgents`` (and ``~/.codex`` etc.) exposed, which is exactly
the class the 2026-07-30 incident fix closed by pinning BOTH.

This is a lint, not a proof -- a file can pin the var and still misuse it. The
per-file tests own the proof (see ``test_confirm_proceeds``, which asserts the
FIXTURE settings.json is what changed). This guard exists so a NEW destructive
test cannot land with no isolation at all.

Run: python3 -m pytest tests/test_host_safety_guard.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# Verbs and hook entrypoints that write host state: settings.json,
# launchd/systemd/schtasks units,
# a runtime's host config dir (~/.codex etc.), or the plugin manifests. Matched
# as CLI arguments, not as prose in a docstring.
#   - cleanup / setup-* / *-uninstall / --uninstall : original set
#   - *-install : codex-install / copilot-install / hermes-install write a
#     sibling runtime's HOME-derived host config
#   - keepwarm-scheduler : installs/removes a launchd/systemd/schtasks unit
#     under HOME (keepwarm-scheduler install|uninstall)
#   - ensure-health / consolidated prompt runners: dispatch daemon-revive,
#     which can install or restart the OS scheduler artifact indirectly
_DESTRUCTIVE = re.compile(
    r"""["'](?:cleanup|ensure-health|setup-[a-z-]+|[a-z-]+-uninstall|[a-z-]+-install|keepwarm-scheduler)["']|["']--uninstall["']|["']hooks/(?:sessionstart|userpromptsubmit)_runner\.py["']"""
)
_SPAWNS = re.compile(r"subprocess\.(?:run|Popen|check_output|call)")
_PINS_CONFIG = "CLAUDE_CONFIG_DIR"
# HOME must be pinned too: launchd/systemd/schtasks unit paths and runtime
# host-config dirs derive from Path.home(), independent of CLAUDE_CONFIG_DIR.
# Matched as a quoted env-dict key so prose/comments mentioning HOME do not
# satisfy it.
_PINS_HOME = re.compile(r"""["']HOME["']""")

# This guard file itself names the verbs in prose/regex; exempt by name.
_EXEMPT = {
    "test_host_safety_guard.py",
    # These subprocesses import a path probe or a generated stub. They never
    # execute the real hook command whose name also appears in the file.
    "test_cowork_hardening.py",
    "test_hook_entry_budgets.py",
    # Spawns pwsh/cmd.exe/bash only against fake fixture runners under
    # tmp_path. Runner names like 'hooks/sessionstart_runner.py' appear as
    # string args to _hook_command (command GENERATION), never executed.
    "test_windows_hook_launcher.py",
}


def _offenders() -> list[str]:
    bad = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        if path.name in _EXEMPT:
            continue
        try:
            src = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not _SPAWNS.search(src):
            continue
        if not _DESTRUCTIVE.search(src):
            continue
        if _PINS_CONFIG not in src or not _PINS_HOME.search(src):
            bad.append(path.name)
    return bad


def test_no_destructive_subprocess_without_pinned_config_dir():
    offenders = _offenders()
    assert not offenders, (
        "These test files spawn a host-mutating measure.py verb without pinning "
        "BOTH CLAUDE_CONFIG_DIR and HOME, so the subprocess resolves "
        "settings.json from the REAL ~/.claude and/or writes a launchd/systemd/"
        "schtasks unit into the REAL ~/Library/LaunchAgents and can damage a "
        f"developer's machine (incident 2026-07-30): {offenders}\n"
        "Fix: set CLAUDE_CONFIG_DIR (absolute, existing, non-symlink) AND HOME "
        "to a tmp_path fixture in the subprocess env, and assert the FIXTURE "
        "file is the one that changed."
    )


def test_guard_detects_a_planted_offender(tmp_path, monkeypatch):
    """Negative test: the guard must actually catch an unisolated file."""
    planted = tmp_path / "test_planted_offender.py"
    planted.write_text(
        "import subprocess, sys\n"
        "def test_x():\n"
        "    subprocess.run([sys.executable, 'measure.py', 'cleanup', '--confirm'])\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(__import__("sys").modules[__name__], "TESTS_DIR", tmp_path)

    assert _offenders() == ["test_planted_offender.py"]


def test_guard_accepts_a_pinned_file(tmp_path, monkeypatch):
    ok = tmp_path / "test_pinned.py"
    ok.write_text(
        "import subprocess, sys\n"
        "def test_x(tmp_path):\n"
        "    env = {'CLAUDE_CONFIG_DIR': str(tmp_path), 'HOME': str(tmp_path)}\n"
        "    subprocess.run([sys.executable, 'measure.py', 'cleanup', '--confirm'], env=env)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(__import__("sys").modules[__name__], "TESTS_DIR", tmp_path)

    assert _offenders() == []


def test_guard_requires_home_not_just_config_dir(tmp_path, monkeypatch):
    """A file that pins CLAUDE_CONFIG_DIR but not HOME is still an offender:
    setup-daemon / keepwarm-scheduler write a launchd unit under Path.home()."""
    ccd_only = tmp_path / "test_ccd_only.py"
    ccd_only.write_text(
        "import subprocess, sys\n"
        "def test_x(tmp_path):\n"
        "    env = {'CLAUDE_CONFIG_DIR': str(tmp_path)}\n"
        "    subprocess.run([sys.executable, 'measure.py', 'setup-daemon'], env=env)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(__import__("sys").modules[__name__], "TESTS_DIR", tmp_path)

    assert _offenders() == ["test_ccd_only.py"]


def test_guard_catches_scheduler_and_runtime_install_verbs(tmp_path, monkeypatch):
    """The verbs the guard exists to catch: a launchd-installing scheduler verb
    and a sibling-runtime host-config install, both unisolated."""
    for verb in ("keepwarm-scheduler", "codex-install", "copilot-install", "hermes-install"):
        planted = tmp_path / "test_verb.py"
        planted.write_text(
            "import subprocess, sys\n"
            "def test_x():\n"
            f"    subprocess.run([sys.executable, 'measure.py', '{verb}', 'install'])\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(__import__("sys").modules[__name__], "TESTS_DIR", tmp_path)
        assert _offenders() == ["test_verb.py"], verb


def test_guard_catches_ensure_health_and_consolidated_runners(tmp_path, monkeypatch):
    """Session hooks can register the daemon indirectly via daemon-revive."""
    cases = (
        "'measure.py', 'ensure-health'",
        "'hooks/sessionstart_runner.py'",
        "'hooks/userpromptsubmit_runner.py'",
    )
    for argv in cases:
        planted = tmp_path / "test_hook_entry.py"
        planted.write_text(
            "import subprocess, sys\n"
            "def test_x():\n"
            f"    subprocess.run([sys.executable, {argv}])\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(__import__("sys").modules[__name__], "TESTS_DIR", tmp_path)
        assert _offenders() == ["test_hook_entry.py"], argv
