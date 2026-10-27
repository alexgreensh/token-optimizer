"""Windows sweep for 5.13.59 (#278 follow-up and the runtime sweep). Each
planted bug brings one back; the listed tests must go red.

Shape: (name, [test node ids], file_relpath, [(old, new), ...]).
"""

MEASURE = "skills/token-optimizer/scripts/measure.py"
INSTALL = "skills/token-optimizer/scripts/codex_install.py"
COPILOT = "skills/token-optimizer/scripts/copilot_install.py"
LAUNCHER = "hooks/windows-launcher.py"
CLAUDE_HOOKS = "hooks/hooks.json"
M = "tests/test_codex_windows_shell_matrix_278.py"
N = "tests/test_codex_shell_neutral_hooks_278.py"
W = "tests/test_hook_wire_contract.py"
C = "tests/test_copilot_windows_powershell_hook.py"
P = "tests/test_windows_pid_liveness.py"
S = "tests/test_windows_settings_hook_interpreter.py"
D = "tests/test_windows_dashboard_launcher_cmd.py"
PL = "tests/test_python_launcher_windows_pythonw.py"
PY_LAUNCHER = "hooks/python-launcher.sh"
NP = "tests/test_python_launcher_no_python3_issue21.py"
IS = "tests/test_install_sh_copilot_wsl_issue78.py"

MUTATIONS = [
    ("#278 a $ % backtick or ! in the plugin path forces the cmd-only hook again",
     [M + "::test_expanding_characters_no_longer_force_the_cmd_only_form",
      M + "::test_bootstrap_command_runs_the_hook_end_to_end"],
     INSTALL,
     [("        code = _launcher_bootstrap(launcher_path) if same_parent else None\n",
       "        code = None\n")]),

    ("#278 launcher and install paths skip their 8.3 short names",
     [M + "::test_short_names_are_preferred_over_the_bootstrap",
      M + "::test_heal_uses_short_names_for_old_commands_with_expanding_characters"],
     INSTALL,
     [("    short = _short_path(parent)\n    if short:\n", "    short = None\n    if short:\n")]),

    ("#278 launcher ignores a relative --baked-root",
     [M + "::test_launcher_resolves_a_relative_baked_root_next_to_itself",
      M + "::test_bootstrap_command_runs_the_hook_end_to_end"],
     LAUNCHER,
     [("    if not root.is_absolute() and here:\n", "    if False:\n")]),

    ("#278 a development checkout hook runs a version folder beside it",
     [M + "::test_dev_checkout_command_runs_the_checkout_in_every_shell"],
     LAUNCHER,
     [("        if not _SEMVER_DIR_RE.fullmatch(root.name):\n", "        if False:\n")]),

    ("#278 Codex Stop writes a 'No transcript data available' placeholder",
     [W + "::test_codex_stop_finds_the_transcript_from_the_session_id_alone",
      N + "::test_compact_capture_writes_no_unknown_placeholder_on_codex"],
     MEASURE,
     [("        if _use_codex_session_adapter(transcript_path or None):\n",
       "        if not session_id and _use_codex_session_adapter():\n")]),

    ("Copilot CLI on Windows gets no powershell hook command",
     [C + "::test_windows_install_writes_a_powershell_command_for_every_hook"],
     COPILOT,
     [('        if sys.platform == "win32":\n            # Copilot CLI runs',
       '        if False:\n            # Copilot CLI runs')]),

    ("Claude plugin hooks on Windows lose the new version after an update",
     [W + "::test_claude_plugin_hooks_survive_an_upgrade_with_a_windows_style_root"],
     CLAUDE_HOOKS,
     [('${D%[/\\\\\\\\]*}', '${D%/*}')]),

    ("Windows liveness probe sends CTRL_C_EVENT via os.kill(pid, 0)",
     [P + "::test_windows_never_sends_a_signal_and_reads_the_process_table"],
     MEASURE,
     [('    if os.name == "nt":\n        return _win_pid_is_running(pid)\n', "")]),

    ("purge --force on Windows asks POSIX ps and never stops the daemon",
     [P + "::test_purge_force_on_windows_identifies_and_stops_the_daemon"],
     MEASURE,
     [('            if os.name == "nt":\n                # No `ps` on Windows',
       '            if False:\n                # No `ps` on Windows')]),

    ("Windows settings.json hooks call bare python3 (missing or the Store stub in Git Bash)",
     [S + "::test_windows_smart_compact_hooks_name_the_real_interpreter",
      S + "::test_setup_quality_bar_on_windows_writes_the_real_interpreter"],
     MEASURE,
     [('    if sys.platform == "win32":\n        return (shlex.quote(str(sys.executable)',
       '    if False:\n        return (shlex.quote(str(sys.executable)')]),

    ("dashboard-launcher.cmd is read in the OEM code page and garbles non-ASCII paths",
     [D + "::test_utf8_code_page_is_set_before_any_path"],
     MEASURE,
     [('        "chcp 65001\\r\\n"\n', "")]),

    ("Windows conda Pythons are refused by the hook launcher",
     [PL + "::test_safe_prefix_accepts_default_windows_conda_scoop_uv_installs"],
     PY_LAUNCHER,
     [("        /[a-zA-Z]/Users/*/miniconda3/*|/[a-zA-Z]/Users/*/anaconda3/*)   return 0 ;;\n", "")]),

    ("#21 hook launcher gives up when Windows has python but no python3",
     [NP + "::test_only_python_on_path_is_used"],
     PY_LAUNCHER,
     [('if py=$(find_interpreter "python"); then', "if false; then")]),

    ("#21 hook launcher gives up when Windows has only the py launcher",
     [NP + "::test_only_the_py_launcher_on_path_runs_py_dash_3"],
     PY_LAUNCHER,
     [('if pyl=$(find_interpreter "py"); then', "if false; then")]),

    ("#78 install.sh --copilot under WSL root writes to /root/.copilot again",
     [IS + "::test_wsl_root_targets_the_single_windows_profile"],
     "install.sh",
     [('            export "${export_as}=${guessed}"\n', "")]),
]
