"""Definition-only implementations; mutable state belongs to measure."""
from __future__ import annotations
from . import _compat


def _get_model_cost(model, input_tokens, output_tokens, cache_read=None, cache_create=None, tier=None,
                    cache_create_1h=None, cache_create_5m=None, per_request=None):
    """Calculate USD cost for a given model and token counts using the active pricing tier.

    Returns cost in USD. OpenAI/Codex and Gemini models use provider-specific
    rate cards; Claude models use the selected Claude provider tier.

    For Claude models, cache_create tokens are priced by TTL tier when the split is known:
      - cache_create_1h: 1-hour TTL writes (2x input rate, e.g. $10/MTok for Opus)
      - cache_create_5m: 5-minute TTL writes (1.25x input rate, e.g. $6.25/MTok for Opus)
    When the split is unavailable (both None), the total cache_create uses the 5m rate
    (conservative; 5m is the more common tier for most Claude Code workloads).

    per_request=True says the token counts are ONE API request. Only then does a
    model with a prompt-length card (Haiku 5.5 over 100K) pay it: the tier is
    decided per request, so a session total, a per-day sum or a 1M-token rate
    probe must be priced on the base card or it reads ~5x too high.
    """
    _measure_facade = _compat.facade()
    if tier is None:
        tier = _measure_facade._load_pricing_tier()
    tier_data = _measure_facade.PRICING_TIERS.get(tier, _measure_facade.PRICING_TIERS["anthropic"])

    full_input = int(input_tokens or 0) + int(cache_read or 0) + int(cache_create or 0)

    for norm_fn, pricing, lc_pricing, lc_threshold in (
        (_measure_facade._normalize_openai_model_name, _measure_facade.OPENAI_MODEL_PRICING, _measure_facade.OPENAI_LONG_CONTEXT_PRICING, _measure_facade.OPENAI_LONG_CONTEXT_INPUT_THRESHOLD),
        (_measure_facade._normalize_gemini_model_name, _measure_facade.GEMINI_MODEL_PRICING, _measure_facade.GEMINI_LONG_CONTEXT_PRICING, _measure_facade.GEMINI_LONG_CONTEXT_INPUT_THRESHOLD),
    ):
        key = norm_fn(model)
        if key:
            rates = pricing[key]
            if full_input > lc_threshold and key in lc_pricing:
                rates = lc_pricing[key]
            return (
                input_tokens * rates["input"] / 1e6
                + output_tokens * rates["output"] / 1e6
                + cache_read * rates["cache_read"] / 1e6
                + cache_create * rates.get("cache_write", 0) / 1e6
            )

    normalized = _measure_facade._claude_price_key(model, tier_data["claude_models"]) if model else None
    if normalized and normalized in tier_data["claude_models"]:
        rates = tier_data["claude_models"][normalized]
        lc_models = tier_data.get("claude_models_lc") or {}
    else:
        # Non-Claude model: use Anthropic tier rates for Claude, skip for others
        rates = _measure_facade.PRICING_TIERS["anthropic"]["claude_models"].get(normalized or "", None)
        lc_models = _measure_facade.PRICING_TIERS["anthropic"].get("claude_models_lc") or {}
        if rates is None:
            return 0.0

    # Prompt-length surcharge: Anthropic counts ALL of a request's input
    # (input + cache reads + cache writes = full_input) against the
    # long-context threshold. Haiku 5.5 >100K pays 5x on every rate. Applies
    # to a single request only (per_request); aggregates use the base card.
    if (per_request and normalized in lc_models
            and full_input > _measure_facade.ANTHROPIC_LONG_CONTEXT_INPUT_THRESHOLD):
        rates = lc_models[normalized]

    # Price cache-write tokens by TTL tier when the split is available.
    # 1h tier = 2x input (cache_write_1h); 5m tier = 1.25x input (cache_write).
    # Fall back to 5m rate for the full total when no split is stored.
    if cache_create_1h is not None and cache_create_5m is not None:
        split_1h = int(cache_create_1h or 0)
        split_5m = int(cache_create_5m or 0)
        unsplit = max(0, int(cache_create or 0) - split_1h - split_5m)
        cc_write_cost = (
            split_1h * rates.get("cache_write_1h", rates["cache_write"]) / 1e6
            + (split_5m + unsplit) * rates["cache_write"] / 1e6
        )
    else:
        cc_write_cost = int(cache_create or 0) * rates["cache_write"] / 1e6

    cost = (
        input_tokens * rates["input"] / 1e6
        + output_tokens * rates["output"] / 1e6
        + cache_read * rates["cache_read"] / 1e6
        + cc_write_cost
    )
    return cost


def _bundled_prices_path():
    _measure_facade = _compat.facade()
    override = _measure_facade.os.environ.get("TOKEN_OPTIMIZER_PRICES_FILE", "").strip()
    if override:
        return _measure_facade.Path(override).expanduser()
    return _measure_facade.Path(_measure_facade.__file__).resolve().parent.parent / "pricing" / "prices.json"


def _claude_cards(table, mult=None):
    """Tier-adjusted copy of a fallback card table (vertex-regional is +10%)."""
    _measure_facade = _compat.facade()
    return {key: {f: round(v * mult, 6) for f, v in card.items()} for key, card in table.items()}


def _pricing_as_of():
    """Date that pricing is resolved against: the wall clock in production, or a pinned date
    from TOKEN_OPTIMIZER_PRICING_AS_OF (YYYY-MM-DD) so the test suite stays regime-stable across
    the 2026-09-01 Sonnet flip. Falls back to now() on an unparseable override."""
    _measure_facade = _compat.facade()
    override = _measure_facade.os.environ.get("TOKEN_OPTIMIZER_PRICING_AS_OF")
    if override:
        try:
            return _measure_facade.datetime.strptime(override, "%Y-%m-%d")  # naive; treated as UTC downstream
        except ValueError:
            pass
    return _measure_facade.datetime.now(_measure_facade.timezone.utc)


def _apply_sonnet_intro_pricing(as_of=None):
    """Swap the Anthropic-tier sonnet card to the introductory rate while it is in effect.
    Idempotent (always recomputes from the canonical standard card, so repeated calls / a
    date change are safe). Returns True when the introductory rate is active. A naive `as_of`
    (or env date) is interpreted as UTC so the boundary compares apples to apples."""
    _measure_facade = _compat.facade()
    d = as_of or _measure_facade._pricing_as_of()
    if d.tzinfo is None:
        d = d.replace(tzinfo=_measure_facade.timezone.utc)
    # 2026-08-29: the launch "introductory" rate became the STANDARD Sonnet 5 price and
    # the scheduled 2026-09-01 increase was cancelled. Verbatim from
    # platform.claude.com/docs/en/about-claude/pricing: "The $2/$10 per million
    # input/output token pricing for Claude Sonnet 5, announced at launch as
    # introductory pricing through August 31, 2026, is now the standard price. The
    # previously scheduled increase to $3/$15 per million input/output tokens on
    # September 1, 2026 will not occur."
    # The date gate is therefore retired: $2/$10 applies at every `as_of`. The old
    # gate would have silently repriced EVERY Sonnet-5 dollar in every card by +50%
    # on 2026-09-01. `_SONNET_STANDARD_RATES` is retained as the Sonnet 4.6/4.5 card,
    # which is what the `sonnet_legacy` bucket prices against.
    _measure_facade.PRICING_TIERS["anthropic"]["claude_models"]["sonnet"] = dict(_measure_facade._SONNET_INTRO_RATES)
    return True


def _apply_gpt56_sol_promo_pricing(as_of=None):
    """Swap the gpt-5.6-sol card to the promotional rate while it is in effect.

    Idempotent (always recomputes from the canonical standard card, so repeated
    calls / a date change are safe). Returns True when the promotional rate is
    active. A naive `as_of` (or env date) is interpreted as UTC so the boundary
    compares apples to apples.
    """
    _measure_facade = _compat.facade()
    d = as_of or _measure_facade._pricing_as_of()
    if d.tzinfo is None:
        d = d.replace(tzinfo=_measure_facade.timezone.utc)
    if d < _measure_facade._GPT56_SOL_PROMO_UNTIL:
        _measure_facade.OPENAI_MODEL_PRICING["gpt-5.6-sol"] = dict(_measure_facade._GPT56_SOL_PROMO)
        _measure_facade.OPENAI_LONG_CONTEXT_PRICING["gpt-5.6-sol"] = dict(_measure_facade._GPT56_SOL_LC_PROMO)
        return True
    _measure_facade.OPENAI_MODEL_PRICING["gpt-5.6-sol"] = dict(_measure_facade._GPT56_SOL_STANDARD)
    _measure_facade.OPENAI_LONG_CONTEXT_PRICING["gpt-5.6-sol"] = dict(_measure_facade._GPT56_SOL_LC_STANDARD)
    return False


def _clean_price_cards(section):
    """Keep only well-formed cards: safe key, finite non-negative rates, input+output present."""
    _measure_facade = _compat.facade()
    out = {}
    if not isinstance(section, dict):
        return out
    for key, card in section.items():
        if not isinstance(key, str) or not _measure_facade._PRICE_KEY_RE.match(key) or not isinstance(card, dict):
            continue
        clean = {}
        for field in _measure_facade._PRICE_FIELDS:
            value = card.get(field)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                clean = None
                break
            value = float(value)
            if not _measure_facade.math.isfinite(value) or value < 0 or value > 1000:
                clean = None
                break
            clean[field] = value
        if clean and "input" in clean and "output" in clean:
            out[key] = clean
    return out


def _apply_bundled_prices(path=None):
    """Merge pricing/prices.json over the built-in rate tables. Returns True when applied."""
    _measure_facade = _compat.facade()
    path = _measure_facade.Path(path) if path else _measure_facade._bundled_prices_path()
    _measure_facade.BUNDLED_PRICES_STATUS.update(loaded=False, path=str(path), generated_at=None, error=None)
    if _measure_facade.os.environ.get("TOKEN_OPTIMIZER_BUNDLED_PRICES", "1").strip() == "0":
        _measure_facade.BUNDLED_PRICES_STATUS["error"] = "disabled by TOKEN_OPTIMIZER_BUNDLED_PRICES=0"
        return False
    try:
        if path.stat().st_size > _measure_facade._PRICES_FILE_MAX_BYTES:
            raise ValueError("prices file too large")
        doc = _measure_facade.json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or doc.get("schema") != 1:
            raise ValueError("unsupported prices schema")
    except (OSError, ValueError) as exc:
        _measure_facade.BUNDLED_PRICES_STATUS["error"] = str(exc)
        return False

    claude = _measure_facade._clean_price_cards(doc.get("anthropic"))
    for card in claude.values():
        card.setdefault("cache_read", round(card["input"] * 0.1, 6))
        card.setdefault("cache_write", round(card["input"] * 1.25, 6))
        card.setdefault("cache_write_1h", round(card["input"] * 2, 6))
    claude_lc = _measure_facade._clean_price_cards(doc.get("anthropic_long_context"))
    for card in claude_lc.values():
        card.setdefault("cache_read", round(card["input"] * 0.1, 6))
        card.setdefault("cache_write", round(card["input"] * 1.25, 6))
        card.setdefault("cache_write_1h", round(card["input"] * 2, 6))
    # First-party rates apply on Vertex global and Bedrock; Vertex regional is +10%.
    for tier_name, tier in _measure_facade.PRICING_TIERS.items():
        mult = 1.1 if tier_name == "vertex-regional" else 1.0
        for key, card in claude.items():
            tier["claude_models"][key] = {f: round(v * mult, 6) for f, v in card.items()}
        lc_table = tier.setdefault("claude_models_lc", {})
        for key, card in claude_lc.items():
            lc_table[key] = {f: round(v * mult, 6) for f, v in card.items()}
    for table, section in ((_measure_facade.OPENAI_MODEL_PRICING, "openai"),
                           (_measure_facade.OPENAI_LONG_CONTEXT_PRICING, "openai_long_context"),
                           (_measure_facade.GEMINI_MODEL_PRICING, "gemini"),
                           (_measure_facade.GEMINI_LONG_CONTEXT_PRICING, "gemini_long_context")):
        for key, card in _measure_facade._clean_price_cards(doc.get(section)).items():
            # The dated promo switch owns this card, so the swap back to the
            # standard rate still happens on its date whatever the feed says.
            if key in _measure_facade._PROMO_MANAGED_OPENAI_KEYS:
                continue
            card.setdefault("cache_read", card["input"])
            card.pop("cache_write_1h", None)
            table[key] = card
    _measure_facade.BUNDLED_PRICES_STATUS.update(loaded=True, generated_at=doc.get("generated_at"))
    return True


def _resolve_cache_profile(model, runtime=None):
    """Resolve a model id (+ runtime) to its provider cache profile.

    Returns the (profile_key, profile_dict). Pure lookup, never raises.
    Resolution: OpenAI/Gemini/code review model families first (provider-specific
    rate cards already recognize these), then Claude (Claude Code vs raw API by
    runtime), else the honest "unknown/none" profile.
    """
    _measure_facade = _compat.facade()
    if _measure_facade._normalize_openai_model_name(model):
        return "openai", _measure_facade.PROVIDER_CACHE_PROFILES["openai"]
    if _measure_facade._normalize_gemini_model_name(model):
        return "gemini", _measure_facade.PROVIDER_CACHE_PROFILES["gemini"]
    value = _measure_facade._strip_provider_prefixes(model) if model else ""
    if value.startswith("deepseek"):
        return "deepseek", _measure_facade.PROVIDER_CACHE_PROFILES["deepseek"]
    if _measure_facade._normalize_model_name(model) in ("fable", "opus", "sonnet", "haiku"):
        if runtime == "claude":
            # Claude Code: the platform requests a 1h cache (no user knob).
            return "claude_code", _measure_facade.PROVIDER_CACHE_PROFILES["claude_code"]
        # Raw Anthropic API/SDK/harness session (e.g. Hermes -> Anthropic): the
        # 5-min default applies and the user can set a 1h cache_control.
        return "anthropic_api", _measure_facade.PROVIDER_CACHE_PROFILES["anthropic_api"]
    return "unknown", _measure_facade.PROVIDER_CACHE_PROFILES["unknown"]


def _strip_provider_prefixes(model):
    _measure_facade = _compat.facade()
    value = str(model).strip().lower()
    while True:
        dotted = _measure_facade._DOTTED_PROVIDER_PREFIX_RE.match(value)
        if dotted:
            value = value[dotted.end():]
            continue
        slash = value.find("/")
        colon = value.find(":")
        if slash == -1 and colon == -1:
            return value
        if slash != -1 and (colon == -1 or slash < colon):
            idx = slash
            delimiter = "/"
        else:
            idx = colon
            delimiter = ":"
        prefix = value[:idx]
        rest = value[idx + 1:]
        if not rest or not _measure_facade.re.search(r"[a-z]", rest):
            return value
        if delimiter == "/" or prefix in _measure_facade._KNOWN_PROVIDER_PREFIXES:
            value = rest
            continue
        return value


def _load_pricing_tier():
    """Load pricing tier preference from config (user, then the project
    layer via _read_config_flag). Defaults to 'anthropic'."""
    _measure_facade = _compat.facade()
    try:
        tier = _measure_facade._read_config_flag("pricing_tier", "anthropic")
        if isinstance(tier, str) and tier in _measure_facade.PRICING_TIERS:
            return tier
    except Exception:
        pass
    return "anthropic"


def _pricing_tier_label(tier):
    _measure_facade = _compat.facade()
    if _measure_facade.detect_runtime() == "codex":
        return "OpenAI API pricing for recognized Codex models"
    return _measure_facade.PRICING_TIERS.get(tier, {}).get("label", "Anthropic API")


def _save_pricing_tier(tier):
    """Persist pricing tier preference via the atomic+locked config writer."""
    _measure_facade = _compat.facade()
    _measure_facade._write_config_flag("pricing_tier", tier)


def _is_priced_model(model, tier=None):
    """True when Token Optimizer has an exact rate card for this model id."""
    _measure_facade = _compat.facade()
    if _measure_facade._normalize_openai_model_name(model):
        return True
    if _measure_facade._normalize_gemini_model_name(model):
        return True
    if tier is None:
        tier = _measure_facade._load_pricing_tier()
    tier_data = _measure_facade.PRICING_TIERS.get(tier, _measure_facade.PRICING_TIERS["anthropic"])
    claude_models = tier_data.get("claude_models", {})
    normalized = _measure_facade._claude_price_key(model, claude_models) if model else None
    return bool(normalized and normalized in claude_models)


def _price_aliases(table):
    """Priced ids of a rate table, longest first; cached until the table changes."""
    _measure_facade = _compat.facade()
    sig = (id(table), len(table))
    cached = _measure_facade._PRICE_ALIAS_CACHE.get(sig)
    # keys() == frozenset compares in C without building sets: this runs once
    # per priced request, so a per-call copy of the table showed up in profiles.
    if cached is None or table.keys() != cached[1]:
        cached = (tuple(sorted(table, key=len, reverse=True)), frozenset(table))
        _measure_facade._PRICE_ALIAS_CACHE[sig] = cached
    return cached[0]


def _match_price_alias(value, table):
    """Longest priced id that `value` equals or that `value` is a snapshot of
    (the id plus a date or number suffix). A different variant (-mini, -lite,
    -image, a later generation) has its own price and never matches a shorter
    card. Memoized per alias set so repeated model ids resolve with one lookup."""
    _measure_facade = _compat.facade()
    aliases = _measure_facade._price_aliases(table)
    memo = _measure_facade._PRICE_MATCH_MEMO.get(id(table))
    if memo is None or memo[0] is not aliases:
        memo = (aliases, {})
        _measure_facade._PRICE_MATCH_MEMO[id(table)] = memo
    hit = memo[1].get(value, _measure_facade._MISSING)
    if hit is _measure_facade._MISSING:
        hit = next((a for a in aliases
                    if value == a or (value.startswith(a)
                                      and _measure_facade._PRICE_SNAPSHOT_RE.fullmatch(value, len(a)))), None)
        if len(memo[1]) < 4096:
            memo[1][value] = hit
    return hit


def _normalize_openai_model_name(model):
    """Return a priced OpenAI model id, or None when we cannot price exactly."""
    _measure_facade = _compat.facade()
    if not model:
        return None
    value = _measure_facade.re.sub(r"[\s_]+", "-", _measure_facade._strip_provider_prefixes(model))
    if not value or value in {"codex", "openai", "unknown"}:
        return None
    if value in ("gpt-5.6", "gpt-5.6-sol-pro"):
        return "gpt-5.6-sol"  # bare alias: OpenAI's docs name Sol the default 5.6; "Sol Pro" is its marketing name
    # Every priced id is an alias, longest first so "gpt-5.4-mini" wins over
    # "gpt-5.4" and dated snapshots ("gpt-5.4-2026-03-05") map to their base.
    alias = _measure_facade._match_price_alias(value, _measure_facade.OPENAI_MODEL_PRICING)
    if alias == "gpt-5.6":
        return "gpt-5.6-sol"  # dated bare-5.6 ids follow the same Sol default
    if alias:
        return alias
    if _measure_facade.re.fullmatch(r"gpt-5\.6(?:" + _measure_facade._PRICE_SNAPSHOT_RE.pattern + ")", value):
        return "gpt-5.6-sol"
    return None


def _normalize_gemini_model_name(model):
    """Return a priced Gemini model id, or None when we cannot price exactly."""
    _measure_facade = _compat.facade()
    if not model:
        return None
    value = _measure_facade._strip_provider_prefixes(model)
    if not value.startswith("gemini-"):
        return None
    if value.startswith("gemini-2.0"):
        if value not in _measure_facade._GEMINI_DEPRECATION_WARNED:
            _measure_facade._GEMINI_DEPRECATION_WARNED.add(value)
            print(f"[Token Optimizer] WARNING: {value} was deprecated June 1, 2026. Migrate to gemini-2.5-flash or gemini-3.5-flash.", file=_measure_facade.sys.stderr)
        return None
    hit = _measure_facade._match_price_alias(value, _measure_facade.GEMINI_MODEL_PRICING)
    if hit:
        return hit
    # A preview build of a priced model ("-preview", "-preview-05-20",
    # "-preview-09-2025") is the same model; any other variant is not.
    base = _measure_facade.re.sub(r"-preview(?:-\d{2}-\d{2,4})?$", "", value)
    return _measure_facade._match_price_alias(base, _measure_facade.GEMINI_MODEL_PRICING) if base != value else None


def _resolve_session_model(session_id=None):
    """Return the dominant normalized model name for cost attribution.

    Resolution order:
      1. Explicit session_id → most frequent `message.model` in that JSONL
      2. CLAUDE_MODEL environment variable (set by Claude Code hook runner)
      3. Most recent session's dominant model from trends DB
      4. Fall back to "sonnet"

    Never raises. Always returns a normalized name ("opus"|"sonnet"|"haiku"|"sonnet" default).
    """
    _measure_facade = _compat.facade()
    if _measure_facade.detect_runtime() == 'codex':
        # Claude message.model/environment/defaults are not Codex telemetry.
        # Do not cache across model switches, or borrow another task's model.
        path = _measure_facade.codex_session.find_session_jsonl_by_id(session_id) if session_id else None
        model = None
        if path:
            for record in _measure_facade.codex_session._iter_json_records(path):
                candidate = _measure_facade.codex_session._extract_model(_measure_facade.codex_session._payload(record))
                if candidate:
                    model = candidate
        return model or (None if session_id else _measure_facade._codex_config_model()) or 'unknown'
    cache_key = session_id or "__env_or_recent__"
    if cache_key in _measure_facade._RESOLVED_MODEL_CACHE:
        return _measure_facade._RESOLVED_MODEL_CACHE[cache_key]

    result = None

    # 1. Try session JSONL
    if session_id:
        session_id = _measure_facade.sanitize_session_id(session_id)
        try:
            # The session id names its transcript exactly; find it wherever it
            # lives rather than trusting a cwd lookup that can guess another
            # project's dir (#265).
            candidate = next(iter(sorted((_measure_facade.CLAUDE_DIR / "projects").glob(f"*/{session_id}.jsonl"))), None)
            if candidate is None:
                projects_dir = _measure_facade.find_projects_dir()
                candidate = projects_dir / f"{session_id}.jsonl" if projects_dir else None
            if candidate is not None:
                if candidate.exists():
                    counts = {}
                    with open(candidate, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            try:
                                data = _measure_facade.json.loads(line)
                                msg = data.get("message") if isinstance(data, dict) else None
                                if isinstance(msg, dict):
                                    m = msg.get("model")
                                    norm = _measure_facade._normalize_model_name(m) if m else None
                                    if norm:
                                        counts[norm] = counts.get(norm, 0) + 1
                            except (_measure_facade.json.JSONDecodeError, KeyError, TypeError):
                                continue
                    if counts:
                        result = max(counts, key=counts.get)
        except (OSError, PermissionError):
            pass

    # 2. Try CLAUDE_MODEL env var — but only for Claude-family runtimes. A
    # foreign runtime (cursor, grok, opencode, copilot, antigravity) running
    # on a host that also has CLAUDE_MODEL set would otherwise be priced at
    # Claude model rates, the exact cross-runtime leak the isolation pass
    # closes for detect_context_window() and quick_scan().
    if not result:
        _rt = _measure_facade.detect_runtime()
        if _rt in ("claude", "hermes"):
            env_model = _measure_facade.os.environ.get("CLAUDE_MODEL") or _measure_facade.os.environ.get("ANTHROPIC_MODEL")
            if env_model:
                norm = _measure_facade._normalize_model_name(env_model)
                if norm in ("opus", "sonnet", "haiku"):
                    result = norm

    # 3. Try trends DB for most-recent dominant model
    if not result:
        try:
            if _measure_facade.TRENDS_DB.exists():
                conn = _measure_facade._init_trends_db()
                _measure_facade.ensure_activity_view(conn)
                try:
                    row = conn.execute(
                        "SELECT model_usage_json FROM session_activity "
                        "WHERE model_usage_json IS NOT NULL AND model_usage_json != '' "
                        "ORDER BY date DESC LIMIT 1"
                    ).fetchone()
                    if row and row[0]:
                        mu = _measure_facade.json.loads(row[0])
                        if isinstance(mu, dict) and mu:
                            # normalize keys and sum
                            norm_counts = {}
                            for k, v in mu.items():
                                nk = _measure_facade._normalize_model_name(k) or k
                                try:
                                    norm_counts[nk] = norm_counts.get(nk, 0) + int(v)
                                except (TypeError, ValueError):
                                    continue
                            if norm_counts:
                                result = max(norm_counts, key=norm_counts.get)
                finally:
                    conn.close()
        except (_measure_facade.sqlite3.Error, _measure_facade.json.JSONDecodeError, OSError):
            pass

    if not result or result not in ("opus", "sonnet", "haiku"):
        result = "sonnet"

    _measure_facade._RESOLVED_MODEL_CACHE[cache_key] = result
    return result


def _simulate_model_switch(session_data, target_model=None):
    """Estimate cost delta if target_model was used instead of the dominant model.

    Returns dict with: current_cost, target_cost, savings_usd, savings_pct.
    """
    _measure_facade = _compat.facade()
    model_usage = session_data.get("model_usage", {})
    total_input = session_data.get("total_input_tokens", 0)
    total_output = session_data.get("total_output_tokens", 0)
    cache_hit = session_data.get("cache_hit_rate", 0)
    cache_read = _measure_facade._safe_int(total_input * cache_hit)
    uncached = max(0, total_input - cache_read)

    dom_model = max(model_usage, key=model_usage.get) if model_usage else "unknown"
    current_cost = _measure_facade._get_model_cost(dom_model, uncached, total_output, cache_read, 0)
    target_cost = _measure_facade._get_model_cost(target_model, uncached, total_output, cache_read, 0)
    savings = current_cost - target_cost

    return {
        "current_cost": round(current_cost, 4),
        "target_cost": round(target_cost, 4),
        "savings_usd": round(max(0, savings), 4),
        "savings_pct": round(savings / current_cost * 100, 1) if current_cost > 0 else 0,
    }


def _cost_from_model_breakdown(model_usage_breakdown, tier=None, cache_create_1h=None, cache_create_5m=None):
    """Calculate exact known cost from per-model token buckets."""
    _measure_facade = _compat.facade()
    if not isinstance(model_usage_breakdown, dict):
        return 0.0
    total = 0.0
    for model, parts in model_usage_breakdown.items():
        if not isinstance(parts, dict):
            continue
        # Long-context pricing applies per request, never to a session sum.
        requests = parts.get('requests')
        if isinstance(requests, list) and requests:
            total += sum(_measure_facade._cost_from_model_breakdown({model: request}, tier=tier)
                         for request in requests if isinstance(request, dict))
            # Bucket totals include deltas from records that were never
            # appended to requests (e.g. a cache-read-only turn with zero
            # input/output, or cache_create which requests never carry).
            # Price the unpriced remainder at the aggregate rate instead of
            # silently dropping those tokens from the cost.
            remainder = {
                key: max(0, int(parts.get(key) or 0) - sum(
                    int(request.get(key) or 0) for request in requests
                    if isinstance(request, dict)))
                for key in ('fresh_input', 'cache_read', 'output',
                            'cache_create', 'cache_create_1h', 'cache_create_5m')
            }
            if any(remainder.values()):
                total += _measure_facade._cost_from_model_breakdown({model: remainder}, tier=tier)
            continue
        part_1h = parts.get("cache_create_1h")
        part_5m = parts.get("cache_create_5m")
        if part_1h is None and part_5m is None and len(model_usage_breakdown) == 1:
            part_1h = cache_create_1h
            part_5m = cache_create_5m
        total += _measure_facade._get_model_cost(
            model,
            int(parts.get("fresh_input") or 0),
            int(parts.get("output") or 0),
            int(parts.get("cache_read") or 0),
            int(parts.get("cache_create") or 0),
            tier=tier,
            cache_create_1h=part_1h,
            cache_create_5m=part_5m,
        )
    return total


def _resolve_platform_models(runtime):
    """Resolve a platform's real tier->model ladder from ITS OWN config, or None.

    Only user-configured platforms need this; None means "use the routing table".
    - opencode: reads opencode.json (`model` / `small_model`, provider-qualified).
      small_model -> budget, model -> mid/capable/frontier. Absent field or
      unreadable config -> None (table fallback).
    - hermes: a Claude runtime with no per-tier model config, so the Claude ladder
      in the table is already correct -> None.
    Never raises.
    """
    _measure_facade = _compat.facade()
    try:
        if runtime != "opencode":
            return None
        from runtime_env import opencode_config_home
        cfg_path = opencode_config_home() / "opencode.json"
        if not cfg_path.is_file():
            return None
        with cfg_path.open("r", encoding="utf-8") as handle:
            cfg = _measure_facade.json.load(handle)
        if not isinstance(cfg, dict):
            return None

        def _leaf(value):
            # "anthropic/claude-sonnet-4" -> "claude-sonnet-4"; keep advisory-friendly.
            _measure_facade = _compat.facade()
            if not (isinstance(value, str) and value.strip()):
                return None
            return value.strip().rsplit("/", 1)[-1]

        main = _leaf(cfg.get("model"))
        small = _leaf(cfg.get("small_model")) or main
        main = main or small
        if not main:
            return None
        return {"budget": small, "mid": main, "capable": main, "frontier": main}
    except Exception:
        return None


def _cmd_route(args):
    """Recommend a model and reasoning effort for a task on the CURRENT platform.

    Works on every supported runtime (it reads only the task text and the
    detected platform, never the Claude tree), so it is intentionally NOT a
    Claude-targeting command. Fails open to a safe middle recommendation.
    """
    _measure_facade = _compat.facade()
    import json as _json
    as_json = "--json" in args
    task = ""
    _eq = [a for a in args if a.startswith("--task=")]
    if "--task" in args:
        i = args.index("--task")
        if i + 1 < len(args):
            task = args[i + 1]
    elif _eq:
        task = _eq[-1][len("--task="):]
    else:
        # Positional task: keep every token except the recognized --json flag so
        # task words that happen to start with "--" are not silently dropped.
        task = " ".join(a for a in args[1:] if a != "--json")
    _rt = _measure_facade.detect_runtime()
    try:
        import routing_advisor
        rec = routing_advisor.recommend(task, _rt, models=_measure_facade._resolve_platform_models(_rt))
    except Exception as _e:
        # Router module unavailable: a real, non-cheap default with the same
        # keys as a normal recommendation so JSON consumers never KeyError.
        # Model name comes from the platform's own ladder, never a hardcoded
        # Claude tier under a non-Claude runtime.
        _mid = "sonnet"
        try:
            _row = routing_advisor.ROUTING_TABLES.get(_rt) or routing_advisor._GENERIC_ROW
            _mid = _row["models"]["mid"]
        except Exception:
            if _rt == "codex":
                _mid = "gpt-5.6-terra"
        rec = {
            "model": _mid, "effort": "medium", "significance": "standard",
            "confidence": "low", "category": "simple", "tier": "mid",
            "effort_kind": "advisory", "effort_knob": "effort",
            "floor": {"min_tier": "mid", "min_effort": "medium"},
            "why": f"router unavailable ({type(_e).__name__}); safe default",
        }
    if as_json:
        print(_json.dumps(rec))
        return
    print(f"  model:  {rec.get('model')}")
    print(f"  effort: {rec.get('effort')}  ({rec.get('effort_knob', 'effort')})")
    print(f"  why:    {rec.get('why')}")


def generate_model_routing_block(trends=None):
    """Generate the model + effort routing block for the CURRENT platform.

    Renders the current platform's own model ladder and effort control, states
    the easy-only rule for the cheapest models and lowest efforts, and points at
    the live `route` command. Recent model-mix stats are added as evidence when
    available. Returns markdown suitable for instruction-file injection.
    """
    _measure_facade = _compat.facade()
    try:
        import routing_advisor
    except Exception:
        return None

    runtime = _measure_facade.detect_runtime()
    row = routing_advisor.platform_row(runtime)
    resolved = _measure_facade._resolve_platform_models(runtime)
    models = resolved if isinstance(resolved, dict) else row["models"]

    easy_m, easy_e = routing_advisor.baseline("easy", runtime, models=resolved)
    std_m, std_e = routing_advisor.baseline("standard", runtime, models=resolved)
    hard_m, hard_e = routing_advisor.baseline("hard", runtime, models=resolved)
    knob = row["effort_knob"]

    lines = [
        "## Model & Effort Routing (by Token Optimizer)",
        f"For each task pick both a model and an effort level. Effort maps to this "
        f"platform's {knob}.",
        f"- Easy (typo, rename, format, grep, single-file): {easy_m} at {easy_e} effort.",
        f"- Standard (a bounded feature or fix): {std_m} at {std_e} effort.",
        f"- Hard (security, migrations, concurrency, architecture): {hard_m} at {hard_e} effort.",
        f"- The cheapest models ({models.get('budget')}) and the lowest efforts are for "
        f"easy work only. Never route a significant task there.",
        "- For a specific task, run: `measure.py route --task \"...\"`.",
    ]

    if trends is None:
        try:
            trends = _measure_facade._collect_trends_data(days=30)
        except Exception:
            trends = None
    try:
        model_mix = (trends or {}).get("model_mix", {})
        total = sum(model_mix.values()) if isinstance(model_mix, dict) else 0
        if total:
            top = ", ".join(f"{round(v / total * 100)}% {k}"
                            for k, v in sorted(model_mix.items(), key=lambda kv: -kv[1])[:3])
            lines.append(f"- Your last 30 days: {top}.")
    except Exception:
        # Malformed trends never break the routing policy block.
        pass

    return "\n".join(lines)


def _dominant_turn_model(turns):
    """Most frequent non-empty model id across a session's turns."""
    _measure_facade = _compat.facade()
    counts = {}
    for t in turns:
        m = t.get("model")
        if m and not str(m).startswith("<"):
            counts[m] = counts.get(m, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _detect_explicit_ttl(turns, tier_data, profile, dominant_model=None, detail_out=None):
    """explicit_ttl detector: a gap > the profile's EFFECTIVE TTL followed by a
    re-WRITE of the prior cached prefix is cache-expiry waste.

    The detection boundary is per-profile (`effective_ttl_seconds`):
      - anthropic_api: 300s (the live 5-min default; the user CAN raise it).
      - claude_code: 3600s. Claude Code REQUESTS a 1-hour cache (v2.1.129 fixed
        the silent downgrade to 5 min), and empirically the prefix survives
        sub-hour pauses, so only pauses LONGER than the hour count as waste.

    The "recoverable" value depends on the profile's `recoverable_kind`:
      - recoverable_1h (anthropic_api): the honest 1h-cache_control counterfactual
        — banks the avoidable repeat-write cost minus the 2x-vs-1.25x premium on
        the first (unavoidable) write, floored at 0.
      - avoidable_behavioral (claude_code): there is NO setting to extend the
        cache; the only lever is behavioral (resume within the hour / batch
        related work). The avoidable value is the full waste delta a within-hour
        resume would have spared (read at 0.1x instead of re-writing). No
        first-write premium is netted (Claude Code already writes at 1h rates).

    Returns (wasted_tokens, wasted_cost_usd, recoverable_usd, had_waste).
    """
    _measure_facade = _compat.facade()
    boundary = profile.get("effective_ttl_seconds")
    # Default to the CONSERVATIVE kind: an unknown recoverable_kind must never
    # inflate the recoverable claim (recoverable_1h is the most aggressive
    # accounting). avoidable_behavioral books no 1h-cache_control counterfactual.
    recoverable_kind = profile.get("recoverable_kind", "avoidable_behavioral")
    # Only the recoverable_1h branch needs the repeat-write annuity + first-write
    # accumulators; for any other kind they are computed and discarded, so gate
    # their accumulation on that kind.
    track_repeat_write = recoverable_kind == "recoverable_1h"
    session_waste_tokens = 0
    session_waste_cost = 0.0
    session_repeat_write_cost = 0.0
    first_write_tokens = 0
    had = False

    # Hoist the model->rate lookup out of the per-turn loop: the session is
    # resolved to a single cache profile via its dominant model, so its rates are
    # constant for the whole session. W5: the caller already resolved the
    # dominant model (the same value used to pick this profile); reuse it instead
    # of re-scanning every turn. Fall back to a local scan only if not supplied.
    model = dominant_model if dominant_model is not None else _measure_facade._dominant_turn_model(turns)
    rates = _measure_facade._claude_rates_for_model(model, tier_data)
    if rates is None:
        return 0, 0.0, 0.0, False
    write_5m = rates.get("cache_write", rates.get("input", 3.0))
    write_1h = rates.get("cache_write_1h", write_5m)
    read_rate = rates.get("cache_read", rates.get("input", 3.0) * 0.1)
    # No per-turn TTL split in the log: price the re-write at the profile's actual
    # write rate. Claude Code holds a 1h cache (avoidable_behavioral -> write_1h);
    # raw API/SDK runs at the 5m default (recoverable_1h -> write_5m).
    default_write = write_1h if recoverable_kind == "avoidable_behavioral" else write_5m

    prev = None
    for turn in turns:
        cc = int(turn.get("cache_creation", 0) or 0)
        cc_1h = int(turn.get("cache_creation_1h", 0) or 0)
        cc_5m = int(turn.get("cache_creation_5m", 0) or 0)
        if track_repeat_write and cc > 0 and first_write_tokens == 0:
            first_write_tokens = cc
        if prev is not None:
            gap = turn.get("gap_since_prev_seconds")
            # Threshold basis is executor occupancy (#287); the dollars below
            # stay on the billed aggregate `cc`.
            prev_cached = _measure_facade._turn_prefix_cached(prev)
            if (
                gap is not None
                and boundary is not None
                and gap > boundary
                and prev_cached > 0
                and _measure_facade._turn_resume_write(turn) >= _measure_facade._CACHE_REWRITE_PREFIX_FRACTION * prev_cached
            ):
                if cc_1h or cc_5m:
                    split_1h = min(cc_1h, cc)
                    split_5m = max(0, cc - split_1h)
                    paid = (split_1h * write_1h + split_5m * write_5m) / 1e6
                else:
                    paid = cc * default_write / 1e6
                avoided_read = cc * read_rate / 1e6
                session_waste_tokens += cc
                session_waste_cost += max(0.0, paid - avoided_read)
                if track_repeat_write:
                    session_repeat_write_cost += cc * write_5m / 1e6
                had = True
                if detail_out is not None and gap > detail_out.get("max_gap", 0):
                    detail_out["max_gap"] = gap  # W8: widest waste-triggering gap
        prev = turn

    recoverable = 0.0
    if had:
        if recoverable_kind == "recoverable_1h":
            # 1h-cache_control counterfactual (anthropic_api only): repeat writes
            # avoided, less the 2x-vs-1.25x premium on the first write.
            first_premium = 0.0
            if first_write_tokens > 0:
                first_premium = first_write_tokens * (write_1h - write_5m) / 1e6
            recoverable = max(0.0, session_repeat_write_cost - first_premium)
        else:
            # avoidable_behavioral (claude_code): no TTL setting to buy back the
            # waste; a within-the-hour resume would have spared the whole delta.
            recoverable = session_waste_cost
    return session_waste_tokens, session_waste_cost, recoverable, had


def _detect_automatic_collapse(turns, profile, dominant_model=None, detail_out=None):
    """automatic_discount / explicit_storage detector (OpenAI, Gemini, code review).

    These providers never emit a cache-CREATION event; the cache is automatic and
    cached reads are discounted. Expiry shows up as a COLLAPSE in the cached/prompt
    ratio: turn N has a healthy cached ratio, the gap to turn N+1 exceeds the
    profile TTL, and turn N+1's cached ratio collapses while its prompt size stays
    comparable. The collapsed (lost) cached tokens are re-billed at the FULL input
    rate instead of the discounted cached rate.

    waste_cost = lost_cached_tokens * (input_rate - cached_rate).
    For these providers the recoverable value EQUALS the waste cost (resuming
    within the cache window would have avoided the full premium; there is no
    extra-write premium to net out as in the explicit_ttl 1h counterfactual).

    Returns (wasted_tokens, wasted_cost_usd, recoverable_usd, had_waste).
    """
    _measure_facade = _compat.facade()
    ttl = profile.get("effective_ttl_seconds") or 300
    session_waste_tokens = 0
    session_waste_cost = 0.0
    had = False

    prev = None
    for turn in turns:
        cr = int(turn.get("cache_read", 0) or 0)
        inp = int(turn.get("input_tokens", 0) or 0)  # total billed input (incl. cached)
        prompt = max(inp, cr)  # turn's prompt size (input_tokens already includes cached)
        ratio = (cr / prompt) if prompt > 0 else 0.0
        if prev is not None:
            gap = turn.get("gap_since_prev_seconds")
            # Codex turns carry timestamps but not a precomputed gap; derive it.
            if gap is None:
                gap = _measure_facade._gap_seconds(prev.get("timestamp"), turn.get("timestamp"))
            prev_cr = int(prev.get("cache_read", 0) or 0)
            prev_inp = int(prev.get("input_tokens", 0) or 0)
            prev_prompt = max(prev_inp, prev_cr)
            prev_ratio = (prev_cr / prev_prompt) if prev_prompt > 0 else 0.0
            if (
                gap is not None
                and gap > ttl
                and prev_ratio >= _measure_facade._CACHE_COLLAPSE_HEALTHY_RATIO
                and ratio < _measure_facade._CACHE_COLLAPSE_THRESHOLD_RATIO
                and prompt >= _measure_facade._CACHE_COLLAPSE_PROMPT_COMPARABLE_FRACTION * prev_prompt
            ):
                # Lost cached tokens = what WOULD have been cached at the prior
                # healthy ratio but now reads as fresh input.
                # W1: the recoverable cached prefix can never exceed what the
                # PREVIOUS turn actually had cached (prev_cr). prev_ratio*prompt
                # extrapolates the prior hit-rate onto this (possibly larger)
                # turn, which can overstate the lost volume when the prompt grew;
                # cap at the real prior cached volume so waste stays grounded.
                lost = min(int(prev_ratio * prompt) - cr, prev_cr)
                if lost > 0:
                    # W2: turns may omit `model` (e.g. a tool-result-only turn);
                    # fall back to the session's dominant model for rate lookup
                    # so the rate is found instead of the pair being dropped.
                    rr = _measure_facade._input_and_cached_read_rates(turn.get("model") or dominant_model)
                    if rr is not None:
                        input_rate, cached_rate = rr
                        premium = lost * (input_rate - cached_rate) / 1e6
                        session_waste_tokens += lost
                        session_waste_cost += max(0.0, premium)
                        had = True
                        if detail_out is not None and gap > detail_out.get("max_gap", 0):
                            detail_out["max_gap"] = gap  # W8: widest collapse gap
        prev = turn

    # For automatic-discount providers, recoverable == waste (no extra-write
    # premium to net out; staying within the window avoids the whole delta).
    return session_waste_tokens, session_waste_cost, session_waste_cost, had


def _gap_seconds(ts_a, ts_b):
    """Seconds between two ISO/epoch timestamps, or None if unparseable."""
    _measure_facade = _compat.facade()
    def _parse(ts):
        _measure_facade = _compat.facade()
        if ts is None:
            return None
        if isinstance(ts, (int, float)):
            return float(ts)
        s = str(ts).strip()
        if not s:
            return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = _measure_facade.datetime.fromisoformat(s)
            # W3: a tz-NAIVE ISO timestamp would otherwise be interpreted in the
            # host's LOCAL zone by .timestamp(), so two naive stamps compared
            # across a DST shift (or on a non-UTC host) would yield a wrong gap.
            # Force UTC for naive values; aware values keep their own offset.
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_measure_facade.timezone.utc)
            return dt.timestamp()
        except ValueError:
            try:
                return float(s)
            except ValueError:
                return None
    a = _parse(ts_a)
    b = _parse(ts_b)
    if a is None or b is None:
        return None
    return abs(b - a)


def _cache_remedy_text(remedy_key):
    """Return the verified, per-profile cache remedy text.

    Verified 2026-06-11 against current public docs (Anthropic prompt-caching,
    Claude Code CHANGELOG, OpenAI prompt-caching, Gemini context-caching,
    code review context-caching). Each remedy reflects exactly what that provider
    actually offers — no overclaiming.
    """
    _measure_facade = _compat.facade()
    remedies = {
        "anthropic_api": (
            'Anthropic API/SDK/agent harness: set cache_control '
            '{"type":"ephemeral","ttl":"1h"} on stable prefixes. 1h write costs 2x '
            "input once, then reads at 0.1x — cheaper than re-writing every 5min."
        ),
        "claude_code": (
            "Claude Code already holds a 1-hour cache. Waste comes from pauses "
            "longer than an hour: resuming within the hour keeps the prefix warm. "
            "No setting can extend this further today."
        ),
        "openai": (
            "OpenAI/Codex prompt caching is automatic (~5-10min inactivity, max ~1h) "
            "with NO TTL knob below the policy level. Behavioral remedy: keep the "
            "prompt PREFIX stable (exact-match, >=1024 tokens) and resume within the "
            "window. For long-lived prefixes set prompt_cache_retention=\"24h\"."
        ),
        "gemini": (
            "Gemini 2.5+ has automatic implicit caching (no knob). For long stable "
            "contexts, use EXPLICIT context caching: create a cached_content resource "
            "with a user-set ttl (default 1h) — note per-hour storage is billed for "
            "the cached tokens, so size the TTL to your reuse pattern."
        ),
        "deepseek": (
            "code review Context Caching on Disk is automatic (no knob); cache hits cost "
            "1/10 of input. Keep prompt prefixes stable and reuse them promptly while "
            "the on-disk cache is warm (it clears within hours-to-days of disuse)."
        ),
        "none": (
            "This model has no prompt cache; prefix churn does not affect its cost. "
            "There is no cache-TTL waste to remedy here."
        ),
    }
    return remedies.get(remedy_key, remedies["none"])


def _cache_coverage_gaps(runtime):
    """Return the explicit not-measurable coverage-gap list (parity rule).

    Each entry: {platform, measurable, reason}. Platforms whose per-turn cache +
    timestamp data the Python engine cannot read are honestly listed here rather
    than silently dropped. A platform that is wired for measurement
    (_CACHE_WIRED_RUNTIMES) or is the active runtime is omitted — so a wired
    platform can never be both measured and listed as a gap.
    """
    _measure_facade = _compat.facade()
    return [
        {"platform": platform, "measurable": False, "reason": reason}
        for platform, reason in _measure_facade._CACHE_COVERAGE_GAP_REASONS.items()
        if platform != runtime and platform not in _measure_facade._CACHE_WIRED_RUNTIMES
    ]


def _session_mix_class_terms(rate_class, *model_usage_blobs):
    """(terms, unpriced_share) for ONE session's own token-weighted model usage at
    one token class, or None when no usable usage is present.

    *terms* is [(share, usd_per_mtok)] for every model id with an exact rate card
    (any provider); *unpriced_share* is the share of the session's recorded tokens
    held by ids with no exact card. *rate_class* is a key of _routing_class_rates
    ("fresh_input", "output", "cache_create_1h", ...). Each id is priced at its
    own generation card (Opus 5.5 is not the Opus family rate); an id without an
    exact card contributes no term and is never borrowed from a family.

    Shares are taken over the session's TOTAL recorded tokens, so a priced sliver
    is never inflated to stand for the whole mix. The caller prices the unpriced
    share at zero and discloses it.

    Blobs are tried in order (model_usage_json, then all_model_usage_json); the
    first that parses to a non-empty dict wins. Never raises.
    """
    _measure_facade = _compat.facade()
    raw = None
    for blob in model_usage_blobs:
        if not blob:
            continue
        try:
            data = _measure_facade.json.loads(blob)
        except (_measure_facade.json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(data, dict) and data:
            raw = data
            break
    if not raw:
        return None
    total = 0.0
    weights = {}
    for key, val in raw.items():
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        if val <= 0 or not isinstance(key, str) or key.startswith("<"):
            continue
        weights[key] = weights.get(key, 0.0) + val
        total += val
    if total <= 0:
        return None
    terms = []
    priced = 0.0
    for key, val in weights.items():
        rates = _measure_facade._routing_class_rates(key)
        rate = (rates or {}).get(rate_class)
        if rate:
            terms.append((val / total, float(rate)))
            priced += val
    return terms, max(0.0, 1.0 - priced / total)


def _stored_model_is_unpriced(model):
    """True when a savings row records a model id that has no exact rate card.
    Rate-card keys and bare family labels ("opus", "sonnet_4_5") are known; a row
    with no model, or a known one, is not unpriced."""
    _measure_facade = _compat.facade()
    if not isinstance(model, str) or not model.strip():
        return False
    if _measure_facade._routing_card_exact(model):
        return False
    try:
        cards = _measure_facade.PRICING_TIERS.get(_measure_facade._load_pricing_tier(), _measure_facade.PRICING_TIERS["anthropic"])["claude_models"]
    except Exception:
        cards = {}
    return model not in cards


def _session_model_reprice(conn, cutoff, session_uuid=None, by_day=None, unpriced_out=None):
    """Per-event_type USD correction from repricing savings_events at each event's
    OWN session model mix. Returns (deltas_by_event_type, repriced_tokens,
    unmeasured_tokens). With *session_uuid* only that session's rows count; with
    *by_day* the deltas are keyed (local YYYY-MM-DD, event_type). When
    *unpriced_out* is a dict, ``unpriced_out["tokens"]`` accumulates the saved
    tokens that belong to models with no exact rate card.

    THE DEFECT THIS REPAIRS. _log_savings_event stamps every row with
    _resolve_session_model(), which falls back to "sonnet" whenever the session
    JSONL cannot be read (sub-agent / sidechain sessions, sessions still open when
    the event is written); older rows carry model IS NULL and are priced at the
    flat _estimate_compression_cost_per_mtok fallback. Measured on the real ledger
    (30d, 2026-08-29): 18.9M saved tokens carry model IS NULL and 13.9M carry
    model='sonnet', while the session_log rows they join to record 17.8M and 4.6M
    of those same tokens inside sessions whose OWN token-weighted usage is Opus.
    The rate card is correct; the per-row model stamp is not.

    WHAT THIS DELIBERATELY IS NOT. It is not a reprice to the WINDOW's blended
    model mix. That would lift a saving made inside a session Token Optimizer had
    routed to a cheaper model up to an Opus-heavy window average, claiming the
    routing win a second time inside the context pool -- and
    _compute_model_routing_savings already owns that delta for the tokens that
    WERE sent. Pricing each event against its own session keeps the two pools
    disjoint by construction: a genuinely-Sonnet session stays at the Sonnet rate.

    HONESTY BOUNDARY. session_log.model_usage_json is a measured column (real
    token counts), but attributing one never-sent saving to a session's blended
    rate is an ALLOCATION, not a measurement -- we cannot know which turn's model
    would have carried tokens that were never sent. So the corrected figure is
    labelled "estimated" wherever it surfaces (see _window_overage_usd), and rows
    that cannot be joined to a session are NOT MEASURED: they keep their
    event-time price and are counted separately rather than guessed at.

    UNPRICED MODELS. The share of a session's tokens that ran on a model with no
    exact rate card (a local or third-party model, a generation without a card) is
    priced at zero, not at the Sonnet stand-in the event writer stamped. Those
    tokens are reported through *unpriced_out*. Stored rows are never changed.

    Degrades to ({}, 0, 0) on any schema or query problem. An older DB without
    session_uuid / model_usage_json simply does not get the repair; a schema error
    must never be allowed to reach the caller's blanket handler and zero out the
    whole savings summary (that failure mode vanished an entire dashboard card on
    2026-08-28).
    """
    _measure_facade = _compat.facade()
    deltas, repriced_tokens, unmeasured_tokens = {}, 0, 0
    try:
        se_cols = {r[1] for r in conn.execute("PRAGMA table_info(savings_events)").fetchall()}
        sl_cols = {r[1] for r in conn.execute("PRAGMA table_info(session_log)").fetchall()}
        if "session_uuid" not in se_cols or "session_uuid" not in sl_cols:
            return {}, 0, 0
        # by_day / session_uuid serve the status-bar buckets. The delta is linear
        # in (tokens, cost) per session, so splitting by local day sums back to
        # exactly the window delta.
        params = [cutoff]
        if session_uuid is not None:
            # Same rows as the savings buckets (non-UUID ids live in session_id).
            # Those rows have no session_uuid to join, so they stay unmeasured here.
            params.extend([session_uuid, session_uuid])
        reprice_sql = _measure_facade._REPRICE_SQL[(
            _measure_facade._counted_table(conn, "savings_events"),
            "model_usage_json" in sl_cols,
            "all_model_usage_json" in sl_cols,
            bool(by_day),
            session_uuid is not None,
            "model" in se_cols,
        )]
        rows = conn.execute(reprice_sql, tuple(params)).fetchall()
    except (_measure_facade.sqlite3.Error, TypeError, ValueError):
        return {}, 0, 0
    for day, event_type, tok, cost, mu_json, amu_json, row_model in rows:
        if event_type in _measure_facade._REPRICE_SKIP_EVENT_TYPES:
            continue  # non-input-rate class; keep the event-time price verbatim
        try:
            tok = int(tok or 0)
        except (TypeError, ValueError):
            continue
        if tok <= 0:
            # Zero-token markers (nudges) and debit rows carry no rate to correct.
            continue
        mix = _measure_facade._session_mix_class_terms(
            _measure_facade._EVENT_RATE_CLASS.get(event_type, "fresh_input"), mu_json, amu_json)
        if mix is None and _measure_facade._stored_model_is_unpriced(row_model):
            # No session match, but the row itself names a model with no rate
            # card: its dollars are priced at zero and the tokens disclosed.
            key = (day, event_type) if by_day else event_type
            if cost:
                deltas[key] = deltas.get(key, 0.0) - float(cost)
            if unpriced_out is not None:
                unpriced_out["tokens"] = unpriced_out.get("tokens", 0) + tok
            continue
        if mix is None:
            unmeasured_tokens += tok
            continue
        terms, unpriced_share = mix
        # The priced share is charged at each model's own card; the unpriced share
        # is charged nothing. The delta moves the stored cost to that total.
        new_cost = sum(sh * rate for sh, rate in terms) * tok / 1_000_000.0
        delta = new_cost - float(cost or 0.0)
        if unpriced_out is not None and unpriced_share > 0:
            unpriced_out["tokens"] = unpriced_out.get("tokens", 0) + tok * unpriced_share
        if delta:
            key = (day, event_type) if by_day else event_type
            deltas[key] = deltas.get(key, 0.0) + delta
        repriced_tokens += tok
    return deltas, repriced_tokens, unmeasured_tokens


def _model_mix_shares(days=None, since=None):
    """Return {model: share_fraction} of total tokens over the window, + total.

    Reads model_daily (per-day per-model token totals). Shares sum to ~1.0.
    Returns {"shares": {...}, "total_tokens": int, "days": days} or empty shares
    when there is no data. Never raises.

    When *since* is a non-empty ISO-8601 timestamp, its DATE part is used as the
    cutoff (``date >= since_date``) instead of the rolling ``now - days``. This
    aligns the routing mix to an external boundary (the subscription limit
    reset) so the routing dollar figure scopes to the same window as the context
    figure. model_daily is day-granular, so alignment is to the reset DAY (the
    finest this table supports); sub-day precision is not available here.
    """
    _measure_facade = _compat.facade()
    out = {"shares": {}, "total_tokens": 0, "days": days}
    try:
        if not _measure_facade.TRENDS_DB.exists():
            return out
        if since:
            cutoff = str(since)[:10]  # date part of the ISO timestamp
        else:
            cutoff = (_measure_facade.datetime.now() - _measure_facade.timedelta(days=days)).strftime("%Y-%m-%d")
        conn = _measure_facade._init_trends_db()
        try:
            out["activity_coverage"] = _measure_facade.activity_coverage(conn)
            rows = conn.execute(
                "SELECT model, COALESCE(SUM(total_tokens),0) FROM model_daily "
                "WHERE date >= ? GROUP BY model",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
        total = sum(int(r[1] or 0) for r in rows)
        if total <= 0:
            return out
        out["shares"] = {r[0]: (int(r[1] or 0) / total) for r in rows if r[0]}
        out["total_tokens"] = total
    except (_measure_facade.sqlite3.Error, OSError):
        pass
    return out


def _default_model_for_runtime():
    """Representative model id to price a session against when the era's model mix
    has no priced entries at all. Runtime-aware so a Codex user is never priced at
    Anthropic rates (and vice versa)."""
    _measure_facade = _compat.facade()
    return "gpt-5-codex" if _measure_facade.detect_runtime() == "codex" else "sonnet"


def _friendly_model(model):
    """Human label for a model id: 'opus' -> 'Opus', 'gpt-5.2-codex' -> 'GPT-5.2-Codex'."""
    _measure_facade = _compat.facade()
    m = _measure_facade._strip_provider_prefixes(str(model or "")) or str(model or "")
    if m in ("opus", "sonnet", "haiku"):
        return m.capitalize()
    if m.startswith("gpt-") or m.startswith("o3") or m.startswith("o4"):
        return m.upper().replace("CODEX", "Codex").replace("MINI", "mini").replace("NANO", "nano").replace("PRO", "Pro")
    return m


def _mix_label(shares):
    """Short label for a model mix: the dominant model + its share, e.g. '95% Opus'
    or '62% GPT-5.2-Codex'. Provider-agnostic display string. 'mixed' when empty."""
    _measure_facade = _compat.facade()
    priced = {m: s for m, s in (shares or {}).items() if s and s > 0}
    if not priced:
        return "mixed"
    top, share = max(priced.items(), key=lambda kv: kv[1])
    return f"{round(share * 100)}% {_measure_facade._friendly_model(top)}"


def _model_rate_per_mtok(model):
    """Conservative $/MTok proxy for a model: its input rate.

    model_daily.total_tokens mixes input/cache/output, so this UNDERSTATES true
    cost (output is up to 5x input). Using the input rate keeps routing savings
    conservative and defensible. Returns 0.0 for unpriced models.
    """
    _measure_facade = _compat.facade()
    try:
        return _measure_facade._get_model_cost(model, 1_000_000, 0)  # input-only -> $/MTok input
    except Exception:
        return 0.0


def _model_output_rate_per_mtok(model):
    """$/MTok OUTPUT rate for a model (e.g. Opus $25 vs Sonnet $5). 0.0 if unpriced."""
    _measure_facade = _compat.facade()
    try:
        return _measure_facade._get_model_cost(model, 0, 1_000_000)  # output-only -> $/MTok output
    except Exception:
        return 0.0


def _session_output_fraction(days=None):
    """Measured OUTPUT share of the FRESH-rate token mix, from the user's session_log.

    Returns SUM(output) / SUM(fresh_input + output) over the window, clamped to
    [0, 1]. The denominator deliberately EXCLUDES cache-read and cache-creation
    tokens: those are priced separately at their own (cheap) cache rates, so the
    blend here only governs the split between FRESH input and output — the two
    classes that share the full input/output rate axis. Using total input_tokens
    (which is dominated by cache-read) as the denominator collapsed this fraction
    to ~0.5% and made the blended rate effectively the input rate, understating
    routing savings ~3x. Fresh input and output are taken from _session_token_vector
    so the negative-fresh clamp is applied consistently. Grounded in the user's OWN
    data, never an invented multiplier. Returns 0.0 (input-only, conservative) when
    there is no usable history.
    """
    _measure_facade = _compat.facade()
    try:
        if not _measure_facade.TRENDS_DB.exists():
            return 0.0
        cutoff = (_measure_facade.datetime.now() - _measure_facade.timedelta(days=days)).strftime("%Y-%m-%d")
        conn = _measure_facade._init_trends_db()
        _measure_facade.ensure_activity_view(conn)
        try:
            # Quality filter removed (audit 2026-08-29): this is a
            # volume-weighted metric (SUM(output)/SUM(fresh+output)), so
            # gating by count is irrelevant. Short sessions with real output
            # tokens must be included.
            rows = conn.execute(
                "SELECT input_tokens, output_tokens, cache_create_5m_tokens, "
                "cache_create_1h_tokens, cache_hit_rate FROM session_activity "
                "WHERE input_tokens IS NOT NULL AND date >= ? "
                "AND COALESCE(is_sidechain, 0) = 0",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
        fresh_total = 0.0
        out_total = 0.0
        for r in rows:
            fi, _cw, _cr, out = _measure_facade._session_token_vector(r)
            fresh_total += fi
            out_total += out
        denom = fresh_total + out_total
        if denom <= 0:
            return 0.0
        return max(0.0, min(1.0, out_total / denom))
    except (_measure_facade.sqlite3.Error, OSError):
        return 0.0


def _foreign_savings_card_exact(model):
    """Accept rate-card IDs and dated snapshots, without arbitrary suffixes."""
    _measure_facade = _compat.facade()
    value = _measure_facade.re.sub(r"[\s_]+", "-", _measure_facade._strip_provider_prefixes(model))
    base = _measure_facade.re.sub(r"-\d{4}-\d{2}-\d{2}$", "", value)
    if base == "gpt-5.6":
        return True
    return base in _measure_facade.OPENAI_MODEL_PRICING or base in _measure_facade.GEMINI_MODEL_PRICING


def _routing_card_exact(model):
    """True when `model` resolves to an exact rate card.

    A Claude id names a generation (claude-opus-5-5, claude-sonnet-4-6) and is
    exact only when that generation's own card exists. An id whose generation has
    no card (claude-opus-99, claude-opus-5-99) would otherwise fall back to a
    broader card, such as the Opus 5 card or the bare family rate; that is a
    stand-in, not a known price. Sonnet 3, 3.5 and 3.7 ids are the verified
    aliases of the legacy Sonnet card. A bare family label ("opus",
    "sonnet_legacy") with no generation is priced at its family card.
    """
    _measure_facade = _compat.facade()
    if not isinstance(model, str) or not model:
        return False
    if _measure_facade._normalize_openai_model_name(model) or _measure_facade._normalize_gemini_model_name(model):
        return _measure_facade._foreign_savings_card_exact(model)
    try:
        cards = dict(_measure_facade.PRICING_TIERS["anthropic"]["claude_models"])
        cards.update(_measure_facade.PRICING_TIERS.get(_measure_facade._load_pricing_tier(), {}).get("claude_models", {}))
    except Exception:
        return False
    low = model.lower()
    legacy = _measure_facade._CLAUDE_LEGACY_ID_RE.search(low)
    if legacy:
        major, minor, family = legacy.groups()
        if major == "3" and family == "sonnet" and minor in (None, "5", "7"):
            return "sonnet_legacy" in cards
        family_major = f"{family}_{major}"
    else:
        generation = _measure_facade._CLAUDE_GENERATION_RE.search(low)
        if not generation:
            key = _measure_facade._claude_price_key(model, cards)
            return bool(key and key in cards)
        family, major, minor = generation.groups()
        family = "fable" if family == "mythos" else family
        family_major, minor = f"{family}_{major}", minor
        return (family_major + (f"_{minor}" if minor else "")) in cards
    key = family_major + (f"_{minor}" if minor else "")
    return key in cards


def _routing_model_status(model):
    """"priced" (exact rate card), "free" (a provider's explicitly free tier,
    id suffix ":free") or "unpriced" (no rate card, price unknown)."""
    _measure_facade = _compat.facade()
    if _measure_facade._routing_card_exact(model):
        return "priced"
    if isinstance(model, str) and model.lower().endswith(":free"):
        return "free"
    return "unpriced"


def _routing_class_rates(model):
    """$/MTok per token class for one model id, from its own generation card.

    Probes _get_model_cost so every provider and generation card
    (Opus 5.5 $4/$20, Opus 4.1 $15/$75, Sonnet 5.5 cache reads $0.10, ...)
    prices exactly as the rest of the product does. Returns None unless the id has
    an exact rate card (see _routing_model_status): an unknown model is not free
    and a missing generation is not its family's price, so callers leave such a
    model out of a comparison instead of pricing it at zero or at a stand-in rate.
    """
    # Family pseudo-ids from the model-daily mix; a bare "sonnet_legacy" would
    # otherwise resolve to the current Sonnet card.
    _measure_facade = _compat.facade()
    probe = "claude-sonnet-4-6" if model == "sonnet_legacy" else model
    if not _measure_facade._routing_card_exact(probe):
        return None
    try:
        m = 1
        rates = {
            "fresh_input": _measure_facade._get_model_cost(probe, m, 0),
            "output": _measure_facade._get_model_cost(probe, 0, m),
            "cache_read": _measure_facade._get_model_cost(probe, 0, 0, m),
            "cache_create_5m": _measure_facade._get_model_cost(probe, 0, 0, 0, m, cache_create_1h=0, cache_create_5m=m),
            "cache_create_1h": _measure_facade._get_model_cost(probe, 0, 0, 0, m, cache_create_1h=m, cache_create_5m=0),
        }
        rates = {key: value * 1_000_000 for key, value in rates.items()}
    except Exception:
        return None
    if not any(v > 0 for v in rates.values()):
        return None
    # Rows without a class split are priced at the input rate (conservative).
    rates["unsplit"] = rates["fresh_input"]
    return rates


def _newest_generation_model(family):
    """Model id of the newest generation card of a Claude family in the active
    rate table, or None. Legacy Sonnet cards are not generation cards."""
    _measure_facade = _compat.facade()
    try:
        tier = _measure_facade._load_pricing_tier()
        cards = _measure_facade.PRICING_TIERS.get(tier, _measure_facade.PRICING_TIERS["anthropic"])["claude_models"]
        best = None
        for key in cards:
            m = _measure_facade._GENERATION_KEY_RE.match(key)
            if m and m.group(1) == family:
                gen = (int(m.group(2)), int(m.group(3) or 0))
                if best is None or gen > best[0]:
                    best = (gen, key)
        if best:
            major, minor = best[0]
            return f"claude-{family}-{major}-{minor}" if best[1].count("_") == 2 else f"claude-{family}-{major}"
    except Exception:
        pass
    return None


def _routing_target_model():
    """Model id of the lighter arm routing is priced against: the newest Sonnet
    generation card in the active rate table."""
    _measure_facade = _compat.facade()
    return _measure_facade._newest_generation_model("sonnet") or "sonnet"


def _routing_reference_model(family):
    """The model id whose card values a family in the standardized routing
    comparison, in both arms: the newest generation card of its tier (legacy
    Sonnet is valued as Sonnet). Other labels (provider models) stand for
    themselves."""
    _measure_facade = _compat.facade()
    if family == "sonnet_legacy":
        family = "sonnet"
    return _measure_facade._newest_generation_model(family) or family


def _bucket_int(value):
    _measure_facade = _compat.facade()
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _add_routing_buckets(dst, parts):
    """Add one per-model usage bucket ({fresh_input, cache_read, cache_create,
    cache_create_1h, cache_create_5m, output}) into class totals. Cache writes
    without a TTL split are priced at the 5-minute rate, as _get_model_cost does."""
    _measure_facade = _compat.facade()
    cc = _measure_facade._bucket_int(parts.get("cache_create"))
    h1 = _measure_facade._bucket_int(parts.get("cache_create_1h"))
    m5 = _measure_facade._bucket_int(parts.get("cache_create_5m"))
    m5 += max(0, cc - h1 - m5)
    dst["fresh_input"] = dst.get("fresh_input", 0) + _measure_facade._bucket_int(parts.get("fresh_input"))
    dst["cache_read"] = dst.get("cache_read", 0) + _measure_facade._bucket_int(parts.get("cache_read"))
    dst["output"] = dst.get("output", 0) + _measure_facade._bucket_int(parts.get("output"))
    dst["cache_create_1h"] = dst.get("cache_create_1h", 0) + h1
    dst["cache_create_5m"] = dst.get("cache_create_5m", 0) + m5


def _routing_pending_sessions(days=None, since=None):
    """Window sessions whose usage collect has not finished re-reading.

    A row counts when its transcript is still on disk and either its per-day
    usage is missing (the daily-usage backfill rebuilds it) or, for a Claude
    Code transcript under the projects folder that collect walks, it was parsed
    by an older usage parser (collect re-parses it). A row whose transcript is
    gone keeps its stored numbers and never counts. Pending is scoped to the
    requested window: a record whose KNOWN last activity day predates the
    cutoff holds no in-window work, while a record with no dated evidence at
    all stays conservatively pending (review A6). Never raises; 0 on error."""
    _measure_facade = _compat.facade()
    try:
        if not _measure_facade.TRENDS_DB.exists():
            return 0
        cutoff = str(since)[:10] if since else (_measure_facade.datetime.now() - _measure_facade.timedelta(days=days)).strftime("%Y-%m-%d")
        conn = _measure_facade._init_trends_db()
        try:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(session_log)").fetchall()}
            has_version = "usage_parser_version" in columns
            last_activity = (
                "COALESCE(NULLIF(last_activity_day, ''), NULLIF(date, ''))"
                if "last_activity_day" in columns else "NULLIF(date, '')")
            rows = conn.execute(
                "SELECT jsonl_path, daily_usage_json, "
                + ("usage_parser_version" if has_version else "NULL") + "," + last_activity +
                " FROM session_log WHERE jsonl_path LIKE '%.jsonl'",
                ()).fetchall()
        finally:
            conn.close()
        projects = _measure_facade.os.path.normcase(_measure_facade.os.path.abspath(str(_measure_facade.CLAUDE_DIR / "projects"))) + _measure_facade.os.sep
        walked = not _measure_facade._use_codex_session_adapter()
        pending = 0
        for path, du_raw, version, last_day in rows:
            daily_missing = du_raw is None
            stale = (has_version and walked
                     and (version is None or int(version) < _measure_facade._USAGE_PARSER_VERSION)
                     and _measure_facade.os.path.normcase(_measure_facade.os.path.abspath(path)).startswith(projects))
            if not (daily_missing or stale):
                continue
            # Last KNOWN activity day: the repaired bounds first, then the
            # retained daily-usage keys for rows whose bounds were never
            # stamped. Unknown coverage stays pending (conservative).
            last_activity_day = str(last_day or "")[:10] or None
            if last_activity_day is None and du_raw:
                usage_days = _measure_facade._activity_days(_measure_facade._safe_json_dict(du_raw))
                if usage_days:
                    last_activity_day = usage_days[-1]
            if last_activity_day is not None and last_activity_day < cutoff:
                continue
            if _measure_facade.os.path.exists(path):
                pending += 1
        return pending
    except (_measure_facade.sqlite3.Error, OSError, TypeError, ValueError):
        return 0


def _routing_usage_by_model(days=None, since=None):
    """Token usage per model id and token class over the window.

    Returns {model_id: {class: tokens}} (classes in _ROUTING_CLASSES). Source per
    session row, best first:
      1. daily_usage_json: per-day, per-model class buckets (parent + subagents),
         cut to the window by day.
      2. model_usage_breakdown_json: per-model class buckets for the session; any
         tokens in all_model_usage_json beyond those buckets (subagent work) go
         to "unsplit".
      3. all_model_usage_json / model_usage_json: token totals with no class
         split, all "unsplit".
    Never raises; empty when there is no data.
    """
    _measure_facade = _compat.facade()
    out = {}
    try:
        if not _measure_facade.TRENDS_DB.exists():
            return out
        cutoff = str(since)[:10] if since else (_measure_facade.datetime.now() - _measure_facade.timedelta(days=days)).strftime("%Y-%m-%d")
        conn = _measure_facade._init_trends_db()
        _measure_facade.ensure_activity_view(conn)
        try:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(session_log)")}
            first = "first_activity_day" if "first_activity_day" in columns else "NULL"
            last = "last_activity_day" if "last_activity_day" in columns else "NULL"
            rows = conn.execute(
                "SELECT date,daily_usage_json,model_usage_breakdown_json,all_model_usage_json,model_usage_json,"
                + first + "," + last + " FROM session_log").fetchall()
        finally:
            conn.close()
        for row_date, du_raw, mb_raw, amu_raw, mu_raw, first_day, last_day in rows:
            du = _measure_facade._safe_json_dict(du_raw)
            if du:
                undated_day = ""
                for day, models in du.items():
                    day = undated_day if day == _measure_facade.UNDATED_USAGE_KEY else day
                    if not isinstance(models, dict) or str(day) < cutoff:
                        continue
                    for model, parts in models.items():
                        if isinstance(parts, dict) and _measure_facade._normalize_model_name(model):
                            _measure_facade._add_routing_buckets(out.setdefault(model, {}), parts)
                continue
            if not first_day or first_day != last_day or first_day < cutoff:
                continue
            totals = _measure_facade._safe_json_dict(amu_raw) or _measure_facade._safe_json_dict(mu_raw)
            mb = _measure_facade._safe_json_dict(mb_raw)
            for model, parts in mb.items():
                if isinstance(parts, dict) and _measure_facade._normalize_model_name(model):
                    dst = out.setdefault(model, {})
                    before = sum(dst.get(k, 0) for k in _measure_facade._ROUTING_BILLABLE_CLASSES)
                    _measure_facade._add_routing_buckets(dst, parts)
                    split = sum(dst.get(k, 0) for k in _measure_facade._ROUTING_BILLABLE_CLASSES) - before
                    extra = _measure_facade._bucket_int(totals.get(model)) - split
                    if extra > 0:
                        dst["unsplit"] = dst.get("unsplit", 0) + extra
            for model, tokens in totals.items():
                if model not in mb and _measure_facade._normalize_model_name(model):
                    dst = out.setdefault(model, {})
                    dst["unsplit"] = dst.get("unsplit", 0) + _measure_facade._bucket_int(tokens)
    except (_measure_facade.sqlite3.Error, OSError):
        return {}
    return out


def _earliest_model_mix(lookback_days=None):
    """Measured install-era model mix from the earliest `lookback_days` of model_daily.

    Fallback baseline when no snapshot model_mix was captured (e.g. existing
    users whose snapshot predates this feature). Measured from the user's own
    history, so realized routing savings are grounded, not self-reported.
    Returns {"shares": {...}, "first_date": str} or None.
    """
    _measure_facade = _compat.facade()
    try:
        if not _measure_facade.TRENDS_DB.exists():
            return None
        conn = _measure_facade._init_trends_db()
        try:
            first = conn.execute("SELECT MIN(date) FROM model_daily").fetchone()
            if not first or not first[0]:
                return None
            first_date = first[0]
            end = (_measure_facade.datetime.fromisoformat(first_date) + _measure_facade.timedelta(days=lookback_days)).strftime("%Y-%m-%d")
            rows = conn.execute(
                "SELECT model, COALESCE(SUM(total_tokens),0) FROM model_daily "
                "WHERE date >= ? AND date <= ? GROUP BY model",
                (first_date, end),
            ).fetchall()
        finally:
            conn.close()
        total = sum(int(r[1] or 0) for r in rows)
        if total <= 0:
            return None
        return {"shares": {r[0]: int(r[1] or 0) / total for r in rows if r[0]}, "first_date": first_date}
    except (_measure_facade.sqlite3.Error, OSError, ValueError):
        return None


def _pretool_baseline_mix():
    """This user's OWN explicit pre-TO model mix, or None. PER-USER, not a default.

    The normal baseline is each user's earliest measured history (see
    _earliest_model_mix). This override exists only for a user whose history does
    not reach back to their pre-Token-Optimizer era, so they can record their own
    true starting point. It is read per-install from a `pretool_baseline.json` in
    THIS user's SNAPSHOT_DIR (`{"opus_share": <their value>}`), or their
    TOKEN_OPTIMIZER_BASELINE_OPUS_SHARE env. There is no shared/default value
    (unset -> fall back to their own history). Remaining share goes to Sonnet.
    Never raises.
    """
    _measure_facade = _compat.facade()
    share = 0.0
    try:
        bpath = _measure_facade.SNAPSHOT_DIR / "pretool_baseline.json"
        if bpath.exists():
            share = float(_measure_facade.json.loads(bpath.read_text(encoding="utf-8")).get("opus_share", 0.0) or 0.0)
    except (_measure_facade.json.JSONDecodeError, OSError, ValueError, TypeError):
        share = 0.0
    if share <= 0.0:
        share = _measure_facade._BASELINE_OPUS_SHARE
    if not (0.0 < share <= 1.0):
        return None
    return {"opus": share, "sonnet": round(1.0 - share, 4)}


def set_pretool_baseline(args):
    """Record the user's pre-Token-Optimizer Opus share (CLI: `set-baseline --opus 0.95`).

    Writes pretool_baseline.json to SNAPSHOT_DIR. Use this when your earliest captured
    history is already optimized, so the "Your transformation" before/after reflects
    your true pre-TO era. Without it, the transformation falls back to your measured
    earliest sessions. Never raises fatally.
    """
    _measure_facade = _compat.facade()
    opus = None
    for i, a in enumerate(args):
        if a == "--opus" and i + 1 < len(args):
            try:
                opus = float(args[i + 1])
            except ValueError:
                opus = None
    if opus is None or not (0.0 < opus <= 1.0):
        print("  Usage: set-baseline --opus <0..1>   (e.g. --opus 0.95 for 95% Opus pre-TO)")
        try:
            bp = _measure_facade.SNAPSHOT_DIR / "pretool_baseline.json"
            if bp.exists():
                cur = _measure_facade.json.loads(bp.read_text(encoding="utf-8"))
                print(f"  Current baseline: {float(cur.get('opus_share', 0)) * 100:.0f}% Opus")
        except (_measure_facade.json.JSONDecodeError, OSError, ValueError, TypeError):
            pass
        return
    try:
        _measure_facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        bp = _measure_facade.SNAPSHOT_DIR / "pretool_baseline.json"
        bp.write_text(_measure_facade.json.dumps({"opus_share": round(opus, 4)}) + "\n", encoding="utf-8")
        print(f"  [Token Optimizer] Pre-TO baseline set: {opus * 100:.0f}% Opus.")
        print("  'Your transformation' now prices the before-era at this mix.")
        print(f"  Saved to {_measure_facade._display_path(bp)}")
    except OSError as e:
        print(f"  [Error] Could not write baseline: {e}")


def _compute_model_routing_savings(days=None, since=None):
    """Model-routing savings: realized (mix shift vs baseline) + potential (remaining Opus).

    REALIZED: compare current model mix against the install-era baseline (snapshot
    model_mix, else the earliest model_daily window). If the user moved tokens off
    expensive models, the counterfactual cost (current volume spread over the
    baseline mix) minus the current cost, both on one standardized price
    schedule, is the estimated saving. This is the pillar that can show a
    realized win (e.g. Opus 94% -> 67%).
    Models priced above the baseline's costliest model (a premium model the user
    chose) are left out of the comparison and the rest renormalized, so they do not
    cancel a real shift toward lighter models; `premium_share` and
    `premium_cost_usd` report them separately.

    POTENTIAL: an assumption, not a measurement. A fixed share
    (`routable_fraction`) of Opus fresh input and output is priced at the
    Opus-to-Sonnet API rate delta. Opus usage without a class split is left out
    and counted in `potential_unsplit_excluded_tokens`. Cache reads and
    writes represent main-session history: switching models rebuilds that cache,
    while routine subagent work carries its own smaller context. The result is
    shown separately from concrete opportunities.

    Realized routing prices fresh input, cache creation, cache reads and output
    by class. Usage without a class split stays outside both comparisons and
    their coverage, and is reported as `unsplit_tokens`. Potential uses the model's
    own generation card for fresh input and output, and the newest Sonnet card
    for its target.
    The class split comes from per-day, per-model session usage.

    Realized routing is a standardized family comparison: the remaining work's
    class volumes per current family, and the same class volumes spread over the
    baseline family shares, are valued on one fixed schedule (the newest card of
    each family's tier) chosen independently of the window's volumes. A
    generation change inside a family therefore cannot create routing credit.
    The exact-card cost of the same work and its gap to the standardized value
    are reported separately (`actual_cost_usd`, `generation_price_delta_usd`).
    The schedule is assumed because the frozen baseline records no generations,
    so the result is labelled "estimated".

    Models with no exact rate card are unpriced, not free (an unknown Claude
    generation does not borrow its family's price): they are left out of the
    comparison and reported as `unpriced_models`, `unpriced_tokens` and
    `unpriced_share`. Explicitly free tiers (":free") are left out the same way
    and reported as `free_models`.
    Labelled estimated (realized) and opportunity (potential). Never raises.
    """
    # `zero` is returned only on no-data / error paths (no current mix, no
    # baseline, or an exception). Label it "unavailable" so a $0 here reads as
    # "couldn't compute" rather than a measured zero -- the success return below
    # sets its own evidence="estimated" when comparable work remains.
    _measure_facade = _compat.facade()
    zero = {
        "realized_cost_usd": 0.0, "potential_cost_usd": 0.0,
        "potential_tokens": 0, "potential_tokens_monthly": 0,
        "potential_billable_tokens": 0, "potential_by_class": {}, "unsplit_tokens": 0,
        "potential_unsplit_excluded_tokens": 0,
        "premium_models": [], "premium_share": 0.0, "premium_cost_usd": 0.0,
        "unpriced_models": [], "unpriced_tokens": 0, "unpriced_share": 0.0,
        "free_models": [], "free_tokens": 0,
        "compared_share": 0.0,
        "baseline_rate_basis": {}, "baseline_reference_priced": [],
        "standardized_baseline_cost_usd": 0.0, "standardized_actual_cost_usd": 0.0,
        "actual_cost_usd": 0.0, "generation_price_delta_usd": 0.0,
        "baseline_opus_share": 0.0, "current_opus_share": 0.0,
        "baseline_source": None, "evidence": "unavailable",
    }
    try:
        current = _measure_facade._model_mix_shares(days=days, since=since)
        zero["activity_coverage"] = current.get("activity_coverage", {"complete": True, "undated_sessions": 0})
        cur_shares = current.get("shares", {})
        cur_total = int(current.get("total_tokens", 0) or 0)
        if not cur_shares or cur_total <= 0:
            return zero

        # Baseline mix: explicit pre-TO baseline (user knows it) first, then
        # snapshot capture, else earliest measured history.
        baseline_shares = None
        baseline_source = None
        explicit = _measure_facade._pretool_baseline_mix()
        if explicit:
            baseline_shares = explicit
            baseline_source = "pretool_baseline"
        if not baseline_shares:
            try:
                snap_path = _measure_facade.SNAPSHOT_DIR / "snapshot_before.json"
                if snap_path.exists():
                    snap = _measure_facade.json.loads(snap_path.read_text(encoding="utf-8"))
                    mm = ((snap.get("model_mix") or {}).get("shares") or {}) if (snap.get("model_mix") or {}).get("activity_basis") == 1 else {}
                    if mm:
                        baseline_shares = mm
                        baseline_source = "snapshot"
            except (_measure_facade.json.JSONDecodeError, OSError, ValueError):
                pass
        if not baseline_shares:
            early = _measure_facade._earliest_model_mix()
            if early:
                baseline_shares = early["shares"]
                baseline_source = "history"
        if not baseline_shares:
            return zero

        # While collect is still re-reading window sessions (an older usage
        # parser, or per-day usage not yet rebuilt), their per-class usage is
        # incomplete and the figure would move pass to pass. Report the count
        # instead of a number.
        pending = _measure_facade._routing_pending_sessions(days=days, since=since)
        if pending:
            return {**zero, "evidence": "pending", "pending_sessions": pending,
                    "baseline_source": baseline_source}

        # Rows of per-model, per-class usage. When the DB holds none (adapters
        # that only record model totals), the model-daily mix stands in as
        # unsplit tokens, reported outside comparison coverage.
        usage = _measure_facade._routing_usage_by_model(days=days, since=since)
        from_rows = bool(usage)
        if not from_rows:
            usage = {m: {"unsplit": share * cur_total} for m, share in cur_shares.items()}

        # Mix keys from the model-daily fallback are already family buckets.
        fam_of = {m: (_measure_facade._normalize_model_name(m) if from_rows else m) for m in usage}
        rates_cache = {}

        def _rates(model):
            _measure_facade = _compat.facade()
            if model not in rates_cache:
                rates_cache[model] = _measure_facade._routing_class_rates(model)
            return rates_cache[model]

        # Only a model with an exact rate card is compared. A model with no card
        # is unpriced (not free) and an explicitly free tier is not credited as a
        # saving: both keep their token counts, are reported, and stay out of
        # every mix and cost below.
        priced_usage = {}
        unpriced_by_model = {}
        free_by_model = {}
        unsplit_tokens = 0.0
        potential_unsplit_excluded = 0.0
        for m, classes in usage.items():
            if not fam_of[m]:
                continue
            if _rates(m) is not None:
                unsplit_tokens += classes.get("unsplit", 0)
                if fam_of[m] == "opus":
                    potential_unsplit_excluded += classes.get("unsplit", 0)
                split = {k: v for k, v in classes.items() if k != "unsplit"}
                if sum(split.values()) > 0:
                    priced_usage[m] = split
                continue
            tokens = sum(classes.get(k, 0) for k in _measure_facade._ROUTING_CLASSES)
            bucket = free_by_model if _measure_facade._routing_model_status(m) == "free" else unpriced_by_model
            bucket[m] = bucket.get(m, 0) + tokens

        fam_models = {}
        for m in priced_usage:
            fam_models.setdefault(fam_of[m], []).append(m)
        fam_classes = {}
        class_totals = {k: 0.0 for k in _measure_facade._ROUTING_CLASSES}
        for fam, ms in fam_models.items():
            dst = fam_classes[fam] = {k: 0.0 for k in _measure_facade._ROUTING_CLASSES}
            for m in ms:
                for k in _measure_facade._ROUTING_CLASSES:
                    dst[k] += priced_usage[m].get(k, 0)
                    class_totals[k] += priced_usage[m].get(k, 0)
        pooled = sum(class_totals.values())

        def _unpriced_report():
            _measure_facade = _compat.facade()
            return [mid for mid, _t in sorted(unpriced_by_model.items(), key=lambda kv: -kv[1])]

        def _free_report():
            _measure_facade = _compat.facade()
            return [mid for mid, _t in sorted(free_by_model.items(), key=lambda kv: -kv[1])]

        unpriced_tokens = int(sum(unpriced_by_model.values()))
        free_tokens = int(sum(free_by_model.values()))

        def _coverage_fields():
            _measure_facade = _compat.facade()
            total = unpriced_tokens + free_tokens + pooled + unsplit_tokens
            return {
                "unpriced_models": _unpriced_report(), "unpriced_tokens": unpriced_tokens,
                "unpriced_share": round(unpriced_tokens / total, 4) if total > 0 else 0.0,
                "free_models": _free_report(), "free_tokens": free_tokens,
                "unsplit_tokens": int(round(unsplit_tokens)),
                "potential_unsplit_excluded_tokens": int(round(potential_unsplit_excluded)),
                "baseline_source": baseline_source,
            }

        if pooled <= 0:
            return {**zero, **_coverage_fields()}

        # STANDARDIZED ROUTING VALUATION. Realized routing measures a shift of work
        # between model families, so both arms are valued on one fixed price
        # schedule: one rate per family and token class, chosen without looking at
        # how much of that family ran in the window. The frozen baseline records
        # shares by family only (no generation), so the schedule is a documented
        # reference-card scenario: each family at the newest generation card of
        # its tier (legacy Sonnet counts as Sonnet; a provider model stands for
        # itself). It is the same for present and absent families, so one token of
        # any generation changes the result only by that token's own contribution,
        # and a generation change within a family cannot become routing credit.
        # What the work actually cost at the exact cards used is reported
        # separately (`actual_cost_usd`, `generation_price_delta_usd`). Because the
        # scenario is assumed, the result is labelled estimated. A baseline that
        # recorded generations would replace the reference card in
        # `_fixed_family_rates` for the families it covers.
        fam_rate_cache = {}
        fam_rate_basis = {}

        def _fixed_family_rates(fam):
            _measure_facade = _compat.facade()
            if fam in fam_rate_cache:
                return fam_rate_cache[fam]
            ref_model = _measure_facade._routing_reference_model(fam)
            out = _rates(ref_model)
            if out is None and fam_models.get(fam):
                # No reference card for a family that did run: its lowest-sorted
                # model id stands in, so the choice never depends on input order.
                ref_model = sorted(fam_models[fam])[0]
                out = _rates(ref_model)
            if out is not None:
                fam_rate_basis[fam] = f"reference:{ref_model}"
            fam_rate_cache[fam] = out
            return out

        def _renormalized(shares):
            _measure_facade = _compat.facade()
            kept = {f: s for f, s in shares.items() if s and s > 0 and _fixed_family_rates(f) is not None}
            total = sum(kept.values())
            return {f: s / total for f, s in kept.items()} if total > 0 else {}

        base_priced = _renormalized(baseline_shares)

        # Effective $/MTok of a family under the window's own class mix.
        def _family_price(f):
            _measure_facade = _compat.facade()
            if pooled <= 0:
                return 0.0
            r = _fixed_family_rates(f)
            return sum(class_totals[k] * r[k] for k in _measure_facade._ROUTING_CLASSES) / pooled

        # Models priced above the most expensive model of the baseline are the
        # user's own choice, not routing: they are taken out of the comparison
        # so it measures the shift toward lighter models. Their share and cost
        # are reported separately. A model must be dearer than the baseline's
        # costliest family on the window's pooled mix AND on the output card:
        # cache-read rates alone do not rank tiers across generations (Sonnet 4.6
        # reads cache at $0.30, Opus 5.5 at $0.20), so a lighter older model is
        # never mistaken for a premium one.
        ceiling_fam = max(base_priced, key=_family_price, default=None)
        baseline_ceiling = _family_price(ceiling_fam) if ceiling_fam else 0.0
        ceiling_output = _fixed_family_rates(ceiling_fam)["output"] if ceiling_fam else 0.0
        premium = sorted(
            f for f in fam_classes
            if _family_price(f) > baseline_ceiling > 0 and _fixed_family_rates(f)["output"] > ceiling_output
        )
        premium_share = sum(cur_shares.get(f, 0.0) for f in premium)
        premium_cost = sum(
            classes.get(k, 0) * _rates(m)[k]
            for m, classes in priced_usage.items() if fam_of[m] in premium
            for k in _measure_facade._ROUTING_CLASSES
        ) / 1_000_000

        # REALIZED: the remaining work's class volumes, per current family, valued
        # on the standardized schedule, against the same total class volumes spread
        # over the baseline family shares on that same schedule. Only the shift
        # between families moves the number. Never negative. Premium and unpriced
        # models are outside both arms.
        kept_models = [m for m in priced_usage if fam_of[m] not in premium]
        kept_totals = {k: sum(priced_usage[m].get(k, 0) for m in kept_models) for k in _measure_facade._ROUTING_CLASSES}
        standardized_actual = sum(
            priced_usage[m].get(k, 0) * _fixed_family_rates(fam_of[m])[k]
            for m in kept_models for k in _measure_facade._ROUTING_CLASSES
        ) / 1_000_000
        standardized_baseline = sum(
            kept_totals[k] * sum(s * _fixed_family_rates(f)[k] for f, s in base_priced.items())
            for k in _measure_facade._ROUTING_CLASSES
        ) / 1_000_000 if base_priced else 0.0
        # Exact-card API-equivalent cost of the same work (each model's own card).
        actual_cost = sum(
            priced_usage[m].get(k, 0) * _rates(m)[k] for m in kept_models for k in _measure_facade._ROUTING_CLASSES
        ) / 1_000_000
        realized_cost = max(0.0, standardized_baseline - standardized_actual) if base_priced and kept_models else 0.0
        compared_tokens = sum(kept_totals.values())
        window_tokens = unpriced_tokens + free_tokens + pooled + unsplit_tokens

        # POTENTIAL: a fixed share of remaining Opus fresh input and output, priced
        # at each Opus model's own card minus the current Sonnet card. It is
        # computed apart from the realized figure: with no target card the
        # potential is zero and the realized routing above is unaffected. Usage
        # without a class split cannot be told apart as input or output, so it is
        # left out and its count is reported.
        target = _measure_facade._routing_target_model()
        target_rates = _rates(target)
        by_class = {k: {"tokens": 0, "usd": 0.0} for k in _measure_facade._ROUTING_CLASSES}
        for m, classes in priced_usage.items():
            if fam_of[m] != "opus":
                continue
            if target_rates is None:
                continue
            m_rates = _rates(m)
            for k in ("fresh_input", "output"):
                tok = classes.get(k, 0)
                if tok:
                    by_class[k]["tokens"] += tok * _measure_facade._ROUTABLE_OPUS_FRACTION
                    by_class[k]["usd"] += tok * _measure_facade._ROUTABLE_OPUS_FRACTION * (m_rates[k] - target_rates[k]) / 1_000_000
        potential_cost = max(0.0, sum(v["usd"] for v in by_class.values()))
        potential_tokens = int(round(sum(v["tokens"] for v in by_class.values())))
        potential_tokens_monthly = int(round(potential_tokens / max(days, 1) * 30))
        potential_billable_tokens = int(round(sum(by_class[k]["tokens"] for k in _measure_facade._ROUTING_BILLABLE_CLASSES)))
        opus_share = cur_shares.get("opus", 0.0)

        return {
            "activity_coverage": zero["activity_coverage"],
            "realized_cost_usd": round(realized_cost, 4),
            "potential_cost_usd": round(potential_cost, 4),
            # Routable tokens cover observed fresh input and output only.
            "potential_tokens": potential_tokens,
            "potential_tokens_monthly": potential_tokens_monthly,
            "potential_billable_tokens": potential_billable_tokens,
            "potential_token_classes": [k for k in _measure_facade._ROUTING_CLASSES if by_class[k]["tokens"]],
            "potential_by_class": {
                k: {"tokens": int(round(v["tokens"])), "usd": round(v["usd"], 4)}
                for k, v in by_class.items() if v["tokens"] or v["usd"]
            },
            "potential_basis": "api_rate_equivalent",
            "target_model": target,
            "pricing_basis": "per_token_class_generation_card",
            # Premium models (priced above the baseline's costliest model) are the
            # user's own choice: excluded from realized routing, shown here.
            "premium_models": premium,
            "premium_share": round(premium_share, 4),
            "premium_cost_usd": round(premium_cost, 4),
            # Models with no exact rate card (unpriced) and explicitly free tiers are
            # left out of the comparison, never priced at zero or at a stand-in rate.
            **_coverage_fields(),
            "baseline_opus_share": round(baseline_shares.get("opus", 0.0), 4),
            "current_opus_share": round(opus_share, 4),
            "baseline_source": baseline_source,
            "routable_fraction": _measure_facade._ROUTABLE_OPUS_FRACTION,
            # Realized routing is a standardized comparison on a fixed price
            # schedule (newest card per family) in both arms, because the baseline
            # records no generations. That schedule is an assumption, so the
            # result is an estimate. The exact-card cost of the same work and its
            # gap to the standardized valuation are reported apart: the gap is
            # price/generation, never routing.
            "realized_basis": "standardized_reference_card",
            "baseline_rate_basis": {f: fam_rate_basis[f] for f in sorted(set(base_priced) | set(fam_classes)) if f in fam_rate_basis},
            "baseline_reference_priced": sorted(base_priced),
            "standardized_baseline_cost_usd": round(standardized_baseline, 4),
            "standardized_actual_cost_usd": round(standardized_actual, 4),
            "actual_cost_usd": round(actual_cost, 4),
            "generation_price_delta_usd": round(actual_cost - standardized_actual, 4),
            # Share of the window's class tokens inside both arms (premium,
            # unpriced and free models, and unsplit tokens are outside).
            "compared_share": round(compared_tokens / window_tokens, 4) if window_tokens > 0 else 0.0,
            "evidence": "estimated" if compared_tokens > 0 and base_priced else "unavailable",
        }
    except Exception:
        return zero
