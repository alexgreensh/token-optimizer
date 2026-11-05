#!/usr/bin/env python3
"""Deterministic golden child. Host process, browser and network calls are fakes."""
from __future__ import annotations
import atexit
import contextlib
import datetime
import json
import importlib
import importlib.machinery
import inspect
import io
import itertools
import marshal
import os
from pathlib import Path
import random
import runpy
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
import tempfile
import threading
import uuid
import webbrowser
from types import ModuleType

payload, effects_path, entry, *arguments = sys.argv[1:]
effects = []
home = Path(os.environ['HOME']).resolve()
work = home.parent
real_popen = subprocess.Popen
real_getpid = os.getpid
real_perf_counter = time.perf_counter
effects_path = Path(effects_path)
random.seed(0)
time.time = lambda: 1793870400.0
time.time_ns = lambda: 1793870400000000000
real_localtime, real_gmtime, real_strftime = time.localtime, time.gmtime, time.strftime
time.localtime = lambda seconds=None: real_localtime(1793870400 if seconds is None else seconds)
time.gmtime = lambda seconds=None: real_gmtime(1793870400 if seconds is None else seconds)
time.strftime = lambda fmt, value=None: real_strftime(fmt, time.localtime() if value is None else value)
os.urandom = lambda size: bytes(random.randrange(256) for _ in range(size))
_ticks = 0
_deadline_threads = []
_deadline_dispatching = False


def monotonic():
    global _ticks, _deadline_dispatching
    _ticks += max(1, int(float(os.environ.get('TO_SPLIT_MONOTONIC_STEP', '0.001')) * 1000))
    now = 1000.0 + _ticks / 1000.0
    if not _deadline_dispatching:
        for thread in list(_deadline_threads):
            deadline = thread._target.__self__
            if deadline._cancelled.is_set():
                _deadline_threads.remove(thread)
            elif deadline.end <= now:
                _deadline_threads.remove(thread)
                _deadline_dispatching = True
                try:
                    thread.run()
                finally:
                    _deadline_dispatching = False
    return now


def sleep(seconds):
    global _ticks
    _ticks += max(1, int(float(seconds) * 1000))


time.monotonic = monotonic
time.monotonic_ns = lambda: int(monotonic() * 1_000_000_000)
time.perf_counter = monotonic
time.sleep = sleep

# An already-expired virtual deadline must fire at a fixed scheduling point,
# rather than racing the main thread against a real OS watchdog thread.
if float(os.environ.get('TO_SPLIT_MONOTONIC_STEP', '0.001')) >= 30:
    class DeadlineThread(threading.Thread):
        def start(self):
            self._fixture_inline = self.name.startswith('token-optimizer-hook-deadline')
            if self.name == 'token-optimizer-hook-deadline':
                _deadline_threads.append(self)
                monotonic()
            elif self._fixture_inline:
                self.run()
            else:
                super().start()

        def join(self, timeout=None):
            if not getattr(self, '_fixture_inline', False):
                return super().join(timeout)

    threading.Thread = DeadlineThread

if hasattr(time, 'tzset'):
    time.tzset()
real_datetime = datetime.datetime


class FrozenDatetime(real_datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(1793870400, tz)
    @classmethod
    def utcnow(cls):
        return cls.fromtimestamp(1793870400, datetime.timezone.utc).replace(tzinfo=None)


datetime.datetime = FrozenDatetime
real_date = datetime.date


class FrozenDate(real_date):
    @classmethod
    def today(cls):
        return cls.fromtimestamp(1793870400)


datetime.date = FrozenDate
tempfile._name_sequence = ('fixture%08d' % index for index in itertools.count())
real_connect = sqlite3.connect
_fixed_sql_now = real_datetime.fromtimestamp(1793870400, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def sql_connect(*args, **kwargs):
    connection = real_connect(*args, **kwargs)
    connection.create_function('current_timestamp', 0, lambda: _fixed_sql_now)
    connection.create_function('current_date', 0, lambda: _fixed_sql_now[:10])
    connection.create_function('current_time', 0, lambda: _fixed_sql_now[11:])
    def clock_function(name):
        def call(*values):
            values = tuple(_fixed_sql_now if value == 'now' else value for value in values)
            if not values and name != 'strftime':
                values = (_fixed_sql_now,)
            with real_connect(':memory:') as engine:
                return engine.execute('SELECT ' + name + '(' + ','.join('?' for _ in values) + ')', values).fetchone()[0]
        return call
    for name in ('datetime', 'date', 'time', 'strftime', 'julianday', 'unixepoch'):
        connection.create_function(name, -1, clock_function(name))
    return connection


sqlite3.connect = sql_connect
uuid.uuid4 = lambda: uuid.UUID('00000000-0000-4000-8000-000000000001')
secrets.token_hex = lambda n=32: 'ab' * n
secrets.token_urlsafe = lambda n=32: 'fixture-token'
secrets.token_bytes = lambda n=32: b'\xab' * n
secrets.randbelow = lambda n: 0
os.getpid = lambda: 4242
os.getppid = lambda: 4241


def clean(value):
    if isinstance(value, bytes):
        return {'bytes_hex': value.hex()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, io.IOBase):
        return {'stream': str(getattr(value, 'name', 'unnamed'))}
    return str(value)


class Process:
    pid = 4243
    returncode = 0
    def __init__(self, args, *a, **kw):
        effects.append(['process', clean(args), clean(kw)])
        self.args = args
        self.text = kw.get('text') or kw.get('universal_newlines') or kw.get('encoding')
        self.stdout = None
        self.stderr = None
    def communicate(self, input=None, timeout=None):
        empty = '' if self.text else b''
        return empty, empty
    def wait(self, timeout=None): return 0
    def poll(self): return 0
    def kill(self): pass
    def terminate(self): pass
    def __enter__(self): return self
    def __exit__(self, *args): return None


def popen(args, *a, **kw):
    # The public hook chain runs for real. Re-enter this deterministic child
    # around known Python entry scripts; scheduler/browser/service calls stay
    # recorded fakes. Nothing outside this payload can be executed.
    values = list(args) if isinstance(args, (list, tuple)) else []
    for index, value in enumerate(values[1:], 1):
        if not isinstance(value, (str, os.PathLike)):
            continue
        candidate = Path(value)
        command = str(values[index+1]) if index+1 < len(values) else ''
        internal = candidate.name == 'module_runner.py' or candidate.name == 'measure.py' and command in ('api-manage', 'v5')
        if internal and candidate.is_absolute() and candidate.resolve().is_relative_to(Path(payload).resolve()):
            effects.append(['internal-python', clean(values)])
            child_effects = work / (effects_path.stem + '-child-' + str(len(effects)) + '.json')
            rewritten = [values[0], str(Path(__file__).resolve()), payload, str(child_effects), 'script',
                         str(candidate), *values[index+1:]]
            child_env = dict(kw.get('env') or os.environ)
            child_env['TOKEN_OPTIMIZER_GOLDEN_CHILD'] = '1'
            kw['env'] = child_env
            return real_popen(rewritten, *a, **kw)
    return Process(args, *a, **kw)


subprocess.Popen = popen
os.kill = lambda *a: effects.append(['kill', clean(a)])
webbrowser.open = lambda *a, **k: effects.append(['browser', clean(a), clean(k)]) or True


def audit(event, args):
    if event in ('open', 'os.listdir', 'os.scandir', 'sqlite3.connect') and args:
        check_home_read(args[0])
    if event == 'subprocess.Popen':
        argv = args[1]
        if isinstance(argv, (list, tuple)) and str(Path(__file__).resolve()) in argv:
            return
    if event in ('socket.connect', 'socket.bind', 'os.system', 'os.posix_spawn', 'os.posix_spawnp', 'os.exec', 'subprocess.Popen'):
        effects.append(['blocked', event, clean(args)])
        raise PermissionError('golden host boundary: ' + event)
    if event == 'open':
        path, mode, flags = args
        writing = isinstance(mode, str) and any(c in mode for c in 'wax+')
        writing = writing or isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        if not writing:
            return
        targets = [path]
    elif event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.utime', 'os.rename', 'os.link', 'os.symlink'):
        targets = args[:2] if event in ('os.rename', 'os.link', 'os.symlink') else args[:1]
    else:
        return
    for target in targets:
        if isinstance(target, (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(target)).resolve()
            if not path.is_relative_to(work):
                raise PermissionError('golden write outside fixture: ' + str(path))


real_home = os.path.normcase(os.path.abspath(os.environ['TO_SPLIT_REAL_HOME']))
source_root = os.path.normcase(str(Path(__file__).resolve().parents[1]))


def check_home_read(target, *, metadata=False):
    if isinstance(target, (str, bytes, os.PathLike)):
        path = os.path.normcase(os.path.abspath(os.fsdecode(target)))
        if (path == real_home or path.startswith(real_home + os.sep)) and not (
                path == source_root or path.startswith(source_root + os.sep) or
                metadata and source_root.startswith(path + os.sep)):
            raise PermissionError('golden read outside synthetic home: ' + path)


for name in ('stat', 'lstat', 'access'):
    original = getattr(os, name)
    def guarded(target, *args, _original=original, _name=name, **kwargs):
        try:
            check_home_read(target, metadata=True)
        except PermissionError:
            if _name == 'access':
                return False
            raise
        return _original(target, *args, **kwargs)
    setattr(os, name, guarded)


sys.addaudithook(audit)
atexit.register(lambda: effects_path.write_text(json.dumps(effects, ensure_ascii=False) + '\n'))
sys.path.insert(0, str(Path(payload) / 'skills/token-optimizer/scripts'))
sys.path.insert(0, str(Path(payload) / 'hooks'))

# Reuse compiled fixture code, preserving source filenames and runpy globals.
# Public subprocess smoke and cold-import timing do not use this cache.
if os.environ.get('TO_SPLIT_CODE_CACHE'):
    codes = marshal.loads(Path(os.environ['TO_SPLIT_CODE_CACHE']).read_bytes())
    original_get_code = importlib.machinery.SourceFileLoader.get_code
    original_get_file = runpy._get_code_from_file
    file_returns_code = len(inspect.signature(original_get_file).parameters) == 1
    def get_code(loader, fullname):
        key = str(Path(loader.path).resolve())
        return codes[key] if key in codes else original_get_code(loader, fullname)
    def get_file(*args):
        fname = args[-1]
        key = str(Path(fname).resolve())
        if key not in codes:
            return original_get_file(*args)
        return codes[key] if file_returns_code else (codes[key], fname)
    importlib.machinery.SourceFileLoader.get_code = get_code
    runpy._get_code_from_file = get_file


def export_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return {'path': str(value)}
    if isinstance(value, ModuleType):
        return {'module': value.__name__}
    if isinstance(value, (tuple, list)):
        return [export_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((export_value(item) for item in value), key=repr)
    if isinstance(value, dict):
        return {str(key): export_value(item) for key, item in value.items()}
    if inspect.isfunction(value) or inspect.isclass(value):
        try:
            signature = str(inspect.signature(value))
        except (ValueError, TypeError):
            signature = None
        return {'name': value.__name__, 'module': value.__module__, 'signature': signature,
                'defaults': export_value(getattr(value, '__defaults__', None)),
                'kwdefaults': export_value(getattr(value, '__kwdefaults__', None)),
                'doc': value.__doc__, 'wrapped': hasattr(value, '__wrapped__')}
    return {'type': type(value).__module__ + '.' + type(value).__qualname__}


if entry == 'cli':
    path = Path(payload) / 'skills/token-optimizer/scripts/measure.py'
    sys.argv = [str(path), *arguments]
    runpy.run_path(str(path), run_name='__main__')
elif entry == 'hook':
    path = Path(payload) / 'hooks/run.py'
    sys.argv = [str(path), *arguments]
    runpy.run_path(str(path), run_name='__main__')
elif entry == 'script':
    path, *arguments = arguments
    sys.argv = [path, *arguments]
    runpy.run_path(path, run_name='__main__')
elif entry == 'exports':
    measure = importlib.import_module('measure')
    exports = {key: export_value(value) for key, value in vars(measure).items()
               if key not in ('__builtins__', '_measure_compat', '_measure_providers')}
    print(json.dumps(exports, ensure_ascii=False, sort_keys=True))
elif entry == 'adapter':
    name = arguments[0]
    if name == 'codex':
        module = importlib.import_module('codex_session')
        result = module.parse_session_jsonl(next((home / '.codex/sessions').rglob('*.jsonl')))
    elif name == 'devin':
        result = importlib.import_module('devin_state').summary()
    else:
        module = importlib.import_module(name + '_session')
        raw = json.loads((home / 'native-fixtures.json').read_text())[name]
        result = module.normalize_session(raw)
    if result is None:
        raise ValueError('native fixture was not recognized: ' + name)
    print(json.dumps(result, ensure_ascii=False, default=str))
elif entry == 'http':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from measure_split_http import response
    console = io.StringIO()
    with contextlib.redirect_stdout(console):
        wire = response(importlib.import_module('measure'), *arguments)
    (work / 'http-console.stdout').write_text(console.getvalue())
    sys.stdout.buffer.write(wire)
elif entry == 'seed':
    measure = importlib.import_module('measure')
    with contextlib.redirect_stdout(io.StringIO()):
        measure.collect_sessions(quiet=True)
        connection = measure._init_trends_db()
        before = connection.total_changes
        for event, tokens, dollars in (('tool_archive', 1000, 0.005), ('tool_archive_reexpand', 200, 0.001)):
            connection.execute('INSERT INTO savings_events (timestamp,event_type,tokens_saved,cost_saved_usd,session_id,session_uuid,model) VALUES (?,?,?,?,?,?,?)',
                ('2026-11-05T00:02:00', event, tokens, dollars, 'fixture-session', 'fixture-session', 'claude-sonnet-5-5'))
        connection.execute('INSERT INTO compression_events (timestamp,session_id,session_uuid,feature,original_tokens,compressed_tokens,compression_ratio,verified,model,tier) VALUES (?,?,?,?,?,?,?,?,?,?)',
            ('2026-11-05T00:02:00', 'fixture-session', 'fixture-session', 'bash_compress', 1000, 200, 0.2, 1, 'claude-sonnet-5-5', 'anthropic'))
        connection.execute('INSERT OR REPLACE INTO counted_reread (event_key,source,session_uuid,event_ts,event_month,tokens,oneshot_usd,reread_tokens,reread_usd,turns_counted,computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            ('se:fixture', 'savings_events', 'fixture-session', '2026-11-05T00:02:00', '2026-11', 1000, 0.005, 2000, 0.001, 2, '2026-11-05T09:20:00'))
        connection.commit()
        assert connection.total_changes > before
        assert connection.execute("SELECT tokens_saved FROM savings_events WHERE event_type='tool_archive'").fetchone() == (1000,)
        assert connection.execute("SELECT reread_tokens FROM counted_reread WHERE event_key='se:fixture'").fetchone() == (2000,)
        connection.close()
        transcript = home / '.claude/projects/-fixture/fixture-session.jsonl'
        measure.quality_cache(force=True, quiet=True, session_jsonl=transcript, session_id='fixture-session')
        checkpoint = measure.compact_capture(transcript_path=str(transcript), session_id='fixture-session')
        assert checkpoint and Path(checkpoint).is_file()
        assert Path(checkpoint).with_suffix('.json').is_file()
        archive = importlib.import_module('archive_result')
        assert archive.archive_original('synthetic tool output\n' * 50, 'fixture-session', 'fixture-tool', 'Read') == 'fixture-tool'
        if arguments and arguments[0] in ('fresh-cache', 'stale-cache'):
            measure.status_bar_payload('fixture-session', transcript=str(transcript), sync=True)
            if arguments[0] == 'stale-cache':
                cache = measure._status_bar_cache_path('fixture-session')
                if cache.is_file():
                    data = json.loads(cache.read_text())
                    data['computed_at'] = 1793784000
                    cache.write_text(json.dumps(data))
                    os.utime(cache, (1793784000, 1793784000))
                for path in home.rglob('*.json'):
                    if path.name.startswith(('quality-cache-', 'status-bar-')):
                        data = json.loads(path.read_text())
                        if isinstance(data, dict):
                            for key in ('timestamp', 'generated_at', 'updated_at', 'computed_at'):
                                if key in data and isinstance(data[key], (int, float)):
                                    data[key] = 1793784000
                            path.write_text(json.dumps(data))
    print('seed-ready')
elif entry == 'api':
    measure = importlib.import_module('measure')
    name, *rest = arguments
    allowed = {'_collect_trends_data', '_collect_management_data', '_collect_health_data',
               '_collect_quality_for_dashboard', 'runway_snapshot', '_get_savings_summary',
               '_generate_daemon_script'}
    if name not in allowed:
        raise ValueError('unmanifested API case: ' + name)
    result = getattr(measure, name)(**(json.loads(rest[0]) if rest else {}))
    if isinstance(result, str):
        print(result, end='')
    else:
        print(json.dumps(result, ensure_ascii=False, default=str))
elif entry == 'perf':
    import statistics
    start = real_perf_counter()
    measure = importlib.import_module('measure')
    cold = real_perf_counter() - start
    transcript = home / '.claude/projects/-fixture/fixture-session.jsonl'
    operations = {
        'quality-cache': lambda: measure.quality_cache(force=True, quiet=True, session_jsonl=transcript, session_id='fixture-session'),
        'compact-capture': lambda: measure.compact_capture(transcript_path=str(transcript), session_id='fixture-session'),
        'status-bar': lambda: measure.status_bar_payload('fixture-session', transcript=str(transcript), sync=True),
        'collection': lambda: measure.collect_sessions(quiet=True),
        'dashboard': lambda: measure.generate_standalone_dashboard(quiet=True, force=True),
    }
    operation = operations[arguments[0]]
    samples = []
    with contextlib.redirect_stdout(io.StringIO()):
        operation()
        for _ in range(7):
            start = real_perf_counter()
            operation()
            samples.append(real_perf_counter() - start)
    print(json.dumps({'cold_import_seconds': cold, 'warm_seconds': samples,
                      'warm_median_seconds': statistics.median(samples)}))
else:
    raise ValueError('unknown golden entry: ' + entry)
