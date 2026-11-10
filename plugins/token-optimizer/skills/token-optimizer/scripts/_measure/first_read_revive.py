"""First-read replacement only after a new, explicit cohort re-promotion."""
import json
import math
import os
from datetime import datetime
from pathlib import Path


def enabled(sidecar_path, language, band):
    """Old promotions and malformed sidecars never activate target replacement."""
    try:
        data = json.loads(Path(sidecar_path).read_text(encoding="utf-8"))
        promoted = data["promoted_at"]
        demoted = data["demoted"]
        key = language + "|" + band
        stamp = promoted[key]
        marker = promoted.get("target_skeleton_v1|" + key)
        return (isinstance(demoted, list) and [language, band] not in demoted
                and isinstance(stamp, (int, float)) and not isinstance(stamp, bool)
                and math.isfinite(stamp) and stamp > 0
                and isinstance(marker, (int, float)) and not isinstance(marker, bool)
                and marker == stamp)
    except (OSError, ValueError, TypeError, OverflowError, KeyError, AttributeError):
        return False


def serve(file_path, stat, language, band, session_id, content, result, orig_tokens, estimate, emit,
          snapshot_dir=None, tool_use_id="", allow_context=None, strict=False, parent_session_id=""):
    """Archive first, emit a real denial, then log its estimated net removal."""
    try:
        from archive_result import archive_entry_exists, archive_original, build_archive_pointer, derive_archive_key
        source = Path(file_path)
        content = source.read_text(encoding="utf-8", errors="replace")
        current = source.stat()
        if current.st_mtime_ns != stat.st_mtime_ns or current.st_size != stat.st_size:
            return False
        orig_tokens = min(orig_tokens, int(estimate(content)))
        key = derive_archive_key(session_id, file_path, stat.st_mtime_ns)
        if archive_original(content, session_id, key, "Read", quiet=True, file_path=file_path, language=language) is None:
            return False
        if not archive_entry_exists(session_id, key):
            return False
        body = build_archive_pointer(result.replacement_text, len(content), key)
        reason = "large-file first read served as a code outline; use expand or a ranged Read for the full file."
        context = "[Token Optimizer] First-read code outline.\n" + body
        if allow_context is not None and not allow_context(context, strict):
            return False
        replacement_tokens = int(estimate(reason) + estimate(context))
        if replacement_tokens <= 0 or replacement_tokens >= orig_tokens:
            return False
        emit("deny", reason, context)
    except Exception:
        return False
    if snapshot_dir is not None and tool_use_id:
        try:
            from .periphery_exposure import record_target_outline
            rows = record_target_outline(snapshot_dir, session_id, tool_use_id, file_path, language, band)
            if rows:
                from .first_read_outcomes import _hash
                rows[0].update(timestamp_local=datetime.now().isoformat(), original_tokens=orig_tokens,
                               compressed_tokens=replacement_tokens, parent_session_id=_hash(parent_session_id or session_id))
                path = Path(snapshot_dir) / "diagnostics" / "first_read_pending.jsonl"
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as stream:
                    stream.write(json.dumps(rows[0], separators=(",", ":")) + "\n")
        except Exception:
            pass
    return True


def reconcile(conn, snapshot_dir):
    """Append measured rows only after the transcript confirms the denial."""
    from .first_read_outcomes import _hash, inspect
    from .setup_attachment_counter import read_transcript
    from .counted_subagents import subagent_files
    path = Path(snapshot_dir) / "diagnostics" / "first_read_pending.jsonl"
    if not path.is_file():
        return 0
    try:
        pending = []
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        pending.append(row)
                except ValueError:
                    continue
        conn.execute("CREATE TABLE IF NOT EXISTS first_read_confirmations (exposure_id TEXT PRIMARY KEY, event_id INTEGER NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS first_read_confirmation_scopes (event_id INTEGER PRIMARY KEY, tool_id_hash TEXT NOT NULL)")
        done = {row[0] for row in conn.execute("SELECT exposure_id FROM first_read_confirmations")}
        sessions = {}
        for uuid, transcript in conn.execute("SELECT DISTINCT session_uuid,jsonl_path FROM session_log WHERE session_uuid IS NOT NULL AND jsonl_path IS NOT NULL"):
            sessions.setdefault(_hash(uuid), []).append((uuid, transcript))
        written = 0
        for row in pending:
            ident = row.get("exposure_id")
            matches = sessions.get(row.get("parent_session_id") or row.get("session_id"), [])
            if not ident or ident in done or len(matches) != 1:
                continue
            uuid, transcript = matches[0]
            try:
                subagent = row.get("session_id") != _hash(uuid)
                if subagent:
                    candidates = [p for p in subagent_files(transcript) if p.stem.startswith("agent-") and _hash(p.stem[6:]) == row.get("session_id")]
                    if len(candidates) != 1:
                        continue
                    transcript = candidates[0]
                original, compressed = int(row["original_tokens"]), int(row["compressed_tokens"])
                timestamp = row["timestamp_local"]
                datetime.fromisoformat(timestamp)
                if original <= compressed or compressed <= 0 or not inspect(read_transcript(transcript), row)["confirmed"]:
                    continue
                conn.execute("SAVEPOINT first_read_confirmation")
                cursor = conn.execute("INSERT INTO compression_events(timestamp,session_id,session_uuid,feature,command_pattern,original_tokens,compressed_tokens,compression_ratio,quality_preserved,verified,detail,model,tier) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                      (timestamp, uuid, uuid, "first_read_skeleton", str(row["language"]) + "|" + str(row["band"]), original, compressed, 1 - compressed / original, 0, 1, "repromoted target outline; quality not verified" + ("; subagent" if subagent else ""), "unknown", "measured"))
                if cursor.rowcount != 1:
                    raise RuntimeError("event was not inserted")
                receipt = conn.execute("INSERT INTO first_read_confirmations(exposure_id,event_id) VALUES(?,?)", (ident, cursor.lastrowid))
                if subagent:
                    conn.execute("INSERT INTO first_read_confirmation_scopes(event_id,tool_id_hash) VALUES(?,?)", (cursor.lastrowid, row["tool_use_id"]))
                stored = conn.execute("SELECT original_tokens,compressed_tokens,tier FROM compression_events WHERE id=?", (cursor.lastrowid,)).fetchone()
                if receipt.rowcount != 1 or stored != (original, compressed, "measured"):
                    raise RuntimeError("event readback failed")
                conn.execute("RELEASE first_read_confirmation")
                done.add(ident)
                written += 1
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return written
    except Exception:
        try:
            conn.execute("ROLLBACK TO first_read_confirmation")
            conn.execute("RELEASE first_read_confirmation")
        except Exception:
            pass
        return 0
