"""Definition-only implementations; mutable state belongs to measure."""
from __future__ import annotations
from . import _compat, counted_orphans, counted_subagents, headline, structural_report, savings_story, transcript_stream

def _counted_event_utc(ts):
    """Savings-event timestamps are naive LOCAL time (datetime.now()); transcript
    timestamps are UTC. Convert local-naive -> UTC-naive (DST-correct for the
    event's own date via the system tz database). Never raises."""
    _measure_facade = _compat.facade()
    try:
        dt = _measure_facade.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.astimezone(_measure_facade.timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError, OSError):
        return None


def _counted_walk_transcript(path, collect_markers=None):
    """Parent-transcript walk for the counted engine; see counted_subagents.walk.
    Returns (turns, compacts, markers); never raises."""
    return _compat.facade()._counted_subagents.walk(_compat.facade(), path, collect_markers)


def _claude_image_tokens(width, height):
    """Image input tokens on Claude: about width*height/750, after the API
    scales the image so its long edge is at most 1568 px and its area at most
    about 1.15 megapixels. Some newer models accept larger images at more
    tokens; the standard limits give the smaller count, a minimum."""
    _measure_facade = _compat.facade()
    try:
        w, h = float(width), float(height)
    except (TypeError, ValueError):
        return 0
    if w <= 0 or h <= 0:
        return 0
    scale = min(1.0, 1568.0 / max(w, h), _measure_facade.math.sqrt(1_150_000.0 / (w * h)))
    return int((w * scale) * (h * scale) / 750)


def _counted_media_results(path):
    """Media tool results in a transcript, oldest first.

    Returns [(utc-naive datetime, tool name, still holds a media block,
    text chars, (width, height) or None, holds an archive marker)] for tool
    results of the tools _MEDIA_TOOL_GLOBS names. Never raises."""
    _measure_facade = _compat.facade()
    import fnmatch
    names, results = {}, []
    try:
        with transcript_stream.open_transcript(path) as f:
            for line in f:
                if '"tool_use"' not in line and '"tool_result"' not in line:
                    continue
                try:
                    d = _measure_facade.json.loads(line)
                except (_measure_facade.json.JSONDecodeError, ValueError):
                    continue
                msg = d.get("message") if isinstance(d, dict) else None
                content = msg.get("content") if isinstance(msg, dict) else None
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        name = str(block.get("name") or "")
                        if any(fnmatch.fnmatchcase(name, g) for g in _measure_facade._MEDIA_TOOL_GLOBS):
                            names[block.get("id")] = name
                        continue
                    if block.get("type") != "tool_result" or block.get("tool_use_id") not in names:
                        continue
                    try:
                        dt = _measure_facade.datetime.strptime(str(d.get("timestamp"))[:19], "%Y-%m-%dT%H:%M:%S")
                    except (ValueError, TypeError):
                        continue
                    body = block.get("content")
                    parts = body if isinstance(body, list) else [body]
                    has_media, text = False, []
                    for part in parts:
                        if isinstance(part, str):
                            text.append(part)
                        elif isinstance(part, dict):
                            if part.get("type") in _measure_facade._COUNTED_MEDIA_BLOCK_TYPES:
                                has_media = True
                            elif isinstance(part.get("text"), str):
                                text.append(part["text"])
                    joined = "".join(text)
                    dims = _measure_facade._COUNTED_MEDIA_DIMS_RX.search(joined)
                    results.append((dt, names[block.get("tool_use_id")], has_media, len(joined),
                                    (int(dims.group(1)), int(dims.group(2))) if dims else None,
                                    "Full result archived" in joined))
    except (OSError, PermissionError):
        return []
    results.sort(key=lambda r: ((_measure_facade := _compat.facade()), r[0])[1])
    return results


def _counted_media_net_tokens(results, used, ev_dt, detail):
    """(net tokens, basis) a media archive removed from context.

    The event is matched to the nearest unused result of the same tool within
    _COUNTED_MEDIA_MATCH_SECONDS. It counts only when that result no longer
    holds the media block and carries the archive marker: the saving is the
    image's token cost (_claude_image_tokens on the size the result text
    states) minus the text left in its place, taken at 2 characters per token
    because the marker keeps base64-like text, which tokenizes densely. Any
    other case (image still in context, no size stated, no match) is 0."""
    _measure_facade = _compat.facade()
    match = _measure_facade._COUNTED_MEDIA_TOOL_RX.match(str(detail or ""))
    tool = match.group(1) if match else None
    best = None
    for index, result in enumerate(results):
        if index in used or (tool and result[1] != tool):
            continue
        gap = abs((result[0] - ev_dt).total_seconds())
        if gap <= _measure_facade._COUNTED_MEDIA_MATCH_SECONDS and (best is None or gap < best[0]):
            best = (gap, index)
    if best is None:
        return 0, "media_unmatched"
    used.add(best[1])
    _dt, _tool, has_media, text_chars, dims, archived = results[best[1]]
    if has_media or not archived:
        return 0, "media_kept_in_context"
    if dims is None:
        return 0, "media_size_unknown"
    net = _measure_facade._claude_image_tokens(*dims) - (text_chars + 1) // 2
    return (net, "media_image_net") if net > 0 else (0, "media_image_net")


def _counted_exact_card(model, tier_data, prompt_tokens=None):
    """Resolve a supported id or dated/provider alias, never a nearby family."""
    _measure_facade = _compat.facade()
    if not isinstance(model, str) or not model:
        return None
    value = _measure_facade._strip_provider_prefixes(model)
    # Provider deployment suffixes do not change the underlying model card.
    value = _measure_facade.re.sub(r"(?:-v\d+:\d+|@\d{8})$", "", value)
    value = _measure_facade.re.sub(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$", "", value)
    def select(table, long_table, key, threshold):
        _measure_facade = _compat.facade()
        if key in long_table:
            if prompt_tokens is None:
                return None
            if prompt_tokens > threshold:
                return long_table[key]
        return table[key]
    for table, long_table, threshold in (
            (_measure_facade.OPENAI_MODEL_PRICING, _measure_facade.OPENAI_LONG_CONTEXT_PRICING, _measure_facade.OPENAI_LONG_CONTEXT_INPUT_THRESHOLD),
            (_measure_facade.GEMINI_MODEL_PRICING, _measure_facade.GEMINI_LONG_CONTEXT_PRICING, _measure_facade.GEMINI_LONG_CONTEXT_INPUT_THRESHOLD)):
        native = "gpt-5.6-sol" if value == "gpt-5.6" else value
        if native in table:
            return select(table, long_table, native, threshold)
    models = tier_data.get("claude_models", {})
    value = value.removeprefix("claude-")
    if value == "mythos":
        value = "fable"
    key = value.replace("-", "_").replace(".", "_")
    if key.startswith("mythos_"):
        key = "fable_" + key[len("mythos_"):]
    if key in models:
        return select(models, tier_data.get("claude_models_lc", {}), key, _measure_facade.ANTHROPIC_LONG_CONTEXT_INPUT_THRESHOLD)
    legacy = _measure_facade.re.fullmatch(r"(\d+)(?:[-.](\d+))?-(opus|sonnet|haiku)", value)
    if legacy:
        major, minor, family = legacy.groups()
        key = f"{family}_{major}" + (f"_{minor}" if minor else "")
        if key in models:
            return select(models, tier_data.get("claude_models_lc", {}), key, _measure_facade.ANTHROPIC_LONG_CONTEXT_INPUT_THRESHOLD)
        if major == "3" and family == "sonnet" and minor in (None, "5", "7"):
            return models.get("sonnet_legacy")
    return None


def _counted_coverage_totals(out):
    """Absolute coverage prevents an unknown credit and debit cancelling."""
    _measure_facade = _compat.facade()
    for kind in ("oneshot", "reread"):
        out[kind + "_unpriced_tokens"] = sum(
            out.get(kind + "_unpriced_" + sign + "_tokens", 0)
            for sign in ("credit", "debit"))
    missing_evidence = bool(out.get("legacy_unverified_events", 0) or out.get("unmeasured_events", 0))
    out["oneshot_pricing_complete"] = not (out["oneshot_unpriced_tokens"] or missing_evidence)
    out["pricing_complete"] = not (
        out["oneshot_unpriced_tokens"] or out["reread_unpriced_tokens"]
        or missing_evidence)
    out["reread_pricing_complete"] = not (
        out["reread_unpriced_tokens"] or missing_evidence)
    out["prices_available"] = bool(out.get("priced_oneshot_events")
                                   or out.get("priced_reread_turns")
                                   or out["pricing_complete"])
    out["oneshot_prices_available"] = bool(out.get("priced_oneshot_events")
                                            or out["oneshot_pricing_complete"])
    out["reread_prices_available"] = bool(out.get("priced_reread_turns")
                                           or out["reread_pricing_complete"])
    out["pricing_note"] = _measure_facade._counted_pricing_note(out)
    out["reread_pricing_note"] = _measure_facade._counted_pricing_note(out, reread_only=True)
    floor_events = int(out.get("oneshot_floor_events", 0))
    out["oneshot_floor_note"] = (
        f"{floor_events:,} initial removals ({int(out.get('oneshot_floor_tokens', 0)):,} tokens) are "
        "priced at the input rate, a minimum: their billed token class is not on record."
        if floor_events else "")
    return out


def _counted_pricing_note(out, reread_only=None):
    """Public coverage explanation shared by payloads and their consumers."""
    _measure_facade = _compat.facade()
    parts = []
    for kind, label in (("oneshot", "initial-removal"), ("reread", "repeat-read")):
        if reread_only and kind == "oneshot":
            continue
        credit = int(out.get(kind + "_unpriced_credit_tokens", 0))
        debit = int(out.get(kind + "_unpriced_debit_tokens", 0))
        if credit or debit:
            parts.append(f"{credit + debit:,} {label} tokens unpriced "
                         f"({credit:,} credits, {debit:,} debits)")
    legacy = int(out.get("legacy_unverified_events", 0))
    if legacy:
        parts.append(f"{legacy:,} historical events have unverifiable prices")
    unmeasured = int(out.get("unmeasured_events", 0))
    if unmeasured:
        credit = int(out.get("unmeasured_removed_credit_tokens", 0))
        debit = int(out.get("unmeasured_removed_debit_tokens", 0))
        parts.append(f"{unmeasured:,} removal events lack usable transcript evidence "
                     f"({credit:,} known removed tokens, {debit:,} re-expanded tokens); "
                     "their repeat-read counts are unavailable")
    text = "Priced subtotal only: " + "; ".join(parts) + ". The complete net dollar value is unavailable." if parts else ""
    mix_tokens = int(out.get("reread_session_mix_priced_tokens", 0))
    if mix_tokens:
        text += (f" {mix_tokens:,} repeat-read tokens are priced at the cache-read rate of the "
                 "models each session recorded, because their per-turn evidence is unavailable.")
    incomplete = int(out.get("refresh_incomplete_events", 0))
    if incomplete:
        text += f" Stored evidence retained for {incomplete:,} incomplete transcript refreshes."
    return text.strip()


def _counted_oneshot_rate(card, cache_class):
    """(rate, basis) for the first call after a removal, or (None, None).

    Context that was never removed would have reached that call as new tokens.
    Basis "class": the call wrote to the cache, so the removed tokens are priced
    at the write class it used (1h share at the 1h write rate, the rest at the
    5m rate). Basis "exact": the call used no cache at all, or the card has no
    cache-write premium, so the input rate is the price. Basis "floor": the call
    gives no write evidence (a cache hit with no write, or no usage record), so
    the input rate is a minimum.
    """
    _measure_facade = _compat.facade()
    import math
    rate = (card or {}).get("input")
    if not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate < 0:
        return None, None
    write = card.get("cache_write")
    if not isinstance(write, (int, float)) or not math.isfinite(write) or write <= rate:
        return rate, "exact"
    if cache_class:
        written, one_hour, _five_minute, read = cache_class
        if written > 0:
            write_1h = card.get("cache_write_1h", write)
            share = min(1.0, max(0.0, one_hour / written))
            return share * write_1h + (1.0 - share) * write, "class"
        if written == 0 and read == 0:
            return rate, "exact"
    return rate, "floor"


def _counted_score_detail(turns, compacts, ev_dt, tokens, tier_data, oneshot_floor_only=None):
    """Price each observed turn independently and retain unknown token classes.

    With oneshot_floor_only the initial removal is priced at the input rate (a
    minimum) even when the next call's cache-write class is on record."""
    _measure_facade = _compat.facade()
    import bisect as _bisect
    import math
    out = dict.fromkeys(_measure_facade._COUNTED_COVERAGE_KEYS, 0)
    out.update(oneshot_usd=0.0, oneshot_class_premium_usd=0.0, reread_tokens=0, reread_usd=0.0,
               turns_counted=0, observed=False, reread_miss_turns=0, reread_miss_premium_usd=0.0)
    times = [turn[0] for turn in turns]
    first = _bisect.bisect_right(times, ev_dt)
    boundary = next((c for c in compacts if c > ev_dt), None)
    end = _bisect.bisect_left(times, boundary) if boundary else len(turns)
    for i in range(first, end):
        out["observed"] = True
        kind = "oneshot" if i == first else "reread"
        card = _measure_facade._counted_exact_card(turns[i][1], tier_data,
                                   turns[i][2] if len(turns[i]) > 2 else None)
        basis = None
        if kind == "oneshot":
            cache_class = turns[i][3] if len(turns[i]) > 3 else None
            rate, basis = _measure_facade._counted_oneshot_rate(card, cache_class)
            if oneshot_floor_only and basis == "class":
                rate, basis = card["input"], "floor"
        else:  # Rebuilds reaching the removal position use the write class.
            rate, miss = _measure_facade._counted_pricing_v5.reread_turn_rate(card, turns[i][3] if len(turns[i]) > 3 else None, tokens, _measure_facade._counted_oneshot_rate, turns[i][2] if len(turns[i]) > 2 else None, turns[first][2] if len(turns[first]) > 2 else None)
            out["reread_tokens"] += tokens
            out["turns_counted"] += 1
            if miss:
                out["reread_miss_turns"] += 1
                out["reread_miss_premium_usd"] += tokens * (rate - card["cache_read"]) / 1e6
        if isinstance(rate, (int, float)) and math.isfinite(rate) and rate >= 0:
            out[kind + "_usd"] += tokens * rate / 1e6
            out["priced_oneshot_events" if kind == "oneshot" else "priced_reread_turns"] += 1
            if basis == "class":
                out["oneshot_class_events"] += 1
                out["oneshot_class_premium_usd"] += tokens * (rate - card["input"]) / 1e6
            elif basis == "floor":
                out["oneshot_floor_events"] += 1
                out["oneshot_floor_tokens"] += abs(tokens)
        else:
            sign = "credit" if tokens >= 0 else "debit"
            out[kind + "_unpriced_" + sign + "_tokens"] += abs(tokens)
    return _measure_facade._counted_coverage_totals(out)


def _counted_score_event(turns, compacts, ev_dt, tokens, tier_data):
    """Compatibility tuple: one-shot dollars, reread tokens/dollars and turns."""
    _measure_facade = _compat.facade()
    out = _measure_facade._counted_score_detail(turns, compacts, ev_dt, tokens, tier_data)
    return tuple(out[k] for k in ("oneshot_usd", "reread_tokens", "reread_usd", "turns_counted"))


def _counted_window_summary(conn, window_start, window_end):
    """Price counted savings inside one real window, including rereads."""
    _measure_facade = _compat.facade()
    out = {"available": False}
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='counted_reread'"
        ).fetchone()
        if not exists:
            return out
        start = window_start.astimezone(_measure_facade.timezone.utc).replace(tzinfo=None) \
            if window_start.tzinfo else window_start
        end = window_end.astimezone(_measure_facade.timezone.utc).replace(tzinfo=None) \
            if window_end.tzinfo else window_end
        if end < start:
            return out
        tier_data = _measure_facade.PRICING_TIERS.get(_measure_facade._load_pricing_tier(), _measure_facade.PRICING_TIERS["anthropic"])
        oneshot = reread_usd = class_premium = 0.0
        reread_tokens = removed_tokens = events = 0
        coverage = dict.fromkeys(_measure_facade._COUNTED_COVERAGE_KEYS, 0)
        missing = dict(unmeasured_events=0, unmeasured_removed_tokens=0,
                       unmeasured_removed_credit_tokens=0, unmeasured_removed_debit_tokens=0)
        candidates, direct = counted_orphans.window_events(_measure_facade, conn, _measure_facade._counted_candidate_events(conn)); scopes = {}
        for _key, session_uuid, ts, tokens, _logged_cost, _model in candidates:
            ev_dt = _measure_facade._counted_event_utc(ts)
            if ev_dt is None or ev_dt < start or ev_dt > end:
                continue
            path = _measure_facade._counted_session_path(conn, session_uuid)
            turns, compacts, _ = _measure_facade._counted_walk_transcript(path) if path is not None else ([], [], [])
            if turns and session_uuid not in scopes:  # v6: a sub-agent's removal scores on its own turns
                scopes[session_uuid] = counted_orphans.apply_scope(counted_subagents.SessionScope(_measure_facade, conn, path, [(c[0], c[2]) for c in candidates if c[1] == session_uuid]), {c[0]: direct[c[0]] for c in candidates if c[1] == session_uuid and c[0] in direct}, path)
            if turns and _key in scopes[session_uuid].unresolved: turns = []
            if turns:
                turns, compacts, _scope = scopes[session_uuid].turns_for(_key, turns, compacts)
            if not turns:
                missing["unmeasured_events"] += 1
                missing["unmeasured_removed_tokens"] += int(tokens or 0)
                sign = "credit" if int(tokens or 0) >= 0 else "debit"
                missing["unmeasured_removed_" + sign + "_tokens"] += abs(int(tokens or 0))
                continue
            turns = [turn for turn in turns if start <= turn[0] <= end]
            compacts = [compact for compact in compacts if start <= compact <= end]
            score = _measure_facade._counted_score_detail(
                turns, compacts, ev_dt, int(tokens or 0), tier_data)
            if not score["observed"]:
                continue
            oneshot += score["oneshot_usd"]
            class_premium += score["oneshot_class_premium_usd"]
            reread_tokens += score["reread_tokens"]
            reread_usd += score["reread_usd"]
            for key in coverage:
                coverage[key] += score[key]
            removed_tokens += int(tokens or 0)
            events += 1
        return _measure_facade._counted_coverage_totals({
            **coverage,
            **missing,
            "available": True,
            "events": events,
            "removed_tokens": removed_tokens,
            "oneshot_usd": round(oneshot, 6),
            "oneshot_class_premium_usd": round(class_premium, 6),
            "reread_tokens": reread_tokens,
            "reread_usd": round(reread_usd, 6),
            "total_usd": round(oneshot + reread_usd, 6),
            "method": "removals x deduped turns to next real compaction, "
                      "scoped to this window; per-turn cache-read rates; "
                      "transcript-backed events only",
        })
    except (AttributeError, OSError, _measure_facade.sqlite3.Error, TypeError, ValueError):
        return out


def _counted_candidate_events(conn):
    """Counted removal events from both ledgers, era-gated, resume_lean excluded.

    Yields (event_key, session_uuid, timestamp, tokens, as_logged_cost, model).
    compression_events has no cost column (as_logged_cost is NULL there). Events
    without a resolvable transcript are excluded by the caller."""
    _measure_facade = _compat.facade()
    se_types = ",".join("?" for _ in _measure_facade._COUNTED_SE_EVENT_TYPES)
    ce_excl = ",".join("?" for _ in _measure_facade._COUNTED_CE_EXCLUDED_FEATURES)
    rows = conn.execute(
        "SELECT 'se:'||id, session_uuid, timestamp, "
        "  CASE WHEN event_type='tool_archive_reexpand' THEN -tokens_saved "
        "       ELSE tokens_saved END, cost_saved_usd, model "
        "FROM " + _measure_facade._counted_table(conn, "savings_events") + " "
        "WHERE event_type IN (" + se_types + ") AND timestamp >= ? "
        "UNION ALL "
        "SELECT 'ce:'||id, session_uuid, timestamp, "
        "  original_tokens - compressed_tokens, NULL, model "
        "FROM " + _measure_facade._counted_table(conn, "compression_events") + " "
        "WHERE (tier IS NULL OR tier = 'measured') AND verified = 1 "
        "  AND original_tokens > compressed_tokens "
        "  AND feature NOT IN (" + ce_excl + ") AND timestamp >= ?",
        (*_measure_facade._COUNTED_SE_EVENT_TYPES, _measure_facade._COUNTED_LEDGER_ERA_START,
         *_measure_facade._COUNTED_CE_EXCLUDED_FEATURES, _measure_facade._COUNTED_LEDGER_ERA_START),
    ).fetchall()
    # Media archives (screenshots and other image results) are left out of the
    # counted views because their logged size is base64 length. They count
    # here under their own key with a size of 0: the collect pass sets the real
    # size from the transcript (see _counted_media_net_tokens). Only when the
    # counted views exist, since the raw-table fallback already includes them.
    if _measure_facade._counted_table(conn, "savings_events") == "counted_savings_events":
        try:
            rows += conn.execute(_measure_facade._COUNTED_MEDIA_CANDIDATE_SQL, (_measure_facade._COUNTED_LEDGER_ERA_START,)).fetchall()
        except _measure_facade.sqlite3.Error:
            pass
    return rows


def _counted_session_path(conn, session_uuid):
    """Transcript path for a session: session_log.jsonl_path, else a projects glob."""
    _measure_facade = _compat.facade()
    try:
        row = conn.execute(
            "SELECT jsonl_path FROM session_log WHERE session_uuid = ? "
            "ORDER BY id DESC LIMIT 1", (session_uuid,)).fetchone()
        if row and row[0] and _measure_facade.Path(row[0]).exists():
            return _measure_facade.Path(row[0])
    except _measure_facade.sqlite3.Error:
        pass
    try:
        base = _measure_facade.CLAUDE_DIR / "projects"
        if base.exists():
            cands = list(base.glob(f"*/{session_uuid}.jsonl"))
            if cands:
                return max(cands, key=lambda p: ((_measure_facade := _compat.facade()), p.stat().st_mtime)[1])
    except (OSError, PermissionError):
        pass
    return None


def _counted_pricing_detail(raw):
    _measure_facade = _compat.facade()
    try:
        value = _measure_facade.json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _ensure_counted_pricing_schema(conn):
    _measure_facade = _compat.facade()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(counted_reread)")}
    if "pricing_detail" not in cols:
        conn.execute("ALTER TABLE counted_reread ADD COLUMN pricing_detail TEXT")


def _reprice_counted_rows_once(conn):
    _measure_facade = _compat.facade()
    try:
        _measure_facade._ensure_counted_pricing_schema(conn)
        conn.execute("CREATE TABLE IF NOT EXISTS token_optimizer_meta "
                     "(key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute("SELECT value FROM token_optimizer_meta WHERE key = ?",
                           (_measure_facade._COUNTED_META_PRICING,)).fetchone()
        if row and row[0] == _measure_facade._COUNTED_PRICING_VERSION:
            return False
        # Keep mtimes and historical amounts as provenance. Readers quarantine
        # every row without the current per-row pricing version immediately.
        conn.execute("DELETE FROM token_optimizer_meta WHERE key IN (?, ?)",
                     (_measure_facade._COUNTED_META_MARKER_DONE, _measure_facade._COUNTED_META_MARKER_CURSOR))
        conn.execute("INSERT OR REPLACE INTO token_optimizer_meta (key, value) VALUES (?, ?)",
                     (_measure_facade._COUNTED_META_PRICING, _measure_facade._COUNTED_PRICING_VERSION))
        conn.commit()
        return True
    except _measure_facade.sqlite3.Error:
        return False


def _counted_file_proof(path, prefix_sizes=None):
    """Hash an observed file and old prefixes without retaining transcript text."""
    _measure_facade = _compat.facade()
    import hashlib
    before = path.stat()
    digest = hashlib.sha256()
    offset, prefixes = 0, {}
    with path.open("rb") as stream:
        for stop in sorted({int(n) for n in prefix_sizes if 0 < int(n) <= before.st_size}
                           | {before.st_size}):
            while offset < stop:
                chunk = stream.read(min(1024 * 1024, stop - offset))
                if not chunk:
                    raise OSError("transcript changed while reading")
                digest.update(chunk)
                offset += len(chunk)
            prefixes[stop] = digest.hexdigest()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError("transcript changed while reading")
    return {"file_size": offset, "file_sha256": digest.hexdigest(),
            "prefixes": prefixes, "mtime_ns": after.st_mtime_ns}


def _counted_mark_incomplete(conn, key, detail, proof=None):
    _measure_facade = _compat.facade()
    detail = dict(detail, refresh_incomplete=True, attempted_version=_measure_facade._COUNTED_PRICING_VERSION,
                  attempted_source=([proof["mtime_ns"], proof["file_size"]] if proof else None))
    conn.execute("UPDATE counted_reread SET pricing_detail=? WHERE event_key=?",
                 (_measure_facade.json.dumps(detail), key))


def _counted_legacy_append_continuous(old, turns, compacts, ev_dt, tokens, tier_data):
    """True when a row stored without a file digest was computed from the
    earlier part of this transcript, which has since only been appended to.

    The turns written up to the row's recorded transcript mtime must rescore to
    exactly the observed counts the row holds, and the transcript must hold
    turns after that point. A replaced or truncated file fails the first test;
    one with nothing new fails the second and stays on the unchanged-file rule.
    """
    _measure_facade = _compat.facade()
    try:
        old_mtime = old["transcript_mtime"]
        if old_mtime is None:
            return False
        cutoff = _measure_facade.datetime.fromtimestamp(float(old_mtime), _measure_facade.timezone.utc).replace(tzinfo=None)
        prefix = [turn for turn in turns if turn[0] <= cutoff]
        if not prefix or len(prefix) >= len(turns):
            return False
        prefix_score = _measure_facade._counted_score_detail(
            prefix, [c for c in compacts if c <= cutoff], ev_dt, tokens, tier_data)
        return (prefix_score["observed"]
                and prefix_score["turns_counted"] == int(old["turns_counted"] or 0)
                and prefix_score["reread_tokens"] == int(old["reread_tokens"] or 0))
    except (TypeError, ValueError, OverflowError, OSError):
        return False


def _counted_store_score(conn, key, source, uuid, ts, tokens, score, mtime,
                         proof, old=None, magnitude_basis=None, allow_unobserved=None,
                         append_check=None, identity_verified=None):
    """Replace only a complete continuation of the previously observed file."""
    _measure_facade = _compat.facade()
    detail = _measure_facade._counted_pricing_detail(old["pricing_detail"]) if old else {}
    if counted_orphans.can_relocate(old, key, source, ts, tokens, identity_verified):
        old = None  # versioned rebuild from a previously unlocated, zero-reread row
    if old:
        size = detail.get("file_size")
        if size is not None:
            continuous = proof["prefixes"].get(size) == detail.get("file_sha256")
        else:
            # Without an old digest, only the recorded unchanged file and
            # identical observed counts can anchor a legacy reprice, or an
            # append-only continuation the caller can show (append_check). A
            # marker row with no recorded mtime is anchored by the caller's
            # exact identity recomputation (identity_verified) instead.
            same_counts = (score["turns_counted"] == int(old["turns_counted"] or 0)
                           and score["reread_tokens"] == int(old["reread_tokens"] or 0))
            if old["transcript_mtime"] is None:
                continuous = identity_verified and same_counts
            else:
                continuous = ((old["transcript_mtime"] == mtime and same_counts)
                              or bool(append_check and append_check(old)))
        identity_matches = (old["source"] == source and old["session_uuid"] == uuid
                            and old["event_ts"] == ts)
        previous_activity = (int(old["turns_counted"] or 0) != 0
                             or float(old["oneshot_usd"] or 0) != 0
                             or bool(detail.get("priced_oneshot_events"))
                             or bool(detail.get("oneshot_unpriced_credit_tokens"))
                             or bool(detail.get("oneshot_unpriced_debit_tokens")))
        if (not continuous or not identity_matches or int(old["tokens"] or 0) != tokens
                or (not score["observed"] and (not allow_unobserved or previous_activity))):
            _measure_facade._counted_mark_incomplete(conn, key, detail, proof)
            return False
    if not score["observed"] and not allow_unobserved:
        return False
    detail = {key: score[key] for key in _measure_facade._COUNTED_COVERAGE_KEYS}
    detail.update(oneshot_class_premium_usd=round(score["oneshot_class_premium_usd"], 6),
                  reread_miss_turns=score.get("reread_miss_turns", 0),
                  reread_miss_premium_usd=round(score.get("reread_miss_premium_usd", 0.0), 6),
                  version=_measure_facade._COUNTED_PRICING_VERSION, refresh_incomplete=False, scope=score.get("scope", "parent"), resolved_agent=score.get("resolved_agent"),
                  magnitude_basis=magnitude_basis, file_size=proof["file_size"],
                  file_sha256=proof["file_sha256"])
    conn.execute(
        "INSERT OR REPLACE INTO counted_reread (event_key,source,session_uuid,event_ts,"
        "event_month,tokens,oneshot_usd,reread_tokens,reread_usd,turns_counted,"
        "transcript_mtime,computed_at,pricing_detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, source, uuid, ts, str(ts)[:7], tokens, round(score["oneshot_usd"], 6),
         score["reread_tokens"], round(score["reread_usd"], 6), score["turns_counted"],
         mtime, _measure_facade.datetime.now().isoformat(), _measure_facade.json.dumps(detail)))
    return True


def _counted_stored_rows(conn):
    _measure_facade = _compat.facade()
    cursor = conn.execute("SELECT * FROM counted_reread")
    names = [c[0] for c in cursor.description]
    return {row[0]: dict(zip(names, row)) for row in cursor}


def _update_counted_cumulative(conn, max_sessions=None,
                               deadline_seconds=None,
                               quiet=None):
    """Incrementally (re)compute counted_reread rows in the collect pass.

    A session is rewalked when events are new or its transcript has grown.
    Bounded by a soft deadline; the backlog converges over passes. Fail-open."""
    _measure_facade = _compat.facade()
    from . import first_read_revive
    first_read_revive.reconcile(conn, _measure_facade.SNAPSHOT_DIR)
    stats = {"sessions_walked": 0, "rows_written": 0, "pending_sessions": 0, "unmeasured_events": 0, **_compat.facade()._counted_subagents.collect_stats()}
    try:
        started = _measure_facade.time.monotonic()
        # Duplicate derived rows remain as evidence and are excluded at read time.
        stats['duplicate_rows_removed'] = 0
        _measure_facade._reprice_counted_rows_once(conn)
        events = _measure_facade._counted_candidate_events(conn)
        if not events:
            return stats
        stored = _measure_facade._counted_stored_rows(conn)
        by_sess, orphan_paths = counted_orphans.group_events(_measure_facade, conn, events, resolve=deadline_seconds is None)
        tier_data = _measure_facade.PRICING_TIERS.get(_measure_facade._load_pricing_tier(), _measure_facade.PRICING_TIERS["anthropic"])

        for key, ts, tok, cost, model in by_sess.pop(None, []) + by_sess.pop("", []):
            stats["unmeasured_events"] += 1

        todo = []
        for uuid, evs in by_sess.items():
            path = _measure_facade._counted_session_path(conn, uuid)
            if path is None:
                for key, *_ in evs:
                    if key in stored:
                        _measure_facade._counted_mark_incomplete(conn, key, _measure_facade._counted_pricing_detail(stored[key].get("pricing_detail")))
                stats["unmeasured_events"] += len(evs)
                continue
            try:
                mtime = path.stat().st_mtime
                source_stamp = [path.stat().st_mtime_ns, path.stat().st_size]
            except OSError:
                continue
            def fresh_event(key):
                _measure_facade = _compat.facade()
                if key not in stored or transcript_stream.needs_resume(orphan_paths.get(key, path)):
                    return False
                detail = _measure_facade._counted_pricing_detail(stored[key].get("pricing_detail"))
                st = orphan_paths.get(key, path).stat(); event_stamp = [st.st_mtime_ns, st.st_size]
                if detail.get("refresh_incomplete"):
                    return (detail.get("attempted_version") == _measure_facade._COUNTED_PRICING_VERSION
                            and detail.get("attempted_source") == event_stamp)
                return (stored[key]["transcript_mtime"] == st.st_mtime
                        and detail.get("file_size") == event_stamp[1]
                        and detail.get("version") == _measure_facade._COUNTED_PRICING_VERSION)
            fresh = all(fresh_event(key) for key, *_ in evs)
            if not fresh:
                todo.append((mtime, uuid, path, evs))
        stats["pending_sessions"] = len(todo)
        todo.sort(reverse=True)  # freshest first: the active session updates every pass

        for mtime, uuid, path, evs in todo[:max_sessions]:
            if deadline_seconds is not None and _measure_facade.time.monotonic() - started > deadline_seconds:
                break
            proof = None
            try:
                proof = _measure_facade._counted_file_proof(path, [
                    _measure_facade._counted_pricing_detail(stored[key].get("pricing_detail")).get("file_size", 0)
                    for key, *_ in evs if key in stored])
                turns, compacts, _ = _measure_facade._counted_walk_transcript(path)
                if transcript_stream.needs_resume(path) or path.stat().st_mtime_ns != proof["mtime_ns"] or path.stat().st_size != proof["file_size"]:
                    raise OSError("transcript changed while scoring")
            except OSError:
                if proof is None:
                    try:
                        stat = path.stat()
                        proof = {"mtime_ns": stat.st_mtime_ns, "file_size": stat.st_size}
                    except OSError:
                        pass
                for key, *_ in evs:
                    if key in stored:
                        _measure_facade._counted_mark_incomplete(conn, key, _measure_facade._counted_pricing_detail(stored[key].get("pricing_detail")), proof)
                stats["pending_sessions"] -= 1
                continue
            media_results, media_used, media_details = None, set(), {}
            if any(key.startswith("md:") for key, *_ in evs):
                media_results = _measure_facade._counted_media_results(path)
                ids = [int(key[3:]) for key, *_ in evs if key.startswith("md:")]
                for chunk in range(0, len(ids), 500):
                    part = ids[chunk:chunk + 500]
                    media_details.update(conn.execute(
                        "SELECT id, detail FROM savings_events WHERE id IN (%s)"
                        % ",".join("?" * len(part)), part).fetchall())
            scope = counted_orphans.apply_scope(counted_subagents.SessionScope(_measure_facade, conn, path, [(e[0], e[1]) for e in evs]), {e[0]: orphan_paths[e[0]] for e in evs if e[0] in orphan_paths}, path)
            counted_subagents.add_stats(stats, scope.stats)
            event_proofs = {}
            for key, ts, tok, cost, model in sorted(evs, key=lambda e: ((_measure_facade := _compat.facade()), str(e[1]))[1]):
                ev_dt = _measure_facade._counted_event_utc(ts)
                if key in scope.unresolved: stats["unmeasured_events"] += 1; continue
                if ev_dt is None or not turns:
                    if key in stored:
                        _measure_facade._counted_mark_incomplete(conn, key, _measure_facade._counted_pricing_detail(stored[key].get("pricing_detail")), proof)
                    stats["unmeasured_events"] += 1
                    continue
                basis = "metered"
                if key.startswith("md:"):
                    tok, basis = _measure_facade._counted_media_net_tokens(
                        media_results, media_used, ev_dt, media_details.get(int(key[3:])))
                event_path = orphan_paths.get(key, path); before_walk = event_path.stat()
                ev_turns, ev_compacts, ev_scope = scope.turns_for(key, turns, compacts)
                score = dict(_measure_facade._counted_score_detail(ev_turns, ev_compacts, ev_dt, int(tok or 0), tier_data,
                    oneshot_floor_only=key.startswith("md:")), scope=ev_scope, resolved_agent=event_path.name if key in orphan_paths and ev_scope == "subagent" else None)
                old_size = (_measure_facade._counted_pricing_detail(stored[key].get("pricing_detail")).get("file_size", 0) or 0) if key in stored else 0; proof_key = (event_path, old_size, before_walk.st_mtime_ns, before_walk.st_size)
                if proof_key not in event_proofs: event_proofs[proof_key] = _measure_facade._counted_file_proof(event_path, [old_size])
                event_proof = event_proofs[proof_key]; event_stat = event_path.stat(); event_mtime = event_stat.st_mtime
                if transcript_stream.needs_resume(event_path) or (before_walk.st_mtime_ns, before_walk.st_size) != (event_proof["mtime_ns"], event_proof["file_size"]) or (event_stat.st_mtime_ns, event_stat.st_size) != (event_proof["mtime_ns"], event_proof["file_size"]):
                    stats["unmeasured_events"] += 1; continue
                if not _measure_facade._counted_store_score(conn, key, key.split(":", 1)[0], uuid,
                                             ts, int(tok or 0), score, event_mtime, event_proof,
                                             old=stored.get(key), allow_unobserved=True, identity_verified=key in orphan_paths,
                                             magnitude_basis=basis,
                                             append_check=lambda old, _ev=ev_dt, _tok=int(tok or 0): (
                                                 ((_measure_facade := _compat.facade()), _measure_facade._counted_legacy_append_continuous(
                                                     old, ev_turns, ev_compacts, _ev, _tok, tier_data))[1])):
                    stats["unmeasured_events"] += 1
                    continue
                stats["rows_written"] += 1
            stats["sessions_walked"] += 1
            stats["pending_sessions"] -= 1
            # Commit each session so a long drain never holds the write lock
            # that a concurrent hook collect is waiting on.
            conn.commit()
        conn.commit()
        if not quiet and stats["rows_written"]:
            print(f"[Token Optimizer] counted-cumulative: walked "
                  f"{stats['sessions_walked']} session(s), "
                  f"{stats['rows_written']} row(s), "
                  f"{stats['pending_sessions']} pending.")
    except Exception:
        pass
    return stats


def _counted_marker_backfill(conn, max_files=None,
                             deadline_seconds=None,
                             quiet=None):
    """One-time representation of the pre-ledger era (before
    _COUNTED_LEDGER_ERA_START) from the transcripts' own "Full result archived"
    markers -- the Mar-Apr ledger logged 17 events against 305 physical markers.
    Cursor-resumable over collect passes. Completed passes retry changed source
    evidence; unavailable history retains its coverage and refresh status."""
    _measure_facade = _compat.facade()
    stats = {"files_scanned": 0, "rows_written": 0, "done": False}
    try:
        _measure_facade._reprice_counted_rows_once(conn)
        stored = _measure_facade._counted_stored_rows(conn)
        pending_markers = [r for r in stored.values() if r["source"] == "mk"
                           and (_measure_facade._counted_pricing_detail(r.get("pricing_detail")).get("version") != _measure_facade._COUNTED_PRICING_VERSION
                                or _measure_facade._counted_pricing_detail(r.get("pricing_detail")).get("refresh_incomplete"))]
        retry_markers = []
        old_by_session = {}
        for old in stored.values():
            if old["source"] == "mk":
                old_by_session.setdefault(old["session_uuid"], []).append(old)
        for old in (r for r in stored.values() if r["source"] == "mk"):
            detail = _measure_facade._counted_pricing_detail(old.get("pricing_detail"))
            path = _measure_facade._counted_session_path(conn, old["session_uuid"])
            try:
                stat = path.stat() if path else None
            except OSError:
                stat = None
            if stat is None:
                _measure_facade._counted_mark_incomplete(conn, old["event_key"], detail)
            elif (detail.get("version") == _measure_facade._COUNTED_PRICING_VERSION
                  and not detail.get("refresh_incomplete")
                  and old["transcript_mtime"] == stat.st_mtime
                  and detail.get("file_size") == stat.st_size):
                continue
            elif (detail.get("attempted_version") != _measure_facade._COUNTED_PRICING_VERSION
                  or detail.get("attempted_source") != [stat.st_mtime_ns, stat.st_size]):
                retry_markers.append(old)
        stats["quarantined_or_incomplete_rows"] = len(pending_markers)
        stats["retryable_rows"] = len(retry_markers)
        conn.execute("CREATE TABLE IF NOT EXISTS token_optimizer_meta "
                     "(key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute("SELECT value FROM token_optimizer_meta WHERE key = ?",
                           (_measure_facade._COUNTED_META_MARKER_DONE,)).fetchone()
        if row and row[0] == "1" and not retry_markers:
            conn.commit()
            stats["done"] = True
            return stats
        if row and row[0] == "1":
            conn.execute("DELETE FROM token_optimizer_meta WHERE key IN (?, ?)",
                         (_measure_facade._COUNTED_META_MARKER_DONE, _measure_facade._COUNTED_META_MARKER_CURSOR))
        row = conn.execute("SELECT value FROM token_optimizer_meta WHERE key = ?",
                           (_measure_facade._COUNTED_META_MARKER_CURSOR,)).fetchone()
        cursor = row[0] if row else ""
        base = _measure_facade.CLAUDE_DIR / "projects"
        paths = sorted(str(p) for p in base.glob("*/*.jsonl")) if base.exists() else []
        # Pre-era files only: anything (re)written after the era start belongs
        # to the ledger era and is already covered by savings/compression rows.
        era_epoch = _measure_facade.datetime.fromisoformat(
            _measure_facade._COUNTED_LEDGER_ERA_START).astimezone(_measure_facade.timezone.utc).timestamp()
        started = _measure_facade.time.monotonic()
        tier_data = _measure_facade.PRICING_TIERS.get(_measure_facade._load_pricing_tier(), _measure_facade.PRICING_TIERS["anthropic"])
        processed_any = False
        last = cursor
        for sp in paths:
            if sp <= cursor:
                continue
            if max_files is not None and stats["files_scanned"] >= max_files:
                break
            if deadline_seconds is not None and _measure_facade.time.monotonic() - started > deadline_seconds:
                break
            last = sp
            stats["files_scanned"] += 1
            processed_any = True
            try:
                path = _measure_facade.Path(sp)
                sid = path.stem
                old_rows = old_by_session.get(sid, [])
                if path.stat().st_mtime >= era_epoch and not old_rows:
                    continue
                proof = _measure_facade._counted_file_proof(path, [
                    _measure_facade._counted_pricing_detail(r.get("pricing_detail")).get("file_size", 0) for r in old_rows])
            except OSError:
                try:
                    stat = path.stat()
                    attempted = {"mtime_ns": stat.st_mtime_ns, "file_size": stat.st_size}
                except OSError:
                    attempted = None
                for old in old_rows:
                    _measure_facade._counted_mark_incomplete(conn, old["event_key"], _measure_facade._counted_pricing_detail(old.get("pricing_detail")), attempted)
                continue
            turns, compacts, markers = _measure_facade._counted_walk_transcript(sp, collect_markers=True)
            if (not turns or not markers or path.stat().st_mtime_ns != proof["mtime_ns"]
                    or path.stat().st_size != proof["file_size"]):
                for old in old_rows:
                    _measure_facade._counted_mark_incomplete(conn, old["event_key"], _measure_facade._counted_pricing_detail(old.get("pricing_detail")), proof)
                continue
            # Marker keys use an index. A replacement can move an old marker
            # to a new index, so require continuity for the entire session
            # before admitting any new keys, not just for existing rows.
            continuous_session = True
            for old in old_rows:
                detail = _measure_facade._counted_pricing_detail(old.get("pricing_detail"))
                size = detail.get("file_size")
                if size is not None:
                    continuous = proof["prefixes"].get(size) == detail.get("file_sha256")
                else:
                    # A row stored without an mtime is judged by the identity
                    # recomputation below alone; a recorded mtime must match.
                    continuous = (old["transcript_mtime"] is None
                                  or old["transcript_mtime"] == path.stat().st_mtime)
                    try:
                        index = int(old["event_key"].rsplit(":", 1)[1])
                        marker_dt, marker_tokens = markers[index]
                        marker_dt = marker_dt or turns[0][0]
                        marker_tokens = int(marker_tokens or _measure_facade._COUNTED_MARKER_MEDIAN_TOKENS)
                        previous = _measure_facade._counted_score_detail(turns, compacts, marker_dt, marker_tokens, tier_data)
                        continuous = (continuous and old["event_ts"] == marker_dt.isoformat()
                                      and old["tokens"] == marker_tokens and previous["observed"]
                                      and old["reread_tokens"] == previous["reread_tokens"]
                                      and old["turns_counted"] == previous["turns_counted"])
                    except (ValueError, IndexError, TypeError):
                        continuous = False
                if not continuous:
                    continuous_session = False
            if not continuous_session:
                for old in old_rows:
                    _measure_facade._counted_mark_incomplete(conn, old["event_key"], _measure_facade._counted_pricing_detail(old.get("pricing_detail")), proof)
                continue
            first_month = turns[0][0].strftime("%Y-%m")
            if first_month >= _measure_facade._COUNTED_LEDGER_ERA_START[:7]:
                continue
            sid = _measure_facade.Path(sp).stem
            for i, (m_dt, m_tok) in enumerate(markers):
                tok = int(m_tok or _measure_facade._COUNTED_MARKER_MEDIAN_TOKENS)
                ev_dt = m_dt or turns[0][0]
                score = _measure_facade._counted_score_detail(
                    turns, compacts, ev_dt, tok, tier_data)
                key = f"mk:{sid}:{i}"
                if _measure_facade._counted_store_score(conn, key, "mk", sid, ev_dt.isoformat(), tok,
                                        score, path.stat().st_mtime, proof, old=stored.get(key),
                                        magnitude_basis="sized_marker" if m_tok else "estimated_marker",
                                        identity_verified=True):
                    stats["rows_written"] += 1
        if paths and last == paths[-1]:
            conn.execute("INSERT OR REPLACE INTO token_optimizer_meta (key, value) "
                         "VALUES (?, '1')", (_measure_facade._COUNTED_META_MARKER_DONE,))
            stats["done"] = True
        elif not paths:
            conn.execute("INSERT OR REPLACE INTO token_optimizer_meta (key, value) "
                         "VALUES (?, '1')", (_measure_facade._COUNTED_META_MARKER_DONE,))
            stats["done"] = True
        else:
            conn.execute("INSERT OR REPLACE INTO token_optimizer_meta (key, value) "
                         "VALUES (?, ?)", (_measure_facade._COUNTED_META_MARKER_CURSOR, last))
        conn.commit()
        if not quiet and stats["rows_written"]:
            print(f"[Token Optimizer] counted-cumulative marker backfill: "
                  f"{stats['files_scanned']} file(s), {stats['rows_written']} marker row(s).")
    except Exception:
        pass
    return stats


def _purge_counted_without_transcripts(conn):
    """Compatibility no-op: quarantine unverifiable prices without erasing history."""
    _measure_facade = _compat.facade()
    return 0


def _counted_backfill_all(conn, quiet=None):
    """Finish refresh attempts; unverifiable history stays quarantined."""
    _measure_facade = _compat.facade()
    stats = _measure_facade._update_counted_cumulative(
        conn, max_sessions=10**9, deadline_seconds=None, quiet=quiet)
    marker_total = {"files_scanned": 0, "rows_written": 0, "done": False}
    while True:
        marker = _measure_facade._counted_marker_backfill(
            conn, max_files=500, deadline_seconds=None, quiet=quiet)
        marker_total["files_scanned"] += int(marker.get("files_scanned", 0) or 0)
        marker_total["rows_written"] += int(marker.get("rows_written", 0) or 0)
        marker_total["done"] = bool(marker.get("done"))
        if marker.get("done") or not marker.get("files_scanned"):
            break
    stats.update({"marker_files_scanned": marker_total["files_scanned"],
                  "marker_rows_written": marker_total["rows_written"],
                  "marker_backfill_done": marker_total["done"],
                  "rows_removed_no_transcript": _measure_facade._purge_counted_without_transcripts(conn)})
    stats["pricing_coverage"] = _measure_facade._counted_rollup_rows(_measure_facade._counted_stored_rows(conn).values(), conn)
    return stats


def _counted_session_cache_mix(conn, session_uuid, tier_data, memo, rate_key=None):
    """[(cache_read_tokens, usd_per_mtok or None)] for one stored session.

    `rate_key` picks the card rate (cache_read by default, input for the
    initial-removal floor); weights are the same either way.

    Weights come from the session's recorded per-model cache-read tokens. A
    model without an exact rate card, or whose card depends on the size of each
    request, has no rate here and its share stays unpriced. None when the
    session has no usable per-model record."""
    _measure_facade = _compat.facade()
    memo_key = (session_uuid, rate_key)
    if memo_key in memo:
        return memo[memo_key]
    mix = None
    try:
        row = conn.execute(
            "SELECT model_usage_breakdown_json FROM session_log "
            "WHERE session_uuid = ? ORDER BY id DESC LIMIT 1", (session_uuid,)).fetchone()
        breakdown = _measure_facade.json.loads(row[0]) if row and row[0] else None
    except (_measure_facade.sqlite3.Error, TypeError, ValueError):
        breakdown = None
    if isinstance(breakdown, dict):
        mix = _measure_facade._counted_pricing_v5.session_cache_mix_terms(
            breakdown, lambda model: _measure_facade._counted_exact_card(model, tier_data), rate_key)
    memo[memo_key] = mix
    return mix


def _counted_rollup_rows(rows, conn=None):
    """Only per-row verified prices are eligible; retain all stored token evidence.

    A stored row without a current per-turn price keeps its signed repeat-read
    token count. With `conn`, that count is priced at the cache-read rate of the
    models its session recorded (token-weighted, exact cards only) and reported
    separately from per-turn prices; its initial removal is priced at the same
    models' input rate, the disclosed floor used when the billed class is not
    known. Any share without a rate stays unpriced."""
    _measure_facade = _compat.facade()
    out = dict.fromkeys(_measure_facade._COUNTED_COVERAGE_KEYS, 0)
    out.update(events=0, removed_tokens=0, reread_tokens=0, oneshot_usd=0.0,
               oneshot_class_premium_usd=0.0, reread_usd=0.0, legacy_unverified_events=0,
               reread_miss_turns=0, reread_miss_premium_usd=0.0,
               unverified_history_events=0, estimated_magnitude_events=0,
               refresh_incomplete_events=0, reread_session_mix_priced_tokens=0,
               session_mix_priced_events=0)
    tier_data = _measure_facade.PRICING_TIERS.get(_measure_facade._load_pricing_tier(), _measure_facade.PRICING_TIERS["anthropic"])
    mix_memo = {}
    for row in rows:
        out["events"] += 1
        tokens, rereads = int(row["tokens"] or 0), int(row["reread_tokens"] or 0)
        out["removed_tokens"] += tokens
        out["reread_tokens"] += rereads
        detail = _measure_facade._counted_pricing_detail(row.get("pricing_detail"))
        current = (detail.get("version") == _measure_facade._COUNTED_PRICING_VERSION
                   and all(isinstance(detail.get(k), int) and detail[k] >= 0 for k in _measure_facade._COUNTED_COVERAGE_KEYS))
        if current:
            out["oneshot_usd"] += float(row["oneshot_usd"] or 0)
            out["reread_usd"] += float(row["reread_usd"] or 0)
            out["oneshot_class_premium_usd"] += float(detail.get("oneshot_class_premium_usd") or 0)
            out["reread_miss_turns"] += int(detail.get("reread_miss_turns") or 0)
            out["reread_miss_premium_usd"] += float(detail.get("reread_miss_premium_usd") or 0)
            for key in _measure_facade._COUNTED_COVERAGE_KEYS:
                out[key] += detail[key]
        else:
            out["legacy_unverified_events"] += 1
            mix = (_measure_facade._counted_session_cache_mix(conn, row.get("session_uuid"), tier_data, mix_memo)
                   if conn is not None and rereads else None)
            input_mix = (_measure_facade._counted_session_cache_mix(conn, row.get("session_uuid"), tier_data,
                                                    mix_memo, rate_key="input")
                         if conn is not None and tokens else None)
            for kind, amount, terms in (("oneshot", tokens, input_mix), ("reread", rereads, mix)):
                if terms:
                    usd, priced, amount = _measure_facade._counted_pricing_v5.session_mix_price(amount, terms)
                    out[kind + "_usd"] += usd
                    if kind == "oneshot":
                        out["oneshot_floor_events"] += 1
                        out["oneshot_floor_tokens"] += priced
                    else:
                        out["reread_session_mix_priced_tokens"] += priced
                        out["session_mix_priced_events"] += 1
                        out["priced_reread_turns"] += max(1, int(row.get("turns_counted") or 0))
                out[kind + "_unpriced_" + ("credit" if amount >= 0 else "debit") + "_tokens"] += abs(amount)
        if not current and row["transcript_mtime"] is None and row["source"] != "mk":
            out["unverified_history_events"] += 1
        if (detail.get("magnitude_basis") == "estimated_marker"
                or (row["source"] == "mk" and not current)):
            out["estimated_magnitude_events"] += 1
        out["refresh_incomplete_events"] += bool(detail.get("refresh_incomplete"))
    out["oneshot_usd"] = round(out["oneshot_usd"], 6)
    out["oneshot_class_premium_usd"] = round(out["oneshot_class_premium_usd"], 6)
    out["reread_miss_premium_usd"] = round(out["reread_miss_premium_usd"], 6)
    out["reread_usd"] = round(out["reread_usd"], 6)
    out["total_usd"] = round(out["oneshot_usd"] + out["reread_usd"], 6)
    return _measure_facade._counted_coverage_totals(out)


def _counted_row_awaits_rescore(row):
    """True for a stored row priced under an older version that no pass has
    yet attempted to re-score under the current one."""
    _measure_facade = _compat.facade()
    detail = _measure_facade._counted_pricing_detail(row.get("pricing_detail"))
    return (detail.get("version") != _measure_facade._COUNTED_PRICING_VERSION
            and detail.get("attempted_version") != _measure_facade._COUNTED_PRICING_VERSION)


def _counted_cumulative_summary(days=None, now=None):
    """Cheap SELECT-only rollup of counted_reread for the Counted-to-date card.

    NEVER walks transcripts (the collect pass owns that); a dashboard regen
    reads stored rows only. With days, select removals made in that period;
    their later rereads stay attributed to the removal (not to the reread date).
    This is an event cohort, not a claim of dollars accrued inside an exact window.
    Fail-open to {"available": False}."""
    _measure_facade = _compat.facade()
    out = {"available": False}
    if days is not None and _measure_facade.detect_runtime() != "claude":
        return out
    if not _measure_facade.TRENDS_DB.exists():
        return out
    try:
        end = (now or _measure_facade.datetime.now(_measure_facade.timezone.utc)).astimezone(_measure_facade.timezone.utc)
        start = end - _measure_facade.timedelta(days=max(1, int(days))) if days is not None else None
        conn = _measure_facade._init_trends_db()
        try:
            excluded = set()
            if _measure_facade._ensure_counted_views(conn):
                excluded.update(r[0] for r in conn.execute(
                    "SELECT 'se:'||id FROM main.savings_events EXCEPT "
                    "SELECT 'se:'||id FROM counted_savings_events"))
                excluded.update(r[0] for r in conn.execute(
                    "SELECT 'ce:'||id FROM main.compression_events EXCEPT "
                    "SELECT 'ce:'||id FROM counted_compression_events"))
            rows = []
            stored = _measure_facade._counted_stored_rows(conn)
            candidates = _measure_facade._counted_candidate_events(conn)
            orphaned = _measure_facade._counted_pricing_v5.orphaned_keys(stored.values(), {c[0] for c in candidates}, conn)
            for row in stored.values():
                if row["event_key"] in excluded or row["event_key"] in orphaned:
                    continue
                if start is not None:
                    if row["source"] == "mk":
                        try:
                            value = _measure_facade.datetime.fromisoformat(str(row["event_ts"]).replace("Z", "+00:00"))
                            stamp = (value if value.tzinfo else value.replace(tzinfo=_measure_facade.timezone.utc)).timestamp()
                        except (ValueError, TypeError, OSError):
                            continue
                    else:
                        value = _measure_facade._counted_event_utc(row["event_ts"])
                        if value is None:
                            continue
                        stamp = value.replace(tzinfo=_measure_facade.timezone.utc).timestamp()
                    if not start.timestamp() <= stamp <= end.timestamp():
                        continue
                rows.append(row)
            missing_by_month = {}
            for key, _uuid, ts, tokens, _cost, _model in candidates:
                # A media archive without a row has no measured size yet; it
                # adds nothing until the collect pass sizes it from its transcript.
                if key in stored or key.startswith("md:"):
                    continue
                value = _measure_facade._counted_event_utc(ts)
                if start is not None and (value is None or not start.timestamp() <=
                        value.replace(tzinfo=_measure_facade.timezone.utc).timestamp() <= end.timestamp()):
                    continue
                month = value.strftime("%Y-%m") if value else None
                missing_by_month.setdefault(month, []).append(int(tokens or 0))

            def add_missing(coverage, amounts):
                _measure_facade = _compat.facade()
                coverage["unmeasured_events"] = len(amounts)
                coverage["unmeasured_removed_tokens"] = sum(amounts)
                coverage["unmeasured_removed_credit_tokens"] = sum(n for n in amounts if n >= 0)
                coverage["unmeasured_removed_debit_tokens"] = sum(-n for n in amounts if n < 0)
                return _measure_facade._counted_coverage_totals(coverage)

            totals = add_missing(_measure_facade._counted_rollup_rows(rows, conn),
                                 [n for values in missing_by_month.values() for n in values])
            # Rows stored under an older pricing version that the collect pass
            # has not yet tried to re-score. Their price in the subtotal is
            # provisional, so the card reports re-measurement instead of a final
            # figure. Rows with no session can never be re-scored and are left
            # to the coverage notes.
            awaiting = [row for row in rows if row.get("session_uuid")
                        and _measure_facade._counted_row_awaits_rescore(row)]
            remeasuring_sessions = len({row["session_uuid"] for row in awaiting})
            media_rows = [row for row in rows if row["source"] == "md"]
            media_priced = [row for row in media_rows if int(row["tokens"] or 0) > 0]
            media_note = ""
            if media_rows:
                media_note = (
                    f"{len(media_priced):,} of {len(media_rows):,} screenshot and image archives "
                    "removed their image from context; each is valued at the image's token cost "
                    "(width x height / 750) minus the text left in its place, with the initial "
                    "removal at the input rate. The other "
                    f"{len(media_rows) - len(media_priced):,} left the image in context or stated "
                    "no image size, so they count $0.")
            by_month = {}
            for row in rows:
                by_month.setdefault(row["event_month"], []).append(row)
            monthly_coverage = {m: add_missing(_measure_facade._counted_rollup_rows(by_month.get(m, []), conn),
                                               missing_by_month.get(m, []))
                                for m in sorted(by_month.keys() | missing_by_month.keys(),
                                                key=lambda month: month or "")}
            try:
                done = conn.execute(
                    "SELECT value FROM token_optimizer_meta WHERE key = ?",
                    (_measure_facade._COUNTED_META_MARKER_DONE,)).fetchone()
            except _measure_facade.sqlite3.Error:
                # A fresh DB may not have the meta table yet (created lazily by
                # the backfill); its absence must not blank the whole card.
                done = None
        finally:
            conn.close()
        first_month = min((month for month in monthly_coverage if month), default=None)
        computed_at = max((row["computed_at"] for row in rows if row["computed_at"]), default=None)
        return {
            **totals,
            "available": True,
            "attribution": "removal_time",
            "period_start": start.isoformat() if start else None,
            "period_end": end.isoformat(),
            "window_start": (first_month + "-01") if first_month else None,
            "months": {m: round(v["total_usd"], 2) for m, v in monthly_coverage.items()},
            "monthly_coverage": monthly_coverage,
            "marker_backfill_done": bool(done and done[0] == "1"),
            "computed_at": computed_at,
            "remeasuring": remeasuring_sessions > 0,
            "remeasuring_sessions": remeasuring_sessions,
            "remeasuring_events": len(awaiting),
            "media_archive_events": len(media_rows),
            "media_priced_events": len(media_priced),
            "media_removed_tokens": sum(int(row["tokens"] or 0) for row in media_priced),
            "media_note": media_note,
            "orphaned_rows_excluded": len(orphaned),
            # Same construction as the estimated tiers: the method is counted,
            # the one modeling choice (trajectory held fixed) is disclosed on
            # the card.
            "method": "removals x deduped turns to next real compaction, "
                      "exact known per-turn cache-read rates; unpriced credits/debits "
                      "and unverifiable historical prices reported separately; "
                      "older unsized markers retain estimated magnitudes",
        }
    except (_measure_facade.sqlite3.Error, OSError, TypeError, ValueError):
        return out


def _dashboard_savings_data(days=None, include_billing_mode=None, fail_open=None):
    """Build one savings payload with counted-to-date as the source of truth."""
    _measure_facade = _compat.facade()
    source_failed = False
    try:
        savings_data = _measure_facade._get_merged_savings(days=days)
    except Exception:
        if fail_open:
            source_failed = True
            savings_data = {"total_tokens": 0, "total_cost_usd": 0.0,
                            "by_category": {}}
        else:
            try:
                savings_data = _measure_facade._get_savings_summary(days=days)
            except Exception:
                savings_data = {"total_tokens": 0, "total_cost_usd": 0.0,
                                "by_category": {}}
    if not isinstance(savings_data, dict):
        savings_data = {"total_tokens": 0, "total_cost_usd": 0.0,
                        "by_category": {}}
    savings_data.pop("mcp_cap_estimated", None)
    if source_failed:
        return savings_data
    try:
        from . import cost_per_message_trend
        conn = _measure_facade._open_trends_db_readonly(timeout=1.0)
        try: savings_data["cost_per_message_trend"] = cost_per_message_trend.query(conn)
        finally: conn.close()
    except Exception:
        savings_data["cost_per_message_trend"] = []
    savings_data["skill_listing_budget"] = structural_report.skill_listing_budget(days or 30)
    try:
        savings_data["since_install"] = _measure_facade._savings_since_install()
    except Exception:
        pass
    try:
        # Always replace any stale merged-view copy. The card must reconcile to
        # the full counted_reread table, not to a partial generation payload.
        savings_data["counted_cumulative"] = _measure_facade._counted_cumulative_summary()
    except Exception:
        savings_data["counted_cumulative"] = {"available": False}
    try:
        savings_data["counted_period"] = _measure_facade._counted_cumulative_summary(days=days)
    except Exception:
        savings_data["counted_period"] = {"available": False}
    _compat.facade()._headline.attach(savings_data, days)
    _measure_facade._apply_counted_oneshot_pricing(savings_data, savings_data["counted_period"])
    savings_data["action_savings"] = _measure_facade._action_savings_components(
        savings_data, savings_data["counted_period"])
    if include_billing_mode:
        try:
            savings_data["billing_mode"] = _measure_facade.keepwarm_billing_mode()
        except Exception:
            pass
    return savings_data


def _net_realized_savings(rows, reprice_deltas):
    """Net savings_events aggregates into the REALIZED figure.

    The one netting rule shared by the window headline (_get_savings_summary)
    and the status-bar per-session / per-day buckets (_realized_savings_buckets),
    so the two can never drift. *rows* is an iterable of
    (event_type, events, tokens_saved, cost_saved_usd); *reprice_deltas* maps
    event_type -> USD correction from _session_model_reprice for the SAME rows.

    Returns (by_category, total_tokens, total_cost, total_events, popped,
    repriced_to_session_mix) where *popped* holds the entries relocated out of
    the realized total (setup_optimization, mcp_cap, hint_followed,
    verbosity_steer, resume_lean, checkpoint_restore, verbosity_steer_measured),
    each an entry dict or None.
    """
    _measure_facade = _compat.facade()
    by_category = {}
    total_tokens = 0
    total_cost = 0.0
    total_events = 0
    for event_type, cnt, tok, cost in rows:
        by_category[event_type] = {
            "events": cnt,
            "tokens_saved": tok or 0,
            "cost_saved_usd": round(cost or 0.0, 4),
        }
        total_tokens += tok or 0
        total_cost += cost or 0.0
        total_events += cnt

    # Apply the model-mix repair BEFORE every relocation and netting rule
    # below, so the pops (setup_optimization, mcp_cap, hint_followed,
    # verbosity_steer) and the tool_archive re-expand netting all operate on
    # corrected dollars and the realized total stays internally consistent.
    # Token COUNTS are never touched -- this is a rate correction only.
    repriced_to_session_mix = False
    for _event_type, _delta in reprice_deltas.items():
        _entry = by_category.get(_event_type)
        if not _entry:
            continue
        _entry["cost_saved_usd"] = round(
            float(_entry.get("cost_saved_usd", 0.0) or 0.0) + _delta, 4)
        total_cost += _delta
        repriced_to_session_mix = True

    # Relocated OUT of the realized total, each into its own non-realized line:
    #   setup_optimization (A1): one-time prefix trim; structural_savings is the
    #     ongoing model of the SAME trim, so the one-shot is never summed with it.
    #   mcp_cap (A3): hardcoded-ratio estimate of Claude Code's own truncation.
    #   hint_followed (U-G), verbosity_steer: observed trigger, estimated size.
    #   resume_lean, checkpoint_restore: counterfactual credits (the cold
    #     reload / working-set re-read never happened, so nothing was metered).
    #   verbosity_steer_measured (savings-truth U5): a before/after of output
    #     with no control. Against matched un-nudged replies it is not a
    #     reduction, so both booked rows and the control study stay diagnostic.
    # savings_events has no tier column: this pop IS the tier=estimated stamp.
    popped = {}
    #   codemap_lookup: a lookup answered instead of a Read; the avoided read is
    #     a counterfactual (file not read within the follow-up window).
    for name in ("setup_optimization", "mcp_cap", "hint_followed", "verbosity_steer",
                 "resume_lean", "checkpoint_restore", "verbosity_steer_measured",
                 "codemap_lookup"):
        entry = popped[name] = by_category.pop(name, None)
        if entry:
            total_tokens -= int(entry.get("tokens_saved", 0) or 0)
            total_cost -= float(entry.get("cost_saved_usd", 0.0) or 0.0)
            total_events -= int(entry.get("events", 0) or 0)

    # Bash archive expansions reverse Bash output savings, not MCP replacements.
    bash = by_category.get("bash_archive_reexpand")
    if bash:
        total_tokens -= 2 * int(bash.get("tokens_saved") or 0)
        total_cost -= 2 * float(bash.get("cost_saved_usd") or 0)
        total_events -= int(bash.get("events") or 0)
        bash["tokens_saved"] = -int(bash.get("tokens_saved") or 0)
        bash["cost_saved_usd"] = -float(bash.get("cost_saved_usd") or 0)

    # B6: net tool-archive re-expansions out of the tool_archive credit. A
    # re-popped result didn't stay collapsed, so its eager credit is reversed
    # even if the result is negative. tool_archive_reexpand is a DEBIT, never its own savings
    # line, so it is removed from by_category and its spurious positive
    # contribution backed out of the totals. The remaining tool_archive figure
    # IS the measured progressive-disclosure win (collapsed and stayed so).
    reexpand = by_category.pop("tool_archive_reexpand", None)
    if reexpand:
        r_tok = int(reexpand.get("tokens_saved", 0) or 0)
        r_cost = float(reexpand.get("cost_saved_usd", 0.0) or 0.0)
        r_events = int(reexpand.get("events", 0) or 0)
        ta = by_category.setdefault("tool_archive", {"events": 0, "tokens_saved": 0, "cost_saved_usd": 0.0})
        c_tok = int(ta["tokens_saved"]) if ta else 0
        c_cost = float(ta["cost_saved_usd"]) if ta else 0.0
        net_tok = c_tok - r_tok
        net_cost = c_cost - r_cost
        if ta:
            ta["tokens_saved"] = net_tok
            ta["cost_saved_usd"] = round(net_cost, 4)
        # Back out the debit's own positive AND the reversed over-credit.
        total_tokens += (net_tok - c_tok) - r_tok
        total_cost += (net_cost - c_cost) - r_cost
        total_events -= r_events  # re-expand rows are not savings events

    return (by_category, total_tokens, total_cost, total_events, popped,
            repriced_to_session_mix)


def _get_savings_summary(days=None, since=None):
    """Query savings events and return a summary dict.

    When *since* is a non-empty ISO-8601 timestamp string, it is used directly
    as the cutoff (``timestamp >= since``) instead of the rolling ``now - days``
    cutoff. This allows callers to align the lookback window to an external
    boundary (e.g. the subscription limit reset). When *since* is None / empty,
    the rolling cutoff is computed from *days* as before.
    """
    _measure_facade = _compat.facade()
    try:
        conn = _measure_facade._init_trends_db()
        try:
            if since:
                cutoff = since
            else:
                cutoff = (_measure_facade.datetime.now() - _measure_facade.timedelta(days=days)).isoformat()
            table = _measure_facade._counted_table(conn, "savings_events")
            event_expr = _measure_facade._counted_cap.reexpand_type() if "detail" in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")} else "event_type"
            rows = conn.execute(
                "SELECT " + event_expr + ", COUNT(*) as cnt, SUM(tokens_saved) as tok, SUM(cost_saved_usd) as cost "
                "FROM " + table + " WHERE timestamp >= ? GROUP BY 1 ORDER BY tok DESC",
                (cutoff,),
            ).fetchall()
            # Model-mix repair, same connection and same cutoff so the corrected
            # rows are exactly the rows summed above. Self-degrading; see
            # _session_model_reprice.
            unpriced_reprice = {}
            reprice_deltas, repriced_tokens, unmeasured_tokens = _measure_facade._session_model_reprice(
                conn, cutoff, unpriced_out=unpriced_reprice)
            tier_data = _measure_facade.PRICING_TIERS.get(_measure_facade._load_pricing_tier(), _measure_facade.PRICING_TIERS["anthropic"])
            resume_fix = _measure_facade._counted_pricing_v5.resume_lean_reprice(conn, _measure_facade._counted_table(conn, "savings_events"), cutoff,
                                                lambda model: _measure_facade._counted_exact_card(model, tier_data))
        finally:
            conn.close()

        (by_category, total_tokens, total_cost, total_events, popped,
         repriced_to_session_mix) = _measure_facade._net_realized_savings(rows, reprice_deltas)
        one_time = popped["setup_optimization"]
        hint_followed_est = popped["hint_followed"]
        codemap_lookup_est = popped["codemap_lookup"]
        verbosity_steer_est = popped["verbosity_steer"]
        resume_lean_est = popped["resume_lean"]
        if resume_lean_est and resume_fix["repriced_events"]:  # exact model card, not the family card
            resume_lean_est["cost_saved_usd"] = round(max(0.0, resume_lean_est["cost_saved_usd"] + resume_fix["delta_usd"]), 4)
            resume_lean_est["exact_card_reprice"] = resume_fix
        checkpoint_restore_est = popped["checkpoint_restore"]
        verbosity_output_est = _measure_facade._output_reread.summary_pool(popped["verbosity_steer_measured"])

        daily_avg = total_cost / days if days > 0 else 0.0

        return {
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 4),
            "total_events": total_events,
            "by_category": by_category,
            "daily_avg_usd": round(daily_avg, 4),
            "period_days": days,
            # A1: one-time first-trim (compare). Reported separately, NEVER summed
            # into the realized total (structural_savings is the ongoing model).
            "one_time_setup": one_time,
            # U-G: observed-trigger / estimated-magnitude avoided-search from
            # proactive hints. Estimated tier, never in the realized total.
            "hint_followed_estimated": hint_followed_est,
            # Code map lookups: observed lookup, counterfactual avoided read. Estimated tier.
            "codemap_lookup_estimated": codemap_lookup_est,
            # Verbosity-steer: estimated output-token reduction from conciseness
            # nudges. Estimated tier, never in the realized total. Surfaced so
            # _estimate_before_after_savings can add it to the transformation
            # headline as a separate estimated addback.
            "verbosity_steer_estimated": verbosity_steer_est,
            # Cold-resume avoided (counterfactual trigger AND inferred
            # magnitude from the target's last-turn context). Estimated
            # tier, never in the realized total.
            "resume_lean_estimated": resume_lean_est,
            # Checkpoint-restore avoided re-reads: the trigger (a restore
            # happened) is observed, but the credited working set is inferred
            # from this session's reads -- the avoided re-read never happened,
            # so nothing was metered. Estimated tier, never realized.
            "checkpoint_restore_estimated": checkpoint_restore_est,
            # Nudged replies and their later rereads, net of matched un-nudged
            # replies (U5). Estimated tier; the booked rows are reference only.
            "verbosity_output_estimated": verbosity_output_est,
            # True when at least one event's dollars moved because its event-time
            # model stamp disagreed with its own session's recorded model mix. The
            # figure is then a rate ALLOCATION, not a pure event-time meter, so
            # every consumer must label it "estimated" (see _window_overage_usd).
            "repriced_to_session_mix": repriced_to_session_mix,
            "reprice_detail": {
                "repriced_tokens": int(repriced_tokens),
                # NOT MEASURED: no joinable session row, so no mix to price
                # against. These keep their event-time price; nothing is invented.
                "unmeasured_tokens": int(unmeasured_tokens),
                # Saved tokens that ran on models with no exact rate card: counted
                # as tokens, priced at zero.
                "unpriced_tokens": int(round(unpriced_reprice.get("tokens", 0))),
                "delta_usd": round(sum(reprice_deltas.values()), 4),
                "basis": "session_log.model_usage_json token-weighted mix",
            },
        }
    except Exception:
        return {
            "total_tokens": 0,
            "total_cost_usd": 0.0,
            "total_events": 0,
            "by_category": {},
            "daily_avg_usd": 0.0,
            "period_days": days,
            "one_time_setup": None,
            "hint_followed_estimated": None,
            "codemap_lookup_estimated": None,
            "verbosity_steer_estimated": None,
            "resume_lean_estimated": None,
            "checkpoint_restore_estimated": None,
            "verbosity_output_estimated": None,
            "repriced_to_session_mix": False,
            "reprice_detail": None,
        }


def _session_cost_norm(text):
    """Case- and form-insensitive text for matching (Hebrew/any script safe)."""
    _measure_facade = _compat.facade()
    import unicodedata  # noqa: PLC0415 - only this command needs it
    return unicodedata.normalize("NFKC", str(text or "")).casefold()


def _session_cost_resolve_date(word, today=None):
    """'today' / 'yesterday' / YYYY-MM-DD -> YYYY-MM-DD, else None."""
    _measure_facade = _compat.facade()
    today = today or _measure_facade.datetime.now().date()
    w = _measure_facade._session_cost_norm(word).strip()
    if w == "today":
        return today.strftime("%Y-%m-%d")
    if w == "yesterday":
        return (today - _measure_facade.timedelta(days=1)).strftime("%Y-%m-%d")
    if _measure_facade._SESSION_COST_DATE_RX.match(w):
        try:
            return _measure_facade.datetime.strptime(w, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def _session_cost_parse_args(argv):
    """Parse session-cost arguments. Returns (opts, error-or-None)."""
    _measure_facade = _compat.facade()
    opts = {"words": [], "id": None, "date": None, "days": None, "limit": 5, "json": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            opts["json"] = True
        elif a in ("--id", "--date", "--days", "--limit"):
            if i + 1 >= len(argv):
                return opts, f"{a} needs a value"
            val = argv[i + 1]
            i += 1
            if a == "--id":
                opts["id"] = val.strip().lower()
            elif a == "--date":
                d = _measure_facade._session_cost_resolve_date(val)
                if d is None:
                    return opts, f"--date takes YYYY-MM-DD, today or yesterday (got {val!r})"
                opts["date"] = d
            else:
                try:
                    n = int(val)
                except ValueError:
                    return opts, f"{a} takes a whole number (got {val!r})"
                if n < 1:
                    return opts, f"{a} must be at least 1"
                opts["days" if a == "--days" else "limit"] = n
        elif a.startswith("--"):
            return opts, f"unknown option {a}"
        else:
            opts["words"].append(a)
        i += 1
    # Plain words may carry the date or the id themselves.
    words = []
    for raw in opts["words"]:
        for w in _measure_facade.re.split(r"[\s,?!.;:]+", raw):
            w = w.strip("\"'()[]{}<>")
            if not w:
                continue
            d = _measure_facade._session_cost_resolve_date(w)
            if d is not None:
                opts["date"] = opts["date"] or d
                continue
            if _measure_facade._SESSION_COST_ID_RX.match(w) and not opts["id"]:
                opts["id"] = w.lower()
                continue
            n = _measure_facade._session_cost_norm(w)
            if n not in _measure_facade._SESSION_COST_STOPWORDS:
                words.append(n)
    opts["words"] = words
    return opts, None


def _session_cost_open_db(db_path):
    """Open trends.db without writing a byte or blocking a writer.

    trends.db runs in WAL mode. With no writer attached (no -wal, or an empty
    one) every row is in the main file, so immutable=1 reads it with no locks
    and creates nothing; a plain mode=ro open would create -wal/-shm files.
    With a live writer (-wal holding data and its -shm present), mode=ro
    joins the shared read protocol: it sees every committed row and a WAL
    reader never blocks a writer. If that fails (an exclusive lock, or a WAL
    with no -shm to share), immutable=1 still answers from the main file,
    which may miss the newest rows. Returns (conn, mode) or (None, error)."""
    _measure_facade = _compat.facade()
    db_path = _measure_facade.Path(db_path).resolve()
    uri_base = db_path.as_uri()
    try:
        wal = _measure_facade.Path(str(db_path) + "-wal")
        live = wal.exists() and wal.stat().st_size > 0
        shm_ok = _measure_facade.Path(str(db_path) + "-shm").exists()
    except OSError:
        live, shm_ok = True, False
    if not live:
        attempts = (("quiescent", "?mode=ro&immutable=1"),)
    elif shm_ok:
        attempts = (("shared-read", "?mode=ro"), ("snapshot", "?mode=ro&immutable=1"))
    else:
        attempts = (("snapshot", "?mode=ro&immutable=1"),)
    last_err = None
    for mode, query in attempts:
        conn = None
        try:
            conn = _measure_facade.sqlite3.connect(uri_base + query, uri=True, timeout=1.5)
            conn.execute("PRAGMA query_only=1")
            conn.execute("SELECT 1 FROM session_log LIMIT 1").fetchall()
            conn.row_factory = _measure_facade.sqlite3.Row
            return conn, mode
        except _measure_facade.sqlite3.Error as exc:
            last_err = exc
            if conn is not None:
                try:
                    conn.close()
                except _measure_facade.sqlite3.Error:
                    pass
    return None, str(last_err)


def _session_cost_haystack(row):
    """Every text field a plain-words question can match: topic, slug, project
    (raw and cleaned) and the transcript's folder name."""
    _measure_facade = _compat.facade()
    project = row["project"] or ""
    parts = [row["topic"], row["slug"], project, _measure_facade._clean_project_name(project)]
    jp = row["jsonl_path"]
    if jp:
        parts.append(_measure_facade.Path(jp).parent.name)
    return _measure_facade._session_cost_norm(" ".join(str(p) for p in parts if p))


def _session_cost_word_hits(words, row):
    """How many of *words* match this row. A word of three characters or fewer
    must be a whole token ('tr' never matches 'trends'); longer words match
    anywhere (Hebrew prefixes attach to the word they modify)."""
    _measure_facade = _compat.facade()
    hay = _measure_facade._session_cost_haystack(row)
    tokens = set(_measure_facade.re.findall(r"\w+", hay))
    hits = 0
    for w in words:
        if (w in tokens) if len(w) <= 3 else (w in hay):
            hits += 1
    return hits


def _session_cost_row_id(row):
    _measure_facade = _compat.facade()
    uuid = row["session_uuid"]
    if uuid:
        return str(uuid)
    jp = row["jsonl_path"] or ""
    return _measure_facade.Path(jp).stem if jp else f"row-{row['id']}"


def _session_cost_components(priced, row, tier):
    """Per-model tokens and dollars from the same buckets the total came from
    (per-day usage when stored, else the parent breakdown, else the
    dominant-model estimate). Returns (models, tokens, cost_parts)."""
    _measure_facade = _compat.facade()
    buckets = {}
    if priced["has_daily"]:
        for day_mb in priced["daily_usage"].values():
            if isinstance(day_mb, dict):
                _measure_facade._merge_daily_usage(buckets, {"_": day_mb})
        buckets = buckets.get("_", {})
    elif priced["breakdown"]:
        buckets = {m: p for m, p in priced["breakdown"].items() if isinstance(p, dict)}
    else:
        inp = int(row["input_tokens"] or 0)
        cr = _measure_facade._safe_int(inp * (row["cache_hit_rate"] or 0))
        cc1 = int(row["cache_create_1h_tokens"] or 0)
        cc5 = int(row["cache_create_5m_tokens"] or 0)
        buckets = {priced["dom_model"]: {
            "fresh_input": max(0, inp - cr - cc1 - cc5), "cache_read": cr,
            "cache_create": cc1 + cc5, "cache_create_1h": cc1, "cache_create_5m": cc5,
            "output": int(row["output_tokens"] or 0)}}
    tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    parts = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}
    models = []
    for model, p in buckets.items():
        fi = int(p.get("fresh_input") or 0)
        out = int(p.get("output") or 0)
        cr = int(p.get("cache_read") or 0)
        cc = int(p.get("cache_create") or 0)
        has_split = "cache_create_1h" in p or "cache_create_5m" in p
        c1 = int(p.get("cache_create_1h") or 0) if has_split else None
        c5 = int(p.get("cache_create_5m") or 0) if has_split else None
        c_in = _measure_facade._get_model_cost(model, fi, 0, 0, 0, tier=tier)
        c_out = _measure_facade._get_model_cost(model, 0, out, 0, 0, tier=tier)
        c_cr = _measure_facade._get_model_cost(model, 0, 0, cr, 0, tier=tier)
        c_cw = _measure_facade._get_model_cost(model, 0, 0, 0, cc, tier=tier, cache_create_1h=c1, cache_create_5m=c5)
        for k, v in (("input", fi), ("output", out), ("cache_read", cr), ("cache_write", cc)):
            tokens[k] += v
        for k, v in (("input", c_in), ("output", c_out), ("cache_read", c_cr), ("cache_write", c_cw)):
            parts[k] += v
        models.append({"model": model, "priced": _measure_facade._is_priced_model(model, tier=tier),
                       "tokens": fi + out + cr + cc,
                       "cost_usd": round(c_in + c_out + c_cr + c_cw, 4)})
    models.sort(key=lambda m: ((_measure_facade := _compat.facade()), (-m["cost_usd"], -m["tokens"]))[1])
    return models, tokens, parts


def _session_cost_entry(row, tier, date_filter=None, current_id=None):
    """One matched session, priced, as a JSON-ready dict."""
    _measure_facade = _compat.facade()
    priced = _measure_facade._price_session_row(row, tier)
    total = float(priced["total_cost"])
    models, tokens, parts = _measure_facade._session_cost_components(priced, row, tier)
    subagents = _measure_facade._safe_json_dict(row["subagents_json"])
    sub_count = sum(int(v or 0) for v in subagents.values() if isinstance(v, (int, float)))
    main = min(float(priced["main_cost"]), total) if priced["has_daily"] else total
    sub_cost = max(0.0, total - main) if priced["has_daily"] else 0.0
    # Components come from the same buckets as the total; any gap is the
    # per-request long-context card on a row with no per-day data.
    adjustment = total - sum(parts.values())
    days = sorted({sl[0] for sl in priced["slices"] if sl[0]})
    sid = _measure_facade._session_cost_row_id(row)
    partial = False
    partial_reason = None
    if current_id and sid.startswith(current_id):
        partial, partial_reason = True, "this is the current session; it is collected again when it ends"
    else:
        try:
            jp = row["jsonl_path"]
            collected = row["collected_at"]
            if jp and collected and _measure_facade.os.path.exists(jp):
                if _measure_facade.datetime.fromtimestamp(_measure_facade.os.stat(jp).st_mtime) > _measure_facade.datetime.fromisoformat(str(collected)) + _measure_facade.timedelta(seconds=60):
                    partial, partial_reason = True, "the transcript grew after it was collected"
        except (OSError, ValueError, TypeError):
            pass
    entry = {
        "session_id": sid,
        "date": row["date"],
        "days": days,
        "topic": _measure_facade._strip_ansi(row["topic"] or "") or None,
        "slug": _measure_facade._strip_ansi(row["slug"] or "") or None,
        "project": _measure_facade._strip_ansi(_measure_facade._clean_project_name(row["project"])),
        "kind": "delegated" if row["is_sidechain"] else "session",
        "origin": row["origin"],
        "duration_minutes": round(float(row["duration_minutes"] or 0), 1),
        "api_calls": int(row["api_calls"] or 0),
        "models": models,
        "tokens": tokens,
        "cost_usd": {
            "input": round(parts["input"], 4),
            "output": round(parts["output"], 4),
            "cache_read": round(parts["cache_read"], 4),
            "cache_write": round(parts["cache_write"], 4),
            "total": round(total, 4),
            "main_thread": round(main, 4),
            "subagents": round(sub_cost, 4),
        },
        "subagents": {"count": sub_count, "types": subagents,
                      "included": bool(priced["has_daily"]) or not sub_count},
        "unpriced_tokens": int(priced["unpriced_tokens"]),
        "collected_at": row["collected_at"],
        "partial": partial,
        "partial_reason": partial_reason,
    }
    if abs(adjustment) >= 0.0001:
        entry["cost_usd"]["long_context_adjustment"] = round(adjustment, 4)
    entry["activity_complete"] = priced["activity_complete"]
    if date_filter:
        entry["cost_usd"]["on_date"] = round(
            sum(sl[1] for sl in priced["slices"] if sl[0] == date_filter), 4)
    return entry


def _session_cost_uncollected(id_prefix):
    """Transcripts on disk for an id that trends.db has no row for (read-only glob)."""
    _measure_facade = _compat.facade()
    if not id_prefix or _measure_facade.detect_runtime() != "claude":
        return []
    try:
        base = _measure_facade.CLAUDE_DIR / "projects"
        if not base.is_dir():
            return []
        safe = _measure_facade.re.sub(r"[^0-9a-fA-F-]", "", id_prefix)
        return [str(p) for p in base.glob(f"*/{safe}*.jsonl")][:3] if safe else []
    except OSError:
        return []


def session_cost_query(argv):
    """Build the session-cost answer as a dict (see session_cost_main)."""
    _measure_facade = _compat.facade()
    opts, err = _measure_facade._session_cost_parse_args(argv)
    tier = _measure_facade._load_pricing_tier()
    out = {
        "db": str(_measure_facade.TRENDS_DB),
        "basis": _measure_facade.SESSION_COST_BASIS.format(tier=_measure_facade._pricing_tier_label(tier)),
        "query": {"words": opts["words"], "id": opts["id"], "date": opts["date"],
                  "days": opts["days"]},
        "sessions": [],
        "match_count": 0,
        "total_cost_usd": 0.0,
        "closest": [],
        "note": None,
    }
    if err:
        out["error"] = err
        return out
    if not _measure_facade.TRENDS_DB.exists():
        out["error"] = ("No Token Optimizer history yet (no trends database at this "
                        "path). Sessions are collected when they end.")
        return out
    conn, mode = _measure_facade._session_cost_open_db(_measure_facade.TRENDS_DB)
    if conn is None:
        out["error"] = f"Could not read the trends database read-only: {mode}"
        return out
    out["read_mode"] = mode
    current_id = (_measure_facade.os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip().lower() or None
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(session_log)").fetchall()}
        select = ", ".join(c if c in cols else f"NULL AS {c}"
                           for c in (x.strip() for x in _measure_facade._SESSION_COST_COLS.split(",")))
        params = []
        where = []
        if opts["date"]:
            if "daily_usage_json" in cols:
                where.append("EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(daily_usage_json) THEN daily_usage_json ELSE '{}' END) WHERE key = ?)")
                params.append(opts["date"])
            else:
                where.append("0")
        elif not opts["id"]:
            days = opts["days"] or 30
            cutoff = (_measure_facade.datetime.now().date() - _measure_facade.timedelta(days=days - 1)).strftime("%Y-%m-%d")
            out["query"]["days"] = days
            if "daily_usage_json" in cols:
                where.append("(daily_usage_json IS NULL OR daily_usage_json IN ('{}','null') OR EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(daily_usage_json) THEN daily_usage_json ELSE '{}' END) WHERE length(key)=10 AND date(key)=key AND key >= ?))")
                params.append(cutoff)
        sql = f"SELECT {select} FROM session_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = conn.execute(sql + " ORDER BY date DESC, id DESC", params).fetchall()
    except _measure_facade.sqlite3.Error as exc:
        conn.close()
        out["error"] = f"Could not read the trends database: {exc}"
        return out
    conn.close()

    # One session counts once: copies of a session (worktree, mirror) keep the
    # most complete row, the same rule as _dedupe_session_rows.
    best = {}
    for r in rows:
        key = _measure_facade._session_cost_row_id(r)
        rank = _measure_facade._session_rank(r["message_count"], r["input_tokens"], r["output_tokens"])
        if key not in best or rank > best[key][0]:
            best[key] = (rank, r)
    rows = [v[1] for v in best.values()]
    rows.sort(key=lambda r: ((_measure_facade := _compat.facade()), (str(r["date"] or ""), int(r["id"] or 0)))[1], reverse=True)

    def _days_of(r):
        _measure_facade = _compat.facade()
        du = _measure_facade._safe_json_dict(r["daily_usage_json"])
        ds = set(_measure_facade._activity_days(du))
        return ds

    def _id_ok(r):
        _measure_facade = _compat.facade()
        if not opts["id"]:
            return True
        sid = _measure_facade._session_cost_row_id(r).lower()
        stem = _measure_facade.Path(r["jsonl_path"] or "").stem.lower()
        return sid.startswith(opts["id"]) or stem.startswith(opts["id"])

    def _date_ok(r):
        _measure_facade = _compat.facade()
        return not opts["date"] or opts["date"] in _days_of(r)

    words = opts["words"]
    scoped = [r for r in rows if _id_ok(r) and _date_ok(r)]
    matched = [r for r in scoped if not words or _measure_facade._session_cost_word_hits(words, r) == len(words)]

    entries = [_measure_facade._session_cost_entry(r, tier, opts["date"], current_id) for r in matched]
    if not opts["id"] and not opts["date"]:
        for row, entry in zip(matched, entries):
            priced = _measure_facade._price_session_row(row, tier)
            entry["cost_usd"]["in_window"] = round(sum(sl[1] for sl in priced["slices"] if sl[0] >= cutoff), 4)
        out["total_scope"] = "dated activity in the requested window; entries retain lifetime totals"
    elif opts["date"]:
        out["total_scope"] = "activity on the requested date; entries retain lifetime totals"
    else:
        out["total_scope"] = "session lifetime"
    out["activity_complete"] = all(e["activity_complete"] for e in entries)
    out["match_count"] = len(entries)
    out["total_cost_usd"] = round(sum(e["cost_usd"].get("on_date", e["cost_usd"].get("in_window", e["cost_usd"]["total"])) for e in entries), 4)
    out["sessions"] = entries[:opts["limit"]]
    out["shown"] = len(out["sessions"])
    if entries:
        return out

    # Nothing matched: say so and offer the closest few. Sessions matching
    # some of the words come first, then the newest in the date scope.
    pool = scoped if (opts["date"] or opts["id"]) and scoped else rows
    if words:
        # Stable sort: within a hit count the newest-first order stays.
        pool = sorted(pool, key=lambda r: ((_measure_facade := _compat.facade()), -_measure_facade._session_cost_word_hits(words, r))[1])
    out["closest"] = [_measure_facade._session_cost_entry(r, tier, opts["date"], current_id) for r in pool[:3]]
    if opts["id"]:
        on_disk = _measure_facade._session_cost_uncollected(opts["id"])
        if on_disk:
            out["note"] = ("That session has a transcript but is not collected yet. "
                           "Collection runs when a session ends; run `measure.py collect` "
                           "to collect now, then ask again.")
            out["uncollected_transcripts"] = on_disk
            return out
    out["note"] = ("No collected session matches. Collection runs when a session ends, "
                   "so a session still open (or ended in the last minute) may not be "
                   "here yet.")
    return out


def _fmt_session_cost_tokens(n):
    _measure_facade = _compat.facade()
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def _fmt_session_cost_duration(minutes):
    _measure_facade = _compat.facade()
    m = int(round(float(minutes or 0)))
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def _usd(v):
    """Dollars as people read them: cents from $1 up, four places below."""
    _measure_facade = _compat.facade()
    v = float(v or 0)
    return f"${v:,.2f}" if abs(v) >= 1 else f"${v:.4f}"


def _print_session_cost_entry(n, e, date_filter=None):
    _measure_facade = _compat.facade()
    title = e["topic"] or e["slug"] or "(untitled session)"
    days = e["days"]
    when = days[0] if len(days) == 1 else f"{days[0]} to {days[-1]}"
    kind = "  [delegated session]" if e["kind"] == "delegated" else ""
    print(f"{n}. {title}{kind}")
    print(f"   {when} · project {e['project']} · id {e['session_id']}")
    sub = e["subagents"]
    sub_txt = f" · {sub['count']} subagent run{'s' if sub['count'] != 1 else ''}" if sub["count"] else ""
    print(f"   {_measure_facade._fmt_session_cost_duration(e['duration_minutes'])} · {e['api_calls']} calls{sub_txt}")
    models = " · ".join(
        f"{m['model']} {_measure_facade._usd(m['cost_usd'])}" + ("" if m["priced"] else " (no price card)")
        for m in e["models"]) or "none recorded"
    print(f"   Models: {models}")
    t = e["tokens"]
    print(f"   Tokens: {_measure_facade._fmt_session_cost_tokens(t['input'])} input · "
          f"{_measure_facade._fmt_session_cost_tokens(t['output'])} output · "
          f"{_measure_facade._fmt_session_cost_tokens(t['cache_read'])} cache read · "
          f"{_measure_facade._fmt_session_cost_tokens(t['cache_write'])} cache write")
    c = e["cost_usd"]
    print(f"   Cost:   input {_measure_facade._usd(c['input'])} · output {_measure_facade._usd(c['output'])} · "
          f"cache read {_measure_facade._usd(c['cache_read'])} · cache write {_measure_facade._usd(c['cache_write'])}"
          + (f" · long-context {_measure_facade._usd(c['long_context_adjustment'])}" if "long_context_adjustment" in c else ""))
    split = ""
    if (sub["count"] or c["subagents"] > 0) and sub["included"]:
        split = f"  (main thread {_measure_facade._usd(c['main_thread'])} + subagents {_measure_facade._usd(c['subagents'])})"
    elif sub["count"]:
        split = "  (main thread only; subagents are added on the next collection)"
    print(f"   Total:  {_measure_facade._usd(c['total'])}{split}")
    if "on_date" in c:
        print(f"   Of which on {date_filter}: {_measure_facade._usd(c['on_date'])}")
    if e["partial"]:
        print(f"   Partial: {e['partial_reason']} (figures as of {e['collected_at']}).")


def session_cost_main(argv):
    """`measure.py session-cost [words...] [--id UUID] [--date D] [--days N]
    [--limit N] [--json]`: what a session cost, from the user's own words."""
    _measure_facade = _compat.facade()
    res = _measure_facade.session_cost_query(argv)
    if "--json" in argv:
        print(_measure_facade.json.dumps(res, indent=2, ensure_ascii=False, default=str))
        return 1 if res.get("error") else 0
    if res.get("error"):
        print(f"[Token Optimizer] {res['error']}")
        return 1
    q = res["query"]
    scope = []
    if q["words"]:
        scope.append("“" + " ".join(q["words"]) + "”")
    if q["id"]:
        scope.append(f"id {q['id']}")
    if q["date"]:
        scope.append(f"on {q['date']}")
    elif q.get("days"):
        scope.append(f"in the last {q['days']} days")
    scope_txt = " ".join(scope) or "all sessions"
    if res["match_count"]:
        n = res["match_count"]
        print(f"Session cost: {n} session{'s' if n != 1 else ''} {scope_txt}"
              + (f", {_measure_facade._usd(res['total_cost_usd'])} in total" if n > 1 else ""))
        if res["shown"] < n:
            print(f"(showing the newest {res['shown']}; --limit N shows more)")
        print()
        for i, e in enumerate(res["sessions"], 1):
            _measure_facade._print_session_cost_entry(i, e, q["date"])
            print()
    else:
        print(f"Session cost: no session matches {scope_txt}.")
        if res.get("note"):
            print(res["note"])
        if res["closest"]:
            print()
            print("Closest sessions:")
            print()
            for i, e in enumerate(res["closest"], 1):
                _measure_facade._print_session_cost_entry(i, e, q["date"])
                print()
    print(f"Basis: {res['basis']}")
    if res.get("read_mode") == "snapshot":
        print("Read from a lock-free snapshot; the very latest writes may be missing.")
    return 0


def _estimate_compression_cost_per_mtok(model=None):
    """Return USD cost per million input tokens for the active pricing tier.

    Used to attribute $ savings to compression_events entries, which only
    store token counts. Uses the session's actual model (via _resolve_session_model
    when model arg is None). Falls back to Sonnet's published rate on any error.
    """
    _measure_facade = _compat.facade()
    try:
        tier = _measure_facade._load_pricing_tier()
        tier_data = _measure_facade.PRICING_TIERS.get(tier, _measure_facade.PRICING_TIERS.get("anthropic", {}))
        normalized = (_measure_facade._claude_price_key(model, tier_data.get("claude_models", {})) if model else None) \
            or _measure_facade._resolve_session_model()
        rates = tier_data.get("claude_models", {}).get(normalized) \
                or tier_data.get("claude_models", {}).get("sonnet", {})
        return float(rates.get("input", 3.0))
    except Exception:
        return 3.0


def _window_input_rate_per_mtok(days=None):
    """Fresh-input USD per million tokens, weighted by each model's share of
    usage over the window. Estimates use it so their price follows the whole
    window's workload, not whichever model the latest session ran. Unpriced
    models are left out of the weighting. Falls back to the session-model rate
    when no model in the window has a price. Never raises."""
    _measure_facade = _compat.facade()
    try:
        if _measure_facade.detect_runtime() == "codex":
            return _measure_facade._estimate_compression_cost_per_mtok()
        tier_data = _measure_facade.PRICING_TIERS.get(_measure_facade._load_pricing_tier(), _measure_facade.PRICING_TIERS.get("anthropic", {}))
        models = tier_data.get("claude_models", {})
        weighted, priced_share = 0.0, 0.0
        for model, share in (_measure_facade._model_mix_shares(days=days).get("shares") or {}).items():
            key = _measure_facade._claude_price_key(model, models)
            rate = (models.get(key) or {}).get("input") if key else None
            if rate is None or not share:
                continue
            weighted += float(share) * float(rate)
            priced_share += float(share)
        if priced_share > 0:
            return weighted / priced_share
    except Exception:
        pass
    return _measure_facade._estimate_compression_cost_per_mtok()


def _resolve_structural_baseline(days=None):
    """Return the pristine prefix-overhead baseline for structural savings, or None.

    Source is the "before" snapshot (an audit of the prefix: skills, MEMORY,
    CLAUDE, MCP, core system) captured pre-pruning. The `days` arg is unused
    here but kept for call-site symmetry with the rest of the savings family.

    There is deliberately NO session_log fallback: session_log.input_tokens is
    the total input MIX (cache-read + cache-write + fresh, billions/month), not
    a prefix size, so using it as a baseline would overstate savings by orders
    of magnitude. Structural savings are measurable only once a real prefix
    snapshot exists; ensure-health auto-captures one on first run for new users.

    Returns {source, date (ISO), baseline_tokens} or None.
    """
    _measure_facade = _compat.facade()
    try:
        snap_path = _measure_facade.SNAPSHOT_DIR / "snapshot_before.json"
        if _measure_facade.SNAPSHOT_DIR.exists() and snap_path.exists():
            data = _measure_facade.json.loads(snap_path.read_text(encoding="utf-8"))
            ts = data.get("timestamp")
            total = int(data.get("totals", {}).get("estimated_total", 0) or 0)
            if ts and total > 0:
                snap_dt = _measure_facade.datetime.fromisoformat(ts.replace("Z", "+00:00").split("+")[0])
                age_days = (_measure_facade.datetime.now() - snap_dt).days
                # A pristine baseline does not expire quickly; cap at a year to
                # avoid crediting against a baseline that predates major
                # legitimate config growth.
                if 0 <= age_days <= 365:
                    return {
                        "source": "snapshot",
                        "date": snap_dt.isoformat(),
                        "baseline_tokens": total,
                    }
    except (_measure_facade.json.JSONDecodeError, KeyError, ValueError, OSError):
        pass
    return None


def _runway_routing_lever(days=None):
    """Routing lever of the plan card, read from _compute_model_routing_savings.

    The factor is the standardized baseline cost over the standardized actual
    cost of the same work (docs/METHODOLOGY.md section 3.2): class-weighted,
    one reference card per family, premium/unpriced/free models outside both
    arms. Above 1 the mix got lighter, below 1 heavier. ``factor`` is None and
    ``state`` says why ("pending" while sessions are re-measured, else
    "unavailable") when there is no routing evidence. Never raises.
    """
    _measure_facade = _compat.facade()
    try:
        r = _measure_facade._compute_model_routing_savings(days=days) or {}
    except Exception:
        r = {}
    out = {
        "state": "unavailable", "factor": None,
        "basis": r.get("realized_basis"),
        "baseline_source": r.get("baseline_source"),
        "excluded_models": {
            "premium": list(r.get("premium_models") or []),
            "unpriced": list(r.get("unpriced_models") or []),
            "free": list(r.get("free_models") or []),
        },
        "coverage": r.get("compared_share"),
        "unsplit_tokens": int(r.get("unsplit_tokens") or 0),
        "pending_sessions": int(r.get("pending_sessions") or 0),
        "standardized_baseline_cost_usd": r.get("standardized_baseline_cost_usd"),
        "standardized_actual_cost_usd": r.get("standardized_actual_cost_usd"),
    }
    evidence = r.get("evidence")
    if evidence == "pending":
        out["state"] = "pending"
        return out
    base = float(r.get("standardized_baseline_cost_usd") or 0.0)
    actual = float(r.get("standardized_actual_cost_usd") or 0.0)
    if evidence == "estimated" and base > 0 and actual > 0:
        out["state"] = "estimated"
        out["factor"] = base / actual
    return out


def _routing_direction(factor):
    """"lighter", "heavier" or "unchanged" for a routing factor."""
    _measure_facade = _compat.facade()
    if factor is None:
        return None
    if factor > 1:
        return "lighter"
    return "heavier" if factor < 1 else "unchanged"


def _routing_mix_reprice_ratio(model_routing=None, days=None, since=None):
    """Baseline/current work-value ratio for the transformation add-backs.

    The transformation headline reprices metered compression savings and the
    estimated avoided-volume pools to what the baseline mix would have paid
    for the same work. That ratio is the routing recipe's standardized
    baseline cost over its standardized actual cost -- the factor the plan
    card publishes and the Savings tab's routing figure is built from -- so
    it inherits exact-card pricing (no family or default fallback), premium
    (Fable) exclusion, reference cards and the pending gate. `model_routing`
    is the recipe payload the caller already computed (with its own `since`
    window); when it is None the recipe is resolved here for `days`/`since`.
    Returns 1.0 -- a neutral reprice -- whenever routing has no estimated
    evidence (pending, unavailable, or a degenerate arm). Never raises.
    """
    _measure_facade = _compat.facade()
    try:
        r = model_routing
        if r is None:
            r = _measure_facade._compute_model_routing_savings(days=days, since=since) or {}
        if not isinstance(r, dict) or r.get("evidence") != "estimated":
            return 1.0
        base = float(r.get("standardized_baseline_cost_usd") or 0.0)
        actual = float(r.get("standardized_actual_cost_usd") or 0.0)
        if base <= 0 or actual <= 0:
            return 1.0
        return base / actual
    except Exception:
        return 1.0


def _routing_reprice_basis(model_routing):
    """Why the transformation add-back reprice is what it is.

    "routing_recipe"        -- the routing recipe supplied a usable
                               standardized baseline/actual ratio.
    "routing_pending"       -- sessions in the window are still being
                               re-measured; the recipe withholds its ratio
                               and the reprice is neutral (1.0).
    "routing_unavailable"   -- no usable recipe (missing payload, non-estimated
                               evidence, or degenerate arms); the reprice is
                               neutral (1.0).
    Surfaces render a plain disclosure line for the two neutral bases so a
    silently-flattened add-back never reads as a measured figure.
    """
    _measure_facade = _compat.facade()
    if not isinstance(model_routing, dict):
        return "routing_unavailable"
    evidence = model_routing.get("evidence")
    if evidence == "pending":
        return "routing_pending"
    if evidence == "estimated":
        try:
            base = float(model_routing.get("standardized_baseline_cost_usd") or 0.0)
            actual = float(model_routing.get("standardized_actual_cost_usd") or 0.0)
        except (TypeError, ValueError):
            base = actual = 0.0
        if base > 0 and actual > 0:
            return "routing_recipe"
    return "routing_unavailable"


def _runway_saved_tokens(conn, cutoff, include_estimated=None):
    """Measured tokens never sent since *cutoff*, netted like the savings totals.

    This is the counted floor behind the window card's "measured token floor":
    the same events the savings report keeps in its measured total, so the
    estimated tiers (setup trim, MCP cap, followed hints, lean resumes,
    checkpoint restores, verbosity steering) are left out. Archive re-expansions are subtracted from
    the archive credit (never below zero) and are not savings of their own. A
    compression feature that savings_events also logs (delta_read,
    structure_map) is counted once, from savings_events. With
    ``include_estimated`` the estimated tiers are kept, for the context
    multiplier, which the card labels as an estimate.
    """
    _measure_facade = _compat.facade()
    table = _measure_facade._counted_table(conn, "savings_events")
    sql = _measure_facade._RUNWAY_SAVED_SQL[table]
    if "detail" not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}: sql = sql.replace(_measure_facade._counted_cap.reexpand_type(), "event_type")
    by_event = {
        event_type: int(tokens or 0)
        for event_type, tokens in conn.execute(
            sql, (cutoff,))
        if include_estimated or event_type not in _measure_facade._RUNWAY_ESTIMATED_EVENT_TYPES}
    reexpanded = by_event.pop("tool_archive_reexpand", 0)
    if "bash_archive_reexpand" in by_event: by_event["bash_archive_reexpand"] *= -1
    if reexpanded:
        by_event["tool_archive"] = by_event.get("tool_archive", 0) - reexpanded
    saved = sum(by_event.values())
    table = _measure_facade._counted_table(conn, "compression_events")
    if "feature" not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
        # An install old enough to log no feature name cannot be split by owner.
        return saved + int(conn.execute(
            _measure_facade._RUNWAY_COMPRESSED_TOTAL_SQL[table], (cutoff,)).fetchone()[0] or 0)
    for feature, tokens in conn.execute(_measure_facade._RUNWAY_COMPRESSED_SQL[table], (cutoff,)):
        if feature in _measure_facade._V5_COMPRESSION_CATEGORIES and feature not in by_event:
            saved += int(tokens or 0)
    return saved


def runway_snapshot(days=None, now=None):
    """Subscription runway: how much of your rate-limit window Token Optimizer
    hands back, and what the same windows would read without it.

    WHY THIS EXISTS AND NOT A DOLLAR FIGURE. On a subscription you are not
    buying tokens, you are buying a rate-limit budget for a flat fee. Reporting
    "$0 saved" to that user measures in the wrong currency (see the same note at
    _keepwarm_*: a keep-warm ping "saves $0"). The unit that matters is how much
    work fits inside the plan already paid for, which converts straight back to
    money the moment a window runs out and the work has to finish on overage or
    API spend.

    TWO INDEPENDENT LEVERS, MULTIPLIED:
      context : tokens never sent at all (archive, skeletons, blocked reads).
                Directly metered from savings_events + compression_events;
                estimated tiers are not included.
      routing : the model-routing recipe's standardized baseline cost over its
                standardized actual cost (_runway_routing_lever). Above 1 the
                mix got lighter, below 1 heavier; a user who never left Opus
                sees 1.0x. None, with routing_state "pending"/"unavailable",
                when there is no routing evidence.

    HONESTY BOUNDARY: the routing lever needs to know how the provider weights
    models inside the 5h/7d windows. That weighting is not published, so this
    uses public per-class API rates (one reference card per family) as a
    documented stand-in and labels the result estimated. `proxy` in the
    returned dict says so out loud; any surface rendering this MUST carry that
    through rather than printing a bare number.

    Returns None when there is nothing trustworthy to say: no meter reading at
    all, or no measurable throughput shift. A stale-but-present meter is NOT dropped
    -- this is the ONLY caller of the meter for the dashboard, which is viewed at
    arbitrary times (the statusline only refreshes the meter while you are
    actively working, so by the time you open the dashboard the reading is
    routinely >30 min old). Vanishing then is exactly the "section doesn't
    update" failure; instead the last real reading is carried through with
    `meter_stale` + `meter_ts` so the card can label its freshness honestly and
    refresh the instant a newer reading lands. A per-window guard below drops any
    window whose reading is older than the window itself (its limit has reset).
    Silence only when there is no reading to show. Never raises.
    """
    _measure_facade = _compat.facade()
    try:
        meters = _measure_facade._keepwarm_read_meters(now=now)
        # The live meter feeds ONLY the per-window bars. The headline throughput
        # multiplier and the weekly savings come from the savings ledger, not the
        # meter, so an UNAVAILABLE meter (missing/contentless rate-limits file)
        # must NOT vanish the whole card. It drops to empty windows (the renderer
        # shows a "meter refreshing" note), exactly like a stale meter. Bug A: the
        # old `if not available: return None` killed the entire card -- multiplier
        # and savings included -- the moment the meter file was gone, which is the
        # "section disappeared and never came back" report. The window loop below
        # already skips a None percentage, so an unavailable meter yields [].
        meter_available = bool(meters.get("available"))
        meter_stale = bool(meters.get("stale")) or not meter_available

        weekly_signal = bool(_measure_facade._weekly_full_savings(resets_at=meters.get("seven_day_resets_at"), now=now)); weekly_full = None  # Do not surface the counterfactual dollars.

        # Context uses dated input and output, including cache.
        consumed = saved = context_saved = 0
        traffic = None
        try:
            conn = _measure_facade._init_trends_db()
            try:
                cutoff = (_measure_facade.datetime.now() - _measure_facade.timedelta(days=days)).isoformat()
                traffic = _compat.facade()._headline.input_traffic(conn, cutoff[:10], _measure_facade.datetime.now().date().isoformat())
                consumed = traffic["tokens"]
                saved = _measure_facade._runway_saved_tokens(conn, cutoff)
            finally:
                conn.close()
        except Exception:
            return None
        if consumed <= 0 and not weekly_full:
            return None
        try:
            token_source = _measure_facade._get_merged_savings(days=days)
            token_source["counted_period"] = _measure_facade._counted_cumulative_summary(days=days)
            token_parts = _compat.facade()._headline.token_headline(token_source)
        except Exception:
            token_parts = _compat.facade()._headline.token_headline({})
        context_saved = token_parts["total_tokens"]
        context_mult = (consumed + context_saved) / consumed if consumed > 0 else 1.0

        # --- routing lever: the model-routing recipe's own standardized ratio ---
        # Pending or unavailable routing remains explicit.
        routing = _measure_facade._runway_routing_lever(days=days)
        routing_state = routing.get("state")
        routing_mult = routing.get("factor")

        mult = context_mult * (routing_mult if routing_mult is not None else 1.0)
        # Hide immaterial changes unless weekly savings are available.
        if mult < 1.02 and not weekly_signal:
            return None

        # --- USD-per-window: API-credit OVERAGE over each window's OWN real span ---
        # The dollar answers the user's question directly: "if I had kept working
        # WITHOUT Token Optimizer and hit my limits, what would the SAME work cost
        # me in API-credit overage?" So each window is priced over ITS OWN span —
        # the weekly window since your current subscription limit period RESET
        # (aligned to the meter's resets_at boundary), not a rolling 7-day lookback
        # that drifts down as heavy days age out. When the meter is unavailable the
        # rolling 7-day fallback is used.
        #
        # The context-dollar part is the counted, transcript-backed ledger for
        # that window: one-shot plus later deduped rereads to compaction. The
        # routing-dollar part remains the merged model-routing counterfactual.
        # The two pools are disjoint (tokens never sent vs cheaper model for the
        # same tokens), so summing is not a double-count. Never re-derived from
        # context_mult/routing_mult, which would double-count the ledger.
        #
        # Sub-day windows (5h) have NO honest figure: the savings ledger is
        # day-granular, so "dollars saved in the last 5 hours" cannot be computed
        # without inventing a proration. Those windows carry no dollar line (None)
        # and show only their headroom bars.
        _overage_cache = {}
        _overage_pricing = {}

        def _window_overage_usd(span_h, resets_at=None):
            """(usd, tier) of API-credit overage avoided over this window's span,
            or (None, None) when the span is sub-day or the ledger is empty.

            When *resets_at* is provided AND the window is the weekly (7d) span,
            the savings ledger is summed from ``resets_at - 7 * 24 * 3600``
            (the true limit-period start) to now instead of a rolling ``now - 7
            days``. This aligns the dollar figure with the actual subscription
            limit window so it does not drift down as heavy days age out of a
            simple rolling lookback. When *resets_at* is None/unavailable, the
            call falls back to the rolling *days* cutoff (unchanged behaviour
            for the 5h window, which never receives a resets_at).
            """
            _measure_facade = _compat.facade()
            if span_h < 24:
                return None, None
            wdays = max(1, int(round(span_h / 24.0)))
            # --- weekly window alignment ---
            # When the meter supplies a resets_at for the 7d window, compute
            # the true limit-period start and sum savings from that boundary.
            # Otherwise fall back to the rolling *wdays* cutoff (legacy path).
            since_iso = None
            if resets_at is not None and wdays == 7:
                window_start = float(resets_at) - (7 * 24 * 3600)
                try:
                    since_iso = _measure_facade.datetime.fromtimestamp(window_start).isoformat()
                except (ValueError, OSError):
                    since_iso = None  # fall back to rolling

            cache_key = (wdays, since_iso) if since_iso else wdays
            # EVERY name the code below reads must be bound on BOTH branches. The
            # parked 2026-08-28 build read `wm` (bound only inside this `if`)
            # after the cache block, so any cache HIT raised UnboundLocalError,
            # which the outer handler turned into a silently vanished card. The
            # reprice flag therefore travels through the cache TUPLE, and nothing
            # below this block touches `wm`.
            if cache_key not in _overage_cache:
                ctx = rt = 0.0
                merged_ctx = 0.0
                est_add = 0.0
                repriced = False
                counted_window = False
                try:
                    wm = _measure_facade._get_merged_savings(days=wdays, since=since_iso)
                    merged_ctx = float(wm.get("total_cost_usd", 0.0) or 0.0)
                    rt = float((wm.get("model_routing") or {}).get("realized_cost_usd", 0.0) or 0.0)
                    repriced = bool(wm.get("repriced_to_session_mix"))
                    est_add = max(0.0, float((wm.get("resume_lean_verified") or {}).get("cost_saved_usd") or 0.0))
                except Exception:
                    pass
                try:
                    conn = _measure_facade._init_trends_db()
                    try:
                        end_utc = _measure_facade.datetime.now(_measure_facade.timezone.utc).replace(tzinfo=None)
                        if since_iso:
                            start_utc = _measure_facade._counted_event_utc(since_iso)
                        else:
                            start_utc = end_utc - _measure_facade.timedelta(days=wdays)
                        counted = _measure_facade._counted_window_summary(conn, start_utc, end_utc)
                        _overage_pricing[cache_key] = {
                            "reread_pricing_complete": counted.get("reread_pricing_complete", True),
                            "reread_unpriced_tokens": counted.get("reread_unpriced_tokens", 0),
                            "reread_pricing_note": counted.get("reread_pricing_note", ""),
                            "unmeasured_events": counted.get("unmeasured_events", 0),
                            "unmeasured_removed_tokens": counted.get("unmeasured_removed_tokens", 0),
                        }
                    finally:
                        conn.close()
                    if counted.get("available"):
                        # Keep setup, output, unmatched events and other logged
                        # savings. Their initial removals are already in merged_ctx.
                        ctx = merged_ctx + float(counted.get("reread_usd", 0.0) or 0.0)
                        counted_window = True
                except Exception:
                    # Older databases without counted_reread retain the legacy
                    # flat-ledger fallback until their counted backfill completes.
                    pass
                if not counted_window:
                    ctx = merged_ctx
                # Fold the estimated-tier levers back into the shown dollar. This
                # flips the tier to "estimated" (est_added below) -- honest: the
                # trigger is counterfactual even though the magnitude is metered.
                ctx += est_add
                est_added = est_add > 0.0
                # The full-workload figure replaces the metered sum only when it
                # is larger: a flat or negative workload month must never hide
                # savings that were actually measured this week.
                if wdays == 7 and weekly_full and float(weekly_full.get("saved_usd") or 0.0) > ctx + rt:
                    ctx, rt = float(weekly_full["saved_usd"]), 0.0
                    est_added = True
                _overage_cache[cache_key] = (ctx, rt, repriced, counted_window, est_added)
            ctx, rt, repriced, counted_window, est_added = _overage_cache[cache_key]
            total = ctx + rt
            # Suppress immaterial dollar lines. Right after a limit reset the
            # aligned window is only hours old, so its overage is a few cents
            # ("≈$0.36 more in API credits") which reads as noise next to the
            # headroom bars. Below _MIN_OVERAGE_USD_SHOWN the window carries no
            # dollar line (returns None, like a sub-day window) and shows only
            # its bars; the figure reappears once real usage accrues in-window.
            if total < _measure_facade._MIN_OVERAGE_USD_SHOWN:
                return None, None
            # Measured-vs-estimated boundary: context $ is metered from actual
            # tokens_saved, EXCEPT where the model-mix repair moved a rate --
            # allocating a never-sent saving to its session's blended rate is an
            # allocation, not a measurement. Routing $ is an estimated
            # counterfactual. Estimated wins the label whenever routing
            # contributes OR the reprice fired.
            tier = "estimated" if (rt > 0 or repriced or est_added) else "measured"
            return round(total, 2), tier

        windows = []
        meter_age_s = meters.get("age_s")
        # Per-window resets_at: only meaningful for the 7d window (aligns the
        # weekly dollar figure to the subscription limit period). The 5h window
        # has no dollar line and skips alignment (resets_at=None).
        _sd_resets = meters.get("seven_day_resets_at")
        for key, label, span_h in (("five_hour", "5h", 5), ("seven_day", "7d", 168)):
            used = meters.get("five_hour_pct" if key == "five_hour" else "seven_day_pct")
            if used is None:
                continue
            # Drop a window whose reading is older than the window itself: once a
            # 5h reading is >5h old the 5h limit has certainly reset, so "12% used"
            # is meaningless and "you'd already be cut off" is a lie. Per-window so
            # a day-old reading still keeps the 7d window (valid for a week) while
            # dropping the reset 5h one. If both drop, the card falls away (below).
            if meter_age_s is not None and meter_age_s > span_h * 3600:
                continue
            counterfactual = min(100.0, used * mult)
            head_now = max(0.0, 100.0 - used)
            head_cf = max(0.0, 100.0 - counterfactual)
            # Pass resets_at for the 7d window so the dollar figure aligns to the
            # true subscription limit period start (resets_at - 7d). Pass None for
            # 5h (no dollar line, uses rolling days as before).
            window_resets = _sd_resets if key == "seven_day" else None
            window_saved_usd, window_usd_tier = _window_overage_usd(span_h, resets_at=window_resets)
            windows.append({
                "key": key, "label": label, "span_hours": span_h,
                "used_pct": round(used, 1),
                "headroom_pct": round(head_now, 1),
                "without_used_pct": round(counterfactual, 1),
                "without_headroom_pct": round(head_cf, 1),
                # None when the counterfactual is already capped: "infinity x
                # more room" is not a sentence anyone should read.
                "headroom_multiple": round(head_now / head_cf, 1) if head_cf > 0.5 else None,
                "would_be_capped": head_cf <= 0.5,
                "saved_usd": window_saved_usd,
                "saved_usd_tier": window_usd_tier,
                "full_value": weekly_full if key == "seven_day" else None,
            })
        # REGRESSION FIX (the "Your plan goes further" card vanished after a quiet
        # week): the per-window guard above drops the 5h window once the meter is
        # >5h old and the 7d window once it is >7d old, so a keep-warm meter that
        # has not refreshed for over a week empties `windows`. The old
        # `if not windows: return None` then dropped the ENTIRE card -- yet the
        # throughput multiplier (extra_work_pct) and the weekly savings do NOT
        # depend on the live meter; they come from the 30d/7d savings ledger. Keep
        # the card alive with empty windows so the headline + savings still show;
        # the renderer omits the per-window bars and shows a "meter refreshing"
        # note. Populate the weekly $ spine directly since the 7d window that
        # normally fills _overage_cache[...] may have been dropped above.
        #
        # Compute the aligned weekly cache key (same logic as _window_overage_usd)
        # so the fallback and spine read use the same entry.
        _wk_since = None
        if _sd_resets is not None:
            _wk_start = float(_sd_resets) - (7 * 24 * 3600)
            try:
                _wk_since = _measure_facade.datetime.fromtimestamp(_wk_start).isoformat()
            except (ValueError, OSError):
                _wk_since = None
        _wk_cache_key = (7, _wk_since) if _wk_since else 7
        # Route the weekly spine through _window_overage_usd UNCONDITIONALLY. When
        # the 7d window rendered above this is a pure cache HIT (the ledger is not
        # recomputed -- _overage_cache guarantees one _get_merged_savings call per
        # key), so the hit branch is exercised on every normal render instead of
        # only in the rare both-windows-dropped case. That is precisely the branch
        # the parked 2026-08-28 build broke; keeping it hot keeps it honest.
        _window_overage_usd(168, resets_at=_sd_resets)

        # Top-level spine reflects the WEEKLY (7d) ledger -- the dollar the card
        # actually shows -- so the tier chip matches the number beside it. The 7d
        # merged call is already cached above; re-read the same components.
        _wk_ctx, _wk_rt, _wk_repriced, _wk_counted, _wk_est = _overage_cache.get(
            _wk_cache_key, (0.0, 0.0, False, False, False))
        # Suppress an immaterial weekly dollar figure (same rule as the per-window
        # line above): below _MIN_OVERAGE_USD_SHOWN the spine reads zero so the
        # card shows no "$X in API credits" line -- only the headroom bars. Right
        # after a limit reset the aligned window is worth a few cents, and that
        # reads as noise beside the bars.
        if (_wk_ctx + _wk_rt) < _measure_facade._MIN_OVERAGE_USD_SHOWN:
            _wk_ctx = _wk_rt = 0.0
        saved_context_usd = _wk_ctx
        saved_routing_usd = _wk_rt
        # Same rule as the per-window line in _window_overage_usd: "estimated"
        # when routing contributes OR the model-mix repair moved a rate. No tier
        # at all when there is no dollar to label (suppressed or genuinely zero),
        # so a repriced-but-immaterial window never advertises a tier for $0.
        saved_usd_tier = (
            None if (saved_context_usd <= 0 and saved_routing_usd <= 0)
            else ("estimated" if (saved_routing_usd > 0 or _wk_repriced or _wk_est) else "measured"))

        return {
            "available": True,
            "tier": "estimated",
            "multiplier": round(mult, 2),
            "context_multiplier": round(context_mult, 4),
            "routing_multiplier": round(routing_mult, 3) if routing_mult is not None else None,
            "routing_state": routing_state,
            "routing_direction": _measure_facade._routing_direction(routing_mult),
            # The window the routing lever (and its mix coverage) was computed
            # over, so renderers never hard-code "30 days" for a parameterised
            # snapshot.
            "routing_window_days": days,
            "routing_pending_sessions": routing.get("pending_sessions", 0),
            "routing_basis": routing.get("basis"),
            "routing_baseline_source": routing.get("baseline_source"),
            "routing_excluded_models": routing.get("excluded_models"),
            "routing_coverage": routing.get("coverage"),
            "routing_unsplit_tokens": routing.get("unsplit_tokens", 0),
            "routing_standardized_baseline_cost_usd": routing.get("standardized_baseline_cost_usd"),
            "routing_standardized_actual_cost_usd": routing.get("standardized_actual_cost_usd"),
            "extra_work_pct": round((mult - 1) * 100, 1),
            "tokens_consumed": int(consumed),
            "tokens_consumed_basis": traffic["basis"] if traffic else None,
            "tokens_consumed_basis_complete": bool(traffic and traffic["complete"]),
            "tokens_saved": int(saved),
            "context_tokens_saved": int(context_saved), "token_headline": token_parts,
            "windows": windows,
            # USD spine: metered context $ + estimated routing $ over the WEEKLY
            # (7d) ledger -- the same span the weekly card shows. Exposed at the top
            # level so the surface and tests can assert the no-double-count invariant
            # (USD traces to the ledger, not to the throughput multipliers).
            "saved_usd_context": round(saved_context_usd, 2),
            "saved_usd_routing": round(saved_routing_usd, 2),
            "saved_usd_tier": saved_usd_tier,
            "counted_pricing": _overage_pricing.get(_wk_cache_key, {}),
            "weekly_full_value": weekly_full,
            "window_savings_basis": (
                "full workload, exact subscription week" if weekly_full else
                "counted transcript window" if _wk_counted else "flat savings ledger fallback"),
            # period_days scopes the throughput MULTIPLIERS (context/routing), not
            # the per-window dollars: each window now prices overage over its OWN
            # span (weekly = 7d), so the dollars are window-real, not a slice.
            "period_days": days,
            "meter_age_s": meters.get("age_s"),
            "meter_ts": meters.get("ts"),
            "meter_stale": meter_stale,
            # Meter's real availability (missing/contentless rate-limits file ->
            # False). Distinct from top-level `available` (which means "the card
            # has a story"): the card stands on the ledger-based multiplier even
            # when meter_available is False -- only the per-window bars need it.
            "meter_available": meter_available,
            # Single authoritative methodology note (GitHub: dashboard wall-of-text
            # fix). Folds the measured-vs-estimated boundary in here so the HTML no
            # longer repeats it. Keep the phrases "metered savings ledger" and "not
            # derived from the throughput multipliers" -- guarded by
            # test_proxy_disclosure_mentions_ledger_reuse.
            "proxy": ("Weekly dollars use the Savings tab's full workload estimate for activity "
                      "since the subscription reset, at constant prices. Measured and estimated "
                      "effects overlap, so they are counted once. This is accrued API-equivalent "
                      "value, not a forecast or an overage bill. The headline percentage covers "
                      "30 days; window comparisons are estimates." if weekly_full else ("Your window usage is measured; the “without” "
                      "comparison and routing dollars are estimated (the provider "
                      "does not publish how it weights models inside a window, so "
                      "public per-class API rates stand in). Per-window dollars reuse "
                      "the metered savings ledger (context tokens never sent, priced "
                      "at input rates, plus a routing estimate) and are not derived "
                      "from the throughput multipliers.")),
        }
    except Exception:
        return None


def _active_model_cache_rates():
    """Return (cache_read, cache_write_5m, cache_write_1h) USD/MTok for the active model.

    Falls back to Opus-class rates on any error so structural compounding never
    silently zeroes out. The 1h write defaults to 1.6x the 5m rate when the tier
    omits an explicit 1h entry (1h = 2x input, 5m = 1.25x input, ratio 1.6).
    """
    _measure_facade = _compat.facade()
    try:
        tier = _measure_facade._load_pricing_tier()
        tier_data = _measure_facade.PRICING_TIERS.get(tier, _measure_facade.PRICING_TIERS.get("anthropic", {}))
        model = _measure_facade._resolve_session_model()
        rates = tier_data.get("claude_models", {}).get(model) \
            or tier_data.get("claude_models", {}).get("opus", {})
        read = float(rates.get("cache_read", 0.5))
        write_5m = float(rates.get("cache_write", 6.25))
        write_1h = float(rates.get("cache_write_1h", write_5m * 1.6))
        return read, write_5m, write_1h
    except Exception:
        return 0.5, 6.25, 10.0


def _mix_cache_rates(days=None):
    """(cache_read, cache_write_5m, cache_write_1h) USD/MTok blended by the
    current model mix.

    _active_model_cache_rates() prices at whatever model THE REPORTING SESSION
    runs, so the same structural opportunity swings several-fold in $/mo between
    sessions with different cache rate tiers
    with zero underlying change. The prefix is re-read by every session in the
    window, so the defensible rate is the window's own model mix -- the same
    mix the transformation pool prices its "now" arm at. Unpriced models
    blend at the runtime default's rate; no mix falls back to the
    session-model rates rather than 0.
    """
    _measure_facade = _compat.facade()
    try:
        shares = (_measure_facade._model_mix_shares(days=days).get("shares") or {})
        items = [(mdl, s) for mdl, s in shares.items() if s and s > 0]
        if not items:
            return _measure_facade._active_model_cache_rates()
        tier = _measure_facade._load_pricing_tier()
        tier_data = _measure_facade.PRICING_TIERS.get(tier, _measure_facade.PRICING_TIERS.get("anthropic", {}))
        models = tier_data.get("claude_models", {})
        default_rates = models.get(_measure_facade._default_model_for_runtime()) \
            or models.get("sonnet", {})
        tot = sum(s for _, s in items)
        read = w5m = w1h = 0.0
        for mdl, s in items:
            rates = models.get(_measure_facade._strip_provider_prefixes(str(mdl)) or mdl) \
                or models.get(mdl) or default_rates
            r5 = float(rates.get("cache_write", 2.5))
            read += s * float(rates.get("cache_read", 0.2))
            w5m += s * r5
            w1h += s * float(rates.get("cache_write_1h", r5 * 1.6))
        return read / tot, w5m / tot, w1h / tot
    except Exception:
        return _measure_facade._active_model_cache_rates()


def _cached_measure_components():
    """measure_components() with a short in-process cache to avoid double scans."""
    _measure_facade = _compat.facade()
    now = _measure_facade.time.time()
    if _measure_facade._COMPONENTS_CACHE["data"] is not None and (now - _measure_facade._COMPONENTS_CACHE["ts"]) < _measure_facade._COMPONENTS_CACHE_TTL_SECONDS:
        return _measure_facade._COMPONENTS_CACHE["data"]
    data = _measure_facade.measure_components()
    _measure_facade._COMPONENTS_CACHE["data"] = data
    _measure_facade._COMPONENTS_CACHE["ts"] = now
    return data


def _current_overhead_tokens():
    """Measured current prefix overhead (estimated_total), same units as a snapshot.

    Cached to a sidecar with a 1h TTL because measure_components() scans the
    skills/MEMORY/CLAUDE filesystem and this runs inside dashboard regen on
    every SessionEnd. Returns None on any error so the caller skips the
    structural calc rather than emit a wrong number.
    """
    _measure_facade = _compat.facade()
    cache_path = _measure_facade.SNAPSHOT_DIR / ".current-overhead-cache.json"
    try:
        if cache_path.exists():
            data = _measure_facade.json.loads(cache_path.read_text(encoding="utf-8"))
            tot = int(data.get("estimated_total", 0) or 0)
            if tot > 0 and (_measure_facade.time.time() - float(data.get("ts", 0))) < _measure_facade._OVERHEAD_CACHE_TTL_SECONDS:
                return tot
    except (_measure_facade.json.JSONDecodeError, OSError, ValueError, TypeError):
        pass
    try:
        total = int(_measure_facade.calculate_totals(_measure_facade._cached_measure_components()).get("estimated_total", 0) or 0)
        if total <= 0:
            return None
        try:
            cache_path.write_text(
                _measure_facade.json.dumps({"ts": _measure_facade.time.time(), "estimated_total": total}),
                encoding="utf-8",
            )
        except OSError:
            pass
        return total
    except Exception:
        return None


def _per_session_prefix_tokens(fallback=None):
    """Average prefix tokens cache-written/read per session (real session-start size).

    Prefers the measured session-start baseline from recent JSONL; falls back to
    the supplied value (e.g. the snapshot/current overhead). Always returns >= 1
    to keep the write-fraction division safe.
    """
    _measure_facade = _compat.facade()
    try:
        vals = [int(b["baseline_tokens"]) for b in _measure_facade.get_session_baselines(5)
                if b.get("baseline_tokens")]
        if vals:
            return max(1, sum(vals) // len(vals))
    except Exception:
        pass
    return max(1, int(fallback or 0)) if fallback else 1


def _compound_structural(s, days, fallback_prefix):
    """Forward value of S prefix tokens over the window (opportunity pricing).

    Thin caller of structural_report.price_synthetic: per request, at that
    request's exact model card, S x cache_read on cache hits and S x the write
    class actually paid on misses (session starts, sub-agent starts, cache
    drops). Replaces the family-keyed mix rate and the "S / prefix x ALL cache
    writes" term, which overstated reads ~2.4x and writes ~2.9x. Only local
    sessions count: synced cloud sessions never load this machine's setup.
    `fallback_prefix` is unused, kept for call-site symmetry. None when S<=0.
    """
    if s <= 0:
        return None
    return _compat.facade()._structural_report.price_synthetic(s, days)


def _compute_structural_savings(days=None):
    """Realized structural savings from the per-component trim ledger.

    Thin caller of structural_report.compute (scripts/_measure/structural_*):
    each trim is a dated event (audit action, owner attestation or detected
    shrink), credited for sessions where its component loads, from the trim
    until the item comes back, priced per request. Unrelated prefix growth
    never offsets a trim. Measured-tier trims are in tokens_saved /
    cost_saved_usd; attested ("estimated") and unattributed trims are reported
    in their own bands and never summed here. No trims -> zeros with
    status "no_trims". Never raises.
    """
    return _compat.facade()._structural_report.compute(days=days or 30)


def _unused_skill_overhead_tokens(days=None):
    """Estimate recoverable prefix tokens from skills never used in the window.

    Claude listing savings require transcript evidence, so inventory is not priced.
    """
    _measure_facade = _compat.facade()
    try:
        components = _measure_facade._cached_measure_components()
        skills_info = components.get("skills", {})
        installed = set(skills_info.get("names", []))
        if not installed:
            return 0, 0
        name_to_dir = skills_info.get("name_to_dir", {})
        count = skills_info.get("count", 0) or len(installed)
        avg_per_skill = (skills_info.get("tokens", 0) // max(count, 1)) or _measure_facade.TOKENS_PER_SKILL_APPROX

        used = set()
        if _measure_facade.TRENDS_DB.exists():
            cutoff = (_measure_facade.datetime.now() - _measure_facade.timedelta(days=days)).strftime("%Y-%m-%d")
            conn = _measure_facade._init_trends_db()
            try:
                rows = conn.execute(
                    "SELECT DISTINCT skill FROM skill_daily WHERE date >= ? AND invocations > 0",
                    (cutoff,),
                ).fetchall()
            finally:
                conn.close()
            for (s,) in rows:
                if s in installed:
                    used.add(s)
                elif s in name_to_dir:
                    used.add(name_to_dir[s])
                elif ":" in s:
                    parent = s.split(":")[0]
                    used.add(parent if parent in installed else name_to_dir.get(parent, s))
                else:
                    used.add(s)
        unused = installed - used
        return (0 if _measure_facade.detect_runtime() == "claude" else len(unused) * avg_per_skill), len(unused)
    except Exception:
        return 0, 0


def _compute_structural_potential(days=None):
    """Opportunity: structural $ available if the user prunes recommended waste.

    Distinct from realized savings -- this is what acting on the optimizer's
    recommendations would unlock. Computes the recoverable prefix tokens (unused
    skill frontmatter, the dominant lever) and compounds them with the same
    verified cache formula. Surfaced as "unclaimed" so the dashboard shows the
    money on the table instead of a wall of $0. Labelled "opportunity". Never raises.
    """
    _measure_facade = _compat.facade()
    zero = {
        "recoverable_tokens": 0, "unused_skills": 0, "events": 0, "tokens_saved": 0,
        "cost_saved_usd": 0.0, "read_savings_usd": 0.0, "write_1h_savings_usd": 0.0,
        "write_5m_savings_usd": 0.0, "evidence": "opportunity",
    }
    try:
        s_potential, unused_count = _measure_facade._unused_skill_overhead_tokens(days=days)
        if s_potential <= 0:
            return {**zero, "unused_skills": int(unused_count), "basis": "no measured listing reduction"}
        current_overhead = _measure_facade._current_overhead_tokens() or s_potential
        terms = _measure_facade._compound_structural(s_potential, days, fallback_prefix=current_overhead)
        if not terms:
            return zero
        return {
            **terms,
            "recoverable_tokens": int(s_potential),
            "unused_skills": int(unused_count),
            "evidence": "opportunity",
        }
    except Exception:
        return zero


def _estimate_uncaptured_runtime(days=None, compression=None, savings=None):
    """Estimate runtime compression that happens but is never logged as an event.

    The logged runtime savings are a FLOOR. Compression that runs inside
    sub-agent dispatches is not attributed back to the parent session, so it
    never lands in compression_events. Estimate it conservatively from the
    measured per-session runtime savings times the sub-agent dispatch count
    (each sub-agent carries its own context and compressors), at a 0.5
    attribution haircut. Clearly labelled estimated; shown separately from the
    measured runtime number, never merged into it. Never raises.

    `compression`/`savings` may be passed in by the caller (_get_merged_savings
    already computes both) to avoid re-querying. Never raises.

    Returns {tokens_saved, cost_saved_usd, subagent_dispatches, evidence}.
    """
    _measure_facade = _compat.facade()
    zero = {"tokens_saved": 0, "cost_saved_usd": 0.0, "subagent_dispatches": 0, "evidence": "estimated"}
    try:
        if not _measure_facade.TRENDS_DB.exists():
            return zero
        cutoff = (_measure_facade.datetime.now() - _measure_facade.timedelta(days=days)).strftime("%Y-%m-%d")
        conn = _measure_facade._init_trends_db()
        _measure_facade.ensure_activity_view(conn)
        try:
            # Quality filter removed (quality-filter audit 2026-08-29): the
            # filter shrank session_count from 2983 to 449 on the real ledger,
            # making per_session 6.6x too high. measured_tokens comes from
            # unfiltered compression events, so the denominator must match.
            session_count = conn.execute(
                "SELECT COUNT(DISTINCT id) FROM session_activity WHERE date >= ? "
                "AND input_tokens IS NOT NULL "
                "AND COALESCE(is_sidechain, 0) = 0",
                (cutoff,),
            ).fetchone()[0] or 0
            rows = conn.execute(
                "SELECT subagents_json FROM session_activity "
                "WHERE date >= ? AND subagents_json IS NOT NULL AND subagents_json != '' "
                "AND COALESCE(is_sidechain, 0) = 0",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
        subagent_dispatches = 0
        for (sj,) in rows:
            try:
                sd = _measure_facade.json.loads(sj) if sj else {}
                if isinstance(sd, dict):
                    for v in sd.values():
                        try:
                            subagent_dispatches += int(v)
                        except (TypeError, ValueError):
                            continue
            except (_measure_facade.json.JSONDecodeError, TypeError):
                continue
        if session_count <= 0 or subagent_dispatches <= 0:
            return zero

        # Measured runtime tokens/session as the per-context compression proxy.
        # Reuse the caller's summaries when provided to avoid duplicate queries.
        if compression is None:
            compression = _measure_facade._get_compression_summary(days=days)
        if savings is None:
            savings = _measure_facade._get_savings_summary(days=days)
        measured_tokens = int(compression.get("total_tokens", 0) or 0) + int(savings.get("total_tokens", 0) or 0)
        per_session = measured_tokens / session_count
        attribution = 0.5  # conservative: sub-agents do less of the heavy bash/structure work
        est_tokens = int(subagent_dispatches * per_session * attribution)
        if est_tokens <= 0:
            return zero
        cost_per_mtok = _measure_facade._window_input_rate_per_mtok(days=days)
        return {
            "tokens_saved": est_tokens,
            "cost_saved_usd": round(est_tokens * cost_per_mtok / 1_000_000, 4),
            "subagent_dispatches": subagent_dispatches,
            "evidence": "estimated",
        }
    except Exception:
        return zero


def _estimate_behavioral_savings(days=None):
    """Estimate savings from behavioral coaching that cannot be directly metered.

    Some of Token Optimizer's biggest wins never produce a billable event,
    because the whole point is that the wasteful work never happens. The
    clearest case is LOOP PREVENTION: when loop detection fires it compresses
    the repeated output (counted as runtime), but more importantly it stops a
    runaway loop that would otherwise have burned several more iterations of the
    same tokens. That avoided continuation is never billed, so it never shows up
    as measured savings -- exactly the "we can't measure it so we ignore it"
    blind spot.

    B5: prevented FUTURE iterations are a pure counterfactual (they never
    happened), so we do not fabricate a multiplier. Instead we anchor to what the
    data actually records -- the observed repetition `count=N` in each loop
    event's detail -- for transparency, and estimate the avoided continuation
    conservatively as ONE more looping span (the measured looped volume), i.e. a
    continuation factor of 1.0. A loop caught after N repeats would plausibly have
    run at least one comparable span more before another guard or the context
    limit stopped it, so this is a deliberate floor, not a guess. Shown as its own
    behavioral tier with the observed repetition count, never summed into the
    measured total. Returns {tokens_saved, cost_saved_usd, loop_events,
    prevented_iterations (observed avg count), evidence}. Never raises.

    NOTE: an earlier version multiplied by original/compressed; real data showed
    compressed_tokens is a tiny sentinel (~4 tok), making that ratio meaningless
    (2000x+) -- the count lives in the detail string, not the token columns.
    """
    _measure_facade = _compat.facade()
    zero = {"tokens_saved": 0, "cost_saved_usd": 0.0, "loop_events": 0,
            "prevented_iterations": 0.0, "evidence": "estimated"}
    try:
        if not _measure_facade.TRENDS_DB.exists():
            return zero
        cutoff = (_measure_facade.datetime.now() - _measure_facade.timedelta(days=days)).strftime("%Y-%m-%d")
        conn = _measure_facade._init_trends_db()
        try:
            source = _measure_facade._counted_table(conn, "compression_events")
            row = conn.execute(_measure_facade._LOOP_TOTALS_SQL[source], (cutoff,)).fetchone()
            detail_rows = conn.execute(_measure_facade._LOOP_DETAILS_SQL[source], (cutoff,)).fetchall()
        finally:
            conn.close()
        loop_events = int(row[0] or 0)
        loop_tokens_saved = int(row[1] or 0)
        if loop_events <= 0 or loop_tokens_saved <= 0:
            return zero
        # Observed repetition counts from the detail string (e.g. "count=4"), for
        # transparency only -- this is what HAPPENED, reported alongside the estimate.
        counts = []
        for (d,) in detail_rows:
            m = _measure_facade.re.search(r"count=(\d+)", str(d or ""))
            if m:
                counts.append(int(m.group(1)))
        avg_count = (sum(counts) / len(counts)) if counts else 0.0
        # Conservative continuation factor: one more equivalent looping span.
        est_tokens = int(loop_tokens_saved * _measure_facade._BEHAVIORAL_CONTINUATION_FACTOR)
        cost_per_mtok = _measure_facade._window_input_rate_per_mtok(days=days)
        return {
            "tokens_saved": est_tokens,
            "cost_saved_usd": round(est_tokens * cost_per_mtok / 1_000_000, 4),
            "loop_events": loop_events,
            "prevented_iterations": round(avg_count, 1),
            "evidence": "estimated",
        }
    except Exception:
        return zero


def _estimate_contamination_exit_savings(days=None):
    """B4: empirical avoided-rework from heeding a low-quality nudge (tier 2).

    The flagship behavioral estimate, built on a NATURAL CONTROL GROUP rather than
    an invented multiplier. quality_nudge fires are logged per session; a verified
    follow-through means the user acted (compacted/cleared) -- HEEDED. A fire with
    no follow-through is IGNORED. We compare the per-session rework signal
    (B1 stale-read waste in session_log) between the two cohorts: the cohorts
    differ only in whether the nudge was heeded, so the rework DELTA is the mess a
    heeded session avoided. Reported with both cohort sizes and a confidence label,
    gated behind a minimum sample so a thin cohort never shows a number. Strictly
    estimated tier, never summed into the realized total. Never raises.
    """
    _measure_facade = _compat.facade()
    zero = {"tokens_saved": 0, "cost_saved_usd": 0.0, "heeded_sessions": 0,
            "ignored_sessions": 0, "delta_tokens_per_session": 0,
            "confidence": "insufficient", "evidence": "estimated"}
    try:
        if not _measure_facade.TRENDS_DB.exists():
            return zero
        cutoff_dt = _measure_facade.datetime.now() - _measure_facade.timedelta(days=days)
        cutoff_iso = cutoff_dt.isoformat()
        cutoff_date = cutoff_dt.strftime("%Y-%m-%d")
        conn = _measure_facade._init_trends_db()
        try:
            source = _measure_facade._counted_table(conn, "compression_events")
            fired = conn.execute(_measure_facade._NUDGE_FIRED_SQL[source], (cutoff_iso,)).fetchall()
            heeded = conn.execute(_measure_facade._NUDGE_HEEDED_SQL[source], (cutoff_iso,)).fetchall()
            sl = conn.execute(
                "SELECT jsonl_path, COALESCE(stale_waste_tokens,0) FROM session_log WHERE date >= ?",
                (cutoff_date,),
            ).fetchall()
        finally:
            conn.close()
        fired_ids = {r[0] for r in fired if r[0]}
        if not fired_ids:
            return zero
        heeded_ids = {r[0] for r in heeded if r[0]} & fired_ids
        ignored_ids = fired_ids - heeded_ids
        # Map session_id -> stale waste (jsonl filename stem == session id).
        waste_by_sid = {}
        for path, waste in sl:
            try:
                waste_by_sid[_measure_facade.Path(path).stem] = int(waste or 0)
            except (TypeError, ValueError):
                continue

        def _avg(ids):
            _measure_facade = _compat.facade()
            vals = [waste_by_sid[s] for s in ids if s in waste_by_sid]
            return (sum(vals) / len(vals), len(vals)) if vals else (0.0, 0)

        avg_heeded, n_heeded = _avg(heeded_ids)
        avg_ignored, n_ignored = _avg(ignored_ids)
        if n_heeded < _measure_facade._CONTAMINATION_COHORT_MIN or n_ignored < _measure_facade._CONTAMINATION_COHORT_MIN:
            z = dict(zero)
            z["heeded_sessions"], z["ignored_sessions"] = n_heeded, n_ignored
            return z
        delta = max(0.0, avg_ignored - avg_heeded)  # rework avoided by heeding
        est_tokens = int(delta * n_heeded)
        smaller = min(n_heeded, n_ignored)
        confidence = "high" if smaller >= 15 else "medium" if smaller >= 5 else "low"
        cost_per_mtok = _measure_facade._window_input_rate_per_mtok(days=days)
        return {
            "tokens_saved": est_tokens,
            "cost_saved_usd": round(est_tokens * cost_per_mtok / 1_000_000, 4),
            "heeded_sessions": n_heeded,
            "ignored_sessions": n_ignored,
            "delta_tokens_per_session": int(delta),
            "confidence": confidence,
            "evidence": "estimated",
        }
    except Exception:
        return zero


def _estimate_handover_rerun_savings(days=None):
    """B7: avoided rework from session-continuity handover (tier 2, cohort-grounded).

    Same empirical method as B4 applied to the continuity split: sessions that
    resumed via a restored checkpoint (a checkpoint_restore savings event) vs
    sessions that did not. The per-session rework signal (B1 stale-read waste) is
    compared between cohorts; a lower figure for restored sessions is the rework a
    handover avoided. Reported in the estimated tier with cohort sizes + a
    confidence label, gated behind a minimum sample, never summed into realized.
    Selection bias is possible (restored sessions may differ), so it is labelled
    estimated and shown alongside its sample sizes, not as a hard number. Never raises.
    """
    _measure_facade = _compat.facade()
    zero = {"tokens_saved": 0, "cost_saved_usd": 0.0, "restored_sessions": 0,
            "baseline_sessions": 0, "delta_tokens_per_session": 0,
            "confidence": "insufficient", "evidence": "estimated"}
    try:
        if not _measure_facade.TRENDS_DB.exists():
            return zero
        cutoff_dt = _measure_facade.datetime.now() - _measure_facade.timedelta(days=days)
        conn = _measure_facade._init_trends_db()
        try:
            restored = conn.execute(
                _measure_facade._CHECKPOINT_RESTORED_SQL[_measure_facade._counted_table(conn, "savings_events")],
                (cutoff_dt.isoformat(),),
            ).fetchall()
            sl = conn.execute(
                "SELECT jsonl_path, COALESCE(stale_waste_tokens,0) FROM session_log WHERE date >= ?",
                (cutoff_dt.strftime("%Y-%m-%d"),),
            ).fetchall()
        finally:
            conn.close()
        restored_ids = {r[0] for r in restored if r[0]}
        waste_by_sid = {}
        for path, waste in sl:
            try:
                waste_by_sid[_measure_facade.Path(path).stem] = int(waste or 0)
            except (TypeError, ValueError):
                continue
        if not waste_by_sid:
            return zero
        restored_vals = [w for s, w in waste_by_sid.items() if s in restored_ids]
        baseline_vals = [w for s, w in waste_by_sid.items() if s not in restored_ids]
        n_restored, n_baseline = len(restored_vals), len(baseline_vals)
        if n_restored < _measure_facade._CONTAMINATION_COHORT_MIN or n_baseline < _measure_facade._CONTAMINATION_COHORT_MIN:
            z = dict(zero)
            z["restored_sessions"], z["baseline_sessions"] = n_restored, n_baseline
            return z
        avg_restored = sum(restored_vals) / n_restored
        avg_baseline = sum(baseline_vals) / n_baseline
        delta = max(0.0, avg_baseline - avg_restored)
        est_tokens = int(delta * n_restored)
        smaller = min(n_restored, n_baseline)
        confidence = "high" if smaller >= 15 else "medium" if smaller >= 5 else "low"
        cost_per_mtok = _measure_facade._window_input_rate_per_mtok(days=days)
        return {
            "tokens_saved": est_tokens,
            "cost_saved_usd": round(est_tokens * cost_per_mtok / 1_000_000, 4),
            "restored_sessions": n_restored,
            "baseline_sessions": n_baseline,
            "delta_tokens_per_session": int(delta),
            "confidence": confidence,
            "evidence": "estimated",
        }
    except Exception:
        return zero


def _estimate_retrieval_serve_savings(days=None):
    """U-D: avoided exploratory search when a TO retrieval / continuity serve
    points the model straight at the right context (ESTIMATED tier).

    The user's example: a proactive hint or an archived-result serve sends an
    agent directly to the answer instead of grep-diving through many files. That
    avoided search is real tokens, but it is a counterfactual ("would have
    searched"), so it lives in the estimated tier, never the hard counted total.

    Method (mirrors the accepted B7 cohort approach): the served cohort is
    sessions that received a TO retrieval/continuity serve (a checkpoint_restore
    or tool_archive savings event); the baseline cohort is sessions without one.
    Compare Read-tool counts (session_log.tool_calls_json): a session steered to
    the answer does fewer exploratory reads. The per-session read delta x a
    conservative average-read-token estimate is the avoided search. Distinct from
    B7 handover_rerun, which measures avoided *stale re-reads*; this measures
    avoided *exploratory first reads*. Capped per session, sample-gated, labelled
    estimated, never summed into realized. Selection bias is possible (served
    sessions may differ), so it is shown with its cohort sizes, not as a hard
    number. Never raises.
    """
    _measure_facade = _compat.facade()
    zero = {"tokens_saved": 0, "cost_saved_usd": 0.0, "served_sessions": 0,
            "baseline_sessions": 0, "avoided_reads_per_session": 0,
            "confidence": "insufficient", "evidence": "estimated"}
    try:
        if not _measure_facade.TRENDS_DB.exists():
            return zero
        cutoff_dt = _measure_facade.datetime.now() - _measure_facade.timedelta(days=days)
        conn = _measure_facade._init_trends_db()
        _measure_facade.ensure_activity_view(conn)
        try:
            served = conn.execute(
                _measure_facade._RETRIEVAL_SERVED_SQL[_measure_facade._counted_table(conn, "savings_events")],
                (cutoff_dt.isoformat(),),
            ).fetchall()
            sl = conn.execute(
                "SELECT jsonl_path, tool_calls_json FROM session_activity "
                "WHERE date >= ? AND tool_calls_json IS NOT NULL",
                (cutoff_dt.strftime("%Y-%m-%d"),),
            ).fetchall()
        finally:
            conn.close()
        # reads_by_sid is keyed by the JSONL stem (= session UUID), so the served
        # cohort must be matched by session_uuid (the join key), not the raw
        # session_id -- many savings rows carry short agent-ids that never match a
        # UUID stem, which would misclassify served sessions as baseline and
        # OVER-state the avoided-read delta. Include both keys to be safe.
        served_ids = set()
        for su, si in served:
            if su:
                served_ids.add(su)
            if si:
                served_ids.add(si)
        reads_by_sid = {}
        for path, tj in sl:
            try:
                sid = _measure_facade.Path(path).stem
                calls = _measure_facade.json.loads(tj)
                reads_by_sid[sid] = int(calls.get("Read", 0) or 0)
            except (TypeError, ValueError, _measure_facade.json.JSONDecodeError):
                continue
        if not reads_by_sid:
            return zero
        served_vals = [r for s, r in reads_by_sid.items() if s in served_ids]
        baseline_vals = [r for s, r in reads_by_sid.items() if s not in served_ids]
        n_served, n_baseline = len(served_vals), len(baseline_vals)
        if n_served < _measure_facade._CONTAMINATION_COHORT_MIN or n_baseline < _measure_facade._CONTAMINATION_COHORT_MIN:
            z = dict(zero)
            z["served_sessions"], z["baseline_sessions"] = n_served, n_baseline
            return z
        avg_served = sum(served_vals) / n_served
        avg_baseline = sum(baseline_vals) / n_baseline
        # Avoided reads per served session, conservatively capped.
        avoided = min(max(0.0, avg_baseline - avg_served), float(_measure_facade._RETRIEVAL_AVOIDED_READS_CAP))
        est_tokens = int(avoided * n_served * _measure_facade._AVG_EXPLORATORY_READ_TOKENS)
        smaller = min(n_served, n_baseline)
        confidence = "high" if smaller >= 15 else "medium" if smaller >= 5 else "low"
        cost_per_mtok = _measure_facade._window_input_rate_per_mtok(days=days)
        return {
            "tokens_saved": est_tokens,
            "cost_saved_usd": round(est_tokens * cost_per_mtok / 1_000_000, 4),
            "served_sessions": n_served,
            "baseline_sessions": n_baseline,
            "avoided_reads_per_session": round(avoided, 2),
            "confidence": confidence,
            "evidence": "estimated",
        }
    except Exception:
        return zero


def _sum_tool_calls(days=None):
    """Return {tool_name: total_calls} summed across session_log.tool_calls_json."""
    _measure_facade = _compat.facade()
    out = {}
    try:
        if not _measure_facade.TRENDS_DB.exists():
            return out
        cutoff = (_measure_facade.datetime.now() - _measure_facade.timedelta(days=days)).strftime("%Y-%m-%d")
        conn = _measure_facade._init_trends_db()
        _measure_facade.ensure_activity_view(conn)
        try:
            rows = conn.execute(
                "SELECT tool_calls_json FROM session_activity "
                "WHERE date >= ? AND tool_calls_json NOT IN ('', '{}') AND tool_calls_json IS NOT NULL",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
        for (tj,) in rows:
            for k, v in _measure_facade._safe_json_dict(tj).items():
                try:
                    out[k] = out.get(k, 0) + int(v)
                except (TypeError, ValueError):
                    continue
    except (_measure_facade.sqlite3.Error, OSError):
        pass
    return out


def _estimate_output_waste(days=None):
    """Estimate avoidable OUTPUT from full-file Writes that could have been Edits.

    Output is the priciest token class ($25/MTok on Opus, 5x input). A full-file
    Write re-emits the entire file; a targeted Edit emits only the changed lines.
    We estimate avoidable output as a conservative share of Write calls times a
    typical per-rewrite output delta, priced at the OUTPUT rate. This is a
    coaching OPPORTUNITY (use Edit over Write), never a forced output cap, which
    would risk truncating real work. OPPORTUNITY tier (avoidable output you COULD
    save by using Edit over Write); not a realized saving, never summed into the
    realized total. Never raises.

    Returns {writes, edits, avoidable_writes, tokens_saved, cost_saved_usd, evidence}.
    """
    _measure_facade = _compat.facade()
    zero = {"writes": 0, "edits": 0, "avoidable_writes": 0, "tokens_saved": 0,
            "cost_saved_usd": 0.0, "evidence": "opportunity"}
    try:
        tools = _measure_facade._sum_tool_calls(days=days)
        writes = int(tools.get("Write", 0) or 0)
        edits = int(tools.get("Edit", 0) or 0)
        if writes <= 0:
            return {**zero, "edits": edits}
        avoidable = int(writes * _measure_facade._AVOIDABLE_WRITE_FRACTION)
        if avoidable <= 0:
            return {**zero, "writes": writes, "edits": edits}
        tokens_saved = avoidable * _measure_facade._AVOIDABLE_WRITE_OUTPUT_TOKENS
        # Output rate for the active model (output is ~5x input).
        try:
            output_rate = _measure_facade._get_model_cost(_measure_facade._resolve_session_model(), 0, 1_000_000)
        except Exception:
            output_rate = 25.0
        return {
            "writes": writes,
            "edits": edits,
            "avoidable_writes": avoidable,
            "tokens_saved": tokens_saved,
            "cost_saved_usd": round(tokens_saved * output_rate / 1_000_000, 4),
            "evidence": "opportunity",
        }
    except Exception:
        return zero


def _estimate_cache_drop_savings(days=None):
    """Cache rebuilds after idle gaps (the 'coffee-break reload'), in dollars.

    Reads the same engine as cache health (`_cache_ttl_waste_cached`), so the
    fold and the health check never disagree. A rebuild is a turn that, after
    a gap longer than the cache TTL the user's own writes show (1 hour for a
    Claude Code user writing 1h caches, 5 minutes for a 5m user), re-wrote the
    prefix the previous turn had cached. Each one is priced on the prefix it
    actually rebuilt (min of the resume write and the prior cached prefix), at
    that turn's 1h/5m write rate for its model, minus the cache read it would
    have cost with the cache alive. Only rebuilds whose resume turn falls in
    the window count, and a transcript copied into two directories counts
    once. Dollars are at API rates. Never raises.

    Returns {drop_sessions, est_drops, prefix_tokens, rebuilt_tokens,
    ttl_seconds, cost_saved_usd, by_class, rate_basis, price_basis,
    unpriced_share, evidence}. `est_drops * prefix_tokens` approximates
    `rebuilt_tokens` (the average rebuilt prefix); prefer rebuilt_tokens.
    OPPORTUNITY tier: rebuilds that already happened, avoidable by resuming
    inside the TTL. Never summed into the realized total.
    """
    _measure_facade = _compat.facade()
    zero = {"drop_sessions": 0, "est_drops": 0, "prefix_tokens": 0, "rebuilt_tokens": 0,
            "ttl_seconds": None, "cost_saved_usd": 0.0, "evidence": "opportunity"}
    try:
        health = _measure_facade._cache_ttl_waste_cached(days=days) or {}
        rebuild = health.get("rebuild") or {}
        events = int(rebuild.get("events", 0) or 0)
        tokens = int(rebuild.get("tokens", 0) or 0)
        if events <= 0 or tokens <= 0:
            return zero
        classes = rebuild.get("tokens_by_class") or {}
        write_usd = float(rebuild.get("write_usd", 0.0) or 0.0)
        read_usd = float(rebuild.get("read_instead_usd", 0.0) or 0.0)
        by_class = {}
        for name in ("cache_write_1h", "cache_write_5m"):
            n = int(classes.get(name, 0) or 0)
            if n > 0:
                by_class[name] = {"tokens": n, "usd": round(write_usd * n / tokens, 4)}
        by_class["cache_read_instead"] = {"tokens": tokens, "usd": round(-read_usd, 4)}
        return {
            "drop_sessions": int(rebuild.get("sessions", 0) or 0),
            "est_drops": events,
            "prefix_tokens": int(round(tokens / events)),
            "rebuilt_tokens": tokens,
            "ttl_seconds": rebuild.get("ttl_seconds"),
            "cost_saved_usd": round(float(rebuild.get("cost_usd", 0.0) or 0.0), 4),
            "by_class": by_class,
            "rate_basis": "observed_turns",
            "price_basis": "api_rates",
            # Models without a rate card are never priced (they add nothing).
            "unpriced_share": 0.0,
            "evidence": "opportunity",
        }
    except Exception:
        return zero


def _session_token_vector(row):
    """Decompose one session_log row into billed token classes.

    Returns (fresh_input, cache_write, cache_read, output). `input_tokens` already
    stores total billed input (fresh + cache_read + cache_write), and
    `cache_hit_rate = cache_read / input`, so this reconstructs each class exactly.
    Row order: (input_tokens, output_tokens, cache_create_5m, cache_create_1h, cache_hit_rate).
    """
    _measure_facade = _compat.facade()
    inp = float(row[0] or 0)
    out = float(row[1] or 0)
    cw = float((row[2] or 0) + (row[3] or 0))
    # M2 (defensive): guard against corrupt/negative cache_create columns.
    cw = max(0.0, cw)
    # A corrupt/recomputed row can report cache_create > input_tokens. Clamp so the
    # class split can never over-count (fi + cw + cr <= inp is preserved).
    cw = min(cw, inp)
    hit = min(1.0, max(0.0, float(row[4] or 0)))
    cr = max(0.0, inp * hit)
    out = max(0.0, out)
    fresh_raw = inp * (1 - hit) - cw
    fi = max(0.0, fresh_raw)
    # cache_hit_rate is a rounded float; cache_create is an integer. Rounding drift can
    # push fresh slightly negative. Absorb that gap back into cache_read (the cheapest,
    # most conservative class) so fi + cw + cr == inp exactly and no tokens are dropped.
    if fresh_raw < 0:
        cr = max(0.0, cr + fresh_raw)
    return (fi, cw, cr, out)


def _spent_daily_usage(parent_raw, combined_raw):
    """Pick the daily-usage map behind a top-level (parent-only) figure.

    parent_daily_usage_json is the parent transcript's own per-day buckets;
    daily_usage_json merges delegated subagent requests too. Rows collected
    before parent provenance existed fall back to the combined map, which must
    be labelled rather than certified as top-level. Returns
    (raw, provenance) with provenance in {"parent", "combined", "none"}; the
    caller parses and validates `raw` itself.
    """
    _measure_facade = _compat.facade()
    if parent_raw is not None:
        return parent_raw, "parent"
    if combined_raw is not None:
        return combined_raw, "combined"
    return None, "none"


def _dashboard_spent_token_basis(conn, days=None):
    """Deduplicated top-level fresh input + output on actual activity days.

    Unknown dates have no invented window placement. Report incomplete coverage
    even if an undated row's ingestion happens to fall outside this window.
    Rows without parent provenance contribute their combined parent+delegated
    usage but are counted and labelled, never silently certified top-level.
    """
    _measure_facade = _compat.facade()
    cutoff = (_measure_facade.datetime.now() - _measure_facade.timedelta(days=days)).date().isoformat()
    columns = {r[1] for r in conn.execute("PRAGMA table_info(session_log)")}
    parent = "parent_daily_usage_json" if "parent_daily_usage_json" in columns else "NULL"
    daily = "daily_usage_json" if "daily_usage_json" in columns else "NULL"
    side = "COALESCE(is_sidechain,0)" if "is_sidechain" in columns else "0"
    rows = conn.execute(f"SELECT {parent}, {daily} FROM session_log WHERE {side}=0").fetchall()
    total, incomplete, combined_rows = 0, 0, 0
    for parent_raw, combined_raw in rows:
        raw, provenance = _measure_facade._spent_daily_usage(parent_raw, combined_raw)
        try:
            usage = _measure_facade.json.loads(raw) if raw is not None else None
        except (TypeError, ValueError):
            usage = None
        valid = isinstance(usage, dict)
        usage = usage if valid else {}
        dated = _measure_facade._activity_days(usage)
        if not valid or (not usage and provenance != "parent") or any(day not in dated for day in usage):
            incomplete += 1
        in_window = False
        for day in dated:
            if day < cutoff:
                continue
            in_window = True
            for bucket in usage[day].values():
                if isinstance(bucket, dict):
                    total += _measure_facade._bucket_int(bucket.get("fresh_input")) + _measure_facade._bucket_int(bucket.get("output"))
        if provenance == "combined" and in_window:
            combined_rows += 1
    return {"tokens": int(total),
            "basis": _measure_facade._DASHBOARD_SPENT_COMBINED_BASIS if combined_rows else _measure_facade._DASHBOARD_SPENT_TOKEN_BASIS,
            "complete": incomplete == 0 and combined_rows == 0,
            "fallback_rows": incomplete,
            "combined_fallback_rows": combined_rows}


def _winsorized_mean_session(rows, cap_pct=None):
    """Winsorized-mean per-session token vector over `rows`.

    Caps each session's TOTAL tokens at the `cap_pct` percentile of the window (by
    scaling its whole class vector down proportionally), then averages across ALL
    sessions. This neutralizes one-off bulk operations (a single full-codebase index
    or vault import that bills hundreds of millions of cache-read tokens in one
    session) without discarding the many genuine heavy working sessions (where Token
    Optimizer's value lands). It is deliberately NOT a median or trimmed mean: this
    workload is heavy-tailed (the mean session is many times the median), so a median
    anchor would discard real heavy work and badly understate; winsorizing only the
    extreme tail is a safety rail against a single pathological session, not a thumb
    on the scale.

    The cap index is a true nearest-rank percentile: `round((n-1) * cap_pct)`. The
    SAME tail fraction is applied at every sample size and on both sides of the
    before/after comparison, so neither side is over-capped relative to the other.
    Below ~1/(1-cap_pct) sessions the index rounds to the max, i.e. nothing is capped
    and the result is a plain mean -- the correct behavior, since the top 1% of a
    handful of sessions is not a meaningful tail to clip.

    Each row is (input_tokens, output_tokens, cache_create_5m, cache_create_1h,
    cache_hit_rate). Returns (vec4, n_used, n_total); vec4 is
    (fresh_input, cache_write, cache_read, output). n_used == n_total (capping does
    not drop rows); the tuple shape is (vec4, n_used, n_total).
    """
    _measure_facade = _compat.facade()
    n = len(rows)
    if n == 0:
        return ((0.0, 0.0, 0.0, 0.0), 0, 0)
    vecs = [_measure_facade._session_token_vector(r) for r in rows]
    totals = sorted(sum(v) for v in vecs)
    cap = totals[min(n - 1, max(0, int(round((n - 1) * cap_pct))))]
    acc = [0.0, 0.0, 0.0, 0.0]
    for v in vecs:
        total = sum(v)
        scale = (cap / total) if (total > cap and total > 0) else 1.0
        for j in range(4):
            acc[j] += v[j] * scale
    return (tuple(a / n for a in acc), n, n)


def _compute_baseline_state():
    """Compute this user's robust pre-TO baseline from their own history, or None.

    The baseline is the typical session at the user's earliest window: skip the
    first `_BASELINE_ONBOARDING_DAYS` (install-day setup), take the next
    `_BASELINE_EARLY_WINDOW_DAYS` (~a month, so the per-session mean is stable), and
    winsorize the heaviest 1% of sessions (cap one-off bulk ops; keep all real heavy
    work, see `_winsorized_mean_session`). This is the user's OWN pre-optimization
    era measured from their OWN real sessions, so it works for existing users who
    installed before any baseline was captured, not just fresh installs. Returns None
    when the window hasn't accrued `_BASELINE_MIN_STABLE_SESSIONS` yet (a brand-new
    install) so we never freeze a half-formed or fake anchor. Never raises.

    Returns a dict: {typical_session: {fresh_input, cache_write, cache_read, output},
    opus_share, opus_share_source, window {start,end,sessions_used,sessions_total,
    elapsed}, method, winsor_pct, structural_overhead_tokens, source}.
    """
    _measure_facade = _compat.facade()
    try:
        if not _measure_facade.TRENDS_DB.exists():
            return None
        conn = _measure_facade._init_trends_db()
        _measure_facade.ensure_activity_view(conn)
        try:
            rows = conn.execute(
                "SELECT date, id, input_tokens, output_tokens, cache_create_5m_tokens, "
                "cache_create_1h_tokens, cache_hit_rate FROM session_activity "
                # Human sessions only -- the baseline anchor must count the same population
                # as _baseline_progress (the "ready" signal) and _mix_from_session_rows, so
                # heavy sidechain/subagent volume can't inflate the frozen typical-session
                # vector or qualify the window before the human-session count is actually met.
                # Quality filter REMOVED (audit 2026-08-29): the filter inflated the
                # frozen anchor by 69% on the real ledger (mean input 28.4M filtered vs
                # 16.8M all). The winsorization already trims outliers; the filter was a
                # second population-shrinking gate that hid the real session-count collapse.
                "WHERE input_tokens IS NOT NULL AND COALESCE(is_sidechain, 0) = 0 "
                "ORDER BY date, id"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return None
        first = _measure_facade.datetime.fromisoformat(str(rows[0][0])[:10])
        win_start = first + _measure_facade.timedelta(days=_measure_facade._BASELINE_ONBOARDING_DAYS)
        win_end = win_start + _measure_facade.timedelta(days=_measure_facade._BASELINE_EARLY_WINDOW_DAYS)
        vectors = {}
        for row in rows:
            if not win_start <= _measure_facade.datetime.fromisoformat(str(row[0])[:10]) < win_end:
                continue
            acc = vectors.setdefault(row[1], [0.0] * 5)
            for index in range(4):
                acc[index] += row[index + 2] or 0
            acc[4] += (row[2] or 0) * (row[6] or 0)
        window_rows = [(*v[:4], v[4] / v[0] if v[0] else 0.0) for v in vectors.values()]
        if len(window_rows) < _measure_facade._BASELINE_MIN_STABLE_SESSIONS:
            return None
        vec, used, total = _measure_facade._winsorized_mean_session(window_rows)
        # Guard against a corrupt DB row poisoning the frozen anchor with a non-finite
        # value (json would persist `Infinity`, then int() raises later). Bail to None.
        if not all(_measure_facade.math.isfinite(x) for x in vec):
            return None

        # Pre-TO Opus share: explicit per-user baseline if set, else the earliest measured
        # mix over the SAME window as the token vector (aligned so the blend is honest;
        # a 14-day mix on a 30-day vector would overstate if the user routed off Opus in
        # weeks 3-4).
        baseline = _measure_facade._pretool_baseline_mix()
        if baseline:
            model_shares = dict(baseline)  # {"opus": x, "sonnet": 1-x}
            opus_share = float(baseline.get("opus", 0.0))
            opus_source = "pretool_baseline"
        else:
            early = _measure_facade._earliest_model_mix(lookback_days=_measure_facade._BASELINE_EARLY_WINDOW_DAYS)
            model_shares = dict((early.get("shares") or {})) if early else {}
            opus_share = float(model_shares.get("opus", 0.0))
            opus_source = "robust_earliest"

        struct_overhead = 0
        try:
            snap = _measure_facade.SNAPSHOT_DIR / "snapshot_before.json"
            if snap.exists():
                struct_overhead = int(
                    _measure_facade.json.loads(snap.read_text(encoding="utf-8"))
                    .get("totals", {}).get("estimated_total", 0) or 0
                )
        except (_measure_facade.json.JSONDecodeError, OSError, ValueError, TypeError):
            struct_overhead = 0

        return {
            "version": _measure_facade._BASELINE_VERSION,
            "activity_basis": 1,
            "typical_session": {
                "fresh_input": round(vec[0], 1), "cache_write": round(vec[1], 1),
                "cache_read": round(vec[2], 1), "output": round(vec[3], 1),
            },
            "opus_share": round(opus_share, 4),
            "opus_share_source": opus_source,
            "model_shares": {k: round(float(v), 4) for k, v in model_shares.items() if v},
            "window": {
                "start": win_start.strftime("%Y-%m-%d"),
                "end": win_end.strftime("%Y-%m-%d"),
                "sessions_used": used, "sessions_total": total,
                "elapsed": _measure_facade.datetime.now() >= win_end,
            },
            "method": "winsorized_mean",
            "winsor_pct": _measure_facade._BASELINE_WINSOR_PCT,
            "structural_overhead_tokens": struct_overhead,
        }
    except (_measure_facade.sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
        return None


def _write_baseline_state(path, data):
    """Atomically write a baseline file, 0600, tempfile + os.replace.

    Same discipline as the original freeze path: a crash mid-write must never
    leave a half-written anchor, and the anchor must not be world-readable.
    """
    _measure_facade = _compat.facade()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = _measure_facade.tempfile.mkstemp(dir=str(path.parent), prefix=".baseline_state-",
                               suffix=".tmp")
    try:
        with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as f:
            _measure_facade.json.dump(data, f, indent=2, default=str)
        _measure_facade.os.chmod(tmp, 0o600)
        _measure_facade.os.replace(tmp, str(path))
        tmp = None
    finally:
        if tmp is not None:
            try:
                _measure_facade.os.unlink(tmp)
            except OSError:
                pass


def _baseline_is_well_formed(data):
    """True when `data` is a baseline we can price and compare."""
    _measure_facade = _compat.facade()
    if not isinstance(data, dict):
        return False
    if data.get("version") not in _measure_facade._BASELINE_KNOWN_VERSIONS:
        return False
    ts = data.get("typical_session")
    if not isinstance(ts, dict):
        return False
    for k in ("fresh_input", "cache_write", "cache_read", "output"):
        v = ts.get(k)
        if not isinstance(v, (int, float)) or v < 0:
            return False
    shares = data.get("model_shares")
    return isinstance(shares, dict) and bool(shares)


def _baseline_priced_cost(data):
    """Cost of one `typical_session` at the baseline's OWN model shares.

    Used only to COMPARE two baselines (adoption tie-break, shrink guard), never
    to report a figure, so it stays on PRICING_TIERS and never invents a rate.
    """
    _measure_facade = _compat.facade()
    try:
        ts = data["typical_session"]
        tier = _measure_facade.PRICING_TIERS.get(_measure_facade._load_pricing_tier(), _measure_facade.PRICING_TIERS["anthropic"])
        models = tier.get("claude_models", {})
        total = 0.0
        for model, share in (data.get("model_shares") or {}).items():
            if not share:
                continue
            norm = _measure_facade._normalize_model_name(str(model)) or "sonnet"
            r = models.get(norm) or models.get("sonnet") or {}
            total += float(share) * (
                ts["fresh_input"] * float(r.get("input", 3.0))
                + ts["output"] * float(r.get("output", 15.0))
                + ts["cache_read"] * float(r.get("cache_read", 0.3))
                + ts["cache_write"] * float(r.get("cache_write", 3.75))
            ) / 1_000_000
        return total
    except (KeyError, TypeError, ValueError):
        return 0.0


def _migrate_baseline(data):
    """Bring an older but readable baseline up to `_BASELINE_VERSION`.

    Returns the migrated dict, or None when no migration path exists. NEVER
    mutates the caller's dict, and never invents numbers: a migration that
    cannot be done from the fields present returns None so the caller falls
    through to the next candidate rather than fabricating a shape.
    """
    _measure_facade = _compat.facade()
    if not isinstance(data, dict):
        return None
    v = data.get("version")
    if v == _measure_facade._BASELINE_VERSION:
        return dict(data)
    if v == 3:
        # v3 -> v4 added `structural_overhead_tokens`. Absent means "not measured
        # at capture time", which is 0 contribution, NOT a smaller baseline.
        out = dict(data)
        out.setdefault("structural_overhead_tokens", 0)
        out["version"] = _measure_facade._BASELINE_VERSION
        out["migrated_from_version"] = 3
        return out
    return None


def _baseline_adoption_candidates():
    """Well-formed baselines belonging to THIS user, from sibling plugin identities.

    A plugin re-install under a different marketplace id gets a fresh data dir, so
    the frozen baseline written by the previous identity is ORPHANED, not deleted.
    Re-capturing from scratch months later reads a thinned history and produces a
    smaller anchor, which silently collapses every reported saving. A user's
    baseline moved from a $14.73/session capture to a $9.87 one exactly this way.

    Security: only paths under the plugin-data base (already vetted by
    `plugin_env._is_safe_subdir`) or the legacy script-install backup dir. Regular
    files only, never symlinks, size-capped, and the resolved path must still be
    inside the allowed base so a symlinked directory cannot escape it.
    """
    _measure_facade = _compat.facade()
    out = []
    if _measure_facade.os.environ.get("TOKEN_OPTIMIZER_BASELINE_ADOPT", "").strip() == "0":
        return out

    def _consider(path, allowed_base):
        _measure_facade = _compat.facade()
        try:
            if not path.is_file() or path.is_symlink():
                return
            if path.stat().st_size > _measure_facade._BASELINE_MAX_BYTES:
                return
            # Confinement. `plugin_env._is_safe_subdir` requires a DIRECTORY, so it
            # guards the containing dir; the file itself is then checked against the
            # RESOLVED base. Both sides must be resolved: on macOS the plugin-data
            # base commonly sits behind a symlinked component (/tmp -> /private/tmp),
            # so comparing a resolved file against an unresolved base rejects every
            # legitimate candidate and adoption silently never fires.
            if allowed_base is not None:
                if not _measure_facade.plugin_env._is_safe_subdir(path.parent, allowed_base):
                    return
                real = path.resolve(strict=True)
                base_real = _measure_facade.Path(allowed_base).resolve(strict=False)
                if not real.is_relative_to(base_real):
                    return
            data = _measure_facade.json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, _measure_facade.json.JSONDecodeError, AttributeError):
            return
        if _measure_facade._baseline_is_well_formed(data):
            out.append((path, data))

    try:
        base = _measure_facade.plugin_env._PLUGIN_DATA_BASE
        for sibling in sorted(_measure_facade.plugin_env.find_sibling_plugin_data_dirs(
                active=_measure_facade._RESOLVED_PLUGIN_DATA)):
            _consider(sibling / "data" / "baseline_state.json", base)
            for variant in sorted(sibling.glob("data.*")):
                if variant.is_symlink() or not variant.is_dir():
                    continue
                _consider(variant / "baseline_state.json", base)
    except (OSError, AttributeError):
        pass

    try:
        legacy_base = _measure_facade.RUNTIME_DIR / "_backups"
        _consider(legacy_base / "token-optimizer" / "baseline_state.json", legacy_base)
    except (OSError, AttributeError):
        pass
    return out


def _adopt_orphaned_baseline():
    """Adopt the best orphaned baseline instead of re-capturing from scratch.

    Selection: the OLDEST `captured_at`, because a baseline describes the earliest
    pre-TO window and the earliest capture sits closest to that era. Ties break to
    the LARGER priced cost, never the smaller -- an adoption must not be the route
    by which a user quietly gets a worse anchor. A candidate missing `captured_at`
    sorts last.

    Returns the adopted dict (with provenance stamped) or None.
    """
    _measure_facade = _compat.facade()
    cands = _measure_facade._baseline_adoption_candidates()
    if not cands:
        return None

    def _key(item):
        _measure_facade = _compat.facade()
        _p, d = item
        raw = d.get("captured_at")
        try:
            ts = _measure_facade.datetime.fromisoformat(str(raw)).timestamp()
        except (TypeError, ValueError):
            ts = float("inf")          # unknown capture time sorts LAST
        return (ts, -_measure_facade._baseline_priced_cost(d))

    for path, data in sorted(cands, key=_key):
        migrated = _measure_facade._migrate_baseline(data)
        if migrated is None:
            continue
        migrated["adopted_from"] = str(path)
        migrated["adopted_at"] = _measure_facade.datetime.now().isoformat()
        migrated["adoption_rule"] = "oldest_captured_at"
        return migrated
    return None


def _baseline_shrink_is_material(incumbent, candidate):
    """True when `candidate` is materially SMALLER than `incumbent`.

    Asymmetric on purpose. A baseline drifting DOWN silently deletes reported
    savings, so it needs a human; drifting up does not. Compares priced cost and
    cache_read (the term that dominates a session, ~2/3 of it) and trips on
    either.
    """
    _measure_facade = _compat.facade()
    try:
        inc_cost = _measure_facade._baseline_priced_cost(incumbent)
        cand_cost = _measure_facade._baseline_priced_cost(candidate)
        if inc_cost > 0 and cand_cost < inc_cost * (1 - _measure_facade._BASELINE_SHRINK_COST_PCT):
            return True
        inc_cr = float((incumbent.get("typical_session") or {}).get("cache_read", 0) or 0)
        cand_cr = float((candidate.get("typical_session") or {}).get("cache_read", 0) or 0)
        if inc_cr > 0 and cand_cr < inc_cr * (1 - _measure_facade._BASELINE_SHRINK_CACHE_READ_PCT):
            return True
    except (TypeError, ValueError):
        return False
    return False


def _get_baseline_state(freeze=None):
    """Return this user's frozen baseline, computing + freezing it once when ready.

    Reads `baseline_state.json` if already frozen. Otherwise computes the robust
    baseline; if it is ready AND its early window has fully elapsed (so we are not
    freezing a partial window), writes it once (atomic, 0600) and returns it. Until
    then returns the live computation (or None), so a new user sees no transformation
    until a real, stable anchor exists. Never raises.
    """
    _measure_facade = _compat.facade()
    bpath = _measure_facade.SNAPSHOT_DIR / "baseline_state.json"
    incumbent = None
    try:
        if bpath.exists():
            data = _measure_facade.json.loads(bpath.read_text(encoding="utf-8"))
            if (isinstance(data, dict) and data.get("typical_session")
                    and data.get("version") == _measure_facade._BASELINE_VERSION and data.get("activity_basis") == 1):
                return data
            # Never unlink. The old code deleted a mismatched baseline and
            # re-captured, so a single bump of _BASELINE_VERSION would destroy EVERY
            # user's only record of their pre-TO cost, on our release schedule, with
            # no way back. Migrate forward when we can; otherwise RETAIN the file
            # under a versioned name so it stays recoverable and can still act as
            # the incumbent for the shrink guard below.
            if isinstance(data, dict):
                migrated = _measure_facade._migrate_baseline(data) if data.get("activity_basis") == 1 else None
                if migrated is not None and _measure_facade._baseline_is_well_formed(migrated):
                    try:
                        _measure_facade._write_baseline_state(bpath, migrated)
                    except OSError:
                        pass
                    return migrated
                if _measure_facade._baseline_is_well_formed(data) and data.get("activity_basis") == 1:
                    incumbent = data
                try:
                    retained = bpath.with_name(
                        f"baseline_state.v{data.get('version', 'unknown')}.json")
                    if not retained.exists():
                        _measure_facade.shutil.copy2(str(bpath), str(retained))
                except (OSError, _measure_facade.shutil.Error):
                    pass
    except (_measure_facade.json.JSONDecodeError, OSError, ValueError):
        pass

    # Before re-capturing from scratch, look for an orphaned baseline left
    # behind by a previous plugin identity. A re-install under a new marketplace id
    # gets a fresh data dir; the old anchor is stranded, not deleted. Re-capturing
    # months later reads a thinned history and yields a smaller, wronger anchor.
    adopted = _measure_facade._adopt_orphaned_baseline()
    if adopted is not None and adopted.get("activity_basis") == 1:
        if incumbent is not None and _measure_facade._baseline_shrink_is_material(incumbent, adopted):
            adopted = None          # never adopt DOWNWARD past the guard
        else:
            try:
                _measure_facade._write_baseline_state(bpath, adopted)
            except OSError:
                pass
            return adopted

    state = _measure_facade._compute_baseline_state()
    if not state:
        return None

    # Refuse to silently replace a good anchor with a materially smaller one.
    # A shrinking baseline means the HISTORY thinned, not that the user got cheaper.
    # Keep the incumbent, park the candidate beside it, and surface it rather than
    # letting the reported saving quietly collapse.
    if incumbent is not None and _measure_facade._baseline_shrink_is_material(incumbent, state):
        try:
            cand = bpath.with_name("baseline_state.candidate.json")
            _measure_facade._write_baseline_state(cand, dict(
                state,
                rejected_reason="materially smaller than the existing baseline",
                rejected_at=_measure_facade.datetime.now().isoformat(),
                incumbent_cost_usd=round(_measure_facade._baseline_priced_cost(incumbent), 4),
                candidate_cost_usd=round(_measure_facade._baseline_priced_cost(state), 4),
            ))
        except OSError:
            pass
        return incumbent
    if freeze and (state.get("window") or {}).get("elapsed"):
        try:
            _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            frozen = dict(state)
            frozen["captured_at"] = _measure_facade.datetime.now().isoformat()
            frozen["source"] = "frozen_from_history"
            # Atomic write: unique temp via mkstemp + os.replace, with try/finally
            # cleanup so a crash mid-write never leaks a stale temp file (matches the
            # config-writer pattern used elsewhere in this file).
            tmp_fd, tmp_path = _measure_facade.tempfile.mkstemp(
                dir=str(_measure_facade.SNAPSHOT_DIR), prefix=".baseline_state-", suffix=".tmp")
            try:
                with _measure_facade.os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    _measure_facade.json.dump(frozen, f, indent=2, default=str)
                _measure_facade.os.chmod(tmp_path, 0o600)
                _measure_facade.os.replace(tmp_path, str(bpath))
                tmp_path = None
            finally:
                if tmp_path is not None:
                    try:
                        _measure_facade.os.unlink(tmp_path)
                    except OSError:
                        pass
            return frozen
        except OSError:
            return state
    return state


def _cost_per_session(fi, cw, cr, out, shares, tier):
    """Weighted-average cost of one typical session priced over a REAL model mix.

    Every share contributes at its true weight; an unpriced share (an unknown or
    local model id with no rate card) is proxy-priced at the runtime-default rate
    rather than dropped or renormalized away. The denominator is the TOTAL present
    share, not the priced share -- so a tiny priced sliver can never be inflated to
    represent the whole mix. (The old renorm-over-priced-share could turn a
    {opus:0.01, <unpriced>:0.99} mix into 100% opus.) Provider-agnostic; using the
    same estimator on the before and after mix keeps the comparison fair.
    """
    _measure_facade = _compat.facade()
    items = [(m, s) for m, s in (shares or {}).items() if s and s > 0]
    default_model = _measure_facade._default_model_for_runtime()
    if not items:
        return _measure_facade._get_model_cost(default_model, int(fi), int(out), int(cr), int(cw), tier=tier)
    total_share = sum(s for _, s in items)
    cost = sum(
        s * _measure_facade._get_model_cost(m if _measure_facade._is_priced_model(m, tier) else default_model,
                            int(fi), int(out), int(cr), int(cw), tier=tier)
        for m, s in items)
    return cost / total_share


def _mix_from_session_rows(cutoff):
    """Main-thread model mix from session_log's stored per-model usage.

    Prices the transformation's ACTUAL arm, which describes MAIN work only:
    model_usage_json is the parent-thread usage, while all_model_usage_json also
    contains rolled-up SUBAGENT tokens (the model attribution fix). Blending subagent usage into
    the main arm double-dips with the sidechain pool and skews the mix toward
    whatever models the subagents ran. So the parent-thread JSON is preferred;
    all_model_usage_json is only a fallback for legacy rows that never stored a
    parent-only column. Sums usage across human (non-sidechain) rows since
    `cutoff` and returns {model: share}. Empty dict when nothing usable -- the
    caller bails on that rather than silently pricing the actual arm at the
    sonnet default. Never raises.
    """
    _measure_facade = _compat.facade()
    totals = {}
    try:
        if not _measure_facade.TRENDS_DB.exists():
            return {}
        conn = _measure_facade._init_trends_db()
        _measure_facade.ensure_activity_view(conn)
        try:
            # Quality filter removed (audit 2026-08-29): the filter removed
            # nearly all Haiku sessions (3.6% -> 0.1%) and reduced Sonnet share
            # (13% -> 10.4%), making the mix look 5.9pp more Opus-heavy than it
            # is. This understates routing savings. The mix should reflect ALL
            # billed tokens, not just tokens from sessions that pass an
            # arbitrary quality gate.
            rows = conn.execute(
                "SELECT model_usage_json, all_model_usage_json FROM session_activity "
                "WHERE date >= ? AND COALESCE(is_sidechain, 0) = 0",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
        for mu, amu in rows:
            usage = _measure_facade._safe_json_dict(mu) or _measure_facade._safe_json_dict(amu)
            for model, toks in usage.items():
                if not model:
                    continue
                try:
                    t = float(toks or 0)
                except (TypeError, ValueError):
                    continue
                if t > 0:
                    totals[model] = totals.get(model, 0) + t
    except (_measure_facade.sqlite3.Error, OSError):
        return {}
    grand = sum(totals.values())
    if grand <= 0:
        return {}
    return {m: t / grand for m, t in totals.items()}


def _subagent_pool_ttl(days):
    """Cache TTL for a subagent-pool window: 15 min for dashboard-fresh short
    windows, 6h for long (since-install) windows whose totals barely move."""
    _measure_facade = _compat.facade()
    try:
        return _measure_facade._SUBAGENT_POOL_TTL if float(days) <= 31 else _measure_facade._SUBAGENT_POOL_TTL_LONG
    except (TypeError, ValueError):
        return _measure_facade._SUBAGENT_POOL_TTL


def _subagent_pool_key_str(key):
    """Stable string form of the (days, share, runtime, tier) tuple for sidecar
    keying. `tier` is part of the key because subagent-pool costs are priced per
    pricing tier -- omitting it would serve a prior tier's figures for up to the
    TTL after a tier change."""
    _measure_facade = _compat.facade()
    return "|".join(str(part) for part in key)


def _read_subagent_pool_sidecar(key, ttl=None):
    """Return the persisted subagent-pool payload for `key` (days, share, runtime)
    if present AND younger than `ttl`, else None. The sidecar holds a
    DICT of {key_str: payload} so distinct windows (e.g. days=30 and days=since-
    install) coexist instead of overwriting one another. Fail-open."""
    _measure_facade = _compat.facade()
    pass
    cache_path = _measure_facade.SNAPSHOT_DIR / "subagent_pool.json"
    try:
        mtime = cache_path.stat().st_mtime
    except OSError:
        return None
    try:
        if (_measure_facade._subagent_pool_sidecar_memo is not None
                and _measure_facade._subagent_pool_sidecar_memo[0] == mtime):
            store = _measure_facade._subagent_pool_sidecar_memo[1]
        else:
            store = _measure_facade.json.loads(cache_path.read_text(encoding="utf-8"))
            if not isinstance(store, dict):
                return None
            _measure_facade._subagent_pool_sidecar_memo = (mtime, store)
        entry = store.get(_measure_facade._subagent_pool_key_str(key))
        if (isinstance(entry, dict)
                and (_measure_facade.time.time() - float(entry.get("_cached_ts", 0)))
                < ttl):
            return {k: v for k, v in entry.items() if not str(k).startswith("_")}
    except (_measure_facade.json.JSONDecodeError, OSError, ValueError, TypeError):
        return None
    return None


def _write_subagent_pool_sidecar(key, payload):
    """Merge `payload` under its key_str into the 0600 sidecar dict (atomic write,
    mkstemp + fchmod + os.replace) so a fresh flush process skips the cold scan.
    Stale entries (older than the TTL) are pruned on write so the store stays tiny.
    Best-effort; never raises."""
    _measure_facade = _compat.facade()
    pass
    cache_path = _measure_facade.SNAPSHOT_DIR / "subagent_pool.json"
    try:
        now = _measure_facade.time.time()
        # Start from the existing store so concurrent windows accumulate.
        store = {}
        try:
            if (_measure_facade._subagent_pool_sidecar_memo is not None
                    and _measure_facade._subagent_pool_sidecar_memo[0] == cache_path.stat().st_mtime):
                store = dict(_measure_facade._subagent_pool_sidecar_memo[1])
            else:
                loaded = _measure_facade.json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    store = loaded
        except (_measure_facade.json.JSONDecodeError, OSError, ValueError, TypeError):
            store = {}
        # Prune stale entries so the store never grows unbounded. Prune at the
        # LONG ttl so long-window (since-install) entries survive between their
        # 6h refreshes; short-window staleness is enforced at read time.
        store = {
            k: v for k, v in store.items()
            if isinstance(v, dict)
            and (now - float(v.get("_cached_ts", 0))) < _measure_facade._SUBAGENT_POOL_TTL_LONG
        }
        entry = dict(payload)
        entry["_cached_ts"] = now
        store[_measure_facade._subagent_pool_key_str(key)] = entry
        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = _measure_facade.tempfile.mkstemp(
            prefix=".subagent_pool.", suffix=".tmp", dir=str(_measure_facade.SNAPSHOT_DIR))
        try:
            if hasattr(_measure_facade.os, "fchmod"):  # POSIX only; mkstemp is already 0600
                _measure_facade.os.fchmod(fd, 0o600)
            with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as fh:
                _measure_facade.json.dump(store, fh)
            _measure_facade.os.replace(tmp_name, str(cache_path))
            try:
                _measure_facade._subagent_pool_sidecar_memo = (_measure_facade.os.stat(cache_path).st_mtime, store)
            except OSError:
                _measure_facade._subagent_pool_sidecar_memo = None
        except Exception:
            try:
                _measure_facade.os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception:
        pass


def _opus_baseline_shares(opus_share):
    """The pre-TO counterfactual mix, built from a MEASURED Opus share.

    No default: a caller that cannot measure the share must not be handed
    an assumed one, because it scales the routing lever directly."""
    _measure_facade = _compat.facade()
    o = max(0.0, min(1.0, float(opus_share)))
    return {"opus": o, "sonnet": round(1.0 - o, 4)}


def _opus_floor_consented():
    """Whether the 0.95-Opus pre-TO default may be applied for an Anthropic user with
    NO measured baseline. Gated so a brand-new user who never ran mostly Opus is
    not handed a fabricated baseline that over-counts. Returns True only on explicit
    opt-in: env override, or a config flag set by one-time consent. A user with a real
    measured frozen baseline never reaches this gate (the frozen share is trusted)."""
    _measure_facade = _compat.facade()
    env = _measure_facade.os.environ.get("TOKEN_OPTIMIZER_PRETOOL_OPUS")
    if env is not None:
        return str(env).strip().lower() in ("1", "true", "yes", "on")
    return bool(_measure_facade._read_config_flag("pretool_opus_baseline_confirmed", default=False))


def _subagent_model_cost(model, fi, cr, out, cw, cw1h, cw5m, shares, tier):
    """Blended TTL-aware cost of one subagent token bundle priced over `shares`.

    Mirrors _cost_per_session (unpriced model -> runtime default, denominator = total
    present share) but threads the cache-write 1h/5m split into _get_model_cost so 1h
    writes bill at 2x input and 5m at 1.25x, consistent with the main pool."""
    _measure_facade = _compat.facade()
    items = [(mdl, s) for mdl, s in (shares or {}).items() if s and s > 0]
    default_model = _measure_facade._default_model_for_runtime()
    if not items:
        return _measure_facade._get_model_cost(default_model, int(fi), int(out), int(cr), int(cw),
                               tier=tier, cache_create_1h=int(cw1h),
                               cache_create_5m=int(cw5m))
    tot = sum(s for _, s in items)
    return sum(
        s * _measure_facade._get_model_cost(
            mdl if _measure_facade._is_priced_model(mdl, tier) else default_model,
            int(fi), int(out), int(cr), int(cw), tier=tier,
            cache_create_1h=int(cw1h), cache_create_5m=int(cw5m))
        for mdl, s in items) / tot


def _subagent_pool_savings(baseline_opus_share, days=None, tier=None, fresh=None,
                           baseline_mix_available=None):
    """Subagent (sidechain) routing pool — the big missing lever.

    Subagents run in sidechain transcripts (isSidechain:true), are NOT stored as
    standalone session_log rows (the collector only walks top-level project JSONL),
    and were therefore entirely excluded from the transformation. They are real spend
    that Token Optimizer routes: a user's CLAUDE.md may route subagents to Haiku/Sonnet;
    pre-TO they would have run on Opus. This pool prices that delta.

    Method (separate from, and summed with, the main pool; these are different
    transcripts, and the main pool prices only its frozen anchor, so the two
    pools never price the same dollars):
      1. Locate sidechain JSONL for the current `days` window. We scan project dirs
         for both top-level transcripts AND the `{uuid}/subagents/*.jsonl` files,
         filtered by mtime, then keep only files _parse_session_jsonl flags
         is_sidechain. The scan is bounded (_SUBAGENT_SCAN_MAX_FILES) and memoized.
      2. For each sidechain session, _parse_session_jsonl applies the SAME
         streaming-aware requestId-MAX dedup as the main pool and returns per-model
         {fresh_input, cache_read, cache_create, output}. Aggregate F/CR/CW/O by model.
      3. actual_sub = price each model's bundle at its OWN (real) rate.
      4. counterfactual_sub = price the SAME bundles at the ~95% Opus baseline mix.
      5. transformation_usd is the signed counterfactual minus actual difference.
         premium_delegation_usd separately discloses the gross cost of per-model
         bundles above the baseline rate, but never replaces the signed pool net.

    Returns {actual_usd, counterfactual_usd, transformation_usd,
    premium_delegation_usd, sessions, by_model}.
    Memoized for _SUBAGENT_POOL_TTL seconds (keyed on days + baseline share) so the
    dashboard isn't slowed by re-reading transcripts. Never raises.
    """
    _measure_facade = _compat.facade()
    zero = {"actual_usd": 0.0, "counterfactual_usd": 0.0,
            "transformation_usd": 0.0, "premium_delegation_usd": 0.0,
            "sessions": 0, "by_model": {}}
    try:
        # Sidechain transcripts are a Claude Code artifact: isSidechain-flagged
        # JSONL under ~/.claude/projects. Every other runtime keeps its session
        # data in its own home and has no such transcripts, so this pool is an
        # honest zero there -- scanning ~/.claude anyway would price a
        # coexisting Claude Code install's delegated spend into another
        # runtime's savings _compat.facade()._headline.
        if _measure_facade.detect_runtime() != "claude":
            return zero
        if tier is None:
            tier = _measure_facade._load_pricing_tier()
        key = (_measure_facade._SUBAGENT_POOL_CACHE_SCHEMA, round(float(days), 3), round(float(baseline_opus_share), 4),
               _measure_facade.detect_runtime(), str(tier), bool(baseline_mix_available))
        now = _measure_facade.time.time()
        ttl = _measure_facade._subagent_pool_ttl(days)
        if (not fresh and _measure_facade._subagent_pool_memo["payload"] is not None
                and _measure_facade._subagent_pool_memo["key"] == key
                and (now - _measure_facade._subagent_pool_memo["ts"]) < ttl):
            return _measure_facade._subagent_pool_memo["payload"]

        # L2: cross-process sidecar. A fresh flush worker (new process, empty module
        # memo) reads the cached result here instead of cold-scanning transcripts.
        if not fresh:
            _sidecar = _measure_facade._read_subagent_pool_sidecar(key, ttl)
            if _sidecar is not None:
                _measure_facade._subagent_pool_memo.update(key=key, ts=now, payload=_sidecar)
                return _sidecar

        projects_base = _measure_facade.CLAUDE_DIR / "projects"
        if not projects_base.exists():
            return zero
        cutoff_ts = now - days * 86400

        # Gather candidate transcripts: top-level project JSONL + nested subagents/.
        # A sidechain can appear either as its own file or under {uuid}/subagents/.
        # Collect ALL in-window files first, then sort newest-first and cap: the
        # old first-N-encountered cap kept whatever directory-iteration order
        # happened to yield, so the kept subset (and the priced dollars) changed
        # between runs whenever the window held more than the cap.
        candidates = []
        for project_dir in projects_base.iterdir():
            if not project_dir.is_dir():
                continue
            try:
                for jf in project_dir.rglob("*.jsonl"):
                    try:
                        m = jf.stat().st_mtime
                    except OSError:
                        continue
                    if m >= cutoff_ts:
                        candidates.append((m, jf))
            except OSError:
                continue
        candidates.sort(key=lambda t: ((_measure_facade := _compat.facade()), t[0])[1], reverse=True)
        candidates = [jf for _, jf in candidates[:_measure_facade._SUBAGENT_SCAN_MAX_FILES]]

        # Aggregate per-model billed token classes across sidechain sessions only.
        by_model = {}  # model -> {"fi","cw","cr","out"}
        n_side = 0
        request_ledger = {}  # a request repeated in another transcript counts once (largest usage)
        for jf in candidates:
            # Classify BEFORE parsing. Fully parsing every main transcript just
            # to read its is_sidechain flag was the dashboard's cold-regen 45s+
            # (GBs of JSON decoded for a boolean per file). Nested subagents/
            # transcripts are sidechains by construction; everything else gets
            # the SAME bounded head-scan the session_log backfill uses, so this
            # pool and the DB agree on what counts as a sidechain. (The old
            # full-file scan also flagged ORCHESTRATOR sessions whose tool calls
            # merely quoted the OSRC marker mid-file — pricing main-session
            # spend into this pool a second time.)
            if jf.parent.name != "subagents" and not _measure_facade._scan_jsonl_is_sidechain(jf):
                continue
            parsed = _measure_facade._parse_session_jsonl(jf)
            if not parsed or not parsed.get("is_sidechain"):
                continue
            n_side += 1
            parsed = _measure_facade._parsed_net_of_ledger(parsed, request_ledger)
            for model, bd in (parsed.get("model_usage_breakdown") or {}).items():
                if not model or str(model).startswith("<"):
                    continue
                slot = by_model.setdefault(
                    model, {"fi": 0.0, "cw": 0.0, "cw1h": 0.0, "cw5m": 0.0,
                            "cr": 0.0, "out": 0.0})
                slot["fi"] += float(bd.get("fresh_input", 0) or 0)
                slot["cr"] += float(bd.get("cache_read", 0) or 0)
                slot["cw"] += float(bd.get("cache_create", 0) or 0)
                slot["cw1h"] += float(bd.get("cache_create_1h", 0) or 0)
                slot["cw5m"] += float(bd.get("cache_create_5m", 0) or 0)
                slot["out"] += float(bd.get("output", 0) or 0)

        if not by_model:
            payload = dict(zero, sessions=n_side)
            _measure_facade._subagent_pool_memo.update(key=key, ts=now, payload=payload)
            _measure_facade._write_subagent_pool_sidecar(key, payload)
            return payload

        # actual = each model at its own rate; counterfactual = same tokens at the
        # measured pre-TO baseline mix. When NO genuinely measured baseline mix
        # exists (baseline_mix_available False: the before-arm merely tracks the
        # actual mix), there is no honest counterfactual to price, so each
        # bundle's counterfactual equals its actual (pool delta 0) instead of a
        # fabricated opus/sonnet mix nobody ran.
        before_shares = _measure_facade._opus_baseline_shares(baseline_opus_share)
        actual = 0.0
        counterfactual = 0.0
        premium = 0.0
        by_model_cost = {}
        for model, s in by_model.items():
            fi, cw, cr, out = s["fi"], s["cw"], s["cr"], s["out"]
            cw1h, cw5m = s["cw1h"], s["cw5m"]
            # TTL-aware: 1h cache-writes bill at 2x input, 5m at 1.25x. Apportion any
            # clamp/rounding gap into 5m (conservative) so the split sums to cw.
            if cw1h + cw5m <= 0:
                cw5m = cw
            a = _measure_facade._subagent_model_cost(model, fi, cr, out, cw, cw1h, cw5m,
                                     {model: 1.0}, tier)
            c = a if not baseline_mix_available else _measure_facade._subagent_model_cost(
                model, fi, cr, out, cw, cw1h, cw5m, before_shares, tier)
            actual += a
            counterfactual += c
            # Keep gross premium visible as supplementary context. The pool's
            # transformation remains the signed aggregate below.
            if c < a:
                premium += a - c
            by_model_cost[_measure_facade._friendly_model(model)] = (
                by_model_cost.get(_measure_facade._friendly_model(model), 0.0) + a)

        payload = {
            "actual_usd": round(actual, 2),
            "counterfactual_usd": round(counterfactual, 2),
            "transformation_usd": round(counterfactual - actual, 2),
            "premium_delegation_usd": round(premium, 2),
            "sessions": n_side,
            "by_model": {k: round(v, 2) for k, v in by_model_cost.items()},
        }
        _measure_facade._subagent_pool_memo.update(key=key, ts=now, payload=payload)
        _measure_facade._write_subagent_pool_sidecar(key, payload)
        return payload
    except (_measure_facade.sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError):
        return zero


def _short_session_pool_savings(cutoff, baseline_opus_share, tier=None,
                                baseline_mix_available=None):
    """Below-threshold (short/low-input) main sessions as their own routing pool.

    The quality gates keep sub-minute one-shot sessions out of the session COUNT
    because that count multiplies a frozen typical-session anchor, and a
    transient automation call is nowhere near a typical session. But those
    sessions still bill real tokens, and routing them to lighter models is real
    transformation, so their volume is priced here directly: each model's
    PARENT-THREAD bundle (model_usage_breakdown_json, which excludes rolled-up
    subagent tokens -- those belong to the sidechain pool) at its own rate vs
    the same bundle at the baseline mix. Both arms keep the sessions' real
    fresh/cache split; no cache-pattern counterfactual is applied because the
    frozen baseline's hit rate describes long interactive sessions, not
    one-shots. Rows without a stored breakdown contribute nothing
    (conservative). Returns {actual_usd, counterfactual_usd, sessions}.
    Never raises.
    """
    _measure_facade = _compat.facade()
    zero = {"actual_usd": 0.0, "counterfactual_usd": 0.0, "sessions": 0}
    try:
        if not _measure_facade.TRENDS_DB.exists():
            return zero
        if tier is None:
            tier = _measure_facade._load_pricing_tier()
        conn = _measure_facade._init_trends_db()
        _measure_facade.ensure_activity_view(conn)
        try:
            # Eligibility is SESSION level here too (the exact complement of the
            # main pool's gates on session_log totals): a session is wholly in
            # this pool or wholly out, never split across its activity days.
            # Only the in-window token slices are summed and sessions counted
            # distinctly (review A3).
            rows = conn.execute(
                "SELECT model_usage_breakdown_json, id FROM session_activity "
                "WHERE input_tokens IS NOT NULL AND date >= ? "
                "AND COALESCE(is_sidechain, 0) = 0 "
                "AND id IN (SELECT id FROM session_log "
                "WHERE COALESCE(is_sidechain, 0) = 0 "
                + _measure_facade._SESSION_QUALITY_FILTER_NEGATED + ")", (cutoff,)
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return zero
        by_model = {}
        for bdj, _sid in rows:
            for model, bd in (_measure_facade._safe_json_dict(bdj) or {}).items():
                if not model or str(model).startswith("<"):
                    continue
                slot = by_model.setdefault(
                    model, {"fi": 0.0, "cw": 0.0, "cw1h": 0.0, "cw5m": 0.0,
                            "cr": 0.0, "out": 0.0})
                slot["fi"] += float(bd.get("fresh_input", 0) or 0)
                slot["cr"] += float(bd.get("cache_read", 0) or 0)
                slot["cw"] += float(bd.get("cache_create", 0) or 0)
                slot["cw1h"] += float(bd.get("cache_create_1h", 0) or 0)
                slot["cw5m"] += float(bd.get("cache_create_5m", 0) or 0)
                slot["out"] += float(bd.get("output", 0) or 0)
        if not by_model:
            return dict(zero, sessions=len({r[1] for r in rows}))
        # Counterfactual mix: only a genuinely measured pre-TO baseline may
        # reprice these bundles. Without one (baseline_mix_available False),
        # counterfactual equals actual -- the sessions' real spend still shows
        # in both arms, but no transformation is claimed on a mix nobody
        # measured (on a non-Anthropic runtime that fabricated mix would even
        # be a different provider's rate card).
        before_shares = _measure_facade._opus_baseline_shares(baseline_opus_share)
        actual = 0.0
        counterfactual = 0.0
        for model, s in by_model.items():
            cw1h, cw5m = s["cw1h"], s["cw5m"]
            if cw1h + cw5m <= 0:
                cw5m = s["cw"]  # no TTL split recorded: bill at 5m (conservative)
            a = _measure_facade._subagent_model_cost(
                model, s["fi"], s["cr"], s["out"], s["cw"], cw1h, cw5m,
                {model: 1.0}, tier)
            actual += a
            counterfactual += a if not baseline_mix_available else _measure_facade._subagent_model_cost(
                model, s["fi"], s["cr"], s["out"], s["cw"], cw1h, cw5m,
                before_shares, tier)
        return {"actual_usd": round(actual, 2),
                "counterfactual_usd": round(counterfactual, 2),
                "sessions": len({r[1] for r in rows})}
    except (_measure_facade.sqlite3.Error, OSError, ValueError, TypeError, KeyError,
            ArithmeticError):
        return zero


def _baseline_progress():
    """How close THIS user is to a frozen baseline, for the dashboard's "building" state.

    A new install has no frozen anchor yet (we never freeze a half-formed window), so the
    headline transformation can't be computed. Rather than show nothing, the dashboard
    shows progress: sessions collected in the early window vs the floor, and days until the
    window closes. Returns None when there is no data at all. Never raises.
    """
    _measure_facade = _compat.facade()
    try:
        if not _measure_facade.TRENDS_DB.exists():
            return None
        conn = _measure_facade._init_trends_db()
        _measure_facade.ensure_activity_view(conn)
        try:
            # Quality filter removed (audit 2026-08-29): the filter can delay
            # the "ready" signal for users with many short sessions. The
            # progress should reflect real session count, not a filtered subset.
            rows = conn.execute(
                "SELECT date,id FROM session_activity WHERE input_tokens IS NOT NULL "
                "AND COALESCE(is_sidechain, 0) = 0 "
                "ORDER BY date, id"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return None
        first = _measure_facade.datetime.fromisoformat(str(rows[0][0])[:10])
        win_start = first + _measure_facade.timedelta(days=_measure_facade._BASELINE_ONBOARDING_DAYS)
        win_end = win_start + _measure_facade.timedelta(days=_measure_facade._BASELINE_EARLY_WINDOW_DAYS)
        in_window = len({r[1] for r in rows
                         if win_start <= _measure_facade.datetime.fromisoformat(str(r[0])[:10]) < win_end})
        now = _measure_facade.datetime.now()
        days_left = max(0, (win_end - now).days)
        ready = in_window >= _measure_facade._BASELINE_MIN_STABLE_SESSIONS and now >= win_end
        return {
            "ready": ready,
            "first_date": str(rows[0][0])[:10],
            "sessions_in_window": in_window,
            "sessions_needed": _measure_facade._BASELINE_MIN_STABLE_SESSIONS,
            "early_window_days": _measure_facade._BASELINE_EARLY_WINDOW_DAYS,
            "days_left": days_left,
        }
    except Exception:
        return None


def _price_parent_window(conn, where, params, tier):
    """Sum USD over EVERY parent session matching `where`, at each session's own mix
    AND at one constant rate card, plus the work-unit counts (api_calls, messages).

    Deliberately does NOT apply _SESSION_QUALITY_FILTER. That filter
    (`input_tokens >= 1000 AND duration_minutes >= 1.0`) exists to keep a per-session
    ANCHOR clean, but using it to select the population being counted is what blinded
    the transformation: on this user's real ledger it passed 58% of June's sessions and
    only 15% of August's, so the 72% fall in tokens-per-session -- the entire
    transformation -- happened almost entirely inside the rows it discards.

    Returns {sessions, usd, tokens, flat_usd, api_calls, messages} or None when the
    window is empty. `usd` is engine-priced (each session at its recorded model mix);
    `flat_usd` prices every session at _WEIGHT_POOL_FLAT_RATES so the two eras are
    measured with the same ruler. Never raises.
    """
    _measure_facade = _compat.facade()
    _measure_facade.ensure_activity_view(conn)
    rows = conn.execute(
        "SELECT input_tokens, output_tokens, cache_create_5m_tokens, "
        "cache_create_1h_tokens, cache_hit_rate, model_usage_json, all_model_usage_json, "
        "COALESCE(api_calls, 0), COALESCE(message_count, 0), model_usage_breakdown_json "
        "FROM session_activity WHERE input_tokens IS NOT NULL "
        "AND COALESCE(is_sidechain, 0) = 0 " + where, params
    ).fetchall()
    api_calls_unknown = conn.execute(
        "SELECT COUNT(DISTINCT id) FROM session_activity WHERE input_tokens IS NOT NULL "
        "AND COALESCE(is_sidechain,0)=0 AND COALESCE(api_calls_known,0)=0 " + where, params
    ).fetchone()[0]
    result = _measure_facade._price_parent_rows(rows, tier, api_calls_unknown=api_calls_unknown)
    if result:
        result["sessions"] = conn.execute(
            "SELECT COUNT(DISTINCT id) FROM session_activity WHERE input_tokens IS NOT NULL "
            "AND COALESCE(is_sidechain,0)=0 " + where, params).fetchone()[0]
    return result


def _price_parent_rows(rows, tier, api_calls_unknown=None):
    _measure_facade = _compat.facade()
    if not rows:
        return None
    default_model = _measure_facade._default_model_for_runtime()
    fr = _measure_facade._WEIGHT_POOL_FLAT_RATES
    usd = 0.0
    flat_usd = 0.0
    tokens = 0.0
    api_calls = 0
    messages = 0
    for r in rows:
        fi, cw, cr, out = _measure_facade._session_token_vector(r[:5])
        tokens += fi + cw + cr + out
        flat_usd += (fi * fr["input"] + out * fr["output"]
                     + cr * fr["cache_read"] + cw * fr["cache_write"]) / 1_000_000
        api_calls += int(r[7] or 0)
        messages += int(r[8] or 0)
        usage = _measure_facade._safe_json_dict(r[5]) or _measure_facade._safe_json_dict(r[6]) or {}
        grand = sum(int(v or 0) for v in usage.values() if v)
        if len(r) > 9 and _measure_facade._safe_json_dict(r[9]):
            cost = _measure_facade._cost_from_model_breakdown(_measure_facade._safe_json_dict(r[9]), tier=tier)
        elif grand > 0:
            # Share denominator is the session TOTAL, so an unpriced (non-Claude, local)
            # model is proxy-priced at the runtime default rather than letting a small
            # priced sliver stand for the whole session. Same rule as _cost_per_session.
            cost = 0.0
            for model, toks in usage.items():
                share = int(toks or 0) / grand
                if share <= 0:
                    continue
                cost += share * _measure_facade._get_model_cost(
                    model if _measure_facade._is_priced_model(model, tier) else default_model,
                    int(fi), int(out), int(cr), int(cw), tier=tier)
        else:
            cost = _measure_facade._get_model_cost(default_model, int(fi), int(out), int(cr), int(cw),
                                   tier=tier)
        usd += cost
    return {"sessions": len(rows), "usd": usd, "tokens": tokens,
            "flat_usd": flat_usd, "api_calls": api_calls, "messages": messages,
            # Sessions whose dated per-day call evidence was never recorded.
            # api_calls is then a lower bound, not a measured total.
            "api_calls_unknown": int(api_calls_unknown or 0)}


def _pinned_workload_anchor():
    """This user's pinned pre-install workload anchor, or None until built.

    Built once from the user's baseline window in baseline_state.json."""
    _measure_facade = _compat.facade()
    try:
        path = _measure_facade.SNAPSHOT_DIR / _measure_facade._PRETOOL_ANCHOR_FILE
        if not path.exists():
            return None
        data = _measure_facade.json.loads(path.read_text(encoding="utf-8"))
        m = data.get("metrics") if isinstance(data, dict) else None
        if (data.get("status") == "pinned" and data.get("activity_basis") == 1
                and isinstance(m, dict)
                and data.get("rates") == _measure_facade._WEIGHT_POOL_FLAT_RATES
                and all(isinstance(m.get(k), (int, float)) and _measure_facade.math.isfinite(m[k]) and m[k] >= 0
                        for k in ("sessions", "usd", "tokens", "flat_usd", "api_calls", "messages"))
                and m["sessions"] >= _measure_facade._SESSION_WEIGHT_MIN_ANCHOR_SESSIONS
                and m["api_calls"] > 0 and m["flat_usd"] > 0):
            return {"metrics": m, "label": str(data.get("label") or "your earliest tracked month").replace("your pre-install baseline", "your earliest tracked month")}
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def _pretool_window():
    """(start, end) dates of this user's frozen pre-install baseline window."""
    _measure_facade = _compat.facade()
    try:
        data = _measure_facade.json.loads((_measure_facade.SNAPSHOT_DIR / "baseline_state.json").read_text(encoding="utf-8"))
        w = data.get("window") or {}
        start, end = str(w.get("start") or "")[:10], str(w.get("end") or "")[:10]
        _measure_facade.datetime.strptime(start, "%Y-%m-%d")
        _measure_facade.datetime.strptime(end, "%Y-%m-%d")
        return (start, end) if start <= end else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _pretool_anchor_step(budget_seconds=None, tier=None):
    """Build the pinned pre-install anchor a few transcripts at a time.

    The ledger keeps a rolling window, so the months before install age out of
    it; their transcripts usually remain on disk. Each call prices up to
    `budget_seconds` of them with the same ruler as the live pool and banks
    running sums, then pins the result once the window is done. A window with
    too few surviving transcripts is marked unavailable, so the check stops.
    Returns the state string. Never raises.
    """
    _measure_facade = _compat.facade()
    path = _measure_facade.SNAPSHOT_DIR / _measure_facade._PRETOOL_ANCHOR_FILE
    try:
        state = _measure_facade.json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(state, dict) or state.get("activity_basis") != 1:
            # State written before activity dating (or surviving an archive
            # collision) was computed on session-date windows; rebuild it.
            state = {}
        if state.get("status") in ("pinned", "unavailable"):
            return state["status"]
        window = _measure_facade._pretool_window()
        if not window:
            return "no_baseline"
        if tier is None:
            tier = _measure_facade._load_pricing_tier()
        start, end = window
        if state.get("window") != [start, end] or state.get("rates") != _measure_facade._WEIGHT_POOL_FLAT_RATES:
            state = {"status": "building", "window": [start, end], "rates": _measure_facade._WEIGHT_POOL_FLAT_RATES,
                     "activity_basis": 1, "cursor": 0, "seen": [],
                     "metrics": {"sessions": 0, "usd": 0.0, "tokens": 0.0, "flat_usd": 0.0,
                                 "api_calls": 0, "messages": 0}}
        files = sorted(str(f) for f, _mtime, _proj in _measure_facade._find_all_jsonl_files(None))
        seen = set(state["seen"])
        deadline = _measure_facade.time.monotonic() + budget_seconds
        cursor = int(state.get("cursor") or 0)
        while cursor < len(files) and _measure_facade.time.monotonic() < deadline:
            fp = files[cursor]
            cursor += 1
            uuid = _measure_facade._canonical_session_uuid(fp) or fp
            if uuid in seen:
                continue  # another copy of a session already priced
            seen.add(uuid)
            row = _measure_facade._pretool_session_row(fp, start, end, state.setdefault("requests", {}))
            priced = _measure_facade._price_parent_rows([row], tier) if row else None
            if priced:
                for k in state["metrics"]:
                    state["metrics"][k] += priced[k]
        state["cursor"], state["seen"] = cursor, sorted(seen)
        if cursor >= len(files):
            m = state["metrics"]
            ok = m["sessions"] >= _measure_facade._SESSION_WEIGHT_MIN_ANCHOR_SESSIONS and m["api_calls"] > 0 and m["flat_usd"] > 0
            state = {"status": "pinned" if ok else "unavailable", "window": [start, end],
                     "rates": _measure_facade._WEIGHT_POOL_FLAT_RATES, "activity_basis": 1, "metrics": m,
                     "label": f"your earliest tracked month ({start} to {end})",
                     "pinned_at": _measure_facade.datetime.now().isoformat()}
        _measure_facade._write_baseline_state(path, state)
        return state["status"]
    except Exception:
        return "error"


def _pretool_session_row(filepath, start, end, ledger):
    """Price only the anchor interval, with one share per API request."""
    _measure_facade = _compat.facade()
    parsed = _measure_facade._parse_session_jsonl(filepath)
    if not parsed:
        return None
    merged = _measure_facade._session_merged_requests(filepath, parsed, _measure_facade._session_subagent_files(filepath))
    if merged is None:
        return None
    merged = {key: usage for key, usage in merged.items()
              if usage.get("day") and start <= usage["day"] <= end}
    merged, _ = _measure_facade._net_and_record(merged, ledger)
    parsed = _measure_facade._parsed_with_requests(parsed, merged)
    if not parsed["api_calls"] and not parsed["total_input_tokens"] and not parsed["total_output_tokens"]:
        return None
    return (parsed["total_input_tokens"], parsed["total_output_tokens"],
            parsed["total_cache_create_5m"], parsed["total_cache_create_1h"], parsed["cache_hit_rate"],
            _measure_facade.json.dumps(parsed["model_usage"]), None, parsed["api_calls"], parsed["message_count"],
            _measure_facade.json.dumps(parsed["model_usage_breakdown"]))


def _stable_workload_anchor(before, month):
    """Keep the existing workload comparison stable during session refreshes."""
    _measure_facade = _compat.facade()
    path = _measure_facade.SNAPSHOT_DIR / "workload_anchor.json"
    try:
        if path.exists():
            frozen = _measure_facade.json.loads(path.read_text(encoding="utf-8"))
            metrics = frozen.get("metrics") if isinstance(frozen, dict) else None
            # Keep the frozen month even after retention prunes it from the ledger:
            # the earliest month left later is closer to (or after) install, so
            # sliding forward would compare the tool against itself. Only a month
            # EARLIER than the frozen one (a history backfill) re-anchors.
            if (isinstance(metrics, dict) and str(frozen.get("month") or "") <= month
                    and frozen.get("rates") == _measure_facade._WEIGHT_POOL_FLAT_RATES
                    and frozen.get("activity_basis") == 1
                    and all(isinstance(metrics.get(k), (int, float))
                            and _measure_facade.math.isfinite(metrics[k]) and metrics[k] >= 0
                            for k in ("sessions", "usd", "tokens", "flat_usd", "api_calls", "messages"))
                    and metrics["sessions"] >= _measure_facade._SESSION_WEIGHT_MIN_ANCHOR_SESSIONS
                    and metrics["api_calls"] > 0 and metrics["flat_usd"] > 0):
                return metrics, str(frozen["month"])
        if (before and before["sessions"] >= _measure_facade._SESSION_WEIGHT_MIN_ANCHOR_SESSIONS
                and before["api_calls"] > 0 and before["flat_usd"] > 0):
            _measure_facade._write_baseline_state(path, {"month": month, "metrics": before,
                                        "rates": _measure_facade._WEIGHT_POOL_FLAT_RATES,
                                        "activity_basis": 1,
                                        "captured_at": _measure_facade.datetime.now().isoformat()})
        return before, month
    except (OSError, ValueError, TypeError):
        return None, month


def _crosscheck_flat(weight_pool, counterfactual_usd):
    """True when both work-unit transformations sit within 5% of spend.

    Figures that close to zero are inside the noise of how work is counted,
    so the honest reading is "no measurable change", not a disagreement."""
    _measure_facade = _compat.facade()
    try:
        cf = float(counterfactual_usd)
        a = float(weight_pool.get("transformation_usd"))
        b = float((weight_pool.get("crosscheck") or {}).get("transformation_usd"))
    except (TypeError, ValueError, AttributeError):
        return False
    if not all(_measure_facade.math.isfinite(x) for x in (cf, a, b)) or cf <= 0:
        return False
    band = _measure_facade._TRANSFORMATION_FLAT_BAND * cf
    return abs(a) <= band and abs(b) <= band


def _crosscheck_agrees(primary_usd, crosscheck_usd):
    """Whether the two independent work-unit transformations agree.

    The per-API-call and per-message methods measure the same workload through
    two different units, so the transformation is only headline-worthy when
    they land within 10% of each other on the same side of zero. Returns
    (agrees, gap_pct) where gap_pct is |a-b| as a percentage of max(|a|,|b|),
    or None when either input is missing or non-finite.
    """
    _measure_facade = _compat.facade()
    try:
        a = float(primary_usd)
        b = float(crosscheck_usd)
    except (TypeError, ValueError):
        return False, None
    if not (_measure_facade.math.isfinite(a) and _measure_facade.math.isfinite(b)):
        return False, None
    denom = max(abs(a), abs(b))
    gap = abs(a - b) / denom if denom > 0 else 0.0
    same_sign = (a >= 0) == (b >= 0)
    return same_sign and gap <= 0.10, round(gap * 100, 1)


def _signed_usd(value):
    """Format a signed dollar figure with the sign before the symbol
    (-$1,234, not $-1,234)."""
    _measure_facade = _compat.facade()
    v = float(value or 0.0)
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def _crosscheck_verdict(pool):
    """Recompute the cross-check verdict from the pool's raw figures.

    A stored `agrees` flag is payload for JSON consumers only; a stale or
    hand-edited flag must never overrule what the numbers say. No cross-check
    means nothing to disagree with."""
    _measure_facade = _compat.facade()
    cross = (pool or {}).get("crosscheck") or {}
    if not cross:
        return True
    agrees, _ = _measure_facade._crosscheck_agrees(pool.get("transformation_usd"),
                                 cross.get("transformation_usd"))
    return agrees


def _session_weight_pool_savings(cutoff, days=None, tier=None, activity_window=None):
    """THE volume lever: cost per UNIT OF WORK, over the whole parent population.

    The frozen-anchor pool holds session volume constant across both arms by
    construction, so it can only ever express model mix and cache-hit redistribution.
    On this user's ledger both of those are flat-to-adverse (cost per token moved
    0.82 -> 0.73 while the quality-filtered mix got *costlier*, because today's mix
    includes Claude Fable 5 at $10/MTok, a model that did not exist in the baseline
    window). Meanwhile tokens per API call fell ~13% (and tokens per session 72%).
    That is the whole transformation and the frozen-anchor pool is structurally
    incapable of seeing it.

    This pool measures it directly: the earliest COMPLETE calendar month in the ledger
    is the anchor, the current window is the actual, and both are priced over every
    parent session with no quality gate, so the two populations are defined by the same
    rule. Returns None (caller keeps the legacy path) when there is no usable anchor.

    The unit of work is API calls (falling back to messages) -- NOT sessions. Sessions
    are not invariant to the session-partitioning the lighter workflow itself causes
    (same total tokens split across 3.4x more sessions), so a per-session price gap is
    an accounting artifact, not a saving; pricing the current session count at the old
    per-session cost counts capacity gained as money saved, which is exactly the
    inflation this pool exists to avoid. `transformation_usd` answers "what would this
    period's dispatched work have cost at the anchor month's cost per unit of work",
    both arms at the CONSTANT flat rate card so vendor/mix price drift stays out (the
    engine-priced variant, which includes that drift, is disclosed as
    `engine_priced_transformation_usd` / `price_mix_drift_usd`, never headline-claimed).

    Never raises.
    """
    _measure_facade = _compat.facade()
    if tier is None:
        tier = _measure_facade._load_pricing_tier()
    try:
        if not _measure_facade.TRENDS_DB.exists():
            return None
        conn = _measure_facade._init_trends_db()
        _measure_facade.ensure_activity_view(conn)
        try:
            months = [
                str(r[0]) for r in conn.execute(
                    "SELECT DISTINCT substr(date, 1, 7) AS ym FROM session_activity "
                    "WHERE input_tokens IS NOT NULL AND COALESCE(is_sidechain, 0) = 0 "
                    "ORDER BY ym"
                ).fetchall() if r and r[0]
            ]
            pinned = _measure_facade._pinned_workload_anchor()
            if len(months) < 2 and not pinned:
                return None
            first_day = conn.execute(
                "SELECT MIN(date) FROM session_activity WHERE input_tokens IS NOT NULL "
                "AND COALESCE(is_sidechain, 0) = 0"
            ).fetchone()
            first_day = str(first_day[0])[:10] if first_day and first_day[0] else None
            this_month = _measure_facade.datetime.now().strftime("%Y-%m")
            anchor_month = None
            for ym in ([] if pinned else months):
                if ym == this_month:
                    continue  # current month is still accruing
                # Skip a first month the ledger only partially covers, or its
                # per-session mean is an artifact of a few days' data.
                if first_day and ym == first_day[:7] and not first_day.endswith("-01"):
                    continue
                anchor_month = ym
                break
            if pinned:
                before, anchor_month = pinned["metrics"], pinned["label"]
            else:
                if not anchor_month:
                    return None
                before = _measure_facade._price_parent_window(
                    conn, "AND date LIKE ?", (anchor_month + "%",), tier)
                before, anchor_month = _measure_facade._stable_workload_anchor(before, anchor_month)
            now = (_measure_facade._price_parent_activity_window(conn, *activity_window, tier)
                   if activity_window else
                   _measure_facade._price_parent_window(conn, "AND date >= ?", (cutoff,), tier))
        finally:
            conn.close()
        if not before or not now:
            return None
        b_n, b_usd, b_tok = before["sessions"], before["usd"], before["tokens"]
        n_n, n_usd, n_tok = now["sessions"], now["usd"], now["tokens"]
        b_flat, n_flat = before["flat_usd"], now["flat_usd"]
        if (b_n < _measure_facade._SESSION_WEIGHT_MIN_ANCHOR_SESSIONS
                or n_n < _measure_facade._AFTER_MIN_SESSIONS or b_usd <= 0 or b_flat <= 0):
            return None
        before_cps = b_usd / b_n
        now_cps = n_usd / n_n
        if not all(_measure_facade.math.isfinite(x) for x in (before_cps, now_cps)) or before_cps <= 0:
            return None

        # WORK-NORMALIZED transformation. A "session" is NOT a stable unit of work on
        # this ledger: the workflow deliberately repartitions the same work into more,
        # lighter sessions (889 x 16.6M tok in the anchor month -> ~3.4x sessions at
        # ~4.7M tok now, with TOTAL tokens ~flat), so per-session cost falls ~72%
        # mechanically even if nothing is saved. Pricing the current session COUNT at
        # the old per-session cost (the rejected ~$32K construction) counts that
        # repartitioning -- and the capacity it bought -- as dollars saved. API calls
        # (unique requestIds, streaming-deduped; retries bill and count in both eras
        # alike) and messages (streaming-deduped, tool-result transport excluded) are
        # invariant to session boundaries, so the transformation is measured per unit
        # of work actually dispatched to the model:
        #     transformation = (anchor_cost_per_unit - current_cost_per_unit)
        #                      x current_units
        # Both arms are priced at the CONSTANT _WEIGHT_POOL_FLAT_RATES card, so model
        # mix and vendor price drift cannot leak into the claim; the engine-priced
        # variant (which includes that drift) is disclosed separately below.
        b_calls, n_calls = before["api_calls"], now["api_calls"]
        b_msgs, n_msgs = before["messages"], now["messages"]
        # api_calls is only a trustworthy work unit when every session in the
        # window has dated per-day call evidence; rows collected before
        # daily_calls_json existed contribute a lower bound, not a measured
        # total, so the unit falls back to messages rather than pretending.
        b_calls_ok = not before.get("api_calls_unknown")
        n_calls_ok = not now.get("api_calls_unknown")
        calls_complete = b_calls_ok and n_calls_ok
        if b_calls > 0 and n_calls > 0 and calls_complete:
            unit, b_units, n_units = "api_call", b_calls, n_calls
        elif b_msgs > 0 and n_msgs > 0:
            unit, b_units, n_units = "message", b_msgs, n_msgs
        else:
            # No partitioning-invariant unit available for one era -> honest blindness,
            # not a session-unit fallback. Caller keeps the legacy frozen-anchor path.
            return None
        before_rate = b_flat / b_units
        now_rate = n_flat / n_units
        if not all(_measure_facade.math.isfinite(x) for x in (before_rate, now_rate)) or before_rate <= 0:
            return None
        transformation = (before_rate - now_rate) * n_units
        counterfactual = before_rate * n_units
        # Cross-check on the OTHER work unit; disclosed, never summed. The two
        # units only corroborate the headline when they agree within 10% on the
        # same side of zero. The verdict is stored for JSON consumers; Python
        # decision paths recompute it from the raw figures (_crosscheck_verdict)
        # rather than trust the stored flag.
        crosscheck = None
        if unit == "api_call" and b_msgs > 0 and n_msgs > 0:
            crosscheck = {"unit": "message",
                          "transformation_usd": (b_flat / b_msgs - n_flat / n_msgs) * n_msgs}
        elif unit == "message" and b_calls > 0 and n_calls > 0 and calls_complete:
            crosscheck = {"unit": "api_call",
                          "transformation_usd": (b_flat / b_calls - n_flat / n_calls) * n_calls}
        if crosscheck is not None:
            agrees, gap_pct = _measure_facade._crosscheck_agrees(
                transformation, crosscheck["transformation_usd"])
            crosscheck["agrees"] = agrees
            crosscheck["gap_pct"] = gap_pct
        # Engine-priced variant (each era at its own recorded model mix): includes the
        # price/mix drift between eras, which is real money off the bill but is NOT
        # Token Optimizer's doing (generational repricing like Sonnet 4.6 -> Sonnet 5
        # $3->$2, plus mix and cache-TTL drift). Disclosed, never headline-claimed.
        engine_transformation = ((b_usd / b_units) - (n_usd / n_units)) * n_units
        price_mix_drift = engine_transformation - transformation
        # Capacity: real value, expressed as COUNTS, never dollars. The extra sessions
        # and calls the lighter workflow made possible are upside, not savings.
        capacity_note = (
            "Work-normalized: your {units:,} {unit_label} this period priced at the "
            "{month} anchor's cost per {unit_singular}, at constant token prices. "
            "Sessions are not a stable unit of work (you ran {now_n:,} this period vs "
            "{then_n:,} in {month}, {growth:.1f}x, at about the same total spend) -- "
            "that extra capacity is real, but it is capacity gained, not dollars saved, "
            "so it is not in the _compat.facade()._headline. Model price/mix drift worth about "
            "${drift:,.0f} this period is also excluded: it would have happened without Token "
            "Optimizer."
            + ("" if pinned else " The anchor month already had partial Token Optimizer "
               "coverage, so if anything this understates the full pre-tool gap.")
        ).format(units=n_units,
                 unit_label="API calls" if unit == "api_call" else "messages",
                 unit_singular="API call" if unit == "api_call" else "message",
                 month=anchor_month, now_n=n_n, then_n=b_n,
                 growth=(n_n / b_n) if b_n else 0.0, drift=price_mix_drift)
        return {
            "anchor_month": anchor_month,
            "work_unit": unit,
            "before_units": b_units,
            "now_units": n_units,
            "before_cost_per_unit": before_rate,
            "after_cost_per_unit": now_rate,
            "before_cost_per_session": before_cps,
            "after_cost_per_session": now_cps,
            "before_tokens_per_session": (b_tok / b_n) if b_n else 0.0,
            "after_tokens_per_session": (n_tok / n_n) if n_n else 0.0,
            "before_sessions": b_n,
            "sessions": n_n,
            "actual_usd": n_flat,
            "counterfactual_usd": counterfactual,
            "transformation_usd": transformation,
            "crosscheck": crosscheck,
            "coverage": now.get("coverage"),
            "engine_priced_transformation_usd": engine_transformation,
            "engine_priced_actual_usd": n_usd,
            "price_mix_drift_usd": price_mix_drift,
            "capacity_assumption": capacity_note,
            # Sessions in each window whose dated per-day call evidence was never
            # recorded; api_calls above is a lower bound when either is nonzero.
            "api_calls_unknown_sessions": {
                "anchor": int(before.get("api_calls_unknown") or 0),
                "current": int(now.get("api_calls_unknown") or 0),
            },
        }
    except (_measure_facade.sqlite3.Error, OSError, ValueError, TypeError, ZeroDivisionError):
        return None


def _price_parent_activity_window(conn, start, end, tier):
    """Price in-window activity and disclose incomplete transcript coverage."""
    from . import weekly_activity
    return weekly_activity.price(_compat.facade(), conn, start, end, tier)


def _weekly_full_savings(resets_at=None, now=None):
    """The Savings tab's full estimate, accrued inside this subscription week.

    Its before/after difference already includes overlapping measured and
    estimated mechanisms. Never add their individual estimates on top.
    """
    _measure_facade = _compat.facade()
    if _measure_facade.detect_runtime() != "claude":
        return None
    try:
        end = _measure_facade.datetime.fromtimestamp(now if now is not None else _measure_facade.time.time(), _measure_facade.timezone.utc)
        start = (_measure_facade.datetime.fromtimestamp(float(resets_at), _measure_facade.timezone.utc) - _measure_facade.timedelta(days=7)
                 if resets_at is not None else end - _measure_facade.timedelta(days=7))
        if resets_at is None or start >= end or end.timestamp() >= float(resets_at):
            return None
        # Dashboard refreshes may run several times per minute in separate
        # processes. Keep an explicitly dated result for at most 60 seconds.
        cache_path = _measure_facade.SNAPSHOT_DIR / "weekly_full_value.json"
        anchor_path = _measure_facade.SNAPSHOT_DIR / "workload_anchor.json"
        anchor_stamp = anchor_path.stat().st_mtime_ns if anchor_path.exists() else None
        if now is None and cache_path.exists():
            try:
                cached = _measure_facade.json.loads(cache_path.read_text(encoding="utf-8"))
                age = end.timestamp() - float(cached["computed_at"])
                if (0 <= age < 60 and cached["start"] == start.isoformat()
                        and cached["anchor_stamp"] == anchor_stamp
                        and cached.get("activity_basis") == 2
                        and cached["version"] == _measure_facade.TOKEN_OPTIMIZER_VERSION):
                    return _measure_facade._positive_full_value(cached["value"])
            except (OSError, ValueError, TypeError, KeyError):
                pass
        pool = _measure_facade._session_weight_pool_savings(
            start.isoformat(), days=7, activity_window=(start, end))
        if not pool:
            return None
        if not _measure_facade._crosscheck_verdict(pool):
            # The two work-unit methods disagree this week, so the
            # whole-workload figure is not trustworthy enough to stand in for
            # the metered one; the caller falls back to measured savings.
            return None
        # Same conservative display cap as the Savings tab.
        saving = max(0.0, min(pool["transformation_usd"], pool["actual_usd"]))
        value = {"saved_usd": round(saving, 2), "start": start.isoformat(),
                "end": end.isoformat(), "method": "full workload",
                "uncapped_usd": round(pool["transformation_usd"], 2),
                "actual_usd": round(pool["actual_usd"], 2),
                "counterfactual_usd": round(pool["counterfactual_usd"], 2),
                "api_calls": pool["now_units"], "anchor_month": pool["anchor_month"],
                "coverage": pool.get("coverage")}
        if now is None:
            try:
                _measure_facade._write_baseline_state(cache_path, {
                    "computed_at": end.timestamp(), "start": start.isoformat(),
                    "anchor_stamp": anchor_path.stat().st_mtime_ns if anchor_path.exists() else None,
                    "activity_basis": 2,
                    "version": _measure_facade.TOKEN_OPTIMIZER_VERSION, "value": value})
            except OSError:
                pass
        return _measure_facade._positive_full_value(value)
    except (OSError, _measure_facade.sqlite3.Error, ValueError, TypeError, KeyError):
        return None


def _positive_full_value(value):
    """The whole-workload weekly figure only stands in for the metered savings
    when it found something; a flat or negative week (heavier models, bigger
    contexts) falls back to what was actually measured instead of showing $0."""
    _measure_facade = _compat.facade()
    try:
        return value if float((value or {}).get("saved_usd") or 0.0) > 0 else None
    except (TypeError, ValueError):
        return None


def _weight_pool_payload(pool):
    """Round/disclosure view of the session-weight pool, shared by the ok and
    net-negative return paths so both describe the SAME arms the headline (or the
    shortfall) was built from."""
    _measure_facade = _compat.facade()
    cross = pool.get("crosscheck") or {}
    cross_agrees, cross_gap = _measure_facade._crosscheck_agrees(
        pool.get("transformation_usd"), cross.get("transformation_usd")) if cross else (True, None)
    return {
        "anchor_month": pool["anchor_month"],
        "work_unit": pool.get("work_unit"),
        "before_units": pool.get("before_units", 0),
        "now_units": pool.get("now_units", 0),
        "before_cost_per_unit": round(pool.get("before_cost_per_unit", 0.0), 6),
        "after_cost_per_unit": round(pool.get("after_cost_per_unit", 0.0), 6),
        "before_cost_per_session": round(pool["before_cost_per_session"], 4),
        "after_cost_per_session": round(pool["after_cost_per_session"], 4),
        "before_tokens_per_session": int(pool["before_tokens_per_session"]),
        "after_tokens_per_session": int(pool["after_tokens_per_session"]),
        "before_sessions": pool["before_sessions"],
        "sessions": pool["sessions"],
        "actual_usd": round(pool["actual_usd"], 2),
        "counterfactual_usd": round(pool["counterfactual_usd"], 2),
        "transformation_usd": round(pool["transformation_usd"], 2),
        "crosscheck": ({
            "unit": cross.get("unit"),
            "transformation_usd": round(float(cross.get("transformation_usd", 0.0)), 2),
            "agrees": cross_agrees,
            "gap_pct": cross_gap,
        } if cross else None),
        "engine_priced_transformation_usd": round(
            float(pool.get("engine_priced_transformation_usd", 0.0)), 2),
        "engine_priced_actual_usd": round(
            float(pool.get("engine_priced_actual_usd", 0.0)), 2),
        "price_mix_drift_usd": round(float(pool.get("price_mix_drift_usd", 0.0)), 2),
        "capacity_assumption": pool["capacity_assumption"],
    }


def _estimate_before_after_savings(days=None, estimated_pools=None, model_routing=None, since=None):
    """THE headline saving: your FROZEN typical pre-TO session, priced at your PRE-TO
    efficiency (the frozen baseline model mix + cache pattern) vs what that same session costs now.

    This is a PER-SESSION COMPARISON on a FROZEN volume anchor. The "old way / session"
    figure is computed entirely from frozen inputs (the captured typical_session token
    vector + frozen model mix + frozen cache pattern), so it is a STABLE, factual baseline
    that does NOT re-roll when this period's workload changes. Only EFFICIENCY (model mix
    + cache-hit) differs between the arms, so the per-session gap is pure behavioral
    transformation. Volume (the current session count) re-enters ONLY as a multiplier for
    the monthly total, never as a confounder of the per-session baseline.

    Method:
      1. FROZEN anchor = baseline_state.json typical_session: bfi=fresh_input,
         bcr=cache_read, bcw=cache_write, bout=output. The current window over `days` is
         read ONLY for the session count (recent_n) and the CURRENT cache-hit (cur_hit).
      2. OLD WAY / session (counterfactual) = price(bfi, bcr, bout) + cache-write, all at
         the BASELINE mix. Depends only on frozen inputs + prices -> stable.
      3. NOW / session (actual) = the SAME frozen tokens, but the fresh+cache_read pool
         (t_pool = bfi + bcr) redistributed at cur_hit (cur_CR = cur_hit*t_pool; cur_F =
         t_pool - cur_CR), priced at the CURRENT mix. Cache-write is held constant in
         tokens but priced at each arm's OWN mix (cache-write bills at the writing model's
         rate, so it is a routing lever). t_pool is exact, so neither arm prices more
         volume than the typical session held.
      4. MAIN TRANSFORMATION (monthly) = (old_cps - now_cps) * recent_n, signed.

    Baseline model mix: the user's stated pre-TO share when one is set, else the frozen
    measured share from their own install-era history; with neither (or a
    non-Anthropic runtime) the before arm uses the current mix, so routing is zero.
    No share is assumed for anyone. base_hit
    comes from the frozen baseline typical session's cache pattern; if unavailable it
    falls back to the current cache-hit (caching lever = 0, conservative).

    Returns zeros (with `reason`) when no stable baseline exists yet. Never raises.

    Returns {before_cost_per_session, after_cost_per_session, savings_per_session,
             sessions_per_month, monthly_savings_usd (= counterfactual - actual),
             counterfactual_monthly_usd, actual_monthly_usd, transformation_pct,
             short_session_transformation_usd / _actual_usd / _counterfactual_usd /
               short_session_count (below-threshold one-shot pool),
             baseline_opus_share, before_opus, after_opus,
             before_tokens, after_tokens, before_sessions, after_sessions,
             baseline_source ("pretool_baseline" | "robust_earliest"),
             breakdown ([{key, waterfall_index, label, monthly_usd}], sorted
               largest-first; `key`/`waterfall_index` are the stable machine fields,
               `label` is presentational and may change),
             breakdown_caveat (str), evidence}.
    On an empty result, `reason` is one of "insufficient_history" / "net_negative" /
    "methods_disagree" / "no_recent_sessions" / "no_mix" / "unsupported_billing" /
    "estimated_billing" (None when never computed). "methods_disagree" is the
    cross-check failure: the per-API-call and per-message transformations could
    not be reconciled within 10%, so neither is headlined and both figures ride
    on session_weight_pool for display. "unsupported_billing" is the GitHub Copilot case:
    premium-request metering has no token-dollar counterfactual, so nothing is
    rendered there. "estimated_billing" is the Antigravity case: it is
    subscription/credit metered, so only the labelled list-price estimate (R8)
    exists and no token-savings headline is claimable.
    """
    _measure_facade = _compat.facade()
    zero = {"before_cost_per_session": 0.0, "after_cost_per_session": 0.0,
            "savings_per_session": 0.0, "sessions_per_month": 0,
            "monthly_savings_usd": 0.0, "counterfactual_monthly_usd": 0.0,
            "actual_monthly_usd": 0.0, "transformation_pct": 0.0,
            "uncapped_monthly_savings_usd": 0.0,
            "savings_capped": False, "savings_cap_note": None,
            "baseline_opus_share": 0.0, "before_opus": 0.0, "after_opus": 0.0,
            "before_mix_label": "", "after_mix_label": "",
            "before_tokens": 0, "after_tokens": 0, "before_sessions": 0,
            "after_sessions": 0, "baseline_source": None, "breakdown": [],
            # None = never computed. Set to "ok" / "net_negative" downstream.
            "transformation_state": None,
            "breakdown_caveat": "", "reason": None, "evidence": "estimated",
            "verbosity_transformation_usd": 0.0, "verbosity_measured_usd": 0.0,
            "compression_reprice_ratio": 1.0,
            "compression_reprice_basis": None,
            "estimated_volume_transformation_usd": 0.0,
            "estimated_volume_pools": {},
            "short_session_transformation_usd": 0.0,
            "short_session_actual_usd": 0.0,
            "short_session_counterfactual_usd": 0.0,
            "short_session_count": 0}
    try:
        # GitHub Copilot meters premium requests / AI credits, not tokens; Cursor
        # stores no local billing data at all. In both cases a token-priced
        # counterfactual has no meaning, so the transformation renders NOTHING
        # (reason discloses why); bash compression events are still counted.
        if _measure_facade.detect_runtime() in ("copilot", "cursor"):
            return {**zero, "reason": "unsupported_billing"}
        # Antigravity is subscription/credit metered; only a labelled USD
        # estimate exists (R8). The token-priced counterfactual has no meaning
        # there, but (unlike Copilot's opaque credits) a list-price estimate is
        # still surfaced by the collector — so the transformation renders
        # nothing with an explicit "estimated_billing" reason instead of a
        # token-savings _compat.facade()._headline.
        if _measure_facade.detect_runtime() == "antigravity":
            return {**zero, "reason": "estimated_billing"}
        if not _measure_facade.TRENDS_DB.exists():
            return zero

        # The frozen robust baseline supplies the PRE-TO efficiency anchors: the model
        # mix (stated or measured share, never assumed) and the cache-hit
        # pattern. It is NOT used for volume -- volume comes entirely from the current
        # window, held constant across both arms.
        baseline = _measure_facade._get_baseline_state()
        if not baseline or not baseline.get("typical_session"):
            return {**zero, "reason": "insufficient_history",
                    "baseline_building": _measure_facade._baseline_progress()}
        ts = baseline["typical_session"]
        bfi = float(ts.get("fresh_input", 0) or 0)
        bcw = float(ts.get("cache_write", 0) or 0)
        bcr = float(ts.get("cache_read", 0) or 0)
        bout = float(ts.get("output", 0) or 0)
        frozen_opus = float(baseline.get("opus_share", 0) or 0)
        baseline_source = baseline.get("opus_share_source") or "robust_earliest"
        bn = int((baseline.get("window") or {}).get("sessions_used", 0) or 0)
        # after_shares (the current/actual model mix) is needed both to price the actual
        # arm AND, for a non-Anthropic runtime with no captured baseline, to define the
        # before-arm mix (never fabricate Opus a user never ran). Resolve it up front.
        cutoff = (_measure_facade.datetime.now() - _measure_facade.timedelta(days=days)).strftime("%Y-%m-%d")
        # The transformation describes main work only. model_daily combines main and
        # sidechain tokens, so premium delegation could otherwise make the "now" arm
        # costlier and lower the _compat.facade()._headline. Build the mix from non-sidechain rows.
        after_shares = dict(_measure_facade._mix_from_session_rows(cutoff))
        # H3: an empty after mix would silently price the ACTUAL arm at the sonnet default
        # (via _cost_per_session's fallback), understating actual and INFLATING savings.
        # If model_daily is empty/stale, derive the actual mix from the SAME session_log
        # rows already loaded (their stored model_usage breakdown). Bail if still empty.
        # Baseline (pre-TO) model mix: the frozen share (stated or measured) for an
        # Anthropic runtime; with none, the current mix. A frozen share is trusted as-is;
        # a non-Anthropic user is priced at their own measured mix, never fabricated Opus.
        anthropic = _measure_facade.detect_runtime() not in {"codex", "hermes", "copilot", "opencode", "cursor", "antigravity", "grok"}
        if anthropic and frozen_opus and frozen_opus > 0:
            # A real measured baseline share exists -> trust it (e.g. an Anthropic
            # user with a frozen ~0.95 Opus share resolves to ~0.95). Gated on
            # `anthropic`: a non-Anthropic runtime never ran Opus, so a frozen
            # opus_share > 0 there is inherited from a different runtime or a
            # mixed-runtime capture. Trusting it prices the BEFORE arm at ~95%
            # Opus against an after arm priced at the user's real non-Anthropic
            # mix, fabricating a routing lever that never existed and OVERSTATING
            # savings. Such a user falls through to before_shares = after_shares,
            # which zeroes the routing lever instead of inventing one.
            baseline_opus_share = frozen_opus
            before_shares = {"opus": baseline_opus_share,
                             "sonnet": round(1.0 - baseline_opus_share, 4)}
        elif anthropic:
            # Anthropic user with no MEASURED baseline. The pre-Token-Optimizer model
            # mix is not knowable here, and a mix we cannot measure is a mix we do not
            # claim: any assumed Opus share would directly scale the routing lever and
            # inflate the _compat.facade()._headline. Price the before-arm at the user's real current mix
            # so the routing lever is exactly zero until a baseline is measured.
            baseline_opus_share = float(after_shares.get("opus", 0.0))
            before_shares = dict(after_shares)
        else:
            # Non-Anthropic (Codex/Hermes/Copilot/OpenCode) with no baseline -> price the
            # before-arm at the user's real current mix. Never inject Opus they never used;
            # with no routing lever the transformation falls to the caching lever alone.
            baseline_opus_share = float(after_shares.get("opus", 0.0))
            before_shares = dict(after_shares)
        # Baseline cache-hit pattern, defined over the FRESH+CACHE_READ POOL only (cache
        # write excluded). C1/C2: the counterfactual redistributes caching over the pool,
        # not over total_in (which includes CW); a total_in-based hit prices MORE volume on
        # the counterfactual arm than actually moved and breaks the volume invariant.
        # base_hit_pool = baseline cache_read / (baseline fresh + baseline cache_read).
        # Falls back to the current pool-hit below (caching lever = 0, conservative) when
        # the baseline session has no usable pool.
        b_pool = bfi + bcr
        base_hit = (bcr / b_pool) if b_pool > 0 else None

        # CURRENT window = human sessions over `days` (sidechains excluded -- they are
        # subagent transcripts, not the user's own billed work). Aggregate the billed
        # token classes; this is the EXACT volume that is held constant across both arms.
        # The quality filter is KEPT on recent_rows because recent_n drives
        # sessions_per_month (the count that multiplies the frozen typical-session
        # anchor — short sessions are priced separately in the short-session pool).
        # But the cache-hit rate (cur_hit) must be computed from ALL sessions, so a
        # separate unfiltered query is used for that below (audit 2026-08-29).
        # session_activity emits one row per activity DAY, so the quality gates
        # are applied at SESSION level (session_log totals) and recent_n counts
        # DISTINCT session ids: a session spanning several qualifying days is
        # one session, not several (review A3).
        conn = _measure_facade._init_trends_db()
        _measure_facade.ensure_activity_view(conn)
        try:
            recent_rows = conn.execute(
                "SELECT input_tokens, output_tokens, cache_create_5m_tokens, "
                "cache_create_1h_tokens, cache_hit_rate, id FROM session_activity "
                "WHERE input_tokens IS NOT NULL AND date >= ? "
                "AND COALESCE(is_sidechain, 0) = 0 "
                "AND id IN (SELECT id FROM session_log "
                "WHERE COALESCE(is_sidechain, 0) = 0 "
                + _measure_facade._SESSION_QUALITY_FILTER + ")", (cutoff,)
            ).fetchall()
            # Separate unfiltered query for the cache-hit rate: short sessions
            # have real cache patterns that the filter would exclude (audit
            # 2026-08-29). On the real ledger the filter excluded 85% of August
            # sessions from the cache-hit derivation.
            all_hit_rows = conn.execute(
                "SELECT input_tokens, output_tokens, cache_create_5m_tokens, "
                "cache_create_1h_tokens, cache_hit_rate FROM session_activity "
                "WHERE input_tokens IS NOT NULL AND date >= ? "
                "AND COALESCE(is_sidechain, 0) = 0",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
        recent_n = len({r[5] for r in recent_rows})
        # The session-weight pool is resolved EARLY and on its own population (every
        # parent session, no quality gate), because this guard counts only gate-passing
        # sessions. A user whose workload has shifted to short one-shots -- exactly the
        # shift this pool exists to measure -- can have recent_n == 0 while running
        # thousands of real sessions, and would otherwise be handed "no_recent_sessions"
        # and an empty card. When the pool is available the guard no longer applies.
        _wp_tier = _measure_facade._load_pricing_tier()
        weight_pool = _measure_facade._session_weight_pool_savings(cutoff, days=days, tier=_wp_tier)
        if recent_n < _measure_facade._AFTER_MIN_SESSIONS and not weight_pool:
            return {**zero, "before_sessions": bn, "reason": "no_recent_sessions",
                    "baseline_building": _measure_facade._baseline_progress()}

        # Aggregate the current window into billed token classes (sum, not mean). The main
        # pool is now priced PER FROZEN TYPICAL SESSION (see below), so the current-window
        # totals are used only to (a) confirm the window is non-empty and (b) derive the
        # CURRENT cache-hit rate (cur_hit), the one efficiency signal the "now" arm needs.
        # Computed from ALL sessions (all_hit_rows), not just filtered recent_rows,
        # because short sessions have real cache patterns too (audit 2026-08-29).
        # On the real ledger the filter excluded 85% of August sessions from the
        # cache-hit derivation, distorting both cur_hit and the after_cache_hit display.
        _vectors = [_measure_facade._session_token_vector(r) for r in all_hit_rows]
        F = CR = CW = 0.0
        for (fi, cw, cr, _out) in _vectors:  # output not aggregated; "now" arm uses frozen bout
            F += fi; CW += cw; CR += cr
        total_in = F + CR + CW
        if total_in <= 0 and not weight_pool:
            return {**zero, "before_sessions": bn, "after_sessions": recent_n,
                    "reason": "no_recent_sessions"}
        # Current cache-hit defined over the same FRESH+CACHE_READ POOL as the baseline
        # (C1/C2: cache-write excluded), so both sides redistribute caching consistently.
        cur_pool = F + CR
        cur_hit = (CR / cur_pool) if cur_pool > 0 else 0.0
        if base_hit is None:
            base_hit = cur_hit  # caching lever neutralized; conservative

        # after_shares resolved up front. H3: if the model mix is empty (model_daily stale
        # while session_log has rows), _cost_per_session would silently fall back to the
        # sonnet default for the ACTUAL arm -> understated actual -> inflated savings.
        # Recover the actual mix from the loaded rows' stored model_usage; bail otherwise.
        if not any(s and s > 0 for s in after_shares.values()):
            after_shares = _measure_facade._mix_from_session_rows(cutoff)
            if not any(s and s > 0 for s in after_shares.values()) and not weight_pool:
                return {**zero, "before_sessions": bn, "after_sessions": recent_n,
                        "reason": "no_mix"}
            # Any user whose before-arm tracks the actual mix (non-Anthropic, OR an
            # Anthropic user with no measured baseline and no Opus-floor consent) must
            # refresh it now that the actual mix is recovered.
            tracks_actual_mix = not (anthropic and frozen_opus and frozen_opus > 0) and (
                not anthropic or not _measure_facade._opus_floor_consented())
            if tracks_actual_mix:
                baseline_opus_share = float(after_shares.get("opus", 0.0))
                before_shares = dict(after_shares)
        after_opus = float(after_shares.get("opus", 0.0))
        tier = _measure_facade._load_pricing_tier()  # resolve once; reused across the waterfall levers

        # Whether a genuinely DISTINCT pre-TO mix exists for the per-bundle pools'
        # counterfactual arm: a measured frozen share, or the consented Anthropic
        # default. In tracking mode (before-arm follows the actual mix) the routing
        # lever is zero by definition, so the sidechain and short-session pools must
        # not reprice bundles at an {opus, sonnet} mix nobody measured -- on a
        # non-Anthropic runtime that mix is a different provider's rate card and the
        # resulting delta is a fabricated savings claim.
        baseline_mix_available = bool(anthropic and frozen_opus and frozen_opus > 0) or (
            anthropic and _measure_facade._opus_floor_consented())

        # SUBAGENT (sidechain) pool, read from sidechain transcripts and priced at
        # each subagent's real model vs the ~95% Opus baseline. Subagent tokens do roll
        # up into session_log rows (v5.4.9), but the main pool prices only the FROZEN
        # anchor (rows contribute a count and a hit rate), and the main mix is built
        # parent-only, so the pools do not double-price these dollars. Summed into the
        # headline below. Memoized for dashboard perf.
        sub_pool = _measure_facade._subagent_pool_savings(
            days=days, baseline_opus_share=baseline_opus_share, tier=tier,
            baseline_mix_available=baseline_mix_available)
        sub_window_actual = float(sub_pool.get("actual_usd", 0.0) or 0.0)
        sub_window_cf = float(sub_pool.get("counterfactual_usd", 0.0) or 0.0)
        sub_sessions = int(sub_pool.get("sessions", 0) or 0)

        # SHORT-SESSION pool (below the quality gates): sub-minute / low-input
        # one-shots are excluded from recent_n because the count multiplies the
        # frozen typical-session anchor, but their billed tokens are real spend
        # whose routing delta belongs in the _compat.facade()._headline. Priced on their own real
        # volume, parent-thread bundles only (no overlap with the sidechain pool).
        short_pool = _measure_facade._short_session_pool_savings(
            cutoff, baseline_opus_share=baseline_opus_share, tier=tier,
            baseline_mix_available=baseline_mix_available)
        short_actual = float(short_pool.get("actual_usd", 0.0) or 0.0)
        short_cf = float(short_pool.get("counterfactual_usd", 0.0) or 0.0)
        short_delta = short_cf - short_actual
        short_n = int(short_pool.get("sessions", 0) or 0)

        def price(fi, cr, out, shares):
            # Price the fresh+cache_read pool and output (NO cache-write). _cost_per_session
            # is per-token-linear, so aggregate totals price the whole window directly.
            _measure_facade = _compat.facade()
            return _measure_facade._cost_per_session(fi, 0.0, cr, out, shares, tier)

        def cw_session_cost(shares):
            # Price the FROZEN typical session's cache-write at `shares`. The pre-TO TTL
            # split is not recorded in the baseline, so treat it as 5m (conservative,
            # matching the current-window no-split fallback). Cache-write IS a routing
            # lever: cache-creation tokens bill at the WRITING model's rate (Opus
            # $6.25 vs Sonnet $3.75 /MTok), so each arm prices CW at its OWN mix.
            # Pass cache_write as the positional `cache_create`; with no 1h/5m split given,
            # _get_model_cost prices the whole amount at the 5m rate (the conservative
            # no-split fallback). bcw is clamped >= 0 against a corrupt baseline_state.json.
            _measure_facade = _compat.facade()
            cw_tok = max(0, int(bcw))
            items = [(mdl, s) for mdl, s in (shares or {}).items() if s and s > 0]
            default_model = _measure_facade._default_model_for_runtime()
            if not items:
                return _measure_facade._get_model_cost(default_model, 0, 0, 0, cw_tok, tier=tier)
            tot = sum(s for _, s in items)
            return sum(
                s * _measure_facade._get_model_cost(
                    mdl if _measure_facade._is_priced_model(mdl, tier) else default_model,
                    0, 0, 0, cw_tok, tier=tier)
                for mdl, s in items) / tot

        # --- PER-TYPICAL-SESSION pricing (FROZEN baseline volume = a factual anchor) ---
        # Both arms price the SAME frozen typical session (bfi/bcr/bcw/bout from
        # baseline_state.json), so the per-session figures move ONLY with efficiency
        # (model mix + cache reuse), NEVER with this period's workload size. Volume
        # re-enters solely as the session-count multiplier for the monthly total below.
        # This makes "the old way / session" a stable, repeatable baseline rather than a
        # number re-derived from whatever volume happened to fall in the current window.
        t_pool = bfi + bcr  # frozen fresh + cache_read pool (cache_write priced separately)
        # OLD WAY (counterfactual) = the typical session exactly as it was pre-TO: its
        # native fresh/cache_read split, priced at the baseline (~95% Opus) mix.
        old_cps = price(bfi, bcr, bout, before_shares) + cw_session_cost(before_shares)
        # NOW (actual) = the SAME frozen tokens, but the pool redistributed at the CURRENT
        # pool-hit rate and priced at the CURRENT mix. cf split keeps t_pool exact.
        cur_CR_s = cur_hit * t_pool
        cur_F_s = t_pool - cur_CR_s
        now_cps = price(cur_F_s, cur_CR_s, bout, after_shares) + cw_session_cost(after_shares)
        # `NaN <= 0` is False, so a non-finite price (e.g. a corrupt custom pricing tier
        # carrying inf/NaN) would slip past the guard. Treat non-finite as insufficient.
        # Bail if either arm is non-finite or non-positive. now_cps<=0 mirrors the old
        # guard on actual_monthly (= now_cps * recent_n): a zero "now" arm would otherwise
        # inflate main_transformation to the full counterfactual (a fake 100% saving).
        _frozen_arms_ok = (all(_measure_facade.math.isfinite(x) for x in (old_cps, now_cps))
                           and old_cps > 0 and now_cps > 0)
        if not _frozen_arms_ok:
            # The frozen-anchor arms are unusable. That is fatal ONLY when they are what
            # the headline is built from; the session-weight pool supersedes them and is
            # measured on its own population, so it can still carry the card.
            if not weight_pool:
                return {**zero, "before_sessions": bn, "after_sessions": recent_n,
                        "reason": "insufficient_history"}
            old_cps = now_cps = 0.0
        # Monthly main arms = the per-session anchor x the current session count. The
        # per-session anchors are stable; only the count (and efficiency) scale the total.
        actual_monthly = now_cps * recent_n
        counterfactual_monthly = old_cps * recent_n

        # COMPRESSION ADD-BACK: the VOLUME-REDUCTION lever. Token Optimizer REMOVES
        # tokens from context (tool_archive, structure_map skeletons, delta_read).
        # Those tokens are real volume the OLD way would
        # have kept re-reading and paying for. resume_lean and checkpoint_restore
        # are counterfactual credits (estimated tier, popped before this total),
        # so they ride as labelled estimated pools below instead of here. They are NOT in session_log (already removed
        # before billing), so this pool is DISJOINT from the main pool AND from the caching
        # lever (which only redistributes BILLED fresh+cache_read tokens). We take the
        # directly-METERED removed-token dollars (savings_events) and reprice them to the
        # baseline mix: the ~95% Opus "old way" would have paid even more per re-read
        # than today's mix did. The measured figure is the proven floor of this lever; the
        # reprice (the routing recipe's standardized baseline/actual ratio) is the only
        # estimated step and is disclosed. actual for this pool is 0 (the tokens were
        # never billed), so its whole repriced value is transformation. Volume reduction
        # remains in the headline as its own labelled pool.
        comp = _measure_facade._get_savings_summary(days=days, since=since)
        comp_measured = float((comp or {}).get("total_cost_usd", 0.0) or 0.0)
        # Reprice add-backs at the routing recipe's standardized baseline/actual
        # ratio -- the factor the plan card publishes, from the recipe that
        # prices the Savings tab's routing figure. The recipe is resolved ONCE
        # here (the caller's `model_routing` payload, or one routing pass on the
        # same `days`/`since` window) so the ratio and its disclosure basis can
        # never disagree and a direct call never runs the pass twice.
        # The recipe owns exact-card pricing, premium exclusion, reference cards
        # and the pending gate; there is no second routing implementation.
        routing_payload = model_routing
        if routing_payload is None:
            try:
                routing_payload = (_measure_facade._compute_model_routing_savings(days=days, since=since)
                                   or {})
            except Exception:
                routing_payload = {}
        comp_reprice = _measure_facade._routing_mix_reprice_ratio(routing_payload, days=days, since=since)
        comp_reprice_basis = _measure_facade._routing_reprice_basis(routing_payload)
        compression_addback = max(0.0, comp_measured * comp_reprice)

        # Estimated avoided-volume pools that no billed-volume arm can observe.
        # Each stays separately labelled and signed. MCP output capping is not a
        # Token Optimizer action, so it remains disclosed in the estimated tier
        # but is deliberately absent from the transformation counterfactual.
        if estimated_pools is None:
            estimated_pools = {
                "uncaptured_runtime": _measure_facade._estimate_uncaptured_runtime(days=days),
                "behavioral_loops": _measure_facade._estimate_behavioral_savings(days=days),
                "hint_followed": (comp or {}).get("hint_followed_estimated") or {},
                "handover_rerun": _measure_facade._estimate_handover_rerun_savings(days=days),
                "retrieval_serve": _measure_facade._estimate_retrieval_serve_savings(days=days),
                "checkpoint_restore": (comp or {}).get("checkpoint_restore_estimated") or {},
                "codemap_lookup": (comp or {}).get("codemap_lookup_estimated") or {},
            }
        pool_labels = {
            "uncaptured_runtime": "Compressed subagent context (estimated)",
            "behavioral_loops": "Prevented loop continuations (estimated)",
            "hint_followed": "Followed context hints (estimated)",
            "handover_rerun": "Continuity handovers (estimated)",
            "retrieval_serve": "Direct context retrieval (estimated)",
            "checkpoint_restore": "Checkpoint restores (estimated)",
            "codemap_lookup": "Code map lookups (estimated)",
        }
        estimated_volume_pools = {}
        for key, label in pool_labels.items():
            current_mix_usd = float((estimated_pools.get(key) or {}).get("cost_saved_usd", 0.0) or 0.0)
            delta = current_mix_usd * comp_reprice
            estimated_volume_pools[key] = {
                "label": label,
                "actual_usd": 0.0,
                "counterfactual_usd": round(delta, 2),
                "transformation_usd": round(delta, 2),
                "current_mix_usd": round(current_mix_usd, 4),
            }
        estimated_volume_addback = sum(
            pool["transformation_usd"] for pool in estimated_volume_pools.values())

        # Verbosity-steer add-back: estimated output tokens never produced due to
        # conciseness nudges. The main counterfactual holds output volume constant
        # (both arms price the same O), so the output reduction from nudges is NOT
        # captured by main_transformation. These are estimated savings (10-15% of
        # affected response tokens), logged to savings_events but relocated to the
        # estimated tier in _get_savings_summary. Reprice to the baseline OUTPUT
        # rate: the old way would have produced the same verbose output at the
        # baseline model mix's output rate, so the avoided cost is higher.
        vs_est = (comp or {}).get("verbosity_steer_estimated") or {}
        vs_measured = 0.0  # diagnostic only; no verbosity credit
        out_after = price(0.0, 0.0, 1_000_000.0, after_shares)
        out_before = price(0.0, 0.0, 1_000_000.0, before_shares)
        vs_reprice = (out_before / out_after) if out_after > 0 else 1.0
        verbosity_addback = max(0.0, vs_measured * vs_reprice)

        # MAIN pool delta (monthly) = counterfactual - actual, UNCLAMPED. The window is 30d,
        # so this is already the monthly figure. A net-negative main pool (now marginally
        # costlier than the frozen baseline this period) is carried honestly into the combined
        # net below rather than clamped to 0. Clamping the main pool while the "now vs old way"
        # arms stayed honest is exactly what made the headline and the arms disagree in sign.
        # The top line summed only non-negative pools and could assert a saving
        # while the arms showed a loss. The headline is now the honest net of the same arms.
        main_transformation = counterfactual_monthly - actual_monthly
        # The sidechain arms already aggregate the full `days` window, so they ARE
        # the monthly figures and enter the net at face value. Rescaling them by a
        # session-count ratio would need a delegations-per-session anchor measured
        # under ONE inclusion rule; the available counts (quality-filtered DB main
        # sessions vs raw on-disk sidechain transcripts) are measured under
        # different rules, so any ratio of them is unitless and shrinks or inflates
        # real delegated spend. Use the raw arms, which exist in every cache
        # payload version, so cache schema cannot change the accounting path. The
        # signed delta preserves genuinely costlier routing.
        sub_actual = sub_window_actual
        sub_cf = sub_window_cf
        sub_delta = sub_cf - sub_actual
        premium_delegation_cost = float(
            sub_pool.get("premium_delegation_usd", 0.0) or 0.0)
        # --- SESSION-WEIGHT pool: the volume lever, over the WHOLE parent population ---
        # When available this SUPERSEDES every other transformation pool rather than
        # adding to them. Its work-normalized per-unit delta (constant prices) already
        # CONTAINS every volume mechanism at once: compression removals, lean-output
        # nudges, loop prevention, handover/retrieval avoidance and session
        # partitioning all express themselves as fewer billed tokens per API call, so
        # adding the compression / verbosity / estimated-volume pools on top would
        # count the same removed tokens twice (once inside the per-call delta, once
        # as repriced events). The main frozen-anchor and short-session pools price
        # the same parent ledger this pool covers completely (no quality gate), so
        # they are superseded rather than summed. Delegated (sidechain) work is NOT
        # in this pool -- sidechain transcripts are separate session_log rows, not
        # rolled into parents on this ledger -- and the sidechain pool's
        # frozen-mix counterfactual is a different methodology, so it is excluded
        # from this headline (disclosed in the caveat; premium-delegation cost still
        # renders its own line). The directly-metered compression floor still renders
        # in its own card from _get_savings_summary, unaffected by this supersession.
        if weight_pool:
            main_transformation = float(weight_pool["transformation_usd"])
            actual_monthly = float(weight_pool["actual_usd"])
            counterfactual_monthly = float(weight_pool["counterfactual_usd"])
            sub_actual = sub_cf = sub_delta = 0.0
            short_actual = short_cf = short_delta = 0.0
            compression_addback = 0.0
            verbosity_addback = 0.0
            estimated_volume_addback = 0.0
            # Mark estimated-volume pools as superseded BEFORE any early return.
            # The net-negative early return below would otherwise leak their
            # nonzero transformation_usd values into the payload, creating an
            # internally inconsistent card (scalar says $0, nested pools say
            # $270). This zeroing must happen here, not after the early return.
            for _pool in estimated_volume_pools.values():
                _pool["superseded_by"] = "session_weight"
                _pool["transformation_usd"] = 0.0
        # Combined arms span every included transformation pool, so the displayed
        # counterfactual and actual values reconcile to the net.
        combined_actual = actual_monthly + sub_actual + short_actual
        combined_cf = (counterfactual_monthly + sub_cf + short_cf
                       + compression_addback + verbosity_addback
                       + estimated_volume_addback)
        net = combined_cf - combined_actual
        # Gate the entire counterfactual hero on a GENUINE net win. When net <= 0 (efficiency
        # roughly flat, or the frozen baseline would have been marginally cheaper this period),
        # surface NO counterfactual claim -- the directly-metered floor card still reports
        # realized savings -- rather than render a negative headline or a clamped-positive one
        # beside net-negative arms. monthly_savings_usd defaults to 0 here, so the dashboard
        # card and the CLI headline (both gated on monthly_savings_usd > 0) stay hidden.
        # The sub-cent band routes to the same state: a positive-but-sub-cent "win" would
        # otherwise return transformation_state "ok" with a $0.00 headline that every
        # renderer gates off, recreating the silent empty card this branch exists to
        # eliminate ("no savings" must stay distinguishable from "the math broke").
        # Both non-headlined states report the SAME real arms and real (signed) delta so the
        # renderer can explain itself; `reason` / `transformation_state` carry which one.
        not_headlined = {**zero, "before_sessions": bn, "after_sessions": recent_n,
                    "actual_monthly_usd": round(combined_actual, 2),
                    "counterfactual_monthly_usd": round(combined_cf, 2),
                    "main_actual_monthly_usd": round(actual_monthly, 2),
                    "main_counterfactual_monthly_usd": round(counterfactual_monthly, 2),
                    "main_transformation_usd": round(main_transformation, 2),
                    "subagent_transformation_usd": round(sub_delta, 2),
                    "premium_delegation_cost_usd": round(premium_delegation_cost, 2),
                    "compression_transformation_usd": 0.0,
                    "compression_measured_usd": round(comp_measured, 2),
                    "compression_reprice_ratio": round(comp_reprice, 4),
                    "compression_reprice_basis": comp_reprice_basis,
                    "estimated_volume_transformation_usd": 0.0,
                    "estimated_volume_pools": estimated_volume_pools,
                    "verbosity_transformation_usd": 0.0,
                    "verbosity_measured_usd": round(vs_measured, 2),
                    "short_session_transformation_usd": round(short_delta, 2),
                    "short_session_actual_usd": round(short_actual, 2),
                    "short_session_counterfactual_usd": round(short_cf, 2),
                    "short_session_count": short_n,
                    "subagent_actual_usd": round(sub_actual, 2),
                    "subagent_counterfactual_usd": round(sub_cf, 2),
                    "subagent_sessions": sub_sessions,
                    "subagent_window_actual_usd": round(sub_window_actual, 2),
                    "subagent_window_counterfactual_usd": round(sub_window_cf, 2),
                    "baseline_opus_share": round(baseline_opus_share, 4),
                    "before_opus": round(baseline_opus_share, 4),
                    "after_opus": round(after_opus, 4),
                    "before_mix_label": _measure_facade._mix_label(before_shares),
                    "after_mix_label": _measure_facade._mix_label(after_shares),
                    # These are known at this point; populate them so the CLI printer's
                    # "Prefix baseline:" line and any serialized caller see real values, not the
                    # `zero` defaults (the dashboard card stays gated on monthly>0).
                    "sessions_per_month": (int(weight_pool["sessions"])
                                           if weight_pool else recent_n),
                    "before_tokens": (int(weight_pool["before_tokens_per_session"])
                                      if weight_pool else int(t_pool + bcw + bout)),
                    "after_tokens": (int(weight_pool["after_tokens_per_session"])
                                     if weight_pool else int(t_pool + bcw + bout)),
                    "baseline_source": baseline_source,
                    # A net-negative card used to return the `zero` per-session fields, so
                    # the dashboard rendered NOTHING and the user could not tell "no savings
                    # this period" from "the computation broke". Report the real arms and the
                    # real (negative) delta, plus a machine-readable state the renderer can
                    # branch on. The headline stays gated on monthly_savings_usd > 0, so this
                    # does NOT resurrect a negative hero number -- it makes the empty state
                    # explainable instead of silent.
                    "before_cost_per_session": round(
                        float(weight_pool["before_cost_per_session"])
                        if weight_pool else old_cps, 4),
                    "after_cost_per_session": round(
                        float(weight_pool["after_cost_per_session"])
                        if weight_pool else now_cps, 4),
                    "savings_per_session": round(
                        (float(weight_pool["before_cost_per_session"])
                         - float(weight_pool["after_cost_per_session"]))
                        if weight_pool else (old_cps - now_cps), 4),
                    "before_cache_hit": round(
                        (bcr / (bfi + bcw + bcr)) if (bfi + bcw + bcr) > 0 else 0.0, 4),
                    "after_cache_hit": round(
                        (CR / (F + CW + CR)) if (F + CW + CR) > 0 else 0.0, 4),
                    "before_opus_label": _measure_facade._mix_label(before_shares),
                    # When the session-weight pool drove the headline, its
                    # arms -- not the frozen-anchor pair -- are the ones the net
                    # was built from; surface them so the card explains itself.
                    "session_weight_pool": (_measure_facade._weight_pool_payload(weight_pool)
                                            if weight_pool else None)}
        if net < 0.01:
            return {**not_headlined,
                    "net_shortfall_usd": round(net, 2),
                    "transformation_state": "net_negative",
                    "reason": "net_negative"}
        # A positive net is only headline-worthy when the two independent
        # work-unit methods agree on it. When the per-API-call and per-message
        # transformations are more than 10% apart (or on opposite sides of
        # zero), neither figure is trustworthy enough to headline: the card
        # reports both instead of picking one. Same non-headlined shape as
        # net_negative; both disputed figures ride on session_weight_pool.
        # Both work-unit methods inside a small band of spend means cost per
        # unit of work did not measurably change: say "flat" rather than show
        # two near-zero figures as a disagreement.
        if weight_pool and _measure_facade._crosscheck_flat(weight_pool, combined_cf):
            return {**not_headlined,
                    "transformation_state": "flat",
                    "reason": "flat"}
        if weight_pool and not _measure_facade._crosscheck_verdict(weight_pool):
            return {**not_headlined,
                    "transformation_state": "methods_disagree",
                    "reason": "methods_disagree"}
        # Combined headline = the honest net of every pool (== combined_cf - combined_actual,
        # equivalently main_delta + subagent_delta + compression + verbosity). The per-lever
        # waterfall below decomposes exactly this quantity, so the breakdown reconciles to the
        # top line even when a pool grew. pct is over the combined counterfactual (the full
        # spend base being transformed); guaranteed in (0, 1] here because net > 0.
        transformation = net
        transformation_pct = (net / combined_cf if combined_cf > 0 else 0.0)

        # --- Attribution breakdown (waterfall over the two behavioral levers) ---
        # The gap is driven by exactly two efficiency levers on a held-constant volume:
        # model routing (mix, now INCLUDING the cache-write reprice) and caching (cache-hit
        # redistribution). Decompose by morphing the counterfactual into the actual one
        # lever at a time. Routing is credited first (the baseline footprint, INCLUDING CW,
        # repriced from the baseline mix to today's mix), then caching at today's mix; the
        # two UNROUNDED steps telescope to the _compat.facade()._headline. Sequential attribution is
        # order-dependent (fixed + disclosed via `waterfall_index`).
        # v_route_s = the typical session's baseline cache split (bfi/bcr) + CW + output
        # repriced at today's mix. CW carries the routing reprice, so the routing lever
        # captures the cache-write mix-delta too. Computed per-session, then x recent_n so
        # the levers telescope to the per-session-based main_transformation exactly.
        # Always decompose the main pool, even when it is net-negative, so the waterfall sums
        # to the honest net including a pool that grew. s_route + s_cache telescope to
        # main_transformation regardless of sign; a grown pool shows honestly as a "-$X" lever.
        v_route_s = price(bfi, bcr, bout, after_shares) + cw_session_cost(after_shares)
        s_route = (old_cps - v_route_s) * recent_n   # before->after mix (incl. CW)
        s_cache = (v_route_s - now_cps) * recent_n   # remaining gap = caching
        # s_route + s_cache telescopes to the MAIN transformation; the subagent pool is a
        # third lever whose own (cf - actual) is added so the breakdown sums to the COMBINED
        # _compat.facade()._headline.
        if weight_pool:
            # The session-weight pool replaced the frozen-anchor arms, so the
            # routing/caching decomposition of those arms no longer describes the
            # headline and must not be shown beside it -- it would not sum. The whole
            # measured delta is one work-normalized lever: cost per unit of work
            # (API call), same population rule both sides, constant prices. (Its
            # routing and caching components are not separable without re-introducing
            # the frozen anchor that could not see the volume change in the first
            # place; the constant-rate-card construction is what keeps vendor/mix
            # price drift out of the claim.)
            _unit_lbl = ("API call" if weight_pool.get("work_unit") == "api_call"
                         else "message")
            _levers = [
                ("session_weight",
                 f"Lighter work (fewer tokens per {_unit_lbl})",
                 f"Heavier work (more tokens per {_unit_lbl})", main_transformation),
            ]
        else:
            _levers = [
                ("routing", "Smarter model routing (lighter mix)",
                 "Model mix shifted to costlier models (added cost)", s_route),
                ("context_rereads", "Lighter sessions (better cache reuse)",
                 "Heavier context re-reads (added cost)", s_cache),
                ("subagent_routing", "Cheaper subagents (lighter models)",
                 "Costlier subagents (premium models)", sub_delta),
                ("short_sessions", "Quick one-shot sessions (lighter mix)",
                 "Quick one-shot sessions (costlier mix)", short_delta),
            ]
        _levers += [
            ("context_compression", "Lighter context (fewer re-reads, metered)",
             "Lighter context (fewer re-reads, metered)", compression_addback),
            ("verbosity_steer", "Lean output nudges (less output, estimated)",
             "Verbosity nudges (less output, estimated)", verbosity_addback),
        ]
        # The estimated-volume pools are SUPERSEDED by the session-weight pool for the
        # same reason `estimated_volume_addback` is zeroed above: avoided reads, loop
        # continuations, followed hints, handovers and retrieval already show up as
        # fewer billed tokens per API call inside the per-unit delta. The scalar was
        # zeroed but these per-pool dicts still carried their own dollar figures into
        # the waterfall, so the breakdown summed to more than the headline it is
        # supposed to decompose. Mark them superseded and keep them OUT of the waterfall;
        # they stay on the payload for disclosure with a zeroed transformation.
        # NOTE: the zeroing itself was moved BEFORE the net-negative early return
        # above to prevent leaking unsuperseded pools in that path. Here we only
        # need the else branch (extend _levers when weight_pool is NOT active).
        if not weight_pool:
            _levers.extend(
                (key, pool["label"], pool["label"], pool["transformation_usd"])
                for key, pool in estimated_volume_pools.items()
            )
        breakdown = sorted(
            ({"key": k, "waterfall_index": i,
              "label": (pos if d >= 0 else neg),
              "monthly_usd": round(d, 2)}
             for i, (k, pos, neg, d) in enumerate(_levers)),
            key=lambda x: ((_measure_facade := _compat.facade()), abs(x["monthly_usd"]))[1], reverse=True,
        )
        if weight_pool:
            breakdown_caveat = (
                "Work-normalized counterfactual: every API call you dispatched this "
                "period, priced at the anchor month's cost per call ("
                + str(weight_pool["anchor_month"]) + ", your earliest complete month "
                "on record), at ONE constant rate card both eras -- so only how much "
                "context each call carries (and how often the cache serves it) moves "
                "the number; model-price and mix changes between eras are excluded and "
                "disclosed separately, not claimed. Sessions are not used as the unit "
                "because the lighter workflow itself splits the same work into more, "
                "smaller sessions, which would count capacity gained as money saved. "
                "The delta includes every volume mechanism at once (context "
                "compression, lean output, loop prevention, continuity handovers), so "
                "the metered and estimated pools are not added on top -- they would "
                "double-count; the directly-metered floor still shows in its own card. "
                "Delegated (subagent) work is excluded from this figure. Two caveats "
                "ride with it: the anchor month already had partial Token Optimizer "
                "coverage (the true pre-tool gap is larger), and the per-call delta "
                "cannot separate Token Optimizer's effect from your own behaviour "
                "drift -- the metered floor is the part we can prove action by action."
            )
        else:
            breakdown_caveat = (
            "Counterfactual attribution: your FROZEN typical pre-TO session is priced two "
            "ways -- the way you work now, and the way you worked before Token Optimizer "
            "(" + ("about %d%% Opus, " % round(100 * baseline_opus_share) if baseline_opus_share > 0 else "") + "your old cache pattern) -- then scaled by your session count this "
            "period. The per-session difference is split between model routing (including "
            "cache-write, which is billed at the writing model's rate) and caching. Subagent "
            "(sidechain) work is a second pool: the window's real delegated tokens priced at "
            "each subagent's actual model vs the same baseline mix, entering the net at face "
            "value. A third pool prices sessions below the quality gates (sub-minute "
            "one-shots) the same way on their own real volume; they stay out of the session "
            "count. A fourth pool adds the directly-metered tokens Token Optimizer removed "
            "from context (tool archiving, skeletons, lean resumes) -- real volume the old "
            "way would have re-read -- repriced at the baseline mix. A fifth pool adds "
            "estimated output-token savings from lean-output conciseness nudges. The "
            "remaining estimated pools add avoided subagent context, loop continuations, "
            "followed hints, continuity reruns, and exploratory retrieval reads as separate "
            "lines. Host-runtime MCP capping and opportunity-tier items stay outside the "
            "transformation. The "
            "per-session volume anchor is frozen, so workload size never inflates or zeroes "
            "the baseline."
        )

        # Per-session keys = the FROZEN typical-session anchors directly (not a re-division
        # of the monthly aggregate, which would re-introduce rounding drift). before_* is
        # "the old way" (stable across runs); after_* is "now" (moves only with efficiency).
        before_cps = old_cps
        after_cps = now_cps
        sessions_out = recent_n
        if weight_pool:
            # The displayed arms must be the arms the headline was actually built from.
            # Reporting the frozen-anchor pair beside a session-weight headline is what
            # made the old card self-contradictory (arms said one thing, top line another).
            before_cps = float(weight_pool["before_cost_per_session"])
            after_cps = float(weight_pool["after_cost_per_session"])
            sessions_out = int(weight_pool["sessions"])
        # Per-session cache-reuse split. Identical displayed tokens + model mix on both arms
        # can still price differently because the OLD way used the baseline session's native
        # cache-read fraction while NOW redistributes the same pool at the current hit rate.
        # Surfacing both fractions makes that cost delta explainable rather than looking
        # impossible ("same tokens, same mix, different cost" -- the contradiction).
        # DISPLAYED hit rate counts cache-WRITE as a miss: creating a cache entry bills
        # 1.25x the input rate, so a session that rewrites its context every turn is not
        # "99.95% cached" in any sense the user would recognise. The pool-only ratio
        # bcr/(bfi+bcr) is additionally a decomposition artifact -- _session_token_vector
        # derives fresh as `input*(1-hit) - cache_write`, so a large cache_write crushes
        # fresh toward zero and drives the ratio to ~1 mechanically (the JULY anchor
        # reported 0.9995 this way). Denominator is the full billed input side.
        before_cache_hit = (bcr / (bfi + bcw + bcr)) if (bfi + bcw + bcr) > 0 else 0.0
        after_cache_hit = (CR / (F + CW + CR)) if (F + CW + CR) > 0 else 0.0
        # NOTE: `base_hit` / `cur_hit` above deliberately keep the pool-only definition --
        # they drive the counterfactual's fresh/cache_read REDISTRIBUTION, where cache-write
        # is held constant in tokens and must stay out of the pool being redistributed.
        # This pair is for DISPLAY only; the two definitions are not interchangeable.
        # combined_actual / combined_cf / net are computed above (the headline is their net).
        # --- Spend cap: the reported saving can never exceed actual spend ---
        # A counterfactual saving larger than the user's real bill is indefensible
        # to a paying customer, no matter how arithmetically correct. The cheap-
        # month-expensive-anchor scenario (holiday, project switch) is not exotic:
        # it happens to anyone whose current window is light. When the cap binds,
        # disclose it -- a number that silently stops moving is its own kind of lie.
        # The uncapped figure is preserved on the payload for auditability.
        uncapped_transformation = transformation
        savings_capped = False
        savings_cap_note = None
        if transformation > combined_actual and combined_actual > 0:
            transformation = combined_actual
            savings_capped = True
            savings_cap_note = (
                "The counterfactual saving for this period exceeds your actual "
                "spend, so the figure is shown conservatively (capped at what "
                "you actually paid). The uncapped estimate was "
                f"${uncapped_transformation:,.2f}."
            )
            # Re-derive pct from the capped transformation so the percentage
            # stays consistent with the capped _compat.facade()._headline.
            transformation_pct = (
                transformation / combined_cf if combined_cf > 0 else 0.0
            )
        return {
            "before_cost_per_session": round(before_cps, 4),
            "after_cost_per_session": round(after_cps, 4),
            "savings_per_session": round(before_cps - after_cps, 4),
            "before_cache_hit": round(before_cache_hit, 4),
            "after_cache_hit": round(after_cache_hit, 4),
            "sessions_per_month": sessions_out,
            "monthly_savings_usd": round(transformation, 2),  # = honest net (combined_cf - combined_actual), capped at actual spend
            "uncapped_monthly_savings_usd": round(uncapped_transformation, 2),  # pre-cap figure for audit
            "savings_capped": savings_capped,  # True when the spend cap bound
            "savings_cap_note": savings_cap_note,  # human-readable disclosure when cap binds
            "main_transformation_usd": round(main_transformation, 2),
            "subagent_transformation_usd": round(sub_delta, 2),
            "premium_delegation_cost_usd": round(premium_delegation_cost, 2),
            "compression_transformation_usd": round(compression_addback, 2),
            "compression_measured_usd": round(comp_measured, 2),
            "compression_reprice_ratio": round(comp_reprice, 4),
            "compression_reprice_basis": comp_reprice_basis,
            "estimated_volume_transformation_usd": round(estimated_volume_addback, 2),
            "estimated_volume_pools": estimated_volume_pools,
            "verbosity_transformation_usd": round(verbosity_addback, 2),
            "verbosity_measured_usd": round(vs_measured, 2),
            "short_session_transformation_usd": round(short_delta, 2),
            "short_session_actual_usd": round(short_actual, 2),
            "short_session_counterfactual_usd": round(short_cf, 2),
            "short_session_count": short_n,
            # Headline arms span every pool (combined).
            "counterfactual_monthly_usd": round(combined_cf, 2),
            "actual_monthly_usd": round(combined_actual, 2),
            # Main-pool-only arms (for drill-down / back-compat).
            "main_counterfactual_monthly_usd": round(counterfactual_monthly, 2),
            "main_actual_monthly_usd": round(actual_monthly, 2),
            "subagent_counterfactual_usd": round(sub_cf, 2),
            "subagent_actual_usd": round(sub_actual, 2),
            "subagent_sessions": sub_sessions,
            "subagent_window_actual_usd": round(sub_window_actual, 2),
            "subagent_window_counterfactual_usd": round(sub_window_cf, 2),
            "subagent_by_model": sub_pool.get("by_model", {}),
            "transformation_pct": round(transformation_pct, 4),
            # Distinguishes a genuine positive result from the net-negative empty state
            # and from "no baseline yet" -- all three used to be an all-zero payload.
            "transformation_state": "ok",
            # Same key set as `zero`: a full result has no bail-out reason. Callers
            # index `["reason"]` on both shapes without a KeyError.
            "reason": None,
            # Session-weight pool disclosure. The headline is the work-normalized
            # (per-API-call, constant-price) delta; `capacity_assumption` is the
            # disclosure sentence the card MUST surface next to the headline rather
            # than bury. There is deliberately NO session-basis dollar figure here:
            # pricing the current session count at the old per-session cost counts
            # capacity gained as money saved, which is the inflation this pool
            # exists to avoid.
            "session_weight_pool": (_measure_facade._weight_pool_payload(weight_pool)
                                    if weight_pool else None),
            "capacity_neutral_monthly_usd": None,
            "baseline_opus_share": round(baseline_opus_share, 4),
            "before_opus": round(baseline_opus_share, 4),
            "after_opus": round(after_opus, 4),
            "before_mix_label": _measure_facade._mix_label(before_shares),
            "after_mix_label": _measure_facade._mix_label(after_shares),
            # Per-session token footprint (the frozen typical session). Both arms hold the
            # same volume, so before == after. This is genuinely PER SESSION, so the
            # dashboard's "tokens/session" label is now correct (was the monthly total).
            "before_tokens": (int(weight_pool["before_tokens_per_session"])
                              if weight_pool else int(t_pool + bcw + bout)),
            "after_tokens": (int(weight_pool["after_tokens_per_session"])
                             if weight_pool else int(t_pool + bcw + bout)),
            "before_sessions": (int(weight_pool["before_sessions"])
                                if weight_pool else bn),
            "after_sessions": (int(weight_pool["sessions"])
                               if weight_pool else recent_n),
            "baseline_source": baseline_source,
            "breakdown": breakdown,
            "breakdown_caveat": breakdown_caveat,
            "evidence": "estimated",
        }
    # ArithmeticError covers ZeroDivisionError + OverflowError + FloatingPointError, so the
    # "Never raises" contract holds even if a future lever adds an unguarded division.
    except (_measure_facade.sqlite3.Error, OSError, ValueError, TypeError, KeyError, ArithmeticError):
        return zero


def _estimate_stale_reads_reclaimable(days=None):
    """Reclaimable stale-read waste over the window — OPPORTUNITY tier only.

    Sums session_log.stale_waste_tokens: per-session reads that slipped through
    (re-read after write, or far-distance stale) and were BILLED. This is NOT
    realized savings — it is waste the user COULD reclaim by avoiding redundant
    re-reads, using .contextignore, or compacting. It is never summed into the
    realized total (that would re-introduce the inflation the integrity fixes
    remove). Priced at the input rate; reports the contributing session count as
    an honest sample size. Returns zeros (evidence=opportunity) with no data.
    Never raises.
    """
    _measure_facade = _compat.facade()
    zero = {"tokens": 0, "cost_usd": 0.0, "sessions": 0, "evidence": "opportunity"}
    try:
        if not _measure_facade.TRENDS_DB.exists():
            return zero
        cutoff = (_measure_facade.datetime.now() - _measure_facade.timedelta(days=days)).strftime("%Y-%m-%d")
        conn = _measure_facade._init_trends_db()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(stale_waste_tokens),0), "
                "COALESCE(SUM(CASE WHEN stale_waste_tokens > 0 THEN 1 ELSE 0 END),0) "
                "FROM session_log WHERE date >= ?",
                (cutoff,),
            ).fetchone()
        finally:
            conn.close()
        tokens = int(row[0] or 0)
        sessions = int(row[1] or 0)
        if tokens <= 0:
            return zero
        rate = _measure_facade._window_input_rate_per_mtok(days=days)
        return {
            "tokens": tokens,
            "cost_usd": round(tokens * rate / 1_000_000, 4),
            "sessions": sessions,
            "evidence": "opportunity",
        }
    except (_measure_facade.sqlite3.Error, OSError):
        return zero


def _progressive_disclosure_summary(days=None):
    """Count-first view of tool-archive progressive disclosure (B6, informational).

    How many tool results were archived vs later re-expanded, and the net tokens
    that stayed collapsed. The $ value of the net is ALREADY in the realized
    tool_archive figure (re-pops are netted out in _get_savings_summary); this is
    a transparency count, NOT an additional saving to sum. Never raises.
    """
    _measure_facade = _compat.facade()
    out = {"archived": 0, "reexpanded": 0, "net_collapsed_tokens": 0}
    try:
        if not _measure_facade.TRENDS_DB.exists():
            return out
        cutoff = (_measure_facade.datetime.now() - _measure_facade.timedelta(days=days)).isoformat()
        conn = _measure_facade._init_trends_db()
        try:
            counted = _measure_facade._counted_table(conn, "savings_events")
            rows = conn.execute(
                "SELECT event_type, COUNT(*), COALESCE(SUM(tokens_saved),0) "
                "FROM " + counted + " WHERE timestamp >= ? "
                "AND event_type IN ('tool_archive','tool_archive_reexpand') GROUP BY event_type",
                (cutoff,),
            ).fetchall()
            re_rows = conn.execute(
                "SELECT detail FROM " + counted + " WHERE timestamp >= ? "
                "AND event_type = 'tool_archive_reexpand'",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
        agg = {r[0]: (int(r[1]), int(r[2] or 0)) for r in rows}
        arch_n, arch_tok = agg.get("tool_archive", (0, 0))
        re_tok = agg.get("tool_archive_reexpand", (0, 0))[1]
        out["archived"] = arch_n
        # Multiple bounded debits can hit one archived item; "re-expanded" is a
        # count of distinct tool_use_ids, not debit events.
        out["reexpanded"] = len({
            detail.split(" ", 1)[0]
            for (detail,) in re_rows
            if detail and detail.strip()
        })
        out["net_collapsed_tokens"] = max(0, arch_tok - re_tok)
    except (_measure_facade.sqlite3.Error, OSError):
        pass
    return out


def _logged_oneshot_pricing(by_category):
    """Disclosure for the logged context removals in *by_category*.

    A removal is logged when the hook fires, before the next call shows how the
    provider billed the context it displaced, so every logged removal is priced
    at the input rate: a minimum. Counts only categories that remove context.
    """
    _measure_facade = _compat.facade()
    events = tokens = 0
    for key, entry in by_category.items():
        removes_context = ((key in _measure_facade._COUNTED_SE_EVENT_TYPES and key != "tool_archive_reexpand")
                           or (key in _measure_facade._V5_COMPRESSION_CATEGORIES
                               and key not in _measure_facade._COUNTED_CE_EXCLUDED_FEATURES))
        saved = int(entry.get("tokens_saved", 0) or 0)
        if removes_context and saved > 0:
            events += int(entry.get("events", 0) or 0)
            tokens += saved
    return {"basis": "input_rate_minimum", "removal_events": events, "removal_tokens": tokens}


def _apply_counted_oneshot_pricing(summary, counted):
    """Add what the counted ledger knows about billed classes to the disclosure."""
    _measure_facade = _compat.facade()
    pricing = dict(summary.get("oneshot_pricing") or {})
    if pricing:
        counted = counted or {}
        pricing.update(
            class_events=int(counted.get("oneshot_class_events", 0) or 0),
            class_premium_usd=round(float(counted.get("oneshot_class_premium_usd", 0.0) or 0.0), 4),
            floor_events=int(counted.get("oneshot_floor_events", 0) or 0),
            floor_tokens=int(counted.get("oneshot_floor_tokens", 0) or 0))
        summary["oneshot_pricing"] = pricing


def _merge_v5_compression(by_category, by_feature, fallback_cost_per_mtok):
    """Merge realized v5 compression_events features into *by_category* in place.

    Shared by _get_merged_savings and the status-bar buckets. Only known v5
    features merge; a key savings_events already owns is skipped (dedup).
    Returns the (tokens, cost_usd, events) added.
    """
    _measure_facade = _compat.facade()
    add_tok, add_cost, add_events = 0, 0.0, 0
    for feature, fdata in by_feature.items():
        if feature not in _measure_facade._V5_COMPRESSION_CATEGORIES:
            continue  # only merge known v5 features; experimental keys stay out
        if feature in by_category:
            continue  # dedup: savings_events already owns this key
        tokens_saved = int(fdata.get("tokens_saved", 0) or 0)
        events = int(fdata.get("events", 0) or 0)
        if tokens_saved <= 0 and events <= 0:
            continue
        # A missing price does not identify a rate; keep the tokens unpriced.
        if "cost_saved_usd" in fdata and fdata["cost_saved_usd"] is not None:
            cost_saved = round(float(fdata["cost_saved_usd"]), 4)
        else:
            cost_saved = 0.0
        unpriced_default = fdata.get("cost_saved_usd") is None
        unpriced_credit = fdata.get("unpriced_credit_tokens", max(0, tokens_saved) if unpriced_default else 0)
        unpriced_debit = fdata.get("unpriced_debit_tokens", max(0, -tokens_saved) if unpriced_default else 0)
        by_category[feature] = {
            "events": events,
            "tokens_saved": tokens_saved,
            "cost_saved_usd": cost_saved,
            "unpriced_tokens": fdata.get("unpriced_tokens", unpriced_credit + unpriced_debit),
            "unpriced_credit_tokens": unpriced_credit,
            "unpriced_debit_tokens": unpriced_debit,
        }
        add_tok += tokens_saved
        add_cost += cost_saved
        add_events += events
    return add_tok, add_cost, add_events


def _allocate_cents(amounts, total=None):
    """(cents per amount, cents of the total), summing exactly.

    The total is the exact sum (or `total`) rounded half up to a cent. Each
    amount gets its floor in cents and the cents still missing go to the
    largest fractional remainders, so printed parts always add up to the
    printed total. Amounts that cannot be reconciled (a `total` further from
    the parts than one cent each) fall back to plain rounding."""
    _measure_facade = _compat.facade()
    from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

    def cents(value):
        _measure_facade = _compat.facade()
        try:
            number = float(value)
        except (TypeError, ValueError):
            return Decimal(0)
        return Decimal(repr(number)) * 100 if _measure_facade.math.isfinite(number) else Decimal(0)

    exact = [cents(a) for a in amounts]
    target = int((cents(total) if total is not None else sum(exact, Decimal(0)))
                 .quantize(Decimal(1), rounding=ROUND_HALF_UP))
    floors = [int(x.to_integral_value(ROUND_FLOOR)) for x in exact]
    leftover = target - sum(floors)
    if not 0 <= leftover <= len(exact):
        return [int(x.quantize(Decimal(1), rounding=ROUND_HALF_UP)) for x in exact], target
    order = sorted(range(len(exact)), key=lambda i: ((_measure_facade := _compat.facade()), (exact[i] - floors[i], -i))[1], reverse=True)
    for i in order[:leftover]:
        floors[i] += 1
    return floors, target


def _format_cents(cents):
    """'1234.56' style text for a whole number of cents."""
    _measure_facade = _compat.facade()
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def _action_savings_display(logged, routing_measured, routing_estimated, repeat_reads,
                            estimates, total_clamped=None, class_premium=None):
    """Whole-cent display values for the action-savings components.

    Raw component values stay exact elsewhere in the payload. Every displayed
    part below adds up to `total_usd`, which is the cents of the exact total."""
    _measure_facade = _compat.facade()
    names = ["logged", "class_premium", "routing_measured", "routing_estimated", "reread"] + [
        "estimate:" + key for key in estimates]
    values = [logged, class_premium or 0.0, routing_measured, routing_estimated, repeat_reads, *estimates.values()]
    parts, total_cents = _measure_facade._allocate_cents(values)
    if total_clamped:
        total_cents = 0
    by_name = dict(zip(names, parts))
    return {
        "total_cents": total_cents,
        "total_usd": total_cents / 100,
        "components_cents": {k: v for k, v in by_name.items() if not k.startswith("estimate:")},
        "components_usd": {k: v / 100 for k, v in by_name.items() if not k.startswith("estimate:")},
        "estimates_cents": {k[len("estimate:"):]: v for k, v in by_name.items() if k.startswith("estimate:")},
        "estimates_usd": {k[len("estimate:"):]: v / 100 for k, v in by_name.items() if k.startswith("estimate:")},
    }


def _action_savings_components(summary, reread=None):
    """Sum action savings for one period without changing the measured ledger.

    Repeat reads add only their later benefit, never initial removals again.
    Observed hints replace the retrieval estimate as soon as a hint fires.
    Opportunity and the separate whole-workload comparison are not additive.
    """
    _measure_facade = _compat.facade()
    estimates = {
        key: (0.0 if key in ("verbosity_steer", "verbosity_output_estimated", "resume_lean_estimated", "checkpoint_restore_estimated") else float((((summary.get("structural_detail") or {}).get("estimated") if key == "structural_estimated" else summary.get(key)) or {}).get("cost_saved_usd", 0) or 0))
        for key in (
            "behavioral_estimate", "uncaptured_runtime",
            "contamination_exit", "handover_rerun", "resume_lean_estimated", "resume_lean_verified",
            "checkpoint_restore_estimated", "verbosity_steer", "verbosity_output_estimated", "structural_estimated",
        )
    }
    overhead = ((summary.get("session_effect") or {}).get("overhead") or {})
    estimates["expand_overhead"] = -max(0.0, float(overhead.get("expand_usd") or 0))
    hints = summary.get("hint_followed") or {}
    avoided_source = "hint_followed" if int(hints.get("events") or 0) > 0 else "retrieval_serve"
    estimates[avoided_source] = float(
        (summary.get(avoided_source) or {}).get("cost_saved_usd", 0) or 0)
    from . import savings_story
    logged = float(summary.get("total_cost_usd", 0) or 0) - savings_story.verbosity_logged(summary)
    routing_data = summary.get("model_routing") or {}
    routing = float(routing_data.get("realized_cost_usd", 0) or 0)
    routing_measured = routing if routing_data.get("evidence", "measured") == "measured" else 0.0
    reread_available = bool((reread or {}).get("available"))
    repeat_reads = float(reread.get("reread_usd", 0) or 0) if reread_available else 0.0
    premium = _measure_facade._counted_pricing_v5.class_premium_usd(reread)  # measured: the next call billed a cache write
    estimated = sum(estimates.values())
    net_total = logged + premium + repeat_reads + estimated
    return {
        "logged_usd": logged,
        "class_premium_usd": premium,
        "class_premium_events": int((reread or {}).get("oneshot_class_events", 0) or 0) if premium else 0,
        "routing_usd": routing,
        "routing_measured_usd": routing_measured,
        "routing_estimated_usd": routing - routing_measured,
        "reread_usd": repeat_reads,
        "reread_available": reread_available,
        "reread_prices_available": bool((reread or {}).get("reread_prices_available", reread_available)),
        "reread_pricing_complete": bool((reread or {}).get("reread_pricing_complete", reread_available)),
        "reread_unpriced_tokens": int((reread or {}).get("reread_unpriced_tokens", 0) or 0),
        "reread_pricing_note": (reread or {}).get("reread_pricing_note", ""),
        "estimates_usd": estimates,
        "estimated_usd": estimated,
        "avoided_search_source": avoided_source,
        "net_total_usd": net_total,
        "total_usd": max(0.0, net_total),
        "total_clamped": net_total < 0,
        "display": _measure_facade._action_savings_display(
            logged, 0.0, 0.0, repeat_reads, estimates,
            total_clamped=net_total < 0, class_premium=premium),
    }


def _get_merged_savings(days=None, since=None):
    """Merge savings_events and compression_events into one unified savings view.

    When *since* is a non-empty ISO-8601 timestamp string, it is forwarded to
    the underlying savings/compression queries as the cutoff (``timestamp >=
    since``) instead of the rolling ``now - days`` cutoff. This lets callers
    align the lookback window to an external boundary (e.g. the subscription
    limit reset). When *since* is None / empty, the rolling *days* cutoff is
    used as before.

    Dedup rule: legacy categories stay authoritative in savings_events.
    v5 categories (delta_read, quality_nudge, loop_prevention, bash_compress_*)
    come from compression_events. If a key somehow appears in both, savings_events
    wins — compression_events rows are skipped for that key.
    """
    _measure_facade = _compat.facade()
    savings = _measure_facade._get_savings_summary(days=days, since=since)
    compression = _measure_facade._get_compression_summary(days=days, since=since)

    by_category = dict(savings.get("by_category", {}))
    total_tokens = int(savings.get("total_tokens", 0) or 0)
    total_cost = float(savings.get("total_cost_usd", 0.0) or 0.0)
    total_events = int(savings.get("total_events", 0) or 0)

    # The compression summary prices each row using its model or session mix.
    fallback_cost_per_mtok = _measure_facade._estimate_compression_cost_per_mtok()

    _add_tok, _add_cost, _add_events = _measure_facade._merge_v5_compression(
        by_category, compression.get("by_feature", {}), fallback_cost_per_mtok)
    total_tokens += _add_tok
    total_cost += _add_cost
    total_events += _add_events

    # Structural (cumulative) savings — baseline delta × context loads since.
    structural = _measure_facade._compute_structural_savings(days=days)
    struct_tokens = int(structural.get("tokens_saved", 0) or 0)
    struct_cost = float(structural.get("cost_saved_usd", 0.0) or 0.0)
    struct_events = int(structural.get("events", 0) or 0)
    if struct_tokens > 0 or structural.get("baseline_source"):
        by_category["structural_savings"] = {
            "events": struct_events,
            "tokens_saved": struct_tokens,
            "cost_saved_usd": struct_cost,
            "baseline_source": structural.get("baseline_source"),
            "baseline_date": structural.get("baseline_date"),
            "overhead_delta": structural.get("overhead_delta", 0),
        }
        total_tokens += struct_tokens
        total_cost += struct_cost
        total_events += struct_events

    # Pricing detail — tells the consumer which model rate was applied.
    active_model = _measure_facade._resolve_session_model()
    pricing_tier = _measure_facade._load_pricing_tier()
    tier_data = _measure_facade.PRICING_TIERS.get(pricing_tier, _measure_facade.PRICING_TIERS["anthropic"])
    rate = tier_data.get("claude_models", {}).get(active_model, {}).get("input", 3.0)

    # Opportunity + estimated tiers. Kept OUT of total_cost/total_tokens: never
    # sum across measured / estimated / opportunity tiers. The dashboard and CLI
    # render these as separate, clearly-labelled lines.
    potential = _measure_facade._compute_structural_potential(days=days)
    uncaptured = _measure_facade._estimate_uncaptured_runtime(days=days, compression=compression, savings=savings)
    behavioral = _measure_facade._estimate_behavioral_savings(days=days)
    contamination_exit = _measure_facade._estimate_contamination_exit_savings(days=days)
    handover_rerun = _measure_facade._estimate_handover_rerun_savings(days=days)
    retrieval_serve = _measure_facade._estimate_retrieval_serve_savings(days=days)
    model_routing = _measure_facade._compute_model_routing_savings(days=days, since=since)
    output_waste = _measure_facade._estimate_output_waste(days=days)
    cache_drop = {**_measure_facade._estimate_cache_drop_savings(days=days), **__import__("large_transcripts").count(_measure_facade.CLAUDE_DIR, days)}
    before_after = _measure_facade._estimate_before_after_savings(days=days, since=since, estimated_pools={
        "uncaptured_runtime": uncaptured,
        "behavioral_loops": behavioral,
        "hint_followed": savings.get("hint_followed_estimated") or {},
        "handover_rerun": handover_rerun,
        "retrieval_serve": retrieval_serve,
        "resume_lean": savings.get("resume_lean_estimated") or {},
        "checkpoint_restore": savings.get("checkpoint_restore_estimated") or {},
    }, model_routing=model_routing)
    stale_reads_reclaimable = _measure_facade._estimate_stale_reads_reclaimable(days=days)
    try:
        import lean_resume_carry
        verified_lean = lean_resume_carry.verified_summary(days=days, since=since)
    except Exception:
        verified_lean = None

    return {
        "resume_lean_verified": verified_lean,
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 4),
        "total_events": total_events,
        "compression_unpriced_tokens": sum(int(v.get("unpriced_tokens", 0) or 0) for v in by_category.values()),
        "oneshot_pricing": _measure_facade._logged_oneshot_pricing(by_category),
        "compression_unpriced_credit_tokens": sum(int(v.get("unpriced_credit_tokens", 0) or 0) for v in by_category.values()),
        "compression_unpriced_debit_tokens": sum(int(v.get("unpriced_debit_tokens", 0) or 0) for v in by_category.values()),
        "by_category": by_category,
        "daily_avg_usd": round(total_cost / max(days, 1), 4),
        "period_days": days,
        "pricing_detail": {
            "model": active_model,
            "tier": pricing_tier,
            "rate_usd_per_mtok": float(rate),
        },
        # Model-mix repair (see _session_model_reprice). True when event-time
        # model stamps were corrected from each event's OWN session mix, which
        # makes total_cost_usd a rate allocation rather than a pure event-time
        # meter -- consumers must then label it "estimated", never "measured".
        # STRUCTURAL IS EXCLUDED BY CONSTRUCTION: struct_cost is added above from
        # _compute_structural_savings, which prices prefix tokens at CACHE rates
        # via _compound_structural (cache_write + cache_read x residual turns).
        # The repair only touches savings_events rows, so structural tokens are
        # never dragged onto a fresh-input rate. The routing pool is likewise
        # untouched and stays disjoint.
        "repriced_to_session_mix": bool(savings.get("repriced_to_session_mix")),
        "reprice_detail": savings.get("reprice_detail"),
        "structural_detail": structural,
        "structural_potential": potential,
        "uncaptured_runtime": uncaptured,
        "behavioral_estimate": behavioral,
        "model_routing": model_routing,
        "output_waste": output_waste,
        "cache_drop": cache_drop,
        # B4 flagship: contamination-exit avoided-rework (estimated tier, cohort-
        # grounded, sample size + confidence shown). Never summed into realized.
        "contamination_exit": contamination_exit,
        # B7: handover/continuity avoided-rework (estimated tier, same method).
        "handover_rerun": handover_rerun,
        # U-D: avoided exploratory search when a retrieval/continuity serve points
        # the model straight at the answer (estimated tier, cohort-grounded, capped).
        # Never summed into the realized counted total.
        "retrieval_serve": retrieval_serve,
        # THE headline before/after transformation: current activity priced at the
        # user's pre-TO per-session weight + mix vs actual. Comprehensive (volume +
        # structural + routing + sub-agents). Estimated tier, counterfactual, grounded
        # in the user's own frozen baseline window vs current sessions. Hidden until the
        # baseline window has elapsed with enough sessions. Never summed into the metered total.
        "before_after": before_after,
        # Opportunity tier: reclaimable stale-read waste. Intentionally NOT in
        # total_tokens/total_cost above — billed waste the user could reclaim,
        # never realized savings.
        "stale_reads_reclaimable": stale_reads_reclaimable,
        # Informational counts for tool-archive progressive disclosure. The net
        # $ is already inside realized tool_archive (re-pops netted); not summed.
        "progressive_disclosure": _measure_facade._progressive_disclosure_summary(days=days),
        # A1: one-time first-trim, surfaced separately, never in the realized total.
        "one_time_setup": savings.get("one_time_setup"),
        # U-G: deterministically-triggered avoided-search from proactive hints
        # (observed hint->read), estimated magnitude. Estimated tier, never realized.
        "hint_followed": savings.get("hint_followed_estimated"),
        # Verbosity-steer: estimated output-token reduction from conciseness
        # nudges. Estimated tier, never in the realized total. Surfaced for
        # the dashboard's estimated-tier display.
        "verbosity_steer": savings.get("verbosity_steer_estimated"),
        # Cold-resume avoided. Counterfactual trigger, measured
        # magnitude (target's real last-turn context at cache-read / 1h-write
        # rates). Estimated tier, never in the realized total.
        "resume_lean_estimated": savings.get("resume_lean_estimated"),
        # Checkpoint-restore avoided re-reads. The restore is observed but the
        # credited working set is inferred -- counterfactual, estimated tier,
        # never in the realized total.
        "checkpoint_restore_estimated": savings.get("checkpoint_restore_estimated"),
        "verbosity_output_estimated": savings.get("verbosity_output_estimated"),
    }


def _install_date():
    """Best-known install / first-capture date (ISO YYYY-MM-DD), or None.

    Prefers the pristine baseline snapshot timestamp (captured at first run);
    falls back to the earliest session_log date. Never raises.
    """
    _measure_facade = _compat.facade()
    try:
        snap = _measure_facade.SNAPSHOT_DIR / "snapshot_before.json"
        if snap.exists():
            ts = _measure_facade.json.loads(snap.read_text(encoding="utf-8")).get("timestamp")
            if ts:
                return ts[:10]
    except (_measure_facade.json.JSONDecodeError, OSError, ValueError):
        pass
    try:
        if _measure_facade.TRENDS_DB.exists():
            conn = _measure_facade._init_trends_db()
            _measure_facade.ensure_activity_view(conn)
            try:
                row = conn.execute("SELECT MIN(date) FROM session_activity").fetchone()
            finally:
                conn.close()
            if row and row[0]:
                return str(row[0])[:10]
    except (_measure_facade.sqlite3.Error, OSError):
        pass
    return None


def _savings_since_install():
    """Cumulative savings since install (NOT monthly-ized): the running total.

    Computes the full merged savings over the whole window since install so the
    measured event tiers sum from day one and the estimate tiers reflect the
    full history. Returns a compact summary for the dashboard's 'since install'
    card, split into measured vs estimated. Returns None when no install date is
    known. Never raises.
    """
    _measure_facade = _compat.facade()
    try:
        d = _measure_facade._install_date()
        if not d:
            return None
        days = (_measure_facade.datetime.now() - _measure_facade.datetime.fromisoformat(d)).days + 1
        days = max(1, min(days, 3650))
        full = _measure_facade._get_merged_savings(days=days)
        try:
            from . import session_effect
            full["session_effect"] = session_effect.summary(days)
        except Exception:
            full["session_effect"] = {}
        # Measured = logged runtime + realized structural. Model routing is an
        # assumption and stays out of every savings total.
        measured = float(full.get("total_cost_usd", 0) or 0) - savings_story.verbosity_logged(full)
        full_routing = full.get("model_routing") or {}
        # Since-install routing for every user: a stated pre-TO share
        # (pretool_baseline.json / TOKEN_OPTIMIZER_BASELINE_OPUS_SHARE) wins,
        # else the model mix measured at install (snapshot or earliest
        # history). Same pricing either way; no baseline, a non-Anthropic
        # runtime, or no Opus in the baseline means $0, never invented Opus.
        routing_source = savings_story.routing_baseline_kind(full_routing, _measure_facade.detect_runtime())
        stated = routing_source is not None
        stated_routing = max(0.0, float(full_routing.get("realized_cost_usd") or 0)) if stated else 0.0
        # Estimated = genuine TO-realized counterfactuals only — the same
        # categories the dashboard's estimated tier (estItems) sums, so the two
        # cards agree about what "estimated" contains. cache_drop and
        # output_waste are OPPORTUNITY (avoidable waste TO does not yet prevent),
        # so they are excluded here, along with the unsupported MCP-cap estimate.
        estimated = sum(
            float((full.get(k) or {}).get("cost_saved_usd", 0) or 0)
            for k in (
                "behavioral_estimate", "uncaptured_runtime",
                "contamination_exit", "handover_rerun", "resume_lean_verified",
            )
        )
        # Avoided-search: prefer the hint->read estimate (hint_followed; the
        # trigger is observed, the avoided-search magnitude is not metered);
        # fall back to the cohort estimate (retrieval_serve)
        # only before any hint has fired, so the two are never summed — the
        # exact dedup rule the dashboard applies (dashboard.html renderSavings).
        hf = full.get("hint_followed") or {}
        rs = full.get("retrieval_serve") or {}
        avoided = hf if int(hf.get("events") or 0) > 0 else rs
        estimated += float(avoided.get("cost_saved_usd", 0) or 0) + float(((full.get("structural_detail") or {}).get("estimated") or {}).get("cost_saved_usd") or 0) + stated_routing - max(0.0, float(((full.get("session_effect") or {}).get("overhead") or {}).get("expand_usd") or 0))
        # The all-in since install uses the monthly headline's own recipe
        # (logged + repeat reads + estimates); routing stays out of both.
        all_in = None
        try:
            counted = _measure_facade._counted_cumulative_summary(days=days)
            _measure_facade._apply_counted_oneshot_pricing(full, counted)
            components = _measure_facade._action_savings_components(full, counted)
            display_parts = dict(components["display"]["components_usd"], routing_estimated=round(stated_routing, 2))
            majority_period = None
            if stated_routing > 0:
                try:
                    from . import routing_period
                    rconn = _measure_facade._open_trends_db_readonly(timeout=1.0)
                    share = float(full_routing.get("baseline_opus_share") or 0)
                    try: majority_period = routing_period.strongest(rconn, _measure_facade, stated_routing, d, shares={"opus": share, "sonnet": round(1.0 - share, 4)})
                    finally: rconn.close()
                except Exception:
                    pass
            all_in = {
                "total_usd": round(components["display"]["total_usd"] + stated_routing, 2),
                "components_usd": display_parts, "routing_majority_period": majority_period,
                "estimated_usd": round(sum(components["display"]["estimates_usd"].values()), 2),
                "routing_usd": round(float(components.get("routing_usd", 0) or 0), 2),
                "routing_baseline_opus_share": full_routing.get("baseline_opus_share"), "routing_baseline_source": routing_source,
                "routing_baseline_basis": full_routing.get("baseline_source"),
                "routing_measured_baseline_opus_share": full_routing.get("measured_baseline_opus_share"), "routing_measured_baseline_realized_cost_usd": full_routing.get("measured_baseline_realized_cost_usd"),
                "routing_pending": full_routing.get("evidence") == "pending",
                "routing_pending_sessions": int(full_routing.get("pending_sessions") or 0),
                "routing_unsplit_tokens": int(full_routing.get("unsplit_tokens") or 0),
                "remeasuring": bool(counted.get("remeasuring")),
                "remeasuring_sessions": int(counted.get("remeasuring_sessions") or 0),
            }
        except Exception:
            all_in = None
        return {
            "install_date": d,
            "days": days,
            "excluded_usd": dict(savings_story.excluded_since_install(full), routing=None) if stated else savings_story.excluded_since_install(full),
            "measured_usd": round(measured, 2),
            "estimated_usd": round(estimated, 2),
            "total_usd": round(measured + estimated, 2),
            "all_in": all_in,
            # measured_usd is no longer a pure event-time meter once the
            # model-mix repair has moved a rate (see _session_model_reprice), so
            # the card that renders it must stop calling itself "Measured".
            "repriced_to_session_mix": bool(full.get("repriced_to_session_mix")),
        }
    except Exception:
        return None


def _live_savings_payload(days=None):
    """Recompute the dashboard's savings + cache-health blocks live.

    Single source shared by the /api/savings serve endpoint and its tests, so a
    browser refresh against the daemon shows current numbers without a full
    dashboard regen. Mirrors the savings/cache_health wiring in
    generate_standalone_dashboard exactly, so the live-patched tiles match what a
    full regen would render. Fail-open: every sub-block is guarded so a slow or
    failed eval degrades to a safe empty shape instead of raising.
    """
    _measure_facade = _compat.facade()
    savings_data = _measure_facade._dashboard_savings_data(
        days=days, include_billing_mode=True, fail_open=True)
    # Mirror generate_standalone_dashboard: only stamp billing_mode when the
    # savings source produced a real payload. On fail-open the source degrades
    # to the bare empty shape, and re-adding billing_mode here would break the
    # safe-empty contract (the live-patched tile must equal a regen's fail-open).
    if savings_data != {"total_tokens": 0, "total_cost_usd": 0.0, "by_category": {}}:
        try:
            savings_data["billing_mode"] = _measure_facade.keepwarm_billing_mode()
        except Exception:
            pass
    try:
        cache_health = _measure_facade._cache_ttl_waste_cached(days=days)
    except Exception:
        cache_health = {"available": False, "tier": "opportunity"}
    try:
        if isinstance(cache_health, dict):
            cache_health["keepwarm"] = _measure_facade.keepwarm_cache_health_block(days=days)
    except Exception:
        pass
    # Window/runway card (5h/7d): include it in the LIVE payload so an open dashboard
    # picks up a fresh rate-limit reading the instant the statusline writes one (the
    # figure is only ever fresh while Claude is actively running). Same builder the
    # full regen uses; fail-open to None so a slow/failed read never breaks /api/savings.
    try:
        runway = _measure_facade.runway_snapshot(days=days)
    except Exception as _rw_err:
        runway = None
        # Trace the fail-open: without this the window card would silently stop
        # live-refreshing with no diagnosable cause (the class of silent failure
        # this batch is otherwise closing).
        try:
            print(f"[Token Optimizer] /api/savings: runway snapshot failed "
                  f"({type(_rw_err).__name__}); window card not refreshed this poll.",
                  file=_measure_facade.sys.stderr)
        except Exception:
            pass
    return {
        "ok": True,
        "savings": savings_data,
        "cache_health": cache_health,
        "runway": runway,
        "generated_at": _measure_facade.datetime.now().isoformat(),
    }


def _headline_tripwire(measured_usd, events, days):
    """Flag a measured-savings collapse that activity does not explain.

    Keeps one reading per day. If the measured total fell below half of the
    most recent earlier reading while the number of savings events stayed at
    70%+ of it, the likely cause is a calculation change, not less saving, and
    a message is returned so the report says so instead of quietly shrinking.
    Returns None otherwise. Never raises."""
    _measure_facade = _compat.facade()
    try:
        path = _measure_facade.SNAPSHOT_DIR / _measure_facade._HEADLINE_HISTORY_FILE
        hist = _measure_facade.json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(hist, dict):
            hist = {}
        key = f"d{int(days)}"
        rows = [r for r in hist.get(key, []) if isinstance(r, dict)]
        today = _measure_facade.datetime.now().strftime("%Y-%m-%d")
        prior = next((r for r in reversed(rows) if r.get("date") != today), None)
        rows = [r for r in rows if r.get("date") != today]
        rows.append({"date": today, "usd": round(float(measured_usd), 2), "events": int(events)})
        hist[key] = rows[-14:]
        _measure_facade._write_baseline_state(path, hist)
        if (prior and float(prior.get("usd") or 0) >= 20
                and measured_usd < 0.5 * float(prior["usd"])
                and events >= 0.7 * int(prior.get("events") or 0)):
            return (f"Measured savings fell from ${float(prior['usd']):,.0f} ({prior['date']}) to "
                    f"${measured_usd:,.0f} while activity held steady. That usually means a "
                    f"calculation change, not less saving. Please report it.")
    except Exception:
        pass
    return None


def _reread_savings_for_window(days):
    """Re-read savings (removed context not re-read on later turns) for the last
    `days`; {} when the counted ledger is unavailable. Never raises."""
    _measure_facade = _compat.facade()
    try:
        if not _measure_facade.TRENDS_DB.exists():
            return {}
        conn = _measure_facade._init_trends_db()
        try:
            end = _measure_facade.datetime.now(_measure_facade.timezone.utc).replace(tzinfo=None)
            out = _measure_facade._counted_window_summary(conn, end - _measure_facade.timedelta(days=days), end)
        finally:
            conn.close()
        return out if out.get("available") else {}
    except Exception:
        return {}


def savings_report(days=None, as_json=None):
    """Display savings from logged actions and the counted repeat-read cohort."""
    _measure_facade = _compat.facade()
    summary = _measure_facade._get_merged_savings(days=days)
    # Use the same stored removal cohort as the dashboard. Its counted benefit
    # survives transcript rotation and never needs another transcript walk.
    try:
        summary["counted_period"] = _measure_facade._counted_cumulative_summary(days=days)
    except Exception:
        summary["counted_period"] = {"available": False}
    summary["reread_avoided"] = summary["counted_period"]
    _compat.facade()._headline.attach(summary, days)
    _measure_facade._apply_counted_oneshot_pricing(summary, summary["counted_period"])
    summary["action_savings"] = _measure_facade._action_savings_components(
        summary, summary["reread_avoided"])

    if as_json:
        print(_measure_facade.json.dumps(summary, indent=2))
        return

    now = _measure_facade.datetime.now()
    start = (now - _measure_facade.timedelta(days=days)).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")

    # Young-install guard: with under 30 tracked days, a "/mo" projection would
    # present a partial sample as a monthly run-rate. Print raw cumulative sums
    # ("so far") instead. Clock skew / future install dates clamp to 1 day
    # (cumulative mode — fails safe, never inflates); no install date falls back
    # to run-rate (totals are ~0, nothing misleading renders).
    inst = _measure_facade._install_date()
    tracked = days
    if inst:
        try:
            tracked = min(days, max(1, (now - _measure_facade.datetime.fromisoformat(inst)).days + 1))
        except ValueError:
            pass
    run_rate = tracked >= 30
    per = "/mo" if run_rate else " so far"

    def _mo(c):
        _measure_facade = _compat.facade()
        return c / max(days, 1) * 30 if run_rate else c

    # Every dollar line below is a whole-cent share of the same total, so the
    # printed parts add up to the printed ALL IN.
    _act = summary["action_savings"]
    _shown = _measure_facade._action_savings_display(
        _mo(_act["logged_usd"]), 0.0, 0.0, _mo(_act["reread_usd"]),
        {k: _mo(v) for k, v in _act["estimates_usd"].items()},
        total_clamped=_act["total_clamped"], class_premium=_mo(_act.get("class_premium_usd", 0.0)))
    _cents = _shown["components_cents"]
    _est_cents = _shown["estimates_cents"]
    _money = _measure_facade._format_cents

    print("\n  Token Optimizer Savings Report")
    print(f"  {'=' * 58}")
    print(f"  Period: Last {days} days ({start} to {end})")
    print(_compat.facade()._headline.cli_headline(summary["token_headline"], days, summary.get("session_effect")))

    # Headline transformation. With the session-weight pool live it is the
    # work-normalized (per-API-call, constant-price) figure; otherwise the legacy
    # frozen-anchor per-session comparison.
    ba = summary.get("before_after") or {}
    if float(ba.get("monthly_savings_usd", 0.0) or 0.0) > 0:
        wp = ba.get("session_weight_pool") or None
        print()
        print(f"  YOUR TRANSFORMATION: ~${ba['monthly_savings_usd']:,.0f} over {days} days")
        # Spend cap disclosure: when the counterfactual exceeds actual spend,
        # the headline is capped and the user must see why, not wonder why the
        # number stopped moving.
        if ba.get("savings_capped"):
            note = ba.get("savings_cap_note") or ""
            if note:
                for line in _measure_facade.textwrap.wrap(note, width=72):
                    print(f"    {line}")
        # A neutral add-back reprice (routing pending/unavailable) shrinks the
        # figure without a visible reason -- disclose it, same as the card.
        _rb = ba.get("compression_reprice_basis")
        if _rb == "routing_pending":
            print("    Note: model-mix repricing is paused while sessions finish "
                  "re-measuring, so the add-backs show their measured value.")
        elif _rb == "routing_unavailable":
            print("    Note: model-mix repricing could not be measured, so the "
                  "add-backs show their measured value.")
        if wp:
            unit_lbl = "API call" if wp.get("work_unit") == "api_call" else "message"
            print(f"    Each {unit_lbl} now carries less billed context than in "
                  f"{wp.get('anchor_month')} (your earliest complete month): "
                  f"${wp.get('after_cost_per_unit', 0):.4f} vs ${wp.get('before_cost_per_unit', 0):.4f} "
                  f"at constant token prices, x {int(wp.get('now_units', 0)):,} {unit_lbl}s "
                  f"this period "
                  f"(est. ${ba.get('actual_monthly_usd', 0):,.0f} now vs ${ba.get('counterfactual_monthly_usd', 0):,.0f} "
                  f"the old way) [estimated]")
            drift = float(wp.get("price_mix_drift_usd", 0.0) or 0.0)
            if abs(drift) >= 1.0:
                print(f"    Not claimed: ~${abs(drift):,.0f} this period of model price/mix drift "
                      f"({'cheaper' if drift >= 0 else 'costlier'} models would have moved "
                      f"your bill without Token Optimizer).")
            cross = wp.get("crosscheck") or {}
            if cross.get("transformation_usd") is not None:
                cross_usd = float(cross.get("transformation_usd") or 0.0)
                cross_unit = "API calls" if cross.get("unit") == "api_call" else "messages"
                gap = cross.get("gap_pct")
                gap_txt = (f", {float(gap):.1f}% apart"
                           if isinstance(gap, (int, float)) else "")
                if _measure_facade._crosscheck_verdict(wp):
                    print(f"    Cross-check on {cross_unit}: "
                          f"{_measure_facade._signed_usd(cross_usd)} this period "
                          f"(independent unit, agrees within ~10%{gap_txt}).")
                else:
                    print(f"    Cross-check on {cross_unit}: "
                          f"{_measure_facade._signed_usd(cross_usd)} this period "
                          f"(independent unit, does not agree{gap_txt}).")
        else:
            print(f"    Had you worked the way you did before Token Optimizer "
                  f"(~{ba.get('before_opus', 0) * 100:.0f}% Opus main work), you'd have paid "
                  f"~${ba['monthly_savings_usd']:,.0f} more "
                  f"(est. ${ba.get('actual_monthly_usd', 0):,.0f} now vs ${ba.get('counterfactual_monthly_usd', 0):,.0f} "
                  f"the old way) [estimated]")
        # Where it comes from: the waterfall attribution (reconciles to the headline).
        ba_breakdown = ba.get("breakdown") or []
        if ba_breakdown:
            print("    Where it comes from:")
            for lev in ba_breakdown:
                amt = float(lev.get("monthly_usd", 0) or 0)
                sign = "-" if amt < 0 else " "
                print(f"      {sign}${abs(amt):>8,.0f} this period  {lev.get('label', lev.get('key', ''))}")
            caveat = ba.get("breakdown_caveat") or ""
            if caveat:
                # Wrap the caveat so the CLI surfaces the same disclosure as the dashboard.
                for line in _measure_facade.textwrap.wrap(caveat, width=72):
                    print(f"      {line}")
        # Premium delegation disclosure is supplementary context. Under the legacy
        # path its signed effect is in the sidechain pool's net above; under the
        # session-weight pool delegated work is excluded from the headline entirely.
        prem = float(ba.get("premium_delegation_cost_usd", 0.0) or 0.0)
        if prem > 0:
            print(f"    Costlier delegation: ${prem:,.0f} this period of premium-model "
                  f"subagent spend. "
                  + ("Not in the headline above (delegated work is measured separately)."
                     if wp else "Included in the sidechain net above."))
    elif ba.get("reason") == "net_negative":
        # Distinguishable empty state: the math RAN and landed at/below baseline.
        # Silence here is what made "no savings" indistinguishable from "broke".
        print()
        print("  YOUR TRANSFORMATION: none to headline this period")
        print(f"    Each request now carries more context than at your baseline "
              f"(est. ${ba.get('actual_monthly_usd', 0):,.0f} now vs "
              f"${ba.get('counterfactual_monthly_usd', 0):,.0f} this period at the baseline's cost "
              f"per request), so there is no whole-workload saving to _compat.facade()._headline. "
              f"The measured savings below are real and counted.")
    elif ba.get("reason") == "flat":
        print()
        print("  YOUR TRANSFORMATION: no measurable change this period")
        print(f"    Cost per unit of work is about the same as before install "
              f"(est. ${ba.get('actual_monthly_usd', 0):,.0f} now vs "
              f"${ba.get('counterfactual_monthly_usd', 0):,.0f} this period the old way). "
              f"The savings below are counted action by action.")
    elif ba.get("reason") == "methods_disagree":
        # The two independent work-unit methods could not be reconciled, so the
        # transformation is not headlined. Both figures stay visible.
        wp = ba.get("session_weight_pool") or {}
        cross = wp.get("crosscheck") or {}
        main_usd = float(wp.get("transformation_usd", 0.0) or 0.0)
        cross_usd = float(cross.get("transformation_usd", 0.0) or 0.0)
        main_unit = ("per-API-call" if wp.get("work_unit") == "api_call"
                     else "per-message")
        cross_unit = ("per-API-call" if cross.get("unit") == "api_call"
                      else "per-message")
        print()
        print("  YOUR TRANSFORMATION: not headlined this period")
        print(f"    The {main_unit} and {cross_unit} methods disagree "
              f"({_measure_facade._signed_usd(main_usd)} vs {_measure_facade._signed_usd(cross_usd)} this period), "
              f"so neither is headlined. The measured savings below are real "
              f"and counted.")

    print("  Pricing: known model rates, API equivalent")

    struct = summary.get("structural_detail") or {}
    if struct.get("baseline_source") == "trim_ledger":
        print(f"  Setup trims: {len(struct.get('trims') or [])} recorded since "
              f"{(struct.get('baseline_date') or '')[:10]} ({struct.get('overhead_delta', 0):,} tokens per call still trimmed)")
    else:
        print(f"  Setup trims: {struct.get('message') or 'no trims recorded yet'}")

    print()
    print(f"  {'Category':<28s} {'Events':>8s} {'Tokens Saved':>14s} {'Cost Saved':>11s}")
    print(f"  {'-' * 25}  {'-' * 8}  {'-' * 14}  {'-' * 11}")

    by_cat = summary.get("by_category", {})

    # Relocated keys are not realized: setup_optimization is a one-time line,
    # mcp_cap is an estimate, verbosity_steer is a deterministic-trigger /
    # estimated-magnitude event, resume_lean and checkpoint_restore are
    # counterfactual credits (popped from by_category in _get_savings_summary
    # to avoid double-counting with the transformation card's estimated pools).
    # They are reported in their own tiers below, never in this
    # measured table (that was the A1/A3 double-count). Without verbosity_steer
    # here, the row shows "0 events" in the measured table even though the
    # feature fires thousands of times/month — the raw nudge injection count is
    # an estimated-tier metric. verbosity_steer_measured is an uncontrolled
    # before/after; its net-of-control figure prints in the estimated tier.
    _RELOCATED = {"setup_optimization", "mcp_cap", "verbosity_steer", "resume_lean",
                  "checkpoint_restore", "verbosity_steer_measured"}

    # Known measured categories (even if zero), then any unknown ones in the data.
    table_rows = [(label, by_cat.get(key, {}))
                  for key, label in _measure_facade._SAVINGS_CATEGORY_LABELS.items() if key not in _RELOCATED]
    table_rows += [(key, cat_data) for key, cat_data in by_cat.items()
                   if key not in _measure_facade._SAVINGS_CATEGORY_LABELS]
    # When the table covers the same span as the monthly line, its total is the
    # logged share of ALL IN, so both lines print the same cents.
    table_total = summary.get("total_cost_usd", 0.0)
    if _mo(1.0) == 1.0 and not summary["action_savings"]["total_clamped"]:
        table_total = _cents["logged"] / 100
    row_cents, table_total_cents = _measure_facade._allocate_cents(
        [cat_data.get("cost_saved_usd", 0.0) for _label, cat_data in table_rows],
        total=table_total)
    for (label, cat_data), cents in zip(table_rows, row_cents):
        events = cat_data.get("events", 0)
        tokens = cat_data.get("tokens_saved", 0)
        print(f"  {label:<28s} {events:>8,} {tokens:>14,} {'$' + _money(cents):>11s}")

    print(f"  {'-' * 25}  {'-' * 8}  {'-' * 14}  {'-' * 11}")

    total_events = summary.get("total_events", 0)
    total_tokens = summary.get("total_tokens", 0)
    total_cost = summary.get("total_cost_usd", 0.0)
    # Removed context stays out on every later turn until compaction. Those
    # avoided re-reads are the "modeled" tier, the same one the dashboard's
    # Savings tab shows as repeat reads avoided.
    rr = summary.get("reread_avoided") or {}
    rr_usd = summary["action_savings"]["reread_usd"]
    daily_avg = summary.get("daily_avg_usd", 0.0)

    print(f"  {'TOTAL (measured)':<28s} {total_events:>8,} {total_tokens:>14,} {'$' + _money(table_total_cents):>11s}")
    unpriced = int(summary.get("compression_unpriced_tokens", 0) or 0)
    if unpriced:
        unpriced_credit = int(summary.get("compression_unpriced_credit_tokens", 0) or 0)
        unpriced_debit = int(summary.get("compression_unpriced_debit_tokens", 0) or 0)
        split = (f" ({unpriced_credit:,} credits, {unpriced_debit:,} debits)"
                 if unpriced_credit or unpriced_debit else "")
        print(f"  {unpriced:,} compression tokens are counted but have no known model rate{split};")
        print("  their dollar value is not included.")
    unpriced_logged = int((summary.get("reprice_detail") or {}).get("unpriced_tokens", 0) or 0)
    if unpriced_logged:
        print(f"  {unpriced_logged:,} logged tokens ran on models with no known rate; "
              "their dollar value is not included.")
    oneshot = summary.get("oneshot_pricing") or {}
    if oneshot.get("removal_events"):
        print("  Initial removals above are priced at the input rate, a minimum.")
        if _cents.get("class_premium"):
            print(f"  The next call billed new context at a cache-write rate for "
                  f"{int(oneshot.get('class_events', 0)):,} removals; that premium is its own line below.")
    trip = _measure_facade._headline_tripwire(total_cost + rr_usd, total_events, days)
    if trip:
        print()
        for line in _measure_facade.textwrap.wrap("CHECK: " + trip, width=72):
            print(f"  {line}")
    print()
    print(f"  Daily average: ${daily_avg:.2f} saved (measured)")
    if run_rate:
        print(f"  Estimated monthly: ${_money(_cents['logged'])} (measured)")
    else:
        print(f"  Total so far ({tracked} day{'' if tracked == 1 else 's'} tracked): ${_money(_cents['logged'])} (measured)")
    if _cents.get("class_premium"):
        print(f"  + write-class premium: ~${_money(_cents['class_premium'])}{per} ({_act.get('class_premium_events', 0):,} "
              "removals whose next call wrote to the cache, priced at that write rate) [measured]")

    # Model routing is an assumption, shown on its own line and kept out of ALL IN.
    routing = summary.get("model_routing") or {}
    routing_realized = float(routing.get("realized_cost_usd", 0.0) or 0.0)
    routing_pending = int(routing.get("pending_sessions", 0) or 0) if routing.get("evidence") == "pending" else 0
    if routing_pending:
        print(f"  i  model routing: re-measuring ({routing_pending:,} "
              f"session{'' if routing_pending == 1 else 's'} left), not in ALL IN")
    if routing_realized > 0:
        r_cents, _ = _measure_facade._allocate_cents([_mo(routing_realized)])
        r_monthly = _money(r_cents[0])
        b_share = routing.get("baseline_opus_share", 0.0) * 100
        c_share = routing.get("current_opus_share", 0.0) * 100
        routing_label = "estimated" if routing.get("evidence") == "estimated" else "measured"
        basis = "assuming" if routing.get("baseline_source") == "pretool_baseline" else "with measured baseline"
        print(f"  i  Model routing (not in ALL IN): ~${r_monthly}{per} "
              f"{basis} {b_share:.0f}% Opus before Token Optimizer (now {c_share:.0f}%) [{routing_label}]")
        mb_share = routing.get("measured_baseline_opus_share")
        if mb_share is not None:
            mb_cents, _ = _measure_facade._allocate_cents([_mo(float(routing.get("measured_baseline_realized_cost_usd") or 0.0))])
            print(f"  i    measured alternative: your earliest tracked month had {float(mb_share) * 100:.1f}% Opus -> ~${_money(mb_cents[0])}{per}")
        ref_priced = [str(f)[:1].upper() + str(f)[1:] for f in routing.get("baseline_reference_priced") or []]
        if ref_priced:
            print(f"  i  routing compares your earlier and current mix on one price list, the newest card of each "
                  f"family ({', '.join(ref_priced)}), because your earlier mix records no model generations, "
                  f"so this figure is an estimate")
        gen_delta = float(routing.get("generation_price_delta_usd", 0.0) or 0.0)
        if abs(_mo(gen_delta)) >= 0.01:
            print(f"  i  at the exact cards you ran, the same work cost ~${abs(_mo(gen_delta)):.2f}{per} "
                  f"{'more' if gen_delta > 0 else 'less'} than on that price list; "
                  f"that gap is model generation pricing, not routing")
    if float(routing.get("premium_share", 0.0) or 0.0) > 0:
        names = ", ".join(str(f)[:1].upper() + str(f)[1:] for f in routing.get("premium_models") or [])
        print(f"  i  premium models you chose ({names}, {float(routing['premium_share']) * 100:.0f}% of tokens, "
              f"~${_mo(float(routing.get('premium_cost_usd', 0.0) or 0.0)):.2f}{per} at API rates) "
              f"are left out of the routing comparison")
    if int(routing.get("unpriced_tokens", 0) or 0) > 0 or int(routing.get("free_tokens", 0) or 0) > 0:
        left_out = [str(x) for x in (routing.get("unpriced_models") or []) + (routing.get("free_models") or [])]
        shown = ", ".join(left_out[:3]) + (f" and {len(left_out) - 3} more" if len(left_out) > 3 else "")
        print(f"  i  models without a known price ({shown}; "
              f"{int(routing.get('unpriced_tokens', 0) or 0) + int(routing.get('free_tokens', 0) or 0):,} tokens) "
              f"are left out of the routing comparison, not counted as free")
    unsplit_routing = int(routing.get("unsplit_tokens", 0) or 0)
    if unsplit_routing > 0:
        # Usage without a token-class split (Cursor totals, model-daily-only
        # history, legacy rows) sits outside both routing arms; say so instead
        # of letting an empty or partial routing figure read as $0 coverage.
        print(f"  i  {unsplit_routing:,} tokens without a token-class split are "
              f"left out of model routing")
    all_in = _money(_shown["total_cents"])
    if rr_usd != 0:
        print(f"  + repeat reads avoided: ~${_money(_cents['reread'])}{per} "
              f"({int(rr.get('reread_tokens', 0) or 0):,} tokens of removed context not re-read "
              f"before the next compaction) [modeled]")
    elif not summary["action_savings"]["reread_available"]:
        print("  i  repeat reads avoided: unavailable (counted ledger unavailable)")
    if rr.get("reread_pricing_note"):
        print("  i  " + rr["reread_pricing_note"])

    # Estimated tier — uncaptured runtime (sub-agent compression not attributed).
    uncaptured = summary.get("uncaptured_runtime") or {}
    unc_cost = float(uncaptured.get("cost_saved_usd", 0.0) or 0.0)
    if unc_cost > 0:
        unc_monthly = _money(_est_cents["uncaptured_runtime"])
        print(f"  + est. uncaptured runtime: ~${unc_monthly}{per} "
              f"(sub-agent compression, {uncaptured.get('subagent_dispatches', 0):,} dispatches) [estimated]")

    # Estimated tier — behavioral coaching (loops prevented, etc.) that never bills.
    behavioral = summary.get("behavioral_estimate") or {}
    beh_cost = float(behavioral.get("cost_saved_usd", 0.0) or 0.0)
    if beh_cost > 0:
        beh_monthly = _money(_est_cents["behavioral_estimate"])
        print(f"  + est. behavioral (loops prevented): ~${beh_monthly}{per} "
              f"({behavioral.get('loop_events', 0)} loops caught, repeated ~{behavioral.get('prevented_iterations', 0)}x "
              f"before catch; avoided continuation estimated at one more span) [estimated]")

    # cache_drop + output_waste are OPPORTUNITY (avoidable waste TO does not yet
    # prevent), shown in the COULD SAVE block below, never in the estimated total.

    # Estimated tier — checkpoint restores (avoided working-set re-reads; the
    # restore is observed, the credited re-read is inferred, never metered).
    cr_est = summary.get("checkpoint_restore_estimated") or {}
    cre_cost = float(cr_est.get("cost_saved_usd", 0.0) or 0.0)
    if cre_cost > 0:
        cre_monthly = _money(_est_cents["checkpoint_restore_estimated"])
        print(f"  + est. checkpoint restores: ~${cre_monthly}{per} "
              f"({cr_est.get('events', 0)} restores, inferred working-set re-reads) [estimated]")

    # Estimated tier — FLAGSHIP: contamination-exit avoided rework (cohort).
    ce = summary.get("contamination_exit") or {}
    if float(ce.get("cost_saved_usd", 0.0) or 0.0) > 0:
        ce_monthly = _money(_est_cents["contamination_exit"])
        print(f"  + est. avoided rework (heeded nudges): ~${ce_monthly}{per} "
              f"({ce.get('heeded_sessions', 0)} heeded vs {ce.get('ignored_sessions', 0)} ignored, "
              f"~{ce.get('delta_tokens_per_session', 0):,} tok/session less rework, "
              f"confidence: {ce.get('confidence', '?')}) [estimated]")

    # Estimated tier — handover/continuity avoided rework (cohort).
    hr = summary.get("handover_rerun") or {}
    if float(hr.get("cost_saved_usd", 0.0) or 0.0) > 0:
        hr_monthly = _money(_est_cents["handover_rerun"])
        print(f"  + est. avoided rework (continuity handover): ~${hr_monthly}{per} "
              f"({hr.get('restored_sessions', 0)} restored vs {hr.get('baseline_sessions', 0)} baseline, "
              f"confidence: {hr.get('confidence', '?')}) [estimated]")

    avoided_source = summary["action_savings"]["avoided_search_source"]
    avoided = summary.get(avoided_source) or {}
    avoided_cost = summary["action_savings"]["estimates_usd"][avoided_source]
    if avoided_cost > 0:
        if avoided_source == "hint_followed":
            detail = f"{avoided.get('events', 0)} hints used"
            label = "hints"
        else:
            detail = f"{avoided.get('served_sessions', 0)} served sessions"
            label = "retrieval"
        print(f"  + est. avoided search ({label}): ~${_money(_est_cents[avoided_source])}{per} "
              f"({detail}) [estimated]")

    print(f"  = ALL IN: ~${all_in}{per} (measured + modeled + estimated)")
    if routing_pending:
        print("  i  ALL IN leaves out model routing; the assumption stays separate.")
    if summary["action_savings"].get("reread_pricing_complete") is False:
        print("  i  ALL IN is a priced subtotal; unpriced repeat-read credits and debits are not included.")
    if summary["action_savings"]["total_clamped"]:
        net_total = _mo(summary["action_savings"]["net_total_usd"])
        print(f"  i  signed adjustments give a net total of ~${net_total:.2f}{per}; "
              "the headline has a $0 floor")
    try:
        since = _measure_facade._savings_since_install()
    except Exception:
        since = None
    since_all_in = (since or {}).get("all_in") or {}
    if float(since_all_in.get("total_usd") or 0) > 0:
        so_far = " so far" if since_all_in.get("routing_pending") or since_all_in.get("remeasuring") else ""
        print(f"  = SINCE INSTALL ({since['install_date']}, {since['days']} days): "
              f"~${since_all_in['total_usd']:,.2f}{so_far} (same parts, added up)")

    # Informational (not summed): one-time first-trim + progressive disclosure.
    one_time = summary.get("one_time_setup") or {}
    if int(one_time.get("tokens_saved", 0) or 0) > 0:
        print(f"  i  first-trim (one-time): {one_time.get('tokens_saved', 0):,} tokens off your prefix "
              f"(counted as ongoing structural savings above, shown here once)")
    pd = summary.get("progressive_disclosure") or {}
    if int(pd.get("archived", 0) or 0) > 0:
        print(f"  i  progressive disclosure: {pd.get('archived', 0)} tool results archived, "
              f"{pd.get('reexpanded', 0)} re-expanded, "
              f"{pd.get('net_collapsed_tokens', 0):,} tokens stayed collapsed")

    # Opportunity tier — what you could save by acting (never realized, never summed).
    potential = summary.get("structural_potential") or {}
    pot_cost = float(potential.get("cost_saved_usd", 0.0) or 0.0)
    routing_potential = float(routing.get("potential_cost_usd", 0.0) or 0.0)
    reclaimable = summary.get("stale_reads_reclaimable") or {}
    recl_cost = float(reclaimable.get("cost_usd", 0.0) or 0.0)
    cache_drop = summary.get("cache_drop") or {}
    # cache-drop shows the re-written prefix in tokens (a provider-neutral fact)
    # and, as an opportunity outside every total, its dollar value at the rates
    # of the models that ran (cache write minus the cache read it would have been).
    cd_tokens = int(cache_drop.get("est_drops", 0) or 0) * int(cache_drop.get("prefix_tokens", 0) or 0)
    cd_cost = float(cache_drop.get("cost_saved_usd", 0.0) or 0.0)
    output_waste = summary.get("output_waste") or {}
    ow_cost = float(output_waste.get("cost_saved_usd", 0.0) or 0.0)
    if pot_cost > 0 or routing_potential > 0 or recl_cost > 0 or cd_tokens > 0 or ow_cost > 0:
        print()
        print(f"  {'-' * 58}")
        print("  COULD SAVE (opportunity — act to realize, not counted above):")
        # Round once so the printed items add up to the printed sum.
        concrete_items = [c for c in (pot_cost, ow_cost, recl_cost) if c > 0]
        item_cents, items_total_cents = _measure_facade._allocate_cents([_mo(c) for c in concrete_items])
        item_text = iter(_measure_facade._format_cents(c) for c in item_cents)
        if pot_cost > 0:
            print(f"    ~${next(item_text)}{per} structural: prune {potential.get('unused_skills', 0)} unused skills "
                  f"(~{potential.get('recoverable_tokens', 0):,} tokens), compounds every session.")
        if ow_cost > 0:
            print(f"    ~${next(item_text)}{per} output: {output_waste.get('avoidable_writes', 0)} of "
                  f"{output_waste.get('writes', 0)} full-file Writes could be targeted Edits.")
        if recl_cost > 0:
            print(f"    ~${next(item_text)}{per} reclaimable: {reclaimable.get('tokens', 0):,} tokens of stale "
                  f"re-reads across {reclaimable.get('sessions', 0)} sessions — avoid re-reading or use .contextignore.")
        if len(concrete_items) > 1:
            print(f"    = ~${_measure_facade._format_cents(items_total_cents)}{per} from the items above (API-rate equivalent)")
        if cd_tokens > 0:  # an opportunity at API rates: own line, never folded into the total above
            ttl = float(cache_drop.get("ttl_seconds") or 0)  # the TTL the user's own cache writes show
            cache_name, resume = ("the 5-minute cache", "5 minutes") if 0 < ttl < 3600 else ("the 1-hour cache" if ttl >= 3600 else "the cache window", "the hour")
            cd_dollars = f", about ${_mo(cd_cost):.2f}{per} at API rates" if cd_cost > 0 else ""
            print()
            print(f"    Cache drops (an opportunity, not a measurement): {cache_drop.get('drop_sessions', 0)} sessions idled "
                  f"past {cache_name} and re-wrote ~{int(_mo(cd_tokens)):,} tokens{per} of prefix{cd_dollars}. "
                  f"Resume within {resume}, or batch related work before stepping away.")
        if routing_potential > 0:
            # Routing is an assumption about the user's own Opus work, so it sits
            # on its own line and is never folded into the total above.
            frac_pct = round(float(routing.get("routable_fraction", 0.2)) * 100, 2)
            frac_text = f"{frac_pct:g}"
            target = routing.get("target_model") or "Sonnet"
            unsplit_left_out = int(_mo(int(routing.get("potential_unsplit_excluded_tokens", 0) or 0)))
            unsplit_note = (f" (~{unsplit_left_out:,} Opus tokens{per} without a class split are left out)"
                            if unsplit_left_out > 0 else "")
            print()
            print(f"    Routing (an assumption, not a measurement): if about {frac_text}% of your "
                  f"Opus work (grunt tasks) ran on {target}: new input and output only{unsplit_note}, "
                  f"about ${_mo(routing_potential):.2f}{per} at API rates.")
    print(f"  {'=' * 58}")
    print("  Tiers are separate: measured (billed events), estimated (your own")
    print("  cohorts), opportunity (not yet realized). They are never summed.")

    if total_events == 0:
        print()
        print("  No savings events recorded yet. Savings are tracked when you:")
        print("    - Run 'compare' after optimizing your setup")
        print("    - Restore from progressive checkpoints (Smart Compaction)")
        print("    - Archive unused tools or skills")


def validate_impact(strategy=None, days=None, as_json=None):
    """Compare session metrics before vs after an optimization event.

    Strategies:
        auto: Split at the most recent savings_event timestamp (or git tag)
        halves: Split session history chronologically in half

    Returns dict with: strategy_used, before_summary, after_summary, deltas, verdict
    """
    _measure_facade = _compat.facade()
    split_ts = None

    if strategy == "auto":
        # Find most recent savings event as the split point
        if _measure_facade.TRENDS_DB.exists():
            try:
                conn = _measure_facade.sqlite3.connect(str(_measure_facade.TRENDS_DB))
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                row = conn.execute(
                    "SELECT MAX(timestamp) as latest FROM savings_events "
                    "WHERE event_type NOT IN ('tool_archive', 'mcp_cap', 'checkpoint_restore')"
                ).fetchone()
                if row and row[0]:
                    try:
                        split_ts = _measure_facade.datetime.fromisoformat(row[0])
                    except (ValueError, TypeError):
                        pass
                conn.close()
            except (_measure_facade.sqlite3.Error, OSError):
                pass

        # Fallback: try most recent git tag in the repo
        if split_ts is None:
            try:
                result = _measure_facade.subprocess.run(
                    ["git", "log", "--tags", "--simplify-by-decoration",
                     "--format=%ai", "-1"],
                    capture_output=True, text=True, timeout=5,
                    cwd=str(_measure_facade.Path(_measure_facade.__file__).parent), creationflags=_measure_facade._NO_WINDOW,
                )
                if result.returncode == 0 and result.stdout.strip():
                    tag_date = result.stdout.strip()
                    split_ts = _measure_facade.datetime.fromisoformat(tag_date.replace(" ", "T"))
            except (_measure_facade.subprocess.TimeoutExpired, OSError, ValueError):
                pass

        # If still no split point, fall back to halves
        if split_ts is None:
            strategy = "halves"

    # Collect and parse session data
    jsonl_files = _measure_facade._find_all_jsonl_files(days=days)
    sessions = []
    tier = _measure_facade._load_pricing_tier()
    for jf, mtime, project in jsonl_files:
        parsed = _measure_facade._parse_session_jsonl(str(jf))
        if parsed and parsed.get("total_input_tokens", 0) > 0:
            # Compute quality score and cost
            sq = _measure_facade.score_session_quality(parsed)
            dom_model = max(parsed["model_usage"], key=parsed["model_usage"].get) if parsed["model_usage"] else "unknown"
            total_input = parsed["total_input_tokens"]
            chr_val = parsed.get("cache_hit_rate", 0)
            mb = parsed.get("model_usage_breakdown", {})
            cost = _measure_facade._cost_from_model_breakdown(
                mb,
                tier=tier,
                cache_create_1h=parsed.get("total_cache_create_1h", 0),
                cache_create_5m=parsed.get("total_cache_create_5m", 0),
            )
            if cost == 0.0:
                cache_create_1h = parsed.get("total_cache_create_1h", 0) or 0
                cache_create_5m = parsed.get("total_cache_create_5m", 0) or 0
                cache_create = cache_create_1h + cache_create_5m
                cache_read_est = _measure_facade._safe_int(total_input * chr_val)
                cost = _measure_facade._get_model_cost(
                    dom_model,
                    max(0, total_input - cache_read_est - cache_create),
                    parsed["total_output_tokens"],
                    cache_read_est,
                    cache_create,
                    tier=tier,
                    cache_create_1h=cache_create_1h if cache_create else None,
                    cache_create_5m=cache_create_5m if cache_create else None,
                )
            sessions.append({
                "mtime": mtime,
                "input_tokens": total_input,
                "output_tokens": parsed["total_output_tokens"],
                "cache_hit_rate": chr_val,
                "quality_score": sq["score"],
                "cost_usd": cost,
                "duration_minutes": parsed.get("duration_minutes", 0),
            })

    if len(sessions) < 4:
        result = {
            "strategy_used": strategy,
            "error": f"Need at least 4 sessions, found {len(sessions)} ({len(jsonl_files)} total files)",
            "verdict": "insufficient_data",
        }
        if as_json:
            print(_measure_facade.json.dumps(result, indent=2, default=str))
        else:
            print(f"\n  Not enough data: {len(sessions)} parseable sessions (need at least 4).")
            print("  Run more sessions or try --days with a larger window.\n")
        return result

    # Sort oldest first
    sessions.sort(key=lambda s: ((_measure_facade := _compat.facade()), s["mtime"])[1])

    # Split into before/after windows
    if strategy == "halves":
        mid = len(sessions) // 2
        before = sessions[:mid]
        after = sessions[mid:]
        split_label = f"chronological midpoint ({mid} sessions each side)"
    else:
        split_epoch = split_ts.timestamp() if split_ts else 0
        before = [s for s in sessions if s["mtime"] < split_epoch]
        after = [s for s in sessions if s["mtime"] >= split_epoch]
        split_label = f"savings event at {split_ts.strftime('%Y-%m-%d %H:%M') if split_ts else 'unknown'}"

    if len(before) < 2 or len(after) < 2:
        # Unbalanced split, fall back to halves
        mid = len(sessions) // 2
        before = sessions[:mid]
        after = sessions[mid:]
        strategy = "halves"
        split_label = f"chronological midpoint (auto fallback, {mid} sessions each side)"

    def _summarize(window):
        _measure_facade = _compat.facade()
        n = len(window)
        if n == 0:
            return {"count": 0, "avg_tokens": 0, "avg_cost": 0, "avg_quality": 0, "avg_cache_hit": 0}
        return {
            "count": n,
            "avg_tokens": int(sum(s["input_tokens"] for s in window) / n),
            "avg_cost": round(sum(s["cost_usd"] for s in window) / n, 4),
            "avg_quality": round(sum(s["quality_score"] for s in window) / n, 1),
            "avg_cache_hit": round(sum(s["cache_hit_rate"] for s in window) / n, 3),
        }

    before_summary = _summarize(before)
    after_summary = _summarize(after)

    # Calculate deltas (positive = improvement)
    def _delta(metric, invert=False):
        _measure_facade = _compat.facade()
        b = before_summary[metric]
        a = after_summary[metric]
        if b == 0:
            return 0
        raw_pct = ((a - b) / abs(b)) * 100
        return round(-raw_pct if invert else raw_pct, 1)

    deltas = {
        "tokens_pct": _delta("avg_tokens", invert=True),    # fewer tokens = better
        "cost_pct": _delta("avg_cost", invert=True),         # lower cost = better
        "quality_pct": _delta("avg_quality"),                  # higher quality = better
        "cache_hit_pct": _delta("avg_cache_hit"),              # higher cache = better
    }

    # Verdict: improved if at least 2 of 4 metrics show >10% improvement
    improved_count = sum(1 for v in deltas.values() if v > 10)
    regressed_count = sum(1 for v in deltas.values() if v < -10)

    if improved_count >= 2:
        verdict = "improved"
    elif regressed_count >= 2:
        verdict = "regressed"
    else:
        verdict = "no_change"

    result = {
        "strategy_used": strategy,
        "split_label": split_label,
        "before_summary": before_summary,
        "after_summary": after_summary,
        "deltas": deltas,
        "verdict": verdict,
    }

    if as_json:
        print(_measure_facade.json.dumps(result, indent=2, default=str))
    else:
        print(f"\n  VALIDATE IMPACT ({strategy} strategy)")
        print(f"  Split: {split_label}")
        print(f"  {'=' * 58}")
        print(f"\n  {'Metric':<20s} {'Before':>10s} {'After':>10s} {'Change':>10s}")
        print(f"  {'-' * 20}  {'-' * 10}  {'-' * 10}  {'-' * 10}")
        before_cost_str = f"${before_summary['avg_cost']:.4f}"
        after_cost_str = f"${after_summary['avg_cost']:.4f}"
        print(f"  {'Avg tokens/session':<20s} {before_summary['avg_tokens']:>10,}  {after_summary['avg_tokens']:>10,}  {deltas['tokens_pct']:>+9.1f}%")
        print(f"  {'Avg cost/session':<20s} {before_cost_str:>10s}  {after_cost_str:>10s}  {deltas['cost_pct']:>+9.1f}%")
        print(f"  {'Avg quality score':<20s} {before_summary['avg_quality']:>10.1f}  {after_summary['avg_quality']:>10.1f}  {deltas['quality_pct']:>+9.1f}%")
        print(f"  {'Avg cache hit rate':<20s} {before_summary['avg_cache_hit']:>10.3f}  {after_summary['avg_cache_hit']:>10.3f}  {deltas['cache_hit_pct']:>+9.1f}%")

        verdict_emoji = {"improved": "UP", "regressed": "DOWN", "no_change": "FLAT"}
        print(f"\n  Verdict: {verdict.upper()} ({verdict_emoji.get(verdict, '?')})")
        print(f"  Sessions: {before_summary['count']} before, {after_summary['count']} after")
        print()

    return result
