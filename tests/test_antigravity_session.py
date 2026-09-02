"""U4 — Antigravity session normalizer."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "token-optimizer" / "scripts"))

from antigravity_session import (  # noqa: E402
    model_id_for_display_name,
    normalize_session,
)

_SCRIPTS = str(Path(__file__).resolve().parents[1] / "skills" / "token-optimizer" / "scripts")


def _raw(**overrides):
    base = {
        "conversation_id": "abc-123",
        "surface": "antigravity-cli",
        "generations": [{"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0,
                         "thinking_tokens": 0, "response_tokens": 1, "model_display_name": "Gemini 3.5 Flash (Medium)",
                         "estimated_tokens_used": 0, "max_context_tokens": 256000}],
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 20,
        "thinking_tokens": 30,
        "last_fill": 0.5,
        "last_max_context": 256000,
        "model_display_name": "Gemini 3.5 Flash (Medium)",
        "undecodable_rows": 0,
        "tool_call_count": 3,
        "user_input_count": 2,
        "start_time": 1000.0,
        "end_time": 1120.0,
        "title": None,
        "workspace": "/home/me/proj",
        "killed": False,
        "not_fully_idle": False,
        "nesting_depth": 0,
    }
    base.update(overrides)
    return base


def test_two_geminiple_generations_summed():
    s = normalize_session(_raw(input_tokens=100, output_tokens=50, cache_read_tokens=20, thinking_tokens=30, user_input_count=2))
    assert s is not None
    assert s["total_input_tokens"] == 120  # fresh + cache_read rollup
    assert s["total_output_tokens"] == 50
    assert s["total_cache_read"] == 20
    assert s["model_id"] == "gemini-3.5-flash"
    assert s["cost_source"] == "antigravity_list_price_estimate"
    assert s["dedup_key"] == "antigravity:antigravity-cli:abc-123"
    assert s["runtime"] == "antigravity"
    assert s["billing_provider"] == "google-antigravity"


def test_import_does_not_pull_measure():
    code = (
        "import sys; sys.path.insert(0, %r); "
        "import antigravity_session; "
        "print('measure' in sys.modules)" % _SCRIPTS
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"


def test_credits_cost_source():
    s = normalize_session(_raw(consumed_credits=12))
    assert s["credits"] == 12
    assert s["cost_source"] == "antigravity_credits"


def test_unknown_model_retained_no_cost():
    s = normalize_session(_raw(model_display_name="Some Future Model"))
    assert s is not None
    assert s["cost_usd"] == 0.0
    assert s["cost_source"] == "antigravity_no_cost_data"
    assert s["model"] == "Some Future Model"
    assert s["model_id"] is None


def test_model_context_window():
    s = normalize_session(_raw(last_max_context=256000))
    assert s["model_context_window"] == 256000
    s2 = normalize_session(_raw(last_max_context=None))
    assert s2["model_context_window"] == 256000  # default lookup


def test_killed_incomplete():
    s = normalize_session(_raw(killed=True))
    assert s["incomplete"] is True
    assert s["end_reason"] == "killed"


def test_empty_raw_is_none():
    assert normalize_session({}) is None
    assert normalize_session(None) is None
    assert normalize_session(_raw(generations=[], user_input_count=0, input_tokens=0, output_tokens=0)) is None


def test_quality_dict_carries_score_grade_band():
    s = normalize_session(_raw())
    assert s is not None
    assert "score" in s["quality"]
    assert "grade" in s["quality"]
    assert "band" in s["quality"]
    assert s["quality_score"] is not None
    assert s["quality_grade"] is not None
    assert s["quality_band"] is not None


def test_model_id_mapping():
    assert model_id_for_display_name("Gemini 3.5 Flash (Medium)") == "gemini-3.5-flash"
    assert model_id_for_display_name("Gemini 3.6 Flash") == "gemini-3.6-flash"
    assert model_id_for_display_name("Gemini 3.7 Flash (High)") is None
    assert model_id_for_display_name("Some Future Model") is None
    assert model_id_for_display_name("") is None
    assert model_id_for_display_name(None) is None
