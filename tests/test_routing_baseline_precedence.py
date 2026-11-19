import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))
from _measure import routing_credit


def test_routing_before_mix_precedence(tmp_path):
    import json
    state = {'model_shares': {'opus': 0.9}, 'opus_share_source': 'robust_earliest',
             'window': {'coverage': 'pre_install'}}
    snapshot = tmp_path / 'snapshot_before.json'
    snapshot.write_text(json.dumps({'model_mix': {'activity_basis': 1, 'shares': {'opus': 0.8}}}))
    f = SimpleNamespace(SNAPSHOT_DIR=tmp_path, _get_baseline_state=lambda: state,
                        _earliest_model_mix=lambda: {'shares': {'opus': 0.7}},
                        _pretool_baseline_mix=lambda: {'opus': 0.6})
    assert routing_credit.baseline(f)[:3] == ({'opus': 0.9}, 'frozen_baseline', False)
    state['window']['coverage'] = 'post_install'
    assert routing_credit.baseline(f)[:3] == ({'opus': 0.8}, 'snapshot', False)
    snapshot.write_text('{}')
    assert routing_credit.baseline(f)[:3] == ({'opus': 0.7}, 'history', False)
    f._earliest_model_mix = lambda: None
    assert routing_credit.baseline(f)[:3] == ({'opus': 0.6}, 'pretool_baseline', True)
