"""Pending command-rewrite staging and post-execution pairing (G0).

Split out of cursor_hook_bridge.py under the repo's 1,000-line file-size
ratchet (scripts/check_file_size.py). The data-dir resolver and the atomic
writer are INJECTED by the bridge's same-name wrappers at call time, so tests
monkeypatching cursor_hook_bridge._to_dir / ._atomic_write_json keep working.
Hash-only staging: rewritten command text never touches disk.
"""

from __future__ import annotations

import json
import os
import time

from cursor_bridge_payload import _SESSION_ID_RE

_PENDING_REWRITE_MAX_AGE = 600  # seconds; staged rewrite evidence horizon


def _pending_rewrite_path(fields, to_dir):
    sid = _SESSION_ID_RE.sub("", str(fields.get("conversation_id") or ""))[:64]
    tuid = _SESSION_ID_RE.sub("", str(fields.get("tool_use_id") or ""))[:64]
    if len(sid) < 6 or len(tuid) < 6:
        return None
    if to_dir is None:
        return None
    return to_dir / "pending-rewrites" / f"{sid}--{tuid}.json"


def _stage_pending_rewrite(fields, rewritten, *, to_dir, atomic_write):
    """Stage THIS session+tool_use_id's rewrite for postToolUse pairing.

    Hash-only: the rewritten command text never touches disk (a whitelisted
    command can still embed a credential in its arguments). Equality is
    proven by comparing digests at postToolUse. Best-effort and bounded:
    the dir is pruned by age on each stage. Never raises.
    """
    try:
        path = _pending_rewrite_path(fields, to_dir)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        now = time.time()
        for old in path.parent.glob("*.json"):
            try:
                if now - old.stat().st_mtime > _PENDING_REWRITE_MAX_AGE:
                    old.unlink()
            except OSError:
                pass
        import hashlib

        atomic_write(path, {
            "sha256": hashlib.sha256(rewritten.encode("utf-8")).hexdigest(),
            "ts": now,
        })
    except Exception:
        pass


def _classify_rewrite(fields, *, to_dir):
    """Pair the executed Shell command with this tool_use_id's staged rewrite.

    honoured: the executed command's digest matches the staged digest.
    ignored: a rewrite was staged but a different command actually ran.
    None: no staged attempt (unknown - never proof in either direction, G0).
    The pop is claim-by-rename so two processes can never both classify one
    staged rewrite.
    """
    try:
        path = _pending_rewrite_path(fields, to_dir)
        if path is None or not path.is_file() or path.is_symlink():
            return None
        claimed = path.with_name(path.name + f".claimed-{os.getpid()}")
        try:
            os.replace(path, claimed)
        except OSError:
            return None  # another process claimed it first (or it vanished)
        try:
            staged = json.loads(claimed.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            staged = None
        try:
            claimed.unlink()
        except OSError:
            pass
        if not isinstance(staged, dict):
            return None
        expected = staged.get("sha256")
        if not isinstance(expected, str):
            return None
        actual = fields["tool_args"].get("command")
        if not isinstance(actual, str) or not actual:
            # Missing/invalid executed-command data is UNKNOWN, never
            # 'ignored' - we cannot say what Cursor actually ran.
            return None
        import hashlib

        digest = hashlib.sha256(actual.encode("utf-8")).hexdigest()
        return "honoured" if digest == expected else "ignored"
    except Exception:
        return None
