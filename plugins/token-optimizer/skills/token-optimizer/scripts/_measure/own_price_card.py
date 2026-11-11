"""Price the work-normalized transformation at the user's own current prices.

The transformation compares cost per unit of work in a baseline window with
the same figure now, both priced on ONE card so a vendor price change or a
model switch cannot count as a saving. That card used to be a fixed Opus 4.x
card for every user, which put a Sonnet, Haiku or other-provider user's result
in dollars two to five times their own. Here the card is the user's own: for
each token class, what they actually paid per million tokens this period,
averaged over the models they ran.

A baseline pinned before per-class token sums were kept cannot be re-priced
until those sums are rebuilt from its transcripts; until then the fixed card
stays in use and is named as such on the page.
"""

CLASSES = ("input", "output", "cache_read", "cache_write")
_ZERO = {"sessions": 0, "usd": 0.0, "tokens": 0.0, "flat_usd": 0.0, "api_calls": 0, "messages": 0}
# A rebuilt baseline may differ this much from the pinned one and still lend it
# its class split; beyond that the transcripts are no longer the same window.
_REBUILD_TOLERANCE = 0.02


def price_rows(facade, rows, tier, api_calls_unknown=None):
    """Sum parent rows: engine-priced USD, fixed-card USD, class tokens, work units."""
    if not rows:
        return None
    default_model = facade._default_model_for_runtime()
    fr = facade._WEIGHT_POOL_FLAT_RATES
    usd = flat_usd = tokens = 0.0
    api_calls = messages = 0
    classes = dict.fromkeys(CLASSES, 0.0)
    for r in rows:
        fi, cw, cr, out = facade._session_token_vector(r[:5])
        tokens += fi + cw + cr + out
        for key, value in zip(CLASSES, (fi, out, cr, cw)):
            classes[key] += value
        flat_usd += (fi * fr["input"] + out * fr["output"]
                     + cr * fr["cache_read"] + cw * fr["cache_write"]) / 1_000_000
        api_calls += int(r[7] or 0)
        messages += int(r[8] or 0)
        usage = facade._safe_json_dict(r[5]) or facade._safe_json_dict(r[6]) or {}
        grand = sum(int(v or 0) for v in usage.values() if v)
        if len(r) > 9 and facade._safe_json_dict(r[9]):
            cost = facade._cost_from_model_breakdown(facade._safe_json_dict(r[9]), tier=tier)
        elif grand > 0:
            # Share denominator is the session TOTAL, so an unpriced (non-Claude, local)
            # model is proxy-priced at the runtime default rather than letting a small
            # priced sliver stand for the whole session. Same rule as _cost_per_session.
            cost = 0.0
            for model, toks in usage.items():
                share = int(toks or 0) / grand
                if share <= 0:
                    continue
                cost += share * facade._get_model_cost(
                    model if facade._is_priced_model(model, tier) else default_model,
                    int(fi), int(out), int(cr), int(cw), tier=tier)
        else:
            cost = facade._get_model_cost(default_model, int(fi), int(out), int(cr), int(cw), tier=tier)
        usd += cost
    return {"sessions": len(rows), "usd": usd, "tokens": tokens,
            "flat_usd": flat_usd, "api_calls": api_calls, "messages": messages,
            "classes": classes,
            # Sessions whose dated per-day call evidence was never recorded.
            # api_calls is then a lower bound, not a measured total.
            "api_calls_unknown": int(api_calls_unknown or 0)}


def classes_of(metrics):
    """The arm's class token sums, or None when it was priced before they were kept."""
    try:
        classes = metrics.get("classes")
        if not isinstance(classes, dict):
            return None
        out = {k: float(classes[k]) for k in CLASSES}
        return out if all(v == v and v >= 0 for v in out.values()) and sum(out.values()) > 0 else None
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def lend_classes(frozen, fresh):
    """Give a frozen month its class split from the same month re-read from the
    ledger, when the two still agree. True when `frozen` was changed."""
    try:
        if classes_of(frozen) or not fresh:
            return False
        split = classes_of(fresh)
        old, new = float(frozen["flat_usd"]), float(fresh["flat_usd"])
        if not split or not (old > 0 and new > 0) or abs(new - old) > _REBUILD_TOLERANCE * old:
            return False
        frozen["classes"] = {k: split[k] * old / new for k in CLASSES}
        return True
    except (KeyError, TypeError, ValueError):
        return False


def card(facade, days):
    """{class: $/MTok} the user paid this period, or None with no priced usage.

    Each class rate is the token-weighted average of the exact cards of the
    models that ran. Cache writes blend the 5-minute and 1-hour rates as used.
    A class with no tokens takes the average card of the models that ran.
    """
    try:
        usage = facade._routing_usage_by_model(days=days) or {}
        spent = dict.fromkeys(CLASSES, 0.0)
        used = dict.fromkeys(CLASSES, 0.0)
        weights = []
        for model, parts in usage.items():
            rates = facade._routing_class_rates(model)
            if not rates:
                continue  # unpriced or free: its price is not known, so it sets none
            split = {"input": [("fresh_input", "fresh_input")], "output": [("output", "output")],
                     "cache_read": [("cache_read", "cache_read")],
                     "cache_write": [("cache_create_5m", "cache_create_5m"), ("cache_create_1h", "cache_create_1h")]}
            total = 0.0
            for name, pairs in split.items():
                for bucket, rate in pairs:
                    tokens = float(parts.get(bucket) or 0)
                    if tokens > 0:
                        spent[name] += tokens * rates[rate]
                        used[name] += tokens
                        total += tokens
            if total > 0:
                weights.append((total, {"input": rates["fresh_input"], "output": rates["output"],
                                        "cache_read": rates["cache_read"], "cache_write": rates["cache_create_5m"]}))
        whole = sum(w for w, _ in weights)
        if whole <= 0:
            return None
        out = {}
        for name in CLASSES:
            rate = spent[name] / used[name] if used[name] > 0 else sum(w * r[name] for w, r in weights) / whole
            if not (rate == rate and rate > 0):
                return None
            out[name] = round(rate, 6)
        return out
    except Exception:
        return None


def apply(facade, before, now, days):
    """Both arms on the user's own card: {"basis", "rates", "before_usd", "now_usd"}.

    None when either arm lacks class sums or no priced usage exists; the caller
    then keeps the fixed card.
    """
    b, n = classes_of(before or {}), classes_of(now or {})
    if not b or not n:
        return None
    rates = card(facade, days)
    if not rates:
        return None

    def value(classes):
        return sum(classes[k] * rates[k] for k in CLASSES) / 1_000_000

    before_usd, now_usd = value(b), value(n)
    if not (before_usd > 0 and now_usd > 0):
        return None
    return {"basis": "own_prices", "rates": rates, "before_usd": before_usd, "now_usd": now_usd}


def _blank():
    return dict(_ZERO, classes=dict.fromkeys(CLASSES, 0.0))


def _walk(facade, progress, start, end, budget_seconds, tier):
    """Price a few more transcripts into `progress`. True when every file is done."""
    files = sorted(str(f) for f, _mtime, _proj in facade._find_all_jsonl_files(None))
    seen = set(progress.get("seen") or [])
    metrics = progress["metrics"]
    metrics.setdefault("classes", dict.fromkeys(CLASSES, 0.0))
    deadline = facade.time.monotonic() + budget_seconds
    cursor = int(progress.get("cursor") or 0)
    while cursor < len(files) and facade.time.monotonic() < deadline:
        fp = files[cursor]
        cursor += 1
        uuid = facade._canonical_session_uuid(fp) or fp
        if uuid in seen:
            continue  # another copy of a session already priced
        seen.add(uuid)
        row = facade._pretool_session_row(fp, start, end, progress.setdefault("requests", {}))
        priced = facade._price_parent_rows([row], tier) if row else None
        if priced:
            for key in _ZERO:
                metrics[key] += priced[key]
            for key in CLASSES:
                metrics["classes"][key] += priced["classes"][key]
    progress["cursor"], progress["seen"] = cursor, sorted(seen)
    return cursor >= len(files)


def _usable(facade, metrics):
    return (metrics["sessions"] >= facade._SESSION_WEIGHT_MIN_ANCHOR_SESSIONS
            and metrics["api_calls"] > 0 and metrics["flat_usd"] > 0)


def _fill_classes(facade, state, budget_seconds, tier):
    """Rebuild the class split of an already pinned baseline, without unpinning it.

    The pinned figures stay as they are. The rebuilt split is adopted only when
    the transcripts still reproduce the pinned total; otherwise the baseline
    keeps the fixed card for good.
    """
    window = state.get("window")
    if not (isinstance(window, list) and len(window) == 2):
        state["classes_status"] = "unavailable"
        return
    fill = state.get("class_fill")
    if not isinstance(fill, dict) or not isinstance(fill.get("metrics"), dict):
        fill = {"cursor": 0, "seen": [], "metrics": _blank()}
    if not _walk(facade, fill, window[0], window[1], budget_seconds, tier):
        state["class_fill"] = fill
        return
    state.pop("class_fill", None)
    old, new = float(state["metrics"]["flat_usd"]), float(fill["metrics"]["flat_usd"])
    if old > 0 and new > 0 and abs(new - old) <= _REBUILD_TOLERANCE * old:
        scale = old / new
        state["metrics"]["classes"] = {k: fill["metrics"]["classes"][k] * scale for k in CLASSES}
        state["classes_filled_at"] = facade.datetime.now().isoformat()
    else:
        state["classes_status"] = "unavailable"


def anchor_step(facade, budget_seconds=None, tier=None):
    """Build the pinned pre-install anchor a few transcripts at a time.

    The ledger keeps a rolling window, so the months before install age out of
    it; their transcripts usually remain on disk. Each call prices up to
    `budget_seconds` of them with the same ruler as the live pool and banks
    running sums, then pins the result once the window is done. A window with
    too few surviving transcripts is marked unavailable, so the check stops.
    An anchor pinned before class sums were kept gets them rebuilt the same way.
    Returns the state string. Never raises.
    """
    path = facade.SNAPSHOT_DIR / facade._PRETOOL_ANCHOR_FILE
    try:
        state = facade.json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(state, dict) or state.get("activity_basis") != 1:
            # State written before activity dating (or surviving an archive
            # collision) was computed on session-date windows; rebuild it.
            state = {}
        if state.get("status") == "unavailable":
            return "unavailable"
        if tier is None:
            tier = facade._load_pricing_tier()
        if state.get("status") == "pinned":
            if classes_of(state.get("metrics") or {}) or state.get("classes_status") == "unavailable":
                return "pinned"
            _fill_classes(facade, state, budget_seconds, tier)
            facade._write_baseline_state(path, state)
            return "pinned"
        window = facade._pretool_window()
        if not window:
            return "no_baseline"
        start, end = window
        if state.get("window") != [start, end] or state.get("rates") != facade._WEIGHT_POOL_FLAT_RATES:
            state = {"status": "building", "window": [start, end], "rates": facade._WEIGHT_POOL_FLAT_RATES,
                     "activity_basis": 1, "cursor": 0, "seen": [], "metrics": _blank()}
        if _walk(facade, state, start, end, budget_seconds, tier):
            m = state["metrics"]
            state = {"status": "pinned" if _usable(facade, m) else "unavailable", "window": [start, end],
                     "rates": facade._WEIGHT_POOL_FLAT_RATES, "activity_basis": 1, "metrics": m,
                     "label": f"your earliest tracked month ({start} to {end})",
                     "pinned_at": facade.datetime.now().isoformat()}
        facade._write_baseline_state(path, state)
        return state["status"]
    except Exception:
        return "error"
