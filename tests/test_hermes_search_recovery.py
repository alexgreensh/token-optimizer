"""Installed Hermes search keeps every match recoverable (#334)."""
import re
import shlex
import subprocess

import pytest

from test_hermes_parity import (  # noqa: F401 (home is a pytest fixture)
    _clean_env, _install, _load_plugin, _terminal, home,
)


@pytest.mark.parametrize('short', [False, True])
def test_installed_search_retains_every_match_or_prints_working_expand(home, short):
    plugin_dir = _install(home)
    installed = _load_plugin(plugin_dir / '__init__.py', 'to_hermes_search_recovery')
    installed._hook_timeout_cache['value'] = 30.0
    raw = (''.join(f'./{f}:{i}:q\n' for f in ('a', 'b') for i in range(1, 21))
           if short else ''.join(f'a:{i}:needle hello\n' for i in range(1, 501)))
    out = _terminal(installed, 'search_recovery', 'grep -rn needle .', raw)
    if short:
        assert out is None or out == raw
    else:
        assert isinstance(out, str)
        assert 'up to 3 matches per file' in out
        match = re.search(r'^\s*(\S+ \S+ --runtime hermes expand [A-Za-z0-9_-]+)\s*$', out, re.MULTILINE)
        assert match, out
        proc = subprocess.run(shlex.split(match.group(1)), env=_clean_env(home),
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0
        assert proc.stdout.strip() == raw.strip()
