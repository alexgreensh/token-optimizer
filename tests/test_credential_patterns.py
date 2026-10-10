"""Credential redaction tests for credential_patterns.py.

Focus: the URL auth-param class (credentials passed as URL query parameters such
as ?token=... / ?api_key=...), which archive_result.py archives verbatim unless
redacted. Also guards that adding this pattern does not regress the existing
full-match patterns or over-match benign query parameters.

All fixtures are invented; no real secret appears here.

Run: python3 -m pytest tests/test_credential_patterns.py -v
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
    CREDENTIAL_PATTERNS,
    redact_credentials,
    scan_for_credentials,
)

# Fake value substrings we assert never survive redaction.
_FAKE_TOKEN = "FAKE_URL_TOKEN_123"
_FAKE_API_KEY = "FAKE_API_KEY_456"
_FAKE_ACCESS = "FAKE_ACCESS_789"
_FAKE_AUTH = "FAKE_AUTH_000"


@pytest.mark.parametrize(
    "raw, secret",
    [
        (f"GET https://example.com/alerts?token={_FAKE_TOKEN} HTTP/1.1", _FAKE_TOKEN),
        (f"curl https://api.example.com/v1/data?api_key={_FAKE_API_KEY}", _FAKE_API_KEY),
        (f"https://x.example.com/p?access_token={_FAKE_ACCESS}&next=1", _FAKE_ACCESS),
        (f"https://x.example.com/p?auth={_FAKE_AUTH}", _FAKE_AUTH),
        (f"https://x.example.com/p?api-key={_FAKE_API_KEY}", _FAKE_API_KEY),
        (f"wss://x.example.com/stream?key={_FAKE_TOKEN}", _FAKE_TOKEN),
    ],
)
def test_url_auth_param_value_is_redacted(raw, secret):
    out = redact_credentials(raw)
    assert secret not in out, f"value leaked verbatim: {out!r}"
    assert "[CREDENTIAL REDACTED: URL auth param]" in out


def test_param_name_is_preserved():
    """Only the value is blanked; the parameter name survives for context."""
    out = redact_credentials(f"https://example.com/a?token={_FAKE_TOKEN}")
    assert out == "https://example.com/a?token=[CREDENTIAL REDACTED: URL auth param]"


def test_following_params_survive():
    """The value match stops at '&' so subsequent params are not swallowed."""
    out = redact_credentials(
        f"https://example.com/a?access_token={_FAKE_ACCESS}&page=2&sort=asc"
    )
    assert _FAKE_ACCESS not in out
    assert "&page=2&sort=asc" in out


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.com/list?page=2&sort=asc",
        "https://example.com/items?monkey=1&donkey=2",  # 'key' only as a substring
        "https://example.com/search?q=token+auth&lang=en",  # words, not param names
        "https://example.com/x?tokenizer=fast",  # 'token' is a prefix of the name
    ],
)
def test_benign_params_not_over_matched(raw):
    assert redact_credentials(raw) == raw
    assert "REDACTED" not in redact_credentials(raw)


def test_existing_patterns_still_redact():
    """Regression: representative pre-existing patterns still fully redact."""
    samples = {
        "AWS access key": "AKIA" + "A" * 16,
        "GitHub OAuth token": "gho_" + "b" * 36,
        "Bearer token": "Authorization: Bearer abc.def_ghi-123",
        "Database URI": "postgres://user:s3cret@db.example.com/app",
    }
    for label, raw in samples.items():
        out = redact_credentials(raw)
        assert f"[CREDENTIAL REDACTED: {label}]" in out, (label, out)


def test_pattern_registered_and_labelled():
    labels = [label for label, _ in CREDENTIAL_PATTERNS]
    assert "URL auth param" in labels
    # scan surfaces the label too (used for coverage reporting).
    hits = scan_for_credentials(f"https://example.com/a?token={_FAKE_TOKEN}")
    assert any(label == "URL auth param" for label, _match, _ln in hits)


# GitLab token families (the `gl*-` prefixes GitLab documents). Joined at runtime so
# no literal in this file looks like a real secret to push protection.
_GITLAB_BODY = "AbCdEfGhIjKlMnOpQrSt"


@pytest.mark.parametrize("prefix", ["glpat", "gldt", "glrt", "glcbt", "glptt", "glft", "glimt", "glagent", "glsoat"])
def test_gitlab_tokens_are_redacted(prefix):
    secret = prefix + "-" + _GITLAB_BODY
    out = redact_credentials(f"echo {secret} | docker login --password-stdin registry.example.com")
    assert _GITLAB_BODY not in out, out
    assert "[CREDENTIAL REDACTED: GitLab token]" in out


def test_gitlab_routable_token_with_dots_is_redacted_whole():
    secret = "glpat-" + _GITLAB_BODY + ".01.abcdef123"
    out = redact_credentials(f"PRIVATE-TOKEN: {secret} end")
    assert "abcdef123" not in out and _GITLAB_BODY not in out


@pytest.mark.parametrize("benign", ["glpat-", "glpat-short", "the glpat- prefix", "gl-pat-" + _GITLAB_BODY, "glob-" + _GITLAB_BODY])
def test_gitlab_pattern_does_not_over_match(benign):
    assert redact_credentials(benign) == benign


def test_gitlab_prefix_trips_the_global_prefilter():
    from credential_patterns import _text_may_contain_credentials
    assert _text_may_contain_credentials("x glpat-" + _GITLAB_BODY)
