"""URL-borne credential patterns for Token Optimizer.

The URL family of credential_patterns.CREDENTIAL_PATTERNS, kept in its own
module to keep credential_patterns.py under the repo's 1,000-line ratchet
(scripts/check_file_size.py). Spliced back at its exact original position by
credential_patterns, so global pattern order (and therefore redaction
behavior) is unchanged. Do not reorder.
"""

from __future__ import annotations

import re
from typing import List, Tuple

# (label, compiled_regex) pairs, in the exact order they held inside
# credential_patterns.CREDENTIAL_PATTERNS.
URL_CREDENTIAL_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("Database URI",            re.compile(r"(?:postgres|postgresql|mysql|mongodb|mongodb\+srv|redis)://[^:\s/]+:[^@\s]+@", re.I)),
    ("HTTP basic auth URL",     re.compile(r"https?://[^:\s/@]+:[^@\s]+@", re.I)),
    # Token-only userinfo (https://TOKEN@host): how git embeds PATs in remote
    # URLs (https://ghp_xxx@github.com/org/repo). No colon, so the basic-auth
    # shape above misses it. The 8-char floor keeps ordinary short usernames
    # out; userinfo on an http(s) URL is almost never benign (G1).
    ("HTTP token userinfo URL", re.compile(r"https?://[^:\s/@]{8,}@", re.I)),
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
