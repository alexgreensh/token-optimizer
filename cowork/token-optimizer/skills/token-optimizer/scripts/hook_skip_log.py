"""Opportunity-tier record of a Bash compression the PostToolUse budget cut off.

When the runner's per-handler deadline interrupts ``bash_compress_hook`` (or the
shared budget is gone before it is dispatched), the raw output reaches the model
uncompressed. Nothing was saved, so nothing may be counted, but the cost of the
skip should be visible: this writes one ``compression_events`` row with
``tier='opportunity'`` and the size of the output that went through unshrunk.

Never a saving, by construction and not only by tier:
  * ``tier = 'opportunity'``: realized-savings readers take measured/estimated only.
  * ``compressed_tokens == original_tokens``: any reader that sums
    ``original_tokens - compressed_tokens`` (the counted views, the reread pass)
    gets exactly 0 from these rows, even one that ignores the tier.
  * The would-be size lives in ``detail`` (``output_tokens``) for opportunity reports.

Hot path: imported only after an overrun, standard library plus the hook-side
``compression_log`` writer (50 ms DB timeout, fail-open). Never raises.
"""
from __future__ import annotations

import json
import re

FEATURE = "bash_compress_skipped"
# bash_compress_hook never compresses stdout shorter than this, so a skip of a
# smaller output cost nothing and is not recorded.
MIN_OUTPUT_CHARS = 100
_FIRST_WORD = re.compile(r"[A-Za-z0-9_.+-]{1,40}")


def _bash_stdout(hook_input):
    """The Bash stdout the hook would have compressed, or '' when there is none."""
    response = hook_input.get("tool_response")
    if isinstance(response, str):
        return response
    if not isinstance(response, dict) or response.get("interrupted") or response.get("isImage"):
        return ""
    return str(response.get("stdout") or "")


def _command_word(command):
    """First program word of the command, for grouping only (never the full line)."""
    for part in re.split(r"&&|\|\||;|\|", str(command or "")):
        part = part.strip()
        if not part or part.startswith("cd "):
            continue
        for word in part.split():
            if "=" in word or word in ("sudo", "env", "time", "timeout"):
                continue
            m = _FIRST_WORD.fullmatch(word.rsplit("/", 1)[-1])
            return m.group(0) if m else "unknown"
    return "unknown"


def skip_row(hook_input, handler, budget_s, reason):
    """The compression_events fields for one skipped compression, or None."""
    if not isinstance(hook_input, dict) or hook_input.get("tool_name") != "Bash":
        return None
    stdout = _bash_stdout(hook_input)
    if len(stdout) < MIN_OUTPUT_CHARS:
        return None
    try:
        from token_estimate import estimate_tokens
        tokens = int(estimate_tokens(stdout))
    except Exception:
        tokens = len(stdout.encode("utf-8", errors="replace")) // 4
    detail = json.dumps({
        "reason": reason, "handler": handler, "budget_s": round(float(budget_s or 0.0), 3),
        "output_chars": len(stdout), "output_tokens": tokens, "saved_tokens": 0,
    }, sort_keys=True)
    command = (hook_input.get("tool_input") or {}).get("command", "")
    return {
        "feature": FEATURE, "session_id": str(hook_input.get("session_id") or "") or None,
        "command_pattern": _command_word(command), "tier": "opportunity",
        "original_tokens": tokens, "compressed_tokens": tokens, "detail": detail,
    }


def log_skipped_compression(hook_input, handler, budget_s, reason="hook_budget"):
    """Write the opportunity row for one skip. Returns True when a row was attempted."""
    try:
        row = skip_row(hook_input, handler, budget_s, reason)
        if row is None:
            return False
        from compression_log import log_compression_event
        log_compression_event(**row)
        return True
    except Exception:
        return False
