#!/usr/bin/env python3
"""Token Optimizer v5.12: Pipeline-aware read-only safety classifier.

Used by the PostToolUse bash_compress_hook.py to decide whether a pipeline
or metachar-containing command is safe to compress. Unlike the PreToolUse
bash_hook.py (which categorically rejects metachar commands), this module
SPLITS the command into pipeline stages and checks EACH stage individually
against a consolidated read-only whitelist.

A pipeline is eligible for compression ONLY when EVERY stage is a known
read-only command. Any unrecognized, side-effecting, or unparseable stage
causes the whole pipeline to be rejected (fail-closed: pass through raw).

SECURITY (post-adversarial-review hardening):
- Raw command string is screened for injection constructs BEFORE tokenization:
  newlines, $(), backticks, <(), >(), and bare & (async/background).
- Every token is checked for glued operators (a|rm passes shlex but
  contains | as a substring). Any token containing | ; && inside it is
  rejected unless it IS exactly that separator.
- The read-only whitelist is narrowed to genuinely read-only commands.
  env, command, xargs, tee, awk, aws, gcloud, az removed.
- Cloud CLIs require explicit read-only subcommand allow-list.
- git uses an explicit ALLOW-list (not deny-list).
- git branch rejects delete (-d/-D/--delete), rename (-m/-M/--move),
  copy (-c/-C/--copy) and --force/-f flags.
- find rejects -delete/-exec/-ok/-fprint/-fls.
- sqlite3 rejects replace, vacuum, .import, .restore, pragma.
- npm run / terraform state / git config / kubectl config / docker pull
  removed from compound whitelist.
- python -m only allowed for known read-only modules (pytest, unittest,
  json.tool — NOT pip).

The consolidated whitelist merges bash_hook._WHITELIST_SINGLE,
bash_hook._WHITELIST_COMPOUND, and additional pipeline-consumer commands.

Security invariants:
- Read-only only: no write, no side-effect, no interpreter
- Fail-closed default: unknown command → not read-only → pass through raw
- No shell=True, no command reconstruction, no re-execution
"""

from __future__ import annotations

import re
import shlex

# ---------------------------------------------------------------------------
# Pre-tokenization injection screen: reject raw command strings containing
# constructs that shlex.split() is blind to.
# ---------------------------------------------------------------------------

# Newlines in commands indicate injection (ls -la\nrm -rf build).
_DANGEROUS_RAW_CHARS = re.compile(r"[\n\r]")

# Command substitution: $(...) and backticks.
_DANGEROUS_SUBSTITUTION = re.compile(r"\$\(|`")

# Process substitution: <(...) and >(...).
_DANGEROUS_PROCESS_SUBST = re.compile(r"[<>]\(")

# ---------------------------------------------------------------------------
# Pipeline separators (tokens that split one command into multiple stages).
# ---------------------------------------------------------------------------
_PIPELINE_SEPARATORS = frozenset({"|", "|&", "&&", "||", ";"})

# Glued-operator check: any token containing these as substrings (but is not
# exactly one of them) indicates a glued operator like a|b or cat;rm.
_GLUED_OPERATOR_CHARS = frozenset({"|", ";"})

# ---------------------------------------------------------------------------
# Redirection tokens and handlers.
# ---------------------------------------------------------------------------
_REDIRECT_TOKENS = frozenset({">", ">>", "<", "<<", "<>", ">&", "&>"})


def _is_redirect_token(tok: str) -> bool:
    """True if token is a shell redirection operator."""
    if tok in _REDIRECT_TOKENS:
        return True
    if any(c.isdigit() for c in tok[:1]) and any(c in tok for c in (">", "<")):
        i = 0
        while i < len(tok) and tok[i].isdigit():
            i += 1
        rest = tok[i:]
        if rest and rest[0] in (">", "<", "&"):
            return True
        if rest.startswith(">") or rest.startswith("<"):
            return True
    return False


# Redirect targets that are safe to strip (not real file writes).
# /dev/null, /dev/stderr, /dev/stdout — these are sinks, not file writes.
# Bare file descriptors (2>&1, 1>&2) are self-contained.
_SAFE_REDIRECT_TARGETS = frozenset({"/dev/null", "/dev/stderr", "/dev/stdout", "/dev/fd/1", "/dev/fd/2"})


def _redirect_has_file_target(tok: str) -> bool:
    """True if this redirect token expects a filename target."""
    if tok in (">", ">>", "<", "<<", "<>"):
        return True
    if any(c.isdigit() for c in tok[:1]):
        i = 0
        while i < len(tok) and tok[i].isdigit():
            i += 1
        rest = tok[i:]
        if rest.startswith(">&"):
            return False
        if rest == ">&":
            return False
        if rest.startswith(">") or rest.startswith("<"):
            return True
    if tok in ("&>", "&>>"):
        return True
    return False


def _redirect_target_is_safe(target: str) -> bool:
    """True if the redirect target is safe (/dev/null, /dev/stderr) not a real file.

    A redirect like ``>/dev/null`` or ``2>&1`` is harmless (sink or fd-duplication).
    A redirect like ``>/etc/hosts`` or ``>>file.log`` is a file WRITE and makes
    the command NOT read-only."""
    if not target:
        return True  # self-contained redirect like 2>&1 has no target
    if target in _SAFE_REDIRECT_TARGETS:
        return True
    return False


def _strip_redirections(tokens: list[str]) -> tuple[list[str], bool]:
    """Remove safe redirection tokens from a stage.

    Returns (cleaned_tokens, has_file_redirect) where has_file_redirect is
    True when ANY redirect in the stage targets a real file path (a write).
    Callers must reject the stage when has_file_redirect is True.
    """
    clean: list[str] = []
    has_file_redirect = False
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _is_redirect_token(tok):
            i += 1
            if _redirect_has_file_target(tok):
                if i < len(tokens) and not _is_redirect_token(tokens[i]):
                    target = tokens[i]
                    if _redirect_target_is_safe(target):
                        i += 1  # skip safe target
                    else:
                        # Redirect to a real file = WRITE → reject
                        has_file_redirect = True
                        i += 1  # still skip (avoids treating /etc/hosts as an arg)
                # else: no target token (redirect at end of command) — reject
                else:
                    has_file_redirect = True
            continue
        clean.append(tok)
        i += 1
    return clean, has_file_redirect


# ---------------------------------------------------------------------------
# Glued-operator detection: any token that contains | or ; as a substring
# but is NOT exactly that separator indicates a glued operator attack.
# "a|rm" passes shlex.split() as a single token and would otherwise be
# treated as a command name.
# ---------------------------------------------------------------------------

def _token_has_glued_operator(tok: str) -> bool:
    """True if token contains | or ; as a glued-operator attack.

    A glued operator is | or ; appearing INSIDE a longer token that is
    NOT the standalone separator. Examples: a|rm, cat;evil.

    Excludes:
      - Tokens that ARE the separator (|, ;, |&, &&, ||).
      - Tokens where | is inside [... or (... (character classes,
        tr patterns, grep regex alternations) — these are not pipes.
    """
    # The token IS the separator — not a glued attack.
    if tok in _PIPELINE_SEPARATORS or tok == ";":
        return False
    for ch in _GLUED_OPERATOR_CHARS:
        if ch in tok:
            # | inside [...] (character class) or (... ) (regex alternation
            # like grep -E '(passing|failing)') is not a pipe operator.
            if ch == "|":
                paren_open = None
                paren_close = None
                # Try brackets first.
                if "[" in tok and "]" in tok:
                    paren_open = tok.index("[")
                    paren_close = tok.rindex("]")
                # Then parentheses (but NOT $( — command sub screened earlier).
                elif "(" in tok and ")" in tok:
                    paren_open = tok.index("(")
                    paren_close = tok.rindex(")")
                if paren_open is not None and paren_close is not None:
                    pipe_pos = tok.index(ch)
                    if paren_open < pipe_pos < paren_close:
                        continue  # | is inside grouping, not a pipe
            return True
    return False


def _split_stages(tokens: list[str]) -> list[list[str]]:
    """Split tokens into pipeline stages on separators (|, &&, ||, ;)."""
    stages: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _PIPELINE_SEPARATORS:
            if current:
                stages.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        stages.append(current)
    return stages


# ============================================================================
# Consolidated read-only command whitelist.
#
# SOURCE OF TRUTH for the base list: bash_hook._WHITELIST_SINGLE and
# bash_hook._WHITELIST_COMPOUND. When those lists are updated, this list
# should be updated too. Pipeline-consumer additions are marked with [P].
#
# POST-AUDIT HARDENING:
#   REMOVED from _READ_ONLY_SINGLE: env, command, xargs, tee, awk,
#     aws, gcloud, az (too broad / bypassable).
#   REMOVED from _READ_ONLY_COMPOUND: npm run, terraform state, git config,
#     kubectl config, docker pull, python -m (only known modules allowed).
#   git: explicit ALLOW-list of read-only subcommands.
#   find: rejected with -delete/-exec/-ok/-fprint/-fls.
#   sqlite3: mutating keywords extended to include replace, vacuum, etc.
#   git branch: -d/-D/--delete/-m/-M guarded.
# ============================================================================

# Commands eligible by name alone (any subcommand, as long as not excluded).
# Cloud CLIs (aws, gcloud, az) REMOVED — require explicit subcommand allow-list.
# env, command, xargs, tee, awk REMOVED — too broad, bypassable.
_READ_ONLY_SINGLE = frozenset({
    # === From bash_hook._WHITELIST_SINGLE ===
    "git", "pytest", "py.test", "jest", "vitest", "rspec", "ls", "find",
    "eslint", "flake8", "pylint", "shellcheck", "rubocop",
    "tail", "journalctl", "tree",
    "tsc", "webpack", "esbuild",
    "mocha", "karma", "tox", "nox", "ava", "gradle", "gradlew", "mvn",
    "deno", "bun",
    "sqlite3", "wc", "du", "df",
    "jq", "yq", "csvtool", "mlr", "csvcut",
    "grep", "rg", "ag", "ack",
    # === [P] Pipeline consumers (read-only text filters) ===
    "head", "sort", "uniq", "cut", "tr", "column",
    "nl", "fmt", "fold", "paste", "join", "comm",
    "sed",
    # === [P] Additional read-only utilities safe in pipelines ===
    "cat", "echo", "printf", "printenv",
    "which", "type", "file", "stat",
    "dirname", "basename", "realpath", "readlink",
    "date", "id", "whoami", "hostname",
    "pwd", "ps", "uptime", "uname",
    "true", "false", "test", "[",
    # === [P] Compression/decompression (read-only stdout) ===
    "gzip", "gunzip", "bzip2", "bunzip2", "xz", "unxz",
    "zcat", "bzcat", "xzcat",
    # === [P] Network diagnostic tools (read-only output) ===
    "ping", "traceroute", "nslookup", "dig", "host",
})

# Compound whitelist: (command, subcommand) pairs.
# POST-AUDIT REMOVED: ("npm","run"), ("pnpm","run"), ("yarn","run"),
#   ("terraform","state"), ("git","config"), ("kubectl","config"),
#   ("docker","pull"), ("python","-m"), ("python3","-m").
_READ_ONLY_COMPOUND = frozenset({
    # === From bash_hook._WHITELIST_COMPOUND ===
    ("git", "status"), ("git", "log"), ("git", "diff"), ("git", "show"),
    ("git", "branch"), ("git", "blame"), ("git", "grep"),
    ("git", "ls-files"), ("git", "ls-tree"),
    ("git", "rev-parse"), ("git", "rev-list"),
    ("git", "describe"), ("git", "shortlog"),
    ("git", "stash"), ("git", "remote"),
    ("npx", "jest"), ("npx", "vitest"),
    ("npm", "test"),
    ("cargo", "test"),
    ("go", "test"),
    ("ruff", "check"),
    ("biome", "lint"),
    ("golangci-lint", "run"),
    ("pip", "list"), ("pip3", "list"),
    ("npm", "ls"),
    ("pnpm", "list"),
    ("docker", "ps"),
    ("brew", "list"),
    ("vite", "build"),
    ("next", "build"),
    ("go", "build"),
    ("cypress", "run"),
    ("playwright", "test"),
    ("npx", "cypress"),
    ("npx", "playwright"),
    ("npx", "mocha"),
    ("npx", "karma"),
    ("npx", "ava"),
    ("gradle", "test"),
    ("gradlew", "test"),
    ("mvn", "test"),
    ("deno", "test"),
    ("bun", "test"),
    ("docker", "logs"),
    ("docker", "inspect"),
    ("docker", "images"),
    ("docker", "info"),
    ("docker", "version"),
    ("docker", "compose"),
    ("kubectl", "get"),
    ("kubectl", "describe"),
    ("kubectl", "logs"),
    ("kubectl", "top"),
    ("kubectl", "events"),
    ("kubectl", "explain"),
    ("kubectl", "api-resources"),
    ("kubectl", "api-versions"),
    ("helm", "list"),
    ("helm", "status"),
    ("helm", "history"),
    ("helm", "get"),
    ("terraform", "plan"),
    ("terraform", "show"),
    ("terraform", "output"),
    ("yarn", "test"),
    ("make", "-n"),
    ("cargo", "check"),
})

# ============================================================================
# git: explicit ALLOW-list of read-only subcommands.
# Everything else (not in this list) is rejected as potentially side-effecting.
# ============================================================================
_GIT_READ_ONLY_SUBCMDS = frozenset({
    "status", "log", "diff", "show", "branch",
    "blame", "grep", "ls-files", "ls-tree",
    "rev-parse", "rev-list", "describe", "shortlog",
    "stash", "remote",
})

# git branch destructive flags. Covers delete (-d/-D/--delete), rename
# (-m/-M/--move) and copy (-c/-C/--copy, which are copy/force-copy, not
# "create"), plus --force/-f. All are state-changing and must not pass the
# read-only gate.
_GIT_BRANCH_DESTRUCTIVE_FLAGS = frozenset({
    "-d", "-D", "--delete",
    "-m", "-M", "--move",
    "-c", "-C", "--copy",
    "-f", "--force",
})

# find destructive flags.
_FIND_DESTRUCTIVE_FLAGS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir",
                                      "-fprint", "-fprint0", "-fls"})

# ============================================================================
# Never-read-only: shell interpreters, privilege escalation, arbitrary code.
# ============================================================================
_NEVER_READ_ONLY = frozenset({
    "bash", "sh", "zsh", "dash", "fish", "ksh",
    "sudo", "su", "doas", "pkexec",
    "python", "python3", "python2",
    "node",
    "ruby", "perl", "php", "lua",
    "eval", "exec", "source",
    "env",       # env can launch arbitrary commands
    "command",   # command can launch arbitrary commands
    "xargs",     # xargs can launch arbitrary commands
})

# python -m modules that are known read-only.
_PYTHON_M_SAFE_MODULES = frozenset({"pytest", "unittest", "json.tool"})

# sqlite3 mutation keywords (case-insensitive check).
_SQLITE3_MUTATION_KEYWORDS = frozenset({
    "insert", "update", "delete", "drop", "alter", "create",
    "replace", "vacuum", "pragma",
})


def _is_stage_read_only(tokens: list[str]) -> tuple[bool, str]:
    """Check if a single pipeline stage is read-only.

    Args:
        tokens: The shlex-split tokens for this stage, with redirections
                already stripped.

    Returns:
        (is_read_only, reason) — reason is a short string for diagnostics.
    """
    if not tokens:
        return False, "empty-stage"

    # Strip leading env var assignments (FOO=bar)
    cmd_start = 0
    while cmd_start < len(tokens) and "=" in tokens[cmd_start] and not tokens[cmd_start].startswith("-"):
        cmd_start += 1

    if cmd_start >= len(tokens):
        return False, "env-only-stage"

    cmd = tokens[cmd_start]
    subcmd = tokens[cmd_start + 1] if cmd_start + 1 < len(tokens) else ""
    remaining = tokens[cmd_start + 2:]

    # Hard block: interpreters, privilege escalation, and broad launchers.
    if cmd in _NEVER_READ_ONLY:
        if cmd in ("python", "python3", "python2"):
            if subcmd == "-m" and cmd_start + 2 < len(tokens):
                module = tokens[cmd_start + 2]
                if module in _PYTHON_M_SAFE_MODULES:
                    return True, "python-m-whitelisted-module"
            return False, "python-not-whitelisted"
        if cmd == "node":
            if subcmd == "-e":
                return False, "node-e-arbitrary-code"
            remaining_for_node = tokens[cmd_start + 1:]
            if any(arg.endswith(".json") or ".json" in arg for arg in remaining_for_node):
                return True, "node-json-inspection"
            return False, "node-not-whitelisted"
        return False, f"never-read-only:{cmd}"

    # -------------------------------------------------------------------
    # git: explicit ALLOW-list (not deny-list).
    # -------------------------------------------------------------------
    if cmd == "git":
        if not subcmd:
            return False, "git-no-subcmd"
        if subcmd not in _GIT_READ_ONLY_SUBCMDS:
            return False, f"git-forbidden-subcmd:{subcmd}"
        # Special guard: git branch with destructive flags.
        if subcmd == "branch":
            for tok in remaining:
                if tok in _GIT_BRANCH_DESTRUCTIVE_FLAGS:
                    return False, f"git-branch-destructive-flag:{tok}"
        return True, "git-read-only"

    # -------------------------------------------------------------------
    # find: reject if destructive flags present.
    # -------------------------------------------------------------------
    if cmd == "find":
        all_tokens = tokens[cmd_start:]
        for tok in all_tokens:
            if tok in _FIND_DESTRUCTIVE_FLAGS:
                return False, f"find-destructive-flag:{tok}"
        return True, "find-read-only"

    # -------------------------------------------------------------------
    # sqlite3: extended mutation keyword blocklist.
    # -------------------------------------------------------------------
    if cmd == "sqlite3":
        all_tokens = tokens[cmd_start:]
        tokens_lower = [t.lower() for t in all_tokens]
        # Check each token for mutation keywords (substring match).
        for t in tokens_lower:
            for kw in _SQLITE3_MUTATION_KEYWORDS:
                if kw in t:
                    return False, f"sqlite3-mutation:{kw}"
            # Dot-commands: .import, .restore are writes.
            if t in (".import", ".restore"):
                return False, f"sqlite3-dotcmd-write:{t}"
        return True, "sqlite3-read-only"

    # -------------------------------------------------------------------
    # sed: -i is in-place edit.
    # -------------------------------------------------------------------
    if cmd == "sed":
        # Scan all arguments after the command (including subcmd/index-1).
        sed_args = tokens[cmd_start + 1:]
        for arg in sed_args:
            if arg == "-i" or (arg.startswith("-") and "i" in arg.replace("-", "")):
                return False, "sed-in-place"
        return True, "sed-read-only"

    # -------------------------------------------------------------------
    # deno / bun: only allow known safe subcommands.
    # "deno test" and "bun test" are in compound. Anything else: reject.
    # "deno run -A evil.ts" would not match compound → falls through to
    # single check → only "test" subcmd allowed.
    # -------------------------------------------------------------------
    if cmd in ("deno", "bun"):
        if subcmd != "test":
            return False, f"{cmd}-forbidden-subcmd:{subcmd}"
        return True, f"{cmd}-test-only"

    # -------------------------------------------------------------------
    # mvn: only "test" subcmd. "mvn deploy" rejected.
    # -------------------------------------------------------------------
    if cmd == "mvn":
        if subcmd != "test":
            return False, f"mvn-forbidden-subcmd:{subcmd}"
        return True, "mvn-test-only"

    # -------------------------------------------------------------------
    # sqlite3 already handled above. Return here only for compound match
    # before single-whitelist fallthrough.
    # -------------------------------------------------------------------

    # -------------------------------------------------------------------
    # Compound whitelist check.
    # -------------------------------------------------------------------
    if (cmd, subcmd) in _READ_ONLY_COMPOUND:
        # kubectl: secrets exclusion.
        if cmd == "kubectl":
            if any(arg in ("secret", "secrets") or arg.startswith(("secret/", "secrets/")) for arg in remaining):
                return False, "kubectl-secrets"
        # docker compose: sub-subcommand check.
        if cmd == "docker" and subcmd == "compose":
            subsub = tokens[cmd_start + 2] if cmd_start + 2 < len(tokens) else ""
            if subsub in ("ps", "logs", "config", "images", "version", "top", "events"):
                return True, "docker-compose-read-only"
            if subsub in ("up", "down", "build", "run", "exec", "pull", "push", "restart", "start", "stop", "rm"):
                return False, "docker-compose-side-effect"
            return False, "docker-compose-unknown"
        return True, "compound-whitelist"

    # -------------------------------------------------------------------
    # Single command whitelist.
    # -------------------------------------------------------------------
    if cmd in _READ_ONLY_SINGLE:
        return True, "single-whitelist"

    # Not in any whitelist.
    return False, f"not-whitelisted:{cmd}"


# ============================================================================
# Main entry points
# ============================================================================

def _raw_command_has_dangerous_constructs(command_str: str) -> tuple[bool, str]:
    """Check the raw command string for constructs shlex is blind to.

    Must run BEFORE shlex.split() to catch injection payloads embedded in
    whitespace, command substitution, process substitution, and background/async.

    Returns (has_dangerous, reason).
    """
    # Newlines indicate injection payloads split across lines.
    if _DANGEROUS_RAW_CHARS.search(command_str):
        return True, "contains-newline-injection"

    # Command substitution: $() and backticks.
    if _DANGEROUS_SUBSTITUTION.search(command_str):
        return True, "contains-command-substitution"

    # Process substitution: <() and >().
    if _DANGEROUS_PROCESS_SUBST.search(command_str):
        return True, "contains-process-substitution"

    return False, "clean"


def is_read_only_pipeline(command_str: str) -> tuple[bool, str]:
    """Check if a shell command is read-only in all pipeline stages.

    Args:
        command_str: The raw shell command string from tool_input.command.

    Returns:
        (is_read_only, reason) tuple. When is_read_only is True, the
        command is safe for output compression. When False, `reason`
        provides a short diagnostic.
    """
    if not command_str or not isinstance(command_str, str):
        return False, "empty-or-non-string"

    if command_str.strip() == "":
        return False, "whitespace-only"

    # --- PRE-TOKENIZATION SCREEN ---
    # Reject raw strings with injection constructs shlex is blind to.
    has_danger, reason = _raw_command_has_dangerous_constructs(command_str)
    if has_danger:
        return False, reason

    # Parse the command into tokens.
    try:
        tokens = shlex.split(command_str, comments=True)
    except ValueError:
        return False, "unparseable-quoting"

    if not tokens:
        return False, "empty-after-split"

    # --- TOKEN-LEVEL SCREEN ---
    # Reject glued operators (a|b, cat;rm) where | or ; appears inside a token
    # but is not the standalone separator token.
    for tok in tokens:
        if _token_has_glued_operator(tok):
            return False, f"glued-operator-in-token:{tok}"
        # Also reject bare & (async/background) — not part of &&, >&, &>.
        if tok == "&":
            return False, "bare-ampersand-async"

    # Split into stages.
    stages = _split_stages(tokens)
    if not stages:
        return False, "no-stages"

    # Check each stage.
    for i, stage_tokens in enumerate(stages):
        clean, has_file_redirect = _strip_redirections(stage_tokens)
        if has_file_redirect:
            return False, f"stage-{i + 1}:file-redirect-write"
        if not clean:
            continue  # empty after redirection stripping (all redirections)
        ok, reason = _is_stage_read_only(clean)
        if not ok:
            return False, f"stage-{i + 1}:{reason}"

    return True, "all-stages-read-only"


def get_pipeline_eligibility(command_str: str) -> dict:
    """Diagnostic function: return detailed eligibility information."""
    result: dict = {
        "is_eligible": False,
        "reason": "",
        "stage_count": 0,
        "stages": [],
    }

    if not command_str or command_str.strip() == "":
        result["reason"] = "empty"
        return result

    # Pre-tokenization screen.
    has_danger, danger_reason = _raw_command_has_dangerous_constructs(command_str)
    if has_danger:
        result["reason"] = danger_reason
        return result

    try:
        tokens = shlex.split(command_str, comments=True)
    except ValueError:
        result["reason"] = "unparseable"
        return result

    if not tokens:
        result["reason"] = "empty-after-split"
        return result

    # Token-level screen.
    for tok in tokens:
        if _token_has_glued_operator(tok):
            result["reason"] = f"glued-operator-in-token:{tok}"
            return result
        if tok == "&":
            result["reason"] = "bare-ampersand-async"
            return result

    stages = _split_stages(tokens)
    result["stage_count"] = len(stages)

    all_read_only = True
    for i, stage_tokens in enumerate(stages):
        clean, has_file_redirect = _strip_redirections(stage_tokens)
        stage_info = {
            "index": i,
            "raw_tokens": stage_tokens,
            "cleaned_tokens": clean,
            "is_read_only": True,
            "reason": "",
        }
        if has_file_redirect:
            stage_info["is_read_only"] = False
            stage_info["reason"] = "file-redirect-write"
            all_read_only = False
        elif not clean:
            stage_info["is_read_only"] = True
            stage_info["reason"] = "empty-after-redirect-strip"
        else:
            ok, reason = _is_stage_read_only(clean)
            stage_info["is_read_only"] = ok
            stage_info["reason"] = reason
            if not ok:
                all_read_only = False

        result["stages"].append(stage_info)

    if all_read_only:
        result["is_eligible"] = True
        result["reason"] = "all-stages-read-only"
    else:
        for s in result["stages"]:
            if not s["is_read_only"]:
                result["reason"] = f"stage-{s['index'] + 1}:{s['reason']}"
                break

    return result
