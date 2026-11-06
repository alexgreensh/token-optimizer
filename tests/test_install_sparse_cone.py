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
from pathlib import Path

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
