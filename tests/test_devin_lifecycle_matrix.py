"""Differential lifecycle matrix for the Devin adapter.

Every cell starts from a described on-disk state, runs one lifecycle command
with the current adapter and a twin built by the embedded previous-release
installer, and asserts the invariants that must hold in every cell: a clean
exit class (no crash, no traceback), no user-written file removed, nothing
outside the Devin home touched, and a documented route back to an
installable state. Where the previous release handled a cell safely, the
current adapter must handle it too: the baseline column is the pin.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
FIXTURE = Path(__file__).resolve().parents[1] / 'tests' / 'fixtures' / 'devin_229_baseline'
MARKER = 'devin-output-filter-v1'


def _devin(home: Path) -> Path:
    return home / '.config' / 'devin'


def _data(home: Path) -> Path:
    return _devin(home) / 'token-optimizer'


def _skill(home: Path) -> Path:
    return _devin(home) / 'skills' / 'token-optimizer-output'


def _env(home: Path, extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env['HOME'] = str(home)
    for key in ('TOKEN_OPTIMIZER_DEVIN_HOME', 'XDG_CONFIG_HOME',
                'TOKEN_OPTIMIZER_RUNTIME', 'DEVIN_CONFIG_DIR'):
        env.pop(key, None)
    if extra:
        env.update(extra)
    return env


def _baseline_installer(baseline_dir: Path) -> Path:
    return baseline_dir / 'skills/token-optimizer/scripts/devin_install.py'


def _baseline_run(baseline_dir: Path, home: Path, argv, extra_env=None):
    run = subprocess.run(
        [sys.executable, str(_baseline_installer(baseline_dir)), *argv],
        env=_env(home, extra_env), capture_output=True, text=True, timeout=60)
    return run.returncode, (run.stderr.strip().splitlines() or [''])[-1]


def _install_baseline(baseline_dir: Path, home: Path, extra_env=None):
    rc, err = _baseline_run(baseline_dir, home, ['install'], extra_env)
    assert rc == 0, err


def _install_current(home: Path, monkeypatch, extra_env=None):
    import devin_install as inst
    monkeypatch.setenv('HOME', str(home))
    for key in ('TOKEN_OPTIMIZER_DEVIN_HOME', 'XDG_CONFIG_HOME'):
        monkeypatch.delenv(key, raising=False)
    if extra_env:
        for key, value in extra_env.items():
            monkeypatch.setenv(key, value)
    inst.install()


def _run_current(fn):
    import contextlib
    import io
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn()
        return 0, buf.getvalue().strip()
    except OSError as exc:
        return 1, str(exc)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2, str(exc)
    except Exception as exc:  # a crash class is always a bug in a cell
        return 99, repr(exc)


def _craft_half(home: Path):
    """Pending record plus a partial payload and no manifest: the state an
    interrupted publication leaves behind."""
    data = _data(home)
    record = json.loads((data / 'install.json').read_text())
    pending = data / 'pending-install.json'
    pending.write_text(json.dumps({'owner': MARKER, 'files': record['files']}))
    pending.chmod(0o600)
    (data / 'install.json').unlink()
    plugin = data / 'plugin'
    for leaf in sorted(p.name for p in plugin.iterdir())[:3]:
        (plugin / leaf).unlink()


def _build(home: Path, name: str, installer) -> dict:
    """Create the starting state; return {'keep': [...], 'env': {...}}."""
    ctx = {'keep': [], 'env': None}
    if name == 'old_dotdot':
        pivot = home / 'seg'
        pivot.mkdir()
        installer(str(home / 'seg/../dh//inner'))
        ctx['env'] = {'TOKEN_OPTIMIZER_DEVIN_HOME': str(home / 'dh' / 'inner')}
        ctx['keep'] = [pivot]
        return ctx
    installer()
    if name == 'edited_skill':
        leaf = _skill(home) / 'SKILL.md'
        leaf.write_text(leaf.read_text() + 'user edit\n')
        ctx['keep'] = [leaf]
    elif name == 'edited_payload':
        leaf = _data(home) / 'plugin' / 'devin_state.py'
        leaf.write_text(leaf.read_text() + '# user edit\n')
        ctx['keep'] = [leaf]
    elif name == 'stray_plugin':
        leaf = _data(home) / 'plugin' / '.DS_Store'
        leaf.write_bytes(b'finder metadata')
        ctx['keep'] = [leaf]
    elif name == 'stray_skill':
        leaf = _skill(home) / 'user-notes.md'
        leaf.write_text('keep me\n')
        ctx['keep'] = [leaf]
    elif name == 'skill_moved':
        parked = home / 'parked-skill'
        shutil.move(str(_skill(home)), str(parked))
        ctx['keep'] = [parked / 'SKILL.md']
    elif name == 'skill_deleted':
        shutil.rmtree(_skill(home))
    elif name == 'data_deleted':
        shutil.rmtree(_data(home))
    elif name == 'manifest_missing':
        (_data(home) / 'install.json').unlink()
    elif name == 'manifest_unreadable':
        (_data(home) / 'install.json').write_bytes(b'not json at all')
    elif name == 'stored_capture':
        capture = _data(home) / 'captures' / ('capture-' + '0' * 32)
        capture.mkdir(parents=True)
        capture.chmod(0o700)
        for leaf in ('stdout', 'stderr'):
            (capture / leaf).write_bytes(b'sealed bytes')
            (capture / leaf).chmod(0o600)
    elif name == 'half_install':
        _craft_half(home)
    elif name == 'old_half':
        _craft_half(home)
    elif name in ('clean', 'old_install'):
        pass
    else:
        raise AssertionError('unknown state ' + name)
    return ctx


STATES = [
    'clean', 'edited_skill', 'edited_payload', 'stray_plugin', 'stray_skill',
    'skill_moved', 'skill_deleted', 'data_deleted', 'manifest_missing',
    'manifest_unreadable', 'half_install', 'old_install', 'old_dotdot', 'old_half',
    'stored_capture',
]

COMMANDS = ['install', 'uninstall', 'purge', 'doctor', 'install_again']


def _command(home: Path, command: str, monkeypatch, extra_env=None):
    import devin_install as inst
    import devin_state as state
    monkeypatch.setenv('HOME', str(home))
    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)
    if command == 'install':
        return _run_current(inst.install)
    if command == 'install_again':
        rc, detail = _run_current(inst.install)
        if rc != 0:
            return rc, detail
        return _run_current(inst.install)
    if command == 'uninstall':
        return _run_current(inst.uninstall)
    if command == 'purge':
        return _run_current(lambda: inst.uninstall(purge=True))
    doctor = state.data_dir() / 'plugin' / 'devin_doctor.py'
    if not doctor.is_file():
        return 'n/a', ''
    run = subprocess.run([sys.executable, str(doctor)], env=_env(home, extra_env),
                         capture_output=True, text=True, timeout=60)
    assert 'Traceback' not in run.stderr, run.stderr
    return run.returncode, run.stderr.strip()[-160:]


def _root(home: Path, extra_env=None) -> Path:
    override = (extra_env or {}).get('TOKEN_OPTIMIZER_DEVIN_HOME')
    return Path(override).resolve() if override else _devin(home)


def _baseline_command(baseline_dir: Path, home: Path, command: str, extra_env=None):
    if command == 'doctor':
        doctor = _root(home, extra_env) / 'token-optimizer' / 'plugin' / 'devin_doctor.py'
        if not doctor.is_file():
            return 'n/a', ''
        run = subprocess.run([sys.executable, str(doctor)], env=_env(home, extra_env),
                             capture_output=True, text=True, timeout=60)
        assert 'Traceback' not in run.stderr, run.stderr
        return run.returncode, run.stderr.strip()[-160:]
    if command == 'install_again':
        rc, detail = _baseline_run(baseline_dir, home, ['install'], extra_env)
        if rc != 0:
            return rc, detail
        return _baseline_run(baseline_dir, home, ['install'], extra_env)
    argv = {'install': ['install'], 'uninstall': ['uninstall'],
            'purge': ['uninstall', '--purge']}[command]
    return _baseline_run(baseline_dir, home, argv, extra_env)


def _outside_snapshot(home: Path, extra_env=None) -> dict:
    """Map each path outside the Devin home to a content fingerprint, so a
    command that writes outside the Devin home -- a new file, changed bytes,
    or a removed file -- fails the cell rather than passing silently."""
    import hashlib
    override = (extra_env or {}).get('TOKEN_OPTIMIZER_DEVIN_HOME')
    root = Path(override).resolve() if override else _devin(home)
    # The adapter legitimately owns a second root: the ancestor-scan cache
    # under XDG_CACHE_HOME (runtime_env.py). It is adapter state, not user
    # content, so it is inside for the purposes of this snapshot.
    cache_base = (extra_env or {}).get('XDG_CACHE_HOME')
    cache_root = (Path(cache_base) if cache_base else home / '.cache') / 'token-optimizer'
    inside = (root.resolve(), cache_root.resolve())
    out = {}
    for path in home.rglob('*'):
        if any(path == o or o in path.parents for o in inside):
            continue
        if path.is_symlink():
            out[str(path)] = 'symlink:' + os.readlink(path)
        elif path.is_dir():
            out[str(path)] = 'dir'
        elif path.is_file():
            out[str(path)] = 'file:' + hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            out[str(path)] = 'other'
    return out


def _documented_recovery(home: Path, monkeypatch, extra_env=None):
    """The documented escape hatches must always return to an installable
    state: plain install, then purge, then move the adapter folders aside."""
    import devin_install as inst
    import devin_state as state
    monkeypatch.setenv('HOME', str(home))
    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)
    try:
        inst.install()
        return 'install'
    except OSError:
        pass
    try:
        inst.uninstall(purge=True)
    except OSError:
        pass
    try:
        inst.install()
        return 'purge'
    except OSError:
        pass
    parked = home / 'parked-aside'
    parked.mkdir(exist_ok=True)
    for folder in (state.data_dir(), state.home() / 'skills' / 'token-optimizer-output'):
        if folder.exists() or folder.is_symlink():
            target = parked / folder.name
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(folder), str(target))
    inst.install()
    return 'move-aside'


# Cells where the previous release's success was the unsafe behaviour the new
# code intentionally closes: the pin does not apply.
PIN_EXEMPT = {('stray_plugin', 'doctor'), ('stray_plugin', 'install'),
              ('stray_plugin', 'install_again'), ('stray_plugin', 'purge')}


@pytest.mark.parametrize('command', COMMANDS)
@pytest.mark.parametrize('state_name', STATES)
def test_lifecycle_cell(tmp_path, monkeypatch, state_name, command):
    tmp_path.chmod(0o700)
    cur_home = tmp_path / 'cur'
    base_home = tmp_path / 'base'
    cur_home.mkdir()
    base_home.mkdir()
    baseline_dir = tmp_path / 'baseline-src'
    from _devin_baseline import copy_baseline
    copy_baseline(FIXTURE, baseline_dir)

    # Current-adapter cell home.
    if state_name.startswith('old_'):
        def cur_installer(override=None):
            env = {'TOKEN_OPTIMIZER_DEVIN_HOME': override} if override else None
            _install_baseline(baseline_dir, cur_home, env)
    else:
        def cur_installer(override=None):
            _install_current(cur_home, monkeypatch)
    cur_ctx = _build(cur_home, state_name, cur_installer)
    canary = cur_home / 'outside-canary.txt'
    canary.write_text('KEEP\n')
    # Pre-create the shared cache parent so the doctor's adapter-owned subtree
    # underneath it does not present as a brand-new outside path; a foreign
    # child next to it would still trip the snapshot.
    (cur_home / '.cache').mkdir(exist_ok=True)
    before_outside = _outside_snapshot(cur_home, cur_ctx['env'])

    # Previous-release twin: same state built by its own installer.
    def base_installer(override=None):
        env = {'TOKEN_OPTIMIZER_DEVIN_HOME': override} if override else None
        _install_baseline(baseline_dir, base_home, env)
    base_ctx = _build(base_home, state_name, base_installer)

    rc_base, base_detail = _baseline_command(baseline_dir, base_home, command, base_ctx['env'])
    rc_cur, cur_detail = _command(cur_home, command, monkeypatch, cur_ctx['env'])

    # Invariants for every cell.
    assert rc_cur in (0, 1, 2, 'n/a'), (rc_cur, cur_detail)
    for keep in cur_ctx['keep']:
        assert keep.exists(), f'{command} on {state_name} removed user file {keep}'
    assert canary.read_text() == 'KEEP\n'
    after_outside = _outside_snapshot(cur_home, cur_ctx['env'])
    assert before_outside == after_outside, (
        f'{command} on {state_name} wrote outside the Devin home: '
        + str(sorted(set(before_outside.items()) ^ set(after_outside.items()))))
    # Effect assertions: a "successful" command must have done the thing. A
    # rc=0 'No owned Devin install found' run removed nothing by design, and
    # stray unowned files may legally survive; the adapter's own payload and
    # manifest may not.
    data = _root(cur_home, cur_ctx['env']) / 'token-optimizer'
    if rc_cur == 0 and command == 'uninstall' and 'Removed owned' in cur_detail:
        import devin_verify as verify
        assert not (data / 'install.json').exists(), (
            f'uninstall on {state_name} succeeded but the manifest survived')
        leftover = [n for n in verify.PAYLOAD if (data / 'plugin' / n).exists()]
        assert not leftover, (
            f'uninstall on {state_name} succeeded but payload files survived: {leftover}')
    if rc_cur == 0 and command == 'purge':
        state_leaves = ('install.json', 'pending-install.json', 'consent.json',
                        'metrics.db', 'metrics.db-journal', 'metrics.db-wal',
                        'metrics.db-shm', 'session-salt')
        leftover_state = [n for n in state_leaves if (data / n).exists()]
        assert not leftover_state, (
            f'purge on {state_name} succeeded but state survived: {leftover_state}')
        caps = data / 'captures'
        leftover_caps = [p.name for p in caps.iterdir()] if caps.is_dir() else []
        assert not leftover_caps, (
            f'purge on {state_name} succeeded but captures survived: {leftover_caps}')
        # Purge may legally preserve edited or unowned folders, but only when
        # it told the user it did.
        skill = _root(cur_home, cur_ctx['env']) / 'skills' / 'token-optimizer-output'
        surviving = [f for f in (data / 'plugin', skill) if f.exists()]
        if surviving:
            assert 'preserved' in cur_detail, (
                f'purge on {state_name} left {surviving} without saying so')
    # Where the previous release handled the cell safely, this one must too.
    if rc_base == 0 and (state_name, command) not in PIN_EXEMPT:
        assert rc_cur == 0, (
            f'{command} on {state_name}: baseline rc=0 ({base_detail}) but current '
            f'rc={rc_cur} ({cur_detail})')
    # The user is never left stuck: a documented sequence must reinstall.
    route = _documented_recovery(cur_home, monkeypatch, cur_ctx['env'])
    print(f'MATRIX|{state_name}|{command}|cur={rc_cur}|base={rc_base}|via={route}')


# ---------------------------------------------------------------------------
# Preflight matrix: the new safety preflights added on top of release 5.13.41.
# Each cell builds a real on-disk state in an isolated HOME and runs the real
# installer entrypoints in-process (the same code `install.sh --devin`
# dispatches to), with no mocks of the functions under test.
# ---------------------------------------------------------------------------


def _new_home(tmp_path: Path, monkeypatch, name: str) -> Path:
    """A fresh isolated HOME inside this test's tmp dir."""
    home = tmp_path / name
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    for key in ('TOKEN_OPTIMIZER_DEVIN_HOME', 'XDG_CONFIG_HOME',
                'TOKEN_OPTIMIZER_RUNTIME', 'DEVIN_CONFIG_DIR'):
        monkeypatch.delenv(key, raising=False)
    return home


def _invoke(home: Path, monkeypatch, action: str, *, dry_run=False, purge=False):
    """One lifecycle command on the current adapter, dispatched exactly the
    way `bash install.sh --devin [--uninstall] [--dry-run] [--purge]` maps
    its flags onto devin_install's entrypoints."""
    import devin_install as inst
    monkeypatch.setenv('HOME', str(home))
    for key in ('TOKEN_OPTIMIZER_DEVIN_HOME', 'XDG_CONFIG_HOME'):
        monkeypatch.delenv(key, raising=False)
    if action == 'install':
        return _run_current(lambda: inst.install(dry_run=dry_run))
    if action == 'uninstall':
        return _run_current(lambda: inst.uninstall(dry_run=dry_run, purge=purge))
    raise AssertionError('unknown lifecycle action ' + action)


def _tree_snapshot(root: Path) -> dict:
    """Full before/after fingerprint of a synthetic home: every path mapped to
    its type, permission bits and content hash, so a command that created,
    removed, chmodded or rewrote anything at all fails the cell."""
    out = {}
    for path in sorted([root, *root.rglob('*')]):
        info = path.lstat()
        mode = format(stat.S_IMODE(info.st_mode), '04o')
        if path.is_symlink():
            out[str(path)] = 'symlink:' + mode + ':' + os.readlink(path)
        elif path.is_dir():
            out[str(path)] = 'dir:' + mode
        elif path.is_file():
            out[str(path)] = 'file:' + mode + ':' + hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            out[str(path)] = 'other:' + mode
    return out


def _assert_tree_unchanged(before: dict, home: Path, label: str):
    after = _tree_snapshot(home)
    assert before == after, (
        label + ' changed the tree: '
        + str(sorted(set(before.items()) ^ set(after.items()))))


def _preflight_state(home: Path, name: str, monkeypatch):
    """Build each unsafe install starting state. Returns (path_fragment,
    reason_fragment): the tokens the refusal message must carry so it names
    the offending path and the true reason."""
    data = _data(home)
    if name == 'clean':
        return '', ''
    if name == 'loose_data_dir':
        data.mkdir(parents=True)
        data.chmod(0o755)
        return str(data), 'owner-only'
    if name == 'symlinked_lock':
        data.mkdir(parents=True, mode=0o700)
        target = home / 'lock-target'
        target.write_text('x')
        (data / 'install.lock').symlink_to(target)
        return 'install.lock', 'symlink'
    if name == 'dir_metrics':
        data.mkdir(parents=True, mode=0o700)
        (data / 'metrics.db').mkdir()
        return str(data / 'metrics.db'), 'regular state file'
    if name == 'pending_present':
        data.mkdir(parents=True, mode=0o700)
        lock = data / 'install.lock'
        lock.write_text('')
        lock.chmod(0o600)
        pending = data / 'pending-install.json'
        pending.write_text(json.dumps({'owner': 'unrelated-adapter', 'files': {}}))
        pending.chmod(0o600)
        return 'pending-install.json', 'pending'
    if name in ('foreign_plugin', 'foreign_skill'):
        data.mkdir(parents=True, mode=0o700)
        lock = data / 'install.lock'
        lock.write_text('')
        lock.chmod(0o600)
        foreign = data / 'plugin' if name == 'foreign_plugin' else _skill(home)
        foreign.mkdir(parents=True, mode=0o700)
        leaf = foreign / ('foreign.py' if name == 'foreign_plugin' else 'SKILL.md')
        leaf.write_text('foreign content')
        return str(foreign), 'foreign install destination'
    if name == 'stray_in_destination':
        _install_current(home, monkeypatch)
        stray = data / 'plugin' / 'user-scratch.txt'
        stray.write_text('user notes\n')
        return str(stray), 'unowned files'
    if name == 'deleted_payload':
        _install_current(home, monkeypatch)
        (data / 'plugin' / 'devin_state.py').unlink()
        return 'devin_state.py', 'missing'
    raise AssertionError('unknown preflight state ' + name)


PREFLIGHT_STATES = [
    'clean', 'loose_data_dir', 'symlinked_lock', 'dir_metrics',
    'pending_present', 'foreign_plugin', 'foreign_skill',
    'stray_in_destination', 'deleted_payload',
]


@pytest.mark.parametrize('state_name', PREFLIGHT_STATES)
def test_install_preflight_dry_real_agree(tmp_path, monkeypatch, state_name):
    """For every unsafe starting state a dry-run install exits nonzero
    exactly when the real install refuses, and a refusal writes nothing."""
    dry_home = _new_home(tmp_path, monkeypatch, 'dry')
    real_home = _new_home(tmp_path, monkeypatch, 'real')
    path_dry, reason_dry = _preflight_state(dry_home, state_name, monkeypatch)
    path_real, reason_real = _preflight_state(real_home, state_name, monkeypatch)
    before_dry = _tree_snapshot(dry_home)
    before_real = _tree_snapshot(real_home)

    rc_dry, detail_dry = _invoke(dry_home, monkeypatch, 'install', dry_run=True)
    rc_real, detail_real = _invoke(real_home, monkeypatch, 'install')

    assert rc_dry in (0, 1, 2), (rc_dry, detail_dry)
    assert rc_real in (0, 1, 2), (rc_real, detail_real)
    assert (rc_dry != 0) == (rc_real != 0), (
        f'{state_name}: dry-run rc={rc_dry} ({detail_dry}) but real '
        f'install rc={rc_real} ({detail_real})')
    # A dry-run never writes, whatever the starting state.
    _assert_tree_unchanged(before_dry, dry_home, f'dry-run install on {state_name}')
    if rc_dry == 0:
        assert 'Would install' in detail_dry
        print(f'MATRIX|preflight|{state_name}|dry={rc_dry}|real={rc_real}')
        return
    # Every refusal names the offending path and the true reason.
    assert reason_dry in detail_dry, (state_name, detail_dry)
    assert path_dry in detail_dry, (state_name, detail_dry)
    assert reason_real in detail_real, (state_name, detail_real)
    assert path_real in detail_real, (state_name, detail_real)
    # And the refused real install wrote nothing either.
    _assert_tree_unchanged(before_real, real_home, f'real install on {state_name}')
    print(f'MATRIX|preflight|{state_name}|dry={rc_dry}|real={rc_real}')


def test_install_refusal_names_missing_payload(tmp_path, monkeypatch):
    """Deleting one payload file from a clean install then running install
    must name the missing file, not report an empty offender list."""
    home = _new_home(tmp_path, monkeypatch, 'home')
    _install_current(home, monkeypatch)
    victim = _data(home) / 'plugin' / 'devin_state.py'
    victim.unlink()
    rc, detail = _invoke(home, monkeypatch, 'install')
    assert rc != 0, detail
    assert victim.name in detail, detail
    print(f'MATRIX|preflight|missing_payload|rc={rc}')


def _purge_keep_state(home: Path, name: str, monkeypatch):
    """User content planted inside an otherwise clean install. Returns
    (kept leaf, container the output must name, purge_expected_to_succeed)."""
    _install_current(home, monkeypatch)
    data = _data(home)
    if name == 'edited_payload':
        leaf = data / 'plugin' / 'devin_state.py'
        leaf.write_bytes(leaf.read_bytes() + b'# user edit\n')
        return leaf, leaf.parent, False
    if name == 'stray_plugin':
        leaf = data / 'plugin' / 'user-scratch.txt'
        leaf.write_text('keep me\n')
        return leaf, leaf.parent, True
    if name == 'stray_skill':
        leaf = _skill(home) / 'user-notes.md'
        leaf.write_text('keep me\n')
        return leaf, _skill(home), True
    if name == 'foreign_capture':
        caps = data / 'captures'
        caps.mkdir(mode=0o700)
        leaf = caps / 'keep.txt'
        leaf.write_text('keep me\n')
        return leaf, caps, True
    if name == 'foreign_inside_capture':
        caps = data / 'captures'
        caps.mkdir(mode=0o700)
        cap = caps / ('capture-' + 'f' * 32)
        cap.mkdir(mode=0o700)
        for fixed in ('stdout', 'stderr'):
            (cap / fixed).write_bytes(b'sealed bytes')
            (cap / fixed).chmod(0o600)
        leaf = cap / 'user-note.txt'
        leaf.write_text('keep me\n')
        return leaf, caps, True
    raise AssertionError('unknown purge-keep state ' + name)


PURGE_KEEP_STATES = ['edited_payload', 'stray_plugin', 'stray_skill',
                     'foreign_capture', 'foreign_inside_capture']


@pytest.mark.parametrize('name', PURGE_KEEP_STATES)
def test_purge_never_deletes_user_content(tmp_path, monkeypatch, name):
    """Whatever purge does, user content survives byte-identical and the
    output says where it was preserved."""
    home = _new_home(tmp_path, monkeypatch, 'home')
    leaf, container, expect_success = _purge_keep_state(home, name, monkeypatch)
    before_bytes = leaf.read_bytes()
    before_tree = _tree_snapshot(home)
    rc, detail = _invoke(home, monkeypatch, 'uninstall', purge=True)
    assert leaf.exists(), f'purge deleted user file {leaf}'
    assert leaf.read_bytes() == before_bytes, f'purge rewrote user file {leaf}'
    if expect_success:
        assert rc == 0, (name, detail)
        assert 'preserved' in detail, (name, detail)
        assert str(container) in detail, (name, detail)
    else:
        # An edited owned file refuses the whole purge, names itself and
        # nothing at all was removed.
        assert rc != 0, (name, detail)
        assert str(leaf) in detail, (name, detail)
        _assert_tree_unchanged(before_tree, home, f'purge on {name}')
    print(f'MATRIX|purge_keep|{name}|rc={rc}')


def _purge_refusal_state(home: Path, name: str, monkeypatch):
    """A state purge must refuse before removing anything. Returns
    (path_fragment, reason_fragment) for the refusal message."""
    _install_current(home, monkeypatch)
    data = _data(home)
    if name == 'dir_state_leaf':
        leaf = data / 'metrics.db'
        leaf.mkdir(mode=0o700)
        return str(leaf), 'directory'
    if name == 'captures_symlink':
        target = home / 'real-captures'
        target.mkdir()
        (target / 'keep.txt').write_text('keep me\n')
        (data / 'captures').symlink_to(target)
        return 'captures', 'symlink'
    if name == 'nonregular_capture_leaf':
        caps = data / 'captures'
        caps.mkdir(mode=0o700)
        cap = caps / ('capture-' + 'b' * 32)
        cap.mkdir(mode=0o700)
        (cap / 'stdout').mkdir()
        return str(cap / 'stdout'), 'regular'
    raise AssertionError('unknown purge-refusal state ' + name)


PURGE_REFUSAL_STATES = ['dir_state_leaf', 'captures_symlink', 'nonregular_capture_leaf']


@pytest.mark.parametrize('name', PURGE_REFUSAL_STATES)
def test_purge_preflight_is_all_or_nothing(tmp_path, monkeypatch, name):
    """When purge refuses, nothing at all was already removed: the whole
    home snapshot, payload and state included, is byte-for-byte identical."""
    home = _new_home(tmp_path, monkeypatch, 'home')
    expect_path, expect_reason = _purge_refusal_state(home, name, monkeypatch)
    before = _tree_snapshot(home)
    # Dry-run and real purge share the same preflight: both must refuse and
    # neither may have removed anything by the time it does.
    rc_dry, detail_dry = _invoke(home, monkeypatch, 'uninstall', dry_run=True, purge=True)
    assert rc_dry != 0, (name, detail_dry)
    _assert_tree_unchanged(before, home, f'dry-run purge on {name}')
    rc, detail = _invoke(home, monkeypatch, 'uninstall', purge=True)
    assert rc != 0, (name, detail)
    assert expect_reason in detail, (name, detail)
    assert expect_path in detail, (name, detail)
    _assert_tree_unchanged(before, home, f'purge on {name}')
    print(f'MATRIX|purge_atomic|{name}|dry={rc_dry}|real={rc}')


def _craft_interrupted_refresh(home: Path):
    """The on-disk state a refresh leaves when it dies after publishing its
    pending record but before rewriting payload: manifest and payload still
    the old bytes, pending-install.json carrying the previous leaves and the
    previous manifest, exactly as install() writes it."""
    data = _data(home)
    manifest = data / 'install.json'
    record = json.loads(manifest.read_text())
    pending = {
        'owner': MARKER,
        'files': record['files'],
        'previous': {name: base64.b64encode(Path(name).read_bytes()).decode('ascii')
                     for name in record['files']},
        'previous_manifest': base64.b64encode(manifest.read_bytes()).decode('ascii'),
    }
    target = data / 'pending-install.json'
    target.write_text(json.dumps(pending, sort_keys=True))
    target.chmod(0o600)


def _assert_no_owned_left(home: Path):
    """The documented end state: no payload, manifest, pending record, state
    leaves or captures. The live install.lock is the only tolerated residue;
    it is the lock the removal itself was holding."""
    data = _data(home)
    assert not _skill(home).exists(), 'skill dir survived documented removal'
    assert not (data / 'install.json').exists(), 'manifest survived documented removal'
    assert not (data / 'pending-install.json').exists(), 'pending record survived documented removal'
    assert not (data / 'plugin').exists(), 'payload dir survived documented removal'
    assert not (data / 'captures').exists(), 'captures survived documented removal'
    leftover = sorted(p.name for p in data.iterdir()) if data.is_dir() else []
    assert leftover in ([], ['install.lock']), f'owned state survived: {leftover}'


@pytest.mark.parametrize('route', ['uninstall_then_purge', 'purge_direct'])
def test_interrupted_refresh_documented_recovery(tmp_path, monkeypatch, route):
    """devin/README.md documents two routes out of an interrupted refresh: a
    plain uninstall that reconciles the pending record followed by
    --uninstall --purge, or --uninstall --purge alone to discard adapter
    state. Each must end with nothing owned left behind."""
    home = _new_home(tmp_path, monkeypatch, 'home')
    _install_current(home, monkeypatch)
    _craft_interrupted_refresh(home)
    if route == 'uninstall_then_purge':
        rc, detail = _invoke(home, monkeypatch, 'uninstall')
        assert rc == 0, (route, detail)
    rc, detail = _invoke(home, monkeypatch, 'uninstall', purge=True)
    assert rc == 0, (route, detail)
    _assert_no_owned_left(home)
    print(f'MATRIX|interrupted_refresh|{route}|rc={rc}')


def test_uninstall_purge_empty_tree_idempotent(tmp_path, monkeypatch):
    """On a tree with nothing installed, uninstall and purge are clean
    no-ops, and stay no-ops when run again."""
    home = _new_home(tmp_path, monkeypatch, 'home')
    for _ in range(2):
        rc, detail = _invoke(home, monkeypatch, 'uninstall')
        assert rc == 0 and 'No owned Devin install' in detail, (rc, detail)
        rc, detail = _invoke(home, monkeypatch, 'uninstall', purge=True)
        assert rc == 0 and 'No owned Devin install' in detail, (rc, detail)
    print('MATRIX|empty_tree|uninstall+purge|rc=0|twice')


def test_uninstall_purge_after_full_removal_idempotent(tmp_path, monkeypatch):
    """After a real install is fully removed, rerunning the removal commands
    stays a clean no-op and leaves nothing owned behind."""
    home = _new_home(tmp_path, monkeypatch, 'home')
    _install_current(home, monkeypatch)
    for kwargs in ({}, {'purge': True}, {'purge': True}, {}):
        rc, detail = _invoke(home, monkeypatch, 'uninstall', **kwargs)
        assert rc == 0, (kwargs, detail)
    _assert_no_owned_left(home)
    print('MATRIX|post_removal|uninstall+purge|rc=0|twice')


# ---------------------------------------------------------------------------
# Follow-up fix matrix: the commits on top of PR #241 (integ-5.13.43) fixed
# purge after an interrupted refresh, made removal refuse a group/other
# writable ancestor, and taught doctor to name unsafe state honestly.
# ---------------------------------------------------------------------------


def _crash_mid_refresh(home: Path, *, scripts: Path | None = None,
                       patch_render: bool = False):
    """Interrupt a real refresh for real: a subprocess runs install() and
    _atomic is wrapped to kill the process with os._exit(77) after the fifth
    payload-file write, leaving pending-install.json, the previous manifest
    and a half-rewritten payload. `patch_render` makes the renderer append a
    marker to every payload, simulating a checkout change between the
    interrupting refresh and recovery. `scripts` points the subprocess at a
    different scripts dir, so the refresh and recovery run from the same
    modified checkout. Asserts the crash left a pending record plus a
    manifest whose digests no longer match some payload files. Returns the
    names of the files the crash rewrote."""
    render_patch = (
        '_render = inst._render_payload\n'
        "inst._render_payload = lambda name: _render(name) + b'\\n# next version\\n'\n"
        if patch_render else '')
    script = (
        'import os, sys\n'
        f'sys.path.insert(0, {str(scripts or SCRIPTS)!r})\n'
        'import devin_install as inst\n'
        + render_patch +
        '_atomic = inst._atomic\n'
        'count = [0]\n'
        'def crashing_atomic(path, content):\n'
        '    _atomic(path, content)\n'
        "    if '/plugin/' in str(path):\n"
        '        count[0] += 1\n'
        '        if count[0] >= 5:\n'
        '            os._exit(77)\n'
        'inst._atomic = crashing_atomic\n'
        'inst.install()\n'
    )
    run = subprocess.run([sys.executable, '-c', script], env=_env(home),
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 77, (run.returncode, run.stdout, run.stderr)
    data = _data(home)
    pending = data / 'pending-install.json'
    manifest = data / 'install.json'
    assert pending.is_file(), 'crash left no pending-install.json'
    assert manifest.is_file(), 'crash left no install.json'
    record = json.loads(manifest.read_text())
    mismatched = sorted(Path(name).name for name, digest in record['files'].items()
                        if not Path(name).exists()
                        or hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest)
    assert mismatched, 'crash left no manifest/payload digest mismatch'
    return mismatched


def test_interrupted_refresh_purge_dry_run_is_noop(tmp_path, monkeypatch):
    """`uninstall --purge --dry-run` on an interrupted refresh exits 0 and
    changes nothing."""
    home = _new_home(tmp_path, monkeypatch, 'home')
    _install_current(home, monkeypatch)
    _crash_mid_refresh(home, patch_render=True)
    before = _tree_snapshot(home)
    rc, detail = _invoke(home, monkeypatch, 'uninstall', dry_run=True, purge=True)
    assert rc == 0, detail
    assert 'Would remove' in detail
    _assert_tree_unchanged(before, home, 'purge --dry-run on interrupted refresh')
    print('MATRIX|interrupted_refresh|purge_dry|rc=0')


def test_interrupted_refresh_purge_removes_all_owned(tmp_path, monkeypatch):
    """`uninstall --purge` on an interrupted refresh exits 0 and leaves
    nothing owned behind, including the half-written new-version files."""
    home = _new_home(tmp_path, monkeypatch, 'home')
    _install_current(home, monkeypatch)
    _crash_mid_refresh(home, patch_render=True)
    rc, detail = _invoke(home, monkeypatch, 'uninstall', purge=True)
    assert rc == 0, detail
    _assert_no_owned_left(home)
    print('MATRIX|interrupted_refresh|purge|rc=0')


def test_interrupted_refresh_purge_blames_only_user_edit(tmp_path, monkeypatch):
    """Interrupted refresh plus a user edit to a payload file the crash did
    not rewrite plus a user-added file in plugin/: purge refuses, names the
    user-edited file (not a refresh-written one), and removes nothing."""
    home = _new_home(tmp_path, monkeypatch, 'home')
    _install_current(home, monkeypatch)
    rewritten = _crash_mid_refresh(home, patch_render=True)
    data = _data(home)
    edited = data / 'plugin' / 'bash_whitelist.py'
    assert edited.name not in rewritten, 'test needs a file the crash did not rewrite'
    edited.write_bytes(edited.read_bytes() + b'# user edit\n')
    stray = data / 'plugin' / 'user-scratch.txt'
    stray.write_text('keep me\n')
    before = _tree_snapshot(home)
    rc, detail = _invoke(home, monkeypatch, 'uninstall', purge=True)
    assert rc != 0, detail
    assert str(edited) in detail, detail
    # Refresh-written files are adapter output, not user edits; the refusal
    # must not blame them.
    assert 'devin_compress.py' not in detail, detail
    assert 'devin_state.py' not in detail, detail
    _assert_tree_unchanged(before, home, 'purge refusal on edited interrupted refresh')
    print(f'MATRIX|interrupted_refresh|purge_user_edit|rc={rc}')


def test_interrupted_refresh_changed_checkout_refuses(tmp_path, monkeypatch):
    """A pending refresh whose committed digests differ from this checkout is
    ambiguous: ordinary install and ordinary uninstall both refuse, change
    nothing, name pending-install.json and point at uninstall --purge, which
    then finishes the removal. Recovery across changed checkout content is
    not guaranteed by the documented contract."""
    home = _new_home(tmp_path, monkeypatch, 'home')
    _install_current(home, monkeypatch)
    _crash_mid_refresh(home, patch_render=True)
    before = _tree_snapshot(home)
    pending = str(_data(home) / 'pending-install.json')
    for action in ('install', 'uninstall'):
        rc, detail = _invoke(home, monkeypatch, action)
        assert rc != 0, (action, detail)
        assert pending in detail, (action, detail)
        assert 'uninstall --purge' in detail, (action, detail)
        _assert_tree_unchanged(before, home,
                               f'{action} on version-ambiguous interrupted refresh')
    rc, detail = _invoke(home, monkeypatch, 'uninstall', purge=True)
    assert rc == 0, detail
    _assert_no_owned_left(home)
    print('MATRIX|interrupted_refresh|changed_checkout|refused_then_purged')


def _scratch_checkout(tmp_path: Path, modify: str) -> Path:
    """A second copy of the installer checkout under tmp_path: the scripts
    dir plus devin/SKILL.md at the layout install() expects, with a trailing
    comment appended to one payload source so the refresh it runs really
    changes bytes. Returns the copied devin_install.py path; a command run
    from it renders the same committed digests every time, so recovery from
    this copy is the same-checkout case the pending record was written for.
    `modify` must be a file early enough in PAYLOAD that the crash writes
    it."""
    scratch = tmp_path / 'scratch'
    scripts = scratch / 'skills' / 'token-optimizer' / 'scripts'
    shutil.copytree(SCRIPTS, scripts)
    (scratch / 'devin').mkdir(parents=True)
    shutil.copy2(SCRIPTS.parents[2] / 'devin' / 'SKILL.md', scratch / 'devin' / 'SKILL.md')
    target = scripts / modify
    target.write_bytes(target.read_bytes() + b'\n# next version\n')
    return scripts / 'devin_install.py'


def _run_installer(home: Path, installer: Path, *argv):
    """Run a devin_install.py at an arbitrary path the way install.sh
    dispatches it: python -I <installer> <action> [flags] in an isolated
    HOME."""
    run = subprocess.run([sys.executable, '-I', str(installer), *argv],
                         env=_env(home), capture_output=True, text=True, timeout=60)
    return run.returncode, (run.stdout + run.stderr).strip()


def test_interrupted_refresh_same_checkout_recovers(tmp_path, monkeypatch):
    """When recovery runs from the same checkout the interrupted refresh ran
    from, the pending record's fingerprinted previous bytes are sufficient:
    ordinary install restores them and finishes into a verified install;
    ordinary uninstall restores them and removes the install."""
    installer = _scratch_checkout(tmp_path, 'devin_state.py')
    inst_home = _new_home(tmp_path, monkeypatch, 'inst')
    un_home = _new_home(tmp_path, monkeypatch, 'un')
    for home in (inst_home, un_home):
        _install_current(home, monkeypatch)
        _crash_mid_refresh(home, scripts=installer.parent)

    rc, detail = _run_installer(inst_home, installer, 'install')
    assert rc == 0, detail
    import devin_verify as verify
    monkeypatch.setenv('HOME', str(inst_home))
    assert verify.verify_install() is True

    rc, detail = _run_installer(un_home, installer, 'uninstall')
    assert rc == 0, detail
    data = _data(un_home)
    assert not (data / 'install.json').exists()
    assert not (data / 'pending-install.json').exists()
    assert not (data / 'plugin').exists()
    assert not _skill(un_home).exists()
    # Plain uninstall keeps numeric state by design; the documented purge
    # finishes the removal and nothing owned may survive it.
    rc, detail = _run_installer(un_home, installer, 'uninstall', '--purge')
    assert rc == 0, detail
    _assert_no_owned_left(un_home)
    print('MATRIX|interrupted_refresh|same_checkout|install+uninstall recovered')


def test_writable_ancestor_refuses_all_removal_commands(tmp_path, monkeypatch):
    """A group/other-writable ancestor (.config at 0775) makes every removal
    spelling refuse: uninstall dry-run, uninstall, purge dry-run and purge.
    Same reason, names the directory, says chmod go-w, changes nothing."""
    home = _new_home(tmp_path, monkeypatch, 'home')
    _install_current(home, monkeypatch)
    config = home / '.config'
    config.chmod(0o775)
    try:
        before = _tree_snapshot(home)
        for label, kwargs in (('uninstall_dry', {'dry_run': True}),
                              ('uninstall', {}),
                              ('purge_dry', {'dry_run': True, 'purge': True}),
                              ('purge', {'purge': True})):
            rc, detail = _invoke(home, monkeypatch, 'uninstall', **kwargs)
            assert rc != 0, (label, detail)
            assert 'writable ancestor' in detail, (label, detail)
            assert str(config) in detail, (label, detail)
            assert 'chmod go-w' in detail, (label, detail)
        _assert_tree_unchanged(before, home, 'removal commands under writable ancestor')
        print('MATRIX|writable_ancestor|uninstall+purge x2|refused')
    finally:
        config.chmod(0o700)


def test_doctor_names_loose_data_dir_not_missing_consent(tmp_path, monkeypatch):
    """A 0755 data dir makes diagnose() report installed False with a
    readiness_error naming the data dir and owner-only, not the misleading
    'missing local data consent' the old code produced."""
    home = _new_home(tmp_path, monkeypatch, 'home')
    _install_current(home, monkeypatch)
    data = _data(home)
    data.chmod(0o755)
    import devin_doctor as doctor
    monkeypatch.setenv('HOME', str(home))
    result = doctor.diagnose()
    assert result['installed'] is False
    error = result['readiness_error'] or ''
    assert str(data) in error, error
    assert 'owner-only' in error, error
    assert 'missing local data consent' not in error, error
    print('MATRIX|doctor|loose_data_dir|installed=False')


def test_doctor_warns_install_lock_will_refuse(tmp_path, monkeypatch):
    """A 0644 install.lock leaves the install valid but warns that lifecycle
    commands will refuse, naming the lock path."""
    home = _new_home(tmp_path, monkeypatch, 'home')
    _install_current(home, monkeypatch)
    lock = _data(home) / 'install.lock'
    lock.chmod(0o644)
    import devin_doctor as doctor
    monkeypatch.setenv('HOME', str(home))
    result = doctor.diagnose()
    assert result['installed'] is True, result
    lock_warnings = [w for w in result['warnings'] if 'will refuse' in w]
    assert lock_warnings, result['warnings']
    assert any(str(lock) in w for w in lock_warnings), lock_warnings
    print('MATRIX|doctor|loose_lock|installed=True, warned')
