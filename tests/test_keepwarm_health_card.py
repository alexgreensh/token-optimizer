import json
import subprocess
from pathlib import Path


def test_health_card_shows_pings_and_measured_meter_impact():
    asset = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/assets/keepwarm_health.js'
    data = {'cache_health': {'keepwarm': {'state': 'active-probe', 'pings_30d': 3,
        'net_usd': 1.25, 'quota_value': {'measured': {'five_hour': {'median_pct': 0.012, 'n': 5}}}}}}
    code = asset.read_text() + '\nconsole.log(keepwarmHealthHtml(' + json.dumps(data) + '));'
    output = subprocess.run(['node', '-e', code], capture_output=True, text=True, check=True).stdout
    assert 'Keep-warm: paused while we prove it is safe and saves you money' in output
    assert '3 pings' in output and '$1.25' in output
    assert '0.012 percentage points per ping (5 samples)' in output


def test_health_explains_negative_net_and_weekly_pings():
    asset = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/assets/keepwarm_health.js'
    data = {'cache_health': {'keepwarm': {'state': 'off-subscription', 'gate_reason': 'negative-net',
                                          'pings_7d': 4, 'net_usd': -0.12}}}
    code = asset.read_text() + '\nconsole.log(keepwarmHealthHtml(' + json.dumps(data) + '));'
    output = subprocess.run(['node', '-e', code], capture_output=True, text=True, check=True).stdout
    assert 'net' in output.lower() and 'negative' in output.lower()
    assert '4 pings' in output and '7 days' in output


def test_unsupported_platform_explains_keepwarm_is_unavailable():
    asset = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/assets/keepwarm_health.js'
    data = {'cache_health': {'keepwarm': {'state': 'platform-gap', 'runtime': 'claude'}}}
    code = asset.read_text() + '\nconsole.log(keepwarmHealthHtml(' + json.dumps(data) + '));'
    output = subprocess.run(['node', '-e', code], capture_output=True, text=True, check=True).stdout
    assert 'Keep-warm is unavailable on this platform' in output


def test_manage_renders_paused_message_without_keepwarm_toggle():
    html = (Path(__file__).resolve().parents[1] / 'skills/token-optimizer/assets/dashboard.html').read_text()
    assert "if (fname === 'keepwarm')" in html
    assert 'Keep-warm: paused while we prove it is safe and saves you money</div>' in html
    assert "return; }" in html[html.index("if (fname === 'keepwarm')"):html.index("if (fname === 'keepwarm')") + 250]


def test_health_labels_manual_warmups_from_mod():
    asset = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/assets/keepwarm_health.js'
    data = {'cache_health': {'keepwarm': {'state': 'paused', 'manual_pings_30d': 2}}}
    code = asset.read_text() + '\nconsole.log(keepwarmHealthHtml(' + json.dumps(data) + '));'
    output = subprocess.run(['node', '-e', code], capture_output=True, text=True, check=True).stdout
    assert '2 manual' in output and 'Claude Code mod' in output
