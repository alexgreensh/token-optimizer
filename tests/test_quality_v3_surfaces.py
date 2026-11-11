"""The same cache is rendered consistently by all quality surfaces."""
import json
import sqlite3
from pathlib import Path

import pytest

from test_usage_counted_once import _load, _noon, _chunk, _write
from test_statusline_quality_cache_dir import SID, _run, _has_node


@pytest.mark.skipif(not _has_node(), reason='node not available')
@pytest.mark.parametrize('measured', [True, False])
def test_cache_statusline_cli_dashboard_agree(tmp_path, monkeypatch, capsys, measured):
    m = _load(tmp_path, monkeypatch)
    home = tmp_path / 'home'
    cache_dir = home / '.claude' / 'token-optimizer'
    cache_dir.mkdir(parents=True)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setattr(m, 'QUALITY_CACHE_DIR', cache_dir)
    path = m.CLAUDE_DIR / 'projects' / 'sample' / (SID + '.jsonl')
    _write(path, [_chunk(_noon(1), req='a', inp=20000, out=100)])
    monkeypatch.setattr(m, '_find_all_jsonl_files', lambda days: [(path, path.stat().st_mtime, 'sample')])
    monkeypatch.setattr(m, '_find_current_session_jsonl', lambda: path)
    m.collect_sessions(days=120, quiet=True)
    m.quality_cache(quiet=True, session_jsonl=str(path), force=True, session_id=SID)
    cache_path = cache_dir / f'quality-cache-{SID}.json'
    cache = json.loads(cache_path.read_text())
    if not measured:
        cache.update(measured=False, grade='?', resource_health_grade='?', score=56.0, resource_health=56.0)
        cache_path.write_text(json.dumps(cache))
    cli = m.quality_analyzer(as_json=True)
    capsys.readouterr()
    conn = m._init_trends_db()
    conn.row_factory = sqlite3.Row
    dashboard = m._query_trends_db(conn, 30)
    conn.close()
    detail = dashboard['daily'][0]['session_details'][0]
    status = _run(home)
    assert cli['score'] == detail['quality_score'] == cache['score']
    assert cli['grade'] == detail['quality_grade'] == cache['grade']
    assert cli['measured'] == detail['quality_measured'] == measured
    if measured:
        assert f"Quality {cache['grade']} {round(cache['score'])}" in status
    else:
        assert 'Quality ~56' in status
