"""A missed SessionStart must not blank ContextQ for the rest of the session.

The quality cache is created by SessionStart (`quality-cache --force --quiet
--once-mark`, timeout 20s). If that hook times out -- a busy machine, a boot
storm, a heavy benchmark, i.e. exactly when someone looks at the statusline --
nothing else created it, and the statusline showed `ContextQ:--` for the whole
session with no way back.

The recovery already existed in `userpromptsubmit_runner.py`, but it sat behind
`_harness_only_context()`, which is true only for containers, remote, or a
CLAUDE_PLUGIN_ROOT containing "harness"/"/plugins/synced/". A normal local user
never reached it. So the in-code claim that "UserPromptSubmit owns the initial
computation" was true only in harness contexts, and false for everyone else.

The fix runs that ONE recovery outside the harness gate, and only when the cache
file is genuinely absent.

What must NOT change: PostToolUse still refuses to bootstrap. It fires on every
tool call, and a transcript parse there is a latency regression. That invariant
is owned by
tests/test_hook_runtime_parity.py::test_throttle_only_cache_miss_never_parses_transcript;
these tests exist to make sure the ContextQ fix never buys itself by breaking it.

Run: python3 -m pytest tests/test_quality_cache_bootstrap.py -v
"""
from helpers.measure_split import measure_source as _split_measure_source
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "token-optimizer" / "scripts"
# Canonical copy. The plugins/ and cowork/ copies are MIRRORS that the suite
# regenerates, so a fix applied only to a mirror is silently reverted mid-run.
RUNNER = ROOT / "hooks" / "userpromptsubmit_runner.py"


@pytest.fixture()
def m(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="to-qc-bootstrap-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    sys.path.insert(0, str(SCRIPTS))
    # Load a fresh measure bound to the temp snapshot dir, then put the
    # shared module back so later tests never see the temp paths.
    original = sys.modules.pop("measure", None)
    import measure
    yield measure
    sys.modules.pop("measure", None)
    if original is not None:
        sys.modules["measure"] = original


def test_hot_path_still_refuses_to_bootstrap(m):
    """The ContextQ fix must not be paid for out of PostToolUse latency."""
    src = _split_measure_source(SCRIPTS / "measure.py")
    idx = src.index("if pure_time_throttle and not force and not cache_path.exists():")
    block = src[idx:idx + 400]
    assert "return None" in block.split("\n")[1], (
        "the throttle-only path must still return None on a cache miss; "
        "PostToolUse fires on every tool call"
    )


def test_recovery_exists_outside_the_harness_gate(m):
    """The bug: the recovery was gated to environments a normal user never has."""
    src = RUNNER.read_text(encoding="utf-8")
    # The dispatcher block is the LAST occurrence; an earlier one guards a
    # short-circuit path near the top of main().
    idx = src.rindex("if _harness_only_context():")
    block = src[idx:idx + 1600]
    assert "elif _quality_cache_is_missing(" in block, (
        "a non-harness session with NO cache must still get one bootstrap; "
        "otherwise a timed-out SessionStart blanks ContextQ permanently"
    )


def test_recovery_is_conditional_on_the_file_being_absent(m):
    """It must not fire on every prompt, only when there is nothing to read."""
    src = RUNNER.read_text(encoding="utf-8")
    assert "def _quality_cache_is_missing(" in src
    fn = src[src.index("def _quality_cache_is_missing("):]
    fn = fn[:fn.index("\n\ndef ")]
    assert ".exists()" in fn, "the check must be a plain existence test"
    assert "return False" in fn, (
        "any error resolving the path must fall back to NOT bootstrapping, so a "
        "broken resolution cannot turn every prompt into a transcript parse"
    )


def test_harness_gate_semantics_are_unchanged(m):
    """The three harness-only subcommands must still be harness-only."""
    src = RUNNER.read_text(encoding="utf-8")
    idx = src.rindex("if _harness_only_context():")
    block = src[idx:idx + 600]
    for sub in ("_sub_ensure_health", "_sub_compact_restore"):
        assert sub in block, f"{sub} must stay inside the harness gate"


def test_no_orphan_bootstrap_marker_files_are_created(m):
    """The previous design wrote per-session marker files; that approach is gone.

    It bought recovery at the cost of the hot-path invariant, and left a new
    class of file behind. Nothing should create `quality-bootstrap-*` any more.
    """
    src = _split_measure_source(SCRIPTS / "measure.py")
    assert "quality-bootstrap-" not in src, (
        "the marker-file approach was abandoned; it must not leave residue"
    )
