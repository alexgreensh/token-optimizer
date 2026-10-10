"""Redaction must stay linear on adversarial input (review r2, F6).

200 KB of text built to make an unanchored regex rescan the same run from every
start used to take 4 to 5 seconds (JWT pattern, unclosed placeholder). Each case
must finish in under a second, and real-shaped tokens must still match.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "skills", "token-optimizer", "scripts"))

import credential_patterns as cp  # noqa: E402

SIZE = 200 * 1024


@pytest.fixture(autouse=True)
def _no_custom_patterns(tmp_path, monkeypatch):
    empty = tmp_path / "none.json"
    empty.write_text('{"patterns": []}', encoding="utf-8")
    monkeypatch.setenv("TOKEN_OPTIMIZER_REDACT_PATTERNS_FILE", str(empty))
    monkeypatch.setattr(cp, "_CUSTOM_STATE", None)
    yield
    cp._CUSTOM_STATE = None


def _fill(unit: str) -> str:
    return (unit * (SIZE // len(unit) + 1))[:SIZE]


ADVERSARIAL = {
    "jwt-header-run": _fill("eyJ"),
    "jwt-header-dash-run": _fill("eyJ-"),
    "jwt-short-gaps": _fill("eyJ" + "A" * 20),
    "jwt-dotted-no-third": _fill("eyJ" + "A" * 10 + "." + "B" * 12 + ".C "),
    "jwt-header-then-dots": "eyJ" + "A" * 9 + "." + _fill("eyJ"),
    # Quadratic growth is gentler here (about 1 s at 200 KB on a fast machine, 5 s
    # on a slow one), so these two use twice the size to keep the red/green clear.
    "unclosed-placeholder": _fill("[CREDENTIAL REDACTED: ") + _fill("[CREDENTIAL REDACTED: "),
    "unclosed-placeholder-in-text": "x " + _fill("[CREDENTIAL REDACTED: a ") * 2,
    "bearer-run": _fill("Bearer "),
    "keyword-run": _fill("key"),
    "assignment-run": _fill("api_key="),
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_adversarial_input_is_fast(name):
    text = ADVERSARIAL[name]
    t0 = time.perf_counter()
    cp.redact_credentials(text)
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0, f"{name}: {elapsed:.2f}s for {len(text)} bytes"


def test_jwt_still_redacted_in_real_positions():
    seg = "AbCdEfGhIjKlMn"
    jwt = "eyJ" + seg + "." + seg + "." + seg
    for text in (f"auth {jwt} end", f"MEDX-123456-{jwt}", f"x{jwt}", f"Authorization: Bearer {jwt}", f'"t":"{jwt}"', f"jwt={jwt}", f"x\n{jwt}"):
        out = cp.redact_credentials(text)
        assert seg not in out, out


def test_closed_placeholder_is_still_protected():
    text = "a [CREDENTIAL REDACTED: Stripe live key] b"
    assert cp.redact_credentials(text) == text


def test_oversized_placeholder_interior_cannot_hide_a_secret():
    secret = "ghp_" + "Q7r8T9u0V1w2X3y4Z5a6B7c8D9e0F1g2H3i4"
    text = "[CREDENTIAL REDACTED: " + "x" * 300 + secret + "]"
    assert "Q7r8T9u0V1w2" not in cp.redact_credentials(text)
