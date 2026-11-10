"""Read Claude transcripts for skill-list and MCP prefix opportunities.

Character counts describe text observed in transcripts. Tokens are an
approximation, not provider billing. Missing evidence never becomes a saving.
"""
from __future__ import annotations

import json
import os
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _server(name):
    if not isinstance(name, str) or not name.startswith('mcp__'):
        return None
    parts = name.split('__', 2)
    return parts[1] if len(parts) == 3 and parts[1] else None


def _normalize(name):
    return re.sub(r'[^A-Za-z0-9_-]', '_', name)


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (AttributeError, ValueError, TypeError):
        return None


def scan(paths, budget=None, now=None, days=30):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    listing, samples, used, seen_calls = [], {}, set(), set()
    incomplete = False
    for path in paths:
        path = Path(path)
        tools, instructions = {}, {}
        have_listing = False
        files = [path] + sorted((path.parent / path.stem / 'subagents').glob('*.jsonl'))
        for source in files:
            try:
                with source.open(encoding='utf-8', errors='replace') as stream:
                    for line in stream:
                        if not any(tag in line for tag in ('"tool_use"', '"skill_listing"', '"deferred_tools_delta"', '"mcp_instructions_delta"')):
                            continue
                        try:
                            record = json.loads(line)
                        except (ValueError, TypeError):
                            incomplete = True
                            continue
                        if not isinstance(record, dict):
                            continue
                        a = record.get('attachment') or {}
                        if not isinstance(a, dict):
                            continue
                        kind = a.get('type')
                        stamp = _time(record.get('timestamp'))
                        recent = stamp is not None and now - timedelta(days=7) <= stamp <= now
                        if source == path and recent and not have_listing and kind == 'skill_listing' and a.get('isInitial') and isinstance(a.get('content'), str):
                            listing.append((len(a['content']), a.get('skillCount')))
                            have_listing = True
                        if source == path and recent and kind in ('deferred_tools_delta', 'mcp_instructions_delta'):
                            names = a.get('addedNames') or []
                            values = a.get('addedLines' if kind == 'deferred_tools_delta' else 'addedBlocks') or []
                            target = tools if kind == 'deferred_tools_delta' else instructions
                            for name in a.get('removedNames') or []:
                                target.pop(name, None)
                            if kind == 'deferred_tools_delta':
                                for value in values:
                                    if isinstance(value, str):
                                        name = value.split(None, 1)[0] if value.strip() else ''
                                        if _server(name):
                                            target[name] = len(value)
                            elif len(names) == len(values):
                                for name, value in zip(names, values):
                                    if isinstance(name, str) and isinstance(value, str):
                                        target[name] = len(value)
                            else:
                                incomplete = True
                        if record.get('type') != 'assistant':
                            continue
                        message = record.get('message') or {}
                        content = message.get('content') if isinstance(message, dict) else []
                        if not isinstance(content, list):
                            continue
                        for item in content:
                            if not isinstance(item, dict) or item.get('type') != 'tool_use':
                                continue
                            server = _server(item.get('name'))
                            call_id = item.get('id')
                            if server and (stamp is None or cutoff <= stamp <= now):
                                if not call_id or call_id not in seen_calls:
                                    used.add(_normalize(server))
                                if call_id:
                                    seen_calls.add(call_id)
            except OSError:
                incomplete = True
        session = {}
        for name, size in tools.items():
            server = _server(name)
            if server:
                session.setdefault(_normalize(server), [0, 0])[0] += size
        for name, size in instructions.items():
            session.setdefault(_normalize(name), [0, 0])[1] += size
        for server, sizes in session.items():
            samples.setdefault(server, []).append(sizes)
    median = int(statistics.median(size for size, _ in listing)) if listing else None
    try:
        budget = int(budget)
        if budget <= 0:
            budget = None
    except (TypeError, ValueError, OverflowError):
        budget = None
    at_cap = None if median is None or budget is None else median >= budget * .98
    basis = 'configured budget' if at_cap is not None else 'unknown'
    if at_cap is None and median:
        plateau = [(size, count) for size, count in listing if abs(size - median) <= median * .01 and isinstance(count, int) and count > 0]
        counts = [count for _, count in plateau]
        if len(counts) >= 5 and max(counts) >= min(counts) * 1.1:
            at_cap, basis = True, 'observed plateau'
    unused = []
    if not incomplete:
        for server in sorted(set(samples) - used):
            rows = samples[server]
            tool = int(statistics.median(r[0] for r in rows))
            instruction = int(statistics.median(r[1] for r in rows))
            unused.append({'server': server, 'tool_chars': tool, 'instruction_chars': instruction,
                           'estimated_tokens_per_call': (tool + instruction) // 4, 'sessions': len(rows)})
    return {'listing': {'median_chars': median, 'samples': len(listing), 'budget_chars': budget, 'at_cap': at_cap, 'basis': basis},
            'unused_connectors': unused, 'incomplete': incomplete}


def from_facade(facade, days=30):
    if facade.detect_runtime() != 'claude':
        return {'listing': {'at_cap': None}, 'unused_connectors': [], 'incomplete': False}
    budget = os.environ.get('SLASH_COMMAND_TOOL_CHAR_BUDGET')
    if not budget:
        try:
            settings = json.loads((facade.CLAUDE_DIR / 'settings.json').read_text(encoding='utf-8'))
            budget = (settings.get('env') or {}).get('SLASH_COMMAND_TOOL_CHAR_BUDGET')
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    try:
        return scan([row[0] for row in facade._find_all_jsonl_files(days=days)], budget=budget, days=days)
    except (OSError, ValueError, TypeError):
        return {'listing': {'at_cap': None}, 'unused_connectors': [], 'incomplete': True}


def listing_message(check):
    state = check['listing']
    if state.get('at_cap'):
        prefix = 'Skill listing appears at its budget (stable listing despite changing skill counts). ' if state.get('basis') == 'observed plateau' else 'Skill listing is at its character budget. '
        return (prefix + 'Archiving skills frees ~0 tokens while the cap holds; '
                'lower SLASH_COMMAND_TOOL_CHAR_BUDGET for shorter descriptions and weaker triggering.')
    if state.get('at_cap') is None:
        return 'Skill-list savings unknown: the effective character budget or transcript listing was not observed.'
    return 'Skill listing is below the observed character budget; archive savings require a before/after measurement.'


def connector_message(check):
    if check.get('incomplete'):
        return 'Unused connectors unknown: some transcript records could not be read.'
    rows = check['unused_connectors']
    if not rows:
        return 'No observed connector qualifies as unused in readable transcripts from the last 30 days.'
    return 'No calls in the last 30 days (observed transcripts; median prefix in recent sessions where present): ' + '; '.join(
        '{}: ~{} tokens per call ({} tool chars, {} instruction chars)'.format(
            r['server'], r['estimated_tokens_per_call'], r['tool_chars'], r['instruction_chars']) for r in rows)


def quick_win(facade, components, skills, check):
    try:
        trends = facade._collect_trends_data(days=30) or {}
        names = set(skills.get('names', []))
        unused = {n for n in trends.get('skills', {}).get('never_used', [])
                  if n in names and not facade._is_own_tool_skill(n)}
        if len(unused) >= 3:
            detail = listing_message(check)
            return {'action': 'Review skills not invoked in the window', 'savings': 0,
                    'detail': detail, 'extend': 'Savings require an observed listing change.'}
    except Exception:
        pass
    return None


def coach_patterns(check):
    return [
        {'name': 'Skill listing budget', 'severity': 'info', 'detail': listing_message(check),
         'fix': 'Review listing evidence before changing skills.', 'savings': 'Not priced'},
        {'name': 'Unused connectors', 'severity': 'info', 'detail': connector_message(check),
         'fix': 'Review whether observed unused connectors are still needed.', 'savings': 'Estimated prefix tokens only'},
    ]


def unused_savings(components, unused, check):
    if check is not None:
        return listing_message(check)
    measured = 0
    missing = 0
    for name in unused:
        try:
            value = int((components.get('skills_detail', {}).get(name) or {}).get('frontmatter_tokens') or 0)
        except (ValueError, TypeError, OverflowError):
            value = 0
        if value > 0:
            measured += value
        else:
            missing += 1
    if not measured:
        return 'Unknown: per-skill token measurements unavailable for the unused set'
    if missing:
        return '~{:,} tokens measured for the unused set ({} of {} lack per-skill measurement)'.format(measured, missing, len(unused))
    return '~{:,} tokens (measured descriptions of the {} unused skills)'.format(measured, len(unused))


def skill_recommendations(facade, components, trends, usage_basis):
    names = sorted(str(n) for n in (trends or {}).get('skills', {}).get('never_used', [])
                   if not facade._is_own_tool_skill(n))
    if len(names) < 2 and (trends or components.get('skills', {}).get('count', 0) <= 10):
        return []
    display = ', '.join(facade._strip_ansi(n) for n in names[:8]) if names else 'Review installed skills.'
    return ['Review skills not invoked (' + usage_basis + '): ' + display + facade._skill_slim_clause(0, 'claude') +
            '  Archive (harder step) only skills you no longer need. Measure the resulting listing change.']
