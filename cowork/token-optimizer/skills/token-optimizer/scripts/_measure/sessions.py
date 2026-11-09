"""Definition-only implementations; mutable state belongs to measure."""
from __future__ import annotations
from . import _compat


def _find_all_jsonl_files(days=None):
    """Find all JSONL session files across all projects within the given day window."""
    _measure_facade = _compat.facade()
    if _measure_facade._use_codex_session_adapter():
        return _measure_facade.codex_session.find_all_jsonl_files(days if days is not None else 36500)

    # Hermes (cross-platform universality): Hermes sessions live in
    # ~/.hermes/state.db and are ingested into trends.db via hermes-rollup /
    # _collect_hermes_sessions — they are NOT stored as ~/.claude/projects
    # JSONL. Returning [] here keeps the JSONL fallback path of
    # _collect_trends_data() (reached by the hermes-exempted `dashboard`
    # command when trends.db is empty) from scanning ~/.claude, and is
    # defense-in-depth for the dispatch-blocked scan subcommands.
    if _measure_facade._use_hermes_session_adapter():
        return []
    # Antigravity sessions are read from ~/.gemini via antigravity-rollup and
    # _collect_antigravity_sessions, never ~/.claude/projects JSONL.
    if _measure_facade._use_antigravity_session_adapter():
        return []

    # Cursor (cross-platform universality, same as Hermes): sessions
    # live in the hook tally and are ingested via cursor-rollup, not
    # ~/.claude/projects JSONL. Returning [] keeps the JSONL fallback path of
    # the cursor-exempted dashboard command from scanning ~/.claude.
    if _measure_facade._use_cursor_session_adapter():
        return []

    projects_base = _measure_facade.CLAUDE_DIR / "projects"
    if not projects_base.exists():
        return []

    cutoff = _measure_facade.datetime.now().timestamp() - (days * 86400) if days is not None else float("-inf")
    results = []
    for project_dir in projects_base.iterdir():
        if not project_dir.is_dir():
            continue
        for jf in project_dir.glob("*.jsonl"):
            try:
                mtime = jf.stat().st_mtime
                if mtime >= cutoff:
                    results.append((jf, mtime, project_dir.name))
            except OSError:
                continue
    results.sort(key=lambda x: ((_measure_facade := _compat.facade()), x[1])[1], reverse=True)
    return results


def _find_subagent_jsonl_files(session_jsonl_path):
    """Find subagent JSONL files for a given session.

    Claude Code stores subagent logs in {session-uuid}/subagents/*.jsonl
    next to the parent {session-uuid}.jsonl file.
    """
    _measure_facade = _compat.facade()
    session_dir = session_jsonl_path.parent / session_jsonl_path.stem
    subagent_dir = session_dir / "subagents"
    if not subagent_dir.is_dir():
        return []
    results = []
    for jf in subagent_dir.glob("*.jsonl"):
        try:
            if jf.stat().st_size > 0:
                results.append(jf)
        except OSError:
            continue
    return results


def _extract_skills_and_agents_from_subagent(filepath):
    """Parse a subagent JSONL file for Skill and Task tool calls only.

    Returns (skills_dict, subagents_dict) without extracting token usage.
    Model-level token attribution is handled separately in collect_sessions()
    via _parse_session_jsonl() on each subagent file (the model attribution fix).
    """
    _measure_facade = _compat.facade()
    skills = {}
    subagents = {}
    try:
        if _measure_facade.os.stat(filepath).st_size > _measure_facade.codex_session.MAX_PARSE_FILE_BYTES:
            return skills, subagents
    except OSError:
        return skills, subagents
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
                if record.get("type") != "assistant":
                    continue
                msg = record.get("message", {})
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    tool_name = block.get("name", "")
                    inp = block.get("input", {})
                    if not isinstance(inp, dict):
                        inp = {}
                    if tool_name == "Skill":
                        skill = inp.get("skill", "unknown")
                        skills[skill] = skills.get(skill, 0) + 1
                    elif tool_name in ("Task", "Agent"):
                        # Current Claude Code names the sub-agent tool "Agent";
                        # older transcripts used "Task". Match both, exactly, so
                        # the TaskCreate/TaskUpdate todo tools are not miscounted.
                        agent_type = inp.get("subagent_type", "unknown")
                        subagents[agent_type] = subagents.get(agent_type, 0) + 1
    except (PermissionError, OSError):
        pass
    return skills, subagents


def _extract_agent_type(subagent_file):
    """Extract the agent type for a subagent JSONL file.

    Priority: .meta.json agentType > filename pattern > filename stem.
    Auto-compaction agents (filename contains 'acompact') are labeled explicitly.
    """
    _measure_facade = _compat.facade()
    sf = _measure_facade.Path(subagent_file) if not isinstance(subagent_file, _measure_facade.Path) else subagent_file

    # Auto-compaction agents are internal, not user-dispatched
    if "acompact" in sf.stem:
        return "Auto-Compaction"

    # Check .meta.json (written by Claude Code for user-dispatched agents)
    meta_path = sf.with_suffix(".meta.json")
    if meta_path.exists():
        try:
            with open(meta_path, "r", encoding="utf-8") as mf:
                meta = _measure_facade.json.load(mf)
                agent_type = meta.get("agentType", "")
                if agent_type:
                    return agent_type
        except (_measure_facade.json.JSONDecodeError, OSError):
            pass

    return "unknown"


def _analyze_subagent_costs(session_jsonl_path, tier=None):
    """Parse subagent JSONL files for a session and return cost breakdown.

    Returns list of dicts grouped by agent type: [{name, tokens, cost_usd, model, count}]
    Auto-compaction agents are separated from user-dispatched agents.
    """
    _measure_facade = _compat.facade()
    if tier is None:
        tier = _measure_facade._load_pricing_tier()
    subagent_files = _measure_facade._find_subagent_jsonl_files(_measure_facade.Path(session_jsonl_path))

    # Collect per-file costs, then group by agent type
    by_type = {}  # {agent_type: {tokens, cost_usd, model_tokens, count}}
    for sf in subagent_files:
        parsed = _measure_facade._parse_session_jsonl(str(sf))
        if not parsed or parsed.get("total_input_tokens", 0) == 0:
            continue
        dom_model = max(parsed["model_usage"], key=parsed["model_usage"].get) if parsed["model_usage"] else "unknown"
        total_input = parsed["total_input_tokens"]
        chr_val = parsed.get("cache_hit_rate", 0)
        cache_read = _measure_facade._safe_int(total_input * chr_val)
        cost = _measure_facade._get_model_cost(dom_model, max(0, total_input - cache_read),
                               parsed["total_output_tokens"], cache_read, 0, tier=tier)

        agent_type = _measure_facade._extract_agent_type(sf)
        norm_model = _measure_facade._normalize_model_name(dom_model) or dom_model

        if agent_type not in by_type:
            by_type[agent_type] = {"tokens": 0, "cost_usd": 0.0, "models": {}, "count": 0}
        entry = by_type[agent_type]
        entry["tokens"] += total_input + parsed["total_output_tokens"]
        entry["cost_usd"] += cost
        entry["count"] += 1
        entry["models"][norm_model] = entry["models"].get(norm_model, 0) + 1

    results = []
    for agent_type, data in by_type.items():
        # Pick the most common model for this agent type
        dominant_model = max(data["models"], key=data["models"].get) if data["models"] else "unknown"
        label = agent_type
        if data["count"] > 1:
            label = f"{agent_type} ({data['count']}x)"
        results.append({
            "name": label,
            "tokens": data["tokens"],
            "cost_usd": round(data["cost_usd"], 4),
            "model": dominant_model,
            "count": data["count"],
        })
    results.sort(key=lambda x: ((_measure_facade := _compat.facade()), x["cost_usd"])[1], reverse=True)
    return results


def _extract_costly_prompts(jsonl_path, tier=None, top_n=None):
    """Extract user prompts paired with the cost of the subsequent assistant turn.

    Returns list of dicts: [{text, tokens_in, tokens_out, cost_usd, model, timestamp}]
    sorted by cost descending, limited to top_n.
    """
    _measure_facade = _compat.facade()
    if tier is None:
        tier = _measure_facade._load_pricing_tier()
    prompts = []
    pending_prompt = None

    try:
        if _measure_facade.os.stat(jsonl_path).st_size > _measure_facade.codex_session.MAX_PARSE_FILE_BYTES:
            return prompts
    except OSError:
        return prompts

    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
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

                if rec_type == "user":
                    # Check it's a real user prompt, not a tool result
                    msg = record.get("message", {})
                    is_sidechain = record.get("isSidechain", False)
                    if is_sidechain:
                        continue
                    content = msg.get("content") if isinstance(msg, dict) else msg
                    if isinstance(content, list):
                        # Skip if all items are tool_result
                        types = [i.get("type") for i in content if isinstance(i, dict)]
                        if types and all(t == "tool_result" for t in types):
                            continue
                    # Same filter as checkpoints: harness text (meta records,
                    # compact summaries, command caveats) is not a prompt.
                    text = _measure_facade._task_user_text(record)
                    if text and len(text) > 5:
                        pending_prompt = {
                            "text": text[:300],
                            "timestamp": record.get("timestamp"),
                        }

                elif rec_type == "assistant" and pending_prompt:
                    msg = record.get("message", {})
                    if not isinstance(msg, dict):
                        msg = {}
                    usage = msg.get("usage", {})
                    if usage and isinstance(usage, dict):
                        inp = _measure_facade._safe_int(usage.get("input_tokens", 0))
                        out = _measure_facade._safe_int(usage.get("output_tokens", 0))
                        cr = _measure_facade._safe_int(usage.get("cache_read_input_tokens", 0))
                        cc = _measure_facade._safe_int(usage.get("cache_creation_input_tokens", 0))
                        model = _measure_facade._record_model(msg)
                        cost = _measure_facade._price_usage_parts(
                            _measure_facade._billed_usage_parts(usage, model, tier=tier),
                            tier=tier, per_request=True, ttl_split=False)
                        pending_prompt["tokens_in"] = inp + cr + cc
                        pending_prompt["tokens_out"] = out
                        pending_prompt["fresh_input"] = inp
                        pending_prompt["cache_read"] = cr
                        pending_prompt["cache_create"] = cc
                        pending_prompt["cost_usd"] = round(cost, 4)
                        pending_prompt["model"] = _measure_facade._normalize_model_name(model) or model
                        prompts.append(pending_prompt)
                    pending_prompt = None

    except (OSError, PermissionError):
        pass

    prompts.sort(key=lambda x: ((_measure_facade := _compat.facade()), x.get("cost_usd", 0))[1], reverse=True)
    return prompts[:top_n]


def _clean_project_name(raw_project):
    """Map Claude Code dashed directory names to human-readable labels.

    e.g. "-Users-jane" -> "home"
         "-Users-jane-projects-acme-api" -> "acme/api"
         "-Users-jane-myproject" -> "myproject"
    """
    _measure_facade = _compat.facade()
    if not raw_project:
        return "unknown"
    # Strip the leading "-Users-<username>-" prefix
    cleaned = _measure_facade.re.sub(r"^-Users-[^-]+-?", "", raw_project)
    if not cleaned:
        return "home"
    # Split remaining path segments and take the last 1-2 meaningful ones
    parts = cleaned.split("-")
    # Filter out empty parts
    parts = [p for p in parts if p]
    if not parts:
        return "home"
    # If the path is long, use last 2 segments joined by /
    if len(parts) > 2:
        return "/".join(parts[-2:])
    return "/".join(parts)


def _extract_topic(text):
    """Extract a clean topic from the first user message text.

    Strips common prefixes like 'Implement the following plan:' and
    extracts the plan title if present. Truncates to 120 chars.
    """
    _measure_facade = _compat.facade()
    if not text or not isinstance(text, str):
        return None
    # Strip leading whitespace/newlines
    text = text.strip()
    # Strip ANSI/VT escape sequences — session-log text is attacker-influenceable
    text = _measure_facade._strip_ansi(text)
    # Remove common prefixes
    prefixes = [
        "Implement the following plan:",
        "Implement the following plan\n",
        "Please implement the following plan:",
        "Execute the following plan:",
    ]
    for prefix in prefixes:
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):].strip()
            break
    # If it starts with a markdown heading, extract that as the topic
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("#"):
            text = line.lstrip("# ").strip()
            break
        if line:
            text = line
            break
    # The topic is user text persisted to session_log/quality-cache and
    # rendered into checkpoints — credentials must not ride along. If the
    # shared redactor is unavailable or refuses, drop the topic rather than
    # persist it raw. Redact BEFORE truncating: a secret that straddles the
    # cut is a prefix no pattern recognises.
    try:
        from credential_patterns import redact_credentials as _topic_redact
        text = _topic_redact(text)
    except Exception:
        return None
    if len(text) > 120:
        text = text[:117] + "..."
    return text or None


def _leading_user_text(message):
    """Return the leading user-authored text of a user message, or ''.

    A real slash invocation IS the whole user turn, so we look only at the first
    text the user actually typed. tool_result blocks (tool returns are written as
    user-role records) are excluded, so a search hit or a pasted transcript
    fragment that happens to echo a <command-name> block is not read as an
    invocation.
    """
    _measure_facade = _compat.facade()
    content = message.get("content") if isinstance(message, dict) else message
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                return ""
            btype = block.get("type")
            if btype == "text":
                return block.get("text", "") or ""
            # A tool_result (or any non-text) leading block means this user record
            # is not a typed invocation.
            return ""
    return ""


def _resolve_installed_skill(name):
    """Map a slash-command name to the INSTALLED skill's directory name, or None.

    Mirrors the never-used normalization (exact dir name / SKILL.md name via
    name_to_dir / namespaced ``plugin:skill``). Returns None for anything that is
    not an installed skill -- built-in commands (/clear, /compact, ...) and custom
    non-skill commands resolve to None, so they never pollute skill-usage stats.
    """
    _measure_facade = _compat.facade()
    if not name:
        return None
    try:
        comp = _measure_facade._cached_measure_components()
        skills = comp.get("skills", {})
        installed = set(skills.get("names", []))
        name_to_dir = skills.get("name_to_dir", {}) or {}
    except Exception:
        return None
    if name in installed:
        return name
    if name in name_to_dir:
        return name_to_dir[name]
    if ":" in name:
        parent, _, child = name.partition(":")
        for cand in (parent, child):
            if cand in installed:
                return cand
            if cand in name_to_dir:
                return name_to_dir[cand]
    return None


def _sidechain_path_reason(filepath):
    """Return the structural sidechain reason for a nested agent transcript."""
    _measure_facade = _compat.facade()
    path = _measure_facade.Path(filepath)
    if "subagents" in path.parts or (path.name.startswith("agent-") and path.suffix == ".jsonl"):
        return "nested_path"
    return None


def _osrc_prompt_text(record):
    """Extract only human-authored text from one user record."""
    _measure_facade = _compat.facade()
    if record.get("type") != "user":
        return ""
    message = record.get("message") or {}
    content = message.get("content") if isinstance(message, dict) else message
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text") or ""
            if isinstance(block, dict) and block.get("type") == "text"
            else block
            if isinstance(block, str)
            else ""
            for block in content
        )
    return ""


def _usage_request_key(record, msg=None):
    """Identity of the API request an assistant record belongs to.

    Claude Code writes `requestId` on every record of a request. Transcripts
    produced by the SDK or imported from a cloud session carry only the
    Anthropic `message.id`, which is equally stable across the streamed chunks
    of one reply. Returns None when the record has neither, so the caller can
    keep it as its own request instead of collapsing unrelated records.
    """
    _measure_facade = _compat.facade()
    req_id = record.get("requestId")
    if req_id:
        return str(req_id)
    if msg is None:
        msg = record.get("message")
    if isinstance(msg, dict):
        mid = msg.get("id")
        if mid:
            return str(mid)
    return None


def _request_local_day(ts_s):
    """Local calendar date of an ISO timestamp, or None when it does not parse."""
    _measure_facade = _compat.facade()
    if not (ts_s and isinstance(ts_s, str)):
        return None
    try:
        return _measure_facade.datetime.fromisoformat(ts_s.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _empty_model_buckets():
    _measure_facade = _compat.facade()
    return {"fresh_input": 0, "cache_read": 0, "cache_create": 0,
            "cache_create_1h": 0, "cache_create_5m": 0, "output": 0}


def _net_request_is_empty(u):
    """True for a request entry that adds nothing to a session: another session
    already counted the call and every token of it."""
    _measure_facade = _compat.facade()
    return (not u.get("n", 1)
            and not any(int(u.get(f) or 0) for f in ("inp", "out", "cr", "cc", "cc_1h", "cc_5m")))


def _aggregate_request_usage(requests):
    """Roll a {request key: usage} map up into session totals.

    `requests` holds the final (streaming-MAX) usage of each API request. Every
    per-session figure derived from usage (token totals, per-model buckets,
    per-day buckets, API call count) comes from here, so a parse, a merge of
    several files and a filtered view of either all agree. Returns
    (totals, request_days).
    """
    _measure_facade = _compat.facade()
    totals = {
        "total_input": 0, "total_output": 0, "total_cache_read": 0,
        "total_cache_create": 0, "total_cache_create_1h": 0,
        "total_cache_create_5m": 0, "api_calls": 0, "api_call_timestamps": [],
        "model_usage": {}, "model_usage_breakdown": {}, "daily_usage": {},
    }
    request_days = {}
    for key, u in requests.items():
        if _measure_facade._net_request_is_empty(u):
            continue
        totals["total_input"] += u["inp"]
        totals["total_output"] += u["out"]
        totals["total_cache_read"] += u["cr"]
        totals["total_cache_create"] += u["cc"]
        totals["total_cache_create_1h"] += u["cc_1h"]
        totals["total_cache_create_5m"] += u["cc_5m"]
        ts_s = u.get("ts")
        if not u.get("advisor") and u.get("n", 1):
            # An advisor part belongs to its request: not another API call.
            # `n` is 0 when another session already counted the call itself.
            totals["api_calls"] += 1
            if ts_s and isinstance(ts_s, str):
                try:
                    totals["api_call_timestamps"].append(
                        _measure_facade.datetime.fromisoformat(ts_s.replace("Z", "+00:00")))
                except (ValueError, TypeError):
                    pass
        model = u["model"]
        billable = u["inp"] + u["cc"] + u["out"]  # fresh + cache_create + output
        totals["model_usage"][model] = totals["model_usage"].get(model, 0) + billable
        bd = totals["model_usage_breakdown"].setdefault(model, _measure_facade._empty_model_buckets())
        bd["fresh_input"] += u["inp"]
        bd["cache_read"] += u["cr"]
        bd["cache_create"] += u["cc"]
        bd["cache_create_1h"] += u["cc_1h"]
        bd["cache_create_5m"] += u["cc_5m"]
        bd["output"] += u["out"]
        # Bucket each request by the LOCAL date it ran, so a session that spans
        # midnight bills each day for its own requests instead of dumping the
        # whole session onto its last-active day (#200). Records without a
        # parseable timestamp are left out here; callers fall back to the
        # session date for the unbucketed remainder.
        day = u["day"] if "day" in u else _measure_facade._request_local_day(ts_s)
        request_days[key] = day
        if day:
            dbd = totals["daily_usage"].setdefault(day, {}).setdefault(model, _measure_facade._empty_model_buckets())
            dbd["fresh_input"] += u["inp"]
            dbd["cache_read"] += u["cr"]
            dbd["cache_create"] += u["cc"]
            dbd["cache_create_1h"] += u["cc_1h"]
            dbd["cache_create_5m"] += u["cc_5m"]
            dbd["output"] += u["out"]
    return totals, request_days


def _parse_session_jsonl(filepath, window_start=None, window_end=None):
    """Parse a single JSONL session file in one streaming pass.

    Returns a dict with extracted session metrics, or None if the file
    is empty or unparseable. Results are memoized by (path, mtime, size)
    so a single process invocation never re-parses the same unchanged file
    (e.g. backfill + collect both touching the same transcript).
    """
    # Memoization: skip re-parse if the file hasn't changed since we last saw it.
    _measure_facade = _compat.facade()
    try:
        st = _measure_facade.os.stat(filepath)
        cache_key = (str(filepath), st.st_mtime_ns, st.st_size, window_start, window_end)
        if cache_key in _measure_facade._parse_session_jsonl_cache:
            return _measure_facade._parse_session_jsonl_cache[cache_key]
    except OSError:
        cache_key = None  # stat failed — parse without caching

    if _measure_facade._use_codex_session_adapter(filepath):
        if window_start is not None or window_end is not None:
            return None  # This adapter does not yet support activity slicing.
        result = _measure_facade.codex_session.parse_session_jsonl(filepath)
        if cache_key is not None:
            if len(_measure_facade._parse_session_jsonl_cache) >= _measure_facade._PARSE_CACHE_MAX:
                _measure_facade._parse_session_jsonl_cache.clear()
            _measure_facade._parse_session_jsonl_cache[cache_key] = result
        return result

    daily_metrics = {}
    skills_used = {}
    subagents_used = {}
    tool_calls = {}
    request_usage_map = {}        # v5.4.9: per-requestId MAX usage (streaming-aware)
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_create = 0
    total_cache_create_1h = 0
    total_cache_create_5m = 0
    model_usage = {}              # v5.4.8: billable tokens (fresh_input + cache_create + output)
    model_usage_breakdown = {}    # v5.4.8: per-model {fresh_input, cache_read, cache_create, output}
    daily_usage = {}              # local date -> per-model breakdown, for per-day cost attribution
    request_days = {}             # request key -> local date (None when untimestamped)
    reported_input = 0             # official /stats-compatible, no streaming dedup
    reported_output = 0
    reported_model_usage = {}
    version = None
    slug = None
    topic = None
    first_ts = None
    last_ts = None
    api_call_timestamps = []
    message_count = 0
    _counted_message_ids = set()  # dedup streamed assistant chunks -> one reply
    _conversation_records = 0     # RAW user/assistant records seen (transcript-is-real test)
    api_calls = 0
    sidechain_reason = _measure_facade._sidechain_path_reason(filepath)
    is_sidechain = sidechain_reason is not None
    first_user_record_seen = False

    # Bound the work like the Codex adapter's _iter_json_records: a multi-GB
    # transcript or one pathological line must not dominate a parse. st is set
    # when the initial os.stat above succeeded; skip oversized files outright
    # and oversized lines individually.
    if cache_key is not None and st.st_size > _measure_facade.codex_session.MAX_PARSE_FILE_BYTES:
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
                    # Valid JSON but not an object (42, "x", true, null, [...])
                    # — a truncated/corrupt transcript line, not a record.
                    continue

                # A subagent sidechain transcript is flagged on its records; any
                # such record marks the whole file as a sidechain (not a human
                # working session) so the cost comparison can exclude it.
                if record.get("isSidechain") is True:
                    is_sidechain = True
                    sidechain_reason = "isSidechain"

                # Detect outsourcerer delegation sessions by content marker.
                # The outsourcerer skill injects an OSRC::PROGRESS protocol
                # block into the delegation PROMPT, so the marker only proves
                # delegation when it appears in a human prompt.
                #
                # Scanning the whole record (the pre-fix behaviour) also matched
                # the marker inside tool RESULTS and assistant text, so an
                # orchestrator session that merely *watches* a delegate (running
                # `outsourcerer.sh status/watch/result`, whose output echoes
                # OSRC::PROGRESS) was itself misfiled as the delegation. That
                # mislabelled 234 of a user's sessions holding 57% of all input
                # tokens -- the longest human working sessions -- and pulled them
                # out of the human pool the cost comparison runs on.
                #
                # Only `type == "user"` records count, and within them only real
                # prompt text: a user record whose content blocks are tool_result
                # is the transport for a tool's output, not something a human
                # typed.
                if record.get("type") == "user" and not first_user_record_seen:
                    _prompt = _measure_facade._osrc_prompt_text(record)
                    if _prompt:
                        first_user_record_seen = True
                    if _prompt and not is_sidechain:
                        if "OSRC::PROGRESS" in _prompt or "OSRC::DONE" in _prompt:
                            is_sidechain = True
                            sidechain_reason = "osrc_prompt"

                # Extract version (take the first non-None we see)
                if version is None:
                    v = record.get("version")
                    if v:
                        version = v

                # Extract slug (first record that has one)
                if slug is None:
                    s = record.get("slug")
                    if s:
                        slug = s

                # Window reports must slice activity, including sessions that began
                # before reset. Classify sidechains before filtering their records.
                if window_start is not None or window_end is not None:
                    try:
                        activity_ts = _measure_facade.datetime.fromisoformat(
                            str(record.get("timestamp")).replace("Z", "+00:00"))
                        if activity_ts.tzinfo is None:
                            activity_ts = activity_ts.replace(tzinfo=_measure_facade.timezone.utc)
                    except (ValueError, TypeError):
                        continue
                    if window_start is not None and activity_ts < window_start:
                        continue
                    if window_end is not None and activity_ts >= window_end:
                        continue

                # Extract timestamp
                ts_str = record.get("timestamp")
                if ts_str and isinstance(ts_str, str):
                    try:
                        ts = _measure_facade.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if first_ts is None:
                            first_ts = ts
                        last_ts = ts
                    except (ValueError, TypeError):
                        pass

                record_day = _measure_facade._request_local_day(ts_str)
                prior_messages = message_count
                prior_maps = [dict(v) for v in (skills_used, subagents_used, tool_calls)]
                rec_type = record.get("type")

                # Topic and checkpoint task share a human-task-only view. Keep
                # raw user text for message scoring and slash-skill accounting.
                if rec_type == "user" and topic is None:
                    topic = _measure_facade._extract_topic(_measure_facade._task_user_text(record))

                if rec_type in ("user", "assistant"):
                    _conversation_records += 1

                # Count user/assistant messages.
                #
                # Two things inflated this against any external message count
                # (Claude Code's own /stats panel reported 181,935 where we
                # reported 196,069 for the same window):
                #
                #   1. STREAMING DUPLICATES. Claude Code writes MULTIPLE assistant
                #      records per `requestId` while streaming -- the same reply,
                #      chunk by chunk. Counting records counted one reply many
                #      times (3,819 raw vs 2,239 deduped on one real session).
                #      Token accounting already dedups these per requestId a few
                #      lines below; the message counter never did.
                #   2. TOOL-RESULT TRANSPORT. A `user` record whose content blocks
                #      are all `tool_result` is how a tool's OUTPUT is carried back
                #      to the model. Nobody typed it, so it is not a message.
                #
                # `message_count` feeds per-session quality scoring and every
                # per-session average on the dashboard, so both inflations pushed
                # those averages down. Count a reply ONCE, and only count a user
                # record a human actually authored.
                if rec_type == "assistant":
                    _mid = None
                    _m = record.get("message")
                    if isinstance(_m, dict):
                        _mid = _m.get("id") or record.get("requestId")
                    _mid = _mid or record.get("requestId")
                    if _mid is None:
                        message_count += 1          # no id to dedup on: count it
                    elif _mid not in _counted_message_ids:
                        _counted_message_ids.add(_mid)
                        message_count += 1
                elif rec_type == "user":
                    _m = record.get("message")
                    _c = _m.get("content") if isinstance(_m, dict) else _m
                    if isinstance(_c, list) and _c and all(
                        isinstance(b, dict) and b.get("type") == "tool_result" for b in _c
                    ):
                        pass                         # tool output, not a human turn
                    elif record.get("isMeta") or record.get("isCompactSummary"):
                        pass                         # harness-written, not a human turn
                    else:
                        message_count += 1

                # Count slash-command invocations of INSTALLED skills as usage. A
                # skill run via /name (e.g. /briefing) emits no Skill tool_use, so
                # it would otherwise be scored "unused". Gate strictly so nothing
                # else leaks into skill stats: a real invocation IS the whole user
                # turn, so require the command block to be the LEADING user-authored
                # text (a pasted fragment in prose or a tool_result echo is not),
                # require the <command-args> sibling Claude Code always emits, and
                # count ONLY names that resolve to an installed skill (so /clear,
                # /compact and other non-skill commands never pollute skill stats).
                if rec_type == "user":
                    _lead = _measure_facade._leading_user_text(record.get("message", {})).lstrip()
                    if _lead.startswith("<command-name>") and "<command-args>" in _lead:
                        for _cn in _measure_facade._SLASH_COMMAND_RE.findall(_lead):
                            _sk = _measure_facade._resolve_installed_skill(_cn)
                            if _sk:
                                skills_used[_sk] = skills_used.get(_sk, 0) + 1

                # Extract tool usage from assistant messages
                if rec_type == "assistant":
                    msg = record.get("message", {})
                    if not isinstance(msg, dict):
                        msg = {}
                    content = msg.get("content", [])

                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") != "tool_use":
                                continue

                            tool_name = block.get("name", "")
                            tool_calls[tool_name] = tool_calls.get(tool_name, 0) + 1

                            inp = block.get("input", {})
                            if not isinstance(inp, dict):
                                inp = {}
                            if tool_name == "Skill":
                                skill = inp.get("skill", "unknown")
                                skills_used[skill] = skills_used.get(skill, 0) + 1
                            elif tool_name in ("Task", "Agent"):
                                # Current Claude Code names the sub-agent tool
                                # "Agent"; older transcripts used "Task". Match
                                # both exactly so TaskCreate/TaskUpdate (todo
                                # tools) are never miscounted as sub-agents.
                                agent_type = inp.get("subagent_type", "unknown")
                                subagents_used[agent_type] = subagents_used.get(agent_type, 0) + 1

                    # v5.4.9 (P1 fix): Streaming-aware dedup. Claude Code writes
                    # MULTIPLE assistant records per requestId during streaming;
                    # each one's usage.output_tokens is the CUMULATIVE count up
                    # to that chunk. The previous dedup (skip-if-seen) kept the
                    # FIRST record, which captured only the initial partial
                    # count and discarded the final cumulative total — causing
                    # a 3-10x under-count of output. Identified by verifying
                    # 48,595 requestIds across 30d had monotonically increasing
                    # output values in local JSONL. Fix: track per-requestId
                    # MAX usage and apply it at end of file.
                    req_id = _measure_facade._usage_request_key(record, msg)
                    usage = msg.get("usage", {})
                    if usage and isinstance(usage, dict):
                        # _safe_int, not raw int(): json.loads accepts the
                        # non-standard Infinity/NaN literals, and int(inf) /
                        # int(nan) raise OverflowError/ValueError — a hostile
                        # or corrupt transcript must degrade to 0, not crash.
                        inp_tok = _measure_facade._safe_int(usage.get("input_tokens", 0))
                        out_tok = _measure_facade._safe_int(usage.get("output_tokens", 0))
                        cr = _measure_facade._safe_int(usage.get("cache_read_input_tokens", 0))
                        cache_creation = usage.get("cache_creation", {})
                        if not isinstance(cache_creation, dict):
                            cache_creation = {}
                        cc_1h = _measure_facade._safe_int(
                            cache_creation.get("ephemeral_1h_input_tokens", 0)
                            or usage.get("ephemeral_1h_input_tokens", 0)
                        )
                        cc_5m = _measure_facade._safe_int(
                            cache_creation.get("ephemeral_5m_input_tokens", 0)
                            or usage.get("ephemeral_5m_input_tokens", 0)
                        )
                        cc = _measure_facade._safe_int(usage.get("cache_creation_input_tokens", 0)) or (cc_1h + cc_5m)
                        model = _measure_facade._record_model(msg)
                        # Claude Code's /stats basis sums every assistant usage
                        # record, including streamed chunks, and excludes cache
                        # read/write classes. Keep it beside the deduped billed
                        # basis used for pricing and cost analysis.
                        reported_input += inp_tok
                        reported_output += out_tok
                        reported_model_usage[model] = (
                            reported_model_usage.get(model, 0)
                            + inp_tok
                            + out_tok
                        )
                        # Records with neither a requestId nor a message id must
                        # never collapse with each other — use the map's own
                        # size as a monotonic per-session counter.
                        key = req_id if req_id else f"__noreq__{len(request_usage_map)}"
                        # Advisor iterations bill at the advisor's own model:
                        # each is a separate entry under "<request>#advisor<n>"
                        # so streamed copies still dedup by MAX per entry.
                        parts = _measure_facade._billed_usage_parts(usage, model)
                        for part_index, (part_model, b) in enumerate(parts):
                            part_key = key if part_index == 0 else f"{key}#advisor{part_index}"
                            prev = request_usage_map.get(part_key)
                            if prev is None:
                                request_usage_map[part_key] = {
                                    "inp": b["inp"], "out": b["out"], "cr": b["cr"],
                                    "cc": b["cc"], "cc_1h": b["cc_1h"],
                                    "cc_5m": b["cc_5m"], "model": part_model,
                                    "ts": ts_str,
                                }
                                if part_index:
                                    request_usage_map[part_key]["advisor"] = True
                            else:
                                # Streaming: keep MAX of each token category.
                                # Model + timestamp: prefer later (non-empty) values.
                                for field in _measure_facade._USAGE_BUCKET_KEYS:
                                    prev[field] = max(prev[field], b[field])
                                if part_model and part_model != "unknown":
                                    prev["model"] = part_model
                                if ts_str:
                                    prev["ts"] = ts_str

                if record_day:
                    metric = daily_metrics.setdefault(record_day, {
                        "message_count": 0, "skills": {}, "subagents": {}, "tool_calls": {},
                        "duration_minutes": 0})
                    metric["message_count"] += message_count - prior_messages
                    for name, before, after in zip(("skills", "subagents", "tool_calls"), prior_maps,
                                                   (skills_used, subagents_used, tool_calls)):
                        for key, value in after.items():
                            delta = value - before.get(key, 0)
                            if delta:
                                metric[name][key] = metric[name].get(key, 0) + delta
                    epoch = _measure_facade.datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                    metric["first"] = min(metric.get("first", epoch), epoch)
                    metric["last"] = max(metric.get("last", epoch), epoch)
                    metric["duration_minutes"] = (metric["last"] - metric["first"]) / 60

    except (PermissionError, OSError):
        return None

    # "Is this a real transcript?" must stay a question about RECORDS, not about
    # the (now deduped) message count. `message_count` no longer counts streamed
    # duplicates or tool_result transport, so a legitimate transcript made only
    # of tool results would otherwise score 0 here and be discarded entirely --
    # which silently dropped whole sessions from every population.
    if _conversation_records == 0:
        if cache_key is not None:
            if len(_measure_facade._parse_session_jsonl_cache) >= _measure_facade._PARSE_CACHE_MAX:
                _measure_facade._parse_session_jsonl_cache.clear()
            _measure_facade._parse_session_jsonl_cache[cache_key] = None
        return None

    # Streaming-aware dedup: each entry in request_usage_map holds the final
    # cumulative usage for one API call. Summing these gives the session's true
    # total without streaming-chunk under-counting.
    for _key, _u in request_usage_map.items():
        _u["day"] = _measure_facade._request_local_day(_u.get("ts"))
    _agg, request_days = _measure_facade._aggregate_request_usage(request_usage_map)
    total_input = _agg["total_input"]
    total_output = _agg["total_output"]
    total_cache_read = _agg["total_cache_read"]
    total_cache_create = _agg["total_cache_create"]
    total_cache_create_1h = _agg["total_cache_create_1h"]
    total_cache_create_5m = _agg["total_cache_create_5m"]
    api_calls = _agg["api_calls"]
    api_call_timestamps = _agg["api_call_timestamps"]
    model_usage = _agg["model_usage"]
    model_usage_breakdown = _agg["model_usage_breakdown"]
    daily_usage = _measure_facade._daily_usage_from_merged(request_usage_map)

    # Calculate duration
    duration_minutes = 0
    if first_ts and last_ts:
        delta = (last_ts - first_ts).total_seconds()
        duration_minutes = max(0, delta / 60)

    # Full input = uncached + cache reads + cache creation
    total_full_input = total_input + total_cache_read + total_cache_create

    # Cache hit rate
    cache_hit_rate = 0.0
    if total_full_input > 0:
        cache_hit_rate = total_cache_read / total_full_input
    gap_stats = _measure_facade._compute_call_gap_stats(api_call_timestamps)

    result = {
        "version": version,
        "slug": slug,
        "topic": topic,
        "duration_minutes": duration_minutes,
        "total_input_tokens": total_full_input,
        "total_output_tokens": total_output,
        "total_cache_read": total_cache_read,
        "total_cache_create": total_cache_create,
        "total_cache_create_1h": total_cache_create_1h,
        "total_cache_create_5m": total_cache_create_5m,
        "cache_hit_rate": cache_hit_rate,
        "avg_call_gap_seconds": gap_stats["avg"],
        "max_call_gap_seconds": gap_stats["max"],
        "p95_call_gap_seconds": gap_stats["p95"],
        "model_usage": model_usage,
        "model_usage_breakdown": model_usage_breakdown,
        "daily_usage": daily_usage,
        "daily_metrics": daily_metrics,
        # Per-request usage (streaming-deduped) so a caller can dedupe requests
        # that Claude Code writes into more than one transcript of a session.
        "request_usage": {
            k: dict(u, day=request_days.get(k)) for k, u in request_usage_map.items()
        },
        "reported_input_tokens": reported_input,
        "reported_output_tokens": reported_output,
        "reported_model_usage": reported_model_usage,
        "skills_used": skills_used,
        "subagents_used": subagents_used,
        "tool_calls": tool_calls,
        "message_count": message_count,
        "api_calls": api_calls,
        "first_ts": first_ts.isoformat() if first_ts else None,
        "is_sidechain": is_sidechain,
        "sidechain_reason": sidechain_reason,
    }
    if cache_key is not None:
        if len(_measure_facade._parse_session_jsonl_cache) >= _measure_facade._PARSE_CACHE_MAX:
            _measure_facade._parse_session_jsonl_cache.clear()
        _measure_facade._parse_session_jsonl_cache[cache_key] = result
    return result


def parse_session_turns(filepath):
    """Parse a JSONL session file and return per-turn token data.

    Returns a list of dicts, one per API call:
      {turn_index, role, input_tokens, output_tokens, cache_read,
       cache_creation, model, timestamp, tools_used, cost_usd}

    Returns empty list if file is empty/unparseable.
    """
    _measure_facade = _compat.facade()
    if _measure_facade._use_codex_session_adapter(filepath):
        turns = _measure_facade.codex_session.parse_session_turns(filepath)
        for turn in turns:
            cost = _measure_facade._get_model_cost(
                turn.get("model"),
                max(0, turn.get("input_tokens", 0) - turn.get("cache_read", 0)),
                turn.get("output_tokens", 0),
                turn.get("cache_read", 0),
                turn.get("cache_creation", 0),
            )
            turn["cost_usd"] = round(cost, 6)
            turn["cost_source"] = "openai_api_pricing" if cost > 0 else "unavailable"
        return turns

    turns = []
    turn_index = 0
    tier = _measure_facade._load_pricing_tier()
    prev_call_ts = None
    turn_by_key = {}
    parts_by_key = {}

    # Bound the work like the Codex adapter's _iter_json_records (see
    # _parse_session_jsonl): skip oversized files and oversized lines.
    try:
        if _measure_facade.os.stat(filepath).st_size > _measure_facade.codex_session.MAX_PARSE_FILE_BYTES:
            return turns
    except OSError:
        return turns

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
                    # Valid JSON but not an object — corrupt transcript line.
                    continue

                rec_type = record.get("type")
                if rec_type != "assistant":
                    continue

                msg = record.get("message", {})
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage", {})
                if not usage or not isinstance(usage, dict):
                    continue

                # _safe_int: Infinity/NaN usage literals degrade to 0 instead of
                # raising OverflowError/ValueError out of _get_model_cost's int().
                inp_tok = _measure_facade._safe_int(usage.get("input_tokens", 0))
                out_tok = _measure_facade._safe_int(usage.get("output_tokens", 0))
                cr = _measure_facade._safe_int(usage.get("cache_read_input_tokens", 0))
                cache_creation = usage.get("cache_creation", {})
                if not isinstance(cache_creation, dict):
                    cache_creation = {}
                cc_1h = _measure_facade._safe_int(
                    cache_creation.get("ephemeral_1h_input_tokens", 0)
                    or usage.get("ephemeral_1h_input_tokens", 0)
                )
                cc_5m = _measure_facade._safe_int(
                    cache_creation.get("ephemeral_5m_input_tokens", 0)
                    or usage.get("ephemeral_5m_input_tokens", 0)
                )
                cc = _measure_facade._safe_int(usage.get("cache_creation_input_tokens", 0)) or (cc_1h + cc_5m)
                model = _measure_facade._record_model(msg)

                # Extract tools used in this turn
                tools = []
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            tools.append(block.get("name", ""))

                # One API request is one turn. Its streamed records share a
                # request id (or message id) and each carries the cumulative
                # usage so far, so they fold into the first turn of that request.
                req_key = _measure_facade._usage_request_key(record, msg)
                prior_turn = turn_by_key.get(req_key) if req_key else None

                ts_str = record.get("timestamp")
                gap_since_prev_seconds = None
                if prior_turn is None and ts_str and isinstance(ts_str, str):
                    try:
                        call_ts = _measure_facade.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if prev_call_ts is not None:
                            gap_since_prev_seconds = int(round(max(0, (call_ts - prev_call_ts).total_seconds())))
                        prev_call_ts = call_ts
                    except (ValueError, TypeError):
                        pass
                parts = _measure_facade._billed_usage_parts(usage, model, tier=tier)
                cost = _measure_facade._price_usage_parts(parts, tier=tier, per_request=True)

                turn = {
                    "turn_index": turn_index,
                    "role": "assistant",
                    "input_tokens": inp_tok,
                    "output_tokens": out_tok,
                    "cache_read": cr,
                    "cache_creation": cc,
                    "cache_creation_1h": cc_1h,
                    "cache_creation_5m": cc_5m,
                    "model": model,
                    "timestamp": ts_str,
                    "gap_since_prev_seconds": gap_since_prev_seconds,
                    "tools_used": tools,
                    "cost_usd": round(cost, 6),
                }
                ctx_usage = _measure_facade._context_usage(usage)
                if ctx_usage is not usage:
                    # Cached prefix the next turn can reuse: the final executor
                    # only, matching the live keep-warm arm (#287).
                    turn["ctx_cache"] = (
                        _measure_facade._context_token_count(ctx_usage)
                        - _measure_facade._safe_int(ctx_usage.get("input_tokens")))
                resume_usage = _measure_facade._resume_usage(usage)
                if resume_usage is not usage:
                    # Billing stays aggregate; pause warmth uses the first
                    # executor, before any cache writes inside this response.
                    resume_read = _measure_facade._safe_int(resume_usage.get("cache_read_input_tokens"))
                    turn["resume_cache_read"] = resume_read
                    turn["resume_cache_creation"] = (
                        _measure_facade._context_token_count(resume_usage) - resume_read
                        - _measure_facade._safe_int(resume_usage.get("input_tokens")))
                if prior_turn is not None:
                    latest_context = turn["output_tokens"] >= prior_turn["output_tokens"]
                    for field in ("input_tokens", "output_tokens", "cache_read",
                                  "cache_creation", "cache_creation_1h", "cache_creation_5m"):
                        prior_turn[field] = max(prior_turn[field], turn[field])
                    prior_turn["tools_used"] += [
                        t for t in tools if t not in prior_turn["tools_used"]]
                    if model and model != "unknown":
                        prior_turn["model"] = model
                    for field in ("ctx_cache", "resume_cache_read", "resume_cache_creation"):
                        if field in turn and (latest_context or field not in prior_turn):
                            prior_turn[field] = turn[field]
                    merged_parts = parts_by_key[req_key]
                    for index, (part_model, buckets) in enumerate(parts):
                        if index == len(merged_parts):
                            merged_parts.append((part_model, dict(buckets)))
                        else:
                            old_model, old_buckets = merged_parts[index]
                            for field in _measure_facade._USAGE_BUCKET_KEYS:
                                old_buckets[field] = max(old_buckets[field], buckets[field])
                            if part_model and part_model != "unknown":
                                old_model = part_model
                            merged_parts[index] = (old_model, old_buckets)
                    prior_turn["cost_usd"] = round(_measure_facade._price_usage_parts(
                        merged_parts, tier=tier, per_request=True), 6)
                    continue
                turns.append(turn)
                if req_key:
                    turn_by_key[req_key] = turn
                    parts_by_key[req_key] = parts
                turn_index += 1

    except (PermissionError, OSError):
        pass

    return turns


def score_session_quality(session_data):
    """Score a single session's quality on a 0-100 scale.

    Uses a simplified version of the 5-signal quality score:
    - Context fill at session end (25%)
    - Message count risk (25%)
    - Cache hit rate (20%)
    - Output/input ratio (15%)
    - Compaction events (15%)

    session_data should include: total_input_tokens, total_output_tokens,
    message_count, cache_hit_rate, api_calls, and optionally total_cache_read.
    """
    _measure_facade = _compat.facade()
    score = 0.0

    # Signal 1: Context fill (25%)
    # Lower fill = better (more room for work)
    context_window = _measure_facade.detect_context_window()[0]
    total_input = session_data.get("total_input_tokens", 0)
    fill_ratio = total_input / context_window if context_window > 0 else 0
    if fill_ratio < 0.3:
        fill_score = 100
    elif fill_ratio < 0.5:
        fill_score = 80
    elif fill_ratio < 0.7:
        fill_score = 55
    elif fill_ratio < 0.85:
        fill_score = 30
    else:
        fill_score = 10
    score += fill_score * 0.25

    # Signal 2: Message count risk (25%)
    # More messages = higher risk of quality degradation
    msg_count = session_data.get("message_count", 0)
    if msg_count <= 20:
        msg_score = 100
    elif msg_count <= 40:
        msg_score = 80
    elif msg_count <= 60:
        msg_score = 55
    elif msg_count <= 100:
        msg_score = 30
    else:
        msg_score = 10
    score += msg_score * 0.25

    # Signal 3: Cache hit rate (20%)
    # Higher cache = better (reusing context efficiently)
    chr_ = session_data.get("cache_hit_rate", 0)
    if chr_ >= 0.8:
        cache_score = 100
    elif chr_ >= 0.6:
        cache_score = 80
    elif chr_ >= 0.4:
        cache_score = 55
    elif chr_ >= 0.2:
        cache_score = 30
    else:
        cache_score = 10
    score += cache_score * 0.20

    # Signal 4: Output/input ratio (15%)
    # Very low ratio = wasteful (loading lots of context, producing little)
    total_output = session_data.get("total_output_tokens", 0)
    if total_input > 0:
        oi_ratio = total_output / total_input
    else:
        oi_ratio = 1.0
    if oi_ratio >= 0.05:
        oi_score = 100
    elif oi_ratio >= 0.02:
        oi_score = 70
    elif oi_ratio >= 0.01:
        oi_score = 40
    else:
        oi_score = 15
    score += oi_score * 0.15

    # Signal 5: API calls vs messages (15%)
    # Healthy: roughly 1 API call per 2 messages
    api_calls = session_data.get("api_calls", 0)
    if msg_count > 0 and api_calls > 0:
        calls_per_msg = api_calls / msg_count
        if calls_per_msg <= 0.6:
            api_score = 100
        elif calls_per_msg <= 0.8:
            api_score = 75
        else:
            api_score = 50
    else:
        api_score = 50
    score += api_score * 0.15

    final = int(round(min(100, max(0, score))))

    return {"score": final, "band": _measure_facade.score_to_band(final), "grade": _measure_facade.score_to_grade(final)}


def _record_model(msg):
    """A transcript record's model id as text. A hand-edited or foreign
    record can carry a number or an object there, and one such record must not
    crash the whole collect; it is simply an unknown model."""
    _measure_facade = _compat.facade()
    model = msg.get("model") if isinstance(msg, dict) else None
    return model if isinstance(model, str) and model else "unknown"


def _normalize_model_name(model_id):
    """Collapse model IDs like 'claude-sonnet-4-6' into 'sonnet'.

    Returns None for synthetic/internal model IDs that should be skipped.
    """
    _measure_facade = _compat.facade()
    if not isinstance(model_id, str) or not model_id or model_id.startswith("<"):
        return None
    m = model_id.lower()
    if "fable" in m:
        return "fable"
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        # Sonnet 5 (released 2026-06-30) is $2/$10; every earlier Sonnet is $3/$15.
        # One shared bucket misprices whichever generation it is not calibrated to --
        # and the pre-2026-07 baseline windows are ALL Sonnet 4.6 or older, so the
        # frozen "before" arm was being priced 33% under its real historical cost.
        # Anything not explicitly an older generation keeps the current "sonnet" card,
        # so an unrecognised future Sonnet is never silently priced as legacy.
        if _measure_facade._SONNET_LEGACY_RE.search(m):
            return "sonnet_legacy"
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return model_id


def _claude_price_key(model_id, claude_models):
    """Rate-card key for a Claude model id, finer than _normalize_model_name.

    Display labels and savings mixes stay on the family buckets ("opus",
    "fable"), but generations are priced differently (Opus 5.5 is $4/$20,
    Opus 4.1 is $15/$75, Fable 5.1 reads cache at $0.25), so pricing looks for
    a generation card first ("opus_5_5", then "opus_5"), then the family.
    Generation cards come from pricing/prices.json, so a new model is priced
    the day the table has it. Mythos shares the Fable cards when it has none.
    """
    _measure_facade = _compat.facade()
    if not model_id or not isinstance(model_id, str):
        return None
    legacy = _measure_facade._CLAUDE_LEGACY_ID_RE.search(model_id.lower())
    if legacy:
        major, minor, family = legacy.groups()
        for key in ((f"{family}_{major}_{minor}", f"{family}_{major}") if minor else (f"{family}_{major}",)):
            if key in claude_models:
                return key
        return _measure_facade._normalize_model_name(model_id)  # Sonnet 3.x lands on sonnet_legacy
    m = _measure_facade._CLAUDE_GENERATION_RE.search(model_id.lower())
    if m:
        family, major, minor = m.groups()
        families = (family, "fable") if family == "mythos" else (family,)
        for fam in families:
            for key in ((f"{fam}_{major}_{minor}", f"{fam}_{major}") if minor else (f"{fam}_{major}",)):
                if key in claude_models:
                    return key
        if family == "mythos" and "fable" in claude_models:
            return "fable"
    elif "mythos" in str(model_id).lower() and "fable" in claude_models:
        return "fable"
    # A bare `haiku` keeps the family card (Haiku 4.5 rates), matching the
    # 200K window _claude_model_window gives it: the alias is provider-
    # dependent, and "haiku" is also the family-bucket label that routing and
    # model-mix callers pass in, so it must not float to the 5.5 card.
    return _measure_facade._normalize_model_name(model_id)


def _load_overhead_snapshots():
    """Load any saved token-optimizer snapshots for overhead trajectory.

    Returns snapshots sorted chronologically by timestamp.
    """
    _measure_facade = _compat.facade()
    snapshots = []
    if not _measure_facade.SNAPSHOT_DIR.exists():
        return snapshots
    for sf in sorted(_measure_facade.SNAPSHOT_DIR.glob("snapshot_*.json")):
        try:
            with open(sf, "r", encoding="utf-8") as f:
                data = _measure_facade.json.load(f)
            snapshots.append({
                "label": data.get("label", sf.stem),
                "timestamp": data.get("timestamp", ""),
                "total": data.get("totals", {}).get("estimated_total", 0),
            })
        except (_measure_facade.json.JSONDecodeError, PermissionError, OSError):
            continue
    # Sort by timestamp so trajectory reads chronologically
    snapshots.sort(key=lambda s: ((_measure_facade := _compat.facade()), s["timestamp"])[1])
    return snapshots


def _is_file_collected(conn, jsonl_path, check_mtime=None):
    """Check stored IDs; file collectors also check delegated transcript changes."""
    _measure_facade = _compat.facade()
    row = conn.execute(
        "SELECT collected_at FROM session_log WHERE jsonl_path = ?",
        (str(jsonl_path),),
    ).fetchone()
    if row is None and check_mtime:
        # A copy that already lost dedupe to a more complete copy counts as
        # collected until it changes; if it grows, it is parsed and compared again.
        try:
            row = conn.execute(
                "SELECT collected_at FROM session_log_aliases WHERE jsonl_path = ?",
                (str(jsonl_path),),
            ).fetchone()
        except _measure_facade.sqlite3.Error:
            row = None
    if row is None:
        return False
    if not check_mtime:
        return True  # Other adapters use synthetic IDs, not filesystem paths.
    try:
        collected = _measure_facade.datetime.fromisoformat(row[0]).timestamp()
        path = _measure_facade.Path(jsonl_path)
        paths = [path, *_measure_facade._find_subagent_jsonl_files(path)]
        return all(path.stat().st_mtime <= collected for path in paths)
    except (OSError, TypeError, ValueError):
        return False


def _adapter_activity_ts(parsed):
    """Latest activity of an adapter session (its end/updated time, else its
    start) as an aware datetime, or None. A resumed chat must land on the day
    it was last used, like a Claude transcript, not the day it started."""
    _measure_facade = _compat.facade()
    best = None
    for field in ("last_ts", "first_ts"):
        raw = parsed.get(field)
        if not raw:
            continue
        try:
            dt = _measure_facade.datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.astimezone()
        if best is None or dt > best:
            best = dt
    return best


def _store_adapter_activity(conn, dedup_key, parsed):
    """Keep adapter provenance; only split totals with actual day evidence.

    A normalized single-day session can be attributed without request records.
    Multi-day sessions without daily usage remain incomplete, never spread or
    assigned wholesale to the collector's date.
    """
    _measure_facade = _compat.facade()
    row = conn.execute("SELECT input_tokens,output_tokens,daily_metrics_json FROM session_log WHERE jsonl_path=?", (dedup_key,)).fetchone()
    if row is None or tuple(row[:2]) != (parsed.get("total_input_tokens"), parsed.get("total_output_tokens")):
        return 0
    bounds = sorted(filter(None, (_measure_facade._request_local_day(parsed.get("first_ts")),
                                 _measure_facade._request_local_day(parsed.get("last_ts")))))
    usage = parsed.get("daily_usage")
    calls = parsed.get("daily_calls") or {}
    if usage is None and len(bounds) == 2 and bounds[0] == bounds[1]:
        usage = {bounds[0]: parsed.get("model_usage_breakdown") or {}}
        calls = {bounds[0]: parsed.get("api_calls", 0)}
    # The adapter writes the same map as combined and parent evidence; retained
    # metadata can still extend beyond either its usage or parsed timestamps.
    days = _measure_facade._activity_bounds(usage, _measure_facade._safe_json_dict(row[2]))
    first = min(bounds + days) if bounds or days else None
    last = max(bounds + days) if bounds or days else None
    raw = _measure_facade.json.dumps(usage) if usage is not None else None
    params = (first, last, last or "", raw, raw, _measure_facade.json.dumps(calls), dedup_key)
    current = conn.execute(
        "SELECT first_activity_day,last_activity_day,date,daily_usage_json,parent_daily_usage_json,daily_calls_json "
        "FROM session_log WHERE jsonl_path=?", (dedup_key,)).fetchone()
    if current and tuple(current) == params[:-1]:
        return 0
    conn.execute(
        "UPDATE session_log SET first_activity_day=?,last_activity_day=?,date=?,daily_usage_json=?, "
        "parent_daily_usage_json=?,daily_calls_json=? WHERE jsonl_path=?", params)
    return 1


def _insert_normalized_session(conn, dedup_key, parsed, platform, project_fallback, quiet=None):
    """Insert or upgrade one normalized session row; returns 1 on new/upgraded.

    Owns the 33-column session_log INSERT and the in-place upgrade. An existing
    row is upgraded whenever its stored ``incomplete`` flag differs from the
    incoming one (0/1), so:
      - an idle-finalized row (incomplete=0) whose chat resumes activity
        (incomplete=1) flips back, instead of freezing (for
        long-lived IDE chats);
      - a crashed/active row (incomplete=1) that later sees sessionEnd flips to
        complete (incomplete=0) with refreshed totals.
    Uses INSERT OR IGNORE so the dedup key (`<platform>:<slug>`) is idempotent.
    """
    _measure_facade = _compat.facade()
    slug = parsed.get("slug") or ""
    if not slug:
        return 0

    active = _measure_facade._adapter_activity_ts(parsed)
    date = active.astimezone().strftime("%Y-%m-%d") if active else ""

    project_name = str(parsed.get("cwd") or project_fallback)
    is_incomplete = 1 if parsed.get("incomplete") else 0

    values = (
        date,
        project_name,
        parsed["duration_minutes"],
        parsed["total_input_tokens"],
        parsed["total_output_tokens"],
        parsed["message_count"],
        parsed.get("api_calls", 0),
        parsed["cache_hit_rate"],
        parsed.get("total_cache_create_1h", 0),
        parsed.get("total_cache_create_5m", 0),
        1,
        parsed.get("avg_call_gap_seconds"),
        parsed.get("max_call_gap_seconds"),
        parsed.get("p95_call_gap_seconds"),
        _measure_facade.json.dumps(parsed.get("skills_used", {})),
        _measure_facade.json.dumps(parsed.get("subagents_used", {})),
        _measure_facade.json.dumps(parsed.get("tool_calls", {})),
        _measure_facade.json.dumps(parsed.get("model_usage", {})),
        _measure_facade.json.dumps(parsed.get("model_usage", {})),
        _measure_facade.json.dumps(parsed.get("model_usage_breakdown", {})),
        parsed.get("version"),
        parsed.get("slug"),
        parsed.get("topic"),
        _measure_facade.datetime.now().isoformat(),
        parsed.get("quality_score", 0),
        parsed.get("quality_grade", "F"),
        0,
        parsed.get("cost_usd", 0.0),
        parsed.get("cost_source"),
        parsed.get("credits"),
        platform,
        is_incomplete,
    )

    if _measure_facade._is_file_collected(conn, dedup_key):
        try:
            existing = conn.execute(
                "SELECT incomplete FROM session_log WHERE jsonl_path = ?",
                (dedup_key,),
            ).fetchone()
        except _measure_facade.sqlite3.Error:
            existing = None
        if existing is not None and int(existing[0]) == is_incomplete:
            return _measure_facade._store_adapter_activity(conn, dedup_key, parsed)
        try:
            conn.execute(
                """UPDATE session_log SET
                     date=?, project=?, duration_minutes=?, input_tokens=?,
                     output_tokens=?, message_count=?, api_calls=?, cache_hit_rate=?,
                     cache_create_1h_tokens=?, cache_create_5m_tokens=?, cache_ttl_scanned=?,
                     avg_call_gap_seconds=?, max_call_gap_seconds=?, p95_call_gap_seconds=?,
                     skills_json=?, subagents_json=?, tool_calls_json=?, model_usage_json=?,
                     all_model_usage_json=?, model_usage_breakdown_json=?, version=?, slug=?,
                     topic=?, collected_at=?, quality_score=?, quality_grade=?,
                     stale_waste_tokens=?, cost_usd=?, cost_source=?, credits=?,
                     platform=?, incomplete=?
                   WHERE jsonl_path=?""",
                values + (dedup_key,),
            )
            _measure_facade._store_adapter_activity(conn, dedup_key, parsed)
            return 1
        except _measure_facade.sqlite3.Error as exc:
            if not quiet:
                print(f"[Token Optimizer] could not upgrade a {platform} session: {exc}")
            return 0

    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO session_log
               (jsonl_path, date, project, duration_minutes, input_tokens,
                output_tokens, message_count, api_calls, cache_hit_rate,
                cache_create_1h_tokens, cache_create_5m_tokens, cache_ttl_scanned,
                avg_call_gap_seconds, max_call_gap_seconds, p95_call_gap_seconds,
                skills_json, subagents_json, tool_calls_json, model_usage_json,
                all_model_usage_json, model_usage_breakdown_json, version, slug, topic, collected_at,
                quality_score, quality_grade, stale_waste_tokens,
                cost_usd, cost_source, credits, platform, incomplete)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (dedup_key,) + values,
        )
    except _measure_facade.sqlite3.Error as exc:
        if not quiet:
            print(f"[Token Optimizer] skipped a {platform} session: {exc}")
        return 0
    if cur.rowcount == 1:
        _measure_facade._store_adapter_activity(conn, dedup_key, parsed)
        return 1
    return 0


def _safe_json_dict(raw):
    _measure_facade = _compat.facade()
    try:
        data = _measure_facade.json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except (_measure_facade.json.JSONDecodeError, TypeError, ValueError):
        return {}


def _rebuild_aggregate_tables(conn):
    """Recompute daily aggregate tables from session_log to avoid double counts.

    The daily_stats portion is a single SQL aggregate (94x faster than the
    row-by-row INSERT-on-conflict loop on 50K rows, and O(1) memory instead
    of O(N)). The skill/model/subagent daily tables still need JSON parsing
    in Python (SQLite has no json_each for arbitrary shapes here), so those
    are batched with executemany per date group instead of one INSERT per row.
    """
    _measure_facade = _compat.facade()
    _measure_facade.ensure_activity_schema(conn)
    conn.execute("DELETE FROM daily_stats")
    conn.execute("DELETE FROM model_daily")
    conn.execute("DELETE FROM skill_daily")
    conn.execute("DELETE FROM subagent_daily")

    # daily_stats: single SQL aggregate. worst_grade uses MIN on a custom
    # collation surrogate (INSTR('FDCBAS', grade)) computed in SQL via a
    # subquery. The per-day worst grade is the one with the smallest INSTR
    # (F=1 is worst, A=5, S=6, NULL excluded).
    conn.execute(
        """INSERT INTO daily_stats
               (date, session_count, total_input, total_output,
                total_duration, avg_cache_hit, avg_quality_score, worst_grade)
           SELECT date,
                  COUNT(*),
                  COALESCE(SUM(input_tokens), 0),
                  COALESCE(SUM(output_tokens), 0),
                  COALESCE(SUM(duration_minutes), 0),
                  COALESCE(AVG(cache_hit_rate), 0),
                  COALESCE(AVG(quality_score), 0),
                  SUBSTR('FDCBAS', MIN(NULLIF(INSTR('FDCBAS', quality_grade),0)),1)
           FROM session_activity
           GROUP BY date"""
    )

    # skill_daily / model_daily / subagent_daily: still need JSON parsing in
    # Python. Batch with executemany per date group to avoid per-row
    # statement overhead. Only fetch the JSON columns + date.
    rows = conn.execute(
        """SELECT date, skills_json, subagents_json,
                  model_usage_json, all_model_usage_json
           FROM session_activity"""
    ).fetchall()
    skill_batch = []
    model_batch = []
    subagent_batch = []
    for date, skills_json, subagents_json, model_usage_json, all_model_usage_json in rows:
        for skill, invocations in _measure_facade._safe_json_dict(skills_json).items():
            skill_batch.append((date, skill, 1, int(invocations or 0)))
        model_usage_for_daily = _measure_facade._safe_json_dict(all_model_usage_json)
        if not model_usage_for_daily:
            model_usage_for_daily = _measure_facade._safe_json_dict(model_usage_json)
        for model_id, tokens in model_usage_for_daily.items():
            normalized = _measure_facade._normalize_model_name(model_id)
            if normalized is None:
                continue
            model_batch.append((date, normalized, int(tokens or 0)))
        for agent_type, count in _measure_facade._safe_json_dict(subagents_json).items():
            subagent_batch.append((date, agent_type, int(count or 0)))
    if skill_batch:
        conn.executemany(
            """INSERT INTO skill_daily (date, skill, session_count, invocations)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(date, skill) DO UPDATE SET
                 session_count = session_count + excluded.session_count,
                 invocations = invocations + excluded.invocations""",
            skill_batch,
        )
    if model_batch:
        conn.executemany(
            """INSERT INTO model_daily (date, model, total_tokens)
               VALUES (?, ?, ?)
               ON CONFLICT(date, model) DO UPDATE SET
                 total_tokens = total_tokens + excluded.total_tokens""",
            model_batch,
        )
    if subagent_batch:
        conn.executemany(
            """INSERT INTO subagent_daily (date, agent_type, spawn_count)
               VALUES (?, ?, ?)
               ON CONFLICT(date, agent_type) DO UPDATE SET
                 spawn_count = spawn_count + excluded.spawn_count""",
            subagent_batch,
        )


def _needs_model_daily_rebuild(conn):
    """Check if DB predates the model attribution fix (schema version < 2)."""
    _measure_facade = _compat.facade()
    try:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        return ver < 2
    except _measure_facade.sqlite3.Error:
        return False


def _needs_streaming_dedup_rebuild(conn):
    """v5.4.9: check if DB was built before the streaming-dedup + subagent
    roll-up fix (schema version < 3). Pre-v5.4.9 sessions stored
    output_tokens from the FIRST streaming chunk instead of the final
    cumulative value, and excluded subagent output entirely."""
    _measure_facade = _compat.facade()
    try:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        return ver < 3
    except _measure_facade.sqlite3.Error:
        return False


def _migrate_model_daily(conn, quiet=None):
    """One-time migration for the model attribution fix: wipe model_daily so it rebuilds correctly.

    Only deletes model_daily (lightweight aggregate table). session_log is
    preserved. New sessions collected after this get correct model attribution.
    Users wanting full historical accuracy can run `collect --rebuild`.
    """
    _measure_facade = _compat.facade()
    try:
        # Set version FIRST to break retry loop on failure (Error Handling H1).
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        conn.execute("DELETE FROM model_daily")
        conn.commit()
        if not quiet:
            print("[Token Optimizer] Migrated model_daily for corrected model attribution.")
            print("  New sessions will have correct model mix. For full historical accuracy:")
            print(_measure_facade._hint("  python3 measure.py collect --rebuild"))
    except _measure_facade.sqlite3.Error as e:
        print(f"  [Token Optimizer] model_daily migration failed: {e}", file=_measure_facade.sys.stderr)


def _migrate_streaming_dedup(conn, quiet=None):
    """v5.4.9 one-time migration: wipe ALL aggregate tables so the next
    session-end-flush rebuilds with streaming-aware MAX-dedup and subagent
    roll-up. session_log stores per-session output_tokens which was wrong
    pre-v5.4.9, so we wipe that too. Reparsing happens automatically on
    next `collect` (triggered by SessionEnd hook or /token-dashboard).

    v5.4.13: set user_version FIRST (before DELETE) to prevent a
    migration loop where a failed COMMIT leaves user_version < 3 and
    triggers a full data wipe on every subsequent SessionStart. If the
    DELETE then fails, user_version is already 3 so the migration won't
    re-fire — the data stays stale but doesn't get wiped repeatedly.
    """
    _measure_facade = _compat.facade()
    try:
        # Set version FIRST to break the retry loop on failure.
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        conn.execute("DELETE FROM session_log")
        conn.execute("DELETE FROM session_log_aliases")
        conn.execute("DELETE FROM usage_request_claims")
        conn.execute("DELETE FROM daily_stats")
        conn.execute("DELETE FROM model_daily")
        conn.execute("DELETE FROM skill_daily")
        conn.execute("DELETE FROM subagent_daily")
        # Drop event tables too: their session_id/session_uuid references would
        # otherwise dangle (no FK constraints) and silently inflate savings.
        conn.execute("DELETE FROM savings_events")
        conn.execute("DELETE FROM compression_events")
        conn.execute("DELETE FROM counted_reread")
        conn.commit()
        if not quiet:
            print("[Token Optimizer] Migrated to v5.4.9 streaming-aware token counting.")
            print("  Old data cleared. Your next session will rebuild automatically.")
    except _measure_facade.sqlite3.Error as e:
        print(f"  [Token Optimizer] migration failed: {e}", file=_measure_facade.sys.stderr)


def _hermes_refresh_session_totals(conn, dedup_key, parsed):
    """Refresh an existing Hermes row's per-session numbers from a newer snapshot.

    Returns True when the row was updated. The rule is monotonic: Hermes's
    state.db message and token counters only grow during a session, so the new
    snapshot wins only when it is >= the stored row on every counter and > on
    at least one. Quality and skills columns keep their own rules (they need a
    rollup's live reading) and are not touched here.
    """
    _measure_facade = _compat.facade()
    stored = conn.execute(
        "SELECT message_count, input_tokens, output_tokens, api_calls "
        "FROM session_log WHERE jsonl_path = ?",
        (dedup_key,),
    ).fetchone()
    if stored is None:
        return False
    new = (
        int(parsed.get("message_count") or 0),
        int(parsed.get("total_input_tokens") or 0),
        int(parsed.get("total_output_tokens") or 0),
        int(parsed.get("api_calls") or 0),
    )
    old = tuple(int(v or 0) for v in stored)
    if any(n < o for n, o in zip(new, old)) or new == old:
        return False
    conn.execute(
        """UPDATE session_log
           SET duration_minutes = ?, input_tokens = ?, output_tokens = ?,
               message_count = ?, api_calls = ?, cache_hit_rate = ?,
               cache_create_1h_tokens = ?, cache_create_5m_tokens = ?,
               avg_call_gap_seconds = ?, max_call_gap_seconds = ?,
               p95_call_gap_seconds = ?, subagents_json = ?, tool_calls_json = ?,
               model_usage_json = ?, all_model_usage_json = ?,
               model_usage_breakdown_json = ?, version = ?, topic = ?,
               collected_at = ?
           WHERE jsonl_path = ?""",
        (
            parsed["duration_minutes"],
            parsed["total_input_tokens"],
            parsed["total_output_tokens"],
            parsed["message_count"],
            parsed.get("api_calls", 0),
            parsed["cache_hit_rate"],
            parsed.get("total_cache_create_1h", 0),
            parsed.get("total_cache_create_5m", 0),
            parsed.get("avg_call_gap_seconds"),
            parsed.get("max_call_gap_seconds"),
            parsed.get("p95_call_gap_seconds"),
            _measure_facade.json.dumps(parsed.get("subagents_used", {})),
            _measure_facade.json.dumps(parsed.get("tool_calls", {})),
            _measure_facade.json.dumps(parsed.get("model_usage", {})),
            _measure_facade.json.dumps(parsed.get("model_usage", {})),
            _measure_facade.json.dumps(parsed.get("model_usage_breakdown", {})),
            parsed.get("version"),
            parsed.get("topic"),
            _measure_facade.datetime.now().isoformat(),
            dedup_key,
        ),
    )
    _measure_facade._store_adapter_activity(conn, dedup_key, parsed)
    return True


def _collect_hermes_sessions(days=None, quiet=None, rebuild=None):
    """Collect Hermes sessions from state.db into the trends DB.

    Mirrors collect_sessions() but sources rows from hermes_state.recent_sessions()
    + hermes_session.normalize_session() instead of JSONL files.
    Uses session slug as the dedup key (jsonl_path column) so idempotent.
    """
    _measure_facade = _compat.facade()
    import hermes_state as _hs  # noqa: PLC0415

    conn = _measure_facade._init_trends_db()
    try:
        if rebuild:
            if not quiet:
                print("[Token Optimizer] Rebuilding Hermes trends DB...")
            conn.execute("PRAGMA user_version = 3")
            conn.execute("DELETE FROM session_log")
            conn.execute("DELETE FROM session_log_aliases")
            conn.execute("DELETE FROM daily_stats")
            conn.execute("DELETE FROM model_daily")
            conn.execute("DELETE FROM skill_daily")
            conn.execute("DELETE FROM subagent_daily")
            # Drop event tables too: their session_id/session_uuid references would
            # otherwise dangle (no FK constraints) and silently inflate savings.
            conn.execute("DELETE FROM savings_events")
            conn.execute("DELETE FROM compression_events")
            conn.execute("DELETE FROM counted_reread")
            conn.commit()

        rows = _hs.recent_sessions(days=days)
        if not rows:
            if not quiet:
                print(f"No Hermes sessions found in the last {days} days.")
            return 0

        new_count = 0
        updated_count = 0
        for row in rows:
            session_id = str(row.get("id") or "")
            live_context_tokens = _measure_facade.__dict__.get("_HERMES_ROLLUP_CONTEXT", {}).get(session_id)
            parsed = _measure_facade.hermes_session.normalize_session(row, context_tokens=live_context_tokens)
            if not parsed:
                continue
            # Skill loads the plugin counted from Hermes's on_skill_lifecycle
            # hook (state.db does not record them).
            rollup_skills = _measure_facade.__dict__.get("_HERMES_ROLLUP_SKILLS", {}).get(session_id)
            if rollup_skills:
                parsed["skills_used"] = dict(rollup_skills)

            slug = parsed.get("slug") or ""
            dedup_key = f"hermes:{slug}" if slug else None
            if not dedup_key:
                continue

            if _measure_facade._is_file_collected(conn, dedup_key):
                new_count += _measure_facade._store_adapter_activity(conn, dedup_key, parsed)
                # A collect that ran while the session was still live stored
                # its totals at that point. Hermes's state.db counters only
                # grow, so refresh the per-session numbers whenever this
                # snapshot is at least as far along on every counter and ahead
                # on one. A snapshot behind on any counter is stale (e.g. a
                # dashboard collect that read state.db before a finished
                # rollup wrote) and never overwrites the newer row. Totals are
                # replaced, never added, and the aggregates are rebuilt from
                # session_log, so a refresh cannot double count.
                if _measure_facade._hermes_refresh_session_totals(conn, dedup_key, parsed):
                    updated_count += 1
                # A rollup carries the session's live occupancy, which an
                # earlier mid-session collect could not see. Refresh the stored
                # grade in place so the first write does not freeze a
                # fill-blind score for the life of the DB. Non-rollup
                # collections (no live reading) keep the dedup's first write.
                if live_context_tokens is not None:
                    conn.execute(
                        """UPDATE session_log
                           SET quality_score = ?, quality_grade = ?
                           WHERE jsonl_path = ?""",
                        (
                            parsed.get("quality_score", 0),
                            parsed.get("quality_grade", "F"),
                            dedup_key,
                        ),
                    )
                    updated_count += 1
                if rollup_skills:
                    conn.execute(
                        "UPDATE session_log SET skills_json = ? WHERE jsonl_path = ?",
                        (_measure_facade.json.dumps(parsed["skills_used"]), dedup_key),
                    )
                    updated_count += 1
                continue

            started_at = row.get("started_at")
            try:
                date = (
                    _measure_facade.datetime.fromtimestamp(float(started_at)).strftime("%Y-%m-%d")
                    if started_at is not None
                    else _measure_facade.datetime.now().strftime("%Y-%m-%d")
                )
            except (TypeError, ValueError, OSError):
                date = _measure_facade.datetime.now().strftime("%Y-%m-%d")
            project_name = str(row.get("cwd") or "hermes")

            cur = conn.execute(
                """INSERT OR IGNORE INTO session_log
                   (jsonl_path, date, project, duration_minutes, input_tokens,
                    output_tokens, message_count, api_calls, cache_hit_rate,
                    cache_create_1h_tokens, cache_create_5m_tokens, cache_ttl_scanned,
                    avg_call_gap_seconds, max_call_gap_seconds, p95_call_gap_seconds,
                    skills_json, subagents_json, tool_calls_json, model_usage_json,
                    all_model_usage_json, model_usage_breakdown_json, version, slug, topic, collected_at,
                    quality_score, quality_grade, stale_waste_tokens, platform)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    dedup_key, date, project_name,
                    parsed["duration_minutes"],
                    parsed["total_input_tokens"],
                    parsed["total_output_tokens"],
                    parsed["message_count"],
                    parsed.get("api_calls", 0),
                    parsed["cache_hit_rate"],
                    parsed.get("total_cache_create_1h", 0),
                    parsed.get("total_cache_create_5m", 0),
                    1,
                    parsed.get("avg_call_gap_seconds"),
                    parsed.get("max_call_gap_seconds"),
                    parsed.get("p95_call_gap_seconds"),
                    _measure_facade.json.dumps(parsed.get("skills_used", {})),
                    _measure_facade.json.dumps(parsed.get("subagents_used", {})),
                    _measure_facade.json.dumps(parsed.get("tool_calls", {})),
                    _measure_facade.json.dumps(parsed.get("model_usage", {})),
                    _measure_facade.json.dumps(parsed.get("model_usage", {})),
                    _measure_facade.json.dumps(parsed.get("model_usage_breakdown", {})),
                    parsed.get("version"),
                    parsed.get("slug"),
                    parsed.get("topic"),
                    _measure_facade.datetime.now().isoformat(),
                    parsed.get("quality_score", 0),
                    parsed.get("quality_grade", "F"),
                    0,
                    "hermes",
                ),
            )
            if cur.rowcount != 1:
                continue
            _measure_facade._store_adapter_activity(conn, dedup_key, parsed)
            new_count += 1

        # Q2: only rebuild aggregates when rows were actually inserted or
        # refreshed (daily_stats carries avg_quality_score / worst_grade).
        if new_count > 0 or updated_count > 0:
            _measure_facade._rebuild_aggregate_tables(conn)
        conn.commit()
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
    finally:
        conn.close()

    if not quiet:
        total = _measure_facade.conn_total_sessions() if _measure_facade.TRENDS_DB.exists() else new_count
        print(f"[Token Optimizer] Collected {new_count} new Hermes sessions. Total in DB: {total}")
    return new_count


def _parse_hermes_rollup_skills(args):
    """Parse ``--session`` + ``--skills-json`` out of hermes-rollup CLI args.

    Returns ``{session_id: {skill_name: loads}}`` or ``{}``. The JSON comes
    from the plugin's in-memory count; anything malformed is dropped, names
    are capped at 128 characters and counts must be positive integers.
    """
    _measure_facade = _compat.facade()
    session = ""
    raw = ""
    i = 0
    while i < len(args):
        if args[i] == "--session" and i + 1 < len(args):
            session = args[i + 1]
            i += 2
        elif args[i] == "--skills-json" and i + 1 < len(args):
            raw = args[i + 1]
            i += 2
        else:
            i += 1
    if not session or not raw or len(raw) > 65_536:
        return {}
    try:
        data = _measure_facade.json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    skills = {}
    for name, count in list(data.items())[:_measure_facade._HERMES_SKILLS_MAX]:
        if (isinstance(name, str) and name.strip() and isinstance(count, int)
                and not isinstance(count, bool) and count > 0):
            skills[name.strip()[:128]] = min(count, 1_000_000)
    return {session: skills} if skills else {}


def _parse_hermes_rollup_context(args):
    """Parse ``--session``/``--context-tokens`` out of hermes-rollup CLI args.

    Returns ``{session_id: context_tokens}`` when both are present and the
    token count is a positive integer; ``{}`` otherwise. A zero, negative, or
    non-numeric value is not a real occupancy reading — it degrades to
    "unavailable" so the collected row omits fill instead of storing a fake
    0% fill. ``--platform``/``--reason`` are accepted and skipped so the
    bridge's exact call signature never causes an error.
    """
    _measure_facade = _compat.facade()
    session = ""
    context_tokens = None
    i = 0
    while i < len(args):
        if args[i] == "--session" and i + 1 < len(args):
            session = args[i + 1]
            i += 2
        elif args[i] == "--context-tokens" and i + 1 < len(args):
            try:
                value = int(args[i + 1])
            except ValueError:
                value = 0
            context_tokens = value if value > 0 else None
            i += 2
        elif args[i] in ("--platform", "--reason") and i + 1 < len(args):
            i += 2
        else:
            i += 1
    return {session: context_tokens} if session and context_tokens is not None else {}


def _hermes_transcript_dir():
    _measure_facade = _compat.facade()
    return _measure_facade.CHECKPOINT_DIR.parent / "hermes-transcripts"


def _sweep_hermes_transcripts(directory, now=None):
    _measure_facade = _compat.facade()
    now = _measure_facade.time.time() if now is None else now
    try:
        for p in directory.glob("*.jsonl"):
            try:
                if not p.is_symlink() and now - p.stat().st_mtime > _measure_facade._HERMES_TRANSCRIPT_STALE_SECONDS:
                    p.unlink()
            except OSError:
                continue
    except OSError:
        pass


def hermes_checkpoint(session_id, trigger=None, fill_pct=None, cwd=None):
    """Write a session checkpoint for a Hermes session.

    Hermes keeps its transcript in state.db, not a JSONL file. Read the
    session's messages read-only, translate them into the record shape the
    shared checkpoint engine reads, and hand that to ``compact_capture``. The
    translated file lives in Token Optimizer's own private directory for the
    duration of the call only.

    Returns the checkpoint path, or None (no session id, no messages, or the
    engine declined). Never falls back to guessing another transcript: a
    Claude Code session on the same machine must never become a Hermes
    checkpoint.
    """
    _measure_facade = _compat.facade()
    sid = str(session_id or "").strip()
    if not sid:
        return None
    try:
        import hermes_state as _hs  # noqa: PLC0415
        import hermes_session as _hsess  # noqa: PLC0415
    except Exception:
        return None
    records = _hsess.messages_to_transcript_records(_hs.session_messages(sid, limit=500))
    if not records:
        return None
    if not cwd:
        # The session's own working directory, so the checkpoint's git line
        # describes the user's project, not wherever this process started.
        row_cwd = (_hs.get_session(sid, live=True) or {}).get("cwd")
        try:
            if isinstance(row_cwd, str) and row_cwd and _measure_facade.Path(row_cwd).is_dir():
                cwd = row_cwd
        except (OSError, ValueError):
            cwd = None
    directory = _measure_facade._hermes_transcript_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            _measure_facade.os.chmod(str(directory), 0o700)
        except OSError:
            pass
    except OSError:
        return None
    _measure_facade._sweep_hermes_transcripts(directory)
    fd, tmp_name = _measure_facade.tempfile.mkstemp(prefix=".hermes-", suffix=".jsonl", dir=str(directory))
    tmp_path = _measure_facade.Path(tmp_name)
    try:
        with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as fh:
            for record in records:
                fh.write(_measure_facade.json.dumps(record, ensure_ascii=False) + "\n")
        return _measure_facade.compact_capture(
            session_id=sid,
            trigger=trigger,
            cwd=cwd,
            fill_pct=fill_pct,
            _trusted_transcript=tmp_path,
        )
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def _hermes_checkpoint_cli(args):
    """``hermes-checkpoint --session <id> [--trigger T] [--fill-pct N] [--cwd DIR] [--quiet]``."""
    _measure_facade = _compat.facade()
    sid = ""
    trigger = "stop"
    fill_pct = None
    cwd = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--session" and i + 1 < len(args):
            sid = args[i + 1]
            i += 2
        elif a == "--trigger" and i + 1 < len(args):
            trigger = args[i + 1]
            i += 2
        elif a == "--fill-pct" and i + 1 < len(args):
            try:
                fill_pct = float(args[i + 1])
            except ValueError:
                fill_pct = None
            i += 2
        elif a == "--cwd" and i + 1 < len(args):
            cwd = args[i + 1] or None
            i += 2
        else:
            i += 1
    quiet = "--quiet" in args or "-q" in args
    if cwd is not None:
        try:
            if not _measure_facade.Path(cwd).is_dir():
                cwd = None
        except (OSError, ValueError):
            cwd = None
    result = _measure_facade.hermes_checkpoint(sid, trigger=trigger, fill_pct=fill_pct, cwd=cwd)
    if result and not quiet:
        print(f"[Token Optimizer] Checkpoint saved: {result}")
    return result


def hermes_turn_context(payload):
    """Per-turn model context for a Hermes session, in one process.

    Same engines Claude Code's SessionStart / UserPromptSubmit hooks run:
      - first turn: the new-session checkpoint pointer (compact_restore)
      - every turn: the topic-matched prior-session hint (_continuity_prompt_hint)
      - when the plugin passes a live fill reading: the lean-output nudge
        (_verbosity_nudge_text). Hermes has no session-efficiency signal, so
        only the strong tier (75-89% fill) can apply; the plugin paces it.

    ``payload`` keys: session_id, prompt, first_turn, fill_pct, score.
    Returns {"parts": [...], "verbosity": bool}. Never raises.
    """
    _measure_facade = _compat.facade()
    out = {"parts": [], "verbosity": False}
    if not isinstance(payload, dict):
        return out
    sid = str(payload.get("session_id") or "").strip()
    if not sid:
        return out
    if payload.get("first_turn"):
        from contextlib import redirect_stdout as _redirect_stdout  # noqa: PLC0415

        buf = _measure_facade.io.StringIO()
        try:
            with _redirect_stdout(buf):
                _measure_facade.compact_restore(session_id=sid, new_session_only=True)
        except Exception:
            pass
        pointer = buf.getvalue().strip()
        if pointer:
            out["parts"].append(pointer)
    prompt = str(payload.get("prompt") or "").strip()
    if prompt:
        try:
            hint = (_measure_facade._continuity_prompt_hint(prompt_text=prompt[:4000], session_id=sid) or "").strip()
        except Exception:
            hint = ""
        if hint:
            out["parts"].append(hint)
    fill = payload.get("fill_pct")
    if isinstance(fill, (int, float)) and not isinstance(fill, bool):
        score = payload.get("score")
        score = float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else 100.0
        # No efficiency signal on Hermes: 100 keeps the gentle tier off.
        window = payload.get("context_window")
        cached = ({"model_context_window": int(window)}
                  if isinstance(window, int) and not isinstance(window, bool) and window > 0 else {})
        nudge = _measure_facade._verbosity_nudge_text(float(fill), score, 100, cached)
        if nudge:
            out["parts"].append(nudge)
            out["verbosity"] = True
    return out


def _resolve_copilot_home_wsl_aware(mnt_root=None):
    """Resolve the Copilot home with a WSL-root /mnt/ exception.

    Thin wrapper around ``runtime_env.copilot_home`` so the WSL ``/mnt/``
    opt-in has a SINGLE source of truth (``runtime_env._safe_home_from_env``
    → ``_wsl_mnt_safe_home``). The opt-in is now STRICTER than the previous
    shipped version: it is gated on actual WSL detection
    (``runtime_env._is_wsl_context`` reads ``/proc/version`` /
    ``/proc/sys/kernel/osrelease`` for the ``microsoft``/``WSL`` markers) so
    native-Linux ``/mnt`` mounts stay on the strict safe-home path and
    behavior there is byte-identical to before.

    Precedence (handled inside ``runtime_env.copilot_home``):
      1. TOKEN_OPTIMIZER_COPILOT_HOME (TO's own override) — strict under-$HOME
         guard, or the WSL-root /mnt/ opt-in. The only var users should set.
      2. COPILOT_HOME (Copilot's own var) — back-compat location hint; a /mnt/
         value earns a guardrail warning because native-Windows Copilot reads
         the same var and a /mnt value breaks its own logging.
      3. WSL-root auto-detect — probe /mnt/c/Users/*/.copilot for the sole
         Windows profile, so no env var is needed.
      4. Otherwise → $HOME/.copilot.

    ``mnt_root`` is a Path override for the WSL mount root, defaulting to
    ``/mnt``. It is a FUNCTION PARAMETER (not an env var) so it can ONLY be
    set by code calling this function directly (tests pass a temp dir since
    macOS can't create /mnt/c/...). The ``copilot-home`` subcommand never
    passes it, so production always uses the real ``/mnt`` — there is no env
    var a user could set to widen the /mnt/ confinement.

    Returns a Path. Never raises — falls back to copilot_home() on any error.
    """
    _measure_facade = _compat.facade()
    from runtime_env import copilot_home as _ch  # noqa: PLC0415

    return _ch(mnt_root=mnt_root)


def _write_copilot_restore_context(sessions, quiet=None):
    """Maintain the continuity file the sessionStart hook injects.

    Summarizes the most recent completed CLI session so a new Copilot session
    starts grounded. Capped well under the bridge's 16KB read limit.
    """
    _measure_facade = _compat.facade()
    try:
        from runtime_env import copilot_home as _ch  # noqa: PLC0415

        cli = [s for s in sessions if s.get("token_source") == "copilot_cli_events"]
        if not cli:
            return
        # Prefer sessions with a real timestamp; only fall back to a dated-None
        # session when none have one (sort key keeps real ts ahead of None).
        latest = max(cli, key=lambda s: ((_measure_facade := _compat.facade()), (s.get("first_ts") is not None, s.get("first_ts") or ""))[1])

        def _clean(value):
            # These come from Copilot's reverse-engineered event stream and are
            # injected verbatim into the model's context — strip newlines/control
            # chars and cap length so a crafted topic/cwd can't inject directives.
            _measure_facade = _compat.facade()
            text = str(value or "")
            text = "".join(ch for ch in text if ch == " " or ch.isprintable())
            return text[:200]

        lines = ["[Token Optimizer] Continuity from your previous Copilot session:"]
        if latest.get("topic"):
            lines.append(f"- Topic: {_clean(latest['topic'])}")
        if latest.get("cwd"):
            lines.append(f"- Working dir: {_clean(latest['cwd'])}")
        lines.append(
            f"- Model: {_clean(latest.get('model', 'unknown'))}; "
            f"{latest.get('total_input_tokens', 0):,} in / "
            f"{latest.get('total_output_tokens', 0):,} out tokens"
        )
        if latest.get("incomplete"):
            lines.append("- NOTE: that session ended without a clean shutdown (crash/kill).")
        path = _ch() / "token-optimizer" / "restore-context.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        # mkstemp + replace: an interrupted write or a cross-device replace
        # never leaves an orphan tmp file behind.
        import tempfile  # noqa: PLC0415

        fd, tmp_name = tempfile.mkstemp(prefix=".restore-context.", suffix=".tmp", dir=str(path.parent))
        try:
            with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            _measure_facade.os.replace(tmp_name, str(path))
        except OSError:
            try:
                _measure_facade.os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception as exc:
        if not quiet:
            print(f"[Token Optimizer] restore-context update skipped: {exc}")


def _grok_summary():
    """Token/cost-led session summary for Grok Build.

    Grok Build ships authoritative per-turn usage (updates.jsonl) with a USD
    cost in 1e10 ticks, so both tokens and cost lead here — unlike Copilot's
    credits-only summary.
    """
    _measure_facade = _compat.facade()
    import grok_state as _gs  # noqa: PLC0415
    import grok_session  # noqa: PLC0415
    from runtime_env import grok_home as _gh  # noqa: PLC0415

    raw_sessions = list(_gs.read_all_sessions())
    normalized = [s for s in (grok_session.normalize_session(r) for r in raw_sessions) if s]

    print("Token Optimizer — Grok Build summary")
    if not normalized:
        print()
        print("  No Grok Build sessions found yet.")
        print(f"  Sessions live under {_gh()}/sessions/.")
        print()
        print("  Full trends: measure.py grok-rollup (sessions land in trends.db).")
        return

    total_cost = sum(s.get("cost_usd") or 0.0 for s in normalized)
    cost_known = sum(1 for s in normalized if s.get("cost_source") == "grok_cost_usd_ticks")
    total_in = sum(s.get("total_input_tokens", 0) for s in normalized)
    total_out = sum(s.get("total_output_tokens", 0) for s in normalized)
    incomplete = sum(1 for s in normalized if s.get("incomplete"))
    estimated = sum(1 for s in normalized if s.get("estimated"))
    models = {}
    for s in normalized:
        for m, v in (s.get("model_usage") or {}).items():
            models[m] = models.get(m, 0) + v
    top_models = sorted(models.items(), key=lambda kv: ((_measure_facade := _compat.facade()), -kv[1])[1])[:3]

    print()
    print(f"  {len(normalized)} session(s)")
    if cost_known:
        print(f"    Cost: ~${total_cost:,.2f} ({cost_known} session(s) with authoritative cost)")
    else:
        print("    Cost: no authoritative billing data recorded (costUsdTicks scrubbed)")
    print(f"    Tokens: {total_in:,} in / {total_out:,} out")
    if top_models:
        print("    Models: " + ", ".join(f"{_measure_facade._strip_ansi(str(m))} ({v:,})" for m, v in top_models))
    if incomplete:
        print(f"    {incomplete} session(s) ended without clean shutdown (usageIsIncomplete)")
    if estimated:
        print(f"    {estimated} session(s) use estimated token counts (~est.)")
    print()
    print("  Full trends: measure.py grok-rollup (sessions land in trends.db).")


def _collect_grok_sessions(days=None, quiet=None, rebuild=None):
    """Collect Grok Build sessions from $GROK_HOME/sessions into the trends DB.

    Mirrors _collect_copilot_sessions: dedup via the jsonl_path column
    (``grok:<session-uuid>``), platform ``grok``, idempotent. Cost is
    token-derived from the authoritative costUsdTicks field (not credits).
    """
    _measure_facade = _compat.facade()
    import grok_state as _gs  # noqa: PLC0415
    import grok_session  # noqa: PLC0415

    try:
        conn = _measure_facade._init_trends_db()
    except _measure_facade.sqlite3.DatabaseError:
        if _measure_facade.TRENDS_DB.exists():
            try:
                stamp = int(_measure_facade.datetime.now().timestamp())
                _measure_facade.TRENDS_DB.rename(_measure_facade.TRENDS_DB.with_suffix(f".db.corrupt.{stamp}"))
            except OSError:
                pass
        conn = _measure_facade._init_trends_db()
    try:
        if rebuild:
            if not quiet:
                print("[Token Optimizer] Rebuilding Grok trends DB...")
            conn.execute("PRAGMA user_version = 3")
            conn.execute("DELETE FROM session_log WHERE jsonl_path LIKE 'grok:%'")
            conn.commit()

        try:
            raw_sessions = list(_gs.read_all_sessions())
        except Exception as exc:
            if not quiet:
                print(f"[Token Optimizer] Grok Build scan failed: {exc}")
            raw_sessions = []

        cutoff = _measure_facade.datetime.now().timestamp() - days * 86400
        new_count = 0
        normalized = []
        for raw in raw_sessions:
            parsed = grok_session.normalize_session(raw)
            if not parsed:
                continue
            normalized.append(parsed)

            slug = parsed.get("slug") or ""
            if not slug:
                continue
            dedup_key = f"grok:{slug}"
            is_incomplete = 1 if parsed.get("incomplete") else 0
            if _measure_facade._is_file_collected(conn, dedup_key):
                new_count += _measure_facade._store_adapter_activity(conn, dedup_key, parsed)
                if not is_incomplete:
                    try:
                        existing = conn.execute(
                            "SELECT incomplete FROM session_log WHERE jsonl_path = ?",
                            (dedup_key,),
                        ).fetchone()
                    except _measure_facade.sqlite3.Error:
                        existing = None
                    if existing is not None and existing[0]:
                        try:
                            conn.execute(
                                """UPDATE session_log SET
                                     input_tokens=?, output_tokens=?, message_count=?,
                                     api_calls=?, cache_hit_rate=?, cache_create_1h_tokens=?,
                                     duration_minutes=?, quality_score=?, quality_grade=?,
                                     cost_usd=?, cost_source=?, credits=?, incomplete=0
                                   WHERE jsonl_path=? AND incomplete=1""",
                                (
                                    parsed["total_input_tokens"], parsed["total_output_tokens"],
                                    parsed["message_count"], parsed.get("api_calls", 0),
                                    parsed["cache_hit_rate"], parsed.get("total_cache_create_1h", 0),
                                    parsed["duration_minutes"], parsed.get("quality_score", 0),
                                    parsed.get("quality_grade", "F"), parsed.get("cost_usd", 0.0),
                                    parsed.get("cost_source"), parsed.get("credits"), dedup_key,
                                ),
                            )
                            _measure_facade._store_adapter_activity(conn, dedup_key, parsed)
                            new_count += 1
                        except _measure_facade.sqlite3.Error as exc:
                            if not quiet:
                                print(f"[Token Optimizer] could not upgrade a Grok session: {exc}")
                continue

            active = _measure_facade._adapter_activity_ts(parsed)
            if active and active.timestamp() < cutoff:
                continue
            date = active.astimezone().strftime("%Y-%m-%d") if active else ""
            project_name = str(parsed.get("cwd") or "grok")

            try:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO session_log
                       (jsonl_path, date, project, duration_minutes, input_tokens,
                        output_tokens, message_count, api_calls, cache_hit_rate,
                        cache_create_1h_tokens, cache_create_5m_tokens, cache_ttl_scanned,
                        avg_call_gap_seconds, max_call_gap_seconds, p95_call_gap_seconds,
                        skills_json, subagents_json, tool_calls_json, model_usage_json,
                        all_model_usage_json, model_usage_breakdown_json, version, slug, topic, collected_at,
                        quality_score, quality_grade, stale_waste_tokens,
                        cost_usd, cost_source, credits, platform, incomplete)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        dedup_key, date, project_name,
                        parsed["duration_minutes"],
                        parsed["total_input_tokens"],
                        parsed["total_output_tokens"],
                        parsed["message_count"],
                        parsed.get("api_calls", 0),
                        parsed["cache_hit_rate"],
                        parsed.get("total_cache_create_1h", 0),
                        parsed.get("total_cache_create_5m", 0),
                        1,
                        parsed.get("avg_call_gap_seconds"),
                        parsed.get("max_call_gap_seconds"),
                        parsed.get("p95_call_gap_seconds"),
                        _measure_facade.json.dumps(parsed.get("skills_used", {})),
                        _measure_facade.json.dumps(parsed.get("subagents_used", {})),
                        _measure_facade.json.dumps(parsed.get("tool_calls", {})),
                        _measure_facade.json.dumps(parsed.get("model_usage", {})),
                        _measure_facade.json.dumps(parsed.get("model_usage", {})),
                        _measure_facade.json.dumps(parsed.get("model_usage_breakdown", {})),
                        parsed.get("version"),
                        parsed.get("slug"),
                        parsed.get("topic"),
                        _measure_facade.datetime.now().isoformat(),
                        parsed.get("quality_score", 0),
                        parsed.get("quality_grade", "F"),
                        0,
                        parsed.get("cost_usd", 0.0),
                        parsed.get("cost_source"),
                        parsed.get("credits"),
                        "grok",
                        is_incomplete,
                    ),
                )
            except _measure_facade.sqlite3.Error as exc:
                if not quiet:
                    print(f"[Token Optimizer] skipped a Grok session: {exc}")
                continue
            if cur.rowcount != 1:
                continue
            _measure_facade._store_adapter_activity(conn, dedup_key, parsed)
            new_count += 1

        if new_count > 0:
            _measure_facade._rebuild_aggregate_tables(conn)
        conn.commit()
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
    finally:
        conn.close()

    if not quiet:
        print(f"[Token Optimizer] Collected {new_count} new Grok Build sessions.")
    return new_count


def _copilot_summary():
    """Credits-led session summary for GitHub Copilot (CLI + VS Code planes).

    Cost leads: Copilot ships its own fill indicators, but since the
    June 2026 AI-Credits billing switch nothing answers "what did this cost me".
    """
    _measure_facade = _compat.facade()
    import copilot_state as _cps  # noqa: PLC0415

    try:
        import copilot_vscode as _cpv  # noqa: PLC0415
    except ImportError:
        _cpv = None

    planes = (
        ("Copilot CLI", list(_cps.read_all_sessions())),
        ("VS Code Copilot", list(_cpv.read_sessions()) if _cpv is not None else []),
    )

    print("Token Optimizer — GitHub Copilot summary")
    any_data = False
    for label, raw_sessions in planes:
        normalized = [s for s in (_measure_facade.copilot_session.normalize_session(r) for r in raw_sessions) if s]
        if not normalized:
            continue
        any_data = True
        total_cost = sum(s.get("cost_usd") or 0.0 for s in normalized)
        credits = sum(s.get("credits") or 0.0 for s in normalized if s.get("credits"))
        total_in = sum(s.get("total_input_tokens", 0) for s in normalized)
        total_out = sum(s.get("total_output_tokens", 0) for s in normalized)
        incomplete = sum(1 for s in normalized if s.get("incomplete"))
        estimated = sum(1 for s in normalized if s.get("estimated"))
        models = {}
        for s in normalized:
            for m, v in (s.get("model_usage") or {}).items():
                models[m] = models.get(m, 0) + v
        top_models = sorted(models.items(), key=lambda kv: ((_measure_facade := _compat.facade()), -kv[1])[1])[:3]

        print()
        print(f"  {label}: {len(normalized)} session(s)")
        if credits:
            print(f"    AI credits: {credits:,.2f}  (~${total_cost:,.2f})")
        elif total_cost:
            print(f"    Cost: ~${total_cost:,.2f} (premium requests)")
        else:
            print("    Cost: no billing data recorded by Copilot for these sessions")
        print(f"    Tokens: {total_in:,} in / {total_out:,} out")
        if top_models:
            print("    Models: " + ", ".join(f"{_measure_facade._strip_ansi(str(m))} ({v:,})" for m, v in top_models))
        if incomplete:
            print(f"    {incomplete} session(s) ended without clean shutdown (partial data)")
        if estimated:
            print(f"    {estimated} session(s) use estimated token counts (~est.)")
    if not any_data:
        print()
        print("  No Copilot sessions found yet.")
        print("  CLI: run a `copilot` session. VS Code: enable the two")
        print('  "github.copilot.chat.agentDebugLog" settings for per-request cost.')
    print()
    print("  Full trends: measure.py copilot-rollup (sessions land in trends.db;")
    print("  cost figures are Copilot's own, never a repriced token estimate).")


def _collect_copilot_sessions(days=None, quiet=None, rebuild=None):
    """Collect Copilot sessions (CLI + VS Code planes) into the trends DB.

    Mirrors _collect_hermes_sessions. The two planes are separate session
    populations with distinct dedup keys — never merged, never summed.
    Idempotent via the jsonl_path dedup column.

    TODO(dedup): the 28-column INSERT here and in _collect_hermes_sessions is
    copy-paste; extract _insert_normalized_session() on the next touch to
    either collector (date resolution + dedup-key stay platform-specific).
    """
    _measure_facade = _compat.facade()
    import copilot_state as _cps  # noqa: PLC0415

    try:
        import copilot_vscode as _cpv  # noqa: PLC0415
    except ImportError:
        _cpv = None

    try:
        conn = _measure_facade._init_trends_db()
    except _measure_facade.sqlite3.DatabaseError:
        # Corrupt trends.db (disk error, partial write): quarantine it and
        # start fresh rather than crashing the silent stop-hook rollup.
        if _measure_facade.TRENDS_DB.exists():
            try:
                stamp = int(_measure_facade.datetime.now().timestamp())
                _measure_facade.TRENDS_DB.rename(_measure_facade.TRENDS_DB.with_suffix(f".db.corrupt.{stamp}"))
            except OSError:
                pass
        conn = _measure_facade._init_trends_db()
    try:
        if rebuild:
            if not quiet:
                print("[Token Optimizer] Rebuilding Copilot trends DB...")
            conn.execute("PRAGMA user_version = 3")
            # Scope the wipe to Copilot rows ONLY — session_log is shared with
            # Claude/Codex/Hermes/OpenCode. An unscoped DELETE would nuke every
            # runtime's history. Aggregates are fully recomputed from the
            # surviving session_log rows below.
            conn.execute("DELETE FROM session_log WHERE jsonl_path LIKE 'copilot:%'")
            conn.commit()

        try:
            raw_sessions = list(_cps.read_all_sessions())
        except Exception as exc:
            if not quiet:
                print(f"[Token Optimizer] Copilot CLI scan failed: {exc}")
            raw_sessions = []
        if _cpv is not None:
            try:
                raw_sessions.extend(_cpv.read_sessions())
            except Exception as exc:
                if not quiet:
                    print(f"[Token Optimizer] VS Code Copilot scan skipped: {exc}")

        cutoff = _measure_facade.datetime.now().timestamp() - days * 86400
        new_count = 0
        normalized = []
        for raw in raw_sessions:
            parsed = _measure_facade.copilot_session.normalize_session(raw)
            if not parsed:
                continue
            normalized.append(parsed)

            slug = parsed.get("slug") or ""
            if not slug:
                continue
            dedup_key = f"copilot:{slug}"
            is_incomplete = 1 if parsed.get("incomplete") else 0
            if _measure_facade._is_file_collected(conn, dedup_key):
                new_count += _measure_facade._store_adapter_activity(conn, dedup_key, parsed)
                # Already recorded — but if the stored row was partial (the
                # session was still active / crashed when first rolled up) and
                # we now have COMPLETE totals, upgrade it in place. Otherwise
                # leave it (INSERT-OR-IGNORE semantics).
                if not is_incomplete:
                    try:
                        existing = conn.execute(
                            "SELECT incomplete FROM session_log WHERE jsonl_path = ?",
                            (dedup_key,),
                        ).fetchone()
                    except _measure_facade.sqlite3.Error:
                        existing = None
                    if existing is not None and existing[0]:
                        try:
                            conn.execute(
                                """UPDATE session_log SET
                                     input_tokens=?, output_tokens=?, message_count=?,
                                     api_calls=?, cache_hit_rate=?, cache_create_1h_tokens=?,
                                     duration_minutes=?, quality_score=?, quality_grade=?,
                                     cost_usd=?, cost_source=?, credits=?, incomplete=0
                                   WHERE jsonl_path=? AND incomplete=1""",
                                (
                                    parsed["total_input_tokens"], parsed["total_output_tokens"],
                                    parsed["message_count"], parsed.get("api_calls", 0),
                                    parsed["cache_hit_rate"], parsed.get("total_cache_create_1h", 0),
                                    parsed["duration_minutes"], parsed.get("quality_score", 0),
                                    parsed.get("quality_grade", "F"), parsed.get("cost_usd", 0.0),
                                    parsed.get("cost_source"), parsed.get("credits"), dedup_key,
                                ),
                            )
                            _measure_facade._store_adapter_activity(conn, dedup_key, parsed)
                            new_count += 1  # an upgrade is meaningful progress
                        except _measure_facade.sqlite3.Error as exc:
                            if not quiet:
                                print(f"[Token Optimizer] could not upgrade a Copilot session: {exc}")
                continue

            active = _measure_facade._adapter_activity_ts(parsed)
            if active and active.timestamp() < cutoff:
                continue
            # Timestamps are UTC; the local calendar day buckets like the JSONL
            # collectors (local mtime), so a late-evening session is not a day off.
            date = active.astimezone().strftime("%Y-%m-%d") if active else ""
            project_name = str(parsed.get("cwd") or "copilot")

            try:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO session_log
                       (jsonl_path, date, project, duration_minutes, input_tokens,
                        output_tokens, message_count, api_calls, cache_hit_rate,
                        cache_create_1h_tokens, cache_create_5m_tokens, cache_ttl_scanned,
                        avg_call_gap_seconds, max_call_gap_seconds, p95_call_gap_seconds,
                        skills_json, subagents_json, tool_calls_json, model_usage_json,
                        all_model_usage_json, model_usage_breakdown_json, version, slug, topic, collected_at,
                        quality_score, quality_grade, stale_waste_tokens,
                        cost_usd, cost_source, credits, platform, incomplete)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        dedup_key, date, project_name,
                        parsed["duration_minutes"],
                        parsed["total_input_tokens"],
                        parsed["total_output_tokens"],
                        parsed["message_count"],
                        parsed.get("api_calls", 0),
                        parsed["cache_hit_rate"],
                        parsed.get("total_cache_create_1h", 0),
                        parsed.get("total_cache_create_5m", 0),
                        1,
                        parsed.get("avg_call_gap_seconds"),
                        parsed.get("max_call_gap_seconds"),
                        parsed.get("p95_call_gap_seconds"),
                        _measure_facade.json.dumps(parsed.get("skills_used", {})),
                        _measure_facade.json.dumps(parsed.get("subagents_used", {})),
                        _measure_facade.json.dumps(parsed.get("tool_calls", {})),
                        _measure_facade.json.dumps(parsed.get("model_usage", {})),
                        # all_model_usage_json must stay {model: int} — the
                        # aggregate rebuilder sums it as model_daily totals.
                        _measure_facade.json.dumps(parsed.get("model_usage", {})),
                        _measure_facade.json.dumps(parsed.get("model_usage_breakdown", {})),
                        parsed.get("version"),
                        parsed.get("slug"),
                        parsed.get("topic"),
                        _measure_facade.datetime.now().isoformat(),
                        parsed.get("quality_score", 0),
                        parsed.get("quality_grade", "F"),
                        0,
                        # Copilot cost is platform-reported, not token-derived.
                        parsed.get("cost_usd", 0.0),
                        parsed.get("cost_source"),
                        parsed.get("credits"),
                        "copilot",
                        is_incomplete,
                    ),
                )
            except _measure_facade.sqlite3.Error as exc:
                # One malformed session must never abort the whole rollup — it
                # runs quiet from the stop hook, so a single bad row would
                # silently drop every other session in the batch.
                if not quiet:
                    print(f"[Token Optimizer] skipped a Copilot session: {exc}")
                continue
            if cur.rowcount != 1:
                continue
            _measure_facade._store_adapter_activity(conn, dedup_key, parsed)
            new_count += 1

        if new_count > 0:
            _measure_facade._rebuild_aggregate_tables(conn)
        conn.commit()
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
    finally:
        conn.close()

    _measure_facade._write_copilot_restore_context(normalized, quiet=quiet)

    if not quiet:
        print(f"[Token Optimizer] Collected {new_count} new Copilot sessions.")
    return new_count


def _write_cursor_restore_context(sessions, quiet=None):
    """Maintain per-workspace continuity files the sessionStart hook injects.

    Unlike Copilot (one terminal, one session), the Cursor IDE runs many
    concurrent chats across repos. A single global restore file would seed every
    new chat with whichever conversation rolled up last — unrelated context paid
    on a platform with no savings headline. So files are keyed by workspace root
    (``sha1(workspace_root)``) under ``restore-context/``; the bridge injects only
    the file matching its own workspace root.
    """
    _measure_facade = _compat.facade()
    try:
        import hashlib  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        from runtime_env import cursor_home as _ch  # noqa: PLC0415

        from datetime import datetime  # noqa: PLC0415
        # Match sessionStart's workspace root and source-session end time.
        by_ws: dict = {}
        for s in sessions:
            if s.get("incomplete"):
                continue  # never seed continuity from a crash/kill session
            wroots = s.get("workspace_roots")
            if isinstance(wroots, list) and wroots and isinstance(wroots[0], str) and wroots[0]:
                root = wroots[0]
            else:
                root = str(s.get("cwd") or s.get("slug") or "")
            if not root:
                continue
            by_ws.setdefault(root, []).append(s)

        def _clean(value):
            _measure_facade = _compat.facade()
            text = str(value or "")
            text = "".join(ch for ch in text if ch == " " or ch.isprintable())
            return text[:200]
        base = _ch() / "token-optimizer" / "restore-context"
        base.mkdir(parents=True, exist_ok=True)
        for root, group in by_ws.items():
            latest = max(group, key=lambda s: (s.get("last_ts") is not None, s.get("last_ts") or ""))
            try:
                end_ts = datetime.fromisoformat(str(latest.get("last_ts") or "").replace("Z", "+00:00")).timestamp()
            except (ValueError, OverflowError):
                continue
            lines = [f"<!-- source-end: {end_ts} -->", f"[Token Optimizer] Continuity from your previous Cursor session in {_clean(root)}:"]
            if latest.get("model"):
                lines.append(
                    f"- Model: {_clean(latest['model'])}; "
                    f"{latest.get('total_input_tokens', 0):,} in / "
                    f"{latest.get('total_output_tokens', 0):,} out tokens"
                )
            digest = hashlib.sha1(root.encode("utf-8", "replace")).hexdigest()
            path = base / f"{digest}.md"
            fd, tmp_name = tempfile.mkstemp(prefix=".restore-context.", suffix=".tmp", dir=str(base))
            try:
                with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
                _measure_facade.os.replace(tmp_name, str(path))
            except OSError:
                try:
                    _measure_facade.os.unlink(tmp_name)
                except OSError:
                    pass
                raise
    except Exception as exc:
        if not quiet:
            print(f"[Token Optimizer] Cursor restore-context update skipped: {exc}")


def _cursor_summary():
    """Token/quality session summary for Cursor (honest: no local cost)."""
    _measure_facade = _compat.facade()
    try:
        from runtime_env import cursor_home as _ch  # noqa: PLC0415
    except ImportError:
        _ch = None
    import cursor_state as _cst  # noqa: PLC0415

    home = _ch() if _ch is not None else _measure_facade.Path.home() / ".cursor"
    tallies = [t for p in _cst.find_tallies(home) if (t := _cst.read_tally(p)) is not None]
    normalized = [s for t in tallies if (s := _measure_facade.cursor_session.normalize_session(t))]

    print("Token Optimizer — Cursor summary")
    if not normalized:
        print()
        print("  No Cursor sessions found yet.")
        print("  Upgrade to a Cursor build with hook support, then run:")
        print("    bash install.sh --cursor")
        print("  and start a session. The hook tally appears after the first tool call.")
        return

    total_in = sum(s.get("total_input_tokens", 0) for s in normalized)
    total_out = sum(s.get("total_output_tokens", 0) for s in normalized)
    incomplete = sum(1 for s in normalized if s.get("incomplete"))
    estimated = sum(1 for s in normalized if s.get("estimated"))
    compactions = sum(s.get("compactions", 0) for s in normalized)
    surfaces = {}
    for s in normalized:
        sur = s.get("surface") or "unknown"
        surfaces[sur] = surfaces.get(sur, 0) + 1

    print(f"  {len(normalized)} session(s)")
    print(f"    Tokens: {total_in:,} in / {total_out:,} out")
    if compactions:
        print(f"    Compactions captured: {compactions}")
    if surfaces:
        print("    Surfaces: " + ", ".join(f"{k} ({v})" for k, v in sorted(surfaces.items())))
    if incomplete:
        print(f"    {incomplete} session(s) still active (hook tally only)")
    if estimated:
        print(
            f"    {estimated} session(s) use estimated token counts "
            "(state.vscdb tokenCount is best-effort; transcripts carry no usage)"
        )
    print()
    print("  Cost: Cursor stores no local billing data — no savings headline is")
    print("  rendered here by design (bash compression events are still counted).")
    print("  Full trends: measure.py cursor-rollup.")


def _collect_cursor_sessions(days=None, quiet=None, rebuild=None):
    """Collect Cursor sessions (hook tally + data planes) into the trends DB.

    Mirrors _collect_copilot_sessions. The hook tally is authoritative for
    calls/turns/compactions/model/cwd/timestamps; tokens come from state.vscdb
    (best-effort) or a chars-over-four transcript estimate. Idempotent via the
    jsonl_path dedup column (`cursor:<conversation_id>`), with in-place upgrade
    via _insert_normalized_session.
    """
    _measure_facade = _compat.facade()
    import cursor_state as _cst  # noqa: PLC0415

    try:
        from runtime_env import cursor_home as _ch  # noqa: PLC0415
    except ImportError:
        _ch = None

    try:
        conn = _measure_facade._init_trends_db()
    except _measure_facade.sqlite3.DatabaseError:
        if _measure_facade.TRENDS_DB.exists():
            try:
                stamp = int(_measure_facade.datetime.now().timestamp())
                _measure_facade.TRENDS_DB.rename(_measure_facade.TRENDS_DB.with_suffix(f".db.corrupt.{stamp}"))
            except OSError:
                pass
        conn = _measure_facade._init_trends_db()
    try:
        if rebuild:
            if not quiet:
                print("[Token Optimizer] Rebuilding Cursor trends DB...")
            conn.execute("PRAGMA user_version = 3")
            conn.execute("DELETE FROM session_log WHERE jsonl_path LIKE 'cursor:%'")
            conn.commit()

        home = _ch() if _ch is not None else _measure_facade.Path.home() / ".cursor"
        tallies = []
        for p in _cst.find_tallies(home):
            t = _cst.read_tally(p)
            if t is not None:
                tallies.append(_cst.idle_finalise(t))

        cutoff = _measure_facade.datetime.now().timestamp() - days * 86400
        composer_ids = [
            str(t.get("conversation_id") or t.get("session_id") or "") for t in tallies
        ]
        try:
            bubble_tokens = _cst.read_state_vscdb_tokens(composer_ids)
        except Exception:
            bubble_tokens = {}

        new_count = 0
        normalized = []
        for t in tallies:
            cid = str(t.get("conversation_id") or t.get("session_id") or "")
            if not cid:
                continue
            t = dict(t)
            t["bubble_tokens"] = bubble_tokens.get(cid)
            try:
                t["transcript_tokens"] = _cst.transcript_estimate(t.get("transcript_path"), home)
            except Exception:
                t["transcript_tokens"] = None
            parsed = _measure_facade.cursor_session.normalize_session(t)
            if not parsed:
                continue
            normalized.append(parsed)

            active = _measure_facade._adapter_activity_ts(parsed)
            if active and active.timestamp() < cutoff:
                continue

            dedup_key = f"cursor:{cid}"
            if _measure_facade._insert_normalized_session(conn, dedup_key, parsed, "cursor", "cursor", quiet=quiet):
                new_count += 1

        if new_count > 0:
            _measure_facade._rebuild_aggregate_tables(conn)
        conn.commit()
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
    finally:
        conn.close()

    _measure_facade._write_cursor_restore_context(normalized, quiet=quiet)

    if not quiet:
        print(f"[Token Optimizer] Collected {new_count} new Cursor sessions.")
    return new_count


def _antigravity_restore_clean(value) -> str:
    """R22 filter: printable-only, single-line, 200-char cap.

    Conversation titles and workspace paths are untrusted input injected into
    the next session's context, so newlines/control chars are stripped and the
    length is capped exactly like the Copilot continuity filter.
    """
    _measure_facade = _compat.facade()
    text = str(value or "")
    text = "".join(ch for ch in text if ch == " " or ch.isprintable())
    return " ".join(text.split())[:200]


def _write_antigravity_restore_context(sessions, quiet=None):
    """Maintain the continuity file the PreInvocation hook injects.

    Summarizes the most recent completed Antigravity session so a new session
    starts grounded. Capped well under the bridge's 16KB read limit.
    """
    _measure_facade = _compat.facade()
    try:
        from runtime_env import antigravity_home  # noqa: PLC0415

        complete = [s for s in sessions if not s.get("incomplete")]
        pool = complete or sessions
        if not pool:
            return
        latest = max(
            pool,
            key=lambda s: ((_measure_facade := _compat.facade()), (s.get("first_ts") is not None, s.get("first_ts") or ""))[1],
        )

        lines = ["[Token Optimizer] Continuity from your previous Antigravity session:"]
        if latest.get("topic"):
            lines.append(f"- Topic: {_measure_facade._antigravity_restore_clean(latest['topic'])}")
        if latest.get("cwd"):
            lines.append(f"- Working dir: {_measure_facade._antigravity_restore_clean(latest['cwd'])}")
        lines.append(
            f"- Model: {_measure_facade._antigravity_restore_clean(latest.get('model', 'unknown'))}; "
            f"{latest.get('total_input_tokens', 0):,} in / "
            f"{latest.get('total_output_tokens', 0):,} out tokens"
        )
        if latest.get("incomplete"):
            lines.append("- NOTE: that session ended without a clean shutdown (crash/kill).")

        path = antigravity_home() / "token-optimizer" / "restore-context.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        import tempfile  # noqa: PLC0415

        fd, tmp_name = tempfile.mkstemp(prefix=".restore-context.", suffix=".tmp", dir=str(path.parent))
        try:
            with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            _measure_facade.os.replace(tmp_name, str(path))
        except OSError:
            try:
                _measure_facade.os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception as exc:
        if not quiet:
            print(f"[Token Optimizer] restore-context update skipped: {exc}")


def _price_antigravity_session(session):
    """Price known model buckets separately; identify incomplete rate coverage."""
    _measure_facade = _compat.facade()
    if session.get("cost_source") != "antigravity_list_price_estimate":
        return
    breakdown = session.get("model_usage_breakdown") or {}
    session["cost_usd"] = sum(
        _measure_facade._get_model_cost(model, part.get("fresh_input", 0), part.get("output", 0),
                        cache_read=part.get("cache_read", 0), per_request=bool(parts.get("requests")))
        for model, parts in breakdown.items() for part in (parts.get("requests") or [parts])
    )
    if any(not _measure_facade._is_priced_model(model) for model in breakdown):
        session["cost_source"] = "antigravity_list_price_partial"


def _antigravity_summary():
    """Per-surface session summary for Antigravity plus decoder health."""
    _measure_facade = _compat.facade()
    import antigravity_state as _ags  # noqa: PLC0415

    print("Token Optimizer — Google Antigravity summary")
    raw_by_surface: dict[str, list] = {}
    undecodable = 0
    decodable = 0
    for raw in _ags.read_all_sessions():
        raw_by_surface.setdefault(str(raw.get("surface") or "unknown"), []).append(raw)
        undecodable += raw.get("undecodable_rows", 0)
        decodable += len(raw.get("generations") or [])

    any_data = False
    for surface in ("antigravity-cli", "antigravity", "antigravity-ide"):
        if surface not in raw_by_surface:
            continue
        normalized = [s for s in (_measure_facade.antigravity_session.normalize_session(r) for r in raw_by_surface[surface]) if s]
        if not normalized:
            continue
        any_data = True
        # Price list-price sessions at Gemini card rates so the "estimated cost"
        # line is a real dollar figure, not the normalizer's placeholder zero
        # (the normalizer never prices — KTD12; the collector does it).
        for s in normalized:
            _measure_facade._price_antigravity_session(s)
        total_in = sum(s.get("total_input_tokens", 0) for s in normalized)
        total_out = sum(s.get("total_output_tokens", 0) for s in normalized)
        total_cache = sum(s.get("total_cache_read", 0) for s in normalized)
        cost = sum(s.get("cost_usd") or 0.0 for s in normalized)
        credits = sum(s.get("credits") or 0.0 for s in normalized if s.get("credits"))
        incomplete = sum(1 for s in normalized if s.get("incomplete"))
        models: dict[str, int] = {}
        for s in normalized:
            for m, v in (s.get("model_usage") or {}).items():
                models[m] = models.get(m, 0) + v
        top_models = sorted(models.items(), key=lambda kv: ((_measure_facade := _compat.facade()), -kv[1])[1])[:3]

        print()
        print(f"  {surface}: {len(normalized)} session(s)")
        if credits:
            print(f"    Antigravity credits: {credits:,.2f}")
        elif cost > 0.0:
            partial = any(s.get("cost_source") == "antigravity_list_price_partial" for s in normalized)
            print(f"    Estimated cost: ~${cost:,.2f} (list prices" + (", known models only" if partial else "") + ")")
        else:
            print("    Cost: unavailable (no model with a known Gemini rate card)")
        print(f"    Tokens: {total_in:,} in / {total_out:,} out / {total_cache:,} cache-read")
        if top_models:
            print("    Models: " + ", ".join(f"{_measure_facade._strip_ansi(str(m))} ({v:,})" for m, v in top_models))
        if incomplete:
            print(f"    {incomplete} session(s) ended without clean shutdown (partial data)")
    if not any_data:
        print()
        print("  No Antigravity sessions found yet.")
    if decodable or undecodable:
        try:
            from antigravity_proto import DECODER_VERSION  # noqa: PLC0415
        except Exception:
            DECODER_VERSION = "ag-v1"
        print()
        print(f"  Decoder health: {decodable} decodable, {undecodable} undecodable "
              f"gen_metadata row(s) (decoder {DECODER_VERSION}).")
    print()
    print("  Full trends: measure.py antigravity-rollup (sessions land in trends.db).")


def _collect_antigravity_sessions(days=None, quiet=None, rebuild=None):
    """Collect Antigravity sessions into the trends DB, priced per R8.

    Mirrors _collect_copilot_sessions. The three surfaces are separate session
    populations with distinct dedup keys (``antigravity:<surface>:<conversation_id>``)
    — never merged, never summed. Idempotent via the jsonl_path dedup column.
    """
    _measure_facade = _compat.facade()
    import antigravity_state as _ags  # noqa: PLC0415

    # R20: no consent record -> no collection, no restore-context write.
    try:
        from runtime_env import antigravity_home  # noqa: PLC0415

        consent_path = antigravity_home() / "token-optimizer" / "config.json"
        consent = False
        if consent_path.is_file():
            cfg = _measure_facade.json.loads(consent_path.read_text(encoding="utf-8"))
            consent = bool(isinstance(cfg, dict) and cfg.get("antigravity_consent"))
    except (OSError, _measure_facade.json.JSONDecodeError, ValueError):
        consent = False
    if not consent:
        if not quiet:
            print("[Token Optimizer] Antigravity data collection is consent-gated; "
                  "run `bash install.sh --antigravity` to record consent.")
        return 0

    try:
        conn = _measure_facade._init_trends_db()
    except _measure_facade.sqlite3.DatabaseError:
        if _measure_facade.TRENDS_DB.exists():
            try:
                stamp = int(_measure_facade.datetime.now().timestamp())
                _measure_facade.TRENDS_DB.rename(_measure_facade.TRENDS_DB.with_suffix(f".db.corrupt.{stamp}"))
            except OSError:
                pass
        conn = _measure_facade._init_trends_db()
    try:
        if rebuild:
            if not quiet:
                print("[Token Optimizer] Rebuilding Antigravity trends rows...")
            conn.execute("PRAGMA user_version = 3")
            conn.execute("DELETE FROM session_log WHERE jsonl_path LIKE 'antigravity:%'")
            conn.commit()

        try:
            raw_sessions = list(_ags.read_all_sessions())
        except Exception as exc:
            if not quiet:
                print(f"[Token Optimizer] Antigravity scan failed: {exc}")
            raw_sessions = []

        cutoff = _measure_facade.datetime.now().timestamp() - days * 86400
        new_count = 0
        normalized = []
        for raw in raw_sessions:
            parsed = _measure_facade.antigravity_session.normalize_session(raw)
            if not parsed:
                continue
            normalized.append(parsed)

            # R8 pricing: only the list-price-estimate path is denominated in
            # USD; credits are Antigravity's own figure and stay USD-less.
            _measure_facade._price_antigravity_session(parsed)

            dedup_key = parsed.get("dedup_key")
            if not dedup_key:
                continue
            is_incomplete = 1 if parsed.get("incomplete") else 0
            if _measure_facade._is_file_collected(conn, dedup_key):
                new_count += _measure_facade._store_adapter_activity(conn, dedup_key, parsed)
                if not is_incomplete and parsed.get("generation_details_complete"):
                    model_usage = _measure_facade.json.dumps(parsed.get("model_usage", {}))
                    breakdown = _measure_facade.json.dumps(parsed.get("model_usage_breakdown", {}))
                    cost = parsed.get("cost_usd", 0.0)
                    source = parsed.get("cost_source")
                    cur = conn.execute(
                        """UPDATE session_log SET model_usage_json=?, all_model_usage_json=?,
                                  model_usage_breakdown_json=?, cost_usd=?, cost_source=?
                           WHERE jsonl_path=? AND (incomplete=1 OR (input_tokens=? AND output_tokens=?))
                             AND (model_usage_json IS NOT ? OR
                                 all_model_usage_json IS NOT ? OR model_usage_breakdown_json IS NOT ? OR
                                 cost_usd IS NOT ? OR cost_source IS NOT ?)""",
                        (model_usage, model_usage, breakdown, cost, source, dedup_key,
                         parsed["total_input_tokens"], parsed["total_output_tokens"],
                         model_usage, model_usage, breakdown, cost, source),
                    )
                    new_count += cur.rowcount
                if not is_incomplete:
                    try:
                        existing = conn.execute(
                            "SELECT incomplete FROM session_log WHERE jsonl_path = ?",
                            (dedup_key,),
                        ).fetchone()
                    except _measure_facade.sqlite3.Error:
                        existing = None
                    if existing is not None and existing[0]:
                        try:
                            conn.execute(
                                """UPDATE session_log SET
                                     input_tokens=?, output_tokens=?, message_count=?,
                                     api_calls=?, cache_hit_rate=?, cache_create_1h_tokens=?,
                                     duration_minutes=?, quality_score=?, quality_grade=?,
                                     cost_usd=?, cost_source=?, credits=?, incomplete=0
                                   WHERE jsonl_path=? AND incomplete=1""",
                                (
                                    parsed["total_input_tokens"], parsed["total_output_tokens"],
                                    parsed["message_count"], parsed.get("api_calls", 0),
                                    parsed["cache_hit_rate"], parsed.get("total_cache_create_1h", 0),
                                    parsed["duration_minutes"], parsed.get("quality_score", 0),
                                    parsed.get("quality_grade", "F"), parsed.get("cost_usd", 0.0),
                                    parsed.get("cost_source"), parsed.get("credits"), dedup_key,
                                ),
                            )
                            _measure_facade._store_adapter_activity(conn, dedup_key, parsed)
                            new_count += 1
                        except _measure_facade.sqlite3.Error as exc:
                            if not quiet:
                                print(f"[Token Optimizer] could not upgrade an Antigravity session: {exc}")
                continue

            active = _measure_facade._adapter_activity_ts(parsed)
            if active and active.timestamp() < cutoff:
                continue
            date = active.astimezone().strftime("%Y-%m-%d") if active else ""
            project_name = str(parsed.get("cwd") or "antigravity")

            try:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO session_log
                       (jsonl_path, date, project, duration_minutes, input_tokens,
                        output_tokens, message_count, api_calls, cache_hit_rate,
                        cache_create_1h_tokens, cache_create_5m_tokens, cache_ttl_scanned,
                        avg_call_gap_seconds, max_call_gap_seconds, p95_call_gap_seconds,
                        skills_json, subagents_json, tool_calls_json, model_usage_json,
                        all_model_usage_json, model_usage_breakdown_json, version, slug, topic, collected_at,
                        quality_score, quality_grade, stale_waste_tokens,
                        cost_usd, cost_source, credits, platform, incomplete)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        dedup_key, date, project_name,
                        parsed["duration_minutes"],
                        parsed["total_input_tokens"],
                        parsed["total_output_tokens"],
                        parsed["message_count"],
                        parsed.get("api_calls", 0),
                        parsed["cache_hit_rate"],
                        parsed.get("total_cache_create_1h", 0),
                        parsed.get("total_cache_create_5m", 0),
                        1,
                        parsed.get("avg_call_gap_seconds"),
                        parsed.get("max_call_gap_seconds"),
                        parsed.get("p95_call_gap_seconds"),
                        _measure_facade.json.dumps(parsed.get("skills_used", {})),
                        _measure_facade.json.dumps(parsed.get("subagents_used", {})),
                        _measure_facade.json.dumps(parsed.get("tool_calls", {})),
                        _measure_facade.json.dumps(parsed.get("model_usage", {})),
                        _measure_facade.json.dumps(parsed.get("model_usage", {})),
                        _measure_facade.json.dumps(parsed.get("model_usage_breakdown", {})),
                        parsed.get("version"),
                        parsed.get("slug"),
                        parsed.get("topic"),
                        _measure_facade.datetime.now().isoformat(),
                        parsed.get("quality_score", 0),
                        parsed.get("quality_grade", "F"),
                        0,
                        parsed.get("cost_usd", 0.0),
                        parsed.get("cost_source"),
                        parsed.get("credits"),
                        "antigravity",
                        is_incomplete,
                    ),
                )
            except _measure_facade.sqlite3.Error as exc:
                if not quiet:
                    print(f"[Token Optimizer] skipped an Antigravity session: {exc}")
                continue
            if cur.rowcount != 1:
                continue
            _measure_facade._store_adapter_activity(conn, dedup_key, parsed)
            new_count += 1

        if new_count > 0:
            _measure_facade._rebuild_aggregate_tables(conn)
        conn.commit()
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
    finally:
        conn.close()

    _measure_facade._write_antigravity_restore_context(normalized, quiet=quiet)

    if not quiet:
        print(f"[Token Optimizer] Collected {new_count} new Antigravity sessions.")
    return new_count


def _merge_daily_usage(into, other):
    """Add one daily_usage map ({date: {model: buckets}}) into another in place."""
    _measure_facade = _compat.facade()
    for day, models in (other or {}).items():
        if not isinstance(models, dict):
            continue
        dst_day = into.setdefault(day, {})
        for model, parts in models.items():
            if not isinstance(parts, dict):
                continue
            dst = dst_day.setdefault(model, {})
            for key, val in parts.items():
                dst[key] = int(dst.get(key) or 0) + int(val or 0)
    return into


def _session_merged_requests(filepath, parsed, subagent_files):
    """Every API request of a session across its parent and subagent files and
    every other copy of it, merged by requestId (largest usage wins,
    streaming-safe). A request Claude Code wrote into two files counts once,
    identical copies add nothing, and copies that diverged (each holding
    requests the other lacks) lose nothing. None for adapter output that has
    no per-request detail."""
    _measure_facade = _compat.facade()
    per_file = [(str(filepath), parsed)]
    per_file += [(str(copy), _measure_facade._parse_session_jsonl(copy)) for copy in _measure_facade._session_copy_files(filepath)]
    per_file += [(str(sub_jf), _measure_facade._parse_session_jsonl(sub_jf)) for sub_jf in subagent_files]
    if not all(p is None or "request_usage" in p for _, p in per_file):
        return None
    merged = {}
    for path, p in per_file:
        for key, u in ((p or {}).get("request_usage") or {}).items():
            if str(key).startswith("__noreq__"):
                # No requestId: keyed by file name and position, so the same
                # record in two copies of one file still counts once.
                key = f"{_measure_facade.Path(path).name}:{key}"
            prev = merged.get(key)
            if prev is None:
                merged[key] = dict(u)
                continue
            for field in ("inp", "out", "cr", "cc", "cc_1h", "cc_5m"):
                prev[field] = max(int(prev.get(field) or 0), int(u.get(field) or 0))
            # Same request seen twice: attributes must not depend on file order.
            # A real model beats "unknown"; the earliest dated day wins.
            days = [d for d in (prev.get("day"), u.get("day")) if d]
            prev["day"] = min(days) if days else None
            models = sorted(m for m in (prev.get("model"), u.get("model")) if m and m != "unknown")
            prev["model"] = models[0] if models else (prev.get("model") or u.get("model"))
    return merged


def _stable_hash63(text):
    """Stable 63-bit integer for a string (fits a signed SQLite INTEGER)."""
    _measure_facade = _compat.facade()
    digest = _measure_facade.hashlib.blake2b(str(text).encode("utf-8", "replace"), digest_size=8).digest()
    return int.from_bytes(digest, "big") >> 1


def _request_base_key(key):
    """The identity under which a request's usage is shared between sessions, or
    None when it cannot be shared. Records with neither a request id nor a
    message id are keyed by file position, so they never match another file.
    An advisor part ("<request>#advisorN") is its own entry with its own usage."""
    _measure_facade = _compat.facade()
    key = str(key)
    if key.startswith("__noreq__") or ":__noreq__" in key:
        return None
    return key


def _usage_owner_id(filepath):
    """Identity under which a session claims its requests: the session UUID, so
    every copy of one session is one owner, else the transcript path."""
    _measure_facade = _compat.facade()
    return _measure_facade._canonical_session_uuid(filepath) or str(filepath)


def _live_usage_owners(conn):
    """Owner hashes of every session that currently has a row (or a tracked copy)."""
    _measure_facade = _compat.facade()
    live = set()
    for table in ("session_log", "session_log_aliases"):
        try:
            for uuid, path in conn.execute(f"SELECT session_uuid, jsonl_path FROM {table}"):
                if uuid:
                    live.add(_measure_facade._stable_hash63(uuid))
                if path:
                    live.add(_measure_facade._stable_hash63(path))
        except _measure_facade.sqlite3.Error:
            continue
    return live


def _empty_share():
    _measure_facade = _compat.facade()
    share = dict.fromkeys(_measure_facade._SHARE_FIELDS, 0)
    share["n"] = 0
    return share


def _claim_state(conn, owner_id, keys, live_owners):
    """What the stored sessions have counted of the requests in `keys`.

    A request Claude Code copied into a resumed or forked session's transcript
    is the same API call, and a streamed reply can be longer in one copy than
    in another. Sessions therefore share a request's largest usage between
    them (see `_net_of_counted`). Returns (others, own): the summed shares of
    the other sessions, and this session's own earlier share. A share held by a
    session that no longer has a row is ignored, so usage is never dropped on
    the strength of a claim nobody holds.
    """
    _measure_facade = _compat.facade()
    me = _measure_facade._stable_hash63(owner_id)
    by_hash = {}
    for key in keys:
        base = _measure_facade._request_base_key(key)
        if base is not None:
            by_hash[_measure_facade._stable_hash63(base)] = base
    others, own = {}, {}
    hashes = list(by_hash)
    for i in range(0, len(hashes), 500):
        batch = hashes[i:i + 500]
        try:
            rows = conn.execute(
                "SELECT request_hash, owner_hash, calls, inp, out, cr, cc, cc_1h, cc_5m, model, day "
                "FROM usage_request_claims WHERE request_hash IN (%s)" % ",".join("?" * len(batch)),
                batch).fetchall()
        except _measure_facade.sqlite3.Error:
            return {}, {}
        for request_hash, owner_hash, calls, *fields in rows:
            if owner_hash == me:
                target = own
            elif owner_hash in live_owners:
                target = others
            else:
                continue
            share = target.setdefault(by_hash[request_hash], _measure_facade._empty_share())
            share["n"] += int(calls or 0)
            for name, value in zip(_measure_facade._SHARE_FIELDS, fields[:len(_measure_facade._SHARE_FIELDS)]):
                share[name] += int(value or 0)
            if target is own:
                share["model"], share["day"] = fields[len(_measure_facade._SHARE_FIELDS)], fields[len(_measure_facade._SHARE_FIELDS) + 1]
    return others, own


def _net_of_counted(requests, counted, own=None):
    """The part of each request this session adds on top of what is already
    counted: per field, its own figure minus the others' total, never below 0.
    A share this session recorded earlier is kept, so collecting it again
    cannot shrink it, whether the transcript is collected for an unrelated
    reason or has lost part of the reply. The shares of every session sum to
    the largest figure any copy holds, whichever session is collected first or
    collected again later. An entry the others already cover completely comes
    back empty (`n` 0, all fields 0)."""
    _measure_facade = _compat.facade()
    own = own or {}
    net = {}
    for key, u in requests.items():
        base = _measure_facade._request_base_key(key)
        share = counted.get(base) if base is not None else None
        prior = own.get(base) if base is not None else None
        entry = dict(u)
        if share or prior:
            prior = prior or {}
            for name in _measure_facade._SHARE_FIELDS:
                raw = int(u.get(name) or 0)
                entry[name] = max(int(prior.get(name) or 0), raw - (share[name] if share else 0), 0)
            entry["n"] = max(int(prior.get("n") or 0), 0 if (share and share["n"] >= 1) else 1)
            if prior:
                # What was recorded about the request (its day and model) stands;
                # a later observation only fills what was missing.
                entry["day"] = prior.get("day") or u.get("day")
                recorded = prior.get("model")
                entry["model"] = (recorded if recorded and recorded != "unknown"
                                  and not str(recorded).startswith("<")
                                  else (u.get("model") or recorded))
        else:
            entry["n"] = 1
        net[key] = entry
    return net


def _retained_requests(conn, owner_id, keys):
    """The session's recorded shares of requests its transcripts no longer hold.

    Transcripts rotate and copies get removed. What a session was already
    credited for stays in its totals; the requests still in the files are
    counted next to it. Shares come back as request entries (keyed so they are
    never treated as shareable) carrying the model and day they were billed to.
    """
    _measure_facade = _compat.facade()
    present = {_measure_facade._stable_hash63(b) for b in (_measure_facade._request_base_key(k) for k in keys) if b is not None}
    try:
        rows = conn.execute(
            "SELECT request_hash, calls, inp, out, cr, cc, cc_1h, cc_5m, model, day "
            "FROM usage_request_claims WHERE owner_hash = ?", (_measure_facade._stable_hash63(owner_id),)).fetchall()
    except _measure_facade.sqlite3.Error:
        return {}
    kept = {}
    for request_hash, calls, *fields, model, day in rows:
        if request_hash in present or not (calls or any(fields)):
            continue
        entry = dict(zip(_measure_facade._SHARE_FIELDS, (int(v or 0) for v in fields)))
        entry.update(n=int(calls or 0), model=model or "unknown", day=day)
        kept[f"__noreq__kept:{request_hash}"] = entry
    return kept


def _claim_requests(conn, owner_id, net_requests):
    """Record this session's share of each request (as returned by `_net_of_counted`)."""
    _measure_facade = _compat.facade()
    me = _measure_facade._stable_hash63(owner_id)
    rows = {}
    for key, u in net_requests.items():
        base = _measure_facade._request_base_key(key)
        if base is None:
            continue
        calls = 0 if u.get("advisor") else int(u.get("n", 1) or 0)
        rows[_measure_facade._stable_hash63(base)] = (
            _measure_facade._stable_hash63(base), me, calls,
            *(int(u.get(name) or 0) for name in _measure_facade._SHARE_FIELDS), u.get("model"), u.get("day"))
    if not rows:
        return
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO usage_request_claims "
            "(request_hash, owner_hash, calls, inp, out, cr, cc, cc_1h, cc_5m, model, day) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", list(rows.values()))
    except _measure_facade.sqlite3.Error:
        pass


def _net_and_record(requests, ledger):
    """In-memory form of the claims for a pass over several transcripts: returns
    the net usage of `requests` against `ledger`, plus the shares counted before
    this call (empty when nothing overlapped), and adds the net to the ledger."""
    _measure_facade = _compat.facade()
    counted = {}
    for key in requests:
        base = _measure_facade._request_base_key(key)
        if base is not None and base in ledger:
            counted[base] = dict(ledger[base])
    net = _measure_facade._net_of_counted(requests, counted)
    for key, u in net.items():
        base = _measure_facade._request_base_key(key)
        if base is None:
            continue
        share = ledger.setdefault(base, _measure_facade._empty_share())
        share["n"] += int(u.get("n", 1) or 0)
        for name in _measure_facade._SHARE_FIELDS:
            share[name] += int(u.get(name) or 0)
    return net, counted


def _parsed_with_requests(parsed, requests):
    """A copy of a parsed transcript restated from the given per-request usage,
    every usage-derived figure recomputed. `parsed` itself is never modified."""
    _measure_facade = _compat.facade()
    agg, _days = _measure_facade._aggregate_request_usage(requests)
    full_input = agg["total_input"] + agg["total_cache_read"] + agg["total_cache_create"]
    out = dict(parsed)
    out.update({
        "total_input_tokens": full_input,
        "total_output_tokens": agg["total_output"],
        "total_cache_read": agg["total_cache_read"],
        "total_cache_create": agg["total_cache_create"],
        "total_cache_create_1h": agg["total_cache_create_1h"],
        "total_cache_create_5m": agg["total_cache_create_5m"],
        "cache_hit_rate": (agg["total_cache_read"] / full_input) if full_input > 0 else 0.0,
        "model_usage": agg["model_usage"],
        "model_usage_breakdown": agg["model_usage_breakdown"],
        "daily_usage": _measure_facade._daily_usage_from_merged(requests),
        "api_calls": agg["api_calls"],
        "request_usage": requests,
    })
    return out


def _parsed_net_of_counted(parsed, counted, own=None):
    """`parsed` restated without what other sessions already counted of its requests."""
    _measure_facade = _compat.facade()
    requests = (parsed or {}).get("request_usage")
    if not requests or not (counted or own):
        return parsed
    return _measure_facade._parsed_with_requests(parsed, _measure_facade._net_of_counted(requests, counted, own))


def _parsed_net_of_ledger(parsed, ledger):
    """`parsed` net of the requests earlier transcripts in a multi-file pass
    already counted; this file's requests are then added to `ledger`. Files
    without per-request detail pass through. The pass-wide total is the largest
    usage of each request, whatever the order of the files."""
    _measure_facade = _compat.facade()
    requests = (parsed or {}).get("request_usage")
    if not requests:
        return parsed
    net, counted = _measure_facade._net_and_record(requests, ledger)
    if not counted:
        return parsed
    return _measure_facade._parsed_with_requests(parsed, net)


def _stale_usage_row_paths(conn):
    """Transcript paths of stored rows written before the current usage
    attribution. Collect re-parses them from their transcript, so a row that
    over-counted repeated requests corrects itself; a row whose transcript is
    gone is never visited and keeps its stored numbers."""
    _measure_facade = _compat.facade()
    try:
        return {str(r[0]) for r in conn.execute(
            "SELECT jsonl_path FROM session_log WHERE jsonl_path IS NOT NULL "
            "AND (usage_parser_version IS NULL OR usage_parser_version < ?)",
            (_measure_facade._USAGE_PARSER_VERSION,))}
    except _measure_facade.sqlite3.Error:
        return set()


def _stamp_usage_parser_version(conn, filepath):
    """Mark a row as current when a re-parse has nothing to change, so it is not
    re-parsed on every collect."""
    _measure_facade = _compat.facade()
    try:
        conn.execute(
            "UPDATE session_log SET usage_parser_version = ? WHERE jsonl_path = ?",
            (_measure_facade._USAGE_PARSER_VERSION, str(filepath)))
    except _measure_facade.sqlite3.Error:
        pass


def _daily_usage_from_merged(merged):
    """Per-day, per-model token buckets of a merged request set."""
    _measure_facade = _compat.facade()
    combined = {}
    for u in merged.values():
        if _measure_facade._net_request_is_empty(u):
            continue
        # Keep unknown activity visible for the coverage disclosure.
        day = u.get("day") or _measure_facade.UNDATED_USAGE_KEY
        bucket = combined.setdefault(day, {}).setdefault(
            u.get("model") or "unknown", _measure_facade._empty_model_buckets())
        bucket["fresh_input"] += int(u.get("inp") or 0)
        bucket["cache_read"] += int(u.get("cr") or 0)
        bucket["cache_create"] += int(u.get("cc") or 0)
        bucket["cache_create_1h"] += int(u.get("cc_1h") or 0)
        bucket["cache_create_5m"] += int(u.get("cc_5m") or 0)
        bucket["output"] += int(u.get("out") or 0)
    return combined


def _session_daily_metrics(parsed, subagent_files):
    _measure_facade = _compat.facade()
    metrics = _measure_facade.copy.deepcopy(parsed.get("daily_metrics") or {})
    for path in subagent_files:
        child = _measure_facade._parse_session_jsonl(path) or {}
        for day, values in (child.get("daily_metrics") or {}).items():
            target = metrics.setdefault(day, {"message_count": 0, "duration_minutes": 0,
                                               "skills": {}, "subagents": {}, "tool_calls": {}})
            for name in ("skills", "subagents"):
                for key, count in values.get(name, {}).items():
                    target.setdefault(name, {})[key] = target.get(name, {}).get(key, 0) + count
    return metrics


def _session_daily_usage(filepath, parsed=None, subagent_files=None, conn=None, live_owners=None):
    """Daily usage for a session transcript plus all of its subagent transcripts.

    Claude Code sometimes writes the same API request into more than one of a
    session's files (the parent and a subagent, or two subagents). Requests are
    therefore merged by request id across every file first, keeping the largest
    usage seen (streaming-safe), so a shared request is billed once. With a
    database connection, what another stored session already counts of a request
    is left out as well.
    """
    _measure_facade = _compat.facade()
    parsed = parsed if parsed is not None else _measure_facade._parse_session_jsonl(filepath)
    if subagent_files is None:
        subagent_files = _measure_facade._session_subagent_files(filepath)
    merged = _measure_facade._session_merged_requests(filepath, parsed, subagent_files)
    if merged is None:
        # Adapter output without per-request detail: sum the per-file buckets.
        combined = {}
        for p in [parsed] + [_measure_facade._parse_session_jsonl(sub_jf) for sub_jf in subagent_files]:
            _measure_facade._merge_daily_usage(combined, (p or {}).get("daily_usage") or {})
        return combined
    if conn is not None:
        if live_owners is None:
            live_owners = _measure_facade._live_usage_owners(conn)
        owner_id = _measure_facade._usage_owner_id(filepath)
        counted, own = _measure_facade._claim_state(conn, owner_id, merged, live_owners)
        if counted or own:
            merged = _measure_facade._net_of_counted(merged, counted, own)
        if _measure_facade._stable_hash63(owner_id) in live_owners:
            merged = {**merged, **_measure_facade._retained_requests(conn, owner_id, merged)}
    return _measure_facade._daily_usage_from_merged(merged)


def _session_activity_evidence(filepath, parsed=None, subagent_files=None, conn=None, live_owners=None):
    """Every dated per-day field collect would store for one transcript.

    Returns {"daily_usage", "parent_usage", "calls", "metrics", "bounds"} or
    None when the transcript cannot be parsed. `daily_usage` is the combined
    parent+delegated map deduplicated by request id; `parent_usage` is the
    parent file's own buckets after the same claim netting collect applies, so
    a backfilled row carries the same provenance a fresh collect would write.
    `bounds` is the sorted union of combined, parent and metadata days, matching
    the collector's first/last activity derivation.
    """
    _measure_facade = _compat.facade()
    parsed = parsed if parsed is not None else _measure_facade._parse_session_jsonl(filepath)
    if parsed is None:
        return None
    if subagent_files is None:
        subagent_files = _measure_facade._session_subagent_files(filepath)
    metrics = _measure_facade._session_daily_metrics(parsed, subagent_files)
    merged = _measure_facade._session_merged_requests(filepath, parsed, subagent_files)
    parent_parsed = parsed
    if merged is None:
        # Adapter output without per-request detail: sum the per-file buckets.
        daily_usage = {}
        for p in [parsed] + [_measure_facade._parse_session_jsonl(sub_jf) for sub_jf in subagent_files]:
            _measure_facade._merge_daily_usage(daily_usage, (p or {}).get("daily_usage") or {})
        call_requests = parsed.get("request_usage") or {}
    else:
        net_merged = merged
        call_requests = merged
        if conn is not None:
            if live_owners is None:
                live_owners = _measure_facade._live_usage_owners(conn)
            owner_id = _measure_facade._usage_owner_id(filepath)
            counted, own = _measure_facade._claim_state(conn, owner_id, merged, live_owners)
            if counted or own:
                net_merged = _measure_facade._net_of_counted(merged, counted, own)
                parent_parsed = _measure_facade._parsed_net_of_counted(parsed, counted, own)
            if _measure_facade._stable_hash63(owner_id) in live_owners:
                retained = _measure_facade._retained_requests(conn, owner_id, merged)
                if retained:
                    net_merged = {**net_merged, **retained}
                    parent_parsed = _measure_facade._parsed_with_requests(
                        parent_parsed,
                        {**(parent_parsed.get("request_usage") or {}), **retained})
        daily_usage = _measure_facade._daily_usage_from_merged(net_merged)
        call_requests = net_merged
    calls = dict(parent_parsed.get("daily_calls") or {})
    for u in call_requests.values():
        if not u.get("advisor") and u.get("n", 1) and u.get("day"):
            day = u["day"]
            calls[day] = calls.get(day, 0) + 1
    bounds = _measure_facade._activity_bounds(daily_usage, parent_parsed.get("daily_usage"), metrics)
    return {"daily_usage": daily_usage,
            "parent_usage": parent_parsed.get("daily_usage") or {},
            "calls": calls, "metrics": metrics, "bounds": bounds}


def _session_rank(message_count, input_tokens, output_tokens):
    """How complete a copy of a session is. Copies are prefixes of the live file,
    so the larger one is the superset. Shared by both dedupe paths so they always
    pick the same winner."""
    _measure_facade = _compat.facade()
    return (int(message_count or 0), int(input_tokens or 0), int(output_tokens or 0))


def _session_dir_index(projects_dir):
    """Map session stem -> project dirs holding a copy of that session (its
    transcript, its folder of subagents, or both).

    One directory listing per project, cached for a minute, so looking up a
    session's copies elsewhere costs a dict hit instead of a stat of every
    project dir for every session (1.6M stats on a large history).
    """
    _measure_facade = _compat.facade()
    key = str(projects_dir)
    now = _measure_facade.time.monotonic()
    cached = _measure_facade._SESSION_DIR_INDEX.get(key)
    if cached and now - cached[0] < _measure_facade._SESSION_DIR_INDEX_TTL:
        return cached[1]
    index = {}
    try:
        projects = [e for e in _measure_facade.os.scandir(projects_dir) if e.is_dir()]
    except OSError:
        projects = []
    for proj in projects:
        try:
            with _measure_facade.os.scandir(proj.path) as entries:
                found = set()
                for e in entries:
                    name = e.name[:-6] if e.name.endswith(".jsonl") else e.name
                    if name not in found and _measure_facade._canonical_session_uuid(name):
                        found.add(name)
                        index.setdefault(name, []).append(_measure_facade.Path(proj.path))
        except OSError:
            continue
    _measure_facade._SESSION_DIR_INDEX.clear()
    _measure_facade._SESSION_DIR_INDEX[key] = (now, index)
    return index


def _session_copy_files(filepath):
    """Other transcripts of the same session under sibling project dirs
    (worktree dir, sandboxed-config mirror, synced folder)."""
    _measure_facade = _compat.facade()
    filepath = _measure_facade.Path(filepath)
    projects_dir = filepath.parent.parent
    if not (_measure_facade._canonical_session_uuid(filepath) and projects_dir.name == "projects"):
        return []
    copies = []
    for sib in _measure_facade._session_dir_index(projects_dir).get(filepath.stem, ()):
        copy_path = sib / filepath.name
        if sib != filepath.parent and copy_path.is_file():
            copies.append(copy_path)
    return copies


def _session_subagent_files(filepath):
    """Subagent transcripts for a session across every copy of it.

    A session resumed in a git worktree keeps writing subagents under that
    worktree's project dir, so after deduping to one row the winner must still
    see the other copies' subagents. Requests are merged by requestId
    downstream, so a subagent file present in two copies is never billed twice.
    """
    _measure_facade = _compat.facade()
    filepath = _measure_facade.Path(filepath)
    files = list(_measure_facade._find_subagent_jsonl_files(filepath))
    seen = {f.name for f in files}
    projects_dir = filepath.parent.parent
    if _measure_facade._canonical_session_uuid(filepath) and projects_dir.name == "projects":
        siblings = [d for d in _measure_facade._session_dir_index(projects_dir).get(filepath.stem, ()) if d != filepath.parent]
        for sib in siblings:
            sub_dir = sib / filepath.stem / "subagents"
            if not sub_dir.is_dir():
                continue
            for jf in sub_dir.glob("*.jsonl"):
                try:
                    if jf.name not in seen and jf.stat().st_size > 0:
                        files.append(jf)
                        seen.add(jf.name)
                except OSError:
                    continue
    return files


def _canonical_session_uuid(filepath):
    """Session UUID for a transcript path, or None when the stem is not one."""
    _measure_facade = _compat.facade()
    try:
        return _measure_facade._extract_session_uuid(_measure_facade.Path(filepath).stem)[0]
    except Exception:
        return None


def _dedupe_session_rows(conn):
    """Collapse session_log rows that are copies of the same session (#200).

    session_log is keyed by jsonl_path, but one session can sit under several
    paths: a git-worktree project dir, a mirror under a sandboxed
    CLAUDE_CONFIG_DIR, a resumed copy. Each copy used to count as its own
    session, inflating every total 2-3x. Keep the most complete row per
    session_uuid (a copy is a prefix of the live file, so the larger one is the
    superset). Rows are derived from transcripts on disk, so nothing is lost.
    """
    _measure_facade = _compat.facade()
    try:
        rows = conn.execute(
            """SELECT id, session_uuid, message_count, input_tokens, output_tokens, jsonl_path, collected_at
               FROM session_log
               WHERE session_uuid IN (
                   SELECT session_uuid FROM session_log
                   WHERE session_uuid IS NOT NULL AND session_uuid != ''
                   GROUP BY session_uuid HAVING COUNT(*) > 1)"""
        ).fetchall()
    except _measure_facade.sqlite3.Error:
        return 0
    best = {}
    for rid, uuid, msgs, inp, out, path, _collected in rows:
        if _measure_facade._extract_session_uuid(uuid or "")[0] is None:
            continue
        on_disk = bool(path) and _measure_facade.os.path.exists(path)
        # Most complete copy wins; on a tie, the one whose transcript still
        # exists (its per-day data can be rebuilt), then the newest row.
        rank = (*_measure_facade._session_rank(msgs, inp, out), on_disk, int(rid))
        if uuid not in best or rank > best[uuid][0]:
            best[uuid] = (rank, rid, on_disk)
    losers = [
        (rid,) for rid, uuid, *_ in rows
        if uuid in best and rid != best[uuid][1]
    ]
    if not losers:
        return 0
    # Recompute the winner's per-day data so it includes the losers' subagents
    # (_session_subagent_files) -- only when its transcript can be re-read.
    winners = [(b[1],) for b in best.values() if b[2]]
    loser_ids = {rid for (rid,) in losers}
    aliases = [(path, uuid, collected) for rid, uuid, _m, _i, _o, path, collected in rows if rid in loser_ids]
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO session_log_aliases (jsonl_path, session_uuid, collected_at) VALUES (?, ?, ?)",
            aliases)
        conn.executemany("DELETE FROM session_log WHERE id = ?", losers)
        conn.executemany("UPDATE session_log SET daily_usage_json = NULL WHERE id = ?", winners)
    except _measure_facade.sqlite3.Error:
        return 0
    return len(losers)


def _collect_drain_mode():
    """"full" inside the detached upgrade drain, "inline" for a user-run
    collect, None on the hook path (and under pytest, where a test opts in by
    patching this function)."""
    _measure_facade = _compat.facade()
    if _measure_facade.os.environ.get("TOKEN_OPTIMIZER_UPGRADE_DRAIN", "").strip() == "1":
        return "full"
    if _measure_facade.os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    try:
        return None if _measure_facade._running_under_hook() else "inline"
    except Exception:
        return None


def _upgrade_drain_key():
    """One drain per counted-pricing version and usage-parser version."""
    _measure_facade = _compat.facade()
    return f"counted{_measure_facade._COUNTED_PRICING_VERSION}-usage{_measure_facade._USAGE_PARSER_VERSION}"


def _upgrade_meta_get(conn, key):
    _measure_facade = _compat.facade()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS token_optimizer_meta "
                     "(key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute("SELECT value FROM token_optimizer_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    except _measure_facade.sqlite3.Error:
        return None


def _upgrade_meta_set(conn, key, value):
    _measure_facade = _compat.facade()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS token_optimizer_meta "
                     "(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT OR REPLACE INTO token_optimizer_meta (key, value) VALUES (?, ?)",
                     (key, str(value)))
        conn.commit()
    except _measure_facade.sqlite3.Error:
        pass


def _upgrade_drain_mark_done(conn):
    _measure_facade = _compat.facade()
    _measure_facade._upgrade_meta_set(conn, _measure_facade._UPGRADE_DRAIN_META, _measure_facade._upgrade_drain_key())


def _upgrade_drain_after_pass(conn, backlog):
    """After a bounded collect pass: once per version, hand any re-scoring
    backlog to a detached drain. A pass with no backlog records the version as
    drained. Returns True when a drain child was started. Never raises."""
    _measure_facade = _compat.facade()
    try:
        key = _measure_facade._upgrade_drain_key()
        if _measure_facade._upgrade_meta_get(conn, _measure_facade._UPGRADE_DRAIN_META) == key:
            return False
        if not backlog:
            _measure_facade._upgrade_drain_mark_done(conn)
            return False
        # Under pytest the child would run against the real ~/.claude.
        if _measure_facade.os.environ.get("PYTEST_CURRENT_TEST") and not _measure_facade.os.environ.get(
                "TOKEN_OPTIMIZER_TEST_ALLOW_DRAIN_SPAWN"):
            return False
        raw = _measure_facade._upgrade_meta_get(conn, _measure_facade._UPGRADE_DRAIN_ATTEMPTS_META) or ""
        attempts = int(raw.split(":", 1)[1]) if raw.startswith(key + ":") else 0
        if attempts >= _measure_facade._UPGRADE_DRAIN_MAX_ATTEMPTS:
            return False
        if not _measure_facade._upgrade_drain_claim_lock():
            return False
        if _measure_facade._spawn_upgrade_drain():
            _measure_facade._upgrade_meta_set(conn, _measure_facade._UPGRADE_DRAIN_ATTEMPTS_META, f"{key}:{attempts + 1}")
            return True
        _measure_facade._upgrade_drain_release_lock()
        return False
    except Exception:
        return False


def _upgrade_drain_claim_lock():
    """Claim the drain lock file (O_EXCL); reclaim one left by a dead child."""
    _measure_facade = _compat.facade()
    marker = _measure_facade.SNAPSHOT_DIR / _measure_facade._UPGRADE_DRAIN_LOCK_NAME
    try:
        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            fd = _measure_facade.os.open(str(marker), _measure_facade.os.O_CREAT | _measure_facade.os.O_EXCL | _measure_facade.os.O_WRONLY, 0o600)
        except FileExistsError:
            if _measure_facade.time.time() - marker.stat().st_mtime <= _measure_facade._UPGRADE_DRAIN_LOCK_STALE_SECONDS:
                return False
            _measure_facade.os.unlink(str(marker))
            fd = _measure_facade.os.open(str(marker), _measure_facade.os.O_CREAT | _measure_facade.os.O_EXCL | _measure_facade.os.O_WRONLY, 0o600)
        with _measure_facade.os.fdopen(fd, "w") as handle:
            handle.write(str(_measure_facade.os.getpid()))
        return True
    except OSError:
        return False


def _upgrade_drain_release_lock():
    _measure_facade = _compat.facade()
    try:
        (_measure_facade.SNAPSHOT_DIR / _measure_facade._UPGRADE_DRAIN_LOCK_NAME).unlink()
    except OSError:
        pass


def _spawn_upgrade_drain():
    """Start `measure.py upgrade-drain` fully detached (POSIX new session,
    Windows DETACHED_PROCESS with no console window). Never raises."""
    _measure_facade = _compat.facade()
    try:
        env = dict(_measure_facade.os.environ)
        env["TOKEN_OPTIMIZER_UPGRADE_DRAIN"] = "1"
        env["TOKEN_OPTIMIZER_INTERACTIVE"] = "1"
        env.pop("TOKEN_OPTIMIZER_HOOK", None)
        popen_kw = {} if _measure_facade.os.name == "nt" else {"start_new_session": True}
        child = _measure_facade.subprocess.Popen(
            [_measure_facade.sys.executable, _measure_facade.os.path.abspath(_measure_facade.__file__), "upgrade-drain", "--quiet"],
            stdin=_measure_facade.subprocess.DEVNULL,
            stdout=_measure_facade.subprocess.DEVNULL,
            stderr=_measure_facade.subprocess.DEVNULL,
            close_fds=True,
            env=env,
            creationflags=(getattr(_measure_facade.subprocess, "DETACHED_PROCESS", 0) | _measure_facade._NO_WINDOW),
            **popen_kw,
        )
        try:
            (_measure_facade.SNAPSHOT_DIR / _measure_facade._UPGRADE_DRAIN_LOCK_NAME).write_text(str(child.pid))
        except OSError:
            pass
        return True
    except Exception as exc:
        try:
            _measure_facade._log_spawn_failure("upgrade drain spawn failed: %s: %s" % (type(exc).__name__, exc))
        except Exception:
            pass
        return False


def _upgrade_drain_days():
    """Days the one-time drain re-reads: all history since install, so routing
    and the since-install total cover every month. At least the usual 90-day
    collect window; ten years when no install date is known."""
    _measure_facade = _compat.facade()
    try:
        d = _measure_facade._install_date()
        if d:
            return max(90, min(3650, (_measure_facade.datetime.now() - _measure_facade.datetime.fromisoformat(d)).days + 2))
    except Exception:
        pass
    return 3650


def _run_upgrade_drain(days=None, quiet=None):
    """Body of `measure.py upgrade-drain`: one uncapped collect (usage re-parse,
    per-day usage, counted ledger and markers), then release the lock."""
    _measure_facade = _compat.facade()
    _measure_facade.os.environ["TOKEN_OPTIMIZER_UPGRADE_DRAIN"] = "1"
    try:
        return _measure_facade.collect_sessions(days=days or _measure_facade._upgrade_drain_days(), quiet=quiet)
    finally:
        _measure_facade._upgrade_drain_release_lock()


def collect_sessions(days=None, quiet=None, rebuild=None):
    """Parse new JSONL files and insert into SQLite. Zero token cost.

    Refreshes changed files in place. Safe to run repeatedly.
    With rebuild=True, drops and re-collects all data (e.g., after a
    measurement fix such as model attribution).
    """
    _measure_facade = _compat.facade()
    if _measure_facade._use_hermes_session_adapter():
        return _measure_facade._collect_hermes_sessions(days=days, quiet=quiet, rebuild=rebuild)

    if _measure_facade._use_copilot_session_adapter():
        return _measure_facade._collect_copilot_sessions(days=days, quiet=quiet, rebuild=rebuild)

    if _measure_facade._use_cursor_session_adapter():
        return _measure_facade._collect_cursor_sessions(days=days, quiet=quiet, rebuild=rebuild)

    if _measure_facade._use_antigravity_session_adapter():
        return _measure_facade._collect_antigravity_sessions(days=days, quiet=quiet, rebuild=rebuild)

    if _measure_facade._use_grok_session_adapter():
        return _measure_facade._collect_grok_sessions(days=days, quiet=quiet, rebuild=rebuild)

    conn = _measure_facade._init_trends_db()
    _session_log_has_origin = bool(_measure_facade._origin_insert_parts(conn, "session_log")[0])
    _session_log_has_usage_version = "usage_parser_version" in {
        r[1] for r in conn.execute("PRAGMA table_info(session_log)").fetchall()}

    # Capture the existing comparison before refreshing historical session rows.
    # The user's baseline must not move as collection catches up.
    if not rebuild and not (_measure_facade.SNAPSHOT_DIR / "workload_anchor.json").exists():
        _measure_facade._session_weight_pool_savings(
            (_measure_facade.datetime.now() - _measure_facade.timedelta(days=30)).date().isoformat())

    # One-time migration for the model attribution fix: wipe model_daily (safe, fast, no data loss)
    if _measure_facade._needs_model_daily_rebuild(conn):
        _measure_facade._migrate_model_daily(conn, quiet=quiet)

    # v5.4.9 one-time migration: streaming-dedup + subagent roll-up changed
    # how output_tokens and model_usage are computed. Pre-v5.4.9 data is wrong.
    # Wipe everything so the loop below re-parses all JSONL files with the
    # corrected logic. This makes `rebuild=True` implicit on first v5.4.9 run.
    if _measure_facade._needs_streaming_dedup_rebuild(conn):
        _measure_facade._migrate_streaming_dedup(conn, quiet=quiet)
        rebuild = True  # force re-parse of all sessions

    if rebuild:
        if not quiet:
            print("[Token Optimizer] Rebuilding trends DB (re-parsing all sessions)...")
        # Mark version FIRST so a killed process doesn't re-trigger
        conn.execute("PRAGMA user_version = 3")
        conn.execute("DELETE FROM session_log")
        conn.execute("DELETE FROM session_log_aliases")
        conn.execute("DELETE FROM usage_request_claims")
        conn.execute("DELETE FROM daily_stats")
        conn.execute("DELETE FROM model_daily")
        conn.execute("DELETE FROM skill_daily")
        conn.execute("DELETE FROM subagent_daily")
        # Drop event tables too: their session_id/session_uuid references would
        # otherwise dangle (no FK constraints) and silently inflate savings.
        conn.execute("DELETE FROM savings_events")
        conn.execute("DELETE FROM compression_events")
        conn.execute("DELETE FROM counted_reread")
        conn.commit()
    files = _measure_facade._find_all_jsonl_files(None)
    if not files:
        if not quiet:
            print(f"No session logs found in the last {days} days.")
        conn.close()
        return 0

    try:
        _measure_facade._dedupe_session_rows(conn)
    except _measure_facade.sqlite3.Error:
        pass

    new_count = 0
    live_owners = _measure_facade._live_usage_owners(conn)
    stale_usage_rows = _measure_facade._stale_usage_row_paths(conn)
    drain = _measure_facade._collect_drain_mode()
    budgets = _measure_facade._COLLECT_DRAIN_BUDGETS.get(drain, _measure_facade._COLLECT_DRAIN_BUDGETS[None])
    parse_deadline = (_measure_facade.time.monotonic() + budgets["parse_seconds"]
                      if budgets["parse_seconds"] is not None else None)
    commit_batch = budgets["commit_batch"] or _measure_facade._COLLECT_COMMIT_BATCH
    parse_truncated = False
    for filepath, mtime, project_name in files:
        if (_measure_facade._is_file_collected(conn, filepath, check_mtime=True)
                and str(filepath) not in stale_usage_rows):
            continue

        # Bounded per-run collection: never parse more than _COLLECT_MAX_PER_RUN new
        # sessions in one flush. `files` is newest-first, so recent sessions are always
        # collected and any large backlog drains over subsequent flushes rather than
        # blocking turn-end. (rebuild re-collects everything, so it opts out.)
        # A drain run (see _collect_drain_mode) has no count cap; an inline drain
        # stops at its wall-clock budget instead.
        if (budgets["count_capped"] and _measure_facade._COLLECT_MAX_PER_RUN and not rebuild
                and new_count >= _measure_facade._COLLECT_MAX_PER_RUN):
            parse_truncated = True
            break
        if parse_deadline is not None and not rebuild and _measure_facade.time.monotonic() > parse_deadline:
            parse_truncated = True
            break

        collection_started = _measure_facade.datetime.now().isoformat()
        # Rollups must not mutate the parser cache when only a child grows.
        parsed = _measure_facade.copy.deepcopy(_measure_facade._parse_session_jsonl(filepath))
        if not parsed:
            _measure_facade._stamp_usage_parser_version(conn, filepath)
            continue
        owner_id = _measure_facade._usage_owner_id(filepath)

        # Scan subagent JSONL files for skills, agents, and model usage.
        # Single pass over subagent files to avoid duplicate glob. Includes
        # subagents stored under other copies of this session (worktrees).
        subagent_files = _measure_facade._session_subagent_files(filepath)
        # Keep parsed["daily_metrics"] parent-only: _session_daily_metrics merges
        # subagent days into a copy, and mutating the memoized parse result would
        # double-count subagent metrics on any later pass over the same file.
        session_daily_metrics = _measure_facade._session_daily_metrics(parsed, subagent_files)
        for sub_jf in subagent_files:
            sub_skills, sub_agents = _measure_facade._extract_skills_and_agents_from_subagent(sub_jf)
            for sk, cnt in sub_skills.items():
                parsed["skills_used"][sk] = parsed["skills_used"].get(sk, 0) + cnt
            for ag, cnt in sub_agents.items():
                parsed["subagents_used"][ag] = parsed["subagents_used"].get(ag, 0) + cnt

        # a follow-up fix: Build combined model usage (parent + subagents) for
        # model_daily attribution. Kept SEPARATE from parsed["model_usage"]
        # which stays parent-only for: session_log storage, dom_model
        # selection, and cost calculations. Merging into model_usage would
        # flip dom_model to a cheaper subagent model, mispricing sessions.
        #
        # v5.4.9: ALSO aggregate subagent input/output/cache tokens into the
        # session-level totals so session_log.output_tokens (and the dashboard
        # "Billable Tokens" headline derived from it) captures work the main
        # thread delegated to agents. Without this, sessions that fan out
        # heavily via Task tool appeared to use ~0 tokens.
        all_model_usage = dict(parsed["model_usage"])
        for sub_jf in subagent_files:
            sub_parsed = _measure_facade._parse_session_jsonl(sub_jf)
            if not sub_parsed:
                continue
            # Aggregate model_usage (for model_daily)
            if sub_parsed.get("model_usage"):
                for model_id, tokens in sub_parsed["model_usage"].items():
                    if model_id.startswith("<"):  # skip synthetic IDs
                        continue
                    all_model_usage[model_id] = (
                        all_model_usage.get(model_id, 0) + tokens
                    )
            # v5.4.9: also roll subagent token totals up into session_log
            parsed["total_input_tokens"] = (parsed.get("total_input_tokens") or 0) + (sub_parsed.get("total_input_tokens") or 0)
            parsed["total_output_tokens"] = (parsed.get("total_output_tokens") or 0) + (sub_parsed.get("total_output_tokens") or 0)
            parsed["total_cache_create_1h"] = (parsed.get("total_cache_create_1h") or 0) + (sub_parsed.get("total_cache_create_1h") or 0)
            parsed["total_cache_create_5m"] = (parsed.get("total_cache_create_5m") or 0) + (sub_parsed.get("total_cache_create_5m") or 0)
            # Recompute cache_hit_rate over combined totals:
            # total_full_input already includes cache_read + cache_create per _parse_session_jsonl
            # but the stored cache_hit_rate is parent-only. Leave as-is (parent-derived)
            # since mixing subagent hit rates isn't meaningful for per-session display.

        merged = _measure_facade._session_merged_requests(filepath, parsed, subagent_files)
        # What another stored session already counts of a request (a resumed or
        # forked transcript repeats its parent's history, and a copy can hold a
        # longer streamed reply) is left out of this one; this session adds only
        # the difference, so the sessions together hold each request's largest usage.
        counted, retained, own_shares = {}, {}, {}
        if merged is not None:
            if _measure_facade._stable_hash63(owner_id) in live_owners:
                retained = _measure_facade._retained_requests(conn, owner_id, merged)
            else:
                # Shares recorded by a session that has no row any more are not history.
                try:
                    conn.execute("DELETE FROM usage_request_claims WHERE owner_hash = ?",
                                 (_measure_facade._stable_hash63(owner_id),))
                except _measure_facade.sqlite3.Error:
                    pass
            counted, own_shares = _measure_facade._claim_state(conn, owner_id, merged, live_owners)
            if counted or own_shares:
                merged = _measure_facade._net_of_counted(merged, counted, own_shares)
                parsed = _measure_facade._parsed_net_of_counted(parsed, counted, own_shares)
            if retained:
                merged = {**merged, **retained}
                parsed = _measure_facade._parsed_with_requests(
                    parsed, {**(parsed.get("request_usage") or {}), **retained})
        if merged is not None:
            daily_usage = _measure_facade._daily_usage_from_merged(merged)
        else:
            daily_usage = _measure_facade._session_daily_usage(filepath, parsed=parsed, subagent_files=subagent_files)
        if subagent_files or counted or retained or own_shares or _measure_facade._session_copy_files(filepath):
            # The per-file sums above count a request written into both the
            # parent and a subagent file twice; restate the stored token totals
            # from the same request-merged set the cost uses.
            if merged is not None:
                parsed["total_input_tokens"] = sum(int(u.get("inp") or 0) + int(u.get("cr") or 0) + int(u.get("cc") or 0)
                                                   for u in merged.values())
                parsed["total_output_tokens"] = sum(int(u.get("out") or 0) for u in merged.values())
                parsed["total_cache_create_1h"] = sum(int(u.get("cc_1h") or 0) for u in merged.values())
                parsed["total_cache_create_5m"] = sum(int(u.get("cc_5m") or 0) for u in merged.values())
                all_model_usage = {}
                for u in merged.values():
                    if _measure_facade._net_request_is_empty(u):
                        continue
                    model_id = u.get("model") or "unknown"
                    if model_id.startswith("<"):
                        continue
                    all_model_usage[model_id] = all_model_usage.get(model_id, 0) + (
                        int(u.get("inp") or 0) + int(u.get("cc") or 0) + int(u.get("out") or 0))

        parent_daily = parsed.get("daily_usage") or {}
        activity_bounds = _measure_facade._activity_bounds(daily_usage, parent_daily, session_daily_metrics)
        date = activity_bounds[-1] if activity_bounds else ""
        skills_used = parsed["skills_used"]
        subagents_used = parsed["subagents_used"]

        # One row per session (#200): if this session is already stored under
        # another path (worktree dir, sandboxed-config mirror), keep whichever
        # copy is more complete instead of counting the session twice.
        session_uuid = _measure_facade._canonical_session_uuid(filepath)
        # Origin of this row. A session first collected in a cloud VM keeps
        # 'cloud' when a more complete copy (a teleported transcript) replaces
        # it, so the one surviving row still says where the session ran.
        row_origin = _measure_facade.run_origin()
        if session_uuid:
            other = conn.execute(
                "SELECT id, message_count, input_tokens, output_tokens FROM session_log "
                "WHERE session_uuid = ? AND jsonl_path != ?",
                (session_uuid, str(filepath)),
            ).fetchall()
            try:
                if conn.execute(
                    "SELECT 1 FROM session_log WHERE session_uuid = ? AND origin = 'cloud' LIMIT 1",
                    (session_uuid,),
                ).fetchone():
                    row_origin = "cloud"
            except _measure_facade.sqlite3.Error:
                pass
            mine = _measure_facade._session_rank(parsed.get("message_count"), parsed.get("total_input_tokens"),
                                 parsed.get("total_output_tokens"))
            # A tie keeps the stored copy, matching _dedupe_session_rows.
            if any(_measure_facade._session_rank(r[1], r[2], r[3]) >= mine for r in other):
                conn.execute(
                    "INSERT OR REPLACE INTO session_log_aliases (jsonl_path, session_uuid, collected_at) "
                    "VALUES (?, ?, ?)", (str(filepath), session_uuid, collection_started))
                # This copy changed since last seen and may hold requests the
                # stored copy lacks; its per-day usage (merged across copies) is
                # recomputed by the next backfill.
                conn.execute(
                    "UPDATE session_log SET daily_usage_json = NULL WHERE session_uuid = ? AND jsonl_path != ?",
                    (session_uuid, str(filepath)))
                _measure_facade._stamp_usage_parser_version(conn, filepath)
                continue
        else:
            other = []

        # Compute quality score at collection time for persistence
        sq = _measure_facade.score_session_quality(parsed)
        # B1: persist reclaimable stale-read waste (opportunity tier, not realized).
        stale_waste = _measure_facade._session_stale_waste_tokens(filepath)

        # Resolve the platform for this session. Codex transcripts carry
        # "runtime": "codex" in their parsed dict; Claude transcripts do not
        # set the key, so detect_runtime() (which returns the active runtime,
        # e.g. "claude" or "opencode") is the correct fallback.
        session_platform = parsed.get("runtime") or _measure_facade.detect_runtime()

        # Insert session_log. ``origin`` rides along only when the column
        # exists: if the migration lost its busy wait, every insert naming a
        # missing column would fail and the whole collect would store nothing
        # (review #10). Same rule as the event writers (_origin_insert_parts).
        o_col, o_mark, o_val, o_upd = "", "", (), ""
        if _session_log_has_origin:
            o_col += ", origin"
            o_mark += ", ?"
            o_val += (row_origin,)
            o_upd += ",\n               origin=COALESCE(session_log.origin, excluded.origin)"
        if _session_log_has_usage_version:
            o_col += ", usage_parser_version"
            o_mark += ", ?"
            o_val += (_measure_facade._USAGE_PARSER_VERSION,)
            o_upd += ",\n               usage_parser_version=excluded.usage_parser_version"
        cur = conn.execute(
            """INSERT INTO session_log
               (jsonl_path, date, project, duration_minutes, input_tokens,
                output_tokens, message_count, api_calls, cache_hit_rate,
                cache_create_1h_tokens, cache_create_5m_tokens, cache_ttl_scanned,
                avg_call_gap_seconds, max_call_gap_seconds, p95_call_gap_seconds,
                skills_json, subagents_json, tool_calls_json, model_usage_json,
                    all_model_usage_json, model_usage_breakdown_json, version, slug, topic, collected_at,
                    quality_score, quality_grade, stale_waste_tokens, is_sidechain,
                    sidechain_reason, reported_input_tokens, reported_output_tokens,
                    reported_model_usage_json, platform, session_uuid, daily_usage_json""" + o_col + """)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?""" + o_mark + """)
               ON CONFLICT(jsonl_path) DO UPDATE SET
               date=excluded.date,
               project=excluded.project,
               duration_minutes=excluded.duration_minutes,
               input_tokens=excluded.input_tokens,
               output_tokens=excluded.output_tokens,
               message_count=excluded.message_count,
               api_calls=excluded.api_calls,
               cache_hit_rate=excluded.cache_hit_rate,
               cache_create_1h_tokens=excluded.cache_create_1h_tokens,
               cache_create_5m_tokens=excluded.cache_create_5m_tokens,
               cache_ttl_scanned=excluded.cache_ttl_scanned,
               avg_call_gap_seconds=excluded.avg_call_gap_seconds,
               max_call_gap_seconds=excluded.max_call_gap_seconds,
               p95_call_gap_seconds=excluded.p95_call_gap_seconds,
               skills_json=excluded.skills_json,
               subagents_json=excluded.subagents_json,
               tool_calls_json=excluded.tool_calls_json,
               model_usage_json=excluded.model_usage_json,
               all_model_usage_json=excluded.all_model_usage_json,
               model_usage_breakdown_json=excluded.model_usage_breakdown_json,
               version=excluded.version,
               slug=excluded.slug,
               topic=excluded.topic,
               collected_at=excluded.collected_at,
               quality_score=excluded.quality_score,
               quality_grade=excluded.quality_grade,
               stale_waste_tokens=excluded.stale_waste_tokens,
               is_sidechain=excluded.is_sidechain,
               sidechain_reason=excluded.sidechain_reason,
               reported_input_tokens=excluded.reported_input_tokens,
               reported_output_tokens=excluded.reported_output_tokens,
               reported_model_usage_json=excluded.reported_model_usage_json,
               platform=excluded.platform,
               session_uuid=COALESCE(excluded.session_uuid, session_log.session_uuid),
               daily_usage_json=excluded.daily_usage_json""" + o_upd + """
               WHERE session_log.collected_at IS NULL
                  OR excluded.collected_at >= session_log.collected_at""",
            (
                str(filepath), date, project_name,
                parsed["duration_minutes"],
                parsed["total_input_tokens"],
                parsed["total_output_tokens"],
                parsed["message_count"],
                parsed.get("api_calls", 0),
                parsed["cache_hit_rate"],
                parsed.get("total_cache_create_1h", 0),
                parsed.get("total_cache_create_5m", 0),
                1,
                parsed.get("avg_call_gap_seconds"),
                parsed.get("max_call_gap_seconds"),
                parsed.get("p95_call_gap_seconds"),
                _measure_facade.json.dumps(skills_used),
                _measure_facade.json.dumps(subagents_used),
                _measure_facade.json.dumps(parsed["tool_calls"]),
                _measure_facade.json.dumps(parsed["model_usage"]),
                _measure_facade.json.dumps(all_model_usage),
                _measure_facade.json.dumps(parsed.get("model_usage_breakdown", {})),
                parsed["version"],
                parsed.get("slug"),
                parsed.get("topic"),
                collection_started,
                sq["score"],
                sq["grade"],
                int(stale_waste or 0),
                1 if parsed.get("is_sidechain") else 0,
                parsed.get("sidechain_reason"),
                int(parsed.get("reported_input_tokens", 0) or 0),
                int(parsed.get("reported_output_tokens", 0) or 0),
                _measure_facade.json.dumps(parsed.get("reported_model_usage", {})),
                session_platform,
                session_uuid,
                _measure_facade.json.dumps(daily_usage),
            ) + o_val,
        )
        if cur.rowcount != 1:
            continue
        calls = dict(parsed.get("daily_calls") or {})
        for u in (merged or parsed.get("request_usage") or {}).values():
            if not u.get("advisor") and u.get("n", 1) and u.get("day"):
                day = u["day"]
                calls[day] = calls.get(day, 0) + 1
        conn.execute(
            "UPDATE session_log SET first_activity_day=?, last_activity_day=?, transcript_mtime=?, "
            "parent_daily_usage_json=?, daily_calls_json=?, daily_metrics_json=? WHERE jsonl_path=?",
            (activity_bounds[0] if activity_bounds else None, activity_bounds[-1] if activity_bounds else None, mtime,
             _measure_facade.json.dumps(parent_daily), _measure_facade.json.dumps(calls), _measure_facade.json.dumps(session_daily_metrics), str(filepath)))
        if merged is not None:
            _measure_facade._claim_requests(conn, owner_id, merged)
            live_owners.add(_measure_facade._stable_hash63(owner_id))
            live_owners.add(_measure_facade._stable_hash63(str(filepath)))
        if session_uuid:
            # Retire the older copies only now that this row has landed; an
            # upsert the WHERE guard rejected must not leave the session with
            # no current row.
            if other:
                losers = conn.execute(
                    "SELECT jsonl_path, collected_at FROM session_log WHERE session_uuid = ? AND jsonl_path != ?",
                    (session_uuid, str(filepath)),
                ).fetchall()
                conn.executemany(
                    "INSERT OR REPLACE INTO session_log_aliases (jsonl_path, session_uuid, collected_at) "
                    "VALUES (?, ?, ?)", [(r[0], session_uuid, r[1]) for r in losers])
                conn.executemany("DELETE FROM session_log WHERE id = ?", [(r[0],) for r in other])
            conn.execute("DELETE FROM session_log_aliases WHERE jsonl_path = ?", (str(filepath),))

        new_count += 1
        # Bank progress every batch so an interrupted deep backfill (a hook timeout
        # mid-parse) persists what it already collected instead of rolling back the
        # whole transaction. _is_file_collected skips banked files on the next run, so
        # the backfill converges over a few cycles rather than looping forever.
        if new_count % commit_batch == 0:
            try:
                conn.commit()
            except _measure_facade.sqlite3.Error:
                pass

    # Bounded backfill of derived metrics for previously-collected sessions that
    # are missing columns added by a later upgrade (e.g. quality_score, call gaps).
    # Called ONCE after the collection loop with a small cap (50) so a new column
    # never re-triggers a full-history re-parse storm. The rolling window converges
    # over a few flushes instead of blocking turn-end for minutes.
    try:
        _measure_facade._backfill_session_metrics(conn, days=days, limit=50)
    except Exception:
        pass
    daily_truncated = False
    try:
        _daily_started = _measure_facade.time.monotonic()
        _measure_facade._backfill_daily_usage(conn, days=days if drain == "full" else min(days, 90),
                              budget_seconds=budgets["daily_seconds"])
        daily_truncated = _measure_facade.time.monotonic() - _daily_started >= budgets["daily_seconds"]
    except Exception:
        pass
    try:
        _measure_facade._pretool_anchor_step(budget_seconds=5.0)
    except Exception:
        pass

    # Keep the counted-cumulative ledger fresh in the SAME bounded
    # collect pass (never at dashboard-regen time). Both helpers carry their
    # own per-pass caps + soft deadline and are fail-open, so a slow transcript
    # can never hold the flush; the backlog converges over passes.
    counted_pending = 0
    try:
        if drain == "full":
            counted_pending = int(_measure_facade._counted_backfill_all(conn, quiet=quiet).get("pending_sessions", 0) or 0)
        else:
            counted_pending = int(_measure_facade._update_counted_cumulative(
                conn, max_sessions=budgets["counted_sessions"],
                deadline_seconds=budgets["counted_seconds"],
                quiet=quiet).get("pending_sessions", 0) or 0)
            if drain == "inline":
                _measure_facade._counted_marker_backfill(conn, max_files=None,
                                         deadline_seconds=budgets["counted_seconds"], quiet=quiet)
            else:
                _measure_facade._counted_marker_backfill(conn, quiet=quiet)
    except Exception:
        pass
    try:
        if drain == "full":
            _measure_facade._upgrade_drain_mark_done(conn)
        elif not rebuild:
            _measure_facade._upgrade_drain_after_pass(
                conn, backlog=bool(parse_truncated or daily_truncated or counted_pending))
    except Exception:
        pass

    # ATOMICITY INVARIANT: _rebuild_aggregate_tables does DELETE-then-reINSERT and must
    # NOT commit internally. The whole wipe+rebuild is one transaction that commits only on
    # the next line, so a _HookTimeout (BaseException) mid-rebuild rolls back cleanly and the
    # prior aggregates survive intact. NEVER insert a conn.commit() between the DELETE and the
    # re-INSERTs -- a half-applied commit would leave the dashboard reading empty aggregates.
    _measure_facade._rebuild_aggregate_tables(conn)
    conn.commit()
    # Ensure schema version is set (idempotent, also set in migration and rebuild)
    conn.execute("PRAGMA user_version = 3")
    conn.commit()  # PRAGMA write must be committed explicitly
    conn.close()

    if not quiet:
        total = _measure_facade.conn_total_sessions() if _measure_facade.TRENDS_DB.exists() else new_count
        print(f"[Token Optimizer] Collected {new_count} new sessions. Total in DB: {total}")
    return new_count


def conn_total_sessions():
    """Quick count of total sessions in the DB.
    
    Uses Total Recall's DB-lock pattern: WAL mode + 5s busy_timeout + retry.
    """
    _measure_facade = _compat.facade()
    try:
        conn = _measure_facade.sqlite3.connect(str(_measure_facade.TRENDS_DB))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.execute("PRAGMA journal_size_limit=67108864")
        cur = conn.execute("SELECT COUNT(*) FROM session_log")
        count = cur.fetchone()[0]
        conn.close()
        return count
    except (_measure_facade.sqlite3.Error, OSError):
        return 0


def _read_path_upkeep(conn, days):
    """Best-effort catch-up writes before a dashboard read.

    Each step waits at most ~200ms for the write lock and the rest are skipped
    once one is contended, so a render never stalls behind a running collector;
    the collector does the same work on its next flush.
    """
    _measure_facade = _compat.facade()
    conn.execute("PRAGMA busy_timeout=200")
    try:
        # Collapse duplicate sessions left by older versions and rebuild the
        # per-day summary tables if any went, so this render never mixes
        # deduped rows with still-doubled aggregates. Dedupe first, so the
        # backfill never spends its budget on rows about to be deleted.
        if _measure_facade._dedupe_session_rows(conn):
            _measure_facade._rebuild_aggregate_tables(conn)
        conn.commit()
        _measure_facade._backfill_session_metrics(conn, days=days)
        _measure_facade._backfill_daily_usage(conn, days=days, budget_seconds=3.0)
    except _measure_facade.sqlite3.Error:
        try:
            conn.rollback()
        except _measure_facade.sqlite3.Error:
            pass
    finally:
        try:
            conn.execute("PRAGMA busy_timeout=5000")
        except _measure_facade.sqlite3.Error:
            pass


def _session_price_buckets(mb, pricing_tier):
    """(priced_tokens, unpriced_tokens) for a per-model breakdown."""
    _measure_facade = _compat.facade()
    priced = unpriced = 0
    for model_name, parts in (mb or {}).items():
        if not isinstance(parts, dict):
            continue
        model_tokens = (
            int(parts.get("fresh_input") or 0)
            + int(parts.get("cache_read") or 0)
            + int(parts.get("cache_create") or 0)
            + int(parts.get("output") or 0)
        )
        if _measure_facade._is_priced_model(model_name, tier=pricing_tier):
            priced += model_tokens
        else:
            unpriced += model_tokens
    return priced, unpriced


def _price_session_row(sr, pricing_tier):
    """Price one session_log row: THE per-session cost for the dashboard and
    for `session-cost`, so the two can never disagree.

    *sr* needs date, input_tokens, output_tokens, cache_hit_rate,
    cache_create_1h_tokens, cache_create_5m_tokens, model_usage_json,
    model_usage_breakdown_json and daily_usage_json. Every model is priced at
    its own generation card (_get_model_cost -> _claude_price_key).

    Returns a dict:
      main_cost   parent-thread cost (stored per-model breakdown; a dominant-model
                  estimate for rows that carry none)
      total_cost  the whole session. With per-day usage (parent + subagents,
                  requests merged by requestId so a shared one bills once) it is
                  the sum of the days; without it, main_cost.
      slices      [[date, cost, priced, unpriced, input, output], ...] every
                  billed day, uncut by any window
      has_daily   whether total_cost includes the subagents
      priced_tokens / unpriced_tokens, dom_model, model_usage, breakdown,
      daily_usage
    """
    _measure_facade = _compat.facade()
    inp_total = sr["input_tokens"] or 0
    out_total = sr["output_tokens"] or 0
    chr_val = sr["cache_hit_rate"] or 0
    cache_read_est = _measure_facade._safe_int(inp_total * chr_val)
    cache_create_1h = sr["cache_create_1h_tokens"] or 0
    cache_create_5m = sr["cache_create_5m_tokens"] or 0
    cache_create_total = cache_create_1h + cache_create_5m
    uncached_est = max(0, inp_total - cache_read_est - cache_create_total)

    mu = _measure_facade._safe_json_dict(sr["model_usage_json"])
    dom_model = max(mu, key=mu.get) if mu else "unknown"
    mb = _measure_facade._safe_json_dict(sr["model_usage_breakdown_json"])
    du = _measure_facade._safe_json_dict(sr["daily_usage_json"])
    if not du and "parent_daily_usage_json" in sr.keys():
        du = _measure_facade._safe_json_dict(sr["parent_daily_usage_json"])

    if mb:
        session_priced_tokens, session_unpriced_tokens = _measure_facade._session_price_buckets(mb, pricing_tier)
    else:
        model_tokens = inp_total + out_total
        if _measure_facade._is_priced_model(dom_model, tier=pricing_tier):
            session_priced_tokens, session_unpriced_tokens = model_tokens, 0
        else:
            session_priced_tokens, session_unpriced_tokens = 0, model_tokens
    # Main-thread cost from the stored session breakdown. Used as-is for rows
    # without per-day data (non-JSONL adapters, transcript gone).
    split = bool(cache_create_1h or cache_create_5m)
    session_cost = _measure_facade._cost_from_model_breakdown(mb, tier=pricing_tier,
                                               cache_create_1h=cache_create_1h if split else None,
                                               cache_create_5m=cache_create_5m if split else None)
    if session_cost == 0.0:
        # Use the stored 1h/5m split when available; fall back to 5m-only rate otherwise.
        if split:
            session_cost = _measure_facade._get_model_cost(dom_model, uncached_est, out_total, cache_read_est, cache_create_total,
                                           tier=pricing_tier, cache_create_1h=cache_create_1h, cache_create_5m=cache_create_5m)
        else:
            session_cost = _measure_facade._get_model_cost(dom_model, uncached_est, out_total, cache_read_est, cache_create_total, tier=pricing_tier)
    if session_cost == 0.0 and session_priced_tokens == 0 and session_unpriced_tokens == 0 and (inp_total or out_total):
        session_unpriced_tokens = inp_total + out_total

    # Unknown days remain in lifetime pricing but never acquire a window date.
    slices = []  # (date, cost, priced, unpriced, input_tokens, output_tokens)
    valid_days = frozenset(_measure_facade._activity_days(du))
    if du:
        by_day = {}
        for day, day_mb in du.items():
            if not isinstance(day_mb, dict):
                continue
            if day not in valid_days:
                continue
            _measure_facade._merge_daily_usage(by_day.setdefault(day, {}), {"_": day_mb})
        for day in sorted(by_day):
            day_mb = by_day[day].get("_", {})
            day_cost = _measure_facade._cost_from_model_breakdown(day_mb, tier=pricing_tier)
            day_priced, day_unpriced = _measure_facade._session_price_buckets(day_mb, pricing_tier)
            day_in = sum(
                int(p.get("fresh_input") or 0) + int(p.get("cache_read") or 0) + int(p.get("cache_create") or 0)
                for p in day_mb.values() if isinstance(p, dict))
            day_out = sum(int(p.get("output") or 0) for p in day_mb.values() if isinstance(p, dict))
            slices.append([day, day_cost, day_priced, day_unpriced, day_in, day_out])
        total_cost = sum(
                _measure_facade._cost_from_model_breakdown(models, tier=pricing_tier)
                for models in du.values() if isinstance(models, dict))
    else:
        total_cost = session_cost
        slices = []  # A lifetime estimate has no dated-window placement.
    return {
        "main_cost": session_cost,
        "total_cost": total_cost,
        "slices": slices,
        "has_daily": bool(du),
        "activity_complete": bool(du) and len(valid_days) == len(du),
        "priced_tokens": sum(sl[2] for sl in slices),
        "unpriced_tokens": sum(sl[3] for sl in slices),
        "dom_model": dom_model,
        "model_usage": mu,
        "breakdown": mb,
        "daily_usage": du,
    }
