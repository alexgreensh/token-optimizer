"""The PostToolUse pipeline path and cross-turn dedup must be headline-eligible
dashboard buckets, not silently excluded.

Regression guard: `bash_compress_hook.py` logs these features with
tier="measured", verified=True, intending them to count. The headline category
set derives from `_V5_COMPRESSION_LABELS` keys, so a missing label key silently
drops the whole path from the reported total (an UNDER-count). These tests fail
if either feature loses its label / headline eligibility, or if the hook stops
tagging the dedup case distinctly.
"""
import inspect
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


def _load(mod):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return __import__(mod)


def test_pipeline_and_dedup_are_headline_categories():
    measure = _load("measure")
    for feat, label in (
        ("bash_compress_pipeline", "Bash compress (pipelines)"),
        ("crossturn_dedup", "Cross-turn dedup"),
    ):
        assert feat in measure._V5_COMPRESSION_CATEGORIES, f"{feat} not headline-eligible"
        assert measure._V5_COMPRESSION_LABELS.get(feat) == label


def test_hook_tags_dedup_and_pipeline_distinctly():
    hook = _load("bash_compress_hook")
    # _log_event carries a feature arg defaulting to the pipeline bucket.
    sig = inspect.signature(hook._log_event)
    assert "feature" in sig.parameters
    assert sig.parameters["feature"].default == "bash_compress_pipeline"
    # main() delegates the payload handling to _run() so the CLAUDE_SESSION_ID
    # env marker can be scoped to the hook run and restored afterward; every
    # savings row it writes carries the per-branch tag.
    src = inspect.getsource(hook._run)
    assert "feature=_log_feature" in src
    assert '_log_feature = "bash_search_noise_fold"' in src
    # Cross-turn dedupe was removed (DECISION row 12): the hook must never mint
    # new crossturn_dedup rows. Historical rows keep their dashboard label
    # (test_pipeline_and_dedup_are_headline_categories above).
    assert '"crossturn_dedup"' not in src
