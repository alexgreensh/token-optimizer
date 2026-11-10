"""Ordinary edits cannot create new cohort demotions."""
from test_counted_cumulative import m, _db
import sqlite3
from datetime import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"))
import compression_backfill


def test_edit_rate_does_not_demote_and_existing_demotions_stay(m, tmp_path, monkeypatch):
    _db(m, tmp_path, monkeypatch)
    old = ("python", "16-64KB")
    active = ("typescript", "16-64KB")
    m._write_tripwire_sidecar({old}, {})
    monkeypatch.setattr(m, "_resolve_active_cohorts", lambda: {old, active})
    monkeypatch.setattr(m, "_tripwire_min_samples", lambda cohort: 1)
    monkeypatch.setattr(m, "_compute_cohort_edit_rates", lambda *a, **k: {
        active: {"skeletons": 100, "followups": 100, "edit_rate": 1.0}})
    result = m.evaluate_cohort_tripwire(force=True)
    assert set(map(tuple, result["demoted"])) == {old}
    assert result["edit_rates"] == {}
    state = m._cohort_tripwire_state()
    assert set(map(tuple, state["demoted"])) == {old}
    assert state["cohorts"] == []


def test_edit_rate_never_claims_promotion_readiness(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    with sqlite3.connect(str(dbp)) as conn:
        conn.execute("INSERT INTO compression_events(timestamp,feature,original_tokens,compressed_tokens,tier) VALUES(?,?,?,?,?)",
                     (datetime.now().isoformat(), "first_read_skeleton", 1000, 100, "opportunity"))
        conn.commit()
    result = m._get_compression_coverage(days=30)
    assert result["first_read_proxy"] is None


def test_backfill_never_recommends_promotion_from_edit_rate():
    cohorts = {("python", "16-64KB"): {"reads": 100, "sessions": set(range(10)),
               "edited_within": 0, "edited_ever": 0, "ratio_sum": 90, "would_be_tokens": 9000}}
    report = compression_backfill.build_report(cohorts, {}, 5)
    assert report["cohorts"][0]["promotion_ready"] is False
