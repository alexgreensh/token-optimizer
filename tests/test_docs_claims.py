"""The countable claims in our docs must match the code that backs them.

This rides the existing tests.yml matrix rather than adding a workflow, so the
gate runs on every push and PR on the same enforcement path the suite already
proved out. See scripts/check_docs_claims.py for why it exists.
"""

from __future__ import annotations

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
