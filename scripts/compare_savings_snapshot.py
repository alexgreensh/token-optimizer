#!/usr/bin/env python3
"""Profile and compare old/new savings on one isolated snapshot clone.

Usage: python3 scripts/compare_savings_snapshot.py SNAPSHOT_HOME DATA SESSION WORK
DATA must be inside SNAPSHOT_HOME. WORK must be a new directory in this worktree.
The caller records WORK/results.json in the report before removing WORK.

Passes, all on one clone: new_empty (new code first, nothing computed on the
clone yet), old1, old_warm, new_cold, new_warm, old2. cache_health.json (the
cache-waste scan's 15-minute sidecar) is deleted before new_empty and new_cold,
so neither new pass can borrow the scan from an old pass. Every dump must equal
old1's.
"""
import difflib
import json
import os
from pathlib import Path
import pstats
import resource
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parents[1]
MIN_FREE = 15 * 1024 ** 3
TIMEOUT = 3600


def clone(source, target):
    if shutil.disk_usage(target.parent).free < MIN_FREE:
        raise RuntimeError('less than 15 GiB free; do not create another clone')
    subprocess.run(['cp', '-cRp', str(source), str(target)], check=True)


def rewrite_db(db, original_home, cloned_home):
    conn = sqlite3.connect(db)
    try:
        names = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        for (name,) in names:
            table = '"' + name.replace('"', '""') + '"'
            for column in conn.execute('PRAGMA table_info(' + table + ')').fetchall():
                if column[1] not in ('jsonl_path', 'transcript_path'):
                    continue
                field = '"' + column[1].replace('"', '""') + '"'
                conn.execute('UPDATE ' + table + ' SET ' + field + '=replace(' + field + ', ?, ?) WHERE ' + field + ' LIKE ?',
                             (str(original_home), str(cloned_home), str(original_home) + '/%'))
        conn.commit()
    finally:
        conn.close()


def ensure_local_links(home):
    for directory, dirs, files in os.walk(home, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink() and not path.resolve().is_relative_to(home):
                raise RuntimeError('snapshot contains a link outside its home: ' + str(path))


def compare(label, baseline, current):
    before = json.dumps(baseline, sort_keys=True, indent=2, allow_nan=False).splitlines()
    after = json.dumps(current, sort_keys=True, indent=2, allow_nan=False).splitlines()
    if before != after:
        print('\n'.join(difflib.unified_diff(before, after, fromfile='old1', tofile=label)))
        return False
    return True


def main():
    if len(sys.argv) != 5:
        raise SystemExit(__doc__)
    source, source_data, session, work = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), sys.argv[3], Path(sys.argv[4]).resolve()
    if not work.is_relative_to(ROOT) or work.exists() or not source_data.is_relative_to(source) or source == Path.home():
        raise SystemExit('WORK must be new and inside the worktree; DATA must be inside a snapshot HOME')
    if not os.environ.get('GIT_DIR') or Path(os.environ.get('GIT_WORK_TREE', '')).resolve() != ROOT:
        raise SystemExit('Export the worktree GIT_DIR and GIT_WORK_TREE before running')
    db = source_data / 'trends.db'
    wal = source_data / 'trends.db-wal'
    if wal.exists() and wal.stat().st_size:
        raise SystemExit('Snapshot has a WAL; create a consistent SQLite backup first')
    ensure_local_links(source)
    work.mkdir(parents=True)
    old = work / 'old-code'; old.mkdir()
    archive = work / 'old.tar'
    with archive.open('wb') as out:
        subprocess.run(['git', 'archive', 'origin/main', 'skills', 'hooks'], cwd=ROOT, stdout=out, check=True)
    with tarfile.open(archive) as tar:
        tar.extractall(old, filter='data')
    dashboard = Path('skills/token-optimizer/scripts/_measure/dashboard.py')
    with (old / dashboard).open('wb') as out:
        subprocess.run(['git', 'show', 'origin/main:' + dashboard.as_posix()], cwd=ROOT, stdout=out, check=True)
    home = work / 'home'
    clone(source, home)
    rel = source_data.relative_to(source)
    src = sqlite3.connect(db.as_uri() + '?mode=ro&immutable=1', uri=True)
    dst = sqlite3.connect(home / rel / 'trends.db')
    try:
        src.backup(dst)
    finally:
        dst.close(); src.close()
    rewrite_db(home / rel / 'trends.db', source, home)
    for lock in (home / rel / 'status-bar').glob('*.refresh.lock'):
        lock.unlink()
    cwd = home / 'workdir'; cwd.mkdir(exist_ok=True)
    env = {**os.environ, 'HOME': str(home), 'USERPROFILE': str(home), 'CLAUDE_CONFIG_DIR': str(home / '.claude'),
           'TOKEN_OPTIMIZER_SNAPSHOT_DIR': str(home / rel), 'PYTHONDONTWRITEBYTECODE': '1'}
    env.pop('CLAUDE_PLUGIN_DATA', None); env.pop('CLAUDE_PROJECT_DIR', None)
    dump_code = ("import json,sys;sys.path.insert(0,sys.argv[1]);import measure as m;"
                 "from _measure import structural_report,setup_step,structural_value;"
                 "r=structural_report.compute(f=m);"
                 "s=setup_step.collect(m.TRENDS_DB,m.SNAPSHOT_DIR,m.CLAUDE_DIR,structural_value.card_resolver(m));"
                 "print(json.dumps({'structural':r,'setup':s},sort_keys=True,allow_nan=False))")
    results, timings = {}, {}
    passes = [('new_empty', ROOT), ('old1', old), ('old_warm', old), ('new_cold', ROOT), ('new_warm', ROOT), ('old2', old)]
    for label, code in passes:
        if label in ('new_empty', 'new_cold'):
            (home / rel / 'cache_health.json').unlink(missing_ok=True)
        scripts = code / 'skills/token-optimizer/scripts'
        profile = work / (label + '.prof')
        argv = [sys.executable, '-m', 'cProfile', '-o', str(profile), str(scripts / 'measure.py'), 'status-bar', '--session', session, '--sync']
        usage = resource.getrusage(resource.RUSAGE_CHILDREN); load = os.getloadavg()[0]; start = time.perf_counter()
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=TIMEOUT)
        wall = time.perf_counter() - start; after = resource.getrusage(resource.RUSAGE_CHILDREN)
        timing = {'wall_s': wall, 'cpu_user_s': after.ru_utime - usage.ru_utime, 'cpu_sys_s': after.ru_stime - usage.ru_stime,
                  'load_before': load, 'load_after': os.getloadavg()[0]}
        if proc.returncode:
            raise RuntimeError(label + ': ' + proc.stderr[-2000:])
        timing['total_calls'] = pstats.Stats(str(profile)).total_calls
        payload = json.loads(proc.stdout); savings = payload.get('savings')
        if not isinstance(savings, dict):
            raise RuntimeError(label + ': no savings: ' + str(payload.get('savings_reason')))
        savings.pop('computed_at', None)
        if label in ('old_warm', 'new_warm'):
            # Age the new code's saved session figure past its 300 s reuse, so the
            # plain call pays the session query a real poll pays every 5 minutes.
            for saved in (home / rel / 'status-bar').glob('session-*.session'):
                os.utime(saved, (time.time() - 400, time.time() - 400))
            plain_before = resource.getrusage(resource.RUSAGE_CHILDREN); plain_load = os.getloadavg()[0]; plain_start = time.perf_counter()
            plain = subprocess.run([sys.executable, str(scripts / 'measure.py'), 'status-bar', '--session', session],
                                   cwd=cwd, env=env, capture_output=True, text=True, timeout=TIMEOUT, check=True)
            plain_after = resource.getrusage(resource.RUSAGE_CHILDREN)
            timings[label + '_plain'] = {'wall_s': time.perf_counter() - plain_start,
                'cpu_user_s': plain_after.ru_utime - plain_before.ru_utime, 'cpu_sys_s': plain_after.ru_stime - plain_before.ru_stime,
                'load_before': plain_load, 'load_after': os.getloadavg()[0]}
            if json.loads(plain.stdout).get('refresh_started'):
                lock = home / rel / 'status-bar' / (session + '.refresh.lock' if label == 'old_warm' else 'global.refresh.lock')
                deadline = time.monotonic() + TIMEOUT
                while lock.exists() and time.monotonic() < deadline:
                    time.sleep(.25)
                if lock.exists():
                    raise RuntimeError(label + ': background refresh did not release its lock')
        dump = subprocess.run([sys.executable, '-c', dump_code, str(scripts)], cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=TIMEOUT, check=True)
        results[label] = {'savings': savings, **json.loads(dump.stdout)}
        timings[label] = timing
        (work / 'results.json').write_text(json.dumps({'timings': timings, 'results': results}, indent=2), encoding='utf-8')
        print(label, json.dumps(timing, sort_keys=True), flush=True)
        if label == 'old1':
            structural = results[label]['structural'].get('tokens_saved', 0)
            steps = results[label]['setup'].get('measured_trims', 0)
            print('structural tokens:', structural, 'measured steps:', steps, flush=True)
            if not structural or not steps:
                raise SystemExit('Snapshot lacks non-zero structural and step evidence')
    ok = all([compare(label, results['old1'], results[label]) for label in ('new_empty', 'old_warm', 'new_cold', 'new_warm', 'old2')])
    structural = results['old1']['structural'].get('tokens_saved', 0)
    steps = results['old1']['setup'].get('measured_trims', 0)
    print('structural tokens:', structural, 'measured steps:', steps, 'equal:', ok, flush=True)
    if not ok or not structural or not steps:
        raise SystemExit('Comparison failed or snapshot lacks non-zero structural and step evidence')


if __name__ == '__main__':
    main()
