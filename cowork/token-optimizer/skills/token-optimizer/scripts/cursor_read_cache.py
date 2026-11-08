"""Cursor read-path parity (U2): shadow-first redundant-read interception.

Reuses the Claude path's pure machinery unchanged - ``structure_map``
(eligibility + structure maps), ``delta_diff`` (fingerprints + deltas),
``read_cache``'s message builders and contextignore eligibility - and owns
only the Cursor-specific parts: payload shape (the bridge's decoded
``fields``), the per-session fingerprint store under the Cursor home, the
mode gate, and the deny + ``user_message`` emit contract.

Modes (mirror Claude): ``shadow`` (default: record what would be injected,
emit nothing), ``soft_block`` (deny + injected map/delta), ``warn`` (allow
+ note), ``block`` (deny, reason only). Arming is NEVER automatic:
``soft_block``/``block`` take effect only when the install's
``<cursor_home>/token-optimizer/read-cache.json`` names the mode. The
doctor reports the channel state (``unknown``/``shadow``/
``armed-unverified``) from the observed ledger; shadow-mode silence
neither arms nor confirms anything, and no ledger pattern verifies -
``armed-verified`` needs positive controlled native evidence no current
source provides.

Delta bases come only from fingerprints observed in THIS session: the
store is keyed by conversation id, so a file last read in another session
is a first read here. KTD9 coexistence: when Claude Token Optimizer hooks
are configured (the 4 Claude settings files, read-only) and no explicit
mode is set, the Read channel defers to record-only - the imported Claude
hook may own the channel; Cursor's import toggle itself has no verified
file surface, so ownership is reported as config-presence-based, never
assumed demonstrated.

Fail open everywhere: unknown tool shapes, oversized payloads, decode or
store failures emit nothing.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

_MAX_STORE_ENTRIES = 200
_MAX_STORE_BYTES = 512 * 1024
_MAX_FILE_BYTES = 1024 * 1024  # never intercept reads of very large files
_MODES = ("shadow", "soft_block", "warn", "block")
_DEFAULT_MODE = "shadow"

# Claude settings files scanned (read-only) for KTD9 coexistence, matching
# the audit's four-file contract; nothing else under ~/.claude is read.
_CLAUDE_SETTINGS_REL = (
    ("user", ".claude/settings.json"),
    ("user-local", ".claude/settings.local.json"),
)


def _home():
    from runtime_env import cursor_home

    return cursor_home()


def _to_dir() -> Path:
    return _home() / "token-optimizer"


def _config_path() -> Path:
    return _to_dir() / "read-cache.json"


def _load_config() -> dict:
    try:
        data = json.loads(_config_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def configured_mode() -> str:
    """The install's configured mode; invalid/missing values mean shadow."""
    return _mode_from(_load_config())


def _mode_from(config: dict) -> str:
    mode = config.get("mode")
    return mode if mode in _MODES else _DEFAULT_MODE


def _claude_to_hooks_present(workspace_roots) -> bool:
    """Config-presence check only (KTD9): a Claude TO hook command in any of
    the user settings files or the workspace's project settings files."""
    candidates = []
    home = os.path.expanduser("~")
    for _, rel in _CLAUDE_SETTINGS_REL:
        candidates.append(Path(home) / rel)
    for root in (workspace_roots or [])[:4]:
        if isinstance(root, str) and root:
            candidates.append(Path(root) / ".claude" / "settings.json")
            candidates.append(Path(root) / ".claude" / "settings.local.json")
    needles = ("read_cache", "read-cache")
    for path in candidates[:8]:
        try:
            if not path.is_file() or path.is_symlink():
                continue
            if path.stat().st_size > 256 * 1024:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if ("token-optimizer" in text or "measure.py" in text) and any(
            n in text for n in needles
        ):
            return True
    return False


def channel_owner(fields, config: dict | None = None) -> str:
    """Who emits on the Read channel: 'claude-import' (config presence, IDE
    toggle unverified) when Claude TO read hooks exist and no explicit mode
    overrides, else 'cursor'."""
    if config is None:
        config = _load_config()
    explicit = config.get("mode") in ("soft_block", "block", "warn")
    if not explicit and _claude_to_hooks_present(fields.get("workspace_roots")):
        return "claude-import"
    return "cursor"


# ---------------------------------------------------------------------------
# Per-session fingerprint store
# ---------------------------------------------------------------------------


def _store_path(sid: str) -> Path:
    safe = "".join(c for c in sid if c.isalnum() or c in "-_")[:64] or "unknown"
    return _to_dir() / "read-fingerprints" / f"{safe}.json"


def _load_store(sid: str) -> dict:
    try:
        path = _store_path(sid)
        if path.is_symlink() or path.stat().st_size > _MAX_STORE_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_store(sid: str, store: dict) -> None:
    try:
        d = _store_path(sid).parent
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        entries = store.get("files", {})
        if len(entries) > _MAX_STORE_ENTRIES:
            entries = dict(
                sorted(entries.items(), key=lambda kv: kv[1].get("ts", 0))[-_MAX_STORE_ENTRIES:]
            )
            store["files"] = entries
        path = _store_path(sid)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(store), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _read_fingerprint(path: Path) -> tuple[str, int, str | None] | None:
    """One read of the target: (sha1, size, text-if-delta-cacheable).

    The text is retained only within delta_diff's content-cache cap so the
    delta path never re-reads the file. Symlinks, unreadable and oversized
    files return None (fail open).
    """
    try:
        if path.is_symlink():
            return None
        st = path.stat()
        if not path.is_file() or st.st_size > _MAX_FILE_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    import hashlib

    text = None
    try:
        from delta_diff import MAX_CONTENT_CACHE_BYTES

        if len(raw) <= MAX_CONTENT_CACHE_BYTES:
            text = raw.decode("utf-8", errors="replace")
    except ImportError:
        pass
    return hashlib.sha1(raw).hexdigest(), st.st_size, text


def _structure_message(file_path: str) -> str | None:
    """Bounded, redacted structure-map message via the Claude builders."""
    try:
        import read_cache
        from structure_map import summarize_code_file

        summary = summarize_code_file(file_path)
        if summary is None or not getattr(summary, "replacement_text", ""):
            return None
        text = summary.replacement_text
        if len(text) > 2000:
            text = text[:2000] + "\n[truncated: structure map exceeds budget]"
            summary.replacement_text = text
        saved = max(1, len(text) // 4)
        msg = read_cache._build_structure_message(file_path, summary, saved)
        from credential_patterns import redact_credentials

        return redact_credentials(msg) or None
    except Exception:
        return None


def _reason_message(file_path: str) -> str:
    try:
        import read_cache

        return read_cache._build_reason_only_message(file_path)
    except Exception:
        name = Path(file_path).name
        return f"[Token Optimizer] {name} is unchanged (already read this session)."


def _eligible(file_path: str) -> bool:
    try:
        from structure_map import is_structure_supported_file

        if not is_structure_supported_file(file_path):
            return False
    except Exception:
        return False
    try:
        import read_cache

        if read_cache._is_contextignored(file_path):
            return False
    except Exception:
        pass  # contextignore unavailable: eligibility stands on file type
    return True


def evaluate(fields) -> dict | None:
    """Evaluate a Cursor preToolUse Read payload.

    Returns {"emit": <bridge emit object>, "observed": <ledger kwargs>} or
    None (emit nothing). Never raises.
    """
    try:
        return _evaluate(fields)
    except Exception:
        return None


def _evaluate(fields) -> dict | None:
    if fields.get("tool_name") != "Read":
        return None
    args = fields.get("tool_args")
    if not isinstance(args, dict):
        return None
    file_path = args.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        return None
    # Partial reads are never intercepted.
    for key in ("offset", "limit"):
        value = args.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value:
            return None
    file_path = file_path[:1024]
    if not _eligible(file_path):
        return None

    sid = fields.get("conversation_id") or "unknown"
    path = Path(file_path)
    fp = _read_fingerprint(path)
    if fp is None:
        return None
    digest, size, current = fp

    config = _load_config()
    mode = _mode_from(config)
    owner = channel_owner(fields, config)
    if owner != "cursor":
        mode = "shadow"  # KTD9 defer: record-only while the import may own it

    store = _load_store(sid)
    files = store.setdefault("files", {})
    entry = files.get(file_path)
    base = {"mode": mode, "owner": owner, "path": Path(file_path).name}

    if entry is None:
        # First read THIS session: record the fingerprint, never deny.
        new_entry = {"hash": digest, "size": size, "ts": time.time()}
        if current is not None:
            new_entry["content"] = current
        files[file_path] = new_entry
        _save_store(sid, store)
        return {"emit": None, "observed": {**base, "decision": "first_read"}}

    if entry.get("hash") != digest:
        # Changed since the observed read: serve a delta when we hold the
        # base content and the file is delta-eligible, else treat as new.
        old_content = entry.get("content")
        files[file_path] = {
            "hash": digest, "size": size, "ts": time.time(),
            **({"content": current} if current is not None else {}),
        }
        _save_store(sid, store)
        if old_content and current:
            try:
                from delta_diff import compute_delta, is_delta_eligible

                if is_delta_eligible(file_path):
                    delta_text, _stats = compute_delta(old_content, current,
                                                       filename=Path(file_path).name)
                    if delta_text:
                        from credential_patterns import redact_credentials

                        delta_text = redact_credentials(delta_text) or delta_text
                        msg = (f"[Token Optimizer] {Path(file_path).name} changed since "
                               f"your last read this session. Delta:\n\n{delta_text}")
                        return _decide(mode, msg, base, "delta")
            except Exception:
                pass
        return {"emit": None, "observed": {**base, "decision": "changed_reread"}}

    # Unchanged redundant read. No store write: the ts refresh would be a
    # per-Read-hook disk write for pruning metadata only.
    msg = _structure_message(file_path) or _reason_message(file_path)
    return _decide(mode, msg, base, "redundant")


def _decide(mode: str, message: str, base: dict, kind: str) -> dict:
    """Map (mode, message) onto the emit contract; shadow emits nothing."""
    if mode == "shadow":
        return {"emit": None, "observed": {**base, "decision": f"shadow_{kind}"}}
    if mode == "warn":
        return {
            "emit": {"user_message": message},
            "observed": {**base, "decision": f"warn_{kind}"},
        }
    if mode == "block":
        note = _reason_message_from(message)
        return {
            "emit": {"permission": "deny", "user_message": note},
            "observed": {**base, "decision": "deny_reason"},
        }
    # soft_block
    decision = "deny_inject" if kind == "redundant" else f"deny_{kind}"
    return {
        "emit": {"permission": "deny", "user_message": message},
        "observed": {**base, "decision": decision},
    }


def _reason_message_from(message: str) -> str:
    first = message.split("\n", 1)[0]
    return first if first else "[Token Optimizer] redundant read blocked."


# ---------------------------------------------------------------------------
# Doctor channel evidence
# ---------------------------------------------------------------------------


def channel_state(ledger_events) -> dict:
    """Compute the Read-channel state for cursor-doctor.

    ``ledger_events`` is the observed-events ledger (list of dicts, oldest
    first). State ladder: no config + no events -> ``unknown``; shadow mode
    or no deny events -> ``shadow``; deny events -> ``armed-unverified``.

    There is NO ledger path to ``armed-verified``: a denied file absent
    from later postToolUse events is negative evidence, consistent with
    cancels, failures, dropped hooks and tool routing - never proof of
    delivery or adaptation. Verification requires positive controlled
    native evidence (a real Cursor session demonstrably receiving the deny
    + user_message and adapting), which no current source establishes.
    """
    mode = configured_mode()
    owner_claude = False
    try:
        owner_claude = _claude_to_hooks_present([])
    except Exception:
        pass
    events = [e for e in (ledger_events or []) if isinstance(e, dict)]
    denies = any(
        e.get("event") == "readCache"
        and str(e.get("decision") or "").startswith("deny")
        for e in events
    )
    if not events and mode == _DEFAULT_MODE:
        state = "unknown"
    elif mode == "shadow" or not denies:
        state = "shadow"
    else:
        state = "armed-unverified"
    return {
        "channel": "read",
        "state": state,
        "mode": mode,
        "claude_import_config_present": owner_claude,
        "note": ("Cursor's Claude-hook import toggle has no verified file "
                 "surface, so KTD9 ownership from config presence is "
                 "inconclusive (record-only defer avoids damage, parity is "
                 "not closed by it). armed-verified needs positive "
                 "controlled native evidence; absence never verifies."),
    }
