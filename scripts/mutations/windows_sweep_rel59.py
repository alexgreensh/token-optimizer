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
    ("Copilot CLI on Windows gets no powershell hook command",
     [C + "::test_windows_install_writes_a_powershell_command_for_every_hook"],
     COPILOT,
     [('        if sys.platform == "win32":\n            # Copilot CLI runs',
       '        if False:\n            # Copilot CLI runs')]),
    ("Claude plugin hooks on Windows lose the new version after an update",
     [W + "::test_claude_plugin_hooks_survive_an_upgrade_with_a_windows_style_root"],
     CLAUDE_HOOKS,
     [('${D%[/\\\\\\\\]*}', '${D%/*}')]),
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
