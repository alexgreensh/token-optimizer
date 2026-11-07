"""#264 cold-compile gate regressions, planted back by scripts/check_regression_guards.py.

Claude desktop on Windows starts hooks with PYTHONDONTWRITEBYTECODE=1 and
measure.py took 20-28 s to compile there, past the host's 15/20 s hook
timeouts, so every hook was killed before the .pyc was written. Each entry
re-introduces one way the fix can break and names the tests in
tests/test_cold_compile_gate.py (real `python hooks/run.py <runner>`
processes, a simulated slow compile) that must turn red.

Shape: (name, [test node ids], file_relpath, [(old, new), ...]).
"""

GATE = "tests/test_cold_compile_gate.py"
SLOW = GATE + "::test_slow_cold_compile_runs_once_in_the_background_and_hooks_fail_open"
FAILED = GATE + "::test_failed_background_compile_falls_back_to_the_inline_compile"
# Spawns the hook with stdout/stderr as pipes, as the Claude Code host does.
PIPES = GATE + "::test_cold_hook_pipes_reach_eof_while_the_background_compile_runs"
LOCKS = GATE + "::test_live_lock_defers_to_the_running_compile_and_stale_lock_is_taken_over"
WAITS = GATE + "::test_every_gated_wait_leaves_room_under_the_host_timeout"

RUNNER = "hooks/module_runner.py"

MUTATIONS = [
    ("#264 hooks compile measure.py inline on the host deadline again",
     [SLOW],
     RUNNER,
     [("            if not _measure_bytecode_ready(_plugin_scripts, _wait):",
       "            if False:")]),

    ("#264 no compile lock: every concurrent cold hook starts its own compile",
     [SLOW, LOCKS],
     RUNNER,
     [("fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)",
       "fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)")]),

    ("#264 bytecode check ignores the active pycache prefix",
     [SLOW],
     RUNNER,
     [("    return importlib.util.cache_from_source(source)\n",
       "    return os.path.join(os.path.dirname(source), \"__pycache__\",\n"
       "                        os.path.basename(importlib.util.cache_from_source(source)))\n")]),

    ("#264 background compiler inherits the hook's stdout (host waits for its EOF)",
     [SLOW, PIPES],
     RUNNER,
     [("stdin=subprocess.DEVNULL,\n"
       "                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)",
       "stdin=subprocess.DEVNULL)")]),

    ("#264 a compile that cannot succeed leaves every hook failing open",
     [FAILED],
     RUNNER,
     [("        if _compile_failed_for(failed, measure_src):\n            return True\n", ""),
      ("            # Ours exited and the cache is still cold: compile inline.\n"
       "            return True\n",
       "            # Ours exited and the cache is still cold: compile inline.\n"
       "            child = None\n")]),

    ("#264 a stale lock from a dead compile is never taken over",
     [LOCKS],
     RUNNER,
     [("            if age < _COLD_COMPILE_LOCK_STALE_S:\n                return False\n",
       "            return False\n")]),

    ("#264 Stop waits for the compile past the host's 15 s timeout",
     [WAITS],
     RUNNER,
     [('    "stop_runner": 7.0,', '    "stop_runner": 14.0,')]),
]
