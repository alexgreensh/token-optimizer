"""The countable and threshold claims in our docs must match the code.

This rides the existing tests.yml matrix rather than adding a workflow, so the
gate runs on every push and PR on the same enforcement path the suite already
proved out. See scripts/check_docs_claims.py for why it exists.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHECKER = REPO / "scripts" / "check_docs_claims.py"


def test_docs_claims_match_code():
    result = subprocess.run(
        [sys.executable, str(CHECKER)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )
    # The checker prints one line per finding; surface them in the failure so a
    # red CI run says which number is wrong, not merely that one is.
    assert result.returncode == 0, (
        "Docs state numbers the code does not back:\n\n"
        f"{result.stdout}{result.stderr}"
    )


def test_ground_truth_covers_the_split_modules_and_still_refuses_a_miss():
    """The facade delegates to _measure/* after the split: a pattern that moved
    there must be found, and a pattern that exists nowhere must still fail."""
    import importlib.util
    import re

    spec = importlib.util.spec_from_file_location("check_docs_claims_under_test", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    pattern = r'best\["confidence"\] < ([0-9]+(?:\.[0-9]+)?)'
    facade = (REPO / module._MEASURE).read_text(encoding="utf-8")
    assert not re.search(pattern, facade), "the firing floor moved out of the facade"
    assert module._source_value(module._MEASURE, pattern, "loop-warning firing floor") > 0

    import pytest
    with pytest.raises(RuntimeError, match="ground truth for planted-miss"):
        module._source_value(
            module._MEASURE, r"planted_miss_9z9z\( (\d+) \)", "planted-miss")


def test_docs_claim_exclusions_match_windows_separators(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_docs_claims_under_test", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class _WindowsOS:
        name = "nt"

        def __getattr__(self, name):
            return getattr(os, name)

    monkeypatch.setattr(module, "os", _WindowsOS())
    assert module._path_matches_prefix(r"openclaw\README.md", ("openclaw/",))
    assert module._path_matches_prefix(
        r"docs-site\src\content\docs\features\fleet-auditor.mdx",
        ("docs-site/src/content/docs/features/fleet-auditor",),
    )


def test_docs_claim_exclusions_preserve_posix_backslash_filenames(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_docs_claims_under_test", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class _PosixOS:
        name = "posix"

        def __getattr__(self, name):
            return getattr(os, name)

    monkeypatch.setattr(module, "os", _PosixOS())
    assert not module._path_matches_prefix(r"openclaw\README.md", ("openclaw/",))
