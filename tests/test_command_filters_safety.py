#!/usr/bin/env python3
"""command-filter add-gate hardening + handler parity.

fix-7: _is_safe_add normalizes argv[0] with basename (so /bin/bash, ./python,
/usr/bin/sudo are caught), resolves a leading `env` (VAR=val + options), and
rejects bare/overly-broad wildcard patterns (just * or **). This is
defense-in-depth correctness (dangerous chars are blocked upstream), making the
gate deliver what its docstring claims.

fix-8: VALID_HANDLERS must equal the set of bash_compress._PATTERN_HANDLERS so a
future handler addition can't silently reject a user's valid config.

Run: python3 -m pytest tests/test_command_filters_safety.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import command_filters as cf  # noqa: E402


# ---------------------------------------------------------------------------
# fix-8: handler parity
# ---------------------------------------------------------------------------

def test_valid_handlers_matches_pattern_handlers():
    import bash_compress
    assert set(cf.VALID_HANDLERS) == set(bash_compress._PATTERN_HANDLERS), (
        "VALID_HANDLERS drifted from bash_compress._PATTERN_HANDLERS; a user "
        "config referencing a real handler would be silently rejected"
    )


# ---------------------------------------------------------------------------
# fix-7: interpreters / privilege escalation caught through path + env
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "/bin/bash -c ls",
    "./python script.py",
    "/usr/bin/python3 x.py",
    "/usr/bin/sudo ls",
    "/sbin/su - root",
    "env bash",
    "env PATH=/x python x.py",
    "env -i node app.js",
    "env -u HOME sudo ls",
    "env FOO=1 BAR=2 /bin/sh -c ls",
])
def test_unsafe_commands_rejected(command):
    assert cf._is_safe_add(command, "ls") is False, command


@pytest.mark.parametrize("command", [
    "cargo test",
    "ruff check",
    "env FOO=1 cargo test",
    "ls -la",
    "/usr/local/bin/rg pattern",   # ripgrep by absolute path is read-only
])
def test_safe_commands_accepted(command):
    # Use a handler the command plausibly maps to; the point is the gate does
    # NOT reject an otherwise read-only command.
    assert cf._is_safe_add(command, "ls") is True, command


# ---------------------------------------------------------------------------
# fix-7: bare / overly-broad wildcards rejected
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", ["*", "**", "***", "* *"])
def test_wildcard_only_patterns_rejected(command):
    assert cf._is_safe_add(command, "ls") is False, command


def test_wildcard_within_command_still_allowed():
    # A wildcard as an ARGUMENT (not the whole pattern / not argv[0]) is fine.
    assert cf._is_safe_add("ls *.py", "ls") is True


# ---------------------------------------------------------------------------
# fix-7: unknown handler and write subcommands still rejected
# ---------------------------------------------------------------------------

def test_unknown_handler_rejected():
    assert cf._is_safe_add("cargo test", "not_a_handler") is False


@pytest.mark.parametrize("command", ["rm -rf /tmp/x", "mv a b", "chmod 777 f"])
def test_write_subcommands_rejected(command):
    assert cf._is_safe_add(command, "ls") is False, command


# ---------------------------------------------------------------------------
# End-to-end: an unsafe add entry is dropped by the loader
# ---------------------------------------------------------------------------

def test_loader_drops_unsafe_add_entry(tmp_path, monkeypatch):
    toml = tmp_path / "command-filters.toml"
    toml.write_text(
        "[filters.add]\n"
        'sneaky = { command = "/bin/bash -c evil", handler = "ls" }\n'
        'good = { command = "cargo test", handler = "pytest" }\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(toml))
    cf.reset_cache()
    config = cf.load_filters()
    names = {a.name for a in config.adds}
    assert "good" in names
    assert "sneaky" not in names, "path-hidden interpreter must be gated out"



# ---------------------------------------------------------------------------
# Destructive-wildcard regression: `r*` must not match `rm -rf ...`
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "r*", "r* -rf", "?m -rf", "[r]m *", "*m", "rm*", "/bin/r*", "env r*",
    "env FOO=1 r*", "s*", "su*", "ba*", "py*", "k*", "m*", "ch*", "d*",
    "git r*", "git *", "git c*", "git -C x p*",
    "RM -rf t", "rm.exe x", "R*.exe", "r*.EXE", "GIT R*", "Git PUSH",
    "git commit", "git push",
])
def test_globs_that_can_match_destructive_commands_rejected(command):
    assert cf._is_safe_add(command, "disk_stats") is False, command


@pytest.mark.parametrize("command", [
    "ls *.py", "cargo test*", "git status *", "git log --oneline *",
    "du -sh *", "ruff *", "pytest tests/*",
])
def test_harmless_globs_still_accepted(command):
    assert cf._is_safe_add(command, "disk_stats") is True, command


@pytest.mark.parametrize("command", [
    "rm -rf target", "/bin/rm -rf target", "env rm -rf target",
    "FOO=1 rm -rf target", "sudo ls", "bash -c ls", "git rm -r x",
    "git -C x push", "git", "find . -delete", "find . -exec ls {}",
    "unterminated 'quote",
    "sed -i s/a/b/ f", "xargs ls", "git branch -D x", "ls > out.txt",
    "RM -rf t", "rm.exe -rf t", "C:\\Windows\\rm.EXE x", "Git.exe push",
    "SUDO ls",
])
def test_is_destructive_command_true(command):
    assert cf.is_destructive_command(command) is True, command


@pytest.mark.parametrize("command", [
    "ls -la", "cargo test", "git status", "du -sh .", "find . -name x",
])
def test_is_destructive_command_false(command):
    assert cf.is_destructive_command(command) is False, command


def test_git_write_set_covers_whitelist_set():
    import bash_whitelist
    assert set(bash_whitelist._GIT_WRITE_SUBCMDS) <= set(cf._GIT_WRITE_SUBCMDS)


def _glob_config(tmp_path, monkeypatch, command, handler="disk_stats"):
    toml = tmp_path / "command-filters.toml"
    toml.write_text(
        "[filters.add]\n"
        f'x = {{ command = "{command}", handler = "{handler}" }}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(toml))
    cf.reset_cache()


def test_loader_drops_wildcard_destructive_entry(tmp_path, monkeypatch):
    _glob_config(tmp_path, monkeypatch, "r*")
    assert cf.load_filters().adds == ()
    cf.reset_cache()


def test_matching_layer_never_returns_add_for_destructive(monkeypatch):
    # Even an entry that bypassed the loader gate (hand-built) cannot match.
    entry = cf.AddEntry(name="x", command="r*", handler="disk_stats")
    eff = cf.EffectiveFilters(user_adds=(entry,))
    assert eff.find_user_add("rm -rf target") is None
    assert eff.find_user_add("ruff check") is entry  # non-destructive glob hit
    cfg = cf.CommandFilterConfig(adds=(entry,))
    assert cfg.find_add("rm -rf target") is None


def test_rm_via_wildcard_not_compressed_end_to_end(tmp_path, monkeypatch):
    """Isolated fixture: the `r*` -> disk_stats entry must not compress or
    even run `rm -rf target`; the fixture files must survive."""
    import subprocess
    import bash_compress
    import bash_whitelist
    _glob_config(tmp_path, monkeypatch, "r*")
    target = tmp_path / "target"
    target.mkdir()
    for i in range(60):
        (target / f"f{i}.txt").write_text("x")
    cmd = f"rm -rf {target}"
    assert bash_whitelist.is_whitelisted(cmd) is False
    assert bash_compress._detect_pattern(cmd) is None
    # Defense in depth: even with a hand-built entry in the effective cache,
    # the wrapper refuses and nothing is deleted.
    cf._cached = cf.EffectiveFilters(
        user_adds=(cf.AddEntry(name="x", command="r*", handler="disk_stats"),)
    )
    try:
        assert bash_whitelist.is_whitelisted(cmd) is False
        assert bash_compress._detect_pattern(cmd) is None
    finally:
        cf.reset_cache()
    env = {**__import__("os").environ, cf.ENV_FILTERS_PATH: str(tmp_path / "command-filters.toml")}
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "bash_compress.py"), "rm", "-rf", str(target)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 1
    assert "not eligible for compression" in proc.stderr
    assert len(list(target.iterdir())) == 60


def test_destructive_verdict_follows_pipeline_analyzer():
    """The match-time guard refuses whatever the compress hook's read-only
    classifier refuses for a reason other than "unknown command"."""
    import pipeline_analyzer as pa
    for cmd in ["sed -i s/a/b/ f", "git branch -D x", "xargs rm", "find . -delete",
                "ls > out.txt", "rm.exe x"]:
        ok, reason = pa.is_read_only_pipeline(cmd)
        assert not ok
        assert cf.is_destructive_command(cmd) is True, (cmd, reason)
    assert cf.is_destructive_command("my-custom-runner --flag") is False


# ---------------------------------------------------------------------------
# Round: runners, writers, git option/alias injection, Windows spellings
# ---------------------------------------------------------------------------

_RUNNER_AND_WRITER_COMMANDS = [
    "timeout 5 rm -rf t", "nice rm x", "nice -n 5 rm x", "ionice -c3 rm x",
    "nohup rm x", "stdbuf -oL rm x", "time rm x", "chrt 1 rm x",
    "taskset 1 rm x", "watch rm x", "xargs rm", "parallel rm", "command rm x",
    "exec rm x", "setsid rm x", "flock f rm x", "busybox rm x",
    "env -- rm x", "env -S rm", "env -i -u A rm x",
    "tee out.txt", "truncate -s 0 f", "shred f", "touch f", "unlink f",
    "install a b", "rsync a b", "patch -p1", "sponge f",
    "cmd /c del x", "powershell -c x", "pwsh x", "wsl rm x",
    "git -c alias.st=!rm st", "git -c core.pager=rm log", "git st",
    "git --exec-path=/x status", "git -C x -c alias.l=!rm l",
]


@pytest.mark.parametrize("command", _RUNNER_AND_WRITER_COMMANDS)
def test_runner_writer_git_injection_is_destructive(command):
    assert cf.is_destructive_command(command) is True, command
    entry = cf.AddEntry(name="x", command=command, handler="ls")
    assert cf.EffectiveFilters(user_adds=(entry,)).find_user_add(command) is None
    assert cf.CommandFilterConfig(adds=(entry,)).find_add(command) is None


def test_git_config_injection_refused_at_token_level():
    """The `-c alias.x=!cmd` refusal lives in _tokens_destructive itself, not
    only in the pipeline-analyzer backstop: this pins the token-level
    behavior directly so a shadowed/analyzer-less path still refuses."""
    bad = [
        "git -c alias.st=!rm st", "git -c core.pager=rm log",
        "git --exec-path=/x status", "git -C x -c alias.l=!rm l",
        "git --config-env=alias.x=CMD x",
    ]
    for cmd in bad:
        assert cf._tokens_destructive(cmd.split()) is True, cmd
    # Allowed global options still pass the token-level git check.
    assert cf._tokens_destructive("git -C x status".split()) is False
    assert cf._tokens_destructive("git --no-pager log".split()) is False
    assert cf._tokens_destructive("git --no-optional-locks status".split()) is False
    # The pipeline-analyzer backstop still refuses compression for ANY git
    # global option (it gates "git-forbidden-subcmd:-C"), matching the
    # compress hook's own approval behavior; only the token-level check
    # distinguishes benign options from config/exec injection.
    assert cf.is_destructive_command("git -C x status") is True


def test_token_level_denylists_hold_without_pipeline_analyzer(monkeypatch):
    """If the pipeline_analyzer import ever fails, the denylist checks in
    _tokens_destructive must still refuse (the analyzer stage is only a
    backstop for writers outside the bounded denylists)."""
    import builtins
    real_import = builtins.__import__
    def no_analyzer(name, *args, **kwargs):
        if name == "pipeline_analyzer":
            raise ImportError("simulated missing analyzer")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", no_analyzer)
    for cmd in ["rm -rf target", "git -c alias.st=!rm st", "git push",
                "timeout 5 rm -rf t", "tee out.txt", "rm.exe x",
                "find . -delete"]:
        assert cf.is_destructive_command(cmd) is True, cmd
    # Unknown custom commands fail open (stay opt-inable), as documented.
    assert cf.is_destructive_command("my-custom-runner --flag") is False


@pytest.mark.parametrize("command", [
    "timeout *", "t*", "ti*", "n*", "x*", "tee*", "time*", "ni*", "w*", "sh*",
    "rm.*", "r*.exe", "r*.EXE", "RM.*", "[r]m.exe", "C:\\bin\\r*", "C:\\bin\\rm.*",
    "/usr/bin/r*", "tou*", "trunc*", "env t*", "env FOO=1 timeout*",
    "timeout 5 rm", "tee out", "nice rm", "git -c alias.st=!rm st",
    "git -C x -c alias.l=!rm l", "git --exec-path=/x s*", "cmd /c*",
])
def test_gate_rejects_runner_writer_and_windows_globs(command):
    assert cf._is_safe_add(command, "disk_stats") is False, command


@pytest.mark.parametrize("command", [
    "ls", "ls -la", "/bin/ls -la", "du -sh *", "df -h", "wc -l *", "cargo test",
    "ruff check", "pytest tests/*", "git status", "git log --oneline -5",
    "find . -name x", "rg foo", "tail -n 5 f", "my-custom-runner",
])
def test_harmless_commands_stay_eligible(command):
    assert cf.is_destructive_command(command) is False, command
    assert cf._is_safe_add(command, "ls") is True, command
    entry = cf.AddEntry(name="x", command=command, handler="ls")
    assert cf.EffectiveFilters(user_adds=(entry,)).find_user_add(command) is entry


def test_windows_spellings_never_match_even_via_backslash_paths():
    for cmd in ["C:\\Windows\\rm.EXE x", "RM.EXE x", "Rm x", ".\\rm.exe x", "TIMEOUT.EXE 5 rm x"]:
        assert cf.is_destructive_command(cmd) is True, cmd
