"""Definition-only implementations; mutable state belongs to measure."""
from __future__ import annotations
from . import _compat


def _input_and_cached_read_rates(model):
    """Return (input_rate, cached_read_rate) per-MTok for OpenAI/Gemini/code review
    automatic-discount models, or None if the model isn't a priced non-Claude one.

    Used by the automatic_discount detector to price a cached-share collapse:
    the lost cached tokens get re-billed at the full input rate instead of the
    discounted cached rate.
    """
    _measure_facade = _compat.facade()
    key = _measure_facade._normalize_openai_model_name(model)
    if key:
        r = _measure_facade.OPENAI_MODEL_PRICING[key]
        return r["input"], r.get("cache_read", r["input"])
    key = _measure_facade._normalize_gemini_model_name(model)
    if key:
        r = _measure_facade.GEMINI_MODEL_PRICING[key]
        return r["input"], r.get("cache_read", r["input"])
    value = _measure_facade._strip_provider_prefixes(model) if model else ""
    if value.startswith("deepseek"):
        # code review cache hit = 1/10 input (90% off). No per-model card here;
        # use the verified ratio against a nominal input rate proxy of 1.0 so the
        # COST scales with the verified 0.9x delta regardless of absolute price.
        return 1.0, 0.1
    return None


def _claude_rates_for_model(model, tier_data):
    """Return the Claude rate-card dict for a model id, defaulting to sonnet.

    Returns None for non-Claude models (OpenAI/Gemini) which have no
    cache-WRITE rate and therefore cannot exhibit Anthropic-style TTL waste.
    """
    _measure_facade = _compat.facade()
    if _measure_facade._normalize_openai_model_name(model) or _measure_facade._normalize_gemini_model_name(model):
        return None
    claude_models = tier_data.get("claude_models", {})
    normalized = _measure_facade._claude_price_key(model, claude_models) if model else None
    if normalized and normalized in claude_models:
        return claude_models[normalized]
    return None


def _get_cache_ttl_waste(days=None, collect_sessions=None):
    """Detect prompt-cache waste caused by cache-expiry.

    When `collect_sessions=True` (cache-report --verbose), the result gains a
    `session_detail` list of per-affected-session rows
    {session, gap_seconds, rewritten_tokens, est_cost_usd} so a disputed headline
    number can be drilled into. The detail is NOT cached (it bloats the sidecar);
    --verbose implies a fresh, uncached compute at the call site.

    Walks each session's turn sequence and resolves the dominant model to a
    provider cache profile. For explicit_ttl profiles, a turn pair whose gap
    exceeds that profile's EFFECTIVE TTL and whose next turn re-WRITES a prefix
    cached on the previous turn (cache_creation >= 50% of the previous turn's
    cached volume) is classified as cache-expiry waste. The boundary is
    per-profile: anthropic_api uses the live 5-min default; CLAUDE CODE uses
    3600s — Claude Code requests a 1-hour cache (v2.1.129 fixed the silent
    downgrade to 5 min) and empirically the prefix survives sub-hour pauses, so
    only pauses longer than the hour count.

    Returns an OPPORTUNITY-tier dict (never enters the realized headline):
      {
        "available": bool,
        "tier": "opportunity",
        "period_days": int,
        "wasted_tokens": int,             # re-written/lost tokens after expiry
        "wasted_cost_usd": float,         # write-premium actually paid vs a read
        "would_be_savings_1h_usd": float, # 1h-cache_control counterfactual, valid
                                          #   only for anthropic_api/window profiles
        "avoidable_behavioral_usd": float,# Claude Code: avoidable ONLY by resuming
                                          #   within the hour (no setting helps)
        "affected_sessions": int,
        "scanned_sessions": int,
        "overall_cache_hit_rate": float,  # across all scanned sessions
        "recommendation": str,            # platform-verified remedy text
        "platform": str,
      }
    """
    _measure_facade = _compat.facade()
    runtime = _measure_facade.detect_runtime()
    tier = _measure_facade._load_pricing_tier()
    tier_data = _measure_facade.PRICING_TIERS.get(tier, _measure_facade.PRICING_TIERS["anthropic"])

    # Per-provider accumulators, keyed by cache-profile key.
    providers = {}  # key -> {label, cache_kind, confidence, remedy_key, sessions,
                    #         affected, wasted_tokens, wasted_cost, recoverable}
    no_cache_sessions = 0   # sessions on a cache_kind == "none" model (honest bucket)
    scanned_sessions = 0
    total_cache_read = 0
    total_full_input = 0

    def _prov(key, profile):
        _measure_facade = _compat.facade()
        p = providers.get(key)
        if p is None:
            p = {
                "provider": key,
                "label": profile["label"],
                "cache_kind": profile["cache_kind"],
                "confidence": profile.get("confidence", "estimated"),
                "remedy_key": profile["remedy_key"],
                # W4: default to the CONSERVATIVE kind. A profile missing
                # recoverable_kind must not book the aggressive recoverable_window
                # / recoverable_1h accounting; avoidable_behavioral claims no
                # cache_control counterfactual. Matches _detect_explicit_ttl's
                # default so both ends agree on the safe interpretation.
                "recoverable_kind": profile.get("recoverable_kind", "avoidable_behavioral"),
                "sessions": 0,
                "affected_sessions": 0,
                "wasted_tokens": 0,
                "wasted_cost_usd": 0.0,
                "recoverable_usd": 0.0,
            }
            providers[key] = p
        return p

    base = {
        "available": False,
        "tier": "opportunity",
        "period_days": days,
        "wasted_tokens": 0,
        "wasted_cost_usd": 0.0,
        "would_be_savings_1h_usd": 0.0,
        "avoidable_behavioral_usd": 0.0,
        "affected_sessions": 0,
        "scanned_sessions": 0,
        "no_cache_economics_sessions": 0,
        "overall_cache_hit_rate": 0.0,
        "platform": runtime,
        "by_provider": [],
        "coverage_gaps": _measure_facade._cache_coverage_gaps(runtime),
    }
    session_detail = [] if collect_sessions else None  # W8 drill-down rows

    # Resolve the session source for the active runtime. Claude reads Claude
    # JSONL; Codex reads Codex rollouts (real cached_input_tokens + timestamps —
    # see codex_session.parse_session_turns). Other platforms have no per-turn
    # cache+timestamp source the Python engine can read; they are documented
    # coverage gaps rendered from `coverage_gaps`.
    parse_fn = None
    if runtime not in _measure_facade._CACHE_WIRED_RUNTIMES:
        files = []
    elif runtime == "claude":
        try:
            files = [fp for fp, _m, _p in _measure_facade._find_all_jsonl_files(days)]
        except Exception:
            files = []
        parse_fn = _measure_facade.parse_session_turns
    elif runtime == "codex":
        try:
            import codex_session
            files = [fp for fp, _m, _p in codex_session.find_all_jsonl_files(days=days)]
            parse_fn = codex_session.parse_session_turns
        except Exception:
            files = []
    else:
        files = []

    if not files or parse_fn is None:
        base["recommendation"] = _measure_facade._cache_remedy_text("none")
        return base

    # A rebuild counts only when its resume turn falls inside the window: file
    # mtime alone would let a long-lived, recently touched file leak older ones.
    cutoff_ts = (_measure_facade.time.time() - days * 86400) if days is not None else None
    rebuild_events = {}   # provider key -> [event]; filtered and deduped after the scan
    ttl_split = {}        # provider key -> [1h write tokens, 5m write tokens] in window
    rebuild_ttl = {}      # provider key -> profile TTL, used when no split is logged
    candidate_floor = 300  # scan at the shortest TTL; the observed boundary filters later

    for filepath in files:
        try:
            turns = parse_fn(filepath)
        except Exception:
            continue
        if not turns:
            continue
        scanned_sessions += 1

        # Resolve the session's dominant model -> cache profile.
        model = _measure_facade._dominant_turn_model(turns)
        prof_key, profile = _measure_facade._resolve_cache_profile(model, runtime=runtime)
        kind = profile["cache_kind"]
        prov = _prov(prof_key, profile)
        prov["sessions"] += 1

        # Track overall hit-rate across all scanned sessions (every cache_kind).
        for turn in turns:
            cr = int(turn.get("cache_read", 0) or 0)
            cc = int(turn.get("cache_creation", 0) or 0)
            total_cache_read += cr
            total_full_input += cr + cc + int(turn.get("input_tokens", 0) or 0)

        if kind == "none":
            no_cache_sessions += 1
            continue

        detail_out = {} if collect_sessions else None
        if kind == "explicit_ttl":
            split = ttl_split.setdefault(prof_key, [0, 0])
            for turn in turns:
                if cutoff_ts is not None:
                    turn_ts = _measure_facade._keepwarm_iso_to_epoch(turn.get("timestamp"))
                    if turn_ts is None or turn_ts < cutoff_ts:
                        continue
                split[0] += int(turn.get("cache_creation_1h", 0) or 0)
                split[1] += int(turn.get("cache_creation_5m", 0) or 0)
            # W5: pass the already-resolved dominant model. Candidates are kept
            # at the shortest TTL; the user's observed TTL filters them below.
            found = []
            _measure_facade._detect_explicit_ttl(
                turns, tier_data, profile, dominant_model=model,
                boundary=min(candidate_floor, profile.get("effective_ttl_seconds") or candidate_floor),
                cutoff_ts=cutoff_ts, events_out=found)
            rebuild_ttl.setdefault(prof_key, profile.get("effective_ttl_seconds") or 3600)
            for event in found:
                event["session"] = str(filepath)
                event["recoverable_kind"] = profile.get("recoverable_kind", "avoidable_behavioral")
            rebuild_events.setdefault(prof_key, []).extend(found)
            continue
        else:  # automatic_discount or explicit_storage
            # W2: pass the dominant model so turns missing their own `model` fall
            # back to it for rate lookup instead of being dropped.
            sw_tokens, sw_cost, recoverable, had = _measure_facade._detect_automatic_collapse(
                turns, profile, dominant_model=model, detail_out=detail_out)

        if had:
            prov["affected_sessions"] += 1
            prov["wasted_tokens"] += sw_tokens
            prov["wasted_cost_usd"] += sw_cost
            prov["recoverable_usd"] += recoverable
            if session_detail is not None:
                session_detail.append({
                    "session": _measure_facade.Path(filepath).name,
                    "gap_seconds": int((detail_out or {}).get("max_gap", 0)),
                    "rewritten_tokens": int(sw_tokens),
                    "est_cost_usd": round(sw_cost, 4),
                })

    # Cache rebuilds: pick each provider's TTL from the user's own 1h/5m write
    # split, keep rebuilds past it, and count a request copied into two
    # transcript directories once (request id, else timestamp + sizes).
    seen_requests, seen_shapes = set(), set()
    rebuild = {"events": 0, "tokens": 0, "cost_usd": 0.0, "write_usd": 0.0,
               "read_instead_usd": 0.0, "sessions": 0, "ttl_seconds": None,
               "tokens_by_class": {"cache_write_1h": 0, "cache_write_5m": 0}}
    rebuild_sessions = set()
    top_cost = -1.0
    for key, events in rebuild_events.items():
        p = providers[key]
        h1, m5 = ttl_split.get(key, (0, 0))
        boundary = (3600 if h1 >= m5 else 300) if (h1 + m5) > 0 else rebuild_ttl.get(key, 3600)
        p["ttl_seconds"] = boundary
        by_session = {}
        # Events with a request id go first, so a copy without ids always
        # meets the shapes of the ones that have them.
        for e in sorted(events, key=lambda ev: not ev.get("request_id")):
            if e["gap_seconds"] is None or e["gap_seconds"] <= boundary:
                continue
            rid = e.get("request_id")
            shape = (e.get("timestamp"), e["resume_write"], e["prev_cached"])
            # A request id is authoritative; the shape only stands in for a
            # copy that lost its ids, so two real sessions never collide.
            if (rid in seen_requests) if rid else (shape in seen_shapes):
                continue
            if rid:
                seen_requests.add(rid)
            seen_shapes.add(shape)
            if e.get("share_1h") is None:
                # No TTL split on the turn: the write rate follows the boundary.
                e["share_1h"] = 1.0 if boundary >= 3600 else 0.0
                e["write_rate"] = e["write_1h"] if boundary >= 3600 else e["write_5m"]
                e["cost_usd"] = max(0.0, e["tokens"] * (e["write_rate"] - e["read_rate"]) / 1e6)
            by_session.setdefault(e["session"], []).append(e)
        prov_cost = 0.0
        for session, kept in by_session.items():
            s_tokens = sum(e["tokens"] for e in kept)
            s_cost = sum(e["cost_usd"] for e in kept)
            if kept[0]["recoverable_kind"] == "recoverable_1h":
                s_rec = max(0.0, sum(e["repeat_write_usd"] for e in kept) - kept[0].get("first_premium_usd", 0.0))
            else:
                s_rec = s_cost
            p["affected_sessions"] += 1
            p["wasted_tokens"] += s_tokens
            p["wasted_cost_usd"] += s_cost
            p["recoverable_usd"] += s_rec
            prov_cost += s_cost
            rebuild_sessions.add(session)
            for e in kept:
                rebuild["events"] += 1
                rebuild["tokens"] += e["tokens"]
                rebuild["cost_usd"] += e["cost_usd"]
                rebuild["write_usd"] += e["tokens"] * e["write_rate"] / 1e6
                rebuild["read_instead_usd"] += e["tokens"] * e["read_rate"] / 1e6
                tokens_1h = int(round(e["tokens"] * e["share_1h"]))
                rebuild["tokens_by_class"]["cache_write_1h"] += tokens_1h
                rebuild["tokens_by_class"]["cache_write_5m"] += e["tokens"] - tokens_1h
            if session_detail is not None:
                session_detail.append({
                    "session": _measure_facade.Path(session).name,
                    "gap_seconds": int(max(e["gap_seconds"] for e in kept)),
                    "rewritten_tokens": int(s_tokens),
                    "est_cost_usd": round(s_cost, 4),
                })
        if prov_cost > top_cost:
            top_cost = prov_cost
            rebuild["ttl_seconds"] = boundary
    rebuild["sessions"] = len(rebuild_sessions)
    for field in ("cost_usd", "write_usd", "read_instead_usd"):
        rebuild[field] = round(rebuild[field], 4)

    overall_hit_rate = (total_cache_read / total_full_input) if total_full_input > 0 else 0.0

    by_provider = []
    total_waste_tokens = 0
    total_waste_cost = 0.0
    total_recoverable = 0.0          # 1h-cache_control counterfactual (anthropic_api/window)
    total_avoidable_behavioral = 0.0  # claude_code: avoidable only by resuming in-hour
    total_affected = 0
    for key, p in sorted(providers.items(), key=lambda kv: ((_measure_facade := _compat.facade()), -kv[1]["wasted_cost_usd"])[1]):
        total_waste_tokens += p["wasted_tokens"]
        total_waste_cost += p["wasted_cost_usd"]
        rec_kind = p.get("recoverable_kind", "avoidable_behavioral")  # W4: conservative default
        if rec_kind == "avoidable_behavioral":
            total_avoidable_behavioral += p["recoverable_usd"]
        else:
            total_recoverable += p["recoverable_usd"]
        total_affected += p["affected_sessions"]
        by_provider.append({
            "provider": key,
            "label": p["label"],
            "cache_kind": p["cache_kind"],
            "confidence": p["confidence"],
            "recoverable_kind": rec_kind,
            "sessions": p["sessions"],
            "affected_sessions": p["affected_sessions"],
            "wasted_tokens": int(p["wasted_tokens"]),
            "wasted_cost_usd": round(p["wasted_cost_usd"], 4),
            "recoverable_usd": round(p["recoverable_usd"], 4),
            "ttl_seconds": p.get("ttl_seconds"),
            "remedy": _measure_facade._cache_remedy_text(p["remedy_key"]),
        })

    base.update({
        "available": scanned_sessions > 0,
        "wasted_tokens": int(total_waste_tokens),
        "wasted_cost_usd": round(total_waste_cost, 4),
        # Honest split: 1h-cache_control counterfactual (valid only for
        # API/SDK/harness sessions) vs avoidable-only-behaviorally (Claude Code,
        # which already holds a 1h cache; only a within-hour resume helps).
        "would_be_savings_1h_usd": round(total_recoverable, 4),
        "avoidable_behavioral_usd": round(total_avoidable_behavioral, 4),
        "affected_sessions": total_affected,
        "scanned_sessions": scanned_sessions,
        "no_cache_economics_sessions": no_cache_sessions,
        "overall_cache_hit_rate": round(overall_hit_rate, 4),
        "by_provider": by_provider,
        # Cache-write rebuilds only (the fold's "Room to save more" line reads
        # this): count, rebuilt tokens and dollars at API rates, TTL observed.
        "rebuild": rebuild,
        "engine": 2,
    })
    if session_detail is not None:
        # W8: biggest-waste sessions first, so the disputed numbers are on top.
        session_detail.sort(key=lambda r: ((_measure_facade := _compat.facade()), -r["est_cost_usd"])[1])
        base["session_detail"] = session_detail
    # Headline recommendation: the remedy for the biggest-waste provider, or the
    # active runtime's default when no waste was found.
    if by_provider and by_provider[0]["wasted_cost_usd"] > 0:
        base["recommendation"] = by_provider[0]["remedy"]
    else:
        default_remedy = {"claude": "claude_code", "codex": "openai"}.get(runtime, "none")
        base["recommendation"] = _measure_facade._cache_remedy_text(default_remedy)
    return base


def _cache_ttl_waste_cached(days=None, fresh=None):
    """`_get_cache_ttl_waste` with a 15-min sidecar cache (cache_health.json).

    Returns the cached payload when the sidecar is younger than the TTL AND its
    stored `days` AND `platform` (runtime) match the current call; otherwise
    recomputes and rewrites the sidecar. The runtime is part of the hit
    condition because a `cd` into a Codex project (or a runtime flip) changes the
    session source — a sidecar computed under a different runtime would otherwise
    be served as if it described the current one. `fresh=True` bypasses the read
    (cache-report --fresh). Freshness uses the mtime idiom (module-level
    (mtime, payload) memo) for consistency with _demoted_cohorts. Fail-open: any
    cache I/O error falls through to a fresh compute.
    """
    _measure_facade = _compat.facade()
    pass
    cache_path = _measure_facade.SNAPSHOT_DIR / "cache_health.json"
    current_runtime = _measure_facade.detect_runtime()

    def _is_hit(payload):
        _measure_facade = _compat.facade()
        try:
            return (
                int(payload.get("period_days", -1)) == int(days)
                and payload.get("platform") == current_runtime
                and payload.get("engine") == 2  # older sidecars predate the rebuild fix
                and (_measure_facade.time.time() - float(payload.get("_cached_ts", 0)))
                < _measure_facade._CACHE_HEALTH_CACHE_TTL_SECONDS
            )
        except (TypeError, ValueError):
            return False

    if not fresh:
        try:
            mtime = cache_path.stat().st_mtime
            # In-process memo: same file version -> no re-read, no re-parse.
            if _measure_facade._cache_health_memo is not None and _measure_facade._cache_health_memo[0] == mtime:
                payload = _measure_facade._cache_health_memo[1]
                if _is_hit(payload):
                    return payload
            else:
                payload = _measure_facade.json.loads(cache_path.read_text(encoding="utf-8"))
                _measure_facade._cache_health_memo = (mtime, payload)
                if _is_hit(payload):
                    return payload
        except (_measure_facade.json.JSONDecodeError, OSError, ValueError, TypeError):
            pass
    result = _measure_facade._get_cache_ttl_waste(days=days)
    try:
        record = dict(result)
        record["_cached_ts"] = _measure_facade.time.time()
        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        # mkstemp + replace: an interrupted write never leaves a half-written
        # sidecar, and the 0600 mode is set before any content is visible.
        fd, tmp_name = _measure_facade.tempfile.mkstemp(
            prefix=".cache_health.", suffix=".tmp", dir=str(_measure_facade.SNAPSHOT_DIR))
        try:
            if hasattr(_measure_facade.os, "fchmod"):  # POSIX only; mkstemp is already 0600
                _measure_facade.os.fchmod(fd, 0o600)
            with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as fh:
                # W7: json.dump can raise ValueError (e.g. a non-serializable
                # value or nan/inf with allow_nan path quirks), not just OSError;
                # broaden the inner cleanup trigger to Exception so a serialize
                # failure still unlinks the temp file instead of leaking it.
                _measure_facade.json.dump(record, fh)
            _measure_facade.os.replace(tmp_name, str(cache_path))
            try:
                _measure_facade._cache_health_memo = (_measure_facade.os.stat(cache_path).st_mtime, record)
            except OSError:
                _measure_facade._cache_health_memo = None
        except Exception:
            try:
                _measure_facade.os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception:
        # W7: the whole memoization layer is best-effort; any failure (I/O or
        # serialize) just means this call returns the fresh result uncached.
        pass
    return result


def _keepwarm_sidecar_path():
    _measure_facade = _compat.facade()
    return _measure_facade.SNAPSHOT_DIR / _measure_facade._KEEPWARM_SIDECAR_NAME


def _keepwarm_lock():
    """Bounded portable lease serializing keep-warm sidecar read-modify-writes.

    Two Stop hooks from different sessions can append concurrently, and the tick
    loop rewrites the whole file during compaction; without this an interleaved
    append + rewrite could lose a record or leave a torn line. Contenders skip
    the mutation after 75ms rather than blocking a hook.
    """
    _measure_facade = _compat.facade()
    lock_path = _measure_facade.SNAPSHOT_DIR / f"{_measure_facade._KEEPWARM_LOCK_NAME}.lease"
    with _measure_facade.lease_lock(
        lock_path,
        deadline=_measure_facade.current_deadline(),
        acquire_timeout=0.075,
    ) as acquired:
        yield acquired


def _keepwarm_append_line(path, line):
    """Append one already-serialized line to `path` under the keep-warm lock.

    The shared sidecar append pattern: take _keepwarm_lock, ensure
    SNAPSHOT_DIR exists, create the file 0600 BEFORE the first write (or re-chmod
    an existing one) so a group/other-readable sidecar can never exist, then
    append. Returns True on success, False on any OSError. Used by the arm-writer
    and the ping ledger; both serialize through the same lock so an
    append can never tear against a concurrent compaction rewrite.
    """
    _measure_facade = _compat.facade()
    try:
        with _measure_facade._keepwarm_lock() as acquired:
            if not acquired:
                return False
            try:
                _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            except OSError:
                return False
            try:
                # Open the append target with O_NOFOLLOW so a pre-planted symlink
                # at this path (same-UID TOCTOU, security M3) cannot redirect our
                # ledger/state JSON into a victim file: ELOOP -> hard skip. Create
                # 0600 if absent. The single fd we write to is the file itself, so
                # no separate chmod-then-open race remains.
                flags = _measure_facade.os.O_APPEND | _measure_facade.os.O_CREAT | _measure_facade.os.O_WRONLY
                if hasattr(_measure_facade.os, "O_NOFOLLOW"):
                    flags |= _measure_facade.os.O_NOFOLLOW
                try:
                    fd = _measure_facade.os.open(str(path), flags, 0o600)
                except OSError:
                    # ELOOP (symlink) or any open failure -> refuse to write.
                    return False
                try:
                    with _measure_facade.os.fdopen(fd, "a", encoding="utf-8") as fh:
                        fh.write(line)
                except OSError:
                    return False
            except OSError:
                return False
        return True
    except Exception:
        return False


def _keepwarm_ttl_kind(usage):
    """Classify a turn's cache TTL as '1h' | '5m' | 'unknown' from usage fields.

    Handles BOTH field shapes Claude Code emits (parse parity with the parser at
    ~7524): the nested `cache_creation.ephemeral_{1h,5m}_input_tokens` object AND
    the flat usage-level `ephemeral_{1h,5m}_input_tokens` fields. Whichever side is
    larger wins; a tie or both-zero is 'unknown' (the default-TTL regression means
    we must never assume 1h — KTD/Risk: per-session measured kind, not a hardcode).
    """
    _measure_facade = _compat.facade()
    if not isinstance(usage, dict):
        return "unknown"
    cache_creation = usage.get("cache_creation", {})
    if not isinstance(cache_creation, dict):
        cache_creation = {}
    cc_1h = int(
        cache_creation.get("ephemeral_1h_input_tokens", 0)
        or usage.get("ephemeral_1h_input_tokens", 0)
        or 0
    )
    cc_5m = int(
        cache_creation.get("ephemeral_5m_input_tokens", 0)
        or usage.get("ephemeral_5m_input_tokens", 0)
        or 0
    )
    if cc_1h > cc_5m:
        return "1h"
    if cc_5m > cc_1h:
        return "5m"
    return "unknown"


def _keepwarm_read_tail(filepath, max_lines=None):
    """Return up to the last `max_lines` raw lines of a file, newest-last.

    Bounded-memory deque scan so a huge transcript costs only one pass and a fixed
    window. Returns [] on any read error (the arm-writer fails open to "write
    nothing"). Pure read; no side effects.
    """
    _measure_facade = _compat.facade()
    from collections import deque
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
            return list(deque(fh, maxlen=max_lines))
    except (OSError, ValueError):
        return []


def _keepwarm_last_usage(filepath):
    """Scan a transcript tail for the most recent assistant turn carrying usage.

    Returns (usage_dict, model, ts_str) for the last record whose
    message.usage is non-empty, or (None, None, None) if none found / unreadable.
    Read-only. Used by the fast arm-writer to derive prefix_proxy + ttl_kind
    without re-parsing the whole session.
    """
    _measure_facade = _compat.facade()
    lines = _measure_facade._keepwarm_read_tail(filepath)
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = _measure_facade.json.loads(line)
        except (_measure_facade.json.JSONDecodeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        # Parser parity (integration M2): the realized-booking path
        # (parse_session_turns) only reads type=="assistant" turns, so the arm
        # writer must use the SAME definition of a usage turn. Arming off a
        # non-assistant usage record would spend pings on a session whose resume
        # turn the booker can never read -- structurally-impossible payback.
        if record.get("type") != "assistant":
            continue
        msg = record.get("message", {})
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage", {})
        if isinstance(usage, dict) and usage:
            model = msg.get("model") or record.get("model")
            ts = record.get("timestamp") or record.get("ts")
            return usage, model, ts
    return None, None, None


def _keepwarm_prefix_proxy(usage):
    """Prefix-size proxy for an armed turn: cache_read + cache_creation tokens.

    cache_creation may be the flat int field OR a nested object (sum its ephemeral
    splits). Falls back to 0 on malformed input.
    """
    _measure_facade = _compat.facade()
    usage = _measure_facade._context_usage(usage)
    if not isinstance(usage, dict):
        return 0
    return _measure_facade._context_token_count(usage) - _measure_facade._safe_int(usage.get("input_tokens"))


def _keepwarm_iso_to_epoch(ts):
    """Parse a transcript ISO-8601 timestamp to epoch seconds, or None.

    Tolerates the trailing 'Z' UTC suffix and any non-string/garbage input
    (returns None). Keeps arm records storing `last_turn_ts` as an epoch float so
    it is the same type the policy and replay paths consume — no silent fallback.
    """
    _measure_facade = _compat.facade()
    if ts is None:
        return None
    try:
        return _measure_facade.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def write_keepwarm_arm_record(session_id, transcript_path, now=None):
    """Append one keep-warm arm record. Dumb + fast; safe to call from the Stop hook.

    Derives prefix_proxy (last-turn cache_read + cache_creation), model, and
    ttl_kind from the transcript tail, then appends a single JSON line to the
    keepwarm_state.jsonl sidecar under flock with an atomic-equivalent append.
    Returns the record dict on success, or None when it wrote nothing (missing
    session_id/transcript, unreadable transcript, or no usage-bearing turn).

    NEVER raises into the hook chain: all errors are swallowed -> return None
    (a hook failure must not break the user's session). Eligibility (api
    billing, prefix >=10k, etc.) is deliberately NOT evaluated here — that is the
    tick loop's job (the hook stays dumb).
    """
    _measure_facade = _compat.facade()
    try:
        if not session_id or not transcript_path:
            return None
        # Claude Code cloud: no local CLI to keep warm and the VM is reclaimed
        # with the session, so an arm record can never be acted on.
        if _measure_facade._remote_lean_profile():
            return None
        # ANTI-RECURSION contract: a keep-warm ping subprocess carries
        # TOKEN_OPTIMIZER_KEEPWARM_PING=1; its own Stop hook must never arm,
        # re-arm, or refresh any record -- no self-feeding ping loops.
        if _measure_facade.os.environ.get("TOKEN_OPTIMIZER_KEEPWARM_PING"):
            return None
        # Only arm under the claude_code runtime: Codex has no write premium to
        # recover, and the prefix proxy below is Claude-shaped. Cheap guard,
        # fail-open to "arm anyway" only if detection errors (tick re-checks).
        try:
            if _measure_facade.detect_runtime() != "claude":
                return None
        except Exception:
            pass
        from . import keepwarm_safety
        owner = keepwarm_safety.find_owner()
        if owner is None:
            return None
        tp = _measure_facade.Path(transcript_path)
        usage, model, ts = _measure_facade._keepwarm_last_usage(tp)
        if not usage:
            return None
        prefix_proxy = _measure_facade._keepwarm_prefix_proxy(usage)
        ttl_kind = _measure_facade._keepwarm_ttl_kind(usage)
        if now is None:
            now = _measure_facade.time.time()
        try:
            st = tp.stat()
            transcript_mtime = st.st_mtime
            # Byte size is the resume signal: an O(1) stat-based
            # equality replaces the old full line-count scan. A resume appends
            # turns -> the file GROWS, so a later size increase past the armed size
            # is the resume marker. This removes the per-arm full read (which the
            # SIGALRM hook budget bounded but a multi-MB transcript still strained).
            byte_size = int(st.st_size)
        except OSError:
            return None
        # Store the last real turn as epoch seconds (the policy/replay type),
        # falling back to the arm time when the transcript timestamp is absent.
        last_turn_epoch = _measure_facade._keepwarm_iso_to_epoch(ts)
        if last_turn_epoch is None:
            last_turn_epoch = float(now)
        record = {
            "session_id": str(session_id),
            "transcript_path": str(tp),
            "model": model,
            "prefix_proxy": int(prefix_proxy),
            "ttl_kind": ttl_kind,
            "last_turn_ts": last_turn_epoch,
            "transcript_mtime": float(transcript_mtime),
            "byte_size": int(byte_size),
            "ts": float(now),
            "state": "armed", "owner": owner,
        }
        line = _measure_facade.json.dumps(record) + "\n"
        # Idempotency (state-machine invariant: ONE active arm per
        # session+transcript-snapshot). A double-registered Stop hook (the
        # settings-fossil case) or two Stop events with no transcript growth
        # between them would otherwise append a second record with an identical
        # (session_id, transcript_mtime, byte_size) snapshot, and the tick then
        # fires one probe ping PER RECORD for the same paused session -- the
        # per-pause probe cap is tracked per record, and the ping's
        # --no-session-persistence never bumps the mtime the classifier watches
        # (regression: tests/test_keepwarm_arm_dedup.py). The check and the
        # append must share ONE lock hold, or a concurrent arm/compaction can
        # slip between them; the lease is not reentrant, so the append body is
        # inlined here instead of calling _keepwarm_append_line (which takes
        # the same lock itself).
        try:
            with _measure_facade._keepwarm_lock() as acquired:
                if not acquired:
                    return None
                for existing in _measure_facade.load_keepwarm_records():
                    if existing.get("session_id") != record["session_id"]:
                        continue
                    try:
                        same_snapshot = (
                            abs(float(existing.get("transcript_mtime", 0) or 0)
                                - record["transcript_mtime"]) < 0.001
                            and existing.get("byte_size") is not None
                            and int(existing.get("byte_size")) == record["byte_size"])
                    except (TypeError, ValueError):
                        same_snapshot = False
                    if same_snapshot:
                        # Already armed for this exact pause state; keep the
                        # original record (and its pings_fired history).
                        return existing
                # The append body of _keepwarm_append_line, inlined so it runs
                # inside this lock hold (same O_NOFOLLOW / 0600 semantics).
                try:
                    _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
                except OSError:
                    return None
                try:
                    flags = _measure_facade.os.O_APPEND | _measure_facade.os.O_CREAT | _measure_facade.os.O_WRONLY
                    if hasattr(_measure_facade.os, "O_NOFOLLOW"):
                        flags |= _measure_facade.os.O_NOFOLLOW
                    try:
                        fd = _measure_facade.os.open(str(_measure_facade._keepwarm_sidecar_path()), flags, 0o600)
                    except OSError:
                        return None
                    try:
                        with _measure_facade.os.fdopen(fd, "a", encoding="utf-8") as fh:
                            fh.write(line)
                    except OSError:
                        return None
                except OSError:
                    return None
        except Exception:
            return None
        return record
    except Exception:
        # Absolute backstop: nothing in the arm path may propagate to the hook.
        return None


def _valid_keepwarm_record(rec):
    """Schema-check one parsed arm record (corrupt -> skip, fail-open to not armed)."""
    _measure_facade = _compat.facade()
    if not isinstance(rec, dict):
        return False
    if not rec.get("session_id") or not rec.get("transcript_path"):
        return False
    try:
        float(rec.get("ts"))
    except (TypeError, ValueError):
        return False
    return True


def load_keepwarm_records(path=None):
    """Load valid arm records from the sidecar; corrupt lines are skipped.

    Returns a list of record dicts (possibly empty). Never raises: a missing file
    -> [], an unreadable file -> [], a malformed line -> skipped (fail-open to
    "not armed" — a record we cannot trust must never cause a ping).
    """
    _measure_facade = _compat.facade()
    if path is None:
        path = _measure_facade._keepwarm_sidecar_path()
    records = []
    try:
        if not _measure_facade.Path(path).exists():
            return []
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _measure_facade.json.loads(line)
                except (_measure_facade.json.JSONDecodeError, ValueError):
                    continue
                if _measure_facade._valid_keepwarm_record(rec):
                    records.append(rec)
    except OSError:
        return []
    return records


def classify_keepwarm_record(rec, now=None):
    """Pure-ish resume classifier for one arm record (tick-side, consumed).

    Compares the arm record's snapshot of the transcript (mtime + byte_size)
    against the transcript's CURRENT state and returns one of:
        'gone'         -> transcript file no longer exists / unreadable
        'resumed'      -> "stop tracking this arm" umbrella, NOT proof of a real
                          resume. Covers three cases: the transcript grew (a true
                          resume), the arm expired past the 14d max age, and the
                          mtime moved but the file became unreadable. The name is
                          historical; callers that BOOK realized savings
                          (keepwarm_detect_realized) must re-read the post-arm
                          turn's actual usage and never book off this verdict
                          alone. Compaction may safely drop all three.
        'still_paused' -> transcript unchanged since the arm record was written

    The ONLY side effect is reading the transcript tail / stat — no writes, no
    hook involvement. `now` is injectable for deterministic tests.
    """
    _measure_facade = _compat.facade()
    if now is None:
        now = _measure_facade.time.time()
    try:
        arm_ts = float(rec.get("ts", 0) or 0)
    except (TypeError, ValueError):
        arm_ts = 0.0
    # Expired arms are treated as resolved so the tick loop stops tracking them.
    if arm_ts and (now - arm_ts) > _measure_facade._KEEPWARM_MAX_AGE_SECONDS:
        return "resumed"

    tp = rec.get("transcript_path")
    if not tp:
        return "gone"
    p = _measure_facade.Path(tp)
    try:
        if not p.exists():
            return "gone"
        st = p.stat()
        cur_mtime = st.st_mtime
        cur_size = int(st.st_size)
    except OSError:
        return "gone"

    try:
        armed_mtime = float(rec.get("transcript_mtime", 0) or 0)
    except (TypeError, ValueError):
        armed_mtime = 0.0

    from . import keepwarm_safety
    if not keepwarm_safety.owner_alive(rec.get("owner")) or keepwarm_safety.ended_since(
            _measure_facade.SNAPSHOT_DIR, rec.get("session_id"), arm_ts):
        return "gone"
    try:
        armed_size = int(rec["byte_size"])
    except (KeyError, TypeError, ValueError):
        return "gone"
    if cur_size != armed_size or cur_mtime != armed_mtime:
        return "resumed"
    return "still_paused"


def compact_keepwarm_sidecar(path=None, now=None):
    """Rewrite the sidecar keeping only records still worth tracking.

    Drops any record classified 'resumed' or 'gone', plus any older than the 14d
    max age. The surviving 'still_paused' records are rewritten atomically
    (mkstemp+fchmod(0600)+os.replace) under flock. Corrupt lines are dropped (they
    are never loaded). Returns (kept_count, dropped_count). Never raises.

    This is tick-side maintenance (the tick calls it each tick); it is the disarm
    path that belongs outside the hook.
    """
    _measure_facade = _compat.facade()
    if path is None:
        path = _measure_facade._keepwarm_sidecar_path()
    path = _measure_facade.Path(path)
    if now is None:
        now = _measure_facade.time.time()
    kept = []
    dropped = 0
    with _measure_facade._keepwarm_lock() as acquired:
        if not acquired:
            return (0, 0)
        records = _measure_facade.load_keepwarm_records(path)
        # Account for corrupt/invalid lines as dropped for the caller's visibility.
        try:
            raw_lines = 0
            if path.exists():
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    raw_lines = sum(1 for line in fh if line.strip())
            dropped += max(0, raw_lines - len(records))
        except OSError:
            pass
        for rec in records:
            verdict = _measure_facade.classify_keepwarm_record(rec, now=now)
            if verdict == "still_paused":
                kept.append(rec)
            else:
                dropped += 1
        if not path.exists() and not kept:
            return 0, dropped
        try:
            _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = _measure_facade.tempfile.mkstemp(
                prefix=".keepwarm_state.", suffix=".tmp", dir=str(_measure_facade.SNAPSHOT_DIR))
            try:
                if hasattr(_measure_facade.os, "fchmod"):
                    _measure_facade.os.fchmod(fd, 0o600)
                with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as fh:
                    for rec in kept:
                        fh.write(_measure_facade.json.dumps(rec) + "\n")
                _measure_facade.os.replace(tmp_name, str(path))
            except Exception:
                try:
                    _measure_facade.os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError:
            pass
    return len(kept), dropped


def _keepwarm_llm_ping_disabled(env=None):
    """The disable switch stops token-spending pings, including subscription pings."""
    _measure_facade = _compat.facade()
    env = _measure_facade.os.environ if env is None else env
    disabled = str(env.get("TOKEN_OPTIMIZER_KEEPWARM_DISABLE", "")).strip().lower()
    old = env.get(_measure_facade._KEEPWARM_ALLOW_LLM_PING_ENV)
    return str(env.get("TOKEN_OPTIMIZER_KEEPWARM", "1")).strip().lower() in ("0", "false", "no", "off") or disabled in ("1", "true", "yes", "on") or (old is not None and str(old).strip().lower() in ("0", "false", "no", "off"))


def _keepwarm_default_claude_json_path():
    """The Claude Code top-level config file (~/.claude.json). Read-only here.

    This lives in HOME, NOT inside ~/.claude/ — Claude Code writes its top-level
    config to ~/.claude.json. Using CLAUDE_DIR/.claude.json would silently miss
    the real file and mis-detect every API user as subscription.
    """
    _measure_facade = _compat.facade()
    return _measure_facade.HOME / ".claude.json"


def _keepwarm_default_settings_path():
    """The Claude Code settings file (~/.claude/settings.json). Read-only here."""
    _measure_facade = _compat.facade()
    return _measure_facade.CLAUDE_DIR / "settings.json"


def _keepwarm_env_says_api(env):
    """True iff an API-key env var is present and non-empty.

    Checks ANTHROPIC_API_KEY then ANTHROPIC_AUTH_TOKEN. Only presence/non-empty
    is consulted — the value itself is never read past truthiness and is never
    logged or returned anywhere.
    """
    _measure_facade = _compat.facade()
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        val = env.get(name)
        if isinstance(val, str) and val.strip():
            return True
    return False


def _keepwarm_json_says_api(path):
    """Inspect a Claude Code JSON config file for API-key billing markers.

    Returns 'api' if a positive API-key indicator is present
    (apiKeyHelper / primaryApiKey / a non-empty customApiKeyResponses set),
    'subscription' if an OAuth/account marker is present and no API-key marker
    is, or None if the file is missing/unreadable/uninformative (caller decides
    the conservative default). Reads ONLY key presence — secret values are never
    dereferenced beyond a truthiness check and never returned or printed.
    """
    _measure_facade = _compat.facade()
    try:
        path = _measure_facade.Path(path)
        # A FIFO would block read_text() forever; only a regular file is a config.
        if not _measure_facade._is_regular_file(path):
            return None
        cfg = _measure_facade.json.loads(path.read_text(encoding="utf-8"))
    except (OSError, _measure_facade.json.JSONDecodeError, ValueError):
        return None
    if not isinstance(cfg, dict):
        return None
    # API-key indicators (any one is decisive — env wins earlier, but a config
    # apiKeyHelper/primaryApiKey is a strong positive on its own).
    helper = cfg.get("apiKeyHelper")
    if isinstance(helper, str) and helper.strip():
        return "api"
    primary = cfg.get("primaryApiKey")
    if isinstance(primary, str) and primary.strip():
        return "api"
    # customApiKeyResponses: Claude Code records approved/rejected API keys here.
    # A non-empty 'approved' list means the user has actively used an API key.
    custom = cfg.get("customApiKeyResponses")
    if isinstance(custom, dict):
        approved = custom.get("approved")
        if isinstance(approved, (list, tuple)) and len(approved) > 0:
            return "api"
    # No API marker. An OAuth/account marker means a logged-in subscription.
    oauth = cfg.get("oauthAccount")
    if isinstance(oauth, dict) and oauth:
        return "subscription"
    return None


def keepwarm_billing_mode(env=None, claude_json_path=None, settings_path=None):
    """Detect billing class: 'api' (eligible) or 'subscription' (hard off).

    Conservative ladder (first decisive rung wins):
      1. env ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN non-empty  -> 'api'
      2. ~/.claude.json   apiKeyHelper/primaryApiKey/customApiKey -> 'api'
                          oauthAccount (and no api marker)        -> 'subscription'
      3. ~/.claude/settings.json apiKeyHelper                     -> 'api'
      4. nothing detectable                                       -> 'subscription'

    Step 4 is the safety default: an unknown user is assumed subscription so
    keep-warm stays off rather than risk burning quota. Pure + injectable.
    """
    _measure_facade = _compat.facade()
    if env is None:
        env = _measure_facade.os.environ
    if claude_json_path is None:
        claude_json_path = _measure_facade._keepwarm_default_claude_json_path()
    if settings_path is None:
        settings_path = _measure_facade._keepwarm_default_settings_path()
    if _measure_facade._keepwarm_env_says_api(env):
        return "api"
    # Rung 2: ~/.claude.json. A positive 'api' is decisive; a 'subscription'
    # marker here is provisional (settings.json could still carry apiKeyHelper).
    json_verdict = _measure_facade._keepwarm_json_says_api(claude_json_path)
    if json_verdict == "api":
        return "api"
    # Rung 3: settings.json apiKeyHelper (some users configure billing only here).
    settings_verdict = _measure_facade._keepwarm_json_says_api(settings_path)
    if settings_verdict == "api":
        return "api"
    # Rung 2 (resolved): if ~/.claude.json positively identified subscription and
    # nothing upgraded it to api, honor it.
    if json_verdict == "subscription":
        return "subscription"
    # Rung 4: nothing detectable -> conservative subscription (off).
    return "subscription"


def keepwarm_consent():
    """Read the sticky tri/quad-state consent record.

    Returns one of 'unasked' | 'asked' | 'declined' | 'enabled'. Absent key (or
    any corruption) reads as 'unasked' — fail-safe to the never-pings default.
    Stored via _read_config_flag so it shares config.json's atomic surface.
    """
    _measure_facade = _compat.facade()
    val = _measure_facade._read_config_flag("v5_keepwarm", None); val = ("enabled" if val else "declined") if isinstance(val, bool) else _measure_facade._read_config_flag(_measure_facade._KEEPWARM_CONSENT_KEY, "unasked")
    if isinstance(val, str) and val in _measure_facade._KEEPWARM_CONSENT_STATES:
        return val
    return "unasked"


def _keepwarm_set_consent(value):
    """Persist the consent state via _write_config_flag (0600, atomic, flock).

    Rejects unknown states (no-op) so a typo can never write a value that
    keepwarm_consent() would then read back as 'unasked' and silently mis-gate.
    Returns the value actually written, or None if rejected.
    """
    _measure_facade = _compat.facade()
    if value not in _measure_facade._KEEPWARM_CONSENT_STATES:
        return None
    _measure_facade._write_config_flag("v5_keepwarm" if value in ("enabled", "declined") else _measure_facade._KEEPWARM_CONSENT_KEY, value == "enabled" if value in ("enabled", "declined") else value)
    return value


def keepwarm_mark_asked():
    """Idempotently record that the one-time consent pitch was shown.

    Transitions only 'unasked' -> 'asked'. 'asked', 'declined' and 'enabled' are
    all left untouched (the pitch is shown exactly once; a terminal answer is
    never reverted to a re-ask). Returns the resulting state.
    """
    _measure_facade = _compat.facade()
    current = _measure_facade.keepwarm_consent()
    if current == "unasked":
        _measure_facade._keepwarm_set_consent("asked")
        return "asked"
    return current


def keepwarm_enable(env=None, claude_json_path=None, settings_path=None, limits_lab=None):
    """Keep user preferences untouched until the release safety pause is lifted."""
    return False, "Keep-warm: paused while we prove it is safe and saves you money"


def _keepwarm_enable_install_scheduler():
    """Install the scheduler as part of enable; return a status line (never raises).

    CRITICAL activation gap: enable now records consent AND installs the
    scheduler (macOS) so an opt-in user actually gets pinged -- the one-switch
    mental model. A failed install NEVER rolls back consent (the consent is
    already recorded); we report the failure and print the manual command. On a
    non-macOS (documented-gap) OS we print the honest manual-wiring note.
    """
    _measure_facade = _compat.facade()
    try:
        ok, msg = _measure_facade.keepwarm_scheduler_install(quiet=True)
    except Exception as exc:  # never let an install error break the enable
        return ("  Scheduler install errored (%s). Run manually: "
                "measure.py keepwarm-scheduler install" % type(exc).__name__)
    if ok:
        return "  Scheduler installed (macOS): keep-warm ticks every ~5 minutes."
    # Consent stays recorded; surface why install did not land + the manual path.
    first = (msg or "").strip().splitlines()
    head = first[0] if first else "scheduler install did not complete"
    return ("  Scheduler NOT installed: %s\n"
            "  Consent is recorded; run manually: "
            "measure.py keepwarm-scheduler install" % head)


def keepwarm_disable():
    """Explicit opt-out from ANY state. Sets consent='declined' (terminal).

    Always succeeds and is always honored — a declined user is never re-asked
    and is never pinged regardless of billing mode. Returns (True, message).
    """
    _measure_facade = _compat.facade()
    _measure_facade._keepwarm_set_consent("declined")
    return (True, (
        "Keep-warm disabled. No pings will fire and you won't be asked again. "
        "Re-run keepwarm-enable any time to opt back in."
    ))


def keepwarm_consent_status(env=None, claude_json_path=None, settings_path=None):
    """Machine-readable gate inputs for the first-run ASK surface.

    Returns {billing_mode, consent, should_ask} where should_ask is True ONLY
    when billing is 'api' AND consent is 'unasked'. The TO skill / dashboard
    calls this to decide whether to present the one-time pitch; it must transition
    unasked->asked (via keepwarm-consent-asked) immediately after showing it so
    the pitch is shown exactly once.
    """
    _measure_facade = _compat.facade()
    mode = _measure_facade.keepwarm_billing_mode(
        env=env, claude_json_path=claude_json_path, settings_path=settings_path)
    consent = _measure_facade.keepwarm_consent()
    return {
        "billing_mode": mode,
        "consent": consent,
        "should_ask": False,
    }


def keepwarm_gate(env=None, claude_json_path=None, settings_path=None):
    """Fail closed until cache refresh is proven safe for live sessions."""
    _measure_facade = _compat.facade(); from . import keepwarm_economics
    env = _measure_facade.os.environ if env is None else env
    if env.get(_measure_facade._KEEPWARM_PING_ENV):
        return False, "ping-process"
    if _measure_facade._keepwarm_llm_ping_disabled(env):
        return False, "kill-switch"
    if not _measure_facade._is_v5_feature_enabled("keepwarm"): return False, "settings-disabled"
    if _measure_facade.keepwarm_consent() == "declined":
        return False, "consent-declined"
    if _measure_facade._keepwarm_tripwire_off():
        return False, "tripwire-off"
    return False, keepwarm_economics.gate_reason(_measure_facade)


def _keepwarm_tripwire_off():
    """True iff the keep-warm tripwire has demoted the feature to 'off'.

    Thin wrapper over keepwarm_tripwire_mode so the gate stays readable. A
    missing sidecar (no breach yet) is NOT off; a corrupt sidecar reads as off
    (fail-closed, inside keepwarm_tripwire_mode). Defined below the gate but
    resolved at call time. Never raises.
    """
    _measure_facade = _compat.facade()
    try:
        return _measure_facade.keepwarm_tripwire_mode() == "off"
    except Exception:
        return True  # fail-closed: if we cannot read the verdict, refuse


def _keepwarm_scheduler_plist_path():
    """LaunchAgent plist path for the keep-warm agent (NOT the dashboard's)."""
    _measure_facade = _compat.facade()
    return _measure_facade.LAUNCH_AGENTS_DIR / f"{_measure_facade._KEEPWARM_SCHEDULER_LABEL}.plist"


def _keepwarm_scheduler_marker_path():
    _measure_facade = _compat.facade()
    return _measure_facade.SNAPSHOT_DIR / _measure_facade._KEEPWARM_SCHEDULER_MARKER_NAME


def _keepwarm_scheduler_log_path():
    _measure_facade = _compat.facade()
    return _measure_facade.SNAPSHOT_DIR / _measure_facade._KEEPWARM_SCHEDULER_LOG_NAME


def _keepwarm_tick_stamp_path():
    _measure_facade = _compat.facade()
    return _measure_facade.SNAPSHOT_DIR / _measure_facade._KEEPWARM_TICK_STAMP_NAME


def _keepwarm_scheduler_log_append(payload):
    """Append ONE structured JSON line to the tick log (0600). Never raises.

    Used for the per-tick observability line (written even under --quiet) and for
    per-record/per-stage error traces. Best-effort: any OSError is swallowed so a
    full disk never breaks the tick.
    """
    _measure_facade = _compat.facade()
    try:
        line = _measure_facade.json.dumps(payload) + "\n"
    except (TypeError, ValueError):
        return False
    return _measure_facade._keepwarm_append_line(_measure_facade._keepwarm_scheduler_log_path(), line)


def _keepwarm_write_tick_stamp(payload, now=None):
    """Atomically write the tick freshness stamp (0600). Never raises."""
    _measure_facade = _compat.facade()
    if now is None:
        now = _measure_facade.time.time()
    record = dict(payload or {})
    record["ts"] = float(now)
    path = _measure_facade._keepwarm_tick_stamp_path()
    try:
        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = _measure_facade.tempfile.mkstemp(
            prefix=".keepwarm-tick-stamp.", suffix=".tmp", dir=str(_measure_facade.SNAPSHOT_DIR))
        try:
            if hasattr(_measure_facade.os, "fchmod"):
                _measure_facade.os.fchmod(fd, 0o600)
            with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as fh:
                _measure_facade.json.dump(record, fh)
            _measure_facade.os.replace(tmp_name, str(path))
        except Exception:
            try:
                _measure_facade.os.unlink(tmp_name)
            except OSError:
                pass
    except Exception:
        pass


def _keepwarm_read_tick_stamp():
    """Read the tick freshness stamp dict, or None. Never raises."""
    _measure_facade = _compat.facade()
    path = _measure_facade._keepwarm_tick_stamp_path()
    try:
        if not path.exists():
            return None
        data = _measure_facade.json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (_measure_facade.json.JSONDecodeError, OSError, ValueError):
        return None


def _keepwarm_measure_script_path():
    """Absolute path to measure.py for the agent's ProgramArguments.

    Prefers the version-INDEPENDENT marketplace clone path so the agent survives
    plugin updates: a version-pinned ``/plugins/cache/.../<VERSION>/`` path dies on
    the next update, and the launchd agent then runs an old measure.py (or none),
    freezing the dashboard's savings — the exact staleness _heal_keepwarm_plist_path
    repairs for already-installed agents. Falls back to this file's resolved path
    when the stable clone can't be resolved (dev checkouts, non-cache installs), so
    a moved cache or a dev symlink still resolves to a real file (parity with how
    the daemon embeds an absolute script path).
    """
    _measure_facade = _compat.facade()
    stable = _measure_facade._stable_marketplace_script_path("measure.py")
    if stable:
        return stable
    return str(_measure_facade.Path(_measure_facade.__file__).resolve())


def _keepwarm_python3_path():
    """Resolve a python3 launcher the same way the dashboard plist does.

    Mirror of _generate_plist's convention: prefer `shutil.which('python3')`
    but skip a version-manager shim (pyenv/asdf '/shims/'), which would resolve
    differently under launchd's empty environment, and fall back to the system
    interpreter. Keeps the agent portable across the same machines the dashboard
    daemon already supports.
    """
    _measure_facade = _compat.facade()
    import shutil as _shutil
    which = _shutil.which("python3") or ""
    if which and "/shims/" not in which:
        return which
    return "/usr/bin/python3"


def _keepwarm_resolve_claude_bin():
    """Resolve the `claude` CLI to an absolute path at fire time, or None.

    LIVE BUG: under launchd the agent inherits a near-empty PATH, so a
    bare `"claude"` argv[0] fails FileNotFoundError on every ping (30/30 errors on
    this machine). We resolve `claude` exactly the way the python3 resolver works:
    `shutil.which` first (honours the user's real PATH when the tick runs in a
    foreground shell), then the common install locations launchd cannot see. We
    persist NOTHING (a resolved path could go stale across an npm/Homebrew update);
    this runs once per fire. Returns the first executable absolute path, or None
    when nothing resolves (caller books a 'claude-not-found' outcome).
    """
    _measure_facade = _compat.facade()
    import shutil as _shutil
    which = _shutil.which("claude")
    if which:
        return which
    home = _measure_facade.Path.home()
    candidates = [
        home / ".claude" / "local" / "claude",
        _measure_facade.Path("/opt/homebrew/bin/claude"),
        _measure_facade.Path("/usr/local/bin/claude"),
        home / ".local" / "bin" / "claude",
    ]
    # Claude Code's local installer lives under its config dir, so a relocated
    # CLAUDE_CONFIG_DIR moves it off ~/.claude/local.
    if _measure_facade.CLAUDE_DIR / "local" / "claude" not in candidates:
        candidates.insert(1, _measure_facade.CLAUDE_DIR / "local" / "claude")
    for cand in candidates:
        try:
            if cand.is_file() and _measure_facade.os.access(str(cand), _measure_facade.os.X_OK):
                return str(cand)
        except OSError:
            continue
    return None


def _keepwarm_scheduler_install_lock(soft_fail=None):
    """Serialise concurrent keep-warm scheduler installs. SEPARATE lock from the
    dashboard's `_daemon_install_lock` (never shared).

    Same mkdir-mutex + stale-PID reaping shape as `_daemon_install_lock`, but a
    distinct lock directory so the two installers never block each other.
    """
    _measure_facade = _compat.facade()
    from contextlib import contextmanager

    lock_dir = _measure_facade.SNAPSHOT_DIR / _measure_facade._KEEPWARM_SCHEDULER_LOCK_NAME
    pid_file = lock_dir / "pid"
    max_age = 10 * 60

    def _pid_alive(pid):
        _measure_facade = _compat.facade()
        return _measure_facade._pid_is_running(pid)

    def _try_reclaim_stale():
        _measure_facade = _compat.facade()
        try:
            mtime = lock_dir.stat().st_mtime
        except OSError:
            return False
        owner_pid = 0
        try:
            owner_pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            owner_pid = 0
        if not _pid_alive(owner_pid) or (_measure_facade.time.time() - mtime) > max_age:
            try:
                if pid_file.exists():
                    pid_file.unlink()
                lock_dir.rmdir()
                return True
            except OSError:
                return False
        return False

    @contextmanager
    def _locked():
        _measure_facade = _compat.facade()
        acquired = False
        try:
            _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            try:
                lock_dir.mkdir(exist_ok=False)
                acquired = True
            except FileExistsError:
                if _try_reclaim_stale():
                    try:
                        lock_dir.mkdir(exist_ok=False)
                        acquired = True
                    except FileExistsError:
                        acquired = False
                else:
                    acquired = False
            if not acquired:
                yield False
                return
            try:
                pid_file.write_text(str(_measure_facade.os.getpid()), encoding="utf-8")
            except OSError:
                pass
            yield True
        finally:
            if acquired:
                try:
                    if pid_file.exists():
                        pid_file.unlink()
                except OSError:
                    pass
                try:
                    lock_dir.rmdir()
                except OSError:
                    pass

    return _locked()


def _cap_append_log(path, max_bytes=None):
    """Keep an append-only diagnostic log under max_bytes by dropping its older
    half (newest lines kept, truncated in place). Never raises."""
    _measure_facade = _compat.facade()
    try:
        path = _measure_facade.Path(path)
        if not path.exists() or path.stat().st_size <= max_bytes:
            return
        with open(path, "rb") as fh:
            fh.seek(-(max_bytes // 2), _measure_facade.os.SEEK_END)
            tail = fh.read()
        nl = tail.find(b"\n")
        if 0 <= nl < len(tail) - 1:
            tail = tail[nl + 1:]
        with open(path, "r+b") as fh:
            fh.write(tail)
            fh.truncate()
    except OSError:
        pass


def _keepwarm_rotate_log(max_bytes=None):
    """Keep the last ~half of the keep-warm tick log when it exceeds max_bytes.

    Cheap rotation guard called on install and on every tick. launchd appends to
    StandardOutPath/StandardErrorPath forever otherwise; a paused-but-armed
    machine that ticks every 5min would grow this unbounded. We READ the tail,
    then truncate-in-place and rewrite the tail (NOT rename) so the inode launchd
    holds stays valid AND a crash burst that triggers rotation does not erase its
    own cause (a hard truncate-to-zero destroyed the only
    diagnostic trail). Best-effort: any OSError is swallowed.
    """
    _measure_facade = _compat.facade()
    path = _measure_facade._keepwarm_scheduler_log_path()
    keep = max(1, max_bytes // 2)
    # The log is opened+created by launchd (StandardOutPath), which makes it
    # world-readable (0644); it records session_ids + activity. Force 0600 here
    # so the rotation guard (called on install + every tick) also hardens the
    # perms. Best-effort.
    try:
        if path.exists():
            _measure_facade.os.chmod(str(path), 0o600)
    except OSError:
        pass
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            tail = b""
            try:
                with open(str(path), "rb") as fh:
                    fh.seek(-keep, _measure_facade.os.SEEK_END)
                    tail = fh.read()
            except OSError:
                tail = b""
            # Drop a partial leading line so the kept tail starts clean.
            nl = tail.find(b"\n")
            if 0 <= nl < len(tail) - 1:
                tail = tail[nl + 1:]
            # Truncate in place (keep the inode launchd's fd points at) then
            # rewrite the retained tail.
            with open(str(path), "wb") as fh:
                fh.write(tail)
            return True
    except OSError:
        pass
    return False


def _keepwarm_generate_scheduler_plist():
    """Build the launchd plist XML for the keep-warm interval agent.

    Mirrors the dashboard plist's ProgramArguments convention (resolved python3 +
    absolute script path) but: distinct Label, StartInterval (not RunAtLoad
    KeepAlive -- this is a periodic batch job, not a long-lived server),
    ProcessType Background + LowPriorityIO (it must never compete with the user's
    foreground session), ThrottleInterval, and a single combined log target.
    """
    _measure_facade = _compat.facade()
    from xml.sax.saxutils import escape as _xml_escape
    python3_path = _xml_escape(_measure_facade._keepwarm_python3_path())
    script = _xml_escape(_measure_facade._keepwarm_measure_script_path())
    log_path = _xml_escape(str(_measure_facade._keepwarm_scheduler_log_path()))
    snapshot_path = _xml_escape(str(_measure_facade.SNAPSHOT_DIR))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_measure_facade._KEEPWARM_SCHEDULER_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python3_path}</string>
        <string>{script}</string>
        <string>keepwarm-tick</string>
        <string>--quiet</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict><key>TOKEN_OPTIMIZER_SNAPSHOT_DIR</key><string>{snapshot_path}</string></dict>
    <key>StartInterval</key>
    <integer>{_measure_facade._KEEPWARM_SCHEDULER_START_INTERVAL}</integer>
    <key>ThrottleInterval</key>
    <integer>{_measure_facade._KEEPWARM_SCHEDULER_THROTTLE}</integer>
    <key>ProcessType</key>
    <string>Background</string>
    <key>LowPriorityIO</key>
    <true/>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""


def _keepwarm_write_scheduler_marker(bootstrap_rc=None):
    """Write the 0600 install marker (installed_at + plist path + bootstrap_rc).

    ensure-health's repair keys on this marker AND consent so a user-deleted
    plist is only regenerated for a machine we actually installed on. Atomic
    0600 write via mkstemp + os.replace. `bootstrap_rc` records the launchctl
    bootstrap outcome: an int rc, or None when bootstrap could not run;
    omitted (sentinel) for callers that don't know it.
    """
    _measure_facade = _compat.facade()
    marker_path = _measure_facade._keepwarm_scheduler_marker_path()
    payload = {
        "installed_at": _measure_facade.time.time(),
        "plist_path": str(_measure_facade._keepwarm_scheduler_plist_path()),
        "label": _measure_facade._KEEPWARM_SCHEDULER_LABEL,
    }
    if bootstrap_rc != "__unset__":
        payload["bootstrap_rc"] = bootstrap_rc
    tmp_path = None
    try:
        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = _measure_facade.tempfile.mkstemp(
            dir=str(_measure_facade.SNAPSHOT_DIR), prefix=".keepwarm-marker-", suffix=".tmp")
        if hasattr(_measure_facade.os, "fchmod"):  # POSIX only (os.fchmod is absent on Windows)
            _measure_facade.os.fchmod(tmp_fd, 0o600)
        with _measure_facade.os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            _measure_facade.json.dump(payload, f)
        _measure_facade.os.replace(tmp_path, str(marker_path))
        tmp_path = None
        return True
    except OSError:
        return False
    finally:
        if tmp_path is not None:
            try:
                _measure_facade.os.unlink(tmp_path)
            except OSError:
                pass


def _keepwarm_read_scheduler_marker():
    """Return the marker dict, or None if absent/corrupt."""
    _measure_facade = _compat.facade()
    try:
        marker_path = _measure_facade._keepwarm_scheduler_marker_path()
        if not marker_path.exists():
            return None
        data = _measure_facade.json.loads(marker_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, _measure_facade.json.JSONDecodeError, ValueError):
        return None


def _keepwarm_remove_scheduler_marker():
    _measure_facade = _compat.facade()
    try:
        marker_path = _measure_facade._keepwarm_scheduler_marker_path()
        if marker_path.exists():
            marker_path.unlink()
    except OSError:
        pass


def _keepwarm_scheduler_last_tick_ok():
    """Best-effort: report whether the most recent tick looks healthy.

    Returns True/False/None (unknown). PRIMARY source is the tick-stamp written on
    every real tick: a tick with errors==0 and a clean gate is
    ok, otherwise not-ok. Falls back to the tick log tail (traceback => not ok)
    when no stamp exists yet. A missing stamp AND empty log is 'unknown' (None).
    """
    _measure_facade = _compat.facade()
    stamp = _measure_facade._keepwarm_read_tick_stamp()
    if stamp is not None:
        try:
            return int(stamp.get("errors", 0) or 0) == 0
        except (TypeError, ValueError):
            return True
    path = _measure_facade._keepwarm_scheduler_log_path()
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        with open(str(path), "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-20:]
    except OSError:
        return None
    if not tail:
        return None
    # Any traceback in the recent tail = not ok.
    joined = "".join(tail)
    if "Traceback (most recent call last)" in joined:
        return False
    return True


def _keepwarm_scheduler_last_tick_age(now=None):
    """Seconds since the last real tick (from the stamp), or None if unknown."""
    _measure_facade = _compat.facade()
    if now is None:
        now = _measure_facade.time.time()
    stamp = _measure_facade._keepwarm_read_tick_stamp()
    if not stamp:
        return None
    try:
        return max(0.0, float(now) - float(stamp.get("ts", 0) or 0))
    except (TypeError, ValueError):
        return None


def _keepwarm_scheduler_install_macos(quiet=None):
    """macOS: write the keep-warm LaunchAgent plist, marker, and bootstrap it.

    Caller (keepwarm_scheduler_install) has already confirmed the gate allows and
    holds the install lock. Idempotent: bootout-then-bootstrap so a re-install
    refreshes the agent in place. Returns (ok, message).
    """
    # Per-account scheduler job: never touched from a redirected HOME
    # (a test sandbox or scratch run would re-point the user's live job).
    _measure_facade = _compat.facade()
    if _measure_facade._scheduler_install_refused():
        return (False, _measure_facade._SCHEDULER_REFUSAL)
    plist_path = _measure_facade._keepwarm_scheduler_plist_path()
    try:
        _measure_facade.LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return (False, f"could not create agent/snapshot dir: {e}")
    # Rotation guard on install so a stale oversized log doesn't carry over.
    _measure_facade._keepwarm_rotate_log()
    try:
        plist_changed = _measure_facade._write_file_if_changed(plist_path, _measure_facade._keepwarm_generate_scheduler_plist())
    except OSError as e:
        return (False, f"could not write plist {plist_path}: {e}")
    uid = _measure_facade.os.getuid()
    # Skip the reload when nothing changed and the agent is already loaded: an
    # unconditional bootout+bootstrap re-fires the macOS background-item banner on
    # every re-install/refresh even though the on-disk agent is already correct.
    if not plist_changed and _measure_facade._launchagent_loaded(_measure_facade._KEEPWARM_SCHEDULER_LABEL):
        _measure_facade._keepwarm_write_scheduler_marker(bootstrap_rc=0)
        return (True, (
            f"Keep-warm scheduler already current ({_measure_facade._KEEPWARM_SCHEDULER_LABEL}); "
            f"left in place (no reload)."))
    # Idempotent: bootout any existing instance first (ignore failure: not loaded).
    try:
        _measure_facade.subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}", str(plist_path)],
            capture_output=True, text=True, errors="replace", timeout=10, creationflags=_measure_facade._NO_WINDOW,
        )
    except (OSError, _measure_facade.subprocess.TimeoutExpired):
        pass
    try:
        result = _measure_facade.subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
            capture_output=True, text=True, errors="replace", timeout=10, creationflags=_measure_facade._NO_WINDOW,
        )
    except (OSError, _measure_facade.subprocess.TimeoutExpired) as e:
        # Bootstrap could not even run: record the failed attempt in the marker
        # (bootstrap_rc=None) so ensure-health knows WE own this path but the
        # bootstrap never succeeded.
        _measure_facade._keepwarm_write_scheduler_marker(bootstrap_rc=None)
        return (False, f"launchctl bootstrap failed: {e}")
    # Marker written AFTER bootstrap, carrying bootstrap_rc: the marker
    # now reflects the ACTUAL bootstrap outcome, not merely an attempt, so a
    # repair keys on a marker that proves a real install path was reached.
    _measure_facade._keepwarm_write_scheduler_marker(bootstrap_rc=int(result.returncode))
    if result.returncode != 0:
        # Plist + marker are on disk; report so the user can bootstrap manually.
        return (False, (
            f"launchctl bootstrap failed (rc {result.returncode}): "
            f"{(result.stderr or '').strip()}\n"
            f"  Plist written to: {plist_path}\n"
            f"  Try manually: launchctl bootstrap gui/{uid} {plist_path}"))
    return (True, (
        f"Keep-warm scheduler installed ({_measure_facade._KEEPWARM_SCHEDULER_LABEL}, every "
        f"{_measure_facade._KEEPWARM_SCHEDULER_START_INTERVAL}s).\n"
        f"  Plist: {plist_path}\n"
        f"  Log:   {_measure_facade._keepwarm_scheduler_log_path()}\n"
        f"  Remove: measure.py keepwarm-scheduler uninstall"))


def keepwarm_scheduler_install(gate=None, quiet=None):
    """Install the keep-warm interval agent. REFUSES unless the gate allows.

    A refused gate leaves scheduler configuration untouched.
    """
    _measure_facade = _compat.facade(); from . import keepwarm_linux
    gate_fn = gate or _measure_facade.keepwarm_gate
    allowed, reason = gate_fn()
    if not allowed:
        return (False, (
            f"Keep-warm scheduler not installed: gate refused ({reason}). "
            "Check the kill switch, opt-out and tripwire."))
    system = _measure_facade._normalized_platform()
    if system == "Linux":
        with _measure_facade._keepwarm_scheduler_install_lock() as acquired:
            return keepwarm_linux.install(_measure_facade) if acquired else (False, "another keep-warm scheduler install is in progress")
    if system != "Darwin":
        return (False, (
            f"Keep-warm scheduler is unavailable on {system}; this is a "
            "documented gap (Task Scheduler support is not yet implemented). "
            "Track parity in "
            "the README platform table. The keepwarm-tick CLI still works if you "
            "wire your own cron/timer to run it every minute."))
    with _measure_facade._keepwarm_scheduler_install_lock() as acquired:
        if not acquired:
            return (False, "another keep-warm scheduler install is in progress")
        return _measure_facade._keepwarm_scheduler_install_macos(quiet=quiet)


def keepwarm_scheduler_uninstall(quiet=None):
    """Remove the keep-warm agent: bootout + rm plist + rm marker. Always works.

    Unconditional (no gate): a user who opted out or hit a documented-gap OS must
    always be able to clean up. Leaves OTHER keep-warm sidecars (state/ledger/
    consent) intact -- only the scheduler's own plist + marker are removed.
    Returns (ok, message).
    """
    # Per-account scheduler job: never touched from a redirected HOME
    # (a test sandbox or scratch run would re-point the user's live job).
    _measure_facade = _compat.facade(); from . import keepwarm_linux
    if _measure_facade._daemon_snapshot_sandboxed():
        return (False, _measure_facade._SCHEDULER_REFUSAL)
    if _measure_facade._normalized_platform() == "Linux": return keepwarm_linux.uninstall(_measure_facade)
    plist_path = _measure_facade._keepwarm_scheduler_plist_path()
    removed = []
    system = _measure_facade._normalized_platform()
    if system == "Darwin":
        uid = _measure_facade.os.getuid()
        try:
            _measure_facade.subprocess.run(
                ["launchctl", "bootout", f"gui/{uid}", str(plist_path)],
                capture_output=True, text=True, errors="replace", timeout=10, creationflags=_measure_facade._NO_WINDOW,
            )
        except (OSError, _measure_facade.subprocess.TimeoutExpired):
            pass
    try:
        if plist_path.exists():
            plist_path.unlink()
            removed.append(str(plist_path))
    except OSError:
        pass
    marker_path = _measure_facade._keepwarm_scheduler_marker_path()
    marker_existed = marker_path.exists()
    _measure_facade._keepwarm_remove_scheduler_marker()
    if marker_existed:
        removed.append(str(marker_path))
    if removed:
        return (True, "Keep-warm scheduler removed:\n  " + "\n  ".join(removed))
    return (True, "Keep-warm scheduler not installed. Nothing to remove.")


def keepwarm_scheduler_status(gate=None):
    """Return the scheduler status dict.

    {installed, plist_path, label, last_tick_ok, consent, billing_mode|reason}.
    `installed` is True only when BOTH the plist and the marker exist (the marker
    proves WE installed it; the plist proves it is still wired). Pure-ish: reads
    files + the gate, never installs. `gate` injectable for tests.
    """
    _measure_facade = _compat.facade(); from . import keepwarm_linux
    gate_fn = gate or _measure_facade.keepwarm_gate
    if _measure_facade._normalized_platform() == "Linux": return keepwarm_linux.status(_measure_facade, gate_fn)
    plist_path = _measure_facade._keepwarm_scheduler_plist_path()
    marker = _measure_facade._keepwarm_read_scheduler_marker()
    plist_exists = plist_path.exists()
    allowed, reason = gate_fn()
    status = {
        "installed": bool(plist_exists and marker is not None),
        "plist_path": str(plist_path),
        "label": _measure_facade._KEEPWARM_SCHEDULER_LABEL,
        "plist_exists": bool(plist_exists),
        "marker_exists": marker is not None,
        "last_tick_ok": _measure_facade._keepwarm_scheduler_last_tick_ok(),
        "last_tick_age": _measure_facade._keepwarm_scheduler_last_tick_age(),
        "consent": _measure_facade.keepwarm_consent(),
        "billing_mode": _measure_facade.keepwarm_billing_mode(),
        "gate_allowed": bool(allowed),
    }
    if not allowed:
        status["reason"] = reason
    return status


def keepwarm_scheduler_repair(gate=None):
    """ensure-health's consent-gated repair. ADDITIVE, no-op fast.

    Regenerate + bootstrap the keep-warm agent ONLY when ALL hold:
      * consent gate allows (api + consent=enabled / limits-lab);
      * the install marker exists (WE installed here before) -- sticky-opt-out;
      * the plist is missing or stale (label drift / older content).
    Otherwise NO-OP. The hot path when nothing needs repair is cheap file-
    existence checks only -- NO subprocess, NO launchctl -- to honour the <50ms
    ensure-health budget. A user who declined/never-consented, or a machine with
    no marker, is NEVER (re)installed (the sticky-opt-out invariant).

    Returns one of: 'noop-gate', 'noop-no-marker', 'noop-fresh', 'repaired',
    'repair-failed'. Does NOT touch the dashboard plist/lock/regen loop.
    """
    # Claude Code cloud: no launchd/systemd user scheduler in the VM.
    _measure_facade = _compat.facade(); from . import keepwarm_linux
    if _measure_facade._remote_lean_profile():
        return "noop-remote"
    if _measure_facade._normalized_platform() == "Linux": return keepwarm_linux.repair(_measure_facade, gate or _measure_facade.keepwarm_gate)
    # Cheapest gate first: marker existence (one stat) before any gate read.
    marker = _measure_facade._keepwarm_read_scheduler_marker()
    gate_fn = gate or _measure_facade.keepwarm_gate
    allowed, _reason = gate_fn()
    if not allowed:
        return "noop-gate"
    plist_path = _measure_facade._keepwarm_scheduler_plist_path()
    if marker is None and plist_path.exists():
        return "noop-unowned"
    needs_repair = marker is None
    if not plist_path.exists():
        needs_repair = True
    else:
        # Stale if the label marker isn't present (e.g. a hand-edited or
        # foreign plist sitting at our path). Cheap CAPPED read: our
        # plist is <1KB, so reading the first 8KB is enough to find the Label;
        # this bounds the warm-path cost against an attacker-planted huge file.
        try:
            with open(str(plist_path), "r", encoding="utf-8",
                      errors="replace") as _pf:
                content = _pf.read(8192)
            if content != _measure_facade._keepwarm_generate_scheduler_plist():
                needs_repair = True
        except OSError:
            needs_repair = True
    if not needs_repair:
        return "noop-fresh"
    # Repair path: now (and only now) do the costly install. Reuse the same
    # macOS installer + lock so we never double-bootstrap concurrently.
    if _measure_facade._normalized_platform() != "Darwin":
        return "noop-gate"
    with _measure_facade._keepwarm_scheduler_install_lock(soft_fail=True) as acquired:
        if not acquired:
            return "noop-fresh"  # another session is repairing; skip quietly
        ok, _msg = _measure_facade._keepwarm_scheduler_install_macos(quiet=True)
        if not ok:
            # Surface ONE stderr line so a silently-failing repair is diagnosable;
            # the first line carries the launchctl rc/reason.
            try:
                first = (_msg or "repair failed").strip().splitlines()[0]
                print(f"[Token Optimizer] keep-warm scheduler repair failed: "
                      f"{first}", file=_measure_facade.sys.stderr)
            except Exception:
                pass
            return "repair-failed"
        return "repaired"


def _keepwarm_derive_pricing():
    """Derive the keep-warm per-token price + premium tables from PRICING_TIERS.

    Returns (input_per_token, cache_read_per_token, write_premium). Reads the
    canonical anthropic tier so the keep-warm money math can never drift from the
    watchdog's pricing tables. Pure; called once at import.
    """
    _measure_facade = _compat.facade()
    models = _measure_facade.PRICING_TIERS["anthropic"]["claude_models"]
    input_per_token = {}
    cache_read_per_token = {}
    for key, rates in models.items():
        input_per_token[key] = rates["input"] / 1_000_000
        cache_read_per_token[key] = rates["cache_read"] / 1_000_000
    # Premiums are ratios on the input rate; take them from sonnet (uniform across
    # models within a tier: cache_write=1.25x input, cache_write_1h=2.0x input).
    s = models["sonnet"]
    premium_5m = s["cache_write"] / s["input"]
    premium_1h = s["cache_write_1h"] / s["input"]
    write_premium = {"1h": premium_1h, "5m": premium_5m, "unknown": premium_5m}
    return input_per_token, cache_read_per_token, write_premium


def _keepwarm_model_norm(model):
    """Normalize an arm record's model id to a pricing key, defaulting to sonnet."""
    _measure_facade = _compat.facade()
    norm = _measure_facade._normalize_model_name(model) if model else None
    if norm in _measure_facade._KEEPWARM_INPUT_USD_PER_TOKEN:
        return norm
    return "sonnet"


def _keepwarm_p_resume(elapsed_seconds):
    """P(user resumes | still paused at `elapsed_seconds`) from the survival table.

    Uses the nearest bucket at or below elapsed. Below the first bucket (<1h) we
    use the 1h rate (the most generous still-defensible estimate); past the last
    bucket we use the last (lowest) rate. Pure.
    """
    _measure_facade = _compat.facade()
    try:
        e = float(elapsed_seconds)
    except (TypeError, ValueError):
        return _measure_facade._KEEPWARM_SURVIVAL_TABLE[0][1]
    rate = _measure_facade._KEEPWARM_SURVIVAL_TABLE[0][1]
    for bucket, p in _measure_facade._KEEPWARM_SURVIVAL_TABLE:
        if e >= bucket:
            rate = p
        else:
            break
    return rate


def _keepwarm_avoided_write_usd(prefix_tokens, ttl_kind, model, elapsed_seconds):
    """Expected USD saved by keeping the prefix warm for a resume at `elapsed`.

    = prefix_tokens * input_rate * write_premium(ttl) * P(resume | elapsed).
    This is the right-hand side of the per-pause break-even inequality. Pure.
    """
    _measure_facade = _compat.facade()
    key = _measure_facade._keepwarm_model_norm(model)
    input_rate = _measure_facade._KEEPWARM_INPUT_USD_PER_TOKEN.get(key, _measure_facade._KEEPWARM_INPUT_USD_PER_TOKEN["sonnet"])
    premium = _measure_facade._KEEPWARM_WRITE_PREMIUM.get(ttl_kind, _measure_facade._KEEPWARM_WRITE_PREMIUM["unknown"])
    try:
        prefix = max(0, int(prefix_tokens))
    except (TypeError, ValueError):
        prefix = 0
    write_rate = input_rate * premium
    return prefix * write_rate * _measure_facade._keepwarm_p_resume(elapsed_seconds)


def _keepwarm_ping_cost_est(prefix_tokens, model):
    """Estimated USD cost of ONE ping (reads the full prefix as a cache hit).

    The ping resends the prefix; it is billed at the cache_read rate plus a tiny
    write refresh we approximate as a fraction of the read. Conservative (slightly
    high) so break-even errs toward stopping. Pure.
    """
    _measure_facade = _compat.facade()
    key = _measure_facade._keepwarm_model_norm(model)
    read_rate = _measure_facade._KEEPWARM_CACHE_READ_USD_PER_TOKEN.get(
        key, _measure_facade._KEEPWARM_CACHE_READ_USD_PER_TOKEN["sonnet"])
    try:
        prefix = max(0, int(prefix_tokens))
    except (TypeError, ValueError):
        prefix = 0
    # Reserve 1,000 fresh input tokens for the ping's uncached overhead.
    input_rate = _measure_facade._KEEPWARM_INPUT_USD_PER_TOKEN[key]
    return prefix * read_rate * 1.05 + 1000 * input_rate


def _keepwarm_cadence_for(ttl_kind):
    """Probe cadence (seconds between activity/ping and the next ping) by TTL."""
    _measure_facade = _compat.facade()
    if ttl_kind == "1h":
        return _measure_facade._KEEPWARM_1H_CADENCE
    # 5m and 'unknown' (treated as 5m for caution) share the 5m cadence; whether
    # a 5m ping fires at all is decided by the break-even formula, not here.
    return _measure_facade._KEEPWARM_5M_CADENCE


def keepwarm_policy_decision(record, now, tripwire_state=None, history_stats=None):
    """Apply the value, expiry, failed-ping and three-attempt limits."""
    _measure_facade = _compat.facade(); from . import keepwarm_economics
    if not isinstance(record, dict):
        return {"action": "stop", "reason": "bad-record", "phase": "probe"}

    # --- elapsed & prefix ---------------------------------------------------
    try:
        arm_ts = float(record.get("ts", 0) or 0)
    except (TypeError, ValueError):
        arm_ts = 0.0
    # Pause "start" is the last real turn if we have it, else the arm time.
    last_activity = arm_ts
    lt = record.get("last_turn_ts")
    if isinstance(lt, (int, float)):
        last_activity = float(lt)
    try:
        prefix = int(record.get("prefix_proxy", 0) or 0)
    except (TypeError, ValueError):
        prefix = 0
    model = record.get("model")
    ttl_kind = record.get("ttl_kind") or "unknown"

    pings = record.get("pings_fired")
    if not isinstance(pings, list):
        pings = []
    ping_count = len(pings)
    if record.get("ping_failed"):
        return {"action": "stop", "reason": "previous-ping-failed", "phase": "probe"}

    # --- 1. eligibility floor ----------------------------------------------
    if prefix < _measure_facade._KEEPWARM_PREFIX_FLOOR:
        return {"action": "stop", "reason": "ineligible-prefix", "phase": "probe"}
    if not model or _measure_facade._normalize_model_name(model) is None:
        return {"action": "stop", "reason": "ineligible-model", "phase": "probe"}

    elapsed = max(0.0, float(now) - arm_ts)

    # Bound both the pause duration and the number of billable refreshes.
    if elapsed >= 3 * 3600 or ping_count >= 3:
        return {"action": "stop", "reason": "hard-cap", "phase": "probe"}
    phase = "probe"
    key = _measure_facade._keepwarm_model_norm(model)
    input_rate = _measure_facade._KEEPWARM_INPUT_USD_PER_TOKEN[key]
    read_rate = _measure_facade._KEEPWARM_CACHE_READ_USD_PER_TOKEN[key]
    premium = _measure_facade._KEEPWARM_WRITE_PREMIUM.get(ttl_kind, _measure_facade._KEEPWARM_WRITE_PREMIUM["unknown"])
    avoided_reload = prefix * max(0.0, input_rate * premium - read_rate)
    ping_cost = _measure_facade._keepwarm_ping_cost_est(prefix, model)
    if ping_cost <= 0 or avoided_reload < 10 * ping_cost:
        return {"action": "stop", "reason": "below-tenfold", "phase": phase}

    # --- 5. cadence gate ----------------------------------------------------
    cadence = _measure_facade._keepwarm_cadence_for(ttl_kind)
    if ping_count == 0:
        # First probe fires once the pause has lasted one cadence since the last
        # real activity (TTL-minus-margin) -- not before.
        since = float(now) - last_activity
    else:
        try:
            last_ping = float(pings[-1])
        except (TypeError, ValueError):
            last_ping = last_activity
        since = float(now) - last_ping
    if since >= (3600 if ttl_kind == "1h" else 300):
        return {"action": "stop", "reason": "cache-expired", "phase": phase}
    if since < cadence:
        return {"action": "wait", "reason": "cadence-not-due", "phase": phase}

    return keepwarm_economics.decision(history_stats, now, max(0.0, float(now) - last_activity), ttl_kind, since, avoided_reload, ping_cost, phase)


def _keepwarm_ledger_path():
    _measure_facade = _compat.facade()
    return _measure_facade.SNAPSHOT_DIR / _measure_facade._KEEPWARM_LEDGER_NAME


def _keepwarm_ledger_append(row):
    """Atomically append one 0600 row to the keep-warm ledger. Never raises.

    Shares the arm-writer's 0600-before-first-append discipline via
    _keepwarm_append_line. Used for BOTH the pre-fire 'firing' spend row
    and the post-fire outcome row.
    """
    _measure_facade = _compat.facade()
    try:
        line = _measure_facade.json.dumps(row) + "\n"
    except (TypeError, ValueError):
        return False
    return _measure_facade._keepwarm_append_line(_measure_facade._keepwarm_ledger_path(), line)


def _keepwarm_scrub_error_reason(stderr, rc, max_len=None):
    """Bounded, secret/path-scrubbed error reason for an rc!=0 ping outcome row.

    The ping prompt is a fixed "Reply with exactly: ok", so stderr never carries
    user content -- but it may carry absolute paths or tokens from the CLI, so we
    strip anything path-like and cap the length. Falls back to 'rc-N' when stderr
    is empty. Pure; never raises.
    """
    _measure_facade = _compat.facade()
    try:
        s = (stderr or "").strip().replace("\n", " ").replace("\r", " ")
        # Drop absolute paths (keep basenames) and anything resembling a token.
        s = _measure_facade.re.sub(r"(/[^\s]+/)([^\s/]+)", r"\2", s)
        s = _measure_facade.re.sub(r"sk-[A-Za-z0-9_-]{6,}", "<redacted>", s)
        s = _measure_facade.re.sub(r"\s+", " ", s).strip()
        if not s:
            return "rc-%s" % rc
        snippet = s[:max_len]
        return "rc-%s: %s" % (rc, snippet)
    except Exception:
        return "rc-%s" % rc


def _keepwarm_acquire_ping_lock(session_id, now=None):
    """Per-session pidfile-style exclusive lock (O_CREAT|O_EXCL), NOT flock.

    flock is released when the tick PROCESS exits, which is exactly the wrong
    lifetime here: a ping that outlives one tick must still exclude the next
    tick's ping for the same session. A pidfile excludes ACROSS tick invocations.
    Stale locks (owning PID gone AND old mtime) are reclaimed. Returns the lock
    path on success or None if another live ping holds it.
    """
    _measure_facade = _compat.facade()
    if now is None:
        now = _measure_facade.time.time()
    safe = _measure_facade.re.sub(r"[^A-Za-z0-9_.-]", "_", str(session_id))[:120] or "unknown"
    lock_path = _measure_facade.SNAPSHOT_DIR / (_measure_facade._KEEPWARM_PING_LOCK_PREFIX + safe + ".lock")
    try:
        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    def _try_create():
        _measure_facade = _compat.facade()
        try:
            fd = _measure_facade.os.open(str(lock_path), _measure_facade.os.O_CREAT | _measure_facade.os.O_EXCL | _measure_facade.os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        except OSError:
            return False
        try:
            _measure_facade.os.write(fd, str(_measure_facade.os.getpid()).encode("ascii"))
        except OSError:
            pass
        finally:
            _measure_facade.os.close(fd)
        return True

    if _try_create():
        return lock_path
    # Lock exists. Reclaim if stale (dead owner or old mtime).
    try:
        st = lock_path.stat()
        age = now - st.st_mtime
        owner_pid = None
        try:
            owner_pid = int(lock_path.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            owner_pid = None
        alive = False
        if owner_pid and owner_pid > 0:
            alive = _measure_facade._pid_is_running(owner_pid)
        if not alive and age > _measure_facade._KEEPWARM_STALE_LOCK_SECONDS:
            try:
                _measure_facade.os.unlink(str(lock_path))
            except OSError:
                return None
            if _try_create():
                return lock_path
    except OSError:
        return None
    return None


def _keepwarm_release_ping_lock(lock_path):
    """Best-effort remove a per-session ping lock. Never raises."""
    _measure_facade = _compat.facade()
    if not lock_path:
        return
    try:
        _measure_facade.os.unlink(str(lock_path))
    except OSError:
        pass


def _keepwarm_cwd_is_contained(resolved):
    """True iff a realpath'd dir is a plausible project dir we may cd into (M2).

    Containment roots: the user's HOME and the private temp roots (TMPDIR,
    /tmp, /private/var/folders on macOS). A transcript-supplied cwd that resolves
    OUTSIDE these (e.g. /etc, /, an attacker symlink target) is rejected so a
    poisoned transcript cannot redirect the ping's working dir into a directory
    carrying a hostile project-scoped .claude/ config.
    """
    _measure_facade = _compat.facade()
    try:
        rp = _measure_facade.os.path.realpath(str(resolved))
    except OSError:
        return False
    roots = []
    try:
        roots.append(_measure_facade.os.path.realpath(str(_measure_facade.Path.home())))
    except OSError:
        pass
    for env_key in ("TMPDIR",):
        val = _measure_facade.os.environ.get(env_key)
        if val:
            try:
                roots.append(_measure_facade.os.path.realpath(val))
            except OSError:
                pass
    for base in ("/tmp", "/private/tmp", "/private/var/folders", "/var/folders"):
        try:
            roots.append(_measure_facade.os.path.realpath(base))
        except OSError:
            pass
    for root in roots:
        if not root:
            continue
        if rp == root or rp.startswith(root + _measure_facade.os.sep):
            return True
    return False


def _keepwarm_extract_cwd(transcript_path):
    """Return the session's ORIGINAL project cwd from the transcript, or None.

    Sessions resolve per project dir, so the ping MUST run with cwd = the
    session's original cwd (verified). Transcript event records carry a 'cwd'
    field; we scan the tail for the most recent non-empty one.

    The cwd is attacker-influenceable (the transcript is a same-UID-writable
    JSONL file), so it is hardened (security M2): the value is
    rejected unless it contains no '..' segment, realpath-resolves (following
    symlinks) to an EXISTING directory, and that resolved dir is contained under
    HOME or a private temp root. Any failure -> None (caller skips; never guess).
    """
    _measure_facade = _compat.facade()
    lines = _measure_facade._keepwarm_read_tail(transcript_path)
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = _measure_facade.json.loads(line)
        except (_measure_facade.json.JSONDecodeError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        cwd = rec.get("cwd")
        if not (isinstance(cwd, str) and cwd.strip()):
            continue
        # Reject path-traversal segments outright (defense in depth before realpath).
        if ".." in _measure_facade.Path(cwd).parts:
            return None
        try:
            resolved = _measure_facade.os.path.realpath(cwd)
            if not _measure_facade.Path(resolved).is_dir():
                return None
        except OSError:
            return None
        if not _measure_facade._keepwarm_cwd_is_contained(resolved):
            return None
        return resolved
    return None


def _keepwarm_valid_arg(value):
    """True iff `value` is a safe post-flag subprocess arg (no leading dash, ^[A-Za-z0-9._][A-Za-z0-9._-]{0,127}$)."""
    _measure_facade = _compat.facade()
    return isinstance(value, str) and bool(_measure_facade._KEEPWARM_ARG_RE.match(value))


def _keepwarm_build_ping_cmd(session_id, model, claude_bin=None,
                             budget_usd=None):
    """Build a forked print command; execution remains disabled pending proof.

    `claude_bin` is the fire-time-resolved absolute path; callers pass
    the resolved binary so launchd's empty PATH cannot break the spawn.
    """
    _measure_facade = _compat.facade()
    return [
        claude_bin,
        "--resume", str(session_id), "--fork-session",
        "-p", _measure_facade._KEEPWARM_PING_PROMPT,
        "--no-session-persistence",
        "--model", str(model),
        "--max-budget-usd", str(budget_usd),
        "--output-format", "json",
    ]


def _keepwarm_parse_ping_json(stdout):
    """Pull (cost, cache_read, cache_write, session_id) from a ping's JSON output.

    Tolerant of extra fields and of the usage living either at top level or
    nested under 'usage'/'cache_creation'. Returns a dict of parsed numbers
    (zeros on absence). Never raises.
    """
    _measure_facade = _compat.facade()
    out = {"cost_usd": 0.0, "cache_read": 0, "cache_write": 0, "session_id": None}
    try:
        data = _measure_facade.json.loads(stdout)
    except (_measure_facade.json.JSONDecodeError, ValueError, TypeError):
        return out
    if not isinstance(data, dict):
        return out
    out["session_id"] = data.get("session_id")
    try:
        out["cost_usd"] = float(data.get("total_cost_usd", 0) or 0)
    except (TypeError, ValueError):
        out["cost_usd"] = 0.0
    usage = data.get("usage")
    if isinstance(usage, dict):
        try:
            out["cache_read"] = int(usage.get("cache_read_input_tokens", 0) or 0)
        except (TypeError, ValueError):
            out["cache_read"] = 0
        try:
            out["cache_write"] = int(usage.get("cache_creation_input_tokens", 0) or 0)
        except (TypeError, ValueError):
            out["cache_write"] = 0
        if out["cache_write"] == 0:
            nested = usage.get("cache_creation")
            if isinstance(nested, dict):
                try:
                    out["cache_write"] = int(
                        (nested.get("ephemeral_1h_input_tokens", 0) or 0)
                        + (nested.get("ephemeral_5m_input_tokens", 0) or 0))
                except (TypeError, ValueError):
                    out["cache_write"] = 0
    return out


def _keepwarm_fire_ping(record, now=None, runner=None):
    """Keep all ping execution paths disabled until the safety proof is complete."""
    return {"fired": False, "status": "skip", "reason": "live-resume-unverified", "session_id": record.get("session_id")}


def _keepwarm_emit_tick_observability(now, allowed, reason, armed, fired,
                                      skipped, booked, errors):
    """Write ONE structured tick line to the scheduler log + the freshness stamp.

    Called on every REAL (non-dry) tick, including gate-refused ones, so the field
    can always answer 'did the scheduler run / why didn't it ping / is it
    erroring'. Best-effort; never raises.
    """
    _measure_facade = _compat.facade()
    payload = {
        "ts": float(now),
        "event": "tick",
        "allowed": bool(allowed),
        "reason": reason,
        "armed": int(armed),
        "fired": int(fired),
        "skipped": int(skipped),
        "booked": int(booked),
        "errors": int(errors),
    }
    try:
        _measure_facade._keepwarm_scheduler_log_append(payload)
    except Exception:
        pass
    try:
        _measure_facade._keepwarm_write_tick_stamp(payload, now=now)
    except Exception:
        pass


def _keepwarm_tick_singleton():
    """Yield True iff this process holds the whole-tick single-instance lock.

    Pidfile-style (O_CREAT|O_EXCL) so a hung/long tick blocks the next scheduled
    tick rather than overlapping it. Stale locks (dead owner + old mtime) are
    reclaimed. Yields False (caller exits quietly) when another tick is live.
    """
    _measure_facade = _compat.facade()
    held = False
    lock_path = _measure_facade.SNAPSHOT_DIR / _measure_facade._KEEPWARM_TICK_LOCK_NAME
    fd = None
    try:
        try:
            _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            yield False
            return
        try:
            fd = _measure_facade.os.open(str(lock_path), _measure_facade.os.O_CREAT | _measure_facade.os.O_EXCL | _measure_facade.os.O_WRONLY, 0o600)
        except FileExistsError:
            # Reclaim if stale.
            now = _measure_facade.time.time()
            try:
                st = lock_path.stat()
                owner = None
                try:
                    owner = int(lock_path.read_text(encoding="utf-8").strip() or 0)
                except (OSError, ValueError):
                    owner = None
                alive = False
                if owner and owner > 0:
                    alive = _measure_facade._pid_is_running(owner)
                if not alive and (now - st.st_mtime) > _measure_facade._KEEPWARM_STALE_LOCK_SECONDS:
                    try:
                        _measure_facade.os.unlink(str(lock_path))
                        fd = _measure_facade.os.open(str(lock_path),
                                     _measure_facade.os.O_CREAT | _measure_facade.os.O_EXCL | _measure_facade.os.O_WRONLY, 0o600)
                    except OSError:
                        yield False
                        return
                else:
                    yield False
                    return
            except OSError:
                yield False
                return
        except OSError:
            yield False
            return
        try:
            _measure_facade.os.write(fd, str(_measure_facade.os.getpid()).encode("ascii"))
        except OSError:
            pass
        held = True
        yield True
    finally:
        if fd is not None:
            try:
                _measure_facade.os.close(fd)
            except OSError:
                pass
        if held:
            try:
                _measure_facade.os.unlink(str(lock_path))
            except OSError:
                pass


def keepwarm_tick(now=None, dry_run=None, env=None, runner=None,
                  gate=None, tripwire_state=None, history_stats=None):
    """One keep-warm tick: gate -> load -> classify -> decide -> fire.

    Order (every step fail-closed; one record's failure never blocks the rest):
      1. keepwarm_gate() FIRST -- if not allowed (subscription / not consented /
         ping-process), return immediately with the gate reason; NOTHING fires.
      2. Single-instance tick lock -- if another tick is live, skip quietly.
      3a. keepwarm_detect_realized() -- BEFORE compaction, because
          compaction drops resumed records. Books realized/loss rows off the
          resumed turn's REAL usage, never off the 'resumed' classification.
      3b. evaluate_keepwarm_tripwire() -- refresh the rolling-7d
          ratio + sticky demotion ladder from the ledger.
      3c. compact_keepwarm_sidecar() -- drop resumed/gone/expired records (the
          disarm path). The tick writes ONLY ping spend + outcome rows here.
      4. For each surviving still_paused record: policy decision; on 'ping' fire
         (unless dry_run) and append the ping ts to the record's pings_fired,
         then rewrite the sidecar with the updated records.

    Returns a summary dict {allowed, reason, decisions:[...], fired:int,
    skipped:int}. dry_run prints/returns decisions without firing anything.
    `gate`/`runner` are injectable for tests; defaults use the real gate and
    subprocess.
    """
    _measure_facade = _compat.facade(); from . import keepwarm_economics
    if now is None:
        now = _measure_facade.time.time()
    if env is None:
        env = _measure_facade.os.environ
    if gate is None:
        gate = _measure_facade.keepwarm_gate
    summary = {"allowed": False, "reason": "", "decisions": [],
               "fired": 0, "skipped": 0}

    # Stage-error counter for the per-tick observability line (FIX-A6a). A
    # gate-refused tick is NOT an error; only thrown stages count.
    stage_errors = 0
    booked_realized = 0

    allowed, reason = gate(env=env)
    summary["reason"] = reason; keepwarm_economics.reconcile_while_paused(_measure_facade, reason, now, dry_run)
    if not allowed:
        # Gate refusal is the silent-exit path (subscription / unconsented /
        # ping-process). Nothing loads, nothing fires. Still record ONE tick line
        # + stamp so 'why didn't it ping' is answerable and freshness is tracked.
        if not dry_run:
            _measure_facade._keepwarm_emit_tick_observability(
                now=now, allowed=False, reason=reason, armed=0, fired=0,
                skipped=0, booked=0, errors=0)
        return summary
    summary["allowed"] = True

    with _measure_facade._keepwarm_tick_singleton() as held:
        if not held:
            summary["reason"] = "tick-already-running"
            return summary

        # 3a. REALIZED detection BEFORE compaction: compaction drops
        # resumed records, so the only-place-savings-are-booked step must run on
        # the still-present resumed records first. Books a realized savings row
        # (warm resume) or a ledger loss row (cold resume), idempotent per pause;
        # a resume the transcript can't prove warm books NOTHING.
        try:
            det = _measure_facade.keepwarm_detect_realized(now=now)
            booked_realized = int(det.get("realized", 0) or 0)
            stage_errors += int(det.get("errors", 0) or 0)
        except Exception:
            stage_errors += 1

        # 3b. Tripwire evaluation: refresh the rolling-7d ratio +
        # demotion ladder from the ledger so the gate's tripwire-off ceiling and
        # the dashboard ratio stay current. Sticky + fail-closed; never raises.
        try:
            _measure_facade.evaluate_keepwarm_tripwire(now=now)
        except Exception:
            stage_errors += 1

        # 3c. Disarm pass (drops resumed/gone/expired). Realized already booked.
        try:
            _measure_facade.compact_keepwarm_sidecar(now=now)
        except Exception:
            stage_errors += 1

        records = _measure_facade.load_keepwarm_records(); history_stats = keepwarm_economics.load_history(_measure_facade, history_stats, records)
        ledger_rows = _measure_facade._keepwarm_load_ledger_rows()
        from . import keepwarm_safety
        changed = False
        for rec in records:
            changed = keepwarm_safety.reconcile_attempts(rec, ledger_rows) or changed
            sid = rec.get("session_id")
            try:
                verdict = _measure_facade.classify_keepwarm_record(rec, now=now)
            except Exception:
                verdict = "gone"
            if verdict != "still_paused":
                # Resumed/gone are handled by compaction; never fire, never book.
                summary["decisions"].append(
                    {"session_id": sid, "action": "stop",
                     "reason": "not-paused-%s" % verdict, "phase": "probe"})
                continue
            try:
                decision = _measure_facade.keepwarm_policy_decision(
                    rec, now, tripwire_state=tripwire_state,
                    history_stats=history_stats)
            except Exception:
                decision = {"action": "stop", "reason": "policy-error",
                            "phase": "probe"}
            entry = {"session_id": sid, "action": decision.get("action"),
                     "reason": decision.get("reason"),
                     "phase": decision.get("phase")}
            summary["decisions"].append(entry)

            if decision.get("action") != "ping":
                continue
            if dry_run:
                # Dry-run fires nothing; the decision is still reported.
                continue
            # Stamp the phase so the executor's ledger rows carry it.
            rec["_phase"] = decision.get("phase", "probe")
            try:
                fire = _measure_facade._keepwarm_fire_ping(rec, now=now, runner=runner)
            except Exception:
                fire = {"fired": False, "status": "error", "reason": "fire-error"}
                stage_errors += 1
            rec.pop("_phase", None)
            entry["fire_status"] = fire.get("status")
            entry["fire_reason"] = fire.get("reason")
            if fire.get("fired"):
                summary["fired"] += 1
                pings = rec.get("pings_fired")
                if not isinstance(pings, list):
                    pings = []
                pings.append(float(now))
                rec["pings_fired"] = pings
                if (fire.get("status") != "ok" or int(fire.get("cache_read") or 0) < int(rec.get("prefix_proxy") or 0) * 0.5
                        or int(fire.get("cache_write") or 0) >= int(rec.get("prefix_proxy") or 0) * 0.5):
                    rec["ping_failed"] = True
                changed = True
            else:
                summary["skipped"] += 1

        # Persist updated ping counters (extends the record dict;
        # _valid_keepwarm_record tolerates extra keys).
        if changed:
            try:
                _measure_facade._keepwarm_rewrite_records(records, now=now)
            except Exception:
                stage_errors += 1

    # One structured observability line + freshness stamp per REAL tick (even
    # under --quiet), so the field can answer 'did it run / why didn't it ping /
    # is it erroring' (FIX-A6a/c). dry-run logs nothing (no real run happened).
    if not dry_run:
        _measure_facade._keepwarm_emit_tick_observability(
            now=now, allowed=True, reason=summary.get("reason", ""),
            armed=len(summary.get("decisions", [])),
            fired=summary.get("fired", 0), skipped=summary.get("skipped", 0),
            booked=booked_realized, errors=stage_errors)
    summary["errors"] = stage_errors
    return summary


def _keepwarm_rewrite_records(records, now=None):
    """Atomically rewrite the whole sidecar with `records` (0600, under flock).

    Used by the tick to persist appended pings_fired entries. Mirrors
    compact_keepwarm_sidecar's atomic rewrite. Never raises.
    """
    _measure_facade = _compat.facade()
    if now is None:
        now = _measure_facade.time.time()
    path = _measure_facade._keepwarm_sidecar_path()
    try:
        with _measure_facade._keepwarm_lock() as acquired:
            if not acquired:
                return
            try:
                _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = _measure_facade.tempfile.mkstemp(
                    prefix=".keepwarm_state.", suffix=".tmp", dir=str(_measure_facade.SNAPSHOT_DIR))
                try:
                    if hasattr(_measure_facade.os, "fchmod"):
                        _measure_facade.os.fchmod(fd, 0o600)
                    with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as fh:
                        for rec in records:
                            if _measure_facade._valid_keepwarm_record(rec):
                                fh.write(_measure_facade.json.dumps(rec) + "\n")
                    _measure_facade.os.replace(tmp_name, str(path))
                except Exception:
                    try:
                        _measure_facade.os.unlink(tmp_name)
                    except OSError:
                        pass
                    raise
            except OSError:
                pass
    except Exception:
        pass


def keepwarm_resume_predictor(features):
    """PURE resume predictor. Score implementable, ping-time-computable features.

    `features` is a plain dict (injected by the caller — the backfill replay or
    the live tick); this function never reads the filesystem or the clock, so it
    cannot see the future and the oracle-leakage guard is structural.

    Recognized feature keys (all OPTIONAL; missing -> neutral/zero):
      prior_resume_count : int   -- how many >1h-gap resumes THIS session already
                                    had before the ping (from its own transcript
                                    tail / the watchdog gap history). Capped to a
                                    small saturating contribution.
      sibling_active     : bool  -- any OTHER transcript in the same project dir
                                    modified within the activity window ENDING at
                                    the ping time.
      machine_active     : bool  -- any transcript under ~/.claude/projects
                                    modified within the activity window ending at
                                    the ping time.
      survival_p         : float -- P(resume | still paused at elapsed) from the
                                    local survival table, evaluated at the ping's
                                    elapsed time. The caller supplies it (it is a
                                    function of elapsed, a ping-time quantity).

    Returns a dict (transparency: the dashboard renders WHY):
      {predictor_continue: bool, score: float, threshold: float, features: {...}}
    where `features` echoes the (sanitized) named inputs that drove the score.
    """
    _measure_facade = _compat.facade()
    f = features if isinstance(features, dict) else {}

    try:
        prior = int(f.get("prior_resume_count", 0) or 0)
    except (TypeError, ValueError):
        prior = 0
    prior = max(0, prior)
    # Saturating: 1+ prior resumes already signals a resumer; 2+ does not keep
    # paying. The weight is the full contribution at >=2.
    prior_term = _measure_facade._KEEPWARM_PRED_W_PRIOR_RESUMES * min(1.0, prior / 2.0)

    sibling = bool(f.get("sibling_active", False))
    machine = bool(f.get("machine_active", False))
    sibling_term = _measure_facade._KEEPWARM_PRED_W_SIBLING if sibling else 0.0
    machine_term = _measure_facade._KEEPWARM_PRED_W_MACHINE if machine else 0.0

    try:
        survival_p = float(f.get("survival_p", 0.0) or 0.0)
    except (TypeError, ValueError):
        survival_p = 0.0
    survival_p = max(0.0, min(1.0, survival_p))
    survival_term = _measure_facade._KEEPWARM_PRED_W_SURVIVAL * survival_p

    score = prior_term + sibling_term + machine_term + survival_term
    cont = score >= _measure_facade._KEEPWARM_PRED_THRESHOLD
    return {
        "predictor_continue": bool(cont),
        "score": round(score, 6),
        "threshold": _measure_facade._KEEPWARM_PRED_THRESHOLD,
        "features": {
            "prior_resume_count": prior,
            "sibling_active": sibling,
            "machine_active": machine,
            "survival_p": round(survival_p, 6),
        },
    }


def _keepwarm_reconstruct_session_events(turns, now):
    """Turn one session's parsed turns into pause events + a tail, for replay.

    Pure (turns + now in; plain data out). Mirrors _detect_explicit_ttl's gap +
    re-write criterion so the replayed economics reconcile with the watchdog.

    Returns {"events": [...], "tail": {...} | None, "resume_times": [...]} where
    each event is:
      {pause_start_ts, resume_ts, elapsed (resume-pause), prefix_proxy, model,
       ttl_kind, rewrote (bool), is_resume (always True here)}
    and `resume_times` is the sorted list of this session's own resume epoch
    seconds (used to count a session's PRIOR >1h resumes at any ping time).
    The tail (last turn, never resumed in-window) is:
      {pause_start_ts, prefix_proxy, model, ttl_kind}.
    """
    _measure_facade = _compat.facade()
    parsed = []
    for t in turns:
        epoch = _measure_facade._keepwarm_iso_to_epoch(t.get("timestamp"))
        if epoch is None:
            continue
        parsed.append((epoch, t))
    if not parsed:
        return {"events": [], "tail": None, "resume_times": []}

    model = _measure_facade._dominant_turn_model(turns)
    ttl_kind = "1h" if _measure_facade._resolve_cache_profile(
        model, runtime="claude")[0] == "claude_code" else "unknown"
    boundary = _measure_facade._KEEPWARM_1H_WINDOW_SECONDS  # claude_code 1h boundary

    events = []
    resume_times = []
    for i in range(1, len(parsed)):
        prev_epoch, prev_turn = parsed[i - 1]
        cur_epoch, cur_turn = parsed[i]
        gap = cur_epoch - prev_epoch
        if gap <= boundary:
            continue
        # Executor occupancy, not the advisor-summed aggregate (#287), so
        # replay agrees with the live arm path.
        prev_cached = _measure_facade._turn_prefix_cached(prev_turn)
        cc = _measure_facade._turn_resume_write(cur_turn)
        if prev_cached <= 0:
            continue
        rewrote = cc >= _measure_facade._CACHE_REWRITE_PREFIX_FRACTION * prev_cached
        resume_times.append(cur_epoch)
        events.append({
            "pause_start_ts": prev_epoch,
            "resume_ts": cur_epoch,
            "elapsed": gap,
            "prefix_proxy": int(prev_cached),
            "model": model,
            "ttl_kind": ttl_kind,
            "rewrote": bool(rewrote),
            "is_resume": True,
        })

    last_epoch, last_turn = parsed[-1]
    last_cached = _measure_facade._turn_prefix_cached(last_turn)
    tail = None
    # Only a tail that idled past the boundary AND had a cacheable prefix is a
    # ping candidate (matches the live arm eligibility floor downstream).
    if last_cached > 0 and (now - last_epoch) > boundary:
        tail = {
            "pause_start_ts": last_epoch,
            "prefix_proxy": int(last_cached),
            "model": model,
            "ttl_kind": ttl_kind,
        }
    resume_times.sort()
    return {"events": events, "tail": tail, "resume_times": resume_times}


def _keepwarm_replay_pause(pause, predictor_mode, now, project_resume_index=None,
                           machine_resume_index=None, own_resume_times=None,
                           is_tail=None):
    """Replay the LIVE policy over one pause/tail and return (pings, cost, saving).

    Drives keepwarm_policy_decision step by step exactly as the tick would, in
    cadence-sized time steps, building the pings_fired list as the policy fires.
    Three predictor_mode values:
      "probe"   : predictor_continue always False  (probe-only floor).
      "predictor": real keepwarm_resume_predictor on reconstructed-at-ping-time
                   features.
      "oracle"  : predictor_continue always True   (hindsight upper bound).

    Honesty rules:
      * a SAVING is booked only when the pause actually resumed AND the ping
        schedule covers the gap through the resume (every cache window from pause
        start to resume_ts has a ping no older than one cadence at its start);
      * partial coverage = cost, NO saving;
      * a tail (never resumed) accrues its ping cost, never a saving.
    Returns (n_pings, ping_cost_usd, saving_usd).
    """
    _measure_facade = _compat.facade(); from . import keepwarm_economics
    record = {
        "session_id": "replay",
        "transcript_path": "/replay",
        "prefix_proxy": pause["prefix_proxy"],
        "ttl_kind": pause["ttl_kind"],
        "model": pause["model"],
        "ts": pause["pause_start_ts"],
        "last_turn_ts": pause["pause_start_ts"],
        "pings_fired": [],
    }
    cadence = _measure_facade._keepwarm_cadence_for(pause["ttl_kind"])
    resume_ts = None if is_tail else pause.get("resume_ts")
    # The replay clock runs from pause start to resume (or to a sane horizon for a
    # tail: the 12h hard cap, where the policy stops anyway).
    horizon = (resume_ts if resume_ts is not None
               else min(now, pause["pause_start_ts"] + _measure_facade._KEEPWARM_HARD_CAP_SECONDS))

    ping_ts = []
    clock = pause["pause_start_ts"]
    # Step in cadence-sized increments; on each due 'ping' fire a ping at that
    # instant (advance to a ping point exactly, like the scheduler's ~cadence
    # wakeups). Bound the loop hard.
    max_steps = int(_measure_facade._KEEPWARM_HARD_CAP_SECONDS / max(1, cadence)) + 4
    steps = 0
    while clock <= horizon and steps < max_steps:
        steps += 1
        decision = _measure_facade.keepwarm_policy_decision(
            record, clock,
            tripwire_state={"allow_sustain": True},
            history_stats=keepwarm_economics.with_gaps(_measure_facade._keepwarm_replay_history_stats(
                pause, predictor_mode, clock, project_resume_index,
                machine_resume_index, own_resume_times), pause.get("gap_sessions")))
        action = decision.get("action")
        if action == "stop":
            break
        if action == "ping":
            ping_ts.append(clock)
            record["pings_fired"] = list(ping_ts)
            clock += cadence
        else:  # wait -> jump to when the first ping becomes due
            if not ping_ts:
                since_needed = cadence - (clock - record["last_turn_ts"])
            else:
                since_needed = cadence - (clock - ping_ts[-1])
            clock += max(cadence, since_needed) if since_needed > 0 else cadence

    n_pings = len(ping_ts)
    ping_cost = n_pings * _measure_facade._keepwarm_ping_cost_est(
        pause["prefix_proxy"], pause["model"])

    saving = 0.0
    if (not is_tail) and resume_ts is not None and pause.get("rewrote") and n_pings > 0:
        if _measure_facade._keepwarm_schedule_covers(ping_ts, pause["pause_start_ts"], resume_ts,
                                     cadence):
            # Covered + resumed + would have re-written: the re-write is avoided.
            # Price it at the realized avoided write (full prefix at the write
            # premium), NOT the P(resume)-discounted expectation -- this pause DID
            # resume, so the whole write is genuinely spared.
            key = _measure_facade._keepwarm_model_norm(pause["model"])
            input_rate = _measure_facade._KEEPWARM_INPUT_USD_PER_TOKEN.get(
                key, _measure_facade._KEEPWARM_INPUT_USD_PER_TOKEN["sonnet"])
            premium = _measure_facade._KEEPWARM_WRITE_PREMIUM.get(
                pause["ttl_kind"], _measure_facade._KEEPWARM_WRITE_PREMIUM["unknown"])
            read_rate = _measure_facade._KEEPWARM_CACHE_READ_USD_PER_TOKEN.get(
                key, _measure_facade._KEEPWARM_CACHE_READ_USD_PER_TOKEN["sonnet"])
            prefix = max(0, int(pause["prefix_proxy"]))
            paid_write = prefix * input_rate * premium
            avoided_read = prefix * read_rate
            saving = max(0.0, paid_write - avoided_read)
    return n_pings, ping_cost, saving


def _keepwarm_schedule_covers(ping_ts, pause_start, resume_ts, cadence):
    """True if the ping schedule keeps the cache warm from pause_start to resume.

    Coverage = the cache never lapses: the first ping fires within one cadence of
    pause start, each subsequent ping within one cadence of the prior, and the
    last ping is within one cadence of the resume. A single gap > cadence anywhere
    means the cache expired in between -> the resume re-writes anyway -> NO saving
    (partial coverage is pure cost). Conservative against keep-warm.
    """
    _measure_facade = _compat.facade()
    if not ping_ts:
        return False
    pts = sorted(ping_ts)
    if pts[0] - pause_start > cadence:
        return False
    for a, b in zip(pts, pts[1:]):
        if b - a > cadence:
            return False
    if resume_ts - pts[-1] > cadence:
        return False
    return True


def _keepwarm_replay_history_stats(pause, predictor_mode, clock,
                                   project_resume_index, machine_resume_index,
                                   own_resume_times):
    """Build the history_stats dict fed to the policy at one replay instant.

    For "probe" -> predictor_continue False; for "oracle" -> True; for
    "predictor" -> run keepwarm_resume_predictor on features reconstructed AS OF
    `clock` only (the oracle-leakage guard: nothing here reads resume_ts or any
    event after `clock`).
    """
    _measure_facade = _compat.facade()
    if predictor_mode == "probe":
        return {"predictor_continue": False}
    if predictor_mode == "oracle":
        return {"predictor_continue": True}

    # predictor mode: reconstruct ping-time features.
    elapsed = max(0.0, clock - pause["pause_start_ts"])
    survival_p = _measure_facade._keepwarm_p_resume(elapsed)
    # prior >1h resumes of THIS session strictly BEFORE the current pause start
    # (cannot include this pause's own resume -> no leakage).
    prior = 0
    for rt in (own_resume_times or []):
        if rt < pause["pause_start_ts"]:
            prior += 1
    win = _measure_facade._KEEPWARM_PRED_ACTIVITY_WINDOW_SECONDS
    sibling = _measure_facade._keepwarm_index_active(
        project_resume_index, clock, win, exclude_ts=pause["pause_start_ts"])
    machine = _measure_facade._keepwarm_index_active(
        machine_resume_index, clock, win, exclude_ts=None)
    return _measure_facade.keepwarm_resume_predictor({
        "prior_resume_count": prior,
        "sibling_active": sibling,
        "machine_active": machine,
        "survival_p": survival_p,
    })


def _keepwarm_index_active(index, clock, window, exclude_ts=None):
    """True if `index` has any activity timestamp in (clock-window, clock).

    `index` is a SORTED list of activity epoch seconds (sibling turns in the
    project, or all machine turns). STRICTLY before `clock` (no future leakage)
    and within `window` of it. `exclude_ts` drops the pause's own activity so a
    session is never counted as its own sibling/machine signal.

    Bisect over the sorted index: O(log n) instead of the prior
    linear scan, which made backfill ~O(n^2) when sessions cluster in one project.
    """
    _measure_facade = _compat.facade()
    if not index:
        return False
    lo = clock - window
    # Candidate slice = index entries strictly in (lo, clock).
    left = _measure_facade.bisect.bisect_right(index, lo)      # first entry > lo
    right = _measure_facade.bisect.bisect_left(index, clock)   # first entry >= clock
    if left >= right:
        return False
    if exclude_ts is None:
        return True
    # Some entry in the slice exists; True unless the ONLY entry is the excluded
    # one (within 1s). Cheap to confirm: scan just the (tiny in practice) window
    # of entries near exclude_ts within the slice.
    for i in range(left, right):
        if abs(index[i] - exclude_ts) >= 1.0:
            return True
    return False


def keepwarm_backfill(days=None, now=None, files=None, parse_fn=None):
    """Replay the LIVE keep-warm policy over the last `days` of real history.

    Reuses the watchdog's session source (_find_all_jsonl_files + the SAME
    parse_session_turns the detector uses) so the replayed economics reconcile
    with `_get_cache_ttl_waste`. Pure-ish: `files`/`parse_fn`/`now` are injectable
    for deterministic tests (a fixed fixture replays identically).

    Three modes:
      probe-only       : predictor always False (the floor; measured +$13).
      predictor-sustain: real predictor on reconstructed-at-ping-time features.
      oracle-sustain   : hindsight upper bound (+$119).

    Honesty: savings booked ONLY for resumed + covered pauses; tails accrue probe
    cost. Returns a dict with per-mode {pings, ping_cost_usd, avoided_writes,
    saving_usd, net_usd} plus reconciliation diagnostics.
    """
    _measure_facade = _compat.facade(); from . import keepwarm_economics
    if now is None:
        now = _measure_facade.time.time()
    if files is None:
        try:
            files = [fp for fp, _m, _p in _measure_facade._find_all_jsonl_files(days)]
        except Exception:
            files = []
    if parse_fn is None:
        parse_fn = _measure_facade.parse_session_turns

    # First pass: reconstruct every session's events + tail, and a machine-wide
    # index of all resume timestamps (for the machine-activity feature).
    sessions = []; gap_sessions = []  # list of (project_key, recon)
    machine_resume_index = []
    project_index = {}  # project_key -> sorted resume ts list
    for fp in files:
        try:
            turns = parse_fn(fp); gap_sessions.append([t for t in (_measure_facade._keepwarm_iso_to_epoch(turn.get('timestamp')) for turn in turns) if t is not None])
        except Exception:
            continue
        if not turns:
            continue
        recon = _measure_facade._keepwarm_reconstruct_session_events(turns, now)
        if not recon["events"] and not recon["tail"]:
            continue
        try:
            project_key = _measure_facade.Path(fp).parent.name
        except Exception:
            project_key = ""
        sessions.append((project_key, recon))
        for rt in recon["resume_times"]:
            machine_resume_index.append(rt)
            project_index.setdefault(project_key, []).append(rt)
    machine_resume_index.sort(); gap_sessions = keepwarm_economics.GapHistory(gap_sessions)
    for k in project_index:
        project_index[k].sort()

    modes = {
        "probe-only": "probe",
        "predictor-sustain": "predictor",
        "oracle-sustain": "oracle",
    }
    out = {m: {"pings": 0, "ping_cost_usd": 0.0, "tail_cost_usd": 0.0, "avoided_writes": 0,
               "tokens_saved": 0, "saving_usd": 0.0, "net_usd": 0.0}
           for m in modes}

    for project_key, recon in sessions:
        own_resume_times = recon["resume_times"]
        proj_idx = project_index.get(project_key, [])
        for label, mode in modes.items():
            agg = out[label]
            for ev in recon["events"]:
                # Eligibility floor mirrors the live policy (the policy enforces
                # it too, but skipping here keeps tails/events symmetric).
                if int(ev["prefix_proxy"]) < _measure_facade._KEEPWARM_PREFIX_FLOOR:
                    continue
                n, cost, saving = _measure_facade._keepwarm_replay_pause(
                    dict(ev, gap_sessions=gap_sessions), mode, now, project_resume_index=proj_idx,
                    machine_resume_index=machine_resume_index,
                    own_resume_times=own_resume_times, is_tail=False)
                agg["pings"] += n
                agg["ping_cost_usd"] += cost
                agg["saving_usd"] += saving
                if saving > 0:
                    agg["avoided_writes"] += 1
                    # The avoided cold re-write would have re-sent the whole
                    # prefix as cache-WRITE tokens; that token volume is the
                    # tangible "tokens saved" number users see in the forecast.
                    agg["tokens_saved"] += int(ev["prefix_proxy"])
            if recon["tail"]:
                if int(recon["tail"]["prefix_proxy"]) >= _measure_facade._KEEPWARM_PREFIX_FLOOR:
                    n, cost, _ = _measure_facade._keepwarm_replay_pause(
                        dict(recon["tail"], gap_sessions=gap_sessions), mode, now, project_resume_index=proj_idx,
                        machine_resume_index=machine_resume_index,
                        own_resume_times=own_resume_times, is_tail=True)
                    agg["pings"] += n
                    agg["ping_cost_usd"] += cost; agg["tail_cost_usd"] += cost

    for label in out:
        agg = out[label]
        agg["net_usd"] = round(agg["saving_usd"] - agg["ping_cost_usd"], 4)
        agg["saving_usd"] = round(agg["saving_usd"], 4)
        agg["ping_cost_usd"] = round(agg["ping_cost_usd"], 4); agg["tail_cost_usd"] = round(agg["tail_cost_usd"], 4)

    return {
        "period_days": days,
        "scanned_sessions": len(sessions),
        "modes": out,
    }


def _keepwarm_promotion_path():
    _measure_facade = _compat.facade()
    return _measure_facade.SNAPSHOT_DIR / _measure_facade._KEEPWARM_PROMOTION_NAME


def keepwarm_backfill_reconcile(result):
    """Compare a backfill result to the expected reconciliation bands. Returns a diag dict.

    Never raises. {probe_net, oracle_net, probe_in_band, oracle_in_band, notes}.
    A False band flag is a "investigate before trusting the fence" signal, not a
    hard error (the caller reports it either way per the honesty rule).
    """
    _measure_facade = _compat.facade()
    try:
        modes = result.get("modes", {})
        probe_net = float(modes.get("probe-only", {}).get("net_usd", 0.0))
        oracle_net = float(modes.get("oracle-sustain", {}).get("net_usd", 0.0))
    except (TypeError, ValueError, AttributeError):
        return {"probe_net": 0.0, "oracle_net": 0.0, "probe_in_band": False,
                "oracle_in_band": False, "notes": "unparseable result"}
    probe_in_band = abs(probe_net - _measure_facade._KEEPWARM_RECON_PROBE_TARGET) <= _measure_facade._KEEPWARM_RECON_PROBE_TOL
    oracle_in_band = (
        abs(oracle_net - _measure_facade._KEEPWARM_RECON_ORACLE_TARGET)
        <= _measure_facade._KEEPWARM_RECON_ORACLE_TARGET * _measure_facade._KEEPWARM_RECON_ORACLE_TOL_FRAC)
    return {
        "probe_net": round(probe_net, 4),
        "oracle_net": round(oracle_net, 4),
        "probe_in_band": bool(probe_in_band),
        "oracle_in_band": bool(oracle_in_band),
        "notes": ("probe within sim band" if probe_in_band else
                  "probe OUTSIDE sim band (+$13 +-$15)"),
    }


def keepwarm_promotion_decide(result):
    """Decide sustain promotion from a backfill result. Pure (no IO).

    PROMOTION FENCE: sustain is allowed ONLY when
        net(predictor-sustain) > net(probe-only) > 0
    on the real replay. Returns {sustain_allowed, reason, basis} where `basis`
    carries the three-mode nets so the sidecar/dashboard can show WHY.
    """
    _measure_facade = _compat.facade()
    modes = (result or {}).get("modes", {})

    def _net(label):
        _measure_facade = _compat.facade()
        try:
            return float(modes.get(label, {}).get("net_usd", 0.0))
        except (TypeError, ValueError):
            return 0.0

    probe_net = _net("probe-only")
    pred_net = _net("predictor-sustain")
    oracle_net = _net("oracle-sustain")
    basis = {
        "probe_net_usd": round(probe_net, 4),
        "predictor_net_usd": round(pred_net, 4),
        "oracle_net_usd": round(oracle_net, 4),
        "period_days": (result or {}).get("period_days"),
        "scanned_sessions": (result or {}).get("scanned_sessions"),
    }
    if probe_net <= 0:
        return {"sustain_allowed": False,
                "reason": "probe-only-not-positive", "basis": basis}
    if pred_net <= probe_net:
        return {"sustain_allowed": False,
                "reason": "predictor-not-better-than-probe", "basis": basis}
    return {"sustain_allowed": True, "reason": "predictor-beats-probe", "basis": basis}


def _keepwarm_write_promotion_sidecar(decision, now=None):
    """Atomically write/refresh the 0600 promotion sidecar. Never raises.

    Writes sustain_allowed + (when allowed) promoted_at + the replay basis. U7
    EXTENDS this file (rolling ratio, demotion); U6 only owns the promotion
    fields. Mirrors _write_tripwire_sidecar's mkstemp+fchmod+os.replace 0600
    discipline. A non-promoting decision still writes the file (sustain_allowed
    False) so the dashboard always has an honest current verdict.
    """
    _measure_facade = _compat.facade()
    if now is None:
        now = _measure_facade.time.time()
    path = _measure_facade._keepwarm_promotion_path()
    record = {
        "sustain_allowed": bool(decision.get("sustain_allowed", False)),
        "reason": decision.get("reason", ""),
        "basis": decision.get("basis", {}),
        "_cached_ts": now,
    }
    if record["sustain_allowed"]:
        record["promoted_at"] = now
    try:
        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = _measure_facade.tempfile.mkstemp(
            prefix=".keepwarm_promotion.", suffix=".tmp", dir=str(_measure_facade.SNAPSHOT_DIR))
        try:
            if hasattr(_measure_facade.os, "fchmod"):
                _measure_facade.os.fchmod(fd, 0o600)
            with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as fh:
                _measure_facade.json.dump(record, fh)
            _measure_facade.os.replace(tmp_name, str(path))
        except Exception:
            try:
                _measure_facade.os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception:
        pass
    return record


def keepwarm_backfill_run(days=None, now=None, write_fence=None, files=None,
                          parse_fn=None):
    """Full U6 backfill: replay -> reconcile -> fence decision -> (write sidecar).

    Returns {result, reconcile, decision, wrote_fence} so the CLI can render the
    three-mode numbers, the reconciliation verdict, and the promotion outcome.
    The fence is written ONLY when `write_fence` and the promotion gate passes
    (net(predictor) > net(probe) > 0); a failing gate still records an honest
    sustain_allowed=False sidecar so live code never reads a stale True.
    """
    _measure_facade = _compat.facade()
    if now is None:
        now = _measure_facade.time.time()
    result = _measure_facade.keepwarm_backfill(days=days, now=now, files=files, parse_fn=parse_fn)
    reconcile = _measure_facade.keepwarm_backfill_reconcile(result)
    decision = _measure_facade.keepwarm_promotion_decide(result)
    wrote = False
    if write_fence:
        # We ALWAYS persist the current honest verdict (True or False) so a prior
        # promotion cannot linger if the latest replay no longer supports it.
        _measure_facade._keepwarm_write_promotion_sidecar(decision, now=now)
        wrote = True
    return {"result": result, "reconcile": reconcile,
            "decision": decision, "wrote_fence": wrote}


def keepwarm_forecast(days=None, now=None, files=None, parse_fn=None):
    """PERSONALIZED projected savings, computed from the user's OWN history.

    The opt-in problem: nobody enables a token-SPENDING
    feature blind -- they need to see what it would save THEM before paying for
    it. This replays the conservative probe-only floor policy (the one that
    actually ships; sustain has to earn promotion separately) over the last
    `days` of real sessions and returns the pitch:

      {period_days, sessions, avoided_writes, tokens_saved, saving_usd,
       ping_cost_usd, net_usd, positive}

    saving_usd / ping_cost_usd / net_usd are API-EQUIVALENT dollars (what the
    avoided cache re-writes would cost at API token rates) -- the tangible value
    even for subscription users, who pay it in quota rather than cash.
    tokens_saved is the cache-WRITE token volume spared. READ-ONLY: runs the
    replay but NEVER writes the promotion sidecar. Never raises.
    """
    _measure_facade = _compat.facade()
    if now is None:
        now = _measure_facade.time.time()
    try:
        result = _measure_facade.keepwarm_backfill(days=days, now=now, files=files,
                                   parse_fn=parse_fn)
        probe = result.get("modes", {}).get("probe-only", {})
        saving = float(probe.get("saving_usd", 0.0) or 0.0)
        ping_cost = float(probe.get("ping_cost_usd", 0.0) or 0.0)
        net = float(probe.get("net_usd", saving - ping_cost) or 0.0)
        return {
            "period_days": result.get("period_days", days),
            "sessions": int(result.get("scanned_sessions", 0) or 0),
            "avoided_writes": int(probe.get("avoided_writes", 0) or 0),
            "tokens_saved": int(probe.get("tokens_saved", 0) or 0),
            "saving_usd": round(saving, 2),
            "ping_cost_usd": round(ping_cost, 2),
            "net_usd": round(net, 2),
            "positive": net > 0,
            "available": True,
        }
    except Exception as exc:
        return {"available": False, "error": str(exc), "period_days": days}


def _keepwarm_realized_write_usd(prefix_tokens, model):
    """USD value of ONE realized warm resume = the avoided 1h prefix re-write.

    Priced at the profile's REAL 1h write rate (input_rate * 2.0 premium) on the
    full prefix. We KNOW the pause resumed warm, so the entire re-write is
    genuinely spared -- this is the realized (not P-discounted) counterfactual,
    matching U6's honesty stance. Reuses the watchdog per-token pricing tables
    (_KEEPWARM_INPUT_USD_PER_TOKEN + _KEEPWARM_WRITE_PREMIUM['1h']). Pure.
    """
    _measure_facade = _compat.facade()
    key = _measure_facade._keepwarm_model_norm(model)
    input_rate = _measure_facade._KEEPWARM_INPUT_USD_PER_TOKEN.get(
        key, _measure_facade._KEEPWARM_INPUT_USD_PER_TOKEN["sonnet"])
    try:
        prefix = max(0, int(prefix_tokens))
    except (TypeError, ValueError):
        prefix = 0
    return prefix * input_rate * _measure_facade._KEEPWARM_WRITE_PREMIUM["1h"]


def _keepwarm_pause_key(session_id, arm_ts):
    """Stable identity for one (session, pause). Idempotency key for booking."""
    _measure_facade = _compat.facade()
    try:
        ts = float(arm_ts or 0)
    except (TypeError, ValueError):
        ts = 0.0
    return "%s|%.0f" % (str(session_id), ts)


def _keepwarm_load_ledger_rows(path=None):
    """Parse the keep-warm ledger into a list of row dicts. Never raises.

    Corrupt/blank lines are skipped. Missing file -> []. Read-only.
    """
    _measure_facade = _compat.facade()
    if path is None:
        path = _measure_facade._keepwarm_ledger_path()
    rows = []
    try:
        p = _measure_facade.Path(path)
        if not p.exists():
            return []
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = _measure_facade.json.loads(line)
                except (_measure_facade.json.JSONDecodeError, ValueError):
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _keepwarm_compact_ledger(now=None, retain_days=None):
    """Compact the keep-warm ledger to the last `retain_days` of rows on tick.

    Safe now that SQLite is the realized-booking dedup authority (FIX-A1): a
    realized/loss marker older than 30d can drop because the savings_events UNIQUE
    index -- not the ledger marker -- prevents a re-book. Rewrites atomically
    (mkstemp + os.replace under the ledger lock) keeping only rows with
    ts >= now - retain_days. Triggered when the file exceeds 5MB OR holds any row
    older than the retain window. Best-effort; never raises. Returns
    (compacted_bool, kept, dropped).
    """
    _measure_facade = _compat.facade()
    if now is None:
        now = _measure_facade.time.time()
    path = _measure_facade._keepwarm_ledger_path()
    try:
        if not path.exists():
            return (False, 0, 0)
        size = path.stat().st_size
    except OSError:
        return (False, 0, 0)
    rows = _measure_facade._keepwarm_load_ledger_rows(path)
    cutoff = float(now) - float(retain_days) * 86400.0
    kept = []
    dropped = 0
    has_old = False
    for r in rows:
        try:
            ts = float(r.get("ts", 0) or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts < cutoff:
            dropped += 1
            has_old = True
        else:
            kept.append(r)
    # Only rewrite when something would actually change (size cap breached OR old
    # rows present). A clean, in-window, small ledger is left untouched.
    if not has_old and size <= _measure_facade._KEEPWARM_LEDGER_MAX_BYTES:
        return (False, len(kept), 0)
    try:
        with _measure_facade._keepwarm_lock() as acquired:
            if not acquired:
                return (False, 0, 0)
            try:
                _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = _measure_facade.tempfile.mkstemp(
                    prefix=".keepwarm_ledger.", suffix=".tmp", dir=str(_measure_facade.SNAPSHOT_DIR))
                try:
                    if hasattr(_measure_facade.os, "fchmod"):
                        _measure_facade.os.fchmod(fd, 0o600)
                    with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as fh:
                        for r in kept:
                            try:
                                fh.write(_measure_facade.json.dumps(r) + "\n")
                            except (TypeError, ValueError):
                                continue
                    _measure_facade.os.replace(tmp_name, str(path))
                except Exception:
                    try:
                        _measure_facade.os.unlink(tmp_name)
                    except OSError:
                        pass
                    raise
            except OSError:
                return (False, len(kept), 0)
    except Exception:
        return (False, len(kept), 0)
    return (True, len(kept), dropped)


def _keepwarm_booked_pause_keys(rows=None):
    """Set of pause keys already booked (realized or loss) in the ledger.

    Used so keepwarm_detect_realized never double-books a (session, pause).
    """
    _measure_facade = _compat.facade()
    if rows is None:
        rows = _measure_facade._keepwarm_load_ledger_rows()
    booked = set()
    for r in rows:
        st = r.get("status")
        if st in (_measure_facade._KEEPWARM_LEDGER_REALIZED, _measure_facade._KEEPWARM_LEDGER_LOSS):
            key = r.get("pause_key")
            if key:
                booked.add(str(key))
    return booked


def _keepwarm_first_usage_after(transcript_path, arm_ts, parse_fn=None):
    """First usage-bearing assistant turn AFTER `arm_ts` in the transcript.

    Returns a turn dict (parse_session_turns shape) or None. A turn counts as
    usage-bearing if it has any cache_read/cache_creation/input_tokens. The arm
    record's `ts` (arm_ts) is the pause boundary; we take the earliest turn whose
    timestamp is strictly after arm_ts. Read-only. Never raises.
    """
    _measure_facade = _compat.facade()
    if parse_fn is None:
        parse_fn = _measure_facade.parse_session_turns
    try:
        turns = parse_fn(str(transcript_path))
    except Exception:
        return None
    if not turns:
        return None
    try:
        boundary = float(arm_ts or 0)
    except (TypeError, ValueError):
        boundary = 0.0
    best = None
    best_ts = None
    for t in turns:
        epoch = _measure_facade._keepwarm_iso_to_epoch(t.get("timestamp"))
        if epoch is None:
            continue
        if epoch <= boundary + 0.001:
            continue
        has_usage = (
            int(t.get("cache_read", 0) or 0) > 0
            or int(t.get("cache_creation", 0) or 0) > 0
            or int(t.get("input_tokens", 0) or 0) > 0
        )
        if not has_usage:
            continue
        if best_ts is None or epoch < best_ts:
            best = t
            best_ts = epoch
    return best


def keepwarm_resume_is_warm(turn, prefix_proxy):
    """Pure warm-vs-cold verdict for a resumed turn's usage.

    Warm iff cache_read >= 50% of the armed prefix AND cache_creation < 50% of
    the armed prefix (the resume reused the kept-warm prefix instead of paying a
    full re-write). Returns None when prefix is unknown/zero (cannot judge ->
    never book). Pure.
    """
    _measure_facade = _compat.facade()
    try:
        prefix = int(prefix_proxy or 0)
    except (TypeError, ValueError):
        prefix = 0
    if prefix <= 0 or not isinstance(turn, dict):
        return None
    cr = int(turn.get("resume_cache_read", turn.get("cache_read", 0)) or 0)
    cc = int(turn.get("resume_cache_creation", turn.get("cache_creation", 0)) or 0)
    warm = (cr >= _measure_facade._KEEPWARM_WARM_READ_FRACTION * prefix
            and cc < _measure_facade._KEEPWARM_WARM_WRITE_FRACTION * prefix)
    return bool(warm)


def _keepwarm_log_realized_savings(tokens_saved, cost_usd, session_id, detail=None,
                                   model=None, pause_key=None):
    """Write ONE realized keep-warm row to savings_events (the canonical merge).

    Realized savings enter _get_merged_savings via savings_events (the
    same table model_routing-style realized rows live in). _get_savings_summary
    groups by event_type with no allowlist, so an event_type='keepwarm' row's
    stored cost_saved_usd flows straight into the merged total -- no edit to
    _get_merged_savings or _V5_COMPRESSION_CATEGORIES (those gate the SEPARATE
    compression_events source only).

    Unlike _log_savings_event (which re-prices tokens at the INPUT rate), the
    keep-warm value is the avoided WRITE (already computed by
    _keepwarm_realized_write_usd at the 1h premium), so the row is inserted with
    the pre-computed cost directly. Same table + columns, correct price.

    SQLite is the dedup AUTHORITY: the row carries pause_key and
    is inserted INSERT OR IGNORE against the partial UNIQUE index on pause_key, so
    re-booking the same (session, pause) -- whether from a lost ledger marker or a
    re-detect across ticks -- is a no-op at the DB layer. Returns the number of
    rows ACTUALLY inserted (1 = new booking, 0 = deduped no-op / error). The caller
    uses this so in-memory realized counters never inflate on a dedup. Never raises
    (savings tracking must never crash the tick).
    """
    _measure_facade = _compat.facade()
    try:
        session_uuid, unjoinable = _measure_facade._extract_session_uuid(session_id)
        normalized = _measure_facade._keepwarm_model_norm(model)
        conn = _measure_facade._init_trends_db()
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO savings_events "
                "(timestamp, event_type, tokens_saved, cost_saved_usd, "
                "session_id, session_uuid, detail, model, unjoinable, pause_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_measure_facade.datetime.now().isoformat(), "keepwarm_estimated",
                 int(tokens_saved), float(cost_usd), session_id, session_uuid,
                 detail, normalized, 1 if unjoinable else 0, pause_key),
            )
            conn.commit()
            inserted = int(cur.rowcount or 0)
        finally:
            conn.close()
        return inserted
    except Exception:
        return 0


def keepwarm_detect_realized(now=None, records=None, parse_fn=None):
    """Book realized/loss outcomes for resumed armed records (THE booking step).

    For each armed record that has pings_fired >= 1 AND classifies 'resumed':
      * read the first usage-bearing turn AFTER the arm ts from the transcript;
      * if it cannot be read / there is no usable turn / prefix unknown -> book
        NOTHING (resume-but-unreadable; classification alone never books);
      * warm resume (cache_read >= 50% prefix AND cache_creation < 50% prefix)
        -> write ONE realized savings_events row (avoided 1h write) AND a
        ledger 'realized' marker row;
      * else (cold resume: prefix was re-written despite the pings) -> write a
        ledger 'loss' marker row only (no savings).

    Idempotent per (session, arm_ts): a pause already carrying a realized/loss
    ledger marker is skipped. MUST run BEFORE compact_keepwarm_sidecar (which
    drops resumed records). Returns {realized, loss, skipped, booked_usd}.
    Never raises.
    """
    _measure_facade = _compat.facade(); from . import keepwarm_economics
    if now is None:
        now = _measure_facade.time.time()
    if records is None:
        records = _measure_facade.load_keepwarm_records()
    out = {"realized": 0, "loss": 0, "skipped": 0, "errors": 0, "booked_usd": 0.0}
    try:
        booked = _measure_facade._keepwarm_booked_pause_keys()
    except Exception:
        booked = set()
    for rec in records:
        try:
            pings = rec.get("pings_fired")
            n_pings = len(pings) if isinstance(pings, list) else 0
            if n_pings < 1 or rec.get("ping_failed"):
                continue
            sid = rec.get("session_id")
            arm_ts = rec.get("ts")
            pause_key = _measure_facade._keepwarm_pause_key(sid, arm_ts)
            if pause_key in booked:
                continue
            try:
                verdict = _measure_facade.classify_keepwarm_record(rec, now=now)
            except Exception:
                verdict = "gone"
            if verdict != "resumed" and not (verdict == "gone" and keepwarm_economics.transcript_changed(rec)):
                # still_paused -> not done yet; gone -> never readable. Neither books.
                continue
            tp = rec.get("transcript_path")
            from . import keepwarm_safety
            if keepwarm_safety.compacted_since(rec):
                out["skipped"] += 1
                continue
            turn = _measure_facade._keepwarm_first_usage_after(tp, arm_ts, parse_fn=parse_fn) if tp else None
            if turn is None:
                # Resumed-but-unreadable (or expired with no post-arm turn): the
                # classifier said 'resumed' but real usage is unavailable, so we
                # book NOTHING (classification alone never books).
                out["skipped"] += 1
                continue
            resume_ts = _measure_facade._keepwarm_iso_to_epoch(turn.get("timestamp"))
            last_activity = rec.get("last_turn_ts") or arm_ts
            ttl_seconds = 3600 if rec.get("ttl_kind") == "1h" else 300
            if resume_ts is None or resume_ts - float(last_activity) <= ttl_seconds:
                out["skipped"] += 1
                continue
            prefix = rec.get("prefix_proxy")
            warm = _measure_facade.keepwarm_resume_is_warm(turn, prefix)
            if warm is None:
                out["skipped"] += 1
                continue
            model = rec.get("model")
            if warm:
                from . import savings_story
                net = savings_story.keepwarm_pause_net(_measure_facade, rec, now, _measure_facade._keepwarm_load_ledger_rows())
                if net is None:
                    out["skipped"] += 1
                    continue
                # Marker FIRST: a stranded ledger marker with no
                # savings row only UNDER-counts = safe; the reverse (savings row,
                # no marker) is what double-books. The DB INSERT OR IGNORE on
                # pause_key is the real dedup authority, so even a duplicate
                # marker+INSERT pair is a DB no-op. We credit in-memory counters
                # ONLY when the INSERT actually wrote a new row.
                _measure_facade._keepwarm_ledger_append({
                    "session_id": str(sid),
                    "ts": float(now),
                    "status": _measure_facade._KEEPWARM_LEDGER_REALIZED,
                    "pause_key": pause_key,
                    "model": _measure_facade._keepwarm_model_norm(model),
                    "prefix_proxy": int(prefix or 0),
                    "pings": n_pings,
                    "realized_usd": round(savings_story.keepwarm_reload_value(_measure_facade, rec), 6),
                    "cache_read": int(turn.get("cache_read", 0) or 0),
                    "cache_creation": int(turn.get("cache_creation", 0) or 0),
                })
                inserted = _measure_facade._keepwarm_log_realized_savings(
                    tokens_saved=0, cost_usd=net, session_id=sid,
                    detail="estimated avoided reload less ping spend",
                    model=model, pause_key=pause_key)
                if inserted:
                    out["realized"] += 1
                    out["booked_usd"] += float(net)
            else:
                _measure_facade._keepwarm_ledger_append({
                    "session_id": str(sid),
                    "ts": float(now),
                    "status": _measure_facade._KEEPWARM_LEDGER_LOSS,
                    "pause_key": pause_key,
                    "model": _measure_facade._keepwarm_model_norm(model),
                    "prefix_proxy": int(prefix or 0),
                    "pings": n_pings,
                    "cache_read": int(turn.get("cache_read", 0) or 0),
                    "cache_creation": int(turn.get("cache_creation", 0) or 0),
                })
                out["loss"] += 1
            booked.add(pause_key)
        except Exception as exc:
            # A record that raises every tick would otherwise be invisible
            # forever. Count it and write ONE scrubbed line so
            # a systematically un-bookable pause is diagnosable in the field.
            out["errors"] += 1
            try:
                _measure_facade._keepwarm_scheduler_log_append({
                    "ts": float(now),
                    "event": "detect_realized_error",
                    "session_id": str(rec.get("session_id"))[:64],
                    "error": type(exc).__name__,
                })
            except Exception:
                pass
            continue
    out["booked_usd"] = round(out["booked_usd"], 6)
    return out


def keepwarm_spend_summary(days=None, now=None, rows=None):
    """Aggregate ping SPEND + realized from the keep-warm ledger (operational).

    Spend = sum of cost_usd over outcome rows (status ok/error/timeout) -- the
    real metered ping cost. Realized = sum of realized_usd over 'realized' rows.
    NET = realized - spend. Returns counts too. `days` windows on row ts (None =
    all). Read-only; the merge total comes from savings_events, NOT this -- this
    is the dedicated spend cost-line and the per-session realized breakdown.
    """
    _measure_facade = _compat.facade(); from . import keepwarm_economics
    if now is None:
        now = _measure_facade.time.time()
    if rows is None:
        rows = _measure_facade._keepwarm_load_ledger_rows()
    cutoff = None
    if days is not None:
        try:
            cutoff = now - float(days) * 86400.0
        except (TypeError, ValueError):
            cutoff = None
    # Pre-pass: index every outcome row's ts per session so we can
    # detect 'firing' rows that never got a sibling outcome (tick SIGKILLed between
    # the pre-log and the outcome append). An orphaned firing is real spend the
    # quota was charged for; counting it keeps the tripwire ratio honest.
    outcome_ts_by_session = {}
    for r in rows:
        if r.get("status") in ("ok", "error", "timeout", "cancelled"):
            try:
                ots = float(r.get("ts", 0) or 0)
            except (TypeError, ValueError):
                ots = 0.0
            outcome_ts_by_session.setdefault(
                (r.get("session_id") or "unknown", r.get("action_id")), []).append(ots)

    spend = 0.0
    realized = 0.0
    pings = 0
    ok_count = 0
    error_count = 0
    timeout_count = 0
    realized_count = 0
    loss_count = 0
    orphan_firing_count = 0
    orphan_firing_usd = 0.0
    per_session = {}
    # Dedup realized/loss by pause_key (defense-in-depth): even
    # though the SQLite UNIQUE index is the booking authority, a duplicated ledger
    # marker must NOT inflate the rolling-7d ratio the tripwire divides by.
    seen_realized_keys = set()
    seen_loss_keys = set()
    for r in rows:
        try:
            ts = float(r.get("ts", 0) or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if cutoff is not None and ts < cutoff:
            continue
        st = r.get("status")
        sid = r.get("session_id") or "unknown"
        if st in ("ok", "error", "timeout"):
            c = float(r.get("cost_usd", 0.0) or 0.0)
            if c <= 0 and r.get("prefix_proxy"):
                c = _measure_facade._keepwarm_ping_cost_est(r["prefix_proxy"], r.get("model"))
            spend += c
            pings += 1
            if st == "ok":
                ok_count += 1
            elif st == "error":
                error_count += 1
            else:
                timeout_count += 1
            ps = per_session.setdefault(sid, {"realized_usd": 0.0, "spend_usd": 0.0, "pings": 0})
            ps["spend_usd"] += c
            ps["pings"] += 1
        elif st == "firing":
            # Orphan iff no outcome row for this session landed at/after the firing
            # ts AND the firing is older than the grace window (a fresh firing may
            # simply be mid-ping). Counted at its recorded est_cost_usd.
            siblings = outcome_ts_by_session.get((sid, r.get("action_id")), [])
            has_outcome = keepwarm_economics.claim_outcome(siblings, ts)
            if not has_outcome:
                ec = float(r.get("est_cost_usd", 0.0) or 0.0)
                spend += ec
                pings += 1
                error_count += 1  # an unresolved ping is a failed outcome
                orphan_firing_count += 1
                orphan_firing_usd += ec
                ps = per_session.setdefault(
                    sid, {"realized_usd": 0.0, "spend_usd": 0.0, "pings": 0})
                ps["spend_usd"] += ec
                ps["pings"] += 1
        elif st == _measure_facade._KEEPWARM_LEDGER_REALIZED:
            key = r.get("pause_key")
            if key is not None:
                if key in seen_realized_keys:
                    continue
                seen_realized_keys.add(key)
            v = float(r.get("realized_usd", 0.0) or 0.0)
            realized += v
            realized_count += 1
            ps = per_session.setdefault(sid, {"realized_usd": 0.0, "spend_usd": 0.0, "pings": 0})
            ps["realized_usd"] += v
        elif st == _measure_facade._KEEPWARM_LEDGER_LOSS:
            key = r.get("pause_key")
            if key is not None:
                if key in seen_loss_keys:
                    continue
                seen_loss_keys.add(key)
            loss_count += 1
    top_sessions = sorted(
        ({"session_id": s, **v} for s, v in per_session.items()),
        key=lambda x: ((_measure_facade := _compat.facade()), x["realized_usd"])[1], reverse=True)[:5]
    return {
        "spend_usd": round(spend, 6),
        "realized_usd": round(realized, 6),
        "net_usd": round(realized - spend, 6),
        "pings": pings,
        "ok_count": ok_count,
        "error_count": error_count,
        "timeout_count": timeout_count,
        "realized_count": realized_count,
        "loss_count": loss_count,
        "orphan_firing_count": orphan_firing_count,
        "orphan_firing_usd": round(orphan_firing_usd, 6),
        "top_sessions": top_sessions,
        "period_days": days,
    }


def _keepwarm_rate_limits_path():
    """The statusline-maintained account-global rate-limit sidecar (read-only).

    Written by statusline.js from Claude Code's own usage data: five_hour /
    seven_day used_percentage + resets_at. THE meter source for the subscription
    quota model.

    Base is RUNTIME_DIR, NOT QUALITY_CACHE_DIR. The meter is ACCOUNT-global, not
    per-session state, so it must not ride the is_cowork() -> plugin-data
    _STATE_BASE divergence that QUALITY_CACHE_DIR uses for run-once markers and
    the quality cache. RUNTIME_DIR resolves to <claude home>/token-optimizer on
    desktop AND inside Cowork (is_cowork() only shifts _STATE_BASE, never
    RUNTIME_DIR). Reading via QUALITY_CACHE_DIR pointed a Cowork or synced-plugin
    session at <plugin-data>/token-optimizer/rate-limits.json -- a path the
    statusline never writes -- so the meter read empty, runway_snapshot returned
    zero windows, and the "your live 5h/7d view reappears next session" placeholder
    got baked into the SHARED dashboard that every session (desktop included) then
    displayed, even with a fresh live meter. Pinning to RUNTIME_DIR removes that
    divergence: RUNTIME_DIR (= claude_home() for the Claude runtime) is stable
    across Cowork, matching where the statusline writes in the common case.

    RUNTIME_DIR resolves through claude_home(), which honors CLAUDE_CONFIG_DIR.
    statusline.js and the VS Code companion resolve the same dir with the same
    rules (#198), so a relocated config reads and writes the meter in one place.
    Keep all three resolvers in step; tests/test_statusline_claude_config_dir.py
    holds them to parity.

    Foreign runtimes (codex/copilot/hermes) have no meter writer, so their
    runtime-scoped RUNTIME_DIR path stays empty. runway_snapshot then returns a card
    with no windows whenever the savings ledger alone has a story, and the template
    shows the "live view refreshes while you work" note (NOT literally nothing).
    """
    _measure_facade = _compat.facade()
    return _measure_facade.RUNTIME_DIR / "token-optimizer" / "rate-limits.json"


def _keepwarm_read_meters(rate_limits_path=None, now=None):
    """Read current 5h / 7d quota utilization (%), or an unavailable marker.

    Returns {available, five_hour_pct, seven_day_pct, ts, age_s, stale}. Pure +
    injectable (tests pass a path). Fail-open: a missing/corrupt/uninformative
    file returns available=False with None fields and never raises. statusline
    writes a millisecond `timestamp`; it is normalised to seconds here.
    """
    _measure_facade = _compat.facade()
    if now is None:
        now = _measure_facade.time.time()
    if rate_limits_path is None:
        rate_limits_path = _measure_facade._keepwarm_rate_limits_path()
    out = {"available": False, "five_hour_pct": None, "seven_day_pct": None,
           "seven_day_resets_at": None, "ts": None, "age_s": None, "stale": True}
    try:
        data = _measure_facade.json.loads(_measure_facade.Path(rate_limits_path).read_text(encoding="utf-8"))
    except (OSError, _measure_facade.json.JSONDecodeError, ValueError):
        return out
    if not isinstance(data, dict):
        return out

    def _pct(win):
        _measure_facade = _compat.facade()
        if isinstance(win, dict):
            v = win.get("used_percentage")
            if isinstance(v, (int, float)) and v == v:  # reject NaN
                return max(0.0, min(100.0, float(v)))
        return None

    def _resets_at(win):
        """Extract resets_at epoch seconds from a window dict. Accepts numeric
        (epoch s) and ISO-8601 string (parsed). Returns None when absent/invalid."""
        _measure_facade = _compat.facade()
        if not isinstance(win, dict):
            return None
        ra = win.get("resets_at")
        if isinstance(ra, (int, float)) and ra > 0 and ra == ra:
            return float(ra)
        if isinstance(ra, str):
            try:
                from datetime import datetime as _dt
                parsed = _dt.fromisoformat(ra.replace("Z", "+00:00"))
                return parsed.timestamp()
            except (ValueError, OSError):
                return None
        return None

    fh = _pct(data.get("five_hour"))
    sd = _pct(data.get("seven_day"))
    sd_resets = _resets_at(data.get("seven_day"))
    ts = data.get("timestamp")
    if isinstance(ts, (int, float)) and ts > 1e12:  # ms epoch -> s
        ts = ts / 1000.0
    age = None
    stale = True
    if isinstance(ts, (int, float)):
        age = max(0.0, now - float(ts))
        stale = age > _measure_facade._KEEPWARM_METER_STALE_SECONDS
    out.update({
        "available": (fh is not None or sd is not None),
        "five_hour_pct": fh, "seven_day_pct": sd,
        "seven_day_resets_at": sd_resets,
        "ts": (float(ts) if isinstance(ts, (int, float)) else None),
        "age_s": age, "stale": stale,
    })
    return out


def _keepwarm_meter_samples_path():
    _measure_facade = _compat.facade()
    return _measure_facade.SNAPSHOT_DIR / _measure_facade._KEEPWARM_METER_SAMPLES_NAME


def _keepwarm_meter_sample_append(record):
    """Append one before/after meter sample around a ping. Fail-open, 0600.

    Each row records the 5h/7d meter immediately before and after a ping fire so
    the measured per-ping meter cost can be estimated over time. Never raises.
    """
    _measure_facade = _compat.facade()
    try:
        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = _measure_facade._keepwarm_meter_samples_path()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(_measure_facade.json.dumps(record, separators=(",", ":")) + "\n")
        try:
            _measure_facade.os.chmod(str(path), 0o600)
        except OSError:
            pass
    except Exception:
        pass


def _keepwarm_load_meter_samples(path=None):
    """Load meter sample rows (fail-open: missing/corrupt -> [])."""
    _measure_facade = _compat.facade()
    if path is None:
        path = _measure_facade._keepwarm_meter_samples_path()
    rows = []
    try:
        text = _measure_facade.Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(_measure_facade.json.loads(line))
        except (_measure_facade.json.JSONDecodeError, ValueError):
            continue
    return rows


def _keepwarm_median(values):
    """Median of a non-empty numeric list; None for empty. Pure."""
    _measure_facade = _compat.facade()
    vals = sorted(v for v in values if isinstance(v, (int, float)) and v == v)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return float(vals[mid])
    return (float(vals[mid - 1]) + float(vals[mid])) / 2.0


def _keepwarm_measured_ping_meter(meter_samples, window):
    """Median observed per-ping meter movement (pct points) for a window.

    window is 'sd' (seven_day) or 'fh' (five_hour). Only `ok` samples with both
    before+after present count. A ping cannot REDUCE the meter, so negative
    deltas (window resets / concurrent-activity noise crossing a reset) are
    dropped rather than clamped -- they are not ping cost. Returns
    {n, median_pct} (median None when n==0). Pure.
    """
    _measure_facade = _compat.facade()
    before_key = window + "_before"
    after_key = window + "_after"
    deltas = []
    for s in meter_samples or ():
        if s.get("status") != "ok":
            continue
        b = s.get(before_key)
        a = s.get(after_key)
        if not isinstance(b, (int, float)) or not isinstance(a, (int, float)):
            continue
        d = float(a) - float(b)
        if d < 0:  # reset crossing / noise -> not ping cost
            continue
        deltas.append(d)
    return {"n": len(deltas), "median_pct": _measure_facade._keepwarm_median(deltas)}


def keepwarm_quota_value_summary(days=None, now=None, rows=None,
                                 meters=None, meter_samples=None,
                                 dollar_summary=None):
    """Subscription QUOTA value of keep-warm: meter SAVED vs meter SPENT.

    READ-ONLY advisory instrument (see the section header). Never changes the
    gate. Never raises. Returns a structured verdict:

      {
        currency: 'rate-limit-quota',
        period_days,
        meters: {available, five_hour_pct, seven_day_pct, stale, age_s},
        modeled: {spend_usd, realized_usd, ratio, net_usd, ok_pings,
                  realized_count},            # cache-weighted dollar-ratio prior
        measured: {seven_day, five_hour},     # per-window {n, median_pct} ping cost
        confidence: 'insufficient-data'|'modeled'|'measured-spend',
        verdict: 'insufficient-data'|'quota-positive'|'quota-negative'|
                 'quota-neutral',
        recommendation: <advisory human string>,
        gate_changed: False,                  # always -- this never flips the gate
      }

    modeled.ratio = realized_usd / spend_usd is the cache-weighted prior for the
    meter ratio (see header). measured refines the SPEND side from real meter
    deltas; the SAVED side stays modeled until cold/warm resume samples exist.
    """
    _measure_facade = _compat.facade()
    if now is None:
        now = _measure_facade.time.time()
    if rows is None:
        rows = _measure_facade._keepwarm_load_ledger_rows()
    if dollar_summary is None:
        dollar_summary = _measure_facade.keepwarm_spend_summary(days=days, now=now, rows=rows)
    if meters is None:
        meters = _measure_facade._keepwarm_read_meters(now=now)
    if meter_samples is None:
        meter_samples = _measure_facade._keepwarm_load_meter_samples()

    spend = float(dollar_summary.get("spend_usd", 0.0) or 0.0)
    realized = float(dollar_summary.get("realized_usd", 0.0) or 0.0)
    ok_pings = int(dollar_summary.get("ok_count", 0) or 0)
    realized_count = int(dollar_summary.get("realized_count", 0) or 0)
    # Ratio prior: realized/spend (cache-weighted meter == dollar ratio). A
    # zero-spend window has no defined ratio (and nothing to spend quota on).
    modeled_ratio = (realized / spend) if spend > 0 else None
    modeled_net = realized - spend

    measured_sd = _measure_facade._keepwarm_measured_ping_meter(meter_samples, "sd")
    measured_fh = _measure_facade._keepwarm_measured_ping_meter(meter_samples, "fh")

    # Confidence + verdict.
    has_completed_work = (ok_pings > 0) or (realized_count > 0)
    enough_samples = (measured_sd["n"] >= _measure_facade._KEEPWARM_QUOTA_MIN_PING_SAMPLES)
    if not has_completed_work:
        confidence = "insufficient-data"
        verdict = "insufficient-data"
    else:
        confidence = "measured-spend" if enough_samples else "modeled"
        if modeled_ratio is None:
            # Realized but zero spend -> pure positive (saved quota, spent none).
            verdict = "quota-positive" if realized > 0 else "quota-neutral"
        elif modeled_ratio > _measure_facade._KEEPWARM_QUOTA_RATIO_POSITIVE:
            verdict = "quota-positive"
        elif modeled_ratio < _measure_facade._KEEPWARM_QUOTA_RATIO_POSITIVE:
            verdict = "quota-negative"
        else:
            verdict = "quota-neutral"

    # Provider quota weighting is not published; meter movement is observational.
    recommendation = ("Keep-warm quota impact is not yet measurable." if verdict == "insufficient-data"
                      else "The saved side is modeled using API rates; observed meter movement may include other activity.")

    return {
        "currency": "rate-limit-quota",
        "period_days": days,
        "meters": {
            "available": meters.get("available", False),
            "five_hour_pct": meters.get("five_hour_pct"),
            "seven_day_pct": meters.get("seven_day_pct"),
            "stale": meters.get("stale", True),
            "age_s": meters.get("age_s"),
        },
        "modeled": {
            "spend_usd": round(spend, 6),
            "realized_usd": round(realized, 6),
            "ratio": (round(modeled_ratio, 4)
                      if modeled_ratio is not None else None),
            "net_usd": round(modeled_net, 6),
            "ok_pings": ok_pings,
            "realized_count": realized_count,
        },
        "measured": {"seven_day": measured_sd, "five_hour": measured_fh},
        "confidence": confidence,
        "verdict": verdict,
        "recommendation": recommendation,
        "gate_changed": False,
    }


def _keepwarm_tripwire_path():
    _measure_facade = _compat.facade()
    return _measure_facade.SNAPSHOT_DIR / _measure_facade._KEEPWARM_TRIPWIRE_NAME


def _valid_keepwarm_tripwire_payload(data):
    """Schema-check the keep-warm tripwire sidecar (corrupt -> fail-closed off)."""
    _measure_facade = _compat.facade()
    if not isinstance(data, dict):
        return False
    mode = data.get("mode")
    if mode not in _measure_facade._KEEPWARM_TRIPWIRE_LADDER:
        return False
    if not isinstance(data.get("strikes", 0), (int, float)):
        return False
    return True


def keepwarm_tripwire_mode():
    """Current tripwire-imposed mode ceiling. Fail-closed to 'off' on corruption.

    Returns one of 'sustain' | 'probe-only' | 'off'. A missing sidecar reads as
    the most-permissive 'sustain' (no tripwire breach yet); an EXISTING but
    corrupt/unparseable sidecar reads as 'off' (fail-closed, never fail-open to
    sustain). Read-only, lock-free fast path.
    """
    _measure_facade = _compat.facade()
    path = _measure_facade._keepwarm_tripwire_path()
    try:
        if not path.exists():
            return "sustain"
        data = _measure_facade.json.loads(path.read_text(encoding="utf-8"))
    except (_measure_facade.json.JSONDecodeError, OSError, ValueError):
        return "off"
    if not _measure_facade._valid_keepwarm_tripwire_payload(data):
        return "off"
    return data.get("mode", "off")


def _keepwarm_tripwire_lock():
    """Bounded portable lease serializing tripwire recompute+write.

    Contenders skip the mutation after 75ms rather than blocking a hook.
    """
    _measure_facade = _compat.facade()
    lock_path = _measure_facade.SNAPSHOT_DIR / f"{_measure_facade._KEEPWARM_TRIPWIRE_LOCK_NAME}.lease"
    with _measure_facade.lease_lock(
        lock_path,
        deadline=_measure_facade.current_deadline(),
        acquire_timeout=0.075,
    ) as acquired:
        yield acquired


def _write_keepwarm_tripwire_sidecar(mode, strikes, ratio, health, now=None,
                                     last_strike_ts=None):
    """Atomically write the keep-warm tripwire sidecar (0600). Never raises."""
    _measure_facade = _compat.facade()
    if now is None:
        now = _measure_facade.time.time()
    path = _measure_facade._keepwarm_tripwire_path()
    record = {
        "mode": mode,
        "strikes": int(strikes),
        "ratio_7d": round(float(ratio), 4),
        "health": health,
        "last_strike_ts": last_strike_ts,
        "_cached_ts": now,
    }
    try:
        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = _measure_facade.tempfile.mkstemp(
            prefix=".keepwarm_tripwire.", suffix=".tmp", dir=str(_measure_facade.SNAPSHOT_DIR))
        try:
            if hasattr(_measure_facade.os, "fchmod"):
                _measure_facade.os.fchmod(fd, 0o600)
            with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as fh:
                _measure_facade.json.dump(record, fh)
            _measure_facade.os.replace(tmp_name, str(path))
        except Exception:
            try:
                _measure_facade.os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception:
        pass
    return record


def _keepwarm_tripwire_health(rows, now=None):
    """Leading health signal: pings on pauses that never resumed / total pings.

    A ping belongs to a 'never resumed' pause if that session has *completed*
    pings in the ledger but no realized/loss booking marker. Returns
    {pings_on_never_resumed, total_pings, errored_pings, fraction, alarm}.
    Read-only, pure over `rows`.

    Only `ok` (successfully completed) pings count toward the waste ratio. An
    `error`/`timeout` ping never warmed a cache and spent ~$0, so it is neither
    waste nor signal -- counting it as "a ping wasted on a never-resumed
    session" produced a FALSE 100% alarm whenever the executor was failing
    (e.g. the stale launchd empty-PATH FileNotFoundError run: 48/48 errored
    pings, fraction 1.0, alarm true, despite zero real keep-warm work). Errored
    pings are an INFRA signal (surfaced separately as `errored_pings`), not a
    never-resumed-waste signal.
    """
    _measure_facade = _compat.facade()
    total_pings = 0
    errored_pings = 0
    pings_by_session = {}
    resolved_sessions = set()
    for r in rows:
        st = r.get("status")
        sid = r.get("session_id") or "unknown"
        if st == "ok":
            total_pings += 1
            pings_by_session[sid] = pings_by_session.get(sid, 0) + 1
        elif st in ("error", "timeout"):
            errored_pings += 1
        elif st in (_measure_facade._KEEPWARM_LEDGER_REALIZED, _measure_facade._KEEPWARM_LEDGER_LOSS):
            resolved_sessions.add(sid)
    never = sum(n for s, n in pings_by_session.items() if s not in resolved_sessions)
    frac = (never / total_pings) if total_pings else 0.0
    return {
        "pings_on_never_resumed": never,
        "total_pings": total_pings,
        "errored_pings": errored_pings,
        "fraction": round(frac, 4),
        "alarm": bool(frac > _measure_facade._KEEPWARM_TRIPWIRE_ALARM_FRACTION),
    }


def evaluate_keepwarm_tripwire(now=None, rows=None, force=None):
    """Recompute the rolling-7d realized/spend ratio and apply the demotion ladder.

    Two-strike, sticky, fail-closed (clone of evaluate_cohort_tripwire shape):
      * ratio = realized_7d / spend_7d (spend 0 -> ratio treated as >1 so a
        zero-spend window never demotes).
      * On a breach (ratio <= 1.0): first strike demotes sustain -> probe-only;
        a second strike demotes probe-only -> off, but ONLY when the window has
        >= _KEEPWARM_TRIPWIRE_MIN_PINGS pings (a full, informative window).
      * Demotions are STICKY: the mode only moves DOWN here; it never re-promotes
        on a later good ratio (that needs an explicit keepwarm-enable re-run).
      * Fail-closed: a corrupt sidecar is read as 'off' before this runs, so a
        poisoned file can never be walked back up to sustain.
    Returns the written sidecar record. Never raises.
    """
    _measure_facade = _compat.facade()
    if now is None:
        now = _measure_facade.time.time()
    if rows is None:
        rows = _measure_facade._keepwarm_load_ledger_rows()
    summary = _measure_facade.keepwarm_spend_summary(days=_measure_facade._KEEPWARM_TRIPWIRE_WINDOW_DAYS,
                                     now=now, rows=rows)
    spend = summary["spend_usd"]
    realized = summary["realized_usd"]
    pings = summary["pings"]
    # Zero-spend windows never demote: no pings means no breach to judge, so the
    # ratio sits strictly above the gate (gate + realized + 1) regardless.
    ratio = (realized / spend) if spend > 0 else (
        _measure_facade._KEEPWARM_TRIPWIRE_RATIO_GATE + realized + 1.0)
    health = _measure_facade._keepwarm_tripwire_health(rows, now=now)

    with _measure_facade._keepwarm_tripwire_lock() as acquired:
        if not acquired:
            return {}
        cur_mode = _measure_facade.keepwarm_tripwire_mode()
        try:
            prev = _measure_facade.json.loads(_measure_facade._keepwarm_tripwire_path().read_text(encoding="utf-8"))
            strikes = int(prev.get("strikes", 0) or 0)
            last_strike_ts = prev.get("last_strike_ts")
        except (_measure_facade.json.JSONDecodeError, OSError, ValueError, TypeError):
            strikes = 0
            last_strike_ts = None

        new_mode = cur_mode
        breach = ratio <= _measure_facade._KEEPWARM_TRIPWIRE_RATIO_GATE
        if breach:
            # Distinct-window strike (FIX-A3): only count a NEW strike when at
            # least 24h has elapsed since the previous one. Repeated breaches
            # within the same window are the SAME strike -- a money-losing config
            # must fail across two separate days, not two adjacent ticks.
            try:
                prev_ts = float(last_strike_ts) if last_strike_ts is not None else None
            except (TypeError, ValueError):
                prev_ts = None
            new_strike = (
                prev_ts is None
                or (float(now) - prev_ts) >= _measure_facade._KEEPWARM_TRIPWIRE_STRIKE_WINDOW_SECONDS)
            if new_strike:
                strikes += 1
                last_strike_ts = float(now)
                # Strike 1: sustain -> probe-only (moot while sustain unpromoted,
                # still wired). Strike 2+: probe-only -> off, only on a full window.
                if cur_mode == "sustain":
                    new_mode = "probe-only"
                elif cur_mode == "probe-only" and strikes >= 2 and pings >= _measure_facade._KEEPWARM_TRIPWIRE_MIN_PINGS:
                    new_mode = "off"
                # 'off' is terminal here (sticky); never walks back up.
            # else: same-window breach -> no new strike, mode/strikes preserved.
        # else: no breach -> sticky, mode unchanged, strikes preserved.

        record = _measure_facade._write_keepwarm_tripwire_sidecar(
            new_mode, strikes, ratio, health, now=now,
            last_strike_ts=last_strike_ts)
    return record


def keepwarm_tripwire_repromote(now=None):
    """Reset the tripwire to the most-permissive 'sustain' (explicit re-enable).

    Called ONLY by keepwarm-enable re-run (records a fresh promoted_at-equivalent
    by zeroing strikes + writing mode 'sustain'). This is the single sanctioned
    walk-up path; the auto-evaluator never re-promotes. Never raises.
    """
    _measure_facade = _compat.facade()
    if now is None:
        now = _measure_facade.time.time()
    rows = _measure_facade._keepwarm_load_ledger_rows()
    health = _measure_facade._keepwarm_tripwire_health(rows, now=now)
    summary = _measure_facade.keepwarm_spend_summary(days=_measure_facade._KEEPWARM_TRIPWIRE_WINDOW_DAYS,
                                     now=now, rows=rows)
    spend = summary["spend_usd"]
    ratio = (summary["realized_usd"] / spend) if spend > 0 else (summary["realized_usd"] + 1.0)
    with _measure_facade._keepwarm_tripwire_lock() as acquired:
        if not acquired:
            return None
        return _measure_facade._write_keepwarm_tripwire_sidecar(
            "sustain", 0, ratio, health, now=now, last_strike_ts=None)


def keepwarm_report(days=None, now=None):
    """Assemble the human/JSON keep-warm money story (CLI keepwarm-report).

    Returns a dict: mode (probe-only/sustain/off/limits-lab), consent + billing,
    scheduler status, 7d + 30d {pings, spend, realized, net}, tripwire state +
    ratio, top-5 sessions by realized. Honest empty-state when no pings yet.
    Never raises (each sub-read is fail-open).
    """
    _measure_facade = _compat.facade(); from . import keepwarm_economics
    if now is None:
        now = _measure_facade.time.time()
    try:
        billing = _measure_facade.keepwarm_billing_mode()
    except Exception:
        billing = "subscription"
    try:
        consent = _measure_facade.keepwarm_consent()
    except Exception:
        consent = "unasked"
    try:
        limits_lab = bool(_measure_facade._read_config_flag("keepwarm_limits_lab", False))
    except Exception:
        limits_lab = False
    try:
        gate_allowed, gate_reason = _measure_facade.keepwarm_gate()
    except Exception:
        gate_allowed, gate_reason = (False, "error")
    try:
        tw_mode = _measure_facade.keepwarm_tripwire_mode()
    except Exception:
        tw_mode = "off"
    # Promotion fence: sustain only if promoted AND tripwire allows it.
    sustain_allowed = False
    try:
        prom = _measure_facade.json.loads(_measure_facade._keepwarm_promotion_path().read_text(encoding="utf-8"))
        sustain_allowed = bool(prom.get("sustain_allowed", False))
    except (_measure_facade.json.JSONDecodeError, OSError, ValueError):
        sustain_allowed = False

    # Effective mode label for humans.
    if limits_lab:
        mode = "limits-lab"
    elif tw_mode == "off" or not gate_allowed:
        mode = "off"
    elif tw_mode == "sustain" and sustain_allowed:
        mode = "sustain"
    else:
        mode = "probe-only"

    rows = _measure_facade._keepwarm_load_ledger_rows()
    s7 = keepwarm_economics.verified_summary(_measure_facade, 7, now)
    s30 = keepwarm_economics.verified_summary(_measure_facade, days, now); s30["manual_pings"] = keepwarm_economics.manual_count(rows, now - float(days or 30) * 86400)
    health = _measure_facade._keepwarm_tripwire_health(rows, now=now)
    try:
        tw_ratio = _measure_facade.json.loads(_measure_facade._keepwarm_tripwire_path().read_text(encoding="utf-8")).get("ratio_7d")
    except (_measure_facade.json.JSONDecodeError, OSError, ValueError):
        tw_ratio = None

    try:
        sched = _measure_facade.keepwarm_scheduler_status()
    except Exception:
        sched = {"installed": False}

    has_pings = s30["pings"] > 0 or s30["realized_count"] > 0
    # Subscription quota-value (advisory, read-only): only meaningful on
    # subscription billing, where the dollar model reads $0. Computed from the 7d
    # window + meter samples; never changes the gate.
    try:
        quota_value = _measure_facade.keepwarm_quota_value_summary(
            days=7, now=now, rows=rows, dollar_summary=s7)
    except Exception:
        quota_value = None
    return {
        "mode": mode,
        "billing": billing,
        "consent": consent,
        "limits_lab": limits_lab,
        "gate_allowed": gate_allowed,
        "gate_reason": gate_reason,
        "sustain_allowed": sustain_allowed,
        "tripwire_mode": tw_mode,
        "tripwire_ratio_7d": tw_ratio,
        "scheduler": sched,
        "last_tick_ok": sched.get("last_tick_ok"),
        "last_tick_age": sched.get("last_tick_age"),
        "window_7d": s7,
        "window_30d": s30,
        "health": health,
        "quota_value": quota_value,
        "top_sessions": s30["top_sessions"],
        "has_pings": has_pings,
        "empty_state": (
            None if has_pings else
            "No keep-warm pings yet. Eligible paused sessions can be refreshed "
            "before the cache expires; estimated value and ping cost appear here."),
    }


def keepwarm_cache_health_block(days=None, now=None):
    """Build the dashboard cache-health keep-warm sub-block (carried in payload).

    One of five tile states + the realized/spend/net numbers + tripwire ratio +
    predictor transparency (feature names when sustain ever activates). Pure
    assembly over keepwarm_report; the dashboard renders it. Never raises.
    States: active-probe | active-sustain | off-subscription | demoted-by-tripwire
    | platform-gap.
    """
    _measure_facade = _compat.facade()
    if now is None:
        now = _measure_facade.time.time()
    try:
        rep = _measure_facade.keepwarm_report(days=days, now=now)
    except Exception:
        rep = None
    # platform-gap: keep-warm only ships on the claude_code runtime. On any
    # other runtime the tile is an explicit documented gap.
    try:
        runtime = _measure_facade.detect_runtime()
    except Exception:
        runtime = "claude"
    if runtime != "claude" or _measure_facade._normalized_platform() == "Windows":
        return {
            "state": "platform-gap",
            "runtime": runtime, "platform": _measure_facade._normalized_platform(),
            "note": ("Keep-warm is unavailable on Windows." if _measure_facade._normalized_platform() == "Windows" else
                     "Keep-warm is available for Claude Code only."),
        }
    if rep is None:
        return {"state": "paused", "runtime": runtime,
                "note": "Keep-warm: paused while we prove it is safe and saves you money"}

    tw_mode = rep["tripwire_mode"]
    if tw_mode == "off":
        state = "demoted-by-tripwire"
    elif rep["mode"] == "sustain":
        state = "active-sustain"
    elif rep["mode"] in ("probe-only", "limits-lab"):
        state = "active-probe"
    else:
        state = "off-subscription"

    block = {
        "state": "paused", "note": "Keep-warm: paused while we prove it is safe and saves you money", "gate_reason": rep["gate_reason"], "pings_7d": rep["window_7d"]["pings"],
        "mode": rep["mode"],
        "billing": rep["billing"],
        "consent": rep["consent"],
        "limits_lab": rep["limits_lab"],
        "realized_usd": rep["window_30d"]["realized_usd"],
        "spend_usd": rep["window_30d"]["spend_usd"],
        "net_usd": rep["window_30d"]["net_usd"],
        "realized_usd_7d": rep["window_7d"]["realized_usd"],
        "spend_usd_7d": rep["window_7d"]["spend_usd"],
        "net_usd_7d": rep["window_7d"]["net_usd"],
        "pings_30d": rep["window_30d"]["pings"], "manual_pings_30d": rep["window_30d"].get("manual_pings", 0),
        "tripwire_mode": tw_mode,
        "tripwire_ratio_7d": rep["tripwire_ratio_7d"],
        "health": rep["health"],
        "has_pings": rep["has_pings"],
        "top_sessions": rep["top_sessions"],
        "quota_value": rep["quota_value"],
        "predictor_features": [
            "prior_resume_count", "sibling_active",
            "machine_active", "survival_p",
        ],
        "predictor_threshold": _measure_facade._KEEPWARM_PRED_THRESHOLD,
        "honest_line": None,
    }
    return block


def _subagent_cache_marker_path():
    """Marker lives in TO's own data dir (plugin-data aware, never settings)."""
    _measure_facade = _compat.facade()
    return _measure_facade.SNAPSHOT_DIR / _measure_facade._SUBAGENT_CACHE_MARKER_NAME


def _subagent_cache_read_marker():
    """Read the state marker, or None when absent/corrupt (None = unknown-old)."""
    _measure_facade = _compat.facade()
    p = _measure_facade._subagent_cache_marker_path()
    try:
        if not p.exists():
            return None
        data = _measure_facade.json.loads(p.read_text(encoding="utf-8"))
    except (_measure_facade.json.JSONDecodeError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _subagent_cache_write_marker(record):
    """Atomically write the state marker (0600). Never raises."""
    _measure_facade = _compat.facade()
    p = _measure_facade._subagent_cache_marker_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = _measure_facade.tempfile.mkstemp(
            prefix=".subagent_cache.", suffix=".tmp", dir=str(p.parent))
        try:
            if hasattr(_measure_facade.os, "fchmod"):
                _measure_facade.os.fchmod(fd, 0o600)
            with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as fh:
                _measure_facade.json.dump(record, fh)
            _measure_facade.os.replace(tmp_name, str(p))
        except Exception:
            try:
                _measure_facade.os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception:
        pass


def _subagent_cache_optout(value=None):
    """True when the opt-out env carries a falsy token (0/false/off/no)."""
    _measure_facade = _compat.facade()
    if value is None:
        value = _measure_facade.os.environ.get(_measure_facade._SUBAGENT_CACHE_OPTOUT_ENV)
    if value is None:
        return False
    return str(value).strip().lower() in ("0", "false", "off", "no")


def _subagent_cache_force_on(value=None):
    """True when the env carries a truthy token (1/true/on/yes): the automatic
    path then enables WITHOUT the evidence gate ("always on")."""
    _measure_facade = _compat.facade()
    if value is None:
        value = _measure_facade.os.environ.get(_measure_facade._SUBAGENT_CACHE_OPTOUT_ENV)
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "on", "yes")


def _subagent_cache_claude_code_version():
    """Detect the Claude Code version as a (major, minor, patch) tuple.

    Same probe the health collection uses: `claude --version` via
    _resolve_runtime_bin (shutil.which first, then the common install
    locations), parsed with _parse_semver. Returns None when nothing
    parseable answers -- callers treat that as "version unknown" and skip
    (fail-closed: never set a setting on an unverifiable host).
    Memoized per process; the SessionStart path hits the memo on every later
    call, and the marker check skips the probe entirely after first success.
    """
    _measure_facade = _compat.facade()
    pass
    if _measure_facade._SUBAGENT_CACHE_CC_VERSION is not _measure_facade._SUBAGENT_CACHE_UNKNOWN:
        return _measure_facade._SUBAGENT_CACHE_CC_VERSION
    try:
        result = _measure_facade.subprocess.run(
            [_measure_facade._resolve_runtime_bin("claude"), "--version"],
            capture_output=True, text=True, timeout=3, creationflags=_measure_facade._NO_WINDOW,
        )
        raw = result.stdout.strip() if result.returncode == 0 else ""
    except (_measure_facade.subprocess.SubprocessError, FileNotFoundError, OSError):
        raw = ""
    m = _measure_facade.re.search(r"\d+\.\d+\.\d+", raw or "")
    _measure_facade._SUBAGENT_CACHE_CC_VERSION = (
        _measure_facade._parse_semver(m.group(0)) if m else _measure_facade._SUBAGENT_CACHE_UNKNOWN)
    return _measure_facade._SUBAGENT_CACHE_CC_VERSION


def _subagent_cache_managed_settings_path():
    """Claude Code's managed-settings.json location for this platform."""
    _measure_facade = _compat.facade()
    system = _measure_facade.platform.system()
    if system == "Windows":
        base = _measure_facade.os.environ.get("ProgramData") or "C:\\ProgramData"
        return _measure_facade.Path(base) / "ClaudeCode" / "managed-settings.json"
    if system == "Darwin":
        return _measure_facade.Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    return _measure_facade.Path("/etc/claude-code/managed-settings.json")


def _subagent_cache_external_settings_sources():
    """Every settings file that outranks the user settings.json for this key.

    Managed (platform path), project (`.claude/settings.json` in the cwd) and
    local (`.claude/settings.local.json`). Claude Code merges all of them and
    the user file ranks LAST, so if any of these already answers the key,
    writing the user file would be a silent no-op -- do nothing instead.
    Returns Paths that may not exist (callers check).
    """
    _measure_facade = _compat.facade()
    cwd = _measure_facade.Path.cwd()
    return [
        _measure_facade._subagent_cache_managed_settings_path(),
        cwd / ".claude" / "settings.json",
        cwd / ".claude" / "settings.local.json",
    ]


def _subagent_cache_external_key_holder():
    """First external settings source that (maybe) sets the key.

    Returns {"path": str} when a READABLE file sets it, or
    {"path": str, "unreadable": True} when one exists but cannot be read
    (fail-closed: an unreadable higher-priority file means "unknown", and
    unknown never writes). None when every higher-priority file is absent
    or readable without the key.
    """
    _measure_facade = _compat.facade()
    for src in _measure_facade._subagent_cache_external_settings_sources():
        try:
            if not src.exists():
                continue
            if not _measure_facade._is_regular_file(src):
                # a FIFO would block read_text(); unknown means "do not write"
                return {"path": str(src), "unreadable": True}
            data = _measure_facade.json.loads(src.read_text(encoding="utf-8-sig"))
        except (_measure_facade.json.JSONDecodeError, PermissionError, OSError, ValueError):
            return {"path": str(src), "unreadable": True}
        if isinstance(data, dict) and _measure_facade._SUBAGENT_CACHE_KEY in data:
            return {"path": str(src)}
    return None


def _subagent_cache_claude_only():
    """The feature is Claude Code only. Cowork is a hard exclusion: it is
    detect_runtime()=="claude" but must never read or write ~/.claude."""
    _measure_facade = _compat.facade()
    try:
        if _measure_facade.detect_runtime() != "claude" or _measure_facade.is_cowork():
            return False
    except Exception:
        return False
    return True


def _subagent_cache_platform_gap(reason=None):
    _measure_facade = _compat.facade()
    return {"state": "platform-gap", "changed": False, "reason": reason,
            "notice": None}


def _subagent_cache_undo(data, now, why, user_initiated=None):
    """Shared undo: remove the key (only) and record why in the marker.

    Caller guarantees the marker says TO set it. `data` is the current
    settings dict. `user_initiated` marks an explicit user command so the
    settings lease must not lose to a herd writer's tombstone.
    Never raises; a refused write leaves everything as-is.
    """
    _measure_facade = _compat.facade()
    if _measure_facade._SUBAGENT_CACHE_KEY not in data:
        return {"state": why, "changed": False, "reason": "key already absent",
                "notice": None}
    if data.get(_measure_facade._SUBAGENT_CACHE_KEY) != "1h":
        return {"state": "user-changed", "changed": False,
                "reason": "value is no longer \"1h\"; leaving it alone",
                "notice": None}
    payload = dict(data)
    payload.pop(_measure_facade._SUBAGENT_CACHE_KEY, None)
    if not _measure_facade._write_settings_atomic(payload, allow_removing_keys={_measure_facade._SUBAGENT_CACHE_KEY},
                                  user_initiated=user_initiated):
        return {"state": "write-refused", "changed": False,
                "reason": _measure_facade._settings_write_refusal_reason(),
                "notice": None}
    _measure_facade._subagent_cache_write_marker({
        "state": why,
        "ts": float(now),
        "set_by": "token-optimizer",
    })
    return {"state": why, "changed": True, "reason": None, "notice": None}


def _subagent_cache_verdict_path():
    _measure_facade = _compat.facade()
    return _measure_facade.SNAPSHOT_DIR / _measure_facade._SUBAGENT_CACHE_VERDICT_NAME


def _subagent_cache_scan_lock_path():
    _measure_facade = _compat.facade()
    return _measure_facade.SNAPSHOT_DIR / _measure_facade._SUBAGENT_CACHE_SCAN_LOCK_NAME


def _subagent_cache_write_json_atomic(path, record, prefix):
    """Atomic 0600 JSON write next to `path`. Never raises."""
    _measure_facade = _compat.facade()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = _measure_facade.tempfile.mkstemp(prefix=prefix, suffix=".tmp",
                                        dir=str(path.parent))
        try:
            if hasattr(_measure_facade.os, "fchmod"):
                _measure_facade.os.fchmod(fd, 0o600)
            with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as fh:
                _measure_facade.json.dump(record, fh)
            _measure_facade.os.replace(tmp_name, str(path))
        except Exception:
            try:
                _measure_facade.os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception:
        pass


def _subagent_cache_read_verdict():
    """The cached scan verdict, or None when absent/corrupt."""
    _measure_facade = _compat.facade()
    try:
        data = _measure_facade.json.loads(_measure_facade._subagent_cache_verdict_path().read_text(encoding="utf-8"))
    except (_measure_facade.json.JSONDecodeError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _subagent_cache_verdict_for(now, since_ts=None):
    """("fresh"|"stale"|"missing", record) for the window `since_ts`.

    `since_ts` None is the plain last-30-days verdict the automatic enable
    judges; a float is the tripwire's post-enable window. A verdict computed
    for another window counts as missing."""
    _measure_facade = _compat.facade()
    rec = _measure_facade._subagent_cache_read_verdict()
    if rec is None:
        return "missing", None
    have = rec.get("since_ts")
    try:
        same_window = ((have is None and since_ts is None)
                       or (have is not None and since_ts is not None
                           and abs(float(have) - float(since_ts)) < 1e-6))
        age = float(now) - float(rec.get("ts"))
    except (TypeError, ValueError):
        return "missing", None
    if not same_window:
        return "missing", None
    if age > _measure_facade._SUBAGENT_CACHE_VERDICT_MAX_AGE or age < -3600:
        return "stale", rec
    return "fresh", rec


def _reclaim_stale_lock_file(lock, stale_seconds, now):
    """Free an abandoned O_EXCL lock without ever deleting a live successor.

    Returns True when the pathname is free (stale lock removed, or already
    gone) so the caller retries the create, False when the lock is live or
    cannot be moved. A bare ``unlink`` after an age check can delete the lock a
    faster process just re-created, and then two processes both think they hold
    it. So the stale file is renamed to a unique name first (atomic: it takes
    whatever sits at the path at that instant), identity-checked against what
    the age check saw, and only then unlinked. If the rename grabbed a live
    successor instead, it is linked back (no-replace) and left alone.
    """
    _measure_facade = _compat.facade()
    try:
        seen = lock.stat()
    except OSError:
        return True
    if float(now) - seen.st_mtime < stale_seconds:
        return False
    victim = lock.with_name(f"{lock.name}.stale-{_measure_facade.os.urandom(6).hex()}")
    try:
        _measure_facade.os.rename(str(lock), str(victim))
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        moved = _measure_facade.os.stat(str(victim))
        ours = (_measure_facade.os.path.samestat(seen, moved)
                and moved.st_mtime_ns == seen.st_mtime_ns)
    except OSError:
        ours = False
    if not ours:
        try:
            _measure_facade.os.link(str(victim), str(lock))
        except OSError:
            pass
    try:
        _measure_facade.os.unlink(str(victim))
    except OSError:
        pass
    return bool(ours)


def _subagent_cache_scan_acquire_lock(now=None):
    """Take the scan lock (O_EXCL); its owner token, or None when it is held.

    A lock older than _SUBAGENT_CACHE_SCAN_LOCK_STALE is abandoned (a crashed
    child) and is reclaimed."""
    _measure_facade = _compat.facade()
    if now is None:
        now = _measure_facade.time.time()
    lock = _measure_facade._subagent_cache_scan_lock_path()
    token = _measure_facade.os.urandom(16).hex()
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in (1, 2):
            try:
                fd = _measure_facade.os.open(str(lock), _measure_facade.os.O_CREAT | _measure_facade.os.O_EXCL | _measure_facade.os.O_WRONLY, 0o600)
            except FileExistsError:
                if not _measure_facade._reclaim_stale_lock_file(
                        lock, _measure_facade._SUBAGENT_CACHE_SCAN_LOCK_STALE, now):
                    return None
                continue
            try:
                _measure_facade.os.write(fd, token.encode("ascii"))
            finally:
                _measure_facade.os.close(fd)
            return token
    except OSError:
        pass
    return None


def _subagent_cache_scan_release_lock(token):
    """Delete the lock only while it still holds `token`. Never raises."""
    _measure_facade = _compat.facade()
    if not token:
        return
    lock = _measure_facade._subagent_cache_scan_lock_path()
    try:
        if lock.read_text(encoding="ascii").strip() == token:
            lock.unlink()
    except OSError:
        pass


def subagent_cache_scan_run(now=None, since_ts=None, time_budget=None,
                            max_files=None, token=None):
    """The background worker: scan once, write the verdict file, free the lock.

    Run by the detached child (`measure.py subagent-cache scan`), which gets
    the lock token from its spawner; run by hand it takes the lock itself and
    returns None when another scan holds it. A partial scan (time budget or
    file cap) or a crash writes an INCOMPLETE record: dated, so the next scan
    is a day away, but with no payoff, so nothing is ever decided from it.
    Returns the record written, or None when it did not run.
    """
    _measure_facade = _compat.facade()
    if not _measure_facade._subagent_cache_claude_only():
        return None
    if now is None:
        now = _measure_facade.time.time()
    if token is None:
        token = _measure_facade._subagent_cache_scan_acquire_lock(now)
        if token is None:
            return None
    budget = _measure_facade._SUBAGENT_CACHE_SCAN_BUDGET_SECONDS if time_budget is None else time_budget
    cap = _measure_facade._SUBAGENT_CACHE_SCAN_MAX_FILES if max_files is None else max_files
    rec = {"version": 1, "ts": float(now), "complete": False,
           "since_ts": None if since_ts is None else float(since_ts),
           "window_days": 30, "payoff": None, "reason": None}
    try:
        payoff = _measure_facade.subagent_cache_payoff(days=30, now=now, since_ts=since_ts,
                                       time_budget=budget, max_files=cap)
        if payoff.get("partial"):
            rec["reason"] = payoff.get("partial_reason") or "partial scan"
        else:
            rec["complete"] = True
            rec["payoff"] = payoff
    except Exception as exc:
        rec["reason"] = f"scan error: {type(exc).__name__}"
    finally:
        _measure_facade._subagent_cache_write_json_atomic(
            _measure_facade._subagent_cache_verdict_path(), rec, ".subagent_verdict.")
        _measure_facade._subagent_cache_scan_release_lock(token)
    return rec


def _subagent_cache_spawn_scan(now=None, since_ts=None):
    """Start the scan as a detached background process. True when started.

    The lock is taken HERE (so two session starts can never both spawn) and
    handed to the child by environment token. spawn_detached owns the Windows
    flags (no console window); the interpreter is the GUI twin on Windows.
    Never raises; a failed spawn frees the lock.
    """
    _measure_facade = _compat.facade()
    token = _measure_facade._subagent_cache_scan_acquire_lock(now)
    if token is None:
        return False
    try:
        argv = [_measure_facade._detached_python_exe(), str(_measure_facade.MEASURE_PY_PATH), "subagent-cache", "scan"]
        if since_ts is not None:
            argv += ["--since", repr(float(since_ts))]
        env = _measure_facade.os.environ.copy()
        env["TOKEN_OPTIMIZER_RUNTIME"] = _measure_facade.detect_runtime()
        env.pop("TOKEN_OPTIMIZER_HOOK", None)
        env[_measure_facade._SUBAGENT_CACHE_SCAN_TOKEN_ENV] = token
        proc = _measure_facade.spawn_detached(
            argv,
            stdin=_measure_facade.subprocess.DEVNULL,
            stdout=_measure_facade.subprocess.DEVNULL,
            stderr=_measure_facade.subprocess.DEVNULL,
            env=env,
            close_fds=True,
        )
    except Exception:
        proc = None
    if proc is None:
        _measure_facade._subagent_cache_scan_release_lock(token)
        try:
            _measure_facade._log_spawn_failure("subagent-cache payoff scan spawn failed")
        except Exception:
            pass
        return False
    return True


def _subagent_cache_judge_payoff(payoff):
    """Pure judgement of a payoff window: {"decision", "reason", ("tokens")}.

    recommend       >= MIN_REQUESTS subagent requests AND saved >= MARGIN x premium
    not-enough-data fewer requests than that
    would-not-pay   enough requests, but the saving does not clear the margin

    "recommend" used to be "enable" (the automatic write is gone);
    it now only feeds the advice line and the recorded verdict.
    """
    _measure_facade = _compat.facade()
    payoff = payoff or {}
    n = int(payoff.get("subagent_requests") or 0)
    days = int(payoff.get("window_days") or 30)
    need = _measure_facade._SUBAGENT_CACHE_TRIPWIRE_MIN_REQUESTS
    if n < need:
        return {"decision": "not-enough-data",
                "reason": (f"not enough data: {n} subagent requests in the last "
                           f"{days} days, need {need}")}
    saved = float(payoff.get("savings_usd_est") or 0.0)
    premium = float(payoff.get("extra_write_cost_usd_est") or 0.0)
    if saved > 0 and saved >= _measure_facade._SUBAGENT_CACHE_AUTO_ENABLE_MARGIN * premium:
        tokens = (int(payoff.get("missed_read_tokens") or 0)
                  + int(payoff.get("realized_read_tokens") or 0))
        return {"decision": "recommend", "tokens": tokens, "days": days,
                "reason": f"pays: saved ${saved:.2f} vs premium ${premium:.2f}"}
    return {"decision": "would-not-pay",
            "reason": f"would not pay: saved ${saved:.2f} vs premium ${premium:.2f}"}


def _subagent_cache_record_decision(marker, decision, now):
    """Remember why the automatic path did (not) write, with a timestamp.

    Re-judged at most once a day: an identical decision on the same verdict
    stands and is not rewritten."""
    _measure_facade = _compat.facade()
    prior = (marker or {}).get("auto_decision") or {}
    try:
        if (prior.get("decision") == decision.get("decision")
                and prior.get("verdict_ts") == decision.get("verdict_ts")
                and float(now) - float(prior.get("ts")) < 86400):
            return
    except (TypeError, ValueError):
        pass
    record = dict(marker or {})
    record["auto_decision"] = dict(decision, ts=float(now))
    _measure_facade._subagent_cache_write_marker(record)


def _subagent_cache_auto_gate(marker, now):
    """Judge the cached verdict for the unset-key (last 30 days) window.

    ADVISE-ONLY: reads the cached verdict only and ALWAYS records the
    judgement in the marker -- a verdict says what the user's own numbers
    support, it is never applied to settings here. With no usable verdict
    (missing, stale, other window) it starts the detached scan and answers
    "pending": the verdict is judged at a later session start."""
    _measure_facade = _compat.facade()
    state, rec = _measure_facade._subagent_cache_verdict_for(now, None)
    if state == "fresh":
        if rec.get("complete") and isinstance(rec.get("payoff"), dict):
            decision = _measure_facade._subagent_cache_judge_payoff(rec["payoff"])
        else:
            decision = {"decision": "not-enough-data",
                        "reason": ("not enough data: the last payoff scan did "
                                   f"not finish ({rec.get('reason') or 'partial'})")}
        decision["verdict_ts"] = rec.get("ts")
    else:
        _measure_facade._subagent_cache_spawn_scan(now=now)
        decision = {"decision": "pending", "verdict_ts": None,
                    "reason": ("payoff scan runs in the background; it is "
                               "judged at a later session start")}
    _measure_facade._subagent_cache_record_decision(marker, decision, now)
    return decision


def subagent_cache_enable(now=None, automatic=None):
    """Set `subagentPromptCacheTtl: "1h"` in the USER settings.json -- once,
    and only under every safety rule in the module docstring.

    `automatic=True` is the SessionStart path. ADVISE-ONLY: with the
    force env unset it NEVER writes -- it judges + records the cached verdict
    and returns the decision as the state -- while still honouring the sticky
    marker states (user-declined / auto-reverted / opted-out / removed) so
    the host can never fight the user, the tripwire, or an explicit
    `disable`. TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H=1 is the only opt-in that
    lets this path write. `automatic=False` is the explicit CLI verb, where a
    deliberate human request may clear any marker state and create a missing
    settings.json (the env opt-out still always wins).

    Returns {"state", "changed", "reason", "notice"}. The notice is the
    one-line SessionStart message for the ONE transition where the key was
    actually set; every other path emits none.
    """
    _measure_facade = _compat.facade()
    if now is None:
        now = _measure_facade.time.time()
    if not _measure_facade._subagent_cache_claude_only():
        return _measure_facade._subagent_cache_platform_gap()

    # Opt-out env: never set; and if TO set it earlier, undo.
    if _measure_facade._subagent_cache_optout():
        marker = _measure_facade._subagent_cache_read_marker()
        data, ok = _measure_facade._read_settings_for_write()
        if ok and marker and marker.get("state") == "set":
            r = _measure_facade._subagent_cache_undo(data, now, "opted-out")
            r["state"] = "opted-out"
            return r
        return {"state": "opted-out", "changed": False,
                "reason": f"{_measure_facade._SUBAGENT_CACHE_OPTOUT_ENV} is set",
                "notice": None}

    marker = _measure_facade._subagent_cache_read_marker()
    mstate = (marker or {}).get("state")

    if automatic and mstate in ("user-declined", "auto-reverted", "opted-out",
                                "removed"):
        return {"state": mstate, "changed": False,
                "reason": "sticky marker state; the automatic path never "
                          "re-sets after a decline, an auto-revert, or a "
                          "manual disable",
                "notice": None}

    if mstate == "set":
        # Fast path: marker first (one file read), settings only to verify.
        # An explicit enable reads with allow_missing so a vanished file is
        # recorded as a decline instead of refusing as "unknown".
        data, ok = _measure_facade._read_settings_for_write(allow_missing=not automatic)
        if not ok:
            return {"state": "unknown-settings", "changed": False,
                    "reason": "settings.json unreadable or missing", "notice": None}
        val = data.get(_measure_facade._SUBAGENT_CACHE_KEY)
        if val == "1h":
            return {"state": "set", "changed": False, "reason": None,
                    "notice": None, "current": "1h"}
        # We set it once and the user removed/changed it afterwards: remember,
        # never touch it again. Only the automatic path records the decline;
        # an explicit `enable` is a deliberate request and falls through to
        # the normal set path below.
        if automatic:
            marker["state"] = "user-declined"
            marker["declined_ts"] = float(now)
            marker["declined_previous"] = val if val is not None else "(absent)"
            _measure_facade._subagent_cache_write_marker(marker)
            return {"state": "user-declined", "changed": False,
                    "reason": "the user removed or changed the key after we set it",
                    "notice": None}

    # Env outranks the setting (documented precedence).
    if _measure_facade.os.environ.get("CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL", "").strip():
        return {"state": "env-override", "changed": False,
                "reason": "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL is set", "notice": None}
    if _measure_facade.os.environ.get("FORCE_PROMPT_CACHING_5M", "").strip():
        return {"state": "env-override", "changed": False,
                "reason": "FORCE_PROMPT_CACHING_5M is set", "notice": None}

    # A higher-priority settings file already answers the key.
    holder = _measure_facade._subagent_cache_external_key_holder()
    if holder is not None:
        if holder.get("unreadable"):
            return {"state": "unknown-settings", "changed": False,
                    "reason": (f"external settings file unreadable: {holder['path']}"),
                    "notice": None}
        return {"state": "external-setting", "changed": False,
                "reason": f"{holder['path']} already sets the key", "notice": None}

    # Unknown-state user settings: never write. An explicit `enable` is the
    # one exception to "missing = unknown": a user who ran the command on a
    # machine with no settings.json wants the file created.
    data, ok = _measure_facade._read_settings_for_write(allow_missing=not automatic)
    if not ok:
        return {"state": "unknown-settings", "changed": False,
                "reason": "settings.json unreadable or missing", "notice": None}
    settings_missing = not _measure_facade.SETTINGS_PATH.exists()

    # Any value the user already set -- including "5m" -- is theirs.
    if _measure_facade._SUBAGENT_CACHE_KEY in data:
        _measure_facade._subagent_cache_write_marker({
            "state": "user-set",
            "ts": float(now),
            "set_by": "user",
        })
        return {"state": "user-set", "changed": False,
                "reason": "the user already set the key", "notice": None,
                "current": data.get(_measure_facade._SUBAGENT_CACHE_KEY)}

    # What may write, in order of strength: an explicit `subagent-cache
    # enable` is unconditional; the opt-in TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H=1
    # is the only automatic write (and the only path that also keeps the
    # 14-day tripwire). Everything else is ADVISE-ONLY: judge + record
    # the cached verdict and return the decision -- Claude Code keeps
    # subagents at 5 minutes because 1h costs more on average for
    # agent-shaped work, and editing a user's settings on an estimate is not
    # ok.
    if not automatic:
        decision = {"decision": "manual",
                    "reason": "enabled by the subagent-cache enable command"}
    elif _measure_facade._subagent_cache_force_on():
        decision = {"decision": "forced-on",
                    "reason": f"{_measure_facade._SUBAGENT_CACHE_OPTOUT_ENV}=1 (always on)"}
    else:
        decision = _measure_facade._subagent_cache_auto_gate(marker, now)
        return {"state": decision["decision"], "changed": False,
                "reason": decision["reason"], "notice": None}

    # Version floor (a `claude --version` subprocess: probed only once every
    # cheaper check has said a write is really about to happen).
    version = _measure_facade._subagent_cache_claude_code_version()
    if version is None or version < _measure_facade._SUBAGENT_CACHE_CC_FLOOR:
        return {"state": "version-unsupported", "changed": False,
                "reason": ("Claude Code version unknown" if version is None else
                           f"Claude Code {'.'.join(map(str, version))} < "
                           f"{'.'.join(map(str, _measure_facade._SUBAGENT_CACHE_CC_FLOOR))}"),
                "notice": None}

    # The only write this feature ever makes: an explicit command, or the
    # force-env opt-in. user_initiated lets the explicit command take the
    # settings lease past a herd writer's tombstone.
    payload = dict(data)
    payload[_measure_facade._SUBAGENT_CACHE_KEY] = "1h"
    if not _measure_facade._write_settings_atomic(payload, user_initiated=not automatic):
        return {"state": "write-refused", "changed": False,
                "reason": _measure_facade._settings_write_refusal_reason(),
                "notice": None}
    _measure_facade._subagent_cache_write_marker({
        "state": "set",
        "set_ts": float(now),
        "previous": {"present": False},
        "set_by": "token-optimizer",
        "auto_decision": dict(decision, ts=float(now)),
    })
    undo_cmd = _measure_facade._subagent_cache_cmd("disable")
    what = "Token Optimizer set the subagent cache to 1 hour (was 5 minutes)."
    if settings_missing:
        what += " Created settings.json -- it did not exist."
    return {
        "state": "set",
        "changed": True,
        "reason": None,
        "notice": f"{what} Undo: {undo_cmd}",
        "created_settings": settings_missing,
    }


def subagent_cache_disable(now=None):
    """Remove the key -- ONLY when the marker says TO set it and the value is
    still "1h". The sanctioned undo; also called by cleanup()/uninstall.
    Final: the "removed" marker is sticky for every automatic path."""
    _measure_facade = _compat.facade()
    if now is None:
        now = _measure_facade.time.time()
    if not _measure_facade._subagent_cache_claude_only():
        return _measure_facade._subagent_cache_platform_gap()
    marker = _measure_facade._subagent_cache_read_marker()
    if not marker or marker.get("state") != "set":
        return {"state": "nothing-to-undo", "changed": False,
                "reason": "no marker says Token Optimizer set this key",
                "notice": None}
    data, ok = _measure_facade._read_settings_for_write()
    if not ok:
        return {"state": "unknown-settings", "changed": False,
                "reason": "settings.json unreadable or missing", "notice": None}
    return _measure_facade._subagent_cache_undo(data, now, "removed", user_initiated=True)


def _subagent_cache_payoff_zero(days):
    _measure_facade = _compat.facade()
    return {
        "window_days": int(days), "estimate": True,
        "subagent_requests": 0,
        # Counterfactual part: requests that wrote at 5m (what 1h would change).
        "subagent_5m_cache_writes": 0, "write_tokens_5m": 0,
        "would_have_been_reads": 0, "missed_read_tokens": 0,
        "within_agent_tokens": 0, "across_spawn_tokens": 0,
        # Realized part: requests that already wrote at 1h (setting is on).
        "subagent_1h_cache_writes": 0, "write_tokens_1h": 0,
        "realized_reads": 0, "realized_read_tokens": 0,
        "savings_usd_est": 0.0, "extra_write_cost_usd_est": 0.0,
        "net_usd_est": 0.0,
        "spawns": 0, "groups": 0, "groups_with_shared_prefix": 0,
        "agent_type_recorded_spawns": 0,
        "grouping": "project+model (agent type not recorded in these transcripts)",
        "partial": False,
    }


def subagent_cache_payoff(days=None, now=None, since_ts=None,
                          time_budget=None, max_files=None):
    """Deterministic payoff estimate from the user's OWN sidechain transcripts.

    One function for a history that mixes both regimes. A request is read by
    what it wrote:

    * 5m regime (cc_5m > 0): what a 1h TTL WOULD change. The 1h write premium
      applies to every such token (extra cost); avoided rewrites are credited
      as reads (savings) in two disjoint parts, never both for one request:
        - within_agent_tokens: a non-first request of a transcript whose gap
          to the previous request of that transcript is 5-60 min (its cc_5m
          is a rewrite 1h would have kept alive);
        - across_spawn_tokens: the FIRST request of a spawn that follows the
          latest activity of its group (project + agent type + model; the
          agent type comes from the transcript's .meta.json when Claude Code
          wrote one, else the group is project + model) by 5-60 min, credited
          min(its cc_5m, shared_prefix_est). shared_prefix_est = smallest
          (cache_read + cache_creation) of the first request over the group's
          spawns; a group needs 2+ spawns, else 0.
    * 1h regime (cc_1h > 0, or a read-only request inheriting the group's
      latest write regime): what the setting REALLY did. The premium actually
      paid is (1h write - 5m write) on the cc_1h tokens; the benefit realized
      is cache READS on a request that follows a 5-60 min gap (within the
      transcript, or the first request of a spawn after its group), because
      at 5m those would have been writes: (5m write - read) on those reads.

    Priced with the active tier's rate card via _get_model_cost (unpriced
    models fall back to the runtime default, like every other pool).
    `since_ts` restricts the token accounting to requests at/after that epoch
    second (the tripwire's post-enable window); earlier requests still serve
    as context (previous activity, shared prefix). Everything here is an
    ESTIMATE: the gap proxy reads request timestamps, not actual cache keys.

    `time_budget` (seconds) and `max_files` bound the scan for the background
    worker: running out of either returns a zero payoff with `partial: True`
    and the reason in `partial_reason` -- a partial scan is NOT a verdict.
    Without them the scan is the explicit-command path (silent cap at
    _SUBAGENT_SCAN_MAX_FILES, no clock).
    Never raises; a scan error is an honest zero.
    """
    _measure_facade = _compat.facade()
    zero = _measure_facade._subagent_cache_payoff_zero(days)
    if not _measure_facade._subagent_cache_claude_only():
        return zero
    if now is None:
        now = _measure_facade.time.time()
    deadline = (None if time_budget is None
                else _measure_facade.time.monotonic() + float(time_budget))

    def _partial(why):
        _measure_facade = _compat.facade()
        return dict(zero, partial=True, partial_reason=why)

    def _out_of_time():
        _measure_facade = _compat.facade()
        return deadline is not None and _measure_facade.time.monotonic() >= deadline

    try:
        projects_base = _measure_facade.CLAUDE_DIR / "projects"
        if not projects_base.exists():
            return zero
        cutoff_ts = now - days * 86400

        candidates = []
        for project_dir in projects_base.iterdir():
            if not project_dir.is_dir():
                continue
            if _out_of_time():
                return _partial("time budget exceeded")
            try:
                for jf in project_dir.rglob("*.jsonl"):
                    try:
                        mtime = jf.stat().st_mtime
                    except OSError:
                        continue
                    if mtime >= cutoff_ts:
                        candidates.append((mtime, jf, project_dir.name))
            except OSError:
                continue
        if max_files is not None and len(candidates) > max_files:
            return _partial(f"file cap exceeded ({len(candidates)} > {max_files})")
        candidates.sort(key=lambda t: ((_measure_facade := _compat.facade()), t[0])[1], reverse=True)
        candidates = candidates[:_measure_facade._SUBAGENT_SCAN_MAX_FILES if max_files is None
                                else max_files]

        tier = _measure_facade._load_pricing_tier()

        def _price(model, tokens):
            _measure_facade = _compat.facade()
            mdl = model if _measure_facade._is_priced_model(model, tier) else _measure_facade._default_model_for_runtime()
            write_5m = _measure_facade._get_model_cost(mdl, 0, 0, 0, int(tokens), tier=tier,
                                       cache_create_1h=0, cache_create_5m=int(tokens))
            read = _measure_facade._get_model_cost(mdl, 0, 0, int(tokens), 0, tier=tier)
            write_1h = _measure_facade._get_model_cost(mdl, 0, 0, 0, int(tokens), tier=tier,
                                       cache_create_1h=int(tokens), cache_create_5m=0)
            return write_5m, read, write_1h

        # 1. Collect spawns (one sidechain transcript = one spawn) into groups.
        groups = {}
        spawn_count = 0
        recorded_count = 0
        for _mt, jf, project in candidates:
            if _out_of_time():
                return _partial("time budget exceeded")
            # Sidechain transcripts only (same rule as _subagent_pool_savings:
            # nested subagents/ paths are sidechains by construction).
            if jf.parent.name != "subagents" and not _measure_facade._scan_jsonl_is_sidechain(jf):
                continue
            parsed = _measure_facade._parse_session_jsonl(jf)
            if not parsed or not parsed.get("is_sidechain"):
                continue
            reqs = []
            for u in (parsed.get("request_usage") or {}).values():
                ts_s = u.get("ts")
                if u.get("advisor") or not ts_s or not isinstance(ts_s, str):
                    # Advisor parts run on the advisor's own cache, not this
                    # subagent's prefix.
                    continue
                try:
                    ts = _measure_facade.datetime.fromisoformat(ts_s.replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError, OverflowError, OSError):
                    continue
                cc5 = int(u.get("cc_5m") or 0)
                cc1 = int(u.get("cc_1h") or 0)
                cr = int(u.get("cr") or 0)
                reqs.append({
                    "ts": ts, "cc5": cc5, "cc1": cc1, "cr": cr,
                    "cc": max(int(u.get("cc") or 0), cc5 + cc1),
                    "model": u.get("model"),
                })
            if not reqs:
                continue
            reqs.sort(key=lambda r: ((_measure_facade := _compat.facade()), r["ts"])[1])
            agent = _measure_facade._extract_agent_type(jf)
            recorded = agent != "unknown"
            spawn_count += 1
            recorded_count += 1 if recorded else 0
            model = next((r["model"] for r in reqs if r["model"]), None) or "unknown"
            gkey = (project, agent if recorded else "", model)
            groups.setdefault(gkey, []).append(reqs)

        # 2. Walk each group chronologically.
        total_requests = 0
        writes_5m = tokens_5m = 0
        would_reads = 0
        within_tokens = across_tokens = 0
        writes_1h = tokens_1h = 0
        realized_reads = realized_tokens = 0
        savings_usd = 0.0
        extra_usd = 0.0
        shared_groups = 0

        def _counted(ts):
            _measure_facade = _compat.facade()
            return ts >= cutoff_ts and (since_ts is None or ts >= since_ts)

        for spawns in groups.values():
            firsts = [sp[0]["cr"] + sp[0]["cc"] for sp in spawns]
            shared = min(firsts) if len(spawns) >= 2 else 0
            if shared > 0:
                shared_groups += 1
            events = []
            for si, sp in enumerate(spawns):
                for ri, r in enumerate(sp):
                    events.append((r["ts"], si, ri))
            events.sort()
            latest_activity = None     # latest request ts seen so far in the group
            last_write_regime = None   # "1h" / "5m": regime of the latest cache write
            for ts, si, ri in events:
                r = spawns[si][ri]
                is_first = ri == 0
                prev_ts = latest_activity if is_first else spawns[si][ri - 1]["ts"]
                gap = (ts - prev_ts) if prev_ts is not None else None
                in_gap = (gap is not None
                          and _measure_facade._SUBAGENT_CACHE_GAP_LOW <= gap <= _measure_facade._SUBAGENT_CACHE_GAP_HIGH)
                cc5, cc1, cr = r["cc5"], r["cc1"], r["cr"]
                if r["cc5"] > 0 or r["cc1"] > 0:
                    regime_1h = cc1 > 0
                else:
                    regime_1h = last_write_regime == "1h"
                if _counted(ts):
                    if cc5 > 0 or cc1 > 0 or cr > 0:
                        total_requests += 1
                    if cc5 > 0:
                        writes_5m += 1
                        tokens_5m += cc5
                        w5, rd, w1 = _price(r["model"], cc5)
                        extra_usd += w1 - w5
                        if in_gap:
                            credited = min(cc5, shared) if is_first else cc5
                            if credited > 0:
                                would_reads += 1
                                cw5, crd, _cw1 = _price(r["model"], credited)
                                savings_usd += cw5 - crd
                                if is_first:
                                    across_tokens += credited
                                else:
                                    within_tokens += credited
                    if cc1 > 0:
                        writes_1h += 1
                        tokens_1h += cc1
                        w5, rd, w1 = _price(r["model"], cc1)
                        extra_usd += w1 - w5
                    if regime_1h and cr > 0 and in_gap:
                        realized_reads += 1
                        realized_tokens += cr
                        w5, rd, _w1 = _price(r["model"], cr)
                        savings_usd += w5 - rd
                if cc5 > 0 or cc1 > 0:
                    last_write_regime = "1h" if cc1 > 0 else "5m"
                latest_activity = ts if latest_activity is None else max(latest_activity, ts)

        if spawn_count == 0:
            agent_note = zero["grouping"]
        elif recorded_count == spawn_count:
            agent_note = "project+agent type+model"
        elif recorded_count == 0:
            agent_note = "project+model (agent type not recorded in these transcripts)"
        else:
            agent_note = (
                "project+agent type+model (agent type not recorded for "
                f"{spawn_count - recorded_count} of {spawn_count} spawns; "
                "those are grouped by project+model)")
        return {
            "window_days": int(days), "estimate": True,
            "subagent_requests": total_requests,
            "subagent_5m_cache_writes": writes_5m,
            "write_tokens_5m": tokens_5m,
            "would_have_been_reads": would_reads,
            "missed_read_tokens": within_tokens + across_tokens,
            "within_agent_tokens": within_tokens,
            "across_spawn_tokens": across_tokens,
            "subagent_1h_cache_writes": writes_1h,
            "write_tokens_1h": tokens_1h,
            "realized_reads": realized_reads,
            "realized_read_tokens": realized_tokens,
            "savings_usd_est": round(savings_usd, 6),
            "extra_write_cost_usd_est": round(extra_usd, 6),
            "net_usd_est": round(savings_usd - extra_usd, 6),
            "spawns": spawn_count,
            "groups": len(groups),
            "groups_with_shared_prefix": shared_groups,
            "agent_type_recorded_spawns": recorded_count,
            "grouping": agent_note,
            "partial": False,
        }
    except Exception:
        return zero


def evaluate_subagent_cache_tripwire(now=None, payoff=None):
    """14+ days of post-enable data with a NEGATIVE net estimate -> revert.

    Only runs under the TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H=1 opt-in:
    the advise-only default never calls it, so a TO-set key is never removed
    silently -- a negative window only feeds the "is costing ... turn off"
    advice.

    Modeled on the keep-warm tripwire: the only write it ever makes is the
    undo of a value TO itself set (marker state "set"); a user-set value is
    never touched; a window with fewer than _SUBAGENT_CACHE_TRIPWIRE_MIN_REQUESTS
    subagent requests is never judged ("not enough data"). It judges the CACHED
    verdict of the background scan (post-enable window), never a scan of its
    own: SessionStart only reads. The net it judges
    mixes both regimes (see subagent_cache_payoff): with the setting on, the
    premium actually paid on 1h writes against the reads actually realized.
    The revert is sticky for the automatic path ("auto-reverted" marker
    state); an explicit `subagent-cache enable` clears it.

    Returns {"reverted": bool, "notice": str|None, "net_usd_est": ...,
    "reason": str|None}. Never raises.
    """
    _measure_facade = _compat.facade()
    out = {"reverted": False, "notice": None, "net_usd_est": None, "reason": None}
    if not _measure_facade._subagent_cache_claude_only():
        return out
    if now is None:
        now = _measure_facade.time.time()
    marker = _measure_facade._subagent_cache_read_marker()
    if not marker or marker.get("state") != "set":
        return out
    set_ts = marker.get("set_ts")
    try:
        elapsed_days = (float(now) - float(set_ts)) / 86400.0
    except (TypeError, ValueError):
        return out
    if elapsed_days < _measure_facade._SUBAGENT_CACHE_TRIPWIRE_MIN_DAYS:
        return out
    # Re-judge at most once per _SUBAGENT_CACHE_TRIPWIRE_REJUDGE_SECONDS:
    # the payoff scan below walks real transcripts, and the steady-state
    # session start must stay a marker read, not a 30-day scan.
    judged_ts = marker.get("judged_ts")
    try:
        if (judged_ts is not None
                and float(now) - float(judged_ts)
                < _measure_facade._SUBAGENT_CACHE_TRIPWIRE_REJUDGE_SECONDS):
            return out
    except (TypeError, ValueError):
        pass
    def _record_judgment():
        _measure_facade = _compat.facade()
        marker["judged_ts"] = float(now)
        _measure_facade._subagent_cache_write_marker(marker)

    if payoff is None:
        # The scan walks real transcripts: never here. Read the cached verdict
        # for the post-enable window; with none, start the detached scan and
        # judge at a later session start (nothing is stamped as judged yet).
        vstate, rec = _measure_facade._subagent_cache_verdict_for(now, float(set_ts))
        if vstate != "fresh":
            _measure_facade._subagent_cache_spawn_scan(now=now, since_ts=float(set_ts))
            return dict(out, reason=("payoff scan runs in the background; "
                                     "judged at a later session start"))
        if not rec.get("complete") or not isinstance(rec.get("payoff"), dict):
            _record_judgment()
            return dict(out, reason=(
                "not enough data: the last payoff scan did not finish "
                f"({rec.get('reason') or 'partial'})"))
        payoff = rec["payoff"]

    # A thin window proves nothing either way.
    sample = (payoff or {}).get("subagent_requests") or 0
    if sample < _measure_facade._SUBAGENT_CACHE_TRIPWIRE_MIN_REQUESTS:
        _record_judgment()
        return dict(out, reason=(
            f"not enough data: {sample} subagent requests since enabling, "
            f"need {_measure_facade._SUBAGENT_CACHE_TRIPWIRE_MIN_REQUESTS}"))
    net = payoff.get("net_usd_est")
    if not isinstance(net, (int, float)) or net >= 0:
        # Judged and kept: report the net that earned the stay (or None when
        # the payoff was unparseable).
        _record_judgment()
        return dict(out, net_usd_est=net if isinstance(net, (int, float)) else None)
    # Net negative over a full window: undo OUR value only.
    data, ok = _measure_facade._read_settings_for_write()
    if not ok:
        return out
    result = _measure_facade._subagent_cache_undo(data, now, "auto-reverted")
    if not result.get("changed"):
        return dict(out, net_usd_est=net)
    enable_cmd = _measure_facade._subagent_cache_cmd("enable")
    return {
        "reverted": True,
        "net_usd_est": net,
        "notice": (
            "Token Optimizer removed the subagent 1-hour cache setting: the "
            f"estimated net over the last {int(elapsed_days)} days was "
            f"-${abs(net):.2f} (the 1-hour write premium outweighed the "
            "5-minute rewrites it avoided). Set again: "
            f"{enable_cmd}"
        ),
    }


def _subagent_cache_session_start_lines(now=None):
    """SessionStart ensure body. ADVISE-ONLY by default.

    With TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H unset this NEVER writes settings
    and never emits a notice: subagent_cache_enable() still owns the env
    opt-out undo, the sticky marker states, and user-declined detection, and
    for an unset key its gate judges + records the cached verdict (spawning
    the detached scan when there is no fresh one). For a key that is already
    "1h" -- ours or the user's -- the same bookkeeping keeps the right
    verdict window fresh so status/doctor can say "is costing / is paying".

    TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H=1 is the explicit opt-in to the old
    automatic write plus the 14-day auto-revert tripwire, and the only path
    that may return a user-facing line. Never raises.
    """
    _measure_facade = _compat.facade()
    try:
        result = _measure_facade.subagent_cache_enable(now=now, automatic=True)
        if _measure_facade._subagent_cache_force_on():
            if result.get("notice"):
                return [result["notice"]]
            # The tripwire only rides the opted-in automation: advise-only
            # never removes a key it did set, and never touches the user's.
            trip = _measure_facade.evaluate_subagent_cache_tripwire(now=now)
            if trip.get("reverted") and trip.get("notice"):
                return [trip["notice"]]
            return []
        if result.get("state") == "set" or (
                result.get("state") == "user-set"
                and result.get("current") == "1h"):
            # A "1h" key already in place gets judged on its own window:
            # post-enable when the marker says TO set it, else the plain
            # last-30-days verdict. Fresh -> judge + record; missing/stale
            # -> spawn the detached scan and judge at a later session start.
            marker = _measure_facade._subagent_cache_read_marker()
            since = None
            if (marker or {}).get("state") == "set":
                try:
                    since = float(marker["set_ts"])
                except (TypeError, ValueError):
                    since = None
            vstate, rec = _measure_facade._subagent_cache_verdict_for(now, since)
            if vstate != "fresh":
                _measure_facade._subagent_cache_spawn_scan(now=now, since_ts=since)
            elif rec.get("complete") and isinstance(rec.get("payoff"), dict):
                decision = _measure_facade._subagent_cache_judge_payoff(rec["payoff"])
                decision["verdict_ts"] = rec.get("ts")
                _measure_facade._subagent_cache_record_decision(marker, decision, now)
    except Exception:
        pass
    return []


def _subagent_cache_recommendation(state, current, payoff, billing,
                                   post_enable_days=None):
    """The advise-only line every surface carries.

    Returns {"action": "enable"|"disable"|"keep"|"none", "line": str} or
    None when there is genuinely nothing to say (platform gap, env override,
    external holder, opted out, or a user-set value that is not "1h").
    `payoff` None means no verdict yet -> one neutral line for doctor and
    status (quick prints nothing for a "none" action). Numbers are always
    the user's own, every actionable line names the exact command, and
    subscription billing keeps the "API-equivalent estimate" label.
    """
    _measure_facade = _compat.facade()
    try:
        if state in ("platform-gap", "opted-out", "env-override",
                     "external-setting", "unknown-settings"):
            return None
        if current not in (None, "1h"):
            return None  # a user-set "5m" (or anything else) is their choice
        cmd = _measure_facade._subagent_cache_cmd()
        est = "API-equivalent estimate" if billing == "subscription" else "estimate"
        days = int((payoff or {}).get("window_days") or 30)
        if post_enable_days is not None:
            window = f"the {post_enable_days} days since it was set"
        else:
            window = f"the last {days} days"
        if payoff is None:
            return {"action": "none",
                    "line": ("no payoff verdict yet; the background scan "
                             "writes it at the next session start")}
        decision = _measure_facade._subagent_cache_judge_payoff(payoff)
        net = float(payoff.get("net_usd_est") or 0.0)
        if current == "1h":
            if decision["decision"] == "not-enough-data":
                return {"action": "none", "line": decision["reason"]}
            if net < 0:
                if state != "set":
                    # No marker says Token Optimizer set this key, so
                    # `subagent-cache disable` would answer nothing-to-undo.
                    return {"action": "disable", "manual": True,
                            "line": (f"is costing about ${abs(net):.2f} net "
                                     f"({est}) over {window}; remove "
                                     f"{_measure_facade._SUBAGENT_CACHE_KEY} from "
                                     f"settings.json by hand")}
                return {"action": "disable",
                        "line": (f"is costing about ${abs(net):.2f} net ({est}) "
                                 f"over {window}; turn off: `{cmd} disable`")}
            if decision["decision"] == "recommend":
                return {"action": "keep",
                        "line": (f"is paying: +${net:.2f} net ({est}) over "
                                 f"{window}; nothing to do")}
            return {"action": "none",
                    "line": (f"about break-even over {window}: saved "
                             f"${float(payoff.get('savings_usd_est') or 0.0):.2f} "
                             f"vs premium ${float(payoff.get('extra_write_cost_usd_est') or 0.0):.2f} "
                             f"({est}); nothing to do")}
        if decision["decision"] == "recommend":
            return {"action": "enable",
                    "line": (f"would have saved about ${net:.2f} net ({est}) "
                             f"over {window}; turn on: `{cmd} enable`")}
        return {"action": "none", "line": decision["reason"]}
    except Exception:
        return None


def _subagent_cache_status_decision(state, marker, payoff, force_on, source):
    """`auto_decision` for status / doctor / quick / coach: what the verdict
    says (or what the opted-in automation decided) and why."""
    _measure_facade = _compat.facade()
    stored = (marker or {}).get("auto_decision")
    if state == "opted-out":
        return {"decision": "opted-out",
                "reason": f"{_measure_facade._SUBAGENT_CACHE_OPTOUT_ENV} is set to off"}
    if state in ("set", "user-set", "user-declined", "auto-reverted",
                 "removed"):
        if state == "set" and isinstance(stored, dict):
            return dict(stored)
        return {"decision": state,
                "reason": {"set": "set by Token Optimizer",
                           "user-set": "you set the key yourself; left alone",
                           "user-declined": "you removed or changed the key after we set it",
                           "auto-reverted": "removed again after a negative 14-day check",
                           "removed": "you removed it with subagent-cache disable; final"}[state]}
    if force_on:
        return {"decision": "forced-on",
                "reason": f"{_measure_facade._SUBAGENT_CACHE_OPTOUT_ENV}=1 (always on)"}
    if payoff is not None:
        return dict(_measure_facade._subagent_cache_judge_payoff(payoff), source=source)
    if isinstance(stored, dict):
        return dict(stored)
    return {"decision": "pending",
            "reason": ("no payoff scan yet; one runs in the background at the "
                       "next session start")}


def subagent_cache_status(days=None, now=None, use_cache=None):
    """State + who set it + the payoff estimate + the verdict + the advice.

    Read-only, never writes. `use_cache=False` (the explicit `status` command)
    scans the transcripts now; `use_cache=True` (doctor, quick, coach) only
    reads the verdict the background scan left, and never scans or spawns."""
    _measure_facade = _compat.facade()
    if not _measure_facade._subagent_cache_claude_only():
        return {"state": "platform-gap", "set_by": None, "payoff": None,
                "recommendation": None, "hint": None,
                "reason": "not Claude Code (Cowork and other runtimes are a "
                          "documented no-op)"}
    if now is None:
        now = _measure_facade.time.time()
    marker = _measure_facade._subagent_cache_read_marker()
    try:
        data, _path, _ok = _measure_facade._read_settings_json_checked()
        current = data.get(_measure_facade._SUBAGENT_CACHE_KEY)
    except Exception:
        current = None
    mstate = (marker or {}).get("state")
    if _measure_facade._subagent_cache_optout():
        state, who = "opted-out", None
        reason = f"{_measure_facade._SUBAGENT_CACHE_OPTOUT_ENV} is set"
    elif mstate == "set" and current == "1h":
        state, who, reason = "set", "token-optimizer", None
    elif mstate == "set":
        state, who = "user-declined", None
        reason = "the user removed or changed the key after we set it"
    elif (mstate in ("user-declined", "auto-reverted", "opted-out", "removed")
          and current is None):
        # Sticky "off" states only describe a key that is actually absent; a
        # key that came back is the user's own and reads as user-set.
        state, who = mstate, None
        reason = mstate.replace("-", " ")
    elif mstate == "user-set" or current is not None:
        state, who, reason = "user-set", "user", None
    else:
        state, who, reason = "off", None, None
    # a "1h" key with no readable marker may be an orphaned TO write
    # (or the user's own) -- say so instead of silently reporting "user-set".
    hint = None
    if current == "1h" and marker is None:
        hint = (
            ("the Token Optimizer marker is corrupt or unreadable, so "
             "`subagent-cache disable` cannot prove this tool set the key; "
             "remove the key from settings.json by hand if Token Optimizer "
             "set it")
            if _measure_facade._subagent_cache_marker_path().exists()
            else ("no Token Optimizer marker records who set this key; if an "
                  "older version set it, remove the key from settings.json "
                  "by hand"))
    extra = {}
    if use_cache:
        want_since = (float(marker["set_ts"]) if state == "set"
                      and (marker or {}).get("set_ts") is not None else None)
        vstate, rec = _measure_facade._subagent_cache_verdict_for(now, want_since)
        if rec is not None and rec.get("complete") and isinstance(rec.get("payoff"), dict):
            payoff, source = rec["payoff"], "cached"
            extra["payoff_age_seconds"] = max(0, int(float(now) - float(rec["ts"])))
        else:
            payoff, source = _measure_facade._subagent_cache_payoff_zero(days), "none"
        judge_with = payoff if source == "cached" else None
    else:
        payoff, source = _measure_facade.subagent_cache_payoff(days=days, now=now), "live"
        judge_with = payoff
    try:
        billing = _measure_facade.keepwarm_billing_mode()
    except Exception:
        billing = "subscription"
    post_days = None
    if state == "set" and source == "cached" and (marker or {}).get("set_ts"):
        try:
            post_days = max(1, int((float(now) - float(marker["set_ts"])) / 86400))
        except (TypeError, ValueError):
            post_days = None
    out = {
        "state": state,
        "set_by": who,
        "billing_mode": billing,
        "reason": reason,
        "set_ts": (marker or {}).get("set_ts") if who == "token-optimizer" else None,
        "payoff": payoff,
        "payoff_source": source,
        "auto_decision": _measure_facade._subagent_cache_status_decision(
            state, marker, judge_with, _measure_facade._subagent_cache_force_on(), source),
        "recommendation": _measure_facade._subagent_cache_recommendation(
            state, current,
            payoff if source in ("live", "cached") else None,
            billing, post_enable_days=post_days),
        "hint": hint,
        "estimate": True,
    }
    out.update(extra)
    return out


def subagent_cache_block(days=None, now=None):
    """The doctor/quick/coach surface: state, who set it, net estimate, the
    verdict, and the advice line. Reads the cached verdict only; never scans."""
    _measure_facade = _compat.facade()
    try:
        st = _measure_facade.subagent_cache_status(days=days, now=now, use_cache=True)
    except Exception as exc:
        return {"state": "unknown", "set_by": None, "payoff": None,
                "recommendation": None, "hint": None,
                "reason": f"status unavailable: {type(exc).__name__}"}
    st.setdefault("estimate", True)
    st.setdefault("recommendation", None)
    st.setdefault("hint", None)
    return st


def _recs_record_path():
    _measure_facade = _compat.facade()
    return _measure_facade.SNAPSHOT_DIR / _measure_facade._RECS_RECORD_NAME


def _recs_history_path():
    _measure_facade = _compat.facade()
    return _measure_facade.SNAPSHOT_DIR / _measure_facade._RECS_HISTORY_NAME


def _recs_lock_path():
    _measure_facade = _compat.facade()
    return _measure_facade.SNAPSHOT_DIR / _measure_facade._RECS_LOCK_NAME


def _recs_read_record():
    """The measured record, or None when absent/corrupt/wrong schema."""
    _measure_facade = _compat.facade()
    try:
        data = _measure_facade.json.loads(_measure_facade._recs_record_path().read_text(encoding="utf-8"))
    except (_measure_facade.json.JSONDecodeError, OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != 1 \
            or not isinstance(data.get("items"), list):
        return None
    return data


def _recs_lock_acquire(now=None):
    """Take the refresh lock (O_EXCL); its owner token, or None when held.

    Same pattern as the subagent-cache scan lock: a lock older than
    _RECS_LOCK_STALE is a crashed child and is reclaimed."""
    _measure_facade = _compat.facade()
    if now is None:
        now = _measure_facade.time.time()
    lock = _measure_facade._recs_lock_path()
    token = _measure_facade.os.urandom(16).hex()
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in (1, 2):
            try:
                fd = _measure_facade.os.open(str(lock), _measure_facade.os.O_CREAT | _measure_facade.os.O_EXCL | _measure_facade.os.O_WRONLY,
                             0o600)
            except FileExistsError:
                if not _measure_facade._reclaim_stale_lock_file(lock, _measure_facade._RECS_LOCK_STALE, now):
                    return None
                continue
            try:
                _measure_facade.os.write(fd, token.encode("ascii"))
            finally:
                _measure_facade.os.close(fd)
            return token
    except OSError:
        pass
    return None


def _recs_lock_release(token):
    """Delete the lock only while it still holds `token`. Never raises."""
    _measure_facade = _compat.facade()
    if not token:
        return
    lock = _measure_facade._recs_lock_path()
    try:
        if lock.read_text(encoding="ascii").strip() == token:
            lock.unlink()
    except OSError:
        pass


def _recs_compact_item(now=None):
    """The compact_window item, built on the bounded compact_advice replay."""
    _measure_facade = _compat.facade()
    base = {"id": "compact_window", "direction": "smaller", "command": "",
            "tradeoff": "Compaction can drop early instructions."}
    if not _measure_facade._subagent_cache_claude_only():
        return dict(base, state="not_applicable", enough_data=False,
                    headline="", numbers={},
                    reason="the compact window is a Claude Code setting")
    try:
        rep = _measure_facade.compact_advice(days=_measure_facade._RECS_WINDOW_DAYS, max_sessions=None,
                             deadline_seconds=_measure_facade._RECS_COMPACT_BUDGET_SECONDS)
    except Exception as exc:
        return dict(base, state="not_enough_data", enough_data=False,
                    headline="compaction history could not be measured yet",
                    numbers={}, partial=True,
                    reason=f"compact_advice failed: {type(exc).__name__}")
    days = max(1, int(rep.get("days") or _measure_facade._RECS_WINDOW_DAYS))
    sessions = int(rep.get("sessions_replayed") or 0)
    total_cr = int(rep.get("total_cache_read_tokens") or 0)
    current = (rep.get("default_window") or {}).get("tokens")
    # The win measure: cache-read tokens per replayed session.
    measure = round(total_cr / sessions, 1) if sessions else None
    numbers = {"setting": current, "recommended": current,
               "measure": measure, "sessions": sessions,
               "cache_read_tokens": total_cr, "window_days": days}
    item = dict(base, numbers=numbers, partial=bool(rep.get("truncated")))
    if rep.get("too_thin") or not rep.get("windows"):
        return dict(item, state="not_enough_data", enough_data=False,
                    headline="not enough session history to judge yet",
                    reason=(f"{sessions} sessions replayed, need at least "
                            f"{_measure_facade._ADVICE_MIN_SESSIONS} and one compaction"))
    if rep.get("truncated"):
        # Never judge on part of the window: a cut-short replay is retried.
        return dict(item, state="not_enough_data", enough_data=False,
                    headline="the last measurement did not finish",
                    reason="the replay ran out of time before the whole "
                           "window was read; it is retried tomorrow")
    eligible = []
    for e in rep["windows"]:
        if e.get("reference"):
            continue  # the user's own window: 0 extra, 0 net by construction
        net = float(e.get("net_usd_unrounded") or 0.0)
        if net <= 0:
            continue
        per_day = float(e.get("extra_compactions") or 0) / days
        if per_day > _measure_facade._RECS_MAX_EXTRA_COMPACTIONS_PER_DAY:
            continue
        if float(e.get("avoided_share_pct") or 0.0) \
                < _measure_facade._RECS_MIN_CACHE_READ_SHARE_PCT:
            continue
        eligible.append((e, per_day))
    if not eligible:
        return dict(item, state="keep", enough_data=True,
                    headline="your current setting fits your usage",
                    reason=("no candidate cleared the bar: positive priced "
                            "net, <=5 extra compactions a day, >=10% of "
                            "cache reads avoided"))
    # Best priced net; ties break to fewer extra compactions a day, then the
    # larger window (least truncation risk).
    eligible.sort(key=lambda t: ((_measure_facade := _compat.facade()), (-t[0]["net_usd_unrounded"], t[1],
                                 -t[0]["window"]))[1])
    best, per_day = eligible[0]
    numbers["recommended"] = best["window"]
    numbers["avoided_share_pct"] = best["avoided_share_pct"]
    numbers["net_usd"] = best["net_usd"]
    numbers["extra_compactions_per_day"] = round(per_day, 2)
    cur_k = f"{int(current) // 1000}K" if current else "your current window"
    return dict(item, state="recommend", enough_data=True,
                command=f"/autocompact {best['window']}",
                headline=(f"compacting at {best['window'] // 1000}K instead "
                          f"of {cur_k} would have avoided about "
                          f"{float(best['avoided_share_pct']):.0f}% of "
                          f"cache-read tokens "
                          f"({_measure_facade._advice_usd(float(best['net_usd']))} net, "
                          f"~{per_day:.1f} extra compactions a day; "
                          f"estimate, last {days} days)"),
                reason="best priced net among candidates inside the caps")


def _recs_subagent_item(now=None):
    """The subagent_cache item, built on the cached payoff verdict."""
    _measure_facade = _compat.facade()
    base = {"id": "subagent_cache", "direction": "", "command": "",
            "tradeoff": ""}
    if not _measure_facade._subagent_cache_claude_only():
        return dict(base, state="not_applicable", enough_data=False,
                    headline="", numbers={},
                    reason="subagentPromptCacheTtl is a Claude Code setting")
    if now is None:
        now = _measure_facade.time.time()
    marker = _measure_facade._subagent_cache_read_marker()
    try:
        data, _path, _ok = _measure_facade._read_settings_json_checked()
        current = data.get(_measure_facade._SUBAGENT_CACHE_KEY)
    except Exception:
        current = None
    mstate = (marker or {}).get("state")
    since = None
    if mstate == "set" and current == "1h":
        try:
            since = float(marker["set_ts"])
        except (TypeError, ValueError):
            since = None
    # The refresh is the one place that may run the bounded payoff scan; a
    # fresh verdict for the right window is reused instead.
    partial = False
    vstate, vrec = _measure_facade._subagent_cache_verdict_for(now, since)
    if vstate != "fresh":
        # Complete only when the inline scan itself finished cleanly; a scan
        # another process holds (None) or one cut short leaves it partial.
        partial = True
        try:
            scanned = _measure_facade.subagent_cache_scan_run(now=now, since_ts=since)
            partial = not (scanned and scanned.get("complete"))
            vrec = scanned or vrec
        except Exception:
            pass
    elif not vrec.get("complete"):
        partial = True
    try:
        st = _measure_facade.subagent_cache_status(now=now, use_cache=True)
    except Exception as exc:
        return dict(base, state="not_enough_data", enough_data=False,
                    headline="the cache payoff could not be read",
                    numbers={}, partial=True,
                    reason=f"status unavailable: {type(exc).__name__}")
    if st.get("state") == "platform-gap":
        return dict(base, state="not_applicable", enough_data=False,
                    headline="", numbers={},
                    reason=st.get("reason") or "not Claude Code")
    payoff = (st.get("payoff")
              if st.get("payoff_source") in ("live", "cached") else None)
    numbers = {"setting": current, "recommended": None, "measure": None}
    if isinstance(payoff, dict):
        numbers.update(measure=payoff.get("net_usd_est"),
                       subagent_requests=payoff.get("subagent_requests"),
                       savings_usd_est=payoff.get("savings_usd_est"),
                       premium_usd_est=payoff.get("extra_write_cost_usd_est"))
    rec = st.get("recommendation") or {}
    action, line = rec.get("action"), rec.get("line") or ""
    mp = _measure_facade.shlex.quote(str(_measure_facade.MEASURE_PY_PATH))
    item = dict(base, numbers=numbers, partial=partial)
    if payoff is None:
        return dict(item, state="not_enough_data", enough_data=False,
                    headline="not enough data yet",
                    reason=(line or "the payoff scan has not finished yet"))
    decision = _measure_facade._subagent_cache_judge_payoff(payoff)
    if decision["decision"] == "not-enough-data":
        return dict(item, state="not_enough_data", enough_data=False,
                    headline="not enough subagent history to judge yet",
                    reason=decision["reason"])
    if action == "enable":
        numbers["recommended"] = "1h"
        return dict(item, state="recommend", enough_data=True,
                    direction="on",
                    command=f"python3 {mp} subagent-cache enable",
                    headline=line.split("; turn on:")[0].rstrip(),
                    reason=decision["reason"])
    if action == "disable" and rec.get("manual"):
        # The user set the key by hand: no command can undo it, so the item
        # carries the manual step in `tradeoff` and an empty `command`.
        numbers["recommended"] = "off"
        return dict(item, state="recommend", enough_data=True,
                    direction="off", command="",
                    headline=line.split("; remove ")[0].rstrip(),
                    tradeoff=(f"Token Optimizer did not set this key, so it "
                              f"cannot remove it: delete {_measure_facade._SUBAGENT_CACHE_KEY} "
                              f"from settings.json by hand."),
                    reason=decision["reason"])
    if action == "disable":
        numbers["recommended"] = "off"
        return dict(item, state="recommend", enough_data=True,
                    direction="off",
                    command=f"python3 {mp} subagent-cache disable",
                    headline=line.split("; turn off:")[0].rstrip(),
                    reason=decision["reason"])
    numbers["recommended"] = "1h" if current == "1h" else "off"
    if st.get("recommendation") is None:
        # opted-out / env-override / external holder / a key the user set by
        # hand: there is nothing to advise over someone else's setting.
        state_name = st.get("state") or ""
        if state_name in ("opted-out", "env-override", "external-setting"):
            return dict(item, state="not_applicable", enough_data=True,
                        headline="", reason=(st.get("reason") or state_name))
        return dict(item, state="keep", enough_data=True,
                    headline="you set the key yourself; left alone",
                    reason=(st.get("reason") or "user-set"))
    headline = (line.split("; nothing to do")[0].rstrip()
                or "your current setting fits your usage")
    if headline.startswith("would not pay: "):
        # Reads as a sentence after the "Subagent cache:" label.
        headline = ("a 1 hour cache would not pay on your usage ("
                    + headline[len("would not pay: "):] + ")")
    return dict(item, state="keep", enough_data=True, headline=headline,
                reason=decision["reason"])


def _recs_history_append(entry):
    """One compact JSON line per refresh; capped, atomically rewritten."""
    _measure_facade = _compat.facade()
    p = _measure_facade._recs_history_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        line = _measure_facade.json.dumps(entry, separators=(",", ":"))
        try:
            existing = [l for l in p.read_text(encoding="utf-8").splitlines()
                        if l.strip()]
        except OSError:
            existing = []
        existing.append(line)
        if len(existing) > _measure_facade._RECS_HISTORY_CAP:
            retained = existing[-_measure_facade._RECS_HISTORY_CAP:]
            fd, tmp_name = _measure_facade.tempfile.mkstemp(prefix=".usage_recs_hist.",
                                            suffix=".tmp", dir=str(p.parent))
            try:
                if hasattr(_measure_facade.os, "fchmod"):
                    _measure_facade.os.fchmod(fd, 0o600)
                with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(retained) + "\n")
                _measure_facade.os.replace(tmp_name, str(p))
            except Exception:
                try:
                    _measure_facade.os.unlink(tmp_name)
                except OSError:
                    pass
        else:
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def _recs_read_history():
    _measure_facade = _compat.facade()
    p = _measure_facade._recs_history_path()
    try:
        return [_measure_facade.json.loads(l) for l in
                p.read_text(encoding="utf-8").splitlines() if l.strip()]
    except (_measure_facade.json.JSONDecodeError, OSError, ValueError):
        return []


def _recs_setting_matches(iid, setting, recommended):
    """Whether an observed setting value counts as having adopted the
    recommendation. For the subagent cache, "off" means the key is absent or
    any non-"1h" value."""
    _measure_facade = _compat.facade()
    if recommended is None:
        return False
    if iid == "subagent_cache":
        if recommended == "1h":
            return setting == "1h"
        if recommended == "off":
            return setting != "1h"
        return False
    return setting == recommended


def _recs_measure_improved(iid, recommended, before, after):
    """Whether the after-measure moved the way the recommendation wanted."""
    _measure_facade = _compat.facade()
    if iid == "compact_window":
        return after < before
    if iid == "subagent_cache":
        # enable -> a positive measured net; disable -> the counterfactual
        # "what 1h would net now" staying non-positive confirms the choice.
        if recommended == "1h":
            return after > 0
        if recommended == "off":
            return after <= 0
    return False


def _recs_usd(value, plus=None):
    """Dollars with the sign before the symbol: -$2.00, never $-2.00.
    `plus` adds an explicit + to a positive amount; a value that rounds to
    zero carries no sign."""
    _measure_facade = _compat.facade()
    v = round(float(value), 2)
    if v == 0:
        v = 0.0
    sign = "-" if v < 0 else ("+" if plus and v > 0 else "")
    return f"{sign}${abs(v):.2f}"


def _recs_win_line(iid, recommended, before, after, improved, since_ts):
    _measure_facade = _compat.facade()
    try:
        date = _measure_facade.datetime.fromtimestamp(since_ts).date().isoformat()
    except (OSError, OverflowError, ValueError):
        date = "the change"
    tail = "estimate from your own sessions"
    if iid == "compact_window":
        try:
            rk = f"{int(recommended) // 1000}K"
        except (TypeError, ValueError):
            rk = str(recommended)
        moved = (f"cache-read tokens per session went from {before:,.0f} "
                 f"to {after:,.0f}")
        if improved:
            return (f"Compact window: since you set it to {rk} on {date}, "
                    f"{moved} ({tail}).")
        return (f"Compact window: since you set it to {rk} on {date}, "
                f"{moved} -- no saving to claim ({tail}).")
    if iid == "subagent_cache":
        if recommended == "1h":
            if improved:
                return (f"Subagent cache: since you set it to 1 hour on "
                        f"{date}, the measured net is {_measure_facade._recs_usd(after, True)} "
                        f"over the last 30 days (projected "
                        f"{_measure_facade._recs_usd(before, True)}; {tail}).")
            return (f"Subagent cache: since you set it to 1 hour on {date}, "
                    f"the measured net is {_measure_facade._recs_usd(after, True)} over the "
                    f"last 30 days -- the projected {_measure_facade._recs_usd(before, True)} "
                    f"did not hold ({tail}).")
        if improved:
            return (f"Subagent cache: since you turned it off on {date}, the "
                    f"1-hour cache still would not pay (net {_measure_facade._recs_usd(after)}; "
                    f"{tail}).")
        return (f"Subagent cache: since you turned it off on {date}, the "
                f"estimate moved to {_measure_facade._recs_usd(after, True)} -- the 1-hour cache may be "
                f"worth another look ({tail}).")
    return ""


def _recs_wins(items, now):
    """Measured-after-change rows. Every gate must hold before one appears."""
    _measure_facade = _compat.facade()
    lines = _measure_facade._recs_read_history()
    wins = []
    for it in items:
        iid = it.get("id")
        nums = it.get("numbers") or {}
        pending = None        # earliest outstanding recommend line
        adopted_ts = None
        for ln in lines:
            ent = (ln.get("items") or {}).get(iid)
            if not isinstance(ent, dict):
                continue
            try:
                ts = float(ln.get("ts"))
            except (TypeError, ValueError):
                continue
            if pending is None:
                if (ent.get("state") == "recommend"
                        and ent.get("recommended") is not None
                        and not _measure_facade._recs_setting_matches(
                            iid, ent.get("setting"), ent.get("recommended"))):
                    pending = ent
                continue
            if adopted_ts is None and _measure_facade._recs_setting_matches(
                    iid, ent.get("setting"), pending.get("recommended")):
                adopted_ts = ts
        if pending is None or adopted_ts is None:
            continue
        if now - adopted_ts < _measure_facade._RECS_WIN_MIN_DAYS * 86400:
            continue
        # The setting must still be the recommended value (a revert voids it).
        if not _measure_facade._recs_setting_matches(iid, nums.get("setting"),
                                     pending.get("recommended")):
            continue
        if not it.get("enough_data"):
            continue
        before, after = pending.get("measure"), nums.get("measure")
        try:
            before, after = float(before), float(after)
        except (TypeError, ValueError):
            continue
        improved = _measure_facade._recs_measure_improved(iid, pending.get("recommended"),
                                          before, after)
        wins.append({"id": iid, "since_ts": adopted_ts, "before": before,
                     "after": after, "improved": improved,
                     "line": _measure_facade._recs_win_line(iid, pending.get("recommended"),
                                            before, after, improved,
                                            adopted_ts)})
    return wins


def _recs_refresh_locked(now):
    """Build both items, append history, compute wins, write the record."""
    _measure_facade = _compat.facade()
    items, partial = [], False
    for builder in (_measure_facade._recs_compact_item, _measure_facade._recs_subagent_item):
        try:
            it = builder(now=now)
        except Exception as exc:
            it = {"id": "unknown", "state": "not_enough_data",
                  "headline": "", "numbers": {}, "command": "",
                  "direction": "", "enough_data": False,
                  "reason": f"builder failed: {type(exc).__name__}"}
        partial = partial or bool(it.pop("partial", False))
        items.append(it)
    hist_items = {}
    for it in items:
        nums = it.get("numbers") or {}
        hist_items[it.get("id")] = {
            "state": it.get("state"),
            "setting": nums.get("setting"),
            "recommended": nums.get("recommended"),
            "measure": nums.get("measure"),
        }
    _measure_facade._recs_history_append({"ts": float(now), "items": hist_items})
    rec = {"schema": 1, "measured_ts": float(now),
           "window_days": _measure_facade._RECS_WINDOW_DAYS, "complete": not partial,
           "items": items, "wins": _measure_facade._recs_wins(items, now)}
    _measure_facade._subagent_cache_write_json_atomic(
        _measure_facade._recs_record_path(), rec, ".usage_recs.")
    return rec


def _recs_write_provisional(now):
    """A killed refresh must still leave a record (the module header says a
    crashed run writes complete:false). Written when the lock is taken, and
    only when no valid record exists: a previous good record stays put (it is
    already past its refresh age, so the next start retries). Never raises."""
    _measure_facade = _compat.facade()
    try:
        if _measure_facade._recs_read_record() is not None:
            return
        _measure_facade._subagent_cache_write_json_atomic(
            _measure_facade._recs_record_path(),
            {"schema": 1, "measured_ts": float(now),
             "window_days": _measure_facade._RECS_WINDOW_DAYS, "complete": False,
             "provisional": True, "items": [], "wins": []},
            ".usage_recs.")
    except Exception:
        pass


def usage_recommendations_refresh(now=None, token=None):
    """The one refresher. Run by the detached child (which presents its lock
    token) or by hand (which takes the lock itself). Returns the record, or
    None when another refresh holds the lock. Never raises."""
    _measure_facade = _compat.facade()
    if now is None:
        now = _measure_facade.time.time()
    if token is not None:
        try:
            if _measure_facade._recs_lock_path().read_text(encoding="utf-8").strip() != token:
                return None
        except OSError:
            return None
    else:
        token = _measure_facade._recs_lock_acquire(now)
        if token is None:
            return None
    try:
        _measure_facade._recs_write_provisional(now)
        return _measure_facade._recs_refresh_locked(now)
    finally:
        _measure_facade._recs_lock_release(token)


def _recs_spawn_refresh(now=None):
    """Start the refresh as a detached background process. True when started.

    The lock is taken HERE (so two spawners can never both fire) and handed
    to the child by environment token. spawn_detached owns the Windows flags
    (no console window); the interpreter is the GUI twin on Windows. Never
    raises; a failed spawn frees the lock."""
    _measure_facade = _compat.facade()
    token = _measure_facade._recs_lock_acquire(now)
    if token is None:
        return False
    try:
        env = _measure_facade.os.environ.copy()
        env["TOKEN_OPTIMIZER_RUNTIME"] = _measure_facade.detect_runtime()
        env.pop("TOKEN_OPTIMIZER_HOOK", None)
        env[_measure_facade._RECS_TOKEN_ENV] = token
        proc = _measure_facade.spawn_detached(
            [_measure_facade._detached_python_exe(), str(_measure_facade.MEASURE_PY_PATH),
             "recommendations", "refresh"],
            stdin=_measure_facade.subprocess.DEVNULL,
            stdout=_measure_facade.subprocess.DEVNULL,
            stderr=_measure_facade.subprocess.DEVNULL,
            env=env,
            close_fds=True,
        )
    except Exception:
        proc = None
    if proc is None:
        _measure_facade._recs_lock_release(token)
        try:
            _measure_facade._log_spawn_failure("usage recommendations refresh spawn failed")
        except Exception:
            pass
        return False
    return True


def _recs_ensure_fresh(now=None):
    """Spawn the detached refresh when the record is missing or >24h old.

    Called from session start and the dashboard data collection; reads one
    small file, never scans. Never raises."""
    _measure_facade = _compat.facade()
    try:
        if not _measure_facade._subagent_cache_claude_only():
            return False
        # Under pytest the detached child would run against the real
        # ~/.claude; tests exercise the spawn by stubbing spawn_detached and
        # clearing this variable.
        if _measure_facade.os.environ.get("PYTEST_CURRENT_TEST"):
            return False
        rec = _measure_facade._recs_read_record()
        if rec is not None:
            # A provisional record means the refresh that wrote it never
            # finished; retry once the lock it held would be reclaimable
            # instead of parking the retry for a day.
            limit = (_measure_facade._RECS_LOCK_STALE if rec.get("provisional")
                     else _measure_facade._RECS_REFRESH_SECONDS)
            try:
                if (float(now if now is not None else _measure_facade.time.time())
                        - float(rec.get("measured_ts") or 0)
                        < limit):
                    return False
            except (TypeError, ValueError):
                pass
        return _measure_facade._recs_spawn_refresh(now=now)
    except Exception:
        return False


def _recs_block(now=None):
    """What surfaces read: the record only, never a scan.

    None on a foreign runtime with no record (nothing to show); otherwise the
    record's items + wins, or present:false while the first measurement is
    still running."""
    _measure_facade = _compat.facade()
    rec = _measure_facade._recs_read_record()
    if rec is not None:
        return {"present": True, "schema": 1,
                "measured_ts": rec.get("measured_ts"),
                "window_days": rec.get("window_days") or _measure_facade._RECS_WINDOW_DAYS,
                "complete": bool(rec.get("complete")),
                "items": rec.get("items") or [],
                "wins": rec.get("wins") or []}
    if not _measure_facade._subagent_cache_claude_only():
        return None
    return {"present": False, "schema": 1, "measured_ts": None,
            "window_days": _measure_facade._RECS_WINDOW_DAYS, "complete": False,
            "items": [], "wins": []}


def _recs_item_line(it):
    """One display line per applicable item, or None for not_applicable."""
    _measure_facade = _compat.facade()
    if it.get("state") == "not_applicable":
        return None
    label = _measure_facade._RECS_LABELS.get(it.get("id"), str(it.get("id") or "?"))
    head = (it.get("headline") or it.get("state") or "no data").rstrip(".")
    if it.get("state") == "recommend" and it.get("command"):
        return f"{label}: {head}. Run: `{it['command']}`"
    return f"{label}: {head}."


def _recs_covers(item_id):
    """True when the record already speaks for this item (any state): the
    legacy verdict-derived advice line then stays out so no line repeats."""
    _measure_facade = _compat.facade()
    try:
        blk = _measure_facade._recs_block()
        for it in (blk or {}).get("items") or []:
            if it.get("id") == item_id:
                return True
    except Exception:
        pass
    return False


def _recs_cli(argv):
    """`measure.py recommendations [status|refresh] [--json]` handler."""
    _measure_facade = _compat.facade()
    as_json = "--json" in argv
    sub = argv[1] if len(argv) > 1 and not argv[1].startswith("--") else "status"
    if sub not in ("status", "refresh"):
        print("usage: measure.py recommendations [status|refresh] [--json]")
        _measure_facade.sys.exit(2)
    if sub == "refresh":
        rec = _measure_facade.usage_recommendations_refresh(
            token=_measure_facade.os.environ.get(_measure_facade._RECS_TOKEN_ENV) or None)
        if as_json:
            print(_measure_facade.json.dumps(rec or {}, indent=2))
            return
        if rec is None:
            print("[Token Optimizer] usage recommendations: skipped (another "
                  "refresh is running)")
        elif rec.get("complete"):
            print("[Token Optimizer] usage recommendations: measured")
        else:
            print("[Token Optimizer] usage recommendations: incomplete; "
                  "it retries tomorrow")
        return
    blk = _measure_facade._recs_block()
    if as_json:
        print(_measure_facade.json.dumps(blk or {}, indent=2))
        return
    print("\n[Token Optimizer] usage recommendations (measured daily; "
          "estimates only)")
    printed = False
    for it in (blk or {}).get("items") or []:
        ln = _measure_facade._recs_item_line(it)
        if ln:
            print(f"  {ln}")
            printed = True
    for w in (blk or {}).get("wins") or []:
        if w.get("line"):
            print(f"  {w['line']}")
            printed = True
    if not printed:
        print("  no usage recommendations yet -- the daily measurement runs "
              "in the background")
    print()


def _coach_cli(args):
    """`measure.py coach [--json] [--focus F]` handler (extracted verbatim
    from the __main__ dispatch so tests can drive the JSON surface)."""
    _measure_facade = _compat.facade()
    focus = None
    output_json = "--json" in args
    for i, a in enumerate(args):
        if a == "--focus" and i + 1 < len(args):
            focus = args[i + 1]
    data = _measure_facade.generate_coach_data(focus=focus, include_deterministic=True)
    if output_json:
        # Subagent-cache surface: state, who set it, net payoff estimate.
        # Fail-open; never breaks the coach JSON.
        try:
            data["subagent_cache"] = _measure_facade.subagent_cache_block()
        except Exception:
            pass
        print(_measure_facade.json.dumps(data, indent=2))
        return
    is_codex = _measure_facade.detect_runtime() == "codex"
    instruction_label = "AGENTS.md" if is_codex else "CLAUDE.md"
    score = data["health_score"]
    snap = data["snapshot"]
    print(f"\n  Token Health Score: {score}/100")
    print(f"  Startup overhead: {snap['total_overhead']:,} tokens ({snap['overhead_pct']}% of {snap['context_window'] // 1000}K)")
    print(f"  Usable context: ~{snap['usable_tokens']:,} tokens (after overhead + autocompact buffer)")
    print(f"  Skills: {snap['skill_count']} ({snap['skill_tokens']:,} tokens)")
    if snap.get("skills_basis"):
        print(f"          ({snap['skills_basis']})")
    print(f"  {instruction_label}: {snap['claude_md_tokens']:,} tokens")
    print(f"  MCP: {snap['mcp_server_count']} servers ({snap['mcp_tokens']:,} tokens)")
    print()
    if data["patterns_bad"]:
        print("  Issues detected:")
        for p in data["patterns_bad"]:
            sev = {"high": "!!!", "medium": "!!", "low": "!"}.get(p["severity"], "!")
            print(f"    [{sev}] {p['name']}: {p['detail']}")
        print()
    if data["patterns_good"]:
        print("  Good practices:")
        for p in data["patterns_good"]:
            print(f"    [OK] {p['name']}: {p['detail']}")
        print()
    if data.get("subagent_costs"):
        sc = data["subagent_costs"]
        print(f"  Subagent spend: ${sc['total_usd']:.2f} ({sc['pct_of_spend']}% of recent sessions)")
        for s in sc["top_subagents"][:3]:
            print(f"    {_measure_facade._strip_ansi(str(s['name']))}: ${s['cost_usd']} ({s['tokens']:,} tokens, {_measure_facade._strip_ansi(str(s['model']))})")
        print()
    if data.get("costly_prompts"):
        print("  Most expensive prompts (last 7 days):")
        for i, p in enumerate(data["costly_prompts"][:5], 1):
            # Session-log text is attacker-influenceable — strip ANSI
            # escapes before printing so a crafted prompt cannot inject
            # terminal control sequences, then truncate the clean text.
            preview = _measure_facade._strip_ansi(str(p["text"]))[:70].replace("\n", " ")
            print(f"    {i}. ${p['cost_usd']} ({p['tokens_in']:,} in) \"{preview}...\"")
        print()
    det = data.get("deterministic_candidates")
    if det:
        print(f"  {_measure_facade._strip_ansi(str(det.get('summary') or _measure_facade.deterministic_candidates.summary_line(det)))}")
        if det.get("candidates"):
            print(_measure_facade._hint("    Details: python3 measure.py deterministic-candidates"))
        print()
    # Usage recommendations: the daily-measured record carries the content;
    # one line per recommend item, never a scan here.
    try:
        _rblk = _measure_facade._recs_block() or {}
        _ritems = _rblk.get("items") or []
        _rcovered = {i.get("id") for i in _ritems}
        _printed = False
        for _it in _ritems:
            if _it.get("state") == "recommend":
                _ln = _measure_facade._recs_item_line(_it)
                if _ln:
                    print(f"  {_ln}")
                    _printed = True
        if _printed:
            print()
    except Exception:
        _rcovered = set()
    # Advise-only subagent cache verdict: actionable recommendations only.
    # Falls silent once the daily record covers the item so no line repeats.
    try:
        if "subagent_cache" not in _rcovered:
            _scr = (_measure_facade.subagent_cache_block().get("recommendation") or {})
            if _scr.get("action") in ("enable", "disable"):
                print(f"  Subagent cache: {_scr['line']}")
                print()
    except Exception:
        pass
    if data["questions"]:
        print("  Coaching questions:")
        for q in data["questions"]:
            print(f"    ? {q}")
        print()


def _subagent_cache_cli(argv):
    """`measure.py subagent-cache status|enable|disable|scan [--json]` handler."""
    _measure_facade = _compat.facade()
    as_json = "--json" in argv
    sub = argv[1] if len(argv) > 1 else "status"
    if sub not in ("status", "enable", "disable", "scan"):
        print("usage: measure.py subagent-cache status|enable|disable [--json]")
        _measure_facade.sys.exit(2)
    if sub == "scan":
        # Internal: the detached child the session start spawns. Quiet by
        # design (stdio is DEVNULL there); a hand run prints one line.
        since = None
        if "--since" in argv:
            try:
                since = float(argv[argv.index("--since") + 1])
            except (IndexError, ValueError):
                since = None
        rec = _measure_facade.subagent_cache_scan_run(
            since_ts=since,
            token=_measure_facade.os.environ.get(_measure_facade._SUBAGENT_CACHE_SCAN_TOKEN_ENV) or None)
        if rec is None:
            print("[Token Optimizer] subagent cache scan: skipped (another scan "
                  "is running, or not Claude Code)")
        elif rec["complete"]:
            print("[Token Optimizer] subagent cache scan: verdict saved")
        else:
            print("[Token Optimizer] subagent cache scan: incomplete "
                  f"({rec['reason']}); no verdict")
        return
    if sub == "enable":
        # Unconditional (a deliberate request), but never blind: show the
        # user's own estimate first.
        p = _measure_facade.subagent_cache_payoff(days=30)
        try:
            billing = _measure_facade.keepwarm_billing_mode()
        except Exception:
            billing = "subscription"
        r = _measure_facade.subagent_cache_enable(automatic=False)
        if as_json:
            print(_measure_facade.json.dumps(dict(r, payoff=p), indent=2))
            return
        print(_measure_facade._subagent_cache_payoff_lines(p, billing))
        if r.get("notice"):
            print(f"[Token Optimizer] {r['notice']}")
        else:
            why = f": {r['reason']}" if r.get("reason") else ""
            print(f"[Token Optimizer] subagent cache: {r['state']}"
                  + (" (settings.json changed)" if r.get("changed") else "") + why)
        return
    if sub == "disable":
        r = _measure_facade.subagent_cache_disable()
        if as_json:
            print(_measure_facade.json.dumps(r, indent=2))
        else:
            why = f": {r['reason']}" if r.get("reason") else ""
            state = r.get("state") or "unknown"
            print(f"[Token Optimizer] subagent cache: {state}"
                  + (" (key removed)" if r.get("changed") else "") + why)
        return
    # status
    r = _measure_facade.subagent_cache_status()
    if as_json:
        print(_measure_facade.json.dumps(r, indent=2))
        return
    p = r.get("payoff") or {}
    print(f"[Token Optimizer] subagent cache: {r['state']}"
          + (f" (set by {r['set_by']})" if r.get("set_by") else ""))
    if r.get("reason"):
        print(f"  why: {r['reason']}")
    ad = r.get("auto_decision") or {}
    if ad:
        # The reason is already self-describing ("would not pay: ...",
        # "pays: ..."); prefixing the decision label doubled the sentence.
        print(f"  verdict: {ad.get('reason') or ad.get('decision')}")
    rec = r.get("recommendation") or {}
    if rec.get("line") and rec["line"] != ad.get("reason") \
            and not _measure_facade._recs_covers("subagent_cache"):
        # One advice line, and never the verdict reason repeated verbatim.
        # When the daily usage-recommendations record already carries this
        # item its own line below is the one that prints.
        print(f"  advice: {rec['line']}")
    if r.get("hint"):
        print(f"  note: {r['hint']}")
    print(_measure_facade._subagent_cache_payoff_lines(p, r.get("billing_mode")))
    # The daily usage-recommendations record: one line per applicable item.
    try:
        _rb = _measure_facade._recs_block() or {}
        if _rb.get("items"):
            print("  usage recommendations (daily record):")
            for _it in _rb["items"]:
                _ln = _measure_facade._recs_item_line(_it)
                if _ln:
                    print(f"    {_ln}")
    except Exception:
        pass


def _subagent_cache_payoff_lines(p, billing_mode):
    """Two short status lines: both parts of the estimate, then the assumptions.

    Subscription plans pay no dollars per token, so tokens lead and the dollar
    figure is labelled API-equivalent.
    """
    _measure_facade = _compat.facade()
    within = int(p.get("within_agent_tokens") or 0)
    across = int(p.get("across_spawn_tokens") or 0)
    realized = int(p.get("realized_read_tokens") or 0)
    premium_tok = int(p.get("write_tokens_5m") or 0) + int(p.get("write_tokens_1h") or 0)
    net = float(p.get("net_usd_est") or 0.0)
    saved = float(p.get("savings_usd_est") or 0.0)
    extra = float(p.get("extra_write_cost_usd_est") or 0.0)
    days = p.get("window_days", 30)
    tokens = (f"{within + across + realized:,} tokens a 1h cache reads instead of rewriting "
              f"(within one agent {within:,}, across spawns {across:,}, "
              f"already realized {realized:,}) against the 1h write premium on "
              f"{premium_tok:,} written tokens")
    sign = "-" if net < 0 else ""
    money = f"net {sign}${abs(net):.2f} (saved ${saved:.2f} - premium ${extra:.2f})"
    if billing_mode == "subscription":
        first = (f"  last {days}d: {tokens}; {money} API-equivalent, "
                 "your plan is not billed per token. ESTIMATE from your own transcripts.")
    else:
        first = (f"  last {days}d: {money}; {tokens}. "
                 "ESTIMATE from your own transcripts.")
    second = (
        "  assumes: a 5-60 min gap means the 5m cache missed and a 1h one hits; "
        "shared prefix = smallest first request of a group, grouped by "
        f"{p.get('grouping') or 'project+model (agent type not recorded)'}; "
        f"{int(p.get('subagent_requests') or 0):,} subagent requests "
        f"(the recommendation needs {_measure_facade._SUBAGENT_CACHE_TRIPWIRE_MIN_REQUESTS}).")
    return first + "\n" + second
