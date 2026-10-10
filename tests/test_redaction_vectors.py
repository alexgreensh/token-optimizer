"""Shared redaction vectors, Python half.

The same table (tests/fixtures/redaction_vectors.json) runs against the three
TypeScript redactors (pi, opencode, openclaw), so the four stay equivalent: a
secret shape one stack masks and another leaves behind is the exact drift that
let the PEM key body leak through the Python redactor.

Run: python3 -m pytest tests/test_redaction_vectors.py -q
"""
import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "skills", "token-optimizer", "scripts"))

from credential_patterns import redact_credentials  # noqa: E402

with open(os.path.join(_REPO, "tests", "fixtures", "redaction_vectors.json"), encoding="utf-8") as _fh:
    _VECTORS = json.load(_fh)["vectors"]


@pytest.fixture(autouse=True)
def _no_custom_patterns(tmp_path, monkeypatch):
    # Built-in set only: a developer's own redact-patterns.json must not leak in.
    empty = tmp_path / "none.json"
    empty.write_text('{"patterns": []}', encoding="utf-8")
    monkeypatch.setenv("TOKEN_OPTIMIZER_REDACT_PATTERNS_FILE", str(empty))
    import credential_patterns as cp
    monkeypatch.setattr(cp, "_CUSTOM_STATE", None)
    yield
    cp._CUSTOM_STATE = None


@pytest.mark.parametrize("vec", _VECTORS, ids=[v["name"] for v in _VECTORS])
def test_python_redactor_matches_shared_vector(vec):
    out = redact_credentials("".join(vec["input"]))
    for needle in vec["absent"]:
        assert needle not in out, f"{vec['name']}: {needle!r} survived: {out!r}"
    for needle in vec["present"]:
        assert needle in out, f"{vec['name']}: {needle!r} was wrongly removed: {out!r}"
