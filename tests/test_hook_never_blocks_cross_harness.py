#!/usr/bin/env python3
"""Cross-harness invariant: a Token Optimizer hook must NEVER return a blocking result.

Origin: a user's Codex shell runner froze because `hooks/python-launcher.sh` did
`exit 127` when no Python was found. Codex (and any harness that treats a non-zero
PreToolUse hook as a tool-blocking failure) then blocked every Read/Bash -> the
session looked dead ("shell runner unavailable").

Token Optimizer supports many harnesses (Claude Code, Codex, Copilot, Hermes,
OpenClaw, OpenCode). They fall into two hook-contract classes:

  * SHELL-HOOK harnesses (Claude Code, Codex, cowork): a hooks.json command runs
    `python-launcher.sh run.py <script>`. The COMMAND'S EXIT CODE is the contract.
    It must be 0 no matter what (missing Python, broken override, script crash).

  * BRIDGE harnesses (Hermes, Copilot, Grok Build): a Python bridge shells to
    our scripts and decides its own exit / permission decision. On any internal
    failure it must swallow the error, never propagate a non-zero exit or a deny.

These tests assert both invariants at the source and behavioral level so the bug
cannot regress into ANY supported harness.

Run:  python3 tests/test_hook_never_blocks_cross_harness.py   (or via pytest)
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "hooks" / "python-launcher.sh"
RUN_PY = REPO / "hooks" / "run.py"
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

# EVERY launcher copy in the repo — the canonical hooks/ AND the generated Codex
# marketplace mirror plugins/token-optimizer/hooks/. A user's Codex froze because
# ONE copy exited 127; a mirror that can drift is itself the bug, so the
# non-blocking invariant is asserted on all copies.
#
# scratchpad/ is excluded: it holds throwaway repro fixtures (fake home dirs,
# third-party plugin copies) that are NOT shipped Token Optimizer code. Scanning
# them produces phantom failures on hooks.json files that belong to other
# plugins (e.g. figma) or test harnesses, not our invariant.
_EXCLUDE_DIRS = {"scratchpad", ".git", "node_modules", "__pycache__", ".pytest_cache"}


def _rglob_excluding(root: Path, pattern: str) -> list[Path]:
    """rglob that skips throwaway / non-shipped directories."""
    results = []
    for p in root.rglob(pattern):
        if any(part in _EXCLUDE_DIRS for part in p.parts):
            continue
        results.append(p)
    return sorted(results)


ALL_LAUNCHERS = _rglob_excluding(REPO, "python-launcher.sh")

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="bash launcher path is POSIX; Windows covered by CI matrix"
)

# All hooks.json shipped in the repo (each maps to one or more harnesses).
HOOKS_JSONS = _rglob_excluding(REPO, "hooks.json")


def _run_chain(target_script: Path, env_extra=None, args=("--quiet",)):
    """Invoke the real hook chain: python-launcher.sh run.py <script>. Return rc."""
    env = {**os.environ}
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(
        ["bash", str(LAUNCHER), str(RUN_PY), str(target_script), *args],
        capture_output=True, text=True, timeout=60, env=env,
    )
    return r.returncode, r.stdout, r.stderr


# ---- Static invariant: the launcher's no-interpreter path is non-blocking -------

@pytest.mark.parametrize("launcher", ALL_LAUNCHERS, ids=lambda p: str(p.relative_to(REPO)))
def test_launcher_no_interpreter_path_exits_zero(launcher):
    src = launcher.read_text()
    # The terminal (no-Python) branch must end in `exit 0`, never a non-zero exit.
    assert re.search(r"no usable Python 3 interpreter found", src), "diagnostic missing"
    tail = src[src.index("no usable Python 3 interpreter found"):]
    assert re.search(r"\nexit 0\b", tail), "no-Python path must end in `exit 0`"
    assert "exit 127" not in tail, "no-Python path must not exit non-zero (would block hooks)"


@pytest.mark.parametrize("launcher", ALL_LAUNCHERS, ids=lambda p: str(p.relative_to(REPO)))
def test_launcher_has_no_nonzero_terminal_exit_anywhere(launcher):
    # Defense in depth: no launcher copy may terminate a hook with exit >0.
    bad = re.findall(r"\nexit ([1-9][0-9]*)\b", launcher.read_text())
    assert not bad, f"{launcher.name} has blocking exit codes {bad}; hooks must exit 0"


def test_at_least_three_launcher_copies_covered():
    # Guard the guard: canonical hooks/ + Codex mirror + Cowork mirror. If a mirror
    # path changes, this test must still find them all so a drifting copy can never
    # silently escape the invariant above.
    assert len(ALL_LAUNCHERS) >= 3, f"expected canonical + 2 mirrors, found {ALL_LAUNCHERS}"


def test_all_launcher_copies_are_byte_identical():
    # D6: check-mirror-sync.sh guards ONLY plugins/, not cowork/, so a cowork drift
    # (e.g. one copy reverting to `exit 127`) was invisible to the release gate. The
    # launcher is a byte-identical mirror everywhere; assert that here so ANY drift
    # fails CI regardless of which mirror tooling covers which path.
    canonical = LAUNCHER.read_bytes()
    for launcher in ALL_LAUNCHERS:
        assert launcher.read_bytes() == canonical, (
            f"{launcher.relative_to(REPO)} drifted from canonical hooks/python-launcher.sh"
        )


# ---- Behavioral: shell-hook chain stays exit 0 under failures we CAN simulate ----

@posix_only
def test_broken_override_falls_through_exit_zero(tmp_path):
    ok_script = tmp_path / "noop.py"
    ok_script.write_text("print('ok')\n")
    rc, _out, err = _run_chain(ok_script, env_extra={"TOKEN_OPTIMIZER_PYTHON": "/nonexistent/python3"})
    assert rc == 0, f"broken override must degrade, got rc={rc}"
    assert "not a working Python 3" in err, "broken override should warn to stderr"


@posix_only
@pytest.mark.parametrize("shell", ["/bin/sh", "/bin/bash", "/bin/dash"])
def test_override_pointing_at_a_shell_does_not_block(shell, tmp_path):
    # Torture batch-2 finding: a POSIX shell accepts `-c ''` exactly like Python,
    # so a naive probe would exec it on run.py, parse Python as shell, and exit
    # non-zero (2) -- a BLOCKING hook. The Python-specific probe must reject any
    # shell and fall through to real discovery instead.
    if not Path(shell).exists():
        pytest.skip(f"{shell} absent")
    ok = tmp_path / "noop.py"
    ok.write_text("print('ok')\n")
    rc, _o, err = _run_chain(ok, env_extra={"TOKEN_OPTIMIZER_PYTHON": shell})
    assert rc == 0, f"a shell override must not block (rc={rc}); it should fall through"
    assert "not a working Python 3" in err, "shell override should be rejected with a warning"


@posix_only
def test_closed_stderr_does_not_block(tmp_path):
    # Torture batch-1 finding: under `set -e`, a diagnostic `echo >&2` fails when the
    # harness invoked the hook with stderr (fd 2) closed, aborting non-zero before the
    # final exit 0 -- a blocking hook. A broken override forces the diagnostic path;
    # closing fd 2 must not turn it into a non-zero exit.
    ok = tmp_path / "noop.py"
    ok.write_text("print('ok')\n")
    r = subprocess.run(
        ["bash", "-c", 'bash "$1" "$2" "$3" --quiet 2>&-', "_", str(LAUNCHER), str(RUN_PY), str(ok)],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "TOKEN_OPTIMIZER_PYTHON": "/nonexistent/python3"},
    )
    assert r.returncode == 0, f"closed-stderr diagnostic path must not block, rc={r.returncode}"


@posix_only
def test_hook_target_script_raising_exits_zero(tmp_path):
    raiser = tmp_path / "raiser.py"
    raiser.write_text("raise RuntimeError('boom')\n")
    rc, _o, _e = _run_chain(raiser)
    assert rc == 0, f"a crashing hook script must not block the tool, got rc={rc}"


@posix_only
def test_hook_target_script_missing_exits_zero(tmp_path):
    rc, _o, _e = _run_chain(tmp_path / "does-not-exist.py")
    assert rc == 0, f"a missing hook script must not block the tool, got rc={rc}"


@posix_only
def test_override_accepts_interpreter_outside_safe_prefix(tmp_path):
    # D2: pyenv/asdf/conda/venv live outside the safe-prefix allow-list. An explicit
    # override must still run them. Simulate via a symlink in a non-safe-prefix dir.
    real = subprocess.run(["bash", "-lc", "command -v python3"], capture_output=True, text=True).stdout.strip()
    if not real:
        pytest.skip("no system python3 to symlink")
    fake = tmp_path / "python3"
    fake.symlink_to(real)
    r = subprocess.run(
        ["bash", str(LAUNCHER), "-c", "print('OVERRIDE_OK')"],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "TOKEN_OPTIMIZER_PYTHON": str(fake)},
    )
    assert r.returncode == 0 and "OVERRIDE_OK" in r.stdout, f"override exec failed: {r.stderr}"


# ---- Cross-harness: every hooks.json wrapper is non-blocking by construction -----

def _iter_hook_commands(hooks_path: Path):
    data = json.loads(hooks_path.read_text())
    for _event, groups in (data.get("hooks") or {}).items():
        for group in groups:
            for h in group.get("hooks", []):
                if h.get("type") == "command" and "command" in h:
                    yield h["command"]


@pytest.mark.parametrize("hooks_path", HOOKS_JSONS, ids=lambda p: str(p.relative_to(REPO)))
def test_every_hooks_json_command_routes_through_launcher_and_exits_zero(hooks_path):
    cmds = list(_iter_hook_commands(hooks_path))
    assert cmds, f"{hooks_path} has no command hooks"
    for cmd in cmds:
        # Universal non-blocking invariant: every hook wrapper terminates in
        # `exit 0`, so even a total bash-discovery miss returns non-blocking.
        assert cmd.rstrip().endswith("exit 0"), f"hook wrapper must end in `exit 0`: {cmd[:80]}"
        # Python hooks (those running run.py) must route through the launcher so
        # the no-interpreter degradation applies. (Pure-bash probes are exempt.)
        if "run.py" in cmd:
            assert "python-launcher.sh" in cmd, f"python hook bypasses launcher: {cmd[:80]}"


# ---- Bridge harnesses (Hermes, Copilot): source-level non-propagation guard ------

@pytest.mark.parametrize("bridge", ["hermes_hook_bridge.py", "copilot_hook_bridge.py",
                                     "cursor_hook_bridge.py", "grok_hook_bridge.py"])
def test_bridge_swallows_subprocess_failure(bridge):
    path = SCRIPTS / bridge
    if not path.exists():
        pytest.skip(f"{bridge} not present")
    src = path.read_text()
    # Exclude the `if __name__ == "__main__"` block: that is a direct-invocation
    # smoke test (a human running the bridge by hand), NOT the hook-serving path
    # the harness calls. A non-zero exit there cannot wedge a tool call.
    hook_src = re.split(r"\nif __name__ ==", src)[0]
    # In the hook-serving code a bridge must run our scripts and swallow failures,
    # never re-raise or sys.exit(non-zero) on a child error.
    assert "subprocess" in src, f"{bridge} should invoke scripts via subprocess"
    assert not re.search(r"sys\.exit\(\s*[1-9]", hook_src), (
        f"{bridge} must not exit non-zero in its hook path (would block the tool)"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
