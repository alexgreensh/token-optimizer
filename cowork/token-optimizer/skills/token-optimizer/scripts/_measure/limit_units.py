"""Limit-unit accounting: one currency for savings and plan limits (SPEC-limit-units).

Prices every token class in input-equivalent limit units using the exact model card
ratios (R9), never a hard-coded 0.1 read factor. Unknown models stay unpriced (R10).
Also handles expand_round_trip debits, per-feature signed net, and transcript
row deduplication / row-timestamp windowing.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

LIMIT_UNITS_LABEL = "share of window, modeled at API-rate weights"


def _resolve_card(facade, model, tier=None, per_request=False, full_input=0):
    """Return the exact rate card dict for *model*, or None if unknown (R9/R10)."""
    if not model:
        return None
    if tier is None:
        tier = facade._load_pricing_tier()
    tier_data = facade.PRICING_TIERS.get(tier, facade.PRICING_TIERS["anthropic"])
    for norm_fn, pricing, lc_pricing, lc_threshold in (
        (facade._normalize_openai_model_name, facade.OPENAI_MODEL_PRICING,
         facade.OPENAI_LONG_CONTEXT_PRICING, facade.OPENAI_LONG_CONTEXT_INPUT_THRESHOLD),
        (facade._normalize_gemini_model_name, facade.GEMINI_MODEL_PRICING,
         facade.GEMINI_LONG_CONTEXT_PRICING, facade.GEMINI_LONG_CONTEXT_INPUT_THRESHOLD),
    ):
        key = norm_fn(model)
        if key:
            rates = pricing[key]
            if per_request and full_input > lc_threshold and key in lc_pricing:
                rates = lc_pricing[key]
            return rates
    normalized = facade._claude_price_key(model, tier_data["claude_models"])
    if normalized and normalized in tier_data["claude_models"]:
        rates = tier_data["claude_models"][normalized]
        lc_models = tier_data.get("claude_models_lc") or {}
    else:
        rates = facade.PRICING_TIERS["anthropic"]["claude_models"].get(normalized or "")
        lc_models = facade.PRICING_TIERS["anthropic"].get("claude_models_lc") or {}
        if rates is None:
            return None
    if (per_request and normalized in lc_models
            and full_input > facade.ANTHROPIC_LONG_CONTEXT_INPUT_THRESHOLD):
        rates = lc_models[normalized]
    return rates


def model_card_ratios(facade, model, tier=None, per_request=False, full_input=0):
    """Return input-normalized class multipliers from the exact model card, or None."""
    rates = _resolve_card(facade, model, tier=tier, per_request=per_request, full_input=full_input)
    if not rates:
        return None
    inp = float(rates.get("input") or 0.0)
    if inp <= 0.0:
        return None
    cw5 = float(rates.get("cache_write", inp * 1.25))
    cw1 = float(rates.get("cache_write_1h", inp * 2.0))
    cr = float(rates.get("cache_read", 0.0))
    out = float(rates.get("output", 0.0))
    return {
        "input": 1.0,
        "cache_write_5m": cw5 / inp,
        "cache_write_1h": cw1 / inp,
        "cache_read": cr / inp,
        "output": out / inp,
        "input_rate_per_m": inp,
        "cache_read_rate_per_m": cr,
    }


def limit_units(row, facade=None, tier=None, per_request=False):
    """Compute input-equivalent limit units for a usage or savings row.

    Formula:
      input*1 + cache_write_5m*1.25 + cache_write_1h*2
      + cache_read*(card read ratio) + output*(card output ratio)
    where 5m/1h write ratios default to 1.25 / 2.0 (or card ratios when set)
    and read/output ratios always come from the exact model card (R9).
    Unknown model cards return priced=False, units=0.0, unpriced_tokens>0 (R10).
    """
    if facade is None:
        from . import _compat
        facade = _compat.facade()
    r = row or {}
    model = r.get("model") or ""
    inp = int(r.get("input", r.get("fresh_input", r.get("input_tokens", 0))) or 0)
    out = int(r.get("output", r.get("output_tokens", 0)) or 0)
    cr = int(r.get("cache_read", r.get("cache_read_input_tokens", 0)) or 0)
    cw1 = r.get("cache_write_1h", r.get("cache_create_1h"))
    cw5 = r.get("cache_write_5m", r.get("cache_create_5m"))
    cw_total = int(r.get("cache_write", r.get("cache_create", r.get("cache_creation_input_tokens", 0))) or 0)
    if cw1 is not None or cw5 is not None:
        w1 = int(cw1 or 0)
        w5 = int(cw5 or 0)
        unsplit = max(0, cw_total - w1 - w5)
        w5 += unsplit
    else:
        w1 = 0
        w5 = cw_total
    full_input = inp + cr + w5 + w1
    ratios = model_card_ratios(facade, model, tier=tier, per_request=per_request, full_input=full_input)
    if ratios is None:
        return {
            "units": 0.0,
            "priced": False,
            "unpriced_tokens": abs(inp) + abs(out) + abs(cr) + abs(w5) + abs(w1),
            "model": model,
            "by_class": {"input": 0.0, "cache_write_5m": 0.0, "cache_write_1h": 0.0,
                         "cache_read": 0.0, "output": 0.0},
        }
    by_class = {
        "input": float(inp) * 1.0,
        "cache_write_5m": float(w5) * 1.25,
        "cache_write_1h": float(w1) * 2.0,
        "cache_read": float(cr) * ratios["cache_read"],
        "output": float(out) * ratios["output"],
    }
    return {
        "units": sum(by_class.values()),
        "priced": True,
        "unpriced_tokens": 0,
        "model": model,
        "by_class": by_class,
        "ratios": ratios,
    }


def _find_next_request_cache_read(facade, session_id, expand_ts=None):
    """Find the next transcript request's (cache_read_tokens, model) after expand_ts."""
    if not session_id or not facade.TRENDS_DB.exists():
        return None
    try:
        conn = facade._init_trends_db()
        try:
            row = conn.execute(
                "SELECT jsonl_path FROM session_log WHERE session_id = ? AND jsonl_path IS NOT NULL LIMIT 1",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    path = Path(row[0])
    if not path.is_file():
        return None
    seen_ids = set()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                ts = obj.get("timestamp") or ""
                if expand_ts and ts and ts <= expand_ts:
                    continue
                msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else obj.get("usage")
                if not isinstance(usage, dict):
                    continue
                mid = msg.get("id") or obj.get("requestId")
                if mid:
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                cr = int(usage.get("cache_read_input_tokens") or usage.get("cache_read") or 0)
                model = msg.get("model") or obj.get("model") or ""
                if cr > 0 and model:
                    return (cr, model)
    except OSError:
        return None
    return None


def log_expand_round_trip(facade, session_id, tool_use_id, feature="tool_archive",
                          expand_ts=None, next_request=None):
    """Debit the next request's cache-read round trip when an expand occurs.

    Logged as event_type='expand_round_trip' (Measured, bookkeeping only).
    If no next request is on record, debits nothing and logs a 0-token marker
    detail='round_trip_unknown' so summaries can report round_trip_unknown.
    """
    try:
        req = next_request if next_request is not None else _find_next_request_cache_read(
            facade, session_id, expand_ts=expand_ts)
        if not req:
            facade._log_savings_event(
                "expand_round_trip", 0, session_id=session_id,
                detail=f"{tool_use_id} ({feature}) round_trip_unknown",
            )
            return {"logged": False, "round_trip_unknown": True, "tokens": 0, "cost_usd": 0.0}
        cr_tokens, model = int(req[0] or 0), str(req[1] or "")
        if cr_tokens <= 0:
            return {"logged": False, "round_trip_unknown": True, "tokens": 0, "cost_usd": 0.0}
        ratios = model_card_ratios(facade, model)
        cost_usd = 0.0
        if ratios is not None:
            cost_usd = cr_tokens * ratios["cache_read_rate_per_m"] / 1e6
        detail = f"{tool_use_id} ({feature}) model={model}"
        facade._log_savings_event(
            "expand_round_trip", cr_tokens, session_id=session_id,
            detail=detail, cost_saved_usd=round(cost_usd, 6),
        )
        return {"logged": True, "round_trip_unknown": False, "tokens": cr_tokens,
                "cost_usd": round(cost_usd, 6), "model": model}
    except Exception:
        return {"logged": False, "round_trip_unknown": True, "tokens": 0, "cost_usd": 0.0}


def apply_round_trip_and_feature_net(summary, counted=None, labels=None, conn=None, cutoff=None):
    """Pop expand_round_trip bookkeeping from by_category and attach per-feature net.

    net = first_write + repeat_reads - reexpand - round_trip (signed, no floor at 0).
    """
    if not isinstance(summary, dict):
        return summary
    by_cat = summary.get("by_category")
    if not isinstance(by_cat, dict):
        by_cat = {}
        summary["by_category"] = by_cat
    labels = labels or {}

    rt_entry = by_cat.pop("expand_round_trip", None)
    rt_unknown = 0
    rt_by_feature = {}
    if conn is not None and cutoff is not None:
        try:
            rows = conn.execute(
                "SELECT detail, tokens_saved, cost_saved_usd FROM savings_events "
                "WHERE event_type = 'expand_round_trip' AND timestamp >= ?",
                (cutoff,),
            ).fetchall()
            for detail, tok, cost in rows:
                d = str(detail or "")
                if "round_trip_unknown" in d or int(tok or 0) == 0:
                    rt_unknown += 1
                    continue
                feat = "tool_archive"
                if "(" in d and ")" in d:
                    feat = d.split("(", 1)[1].split(")", 1)[0].strip() or "tool_archive"
                slot = rt_by_feature.setdefault(feat, [0, 0.0])
                slot[0] += int(tok or 0)
                slot[1] += float(cost or 0.0)
        except Exception:
            pass
    elif rt_entry:
        rt_tok = int(rt_entry.get("tokens_saved") or 0)
        rt_cost = float(rt_entry.get("cost_saved_usd") or 0.0)
        if rt_tok > 0:
            rt_by_feature["tool_archive"] = [rt_tok, rt_cost]
        else:
            rt_unknown += int(rt_entry.get("events") or 0)

    if rt_entry:
        rt_tok = int(rt_entry.get("tokens_saved") or 0)
        rt_cost = float(rt_entry.get("cost_saved_usd") or 0.0)
        rt_ev = int(rt_entry.get("events") or 0)
        summary["total_tokens"] = int(summary.get("total_tokens") or 0) - 2 * rt_tok
        summary["total_cost_usd"] = round(float(summary.get("total_cost_usd") or 0.0) - 2 * rt_cost, 4)
        summary["total_events"] = max(0, int(summary.get("total_events") or 0) - rt_ev)

    reread_by_cat = {}
    if isinstance(counted, dict):
        for k, v in (counted.get("by_category") or {}).items():
            if isinstance(v, dict):
                reread_by_cat[k] = (
                    int(v.get("reread_tokens") or 0),
                    float(v.get("reread_usd") or 0.0),
                )

    feature_net = {}
    all_keys = list(by_cat.keys()) + [k for k in rt_by_feature if k not in by_cat]
    for feat in all_keys:
        entry = by_cat.get(feat) or {}
        fw_tok = int(entry.get("tokens_saved") or 0)
        fw_usd = float(entry.get("cost_saved_usd") or 0.0)
        rr_tok, rr_usd = reread_by_cat.get(feat, (0, 0.0))
        rt_tok, rt_usd = rt_by_feature.get(feat, (0, 0.0))
        if rt_tok and feat in by_cat:
            entry["tokens_saved"] = fw_tok - rt_tok
            entry["cost_saved_usd"] = round(fw_usd - rt_usd, 4)
        net_tok = fw_tok + rr_tok - rt_tok
        net_usd = round(fw_usd + rr_usd - rt_usd, 4)
        feature_net[feat] = {
            "label": labels.get(feat, feat.replace("_", " ").title()),
            "first_write_tokens": fw_tok,
            "repeat_read_tokens": rr_tok,
            "round_trip_tokens": rt_tok,
            "net_tokens": net_tok,
            "first_write_usd": round(fw_usd, 4),
            "repeat_read_usd": round(rr_usd, 4),
            "round_trip_usd": round(rt_usd, 4),
            "net_usd": net_usd,
        }
    summary["feature_net"] = feature_net
    summary["round_trip_unknown"] = rt_unknown
    summary["expand_round_trip_tokens"] = sum(v[0] for v in rt_by_feature.values())
    summary["expand_round_trip_usd"] = round(sum(v[1] for v in rt_by_feature.values()), 4)
    return summary


def plan_limits_summary(facade, conn, days, summary=None, counted=None, default_model=None):
    """Compute plan-limits share in limit units using limit_units() for both sides.

    Denominator: all top-level requests in the window in limit_units().
    Numerator: measured savings (first write + repeat reads - re-expand - round trip)
    converted through the same limit_units() function.
    Keeps old fresh+output denominator in diagnostic_fresh_plus_output_tokens.
    """
    cutoff = (facade.datetime.now() - facade.timedelta(days=days)).date().isoformat()
    today = facade.datetime.now().date().isoformat()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(session_log)")}
    parent = "parent_daily_usage_json" if "parent_daily_usage_json" in columns else "daily_usage_json"
    side = "COALESCE(is_sidechain,0)=0" if "is_sidechain" in columns else "1=1"

    denom_units = 0.0
    unpriced_tokens = 0
    diag_fresh_out = 0
    by_class = {"input": 0.0, "cache_write_5m": 0.0, "cache_write_1h": 0.0,
                "cache_read": 0.0, "output": 0.0}
    model_weights = {}

    for (raw,) in conn.execute(f"SELECT {parent} FROM session_log WHERE {side}"):
        try:
            daily = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(daily, dict):
            continue
        for day, models in daily.items():
            if not (isinstance(day, str) and cutoff <= day <= today and isinstance(models, dict)):
                continue
            for model, usage in models.items():
                if not isinstance(usage, dict):
                    continue
                diag_fresh_out += int(usage.get("fresh_input") or 0) + int(usage.get("output") or 0)
                lu = limit_units({"model": model, **usage}, facade=facade)
                if lu["priced"]:
                    denom_units += lu["units"]
                    model_weights[model] = model_weights.get(model, 0.0) + lu["units"]
                    for k in by_class:
                        by_class[k] += lu["by_class"][k]
                else:
                    unpriced_tokens += lu["unpriced_tokens"]

    # Pick dominant priced model in the window for converting aggregate saved tokens
    ref_model = default_model
    if not ref_model and model_weights:
        ref_model = max(model_weights.items(), key=lambda kv: kv[1])[0]

    saved_units = 0.0
    if summary and ref_model:
        fw_tok = int(summary.get("total_tokens") or 0)
        rr_tok = int((counted or {}).get("reread_tokens") or 0)
        # First-write removals save cache_write (5m conservative) or input;
        # repeat reads save cache_read. Both go through limit_units().
        saved_row = {
            "model": ref_model,
            "input": 0,
            "cache_write_5m": fw_tok,
            "cache_read": rr_tok,
            "output": 0,
        }
        saved_lu = limit_units(saved_row, facade=facade)
        if saved_lu["priced"]:
            saved_units = saved_lu["units"]

    share_pct = round(saved_units / denom_units * 100.0, 4) if denom_units > 0 else None
    from . import cold_guard
    guard = (summary or {}).get(cold_guard.ESTIMATE_KEY)
    if guard is None:
        guard = cold_guard.estimate(facade, days=days)
    estimated = {}
    units = guard.get("limit_units_saved")
    if guard.get("events") and units is not None:
        estimated[cold_guard.ESTIMATE_KEY] = {
            "label": guard["label"], "saved_units": units,
            "share_pct": round(units / denom_units * 100.0, 4) if denom_units > 0 else None,
        }
    return {
        "label": LIMIT_UNITS_LABEL,
        "share_pct": share_pct,
        "saved_units": round(saved_units, 2),
        "denominator_units": round(denom_units, 2),
        "unpriced_tokens": unpriced_tokens,
        "diagnostic_fresh_plus_output_tokens": diag_fresh_out,
        "by_class": {k: round(v, 2) for k, v in by_class.items()},
        "reference_model": ref_model,
        "estimated_by_feature": estimated,
    }


def dedupe_transcript_rows(rows, cutoff_iso=None, end_iso=None):
    """Deduplicate assistant rows by message id and tool results by tool_use_id
    across files, filtering strictly by each row's own timestamp (R6).
    """
    seen_msg_ids = set()
    seen_tool_ids = set()
    kept = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = row.get("timestamp") or ""
        if cutoff_iso and (not ts or ts < cutoff_iso):
            continue
        if end_iso and ts and ts > end_iso:
            continue
        msg = row.get("message") if isinstance(row.get("message"), dict) else {}
        role = msg.get("role") or row.get("type") or ""
        if role == "assistant" or msg.get("id") or row.get("requestId"):
            mid = msg.get("id") or row.get("requestId")
            if mid:
                if mid in seen_msg_ids:
                    continue
                seen_msg_ids.add(mid)
            kept.append(row)
            continue
        # Tool result rows: dedupe by tool_use_id across files
        content = msg.get("content") if isinstance(msg.get("content"), list) else []
        tids = [b.get("tool_use_id") for b in content
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id")]
        if not tids and row.get("tool_use_id"):
            tids = [row["tool_use_id"]]
        if tids:
            if all(t in seen_tool_ids for t in tids):
                continue
            seen_tool_ids.update(tids)
        kept.append(row)
    return kept
