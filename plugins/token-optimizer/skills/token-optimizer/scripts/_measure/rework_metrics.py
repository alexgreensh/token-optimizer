#!/usr/bin/env python3
"""Transcript-only rework diagnostics. Observational differences are not savings.

Input is an explicit JSON manifest of sessions with path, project and arm (on/off).
No transcript content or file names are emitted. No product data is modified.
"""
import argparse
import bisect
import json
import random
import re
import statistics
from collections import defaultdict


def _blocks(row):
    content = (row.get('message') or {}).get('content', [])
    return content if isinstance(content, list) else []


def _main(row):
    return not row.get('isSidechain') and not row.get('isMeta')


def hook_statuses(rows):
    statuses = {}
    for row in rows:
        if not _main(row) or row.get('type') != 'attachment':
            continue
        a = row.get('attachment') or {}
        if a.get('hookName') != 'PostToolUse:Bash' or not a.get('toolUseID'):
            continue
        output = str(a.get('stdout') or '') + str(a.get('content') or '')
        stderr = str(a.get('stderr') or '')
        key = a['toolUseID']
        if 'bash_compress_hook budget exceeded' in stderr:
            statuses[key] = 'timeout'
        elif statuses.get(key) != 'timeout' and ('[Full result archived' in output or '[bash_compress]' in output):
            statuses[key] = 'compressed'
    return statuses


def session_metrics(rows, window=10):
    if window < 1:
        raise ValueError('window must be positive')
    rows = [r for r in rows if _main(r)]
    failures = set()
    for row in rows:
        for b in _blocks(row):
            if b.get('type') == 'tool_result':
                match = re.search(r'(?i)\bexit code\s*:?\s*(\d+)\b', str(b.get('content') or ''))
                if match and int(match.group(1)) != 0 and b.get('is_error') is True:
                    failures.add(b.get('tool_use_id'))
        result = row.get('toolUseResult')
        if isinstance(result, dict) and isinstance(result.get('exitCode'), int) and result['exitCode'] != 0:
            for b in _blocks(row):
                if b.get('type') == 'tool_result':
                    failures.add(b.get('tool_use_id'))
    turns = calls = requests = edits = repeated_failures = compactions = loops = 0
    seen_messages, seen_tools, last_edit, last_failed = set(), set(), {}, {}
    events = []
    for row in rows:
        typ = row.get('type')
        if typ == 'system' and row.get('subtype') == 'compact_boundary':
            compactions += 1
        if typ == 'attachment':
            a = row.get('attachment') or {}
            if 'loop_detection' in str(a.get('stderr') or '') + str(a.get('stdout') or ''):
                loops += 1
        if typ == 'user':
            content = (row.get('message') or {}).get('content')
            if (isinstance(content, str) and content.strip()) or any(b.get('type') == 'text' and str(b.get('text', '')).strip() for b in _blocks(row)):
                requests += 1
        if typ != 'assistant':
            continue
        message = row.get('message') or {}
        mid = message.get('id') or row.get('message_id') or row.get('uuid')
        if mid is None or mid not in seen_messages:
            turns += 1
            if mid is not None:
                seen_messages.add(mid)
        for b in _blocks(row):
            if b.get('type') != 'tool_use' or b.get('id') in seen_tools:
                continue
            tid, name, args = b.get('id'), b.get('name'), b.get('input') or {}
            if tid is not None:
                seen_tools.add(tid)
            calls += 1
            repeat_edit = repeat_failure = False
            if name in ('Edit', 'Write', 'MultiEdit') and args.get('file_path'):
                key = args['file_path']
                repeat_edit = key in last_edit and turns - last_edit[key] <= window
                edits += repeat_edit
                last_edit[key] = turns
            if name == 'Bash' and args.get('command') and tid in failures:
                key = args['command']
                repeat_failure = key in last_failed and turns - last_failed[key] <= window
                repeated_failures += repeat_failure
                last_failed[key] = turns
            events.append({'id': tid, 'turn': turns, 'repeat_edit': int(repeat_edit), 'repeat_failure': int(repeat_failure)})
    return {'turns': turns, 'tool_calls': calls, 'user_requests': requests,
            'repeat_edits': edits, 'repeat_failures': repeated_failures,
            'compactions': compactions, 'loop_detections': loops,
            'tool_calls_per_request': calls / requests if requests else None,
            'turns_per_request': turns / requests if requests else None,
            'compactions_per_100_calls': 100 * compactions / calls if calls else None,
            'events': events}


def _interval(values, bootstrap, seed):
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    n = len(values)
    samples = sorted(sum(rng.choice(values) for _ in range(n)) / n for _ in range(bootstrap))
    return [samples[int(.025 * (bootstrap - 1))], samples[int(.975 * (bootstrap - 1))]]


def matched_effect(rows, metric, bootstrap=2000, seed=0):
    # Greedy nearest-size matches, no reuse, same project, within 2x size.
    on = sorted((r for r in rows if r['arm'] == 'on' and r.get(metric) is not None), key=lambda r: (r['project'], r['size']))
    off = [r for r in rows if r['arm'] == 'off' and r.get(metric) is not None]
    differences = []
    for r in on:
        candidates = [(abs(r['size'] - x['size']), i) for i, x in enumerate(off)
                      if x['project'] == r['project'] and min(r['size'], x['size']) > 0
                      and max(r['size'], x['size']) <= 2 * min(r['size'], x['size'])]
        if not candidates:
            continue
        _, i = min(candidates)
        other = off.pop(i)
        differences.append(r[metric] - other[metric])
    return {'pairs': len(differences), 'difference': statistics.mean(differences) if differences else None,
            'interval_95': _interval(differences, bootstrap, seed)}


def cloud_effect(sessions, window=10, bootstrap=2000, seed=0):
    # Paired within session, then resampled by session to avoid treating calls as independent.
    pairs = []
    for rows in sessions:
        statuses = hook_statuses(rows)
        events = session_metrics(rows, window)['events']
        groups = defaultdict(list)
        turns = [e['turn'] for e in events]
        prefix = [0]
        for e in events:
            prefix.append(prefix[-1] + e['repeat_edit'] + e['repeat_failure'])
        final_turn = session_metrics(rows, window)['turns']
        for e in events:
            status = statuses.get(e['id'])
            if status and e['turn'] + window <= final_turn:
                lo = bisect.bisect_right(turns, e['turn'])
                hi = bisect.bisect_right(turns, e['turn'] + window)
                groups[status].append(prefix[hi] - prefix[lo])
        if groups['timeout'] and groups['compressed']:
            pairs.append(statistics.mean(groups['timeout']) - statistics.mean(groups['compressed']))
    return {'sessions': len(pairs), 'difference': statistics.mean(pairs) if pairs else None,
            'interval_95': _interval(pairs, bootstrap, seed)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', help='JSON array: path, project, arm; optional cloud boolean')
    parser.add_argument('--window', type=int, default=10)
    args = parser.parse_args()
    with open(args.manifest, encoding='utf-8') as f:
        manifest = json.load(f)
    data, cloud_pairs = [], []
    for entry in manifest:
        with open(entry['path'], encoding='utf-8') as f:
            rows = []
            for line in f:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        metrics = session_metrics(rows, args.window)
        metrics.update(project=entry['project'], arm=entry['arm'], size=metrics['tool_calls'])
        data.append(metrics)
        if entry.get('cloud'):
            effect = cloud_effect([rows], args.window)
            if effect['sessions']:
                cloud_pairs.append(effect['difference'])
    names = ('repeat_edits', 'repeat_failures', 'tool_calls_per_request', 'turns_per_request', 'compactions_per_100_calls', 'loop_detections')
    print(json.dumps({'sessions': len(data), 'window_turns': args.window,
                      'matched_on_minus_off': {name: matched_effect(data, name) for name in names},
                      'cloud_timeout_minus_compressed': {'sessions': len(cloud_pairs),
                          'difference': statistics.mean(cloud_pairs) if cloud_pairs else None,
                          'interval_95': _interval(cloud_pairs, 2000, 0)},
                      'note': 'Diagnostic only. Observational comparisons do not establish causality or savings.'}, indent=2))


if __name__ == '__main__':
    main()
