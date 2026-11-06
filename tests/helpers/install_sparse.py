"""Execute the installer's own sparse commands without reparsing shell argv."""
import re
import os
from pathlib import Path
import subprocess
import shutil


def install_bash():
    """Match the install-sh test runtime, never Windows' WSL launcher."""
    if os.name != 'nt':
        return shutil.which('bash') or 'bash'
    for candidate in (
        os.environ.get('GIT_BASH'),
        r'C:\Program Files\Git\bin\bash.exe',
        r'C:\Program Files\Git\usr\bin\bash.exe',
        r'C:\Program Files (x86)\Git\bin\bash.exe',
    ):
        if candidate and 'system32' not in candidate.lower() and Path(candidate).is_file():
            return candidate
    candidate = shutil.which('bash')
    if candidate and 'system32' not in candidate.lower():
        return candidate
    raise RuntimeError('Git Bash is required for sparse installer probes; WSL bash.exe is not supported')


def sparse_assignment(source):
    return re.search(r'^SPARSE_CONE_DIRS=.*$', source, re.M).group(0)


def shell_function(source, name):
    start = source.index(name + '() {')
    end = source.index('\n}', start) + 2
    return source[start:end]


def sparse_set_commands(source):
    # Join shell continuations only; Bash still interprets expansions and
    # redirections. Never turn a redirection such as 2>/dev/null into a git arg.
    logical = source.replace('\\\n', ' ')
    return re.findall(r'^\s*git -C .*? sparse-checkout set[^\n]*', logical, re.M)


def assert_sparse_paths(script):
    commands = sparse_set_commands(script)
    assert len(commands) == 2, "only disposable fresh clones may narrow the checkout"
    # Execute every real command with a recording git function. Bash, rather
    # than a string splitter, expands the shared list and handles redirections.
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        log = Path(work) / 'argv'
        env = dict(os.environ, ARG_LOG=str(log), INSTALL_DIR=work, candidate_dir=work, clone_log=str(Path(work)/'clone.log'))
        probe = ('git() { printf "%s\\0" "$@" >> "$ARG_LOG"; printf "\n" >> "$ARG_LOG"; };\n'
                 + sparse_assignment(script) + '\n' + '\n'.join(commands))
        run = subprocess.run([install_bash(), '-c', 'set -ex\n: > "$ARG_LOG"\n: > "$clone_log"\n'
                              'test -d "$INSTALL_DIR"\ntest -d "$candidate_dir"\n' + probe], env=env,
                             cwd=work, capture_output=True, text=True)
        if run.returncode:
            # This is infrastructure failure, never a caught missing-path
            # mutation. Preserve stderr instead of CalledProcessError hiding it.
            raise RuntimeError(f"sparse probe failed ({run.returncode})\n"
                               f"stdout: {run.stdout}\nstderr: {run.stderr}")
        calls = [row.split(b'\0')[:-1] for row in log.read_bytes().splitlines()]
    assert len(calls) == 2
    for args in calls:
        assert args[:1] == [b'-C'] and args[2:4] == [b'sparse-checkout', b'set']
        assert args[4:] == [b'skills/', b'hooks/', b'devin/', b'.claude-plugin/', b'.codex-plugin/', b'.codex/']


def verify_sparse_clone(source, repo, manifest, tmp_path, mode):
    functions=tmp_path/'verifier-functions.sh'
    names = ['verify_checksum_manifest_coverage', 'verify_checksums_in_dir',
             'verify_release_candidate_before_live_update', 'clone_repo']
    functions.write_bytes(('warn() { printf "%s\\n" "$1"; }\nverification_enabled() { return 0; }\n'
        + sparse_assignment(source) + '\n' + '\n'.join(shell_function(source, name) for name in names)).encode())
    target=tmp_path/'fresh clone'
    env=dict(os.environ, CHECKSUM_FILE=str(manifest), TMP_DIR=str(tmp_path), RELEASE_TAG='v1.0.0',
             REPO_HTTPS=repo.as_uri(), INSTALL_DIR=str(target), GIT_CONFIG_COUNT='1',
             GIT_CONFIG_KEY_0='core.autocrlf', GIT_CONFIG_VALUE_0='false')
    action = 'clone_repo; verify_checksums_in_dir "$INSTALL_DIR"' if mode == 'fresh' else 'verify_release_candidate_before_live_update'
    run=subprocess.run([install_bash(),'-e','-c','source "$1"; '+action,'test',str(functions)],env=env,capture_output=True)
    assert run.returncode==0,run.stderr
    checked = target if mode == 'fresh' else tmp_path/'release-candidate'
    assert (checked/'install.sh').is_file() and (checked/'devin/SKILL.md').is_file()
    return checked
