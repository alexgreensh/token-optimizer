"""U9: run the real savings-story renderer (assets/savings_story.js) in Node.

Synthetic payloads only. Covers: old payloads render nothing and never throw;
one hero card whose total is the sum of its measured and estimated rows, led
by two big numbers; no percentage anywhere above the folds (per-session shares,
split by where sessions ran, live in the hero's fold); one 2x2 block; one coverage line; unfinished
pools say "Measuring" with no number; the Savings tab folds everything else
into one row; context quality moves to Health; the Tokens Saved card shows no
share against the fresh-only spent basis; the generator inlines the module.
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
       "c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));\n"
       "const fn = n => String(Math.round(Number(n) || 0));\n")


def _node(script, payload):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required to execute the dashboard renderer')
    r = subprocess.run([node, '-', json.dumps(payload)], input=script,
                       capture_output=True, text=True, check=True)
    return r.stdout


def call(expr, data):
    script = ESC + STORY.read_text() + '\nconst data = JSON.parse(process.argv[2]);\nprocess.stdout.write(' + expr + ');'
    return _node(script, data)


def story(data):
    return call('savingsStoryHtml(data)', data)


def _harness(fn_start, fn_end, call_js, data, with_story=True):
    html = TEMPLATE.read_text()
    body = html[html.index(fn_start):html.index(fn_end)]
    helper = html[html.index('function tokenSplitText('):html.index('function hostRefreshHtml(')]
    if with_story:
        helper = dashboard_assets.inline(helper, ASSETS)
    prefix = ('const data = JSON.parse(process.argv[2]);\n'
              "const el = {innerHTML: '', querySelectorAll: () => []};\n"
              'const document = {getElementById: () => el};\n' + ESC +
              "const runwayCardHtml = () => '<div id=\"legacy-runway\"></div>';\n"
              "const tokensSavedCardHtml = () => '';\nconst tcliHtml = x => x;\n")
    return _node(prefix + helper + body + '\n' + call_js, data)


def render_savings(savings, with_story=True, extra=None):
    data = dict(extra or {}, savings=savings)
    return _harness('  function renderSavings()', '  function renderHealth()',
                    'renderSavings(); process.stdout.write(el.innerHTML);', data, with_story)


def test_first_call_chart_renders_in_savings_view():
    context = {'months': [{'month': '2026-11', 'median': 1000, 'p25': 800, 'p75': 1200,
                           'n': 2, 'components': {'skills': {'median': 50, 'observed': 2}}}],
               'trim_dates': ['2026-11-03']}
    html = render_savings({}, extra={'trends': {'first_call_context': context}})
    assert 'Context before your first message' in html
    assert 'Skills listing: 50 estimated tokens (2/2 observed)' in html
    assert '2026-11-03' in html and '<svg' in html


def effect():
    return {
        'available': True,
        'window': {'start': '2026-01-01T00:00:00', 'end': '2026-01-31T00:00:00'},
        'per_session': {'sessions': 120, 'median': 0.05, 'p75': 0.11, 'p90': 0.2,
                        'net_median': 0.048, 'net_p75': 0.105, 'net_max': 0.25,
                        'by_place': {'local': {'sessions': 100, 'net_median': 0.031, 'net_p75': 0.09, 'best': {'net_max': 0.25, 'filtered': False}},
                                     'cloud': {'sessions': 20, 'net_median': 0.22, 'net_p75': 0.4}}},
        'coverage': {'sessions': 200, 'ran_sessions': 150, 'ran_traffic_share': 0.75,
                     'ran_no_ledger_sessions': 12, 'unknown_sessions': 0,
                     'classes': {'ledger': {'sessions': 138, 'traffic_share': 0.6}}},
        'fleet': {'cost_share': 0.02, 'cost_share_net': 0.019},
        'overhead': {'usd': 1.5, 'reread_tokens': 4000000},
    }


def base():
    """Fields that exist today (U4 + U1 + U8); U3, U5 and U7 not landed."""
    return {
        'period_days': 30,
        'session_effect': effect(),
        'action_savings': {'logged_usd': 40.2, 'reread_usd': 30.4, 'estimated_usd': 12.0,
                           'class_premium_usd': 8.4, 'routing_usd': 50.0,
                           'estimates_usd': {'resume_lean_estimated': 5.0}},
        'token_headline': {'context_removed_tokens': 2000000, 'output_not_generated_tokens': 0,
                           'reload_avoided_tokens': 1000000, 'reread_tokens': 80000000,
                           'reread_available': True},
        'counted_period': {'removed_tokens': 2000000, 'reread_tokens': 80000000},
        'structural_detail': {'cost_saved_usd': 0, 'tokens_saved': 0, 'evidence': 'measured'},
        'model_routing': {'baseline_opus_share': 0.95, 'baseline_source': 'pretool_baseline',
                          'current_opus_share': 0.9, 'actual_cost_usd': 900.0, 'realized_cost_usd': 50.0},
        'structural_potential': {'cost_saved_usd': 11.0, 'unused_skills': 3},
        'since_install': {'days': 90, 'install_date': '2025-11-01',
                          'all_in': {'components_usd': {'logged': 100.0, 'reread': 200.0, 'class_premium': 10.0,
                                                        'routing': 999.0}, 'estimated_usd': 40.0}},
    }


def full():
    s = base()
    s['action_savings']['logged_usd'] = 44.2
    s['structural_detail'] = {'method': 'trim_ledger', 'cost_saved_usd': 4.0, 'tokens_saved': 500000,
                              'estimated': {'cost_saved_usd': 20.0, 'tokens_saved': 90000000},
                              'trims': [{'label': 'Archived skills', 'ts': '2026-01-20T00:00:00+00:00',
                                         'source': 'audit_action', 'tokens_per_call': 1000,
                                         'requests': 500, 'cost_saved_usd': 4.0, 'projected_30d_usd': 9.0}]}
    s['output_reread'] = {'available': True, 'cost_saved_usd': 3.0, 'tokens_saved': 7000000, 'control_net': None}
    s['model_routing'].update(measured_baseline_opus_share=0.8, measured_baseline_realized_cost_usd=0.0)
    s['lean_resume_carry'] = {'available': True, 'carry_tokens': 1000000, 'carry_cost_usd': 77.0}
    return s


def test_setup_card_does_not_claim_unattributed_trim_is_in_total():
    s = full()
    s['structural_detail']['trims'].append({'label': 'Unknown origin', 'tier': 'unattributed',
        'ts': '2026-02-01', 'tokens_per_call': 100, 'requests': 10, 'cost_saved_usd': 99})
    assert 'Unknown origin' not in story({'savings': s})


def test_stated_routing_is_not_described_as_excluded_or_measuring():
    s = full()
    s['since_install']['all_in']['components_usd']['routing_estimated'] = 3484
    s['since_install']['excluded_usd'] = {'routing': None, 'lean_resumes': 12, 'verbosity': 2}
    html = story({'savings': s})
    assert 'routing Measuring' not in html
    assert 'Excluded since install: routing' not in html
    assert 'unverified lean-resume proxy' in html


def hero(out):
    return re.split(r'class="card st-table"|class="st-grid"', out.split('class="card st-hero"')[1])[0]


def table(out):
    return out.split('class="card st-table"')[1].split('class="st-grid"')[0]


def routed():
    s = full()
    s['since_install']['all_in']['components_usd']['routing_estimated'] = 3484
    s['since_install']['all_in']['routing_baseline_opus_share'] = 0.95
    s['model_routing'].update(potential_cost_usd=61.4, routable_fraction=0.2, target_model='claude-sonnet-5-5')
    return s


@pytest.mark.parametrize('savings', [
    {}, {'session_effect': None}, {'session_effect': {'available': False, 'reason': 'codex'}},
    {'session_effect': {'available': True}},
    {'session_effect': {'available': True, 'per_session': 'junk', 'coverage': 7}, 'action_savings': None,
     'structural_detail': {'method': 'trim_ledger', 'trims': [None, {}]}, 'model_routing': []},
])
def test_old_or_malformed_payloads_never_throw(savings):
    for fn in ('savingsStoryHtml(data)', 'healthNotesHtml(data)', 'savingsCoverageFold(data)'):
        out = call(fn, {'savings': savings})
        assert 'NaN' not in out and 'undefined' not in out


def test_old_payload_returns_nothing_and_tab_keeps_previous_layout():
    assert story({'savings': {'period_days': 30}}) == ''
    html = render_savings({'period_days': 30, 'total_cost_usd': 5})
    assert 'legacy-runway' in html and 'class="st"' not in html
    assert '<div class="label">Value</div>' in html


def test_hero_total_is_sum_of_bands_in_whole_dollars_and_excludes_routing():
    h = hero(story({'savings': full()}))
    # The action total is 83 measured + 12 estimated; diagnostic pools are excluded.
    assert '$95' in h and 'Measured <span class="st-v">$83' in h and 'Estimated <span class="st-v">$12' in h
    assert not re.search(r'\$[\d,]+\.\d{2}', _visible(h))
    assert '$50' not in h  # routing never enters the total
    assert 'Context removed' in h and 'Write-class' not in h and 'ratio' not in h


def _visible(html):
    """Text outside every fold: what shows before anything is opened."""
    return re.sub(r'<[^>]+>', ' ', re.sub(r'(?s)<details.*?</details>', '', html))


def test_session_shares_above_fold_and_place_split_in_fold():
    out = story({'savings': full()})
    h = hero(out)
    assert 'st-mega st-mega--usd">$95</span>' in h and 'st-mega st-mega--tok">' in h
    assert 'tokens kept out of context' in h
    assert 'On this Mac: typical 3.1%, top quarter 9%+, best 25%' in _visible(h)
    assert 'Cloud: typical 22%' in _visible(h)
    fold = h.split('<details')[1]
    assert "share of that session&rsquo;s own context" in fold
    assert 'On this Mac: typical 3.1%, top quarter 9% or more (100 sessions)' in fold
    assert 'Cloud sessions: typical 22%, top quarter 40% or more (20 sessions)' in fold


def test_split_falls_back_to_all_sessions_without_by_place():
    s = full()
    del s['session_effect']['per_session']['by_place']
    fold = hero(story({'savings': s})).split('<details')[1]
    assert 'All sessions: typical 4.8%' in fold and 'On this Mac' not in fold


def test_unavailable_reread_price_is_not_claimed_as_part_of_total():
    s = full()
    s['action_savings']['reread_available'] = False
    out = story({'savings': s})
    assert 'Removed once, saved on every later turn' in out
    assert 'came from re-reads avoided' not in out


def test_since_install_fold_names_exclusions_from_payload():
    s = full()
    s['since_install']['excluded_usd'] = {'routing': 101, 'lean_resumes': 20, 'verbosity': 9}
    out = story({'savings': s})
    assert 'Excluded since install: routing $101, unverified lean-resume proxy $20, and verbosity $9.' in out


def test_local_best_filter_is_explained_in_fold():
    s = full()
    s['session_effect']['per_session']['by_place']['local']['best']['filtered'] = True
    out = story({'savings': s})
    assert 'selected among sessions with at least 20 turns' in out


def test_no_forbidden_wording_or_eyebrows():
    out = story({'savings': full()}) + call('healthNotesHtml(data)', {'savings': full(), 'trends': {
        'quality_trend': [{'avg_quality': 56, 'sessions': 10}] * 8}})
    low = out.lower()
    assert '$0 on subscription' not in low and 'fresh' not in low
    assert '<em' not in out and 'italic' not in low
    assert 'class="label"' not in out and 'holding steady' not in low
    assert 'Diagnostic, not counted' in out


def test_savings_tab_one_row_of_folds_and_quality_moves_to_health():
    s = full(); s['total_cost_usd'] = 40.0
    s['before_after'] = {'monthly_savings_usd': 300, 'actual_monthly_usd': 1000, 'counterfactual_monthly_usd': 1300}
    extra = {'trends': {'quality_trend': [{'avg_quality': 60, 'sessions': 5}] * 6}}
    html = render_savings(s, extra=extra)
    row = html.split('class="st-morerow"')[1]
    assert html.index('class="st"') < html.index('class="st-morerow"')
    assert re.findall(r'<summary>([^<]+)', row)[:3] == ['Room to save more', 'Where your sessions ran', 'Full ledger and history']
    assert 'Whole-workload' not in html and 'Context quality' not in html and 'legacy-runway' not in html
    assert '<div class="label">Value</div>' not in html and 'still on the table' not in html
    assert 'None of this is in any total above' in row
    health = call('healthNotesHtml(data)', dict(extra, savings=s))
    assert 'Context quality' in health and 'scored <span class="st-num">60</span> out of 100' in health


def test_template_without_inlined_module_falls_back():
    html = render_savings(full(), with_story=False)
    assert 'class="st"' not in html and 'legacy-runway' in html


def test_tokens_saved_card_has_no_fresh_only_share():
    """Guard: trends.total_tokens is fresh input + output (no cache); saved tokens
    include cache-class reloads. No surface may divide one by the other."""
    tpl = TEMPLATE.read_text()
    a, b = tpl.index('  function tokensSavedCardHtml('), tpl.index('  function renderTrends()')
    card_src = tpl[a:b]
    mix = tpl[tpl.index('  function modelMixBodyHtml('):a]
    data = {'runtime': 'claude', 'trends': {'total_tokens': 80000000, 'period_days': 30,
                                            'model_mix': {'opus': 60000000, 'sonnet': 20000000}},
            'savings': dict(base(), total_tokens=1000000, lean_resume_carry={'carry_tokens': 999999999, 'carry_cost_usd': 100})}
    prefix = 'const data = JSON.parse(process.argv[2]);\n' + ESC + STORY.read_text()
    out = _node(prefix + mix + card_src + '\nprocess.stdout.write(tokensSavedCardHtml(data.savings));', data)
    expected = call('savingsStoryFmt().tok(savingsStoryBands(data).totalTok)', data)
    assert expected in out and expected in story(data)
    assert 'Same figure as the Savings card' in out and call('savingsStoryFmt().tok(data.savings.token_headline.removed_tokens)', data) in out
    assert '999999999' not in out and 'measured + estimated' not in out
    assert '%' not in out.split('model-bar')[0]  # no share before the model-mix bar
    for banned in ('prevented', 'would otherwise', 'without Token Optimizer', 'Spent vs Saved', 'class="label"',
                   'Consider defaulting', 'defaulting agents'):
        assert banned not in out
    # Static: no surface in the template builds a share from the fresh-only spent basis.
    assert not re.search(r'tsSpentTokens\s*\+\s*tsFullSaved', tpl)
    assert not re.search(r'/\s*tsWouldSendTotal|tsSavedPct', tpl)
    assert 'spent_token_basis' not in STORY.read_text() and 'trends.total_tokens' not in STORY.read_text()


def test_trends_does_not_invent_unused_skill_context_cost():
    source = TEMPLATE.read_text()
    assert 'Each still loads ~100 tokens' not in source
    assert 'neverUsed.length * 100' not in source
    assert 'Context impact: Measuring' in source
    assert 'Deduplicated Fresh Input + Output' in source
    assert 'Deduplicated usage; the raw token classes below use a different basis.' in source


def test_inliner(tmp_path):
    (tmp_path / 'a.js').write_text('function a(){return "</script>";}')
    out = dashboard_assets.inline('x /*@include a.js*/ y /*@include missing.js*/', tmp_path)
    assert 'function a()' in out and '<\\/script>' in out and '/*@include missing.js*/' in out
    tpl = TEMPLATE.read_text()
    assert '/*@include savings_story.js*/' in tpl
    once = dashboard_assets.inline(tpl, ASSETS)
    assert 'function savingsStoryHtml' in once
    assert '@include' not in STORY.read_text()
    assert dashboard_assets.inline(once, ASSETS) == once


def test_ledger_fold_matches_hero_and_never_adds_routing():
    s = full()
    s['since_install'] = {'install_date': '2025-12-01', 'days': 60, 'all_in': {
        'components_usd': {'logged': 100.0, 'class_premium': 10.0, 'reread': 200.0,
                           'routing_estimated': 999.0, 'routing_measured': 0.0},
        'estimated_usd': 40.0, 'total_usd': 1349.0}}
    out = call('savingsLedgerHtml(data)', {'savings': s})
    assert '$95.00' in out  # same total as the hero (83 + 12), to the cent
    assert '$350.00' in out and '$999.00' in out
    assert '$1,349.00' not in out
    assert out.index('$350.00') < out.index('Estimated routing') < out.index('$999.00')
    assert 'class="label"' not in out
    html = render_savings(s)
    ledger = html.split('Full ledger and history')[1]
    assert 'savings-actions' not in ledger and 'savings-history' not in ledger  # contradicting cards are gone


def test_reread_row_rounds_independently_and_matches_card():
    s = full()
    s['action_savings'].update(logged_usd=21.39, class_premium_usd=12.28,
                               reread_usd=84.01, estimated_usd=5.31)
    s['structural_detail'].update(cost_saved_usd=0.90,
                                  estimated={'cost_saved_usd': 2.29})
    s['counted_period'].update(removed_tokens=1000, reread_tokens=67000,
                               reread_usd=84.01)
    out = story({'savings': s})
    assert 'Re-reads avoided</span><span class="st-v">$84</span>' in out
    assert 'worth $84 at API rates' in out
    assert 'Re-reads avoided</span><span class="st-v">$85</span>' not in out


def test_every_measured_row_is_visible_in_hero_and_includes_premium():
    s = full()
    s['action_savings']['class_premium_usd'] = 7
    s['structural_detail'] = {'method': 'trim_ledger', 'cost_saved_usd': 3,
                              'tokens_saved': 100, 'trims': [{'tier': 'measured', 'tokens_per_call': 1}]}
    bands = json.loads(call('JSON.stringify(savingsStoryBands(data))', {'savings': s}))
    visible = hero(story({'savings': s})).split('<details')[0]
    for row in bands['measured']:
        assert row['label'] in visible
    assert sum(row['usd'] for row in bands['measured']) == pytest.approx(bands['mUsd'])
    assert next(row['usd'] for row in bands['measured'] if row['key'] == 'context') == s['action_savings']['logged_usd'] - 3 + 7


def test_setup_forecast_is_not_in_the_historical_total():
    s = full()
    s['structural_detail'] = {'method': 'trim_ledger', 'cost_saved_usd': 1,
                              'tokens_saved': 100, 'projected_30d': {'measured': {'usd': 36}},
                              'trims': [{'item': 'Memory', 'tier': 'measured', 'tokens_per_call': 2, 'cost_saved_usd': 1, 'projected_30d_usd': 36}]}
    out = story({'savings': s})
    assert 'Projected next 30 days' not in _visible(out)
    assert 'Estimated next 30 days at the recent rate: about $36' in out
    assert '$36' not in _visible(out)
    bands = json.loads(call('JSON.stringify(savingsStoryBands(data))', {'savings': s}))
    assert bands['mUsd'] != 36


def test_setup_card_prioritizes_valued_trims():
    s = full()
    s['structural_detail'] = {'method': 'trim_ledger', 'cost_saved_usd': 0.27,
                              'tokens_saved': 100, 'projected_30d': {'measured': {'usd': 36}},
                              'trims': [{'item': 'Unused listing', 'tier': 'estimated', 'tokens_per_call': 10, 'cost_saved_usd': 0, 'projected_30d_usd': 0},
                                        {'item': 'Memory', 'tier': 'measured', 'tokens_per_call': 10, 'cost_saved_usd': 0.27, 'projected_30d_usd': 36}]}
    setup = story({'savings': s}).split('<h2 class="st-h">Setup cleanup')[1].split('</div>')[0]
    assert 'Memory' in setup
    assert 'Unused listing' not in setup


def test_visible_dollars_are_whole_and_exact_values_remain_in_fold():
    s = full()
    s['action_savings'].update(logged_usd=31.4116, class_premium_usd=27.187941,
                               reread_usd=117.358087, estimated_usd=6.5324,
                               estimates_usd={})
    s['structural_detail'] = {'method': 'trim_ledger', 'cost_saved_usd': 0.266,
                              'tokens_saved': 100, 'projected_30d': {'measured': {'usd': 36.8432}},
                              'trims': [{'item': 'Memory', 'tier': 'measured', 'tokens_per_call': 2,
                                         'cost_saved_usd': 0.266, 'projected_30d_usd': 36.8432}]}
    s['output_reread'] = {}
    h = hero(story({'savings': s}))
    visible = _visible(h)
    assert '$182' in visible and '$176' in visible and 'under $1' in visible
    assert not re.search(r'\$[\d,]+\.\d{2}', visible)
    assert '$0.27' in h and '$117.36' in h
    assert 'Re-reads avoided</span><span class="st-v">$117' in h
    assert 'Context removed</span><span class="st-v">$58' in h
    assert 'Estimated <span class="st-v">$7' in h


def test_subdollar_band_does_not_display_zero():
    s = base()
    s['action_savings'].update(logged_usd=0.27, class_premium_usd=0,
                               reread_usd=0, estimated_usd=0, estimates_usd={})
    visible = _visible(hero(story({'savings': s})))
    assert 'Measured  under $1' in visible
    assert 'Measured  $0' not in visible


def test_fallback_never_shows_transformation_hero():
    savings = base()
    savings['before_after'] = {'monthly_savings_usd': 1000, 'actual_monthly_usd': 500, 'counterfactual_monthly_usd': 1500}
    out = render_savings(savings, with_story=False)
    assert 'class="card wide savings-transformation"' not in out


def test_archive_fold_shows_net_and_bash_debit_separately():
    s = full()
    s['by_category'] = {'tool_archive': {'tokens_saved': -2000, 'cost_saved_usd': -1.25},
                        'bash_compress_pipeline': {'tokens_saved': 5000, 'cost_saved_usd': 5},
                        'bash_archive_reexpand': {'tokens_saved': -3000, 'cost_saved_usd': -2}}
    out = story({'savings': s})
    assert 'Tool archive, net of re-expands' in out and '-$1' in out
    assert 'Bash compression, net of re-expands: $3' in out


def test_hero_matches_action_total_without_adding_diagnostic_pools():
    s = full()
    s['action_savings']['total_usd'] = 95.0
    s['output_reread']['cost_saved_usd'] = 300
    s['structural_detail']['estimated']['cost_saved_usd'] = 200
    h = hero(story({'savings': s}))
    assert 'st-mega st-mega--usd">$95</span>' in h
    assert '$595' not in h


# ---- Round 2 layout: hero band, Still on the table, routing card ----

def test_hero_band_leads_with_three_numbers_and_since_install_estimate_line():
    h = hero(story({'savings': routed()}))
    megas = re.findall(r'<span class="st-mega st-mega--(usd|tok)">([^<]+)</span>', h)
    # 30 days: 44.2 + 8.4 + 30.4 + 12 = 95; since install 100+200+10; tokens 1.5M+80M+0.5M+1M.
    assert megas == [('usd', '$95'), ('usd', '$310'), ('tok', '83.0M')]
    assert 'st-mega__row st-mega__row--hero' in h
    assert '<span class="st-mega__note">+ $40 estimated</span>' in h
    assert 'measured since Nov 1, 2025</span>' in h
    # Routing never joins the since-install figures.
    assert '$3,484' not in h and '$3,794' not in h and '$350' not in h


def test_saved_since_install_card_is_gone_and_its_fold_moves_to_hero():
    out = story({'savings': routed()})
    assert 'Saved since install' not in out
    assert 'How since-install is valued' in hero(out)
    assert 'st-routing-band' not in out


def test_grid_is_reread_routing_then_setup_then_coverage():
    out = story({'savings': routed()})
    grid = out.split('class="st-grid"')[1].split('class="st-cov"')[0]
    titles = re.findall(r'<h2 class="st-h">([^<]+)', grid)
    assert titles == ['$30 of your $95 came from re-reads avoided', 'Model routing ', 'Setup cleanup']
    assert 'st-mega--tok st-mega--card">40&times;</span>' in grid
    cov = out.split('class="st-cov"')[1]
    assert 'ran in 150 of your 200 sessions' in cov and '12 of them are not in the ledger yet' in cov


def test_routing_card_has_one_huge_estimated_number_and_short_explainer():
    s = routed()
    s['since_install']['all_in']['routing_majority_period'] = {'start': '2026-04', 'end': '2026-07'}
    s['since_install']['all_in']['routing_measured_baseline_opus_share'] = 0.8369
    s['since_install']['all_in']['routing_measured_baseline_realized_cost_usd'] = 0
    card = story({'savings': s}).split('class="card st-card st-card--route"')[1].split('</details></div>')[0]
    assert '<span class="st-tag">Estimated</span>' in card
    assert 'st-mega st-mega--route st-mega--card">$3,484</span>' in card
    assert '<p class="st-lead">At your stated 95% Opus baseline. Most of it from Apr to Jul.</p>' in card
    assert '83.69%' in card and 'never added to any savings total' in card
    assert 'Last 30 days: $50 of it.' in card
    assert 'Measuring' not in card


def test_no_routing_card_without_a_since_install_routing_estimate():
    out = story({'savings': full()})
    assert 'st-card--route' not in out
    assert 'Model routing <span class="st-tag">Estimated</span>' not in out


def test_still_on_the_table_sums_routing_and_skill_listing_in_warm_accent():
    s = routed()
    s['skill_listing_budget'] = {'available': True, 'period_days': 30, 'current_chars': 30000, 'baseline_chars': 6000,
                                 'baseline_month': '2026-04', 'growth_tokens': 6000, 'calls': 12000, 'cost_usd': 108.2}
    out = story({'savings': s})
    assert out.index('class="card st-hero"') < out.index('class="card st-table"') < out.index('class="st-grid"')
    t = table(out)
    assert 'st-mega st-mega--warm st-mega--card">$170</span>' in t and 'st-mega--usd' not in t
    assert re.findall(r'<li><span>([^<]+)<span class="st-sub">([^<]+)</span></span><span class="st-v">([^<]+)</span>', t) == [
        ('Model routing', 'Send routine work to Sonnet', '$61'), ('Skill listing', 'Trim the skill list budget', '$108')]
    assert '30,000 characters' in t and '6,000 in Apr 2026' in t and '12,000 calls' in t
    # Opportunity never joins a savings figure.
    assert '$170' not in hero(out) and 'savings total' in t


def test_unsized_skill_listing_says_measuring_and_stays_out_of_the_sum():
    s = routed()
    s['skill_listing_budget'] = {'available': False, 'reason': 'not enough skill listings in transcripts'}
    t = table(story({'savings': s}))
    assert 'st-mega--warm st-mega--card">$61</span>' in t
    assert '<span class="st-v">Measuring</span>' in t


def test_still_on_the_table_with_nothing_sized_shows_measuring_not_zero():
    s = full()
    s['skill_listing_budget'] = {'available': False, 'reason': 'no usage data yet'}
    t = table(story({'savings': s}))
    assert 'st-mega--word">Measuring</span>' in t and '$0' not in t


def test_still_on_the_table_absent_without_any_opportunity():
    s = full()
    s['skill_listing_budget'] = {'available': True, 'cost_usd': 0, 'growth_tokens': 0}
    assert 'st-table' not in story({'savings': s})


def test_room_fold_drops_routing_now_shown_on_the_table():
    s = routed(); s['total_cost_usd'] = 40.0
    s['model_routing']['potential_cost_usd'] = 61.4
    html = render_savings(s)
    row = html.split('class="st-morerow"')[1]
    assert 'Route routine work to lighter models' not in row
    assert 'Opus work ran on' not in row.split('Full ledger and history')[0]


@pytest.mark.parametrize('latest,shown', [(95, False), (91, False), (90, True), (72, True)])
def test_cost_per_message_renders_only_when_down_ten_percent(latest, shown):
    s = full()
    s['cost_per_message_trend'] = [{'month': '2026-03', 'index': 100}, {'month': '2026-07', 'index': 120},
                                   {'month': '2026-11', 'index': latest}]
    out = story({'savings': s})
    assert ('<polyline' in out) is shown
    if shown:
        assert 'Cost per message down ' + str(100 - latest) + '% since March</h2>' in out
        assert 'Start 100' in out and 'Latest ' + str(latest) in out and 'Mar' in out and 'Nov' in out
        assert 'not attributed to Token Optimizer' in out


def test_cost_per_message_rising_never_renders():
    s = full()
    s['cost_per_message_trend'] = [{'month': '2026-08', 'index': 100}, {'month': '2026-09', 'index': 120}]
    assert 'Cost per message' not in story({'savings': s})


def _trends_cache_card(cache_health, pop):
    data = {'trends': {'period_days': 30, 'session_count': 10, 'total_tokens': 1, 'total_messages': 1,
                       'population_metrics': pop, 'daily': []},
            'cache_health': cache_health, 'runtime': 'claude'}
    html = TEMPLATE.read_text()
    start = html.index("    var eligibleCacheHit = Number(pop.cache_eligible_main_hit_rate);")
    end = html.index("    var tokenBreakdownNote")
    prefix = ('const data = JSON.parse(process.argv[2]); const t = data.trends; const pop = t.population_metrics;\n'
              "const isCodex = false; const avgCacheHit = 'N/A';\n")
    return _node(prefix + html[start:end] + '\nprocess.stdout.write(JSON.stringify([String(avgCacheDisplay), cacheLabel, cacheSessionLines]));', data)


def test_trends_cache_card_leads_with_token_weighted_rate():
    pop = {'cache_eligible_main_sessions': 40, 'cache_eligible_main_hit_rate': 0.7712,
           'cache_ineligible_sessions': 9, 'cache_ineligible_hit_rate': 0.4321}
    big, label, lines = json.loads(_trends_cache_card({'overall_cache_hit_rate': 0.9733}, pop))
    assert big == '97.3' and label == 'Cache hit rate (all tokens)'
    assert 'Average session, 20+ messages: 77%</div>' in lines
    assert 'Average short or delegated session: 43%</div>' in lines
    assert 'cache-eligible' not in lines.lower() and 'cache-building' not in lines


def test_trends_cache_card_falls_back_without_cache_health():
    pop = {'cache_eligible_main_sessions': 40, 'cache_eligible_main_hit_rate': 0.7712}
    big, label, _ = json.loads(_trends_cache_card({}, pop))
    assert big == '77' and label == 'Main-work cache hit rate'


def test_unfinished_pools_say_measuring_with_no_number():
    out = story({'savings': base()})
    grid = out.split('class="st-grid"')[1].split('class="st-cov"')[0]
    setup = [c for c in grid.split('class="card st-card"')[1:] if '<h2 class="st-h">Setup cleanup' in c][0]
    assert 'Measuring' in setup and '$' not in setup
    assert 'Still measuring, not in the total yet: setup cleanup and shorter answers' in hero(out)


def test_subdollar_amounts_read_naturally():
    s = full()
    s['structural_detail']['trims'][0]['projected_30d_usd'] = 0.4
    s['action_savings']['estimates_usd']['expand_overhead'] = -0.6
    out = story({'savings': s})
    assert 'at the recent rate: under $1.' in out and 'about under' not in out
    assert 'overhead: under $1.' in out and '-under' not in out


def test_compounding_card_names_its_priced_removal_basis():
    grid = story({'savings': full()}).split('class="st-grid"')[1]
    assert '<strong>2.0M priced tokens removed</strong> became <strong>80.0M tokens not sent</strong>' in grid
