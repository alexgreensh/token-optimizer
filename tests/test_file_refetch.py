"""W3: re-fetch metric for file content printed through the shell. Synthetic transcripts only."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))
from _measure import coverage_report, file_refetch  # noqa: E402

SENTINEL = 'zq9sentinelname'
CUT = ('line one of the file\n' * 30) + '... 240 lines omitted ...\n' + ('last lines of the file\n' * 30)
FULL = 'whole file text\n' * 20
CWD = '/work/proj'


def bash(command, out=CUT, err=False):
    return ('Bash', {'command': command}, out, err)


def read(path, **kw):
    return ('Read', dict(file_path=path, **kw), 'content', False)


def other():
    return ('Grep', {'pattern': 'x'}, 'hits', False)


def write_transcript(path, calls, cwd=CWD):
    rows = []
    for i, (name, inp, out, err) in enumerate(calls):
        tid = f'toolu_{i}'
        rows.append({'type': 'assistant', 'cwd': cwd, 'message': {'content': [
            {'type': 'tool_use', 'id': tid, 'name': name, 'input': inp}]}})
        rows.append({'type': 'user', 'cwd': cwd, 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': tid, 'content': out, 'is_error': err}]}})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')
    return path


def count(tmp_path, calls, cwd=CWD):
    p = write_transcript(tmp_path / 'p' / 't.jsonl', calls, cwd)
    return file_refetch.scan(file_refetch.calls(p))


def test_full_read_inside_the_window_is_a_refetch(tmp_path):
    for gap in range(0, 3):
        acc = count(tmp_path, [bash('sed -n 1,400p /work/proj/a.py')] + [other()] * gap + [read('/work/proj/a.py')])
        assert (acc['events'], acc['refetched']) == (1, 1), gap


def test_read_outside_the_window_is_not(tmp_path):
    acc = count(tmp_path, [bash('cat /work/proj/a.py')] + [other()] * 3 + [read('/work/proj/a.py')])
    assert (acc['events'], acc['refetched'], acc['refetched_file']) == (1, 0, 0)


def test_other_range_of_the_same_file_is_not_the_cut_content(tmp_path):
    acc = count(tmp_path, [bash('sed -n 1,100p /work/proj/a.py'), read('/work/proj/a.py', offset=500, limit=50)])
    assert (acc['events'], acc['refetched'], acc['refetched_file']) == (1, 0, 1)


def test_overlapping_range_counts(tmp_path):
    acc = count(tmp_path, [bash('sed -n 1,100p /work/proj/a.py'), read('/work/proj/a.py', offset=90, limit=50)])
    assert acc['refetched'] == 1


def test_shell_print_of_the_same_file_counts(tmp_path):
    calls = [bash('sed -n 1,400p /work/proj/a.py'), bash('cat /work/proj/a.py', FULL)]
    assert count(tmp_path, calls)['refetched'] == 1


def test_relative_paths_and_cd_prefix_resolve_to_the_same_file(tmp_path):
    calls = [bash('cd /work/proj && cat src/a.py'), read('/work/proj/src/a.py')]
    assert count(tmp_path, calls)['refetched'] == 1
    calls = [bash('head -n 50 src/a.py'), bash('cd src && cat a.py', FULL)]
    assert count(tmp_path, calls)['refetched'] == 1
    assert count(tmp_path, [bash('cat a.py'), read('/elsewhere/a.py')])['refetched'] == 0


def test_only_elided_prints_are_events(tmp_path):
    calls = [bash('cat /work/proj/a.py', FULL), read('/work/proj/a.py')]
    assert count(tmp_path, calls)['events'] == 0


def test_pipelines_and_non_prints_are_not_events(tmp_path):
    for cmd in ['cat /work/proj/a.py | head -50', 'cat /work/proj/a.py > /work/proj/b', 'sed -i s/a/b/ /work/proj/a.py',
                'sed -n 1,5p /work/proj/a.py; ls', 'grep x /work/proj/a.py', 'ls /work/proj', 'cat $(ls)']:
        assert count(tmp_path, [bash(cmd), read('/work/proj/a.py')])['events'] == 0, cmd


def test_failed_prints_are_not_events(tmp_path):
    assert count(tmp_path, [bash('cat /work/proj/a.py', CUT, err=True)])['events'] == 0


def test_git_show_matches_only_the_same_rev_and_file(tmp_path):
    calls = [bash('git show abc123:src/a.py'), bash('git show abc123:src/a.py', FULL)]
    assert count(tmp_path, calls)['refetched'] == 1
    calls = [bash('git show abc123:src/a.py'), bash('git show def456:src/a.py', FULL), read('/work/proj/src/a.py')]
    acc = count(tmp_path, calls)
    assert (acc['events'], acc['refetched']) == (1, 0)


def test_head_tail_nl_default_ranges(tmp_path):
    assert file_refetch.print_targets('head -n 20 /x/a', '/')[0][1] == (1, 20)
    assert file_refetch.print_targets('head -5 /x/a', '/')[0][1] == (1, 5)
    assert file_refetch.print_targets('head /x/a', '/')[0][1] == (1, 10)
    assert file_refetch.print_targets('tail -n 30 /x/a', '/')[0][1] is None
    assert file_refetch.print_targets('nl /x/a', '/')[0][1] is None
    assert file_refetch.print_targets("sed -n '7p' /x/a", '/')[0][1] == (7, 7)
    assert [t[0] for t in file_refetch.print_targets('cat /x/a /x/b', '/')] == ['/x/a', '/x/b']


@pytest.mark.parametrize('events,refetched,expected', [
    (199, 100, 'CANNOT-MEASURE'),
    (0, 0, 'CANNOT-MEASURE'),
    (200, 31, 'MAKE-VERBATIM'),   # 15.5%
    (200, 30, 'NO-CHANGE'),       # exactly 15.0%
    (200, 10, 'NO-CHANGE'),       # exactly 5.0%
    (200, 9, 'MAY-WIDEN'),        # 4.5%
    (1000, 0, 'MAY-WIDEN'),
    (400, 100, 'MAKE-VERBATIM'),
])
def test_verdict_follows_the_written_rule(events, refetched, expected):
    assert file_refetch.verdict(events, refetched) == expected


def test_summary_rates_and_no_events(tmp_path):
    s = file_refetch.summarize({'events': 4, 'refetched': 1, 'refetched_file': 2})
    assert s['rate'] == 0.25 and s['rate_any_range'] == 0.5 and s['verdict'] == 'CANNOT-MEASURE'
    empty = file_refetch.summarize(file_refetch.new_counts())
    assert empty['rate'] is None and 'n/a' in '\n'.join(file_refetch.format_lines(empty))


def _tree_hash(root):
    h = hashlib.sha256()
    for p in sorted(root.rglob('*')):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes() + str(p.stat().st_mtime_ns).encode())
    return h.hexdigest()


def _projects(tmp_path):
    root = tmp_path / 'projects'
    write_transcript(root / 'a' / 'one.jsonl', [bash(f'cat {CWD}/{SENTINEL}.py'), read(f'{CWD}/{SENTINEL}.py'),
                                                 bash(f'sed -n 1,9p {CWD}/other.py'), other()])
    write_transcript(root / 'a' / 'sub' / 'two.jsonl', [bash(f'head -n 3 {CWD}/x.py')])
    return root


def test_replay_carries_the_metric_read_only_and_leaks_nothing(tmp_path, capsys):
    root = _projects(tmp_path)
    before = _tree_hash(root)
    s = coverage_report.replay(root, days=36500)
    assert _tree_hash(root) == before
    fr = s['file_refetch']
    assert (fr['events'], fr['refetched']) == (3, 1) and fr['verdict'] == 'CANNOT-MEASURE'
    assert coverage_report.replay_cli(['--days', '36500'], root) == 0
    out = capsys.readouterr().out
    assert 'cut by the generic fallback: 3 events' in out and 'CANNOT-MEASURE' in out
    assert SENTINEL not in out and CWD not in out
    assert coverage_report.refetch_cli(['--days', '36500', '--json'], root) == 0
    j = capsys.readouterr().out
    assert SENTINEL not in j and json.loads(j)['events'] == 3
    assert coverage_report.refetch_cli(['--days', '36500'], root) == 0
    assert 'Verdict' in capsys.readouterr().out
    assert coverage_report.refetch_cli([], tmp_path / 'missing') == 1


def test_cli_command_under_a_scratch_home(tmp_path):
    home = tmp_path / 'home'
    (home / '.claude').mkdir(parents=True)
    _projects(tmp_path).rename(home / '.claude' / 'projects')
    env = {k: v for k, v in os.environ.items() if k not in ('CLAUDE_PLUGIN_ROOT', 'TOKEN_OPTIMIZER_RUNTIME', 'CLAUDE_CONFIG_DIR')}
    env.update(HOME=str(home), TOKEN_OPTIMIZER_SNAPSHOT_DIR=str(tmp_path / 'snap'), PYTHONDONTWRITEBYTECODE='1')
    before = _tree_hash(home / '.claude' / 'projects')
    r = subprocess.run([sys.executable, str(SCRIPTS / 'measure.py'), 'file-refetch', '--days', '36500', '--json'],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout[r.stdout.index('{'):])['events'] == 3
    assert _tree_hash(home / '.claude' / 'projects') == before
    assert not (tmp_path / 'snap').exists()


def test_compression_is_untouched():
    """Metric only: this change must not alter any compressor module."""
    import bash_compress
    src = (SCRIPTS / 'bash_compress.py').read_text(encoding='utf-8')
    assert 'file_refetch' not in src and 'file_refetch' not in (SCRIPTS / 'bash_compress_hook.py').read_text(encoding='utf-8')
    assert bash_compress.compress('sed -n 1,5p f', 'x') == 'x'
