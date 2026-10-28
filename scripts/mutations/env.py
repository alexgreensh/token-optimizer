"""Environment-variance / runtime-detection regressions that shipped before.

Each entry re-introduces one escaped bug; the listed tests must FAIL on it.
Shape: (name, [test node ids], file_relpath, [(old, new), ...]).
"""

RUNTIME_ENV = "skills/token-optimizer/scripts/runtime_env.py"
MEASURE = "skills/token-optimizer/scripts/measure.py"
MATRIX = "tests/test_env_fingerprint_matrix.py"

_REMOTE_HOST_HEAD = '        env_type = os.environ.get(_REMOTE_ENV_TYPE_ENV, "").strip().lower()\n'

MUTATIONS = [
    ("#176 native CLI hook read as Cowork (AI_AGENT _harness marker)",
     [MATRIX + "::test_fresh_process_detects_the_host[cli]",
      MATRIX + "::test_compression_rows_carry_the_hosts_origin[cli]"],
     RUNTIME_ENV,
     [(_REMOTE_HOST_HEAD,
       '        _ai = os.environ.get("AI_AGENT", "")\n'
       '        if "claude-code" in _ai and "harness" in _ai:\n'
       '            return REMOTE_HOST_COWORK\n' + _REMOTE_HOST_HEAD)]),

    ("2026-10-19 cloud VM read as Cowork (container id checked before cloud evidence)",
     [MATRIX + "::test_fresh_process_detects_the_host[cloud]",
      MATRIX + "::test_compression_rows_carry_the_hosts_origin[cloud]"],
     RUNTIME_ENV,
     [(_REMOTE_HOST_HEAD,
       '        if os.environ.get(_COWORK_CONTAINER_ENV, "").strip():\n'
       '            return REMOTE_HOST_COWORK\n' + _REMOTE_HOST_HEAD)]),

    ("2026-10-19 CLAUDE_CODE_REMOTE=true read as Cowork",
     [MATRIX + "::test_fresh_process_detects_the_host[cloud]",
      MATRIX + "::test_cloud_vm_session_is_lean"],
     RUNTIME_ENV,
     [(_REMOTE_HOST_HEAD,
       '        if _truthy_env(_COWORK_REMOTE_ENV):\n'
       '            return REMOTE_HOST_COWORK\n' + _REMOTE_HOST_HEAD)]),

    ("trends rows lose their origin (every VM row stored as local)",
     [MATRIX + "::test_compression_rows_carry_the_hosts_origin[cloud]",
      MATRIX + "::test_compression_rows_carry_the_hosts_origin[cowork]"],
     RUNTIME_ENV,
     [('    return remote_host() or "local"\n', '    return "local"\n')]),

    ("cloud VM runs the desktop-only daemon / scheduler work",
     [MATRIX + "::test_cloud_vm_session_is_lean"],
     MEASURE,
     [("            return False\n        return is_claude_code_cloud()\n",
       "            return False\n        return False\n")]),

    ("#198 CLAUDE_CONFIG_DIR ignored (state split into ~/.claude)",
     [MATRIX + "::test_fresh_process_detects_the_host[cli_config_dir]",
      MATRIX + "::test_session_state_stays_in_the_hosts_own_home[cli_config_dir]"],
     RUNTIME_ENV,
     [('    raw = os.environ.get(_CLAUDE_CONFIG_DIR_ENV, "").strip()\n    if not raw:\n        return fallback\n',
       '    raw = ""\n    if not raw:\n        return fallback\n')]),

    ("#249 settings autoCompactWindow ignored (fill measured against the model window)",
     [MATRIX + "::test_context_fill_uses_the_users_compact_window[settings_autoCompactWindow-cli]",
      MATRIX + "::test_context_fill_uses_the_users_compact_window[settings_autoCompactWindow-cli_config_dir]"],
     MEASURE,
     [('            top_val = settings.get("autoCompactWindow")', '            top_val = None')]),

    ("#249 compact-window % labelled with the model window",
     [MATRIX + "::test_context_fill_uses_the_users_compact_window[settings_autoCompactWindow-cli]"],
     MEASURE,
     [('    if not cached.get("compact_window_reduced"):\n        return ""\n', '    return ""\n')]),

    ("#209 CLAUDE_CODE_AUTO_COMPACT_WINDOW env ignored",
     [MATRIX + "::test_context_fill_uses_the_users_compact_window[env_auto_compact_window-cli]"],
     MEASURE,
     [('        for var in ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"):',
       '        for var in ("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",):')]),

    ("#120 skill preamble: Claude Bash tool with ~/.codex on disk resolves to codex",
     [MATRIX + "::test_skill_preamble_picks_the_running_harness[claude_bash_tool_codex_on_disk-commands/quick.md]",
      MATRIX + "::test_quick_command_end_to_end_in_a_claude_bash_tool"],
     "commands/quick.md",
     [('  elif [ -n "$CLAUDECODE" ] || [ -n "$CLAUDE_CODE_ENTRYPOINT" ] || [ -n "$CLAUDE_CODE_SESSION_ID" ]; then\n'
       '    RUNTIME="claude"\n', "")]),

    ("#120 skill preamble (token-dashboard copy) loses the CLAUDECODE tier",
     [MATRIX + "::test_skill_preamble_picks_the_running_harness[claude_bash_tool_codex_on_disk-skills/token-dashboard/SKILL.md]"],
     "skills/token-dashboard/SKILL.md",
     [('[ -n "$CLAUDECODE" ] || [ -n "$CLAUDE_CODE_ENTRYPOINT" ] || [ -n "$CLAUDE_CODE_SESSION_ID" ]',
       '[ -n "$CLAUDE_CODE_TO_NEVER_SET" ]')]),

    ("#57 skill preamble: OpenCode env signal checked after the ~/.codex heuristic",
     [MATRIX + "::test_skill_preamble_picks_the_running_harness[opencode_env_signal-commands/health.md]"],
     "commands/health.md",
     [('  elif [ -n "$OPENCODE" ] || [ -n "$OPENCODE_BIN" ] || [ -n "$OPENCODE_CONFIG_DIR" ] || [ -n "$OPENCODE_CONFIG" ]; then\n'
       '    RUNTIME="opencode"\n', "")]),

    ("#120 Python: Claude Bash tool with ~/.codex on disk detected as codex",
     [MATRIX + "::test_python_detection_in_a_claude_bash_tool_ignores_codex_on_disk"],
     RUNTIME_ENV,
     [("    if any(os.environ.get(v) for v in _CLAUDE_CODE_ENVS):\n        return _RUNTIME_CLAUDE\n",
       "    if (_safe_home() / \".codex\").is_dir():\n        return _RUNTIME_CODEX\n")]),

    ("#140 a foreign CLAUDE_PLUGIN_DATA is adopted (expand finds nothing)",
     [MATRIX + "::test_archived_result_expands_when_a_foreign_plugin_data_leaked_in"],
     "skills/token-optimizer/scripts/plugin_env.py",
     [("    registered = {r.resolve(strict=False) for r in _registered_plugin_data_dirs()}\n"
       "    if resolved in registered:\n        return resolved\n",
       "    return resolved\n"),
      # The read side's sibling search (a later defence layer) goes too: the
      # 5.11.93 CLI read only the active identity, which is what the user hit.
      ('    candidates.extend(path / "data" for path in find_sibling_plugin_data_dirs(active))\n', ""),
      # So does expand's cross-runtime fallback (5.13.59), which re-scans
      # every token-optimizer-* identity under each runtime's home.
      ("            for path in sorted(base.glob(f\"{_PLUGIN_NAME}-*\"), key=lambda p: p.name):\n",
       "            for path in ():\n")]),

    ("#57 class: a Codex plugin hook is detected as Claude (state + statusLine into ~/.claude)",
     [MATRIX + "::test_fresh_process_detects_the_host[codex_plugin]",
      MATRIX + "::test_session_state_stays_in_the_hosts_own_home[codex_plugin]"],
     RUNTIME_ENV,
     [("    if _codex_plugin_env_signal():\n        return _RUNTIME_CODEX\n", "")]),

    ("#73 ps under a non-English locale: localized lstart drops every session",
     [MATRIX + "::test_health_sees_terminal_and_desktop_wsl_sessions_in_any_locale[he_IL.UTF-8]"],
     MEASURE,
     [('            # and dropping every session.\n'
       '            env={**os.environ, "LC_ALL": "C", "LC_TIME": "C"}, creationflags=_NO_WINDOW,',
       '            # and dropping every session.\n'
       '            env={**os.environ}, creationflags=_NO_WINDOW,')]),

    ("#192 Claude Desktop WSL 2 ccd-cli launcher not recognised as a session",
     [MATRIX + "::test_health_sees_terminal_and_desktop_wsl_sessions_in_any_locale[en_US.UTF-8]"],
     MEASURE,
     [('_CCD_CLI_LAUNCHER_SEGMENT = "/.claude/remote/ccd-cli/"',
       '_CCD_CLI_LAUNCHER_SEGMENT = "/.claude/remote/ccd-cli-unknown/"')]),

    ("#93 Umbrel: the HERMES_HOME opt-in for a home above $HOME stops working",
     [MATRIX + "::test_umbrel_hermes_home_above_home[1-opt/data]"],
     RUNTIME_ENV,
     [("        and _truthy_env(_UNSAFE_RUNTIME_HOME_OVERRIDE_ENV)\n",
       "        and _truthy_env(_UNSAFE_RUNTIME_HOME_OVERRIDE_ENV) and False\n")]),

    ("checkpoint git label follows git's auto-lengthened --short",
     [MATRIX + "::test_checkpoint_git_label_ignores_abbrev_length_and_keeps_unicode_branch"],
     MEASURE,
     [('r = subprocess.run(["git", "rev-parse", "HEAD", "--abbrev-ref", "HEAD"]',
       'r = subprocess.run(["git", "rev-parse", "--short", "HEAD", "--abbrev-ref", "HEAD"]'),
      ("return out[1].strip() or None, out[0].strip()[:7] or None",
       "return out[1].strip() or None, out[0].strip() or None")]),

    ("#177 prompt hook envelope named SessionStart (host rejects it)",
     [MATRIX + "::test_every_hook_envelope_names_the_event_that_fired_it[cli]"],
     "hooks/userpromptsubmit_runner.py",
     [("        combined,\n        \"UserPromptSubmit\",\n", "        combined,\n        \"SessionStart\",\n")]),

    ("#141 PreToolUse Bash hook rewrites commands in a worktree-isolated session",
     [MATRIX + "::test_worktree_isolated_session_bash_commands_pass_through[cli]"],
     "skills/token-optimizer/scripts/bash_hook.py",
     [("    # Never print: any PreToolUse output here would be a permission decision.\n    return\n",
       "    print('{\"hookSpecificOutput\": {\"hookEventName\": \"PreToolUse\", \"updatedInput\": "
       "{\"command\": \"for b in bash; do exec $b ls; done\"}}}')\n    return\n")]),

    ("#148 class: Copilot installer ships the bridge without spawn_utils / hook_runtime",
     [MATRIX + "::test_installed_bridge_payload_imports_without_the_repo[copilot_install]",
      MATRIX + "::test_copilot_install_then_hooks_run_clean_and_leave_claude_alone"],
     "skills/token-optimizer/scripts/copilot_install.py",
     [('    "spawn_utils.py",\n    "hook_runtime.py",\n', "")]),

    ("#148 class: Cursor installer ships bash_compress without credential_patterns",
     [MATRIX + "::test_installed_bridge_payload_imports_without_the_repo[cursor_install]"],
     "skills/token-optimizer/scripts/cursor_install.py",
     [('    "credential_patterns.py",\n', "")]),

    ("Codex plugin users get the Claude Code '/plugin -> Marketplaces' first-run tip",
     [MATRIX + "::test_every_hook_envelope_names_the_event_that_fired_it[codex_plugin]"],
     MEASURE,
     [('                and detect_runtime() == "claude"\n                and _is_running_from_plugin_cache()',
       '                and _is_running_from_plugin_cache()')]),

    ("#57 OpenCode beside Claude Code: a stray CLAUDE_PLUGIN_ROOT beats the opencode ancestor",
     [MATRIX + "::test_opencode_session_beside_claude_code_leaves_claude_untouched"],
     RUNTIME_ENV,
     [("    if _opencode_process_signal():\n        return _RUNTIME_OPENCODE\n\n    # Codex loads", "    # Codex loads")]),

    ("Bug 1 repair: a script install keeps a statusLine that runs the Codex plugin cache's copy",
     [MATRIX + "::test_claude_session_repoints_statusline_from_codex_cache[script_install]"],
     MEASURE,
     [("        if Path(old_path).expanduser().is_file() and not in_codex:",
       "        if Path(old_path).expanduser().is_file():")]),

    ("Bug 1 repair: ensure-health never calls the dangling-statusLine heal",
     [MATRIX + "::test_claude_session_repoints_statusline_to_a_deleted_file[plugin_cache]",
      MATRIX + "::test_claude_session_repoints_statusline_to_a_deleted_file[script_install]"],
     MEASURE,
     [("            if _heal_dangling_statusline():", "            if False:")]),

    ("Bug 1 repair overreaches: an existing dev-clone statusline.js gets re-pointed",
     [MATRIX + "::test_claude_session_leaves_other_statuslines_alone[dev_clone]"],
     MEASURE,
     [("        if Path(old_path).expanduser().is_file() and not in_codex:\n            return False\n", "")]),

    ("Bug 1 repair overreaches: another tool's missing statusline.js gets taken over",
     [MATRIX + "::test_claude_session_leaves_other_statuslines_alone[foreign_missing_js]"],
     MEASURE,
     [('        if not _statusline_cmd_is_ours(cmd):\n            return False\n        found = _statusline_js_in_command(cmd)',
       '        if "statusline.js" not in cmd:\n            return False\n        found = _statusline_js_in_command(cmd)')]),

    ("doctor reports a status line running a missing file as healthy",
     [MATRIX + "::test_doctor_flags_a_status_line_running_a_missing_file"],
     MEASURE,
     [('    if qb["statusline"] and _qb_sl_path and not Path(_qb_sl_path).expanduser().is_file():',
       '    if False:')]),

    ("doctor stays quiet about a status line running another Token Optimizer checkout",
     [MATRIX + "::test_doctor_names_another_token_optimizer_checkout"],
     MEASURE,
     [('            _qb_note = f" (runs another Token Optimizer copy: {_qb_sl_path})"',
       '            _qb_note = ""')]),

    ("Skill preamble (quick.md) says claude under COPILOT_HOME and overrides Python's copilot",
     [MATRIX + "::test_skill_preamble_picks_the_running_harness[copilot_nested_in_claude-commands/quick.md]",
      MATRIX + "::test_skill_preamble_picks_the_running_harness[copilot_home_codex_on_disk-commands/quick.md]"],
     "commands/quick.md",
     [('  elif [ -n "$COPILOT_HOME" ] || [ -n "$TOKEN_OPTIMIZER_COPILOT_HOME" ]; then\n    RUNTIME="copilot"\n', "")]),

    ("Skill preamble (token-coach) says codex under HERMES_HOME with ~/.codex on disk",
     [MATRIX + "::test_skill_preamble_picks_the_running_harness[hermes_home_codex_on_disk-skills/token-coach/SKILL.md]"],
     "skills/token-coach/SKILL.md",
     [('elif [ -n "$HERMES_HOME" ]; then', 'elif false; then')]),

    ("Skill preamble (fleet-auditor) misses GROK_HOME / Cursor / Antigravity homes",
     [MATRIX + "::test_skill_preamble_picks_the_running_harness[grok_home-skills/fleet-auditor/SKILL.md]",
      MATRIX + "::test_skill_preamble_picks_the_running_harness[cursor_to_home-skills/fleet-auditor/SKILL.md]",
      MATRIX + "::test_skill_preamble_picks_the_running_harness[antigravity_to_home-skills/fleet-auditor/SKILL.md]"],
     "skills/fleet-auditor/SKILL.md",
     [('elif [ -n "$TOKEN_OPTIMIZER_CURSOR_HOME" ]; then', 'elif false; then'),
      ('elif [ -n "$TOKEN_OPTIMIZER_ANTIGRAVITY_HOME" ]; then', 'elif false; then'),
      ('elif [ -n "$GROK_HOME" ] || [ -n "$TOKEN_OPTIMIZER_GROK_HOME" ]; then', 'elif false; then')]),

    ("Python detector loses COPILOT_HOME, so preamble and Python disagree",
     [MATRIX + "::test_skill_preamble_agrees_with_python_detector[copilot_home]"],
     RUNTIME_ENV,
     [('_COPILOT_HOME_ENV = "COPILOT_HOME"', '_COPILOT_HOME_ENV = "COPILOT_HOME_X"')]),

    ("Copilot install bakes a world-writable python into the hooks file",
     [MATRIX + "::test_copilot_install_refuses_a_world_writable_interpreter"],
     "skills/token-optimizer/scripts/py_trust.py",
     [("    return py_trust_reason(p) is None", "    return True")]),
]
