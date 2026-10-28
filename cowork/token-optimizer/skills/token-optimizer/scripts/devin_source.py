"""Source-only imports for the installed payload. Never trust cached bytecode."""
import importlib.abc
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

PAYLOAD = ('devin_compress.py', 'devin_state.py', 'devin_doctor.py', 'devin_verify.py', 'devin_source.py',
           'bash_compress.py', 'bash_whitelist.py', 'credential_patterns.py',
           'token_estimate.py', 'command_filters.py', 'runtime_env.py', 'py_trust.py', 'plugin_env.py', 'refetch_fingerprint.py',
           'devin_hooks.py')
ALLOWED_MODULES = frozenset(Path(name).stem for name in PAYLOAD)

# Repo names outside the installed closure must never fall back to sys.path.
REPO_MODULES = frozenset((
    'activity_tracker', 'antigravity_doctor', 'antigravity_hook_bridge', 'antigravity_install',
    'antigravity_proto', 'antigravity_session', 'antigravity_state', 'archive_result',
    'bash_compress', 'bash_compress_hook', 'bash_hook', 'bash_whitelist', 'benchmark', 'cloud_kit',
    'build_output_compress', 'codex_command_compress', 'codex_compact_prompt', 'codex_doctor',
    'codex_hook_bridge', 'codex_install', 'codex_io', 'codex_log_index', 'codex_models',
    'codex_session', 'codex_state', 'codex_statusline', 'command_filters',
    'compression_backfill', 'compression_log', 'context_intel', 'context_pressure',
    'copilot_doctor', 'copilot_hook_bridge', 'copilot_install', 'copilot_session',
    'copilot_state', 'copilot_vscode', 'cowork_doctor', 'cowork_install',
    'credential_patterns', 'cursor_doctor', 'cursor_hook_bridge', 'cursor_install',
    'cursor_session', 'cursor_state', 'delta_diff', 'detectors', 'deterministic_candidates',
    'devin_compress', 'devin_doctor', 'devin_hooks', 'devin_install', 'devin_source', 'devin_state',
    'devin_verify', 'grok_doctor', 'grok_hook_bridge', 'grok_install', 'grok_session',
    'grok_state', 'hermes_doctor', 'hermes_hook_bridge', 'hermes_install', 'hermes_session',
    'hermes_state', 'hermes_tool_hooks', 'hook_io', 'hook_runtime', 'injection', 'install_reconcile', 'measure',
    'outline', 'pipeline_analyzer', 'plugin_env', 'py_trust', 'quality_cache_gate',
    'read_cache', 'recovery_output', 'refetch_fingerprint', 'refetch_guard', 'routing_advisor',
    'runtime_env', 'session_store', 'spawn_utils', 'structure_map', 'structure_map_ts',
    'structure_replay', 'thrash_guard', 'token_estimate', 'tool_search_diagnostic', 'utf8_io',
))

class SourceOnly(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, root):
        self.root=Path(root)
        self.digests={}
    def find_spec(self, fullname, path=None, target=None):
        if fullname not in ALLOWED_MODULES:
            if fullname.split('.')[0] in REPO_MODULES:
                raise ImportError('repo module outside Devin payload')
            return None
        candidate=self.root/(fullname+'.py')
        try:info=candidate.lstat()
        except FileNotFoundError as exc:raise ImportError('manifested payload source missing: '+str(candidate)) from exc
        if not stat.S_ISREG(info.st_mode):raise ImportError('regular payload source required')
        return importlib.util.spec_from_file_location(fullname,candidate,loader=self)
    def create_module(self,spec):return None
    def exec_module(self,module):
        source=Path(module.__spec__.origin)
        fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_nlink!=1 or info.st_mode&0o077:
                raise ImportError('private regular payload source required')
            chunks=[];remaining=8*1024*1024+1
            while remaining:
                chunk=os.read(fd,min(65536,remaining))
                if not chunk:break
                chunks.append(chunk);remaining-=len(chunk)
            content=b''.join(chunks)
            if len(content)>8*1024*1024:raise ImportError('payload source exceeds bound')
        finally:os.close(fd)
        if hashlib.sha256(content).hexdigest()!=self.digests.get(str(source)):
            raise ImportError('payload source differs from install manifest: '+str(source))
        exec(compile(content,str(source),'exec'),module.__dict__)

def activate(root):
    root=Path(root).resolve()
    # Imported payload entrypoints activate again; do not evict in-progress modules.
    for active in sys.meta_path:
        if getattr(active,'_devin_source_root',None)==root:
            return
    finder=SourceOnly(root)
    finder._devin_source_root=root
    manifest=root.parent/'install.json'
    fd=os.open(manifest,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid()
                or info.st_nlink!=1 or info.st_mode&0o077 or info.st_size>65536):
            raise ImportError('private bounded install manifest required')
        with os.fdopen(fd,'rb',closefd=False) as stream:
            content=stream.read(65537)
    finally:os.close(fd)
    if len(content)>65536:raise ImportError('install manifest exceeds bound')
    record=json.loads(content)
    if not isinstance(record,dict) or record.get('owner')!='devin-output-filter-v1':
        raise ImportError('invalid install manifest')
    digests=record.get('files')
    if not isinstance(digests,dict):raise ImportError('invalid install manifest files')
    expected={str(root/name) for name in PAYLOAD}
    if not expected.issubset(digests):raise ImportError('missing payload commitments')
    if any(not isinstance(digests[name],str) or len(digests[name])!=64
           or any(ch not in '0123456789abcdef' for ch in digests[name]) for name in expected):
        raise ImportError('invalid payload digest')
    finder.digests={name:digests[name] for name in expected}
    # Remove root, ancestor and descendant routes, including current-directory and symlink aliases.
    # This is an activation-time boundary, not control over later path mutations.
    retained=[]
    for entry in sys.path:
        if not isinstance(entry,str):continue
        try:
            candidate=Path(entry or os.getcwd()).resolve()
            if root==candidate or root.is_relative_to(candidate) or candidate.is_relative_to(root):continue
        except (OSError,RuntimeError,ValueError):
            continue
        retained.append(entry)
    sys.path[:]=retained
    for name in tuple(sys.modules):
        if name.split('.')[0] in REPO_MODULES:
            sys.modules.pop(name,None)
    sys.meta_path.insert(0,finder)
