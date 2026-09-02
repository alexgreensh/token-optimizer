"""Bug A regression: copilot/hermes/cursor rollups arm a wall-clock deadline.

The rollup subcommands are spawned fire-and-forget by the Stop hook bridges
(`copilot_hook_bridge.handle_stop` / `hermes_hook_bridge.run_rollup` /
`cursor_hook_bridge._spawn_rollup`) with
``start_new_session=True`` and no PID tracking, so the parent hook's 20s
ceiling never reaches the detached grandchild.  Before this fix neither
rollup branch installed a ``HookDeadline``, so a stuck SQLite / pathological
session dir left a permanent orphan per Stop event, silently contending for
``trends.db``.

These tests prove:
  * a blocking ``_collect_*_sessions`` is terminated by the deadline within a
    bounded time (subprocess monkeypatches ``_install_hook_budget`` to a tiny
    ``HookDeadline`` and the collect to a long sleep; an unarmed branch hangs,
    an armed branch bounded-exits).
  * a fast collect completes and exits 0 with no spurious "hook budget
    exceeded" diagnostic.
  * both rollup branches contain ``_install_hook_budget(`` and
    ``_clear_hook_budget(`` by source inspection (mirrors
    ``test_all_three_user_prompt_handlers_install_and_clear_budgets``).

Run: python3 -m pytest tests/test_rollup_deadline.py -q
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
MEASURE = SCRIPTS / "measure.py"
sys.path.insert(0, str(SCRIPTS))

from hook_runtime import HookDeadline  # noqa: E402


def _rollup_dispatch_code(
    subcommand: str,
    collect_name: str,
    tmp_path: Path,
    *,
    collect_block_seconds: float,
    budget_seconds: float,
) -> str:
    """Build a subprocess script that runs measure.py's ``__main__`` dispatch
    with a monkeypatched hook budget and collect.

    The patch is injected just before the ``if __name__ == "__main__":`` block
    so it overrides the module-level ``_install_hook_budget`` and the named
    ``_collect_*_sessions`` AFTER their real definitions but BEFORE the
    dispatch reads them.  ``PYTHONUTF8=1`` is set so ``reexec_in_utf8_mode``
    is a no-op (it would otherwise re-exec the wrapper and discard the patch).
    """
    return (
        "import os, sys, time\n"
        "sys.path.insert(0, {scripts!r})\n"
        "sys.argv = ['measure.py', {sub!r}]\n"
        "os.environ.setdefault('PYTHONUTF8', '1')\n"
        "os.environ.setdefault('TOKEN_OPTIMIZER_SNAPSHOT_DIR', {snap!r})\n"
        "src = open({measure!r}).read()\n"
        "patch = (\n"
        "    'import time as _t\\n'\n"
        "    'from hook_runtime import HookDeadline as _HD\\n'\n"
        "    'def _test_install(seconds=8):\\n'\n"
        "    '    return _HD({budget!r}).start()\\n'\n"
        "    'def _test_collect(days=90, quiet=False):\\n'\n"
        "    '    _t.sleep({block!r})\\n'\n"
        "    '_install_hook_budget = _test_install\\n'\n"
        "    '{collect} = _test_collect\\n'\n"
        ")\n"
        "marker = 'if __name__ == \"__main__\":'\n"
        "src = src.replace(marker, patch + marker, 1)\n"
        "ns = {{'__name__': '__main__', '__file__': {measure!r}}}\n"
        "exec(compile(src, ns['__file__'], 'exec'), ns)\n"
    ).format(
        scripts=str(SCRIPTS),
        sub=subcommand,
        snap=str(tmp_path),
        measure=str(MEASURE),
        budget=budget_seconds,
        block=collect_block_seconds,
        collect=collect_name,
    )


def _run_rollup_subprocess(subcommand, collect_name, tmp_path, *, block, budget):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SCRIPTS)
    env["PYTHONUTF8"] = "1"
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(tmp_path)
    if subcommand.startswith("cursor-"):
        # The cursor-rollup dispatch refuses unless the cursor runtime is pinned,
        # exactly as the real cursor_hook_bridge does when it spawns the rollup.
        env["TOKEN_OPTIMIZER_RUNTIME"] = "cursor"
    code = _rollup_dispatch_code(
        subcommand, collect_name, tmp_path,
        collect_block_seconds=block, budget_seconds=budget,
    )
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, timeout=8,
    )
    return proc, time.monotonic() - started


@pytest.mark.parametrize("subcommand,collect_name", [
    ("copilot-rollup", "_collect_copilot_sessions"),
    ("hermes-rollup", "_collect_hermes_sessions"),
    ("cursor-rollup", "_collect_cursor_sessions"),
    ("grok-rollup", "_collect_grok_sessions"),
])
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="wall-clock assertion includes POSIX-fast interpreter startup",
)
def test_rollup_bounded_on_blocking_collect(subcommand, collect_name, tmp_path):
    """A blocking collect is terminated by the armed deadline, not allowed to
    hang.  On unfixed code no deadline is armed so the subprocess sleeps the
    full block and hits the 8s timeout -> TimeoutExpired -> test fails."""
    try:
        proc, elapsed = _run_rollup_subprocess(
            subcommand, collect_name, tmp_path,
            block=30, budget=0.3,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"{subcommand} hung past 8s; no wall-clock deadline armed on a "
            f"blocking _collect_*_sessions (permanent orphan risk)"
        )
    assert proc.returncode == 0, (
        f"{subcommand} exit code {proc.returncode}; stderr={proc.stderr!r}"
    )
    # 0.3s budget + ~0.3s process startup; well under the 8s hang ceiling.
    assert elapsed < 4.0, (
        f"{subcommand} did not bounded-exit: took {elapsed:.2f}s "
        f"(deadline should have fired at ~0.3s)"
    )


@pytest.mark.parametrize("subcommand,collect_name", [
    ("copilot-rollup", "_collect_copilot_sessions"),
    ("hermes-rollup", "_collect_hermes_sessions"),
    ("cursor-rollup", "_collect_cursor_sessions"),
    ("grok-rollup", "_collect_grok_sessions"),
])
def test_rollup_normal_completion_exits_zero_no_diagnostic(subcommand, collect_name, tmp_path):
    """A fast collect completes, exits 0, and clears the budget -- no spurious
    'hook budget exceeded' diagnostic fires on normal completion."""
    proc, elapsed = _run_rollup_subprocess(
        subcommand, collect_name, tmp_path,
        block=0, budget=5.0,
    )
    assert proc.returncode == 0, (
        f"{subcommand} exit code {proc.returncode}; stderr={proc.stderr!r}"
    )
    assert "hook budget exceeded" not in proc.stderr, (
        f"{subcommand} emitted a spurious timeout diagnostic on normal "
        f"completion: stderr={proc.stderr!r}"
    )


def test_both_rollup_branches_install_and_clear_budget_source():
    """Source inspection: both copilot-rollup and hermes-rollup branches call
    _install_hook_budget( and _clear_hook_budget( (mirrors the
    test_all_three_user_prompt_handlers_install_and_clear_budgets pattern)."""
    source = MEASURE.read_text(encoding="utf-8")
    names = ["copilot-rollup", "hermes-rollup", "cursor-rollup", "grok-rollup"]
    blocks = []
    for name in names:
        start = source.index(f'elif args[0] == "{name}":')
        nxt = source.find('elif args[0] ==', start + 1)
        end = nxt if nxt != -1 else len(source)
        blocks.append(source[start:end])
    missing = [
        name for name, block in zip(names, blocks)
        if "_install_hook_budget(" not in block or "_clear_hook_budget(" not in block
    ]
    assert not missing, (
        f"rollup branches missing _install_hook_budget/_clear_hook_budget: {missing}"
    )
