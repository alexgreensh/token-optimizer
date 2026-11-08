"""Copied adapters must render search results without imports from the checkout."""
import importlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))

PROBE = '''import sys
from pathlib import Path
root=Path(sys.argv[1])
sys.path.insert(0,str(root))
if (root/'devin_source.py').exists() and (root.parent/'install.json').exists():
    namespace={}
    exec(compile((root/'devin_source.py').read_bytes(),str(root/'devin_source.py'),'exec'),namespace)
    namespace['activate'](root)
import bash_compress
raw=''.join(f'a:{i}:needle hello\\n' for i in range(1,501))
out=bash_compress.compress('grep -rn needle .',raw)
assert 'up to 3 matches per file' in out, out[:200]
assert len(out)<len(raw)//2
import bash_search_results
assert Path(bash_search_results.__file__).parent==root
'''


@pytest.mark.parametrize('adapter', ['grok', 'copilot', 'cursor', 'antigravity', 'devin', 'codex', 'cowork'])
def test_installed_adapter_can_render_search_without_checkout(tmp_path, monkeypatch, request, adapter):
    if adapter == 'devin' and os.name != 'posix':
        pytest.skip('Devin installer intentionally supports POSIX only')
    if os.name == 'posix':
        # Hosted CI Python can be world-writable. Use real copied executable
        # bytes with private modes, never bypass the installers' trust gate.
        binary, prefix = request.getfixturevalue('devin_real_python')
        monkeypatch.setattr(sys, 'executable', str(binary))
        monkeypatch.setenv('TOKEN_OPTIMIZER_PYTHON', str(binary))
        monkeypatch.setenv('PYTHONHOME', prefix)
        monkeypatch.setenv('LD_LIBRARY_PATH', str(Path(prefix) / 'lib'))
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    monkeypatch.delenv('DEVIN_HOME', raising=False)
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    monkeypatch.delenv('TOKEN_OPTIMIZER_DEVIN_HOME', raising=False)
    if adapter in {'codex', 'cowork'}:
        # These ship a complete generated skill tree, not an explicit Python list.
        import shutil
        source = SCRIPTS.parents[2] / ('plugins' if adapter == 'codex' else 'cowork') / 'token-optimizer/skills/token-optimizer/scripts'
        root = tmp_path / 'payload'
        shutil.copytree(source, root, ignore=shutil.ignore_patterns('__pycache__'))
    elif adapter == 'devin':
        installer = importlib.import_module('devin_install')
        installer.install()
        root = installer.state.data_dir() / 'plugin'
    else:
        installer = importlib.import_module(adapter + '_install')
        home = tmp_path / adapter
        installer.install(home=home)
        root = installer.plugin_dir(home) if adapter == 'antigravity' else installer._plugin_dir(home)
    env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
    env['PYTHONNOUSERSITE'] = '1'
    proc = subprocess.run([sys.executable, '-I', '-c', PROBE, str(root)], cwd=tmp_path,
                          env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
