"""#275/#276 (5.13.57): Codex install bugs on Windows and POSIX, and the
model-pin tip. Each planted bug brings one back; listed tests must go red.

Shape: (name, [test node ids], file_relpath, [(old, new), ...]).
"""

MEASURE = "skills/token-optimizer/scripts/measure.py"
INSTALL = "skills/token-optimizer/scripts/codex_install.py"
PROMPT = "skills/token-optimizer/scripts/codex_compact_prompt.py"
DOCTOR = "skills/token-optimizer/scripts/codex_doctor.py"
T = "tests/test_codex_install_issue_276.py"
L = "tests/test_windows_hook_launcher.py"
N = "tests/test_codex_shell_neutral_hooks_278.py"

MUTATIONS = [
    ("Windows Codex hook command starts with a quote again (cmd strips it)",
     [L + "::test_windows_codex_command_never_starts_with_a_quote",
      N + "::test_spaced_interpreter_without_short_name_falls_back"],
     INSTALL,
     [("        _WIN_UNQUOTED_PREFIX,\n", "")]),

    ("Windows hooks saved by older versions are not repaired",
     [T + "::test_heal_repairs_only_our_quoted_launcher_commands",
      T + "::test_heal_falls_back_to_unquoted_set_prefix_when_no_neutral_form"],
     INSTALL,
     [("                            handler[\"command\"] = f\"{_WIN_UNQUOTED_PREFIX} {command}\"\n",
       "                            pass\n")]),

    ("compact prompt appended at end of file, inside the last table",
     [T + "::test_install_puts_setting_at_root_not_in_last_table",
      T + "::test_reinstall_moves_a_misplaced_block_to_the_root"],
     PROMPT,
     [("    candidates = [m.start() for m in _TABLE_HEADER_RE.finditer(config_text)]\n",
       "    candidates = []\n")]),

    ("insert keeps a placement that breaks the TOML",
     [T + "::test_array_line_that_looks_like_a_header_is_not_used"],
     PROMPT,
     [('        if state == "ok" and value == str(prompt_path):\n            return updated',
       '        if state != "unavailable":\n            return updated')]),

    ("status compares raw text, so escaped Windows paths read as custom",
     [T + "::test_status_decodes_escaped_windows_path",
      T + "::test_status_without_a_toml_parser"],
     PROMPT,
     [('            if decoded == str(prompt_path):', '            if match.group(1) == str(prompt_path):'),
      ('            return "ours" if str(value) == str(prompt_path) else "custom"',
       '            return "ours" if json.dumps(str(prompt_path)) in config_text else "custom"')]),

    ("runner hooks not recognized as ours, so reinstall duplicates them",
     [T + "::test_posix_runner_hooks_are_replaced_not_duplicated"],
     INSTALL,
     [("        _RUNNER_SCRIPT_RE.search(serialized)\n        and _RUNNER_ENTRY_RE.search(serialized)",
       "        False\n        and _RUNNER_ENTRY_RE.search(serialized)")]),

    ("doctor looks only for the old per-script hook names",
     [T + "::test_doctor_sees_consolidated_runners_in_global_hooks"],
     DOCTOR,
     [('("stop_runner.py", "session-end-flush")', '("session-end-flush",)')]),

    ("doctor ignores the global hooks file for features",
     [T + "::test_doctor_sees_consolidated_runners_in_global_hooks"],
     DOCTOR,
     [('    for hooks_path in (codex_home() / "hooks.json", project / ".codex" / "hooks.json"):',
       '    for hooks_path in (project / ".codex" / "hooks.json",):')]),

    ("doctor does not flag a misplaced compact prompt",
     [T + "::test_doctor_flags_misplaced_compact_prompt"],
     DOCTOR,
     [('    if state == "misplaced":\n        return _check("FAIL"', '    if False:\n        return _check("FAIL"')]),

    ("Stop flush outcome never recorded",
     [T + "::test_stop_flush_records_throttled_skip"],
     MEASURE,
     [('        _record_hook_status("stop_flush", _flush_result)\n', "")]),

    ("Codex compact window falls through to the Claude 1M default",
     [T + "::test_codex_compact_window_uses_codex_model_window"],
     MEASURE,
     [('    if detect_runtime() == "codex":\n        # Codex: the Claude table',
       '    if False:\n        # Codex: the Claude table')]),

    ("every cached plugin version counted",
     [T + "::test_plugin_skills_count_only_newest_cached_version"],
     MEASURE,
     [("                    and newest_version.get((rel.parts[0], rel.parts[1])) != rel.parts[2]):",
       "                    and False):")]),

    ("model-pin tip ignores routing plugins",
     [T + "::test_pin_tip_hidden_when_router_plugin_enabled",
      T + "::test_pin_tip_hidden_by_env_opt_out"],
     MEASURE,
     [("                        and not _model_routing_handled_elsewhere()):", "                        ):")]),
]
