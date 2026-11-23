"""Approval boundaries for the safe savings command."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))
import measure
from _measure import dashboard, structural_ledger, apply_safe


def test_apply_receipts_append_without_fchmod(tmp_path, monkeypatch):
    facade = SimpleNamespace(SNAPSHOT_DIR=tmp_path)
    monkeypatch.delattr(os, 'fchmod', raising=False)
    for status in ('pending', 'complete'):
        apply_safe._append_receipt(facade, {'status': status})
    assert (tmp_path / 'apply_safe_receipts.jsonl').read_bytes() == (
        b'{"status": "pending"}\n{"status": "complete"}\n')


@pytest.mark.parametrize('kind,ident,key,old,new', [
    ('cold_guard', 'cold_guard:ask', 'cold_guard_mode', 'warn', 'ask'),
    ('keepwarm_auto', 'keepwarm_auto:on', 'keepwarm_auto', False, True),
])
def test_lane_apply_and_undo_restore_value(overlaps, monkeypatch, kind, ident, key, old, new):
    from contextlib import nullcontext
    flags = {key: old}
    monkeypatch.setattr(measure, '_read_config_flag', lambda k, default=None: flags.get(k, default))
    monkeypatch.setattr(measure, '_write_config_flag', lambda k, v: flags.update({k: v}))
    monkeypatch.setattr(measure, '_settings_lock', lambda **kw: nullcontext(True))
    item = {'id': ident, 'kind': kind, 'net_usd_month': 1, 'own_cost_usd_month': 0}
    monkeypatch.setattr(apply_safe, 'build_plan', lambda _: {'items': [item]})
    result = apply_safe.apply(measure, {}, [ident], apply_safe.plan_token([item]))
    assert result['ok'] and flags[key] == new
    assert apply_safe.undo(measure, result['receipt']['receipt_id'])['ok']
    assert flags[key] == old


def test_undo_recovers_change_after_crash_before_item_receipt(overlaps, monkeypatch):
    from contextlib import nullcontext
    flags = {'cold_guard_mode': 'warn'}
    monkeypatch.setattr(measure, '_read_config_flag', lambda k, default=None: flags.get(k, default))
    monkeypatch.setattr(measure, '_write_config_flag', lambda k, v: flags.update({k: v}))
    monkeypatch.setattr(measure, '_settings_lock', lambda **kw: nullcontext(True))
    item = {'id': 'cold_guard:ask', 'kind': 'cold_guard', 'net_usd_month': 1, 'own_cost_usd_month': 0}
    monkeypatch.setattr(apply_safe, 'build_plan', lambda _: {'items': [item]})
    append = apply_safe._append_receipt
    def crash(facade, row):
        if row.get('status') == 'item':
            raise KeyboardInterrupt()
        append(facade, row)
    monkeypatch.setattr(apply_safe, '_append_receipt', crash)
    with pytest.raises(KeyboardInterrupt):
        apply_safe.apply(measure, {}, [item['id']], apply_safe.plan_token([item]))
    assert flags['cold_guard_mode'] == 'ask'
    pending = apply_safe._receipts(measure)[0]
    monkeypatch.setattr(apply_safe, '_append_receipt', append)
    assert apply_safe.undo(measure, pending['receipt_id'])['ok']
    assert flags['cold_guard_mode'] == 'warn'


@pytest.mark.parametrize('kind,key,initial,applied,changed', [
    ('cold_guard', 'cold_guard_mode', 'warn', 'ask', 'off'),
    ('keepwarm_auto', 'keepwarm_auto', False, True, False),
])
def test_lane_undo_refuses_a_later_edit(monkeypatch, kind, key, initial, applied, changed):
    from importlib import import_module
    from types import SimpleNamespace
    flags = {key: initial}
    facade = SimpleNamespace(_read_config_flag=lambda k, default=None: flags.get(k, default),
                             _write_config_flag=lambda k, v: flags.update({k: v}))
    module = import_module('_measure.' + kind)
    receipt = module.apply_item(facade, {'id': kind + (':on' if kind == 'keepwarm_auto' else ':ask')})
    assert flags[key] == applied
    flags[key] = changed
    assert not module.undo_item(facade, receipt)
    assert flags[key] == changed


def test_undo_recovers_archive_after_crash_before_item_receipt(overlaps, monkeypatch):
    from contextlib import nullcontext
    item = {'id': 'unused_skill:first', 'kind': 'unused_skill', 'tokens_per_request': 100,
            'net_usd_month': 1, 'own_cost_usd_month': 0}
    monkeypatch.setattr(apply_safe, 'build_plan', lambda _: {'items': [item]})
    monkeypatch.setattr(measure, '_manage_skill', lambda action, name: dashboard._manage_skill_locked(action, name))
    monkeypatch.setattr(measure, '_settings_lock', lambda **kw: nullcontext(True))
    append = apply_safe._append_receipt
    def crash(facade, row):
        if row.get('status') == 'item':
            raise KeyboardInterrupt()
        append(facade, row)
    monkeypatch.setattr(apply_safe, '_append_receipt', crash)
    with pytest.raises(KeyboardInterrupt):
        apply_safe.apply(measure, {}, [item['id']], apply_safe.plan_token([item]))
    assert not (overlaps / 'first').exists()
    pending = apply_safe._receipts(measure)[0]
    monkeypatch.setattr(apply_safe, '_append_receipt', append)
    assert apply_safe.undo(measure, pending['receipt_id'])['ok']
    assert (overlaps / 'first/SKILL.md').exists()


@pytest.fixture
def overlaps(tmp_path, monkeypatch):
    root = tmp_path / 'claude'
    local = root / 'skills'
    plugin = root / 'plugin'
    for name in ('first', 'second'):
        for parent in (local, plugin / 'skills'):
            (parent / name).mkdir(parents=True, exist_ok=True)
            (parent / name / 'SKILL.md').write_text('---\nname: '+name+'\n---\n')
    (root / 'plugins').mkdir()
    (root / 'plugins/installed_plugins.json').write_text(json.dumps({'plugins': {'p': [{'installPath': str(plugin)}]}}))
    (root / 'settings.json').write_text('{}')
    monkeypatch.setattr(measure, 'CLAUDE_DIR', root)
    monkeypatch.setattr(measure, 'SETTINGS_PATH', root / 'settings.json')
    monkeypatch.setattr(measure, 'SNAPSHOT_DIR', root / 'snapshot')
    monkeypatch.setattr(measure, '_is_foreign_runtime', lambda: False)
    monkeypatch.setattr(measure, 'estimate_tokens_from_frontmatter', lambda _: 100)
    return local


def test_cleanup_without_selection_keeps_existing_behavior(overlaps):
    actions = dashboard.plugin_cleanup(dry_run=False, quiet=True)
    assert len(actions) == 2
    assert not (overlaps / 'first').exists()
    assert not (overlaps / 'second').exists()


def test_cleanup_selected_only(overlaps, monkeypatch):
    items = [{'id': 'duplicate_skill:first', 'tokens_per_request': 100, 'net_usd_month': 1, 'own_cost_usd_month': 0}]
    monkeypatch.setattr(apply_safe, 'build_plan', lambda _: {'items': items}, raising=False)
    actions = dashboard.plugin_cleanup(dry_run=False, quiet=True, selected_items={'first'}, plan_token=apply_safe.plan_token(items), selected_ids=[items[0]['id']])
    assert actions == ['archived duplicate local skill: first']
    assert not (overlaps / 'first').exists()
    assert (overlaps / 'second/SKILL.md').exists()


def test_cleanup_rejects_stale_token(overlaps, monkeypatch):
    items = [{'id': 'duplicate_skill:first', 'tokens_per_request': 100, 'net_usd_month': 1, 'own_cost_usd_month': 0}]
    token = apply_safe.plan_token(items)
    items[0]['tokens_per_request'] = 101
    monkeypatch.setattr(apply_safe, 'build_plan', lambda _: {'items': items}, raising=False)
    assert dashboard.plugin_cleanup(dry_run=False, quiet=True, selected_items={'first'}, plan_token=token, selected_ids=[items[0]['id']]) == []
    assert (overlaps / 'first/SKILL.md').exists()
    assert (overlaps / 'second/SKILL.md').exists()


def test_cleanup_rejects_unapproved_name(overlaps, monkeypatch):
    items = [{'id': 'duplicate_skill:first', 'tokens_per_request': 100, 'net_usd_month': 1, 'own_cost_usd_month': 0}]
    monkeypatch.setattr(apply_safe, 'build_plan', lambda _: {'items': items}, raising=False)
    assert dashboard.plugin_cleanup(dry_run=False, quiet=True, selected_items={'second'}, plan_token=apply_safe.plan_token(items), selected_ids=[items[0]['id']]) == []
    assert (overlaps / 'second/SKILL.md').exists()


def test_selected_cleanup_can_be_restored(overlaps, monkeypatch):
    items = [{'id': 'duplicate_skill:first', 'tokens_per_request': 100, 'net_usd_month': 1, 'own_cost_usd_month': 0}]
    monkeypatch.setattr(apply_safe, 'build_plan', lambda _: {'items': items}, raising=False)
    original = (overlaps / 'first/SKILL.md').read_bytes()
    dashboard.plugin_cleanup(dry_run=False, quiet=True, selected_items={'first'}, plan_token=apply_safe.plan_token(items), selected_ids=[items[0]['id']])
    assert dashboard._manage_skill_locked('restore', 'first')
    assert (overlaps / 'first/SKILL.md').read_bytes() == original


def test_plan_excludes_used_recent_and_nonlocal_skills(tmp_path, monkeypatch):
    import os
    import sqlite3
    import time
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    root = tmp_path / 'claude'
    skills = root / 'skills'
    for name in ('used', 'recent', 'eligible'):
        (skills / name).mkdir(parents=True)
        (skills / name / 'SKILL.md').write_text('---\nname: ' + name + '\n---\n')
    old = time.time() - 40 * 86400
    for name in ('used', 'eligible'):
        os.utime(skills / name, (old, old))
    settings = root / 'settings.json'
    settings.write_text('{}')
    db = root / 'trends.db'
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE session_log (parent_daily_usage_json TEXT, daily_calls_json TEXT, last_activity_day TEXT, date TEXT, daily_metrics_json TEXT)')
    conn.execute('CREATE TABLE skill_daily (date TEXT, skill TEXT, invocations INTEGER)')
    today = datetime.now().date().isoformat()
    conn.execute('INSERT INTO skill_daily VALUES (?, ?, 1)', (today, 'used'))
    conn.execute('INSERT INTO session_log VALUES (?, ?, ?, ?, ?)', (json.dumps({today: {'m': {'requests': 10, 'cache_read': 10000}}}), json.dumps({today: 10}), today, today, json.dumps({today: {'tool_calls': {}}})))
    conn.commit()
    conn.close()
    components = {'skills': {'names': ['used', 'recent', 'eligible', '../project', 'missing']}, 'skills_detail': {name: {'frontmatter_tokens': 100} for name in ('used', 'recent', 'eligible')}}
    facade = SimpleNamespace(CLAUDE_DIR=root, SETTINGS_PATH=settings, datetime=datetime, timedelta=timedelta,
                             measure_components=lambda: components, _collect_management_data=lambda **kw: {},
                             _open_trends_db_readonly=lambda **kw: sqlite3.connect(db),
                             _find_all_jsonl_files=lambda **kw: [('session', 0, '')],
                             _find_subagent_jsonl_files=lambda path: [],
                             _parse_session_jsonl=lambda path: {'daily_metrics': {today: {'skills': {}, 'tool_calls': {}}}})
    monkeypatch.setattr(apply_safe.limit_units, '_resolve_card', lambda *a, **kw: {'cache_read': .1, 'cache_write_1h': 2, 'input': 1, 'output': 1})
    monkeypatch.setattr(apply_safe.limit_units, 'limit_units', lambda *a, **kw: {'priced': True, 'units': 100, 'unpriced_tokens': 0})
    monkeypatch.setattr(apply_safe.transcript_product, 'from_facade', lambda *a, **kw: {'listing': {'at_cap': False}, 'unused_connectors': [], 'incomplete': False})
    monkeypatch.setattr(apply_safe.autocompact_advice, 'plan_item', lambda _: None)
    plan = apply_safe.build_plan(facade)
    assert [item['id'] for item in plan['items']] == ['unused_skill:eligible']
    assert apply_safe.validate_token(facade, ['unused_skill:eligible'], plan['token'])


def test_apply_then_undo_restores_settings_and_skill(overlaps, monkeypatch):
    from contextlib import nullcontext
    item = {'id': 'unused_skill:first', 'kind': 'unused_skill', 'label': 'Archive first', 'tokens_per_request': 100, 'net_usd_month': 1.0, 'own_cost_usd_month': 0.0}
    plan = {'items': [item]}
    monkeypatch.setattr(apply_safe, 'build_plan', lambda _: plan)
    monkeypatch.setattr(measure, '_manage_skill', lambda action, name: dashboard._manage_skill_locked(action, name))
    monkeypatch.setattr(measure, '_settings_lock', lambda **kw: nullcontext(True))
    before = measure.SETTINGS_PATH.read_bytes()
    skill = (overlaps / 'first/SKILL.md').read_bytes()
    result = apply_safe.apply(measure, plan, [item['id']], apply_safe.plan_token([item]))
    assert result['ok']
    assert not (overlaps / 'first').exists()
    assert apply_safe.undo(measure, result['receipt']['receipt_id'])['ok']
    assert measure.SETTINGS_PATH.read_bytes() == before
    assert (overlaps / 'first/SKILL.md').read_bytes() == skill


def test_apply_stale_token_changes_nothing(overlaps, monkeypatch):
    item = {'id': 'unused_skill:first', 'kind': 'unused_skill', 'tokens_per_request': 101, 'net_usd_month': 1.0, 'own_cost_usd_month': 0.0}
    monkeypatch.setattr(apply_safe, 'build_plan', lambda _: {'items': [item]})
    previous = dict(item, tokens_per_request=100)
    assert not apply_safe.apply(measure, {}, [item['id']], apply_safe.plan_token([previous]))['ok']
    assert (overlaps / 'first/SKILL.md').exists()
    assert not measure.SNAPSHOT_DIR.exists()


def test_noninteractive_plan_does_not_write(overlaps, monkeypatch, capsys):
    from types import SimpleNamespace
    item = {'id': 'unused_skill:first', 'kind': 'unused_skill', 'label': 'Archive first', 'tokens_per_request': 100, 'net_usd_month': 1.0, 'own_cost_usd_month': 0.0}
    monkeypatch.setattr(apply_safe, 'build_plan', lambda _: {'items': [item], 'token': apply_safe.plan_token([item])})
    monkeypatch.setattr(apply_safe.sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    before = {p: p.stat().st_mtime_ns for p in (measure.SETTINGS_PATH, overlaps / 'first', overlaps / 'first/SKILL.md')}
    assert apply_safe.main(measure, []) == 0
    assert {p: p.stat().st_mtime_ns for p in before} == before
    assert not measure.SNAPSHOT_DIR.exists()


def test_failed_item_does_not_block_later_item(overlaps, monkeypatch):
    items = [{'id': 'unused_skill:' + name, 'kind': 'unused_skill', 'tokens_per_request': 100, 'net_usd_month': 1.0, 'own_cost_usd_month': 0.0} for name in ('first', 'second')]
    monkeypatch.setattr(apply_safe, 'build_plan', lambda _: {'items': items})
    monkeypatch.setattr(measure, '_manage_skill', lambda action, name: dashboard._manage_skill_locked(action, name) if name == 'second' else False)
    result = apply_safe.apply(measure, {}, [i['id'] for i in items], apply_safe.plan_token(items))
    assert [row['ok'] for row in result['receipt']['items']] == [False, True]
    receipt = [r for r in apply_safe._receipts(measure) if r.get('status') == 'complete'][0]
    assert [row['ok'] for row in receipt['items']] == [False, True]


def test_one_archive_records_one_structural_trim(overlaps, monkeypatch):
    import importlib
    importlib.reload(structural_ledger)
    item = {'id': 'unused_skill:first', 'kind': 'unused_skill', 'tokens_per_request': 100, 'net_usd_month': 1.0, 'own_cost_usd_month': 0.0}
    monkeypatch.setattr(apply_safe, 'build_plan', lambda _: {'items': [item]})
    monkeypatch.setattr(measure, '_manage_skill', lambda action, name: dashboard._manage_skill_locked(action, name))
    monkeypatch.setattr(measure, 'estimate_tokens_from_frontmatter', lambda _: 100)
    monkeypatch.setattr(measure, '_log_savings_event', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('duplicate event')))
    assert apply_safe.apply(measure, {}, [item['id']], apply_safe.plan_token([item]))['ok']
    trims = structural_ledger.load_trims(measure.SNAPSHOT_DIR)
    assert len(trims) == 1
    assert trims[0]['item'] == 'first'


def test_live_usage_includes_uncollected_session_uses():
    from types import SimpleNamespace
    facade = SimpleNamespace(_find_all_jsonl_files=lambda **kw: [('session', 0, '')],
                             _find_subagent_jsonl_files=lambda path: [],
                             _parse_session_jsonl=lambda path: {'daily_metrics': {'2026-11-23': {'skills': {'used': 1}, 'tool_calls': {'mcp__server__tool': 1}}}})
    used, tools = apply_safe._live_usage(facade, '2026-11-01')
    assert used == {'used'}
    assert tools == {'mcp__server__tool'}


def test_live_usage_missing_evidence_is_unknown():
    from types import SimpleNamespace
    facade = SimpleNamespace(_find_all_jsonl_files=lambda **kw: [('session', 0, '')],
                             _find_subagent_jsonl_files=lambda path: [],
                             _parse_session_jsonl=lambda path: None)
    assert apply_safe._live_usage(facade, '2026-11-01') is None


def test_mcp_apply_and_undo_restore_original_settings_bytes(overlaps, monkeypatch):
    from contextlib import nullcontext
    original = b'{\n  "mcpServers": {"srv": {"command": "worker"}},\n  "other": true\n}\n'
    measure.SETTINGS_PATH.write_bytes(original)
    def read():
        return json.loads(measure.SETTINGS_PATH.read_text()), True
    def manage(action, name):
        data, _ = read()
        if action == 'disable':
            data['_disabledMcpServers'] = {name: data['mcpServers'].pop(name)}
        else:
            data['mcpServers'][name] = data['_disabledMcpServers'].pop(name)
            data.pop('_disabledMcpServers')
        measure.SETTINGS_PATH.write_text(json.dumps(data))
        return True
    monkeypatch.setattr(measure, '_read_settings_for_write', read)
    monkeypatch.setattr(measure, '_manage_mcp', manage)
    monkeypatch.setattr(measure, '_settings_lock', lambda **kw: nullcontext(True))
    item = {'id': 'unused_mcp:srv', 'kind': 'unused_mcp', 'tokens_per_request': 100, 'net_usd_month': 1.0, 'own_cost_usd_month': 0.0}
    monkeypatch.setattr(apply_safe, 'build_plan', lambda _: {'items': [item]})
    result = apply_safe.apply(measure, {}, [item['id']], apply_safe.plan_token([item]))
    assert result['ok']
    assert apply_safe.undo(measure, result['receipt']['receipt_id'])['ok']
    assert measure.SETTINGS_PATH.read_bytes() == original
    trims = structural_ledger.load_trims(measure.SNAPSHOT_DIR)
    assert len(trims) == 1 and trims[0]['closed_at']


@pytest.mark.parametrize('without_fchmod', [False, True], ids=['native', 'without-fchmod'])
def test_restore_settings_format_preserves_symlink(tmp_path, monkeypatch, without_fchmod):
    from contextlib import nullcontext
    if without_fchmod:
        monkeypatch.delattr(os, 'fchmod', raising=False)
    target = tmp_path / 'actual.json'
    target.write_text('{"key": 1}')
    link = tmp_path / 'settings.json'
    link.symlink_to(target)
    backup = tmp_path / 'backup.json'
    backup.write_text('{\n  "key": 1\n}\n')
    facade = SimpleNamespace(SETTINGS_PATH=link, _settings_lock=lambda **kw: nullcontext(True))
    assert apply_safe._restore_settings_format(facade, backup)
    assert link.is_symlink()
    assert target.read_bytes() == backup.read_bytes()


def test_yes_with_stale_token_does_not_change_files(overlaps, monkeypatch, capsys):
    item = {'id': 'unused_skill:first', 'kind': 'unused_skill', 'label': 'Archive first', 'tokens_per_request': 100, 'net_usd_month': 1.0, 'own_cost_usd_month': 0.0}
    plan = {'items': [item], 'token': apply_safe.plan_token([item])}
    monkeypatch.setattr(apply_safe, 'build_plan', lambda _: plan)
    assert apply_safe.main(measure, ['--yes', '--token', 'stale']) == 1
    assert (overlaps / 'first/SKILL.md').exists()
    assert not measure.SNAPSHOT_DIR.exists()
    assert 'Your setup changed' in capsys.readouterr().out


def test_first_yes_accepts_dollar_changes_below_display_precision(overlaps, monkeypatch, capsys):
    from contextlib import nullcontext

    item = {'id': 'unused_skill:first', 'kind': 'unused_skill', 'label': 'Archive first',
            'tokens_per_request': 100, 'net_usd_month': 1.001, 'own_cost_usd_month': 0.101}
    monkeypatch.setattr(apply_safe, 'build_plan', lambda _: {'items': [item], 'token': apply_safe.plan_token([item])})
    monkeypatch.setattr(measure, '_manage_skill', dashboard._manage_skill_locked)
    monkeypatch.setattr(measure, '_settings_lock', lambda **kw: nullcontext(True))
    assert apply_safe.main(measure, ['--json']) == 0
    token = json.loads(capsys.readouterr().out)['token']

    item.update(net_usd_month=1.004, own_cost_usd_month=0.104)
    assert apply_safe.main(measure, ['--yes', '--token', token]) == 0
    assert not (overlaps / 'first').exists()


@pytest.mark.parametrize('change', [
    {'id': 'autocompact_window:new'}, {'tokens_per_request': 1}, {'net_usd_month': 1.006},
    {'own_cost_usd_month': 0.106}, {'net_usd_month': None}, {'target': 500000},
    {'current': 500000}, {'per_model': False}, {'model': 'different'},
])
def test_plan_token_rejects_changed_actions_or_display_figures(monkeypatch, change):
    item = {'id': 'autocompact_window', 'kind': 'autocompact_window', 'tokens_per_request': 0,
            'net_usd_month': 1.004, 'own_cost_usd_month': 0.104, 'current': 600000,
            'target': 450000, 'per_model': True, 'model': 'fixture'}
    token = apply_safe.plan_token([item])
    monkeypatch.setattr(apply_safe, 'build_plan', lambda _: {'items': [dict(item, **change)]})

    assert not apply_safe.validate_token(measure, [item['id']], token)
