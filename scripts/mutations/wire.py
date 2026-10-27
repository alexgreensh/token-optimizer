"""Hook wire contract regressions, planted back by scripts/check_regression_guards.py.

Every entry re-introduces a bug that reached users on the path
manifest command -> sh -> bash -> python-launcher.sh -> run.py -> handler -> stdout,
and names the tests in tests/test_hook_wire_contract.py (which fire the literal
shipped command strings) that must turn red.

Shape: (name, [test node ids], file_relpath, [(old, new), ...]).
"""

WIRE = "tests/test_hook_wire_contract.py"
EVERY_ENTRY = WIRE + "::test_every_manifest_entry_runs_its_handler_and_honours_the_output_contract"

UPS_RUNNER = "hooks/userpromptsubmit_runner.py"
SS_RUNNER = "hooks/sessionstart_runner.py"
STOP_RUNNER = "hooks/stop_runner.py"
CLAUDE_HOOKS = "hooks/hooks.json"
CODEX_PLUGIN_HOOKS = "plugins/token-optimizer/hooks/hooks.json"
CODEX_PLUGIN_RUN = "plugins/token-optimizer/hooks/run.py"
LAUNCHER = "hooks/python-launcher.sh"
MEASURE = "skills/token-optimizer/scripts/measure.py"
CLOUD_KIT = "skills/token-optimizer/scripts/cloud_kit.py"
CODEX_INSTALL = "skills/token-optimizer/scripts/codex_install.py"

_BASH_LOOP = "for b in bash /bin/bash /usr/bin/bash /usr/local/bin/bash /opt/homebrew/bin/bash; do"
# hooks/hooks.json as JSON text: the version resolver every entry runs (5.13.51+)
# and the pre-5.13.51 guard that trusted ${CLAUDE_PLUGIN_ROOT} blindly.
_RESOLVER = (
    'D=\\"${CLAUDE_PLUGIN_ROOT}\\"; [ -r \\"$D/hooks/python-launcher.sh\\" ] || '
    'D=\\"$(ls -d \\"${D%[/\\\\\\\\]*}\\"/*/ 2>/dev/null | grep -E \\"/[0-9]+[.][0-9]+[.][0-9]+/$\\" | sort -V | tail -n1)\\"; '
    'D=\\"${D%/}\\"; [ -r \\"$D/hooks/python-launcher.sh\\" ] || exit 0; '
    'exec \\"$b\\" \\"$D/hooks/python-launcher.sh\\" \\"$D/hooks/run.py\\"'
)
_OLD_GUARD = (
    'L=\\"${CLAUDE_PLUGIN_ROOT}/hooks/python-launcher.sh\\"; [ -r \\"$L\\" ] || exit 0; '
    'exec \\"$b\\" \\"$L\\" \\"${CLAUDE_PLUGIN_ROOT}/hooks/run.py\\"'
)
_UPS_CMD = (
    _BASH_LOOP + ' command -v \\"$b\\" >/dev/null 2>&1 || continue; '
    + _RESOLVER + ' hooks/userpromptsubmit_runner.py; done; exit 0'
)
_UPS_HOOK_TAIL = 'hooks/userpromptsubmit_runner.py; done; exit 0",\n            "timeout": 20\n          }'

MUTATIONS = [
    ("#177 UserPromptSubmit output stamped hookEventName 'SessionStart'",
     [WIRE + "::test_resume_prompt_injects_context_under_the_userpromptsubmit_event[claude]"],
     UPS_RUNNER,
     [('        measure._emit_hook_stdout_envelope,\n        combined,\n        "UserPromptSubmit",',
       '        measure._emit_hook_stdout_envelope,\n        combined,\n        "SessionStart",')]),

    ("#162 SessionStart in a remote session stamped 'UserPromptSubmit' (event from env, not stdin)",
     [WIRE + "::test_session_start_speaks_valid_json_under_its_own_event[compact-cloud-kit]"],
     SS_RUNNER,
     [('    return "SessionStart" if event != "SessionStart" else event',
       '    return "UserPromptSubmit" if os.environ.get("CLAUDE_CODE_REMOTE") else "SessionStart"')]),

    ("#81 Codex SessionStart prints plain '[Token Optimizer] ...' text instead of one JSON object",
     [WIRE + "::test_session_start_speaks_valid_json_under_its_own_event[startup-codex-global]",
      WIRE + "::test_session_start_speaks_valid_json_under_its_own_event[startup-claude]"],
     SS_RUNNER,
     [("        measure._emit_hook_stdout_envelope,\n        combined,\n        _envelope_event(hook_input),",
       "        lambda text, _event: print(text),\n        combined,\n        _envelope_event(hook_input),")]),

    ("#76 Stop hook exits 0 but prints a plain line (Codex fails the hook)",
     [WIRE + "::test_stop_after_real_work_keeps_stdout_host_valid[codex-global]",
      WIRE + "::test_stop_after_real_work_keeps_stdout_host_valid[claude]"],
     STOP_RUNNER,
     [("        for buf in _stdout_bufs:\n            sys.stdout.write(buf)",
       "        sys.stdout.write('[Token Optimizer] checkpoint saved\\n')\n"
       "        for buf in _stdout_bufs:\n            sys.stdout.write(buf)")]),

    # The Codex marketplace plugin runs its OWN copies of the runners (the
    # plugins/token-optimizer mirror), so the Codex-host variants plant there.
    ("#81 (Codex plugin copy) SessionStart prints plain text instead of one JSON object",
     [WIRE + "::test_session_start_speaks_valid_json_under_its_own_event[startup-codex-plugin]"],
     "plugins/token-optimizer/" + SS_RUNNER,
     [("        measure._emit_hook_stdout_envelope,\n        combined,\n        _envelope_event(hook_input),",
       "        lambda text, _event: print(text),\n        combined,\n        _envelope_event(hook_input),")]),

    ("#76 (Codex plugin copy) Stop hook exits 0 but prints a plain line",
     [WIRE + "::test_stop_after_real_work_keeps_stdout_host_valid[codex-plugin]"],
     "plugins/token-optimizer/" + STOP_RUNNER,
     [("        for buf in _stdout_bufs:\n            sys.stdout.write(buf)",
       "        sys.stdout.write('[Token Optimizer] checkpoint saved\\n')\n"
       "        for buf in _stdout_bufs:\n            sys.stdout.write(buf)")]),

    ("#83 async hook in the Codex-loaded manifest (Codex skips it)",
     [WIRE + "::test_codex_loaded_manifests_carry_no_async_hooks"],
     CODEX_PLUGIN_HOOKS,
     [('checkpoint-trigger --milestone pre-fanout --quiet; done; exit 0",\n            "timeout": 20',
       'checkpoint-trigger --milestone pre-fanout --quiet; done; exit 0",\n            "async": true,\n'
       '            "timeout": 20')]),

    ("#80 hooks.json calls bare `bash` (PATH without bash -> exit 127)",
     [WIRE + "::test_hooks_run_when_path_has_no_bash[claude]"],
     CLAUDE_HOOKS,
     [(_BASH_LOOP + ' command -v \\"$b\\" >/dev/null 2>&1 || continue; ' + _RESOLVER
       + ' skills/token-optimizer/scripts/bash_hook.py',
       'for b in bash; do command -v \\"$b\\" >/dev/null 2>&1 || continue; ' + _RESOLVER
       + ' skills/token-optimizer/scripts/bash_hook.py')]),

    ("#80 class in the cloud kit: settings hook execs bare `sh` (stripped PATH -> exit 127)",
     [WIRE + "::test_hooks_run_when_path_has_no_bash[cloud-kit]"],
     CLOUD_KIT,
     [('return "[ -r %s ] || exit 0; exec /bin/sh %s %s" % (r, r, " ".join(args))',
       'return "[ -r %s ] || exit 0; exec sh %s %s" % (r, r, " ".join(args))')]),

    ("#80 class in the cloud kit: run.sh execs bare `bash`",
     [WIRE + "::test_hooks_run_when_path_has_no_bash[cloud-kit]"],
     CLOUD_KIT,
     [('for b in bash /bin/bash /usr/bin/bash /usr/local/bin/bash; do\n  command -v "$b" >/dev/null 2>&1 || continue\n'
       '  exec "$b" "$L" "$R/hooks/run.py" "$@"\ndone\nexit 0\n',
       'exec bash "$L" "$R/hooks/run.py" "$@"\n')]),

    ("cloud: settings hooks lean on ${CLAUDE_PLUGIN_ROOT}, which cloud VMs leave unset (P4)",
     [WIRE + "::test_settings_level_hooks_run_without_plugin_root[cloud-kit]"],
     CLOUD_KIT,
     [('return "[ -r %s ] || exit 0; exec /bin/sh %s %s" % (r, r, " ".join(args))',
       'return \'L="${CLAUDE_PLUGIN_ROOT}/hooks/python-launcher.sh"; [ -r "$L" ] || exit 0; '
       'exec /bin/sh %s %s\' % (r, " ".join(args))')]),

    ("#92 class on Claude Code: no readable-launcher check, a missing root exits 127",
     [WIRE + "::test_plugin_manifest_without_plugin_root_exits_clean[claude]"],
     CLAUDE_HOOKS,
     [('D=\\"${D%/}\\"; [ -r \\"$D/hooks/python-launcher.sh\\" ] || exit 0; '
       'exec \\"$b\\" \\"$D/hooks/python-launcher.sh\\" \\"$D/hooks/run.py\\" hooks/sessionstart_runner.py',
       'D=\\"${D%/}\\"; '
       'exec \\"$b\\" \\"$D/hooks/python-launcher.sh\\" \\"$D/hooks/run.py\\" hooks/sessionstart_runner.py')]),

    ("#92 #75 on Claude Code: one entry trusts the root an update deleted (no sibling fallback)",
     [WIRE + "::test_claude_plugin_hooks_survive_an_upgrade_mid_session"],
     CLAUDE_HOOKS,
     [(_RESOLVER + ' hooks/stop_runner.py; done; exit 0",\n            "timeout": 15',
       _OLD_GUARD + ' hooks/stop_runner.py; done; exit 0",\n            "timeout": 15')]),

    ("#92 #75 codex-install pins the versioned plugin dir an upgrade deletes",
     [WIRE + "::test_codex_global_hooks_survive_an_upgrade"],
     CODEX_INSTALL,
     [("f'R=\"$(ls -d {base}/*/ 2>/dev/null", "f'R=\"$(ls -d {base}/NONE/ 2>/dev/null")]),

    ("Codex plugin: upgrade mid-session, run.py trusts the deleted CLAUDE_PLUGIN_ROOT and no-ops",
     [WIRE + "::test_codex_plugin_hooks_survive_an_upgrade_mid_session"],
     CODEX_PLUGIN_RUN,
     [("    stale_plugin_root = bool(plugin_root) and not os.path.isdir(plugin_root)",
       "    stale_plugin_root = False")]),

    ("#145 launcher rejects a user-owned version-manager python shim (mise)",
     [WIRE + "::test_version_manager_python_shim_is_the_interpreter_used[claude]"],
     LAUNCHER,
     [('        _to_owned_unwritable "$binpath" && return 0', "        :")]),

    ("#139 several UserPromptSubmit entries, a process chain each, on every prompt",
     [WIRE + "::test_hot_path_events_stay_within_a_process_budget[claude]"],
     CLAUDE_HOOKS,
     [(_UPS_HOOK_TAIL,
       _UPS_HOOK_TAIL + ',\n          {"type": "command", "command": "' + _UPS_CMD + '", "timeout": 20}')]),

    ("#53 the foreign-status-line advisory is re-injected on every prompt",
     [WIRE + "::test_prompts_do_not_reinject_the_same_advisory_every_turn"],
     UPS_RUNNER,
     [("                not _is_plugin\n                and not _qb_disabled", "                not _qb_disabled"),
      ("                    measure.setup_quality_bar(quiet=True)", "                    measure.setup_quality_bar()")]),

    ("#253 compact-capture exits 0 without writing the checkpoint",
     [WIRE + "::test_checkpoint_hooks_really_write_the_checkpoint[claude]"],
     MEASURE,
     [("    if not _write_checkpoint_atomic(checkpoint_path, checkpoint_content):\n        return None",
       "    if True:\n        return None")]),

    ("Codex plugin Stop: rollout transcript refused by a runtime-only confinement, no checkpoint (#253 class)",
     [WIRE + "::test_checkpoint_hooks_really_write_the_checkpoint[codex-plugin]"],
     "plugins/token-optimizer/" + MEASURE,
     [("    if _use_codex_session_adapter(resolved):\n        try:\n            roots = [r.resolve() for r in codex_session.session_roots()]",
       "    if _use_codex_session_adapter():\n        try:\n            roots = [r.resolve() for r in codex_session.session_roots()]")]),

    ("resolver $(...) collapses every hook to one dedup identity (setup_all_hooks/cleanup)",
     ["tests/test_hook_launcher_failopen_guard.py::test_resolver_keeps_per_handler_dedup_identity"],
     MEASURE,
     [("    if 'D=\"$(ls -d ' in cmd:", "    if False:")]),
]
