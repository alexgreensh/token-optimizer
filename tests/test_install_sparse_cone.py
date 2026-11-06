"""Verified installs must materialize root files the checksum manifest covers.

install.sh clones with ``git clone --sparse`` and then narrows the checkout to
the runtime directories. The code assumed cone-mode semantics ("root-level
files are included automatically"), but ``git sparse-checkout set`` only
defaults to cone mode on newer git. On git 2.34.1 (Ubuntu 22.04 LTS) cone mode
stays off, root files such as install.sh silently vanish from the checkout,
and CHECKSUMS.sha256 -- which lists install.sh -- fails every verified install
with "Your install may be compromised".

The fix enables cone mode explicitly (``sparse-checkout init --cone``) before
every ``set`` that runs on a fresh clone. These tests lock both the source
shape and the git behavior the fix relies on, using a local file:// remote so
nothing touches the network.

Run: python3 -m pytest tests/test_install_sparse_cone.py -v
"""

import hashlib
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"

SPARSE_DIRS = "skills/ hooks/ devin/ .claude-plugin/ .codex-plugin/ .codex/"


def _src():
    return INSTALL_SH.read_text(encoding="utf-8")


def _function_block(src, name):
    start = src.index(name + "() {")
    next_fn = src.find("\n}\n", start)
    assert next_fn != -1, f"{name} body not found"
    return src[start:next_fn]


def test_try_clone_enables_cone_mode_before_set():
    block = _function_block(_src(), "try_clone")
    init = block.find("sparse-checkout init --cone")
    assert init != -1, "try_clone must enable cone mode explicitly"
    narrow = block.find("sparse-checkout set", init)
    assert narrow != -1, "try_clone must narrow the checkout after enabling cone"


def test_candidate_verification_enables_cone_mode_before_set():
    block = _function_block(_src(), "verify_release_candidate_before_live_update")
    init = block.find("sparse-checkout init --cone")
    assert init != -1, "candidate clone must enable cone mode explicitly"
    narrow = block.find("sparse-checkout set", init)
    assert narrow != -1, "candidate clone must narrow after enabling cone"


def _git(*args, cwd=None):
    return subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


@pytest.fixture()
def remote(tmp_path):
    """A tiny stand-in for the GitHub repo: a root file the manifest covers,
    plus one runtime directory."""
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-q", "-b", "main", str(src))
    _git("config", "core.autocrlf", "false", cwd=src)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "init", cwd=src)
    (src / "install.sh").write_bytes(b"#!/bin/bash\n# installer\n")
    (src / "skills").mkdir()
    (src / "skills" / "x.py").write_bytes(b"print('x')\n")
    (src / "openclaw").mkdir()
    (src / "openclaw" / "skip.txt").write_bytes(b"not part of installs\n")
    _git("add", "-A", cwd=src)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
         "-m", "payload", cwd=src)
    digest = hashlib.sha256((src / "install.sh").read_bytes()).hexdigest()
    (src / "CHECKSUMS.sha256").write_bytes((
        f"{digest}  install.sh\n"
        f"{hashlib.sha256((src / 'skills' / 'x.py').read_bytes()).hexdigest()}"
        "  skills/x.py\n").encode("utf-8"))
    _git("add", "CHECKSUMS.sha256", cwd=src)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
         "-m", "manifest", cwd=src)
    _git("tag", "v1.0.0", cwd=src)
    return src


def _clone(remote, dest, *pre_set):
    _git("clone", "--depth", "1", "--filter=blob:none", "--sparse",
         "--branch", "v1.0.0", f"file://{remote}", str(dest))
    for step in pre_set:
        _git(*step, cwd=dest)
    _git("sparse-checkout", "set", *SPARSE_DIRS.split(), cwd=dest)


def test_fixed_clone_sequence_materializes_manifest_root_files(remote, tmp_path):
    dest = tmp_path / "fixed"
    _clone(remote, dest, ("sparse-checkout", "init", "--cone"))
    cone = _git("config", "core.sparseCheckoutCone", cwd=dest).stdout.strip()
    assert cone == "true"
    assert (dest / "install.sh").is_file(), (
        "root file install.sh must survive the sparse checkout: the release "
        "manifest covers it and verification aborts when it is missing")
    check = subprocess.run(["sha256sum", "-c", "CHECKSUMS.sha256", "--quiet"],
                           cwd=dest, capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
    assert not (dest / "openclaw" / "skip.txt").exists()


def test_bare_set_behavior_matches_cone_config(remote, tmp_path):
    """Document the defect condition without depending on one git version:
    when cone mode is off, root files vanish; when on, they survive."""
    dest = tmp_path / "bare"
    _clone(remote, dest)
    probe = subprocess.run(["git", "config", "--bool", "core.sparseCheckoutCone"],
                           cwd=dest, capture_output=True, text=True)
    cone = probe.stdout.strip() or "false"
    if cone == "true":
        assert (dest / "install.sh").is_file()
    else:
        assert not (dest / "install.sh").exists(), (
            "non-cone sparse checkout must not claim root files it does "
            "not materialize")


def test_noncone_root_omission_is_repaired_without_changing_checksums(remote, tmp_path):
    dest = tmp_path / "legacy cone off"
    _clone(remote, dest)
    _git("sparse-checkout", "set", "--no-cone", *SPARSE_DIRS.split(), cwd=dest)
    assert not (dest / "install.sh").exists()
    manifest = (remote / "CHECKSUMS.sha256").read_bytes()
    check = subprocess.run(["sha256sum", "-c", "CHECKSUMS.sha256", "--quiet"],
                           cwd=dest, capture_output=True)
    assert check.returncode != 0, "the pre-fix non-cone tree must fail verification"
    _git("sparse-checkout", "init", "--cone", cwd=dest)
    _git("sparse-checkout", "set", *SPARSE_DIRS.split(), cwd=dest)
    check = subprocess.run(["sha256sum", "-c", "CHECKSUMS.sha256", "--quiet"],
                           cwd=dest, capture_output=True)
    assert check.returncode == 0, check.stderr
    assert (dest / "CHECKSUMS.sha256").read_bytes() == manifest


@pytest.mark.parametrize("path_index", range(6))
def test_semantic_sparse_guard_catches_each_missing_release_template_path(path_index):
    from helpers.install_sparse import assert_sparse_paths, sparse_set_commands
    source = _src().replace('\\\n', ' ')
    command = sparse_set_commands(source)[path_index]
    broken = command.replace('$SPARSE_CONE_DIRS', 'skills/ hooks/ .claude-plugin/ .codex-plugin/ .codex/')
    assert broken != command
    cursor = 0
    for current in sparse_set_commands(source)[:path_index + 1]:
        start = source.index(current, cursor)
        cursor = start + len(current)
    source = source[:start] + broken + source[cursor:]
    with pytest.raises(AssertionError):
        assert_sparse_paths(source)



def test_semantic_sparse_guard_control_reaches_all_six_commands(monkeypatch):
    import tempfile
    from helpers.install_sparse import assert_sparse_paths
    original = tempfile.TemporaryDirectory
    monkeypatch.setattr(tempfile, "TemporaryDirectory", lambda: original(prefix="sparse probe é "))
    assert_sparse_paths(_src())



def test_sparse_probe_execution_error_is_not_a_caught_mutation():
    from helpers.install_sparse import assert_sparse_paths, sparse_assignment
    source = _src()
    assignment = sparse_assignment(source)
    broken = assignment.split('#', 1)[0] + '; ARG_LOG="$INSTALL_DIR/missing-parent/argv"'
    with pytest.raises(RuntimeError, match="stderr:"):
        assert_sparse_paths(source.replace(assignment, broken, 1))


@pytest.mark.parametrize('candidate', [
    r'C:\Program Files\Git\bin\bash.exe',
    r'C:\Program Files\Git\usr\bin\bash.exe',
    r'C:\Program Files (x86)\Git\bin\bash.exe',
])
def test_sparse_probe_uses_git_bash_instead_of_windows_wsl(monkeypatch, candidate):
    from types import SimpleNamespace
    from helpers import install_sparse
    monkeypatch.setattr(install_sparse, 'os', SimpleNamespace(name='nt', environ={}))
    monkeypatch.setattr(install_sparse.Path, 'is_file', lambda path: str(path) == candidate)
    monkeypatch.setattr(install_sparse.shutil, 'which', lambda name: r'C:\Windows\System32\bash.exe')
    assert install_sparse.install_bash() == candidate


@pytest.mark.parametrize('path', [None, r'C:\Windows\System32\bash.exe', r'C:\WINDOWS\SYSTEM32\bash.exe'])
def test_sparse_probe_refuses_missing_or_wsl_only_windows_shell(monkeypatch, path):
    from types import SimpleNamespace
    from helpers import install_sparse
    monkeypatch.setattr(install_sparse, 'os', SimpleNamespace(name='nt', environ={}))
    monkeypatch.setattr(install_sparse.Path, 'is_file', lambda _: False)
    monkeypatch.setattr(install_sparse.shutil, 'which', lambda _: path)
    with pytest.raises(RuntimeError, match='Git Bash is required'):
        install_sparse.install_bash()


@pytest.mark.parametrize('path', [r'D:\Portable Git\bin\bash.exe', '/usr/bin/bash'])
@pytest.mark.parametrize('path_type', [Path, PureWindowsPath])
def test_sparse_probe_honors_explicit_git_bash_override(monkeypatch, path, path_type):
    from types import SimpleNamespace
    from helpers import install_sparse
    monkeypatch.setattr(install_sparse, 'os', SimpleNamespace(name='nt', environ={'GIT_BASH': path}))
    monkeypatch.setattr(install_sparse, 'Path', path_type)
    monkeypatch.setattr(path_type, 'is_file', lambda candidate: candidate == path_type(path), raising=False)
    monkeypatch.setattr(install_sparse.shutil, 'which', lambda _: r'C:\Windows\System32\bash.exe')
    assert install_sparse.install_bash() == path


def test_sparse_probe_both_execution_paths_use_selected_shell():
    import ast
    from helpers import install_sparse
    tree = ast.parse(Path(install_sparse.__file__).read_text(encoding='utf-8'))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == 'run']
    assert len(calls) == 2
    assert all(isinstance(call.args[0].elts[0], ast.Call)
               and call.args[0].elts[0].func.id == 'install_bash' for call in calls)


@pytest.mark.parametrize('name,path,expected', [
    ('posix', '/usr/bin/bash', '/usr/bin/bash'),
    ('posix', None, 'bash'),
    ('nt', r'D:\Portable Git\bin\bash.exe', r'D:\Portable Git\bin\bash.exe'),
])
def test_sparse_probe_platform_path_fallback(monkeypatch, name, path, expected):
    from types import SimpleNamespace
    from helpers import install_sparse
    monkeypatch.setattr(install_sparse, 'os', SimpleNamespace(name=name, environ={}))
    monkeypatch.setattr(install_sparse.Path, 'is_file', lambda _: False)
    monkeypatch.setattr(install_sparse.shutil, 'which', lambda _: path)
    assert install_sparse.install_bash() == expected


def test_sparse_probe_does_not_accept_wsl_even_as_override(monkeypatch):
    from types import SimpleNamespace
    from helpers import install_sparse
    wsl = r'C:\WINDOWS\SYSTEM32\bash.exe'
    monkeypatch.setattr(install_sparse, 'os', SimpleNamespace(name='nt', environ={'GIT_BASH': wsl}))
    monkeypatch.setattr(install_sparse.Path, 'is_file', lambda _: True)
    monkeypatch.setattr(install_sparse.shutil, 'which', lambda _: wsl)
    assert install_sparse.install_bash() == r'C:\Program Files\Git\bin\bash.exe'
