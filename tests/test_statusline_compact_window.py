"""statusline.js: fill bar uses the effective compact window; prompt_cache is bridged.

The terminal status line must divide live tokens by the window the session will
actually compact at when the user shrank it (env, /autocompact modelSettings,
autoCompactWindow, CLAUDE_AUTOCOMPACT_PCT_OVERRIDE), exactly like the Python
resolver (measure.effective_compact_window) and the desktop band (PR #210):
the tuned default is NOT an override, so the host's own percentage stands.

Run: python3 -m pytest tests/test_statusline_compact_window.py -q
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
STATUSLINE = SCRIPTS / "statusline.js"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

SID = "bbbb1111-2222-3333-8444-bbbbbbbbbbbb"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _run(tmp_path, payload, settings=None, env_extra=None):
    install = tmp_path / "scripts"
    install.mkdir(exist_ok=True)
    shutil.copy2(STATUSLINE, install / "statusline.js")
    shutil.copy2(SCRIPTS / "measure.py", install / "measure.py")
    home = tmp_path / "home"
    # The hooks create the cache dir in real installs; the status line only writes into it.
    (home / ".claude" / "token-optimizer").mkdir(parents=True, exist_ok=True)
    if settings is not None:
        (home / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    import os
    env = dict(os.environ)
    for k in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"):
        env.pop(k, None)
    env.update({"HOME": str(home), "USERPROFILE": str(home),
                "HOMEDRIVE": home.drive or env.get("HOMEDRIVE", ""),
                "HOMEPATH": str(home)[len(home.drive):] if home.drive else str(home)})
    env.update(env_extra or {})
    res = subprocess.run(["node", str(install / "statusline.js")], input=json.dumps(payload),
                         capture_output=True, text=True, encoding="utf-8", timeout=60, env=env)
    assert res.returncode == 0, res.stderr
    return ANSI.sub("", res.stdout), home / ".claude" / "token-optimizer"


def _payload(tokens=200_000, window=1_000_000, model="claude-opus-5-5", host_pct=None, **extra):
    pct = host_pct if host_pct is not None else tokens / window * 100
    p = {
        "model": {"id": model, "display_name": "Opus"},
        "workspace": {"current_dir": "."},
        "session_id": SID,
        "context_window": {
            "context_window_size": window,
            "used_percentage": pct,
            "remaining_percentage": 100 - pct,
            "current_usage": {"input_tokens": 10, "output_tokens": 5,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": tokens - 10},
        },
    }
    p.update(extra)
    return p


def _pct_shown(out):
    m = re.search(r"[█░]{10} (\d+)%", out)
    assert m, out
    return int(m.group(1))


def test_no_override_shows_host_percentage(tmp_path):
    out, _ = _run(tmp_path, _payload(tokens=200_000))
    assert _pct_shown(out) == 20


def test_env_window_is_the_denominator(tmp_path):
    out, cache = _run(tmp_path, _payload(tokens=200_000),
                      env_extra={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000"})
    assert _pct_shown(out) == 50
    live = json.loads((cache / "live-fill.json").read_text())
    # live-fill keeps the HOST's model-window fill + the numerator for Python.
    assert live["used_percentage"] == 20
    assert live["context_tokens"] == 200_000


def test_per_model_autocompact_beats_top_level(tmp_path):
    settings = {"autoCompactWindow": 800_000,
                "modelSettings": {"claude-opus-5-5": {"autoCompactWindow": 250_000}}}
    out, _ = _run(tmp_path, _payload(tokens=200_000), settings=settings)
    assert _pct_shown(out) == 80


def test_other_models_entry_is_ignored(tmp_path):
    settings = {"modelSettings": {"claude-sonnet-5-5": {"autoCompactWindow": 250_000}}}
    out, _ = _run(tmp_path, _payload(tokens=200_000), settings=settings)
    assert _pct_shown(out) == 20


def test_top_level_setting_and_clamp(tmp_path):
    out, _ = _run(tmp_path, _payload(tokens=50_000), settings={"autoCompactWindow": 10_000})
    assert _pct_shown(out) == 50  # 10K clamps up to 100K


def test_window_above_model_window_is_not_a_reduction(tmp_path):
    out, _ = _run(tmp_path, _payload(tokens=100_000, window=200_000, model="claude-opus-4-6"),
                  settings={"autoCompactWindow": 900_000})
    assert _pct_shown(out) == 50  # capped at 200K == model window: host number


def test_pct_override_scales_the_window(tmp_path):
    out, _ = _run(tmp_path, _payload(tokens=250_000),
                  env_extra={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"})
    # 50% of the ~967K default = 483.5K -> 250K / 483.5K = 52%
    assert _pct_shown(out) == 52


def test_pct_override_cannot_raise(tmp_path):
    out, _ = _run(tmp_path, _payload(tokens=200_000),
                  env_extra={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "100"})
    assert _pct_shown(out) == 20


@pytest.mark.parametrize("env,settings,model,window", [
    ({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000"}, {}, "claude-opus-5-5", 1_000_000),
    ({}, {"autoCompactWindow": 300_000}, "claude-opus-5-5", 1_000_000),
    ({}, {"modelSettings": {"claude-opus-5-5": {"autoCompactWindow": 300_000}},
          "autoCompactWindow": 700_000}, "claude-opus-5-5[1m]", 1_000_000),
    ({"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "40"}, {}, "claude-sonnet-5-5", 1_000_000),
    ({"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "40"}, {}, "claude-opus-4-6", 200_000),
    ({}, {"autoCompactWindow": 150_000}, "claude-opus-4-6", 200_000),
])
def test_js_matches_python_resolver(tmp_path, env, settings, model, window):
    """Parity: the JS bar divides by exactly the Python resolver's answer
    whenever the resolver reports a real user override below the window."""
    import measure
    py = measure._resolve_compact_window(model, env=env, settings=settings)
    tokens = 90_000
    out, _ = _run(tmp_path, _payload(tokens=tokens, window=window, model=model),
                  settings=settings, env_extra=env)
    denom = py["tokens"] if (py["user_override"] and py["tokens"] < window) else window
    expected = min(100, int(tokens / denom * 100 + 0.5))  # JS Math.round: half up
    assert _pct_shown(out) == expected


def test_prompt_cache_object_is_bridged_to_a_sidecar(tmp_path):
    pc = {"ttl": "1h", "expires_at": 1_900_000_000, "warm": True, "hit_ratio": 0.9}
    _, cache = _run(tmp_path, _payload(prompt_cache=pc))
    side = json.loads((cache / f"prompt-cache-{SID}.json").read_text())
    assert side["ttl"] == "1h" and side["expires_at"] == 1_900_000_000 and side["warm"] is True
    assert "timestamp" in side


def test_no_prompt_cache_object_writes_no_sidecar(tmp_path):
    _, cache = _run(tmp_path, _payload())
    assert not (cache / f"prompt-cache-{SID}.json").exists()


# ---------------------------------------------------------------------------
# F5/F6: the JS twin follows the host's env parsing and `/autocompact auto`
# ---------------------------------------------------------------------------

_ENV_VECTORS = json.loads(
    (REPO / "tests" / "fixtures" / "compact_window_env_vectors.json").read_text(encoding="utf-8")
)["vectors"]


@pytest.mark.parametrize("vec", _ENV_VECTORS, ids=lambda v: repr(v["raw"])[:40])
def test_env_window_parses_like_the_host(tmp_path, vec):
    """Same shared vectors as the Python resolver and the desktop parser."""
    tokens = 90_000
    out, _ = _run(tmp_path, _payload(tokens=tokens),
                  env_extra={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": vec["raw"]})
    denom = vec["window"] if vec["window"] is not None else 1_000_000
    assert _pct_shown(out) == min(100, int(tokens / denom * 100 + 0.5)), vec


def test_model_settings_auto_beats_top_level(tmp_path):
    settings = {"autoCompactWindow": 300_000,
                "modelSettings": {"claude-opus-5-5": {"autoCompactWindow": "auto"}}}
    out, _ = _run(tmp_path, _payload(tokens=200_000), settings=settings)
    assert _pct_shown(out) == 20  # tuned default: the host's own number, not 200K/300K = 67%


def test_model_settings_auto_is_per_model(tmp_path):
    settings = {"autoCompactWindow": 400_000,
                "modelSettings": {"claude-sonnet-5-5": {"autoCompactWindow": "auto"}}}
    out, _ = _run(tmp_path, _payload(tokens=200_000), settings=settings)
    assert _pct_shown(out) == 50  # opus is not the model set to auto


def test_exact_id_beats_family_alias_regardless_of_key_order(tmp_path):
    """F-T2-4: {"opus": 250K, "claude-opus-5-5": 400K} -- the alias iterates
    first but the exact id must win (200K/400K = 50%, not 200K/250K = 80%)."""
    settings = {"modelSettings": {"opus": {"autoCompactWindow": 250_000},
                                  "claude-opus-5-5": {"autoCompactWindow": 400_000}}}
    out, _ = _run(tmp_path, _payload(tokens=200_000), settings=settings)
    assert _pct_shown(out) == 50


def test_family_alias_still_applies_when_no_exact_key(tmp_path):
    settings = {"modelSettings": {"opus": {"autoCompactWindow": 250_000}}}
    out, _ = _run(tmp_path, _payload(tokens=200_000), settings=settings)
    assert _pct_shown(out) == 80


def test_pct_override_accepts_a_float(tmp_path):
    out, _ = _run(tmp_path, _payload(tokens=250_000),
                  env_extra={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50.5"})
    # 50.5% of 967K = 488,335 -> 250K / 488,335 = 51%
    assert _pct_shown(out) == 51


@pytest.mark.parametrize("env,settings", [
    ({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "5e5"}, {}),
    ({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "500,000"}, {}),
    ({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "0"}, {"autoCompactWindow": 300_000}),
    ({"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "37.5"}, {}),
    ({}, {"autoCompactWindow": 300_000,
          "modelSettings": {"claude-opus-5-5": {"autoCompactWindow": "auto"}}}),
])
def test_js_matches_python_resolver_host_cases(tmp_path, env, settings):
    import measure
    py = measure._resolve_compact_window("claude-opus-5-5", env=env, settings=settings)
    tokens = 90_000
    out, _ = _run(tmp_path, _payload(tokens=tokens), settings=settings, env_extra=env)
    denom = py["tokens"] if (py["user_override"] and py["tokens"] < 1_000_000) else 1_000_000
    assert _pct_shown(out) == min(100, int(tokens / denom * 100 + 0.5))
