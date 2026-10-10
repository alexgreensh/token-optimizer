"""`status-bar --json` compactWindow field + native prompt_cache bridging.

Item 7: the band needs the resolver's answer for THIS session's model so it
can honor /autocompact (modelSettings) and autoCompactWindow, not just the env
var. ``tokens`` is null when no override is known (the band then uses the
host's own reported window). Source always names the provenance.

Item 5: statusline.js mirrors Claude Code v2.1.251+'s native prompt_cache
object to prompt-cache-<sid>.json; the payload prefers it over the
transcript-derived lifetime for cache_lifetime / cache_expires_at /
cache_warm, falling back to the transcript on older hosts or a stale mirror.

Run: python3 -m pytest tests/test_status_bar_compact_window.py -q
"""
import importlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "token-optimizer" / "scripts"

SID = "cccccccc-1111-2222-3333-444444444444"


@pytest.fixture()
def sb(tmp_path, monkeypatch):
    snap = tmp_path / "snap"
    home = tmp_path / "home"
    claude = home / ".claude"
    for d in (snap, claude / "projects" / "-proj", claude / "token-optimizer"):
        d.mkdir(parents=True, exist_ok=True)
    for k, v in {
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(snap),
        "CLAUDE_CONFIG_DIR": str(claude),
        "HOME": str(home),
        "USERPROFILE": str(home),
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    for k in ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
              "TOKEN_OPTIMIZER_CONTEXT_SIZE", "CLAUDE_CODE_DISABLE_1M_CONTEXT",
              "CLAUDE_MODEL", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(k, raising=False)
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    m = importlib.import_module("measure")
    m._sb_claude = claude
    yield m
    sys.modules.pop("measure", None)


def _assistant(ts, usage, model="claude-sonnet-5"):
    return json.dumps({"type": "assistant", "timestamp": ts,
                       "message": {"model": model, "usage": usage}})


def _write_transcript(sb, lines, model="claude-sonnet-5"):
    p = sb._sb_claude / "projects" / "-proj" / f"{SID}.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _write_settings(sb, data):
    p = sb._sb_claude / "settings.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _write_prompt_cache(sb, sid=SID, **fields):
    d = dict(fields)
    d.setdefault("timestamp", time.time() * 1000)
    p = sb._sb_claude / "token-optimizer" / f"prompt-cache-{sid}.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    return p


def _seed_transcript(sb):
    _write_transcript(sb, [
        _assistant("2026-10-03T10:00:00Z",
                   {"cache_creation": {"ephemeral_5m_input_tokens": 900}}),
    ])


# ---------------------------------------------------------------------------
# compactWindow
# ---------------------------------------------------------------------------

def test_compact_window_null_when_no_override(sb):
    _seed_transcript(sb)
    _write_settings(sb, {})
    out = sb.status_bar_payload(SID)
    cw = out["compactWindow"]
    assert cw["tokens"] is None
    assert "default" in cw["source"]


def test_compact_window_env_override(sb, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "400000")
    _seed_transcript(sb)
    _write_settings(sb, {})
    cw = sb.status_bar_payload(SID)["compactWindow"]
    assert cw["tokens"] == 400000
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" in cw["source"]


def test_compact_window_model_settings_for_session_model(sb):
    """The transcript's model picks the modelSettings key, canonicalized."""
    _write_transcript(sb, [
        _assistant("2026-10-03T10:00:00Z", {"input_tokens": 5},
                   model="claude-sonnet-5-20261001"),
    ])
    _write_settings(sb, {
        "modelSettings": {"claude-sonnet-5": {"autoCompactWindow": 500000}},
        "autoCompactWindow": 700000,
    })
    cw = sb.status_bar_payload(SID)["compactWindow"]
    assert cw["tokens"] == 500000
    assert "modelSettings" in cw["source"]


def test_compact_window_top_level_setting(sb):
    _seed_transcript(sb)
    _write_settings(sb, {"autoCompactWindow": 650000})
    cw = sb.status_bar_payload(SID)["compactWindow"]
    assert cw["tokens"] == 650000
    assert "autoCompactWindow" in cw["source"]


def test_compact_window_env_beats_settings(sb, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "300000")
    _seed_transcript(sb)
    _write_settings(sb, {
        "modelSettings": {"claude-sonnet-5": {"autoCompactWindow": 500000}},
        "autoCompactWindow": 700000,
    })
    cw = sb.status_bar_payload(SID)["compactWindow"]
    assert cw["tokens"] == 300000


def test_compact_window_pct_override_counts_as_override(sb, monkeypatch):
    """The pct override alone still shrinks the window: tokens is an int."""
    monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "50")
    _seed_transcript(sb)
    _write_settings(sb, {})
    cw = sb.status_bar_payload(SID)["compactWindow"]
    assert cw["tokens"] == 967000 * 50 // 100
    assert "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" in cw["source"]


def test_compact_window_capped_at_model_window(sb, monkeypatch):
    """A 200K model caps a 1M explicit setting."""
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "1000000")
    _write_transcript(sb, [
        _assistant("2026-10-03T10:00:00Z", {"input_tokens": 5},
                   model="claude-opus-4-6"),
    ])
    _write_settings(sb, {})
    cw = sb.status_bar_payload(SID)["compactWindow"]
    assert cw["tokens"] == 200000


def test_compact_window_never_raises(sb):
    """status-bar output is always a dict with tokens + source."""
    out = sb.status_bar_payload("unknown")
    assert out["compactWindow"] == {"tokens": None, "source": "unresolved"}


# ---------------------------------------------------------------------------
# Native prompt_cache bridging
# ---------------------------------------------------------------------------

def test_prompt_cache_sidecar_beats_transcript(sb):
    _seed_transcript(sb)
    _write_settings(sb, {})
    exp = int(time.time()) + 3600
    _write_prompt_cache(sb, ttl="1h", expires_at=exp, warm=True)
    out = sb.status_bar_payload(SID)
    assert out["cache_lifetime"] == "1h"
    assert out["cache_expires_at"] == exp
    assert out["cache_warm"] is True
    assert out["cache_source"] == "prompt_cache"


def test_prompt_cache_stale_sidecar_falls_back_to_transcript(sb):
    _seed_transcript(sb)
    _write_settings(sb, {})
    p = _write_prompt_cache(sb, ttl="1h", expires_at=int(time.time()) + 3600,
                            warm=True, timestamp=(time.time() - 3600) * 1000)
    out = sb.status_bar_payload(SID)
    assert out["cache_lifetime"] == "5m"
    assert out["cache_source"] == "transcript"
    assert out["cache_expires_at"] is None
    assert out["cache_warm"] is None


def test_prompt_cache_absent_uses_transcript(sb):
    _seed_transcript(sb)
    _write_settings(sb, {})
    out = sb.status_bar_payload(SID)
    assert out["cache_lifetime"] == "5m"
    assert out["cache_source"] == "transcript"


def test_prompt_cache_malformed_sidecar_falls_back(sb):
    _seed_transcript(sb)
    _write_settings(sb, {})
    (sb._sb_claude / "token-optimizer" / f"prompt-cache-{SID}.json").write_text(
        "not json{", encoding="utf-8")
    out = sb.status_bar_payload(SID)
    assert out["cache_lifetime"] == "5m"
    assert out["cache_source"] == "transcript"


def test_no_transcript_no_sidecar_nulls(sb):
    _write_settings(sb, {})
    out = sb.status_bar_payload(SID)
    assert out["cache_lifetime"] is None
    assert out["cache_source"] is None
    # Model unknown: resolver still answers (env > settings > fallback default).
    assert out["compactWindow"]["tokens"] is None
    assert "default" in out["compactWindow"]["source"]
