#!/usr/bin/env python3
"""Tests for _measure/routing_default.py (T3: routing on by default).

Covers the spec's required cases:
  * fresh install injects the MODEL_ROUTING block once and stamps the date
  * a second session is a no-op (unchanged block is never rewritten)
  * TOKEN_OPTIMIZER_ROUTING=0 skips injection
  * inject-routing --uninstall removes the block and records routing_removed_at
  * block bytes are identical on two different days (cache stability)
  * the audit offer shows once and never when setup trims are recorded
  * Codex gets AGENTS.md, Claude Code gets the global CLAUDE.md

Every test runs against tmp dirs only: TOKEN_OPTIMIZER_SNAPSHOT_DIR,
CLAUDE_CONFIG_DIR / CODEX_HOME, and TOKEN_OPTIMIZER_RUNTIME are all pointed
at tmp_path before measure.py is imported (SNAPSHOT_DIR freezes at import).
PYTEST_CURRENT_TEST is removed so session_start() does not take its
pytest no-op guard; nothing here touches the developer's real home.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

MARKER_OPEN = "<!-- TOKEN_OPTIMIZER:MODEL_ROUTING -->"


def _load(tmp_path, monkeypatch, runtime="claude"):
    """Import measure.py + _measure.routing_default against tmp dirs."""
    snap = tmp_path / "data"
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(snap))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", runtime)
    monkeypatch.setenv("HOME", str(home))
    # session_start() no-ops under pytest to protect real user dirs; this
    # suite isolates every target, so lift the guard for the call.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    sys.modules.pop("measure", None)
    measure = importlib.import_module("measure")
    from _measure import routing_default
    return measure, routing_default, snap, home


def _instruction_file(home, runtime="claude"):
    if runtime == "codex":
        return home / ".codex" / "AGENTS.md"
    return home / ".claude" / "CLAUDE.md"


def _block_bytes(path):
    return path.read_bytes() if path.exists() else None


# ------------------------------------------------------------------ inject


def test_fresh_install_injects_once_and_stamps_date(tmp_path, monkeypatch):
    _m, rd, snap, home = _load(tmp_path, monkeypatch)
    target = _instruction_file(home)

    notices = rd.session_start()

    assert target.is_file(), "MODEL_ROUTING block was not injected"
    text = target.read_text(encoding="utf-8")
    assert MARKER_OPEN in text
    assert "Model & Effort Routing" in text
    assert "Your last 30 days" not in text, "auto block must drop the stats line"
    injected = (snap / "routing_injected_at").read_text(encoding="utf-8").strip()
    assert injected == rd._utc_today()
    assert rd.routing_enabled_since() == injected
    assert any("routing" in n.lower() for n in notices)
    assert any("/token-optimizer" in n for n in notices), "audit offer expected"


def test_second_session_is_a_noop(tmp_path, monkeypatch):
    _m, rd, snap, home = _load(tmp_path, monkeypatch)
    target = _instruction_file(home)

    rd.session_start()
    first_bytes = _block_bytes(target)
    first_stamp = (snap / "routing_injected_at").read_text(encoding="utf-8")

    notices = rd.session_start()

    assert notices == [], "second session must not re-notify"
    assert _block_bytes(target) == first_bytes, "unchanged block must not be rewritten"
    assert (snap / "routing_injected_at").read_text(encoding="utf-8") == first_stamp


def test_block_bytes_identical_on_two_different_days(tmp_path, monkeypatch):
    _m, rd, _snap, home = _load(tmp_path, monkeypatch)
    target = _instruction_file(home)

    monkeypatch.setattr(rd, "_utc_today", lambda: "2026-01-05")
    rd.session_start()
    day_one = _block_bytes(target)
    assert b"updated 2026-01-05T00:00" in day_one

    monkeypatch.setattr(rd, "_utc_today", lambda: "2026-03-20")
    rd.session_start()
    day_two = _block_bytes(target)

    assert day_two == day_one, "auto-injected block must be byte-stable across days"


def test_opt_out_env_skips_injection(tmp_path, monkeypatch):
    _m, rd, snap, home = _load(tmp_path, monkeypatch)
    target = _instruction_file(home)

    notices = rd.session_start(environ={"TOKEN_OPTIMIZER_ROUTING": "0"})

    assert not target.exists(), "opt-out must skip injection entirely"
    assert not (snap / "routing_injected_at").exists()
    assert rd.routing_enabled_since() is None
    # The audit offer is a separate feature and still shows once.
    assert any("/token-optimizer" in n for n in notices)


def test_codex_injects_into_global_agents_md(tmp_path, monkeypatch):
    _m, rd, snap, home = _load(tmp_path, monkeypatch, runtime="codex")

    rd.session_start()

    target = home / ".codex" / "AGENTS.md"
    assert target.is_file()
    assert MARKER_OPEN in target.read_text(encoding="utf-8")
    assert not (home / ".claude" / "CLAUDE.md").exists()
    assert (snap / "routing_injected_at").exists()


# ----------------------------------------------------------------- uninstall


def test_uninstall_removes_block_and_records_removal(tmp_path, monkeypatch):
    _m, rd, snap, home = _load(tmp_path, monkeypatch)
    target = _instruction_file(home)

    rd.session_start()
    injected = rd.routing_enabled_since()
    assert injected is not None

    result = rd.uninstall_routing()

    assert str(target) in result["files"]
    assert MARKER_OPEN not in target.read_text(encoding="utf-8")
    removed = rd.routing_removed_at()
    assert removed == rd._utc_today()
    assert rd.routing_disabled_at() == removed
    assert rd.routing_credit_window() == (injected, removed)

    # SessionStart must not re-add the block after an explicit uninstall.
    rd.session_start()
    assert MARKER_OPEN not in target.read_text(encoding="utf-8")


def test_uninstall_then_manual_inject_reenables(tmp_path, monkeypatch):
    _m, rd, snap, home = _load(tmp_path, monkeypatch)
    target = _instruction_file(home)

    rd.session_start()
    rd.uninstall_routing()
    assert rd.routing_removed_at() is not None

    rd.record_injection()

    assert rd.routing_removed_at() is None
    assert rd.routing_enabled_since() == rd._utc_today(), (
        "re-enable after uninstall must restart the credit window"
    )


def test_cli_inject_routing_uninstall_end_to_end(tmp_path, monkeypatch):
    """`measure.py inject-routing --uninstall` removes the block and stamps
    routing_removed_at through the real CLI dispatch."""
    measure, rd, snap, home = _load(tmp_path, monkeypatch)
    target = _instruction_file(home)
    rd.session_start()
    assert MARKER_OPEN in target.read_text(encoding="utf-8")

    from _measure import cli as cli_mod
    # main() re-execs Python in UTF-8 mode on a non-UTF-8 locale (Windows
    # cp1252 runners); inside pytest that replaces the test process itself.
    monkeypatch.setattr(measure, "reexec_in_utf8_mode", lambda: None)
    monkeypatch.setattr(sys, "argv", ["measure.py", "inject-routing", "--uninstall"])
    cli_mod.main()

    assert MARKER_OPEN not in target.read_text(encoding="utf-8")
    assert rd.routing_removed_at() == rd._utc_today()


# -------------------------------------------------------------- audit offer


def test_audit_offer_shows_once(tmp_path, monkeypatch):
    _m, rd, snap, home = _load(tmp_path, monkeypatch)

    first = rd.session_start()
    second = rd.session_start()

    assert any("/token-optimizer" in n for n in first)
    assert not any("/token-optimizer" in n for n in second)
    assert (snap / "once-audit-offer.json").exists()


def test_audit_offer_never_shows_when_trims_exist(tmp_path, monkeypatch):
    _m, rd, snap, home = _load(tmp_path, monkeypatch)

    from _measure import structural_ledger
    trim = structural_ledger.make_trim(
        "agents", "standup-notes", "global", 500, 200,
        "2026-01-01T00:00:00", "audit_action")
    assert structural_ledger.record_trim(snap, trim)

    notices = rd.session_start()

    assert not any("/token-optimizer" in n for n in notices)
    # Marker is still latched so the offer can never appear later either.
    assert (snap / "once-audit-offer.json").exists()
    rd.session_start()
    assert not any("/token-optimizer" in n for n in rd.session_start())


# ------------------------------------------------------------- cheap path


def test_session_start_never_scans_history(tmp_path, monkeypatch):
    """The auto path must not call the 30-day trends scan (SessionStart budget)."""
    measure, rd, _snap, _home = _load(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        measure, "_collect_trends_data",
        lambda *a, **k: calls.append((a, k)) or {"model_mix": {"x": 1}})

    rd.session_start()

    assert calls == [], "SessionStart routing work must not scan history"


def test_injection_failure_is_reported_and_cleared(tmp_path, monkeypatch):
    _m, rd, snap, _home = _load(tmp_path, monkeypatch)
    import injection
    from _measure import pilot_report
    original = injection.inject_managed_block
    def fail(*args, **kwargs):
        raise PermissionError('private path')
    monkeypatch.setattr(injection, 'inject_managed_block', fail)
    rd._inject_once(None, [])
    from types import SimpleNamespace
    state = pilot_report._routing(SimpleNamespace(SNAPSHOT_DIR=snap))
    assert state['failure'] == 'PermissionError'
    assert 'Routing guidance: failed (PermissionError)' in pilot_report.render({'routing': state})
    monkeypatch.setattr(injection, 'inject_managed_block', original)
    rd._inject_once(None, [])
    assert not (snap / rd.FAILURE_MARKER).exists()
