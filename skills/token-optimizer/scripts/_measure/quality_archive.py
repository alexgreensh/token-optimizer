"""Definition-only implementations; mutable state belongs to measure."""
from __future__ import annotations
from . import _compat


def _int_env(key: str, default: int) -> int:
    _measure_facade = _compat.facade()
    raw = _measure_facade.os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[Token Optimizer] Warning: invalid {key}={raw!r}, using default {default}", file=_measure_facade.sys.stderr)
        return default


def _float_env(key: str, default: float) -> float:
    _measure_facade = _compat.facade()
    raw = _measure_facade.os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[Token Optimizer] Warning: invalid {key}={raw!r}, using default {default}", file=_measure_facade.sys.stderr)
        return default


def _scaled_tool_call_thresholds():
    """Compute tool call thresholds scaled by context window size.

    Uses superlinear scaling (x^1.3) because on 1M windows, tool results
    are a much smaller fraction of the context. Linear 5x was too
    conservative: it warned at 125 calls on 1M even at 15% fill.
    """
    _measure_facade = _compat.facade()
    try:
        ctx_size, _ = _measure_facade.detect_context_window()
    except Exception:
        ctx_size = 200_000
    scale = max(1.0, (ctx_size / 200_000) ** 1.3)
    base_warn = 25
    base_crit = 40
    warn = _measure_facade._int_env("TOKEN_OPTIMIZER_TOOL_CALL_WARN", int(base_warn * scale))
    crit = _measure_facade._int_env("TOKEN_OPTIMIZER_TOOL_CALL_CRITICAL", int(base_crit * scale))
    return warn, crit


def _tool_call_warn_thresholds():
    """[(threshold, level, message)] for the current context window, highest first.

    The list keeps its name and literal messages (assigned, not built) because
    tests/test_verbosity_diet_guard.py scrapes ``_TOOL_CALL_WARN_THRESHOLDS``
    by AST to budget the injected message length.
    """
    _measure_facade = _compat.facade()
    warn, crit = _measure_facade._scaled_tool_call_thresholds()
    _TOOL_CALL_WARN_THRESHOLDS = [
        (crit, "CRITICAL", f"{crit}+ tool calls, adherence degraded"),
        (warn, "WARNING",  f"{warn}+ tool calls, consider fresh session"),
    ]
    return _TOOL_CALL_WARN_THRESHOLDS


def _external_memory_present() -> bool:
    """Best-effort auto-detection of a co-installed durable-memory tool.

    Signalling (first decisive signal wins):
      1. Env override TO_EXTERNAL_MEMORY -- explicit 0/false/no/off FORCES
         absent, explicit 1/true/yes/on FORCES present (case-insensitive),
         without touching the filesystem.
      2. A neutral coordination marker file, whose mere existence means an
         external durable-memory tool is active:
         ``${TO_EXTERNAL_MEMORY_MARKER:-<claude home>/durable-memory.json}``, where the
         Claude home honors CLAUDE_CONFIG_DIR (CLAUDE_DIR / claude_home()).

    The external tool writes the marker once (set-and-forget), so detection is
    automatic with no per-session configuration. Cached in a module global so
    repeated calls within one process cost a single check. Never raises -- any
    exception during detection is treated as "absent".
    """
    _measure_facade = _compat.facade()
    pass
    if _measure_facade._EXTERNAL_MEMORY_CACHE is not None:
        return _measure_facade._EXTERNAL_MEMORY_CACHE
    result = False
    try:
        raw = _measure_facade.os.environ.get("TO_EXTERNAL_MEMORY", "").strip().lower()
        if raw in ("0", "false", "no", "off"):
            _measure_facade._EXTERNAL_MEMORY_CACHE = False
            return False
        if raw in ("1", "true", "yes", "on"):
            _measure_facade._EXTERNAL_MEMORY_CACHE = True
            return True

        marker_env = _measure_facade.os.environ.get("TO_EXTERNAL_MEMORY_MARKER", "").strip()
        marker = (
            _measure_facade.Path(marker_env).expanduser()
            if marker_env
            else _measure_facade.CLAUDE_DIR / "durable-memory.json"
        )
        try:
            if marker.exists():
                result = True
        except Exception:
            result = False
    except Exception:
        result = False
    _measure_facade._EXTERNAL_MEMORY_CACHE = result
    return result


def sanitize_session_id(sid):
    """Sanitize session ID for safe use in filenames. Prevents path traversal."""
    _measure_facade = _compat.facade()
    if not sid:
        return "unknown"
    # Coerce non-string JSON values (int, list, dict) to str before regex.
    # Hook stdin JSON is attacker-influenceable: {"session_id": 123} or
    # {"session_id": [1,2]} would otherwise raise TypeError in re.sub().
    if not isinstance(sid, str):
        sid = str(sid)
    sanitized = _measure_facade.re.sub(r"[^a-zA-Z0-9_-]", "", sid)
    if _measure_facade.detect_runtime() == 'codex':
        sanitized = _measure_facade.codex_session._safe_session_id(sanitized)
    return sanitized if len(sanitized) >= 6 else "unknown"


def _extract_user_text(record):
    """Extract text from a user message record. Handles str and list content."""
    _measure_facade = _compat.facade()
    msg = record.get("message", {})
    if isinstance(msg, dict):
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        elif isinstance(content, list):
            return " ".join(
                b.get("text", "") if isinstance(b, dict) else str(b)
                for b in content
            )
    elif isinstance(msg, str):
        return msg
    return ""


def _command_task_text(text):
    """Unwrap a complete command once, with bounded, non-backtracking scans."""
    _measure_facade = _compat.facade()
    fields = {}
    pos = 0
    while pos < len(text):
        if not text[pos:].strip():
            break
        tag = _measure_facade._COMMAND_TASK_TAG_RE.match(text, pos)
        if tag is None or tag.group(1) in fields:
            return ""
        name = tag.group(1)
        close = "</command-" + name + ">"
        end = text.find(close, tag.end())
        if end < 0:
            return ""
        fields[name] = text[tag.end():end]
        pos = end + len(close)
    command = _measure_facade._SLASH_COMMAND_RE.fullmatch(
        "<command-name>" + fields.get("name", "") + "</command-name>")
    if command is None:
        return ""
    name = command.group(1)
    if name.lower() in ("compact", "clear"):
        return ""
    # Some versions omit an empty <command-args>: treat a missing tag as empty.
    args = fields.get("args", "").strip()
    if (not args and name.lower() in _measure_facade._LOCAL_SLASH_COMMANDS
            and not _measure_facade._resolve_installed_skill(name)):
        return ""
    return "/" + name + (" " + args if args else "")


def _task_user_text(record):
    """Task text for continuity only, excluding harness/meta/tool transport.

    A slash-command wrapper is transport, but its name and arguments can be
    a real task. Preserve those without changing raw scoring/skill-use input.
    """
    _measure_facade = _compat.facade()
    if record.get("isMeta") or record.get("isCompactSummary"):
        return ""
    message = record.get("message", {})
    content = message.get("content") if isinstance(message, dict) else message
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [
            block if isinstance(block, str) else block.get("text", "")
            for block in content
            if isinstance(block, str) or (
                isinstance(block, dict) and block.get("type") == "text")
        ]
    else:
        return ""
    texts = []
    for part in parts:
        if not isinstance(part, str):
            continue
        text = part.strip()
        # A <system-reminder> block can lead a real prompt: drop the block,
        # keep what follows. An unclosed block is all harness.
        while text.startswith("<system-reminder>"):
            end = text.find("</system-reminder>")
            text = "" if end < 0 else text[end + len("</system-reminder>"):].strip()
        if not text or text.startswith(_measure_facade._HARNESS_TASK_PREFIXES):
            continue
        if text.startswith(("<command-name>", "<command-message>", "<command-args>")):
            # Only a complete wrapper is an invocation. Do not turn malformed
            # harness markup into a recovered task, or strip tags from prose.
            text = _measure_facade._command_task_text(text)
            if not text:
                continue
        texts.append(text)
    return "\n".join(texts)


def _read_stdin_hook_input(max_bytes=None):
    """Thin wrapper: measure.py callers default to 64KB (PreToolUse payloads)."""
    _measure_facade = _compat.facade()
    return _measure_facade._read_stdin_hook_input_shared(max_bytes)


def _read_windows_stdin_bytes(max_bytes):
    """Return bytes already buffered in a Windows stdin pipe, never waiting."""
    _measure_facade = _compat.facade()
    try:
        import ctypes
        import msvcrt

        stream = getattr(_measure_facade.sys.stdin, "buffer", _measure_facade.sys.stdin)
        fd = stream.fileno()
        available = ctypes.c_ulong()
        handle = msvcrt.get_osfhandle(fd)
        ok = ctypes.windll.kernel32.PeekNamedPipe(
            handle, None, 0, None, ctypes.byref(available), None
        )
        if not ok or not available.value:
            return b""
        return _measure_facade.os.read(fd, min(max_bytes, available.value))
    except (AttributeError, ImportError, OSError, ValueError):
        return b""


def _read_throttle_only_hook_input():
    """Read throttle metadata without risking a Windows open-pipe wait."""
    _measure_facade = _compat.facade()
    if _measure_facade.os.name == "nt":
        try:
            raw = _measure_facade._read_windows_stdin_bytes(1_000_000)
            return _measure_facade.json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, _measure_facade.json.JSONDecodeError, OSError):
            return {}
    return _measure_facade._read_stdin_hook_input(max_bytes=1_000_000)


def _quality_cache_throttle_only_state(throttle, force):
    """Return hook metadata and whether the throttle-only CLI tick is due."""
    _measure_facade = _compat.facade()
    payload = _measure_facade._read_throttle_only_hook_input()
    if force:
        return payload, True
    return payload, _measure_facade._quality_cache_tick_due(
        throttle,
        payload.get("transcript_path"),
        payload.get("session_id"),
    )


def _hook_payload_text(node, _depth=None):
    """Recursively pull display text out of a hook attachment payload."""
    _measure_facade = _compat.facade()
    if _depth > 6:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(_measure_facade._hook_payload_text(v, _depth + 1) for v in node)
    if isinstance(node, dict):
        return "\n".join(
            _measure_facade._hook_payload_text(node[k], _depth + 1) for k in _measure_facade._HOOK_TEXT_KEYS if k in node)
    return ""


def _recovery_window_metrics(calls, notices, seg, lo, hi):
    """Aggregate one matched window of completed calls plus in-range loop
    notices. Same metric names as the retrospective audit so live evidence
    and the weekly event study compare like-for-like."""
    _measure_facade = _compat.facade()
    n = len(calls)
    loops = sum(
        1 for nt in notices
        if nt["kind"] == "loop" and nt["seg"] == seg and lo <= nt["line"] <= hi
    )
    return {
        "calls": n,
        "errors": sum(c["error"] for c in calls),
        "test_errors_with_edits": sum(bool(c["error"] and c.get("test_error_edited")) for c in calls),
        "shell_nonzero": sum(c["shell_nonzero"] for c in calls),
        "repeats": sum(c["repeat"] for c in calls),
        "repeat_after_error": sum(c["repeat_after_error"] for c in calls),
        "reads": sum(c["read"] for c in calls),
        "rereads": sum(c["reread"] for c in calls),
        "rereads_unchanged": sum(c["reread_unchanged"] for c in calls),
        "expansions": sum(c["expand"] for c in calls),
        "loop_notices": loops,
        "first_line": calls[0]["line"] if calls else lo,
        "last_line": calls[-1]["line"] if calls else hi,
    }


def _finalize_compaction_boundary(boundary, notices):
    """Close out one boundary's matched pre/post windows at the next
    boundary (or EOF). m = min(20, pre calls, post calls); eligible at >=10."""
    _measure_facade = _compat.facade()
    m = min(_measure_facade._RECOVERY_WINDOW_CALLS, boundary["pre_seg_calls"], boundary["post_seg_calls"])
    boundary["matched_calls"] = m
    boundary["eligible"] = m >= _measure_facade._RECOVERY_MIN_MATCHED_CALLS
    boundary["pre"] = None
    boundary["post"] = None
    boundary["end_epoch"] = None
    if m:
        pc = boundary["pre_tail"][-m:]
        qc = boundary["post_calls"][:m]
        boundary["pre"] = _measure_facade._recovery_window_metrics(
            pc, notices, boundary["pre_seg"], pc[0]["line"], boundary["line"] - 1)
        post_hi = max(c["result_line"] or c["line"] for c in qc)
        boundary["post"] = _measure_facade._recovery_window_metrics(
            qc, notices, boundary["ordinal"], boundary["line"] + 1, post_hi)
        end_epochs = [
            _measure_facade._keepwarm_iso_to_epoch(c.get("result_ts")) for c in qc
        ]
        end_epochs = [e for e in end_epochs if e is not None]
        boundary["end_epoch"] = max(end_epochs) if end_epochs else None
    boundary.pop("pre_tail", None)
    boundary.pop("post_calls", None)
    boundary["ts_epoch"] = _measure_facade._keepwarm_iso_to_epoch(boundary.get("ts"))


def _recovery_boundary_eval(boundary):
    """Provisionally score one eligible boundary. Shadow-only thresholds
    await calibration on held-out labeled
    failures (COMPACTION-QUALITY.md section 6): they flag only repeated
    failed actions, repeated retrieval of unchanged artifacts, clear error
    spikes, observed loops, and capture-without-delivery -- never a bare
    count, a legitimate re-read, or an unobserved restore."""
    _measure_facade = _compat.facade()
    if (not boundary.get("eligible")
            or not isinstance(boundary.get("pre"), dict)
            or not isinstance(boundary.get("post"), dict)):
        return "unmeasurable", None, []
    pre, post = boundary["pre"], boundary["post"]
    flags = []
    if post["repeat_after_error"] >= 2:
        flags.append("repeated_failed_action")
    if (post["rereads_unchanged"] >= 3
            and post["rereads_unchanged"] > pre["rereads_unchanged"]):
        flags.append("unchanged_artifact_rereads")
    if post["calls"] and pre["calls"]:
        pre_errors = pre["errors"] - pre.get("test_errors_with_edits", 0)
        post_errors = post["errors"] - post.get("test_errors_with_edits", 0)
        err_delta = post_errors / post["calls"] - pre_errors / pre["calls"]
        if post_errors >= 3 and err_delta >= 0.10:
            flags.append("error_rate_increase")
    loops = post["loop_notices"] + int(boundary.get("db_loops") or 0)
    if loops >= 1:
        flags.append("loop_events")
    restore_gap = boundary.get("restore_evidence") == "none" and bool(
        boundary.get("capture_notice"))
    if restore_gap:
        flags.append("restore_not_delivered")
    status = "needs_attention" if flags else "no_concerning_signal_observed"
    score = 100
    score -= min(36, post["repeat_after_error"] * 12)
    score -= min(20, max(0, post["rereads_unchanged"] - pre["rereads_unchanged"]) * 4)
    score -= min(24, max(0, post["errors"] - pre["errors"]) * 8)
    score -= min(30, loops * 15)
    if restore_gap:
        score -= 15
    return status, max(0, int(round(score))), flags


def _rec_track_tool_use(rec, block, idx, ts, cwd):
    """Record a tool_use for the recovery tracker (issue-time signals:
    repeat / re-read / unchanged-re-read / archive expansion)."""
    _measure_facade = _compat.facade()
    tid = block.get("id")
    if not tid or len(rec["pending"]) >= _measure_facade._RECOVERY_PENDING_MAX:
        return
    name = block.get("name") or ""
    inp = block.get("input")
    inp = inp if isinstance(inp, dict) else {}
    sig = _measure_facade.hashlib.sha256(
        (name + _measure_facade.json.dumps(inp, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))).encode()
    ).hexdigest()
    prev = rec["latest_sig"].get(sig)
    repeat = sig in rec["recent_sigs"]
    fp = ""
    if name in ("Read", "Edit", "Write", "MultiEdit"):
        fp = inp.get("file_path", inp.get("path", "")) or ""
    if fp:
        fp = _measure_facade.os.path.normpath(fp if _measure_facade.os.path.isabs(fp) else _measure_facade.os.path.join(cwd or "", fp))
    isread = name == "Read" and bool(fp)
    reread = isread and fp in rec["read_versions"]
    unchanged = reread and rec["read_versions"][fp] == rec["versions"][fp]
    if isread:
        rec["read_versions"][fp] = rec["versions"][fp]
    if name in ("Edit", "Write", "MultiEdit") and fp:
        rec["versions"][fp] += 1
        # Failed tests followed by an edit are ordinary red/green work.
        for previous in rec["completed"]:
            if previous.get("test_runner") and previous["error"]:
                previous["test_error_edited"] = True
    edit_version = sum(rec["versions"].values())
    cmd = inp.get("command", "") if name in ("Bash", "bash", "exec_command") else ""
    call = {
        "id": tid, "line": idx, "seg": rec["seg"], "ts": ts, "name": name,
        "sig": sig,
        "edit_version": edit_version,
        "test_runner": bool(_measure_facade.re.search(
            r"(?:\bpytest\b|\bunittest\b|\b(?:npm|pnpm|yarn|bun|cargo|go)\s+(?:run\s+)?test\b|\bnode\s+[^\n]*--test\b)", cmd)),
        "read": int(isread), "reread": int(reread),
        "reread_unchanged": int(unchanged),
        "repeat": int(repeat),
        "repeat_after_error": int(bool(repeat and prev and prev.get("error")
                                       and prev.get("edit_version") == edit_version)),
        "expand": int(bool(_measure_facade._REC_EXPAND_RE.search(cmd))),
        "result_line": None, "result_ts": None,
        "error": 0, "shell_nonzero": 0,
    }
    rec["pending"][tid] = call
    rec["seg_issued"] += 1
    rec["recent_sigs"].append(sig)
    rec["latest_sig"][sig] = call


def _rec_track_tool_result(rec, block, idx, ts):
    """Match a tool_result to its pending call. Calls crossing a boundary
    are excluded from BOTH windows (they belong to neither side)."""
    _measure_facade = _compat.facade()
    tid = block.get("tool_use_id")
    call = rec["pending"].pop(tid, None) if tid else None
    if call is None or call["seg"] != rec["seg"]:
        return
    text = _measure_facade._extract_tool_result_text(block)
    call["result_line"] = idx
    call["result_ts"] = ts
    call["error"] = int(bool(block.get("is_error")))
    call["shell_nonzero"] = int(bool(_measure_facade._REC_EXIT_CODE_RE.search(text[:4000])))
    rec["completed"].append(call)
    rec["seg_completed"] += 1
    open_b = rec["open"]
    if open_b is not None and call["seg"] == open_b["ordinal"]:
        open_b["post_seg_calls"] += 1
        if len(open_b["post_calls"]) < 25:
            open_b["post_calls"].append(call)


def _parse_jsonl_for_quality(filepath):
    """Parse a JSONL session file and extract quality-relevant data.

    Returns a dict with chronological lists of reads, writes, tool results,
    system reminders, messages, and compaction markers. Returns None if
    the file is empty or unparseable.
    """
    _measure_facade = _compat.facade()
    if _measure_facade._use_codex_session_adapter(filepath):
        return _measure_facade.codex_session.parse_jsonl_for_quality(filepath)

    reads = []       # (index, path, timestamp)
    writes = []      # (index, path, timestamp)
    tool_results = []  # (index, tool_name, result_size_chars, referenced_later)
    tool_result_meta = []  # richer metadata for live detectors
    tool_name_by_id = {}
    system_reminders = []  # (index, content_hash, size_chars)
    messages = []    # (index, role, text_length, is_substantive)
    compactions = 0
    tool_calls = 0   # cumulative tool call count (not reset on compact)
    agent_dispatches = []  # (index, prompt_size, result_size)
    decisions = []   # (index, text_snippet)
    # Latest assistant usage = the live context footprint. input + both cache
    # buckets is the same sum ccusage and the VS Code companion use. The last
    # assistant turn's usage already reflects any compaction, so we keep the most
    # recent and divide by the real window for an accurate fill (the char-length
    # estimate below misses cached context — the bulk of real token usage).
    context_tokens = None
    current_model = None
    safe_pause = False

    # Recovery tracker state (see helpers above). NEVER reset at boundaries.
    _rec = {
        "pending": {},
        "completed": _measure_facade.deque(maxlen=25),
        "seg": 0,
        "seg_completed": 0,
        "seg_issued": 0,
        "recent_sigs": _measure_facade.deque(maxlen=50),
        "latest_sig": {},
        "read_versions": {},
        "versions": _measure_facade.Counter(),
        "boundaries": [],
        "open": None,
        "notices": [],
        "hookseen": set(),
    }
    _rec_first_ts = None
    _rec_last_ts = None
    _rec_cwd = ""

    idx = 0
    try:
        if _measure_facade.os.stat(filepath).st_size > _measure_facade.codex_session.MAX_PARSE_FILE_BYTES:
            return None
    except OSError:
        return None
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if len(line) > _measure_facade.codex_session.MAX_JSONL_LINE_CHARS:
                    continue
                try:
                    record = _measure_facade.json.loads(line)
                except _measure_facade.json.JSONDecodeError:
                    continue

                if not isinstance(record, dict):
                    continue
                rec_type = record.get("type")
                ts = record.get("timestamp", "")
                if rec_type == "tool_result" and not record.get("isSidechain"):
                    safe_pause = False
                if ts:
                    if _rec_first_ts is None or ts < _rec_first_ts:
                        _rec_first_ts = ts
                    if _rec_last_ts is None or ts > _rec_last_ts:
                        _rec_last_ts = ts
                _is_sidechain = bool(record.get("isSidechain"))
                if not _is_sidechain and not _rec_cwd:
                    _cwd_val = record.get("cwd")
                    if isinstance(_cwd_val, str) and _cwd_val:
                        _rec_cwd = _cwd_val

                # Detect context-clearing boundaries:
                # 1. compact_boundary (from /compact or autocompact)
                # 2. ExitPlanMode (plan mode clears context but leaves no boundary marker)
                # On boundary: reset all signal accumulators so quality score
                # reflects the CURRENT context window, not full session history
                is_compact = rec_type == "system" and (
                    record.get("subtype") == "compact_boundary"
                    or "compactMetadata" in record
                )
                is_plan_exit = False
                if rec_type == "assistant":
                    for block in record.get("message", {}).get("content", []):
                        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "ExitPlanMode":
                            is_plan_exit = True
                            break
                if is_compact or is_plan_exit:
                    if not _is_sidechain:
                        safe_pause = False
                    if is_compact:
                        compactions += 1
                        if not _is_sidechain:
                            # Close the previous boundary's post window, then
                            # open this one with a snapshot of the pre window.
                            if _rec["open"] is not None:
                                _measure_facade._finalize_compaction_boundary(_rec["open"], _rec["notices"])
                                _rec["boundaries"].append(_rec["open"])
                                if len(_rec["boundaries"]) > _measure_facade._RECOVERY_EVENTS_KEPT:
                                    _rec["boundaries"] = _rec["boundaries"][-_measure_facade._RECOVERY_EVENTS_KEPT:]
                            _md = record.get("compactMetadata")
                            _md = _md if isinstance(_md, dict) else {}
                            _pre_seg = _rec["seg"]
                            _rec["open"] = {
                                "boundary_id": str(record.get("uuid") or f"line-{idx}"),
                                "ordinal": compactions,
                                "line": idx,
                                "ts": ts,
                                "trigger": _md.get("trigger"),
                                "pre_tokens": _md.get("preTokens"),
                                "post_tokens": _md.get("postTokens"),
                                "host_version": record.get("version"),
                                "model": current_model,
                                "pre_seg": _pre_seg,
                                "pre_seg_calls": _rec["seg_completed"],
                                "pre_tail": [c for c in _rec["completed"] if c["seg"] == _pre_seg],
                                "post_seg_calls": 0,
                                "post_calls": [],
                                "restore_transcript": False,
                                "restore_line": None,
                                "capture_notice": False,
                                "window_data": True,
                            }
                            _rec["seg"] += 1
                            _rec["seg_completed"] = 0
                            _rec["seg_issued"] = 0
                    reads = []
                    writes = []
                    tool_results = []
                    tool_result_meta = []
                    tool_name_by_id = {}
                    system_reminders = []
                    messages = []
                    agent_dispatches = []
                    decisions = []
                    # Drop the pre-compaction footprint: the next assistant turn's
                    # usage reports the new (smaller) context. If the session ends
                    # right after a compaction with no further turn, leaving the old
                    # value would over-report fill. None falls through to the
                    # char-length estimate of whatever follows the boundary.
                    context_tokens = None
                    idx += 1
                    continue

                # Hook-delivered recovery evidence: restore markers, loop
                # notices, capture confirmations. The raw-line substring gate
                # keeps this free on records that cannot carry the markers.
                if (not _is_sidechain
                        and rec_type in ("attachment", "system", "progress")
                        and "[Token Optimizer]" in line):
                    _hook_text = _measure_facade._hook_payload_text(record)
                    if _hook_text:
                        if _measure_facade._REC_LOOP_NOTICE_RE.search(_hook_text):
                            _hkey = ("loop", _rec["seg"], idx)
                            if _hkey not in _rec["hookseen"]:
                                _rec["hookseen"].add(_hkey)
                                _rec["notices"].append({"seg": _rec["seg"], "kind": "loop", "line": idx})
                        if _measure_facade._REC_RESTORE_NOTICE_RE.search(_hook_text):
                            _hkey = ("restore", _rec["seg"])
                            if _hkey not in _rec["hookseen"]:
                                _rec["hookseen"].add(_hkey)
                                _rec["notices"].append({"seg": _rec["seg"], "kind": "restore", "line": idx})
                            if (_rec["open"] is not None
                                    and _rec["seg"] == _rec["open"]["ordinal"]
                                    and _rec["seg_issued"] <= _measure_facade._RECOVERY_WINDOW_CALLS):
                                _rec["open"]["restore_transcript"] = True
                                _rec["open"]["restore_line"] = idx
                        if (_rec["open"] is not None
                                and _rec["seg"] == _rec["open"]["ordinal"]
                                and _measure_facade._REC_CAPTURE_NOTICE_RE.search(_hook_text)):
                            _rec["open"]["capture_notice"] = True

                # System reminders (detect duplicates via content hash)
                if rec_type == "system":
                    msg_content = str(record.get("message", ""))
                    if "system-reminder" in msg_content:
                        content_hash = _measure_facade.hashlib.sha256(msg_content.encode()).hexdigest()[:16]
                        system_reminders.append((idx, content_hash, len(msg_content)))

                # User messages
                if rec_type == "user":
                    text = _measure_facade._extract_user_text(record)
                    is_substantive = len(text.split()) > 10
                    if not _is_sidechain:
                        safe_pause = bool(text.strip()) and not record.get("isMeta")
                    messages.append((idx, "user", len(text), is_substantive))

                # Assistant messages
                if rec_type == "assistant":
                    msg = record.get("message", {})
                    if not isinstance(msg, dict):
                        msg = {}
                    if not _is_sidechain:
                        safe_pause = msg.get("stop_reason") in ("end_turn", "stop_sequence")
                    content = msg.get("content", [])
                    text_length = 0
                    is_substantive = False

                    # Track the live context footprint + model from API usage.
                    # Guard the shape: malformed/truncated JSONL can carry a
                    # non-dict `usage` or non-int token fields; a raw + would
                    # raise and abort the whole parse (losing all quality data).
                    usage = msg.get("usage")
                    if isinstance(usage, dict):
                        tok = _measure_facade._context_token_count(_measure_facade._context_usage(usage))
                        if tok > 0:
                            context_tokens = tok
                    model_str = msg.get("model")
                    if isinstance(model_str, str) and model_str:
                        current_model = model_str

                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue

                            if block.get("type") == "text":
                                txt = block.get("text", "")
                                text_length += len(txt)
                                if len(txt.split()) > 20:
                                    is_substantive = True
                                # Check for decisions
                                if _measure_facade._DECISION_RE.search(txt):
                                    snippet = txt[:200].strip()
                                    decisions.append((idx, snippet))

                            elif block.get("type") == "tool_use":
                                is_substantive = True  # tool invocations ARE decisions
                                tool_calls += 1
                                tool_name = block.get("name", "")
                                tool_id = block.get("id", "")
                                if tool_id:
                                    tool_name_by_id[tool_id] = tool_name
                                if not _is_sidechain:
                                    _measure_facade._rec_track_tool_use(_rec, block, idx, ts, _rec_cwd)
                                inp = block.get("input", {})

                                if tool_name == "Read":
                                    path = inp.get("file_path", "")
                                    if path:
                                        reads.append((idx, path, ts))
                                elif tool_name in ("Edit", "Write"):
                                    path = inp.get("file_path", "")
                                    if path:
                                        writes.append((idx, path, ts))
                                elif tool_name in ("Task", "Agent"):
                                    prompt_text = inp.get("prompt", "")
                                    agent_dispatches.append((idx, len(prompt_text), 0))

                    messages.append((idx, "assistant", text_length, is_substantive))

                # Tool results
                if rec_type == "tool_result" or (
                    rec_type == "user" and isinstance(record.get("message", {}), dict)
                    and isinstance(record.get("message", {}).get("content", []), list)
                ):
                    msg = record.get("message", {})
                    if isinstance(msg, dict):
                        content = msg.get("content", [])
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "tool_result":
                                    result_text = _measure_facade._extract_tool_result_text(block)
                                    tool_id = block.get("tool_use_id", "")
                                    tool_results.append((idx, tool_id, len(result_text), False))
                                    if not _is_sidechain:
                                        _measure_facade._rec_track_tool_result(_rec, block, idx, ts)
                                    tool_result_meta.append({
                                        "index": idx,
                                        "tool_id": tool_id,
                                        "tool_name": tool_name_by_id.get(tool_id, ""),
                                        "size": len(result_text),
                                        "is_failure": _measure_facade._tool_result_looks_failed(block, result_text),
                                    })

                                    # Update agent dispatch result sizes
                                    if agent_dispatches and agent_dispatches[-1][2] == 0:
                                        last = agent_dispatches[-1]
                                        agent_dispatches[-1] = (last[0], last[1], len(result_text))

                idx += 1

    except (PermissionError, OSError):
        return None

    # Close the last open boundary at EOF: its post window is whatever the
    # session produced before ending (possibly still filling -> "observing").
    if _rec["open"] is not None:
        _measure_facade._finalize_compaction_boundary(_rec["open"], _rec["notices"])
        _rec["boundaries"].append(_rec["open"])

    if not messages:
        return None

    return {
        "reads": reads,
        "writes": writes,
        "tool_results": tool_results,
        "tool_result_meta": tool_result_meta,
        "system_reminders": system_reminders,
        "messages": messages,
        "compactions": compactions,
        "tool_calls": tool_calls,
        "safe_pause": safe_pause,
        "agent_dispatches": agent_dispatches,
        "decisions": decisions,
        "total_entries": idx,
        "context_tokens": context_tokens,
        "model": current_model,
        "compaction_events": _rec["boundaries"],
        "first_ts": _measure_facade._keepwarm_iso_to_epoch(_rec_first_ts) if _rec_first_ts else None,
        "last_ts": _measure_facade._keepwarm_iso_to_epoch(_rec_last_ts) if _rec_last_ts else None,
        # Context-window size for THIS session's actual model, not the
        # env/config global that detect_context_window() falls back to.
        # See _context_window_for_model_str() -- fixes fill%/quality scoring
        # for sessions whose live model differs from CLAUDE_MODEL/settings.json
        # (e.g. a 1M-context variant while the global default says plain).
        "model_context_window": _measure_facade._context_window_for_model_str(current_model),
    }


def detect_stale_reads(quality_data):
    """Find Read tool calls whose content is genuinely outdated in context.

    A read is counted as stale ONLY when it represents wasted token budget,
    not when it is part of a normal edit workflow. The canonical happy path
    (Read file X, then Edit X a few turns later to incorporate the read
    content) is NOT stale — the read informed the edit, and is expected.

    Two real stale patterns are detected:

    1. **Re-read after write**: the same file was written earlier in the
       session and the read is happening again. The context already has
       the post-write content, so re-reading is wasted tokens. Always
       counted.

    2. **Far-distance stale**: a read whose corresponding write happens
       more than ``_STALE_READ_DISTANCE_THRESHOLD`` records later. The
       read content sat in the working set long enough that the edit
       is unrelated to it (or is a late-breaking change), and the old
       snapshot in context is now outdated. Only counted when the read
       is not followed by a later re-read on the same path.

    Returns: list of (path, read_index, trigger_index) and estimated
    waste tokens. ``trigger_index`` is the index of the prior write
    (for re-reads) or the later write (for far-distance stale), so
    downstream explanations can surface both halves of the pattern.
    """
    _measure_facade = _compat.facade()
    reads = quality_data["reads"]
    writes = quality_data["writes"]

    writes_by_path = {}
    for widx, wpath, _wts in writes:
        writes_by_path.setdefault(wpath, []).append(widx)
    for wlist in writes_by_path.values():
        wlist.sort()

    reads_by_path = {}
    for ridx, rpath, _rts in reads:
        reads_by_path.setdefault(rpath, []).append(ridx)
    for rlist in reads_by_path.values():
        rlist.sort()

    stale = []
    estimated_waste_tokens = 0
    AVG_READ_TOKENS = 2000

    for ridx, rpath, _rts in reads:
        path_writes = writes_by_path.get(rpath, [])
        if not path_writes:
            continue

        # 1. Re-read after write — we already modified the file, reading
        # again is wasted tokens (context already has the post-write view
        # implicitly via the write itself).
        prior_writes = [w for w in path_writes if w < ridx]
        if prior_writes:
            stale.append((rpath, ridx, prior_writes[-1]))
            estimated_waste_tokens += AVG_READ_TOKENS
            continue

        # 2. Far-distance stale — read happened, then the file sat for a
        # long time before being edited. The original read content is
        # still sitting in context even though it has drifted. Only flag
        # when the distance is large; normal Read-then-Edit is NOT stale.
        later_writes = [w for w in path_writes if w > ridx]
        if not later_writes:
            continue
        first_later_write = later_writes[0]
        if first_later_write - ridx > _measure_facade._STALE_READ_DISTANCE_THRESHOLD:
            # Don't double-count: if there is a later re-read of the same
            # path, the re-read will be flagged on its own iteration.
            later_reads = [r for r in reads_by_path.get(rpath, []) if r > ridx]
            if not later_reads:
                stale.append((rpath, ridx, first_later_write))
                # Half the waste estimate — the read was still used for
                # the eventual edit, it just aged in context.
                estimated_waste_tokens += AVG_READ_TOKENS // 2

    return {
        "stale_reads": stale,
        "count": len(stale),
        "estimated_waste_tokens": estimated_waste_tokens,
    }


def _session_stale_waste_tokens(filepath):
    """Reclaimable stale-read waste (tokens) for one session's JSONL, or 0.

    Reuses the same quality parse + detect_stale_reads the scorer runs, but
    persists the token estimate the scorer discards. This is RECLAIMABLE waste
    (reads that slipped through and were billed), surfaced ONLY in the
    opportunity tier — it is never counted as realized savings. Guarded: any
    parse failure or oversized file yields 0 (conservative under-count).
    """
    _measure_facade = _compat.facade()
    try:
        qd = _measure_facade._parse_jsonl_for_quality(filepath)
        if not qd:
            return 0
        return int(_measure_facade.detect_stale_reads(qd).get("estimated_waste_tokens", 0) or 0)
    except Exception:
        return 0


def detect_reread_loops(quality_data):
    """Find files read 3+ times in the session, a signal of context rot.

    When the model re-reads the same file repeatedly, it has likely forgotten
    what it already read. This is distinct from stale reads (which track
    read-after-write). Bounded to the last _QUALITY_ROLLING_WINDOW reads.
    """
    _measure_facade = _compat.facade()
    reads = quality_data["reads"][-_measure_facade._QUALITY_ROLLING_WINDOW:]
    path_counts = {}
    for _ridx, rpath, _rts in reads:
        path_counts[rpath] = path_counts.get(rpath, 0) + 1

    reread_paths = {p: c for p, c in path_counts.items() if c >= 3}
    total_excess = sum(c - 1 for c in reread_paths.values())
    AVG_READ_TOKENS = 2000

    return {
        "reread_paths": reread_paths,
        "count": len(reread_paths),
        "excess_reads": total_excess,
        "estimated_waste_tokens": total_excess * AVG_READ_TOKENS,
    }


def detect_bloated_results(quality_data):
    """Find large tool results (>4KB) never meaningfully referenced afterward.

    A tool result is "bloated" if it's large and no subsequent assistant
    message references key terms from it.

    Returns: list of bloated results and estimated waste tokens.
    """
    _measure_facade = _compat.facade()
    BLOAT_THRESHOLD_CHARS = 4000  # ~1000 tokens
    tool_results = quality_data["tool_results"]
    messages = quality_data["messages"]

    bloated = []
    estimated_waste_tokens = 0

    for ridx, tool_id, result_size, _ in tool_results:
        if result_size < BLOAT_THRESHOLD_CHARS:
            continue

        # Check if any subsequent assistant message is substantive
        # (simplified heuristic: if the next few messages are substantive,
        # the result was probably used)
        was_referenced = False
        for midx, role, text_len, is_substantive in messages:
            if midx > ridx and role == "assistant" and is_substantive:
                was_referenced = True
                break
            if midx > ridx + 10:  # Only look ahead 10 entries
                break

        if not was_referenced:
            bloated.append((tool_id, ridx, result_size))
            estimated_waste_tokens += int(result_size / _measure_facade.CHARS_PER_TOKEN)

    return {"bloated_results": bloated, "count": len(bloated), "estimated_waste_tokens": estimated_waste_tokens}


def detect_duplicates(quality_data):
    """Find repeated system reminders or re-injected content.

    Returns: count of duplicate injections and estimated waste tokens.
    """
    _measure_facade = _compat.facade()
    reminders = quality_data["system_reminders"]
    seen_hashes = {}
    duplicates = 0
    estimated_waste_tokens = 0

    for ridx, content_hash, size_chars in reminders:
        if content_hash in seen_hashes:
            duplicates += 1
            estimated_waste_tokens += int(size_chars / _measure_facade.CHARS_PER_TOKEN)
        else:
            seen_hashes[content_hash] = ridx

    return {"duplicates": duplicates, "estimated_waste_tokens": estimated_waste_tokens}


def _session_recovery_db_events(session_id, since_epoch=None):
    """Best-effort pull of TO lifecycle events for restore/loop evidence.

    Returns [{"kind", "ts"(epoch), "delta"?}] or []. Read-only, bounded,
    and silent on any failure -- missing telemetry is unknown, not healthy.
    """
    _measure_facade = _compat.facade()
    try:
        uuid, _unjoinable = _measure_facade._extract_session_uuid(session_id)
        if not uuid or not _measure_facade.TRENDS_DB.exists():
            return []
        conn = _measure_facade.sqlite3.connect(f"file:{_measure_facade.TRENDS_DB}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT timestamp, kind FROM ("
                "  SELECT timestamp, event_type AS kind FROM savings_events"
                "   WHERE session_uuid = ? AND event_type IN"
                "     ('checkpoint_restore','loop_detection','tool_archive_reexpand')"
                "  UNION ALL"
                "  SELECT timestamp, feature AS kind FROM compression_events"
                "   WHERE session_uuid = ? AND feature IN"
                "     ('checkpoint_restore','loop_detection','tool_archive_reexpand')"
                ") ORDER BY timestamp DESC LIMIT 2000",
                (uuid, uuid),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    out = []
    for ts_raw, kind in rows:
        epoch = _measure_facade._keepwarm_iso_to_epoch(ts_raw)
        if epoch is None:
            # DB timestamps are local naive ISO; .timestamp() applies local tz.
            try:
                epoch = _measure_facade.datetime.fromisoformat(str(ts_raw)).timestamp()
            except (ValueError, TypeError):
                continue
        if since_epoch is not None and epoch < since_epoch - 900:
            continue
        out.append({"kind": kind, "ts": epoch})
    return out


def _assess_compaction_recovery(quality_data, session_id=None):
    """Build the recovery panel for the quality result.

    The public status stays observing while thresholds are calibrated. Raw
    evidence retains provisional boundary statuses, including unmeasurable.
    Compaction count carries no weight, and missing evidence stays unknown.
    """
    _measure_facade = _compat.facade()
    coverage = {
        "errors": True, "rereads": True, "repeats": True,
        "loops_transcript": True, "loops_db": False, "restore_db": False,
        "corrections": False, "state_sections": "not_applicable_v1",
    }
    try:
        compactions = int(quality_data.get("compactions") or 0)
    except (TypeError, ValueError):
        compactions = 0
    events = [e for e in (quality_data.get("compaction_events") or [])
              if isinstance(e, dict)]
    if compactions <= 0:
        return {"status": None, "score": None, "evidence": [], "coverage": coverage}
    if not events:
        return {"status": "observing", "score": None, "evidence": [],
                "coverage": coverage}

    epochs = [e.get("ts_epoch") for e in events if e.get("ts_epoch") is not None]
    since = min(epochs) if epochs else None
    db_events = (_measure_facade._session_recovery_db_events(session_id, since_epoch=since)
                 if session_id else [])
    if db_events:
        coverage["loops_db"] = coverage["restore_db"] = True
    restores = [e for e in db_events if e["kind"] == "checkpoint_restore"]
    loops = [e for e in db_events if e["kind"] == "loop_detection"]

    # Each restore event belongs to the nearest boundary within -120/+300s.
    for ev in restores:
        best, best_d = None, None
        for b in events:
            bt = b.get("ts_epoch")
            if bt is None:
                continue
            d = ev["ts"] - bt
            if -120 <= d <= 300 and (best is None or abs(d) < abs(best_d)):
                best, best_d = b, d
        if best is not None:
            best.setdefault("db_restore_deltas", []).append(round(best_d, 1))
            best["restore_db"] = True

    for b in events:
        start, end = b.get("ts_epoch"), b.get("end_epoch")
        b["db_loops"] = (sum(1 for e in loops if start <= e["ts"] <= end)
                         if start is not None and end is not None else 0)
        rt, rd = bool(b.get("restore_transcript")), bool(b.get("restore_db"))
        b["restore_evidence"] = ("both" if rt and rd else "transcript" if rt
                                 else "db" if rd else "none")
        status, score, flags = _measure_facade._recovery_boundary_eval(b)
        b["status"], b["score"], b["flags"] = status, score, flags

    # The latest boundary with an unfinished window is still being observed;
    # older truncated windows are simply unmeasurable.
    last = events[-1]
    if (last.get("status") == "unmeasurable"
            and last.get("window_data", True)
            and int(last.get("post_seg_calls") or 0) < _measure_facade._RECOVERY_MIN_MATCHED_CALLS):
        last["status"] = "observing"
        last["score"] = None

    evidence = [{k: v for k, v in b.items() if k not in ("pre_tail", "post_calls")}
                for b in events[-_measure_facade._RECOVERY_EVENTS_KEPT:]]

    # Thresholds are shadow-only until calibrated. Preserve raw evaluations
    # in evidence, but never turn them into a public health judgement.
    return {"status": "observing", "score": None, "evidence": evidence,
            "coverage": coverage}


def _recovery_panel_detail(compactions, recovery):
    """One-line neutral evidence summary, e.g. '17 compactions; restore
    delivered; 20 calls observed; 1 tool error; no repeated failed action'."""
    _measure_facade = _compat.facade()
    if compactions <= 0:
        return "No compactions"
    status = (recovery or {}).get("status") or "unknown"
    ev = (recovery or {}).get("evidence") or []
    latest = ev[-1] if ev else {}
    post = latest.get("post") or {}
    restore = latest.get("restore_evidence", "none")
    restore_label = {
        "transcript": "restore delivered", "both": "restore delivered",
        "db": "restore recorded", "none": "no restore observed",
    }.get(restore, "restore unknown")
    parts = [f"{compactions} compaction(s)", restore_label]
    if post.get("calls"):
        parts.append(f"{post['calls']} calls observed")
        parts.append(f"{post['errors']} tool error(s)")
        parts.append("repeated failed action"
                     if post.get("repeat_after_error") else "no repeated failed action")
    parts.append({
        "observing": "observing",
        "no_concerning_signal_observed": "no concerning signal observed",
        "needs_attention": "needs attention",
        "unmeasurable": "unmeasurable",
    }.get(status, status))
    return "; ".join(parts)


def compute_quality_score(quality_data, session_id=None):
    """Compute weighted composite quality score 0-100.

    session_id, when provided, gates the global live-fill.json sidecar so a
    session is never scored against another concurrent session's context fill.

    Each signal is scored 0-100, then weighted per _QUALITY_WEIGHTS.
    Higher = better quality (less waste).
    7 signals: context fill degradation, stale reads, bloated results,
    duplicates, compaction depth, decision density, agent efficiency.
    """
    _measure_facade = _compat.facade()
    total_messages = len(quality_data["messages"])
    if total_messages == 0:
        return {"score": 0, "signals": {}, "breakdown": {}}

    # 0. Context fill degradation
    # Priority: live fill from statusline (real-time) > JSONL context_tokens (previous turn) > char-length estimate
    ctx_window, ctx_window_source = _measure_facade.detect_context_window()
    model_context_window = quality_data.get("model_context_window") or ctx_window
    # Provenance follows whichever window we actually divide by. Without it a
    # wrong denominator is indistinguishable from a right one: the
    # numerator looks correct, so the percentage looks plausible.
    model_context_window_source = (
        "session data" if quality_data.get("model_context_window") else ctx_window_source
    )
    model_name = quality_data.get("model") or quality_data.get("current_model")
    # Transcripts carry the plain 4.6 id even for the 1M variant: promote when
    # settings say [1m] or the observed tokens already prove it.
    window_inferred_from_tokens = False
    if quality_data.get("model_context_window"):
        _promoted, _why, window_inferred_from_tokens = _measure_facade._promote_plain_46_window(
            model_name, model_context_window, quality_data.get("context_tokens"))
        if _why:
            model_context_window = _promoted
            model_context_window_source = _why

    # Effective compact window for this session's model (env > modelSettings
    # > autoCompactWindow > default). Fill denominates against it ONLY when a
    # real user override shrank the window below the model window (PR #210
    # semantics: the tuned default is not an override of the host's number).
    compact_window_resolved = _measure_facade._resolve_compact_window(model_name)
    compact_window = compact_window_resolved["tokens"]
    compact_window_source = compact_window_resolved["source"]
    compact_window_reduced = (
        compact_window_resolved["user_override"]
        and 0 < compact_window < model_context_window
    )
    fill_denominator = compact_window if compact_window_reduced else model_context_window

    def _effective_fill(model_share, tokens=None):
        """Share of the EFFECTIVE window for a given share of the model window.

        Without a user override the two are the same number. With one, the
        override is an absolute token count, so real tokens are divided by it
        directly (no dependence on our inferred model window); only when the
        token count is unknown is the model share rescaled.
        """
        _measure_facade = _compat.facade()
        if not compact_window_reduced or not fill_denominator:
            return model_share
        if tokens is not None and tokens > 0:
            return min(1.0, max(0.0, float(tokens) / fill_denominator))
        return min(1.0, max(0.0, model_share * model_context_window / fill_denominator))

    # fill_pct: share of the EFFECTIVE window (what the user sees, what nudges
    # gate on). model_fill: share of the MODEL window -- retrieval quality is a
    # function of real fill, so the MRCR curve and degradation bands keep the
    # model denominator.
    fill_pct = None
    model_fill = None
    # Set when observed tokens exceed the window: that is not a full context, it
    # is a wrong window, and it must not be reported as a percentage.
    window_contradicted = False
    # Same-session host fill reading (any age). The host measures the real
    # window; our token arithmetic is only as good as an inferred denominator.
    # Below, a recent same-session reading that disagrees with our computed fill
    # by >10 points wins the sanity check — the phantom-fill failure mode that
    # produced "bar shows 16%, score is 59".
    host_fill_pct = None
    host_fill_age_s = None
    host_live_tokens = None
    host_disagreement = None
    # Which source produced fill_pct, for cache diagnosability.
    fill_source = None
    try:
        live_fill_path = _measure_facade.QUALITY_CACHE_DIR / "live-fill.json"
        if live_fill_path.exists():
            live = _measure_facade.json.loads(live_fill_path.read_text(encoding="utf-8"))
            age = _measure_facade.time.time() - live.get("timestamp", 0) / 1000  # JS timestamp is ms
            live_sid = _measure_facade.sanitize_session_id(str(live.get("session_id") or ""))
            want_sid = _measure_facade.sanitize_session_id(str(session_id or ""))
            if want_sid and live_sid == want_sid:
                _used = float(live["used_percentage"])
                # json.loads accepts the non-standard literals NaN/Infinity. NaN
                # compares False against everything, so max()/min() would pass it
                # through as a silent 0.0 and suppress every nudge with no error.
                if not _measure_facade.math.isfinite(_used):
                    raise ValueError("non-finite used_percentage")
                # The host's own percentage is the one number here that does not
                # depend on our inferred window, so it is kept as reported.
                host_fill_pct = min(1.0, max(0.0, _used / 100.0))
                host_fill_age_s = age
                live_tokens = live.get("context_tokens")
                if not (isinstance(live_tokens, (int, float)) and live_tokens > 0):
                    live_tokens = None
                host_live_tokens = live_tokens
                # The host knows the real window; we only infer it. When the
                # host rescues us from a bad denominator the user sees a correct
                # number and the misconfiguration stays invisible, so record the
                # disagreement rather than quietly accepting the save.
                if age < 10:
                    model_fill = host_fill_pct
                    fill_pct = _effective_fill(model_fill, live_tokens)
                    fill_source = "host-live"
    except (_measure_facade.json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
        # used_percentage arrives from a JSON file on disk. A non-numeric value
        # raises TypeError on the division, which was NOT caught here and
        # propagated out of compute_quality_score to crash the whole invocation.
        pass
    if fill_pct is None:
        try:
            context_tokens = quality_data.get("context_tokens")
            if context_tokens is not None and model_context_window:
                raw_ratio = float(context_tokens) / float(model_context_window)
                # min(1.0, ...) turns "250k tokens in a 200k window" into a
                # confident-looking 100%. That clamp is what let a misdetected
                # denominator pass as a real reading. Record the
                # contradiction first; the clamp still runs so downstream
                # curve math keeps its 0-1 contract.
                window_contradicted = raw_ratio > 1.0
                model_fill = min(1.0, max(0.0, raw_ratio))
                fill_pct = _effective_fill(model_fill, float(context_tokens))
                fill_source = "transcript-tokens"
        except (TypeError, ValueError):
            fill_pct = None
    if fill_pct is None:
        CHARS_PER_TOKEN = 4
        total_chars = sum(tlen for _, _, tlen, _ in quality_data["messages"])
        total_chars += sum(rsize for _, _, rsize, _ in quality_data["tool_results"])
        total_chars += sum(ssize for _, _, ssize in quality_data["system_reminders"])
        estimated_tokens = total_chars / CHARS_PER_TOKEN
        if ctx_window > 0:
            model_fill = min(1.0, estimated_tokens / ctx_window)
        else:
            model_fill = 0
        fill_pct = min(1.0, estimated_tokens / fill_denominator) if fill_denominator > 0 else 0
        fill_source = "char-estimate"
    if model_fill is None:
        model_fill = fill_pct
    # the session id survives /compact, so a reading from just before one
    # still names this session. When the host's token count is off ours by more
    # than 2x it describes a different moment (a numerator change), not a wrong
    # denominator, so the override below skips it.
    # Cross-check: if the host told us the fill and our own arithmetic would have
    # produced a materially different one, our window is wrong even though the
    # displayed number is right. Always recorded — and when the host reading is
    # recent enough to still describe this session, the host wins: a >10-point
    # gap is a denominator error (multiples), not measurement noise, and
    # serving the phantom fill is how a fresh 1M session reported
    # "16% fill, score 59". Our arithmetic never overrules the host; only the
    # host overrules it.
    if host_fill_pct is not None:
        try:
            _tokens = quality_data.get("context_tokens")
            if _tokens is not None and model_context_window:
                _ours = min(1.0, max(0.0, float(_tokens) / float(model_context_window)))
                # Wide margin on purpose: this fires on denominator errors, which
                # are multiples, not on rounding.
                if abs(_ours - host_fill_pct) > 0.10:
                    host_disagreement = {
                        "host_pct": round(host_fill_pct * 100, 1),
                        "computed_pct": round(_ours * 100, 1),
                        "window": model_context_window,
                        "window_source": model_context_window_source,
                    }
                    # Sanity window: a reading older than ~5min describes a
                    # different moment (same convention as the statusline's
                    # 5-min staleness guard); fresher than that, the host's real
                    # window beats our inferred one.
                    _other_moment = bool(  # host tokens >2x off ours (see above)
                        host_live_tokens and float(_tokens) > 0
                        and max(host_live_tokens, float(_tokens))
                        > 2.0 * min(host_live_tokens, float(_tokens)))
                    if fill_source != "host-live" and not _other_moment and (
                            host_fill_age_s is not None and host_fill_age_s < 300):
                        model_fill = host_fill_pct
                        fill_pct = _effective_fill(model_fill, float(_tokens))
                        fill_source = "host-stale-override"
        except (TypeError, ValueError):
            pass

    fill_quality, curve_name = _measure_facade._estimate_quality_with_curve(
        model_fill,
        model=model_name,
        context_window=model_context_window,
    )
    # Scale to 0-100 score (76 at worst = 0, 98 at best = 100)
    fill_score = max(0, min(100, (fill_quality - 76) / (98 - 76) * 100))

    # 1. Stale reads: rolling window to prevent denominator-expansion bias.
    # Only the last N reads are scored, so early stale reads don't get
    # diluted by later clean reads accumulating in the denominator.
    all_reads = quality_data["reads"]
    stale_data = _measure_facade.detect_stale_reads(quality_data)
    stale_set = {(rpath, ridx) for rpath, ridx, _ in stale_data["stale_reads"]}
    window_reads = all_reads[-_measure_facade._QUALITY_ROLLING_WINDOW:]
    window_stale = 0
    if window_reads:
        window_stale = sum(1 for ridx, rpath, _ in window_reads if (rpath, ridx) in stale_set)
        stale_ratio = window_stale / len(window_reads)
        stale_score = max(0, min(100, 100 - stale_ratio * 100))
    else:
        stale_score = 100

    # 2. Bloated results: rolling window over last N tool results.
    all_results = quality_data["tool_results"]
    bloated_data = _measure_facade.detect_bloated_results(quality_data)
    bloated_set = {bidx for _, bidx, _ in bloated_data["bloated_results"]}
    window_results = all_results[-_measure_facade._QUALITY_ROLLING_WINDOW:]
    window_bloated = 0
    if window_results:
        window_bloated = sum(1 for ridx, _, _, _ in window_results if ridx in bloated_set)
        bloated_ratio = window_bloated / len(window_results)
        bloated_score = max(0, min(100, 100 - bloated_ratio * 300))
    else:
        bloated_score = 100

    # 3. Duplicates: display-only signal (not in either composite weight dict).
    # Duplicate waste feeds ResourceHealth indirectly via absolute_waste_tokens.
    dup_data = _measure_facade.detect_duplicates(quality_data)
    dup_score = max(0, min(100, 100 - dup_data["duplicates"] * 10))

    # 4. Compaction count is descriptive metadata only -- it carries NO
    # quality weight. Measured post-boundary recovery signals are reported
    # separately in the recovery panel (see _assess_compaction_recovery);
    # the count-only "~65/88/95% lost" model was unsupported by the paired
    # event study and is removed (COMPACTION-QUALITY.md).
    compactions = quality_data["compactions"]

    # 5. Decision density: rolling window over last N messages.
    all_messages = quality_data["messages"]
    window_messages = all_messages[-_measure_facade._QUALITY_ROLLING_WINDOW:]
    substantive = sum(1 for _, _, _, s in window_messages if s)
    window_msg_count = len(window_messages)
    if window_msg_count >= _measure_facade._DENSITY_MIN_MESSAGES:
        density_ratio = substantive / window_msg_count
        density_score = min(100, density_ratio * 200)  # 50% substantive = 100
    else:
        # Too few messages to judge decision density (brand-new session). Stay
        # neutral instead of penalizing — mirrors agent_efficiency's no-data 80.
        density_ratio = substantive / window_msg_count if window_msg_count else 0
        density_score = 80

    # 6. Agent efficiency: rolling window over last N dispatches.
    all_dispatches = quality_data["agent_dispatches"]
    window_dispatches = all_dispatches[-_measure_facade._QUALITY_ROLLING_WINDOW:]
    if window_dispatches:
        total_prompt = sum(p for _, p, _ in window_dispatches)
        total_result = sum(r for _, _, r in window_dispatches)
        if total_prompt > 0:
            efficiency = total_result / (total_prompt + total_result) if (total_prompt + total_result) > 0 else 0.5
            agent_score = min(100, efficiency * 150)  # 67% efficiency = 100
        else:
            agent_score = 80
    else:
        agent_score = 80  # No agents = neutral score

    # 7. Signal-to-noise ratio (diagnostic-only, not weighted in either composite yet)
    noise_chars = sum(ssize for _, _, ssize in quality_data["system_reminders"])
    noise_chars += dup_data.get("estimated_waste_tokens", 0) * 4  # tokens to chars estimate
    signal_chars = sum(tlen for _, _, tlen, s in all_messages if s)
    total_chars = signal_chars + noise_chars
    if total_chars > 0:
        snr_ratio = signal_chars / total_chars
        snr_score = min(100, snr_ratio * 125)  # 80% signal = perfect score
    else:
        snr_score = 50  # neutral when no data

    # 8. Re-reading loop detection (diagnostic-only, not weighted yet)
    reread_data = _measure_facade.detect_reread_loops(quality_data)

    signals = {
        "context_fill_degradation": round(fill_score, 1),
        "stale_reads": round(stale_score, 1),
        "bloated_results": round(bloated_score, 1),
        "duplicates": round(dup_score, 1),
        "decision_density": round(density_score, 1),
        "agent_efficiency": round(agent_score, 1),
    }

    # Build breakdown with token estimates
    total_waste = (
        stale_data["estimated_waste_tokens"]
        + bloated_data["estimated_waste_tokens"]
        + dup_data["estimated_waste_tokens"]
    )

    # Absolute waste tokens signal for ResourceHealth (0-100, higher = less waste)
    waste_fraction = total_waste / max(ctx_window, 1) if ctx_window > 0 else 0
    waste_score = max(0, min(100, 100 - waste_fraction * 1000))
    signals["absolute_waste_tokens"] = round(waste_score, 1)

    # v6 dual composite: ResourceHealth (monotonic) + SessionEfficiency (flexible)
    resource_health = sum(
        signals[k] * _measure_facade._RESOURCE_HEALTH_WEIGHTS[k]
        for k in _measure_facade._RESOURCE_HEALTH_WEIGHTS
    )

    # Which signal actually pulls ResourceHealth down? The displayed score IS
    # resource_health, so attribute its deficit by weighted contribution —
    # weight * (100 - signal) — not by which waste key happens to exist. This
    # is what the "biggest drag" wording everywhere must reflect: a 70% fill
    # drags harder than one stale read even when no waste was recorded.
    _drag_deficit = {
        k: _measure_facade._RESOURCE_HEALTH_WEIGHTS[k] * max(0.0, 100.0 - signals[k])
        for k in _measure_facade._RESOURCE_HEALTH_WEIGHTS
    }
    top_drag = None
    # Fill is intrinsic to any conversation: a deficit under ~14 pts is the
    # cost of having talked at all, not a drag worth naming on an otherwise
    # healthy session. Rescaled from the v1 floor of 10 weighted pts at the
    # old 0.5 fill weight so the same ~22%-fill boundary applies under the
    # v2 5/7 weight. Waste keeps the 3-point floor.
    _fill_floor = 10.0 * (_measure_facade._RESOURCE_HEALTH_WEIGHTS["context_fill_degradation"] / 0.5)
    _nameable = {
        k: pts for k, pts in _drag_deficit.items()
        if pts >= (_fill_floor if k == "context_fill_degradation" else 3.0)
    }
    if _nameable:
        _worst_key = max(_nameable, key=_nameable.get)
        _worst_pts = _nameable[_worst_key]
        if _worst_key == "context_fill_degradation" and window_contradicted:
            # Tokens exceed the window: the window is wrong, the context is not
            # necessarily full. Say that instead of "100% context fill".
            top_drag = {
                "key": "context_window_mismatch",
                "label": "context window mismatch (tokens exceed the detected window)",
                "points": round(_worst_pts, 1),
            }
        elif _worst_key == "context_fill_degradation":
            top_drag = {
                "key": "context_fill",
                "label": (f"{round(model_fill * 100)}% context fill ({round(fill_pct * 100)}% of the way to auto-compact)"
                          if compact_window_reduced else f"{round(fill_pct * 100)}% context fill"),
                "points": round(_worst_pts, 1),
            }
        else:  # absolute_waste_tokens — name the dominant waste cause
            # Only causes that actually feed total_waste (and therefore this
            # signal's deficit) qualify: reread-loop waste is diagnostic-only
            # and must never be named as the drag it did not cause.
            _waste_parts = {
                "bloated_results": (bloated_data["estimated_waste_tokens"],
                                    "bloated tool results"),
                "stale_reads": (stale_data["estimated_waste_tokens"],
                                "stale file reads"),
                "duplicates": (dup_data["estimated_waste_tokens"],
                               "repeated system reminders"),
            }
            _waste_cause = max(_waste_parts, key=lambda k: ((_measure_facade := _compat.facade()), _waste_parts[k][0])[1])
            top_drag = {
                "key": f"waste:{_waste_cause}",
                "label": _waste_parts[_waste_cause][1],
                "points": round(_worst_pts, 1),
            }
    session_efficiency = sum(
        signals[k] * _measure_facade._SESSION_EFFICIENCY_WEIGHTS[k]
        for k in _measure_facade._SESSION_EFFICIENCY_WEIGHTS
    )

    # Bands describe retrieval-quality zones, so they follow the model-window
    # fill (model_fill), not the compact-window fill that nudges gate on.
    band_name, _ = _measure_facade._degradation_band(model_fill)

    _cfd_detail = f"{round(fill_pct * 100)}% fill, {band_name.lower()} ({curve_name})"
    if compact_window_reduced:
        _cfd_detail = (
            f"{round(model_fill * 100)}% of model window "
            f"({round(fill_pct * 100)}% of the way to auto-compact), "
            f"{band_name.lower()} ({curve_name})"
        )

    recovery = _measure_facade._assess_compaction_recovery(quality_data, session_id)

    breakdown = {
        "context_fill_degradation": {
            "score": signals["context_fill_degradation"],
            "fill_pct": round(fill_pct * 100, 1),
            "model_fill_pct": round(model_fill * 100, 1),
            "compact_window": compact_window,
            "compact_window_source": compact_window_source,
            "compact_window_reduced": compact_window_reduced,
            "quality_estimate": fill_quality,
            "quality_curve": curve_name,
            "model": model_name or "unknown",
            "model_context_window": model_context_window,
            "model_context_window_source": model_context_window_source,
            "window_contradicted": window_contradicted,
            "window_inferred_from_tokens": window_inferred_from_tokens,
            "host_disagreement": host_disagreement,
            "fill_source": fill_source,
            "band": band_name,
            "detail": _cfd_detail,
        },
        "stale_reads": {
            "score": signals["stale_reads"],
            "count": stale_data["count"],
            "total_reads": len(all_reads),
            "window_reads": len(window_reads),
            "window_stale": window_stale,
            "estimated_waste_tokens": stale_data["estimated_waste_tokens"],
            "detail": f"{stale_data['count']} stale file reads ({len(window_reads)} in window)" if stale_data["count"] else "No stale reads",
        },
        "bloated_results": {
            "score": signals["bloated_results"],
            "count": bloated_data["count"],
            "total_results": len(all_results),
            "window_results": len(window_results),
            "window_bloated": window_bloated,
            "estimated_waste_tokens": bloated_data["estimated_waste_tokens"],
            "detail": f"{bloated_data['count']} bloated results ({len(window_results)} in window)" if bloated_data["count"] else "No bloated results",
        },
        "duplicates": {
            "score": signals["duplicates"],
            "count": dup_data["duplicates"],
            "estimated_waste_tokens": dup_data["estimated_waste_tokens"],
            "detail": f"{dup_data['duplicates']} duplicate reminders" if dup_data["duplicates"] else "No duplicates",
        },
        "compactions": {
            "count": compactions,
            "weighted": False,
            "detail": f"{compactions} compaction(s)" if compactions > 0 else "No compactions",
        },
        "compaction_recovery": {
            "status": recovery["status"],
            "score": recovery["score"],
            "detail": _measure_facade._recovery_panel_detail(compactions, recovery),
        },
        "decision_density": {
            "score": signals["decision_density"],
            "substantive_messages": substantive,
            "total_messages": total_messages,
            "window_messages": window_msg_count,
            "ratio": round(density_ratio, 2) if window_msg_count > 0 else 0,
            "detail": f"{round(density_ratio * 100)}% substantive ({window_msg_count} in window)" if window_msg_count > 0 else "No messages",
        },
        "agent_efficiency": {
            "score": signals["agent_efficiency"],
            "dispatch_count": len(all_dispatches),
            "window_dispatches": len(window_dispatches),
            "detail": f"{len(all_dispatches)} agent dispatches ({len(window_dispatches)} in window)" if all_dispatches else "No agents used",
        },
        "absolute_waste_tokens": {
            "score": signals["absolute_waste_tokens"],
            "total_waste_tokens": total_waste,
            "waste_fraction": round(waste_fraction, 4),
            "detail": f"{total_waste} waste tokens ({round(waste_fraction * 100, 1)}% of window)" if total_waste > 0 else "No measurable waste",
        },
        "snr": {
            "score": round(snr_score, 1),
            "signal_chars": signal_chars,
            "noise_chars": noise_chars,
            "ratio": round(snr_ratio, 2) if total_chars > 0 else 0,
            "detail": f"{round(snr_ratio * 100)}% signal" if total_chars > 0 else "No data",
        },
        "reread_loops": {
            "count": reread_data["count"],
            "excess_reads": reread_data["excess_reads"],
            "paths": list(reread_data["reread_paths"].keys())[:5],
            "estimated_waste_tokens": reread_data["estimated_waste_tokens"],
            "detail": f"{reread_data['count']} files re-read 3+ times" if reread_data["count"] else "No re-reading loops",
        },
        "total_estimated_waste_tokens": total_waste,
    }

    # Tier 1: independent fill warnings that cannot be masked by composite score
    fill_warning = None
    for threshold, level, message in _measure_facade._FILL_WARN_THRESHOLDS:
        if fill_pct >= threshold:
            fill_warning = {"level": level, "fill_pct": round(fill_pct * 100, 1), "message": message}
            break

    # Tool call fatigue warning (cumulative, not reset on compact).
    # Gated on fill_pct >= 50%: at low fill, tool calls aren't degrading
    # instruction adherence regardless of count (the research was on
    # 200K models where tool results quickly fill the window).
    tc = quality_data.get("tool_calls", 0)
    tool_call_warning = None
    if fill_pct >= 0.50:
        for threshold, level, message in _measure_facade._tool_call_warn_thresholds():
            if tc >= threshold:
                tool_call_warning = {"level": level, "tool_calls": tc, "message": message}
                break

    # 50% fill regime change (COLM 2025: positional bias pattern shifts)
    regime_change = None
    if fill_pct > 0.50:
        regime_change = {
            "fill_pct": round(fill_pct * 100, 1),
            "message": "System prompt erosion accelerating, middle content at highest risk",
        }

    rh_rounded = round(resource_health, 1)
    se_rounded = round(session_efficiency, 1)
    rh_grade = _measure_facade.score_to_grade(round(resource_health))
    se_grade = _measure_facade.score_to_grade(round(session_efficiency))

    return {
        "score": rh_rounded,
        "grade": rh_grade,
        "score_version": _measure_facade._QUALITY_SCORE_VERSION,
        "resource_health": rh_rounded,
        "resource_health_grade": rh_grade,
        "session_efficiency": se_rounded,
        "session_efficiency_grade": se_grade,
        "compactions": compactions,
        "recovery_status": recovery["status"],
        "recovery_score": recovery["score"],
        "recovery_evidence": recovery["evidence"],
        "recovery_coverage": recovery["coverage"],
        "top_drag": top_drag,
        "signals": signals,
        "breakdown": breakdown,
        "fill_warning": fill_warning,
        "tool_call_warning": tool_call_warning,
        "regime_change": regime_change,
        "tool_calls": tc,
        # The window fill_pct was measured against: the user's effective
        # compact window when an override shrank it, else the model window.
        # Callers that convert fill% back to tokens MUST use this window.
        "fill_denominator": fill_denominator,
        "compact_window": compact_window,
        "compact_window_source": compact_window_source,
        # Whether fill_pct is a share of that compact window rather than the
        # model window, and the model-window share beside it, so a reader of
        # the cache (the context hint) can label the percentage correctly (#249).
        "compact_window_reduced": bool(compact_window_reduced),
        "model_fill_pct": round(model_fill * 100, 1),
    }


def _codex_identity_reason():
    """Human reason the invoking Codex thread id is unavailable."""
    _measure_facade = _compat.facade()
    status = _measure_facade.codex_state.thread_identity_status()
    if status == "invalid":
        return "CODEX_THREAD_ID is set but not a valid thread id"
    return "CODEX_THREAD_ID is not set or is empty"


def _codex_thread_resolution():
    """Single resolution outcome for the invoking Codex thread.

    Returns ``{"status", "thread_id", "path", "reason"}``. ``status`` is
    ``resolved`` (identity and transcript found) or ``unresolved`` with a
    ``reason`` of ``identity_absent``, ``identity_invalid`` or
    ``transcript_not_found``. Every Codex consumer renders this one outcome;
    none falls back to another thread's session.
    """
    _measure_facade = _compat.facade()
    try:
        tid = _measure_facade.codex_state.current_thread_id()
    except Exception:
        tid = None
    if not tid:
        invalid = _measure_facade.codex_state.thread_identity_status() == "invalid"
        return {"status": "unresolved", "thread_id": None, "path": None,
                "reason": "identity_invalid" if invalid else "identity_absent"}
    path = _measure_facade.codex_session.find_session_jsonl_exact(tid)
    if path is None:
        return {"status": "unresolved", "thread_id": tid, "path": None,
                "reason": "transcript_not_found"}
    return {"status": "resolved", "thread_id": tid, "path": path, "reason": None}


def _codex_unresolved_message(res):
    _measure_facade = _compat.facade()
    if res["reason"] == "transcript_not_found":
        return (f"Could not resolve the current Codex thread: no transcript found for "
                f"{res['thread_id']}. Pass a session id explicitly if it lives elsewhere.")
    return (f"Could not resolve the current Codex thread ({_measure_facade._codex_identity_reason()}). "
            "Pass a session id explicitly instead of guessing the newest session.")


def _current_jsonl_for_hook(session_id):
    """Resolve the transcript for a hook that may carry an explicit session id.

    Claude/other runtimes: session id first, then the mtime guess (unchanged).
    Codex: when a session id is supplied, its own transcript or None. Falling
    through to the invoking-thread resolver would mix another session's
    transcript into this session's checkpoint/cache (a subagent hook can
    inherit the parent's CODEX_THREAD_ID). With no session id, the invoking
    thread resolver applies.
    """
    _measure_facade = _compat.facade()
    if session_id and _measure_facade._use_codex_session_adapter():
        return _measure_facade._find_session_jsonl_by_id(session_id)
    filepath = _measure_facade._find_session_jsonl_by_id(session_id) if session_id else None
    if filepath is None:
        filepath = _measure_facade._find_current_session_jsonl()
    return filepath


def _find_current_session_jsonl():
    """Find the most recently modified JSONL file across all project directories.

    Claude runtime: searches ALL project dirs and picks the globally most
    recent JSONL, because hooks often run from a CWD that doesn't match the
    active session's project dir.

    Codex runtime: never guesses by mtime. Resolves the invoking thread
    (CODEX_THREAD_ID) to its own transcript, and returns None when the
    identity or its transcript is unavailable. Every Codex caller, including
    checkpoint, cache and dashboard writers, therefore sees "no session" rather
    than another thread's session.
    """
    _measure_facade = _compat.facade()
    if _measure_facade._use_codex_session_adapter():
        return _measure_facade._codex_thread_resolution()["path"]

    # Hermes: no ~/.claude/projects JSONL to scan (sessions live in state.db).
    if _measure_facade._use_hermes_session_adapter():
        return None
    # Antigravity: no ~/.claude/projects JSONL (sessions live in ~/.gemini).
    if _measure_facade._use_antigravity_session_adapter():
        return None

    projects_base = _measure_facade.CLAUDE_DIR / "projects"
    if not projects_base.exists():
        return None
    all_jsonl = []
    for d in projects_base.iterdir():
        if d.is_dir():
            all_jsonl.extend(d.glob("*.jsonl"))
    if not all_jsonl:
        return None
    return max(all_jsonl, key=lambda f: ((_measure_facade := _compat.facade()), f.stat().st_mtime)[1])


def _find_session_jsonl_by_id(session_id):
    """Find a JSONL file by session ID (UUID filename)."""
    # Sanitize to prevent path traversal
    _measure_facade = _compat.facade()
    safe_id = _measure_facade.sanitize_session_id(session_id)
    if safe_id == "unknown":
        return None
    if _measure_facade._use_codex_session_adapter():
        return _measure_facade.codex_session.find_session_jsonl_by_id(safe_id)

    # Hermes: no ~/.claude/projects JSONL to scan (sessions live in state.db).
    if _measure_facade._use_hermes_session_adapter():
        return None
    # Antigravity: no ~/.claude/projects JSONL (sessions live in ~/.gemini).
    if _measure_facade._use_antigravity_session_adapter():
        return None

    projects_base = _measure_facade.CLAUDE_DIR / "projects"
    if not projects_base.exists():
        return None
    for project_dir in projects_base.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / f"{safe_id}.jsonl"
        if candidate.exists():
            return candidate
    return None


def _session_id_prefix_matches(prefix):
    """``<id>.jsonl`` transcripts whose session id starts with ``prefix``.

    ``status-bar --session`` takes a pasted, possibly truncated id
    (our own listings print ``s[:8]``). A short id sanitizes to "unknown"
    and a longer prefix finds no exact file; both used to degrade silently.
    This collects every candidate so the caller can resolve a UNIQUE prefix
    and say why when the match is zero or ambiguous. ``prefix`` must already
    be reduced to the session-id charset (no glob metacharacters can sneak
    in). Never raises; returns [] for runtimes without a projects tree.
    """
    _measure_facade = _compat.facade()
    if not prefix:
        return []
    if (_measure_facade._use_codex_session_adapter() or _measure_facade._use_hermes_session_adapter()
            or _measure_facade._use_antigravity_session_adapter()):
        return []
    projects_base = _measure_facade.CLAUDE_DIR / "projects"
    if not projects_base.is_dir():
        return []
    out = []
    try:
        for project_dir in projects_base.iterdir():
            if not project_dir.is_dir():
                continue
            for p in project_dir.glob(prefix + "*.jsonl"):
                if p.is_file():
                    out.append(p)
    except OSError:
        return []
    return out


def quality_analyzer(session_id=None, as_json=None):
    """Analyze context quality of a session. Main entry point.

    Args:
        session_id: Specific session UUID, or None for most recent.
        as_json: Return JSON instead of printing.
    """
    _measure_facade = _compat.facade()
    if session_id and session_id != "current":
        filepath = _measure_facade._find_session_jsonl_by_id(session_id)
    else:
        filepath = _measure_facade._find_current_session_jsonl()

    if not filepath and (not session_id or session_id == "current") and _measure_facade._use_codex_session_adapter():
        # One resolution outcome for the invoking Codex thread: either its
        # transcript, or an explicit unresolved state with the reason. Never
        # another thread's session, never the Claude-runtime message.
        res = _measure_facade._codex_thread_resolution()
        msg = _measure_facade._codex_unresolved_message(res)
        if as_json:
            print(_measure_facade.json.dumps({"error": msg, "thread_resolution": "unresolved",
                              "reason": res["reason"], "selected_thread_id": res["thread_id"]}))
        else:
            print(f"[Token Optimizer] {msg}")
        return None

    if not filepath:
        if as_json:
            print(_measure_facade.json.dumps({"error": "No session logs found. Run a Claude Code session first."}))
        else:
            print("[Token Optimizer] No session logs found. Run a Claude Code session first.")
        return None

    quality_data = _measure_facade._parse_jsonl_for_quality(filepath)
    if not quality_data:
        if as_json:
            print(_measure_facade.json.dumps({"error": "Session log is empty or unparseable."}))
        else:
            print("[Token Optimizer] Session log is empty or unparseable.")
        return None

    result = _measure_facade.compute_quality_score(quality_data)
    result["session_file"] = _measure_facade.Path(filepath).name
    result["total_messages"] = len(quality_data["messages"])
    result["decisions_found"] = len(quality_data["decisions"])
    result["runtime"] = _measure_facade.detect_runtime()
    result["runtime_label"] = _measure_facade.runtime_name_for_humans()
    if quality_data.get("estimated"):
        result["estimated"] = True

    if as_json:
        print(_measure_facade.json.dumps(result, indent=2))
        return result

    # Pretty print
    score = result["score"]
    bd = result["breakdown"]

    # Score band
    if score >= 85:
        band = "Excellent"
    elif score >= 70:
        band = "Good"
    elif score >= 50:
        band = "Degraded"
    else:
        band = "Critical"

    # Degradation band
    cfd = bd.get("context_fill_degradation", {})
    fill_band = cfd.get("band", "")

    grade = result.get("grade", _measure_facade.score_to_grade(round(score)))

    print("\n  Context Quality Report")
    print(f"  {'=' * 40}")
    print(f"  Content quality:     {grade} ({score}/100) ({band})")
    if fill_band:
        print(f"  Degradation band:    {fill_band} ({cfd.get('fill_pct', 0):.0f}% fill, ~{cfd.get('quality_estimate', 0)}/100 MRCR)")
    top_drag = result.get("top_drag")
    if top_drag:
        print(f"  Biggest drag:        {top_drag['label']} (-{top_drag['points']} pts)")
    print(f"  Messages analyzed:   {result['total_messages']}")
    print(f"  Decisions captured:  {result['decisions_found']}")
    print()

    # Issues found
    issues = []
    if bd["stale_reads"]["count"] > 0:
        sr = bd["stale_reads"]
        tokens = sr["estimated_waste_tokens"]
        issues.append(f"  {sr['count']:3d} stale file reads    ({tokens:,} tokens est.)  files edited since reading")
    if bd["bloated_results"]["count"] > 0:
        br = bd["bloated_results"]
        tokens = br["estimated_waste_tokens"]
        issues.append(f"  {br['count']:3d} bloated results     ({tokens:,} tokens est.)  tool outputs never referenced again")
    if bd["duplicates"]["count"] > 0:
        dp = bd["duplicates"]
        tokens = dp["estimated_waste_tokens"]
        issues.append(f"  {dp['count']:3d} duplicate reminders ({tokens:,} tokens est.)  repeated system-reminder injections")
    if bd["compactions"]["count"] > 0:
        cd = bd["compactions"]
        issues.append(f"  {cd['count']:3d} compaction(s)  {bd['compaction_recovery']['detail']}")

    if issues:
        print("  Issues found:")
        for issue in issues:
            print(issue)
        print()

    # Signal-to-noise
    dd = bd["decision_density"]
    ae = bd["agent_efficiency"]
    print("  Signal-to-noise:")
    print(f"    Decision density:  {dd['ratio']} ({dd['detail']})")
    print(f"    Agent efficiency:  {ae['detail']}")
    print()

    # Recommendation
    total_waste = bd["total_estimated_waste_tokens"]
    compactions = bd["compactions"]["count"]
    if total_waste > 0:
        print("  Recommendation:")
        print(f"    /compact would free ~{total_waste:,} tokens of low-value content")
        if score < 70:
            print("    Consider /clear with checkpoint if quality below 50")
        if result["decisions_found"] > 0:
            print(f"    Smart Compact checkpoint would preserve {result['decisions_found']} decision(s)")
    elif score >= 85:
        print("  Session is clean. No action needed.")

    # Cache preservation tip when compactions detected
    if compactions > 0:
        print("  Cache impact:")
        print(f"    {compactions} compaction(s) triggered full cache rebuilds this session.")
        print("    Each rebuild re-bills all context at full input price (not cached 10% rate).")
        if bd["bloated_results"]["count"] > 0:
            print(f"    {bd['bloated_results']['count']} bloated tool results detected. For API users: Anthropic's")
            print("    Context Editing API (clear_tool_uses) can evict stale results WITHOUT")
            print("    triggering compaction, preserving your cache prefix.")
        print("    To reduce compactions: keep context lean, use Smart Compaction to")
        print("    preserve state when compaction does fire.")

    # Phase-boundary compaction timing guide
    if compactions > 0 or (total_waste > 5000 and score < 80):
        print()
        print("  When to compact (timing matters for cache preservation):")
        print("    After research/exploration, before execution  -- bulky context, plan is the output")
        print("    After debugging, before next feature           -- debug traces pollute unrelated work")
        print("    After a failed approach, before retrying        -- clear dead-end reasoning")
        print("    After completing a milestone (commit/merge)     -- natural checkpoint, fresh start")
        print("    NOT mid-implementation                          -- losing file paths and partial state is costly")
        print("    NOT mid-debugging                               -- losing hypothesis state forces re-investigation")
        print("    NOT during multi-step operations                -- breaks continuity across related steps")
    print()

    return result


def _collect_quality_for_dashboard():
    """Collect quality data for dashboard embedding. Returns dict or None."""
    _measure_facade = _compat.facade()
    try:
        filepath = _measure_facade._find_current_session_jsonl()
        if not filepath:
            return None
        quality_data = _measure_facade._parse_jsonl_for_quality(filepath)
        if not quality_data:
            return None
        result = _measure_facade.compute_quality_score(quality_data)
        result["total_messages"] = len(quality_data["messages"])
        result["decisions_found"] = len(quality_data["decisions"])
        result["runtime"] = _measure_facade.detect_runtime()
        result["runtime_label"] = _measure_facade.runtime_name_for_humans()
        result["session_file"] = _measure_facade.Path(filepath).name
        if quality_data.get("estimated"):
            result["estimated"] = True
        return result
    except Exception:
        return None


def _extract_tool_result_text(block):
    """Extract text content from a tool_result content block.

    Handles both string and list content formats. Used by quality parsing,
    jsonl_inspect, jsonl_trim, and _jsonl_record_text_size.
    """
    _measure_facade = _compat.facade()
    rc = block.get("content", "")
    if isinstance(rc, list):
        return " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in rc
        )
    return str(rc)


def _tool_result_looks_failed(block, result_text):
    """Return True only for result blocks that carry a concrete failure signal."""
    _measure_facade = _compat.facade()
    if block.get("is_error") is True:
        return True
    text = (result_text or "").strip()
    if not text:
        return False
    if _measure_facade._TOOL_SUCCESS_COUNT_RE.search(text) and not _measure_facade._TOOL_NONZERO_FAILURE_COUNT_RE.search(text):
        return False
    return bool(_measure_facade._TOOL_FAILURE_RE.search(text[:2000]))


def _resolve_jsonl_path(arg=None):
    """Resolve a JSONL file path from a session ID, file path, or auto-detect.

    Returns (Path, error_string). On success error_string is None.
    """
    _measure_facade = _compat.facade()
    if arg and not arg.startswith("--"):
        p = _measure_facade.Path(arg)
        if p.exists() and p.suffix == ".jsonl":
            return p, None
        # Treat as session ID
        found = _measure_facade._find_session_jsonl_by_id(arg)
        if found:
            return found, None
        return None, f"Session '{arg}' not found."
    # Auto-detect
    found = _measure_facade._find_current_session_jsonl()
    if found:
        return found, None
    if _measure_facade._use_codex_session_adapter():
        return None, _measure_facade._codex_unresolved_message(_measure_facade._codex_thread_resolution())
    return None, "No active session found. Provide a session ID or path."


def _jsonl_record_text_size(record):
    """Return total character count of meaningful text in a record."""
    _measure_facade = _compat.facade()
    rec_type = record.get("type", "")
    total = 0

    if rec_type == "user":
        text = _measure_facade._extract_user_text(record)
        total += len(text)
    elif rec_type == "assistant":
        msg = record.get("message", {})
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        total += len(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        total += len(_measure_facade.json.dumps(block.get("input", {})))
    elif rec_type == "system":
        total += len(str(record.get("message", "")))
    # tool_result records embedded in user messages (skip for user records to avoid double-counting)
    if rec_type != "user":
        msg = record.get("message", {})
        if isinstance(msg, dict):
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        total += len(_measure_facade._extract_tool_result_text(block))
    return total


def _classify_record(record):
    """Classify a JSONL record into a category string.

    Returns one of: 'user', 'assistant', 'system', 'system_reminder',
    'tool_result', 'compact_boundary', 'unknown'.
    """
    _measure_facade = _compat.facade()
    rec_type = record.get("type", "")
    if rec_type == "system":
        msg_content = str(record.get("message", ""))
        if record.get("subtype") == "compact_boundary" or "compactMetadata" in record:
            return "compact_boundary"
        if "system-reminder" in msg_content:
            return "system_reminder"
        return "system"
    if rec_type == "user":
        # Check if it contains tool_result blocks
        msg = record.get("message", {})
        if isinstance(msg, dict):
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        return "tool_result"
        return "user"
    if rec_type == "assistant":
        return "assistant"
    return rec_type or "unknown"


def jsonl_inspect(arg=None, as_json=None):
    """Inspect a JSONL session file and print stats."""
    _measure_facade = _compat.facade()
    filepath, err = _measure_facade._resolve_jsonl_path(arg)
    if err:
        if as_json:
            print(_measure_facade.json.dumps({"error": err}))
        else:
            print(f"[Error] {err}")
        return

    file_size = filepath.stat().st_size

    counts_by_type = {}
    total_records = 0
    compaction_count = 0
    largest_records = []  # (index, char_count, category, line_preview)
    tool_result_chars = 0
    message_chars = 0
    system_reminder_chars = 0
    system_reminder_hashes = []

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for idx, line in enumerate(f):
                try:
                    record = _measure_facade.json.loads(line)
                except _measure_facade.json.JSONDecodeError:
                    continue

                if not isinstance(record, dict):
                    continue
                total_records += 1
                category = _measure_facade._classify_record(record)
                counts_by_type[category] = counts_by_type.get(category, 0) + 1

                if category == "compact_boundary":
                    compaction_count += 1

                char_count = _measure_facade._jsonl_record_text_size(record)

                # Track distribution
                if category == "tool_result":
                    tool_result_chars += char_count
                elif category == "system_reminder":
                    system_reminder_chars += char_count
                    content_hash = _measure_facade.hashlib.sha256(str(record.get("message", "")).encode()).hexdigest()[:16]
                    system_reminder_hashes.append(content_hash)
                elif category in ("user", "assistant", "system"):
                    message_chars += char_count

                # Track largest (min-heap of top 10)
                entry = (char_count, idx, category)
                if len(largest_records) < 10:
                    _measure_facade.heapq.heappush(largest_records, entry)
                elif char_count > largest_records[0][0]:
                    _measure_facade.heapq.heapreplace(largest_records, entry)

    except (PermissionError, OSError) as e:
        if as_json:
            print(_measure_facade.json.dumps({"error": str(e)}))
        else:
            print(f"[Error] Cannot read file: {e}")
        return

    # Sort top 10 largest (heap entries are (char_count, idx, category))
    top10 = sorted(largest_records, reverse=True)

    total_chars = tool_result_chars + message_chars + system_reminder_chars
    est_tokens = int(total_chars / _measure_facade.CHARS_PER_TOKEN)

    # Duplicate system reminders
    seen_hashes = set()
    dup_reminder_count = 0
    for h in system_reminder_hashes:
        if h in seen_hashes:
            dup_reminder_count += 1
        seen_hashes.add(h)

    result = {
        "file": str(filepath),
        "file_size_bytes": file_size,
        "total_records": total_records,
        "estimated_tokens": est_tokens,
        "counts_by_type": counts_by_type,
        "compaction_markers": compaction_count,
        "token_distribution": {
            "tool_results": int(tool_result_chars / _measure_facade.CHARS_PER_TOKEN),
            "messages": int(message_chars / _measure_facade.CHARS_PER_TOKEN),
            "system_reminders": int(system_reminder_chars / _measure_facade.CHARS_PER_TOKEN),
        },
        "duplicate_system_reminders": dup_reminder_count,
        "top_10_largest": [
            {"index": r[1], "chars": r[0], "type": r[2], "est_tokens": int(r[0] / _measure_facade.CHARS_PER_TOKEN)}
            for r in top10
        ],
    }

    if as_json:
        print(_measure_facade.json.dumps(result, indent=2))
        return

    # Pretty print
    print("\n  JSONL Session Inspector")
    print(f"  {'=' * 50}")
    print(f"  File: {filepath}")
    print(f"  Size: {file_size:,} bytes ({file_size / 1024:.1f} KB)")
    print(f"  Records: {total_records:,}")
    print(f"  Estimated tokens: {est_tokens:,}")
    print()

    print("  Record counts by type:")
    for rtype, count in sorted(counts_by_type.items(), key=lambda x: ((_measure_facade := _compat.facade()), -x[1])[1]):
        print(f"    {rtype:25s} {count:6,}")
    print()

    print("  Token distribution:")
    for label, tokens in result["token_distribution"].items():
        pct = (tokens / est_tokens * 100) if est_tokens > 0 else 0
        bar = "#" * int(pct / 2)
        print(f"    {label:25s} {tokens:8,} tokens ({pct:5.1f}%)  {bar}")
    print()

    if compaction_count > 0:
        print(f"  Compaction markers: {compaction_count}")
    if dup_reminder_count > 0:
        print(f"  Duplicate system reminders: {dup_reminder_count} (waste)")
    print()

    if top10:
        print("  Top 10 largest records:")
        print(f"    {'Index':>8s}  {'Type':>20s}  {'Chars':>10s}  {'~Tokens':>8s}")
        print(f"    {'-' * 8}  {'-' * 20}  {'-' * 10}  {'-' * 8}")
        for r in top10:
            print(f"    {r[1]:>8,}  {r[2]:>20s}  {r[0]:>10,}  {int(r[0] / _measure_facade.CHARS_PER_TOKEN):>8,}")
    print()


def jsonl_trim(arg=None, apply=None, threshold=None):
    """Trim large tool_result content from historical JSONL records.

    Default is dry-run. Pass apply=True to actually modify.
    Threshold is in characters (default 4000, roughly 1000 tokens).
    """
    _measure_facade = _compat.facade()
    filepath, err = _measure_facade._resolve_jsonl_path(arg)
    if err:
        print(f"[Error] {err}")
        return

    # First pass: count what would be trimmed
    trimmable = []  # (line_index, tool_use_id, original_size, est_tokens)

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for idx, line in enumerate(f):
                try:
                    record = _measure_facade.json.loads(line)
                except _measure_facade.json.JSONDecodeError:
                    continue

                msg = record.get("message", {})
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue

                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_result":
                        continue
                    result_text = _measure_facade._extract_tool_result_text(block)

                    if len(result_text) > threshold:
                        tool_id = block.get("tool_use_id", "unknown")
                        est_tok = int(len(result_text) / _measure_facade.CHARS_PER_TOKEN)
                        trimmable.append((idx, tool_id, len(result_text), est_tok))

    except (PermissionError, OSError) as e:
        print(f"[Error] Cannot read file: {e}")
        return

    if not trimmable:
        print(f"[Token Optimizer] No tool results exceed {threshold} chars. Nothing to trim.")
        return

    total_chars_saved = sum(t[2] for t in trimmable)
    total_tokens_saved = int(total_chars_saved / _measure_facade.CHARS_PER_TOKEN)

    print(f"\n  JSONL Trim {'(DRY RUN)' if not apply else '(APPLYING)'}")
    print(f"  {'=' * 50}")
    print(f"  File: {filepath}")
    print(f"  Threshold: {threshold:,} chars (~{int(threshold / _measure_facade.CHARS_PER_TOKEN):,} tokens)")
    print(f"  Trimmable tool results: {len(trimmable)}")
    print(f"  Total chars to trim: {total_chars_saved:,}")
    print(f"  Estimated token savings: {total_tokens_saved:,}")
    print()

    # Show top 5 largest trimmable
    sorted_trim = sorted(trimmable, key=lambda x: ((_measure_facade := _compat.facade()), -x[2])[1])[:5]
    print("  Top trimmable records:")
    print(f"    {'Line':>8s}  {'Tool ID':>20s}  {'Chars':>10s}  {'~Tokens':>8s}")
    print(f"    {'-' * 8}  {'-' * 20}  {'-' * 10}  {'-' * 8}")
    for t in sorted_trim:
        tid = t[1][:20] if len(t[1]) > 20 else t[1]
        print(f"    {t[0]:>8,}  {tid:>20s}  {t[2]:>10,}  {t[3]:>8,}")
    print()

    if not apply:
        print("  This is a dry run. Use --apply to trim.")
        print()
        return

    # Apply: create backup, write sidecar, stream-modify
    import shutil
    ts = _measure_facade.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = _measure_facade.Path(str(filepath) + f".{ts}.bak")
    sidecar_path = _measure_facade.Path(str(filepath).replace(".jsonl", ".trimmed.jsonl"))

    # Backup
    shutil.copy2(filepath, backup_path)
    print(f"  Backup saved: {backup_path}")

    # Build set of trimmable line indices for fast lookup
    trim_lines = set(t[0] for t in trimmable)

    # Stream: read original, write modified to temp, then atomic replace
    tmp_fd, tmp_path = _measure_facade.tempfile.mkstemp(suffix=".jsonl", dir=str(filepath.parent))
    sidecar_entries = []
    trimmed_count = 0

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as fin, \
             _measure_facade.os.fdopen(tmp_fd, "w", encoding="utf-8") as fout:
            for idx, line in enumerate(fin):
                if idx not in trim_lines:
                    fout.write(line)
                    continue

                try:
                    record = _measure_facade.json.loads(line)
                except _measure_facade.json.JSONDecodeError:
                    fout.write(line)
                    continue

                msg = record.get("message", {})
                if not isinstance(msg, dict):
                    fout.write(line)
                    continue
                content = msg.get("content", [])
                if not isinstance(content, list):
                    fout.write(line)
                    continue

                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_result":
                        continue
                    result_text = _measure_facade._extract_tool_result_text(block)

                    if len(result_text) > threshold:
                        tool_id = block.get("tool_use_id", "unknown")
                        est_tok = int(len(result_text) / _measure_facade.CHARS_PER_TOKEN)

                        # Save original to sidecar
                        sidecar_entries.append({
                            "record_index": idx,
                            "tool_use_id": tool_id,
                            "original_chars": len(result_text),
                            "original_content": block.get("content", ""),
                        })

                        # Replace content with placeholder
                        block["content"] = f"[trimmed - {len(result_text)} chars, {est_tok} tokens]"
                        trimmed_count += 1

                fout.write(_measure_facade.json.dumps(record) + "\n")

        # Atomic replace
        _measure_facade.os.replace(tmp_path, filepath)

        # Write sidecar
        with open(sidecar_path, "w", encoding="utf-8") as sf:
            for entry in sidecar_entries:
                sf.write(_measure_facade.json.dumps(entry) + "\n")

        print(f"  Trimmed {trimmed_count} tool results.")
        print(f"  Sidecar saved: {sidecar_path}")
        print(f"  Estimated tokens recovered: {total_tokens_saved:,}")
        print()

    except Exception as e:
        # Clean up temp file on failure
        try:
            _measure_facade.os.unlink(tmp_path)
        except OSError:
            pass
        print(f"[Error] Trim failed: {e}")
        print(f"  Original file is unchanged (backup at {backup_path})")


def jsonl_dedup(arg=None, apply=None):
    """Detect and remove duplicate system_reminder injections from JSONL.

    Default is dry-run. Pass apply=True to actually modify.
    """
    _measure_facade = _compat.facade()
    filepath, err = _measure_facade._resolve_jsonl_path(arg)
    if err:
        print(f"[Error] {err}")
        return

    # First pass: find duplicates
    seen_hashes = {}  # hash -> first line index
    duplicates = []   # (line_index, content_hash, char_count, est_tokens)

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for idx, line in enumerate(f):
                try:
                    record = _measure_facade.json.loads(line)
                except _measure_facade.json.JSONDecodeError:
                    continue

                rec_type = record.get("type", "")
                if rec_type != "system":
                    continue

                msg_content = str(record.get("message", ""))
                if "system-reminder" not in msg_content:
                    continue

                content_hash = _measure_facade.hashlib.sha256(msg_content.encode()).hexdigest()[:16]
                char_count = len(msg_content)
                est_tok = int(char_count / _measure_facade.CHARS_PER_TOKEN)

                if content_hash in seen_hashes:
                    duplicates.append((idx, content_hash, char_count, est_tok))
                else:
                    seen_hashes[content_hash] = idx

    except (PermissionError, OSError) as e:
        print(f"[Error] Cannot read file: {e}")
        return

    total_waste_chars = sum(d[2] for d in duplicates)
    total_waste_tokens = int(total_waste_chars / _measure_facade.CHARS_PER_TOKEN)

    print(f"\n  JSONL Dedup {'(DRY RUN)' if not apply else '(APPLYING)'}")
    print(f"  {'=' * 50}")
    print(f"  File: {filepath}")
    print(f"  Unique system reminders: {len(seen_hashes)}")
    print(f"  Duplicate injections: {len(duplicates)}")
    print(f"  Estimated waste: {total_waste_chars:,} chars (~{total_waste_tokens:,} tokens)")
    print()

    if not duplicates:
        print("  No duplicate system reminders found. File is clean.")
        print()
        return

    # Group duplicates by hash for reporting
    dup_by_hash = {}
    for d in duplicates:
        dup_by_hash.setdefault(d[1], []).append(d)

    print("  Duplicate groups:")
    for h, dups in sorted(dup_by_hash.items(), key=lambda x: ((_measure_facade := _compat.facade()), -sum(d[2] for d in x[1]))[1]):
        first_idx = seen_hashes[h]
        waste = sum(d[2] for d in dups)
        print(f"    Hash {h}: first at line {first_idx}, {len(dups)} duplicate(s), ~{int(waste / _measure_facade.CHARS_PER_TOKEN):,} wasted tokens")
    print()

    if not apply:
        print("  This is a dry run. Use --apply to remove duplicates.")
        print()
        return

    # Apply: backup, stream, remove duplicate lines
    import shutil
    ts = _measure_facade.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = _measure_facade.Path(str(filepath) + f".{ts}.bak")
    shutil.copy2(filepath, backup_path)
    print(f"  Backup saved: {backup_path}")

    dup_line_indices = set(d[0] for d in duplicates)

    tmp_fd, tmp_path = _measure_facade.tempfile.mkstemp(suffix=".jsonl", dir=str(filepath.parent))
    removed_count = 0

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as fin, \
             _measure_facade.os.fdopen(tmp_fd, "w", encoding="utf-8") as fout:
            for idx, line in enumerate(fin):
                if idx in dup_line_indices:
                    removed_count += 1
                    continue
                fout.write(line)

        _measure_facade.os.replace(tmp_path, filepath)
        print(f"  Removed {removed_count} duplicate system reminders.")
        print(f"  Estimated tokens recovered: {total_waste_tokens:,}")
        print()

    except Exception as e:
        try:
            _measure_facade.os.unlink(tmp_path)
        except OSError:
            pass
        print(f"[Error] Dedup failed: {e}")
        print(f"  Original file is unchanged (backup at {backup_path})")


def _archive_dir_for_session(session_id):
    """Return the archive directory for a given session, or None if ID is invalid."""
    _measure_facade = _compat.facade()
    sid = _measure_facade.sanitize_session_id(session_id)
    if sid == "unknown":
        return None
    return _measure_facade.SNAPSHOT_DIR / "tool-archive" / sid


def _ensure_private_dir(path):
    _measure_facade = _compat.facade()
    path.mkdir(parents=True, exist_ok=True)
    try:
        _measure_facade.os.chmod(str(path), 0o700)
    except OSError:
        pass
    return path


def archive_result(quiet=None):
    """Compatibility wrapper for the canonical standalone PostToolUse handler."""
    _measure_facade = _compat.facade()
    try:
        from archive_result import archive_result as archive_result_main
    except Exception as exc:
        if not quiet:
            print(f"[Tool Archive] Failed to load standalone archive_result.py: {exc}", file=_measure_facade.sys.stderr)
        return
    archive_result_main(quiet=quiet)


def _sanitize_tool_use_id(tool_use_id):
    """Archive key for a tool_use_id: the same key the PostToolUse hook uses.

    Ids outside [a-zA-Z0-9_-] (or longer than 128) map to a digest, so two
    different ids never share a key the way "a b", "a.b" and "a_b" used to.
    """
    _measure_facade = _compat.facade()
    raw = str(tool_use_id or "")
    if not raw or raw == "unknown":
        return _measure_facade.hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]
    from archive_result import _safe_archive_key
    return _safe_archive_key(raw)


def _summarize_tool_output_for_recovery(text):
    """Small, deterministic summary for checkpoint/session-store pointers."""
    _measure_facade = _compat.facade()
    raw = str(text or "")
    if _measure_facade.re.search(r"\b(error|failed|traceback|exception|permission denied|not found)\b", raw[:20_000], _measure_facade.re.IGNORECASE):
        return "Large tool output archived; contains error/failure signals."
    return "Large tool output archived."


def _archived_response_hash(entry_path):
    """sha256 of the stored response of an archive entry, or None if unreadable."""
    _measure_facade = _compat.facade()
    try:
        data = _measure_facade.json.loads(_measure_facade.Path(entry_path).read_text(encoding="utf-8"))
        return _measure_facade.hashlib.sha256(
            str(data.get("response") or "").encode("utf-8", errors="replace")).hexdigest()
    except Exception:
        return None


def _codex_backfill_tool_archive(filepath=None, session_id=None, max_outputs=None):
    """Backfill durable tool-output pointers from Codex JSONL.

    Claude gets PostToolUse archive hooks. Codex balanced mode deliberately
    avoids noisy per-tool hooks, so the Stop worker scans the bounded transcript
    and archives large/high-signal outputs into the same on-disk archive and
    SessionStore. Duplicate writes are skipped by filename and SQLite keys.
    Backfill is durability metadata, not token savings, because the original
    output already entered the transcript.
    """
    _measure_facade = _compat.facade()
    if not _measure_facade._use_codex_session_adapter(filepath):
        return 0
    path = _measure_facade.Path(filepath) if filepath else _measure_facade._find_current_session_jsonl()
    if not path or not path.exists():
        return 0

    sid = _measure_facade.sanitize_session_id(session_id or path.stem)
    archive_dir = _measure_facade._archive_dir_for_session(sid)
    if not archive_dir:
        return 0
    # No persistence without the shared redactor — tool output is the single
    # most likely place a credential lands in a transcript.
    try:
        from credential_patterns import redact_credentials as _bf_redact
    except Exception:
        return 0
    archived = 0
    try:
        outputs = _measure_facade.codex_session.iter_tool_outputs(
            path,
            min_chars=_measure_facade._ARCHIVE_THRESHOLD,
            max_outputs=max_outputs,
        )
    except Exception:
        return 0

    store = None
    try:
        from session_store import SessionStore
        store = SessionStore(sid)
    except Exception:
        store = None

    try:
        for item in outputs:
            output_text = str(item.get("output") or "")
            if not output_text:
                continue
            tool_use_id = _measure_facade._sanitize_tool_use_id(item.get("tool_use_id"))
            _measure_facade._ensure_private_dir(archive_dir)
            entry_path = archive_dir / f"{tool_use_id}.json"

            over_cap = len(output_text) > 5_242_880

            try:
                # Redact BEFORE hashing/summarizing/persisting so the archive
                # entry, manifest, and SessionStore row all carry the same
                # safe bytes. A redactor refusal (broken custom pattern
                # config) skips this output rather than storing it raw.
                # Redact a window past the cap and cut AFTER: a secret
                # straddling the cap is otherwise a prefix no pattern sees.
                output_text = _bf_redact(output_text[:5_242_880 + 4096] if over_cap else output_text)
            except Exception:
                continue
            if over_cap:
                output_text = output_text[:5_242_880] + "\n[... truncated by Token Optimizer archive cap]"

            char_count = len(output_text)
            token_est = int(char_count / _measure_facade.CHARS_PER_TOKEN)
            tool_name = str(item.get("tool_name") or "Tool")
            tool_type = str(item.get("tool_type") or "codex")
            try:
                command_or_path = _bf_redact(str(item.get("command_or_path") or ""), command=True)
            except Exception:
                continue
            output_hash = _measure_facade.hashlib.sha256(output_text.encode("utf-8", errors="replace")).hexdigest()
            if entry_path.exists():
                # Same id, same payload: already archived. Same id, different
                # payload: keep both, under a content-qualified key.
                if _measure_facade._archived_response_hash(entry_path) == output_hash:
                    continue
                tool_use_id = f"{tool_use_id[:100]}-{output_hash[:12]}"
                entry_path = archive_dir / f"{tool_use_id}.json"
                if entry_path.exists() and _measure_facade._archived_response_hash(entry_path) == output_hash:
                    continue
            summary = _measure_facade._summarize_tool_output_for_recovery(output_text)
            entry_data = {
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "chars": char_count,
                "tokens_est": token_est,
                "timestamp": item.get("timestamp") or _measure_facade.datetime.now(_measure_facade.timezone.utc).isoformat(),
                "archived_from": "codex-session-backfill",
                "command_or_path": command_or_path,
                "summary": summary,
                "response": output_text,
            }
            fd = _measure_facade.os.open(str(entry_path), _measure_facade.os.O_WRONLY | _measure_facade.os.O_CREAT | _measure_facade.os.O_TRUNC, 0o600)
            with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as f:
                _measure_facade.json.dump(entry_data, f)

            manifest_entry = {
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "chars": char_count,
                "tokens_est": token_est,
                "timestamp": entry_data["timestamp"],
                "archived_from": "codex-session-backfill",
                "path": str(entry_path),
                "summary": summary,
            }
            fd = _measure_facade.os.open(str(archive_dir / "manifest.jsonl"), _measure_facade.os.O_WRONLY | _measure_facade.os.O_CREAT | _measure_facade.os.O_APPEND, 0o600)
            with _measure_facade.os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(_measure_facade.json.dumps(manifest_entry) + "\n")

            if store is not None:
                try:
                    store.insert_tool_output(
                        tool_use_id,
                        tool_name,
                        tool_type,
                        command_or_path,
                        output_hash,
                        char_count,
                        token_est,
                        compressed_preview=summary,
                        # LOW fix: make the backfilled archive full-text
                        # searchable and give it the same lineage tag the JSON
                        # entry + manifest already carry. output_text=summary
                        # (not the full response) keeps the session DB lean
                        # while still discoverable via search.
                        output_text=summary,
                        archived_from="codex-session-backfill",
                        truncated=1 if over_cap else 0,
                    )
                    store.insert_intel_event(tool_name, tool_use_id, summary, char_count)
                except Exception:
                    pass
            archived += 1
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass

    return archived


def _merge_char_ranges(ranges):
    """Sort + merge overlapping/adjacent (start, end) char ranges."""
    _measure_facade = _compat.facade()
    if not ranges:
        return []
    rs = sorted((min(int(s), int(e)), max(int(s), int(e))) for s, e in ranges)
    out = [rs[0]]
    for s, e in rs[1:]:
        if s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _subtract_char_ranges(ranges, covered):
    """The parts of ``ranges`` not already inside merged ``covered`` ranges."""
    _measure_facade = _compat.facade()
    out = []
    for s, e in _measure_facade._merge_char_ranges(ranges):
        cur = s
        for cs, ce in covered:
            if ce <= cur:
                continue
            if cs >= e:
                break
            if cs > cur:
                out.append((cur, min(cs, e)))
            cur = max(cur, ce)
            if cur >= e:
                break
        if cur < e:
            out.append((cur, e))
    return _measure_facade._merge_char_ranges(out)


def _reexpand_detail_ranges(detail, total_chars):
    """Char ranges a prior re-expansion debit covered, or None for a legacy row.

    Legacy rows (``key`` / ``key (tool)`` with no `` chars `` spec) re-expanded
    the WHOLE stored response, so they count as full coverage. Bounded rows
    carry `` chars s-e[,s-e...]`` — the ranges actually returned.
    """
    _measure_facade = _compat.facade()
    m = _measure_facade._REEXPAND_RANGES_RE.search(detail or "")
    if not m:
        return None
    out = []
    for part in m.group(1).split(","):
        s, e = part.split("-", 1)
        s, e = int(s), int(e)
        s = max(0, min(s, total_chars))
        e = max(0, min(e, total_chars))
        if e > s:
            out.append((s, e))
    return _measure_facade._merge_char_ranges(out) or None


def _log_reexpand_debit(session_id, tool_use_id, response_text, tool_name=None,
                        content_ranges=None):
    """Record a tool_archive re-expansion as a debit (B6 integrity).

    An archived tool result that gets re-popped into context did not stay
    collapsed, so its eager `tool_archive` credit must be reversed. We log the
    re-popped token estimate under `tool_archive_reexpand`; `_get_savings_summary`
    treats that category as a DEBIT against `tool_archive` (floored at 0), never
    as its own positive savings line.

    ``content_ranges`` carries the (start, end) char offsets of ``response_text``
    that were actually returned — a bounded ``expand --lines/--chars/--match``
    debits only those tokens, while a full expand (``None``) asks for the whole
    response. A debit is logged only for characters no earlier expansion already
    covered, so overlapping windows and full-then-bounded reads cannot reverse
    more than the item once. Legacy rows without a `` chars `` spec count as full
    coverage. The detail records the range set actually debited. Never raises.
    """
    _measure_facade = _compat.facade()
    try:
        total_chars = len(response_text or "")
        if total_chars <= 0:
            return
        if content_ranges is None:
            wanted = [(0, total_chars)]
            requested_bounded = False
        else:
            wanted = []
            for s, e in content_ranges:
                s = max(0, min(int(s), total_chars))
                e = max(0, min(int(e), total_chars))
                if e > s:
                    wanted.append((s, e))
            requested_bounded = True
        wanted = _measure_facade._merge_char_ranges(wanted)
        if not wanted:
            return
        sid = _measure_facade.sanitize_session_id(session_id) if session_id else None
        from . import mcp_archive_tripwire
        if '__' in (tool_name or ''): mcp_archive_tripwire.record_expand(_measure_facade.TRENDS_DB, sid, tool_use_id)
        covered = []
        if _measure_facade.TRENDS_DB.exists():
            conn = _measure_facade._init_trends_db()
            try:
                rows = conn.execute(
                    "SELECT detail FROM savings_events WHERE event_type='tool_archive_reexpand' "
                    "AND (session_id = ? OR (? IS NULL AND session_id IS NULL)) "
                    "AND (detail = ? OR substr(detail, 1, ?) = ? OR substr(detail, 1, ?) = ?)",
                    (sid, sid, tool_use_id, len(tool_use_id) + 2, tool_use_id + " (",
                     len(tool_use_id) + 1, tool_use_id + " "),
                ).fetchall()
            finally:
                conn.close()
            for (prior_detail,) in rows:
                prior = _measure_facade._reexpand_detail_ranges(prior_detail, total_chars)
                # A legacy row covers everything; bounded rows cover their ranges.
                if prior is None:
                    covered = [(0, total_chars)]
                    break
                covered.extend(prior)
            covered = _measure_facade._merge_char_ranges(covered)
        debited = _measure_facade._subtract_char_ranges(wanted, covered)
        if not debited:
            return
        fresh_text = "".join(
            response_text[s:e] for s, e in debited)
        tokens = _measure_facade._estimate_tokens(fresh_text)
        if tokens <= 0:
            return
        # The tool name lets the counted views treat a media re-expansion the
        # same way as its media archive row.
        name = _measure_facade.re.sub(r"[^A-Za-z0-9_.:-]", "", str(tool_name or ""))[:120]
        detail = f"{tool_use_id} ({name})" if name else tool_use_id
        if requested_bounded or debited != wanted:
            detail += " chars " + ",".join(f"{s}-{e}" for s, e in debited)
        _measure_facade._log_savings_event("tool_archive_reexpand", tokens, session_id=sid, detail=detail)
    except Exception:
        pass


def _find_archive_entry(archive_roots, tool_use_id, session_id=None, strict=None):
    """First ``<root>/<session>/<tool_use_id>.json`` that exists, or None.

    *tool_use_id* must already have passed expand's ``[a-zA-Z0-9_-]+`` check, so
    the filename can never leave its session dir. *strict* (the cross-runtime
    fallback) additionally requires a regular, non-symlink file whose resolved
    path stays inside its archive root.
    """
    _measure_facade = _compat.facade()
    for archive_root in archive_roots:
        if session_id:
            sid = _measure_facade.sanitize_session_id(session_id)
            if sid == "unknown":
                continue
            session_dirs = [archive_root / sid]
        elif archive_root.is_dir() and not archive_root.is_symlink():
            try:
                session_dirs = [d for d in archive_root.iterdir()
                                if d.is_dir() and not d.is_symlink()]
            except OSError:
                continue
        else:
            continue
        for sd in session_dirs:
            entry_path = sd / f"{tool_use_id}.json"
            try:
                if not entry_path.exists():
                    continue
                if strict:
                    if (archive_root.is_symlink() or sd.is_symlink()
                            or entry_path.is_symlink() or not entry_path.is_file()):
                        continue
                    entry_path.resolve(strict=True).relative_to(archive_root.resolve(strict=True))
            except (OSError, ValueError):
                continue
            return entry_path
    return None


def _expand_may_cross_runtimes():
    """True for a bare expand: no --runtime / TOKEN_OPTIMIZER_RUNTIME pin and
    no sandbox snapshot override."""
    _measure_facade = _compat.facade()
    return not (_measure_facade.os.environ.get("TOKEN_OPTIMIZER_RUNTIME", "").strip()
                or _measure_facade.os.environ.get("TOKEN_OPTIMIZER_SNAPSHOT_DIR", "").strip())


def _cross_runtime_archive_roots(exclude):
    """tool-archive dirs under every other runtime's home. Never raises."""
    _measure_facade = _compat.facade()
    roots = []
    seen = set(exclude)
    try:
        from runtime_env import runtime_home_for
        from plugin_env import snapshot_roots_for_home
    except Exception:
        return roots
    for runtime in _measure_facade._EXPAND_FALLBACK_RUNTIMES:
        try:
            home = runtime_home_for(runtime)
            for snapshot_root in snapshot_roots_for_home(home):
                root = snapshot_root / "tool-archive"
                if root not in seen:
                    seen.add(root)
                    roots.append(root)
        except Exception:
            continue
    return roots


def _expand_window_command(tool_use_id, session_dir_name, args=None):
    """A runnable expand command for one bounded window of an archive entry.

    Built on expand_command() — the same single source the archive pointer
    uses — with the entry's own session pinned so the command resolves the
    same archive the current lookup found (including cross-runtime fallback).
    The session dir name goes through shell_path(): archive roots are scanned
    by directory, so a nonstandard dir name must not leak shell syntax into a
    printed command.
    """
    _measure_facade = _compat.facade()
    from refetch_fingerprint import expand_command, shell_path
    cmd = (expand_command(tool_use_id)
           + f" --session {shell_path(str(session_dir_name))}")
    return cmd + (f" {args}" if args else "")


def _expand_hit_header(data, tool_use_id, session_dir_name, response, total_lines):
    """One-line provenance header for a bounded expand: where the hit lives,
    when it was captured, its hash, total length and stored-truncation state."""
    _measure_facade = _compat.facade()
    tool_name = data.get("tool_name") or "?"
    origin = data.get("archived_from") or "?"
    captured = str(data.get("timestamp") or "?")
    digest = _measure_facade.hashlib.sha256(
        response.encode("utf-8", errors="replace")).hexdigest()[:16]
    stored_truncated = "yes" if data.get("truncated") else "no"
    bits = [
        f"expand {tool_use_id}",
        f"session={session_dir_name}",
        f"tool={tool_name}",
        f"origin={origin}",
        f"captured={captured}",
        f"sha256:{digest}",
        f"total={len(response)} chars / {total_lines} lines",
        f"stored_truncated={stored_truncated}",
    ]
    original = data.get("original_chars")
    if original and int(original) != len(response):
        bits.append(f"original={original} chars")
    src = []
    seen_labels = set()
    for key, label in (("file_path", "file"), ("source_file_path", "file"),
                       ("command_or_path", "cmd")):
        value = data.get(key)
        if value and label not in seen_labels:
            seen_labels.add(label)
            src.append(f"{label}={str(value)[:80]}")
    if src:
        bits.append("src=" + ",".join(src))
    return "[" + " | ".join(bits) + "]"


def _expand_bounded(data, response, tool_use_id, session_dir_name, spec):
    """Render a bounded window of an archived response.

    Returns ``(text, ranges)``: the printable output and the (start, end) char
    offsets of ``response`` it contains, for the re-expansion debit. SystemExit
    on an unsatisfiable spec (bad range, no match) — nothing was returned, so
    no debit is owed.
    """
    _measure_facade = _compat.facade()
    from archive_window import SpecError, char_window, line_window, match_windows
    total_chars = len(response)
    try:
        if spec.kind == "chars":
            windows = [char_window(response, spec.start, spec.chars)]
            group_total = 0
        elif spec.kind == "lines":
            windows = [line_window(response, spec.lines)]
            group_total = 0
        else:
            windows, group_total = match_windows(
                response, spec.pattern, context=spec.context, skip=spec.skip)
    except SpecError as exc:
        print(f"[Error] {exc}", file=_measure_facade.sys.stderr)
        _measure_facade.sys.exit(1)

    ranges = [(w.start, w.end) for w in windows]
    if spec.kind == "match" and not windows:
        if group_total == 0:
            detail = f"[no matches for /{spec.pattern}/ in {total_chars} chars]"
        else:
            detail = (f"[--skip {spec.skip} is past the last match group "
                      f"({group_total} total)]")
        header = _measure_facade._expand_hit_header(data, tool_use_id, session_dir_name,
                                    response, _measure_facade._aw_line_count(response))
        full_cmd = _measure_facade._expand_window_command(tool_use_id, session_dir_name)
        return f"{header}\n{detail}\n[full: {full_cmd}]\n", []

    header = _measure_facade._expand_hit_header(data, tool_use_id, session_dir_name,
                                response, windows[0].total_lines)
    full_cmd = _measure_facade._expand_window_command(tool_use_id, session_dir_name)

    if spec.kind == "match":
        out = [header]
        shown = spec.skip + len(windows)
        for w in windows:
            nav = (f"[match {w.group_index}/{group_total} | "
                   f"chars {w.start}:{w.end} of {total_chars} | "
                   f"lines {w.line_start}:{w.line_end} of {w.total_lines}")
            if w.clipped:
                nav += " | clipped=yes"
            matched = ",".join(str(n) for n in w.matched_lines[:12])
            if len(w.matched_lines) > 12:
                matched += ",..."
            nav += f" | matched lines: {matched}]"
            out.append(nav)
            out.append(w.text)
            if w.clipped:
                exact = f"--start {w.start} --chars {w.end - w.start}"
            else:
                exact = f"--lines {w.line_start}:{w.line_end}"
            out.append(f"[exact: {_measure_facade._expand_window_command(tool_use_id, session_dir_name, exact)}]")
        if shown < group_total:
            out.append(f"[{group_total - shown} more match group(s) not shown — "
                       f"narrow --match or re-run with --skip {shown}]")
        elif spec.skip:
            out.append(f"[first {spec.skip} match group(s) skipped by --skip]")
        out.append(f"[full: {full_cmd}]")
        return "\n".join(out) + "\n", ranges

    w = windows[0]
    nav = [f"window chars {w.start}:{w.end} of {total_chars}",
           f"lines {w.line_start}:{w.line_end} of {w.total_lines}"]
    if spec.kind == "chars":
        length = w.end - w.start
        if w.has_prev:
            ps = max(0, w.start - length)
            nav.append("prev: " + _measure_facade._expand_window_command(
                tool_use_id, session_dir_name,
                f"--start {ps} --chars {w.start - ps}"))
        if w.has_next:
            nav.append("next: " + _measure_facade._expand_window_command(
                tool_use_id, session_dir_name,
                f"--start {w.end} --chars {length}"))
    else:
        count = w.line_end - w.line_start + 1
        if w.has_prev:
            nav.append("prev: " + _measure_facade._expand_window_command(
                tool_use_id, session_dir_name,
                f"--lines {max(1, w.line_start - count)}:{w.line_start - 1}"))
        if w.has_next:
            nav.append("next: " + _measure_facade._expand_window_command(
                tool_use_id, session_dir_name,
                f"--lines {w.line_end + 1}:{min(w.total_lines, w.line_end + count)}"))
    nav.append(f"full: {full_cmd}")
    footer = "[" + " | ".join(nav) + "]"
    body = w.text if w.text.endswith("\n") else w.text + "\n"
    return f"{header}\n{body}{footer}\n", ranges


def _aw_line_count(text):
    _measure_facade = _compat.facade()
    from archive_window import line_count
    return line_count(text)


def expand_archived(tool_use_id=None, session_id=None, list_all=None, window_flags=None):
    """Retrieve an archived tool result, or list all archived results.

    If list_all is True, prints a summary of all archived results.
    Otherwise, searches for tool_use_id and prints the full response — or, when
    window_flags carries --lines/--start/--chars/--match, only the bounded
    window that was asked for.
    """
    _measure_facade = _compat.facade()
    archive_roots = []
    for snapshot_dir in _measure_facade.snapshot_dir_candidates():
        archive_root = snapshot_dir / "tool-archive"
        if archive_root not in archive_roots:
            archive_roots.append(archive_root)

    if list_all:
        if not any(root.is_dir() for root in archive_roots):
            print("[Tool Archive] No archived results found.")
            return
        total = 0
        session_dirs = []
        for archive_root in archive_roots:
            if archive_root.is_dir() and not archive_root.is_symlink():
                session_dirs.extend(sorted(archive_root.iterdir()))
        if session_id:
            sid = _measure_facade.sanitize_session_id(session_id)
            session_dirs = [d for d in session_dirs if d.name == sid]

        for sd in session_dirs:
            if sd.is_symlink() or not sd.is_dir():
                continue
            manifest_path = sd / "manifest.jsonl"
            if not manifest_path.exists():
                continue
            manifest = []
            with open(manifest_path, encoding="utf-8") as mf:
                for mline in mf:
                    mline = mline.strip()
                    if mline:
                        try:
                            manifest.append(_measure_facade.json.loads(mline))
                        except _measure_facade.json.JSONDecodeError:
                            continue
            if not manifest:
                continue
            print(f"\n  Session: {sd.name} ({len(manifest)} archived)")
            for entry in manifest:
                ts = entry.get("timestamp", "?")
                if "T" in ts:
                    ts = ts.split("T")[0] + " " + ts.split("T")[1][:8]
                print(f"    {entry.get('tool_name', '?'):30s} {entry.get('chars', '?'):>8} chars  {entry.get('tool_use_id', '?')}  {ts}")
                total += 1
        if total == 0:
            print("[Tool Archive] No archived results found.")
        else:
            print(f"\n  Total: {total} archived results")
        print()
        return

    # Search for specific tool_use_id
    if not tool_use_id:
        print("[Error] No tool_use_id provided. Use: expand TOOL_USE_ID, expand --search QUERY, or expand --list", file=_measure_facade.sys.stderr)
        _measure_facade.sys.exit(1)

    # A verbatim copy of an old-format archive pointer ends with ']' (the
    # closing bracket used to sit on the command line). Ids can never contain
    # ']', so stripping one is free and the copy still retrieves the entry.
    if tool_use_id.endswith("]"):
        tool_use_id = tool_use_id[:-1]

    # Sanitize tool_use_id (same pattern as session_id)
    if not _measure_facade.re.match(r'^[a-zA-Z0-9_-]+\Z', tool_use_id):
        print("[Error] Invalid tool_use_id format.", file=_measure_facade.sys.stderr)
        _measure_facade.sys.exit(1)

    entry_path = _measure_facade._find_archive_entry(archive_roots, tool_use_id, session_id)
    if entry_path is None and _measure_facade._expand_may_cross_runtimes():
        # Not in this runtime's archive. A pointer printed by Claude Code is
        # never pinned, so a shell that detects another runtime (or a scan
        # that failed open to Claude Code when the archive was written under
        # another runtime) would otherwise miss an entry that exists. Only an
        # unpinned expand falls back; an explicit --runtime stays strict.
        fallback_roots = _measure_facade._cross_runtime_archive_roots(archive_roots)
        entry_path = _measure_facade._find_archive_entry(fallback_roots, tool_use_id, session_id, strict=True)
        if entry_path is None and not any(root.is_dir() for root in archive_roots):
            archive_roots = archive_roots + [r for r in fallback_roots if r.is_dir()]

    if entry_path is None and not any(root.is_dir() for root in archive_roots):
        print("[Error] No archive directory found. No results have been archived yet.", file=_measure_facade.sys.stderr)
        _measure_facade.sys.exit(1)

    if entry_path is not None:
        sd = entry_path.parent
        try:
            data = _measure_facade.json.loads(entry_path.read_text(encoding="utf-8"))
            response = data.get("response", "")
            if response:
                spec = None
                if window_flags:
                    from archive_window import SpecError, spec_from_flags
                    try:
                        spec = spec_from_flags(window_flags)
                    except SpecError as exc:
                        print(f"[Error] {exc}", file=_measure_facade.sys.stderr)
                        _measure_facade.sys.exit(1)
                if spec is None:
                    # B6 integrity: re-popping an archived result means it did
                    # NOT stay collapsed, so reverse its tool_archive credit.
                    # Log the re-popped tokens as a debit (netted in
                    # _get_savings_summary), deduped per identical range set.
                    _measure_facade._log_reexpand_debit(sd.name, tool_use_id, response,
                                        data.get("tool_name") or "")
                    # No added newline: expand returns the stored text as stored.
                    _measure_facade.sys.stdout.write(response)
                    _measure_facade.sys.stdout.flush()
                    return
                out, ranges = _measure_facade._expand_bounded(data, response, tool_use_id,
                                              sd.name, spec)
                _measure_facade._log_reexpand_debit(sd.name, tool_use_id, response,
                                    data.get("tool_name") or "",
                                    content_ranges=ranges)
                _measure_facade.sys.stdout.write(out)
                _measure_facade.sys.stdout.flush()
                return
            else:
                print(f"[Error] Archived entry found but response is empty: {entry_path}", file=_measure_facade.sys.stderr)
                _measure_facade.sys.exit(1)
        except (_measure_facade.json.JSONDecodeError, OSError) as e:
            print(f"[Error] Failed to read archived result: {e}", file=_measure_facade.sys.stderr)
            _measure_facade.sys.exit(1)

    print(f"[Error] Tool result not found: {tool_use_id}", file=_measure_facade.sys.stderr)
    if not session_id:
        print("  Tip: Use 'expand --list' to see all archived results.", file=_measure_facade.sys.stderr)
    _measure_facade.sys.exit(1)


def expand_search(query, session_id=None, limit=None):
    """Search archived tool outputs by keyword/lineage.

    Uses SessionStore.search_tool_outputs (FTS5 with LIKE fallback) to resolve
    a query to matching archived keys. Prints each match as a single line with
    key, tool name, command/path, source file path, language, archived_from,
    and a short snippet. The agent then runs ``expand <key>`` to retrieve the
    full content. No new persistence: reads the U6 index only. Never raises.
    """
    _measure_facade = _compat.facade()
    if not query or not query.strip():
        print("Usage: expand --search <query> [--session <session_id>]", file=_measure_facade.sys.stderr)
        _measure_facade.sys.exit(1)
    try:
        from session_store import SessionStore
        # Search the current session when no --session is given; otherwise
        # search the specified session. Falls back to "unknown" when neither
        # CLAUDE_SESSION_ID nor an explicit sid is available.
        sid = session_id or _measure_facade.os.environ.get("CLAUDE_SESSION_ID", "unknown")
        store = SessionStore(sid)
        results = store.search_tool_outputs(query, limit=limit)
        store.close()
    except Exception as e:
        print(f"[Error] Search failed: {e}", file=_measure_facade.sys.stderr)
        _measure_facade.sys.exit(1)

    if not results:
        print(f"[Search] No results found for: {query}")
        return

    print(f"[Search] {len(results)} result(s) for: {query}\n")
    for r in results:
        key = r.get("tool_use_id", "?")
        name = r.get("tool_name", "?")
        cmd = r.get("command_or_path", "") or ""
        src = r.get("source_file_path", "") or ""
        lang = r.get("language", "") or ""
        origin = r.get("archived_from", "") or ""
        snippet = (r.get("snippet", "") or "").replace("\n", " ")[:120]
        captured = ""
        ts = r.get("timestamp")
        if ts:
            try:
                captured = _measure_facade.datetime.fromtimestamp(
                    float(ts), _measure_facade.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            except (TypeError, ValueError, OSError):
                captured = str(ts)
        digest = str(r.get("output_hash") or "")[:16]
        chars = r.get("output_chars")
        truncated = r.get("truncated")
        trunc_s = "yes" if truncated else ("no" if truncated == 0 else "?")
        print(f"  {key}")
        print(f"    tool: {name}  origin: {origin}  lang: {lang}")
        print(f"    captured: {captured or '?'}  hash: {digest or '?'}  "
              f"chars: {chars if chars is not None else '?'}  "
              f"stored_truncated: {trunc_s}")
        if cmd:
            print(f"    cmd/path: {cmd[:120]}")
        if src:
            print(f"    source: {src[:120]}")
        if snippet:
            print(f"    snippet: {snippet}")
        print()
    print("  Use 'expand <key>' for the full content, or a bounded window:")
    print("    expand <key> --lines A:B   expand <key> --start N --chars M   "
          "expand <key> --match TEXT [--context N]")


def archive_cleanup(session_id=None):
    """Clean up archived tool results.

    If session_id is given, removes that session's archive directory.
    Otherwise, removes archives older than 24 hours.
    """
    _measure_facade = _compat.facade()
    import shutil

    archive_root = _measure_facade.SNAPSHOT_DIR / "tool-archive"
    if not archive_root.is_dir() or archive_root.is_symlink():
        print("[Tool Archive] No archive directory found. Nothing to clean.")
        return

    cleaned = 0
    cleaned_chars = 0

    if session_id:
        sid = _measure_facade.sanitize_session_id(session_id)
        target = archive_root / sid
        if target.is_symlink():
            print(f"[Tool Archive] Skipping symlink: {sid}")
            return
        if target.is_dir():
            # Count before removing
            manifest_path = target / "manifest.jsonl"
            if manifest_path.exists():
                try:
                    with open(manifest_path, encoding="utf-8") as mf:
                        for mline in mf:
                            mline = mline.strip()
                            if mline:
                                try:
                                    entry = _measure_facade.json.loads(mline)
                                    cleaned += 1
                                    cleaned_chars += entry.get("chars", 0)
                                except _measure_facade.json.JSONDecodeError:
                                    continue
                except OSError:
                    pass
            shutil.rmtree(str(target), ignore_errors=True)
            print(f"[Tool Archive] Cleaned session {sid}: {cleaned} results, {cleaned_chars:,} chars freed.")
        else:
            print(f"[Tool Archive] No archive found for session {sid}.")
        return

    # Clean up archives older than the configured retention window
    cutoff = _measure_facade.time.time() - (_measure_facade._ARCHIVE_RETENTION_HOURS * 3600)
    for sd in list(archive_root.iterdir()):
        if sd.is_symlink() or not sd.is_dir():
            continue
        # Check manifest timestamp or directory mtime
        try:
            mtime = sd.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            manifest_path = sd / "manifest.jsonl"
            count = 0
            chars = 0
            if manifest_path.exists():
                try:
                    with open(manifest_path, encoding="utf-8") as mf:
                        for mline in mf:
                            mline = mline.strip()
                            if mline:
                                try:
                                    entry = _measure_facade.json.loads(mline)
                                    count += 1
                                    chars += entry.get("chars", 0)
                                except _measure_facade.json.JSONDecodeError:
                                    continue
                except OSError:
                    pass
            shutil.rmtree(str(sd), ignore_errors=True)
            cleaned += count
            cleaned_chars += chars

    if cleaned:
        print(f"[Tool Archive] Cleaned {cleaned} archived results ({cleaned_chars:,} chars) older than {_measure_facade._ARCHIVE_RETENTION_HOURS}h.")
    else:
        print(f"[Tool Archive] No stale archives to clean (all < {_measure_facade._ARCHIVE_RETENTION_HOURS}h old).")

    # Remove empty archive root if nothing left
    try:
        remaining = list(archive_root.iterdir())
        if not remaining:
            archive_root.rmdir()
    except OSError:
        pass


def _purge_all_data(confirm=None, force=None):
    """Delete all Token Optimizer local data across all supported platforms.

    Enterprise compliance helper: one command removes every file Token Optimizer
    owns.  Transcripts, settings.json, and hook registrations are never touched.

    Dry-run by default; pass confirm=True to actually delete.
    Pass force=True to stop a running dashboard daemon automatically.
    """
    _measure_facade = _compat.facade()
    import shutil
    from runtime_env import (
        claude_home as _ch,
        codex_home as _codex,
        hermes_home as _hermes,
        opencode_data_home as _opencode,
    )

    def _dir_size_bytes(p):
        """Return total size in bytes of all regular files under p."""
        _measure_facade = _compat.facade()
        try:
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        except OSError:
            return 0

    def _fmt_size(n):
        _measure_facade = _compat.facade()
        if n >= 1_048_576:
            return f"{n / 1_048_576:.1f} MB"
        if n >= 1_024:
            return f"{n / 1_024:.1f} KB"
        return f"{n} B"

    # --- Discover all candidate directories ---
    # Each entry: (Path, human_label, [detail lines])
    candidates = []

    platform_homes = [
        (_ch(),       "~/.claude"),
        (_codex(),    "~/.codex"),
        (_hermes(),   "~/.hermes"),
        (_opencode(), "~/.local/share/opencode"),
    ]

    for home, label in platform_homes:
        # Primary data directory
        to_dir = home / "token-optimizer"
        if to_dir.exists() and not to_dir.is_symlink():
            details = []
            try:
                for child in sorted(to_dir.iterdir()):
                    details.append(f"    - {child.name}")
            except OSError:
                pass
            candidates.append((to_dir, f"{label}/token-optimizer/", details))

        # Legacy backup directory
        backup_dir = home / "_backups" / "token-optimizer"
        if backup_dir.exists() and not backup_dir.is_symlink():
            candidates.append((backup_dir, f"{label}/_backups/token-optimizer/", []))

    # Plugin data directory (trends.db, session-store/, tool-archive/, dashboard, logs, daemon)
    if _measure_facade._RESOLVED_PLUGIN_DATA is not None:
        plugin_data = _measure_facade._RESOLVED_PLUGIN_DATA / "data"
        if plugin_data.exists() and not plugin_data.is_symlink():
            details = []
            try:
                for child in sorted(plugin_data.iterdir()):
                    details.append(f"    - {child.name}")
            except OSError:
                pass
            # Build a shortened display path relative to home
            try:
                display = "~/" + str(plugin_data.relative_to(_measure_facade.Path.home()))
            except ValueError:
                display = str(plugin_data)
            candidates.append((plugin_data, display + "/", details))

    # --- Daemon guard ---
    pid_file = _measure_facade.DAEMON_LOG_DIR / "daemon.pid"
    running_pid = None
    if pid_file.exists():
        try:
            raw = pid_file.read_text(encoding="utf-8").strip()
            candidate_pid = int(raw)
            if _measure_facade._pid_is_running(candidate_pid):
                running_pid = candidate_pid
            # else: process not alive; PID file is stale
        except (OSError, ValueError):
            pass

    if running_pid is not None and not force:
        print(f"WARNING: Dashboard daemon is running (PID {running_pid}). Stop it first with:")
        print(f"  {_measure_facade._measure_cli('kill-stale')}")
        print("Or re-run with --force to stop it automatically.")
        return

    if running_pid is not None and force:
        import signal as _signal
        import subprocess as _sp
        # Verify PID identity before SIGTERM to avoid killing a recycled PID
        is_ours = False
        try:
            if _measure_facade.os.name == "nt":
                # No `ps` on Windows: the daemon was never stopped there, and
                # the purge then hit the files it still held open.
                proc_name = ((_measure_facade._windows_process_names() or {}).get(running_pid) or (0, ""))[1]
            else:
                result = _sp.run(["ps", "-p", str(running_pid), "-o", "comm="],
                                 capture_output=True, text=True, timeout=3)
                proc_name = result.stdout.strip().lower()
            is_ours = "python" in proc_name or "measure" in proc_name
        except Exception:
            is_ours = False
        if is_ours:
            try:
                _measure_facade.os.kill(running_pid, _signal.SIGTERM)
                print(f"[Token Optimizer] Stopped dashboard daemon (PID {running_pid}).")
            except (ProcessLookupError, PermissionError, OSError) as e:
                print(f"[Token Optimizer] Warning: could not stop daemon PID {running_pid}: {e}")
        else:
            print(f"[Token Optimizer] PID {running_pid} is not a Python/Token Optimizer process (recycled PID). Ignoring stale PID file.")

    # --- Print summary ---
    print("Token Optimizer Data Purge")
    print("==========================")
    print()

    if not candidates:
        print("Nothing found. Token Optimizer has no local data to remove.")
        return

    total_bytes = sum(_dir_size_bytes(p) for p, _, _ in candidates)

    if confirm:
        print(
            f"Deleting {len(candidates)} director{'y' if len(candidates) == 1 else 'ies'}"
            f" ({_fmt_size(total_bytes)} total):\n"
        )
    else:
        print(
            f"The following {'directory' if len(candidates) == 1 else 'directories'} will be deleted"
            f" ({_fmt_size(total_bytes)} total):\n"
        )

    for path, label, details in candidates:
        size = _dir_size_bytes(path)
        print(f"  {label} ({_fmt_size(size)})")
        for d in details[:8]:  # cap detail lines so output stays readable
            print(d)
        if len(details) > 8:
            print(f"    ... and {len(details) - 8} more items")

    print()
    print("NOT deleted (belongs to host platform):")
    print("  - Claude Code transcripts (~/.claude/projects/*/sessions/)")
    print("  - settings.json hook registrations")
    print()

    if not confirm:
        print("Run with --confirm to delete. Run cleanup-duplicate-hooks after if uninstalling.")
        return

    # Double confirmation gate: requires interactive human input.
    # An AI agent cannot bypass this — stdin prompt blocks non-interactive callers.
    print("WARNING: This will permanently delete all Token Optimizer data listed above.")
    print("Type 'PURGE' (all caps) to confirm deletion:")
    try:
        response = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return
    if response != "PURGE":
        print(f"Expected 'PURGE', got '{response}'. Aborted.")
        return

    # --- Perform deletion ---
    deleted = 0
    errors = 0
    for path, label, _ in candidates:
        try:
            if path.is_symlink():
                print(f"  Skipped (symlink): {label}")
                continue
            shutil.rmtree(str(path))
            print(f"  Deleted: {label}")
            deleted += 1
        except OSError as e:
            print(f"  Error deleting {label}: {e}")
            errors += 1

    print()
    if errors == 0:
        print(
            f"Purge complete. {deleted} director{'y' if deleted == 1 else 'ies'} removed"
            f" ({_fmt_size(total_bytes)})."
        )
        print(_measure_facade._hint("Tip: Run 'python3 measure.py cleanup-duplicate-hooks' if you are uninstalling."))
    else:
        print(
            f"Purge finished with {errors} error(s)."
            f" {deleted} director{'y' if deleted == 1 else 'ies'} removed."
        )


def _security_report(as_json=None):
    """Generate a self-assessment security report for enterprise IT."""
    _measure_facade = _compat.facade()
    from runtime_env import detect_runtime, runtime_name_for_humans
    try:
        from credential_patterns import CREDENTIAL_PATTERNS
        cred_count = len(CREDENTIAL_PATTERNS)
        cred_types = [label for label, _ in CREDENTIAL_PATTERNS]
    except ImportError:
        cred_count = 0
        cred_types = []
    try:
        from credential_patterns import custom_patterns_status
        cred_custom = custom_patterns_status()
    except Exception:
        cred_custom = {"active": True, "failure": None, "count": 0, "labels": [],
                       "source": None, "sha256": None, "errors": [],
                       "rejected": 0, "duplicates_skipped": 0}

    runtime = detect_runtime()
    runtime_label = runtime_name_for_humans()

    def _dir_info(p):
        _measure_facade = _compat.facade()
        if not p.exists() or p.is_symlink():
            return {"exists": False, "size_bytes": 0, "permissions": None}
        try:
            size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        except OSError:
            size = 0
        try:
            perms = oct(p.stat().st_mode)[-3:]
        except OSError:
            perms = "???"
        return {"exists": True, "size_bytes": size, "permissions": perms}

    def _file_info(p):
        _measure_facade = _compat.facade()
        if not p.exists() or p.is_symlink():
            return {"exists": False, "size_bytes": 0, "permissions": None, "mtime": None}
        try:
            st = p.stat()
            return {
                "exists": True,
                "size_bytes": st.st_size,
                "permissions": oct(st.st_mode)[-3:],
                "mtime": _measure_facade.datetime.fromtimestamp(st.st_mtime).isoformat(),
            }
        except OSError:
            return {"exists": True, "size_bytes": 0, "permissions": "???", "mtime": None}

    # Report the unified state base so the inventory reflects the real
    # Cowork locations (these globals == RUNTIME_DIR/token-optimizer on desktop).
    checkpoint_dir = _measure_facade.CHECKPOINT_DIR
    quality_cache_dir = _measure_facade.QUALITY_CACHE_DIR
    events_file = _measure_facade.CHECKPOINT_EVENT_LOG
    config_file = _measure_facade.CONFIG_DIR / "config.json"

    stores = {
        "trends_db": _file_info(_measure_facade.SNAPSHOT_DIR / "trends.db"),
        "session_store_dir": _dir_info(_measure_facade.SNAPSHOT_DIR / "session-store"),
        "tool_archive_dir": _dir_info(_measure_facade.SNAPSHOT_DIR / "tool-archive"),
        "checkpoint_dir": _dir_info(checkpoint_dir),
        "quality_cache_dir": _dir_info(quality_cache_dir),
        "dashboard_html": _file_info(_measure_facade.SNAPSHOT_DIR / "dashboard.html"),
        "daemon_token": _file_info(_measure_facade.DAEMON_TOKEN_PATH),
        "daemon_logs": _dir_info(_measure_facade.DAEMON_LOG_DIR),
        "config": _file_info(config_file),
        "checkpoint_events": _file_info(events_file),
    }

    consent_shown = _measure_facade._read_config_flag("enterprise_consent_shown", False)
    v5_shown = _measure_facade._read_config_flag("v5_welcome_shown", False)

    hooks_json_path = _measure_facade.Path(_measure_facade.os.environ.get("CLAUDE_PLUGIN_ROOT", "")) / "hooks" / "hooks.json" if _measure_facade.os.environ.get("CLAUDE_PLUGIN_ROOT") else None
    hooks_list = []
    if hooks_json_path and hooks_json_path.exists():
        try:
            hooks_list = _measure_facade.json.loads(hooks_json_path.read_text())
        except Exception:
            pass

    cleanup_period = None
    try:
        settings_path = _measure_facade.RUNTIME_DIR / "settings.json"
        if _measure_facade._is_regular_file(settings_path):
            settings = _measure_facade.json.loads(settings_path.read_text())
            cleanup_period = settings.get("cleanupPeriodDays")
    except Exception:
        pass

    daemon_pid = None
    daemon_running = False
    pid_file = _measure_facade.DAEMON_LOG_DIR / "daemon.pid"
    if pid_file.exists():
        try:
            daemon_pid = int(pid_file.read_text(encoding="utf-8").strip())
            daemon_running = _measure_facade._pid_is_running(daemon_pid)
        except (ValueError, OSError):
            pass

    report = {
        "report_version": 1,
        "generated_at": _measure_facade.datetime.now().isoformat(),
        "token_optimizer_version": _measure_facade.TOKEN_OPTIMIZER_VERSION,
        "runtime": {"name": runtime, "label": runtime_label, "home": str(_measure_facade.RUNTIME_DIR), "plugin_data": str(_measure_facade.SNAPSHOT_DIR)},
        "consent": {"enterprise_consent_shown": consent_shown, "v5_welcome_shown": v5_shown},
        "data_stores": stores,
        "retention": {
            "checkpoint_days": _measure_facade._CHECKPOINT_RETENTION_DAYS,
            "checkpoint_max": _measure_facade._CHECKPOINT_RETENTION_MAX,
            "quality_cache_days": _measure_facade._QUALITY_CACHE_RETENTION_DAYS if _measure_facade._QUALITY_CACHE_RETENTION_DAYS > 0 else "unlimited",
            "archive_hours": _measure_facade._ARCHIVE_RETENTION_HOURS,
            "trends_days": _measure_facade._TRENDS_RETENTION_DAYS if _measure_facade._TRENDS_RETENTION_DAYS > 0 else "unlimited",
            "session_store_hours": 48,
            "checkpoint_event_max": _measure_facade._CHECKPOINT_EVENT_MAX,
        },
        "credential_scanning": {"pattern_count": cred_count, "types": cred_types, "custom": cred_custom},
        "hooks": {"count": len(hooks_list), "source": str(hooks_json_path) if hooks_json_path else None},
        "dashboard": {"daemon_pid": daemon_pid, "daemon_running": daemon_running, "token_file_exists": _measure_facade.DAEMON_TOKEN_PATH.exists(), "token_file_permissions": _file_info(_measure_facade.DAEMON_TOKEN_PATH).get("permissions"), "bind_address": (_measure_facade._read_dashboard_host_file() or "127.0.0.1")},  # reflect persisted host
        "transcript_preservation": {"cleanup_period_days": cleanup_period, "note": "Intentional: preserves transcripts for trend analysis. Transcripts are host platform data."},
    }

    if as_json:
        print(_measure_facade.json.dumps(report, indent=2, default=str))
        return

    print(f"Token Optimizer Security Report")
    print(f"{'=' * 40}")
    print(f"Generated: {report['generated_at']}")
    print(f"Version: {_measure_facade.TOKEN_OPTIMIZER_VERSION}")
    print()

    print(f"1. RUNTIME ENVIRONMENT")
    print(f"   Platform: {runtime_label} ({runtime})")
    print(f"   Runtime home: {_measure_facade.RUNTIME_DIR}")
    print(f"   Plugin data: {_measure_facade.SNAPSHOT_DIR}")
    print()

    print(f"2. CONSENT STATUS")
    print(f"   Enterprise consent: {'GRANTED' if consent_shown else 'NOT GRANTED'}")
    print(f"   V5 welcome shown: {'yes' if v5_shown else 'no'}")
    print()

    print(f"3. DATA STORES")
    for name, info in stores.items():
        status = "EXISTS" if info.get("exists") else "ABSENT"
        perms = info.get("permissions", "N/A")
        size = info.get("size_bytes", 0)
        size_str = f"{size / 1024:.1f} KB" if size < 1048576 else f"{size / 1048576:.1f} MB"
        print(f"   {name}: {status} | {perms} | {size_str}")
    print()

    print(f"4. RETENTION CONFIGURATION")
    ret = report["retention"]
    print(f"   Checkpoints: {ret['checkpoint_days']} days / max {ret['checkpoint_max']} files")
    print(f"   Quality cache: {ret['quality_cache_days']} days")
    print(f"   Tool archives: {ret['archive_hours']} hours")
    print(f"   Trends DB: {ret['trends_days']}")
    print(f"   Session stores: {ret['session_store_hours']} hours")
    print(f"   Checkpoint events: max {ret['checkpoint_event_max']} entries")
    print()

    print(f"5. HOOK CONFIGURATION")
    print(f"   Hooks registered: {len(hooks_list)}")
    if hooks_json_path:
        print(f"   Source: {hooks_json_path}")
    print()

    print(f"6. CREDENTIAL SCANNING")
    print(f"   Active patterns: {cred_count}")
    if cred_types:
        for t in cred_types[:5]:
            print(f"     - {t}")
        if len(cred_types) > 5:
            print(f"     ... and {len(cred_types) - 5} more")
    if cred_custom.get("active") is False:
        print(f"   CUSTOM REDACTION INACTIVE: {cred_custom.get('failure') or 'pattern file failed to load'}")
        if cred_custom.get("source"):
            print(f"     File: {cred_custom['source']}")
        print("     Disk writers are skipping redacted writes until this is fixed.")
    else:
        print(f"   Custom patterns: {cred_custom['count']}")
        if cred_custom.get("source"):
            print(f"     Source: {cred_custom['source']}")
        if cred_custom.get("rejected"):
            print(f"     {cred_custom['rejected']} patterns rejected (see below)")
    for err in cred_custom.get("errors", [])[:5]:
        print(f"     ! {err}")
    print()

    print(f"7. DASHBOARD SECURITY")
    print(f"   Daemon running: {'yes (PID ' + str(daemon_pid) + ')' if daemon_running else 'no'}")
    print(f"   Token file: {'exists' if _measure_facade.DAEMON_TOKEN_PATH.exists() else 'absent'} | {_file_info(_measure_facade.DAEMON_TOKEN_PATH).get('permissions', 'N/A')}")
    # v5.11.1: reflect the persisted bind-host like the JSON path does,
    # instead of hardcoding loopback (which lied when network mode was on).
    _sec_bind = _measure_facade._read_dashboard_host_file() or "127.0.0.1"
    _sec_scope = "LAN (network mode)" if _sec_bind == "0.0.0.0" else "loopback only"
    print(f"   Bind address: {_sec_bind} ({_sec_scope})")
    print()

    print(f"8. TRANSCRIPT PRESERVATION")
    print(f"   cleanupPeriodDays: {cleanup_period}")
    print(f"   Note: Intentional for trend analysis. Transcripts are host platform data.")
    print()

    print(f"9. VERSION")
    print(f"   Token Optimizer: {_measure_facade.TOKEN_OPTIMIZER_VERSION}")
    print(f"   Runtime: {runtime_label}")


def _quality_cache_path_for(filepath=None):
    """Return per-session cache path if filepath given, else global fallback."""
    _measure_facade = _compat.facade()
    if filepath:
        uuid = _measure_facade.Path(filepath).stem  # e.g. "abc123" from "abc123.jsonl"
        return _measure_facade.QUALITY_CACHE_DIR / f"quality-cache-{uuid}.json"
    return _measure_facade.QUALITY_CACHE_PATH


def _quality_cache_throttle_marker(
    filepath=None, session_id=None, cache_path=None
):
    """Return the throttle marker for one session (legacy global if unknown)."""
    _measure_facade = _compat.facade()
    identity = None
    if cache_path is not None:
        identity = _measure_facade.Path(cache_path).stem.removeprefix("quality-cache-")
    elif filepath:
        identity = _measure_facade.Path(filepath).stem
    elif session_id:
        identity = _measure_facade.sanitize_session_id(session_id)
    if identity:
        identity = _measure_facade.re.sub(r"[^a-zA-Z0-9_-]", "_", str(identity))[:128]
        return _measure_facade.QUALITY_CACHE_DIR / f".quality-cache-throttle-{identity}"
    return _measure_facade.QUALITY_CACHE_DIR / ".quality-cache-throttle"


def _quality_cache_tick_due(
    throttle_seconds, filepath=None, session_id=None
):
    """One-stat gate for the PostToolUse throttle-only hot path.

    A missing marker is a cache miss, not permission to parse a transcript.
    UserPromptSubmit creates the first cache and marker.
    """
    _measure_facade = _compat.facade()
    try:
        marker = _measure_facade._quality_cache_throttle_marker(filepath, session_id)
        age = _measure_facade.time.time() - marker.stat().st_mtime
        return age >= max(0, throttle_seconds)
    except OSError:
        return False


def _qlease_sweep_due():
    """One-stat gate: True when the last sweep is older than 24h (or never ran)."""
    _measure_facade = _compat.facade()
    try:
        age = _measure_facade.time.time() - _measure_facade._QLEASE_SWEEP_MARKER.stat().st_mtime
        return age >= _measure_facade._QLEASE_SWEEP_THROTTLE_SECONDS
    except OSError:
        return True


def maybe_sweep_stale_leases(directory=None):
    """Throttled, fail-open invocation of the stale-lease sweeper.

    Runs at most once per 24h per install. Wrapped end-to-end in try/except so
    it can NEVER raise into a hook or block a session. Returns the number of
    files removed (0 on any error / throttle skip). Exposed for tests.
    """
    _measure_facade = _compat.facade()
    try:
        if not _measure_facade._qlease_sweep_due():
            return 0
        target = _measure_facade.Path(directory) if directory is not None else _measure_facade.QUALITY_CACHE_DIR
        removed = _measure_facade._sweep_stale_leases(target)
        # Stamp AFTER a successful run so a mid-sweep crash retries next session.
        try:
            _measure_facade._QLEASE_SWEEP_MARKER.parent.mkdir(parents=True, exist_ok=True)
            _measure_facade._QLEASE_SWEEP_MARKER.touch()
        except OSError:
            pass
        return removed
    except Exception:
        return 0


def _write_checkpoint_atomic(checkpoint_path, content):
    """Atomically write a checkpoint markdown file. Returns True on success.

    Uses tempfile.mkstemp + os.replace so a process interruption during write
    never leaves a partial checkpoint on disk. compact_restore would otherwise
    read a truncated file cleanly and inject it as authoritative recovery
    context, feeding the model incomplete information silently.

    mkstemp creates the temp file with 0o600 by default on POSIX, and
    os.replace preserves that permission on the final path.

    Uses try/finally (not try/except OSError) so cleanup also runs when
    _HookTimeout (a BaseException) fires mid-write. Setting tmp_path to
    None after a successful os.replace prevents the finally clause from
    unlinking the already-renamed final file.
    """
    _measure_facade = _compat.facade()
    _measure_facade.CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _measure_facade.os.chmod(str(_measure_facade.CHECKPOINT_DIR), 0o700)
    except OSError:
        pass
    tmp_path = None
    ok = False
    try:
        fd, tmp_path = _measure_facade.tempfile.mkstemp(
            dir=str(_measure_facade.CHECKPOINT_DIR),
            prefix=".checkpoint-",
            suffix=".md",
        )
        with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        _measure_facade.os.replace(tmp_path, str(checkpoint_path))
        tmp_path = None  # successfully replaced; do not unlink the destination
        ok = True
    except OSError:
        ok = False
    finally:
        if tmp_path is not None:
            try:
                _measure_facade.os.unlink(tmp_path)
            except OSError:
                pass
    return ok


class _HookTimeout(BaseException):
    """Compatibility sentinel for tests that inject the former timeout."""
    pass


def _install_hook_budget(seconds=None, on_timeout=None):
    """Start the cross-platform hard wall-clock guard."""
    _measure_facade = _compat.facade()
    return _measure_facade.HookDeadline(seconds, on_timeout=on_timeout).start()


def _compact_capture_budget_seconds(args):
    """Wall-clock budget for `compact-capture`, from `--budget-seconds N`.

    Hooks pass nothing and keep the 14 s default. A missing, unreadable or
    non-positive value gives the default; anything above 60 s is capped at 60."""
    _measure_facade = _compat.facade()
    try:
        argv = list(args or ())
        if "--budget-seconds" in argv:
            i = argv.index("--budget-seconds")
            value = float(argv[i + 1])
            if value == value and value > 0:  # rejects NaN
                return min(value, float(_measure_facade._COMPACT_CAPTURE_BUDGET_MAX_S))
    except (IndexError, ValueError, TypeError):
        pass
    return _measure_facade._COMPACT_CAPTURE_BUDGET_DEFAULT_S


def _dashboard_hook_budget_seconds(default=None):
    """Wall-clock budget for a hook-run dashboard rebuild.

    TOKEN_OPTIMIZER_DASHBOARD_HOOK_BUDGET overrides it (seconds). A rebuild
    the budget kills is finished by a detached child, so this only decides
    how much of the work happens inside the hook itself."""
    _measure_facade = _compat.facade()
    raw = _measure_facade.os.environ.get("TOKEN_OPTIMIZER_DASHBOARD_HOOK_BUDGET", "").strip()
    try:
        value = float(raw) if raw else float(default)
    except ValueError:
        value = float(default)
    return max(1.0, min(value, 600.0))


def _arm_dashboard_selfheal_on_timeout(days=None):
    """Make the active hook deadline hand the rebuild to the detached child
    if it fires. The hard watchdog exits the process without unwinding the
    stack, so this is the only path that runs when a rebuild is killed;
    nothing downstream of the kill can spawn anything. force=True because a
    killed rebuild may have just written a stale file inside the 60s throttle
    window; the on-disk version guard still applies inside the child."""
    _measure_facade = _compat.facade()
    try:
        from hook_runtime import current_deadline
        deadline = current_deadline()
    except Exception:
        return
    if deadline is None:
        return

    def _spawn():
        _measure_facade = _compat.facade()
        try:
            _measure_facade._spawn_detached_dashboard_selfheal(days=days, force=True)
        except Exception:
            pass

    deadline.add_on_timeout(_spawn)


def _running_under_hook():
    """True when this process was launched by a hook runner, not a CLI user.

    Used by ``_dispatch_collect`` / ``_dispatch_dashboard`` to arm the 20s
    HookDeadline ONLY on the hook/fossil path. The regression:
    the budget was unconditional, so an interactive ``collect --rebuild`` on a
    large history that needs >20s got ``os._exit(0)``-ed mid-transaction ->
    SQLite rolled back -> "needs rebuild" stayed true -> every later run
    wedged, silently exiting 0.

    Detection (any one):
      * ``--hook`` passed explicitly on the command line (deterministic flag
        for tests and explicit hook launches).
      * ``TOKEN_OPTIMIZER_HOOK`` env var set (``1``/``true``/``yes``/``on``),
        so a launcher can mark the hook context without changing argv.
      * (``--user`` on argv declares a user-initiated run and beats the stdin
        heuristic below, but not an explicit ``--hook`` / TOKEN_OPTIMIZER_HOOK.)
      * stdin is NOT a tty. Hook runners (Claude Code, Codex) pipe a JSON
        payload on stdin; an interactive terminal run has a tty on stdin.
        This is what catches the raw fossil command shape, which invokes
        measure.py directly without ``--hook`` or the env marker.

    When stdin cannot be probed (closed/redirected in ways ``isatty`` can't
    handle), fail toward the bounded path -- the safer default for a process
    holding a hook stdout pipe. An interactive terminal run keeps its tty and
    is left unbounded.
    """
    # A user-initiated open (the token-dashboard skill, the daemon's manual
    # "Regenerate" button, the detached self-heal below) sets this to declare an
    # INTERACTIVE context. It wins over the non-tty heuristic, which otherwise
    # misreads the skill's piped-stdin invocation as a hook and lets the 20s
    # budget kill a heavy rebuild -- Bug B: the dashboard then never regenerates
    # for a large history and never self-heals ("it didn't regenerate").
    _measure_facade = _compat.facade()
    if _measure_facade.os.environ.get("TOKEN_OPTIMIZER_INTERACTIVE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return False
    if "--hook" in _measure_facade.sys.argv:
        return True
    if _measure_facade.os.environ.get("TOKEN_OPTIMIZER_HOOK", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return True
    # ``--user`` is the argv twin of TOKEN_OPTIMIZER_INTERACTIVE for launchers
    # that cannot pass env (the desktop band's "Full dashboard" link): a person
    # clicked, so the non-tty heuristic below must not read it as a hook. An
    # explicit hook marker above still wins.
    if "--user" in _measure_facade.sys.argv:
        return False
    try:
        return not _measure_facade.sys.stdin.isatty()
    except (ValueError, OSError, AttributeError):
        return True


def _settings_has_any_to_hooks():
    """Check if ~/.claude/settings.json has ANY Token Optimizer hooks.

    Returns True if at least one hook command in any event references a Token
    Optimizer script (measure.py, read_cache.py, etc.). Returns False if the
    file has no hooks at all, or no hooks that look like ours, or if the file
    is missing/malformed/unreadable.

    Used by _should_skip_self_heal_hooks() to detect the double-registration
    scenario: our SessionStart hook is firing (so hooks are registered in SOME
    layer), but settings.json has zero of our hooks (so they're in another
    layer like --settings, project settings, or managed policy).
    """
    _measure_facade = _compat.facade()
    try:
        current, _ok = _measure_facade._read_settings_for_write()
        if not current:
            return False
        current_hooks = current.get("hooks", {})
        for _event, groups in current_hooks.items():
            for group in groups:
                for h in group.get("hooks", []):
                    cmd = str(h.get("command", ""))
                    cmd_lower = cmd.lower()
                    # Match any command that references our scripts. The
                    # identity function (_hook_command_identity) is not used
                    # here because it requires knowing the plugin's desired
                    # hooks; this is a simpler "any trace of us" check.
                    # The runner script names (sessionstart_runner.py, etc.)
                    # are included because the run.py wrapper commands for
                    # SessionStart/Stop/SessionEnd/UserPromptSubmit/PostToolUse
                    # only contain "token-optimizer" if the install path
                    # includes it; the runner script names are stable
                    # regardless of install path.
                    if ("measure.py" in cmd_lower or "read_cache.py" in cmd_lower
                            or "token-optimizer" in cmd_lower
                            or "token_optimizer" in cmd_lower
                            or "sessionstart_runner.py" in cmd_lower
                            or "stop_runner.py" in cmd_lower
                            or "userpromptsubmit_runner.py" in cmd_lower
                            or "posttooluse_runner.py" in cmd_lower):
                        return True
        return False
    except Exception:
        return False


def _should_skip_self_heal_hooks():
    """Guard against double hook registration from external settings layers.

    The host (Claude Code) does NOT expose the --settings path, the merged
    hook set, or any indication of external settings layers to hook
    subprocesses. No env var, no hook input field reveals this. So the
    detection is indirect:

    If our SessionStart hook is firing (_running_under_hook() is True) but
    ~/.claude/settings.json has ZERO Token Optimizer hooks, then our hooks
    are coming from another layer (--settings flag, project settings, managed
    policy). Adding them to settings.json would double-register and every
    hook would fire twice for the rest of the session.

    This is the exact bug that corrupted the Token Optimizer benchmark: each
    container's settings.json had no hooks (hooks supplied via --settings),
    self-heal re-added all 26, and every PreToolUse/PostToolUse fired twice.

    Returns (should_skip, confident):
      should_skip=True  → self-heal should SKIP (hooks are from another layer).
      should_skip=False → self-heal should RUN (genuine drift or manual run).
      confident=True    → the skip decision is based on a readable settings.json
                          with zero TO hooks; safe to advance the 24h throttle.
      confident=False   → the skip is due to an unreadable/missing/malformed
                          settings.json; do NOT advance the throttle (the
                          condition may be transient and re-checking sooner
                          is safer than locking out healing for 24h).
    """
    _measure_facade = _compat.facade()
    if not _measure_facade._running_under_hook():
        # Manual `measure.py ensure-health` from a terminal: always heal.
        return False, True
    if _measure_facade._settings_has_any_to_hooks():
        # We have SOME hooks in settings.json but maybe not all → genuine
        # drift within our layer. Heal the missing/stale ones.
        return False, True
    # We're running as a hook but settings.json has zero of our hooks.
    # Check if the file was actually readable. If not, the skip is uncertain.
    try:
        _data, ok = _measure_facade._read_settings_for_write()
        if not ok:
            # Missing/malformed/unreadable settings.json. The skip is uncertain:
            # the file might contain our hooks but we can't tell. Don't advance
            # the 24h throttle so the next SessionStart re-checks sooner.
            return True, False
    except Exception:
        return True, False
    # settings.json is readable and has zero TO hooks. Our hooks are active
    # from another layer. Don't double-register. Safe to advance the throttle.
    return True, True


def _clear_hook_budget(deadline):
    """Cancel a normally completed hook's watchdog."""
    _measure_facade = _compat.facade()
    if deadline is not None:
        deadline.cancel()


def _write_quality_cache(cache_path, result):
    """Atomically write result dict to per-session cache. Returns True on success.

    Previously also wrote a global fallback (quality-cache.json), but that caused
    cross-session data pollution. The statusline now reads only the per-session cache
    matched by session_id, so the global fallback is no longer needed.

    Uses try/finally (not try/except OSError) so cleanup also runs when
    _HookTimeout (a BaseException) fires mid-write. tmp_path is set to None
    after a successful os.replace so the finally clause does not unlink the
    already-renamed destination.
    """
    _measure_facade = _compat.facade()
    _measure_facade.QUALITY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    ok = False
    try:
        fd, tmp_path = _measure_facade.tempfile.mkstemp(dir=str(_measure_facade.QUALITY_CACHE_DIR), suffix=".json")
        with _measure_facade.os.fdopen(fd, "w") as f:
            _measure_facade.json.dump(result, f)
        _measure_facade.os.replace(tmp_path, str(cache_path))
        tmp_path = None
        try:
            _measure_facade._quality_cache_throttle_marker(cache_path=cache_path).touch()
        except OSError:
            pass
        ok = True
    except OSError:
        ok = False
    finally:
        if tmp_path is not None:
            try:
                _measure_facade.os.unlink(tmp_path)
            except OSError:
                pass
    return ok


def _extract_session_start_ts(filepath):
    """Extract the first timestamp from a JSONL session file. Returns epoch seconds or None."""
    _measure_facade = _compat.facade()
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    record = _measure_facade.json.loads(line)
                    if not isinstance(record, dict):
                        continue
                    ts_str = record.get("timestamp")
                    if ts_str:
                        ts = _measure_facade.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        return int(ts.timestamp())
                except (_measure_facade.json.JSONDecodeError, ValueError, TypeError):
                    continue
    except (PermissionError, OSError):
        pass
    return None


def _extract_active_agents(filepath):
    """Extract currently running subagents from a JSONL session transcript.

    Scans for Task/Agent tool_use dispatches and their corresponding
    tool_result completions (which appear in user-type records).
    Returns only agents that are still running (no result yet).
    """
    _measure_facade = _compat.facade()
    dispatched = {}  # tool_use_id -> {model, description, start_time}
    completed = set()  # tool_use_ids that have results

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    record = _measure_facade.json.loads(line)
                except _measure_facade.json.JSONDecodeError:
                    continue

                if not isinstance(record, dict):
                    continue
                rec_type = record.get("type")
                msg = record.get("message", {})
                content = msg.get("content", []) if isinstance(msg, dict) else []
                if not isinstance(content, list):
                    continue

                ts_str = record.get("timestamp")

                for block in content:
                    if not isinstance(block, dict):
                        continue

                    # Agent dispatch (in assistant messages)
                    if rec_type == "assistant" and block.get("type") == "tool_use" and block.get("name") in ("Task", "Agent"):
                        tool_id = block.get("id", "")
                        inp = block.get("input", {})
                        dispatched[tool_id] = {
                            "model": inp.get("model", ""),
                            "description": (inp.get("description") or inp.get("prompt", ""))[:20],
                            "start_time": ts_str,
                        }

                    # Tool result (in user messages, not assistant)
                    if block.get("type") == "tool_result":
                        result_id = block.get("tool_use_id", "")
                        if result_id in dispatched:
                            completed.add(result_id)

    except (PermissionError, OSError):
        pass

    # Return only agents still running, most recent last, cap at 5
    running = [
        {"model": info["model"], "description": info["description"],
         "start_time": info["start_time"], "status": "running"}
        for tid, info in dispatched.items()
        if tid not in completed
    ]
    return running[-5:]


def _read_quality_cache(cache_path):
    """Read a per-session quality cache file. Returns dict or empty dict."""
    _measure_facade = _compat.facade()
    try:
        return _measure_facade.json.loads(cache_path.read_text(encoding="utf-8"))
    except (_measure_facade.json.JSONDecodeError, OSError, AttributeError):
        return {}


def _checkpoint_cooldown_remaining(result):
    """Return remaining cooldown seconds before another checkpoint may fire."""
    _measure_facade = _compat.facade()
    last_epoch = result.get("last_checkpoint_epoch")
    if not last_epoch:
        return 0
    try:
        remaining = int(last_epoch + _measure_facade._CHECKPOINT_COOLDOWN_SECONDS - _measure_facade.time.time())
    except (TypeError, ValueError):
        return 0
    return max(0, remaining)


def _record_checkpoint_metadata(result, cache_path, trigger, checkpoint_path, *, fill_pct=None, quality_score=None):
    """Persist checkpoint trigger metadata back into the per-session cache."""
    _measure_facade = _compat.facade()
    result["last_checkpoint_epoch"] = int(_measure_facade.time.time())
    result["last_checkpoint_trigger"] = trigger
    result["last_checkpoint_path"] = checkpoint_path
    if fill_pct is not None:
        result["last_checkpoint_fill_pct"] = round(fill_pct, 1)
    if quality_score is not None:
        result["last_checkpoint_quality_score"] = round(quality_score, 1)
    _measure_facade._write_quality_cache(cache_path, result)
    _measure_facade._append_checkpoint_event(
        session_id=_measure_facade.Path(cache_path).stem.replace("quality-cache-", "", 1),
        trigger=trigger,
        checkpoint_path=checkpoint_path,
        fill_pct=fill_pct,
        quality_score=quality_score,
    )


def _append_checkpoint_event(session_id, trigger, checkpoint_path, *, fill_pct=None, quality_score=None):
    """Append a deterministic local checkpoint event for rollout telemetry."""
    _measure_facade = _compat.facade()
    if not _measure_facade._CHECKPOINT_TELEMETRY_ENABLED:
        return
    try:
        _measure_facade.CHECKPOINT_EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "timestamp": _measure_facade.datetime.now(_measure_facade.timezone.utc).isoformat(),
            "platform": "claude-code",
            "session_id": _measure_facade.sanitize_session_id(session_id),
            "trigger": trigger,
            "checkpoint_path": str(checkpoint_path),
        }
        if fill_pct is not None:
            event["fill_pct"] = round(fill_pct, 1)
        if quality_score is not None:
            event["quality_score"] = round(quality_score, 1)
        fd = _measure_facade.os.open(str(_measure_facade.CHECKPOINT_EVENT_LOG), _measure_facade.os.O_WRONLY | _measure_facade.os.O_CREAT | _measure_facade.os.O_APPEND, 0o600)
        with _measure_facade.os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(_measure_facade.json.dumps(event) + "\n")
    except OSError:
        pass


def checkpoint_stats(days=None, as_json=None):
    """Summarize local checkpoint telemetry for rollout validation."""
    _measure_facade = _compat.facade()
    cutoff = _measure_facade.datetime.now(_measure_facade.timezone.utc) - _measure_facade.timedelta(days=days)
    events = []
    if _measure_facade.CHECKPOINT_EVENT_LOG.exists():
        try:
            with open(_measure_facade.CHECKPOINT_EVENT_LOG, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = _measure_facade.json.loads(line)
                    except _measure_facade.json.JSONDecodeError:
                        continue
                    ts_raw = event.get("timestamp")
                    try:
                        ts = _measure_facade.datetime.fromisoformat(ts_raw.replace("Z", "+00:00")) if isinstance(ts_raw, str) else None
                    except ValueError:
                        ts = None
                    if ts is None:
                        continue
                    event["_ts"] = ts
                    events.append(event)
        except OSError:
            events = []

    recent = [e for e in events if e["_ts"] >= cutoff]
    by_trigger = {}
    for event in recent:
        trigger = event.get("trigger", "unknown")
        by_trigger[trigger] = by_trigger.get(trigger, 0) + 1

    last_event = None
    if recent:
        recent.sort(key=lambda e: ((_measure_facade := _compat.facade()), e["_ts"])[1], reverse=True)
        last_event = {
            "timestamp": recent[0].get("timestamp"),
            "session_id": recent[0].get("session_id"),
            "trigger": recent[0].get("trigger"),
            "fill_pct": recent[0].get("fill_pct"),
            "quality_score": recent[0].get("quality_score"),
        }

    summary = {
        "enabled": _measure_facade._CHECKPOINT_TELEMETRY_ENABLED,
        "event_log": str(_measure_facade.CHECKPOINT_EVENT_LOG),
        "days": days,
        "total_events": len(events),
        "recent_events": len(recent),
        "by_trigger": dict(sorted(by_trigger.items())),
        "last_event": last_event,
    }

    if as_json:
        print(_measure_facade.json.dumps(summary, indent=2))
    else:
        print(f"\n  Checkpoint Telemetry ({days}d)")
        print(f"  {'=' * 40}")
        print(f"  Enabled:       {'yes' if summary['enabled'] else 'no'}")
        print(f"  Event log:     {summary['event_log']}")
        print(f"  Total events:  {summary['total_events']}")
        print(f"  Recent events: {summary['recent_events']}")
        if summary["by_trigger"]:
            print("  By trigger:")
            for trigger, count in summary["by_trigger"].items():
                print(f"    {trigger:28s} {count}")
        if last_event:
            print("  Last event:")
            print(f"    {last_event['timestamp']}  {last_event['trigger']}  session={last_event['session_id']}")
    return summary


def _current_edit_batch_stats(quality_data):
    """Return write-count and unique modified file-count for the current context window."""
    _measure_facade = _compat.facade()
    writes = quality_data.get("writes", [])
    write_count = len(writes)
    unique_file_count = len({path for _, path, _ in writes if path})
    return {
        "write_count": write_count,
        "unique_file_count": unique_file_count,
    }


def _maybe_checkpoint_on_quality_or_milestone(quality_data, cache_path, result, filepath):
    """Capture one-shot quality checkpoints and repeatable edit-batch milestones."""
    _measure_facade = _compat.facade()
    if not filepath:
        return

    score = result.get("score")
    fill_pct = result.get("fill_pct")
    cooldown_remaining = _measure_facade._checkpoint_cooldown_remaining(result)

    quality_captured = result.get("quality_thresholds_captured", [])
    if score is not None and cooldown_remaining <= 0:
        for threshold in _measure_facade._QUALITY_CHECKPOINT_THRESHOLDS:
            if score < threshold and threshold not in quality_captured:
                trigger = f"quality-{threshold}"
                cp_path = _measure_facade.compact_capture(
                    transcript_path=str(filepath),
                    session_id=_measure_facade.Path(filepath).stem,
                    trigger=trigger,
                    fill_pct=fill_pct,
                    quality_score=score,
                )
                if cp_path:
                    quality_captured.append(threshold)
                    quality_captured.sort(reverse=True)
                    result["quality_thresholds_captured"] = quality_captured
                    _measure_facade._record_checkpoint_metadata(
                        result,
                        cache_path,
                        trigger,
                        cp_path,
                        fill_pct=fill_pct,
                        quality_score=score,
                    )
                    return
                break

    edit_stats = _measure_facade._current_edit_batch_stats(quality_data)
    marker = result.get("edit_batch_marker", {})
    marker_writes = int(marker.get("write_count", 0) or 0)
    marker_files = int(marker.get("unique_file_count", 0) or 0)

    write_delta = edit_stats["write_count"] - marker_writes
    file_delta = edit_stats["unique_file_count"] - marker_files

    if (
        cooldown_remaining <= 0
        and (
            write_delta >= _measure_facade._EDIT_BATCH_WRITE_THRESHOLD
            or file_delta >= _measure_facade._EDIT_BATCH_FILE_THRESHOLD
        )
    ):
        trigger = "milestone-edit-batch"
        cp_path = _measure_facade.compact_capture(
            transcript_path=str(filepath),
            session_id=_measure_facade.Path(filepath).stem,
            trigger=trigger,
            fill_pct=fill_pct,
            quality_score=score,
        )
        if cp_path:
            result["edit_batch_marker"] = edit_stats
            milestone_log = result.get("milestone_history", [])
            milestone_log.append({
                "trigger": trigger,
                "write_count": edit_stats["write_count"],
                "unique_file_count": edit_stats["unique_file_count"],
                "timestamp": _measure_facade.datetime.now(_measure_facade.timezone.utc).isoformat(),
            })
            result["milestone_history"] = milestone_log[-10:]
            _measure_facade._record_checkpoint_metadata(
                result,
                cache_path,
                trigger,
                cp_path,
                fill_pct=fill_pct,
                quality_score=score,
            )


def _maybe_progressive_checkpoint(fill_pct, cache_path, result, filepath):
    """Create a progressive checkpoint if fill_pct crosses an uncaptured band.

    Progressive checkpoints capture richer session state at 20%, 35%, 50%, 65%, and 80%
    context fill, instead of only at ~98% (PreCompact). Earlier capture means
    more decisions, files, and context are preserved.

    Mutates `result` dict to track captured bands. Writes updated cache.
    """
    _measure_facade = _compat.facade()
    if not filepath or fill_pct <= 0:
        return

    bands_captured = result.get("progressive_bands_captured", [])
    cooldown_remaining = _measure_facade._checkpoint_cooldown_remaining(result)
    if cooldown_remaining > 0:
        return

    # Find the highest band crossed but not yet captured
    target_band = None
    for band in sorted(_measure_facade._PROGRESSIVE_BANDS, reverse=True):
        if fill_pct >= band and band not in bands_captured:
            target_band = band
            break

    if target_band is None:
        return

    t0 = _measure_facade.time.time()

    # Determine session ID from filepath (JSONL filename = session UUID)
    session_id = filepath.stem if hasattr(filepath, "stem") else _measure_facade.Path(filepath).stem

    try:
        cp_path = _measure_facade.compact_capture(
            transcript_path=str(filepath),
            session_id=session_id,
            trigger=f"progressive-{target_band}",
            fill_pct=fill_pct,
        )
    except Exception:
        return

    elapsed_ms = int((_measure_facade.time.time() - t0) * 1000)

    if cp_path:
        # Mark this band AND all lower bands as captured
        for band in _measure_facade._PROGRESSIVE_BANDS:
            if band <= target_band and band not in bands_captured:
                bands_captured.append(band)
        bands_captured.sort()

        result["progressive_bands_captured"] = bands_captured
        result["progressive_last_checkpoint"] = cp_path
        result["progressive_capture_ms"] = elapsed_ms
        _measure_facade._record_checkpoint_metadata(
            result,
            cache_path,
            f"progressive-{target_band}",
            cp_path,
            fill_pct=fill_pct,
            quality_score=result.get("score"),
        )


def _verbosity_nudge_min_fill() -> int:
    """Resolve the gentle-tier floor, clamped to the range where it means
    anything.

    Unclamped this is a foot-gun in both directions. At 0 or negative the
    gentle tier fires at ANY fill, including the first prompt of an empty
    session -- the exact failure the session-identity guard was written to
    stop. At >= 75 the strong tier already owns the range, so the gentle tier
    becomes unreachable and the feature silently does nothing. Both are
    misconfigurations a user would never see, so clamp loudly instead.
    """
    _measure_facade = _compat.facade()
    raw = _measure_facade._int_env("TOKEN_OPTIMIZER_VERBOSITY_MIN_FILL", 25)
    clamped = max(1, min(raw, 74))
    if clamped != raw:
        print(
            f"[Token Optimizer] Warning: TOKEN_OPTIMIZER_VERBOSITY_MIN_FILL={raw} "
            f"is outside the useful range 1-74, clamped to {clamped}.",
            file=_measure_facade.sys.stderr,
        )
    return clamped


def _recent_compact_boundary(path, within_s=None, now=None):
    """True when the transcript's newest compact_boundary row is under `within_s` old.

    Then the compaction the PostCompact hook is reporting is already in the parse,
    and counting it again would double it. Reads only the tail of the file.
    """
    _measure_facade = _compat.facade()
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, _measure_facade.os.SEEK_END)
            fh.seek(max(0, size - _measure_facade._COMPACT_TAIL_BYTES))
            tail = fh.read()
    except (OSError, TypeError, ValueError):
        return False
    # The boundary row itself, not the words inside a message: same marker as the
    # counter, and the parsed row must be a system compact_boundary.
    i = tail.rfind(_measure_facade._COMPACT_MARK)
    while i >= 0:
        start = tail.rfind(b"\n", 0, i) + 1
        end = tail.find(b"\n", i)
        try:
            row = _measure_facade.json.loads(tail[start:end if end >= 0 else len(tail)])
        except Exception:
            row = None
        if isinstance(row, dict) and row.get("type") == "system" and row.get("subtype") == "compact_boundary":
            break
        i = tail.rfind(_measure_facade._COMPACT_MARK, 0, start)
    else:
        return False
    try:
        ts = _measure_facade._keepwarm_iso_to_epoch(row.get("timestamp"))
    except Exception:
        return False
    if ts is None:
        return False
    now = _measure_facade.time.time() if now is None else now
    return 0 <= now - ts <= within_s


def _settled_compactions(parsed, prev_result, post_compact, now=None):
    """The session's compaction count, never lower than what was already known.

    Claude Code appends the compact_boundary row to the transcript after the
    PostCompact hooks run, so the PostCompact refresh parses a transcript that
    does not show the compaction yet. That refresh therefore counts one more
    than before when the parse has not caught up, once per compaction (a
    second PostCompact within a minute is the same one). A later parse that
    sees the boundary agrees with that count rather than adding to it. The
    caller passes post_compact=False when the boundary is already in the
    transcript. Two real compactions inside one minute read as one until the
    next parse sees both boundaries, which then corrects the count.
    """
    _measure_facade = _compat.facade()
    try:
        prev = max(0, int((prev_result or {}).get("compactions", 0) or 0))
    except (TypeError, ValueError, OverflowError):
        prev = 0
    try:
        parsed = max(0, int(parsed or 0))
    except (TypeError, ValueError, OverflowError):
        parsed = 0
    count = max(parsed, prev)
    if post_compact and parsed <= prev:
        now = _measure_facade.time.time() if now is None else now
        try:
            last = float((prev_result or {}).get("_compact_bumped_at") or 0)
        except (TypeError, ValueError, OverflowError):
            last = 0.0
        if now - last > _measure_facade._COMPACT_BUMP_WINDOW_S:
            count = prev + 1
    return count


def _quality_scores_comparable(prev_result, result):
    """True when two quality results share a score_version and may be dampened
    or compared for nudge baselines. Legacy unversioned caches are v1-era and
    never compare against _QUALITY_SCORE_VERSION."""
    _measure_facade = _compat.facade()
    pv = (prev_result or {}).get("score_version")
    rv = (result or {}).get("score_version", _measure_facade._QUALITY_SCORE_VERSION)
    return pv is not None and pv == rv


def _check_realtime_loops(quality_data):
    """Lightweight loop detection using already-parsed quality_data.

    Returns a list of warning dicts (empty if no loops detected).
    Never raises -- all errors caught internally.
    """
    _measure_facade = _compat.facade()
    warnings = []
    try:
        # --- Message loop detection ---
        messages = quality_data.get("messages", [])
        # Extract last N user message texts
        user_msgs = []
        for entry in messages:
            if len(entry) >= 4 and entry[1] == "user" and entry[3]:  # (idx, role, text_length, is_substantive)
                user_msgs.append(entry)
        recent_user = user_msgs[-_measure_facade._LOOP_LAST_MESSAGES:] if len(user_msgs) >= _measure_facade._LOOP_LAST_MESSAGES else []

        if len(recent_user) >= _measure_facade._LOOP_LAST_MESSAGES:
            # We only have text_length, not text content, in quality_data messages.
            # Check for length-based similarity as a proxy (same length = suspicious).
            lengths = [m[2] for m in recent_user]
            if lengths and max(lengths) > 0:
                # If all recent messages are within 20% length of each other, flag
                avg_len = sum(lengths) / len(lengths)
                if avg_len > 50 and all(abs(length - avg_len) / max(avg_len, 1) < 0.2 for length in lengths):
                    warnings.append({
                        "type": "message_loop",
                        "confidence": 0.7,
                        "count": len(recent_user),
                    })

        # --- Retry churn detection ---
        # Short tool results are common for successful operations ("done",
        # empty search results, concise shell output). Only warn when recent
        # short results also carry concrete failure signals and come from
        # the same tool family.
        tool_result_meta = quality_data.get("tool_result_meta", [])
        if len(tool_result_meta) >= 3:
            recent_tools = tool_result_meta[-5:]
            short_failures = [
                t for t in recent_tools
                if t.get("is_failure") and t.get("size", 0) < 400
            ]
            if len(short_failures) >= 3:
                by_tool = {}
                for item in short_failures:
                    tool_name = item.get("tool_name") or "unknown"
                    by_tool[tool_name] = by_tool.get(tool_name, 0) + 1
                most_repeated = max(by_tool.values()) if by_tool else 0
                if most_repeated >= 3:
                    warnings.append({
                        "type": "retry_churn",
                        "confidence": 0.75,
                        "count": most_repeated,
                    })

    except Exception:
        pass  # Never crash quality_cache

    return warnings


def _checkpoint_restore_credited_recently(session_id, window_seconds=None):
    """True if a checkpoint_restore savings event was logged for this session in
    the last `window_seconds`. Used by A2 to stop quality_nudge follow-through
    double-crediting a compaction recovery already counted by checkpoint_restore.
    Never raises (returns False on any error)."""
    _measure_facade = _compat.facade()
    if not session_id or not _measure_facade.TRENDS_DB.exists():
        return False
    try:
        cutoff = (_measure_facade.datetime.now() - _measure_facade.timedelta(seconds=window_seconds)).isoformat()
        conn = _measure_facade._init_trends_db()
        try:
            row = conn.execute(
                "SELECT 1 FROM savings_events WHERE event_type='checkpoint_restore' "
                "AND session_id = ? AND timestamp >= ? LIMIT 1",
                (session_id, cutoff),
            ).fetchone()
        finally:
            conn.close()
        return bool(row)
    except (_measure_facade.sqlite3.Error, OSError):
        return False


def _fresh_session_savings_estimate(fill_pct, window=None):
    """Tokens reclaimed by starting fresh now: the current context (fill% of the
    window) that a continued session keeps re-paying, minus the small lean block a
    fresh resume re-injects. Returns (saved_tokens, window). Best-effort.

    `window` MUST be the same window fill_pct was measured against -- callers pass
    result["model_context_window"]. Two failure modes this guards against:
    (1) detect_context_window() returns a (size, source) TUPLE, so the old
    int(detect_context_window()) ALWAYS raised and silently fell back to 200K --
    making the token count 5x too low on a 1M window; (2) re-detecting in this
    process can see a different window than the live session, desyncing the token
    count from the fill %.
    """
    _measure_facade = _compat.facade()
    try:
        window = int(window) if window else 0
    except (TypeError, ValueError):
        window = 0
    if window <= 0:
        try:
            win = _measure_facade.detect_context_window()
            window = int(win[0] if isinstance(win, tuple) else win)
        except Exception:
            window = 200_000
    current_ctx = int(max(0.0, min(100.0, float(fill_pct or 0))) / 100.0 * window)
    saved = max(0, current_ctx - _measure_facade._FRESH_NUDGE_LEAN_BLOCK_TOKENS)
    return saved, window


def _fresh_session_turn_economics(saved_tokens, session_id=None):
    """Per-turn saving and break-even turns for starting a fresh session now.

    saved_tokens is the warm context (current fill minus the lean block) a
    continued session keeps re-sending. The restart is not free, so the
    figure reports the RECURRING per-turn benefit and the turns needed to
    repay the one-time costs -- a counterfactual ESTIMATE, never a meter,
    and no horizon is assumed:

      per_turn   = max(0, saved - rebuild) * cache_read
                   -- warm context the continued session re-sends each
                      turn, minus the working set the fresh session carries
                      anyway (same estimate the checkpoint-restore credit
                      uses);
      one_time   = lean block * cache_write + rebuild * input
                   -- the checkpoint/lean block written once so the new
                      session can pick up, plus first reads that are
                      uncached;
      breakeven  = ceil(one_time / per_turn) turns, None when per_turn <= 0.

    Priced at the session's own model rates. Best-effort -> zeroed dict on
    any error.
    """
    _measure_facade = _compat.facade()
    zero = {"per_turn_usd": 0.0, "one_time_usd": 0.0, "breakeven_turns": None}
    try:
        tier = _measure_facade._load_pricing_tier()
        tier_data = _measure_facade.PRICING_TIERS.get(tier, _measure_facade.PRICING_TIERS["anthropic"])
        model = _measure_facade._resolve_session_model(session_id)
        rates = tier_data["claude_models"].get(
            model, tier_data["claude_models"].get("sonnet", {}))
        in_rate = float(rates.get("input", 3.0))
        cr_rate = float(rates.get("cache_read") or in_rate)
        cw_rate = float(rates.get("cache_write") or in_rate)
        saved = max(0, int(saved_tokens or 0))
        rebuild = _measure_facade._checkpoint_restore_recovery_tokens(
            session_id, _measure_facade._FRESH_NUDGE_LEAN_BLOCK_TOKENS)
        per_turn = max(0.0, (saved - rebuild) * cr_rate / 1_000_000)
        one_time = (_measure_facade._FRESH_NUDGE_LEAN_BLOCK_TOKENS * cw_rate
                    + rebuild * in_rate) / 1_000_000
        breakeven = (max(1, _measure_facade.math.ceil(one_time / per_turn))
                     if per_turn > 0 else None)
        return {"per_turn_usd": per_turn, "one_time_usd": one_time,
                "breakeven_turns": breakeven}
    except Exception:
        return dict(zero)


def _maybe_fresh_session_nudge(result, cache_path, quality_data, quiet=None):
    """Suggest starting a fresh session when this one is BOTH long and degraded.

    Fires once per session when quality < _FRESH_NUDGE_QUALITY_THRESHOLD AND fill
    >= _FRESH_NUDGE_MIN_FILL. Shows the estimated tokens they'd reclaim
    by starting fresh right now. Returns the systemMessage string or None.
    Takes precedence over the /compact nudge (the caller skips that when this fires).
    """
    _measure_facade = _compat.facade()
    if not _measure_facade._is_v5_feature_enabled("quality_nudges"):
        return None
    score = result.get("score")
    fill_pct = result.get("fill_pct", 0)
    if score is None:
        return None
    # Once per session.
    if result.get("_fresh_nudge_fired"):
        return None
    # Post-compaction suppression: no prior score recorded yet -> not a long
    # established session, let _maybe_nudge seed the baseline instead.
    if result.get("_nudge_previous_score") is None:
        return None
    if not (score < _measure_facade._FRESH_NUDGE_QUALITY_THRESHOLD and fill_pct >= _measure_facade._FRESH_NUDGE_MIN_FILL):
        return None

    # "Start fresh" is a restart recommendation and shares the same
    # absolute-token gate as /compact nudges: >=300K current-context tokens
    # with real telemetry, or nothing fires.
    _gate_sid = _measure_facade.Path(cache_path).stem.replace("quality-cache-", "", 1) if cache_path else None
    _gate_tel = _measure_facade._absolute_context_tokens(quality_data, _gate_sid)
    if _gate_tel is None or _gate_tel["tokens"] < _measure_facade._SIZE_NUDGE_MIN_TOKENS:
        return None

    session_id = _measure_facade.Path(cache_path).stem.replace("quality-cache-", "", 1) if cache_path else None
    # The flag above lives in the quality cache, which other writes can replace
    # (a lost race, an unreadable transcript). A marker file claimed with
    # O_EXCL keeps "once per session" true whatever happens to the cache, and
    # only one of two racing processes can claim it.
    if _measure_facade._ran_once_this_session("fresh-nudge", session_id):
        result["_fresh_nudge_fired"] = True
        return None
    saved, _window = _measure_facade._fresh_session_savings_estimate(
        fill_pct, window=result.get("fill_denominator") or result.get("model_context_window"))
    result["_fresh_nudge_fired"] = True
    _measure_facade._log_compression_event(
        feature="fresh_session_nudge",
        session_id=session_id,
        detail=f"score={score} fill_pct={fill_pct} est_saved={saved}",
        verified=False,
    )
    saved_str = f"~{saved // 1000}K" if saved >= 1000 else f"~{saved}"
    econ = _measure_facade._fresh_session_turn_economics(saved, session_id)
    per_turn = econ["per_turn_usd"]
    # The dollar figure is a counterfactual estimate: the recurring per-turn
    # saving plus the turns that repay the one-time handover/rebuild cost.
    # No horizon is assumed, so the message never claims a session-total.
    cost_str = ""
    if per_turn >= 0.001:
        per_str = f"${per_turn:.2f}" if per_turn >= 0.01 else "<$0.01"
        breakeven = econ["breakeven_turns"]
        be_str = (f", pays back after ~{breakeven} turn"
                  f"{'s' if breakeven != 1 else ''}" if breakeven else "")
        cost_str = f" (saves ~{per_str} per turn{be_str}, est.)"
    # Share of the model window: what the user's meter shows (see _format_context_fill).
    shown = _measure_facade._model_window_fill(result)
    shown = shown if isinstance(shown, (int, float)) else fill_pct
    return (
        f"[Token Optimizer] {int(shown)}% full, quality {score}. "
        f"Fresh session reclaims {saved_str}{cost_str}. "
        f"Say \"continue this\"."
    )


def _maybe_nudge(result, cache_path, quality_data, quiet=None):
    """Check if a quality nudge should fire. Returns systemMessage string or None.

    Nudges fire when:
    - Score dropped >15 points since last check, OR
    - Score crossed below 60
    Respects: cooldown (5 min), session cap (3), post-compaction suppression.
    """
    _measure_facade = _compat.facade()
    if not _measure_facade._is_v5_feature_enabled("quality_nudges"):
        return None

    score = result.get("score")
    if score is None:
        return None

    previous_score = result.get("_nudge_previous_score")
    nudge_count = result.get("_nudge_count", 0)
    last_nudge_epoch = result.get("_nudge_last_epoch", 0)

    # Post-compaction suppression: if no previous score, just record current and skip
    if previous_score is None:
        result["_nudge_previous_score"] = score
        return None

    # Session cap
    if nudge_count >= _measure_facade._NUDGE_SESSION_CAP:
        result["_nudge_previous_score"] = score
        return None

    # Cooldown
    now = _measure_facade.time.time()
    if now - last_nudge_epoch < _measure_facade._NUDGE_COOLDOWN_SECONDS:
        result["_nudge_previous_score"] = score
        return None

    # Check thresholds
    drop = previous_score - score
    should_nudge = (drop > 15) or (score < 60 and previous_score >= 60)

    result["_nudge_previous_score"] = score

    if not should_nudge:
        return None

    # Absolute-token gate: every compaction-recommending nudge requires
    # >=300K CURRENT live-context tokens backed by real telemetry. A quality
    # drop at 120K is still reported, but "/compact" is a size remedy and
    # size remedies are floored at 300K (COMPACTION-QUALITY.md section 1).
    session_id = _measure_facade.Path(cache_path).stem.replace("quality-cache-", "", 1) if cache_path else None
    telemetry = _measure_facade._absolute_context_tokens(quality_data, session_id)
    if telemetry is None or telemetry["tokens"] < _measure_facade._SIZE_NUDGE_MIN_TOKENS:
        return None

    # Fire nudge
    result["_nudge_count"] = nudge_count + 1
    result["_nudge_last_epoch"] = now

    # Log the nudge as a behavioral intervention. Store fill_pct so
    # PostCompact can measure the actual token recovery if the user
    # compacts after seeing this nudge.
    fill_pct = result.get("fill_pct", 0)
    result["_nudge_fill_pct_at_fire"] = fill_pct
    # A6: persist an explicit fire timestamp alongside the fill snapshot (written
    # to the sidecar with the rest of `result`). The follow-through uses it to
    # gate credit to compactions that actually followed the nudge, and the B4
    # cohort uses fire-vs-follow-through timing to split heeded vs ignored.
    result["_nudge_fire_epoch"] = now
    _measure_facade._log_compression_event(
        feature="quality_nudge",
        session_id=session_id,
        detail=f"score={score} prev={previous_score} drop={drop} fill_pct={fill_pct}",
        verified=False,
    )

    return f"[Token Optimizer] Quality {score} (was {previous_score}). /compact."


def _absolute_context_tokens(quality_data, session_id=None):
    """Current live-context token count, or None when telemetry is missing
    or contradictory. THE gate every size-based restart/compact
    recommendation must pass -- no nudge below 300K, and never a guessed
    window standing in for a measurement.

    Sources, in order:
      1. host live-fill.json for THIS session, fresh (<5 min): context_tokens
         cross-checked against the session's own window (or the host's
         used_percentage as denominator).
      2. transcript-reported assistant usage (context_tokens) over the
         session's own model_context_window.

    Returns {"tokens": int, "window": int|None, "source": str} or None.
    """
    _measure_facade = _compat.facade()
    window = None
    try:
        mcw = (quality_data or {}).get("model_context_window")
        if isinstance(mcw, (int, float)) and mcw > 0:
            window = int(mcw)
    except (TypeError, ValueError):
        window = None

    # 1. Host-reported live fill, same session only.
    try:
        live_fill_path = _measure_facade.QUALITY_CACHE_DIR / "live-fill.json"
        if live_fill_path.exists():
            live = _measure_facade.json.loads(live_fill_path.read_text(encoding="utf-8"))
            ts_ms = live.get("timestamp")
            age_s = (_measure_facade.time.time() - float(ts_ms) / 1000.0) if ts_ms else None
            live_sid = _measure_facade.sanitize_session_id(str(live.get("session_id") or ""))
            want_sid = _measure_facade.sanitize_session_id(str(session_id or ""))
            tokens = live.get("context_tokens")
            if (age_s is not None and 0 <= age_s < _measure_facade._CONTEXT_ADVISORY_TELEMETRY_MAX_AGE_S
                    and want_sid and live_sid == want_sid
                    and isinstance(tokens, (int, float)) and tokens > 0):
                w = window
                pct = live.get("used_percentage")
                if (not w or w <= 0) and isinstance(pct, (int, float)) and 0 < pct <= 100:
                    w = int(round(tokens / (pct / 100.0)))
                # Contradictory telemetry (tokens exceed the measured window)
                # is unknown, never a nudge.
                if w and w > 0 and tokens <= w * 1.02:
                    return {"tokens": int(tokens), "window": int(w), "source": "host-live"}
                return None
    except (OSError, ValueError, TypeError, KeyError, _measure_facade.json.JSONDecodeError):
        pass

    # 2. Transcript usage over the session's own window.
    try:
        tokens = (quality_data or {}).get("context_tokens")
        if (isinstance(tokens, (int, float)) and tokens > 0
                and window and window > 0):
            if tokens <= window * 1.02:
                return {"tokens": int(tokens), "window": window, "source": "transcript"}
            return None
    except (TypeError, ValueError):
        pass
    return None


def _context_advisory_threshold():
    """Configured context-size advisory threshold, clamped to [300K, 400K].

    The floor is a hard product rule: no size-based nudge below 300K tokens.
    Out-of-range config is clamped loudly rather than silently honored.
    """
    _measure_facade = _compat.facade()
    raw = _measure_facade._int_env("TOKEN_OPTIMIZER_CONTEXT_ADVISORY_TOKENS",
                   _measure_facade._CONTEXT_ADVISORY_DEFAULT_TOKENS)
    clamped = max(_measure_facade._CONTEXT_ADVISORY_MIN_TOKENS,
                  min(raw, _measure_facade._CONTEXT_ADVISORY_MAX_TOKENS))
    if clamped != raw:
        print(f"[Token Optimizer] Warning: TOKEN_OPTIMIZER_CONTEXT_ADVISORY_TOKENS={raw} "
              f"outside {_measure_facade._CONTEXT_ADVISORY_MIN_TOKENS}-{_measure_facade._CONTEXT_ADVISORY_MAX_TOKENS}, "
              f"clamped to {clamped}.", file=_measure_facade.sys.stderr)
    return clamped


def _advisory_checkpoint_has_restorable_body(checkpoint):
    """Use restore eligibility for advisory checkpoints, including sidecars."""
    _measure_facade = _compat.facade()
    return _measure_facade._checkpoint_has_restorable_body(checkpoint.get("path"))


def _session_has_current_checkpoint(session_id, quality_data=None):
    """Require a restorable session checkpoint from the last 30 minutes,
    saved since the latest compaction. Unknown boundary time fails closed."""
    _measure_facade = _compat.facade()
    try:
        if not session_id:
            return False
        want = {str(session_id), _measure_facade.sanitize_session_id(str(session_id))}
        now = _measure_facade.time.time()
        cutoff = now - 30 * 60
        qd = quality_data or {}
        if qd.get("compactions"):
            events = qd.get("compaction_events") or []
            if not events:
                return False
            latest = events[-1]
            boundary = latest.get("ts_epoch") or _measure_facade._keepwarm_iso_to_epoch(latest.get("ts"))
            if boundary is None:
                return False
            cutoff = max(cutoff, boundary)
        for cp in _measure_facade.list_checkpoints():
            if _measure_facade._checkpoint_session(cp["filename"]) not in want:
                continue
            if (cutoff <= cp["created"].timestamp() <= now
                    and _measure_facade._advisory_checkpoint_has_restorable_body(cp)):
                return True
        return False
    except Exception:
        return False


def _advisory_blockers(result, quality_data, session_id):
    """Eligibility blockers for the context-size advisory, independent of the
    configured threshold. A threshold is eligibility, not an instruction:
    require real progress, a safe pause, a valid current checkpoint, and a
    plausible benefit after summary+retrieval overhead."""
    _measure_facade = _compat.facade()
    blockers = {}
    tool_calls = int((quality_data or {}).get("tool_calls") or 0)
    user_turns = sum(
        1 for m in (quality_data or {}).get("messages", [])
        if len(m) >= 3 and m[1] == "user" and m[2] > 0)
    # Progress beyond the initial prompt: real tool work or a follow-up turn.
    blockers["no_progress"] = not (tool_calls >= 5 or user_turns >= 2)
    # Parsers distinguish real prompts/end_turn from tool-result records.
    blockers["mid_turn"] = (quality_data or {}).get("safe_pause") is not True
    # Valid current checkpoint for THIS session.
    blockers["no_checkpoint"] = not _measure_facade._session_has_current_checkpoint(session_id, quality_data)
    return blockers


def _log_context_advisory_shadow(session_id, telemetry, result, quality_data):
    """Hourly-throttled shadow record of what each candidate threshold
    (300K/350K/400K) would decide. Feeds the advisor evaluation; emits no
    user-facing output."""
    _measure_facade = _compat.facade()
    try:
        tokens = int(telemetry.get("tokens") or 0)
        if tokens < _measure_facade._CONTEXT_ADVISORY_MIN_TOKENS or not session_id:
            return
        bucket = int(_measure_facade.time.time() // 3600)
        marker = _measure_facade._once_per_session_marker("context-advisory-shadow", session_id)
        if marker is None:
            return
        try:
            if _measure_facade.json.loads(marker.read_text(encoding="utf-8")).get("bucket") == bucket:
                return
        except (OSError, ValueError, TypeError):
            pass
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(_measure_facade.json.dumps({"bucket": bucket}), encoding="utf-8")
        blockers = _measure_facade._advisory_blockers(result, quality_data, session_id)
        eval_at = {
            str(t): {
                "eligible": tokens >= t and not any(blockers.values()),
                "below": tokens < t,
            } for t in _measure_facade._CONTEXT_ADVISORY_SHADOW_TOKENS
        }
        _measure_facade._log_compression_event(
            feature="context_advisory_shadow",
            tier="estimated",
            session_id=session_id,
            detail=_measure_facade.json.dumps({
                "tokens": tokens, "window": telemetry.get("window"),
                "source": telemetry.get("source"),
                "thresholds": eval_at,
                "blockers": {k: v for k, v in blockers.items() if v},
            }, sort_keys=True),
            verified=False,
        )
    except Exception:
        pass


def _maybe_context_size_advisory(result, cache_path, quality_data, suppressed=None):
    """Context-size advisory: at >= threshold current tokens, suggest
    *considering* compaction at the end of the step -- eligibility, not an
    instruction. Once per session, current telemetry only, never below 300K,
    silent when telemetry is missing or contradictory. `suppressed` (a
    stronger fresh-session/quality remedy already fired this turn) skips the
    fire WITHOUT consuming the once-per-session slot -- the user never saw
    it, so it must not count as fired. Shadow logging still runs. Returns
    the systemMessage string or None."""
    _measure_facade = _compat.facade()
    if not _measure_facade._is_v5_feature_enabled("context_size_advisory"):
        return None
    session_id = _measure_facade.Path(cache_path).stem.replace("quality-cache-", "", 1) if cache_path else None
    telemetry = _measure_facade._absolute_context_tokens(quality_data, session_id)
    if telemetry is None:
        return None
    # Shadow-evaluate all three candidate thresholds for the weekly eval,
    # whether or not the configured one fires.
    _measure_facade._log_context_advisory_shadow(session_id, telemetry, result, quality_data)
    if suppressed or result.get("_context_advisory_fired"):
        return None
    threshold = _measure_facade._context_advisory_threshold()
    tokens = telemetry["tokens"]
    if tokens < threshold or tokens < _measure_facade._CONTEXT_ADVISORY_MIN_TOKENS:
        return None
    blockers = _measure_facade._advisory_blockers(result, quality_data, session_id)
    # Plausible benefit after summary + retrieval overhead.
    benefit = tokens - _measure_facade._CONTEXT_ADVISORY_POST_COMPACT_TOKENS - 10_000
    if benefit <= 0 or any(blockers.values()):
        return None
    if _measure_facade._ran_once_this_session("context-size-advisory", session_id):
        result["_context_advisory_fired"] = True
        return None
    result["_context_advisory_fired"] = True
    _measure_facade._log_compression_event(
        feature="context_size_advisory",
        tier="estimated",
        session_id=session_id,
        detail=(f"tokens={tokens} threshold={threshold} "
                f"window={telemetry.get('window')} source={telemetry.get('source')}"),
        verified=False,
    )
    k = tokens // 1000
    return (f"[Token Optimizer] {k}K tokens in use. Checkpoint saved recently; "
            f"consider compacting at the end of this step.")


def _maybe_quality_warn(result, warn_threshold):
    """Gate the `quality-cache --warn` CLI message with the same guardrails
    _maybe_nudge already has: session cap + cooldown + "only re-fire on a NEW
    lower quality band."

    Without this, the plain-text warning ("Context quality: N/100. Stale reads
    and bloated results building up. Consider /compact.") re-printed on every
    UserPromptSubmit where score < warn_threshold -- no cap, no cooldown, no
    "only if it got worse" check. A session hovering in one band under the
    default warn_threshold=70 (e.g. 69 -> 66 -> 64 -> 62) nagged on every
    single prompt. _maybe_nudge, its sibling above, already solved this exact
    problem for the proactive nudge; this reuses the same constants
    (_NUDGE_SESSION_CAP, _NUDGE_COOLDOWN_SECONDS) so both paths behave
    consistently. A later drop into a new lower 10-point band (e.g. from the
    60s into the 50s) still warns again, since that is new information worth
    surfacing even mid-cooldown-window.

    Returns True if the CLI should print the warning this tick.
    """
    _measure_facade = _compat.facade()
    score = result.get("score")
    if score is None or score >= warn_threshold:
        return False

    warn_count = result.get("_warn_count", 0)
    last_warn_epoch = result.get("_warn_last_epoch", 0)
    last_band = result.get("_warn_last_band")
    band = int(score // 10)

    if warn_count >= _measure_facade._NUDGE_SESSION_CAP:
        return False

    now = _measure_facade.time.time()
    if now - last_warn_epoch < _measure_facade._NUDGE_COOLDOWN_SECONDS:
        return False

    # Only re-warn once quality has dropped into a NEW lower band since the
    # last warning fired (e.g. from the 60s into the 50s). Repeated ticks
    # within the same band are exactly the spam this guards against.
    if last_band is not None and band >= last_band:
        return False

    result["_warn_count"] = warn_count + 1
    result["_warn_last_epoch"] = now
    result["_warn_last_band"] = band
    return True


def _maybe_loop_warning(result, cache_path, quality_data, quiet=None):
    """Check for real-time loops. Returns systemMessage string or None."""
    _measure_facade = _compat.facade()
    if not _measure_facade._is_v5_feature_enabled("loop_detection"):
        return None

    loop_count = result.get("_loop_warning_count", 0)
    if loop_count >= _measure_facade._LOOP_SESSION_CAP:
        return None

    warnings = _measure_facade._check_realtime_loops(quality_data)
    if not warnings:
        return None

    # Pick highest confidence warning
    best = max(warnings, key=lambda w: ((_measure_facade := _compat.facade()), w.get("confidence", 0))[1])
    if best["confidence"] < 0.6:
        return None

    result["_loop_warning_count"] = loop_count + 1

    # Record the OBSERVED loop volume: the chars of the turns that actually
    # repeated. quality_data.messages has (idx, role, text_length,
    # is_substantive); sum the looping turns' text_length, then estimate tokens
    # at chars/4. This measures spend that already happened -- it is NOT a
    # saving: the warning compresses nothing and the avoided continuation is
    # counterfactual, so the row is tier=estimated, verified=False, and the
    # feature sits outside _V5_COMPRESSION_CATEGORIES (see
    # _NON_SAVINGS_COMPRESSION_FEATURES). The dollar value of what the warning
    # prevents lives only in _estimate_behavioral_savings, labelled estimated.
    try:
        loop_count_n = max(1, int(best.get("count", 2)))
    except (TypeError, ValueError):
        loop_count_n = 2
    messages = quality_data.get("messages", [])
    loop_turn_chars = 0
    if messages:
        recent = messages[-loop_count_n * 2:]  # user+assistant pairs
        loop_turn_chars = sum(int(m[2] or 0) for m in recent)
    observed_loop_tokens = int(max(loop_turn_chars / _measure_facade.CHARS_PER_TOKEN, 500))

    session_id = _measure_facade.Path(cache_path).stem.replace("quality-cache-", "", 1) if cache_path else None
    _measure_facade._log_compression_event(
        feature="loop_detection",
        original_text=" " * (observed_loop_tokens * 4),
        compressed_text=f"loop:{best['type']}",
        session_id=session_id,
        detail=f"type={best['type']} confidence={best['confidence']:.2f} count={loop_count_n} observed_chars={loop_turn_chars} observed_tokens={observed_loop_tokens}",
        verified=False,
        tier="estimated",
    )

    if best["type"] == "message_loop":
        return (
            f"[Token Optimizer] Loop: {best.get('count', 0)} similar messages "
            f"in last {_measure_facade._LOOP_LAST_MESSAGES} turns. Try a different approach."
        )
    elif best["type"] == "retry_churn":
        return (
            f"[Token Optimizer] Retry loop: {best.get('count', 0)} similar short results. "
            f"Same approach may keep failing."
        )
    return None


def _acquire_quality_lock(cache_path, acquire_timeout=None, reclaim_released=None):
    """Bounded portable lock serializing quality_cache recompute+write.

    Returns a LeaseLock on success or False when unavailable/contended. The
    caller must skip the mutation on False; it never proceeds unlocked.

    Why: the PostToolUse refresh fires on every tool call, so with parallel
    subagents many quality_cache() processes can recompute concurrently. The
    score itself is idempotent, but the nudge/loop carry-forward counters are
    not — concurrent read-modify-write would clobber them and double-fire
    nudges. Serializing recompute eliminates that race and the redundant
    thundering-herd recomputes. Hook processes are one-shot, so the advisory
    lock is also released on process exit as a safety net.

    ``acquire_timeout`` is caller-tuned: a REFRESH stays short (serving the
    just-written value one tick late is fine), but a BOOTSTRAP must wait long
    enough to win, because the real recompute hold is ~200ms on a large
    transcript (scales with size) -- far longer than the old fixed 75ms, so a
    bootstrap contending with a refresh could never win and the session showed
    "ContextQ:--" forever.
    """
    _measure_facade = _compat.facade()
    lock = _measure_facade.LeaseLock(
        cache_path.with_suffix(".qlease"),
        deadline=_measure_facade.current_deadline(),
        acquire_timeout=acquire_timeout,
        reclaim_released=reclaim_released,
    )
    if not lock.acquire():
        return False
    return lock


def _release_quality_lock(lock):
    _measure_facade = _compat.facade()
    if not lock:
        return
    lock.release()


def quality_cache(throttle_seconds=None, warn_threshold=None, quiet=None, session_jsonl=None, force=None, pure_time_throttle=None, session_id=None, warn=None, post_compact=None):
    """Run quality analysis and write score to cache file for status line.

    Skips analysis if cache is younger than throttle_seconds (unless force=True).
    Args:
        session_jsonl: Path string to the session JSONL (from hook transcript_path).
                       If provided, used directly instead of guessing by mtime.
        force: If True, bypass throttle (used by PostCompact hook for immediate refresh).
        warn: If True and the (possibly just-recomputed) score is below warn_threshold,
                       print the plain-text "Context quality: N/100..." CLI warning --
                       gated by _maybe_quality_warn (cap + cooldown + band-drop) the
                       same way the proactive nudge below is gated.
        pure_time_throttle: If True, throttle purely on cache age and ignore whether
                       the transcript changed. The default (False) recomputes whenever
                       the session changed, which is right for the infrequent
                       UserPromptSubmit path. The PostToolUse path sets this True:
                       during a long autonomous run the transcript changes on every
                       tool call, so without it the status score would either recompute
                       on every call (costly) or — as it did before this existed — never
                       refresh at all, leaving the bar frozen at a stale value. With it,
                       the score refreshes at most once per throttle window mid-run.
    Returns the quality score, or None if skipped/failed.
    """
    # Resolve the session file: prefer explicit path, fall back to mtime guess
    _measure_facade = _compat.facade()
    if session_jsonl:
        filepath = _measure_facade.Path(session_jsonl) if _measure_facade.Path(session_jsonl).exists() else None
    else:
        # Resolve by identity before falling back to an mtime guess: a wrong
        # guess writes this session's score into another session's cache.
        filepath = _measure_facade._current_jsonl_for_hook(session_id)

    # Per-session cache: each session has its own file to avoid cross-session pollution
    cache_path = _measure_facade._quality_cache_path_for(filepath)

    # A throttle-only PostToolUse tick NEVER bootstraps a missing cache. This is
    # a deliberate latency invariant (see
    # tests/test_hook_runtime_parity.py::test_throttle_only_cache_miss_never_parses_transcript):
    # PostToolUse fires on every tool call, and a full transcript parse there
    # would add hundreds of milliseconds to each one.
    #
    # The ORIGINAL comment here said "UserPromptSubmit owns the initial
    # computation". That describes the intended design, but hooks.json never
    # wired UserPromptSubmit to compute it -- only SessionStart did
    # (`quality-cache --force --quiet --once-mark`, timeout 20s). So a single
    # SessionStart that timed out (a busy machine, a boot storm, a heavy
    # benchmark -- exactly when a user checks the statusline) left the cache
    # uncreated forever and the statusline stuck on `ContextQ:--` with no
    # recovery path. The fix is to wire the hook the design already assumed,
    # NOT to make the hot path slow.
    if pure_time_throttle and not force and not cache_path.exists():
        return None

    # Throttle: skip only if cache is recent AND the session transcript has not changed.
    # This keeps latency low without missing threshold crossings on active sessions.
    if not force and cache_path.exists():
        try:
            age = _measure_facade.time.time() - cache_path.stat().st_mtime
            session_unchanged = filepath is not None and filepath.stat().st_mtime <= cache_path.stat().st_mtime
            # pure_time_throttle still requires a resolvable session file: a missing
            # file must return None (no score), never serve a cached value as if live.
            throttle_skip = (age < throttle_seconds and filepath is not None) if pure_time_throttle else (age < throttle_seconds and session_unchanged)
            if throttle_skip:
                if not quiet:
                    try:
                        cached = _measure_facade._read_quality_cache(cache_path)
                        return cached.get("score")
                    except (_measure_facade.json.JSONDecodeError, OSError):
                        pass
                return None
        except OSError:
            pass

    if not filepath:
        return None

    # Serialize recompute+write per session (closes the concurrent-PostToolUse
    # nudge-state race). Released at every return below; process exit is the
    # safety net for one-shot hook invocations.
    # Bootstrap (no cache yet) owns the FIRST score for this session; losing the
    # lease here means "ContextQ:--" persists, so wait long enough to win it. A
    # refresh (cache exists) stays short -- the holder is already writing a fresh
    # value, so serving stale one tick is fine. Both env-overridable.
    _bootstrapping = not cache_path.exists()
    _lock_timeout = (
        _measure_facade._float_env("TOKEN_OPTIMIZER_QUALITY_BOOTSTRAP_LOCK_TIMEOUT", 2.0)
        if _bootstrapping
        else _measure_facade._float_env("TOKEN_OPTIMIZER_QUALITY_LOCK_TIMEOUT", 0.15)
    )
    # The PostCompact refresh is the one recompute that must land (it records the
    # compaction); the cohort reservation another hook's released lease leaves
    # behind must not turn it away.
    _qlock = (
        _measure_facade._acquire_quality_lock(cache_path, acquire_timeout=_lock_timeout, reclaim_released=True)
        if post_compact
        else _measure_facade._acquire_quality_lock(cache_path, acquire_timeout=_lock_timeout)
    )
    if _qlock is False:
        # Breadcrumb (was silent): a lost lease serves the STALE score on a refresh,
        # or writes NOTHING on a bootstrap -- the "ContextQ stuck / not showing"
        # symptom. Leave a trace on stderr (visible under `claude --debug`) so a
        # live freeze is diagnosable instead of invisible.
        try:
            print(
                "[Token Optimizer] quality-cache: lease busy, "
                + ("bootstrap skipped (no cache written)"
                   if _bootstrapping else "served stale score")
                + f" for session ...{_measure_facade.re.sub(r'[^0-9A-Za-z-]', '', _measure_facade.Path(cache_path).stem[-8:])}",
                file=_measure_facade.sys.stderr,
            )
        except Exception:
            pass
        if not quiet:
            try:
                cached = _measure_facade._read_quality_cache(cache_path)
                return cached.get("score") if cached else None
            except (_measure_facade.json.JSONDecodeError, OSError):
                pass
        return None

    # Double-checked locking: the holder may have written a fresh cache while we
    # waited for the lease (especially the 2.0s bootstrap wait). If we entered as a
    # bootstrap but the cache now exists, another process just bootstrapped it --
    # return that instead of a redundant ~200ms recompute of the same score.
    if _bootstrapping and cache_path.exists():
        try:
            _fresh = _measure_facade._read_quality_cache(cache_path)
        except (_measure_facade.json.JSONDecodeError, OSError):
            _fresh = None
        if _fresh:
            _measure_facade._release_quality_lock(_qlock)
            return _fresh.get("score") if not quiet else None

    try:
        # Run quality analysis
        quality_data = _measure_facade._parse_jsonl_for_quality(filepath)
        if not quality_data:
            # New/empty session - write a clean score to cache so stale score doesn't persist.
            # A transcript the parser gives up on (very large) is not a new session: keep the
            # compaction count it already had, so "never decreases" holds there too.
            try:
                _prev_empty = _measure_facade._read_quality_cache(cache_path) if cache_path.exists() else {}
            except Exception:
                _prev_empty = {}
            result = {
                "score": 100,
                "grade": "S",
                "score_version": _measure_facade._QUALITY_SCORE_VERSION,
                "signals": {},
                "breakdown": {},
                "total_messages": 0,
                "decisions_found": 0,
                "compactions": _measure_facade._settled_compactions(
                    0, _prev_empty or {}, post_compact and not _measure_facade._recent_compact_boundary(filepath)),
                "recovery_status": None,
                "recovery_score": None,
                "recovery_evidence": [],
                "turns": 0,
                "timestamp": _measure_facade.datetime.now(_measure_facade.timezone.utc).isoformat(),
                "session_file": _measure_facade.Path(filepath).name,
                "model_context_window": _measure_facade.detect_context_window()[0],
            }
            # Same bump bookkeeping as the full path: one count per compaction.
            if (_prev_empty or {}).get("_compact_bumped_at"):
                result["_compact_bumped_at"] = _prev_empty["_compact_bumped_at"]
            # Keep the nudge and warning state too: dropping it re-arms nudges
            # that already fired once the transcript parses again.
            for carry_key in _measure_facade._CARRY_KEYS:
                if carry_key in (_prev_empty or {}):
                    result[carry_key] = _prev_empty[carry_key]
            # A legacy/v1 nudge baseline must not seed v2 comparisons.
            if (_prev_empty or {}).get("score_version") != _measure_facade._QUALITY_SCORE_VERSION:
                result.pop("_nudge_previous_score", None)
            try:
                _prev_n = int((_prev_empty or {}).get("compactions", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                _prev_n = 0
            if post_compact and result["compactions"] > _prev_n:
                result["_compact_bumped_at"] = _measure_facade.time.time()
            _measure_facade._write_quality_cache(cache_path, result)
            return 100

        # Carry forward nudge/loop state from previous cache (survives across
        # UserPromptSubmit → PostCompact boundary for follow-through measurement)
        prev_result = {}
        if cache_path.exists():
            try:
                prev_result = _measure_facade._read_quality_cache(cache_path) or {}
            except Exception:
                prev_result = {}

        _session_id = _measure_facade.Path(cache_path).stem.replace("quality-cache-", "", 1) if cache_path else None
        result = _measure_facade.compute_quality_score(quality_data, session_id=_session_id)
        for carry_key in _measure_facade._CARRY_KEYS:
            if carry_key in prev_result and carry_key not in result:
                result[carry_key] = prev_result[carry_key]
        result["total_messages"] = len(quality_data["messages"])
        # This count is NOT deduped by the chunk-dedup / tool-result-drop
        # logic (which lives in
        # _parse_session_jsonl and feeds the session_log INSERT). The
        # statusline's ContextQ and the dashboard's stored quality_score
        # therefore use different message denominators. This is intentional:
        # quality_cache is a real-time statusline signal that needs to be
        # fast, while session_log is a persisted record that needs to be
        # accurate. Routing the statusline through the same dedup would add
        # latency to every prompt for a number the user glances at.
        result["decisions_found"] = len(quality_data["decisions"])
        result["compactions"] = _measure_facade._settled_compactions(
            quality_data["compactions"], prev_result,
            post_compact and not _measure_facade._recent_compact_boundary(filepath))
        try:
            _prev_compactions = int(prev_result.get("compactions", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            _prev_compactions = 0
        if post_compact and result["compactions"] > max(int(quality_data["compactions"] or 0), _prev_compactions):
            result["_compact_bumped_at"] = _measure_facade.time.time()
        elif prev_result.get("_compact_bumped_at"):
            result["_compact_bumped_at"] = prev_result["_compact_bumped_at"]
        result["turns"] = len([m for m in quality_data["messages"] if m[1] == "user"])
        result["timestamp"] = _measure_facade.datetime.now(_measure_facade.timezone.utc).isoformat()
        result["session_file"] = _measure_facade.Path(filepath).name
        result["model"] = quality_data.get("model")
        result["topic"] = quality_data.get("topic")
        # Add degradation band for status line
        cfd = result.get("breakdown", {}).get("context_fill_degradation", {})
        result["degradation_band"] = cfd.get("band", "")
        result["fill_pct"] = cfd.get("fill_pct", 0)
        # Surface the resolved context window at the TOP LEVEL of the cache. Consumers
        # that compute fill from raw transcript tokens (the VS Code companion's
        # cacheReader, the dashboard) read `model_context_window` here; without it they
        # fall back to a 200k guess and inflate every 1M-context session ~5x.
        result["model_context_window"] = cfd.get("model_context_window") or _measure_facade.detect_context_window()[0]
        # The window alone cannot be sanity-checked by a reader; the source can.
        result["model_context_window_source"] = (
            cfd.get("model_context_window_source") or _measure_facade.detect_context_window()[1]
        )
        result["context_window_contradicted"] = bool(cfd.get("window_contradicted"))

        # Dampen ResourceHealth swings within a session.
        # Fill_pct fluctuates between measurements (context adds/removes, compaction).
        # Allow recovery but dampen: drops are immediate, recovery moves 30% toward
        # the new value per measurement to prevent wild upward swings.
        # Only within the same score_version: a v1 score mixed in the count-only
        # compaction penalty, so importing its deficit (or its nudge baseline)
        # would silently compare apples to oranges (COMPACTION-QUALITY.md).
        if _measure_facade._quality_scores_comparable(prev_result, result):
            prev_rh = prev_result.get("resource_health")
            current_rh = result.get("resource_health")
            if prev_rh is not None and current_rh is not None:
                if current_rh >= prev_rh:
                    result["resource_health"] = round(prev_rh + (current_rh - prev_rh) * 0.3, 1)
                result["score"] = result["resource_health"]
                result["grade"] = _measure_facade.score_to_grade(round(result["resource_health"]))
                result["resource_health_grade"] = result["grade"]
        else:
            # Version boundary or legacy unversioned cache: reseed the nudge
            # baseline instead of comparing across score schemes.
            result.pop("_nudge_previous_score", None)

        # Session duration + active agents for statusline (v2.6)
        result["session_start_ts"] = _measure_facade._extract_session_start_ts(filepath)
        result["active_agents"] = _measure_facade._extract_active_agents(filepath)

        # Cache hit rate for statusline (v5.4.27). A billing ratio, so it reads
        # the top-level (advisor-summed) usage on purpose, unlike context fill,
        # which uses _context_usage (#287).
        try:
            total_input_all = 0
            total_cache_read = 0
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        rec = _measure_facade.json.loads(line)
                        usage = rec.get("message", {}).get("usage", {}) if rec.get("type") == "assistant" else {}
                        if usage:
                            total_input_all += usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
                            total_cache_read += usage.get("cache_read_input_tokens", 0)
                    except (_measure_facade.json.JSONDecodeError, AttributeError):
                        continue
            result["cache_hit_rate"] = round(total_cache_read / total_input_all, 3) if total_input_all > 0 else 0
        except (OSError, ZeroDivisionError):
            result["cache_hit_rate"] = 0

        if not _measure_facade._write_quality_cache(cache_path, result):
            return None

        # v5.0: Quality nudges + loop detection
        # These always run regardless of --quiet, because they emit systemMessage JSON
        # that Claude Code injects into context. Suppressing them defeats their purpose.
        system_messages = []

        # Fill warnings fire independently of composite score (Tier 1 monotonicity fix).
        # These cannot be masked by improving ratio signals. Deduplicated per level
        # so the same threshold doesn't fire every prompt.
        fill_warning = result.get("fill_warning")
        if fill_warning and fill_warning["level"] in ("WARNING", "CRITICAL"):
            prev_fill_warn_level = prev_result.get("_last_fill_warn_level")
            if fill_warning["level"] != prev_fill_warn_level:
                result["_last_fill_warn_level"] = fill_warning["level"]
                system_messages.append(
                    f"[Token Optimizer] {fill_warning['level']}: {fill_warning['message']}"
                )

        # Tool call fatigue warnings (independent of composite score)
        tool_call_warning = result.get("tool_call_warning")
        if tool_call_warning and tool_call_warning["level"] in ("WARNING", "CRITICAL"):
            prev_tc_level = prev_result.get("_last_tool_call_warn_level")
            if tool_call_warning["level"] != prev_tc_level:
                result["_last_tool_call_warn_level"] = tool_call_warning["level"]
                system_messages.append(
                    f"[Token Optimizer] {tool_call_warning['level']}: {tool_call_warning['message']}"
                )

        # Fresh-session nudge takes precedence: when a session is long AND degraded,
        # "start fresh (context is preserved)" is the stronger remedy than /compact, and
        # firing both would be noise. Only fall back to the /compact nudge if it didn't fire.
        fresh_msg = _measure_facade._maybe_fresh_session_nudge(result, cache_path, quality_data)
        remedy_fired = bool(fresh_msg)
        if not fresh_msg:
            nudge_msg = _measure_facade._maybe_nudge(result, cache_path, quality_data)
            if nudge_msg:
                system_messages.append(nudge_msg)
                remedy_fired = True
        loop_msg = _measure_facade._maybe_loop_warning(result, cache_path, quality_data)
        if loop_msg:
            system_messages.append(loop_msg)
        # Context-size advisory: eligibility, not an instruction -- at most
        # once per session, suppressed while a stronger remedy already fired
        # (suppression does not consume the once-per-session slot).
        advisory_msg = _measure_facade._maybe_context_size_advisory(
            result, cache_path, quality_data, suppressed=remedy_fired)
        if advisory_msg:
            system_messages.append(advisory_msg)
        # Persist nudge/loop/fresh state (the once-per-session _fresh_nudge_fired flag
        # must survive even if only the fresh nudge fired this turn).
        if system_messages or fresh_msg:
            _measure_facade._write_quality_cache(cache_path, result)
        # ONE JSON object, never a stream of them. Printing N separate
        # {"systemMessage": ...} lines is NDJSON, and a host that parses hook
        # stdout as a single JSON document rejects the whole payload -- Codex
        # fails the hook outright ("hook returned invalid session start JSON
        # output"), losing every message including the ones it could have shown.
        # Joining them costs nothing and both hosts render the result identically.
        _emitted_messages = []
        if system_messages:
            # Gate the routine informational batch (quality/loop/fill warnings) under
            # context pressure.
            _emit_msgs = True
            try:
                from context_pressure import should_inject, get_pressure_level, log_suppression
                _sf = str(filepath) if filepath else None
                _sid = _measure_facade.Path(cache_path).stem.replace("quality-cache-", "", 1) if cache_path else None
                if not should_inject(_sf, session_id=_sid, priority="informational"):
                    log_suppression("quality_system_messages", get_pressure_level(_sf, session_id=_sid))
                    _emit_msgs = False
            except Exception:
                pass
            if _emit_msgs:
                _emitted_messages.extend(system_messages)
        # The fresh-session nudge BYPASSES pressure suppression on purpose: it fires at
        # most once per session and is most relevant precisely when the session is
        # degraded (i.e. under pressure). Suppressing it would defeat its purpose and
        # silently burn the one-shot -- the exact bug where users never saw it.
        if fresh_msg:
            _emitted_messages.append(fresh_msg)
        if _emitted_messages:
            try:
                print(_measure_facade.json.dumps({"systemMessage": "\n\n".join(_emitted_messages)}))
            except Exception:
                pass

        # `quality-cache --warn` plain-text CLI warning (distinct from the
        # systemMessage JSON above -- this is what the UserPromptSubmit hook
        # actually installs: `quality-cache --warn --quiet`). Gated by
        # _maybe_quality_warn so it no longer re-prints on every prompt while a
        # session sits in one quality band; see that function for the rationale.
        if warn and _measure_facade._maybe_quality_warn(result, warn_threshold):
            _emit_warn = True
            try:
                from context_pressure import should_inject, get_pressure_level, log_suppression
                _wf = str(filepath) if filepath else None
                if not should_inject(_wf, session_id=_session_id, priority="informational"):
                    log_suppression("quality_warning", get_pressure_level(_wf, session_id=_session_id))
                    _emit_warn = False
            except Exception:
                pass
            if _emit_warn:
                # Same absolute-token gate: below 300K live tokens the
                # score-driven "/compact" recommendation is a size nudge in
                # disguise, which the product rules forbid. The score still
                # prints -- only the remedy suggestion is gated.
                _warn_tel = _measure_facade._absolute_context_tokens(quality_data, _session_id)
                _size_ok = _warn_tel is not None and _warn_tel["tokens"] >= _measure_facade._SIZE_NUDGE_MIN_TOKENS
                if result["score"] < 50:
                    if _size_ok:
                        print(f"[Token Optimizer] Quality {result['score']}/100 (critical). /clear with checkpoint.")
                    else:
                        print(f"[Token Optimizer] Quality {result['score']}/100 (critical).")
                else:
                    if _size_ok:
                        print(f"[Token Optimizer] Quality {result['score']}/100. /compact.")
                    else:
                        print(f"[Token Optimizer] Quality {result['score']}/100.")
            _measure_facade._write_quality_cache(cache_path, result)

        # Nudge follow-through: if PostCompact triggered this run (force=True)
        # and a nudge preceded the compact, measure the actual fill_pct recovery.
        if force and result.get("fill_pct", 0) > 0:
            # A6: the nudge fires during a prior UserPromptSubmit process; this
            # follow-through runs in a SEPARATE PostCompact process. Fire-state reaches
            # us via the atomic per-session quality cache (carried forward at the top of
            # this function). Fall back to prev_result directly in case the carry did not
            # run, so a heeded session is never misclassified as ignored. Wrapped so a
            # corrupt cache value can never crash the host hook — it degrades to "no
            # credit", not a traceback.
            try:
                nudge_fill = result.get("_nudge_fill_pct_at_fire") or prev_result.get("_nudge_fill_pct_at_fire", 0)
                if nudge_fill > 0:
                    current_fill = result["fill_pct"]
                    fill_delta = nudge_fill - current_fill
                    ft_sid = _measure_facade.Path(cache_path).stem.replace("quality-cache-", "", 1) if cache_path else None
                    # A2 temporal gate: only credit a compaction that followed the nudge
                    # within the window. A compaction long after the nudge was not its
                    # follow-through, so it must not borrow the nudge's credit.
                    fire_epoch = result.get("_nudge_fire_epoch") or prev_result.get("_nudge_fire_epoch", 0)
                    within_window = fire_epoch and (_measure_facade.time.time() - fire_epoch) <= _measure_facade._NUDGE_FOLLOWTHROUGH_WINDOW_SECONDS
                    # A2 dedup: if a checkpoint_restore already credited this session's
                    # recovery for this compaction (same SessionStart cycle), the nudge
                    # must not double-credit the same tokens.
                    already_credited = _measure_facade._checkpoint_restore_credited_recently(ft_sid)
                    should_credit = fill_delta > 5 and within_window and not already_credited
                    # Consume the fire-state FIRST, then credit only if the consume was
                    # actually persisted. The fire-state lives in exactly one place (this
                    # per-session cache), so once it is cleared from disk no later
                    # PostCompact can re-enter this block for the same nudge — that makes
                    # the credit idempotent without a second DB query. If the process
                    # dies between here and the credit, or the clear-write fails, we skip
                    # the credit (a conservative under-count) rather than risk a
                    # double-count that would inflate the savings number.
                    result.pop("_nudge_fill_pct_at_fire", None)
                    result.pop("_nudge_fire_epoch", None)
                    consumed = _measure_facade._write_quality_cache(cache_path, result)
                    if should_credit and consumed:
                        context_size = _measure_facade.detect_context_window()[0]
                        measured_tokens_recovered = int(context_size * fill_delta / 100)
                        _measure_facade._log_compression_event(
                            feature="quality_nudge",
                            original_text=" " * (measured_tokens_recovered * 4),
                            compressed_text=f"nudge_followthrough:fill={nudge_fill}->{current_fill}",
                            session_id=ft_sid,
                            detail=f"measured_recovery: fill {nudge_fill}%->{current_fill}% = {measured_tokens_recovered} tokens on {context_size} context",
                            verified=True,
                        )
            except Exception:
                pass

        # Progressive checkpoints (v3.0)
        if _measure_facade._PROGRESSIVE_ENABLED and result.get("fill_pct", 0) > 0:
            _measure_facade._maybe_progressive_checkpoint(
                fill_pct=result["fill_pct"],
                cache_path=cache_path,
                result=result,
                filepath=filepath,
            )

        _measure_facade._maybe_checkpoint_on_quality_or_milestone(
            quality_data=quality_data,
            cache_path=cache_path,
            result=result,
            filepath=filepath,
        )

        return result.get("score")
    finally:
        _measure_facade._release_quality_lock(_qlock)


def _advice_norm_path(p):
    """Same file, same key: forward slashes, collapsed ./ and ../, no trailing
    slash; case-folded only for Windows drive paths."""
    _measure_facade = _compat.facade()
    if not isinstance(p, str) or not p.strip():
        return None
    s = p.strip().replace("\\", "/")
    s = _measure_facade.posixpath.normpath(s)
    if s in (".", "/"):
        return None
    if _measure_facade._ADVICE_WIN_DRIVE_RE.match(s):
        s = s.lower()
    return s


def _advice_tool_targets(name, inp):
    """Normalised paths a Read/Grep/Glob/Bash tool_use targets (may be empty)."""
    _measure_facade = _compat.facade()
    if name not in _measure_facade._ADVICE_REREAD_TOOLS or not isinstance(inp, dict):
        return []
    out = []
    if name == "Read":
        cands = [inp.get("file_path"), inp.get("path")]
    elif name in ("Grep", "Glob"):
        cands = [inp.get("path")]
    else:  # Bash: path-like tokens in the command line
        cmd = inp.get("command")
        cands = []
        if isinstance(cmd, str):
            for tok in _measure_facade._ADVICE_PATH_TOKEN_RE.findall(cmd[:4000]):
                if "/" in tok or "\\" in tok:
                    cands.append(tok)
    for c in cands:
        n = _measure_facade._advice_norm_path(c)
        if n:
            out.append(n)
    return out


def _advice_result_text(content):
    _measure_facade = _compat.facade()
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and isinstance(b.get("text"), str):
                parts.append(b["text"])
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    return ""


def _advice_session_data(path):
    """Replay + measurement data for a main-conversation transcript.

    Returns a dict:
      turns          per-request context size (input + cache_read + cache_creation)
                     of each main-thread assistant usage row, in order; streamed
                     chunks sharing a requestId collapse to the largest row.
      cache_reads    cache_read_input_tokens per turn (same indexing). This is
                     the billed aggregate, while `turns` is executor occupancy
                     (#287). On advisor sessions avoided reads (derived from
                     `turns`) are measured against the larger billed total, so
                     avoided_share_pct can only understate the saving.
      boundaries     indexes in `turns` that directly follow a compact_boundary.
      model          newest model id on a main-thread row, or None.
      post_compact_ctx  context size of the first request after each recorded
                     boundary that has a following turn.
      rereads        per such boundary: tokens of Read/Grep/Glob/Bash tool
                     results, in the next _ADVICE_REREAD_TURNS assistant turns,
                     that target a path (same normalised path) already targeted
                     by one of those tools before the boundary.
    Sidechain/subagent rows are ignored throughout.
    """
    _measure_facade = _compat.facade()
    turns, cache_reads, boundaries, model = [], [], set(), None
    seen = set()            # normalised paths touched so far (cumulative)
    bounds = []             # [start_turn_idx, frozenset(prior paths), reread_tokens]
    pending = {}            # tool_use id -> bounds entry (None when not a re-read)
    prev_req = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if ('"assistant"' not in line and "compact_boundary" not in line
                        and '"tool_result"' not in line):
                    continue
                try:
                    rec = _measure_facade.json.loads(line)
                except (_measure_facade.json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(rec, dict):
                    continue
                rtype = rec.get("type")
                if rtype == "system" and rec.get("subtype") == "compact_boundary":
                    idx = len(turns)
                    if idx not in boundaries:
                        boundaries.add(idx)
                        bounds.append([idx, frozenset(seen), 0])
                    continue
                if rec.get("isSidechain") is True or rec.get("agentId"):
                    continue
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                if rtype == "user":
                    content = msg.get("content")
                    if isinstance(content, list):
                        for blk in content:
                            if (isinstance(blk, dict) and blk.get("type") == "tool_result"):
                                ent = pending.pop(blk.get("tool_use_id"), None)
                                if ent is not None:
                                    ent[2] += _measure_facade._estimate_tokens(
                                        _measure_facade._advice_result_text(blk.get("content")))
                    continue
                if rtype != "assistant" or msg.get("model") == "<synthetic>":
                    continue
                u = msg.get("usage")
                if not isinstance(u, dict):
                    continue
                # Cache-read traffic is billed aggregate usage; occupancy is
                # only the final executor iteration of an advisor response.
                cr = _measure_facade._safe_int(u.get("cache_read_input_tokens"))
                ctx = _measure_facade._context_token_count(_measure_facade._context_usage(u))
                if ctx <= 0:
                    continue
                m = msg.get("model")
                if isinstance(m, str) and m:
                    model = m
                req = rec.get("requestId") or msg.get("id")
                if req is not None and req == prev_req and turns:
                    # streamed chunk of the same request: keep the largest usage
                    turns[-1] = max(turns[-1], ctx)
                    cache_reads[-1] = max(cache_reads[-1], cr)
                else:
                    prev_req = req
                    turns.append(ctx)
                    cache_reads.append(cr)
                tidx = len(turns) - 1
                content = msg.get("content")
                if isinstance(content, list):
                    # Window owner: the latest boundary at or before this turn.
                    owner = None
                    for b in bounds:
                        if b[0] <= tidx < b[0] + _measure_facade._ADVICE_REREAD_TURNS:
                            owner = b
                    for blk in content:
                        if not (isinstance(blk, dict) and blk.get("type") == "tool_use"):
                            continue
                        targets = _measure_facade._advice_tool_targets(blk.get("name"), blk.get("input"))
                        if not targets:
                            continue
                        if owner is not None and blk.get("id") and any(
                                t in owner[1] for t in targets):
                            pending[blk["id"]] = owner
                        seen.update(targets)
    except (OSError, PermissionError):
        # `unreadable` lets compact_advice flag the replay as partial instead
        # of silently counting one session fewer.
        return {"turns": [], "cache_reads": [], "boundaries": set(), "model": None,
                "post_compact_ctx": [], "rereads": [], "unreadable": True}
    post_ctx, rereads = [], []
    for b in bounds:
        if b[0] < len(turns):
            post_ctx.append(turns[b[0]])
            rereads.append(b[2])
    return {"turns": turns, "cache_reads": cache_reads, "boundaries": boundaries,
            "model": model, "post_compact_ctx": post_ctx, "rereads": rereads}


def _advice_session_turns(path):
    """(turns, boundaries, model) -- the replay subset of _advice_session_data."""
    _measure_facade = _compat.facade()
    d = _measure_facade._advice_session_data(path)
    return d["turns"], d["boundaries"], d["model"]


def _advice_median(values):
    _measure_facade = _compat.facade()
    v = sorted(values)
    n = len(v)
    if n == 0:
        return 0
    mid = n // 2
    return int(v[mid]) if n % 2 else int(round((v[mid - 1] + v[mid]) / 2))


def _advice_measured_assumptions(rereads, post_ctx):
    """Median re-read tokens and post-compact context from the user's own
    recorded compactions, each with its n; a stated default below
    _ADVICE_MIN_MEASURED real compactions."""
    _measure_facade = _compat.facade()
    def _pick(values, default):
        _measure_facade = _compat.facade()
        n = len(values)
        if n >= _measure_facade._ADVICE_MIN_MEASURED:
            return {"value": _measure_facade._advice_median(values), "n": n,
                    "source": "measured"}
        return {"value": default, "n": n, "source": _measure_facade._ADVICE_ASSUMED_TAG}
    return {
        "reread_tokens_per_compaction": _pick(rereads, _measure_facade._ADVICE_REREAD_DEFAULT),
        "post_compact_context_tokens": _pick(post_ctx, _measure_facade._ADVICE_POST_COMPACT_CONTEXT),
    }


def _advice_replay(turns, boundaries, window, post_compact=None):
    """Replay a session against a candidate compact window.

    Recorded compactions always happen (a compact_boundary, or any drop in
    context, ends a segment and resyncs the replay to the recorded value). The
    candidate can only ADD compactions inside a segment: a segment whose real
    peak never exceeded `window` is untouched, so a candidate at or above the
    session's real behaviour is a no-op. Returns (extra_compactions,
    avoided_tokens), never negative; avoided = prompt tokens the earlier
    compaction would have shaved off later requests in the segment.
    """
    _measure_facade = _compat.facade()
    if not turns:
        return 0, 0
    segments, cur = [], [0]
    for i in range(1, len(turns)):
        if i in boundaries or turns[i] < turns[i - 1]:
            segments.append(cur)
            cur = []
        cur.append(i)
    segments.append(cur)
    extra = 0
    avoided = 0
    for seg in segments:
        if window >= max(turns[i] for i in seg):
            continue
        v = turns[seg[0]]
        prev = v
        for i in seg[1:]:
            ctx = turns[i]
            v += ctx - prev
            if v >= window:
                extra += 1
                v = post_compact + (ctx - prev)
            avoided += max(0, ctx - v)
            prev = ctx
    return extra, avoided


def _advice_quality_by_fill_band():
    """Average recorded quality score per model-fill band across the quality caches."""
    _measure_facade = _compat.facade()
    sums = {"<50%": [0, 0], "50-70%": [0, 0], "70-80%": [0, 0], "80%+": [0, 0]}
    seen = set()
    for d in _measure_facade._status_bar_quality_cache_dirs():
        try:
            files = list(d.glob("quality-cache-*.json"))
        except OSError:
            continue
        for f in files:
            key = str(f)
            if key in seen:
                continue
            seen.add(key)
            try:
                q = _measure_facade.json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(q, dict):
                continue
            score = q.get("score")
            cfd = (q.get("breakdown") or {}).get("context_fill_degradation") or {}
            fill = cfd.get("model_fill_pct")
            if fill is None:
                fill = cfd.get("fill_pct")
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                continue
            if not isinstance(fill, (int, float)) or isinstance(fill, bool):
                continue
            band = ("<50%" if fill < 50 else "50-70%" if fill < 70
                    else "70-80%" if fill < 80 else "80%+")
            sums[band][0] += score
            sums[band][1] += 1
    return {
        band: {"sessions": n, "avg_score": round(total / n, 1) if n else None}
        for band, (total, n) in sums.items()
    }


def _advice_billing_mode():
    """'api' | 'subscription' | 'unknown'. Only labels how dollars are read."""
    _measure_facade = _compat.facade()
    try:
        return _measure_facade.keepwarm_billing_mode()
    except Exception:
        return "unknown"


def _advice_round_pct(n, d):
    _measure_facade = _compat.facade()
    return round(n / d * 100, 1) if d else 0.0


def compact_advice(days=None, max_sessions=None, deadline_seconds=None):
    """Build the compact-advice report dict. Purely read-only; never raises.

    max_sessions / deadline_seconds cap the work (newest sessions first) for
    the coach path; `truncated` says when a cap cut the scan short.
    """
    _measure_facade = _compat.facade()
    out = {
        "schema": 2,
        "days": days,
        "estimate": True,
        "billing": _measure_facade._advice_billing_mode(),
        "sessions_scanned": 0,
        "sessions_replayed": 0,
        "recorded_compactions": 0,
        "total_cache_read_tokens": 0,
        "truncated": False,
        "too_thin": True,
        "default_window": None,
        "smallest_positive_window": None,
        "assumptions": {},
        "measurements": {},
        "windows": [],
        "quality_by_fill_band": {},
    }
    try:
        files = _measure_facade._find_all_jsonl_files(days=days)
    except Exception:
        files = []
    out["sessions_scanned"] = len(files)
    if max_sessions is not None and len(files) > max_sessions:
        files = files[:max(0, int(max_sessions))]
        out["truncated"] = True
    deadline = (None if deadline_seconds is None
                else _measure_facade.time.monotonic() + float(deadline_seconds))

    sessions = []
    model_counts = {}
    all_rereads, all_post_ctx = [], []
    for jf, _mtime, _proj in files:
        if deadline is not None and _measure_facade.time.monotonic() > deadline:
            out["truncated"] = True
            break
        try:
            d = _measure_facade._advice_session_data(jf)
        except Exception:
            out["truncated"] = True
            continue
        if d.get("unreadable"):
            out["truncated"] = True
        all_rereads.extend(d["rereads"])
        all_post_ctx.extend(d["post_compact_ctx"])
        if len(d["turns"]) < 2:
            continue
        sessions.append(d)
        if d["model"]:
            model_counts[d["model"]] = model_counts.get(d["model"], 0) + 1
    out["sessions_replayed"] = len(sessions)
    out["recorded_compactions"] = len(all_post_ctx)
    total_cache_read = sum(sum(d["cache_reads"]) for d in sessions)
    out["total_cache_read_tokens"] = total_cache_read

    meas = _measure_facade._advice_measured_assumptions(all_rereads, all_post_ctx)
    reread_tok = meas["reread_tokens_per_compaction"]["value"]
    post_ctx = meas["post_compact_context_tokens"]["value"]
    out["measurements"] = meas
    out["assumptions"] = {
        "post_compact_context_tokens": post_ctx,
        "reread_tokens_per_compaction": reread_tok,
        "summary_output_tokens": _measure_facade._ADVICE_SUMMARY_OUTPUT_TOKENS,
        "summary_output_tokens_source": "assumed; the summary length is not recoverable from a transcript",
        "avoided_tokens_priced_as": "cache_read of the session's model card",
        "compaction_cost_priced_as": (
            "summary_output_tokens x output + post_compact_context_tokens x "
            "cache_write + reread_tokens_per_compaction x (input + cache_write), "
            "all on the session's model card (the reread charge is deliberately "
            "conservative)"),
        "note": (
            "A recorded compaction always happens in the replay; a candidate "
            "window can only add compactions before it. Your own resolved "
            "window is the reference row: 0 extra, 0 net by construction. "
            "Dollars are API-equivalent list-price estimates."),
    }

    tier_data = _measure_facade.PRICING_TIERS.get(_measure_facade._load_pricing_tier(), _measure_facade.PRICING_TIERS["anthropic"])
    modal_model = max(model_counts, key=model_counts.get) if model_counts else None
    try:
        default_res = _measure_facade._resolve_compact_window(modal_model)
        default_tokens = default_res["tokens"]
        out["default_window"] = {
            "tokens": default_tokens,
            "source": default_res["source"],
            "model": modal_model,
        }
    except Exception:
        default_tokens = _measure_facade._COMPACT_WINDOW_1M_DEFAULT

    candidates = list(_measure_facade._ADVICE_CANDIDATE_WINDOWS)
    if all(default_tokens != w for w in candidates):
        candidates.append(default_tokens)
    candidates.sort()

    for w in candidates:
        # The row for the user's own window, and anything above it, is the
        # reference behaviour: nothing is added, so nothing is computed.
        reference = w >= default_tokens
        extra_total = 0
        avoided_tokens = 0
        avoided_usd = 0.0
        cost_usd = 0.0
        affected = 0
        if not reference:
            for d in sessions:
                extra, avoided = _measure_facade._advice_replay(
                    d["turns"], d["boundaries"], w, post_compact=post_ctx)
                if extra <= 0 and avoided <= 0:
                    continue
                extra_total += extra
                avoided_tokens += avoided
                if extra > 0:
                    affected += 1
                rates = _measure_facade._claude_rates_for_model(d["model"], tier_data) or {}
                in_rate = float(rates.get("input", 0.0) or 0.0)
                read_rate = float(rates.get("cache_read", in_rate) or 0.0)
                write_rate = float(rates.get("cache_write", in_rate) or 0.0)
                out_rate = float(rates.get("output", 0.0) or 0.0)
                avoided_usd += avoided * read_rate / 1e6
                cost_usd += extra * (
                    _measure_facade._ADVICE_SUMMARY_OUTPUT_TOKENS * out_rate
                    + post_ctx * write_rate
                    + reread_tok * (in_rate + write_rate)) / 1e6
        cost_tokens = extra_total * (_measure_facade._ADVICE_SUMMARY_OUTPUT_TOKENS + post_ctx + reread_tok)
        is_default = w == default_tokens
        label = f"{w // 1000}K" + (" default" if is_default else "")
        out["windows"].append({
            "window": w,
            "label": label,
            "is_default": is_default,
            "reference": reference,
            "extra_compactions": extra_total,
            "affected_sessions": affected,
            "affected_share": (round(affected / len(sessions), 3) if sessions else 0),
            "cache_read_tokens_avoided": avoided_tokens,
            "avoided_share_pct": _measure_facade._advice_round_pct(avoided_tokens, total_cache_read),
            "compaction_cost_tokens": cost_tokens,
            "net_tokens": avoided_tokens - cost_tokens,
            "net_usd": round(avoided_usd - cost_usd, 2),
            "net_usd_unrounded": avoided_usd - cost_usd,
            "estimate": True,
        })

    # Smallest window whose net stays positive (after the measured costs) at
    # every larger candidate below the user's own window. "Net" here is the
    # priced one: a cache-read token is far cheaper than the output and
    # cache-write tokens a compaction spends, so a positive token net can still
    # be a loss.
    below = sorted((e for e in out["windows"] if not e["reference"]),
                   key=lambda e: ((_measure_facade := _compat.facade()), e["window"])[1])
    smallest = None
    for i, e in enumerate(below):
        if all(x["net_usd_unrounded"] > 0 for x in below[i:]):
            smallest = e["window"]
            break
    out["smallest_positive_window"] = smallest

    out["too_thin"] = (
        len(sessions) < _measure_facade._ADVICE_MIN_SESSIONS
        or (out["recorded_compactions"] == 0
            and all(e["extra_compactions"] == 0 for e in out["windows"]))
    )
    try:
        out["quality_by_fill_band"] = _measure_facade._advice_quality_by_fill_band()
    except Exception:
        out["quality_by_fill_band"] = {}
    return out


def _advice_cache_path():
    _measure_facade = _compat.facade()
    return _measure_facade.SNAPSHOT_DIR / "compact_advice_cache.json"


def _advice_coach_view(rep):
    """The small `compact_advice` block coach --json carries."""
    _measure_facade = _compat.facade()
    dw = rep.get("default_window") or {}
    return {
        "estimate": True,
        "days": rep.get("days"),
        "billing": rep.get("billing"),
        "dollars": "API-equivalent list-price estimates, not a bill",
        "resolved_window": {
            "tokens": dw.get("tokens"),
            "source": dw.get("source"),
            "model": dw.get("model"),
        },
        "sessions_replayed": rep.get("sessions_replayed"),
        "recorded_compactions": rep.get("recorded_compactions"),
        "truncated": bool(rep.get("truncated")),
        "too_thin": bool(rep.get("too_thin")),
        "rows": [] if rep.get("too_thin") else [
            {k: e.get(k) for k in (
                "label", "window", "is_default", "extra_compactions",
                "cache_read_tokens_avoided", "avoided_share_pct",
                "compaction_cost_tokens", "net_tokens", "net_usd")}
            for e in rep.get("windows", [])
        ],
        "smallest_positive_window": rep.get("smallest_positive_window"),
        "assumptions": {k: dict(v) for k, v in (rep.get("measurements") or {}).items()},
    }


def _coach_compact_advice_block(days=None):
    """Capped, cached, fail-open compact-advice block for `coach --json`.

    Newest `_ADVICE_COACH_MAX_SESSIONS` sessions within a wall-clock budget;
    the result is cached for six hours. Any failure returns None so coach never
    fails because of it.
    """
    _measure_facade = _compat.facade()
    try:
        cache_path = _measure_facade._advice_cache_path()
        try:
            cached = _measure_facade.json.loads(cache_path.read_text(encoding="utf-8"))
            if (isinstance(cached, dict) and cached.get("days") == days
                    and cached.get("_schema") == 2
                    and _measure_facade.time.time() - float(cached.get("_cached_ts", 0))
                    < _measure_facade._ADVICE_COACH_CACHE_TTL_SECONDS):
                return {k: v for k, v in cached.items() if not k.startswith("_")}
        except (OSError, ValueError, TypeError):
            pass
        rep = _measure_facade.compact_advice(days=days, max_sessions=_measure_facade._ADVICE_COACH_MAX_SESSIONS,
                             deadline_seconds=_measure_facade._ADVICE_COACH_BUDGET_SECONDS)
        block = _measure_facade._advice_coach_view(rep)
        try:
            record = dict(block)
            record["_cached_ts"] = _measure_facade.time.time()
            record["_schema"] = 2
            _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = _measure_facade.tempfile.mkstemp(
                prefix=".compact_advice.", suffix=".tmp", dir=str(_measure_facade.SNAPSHOT_DIR))
            try:
                if hasattr(_measure_facade.os, "fchmod"):
                    _measure_facade.os.fchmod(fd, 0o600)
                with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as fh:
                    _measure_facade.json.dump(record, fh)
                _measure_facade.os.replace(tmp_name, str(cache_path))
            except Exception:
                try:
                    _measure_facade.os.unlink(tmp_name)
                except OSError:
                    pass
        except Exception:
            pass
        return block
    except Exception:
        return None


def _advice_usd(v):
    _measure_facade = _compat.facade()
    return f"{'-' if v < 0 else ''}${abs(v):,.2f}"


def _compact_advice_cli(args):
    """`measure.py compact-advice [--json] [--days N]` — replay history against
    candidate compact windows. Read-only; prints estimates, never settings."""
    _measure_facade = _compat.facade()
    output_json = "--json" in args
    days = 30
    i = 0
    while i < len(args):
        if args[i] == "--days" and i + 1 < len(args):
            try:
                days = max(1, int(args[i + 1]))
            except ValueError:
                pass
            i += 2
        else:
            i += 1
    report = _measure_facade.compact_advice(days=days)
    if output_json:
        print(_measure_facade.json.dumps(report, indent=2))
        return
    print("\nTOKEN OPTIMIZER: COMPACT-WINDOW ADVICE (estimates only)")
    print("=" * 58)
    print(f"  Sessions scanned: {report['sessions_scanned']} "
          f"({report['sessions_replayed']} replayed, last {report['days']} days, "
          f"{report['recorded_compactions']} recorded compactions)")
    dw = report.get("default_window") or {}
    if dw.get("tokens"):
        print(f"  Your resolved window: {dw['tokens']:,} ({dw.get('source', '')})")
    if report["too_thin"]:
        print("\n  History is too thin to estimate: not enough sessions ever")
        print("  reached a compaction context in this window. Re-run after more")
        print("  real sessions (or widen --days).")
    else:
        print("\n  Replays of YOUR sessions against candidate compact windows.")
        print("  Recorded compactions always happen; a smaller window can only add")
        print("  compactions before them. Your own window is the reference row")
        print("  (0 extra, 0 net by construction).")
        print(f"  Total cache-read tokens in this window: "
              f"{report['total_cache_read_tokens']:,}\n")
        print(f"  {'window':<16} {'extra':>6} {'cache-read avoided':>19} {'share':>7} "
              f"{'compact cost':>13} {'net tokens':>12} {'net $ API-equivalent':>21} "
              f"{'sessions':>9}")
        for e in report["windows"]:
            print(f"  {e['label']:<16} {e['extra_compactions']:>6} "
                  f"{e['cache_read_tokens_avoided']:>19,} "
                  f"{e['avoided_share_pct']:>6.1f}% "
                  f"{e['compaction_cost_tokens']:>13,} {e['net_tokens']:>12,} "
                  f"{_measure_facade._advice_usd(e['net_usd']):>21} {e['affected_sessions']:>9}")
        print("\n  Token counts are not equal in price: a cache read costs far less than the")
        print("  output and cache-write tokens a compaction spends, so the dollar column")
        print("  is the priced net.")
        if report.get("billing") == "api":
            print("  Dollars are API-equivalent: tokens priced at the list rate.")
        else:
            print("  Dollars are API-equivalent: tokens priced at the list rate. On a")
            print("  subscription this is not a bill, it is a size-of-the-effect gauge.")
        q = report.get("quality_by_fill_band") or {}
        if any(v.get("sessions") for v in q.values()):
            print("\n  Average quality score by model-fill band (recorded caches):")
            for band, v in q.items():
                if v["sessions"]:
                    print(f"    {band:<7} avg {v['avg_score']:>5}  ({v['sessions']} sessions)")
        print("  Compaction can drop early instructions; the quality score does not measure that.")
        # Half of the score IS the fill curve, so this table is not independent proof.
        print("  Half of the score is context fill itself, so read this as the scoring")
        print("  curve at work, not as separate evidence that fuller sessions go worse.")
    print("\n  Assumptions (measured from your own recorded compactions where possible):")
    for name, mm in (report.get("measurements") or {}).items():
        print(f"    {name}: {mm['value']:,} ({mm['source']}, n={mm['n']})")
    print(f"    summary_output_tokens: {_measure_facade._ADVICE_SUMMARY_OUTPUT_TOKENS:,} "
          f"({report['assumptions'].get('summary_output_tokens_source', 'assumed')})")
    for ln in _measure_facade.textwrap.wrap(
            "compaction_cost_priced_as: "
            + report["assumptions"].get("compaction_cost_priced_as", ""),
            width=88, initial_indent="    ", subsequent_indent="      "):
        print(ln)
    if report.get("smallest_positive_window") and not report["too_thin"]:
        w = report["smallest_positive_window"]
        print(f"\n  Smallest window whose net stays positive after the measured costs: {w // 1000}K.")
        print("  To try one: /autocompact <n>")
    elif not report["too_thin"]:
        print("\n  No candidate window had a net that stays positive after the measured costs.")
    print()


def _format_window_size(window):
    """1_000_000 -> "1M", 600_000 -> "600k", 512 -> "512"."""
    _measure_facade = _compat.facade()
    if window >= 1_000_000 and window % 1_000_000 == 0:
        return f"{window // 1_000_000}M"
    if window >= 1000:
        return f"{window // 1000}k"
    return str(window)


def _safe_note_text(text, extra=None, limit=None):
    """Restrict an UNTRUSTED provenance string to a charset that cannot carry
    newlines or control characters, collapse whitespace, and hard-cap it."""
    _measure_facade = _compat.facade()
    text = "".join(c for c in (text or "") if c.isalnum() or c in " _-:/+" + extra)
    return " ".join(text.split())[:limit].strip()


def _format_reduced_window_note(cached, model_window, model_size):
    """The clause for a fill measured against a REDUCED compact window (#249).

    With ``autoCompactWindow`` (or CLAUDE_CODE_AUTO_COMPACT_WINDOW) below the
    model window, compute_quality_score divides by the compact window: the
    percentage answers "how close to auto-compact", which is what nudges gate
    on. The note used to name the model window beside it ("87% (1M window)"),
    so the reader took the opposite denominator. Returns "" when no reduction
    is in effect or the cache predates the fields, so the plain note is used.
    """
    _measure_facade = _compat.facade()
    if not cached.get("compact_window_reduced"):
        return ""
    try:
        compact = int(cached.get("compact_window") or 0)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not 0 < compact < model_window:
        return ""
    model_fill = cached.get("model_fill_pct")
    if not isinstance(model_fill, (int, float)):
        fill = cached.get("fill_pct")
        model_fill = fill * compact / model_window if isinstance(fill, (int, float)) else None
    share = f"{model_fill:.0f}% of " if model_fill is not None else ""
    source = _measure_facade._safe_note_text(cached.get("compact_window_source"), extra="=.[]'", limit=64)
    origin = f", source: {source}" if source else ""
    return f" (of {_measure_facade._format_window_size(compact)} compact window, {share}{model_size} model window{origin})"


def _format_context_fill(fill_pct, cached):
    """The fill phrase every model-facing message uses, e.g. "38% of 1M window".

    Leads with the share of the MODEL window: the number the user's own context
    meter shows. With a reduced compact window the compact share follows as
    distance to auto-compact. Models quote the first number they read; leading
    with the compact share made sessions report "76% full" and offer handovers
    at 38% real fill.
    """
    _measure_facade = _compat.facade()
    cached = cached or {}
    try:
        model_window = int(cached.get("model_context_window") or 0)
        compact = int(cached.get("compact_window") or 0)
    except (TypeError, ValueError, OverflowError):
        model_window = compact = 0
    model_fill = cached.get("model_fill_pct")
    if (cached.get("compact_window_reduced") and 0 < compact < model_window
            and isinstance(fill_pct, (int, float))):
        if not isinstance(model_fill, (int, float)):
            model_fill = fill_pct * compact / model_window
        source = _measure_facade._safe_note_text(cached.get("compact_window_source"), extra="=.[]'", limit=64)
        origin = f", source: {source}" if source else ""
        return (f"{model_fill:.0f}% of {_measure_facade._format_window_size(model_window)} window "
                f"(auto-compact at {_measure_facade._format_window_size(compact)}, {fill_pct:.0f}% of the way there{origin})")
    if not isinstance(fill_pct, (int, float)):
        return "?%"
    return f"{fill_pct:.0f}%{_measure_facade._format_window_note(cached)}"


def _model_window_fill(result):
    """Share of the model window for display; fill_pct when no reduction applies."""
    _measure_facade = _compat.facade()
    result = result or {}
    if result.get("compact_window_reduced") and isinstance(result.get("model_fill_pct"), (int, float)):
        return result["model_fill_pct"]
    return result.get("fill_pct")


def _format_window_note(cached):
    """Render the denominator and its origin, e.g. " (1M window, source: default 1M)".

    A fill percentage is only as trustworthy as the window it was divided by, and
    until now the window was never shown. Someone seeing "89% capacity" had no way
    to tell whether the denominator was 200k or 1M -- the numerator is correct in
    both cases, so a 5x-wrong reading looks exactly as plausible as a right one.
    detect_context_window() already computes a provenance string;
    every call site discarded it.

    Returns "" for a cache written before this field existed, so an older cache
    degrades to the previous message rather than raising.
    """
    _measure_facade = _compat.facade()
    window = cached.get("model_context_window")
    if not window:
        return ""
    try:
        window = int(window)
    except (TypeError, ValueError, OverflowError):
        # OverflowError is what int(float("inf")) raises, and JSON parses the
        # non-standard literal Infinity by default, so a poisoned cache reaches
        # here as a float that cannot be narrowed.
        return ""
    if window <= 0:
        return ""

    size = _measure_facade._format_window_size(window)

    # A fill measured against a reduced compact window is labelled with THAT
    # window, and the model window beside it, never as a share of the model
    # window alone (#249).
    reduced = _measure_facade._format_reduced_window_note(cached, window, size)
    if reduced:
        return reduced

    # The provenance string is UNTRUSTED. It embeds env values (CLAUDE_MODEL,
    # ANTHROPIC_MODEL) and config fields verbatim, and this note is injected into
    # the assistant's context, where text reads as instructions. Trimming the
    # trailing hint is not sanitisation: everything BEFORE the first "." or "("
    # survived, unbounded, so `CLAUDE_MODEL="ignore prior instructions ... haiku"`
    # would have been rendered whole into the model's context.
    #
    # Three defenses, in order: cut to the origin clause, restrict to a charset
    # that cannot carry newlines or control characters, and hard-cap the length
    # so a multi-megabyte value cannot bloat the very window this tool protects.
    source = (cached.get("model_context_window_source") or "")
    source = source.split(".")[0].split("(")[0].strip()
    source = _measure_facade._safe_note_text(source, limit=_measure_facade._WINDOW_SOURCE_MAX_CHARS)
    return f" ({size} window, source: {source})" if source else f" ({size} window)"


def _verbosity_measured_savings(baseline_avg, post_nudge_outputs):
    """Estimated positive output reduction against the session's prior mean.

    This before/after comparison has no control and is not a causal measure.
    Signed changes are retained separately in the calibration event detail.
    """
    _measure_facade = _compat.facade()
    try:
        baseline = int(baseline_avg) if baseline_avg is not None else 0
    except (TypeError, ValueError):
        return 0
    try:
        outs = [int(o) for o in (post_nudge_outputs or []) if int(o) > 0]
    except (TypeError, ValueError):
        return 0
    if not outs:
        return 0
    post_avg = sum(outs) / len(outs)
    delta = baseline - post_avg
    if delta <= 0:
        return 0
    return int(delta * len(outs))


def _transcript_request_outputs(filepath):
    """Output tokens of each API request in a transcript, in request order.

    Claude Code writes several assistant lines per request while it streams,
    each carrying the cumulative output so far, so the request's output is the
    MAX over its lines (the same fold ``_parse_session_jsonl`` applies). Lines
    with neither a requestId nor a message id stay separate requests. Returns
    ``[(positive_lines_before, output_tokens), ...]`` for requests with output
    above zero, where ``positive_lines_before`` counts the output-bearing
    assistant lines that precede the request's first one (used to place a
    boundary recorded in lines by an older nudge).
    """
    _measure_facade = _compat.facade()
    order = []
    by_key = {}
    seen_lines = 0
    for line in _measure_facade.Path(filepath).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = _measure_facade.json.loads(line)
        except (_measure_facade.json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage", {})
        out = _measure_facade._safe_int(usage.get("output_tokens", 0)) if isinstance(usage, dict) else 0
        if out <= 0:
            continue
        key = _measure_facade._usage_request_key(entry, msg)
        if key is None:
            key = f"__noreq__{len(order)}"
        slot = by_key.get(key)
        if slot is None:
            slot = [seen_lines, out]
            by_key[key] = slot
            order.append(slot)
        else:
            slot[1] = max(slot[1], out)
        seen_lines += 1
    return [(before, out) for before, out in order]


def _calibrate_prior_verbosity_nudges(session_id, filepath):
    """Book the before/after output change of a prior verbosity nudge (R5, D1).

    The booked row is an uncontrolled comparison and stays out of the realized
    total; the estimated tier credits only the net-of-control figure
    (``output_reread.summary_pool``). The signed change is kept in the detail.

    The nudge event itself books 0 -- there is no counterfactual at fire time.
    This pass runs on a later UserPromptSubmit turn: it finds the most recent
    ``verbosity_steer`` event for this session, reads how many pre-nudge turns
    and what pre-nudge average output it recorded, then measures the ACTUAL
    post-nudge output from the live transcript and books
    ``max(0, baseline - post_avg) x post_turns`` as a ``verbosity_steer_measured``
    event via :func:`_verbosity_measured_savings`. Without this wiring the
    measurement was dead code -- nothing in the real path ever computed the
    counterfactual, so a nudge's saving was never actually credited (D1).

    Idempotent per source nudge: a measured row records ``calibrates_ts=<ts>`` of
    the nudge it settled, and calibrated timestamps are skipped, so it never
    double-credits. When there are no post-nudge turns yet the nudge is left
    uncalibrated for a later turn to measure. Returns the measured tokens booked
    (0 if none). Best-effort: never raises into the caller.
    """
    _measure_facade = _compat.facade()
    try:
        if not filepath or not _measure_facade.Path(filepath).exists():
            return 0
        sid = str(session_id or "").strip()
        if not sid:
            return 0
        session_uuid, _ = _measure_facade._extract_session_uuid(sid)
        conn = _measure_facade._init_trends_db()
        try:
            rows = conn.execute(
                "SELECT timestamp, detail FROM savings_events "
                "WHERE event_type='verbosity_steer' "
                "AND (session_id = ? OR session_uuid = ?) "
                "AND detail LIKE '%pre_turns=%' "
                "ORDER BY timestamp DESC LIMIT 5",
                (sid, session_uuid),
            ).fetchall()
            done = conn.execute(
                "SELECT detail FROM savings_events "
                "WHERE event_type='verbosity_steer_measured' "
                "AND (session_id = ? OR session_uuid = ?)",
                (sid, session_uuid),
            ).fetchall()
        finally:
            conn.close()

        calibrated_ts = set()
        for (d,) in done:
            match = _measure_facade.re.search(r"calibrates_ts=(\S+)", d or "")
            if match:
                calibrated_ts.add(match.group(1))

        source = None
        for ts, detail in rows:
            if ts in calibrated_ts:
                continue
            source = (ts, detail or "")
            break
        if not source:
            return 0
        src_ts, src_detail = source
        m_turns = _measure_facade.re.search(r"pre_turns=(\d+)", src_detail)
        m_avg = _measure_facade.re.search(r"pre_avg_out=(\d+)", src_detail)
        if not m_turns or not m_avg:
            return 0
        pre_turns = int(m_turns.group(1))
        pre_avg = int(m_avg.group(1))

        # Output per API request in transcript order. A nudge that recorded
        # its boundary in requests ("unit=req") indexes the request list; an
        # older one recorded transcript lines, so the boundary is placed by
        # line position and only whole requests after it are counted.
        requests = _measure_facade._transcript_request_outputs(filepath)
        if "unit=req" in src_detail:
            post_outputs = [out for _, out in requests[pre_turns:]]
        else:
            post_outputs = [out for before, out in requests if before >= pre_turns]
        if not post_outputs:
            # No post-nudge turns yet: nothing to measure. Leave the nudge
            # uncalibrated so a later turn settles it.
            return 0

        measured = _measure_facade._verbosity_measured_savings(pre_avg, post_outputs)
        _measure_facade._log_savings_event(
            "verbosity_steer_measured",
            measured,
            session_id=sid,
            detail=(f"calibrates_ts={src_ts} post_turns={len(post_outputs)} "
                    f"pre_avg_out={pre_avg} signed_delta={int(pre_avg * len(post_outputs) - sum(post_outputs))}"),
        )
        return measured
    except Exception:
        return 0


def _canonical_session_id_for_compare(sid: str) -> str:
    """Canonicalize a session id for the verbosity-steer identity guard.

    Codex cache names retain rollout timestamps while live hook payloads carry
    the bare UUID, so both sides must be reduced to the same canonical form
    before the equality check.
    """
    _measure_facade = _compat.facade()
    if _measure_facade.detect_runtime() == 'codex':
        canonical, _ = _measure_facade._extract_session_uuid(sid)
        return canonical or sid
    return sid


def run_verbosity_steer(transcript_path=None, quiet=None, session_id=None):
    """Tiered conciseness nudge for UserPromptSubmit.

    Reads the quality cache to get fill_pct + score, then emits a JSON
    hookSpecificOutput payload on stdout (or returns it if quiet=True).

    Returns the JSON string if a nudge was emitted, empty string otherwise.

    Tiered messaging:
      25-74% fill + degraded session  → gentle nudge (floor is
                                        _VERBOSITY_NUDGE_MIN_FILL;
                                        SessionEfficiency gates it)
      75-89% fill                     → strong nudge with specific directives
      90%+ fill                        → suppressed (adding tokens makes it worse)

    Cooldown: max 3 nudges per session, 5 min between nudges.
    """
    _measure_facade = _compat.facade()
    try:
        filepath = None
        # Whether this transcript was told to us or inferred. _find_current_session_jsonl()
        # returns the most recently active transcript, which on a brand-new session
        # is somebody else's -- so a guessed path must clear a higher bar below.
        guessed = False
        if transcript_path and _measure_facade.Path(transcript_path).exists():
            filepath = _measure_facade.Path(transcript_path)
        else:
            filepath = _measure_facade._find_current_session_jsonl()
            guessed = True
        if not filepath:
            return ""
        cache_path = _measure_facade._quality_cache_path_for(filepath)
        cached = _measure_facade._read_quality_cache(cache_path) if cache_path.exists() else {}
        if not cached:
            return ""
        fill_pct = cached.get("fill_pct", 0) or 0
        score = cached.get("score", 100) or 100
        session_efficiency = cached.get("session_efficiency", 100)
        if session_efficiency is None:
            session_efficiency = 100
        # Session identity guard.
        #
        # This path reported another session's fill and quality as the user's own:
        # a nudge fired on the FIRST prompt of an empty session quoting fill 47%
        # / score 73, numbers that session could not have produced. The cache is
        # keyed by the resolved transcript, and nothing checked that the resolved
        # transcript was the live one.
        #
        # Silence is the correct failure mode. A missing nudge costs a small
        # optimization; a wrong one makes the assistant conserve tokens that were
        # never scarce, for the rest of the session.
        # sanitize_session_id() returns the sentinel "unknown" for empty or
        # unusable input, and "unknown" is truthy. Treating that as an identity
        # would make every caller that omits session_id fail the comparison and
        # silently kill all nudges -- the same class of invisible failure this
        # guard exists to prevent.
        raw_sid = str(session_id or "").strip()
        want_sid = _measure_facade.sanitize_session_id(raw_sid) if raw_sid else ""
        if want_sid == "unknown":
            want_sid = ""
        if want_sid:
            # Identity known: it must match, and a cache too old to carry one
            # cannot be vouched for either. Derived only when needed, and failing
            # closed on its own rather than by raising into the outer handler --
            # an exception here would look identical to "no nudge today".
            have_sid = ""
            try:
                have_sid = _measure_facade.sanitize_session_id(
                    _measure_facade.Path(str(cache_path)).stem.replace("quality-cache-", "", 1)
                )
            except (TypeError, ValueError):
                have_sid = ""
            have_sid = _measure_facade._canonical_session_id_for_compare(have_sid)
            want_sid = _measure_facade._canonical_session_id_for_compare(want_sid)
            if not have_sid or have_sid == "unknown" or have_sid != want_sid:
                # Say so. If this mismatch is structural to the environment
                # (container path translation, WSL mounts, a runtime emitting a
                # short session id) it recurs on every prompt, and a silent
                # return would disable the tool for good with nothing to grep.
                if not quiet:
                    _measure_facade.sys.stderr.write(
                        "[Token Optimizer] Skipping nudge: resolved session "
                        f"{have_sid or 'unknown'!s} does not match the live session "
                        f"{want_sid!s}.\n"
                    )
                return ""
        elif guessed:
            # No identity to check AND the transcript was inferred. Never fall
            # back to a guess: that combination is exactly the observed failure.
            if not quiet:
                _measure_facade.sys.stderr.write(
                    "[Token Optimizer] Skipping nudge: no session id to verify "
                    "the inferred transcript against.\n"
                )
            return ""

        # U5/D1: before deciding on a new nudge, settle any prior nudge in this
        # session whose post-nudge output we can now observe. The nudge event
        # books 0 at fire time; THIS is where the real counterfactual
        # (_verbosity_measured_savings) is computed and credited. Runs before the
        # cooldown/fill early-returns so a settled nudge is measured even on a
        # turn that does not itself nudge. Best-effort; never blocks the nudge.
        try:
            _measure_facade._calibrate_prior_verbosity_nudges(
                session_id or _measure_facade.os.environ.get("CLAUDE_SESSION_ID", ""), filepath)
        except Exception:
            pass

        window_note = _measure_facade._format_window_note(cached)

        # The window is provably wrong: more tokens were observed than it can
        # hold. Say so and stop, rather than nudging on a percentage derived
        # from it. Deliberately no second guess at the "real" window -- guessing
        # a denominator is the original defect.
        if cached.get("context_window_contradicted"):
            if not quiet:
                _measure_facade.sys.stderr.write(
                    f"[Token Optimizer] Context window looks misdetected{window_note}: "
                    "observed tokens exceed it, so the fill percentage is suppressed. "
                    "Set TOKEN_OPTIMIZER_CONTEXT_SIZE to your real window.\n"
                )
            return ""

        # Cooldown check
        import time as _vs_time
        _now = _vs_time.time()
        _nudge_count = cached.get("nudge_count", 0) or 0
        _last_nudge = cached.get("last_nudge_time", 0) or 0
        if _nudge_count >= _measure_facade._VERBOSITY_MAX_NUDGES:
            return ""
        if _now - _last_nudge < _measure_facade._VERBOSITY_COOLDOWN_SEC:
            return ""

        # At 90%+ fill, don't add more tokens
        if fill_pct >= 90:
            if not quiet:
                _measure_facade.sys.stderr.write(
                    f"[Token Optimizer] Context at {_measure_facade._format_context_fill(fill_pct, cached)} — consider /compact or /clear. "
                    f"Verbosity nudge suppressed to avoid adding tokens.\n"
                )
            return ""

        nudge = _measure_facade._verbosity_nudge_text(fill_pct, score, session_efficiency, cached)
        if not nudge:
            return ""

        payload = _measure_facade.json.dumps({
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": nudge,
            },
        })

        # U5: book 0 at nudge time. The old code booked a hardcoded
        # 10-15% x 800 ASSUMED saving the moment the nudge fired, without
        # measuring the counterfactual. In the competitor's data output actually
        # ROSE after the nudge, so the assumed figure was actively wrong. Now:
        # the nudge event books 0 (no counterfactual yet), and we record the
        # session's pre-nudge avg output so a later calibration pass can compute
        # the real measured saving via _verbosity_measured_savings (max(0,
        # baseline_avg - actual_post_nudge_avg) x turns). Never an estimate,
        # never a negative saving.
        try:
            _session_id = session_id or _measure_facade.os.environ.get("CLAUDE_SESSION_ID", "")

            # Empirical calibration: record the session's pre-nudge avg output
            # so a later pass can compare against the actual post-nudge output.
            _pre_nudge_avg_output = 0
            _pre_nudge_turns = 0
            try:
                if filepath and filepath.exists():
                    _turn_outputs = [
                        _o for _, _o in _measure_facade._transcript_request_outputs(filepath)]
                    if _turn_outputs:
                        _pre_nudge_avg_output = round(sum(_turn_outputs) / len(_turn_outputs))
                        _pre_nudge_turns = len(_turn_outputs)
            except Exception:
                pass

            _tier_label = 'strong' if fill_pct >= 75 else 'gentle'
            _detail = (f"fill={fill_pct:.0f}% score={score:.0f} tier={_tier_label}"
                       f" pre_avg_out={_pre_nudge_avg_output} pre_turns={_pre_nudge_turns}"
                       f" unit=req")
            _measure_facade._log_savings_event(
                "verbosity_steer",
                0,
                session_id=_session_id,
                detail=_detail,
            )
        except Exception:
            pass

        # Update cooldown in cache (best-effort, non-blocking)
        try:
            cached["nudge_count"] = _nudge_count + 1
            cached["last_nudge_time"] = _now
            cache_path.write_text(_measure_facade.json.dumps(cached), encoding="utf-8")
        except Exception:
            pass

        return payload
    except Exception:
        return ""


def _verbosity_nudge_text(fill_pct, score, session_efficiency, cached):
    """The lean-output nudge for a fill level, or "" when none applies.

    Shared by Claude Code / Codex (run_verbosity_steer) and Hermes
    (hermes-turn-context), so every runtime gets the same tiers and wording.
    ``cached`` is the quality-cache dict (or a small stand-in with
    ``model_context_window``) that ``_format_context_fill`` reads.

    Tiers:
      90%+ fill                        -> "" (adding tokens makes it worse)
      75-89% fill                      -> strong nudge
      _VERBOSITY_NUDGE_MIN_FILL-74% and session efficiency below
      _VERBOSITY_NUDGE_EFFICIENCY_THRESHOLD -> gentle nudge
    """
    _measure_facade = _compat.facade()
    if fill_pct >= 90:
        return ""
    # Determine nudge tier.
    #
    # The spartan/ADHD phrasing is used verbatim because it produced the
    # largest reduction in verbose output across multiple models while
    # maintaining answer quality. Notes on what the data showed:
    #   - Word ceilings were REMOVED. An earlier draft capped answers at
    #     80/120 words. A cap makes a model either truncate a genuinely
    #     complex answer or ignore the nudge, and a nudge that gets ignored
    #     is the original failure this rewrite exists to fix.
    #   - The real waste is elaboration, not preamble. The padding-focused
    #     wording targeted preamble and restatement and cut half as much,
    #     because verbose answers mostly repeat themselves rather than
    #     restate the question.
    #   - Quality held. On the hardest fixture (prompt-caching explainer,
    #     1159 -> 759 words) the spartan answer kept every concept and
    #     covered TTL MORE; what it dropped was the same point made six
    #     times instead of three.
    #   - Reasoning and deliverables are fenced off explicitly, because the
    #     nudge fires mid-task where "spartan" could otherwise be read as
    #     "write less code".
    #   - Opus is roughly half as responsive as Sonnet here. Do not quote
    #     the Sonnet figure as the product number.
    if fill_pct >= 75:
        return (
            f"[Token Optimizer] Context {_measure_facade._format_context_fill(fill_pct, cached)}, resource health {score:.0f}/100. "
            "Answer in a focused and spartan way, as if the user has ADHD. Cut everything "
            "that is not the answer itself. Think as long as you need, and size code, "
            "diffs and file contents to the task."
        )
    if (fill_pct >= _measure_facade._VERBOSITY_NUDGE_MIN_FILL
          and session_efficiency < _measure_facade._VERBOSITY_NUDGE_EFFICIENCY_THRESHOLD):
        return (
            f"[Token Optimizer] Context {_measure_facade._format_context_fill(fill_pct, cached)}, "
            f"session efficiency {session_efficiency:.0f}/100. "
            "Answer in a focused and spartan way, as if the user has ADHD. "
            "Think as long as you need, and size code, diffs and file contents to the task."
        )
    return ""
