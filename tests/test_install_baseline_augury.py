import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"))
from datetime import datetime, timedelta
from types import SimpleNamespace

from _measure import install_baseline, routing_credit


def test_baseline_uses_preinstall_window_and_measured_mix(tmp_path, monkeypatch):
    db = tmp_path / 'trends.db'
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE session_activity (date TEXT, id TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_create_5m_tokens INTEGER, cache_create_1h_tokens INTEGER, cache_hit_rate REAL, is_sidechain INTEGER)')
    conn.execute('CREATE TABLE model_daily (date TEXT, model TEXT, total_tokens INTEGER)')
    install = datetime(2026, 11, 1)
    for i in range(150):
        day = (install - timedelta(days=120) + timedelta(days=i)).date().isoformat()
        conn.execute('INSERT INTO session_activity VALUES (?, ?, 100, 20, 0, 0, 0, 0)', (day, str(i)))
        conn.execute('INSERT INTO model_daily VALUES (?, ?, 100)', (day, 'opus' if i < 120 else 'sonnet'))
    conn.commit()
    conn.close()
    f = SimpleNamespace(_init_trends_db=lambda: sqlite3.connect(db), ensure_activity_view=lambda c: None,
        _BASELINE_ONBOARDING_DAYS=1, _BASELINE_EARLY_WINDOW_DAYS=30,
        _BASELINE_MIN_STABLE_SESSIONS=30, _BASELINE_VERSION=5, _BASELINE_WINSOR_PCT=.99,
        _winsorized_mean_session=lambda rows: ([100, 20, 0, 0], len(rows), len(rows)),
        _normalize_model_name=lambda x: x, _routing_class_rates=lambda x: {'input': 1},
        _pretool_baseline_mix=lambda: {'opus': 0.1, 'sonnet': 0.9},
        SNAPSHOT_DIR=tmp_path, sqlite3=sqlite3)
    monkeypatch.setattr(install_baseline._compat, 'facade', lambda: f)
    monkeypatch.setattr(install_baseline.baseline_provenance, 'install_evidence', lambda _: {'date': '2026-11-01', 'source': 'snapshot'})
    baseline = install_baseline.compute()
    assert baseline['window']['start'] == '2026-10-02'
    assert baseline['window']['end'] == '2026-10-31'
    assert baseline['window']['coverage'] == 'pre_install'
    assert baseline['model_shares'] == {'opus': 1.0}
    assert baseline['opus_share_source'] != 'pretool_baseline'
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM session_activity WHERE date >= '2026-10-02' AND date < '2026-11-01' AND CAST(id AS INTEGER) % 2 = 0")
    conn.commit()
    conn.close()
    fallback = install_baseline.compute()
    assert fallback['window']['coverage'] == 'post_install'
    assert fallback['window']['start'] == '2026-07-05'


def test_routing_credit_starts_at_injection(tmp_path, monkeypatch):
    cutoffs = []
    def unrestricted(days, since):
        cutoffs.append(since)
        return {'realized_cost_usd': 12, 'measured_baseline_realized_cost_usd': 4}
    facade = SimpleNamespace(SNAPSHOT_DIR=tmp_path, _routing_unrestricted=unrestricted)
    monkeypatch.setattr(routing_credit._compat, 'facade', lambda: facade)
    assert routing_credit.compute(since='2026-07-01')['realized_cost_usd'] == 0
    (tmp_path / 'routing_injected_at').write_text('2026-11-01')
    assert routing_credit.compute(since='2026-07-01')['realized_cost_usd'] == 12
    assert cutoffs[-1] == '2026-11-01'


def test_legacy_baseline_is_kept_when_history_cannot_be_reanchored(tmp_path, monkeypatch):
    import json
    old = {'version': 4, 'activity_basis': 1, 'typical_session': {'fresh_input': 1},
           'model_shares': {'opus': 1}}
    (tmp_path / 'baseline_state.json').write_text(json.dumps(old))
    facade = SimpleNamespace(SNAPSHOT_DIR=tmp_path, _BASELINE_VERSION=5,
        _BASELINE_KNOWN_VERSIONS=(3, 4, 5), _compute_baseline_state=lambda: None,
        _baseline_is_well_formed=lambda state: True)
    monkeypatch.setattr(install_baseline._compat, 'facade', lambda: facade)
    assert install_baseline.get() == old
    assert json.loads((tmp_path / 'baseline_state.json').read_text()) == old
