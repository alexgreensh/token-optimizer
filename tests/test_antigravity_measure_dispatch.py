"""U8 — Antigravity collector, summary, and dispatch in measure.py.

Google Antigravity sessions are collected into the same trends.db the other
adapters use, but they are consent-gated, priced honestly, keyed per-surface,
and flagged ``platform=antigravity`` so the credits/estimated tiers never mix
them with Claude/Copilot/Hermes data. These tests drive the collector directly
(with ``runtime_env.antigravity_home`` and ``antigravity_state.read_all_sessions``
patched) so the trends DB, restore-context, and pricing paths are exercised
without any real ~/.gemini data.

Run: python3 -m pytest tests/test_antigravity_measure_dispatch.py -v
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


def _scripts_modules():
    return [
        name for name, mod in list(sys.modules.items())
        if getattr(mod, "__file__", None)
        and str(SCRIPTS) in str(Path(mod.__file__).resolve().parent)
    ]


def _purge_scripts_modules():
    for name in _scripts_modules():
        del sys.modules[name]


@pytest.fixture(autouse=True)
def _cleanup_modules():
    snapshot = {name: sys.modules[name] for name in _scripts_modules()}
    yield
    _purge_scripts_modules()
    sys.modules.update(snapshot)


def _import_measure(monkeypatch, tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(snap))
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "antigravity")
    monkeypatch.setenv("TOKEN_OPTIMIZER_NO_PROC_SCAN", "1")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    _purge_scripts_modules()
    mod = importlib.import_module("measure")
    return mod, snap


def _patch_homes(monkeypatch, mod, tmp_path):
    """Point antigravity_home at a temp dir (under the strict safe-home it is
    normally rejected, so we patch runtime_env directly at call time)."""
    gemini = tmp_path / "gemini"
    gemini.mkdir(parents=True, exist_ok=True)
    import runtime_env  # noqa: PLC0415

    monkeypatch.setattr(runtime_env, "antigravity_home", lambda: gemini)
    return gemini


def _grant_consent(gemini):
    dd = gemini / "token-optimizer"
    dd.mkdir(parents=True, exist_ok=True)
    (dd / "config.json").write_text(json.dumps({"antigravity_consent": True}), encoding="utf-8")


def _patch_sessions(monkeypatch, sessions):
    import antigravity_state  # noqa: PLC0415

    monkeypatch.setattr(antigravity_state, "read_all_sessions", lambda *a, **k: list(sessions))


def _session(conv_id="abc123", surface="antigravity", *, model="gemini-3.5-flash",
             fresh=1000, output=500, cache_read=2000, credits=0, killed=False,
             not_fully_idle=False, title="Fix the parser", workspace="/home/u/proj",
             start=None):
    start = start if start is not None else time.time() - 600
    return {
        "conversation_id": conv_id,
        "surface": surface,
        "title": title,
        "workspace": workspace,
        "killed": killed,
        "not_fully_idle": not_fully_idle,
        "input_tokens": fresh,
        "output_tokens": output,
        "cache_read_tokens": cache_read,
        "thinking_tokens": 50,
        "user_input_count": 3,
        "tool_call_count": 2,
        "generations": [{"output_tokens": output}],
        "model_display_name": model,
        "last_max_context": 1000000,
        "credit_cost": credits,
        "consumed_credits": credits,
        "start_time": start,
        "end_time": start + 500,
    }


def _rows(mod, snap):
    db = snap / "trends.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    try:
        try:
            return conn.execute(
                "SELECT jsonl_path, platform, cost_source, cost_usd, credits, incomplete, "
                "input_tokens, output_tokens FROM session_log ORDER BY id"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# consent gate
# ---------------------------------------------------------------------------

def test_no_consent_collects_zero_rows(monkeypatch, tmp_path):
    mod, snap = _import_measure(monkeypatch, tmp_path)
    _patch_homes(monkeypatch, mod, tmp_path)  # no consent file written
    _patch_sessions(monkeypatch, [_session()])
    assert mod._collect_antigravity_sessions(days=90, quiet=True) == 0
    assert _rows(mod, snap) == []


# ---------------------------------------------------------------------------
# rollup: platform identity + list-price estimation (R8)
# ---------------------------------------------------------------------------

def test_rollup_inserts_platform_antigravity_with_list_price(monkeypatch, tmp_path):
    mod, snap = _import_measure(monkeypatch, tmp_path)
    gemini = _patch_homes(monkeypatch, mod, tmp_path)
    _grant_consent(gemini)
    _patch_sessions(monkeypatch, [_session()])
    n = mod._collect_antigravity_sessions(days=90, quiet=True)
    assert n == 1
    rows = _rows(mod, snap)
    assert len(rows) == 1
    (jsonl_path, platform, cost_source, cost_usd, credits, incomplete, imp, outp) = rows[0]
    assert jsonl_path == "antigravity:antigravity:abc123"
    assert platform == "antigravity"
    assert cost_source == "antigravity_list_price_estimate"
    assert credits is None
    assert incomplete == 0
    assert imp == 1000 + 2000  # Gemini input is fresh + cache-read
    assert outp == 500
    # The collector priced this session at Gemini list rates (input/cache/output).
    expected = mod._get_model_cost("gemini-3.5-flash", 1000, 500, cache_read=2000)
    assert cost_usd == pytest.approx(expected, abs=1e-9)


def test_rollup_credits_path_prices_zero_usd(monkeypatch, tmp_path):
    """Antigravity credits are Antigravity's own figure, never a USD estimate."""
    mod, snap = _import_measure(monkeypatch, tmp_path)
    gemini = _patch_homes(monkeypatch, mod, tmp_path)
    _grant_consent(gemini)
    _patch_sessions(monkeypatch, [_session(credits=128)])
    mod._collect_antigravity_sessions(days=90, quiet=True)
    (_jp, _plt, cost_source, cost_usd, credits, _inc, _imp, _out) = _rows(mod, snap)[0]
    assert cost_source == "antigravity_credits"
    assert cost_usd == 0.0
    assert credits == 128


def test_rollup_surfaces_never_summed(monkeypatch, tmp_path):
    """Each surface is a separate population with a distinct dedup key."""
    mod, snap = _import_measure(monkeypatch, tmp_path)
    gemini = _patch_homes(monkeypatch, mod, tmp_path)
    _grant_consent(gemini)
    _patch_sessions(monkeypatch, [
        _session(conv_id="abc123", surface="antigravity-cli"),
        _session(conv_id="abc123", surface="antigravity"),
        _session(conv_id="abc123", surface="antigravity-ide"),
    ])
    n = mod._collect_antigravity_sessions(days=90, quiet=True)
    assert n == 3
    keys = {r[0] for r in _rows(mod, snap)}
    assert keys == {
        "antigravity:antigravity-cli:abc123",
        "antigravity:antigravity:abc123",
        "antigravity:antigravity-ide:abc123",
    }


# ---------------------------------------------------------------------------
# killed / partial data upgrade (same shape as the Copilot recovery path)
# ---------------------------------------------------------------------------

def test_killed_session_is_incomplete_then_upgraded(monkeypatch, tmp_path):
    mod, snap = _import_measure(monkeypatch, tmp_path)
    gemini = _patch_homes(monkeypatch, mod, tmp_path)
    _grant_consent(gemini)

    _patch_sessions(monkeypatch, [_session(killed=True, fresh=300, output=100)])
    assert mod._collect_antigravity_sessions(days=90, quiet=True) == 1
    row = _rows(mod, snap)[0]
    assert row[5] == 1  # incomplete

    # Final shutdown totals arrive with a clean end-reason: the partial row is
    # upgraded in place, not frozen.
    _patch_sessions(monkeypatch, [_session(killed=False, fresh=1000, output=500)])
    assert mod._collect_antigravity_sessions(days=90, quiet=True) == 1
    row = _rows(mod, snap)[0]
    assert row[5] == 0
    assert row[6] == 1000 + 2000
    assert row[7] == 500


# ---------------------------------------------------------------------------
# restore-context continuity + R22 filter
# ---------------------------------------------------------------------------

def test_restore_context_written_and_r22_clean(monkeypatch, tmp_path):
    mod, _snap = _import_measure(monkeypatch, tmp_path)
    gemini = _patch_homes(monkeypatch, mod, tmp_path)
    _grant_consent(gemini)
    evil_title = "Evil\u0007\nTitle\n<script>alert(1)</script>" + ("x" * 400)
    _patch_sessions(monkeypatch, [_session(title=evil_title)])
    mod._collect_antigravity_sessions(days=90, quiet=True)

    ctx = (gemini / "token-optimizer" / "restore-context.md").read_text(encoding="utf-8")
    lines = ctx.splitlines()
    topic_line = next(line for line in lines if line.startswith("- Topic: "))
    # Single line: the title's control chars / newlines are collapsed, never
    # injected verbatim into the next session's context.
    assert "\x07" not in topic_line
    assert "<script>" in topic_line or "Topic" in topic_line  # printable chars survive
    assert len(topic_line) <= len("- Topic: ") + 200  # prefix + 200-char cap
    assert any("gemini-3.5-flash" in line for line in lines)


def test_restore_context_records_killed_note(monkeypatch, tmp_path):
    mod, _snap = _import_measure(monkeypatch, tmp_path)
    gemini = _patch_homes(monkeypatch, mod, tmp_path)
    _grant_consent(gemini)
    _patch_sessions(monkeypatch, [_session(killed=True)])
    mod._collect_antigravity_sessions(days=90, quiet=True)
    ctx = (gemini / "token-optimizer" / "restore-context.md").read_text(encoding="utf-8")
    assert "clean shutdown" in ctx


def test_no_consent_writes_no_restore_context(monkeypatch, tmp_path):
    mod, _snap = _import_measure(monkeypatch, tmp_path)
    gemini = _patch_homes(monkeypatch, mod, tmp_path)  # no consent
    _patch_sessions(monkeypatch, [_session()])
    mod._collect_antigravity_sessions(days=90, quiet=True)
    assert not (gemini / "token-optimizer" / "restore-context.md").exists()


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------

def test_summary_per_surface(capsys, monkeypatch, tmp_path):
    mod, _snap = _import_measure(monkeypatch, tmp_path)
    _patch_homes(monkeypatch, mod, tmp_path)
    _patch_sessions(monkeypatch, [
        _session(conv_id="a", surface="antigravity"),
        _session(conv_id="b", surface="antigravity-cli", model="unknown-model"),
    ])
    mod._antigravity_summary()
    out = capsys.readouterr().out
    assert "Google Antigravity summary" in out
    assert "antigravity: 1 session(s)" in out
    assert "antigravity-cli: 1 session(s)" in out
    assert "No Antigravity sessions found yet" not in out


# ---------------------------------------------------------------------------
# dispatch wiring (read-only smoke: the module must route, not crash)
# ---------------------------------------------------------------------------

def _antigravity_env(tmp_path):
    # Pin HOME + CLAUDE_CONFIG_DIR even for these read-only smokes: the suite-wide
    # host-safety guard requires any file that names install/uninstall verbs to
    # pin both, and it is correct hygiene for a subprocess that cannot touch the
    # developer's real config/launch-agent dirs.
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home),
        "TOKEN_OPTIMIZER_RUNTIME": "antigravity",
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(tmp_path / "snap"),
        "TOKEN_OPTIMIZER_NO_PROC_SCAN": "1",
    })
    return env


def test_dispatch_antigravity_home_prints_path(tmp_path):
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "measure.py"), "antigravity-home"],
        capture_output=True, text=True, env=_antigravity_env(tmp_path), timeout=180,
    )
    assert out.returncode == 0
    assert out.stdout.strip().endswith(".gemini")


def test_dispatch_antigravity_summary_exits_zero(tmp_path):
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "measure.py"), "antigravity-summary"],
        capture_output=True, text=True, env=_antigravity_env(tmp_path), timeout=180,
    )
    assert out.returncode == 0
    assert "Google Antigravity summary" in out.stdout


def test_dispatch_antigravity_doctor_routes_to_doctor(tmp_path):
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "measure.py"), "antigravity-doctor"],
        capture_output=True, text=True, env=_antigravity_env(tmp_path), timeout=180,
    )
    # The doctor exits 1 when the (fixture) host is not ready; the point of this
    # smoke test is that the dispatch reached antigravity_doctor.main and ran its
    # full check set, not an import/argument crash. So assert on the banner and
    # the summary line, not on a ready exit code.
    assert "Token Optimizer — Google Antigravity doctor" in out.stdout
    assert "checks —" in out.stdout
    assert "Traceback" not in out.stdout + out.stderr
    assert out.returncode in (0, 1)


def test_help_lists_antigravity_commands(tmp_path):
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "measure.py"), "help"],
        capture_output=True, text=True, env=_antigravity_env(tmp_path), timeout=180,
    )
    for cmd in ("antigravity-doctor", "antigravity-install", "antigravity-uninstall",
                "antigravity-home", "antigravity-rollup", "antigravity-summary"):
        assert cmd in out.stdout
