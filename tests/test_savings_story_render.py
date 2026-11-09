"""U9: run the real savings-story renderer (assets/savings_story.js) in Node.

Synthetic payloads only. Covers: old payloads render nothing and never throw,
the total is the sum of its measured and estimated rows, routing and opportunity
stay out of it, the Savings tab folds the previous cards under the story, and the
generator inlines the module (a missing module leaves the old layout).
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'skills/token-optimizer/assets'
STORY = ASSETS / 'savings_story.js'
TEMPLATE = ASSETS / 'dashboard.html'
sys.path.insert(0, str(ROOT / 'skills/token-optimizer/scripts'))
import dashboard_assets  # noqa: E402

ESC = ("const esc = x => String(x == null ? '' : x).replace(/[&<>\"']/g, "
       "c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));\n")


def _node(script, payload):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required to execute the dashboard renderer')
    r = subprocess.run([node, '-', json.dumps(payload)], input=script,
                       capture_output=True, text=True, check=True)
    return r.stdout


def story(data):
    script = ESC + STORY.read_text() + (
        '\nconst data = JSON.parse(process.argv[2]);'
        '\nprocess.stdout.write(savingsStoryHtml(data));')
    return _node(script, data)


def render_savings(savings, with_story=True):
    html = TEMPLATE.read_text()
    body = html[html.index('  function renderSavings()'):html.index('  function renderHealth()')]
    helper = html[html.index('function tokenSplitText('):html.index('function hostRefreshHtml(')]
    if with_story:
        helper = dashboard_assets.inline(helper, ASSETS)
    prefix = ('const data = JSON.parse(process.argv[2]);\n'
              "const el = {innerHTML: '', querySelectorAll: () => []};\n"
              'const document = {getElementById: () => el};\n' + ESC +
              "const runwayCardHtml = () => '<div id=\"legacy-runway\"></div>';\n"
              "const tokensSavedCardHtml = () => '';\n")
    return _node(prefix + helper + body + '\nrenderSavings(); process.stdout.write(el.innerHTML);',
                 {'savings': savings})


def effect():
    return {
        'available': True,
        'window': {'start': '2026-01-01T00:00:00', 'end': '2026-01-31T00:00:00'},
        'per_session': {'sessions': 120, 'median': 0.05, 'p75': 0.11, 'p90': 0.2,
                        'net_median': 0.048, 'net_p75': 0.105},
        'coverage': {'sessions': 200, 'ran_sessions': 150, 'ran_traffic_share': 0.75,
                     'ran_no_ledger_sessions': 12, 'unknown_sessions': 0,
                     'classes': {'ledger': {'sessions': 138, 'traffic_share': 0.6}}},
        'fleet': {'cost_share': 0.02, 'cost_share_net': 0.019},
        'overhead': {'usd': 1.5, 'reread_tokens': 4000000},
    }


def full():
    return {
        'period_days': 30,
        'session_effect': effect(),
        'action_savings': {'logged_usd': 40.0, 'reread_usd': 30.0, 'estimated_usd': 12.0,
                           'class_premium_usd': 8.0, 'routing_usd': 50.0,
                           'estimates_usd': {'resume_lean_estimated': 5.0}},
        'token_headline': {'context_removed_tokens': 2000000, 'output_not_generated_tokens': 0,
                           'reload_avoided_tokens': 1000000, 'reread_tokens': 80000000,
                           'reread_available': True},
        'counted_period': {'removed_tokens': 2000000, 'reread_tokens': 80000000,
                           'oneshot_usd': 10.0, 'reread_usd': 30.0},
        'structural_detail': {'method': 'trim_ledger', 'cost_saved_usd': 4.0, 'tokens_saved': 500000,
                              'estimated': {'cost_saved_usd': 20.0, 'tokens_saved': 90000000},
                              'trims': [{'label': 'Archived skills', 'ts': '2026-01-20T00:00:00+00:00',
                                         'source': 'audit_action', 'tokens_per_call': 1000,
                                         'requests': 500, 'cost_saved_usd': 4.0,
                                         'projected_30d_usd': 9.0}]},
        'transcript_backfill': {'sessions': 30, 'measured_usd': 6.0, 'measured_tokens': 9000000},
        'output_reread': {'available': True, 'cost_saved_usd': 3.0, 'tokens_saved': 7000000,
                          'control_net': None},
        'hook_skips': {'overruns': 40, 'skipped_compressions': 3, 'skip_rate': 0.02},
        'lean_resume_carry': {'available': True, 'carry_tokens': 1000000, 'carry_cost_usd': 77.0},
        'model_routing': {'baseline_opus_share': 0.95, 'baseline_source': 'pretool_baseline',
                          'current_opus_share': 0.9, 'actual_cost_usd': 900.0,
                          'measured_baseline_opus_share': 0.8, 'measured_baseline_realized_cost_usd': 0.0},
        'structural_potential': {'cost_saved_usd': 11.0, 'unused_skills': 3},
    }


@pytest.mark.parametrize('savings', [
    {}, {'session_effect': None}, {'session_effect': {'available': False, 'reason': 'codex'}},
    {'session_effect': {'available': True}},  # available but every block missing
    {'session_effect': {'available': True, 'per_session': 'junk', 'coverage': 7}, 'action_savings': None,
     'structural_detail': {'method': 'trim_ledger', 'trims': [None, {}]}, 'model_routing': []},
])
def test_old_or_malformed_payloads_never_throw(savings):
    out = story({'savings': savings})
    assert 'NaN' not in out and 'undefined' not in out


def test_old_payload_returns_nothing_and_tab_keeps_previous_layout():
    assert story({'savings': {'period_days': 30}}) == ''
    html = render_savings({'period_days': 30, 'total_cost_usd': 5})
    assert 'legacy-runway' in html and 'class="st"' not in html
    assert '<div class="label">Value</div>' in html


def test_total_is_the_sum_of_its_bands_and_excludes_routing():
    out = story({'savings': full()})
    # measured: context 40-4=36, premium 8, rereads 30, cloud 6, setup 4 = 84
    # estimated: attested 20, output 3, resumes 5, other 12-5=7 = 35
    assert '$119' in out and '$84.00' in out and '$35.00' in out
    assert 'Context removed' in out and '$36.00' in out and 'Other estimates' in out and '$7.00' in out
    total = out.split('st-total')[1].split('st-pair')[0]
    assert '$50' not in total  # routing never enters the total
    assert 'Model routing' not in total.split('How this total is built')[0]


def test_hero_coverage_limit_and_routing_copy():
    out = story({'savings': full()})
    assert 'In a typical session, <span class="st-num">4.8%' in out
    assert 'top quarter of sessions, 10.5% or more' in out
    assert '120 sessions measured' in out
    assert 'ran in <strong>75%</strong>' in out and '12 sessions ran Token Optimizer but are' in out
    assert 'understates' in out
    assert 'Hooks ran out of time 40 times' in out
    assert 'Estimated' in out and re.search(r'11\.7%</span> of what this work', out)  # 119/(900+119)
    assert 'assuming 95% of your work ran on Opus' in out and 'earliest tracked month ran 80% Opus' in out
    assert 'Diagnostic, not counted' in out and '$77.00' in out


def test_no_forbidden_wording():
    out = story({'savings': full()})
    low = out.lower()
    assert '$0 on subscription' not in low and 'fresh' not in low
    assert '<em' not in out and 'font-style:italic' not in low
    assert 'class="label"' not in out  # no small-caps eyebrow labels in the story


def test_setup_trims_empty_state():
    s = full(); s['structural_detail'] = {'method': 'trim_ledger', 'status': 'no_trims', 'trims': []}
    out = story({'savings': s})
    assert 'No setup trims recorded yet' in out


def test_savings_tab_folds_previous_cards_and_opportunity_under_the_story():
    s = full(); s['total_cost_usd'] = 40.0
    s['before_after'] = {'monthly_savings_usd': 300, 'actual_monthly_usd': 1000, 'counterfactual_monthly_usd': 1300}
    html = render_savings(s)
    assert html.index('class="st"') < html.index('Room to save more') < html.index('Full ledger, history and method')
    assert 'Whole-workload comparison' in html and 'legacy-runway' not in html
    assert '<div class="label">Value</div>' not in html
    assert 'still on the table' not in html  # the large amber opportunity card is gone
    assert 'per 30 days' in html.split('Room to save more')[1][:60]


def test_template_without_inlined_module_falls_back():
    html = render_savings(full(), with_story=False)
    assert 'class="st"' not in html and 'legacy-runway' in html


def test_inliner(tmp_path):
    (tmp_path / 'a.js').write_text('function a(){return "</script>";}')
    out = dashboard_assets.inline('x /*@include a.js*/ y /*@include missing.js*/', tmp_path)
    assert 'function a()' in out and '<\\/script>' in out and '/*@include missing.js*/' in out
    tpl = TEMPLATE.read_text()
    assert '/*@include savings_story.js*/' in tpl
    once = dashboard_assets.inline(tpl, ASSETS)
    assert 'function savingsStoryHtml' in once
    assert '@include' not in STORY.read_text()  # inlining twice must be a no-op
    assert dashboard_assets.inline(once, ASSETS) == once
