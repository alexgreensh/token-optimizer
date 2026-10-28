#!/usr/bin/env python3
"""Hermes tool-hook dispatcher for Token Optimizer.

The Hermes plugin (``hermes/__init__.py``) runs inside the Hermes process. To
keep that process clean, every per-tool engine call happens here, in a short
child process, fed one Claude-Code-shaped payload on stdin. The engines are the
SAME ones the Claude Code PostToolUse / PreToolUse hooks run; this file only
routes a payload to them and normalizes what they print. No engine logic lives
here.

    python3 hermes_tool_hooks.py post-tool     < payload.json
    python3 hermes_tool_hooks.py pre-tool      < payload.json
    python3 hermes_tool_hooks.py clear-reads   < payload.json
    python3 hermes_tool_hooks.py redaction-patterns

Output is ONE JSON object on stdout (or nothing):

    post-tool:   {"replacement": str | null, "notes": [str, ...]}
    pre-tool:    {"block": str | null, "notes": [str, ...]}
    clear-reads: {}
    redaction-patterns: {"patterns": [str, ...]}

Engines run here (post-tool), by Claude Code tool name:
  - bash_compress_hook  (Bash): output compression, cross-turn dedup, loop
    detection (thrash guard), archive + expand pointer, savings logging.
  - archive_result      (Bash|Read|Glob|Grep|Agent|mcp__*): archive large
    results; MCP results are replaced by a preview + expand pointer.
  - context_intel       (most tools): activity mode + decision extraction.
  - read_cache --invalidate (Edit|Write): keep delta reads correct after edits.

The Claude Code runner's quality-cache tick is deliberately NOT run: with no
transcript for Hermes it would fall back to the most recently modified Claude
Code transcript and score the wrong session.

Fail-open everywhere: any error prints nothing and exits 0, so the caller keeps
the original tool output.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

# Same cap the Claude Code PostToolUse runner uses (archive_result's limit).
_STDIN_MAX_BYTES = 5_242_880 + 262_144

# Matchers copied from hooks/posttooluse_runner.py (searched, not anchored).
_MATCH_BASH_COMPRESS = re.compile("Bash")
_MATCH_ARCHIVE = re.compile("Bash|PowerShell|Read|Glob|Grep|Agent|mcp__.*")
_MATCH_CONTEXT_INTEL = re.compile(
    "Bash|PowerShell|Read|Grep|Glob|Edit|Write|MultiEdit|NotebookEdit|mcp__.*"
)
_MATCH_READ_INVALIDATE = re.compile("Edit|Write|MultiEdit|NotebookEdit")


def _read_payload() -> dict:
    try:
        from hook_io import read_stdin_hook_input

        data = read_stdin_hook_input(_STDIN_MAX_BYTES)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _share_payload(payload: dict) -> None:
    """Every engine reads stdin through hook_io; hand them all this payload."""

    def _shared(max_bytes=None, *_a, **_kw):
        return dict(payload)

    try:
        import hook_io

        hook_io.read_stdin_hook_input = _shared
    except Exception:
        pass


def _rebind(module) -> None:
    """Re-patch a module that bound read_stdin_hook_input at import time."""
    try:
        if hasattr(module, "read_stdin_hook_input"):
            import hook_io

            module.read_stdin_hook_input = hook_io.read_stdin_hook_input
    except Exception:
        pass


def _capture(fn, *args, **kwargs) -> str:
    """Run one engine, return what it printed. Never raises."""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            fn(*args, **kwargs)
    except BaseException:  # SystemExit included: an engine must not end the dispatcher
        pass
    return buf.getvalue()


def _json_objects(text: str) -> list[dict]:
    """Parse every JSON object line an engine printed."""
    out: list[dict] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _collect(objs: list[dict], result: dict) -> None:
    """Fold Claude Code hook envelopes into the normalized result."""
    for obj in objs:
        hso = obj.get("hookSpecificOutput")
        if isinstance(hso, dict):
            updated = hso.get("updatedToolOutput")
            if isinstance(updated, dict) and isinstance(updated.get("stdout"), str):
                text = updated["stdout"]
                stderr = updated.get("stderr")
                if isinstance(stderr, str) and stderr:
                    text = f"{text}\n{stderr}" if text else stderr
                result["replacement"] = text
            mcp = hso.get("updatedMCPToolOutput")
            if isinstance(mcp, str):
                result["replacement"] = mcp
            ctx = hso.get("additionalContext")
            if isinstance(ctx, str) and ctx.strip():
                result["notes"].append(ctx.strip())
            if hso.get("permissionDecision") == "deny":
                reason = hso.get("permissionDecisionReason")
                if isinstance(reason, str) and reason.strip():
                    result["block"] = reason.strip()
        msg = obj.get("systemMessage")
        if isinstance(msg, str) and msg.strip():
            result["notes"].append(msg.strip())


def _bash_compress_enabled() -> bool:
    """The same switch every other bridge honours (env var or dashboard)."""
    try:
        from plugin_env import is_v5_flag_enabled

        return bool(is_v5_flag_enabled("v5_bash_compress", "TOKEN_OPTIMIZER_BASH_COMPRESS", default=True))
    except Exception:
        return True


def run_post_tool(payload: dict) -> dict:
    result: dict = {"replacement": None, "notes": []}
    tool_name = str(payload.get("tool_name") or "")
    if not tool_name or not payload.get("session_id"):
        return result
    _share_payload(payload)

    if _MATCH_BASH_COMPRESS.search(tool_name):
        import bash_compress_hook

        if not _bash_compress_enabled():
            # The user turned compression off (env or dashboard toggle). Keep
            # loop detection: the hook's nudge-only mode skips the compression
            # compute and still reports reruns.
            os.environ["TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT"] = "1"
        _rebind(bash_compress_hook)
        _collect(_json_objects(_capture(bash_compress_hook.main)), result)
    if _MATCH_ARCHIVE.search(tool_name) and result["replacement"] is None:
        # Bash output already compressed and archived by bash_compress_hook
        # never needs a second archive copy; everything else goes through.
        import archive_result

        _rebind(archive_result)
        _collect(_json_objects(_capture(archive_result.archive_result, quiet=True)), result)
    if _MATCH_CONTEXT_INTEL.search(tool_name):
        import context_intel

        _rebind(context_intel)
        _capture(context_intel.handle_post_tool_use)
    if _MATCH_READ_INVALIDATE.search(tool_name):
        import read_cache

        _capture(read_cache.handle_invalidate, dict(payload), True)
    return result


def run_pre_tool(payload: dict) -> dict:
    result: dict = {"block": None, "notes": []}
    tool_name = str(payload.get("tool_name") or "")
    if not tool_name:
        return result
    _share_payload(payload)

    if tool_name == "Read":
        import read_cache

        _rebind(read_cache)
        saved_argv = sys.argv
        try:
            sys.argv = ["read_cache.py", "--quiet"]
            _collect(_json_objects(_capture(read_cache.main)), result)
        finally:
            sys.argv = saved_argv
    elif tool_name.startswith("mcp__"):
        import refetch_guard

        _rebind(refetch_guard)
        _collect(_json_objects(_capture(refetch_guard.refetch_guard)), result)
    return result


def run_clear_reads(payload: dict) -> dict:
    if not payload.get("session_id"):
        return {}
    import read_cache

    _capture(read_cache.handle_clear_compacted, dict(payload), True)
    return {}


def run_redaction_patterns(_payload: dict) -> dict:
    """Credential regexes (built-in + the user's custom file for this runtime).

    The plugin hands these to Hermes's own redactor. Loaded here, under the
    Hermes runtime, so the custom pattern file is the one the engines use.
    A broken custom file contributes nothing; the built-ins still go.
    """
    import credential_patterns

    compiled = list(getattr(credential_patterns, "PATTERNS_ONLY", []))
    try:
        compiled += [pat for _, pat in credential_patterns.get_custom_patterns()]
    except Exception:
        pass
    seen: list[str] = []
    for pat in compiled:
        text = getattr(pat, "pattern", None)
        if isinstance(text, str) and text not in seen:
            seen.append(text)
    return {"patterns": seen}


_HANDLERS = {
    "post-tool": run_post_tool,
    "pre-tool": run_pre_tool,
    "clear-reads": run_clear_reads,
    "redaction-patterns": run_redaction_patterns,
}
# Events that need no stdin payload.
_NO_PAYLOAD = frozenset({"redaction-patterns"})


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in _HANDLERS:
        return 0
    os.environ.setdefault("TOKEN_OPTIMIZER_RUNTIME", "hermes")
    try:
        from utf8_io import enforce_utf8_io

        enforce_utf8_io()
    except Exception:
        pass
    payload = {} if args[0] in _NO_PAYLOAD else _read_payload()
    if not payload and args[0] not in _NO_PAYLOAD:
        return 0
    try:
        out = _HANDLERS[args[0]](payload)
    except Exception:
        return 0
    if out:
        sys.stdout.write(json.dumps(out))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
