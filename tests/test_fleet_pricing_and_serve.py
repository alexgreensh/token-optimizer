"""Fleet auditor: generation-aware price overrides and a locked-down dashboard server."""

import http.client
import json
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "fleet-auditor" / "scripts"))

import fleet  # noqa: E402


def test_generation_override_does_not_leak_onto_the_family(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, "FLEET_DB_DIR", tmp_path)
    monkeypatch.setattr(fleet, "_pricing_override", None)
    (tmp_path / "pricing.json").write_text(json.dumps({"opus-5-5": {"input": 1e-6, "output": 2e-6}}))
    pricing = fleet._load_pricing()
    assert pricing["opus-5-5"]["input"] == pytest.approx(1e-6)
    assert pricing["opus"]["input"] != pytest.approx(1e-6)
    assert fleet._pricing_key("claude-opus-5-5") == "opus-5-5"
    monkeypatch.setattr(fleet, "_pricing_override", None)


def test_dashboard_server_serves_only_the_page(tmp_path, monkeypatch):
    page = tmp_path / "fleet-dashboard.html"
    page.write_text("<h1>fleet</h1>")
    (tmp_path / "fleet.db").write_text("private")
    monkeypatch.setattr(fleet, "FLEET_DASHBOARD_PATH", page)
    port = 18000 + (hash(str(tmp_path)) % 1000)
    threading.Thread(target=fleet._serve_dashboard, args=("127.0.0.1", port), daemon=True).start()
    time.sleep(0.4)

    def status(path, host):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path, headers={"Host": host})
        return conn.getresponse().status

    assert status("/fleet-dashboard.html", f"127.0.0.1:{port}") == 200
    assert status("/", f"localhost:{port}") == 200
    assert status("/fleet.db", f"127.0.0.1:{port}") == 404
    assert status("/pricing.json", f"127.0.0.1:{port}") == 404
    assert status("/fleet-dashboard.html", f"attacker.example:{port}") == 403


def test_fleet_haiku_5_5_long_prompt_tier_is_per_request(tmp_path, monkeypatch):
    """Haiku 5.5 pays 5x when ONE request's full prompt (input + cache reads +
    cache writes) is over 100K; totals over many requests must not be repriced."""
    monkeypatch.setattr(fleet, "FLEET_DB_DIR", tmp_path)
    monkeypatch.setattr(fleet, "_pricing_override", None)
    base = fleet._load_pricing()["haiku-5-5"]
    lc = fleet._bundled_long_context_prices()["haiku-5-5"]
    for field in ("input", "output", "cache_read", "cache_write", "cache_write_1h"):
        assert lc[field] == pytest.approx(base[field] * 5)
    key = fleet._pricing_key("claude-haiku-5-5")
    assert key == "haiku-5-5"
    small = fleet.TokenBreakdown(input=10_000, output=1_000, cache_read=50_000, cache_write=0)
    big = fleet.TokenBreakdown(input=10_000, output=1_000, cache_read=95_000, cache_write=0)
    assert fleet.calculate_cost(small, key, per_request=True) == pytest.approx(
        10_000 * base["input"] + 1_000 * base["output"] + 50_000 * base["cache_read"])
    assert fleet.calculate_cost(big, key, per_request=True) == pytest.approx(
        10_000 * lc["input"] + 1_000 * lc["output"] + 95_000 * lc["cache_read"])
    # Aggregate callers keep the flat card.
    assert fleet.calculate_cost(big, key) == pytest.approx(
        10_000 * base["input"] + 1_000 * base["output"] + 95_000 * base["cache_read"])
    # A model without an LC card never surcharges.
    opus = fleet._load_pricing()["opus"]
    assert fleet.calculate_cost(big, "opus", per_request=True) == pytest.approx(
        10_000 * opus["input"] + 1_000 * opus["output"] + 95_000 * opus["cache_read"])
    monkeypatch.setattr(fleet, "_pricing_override", None)


def test_fleet_user_price_override_beats_long_prompt_tier(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, "FLEET_DB_DIR", tmp_path)
    monkeypatch.setattr(fleet, "_pricing_override", None)
    (tmp_path / "pricing.json").write_text(json.dumps({"haiku-5-5": {"input": 2e-7, "output": 1e-6}}))
    key = fleet._pricing_key("claude-haiku-5-5")
    big = fleet.TokenBreakdown(input=150_000, output=0, cache_read=0, cache_write=0)
    assert fleet.calculate_cost(big, key, per_request=True) == pytest.approx(150_000 * 2e-7)
    monkeypatch.setattr(fleet, "_pricing_override", None)


def test_no_stale_fixed_percent_compact_advice_in_fleet():
    text = (REPO / "skills" / "fleet-auditor" / "scripts" / "fleet.py").read_text(encoding="utf-8")
    demo = (REPO / "skills" / "token-optimizer" / "assets" / "fleet-demo.html").read_text(encoding="utf-8")
    assert "Use /compact at 50-70%" not in text
    assert "Use /compact at 50-70%" not in demo
