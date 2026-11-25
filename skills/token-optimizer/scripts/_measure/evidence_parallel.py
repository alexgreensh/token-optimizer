"""Bounded parallel transcript scanning without sharing database handles."""
import os
import time
import multiprocessing
import sys
import threading
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

_POOL_GATE = threading.BoundedSemaphore(1)


def safe_entrypoint():
    """Only spawn from the guarded CLI, never an arbitrary embedding script."""
    main = getattr(sys.modules.get('__main__'), '__file__', '')
    try:
        return bool(main) and Path(main).resolve() == (Path(__file__).resolve().parents[1] / 'measure.py').resolve()
    except (OSError, ValueError):
        return False


def watch_parent():
    """A killed regeneration must not strand idle scanner processes."""
    parent = multiprocessing.parent_process()
    if parent is None:
        return
    def stop_with_parent():
        parent.join()
        os._exit(1)
    threading.Thread(target=stop_with_parent, daemon=True).start()


def _spawn_context():
    """Start Windows workers with pythonw.exe so they open no console window.

    The dashboard regeneration runs without a console, so every python.exe
    worker would get a new visible one. pythonw.exe sits next to python.exe in
    python.org, winget, install-manager and conda layouts. Store installs
    (WindowsApps) are left alone, like python-launcher.sh does, and so are
    venvs: the base pythonw.exe would start without the venv, and the venv's
    own pythonw.exe is a redirector that multiprocessing cannot hand pipes to. When no usable
    twin exists nothing changes, and a broken one only breaks the pool, which
    batches() already falls back from to serial scanning.
    """
    ctx = multiprocessing.get_context('spawn')
    if os.name == 'nt' and sys.prefix == sys.base_prefix:
        from multiprocessing import spawn
        exe = Path(spawn.get_executable() or sys.executable)
        gui = exe.with_name('pythonw.exe')
        if exe.name.lower() != 'pythonw.exe' and 'windowsapps' not in str(exe).lower() and gui.is_file():
            ctx.set_executable(str(gui))
    return ctx


def _scan(path):
    from .session_effect import scan_transcript
    try:
        return scan_transcript(path)
    except (OSError, ValueError, TypeError):
        return None


def first_call(path):
    from .first_call_context import observe, transcript
    try:
        return observe(transcript(path), lambda value: round(len(value) / 4)), False
    except (OSError, ValueError, TypeError):
        return None, True


def _scan_chunk(worker, paths):
    return [worker(path) for path in paths]


def batches(items, deadline, max_bytes, worker=_scan):
    """Yield in order while workers scan ahead of the parent's large-file work."""
    workers = min(8, os.cpu_count() or 1)
    position = 0
    pool, futures, next_submit = None, {}, 0
    acquired = not multiprocessing.current_process().daemon and _POOL_GATE.acquire(blocking=False)
    try:
        if acquired:
            pool = ProcessPoolExecutor(max_workers=workers, mp_context=_spawn_context(), initializer=watch_parent)
            while position < len(items) and (deadline is None or time.monotonic() <= deadline):
                while next_submit < len(items) and len(futures) < 128:
                    indexes = []
                    while next_submit < len(items) and len(indexes) < 4 and len(futures) + len(indexes) < 128:
                        if items[next_submit][1].st_size <= max_bytes:
                            indexes.append(next_submit)
                        next_submit += 1
                    if indexes:
                        future = pool.submit(_scan_chunk, worker, [items[index][0] for index in indexes])
                        futures.update({index: (future, offset) for offset, index in enumerate(indexes)})
                path, stat = items[position]
                if stat.st_size <= max_bytes:
                    future, offset = futures.pop(position)
                    result = future.result()[offset]
                else:
                    result = worker(path)
                yield [((path, stat), result)]
                position += 1
    except (OSError, RuntimeError, AssertionError):
        pass
    finally:
        for future, _ in futures.values():
            future.cancel()
        try:
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
        finally:
            if acquired:
                _POOL_GATE.release()
    if position < len(items):
        while position < len(items) and (deadline is None or time.monotonic() <= deadline):
            batch = items[position:position + 64]
            yield [(item, worker(item[0])) for item in batch]
            position += len(batch)
