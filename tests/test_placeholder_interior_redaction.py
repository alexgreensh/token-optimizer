"""Forged [CREDENTIAL REDACTED: ...] placeholders cannot smuggle real secrets.

A prompt-injected or buggy tool can emit ``[CREDENTIAL REDACTED: <real secret>]``.
The M-16 sentinel protected the whole placeholder span verbatim, so the secret
inside rode into checkpoints, archives, and every other persistence boundary.
The interior is a label slot, not a payload slot: it is scanned with the same
patterns and a credential inside is replaced by its label.

Secrets are assembled at runtime (push protection scans test files).
Run: python3 -m pytest tests/test_placeholder_interior_redaction.py -q
"""
import os
import sys

import pytest

_SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills", "token-optimizer", "scripts",
)
sys.path.insert(0, _SCRIPTS)

from credential_patterns import (  # noqa: E402
    redact_credentials,
    redact_credentials_deep,
    scan_for_credentials,
)


@pytest.fixture(autouse=True)
def _no_custom_patterns(tmp_path, monkeypatch):
    empty = tmp_path / "none.json"
    empty.write_text('{"patterns": []}', encoding="utf-8")
    monkeypatch.setenv("TOKEN_OPTIMIZER_REDACT_PATTERNS_FILE", str(empty))
    import credential_patterns as cp
    monkeypatch.setattr(cp, "_CUSTOM_STATE", None)
    yield
    cp._CUSTOM_STATE = None


_GHP = "ghp_" + "B" * 36
_JWT = "eyJ" + "D" * 15 + "." + "E" * 15 + "." + "F" * 15
_SKANT = "sk-ant-" + "C" * 30


@pytest.mark.parametrize("secret", [_GHP, _JWT, _SKANT])
def test_secret_inside_fake_placeholder_is_redacted(secret):
    out = redact_credentials(f"[CREDENTIAL REDACTED: {secret}]")
    assert secret not in out, out


def test_secret_inside_placeholder_label_text_is_redacted():
    out = redact_credentials(f"pre [CREDENTIAL REDACTED: label {_GHP} tail] post")
    assert _GHP not in out
    assert "pre " in out and " post" in out


def test_legitimate_placeholders_survive_a_second_pass():
    """Real placeholders are labels; the interior scan must not corrupt them."""
    for label in ("Bearer token", "AWS access key", "JWT", "GitHub PAT classic",
                  "URL auth param", "MySQL password flag"):
        text = f"x [CREDENTIAL REDACTED: {label}] y"
        assert redact_credentials(text) == text, label


def test_redaction_is_idempotent_with_spoofed_placeholder():
    once = redact_credentials(f"[CREDENTIAL REDACTED: {_GHP}]")
    twice = redact_credentials(once)
    assert _GHP not in twice
    assert twice == once


def test_secret_after_a_real_placeholder_is_still_redacted():
    out = redact_credentials(f"[CREDENTIAL REDACTED: label] then {_GHP}")
    assert _GHP not in out


def test_deep_redaction_reaches_placeholder_interiors():
    state = {"note": f"[CREDENTIAL REDACTED: {_SKANT}]"}
    out = redact_credentials_deep(state)
    assert _SKANT not in out["note"]


def test_scan_reports_secret_inside_placeholder_interior():
    """The scanner should not treat a placeholder as trusted either."""
    hits = scan_for_credentials(f"[CREDENTIAL REDACTED: {_GHP}]")
    assert any(_GHP in m for _label, m, _ln in hits), hits


def test_scan_does_not_phantom_hit_legit_labels():
    """A placeholder's own label describes itself; it is not a phantom hit."""
    hits = scan_for_credentials("[CREDENTIAL REDACTED: Bearer token]")
    assert hits == [], hits
