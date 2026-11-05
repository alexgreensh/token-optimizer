#!/usr/bin/env python3
"""Capture immutable raw-byte evidence at a fixed disposable payload path.

Expected captures are never updated by comparison. A provisional run is not a
release baseline; use the final BASE source tree for the accepted capture.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
import hashlib
import json
import marshal
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import measure_split_crashes as crashes  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / 'tests/fixtures/measure_split/cases.json'


def compare(expected, actual):
    """No path rewriting, output sorting, dollar rounding, or line stripping."""
    left, right = Path(expected), Path(actual)
    names = {p.relative_to(left).as_posix() for p in left.rglob('*') if p.is_file()}
    other = {p.relative_to(right).as_posix() for p in right.rglob('*') if p.is_file()}
    errors = [f'missing: {name}' for name in sorted(names-other)]
    errors += [f'unexpected: {name}' for name in sorted(other-names)]
    for name in sorted(names & other):
        if (left / name).read_bytes() != (right / name).read_bytes():
            errors.append('bytes differ: ' + name)
    return errors


def fixture(work, variant='synthetic-current'):
    """Each case starts from synthetic data, never from the owner's home."""
    home = work / 'home é with spaces'
    project = home / 'projects' / 'fixture project'
    project.mkdir(parents=True)
    claude = home / '.claude'
    claude.mkdir()
    for relative in ('.codex', '.hermes', '.copilot', '.cursor', '.grok', '.gemini'):
        (home / relative).mkdir()
    (claude / 'settings.json').write_text(json.dumps({'unrelated_user_key': {'keep': True},
                                                    'mcpServers': {'fixture': {'command': 'fixture-mcp'}}}))
    (claude / 'CLAUDE.md').write_text('# Fixture\nKeep edits small.\n')
    for runtime in (claude, home / '.codex'):
        skill = runtime / 'skills/fixture/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('---\nname: fixture\ndescription: A synthetic test skill.\n---\nKeep fixture edits small.\n')
    (home / '.codex/config.toml').write_text('model = "gpt-5.6-sol"\n[mcp_servers.fixture]\ncommand = "fixture-mcp"\n')
    (project / 'CLAUDE.md').write_text('# Fixture project\nKeep local tests bounded.\n')
    (project / 'example.py').write_text('print("fixture")\n')
    config = claude / 'token-optimizer' / 'config.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'v5_welcome_shown': True, 'enterprise_consent_shown': True,
                                  'daemon_enabled': False, 'keepwarm_enabled': False}))
    transcript = claude / 'projects' / '-fixture' / 'fixture-session.jsonl'
    transcript.parent.mkdir(parents=True)
    rows = [
        {'type': 'user', 'sessionId': 'fixture-session', 'uuid': 'u1', 'timestamp': '2026-11-04T23:59:00Z',
         'message': {'role': 'user', 'content': 'continue the fixture task'}},
        {'type': 'assistant', 'sessionId': 'fixture-session', 'uuid': 'a1', 'requestId': 'r1',
         'timestamp': '2026-11-05T00:01:00Z', 'message': {'id': 'm1', 'role': 'assistant',
         'model': 'claude-sonnet-5-5', 'content': [{'type': 'text', 'text': 'Fixture response'}],
         'usage': {'input_tokens': 100, 'output_tokens': 25, 'cache_read_input_tokens': 200,
                   'cache_creation_input_tokens': 40}}},
    ]
    transcript.write_text(''.join(json.dumps(row) + '\n' for row in rows + rows[-1:]))
    child = transcript.parent / 'subagents/agent-fixture.jsonl'
    child.parent.mkdir()
    child_row = dict(rows[-1], uuid='sidechain-a1', requestId='sidechain-r1', isSidechain=True)
    child.write_text(json.dumps(child_row) + '\n')
    codex = home / '.codex/sessions/2026/11/05/rollout-fixture.jsonl'
    codex.parent.mkdir(parents=True)
    codex.write_text(''.join(json.dumps(row) + '\n' for row in (
        {'type': 'session_meta', 'timestamp': '2026-11-05T00:00:00Z', 'payload': {'id': '00000000-0000-4000-8000-000000000002', 'cwd': str(project), 'originator': 'codex_cli_rs'}},
        {'type': 'turn_context', 'timestamp': '2026-11-05T00:01:00Z', 'payload': {'model': 'gpt-5.6-sol', 'cwd': str(project)}},
        {'type': 'event_msg', 'timestamp': '2026-11-05T00:02:00Z', 'payload': {'type': 'token_count', 'info': {
            'total_token_usage': {'input_tokens': 200, 'cached_input_tokens': 100, 'output_tokens': 50, 'reasoning_output_tokens': 10, 'total_tokens': 250},
            'last_token_usage': {'input_tokens': 200, 'cached_input_tokens': 100, 'output_tokens': 50, 'reasoning_output_tokens': 10, 'total_tokens': 250},
            'model_context_window': 258400}}},
    )))
    hermes = {'id': 'hermes-fixture', 'model': 'gpt-5.6-sol', 'started_at': 1793869800,
              'ended_at': 1793870400, 'input_tokens': 1000, 'output_tokens': 100,
              'cache_read_tokens': 500, 'cache_write_tokens': 0, 'message_count': 4,
              'api_call_count': 2, 'cost_status': 'unknown'}
    with closing(sqlite3.connect(home / '.hermes/state.db')) as connection, connection:
        connection.execute('CREATE TABLE sessions (id TEXT, model TEXT, started_at REAL, ended_at REAL, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, message_count INTEGER, api_call_count INTEGER, cost_status TEXT)')
        connection.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)', tuple(hermes.values()))
        assert connection.total_changes == 1
        assert connection.execute('SELECT id FROM sessions').fetchone() == ('hermes-fixture',)
    cursor = {'conversation_id': 'cursor-fixture', 'first_ts': 1793869800, 'updated_at': 1793870400,
              'final': True, 'end_reason': 'sessionEnd', 'turns': 2, 'tool_calls': 1,
              'tool_names': {'Read': 1}, 'cwd': str(project), 'models': {'claude-sonnet-5-5': 2},
              'bubble_tokens': {'input_tokens': 1000, 'output_tokens': 100, 'model': 'claude-sonnet-5-5'},
              'cursor_version': '3.18.9'}
    tally = home / '.cursor/token-optimizer/sessions/cursor-fixture.json'
    tally.parent.mkdir(parents=True)
    tally.write_text(json.dumps(cursor))
    native = {'hermes': hermes, 'cursor': cursor,
              'copilot': {'session_id': 'copilot-fixture', 'models': {'claude-sonnet-5-5': {'input_tokens': 1000, 'output_tokens': 100, 'cache_read_tokens': 500, 'cache_write_tokens': 0, 'requests': 2}}, 'message_count': 4, 'start_time': 1793869800, 'end_time': 1793870400},
              'grok': {'session_id': 'grok-fixture', 'usage': {'turns': 2, 'input_tokens': 1000, 'output_tokens': 100, 'cache_read_tokens': 500}, 'start_time': 1793869800, 'end_time': 1793870400},
              'antigravity': {'conversation_id': 'antigravity-fixture', 'surface': 'antigravity-cli',
                             'generations': [{'input_tokens': 1000, 'output_tokens': 100, 'cache_read_tokens': 500, 'model_display_name': 'Gemini 3.5 Flash (Medium)'}],
                             'input_tokens': 1000, 'output_tokens': 100, 'cache_read_tokens': 500, 'user_input_count': 2,
                             'model_display_name': 'Gemini 3.5 Flash (Medium)', 'start_time': 1793869800, 'end_time': 1793870400, 'workspace': str(project)}}
    (home / 'native-fixtures.json').write_text(json.dumps(native))
    copilot = home / '.copilot/session-state/copilot-fixture'
    copilot.mkdir(parents=True)
    (copilot / 'events.jsonl').write_text(''.join(json.dumps(event)+'\n' for event in (
        {'type': 'session.start', 'timestamp': '2026-11-05T00:00:00Z', 'cwd': str(project), 'version': 'fixture'},
        {'type': 'user.message', 'timestamp': '2026-11-05T00:01:00Z'},
        {'type': 'assistant.message', 'timestamp': '2026-11-05T00:02:00Z', 'outputTokens': 100},
        {'type': 'session.shutdown', 'timestamp': '2026-11-05T00:03:00Z', 'modelMetrics': {
            'claude-sonnet-5-5': {'inputTokens': 1000, 'outputTokens': 100, 'cacheReadTokens': 500, 'apiCalls': 2}}},
    )))
    grok = home / '.grok/sessions/fixture-group/grok-fixture'
    grok.mkdir(parents=True)
    (grok / 'summary.json').write_text(json.dumps({'info': {'id': 'grok-fixture', 'cwd': str(project)},
        'sessionSummary': 'A synthetic Grok session', 'createdAt': '2026-11-05T00:00:00Z',
        'updatedAt': '2026-11-05T00:10:00Z', 'numMessages': 4, 'currentModelId': 'grok-4'}))
    (grok / 'updates.jsonl').write_text(json.dumps({'update': {'sessionUpdate': 'turn_completed',
        'usage': {'inputTokens': 1000, 'outputTokens': 100, 'cachedReadTokens': 500, 'modelCalls': 2}}}) + '\n')
    if variant == 'legacy':
        data = home / 'data'
        data.mkdir()
        with closing(sqlite3.connect(data / 'trends.db')) as connection, connection:
            connection.execute('CREATE TABLE session_log (id INTEGER PRIMARY KEY, jsonl_path TEXT UNIQUE, date TEXT NOT NULL, project TEXT, duration_minutes REAL, input_tokens INTEGER, output_tokens INTEGER, message_count INTEGER, api_calls INTEGER, cache_hit_rate REAL, skills_json TEXT, subagents_json TEXT, tool_calls_json TEXT, model_usage_json TEXT, version TEXT, slug TEXT, topic TEXT, collected_at TEXT)')
            connection.execute("INSERT INTO session_log (jsonl_path,date,input_tokens,output_tokens) VALUES ('missing-fixture.jsonl','2026-11-04',100,10)")
            assert connection.total_changes == 1
            assert connection.execute('SELECT input_tokens FROM session_log').fetchone() == (100,)
    if variant == 'locked-settings':
        (claude / '.settings.lease').write_text(json.dumps({'pid': 4241, 'nonce': 'fixture-live-owner',
            'released': 0, 'created_wall': 1793870400, 'reuse_wall': 1793874000, 'expires_wall': 1793874000}))
    if variant == 'readonly-settings':
        (claude / 'settings.json').chmod(0o444)
    for path in home.rglob('*'):
        os.utime(path, (1793870400, 1793870400))
    return home, project


def environment(home, payload, work):
    env = {key: value for key, value in os.environ.items()
           if key.upper() in ('PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'SYSTEMDRIVE')}
    env.update({'HOME': str(home), 'USERPROFILE': str(home), 'CLAUDE_CONFIG_DIR': str(home / '.claude'),
                'TO_SPLIT_REAL_HOME': str(Path.home()),
                'XDG_CONFIG_HOME': str(home / '.config'), 'XDG_CACHE_HOME': str(home / '.cache'),
                'XDG_DATA_HOME': str(home / '.local/share'), 'XDG_STATE_HOME': str(home / '.local/state'),
                'LOCALAPPDATA': str(home / 'AppData/Local'), 'CODEX_HOME': str(home / '.codex'),
                'TOKEN_OPTIMIZER_SNAPSHOT_DIR': str(home / 'data'), 'TOKEN_OPTIMIZER_NO_PROC_SCAN': '1',
                'TOKEN_OPTIMIZER_COLD_COMPILE_WAIT_S': '-1', 'TOKEN_OPTIMIZER_REMOTE_LEAN': '1',
                'CLAUDE_PLUGIN_ROOT': str(payload), 'PYTHONHASHSEED': '0', 'TZ': 'UTC',
                'LC_ALL': 'C', 'LANG': 'C', 'USER': 'fixture', 'LOGNAME': 'fixture',
                'NO_COLOR': '1', 'TERM': 'dumb', 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                'PYTHONPYCACHEPREFIX': str(work / 'pycache'),
                'TMPDIR': str(work / 'tmp'), 'TEMP': str(work / 'tmp'), 'TMP': str(work / 'tmp')})
    if os.name == 'nt':
        env['HOMEDRIVE'] = home.drive
        env['HOMEPATH'] = str(home)[len(home.drive):]
    (work / 'tmp').mkdir()
    return env


def snapshot(home, output):
    files = output / 'files'
    files.mkdir()
    databases = {}
    for path in sorted(home.rglob('*')):
        if not path.is_file():
            continue
        relative = str(path.relative_to(home))
        if path.suffix in ('.db', '.sqlite', '.sqlite3'):
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
                tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                databases[relative] = {'schema': conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall(),
                                       'tables': {name: sorted(conn.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall(), key=repr)
                                                  for name in tables}}
        elif path.suffix not in ('.pyc',) and not path.name.endswith(('-wal', '-shm', '-journal')):
            dest = files / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
    (output / 'database.json').write_text(json.dumps(databases, ensure_ascii=False, default=repr) + '\n')


def capture(source, workspace, output, cases, *, compiled_cache=True):
    workspace, output = Path(workspace).resolve(), Path(output).resolve()
    if not workspace.is_relative_to((ROOT / '.scratch-u0').resolve()):
        raise ValueError('disposable payload workspace must be under .scratch-u0')
    if output.exists():
        raise ValueError('capture already exists, choose a fresh evidence directory')
    output.mkdir(parents=True)
    payload = workspace / 'payload'
    if payload.exists():
        shutil.rmtree(payload)
    payload.mkdir(parents=True)
    for folder in ('skills', 'hooks', '.claude-plugin', '.codex-plugin', '.codex',
                   'hermes', 'copilot', 'cursor', 'antigravity', 'devin'):
        if (source / folder).is_dir():
            shutil.copytree(source / folder, payload / folder,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'node_modules', '.venv'))
    for name in ('install.sh', 'package.json', 'LICENSE', 'PRIVACY.md', 'README.md'):
        if (source / name).is_file():
            shutil.copyfile(source / name, payload / name)
    for path in payload.rglob('*'):
        os.utime(path, (1793870400, 1793870400))
    code_cache = workspace / 'compiled-fixture.marshal'
    code_cache.write_bytes(marshal.dumps({str(path.resolve()): compile(path.read_bytes(), str(path.resolve()), 'exec', dont_inherit=True)
                                          for folder in ('skills/token-optimizer/scripts', 'hooks')
                                          for path in (payload / folder).rglob('*.py')}))
    for row in cases:
        work = workspace / 'case'
        if work.exists():
            def writable_remove(function, path, _error):
                Path(path).chmod(0o700)
                function(path)
            shutil.rmtree(work, onerror=writable_remove)
        work.mkdir()
        home, project = fixture(work, row['fixture'])
        env = environment(home, payload, work)
        if compiled_cache and row['entry'] != 'perf':
            env['TO_SPLIT_CODE_CACHE'] = str(code_cache)
        env['TOKEN_OPTIMIZER_RUNTIME'] = row['runtime']
        if row.get('fixture') in ('seeded-current', 'fresh-cache', 'stale-cache', 'busy-db'):
            seed = subprocess.run([sys.executable, str(ROOT / 'scripts/measure_split_probe.py'), str(payload),
                                   str(work / 'seed-effects.json'), 'seed', row['fixture']],
                                  input=b'', cwd=project, env=env, capture_output=True, timeout=60)
            if seed.returncode or seed.stdout != b'seed-ready\n':
                raise RuntimeError(f'fixture preparation failed: exit={seed.returncode} stdout={seed.stdout[-500:]!r} '
                                   + seed.stderr.decode('utf-8', 'replace'))
            # Seeded checkpoints are fixture inputs too. Keep intentional stale
            # cache times; newly created files otherwise inherit the host clock.
            for path in home.rglob('*'):
                stamp = 1793784000 if path.stat().st_mtime == 1793784000 else 1793870400
                os.utime(path, (stamp, stamp))
        if row.get('host_variant') == 'cowork':
            env['TOKEN_OPTIMIZER_REMOTE_HOST'] = 'cowork'
        if row.get('input_variant') == 'deadline':
            env['TO_SPLIT_MONOTONIC_STEP'] = '30'
        record = output / row['id']
        record.mkdir()
        argv = [sys.executable, str(ROOT / 'scripts/measure_split_probe.py'), str(payload),
                str(work / 'effects.json'), row['entry'], *row['argv']]
        if row['entry'] == 'node':
            node = shutil.which('node')
            if not node:
                raise RuntimeError('Node is required for statusline golden cases')
            argv = [node, '-r', str(ROOT / 'tests/fixtures/measure_split/node-clock.cjs'),
                    str(payload / 'skills/token-optimizer/scripts/statusline.js')]
            env['TO_SPLIT_NODE_EFFECTS'] = str(work / 'effects.json')
            env['TO_SPLIT_NODE_WORK'] = str(work)
        stdin = base64.b64decode(row['stdin_base64'])
        if row.get('hook_event') and row.get('input_variant') in ('success', 'deadline'):
            codex = row['runtime'] == 'codex'
            transcript = next((home / '.codex/sessions').rglob('*.jsonl')) if codex else home / '.claude/projects/-fixture/fixture-session.jsonl'
            stdin = json.dumps({'hook_event_name': row['hook_event'],
                                'session_id': '00000000-0000-4000-8000-000000000002' if codex else 'fixture-session',
                                'cwd': str(project), 'transcript_path': str(transcript),
                                'prompt': 'continue the fixture task', 'source': 'startup',
                                'tool_name': 'Read', 'tool_input': {'file_path': str(project / 'example.py')},
                                'tool_response': 'fixture tool output'}).encode()
        if row['entry'] == 'node':
            stdin = json.dumps({'session_id': 'fixture-session', 'workspace': {'current_dir': str(project)},
                'model': {'id': 'claude-sonnet-5-5', 'display_name': 'Sonnet'},
                'context_window': {'context_window_size': 200000, 'used_percentage': 10,
                                   'current_usage': {'input_tokens': 1000, 'cache_read_input_tokens': 19000}},
                'cost': {'total_cost_usd': 0.05, 'total_duration_ms': 60000}}).encode()
        busy = None
        if row.get('fixture') == 'busy-db':
            busy = sqlite3.connect(home / 'data/trends.db')
        try:
            if busy is not None:
                busy.execute('BEGIN EXCLUSIVE')
            proc = subprocess.run(argv, input=stdin, cwd=project,
                                  env=env, capture_output=True, timeout=row.get('timeout', 30))
        finally:
            if busy is not None:
                try:
                    busy.rollback()
                finally:
                    busy.close()
        (record / 'stdout').write_bytes(proc.stdout)
        (record / 'stderr').write_bytes(proc.stderr)
        (record / 'exit').write_text(str(proc.returncode) + '\n')
        if (row['entry'] == 'cli' and b'Traceback (most recent call last)' in proc.stderr
                and crashes.known_crash(row['id'], proc.stderr) is None):
            raise RuntimeError(row['id'] + ': unexpected traceback in CLI fixture: ' + proc.stderr.decode('utf-8', 'replace'))
        if (work / 'http-console.stdout').exists():
            (record / 'server.stdout').write_bytes((work / 'http-console.stdout').read_bytes())
        if row['entry'] in ('api', 'exports', 'node', 'http') and proc.returncode:
            raise RuntimeError(row['id'] + ': harness case failed: ' + proc.stderr.decode('utf-8', 'replace'))
        if row['entry'] == 'http':
            expected = {'unauthorized': 403, 'malformed': 400}.get(row['argv'][1], 200)
            if not proc.stdout.startswith(('HTTP/1.0 ' + str(expected)).encode()):
                raise RuntimeError(row['id'] + ': unexpected HTTP status: ' + repr(proc.stdout[:2000]))
        effects = work / 'effects.json'
        combined = json.loads(effects.read_text()) if effects.exists() else []
        for child in sorted(work.glob('effects-child-*.json')):
            combined.extend(json.loads(child.read_text()))
        (record / 'effects.json').write_text(json.dumps(combined, ensure_ascii=False) + '\n')
        snapshot(home, record)
        if (row.get('hook_event') == 'PreCompact' and row.get('input_variant') == 'success'
                and 'compact-capture' in row['argv']):
            if not any('checkpoints' in path.parts and path.suffix == '.md' for path in home.rglob('*.md')):
                raise RuntimeError('PreCompact returned without persisting its checkpoint')
    return len(cases)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--capture', type=Path)
    parser.add_argument('--compare', nargs=2, type=Path)
    parser.add_argument('--case', action='append')
    args = parser.parse_args()
    if args.compare:
        errors = compare(*args.compare)
        print('\n'.join(errors) if errors else 'raw-byte equality')
        return bool(errors)
    rows = json.loads(CASES.read_text())['cases']
    if args.case:
        rows = [row for row in rows if row['id'] in args.case]
    if not rows or not args.capture:
        parser.error('capture needs an output and at least one case')
    print(f'captured {capture(args.source.resolve(), args.workspace, args.capture, rows)} cases')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
