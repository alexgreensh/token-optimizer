import json
from datetime import datetime, timezone
from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))
from _measure import archive_void_evidence as av


def test_only_unique_full_original_is_voided(tmp_path):
    path = tmp_path / 'transcript.jsonl'
    t = '2026-08-15T11:00:00Z'
    records = [
        {'type': 'assistant', 'timestamp': t, 'message': {'content': [{'type': 'tool_use', 'id': 'toolu_a', 'name': 'mcp__x__fetch'}]}},
        {'type': 'user', 'timestamp': t, 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_a', 'content': 'a' * 4000}]}},
    ]
    path.write_text('\n'.join(map(json.dumps, records)))
    local = datetime.fromisoformat(t.replace('Z', '+00:00')).astimezone().replace(tzinfo=None).isoformat()
    rows = [(1, local, 'replaced mcp__x__fetch (4,000 chars -> 300 chars)'),
            (2, local, 'replaced mcp__x__other (4,000 chars -> 300 chars)'),
            (3, local, 'replaced mcp__x__fetch (5,000 chars -> 300 chars)')]
    assert av.full_result_ids(path, rows) == {1}
    assert av.full_result_ids(path, [rows[0], (4, local, rows[0][2])]) == set()
