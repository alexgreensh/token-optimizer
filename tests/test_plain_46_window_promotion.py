#!/usr/bin/env python3
"""Plain claude-opus-4-6 / claude-sonnet-4-6 transcript ids may be the 1M variant.

Claude Code writes the plain API id into the transcript, never the `[1m]`
suffix (a real machine: 272K opus-4-6 lines, 0 with [1m]). A 4.6 session that
runs the 1M variant was therefore sized 200K, and 250K tokens became a
confident "100% full, window contradicted".

Rules pinned here:
  * tokens above the window prove the window is bigger: promote 4.6 to 1M and
    say so (`window_inferred_from_tokens`), instead of clamping to 100%.
  * a `[1m]` on the settings/env model wins for a plain 4.6 transcript id.
  * promotion is NOT justified for ids that cannot be 1M (opus-4-5), nor when
    tokens exceed even 1M: those stay "contradicted", never a fake 100%.

Run: python3 -m pytest tests/test_plain_46_window_promotion.py -q
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def measure(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    spec.loader.exec_module(mod)
    home = tmp_path / "claude"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(mod, "SETTINGS_PATH", home / "settings.json")
    monkeypatch.setattr(mod, "CLAUDE_DIR", home)
    monkeypatch.setattr(mod, "QUALITY_CACHE_DIR", tmp_path / "qcache")
    for key in ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
                "TOKEN_OPTIMIZER_CONTEXT_SIZE", "CLAUDE_CODE_DISABLE_1M_CONTEXT",
                "CLAUDE_MODEL", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(key, raising=False)
    mod._cli_context_size = None
    yield mod, home
    sys.modules.pop("measure", None)


def _score(mod, tmp_path, model, tokens):
    recs = []
    for i in range(6):
        recs.append({"type": "user", "message": {"content": [
            {"type": "text", "text": "please do step %d of the migration now" % i}]}})
        recs.append({"type": "assistant", "message": {
            "model": model,
            "content": [{"type": "text", "text": "working on step %d done" % i}],
            "usage": {"input_tokens": 10, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": tokens * (i + 1) // 6}}})
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")
    qd = mod._parse_jsonl_for_quality(p)
    res = mod.compute_quality_score(qd, session_id="s")
    return res["breakdown"]["context_fill_degradation"]


@pytest.mark.parametrize("model", ["claude-opus-4-6", "claude-sonnet-4-6",
                                   "claude-opus-4-6-20260301"])
def test_tokens_above_200k_promote_plain_46_to_1m(measure, tmp_path, model):
    mod, _ = measure
    cfd = _score(mod, tmp_path, model, 250_000)
    assert cfd["model_context_window"] == 1_000_000
    assert cfd["window_contradicted"] is False
    assert cfd["window_inferred_from_tokens"] is True
    assert cfd["fill_pct"] == pytest.approx(25.0, abs=0.5)
    assert cfd["fill_source"] == "transcript-tokens"


def test_plain_46_under_200k_stays_200k_and_not_inferred(measure, tmp_path):
    mod, _ = measure
    cfd = _score(mod, tmp_path, "claude-opus-4-6", 120_000)
    assert cfd["model_context_window"] == 200_000
    assert cfd["window_inferred_from_tokens"] is False
    assert cfd["window_contradicted"] is False
    assert cfd["fill_pct"] == pytest.approx(60.0, abs=0.5)


def test_settings_model_with_1m_suffix_wins_for_plain_46(measure, tmp_path):
    mod, home = measure
    (home / "settings.json").write_text(json.dumps({"model": "opus[1m]"}), encoding="utf-8")
    cfd = _score(mod, tmp_path, "claude-opus-4-6", 120_000)
    assert cfd["model_context_window"] == 1_000_000
    assert cfd["fill_pct"] == pytest.approx(12.0, abs=0.5)
    assert "[1m]" in cfd["model_context_window_source"]


def test_env_model_with_1m_suffix_wins_for_plain_46(measure, tmp_path, monkeypatch):
    mod, _ = measure
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-6[1m]")
    cfd = _score(mod, tmp_path, "claude-sonnet-4-6", 120_000)
    assert cfd["model_context_window"] == 1_000_000


def test_settings_without_1m_suffix_does_not_promote(measure, tmp_path):
    mod, home = measure
    (home / "settings.json").write_text(json.dumps({"model": "claude-opus-4-6"}), encoding="utf-8")
    cfd = _score(mod, tmp_path, "claude-opus-4-6", 120_000)
    assert cfd["model_context_window"] == 200_000


def test_one_m_settings_do_not_promote_a_model_that_cannot_be_1m(measure, tmp_path):
    """The settings [1m] describes the user's chosen model; a transcript that
    names opus-4-5 is not it, and 4.5 has no 1M variant."""
    mod, home = measure
    (home / "settings.json").write_text(json.dumps({"model": "opus[1m]"}), encoding="utf-8")
    cfd = _score(mod, tmp_path, "claude-opus-4-5", 120_000)
    assert cfd["model_context_window"] == 200_000


def test_tokens_over_window_for_non_46_stay_contradicted(measure, tmp_path):
    mod, _ = measure
    cfd = _score(mod, tmp_path, "claude-opus-4-5", 250_000)
    assert cfd["model_context_window"] == 200_000
    assert cfd["window_contradicted"] is True
    assert cfd["window_inferred_from_tokens"] is False


def test_tokens_over_1m_for_plain_46_stay_contradicted(measure, tmp_path):
    mod, _ = measure
    cfd = _score(mod, tmp_path, "claude-opus-4-6", 1_200_000)
    assert cfd["window_contradicted"] is True


def test_explicit_context_size_override_is_never_promoted(measure, tmp_path, monkeypatch):
    mod, _ = measure
    monkeypatch.setenv("TOKEN_OPTIMIZER_CONTEXT_SIZE", "200000")
    cfd = _score(mod, tmp_path, "claude-opus-4-6", 250_000)
    assert cfd["model_context_window"] == 200_000
    assert cfd["window_contradicted"] is True
