#!/usr/bin/env python3
"""
Offline proof runner for structure-aware compression.

This script replays local Claude session JSONL transcripts and optional
read-cache decision logs, then simulates a conservative Python-only
structure-map replacement for redundant whole-file rereads.

Goals:
  - stdlib only
  - no remote telemetry
  - separate transcript and decision-log summaries to avoid double counting
  - conservative eligibility checks to avoid overstating savings
  - compact proof metrics for before/after reporting

Usage examples:
  python3 structure_replay.py
  python3 structure_replay.py ~/.claude/projects/.../session.jsonl
  python3 structure_replay.py --json ~/.claude/projects .../read-cache/decisions
  python3 structure_replay.py --torture
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from structure_map import (
    StructureMapResult,
    detect_structure_language,
    estimate_tokens,
    is_python_file,
    is_structure_supported_file,
    summarize_code_source,
)

DEFAULT_MIN_FILE_TOKENS = 1000
DEFAULT_MAX_BYTES = 400 * 1024
DEFAULT_MAX_LINES = 5000
DEFAULT_STRUCTURE_CAPS = {
    "signatures": 500,
    "top_level": 700,
    "skeleton": 1200,
}
DEFAULT_REMINDER_OVERHEAD_TOKENS = 20
DEFAULT_REASON_ONLY_OVERHEAD_TOKENS = 10


@dataclass
class ReplayEvent:
    source_path: Path
    source_kind: str  # "transcript" or "decision_log"
    session_id: str
    timestamp: str
    order: int
    event_kind: str  # "read", "edit", "write", "decision"
    raw_file_path: str
    normalized_file_path: str
    tool_use_id: str = ""
    cwd: str = ""
    offset: int = 0
    limit: int = 0
    pages: str = ""
    decision: str = ""
    reason: str = ""
    tokens_est_hint: Optional[int] = None
    replacement_tokens_est: Optional[int] = None
    net_saved_tokens_est: Optional[int] = None
    repeat_replacement_count: int = 0
    actual_substitution: bool = False
    read_result_text: str = ""
    read_result_tokens_est: Optional[int] = None
    read_result_total_lines: Optional[int] = None
    read_result_start_line: Optional[int] = None
    read_result_num_lines: Optional[int] = None
    is_error: bool = False
    event_id: str = ""

    @property
    def is_whole_file(self) -> bool:
        return self.event_kind == "read" and self.offset == 0 and self.limit == 0 and not self.pages

    @property
    def read_signature(self) -> Tuple[int, int, str]:
        return (self.offset, self.limit, self.pages or "")


@dataclass
class FileSnapshot:
    path: Path
    exists: bool
    provenance: str = "disk"
    stat_key: str = ""
    size_bytes: int = 0
    line_count: int = 0
    mtime_ns: int = 0
    sha256: str = ""
    tokens_est: int = 0
    text: str = ""
    generated_like: bool = False
    parse_error: str = ""


@dataclass
class ReplayFileStats:
    source_path: str
    normalized_file_path: str
    session_id: str
    total_reads: int = 0
    whole_file_reads: int = 0
    redundant_candidates: int = 0
    eligible_replacements: int = 0
    repeat_replacements: int = 0
    verified_replacements: int = 0
    skipped: Counter = field(default_factory=Counter)
    baseline_tokens: int = 0
    structure_tokens: int = 0
    reminder_tokens: int = 0
    reason_only_tokens: int = 0
    net_saved_tokens: int = 0
    directional_baseline_tokens: int = 0
    directional_saved_tokens: int = 0
    historical_result_backed: int = 0
    current_disk_fallbacks: int = 0
    replacement_followups: int = 0
    replacement_different_range_followups: int = 0
    error_replacement_attempts: int = 0
    replacement_fingerprints: set = field(default_factory=set)


@dataclass
class ReplaySummary:
    source_kind: str
    source_count: int = 0
    sessions: int = 0
    total_events: int = 0
    read_events: int = 0
    edit_events: int = 0
    candidate_events: int = 0
    eligible_replacements: int = 0
    repeat_replacements: int = 0
    verified_replacements: int = 0
    baseline_tokens: int = 0
    structure_tokens: int = 0
    reminder_tokens: int = 0
    reason_only_tokens: int = 0
    net_saved_tokens: int = 0
    directional_baseline_tokens: int = 0
    directional_saved_tokens: int = 0
    historical_result_backed: int = 0
    current_disk_fallbacks: int = 0
    skipped: Counter = field(default_factory=Counter)
    files: Dict[str, ReplayFileStats] = field(default_factory=dict)
    sessions_seen: set = field(default_factory=set)

    def add_file_stats(self, stats: ReplayFileStats) -> None:
        self.files[stats.normalized_file_path] = stats
        self.eligible_replacements += stats.eligible_replacements
        self.repeat_replacements += stats.repeat_replacements
        self.verified_replacements += stats.verified_replacements
        self.baseline_tokens += stats.baseline_tokens
        self.structure_tokens += stats.structure_tokens
        self.reminder_tokens += stats.reminder_tokens
        self.reason_only_tokens += stats.reason_only_tokens
        self.net_saved_tokens += stats.net_saved_tokens
        self.directional_baseline_tokens += stats.directional_baseline_tokens
        self.directional_saved_tokens += stats.directional_saved_tokens
        self.historical_result_backed += stats.historical_result_backed
        self.current_disk_fallbacks += stats.current_disk_fallbacks
        self.skipped.update(stats.skipped)
        self.sessions_seen.add(stats.session_id)

    @property
    def capture_rate(self) -> float:
        if self.candidate_events == 0:
            return 0.0
        return self.eligible_replacements / self.candidate_events

    @property
    def repeat_share(self) -> float:
        if self.eligible_replacements == 0:
            return 0.0
        return self.repeat_replacements / self.eligible_replacements

    @property
    def savings_rate(self) -> float:
        if self.baseline_tokens == 0:
            return 0.0
        return self.net_saved_tokens / self.baseline_tokens

    @property
    def directional_savings_rate(self) -> float:
        if self.directional_baseline_tokens == 0:
            return 0.0
        return self.directional_saved_tokens / self.directional_baseline_tokens

    @property
    def replacement_events(self) -> int:
        return self.eligible_replacements + self.repeat_replacements

    @property
    def followup_rate(self) -> float:
        replacement_events = self.replacement_events
        if replacement_events == 0:
            return 0.0
        total_followups = sum(stats.replacement_followups for stats in self.files.values())
        return total_followups / replacement_events

    @property
    def false_block_proxy_rate(self) -> float:
        replacement_events = self.replacement_events
        if replacement_events == 0:
            return 0.0
        total_followups = sum(
            stats.replacement_different_range_followups for stats in self.files.values()
        )
        return total_followups / replacement_events


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_session_id(value: Any, fallback: str = "unknown") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


def _normalize_path_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return str(Path(text).expanduser())


def _normalize_file_path(value: Any) -> str:
    return _normalize_path_text(value)


def _normalize_timestamp(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (OverflowError, ValueError):
            return str(value)
    return str(value)


def _iter_jsonl_records(path: Path) -> Iterator[Tuple[int, dict]]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            for index, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield index, obj
    except OSError:
        return


def _walk_values(value: Any) -> Iterator[dict]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk_values(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_values(item)


def _looks_like_tool_use(obj: dict) -> bool:
    name = obj.get("name")
    input_obj = obj.get("input")
    return isinstance(name, str) and isinstance(input_obj, dict)


def _tool_name(obj: dict) -> str:
    return str(obj.get("name") or "").strip()


def _extract_file_path(input_obj: dict) -> str:
    for key in ("file_path", "path", "file", "filename"):
        value = input_obj.get(key)
        if value:
            return _normalize_file_path(value)
    return ""


def _extract_read_fields(input_obj: dict) -> Tuple[int, int, str]:
    offset = _safe_int(input_obj.get("offset"), 0)
    limit = _safe_int(input_obj.get("limit"), 0)
    pages = ""
    if "pages" in input_obj and input_obj.get("pages") is not None:
        pages = str(input_obj.get("pages")).strip()
    elif "page" in input_obj and input_obj.get("page") is not None:
        pages = str(input_obj.get("page")).strip()
    return offset, limit, pages


def _extract_event_id(record: dict, line_no: int, source_path: Path) -> str:
    for key in ("uuid", "toolUseID", "tool_use_id", "id", "event_id", "requestId"):
        value = record.get(key)
        if value:
            return str(value)
    return f"{source_path.name}:{line_no}"


def _extract_read_result_payload(
    block: dict,
    tool_use_result: Any,
) -> Tuple[str, Optional[int], Optional[int], Optional[int], Optional[int]]:
    text = ""
    tokens_est: Optional[int] = None
    start_line: Optional[int] = None
    num_lines: Optional[int] = None
    total_lines: Optional[int] = None

    if isinstance(tool_use_result, dict):
        file_payload = tool_use_result.get("file")
        if isinstance(file_payload, dict):
            content = file_payload.get("content")
            if isinstance(content, str):
                text = content
                tokens_est = estimate_tokens(content)
            start_line = _safe_int(file_payload.get("startLine"), 0) or None
            num_lines = _safe_int(file_payload.get("numLines"), 0) or None
            total_lines = _safe_int(file_payload.get("totalLines"), 0) or None
        elif isinstance(tool_use_result.get("content"), str):
            text = str(tool_use_result.get("content"))
            tokens_est = estimate_tokens(text)

    if not text:
        content = block.get("content")
        if isinstance(content, str):
            text = content
            tokens_est = estimate_tokens(text)
        elif isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                    text_parts.append(item["text"])
            if text_parts:
                text = "\n".join(text_parts)
                tokens_est = estimate_tokens(text)

    return text, tokens_est, start_line, num_lines, total_lines


def _classify_jsonl(path: Path) -> str:
    for _, record in _iter_jsonl_records(path):
        if "decision" in record and ("file" in record or "file_path" in record):
            return "decision_log"
        if "message" in record or "type" in record or "toolUseResult" in record:
            return "transcript"
        break
    return "transcript"


def _extract_transcript_events(path: Path) -> List[ReplayEvent]:
    events: List[ReplayEvent] = []
    tool_events_by_id: Dict[str, ReplayEvent] = {}
    seen_tool_ids: set[str] = set()
    for line_no, record in _iter_jsonl_records(path):
        session_id = _normalize_session_id(
            record.get("sessionId") or record.get("session_id") or record.get("session") or path.stem
        )
        timestamp = _normalize_timestamp(record.get("timestamp"))
        event_id_prefix = _extract_event_id(record, line_no, path)
        cwd = _normalize_path_text(record.get("cwd"))
        msg = record.get("message", {})
        content = msg.get("content", []) if isinstance(msg, dict) else []

        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_name = str(block.get("name") or "").strip().lower()
                input_obj = block.get("input", {})
                if not isinstance(input_obj, dict):
                    continue
                raw_file_path = _extract_file_path(input_obj)
                if not raw_file_path:
                    continue
                tool_use_id = str(block.get("id") or "")
                dedupe_key = f"{session_id}:{tool_use_id}"
                if tool_use_id and dedupe_key in seen_tool_ids:
                    continue
                if tool_use_id:
                    seen_tool_ids.add(dedupe_key)

                normalized_file_path = str(Path(raw_file_path).expanduser())
                event_kind = "read" if tool_name == "read" else "edit" if tool_name in {
                    "edit",
                    "write",
                    "multiedit",
                    "notebookedit",
                } else ""
                if not event_kind:
                    continue
                offset, limit, pages = _extract_read_fields(input_obj) if event_kind == "read" else (0, 0, "")
                event = ReplayEvent(
                    source_path=path,
                    source_kind="transcript",
                    session_id=session_id,
                    timestamp=timestamp,
                    order=len(events),
                    event_kind=event_kind,
                    raw_file_path=raw_file_path,
                    normalized_file_path=normalized_file_path,
                    tool_use_id=tool_use_id,
                    cwd=cwd,
                    offset=offset,
                    limit=limit,
                    pages=pages,
                    event_id=f"{event_id_prefix}:{tool_name}:{len(events)}",
                )
                events.append(event)
                if tool_use_id:
                    tool_events_by_id[tool_use_id] = event

            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_use_id = str(block.get("tool_use_id") or "")
                if not tool_use_id or tool_use_id not in tool_events_by_id:
                    continue
                event = tool_events_by_id[tool_use_id]
                event.is_error = bool(block.get("is_error"))
                if event.event_kind != "read" or event.is_error:
                    continue
                text, tokens_est, start_line, num_lines, total_lines = _extract_read_result_payload(
                    block,
                    record.get("toolUseResult"),
                )
                if text:
                    event.read_result_text = text
                    event.read_result_tokens_est = tokens_est
                    event.read_result_start_line = start_line
                    event.read_result_num_lines = num_lines
                    event.read_result_total_lines = total_lines
    return events


def _extract_decision_log_events(path: Path) -> List[ReplayEvent]:
    events: List[ReplayEvent] = []
    for line_no, record in _iter_jsonl_records(path):
        session_id = _normalize_session_id(record.get("session") or record.get("session_id") or path.stem)
        timestamp = _normalize_timestamp(record.get("ts") or record.get("timestamp"))
        raw_file_path = _extract_file_path(record)
        if not raw_file_path:
            continue
        normalized_file_path = str(Path(raw_file_path).expanduser())
        decision = str(record.get("decision") or "").strip().lower()
        reason = str(record.get("reason") or "").strip()
        tokens_est_hint = record.get("file_tokens_est")
        if tokens_est_hint is not None:
            tokens_est_hint = _safe_int(tokens_est_hint, 0)
        events.append(
            ReplayEvent(
                source_path=path,
                source_kind="decision_log",
                session_id=session_id,
                timestamp=timestamp,
                order=len(events),
                event_kind="decision",
                raw_file_path=raw_file_path,
                normalized_file_path=normalized_file_path,
                decision=decision,
                reason=reason,
                tokens_est_hint=tokens_est_hint if isinstance(tokens_est_hint, int) else None,
                replacement_tokens_est=_safe_int(record.get("replacement_tokens_est"), 0),
                net_saved_tokens_est=_safe_int(record.get("net_saved_tokens_est"), 0),
                repeat_replacement_count=_safe_int(record.get("repeat_replacement_count"), 0),
                actual_substitution=bool(record.get("actual_substitution")),
                event_id=f"{path.name}:{line_no}",
            )
        )
    return events


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _looks_generated(path: Path, text: str) -> bool:
    name = path.name.lower()
    if re.search(r"(?:^|[._-])(generated|autogen|autogenerated|gen)(?:[._-]|$)", name):
        return True
    for line in text.splitlines()[:40]:
        stripped = line.strip().lower()
        if not stripped:
            continue
        if stripped.startswith(("#", "//", "/*", "*", '"""', "'''")) and any(
            phrase in stripped for phrase in ("generated by", "auto-generated", "autogenerated", "do not edit")
        ):
            return True
    return False


def _current_file_snapshot(path_text: str, cache: Dict[str, FileSnapshot]) -> FileSnapshot:
    cached = cache.get(path_text)
    path = Path(path_text)
    try:
        stat = path.stat()
    except OSError:
        snapshot = FileSnapshot(path=path, exists=False, provenance="disk")
        cache[path_text] = snapshot
        return snapshot

    stat_key = f"{stat.st_mtime_ns}:{stat.st_size}"
    if cached and cached.exists and cached.stat_key == stat_key:
        return cached

    try:
        text = _read_text(path)
    except OSError as exc:
        snapshot = FileSnapshot(
            path=path,
            exists=True,
            provenance="disk",
            stat_key=stat_key,
            size_bytes=stat.st_size,
            line_count=0,
            mtime_ns=stat.st_mtime_ns,
            sha256="",
            tokens_est=max(1, stat.st_size // 4),
            text="",
            generated_like=False,
            parse_error=str(exc),
        )
        cache[path_text] = snapshot
        return snapshot

    line_count = text.count("\n") + (0 if not text else 1)
    tokens_est = max(1, len(text) // 4)
    snapshot = FileSnapshot(
        path=path,
        exists=True,
        provenance="disk",
        stat_key=stat_key,
        size_bytes=stat.st_size,
        line_count=line_count,
        mtime_ns=stat.st_mtime_ns,
        sha256=hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest(),
        tokens_est=tokens_est,
        text=text,
        generated_like=_looks_generated(path, text),
    )
    cache[path_text] = snapshot
    return snapshot


def _snapshot_from_text(
    path_text: str,
    text: str,
    *,
    start_line: Optional[int] = None,
    num_lines: Optional[int] = None,
    total_lines: Optional[int] = None,
) -> FileSnapshot:
    path = Path(path_text)
    content = text or ""
    derived_line_count = content.count("\n") + (0 if not content else 1)
    line_count = total_lines or derived_line_count
    is_full_coverage = (
        start_line in (None, 0, 1)
        and (
            total_lines is None
            or num_lines is None
            or num_lines >= total_lines
        )
    )
    return FileSnapshot(
        path=path,
        exists=True,
        provenance="historical_full" if is_full_coverage else "historical_partial",
        stat_key=f"historical:{hashlib.sha256(content.encode('utf-8', errors='ignore')).hexdigest()}",
        size_bytes=len(content.encode("utf-8", errors="ignore")),
        line_count=line_count,
        mtime_ns=0,
        sha256=hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest(),
        tokens_est=estimate_tokens(content),
        text=content,
        generated_like=False,
        parse_error="",
    )


def _is_supported_candidate(
    event: ReplayEvent,
    snapshot: FileSnapshot,
    min_file_tokens: int,
    max_bytes: int,
    max_lines: int,
    language_scope: str,
) -> Tuple[bool, str]:
    if event.event_kind != "read":
        return False, "not_read"
    if not event.is_whole_file:
        return False, "partial_read"
    if language_scope == "python-only":
        if not is_python_file(event.normalized_file_path):
            return False, "unsupported_language"
    elif not is_structure_supported_file(event.normalized_file_path):
        return False, "unsupported_language"
    if not snapshot.exists:
        return False, "file_missing"
    if snapshot.generated_like:
        return False, "generated_like"
    if snapshot.size_bytes > max_bytes:
        return False, "over_byte_cap"
    if snapshot.line_count > max_lines:
        return False, "over_line_cap"
    if snapshot.tokens_est < min_file_tokens:
        return False, "below_min_tokens"
    if snapshot.parse_error:
        return False, "file_unreadable"
    return True, "eligible"


def _summarize_snapshot(
    snapshot: FileSnapshot,
    *,
    file_tokens_est: Optional[int] = None,
) -> StructureMapResult:
    return summarize_code_source(
        snapshot.text,
        file_path=str(snapshot.path),
        offset=0,
        limit=0,
        file_tokens_est=file_tokens_est or snapshot.tokens_est,
        file_size_bytes=snapshot.size_bytes,
    )


def _build_replacement_fingerprint(snapshot: FileSnapshot, summary: StructureMapResult, offset: int, limit: int) -> str:
    digest_input = "|".join(
        [
            str(snapshot.path),
            str(snapshot.mtime_ns),
            str(snapshot.size_bytes),
            snapshot.sha256,
            summary.replacement_type,
            summary.fingerprint,
            str(offset),
            str(limit),
        ]
    )
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()


def _simulate_transcript_group(
    source_path: Path,
    events: List[ReplayEvent],
    min_file_tokens: int,
    max_bytes: int,
    max_lines: int,
    caps: Dict[str, int],
    reminder_overhead_tokens: int,
    reason_only_overhead_tokens: int,
    language_scope: str,
) -> ReplaySummary:
    summary = ReplaySummary(source_kind="transcript")
    summary.source_count = len({event.source_path for event in events}) or 1
    summary.total_events = len(events)
    summary.sessions = len({event.session_id for event in events})
    summary.read_events = sum(1 for event in events if event.event_kind == "read")
    summary.edit_events = sum(1 for event in events if event.event_kind == "edit")

    snapshots: Dict[str, FileSnapshot] = {}
    by_session: Dict[str, List[Tuple[int, ReplayEvent]]] = defaultdict(list)
    for index, event in enumerate(events):
        by_session[event.session_id].append((index, event))

    for session_id, indexed_events in by_session.items():
        per_file_state: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {
                "dirty": False,
                "last_signature": None,
                "last_read_index": None,
                "seen_fingerprints": set(),
                "last_success_snapshot": None,
                "history": [],
            }
        )

        for position, (index, event) in enumerate(indexed_events):
            file_key = event.normalized_file_path
            if event.event_kind == "edit":
                per_file_state[file_key]["dirty"] = True
                per_file_state[file_key]["history"].append((index, "edit", event.read_signature))
                continue

            if event.event_kind != "read":
                continue

            disk_snapshot = _current_file_snapshot(file_key, snapshots)
            snapshot = (
                _snapshot_from_text(
                    file_key,
                    event.read_result_text,
                    start_line=event.read_result_start_line,
                    num_lines=event.read_result_num_lines,
                    total_lines=event.read_result_total_lines,
                )
                if event.read_result_text
                else disk_snapshot
            )
            file_stats = summary.files.get(file_key)
            if file_stats is None:
                file_stats = ReplayFileStats(
                    source_path=str(event.source_path),
                    normalized_file_path=file_key,
                    session_id=session_id,
                )
                summary.files[file_key] = file_stats

            file_stats.total_reads += 1
            if event.is_whole_file:
                file_stats.whole_file_reads += 1

            state = per_file_state[file_key]
            evidence_snapshot = state["last_success_snapshot"] or snapshot
            is_redundant = (
                not state["dirty"]
                and state["last_signature"] == event.read_signature
                and state["last_read_index"] is not None
            )
            candidate_snapshot = evidence_snapshot if is_redundant else snapshot
            candidate, reason = _is_supported_candidate(
                event,
                candidate_snapshot,
                min_file_tokens,
                max_bytes,
                max_lines,
                language_scope,
            )
            if not candidate:
                file_stats.skipped[reason] += 1
                continue

            if not is_redundant:
                state["dirty"] = False
                state["last_signature"] = event.read_signature
                state["last_read_index"] = index
                if not event.is_error and snapshot.exists and snapshot.provenance != "historical_partial":
                    state["last_success_snapshot"] = snapshot
                elif state["last_success_snapshot"] is None and snapshot.exists and snapshot.provenance == "disk":
                    state["last_success_snapshot"] = snapshot
                state["history"].append((index, "first", event.read_signature))
                continue

            baseline_tokens = event.read_result_tokens_est or evidence_snapshot.tokens_est
            file_stats.redundant_candidates += 1
            summary.candidate_events += 1

            summary_result = _summarize_snapshot(evidence_snapshot, file_tokens_est=baseline_tokens)
            if not summary_result.eligible or summary_result.replacement_type not in caps:
                file_stats.skipped["structure_digest_fallback"] += 1
                state["history"].append((index, "digest", event.read_signature, summary_result.reason))
                continue

            if evidence_snapshot.provenance == "historical_full":
                file_stats.historical_result_backed += 1
            else:
                file_stats.current_disk_fallbacks += 1

            fingerprint = _build_replacement_fingerprint(
                evidence_snapshot,
                summary_result,
                event.offset,
                event.limit,
            )
            duplicate = fingerprint in state["seen_fingerprints"]
            state["seen_fingerprints"].add(fingerprint)

            replacement_cost = summary_result.replacement_tokens_est
            if duplicate:
                file_stats.repeat_replacements += 1
                replacement_cost = reminder_overhead_tokens if file_stats.repeat_replacements == 1 else reason_only_overhead_tokens
                if file_stats.repeat_replacements == 1:
                    file_stats.reminder_tokens += replacement_cost
                else:
                    file_stats.reason_only_tokens += replacement_cost
            else:
                file_stats.eligible_replacements += 1
                file_stats.structure_tokens += summary_result.replacement_tokens_est
                file_stats.replacement_fingerprints.add(fingerprint)

            if event.is_error:
                file_stats.error_replacement_attempts += 1

            actual_historical_read = (
                not event.is_error
                and bool(event.read_result_text)
                and snapshot.provenance == "historical_full"
            )
            saved_tokens = max(0, baseline_tokens - replacement_cost)
            if actual_historical_read:
                file_stats.verified_replacements += 1
                file_stats.baseline_tokens += baseline_tokens
                file_stats.net_saved_tokens += saved_tokens
            else:
                file_stats.directional_baseline_tokens += baseline_tokens
                file_stats.directional_saved_tokens += saved_tokens

            # Track simple follow-up proxies.
            later_events = indexed_events[position + 1 :]
            for _, later_event in later_events:
                if later_event.normalized_file_path != file_key:
                    continue
                if later_event.event_kind == "edit":
                    break
                if later_event.event_kind == "read":
                    if later_event.is_error:
                        continue
                    file_stats.replacement_followups += 1
                    if later_event.read_signature != event.read_signature:
                        file_stats.replacement_different_range_followups += 1
                    break

            state["dirty"] = False
            state["last_signature"] = event.read_signature
            state["last_read_index"] = index
            if not event.is_error and snapshot.exists and snapshot.provenance != "historical_partial":
                state["last_success_snapshot"] = snapshot
            state["history"].append((index, summary_result.replacement_type, event.read_signature))

    for stats in summary.files.values():
        summary.add_file_stats(stats)
    return summary


def _simulate_decision_log_group(
    source_path: Path,
    events: List[ReplayEvent],
    min_file_tokens: int,
    max_bytes: int,
    max_lines: int,
    caps: Dict[str, int],
    language_scope: str,
) -> ReplaySummary:
    summary = ReplaySummary(source_kind="decision_log")
    summary.source_count = len({event.source_path for event in events}) or 1
    summary.total_events = len(events)
    summary.sessions = len({event.session_id for event in events})
    summary.read_events = len(events)

    snapshots: Dict[str, FileSnapshot] = {}
    by_file: Dict[str, List[ReplayEvent]] = defaultdict(list)
    for event in events:
        by_file[event.normalized_file_path].append(event)

    for file_key, file_events in by_file.items():
        file_stats = ReplayFileStats(
            source_path=str(file_events[0].source_path if file_events else source_path),
            normalized_file_path=file_key,
            session_id=file_events[0].session_id if file_events else "unknown",
        )
        snapshot = _current_file_snapshot(file_key, snapshots)

        for event in file_events:
            if event.decision not in {"warn", "block"} and not re.search(r"redundant_read", event.reason, re.IGNORECASE):
                continue
            if not event.actual_substitution:
                file_stats.skipped["not_actual_substitution"] += 1
                continue
            candidate, reason = _is_supported_candidate(
                ReplayEvent(
                    source_path=source_path,
                    source_kind="decision_log",
                    session_id=event.session_id,
                    timestamp=event.timestamp,
                    order=event.order,
                    event_kind="read",
                    raw_file_path=event.raw_file_path,
                    normalized_file_path=event.normalized_file_path,
                ),
                snapshot,
                min_file_tokens,
                max_bytes,
                max_lines,
                language_scope,
            )
            if not candidate:
                file_stats.skipped[reason] += 1
                continue

            summary_result = _summarize_snapshot(snapshot, file_tokens_est=event.tokens_est_hint or snapshot.tokens_est)
            if not summary_result.eligible or summary_result.replacement_type not in caps:
                file_stats.skipped["structure_digest_fallback"] += 1
                continue
            file_stats.total_reads += 1
            file_stats.whole_file_reads += 1
            file_stats.redundant_candidates += 1
            summary.candidate_events += 1
            file_stats.eligible_replacements += 1
            file_stats.verified_replacements += 1
            baseline_tokens = event.tokens_est_hint or snapshot.tokens_est
            structure_tokens = event.replacement_tokens_est or summary_result.replacement_tokens_est
            net_saved_tokens = event.net_saved_tokens_est or max(0, baseline_tokens - structure_tokens)
            file_stats.baseline_tokens += baseline_tokens
            file_stats.structure_tokens += structure_tokens
            file_stats.net_saved_tokens += net_saved_tokens
            file_stats.current_disk_fallbacks += 1
            if event.repeat_replacement_count > 1:
                file_stats.repeat_replacements += 1
        summary.add_file_stats(file_stats)

    return summary


def _gather_input_paths(raw_paths: Sequence[str]) -> List[Path]:
    expanded: List[Path] = []
    if not raw_paths:
        defaults = [
            Path.home() / ".claude" / "projects",
            Path.home() / ".claude" / "_backups" / "token-optimizer" / "read-cache" / "decisions",
        ]
        for root in defaults:
            if root.exists():
                expanded.append(root)
        return expanded

    for raw in raw_paths:
        if any(ch in raw for ch in "*?[]"):
            for match in sorted(glob.glob(raw)):
                expanded.append(Path(match))
            continue
        path = Path(raw).expanduser()
        expanded.append(path)
    return expanded


def _iter_jsonl_files(paths: Sequence[Path]) -> Iterator[Path]:
    seen = set()
    for path in paths:
        if not path.exists():
            continue
        if path.is_file() and path.suffix.lower() == ".jsonl":
            resolved = str(path.resolve())
            if resolved not in seen:
                seen.add(resolved)
                yield path
        elif path.is_dir():
            for child in sorted(path.rglob("*.jsonl")):
                resolved = str(child.resolve())
                if resolved not in seen:
                    seen.add(resolved)
                    yield child


def _split_sources(paths: Sequence[Path]) -> Tuple[List[Path], List[Path]]:
    transcript_files: List[Path] = []
    decision_files: List[Path] = []
    for path in _iter_jsonl_files(paths):
        kind = _classify_jsonl(path)
        if kind == "decision_log":
            decision_files.append(path)
        else:
            transcript_files.append(path)
    return transcript_files, decision_files


def _load_replay_events(paths: Sequence[Path]) -> Tuple[List[ReplayEvent], List[ReplayEvent]]:
    transcript_events: List[ReplayEvent] = []
    decision_events: List[ReplayEvent] = []
    for path in _iter_jsonl_files(paths):
        kind = _classify_jsonl(path)
        if kind == "decision_log":
            decision_events.extend(_extract_decision_log_events(path))
        else:
            transcript_events.extend(_extract_transcript_events(path))
    return transcript_events, decision_events


def _filter_transcript_events(
    events: Sequence[ReplayEvent],
    *,
    cwd_contains: Sequence[str],
    exclude_cwd_contains: Sequence[str],
) -> List[ReplayEvent]:
    if not cwd_contains and not exclude_cwd_contains:
        return list(events)

    filtered: List[ReplayEvent] = []
    for event in events:
        cwd = event.cwd or ""
        if cwd_contains and not any(fragment in cwd for fragment in cwd_contains):
            continue
        if exclude_cwd_contains and any(fragment in cwd for fragment in exclude_cwd_contains):
            continue
        filtered.append(event)
    return filtered


def _format_tokens(tokens: int) -> str:
    return f"{tokens:,}"


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _print_summary(summary: ReplaySummary, label: str) -> None:
    print(f"\n[{label}]")
    print(f"  source files: {summary.source_count}")
    print(f"  sessions: {summary.sessions}")
    print(f"  events: {summary.total_events} total | {summary.read_events} reads | {summary.edit_events} edits")
    print(f"  redundant candidates: {summary.candidate_events}")
    print(f"  eligible replacements: {summary.eligible_replacements}")
    print(f"  repeat replacements: {summary.repeat_replacements}")
    print(f"  verified replacements: {summary.verified_replacements}")
    print(f"  capture rate: {_percent(summary.capture_rate)}")
    print(f"  repeat share: {_percent(summary.repeat_share)}")
    print(f"  baseline tokens: {_format_tokens(summary.baseline_tokens)}")
    print(f"  structure tokens: {_format_tokens(summary.structure_tokens)}")
    if summary.reminder_tokens or summary.reason_only_tokens:
        print(f"  reminder / reason-only tokens: {_format_tokens(summary.reminder_tokens + summary.reason_only_tokens)}")
    print(f"  net saved tokens (historical-only): {_format_tokens(summary.net_saved_tokens)}")
    print(f"  savings rate (historical-only): {_percent(summary.savings_rate)}")
    if summary.directional_baseline_tokens or summary.directional_saved_tokens:
        print(
            f"  directional opportunity: {_format_tokens(summary.directional_saved_tokens)} "
            f"on {_format_tokens(summary.directional_baseline_tokens)} baseline"
        )
        print(f"  directional savings rate: {_percent(summary.directional_savings_rate)}")
    print(
        f"  evidence: historical results={summary.historical_result_backed} | "
        f"disk fallback={summary.current_disk_fallbacks}"
    )
    print(f"  follow-up same-file rate: {_percent(summary.followup_rate)}")
    print(f"  false-block proxy: {_percent(summary.false_block_proxy_rate)}")

    if summary.skipped:
        print("  skips:")
        for reason, count in summary.skipped.most_common(10):
            print(f"    {reason}: {count}")

    if summary.files:
        top_files = sorted(summary.files.values(), key=lambda s: (s.net_saved_tokens, s.eligible_replacements), reverse=True)[:8]
        print("  top files:")
        for stats in top_files:
            if stats.net_saved_tokens <= 0 and stats.eligible_replacements <= 0:
                continue
            print(
                f"    {stats.normalized_file_path} | saved={_format_tokens(stats.net_saved_tokens)} "
                f"| eligible={stats.eligible_replacements} | repeats={stats.repeat_replacements}"
            )


def _json_ready_counter(counter: Counter) -> Dict[str, int]:
    return {str(key): int(value) for key, value in counter.items()}


def _summary_to_dict(summary: ReplaySummary) -> Dict[str, Any]:
    return {
        "source_kind": summary.source_kind,
        "source_count": summary.source_count,
        "sessions": summary.sessions,
        "total_events": summary.total_events,
        "read_events": summary.read_events,
        "edit_events": summary.edit_events,
        "candidate_events": summary.candidate_events,
        "eligible_replacements": summary.eligible_replacements,
        "repeat_replacements": summary.repeat_replacements,
        "verified_replacements": summary.verified_replacements,
        "capture_rate": summary.capture_rate,
        "repeat_share": summary.repeat_share,
        "baseline_tokens": summary.baseline_tokens,
        "structure_tokens": summary.structure_tokens,
        "reminder_tokens": summary.reminder_tokens,
                "reason_only_tokens": summary.reason_only_tokens,
                "net_saved_tokens": summary.net_saved_tokens,
                "directional_baseline_tokens": summary.directional_baseline_tokens,
                "directional_saved_tokens": summary.directional_saved_tokens,
                "historical_result_backed": summary.historical_result_backed,
                "current_disk_fallbacks": summary.current_disk_fallbacks,
                "savings_rate": summary.savings_rate,
                "directional_savings_rate": summary.directional_savings_rate,
                "followup_rate": summary.followup_rate,
                "false_block_proxy_rate": summary.false_block_proxy_rate,
        "skipped": _json_ready_counter(summary.skipped),
        "files": {
            path: {
                "source_path": stats.source_path,
                "session_id": stats.session_id,
                "total_reads": stats.total_reads,
                "whole_file_reads": stats.whole_file_reads,
                "redundant_candidates": stats.redundant_candidates,
                "eligible_replacements": stats.eligible_replacements,
                "repeat_replacements": stats.repeat_replacements,
                "verified_replacements": stats.verified_replacements,
                "baseline_tokens": stats.baseline_tokens,
                "structure_tokens": stats.structure_tokens,
                "reminder_tokens": stats.reminder_tokens,
                "reason_only_tokens": stats.reason_only_tokens,
                "net_saved_tokens": stats.net_saved_tokens,
                "directional_baseline_tokens": stats.directional_baseline_tokens,
                "directional_saved_tokens": stats.directional_saved_tokens,
                "historical_result_backed": stats.historical_result_backed,
                "current_disk_fallbacks": stats.current_disk_fallbacks,
                "skipped": _json_ready_counter(stats.skipped),
                "replacement_followups": stats.replacement_followups,
                "replacement_different_range_followups": stats.replacement_different_range_followups,
                "error_replacement_attempts": stats.error_replacement_attempts,
            }
            for path, stats in summary.files.items()
        },
    }


def _create_transcript_fixture(tmpdir: Path) -> Path:
    py_file = tmpdir / "fixture.py"
    filler = "\n".join(f"# filler line {i} with extra payload to clear token threshold" for i in range(360))
    py_file.write_text(
        f"import os\n\n"
        f"class Greeter:\n"
        f"    def hello(self, name):\n"
        f"        return f'hello {{name}}'\n\n"
        f"def top_level(x, y):\n"
        f"    return x + y\n\n"
        f"{filler}\n",
        encoding="utf-8",
    )
    transcript = tmpdir / "fixture.jsonl"
    session_id = "fixture-session"
    records = [
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": "2026-04-01T10:00:00Z",
            "uuid": "1",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Read", "id": "toolu_py_1", "input": {"file_path": str(py_file)}}
                ]
            },
        },
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": "2026-04-01T10:00:00Z",
            "uuid": "1b",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_py_1", "content": py_file.read_text(encoding="utf-8")}],
            },
            "toolUseResult": {
                "type": "text",
                "file": {
                    "filePath": str(py_file),
                    "content": py_file.read_text(encoding="utf-8"),
                    "numLines": len(py_file.read_text(encoding="utf-8").splitlines()),
                    "startLine": 1,
                    "totalLines": len(py_file.read_text(encoding="utf-8").splitlines()),
                }
            },
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": "2026-04-01T10:00:01Z",
            "uuid": "2",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Read", "id": "toolu_py_2", "input": {"file_path": str(py_file)}}
                ]
            },
        },
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": "2026-04-01T10:00:01Z",
            "uuid": "2b",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_py_2", "content": py_file.read_text(encoding="utf-8")}],
            },
            "toolUseResult": {
                "type": "text",
                "file": {
                    "filePath": str(py_file),
                    "content": py_file.read_text(encoding="utf-8"),
                    "numLines": len(py_file.read_text(encoding="utf-8").splitlines()),
                    "startLine": 1,
                    "totalLines": len(py_file.read_text(encoding="utf-8").splitlines()),
                }
            },
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": "2026-04-01T10:00:02Z",
            "uuid": "3",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": str(py_file)}}
                ]
            },
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": "2026-04-01T10:00:03Z",
            "uuid": "4",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": str(py_file)}}
                ]
            },
        },
    ]
    with transcript.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return transcript


def _create_ts_transcript_fixture(tmpdir: Path) -> Path:
    ts_file = tmpdir / "fixture.tsx"
    filler = "\n".join(f"// filler line {i} to keep this file safely above the token threshold" for i in range(260))
    ts_file.write_text(
        "import React from 'react';\n"
        "import { z } from 'zod';\n\n"
        "export interface WidgetProps {\n"
        "  title: string;\n"
        "  count?: number;\n"
        "}\n\n"
        "export type WidgetMode = 'compact' | 'full';\n\n"
        "export class WidgetService {\n"
        "  constructor(private readonly mode: WidgetMode) {}\n"
        "  getTitle(input: WidgetProps) {\n"
        "    return `${input.title}:${this.mode}`;\n"
        "  }\n"
        "}\n\n"
        "export const Widget = ({ title, count = 0 }: WidgetProps) => {\n"
        "  return <div>{title}:{count}</div>;\n"
        "};\n\n"
        "export default Widget;\n\n"
        f"{filler}\n",
        encoding="utf-8",
    )
    transcript = tmpdir / "fixture-ts.jsonl"
    session_id = "fixture-ts-session"
    records = [
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": "2026-04-01T11:00:00Z",
            "uuid": "ts-1",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Read", "id": "toolu_ts_1", "input": {"file_path": str(ts_file)}}
                ],
            },
        },
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": "2026-04-01T11:00:01Z",
            "uuid": "ts-2",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_ts_1", "content": ts_file.read_text(encoding="utf-8")}],
            },
            "toolUseResult": {
                "type": "text",
                "file": {
                    "filePath": str(ts_file),
                    "content": ts_file.read_text(encoding="utf-8"),
                    "numLines": len(ts_file.read_text(encoding="utf-8").splitlines()),
                    "startLine": 1,
                    "totalLines": len(ts_file.read_text(encoding="utf-8").splitlines()),
                }
            },
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": "2026-04-01T11:00:02Z",
            "uuid": "ts-3",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Read", "id": "toolu_ts_2", "input": {"file_path": str(ts_file)}}
                ],
            },
        },
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": "2026-04-01T11:00:03Z",
            "uuid": "ts-4",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_ts_2", "content": ts_file.read_text(encoding="utf-8")}],
            },
            "toolUseResult": {
                "type": "text",
                "file": {
                    "filePath": str(ts_file),
                    "content": ts_file.read_text(encoding="utf-8"),
                    "numLines": len(ts_file.read_text(encoding="utf-8").splitlines()),
                    "startLine": 1,
                    "totalLines": len(ts_file.read_text(encoding="utf-8").splitlines()),
                }
            },
        },
    ]
    with transcript.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return transcript


def _run_torture() -> int:
    failures: List[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        transcript = _create_transcript_fixture(tmpdir)
        events = _extract_transcript_events(transcript)
        summary = _simulate_transcript_group(
            transcript,
            events,
            1,
            DEFAULT_MAX_BYTES,
            DEFAULT_MAX_LINES,
            DEFAULT_STRUCTURE_CAPS,
            DEFAULT_REMINDER_OVERHEAD_TOKENS,
            DEFAULT_REASON_ONLY_OVERHEAD_TOKENS,
            "all-supported",
        )
        if summary.candidate_events != 1:
            failures.append(f"expected 1 redundant candidate, got {summary.candidate_events}")
        if summary.eligible_replacements != 1:
            failures.append(f"expected 1 eligible replacement, got {summary.eligible_replacements}")
        if summary.repeat_replacements != 0:
            failures.append(f"expected 0 repeat replacements, got {summary.repeat_replacements}")
        if summary.net_saved_tokens <= 0:
            failures.append("expected positive net savings on synthetic fixture")

        ts_transcript = _create_ts_transcript_fixture(tmpdir)
        ts_events = _extract_transcript_events(ts_transcript)
        ts_summary = _simulate_transcript_group(
            ts_transcript,
            ts_events,
            DEFAULT_MIN_FILE_TOKENS,
            DEFAULT_MAX_BYTES,
            DEFAULT_MAX_LINES,
            DEFAULT_STRUCTURE_CAPS,
            DEFAULT_REMINDER_OVERHEAD_TOKENS,
            DEFAULT_REASON_ONLY_OVERHEAD_TOKENS,
            "all-supported",
        )
        if ts_summary.candidate_events != 1:
            failures.append(f"expected 1 TS redundant candidate, got {ts_summary.candidate_events}")
        if ts_summary.eligible_replacements != 1:
            failures.append(f"expected 1 TS eligible replacement, got {ts_summary.eligible_replacements}")
        if ts_summary.net_saved_tokens <= 0:
            failures.append("expected positive TS net savings on synthetic fixture")

        generated = tmpdir / "generated.py"
        generated.write_text("# generated by foo\nx = 1\n", encoding="utf-8")
        snap = _current_file_snapshot(str(generated), {})
        ok, reason = _is_supported_candidate(
            ReplayEvent(
                source_path=transcript,
                source_kind="transcript",
                session_id="fixture-session",
                timestamp="2026-04-01T10:00:00Z",
                order=0,
                event_kind="read",
                raw_file_path=str(generated),
                normalized_file_path=str(generated),
            ),
            snap,
            DEFAULT_MIN_FILE_TOKENS,
            DEFAULT_MAX_BYTES,
            DEFAULT_MAX_LINES,
            "all-supported",
        )
        if ok:
            failures.append("generated-looking file should not be eligible")

    if failures:
        print("[torture] FAIL")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("[torture] PASS")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay local session artifacts for structure-aware compression proof.")
    parser.add_argument("paths", nargs="*", help="JSONL transcript files, decision logs, directories, or globs.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text.")
    parser.add_argument("--torture", action="store_true", help="Run the built-in offline torture fixture and exit.")
    parser.add_argument("--min-file-tokens", type=int, default=DEFAULT_MIN_FILE_TOKENS, help="Minimum estimated file tokens before a code file is eligible.")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="Maximum file size in bytes for structure summarization.")
    parser.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES, help="Maximum line count for structure summarization.")
    parser.add_argument("--reminder-overhead-tokens", type=int, default=DEFAULT_REMINDER_OVERHEAD_TOKENS, help="Estimated tokens for the second identical reread reminder.")
    parser.add_argument("--reason-only-overhead-tokens", type=int, default=DEFAULT_REASON_ONLY_OVERHEAD_TOKENS, help="Estimated tokens for third+ identical reread reasons.")
    parser.add_argument("--signatures-cap", type=int, default=DEFAULT_STRUCTURE_CAPS["signatures"], help="Maximum characters for signatures view.")
    parser.add_argument("--top-level-cap", type=int, default=DEFAULT_STRUCTURE_CAPS["top_level"], help="Maximum characters for top-level view.")
    parser.add_argument("--skeleton-cap", type=int, default=DEFAULT_STRUCTURE_CAPS["skeleton"], help="Maximum characters for skeleton view.")
    parser.add_argument("--language-scope", choices=["all-supported", "python-only"], default="all-supported", help="Replay either all supported code languages or the original Python-only slice.")
    parser.add_argument("--cwd-contains", action="append", default=[], help="Only include transcript events whose cwd contains this substring. Repeatable.")
    parser.add_argument("--exclude-cwd-contains", action="append", default=[], help="Exclude transcript events whose cwd contains this substring. Repeatable.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.torture:
        return _run_torture()

    raw_paths = list(args.paths)
    path_objs = _gather_input_paths(raw_paths)
    if not path_objs:
        print("No input paths found.", file=sys.stderr)
        return 1

    transcript_events, decision_events = _load_replay_events(path_objs)
    transcript_events = _filter_transcript_events(
        transcript_events,
        cwd_contains=args.cwd_contains,
        exclude_cwd_contains=args.exclude_cwd_contains,
    )
    caps = {
        "signatures": max(1, args.signatures_cap),
        "top_level": max(1, args.top_level_cap),
        "skeleton": max(1, args.skeleton_cap),
    }

    transcript_summary = None
    if transcript_events:
        transcript_summary = _simulate_transcript_group(
            source_path=path_objs[0],
            events=transcript_events,
            min_file_tokens=max(1, args.min_file_tokens),
            max_bytes=max(1, args.max_bytes),
            max_lines=max(1, args.max_lines),
            caps=caps,
            reminder_overhead_tokens=max(0, args.reminder_overhead_tokens),
            reason_only_overhead_tokens=max(0, args.reason_only_overhead_tokens),
            language_scope=args.language_scope,
        )
        transcript_summary.source_count = len({event.source_path for event in transcript_events}) or 1

    decision_summary = None
    if decision_events:
        decision_summary = _simulate_decision_log_group(
            source_path=path_objs[0],
            events=decision_events,
            min_file_tokens=max(1, args.min_file_tokens),
            max_bytes=max(1, args.max_bytes),
            max_lines=max(1, args.max_lines),
            caps=caps,
            language_scope=args.language_scope,
        )
        decision_summary.source_count = len({event.source_path for event in decision_events}) or 1

    if args.json:
        payload = {
            "inputs": [str(path) for path in path_objs],
            "filters": {
                "language_scope": args.language_scope,
                "cwd_contains": list(args.cwd_contains),
                "exclude_cwd_contains": list(args.exclude_cwd_contains),
            },
            "transcript": _summary_to_dict(transcript_summary) if transcript_summary else None,
            "decision_log": _summary_to_dict(decision_summary) if decision_summary else None,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print("Structure-aware replay proof")
    print("============================")
    print(f"inputs: {len(path_objs)}")
    print(f"language scope: {args.language_scope}")
    if args.cwd_contains or args.exclude_cwd_contains:
        print(f"cwd filters: include={list(args.cwd_contains)} exclude={list(args.exclude_cwd_contains)}")
    print(f"transcript events: {len(transcript_events)}")
    print(f"decision-log events: {len(decision_events)}")
    if transcript_summary:
        _print_summary(transcript_summary, "transcript replay")
    else:
        print("\n[transcript replay]")
        print("  no transcript data found")
    if decision_summary:
        _print_summary(decision_summary, "decision-log replay")
    elif decision_events:
        print("\n[decision-log replay]")
        print("  no usable decision entries found")
    print("\nNotes:")
    print("  - Transcript and decision-log totals are reported separately to avoid double counting.")
    print("  - Transcript replay prefers historical Read tool results when available and falls back to current local file state otherwise.")
    print("  - Quality impact is proxy-only here; this tool is a savings proof runner, not a runtime hook.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
