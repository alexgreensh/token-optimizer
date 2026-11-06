"""Platform-reality regressions (Windows above all), re-planted for CI proof.

Shape (shared by every scripts/mutations/<area>.py):
    MUTATIONS = [(name, [test node ids], file_relpath, [(old, new), ...])]

Each entry re-introduces a bug that shipped (or the hot-path shape of one) into
a throwaway copy; the listed tests must FAIL on it. Every ``old`` must match the
file exactly once, or the check reports the entry as stale.
"""

from __future__ import annotations

import json

MEASURE = "skills/token-optimizer/scripts/measure.py"
BUDGET = "tests/test_hot_path_process_budget.py"

# --- #139: UserPromptSubmit fanned out to six hooks.json entries -------------
_UPS_ENTRY = (
    '      {\n'
    '        "hooks": [\n'
    '          {\n'
    '            "type": "command",\n'
    '            "command": ' + json.dumps(
        'for b in bash /bin/bash /usr/bin/bash /usr/local/bin/bash /opt/homebrew/bin/bash; '
        'do command -v "$b" >/dev/null 2>&1 || continue; L="${CLAUDE_PLUGIN_ROOT}/hooks/python-launcher.sh"; '
        '[ -r "$L" ] || exit 0; exec "$b" "$L" "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" '
        'skills/token-optimizer/scripts/measure.py %s; done; exit 0') + ',\n'
    '            "timeout": 10\n'
    '          }\n'
    '        ]\n'
    '      },\n'
)
_PRE_139_UPS = "".join(_UPS_ENTRY % sub for sub in (
    "quality-cache --warn --quiet", "prompt-continuity --quiet", "verbosity-steer --quiet",
    "ensure-health --once-per-session", "quality-cache --force --quiet --once-per-session"))

MUTATIONS = [
    ("#139 UserPromptSubmit fans out to six hooks.json entries",
     [BUDGET + "::test_hooks_json_python_processes_per_event_within_budget"],
     "hooks/hooks.json",
     [('    "UserPromptSubmit": [\n', '    "UserPromptSubmit": [\n' + _PRE_139_UPS)]),
    ("#139 UserPromptSubmit runner spawns measure.py per subcommand",
     [BUDGET + "::test_healthy_prompt_and_tool_hooks_start_nothing",
      BUDGET + "::test_real_dispatch_extra_processes_within_budget"],
     "hooks/userpromptsubmit_runner.py",
     [("    _install_runner_deadline()\n\n    # Capture each subcommand's stdout",
       "    _install_runner_deadline()\n"
       "    __import__('subprocess').run([sys.executable, str(Path(measure.__file__)),"
       " 'quality-cache', '--quiet'], capture_output=True)\n\n"
       "    # Capture each subcommand's stdout")]),
    ("#104/#107 run.py starts the hook child without DETACHED_PROCESS",
     ["tests/test_windows_spawn_no_window.py::test_run_py_spawn_nt_uses_detached_process"],
     "hooks/run.py",
     [('_flags = getattr(subprocess, "DETACHED_PROCESS", 0)', "_flags = 0")]),
    ("#104/#107 spawn_detached drops DETACHED_PROCESS",
     ["tests/test_windows_spawn_no_window.py::test_spawn_detached_nt_passes_creationflags"],
     "skills/token-optimizer/scripts/spawn_utils.py",
     [('for _name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP",',
       'for _name in ("CREATE_NEW_PROCESS_GROUP",')]),
    ("#105 UTF-8 re-exec child gets no explicit std handles (stdout lost)",
     ["tests/test_windows_spawn_no_window.py::test_utf8_io_reexec_nt_passes_std_handles"],
     "skills/token-optimizer/scripts/utf8_io.py",
     [("                    _popen_kwargs[_name] = _s\n", "                    pass\n")]),
    ("#143 WindowsApps probe lets a live pythonw twin vouch for a dead candidate",
     ["tests/test_windowsapps_twin_cannot_vouch.py"],
     "hooks/python-launcher.sh",
     [('        timeout --kill-after=1s 2s "$binpath" -c \\\n',
       '        timeout --kill-after=1s 2s "${binpath%/*}/pythonw.exe" -c \\\n')]),
    ("#188 Windows launcher runs the newest version dir without opening its run.py",
     ["tests/test_windows_hook_launcher.py::test_windows_version_resolver_skips_incomplete_newest_upgrade"],
     "hooks/windows-launcher.py",
     [('            try:\n                with open(run_py, "rb") as fh:\n                    if not fh.read(1):\n'
       "                        continue  # zero-byte/truncated: would run nothing rc 0\n"
       "            except OSError:\n                continue  # missing, incomplete upgrade, or ACL-denied\n"
       "            root = candidate",
       "            root = candidate")]),
    ("#253 PreCompact checkpoint back under the 2.5 s Stop budget",
     ["tests/test_hook_entry_budgets.py::test_every_in_scope_hooks_json_entry_has_the_expected_budget"],
     "skills/token-optimizer/scripts/hook_runtime.py",
     [('("measure", _trigger("compact-capture", "auto"), BUDGET_PRECOMPACT_CAPTURE,',
       '("measure", _trigger("compact-capture", "auto"), BUDGET_STOP,')]),
    ("budget recorder counts refused CreateProcess attempts as processes",
     [BUDGET + "::test_recorder_counts_started_processes_not_refused_attempts"],
     BUDGET,
     [("                try:\n                    super().__init__(args, *a, **kw)\n",
       '                _w("popen", argv)\n'
       "                try:\n                    super().__init__(args, *a, **kw)\n")]),
    ("budget recorder double-counts Popen's own posix_spawn",
     [BUDGET + "::test_recorder_counts_started_processes_not_refused_attempts[posix_spawn]"],
     BUDGET,
     [("                if getattr(_tls, \"inside\", 0):\n"
       "                    return real(*a, **k)  # Popen's own start: already recorded\n", "")]),
]

MUTATIONS += [
]

MUTATIONS += [
]
