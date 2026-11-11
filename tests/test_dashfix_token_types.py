import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))
from _measure import token_type_split


def test_known_classes_and_unknowns_are_kept_separate():
    detail = {'version': 6, 'reread_miss_turns': 1, 'oneshot_class_events': 1,
              'priced_oneshot_events': 1}
    rows = [
        {'tokens': 100, 'reread_tokens': 300, 'turns_counted': 3, 'pricing_detail': json.dumps(detail)},
        {'tokens': -20, 'reread_tokens': -40, 'turns_counted': 2, 'pricing_detail': json.dumps(detail)},
        {'tokens': 500, 'reread_tokens': 500, 'turns_counted': 1, 'pricing_detail': '{}'},
    ]
    result = token_type_split.from_rows(rows, 6)
    assert result == {'cache_reread': 180, 'cache_write': 160}
    assert 'fresh' not in result


def test_missing_or_inconsistent_evidence_is_omitted():
    assert token_type_split.from_rows([{'pricing_detail': '{}'}], 6) == {}
    detail = json.dumps({'version': 6, 'reread_miss_turns': 3})
    row = {'tokens': 10, 'reread_tokens': 20, 'turns_counted': 2, 'pricing_detail': detail}
    assert token_type_split.from_rows([row], 6) == {}


def test_incomplete_refresh_is_not_presented_as_classified():
    detail = {'version': 6, 'refresh_incomplete': True, 'reread_miss_turns': 1,
              'oneshot_class_events': 1, 'priced_oneshot_events': 1}
    row = {'tokens': 10, 'reread_tokens': 20, 'turns_counted': 2,
           'pricing_detail': json.dumps(detail)}
    assert token_type_split.from_rows([row], 6) == {}


def test_method_discloses_partial_classification():
    from test_savings_story_render import full, story
    savings = full()
    savings['token_type_breakdown'] = {'cache_write': 42}
    html = story({'savings': savings})
    assert 'Token types cover only ledger events with recorded turn classes' in html
    assert 'Unclassified tokens are excluded' in html
