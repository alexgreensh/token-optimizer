"""Cursor read-path parity (U2): shadow-first redundant-read interception.

Reuses the Claude path's ``structure_map`` eligibility and structure maps,
and ``read_cache``'s message builders and contextignore eligibility, and owns
only the Cursor-specific parts: payload shape (the bridge's decoded
``fields``), the per-session fingerprint store under the Cursor home, the
mode gate, and the deny + ``user_message`` emit contract.

Modes (same names as Claude, one deliberate difference): ``shadow``
(default: record what would be injected, emit nothing), ``soft_block``
(deny + injected map - BOUNDED here: after 3 denies for the same
session+file it degrades to ``warn`` so work always proceeds; Claude's
``soft_block`` denies without a bound), ``warn`` (allow + note), ``block``
(deny, reason only - never degrades). Arming is NEVER automatic:
``soft_block``/``block`` take effect only when the install's
``<cursor_home>/token-optimizer/read-cache.json`` names the mode. The
doctor reports the channel state (``unknown``/``shadow``/
``armed-unverified``) from the observed ledger; shadow-mode silence
neither arms nor confirms anything, and no ledger pattern verifies -
``armed-verified`` needs positive controlled native evidence no current
source provides.

Fingerprints are scoped to the conversation and promoted only after a
matching successful Read hook. Changed files are always allowed. KTD9
coexistence: when Claude Token Optimizer hooks
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
import hashlib
import fnmatch
import uuid
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

_MAX_STORE_ENTRIES = 200
_MAX_STORE_BYTES = 512 * 1024
_MAX_FILE_BYTES = 1024 * 1024  # never intercept reads of very large files
_MODES = ("shadow", "soft_block", "warn", "block")
_SOFT_BLOCK_MAX_DENIES = 3  # denials per session+file before degrading to warn
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
    safe = sid if sid and len(sid) <= 64 and all(c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in sid) else "~" + hashlib.sha256(sid.encode("utf-8", "surrogatepass")).hexdigest()
    return _to_dir() / "read-fingerprints" / f"{safe}.json"


def _file_key(file_path: str) -> str:
    if len(file_path) <= 1024:
        return file_path
    return "sha256:" + hashlib.sha256(file_path.encode("utf-8", "surrogatepass")).hexdigest()


def clear_session_reads(sid):
    """Advance the compaction epoch; if that fails, discard the store."""
    if not sid or sid == "unknown":
        return
    path = _store_path(sid).with_suffix(".epoch")
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".epoch-", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(uuid.uuid4().hex)
        for attempt in range(3):
            try:
                os.replace(tmp, path)
                return
            except OSError:
                if attempt < 2:
                    time.sleep(0.01)
    except OSError:
        pass
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    with _store_lock(sid) as acquired:
        if not acquired:
            raise OSError("cannot invalidate read fingerprints")
        _store_path(sid).unlink(missing_ok=True)


def _epoch(sid):
    path = _store_path(sid).with_suffix(".epoch")
    if path.is_symlink():
        raise ValueError("invalid epoch")
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _load_store(sid: str) -> dict:
    try:
        path = _store_path(sid)
        if path.is_symlink() or path.stat().st_size > _MAX_STORE_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        epoch = _epoch(sid)
        return data if isinstance(data, dict) and data.get("epoch", "") == epoch else {"epoch": epoch}
    except FileNotFoundError:
        return {"epoch": _epoch(sid)}
    except (OSError, ValueError):
        raise


@contextmanager
def _store_lock(sid):
    from hook_runtime import lease_lock
    directory = _store_path(sid).parent
    directory.mkdir(parents=True, exist_ok=True)
    with lease_lock(directory / ("." + _store_path(sid).stem + ".lock"),
                    acquire_timeout=0.075, cohort_throttle=False,
                    reclaim_released=True) as acquired:
        yield acquired


def _save_store(sid: str, store: dict) -> None:
    tmp = None
    try:
        path = _store_path(sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        store.setdefault("epoch", _epoch(sid))
        entries = store.setdefault("files", {})
        while entries:
            encoded = json.dumps(store).encode("utf-8")
            if len(entries) <= _MAX_STORE_ENTRIES and len(encoded) <= _MAX_STORE_BYTES:
                break
            oldest = min(entries, key=lambda key: entries[key].get("ts", 0))
            del entries[oldest]
        encoded = json.dumps(store).encode("utf-8")
        if len(encoded) > _MAX_STORE_BYTES:
            return
        fd, tmp = tempfile.mkstemp(prefix=".fingerprint-", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "wb") as fh:
            fh.write(encoded)
        os.replace(tmp, path)
        tmp = None
    except OSError:
        pass
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _redact_for_storage(text: str) -> str | None:
    """Redact file text before it reaches the persisted fingerprint store.

    Fail CLOSED: a redactor failure degrades the entry to hash-only (the
    cached content is omitted) rather than persisting raw file content
    (G1). Change detection is unaffected - the sha1 is over the raw bytes.
    """
    try:
        from credential_patterns import redact_credentials

        redacted = redact_credentials(text)
        return redacted if isinstance(redacted, str) else None
    except Exception:
        return None


def _read_fingerprint(path: Path) -> tuple[str, int, str | None] | None:
    """One read of the target: (sha1, size, redacted-text-if-delta-cacheable).

    The text is retained only within delta_diff's content-cache cap.
    Symlinks, unreadable and oversized
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
    text = None
    try:
        from delta_diff import MAX_CONTENT_CACHE_BYTES

        if len(raw) <= MAX_CONTENT_CACHE_BYTES:
            text = _redact_for_storage(raw.decode("utf-8", errors="replace"))
    except ImportError:
        pass
    return hashlib.sha1(raw).hexdigest(), st.st_size, text


def _structure_message(file_path: str) -> str | None:
    """Bounded, redacted structure-map message via the Claude builders."""
    try:
        import read_cache
        from structure_map import summarize_code_file

        summary = summarize_code_file(file_path)
        if summary is None or not getattr(summary, "eligible", False) or not getattr(summary, "replacement_text", ""):
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
        from structure_map import NON_CODE_SUFFIXES, is_structure_supported_file

        if Path(file_path).suffix.lower() in NON_CODE_SUFFIXES or not is_structure_supported_file(file_path):
            return False
    except Exception:
        return False
    return True



def _resolve_read_path(fields, file_path):
    roots = fields.get("workspace_roots") or []
    root = fields.get("cwd") or (roots[0] if roots else None)
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        if not root or not Path(root).is_absolute():
            return None
        path = Path(root) / path
    if path.is_symlink():
        return None
    return os.path.normcase(str(path.resolve()))


def _contextignored(path, fields):
    roots = fields.get("workspace_roots") or []
    root = roots[0] if roots else fields.get("cwd")
    root = str(Path(root).resolve()) if root else None
    from runtime_env import runtime_home
    candidates = ([Path(root) / ".contextignore"] if root else []) + [runtime_home() / ".contextignore"]
    for ignore in candidates:
        if not ignore.is_file() or ignore.is_symlink():
            continue
        if ignore.stat().st_size > 256 * 1024:
            return True
        for line in ignore.read_text(encoding="utf-8").splitlines()[:1000]:
            pattern = line.strip()
            if pattern and not pattern.startswith("#"):
                relative = os.path.relpath(path, root) if root else path
                values = (path, relative, Path(path).name)
                if any(fnmatch.fnmatch(value, pattern) for value in values):
                    return True
                if pattern.endswith("/") and (relative.replace(os.sep, "/") + "/").startswith(pattern):
                    return True
    return False


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
    file_path = _resolve_read_path(fields, file_path)
    if not file_path or not _eligible(file_path) or _contextignored(file_path, fields):
        return None

    sid = fields.get("cache_conversation_id") or fields.get("conversation_id")
    agent = fields.get("agent_id")
    if not sid or sid == "unknown" or not isinstance(agent, str) or not agent:
        return None
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

    with _store_lock(sid) as acquired:
        if not acquired:
            return None
        return _evaluate_locked(fields, sid, file_path, digest, size, current, mode, owner)


def _evaluate_locked(fields, sid, file_path, digest, size, current, mode, owner):
    store = _load_store(sid)
    files = store.setdefault("files", {})
    key = hashlib.sha256(fields["agent_id"].encode("utf-8", "surrogatepass")).hexdigest() + ":" + _file_key(file_path)
    entry = files.get(key)
    base = {"mode": mode, "owner": owner, "path": Path(file_path).name}

    if entry is None or not entry.get("hash"):
        # A pre-hook cannot prove the model received a first read either.
        new_entry = {"hash": digest, "size": size, "ts": time.time()}
        if current is not None:
            new_entry["content"] = current
        new_entry["tool_use_id"] = fields.get("tool_use_id") or ""
        files[key] = {"ts": new_entry["ts"], "pending": new_entry}
        _save_store(sid, store)
        return {"emit": None, "observed": {**base, "decision": "first_read"}}

    if entry.get("hash") != digest:
        # Stage only: a postToolUse success confirms that the new bytes arrived.
        entry["pending"] = {"hash": digest, "size": size, "ts": time.time(),
                            "tool_use_id": fields.get("tool_use_id") or "",
                            **({"content": current} if current is not None else {})}
        _save_store(sid, store)
        return {"emit": None, "observed": {**base, "decision": "changed_reread"}}

    # Unchanged redundant read. Shadow/warn do no store write: the ts
    # refresh would be a per-Read-hook disk write for pruning metadata only.
    msg = _structure_message(file_path)
    if not msg:
        return _decide("warn" if mode in ("soft_block", "block") else mode,
                       _reason_message(file_path), base, "redundant")
    return _soft_block_decide(mode, msg, base, "redundant", store, sid, key, entry)


def confirm_read(fields):
    """Promote a staged changed fingerprint only after a successful Read hook."""
    try:
        sid = fields.get("cache_conversation_id") or fields.get("conversation_id")
        agent = fields.get("agent_id")
        path = fields.get("tool_args", {}).get("file_path")
        if not sid or sid == "unknown" or not isinstance(agent, str) or not agent or not isinstance(path, str):
            return
        path = _resolve_read_path(fields, path)
        if not path:
            return
        key = hashlib.sha256(agent.encode("utf-8", "surrogatepass")).hexdigest() + ":" + _file_key(path)
        with _store_lock(sid) as acquired:
            if not acquired:
                return
            store = _load_store(sid)
            entry = store.get("files", {}).get(key)
            pending = entry.get("pending") if isinstance(entry, dict) else None
            if not isinstance(pending, dict):
                return
            expected_id = pending.get("tool_use_id")
            if not expected_id or expected_id != fields.get("tool_use_id"):
                return
            fp = _read_fingerprint(Path(path))
            if fp is None or fp[0] != pending.get("hash"):
                return
            pending.pop("tool_use_id", None)
            store["files"][key] = pending
            _save_store(sid, store)
    except Exception:
        return


def _soft_block_decide(mode, msg, base, kind, store, sid, file_path, entry):
    """_decide with a bounded soft_block: deny loops degrade to warn (G0).

    The model gets _SOFT_BLOCK_MAX_DENIES real denials (each carrying the
    injected map), then soft_block degrades to warn so work can always
    proceed. Explicit block mode never degrades. The counter costs one store
    write per DENIED read only; other modes keep the write-free hot path.
    """
    if mode != "soft_block":
        return _decide(mode, msg, base, kind)
    denies = int(entry.get("denies", 0) or 0) + 1
    entry["denies"] = denies
    store.setdefault("files", {})[file_path] = entry
    _save_store(sid, store)
    if denies > _SOFT_BLOCK_MAX_DENIES:
        return _decide("warn", msg, base, kind)
    return _decide(mode, msg, base, kind)


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
