import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))
from _measure import ttl_period


def test_ttl_summary_reads_only_the_base_database_trends():
    class Facade:
        def _collect_trends_from_db(self, days):
            return {'daily': [{'day': days}]}

        def _collect_trends_from_jsonl(self, days):
            raise AssertionError('database data must not require a fallback')

    assert ttl_period.base_trends(Facade(), 30) == {'daily': [{'day': 30}]}


def test_ttl_summary_falls_back_only_when_database_has_no_result():
    class Facade:
        def _collect_trends_from_db(self, days):
            return None

        def _collect_trends_from_jsonl(self, days):
            return {'daily': [{'day': days}]}

    assert ttl_period.base_trends(Facade(), 7) == {'daily': [{'day': 7}]}


def test_ttl_card_counts_same_session_classes(monkeypatch):
    from _measure import dashboard
    class Facade:
        def _collect_trends_from_db(self, days):
            return {'daily': [{'session_details': [
                {'cache_create_1h_tokens': 20, 'cache_create_5m_tokens': 10},
                {'cache_create_5m_tokens': 10},
                {'cache_create_1h_tokens': 5},
                {'cache_create_5m_tokens': 5, 'continuation': True},
            ]}]}
    monkeypatch.setattr(dashboard._compat, 'facade', lambda: Facade())
    assert dashboard._build_ttl_period_summary(30) == {
        'label': '30d: 1 mixed, 1 5m-only', 'period_days': 30,
        'mixed_sessions': 1, 'five_only_sessions': 1, 'one_hour_only_sessions': 1,
    }
