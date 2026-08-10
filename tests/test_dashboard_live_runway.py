#!/usr/bin/env python3
"""Live window/runway card on the dashboard.

The 5h/7d "window reading" is only ever fresh while the statusline is writing it
(active turns). The dashboard already has a live path (`GET /api/savings` ->
`_live_savings_payload` -> client `refreshSavings`) that patched only the savings
and cache-health tiles, so the ONE number that can jump the instant a new reading
lands (the window card) was the one number nothing live-updated, forcing a manual
Regenerate that provably cannot refresh it.

This adds `runway` to the live payload, patches it client-side, and polls every
~60s, so an open dashboard reflects a new reading on its own.

Guarantees under test:
- `_live_savings_payload` carries `runway`, equal to the shared `runway_snapshot`
  builder (single source of truth; a live-patched card can't diverge from a regen)
- fail-open: a raising `runway_snapshot` degrades to `runway: None`, never raises
- the payload reflects whatever reading exists now (live-reflect)
- client wiring is present in BOTH the canonical and plugin-mirror dashboard.html
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
ASSET = REPO / "skills" / "token-optimizer" / "assets" / "dashboard.html"
ASSET_MIRROR = (
    REPO / "plugins" / "token-optimizer" / "skills" / "token-optimizer"
    / "assets" / "dashboard.html"
)


@pytest.fixture()
def measure(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR",
        str(tmp_path / "base" / "token-optimizer-a" / "data"),
    )
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("measure", None)


# ---- payload ----

def test_payload_carries_runway(measure):
    """The live payload includes the window/runway card, equal to the shared
    builder (mirrors test_matches_full_regen_source's single-source rule)."""
    p = measure._live_savings_payload(days=30)
    assert "runway" in p
    assert p["runway"] == measure.runway_snapshot(days=30)


def test_runway_fail_open(measure, monkeypatch):
    """A raising runway builder must not break /api/savings: payload stays ok,
    runway degrades to None (same fail-open shape as the savings/cache blocks)."""
    def _boom(*a, **k):
        raise RuntimeError("meter read blew up")
    monkeypatch.setattr(measure, "runway_snapshot", _boom)
    p = measure._live_savings_payload(days=30)
    assert p["ok"] is True
    assert p["runway"] is None


def test_runway_reflects_current_reading(measure, monkeypatch):
    """Live-reflect: the payload carries whatever reading exists NOW, so an open
    dashboard shows a fresh 5h/7d figure the instant the statusline writes one."""
    sentinel = {"meter_ts": 1234567890, "meter_stale": False, "windows": ["fresh"]}
    monkeypatch.setattr(measure, "runway_snapshot", lambda *a, **k: sentinel)
    p = measure._live_savings_payload(days=30)
    assert p["runway"] == sentinel


# ---- client wiring (edit-time string asserts, mirrored to the plugin tree) ----

@pytest.mark.parametrize("asset", [ASSET, ASSET_MIRROR], ids=["canonical", "plugin-mirror"])
def test_client_patches_runway(asset):
    """refreshSavings must assign the live runway into data.runway so the repaint
    picks up the fresher reading."""
    html = asset.read_text(encoding="utf-8")
    assert "data.runway = payload.runway" in html


@pytest.mark.parametrize("asset", [ASSET, ASSET_MIRROR], ids=["canonical", "plugin-mirror"])
def test_client_has_periodic_poll(asset):
    """A periodic poll of the live endpoint must exist so the window auto-updates
    without a manual Regenerate."""
    html = asset.read_text(encoding="utf-8")
    assert "setInterval(" in html
    assert "refreshSavingsLive" in html
    # the poll body calls the guarded live refresh (no-op unless __TOKEN_API_LIVE)
    assert "refreshSavingsLive()" in html
