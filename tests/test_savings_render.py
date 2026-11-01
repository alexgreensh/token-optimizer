"""Run the actual Savings renderer to verify totals and concise disclosures."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/assets/dashboard.html'


def render(savings):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required to execute the dashboard renderer')
    html = TEMPLATE.read_text()
    body = html[html.index('  function renderSavings()'):html.index('  function renderHealth()')]
    prefix = '''const data = JSON.parse(process.argv[2]);
const el = {innerHTML: '', querySelectorAll: () => []};
const document = {getElementById: () => el};
const esc = x => String(x == null ? '' : x).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const runwayCardHtml = () => '';
const tokensSavedCardHtml = () => '';
const isCodex = false;
const runtimeLabel = 'Claude Code';
'''
    script = prefix + body + '\nrenderSavings(); process.stdout.write(el.innerHTML);'
    # Windows limits CreateProcess command lines to about 32 KiB. Send the
    # extracted renderer over stdin so the test exercises the real code there.
    result = subprocess.run(
        [node, '-', json.dumps({'savings': savings})],
        input=script, capture_output=True, text=True, check=True,
    )
    return result.stdout


def fixture():
    return {
        'period_days': 30, 'total_cost_usd': 100,
        'since_install': {'days': 90},
        'model_routing': {'realized_cost_usd': 7},
        'behavioral_estimate': {'cost_saved_usd': 16},
        'resume_lean_estimated': {'cost_saved_usd': 100},
        'counted_period': {'available': True, 'oneshot_usd': 89, 'reread_usd': 954, 'total_usd': 1043},
        'counted_cumulative': {'total_usd': 2613, 'oneshot_usd': 262, 'reread_usd': 2351},
        'before_after': {'monthly_savings_usd': 2000, 'actual_monthly_usd': 13000,
                         'counterfactual_monthly_usd': 15000,
                         'before_cost_per_session': 10, 'after_cost_per_session': 2.5,
                         'session_weight_pool': {'anchor_month': '2026-06', 'now_units': 60000}},
    }


def test_action_hero_adds_rereads_once_and_keeps_other_logged_savings():
    html = render(fixture())
    actions = html.split('savings-actions')[1].split('savings-history')[0]
    assert '$1,177' in actions  # 100 + 7 + 954 + 16 + 100, not +89 again
    assert 'Logged actions <strong>$107' in actions
    assert 'Repeat reads avoided <strong>~$954' in actions
    assert 'Other estimates <strong>~$116' in actions
    assert 'smaller and exact' not in html


@pytest.mark.parametrize('counted', [None, {}, {'available': False}, {'available': True, 'reread_usd': 0}])
def test_missing_or_empty_rereads_preserves_logged_floor(counted):
    s = fixture(); s['counted_period'] = counted
    actions = render(s).split('savings-actions')[1].split('savings-history')[0]
    assert '$223' in actions
    assert '$2,613' not in actions  # never borrow lifetime value for an empty period


def test_young_install_uses_actual_totals_and_labels_estimates():
    s = fixture(); s['since_install']['days'] = 4
    actions = render(s).split('savings-actions')[1].split('savings-history')[0]
    assert '4 days tracked' in actions
    assert '$1,177' in actions
    assert 'logged + estimated' in actions
    assert '/mo' not in actions


def test_negative_reread_debits_are_not_clamped_away():
    s = fixture(); s['counted_period']['reread_usd'] = -23
    actions = render(s).split('savings-actions')[1].split('savings-history')[0]
    assert '$200' in actions


def test_transformation_percentage_matches_dollars_and_method_is_folded():
    html = render(fixture())
    card = html.split('savings-transformation')[1].split('savings-actions')[0]
    assert '~13% lower cost' in card  # 2000 / 15000, not the 75% per-session gap
    assert 'cheaper/session' not in card
    assert 'last 30 days' in card
    assert '<details class="fold"><summary>How this is estimated</summary>' in card
    assert '<details open' not in card
    visible = card.split('<details')[0]
    assert len(re.sub('<[^>]+>', ' ', visible).split()) < 85


def test_capped_transformation_percentage_matches_the_capped_dollars():
    s = fixture()
    s['before_after'].update({
        'monthly_savings_usd': 100,
        'uncapped_monthly_savings_usd': 900,
        'actual_monthly_usd': 100,
        'counterfactual_monthly_usd': 1000,
        'transformation_pct': 0.1,
        'savings_capped': True,
        'savings_cap_note': 'Capped at actual spend; uncapped estimate was $900.',
    })
    card = render(s).split('savings-transformation')[1].split('savings-actions')[0]
    assert '~10% lower cost' in card
    assert '~90% lower cost' not in card
    assert 'Capped at actual spend; uncapped estimate was $900.' in card


def test_history_compares_same_context_cohort_and_discloses_overlap():
    html = render(fixture()).split('savings-history')[1]
    assert 'Context savings &middot; all time' in html
    assert '$1,043' in html and '$2,613' in html
    assert 'Excludes routing, setup and other estimates' in html


def test_seven_day_card_does_not_project_thirty_day_value():
    s = fixture(); s['period_days'] = 7
    actions = render(s).split('savings-actions')[1].split('savings-history')[0]
    assert '$1,177' in actions
    assert '$5,044' not in actions
    assert '7 days tracked' in actions


def test_verbosity_estimate_is_included_once():
    s = fixture(); s['verbosity_steer'] = {'cost_saved_usd': 10, 'events': 2}
    actions = render(s).split('savings-actions')[1].split('savings-history')[0]
    assert '$1,187' in actions
    assert 'Other estimates <strong>~$126' in actions


def test_repeat_read_pricing_note_shows_once_beside_its_figure():
    # The note explains the repeat-read dollar figure, so it sits in the action
    # savings card only, not in the plan card above it.
    source = TEMPLATE.read_text()
    assert "counted_pricing.reread_pricing_note" not in source
    assert "cp.reread_pricing_note" in source


def test_flat_transformation_shows_plain_no_change_card():
    s = fixture()
    s['before_after'] = {'monthly_savings_usd': 0, 'reason': 'flat', 'transformation_state': 'flat',
                         'actual_monthly_usd': 27767.09, 'counterfactual_monthly_usd': 27820.04}
    html = render(s)
    assert 'No measurable change in cost per unit of work' in html
    assert 'disagree' not in html.lower()
