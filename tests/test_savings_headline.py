"""savings_headline: the one headline the band, status line and dashboard share.

Covers the ux-contract object (usd_30d = action_savings.total_usd + the
model-routing figure, usd_week always null, tokens_30d from token_headline,
display thresholds and wording), the status-bar/dashboard equality gate, and
the statusline.js savings segment (green, OSC 8 when supported, omitted when
display is null, appended to row 1 in slim mode).

Run: python3 -m pytest tests/test_savings_headline.py -q
"""
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "token-optimizer" / "scripts"
MEASURE = SCRIPTS / "measure.py"
STATUSLINE = SCRIPTS / "statusline.js"
NODE = shutil.which("node")

sys.path.insert(0, str(SCRIPTS))
from _measure import savings_headline  # noqa: E402

URL = "http://localhost:24842/token-optimizer#savings"


def _summary(usd_action=0.0, routing=0.0, tokens=0):
    return {
        "action_savings": {"total_usd": usd_action, "routing_usd": routing},
        "model_routing": {"realized_cost_usd": routing},
        "token_headline": {"total_tokens": tokens},
    }


# --------------------------------------------------------------------------
# Contract object
# --------------------------------------------------------------------------

def test_usd_30d_is_action_total_plus_routing():
    h = savings_headline.build(_summary(usd_action=100.0, routing=7.5, tokens=5))
    assert h["usd_30d"] == pytest.approx(107.5)
    assert h["usd_week"] is None
    assert h["tokens_30d"] == 5


def test_routing_falls_back_to_action_routing_usd():
    s = _summary(usd_action=100.0, routing=7.5)
    s["model_routing"] = {}
    h = savings_headline.build(s)
    assert h["usd_30d"] == pytest.approx(107.5)


def test_missing_inputs_read_as_zero_and_hide_display():
    h = savings_headline.build({})
    assert h["usd_30d"] == 0.0
    assert h["usd_week"] is None
    assert h["tokens_30d"] == 0
    assert h["display"] is None
    assert savings_headline.build(None)["display"] is None
    assert savings_headline.build("junk")["display"] is None


def test_display_30d_at_threshold():
    h = savings_headline.build(_summary(usd_action=60.0, tokens=602_700_000))
    d = h["display"]
    assert d["kind"] == "30d"
    assert d["short"] == "Saved $60/30d · 602.7M kept out"
    assert d["long"] == "$60 saved in 30 days · 602.7M tokens kept out of context"
    assert d["url"] == URL


def test_display_30d_below_threshold_falls_to_tokens():
    h = savings_headline.build(_summary(usd_action=59.99, tokens=1_500_000))
    d = h["display"]
    assert d["kind"] == "tokens"
    assert d["short"] == "1.5M tokens kept out of context"
    assert d["long"] == d["short"]


def test_display_null_below_all_thresholds():
    h = savings_headline.build(_summary(usd_action=59.0, tokens=999_999))
    assert h["display"] is None


def test_display_tokens_threshold_boundary():
    assert savings_headline.build(_summary(tokens=1_000_000))["display"]["kind"] == "tokens"
    assert savings_headline.build(_summary(tokens=999_999))["display"] is None


def test_display_30d_threshold_boundary():
    assert savings_headline.build(_summary(usd_action=60.0))["display"]["kind"] == "30d"
    assert savings_headline.build(_summary(usd_action=59.99, tokens=2_000_000))["display"]["kind"] == "tokens"


def test_usd_week_never_prorates():
    # Even a large 30-day figure must not fabricate a weekly one.
    h = savings_headline.build(_summary(usd_action=9999.0, tokens=50_000_000))
    assert h["usd_week"] is None
    assert h["display"]["kind"] == "30d"


def test_dollar_format_thousands_separator_and_whole_dollars():
    h = savings_headline.build(_summary(usd_action=1234.4, tokens=0))
    assert h["display"]["short"].startswith("Saved $1,234/30d")
    h = savings_headline.build(_summary(usd_action=999.6, tokens=0))
    assert h["display"]["short"].startswith("Saved $1,000/30d")


def test_token_format_one_decimal_k_m_b():
    assert savings_headline._token_text(602_700_000) == "602.7M"
    assert savings_headline._token_text(1_500_000_000) == "1.5B"
    assert savings_headline._token_text(602_700) == "602.7K"
    assert savings_headline._token_text(500) == "500"


def test_30d_short_omits_tokens_when_zero():
    h = savings_headline.build(_summary(usd_action=100.0, tokens=0))
    assert h["display"]["short"] == "Saved $100/30d"
    assert h["display"]["long"] == "$100 saved in 30 days"


def test_negative_and_junk_values_never_raise():
    h = savings_headline.build({
        "action_savings": {"total_usd": "junk", "routing_usd": None},
        "model_routing": {"realized_cost_usd": float("nan")},
        "token_headline": {"total_tokens": "lots"},
    })
    assert h["usd_30d"] == 0.0
    assert h["display"] is None


# --------------------------------------------------------------------------
# Equality gate: status-bar usd_30d == dashboard hero sum, one fixture
# --------------------------------------------------------------------------

SID = "dddddddd-1111-2222-3333-444444444444"


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
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    m = importlib.import_module("measure")
    yield m
    sys.modules.pop("measure", None)


def _seed_usd(sb):
    """One fixture with enough realized savings to clear the 30d threshold."""
    from datetime import datetime, timedelta
    conn = sb._init_trends_db()
    try:
        base = datetime.now() - timedelta(days=3)
        for i, (tok, cost) in enumerate([(4000, 30.0), (6000, 45.0), (2000, 20.0)]):
            ts = (base + timedelta(days=i)).isoformat()
            conn.execute(
                "INSERT INTO savings_events (timestamp, event_type, tokens_saved, "
                "cost_saved_usd, session_id, session_uuid) VALUES (?,?,?,?,?,?)",
                (ts, "tool_archive", tok, cost, SID, SID))
        conn.commit()
    finally:
        conn.close()


def test_status_bar_usd_30d_equals_dashboard_hero_sum(sb):
    _seed_usd(sb)
    sav, reason = sb._status_bar_savings_or_reason(SID)
    assert reason is None and sav is not None
    bar = sav["savings_headline"]
    assert bar is not None and bar["display"] is not None

    dash = sb._dashboard_savings_data(days=30)
    hero = dash["savings_headline"]
    assert hero is not None

    # The contract: action_savings.total_usd + the routing figure the hero adds.
    routing = dash["model_routing"].get("realized_cost_usd")
    if routing is None:
        routing = (dash.get("action_savings") or {}).get("routing_usd", 0.0)
    expected = dash["action_savings"]["total_usd"] + max(0.0, float(routing or 0.0))
    assert bar["usd_30d"] == pytest.approx(expected)
    assert hero["usd_30d"] == pytest.approx(expected)
    assert bar["usd_30d"] == pytest.approx(hero["usd_30d"])
    # Honest weekly figure: never fabricated from the 30-day number.
    assert bar["usd_week"] is None and hero["usd_week"] is None


def test_status_bar_payload_carries_headline_and_keeps_metered_field(sb):
    _seed_usd(sb)
    out = sb.status_bar_payload(SID, sync=True)
    sav = out["savings"]
    assert sav["savings_headline"]["usd_30d"] > 0
    # The metered-only field keeps its old meaning next to the shared headline.
    assert "total_30d_usd" in sav
    assert sav["total_30d_usd"] == pytest.approx(
        sb._get_merged_savings(days=30)["total_cost_usd"])


# --------------------------------------------------------------------------
# statusline.js: savings segment
# --------------------------------------------------------------------------

_ANSI_CSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _vlen(s):
    return len(_ANSI_CSI.sub("", s))


@pytest.fixture()
def sl_env(tmp_path, monkeypatch):
    snap = tmp_path / "snap"
    claude = tmp_path / "claude"
    (snap / "status-bar").mkdir(parents=True)
    claude.mkdir(parents=True)
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(snap))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    monkeypatch.delenv("TOKEN_OPTIMIZER_HYPERLINKS", raising=False)
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.delenv("TOKEN_OPTIMIZER_STATUS_BAR_SIZE", raising=False)
    for var in ("TMUX", "STY", "TERM", "TERM_PROGRAM", "WT_SESSION",
                "KONSOLE_VERSION", "ALACRITTY_WINDOW_ID", "ALACRITTY_LOG",
                "KITTY_WINDOW_ID", "GHOSTTY_RESOURCES_DIR", "VTE_VERSION",
                "CLAUDE_PLUGIN_DATA"):
        monkeypatch.delenv(var, raising=False)
    return snap


def _write_status_bar_cache(snap, session_id, display, computed_at=None):
    import time as _t
    payload = {
        "schema": 1,
        "computed_at": computed_at if computed_at is not None else _t.time(),
        "savings": {"savings_headline": {"display": display}},
        "reason": None,
    }
    (snap / "status-bar" / f"{session_id}.json").write_text(json.dumps(payload))


_SL_PAYLOAD = {
    "model": {"display_name": "Opus 4.8"},
    "workspace": {"current_dir": "/tmp"},
    "session_id": "sl-session-0001",
    "context_window": {"used_percentage": 42},
}


def _run_statusline(payload=None, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    p = subprocess.run(
        [NODE, str(STATUSLINE)],
        input=json.dumps(payload or _SL_PAYLOAD),
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert p.returncode == 0, p.stderr
    return p.stdout.replace("\r\n", "\n")


_DISPLAY = {"kind": "30d", "short": "Saved $237/30d · 602.7M kept out",
            "long": "$237 saved in 30 days", "url": URL}


@pytest.mark.skipif(NODE is None, reason="node unavailable")
def test_statusline_row2_starts_with_green_savings_segment(sl_env):
    _write_status_bar_cache(sl_env, "sl-session-0001", _DISPLAY)
    out = _run_statusline()
    lines = out.split("\n")
    assert len(lines) >= 2
    row2 = lines[1]
    # SEP is " <dim>|<reset> "; strip CSI to see the visible segments.
    visible = _ANSI_CSI.sub("", row2)
    assert visible.startswith("Saved $237/30d · 602.7M kept out ↗")
    first_seg = row2.split("\x1b[38;5;245m|")[0]
    assert "\x1b[32m" in first_seg, "savings segment must be green"
    # Plain-text fallback: no OSC 8 without a capable terminal env.
    assert "\x1b]8;;" not in out


@pytest.mark.skipif(NODE is None, reason="node unavailable")
def test_statusline_osc8_hyperlink_when_supported(sl_env):
    _write_status_bar_cache(sl_env, "sl-session-0001", _DISPLAY)
    out = _run_statusline(env_extra={"TOKEN_OPTIMIZER_HYPERLINKS": "1"})
    assert f"\x1b]8;;{URL}\x07" in out
    assert "\x1b]8;;\x07" in out


@pytest.mark.skipif(NODE is None, reason="node unavailable")
def test_statusline_osc8_suppressed_when_unsupported(sl_env):
    _write_status_bar_cache(sl_env, "sl-session-0001", _DISPLAY)
    out = _run_statusline(env_extra={"TOKEN_OPTIMIZER_HYPERLINKS": "0"})
    assert "\x1b]8;;" not in out
    assert "Saved $237/30d" in out


@pytest.mark.skipif(NODE is None, reason="node unavailable")
def test_statusline_omits_segment_when_display_null(sl_env):
    _write_status_bar_cache(sl_env, "sl-session-0001", None)
    out = _run_statusline()
    assert "Saved" not in out
    assert "kept out" not in out


@pytest.mark.skipif(NODE is None, reason="node unavailable")
def test_statusline_omits_segment_without_cache(sl_env):
    out = _run_statusline()
    assert "Saved $" not in out


@pytest.mark.skipif(NODE is None, reason="node unavailable")
def test_statusline_slim_appends_savings_to_row1(sl_env, monkeypatch):
    _write_status_bar_cache(sl_env, "sl-session-0001", _DISPLAY)
    out = _run_statusline(env_extra={"TOKEN_OPTIMIZER_STATUS_BAR_SIZE": "slim"})
    assert "\n" not in out.strip(), "slim must stay one line"
    assert "Saved $237/30d · 602.7M kept out" in out


@pytest.mark.skipif(NODE is None, reason="node unavailable")
def test_statusline_savings_survives_narrow_wrap(sl_env):
    _write_status_bar_cache(sl_env, "sl-session-0001", _DISPLAY)
    out = _run_statusline(env_extra={"COLUMNS": "40", "TOKEN_OPTIMIZER_HYPERLINKS": "1"})
    lines = out.split("\n")
    # The savings segment is first on logical row 2, so it lands on the first
    # physical row of that group even when the rest wraps.
    joined = "\n".join(lines)
    assert "Saved $237/30d" in joined
    # And the OSC sequence must not corrupt the width accounting: no packed
    # line may exceed the budget by more than one unsplittable segment allows.
    for ln in lines:
        visible = _ANSI_CSI.sub("", ln)
        visible = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", visible)
        visible = re.sub(r"[\x00-\x1f]", "", visible)
        if len(visible) > 40:
            assert " | " not in visible, f"packed line over 40 cols: {ln!r}"


@pytest.mark.skipif(NODE is None, reason="node unavailable")
def test_statusline_plain_labels(sl_env):
    """F12: Quality A 82 / week 87% / 5 hours 34%, no codes."""
    from datetime import datetime, timezone
    _write_status_bar_cache(sl_env, "sl-session-0001", None)
    claude = Path(os.environ["CLAUDE_CONFIG_DIR"])
    qc = claude / "token-optimizer" / f"quality-cache-sl-session-0001.json"
    qc.parent.mkdir(parents=True, exist_ok=True)
    qc.write_text(json.dumps({
        "session_file": "sl-session-0001.jsonl",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "resource_health": 82, "resource_health_grade": "A",
        "session_efficiency": 94, "compactions": 0,
    }))
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(claude)
    p = subprocess.run(
        [NODE, str(STATUSLINE)], input=json.dumps(_SL_PAYLOAD),
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    out = p.stdout
    assert "Quality A 82" in out
    assert "ContextQ" not in out
    assert "Eff:" not in out
    assert "Compacts" not in out
    assert "compacted" not in out
