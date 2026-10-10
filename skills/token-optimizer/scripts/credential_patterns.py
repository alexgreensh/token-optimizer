"""Shared credential detection and redaction for Token Optimizer.

Provides compiled regex patterns for common API keys, tokens, and secrets,
plus scan/redact functions usable by bash compression, read cache, and
tool archive writers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# (label, compiled_regex) pairs. Label is used in redaction placeholders.
CREDENTIAL_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("AWS access key",          re.compile(r"AKIA[0-9A-Z]{16}")),
    # Anthropic first so a sk-ant- key keeps its own label; the generic sk- class
    # carries "_" and "-" (sk-proj-..., sk-ant-api03-...) exactly like the TS engines.
    ("Anthropic key",           re.compile(r"sk-ant-[a-zA-Z0-9_\-]{20,}")),
    ("OpenAI/Anthropic key",    re.compile(r"sk-[a-zA-Z0-9_\-]{20,}")),
    ("GitHub PAT classic",      re.compile(r"ghp_[a-zA-Z0-9]{36}")),
    ("GitHub OAuth token",      re.compile(r"gho_[a-zA-Z0-9]{36}")),
    ("GitHub user-to-server token", re.compile(r"ghu_[a-zA-Z0-9]{36}")),
    ("GitHub server token",     re.compile(r"ghs_[a-zA-Z0-9]{36}")),
    ("GitHub refresh token",    re.compile(r"ghr_[a-zA-Z0-9]{36}")),
    ("GitHub fine-grained PAT", re.compile(r"github_pat_[a-zA-Z0-9_]{20,}")),
    ("npm token",               re.compile(r"npm_[a-zA-Z0-9]{36}")),
    # Current Slack shapes: long dash-separated bodies (xoxb-1-2-<24 secret>),
    # app-level tokens, and incoming-webhook URLs (the URL path IS the secret).
    # Listed before the short legacy rows so the whole token is claimed, not just
    # its numeric head. The legacy rows below still catch bodies under 20 characters.
    ("Slack token",             re.compile(r"xox[bpa]-[0-9A-Za-z\-]{20,}")),
    ("Slack app-level token",   re.compile(r"xapp-\d-[A-Z0-9]+-\d+-[0-9a-f]+")),
    ("Slack webhook URL",       re.compile(r"https://hooks\.slack\.com/services/\S+")),
    ("Slack bot token",         re.compile(r"xoxb-[0-9]+-[a-zA-Z0-9]+")),
    ("Slack user token",        re.compile(r"xoxp-[0-9]+-[a-zA-Z0-9]+")),
    ("Slack app token",         re.compile(r"xoxa-[0-9]+-[a-zA-Z0-9]+")),
    ("Stripe live key",         re.compile(r"sk_live_[a-zA-Z0-9]{24,}")),
    ("Stripe test key",         re.compile(r"sk_test_[a-zA-Z0-9]{24,}")),
    ("Stripe restricted key",   re.compile(r"rk_live_[a-zA-Z0-9]{24,}")),
    ("HuggingFace token",       re.compile(r"hf_[a-zA-Z0-9]{34}")),
    # GitLab: personal/project/group access (glpat), deploy (gldt), runner (glrt),
    # CI build (glcbt), pipeline trigger (glptt), feed (glft), incoming mail (glimt),
    # agent for Kubernetes (glagent), SCIM (glsoat). Newer "routable" tokens carry
    # dots (glpat-<body>.01.<suffix>), so the body class includes ".".
    ("GitLab token",            re.compile(r"gl(?:pat|dt|rt|cbt|ptt|ft|imt|agent|soat)-[a-zA-Z0-9_.\-]{20,}")),
    # M-16: negative lookahead so the Bearer pattern doesn't re-match text
    # inside its own redaction placeholder [CREDENTIAL REDACTED: Bearer token].
    # The lookahead checks the text BEFORE Bearer, but Python regex doesn't
    # support variable-width lookbehinds. Instead, redact_credentials protects
    # placeholders with a sentinel before running patterns. The lookahead here
    # is a defense-in-depth for direct pattern.search() callers.
    ("Bearer token",            re.compile(r"Bearer\s+[a-zA-Z0-9\-._~+/]+=*", re.I)),
    ("Google API key",          re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("Google OAuth token",      re.compile(r"ya29\.[0-9A-Za-z_\-]{20,}")),
    # Linear on adversarial input. The old form rescanned a long base64url run from every
    # "eyJ" inside it (4 s at 100 KB). The first segment may not contain another "eyJ"
    # (a JWT header never does), so each start stops at the next candidate. The pattern
    # still opens with the literal, so clean text keeps the engine's fast prefix scan,
    # and a JWT glued to other characters (MEDX-123456-eyJ..., xeyJ...) still matches.
    ("JWT",                     re.compile(
        r"eyJ(?:(?!eyJ)[A-Za-z0-9_\-]){10,}"
        r"\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    # Whole block: the key body is the secret, the BEGIN line is not. A block
    # with no END (cut by a length limit, or still being typed) runs to the end
    # of the text so a truncated body is never left behind.
    ("PEM private key",         re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)")),
    ("Database URI",            re.compile(r"(?:postgres|postgresql|mysql|mongodb|mongodb\+srv|redis)://[^:\s/]+:[^@\s]+@", re.I)),
    ("HTTP basic auth URL",     re.compile(r"https?://[^:\s/@]+:[^@\s]+@", re.I)),
    # Credentials passed as URL query/matrix parameters OR OAuth-implicit-flow
    # fragment params (e.g. ?token=..., ?api_key=..., ;password=..., #access_token=...).
    # The named `keep` group captures the "?name="/"#name=" prefix so redaction
    # preserves the parameter name and blanks only the value (see redact_credentials).
    # The value class stops at the next delimiter (& # ; whitespace quote < >) but
    # otherwise matches EVERYTHING — including brackets — so a secret that itself
    # contains a `[` (common in passwords) is redacted whole, not leaked past the
    # bracket. To still avoid re-wrapping an already-inserted "[CREDENTIAL REDACTED:
    # ...]" placeholder (when the value is itself another credential shape an earlier
    # pattern redacted, e.g. ?token=<Bearer ...>), a negative lookahead skips a value
    # that begins with the placeholder rather than excluding brackets from real values.
    ("URL auth param",          re.compile(
        r"(?P<keep>[?&#;](?:authorization|access[_-]?token|refresh[_-]?token|client[_-]?secret"
        r"|session[_-]?token|id[_-]?token|api[_-]?key|sessionid|session|password|passwd|signature"
        r"|secret|bearer|token|auth|sig|pwd|key|jwt)=)(?!\[CREDENTIAL REDACTED:)[^&#;\s\"'<>]+",
        re.I,
    )),
    # M-12: mysql -p<password> (inline password after -p with no space).
    # The -p flag is special: the password immediately follows with no = or space.
    # N-3: re.I so "MySQL -pSECRET" (capitalized client name, as MySQL ships
    # it) is redacted too; the anchor gate already lowercases, so gating is
    # unaffected.
    ("MySQL password flag",     re.compile(
        r"(?P<keep>\bmysql\s+.*?(?<!\S)(?-i:-p)\s*)(?!-)(?!\[CREDENTIAL REDACTED:)"
        r"(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s\"']+)",
        re.I,
    )),
    # M-12: PGPASSWORD=, MYSQL_PWD=, and similar *_PASSWORD= / *_PWD= env assignments.
    # These appear as shell command prefixes (FOO=bar cmd ...) or in config output.
    ("Database env password",   re.compile(
        r"(?P<keep>\b(?:PGPASSWORD|MYSQL_PWD|REDIS_PASSWORD|MONGO_PASSWORD|DB_PASSWORD"
        r"|DATABASE_PASSWORD|PGPASSWD)=(?P<oq>[\"\'])?)(?!\[CREDENTIAL REDACTED:)"
        # An opened quote runs to its twin (a value with spaces is one secret);
        # an unquoted value stops at whitespace or a quote.
        r"(?(oq)(?:(?!(?P=oq))[^\n])+|[^\s\"'\n]+)",
        re.I,
    )),
    # M-12: AWS secret access key (40-char base64). Distinct from the access key
    # (AKIA prefix). Secret keys are mixed-case base64, 40 chars, no prefix.
    # Use a context prefix to avoid false positives on random 40-char base64
    # strings. No trailing \b because the secret may end with = or + (non-word).
    ("AWS secret key",          re.compile(
        r"(?P<keep>\b(?:aws_secret_access_key|aws_secret|secret_access_key|SecretAccessKey)[\"\'\s:=]+)"
        r"(?!\[CREDENTIAL REDACTED:)[A-Za-z0-9/+=]{40}",
        re.I,
    )),
    # Inline CLI password flags. Two patterns:
    # (a) Long forms (--password=V, --password V, --passwd=V, --passcode=V,
    #     --auth-token=V) — unambiguous, always redact.
    # (b) Short forms (-p V, -a V) restricted to known password-carrying
    #     commands (sshpass, mysql, mariadb, redis-cli) to avoid false
    #     positives on -p port/plugin/preserve flags in other commands.
    # The named `keep` group captures the flag (+ command context for short
    # forms) so redaction preserves it and blanks only the value.
    ("CLI password flag (long)", re.compile(
        r"(?P<keep>(?:--password|--passwd|--passcode|--auth-token)(?![\w-])(?:\s*=\s*|\s+))"
        r"(?!-)(?!\[CREDENTIAL REDACTED:)"
        r"(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s\"']+)",
        re.I,
    )),
    ("CLI password flag (short)", re.compile(
        r"(?P<keep>sshpass\b.*?(?<!\S)(?-i:-p)\s*"
        r"|redis-cli\b.*?(?<!\S)(?-i:-a)\s+"
        r"|mariadb\b.*?(?<!\S)(?-i:-p)\s*)"
        r"(?!-)(?!\[CREDENTIAL REDACTED:)"
        r"(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s\"']+)",
        re.I,
    )),
]

# Bare compiled patterns list for backward compat with bash_compress.py
PATTERNS_ONLY: List["re.Pattern[str]"] = [pat for _, pat in CREDENTIAL_PATTERNS]

# ---------------------------------------------------------------------------
# H-8: fast pre-check for credential redaction.
#
# The old redact_credentials ran 23 sequential re.sub() calls on the full
# text unconditionally: 97ms for 10K lines, 675ms for 50K lines, even when
# the text contained NO credentials (the common case for command output).
# Python's re engine uses backtracking, not a DFA, so combining all
# patterns into a single alternation is actually SLOWER (154ms for 10K
# clean lines) due to the complex NFA state per character.
#
# The fix: a fast prefix scan using simple string containment checks
# before any regex runs. If none of the credential prefixes appear in the
# text, skip all 23 re.sub() calls entirely. This makes clean text O(n)
# with a tiny constant (a single str.find pass per prefix), while text
# with credentials still gets the full sequential redaction (correctness
# preserved, no regex complexity change).
#
# The prefix list is derived from the literal prefixes of each pattern:
# "AKIA", "sk-", "ghp_", "gho_", "ghs_", "ghr_", "github_pat_", "npm_",
# "xoxb-", "xoxp-", "xoxa-", "sk_live_", "rk_live_", "hf_", "gl*-", "Bearer",
# "AIza", "ya29.", "eyJ", "-----BEGIN", and the URL scheme prefixes for
# database/basic-auth URIs. The URL auth param pattern has no single
# literal prefix (it matches parameter names), so we check for "=" as a
# coarse pre-filter — but only if other prefixes didn't already match.
# ---------------------------------------------------------------------------
_CREDENTIAL_PREFIXES: Tuple[str, ...] = (
    "AKIA", "sk-", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_",
    "npm_", "xoxb-", "xoxp-", "xoxa-", "xapp-", "sk_live_", "sk_test_", "rk_live_", "hf_",
    "glpat-", "gldt-", "glrt-", "glcbt-", "glptt-", "glft-", "glimt-", "glagent-", "glsoat-",  # GitLab
    "Bearer", "bearer", "AIza", "ya29.", "eyJ",
    "-----BEGIN",  # PEM private key
    "postgres://", "postgresql://", "mysql://", "mongodb://",
    "mongodb+srv://", "redis://",  # database URI
    "http://", "https://",  # basic auth URL (coarse, but covers the pattern)
    # M-12: new credential prefixes
    "mysql ",  # mysql -p<password>
    "PGPASSWORD=", "MYSQL_PWD=", "REDIS_PASSWORD=", "MONGO_PASSWORD=",
    "DB_PASSWORD=", "DATABASE_PASSWORD=", "PGPASSWD=",
    "aws_secret", "secret_access_key", "SecretAccessKey",
)
# URL auth param parameter names (lowercase, checked case-insensitively).
_URL_AUTH_PARAM_NAMES: Tuple[str, ...] = (
    "authorization=", "access_token=", "access-token=", "refresh_token=",
    "refresh-token=", "client_secret=", "client-secret=", "session_token=",
    "session-token=", "id_token=", "id-token=", "api_key=", "api-key=",
    "sessionid=", "session=", "password=", "passwd=", "signature=",
    "secret=", "bearer=", "token=", "auth=", "sig=", "pwd=", "key=",
    "jwt=",
)


def _text_may_contain_credentials(text: str) -> bool:
    """Fast prefix scan: return True if any credential prefix appears in text.

    This is a coarse pre-filter using str.find (C-level, no regex engine).
    False negatives would be a security bug, so every prefix is checked.
    False positives are fine — the full regex suite runs and finds nothing.
    """
    for prefix in _CREDENTIAL_PREFIXES:
        if prefix in text:
            return True
    # URL auth param names are case-insensitive in the pattern. Use a
    # lowercase copy for the check.
    lower = text.lower()
    for name in _URL_AUTH_PARAM_NAMES:
        if name in lower:
            return True
    return False


# ---------------------------------------------------------------------------
# Custom (user-defined) redaction patterns.
#
# Organizations have secret shapes no built-in list can know about: internal
# API keys, service tokens, record identifiers. Users add them in a JSON file
# instead of editing this module:
#
#   <runtime-home>/token-optimizer/redact-patterns.json   (default location)
#
# or point TOKEN_OPTIMIZER_REDACT_PATTERNS_FILE at another path (process env
# first, then the settings.json "env" block, like the other TOKEN_OPTIMIZER_*
# knobs). Format:
#
#   {"patterns": [
#       "acme_[A-Za-z0-9]{32}",
#       {"label": "Acme service token", "regex": "(?P<keep>ACME_TOKEN=)\\S+",
#        "ignore_case": true}
#   ]}
#
# Custom patterns run BEFORE the built-ins: a fragment like
# "MEDX-123456-<jwt>" must be claimed whole by the org pattern, not left as a
# readable id prefix next to a "[CREDENTIAL REDACTED: JWT]" placeholder. They
# still cannot touch a "[CREDENTIAL REDACTED: ...]" placeholder that was
# already in the input (re-running redaction on archived text stays
# idempotent). They apply to redact_credentials() and scan_for_credentials(),
# i.e. everything written to disk. CREDENTIAL_PATTERNS and PATTERNS_ONLY stay
# the built-in set (import-time constants, used by compressors to decide which
# lines to keep verbatim).
#
# Loading is lazy (first redaction call) and cached per process. Two failure
# tiers, deliberately different:
#
#   * A bad ENTRY (invalid regex, unsafe shape, over-broad) is skipped on its
#     own with a recorded error; the rest of the file still loads.
#   * A FILE that was configured or is present but cannot be trusted
#     (unreadable, invalid JSON, wrong shape, a relative env path) makes
#     redact_credentials() raise RedactionConfigError — every disk writer in
#     the pipeline then skips persisting that content rather than storing text
#     the user's own patterns were meant to cover. Fail closed, never open.
# ---------------------------------------------------------------------------
CUSTOM_PATTERNS_FILE_ENV = "TOKEN_OPTIMIZER_REDACT_PATTERNS_FILE"
CUSTOM_PATTERNS_FILENAME = "redact-patterns.json"
_CUSTOM_STATUS_FILENAME = "redact-patterns-status.json"
_CUSTOM_PROBE_CACHE_FILENAME = "redact-probe-cache.json"
_CUSTOM_DEFAULT_LABEL = "custom pattern"
_CUSTOM_MAX_FILE_BYTES = 1_048_576
_CUSTOM_MAX_PATTERNS = 200
_CUSTOM_MAX_REGEX_CHARS = 1000
_CUSTOM_MAX_LABEL_CHARS = 60
# Hard wall-clock budget for the whole safety-probe subprocess, and the max
# number of probe passes (each timeout rejects the in-flight pattern and the
# remainder get one more pass).
_CUSTOM_PROBE_TIMEOUT_SECONDS = 2.0
_CUSTOM_PROBE_MAX_PASSES = 3
# Probe children must not flash a console window when the hook runs under a
# GUI process on Windows; 0 is a no-op off Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Characters that would break the "[CREDENTIAL REDACTED: <label>]" placeholder
# (and _PLACEHOLDER_RE, which stops at "]") or the one-line archive format.
_LABEL_UNSAFE_RE = re.compile(r"[\]\[\x00-\x1f\x7f]")

_BUILTIN_KEYS = frozenset((pat.pattern, pat.flags) for _, pat in CREDENTIAL_PATTERNS)


class RedactionConfigError(RuntimeError):
    """A configured custom redaction-pattern file could not be trusted.

    Raised by redact_credentials() and scan_for_credentials() when the file the
    user pointed us at (or dropped at the default location) is present but
    unloadable — unreadable, malformed JSON, wrong top-level shape, or a
    relative TOKEN_OPTIMIZER_REDACT_PATTERNS_FILE value. Distinct from a
    per-entry rejection so callers that persist text can fail CLOSED: skip the
    write entirely instead of storing content that org-specific secret shapes
    were configured to cover.
    """


class _CustomPatternState:
    """Result of one load: the compiled patterns, where they came from, and
    human-readable problems (for the security report and stderr)."""

    __slots__ = ("patterns", "source", "errors", "duplicates", "rejected",
                 "failed", "failure", "file_hash")

    def __init__(self) -> None:
        self.patterns: List[Tuple[str, "re.Pattern[str]"]] = []
        self.source: Optional[str] = None
        self.errors: List[str] = []
        self.duplicates: int = 0
        self.rejected: int = 0
        # failed/failure: the file itself could not be trusted — redaction must
        # fail closed (raise) rather than run with fewer patterns than the user
        # configured.
        self.failed: bool = False
        self.failure: Optional[str] = None
        # sha256 of the file bytes actually read; keys the probe-verdict cache
        # and the once-per-hash user warning. None when nothing was read.
        self.file_hash: Optional[str] = None


_CUSTOM_STATE: Optional[_CustomPatternState] = None


def _warn(msg: str) -> None:
    try:
        print(f"[token-optimizer] {msg}", file=sys.stderr)
    except Exception:
        pass


def _settings_env_value(name: str) -> str:
    """Read ``name`` from the settings.json "env" block. Never raises.

    Delegates to runtime_env.settings_env_value — the same resolution the other
    TOKEN_OPTIMIZER_* env knobs use (the user's global settings.json only;
    project settings are not read by hooks)."""
    try:
        from runtime_env import settings_env_value
        return settings_env_value(name)
    except Exception:
        return ""


def _default_custom_patterns_path() -> Optional[Path]:
    try:
        from runtime_env import runtime_home
        return runtime_home() / "token-optimizer" / CUSTOM_PATTERNS_FILENAME
    except Exception:
        return None


def _resolve_custom_patterns_path() -> Tuple[Optional[Path], bool, Optional[str]]:
    """Return (path, explicit, error). ``explicit`` is True when the user named
    the file via TOKEN_OPTIMIZER_REDACT_PATTERNS_FILE. ``error`` is set when the
    env value is unusable — a relative path would resolve against whatever cwd
    the hook happened to launch with, so a repo could silently substitute its
    own pattern file; only absolute paths are accepted."""
    raw = os.environ.get(CUSTOM_PATTERNS_FILE_ENV, "").strip()
    if not raw:
        raw = _settings_env_value(CUSTOM_PATTERNS_FILE_ENV)
    if not raw:
        return _default_custom_patterns_path(), False, None
    # Windows users often paste a quoted path into env vars; strip one
    # surrounding quote pair before expansion.
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1].strip()
    if not raw:
        return _default_custom_patterns_path(), False, None
    path = Path(os.path.expanduser(os.path.expandvars(raw)))
    if not path.is_absolute():
        return (None, True,
                f"{CUSTOM_PATTERNS_FILE_ENV} must be an absolute path "
                f"(after ~ and env expansion), got {raw!r}")
    return path, True, None


def _clean_label(raw: object) -> str:
    if not isinstance(raw, str):
        return _CUSTOM_DEFAULT_LABEL
    label = _LABEL_UNSAFE_RE.sub("", raw).strip()
    label = " ".join(label.split())[:_CUSTOM_MAX_LABEL_CHARS].strip()
    return label or _CUSTOM_DEFAULT_LABEL


def _reject_entry(state: _CustomPatternState, msg: str) -> None:
    """Skip one entry and record why. File-level failures use state.failed
    instead — an entry rejection leaves the rest of the file usable."""
    state.errors.append(msg)
    state.rejected += 1


# --- ReDoS guards ------------------------------------------------------------
#
# Custom regexes run on the hook hot path against arbitrary tool output, and
# Python's re engine backtracks: a pathological pattern like (x+x+)+y does not
# error, it just burns exponential time and wedges the hook mid-write. Two
# layers keep that out:
#
#   1. _redos_lint — a syntactic check run at load time that rejects the two
#      classic catastrophic shapes: an unbounded-quantified group whose body
#      contains an unbounded quantifier ((x+x+)+), and an unbounded-quantified
#      alternation whose branches overlap ((a|aa)+, including an empty branch).
#   2. _probe_candidates — anything subtler is measured, not guessed: every
#      surviving pattern is run in a subprocess against adversarial probe
#      strings under a hard time budget; a pattern that times out is rejected.
#
# The probe verdict is cached on disk keyed by the pattern file's sha256, so
# the subprocess only runs when the file changes — never per hook invocation.


def _quantifier_at(src: str, i: int) -> Tuple[int, bool]:
    """Quantifier starting at ``i``: return (length, is_unbounded).

    Unbounded means the repeat has no fixed upper bound: ``*``, ``+`` or
    ``{n,}`` (lazy ``?`` suffix included). ``?`` and ``{n}``/``{n,m}`` are
    bounded. Returns (0, False) when the char is not a quantifier — e.g. a
    literal ``{`` that does not form ``{m}``/``{m,n}`` syntax.
    """
    if i >= len(src):
        return 0, False
    ch = src[i]
    if ch in "*+":
        j = i + 1
        if j < len(src) and src[j] == "?":
            j += 1
        return j - i, True
    if ch == "?":
        j = i + 1
        if j < len(src) and src[j] == "?":
            j += 1
        return j - i, False
    if ch == "{":
        m = re.match(r"\{(\d+)(?:,(\d*))?\}\??", src[i:])
        if not m:
            return 0, False
        # {n} and {n,m} are bounded; only the open-ended {n,} form is not.
        unbounded = "," in m.group(0) and not m.group(2)
        return m.end(), unbounded
    return 0, False


def _redos_lint(src: str) -> Optional[str]:
    """Static check for catastrophic-backtracking shapes; error text or None.

    Tracks a stack of group frames. Each frame records whether its body
    contains an unbounded quantifier (``inner``) and the first literal char of
    each top-level ``|`` branch. When a group closes with an unbounded
    quantifier, an ``inner`` body means nested unbounded quantifiers and
    overlapping branch starts mean an ambiguous alternation — both reject.
    A quantified or inner-unbounded group marks its parent's ``inner`` too, so
    shapes like ``((ab)+)+`` are caught at the outer level.
    """
    def _frame() -> dict:
        # first: first literal char of the branch being scanned (None when it
        # starts with a class/group/dot — i.e. we cannot tell); empty: branch
        # has consumed no atom yet.
        return {"inner": False, "branches": [], "first": None,
                "awaiting_first": True, "empty": True}

    stack = [_frame()]
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        cur = stack[-1]
        if ch == "\\":
            if i + 1 >= n:
                break
            nxt = src[i + 1]
            if cur["awaiting_first"] and nxt not in "AbBZGz":
                # Class escapes (\d \w \s ...) and the rest: the first char is
                # unknowable statically. Escaped literals (\. \\ \[ ...) are.
                cur["first"] = nxt if nxt not in "dDsSwW" else None
                cur["awaiting_first"] = False
                cur["empty"] = False
            i += 2
            continue
        if ch == "[":
            j = i + 1
            if j < n and src[j] == "^":
                j += 1
            if j < n and src[j] == "]":
                j += 1
            while j < n:
                if src[j] == "\\":
                    j += 1
                elif src[j] == "]":
                    break
                j += 1
            if cur["awaiting_first"]:
                cur["first"] = None
                cur["awaiting_first"] = False
                cur["empty"] = False
            i = min(j + 1, n)
            continue
        if ch == "(":
            # (?P=name) is a backreference atom and (?#...) a comment: both end
            # at the next ")" and open no group frame.
            if src.startswith("(?P=", i) or src.startswith("(?#", i):
                end = src.find(")", i)
                if cur["awaiting_first"] and src.startswith("(?P=", i):
                    cur["first"] = None
                    cur["awaiting_first"] = False
                    cur["empty"] = False
                i = n if end < 0 else end + 1
                continue
            # (?imsx) sets flags inline — an atom, not a group.
            m = re.match(r"\(\?[aiLmsux]+\)", src[i:])
            if m:
                i += m.end()
                continue
            # Every other "(..." opens a group frame: (?: (?= (?! (?<= (<!
            # (?P<name> and scoped-flag (?imsx: bodies.
            if src.startswith("(?P<", i):
                gt = src.find(">", i)
                i = n if gt < 0 else gt + 1
            elif src[i:i + 4] in ("(?<=", "(?<!"):
                i += 4
            elif src.startswith("(?", i):
                j = i + 2
                while j < n and src[j] in "aiLmsux-":
                    j += 1
                i = j + 1 if j < n and src[j] == ":" else i + 3
            else:
                i += 1
            stack.append(_frame())
            continue
        if ch == "|":
            cur["branches"].append((cur["first"], cur["empty"]))
            cur["first"] = None
            cur["awaiting_first"] = True
            cur["empty"] = True
            i += 1
            continue
        if ch == ")":
            if len(stack) <= 1:
                i += 1  # unmatched ")" — re.compile already reported it
                continue
            done = stack.pop()
            done["branches"].append((done["first"], done["empty"]))
            qlen, unbounded = _quantifier_at(src, i + 1)
            if qlen == 0 and i + 1 < n and src[i + 1] == "?":
                qlen = 1  # bounded (0-or-1) group repeat
            if unbounded:
                if done["inner"]:
                    return ("nested unbounded quantifiers can backtrack "
                            "exponentially (a repeated group containing "
                            "*, + or {n,})")
                firsts = [f for f, _e in done["branches"] if f is not None]
                if len(firsts) != len(set(firsts)) or any(
                        e for _f, e in done["branches"]):
                    return ("a repeated alternation with overlapping or empty "
                            "branches can backtrack exponentially")
            # Propagate: a quantified group, or one containing an unbounded
            # quantifier, makes the enclosing body ambiguously repeatable.
            parent = stack[-1]
            if done["inner"] or qlen:
                parent["inner"] = True
            if parent["awaiting_first"]:
                parent["first"] = None
                parent["awaiting_first"] = False
                parent["empty"] = False
            i += 1 + qlen
            continue
        if ch in "*+?{":
            qlen, unbounded = _quantifier_at(src, i)
            if qlen:
                if unbounded:
                    cur["inner"] = True
                i += qlen
                continue
            # else: a "{" that is not quantifier syntax — literal char below.
        # Literal atom (anchors ^$ and "." can't be pinned to a first char).
        if cur["awaiting_first"]:
            cur["first"] = ch if ch not in "^$." else None
            cur["awaiting_first"] = False
            cur["empty"] = False
        i += 1
    return None


# A deliberately boring text sample: prose, code-ish lines, a URL, an email —
# what tool output looks like when it holds no secrets. A custom pattern that
# matches a single character or eats most of this would shred every archived
# output, not just secrets.
_BENIGN_SAMPLE = (
    "The quick brown fox jumps over the lazy dog while the server hums.\n"
    "def compute_total(items):\n"
    "    return sum(item.price for item in items)\n"
    "See https://docs.example.com/setup for the install guide and notes.\n"
    "User alice@example.com ran 3 reports at 12:30 and logged off.\n"
    "warning: retry 2 of 5 in worker-7, elapsed 481 ms\n"
)
# Reject when a pattern's matches would cover more than this fraction of the
# benign sample (the `keep` group, which survives, is not counted).
_BENIGN_MAX_COVER = 0.5


def _broadness_error(pat: "re.Pattern[str]") -> Optional[str]:
    """Reject patterns that would redact nearly everything they see."""
    try:
        covered = 0
        limit = len(_BENIGN_SAMPLE) * _BENIGN_MAX_COVER
        for m in pat.finditer(_BENIGN_SAMPLE):
            width = m.end() - m.start()
            if "keep" in pat.groupindex and m.group("keep") is not None:
                ks, ke = m.span("keep")
                width -= ke - ks
            if width <= 1:
                return "matches single characters; too broad to be a credential shape"
            covered += width
            if covered > limit:
                return "matches most of an ordinary paragraph; too broad to be a credential shape"
    except Exception:
        return "could not be evaluated safely"
    return None


def _compile_custom_entry(entry: object, index: int, state: _CustomPatternState,
                          seen: set, candidates: list) -> None:
    where = f"entry {index}"
    if isinstance(entry, str):
        regex, label, ignore_case = entry, _CUSTOM_DEFAULT_LABEL, False
    elif isinstance(entry, dict):
        regex = entry.get("regex")
        label = _clean_label(entry.get("label"))
        ignore_case = entry.get("ignore_case", False)
        if not isinstance(ignore_case, bool):
            _reject_entry(state, f"{where}: ignore_case must be true or false")
            return
    else:
        _reject_entry(state, f"{where}: must be a string or an object with a \"regex\" key")
        return
    if not isinstance(regex, str) or not regex.strip():
        _reject_entry(state, f"{where}: regex is missing or empty")
        return
    if len(regex) > _CUSTOM_MAX_REGEX_CHARS:
        _reject_entry(state, f"{where}: regex longer than {_CUSTOM_MAX_REGEX_CHARS} characters")
        return
    try:
        pat = re.compile(regex, re.I if ignore_case else 0)
    except (re.error, RecursionError, OverflowError, ValueError) as exc:
        _reject_entry(state, f"{where}: invalid regex ({exc})")
        return
    # A pattern that matches the empty string would insert a placeholder
    # between every character of every archived output.
    try:
        matches_empty = pat.search("") is not None
    except Exception:
        matches_empty = True
    if matches_empty:
        _reject_entry(state, f"{where}: regex matches empty text")
        return
    lint = _redos_lint(regex)
    if lint:
        _reject_entry(state, f"{where}: unsafe regex — {lint}")
        return
    broad = _broadness_error(pat)
    if broad:
        _reject_entry(state, f"{where}: {broad}")
        return
    key = (pat.pattern, pat.flags)
    if key in _BUILTIN_KEYS or key in seen:
        state.duplicates += 1
        return
    seen.add(key)
    candidates.append((index, label, regex, ignore_case, pat))


# Probe strings: long single-class runs (where nested quantifiers blow up),
# near-miss shapes, and the benign sample. Kept small so a healthy pattern
# finishes in well under a millisecond each.
_PROBE_STRINGS: Tuple[str, ...] = (
    "a" * 512,
    "x" * 512,
    "0" * 512,
    "_" * 512,
    "=" * 512,
    "Aa" * 256,
    "ab1_" * 128,
    "https://example.com/" + "a" * 512,
    "x" * 256 + "y",
    _BENIGN_SAMPLE,
)

# Self-contained probe program (stdlib only): reads {"patterns": [...],
# "probes": [...]} on stdin, prints BEGIN <i> before each pattern and DONE <i>
# (or ERR <i> <class>) after it. The BEGIN/DONE markers let the parent tell —
# after killing a timed-out subprocess — exactly which pattern was in flight.
_PROBE_SNIPPET = (
    "import json,re,sys\n"
    "spec=json.load(sys.stdin)\n"
    "probes=spec['probes']\n"
    "for it in spec['patterns']:\n"
    "    i=it['index']\n"
    "    print('BEGIN',i,flush=True)\n"
    "    try:\n"
    "        pat=re.compile(it['regex'], re.I if it.get('ignore_case') else 0)\n"
    "        for s in probes:\n"
    "            pat.search(s)\n"
    "    except Exception as e:\n"
    "        print('ERR',i,type(e).__name__,flush=True)\n"
    "        continue\n"
    "    print('DONE',i,flush=True)\n"
    "print('ALLDONE',flush=True)\n"
)


def _runtime_token_dir() -> Optional[Path]:
    try:
        from runtime_env import runtime_home
        return runtime_home() / "token-optimizer"
    except Exception:
        return None


def _probe_cache_path() -> Optional[Path]:
    d = _runtime_token_dir()
    return d / _CUSTOM_PROBE_CACHE_FILENAME if d is not None else None


def _cached_probe_verdicts(file_hash: Optional[str]) -> Optional[Dict[int, Optional[str]]]:
    """Probe verdicts for this exact file content, if a previous process
    already ran the probe for it. The cache lives under the runtime home so it
    is keyed to this machine's user, and the key is the file's sha256 — a file
    edit is a new hash and gets probed fresh."""
    path = _probe_cache_path()
    if not file_hash or path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rec = data.get(file_hash) if isinstance(data, dict) else None
        if isinstance(rec, dict) and rec.get("complete") and isinstance(rec.get("verdicts"), dict):
            return {int(k): v for k, v in rec["verdicts"].items()}
    except Exception:
        return None
    return None


def _cache_probe_verdicts(file_hash: Optional[str], verdicts: Dict[int, Optional[str]]) -> None:
    path = _probe_cache_path()
    if not file_hash or path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                data = {}
        data[file_hash] = {
            "verdicts": {str(k): v for k, v in verdicts.items()},
            "complete": True,
            "probed_at": datetime.now(timezone.utc).isoformat(),
        }
        # Keep the cache small: retain the newest few file hashes.
        while len(data) > 8:
            data.pop(next(iter(data)))
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


def _probe_pass(items: list) -> Tuple[Dict[int, Optional[str]], Optional[int], bool]:
    """One probe subprocess over ``items``.

    Returns (verdicts, inflight, finished): verdicts maps candidate index to
    None (passed) or an error string; inflight is the index mid-probe when the
    budget ran out (None unless the subprocess was killed); finished is True
    when the subprocess reached the end of the list.
    """
    payload = {
        "probes": list(_PROBE_STRINGS),
        "patterns": [{"index": idx, "regex": regex, "ignore_case": ic}
                     for idx, _label, regex, ic, _pat in items],
    }
    try:
        proc = subprocess.run(
            [sys.executable or "python3", "-c", _PROBE_SNIPPET],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=_CUSTOM_PROBE_TIMEOUT_SECONDS,
            creationflags=_NO_WINDOW,
        )
        out = proc.stdout or ""
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or ""
        out = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    except Exception:
        # No interpreter to probe with — nothing in this batch is verified.
        return ({idx: "safety probe could not run" for idx, *_ in items},
                None, True)
    verdicts: Dict[int, Optional[str]] = {}
    inflight: Optional[int] = None
    finished = False
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        tag = parts[0]
        if tag == "BEGIN":
            try:
                inflight = int(parts[1])
            except ValueError:
                pass
        elif tag == "DONE":
            try:
                verdicts[int(parts[1])] = None
            except ValueError:
                pass
            inflight = None
        elif tag == "ERR":
            why = parts[2] if len(parts) > 2 else "error"
            try:
                verdicts[int(parts[1])] = f"failed its safety probe ({why})"
            except ValueError:
                pass
            inflight = None
        elif tag == "ALLDONE":
            finished = True
    return verdicts, inflight, finished


def _probe_candidates(candidates: list) -> Dict[int, Optional[str]]:
    """Run every candidate pattern against the probe strings in a subprocess
    with a hard time budget. A pattern that cannot finish is rejected — the
    cost of a slow regex is paid on every archived output, forever, so only
    patterns proven fast are loaded. Rejects conservatively when the probe
    itself cannot run."""
    verdicts: Dict[int, Optional[str]] = {}
    pending = list(candidates)
    for _attempt in range(_CUSTOM_PROBE_MAX_PASSES):
        if not pending:
            break
        part, inflight, finished = _probe_pass(pending)
        verdicts.update(part)
        if inflight is not None:
            verdicts[inflight] = ("safety probe timed out; pattern may "
                                  "backtrack catastrophically")
        if finished:
            break
        pending = [c for c in pending if c[0] not in verdicts]
    for c in pending:
        verdicts.setdefault(c[0], "safety probe did not complete")
    return verdicts


def _run_safety_probe(state: _CustomPatternState, candidates: list) -> None:
    """Move candidates that pass the probe into state.patterns."""
    if not candidates:
        return
    verdicts = _cached_probe_verdicts(state.file_hash)
    if verdicts is None:
        verdicts = _probe_candidates(candidates)
        _cache_probe_verdicts(state.file_hash, verdicts)
    for index, label, _regex, _ic, pat in candidates:
        err = verdicts.get(index)
        if err:
            _reject_entry(state, f"entry {index}: {err}")
        else:
            state.patterns.append((label, pat))


def _persist_custom_status(state: _CustomPatternState) -> None:
    """Best-effort record of the last load under the runtime home, so the
    outcome is inspectable even when the hook that hit it is long gone."""
    if state.source is None and not state.failed:
        return  # feature not in use — don't create noise
    d = _runtime_token_dir()
    if d is None:
        return
    try:
        d.mkdir(parents=True, exist_ok=True)
        payload = {
            "source": state.source,
            "sha256": state.file_hash,
            "active": not state.failed,
            "failure": state.failure,
            "patterns_loaded": len(state.patterns),
            "entries_rejected": state.rejected,
            "duplicates_skipped": state.duplicates,
            "errors": list(state.errors)[:20],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp = d / (_CUSTOM_STATUS_FILENAME + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, d / _CUSTOM_STATUS_FILENAME)
    except Exception:
        pass


def _fail(state: _CustomPatternState, msg: str) -> _CustomPatternState:
    state.failed = True
    state.failure = msg
    state.errors.append(msg)
    return state


def _load_custom_patterns() -> _CustomPatternState:
    state = _CustomPatternState()
    try:
        path, explicit, resolve_error = _resolve_custom_patterns_path()
        if resolve_error is not None:
            state.source = os.environ.get(CUSTOM_PATTERNS_FILE_ENV, "").strip() or None
            return _fail(state, resolve_error)
        if path is None:
            return state
        state.source = str(path)
        try:
            is_file = path.is_file()
            size = path.stat().st_size if is_file else 0
        except OSError as exc:
            return _fail(state, f"could not stat file ({exc.__class__.__name__}: {exc})")
        if not is_file:
            if explicit:
                return _fail(
                    state,
                    f"{CUSTOM_PATTERNS_FILE_ENV} points to a path that is "
                    "missing or not a regular file")
            return state
        if size > _CUSTOM_MAX_FILE_BYTES:
            return _fail(state, "file is larger than 1 MB; not loaded")
        try:
            # utf-8-sig: Windows Notepad saves UTF-8 with a BOM.
            raw_bytes = path.read_bytes()
        except OSError as exc:
            return _fail(state, f"could not read file ({exc.__class__.__name__}: {exc})")
        state.file_hash = hashlib.sha256(raw_bytes).hexdigest()
        try:
            data = json.loads(raw_bytes.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError) as exc:
            return _fail(state, f"could not parse file ({exc.__class__.__name__}: {exc})")
        entries = data.get("patterns") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return _fail(state, 'top level must be an object with a "patterns" list')
        if len(entries) > _CUSTOM_MAX_PATTERNS:
            state.errors.append(
                f"{len(entries)} patterns listed; only the first {_CUSTOM_MAX_PATTERNS} are used"
            )
            entries = entries[:_CUSTOM_MAX_PATTERNS]
        seen: set = set()
        candidates: list = []
        for i, entry in enumerate(entries, 1):
            _compile_custom_entry(entry, i, state, seen, candidates)
        _run_safety_probe(state, candidates)
    except Exception as exc:  # pragma: no cover - defense in depth
        _fail(state, f"unexpected error ({exc.__class__.__name__})")
    return state


def _custom_state() -> _CustomPatternState:
    global _CUSTOM_STATE
    if _CUSTOM_STATE is None:
        state = _load_custom_patterns()
        _CUSTOM_STATE = state
        _persist_custom_status(state)
        for err in state.errors:
            _warn(f"custom redaction patterns ({state.source}): {err}")
    return _CUSTOM_STATE


def _raise_if_inactive(state: _CustomPatternState) -> None:
    if state.failed:
        raise RedactionConfigError(
            f"custom credential redaction is inactive: {state.failure} "
            f"(file: {state.source})"
        )


def get_custom_patterns() -> List[Tuple[str, "re.Pattern[str]"]]:
    """User-defined (label, compiled_regex) pairs, loaded once per process.

    Raises RedactionConfigError when the configured file failed to load —
    callers that persist text must not proceed unredacted."""
    state = _custom_state()
    _raise_if_inactive(state)
    return list(state.patterns)


def custom_patterns_status() -> dict:
    """Summary for diagnostics: count, labels, source path, problems. Never
    raises — the security report reads this even when loading failed."""
    state = _custom_state()
    return {
        "active": not state.failed,
        "failure": state.failure,
        "count": len(state.patterns),
        "labels": [label for label, _ in state.patterns],
        "source": state.source,
        "sha256": state.file_hash,
        "errors": list(state.errors),
        "rejected": state.rejected,
        "duplicates_skipped": state.duplicates,
    }


_WARNED_HASHES: set = set()


def pop_redaction_warning() -> Optional[str]:
    """A one-time, user-facing warning that custom redaction is inactive.

    Returns the message once per pattern-file content hash (persisted under
    the runtime home, so it survives across the short-lived hook processes),
    and None on repeat calls or when nothing is wrong. Hooks fold the returned
    text into their own ``{"systemMessage": ...}`` stdout envelope — a bare
    extra JSON line could break hosts that parse hook stdout as a single JSON
    document."""
    try:
        state = _custom_state()
    except Exception:
        return None
    if not state.failed:
        return None
    key = state.file_hash or hashlib.sha256(
        f"{state.source}|{state.failure}".encode("utf-8", errors="replace")
    ).hexdigest()
    if key in _WARNED_HASHES:
        return None
    try:
        d = _runtime_token_dir()
        flag = d / f"redact-warning-{key[:16]}.flag" if d is not None else None
        if flag is not None and flag.exists():
            _WARNED_HASHES.add(key)
            return None
        if d is not None:
            d.mkdir(parents=True, exist_ok=True)
            flag.write_text("warned\n", encoding="utf-8")
    except Exception:
        pass
    _WARNED_HASHES.add(key)
    return ("[Token Optimizer] Custom credential redaction is INACTIVE: "
            f"{state.failure} (file: {state.source}). Tool outputs and file "
            "contents are not being written to local caches until the pattern "
            "file is fixed or removed.")


def reset_custom_patterns_cache() -> None:
    """Forget the loaded custom patterns (tests, long-lived processes)."""
    global _CUSTOM_STATE
    _CUSTOM_STATE = None


def scan_for_credentials(text: str) -> List[Tuple[str, str, int]]:
    """Scan text for credentials. Returns [(label, matched_text, line_number), ...].

    Matching is per line and mirrors redact_credentials ordering: custom
    patterns claim each line first, then the built-ins. Text already inside a
    ``[CREDENTIAL REDACTED: ...]`` placeholder is skipped for both, so a label
    or marker inside a placeholder never produces a phantom hit.
    """
    state = _custom_state()
    _raise_if_inactive(state)
    ordered = list(state.patterns) + CREDENTIAL_PATTERNS
    results = []
    for line_num, line in enumerate(text.splitlines()):
        # Split the line on placeholders and scan only the gaps.
        last = 0
        segments = []
        for m in _PLACEHOLDER_RE.finditer(line):
            if m.start() > last:
                segments.append(line[last:m.start()])
            # A forged "[CREDENTIAL REDACTED: <real secret>]" can hide a
            # credential in the label slot — scan the interior too. A hit whose
            # label equals the whole interior is the placeholder's own label
            # describing itself, never a phantom hit.
            interior = m.group(0)[len("[CREDENTIAL REDACTED: "):-1]
            for label, pat in ordered:
                m2 = pat.search(interior)
                if m2 and not (label == interior and m2.group() == interior):
                    results.append((label, m2.group(), line_num))
            last = m.end()
        if last < len(line):
            segments.append(line[last:])
        for segment in segments:
            for label, pat in ordered:
                m = pat.search(segment)
                if m:
                    results.append((label, m.group(), line_num))
    return results


# M-16: regex to find already-redacted placeholders so they can be protected
# from re-matching during a second redaction pass.
# The interior is a label (60 characters at most, see _CUSTOM_MAX_LABEL_CHARS), so the
# bound is generous. Unbounded, an unclosed "[CREDENTIAL REDACTED: " rescanned to the end
# of the text from every start (quadratic). An over-long interior is not a placeholder:
# it is scanned like any other text, so it cannot smuggle a secret.
_PLACEHOLDER_RE = re.compile(r"\[CREDENTIAL REDACTED: [^\]]{1,256}\]")
_PLACEHOLDER_SENTINEL = "\x00\x01REDACTED\x00\x01"

# Per-pattern literal anchors (checked on a lowercased copy of the ORIGINAL
# text, once, before the loop). A pattern whose anchors are all absent cannot
# match, so its re.sub() is skipped. This is what makes clean-but-URL-bearing
# output cheap: the global prefix scan above fires on any "https://", after
# which the three M-12 patterns and the two URI patterns used to cost more
# than everything else combined (measured 24ms -> 55ms per 10K realistic
# lines on the first H-8 attempt). Substitutions only remove secrets and add
# placeholders, never new anchors, so a pre-loop check stays sound.
_PATTERN_ANCHORS = {
    # Only the patterns that are expensive to run (case-insensitive
    # alternations, or a scheme scan) are gated. The literal-prefix patterns
    # (AKIA..., ghp_..., xoxb-...) are already a fast scan in the regex engine
    # and cost less than an extra anchor check would.
    "Bearer token": ("bearer",),
    "Database URI": ("://",),
    "HTTP basic auth URL": ("://",),
    "URL auth param": ("=",),
    "MySQL password flag": ("mysql",),
    "Database env password": ("password=", "pwd=", "passwd=",
                              "password='", "password=\"", "pwd='", "pwd=\"",
                              "passwd='", "passwd=\""),
    "AWS secret key": ("aws_secret", "secret_access_key", "secretaccesskey"),
    "CLI password flag (long)": ("--password", "--passwd", "--passcode", "--auth-token"),
    "CLI password flag (short)": ("sshpass", "mariadb", "redis-cli"),
}


# ---------------------------------------------------------------------------
# Generic secret assignment: NAME=value, NAME: value, "name": "value".
#
# The closed env-name list above only knows seven names. Anything else
# (API_KEY=..., SECRET_KEY=..., a YAML "password: ...", a JSON "api_key": "...",
# a quoted value with spaces) used to reach disk as typed. One rule covers the
# class: a NAME that contains KEY, TOKEN, SECRET, PASSWORD, PASSWD, PWD or
# CREDENTIAL, then "=" or ":", then a value. The NAME stays, only the VALUE goes.
#
# Prose and ordinary code must survive, so the rule is deliberately narrow:
#   * the keyword must stand as a word part (api_key, apiKey, API_KEY, secretkey),
#     not sit inside another word (monkey, keyboard, tokenizer, keyword);
#   * names that end in a quantity or locator (token_count, KEY_FILE, SECRET_NAME)
#     are not secrets;
#   * a plain number (KEY/TOKEN names only) or boolean value is not a secret
#     (max_tokens=4096, tokens: 1200, key: true);
#   * an unquoted value after a SPACED "=" is code (token_count = len(x)), not an
#     env/config assignment; a quoted value is redacted either way;
#   * references are not secrets: $VAR, ${VAR}, <placeholder>, f(x) calls,
#     self.x / os.environ / process.env / args.x, and kwarg pass-through
#     (api_key=api_key), type names (apiKey: string).
# Scanned on one line at a time, so no match can cross a newline.
# ---------------------------------------------------------------------------
_ASSIGN_KEYWORD_RE = re.compile(r"key|token|secret|password|passwd|pwd|credential", re.I)
_ASSIGN_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
_ASSIGN_NAME_TAIL_RE = re.compile(r"[A-Za-z0-9_.\-]*")
# What follows a name: optional closing quote, "=" or ":" (not "=="), then the value.
_ASSIGN_TAIL_RE = re.compile(
    r"(?P<q>[\"']?)(?P<sep>[ \t]*[=:](?!=)[ \t]*)"
    r"(?P<val>\"[^\"\n]*\"|'[^'\n]*'|[^\s\"']\S*)"
)
_ASSIGN_QUALIFIERS = (
    "api", "access", "auth", "secret", "private", "public", "client", "session",
    "refresh", "bearer", "app", "db", "user", "admin", "root", "master", "signing",
    "encryption", "license", "ssh", "gpg", "jwt", "oauth", "service", "webhook",
)
_ASSIGN_LOCATOR_SUFFIX_RE = re.compile(
    r"[_.\-]*(?:path|file|dir|url|uri|name|id|count|limit|max|min|size|length|len|"
    r"ttl|expiry|expires|type|field|header|endpoint|regex|budget|label)s?", re.I)
_ASSIGN_LITERAL_RE = re.compile(
    r"(?:true|false|yes|no|on|off|null|none|nil|undefined|nan|"
    r"str|string|int|integer|float|bool|boolean|number|any|object|bytes|list|dict|"
    r"secretstr|path)", re.I)
_ASSIGN_NUMBER_RE = re.compile(r"\d{1,9}(?:\.\d+)?")
_ASSIGN_CALL_RE = re.compile(r"[A-Za-z_][\w.]*\(")
_ASSIGN_GENERIC_TYPE_RE = re.compile(r"[A-Z][A-Za-z]*\[")
_ASSIGN_REFERENCE_RE = re.compile(
    r"(?:\$|<|\{|%|self\.|this\.|os\.environ|process\.env|args\.|opts\.|options\.|config\.|cfg\.|"
    r"settings\.|env\.|\[CREDENTIAL|\x00|/|~|\./|\.\./|[A-Za-z]:[\\/])")
_ASSIGN_CLOSERS_RE = re.compile(r"[,;)}\]]+$")
_ASSIGN_PLACEHOLDER = "[CREDENTIAL REDACTED: Secret assignment]"


def _assign_name_is_secret(name: str) -> Optional[str]:
    """Return "strong" (password/secret class), "weak" (key/token class) or None."""
    found: Optional[str] = None
    for m in _ASSIGN_KEYWORD_RE.finditer(name):
        kw = m.group(0)
        before, after = name[:m.start()], name[m.end():]
        low = kw.lower()
        if after and after[0].isalpha():
            plural = after[0] in "sS" and (len(after) == 1 or not after[1].isalpha())
            camel = after[0].isupper() and not name.isupper()
            if not (plural or camel):
                continue
        if _ASSIGN_LOCATOR_SUFFIX_RE.fullmatch(after):
            continue
        strong = low in ("password", "passwd", "secret", "credential")
        if not strong and before and before[-1].isalpha():
            ok_before = kw[0].isupper() or before.lower().endswith(_ASSIGN_QUALIFIERS)
            if not ok_before:
                continue
        if strong:
            return "strong"
        found = "weak"
    return found


def _assign_value(name: str, q: str, sep: str, val: str) -> Optional[str]:
    """The text that replaces `val`, or None to leave the assignment alone."""
    kind = _assign_name_is_secret(name)
    if kind is None:
        return None
    quote = val[0] if val[0] in "\"'" and len(val) >= 2 and val[-1] == val[0] else ""
    tail = ""
    if quote:
        body = val[1:-1]
    else:
        body = val
        t = _ASSIGN_CLOSERS_RE.search(body)
        if t:
            tail, body = body[t.start():], body[:t.start()]
    if not body.strip():
        return None
    # Spaced "=" with a bare value is code (token_count = len(x)), not an assignment.
    if not quote and sep != "=" and sep.strip() == "=":
        return None
    if _ASSIGN_LITERAL_RE.fullmatch(body):
        return None
    if kind == "weak" and _ASSIGN_NUMBER_RE.fullmatch(body):
        return None
    if _ASSIGN_REFERENCE_RE.match(body) or _ASSIGN_CALL_RE.match(body) \
            or _ASSIGN_GENERIC_TYPE_RE.match(body):
        return None
    if body.lower() == name.lower() or body.lower() == name.lower().rsplit(".", 1)[-1]:
        return None
    return f"{quote}{_ASSIGN_PLACEHOLDER}{quote}{tail}"


def _redact_assignments(text: str) -> str:
    """Walk the keyword hits, not every word: cost follows the hits, and a name is
    visited once (the scan resumes after it), so keyword-dense input stays linear."""
    out: List[str] = []
    copied = 0
    pos = 0
    n = len(text)
    while pos < n:
        hit = _ASSIGN_KEYWORD_RE.search(text, pos)
        if hit is None:
            break
        name_end = _ASSIGN_NAME_TAIL_RE.match(text, hit.end()).end()
        pos = name_end
        start = hit.start()
        while start > 0 and text[start - 1] in _ASSIGN_NAME_CHARS:
            start -= 1
        name = text[start:name_end]
        if not (name[0].isalpha() or name[0] == "_"):
            continue
        tail = _ASSIGN_TAIL_RE.match(text, name_end)
        if tail is None:
            continue
        replacement = _assign_value(name, tail.group("q"), tail.group("sep"), tail.group("val"))
        if replacement is None:
            continue
        v0, v1 = tail.span("val")
        out.append(text[copied:v0])
        out.append(replacement)
        copied = v1
        pos = v1
    if not out:
        return text
    out.append(text[copied:])
    return "".join(out)


# ---------------------------------------------------------------------------
# Command-syntax secrets: credentials that sit in a shell command's own grammar
# rather than in a token shape (curl -u user:pass, --token VALUE, user:pass@host
# with no scheme, echo SECRET | docker login, Windows net use / cmdkey).
#
# These rules are only safe on COMMAND text. In free output they would rewrite
# prose ("mailto:me@x.com", "echo hi | wc"), so redact_credentials runs them
# only when the caller says command=True. The same rules serve
# deterministic_candidates (via scrub_command_syntax), so there is one copy.
# ---------------------------------------------------------------------------
_ARG = r"(?:\"[^\"]*\"|'[^']*'|[^\s\"']+)"
# curl -u user:pass, --user user:pass, --proxy-user ... (value must contain a colon,
# and not be a bare uid:gid like docker's `-u 1000:1000`)
_USER_FLAG_RE = re.compile(r"(?<![\w-])(-u|-U|--user|--proxy-user)(\s+|=)?(" + _ARG + ")")
_SECRET_FLAG_RE = re.compile(
    r"(?<![\w-])(--(?:password|passwd|pass|pwd|passphrase|secret|client-secret|token|auth-token|"
    r"access-token|api-key|apikey|api_key))(\s+|=)(?!-)(" + _ARG + ")",
    re.I,
)
# scheme://user:pass@host (any scheme: ftp, ssh, amqp, https...). Greedy to the last
# `@` before the path, so a password containing `@` is covered whole.
_URL_USERINFO_RE = re.compile(r"(://)[^\s/\"':@]+:[^\s/\"']*@")
# user:pass@host with no scheme (rsync, scp, git remotes). Docker digest refs
# (image:tag@sha256:...) and Windows paths are not credentials.
_BARE_USERINFO_RE = re.compile(r"(?<![\w/@.:+\\-])[\w.+-]+:[^\s/\\@\"':]+@(?!sha\d+:)(?=[A-Za-z0-9])")
# echo/printf <anything> | cmd : the piped argument is, in practice, the secret that
# the next command reads from stdin. Flags (-n, -e) survive; quoted args may hold `|`.
_PIPED_ECHO_RE = re.compile(
    r"(?<![\w/.-])(echo|printf)(\s+(?:-[A-Za-z]+\s+)*)"
    r"((?:\"[^\"]*\"|'[^']*'|[^|;&\n\"'])+?)(\s*)\|(?!\|)"
)
# Windows: `net use [dev:] \\srv\share [password] [/user:name]` and `... /user:name password`,
# `cmdkey /pass:...`. `*` (prompt for the password) is not a secret.
_NET_USE_PW_BEFORE_RE = re.compile(r"(\bnet\s+use\s+(?:[A-Za-z]:\s+|\*\s+)?\\\\\S+\s+)(?![/*-])(\S+)(?=\s|$)", re.I)
_NET_USE_PW_AFTER_RE = re.compile(r"(\bnet\s+use\b[^|;&\n]*?/user:\S+\s+)(?![/*-])(\S+)", re.I)
_WIN_USER_RE = re.compile(r"(/user:)\S+", re.I)
_WIN_PASS_RE = re.compile(r"(/(?:pass|password|passwd|pwd):)\S+", re.I)


class CommandMarks:
    """What each command-syntax rule writes in place of the secret."""
    __slots__ = ("creds", "arg", "secret", "user")

    def __init__(self, creds: str, arg: str, secret: str, user: str):
        self.creds, self.arg, self.secret, self.user = creds, arg, secret, user


# Redaction output: the standard labeled placeholder.
_REDACT_MARKS = CommandMarks(
    creds="[CREDENTIAL REDACTED: user:password]",
    arg="[CREDENTIAL REDACTED: piped value]",
    secret="[CREDENTIAL REDACTED: command secret]",
    user="[CREDENTIAL REDACTED: Windows user]",
)


def scrub_command_syntax(text: str, marks: "CommandMarks" = _REDACT_MARKS) -> str:
    """Blank credentials that live in a command's syntax (see block comment above)."""
    def _user_flag_sub(m: "re.Match[str]") -> str:
        value = m.group(3).strip("\"'")
        if ":" not in value or re.fullmatch(r"\d+:\d+", value):
            return m.group(0)
        return f"{m.group(1)}{m.group(2) or ''}{marks.creds}"

    text = _URL_USERINFO_RE.sub(lambda m: f"{m.group(1)}{marks.creds}@", text)
    text = _PIPED_ECHO_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{marks.arg}{m.group(4)}|", text)
    text = _USER_FLAG_RE.sub(_user_flag_sub, text)
    text = _SECRET_FLAG_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{marks.secret}", text)
    text = _NET_USE_PW_BEFORE_RE.sub(lambda m: f"{m.group(1)}{marks.secret}", text)
    text = _NET_USE_PW_AFTER_RE.sub(lambda m: f"{m.group(1)}{marks.secret}", text)
    text = _WIN_PASS_RE.sub(lambda m: f"{m.group(1)}{marks.secret}", text)
    text = _WIN_USER_RE.sub(lambda m: f"{m.group(1)}{marks.user}", text)
    text = _BARE_USERINFO_RE.sub(lambda m: f"{marks.creds}@", text)
    return text


def _sub_with_placeholder(pat: "re.Pattern[str]", label: str, text: str) -> str:
    # A function replacement, not a template string: a custom label is user
    # text and must never be interpreted as a backreference ("\\1", "\\g<0>").
    placeholder = f"[CREDENTIAL REDACTED: {label}]"
    if "keep" in pat.groupindex:
        def _repl(m):
            if m.group("keep") is None:
                if m.start() == m.end():
                    return m.group(0)
                return placeholder
            ks, ke = m.span("keep")
            # The kept group covers the whole match: nothing to redact.
            if ks == m.start() and ke == m.end():
                return m.group(0)
            # Keep the group where it sits; replace what comes before and/or
            # after it. Built-in patterns always put `keep` first, so for them
            # this is exactly "<keep>[CREDENTIAL REDACTED: label]".
            return ((placeholder if ks > m.start() else "")
                    + m.group("keep")
                    + (placeholder if ke < m.end() else ""))
    else:
        def _repl(m):
            # Zero-width matches (\b, lookaheads) would otherwise sprinkle
            # placeholders between characters without removing anything.
            if m.start() == m.end():
                return m.group(0)
            return placeholder
    return pat.sub(_repl, text)


def redact_credentials(text: str, *, command: bool = False) -> str:
    """Replace credential matches with [CREDENTIAL REDACTED: <type>] placeholders.

    A pattern may define a named `keep` group for a non-secret prefix that should
    survive redaction (e.g. the "?token=" part of a URL auth parameter); only the
    value after it is replaced. Patterns without a `keep` group redact the whole
    match, unchanged.

    H-8: uses a fast prefix scan to skip all re.sub() calls when the text
    contains no credential prefixes (the common case for clean command output).
    This makes clean text O(n) with a tiny constant instead of O(n × 28) regex
    passes. Text with credentials still gets the full sequential redaction,
    preserving correctness and the existing two-phase ordering (standalone
    credentials before URL auth params, so the negative lookahead works).

    Custom patterns run BEFORE the built-ins, on the segments between
    pre-existing placeholders. A custom shape wins over a built-in on overlap —
    that precedence is deliberate, so an org pattern can claim a whole
    composite fragment (e.g. "MEDX-123456-<jwt>") instead of leaving a readable
    prefix beside a built-in placeholder.

    M-16: protects already-redacted [CREDENTIAL REDACTED: ...] placeholders
    from re-matching by replacing them with a sentinel before redaction and
    restoring them after. This fixes the Bearer pattern re-matching "Bearer
    token" inside its own placeholder, which nested placeholders on re-runs.

    command=True marks the text as a shell command (not output or prose) and also
    runs the command-syntax rules: curl -u user:pass, --token VALUE, scheme-less
    user:pass@host, echo SECRET | cmd, Windows net use. Every writer that persists
    command text passes it.

    Raises RedactionConfigError when a configured custom pattern file failed
    to load — callers persisting the result must treat that as "do not write".
    """
    state = _custom_state()
    # A configured-but-broken pattern file must not silently degrade to
    # built-ins only: the caller is about to persist this text, and the user's
    # own secret shapes would leak. Fail closed — the writer skips the write.
    _raise_if_inactive(state)

    # Custom patterns run BEFORE the built-ins, but only on the text between
    # pre-existing placeholders (_redact_custom splits on _PLACEHOLDER_RE, so
    # an existing "[CREDENTIAL REDACTED: ...]" can never be re-matched or
    # corrupted). Running first is what lets an org pattern claim a fragment
    # like "MEDX-123456-<jwt>" whole instead of leaving the id prefix behind.
    if state.patterns:
        text = _redact_custom(text, state.patterns)

    # M-16: protect placeholders from re-matching — both the ones that were in
    # the input and the ones the custom patterns just inserted. The interior is
    # a LABEL slot, not a payload slot: a forged "[CREDENTIAL REDACTED: <real
    # secret>]" used to ride the sentinel protection verbatim into every
    # persistence boundary. Scan each interior and replace a credential match
    # with its label, so a legit placeholder survives unchanged (the only
    # self-matching label is "Bearer token", which substitutes to itself) while
    # a smuggled secret collapses to e.g. "GitHub PAT classic".
    placeholders = []
    ordered = list(state.patterns) + CREDENTIAL_PATTERNS

    def _clean_interior(ph):
        interior = ph[len("[CREDENTIAL REDACTED: "):-1]
        if _text_may_contain_credentials(interior):
            for label, pat in ordered:
                interior = pat.sub(
                    lambda m, lbl=label: (
                        m.group(0) if m.start() == m.end() else lbl
                    ),
                    interior,
                )
        return "[CREDENTIAL REDACTED: " + interior + "]"

    def _save_placeholder(m):
        placeholders.append(_clean_interior(m.group(0)))
        return _PLACEHOLDER_SENTINEL
    if "[CREDENTIAL REDACTED:" in text:
        text = _PLACEHOLDER_RE.sub(_save_placeholder, text)

    # H-8: fast path — skip all regex work if no credential prefix is present.
    lowered = text.lower()
    for label, pat in CREDENTIAL_PATTERNS:
        anchors = _PATTERN_ANCHORS.get(label)
        if anchors and not any(a in lowered for a in anchors):
            continue
        text = _sub_with_placeholder(pat, label, text)

    # Command-syntax shapes (command text only), then generic NAME=value /
    # NAME: value secrets. Both run after the specific shapes so a labelled
    # placeholder from a rule above is never overwritten.
    if command:
        text = scrub_command_syntax(text)
    text = _redact_assignments(text)

    # M-16: restore protected placeholders.
    for ph in placeholders:
        text = text.replace(_PLACEHOLDER_SENTINEL, ph, 1)
    return text


def _redact_custom(text: str, custom: List[Tuple[str, "re.Pattern[str]"]]) -> str:
    for label, pat in custom:
        parts = []
        last = 0
        for m in _PLACEHOLDER_RE.finditer(text):
            parts.append(_sub_with_placeholder(pat, label, text[last:m.start()]))
            parts.append(m.group(0))
            last = m.end()
        parts.append(_sub_with_placeholder(pat, label, text[last:]))
        text = "".join(parts)
    return text


def redact_credentials_deep(value):
    """Apply redact_credentials to every string in a nested structure.

    One pass over a whole checkpoint/handoff state so a NEW string field can
    never bypass the credential pass — the failure mode the per-field
    redactors had (open_questions, todos, agent descriptions and paths were
    persisted verbatim because nobody listed them).

    Rebuilds dicts, lists, tuples and sets preserving container type; keys
    are redacted too (a key derived from transcript text is attacker
    content). Non-strings pass through unchanged. Returns a new structure;
    the input is not mutated.

    Raises RedactionConfigError exactly like redact_credentials — callers
    persisting the result must treat that as "do not write".
    """
    if isinstance(value, str):
        return redact_credentials(value)
    if isinstance(value, dict):
        return {
            redact_credentials_deep(k): redact_credentials_deep(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(redact_credentials_deep(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return type(value)(redact_credentials_deep(v) for v in value)
    return value
