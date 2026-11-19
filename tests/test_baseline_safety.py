"""A user must never silently lose, or silently downgrade, their frozen baseline.

The baseline is the ONLY record of what a user's sessions cost BEFORE Token
Optimizer. Every "you saved X" figure is measured against it. Three ways it was
losable, all found on the author's own machine:

1. ORPHANED. A re-install under a different marketplace id gets a fresh plugin
   data dir. The previous identity's baseline is stranded, not deleted, so the
   code re-captured from scratch months later against a thinned history. Her
   anchor went from a $14.73/session capture to a $9.87 one, which collapsed the
   headline saving from ~$2,076/mo to $498/mo.
2. DELETED ON VERSION BUMP. `_get_baseline_state` unlinked any baseline whose
   `version` != `_BASELINE_VERSION`, so bumping that constant would destroy every
   user's anchor on OUR release schedule, with no recovery.
3. SILENTLY SHRUNK. Nothing compared a recapture against the incumbent, so a
   thinner history quietly replaced a good anchor with a worse one.

Run: python3 -m pytest tests/test_baseline_safety.py -v
"""
import json
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("TOKEN_OPTIMIZER_BASELINE_ADOPT", "0")  # off unless a test wants it
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    import measure
    return measure


def _baseline(cache_read, *, version=4, captured_at="2026-06-23T16:06:59",
              opus=0.8369, cache_write=688319, fresh=43396, output=74232):
    return {
        "version": version,
        "typical_session": {"fresh_input": fresh, "cache_write": cache_write,
                            "cache_read": cache_read, "output": output},
        "opus_share": opus,
        "model_shares": {"opus": opus, "sonnet": round(1 - opus, 4)},
        "captured_at": captured_at,
        "source": "frozen_from_history",
    }


# ---------------------------------------------------------------- well-formed

def test_rejects_malformed_and_unknown_versions(m):
    assert m._baseline_is_well_formed(_baseline(20_616_554)) is True
    assert m._baseline_is_well_formed({}) is False
    assert m._baseline_is_well_formed(_baseline(1, version=99)) is False
    bad = _baseline(1)
    bad["typical_session"]["cache_read"] = -5
    assert m._baseline_is_well_formed(bad) is False
    no_shares = _baseline(1)
    no_shares["model_shares"] = {}
    assert m._baseline_is_well_formed(no_shares) is False


# ------------------------------------------------------- shrink guard

def test_shrink_guard_catches_the_real_july_regression(m):
    """The exact numbers from two captures: $14.73 -> $9.87."""
    june = _baseline(20_616_554)
    july = _baseline(13_133_302, captured_at="2026-07-24T14:41:00",
                     opus=0.95, cache_write=408_302, fresh=7_186, output=40_943)
    assert m._baseline_priced_cost(june) > m._baseline_priced_cost(july)
    assert m._baseline_shrink_is_material(june, july) is True


def test_shrink_guard_is_asymmetric(m):
    """Growing is fine; only a DOWNWARD drift needs a human."""
    june = _baseline(20_616_554)
    july = _baseline(13_133_302, opus=0.95, cache_write=408_302,
                     fresh=7_186, output=40_943)
    assert m._baseline_shrink_is_material(july, june) is False


def test_shrink_guard_does_not_fire_on_a_deterministic_recapture(m):
    """An honest re-run moves 0%, so it must never trip the guard."""
    b = _baseline(20_616_554)
    assert m._baseline_shrink_is_material(b, dict(b)) is False


def test_shrink_guard_tolerates_small_honest_drift(m):
    """5% is noise, not a thinned history; the threshold is 15% cost."""
    inc = _baseline(20_616_554)
    slightly_less = _baseline(int(20_616_554 * 0.95))
    assert m._baseline_shrink_is_material(inc, slightly_less) is False


def test_recapture_materially_smaller_keeps_the_incumbent(m, tmp_path, monkeypatch):
    """Upgrade re-anchor: a tiny recomputed baseline must not replace a frozen one."""
    snap = tmp_path / "snap"
    snap.mkdir(parents=True, exist_ok=True)
    # Install proof lets get() past the "keep legacy baseline" early return.
    (snap / "snapshot_before.json").write_text(
        json.dumps({"timestamp": "2026-11-01T00:00:00"}), encoding="utf-8")
    bpath = snap / "baseline_state.json"
    incumbent = _baseline(20_616_554, version=4, fresh=100_000)
    incumbent["activity_basis"] = 1
    bpath.write_text(json.dumps(incumbent), encoding="utf-8")

    candidate = _baseline(100, version=5, captured_at="2026-11-10T00:00:00")
    candidate["activity_basis"] = 1
    candidate["typical_session"] = {"fresh_input": 10, "cache_write": 0,
                                   "cache_read": 100, "output": 10}
    candidate["window"] = {"start": "2026-10-01", "end": "2026-10-30",
                           "sessions_used": 40, "sessions_total": 40, "elapsed": True}
    monkeypatch.setattr(m, "_compute_baseline_state", lambda: candidate)

    got = m._get_baseline_state()

    assert got["typical_session"]["fresh_input"] == 100_000, (
        "a materially smaller recapture must not become the active baseline"
    )
    on_disk = json.loads(bpath.read_text(encoding="utf-8"))
    assert on_disk["typical_session"]["fresh_input"] == 100_000
    assert on_disk["version"] == 4
    parked = json.loads((snap / "baseline_state.candidate.json").read_text(encoding="utf-8"))
    assert parked["rejected_reason"] == "materially smaller than the existing baseline"
    assert parked["typical_session"]["fresh_input"] == 10
    assert parked["incumbent_cost_usd"] > parked["candidate_cost_usd"]


def test_recapture_within_noise_still_freezes(m, tmp_path, monkeypatch):
    """The guard is asymmetric: honest drift under the threshold still re-freezes."""
    snap = tmp_path / "snap"
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "snapshot_before.json").write_text(
        json.dumps({"timestamp": "2026-11-01T00:00:00"}), encoding="utf-8")
    bpath = snap / "baseline_state.json"
    incumbent = _baseline(20_616_554, version=4, fresh=100_000)
    incumbent["activity_basis"] = 1
    bpath.write_text(json.dumps(incumbent), encoding="utf-8")

    candidate = _baseline(20_616_554, version=5, fresh=90_000)
    candidate["activity_basis"] = 1
    candidate["window"] = {"start": "2026-10-01", "end": "2026-10-30",
                           "sessions_used": 40, "sessions_total": 40, "elapsed": True}
    monkeypatch.setattr(m, "_compute_baseline_state", lambda: candidate)

    got = m._get_baseline_state()

    assert got["version"] == m._BASELINE_VERSION
    assert got["typical_session"]["fresh_input"] == 90_000
    on_disk = json.loads(bpath.read_text(encoding="utf-8"))
    assert on_disk["version"] == m._BASELINE_VERSION
    assert on_disk["typical_session"]["fresh_input"] == 90_000
    assert not (snap / "baseline_state.candidate.json").exists()


# ------------------------------------------------- migrate, never delete

def test_v3_migrates_forward_instead_of_being_deleted(m):
    v3 = _baseline(20_616_554, version=3)
    out = m._migrate_baseline(v3)
    assert out is not None
    assert out["version"] == m._BASELINE_VERSION
    assert out["migrated_from_version"] == 3
    assert out["structural_overhead_tokens"] == 0, "absent != smaller"
    assert v3["version"] == 3, "must not mutate the caller's dict"


def test_unmigratable_version_returns_none_not_a_guess(m):
    assert m._migrate_baseline(_baseline(1, version=99)) is None
    assert m._migrate_baseline("not a dict") is None


def test_version_mismatch_retains_the_old_file(m, tmp_path):
    """THE LANDMINE: a _BASELINE_VERSION bump must not destroy the only copy."""
    snap = tmp_path / "snap"
    snap.mkdir(parents=True, exist_ok=True)
    bpath = snap / "baseline_state.json"
    bpath.write_text(json.dumps(_baseline(20_616_554, version=99)), encoding="utf-8")

    m._get_baseline_state(freeze=False)

    assert bpath.exists() or (snap / "baseline_state.v99.json").exists(), (
        "an unmigratable baseline must be retained, never unlinked"
    )


# ------------------------------------------------------- adoption rules

def test_adoption_prefers_oldest_then_larger_never_smaller(m, tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_BASELINE_ADOPT", "1")
    base = tmp_path / "plugins" / "data"
    older = base / "id-a" / "data.disabled-20260623"
    newer = base / "id-b" / "data"
    for d in (older, newer):
        d.mkdir(parents=True, exist_ok=True)
    older.joinpath("baseline_state.json").write_text(
        json.dumps(_baseline(20_616_554, captured_at="2026-06-23T16:06:59")), encoding="utf-8")
    newer.joinpath("baseline_state.json").write_text(
        json.dumps(_baseline(13_133_302, captured_at="2026-07-24T14:41:00")), encoding="utf-8")

    monkeypatch.setattr(m.plugin_env, "_PLUGIN_DATA_BASE", base, raising=False)
    monkeypatch.setattr(m.plugin_env, "find_sibling_plugin_data_dirs",
                        lambda active=None: [base / "id-a", base / "id-b"], raising=False)

    got = m._adopt_orphaned_baseline()
    assert got is not None
    assert got["typical_session"]["cache_read"] == 20_616_554, (
        "must adopt the OLDER capture, which here is also the larger one"
    )
    assert got["adoption_rule"] == "oldest_captured_at"
    assert "adopted_from" in got and "adopted_at" in got
    assert got["captured_at"] == "2026-06-23T16:06:59", "original capture time preserved"


def test_adoption_can_be_disabled(m, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_BASELINE_ADOPT", "0")
    assert m._baseline_adoption_candidates() == []


def test_adoption_skips_symlinks_and_oversized_files(m, tmp_path, monkeypatch):
    """A symlink must not be a route out of the plugin-data base."""
    monkeypatch.setenv("TOKEN_OPTIMIZER_BASELINE_ADOPT", "1")
    base = tmp_path / "plugins" / "data"
    ident = base / "id-a" / "data"
    ident.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_baseline(99_999_999)), encoding="utf-8")
    ident.joinpath("baseline_state.json").symlink_to(outside)

    monkeypatch.setattr(m.plugin_env, "_PLUGIN_DATA_BASE", base, raising=False)
    monkeypatch.setattr(m.plugin_env, "find_sibling_plugin_data_dirs",
                        lambda active=None: [base / "id-a"], raising=False)

    assert m._baseline_adoption_candidates() == [], "symlinked baseline must be skipped"
