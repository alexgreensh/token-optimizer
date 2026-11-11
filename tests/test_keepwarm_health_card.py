import json
import subprocess
from pathlib import Path


def test_health_card_shows_pings_and_measured_meter_impact():
    asset = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/assets/keepwarm_health.js'
    data = {'cache_health': {'keepwarm': {'state': 'active-probe', 'pings_30d': 3,
        'net_usd': 1.25, 'quota_value': {'measured': {'five_hour': {'median_pct': 0.012, 'n': 5}}}}}}
    code = asset.read_text() + '\nconsole.log(keepwarmHealthHtml(' + json.dumps(data) + '));'
    output = subprocess.run(['node', '-e', code], capture_output=True, text=True, check=True).stdout
    assert 'Automatic pings are paused' in output
    assert '3 pings' in output and '$1.25' in output
    assert '0.012 percentage points per ping (5 samples)' in output
