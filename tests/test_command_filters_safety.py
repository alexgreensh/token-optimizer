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
    # Versioned interpreters, the Windows py launcher, and git --output.
    "python3.12 x.py", "python3.1* *", "python3.*", "pypy3 x.py", "pypy*",
    "py -3 x.py", "PY.EXE x", "python3.12.exe x",
    "git diff --output=x", "git log --output x",
])
def test_globs_that_can_match_destructive_commands_rejected(command):
    assert cf._is_safe_add(command, "disk_stats") is False, command


@pytest.mark.parametrize("command", [
    "ls *.py", "cargo test*", "git status *", "git log --oneline *",
    "du -sh *", "ruff *", "pytest tests/*",
    "pyright *", "pytest*", "git diff *", "git diff --stat",
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
    "python3.12 -c 1", "python3 -c 1", "pypy3 x.py", "pypy x.py", "py -3 x.py",
    "PY.EXE x", "C:\\Python312\\python3.12.exe x", "env python3.13 x",
    "git diff --output=/tmp/x", "git log --output /tmp/x",
    "git -C repo show --output=f HEAD",
])
def test_is_destructive_command_true(command):
    assert cf.is_destructive_command(command) is True, command


@pytest.mark.parametrize("command", [
    "ls -la", "cargo test", "git status", "du -sh .", "find . -name x",
    "pytest -q", "pyright src", "git diff --stat", "git log --oneline",
    "git diff -- output.txt",
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


@pytest.mark.skipif(sys.version_info < (3, 11),
                    reason="tomllib absent pre-3.11 (pre-existing, #312 scope)")
@pytest.mark.parametrize("entry_cmd,argv", [
    ("2*", ["2>/dev/null", "rm"]),
    ("2*", ["2>&1", "rm"]),
    ("1*", ["1>&2", "rm"]),
    ("0*", ["0<", "/dev/null", "rm"]),
    ("<*", ["<", "/dev/null", "rm"]),
    ("ls *", ["ls", "2>&1", "3>&2", "rm"]),
])
def test_redirect_prefix_camouflage_not_compressed_end_to_end(tmp_path, monkeypatch, entry_cmd, argv):
    """HOLD 4: redirect-prefixed commands hiding a writer are refused end to
    end - the TOML-loaded glob entry matches the prefix by fnmatch, but the
    match-time guard fails closed before anything executes. The fixture
    (harmless /dev/null operand, isolated target dir) must survive."""
    import subprocess
    import bash_compress
    import bash_whitelist
    _glob_config(tmp_path, monkeypatch, entry_cmd)
    target = tmp_path / "target"
    target.mkdir()
    for i in range(60):
        (target / f"f{i}.txt").write_text("x")
    env = {**__import__("os").environ, cf.ENV_FILTERS_PATH: str(tmp_path / "command-filters.toml")}
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "bash_compress.py"), *argv, str(target)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 1, (argv, proc.stdout, proc.stderr)
    assert "not eligible for compression" in proc.stderr
    assert len(list(target.iterdir())) == 60


def test_destructive_verdict_is_self_contained():
    """The match-time guard refuses the destructive classes the hook's
    read-only classifier would refuse - but WITHOUT importing it: this
    module ships in the Devin payload closed set, where pipeline_analyzer
    is not allowed (test_v3_repo_name_policy enforces that)."""
    import inspect
    module_src = inspect.getsource(cf)
    assert "import pipeline_analyzer" not in module_src
    assert "from pipeline_analyzer" not in module_src
    for cmd in ["sed -i s/a/b/ f", "git branch -D x", "xargs rm", "find . -delete",
                "ls > out.txt", "echo hi >> f", "ls | tee out.txt", "rm.exe x",
                "git st", "git config user.name x", "echo $(rm -rf x)",
                "echo `rm -rf x`",
                # Adversarial parity cases (previously only caught via the
                # pipeline-analyzer backstop; now self-contained):
                "cat; rm -rf x", "cat;rm -rf x", "ls|rm -rf x",
                "mytool --flag\nrm -rf x", "cat <(rm -rf x)",
                "echo a > b | cat", "FOO=1 ls && rm x",
                "if true; then rm x; fi", "for f in *; do rm $f; done",
                "while read l; do rm $l; done < files",
                "until false; do rm x; done", "{ rm x; }", "(rm x)",
                "case x in x) rm x;; esac",
                "ls |& rm x", "ls|&rm x", "f(){ :;};rm x", "f(){ rm x;}",
                "echo hi;;rm x", "echo hi;& rm x", "echo a &> f", "echo a |& tee f",
                # redirect-prefix camouflage (judge HOLD 4): every redirect
                # token fails closed, the hidden writer is never reached
                "< /etc/passwd rm x", "0< /etc/passwd rm x",
                "2>&1 rm x", "1>&2 rm x", "2>&1 sudo rm x",
                "2>/dev/null rm x", "2>>/dev/null rm x", "ls 2>&1 3>&2 rm x"]:
        assert cf.is_destructive_command(cmd) is True, cmd
    # Read-only git, read-only pipelines and unknown custom commands stay
    # eligible for user adds.
    for cmd in ["git status", "git log --oneline -3", "ls | head -5",
                "git log --oneline | head", "echo a | grep a",
                "cat f | sort | uniq", "my-custom-runner --flag"]:
        assert cf.is_destructive_command(cmd) is False, cmd
    # Redirects and fd dups fail closed (conservative; the old module's
    # pipeline_analyzer backstop allowed some of these - no exception here
    # can hide a writer).
    for cmd in ["ls 2>&1 | head", "cat < /dev/null", "grep x f 2>/dev/null"]:
        assert cf.is_destructive_command(cmd) is True, cmd


@pytest.mark.skipif(sys.version_info < (3, 11),
                    reason="tomllib absent pre-3.11 (pre-existing, #312 scope)")
@pytest.mark.parametrize("entry_cmd,attack", [
    ("cat*", "cat; rm -rf x"),
    ("cat*", "cat <(rm -rf x)"),
    ("mytool*", "mytool --flag\nrm -rf x"),
    ("ls *", "ls | tee out.txt"),
    ("git status *", "git status $(rm -rf x)"),
    ("cat*", "{ rm x; }"),
    ("cat*", "(rm x)"),
    ("ls *", "ls |& rm x"),
    ("ls *", "ls|&rm x"),
    ("echo*", "echo hi;;rm x"),
    # redirect-prefix camouflage (judge HOLD 4), entries with valid handlers
    ("2*", "2>&1 rm x"),
    ("1*", "1>&2 rm x"),
    ("2*", "2>&1 sudo rm x"),
    ("2*", "2>/dev/null rm x"),
    ("2*", "2>>/dev/null rm x"),
    ("0*", "0< /etc/passwd rm x"),
    ("ls *", "ls 2>&1 3>&2 rm x"),
    ("ls *", "ls |& rm -rf /"),
])
def test_toml_entry_never_matches_destructive_command(tmp_path, monkeypatch, entry_cmd, attack):
    """Loader-accepted glob entry must never match a destructive executed
    command, even via glued operators, newlines or substitution."""
    _glob_config(tmp_path, monkeypatch, entry_cmd)
    config = cf.load_filters()
    assert len(config.adds) == 1, "the benign glob entry itself must load"
    eff = cf.EffectiveFilters(user_adds=config.adds)
    assert eff.find_user_add(attack) is None, attack
    assert config.find_add(attack) is None, attack
    cf.reset_cache()


@pytest.mark.parametrize("command", [
    "if*", "if true; then rm x; fi", "for*", "while*", "until*", "[{] rm *",
    "{ rm x; }", "case*", "do*",
])
def test_gate_rejects_shell_keyword_entries(command):
    assert cf._is_safe_add(command, "disk_stats") is False, command


def test_gated_entry_still_refused_if_hand_built():
    """Gate-refused entries must not match destructive commands even when
    hand-built past the loader."""
    for entry_cmd, attack in [
        ("r*", "rm -rf target"),
        ("if*", "if true; then rm x; fi"),
        ("[{] rm *", "{ rm x; }"),
    ]:
        entry = cf.AddEntry(name="x", command=entry_cmd, handler="disk_stats")
        assert cf.EffectiveFilters(user_adds=(entry,)).find_user_add(attack) is None, attack
        assert cf.CommandFilterConfig(adds=(entry,)).find_add(attack) is None, attack


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
    assert cf.is_destructive_command("git -C x status") is False


def test_token_level_denylists_are_self_sufficient():
    """The denylist checks refuse the known destructive classes on their
    own; unknown custom commands fail open (stay opt-inable)."""
    for cmd in ["rm -rf target", "git -c alias.st=!rm st", "git push",
                "timeout 5 rm -rf t", "tee out.txt", "rm.exe x",
                "find . -delete", "sed -i s/a/b/ f", "ls > out.txt"]:
        assert cf.is_destructive_command(cmd) is True, cmd
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


# ---------------------------------------------------------------------------
# Round: env unwrap fails closed (nested env, unrecognized option forms, -S)
# ---------------------------------------------------------------------------

_ENV_UNWRAP_BYPASSES = [
    # Nested env: the unwrap must loop, not stop at the second `env`.
    "env env rm -rf x", "env env env rm -rf x",
    "env A=1 env rm x", "A=1 env env rm x",
    # `--opt=val` and joined short-option forms the old unwrapper did not
    # model: it broke at the option token and read IT as the command.
    "env --unset=FOO rm -rf x", "env --chdir=/tmp rm -rf x",
    "env -uFOO rm -rf x", "env -C/tmp rm -rf x", "env -Srm -rf x",
    # `-S`/`--split-string`: the operand is itself a command string, so the
    # real command hides inside what looks like an option argument.
    "env -S rm -rf x", "env --split-string rm -rf x",
    "env -S 'rm -rf x'", "env -i -S rm -rf x",
    # `--` as an option operand: the blanket `--` strip used to delete it
    # before the unwrapper ran, so `-u` swallowed the real command word.
    # Real env consumes `--` literally as `-u`'s operand and runs `rm`.
    "env -u -- rm x", "env --unset -- rm x", "env -i -u -- rm x",
    "env env -u -- rm x", "env A=1 -u -- rm x",
    "env -u -- mv a b", "env -u -- cp a b", "env -u -- chmod +x f",
    "env -- rm x", "env -- env rm x", "env -u --",
]


@pytest.mark.parametrize("command", _ENV_UNWRAP_BYPASSES)
def test_env_unwrap_bypasses_are_destructive(command):
    assert cf.is_destructive_command(command) is True, command
    entry = cf.AddEntry(name="x", command="env*", handler="ls")
    assert cf.EffectiveFilters(user_adds=(entry,)).find_user_add(command) is None
    assert cf.CommandFilterConfig(adds=(entry,)).find_add(command) is None


@pytest.mark.parametrize("command", [
    "env FOO=1 mytool --json", "env -i mytool", "env -u FOO mytool",
    "env --unset FOO mytool", "env -C /tmp mytool", "env --chdir /tmp mytool",
    "env MY_FLAG=1 ./tool --flag", "env env FOO=1 mytool",
    # ``--`` as env's end-of-options marker resolves to the real command.
    "env -- mytool", "env -u FOO -- mytool", "env FOO=1 -- mytool",
])
def test_env_unwrap_recognised_forms_still_resolve(command):
    """Recognized env grammar still unwraps to the real (benign) command."""
    assert cf.is_destructive_command(command) is False, command


@pytest.mark.parametrize("command", [
    "osascript -e 'do shell script \"rm x\"'",
    "php -r 'system(\"rm x\")'", "gawk 'BEGIN{system(\"rm x\")}'",
    "mawk 'BEGIN{system(\"rm x\")}'", "nawk 'BEGIN{system(\"rm x\")}'",
    "Rscript -e 'system(\"rm x\")'",
])
def test_added_interpreters_are_destructive(command):
    assert cf.is_destructive_command(command) is True, command


def test_env_unwrap_end_to_end_not_whitelisted(tmp_path, monkeypatch):
    """A TOML-loaded `env*` add must not make env-wrapped destructive
    commands eligible: the wrapper's own whitelist re-check must refuse."""
    import bash_whitelist
    _glob_config(tmp_path, monkeypatch, "env*")
    config = cf.load_filters()
    assert len(config.adds) == 1, "the benign env* entry itself must load"
    for cmd in _ENV_UNWRAP_BYPASSES:
        assert bash_whitelist.is_whitelisted(cmd) is False, cmd
    cf.reset_cache()


# ---------------------------------------------------------------------------
# Hot path: zero add entries skips the tokenizing destructive check entirely,
# and with entries present the verdict is exactly what it was before.
# ---------------------------------------------------------------------------

def _count_destructive_calls(monkeypatch):
    calls = []
    real = cf.is_destructive_command

    def spy(command_str):
        calls.append(command_str)
        return real(command_str)

    monkeypatch.setattr(cf, "is_destructive_command", spy)
    return calls


@pytest.mark.parametrize("command", ["ls -la", "rm -rf target", "cat x | tee out"])
def test_no_add_entries_skips_destructive_check(monkeypatch, command):
    calls = _count_destructive_calls(monkeypatch)
    assert cf.EffectiveFilters().find_user_add(command) is None
    assert cf.CommandFilterConfig().find_add(command) is None
    assert calls == []


@pytest.mark.parametrize("command,expected", [
    ("ls -la", "lsall"),
    ("rm -rf target", None),
    ("ls -la > out", None),
])
def test_add_entries_keep_destructive_verdict(monkeypatch, command, expected):
    calls = _count_destructive_calls(monkeypatch)
    entry = cf.AddEntry(name="lsall", command="*", handler="disk_stats")
    for found in (cf.EffectiveFilters(user_adds=(entry,)).find_user_add(command),
                  cf.CommandFilterConfig(adds=(entry,)).find_add(command)):
        assert (found.name if found else None) == expected, command
    assert calls == [command, command]


# ---------------------------------------------------------------------------
# Interpreter spellings with patch versions, ABI suffixes (t = free-threaded,
# m = pymalloc) and PyPy release tags; their -config helpers and py* tools
# that are not interpreters stay allowed.
# ---------------------------------------------------------------------------

_INTERPRETER_SPELLINGS = [
    "python3.12.1", "python3.13t", "python3.12m", "python3.14td", "pypy3.10-v7.3",
    "pypy3.10-v7.3.17", "pypy3.10", "PYTHON3.13T.EXE",
]
_NOT_INTERPRETERS = ["python3-config", "python3.12-config", "pyright", "pytest", "pyenv"]


@pytest.mark.parametrize("name", _INTERPRETER_SPELLINGS)
def test_interpreter_spellings_are_destructive(name):
    assert cf.is_destructive_command(f"{name} -c 1") is True, name


@pytest.mark.parametrize("name", _INTERPRETER_SPELLINGS)
def test_interpreter_spellings_rejected_as_add(name):
    assert cf._is_safe_add(f"{name} *", "disk_stats") is False, name


@pytest.mark.parametrize("command", [
    "python3.13? *", "python3.12? x", "pypy3.1? *", "pypy3.1* *", "python3.1?? *",
])
def test_interpreter_globs_with_suffixes_rejected(command):
    assert cf._is_safe_add(command, "disk_stats") is False, command


@pytest.mark.parametrize("name", _NOT_INTERPRETERS)
def test_non_interpreter_py_tools_still_allowed(name):
    assert cf.is_destructive_command(f"{name} --version") is False, name
    assert cf._is_safe_add(f"{name} *", "disk_stats") is True, name
