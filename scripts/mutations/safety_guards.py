"""Safety guards whose tests used to pass with the guard deleted (test review, 2026-10-21).

Shape: MUTATIONS = [(name, [test node ids], file_relpath, [(old, new), ...])]
"""

from __future__ import annotations

MEASURE = "skills/token-optimizer/scripts/measure.py"
IDENTITY = "tests/test_posix_session_identity.py"

MUTATIONS = [
]

REFETCH = "skills/token-optimizer/scripts/refetch_guard.py"
DATA_ID = "tests/test_plugin_data_identity.py"

MUTATIONS += [
    ("refetch guard denies a re-fetch whose archive entry cannot be expanded",
     [DATA_ID + "::test_refetch_guard_renderable_check",
      DATA_ID + "::test_refetch_guard_empty_response_is_not_renderable",
      DATA_ID + "::test_refetch_guard_corrupt_json_not_renderable"],
     REFETCH,
     [("if tool_use_id and _entry_is_renderable(archive_dir, tool_use_id):",
       "if tool_use_id:")]),
    ("refetch guard counts an empty archived response as expandable",
     [DATA_ID + "::test_refetch_guard_empty_response_is_not_renderable"],
     REFETCH,
     [("return isinstance(payload.get(\"response\"), str) and bool(payload[\"response\"])",
       "return isinstance(payload.get(\"response\"), str)")]),
]

EXT = "tests/test_post_flush_extensions.py"

MUTATIONS += [
]

MUTATIONS += [
    ("module_runner runs a dotted module name (arbitrary module execution)",
     ["tests/test_module_runner_guard.py::test_dotted_module_name_refused"],
     "hooks/module_runner.py",
     [("    if not module_name.isidentifier():\n        return 0\n", "")]),
]

MUTATIONS += [
    ("Copilot bridge auto-approves every rewritten command",
     ["tests/test_no_hook_auto_approve.py::test_copilot_bridge_does_not_approve_destructive_commands"],
     "skills/token-optimizer/scripts/copilot_hook_bridge.py",
     [("    if caps.get(CAP_ALLOW) and _bash_hook._is_auto_approvable(command):",
       "    if caps.get(CAP_ALLOW):")]),
]

RUNNER = "hooks/module_runner.py"
COLD = "tests/test_cold_compile_gate.py"
RECONCILE = "skills/token-optimizer/scripts/install_reconcile.py"

MUTATIONS += [
    ("PreCompact save gives up on a cold compile as fast as other measure.py hooks",
     [COLD + "::test_precompact_save_waits_out_a_slow_cold_compile",
      COLD + "::test_only_the_precompact_save_gets_the_longer_wait"],
     RUNNER,
     [("_COLD_COMPILE_WAIT_PRECOMPACT_SAVE_S = 12.0", "_COLD_COMPILE_WAIT_PRECOMPACT_SAVE_S = 4.0")]),
    ("uninstall cleanup treats a live install as stale",
     ["tests/test_uninstall_manifest_reconcile.py::test_remove_keeps_active_our_entries"],
     RECONCILE,
     [("        all_gone = all(\n", "        all_gone = True or all(\n")]),
]

MUTATIONS += [
    ('search recovery 334 hook emits unarchived lossy previews',
     ['tests/test_posttooluse_bash_compress_hook.py::TestSearchRecovery334'],
     'skills/token-optimizer/scripts/bash_compress_hook.py',
     [('        if _archive_key is None and _detect_pattern(command) == \"search_results\":\n            compressed = stdout\n', '')]),
    ('search recovery 334 CLI emits unarchived lossy previews',
     ['tests/test_posttooluse_bash_compress_hook.py::TestSearchRecovery334::test_cli_search_without_archive_keeps_original'],
     'skills/token-optimizer/scripts/bash_compress.py',
     [('        if _archive_key is None and _detect_pattern(command_str) == \"search_results\":\n            compressed = raw_output\n', '')]),
    ('search recovery 334 banner hides per-file cap',
     ['tests/test_posttooluse_bash_compress_hook.py::TestSearchRecovery334::test_search_banner_names_both_caps'],
     'skills/token-optimizer/scripts/bash_search_results.py',
     [('up to {_MAX_PER_FILE} matches per file', 'per-line content')]),
]
