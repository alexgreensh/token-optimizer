"""Lane B: apply safe savings."""
import hashlib
import hmac
import json
import os
import argparse
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

from . import autocompact_advice, limit_units, prefix_cost, structural_ledger, transcript_product


def _old_enough(path):
    try:
        return path.lstat().st_mtime <= (datetime.now() - timedelta(days=30)).timestamp()
    except OSError:
        return False


def _price_prefix(facade, activity, tokens):
    total = 0.0
    for model, count in activity['requests_by_model'].items():
        card = limit_units._resolve_card(facade, model)
        if not card or card.get('cache_read') is None or card.get('input') is None:
            return None
        write = card.get('cache_write_1h', card['input'] * 2)
        total += tokens * (count * card['cache_read'] + activity['writes_events_by_model'].get(model, 0) * write) / 1000000
    return total if activity['requests_by_model'] else None


def _live_usage(facade, cutoff):
    """Check current transcripts too, so an outdated database cannot hide a use."""
    used, tools = set(), set()
    files = facade._find_all_jsonl_files(days=30)
    if not files:
        return None
    for path, _, _ in files:
        for transcript in [path, *facade._find_subagent_jsonl_files(path)]:
            parsed = facade._parse_session_jsonl(transcript)
            if not isinstance(parsed, dict) or not isinstance(parsed.get('daily_metrics'), dict):
                return None
            for field, target in (('skills_used', used), ('tool_calls', tools)):
                lifetime = parsed.get(field) or {}
                if not isinstance(lifetime, dict):
                    return None
                target.update(name for name, count in lifetime.items() if count)
            for day, metrics in parsed['daily_metrics'].items():
                if day < cutoff:
                    continue
                if not isinstance(metrics, dict) or not isinstance(metrics.get('skills'), dict) or not isinstance(metrics.get('tool_calls'), dict):
                    return None
                used.update(name for name, count in metrics['skills'].items() if count)
                tools.update(name for name, count in metrics['tool_calls'].items() if count)
    return used, tools


def build_plan(facade):
    """Build an advisory plan using only locally recorded evidence."""
    now = datetime.now()
    items = []
    components = facade.measure_components()
    management = facade._collect_management_data(components=components)
    conn = facade._open_trends_db_readonly(timeout=1.0)
    try:
        activity = prefix_cost._ledger_activity(facade, conn, 30)
        cutoff = (now - timedelta(days=30)).date().isoformat()
        used = {row[0] for row in conn.execute('SELECT DISTINCT skill FROM skill_daily WHERE date >= ? AND invocations > 0', (cutoff,))}
        metrics = conn.execute('SELECT daily_metrics_json FROM session_log WHERE COALESCE(last_activity_day,date) >= ?', (cutoff,)).fetchall()
        tools = set()
        tools_known = bool(metrics)
        for (raw,) in metrics:
            try:
                days = json.loads(raw)
                if not isinstance(days, dict):
                    raise ValueError('missing metrics')
                for day, values in days.items():
                    if day >= cutoff:
                        if not isinstance(values, dict) or not isinstance(values.get('tool_calls'), dict):
                            raise ValueError('missing tool calls')
                        tools.update(name for name, count in values['tool_calls'].items() if count)
            except (ValueError, TypeError, AttributeError):
                tools_known = False
    finally:
        conn.close()

    live = _live_usage(facade, cutoff)
    if live is None:
        used = set((components.get('skills') or {}).get('names') or ())
        tools_known = False
    else:
        used.update(live[0])
        tools.update(live[1])

    skill_info = components.get('skills') or {}
    observations = transcript_product.from_facade(facade, days=30)
    listing_eligible = not observations.get('incomplete') and (observations.get('listing') or {}).get('at_cap') is False
    aliases = skill_info.get('name_to_dir') or {}
    used_dirs = set()
    for name in used:
        if isinstance(name, str):
            for part in (name, *name.split(':')):
                used_dirs.add(part)
                used_dirs.add(aliases.get(part, part))
    plugin_names = {name.split(':', 1)[-1] for name in (components.get('plugin_skills') or {}).get('names', [])}
    details = components.get('skills_detail') or {}
    inventory = []
    for name in (skill_info.get('names') or []) if listing_eligible else ():
        if not isinstance(name, str) or not name or '/' in name or '\\' in name or name in ('.', '..'):
            continue
        path = facade.CLAUDE_DIR / 'skills' / name
        if not path.is_dir() or not _old_enough(path) or name in used_dirs:
            continue
        tokens = int((details.get(name) or {}).get('frontmatter_tokens') or 0)
        if tokens <= 0:
            continue
        duplicate = name in plugin_names and (path.is_symlink() or not [p for p in path.iterdir() if p.name != 'SKILL.md' and not p.name.startswith('.')])
        inventory.append({'name': name, 'kind': 'skill', 'tokens': tokens, 'uses': 0, 'duplicate': duplicate})
    settings_old = _old_enough(facade.SETTINGS_PATH)
    if tools_known and settings_old:
        connector_rows = {transcript_product._normalize(item['server']): item for item in observations.get('unused_connectors', []) if item.get('server')}
        for server in (management.get('mcp_servers') or {}).get('active', []):
            name = server.get('name')
            normalized = lambda text: ''.join(ch.lower() for ch in text if ch.isalnum())
            called = any(tool.startswith('mcp__') and normalized(tool.split('__', 2)[1]) == normalized(name) for tool in tools if isinstance(tool, str) and isinstance(name, str))
            observed = connector_rows.get(transcript_product._normalize(name)) if isinstance(name, str) else None
            tokens = int((observed or {}).get('estimated_tokens_per_call') or 0)
            if name and tokens > 0 and not called and not observations.get('incomplete'):
                inventory.append({'name': name, 'kind': 'mcp_server', 'tokens': tokens, 'uses': 0})
    conn = facade._open_trends_db_readonly(timeout=1.0)
    try:
        report = prefix_cost.prefix_cost_report(facade, conn, days=30, inventory=inventory)
    finally:
        conn.close()
    source_rows = {(row['kind'], row['name']): row for row in report['all_components']}
    for row in inventory if activity['total_requests'] > 0 else ():
        name, tokens = row['name'], row['tokens']
        evidence = source_rows.get((row['kind'], name))
        if not evidence or not evidence['unused_here']:
            continue
        tokens = evidence['tokens']
        kind = ('duplicate_skill' if row.get('duplicate') else 'unused_skill') if row['kind'] == 'skill' else 'unused_mcp'
        net = _price_prefix(facade, activity, tokens)
        if net is not None and net <= 0:
            continue
        items.append({'id': kind + ':' + name, 'kind': kind, 'label': ('Archive skill ' if kind != 'unused_mcp' else 'Disable unused server ') + name,
                      'tokens_per_request': tokens, 'net_usd_month': net, 'own_cost_usd_month': 0.0,
                      'basis': '0 uses in 30 days; {} tokens on {} requests'.format(tokens, activity['total_requests']),
                      'undo': 'measure.py apply-safe --undo <receipt_id>', 'default_selected': True})
    items.sort(key=lambda item: (('duplicate_skill', 'unused_skill', 'unused_mcp').index(item['kind']), item['id']))
    advice = autocompact_advice.recommend(facade)
    compact = autocompact_advice.plan_item(facade, advice) if advice and not advice.get('blocked') else None
    if compact:
        items.append(compact)
    for module in ('cold_guard', 'keepwarm_auto'):
        try:
            from importlib import import_module
            planner = getattr(import_module('.' + module, __package__), 'plan_item', None)
            item = planner(facade) if callable(planner) else None
            if isinstance(item, dict) and (item.get('net_usd_month') is None or float(item['net_usd_month']) > 0):
                items.append(item)
        except Exception:
            pass
    return {'schema': 1, 'generated_at': now.isoformat(), 'window_days': 30, 'items': items, **({'advice': advice['text']} if advice and advice.get('blocked') else {}),
            'total_net_usd_month': sum(float(i['net_usd_month']) for i in items if i.get('net_usd_month') is not None),
            'token': plan_token(items)}


def plan_token(items):
    """Bind approval to the selected identities and their financial figures."""
    fields = ('id', 'tokens_per_request', 'net_usd_month', 'own_cost_usd_month')
    selected = [{key: item.get(key) for key in fields} for item in items]
    payload = json.dumps(sorted(selected, key=lambda item: item['id']), sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def validate_token(facade, selected_ids, token):
    """Rebuild the plan and reject absent, changed, or duplicated selections."""
    if not isinstance(token, str) or not isinstance(selected_ids, (list, tuple)) or not selected_ids or len(set(selected_ids)) != len(selected_ids):
        return False
    try:
        items = build_plan(facade)['items']
        selected = [item for item in items if item['id'] in selected_ids]
        return len(selected) == len(selected_ids) and hmac.compare_digest(token, plan_token(selected))
    except (ValueError, TypeError, KeyError, NameError, AttributeError):
        return False


def _append_receipt(facade, receipt):
    path = facade.SNAPSHOT_DIR / 'apply_safe_receipts.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        data = (json.dumps(receipt, sort_keys=True) + '\n').encode()
        if os.write(fd, data) != len(data):
            raise OSError('incomplete receipt')
        os.fsync(fd)
    finally:
        os.close(fd)


def _archive_paths(facade, name):
    parent = facade.CLAUDE_DIR / '_backups'
    try:
        return {str(p / name) for p in parent.iterdir() if p.is_dir() and p.name.startswith(facade.SKILL_ARCHIVE_DIR_PREFIXES) and ((p / name).exists() or (p / name).is_symlink())}
    except OSError:
        return set()


def apply(facade, plan, selected_ids, token):
    """Apply an approved fresh plan, recording each result without double credit."""
    if facade._is_foreign_runtime():
        return {'ok': False, 'error': 'This runtime cannot modify these settings.'}
    if not validate_token(facade, selected_ids, token):
        return {'ok': False, 'error': 'Your setup changed since the plan was shown. Run it again.'}
    fresh = build_plan(facade)
    by_id = {item['id']: item for item in fresh['items']}
    try:
        selected = [by_id[ident] for ident in selected_ids]
        if not hmac.compare_digest(token, plan_token(selected)):
            raise ValueError('stale')
    except (KeyError, ValueError, TypeError):
        return {'ok': False, 'error': 'Your setup changed since the plan was shown. Run it again.'}
    receipt_id = uuid.uuid4().hex
    backup = facade._backup_settings_file(facade.SNAPSHOT_DIR / 'apply-safe-backups')
    if not backup:
        return {'ok': False, 'error': 'Settings backup failed. Nothing was changed.'}
    receipt = {'receipt_id': receipt_id, 'ts': datetime.now().isoformat(), 'items': [], 'backup_path': str(backup)}
    try:
        _append_receipt(facade, dict(receipt, status='pending', selected_ids=selected_ids))
    except OSError:
        return {'ok': False, 'error': 'Receipt could not be written. Nothing was changed.'}
    duplicates = [item for item in selected if item['kind'] == 'duplicate_skill']
    duplicate_results = set()
    prior_trims = {trim['id'] for trim in structural_ledger.load_trims(facade.SNAPSHOT_DIR)}
    before_archives = {item['id']: _archive_paths(facade, item['id'].split(':', 1)[1]) for item in duplicates}
    if duplicates:
        try:
            result = facade.plugin_cleanup(dry_run=False, quiet=True,
                selected_items={item['id'].split(':', 1)[1] for item in duplicates}, plan_token=token, selected_ids=selected_ids)
            duplicate_results = {line.removeprefix('archived duplicate local skill: ') for line in result if line.startswith('archived duplicate local skill: ')}
        except Exception:
            pass
    for item in selected:
        ident, kind = item['id'], item['kind']
        name = ident.split(':', 1)[1] if ':' in ident else ''
        row = {'id': ident, 'kind': kind, 'undo_action': None, 'before': None, 'after': None, 'ok': False}
        previous_archives = before_archives.get(ident, _archive_paths(facade, name)) if kind in ('duplicate_skill', 'unused_skill') else set()
        try:
            if kind == 'duplicate_skill':
                row.update(ok=name in duplicate_results, undo_action='restore', before=name, after=name)
            elif kind == 'unused_skill':
                row.update(undo_action='restore', before=name, after=name)
                row['ok'] = bool(facade._manage_skill('archive', name))
            elif kind == 'unused_mcp':
                row.update(ok=bool(facade._manage_mcp('disable', name)), undo_action='enable', before=name, after=name)
                if row['ok']:
                    row['applied'] = True
                    trim = structural_ledger.make_trim('mcp_tools', name, 'global', item['tokens_per_request'], 0,
                        structural_ledger.now_iso(), 'apply_safe', evidence=str(backup))
                    if not structural_ledger.record_trim(facade.SNAPSHOT_DIR, trim):
                        raise RuntimeError('trim not recorded')
                    row['trim_id'] = trim['id']
            elif kind == 'autocompact_window':
                result = autocompact_advice.apply_item(facade, item)
                row.update(ok=bool(result.get('ok')), undo_action='restore_window', before=result.get('before'), after=result.get('after'), setting=result, main_model=item.get('model'), previous_window=item.get('current'), applied_at=datetime.now().isoformat())
            elif kind in ('cold_guard', 'keepwarm_auto'):
                from importlib import import_module
                module = import_module('.' + kind, __package__)
                result = module.apply_item(facade, item)
                row.update(ok=bool(result.get('ok')), undo_action='lane', before=result.get('before'), after=result.get('after'), lane_result=result)
        except Exception as exc:
            row['ok'] = False
            row['error'] = type(exc).__name__
        if kind in ('duplicate_skill', 'unused_skill'):
            added = _archive_paths(facade, name) - previous_archives
            source = facade.CLAUDE_DIR / 'skills' / name
            if len(added) == 1 and not source.exists() and not source.is_symlink():
                row.update(applied=True, archive=added.pop())
                new_trims = [trim for trim in structural_ledger.load_trims(facade.SNAPSHOT_DIR)
                             if trim['id'] not in prior_trims and trim.get('component') == 'skills' and trim.get('item') == name]
                if len(new_trims) != 1:
                    row.update(ok=False, error='structural-trim-not-verified')
            elif row['ok']:
                row.update(ok=False, error='archive-not-verified')
        receipt['items'].append(row)
        try:
            _append_receipt(facade, {'receipt_id': receipt_id, 'status': 'item', 'item': row})
        except OSError:
            row['receipt_error'] = True
            break
    try:
        receipt['settings_after_sha256'] = hashlib.sha256(facade.SETTINGS_PATH.read_bytes()).hexdigest()
        _append_receipt(facade, dict(receipt, status='complete'))
    except OSError:
        return {'ok': False, 'error': 'Changes were made but the receipt could not be completed.', 'receipt': receipt}
    return {'ok': all(row['ok'] for row in receipt['items']) and len(receipt['items']) == len(selected), 'receipt': receipt}


def _receipts(facade):
    try:
        lines = (facade.SNAPSHOT_DIR / 'apply_safe_receipts.jsonl').read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except ValueError:
            continue
    return rows


def _restore_settings_format(facade, backup):
    """Restore original bytes only when no other setting differs."""
    original = backup.read_bytes()
    original_data = json.loads(original)
    with facade._settings_lock(user_initiated=True) as acquired:
        if not acquired:
            return False
        path = facade.SETTINGS_PATH.resolve(strict=True)
        current = path.read_bytes()
        if json.loads(current) != original_data:
            return True
        stat = path.stat()
        fd, temp = tempfile.mkstemp(prefix='.apply-safe-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as out:
                os.fchmod(out.fileno(), stat.st_mode & 0o777)
                out.write(original)
                out.flush()
                os.fsync(out.fileno())
            if facade.SETTINGS_PATH.resolve(strict=True) != path or path.read_bytes() != current:
                return False
            os.replace(temp, path)
            return True
        finally:
            if os.path.exists(temp):
                os.unlink(temp)


def undo(facade, receipt_id):
    if facade._is_foreign_runtime():
        return {'ok': False, 'error': 'This runtime cannot modify these settings.'}
    rows = _receipts(facade)
    previous_undo = [row for row in rows if row.get('undo_of') == receipt_id]
    if any(row.get('ok') for row in previous_undo):
        return {'ok': False, 'error': 'This receipt was already undone.'}
    already_restored = {item['id'] for row in previous_undo for item in row.get('items', []) if item.get('ok')}
    receipt = next((row for row in reversed(rows) if row.get('receipt_id') == receipt_id and row.get('status') == 'complete'), None)
    if not receipt:
        return {'ok': False, 'error': 'Receipt not found.'}
    backup = facade.Path(receipt['backup_path'])
    try:
        backup.resolve().relative_to((facade.SNAPSHOT_DIR / 'apply-safe-backups').resolve())
        original = json.loads(backup.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return {'ok': False, 'error': 'Settings backup is unavailable.'}
    results = []
    for item in reversed(receipt['items']):
        if not (item.get('ok') or item.get('applied')) or item['id'] in already_restored:
            continue
        ident, action = item['id'], item.get('undo_action')
        name = ident.split(':', 1)[1] if ':' in ident else ''
        ok = False
        try:
            if action == 'restore':
                parent = facade.CLAUDE_DIR / '_backups'
                candidates = sorted((p / name for p in parent.iterdir() if p.is_dir() and p.name.startswith(facade.SKILL_ARCHIVE_DIR_PREFIXES) and (p / name).exists()), reverse=True)
                if candidates and str(candidates[0]) == item.get('archive'):
                    ok = bool(facade._manage_skill('restore', name))
            elif action == 'enable':
                current, valid = facade._read_settings_for_write()
                old_config = (original.get('mcpServers') or {}).get(name)
                disabled = (current.get('_disabledMcpServers') or {}).get(name)
                if valid and old_config is not None and disabled == old_config and name not in (current.get('mcpServers') or {}):
                    ok = bool(facade._manage_mcp('enable', name))
            elif action == 'restore_window':
                ok = autocompact_advice.undo_item(facade, item.get('setting') or {})
            elif action == 'lane':
                from importlib import import_module
                module = import_module('.' + item['kind'], __package__)
                ok = bool(module.undo_item(facade, item.get('lane_result') or {}))
            if ok and item.get('trim_id'):
                structural_ledger.close_trim(facade.SNAPSHOT_DIR, item['trim_id'], structural_ledger.now_iso(), 'apply-safe undo')
        except Exception:
            ok = False
        results.append({'id': ident, 'ok': ok})
    try:
        format_ok = _restore_settings_format(facade, backup) if all(row['ok'] for row in results) else False
        result = {'receipt_id': uuid.uuid4().hex, 'undo_of': receipt_id, 'ts': datetime.now().isoformat(),
                  'items': results, 'ok': all(row['ok'] for row in results) and format_ok}
        _append_receipt(facade, result)
        return result
    except (OSError, ValueError, TypeError):
        return {'ok': False, 'error': 'Undo could not be fully recorded.', 'items': results}


def main(facade, args):
    parser = argparse.ArgumentParser(prog='measure.py apply-safe')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--token')
    parser.add_argument('--only')
    parser.add_argument('--undo')
    parser.add_argument('--list', action='store_true')
    options = parser.parse_args(args)
    if options.list:
        print(json.dumps(_receipts(facade), indent=2))
        return 0
    if options.undo:
        result = undo(facade, options.undo)
        print(json.dumps(result, indent=2))
        return 0 if result.get('ok') else 1
    try:
        plan = build_plan(facade)
    except Exception:
        print('Could not build a safe plan from the available history.')
        return 1
    if options.only:
        requested = options.only.split(',')
        available = {item['id'] for item in plan['items']}
        if len(requested) != len(set(requested)) or not set(requested) <= available:
            print('One or more requested items are not in the current plan.')
            return 1
        plan['items'] = [item for item in plan['items'] if item['id'] in requested]
        plan['total_net_usd_month'] = sum(float(item['net_usd_month']) for item in plan['items'] if item.get('net_usd_month') is not None)
        plan['token'] = plan_token(plan['items'])
    if options.json:
        print(json.dumps(plan, indent=2))
    else:
        for item in plan['items']:
            value = item.get('net_usd_month')
            print('{}: {} ({})'.format(item['id'], item['label'], 'unpriced' if value is None else '${:.2f}/month'.format(value)))
            if item.get('basis'):
                print('  ' + str(item['basis']))
        if plan.get('advice'):
            print(plan['advice'])
        print('Plan token: ' + plan['token'])
    selected = [item['id'] for item in plan['items'] if item.get('default_selected', True)]
    if not selected or (options.json and not options.yes):
        return 0
    if options.yes:
        if not options.token:
            print('--yes requires --token from the displayed plan.')
            return 2
        token = options.token
    else:
        if not sys.stdin.isatty():
            return 0
        if input('Apply these {} changes? [y/N] '.format(len(selected))).strip().lower() != 'y':
            return 0
        token = plan_token([item for item in plan['items'] if item['id'] in selected])
    result = apply(facade, plan, selected, token)
    print(json.dumps(result, indent=2))
    return 0 if result.get('ok') else 1
