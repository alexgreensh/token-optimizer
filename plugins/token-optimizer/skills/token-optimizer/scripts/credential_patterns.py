"""Shared credential detection and redaction for Token Optimizer.

Provides compiled regex patterns for common API keys, tokens, and secrets,
plus scan/redact functions usable by bash compression, read cache, and
tool archive writers.
"""

from __future__ import annotations

import re
from typing import List, Tuple

# (label, compiled_regex) pairs. Label is used in redaction placeholders.
CREDENTIAL_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("AWS access key",          re.compile(r"AKIA[0-9A-Z]{16}")),
    ("OpenAI/Anthropic key",    re.compile(r"sk-[a-zA-Z0-9]{20,}")),
    ("Anthropic key",           re.compile(r"sk-ant-[a-zA-Z0-9\-]{20,}")),
    ("GitHub PAT classic",      re.compile(r"ghp_[a-zA-Z0-9]{36}")),
    ("GitHub OAuth token",      re.compile(r"gho_[a-zA-Z0-9]{36}")),
    ("GitHub server token",     re.compile(r"ghs_[a-zA-Z0-9]{36}")),
    ("GitHub refresh token",    re.compile(r"ghr_[a-zA-Z0-9]{36}")),
    ("GitHub fine-grained PAT", re.compile(r"github_pat_[a-zA-Z0-9_]{80,}")),
    ("npm token",               re.compile(r"npm_[a-zA-Z0-9]{36}")),
    ("Slack bot token",         re.compile(r"xoxb-[0-9]+-[a-zA-Z0-9]+")),
    ("Slack user token",        re.compile(r"xoxp-[0-9]+-[a-zA-Z0-9]+")),
    ("Slack app token",         re.compile(r"xoxa-[0-9]+-[a-zA-Z0-9]+")),
    ("Stripe live key",         re.compile(r"sk_live_[a-zA-Z0-9]{24,}")),
    ("Stripe restricted key",   re.compile(r"rk_live_[a-zA-Z0-9]{24,}")),
    ("HuggingFace token",       re.compile(r"hf_[a-zA-Z0-9]{34}")),
    ("Bearer token",            re.compile(r"Bearer\s+[a-zA-Z0-9\-._~+/]+=*", re.I)),
    ("Google API key",          re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("Google OAuth token",      re.compile(r"ya29\.[0-9A-Za-z_\-]{20,}")),
    ("JWT",                     re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("PEM private key",         re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
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
]

# Bare compiled patterns list for backward compat with bash_compress.py
PATTERNS_ONLY: List["re.Pattern[str]"] = [pat for _, pat in CREDENTIAL_PATTERNS]


def scan_for_credentials(text: str) -> List[Tuple[str, str, int]]:
    """Scan text for credentials. Returns [(label, matched_text, line_number), ...]."""
    results = []
    for line_num, line in enumerate(text.splitlines()):
        for label, pat in CREDENTIAL_PATTERNS:
            m = pat.search(line)
            if m:
                results.append((label, m.group(), line_num))
    return results


def redact_credentials(text: str) -> str:
    """Replace credential matches with [CREDENTIAL REDACTED: <type>] placeholders.

    A pattern may define a named `keep` group for a non-secret prefix that should
    survive redaction (e.g. the "?token=" part of a URL auth parameter); only the
    value after it is replaced. Patterns without a `keep` group redact the whole
    match, unchanged.
    """
    for label, pat in CREDENTIAL_PATTERNS:
        if "keep" in pat.groupindex:
            text = pat.sub(rf"\g<keep>[CREDENTIAL REDACTED: {label}]", text)
        else:
            text = pat.sub(f"[CREDENTIAL REDACTED: {label}]", text)
    return text
