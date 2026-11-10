from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))
from _measure.session_best import verified_best


class Facade:
    def __init__(self, sizes):
        self.sizes = sizes

    def _counted_session_path(self, conn, session_id):
        return session_id if session_id in self.sizes else None

    def _counted_walk_transcript(self, path):
        count, context = self.sizes[path]
        return ([(None, None, None, (context, 0, 0, 0))] * count, [], [])


def test_best_uses_net_rank_when_not_small():
    best = verified_best(None, Facade({'a': (25, 60000), 'b': (50, 90000)}), [(0.6, 'a'), (0.5, 'b')])
    assert best['net_max'] == 0.6 and not best['filtered']


def test_small_best_uses_best_session_with_twenty_turns():
    best = verified_best(None, Facade({'a': (5, 80000), 'b': (25, 60000)}), [(0.9, 'a'), (0.5, 'b')])
    assert best['net_max'] == 0.5 and best['filtered']


def test_unverifiable_higher_session_never_claims_lower_one_is_best():
    assert verified_best(None, Facade({'b': (25, 60000)}), [(0.9, 'a'), (0.5, 'b')]) is None


def test_low_context_still_discloses_twenty_turn_filter():
    best = verified_best(None, Facade({'a': (25, 30000)}), [(0.8, 'a')])
    assert best['net_max'] == 0.8 and best['filtered']
