"""Decision extraction must not persist credentials from tool output."""

from __future__ import annotations

import json
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from context_intel import _MAX_DECISIONS, _extract_decisions  # noqa: E402


class _Store:
    def __init__(self):
        self.values: dict[str, str] = {}

    def get_meta(self, key: str):
        return self.values.get(key)

    def set_meta(self, key: str, value: str):
        self.values[key] = value


def test_decision_extraction_redacts_credential_before_persisting():
    store = _Store()
    raw_key = "AKIA" + "A" * 16

    _extract_decisions(f"We decided to rotate {raw_key} tomorrow.", store)

    stored = store.values["session_decisions"]
    assert raw_key not in stored
    assert "[CREDENTIAL REDACTED: AWS access key]" in stored


def test_decision_extraction_repairs_legacy_credentials_at_the_decision_cap():
    store = _Store()
    raw_key = "AKIA" + "B" * 16
    store.values["session_decisions"] = json.dumps([raw_key] * _MAX_DECISIONS)

    _extract_decisions(
        "We decided to keep the current release plan while coordinating the full regression suite.", store
    )

    stored = store.values["session_decisions"]
    assert raw_key not in stored
    assert "[CREDENTIAL REDACTED: AWS access key]" in stored


def test_decision_extraction_repairs_legacy_credentials_without_a_decision():
    store = _Store()
    raw_key = "AKIA" + "C" * 16
    store.values["session_decisions"] = json.dumps([raw_key])

    _extract_decisions("no decision here", store)

    stored = store.values["session_decisions"]
    assert raw_key not in stored
    assert "[CREDENTIAL REDACTED: AWS access key]" in stored
