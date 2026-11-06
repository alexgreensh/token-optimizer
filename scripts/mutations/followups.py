"""Follow-up regressions for 5.13.51, planted back for CI proof.

Shape (shared by every scripts/mutations/<area>.py):
    MUTATIONS = [(name, [test node ids], file_relpath, [(old, new), ...])]

Each entry re-introduces a bug (or its exact shape) into a throwaway copy; the
listed tests must FAIL on it. The desktop Clean up fix (#207) is TypeScript:
its planted-bug proof runs in the desktop-mod-pure job's tests, not here.
"""

MEASURE = "skills/token-optimizer/scripts/measure.py"

RESTORE = "tests/test_compact_restore_choice.py"
DOCTOR = "tests/test_doctor_hook_source_label.py"

MUTATIONS = [
]
