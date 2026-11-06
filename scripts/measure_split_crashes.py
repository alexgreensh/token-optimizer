"""BASE crashes the split must reproduce, never hide (plan section 6).

Each entry was reviewed by hand: its platform, the case ids it may cover, the
innermost frame and the exact final line. Capture accepts a listed crash only
when all of them match. Comparison may then ignore one thing in that case's
stderr: the line numbers of measure.py frames, which every extraction shifts.
Stdout, exit status, every other stderr byte and every other case stay raw.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

PLATFORM = os.name

KNOWN_BASE_CRASHES = (
    {
        'name': 'devin-sigpipe-windows',
        'platform': 'nt',
        'case_prefix': 'cli-devin-',
        'frame_file': 'devin_compress.py',
        'frame_source': b'signal.signal(signal.SIGPIPE, signal.SIG_IGN)',
        'final_prefix': b"AttributeError: module 'signal' has no attribute 'SIGPIPE'",
        'why': 'BASE 5.13.64 devin_compress.main ignores SIGPIPE unconditionally and Windows has no '
               'SIGPIPE, so measure.py devin-filter crashes on Windows in BASE and candidate alike. '
               'Product bug, reported separately; the split must not change it.',
    },
    {
        'name': 'devin-doctor-o-nonblock-windows',
        'platform': 'nt',
        'case_prefix': 'cli-devin-doctor',
        'frame_file': 'devin_doctor.py',
        'frame_source': b'fd = os.open(product, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)',
        'final_prefix': b"AttributeError: module 'os' has no attribute 'O_NONBLOCK'",
        'why': 'BASE 5.13.64 devin_doctor.local_agent_surfaces opens with os.O_NONBLOCK and '
               'os.O_NOFOLLOW, which Windows lacks, so measure.py devin-doctor crashes on Windows in '
               'BASE and candidate alike (CI 42138301440). Product bug, reported separately.',
    },
)

_FRAME = re.compile(rb'^\s*File "([^"]+)", line \d+, in ')
# A real traceback frame header for measure.py, anchored on the whole line.
_MEASURE_FRAME = re.compile(rb'(  File "[^"\r\n]*[\\/]measure\.py", line )\d+(, in [^\r\n]*)')
_TRACEBACK = b'Traceback (most recent call last):'


def known_crash(case_id, stderr, platform=None):
    """The reviewed entry this traceback matches, or None."""
    platform = PLATFORM if platform is None else platform
    lines = [line.rstrip(b'\r') for line in stderr.split(b'\n')]
    lines = [line for line in lines if line.strip()]
    frames = [index for index, line in enumerate(lines) if _FRAME.match(line)]
    if not frames or b'Traceback (most recent call last)' not in stderr:
        return None
    innermost = frames[-1]
    frame_file = Path(_FRAME.match(lines[innermost]).group(1).decode('utf-8', 'replace').replace('\\', '/')).name
    source = lines[innermost + 1].strip() if innermost + 1 < len(lines) else b''
    for entry in KNOWN_BASE_CRASHES:
        if (platform == entry['platform'] and case_id.startswith(entry['case_prefix'])
                and frame_file == entry['frame_file'] and source == entry['frame_source']
                and lines[-1].startswith(entry['final_prefix'])):
            return entry
    return None


def _masked(stderr):
    """Mask measure.py line numbers in traceback frame headers only.

    A frame header is a whole line inside a traceback block: after a
    'Traceback (most recent call last):' line and before the first unindented
    line (the exception). Every other byte, including frame-shaped text in
    diagnostics, source lines or exception messages, is returned unchanged.
    """
    out, inside = [], False
    for line in stderr.splitlines(keepends=True):
        body = line.rstrip(b'\r\n')
        if body == _TRACEBACK:
            inside = True
        elif inside and not body.startswith(b' '):
            inside = False
        elif inside:
            match = _MEASURE_FRAME.fullmatch(body)
            if match:
                line = match.group(1) + b'<measure-line>' + match.group(2) + line[len(body):]
        out.append(line)
    return b''.join(out)


def normalize_known_crashes(errors, expected, actual, platform=None):
    """Move only listed-crash stderr line-number differences out of ``errors``."""
    remaining, normalized = [], []
    for error in errors:
        prefix = 'bytes differ: '
        if error.startswith(prefix) and error.endswith('/stderr'):
            case = error[len(prefix):-len('/stderr')]
            before = (Path(expected) / case / 'stderr').read_bytes()
            after = (Path(actual) / case / 'stderr').read_bytes()
            entry = known_crash(case, before, platform)
            if entry is not None and entry is known_crash(case, after, platform) and _masked(before) == _masked(after):
                normalized.append(case + '/stderr: ' + entry['name'])
                continue
        remaining.append(error)
    return remaining, normalized
