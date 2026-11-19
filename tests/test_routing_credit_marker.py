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
