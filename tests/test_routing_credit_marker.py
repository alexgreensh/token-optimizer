import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"))
from _measure.routing_credit import routing_enabled_since


def test_routing_marker_requires_a_valid_date(tmp_path):
    assert routing_enabled_since(tmp_path) is None
    marker = tmp_path / "routing_injected_at"
    marker.write_text("invalid", encoding="utf-8")
    assert routing_enabled_since(tmp_path) is None
    marker.write_text("2026-11-19\n", encoding="utf-8")
    assert routing_enabled_since(tmp_path) == "2026-11-19"


def test_routing_credit_stops_at_removal(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from _measure import routing_credit

    (tmp_path / "routing_injected_at").write_text("2026-11-01", encoding="utf-8")
    (tmp_path / "routing_removed_at").write_text("2026-11-10", encoding="utf-8")
    activity = {"2026-11-05": 12, "2026-11-10": 30, "2026-11-15": 50}

    def unrestricted(days, since, *, until=None):
        credit = sum(value for day, value in activity.items()
                     if day >= since and (until is None or day < until))
        return {"realized_cost_usd": credit, "measured_baseline_realized_cost_usd": credit}

    facade = SimpleNamespace(SNAPSHOT_DIR=tmp_path, _routing_unrestricted=unrestricted)
    monkeypatch.setattr(routing_credit._compat, "facade", lambda: facade)
    assert routing_credit.compute(since="2026-11-01")["realized_cost_usd"] == 12
    assert routing_credit.compute(since="2026-11-11")["realized_cost_usd"] == 0
