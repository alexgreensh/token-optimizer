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


def test_negative_trailing_net_pauses_even_for_abandoned_pings():
    from _measure.keepwarm_economics import trailing_net_negative

    class Facade:
        @staticmethod
        def keepwarm_spend_summary(days):
            assert days == 30
            return {'pings': 1, 'net_usd': -0.12}

    assert trailing_net_negative(Facade)
