#!/usr/bin/env python3
"""Install the Devin output skill; with --auto, also own two hook entries in the Devin user config."""
from __future__ import annotations
import argparse
import ast
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import shutil
import sys
import tempfile

SCRIPTS = Path(__file__).resolve().parent
# Isolated startup (-I) removes the script directory; the installer always
# runs from a checkout, so restore only this checked scripts root.
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import devin_state as state
from devin_verify import PAYLOAD, payload_key_shapes
from py_trust import py_path_is_trusted
import devin_hooks as hooks
SKILL_NAME = 'token-optimizer-output'
MARKER = 'devin-output-filter-v1'


def _render_payload(name: str) -> bytes:
    """Commit installed bytes, with a direct-execution gate on internal modules."""
    source = (SCRIPTS / name).read_bytes()
    if name in ('devin_compress.py', 'devin_doctor.py'):
        return source
    tree = ast.parse(source)
    prefix_end = 0
    prefix_count = 0
    for index, node in enumerate(tree.body):
        docstring = (index == 0 and isinstance(node, ast.Expr)
                     and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str))
        future = isinstance(node, ast.ImportFrom) and node.module == '__future__'
        if not (docstring or future):
            break
        prefix_end = node.end_lineno
        prefix_count += 1
    if any(node.lineno <= prefix_end for node in tree.body[prefix_count:]):
        raise OSError('payload prefix shares a line with executable code')
    lines = source.splitlines(keepends=True)
    guard = (b"\n# Installed Devin payload: internal modules have no CLI contract.\n"
             b"if __name__ == '__main__':\n"
             b"    import sys\n"
             b"    print('Token Optimizer: internal module requires an activated entrypoint. Originals remain unchanged.', file=sys.stderr)\n"
             b"    raise SystemExit(2)\n\n")
    result = b''.join(lines[:prefix_end]) + guard + b''.join(lines[prefix_end:])
    compile(result, name, 'exec')
    return result


def skill_dir() -> Path:
    return state.home() / 'skills' / SKILL_NAME


def manifest_path() -> Path:
    return state.data_dir() / 'install.json'


def _private_bytes(path: Path) -> bytes:
    state.no_symlinks(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        # fstat before fdopen: a directory wraps into a stream that reports the
        # bare descriptor number in its error, not the path being refused.
        before=os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid!=os.geteuid()
                or before.st_mode&0o077 or before.st_nlink!=1):
            raise OSError('private single-link ownership file required: ' + str(path))
        with os.fdopen(fd,'rb') as handle:
            fd=-1
            content=handle.read()
            after=os.fstat(handle.fileno())
    finally:
        if fd>=0:
            os.close(fd)
    if (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):
        raise OSError('ownership file changed while reading')
    return content


def _hash(path: Path) -> str:
    try:return hashlib.sha256(_private_bytes(path)).hexdigest()
    except FileNotFoundError:
        raise OSError('owned file is missing: ' + str(path) + '; uninstall then install to repair while preserving original captures')


def _clear_pending(path: Path) -> None:
    path.unlink(missing_ok=True)
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


class _ForeignLayout(OSError):
    """Recorded keys do not map to this version's expected install paths."""


class _EditedInstall(OSError):
    """A recorded file's digest no longer matches the manifest."""


def _resolved_keys(files) -> list:
    """Recorded manifest keys may carry a non-canonical home spelling (for
    example '..' segments) written before the override was resolved; compare
    them by where they resolve."""
    return sorted(str(Path(name).resolve()) for name in files)


def _resolved_files(files) -> dict:
    return {str(Path(name).resolve()): digest for name, digest in files.items()}


def _ownership(*, allow_missing=False) -> dict | None:
    p = state.private_path(manifest_path())
    if not p.exists():
        return None
    if p.exists() and p.stat().st_nlink != 1:raise OSError('ownership record hardlink refused')
    try:
        record = json.loads(_private_bytes(p))
    except (ValueError, RecursionError):
        raise OSError('invalid ownership record; run uninstall --purge (which also removes stored captures) to discard adapter state')
    if not isinstance(record, dict) or record.get('owner') != MARKER or not isinstance(record.get('files'), dict):
        raise OSError('invalid ownership record; run uninstall --purge (which also removes stored captures) to discard adapter state')
    _, shapes = payload_key_shapes(state.data_dir() / 'plugin', PAYLOAD, skill_dir() / 'SKILL.md')
    try:
        record_keys = _resolved_keys(record['files'])
    except (OSError, ValueError, TypeError, RuntimeError):
        raise OSError('recorded paths cannot be resolved; run uninstall --purge (which also removes stored captures) to discard adapter state')
    if record_keys not in shapes:
        # A recorded path routed through a symlink resolves elsewhere; name the
        # real cause before falling through to the version/spelling advice.
        for name in record['files']:
            state.no_symlinks(Path(name))
        raise _ForeignLayout('ownership paths do not match this install; the manifest was recorded by a different adapter version or home spelling -- run uninstall --purge (which also removes stored captures) to discard adapter state')
    for name, digest in record['files'].items():
        raw = Path(name)
        resolved = raw.resolve()
        # A key like plugin/ghost/../x.py resolves onto an owned path yet
        # cannot be traversed; refusing here beats a "successful" removal that
        # silently leaves the file behind. An ordinary missing file or moved
        # folder resolves to nothing and stays tolerable under allow_missing.
        if resolved.exists() and not raw.exists():
            raise OSError('recorded path cannot be reached inside the install directories: ' + name +
                          '; move ' + str(state.data_dir()) + ' and ' + str(skill_dir()) + ' aside to repair')
        if allow_missing and not raw.exists():
            state.no_symlinks(raw)
            continue
        if _hash(raw) != digest:
            raise _EditedInstall('installed file changed; refusing overwrite/removal: ' + name +
                                 '; to remove the adapter anyway move ' + str(state.data_dir()) +
                                 ' and ' + str(skill_dir()) + ' aside first')
    return record


def _ownership_for_removal(*, purge: bool):
    """Purge tolerates a manifest this version cannot map or read. A digest
    mismatch still refuses, exactly like a plain uninstall, unless every
    mismatching file holds bytes the adapter itself wrote (an interrupted
    refresh): those are not user edits, and the purge sweep removes them."""
    try:
        return _ownership(allow_missing=True)
    except _EditedInstall:
        if not purge:
            raise
        edited = _user_edited_file()
        if edited is None:
            return None
        if edited:
            # Name the file the user changed, not an interrupted-refresh leaf.
            raise _EditedInstall('installed file changed; refusing overwrite/removal: ' + edited +
                                 '; to remove the adapter anyway move ' + str(state.data_dir()) +
                                 ' and ' + str(skill_dir()) + ' aside first') from None
        raise
    except (OSError, ValueError, RecursionError):
        if not purge:
            raise
        return None



def _purge_record_bytes(path):
    """Purge may recover historical digests from loose-mode or linked records."""
    state.no_symlinks(path)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 8 * 1024 * 1024:
            raise OSError('bounded regular purge record required: ' + str(path))
        with os.fdopen(fd, 'rb') as handle:
            fd = -1
            content = handle.read(8 * 1024 * 1024 + 1)
        if len(content) > 8 * 1024 * 1024:
            raise OSError('purge record too large: ' + str(path))
        return content
    finally:
        if fd >= 0:
            os.close(fd)


def _user_edited_file():
    """None when every installed file that differs from the manifest carries a
    digest a transaction record or this checkout produced (an interrupted
    refresh, not an edit). Otherwise the first file with an unknown digest, or
    '' when that cannot be determined; either keeps the refusal."""
    try:
        record = json.loads(_purge_record_bytes(manifest_path()))
        files = record['files'] if isinstance(record, dict) else None
        if not isinstance(files, dict) or not files:
            return ''
        recorded, known = _purge_recorded(expected_only=True), _known_payload_digests()
        for name, digest in files.items():
            path = Path(name)
            state.no_symlinks(path)
            if not path.exists():
                continue
            actual = hashlib.sha256(_private_bytes(path)).hexdigest()
            resolved = str(path.resolve())
            if actual != digest and actual not in recorded.get(resolved, ()) and actual != known.get(resolved):
                return name
        return None
    except (OSError, ValueError, TypeError, RuntimeError, RecursionError):
        return ''


def _purge_recorded(*, expected_only=False) -> dict:
    """Best-effort recorded digests for the purge sweep, as path -> set of
    digests: a well-formed manifest or pending transaction record contributes
    every digest it committed (the intended new bytes and the previous ones);
    anything unreadable yields nothing."""
    out = {}

    # Lifting the edited-file refusal is limited to this version's own payload
    # paths; the sweep also honours another version's recorded file names.
    expected = ({str((state.data_dir() / 'plugin' / name).resolve()) for name in PAYLOAD}
                | {str((skill_dir() / 'SKILL.md').resolve())})

    def _put(name, digest):
        resolved = str(Path(name).resolve())
        if not expected_only or resolved in expected:
            out.setdefault(resolved, set()).add(digest)

    def _add(files):
        if not isinstance(files, dict):
            return
        for name, digest in files.items():
            if (not isinstance(name, str) or not isinstance(digest, str) or len(digest) != 64
                    or any(c not in '0123456789abcdef' for c in digest)):
                continue
            try:
                _put(name, digest)
            except (OSError, ValueError, RuntimeError):
                continue

    for leaf in ('install.json', 'pending-install.json'):
        try:
            record = json.loads(_purge_record_bytes(state.data_dir() / leaf))
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(record, dict) or record.get('owner') != MARKER:
            continue
        _add(record.get('files'))
        previous = record.get('previous')
        if isinstance(previous, dict):
            for name, content in previous.items():
                if not isinstance(name, str) or not isinstance(content, str):
                    continue
                try:
                    _put(name, hashlib.sha256(base64.b64decode(content, validate=True)).hexdigest())
                except (OSError, ValueError, RuntimeError):
                    continue
        old_manifest = record.get('previous_manifest')
        if isinstance(old_manifest, str):
            try:
                old = json.loads(base64.b64decode(old_manifest, validate=True))
            except (ValueError, RecursionError):
                old = None
            if isinstance(old, dict):
                _add(old.get('files'))
    return out


def _sweep_candidates(recorded, known) -> list:
    """Files the purge sweep removes: regular files directly inside plugin/
    and the skill folder whose bytes a record or this checkout produced.
    Unreadable or foreign content yields nothing and is preserved."""
    found = []
    for folder in (state.data_dir() / 'plugin', skill_dir()):
        try:
            if folder.is_symlink() or not folder.is_dir():
                continue
            state.no_symlinks(folder)
            items = list(folder.iterdir())
        except OSError:
            continue
        for item in items:
            try:
                if not item.is_file() or item.is_symlink():
                    continue
                digest = hashlib.sha256(item.read_bytes()).hexdigest()
                resolved = str(item.resolve())
            except OSError:
                continue
            if digest in recorded.get(resolved, ()) or known.get(resolved) == digest:
                found.append(item)
    return found


def _preflight_sweep_access(recorded, known) -> None:
    """Refuse before any state is purged when unchanged adapter files sit in
    a folder that cannot be modified: purging the records first would strand
    those files with nothing left to identify them."""
    for folder in sorted({item.parent for item in _sweep_candidates(recorded, known)}):
        if not os.access(folder, os.W_OK | os.X_OK, effective_ids=True):
            raise OSError('removal directory is not writable/searchable: ' + str(folder) +
                          '; it holds unchanged adapter files. Make it writable (chmod u+wx) and retry removal')


def _known_payload_digests() -> dict:
    """Digests of this tree's rendered payload: unchanged installed bytes are
    safe for purge to remove even when the manifest cannot be read."""
    try:
        known = {str((state.data_dir() / 'plugin' / name).resolve()):
                 hashlib.sha256(_render_payload(name)).hexdigest() for name in PAYLOAD}
        source = SCRIPTS.parents[2] / 'devin' / 'SKILL.md'
        if source.is_file():
            text = source.read_text().replace('@PYTHON@', shlex.quote(os.path.realpath(sys.executable)))
            text = text.replace('@FILTER@', shlex.quote(str(state.data_dir() / 'plugin' / 'devin_compress.py')))
            known[str((skill_dir() / 'SKILL.md').resolve())] = hashlib.sha256(text.encode()).hexdigest()
        return known
    except OSError:
        return {}


@contextmanager
def _lock():
    import fcntl
    state.private_path(state.data_dir(), directory=True, create=True)
    lock = state.private_path(state.data_dir() / 'install.lock', create=True)
    with lock.open('r+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _atomic(path: Path, content: bytes) -> None:
    state.no_symlinks(path)
    # Unique private temp avoids a stale O_EXCL name wedging future attempts.
    # A killed pre-publication temp is retained, never claimed/deleted as owned.
    fd, temporary_name = tempfile.mkstemp(prefix='.install-',dir=state.data_dir())
    temporary=Path(temporary_name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(content); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


# ── Automatic mode: hook entries in the Devin user config ───────────────────
# Devin reads global hooks from the "hooks" key of its user config. The adapter
# owns exactly the entries it recorded; every other key and hook is the
# user's and survives install, refresh, uninstall and purge byte for byte.

HOOK_STATE_LEAVES = (hooks.RECORD, hooks.BINDINGS, hooks.BINDINGS + '.lock', 'auto-captures.json',
                     'auto-captures.json.lock', 'devin-config-backup.json')
MAX_CONFIG = 4 * 1024 * 1024
ATTESTATION_NOTICE = ('Automatic mode records your statement that no Devin team or enterprise permission policy '
                      'applies here: those policies are never stored on this machine, so they cannot be checked.')


def devin_config() -> Path:
    return state.home() / 'config.json'


def _hook_entries() -> dict:
    return hooks.owned_entries(os.path.realpath(sys.executable), str(state.data_dir() / 'plugin' / 'devin_compress.py'))


def _hook_record(*, tolerant=False):
    """The recorded hook entries, or None. tolerant: unreadable = None (purge)."""
    path = state.data_dir() / hooks.RECORD
    try:
        state.private_path(path)
        if not path.exists():
            return None
        record = json.loads(_private_bytes(path))
        if (not isinstance(record, dict) or record.get('owner') != MARKER
                or not isinstance(record.get('entries'), dict) or not isinstance(record.get('stale', []), list)):
            raise OSError('invalid hook record: ' + str(path))
        return record
    except (OSError, ValueError, RecursionError) as exc:
        if tolerant:
            return None
        raise OSError('invalid hook record: ' + str(path) + ' (' + str(exc) + ')') from None


def _read_devin_config(path: Path):
    """(config, exact bytes or None when absent, mode). Refuses what it cannot edit safely."""
    state.no_symlinks(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}, None, 0o600
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise OSError('Devin config must be a regular, single-link file you own: ' + str(path))
    if info.st_size > MAX_CONFIG:
        raise OSError('Devin config is too large to edit safely: ' + str(path))
    raw = path.read_bytes()
    try:
        config = json.loads(raw)
    except (ValueError, RecursionError):
        raise OSError('Devin config is not plain JSON (comments or trailing commas?): ' + str(path)) from None
    if not isinstance(config, dict):
        raise OSError('Devin config must be a JSON object: ' + str(path))
    return config, raw, stat.S_IMODE(info.st_mode)


def _write_devin_config(path: Path, config: dict, raw, mode: int) -> None:
    """Atomic, mode-preserving replace; refuses when the file changed meanwhile."""
    if raw is not None:
        # The exact bytes before this edit, private, for manual recovery.
        backup = state.data_dir() / 'devin-config-backup.json'
        state.private_path(backup)
        _atomic(backup, raw)
    indent = 2
    if raw:
        match = re.search(rb'\n( +)"', raw)
        if match:
            indent = len(match.group(1))
    content = (json.dumps(config, indent=indent, ensure_ascii=False) + '\n').encode('utf-8')
    fd, temporary_name = tempfile.mkstemp(prefix='.config-token-optimizer-', dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(content); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = None
        if current != raw:
            raise OSError('Devin config changed while it was being edited; nothing was written. Retry: ' + str(path))
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _recorded_pairs(record) -> list:
    """Every (event, entry) a record may have written: current plus stale."""
    pairs = [(event, entry) for event, entry in record['entries'].items()]
    for item in record.get('stale', []):
        if isinstance(item, list) and len(item) == 2 and isinstance(item[0], str):
            pairs.append((item[0], item[1]))
    return pairs


def _wire_hooks() -> None:
    """Add (or refresh) the two owned entries. The record is written first and
    names the previous entries as stale, so an interrupted run never leaves an
    entry that a later uninstall cannot identify."""
    path = devin_config()
    previous = _hook_record()
    config, raw, mode = _read_devin_config(path)
    entries = _hook_entries()
    stale = [[event, entry] for event, entry in (_recorded_pairs(previous) if previous else [])
             if entries.get(event) != entry]
    merged = config
    for event, entry in stale:
        merged, _ = hooks.remove_hooks(merged, {event: entry})
    merged = hooks.merge_hooks(merged, entries)
    record = {'owner': MARKER, 'config': str(path), 'entries': entries, 'stale': stale,
              'org_policy': hooks.ATTESTATION}
    target = state.private_path(state.data_dir() / hooks.RECORD)
    _atomic(target, json.dumps(record, sort_keys=True).encode())
    if merged != config or raw is None:
        _write_devin_config(path, merged, raw, mode)
    if stale:
        record['stale'] = []
        _atomic(target, json.dumps(record, sort_keys=True).encode())


def _unwire_hooks(*, tolerant=False) -> bool:
    """Remove only the recorded, byte-equal entries, then the record. True when
    a record existed. Edited entries stay, and are named."""
    record = _hook_record(tolerant=tolerant)
    if record is None:
        return False
    path = devin_config()
    config, raw, mode = _read_devin_config(path)
    if raw is not None:
        result = config
        for event, entry in _recorded_pairs(record):
            result, _ = hooks.remove_hooks(result, {event: entry})
        preserved = [event for event, found in hooks.installed_state(result, record['entries']).items()
                     if found == 'edited']
        if result != config:
            _write_devin_config(path, result, raw, mode)
        if preserved:
            print('Kept edited Token Optimizer hook entries in ' + str(path) + ' (' + ', '.join(preserved) +
                  '); they no longer match what was installed. Remove them by hand if unwanted.')
    (state.data_dir() / hooks.RECORD).unlink(missing_ok=True)
    # The backup is a full copy of the user's config: never outlive the wiring.
    (state.data_dir() / 'devin-config-backup.json').unlink(missing_ok=True)
    return True


def _recover_pending() -> None:
    """Resume only a fingerprinted fresh-install transaction, never foreign files."""
    pending=state.private_path(state.data_dir()/'pending-install.json')
    if not pending.exists():return
    if pending.stat().st_nlink!=1:raise OSError('pending ownership hardlink refused: ' + str(pending))
    try:
        value=json.loads(_private_bytes(pending))
    except (ValueError,RecursionError):
        value=None
    expected,shapes=payload_key_shapes(state.data_dir()/'plugin',PAYLOAD,skill_dir()/'SKILL.md')
    files=value.get('files') if isinstance(value,dict) else None
    try:
        keys=_resolved_keys(files) if isinstance(files,dict) else None
    except (OSError,ValueError,TypeError,RuntimeError):
        keys=None
    if (not isinstance(value,dict) or value.get('owner')!=MARKER
            or keys not in shapes):
        raise OSError('invalid pending install record: ' + str(pending) + '; preserved; run uninstall --purge (which also removes stored captures) to discard adapter state')
    # Reconcile published new bytes first: stale pending must not undo a completed update.
    if manifest_path().exists():
        try:
            published=_ownership()
        except OSError:
            published=None
        if published and _resolved_files(published['files'])==_resolved_files(value['files']):
            _clear_pending(pending);return
    source=SCRIPTS.parents[2]/'devin/SKILL.md'
    if not source.is_file():raise OSError('pending repair requires original checkout')
    rendered=source.read_text().replace('@PYTHON@',shlex.quote(os.path.realpath(sys.executable))).replace('@FILTER@',shlex.quote(str(state.data_dir()/'plugin/devin_compress.py')))
    current={str(state.data_dir()/'plugin'/name):hashlib.sha256(_render_payload(name)).hexdigest() for name in PAYLOAD}
    current[str(skill_dir()/'SKILL.md')]=hashlib.sha256(rendered.encode()).hexdigest()
    # A previous-version record cannot match this tree's rendered digests; it
    # reconciles against the digests it committed, per file, in the loop below.
    if keys==sorted(expected) and _resolved_files(value['files'])!=current:
        raise OSError('pending version/content ambiguous: ' + str(pending) + '; preserved; run uninstall --purge (which also removes stored captures) to discard adapter state, or follow the documented manual repair')
    old=value.get('previous')
    if old is not None:
        if not isinstance(old,dict) or _resolved_keys(old) not in shapes:raise OSError('invalid refresh recovery record')
        if any(not isinstance(content,str) for content in old.values()) or not isinstance(value.get('previous_manifest'),str):
            raise OSError('invalid refresh recovery encoding')
        previous={name:base64.b64decode(content,validate=True) for name,content in old.items()}
        previous_manifest=base64.b64decode(value['previous_manifest'],validate=True)
        previous_record=json.loads(previous_manifest)
        if (not isinstance(previous_record,dict) or previous_record.get('owner')!=MARKER
                or not isinstance(previous_record.get('files'),dict)
                or set(previous_record['files'])!=set(previous)
                or any(not isinstance(digest,str) or len(digest)!=64
                       or any(ch not in '0123456789abcdef' for ch in digest)
                       for digest in previous_record['files'].values())):
            raise OSError('invalid previous ownership commitments; preserved')
        if _private_bytes(manifest_path())!=previous_manifest:
            raise OSError('previous ownership differs from preserved manifest; preserved')
        if any(hashlib.sha256(content).hexdigest()!=previous_record['files'][name]
               for name,content in previous.items()):
            raise OSError('previous rollback source differs from ownership commitments; preserved')
        for name,content in previous.items():
            path=Path(name);state.no_symlinks(path)
            if path.exists() and _hash(path) not in (hashlib.sha256(content).hexdigest(),value['files'][name]):
                raise OSError('edited interrupted refresh; preserved')
            temporary=path.with_name(path.name+'.install-tmp')
            if temporary.exists():
                raise OSError('pre-existing refresh temporary ownership ambiguous; preserved; use manual repair')
        added=set(value['files']) - set(previous)
        for name in added:
            path=Path(name);state.no_symlinks(path)
            if path.exists() and _hash(path)!=value['files'][name]:
                raise OSError('edited newly added refresh leaf; preserved')
        for name in added:
            Path(name).unlink(missing_ok=True)
        for name,content in previous.items():
            path=Path(name)
            _atomic(path,content)
        _atomic(manifest_path(),previous_manifest)
        _clear_pending(pending);return
    # If the fresh install completed publication, validate before clearing pending.
    if manifest_path().exists():
        _ownership();_clear_pending(pending);return
    removable=[]
    recorded=_resolved_files(value['files'])
    for folder in (skill_dir(),state.data_dir()/'plugin'):
        state.no_symlinks(folder)
        if not folder.exists():continue
        for item in folder.iterdir():
            key=str(item)
            original=key.removesuffix('.install-tmp')
            if key.endswith('.install-tmp'):
                raise OSError('pre-existing temporary ownership ambiguous; preserved; use manual repair')
            if original not in expected or not item.is_file() or item.is_symlink():
                raise OSError('foreign content in interrupted install; preserved')
            # Partial/foreign temps are never erased: exact intended bytes must match.
            info=item.stat()
            if info.st_uid!=os.geteuid() or info.st_mode&0o077 or info.st_nlink!=1:
                raise OSError('unsafe interrupted install content; preserved')
            if _hash(item)!=recorded.get(original,''):
                raise OSError('edited interrupted install content; preserved')
            removable.append(item)
    for item in removable:item.unlink()
    for folder in (skill_dir(),state.data_dir()/'plugin'):
        try:folder.rmdir()
        except FileNotFoundError:pass
    _clear_pending(pending)


def _preflight_state_files(root):
    for leaf in ('install.lock', 'consent.json', 'pending-install.json'):
        state.private_path(root / leaf)


def _numeric_state_warnings(root) -> list:
    """Numeric estimates are optional: the filter works without them, so an
    unsafe metrics leaf is reported, never a reason to refuse the skill."""
    found = []
    for leaf in state.NUMERIC_STATE_LEAVES:
        try:
            state.private_path(root / leaf)
        except OSError as exc:
            found.append('Warning: numeric estimates will not be recorded: ' + str(exc))
    return found


def _preflight_ancestors(root, skill):
    for destination in (root, skill):
        for ancestor in reversed(destination.parents):
            if not ancestor.exists():
                continue
            info = ancestor.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise OSError('install ancestor is not a directory: ' + str(ancestor))
            if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
                raise OSError('group/other-writable ancestor directory refused: ' + str(ancestor) + ' (chmod go-w it, or set its sticky bit)')



def _preflight_write_access(root, skill):
    """Check effective access without creating leaves; ACL inheritance may differ."""
    for destination in (root, root / 'plugin', skill):
        cursor = destination
        while not cursor.exists():
            cursor = cursor.parent
        mode = os.W_OK | os.X_OK
        if cursor == destination:
            mode |= os.R_OK
        if not os.access(cursor, mode, effective_ids=True):
            raise OSError('install destination is not readable/writable/searchable: ' + str(cursor))
        for ancestor in cursor.parents:
            if not os.access(ancestor, os.X_OK, effective_ids=True):
                raise OSError('install ancestor is not searchable: ' + str(ancestor))
    lock = root / 'install.lock'
    if lock.exists() and not os.access(lock, os.R_OK | os.W_OK, effective_ids=True):
        raise OSError('install lock is not readable/writable: ' + str(lock))


def _preflight_file_flags(names) -> None:
    """An immutable or append-only file cannot be replaced or removed. Only
    platforms that expose file flags (macOS, BSD) are checked here."""
    blocked = getattr(stat, 'UF_IMMUTABLE', 0) | getattr(stat, 'SF_IMMUTABLE', 0) | \
        getattr(stat, 'UF_APPEND', 0) | getattr(stat, 'SF_APPEND', 0)
    for name in names:
        try:
            flags = getattr(os.lstat(name), 'st_flags', 0)
        except OSError:
            continue
        if flags & blocked:
            raise OSError('owned file is immutable and cannot be replaced or removed: ' + str(name) +
                          '; clear its flag (chflags nouchg) and retry')


def _preflight_removal_access(root, skill, owned, *, purge=False):
    # Without an ownership record nothing in plugin/ or the skill folder is
    # removed by name: the purge sweep skips what it cannot change and reports
    # it as preserved, so foreign folders are never a reason to refuse.
    destinations = [root]
    if owned:
        destinations.extend(Path(name).parent for name in owned['files'])
        # The emptied skill folder is removed from its parent.
        if skill.exists():
            destinations.append(skill.parent)
    for folder in set(destinations):
        if folder.exists() and not os.access(folder, os.W_OK | os.X_OK, effective_ids=True):
            raise OSError('removal directory is not writable/searchable: ' + str(folder) + '; make it writable (chmod u+wx) and retry removal')
    if owned:
        _preflight_file_flags(owned['files'])
    cache = root / 'plugin' / '__pycache__'
    if not owned and (purge and (root / 'plugin').is_symlink()
                      or not os.access(root / 'plugin', os.W_OK | os.X_OK, effective_ids=True)):
        cache = None  # Foreign symlink targets and folders are never inspected or removed.
    if cache is not None:
        state.no_symlinks(cache)
    if cache is not None and cache.exists():
        if not cache.is_dir():
            raise OSError('removal cache is not a directory; preserved: ' + str(cache))
        if not os.access(cache, os.R_OK | os.W_OK | os.X_OK, effective_ids=True):
            raise OSError('removal cache is not readable/writable/searchable: ' + str(cache))
    lock = root / 'install.lock'
    if lock.exists() and not os.access(lock, os.R_OK | os.W_OK, effective_ids=True):
        raise OSError('install lock is not readable/writable: ' + str(lock))


def _has_content(folder) -> bool:
    """An empty directory left by an earlier removal is not foreign content."""
    if not folder.exists() and not folder.is_symlink():
        return False
    if folder.is_symlink() or not folder.is_dir():
        return True
    return any(entry.name != '.DS_Store' for entry in folder.iterdir())


def _preflight_destination(root, skill):
    _preflight_ancestors(root, skill)
    _preflight_state_files(root)
    state.private_path(root, directory=True)
    state.private_path(root / 'plugin', directory=True)
    owned = _ownership()
    existing = [] if owned else [str(folder) for folder in (skill, root / 'plugin') if _has_content(folder)]
    if existing:
        raise OSError('foreign install destination already exists: ' + ', '.join(existing) + '; move aside or remove and retry')
    state.private_path(skill, directory=True)
    if owned:
        _preflight_file_flags(owned['files'])
        actual = {str(p) for folder in (skill, root / 'plugin') for p in folder.rglob('*')
                  if p.is_file() and '__pycache__' not in p.parts and p.name != '.DS_Store'}
        recorded = set(_resolved_files(owned['files']))
        if actual != recorded:
            strays, missing = sorted(actual - recorded), sorted(recorded - actual)
            if strays:
                raise OSError('unowned files in install destination: ' + ', '.join(strays) + '; move aside or remove and retry')
            raise OSError('owned files are missing from the install destination: ' + ', '.join(missing) + '; run uninstall, then install')
    return owned


def install(*, dry_run=False, mode=None) -> None:
    """mode: 'auto' wires the hooks, 'manual' unwires them, None keeps the current mode."""
    if os.name != 'posix':
        raise OSError('native Windows unsupported; use a POSIX environment')
    root, skill = state.data_dir(), skill_dir()
    state.no_symlinks(root); state.no_symlinks(skill)
    # Stable trusted interpreter, never a bare PATH command in an installed skill.
    if not py_path_is_trusted(sys.executable):
        raise OSError('no trusted Python interpreter')
    source = SCRIPTS.parents[2] / 'devin' / 'SKILL.md'
    if not source.is_file():
        raise OSError('run install from a Token Optimizer checkout')
    for name in PAYLOAD:
        if not (SCRIPTS / name).is_file():
            raise OSError('missing payload: ' + name)
    if state.loader_path_info()['status'] != 'documented_default':
        print('Warning: nondefault Devin home; Desktop discovery is unverified. Check the loaded skill in Desktop Customizations; this installer does not configure its loader.')
    _preflight_ancestors(root, skill)
    _preflight_state_files(root)
    for warning in _numeric_state_warnings(root):
        print(warning)
    if dry_run:
        if (root / 'pending-install.json').exists():
            raise OSError('dry-run cannot predict recovery and refuses while recovery is pending: ' + str(root / 'pending-install.json') + '; run a real install or uninstall to attempt recovery. If that refuses, uninstall --purge discards adapter state and stored captures')
        _preflight_destination(root, skill)
        _preflight_write_access(root, skill)
        print('Would install explicit Devin output skill at ' + str(skill))
        recorded = _hook_record()
        if mode == 'auto' or (mode is None and recorded):
            _read_devin_config(devin_config())
            print('Would wire automatic compression hooks into ' + str(devin_config()))
        elif mode == 'manual' and recorded:
            _read_devin_config(devin_config())
            print('Would remove automatic compression hooks from ' + str(devin_config()))
        return
    with _lock():
        _recover_pending()
        owned = _preflight_destination(root, skill)
        # Fresh installs roll back only leaves/directories created by this attempt.
        # Refresh retains old bytes and manifest until a complete update succeeds.
        old_bytes={name:Path(name).read_bytes() for name in owned['files']} if owned else {}
        old_manifest=manifest_path().read_bytes() if owned else None
        new_leaves=[]
        pending=state.data_dir()/'pending-install.json'
        intended={str(root/'plugin'/name):hashlib.sha256(_render_payload(name)).hexdigest() for name in PAYLOAD}
        rendered=source.read_text().replace('@PYTHON@',shlex.quote(os.path.realpath(sys.executable))).replace('@FILTER@',shlex.quote(str(root/'plugin/devin_compress.py')))
        intended[str(skill/'SKILL.md')]=hashlib.sha256(rendered.encode()).hexdigest()
        transaction={'owner':MARKER,'files':intended}
        if owned:
            transaction['previous']={name:base64.b64encode(content).decode('ascii') for name,content in old_bytes.items()}
            transaction['previous_manifest']=base64.b64encode(old_manifest).decode('ascii')
        _atomic(pending,json.dumps(transaction,sort_keys=True).encode())
        try:
            state.private_path(root / 'plugin', directory=True, create=True)
            state.private_dir(skill)
            files = {}
            for name in PAYLOAD:
                target = root / 'plugin' / name
                new_leaves.append(str(target))
                _atomic(target, _render_payload(name))
                files[str(target)] = _hash(target)
            text = source.read_text().replace('@PYTHON@', shlex.quote(os.path.realpath(sys.executable)))
            text = text.replace('@FILTER@', shlex.quote(str(root / 'plugin' / 'devin_compress.py')))
            target = skill / 'SKILL.md'
            new_leaves.append(str(target))
            _atomic(target, text.encode())
            files[str(target)] = _hash(target)
            _atomic(manifest_path(), json.dumps({'owner': MARKER, 'files': files}, sort_keys=True).encode())
            state.enable_consent()
            _clear_pending(pending)
        except Exception:
            if owned:
                for name in set(new_leaves) - set(old_bytes):
                    Path(name).unlink(missing_ok=True)
                for name,content in old_bytes.items():
                    _atomic(Path(name),content)
                _atomic(manifest_path(),old_manifest)
            else:
                for name in new_leaves:
                    Path(name).unlink(missing_ok=True)
                manifest_path().unlink(missing_ok=True)
                for folder in (skill,root/'plugin'):
                    try:folder.rmdir()
                    except OSError:pass
            _clear_pending(pending)
            raise
        hook_error = None
        try:
            recorded = _hook_record()
            if mode == 'auto' or (mode is None and recorded):
                if mode == 'auto':
                    print(ATTESTATION_NOTICE)
                _wire_hooks()
                wired = True
            else:
                if mode == 'manual':
                    _unwire_hooks()
                wired = False
        except (OSError, ValueError) as exc:
            hook_error, wired = str(exc), False
    print('Installed Devin output skill: /token-optimizer-output works in every session.')
    if wired:
        print('Automatic mode ON: hooks wired into ' + str(devin_config()) + '. Large output from commands your Devin '
              'rules already allow (test runs, grep, git status, long logs...) is compressed with no extra prompts; commands Devin '
              'asks about still ask. Restart running Devin sessions to load the hooks.')
    elif mode == 'manual':
        print('Manual mode: no hooks in the Devin config.')
    else:
        print('Manual mode. For automatic compression run: install.sh --devin --auto')
    print('Consent: private captures may contain confidential/personal data; the adapter makes no extra copies beyond the captures you create.')
    print('A raw passthrough removes its capture after delivery; a compressed result keeps its capture for recovery until you delete it or uninstall --purge. A failed delivery keeps the capture.')
    print('Failures, stderr, binary and credential-matching output pass through RAW to Devin/model context.')
    print('Numeric local estimates retain 30 days, at most 10000 rows.')
    if hook_error:
        raise OSError('the skill is installed, but automatic hooks were NOT wired: ' + hook_error)


def _preflight_purge(root):
    # Purge removes state leaves by name, even unsafe modes/links. It cannot
    # unlink directories: detect those before removing any owned payload.
    for name in ('metrics.db', 'metrics.db-journal', 'metrics.db-wal', 'metrics.db-shm',
                 'consent.json', 'session-salt', 'install.json', 'pending-install.json', *HOOK_STATE_LEAVES):
        leaf = root / name
        if leaf.is_dir() and not leaf.is_symlink():
            raise OSError('purge state leaf is a directory; preserved: ' + str(leaf))

    captures = root / 'captures'
    state.no_symlinks(captures)
    if captures.exists():
        if not captures.is_dir():
            raise OSError('purge captures root is not a directory: ' + str(captures))
        if not os.access(captures, os.R_OK | os.W_OK | os.X_OK, effective_ids=True):
            raise OSError('captures root is not readable/writable/searchable: ' + str(captures))
        # delete_capture opens every absolute component O_RDONLY|O_DIRECTORY.
        # Verify that descriptor walk before any state/payload removal.
        folders = list(captures.iterdir())
        if any(folder.is_dir() and not folder.is_symlink() and re.fullmatch(r'capture-[0-9a-f]{32}', folder.name) for folder in folders):
            try:
                fd = state.directory_fd(captures)
                os.close(fd)
            except OSError as exc:
                raise OSError('capture descriptor path refused: ' + str(captures) + '; inspect ancestors and move state aside if needed: ' + str(exc)) from exc
        for folder in folders:
            if not folder.is_dir() or folder.is_symlink() or not re.fullmatch(r'capture-[0-9a-f]{32}', folder.name):
                continue
            state.private_path(folder, directory=True)
            if not os.access(folder, os.R_OK | os.W_OK | os.X_OK, effective_ids=True):
                raise OSError('capture directory is not writable/searchable: ' + str(folder))
            for name in ('stdout', 'stderr', '.prepared', 'complete.json'):
                state.no_symlinks(folder / name)
                leaf = folder / name
                if leaf.exists() and not stat.S_ISREG(leaf.lstat().st_mode):
                    raise OSError('purge capture leaf must be regular: ' + str(leaf))


def uninstall(*, dry_run=False, purge=False) -> None:
    if os.name != 'posix':
        raise OSError('native Windows unsupported')
    root = state.data_dir()
    if (not root.exists() and not root.is_symlink()
            and not skill_dir().exists() and not skill_dir().is_symlink()):
        print('No owned Devin install found.')
        return
    # The removal lock refuses a group/other-writable ancestor; say so here so
    # a dry-run predicts it.
    _preflight_ancestors(root, skill_dir())
    state.private_path(root, directory=True)
    state.private_path(root / 'install.lock')
    if purge:
        _preflight_purge(root)
    pending = root / 'pending-install.json'
    if dry_run and not purge and (pending.exists() or pending.is_symlink()):
        state.private_path(pending)
        raise OSError('dry-run refuses while recovery is pending: ' + str(pending) + '; run a real install or uninstall to attempt recovery. If that refuses, uninstall --purge discards adapter state and stored captures')
    # Purge is independent of an install manifest: plain uninstall may precede it.
    # Under purge a stale or foreign-version pending record is deleted, not
    # reconciled, so it must never block the purge path.
    if not dry_run and not purge and root.exists():
        with _lock():_recover_pending()
    owned = _ownership_for_removal(purge=purge)
    if not owned and not purge:
        print('No owned Devin install found.')
        return
    _preflight_removal_access(root, skill_dir(), owned, purge=purge)
    if dry_run:
        if purge and not owned:
            _preflight_sweep_access(_purge_recorded(), _known_payload_digests())
        if _hook_record(tolerant=purge):
            _read_devin_config(devin_config())
            print('Would remove automatic compression hooks from ' + str(devin_config()) + '; other hooks stay.')
        if purge:
            print('Would remove unchanged owned files, numeric state and fixed stored-capture leaves; foreign capture content is preserved.')
        else:
            print('Would remove owned skill/payload; numeric state and captures remain unchanged.')
        return
    with _lock():
        if purge:
            # Recheck after taking the lock: preflight paths may have changed.
            _preflight_purge(root)
        owned = _ownership_for_removal(purge=purge)
        _preflight_removal_access(root, skill_dir(), owned, purge=purge)
        # Unwire before any payload is removed. A plain uninstall stops when the
        # config cannot be edited; purge carries on (a stale entry is inert:
        # its command ends in '|| true' and never blocks a tool call).
        try:
            if _unwire_hooks(tolerant=purge):
                print('Removed automatic compression hooks from ' + str(devin_config()) + '; other hooks unchanged.')
        except (OSError, ValueError) as exc:
            if not purge:
                raise OSError('automatic hooks could not be removed, nothing was uninstalled: ' + str(exc)) from None
            print('Warning: automatic hooks could not be removed from ' + str(devin_config()) + ': ' + str(exc) +
                  '. Delete the Token Optimizer entries under "hooks" by hand; until then they do nothing.')
        removed_payload = bool(owned)
        if purge:
            recorded = _purge_recorded()
            known = _known_payload_digests()
            if not owned:
                _preflight_sweep_access(recorded, known)
        if owned:
            # Directory access was checked above; file flags and ACLs can still
            # refuse one unlink. Finish the rest, keep the ownership record so a
            # rerun completes, and stop before any other state is removed.
            stuck = []
            for name in owned['files']:
                try:
                    Path(name).unlink(missing_ok=True)
                except OSError as exc:
                    stuck.append(name + ' (' + (exc.strerror or str(exc)) + ')')
            if stuck:
                raise OSError('could not remove owned files: ' + ', '.join(stuck) +
                              '; other owned files were removed and the ownership record was kept.'
                              ' Clear the file flags or permissions, then rerun the same uninstall')
            manifest_path().unlink(missing_ok=True)
        cache = state.data_dir() / 'plugin' / '__pycache__'
        try:
            cache_present = cache.exists() and not (purge and not owned and (state.data_dir() / 'plugin').is_symlink())
        except OSError:
            cache_present = False  # Inaccessible foreign folder: never inspected.
        if cache_present:
            state.no_symlinks(cache)
            try:
                for item in cache.iterdir():
                    if item.name.endswith('.pyc') and item.is_file() and not item.is_symlink():
                        item.unlink()
                cache.rmdir()
            except OSError:
                print('Cache content was preserved under ' + str(cache) + '; inspect it before retrying removal.')
        for folder in (skill_dir(), state.data_dir() / 'plugin'):
            try: folder.rmdir()
            except OSError: pass
        if purge:
            # Identify unchanged adapter files while the records still exist.
            swept = _sweep_candidates(recorded, known)
            for name in ('metrics.db', 'metrics.db-journal', 'metrics.db-wal', 'metrics.db-shm',
                         'consent.json', 'session-salt', 'install.json', 'pending-install.json', *HOOK_STATE_LEAVES):
                # Purge removes the state leaf by name even when its mode,
                # link count or spelling would fail the privacy gate.
                (state.data_dir() / name).unlink(missing_ok=True)
            # The sweep removes only files whose digest the manifest recorded
            # or a known shipped version produced: unchanged adapter files.
            # Edited files and foreign content are preserved.
            for item in swept:
                try:
                    item.unlink()
                    removed_payload = True
                except OSError:
                    pass
            for folder in (state.data_dir() / 'plugin', skill_dir()):
                try:
                    if folder.is_symlink() or not folder.is_dir():
                        continue
                    state.no_symlinks(folder)
                except OSError:
                    continue
                try: folder.rmdir()
                except OSError: pass
            captures = state.data_dir() / 'captures'
            if captures.exists():
                state.no_symlinks(captures)
                for folder in captures.iterdir():
                    if not folder.is_dir() or folder.is_symlink() or not re.fullmatch(r'capture-[0-9a-f]{32}', folder.name):
                        continue
                    # Stored captures may hold credentials; purge removes them.
                    # Foreign-named content keeps its directory (and itself).
                    state.delete_capture(folder)
                try: captures.rmdir()
                except OSError: pass
            remaining = [str(folder) for folder in (state.data_dir() / 'plugin', skill_dir(), captures) if folder.exists()]
            if remaining:
                print('Unowned or edited files were preserved under ' + ' and '.join(remaining) +
                      '; inspect them and move the folders aside to finish removing the adapter.')
            print('Purged numeric metrics, SQLite sidecars, consent and HMAC key.' +
                  (' Some capture content was preserved; inspect the reported captures directory.' if captures.exists() else ' Stored captures removed.'))
    if not purge:
        print('Original captures unchanged.')
    print('Removed owned Devin skill/payload.' if removed_payload else 'No owned Devin skill/payload was installed.')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'uninstall'))
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--purge', action='store_true')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--auto', action='store_true',
                       help='also wire automatic compression hooks; ' + ATTESTATION_NOTICE)
    modes.add_argument('--manual', action='store_true', help='remove automatic hooks, keep the skill')
    args = parser.parse_args(argv)
    if args.action == 'install' and args.purge:
        parser.error('--purge is uninstall-only')
    if args.action == 'uninstall' and (args.auto or args.manual):
        parser.error('--auto and --manual are install-only')
    try:
        if args.action == 'install':
            install(dry_run=args.dry_run, mode='auto' if args.auto else 'manual' if args.manual else None)
        else:
            uninstall(dry_run=args.dry_run, purge=args.purge)
        return 0
    except RecursionError as exc:
        print('Devin install refused: ' + str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print('Devin install refused: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
