"""Provider-prefixed model ids resolve to the same window/settings as bare ids.

``anthropic/claude-haiku-4-5``, ``bedrock/claude-sonnet-4-5`` and
Bedrock's dotted ``us.anthropic.claude-haiku-4-5`` all missed the anchored
family regex in ``_claude_model_window`` and fell through to the 1M
"unrecognized" default, understating fill ~5x for 200K models. The same prefix
blindness kept per-model ``modelSettings`` keys (canonicalized id match) from
applying to provider-prefixed session models in both measure.py and
statusline.js, and dropped Hermes' 1M models to the 200K fallback.

Prefixes cover gateways (``anthropic/``, ``bedrock/``, ``openrouter/``),
Bedrock inference profiles (``us.anthropic.``, ``eu.anthropic.``,
``global.anthropic.``, plain ``anthropic.``) and known colon prefixes
(``bedrock:``).

Run: python3 -m pytest tests/test_provider_prefixed_model_ids.py -q
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture()
def measure(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    spec.loader.exec_module(mod)
    for key in ("CLAUDE_CODE_DISABLE_1M_CONTEXT", "TOKEN_OPTIMIZER_CONTEXT_SIZE"):
        monkeypatch.delenv(key, raising=False)
    yield mod
    sys.modules.pop("measure", None)


import hermes_session  # noqa: E402


# ---------------------------------------------------------------------------
# measure._claude_model_window: prefixed ids resolve like their bare form
# ---------------------------------------------------------------------------

_200K = 200_000
_1M = 1_000_000


@pytest.mark.parametrize("model,expected", [
    # the torture-report repro ids
    ("anthropic/claude-haiku-4-5", _200K),
    ("bedrock/claude-sonnet-4-5", _200K),
    ("anthropic/claude-sonnet-4-6", _200K),
    ("us.anthropic.claude-haiku-4-5", _200K),
    # every prefix flavor on both sides of the 200K/1M line
    ("vertex/claude-opus-4-7", _1M),
    ("openrouter/anthropic/claude-haiku-4-5", _200K),
    ("eu.anthropic.claude-haiku-4-5", _200K),
    ("global.anthropic.claude-opus-4-7", _1M),
    ("anthropic.claude-sonnet-4-5", _200K),
    ("us.anthropic.claude-sonnet-5-5", _1M),
    ("bedrock:claude-haiku-4-5", _200K),
    ("anthropic/claude-sonnet-4-6[1m]", _1M),
    ("us.anthropic.claude-sonnet-4-6", _200K),
])
def test_prefixed_ids_resolve_like_bare_ids(measure, model, expected):
    bare = re.sub(r"^(.*/)*(?:(?:anthropic|us|eu|global|bedrock)\.)*", "",
                  model.lower()).replace("[1m]", "")
    assert measure._claude_model_window(model) == expected
    # And identical to the bare spelling's answer.
    assert measure._claude_model_window(model) == \
        measure._claude_model_window(bare + ("[1m]" if "[1m]" in model else ""))


def test_dotted_prefix_not_confused_by_family_word(measure):
    """Only KNOWN provider/region tokens strip on a dot: ``us.`` strips, the
    unknown ``sonnet.`` does not, so the rest parses exactly like the
    unprefixed form (bare ``sonnet`` alias + suffix)."""
    assert measure._claude_model_window("us.sonnet.claude-haiku-4-5") == \
        measure._claude_model_window("sonnet.claude-haiku-4-5")


# ---------------------------------------------------------------------------
# modelSettings matching: canonicalized key match survives provider prefixes
# ---------------------------------------------------------------------------


def test_canonical_compact_model_id_strips_provider_prefixes(measure):
    assert measure._canonical_compact_model_id(
        "us.anthropic.claude-haiku-4-5") == "claude-haiku-4-5"
    assert measure._canonical_compact_model_id(
        "anthropic/claude-sonnet-4-6[1m]") == "claude-sonnet-4-6"


def test_model_settings_apply_to_prefixed_model(measure):
    settings = {"modelSettings": {"claude-haiku-4-5": {"autoCompactWindow": 150_000}}}
    r = measure._resolve_compact_window(
        "us.anthropic.claude-haiku-4-5", env={}, settings=settings)
    assert r["tokens"] == 150_000
    assert r["user_override"] is True


# ---------------------------------------------------------------------------
# hermes_session mirror
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model,expected", [
    ("anthropic/claude-haiku-4-5", _200K),
    ("bedrock/claude-sonnet-4-5", _200K),
    ("us.anthropic.claude-haiku-4-5", _200K),
    ("us.anthropic.claude-sonnet-5-5", _1M),
    ("eu.anthropic.claude-opus-4-7", _1M),
    ("anthropic.claude-haiku-4-5", _200K),
    ("global.anthropic.claude-sonnet-5", _1M),
])
def test_hermes_prefixed_ids(model, expected):
    assert hermes_session.context_window_for_model(model) == expected


# ---------------------------------------------------------------------------
# statusline.js mirror: modelSettings key match on a prefixed model id
# ---------------------------------------------------------------------------

STATUSLINE = SCRIPTS / "statusline.js"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_statusline_model_settings_match_prefixed_model(tmp_path):
    install = tmp_path / "scripts"
    install.mkdir()
    shutil.copy2(STATUSLINE, install / "statusline.js")
    shutil.copy2(SCRIPTS / "measure.py", install / "measure.py")
    home = tmp_path / "home"
    (home / ".claude" / "token-optimizer").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps(
        {"modelSettings": {"claude-haiku-4-5": {"autoCompactWindow": 150_000}}}),
        encoding="utf-8")
    payload = {
        "model": {"id": "us.anthropic.claude-haiku-4-5", "display_name": "Haiku"},
        "workspace": {"current_dir": str(tmp_path)},
        "session_id": "bbbb1111-2222-3333-8444-bbbbbbbbbbbb",
        "context_window": {
            "context_window_size": 200_000,
            "used_percentage": 37,
            "remaining_percentage": 63,
            "current_usage": {"input_tokens": 10, "output_tokens": 5,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 74_985},
        },
    }
    import os
    env = dict(os.environ)
    for k in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
              "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"):
        env.pop(k, None)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    res = subprocess.run(["node", str(install / "statusline.js")],
                         input=json.dumps(payload), capture_output=True,
                         text=True, encoding="utf-8", timeout=60, env=env)
    assert res.returncode == 0, res.stderr
    out = ANSI.sub("", res.stdout)
    m = re.search(r"[█░]{10} (\d+)%", out)
    assert m, out
    # 75K/150K override = 50%; on the raw 200K window it would read 37%.
    assert int(m.group(1)) == 50
