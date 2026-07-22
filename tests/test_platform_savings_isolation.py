"""Cross-platform honesty of the estimated-transformation pools and runway card.

The estimated tier (headline before/after transformation, sidechain pool,
short-session pool, subscription runway) was designed and validated against
Claude Code data. These tests pin what each lever does on the other runtimes
(Codex, GitHub Copilot, Hermes, OpenCode): a figure that cannot be computed
honestly on a platform must render nothing there. Concretely:

  1. The sidechain (subagent) pool reads ~/.claude/projects transcripts, a
     Claude Code artifact. On any other runtime it must return an honest zero
     WITHOUT scanning Claude's transcript tree -- otherwise a coexisting
     Claude Code install's delegated spend is priced into another product's
     savings headline.
  2. The short-session pool's counterfactual arm reprices token bundles at a
     pre-TO model mix. When no genuinely measured pre-TO mix exists (every
     non-Anthropic runtime without a frozen Opus-era baseline, and Claude
     installs still in mix-tracking mode), the pool must not fabricate an
     opus/sonnet counterfactual -- its delta must be zero.
  3. The subscription runway card reads a rate-limits meter that only the
     Claude Code statusline writes. Its path must be runtime-scoped so a
     foreign runtime never reads Claude's meter, and the card renders nothing
     when the meter is absent.
  4. GitHub Copilot meters premium requests, not tokens; the `savings` /
     `dashboard` commands (the estimated tier's only surfaces) must stay
     dispatch-blocked there, printing the runtime notice instead of numbers.

Run: python3 -m pytest tests/test_platform_savings_isolation.py -v
"""
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"

OPUS = "claude-opus-4-7"
GPT = "gpt-5"

FOREIGN_RUNTIMES = ("codex", "hermes", "copilot", "opencode")

# Frozen typical session with NO measured Opus-era mix (opus_share 0): the shape a
# non-Anthropic runtime's own history produces, and the mix-tracking shape on Claude.
BASELINE_NO_OPUS_ERA = {
    "version": 4,
    "typical_session": {
        "fresh_input": 30000.0, "cache_write": 487000.0,
        "cache_read": 13500000.0, "output": 51000.0,
    },
    "opus_share": 0.0,
    "opus_share_source": None,
    "model_shares": {},
    "window": {"start": "2026-03-18", "end": "2026-04-17",
               "sessions_used": 750, "sessions_total": 750, "elapsed": True},
    "method": "winsorized_mean", "winsor_pct": 0.99,
    "structural_overhead_tokens": 25288,
    "captured_at": "2026-04-17T01:21:13", "source": "frozen_from_history",
}

_SCHEMA = """
CREATE TABLE session_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jsonl_path TEXT UNIQUE, date TEXT NOT NULL, project TEXT,
    duration_minutes REAL, input_tokens INTEGER, output_tokens INTEGER,
    message_count INTEGER, api_calls INTEGER, cache_hit_rate REAL,
    cache_create_1h_tokens INTEGER DEFAULT 0, cache_create_5m_tokens INTEGER DEFAULT 0,
    cache_ttl_scanned INTEGER DEFAULT 0, avg_call_gap_seconds REAL,
    max_call_gap_seconds REAL, p95_call_gap_seconds REAL, skills_json TEXT,
    subagents_json TEXT, tool_calls_json TEXT, model_usage_json TEXT,
    all_model_usage_json TEXT, model_usage_breakdown_json TEXT, version TEXT,
    slug TEXT, topic TEXT, collected_at TEXT, quality_score REAL, quality_grade TEXT,
    stale_waste_tokens INTEGER DEFAULT 0, session_uuid TEXT, is_sidechain INTEGER DEFAULT 0
);
"""

_BASE_HIT = 13500000.0 / (13500000.0 + 30000.0)


def _scripts_modules():
    """Names of currently imported modules that live in the scripts dir."""
    names = []
    for name, mod in list(sys.modules.items()):
        mod_file = getattr(mod, "__file__", None)
        if mod_file and str(SCRIPTS) in str(Path(mod_file).resolve().parent):
            names.append(name)
    return names


def _purge_scripts_modules():
    """Drop every module imported from the scripts dir so env is re-read on import.

    runtime_env caches detect_runtime() with lru_cache and measure.py resolves
    CLAUDE_DIR / RUNTIME_DIR / QUALITY_CACHE_DIR at import time, so a stale module
    would pin the previous test's runtime.
    """
    for name in _scripts_modules():
        del sys.modules[name]


def _import_measure(monkeypatch, tmp_path, runtime):
    snap = tmp_path / "snap"
    snap.mkdir(parents=True, exist_ok=True)
    claude_dir = tmp_path / "claude-home"
    claude_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(snap))
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", runtime)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setenv("TOKEN_OPTIMIZER_NO_PROC_SCAN", "1")
    monkeypatch.delenv("TOKEN_OPTIMIZER_PRETOOL_OPUS", raising=False)
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    _purge_scripts_modules()
    mod = importlib.import_module("measure")
    return mod, snap, claude_dir


@pytest.fixture(autouse=True)
def _cleanup_modules():
    """Snapshot + restore the scripts modules around each test.

    Other test files hold module-level references (e.g. `import runtime_env`)
    captured at collection time. A bare purge would strand those references on
    orphaned module objects while later `import measure` calls build fresh ones,
    so the pre-test module objects are restored, not merely evicted.
    """
    snapshot = {name: sys.modules[name] for name in _scripts_modules()}
    yield
    _purge_scripts_modules()
    sys.modules.update(snapshot)


def _write_sidechain_transcript(claude_dir, n_calls=3, input_tokens=400000):
    """A genuine Claude Code sidechain transcript under <claude>/projects/."""
    proj = claude_dir / "projects" / "-Users-someone-proj"
    proj.mkdir(parents=True, exist_ok=True)
    records = []
    for i in range(n_calls):
        records.append({
            "type": "assistant",
            "isSidechain": True,
            "timestamp": f"2026-07-22T10:0{i}:00Z",
            "requestId": f"req_{i}",
            "message": {
                "model": OPUS,
                "content": [],
                "usage": {"input_tokens": input_tokens, "output_tokens": 9000,
                          "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 0},
            },
        })
    path = proj / "abc123.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    now = time.time()
    os.utime(path, (now, now))
    return path


def _seed_db(snap, *, model, n_quality=12, n_short=6, quality_input=2_000_000,
             short_input=800, baseline=BASELINE_NO_OPUS_ERA):
    (snap / "baseline_state.json").write_text(json.dumps(baseline))
    db = snap / "trends.db"
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    today = datetime.now().strftime("%Y-%m-%d")
    for i in range(n_quality):
        conn.execute(
            "INSERT INTO session_log (jsonl_path, date, input_tokens, output_tokens, "
            "cache_hit_rate, cache_create_5m_tokens, all_model_usage_json, "
            "is_sidechain, duration_minutes) VALUES (?,?,?,?,?,?,?,0,10.0)",
            (f"/q/{i}.jsonl", today, quality_input, int(quality_input * 0.01),
             _BASE_HIT, 0,
             json.dumps({model: quality_input})),
        )
    for i in range(n_short):
        breakdown = {model: {"fresh_input": short_input, "cache_read": 0,
                             "cache_create": 0, "cache_create_1h": 0,
                             "cache_create_5m": 0, "output": 300}}
        conn.execute(
            "INSERT INTO session_log (jsonl_path, date, input_tokens, output_tokens, "
            "cache_hit_rate, model_usage_breakdown_json, is_sidechain, "
            "duration_minutes) VALUES (?,?,?,?,?,?,0,0.2)",
            (f"/short/{i}.jsonl", today, short_input, 300, 0.0,
             json.dumps(breakdown)),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 1. Sidechain pool: Claude-only data source, honest zero everywhere else.
# ---------------------------------------------------------------------------

def test_sidechain_pool_prices_claude_transcripts_on_claude(monkeypatch, tmp_path):
    """Control: the fixture transcript is real -- the Claude runtime prices it."""
    mod, _snap, claude_dir = _import_measure(monkeypatch, tmp_path, "claude")
    _write_sidechain_transcript(claude_dir)
    pool = mod._subagent_pool_savings(days=30, baseline_opus_share=0.95,
                                      tier="anthropic", fresh=True)
    assert pool["sessions"] == 1
    assert pool["actual_usd"] > 0


@pytest.mark.parametrize("runtime", FOREIGN_RUNTIMES)
def test_sidechain_pool_silent_off_claude(monkeypatch, tmp_path, runtime):
    """A foreign runtime must never price ~/.claude sidechain transcripts.

    The transcript here is identical to the control above; only the runtime
    changes. Anything nonzero means a coexisting Claude Code install's
    delegated spend leaked into this runtime's savings headline.
    """
    mod, _snap, claude_dir = _import_measure(monkeypatch, tmp_path, runtime)
    _write_sidechain_transcript(claude_dir)
    pool = mod._subagent_pool_savings(days=30, baseline_opus_share=0.0,
                                      tier="anthropic", fresh=True)
    assert pool["sessions"] == 0
    assert pool["actual_usd"] == 0.0
    assert pool["counterfactual_usd"] == 0.0
    assert pool["transformation_usd"] == 0.0
    assert pool["premium_delegation_usd"] == 0.0


# ---------------------------------------------------------------------------
# 2. Short-session pool: no fabricated Anthropic counterfactual off-baseline.
# ---------------------------------------------------------------------------

def test_short_pool_no_anthropic_counterfactual_on_codex(monkeypatch, tmp_path):
    """Codex bills OpenAI models. With no measured Opus-era baseline the short
    pool has no honest counterfactual mix, so its delta must be zero -- not
    'your gpt-5 one-shots repriced at Sonnet rates', which manufactures a
    savings claim out of a mix the user never ran."""
    mod, snap, _claude = _import_measure(monkeypatch, tmp_path, "codex")
    _seed_db(snap, model=GPT)
    res = mod._estimate_before_after_savings(days=30)
    # The pool still sees the sessions and their real spend...
    assert res["short_session_count"] > 0
    assert res["short_session_actual_usd"] > 0
    # ...but claims no transformation on them.
    assert res["short_session_counterfactual_usd"] == pytest.approx(
        res["short_session_actual_usd"], abs=0.01)
    assert res["short_session_transformation_usd"] == pytest.approx(0.0, abs=0.01)
    # And the fabricated delta must not leak into the headline.
    assert float(res.get("monthly_savings_usd", 0.0)) == pytest.approx(0.0, abs=0.01)


def test_short_pool_tracking_mode_claude_no_fabricated_mix(monkeypatch, tmp_path):
    """Claude install still in mix-tracking mode (no frozen Opus share, no
    consent): the main pool's routing lever is zero by definition, so the short
    pool must not price a counterfactual mix nobody measured."""
    mod, snap, _claude = _import_measure(monkeypatch, tmp_path, "claude")
    _seed_db(snap, model=OPUS)
    res = mod._estimate_before_after_savings(days=30)
    assert res["short_session_transformation_usd"] == pytest.approx(0.0, abs=0.01)


def test_short_pool_keeps_real_baseline_behavior(monkeypatch, tmp_path):
    """Guard: with a genuinely frozen Opus-era baseline the short pool still
    prices its routing delta (cheap short sessions vs the 95% Opus era)."""
    mod, snap, _claude = _import_measure(monkeypatch, tmp_path, "claude")
    frozen = dict(BASELINE_NO_OPUS_ERA)
    frozen.update({"opus_share": 0.95, "opus_share_source": "pretool_baseline",
                   "model_shares": {"opus": 0.95, "sonnet": 0.05}})
    _seed_db(snap, model="claude-haiku-4-5", n_short=6, short_input=900,
             baseline=frozen)
    res = mod._estimate_before_after_savings(days=30)
    assert res["short_session_count"] > 0
    # Haiku one-shots vs a 95% Opus era: the counterfactual must exceed actual.
    assert (res["short_session_counterfactual_usd"]
            > res["short_session_actual_usd"])


# ---------------------------------------------------------------------------
# 3. Runway card: meter path is runtime-scoped; silence when absent.
# ---------------------------------------------------------------------------

def test_runway_meter_path_is_runtime_scoped_and_silent(monkeypatch, tmp_path):
    home = tmp_path / "home"
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    mod, _snap, claude_dir = _import_measure(monkeypatch, tmp_path, "codex")
    # A live Claude meter exists on the same machine...
    meter_dir = claude_dir / "token-optimizer"
    meter_dir.mkdir(parents=True, exist_ok=True)
    (meter_dir / "rate-limits.json").write_text(json.dumps({
        "five_hour": {"used_percentage": 62.0},
        "seven_day": {"used_percentage": 41.0},
        "timestamp": time.time() * 1000,
    }))
    # ...but the Codex runtime's meter path must live under the Codex home,
    # and with no meter there the runway card must render nothing.
    assert str(mod._keepwarm_rate_limits_path()).startswith(str(codex_home))
    assert mod.runway_snapshot(days=30) is None


# ---------------------------------------------------------------------------
# 4. Copilot (premium-request billing): estimated tier stays dispatch-blocked.
# ---------------------------------------------------------------------------

def test_estimated_tier_surfaces_blocked_for_foreign_runtimes(monkeypatch, tmp_path):
    mod, _snap, _claude = _import_measure(monkeypatch, tmp_path, "claude")
    for cmd in ("savings", "dashboard", "trends", "report", "validate-impact"):
        assert cmd in mod._CLAUDE_TARGET_CMDS
    assert {"copilot", "opencode", "hermes"} <= set(mod._FOREIGN_RUNTIMES)
    # Copilot and OpenCode exempt nothing; Hermes exempts only its own
    # runtime-aware dashboard.
    assert not mod._FOREIGN_RUNTIME_EXEMPTIONS.get("copilot")
    assert not mod._FOREIGN_RUNTIME_EXEMPTIONS.get("opencode")
    assert mod._FOREIGN_RUNTIME_EXEMPTIONS.get("hermes") == frozenset({"dashboard"})


def test_copilot_transformation_renders_nothing_even_with_data(monkeypatch, tmp_path):
    """Dashboards can be generated internally under Copilot (install-time seed,
    session-end flush), so the dispatch block alone is not enough: the
    transformation itself must refuse. A DB that yields numbers on other
    runtimes must yield the zero payload with an explicit reason on Copilot,
    because premium-request billing has no token-dollar counterfactual."""
    mod, snap, _claude = _import_measure(monkeypatch, tmp_path, "copilot")
    frozen = dict(BASELINE_NO_OPUS_ERA)
    frozen.update({"opus_share": 0.95, "opus_share_source": "pretool_baseline"})
    _seed_db(snap, model="claude-sonnet-4-6", baseline=frozen)
    res = mod._estimate_before_after_savings(days=30)
    assert res["reason"] == "unsupported_billing"
    assert res["monthly_savings_usd"] == 0.0
    assert res["counterfactual_monthly_usd"] == 0.0
    assert res["actual_monthly_usd"] == 0.0
    assert res["short_session_actual_usd"] == 0.0
    assert res["breakdown"] == []


def test_copilot_savings_command_prints_notice_not_numbers(tmp_path):
    """`measure.py savings` under Copilot must print the runtime notice and no
    token-dollar figures: Copilot bills premium requests, so a token-savings
    number is not claimable there."""
    env = os.environ.copy()
    env.update({
        "TOKEN_OPTIMIZER_RUNTIME": "copilot",
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(tmp_path / "snap"),
        "TOKEN_OPTIMIZER_NO_PROC_SCAN": "1",
    })
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "measure.py"), "savings", "--json"],
        capture_output=True, text=True, env=env, timeout=180)
    assert out.returncode == 0
    assert "Copilot runtime detected" in out.stdout
    assert "monthly_savings_usd" not in out.stdout
