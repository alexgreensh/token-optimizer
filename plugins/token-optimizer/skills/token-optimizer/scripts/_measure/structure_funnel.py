"""Privacy-bounded diagnostics for structure-map decisions."""
import json
import hashlib
import os
import time
from collections import Counter

REASONS = frozenset(("ok", "unsupported_language", "partial_range_not_supported",
                     "empty_file", "below_min_tokens", "over_ast_caps", "generated_like",
                     "ast_parse_failed", "structure_over_cap", "fallback_digest",
                     "low_signal_structure", "too_large_to_parse", "parse_error",
                     "no_keys", "no_headings", "no_structure", "unreadable"))


class Funnel:
    def __init__(self, session_id, agent_id, tool_use_id, sink):
        self.sink = sink
        self.row = {"timestamp": time.time(), "session_id": _key(session_id),
                    "agent_id": _key(agent_id), "tool_use_id": _key(tool_use_id),
                    "hook_entered": True, "state_found": None, "mtime_match": None,
                    "size_match": None, "range_covered": None, "structure_eligible": None,
                    "structure_reason": None, "exit_reason": None, "mode": None,
                    "output_emitted": False, "event_written": False}

    def state(self, found):
        self.row["state_found"] = bool(found)

    def metadata(self, mtime, size, covered):
        self.row.update(mtime_match=bool(mtime), size_match=bool(size), range_covered=bool(covered))

    def structure(self, eligible, reason):
        self.row["structure_eligible"] = bool(eligible)
        self.row["structure_reason"] = reason if reason in REASONS else "other"

    def finish(self):
        if not self.row["session_id"] or not self.row["tool_use_id"]:
            return
        try:
            self.sink(self.row)
        except (OSError, TypeError, ValueError):
            pass


def _key(value):
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest() if value else ""


def append(snapshot_dir, row):
    """Append a single path-free record; do not store file names or content."""
    directory = snapshot_dir / "diagnostics"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(directory / "structure_funnel.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")


def summarize(records, transcript_rows):
    """Summarize one session; transcript results provide conservative delivery evidence."""
    if len({r.get("session_id") for r in records if r.get("session_id")}) > 1:
        raise ValueError("one session per replay")
    counts = Counter((r.get("session_id"), r.get("agent_id"), r.get("tool_use_id")) for r in records)
    delivered_ids = set()
    for row in transcript_rows:
        message = row.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            text = block.get("content")
            if isinstance(text, list):
                text = " ".join(str(x.get("text") or "") for x in text if isinstance(x, dict))
            if not isinstance(text, str) or len(text) >= 500:
                continue
            if "unchanged and already in context" in text.lower() and block.get("tool_use_id"):
                delivered_ids.add(_key(block["tool_use_id"]))
    out = Counter()
    out["ambiguous_duplicates"] = sum(n > 1 for n in counts.values())
    for r in records:
        key = (r.get("session_id"), r.get("agent_id"), r.get("tool_use_id"))
        if not all((key[0], key[2])) or counts[key] != 1:
            continue
        out["hook_entered"] += 1
        out["state_found"] += r.get("state_found") is True
        out["eligible_metadata_range"] += all(r.get(k) is True for k in ("mtime_match", "size_match", "range_covered"))
        eligible = r.get("structure_eligible") is True
        out["eligible_structure"] += eligible
        out["output_emitted"] += eligible and r.get("output_emitted") is True
        delivered = eligible and r.get("output_emitted") is True and r.get("tool_use_id") in delivered_ids
        out["delivered"] += delivered
        out["event_written"] += eligible and r.get("event_written") is True
        out["event_without_delivery_evidence"] += eligible and r.get("event_written") is True and not delivered
        reason = r.get("structure_reason")
        if reason in REASONS or reason == "other":
            out["reason:" + reason] += 1
    for name in ("hook_entered", "state_found", "eligible_metadata_range", "eligible_structure", "output_emitted", "delivered", "event_written", "event_without_delivery_evidence", "ambiguous_duplicates"):
        out.setdefault(name, 0)
    return dict(out)
