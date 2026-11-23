"""Prefix cost report with approve-to-apply cuts (SPEC-prefix-cost).

Advisory ONLY: reports what each always-on prefix component costs in limit units
and weekly window share, paired with local usage counts. Never auto-applies any
change, never proposes removing an item used in the window, and never touches
project files.
"""
from __future__ import annotations

import json
from typing import Any

from . import limit_units as lu

DEFERRED_TOOL_TOKENS_PER_TOOL = 25
FULL_TOOL_TOKENS_PER_TOOL = 400
STATUS_BAR_HINT_THRESHOLD_PCT = 2.0


def _proposed_fix(comp: dict[str, Any]) -> str | None:
    """Return existing approve-to-apply command or manual step, ONLY for unused items."""
    if int(comp.get("uses") or 0) > 0:
        return None
    kind = str(comp.get("kind") or "")
    name = str(comp.get("name") or "")
    if kind == "mcp_server":
        return f"approve-to-apply: measure.py api-manage mcp/disable {name}"
    if kind == "skill":
        return f"manual: add `disable-model-invocation: true` to skill {name}"
    if kind == "rule":
        return f"manual: add `paths:` glob frontmatter to rule {name}"
    if kind == "agent":
        return f"manual: archive or disable unused agent definition {name}"
    return f"manual: trim unused prefix component {name}"


def _ledger_activity(facade, conn, days: int) -> dict[str, Any]:
    """Read per-model request counts, session starts, cold rewrites, and window limit units."""
    cutoff = (facade.datetime.now() - facade.timedelta(days=days)).date().isoformat()
    today = facade.datetime.now().date().isoformat()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(session_log)")}
    usage_col = "parent_daily_usage_json" if "parent_daily_usage_json" in columns else "daily_usage_json"
    calls_col = "daily_calls_json" if "daily_calls_json" in columns else "NULL"

    requests_by_model: dict[str, int] = {}
    writes_events_by_model: dict[str, int] = {}
    denom_units = 0.0
    unpriced_tokens = 0
    total_requests = 0

    for raw_usage, raw_calls in conn.execute(f"SELECT {usage_col}, {calls_col} FROM session_log"):
        try:
            daily = json.loads(raw_usage or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(daily, dict):
            continue
        calls_map = {}
        if raw_calls:
            try:
                parsed = json.loads(raw_calls)
                if isinstance(parsed, dict):
                    calls_map = parsed
            except (TypeError, ValueError):
                pass
        session_started = False
        for day, models in sorted(daily.items()):
            if not (isinstance(day, str) and cutoff <= day <= today and isinstance(models, dict)):
                continue
            day_calls = int(calls_map.get(day) or 0)
            model_items = [(m, u) for m, u in models.items() if isinstance(u, dict)]
            if not model_items:
                continue
            for model, usage in model_items:
                res = lu.limit_units({"model": model, **usage}, facade=facade)
                if res["priced"]:
                    denom_units += res["units"]
                else:
                    unpriced_tokens += res["unpriced_tokens"]
                n_req = max(1, int(usage.get("requests") or day_calls or 1))
                requests_by_model[model] = requests_by_model.get(model, 0) + n_req
                total_requests += n_req
                # Session start + cold rewrites (where cache_read < 50% of prompt)
                starts = 0 if session_started else 1
                session_started = True
                cr = int(usage.get("cache_read") or 0)
                cw = int(usage.get("cache_create") or usage.get("cache_create_1h") or 0)
                cold = int(usage.get("cold_rewrites") or (1 if cw > cr and cw > 0 else 0))
                writes_events_by_model[model] = writes_events_by_model.get(model, 0) + starts + cold

    return {
        "requests_by_model": requests_by_model,
        "writes_events_by_model": writes_events_by_model,
        "denominator_units": denom_units,
        "unpriced_tokens": unpriced_tokens,
        "total_requests": total_requests,
    }


def _component_tokens(comp: dict[str, Any], deferred_tools: bool) -> int:
    """Compute effective prefix tokens for a component, honoring deferred-tools setups."""
    if deferred_tools and comp.get("kind") == "mcp_server" and comp.get("deferred", True):
        if "deferred_tokens" in comp:
            return max(0, int(comp["deferred_tokens"]))
        tool_count = int(comp.get("tool_count") or 0)
        if tool_count > 0:
            return min(int(comp.get("tokens") or 0), tool_count * DEFERRED_TOOL_TOKENS_PER_TOOL)
    return max(0, int(comp.get("tokens") or 0))


def prefix_cost_report(
    facade,
    conn=None,
    *,
    days: int = 30,
    inventory: list[dict[str, Any]] | None = None,
    usage_counts: dict[str, int] | None = None,
    deferred_tools: bool = False,
) -> dict[str, Any]:
    """Build ranked advisory prefix-cost report in limit units and weekly window share."""
    close_conn = False
    if conn is None:
        conn = facade._init_trends_db()
        close_conn = True
    try:
        act = _ledger_activity(facade, conn, days)
    finally:
        if close_conn:
            conn.close()

    inv = list(inventory) if inventory is not None else []
    counts = dict(usage_counts or {})
    denom_units = act["denominator_units"]
    req_by_model = act["requests_by_model"]
    wr_by_model = act["writes_events_by_model"]

    rows: list[dict[str, Any]] = []
    total_prefix_units = 0.0
    unpriced_prefix_tokens = 0

    for item in inv:
        name = str(item.get("name") or "")
        kind = str(item.get("kind") or "other")
        tok = _component_tokens(item, deferred_tools=deferred_tools)
        uses = int(counts.get(name, item.get("uses", 0)) or 0)
        unused_here = uses == 0

        comp_units = 0.0
        comp_unpriced = 0
        for model, n_req in req_by_model.items():
            n_wr = wr_by_model.get(model, 0)
            row_lu = lu.limit_units({
                "model": model,
                "input": 0,
                "cache_read": tok * n_req,
                "cache_write_1h": tok * n_wr,
                "output": 0,
            }, facade=facade)
            if row_lu["priced"]:
                comp_units += row_lu["units"]
            else:
                comp_unpriced += row_lu["unpriced_tokens"]

        total_prefix_units += comp_units
        unpriced_prefix_tokens += comp_unpriced
        share_pct = (comp_units / denom_units * 100.0) if denom_units > 0 else 0.0
        row = {
            "name": name,
            "kind": kind,
            "tokens": tok,
            "uses": uses,
            "unused_here": unused_here,
            "limit_units": round(comp_units, 2),
            "weekly_window_share_pct": round(share_pct, 2),
            "summary_line": f"costs you {share_pct:.1f}% of a weekly window, used {uses} times",
            "proposed_fix": _proposed_fix({"name": name, "kind": kind, "uses": uses}),
        }
        rows.append(row)

    # Rank descending by limit_units, then name for determinism
    rows.sort(key=lambda r: (-r["limit_units"], r["name"]))
    top10 = rows[:10]

    unused_top3_pct = sum(r["weekly_window_share_pct"] for r in [r for r in rows if r["unused_here"]][:3])
    status_bar_hint = (
        f"Prefix audit: top unused items cost {unused_top3_pct:.1f}% of your window (run measure.py audit)"
        if unused_top3_pct > STATUS_BAR_HINT_THRESHOLD_PCT
        else None
    )

    return {
        "advisory": True,
        "auto_applied": False,
        "days": days,
        "deferred_tools": bool(deferred_tools),
        "label": lu.LIMIT_UNITS_LABEL,
        "denominator_units": round(denom_units, 2),
        "total_prefix_limit_units": round(total_prefix_units, 2),
        "unpriced_tokens": act["unpriced_tokens"] + unpriced_prefix_tokens,
        "top_10": top10,
        "all_components": rows,
        "status_bar_hint": status_bar_hint,
    }


def record_applied_prefix_cut(facade, component_name: str, tokens_before: int,
                              tokens_after: int, session_id: str | None = None) -> int:
    """Log user-approved prefix trim as setup_optimization (Estimated tier, no new class)."""
    delta = max(0, int(tokens_before) - int(tokens_after))
    if delta > 0:
        facade._log_savings_event(
            "setup_optimization", delta, session_id=session_id,
            detail=f"prefix_cut:{component_name} ({tokens_before}->{tokens_after})",
        )
    return delta
