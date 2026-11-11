"""Child processes started by tests can never resolve the real home or data dir.

In August 2026 a hook harness spawned the real hook with an env that resolved
to the developer's home, and 532 synthetic compression rows landed in the live
ledger. The children here only print what they resolve; nothing is written.
"""
import os
import subprocess
import sys

import _home_guard

PROBE = ("import os, pathlib; print(pathlib.Path.home()); "
         "print(os.environ.get('CLAUDE_CONFIG_DIR', '')); print(os.environ.get('TOKEN_OPTIMIZER_SNAPSHOT_DIR', ''))")


def _child(env):
    out = subprocess.run([sys.executable, "-c", PROBE], env=env, capture_output=True, text=True, check=True)
    return out.stdout.splitlines()


def _clear(request):
    return _home_guard.take_hits(request.node.nodeid)


def test_child_without_home_gets_the_sandbox(request):
    env = {k: v for k, v in os.environ.items() if k not in ("HOME", "USERPROFILE")}
    home, _cfg, _snap = _child(env)
    assert os.path.realpath(home) == os.path.realpath(str(_home_guard.SANDBOX / "home"))
    assert _clear(request) == []


def test_child_handed_the_real_home_is_redirected_and_flagged(request):
    real = _home_guard.REAL_HOME
    env = dict(os.environ, HOME=real, CLAUDE_CONFIG_DIR=os.path.join(real, ".claude"),
               TOKEN_OPTIMIZER_SNAPSHOT_DIR=os.path.join(real, ".claude", "plugins", "data"))
    home, cfg, snap = _child(env)
    sandbox = os.path.realpath(str(_home_guard.SANDBOX / "home"))
    assert os.path.realpath(home) == sandbox
    assert cfg.startswith(sandbox) and snap.startswith(sandbox)
    hits = _clear(request)
    assert {h[3].split("=")[0] for h in hits} == {"HOME", "CLAUDE_CONFIG_DIR", "TOKEN_OPTIMIZER_SNAPSHOT_DIR"}


def test_child_env_inside_sandbox_or_repo_is_left_alone(request, tmp_path):
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(tmp_path / ".claude"))
    _home, cfg, _snap = _child(env)
    assert cfg == str(tmp_path / ".claude")
    assert _clear(request) == []


def test_a_named_home_variable_fills_its_sibling_not_the_sandbox(request, tmp_path):
    # Windows reads USERPROFILE, POSIX reads HOME: a test that names one means that home.
    named = str(tmp_path / "home")
    only_home = _home_guard.sanitize_child_env({"HOME": named})
    assert only_home["HOME"] == named and only_home["USERPROFILE"] == named
    only_profile = _home_guard.sanitize_child_env({"USERPROFILE": named})
    assert only_profile["HOME"] == named and only_profile["USERPROFILE"] == named
    neither = _home_guard.sanitize_child_env({})
    sandbox = str(_home_guard.SANDBOX / "home")
    assert neither["HOME"] == sandbox and neither["USERPROFILE"] == sandbox
    assert _clear(request) == []
