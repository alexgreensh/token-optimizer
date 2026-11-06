"""Regressions fixed on 2026-10-19 (5.13.50 / 5.13.51 cloud parity), planted
back by scripts/check_regression_guards.py. Each must turn its tests red.

Shape: (name, [test node ids], file_relpath, [(old, new), ...]).
"""

MEASURE = "skills/token-optimizer/scripts/measure.py"
RUNTIME_ENV = "skills/token-optimizer/scripts/runtime_env.py"
CLOUD_KIT = "skills/token-optimizer/scripts/cloud_kit.py"
READ_CACHE = "skills/token-optimizer/scripts/read_cache.py"

PARITY = "tests/test_cloud_parity.py"
SCHED_GATES = PARITY + "::test_scheduler_writers_run_cheap_gates_before_the_job_query"
REMOTE_HOST = PARITY + "::test_remote_host_resolution"
LIVE_CLOUD = PARITY + "::test_live_cloud_vm_env_is_cloud_not_cowork"

MUTATIONS = [
    ("cloud kit hook command renders the runner with str(Path), not _sh_path (4e0c4b37)",
     ["tests/test_cloud_kit.py::test_windows_host_paths_render_as_posix_for_sh"],
     CLOUD_KIT,
     [("    r = shlex.quote(_sh_path(runner))", "    r = shlex.quote(str(runner))")]),
    ("cloud kit run.sh renders root/pycache with str(Path), not _sh_path (4e0c4b37)",
     ["tests/test_cloud_kit.py::test_windows_host_paths_render_as_posix_for_sh"],
     CLOUD_KIT,
     [("root=shlex.quote(_sh_path(root)), pycache=shlex.quote(_sh_path(pycache))",
       "root=shlex.quote(str(root)), pycache=shlex.quote(str(pycache))")]),
    ("container id checked before cloud evidence: the cloud VM reads as Cowork (REVIEW-2 MED-3)",
     [REMOTE_HOST, LIVE_CLOUD],
     RUNTIME_ENV,
     [("        env_type = os.environ.get(_REMOTE_ENV_TYPE_ENV, \"\").strip().lower()\n",
       "        if os.environ.get(_COWORK_CONTAINER_ENV, \"\").strip():\n"
       "            return REMOTE_HOST_COWORK\n"
       "        env_type = os.environ.get(_REMOTE_ENV_TYPE_ENV, \"\").strip().lower()\n")]),
    ("container id without cloud evidence no longer means Cowork (REVIEW-2 MED-3)",
     [REMOTE_HOST],
     RUNTIME_ENV,
     [("        if os.environ.get(_COWORK_CONTAINER_ENV, \"\").strip():\n            return REMOTE_HOST_COWORK",
       "        if os.environ.get(_COWORK_CONTAINER_ENV, \"\").strip():\n            return REMOTE_HOST_UNKNOWN")]),
    ("synced plugin root checked before cloud evidence: the cloud VM reads as Cowork",
     [REMOTE_HOST, LIVE_CLOUD],
     RUNTIME_ENV,
     [("        env_type = os.environ.get(_REMOTE_ENV_TYPE_ENV, \"\").strip().lower()\n",
       "        for env_var in _CLAUDE_PLUGIN_ENVS:\n"
       "            val = os.environ.get(env_var, \"\")\n"
       "            if val and _COWORK_SYNCED_PLUGIN_MARKER in val.replace(\"\\\\\", \"/\"):\n"
       "                return REMOTE_HOST_COWORK\n"
       "        env_type = os.environ.get(_REMOTE_ENV_TYPE_ENV, \"\").strip().lower()\n")]),
    ("bare CLAUDE_CODE_REMOTE=true read as Cowork (the original cloud mislabel)",
     [REMOTE_HOST],
     RUNTIME_ENV,
     [("        if _truthy_env(_COWORK_REMOTE_ENV):\n            return REMOTE_HOST_UNKNOWN",
       "        if _truthy_env(_COWORK_REMOTE_ENV):\n            return REMOTE_HOST_COWORK")]),
    ("cloud kit sorts a release before its own prereleases",
     ["tests/test_cloud_kit.py::test_prerelease_versions_are_accepted_and_sorted"],
     CLOUD_KIT,
     [("    if not pre:\n        return nums + ((1,),)", "    if not pre:\n        return nums + ((-1,),)")]),
    ("cloud kit sorts prerelease numbers as text (rc.10 before rc.2)",
     ["tests/test_cloud_kit.py::test_prerelease_versions_are_accepted_and_sorted"],
     CLOUD_KIT,
     [("(0, int(x), \"\") if x.isdigit()", "(0, 0, x) if x.isdigit()")]),
    ("re-Read after compaction is soft-blocked as already in context (5.13.50)",
     [PARITY + "::test_reread_after_compaction_is_not_soft_blocked"],
     READ_CACHE,
     [("        if _scan_for_compaction(store, hook_input.get(\"transcript_path\")):\n",
       "        if False:\n")]),
    ("a tool result quoting compact_boundary clears the read cache (5.13.50)",
     [PARITY + "::test_reread_after_compaction_is_not_soft_blocked"],
     READ_CACHE,
     [("    if not ((row.get(\"type\") == \"system\" and row.get(\"subtype\") == \"compact_boundary\")\n"
       "            or row.get(\"isCompactSummary\") is True):\n        return None\n",
       "")]),
    ("deadline remaining() can exceed its own length by float rounding (Windows CI, 5.13.51)",
     ["tests/test_cloud_parity.py::test_deadline_remaining_never_exceeds_its_length"],
     "skills/token-optimizer/scripts/hook_runtime.py",
     [("        return min(self.seconds, max(0.0, self.end - time.monotonic()))",
       "        return max(0.0, self.end - time.monotonic())")]),
]
