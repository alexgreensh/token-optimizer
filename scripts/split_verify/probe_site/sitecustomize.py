"""Split-verify liveness probe, loaded through PYTHONPATH in liveness runs only.

Each Python process records, at exit, which facade file it read, which
`_measure` modules it imported (and from where) and its own peak RSS. Timed
performance samples never load this file. It writes only inside the directory
named by TO_SPLIT_VERIFY_LIVENESS and never prints, so captured output stays raw.
"""
import atexit as _atexit
import os as _os
import sys as _sys

_DIRECTORY = _os.environ.get('TO_SPLIT_VERIFY_LIVENESS')
_FACADE_READS = set()


def _audit(event, args):
    if event == 'open' and args and isinstance(args[0], str):
        name = _os.path.basename(args[0])
        if name == 'measure.py' or (name.startswith('measure.') and name.endswith('.pyc')):
            _FACADE_READS.add(_os.path.abspath(args[0]))
    elif event == 'exec' and args and getattr(args[0], 'co_filename', '').endswith('measure.py'):
        _FACADE_READS.add(_os.path.abspath(args[0].co_filename))


def _peak_rss():
    if _os.name == 'nt':
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                        ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                        ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        # Typed signatures: the pseudo-handle is pointer-sized, an untyped
        # call truncates it to a 32-bit int and the query fails.
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        query = kernel32.K32GetProcessMemoryInfo
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        query.restype = wintypes.BOOL
        if not query(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise OSError('K32GetProcessMemoryInfo failed: %d' % ctypes.get_last_error())
        return int(counters.PeakWorkingSetSize)
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak) if _sys.platform == 'darwin' else int(peak) * 1024


def _record():
    import json
    record = {'pid': _os.getpid(), 'argv': list(_sys.argv[:4]), 'executable': _sys.executable}
    try:
        record['split_modules'] = {name: getattr(module, '__file__', None)
                                   for name, module in list(_sys.modules.items())
                                   if name == '_measure' or name.startswith('_measure.')}
        record['facade_reads'] = sorted(_FACADE_READS)
        record['peak_rss_bytes'] = _peak_rss()
    except Exception as error:  # the record itself says the probe failed
        record['error'] = repr(error)
    path = _os.path.join(_DIRECTORY, 'proc-%d-%d.json' % (_os.getpid(), id(record)))
    with open(path, 'w', encoding='utf-8') as stream:
        json.dump(record, stream)


if _DIRECTORY:
    _sys.addaudithook(_audit)
    _atexit.register(_record)
