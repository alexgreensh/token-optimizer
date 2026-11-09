"""Installed payload integrity/readiness shared by doctor and filter."""

import hashlib
import json
import os
import re
import stat
from pathlib import Path
import devin_state as state

from devin_source import PAYLOAD

def verify_install():
    root=state.data_dir()
    plugin=state.private_path(root/'plugin',directory=True)
    for entry in plugin.iterdir():
        if entry.name in PAYLOAD:
            state.private_path(entry)
            continue
        if entry.name=='__pycache__':
            state.no_symlinks(entry)
            info=entry.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o022:
                raise OSError('unsafe incidental cache directory')
            for cached in entry.iterdir():
                state.no_symlinks(cached)
                try:
                    info=cached.lstat()
                except FileNotFoundError:
                    continue  # a concurrent import renamed its temporary file into place
                # Python writes NAME.pyc.<digits> and renames it to NAME.pyc;
                # parallel first runs see that in-flight file. It is never imported.
                if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid()
                        or info.st_nlink!=1 or info.st_mode&0o022
                        or not re.fullmatch(r'[\w.-]+\.pyc(?:\.[0-9]+)?',cached.name)):
                    raise OSError('unmanifested artifact in incidental cache: ' + str(cached) +
                                  '; remove it and retry')
            continue
        raise OSError('unmanifested artifact in installed payload: ' + str(entry) +
                      '; remove it and retry')
    manifest=state.private_path(root/'install.json')
    if manifest.stat().st_nlink!=1:raise OSError('manifest hardlink refused')
    try:
        record=json.loads(manifest.read_text())
    except (ValueError,RecursionError):
        raise OSError('invalid installed payload manifest')
    if not isinstance(record,dict) or not isinstance(record.get('files'),dict):
        raise OSError('invalid installed payload manifest types')
    if any(not isinstance(k,str) for k in record['files']):
        raise OSError('invalid installed payload manifest')
    if any(not isinstance(v,str) or len(v)!=64 or any(ch not in '0123456789abcdef' for ch in v) for v in record['files'].values()):
        raise OSError('invalid manifest digest')
    expected={str(root/'plugin'/name) for name in PAYLOAD}|{str(state.home()/'skills/token-optimizer-output/SKILL.md')}
    if record.get('owner')!='devin-output-filter-v1' or set(record.get('files',{}))!=expected:
        raise OSError('invalid installed payload manifest')
    for name,digest in record['files'].items():
        path=state.private_path(Path(name))
        if path.stat().st_nlink!=1:raise OSError('payload hardlink refused')
        content=path.read_bytes()
        if hashlib.sha256(content).hexdigest()!=digest:raise OSError('installed payload missing or changed')
        if path.suffix=='.py':compile(content,str(path),'exec')
    # Name an unsafe data directory or consent file before reporting consent
    # as missing: the record may be present and merely unreadable by policy.
    state.private_path(state.data_dir(),directory=True)
    state.private_path(state.data_dir()/'consent.json')
    if not state.consent_ok():raise OSError('missing local data consent')
    return True


def payload_key_shapes(plugin, names, skill_md):
    """Recorded-key sets for every payload shape an install record may carry.

    Returns ``(expected, shapes)``: the current payload's path set plus the
    sorted key lists for both independent module additions and older releases.
    """
    expected={str(plugin/name) for name in names}|{str(skill_md)}
    tomli=str(plugin/'_tomli.py')
    search=str(plugin/'bash_search_results.py')
    urls=str(plugin/'credential_url_patterns.py')
    variants=(expected,expected-{search},expected-{urls},expected-{search,urls})
    before_both=expected-{search,urls}
    previous=before_both-{str(plugin/'devin_hooks.py'),tomli}
    legacy=previous-{str(plugin/name) for name in ('plugin_env.py','refetch_fingerprint.py')}
    return expected,tuple(sorted(keys) for keys in (*variants,before_both-{tomli},previous,legacy))
