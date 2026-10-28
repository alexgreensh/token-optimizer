# rel59 unit: windows-sweep (Token Optimizer 5.13.59)

Branch: `claude/rel59-checkpoint-diagnostics-ku6t9e` (based on `origin/integ/5.13.59`).
The brief named `rel59/windows`; this session's harness only allows pushing to the branch above, so the work lives there (see Decisions).

## Summary

- #278 is now fixed for every path we could model, including the `$`, backtick, `%` and `!` paths PR #280 still left on the cmd.exe-only form.
- Codex Stop never writes a "No transcript data available" placeholder.
- The runtime sweep found and fixed 9 more Windows bugs. The biggest: Copilot CLI hooks never fired on Windows, and Claude Code plugin hooks went silent after an update.
- 16 new regression mutations are registered in `scripts/mutations/windows_sweep_rel59.py`. Each is proven caught by `scripts/check_regression_guards.py`.
- A test-environment leak was hiding guards: `CLAUDE_CODE_REMOTE` from an agent session made the #117 guard pass on broken code. `tests/conftest.py` now drops the host agent's own variables.

## Step 1: #278

### How Codex really launches a hook

Read from openai/codex `codex-rs/hooks/src/engine/command_runner.rs`:

- With no configured shell, Codex runs `%COMSPEC%` (fallback `cmd.exe`) as `/C` plus `raw_arg("\"<command>\"")`. The command line is passed raw, wrapped in one extra pair of quotes, which cmd strips.
- With a configured session shell (PowerShell), Codex passes the configured args and then the command as one ordinary argument, using Rust MSVC quoting, which PowerShell undoes.

So PR #280's shape (a bare interpreter, then double-quoted arguments) is right for both shells. The reporter's failing "argv LIST" cmd case is not how Codex launches hooks.

### What was still broken, and the fix

1. **Paths with `$`, a backtick, `%` or `!`** (usually from the Windows user name) still fell back to the cmd-only `set ... &&` form, which exits 1 under PowerShell. The command builder (`codex_install._neutral_launcher_command`) now tries, in order:
   1. the literal double-quoted path;
   2. the 8.3 short name of the folder, keeping the version or launcher file name long;
   3. for the launcher only, a readable bootstrap: `python.exe -c "import os,runpy;runpy.run_path(os.path.join(os.environ['USERPROFILE'],'.codex',...,'windows-launcher.py'),run_name='__main__')"`. Python reads the folder from the environment, so neither shell ever sees the character. In this case `--baked-root` is written as just the version name, and the launcher resolves it next to itself (`hooks/windows-launcher.py`).
   - The bootstrap is plain source with no base64, so #183 stays fixed (test below).
   - Short names alone are not enough: `$`, `%`, `!` and the backtick are legal 8.3 characters, so `Jo$h` keeps its `$`.
2. **No PowerShell form is possible only when Python itself sits in such a folder**, for example a per-user Python under `C:\Users\Jo$h\...`. The cmd-only form is kept so cmd still works, and the doctor now says exactly what to do: "Install Python in a plain folder such as C:\Python313 (no spaces and none of $ ` % !), run that Python's measure.py codex-install again, then restart Codex". For a plugin folder outside every environment base, it tells the user to set CODEX_HOME to a plain folder. Code: `codex_install.explain_cmd_only_command` and `codex_doctor._hook_shell_check`.
3. **Development-checkout installs** still wrote the cmd-only form. They now use the same shell-neutral launcher command through the checkout's own `hooks/windows-launcher.py`. The launcher never trades a non-version root for a version-named sibling folder.
4. **Stop placeholder.** With a session id but no findable transcript, `compact_capture` still wrote `<sid>-...-stop.md` containing "No transcript data available". On Codex it now writes nothing and records why in hook-status, for the doctor (never the id or path). The Stop runner already forwarded the identity (PR #280). An end-to-end test shows that the session id alone finds the real rollout.
5. PowerShell 5.1 may drop a bare `--` before the native program sees it. The launcher already copes (it treats the first unknown token as the script). A test now proves it.

### Shell models and the path matrix

`tests/_win_shells.py` models three ways a hook command can run. The models are conservative: anything not provably literal raises.

- **cmd.exe as Codex runs it.** cmd strips the outer quotes, expands `%VAR%` (and `!VAR!` with delayed expansion), refuses unquoted `& | < >`, removes `^`, then splits arguments the MSVCRT way.
- **Windows PowerShell 5.1.** Command-mode tokenizing: a quoted first token is an expression; `$` and the backtick expand inside double quotes; typographic quotes count as quotes. Legacy native-argument passing: the first bare `--` and empty arguments are dropped, and arguments with spaces are wrapped in quotes without escaping.
- **PowerShell 7.** The same tokenizing, with standard argument passing.

`tests/test_codex_windows_shell_matrix_278.py` runs 13 user folders against 3 interpreter locations, with 8.3 names on and off, through all 4 shell models. The folders include spaces, é/ü, CJK, `'`, `R&D (Lab)`, `$`, `%`, `a%PATH%b`, `!`, the backtick, `’` and combinations. For each case it checks that python.exe ends up running the right launcher, install folder, script, arguments and environment pairs.

## Step 2: runtime sweep (what runs each command on Windows)

| Runtime | Shell on Windows | Status |
|---|---|---|
| Codex CLI and desktop | Configured session shell (PowerShell `-Command`), else `cmd /C` raw | Fixed (Step 1) |
| Claude Code CLI and desktop, plugin `hooks/hooks.json` | Git Bash (the outer `for` loop runs in sh) | Fixed: upgrade fallback with a backslash `CLAUDE_PLUGIN_ROOT` |
| Claude Code settings.json hooks (smart compact, quality bar, status line) | Git Bash | Fixed: real interpreter instead of `python3`; apostrophe-safe status line |
| Copilot CLI | Its `powershell` field | Fixed: the field was never written |
| Cursor, Grok, Antigravity, Devin | Installers refuse native Windows | No change |
| OpenCode, OpenClaw, VS Code extension, desktop mod | Node `child_process` with argv, `windowsHide` set | No defect found |
| Cowork | Mirror of `hooks/` + `skills/` | Regenerated |

Other checks:

- **CREATE_NO_WINDOW:** every hook and runner path passes `creationflags` (spawn_utils / `_NO_WINDOW`). Only the antigravity and devin doctors lack it, and both refuse Windows or run from a terminal.
- **POSIX-only calls:** `fchmod`, `getuid` and `fcntl` are all guarded (msvcrt fallback present).
- **New fix:** `os.kill(pid, 0)` was used as a liveness probe at 6 sites. On Windows it sends CTRL_C_EVENT, so a detached hook read a live lock owner as dead. All 6 now call `_pid_is_running`, which uses OpenProcess and GetExitCodeProcess on Windows.
- **New fix:** `purge --force` asked POSIX `ps` for the daemon's name, so on Windows it never stopped the daemon.
- **Store Python stubs (#261, #247):** guarded by the desktop-mod TS tests. The hook launcher now also accepts the default conda, Scoop and uv folders.
- **UTF-8 (#105):** covered by the re-exec tests. New fix: `dashboard-launcher.cmd` switches to code page 65001 on an ASCII line before any path appears.
- **CLAUDE_CONFIG_DIR (#198, #265):** stragglers listed under "Could not do".

## Fixes and their proving tests (each red without the fix, green with it)

| Fix | Tests | Mutation in windows_sweep_rel59.py |
|---|---|---|
| `$ % ` ! ` paths get a PowerShell-safe command | `test_codex_windows_shell_matrix_278.py::test_generated_command_means_the_same_in_every_codex_shell[*]`, `::test_expanding_characters_no_longer_force_the_cmd_only_form[*]`, `::test_bootstrap_command_runs_the_hook_end_to_end` | yes |
| 8.3 names for launcher and install folders | `::test_short_names_are_preferred_over_the_bootstrap`, `::test_heal_uses_short_names_for_old_commands_with_expanding_characters` | yes |
| Relative `--baked-root` | `::test_launcher_resolves_a_relative_baked_root_next_to_itself` | yes |
| Dev checkout runs in PowerShell, pinned | `::test_dev_checkout_command_runs_the_checkout_in_every_shell`, `test_codex_shell_neutral_hooks_278.py::test_doctor_flags_generated_dev_checkout_command` | yes |
| Doctor names the exact fix | `::test_generated_command_means_the_same_in_every_codex_shell` (cmd-only branch), `::test_bootstrap_folder_outside_every_base_falls_back_with_exact_advice`, `::test_heal_never_writes_the_bootstrap_form` | (covered) |
| No placeholder checkpoint on Codex | `test_hook_wire_contract.py::test_codex_stop_finds_the_transcript_from_the_session_id_alone[codex-global,codex-plugin]`, `test_codex_shell_neutral_hooks_278.py::test_compact_capture_writes_no_unknown_placeholder_on_codex` | yes |
| Copilot `powershell` field | `test_copilot_windows_powershell_hook.py` (path matrix, install, real PowerShell run on Windows CI) | yes |
| Claude plugin fallback with backslash root | `test_hook_wire_contract.py::test_claude_plugin_hooks_survive_an_upgrade_with_a_windows_style_root` | yes |
| Liveness without signals; purge --force | `test_windows_pid_liveness.py` (3 tests, one runs on the real OS) | yes (2) |
| settings.json hooks use the real Python | `test_windows_settings_hook_interpreter.py` | yes |
| Status line with an apostrophe | `test_windows_settings_hook_interpreter.py::test_status_line_command_survives_an_apostrophe_in_the_path` | yes |
| dashboard .cmd code page and quoting | `test_windows_dashboard_launcher_cmd.py` | yes |
| conda / Scoop / uv allowlist | `test_python_launcher_windows_pythonw.py::test_safe_prefix_accepts_default_windows_conda_scoop_uv_installs`, `::test_safe_prefix_still_refuses_lookalike_windows_dirs` | yes |
| Agent env leak (makes #117 guard real) | `check_regression_guards.py --only "#117"`: caught; with `TO_TEST_KEEP_HOST_ENV=1` it reports "guard tests PASSED on the broken code" | n/a |

## Step 3: closed issue -> guarding test

All verdicts were checked by reading the tests. "Mutation" means a registered entry that the harness proves is caught.

| Issue | Guard |
|---|---|
| #278 | `test_codex_windows_shell_matrix_278.py`, `test_codex_shell_neutral_hooks_278.py`, wire test above (mutations) |
| #276 | `test_codex_install_issue_276.py`, `test_windows_hook_launcher.py::test_windows_codex_command_never_starts_with_a_quote` (mutations) |
| #180 | `test_windows_hook_launcher.py::test_windows_versioned_hook_executes_through_comspec` (real cmd, Windows CI), `test_codex_windows_command.py`; models in the matrix test run on Linux too |
| #192 | `test_env_fingerprint_matrix.py::test_health_sees_terminal_and_desktop_wsl_sessions_in_any_locale`, `test_session_locale_and_hooks.py::test_ccd_cli_launcher_detected_issue_192` |
| #107, #104 | `test_windows_spawn_no_window.py`, `test_hook_scripts_no_window.py`, `test_hooks_root_no_window.py`, `test_bridges_no_window.py`, `test_measure_no_window.py` |
| #183 | `test_windows_hook_launcher.py::test_installer_generator_source_cannot_regress_to_encoded_exec` and related; new `test_codex_windows_shell_matrix_278.py::test_bootstrap_is_plain_source_with_no_encoded_payload` |
| #253 | `test_false_success.py::test_precompact_hook_writes_the_checkpoint_the_restore_injects`, `test_hook_wire_contract.py::test_checkpoint_hooks_really_write_the_checkpoint` |
| #261, #247 | `desktop/token-optimizer-desktop/tests/pure/data.spec.ts` (desktop-mod-pure CI job) |
| #118 | `test_windows_hook_launcher.py::test_claude_windows_hook_command_executes_under_bash_without_nul_file`, `::test_measure_py_has_no_cmd_null_redirect_anywhere` |
| #246 | `test_tool_call_thresholds_lazy.py`, `test_swallowed_errors.py` |
| #215 | `test_windows_spawn_no_window.py::test_run_py_spawn_nt_uses_detached_process`, `test_windows_detached_child_e2e.py` |
| #188 | `test_windows_hook_launcher.py::test_windows_version_resolver_skips_incomplete_newest_upgrade` (+ variants) |
| #264 | `test_cold_compile_gate.py`, `test_module_runner_guard.py::test_pycache_written_despite_pythondontwritebytecode` |
| #78 | **New** `test_install_sh_copilot_wsl_issue78.py` (5 tests, mutation); Python side `test_copilot_home_env_collision.py`, `test_runtime_env_wsl_mnt.py` |
| #288 (open) | Out of this unit's scope (checkpoint topic unit) |
| #265 | `test_msix_project_dir.py` |
| #211 | `test_windows_session_identity.py::test_desktop_app_collapses_to_the_three_real_sessions` |
| #198 | `test_statusline_claude_config_dir.py`, `test_env_fingerprint_matrix` `[cli_config_dir]` |
| #287 (open) | Out of scope (advisor double-count unit) |
| #249 | `test_context_window_disclosure.py` |
| #144 | `test_windows_fchmod_guard.py::test_dashboard_generation_survives_missing_fchmod` |
| #205 | `opencode/src/v2.test.ts` (feature) |
| #204 | `test_dashboard_selfheal_singleflight.py::test_rebuild_lock_stays_held_past_marker_window` |
| #145 | `test_hook_wire_contract.py::test_version_manager_python_shim_is_the_interpreter_used` |
| #117 | `test_windows_process_enum.py::test_windows_sessions_use_get_process_not_tasklist`, `test_hot_path_process_budget.py` (now real inside agent sessions too) |
| #114 | `test_hook_pipe_eof_on_parent_kill.py::test_collect_deadline_closes_stdout_pipe`, `test_stop_runner.py` |
| #21 | **New** `test_python_launcher_no_python3_issue21.py` (only `python`, only `py` -> `py -3`; 2 mutations) |
| #105 | `test_windows_spawn_no_window.py::test_utf8_io_reexec_nt_passes_std_handles`, `::test_utf8_io_reexec_pipe_round_trip` |
| #143 | `test_windowsapps_twin_cannot_vouch.py`, `test_python_launcher_probe.py` |
| #97 | `test_context_window_claude5_family.py` |
| #106 | `test_cleanup_command.py`, `test_uninstall_manifest_reconcile.py`, `test_setup_hook_uninstall.py`: WEAK (no on-disk leftover count; see below) |
| #61 | `test_msix_project_dir.py::test_msix_cwd_finds_its_own_project_not_the_newest_one` |
| #24 | Store-stub fix (c50cdcc0): `test_python_launcher_windows_pythonw.py::test_safe_prefix_*`, `test_windowsapps_twin_cannot_vouch.py` |
| #94 | `test_claude_config_dir.py` |
| #26 | `test_no_hook_auto_approve.py::test_claude_code_bash_hook_never_answers`, `test_hook_wire_contract` |
| #28 | `dashboard-proxy.py` no longer exists; nothing to regress |
| #57 | `test_env_fingerprint_matrix` (#57 mutations) |

## Test runs

- Targeted suites and all new tests: green.
- `check_regression_guards.py` on all 16 new mutations and on #117: all caught.
- Full suite: see the "Full suite result" section at the end.
- Two tests fail in this sandbox both before and after my changes. Both are environment-only:
  - `test_copilot_safe_python.py::test_gate_rejects_foreign_group_writable_dir` assumes a non-root user; the sandbox runs as uid 0. Its `os.stat` patch also breaks pytest's own error reporting.
  - `test_cowork_adapter.py::test_cowork_signal_true_under_synced_plugin_root`: the home guard sees the sandbox's real home `/root`.
- Neither is touched by this unit. CI runs as a normal user.

## Could not do, and why

- **Windows itself was not available.** Everything Windows-specific was simulated: shell models, `os.name`/`sys.platform` patches, a fake `ctypes`, and literal-backslash folder names for MSYS. Tests that need a real shell run on the Windows CI jobs: Copilot through real PowerShell, Codex through real cmd and PowerShell, and the liveness check through the real Windows API.
- **No live Codex Stop on Windows.** I could not confirm that a real automatic Stop on Windows creates a transcript-derived checkpoint. The end-to-end test fires the installed Stop command on Linux with Codex's payload shape. The doctor now records field presence and why capture was skipped.
- **Claude Code without Git Bash:** the plugin hooks are POSIX sh. Claude Code on native Windows requires Git Bash, so I did not add a PowerShell or exec form (see Decisions).
- **Copilot through WSL into a Windows home** (`install.sh --copilot` from a Windows shell): the hooks file gets WSL paths in `bash` and no `powershell` field. Whether native Copilot runs `bash` through WSL's bash.exe is unverified, so I left the documented flow alone.
- **CLAUDE_CONFIG_DIR stragglers** found by the sweep, not fixed:
  - `detectors/cache_instability.py:106` fallback;
  - `measure.py` `~/.claude/durable-memory.json`;
  - `vscode-extension/src/paths.ts` ignores `TOKEN_OPTIMIZER_COPILOT_HOME`.
  - These are not Windows-specific, and they touch the data layout.
- **#106** stays weakly guarded. A real test would install into a sandbox, run hooks and `cleanup`, then count leftover files. That needs a full install harness.
- Did not bump versions, touch CHECKSUMS, tag, or touch `main`.

## Decisions

1. **Branch.** Pushed to `claude/rel59-checkpoint-diagnostics-ku6t9e`, the only branch this session may push to, instead of `rel59/windows`.
2. **Bootstrap.** Used a `python -c` bootstrap only for launcher paths no quoted or 8.3 form can carry. It is readable source with no base64 (#183). Heal (the text-only repair any command runs) never writes it, because it needs this release's launcher, which only `codex-install` copies.
3. **Placeholder checkpoints.** On Codex, no placeholder is written even when a session id is known. This changes PR #280's choice of keeping a named marker, because the brief says Stop must never write "No transcript data available". Claude Code keeps its placeholder behavior.
4. **Claude plugin fallback.** Used `${D%[/\\]*}` (cut at the last `/` or `\`) instead of converting backslashes with `tr`. The first try added a `$(...)`, which made `_hook_command_identity` read every hook as the same one and could have broken dedup. The final form adds no subshell and is plain POSIX sh (checked in dash and bash).
5. **Dashboard shim.** `chcp 65001` without a `>NUL` redirect, because the #118 guard bans `>NUL` anywhere in `measure.py`. The shim's console is hidden, so the one line of output is harmless.
6. **Copilot.** Added the `powershell` field only when installing from native Windows. The documented WSL flow is unchanged.
7. **Test environment.** The conftest scrub drops `CLAUDE*`, `CODEX_*`, `COPILOT_*` and similar host-agent variables at import. Set `TO_TEST_KEEP_HOST_ENV=1` to keep them.
8. **Unrelated test bugs.** Left the two environment-only failures alone (root user, `/root` home); they are not Windows bugs.

UNIT DONE
