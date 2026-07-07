#!/usr/bin/env python3
"""Follow-up hardening for the URL auth-param redactor (extends PR #84).

PR #84 (nointerview1548) added the URL query-param credential class. Review
found three same-shape gaps + one cosmetic corruption; this pins the fixes:

  - OAuth implicit-flow FRAGMENT tokens (#access_token=...) — a classic leak
    vector the `[?&]`-only prefix missed.
  - additional credential-bearing param names: password/passwd/pwd, secret,
    client_secret, refresh_token, id_token, session(id), sig/signature,
    authorization, bearer, jwt.
  - pattern-order garble: when a query VALUE is itself another credential shape
    (?token=Bearer <...>) an earlier pattern redacts it first; the URL pattern
    must NOT re-wrap the resulting "[CREDENTIAL REDACTED: ...]" placeholder.

All fixtures are invented; no real secret appears here.

Run: python3 -m pytest tests/test_credential_patterns_url_hardening.py -v
"""
import os
import sys

import pytest

_SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills", "token-optimizer", "scripts",
)
sys.path.insert(0, _SCRIPTS)

from credential_patterns import redact_credentials  # noqa: E402

REDACTED = "[CREDENTIAL REDACTED: URL auth param]"


@pytest.mark.parametrize(
    "name",
    [
        "password", "passwd", "pwd", "secret", "client_secret", "client-secret",
        "refresh_token", "id_token", "session", "sessionid", "sig", "signature",
        "authorization", "bearer", "jwt",
    ],
)
def test_additional_param_names_redacted(name):
    out = redact_credentials(f"https://x.com/a?{name}=FAKE_SECRET_VALUE_123&next=2")
    assert "FAKE_SECRET_VALUE_123" not in out, f"{name} value leaked: {out}"
    assert REDACTED in out
    assert "&next=2" in out, f"following param swallowed for {name}: {out}"
    assert f"?{name}=" in out or f"?{name.replace('-', '-')}=" in out


def test_fragment_oauth_token_redacted():
    out = redact_credentials(
        "https://x.com/cb#access_token=FAKE_FRAG_SECRET&token_type=bearer"
    )
    assert "FAKE_FRAG_SECRET" not in out
    assert "#access_token=" + REDACTED in out
    # token_type=bearer is a non-secret flag value; it must survive.
    assert "token_type=bearer" in out


def test_value_stops_at_fragment_anchor():
    out = redact_credentials("https://x.com/a?token=FAKE_SECRET#section-anchor")
    assert "FAKE_SECRET" not in out
    assert "#section-anchor" in out  # anchor preserved, not swallowed


def test_no_double_redaction_when_value_is_itself_a_credential():
    # An earlier pattern (Bearer) redacts the value first; the URL pattern must
    # not re-wrap the placeholder into malformed "] REDACTED: ..." garble.
    out = redact_credentials(
        "https://x.com/a?token=Bearer FAKETOKEN123456789012345678901234"
    )
    assert "URL auth param] REDACTED" not in out, f"garbled double-redaction: {out}"
    assert out.count("[CREDENTIAL REDACTED") == 1
    assert "FAKETOKEN123456789012345678901234" not in out


@pytest.mark.parametrize("benign", ["page", "monkey", "tokenizer", "keyboard", "sessions_count"])
def test_substring_lookalikes_not_redacted(benign):
    out = redact_credentials(f"https://x.com/s?{benign}=SOMEVALUE")
    assert out == f"https://x.com/s?{benign}=SOMEVALUE", f"false positive on {benign}: {out}"


def test_secret_containing_brackets_is_fully_redacted():
    # A real secret may contain a literal `[` (common in passwords). The value must
    # be redacted WHOLE — never truncated at the bracket, leaking the remainder.
    out = redact_credentials("https://x.com/l?password=Sup3r[Secret]!&u=bob")
    assert "Secret]!" not in out, f"secret leaked past bracket: {out}"
    assert "Sup3r" not in out
    assert out == "https://x.com/l?password=" + REDACTED + "&u=bob"


def test_matrix_semicolon_param_redacted():
    out = redact_credentials("https://x.com/a?foo=1;password=SEKRETVAL")
    assert "SEKRETVAL" not in out
    assert "foo=1" in out  # the non-secret matrix param survives


@pytest.mark.parametrize("name", ["session_token", "session-token"])
def test_session_token_param_redacted(name):
    out = redact_credentials(f"https://x.com/a?{name}=FAKE_SESSION_TOK&x=1")
    assert "FAKE_SESSION_TOK" not in out
    assert "&x=1" in out
