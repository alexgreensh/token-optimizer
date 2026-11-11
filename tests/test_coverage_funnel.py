"""Bash coverage funnel (W1): counters, summary, replay. Synthetic fixtures only.

Every test runs against a scratch snapshot dir and a scratch HOME. Nothing here
reads a real transcript, ledger or settings file.
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skills/token-optimizer/scripts'
HOOK = SCRIPTS / 'bash_compress_hook.py'
sys.path.insert(0, str(SCRIPTS))
import coverage_funnel  # noqa: E402
from _measure import coverage_report  # noqa: E402

SENTINEL = 'zq9sentinelpath'
SESSION = '11111111-2222-3333-4444-555555555555'

LONG_LS = '\n'.join(
    f'-rw-r--r--  1 user  staff  {1000 + i:>6} Aug {10 + (i % 20):>2} 10:{i % 60:02d} file_{i:04d}.py'
    for i in range(1, 201))
PLAIN = '\n'.join(f'line number {i} of a short plain output' for i in range(1, 6))
ERRORS = '\n'.join(f'FAILED tests/test_mod_{i}.py::test_case - AssertionError: boom' for i in range(20)) \
    + '\nTraceback (most recent call last):\n  File x\nValueError: bad\n'
BUILD = '\n'.join(f'   Compiling crate{i} v0.{i}.0' for i in range(80)) + '\n    Finished dev target(s) in 12.3s\n'

# name -> (command, stdout, exit_code, expected gate, expected reason)
CASES = {
    'short': ('ls', 'a\n', None, 'error_or_short', 'short'),
    'failed': ('ls', PLAIN, 1, 'error_or_short', 'failed_command'),
    'error_words': ('ls', ERRORS, None, 'error_or_short', 'error_words'),
    'marker': ('ls', 'x' * 120 + '\n[bash_compress] done', None, 'already_compressed', 'marker'),
    # `cd /p; ls` used to be the not-read-only example. The post-run classifier is on by
    # default now, so that prefix no longer hides `ls` from its compressor; a redirect still does.
    'not_read_only': (f'ls > /{SENTINEL}/out', PLAIN, None, 'not_read_only', 'file-redirect-write'),
    'prefixed_but_eligible': (f'cd /{SENTINEL}; ls', PLAIN, None, 'under_10pct', 'read_only'),
    'under_10pct': ('ls', PLAIN, None, 'under_10pct', 'read_only'),
    'compressed': ('ls -la', LONG_LS, None, 'compressed', 'read_only'),
    'build_compressed': ('make all', BUILD, None, 'compressed', 'build_output'),
}


def _hook(tmp_path, command, stdout, exit_code=None):
    env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_PLUGIN_ROOT'}
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    env.update(HOME=str(home), CLAUDE_CONFIG_DIR=str(home / '.claude'), CLAUDE_SESSION_ID=SESSION,
               TOKEN_OPTIMIZER_SNAPSHOT_DIR=str(tmp_path / 'snap'), PYTHONDONTWRITEBYTECODE='1')
    response = {'stdout': stdout, 'stderr': '', 'interrupted': False, 'isImage': False}
    if exit_code is not None:
        response['exit_code'] = exit_code
    payload = {'session_id': SESSION, 'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
               'tool_input': {'command': command}, 'tool_response': response,
               'tool_use_id': 'toolu_synthetic'}
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True,
                          text=True, env=env, timeout=60)


def _rows(tmp_path):
    db = tmp_path / 'snap' / 'trends.db'
    if not db.exists():
        return []
    with sqlite3.connect(db) as conn:
        try:
            return conn.execute('SELECT gate, reason, pattern, tokens, removed_tokens, session_uuid, '
                                'timestamp FROM bash_coverage ORDER BY id').fetchall()
        except sqlite3.OperationalError:
            return []


@pytest.mark.parametrize('name', sorted(CASES))
def test_each_gate_writes_exactly_one_row(tmp_path, name):
    command, stdout, code, gate, reason = CASES[name]
    proc = _hook(tmp_path, command, stdout, code)
    assert proc.returncode == 0, proc.stderr
    rows = _rows(tmp_path)
    assert len(rows) == 1, rows
    got_gate, got_reason, _pattern, tokens, removed, session, _ts = rows[0]
    assert (got_gate, got_reason) == (gate, reason)
    assert tokens > 0 and removed >= 0
    assert (removed > 0) == (gate == 'compressed')
    assert session == SESSION


@pytest.mark.parametrize('name', sorted(CASES))
def test_replay_classifier_agrees_with_the_hook(tmp_path, name):
    """One gate order in two places: the replay must say what the hook did."""
    command, stdout, code, _gate, _reason = CASES[name]
    _hook(tmp_path, command, stdout, code)
    hook_row = _rows(tmp_path)[0]
    replay = coverage_funnel.classify_result(command, stdout, is_error=bool(code))
    assert (replay['gate'], replay['reason']) == (hook_row[0], hook_row[1])
    assert replay['pattern'] == hook_row[2]
    assert abs(replay['tokens'] - hook_row[3]) <= max(2, hook_row[3] // 50)


def test_every_gate_is_covered_by_a_case():
    assert {c[3] for c in CASES.values()} == set(coverage_funnel.GATES)
    assert len(coverage_funnel.GATES) == 5


def _hook_without_counting(tmp_path, command, stdout):
    """Same hook code with the counter writer stubbed out, to prove counting changes nothing."""
    home = tmp_path / 'home'
    home.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_PLUGIN_ROOT'}
    env.update(HOME=str(home), CLAUDE_CONFIG_DIR=str(home / '.claude'), CLAUDE_SESSION_ID=SESSION,
               TOKEN_OPTIMIZER_SNAPSHOT_DIR=str(tmp_path / 'snap'), PYTHONDONTWRITEBYTECODE='1')
    payload = {'session_id': SESSION, 'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
               'tool_input': {'command': command},
               'tool_response': {'stdout': stdout, 'stderr': '', 'interrupted': False, 'isImage': False},
               'tool_use_id': 'toolu_synthetic'}
    code = ("import sys, types; m = types.ModuleType('coverage_funnel'); m.record = lambda *a, **k: None; "
            f"sys.modules['coverage_funnel'] = m; sys.path.insert(0, {str(SCRIPTS)!r}); "
            "import bash_compress_hook as h; h.main()")
    return subprocess.run([sys.executable, '-c', code], input=json.dumps(payload), capture_output=True,
                          text=True, env=env, timeout=60)


def test_replay_keeps_the_hooks_mixed_output_safety(tmp_path):
    """Two producers in one command are never rendered as one: hook and replay both say mixed_output."""
    command = f'ls /{SENTINEL}; rm /{SENTINEL}'
    _hook(tmp_path, command, LONG_LS, None)
    hook_row = _rows(tmp_path)[0]
    replay = coverage_funnel.classify_result(command, LONG_LS)
    assert (hook_row[0], hook_row[1]) == ('not_read_only', 'mixed_output')
    assert (replay['gate'], replay['reason']) == ('not_read_only', 'mixed_output')


@pytest.mark.parametrize('name', ['compressed', 'under_10pct', 'not_read_only', 'build_compressed', 'error_words'])
def test_counting_does_not_change_what_the_model_sees(tmp_path, name):
    import re
    command, stdout, code, *_ = CASES[name]
    (tmp_path / 'a').mkdir()
    (tmp_path / 'b').mkdir()
    counted = _hook(tmp_path / 'a', command, stdout, code)
    plain = _hook_without_counting(tmp_path / 'b', command, stdout)
    key = re.compile(r'expand [0-9a-f]{16}')
    assert key.sub('expand K', counted.stdout) == key.sub('expand K', plain.stdout)
    assert counted.returncode == plain.returncode == 0
    assert len(_rows(tmp_path / 'a')) == 1 and _rows(tmp_path / 'b') == []


def test_rows_hold_no_command_or_output_text(tmp_path):
    for name in ('not_read_only', 'compressed', 'build_compressed', 'under_10pct'):
        command, stdout, code, *_ = CASES[name]
        _hook(tmp_path, command + f' --token={SENTINEL}', stdout + f'\n{SENTINEL}', code)
    assert len(_rows(tmp_path)) == 4
    with sqlite3.connect(tmp_path / 'snap' / 'trends.db') as conn:
        dump = '\n'.join(conn.iterdump())
    assert SENTINEL not in dump and 'file_0001' not in dump


def test_unwritable_ledger_never_breaks_the_hook(tmp_path):
    (tmp_path / 'snap').write_text('a file where the directory should be')
    proc = _hook(tmp_path, 'ls -la', LONG_LS)
    assert proc.returncode == 0
    assert 'updatedToolOutput' in proc.stdout


def test_record_ignores_unknown_gate_and_never_raises(tmp_path):
    coverage_funnel.record('nonsense', snapshot_dir=tmp_path)
    coverage_funnel.record('compressed', tokens='x', snapshot_dir=tmp_path / 'f' / 'g')
    assert not (tmp_path / 'trends.db').exists()


def test_reject_category_drops_command_tokens():
    f = coverage_funnel.reject_category
    assert f('stage-1:not-whitelisted:cd') == 'not-whitelisted'
    assert f('glued-operator-in-token:/secret/path;') == 'glued-operator-in-token'
    assert f('contains-command-substitution') == 'contains-command-substitution'
    assert f(None) == 'unknown' and f('') == 'unknown'


def _seed(tmp_path, rows, table=True):
    db = tmp_path / 'trends.db'
    conn = sqlite3.connect(db)
    if table:
        conn.executescript(coverage_funnel._SCHEMA)
        conn.executemany('INSERT INTO bash_coverage (timestamp, gate, reason, pattern, tokens, removed_tokens) '
                         'VALUES (?,?,?,?,?,?)', rows)
    conn.commit()
    conn.close()
    return db


NOW = datetime(2030, 3, 1, 12, 0, 0)


def _ts(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


def test_shares_sum_to_one_and_each_row_counts_once(tmp_path):
    rows = [(_ts(1), 'not_read_only', 'glued-operator-in-token', '', 900, 0),
            (_ts(1), 'not_read_only', 'contains-newline-injection', '', 300, 0),
            (_ts(2), 'under_10pct', 'read_only', 'ls', 49, 0),
            (_ts(2), 'already_compressed', 'marker', '', 40, 0),
            (_ts(3), 'compressed', 'read_only', 'git_diff', 5, 3),
            (_ts(3), 'error_or_short', 'short', '', 6, 0)]
    s = coverage_report.collect(_seed(tmp_path, rows), now=NOW)
    assert s['available'] and not s['empty']
    assert s['results'] == 6 and s['tokens'] == 1300
    assert abs(sum(g['share'] for g in s['gates']) - 1.0) < 1e-9
    assert [g['gate'] for g in s['gates']] == list(coverage_funnel.GATES)
    assert {g['gate']: g['results'] for g in s['gates']} == {
        'not_read_only': 2, 'under_10pct': 1, 'already_compressed': 1, 'compressed': 1, 'error_or_short': 1}
    assert s['top_reject']['reason'] == 'glued-operator-in-token'
    assert abs(s['not_eligible_share'] - 1200 / 1300) < 1e-9
    assert abs(s['acted_share'] - 5 / 1300) < 1e-9
    assert s['removed_tokens'] == 3
    line = coverage_report.headline(s)
    assert 'acted on' in line and 'Not eligible' in line and 'glued-operator-in-token' in line
    assert '—' not in line


def test_window_excludes_old_rows(tmp_path):
    rows = [(_ts(1), 'compressed', 'read_only', 'ls', 10, 5), (_ts(90), 'not_read_only', 'x', '', 999, 0)]
    s = coverage_report.collect(_seed(tmp_path, rows), days=30, now=NOW)
    assert s['results'] == 1 and s['not_eligible_share'] == 0.0


def test_missing_db_or_table_is_unknown_not_zero(tmp_path):
    assert coverage_report.collect(tmp_path / 'none.db')['available'] is False
    (tmp_path / 'n').mkdir()
    assert coverage_report.collect(_seed(tmp_path / 'n', [], table=False))['available'] is False
    (tmp_path / 'e').mkdir()
    empty = coverage_report.collect(_seed(tmp_path / 'e', []), now=NOW)
    assert empty['available'] and empty['empty']
    assert 'No command output' in coverage_report.headline(empty)
    assert 'unknown' in coverage_report.headline(coverage_report.unavailable('x'))


def test_collect_never_writes(tmp_path):
    db = _seed(tmp_path, [(_ts(1), 'compressed', 'read_only', 'ls', 10, 5)])
    before = db.read_bytes()
    coverage_report.collect(db, now=NOW)
    assert db.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ['trends.db']


# ---- replay ---------------------------------------------------------------

def _transcript(path, calls):
    """calls: list of (command, output, is_error). Writes the Claude Code row shape."""
    lines = []
    for i, (cmd, out, err) in enumerate(calls):
        tid = f'toolu_{i}'
        lines.append({'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': tid, 'name': 'Bash', 'input': {'command': cmd}}]}})
        content = out if i % 2 == 0 else [{'type': 'text', 'text': out}]
        lines.append({'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': tid, 'content': content, 'is_error': err}]}})
    lines.append({'type': 'assistant', 'message': {'content': [
        {'type': 'tool_use', 'id': 'toolu_other', 'name': 'Read', 'input': {'file_path': '/x'}}]}})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(json.dumps(r) for r in lines) + '\nnot json\n', encoding='utf-8')


def _projects(tmp_path):
    root = tmp_path / 'projects'
    calls = [(c[0], c[1], c[2] == 1) for n, c in sorted(CASES.items())]
    _transcript(root / 'proj-a' / 'one.jsonl', calls)
    _transcript(root / 'proj-a' / 'sub' / 'two.jsonl', [CASES['compressed'][:2] + (False,)])
    return root


def _tree_hash(root):
    h = hashlib.sha256()
    for p in sorted(root.rglob('*')):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
            h.update(str(p.stat().st_mtime_ns).encode())
    return h.hexdigest()


def test_replay_counts_each_gate_and_is_read_only(tmp_path):
    root = _projects(tmp_path)
    before = _tree_hash(root)
    s = coverage_report.replay(root, days=36500)
    assert _tree_hash(root) == before
    assert s['results'] == len(CASES) + 1 and s['files'] == 2
    by = {g['gate']: g['results'] for g in s['gates']}
    assert by == {'not_read_only': 1, 'under_10pct': 2, 'already_compressed': 1,
                  'compressed': 3, 'error_or_short': 3}
    assert abs(sum(g['share'] for g in s['gates']) - 1.0) < 1e-9
    assert s['truncated'] is False


def test_replay_limit_and_missing_folder(tmp_path):
    root = _projects(tmp_path)
    s = coverage_report.replay(root, days=36500, max_results=3)
    assert s['results'] == 3 and s['truncated'] is True
    assert coverage_report.replay(tmp_path / 'nope')['available'] is False


def test_replay_output_has_no_transcript_text(tmp_path, capsys):
    root = _projects(tmp_path)
    assert coverage_report.replay_cli(['--days', '36500'], root) == 0
    out = capsys.readouterr().out
    assert SENTINEL not in out and 'file_0001' not in out and 'Compiling crate' not in out
    assert 'Command shape not eligible' in out and 'read-only' in out
    assert coverage_report.replay_cli(['--days', '36500', '--json'], root) == 0
    assert SENTINEL not in capsys.readouterr().out


def test_cli_command_runs_end_to_end_under_a_scratch_home(tmp_path):
    home = tmp_path / 'home'
    projects = home / '.claude' / 'projects'
    root = _projects(tmp_path)
    projects.parent.mkdir(parents=True)
    root.rename(projects)
    env = {k: v for k, v in os.environ.items() if k not in ('CLAUDE_PLUGIN_ROOT', 'TOKEN_OPTIMIZER_RUNTIME')}
    env.update(HOME=str(home), TOKEN_OPTIMIZER_SNAPSHOT_DIR=str(tmp_path / 'snap'), PYTHONDONTWRITEBYTECODE='1')
    env.pop('CLAUDE_CONFIG_DIR', None)
    before = _tree_hash(projects)
    r = subprocess.run([sys.executable, str(SCRIPTS / 'measure.py'), 'coverage-replay', '--days', '36500', '--json'],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout[r.stdout.index('{'):])
    assert data['results'] == len(CASES) + 1
    assert _tree_hash(projects) == before


# ---- dashboard wiring -----------------------------------------------------

def test_renderer_handles_states(tmp_path):
    node = subprocess.run(['which', 'node'], capture_output=True, text=True).stdout.strip()
    if not node:
        pytest.skip('node not installed')
    js = (ROOT / 'skills/token-optimizer/assets/coverage_funnel.js').read_text(encoding='utf-8')
    script = js + '''
    var a = coverageFunnelHtml({coverage_funnel: {available: false}});
    if (a.indexOf('unknown (not zero)') < 0) throw new Error('unavailable copy');
    var b = coverageFunnelHtml({coverage_funnel: {available: true, empty: true}});
    if (b.indexOf('No command output has been counted yet') < 0) throw new Error('empty copy');
    var c = coverageFunnelHtml({coverage_funnel: {available: true, empty: false, window_days: 30, tokens: 4200000,
      acted_share: 0.005, not_eligible_share: 0.9, top_reject: {reason: 'glued<b>', share: 0.5},
      gates: [{gate: 'compressed', label: 'Compressed <i>', results: 3, tokens: 21000, share: 0.005}]}});
    if (c.indexOf('4.2M tokens') < 0 || c.indexOf('acted on 0.5%') < 0 || c.indexOf('Not eligible: 90.0%') < 0) throw new Error('lead');
    if (c.indexOf('glued&lt;b&gt;') < 0 || c.indexOf('Compressed &lt;i&gt;') < 0) throw new Error('escape');
    if (/\\u2014/.test(a + b + c)) throw new Error('em dash');
    if (coverageFunnelHtml({}) !== '') throw new Error('absent');
    '''
    path = tmp_path / 'cf.js'
    path.write_text(script, encoding='utf-8')
    r = subprocess.run([node, str(path)], capture_output=True, text=True, env=dict(os.environ))
    assert r.returncode == 0, r.stderr


def test_template_includes_renderer_and_payload_key():
    html = (ROOT / 'skills/token-optimizer/assets/dashboard.html').read_text(encoding='utf-8')
    assert '/*@include coverage_funnel.js*/' in html
    assert html.count('html += coverageFunnelHtml(data)') == 2
    src = (SCRIPTS / '_measure/dashboard.py').read_text(encoding='utf-8')
    assert src.count('"coverage_funnel": coverage_report.collect(') == 2
    assert 'savings_story.js' not in (ROOT / 'skills/token-optimizer/assets/coverage_funnel.js').read_text(encoding='utf-8')


# ---- hook-budget guard ------------------------------------------------------

def test_token_count_is_bounded_on_large_non_ascii_output(monkeypatch):
    """The shared estimator walks non-ASCII text per character; the counter must not."""
    import token_estimate
    seen = []
    real = token_estimate.estimate_tokens
    monkeypatch.setattr(token_estimate, 'estimate_tokens', lambda t: (seen.append(len(t)), real(t))[1])
    text = 'שורה של פלט בעברית\n' * 40000
    got = coverage_funnel.tokens_of(text)
    assert max(seen) <= coverage_funnel._SAMPLE_CHARS
    assert abs(got - real(text)) <= 0.02 * real(text)
    seen.clear()
    ascii_text = 'plain command output line\n' * 40000
    assert coverage_funnel.tokens_of(ascii_text) == real(ascii_text)  # ASCII keeps the exact estimate
    assert coverage_funnel.tokens_of('') == 0
