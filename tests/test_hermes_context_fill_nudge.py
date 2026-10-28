"""Regression tests for the Hermes context-fill nudge (cumulative vs live prompt size).

WHY THIS EXISTS
---------------
The nudge divided the session-CUMULATIVE input tally by the model window. Every host re-sends the
whole conversation on each turn, so that sum climbs past the window no matter how small the live
context is, and the nudge eventually announced a context emergency that did not exist.

Measured on a real Hermes session (2026-09-15, 129 API calls): the plugin's cumulative figure was
1,285,803 tokens against a 1,000,000 window -> "Context ~100% full ... Grade: F" (the percentage is
capped at 100), while Hermes itself reported 278,545 / 1,000,000 = 28% for the same session.

The fill must come from the prompt the LAST call actually sent (fresh + cached prompt tokens);
the cumulative tally stays available for cost/usage reporting.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HERMES_INIT = REPO / "hermes" / "__init__.py"
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))
WINDOW = 1_000_000


@pytest.fixture()
def plugin():
    """Load hermes/__init__.py by path, with a fixed window and clean per-session state."""
    spec = importlib.util.spec_from_file_location("to_hermes_init_under_test", HERMES_INIT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    # Deterministic window: the real resolver reads a model catalog we do not want to couple to.
    module._context_window = lambda model: WINDOW  # noqa: SLF001 — deliberate test seam
    module._native_compression_needs_help = lambda session_id: True  # fallback-policy seam
    # Fill-band checkpoints start background processes; not under test here.
    module._maybe_checkpoint = lambda *a, **k: False
    # Lean-output nudge and continuity run a child process per turn; covered
    # in test_hermes_parity.py, not under test here.
    module._lean_output_reading = lambda *a, **k: None

    module._TALLY.clear()
    module._NUDGED.clear()
    module._ROLLED_UP.clear()
    yield module
    module._TALLY.clear()
    module._NUDGED.clear()
    module._ROLLED_UP.clear()


def _call(plugin, session_id: str, input_tokens: int, cache_read: int = 0,
          cache_write: int = 0) -> None:
    plugin.on_post_api_request(
        session_id=session_id,
        usage={"input_tokens": input_tokens, "output_tokens": 1, "cache_read_tokens": cache_read,
               "cache_write_tokens": cache_write, "reasoning_tokens": 0},
    )


def _nudge(plugin, session_id: str, history_len: int = 10):
    return plugin.on_pre_llm_call(
        session_id=session_id,
        model="any-model",
        conversation_history=[{"role": "user", "content": "x"}] * history_len,
    )


def test_cumulative_tally_never_triggers_the_nudge(plugin):
    """Many small turns: cumulative input exceeds the window, live occupancy stays tiny."""
    for _ in range(200):
        _call(plugin, "s-cumulative", input_tokens=10_000, cache_read=0)

    tally = plugin._TALLY["s-cumulative"]
    assert tally["input"] == 2_000_000 > WINDOW, "precondition: cumulative tally passed the window"
    assert tally["last_prompt"] == 10_000, "live prompt size must be the LAST call only"

    assert _nudge(plugin, "s-cumulative") is None


def test_nudge_fires_on_live_prompt_size(plugin):
    """A genuinely full live context still nudges, and reports the live number."""
    _call(plugin, "s-live", input_tokens=5_000, cache_read=0)
    _call(plugin, "s-live", input_tokens=790_000, cache_read=0)  # 79% of the window, fresh tokens

    nudge = _nudge(plugin, "s-live")
    assert nudge is not None and "context" in nudge
    text = nudge["context"]
    assert "790,000" in text and "1,000,000" in text
    assert "79%" in text
    assert "795,000" not in text  # never the cumulative figure (5,000 + 790,000)


def test_cached_prompt_tokens_count_toward_fill(plugin):
    """Cached prompt tokens occupy the window too — ignoring them undersells the fill."""
    _call(plugin, "s-cache", input_tokens=100, cache_read=750_000)

    nudge = _nudge(plugin, "s-cache")
    assert nudge is not None
    assert "750,100" in nudge["context"]  # input (100) + cache_read (750,000); exact, not rounded


def test_cache_write_tokens_count_toward_fill(plugin):
    """A cache-CREATION turn writes tokens that also occupy the window. Counting only
    input + cache_read would undercount the fill on that turn; the live prompt is the
    full prompt_tokens = input + cache_read + cache_write."""
    _call(plugin, "s-write", input_tokens=100, cache_read=200_000, cache_write=600_000)

    assert plugin._TALLY["s-write"]["last_prompt"] == 800_100
    nudge = _nudge(plugin, "s-write")
    assert nudge is not None
    assert "800,100" in nudge["context"]


def test_zero_usage_call_does_not_reset_last_prompt(plugin):
    """An errored/retried request can report empty/zero usage. It must NOT clobber the
    good live reading with 0 (which would make on_pre_llm_call treat the next turn as the
    first turn and fall back to the history estimate, leaving a ~full window unwarned)."""
    _call(plugin, "s-zero", input_tokens=950_000, cache_read=0)  # 95% of the window
    assert plugin._TALLY["s-zero"]["last_prompt"] == 950_000

    # All-zero usage (errored/retried request): no positive prompt anywhere.
    plugin.on_post_api_request(
        session_id="s-zero",
        usage={"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
               "cache_write_tokens": 0, "reasoning_tokens": 0},
    )
    assert plugin._TALLY["s-zero"]["last_prompt"] == 950_000, "zero usage must not reset last_prompt"

    nudge = _nudge(plugin, "s-zero")
    assert nudge is not None and "context" in nudge
    assert "95%" in nudge["context"]
    assert "950,000" in nudge["context"]


def test_prompt_tokens_field_is_preferred_when_present(plugin):
    """When the host supplies canonical prompt_tokens, use it verbatim (drift-proof)."""
    plugin.on_post_api_request(
        session_id="s-canon",
        usage={"input_tokens": 100, "output_tokens": 1, "cache_read_tokens": 200,
               "cache_write_tokens": 300, "reasoning_tokens": 0, "prompt_tokens": 880_000},
    )
    assert plugin._TALLY["s-canon"]["last_prompt"] == 880_000


def test_nudge_is_once_per_session(plugin):
    _call(plugin, "s-once", input_tokens=900_000)
    assert _nudge(plugin, "s-once") is not None
    assert _nudge(plugin, "s-once") is None


def test_first_turn_without_tally_estimates_from_history(plugin):
    """Before any completed call the history estimate is still the fallback of record."""
    # ~3.3 chars per token: 300k chars ~= 90k tokens -> silent.
    short = plugin.on_pre_llm_call(
        session_id="s-first",
        model="any-model",
        conversation_history=[{"role": "user", "content": "y" * 300_000}],
    )
    assert short is None

    # ~3.3M chars ~= 1M tokens -> over the window, nudges.
    long_history = plugin.on_pre_llm_call(
        session_id="s-first-long",
        model="any-model",
        conversation_history=[{"role": "user", "content": "y" * 3_300_000}],
    )
    assert long_history is not None


def test_native_healthy_compressor_suppresses_competing_nudge(plugin, monkeypatch):
    """Hermes owns compression; TO stays observational while it is healthy."""
    monkeypatch.setattr(plugin, "_native_compression_needs_help", lambda _sid: False)
    _call(plugin, "s-native-healthy", 800_000)
    assert _nudge(plugin, "s-native-healthy") is None


def test_failed_native_compressor_allows_fallback_nudge(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_native_compression_needs_help", lambda _sid: True)
    _call(plugin, "s-native-failed", 800_000)
    nudge = _nudge(plugin, "s-native-failed")
    assert nudge is not None
    assert "800,000" in nudge["context"]


def test_nudge_grade_matches_stored_grade_at_high_fill(plugin):
    """The nudge must grade with live occupancy, same as the stored rollup.
    ~90% full is a D (fill 10*.40 + msg 100*.35 + oi 15*.25 = 43) — the
    fill-blind renormalized path would report a softer C (65)."""
    _call(plugin, "s-grade", input_tokens=900_000)
    nudge = _nudge(plugin, "s-grade")
    assert nudge is not None
    assert "Grade: D" in nudge["context"]



def _load_unpatched_plugin():
    spec = importlib.util.spec_from_file_location("to_hermes_policy_under_test", HERMES_INIT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_native_compression_disabled_needs_help(monkeypatch):
    actual = _load_unpatched_plugin()
    config_mod = types.ModuleType("hermes_cli.config")
    config_mod.load_config_readonly = lambda: {"compression": {"enabled": False}}
    package = types.ModuleType("hermes_cli")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_mod)
    assert actual._native_compression_needs_help("s-disabled") is True


def test_native_compression_flat_bool_disabled_needs_help(monkeypatch):
    """``compression = false`` as a bare boolean (not a table) must also read
    as disabled — a config that style would otherwise silently keep the gate
    in observer mode while the native compressor never runs."""
    actual = _load_unpatched_plugin()
    config_mod = types.ModuleType("hermes_cli.config")
    config_mod.load_config_readonly = lambda: {"compression": False}
    package = types.ModuleType("hermes_cli")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_mod)
    assert actual._native_compression_needs_help("s-flat-disabled") is True


def test_native_compression_failure_needs_help(monkeypatch):
    actual = _load_unpatched_plugin()
    _install_native_policy_modules(
        monkeypatch, actual, {"compression_ineffective_count": 2}
    )
    assert actual._native_compression_needs_help("s-failed") is True


def test_config_probe_failure_still_consults_health_probe(monkeypatch):
    """An unreadable config must fall through to the persisted session-health
    probe, not mask a compressor that is actually failing."""
    actual = _load_unpatched_plugin()
    config_mod = types.ModuleType("hermes_cli.config")

    def _unreadable():
        raise RuntimeError("config unreadable")

    config_mod.load_config_readonly = _unreadable
    package = types.ModuleType("hermes_cli")
    package.__path__ = []
    state_mod = types.ModuleType("hermes_state")
    state_mod.get_session = lambda _sid, **_kw: {"compression_ineffective_count": 2}
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_mod)
    monkeypatch.setattr(actual, "_import_state", lambda: state_mod, raising=False)
    assert actual._native_compression_needs_help("s-failed") is True


def test_health_probe_loads_sibling_state_module_by_path(tmp_path, monkeypatch):
    """In the installed layout the plugin must not rely on ``import
    hermes_state``: the host can already have its own same-named module bound
    in sys.modules, which would silently feed the health probe foreign data.
    The sibling file shipped next to __init__.py is loaded by path instead."""
    plugin_dir = tmp_path / "token-optimizer"
    plugin_dir.mkdir()
    shutil.copy(HERMES_INIT, plugin_dir / "__init__.py")
    shutil.copy(SCRIPTS / "hermes_state.py", plugin_dir / "hermes_state.py")

    # A foreign same-named module already bound, as a host's own hermes_state
    # would be: its tripped counters would wrongly force a "needs help" verdict.
    foreign = types.ModuleType("hermes_state")
    foreign.get_session = lambda *_a, **_kw: {"compression_ineffective_count": 99}
    monkeypatch.setitem(sys.modules, "hermes_state", foreign)

    # Point the real reader at an empty Hermes home so get_session finds no DB.
    empty_home = tmp_path / "hermes-home"
    empty_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(empty_home))
    monkeypatch.setenv("TOKEN_OPTIMIZER_ALLOW_UNSAFE_RUNTIME_HOME", "1")

    spec = importlib.util.spec_from_file_location(
        "to_hermes_init_sibling_test", plugin_dir / "__init__.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    state = module._import_state()
    assert state is not foreign
    assert Path(state.__file__).resolve() == (plugin_dir / "hermes_state.py").resolve()
    # And the probe used it: the foreign row would have returned True.
    assert module._native_compression_needs_help("s-nonexistent-test-session") is False


def _install_native_policy_modules(monkeypatch, module, row):
    config_mod = types.ModuleType("hermes_cli.config")
    config_mod.load_config_readonly = lambda: {"compression": {"enabled": True}}
    package = types.ModuleType("hermes_cli")
    package.__path__ = []
    state_mod = types.ModuleType("hermes_state")
    state_mod.get_session = lambda _sid, **_kw: row
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_mod)
    monkeypatch.setattr(module, "_import_state", lambda: state_mod)


def test_expired_failure_cooldown_does_not_need_help(monkeypatch):
    actual = _load_unpatched_plugin()
    _install_native_policy_modules(monkeypatch, actual, {
        "compression_failure_error": "old provider error",
        "compression_failure_cooldown_until": 2_000.0,
    })
    monkeypatch.setattr(actual.time, "time", lambda: 2_001.0)
    assert actual._native_compression_needs_help("s-recovered") is False


def test_active_failure_cooldown_needs_help(monkeypatch):
    actual = _load_unpatched_plugin()
    _install_native_policy_modules(monkeypatch, actual, {
        "compression_failure_error": "current provider error",
        "compression_failure_cooldown_until": 2_000.0,
    })
    monkeypatch.setattr(actual.time, "time", lambda: 1_999.0)
    assert actual._native_compression_needs_help("s-failed") is True


def test_expired_anti_thrash_recovery_does_not_need_help(monkeypatch):
    actual = _load_unpatched_plugin()
    _install_native_policy_modules(monkeypatch, actual, {
        "compression_fallback_streak": 2,
        "compression_ineffective_count": 2,
        "compression_recovery_deadline": 2_000.0,
    })
    monkeypatch.setattr(actual.time, "time", lambda: 2_001.0)
    assert actual._native_compression_needs_help("s-probe-due") is False


def test_active_anti_thrash_recovery_needs_help(monkeypatch):
    actual = _load_unpatched_plugin()
    _install_native_policy_modules(monkeypatch, actual, {
        "compression_fallback_streak": 2,
        "compression_recovery_deadline": 2_000.0,
    })
    monkeypatch.setattr(actual.time, "time", lambda: 1_999.0)
    assert actual._native_compression_needs_help("s-blocked") is True


def test_recovered_anti_thrash_counter_does_not_need_help(monkeypatch):
    actual = _load_unpatched_plugin()
    _install_native_policy_modules(monkeypatch, actual, {
        "compression_fallback_streak": 1,
        "compression_ineffective_count": 1,
        "compression_recovery_deadline": 0.0,
    })
    assert actual._native_compression_needs_help("s-probation") is False


def test_native_probe_failure_stays_observational(monkeypatch):
    actual = _load_unpatched_plugin()
    monkeypatch.delitem(sys.modules, "hermes_cli", raising=False)
    monkeypatch.delitem(sys.modules, "hermes_cli.config", raising=False)
    monkeypatch.setattr(actual, "_import_state", lambda: None, raising=False)
    assert actual._native_compression_needs_help("s-unknown") is False
