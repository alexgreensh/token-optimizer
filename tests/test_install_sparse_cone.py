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
    # Persist LF checkout policy so Git called later by Bash matches setup.
    if args and args[0] == "clone":
        args = ("clone", "--config", "core.autocrlf=false", *args[1:])
    return subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd, check=True,
                          capture_output=True, text=True, encoding="utf-8")


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
                           cwd=dest, capture_output=True, text=True, encoding="utf-8")
    assert check.returncode == 0, check.stderr
    assert not (dest / "openclaw" / "skip.txt").exists()


def test_bare_set_behavior_matches_cone_config(remote, tmp_path):
    """Document the defect condition without depending on one git version:
    when cone mode is off, root files vanish; when on, they survive."""
    dest = tmp_path / "bare"
    _clone(remote, dest)
    probe = subprocess.run(["git", "config", "--bool", "core.sparseCheckoutCone"],
                           cwd=dest, capture_output=True, text=True, encoding="utf-8")
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
    _git("sparse-checkout", "disable", cwd=dest)
    check = subprocess.run(["sha256sum", "-c", "CHECKSUMS.sha256", "--quiet"],
                           cwd=dest, capture_output=True)
    assert check.returncode == 0, check.stderr
    assert (dest / "CHECKSUMS.sha256").read_bytes() == manifest


@pytest.mark.parametrize("path_index", range(2))
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



def test_semantic_sparse_guard_control_reaches_both_fresh_clone_commands(monkeypatch):
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


@pytest.mark.parametrize('mode', ['legacy', 'cone', 'full'])
@pytest.mark.parametrize('outcome', ['success', 'bad-checksum', 'failed-update', 'disable-failed'])
@pytest.mark.parametrize('ignore', ['exclude', 'gitignore', 'untracked'])
def test_existing_install_keeps_ignored_extra_folder_data(remote, tmp_path, mode, outcome, ignore):
    from helpers.install_sparse import install_bash, shell_function, sparse_assignment
    import os
    dest = tmp_path / 'installed é extra'
    _git('clone', remote.as_uri(), str(dest))
    if mode == 'cone':
        _git('sparse-checkout', 'init', '--cone', cwd=dest)
    if mode != 'full':
        _git('sparse-checkout', 'set', '--cone' if mode == 'cone' else '--no-cone',
             *SPARSE_DIRS.split(), 'openclaw/', cwd=dest)
    database = dest / 'openclaw/store/private.db'
    database.parent.mkdir(parents=True)
    secret_fixture = b'private fixture db\x00\xff'
    database.write_bytes(secret_fixture)
    if ignore != 'untracked':
        ignore_file = dest / ('.git/info/exclude' if ignore == 'exclude' else '.gitignore')
        ignore_file.write_text('openclaw/store/\n', encoding='utf-8')
        assert _git('check-ignore', 'openclaw/store/private.db', cwd=dest).returncode == 0
    source = _src()
    start = source.index('\n', source.index('    INSTALL_OLD_HEAD=$(git -C')) + 1
    end = source.index('\nelif [ -d "$INSTALL_DIR" ]; then', start)
    branch = source[start:end]
    integrity_start = source.index('if verification_enabled; then', end)
    integrity_end = source.index('\n# Log the current commit SHA', integrity_start)
    branch += '\n' + source[integrity_start:integrity_end]
    old_head = _git('rev-parse', 'HEAD', cwd=dest).stdout.strip()
    new_head = old_head
    if outcome == 'bad-checksum':
        (remote / 'install.sh').write_bytes(b'changed unverified installer\n')
        _git('add', 'install.sh', cwd=remote)
        _git('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'bad release', cwd=remote)
        new_head = _git('rev-parse', 'HEAD', cwd=remote).stdout.strip()
        _git('fetch', 'origin', cwd=dest)
    functions = '\n'.join(shell_function(source, name) for name in
                          ('rollback_install_update', 'fail_verified_install'))
    script = ('set -e\ninfo() { :; }; warn() { :; }; fail() { exit 41; }; '
              'verify_skill_payload() { return 0; }; repair_and_verify_skill_payload() { :; }; '
              'verification_enabled() { return 0; }; fetch_release_checksums() { return 0; }; '
              'verify_checksums_in_dir() { (cd \"$INSTALL_DIR\" && sha256sum -c \"$CHECKSUM_FILE\" --quiet); }; '
              + sparse_assignment(source) + '\n' + functions + '\n'
              + 'update_repo() { : > \"$UPDATE_MARKER\"; '
              + ('return 1;' if outcome == 'failed-update' else 'git -C \"$INSTALL_DIR\" checkout --detach -q \"$NEW_HEAD\"; INSTALL_UPDATED=1;') + ' };\n'
              + branch)
    if outcome == 'disable-failed':
        script = script.replace('set -e\n', 'set -e\ngit() { if [ \"$3\" = sparse-checkout ] && [ \"$4\" = disable ]; then return 1; fi; command git \"$@\"; };\n', 1)
    env = dict(os.environ, INSTALL_DIR=str(dest), INSTALL_OLD_HEAD=old_head,
               INSTALL_UPDATED='0', NEW_HEAD=new_head, RELEASE_TAG='v1.0.0', TMP_DIR=str(tmp_path),
               CHECKSUM_FILE=str(remote / 'CHECKSUMS.sha256'), UPDATE_MARKER=str(tmp_path / 'update-attempted'))
    result = subprocess.run([install_bash(), '-c', script], env=env, capture_output=True, text=True, encoding="utf-8")
    assert (tmp_path / 'update-attempted').exists() == (outcome != 'disable-failed' or mode != 'legacy')
    expected = 0 if outcome == 'success' or (outcome == 'disable-failed' and mode != 'legacy') else 41
    assert result.returncode == expected, result.stderr + result.stdout
    assert _git('rev-parse', 'HEAD', cwd=dest).stdout.strip() == old_head
    assert database.read_bytes() == secret_fixture
    assert (dest / 'openclaw/skip.txt').exists()
    if outcome != 'disable-failed' or mode != 'legacy':
        assert (dest / 'install.sh').is_file()


def test_payload_repair_widens_without_deleting_ignored_extra_data(remote, tmp_path):
    from helpers.install_sparse import install_bash, shell_function, sparse_assignment
    import os
    dest = tmp_path / 'payload repair'
    _clone(remote, dest, ('sparse-checkout', 'init', '--cone'))
    _git('sparse-checkout', 'add', 'openclaw/', cwd=dest)
    database = dest / 'openclaw/store/private.db'
    database.parent.mkdir(parents=True)
    database.write_bytes(b'payload repair fixture')
    (dest / '.git/info/exclude').write_text('openclaw/store/\n')
    source = _src()
    script = ('set -e; warn() { :; }; info() { :; }; fail_verified_install() { exit 41; }; '
              'verify_skill_payload() { test -f "$INSTALL_DIR/install.sh" && '
              'test "$(git -C "$INSTALL_DIR" config --bool core.sparseCheckout)" != true; }; '
              + sparse_assignment(source) + '\n'
              + shell_function(source, 'repair_and_verify_skill_payload')
              + '\nrepair_and_verify_skill_payload')
    result = subprocess.run([install_bash(), '-c', script],
                            env=dict(os.environ, INSTALL_DIR=str(dest)), capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert database.read_bytes() == b'payload repair fixture'
    assert subprocess.run(['git', 'config', '--bool', 'core.sparseCheckout'], cwd=dest, capture_output=True, text=True, encoding="utf-8").stdout.strip() != 'true'


@pytest.mark.parametrize('edit', ['unstaged', 'staged', 'mixed', 'binary', 'deleted', 'mode'])
def test_failed_verification_rollback_preserves_untouched_tracked_edits(remote, tmp_path, edit):
    from helpers.install_sparse import install_bash, shell_function
    import os
    dest = tmp_path / 'rollback é install'
    _git('clone', remote.as_uri(), str(dest))
    local = dest / 'local.txt'
    local.write_bytes(b'original\n')
    _git('add', 'local.txt', cwd=dest)
    _git('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'local tracked file', cwd=dest)
    old_head = _git('rev-parse', 'HEAD', cwd=dest).stdout.strip()
    (dest / 'install.sh').write_bytes(b'unverified new release\n')
    _git('add', 'install.sh', cwd=dest)
    _git('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'new release', cwd=dest)
    new_head = _git('rev-parse', 'HEAD', cwd=dest).stdout.strip()
    _git('checkout', '--detach', old_head, cwd=dest)
    if edit == 'deleted':
        local.unlink()
    elif edit == 'mode':
        if os.name == 'nt':
            pytest.skip('POSIX executable-bit edit')
        _git('config', 'core.fileMode', 'true', cwd=dest)
        local.chmod(0o755)
    else:
        local.write_bytes(b'local\x00\xff' if edit == 'binary' else b'local staged\n')
    if edit in ('staged', 'mixed', 'deleted'):
        _git('add', '-A', 'local.txt', cwd=dest)
    if edit == 'mixed':
        local.write_bytes(b'local unstaged after staging\n')
    database = dest / 'openclaw/store/private.db'
    database.parent.mkdir(parents=True)
    database.write_bytes(b'ignored rollback fixture')
    (dest / '.git/info/exclude').write_text('openclaw/store/\n')
    before_status = _git('status', '--porcelain', '--untracked-files=no', cwd=dest).stdout
    before_index = _git('ls-files', '--stage', cwd=dest).stdout
    before_bytes = local.read_bytes() if local.exists() else None
    before_mode = local.stat().st_mode if local.exists() else None
    # The update can switch commits because the release leaves local.txt untouched.
    _git('checkout', '--detach', new_head, cwd=dest)
    assert _git('status', '--porcelain', '--untracked-files=no', cwd=dest).stdout == before_status
    source = _src()
    functions = '\n'.join(shell_function(source, name) for name in
                          ('rollback_install_update', 'fail_verified_install'))
    script = ('warn() { printf "%s\n" "$1"; }; info() { printf "%s\n" "$1"; }; '
              'fail() { exit 41; }; ' + functions + '\nif (cd \"$INSTALL_DIR\" && sha256sum -c \"$CHECKSUM_FILE\" --quiet); then exit 93; fi; fail_verified_install "checksum failed"')
    result = subprocess.run([install_bash(), '-c', script], capture_output=True, text=True, encoding="utf-8",
                            env=dict(os.environ, INSTALL_DIR=str(dest), INSTALL_OLD_HEAD=old_head,
                                     INSTALL_UPDATED='1', CHECKSUM_FILE=str(remote / 'CHECKSUMS.sha256')))
    assert result.returncode == 41, result.stderr
    assert _git('rev-parse', 'HEAD', cwd=dest).stdout.strip() == old_head
    assert _git('status', '--porcelain', '--untracked-files=no', cwd=dest).stdout == before_status
    # Compare the local file's index entry: the release's install.sh entry changes on rollback.
    assert [row for row in _git('ls-files', '--stage', cwd=dest).stdout.splitlines() if row.endswith('\tlocal.txt')] == [row for row in before_index.splitlines() if row.endswith('\tlocal.txt')]
    assert (local.read_bytes() if local.exists() else None) == before_bytes
    assert (local.stat().st_mode if local.exists() else None) == before_mode
    assert database.read_bytes() == b'ignored rollback fixture'


@pytest.mark.parametrize('failure', ['conflict-unstaged', 'conflict-staged', 'unknown-target',
                                     'not-updated', 'missing-target', 'non-git'])
def test_rollback_refusal_or_no_update_keeps_work_and_reports_recovery(remote, tmp_path, failure):
    from helpers.install_sparse import install_bash, shell_function
    import os
    dest = tmp_path / 'rollback recovery é path'
    _git('clone', remote.as_uri(), str(dest))
    old_head = _git('rev-parse', 'HEAD', cwd=dest).stdout.strip()
    changed = dest / 'install.sh'
    changed.write_bytes(b'new release\n')
    _git('add', 'install.sh', cwd=dest)
    _git('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'release', cwd=dest)
    new_head = _git('rev-parse', 'HEAD', cwd=dest).stdout.strip()
    changed.write_bytes(b'local edit after update\n')
    if failure == 'conflict-staged':
        _git('add', 'install.sh', cwd=dest)
    before_status = _git('status', '--porcelain', '--untracked-files=no', cwd=dest).stdout
    before_index = _git('ls-files', '--stage', cwd=dest).stdout
    if failure == 'non-git':
        (dest / '.git').rename(dest / 'saved-git')
    target = 'nonexistent-recovery-target' if failure == 'unknown-target' else old_head
    if failure == 'missing-target':
        target = ''
    script = ('warn() { printf "%s\n" "$1"; }; info() { printf "%s\n" "$1"; }; '
              + shell_function(_src(), 'rollback_install_update') + '\nrollback_install_update')
    result = subprocess.run([install_bash(), '-c', script], capture_output=True, text=True, encoding="utf-8",
                            env=dict(os.environ, INSTALL_DIR=str(dest), INSTALL_OLD_HEAD=target,
                                     INSTALL_UPDATED='0' if failure == 'not-updated' else '1'))
    assert result.returncode == 0, result.stderr
    assert changed.read_bytes() == b'local edit after update\n'
    if failure == 'non-git':
        (dest / 'saved-git').rename(dest / '.git')
    assert _git('rev-parse', 'HEAD', cwd=dest).stdout.strip() == new_head
    assert _git('status', '--porcelain', '--untracked-files=no', cwd=dest).stdout == before_status
    assert _git('ls-files', '--stage', cwd=dest).stdout == before_index
    if failure.startswith('conflict') or failure == 'unknown-target':
        assert 'Rollback succeeded' not in result.stdout
        assert 'Rollback refused or failed' in result.stdout
        assert 'After saving local work' in result.stdout
        assert f'git -C "{dest}" checkout --no-overwrite-ignore --detach "{target}"' in result.stdout
    else:
        assert result.stdout == ''


@pytest.mark.parametrize('collision', ['ignored-file', 'ignored-directory', 'ordinary-file'])
def test_rollback_refuses_to_overwrite_local_file_removed_by_release(remote, tmp_path, collision):
    from helpers.install_sparse import install_bash, shell_function
    import os
    dest = tmp_path / 'rollback ignored é collision'
    _git('clone', remote.as_uri(), str(dest))
    target = dest / 'user.db'
    target.write_bytes(b'old tracked release content')
    _git('add', 'user.db', cwd=dest)
    _git('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'old tracked database', cwd=dest)
    old_head = _git('rev-parse', 'HEAD', cwd=dest).stdout.strip()
    _git('rm', 'user.db', cwd=dest)
    _git('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'release removes database', cwd=dest)
    new_head = _git('rev-parse', 'HEAD', cwd=dest).stdout.strip()
    if collision == 'ignored-directory':
        target.mkdir()
        private = target / 'private.db'
    else:
        private = target
    private.write_bytes(b'private binary\x00\xff')
    if collision.startswith('ignored'):
        (dest / '.git/info/exclude').write_text('user.db\n')
        assert _git('check-ignore', str(private.relative_to(dest)), cwd=dest).returncode == 0
    before_index = _git('ls-files', '--stage', cwd=dest).stdout
    script = ('warn() { printf "%s\n" "$1"; }; info() { printf "%s\n" "$1"; }; '
              + shell_function(_src(), 'rollback_install_update') + '\nrollback_install_update')
    result = subprocess.run([install_bash(), '-c', script], capture_output=True, text=True, encoding="utf-8",
                            env=dict(os.environ, INSTALL_DIR=str(dest), INSTALL_OLD_HEAD=old_head,
                                     INSTALL_UPDATED='1'))
    assert result.returncode == 0, result.stderr
    assert _git('rev-parse', 'HEAD', cwd=dest).stdout.strip() == new_head
    assert _git('ls-files', '--stage', cwd=dest).stdout == before_index
    assert private.read_bytes() == b'private binary\x00\xff'
    assert 'Rollback succeeded' not in result.stdout
    assert 'Rollback refused or failed' in result.stdout
    assert 'After saving local work' in result.stdout



def test_recovery_command_preserves_unicode_with_legacy_parent_encoding(remote, tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, '_text_encoding', lambda: 'cp1252', raising=False)
    import locale
    monkeypatch.setattr(locale, 'getpreferredencoding', lambda do_setlocale=True: 'cp1252')
    test_rollback_refusal_or_no_update_keeps_work_and_reports_recovery(remote, tmp_path, 'unknown-target')


@pytest.mark.parametrize('scenario', [
    'legacy-exclude', 'legacy-gitignore', 'legacy-untracked',
    'rollback-unstaged', 'rollback-staged', 'rollback-mixed', 'rollback-binary', 'rollback-deleted',
    'recovery-conflict-unstaged', 'recovery-conflict-staged', 'recovery-unknown-target',
])
def test_preservation_fixtures_resist_inherited_crlf_and_legacy_encoding(remote, tmp_path, monkeypatch, scenario):
    # Reproduce the two independent Windows boundary conditions, without
    # changing the product or normalizing the data/status assertions.
    global_config = tmp_path / 'git-global-config'
    global_config.write_bytes(b'[core]\n\tautocrlf = true\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(global_config))
    monkeypatch.setattr(subprocess, '_text_encoding', lambda: 'cp1252', raising=False)
    import locale
    monkeypatch.setattr(locale, 'getpreferredencoding', lambda do_setlocale=True: 'cp1252')
    if scenario.startswith('legacy-'):
        test_existing_install_keeps_ignored_extra_folder_data(
            remote, tmp_path, 'legacy', 'success', scenario.removeprefix('legacy-'))
    elif scenario.startswith('rollback-'):
        test_failed_verification_rollback_preserves_untouched_tracked_edits(
            remote, tmp_path, scenario.removeprefix('rollback-'))
    else:
        test_rollback_refusal_or_no_update_keeps_work_and_reports_recovery(
            remote, tmp_path, scenario.removeprefix('recovery-'))


def _run_rollback(dest, old_head, old_branch):
    from helpers.install_sparse import install_bash, shell_function
    import os
    script = ('warn() { printf "%s\n" "$1"; }; info() { printf "%s\n" "$1"; }; '
              + shell_function(_src(), 'rollback_install_update') + '\nrollback_install_update')
    return subprocess.run([install_bash(), '-c', script], capture_output=True, text=True, encoding="utf-8",
                          env=dict(os.environ, INSTALL_DIR=str(dest), INSTALL_OLD_HEAD=old_head,
                                   INSTALL_OLD_BRANCH=old_branch, INSTALL_UPDATED='1'))


def _commit_release(remote):
    (remote / 'skills' / 'x.py').write_bytes(b"print('unverified release')\n")
    _git('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qam', 'unverified', cwd=remote)
    return _git('rev-parse', 'HEAD', cwd=remote).stdout.strip()


def test_skip_verify_rollback_restores_branch_so_next_pull_works(remote, tmp_path):
    """SKIP_VERIFY updates with `git pull --ff-only` on a branch. A rollback must
    leave that branch checked out at the old commit, keep ignored and untracked
    user files, and let the next update pull again."""
    dest = tmp_path / 'skip verify é install'
    _git('clone', remote.as_uri(), str(dest))
    old_head = _git('rev-parse', 'HEAD', cwd=dest).stdout.strip()
    (dest / '.git/info/exclude').write_text('local.db\n')
    (dest / 'local.db').write_bytes(b'private\x00db')
    (dest / 'notes.txt').write_bytes(b'untracked user note\n')
    new_head = _commit_release(remote)
    _git('pull', '--ff-only', '-q', cwd=dest)
    assert _git('rev-parse', 'HEAD', cwd=dest).stdout.strip() == new_head

    result = _run_rollback(dest, old_head, 'main')
    assert result.returncode == 0, result.stderr
    assert 'Rollback succeeded' in result.stdout
    assert _git('symbolic-ref', 'HEAD', cwd=dest).stdout.strip() == 'refs/heads/main'
    assert _git('rev-parse', 'main', cwd=dest).stdout.strip() == old_head
    assert (dest / 'skills' / 'x.py').read_bytes() == b"print('x')\n"
    assert (dest / 'local.db').read_bytes() == b'private\x00db'
    assert (dest / 'notes.txt').read_bytes() == b'untracked user note\n'
    assert _git('status', '--porcelain', '--untracked-files=no', cwd=dest).stdout == ''

    # The next SKIP_VERIFY update still has a branch to fast-forward.
    _git('pull', '--ff-only', '-q', cwd=dest)
    assert _git('rev-parse', 'HEAD', cwd=dest).stdout.strip() == new_head


def test_verified_rollback_reattaches_unmoved_branch(remote, tmp_path):
    """The verified path updates with a detached checkout, so the branch never
    moved: rollback re-attaches it without touching the ref."""
    dest = tmp_path / 'verified install'
    _git('clone', remote.as_uri(), str(dest))
    old_head = _git('rev-parse', 'HEAD', cwd=dest).stdout.strip()
    new_head = _commit_release(remote)
    _git('fetch', '-q', 'origin', cwd=dest)
    _git('checkout', '-q', '--detach', new_head, cwd=dest)

    result = _run_rollback(dest, old_head, 'main')
    assert result.returncode == 0, result.stderr
    assert _git('symbolic-ref', 'HEAD', cwd=dest).stdout.strip() == 'refs/heads/main'
    assert _git('rev-parse', 'main', cwd=dest).stdout.strip() == old_head


def test_rollback_never_moves_a_branch_it_did_not_move(remote, tmp_path):
    """A branch pointing somewhere the update did not put it is left alone and
    HEAD stays detached at the old commit (the pre-fix behaviour)."""
    dest = tmp_path / 'foreign branch'
    _git('clone', remote.as_uri(), str(dest))
    old_head = _git('rev-parse', 'HEAD', cwd=dest).stdout.strip()
    _git('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-q', '--allow-empty',
         '-m', 'user work on main', cwd=dest)
    user_tip = _git('rev-parse', 'main', cwd=dest).stdout.strip()
    new_head = _commit_release(remote)
    _git('fetch', '-q', 'origin', cwd=dest)
    _git('checkout', '-q', '--detach', new_head, cwd=dest)

    result = _run_rollback(dest, old_head, 'main')
    assert result.returncode == 0, result.stderr
    assert 'Rollback succeeded' in result.stdout
    assert _git('rev-parse', 'main', cwd=dest).stdout.strip() == user_tip
    assert _git('rev-parse', 'HEAD', cwd=dest).stdout.strip() == old_head
    detached = subprocess.run(['git', 'symbolic-ref', '-q', 'HEAD'], cwd=dest, capture_output=True)
    assert detached.returncode != 0
