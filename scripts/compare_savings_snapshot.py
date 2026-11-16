#!/usr/bin/env python3
"""Compare origin/main and this checkout against isolated copies of a snapshot.

The input home must already contain .claude/projects, the relevant setup files,
and a SQLite-backed snapshot in DATA. No command reads from the live home.
Run on APFS: python3 scripts/compare_savings_snapshot.py HOME DATA SESSION WORK
WORK must be a new directory in the worktree. It retains the clones for inspection.
"""
import json
import difflib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import time


ROOT = Path(__file__).resolve().parents[1]


def clone(source, target):
    subprocess.run(['cp', '-cRp', str(source), str(target)], check=True)


def rewrite_db(db, original_home, cloned_home):
    conn = sqlite3.connect(db)
    try:
        names = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        for (name,) in names:
            safe_name = '"' + name.replace('"', '""') + '"'
            for col in conn.execute('PRAGMA table_info(' + safe_name + ')').fetchall():
                if col[1] not in ('jsonl_path', 'transcript_path'):
                    continue
                safe_col = '"' + col[1].replace('"', '""') + '"'
                conn.execute('UPDATE ' + safe_name + ' SET ' + safe_col + '=replace(' + safe_col + ', ?, ?) WHERE ' + safe_col + ' LIKE ?',
                             (str(original_home), str(cloned_home), str(original_home) + '/%'))
        conn.commit()
    finally:
        conn.close()


def main():
    if len(sys.argv) != 5:
        raise SystemExit(__doc__)
    source_home, source_data, session, work = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), sys.argv[3], Path(sys.argv[4]).resolve()
    if not work.is_relative_to(ROOT) or work.exists() or not source_data.is_relative_to(source_home):
        raise SystemExit('WORK must be new and inside the worktree; DATA must be inside HOME')
    if not os.environ.get('GIT_DIR') or Path(os.environ.get('GIT_WORK_TREE', '')).resolve() != ROOT:
        raise SystemExit('Export the worktree GIT_DIR and GIT_WORK_TREE before running')
    db = source_data / 'trends.db'
    if (source_data / 'trends.db-wal').exists() and (source_data / 'trends.db-wal').stat().st_size:
        raise SystemExit('Snapshot has a WAL; create a consistent SQLite backup first')
    work.mkdir(parents=True)
    old = work / 'old-code'
    old.mkdir()
    archive = work / 'old.tar'
    with archive.open('wb') as out:
        subprocess.run(['git', 'archive', 'origin/main', 'skills', 'hooks'], cwd=ROOT, stdout=out, check=True)
    with tarfile.open(archive) as tar:
        tar.extractall(old, filter='data')
    dashboard = Path('skills/token-optimizer/scripts/_measure/dashboard.py')
    with (old / dashboard).open('wb') as out:
        subprocess.run(['git', 'show', 'origin/main:' + dashboard.as_posix()], cwd=ROOT, stdout=out, check=True)
    data_rel = source_data.relative_to(source_home)
    results = {}
    homes = {}
    for label, code in [('old1', old), ('old_warm', old), ('new_cold', ROOT), ('new_warm', ROOT), ('old2', old)]:
        if label in ('old_warm', 'new_warm'):
            home = homes['old1' if label == 'old_warm' else 'new_cold']
        else:
            home = work / label
            clone(source_home, home)
            source = sqlite3.connect(db.as_uri() + '?mode=ro&immutable=1', uri=True)
            target = sqlite3.connect(home / data_rel / 'trends.db')
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            rewrite_db(home / data_rel / 'trends.db', source_home, home)
            for lock in (home / data_rel / 'status-bar').glob('*.refresh.lock'):
                lock.unlink()
            homes[label] = home
        env = {**os.environ, 'HOME': str(home), 'USERPROFILE': str(home), 'CLAUDE_CONFIG_DIR': str(home / '.claude'),
               'TOKEN_OPTIMIZER_SNAPSHOT_DIR': str(home / data_rel), 'PYTHONDONTWRITEBYTECODE': '1'}
        env.pop('CLAUDE_PLUGIN_DATA', None)
        env.pop('CLAUDE_PROJECT_DIR', None)
        cwd = home / 'workdir'
        cwd.mkdir(exist_ok=True)
        load_before = os.getloadavg()[0]
        start = time.perf_counter()
        proc = subprocess.run([sys.executable, str(code / 'skills/token-optimizer/scripts/measure.py'), 'status-bar', '--session', session, '--sync'],
                              cwd=cwd, env=env, text=True, capture_output=True, timeout=1800)
        elapsed = time.perf_counter() - start
        if proc.returncode:
            raise RuntimeError(label + ': ' + proc.stderr[-2000:])
        payload = json.loads(proc.stdout)
        savings = payload.get('savings')
        if not isinstance(savings, dict):
            raise RuntimeError(label + ': no savings: ' + str(payload.get('savings_reason')))
        savings.pop('computed_at', None)
        results[label] = json.dumps(savings, sort_keys=True, separators=(',', ':'), allow_nan=False)
        print(label, '%.3fs' % elapsed, 'load %.2f -> %.2f' % (load_before, os.getloadavg()[0]), flush=True)
    print('old sandwich:', results['old1'] == results['old2'])
    print('old warm equals old:', results['old_warm'] == results['old1'])
    print('new cold equals old:', results['new_cold'] == results['old1'])
    print('new warm equals old:', results['new_warm'] == results['old1'])
    if not (results['old1'] == results['old_warm'] == results['old2'] == results['new_cold'] == results['new_warm']):
        for label in ('old_warm', 'old2', 'new_cold', 'new_warm'):
            if results[label] != results['old1']:
                before = json.dumps(json.loads(results['old1']), indent=2, sort_keys=True).splitlines()
                after = json.dumps(json.loads(results[label]), indent=2, sort_keys=True).splitlines()
                print('\n'.join(difflib.unified_diff(before, after, fromfile='old1', tofile=label)))
        raise SystemExit('Savings differ; snapshot or output is not equivalent')


if __name__ == '__main__':
    main()
