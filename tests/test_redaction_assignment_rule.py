"""Generic NAME=value / NAME: value secret rule in credential_patterns (review r2, F3).

Every secret below is invented. Values are joined at runtime so no secret-shaped
literal sits in the file.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "skills", "token-optimizer", "scripts"))

import credential_patterns as cp  # noqa: E402


@pytest.fixture(autouse=True)
def _no_custom_patterns(tmp_path, monkeypatch):
    empty = tmp_path / "none.json"
    empty.write_text('{"patterns": []}', encoding="utf-8")
    monkeypatch.setenv("TOKEN_OPTIMIZER_REDACT_PATTERNS_FILE", str(empty))
    monkeypatch.setattr(cp, "_CUSTOM_STATE", None)
    yield
    cp._CUSTOM_STATE = None


REDACTED = [
    ("PASSWORD=" + "hunter2FAKE" + " ./deploy.sh", "hunter2FAKE", "PASSWORD="),
    ("db_passwd: " + "FAKEpw99Zz", "FAKEpw99Zz", "db_passwd: "),
    ("API_KEY = \"" + "FAKEspacedQuoted7" + "\"", "FAKEspacedQuoted7", "API_KEY = \""),
    ("client-secret=" + "FAKEclientSecret1" + ",", "FAKEclientSecret1", "client-secret="),
    ("export STRIPE_SECRET_KEY=" + "FAKEstripeSecretVal9", "FAKEstripeSecretVal9", "STRIPE_SECRET_KEY="),
    ("{'auth_token': '" + "FAKEdictTokenVal3" + "'}", "FAKEdictTokenVal3", "'auth_token': '"),
    ("apikey=" + "FAKErunTogether88", "FAKErunTogether88", "apikey="),
    ("MY_CREDENTIAL=" + "FAKEcredVal66", "FAKEcredVal66", "MY_CREDENTIAL="),
    ("PASSWORD=" + "12345678", "12345678", "PASSWORD="),
]

KEPT = [
    "the token count is 500",
    "token_count = len(x)",
    "max_tokens=4096",
    "tokens: 1200",
    "key: 5",
    "key: true",
    "headers = {'x': token}",
    "if token == expected:",
    "const tokenizer = new Tokenizer()",
    "KEY_FILE=/etc/app/key.pem",
    "SECRET_NAME=prod-db",
    "password: $DB_PASS",
    "password: ${DB_PASS}",
    "apiKey: string;",
    "api_key: Optional[str] = None",
    "password = os.environ['X']",
    "key = get_key()",
]


@pytest.mark.parametrize("text,secret,keeps", REDACTED)
def test_secret_value_goes_name_stays(text, secret, keeps):
    out = cp.redact_credentials(text)
    assert secret not in out, out
    assert keeps in out, out


@pytest.mark.parametrize("text", KEPT)
def test_ordinary_text_and_code_untouched(text):
    assert cp.redact_credentials(text) == text


def test_idempotent():
    text = "PASSWORD=" + "hunter2FAKE" + "\nx: \"token\": \"" + "FAKEv" + "\""
    once = cp.redact_credentials(text)
    assert cp.redact_credentials(once) == once


def test_existing_placeholders_survive_in_order():
    text = ("a [CREDENTIAL REDACTED: JWT] b token=" + "FAKEsecretValue9" +
            " c [CREDENTIAL REDACTED: Bearer token] d")
    out = cp.redact_credentials(text)
    assert "FAKEsecretValue9" not in out
    assert out.index("JWT") < out.index("token=") < out.index("Bearer token")


def test_multiline_value_never_crosses_a_line():
    out = cp.redact_credentials("password: " + "FAKEpwLine1" + "\nhost: example\nname: x")
    assert "FAKEpwLine1" not in out
    assert "host: example\nname: x" in out


def test_keyword_dense_input_is_linear():
    for blob in ("key" * 70000, "api_key_" * 30000, "token=" * 40000, "password:" * 25000):
        t0 = time.perf_counter()
        cp.redact_credentials(blob)
        assert time.perf_counter() - t0 < 2.0, blob[:12]


# Rows where this engine and the TS engines behave differently on purpose (r2a report).
# Python keeps these because earlier Python tests pin them; TS hides the value.
PYTHON_ONLY_KEPT = [
    "https://x.com/s?monkey=SOMEVALUE",   # "key" inside another word is not a name
    "KEY_FILE=/etc/app/key.pem",           # names ending in a locator hold no secret
    "SECRET_NAME=prod-db",
    "token_type=bearer",
    'max_tokens="4096"',                   # a quoted small number is still a number (TS backtracks and hides it)
    'ok="true" password="false"',
]


@pytest.mark.parametrize("text", PYTHON_ONLY_KEPT)
def test_python_only_exemptions(text):
    assert cp.redact_credentials(text) == text


# Same in both engines: no kwarg pass-through or path exemption any more.
@pytest.mark.parametrize("text,secret", [
    ("f(api_key=" + "api_key" + ")", "api_key)"),
    ("PWD=" + "/Users/someone/project", "/Users/someone"),
    ("token: " + "<your-token>", "<your-token>"),
])
def test_values_that_only_look_harmless_are_hidden(text, secret):
    assert secret not in cp.redact_credentials(text)
