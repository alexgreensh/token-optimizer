from pathlib import Path
import re
import sys
import os
import pytest
pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Native Windows unsupported by production design (installer refuses os.name != posix); tests gated accordingly.')

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))

def test_filter_never_executes_original(tmp_path, monkeypatch):
    import devin_compress as d
    import devin_state as state
    monkeypatch.setenv('HOME',str(tmp_path))
    state.enable_consent()
    capture=state.prepare_capture()
    out=capture/'stdout'; err=capture/'stderr'
    out.write_bytes(b'repeated log line\n'*1000); err.write_bytes(b'')
    out.chmod(0o600); err.chmod(0o600)
    state.enable_consent()
    d.seal_capture(capture,0)
    result=d.filter_capture(out,err,0,'generic','session-a')
    assert result.code == 0
    assert result.stderr == b''
    assert len(result.stdout) < out.stat().st_size
    assert out.parent.name.encode() in result.stdout
    assert out.read_bytes() == b'repeated log line\n'*1000

import pytest

def test_arbitrary_private_file_refused(tmp_path, monkeypatch):
    import devin_compress as d
    import devin_state as state
    monkeypatch.setenv('HOME', str(tmp_path)); state.enable_consent()
    out=tmp_path/'secret'; err=tmp_path/'stderr'
    out.write_text('private');err.write_text('');out.chmod(0o600);err.chmod(0o600)
    with pytest.raises(OSError):
        d.filter_capture(out,err,0,'generic','session-a')

@pytest.mark.parametrize('code,errors',[(1,b''),(0,b'warning\n'),(137,b'killed\n')])
def test_raw_failure_and_stderr(tmp_path,monkeypatch,code,errors):
    import devin_compress as d
    import devin_state as s
    monkeypatch.setenv('HOME',str(tmp_path));s.enable_consent();root=s.prepare_capture()
    out,err=root/'stdout',root/'stderr';raw=b'x\n'*1000
    out.write_bytes(raw);err.write_bytes(errors);d.seal_capture(root,code)
    value=d.filter_capture(out,err,code,'generic','session')
    assert (value.stdout,value.stderr,value.code)==(raw,errors,code)

@pytest.mark.parametrize('raw',[b'a\x00b\n'*1000,b'\xff'*1000,b'github_pat_'+b'A'*40+b'\n'*1000])
def test_binary_and_credentials_passthrough(tmp_path,monkeypatch,raw):
    import devin_compress as d
    import devin_state as s
    monkeypatch.setenv('HOME',str(tmp_path));s.enable_consent();root=s.prepare_capture()
    (root/'stdout').write_bytes(raw);d.seal_capture(root,0)
    value=d.filter_capture(root/'stdout',root/'stderr',0,'generic','session')
    assert value.stdout==raw

@pytest.mark.parametrize('change',['append','truncate','replace'])
def test_changed_completed_capture_refused(tmp_path,monkeypatch,change):
    import devin_compress as d
    import devin_state as s
    monkeypatch.setenv('HOME',str(tmp_path));s.enable_consent();root=s.prepare_capture()
    (root/'stdout').write_text('text\n'*1000);d.seal_capture(root,0)
    if change=='append':
        with (root/'stdout').open('a') as f:f.write('later')
    elif change=='truncate': (root/'stdout').write_text('short')
    else:
        (root/'stdout').unlink();(root/'stdout').write_text('new');(root/'stdout').chmod(0o600)
    with pytest.raises(OSError):d.filter_capture(root/'stdout',root/'stderr',0,'generic','session')

@pytest.mark.parametrize('attack',['symlink','fifo','hardlink','permissions','oversize','unsealed'])
def test_capture_attacks_refused(tmp_path,monkeypatch,attack):
    import os
    import devin_compress as d
    import devin_state as s
    monkeypatch.setenv('HOME',str(tmp_path));s.enable_consent();root=s.prepare_capture();out=root/'stdout'
    if attack!='unsealed':d.seal_capture(root,0)
    if attack=='symlink':out.unlink();out.symlink_to(tmp_path/'secret')
    elif attack=='fifo':out.unlink();os.mkfifo(out)
    elif attack=='hardlink':os.link(out,root/'other')
    elif attack=='permissions':out.chmod(0o644)
    elif attack=='oversize':
        with out.open('wb') as f:f.truncate(d.MAX_CAPTURE+1)
    with pytest.raises((OSError,ValueError)):d.filter_capture(out,root/'stderr',0,'generic','session')

@pytest.mark.parametrize('code',[-1,256,True,'0'])
def test_bad_exit_codes_refused(tmp_path,monkeypatch,code):
    import devin_compress as d
    import devin_state as s
    monkeypatch.setenv('HOME',str(tmp_path));s.enable_consent();root=s.prepare_capture()
    with pytest.raises(ValueError):d.filter_capture(root/'stdout',root/'stderr',code,'generic','s')

@pytest.mark.parametrize('session_number',range(48))
def test_real_cli_sessions(tmp_path,monkeypatch,session_number):
    import subprocess
    import devin_compress as d
    import devin_state as s
    monkeypatch.setenv('HOME',str(tmp_path));s.enable_consent();root=s.prepare_capture()
    raw=('session-%d repeated output line\n'%session_number).encode()*1500
    (root/'stdout').write_bytes(raw);d.seal_capture(root,0)
    run=subprocess.run([sys.executable,str(SCRIPTS/'devin_compress.py'),
        '--stdout',str(root/'stdout'),'--stderr',str(root/'stderr'),'--exit-code','0',
        '--format','generic','--session','synthetic-'+str(session_number)],capture_output=True)
    assert run.returncode==0 and not run.stderr
    assert len(run.stdout)<len(raw) and root.name.encode() in run.stdout
    assert (root/'stdout').read_bytes()==raw


def _filtered(tmp_path, monkeypatch, fmt, raw, code=0, err=b''):
    import devin_compress as d
    import devin_state as s
    monkeypatch.setenv('HOME', str(tmp_path))
    s.enable_consent()
    root = s.prepare_capture()
    (root / 'stdout').write_bytes(raw)
    (root / 'stderr').write_bytes(err)
    d.seal_capture(root, code)
    return d.filter_capture(root / 'stdout', root / 'stderr', code, fmt, 'session')


def _git_log_sample():
    parts = []
    for i in range(60):
        parts += ['commit %040x' % i,
                  'Author: A U Thor <author@example.com>',
                  'Date:   Tue Oct 13 10:00:00 2026 +0300', '',
                  '    fix(module-%d): change behavior %d' % (i, i), '']
        parts += [' src/pkg/mod_%d/file_%d.py | %d +++---' % (i, f, 10 + f) for f in range(4)]
        parts += [' 4 files changed, 40 insertions(+), 12 deletions(-)', '']
    return '\n'.join(parts).encode()


def _git_status_sample():
    lines = ['On branch main', 'Changes not staged for commit:',
             '  (use "git add <file>..." to update what will be committed)', '']
    lines += ['\tmodified:   src/deep/path/component_%04d.py' % i for i in range(900)]
    lines += ['', 'Untracked files:', '']
    lines += ['\tnew_file_%04d.txt' % i for i in range(300)]
    return '\n'.join(lines).encode()


def _listing_sample():
    return '\n'.join('-rw-r--r--  1 user  staff    %5d Oct 13 10:00 file_%04d.txt'
                     % (1000 + i, i) for i in range(1500)).encode()


def _search_sample():
    return '\n'.join('src/pkg/module_%03d/file_%04d.py:42:        match_target call here'
                     % (i % 97, i) for i in range(1200)).encode()


def _tests_sample():
    lines = ['tests/test_mod_%03d.py::test_case_%04d PASSED' % (i % 31, i) for i in range(1200)]
    lines += ['', '==================== 1200 passed in 42.10s ====================']
    return '\n'.join(lines).encode()


def _build_sample():
    return '\n'.join('src/mod_%03d/file_%04d.ts(%d,%d): error TS2339: Property x does not exist on type Y.'
                     % (i % 41, i, i % 50, i % 80) for i in range(400)).encode()


_FORMAT_SAMPLES = {
    'generic': b'worker-7 processed item number 42 successfully\n' * 1000,
    'git-status': _git_status_sample(),
    'git-log': _git_log_sample(),
    'listing': _listing_sample(),
    'search': _search_sample(),
    'tests': _tests_sample(),
    'build': _build_sample(),
}


def _multibyte_sample(fmt):
    """CJK mixed with Latin in each domain's shape. A CJK character is ~3
    bytes and ~1 token, so a comparison in characters misjudges cost."""
    if fmt == 'search':
        # A search handler emits its few kept match lines verbatim: with
        # CJK-dense matches it produces FEWER characters than generic yet
        # MORE delivered bytes. The byte comparison must catch that.
        cjk = lambda seed: ''.join(
            chr(0x4E00 + (seed * 37 + i * 11) % 0x5000) for i in range(40))
        parts = []
        for i in range(60):
            parts += [' src/f%d.py | %d ++- %s' % (i, i % 7, 'x' * 45),
                      '    fix: thing %d %s' % (i, 'y' * 45), 'z' * 65]
        for f in range(5):
            parts += ['src/m%d.py:%d: %s' % (f, f * 20 + n, cjk(f * 20 + n))
                      for n in range(20)]
        for i in range(500):
            parts += [' src/g%d.py | %d ++- %s' % (i, i % 7, 'x' * 45),
                      'z' * 65]
        return '\n'.join(parts).encode('utf-8')
    cjk = '壱弐参四五六七八九十字符列です丂丄丅丆丏丒丟丠両丣並丩丮丯'
    parts = []
    for i in range(1200):
        if fmt == 'git-status':
            parts.append('\tmodified:   src/pkg/コンポーネント_%04d.py' % i)
        elif fmt == 'git-log':
            parts += ['commit %040x' % i, 'Author: A U Thor <author@example.com>',
                      '    fix(モジュール): %s item %d' % (cjk, i), '']
        elif fmt == 'listing':
            parts.append('-rw-r--r--  1 user  staff  %5d Oct 13 10:00 ファイル_%04d.txt' % (1000 + i, i))
        elif fmt == 'tests':
            parts.append('tests/test_モジュール_%03d.py::test_ケース_%04d PASSED' % (i % 31, i))
        elif fmt == 'build':
            parts.append('src/mod_%03d/ファイル_%04d.ts(%d,%d): error TS2339: プロパティ x が型 Y に存在しません'
                         % (i % 41, i, i % 50, i % 80))
        else:
            parts.append('worker-7 %s processed item number %d successfully' % (cjk, i))
    return '\n'.join(parts).encode('utf-8')


_FORMAT_CASES = ([(fmt, 'ascii', raw) for fmt, raw in _FORMAT_SAMPLES.items()]
                 + [(fmt, 'multibyte', _multibyte_sample(fmt)) for fmt in sorted(_FORMAT_SAMPLES)])


@pytest.mark.parametrize('fmt,label,raw', _FORMAT_CASES,
                         ids=['%s-%s' % (f, l) for f, l, _ in _FORMAT_CASES])
def test_named_format_never_worse_than_generic(tmp_path, monkeypatch, fmt, label, raw):
    """A named format must never do worse than generic on the same bytes.
    The bound is what is delivered: encoded bytes, not characters."""
    generic = _filtered(tmp_path, monkeypatch, 'generic', raw)
    named = _filtered(tmp_path, monkeypatch, fmt, raw)
    assert len(named.stdout) <= len(generic.stdout), (fmt, len(named.stdout), len(generic.stdout))


_WRONG_DOMAIN = {
    'git-status': 'search', 'git-log': 'search', 'listing': 'search',
    'tests': 'search', 'build': 'search', 'search': 'tests',
}


@pytest.mark.parametrize('fmt', sorted(_WRONG_DOMAIN))
def test_wrong_hint_delivers_generic_result(tmp_path, monkeypatch, fmt):
    """A named hint that finds no structure for its format must not emit a
    near-empty body: it delivers what the generic handler produced."""
    raw = _FORMAT_SAMPLES[_WRONG_DOMAIN[fmt]]
    named = _filtered(tmp_path, monkeypatch, fmt, raw)
    generic = _filtered(tmp_path, monkeypatch, 'generic', raw)
    strip_id = lambda body: re.sub(rb'capture-[0-9a-f]{32}', b'capture-X', body)
    assert strip_id(named.stdout) == strip_id(generic.stdout), (fmt, len(named.stdout), len(generic.stdout))


@pytest.mark.parametrize('fmt', sorted(_FORMAT_SAMPLES))
@pytest.mark.parametrize('raw,code', [
    (b'github_pat_' + b'A' * 40 + b'\n' + b'x\n' * 1000, 0),   # credential
    (b'\xff' * 1000, 0),                                        # binary
    (b'hello\n', 0),                                            # tiny
    (b'x\n' * 1000, 3),                                         # failure
])
def test_named_format_passthrough_battery(tmp_path, monkeypatch, fmt, raw, code):
    """Credential, binary, tiny and failing captures pass through raw under
    every format hint."""
    value = _filtered(tmp_path, monkeypatch, fmt, raw, code=code)
    assert value.stdout == raw and value.code == code


_SGR = b'\x1b[1;31m'
_SGR_END = b'\x1b[m'
_COLOUR_SPLIT_SECRETS = [
    b'GITHUB_TOKEN=ghp_' + _SGR + b'abcdefghijklmnopqrstuvwxyz0123456789' + _SGR_END,
    b'AWS_SECRET_ACCESS_KEY=AKIA' + _SGR + b'IOSFODNN7EXAMPLE' + _SGR_END,
    b'key=sk-ant-' + _SGR + b'notarealkeynotarealkeynotareal' + _SGR_END,
]


@pytest.mark.parametrize('fmt', sorted(_FORMAT_SAMPLES))
@pytest.mark.parametrize('secret', _COLOUR_SPLIT_SECRETS)
def test_colour_split_credential_passes_raw(tmp_path, monkeypatch, fmt, secret):
    """grep --color=always puts SGR escapes inside a matched token. The
    credential gate must see the escape-stripped text too, or the joined
    token is compressed and stored instead of passed through raw."""
    raw = b'x\n' * 500 + secret + b'\n' + b'y\n' * 500
    value = _filtered(tmp_path, monkeypatch, fmt, raw)
    assert value.stdout == raw and value.code == 0
    assert value.outcome == 'credential_passthrough'


def test_capture_leaf_as_directory_names_leaf(tmp_path, monkeypatch):
    """A capture leaf that is a directory must refuse by name; a bare
    '[Errno 21] Is a directory: <fd>' leaks a file descriptor number."""
    import devin_compress as d
    import devin_state as s
    monkeypatch.setenv('HOME', str(tmp_path))
    s.enable_consent()
    root = s.prepare_capture()
    (root / 'stdout').unlink()
    (root / 'stdout').mkdir()
    with pytest.raises(OSError) as info:
        d.read_pair(root, sealed=False)
    assert 'stdout' in str(info.value) and 'Is a directory' not in str(info.value)


# --- Wrong-hint class: a named format on foreign-domain output ---


def _git_stat_sample():
    """git log --stat with prose bodies carrying the shapes that fool the
    status parser: numbered lists, leading 'M '/'A '/'??', '1.' lines,
    indented text and mid-sentence section names."""
    parts = []
    for i in range(40):
        parts += ['commit %040x' % i,
                  'Author: A U Thor <author@example.com>',
                  'Date:   Tue Oct 13 10:%02d:00 2026 +0300' % (i % 60), '']
        parts += ['    fix(pkg-%d): rework the %dth stage (change-%04d)' % (i % 13, i, i), '',
                  '    The body explains what changed in rev-%04d:' % i, '',
                  '    1. The opening quote is atomic (lookahead plus backreference), so a skipped quoted',
                  '    M paths behave differently once the second stage runs (see ticket-%04d)' % i,
                  '    A reference section follows where Untracked files: appear in prose',
                  '    ?? marks and phrases like Changes not staged can sit mid-sentence',
                  '']
        if i % 19 == 0:
            parts += ['        nested indented detail line %d' % i, '']
        parts += [' src/pkg/mod_%02d/file_%03d.py | %d +++---'
                  % (i % 13, (i * 7 + f) % 100, 10 + (i + f) % 8) for f in range(6)]
        parts += [' 6 files changed, 60 insertions(+), 18 deletions(-) in range-%04d' % i, '']
    return '\n'.join(parts).encode()


def _git_oneline_sample():
    return '\n'.join('%07x change number %d in the module' % (i * 2654435761 % (1 << 28), i)
                     for i in range(1500)).encode()


def _git_status_short_sample():
    lines = ['## main...origin/main [ahead 2]']
    lines += [' M src/pkg/file_%04d.py' % i for i in range(400)]
    lines += ['M  src/staged/file_%04d.py' % i for i in range(200)]
    lines += ['?? scratch_%04d.txt' % i for i in range(150)]
    return '\n'.join(lines).encode()


def _markdown_doc_sample():
    """A markdown document that quotes git-status-looking lines mid-text: an
    'On branch' sentence and '## ' headings away from the first line. Loose
    single-line checks would call this git status output."""
    parts = ['# Design notes for the release process', '']
    for i in range(300):
        parts += ['Section %d discusses rollout step %d in detail.' % (i, i),
                  'The paragraph expands on the reasoning for step %d.' % i, '']
        if i == 40:
            parts += ['On branch naming, we keep the existing convention.', '']
        if i == 80:
            parts += ['## Rollback plan', 'Text under the heading.', '']
        if i == 160:
            parts += ['## Checklist', 'More prose follows here.', '']
    return '\n'.join(parts).encode()


def _doc_with_total_sample():
    """A log-style document with a mid-file 'total 12' line. The ls -l header
    is only authoritative as the first line of a listing."""
    parts = ['processing report for batch run', '']
    for i in range(400):
        parts.append('worker node %d handled request %d cleanly' % (i % 17, i))
        if i == 200:
            parts += ['', 'total 12', '']
    return '\n'.join(parts).encode()


def _prose_with_commit_sample():
    """Prose that mentions 'commit <hex>' mid-text. A commit header is only a
    git-log signature on the first line of the capture."""
    parts = ['Migration notes for the storage layer', '']
    for i in range(400):
        parts.append('entry %d records the state after step %d' % (i, i))
        if i == 150:
            parts += ['',
                      'commit a94a8fe5ccb19ba61c4c0873d391e987982fbbd3 changed the layout',
                      '']
    return '\n'.join(parts).encode()


_DOMAIN_SAMPLES = {
    'git_stat': _git_stat_sample(),
    'git_oneline': _git_oneline_sample(),
    'git_status_long': _git_status_sample(),
    'git_status_short': _git_status_short_sample(),
    'ls_la': _listing_sample(),
    'grep': _search_sample(),
    'pytest_run': _tests_sample(),
    'build_log': _build_sample(),
    'md_doc': _markdown_doc_sample(),
    'doc_with_total': _doc_with_total_sample(),
    'prose_with_commit': _prose_with_commit_sample(),
}

_DOMAIN_FORMATS = {
    'git-status': ('git_status_long',),
    'git-log': ('git_stat', 'git_oneline'),
    'listing': ('ls_la',),
    'search': ('grep',),
    'tests': ('pytest_run',),
    'build': ('build_log',),
}

# Delivered payload byte counts for own-domain cells on the pre-change adapter
# (the compressed body between the untrusted-output markers; the pointer line
# embeds sys.executable so the frame is not interpreter-stable). A named
# handler must keep compressing its own domain exactly as well as before.
_OWN_DOMAIN_PIN = {
    ('git-status', 'git_status_long'): 1275,
    ('git-log', 'git_stat'): 4113,
    ('git-log', 'git_oneline'): 1632,
    ('listing', 'ls_la'): 2522,
    ('search', 'grep'): 2682,
    ('tests', 'pytest_run'): 1805,
    ('build', 'build_log'): 33469,
}


def _strip_id(body):
    return re.sub(rb'capture-[0-9a-f]{32}', b'capture-X', body)


def _payload(result):
    """The compressed body inside the capture frame; raw bytes when the
    capture was passed through untouched."""
    m = re.match(rb'[^\n]*\n\[Begin untrusted captured output\]\n'
                 rb'(.*)\n\[End untrusted captured output\]\n?$',
                 result.stdout, re.S)
    return m.group(1) if m else result.stdout


_CROSS = [(fmt, domain) for fmt, domains in _DOMAIN_FORMATS.items()
          for domain in _DOMAIN_SAMPLES if domain not in domains]


@pytest.mark.parametrize('fmt,domain', _CROSS, ids=['%s-on-%s' % c for c in _CROSS])
def test_wrong_hint_delivers_generic_everywhere(tmp_path, monkeypatch, fmt, domain):
    """A named format hinted on output from another domain must deliver what
    generic delivers for the same bytes -- a named handler that sees foreign
    text can emit a near-empty body from coincidental line matches."""
    raw = _DOMAIN_SAMPLES[domain]
    named = _filtered(tmp_path, monkeypatch, fmt, raw)
    generic = _filtered(tmp_path, monkeypatch, 'generic', raw)
    assert _strip_id(named.stdout) == _strip_id(generic.stdout), (
        fmt, domain, len(named.stdout), len(generic.stdout))


_OWN = [(fmt, domain) for fmt, domains in _DOMAIN_FORMATS.items() for domain in domains]


@pytest.mark.parametrize('fmt,domain', _OWN, ids=['%s-on-%s' % c for c in _OWN])
def test_own_domain_pinned(tmp_path, monkeypatch, fmt, domain):
    """On its own domain a named format is no larger than generic and keeps
    delivering what it did in 5.13.40 -- the detector must not shrink the
    formats it is guarding."""
    raw = _DOMAIN_SAMPLES[domain]
    named = _filtered(tmp_path, monkeypatch, fmt, raw)
    generic = _filtered(tmp_path, monkeypatch, 'generic', raw)
    assert len(named.stdout) <= len(generic.stdout), (
        fmt, domain, len(named.stdout), len(generic.stdout))
    assert len(_payload(named)) == _OWN_DOMAIN_PIN[(fmt, domain)], (
        fmt, domain, len(_payload(named)), _OWN_DOMAIN_PIN[(fmt, domain)])


@pytest.mark.parametrize('fmt,domain', [
    ('git-status', 'md_doc'),
    ('listing', 'doc_with_total'),
    ('git-log', 'prose_with_commit'),
])
def test_detector_rejects_quoted_signature_lines(fmt, domain):
    """A format signature quoted mid-text is not evidence: headers like
    'On branch', 'total N' and 'commit <hash>' only count on the first
    non-empty line, where the real command actually prints them."""
    import devin_compress as d
    assert not d._looks_like_format(fmt, _DOMAIN_SAMPLES[domain].decode())


def test_live_repro_git_stat_hinted_git_status(tmp_path, monkeypatch):
    """The live 5.13.40 reproduction: git log --stat filtered with
    --format git-status delivered 523 bytes ('branch: ?, 1. The opening
    quote is atomic ...'). The result must be exactly the generic result."""
    raw = _git_stat_sample()
    named = _filtered(tmp_path, monkeypatch, 'git-status', raw)
    generic = _filtered(tmp_path, monkeypatch, 'generic', raw)
    assert _strip_id(named.stdout) == _strip_id(generic.stdout)
    assert b'branch: ?' not in named.stdout


def test_short_status_keeps_its_file_names(tmp_path, monkeypatch):
    """`git status --short` has no sections for the git-status handler to
    read: hinted git-status it used to deliver the header alone and drop every
    file name. It must take the generic route instead."""
    raw = _DOMAIN_SAMPLES['git_status_short']
    named = _filtered(tmp_path, monkeypatch, 'git-status', raw)
    generic = _filtered(tmp_path, monkeypatch, 'generic', raw)
    assert _strip_id(named.stdout) == _strip_id(generic.stdout)
    assert len(_payload(named)) > 42
    assert b'branch: ?' not in named.stdout


def test_auto_index_is_checked_only_under_its_lock(tmp_path, monkeypatch):
    """Every writer replaces auto-captures.json while holding its flock, and a
    stat racing a rival's os.replace can return the replaced inode with
    st_nlink 0 (Linux and macOS both do). Checked before taking the lock, the
    index was refused as owner-only state: the compressed run had already
    delivered, so its kept capture silently never reached the index."""
    import fcntl
    import json
    import devin_compress as d
    import devin_state as s
    monkeypatch.setenv('HOME', str(tmp_path)); s.enable_consent()
    first, second = 'capture-' + 'a' * 32, 'capture-' + 'b' * 32
    d._prune_auto_captures(first)
    index = s.data_dir() / d.AUTO_INDEX
    locked = []
    real_flock, real_lstat = fcntl.flock, Path.lstat

    def flock(fd, operation):
        real_flock(fd, operation)
        locked.append(operation & fcntl.LOCK_EX)

    def lstat(self):
        info = real_lstat(self)
        if self == index and not any(locked):
            # Outside the lock a rival may be replacing the index right now.
            info = os.stat_result((info.st_mode, info.st_ino, info.st_dev, 0, info.st_uid,
                                   info.st_gid, info.st_size, info.st_atime, info.st_mtime, info.st_ctime))
        return info

    monkeypatch.setattr(fcntl, 'flock', flock)
    monkeypatch.setattr(Path, 'lstat', lstat)
    d._prune_auto_captures(second)
    assert [name for name, _ in json.loads(index.read_text())] == [first, second]


@pytest.mark.parametrize('stage', ['leaf', 'marker', 'publish'])
def test_failed_prepare_leaves_no_partial_capture(tmp_path, monkeypatch, stage):
    """The runner falls back to the original command when prepare_capture
    raises; a capture directory made before the failure must not stay behind,
    unindexed and never cleaned up."""
    import tempfile
    import devin_state as s
    monkeypatch.setenv('HOME', str(tmp_path)); s.enable_consent()
    if stage == 'leaf':
        real = s.private_path
        def private_path(path, **kwargs):
            if path.name == 'stderr':
                raise OSError('injected')
            return real(path, **kwargs)
        monkeypatch.setattr(s, 'private_path', private_path)
    elif stage == 'marker':
        real = tempfile.NamedTemporaryFile
        def named(*args, **kwargs):
            handle = real(*args, **kwargs)
            handle.write = lambda data: (_ for _ in ()).throw(OSError('injected'))
            return handle
        monkeypatch.setattr(tempfile, 'NamedTemporaryFile', named)
    else:
        real = os.replace
        monkeypatch.setattr(os, 'replace', lambda a, b: (_ for _ in ()).throw(OSError('injected'))
                            if Path(b).name == '.prepared' else real(a, b))
    captures = s.data_dir() / 'captures'
    with pytest.raises(OSError, match='injected'):
        s.prepare_capture()
    assert list(captures.iterdir()) == []
