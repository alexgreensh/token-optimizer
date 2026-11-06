"""Every shipped SessionEnd command must use session-end-flush.

The collect --quiet && dashboard --quiet shape runs the heavy flush inline
with no budget and wedges Windows stop-hooks at 3/4. Both prior fixes
only covered the session-end-flush argv path; this test would have caught
the still-shipped HOOK_COMMAND / hooks-starter.json fossil.
"""

from __future__ import annotations
from helpers.measure_split import measure_source as _split_measure_source

import ast
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
MEASURE = SCRIPTS / "measure.py"

_FOSSIL_CHAIN = re.compile(r"collect\s+--quiet\s+&&")
_DASHBOARD_CHAIN = re.compile(r"dashboard\s+--quiet")


def _session_end_commands_from_hooks_json(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cmds = []
    for group in data.get("hooks", {}).get("SessionEnd", []) or []:
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks", []) or []:
            if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                cmds.append(hook["command"])
    return cmds


def _stop_commands_from_hooks_json(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cmds = []
    for group in data.get("hooks", {}).get("Stop", []) or []:
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks", []) or []:
            if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                cmds.append(hook["command"])
    return cmds


def _commands_from_hooks_json(path: Path, events: tuple[str, ...]) -> list[tuple[str, str]]:
    """Return (event, command) pairs for the given hook events in a hooks.json."""
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for event in events:
        for group in data.get("hooks", {}).get(event, []) or []:
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks", []) or []:
                if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                    out.append((event, hook["command"]))
    return out


def _load_win32_hook_command() -> str:
    tree = ast.parse(_split_measure_source(MEASURE))
    node = None
    for candidate in tree.body:
        if not isinstance(candidate, ast.If):
            continue
        for stmt in ast.walk(candidate):
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "HOOK_COMMAND" for t in stmt.targets
            ):
                node = candidate
                break
        if node is not None:
            break
    assert node is not None, "module-level HOOK_COMMAND assignment not found"
    namespace = {
        "sys": SimpleNamespace(
            platform="win32",
            executable="C:\\Python313\\python.exe",
        ),
        "shlex": __import__("shlex"),
        "Path": Path,
        "MEASURE_PY_PATH": Path("C:/Users/Test User/.claude/token-optimizer/scripts/measure.py"),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(MEASURE), "exec"), namespace)
    return namespace["HOOK_COMMAND"]


def _load_posix_hook_command() -> str:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import measure
    if sys.platform == "win32":
        pytest.skip("POSIX HOOK_COMMAND branch is the else of sys.platform == win32")
    return measure.HOOK_COMMAND


def _assert_current_shape(label: str, cmd: str) -> None:
    # The consolidated stop_runner.py dispatcher internally calls
    # session-end-flush; the hooks.json command points at the runner, not at
    # measure.py session-end-flush directly. Accept either shape.
    assert "session-end-flush" in cmd or "stop_runner.py" in cmd, (
        f"{label} must invoke session-end-flush (directly or via stop_runner.py): {cmd!r}"
    )
    assert _FOSSIL_CHAIN.search(cmd) is None, (
        f"{label} still ships the collect --quiet && fossil: {cmd!r}"
    )
    assert "dashboard --quiet" not in cmd, (
        f"{label} still ships a dashboard chain: {cmd!r}"
    )
    assert _DASHBOARD_CHAIN.search(cmd) is None or "session-end-flush" in cmd


def _assert_stop_shape(label: str, cmd: str) -> None:
    """Assert a Stop-event command carries no fossil and uses the flush shape.

    The consolidated stop_runner.py dispatcher internally calls
    compact-capture --trigger stop, session-end-flush --trigger stop --defer,
    and keepwarm-arm; the hooks.json command points at the runner. The
    fossil is the ``collect --quiet && dashboard --quiet`` chain, which must
    never appear on Stop (it is the inline heavy flush that wedges Windows
    stop-hooks at 3/4). A direct session-end-flush hook on Stop must use
    ``--trigger stop``.
    """
    assert _FOSSIL_CHAIN.search(cmd) is None, (
        f"{label} Stop ships the collect --quiet && fossil: {cmd!r}"
    )
    assert "dashboard --quiet" not in cmd, (
        f"{label} Stop ships a dashboard chain: {cmd!r}"
    )
    if "session-end-flush" in cmd and "stop_runner.py" not in cmd:
        assert "--trigger stop" in cmd, (
            f"{label} Stop flush must use --trigger stop, not the end/legacy shape: {cmd!r}"
        )
        assert _DASHBOARD_CHAIN.search(cmd) is None


def test_root_hooks_json_sessionend_is_flush_not_collect():
    cmds = _session_end_commands_from_hooks_json(REPO / "hooks" / "hooks.json")
    assert cmds, "root hooks/hooks.json must ship a SessionEnd hook"
    for i, cmd in enumerate(cmds):
        _assert_current_shape(f"root hooks.json SessionEnd[{i}]", cmd)


def test_codex_mirror_hooks_json_sessionend_is_flush_not_collect():
    cmds = _session_end_commands_from_hooks_json(
        REPO / "plugins" / "token-optimizer" / "hooks" / "hooks.json"
    )
    assert cmds, "Codex mirror must ship a SessionEnd hook"
    for i, cmd in enumerate(cmds):
        _assert_current_shape(f"codex-mirror hooks.json SessionEnd[{i}]", cmd)


def test_cowork_hooks_json_sessionend_is_flush_if_present():
    path = REPO / "cowork" / "token-optimizer" / "hooks" / "hooks.json"
    if not path.is_file():
        pytest.skip("cowork hooks.json not present")
    for i, cmd in enumerate(_session_end_commands_from_hooks_json(path)):
        _assert_current_shape(f"cowork hooks.json SessionEnd[{i}]", cmd)


def test_root_hooks_json_stop_is_flush_not_collect():
    """Root hooks.json Stop event must use session-end-flush --trigger stop.

    The bug is literally a stop-hooks hang; the Stop event is where the
    fossil wedges Windows at 3/4. Asserting only SessionEnd left Stop
    unverified, so a Stop fossil could ship undetected.
    """
    path = REPO / "hooks" / "hooks.json"
    cmds = _stop_commands_from_hooks_json(path)
    assert cmds, "root hooks.json must ship a Stop hook"
    for i, cmd in enumerate(cmds):
        _assert_stop_shape(f"root hooks.json Stop[{i}]", cmd)
    assert any(
        "stop_runner.py" in c or ("session-end-flush" in c and "--trigger stop" in c)
        for c in cmds
    ), "root hooks.json Stop must include a session-end-flush --trigger stop hook (directly or via stop_runner.py)"


def test_codex_mirror_hooks_json_stop_is_flush_not_collect():
    path = REPO / "plugins" / "token-optimizer" / "hooks" / "hooks.json"
    cmds = _stop_commands_from_hooks_json(path)
    assert cmds, "Codex mirror must ship a Stop hook"
    for i, cmd in enumerate(cmds):
        _assert_stop_shape(f"codex-mirror hooks.json Stop[{i}]", cmd)
    assert any(
        "stop_runner.py" in c or ("session-end-flush" in c and "--trigger stop" in c)
        for c in cmds
    ), "codex-mirror hooks.json Stop must include a session-end-flush --trigger stop hook (directly or via stop_runner.py)"


def test_cowork_hooks_json_stop_is_current_flush():
    """cowork ships ONLY Stop hooks (no SessionEnd), so the SessionEnd-only
    parity check passes vacuously. Assert over the Stop event directly: the
    cowork Stop flush hook is the current session-end-flush --trigger stop
    shape and carries no collect/dashboard fossil. This is the real assertion
    that was missing -- without it cowork's Stop hooks were unchecked.
    """
    path = REPO / "cowork" / "token-optimizer" / "hooks" / "hooks.json"
    if not path.is_file():
        pytest.skip("cowork hooks.json not present")
    cmds = _stop_commands_from_hooks_json(path)
    assert cmds, "cowork hooks.json must ship a Stop hook (cowork is Stop-only)"
    for i, cmd in enumerate(cmds):
        _assert_stop_shape(f"cowork hooks.json Stop[{i}]", cmd)
    assert any(
        "stop_runner.py" in c or ("session-end-flush" in c and "--trigger stop" in c)
        for c in cmds
    ), "cowork hooks.json Stop must include a session-end-flush --trigger stop hook (directly or via stop_runner.py)"
    # cowork has no SessionEnd event at all -- confirm that explicitly so a
    # future SessionEnd addition does not slip past the SessionEnd parity check.
    se_cmds = _session_end_commands_from_hooks_json(path)
    for i, cmd in enumerate(se_cmds):
        _assert_current_shape(f"cowork hooks.json SessionEnd[{i}]", cmd)


def test_hooks_starter_stop_carries_no_collect_dashboard_chain():
    """hooks-starter.json Stop must not carry the fossil chain either."""
    path = REPO / "skills" / "token-optimizer" / "examples" / "hooks-starter.json"
    cmds = _stop_commands_from_hooks_json(path)
    for i, cmd in enumerate(cmds):
        _assert_stop_shape(f"hooks-starter.json Stop[{i}]", cmd)


def test_hook_command_win32_branch_is_flush_not_collect():
    cmd = _load_win32_hook_command()
    _assert_current_shape("win32 HOOK_COMMAND", cmd)
    assert "--trigger" in cmd and "end" in cmd
    assert cmd.endswith(">/dev/null 2>&1")


def test_hook_command_posix_branch_is_flush_not_collect():
    src = _split_measure_source(MEASURE)
    assert "session-end-flush --trigger end" in src
    assert "collect --quiet &&" not in src
    if sys.platform != "win32":
        cmd = _load_posix_hook_command()
        _assert_current_shape("posix HOOK_COMMAND", cmd)


def test_hooks_starter_sessionend_is_flush_not_collect():
    cmds = _session_end_commands_from_hooks_json(
        REPO / "skills" / "token-optimizer" / "examples" / "hooks-starter.json"
    )
    # The starter now uses the runner-based wiring (matching hooks.json), so
    # SessionEnd points at stop_runner.py (which internally calls
    # session-end-flush). Accept either the direct flush shape or the runner.
    flush_cmds = [
        c for c in cmds
        if ("session-end-flush" in c or "stop_runner.py" in c)
        and "compact-capture" not in c
    ]
    assert flush_cmds, "hooks-starter.json must ship a SessionEnd flush/runner command"
    for i, cmd in enumerate(flush_cmds):
        _assert_current_shape(f"hooks-starter.json SessionEnd[{i}]", cmd)


def test_codex_installer_does_not_emit_collect_dashboard_chain():
    sys.path.insert(0, str(SCRIPTS))
    import codex_install

    hooks = codex_install._managed_hooks()
    blob = json.dumps(hooks)
    assert "collect --quiet &&" not in blob
    # The Codex installer now routes Stop through stop_runner.py (which
    # internally calls session-end-flush), so accept either the direct
    # flush shape or the runner.
    assert "session-end-flush" in blob or "stop_runner.py" in blob, (
        "Codex Stop must invoke session-end-flush directly or via stop_runner.py"
    )
    for event, groups in hooks.items():
        if event not in ("SessionEnd", "Stop"):
            continue
        for group in groups:
            for hook in group.get("hooks", []):
                cmd = hook.get("command", "")
                if "measure.py" in cmd and (
                    "session-end-flush" in cmd or "collect" in cmd or "dashboard" in cmd
                ):
                    assert "session-end-flush" in cmd, f"codex {event} still heavy: {cmd!r}"
                    assert "collect --quiet &&" not in cmd


# --- Repo-wide glob guard -------------------------------------------------
# The recurrence: two prior fixes missed a still-shipped source because
# each fix only checked the exact identity it had just rewritten. This glob is
# the belt to the per-file whitelist's suspenders: it asserts that NO shipped
# hooks.json / hooks-starter / example hook JSON anywhere in the repo carries
# the ``collect --quiet &&`` or ``dashboard --quiet`` chain in any SessionEnd
# or Stop command. If a new shipped source reintroduces the fossil, this test
# fails regardless of which identity the per-file tests pin.

_GLOB_HOOK_PATTERNS = ("**/hooks.json", "**/hooks-starter*.json")


def _all_shipped_hook_jsons() -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for pattern in _GLOB_HOOK_PATTERNS:
        for p in REPO.glob(pattern):
            if ".git" in p.parts:
                continue
            if p in seen:
                continue
            seen.add(p)
            out.append(p)
    return sorted(out)


def test_no_shipped_hooks_json_carries_collect_dashboard_chain():
    """Repo-wide: no shipped hooks.json/starter/example carries the
    ``collect --quiet &&`` / ``dashboard --quiet`` fossil in SessionEnd or Stop.

    This is the recurrence guard: the fossil survived two fixes by
    living in a source the per-file parity tests did not glob. Every shipped
    hook JSON is scanned so a new source carrying the chain fails here even if
    no per-file test pins it yet.
    """
    files = _all_shipped_hook_jsons()
    assert files, "expected at least one shipped hooks.json/hooks-starter in the repo"
    offenders: list[str] = []
    for path in files:
        try:
            pairs = _commands_from_hooks_json(path, ("SessionEnd", "Stop"))
        except (json.JSONDecodeError, OSError) as e:
            offenders.append(f"{path}: unreadable ({e})")
            continue
        for event, cmd in pairs:
            if _FOSSIL_CHAIN.search(cmd):
                offenders.append(f"{path} {event}: collect --quiet && fossil: {cmd!r}")
            if "dashboard --quiet" in cmd:
                offenders.append(f"{path} {event}: dashboard --quiet chain: {cmd!r}")
    assert not offenders, (
        "shipped hook JSON carries the collect/dashboard fossil chain:\n  "
        + "\n  ".join(offenders)
    )


def test_glob_guard_actually_scans_known_shipped_sources():
    """Sanity: the glob guard reaches the three shipped hooks.json and the
    starter, so a future path rename does not silently drop coverage.
    """
    files = {p.relative_to(REPO).as_posix() for p in _all_shipped_hook_jsons()}
    assert "hooks/hooks.json" in files
    assert "plugins/token-optimizer/hooks/hooks.json" in files
    assert "cowork/token-optimizer/hooks/hooks.json" in files
    assert "skills/token-optimizer/examples/hooks-starter.json" in files
