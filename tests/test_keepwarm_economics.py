"""Keep-warm must learn return odds without looking into future activity."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))


def test_return_probability_counts_abandoned_tails_and_requires_history():
    from _measure.keepwarm_economics import return_probability
    now = 100000
    assert return_probability([], now, 100, 100) == 0
    sessions = [[1000, 1150] for _ in range(20)] + [[1000] for _ in range(20)]
    assert return_probability(sessions, now, 100, 100) == 20 / 70


def test_return_probability_excludes_future_gaps_and_shorter_pauses():
    from _measure.keepwarm_economics import return_probability
    sessions = [[1000, 1050], [1000, 1150], [99950, 100100]]
    assert return_probability(sessions, 100000, 100, 100, minimum=1, prior=0) == 1 / 3


def test_history_uses_local_main_session_turns():
    from _measure.keepwarm_economics import load_history

    class Facade:
        @staticmethod
        def _find_all_jsonl_files(days):
            assert days == 30
            return [('session-a', 1, 'project')]

        @staticmethod
        def parse_session_turns(path):
            assert path == 'session-a'
            return [{'timestamp': '1'}, {'timestamp': '2'}]

        _keepwarm_iso_to_epoch = staticmethod(float)

    history = load_history(Facade, None, [{'session_id': 'active'}])
    assert history['gap_sessions'].sessions == [[1.0, 2.0]]


def test_indexed_history_matches_observations_at_each_cutoff():
    from _measure.keepwarm_economics import GapHistory, return_probability
    sessions = [[1000, 1150, 5000], [1300], [99950, 100100]] * 12
    index = GapHistory(sessions)
    for now in (1500, 10000, 100000):
        assert return_probability(index, now, 100, 100) == return_probability(sessions, now, 100, 100)


def test_negative_trailing_net_pauses_even_for_abandoned_pings(monkeypatch):
    from _measure import keepwarm_economics

    class Facade:
        class time:
            time = staticmethod(lambda: 100000)

    def summary(facade, days, now):
            assert days == 30
            return {'pings': 1, 'net_usd': -0.12}

    monkeypatch.setattr(keepwarm_economics, 'verified_summary', summary)
    assert keepwarm_economics.gate_reason(Facade) == 'negative-net'


def test_headline_includes_cost_of_pings_with_no_return():
    from _measure.keepwarm_economics import headline_pool

    class Facade:
        class time:
            time = staticmethod(lambda: 100000)
        class Conn:
            def execute(self, query):
                return [('verified',)]
            def close(self):
                pass
        _open_trends_db_readonly = staticmethod(lambda timeout: Facade.Conn())
        _keepwarm_load_ledger_rows = staticmethod(lambda: [
            {'status': 'realized', 'pause_key': 'verified'},
            {'status': 'realized', 'pause_key': 'stranded'},
            {'status': 'ok'}])
        _keepwarm_iso_to_epoch = staticmethod(lambda value: 90000)
        @staticmethod
        def keepwarm_spend_summary(days, now, rows):
            assert len(rows) == 2
            return {'pings': 2, 'realized_count': 1, 'net_usd': -0.08}

    result = headline_pool(Facade, 'window-start')
    assert result['cost_saved_usd'] == -0.08
    assert result['events'] == 3
