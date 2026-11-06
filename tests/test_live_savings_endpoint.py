#!/usr/bin/env python3
"""Live /api/savings recompute.

The dashboard daemon serves a static HTML file, so a browser refresh re-serves
stale numbers. `_live_savings_payload` recomputes the savings + cache-health
blocks on demand (the endpoint the page fetches on load/refocus) so a refresh
shows current savings without a full multi-second dashboard regen.

Guarantees under test:
- returns the shape the frontend patches into `data.savings` / `data.cache_health`
- carries a `generated_at` stamp and `ok` flag
- fail-open: a broken savings source degrades to a safe empty shape, never raises

Run: python3 -m pytest tests/test_live_savings_endpoint.py -q
"""

from __future__ import annotations
from helpers.measure_split import exec_source_module as _split_exec_module

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


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
    _split_exec_module(mod)
    yield mod
    sys.modules.pop("measure", None)


def test_payload_shape(measure):
    p = measure._live_savings_payload(days=30)
    assert p["ok"] is True
    assert isinstance(p["savings"], dict)
    assert isinstance(p["cache_health"], dict)
    assert p["generated_at"]  # ISO timestamp present


def test_savings_carries_since_install_key(measure):
    # The frontend reads s.since_install; the endpoint must always set the key
    # (its value may be None when no install date is known).
    p = measure._live_savings_payload(days=30)
    assert "since_install" in p["savings"]


def test_matches_full_regen_source(measure):
    # Single source of truth: the endpoint's savings block must equal what the
    # dashboard generator would embed (merged savings + since_install), so a
    # live-patched tile can never diverge from a full regen.
    p = measure._live_savings_payload(days=30)
    expected = measure._get_merged_savings(days=30)
    # by_category / totals come straight from _get_merged_savings
    assert p["savings"].get("total_cost_usd") == expected.get("total_cost_usd")
    assert p["savings"].get("total_tokens") == expected.get("total_tokens")


def test_fail_open_when_savings_source_raises(measure, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(measure, "_get_merged_savings", boom)
    p = measure._live_savings_payload(days=30)
    # degrades to a safe empty shape instead of propagating the error
    assert p["ok"] is True
    assert p["savings"] == {"total_tokens": 0, "total_cost_usd": 0.0, "by_category": {}}
    assert isinstance(p["cache_health"], dict)


def test_fail_open_when_cache_health_raises(measure, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("cache eval slow/failed")

    monkeypatch.setattr(measure, "_cache_ttl_waste_cached", boom)
    p = measure._live_savings_payload(days=30)
    assert p["ok"] is True
    # cache_health degrades to the safe base (available False / opportunity tier);
    # the keepwarm sub-block may still be attached since it evaluates separately.
    assert p["cache_health"].get("available") is False
    assert p["cache_health"].get("tier") == "opportunity"
