"""Silent-failure and false-success regressions, planted back for CI proof.

Shape (shared by every scripts/mutations/<area>.py):
    MUTATIONS = [(name, [test node ids], file_relpath, [(old, new), ...])]

Each entry re-introduces a bug that shipped (or its exact shape) into a
throwaway copy; the listed tests must FAIL on it.
"""

MEASURE = "skills/token-optimizer/scripts/measure.py"
HOOK_RUNTIME = "skills/token-optimizer/scripts/hook_runtime.py"
MODULE_RUNNER = "hooks/module_runner.py"
UPS_RUNNER = "hooks/userpromptsubmit_runner.py"
FLEET = "skills/fleet-auditor/scripts/fleet.py"
FLEET_BASE = "skills/fleet-auditor/scripts/fleet_base.py"

SWALLOWED = "tests/test_swallowed_errors.py"
FALSE_SUCCESS = "tests/test_false_success.py"
REAL_LAYOUT = "tests/test_module_runner_real_layout.py"

DRIFT = "tests/test_release_drift.py"

UPS_HOOK = SWALLOWED + "::test_hook_raises_no_programming_error[UserPromptSubmit[*]:hooks/userpromptsubmit_runner.py]"
POST_HOOK = (SWALLOWED + "::test_hook_raises_no_programming_error[PostToolUse[Bash|PowerShell|Read|Glob|Grep|"
             "Agent|Edit|Write|MultiEdit|NotebookEdit|mcp__.*]:hooks/posttooluse_runner.py]")

MUTATIONS = [
    ("swallowed NameError in the UserPromptSubmit runner",
     [UPS_HOOK],
     UPS_RUNNER,
     [("def main() -> int:\n",
       "def main() -> int:\n    try:\n        _planted_helper_renamed_away()  # noqa: F821\n"
       "    except Exception:\n        pass\n")]),
    ("module_runner cannot reach hook_runtime from hooks/ runners",
     [REAL_LAYOUT + "::test_runner_deadline_is_armed_in_the_real_layout[posttooluse_runner-4.5-canonical]",
      POST_HOOK],
     MODULE_RUNNER,
     [("        sys.path.append(_plugin_scripts)\n", "        pass\n")]),
    ("#253 PreCompact capture cut off by its budget, hook still exits 0",
     [FALSE_SUCCESS + "::test_precompact_hook_writes_the_checkpoint_the_restore_injects"],
     HOOK_RUNTIME,
     [("BUDGET_PRECOMPACT_CAPTURE = 15.0\n", "BUDGET_PRECOMPACT_CAPTURE = 0.01\n")]),
    ("#149 detected fleet system reports a silent zero",
     [FALSE_SUCCESS + "::test_fleet_scan_never_reports_a_silent_zero_for_a_detected_system[opencode]",
      FALSE_SUCCESS + "::test_fleet_scan_never_reports_a_silent_zero_for_a_detected_system[nanoclaw]"],
     FLEET_BASE,
     [('        return [], [f"{self.display_name} fleet collection is not implemented yet, "\n'
       '                    f"so its runs are not counted here (0 is not a measurement)."]\n',
       "        return [], []\n")]),
    ("raise monitor stops listening (every swallowed-error test goes vacuous)",
     [SWALLOWED + "::test_monitor_catches_planted_swallowed_name_error"],
     "tests/fixtures/raise_monitor/sitecustomize.py",
     [("            _mon.set_events(_tool, _mon.events.RAISE)\n", "            _mon.set_events(_tool, 0)\n")]),
    ("unknown fleet --system prints a zero instead of an error",
     [FALSE_SUCCESS + "::test_fleet_unknown_system_is_an_error_not_a_zero"],
     FLEET,
     [("    if target_system not in known:\n", "    if False:\n")]),
    ("npm tree match ignores changed bytes",
     [DRIFT + "::test_tree_comparison_names_every_kind_of_drift"],
     "scripts/check_npm_published.py",
     [("        elif found[rel] != expected[rel]:\n", "        elif False:\n")]),
    ("release workflow stops running the npm tree match",
     [DRIFT + "::test_sign_release_workflow_runs_the_tree_match"],
     ".github/workflows/sign-release.yml",
     [('then match="--match-tree"; fi', 'then match=""; fi')]),
    ("signer drops a root the installer still requires a checksum for",
     [DRIFT + "::test_signer_and_installer_checksum_the_same_roots"],
     "scripts/sign-release.sh",
     [("    .codex/ \\\n", "")]),
]

MUTATIONS += [
    ("hook stdin read cut off at 0.5 s while a big payload is still streaming",
     ["tests/test_hook_robustness_regressions.py::test_stdin_payload_still_streaming_after_the_first_half_second_is_read_whole"],
     "skills/token-optimizer/scripts/hook_io.py",
     [("_STDIN_TOTAL_CAP = 5.0", "_STDIN_TOTAL_CAP = 0.5")]),
]
