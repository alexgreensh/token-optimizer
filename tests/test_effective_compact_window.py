#!/usr/bin/env python3
"""One resolver for "where will this session compact", and current model windows.

Spec (verified 2026-10-10 from code.claude.com/docs/en/model-config and
platform.claude.com pricing; see briefs/FACTS.md):

Windows:
  * Haiku 5.5: 1M on every plan, no [1m] suffix.
  * Sonnet 4.6 / Opus 4.6: 1M ONLY through [1m]; without it 200K.
  * Sonnet 5+, Opus 4.7+, Fable: 1M by default, no suffix.

effective_compact_window(model, env, settings) precedence:
  env CLAUDE_CODE_AUTO_COMPACT_WINDOW > per-model modelSettings
  autoCompactWindow (/autocompact, v2.1.288+) > top-level autoCompactWindow >
  default (about 967K on 1M models, the model limit on 200K models);
  clamped 100000..1000000, capped at the model window, then
  CLAUDE_AUTOCOMPACT_PCT_OVERRIDE applied (used percentage, cannot raise).
  Returns (tokens, provenance).

Run: python3 -m pytest tests/test_effective_compact_window.py -q
"""

from __future__ import annotations

import importlib.util
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
    settings = home / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(mod, "SETTINGS_PATH", settings)
    monkeypatch.setattr(mod, "CLAUDE_DIR", home)
    for key in ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
                "TOKEN_OPTIMIZER_CONTEXT_SIZE", "CLAUDE_CODE_DISABLE_1M_CONTEXT",
                "CLAUDE_MODEL", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(key, raising=False)
    yield mod
    sys.modules.pop("measure", None)


# ---------------------------------------------------------------------------
# Model context windows (table-driven, item 3)
# ---------------------------------------------------------------------------

MODEL_WINDOWS = [
    # Haiku 5.5: 1M on every plan, no [1m].
    ("claude-haiku-5-5", 1_000_000),
    ("claude-haiku-5-5-20261001", 1_000_000),
    # Older Haiku stays 200K.
    ("claude-haiku-4-5", 200_000),
    ("claude-haiku-4-5-20251001", 200_000),
    ("claude-3-haiku", 200_000),
    # Sonnet 4.6: 1M only with [1m]; without it 200K.
    ("claude-sonnet-4-6", 200_000),
    ("claude-sonnet-4-6-20250929", 200_000),
    ("claude-sonnet-4-6[1m]", 1_000_000),
    ("sonnet[1m]", 1_000_000),
    # Sonnet 5 and later: 1M by default.
    ("claude-sonnet-5", 1_000_000),
    ("claude-sonnet-5-5", 1_000_000),
    ("sonnet", 1_000_000),
    # Opus 4.6: 1M only with [1m]; Opus 4.7+ 1M by default.
    ("claude-opus-4-6", 200_000),
    ("claude-opus-4-6[1m]", 1_000_000),
    ("claude-opus-4-7", 1_000_000),
    ("claude-opus-4-8", 1_000_000),
    ("claude-opus-5", 1_000_000),
    ("claude-opus-5-5", 1_000_000),
    ("opus", 1_000_000),
    # Fable / Mythos: 1M by default.
    ("claude-fable-5", 1_000_000),
    ("claude-fable-5-1", 1_000_000),
    ("fable", 1_000_000),
    ("claude-mythos-5-1", 1_000_000),
    # A bare `haiku` alias maps to different versions per provider; the
    # conservative window stays 200K (matches the pre-existing behavior).
    ("haiku", 200_000),
]


@pytest.mark.parametrize("model,window", MODEL_WINDOWS)
def test_model_context_windows_table(measure, model, window):
    assert measure._claude_model_window(model) == window, (
        f"_claude_model_window({model!r}) must be {window} per the verified docs"
    )


def test_context_size_env_override_still_wins(measure, monkeypatch):
    """TOKEN_OPTIMIZER_CONTEXT_SIZE remains the hard override (item 3)."""
    monkeypatch.setenv("TOKEN_OPTIMIZER_CONTEXT_SIZE", "200000")
    assert measure._context_window_for_model_str("claude-sonnet-5") == 200_000
    assert measure._context_window_for_model_str("claude-haiku-5-5") == 200_000


# ---------------------------------------------------------------------------
# effective_compact_window (item 2)
# ---------------------------------------------------------------------------

def test_default_on_1m_model_is_967k(measure):
    window, provenance = measure.effective_compact_window(
        "claude-sonnet-5", env={}, settings={})
    assert window == 967_000
    assert "967" in provenance and "default" in provenance


def test_default_on_200k_model_is_the_model_limit(measure):
    window, provenance = measure.effective_compact_window(
        "claude-sonnet-4-6", env={}, settings={})
    assert window == 200_000
    assert "200" in provenance


def test_env_var_beats_settings(measure):
    settings = {
        "autoCompactWindow": 300000,
        "modelSettings": {"claude-sonnet-5": {"autoCompactWindow": 400000}},
    }
    window, provenance = measure.effective_compact_window(
        "claude-sonnet-5",
        env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "500000"},
        settings=settings)
    assert window == 500_000
    assert "env" in provenance


def test_per_model_modelSettings_beats_top_level(measure):
    settings = {
        "autoCompactWindow": 300000,
        "modelSettings": {"claude-sonnet-5": {"autoCompactWindow": 400000}},
    }
    window, provenance = measure.effective_compact_window(
        "claude-sonnet-5", env={}, settings=settings)
    assert window == 400_000
    assert "modelSettings" in provenance


def test_top_level_autocompactwindow_used_when_no_per_model(measure):
    window, provenance = measure.effective_compact_window(
        "claude-sonnet-5", env={}, settings={"autoCompactWindow": 300000})
    assert window == 300_000
    assert "autoCompactWindow" in provenance


def test_modelSettings_auto_falls_back_to_default(measure):
    settings = {"modelSettings": {"claude-sonnet-5": {"autoCompactWindow": "auto"}}}
    window, provenance = measure.effective_compact_window(
        "claude-sonnet-5", env={}, settings=settings)
    assert window == 967_000
    assert "auto" in provenance and "default" in provenance


def test_modelSettings_matches_1m_and_date_suffixed_ids(measure):
    """Claude Code matches [1m] and date-suffixed IDs to the canonical entry."""
    settings = {"modelSettings": {"claude-opus-5-5": {"autoCompactWindow": 400000}}}
    for model in ("claude-opus-5-5", "claude-opus-5-5[1m]", "claude-opus-5-5-20261001"):
        window, _ = measure.effective_compact_window(model, env={}, settings=settings)
        assert window == 400_000, f"modelSettings must match {model!r}"


def test_modelSettings_real_shape_keyed_by_full_model_id(measure):
    """Shape seen in a real settings.json: keyed by full model id, a per-model
    object that also carries unrelated fields (effortLevel). The window field
    name inside it (autoCompactWindow) is NOT verified against a real file."""
    settings = {"modelSettings": {
        "claude-opus-5-5": {"effortLevel": "medium", "autoCompactWindow": 450000},
        "claude-sonnet-5-5": {"effortLevel": "high"},
    }}
    win, prov = measure.effective_compact_window("claude-opus-5-5", env={}, settings=settings)
    assert win == 450_000 and "modelSettings" in prov
    # A sibling entry without a window field must not be mistaken for an override.
    win2, _ = measure.effective_compact_window("claude-sonnet-5-5", env={}, settings=settings)
    assert win2 == 967_000


def test_window_clamped_to_100k_1m(measure):
    low, _ = measure.effective_compact_window(
        "claude-sonnet-5", env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "5000"}, settings={})
    high, _ = measure.effective_compact_window(
        "claude-sonnet-5", env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "5000000"}, settings={})
    assert low == 100_000
    assert high == 1_000_000


def test_window_capped_at_model_window(measure):
    """A 200K model cannot get a 500K compact window."""
    window, provenance = measure.effective_compact_window(
        "claude-sonnet-4-6",
        env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "500000"}, settings={})
    assert window == 200_000
    assert "cap" in provenance or "model" in provenance


def test_pct_override_scales_down(measure):
    """CLAUDE_AUTOCOMPACT_PCT_OVERRIDE is the USED percentage: 70% of 967K."""
    window, provenance = measure.effective_compact_window(
        "claude-sonnet-5",
        env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70"}, settings={})
    assert window == 967_000 * 70 // 100
    assert "70%" in provenance or "PCT" in provenance


def test_pct_override_cannot_raise(measure):
    for pct in ("100", "150", "0", "-5"):
        window, provenance = measure.effective_compact_window(
            "claude-sonnet-5",
            env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": pct}, settings={})
        assert window == 967_000, f"pct={pct} must not raise the threshold"


def test_pct_override_applies_to_explicit_window(measure):
    window, _ = measure.effective_compact_window(
        "claude-sonnet-5",
        env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000",
             "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"}, settings={})
    assert window == 200_000


# ---------------------------------------------------------------------------
# The fill denominator follows the effective window (quality score)
# ---------------------------------------------------------------------------

def _quality_data(tokens, model="claude-sonnet-5", window=1_000_000):
    return {
        "messages": [("user", "hello world " * 10, 40, None)],
        "tool_results": [],
        "system_reminders": [],
        "reads": [],
        "writes": [],
        "compactions": 0,
        "agent_dispatches": [],
        "decisions": [],
        "context_tokens": tokens,
        "model_context_window": window,
        "model": model,
        "tool_calls": 0,
    }


def test_quality_score_fill_uses_effective_window(measure, monkeypatch):
    """150K tokens against a 200K compact window = 75% fill, not 15%."""
    settings = {"autoCompactWindow": 200000}
    monkeypatch.setattr(measure, "_read_settings_json", lambda: (settings, None))
    monkeypatch.setattr(measure, "_resolve_feature_env", lambda v: None)

    result = measure.compute_quality_score(_quality_data(150_000), session_id="s1")
    fill = result["breakdown"]["context_fill_degradation"]
    assert fill["fill_pct"] == 75.0, (
        "the fill percent must use the effective compact window as denominator "
        "when it is smaller than the model window"
    )
    assert fill.get("compact_window") == 200_000


def test_quality_score_fill_unchanged_when_window_not_reduced(measure, monkeypatch):
    monkeypatch.setattr(measure, "_read_settings_json", lambda: ({}, None))
    monkeypatch.setattr(measure, "_resolve_feature_env", lambda v: None)
    result = measure.compute_quality_score(_quality_data(150_000), session_id="s1")
    fill = result["breakdown"]["context_fill_degradation"]
    assert fill["fill_pct"] == 15.0


def test_quality_score_fill_warning_fires_earlier_with_small_window(measure, monkeypatch):
    """A fill warning must fire from the EFFECTIVE fill, not the model fill."""
    settings = {"autoCompactWindow": 200000}
    monkeypatch.setattr(measure, "_read_settings_json", lambda: (settings, None))
    monkeypatch.setattr(measure, "_resolve_feature_env", lambda v: None)
    result = measure.compute_quality_score(_quality_data(190_000), session_id="s1")
    assert result["fill_warning"] is not None, (
        "190K/200K = 95% effective fill must raise the fill warning"
    )
    assert result["fill_warning"]["fill_pct"] == 95.0


def test_no_fixed_percent_compaction_advice_left_in_measure():
    """Compaction advice follows the resolver / current facts, not a fixed 50-70%."""
    text = (SCRIPTS / "measure.py").read_text(encoding="utf-8")
    assert "Use /compact at 50-70%" not in text
    assert "Compact around 50-70%" not in text
    assert "undocumented and has inverted" not in text
