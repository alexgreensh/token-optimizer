import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))
from _measure import next_actions


class NextActionsTest(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parents[1] / 'out' / '.tmp'
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(self.temp.cleanup)
        self.f = SimpleNamespace(SNAPSHOT_DIR=Path(self.temp.name))
        self.summary = {'period_days': 30, 'skill_listing_budget': {'available': True, 'cost_usd': 12},
                        'output_waste': {'cost_saved_usd': 30}, 'stale_reads_reclaimable': {'cost_usd': 20},
                        'model_routing': {'potential_cost_usd': 10}}
        self.f._get_merged_savings = lambda days: self.summary

    def test_order_decline_and_restore(self):
        result = next_actions.build(self.f, show_all=True)
        self.assertEqual([x['id'] for x in result['actions']], ['skill_list', 'writes', 'stale_reads', 'routing'])
        self.assertEqual(next_actions.main(self.f, ['decline', 'writes']), 0)
        self.assertNotIn('writes', [x['id'] for x in next_actions.build(self.f)['actions']])
        self.assertEqual(next_actions.main(self.f, ['restore', 'writes']), 0)
        self.assertIn('writes', [x['id'] for x in next_actions.build(self.f)['actions']])
        self.assertNotEqual(next_actions.main(self.f, ['decline', 'invalid']), 0)

    def test_refresh_reuses_fresh_plan_and_rebuilds_expired_plan(self):
        from _measure import apply_safe
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        plan = {'items': [], 'total_net_usd_month': 0}
        with patch.object(apply_safe, 'build_plan', return_value=plan) as build:
            with patch.object(next_actions, '_now', return_value=now):
                next_actions.on_savings_refresh(self.f, self.summary)
                next_actions.on_savings_refresh(self.f, self.summary)
            self.assertEqual(build.call_count, 1)
            with patch.object(next_actions, '_now', return_value=now + timedelta(hours=2)):
                next_actions.on_savings_refresh(self.f, self.summary)
            self.assertEqual(build.call_count, 2)

    def test_read_only_and_routing_off(self):
        before = {p.name: p.read_bytes() for p in Path(self.temp.name).iterdir()}
        with patch.dict(os.environ, {'TOKEN_OPTIMIZER_ROUTING': '0'}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(next_actions.main(self.f, ['--json']), 0)
            self.assertIn({'id': 'routing', 'why': 'routing-off'}, next_actions.build(self.f)['hidden'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in Path(self.temp.name).iterdir()})

    def test_cli_missing_cache_prints_stored_total_and_preparing(self):
        from _measure import apply_safe, counted_headline

        headline = {'display': {'short': 'Saved $42 since install'}}
        out = io.StringIO()
        self.f._get_merged_savings = lambda days: self.fail('CLI must not compute a summary')
        with patch.object(counted_headline, 'build_overall', return_value=headline), \
             patch.object(apply_safe, 'build_plan', side_effect=AssertionError('CLI built a plan')), \
             contextlib.redirect_stdout(out):
            self.assertEqual(next_actions.main(self.f, []), 0)
        self.assertEqual(out.getvalue().splitlines()[0], headline['display']['short'])
        self.assertIn('The full list is being prepared', out.getvalue())
        self.assertNotIn('Nothing to do', out.getvalue())

    def test_apply_line_covers_skill_and_cold_savings_once(self):
        from _measure import apply_safe, cold_guard

        plan = {'items': [{'kind': 'unused_skill'}, {'kind': 'keepwarm_auto'}],
                'total_net_usd_month': 11}
        with patch.object(apply_safe, 'build_plan', return_value=plan), \
             patch.object(cold_guard, 'pool', return_value={'cost_saved_usd': 50}):
            result = next_actions.build(self.f, show_all=True)
        self.assertEqual([action['id'] for action in result['actions']],
                         ['apply_safe', 'writes', 'stale_reads', 'routing'])
        self.assertIn('/token-optimizer:apply-safe', result['actions'][0]['how'])

    def test_cli_without_full_report_can_show_a_ready_apply_plan(self):
        from _measure import apply_safe

        plan = {'items': [{'kind': 'cold_guard'}], 'total_net_usd_month': 0}
        (self.f.SNAPSHOT_DIR / next_actions.PLAN_NAME).write_text(json.dumps({
            'computed_at': next_actions._now().isoformat(), 'plan': plan,
        }))
        out = io.StringIO()
        with patch.object(apply_safe, 'build_plan', side_effect=AssertionError('CLI built a plan')), \
             contextlib.redirect_stdout(out):
            self.assertEqual(next_actions.main(self.f, []), 0)
        self.assertIn('Apply safe savings (1 change, one approval)', out.getvalue())
        self.assertIn('Run /token-optimizer:apply-safe.', out.getvalue())
        self.assertIn('The full list is being prepared', out.getvalue())

    def test_cached_cli_honors_current_routing_decline_and_prints_as_of(self):
        from _measure import apply_safe, cold_guard

        with patch.object(apply_safe, 'build_plan', return_value={'items': []}), \
             patch.object(cold_guard, 'pool', return_value={}):
            self.assertEqual(next_actions.main(self.f, ['--refresh-cache']), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(next_actions.main(self.f, ['decline', 'routing']), 0)
        out = io.StringIO()
        with patch.object(next_actions, '_compute_summary', side_effect=AssertionError('CLI repriced summary')), \
             contextlib.redirect_stdout(out):
            self.assertEqual(next_actions.main(self.f, ['--all', '--json']), 0)
        result = json.loads(out.getvalue())
        self.assertTrue(result['cached'])
        self.assertTrue(result['as_of'])
        self.assertFalse(result['preparing'])
        self.assertIn({'id': 'routing', 'why': 'declined'}, result['hidden'])
        self.assertNotIn('routing', [action['id'] for action in result['actions']])

    @unittest.skipIf(os.name == 'nt', 'POSIX lock handoff')
    def test_detached_worker_keeps_lock_after_parent_handoff(self):
        script = self.f.SNAPSHOT_DIR / 'worker.py'
        ready = self.f.SNAPSHOT_DIR / 'worker.ready'
        release = self.f.SNAPSHOT_DIR / 'worker.release'
        script.write_text('import time\nfrom pathlib import Path\n'
                          f'Path({str(ready)!r}).touch()\n'
                          'deadline = time.monotonic() + 10\n'
                          f'while not Path({str(release)!r}).exists() and time.monotonic() < deadline:\n'
                          '    time.sleep(0.01)\n')
        self.f.__file__ = str(script)
        children = []
        spawn = next_actions.spawn_detached

        def track_spawn(*args, **kwargs):
            child = spawn(*args, **kwargs)
            children.append(child)
            return child

        try:
            with patch.object(next_actions, 'spawn_detached', side_effect=track_spawn):
                next_actions._start_refresh(self.f)
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                next_actions._start_refresh(self.f)
                self.assertEqual(len(children), 1)
        finally:
            release.touch()
            for child in children:
                if child is not None:
                    self.assertEqual(child.wait(timeout=5), 0)

    def test_status_threshold_and_no_nudges(self):
        self.summary['skill_listing_budget']['cost_usd'] = 4.99
        self.summary['nudge'] = 'MODEL FACING SECRET NUDGE'
        next_actions.on_savings_refresh(self.f, self.summary)
        self.assertEqual(json.loads((self.f.SNAPSHOT_DIR / 'next_action.json').read_text()), {})
        self.summary['skill_listing_budget']['cost_usd'] = 5
        next_actions.on_savings_refresh(self.f, self.summary)
        action = next_actions.status_action(self.f, 'test')
        self.assertEqual(action['id'], 'skill_list')
        self.assertLessEqual(len(action['short']), 40)
        self.assertNotIn('NUDGE', json.dumps(action))

    def test_sources_overlap_and_order_limit(self):
        from _measure import apply_safe, cold_guard, counted_headline
        plan = {'items': [{'kind': 'cold_guard'}], 'total_net_usd_month': 41.2}
        with patch.object(apply_safe, 'build_plan', return_value=plan, create=True), \
             patch.object(cold_guard, 'pool', return_value={'cost_saved_usd': 596}, create=True), \
             patch.object(counted_headline, 'build_overall', return_value={'display': {'short': 'Saved $1,083 since Mar 2026'}}):
            result = next_actions.build(self.f, True)
            self.assertEqual([a['id'] for a in result['actions']], ['apply_safe', 'skill_list', 'writes', 'stale_reads', 'routing'])
            self.assertIn({'id': 'cold_fresh', 'why': 'overlap'}, result['hidden'])
            plan['items'] = []
            self.assertEqual(len(next_actions.build(self.f)['actions']), 5)
            self.assertEqual(len(next_actions.build(self.f, True)['actions']), 6)
            self.assertEqual(next_actions.build(self.f, True)['actions'][2]['id'], 'cold_fresh')
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                next_actions.main(self.f, [])
            self.assertNotIn('$699', out.getvalue())
            self.assertLessEqual(len(out.getvalue().splitlines()), 12)

    def test_sources_raise_and_missing(self):
        from _measure import apply_safe, cold_guard, counted_headline
        with patch.object(apply_safe, 'build_plan', side_effect=RuntimeError, create=True), \
             patch.object(cold_guard, 'pool', side_effect=RuntimeError, create=True), \
             patch.object(counted_headline, 'build_overall', side_effect=RuntimeError):
            self.assertEqual([x['id'] for x in next_actions.build(self.f, True)['actions']],
                             ['skill_list', 'writes', 'stale_reads', 'routing'])
            self.assertIsNone(next_actions.build(self.f)['counted'])

    def test_routing_marker_and_permissions(self):
        marker = self.f.SNAPSHOT_DIR / 'routing_removed_at'
        marker.write_text('2026-11-01')
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.f.SNAPSHOT_DIR.iterdir()}
        self.assertIn({'id': 'routing', 'why': 'routing-off'}, next_actions.build(self.f)['hidden'])
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.f.SNAPSHOT_DIR.iterdir()})
        next_actions.main(self.f, ['decline', 'skill_list'])
        self.assertEqual((self.f.SNAPSHOT_DIR / 'next_declined.json').stat().st_mode & 0o777, 0o600)

    def test_quiet_period_and_doubling(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch.object(next_actions, '_now', return_value=now):
            next_actions.on_savings_refresh(self.f, self.summary)
        with patch.object(next_actions, '_now', return_value=now + timedelta(days=7)):
            next_actions.on_savings_refresh(self.f, self.summary)
            self.assertIsNone(next_actions.status_action(self.f, 's'))
            self.summary['skill_listing_budget']['cost_usd'] = 24
            next_actions.on_savings_refresh(self.f, self.summary)
            self.assertEqual(next_actions.status_action(self.f, 's')['id'], 'skill_list')
        self.assertEqual((self.f.SNAPSHOT_DIR / 'next_action.json').stat().st_mode & 0o777, 0o600)

    def test_fresh_cache_used_and_old_cache_ignored(self):
        import time
        directory = self.f.SNAPSHOT_DIR / 'status-bar'
        directory.mkdir()
        self.f._status_bar_dir = lambda: directory
        path = directory / 'global.json'
        path.write_text(json.dumps({'computed_at': time.time(), 'savings': self.summary}))
        self.f._get_merged_savings = lambda days: self.fail('fresh cache must be reused')
        self.assertTrue(next_actions.build(self.f)['cached'])
        self.f._get_merged_savings = lambda days: {'period_days': 30, 'output_waste': {'cost_saved_usd': 99}}
        path.write_text(json.dumps({'computed_at': time.time() - 86401, 'savings': self.summary}))
        result = next_actions.build(self.f)
        self.assertNotIn('cached', result)
        self.assertEqual(result['actions'][0]['net_usd_month'], 99)

    def test_dashboard_embedded_summary_cache(self):
        path = self.f.SNAPSHOT_DIR / 'dashboard.html'
        self.f.DASHBOARD_PATH = path
        path.write_text('<script>window.__TOKEN_DATA__ = ' + json.dumps({'savings': self.summary}) + ';</script>')
        self.f._get_merged_savings = lambda days: self.fail('dashboard summary should be reused')
        result = next_actions.build(self.f)
        self.assertTrue(result['cached'])
        self.assertEqual(result['actions'][0]['id'], 'skill_list')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            next_actions.main(self.f, [])
        self.assertIn('as of ', out.getvalue())

    def test_upper_actions_never_reach_status_bar(self):
        self.summary['skill_listing_budget']['available'] = False
        next_actions.on_savings_refresh(self.f, self.summary)
        self.assertEqual(json.loads((self.f.SNAPSHOT_DIR / 'next_action.json').read_text()), {})
        self.assertIsNone(next_actions.status_action(self.f, 's'))

    def test_silence_ends_after_thirty_days(self):
        from datetime import datetime, timedelta, timezone
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for days, expected in ((0, True), (7, False), (36, False), (37, True)):
            with patch.object(next_actions, '_now', return_value=start + timedelta(days=days)):
                next_actions.on_savings_refresh(self.f, self.summary)
                self.assertEqual(next_actions.status_action(self.f, 's') is not None, expected)

    def test_fake_home_unchanged(self):
        home = self.f.SNAPSHOT_DIR / 'home'
        claude = home / '.claude'
        claude.mkdir(parents=True)
        (claude / 'settings.json').write_text('{"setting":true}')
        before = {p.relative_to(home): p.read_bytes() for p in home.rglob('*') if p.is_file()}
        with patch.dict(os.environ, {'HOME': str(home)}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(next_actions.main(self.f, ['--json']), 0)
        self.assertEqual(before, {p.relative_to(home): p.read_bytes() for p in home.rglob('*') if p.is_file()})


def test_refresh_seeds_the_saved_total_for_an_upgraded_install(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from _measure import next_actions as module
    calls = []
    results = {'build_overall': None, '_savings_since_install': {'all_in': {'total_usd': 9}},
               'build_plan': {}, 'pool': {}}

    def fake_call(name, function, *args):
        calls.append(function)
        if function == 'on_savings_refresh':
            calls.append(args[1]['since_install'])
        return results.get(function)

    monkeypatch.setattr(module, '_compute_summary', lambda facade: {})
    monkeypatch.setattr(module, '_call', fake_call)
    monkeypatch.setattr(module, '_report', lambda *args: {'schema': 1, 'candidates': []})
    assert module._refresh(SimpleNamespace(SNAPSHOT_DIR=tmp_path)) == 0
    assert {'all_in': {'total_usd': 9}} in calls

    calls.clear()
    results['build_overall'] = {'display': {'short': 'Saved $9 since install'}}
    assert module._refresh(SimpleNamespace(SNAPSHOT_DIR=tmp_path)) == 0
    assert 'on_savings_refresh' not in calls


@pytest.mark.parametrize('flag', ['routing_pending', 'remeasuring'])
@pytest.mark.parametrize('summary_has_since_install', [False, True])
def test_next_waits_for_a_complete_since_install_total(tmp_path, monkeypatch, flag, summary_has_since_install):
    import measure
    from _measure import apply_safe, cold_guard, counted_headline, savings

    db = tmp_path / 'trends.db'
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.executescript(measure._SCHEMA)
    since_install = {'install_date': '2026-03-24', 'all_in': {'total_usd': 9, flag: True}}
    summary = {'since_install': since_install} if summary_has_since_install else {}
    monkeypatch.setattr(measure, 'TRENDS_DB', db)
    monkeypatch.setattr(measure, 'SNAPSHOT_DIR', tmp_path)
    monkeypatch.setattr(measure, '_get_merged_savings', lambda **kw: dict(summary))
    monkeypatch.setattr(savings, '_savings_since_install', lambda: since_install)
    monkeypatch.setattr(apply_safe, 'build_plan', lambda _: {'items': []})
    monkeypatch.setattr(cold_guard, 'pool', lambda _: {})

    assert next_actions.main(measure, ['--refresh-cache']) == 0
    assert counted_headline.build_overall(measure) is None
    assert json.loads((tmp_path / next_actions.REPORT_NAME).read_text())['schema'] == 1

    since_install['all_in'].update({flag: False, 'total_usd': 15})
    assert next_actions.main(measure, ['--refresh-cache']) == 0
    assert counted_headline.build_overall(measure)['usd'] == 15


def test_windows_refresh_clears_its_pid_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(next_actions, 'os', SimpleNamespace(**{**vars(os), 'name': 'nt'}))
    facade = SimpleNamespace(SNAPSHOT_DIR=tmp_path, __file__=__file__, _pid_is_running=lambda _: True,
                             _get_merged_savings=lambda **kw: None)
    starts = []

    def start(argv, **kwargs):
        starts.append(argv)
        return SimpleNamespace(pid=os.getpid())

    monkeypatch.setattr(next_actions, 'spawn_detached', start)
    next_actions._start_refresh(facade)
    next_actions._start_refresh(facade)
    assert len(starts) == 1

    assert next_actions.main(facade, ['--refresh-cache']) == 1
    assert not (tmp_path / 'next_refresh_pid.json').exists()

    next_actions._start_refresh(facade)
    assert len(starts) == 2


@pytest.mark.parametrize('marker', [{'pid': 123}, {'pid': 123, 'spawned_at': 1}])
def test_windows_refresh_ignores_abandoned_pid_markers(tmp_path, monkeypatch, marker):
    monkeypatch.setattr(next_actions, 'os', SimpleNamespace(**{**vars(os), 'name': 'nt'}))
    facade = SimpleNamespace(SNAPSHOT_DIR=tmp_path, __file__=__file__, _pid_is_running=lambda _: True)
    path = tmp_path / 'next_refresh_pid.json'
    path.write_text(json.dumps(marker))
    monkeypatch.setattr(next_actions, 'spawn_detached', lambda *a, **kw: SimpleNamespace(pid=456))

    next_actions._start_refresh(facade)
    assert json.loads(path.read_text())['pid'] == 456
