#!/usr/bin/env python3
"""Deterministic-candidate analysis for Token Coach.

Finds the parts of a user's workflow that could be plain code instead of model
calls, using only the local session transcripts. No model calls, no network,
read-only (the single write is an optional result cache in Token Optimizer's own
data dir, holding redacted command shapes and counts, never file contents).

Four detectors, all measured from the transcripts:

  repeated_sequence  the same ordered run of 3+ tool calls in 3+ sessions
  templated_subagent subagent launches whose normalised first 300 chars match (5+)
  polling_loop       one literal command run 4+ times with no edit in between
                     (or a wait/check pattern: `sleep` + a status command)
  parameter_sweep    one normalised command shape run 4+ times over distinct
                     literal parameters (run-0, run-1, ...) with no edit
  check_only_turn    a turn whose only tool call was a passing test/build/lint run
                     and whose reply was short: the exit code already answered

Transcript readers exist for Claude Code (also used by Cowork) and Codex, the two
runtimes whose transcripts carry ordered tool calls with their inputs. Every other
runtime gets an explicit "not measurable on <runtime>" entry (see RUNTIME_SUPPORT).

Reading the numbers: ``tokens`` and ``cost_usd`` are what the turns that ran
those calls used (whole-turn input including cache reads, plus output), priced at
API-equivalent rates. They are not what a script would save: moving a step to
code removes those turns, so the saving is at most this much. Every figure is
"measured on your transcripts".

An inline script (``python3 - <<'EOF'``, ``python3 -c``, ``bash -c``, ``node -e``,
``ruby -e``, ``perl -e``, here-strings) is a different command when its body
differs: the call key carries a fingerprint of the redacted, literal-blanked body.

Pricing is injected (``price_fn``) so this module stays free of measure.py and the
numbers come from the same rate cards as every other dollar figure in the tool.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

from credential_patterns import CommandMarks, scrub_command_syntax

# Part of the cache key. Bump when the SHAPE of stored examples changes, so a cache
# written by an older algorithm (possibly holding an example the newer scrubbers would
# have blanked) is never served again. 3: command-shape secret scrubbers (review F4).
ALGO_VERSION = 3
BASIS = "measured on your transcripts"
USED_NOT_SAVED_NOTE = "Moving a step to code removes those turns; the saving is at most this much."
PROGRESS_MIN_INTERVAL_S = 2.0
INLINE_SNIPPET_CHARS = 60

MIN_SEQ_LEN = 3
MAX_SEQ_LEN = 8
MIN_SEQ_SESSIONS = 3
MIN_SUBAGENT_LAUNCHES = 5
MIN_POLL_RUNS = 4
MIN_CHECK_TURNS = 3
MAX_CANDIDATES = 10
SHORT_REPLY_CHARS = 200
EXAMPLE_CAP = 200
PROMPT_KEY_CHARS = 300
MAX_CALLS_PER_SESSION = 5000
INLINE_READ_CHARS = 8000
PARTIAL_CACHE_TTL_S = 600
MAX_PARSE_FILE_BYTES = 96 * 1024 * 1024
MAX_JSONL_LINE_CHARS = 8 * 1024 * 1024
_BIG_LINE_CHARS = 20000

WITHHELD = "[example withheld: credential redactor unavailable]"

# Which runtimes can be measured. Only Claude Code (and Cowork, which writes the
# same transcripts) and Codex keep ordered tool calls WITH their inputs in a
# transcript Token Optimizer can read. The others keep counts or names only.
RUNTIME_SUPPORT: dict[str, dict[str, Any]] = {
    "claude": {"measurable": True, "reader": "claude_jsonl"},
    "codex": {"measurable": True, "reader": "codex_jsonl"},
    "opencode": {"measurable": False, "reason": "OpenCode runs Token Optimizer as a native TypeScript plugin; measure.py has no reader for its session store"},
    "copilot": {"measurable": False, "reason": "Copilot adapters keep tool-call counts and names, not ordered tool inputs"},
    "hermes": {"measurable": False, "reason": "Hermes sessions are read from state.db, which keeps a tool-call count, not ordered tool inputs"},
    "cursor": {"measurable": False, "reason": "Cursor sessions are read from a hook tally of tool counts, not ordered tool inputs"},
    "antigravity": {"measurable": False, "reason": "Antigravity sessions are read from a conversation store that keeps a tool-call count, not ordered tool inputs"},
    "grok": {"measurable": False, "reason": "Grok Build sessions keep a tool-call count, not ordered tool inputs"},
    "pi": {"measurable": False, "reason": "Pi runs Token Optimizer as a native TypeScript extension; measure.py has no reader for its session store"},
}


def not_measurable(runtime: str) -> dict[str, Any]:
    info = RUNTIME_SUPPORT.get(runtime) or {}
    reason = info.get("reason") or "this runtime does not keep ordered tool-call inputs in a transcript Token Optimizer reads"
    return {"runtime": runtime, "measurable": False, "reason": f"not measurable on {runtime}: {reason}"}


def runtime_support_table() -> list[dict[str, Any]]:
    out = []
    for name, info in RUNTIME_SUPPORT.items():
        if info["measurable"]:
            out.append({"runtime": name, "measurable": True})
        else:
            out.append(not_measurable(name))
    return out


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?\w+")
_QUOTED_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'')
_ENV_SECRET_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL)[A-Za-z0-9_]*)"
    r"=(?:\[CREDENTIAL REDACTED[^\]]*\]|\"[^\"]*\"|'[^']*'|\S+)",
    re.I,
)

# Email addresses identify a person. `git@host` (the ssh remote user) is not one.
_EMAIL_RE = re.compile(r"(?<![\w.+-])(?!git@)[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")


# Placeholders for the shaping step: short tokens, not labeled redaction text.
_SHAPE_MARKS = CommandMarks(creds="<creds>", arg="<arg>", secret="<secret>", user="<user>")


def scrub_command_secrets(text: str) -> str:
    """Blank credentials and personal identifiers that live in a command's syntax.

    The credential rules live in credential_patterns.scrub_command_syntax (one
    engine, also used to redact stored command text); this adds the email rule,
    which is shaping-only.
    """
    text = scrub_command_syntax(text, _SHAPE_MARKS)
    text = _EMAIL_RE.sub("<email>", text)
    return text


_WIN_PATH_RE = re.compile(
    r"(?<![\w\\])(?:[A-Za-z]:[\\/]+|\\{2,}[^\s\\/\"'|;&<>()]+[\\/]+)(?:[^\s\\/\"'|;&<>()]+[\\/]+)*[^\s\\/\"'|;&<>()]*"
)
_POSIX_PATH_RE = re.compile(
    r"(?<![\w/.\-:<>$\\])(?:~|\$\{?HOME\}?)?/(?!(?:dev|proc|sys)/)(?:[^\s/\"'|;&<>()=]+/+)+[^\s/\"'|;&<>()=]+"
)
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_DATETIME_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
)
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}:\d{2}\b")
_HASH_RE = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,64}\b")
_TOKEN_RE = re.compile(r"\b(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{24,}\b")
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)*(?![A-Za-z0-9])")
_CD_PREFIX_RE = re.compile(r"^(?:cd\s+\S+\s*(?:&&|;)\s*)+")
_WS_RE = re.compile(r"\s+")


def _path_to_placeholder(match: "re.Match[str]") -> str:
    text = match.group(0)
    base = re.split(r"[\\/]+", text.rstrip("\\/"))[-1] if text.rstrip("\\/") else ""
    return f"<dir>/{base}" if base else "<dir>"


def _blank_literals(text: str, max_chars: int) -> str:
    """Whitespace, secrets, quoted strings, paths, ids and numbers reduced to placeholders."""
    text = text[:max_chars * 4]
    text = _WS_RE.sub(" ", text).strip()
    text = _ENV_SECRET_RE.sub(lambda mm: f"{mm.group(1)}=<secret>", text)
    text = scrub_command_secrets(text)
    text = _QUOTED_RE.sub(lambda mm: "<str:long>" if len(mm.group(0)) > 80 else "<str>", text)
    text = _WIN_PATH_RE.sub(_path_to_placeholder, text)
    text = _POSIX_PATH_RE.sub(_path_to_placeholder, text)
    text = _UUID_RE.sub("<uuid>", text)
    text = _DATETIME_RE.sub("<date>", text)
    text = _TIME_RE.sub("<time>", text)
    text = _HASH_RE.sub("<hash>", text)
    text = _TOKEN_RE.sub("<token>", text)
    text = _NUMBER_RE.sub("<n>", text)
    text = _CD_PREFIX_RE.sub("", text)
    return text[:max_chars]


def normalise_command(text: Any, *, max_chars: int = 600) -> str:
    """Reduce a command (or prompt) to its reusable shape.

    Quoted strings, numbers, hashes, UUIDs, dates, long opaque tokens and the
    directory part of absolute paths (POSIX and Windows) become placeholders, so
    two runs of the same command on different days, commits or checkouts compare
    equal. Secret-looking env assignments are blanked. A leading ``cd <dir> &&``
    is dropped. Heredoc bodies are cut. The result is one line, never the input.
    """
    if not isinstance(text, str):
        return ""
    text = text[:max_chars * 4]
    m = _HEREDOC_RE.search(text)
    if m and m.start() > 0:
        text = text[:m.start()]
    return _blank_literals(text, max_chars)


# ---------------------------------------------------------------------------
# Inline scripts (heredoc, -c / -e, here-string): the launcher is not the command
# ---------------------------------------------------------------------------

_INTERPRETERS = frozenset({
    "python", "py", "pypy", "node", "nodejs", "ruby", "perl", "php", "bash", "sh", "zsh", "dash", "ksh",
    "deno", "bun", "rscript", "pwsh", "powershell",
})
_LAUNCH_WRAPPERS = frozenset({"sudo", "env", "command", "exec", "time", "nohup", "uv", "poetry", "pipenv", "pdm",
                              "hatch", "rye", "run"})
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||;|\|")
_HEREDOC_FULL_RE = re.compile(r"<<<|<<(-?)\s*(['\"]?)(\w+)\2")
_WORD_RE = re.compile(r"\s*(\"(?:[^\"\\]|\\.)*\"|'[^']*'|\S+)")
_INTERP_TOKEN_RE = re.compile(r"(?:^|(?<=[\s;&|(]))((?:\S*[\\/])?[A-Za-z][A-Za-z0-9._]*)(?=\s)")
_INLINE_QUOTED_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'')


def _interp_family(word: str) -> str:
    base = re.split(r"[\\/]", word)[-1].lower()
    if base.endswith(".exe"):
        base = base[:-4]
    base = re.sub(r"[\d.]+$", "", base) if re.match(r"^(?:python|pypy)[\d.]*$", base) else base
    return base if base in _INTERPRETERS else ""


def _inline_flag(family: str, flag: str) -> bool:
    low = flag.lower()
    if family in ("node", "nodejs"):
        return low in ("--eval", "--print") or bool(re.fullmatch(r"-[ep]+", flag))
    if family in ("ruby", "perl"):
        return bool(re.fullmatch(r"-[A-Za-z]*[eE][A-Za-z]*", flag))
    if family == "php":
        return flag == "-r"
    if family == "rscript":
        return flag == "-e"
    if family in ("pwsh", "powershell"):
        return low in ("-c", "-command")
    if family in ("deno", "bun"):
        return False
    return bool(re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", flag))      # python / shells: -c, -lc, -ec, -Bc


def _launcher_of_segment(segment: str) -> str:
    for tok in segment.split():
        if _ENV_ASSIGN_RE.match(tok) or tok.lower() in _LAUNCH_WRAPPERS or tok.startswith("-"):
            continue
        return _interp_family(tok)
    return ""


def _unquote(body: str) -> str:
    if len(body) >= 2 and body[0] == body[-1] and body[0] in "\"'":
        return body[1:-1]
    return body


def split_inline_script(text: str) -> "tuple[str, str] | None":
    """Return (text with the script body removed, script body) for an interpreter given a script inline.

    Recognises ``python3 - <<'EOF'`` style heredocs, ``-c`` / ``-e`` style flags
    (python, bash/sh/zsh, node, ruby, perl, php, Rscript, PowerShell) and
    ``<<<`` here-strings. Anything else, including ``cat <<EOF > file``, is not a script.
    """
    for m in _INTERP_TOKEN_RE.finditer(text):
        family = _interp_family(m.group(1))
        if not family:
            continue
        pos = m.end()
        while True:
            w = _WORD_RE.match(text, pos)
            if not w or not w.group(1).startswith("-") or w.group(1) in ("-", "--"):
                break
            flag = w.group(1)
            pos = w.end()
            if _inline_flag(family, flag):
                b = _WORD_RE.match(text, pos)
                if b:
                    body = _unquote(b.group(1))
                    if body.strip():
                        return text[:b.start(1)] + '""' + text[b.end(1):], body
                break
    h = _HEREDOC_FULL_RE.search(text)
    if not h:
        return None
    segment = _SEGMENT_SPLIT_RE.split(text[:h.start()])[-1]
    if not _launcher_of_segment(segment):
        return None
    if h.group(0) == "<<<":
        b = _WORD_RE.match(text, h.end())
        if not b:
            return None
        body = _unquote(b.group(1))
        return (text[:b.start(1)] + '""' + text[b.end(1):], body) if body.strip() else None
    nl = text.find("\n", h.end())
    if nl < 0:
        return None
    tag = h.group(3)
    lines = []
    for line in text[nl + 1:].split("\n"):
        if line.strip() == tag:
            break
        lines.append(line)
    body = "\n".join(lines)
    return (text[:h.end()], body) if body.strip() else None


def inline_script_fingerprint(body: str) -> str:
    """Stable id of a script body after literal blanking (ids, numbers, strings, paths)."""
    shaped = _blank_literals(body, 4000)
    return hashlib.sha1(shaped.encode("utf-8", "replace")).hexdigest()[:10]


def inline_script_snippet(body: str) -> str:
    flat = scrub_command_secrets(_WS_RE.sub(" ", body).strip())
    return flat if len(flat) <= INLINE_SNIPPET_CHARS else flat[:INLINE_SNIPPET_CHARS].rstrip() + "..."


# ---------------------------------------------------------------------------
# Check-command classification (tests, builds, linters)
# ---------------------------------------------------------------------------

_CHECK_PREFIX = (
    r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*"
    r"(?:(?:uv|poetry|pipenv|pdm|hatch|rye)\s+run\s+|npx\s+|pnpx\s+|bunx\s+|pnpm\s+exec\s+|yarn\s+|"
    r"python3?(?:\.\d+)?\s+-m\s+|py\s+-3\s+-m\s+)?"
)
_CHECK_RE = re.compile(
    _CHECK_PREFIX
    + r"(?:pytest|py\.test|tox|nox|unittest|flake8|mypy|pyright|pylint|tsc|eslint|jest|vitest|mocha|"
    r"playwright\s+test|golangci-lint|rspec|rubocop|phpunit|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|t|build|lint|typecheck|type-check|check|tsc|ci|test:\S+|lint:\S+|build:\S+|check:\S+)|"
    r"cargo\s+(?:test|build|check|clippy|nextest)|"
    r"go\s+(?:test|build|vet)|"
    r"(?:\./)?gradlew?\s+(?:test|build|check|\S*test\S*)|mvnw?\s+(?:test|verify|package|compile)|"
    r"dotnet\s+(?:test|build)|swift\s+(?:test|build)|"
    r"bundle\s+exec\s+(?:rspec|rake\s+test|rubocop)|rake\s+test|"
    r"ruff(?:\s+check)?|"
    r"(?:\./|bash\s+|sh\s+)[^\s]*(?:test|check|lint)[^\s]*)(?=\s|$)"
)
# Formatters only count as a check when they do not rewrite files.
_FORMAT_CHECK_RE = re.compile(
    _CHECK_PREFIX + r"(?:black|isort|prettier|biome(?:\s+(?:check|ci))?|ruff\s+format|cargo\s+fmt)(?=\s|$)"
)
_MUTATING_FLAG_RE = re.compile(r"(?:^|\s)(?:--fix|--write|--apply|--unsafe-fixes|-w|--update-snapshots?|-u)(?=\s|$)")
_NONMUTATING_FLAG_RE = re.compile(r"(?:^|\s)(?:--check|--diff|-check|--dry-run)(?=\s|$)")
_MAKE_RE = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*g?make(?P<rest>(?:\s+\S+)*)$")
_MAKE_OK_TARGET_WORDS = frozenset({"test", "tests", "check", "lint", "build", "verify", "ci", "typecheck", "all", "compile"})
_FILTER_CMDS = ("tail", "head", "grep", "wc", "tee", "cat", "sed", "awk", "cut", "sort", "uniq", "tr")
_TRAILING_REDIRECT_RE = re.compile(r"\s*(?:\d?>\s*/dev/null|2>&1|&>\s*\S+)\s*$")
_TRIVIAL_SEGMENTS = {"true", "echo", ":", "exit"}


def _is_check_segment(seg: str) -> bool:
    seg = seg.strip()
    while True:
        new = _TRAILING_REDIRECT_RE.sub("", seg)
        if new == seg:
            break
        seg = new
    if not seg:
        return False
    mk = _MAKE_RE.match(seg)
    if mk:
        words = [w for w in mk.group("rest").split() if not w.startswith("-") and "=" not in w]
        return all(_MAKE_OK_TARGET_WORDS & set(re.split(r"[-_:./]+", w.lower())) for w in words)
    if _FORMAT_CHECK_RE.match(seg):
        return bool(_NONMUTATING_FLAG_RE.search(seg)) and not _MUTATING_FLAG_RE.search(seg)
    if _CHECK_RE.match(seg):
        return not _MUTATING_FLAG_RE.search(seg)
    return False


def is_check_command(cmd: str) -> bool:
    """True when the whole command is a test/build/lint run (exit code is the answer)."""
    norm = _CD_PREFIX_RE.sub("", _WS_RE.sub(" ", cmd or "").strip())
    if not norm:
        return False
    found = False
    for chunk in re.split(r"\s*(?:&&|;)\s*", norm):
        parts = [p.strip() for p in re.split(r"(?<!\|)\|(?!\|)", chunk)]
        head, filters = parts[0], parts[1:]
        if not head:
            continue
        if head.split()[0] in _TRIVIAL_SEGMENTS:
            continue
        if not _is_check_segment(head):
            return False
        for f in filters:
            if not f or f.split()[0] not in _FILTER_CMDS:
                return False
        found = True
    return found


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class Call:
    __slots__ = ("key", "shape", "kind", "turn", "ut", "ok", "prompt_key", "cmd_ok_check", "lit")

    def __init__(self, key, shape, kind, turn, ut, prompt_key=None, cmd_ok_check=False, lit=None):
        self.key = key
        self.shape = shape
        self.kind = kind          # bash | read | grep | glob | edit | agent | other
        self.turn = turn          # index into Trace.turns
        self.ut = ut              # index into Trace.uturns
        self.ok = None            # None unknown, True success, False failure
        self.prompt_key = prompt_key
        self.cmd_ok_check = cmd_ok_check
        self.lit = lit            # redacted literal command (no placeholder collapse)


class Turn:
    __slots__ = ("fresh", "cr", "cc", "cc1h", "cc5m", "out", "model", "reply_chars", "ut")

    def __init__(self, ut):
        self.fresh = self.cr = self.cc = self.cc1h = self.cc5m = self.out = 0
        self.model = "unknown"
        self.reply_chars = 0
        self.ut = ut


class Trace:
    __slots__ = ("session", "calls", "turns", "uturns", "path")

    def __init__(self, session, path=""):
        self.session = session
        self.path = path
        self.calls: list[Call] = []
        self.turns: list[Turn] = []
        self.uturns: list[dict[str, list[int]]] = []   # {"turns": [...], "calls": [...]}

    def new_uturn(self) -> int:
        self.uturns.append({"turns": [], "calls": []})
        return len(self.uturns) - 1

    def current_uturn(self) -> int:
        return len(self.uturns) - 1 if self.uturns else self.new_uturn()

    def new_turn(self) -> int:
        ut = self.current_uturn()
        self.turns.append(Turn(ut))
        idx = len(self.turns) - 1
        self.uturns[ut]["turns"].append(idx)
        return idx

    def add_call(self, call: Call) -> None:
        self.calls.append(call)
        self.uturns[call.ut]["calls"].append(len(self.calls) - 1)


class BudgetExceeded(Exception):
    pass


class Redactor:
    """Wraps the credential redactor; remembers if it ever failed (fail closed)."""

    def __init__(self, fn: Callable[[str], str] | None):
        self.fn = fn
        self.failed = fn is None

    def __call__(self, text: str) -> str:
        if self.fn is None:
            self.failed = True
            return text
        try:
            return self.fn(text)
        except Exception:
            self.failed = True
            return text


class Throttle:
    """Wrap a progress callback so it fires at most once per ``interval_s`` (first call always passes)."""

    def __init__(self, fn: "Callable[[str], None] | None", interval_s: float = PROGRESS_MIN_INTERVAL_S,
                 clock: Callable[[], float] = time.monotonic):
        self.fn = fn
        self.interval_s = interval_s
        self.clock = clock
        self._last: float | None = None

    def __call__(self, msg: str) -> None:
        if self.fn is None:
            return
        now = self.clock()
        if self._last is not None and now - self._last < self.interval_s:
            return
        self._last = now
        self.fn(msg)


def default_redactor() -> Redactor:
    try:
        from credential_patterns import redact_credentials
        return Redactor(redact_credentials)
    except Exception:
        return Redactor(None)


# ---------------------------------------------------------------------------
# Tool-call keys
# ---------------------------------------------------------------------------

_BOOKKEEPING = frozenset({
    "TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TaskOutput", "TaskStop",
    "ExitPlanMode", "EnterPlanMode", "AskUserQuestion", "ToolSearch", "SendMessage",
    "update_plan", "wait_agent", "close_agent", "ListAgents", "ReadNotifications",
})
_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"})
_READ_TOOLS = frozenset({"Read", "NotebookRead", "view_image"})
_AGENT_TOOLS = frozenset({"Agent", "Task", "spawn_agent"})
_BASH_TOOLS = frozenset({"Bash", "exec_command", "shell", "shell_command", "container.exec", "local_shell", "write_stdin"})


def _ext_of(path: Any) -> str:
    if not isinstance(path, str) or not path:
        return "?"
    base = re.split(r"[\\/]+", path.rstrip("\\/"))[-1]
    ext = os.path.splitext(base)[1].lower()
    return ext or (base.lower() if base else "?")


class _KeyMaker:
    """Turns a tool call into (key, shape, kind). Memoised; redacts before shaping."""

    def __init__(self, redact: Redactor):
        self.redact = redact
        self._cmd_memo: dict[str, tuple[str, str, bool]] = {}

    def command(self, raw: str) -> tuple[str, str, bool, str]:
        hit = self._cmd_memo.get(raw)
        if hit is None:
            clean = self.redact(raw[:INLINE_READ_CHARS])
            inline = split_inline_script(clean)
            if inline is not None:
                head, body = inline
                norm = normalise_command(head)
                key = f"B:{norm}#{inline_script_fingerprint(body)}"
                shape = f"Bash: {norm} [script: {inline_script_snippet(body)}]"
            else:
                norm = normalise_command(clean)
                key, shape = "B:" + norm, "Bash: " + norm
            # The literal form keeps every parameter: polling means re-running
            # ONE literal command; a normalised shape that hides several
            # literals is a parameter sweep.
            lit = _CD_PREFIX_RE.sub("", _WS_RE.sub(" ", clean).strip())
            hit = (key, shape, is_check_command(clean), lit)
            if len(self._cmd_memo) < 20000:
                self._cmd_memo[raw] = hit
        return hit

    def prompt(self, raw: str) -> str:
        clean = self.redact(raw[:1500])
        return normalise_command(clean, max_chars=PROMPT_KEY_CHARS)

    def generic(self, name: str, inp: dict) -> tuple[str, str]:
        parts = []
        for k in sorted(inp):
            v = inp[k]
            if isinstance(v, (int, float, bool)) and not isinstance(v, bool):
                parts.append(f"{k}=<n>")
            elif isinstance(v, bool):
                parts.append(f"{k}={str(v).lower()}")
            elif isinstance(v, str):
                s = normalise_command(self.redact(v[:300]), max_chars=60)
                parts.append(f"{k}={s}")
            else:
                parts.append(f"{k}=<{type(v).__name__}>")
        body = ",".join(parts)[:160]
        return f"O:{name}({body})", f"{name}({body})"

    def build(self, name: str, inp: dict) -> tuple[str, str, str, str | None, bool, str | None]:
        """Return (key, shape, kind, prompt_key, is_check, lit)."""
        if name in _BASH_TOOLS:
            cmd = inp.get("command") if "command" in inp else inp.get("cmd")
            if isinstance(cmd, list):
                cmd = cmd[-1] if cmd and isinstance(cmd[-1], str) and len(cmd) >= 3 and cmd[1] in ("-c", "-lc") else " ".join(str(c) for c in cmd)
            if name == "write_stdin":
                polling = not inp.get("chars")
                label = "write_stdin <poll>" if polling else "write_stdin <input>"
                return "B:" + label, "Bash: " + label, "bash", None, False, label
            if not isinstance(cmd, str) or not cmd.strip():
                return "B:?", "Bash: ?", "bash", None, False, "?"
            key, shape, chk, lit = self.command(cmd)
            return key, shape, "bash", None, chk, lit
        if name in _READ_TOOLS:
            ext = _ext_of(inp.get("file_path") or inp.get("path") or inp.get("notebook_path"))
            return f"R:{ext}", f"Read *{ext}", "read", None, False, None
        if name in _EDIT_TOOLS:
            if name == "apply_patch":
                ext = _ext_of(inp.get("_patch_path"))
            else:
                ext = _ext_of(inp.get("file_path") or inp.get("path") or inp.get("notebook_path"))
            return f"E:{name}:{ext}", f"{name} *{ext}", "edit", None, False, None
        if name == "Grep":
            pat = normalise_command(self.redact(str(inp.get("pattern") or "")[:200]), max_chars=80)
            scope = inp.get("glob") or inp.get("type") or ""
            scope = _ext_of(scope) if isinstance(scope, str) and scope else ""
            return f"G:{pat}|{scope}", f'Grep "{pat}" {scope}'.strip(), "grep", None, False, None
        if name == "Glob":
            pat = normalise_command(self.redact(str(inp.get("pattern") or "")[:200]), max_chars=80)
            return f"L:{pat}", f"Glob {pat}", "glob", None, False, None
        if name in _AGENT_TOOLS:
            prompt = inp.get("prompt") or inp.get("message") or inp.get("task") or inp.get("description") or ""
            if not isinstance(prompt, str):
                prompt = ""
            pk = self.prompt(prompt)
            return f"A:{pk}", f"Agent: {pk}", "agent", pk, False, None
        key, shape = self.generic(name, inp)
        return key, shape, "other", None, False, None


# ---------------------------------------------------------------------------
# Claude Code transcript reader
# ---------------------------------------------------------------------------

_TOOL_USE_ID_RE = re.compile(r'"tool_use_id"\s*:\s*"([^"]+)"')
_IS_ERROR_TRUE_RE = re.compile(r'"is_error"\s*:\s*true')


def _safe_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _iter_lines(path: Path, deadline: float | None):
    try:
        if os.stat(path).st_size > MAX_PARSE_FILE_BYTES:
            return
    except OSError:
        return
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh):
            if deadline is not None and (n & 1023) == 0 and time.monotonic() >= deadline:
                raise BudgetExceeded()
            if len(line) > MAX_JSONL_LINE_CHARS:
                continue
            yield line


def _human_user_text(rec: dict) -> bool:
    """True for a record a human authored (not tool output, not injected meta)."""
    if rec.get("isMeta") or rec.get("isCompactSummary"):
        return False
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else msg
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        has_text = False
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "tool_result":
                    return False
                if b.get("type") == "text" and str(b.get("text") or "").strip():
                    has_text = True
            elif isinstance(b, str) and b.strip():
                has_text = True
        return has_text
    return False


def extract_claude(path: Path | str, keys: _KeyMaker, seen_ids: set,
                   deadline: float | None = None) -> Trace | None:
    """Read one Claude Code transcript into a Trace (None if it holds nothing usable).

    Records already seen in a newer transcript (a resumed or forked session copies
    its history, including requestIds and tool_use ids) are skipped so history is
    counted once. ``seen_ids`` is updated only when the whole file was read.
    """
    path = Path(path)
    trace = Trace(path.stem, str(path))
    turn_by_req: dict[str, int] = {}
    call_by_id: dict[str, Call] = {}
    local_ids: set[str] = set()
    for line in _iter_lines(path, deadline):
        if '"assistant"' not in line and '"user"' not in line:
            continue
        if len(line) > _BIG_LINE_CHARS and '"type":"user"' in line[:2000] and '"tool_result"' in line:
            ids = _TOOL_USE_ID_RE.findall(line)
            if len(ids) == 1:
                call = call_by_id.get(ids[0])
                if call is not None:
                    call.ok = not _IS_ERROR_TRUE_RE.search(line)
                continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(rec, dict) or rec.get("isSidechain") is True:
            continue
        rtype = rec.get("type")
        if rtype == "assistant":
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            rid = rec.get("requestId") or msg.get("id")
            if isinstance(rid, str) and rid in seen_ids:
                continue
            tidx = turn_by_req.get(rid) if isinstance(rid, str) else None
            if tidx is None:
                tidx = trace.new_turn()
                if isinstance(rid, str):
                    turn_by_req[rid] = tidx
                    local_ids.add(rid)
            turn = trace.turns[tidx]
            usage = msg.get("usage")
            if isinstance(usage, dict):
                cc_obj = usage.get("cache_creation")
                cc_obj = cc_obj if isinstance(cc_obj, dict) else {}
                cc1 = _safe_int(cc_obj.get("ephemeral_1h_input_tokens") or usage.get("ephemeral_1h_input_tokens"))
                cc5 = _safe_int(cc_obj.get("ephemeral_5m_input_tokens") or usage.get("ephemeral_5m_input_tokens"))
                cc = _safe_int(usage.get("cache_creation_input_tokens")) or (cc1 + cc5)
                turn.fresh = max(turn.fresh, _safe_int(usage.get("input_tokens")))
                turn.out = max(turn.out, _safe_int(usage.get("output_tokens")))
                turn.cr = max(turn.cr, _safe_int(usage.get("cache_read_input_tokens")))
                turn.cc = max(turn.cc, cc)
                turn.cc1h = max(turn.cc1h, cc1)
                turn.cc5m = max(turn.cc5m, cc5)
            model = msg.get("model")
            if isinstance(model, str) and model and model != "<synthetic>":
                turn.model = model
            content = msg.get("content")
            if isinstance(content, str):
                turn.reply_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        turn.reply_chars += len(str(block.get("text") or ""))
                    elif btype == "tool_use":
                        name = block.get("name")
                        cid = block.get("id")
                        if not isinstance(name, str) or name in _BOOKKEEPING:
                            continue
                        if isinstance(cid, str) and (cid in seen_ids or cid in call_by_id):
                            continue
                        if len(trace.calls) >= MAX_CALLS_PER_SESSION:
                            continue
                        inp = block.get("input")
                        inp = inp if isinstance(inp, dict) else {}
                        key, shape, kind, pkey, chk, lit = keys.build(name, inp)
                        call = Call(key, shape, kind, tidx, turn.ut, pkey, chk, lit)
                        trace.add_call(call)
                        if isinstance(cid, str):
                            call_by_id[cid] = call
                            local_ids.add(cid)
        elif rtype == "user":
            msg = rec.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        call = call_by_id.get(block.get("tool_use_id"))
                        if call is not None:
                            call.ok = not bool(block.get("is_error"))
            if _human_user_text(rec):
                if not trace.uturns or trace.uturns[-1]["turns"] or trace.uturns[-1]["calls"]:
                    trace.new_uturn()
    if not trace.turns:
        return None
    seen_ids.update(local_ids)
    return trace


# ---------------------------------------------------------------------------
# Codex transcript reader
# ---------------------------------------------------------------------------

_CODEX_EXIT_RE = re.compile(r"(?:exited with code|exit code:?)\s*(-?\d+)", re.I)
_CODEX_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.M)
_CODEX_ENV_PREFIXES = ("<environment_context>", "<user_instructions>", "<user_action>", "# AGENTS.md", "<permissions")


def _codex_args(payload: dict) -> dict:
    raw = payload.get("arguments")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _codex_text(payload: dict) -> str:
    if payload.get("type") in ("user_message", "agent_message"):
        return str(payload.get("message") or "")
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(i.get("text") or "") if isinstance(i, dict) else str(i) for i in content)
    return ""


def _codex_exit_from_output(output: Any) -> int | None:
    if isinstance(output, str):
        stripped = output.lstrip()
        if stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                obj = None
            if isinstance(obj, dict):
                meta = obj.get("metadata")
                if isinstance(meta, dict) and isinstance(meta.get("exit_code"), int):
                    return meta["exit_code"]
        m = _CODEX_EXIT_RE.search(output[:4000])
        if m:
            return int(m.group(1))
    return None


def extract_codex(path: Path | str, keys: _KeyMaker, seen_ids: set,
                  deadline: float | None = None) -> Trace | None:
    path = Path(path)
    trace = Trace(path.stem, str(path))
    call_by_id: dict[str, Call] = {}
    local_ids: set[str] = set()
    open_turn: int | None = None
    agent_msg_chars = 0
    message_chars = 0
    model = "unknown"
    prev_total: dict[str, int] | None = None
    uturn_reply: dict[int, list[int]] = {}

    def ensure_turn() -> int:
        nonlocal open_turn
        if open_turn is None:
            open_turn = trace.new_turn()
            trace.turns[open_turn].model = model
        return open_turn

    def add_reply(n_agent: int, n_msg: int) -> None:
        ut = trace.current_uturn()
        acc = uturn_reply.setdefault(ut, [0, 0])
        acc[0] += n_agent
        acc[1] += n_msg

    for line in _iter_lines(path, deadline):
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type")
        rtype = rec.get("type")
        m = payload.get("model")
        if isinstance(m, str) and m.strip():
            model = m.strip()
            if open_turn is not None:
                trace.turns[open_turn].model = model

        if rtype == "session_meta":
            sid = payload.get("id")
            if isinstance(sid, str) and sid:
                trace.session = sid
            continue

        if ptype == "user_message" or (rtype == "response_item" and ptype == "message" and payload.get("role") == "user"):
            text = _codex_text(payload).lstrip()
            if ptype == "message" and (not text or text.startswith(_CODEX_ENV_PREFIXES)):
                continue
            if not trace.uturns or trace.uturns[-1]["turns"] or trace.uturns[-1]["calls"]:
                trace.new_uturn()
            open_turn = None
        elif ptype == "agent_message":
            ensure_turn()
            add_reply(len(_codex_text(payload)), 0)
        elif ptype == "message" and payload.get("role") == "assistant":
            ensure_turn()
            add_reply(0, len(_codex_text(payload)))
        elif ptype in ("function_call", "custom_tool_call", "local_shell_call"):
            raw_name = str(payload.get("name") or ("local_shell" if ptype == "local_shell_call" else "unknown"))
            cid = payload.get("call_id") or payload.get("id")
            if raw_name in _BOOKKEEPING:
                continue
            if isinstance(cid, str) and (cid in seen_ids or cid in call_by_id):
                continue
            if len(trace.calls) >= MAX_CALLS_PER_SESSION:
                continue
            args = _codex_args(payload)
            if ptype == "local_shell_call":
                action = payload.get("action")
                if isinstance(action, dict):
                    args = {"command": action.get("command")}
            if raw_name == "apply_patch":
                patch = payload.get("input") if isinstance(payload.get("input"), str) else str(args.get("input") or "")
                pm = _CODEX_PATCH_FILE_RE.search(patch or "")
                args = {"_patch_path": pm.group(1) if pm else ""}
            tidx = ensure_turn()
            key, shape, kind, pkey, chk, lit = keys.build(raw_name, args)
            call = Call(key, shape, kind, tidx, trace.turns[tidx].ut, pkey, chk, lit)
            trace.add_call(call)
            if isinstance(cid, str):
                call_by_id[cid] = call
                local_ids.add(cid)
        elif ptype == "collab_agent_spawn_end":
            cid = payload.get("call_id")
            if isinstance(cid, str) and (cid in call_by_id or cid in seen_ids):
                continue
            prompt = payload.get("prompt")
            if isinstance(prompt, str) and prompt.strip():
                tidx = ensure_turn()
                key, shape, kind, pkey, chk, lit = keys.build("spawn_agent", {"prompt": prompt})
                call = Call(key, shape, kind, tidx, trace.turns[tidx].ut, pkey, chk, lit)
                call.ok = True
                trace.add_call(call)
                if isinstance(cid, str):
                    call_by_id[cid] = call
                    local_ids.add(cid)
        elif ptype in ("function_call_output", "custom_tool_call_output"):
            call = call_by_id.get(payload.get("call_id"))
            if call is not None and call.ok is None:
                code = _codex_exit_from_output(payload.get("output"))
                if code is not None:
                    call.ok = code == 0
        elif ptype == "exec_command_end":
            call = call_by_id.get(payload.get("call_id"))
            code = payload.get("exit_code")
            if call is not None and isinstance(code, int):
                call.ok = code == 0
        elif ptype == "token_count":
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            total = info.get("total_token_usage")
            last = info.get("last_token_usage")
            use = None
            if isinstance(total, dict):
                cur = {k: _safe_int(total.get(k)) for k in ("input_tokens", "cached_input_tokens", "output_tokens")}
                if prev_total is not None and cur["input_tokens"] >= prev_total["input_tokens"]:
                    use = {k: max(0, cur[k] - prev_total[k]) for k in cur}
                    prev_total = cur
                elif prev_total is not None:
                    # Cumulative went backwards (compaction/resume): the per-call figure is the only safe one.
                    if isinstance(last, dict):
                        use = {k: _safe_int(last.get(k)) for k in cur}
                else:
                    use = {k: _safe_int(last.get(k)) for k in cur} if isinstance(last, dict) else cur
                    prev_total = cur
            elif isinstance(last, dict):
                use = {k: _safe_int(last.get(k)) for k in ("input_tokens", "cached_input_tokens", "output_tokens")}
            if not use or not (use["input_tokens"] or use["output_tokens"]):
                continue
            tidx = ensure_turn()
            t = trace.turns[tidx]
            cached = min(use["cached_input_tokens"], use["input_tokens"])
            t.fresh += use["input_tokens"] - cached
            t.cr += cached
            t.out += use["output_tokens"]
            open_turn = None

    if not trace.turns:
        return None
    for ut, (agent_chars, msg_chars) in uturn_reply.items():
        # response_item messages repeat the event_msg text; take the larger, never the sum.
        if ut < len(trace.uturns) and trace.uturns[ut]["turns"]:
            trace.turns[trace.uturns[ut]["turns"][-1]].reply_chars += max(agent_chars, msg_chars)
    seen_ids.update(local_ids)
    return trace


EXTRACTORS = {"claude": extract_claude, "codex": extract_codex}


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

_SUGGESTIONS = {
    "repeated_sequence": "Wrap this exact sequence in one script (or a skill/hook that runs it) and let the model read only the result.",
    "templated_subagent": "Same prompt shape launched repeatedly: if the work is mechanical, make it a parameterised script or scheduled job; keep a subagent only for the judgment step.",
    "polling_loop": "Replace the model re-check loop with a script that waits (until <condition>; exit code) or a hook/scheduled job that notifies on completion.",
    "parameter_sweep": "Same command shape run with different parameters: drive the values from one script loop (for x in ...; do cmd \"$x\"; done) or a matrix/table run, and let the model read only the results.",
    "check_only_turn": "Run it from a hook, pre-commit or make target and read the exit code; the model does not need a turn to learn that it passed.",
}


class _Cand:
    __slots__ = ("kind", "example", "times", "sessions", "turns", "extra")

    def __init__(self, kind, example):
        self.kind = kind
        self.example = example
        self.times = 0
        self.sessions: set[int] = set()
        self.turns: set[tuple[int, int]] = set()   # (trace index, turn index)
        self.extra: dict[str, Any] = {}


def _distinct_ok(calls: list[Call]) -> bool:
    return len({c.key for c in calls}) >= 2 and any(c.kind in ("bash", "grep", "glob", "agent", "other") for c in calls)


def detect_sequences(traces: list[Trace], deadline: float | None) -> tuple[list[_Cand], bool]:
    """Ordered runs of 3+ calls seen in 3+ sessions. Apriori-pruned so memory stays small."""
    intern: dict[str, int] = {}
    seqs: list[list[int]] = []
    seq_calls: list[list[Call]] = []
    for tr in traces:
        ids = []
        for c in tr.calls:
            ids.append(intern.setdefault(c.key, len(intern)))
        seqs.append(ids)
        seq_calls.append(tr.calls)
    partial = False

    def over() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    # k = 1: keys in 3+ sessions
    sess_count: Counter = Counter()
    for ids in seqs:
        sess_count.update(set(ids))
    good_prev = {(k,) for k, c in sess_count.items() if c >= MIN_SEQ_SESSIONS}
    good_by_k: dict[int, set] = {}
    k = 1
    while good_prev and k < MAX_SEQ_LEN:
        k += 1
        counts: Counter = Counter()
        for ids in seqs:
            if over():
                return [], True
            if len(ids) < k:
                continue
            grams = set(zip(*[ids[j:] for j in range(k)]))
            counts.update(g for g in grams if g[:-1] in good_prev and g[1:] in good_prev)
        good_prev = {g for g, c in counts.items() if c >= MIN_SEQ_SESSIONS}
        if k >= MIN_SEQ_LEN and good_prev:
            good_by_k[k] = good_prev
    if not good_by_k:
        return [], partial

    # Occurrences for the surviving n-grams only.
    occ: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
    for si, ids in enumerate(seqs):
        if over():
            return [], True
        for k, good in good_by_k.items():
            if len(ids) < k:
                continue
            for pos, g in enumerate(zip(*[ids[j:] for j in range(k)])):
                if g in good:
                    occ[g].append((si, pos))

    rev = {v: k for k, v in intern.items()}
    ranked = []
    for g, places in occ.items():
        sessions = {si for si, _ in places}
        if len(sessions) < MIN_SEQ_SESSIONS:
            continue
        si0, pos0 = places[0]
        calls0 = seq_calls[si0][pos0:pos0 + len(g)]
        if not _distinct_ok(calls0):
            continue
        ranked.append((len(g), len(sessions), g, places, sessions))
    # Longest first; a shorter run that lives inside an accepted longer run in the same sessions is noise.
    ranked.sort(key=lambda r: (-r[0], -r[1]))
    covered: dict[tuple, int] = {}
    cands: list[_Cand] = []
    for k, nsess, g, places, sessions in ranked:
        if covered.get(g) == nsess:
            continue
        for kk in range(MIN_SEQ_LEN, k + 1):
            for s in range(0, k - kk + 1):
                covered.setdefault(g[s:s + kk], nsess)
        si0, pos0 = places[0]
        calls0 = seq_calls[si0][pos0:pos0 + k]
        cand = _Cand("repeated_sequence", " -> ".join(c.shape for c in calls0))
        by_session: dict[int, list[int]] = defaultdict(list)
        for si, pos in places:
            by_session[si].append(pos)
        for si, plist in by_session.items():
            last_end = -1
            for pos in sorted(plist):
                if pos < last_end:
                    continue
                last_end = pos + k
                cand.times += 1
                cand.sessions.add(si)
                for c in seq_calls[si][pos:pos + k]:
                    cand.turns.add((si, c.turn))
        cand.extra["sequence_length"] = k
        cands.append(cand)
        if len(cands) >= 400:
            break
    return cands, partial


def detect_subagents(traces: list[Trace]) -> list[_Cand]:
    groups: dict[str, _Cand] = {}
    for si, tr in enumerate(traces):
        for c in tr.calls:
            if c.kind != "agent" or not c.prompt_key:
                continue
            cand = groups.get(c.prompt_key)
            if cand is None:
                cand = groups[c.prompt_key] = _Cand("templated_subagent", c.prompt_key)
            cand.times += 1
            cand.sessions.add(si)
            cand.turns.add((si, c.turn))
    return [c for c in groups.values() if c.times >= MIN_SUBAGENT_LAUNCHES]


# A `sleep` segment is the model waiting for state to change: that is a
# re-check loop even when the checked target's parameters differ each run.
_SLEEP_SEGMENT_RE = re.compile(r"(?:^|&&|\|\||;|\|)\s*sleep(?=\s|$)")


def detect_polling(traces: list[Trace]) -> list[_Cand]:
    groups: dict[tuple[str, str], _Cand] = {}
    for si, tr in enumerate(traces):
        runs: dict[str, list[Call]] = defaultdict(list)

        def flush():
            for key, calls in runs.items():
                if len(calls) < MIN_POLL_RUNS:
                    continue
                by_lit: dict[str, list[Call]] = defaultdict(list)
                for c in calls:
                    by_lit[c.lit or c.shape].append(c)
                # a normalised shape collapses digits/strings/paths,
                # so `run-0`..`run-5` lands in one group. A polling loop
                # re-runs ONE literal command; several distinct literals is
                # a parameter sweep and needs different advice.
                loops = [lc for lc in by_lit.values() if len(lc) >= MIN_POLL_RUNS]
                if not loops:
                    waiting = [c for c in calls if _SLEEP_SEGMENT_RE.search(c.lit or "")]
                    if len(waiting) >= MIN_POLL_RUNS:
                        loops = [waiting]
                if loops:
                    loop_calls = [c for lc in loops for c in lc]
                    cand = groups.get((key, "polling_loop"))
                    if cand is None:
                        cand = groups[(key, "polling_loop")] = _Cand("polling_loop", loop_calls[0].shape)
                    cand.times += len(loop_calls)
                    cand.sessions.add(si)
                    for c in loop_calls:
                        cand.turns.add((si, c.turn))
                elif len(by_lit) >= 2:
                    cand = groups.get((key, "parameter_sweep"))
                    if cand is None:
                        cand = groups[(key, "parameter_sweep")] = _Cand("parameter_sweep", calls[0].shape)
                    cand.times += len(calls)
                    cand.sessions.add(si)
                    for c in calls:
                        cand.turns.add((si, c.turn))
            runs.clear()

        for c in tr.calls:
            if c.kind == "edit":
                flush()
            elif c.kind == "bash":
                runs[c.key].append(c)
        flush()
    return list(groups.values())


def detect_check_turns(traces: list[Trace]) -> list[_Cand]:
    groups: dict[str, _Cand] = {}
    for si, tr in enumerate(traces):
        for ut in tr.uturns:
            if len(ut["calls"]) != 1:
                continue
            call = tr.calls[ut["calls"][0]]
            if call.kind != "bash" or not call.cmd_ok_check or call.ok is not True:
                continue
            if sum(tr.turns[t].reply_chars for t in ut["turns"]) > SHORT_REPLY_CHARS:
                continue
            cand = groups.get(call.key)
            if cand is None:
                cand = groups[call.key] = _Cand("check_only_turn", call.shape)
            cand.times += 1
            cand.sessions.add(si)
            for t in ut["turns"]:
                cand.turns.add((si, t))
    return [c for c in groups.values() if c.times >= MIN_CHECK_TURNS]


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

PriceFn = Callable[[str, int, int, int, int, int, int], "float | None"]


def _price_turns(traces: list[Trace], turns: Iterable[tuple[int, int]], price: PriceFn) -> dict[str, Any]:
    fresh = cr = cc = out = 0
    cost = 0.0
    priced = unpriced = 0
    for si, ti in turns:
        t = traces[si].turns[ti]
        fresh += t.fresh
        cr += t.cr
        cc += t.cc
        out += t.out
        c = price(t.model, t.fresh, t.out, t.cr, t.cc, t.cc1h, t.cc5m)
        if c is None or (c == 0 and (t.fresh or t.cr or t.cc or t.out)):
            unpriced += 1
        else:
            cost += c
            priced += 1
    input_tokens = fresh + cr + cc
    return {
        "input_tokens": input_tokens,
        "cache_read_tokens": cr,
        "output_tokens": out,
        "total_tokens": input_tokens + out,
        "cost_usd": round(cost, 4),
        "cost_complete": unpriced == 0,
        "turns": priced + unpriced,
    }


def _finish_example(text: str, redact: Redactor) -> str:
    if redact.failed:
        return WITHHELD
    out = redact(text)
    if redact.failed:
        return WITHHELD
    out = _WS_RE.sub(" ", out).strip()
    return out if len(out) <= EXAMPLE_CAP else out[:EXAMPLE_CAP - 3] + "..."


def analyse(traces: list[Trace], price: PriceFn, redact: Redactor,
            deadline: float | None = None) -> tuple[list[dict], dict[str, Any], bool]:
    """Run all detectors. Returns (candidates, union totals, partial)."""
    cands = detect_check_turns(traces) + detect_polling(traces) + detect_subagents(traces)
    partial = False
    try:
        seq, seq_partial = detect_sequences(traces, deadline)
        cands += seq
        partial = partial or seq_partial
    except MemoryError:
        partial = True
    scored = []
    for c in cands:
        stats = _price_turns(traces, c.turns, price)
        scored.append((stats["total_tokens"], stats["cost_usd"], c, stats))
    scored.sort(key=lambda r: (-r[0], -r[1], -r[2].times))
    top = scored[:MAX_CANDIDATES]
    out = []
    union: set[tuple[int, int]] = set()
    for _, _, c, stats in top:
        union |= c.turns
        entry = {
            "kind": c.kind,
            "example": _finish_example(c.example, redact),
            "times_seen": c.times,
            "sessions_seen": len(c.sessions),
            "tokens": {k: stats[k] for k in ("input_tokens", "cache_read_tokens", "output_tokens", "total_tokens")},
            "cost_usd": stats["cost_usd"],
            "cost_complete": stats["cost_complete"],
            "suggestion": _SUGGESTIONS[c.kind],
            "basis": BASIS,
        }
        if "sequence_length" in c.extra:
            entry["sequence_length"] = c.extra["sequence_length"]
        if c.kind == "templated_subagent":
            entry["tokens_scope"] = "launching turns only; subagent transcripts are not attributed"
        out.append(entry)
    totals = _price_turns(traces, union, price)
    return out, totals, partial


def summary_line(result: dict[str, Any]) -> str:
    """One human line for `coach` output."""
    status = result.get("status")
    if status == "not_measurable":
        return f"Deterministic candidates: {result.get('reason') or 'not measurable on this runtime'}"
    if status == "error":
        return "Deterministic candidates: unavailable (analysis error, coach output unaffected)"
    window = f"last {result.get('days', 30)}d, {result.get('sessions_scanned', 0)} sessions"
    cands = result.get("candidates") or []
    suffix = " (partial: time budget reached)" if result.get("partial") else ""
    if not cands:
        return f"Deterministic candidates: none found ({window}; {BASIS}){suffix}"
    tot = result.get("totals") or {}
    top = cands[0]
    return (
        f"Deterministic candidates: {len(cands)} workflow pattern(s) that could be plain code; "
        f"the turns that ran them used {tot.get('total_tokens', 0):,} tokens "
        f"(~${tot.get('cost_usd', 0):.2f} API-equivalent) "
        f"({window}; {BASIS}). Top: {top['kind']} x{top['times_seen']}: {top['example'][:80]}{suffix}"
    )


# ---------------------------------------------------------------------------
# Cache (Token Optimizer's own data dir; redacted shapes and counts only)
# ---------------------------------------------------------------------------

CACHE_NAME = "deterministic-candidates-cache.json"
_CACHE_MAX_ENTRIES = 6


def cache_key(runtime: str, days: int, cap: int, tier: str, files: list[tuple[Path, float]]) -> str:
    newest = 0
    for _, mt in files:
        newest = max(newest, int(mt * 1e9))
    raw = f"{ALGO_VERSION}|{runtime}|{days}|{cap}|{tier}|{newest}|{len(files)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def cache_load(cache_dir: Path | None, key: str, now: float | None = None) -> dict | None:
    if cache_dir is None:
        return None
    try:
        data = json.loads((Path(cache_dir) / CACHE_NAME).read_text(encoding="utf-8"))
        entry = data.get("entries", {}).get(key)
        if not isinstance(entry, dict) or not isinstance(entry.get("result"), dict):
            return None
        result = entry["result"]
        if result.get("partial"):
            if (now if now is not None else time.time()) - float(entry.get("saved_at") or 0) > PARTIAL_CACHE_TTL_S:
                return None
        return result
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def cache_store(cache_dir: Path | None, key: str, result: dict, now: float | None = None) -> bool:
    if cache_dir is None:
        return False
    try:
        directory = Path(cache_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / CACHE_NAME
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entries = data.get("entries") if isinstance(data, dict) else None
        except (OSError, ValueError):
            entries = None
        entries = entries if isinstance(entries, dict) else {}
        entries[key] = {"saved_at": now if now is not None else time.time(), "result": result}
        if len(entries) > _CACHE_MAX_ENTRIES:
            for old in sorted(entries, key=lambda k: float(entries[k].get("saved_at") or 0))[:len(entries) - _CACHE_MAX_ENTRIES]:
                entries.pop(old, None)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"entries": entries}), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(
    runtime: str,
    files: list[tuple[Path, float]],
    price: PriceFn,
    *,
    days: int = 30,
    budget_s: float = 8.0,
    max_sessions: int = 60,
    cache_dir: Path | None = None,
    tier: str = "",
    use_cache: bool = True,
    redact: Redactor | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Scan ``files`` (newest first) and return the deterministic_candidates block.

    Never raises: any internal failure comes back as ``status: "error"`` so
    ``coach --json`` cannot fail because of this block.
    """
    started = time.monotonic()
    base: dict[str, Any] = {
        "runtime": runtime, "days": days, "basis": BASIS, "tokens_note": USED_NOT_SAVED_NOTE, "partial": False,
        "candidates": [], "runtime_support": runtime_support_table(),
    }
    try:
        if not RUNTIME_SUPPORT.get(runtime, {}).get("measurable"):
            nm = not_measurable(runtime)
            base.update(status="not_measurable", reason=nm["reason"], sessions_scanned=0, not_measurable=[nm])
            base["summary"] = summary_line(base)
            return base
        files = sorted(files, key=lambda f: f[1], reverse=True)
        considered = files[:max(1, max_sessions)]
        key = cache_key(runtime, days, max_sessions, tier, considered)
        if use_cache:
            hit = cache_load(cache_dir, key)
            if hit is not None:
                hit = dict(hit)
                hit["cached"] = True
                return hit
        redact = redact or default_redactor()
        keys = _KeyMaker(redact)
        progress = Throttle(progress) if progress else None
        extract = EXTRACTORS[runtime]
        parse_deadline = started + budget_s * 0.7
        total_deadline = started + budget_s
        seen_ids: set = set()
        traces: list[Trace] = []
        partial = False
        scanned = 0
        for i, (path, _mt) in enumerate(considered):
            # >=: a spent budget (0.0 included) must stop even when the clock has
            # not ticked since `started`. Windows' monotonic clock moves in ~15 ms
            # steps, so a strict > let a zero budget scan every file.
            if time.monotonic() >= parse_deadline:
                partial = True
                break
            if progress:
                progress(f"deterministic-candidates: reading session {i + 1}/{len(considered)}")
            try:
                tr = extract(path, keys, seen_ids, parse_deadline)
            except BudgetExceeded:
                partial = True
                break
            except OSError:
                continue
            scanned += 1
            if tr is not None:
                traces.append(tr)
        if len(files) > len(considered):
            base["sessions_not_scanned_cap"] = len(files) - len(considered)
        cands, totals, a_partial = analyse(traces, price, redact, total_deadline)
        partial = partial or a_partial
        base.update(
            status="ok" if cands else "no_data",
            candidates=cands,
            totals=totals,
            partial=partial,
            sessions_scanned=scanned,
            sessions_found=len(files),
            elapsed_s=round(time.monotonic() - started, 2),
            cached=False,
        )
        base["summary"] = summary_line(base)
        if use_cache:
            cache_store(cache_dir, key, base)
        return base
    except Exception as exc:  # the coach must never fail because of this block
        base.update(status="error", error=f"{type(exc).__name__}: {str(exc)[:120]}", partial=True,
                    sessions_scanned=0, summary="Deterministic candidates: unavailable (analysis error, coach output unaffected)")
        return base
