"""Structural savings payload, opportunity pricing and the `structural` CLI.

_compute_structural_savings and _compound_structural (savings.py) are thin
callers of compute() and price_synthetic() here.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from . import _compat, structural_ledger as ledger, structural_listing as listing, structural_value as value, structural_dedupe as dedupe

NO_TRIMS = "no trims recorded yet"


def _local_sessions(conn, home):
    out = []
    cloud = value.cloud_sessions(conn)
    winners, _ = dedupe.canonical_sessions(conn, lambda uuid, path: value.proven_foreign(uuid, path, cloud))
    for row in winners:
        if value.scope_matches("global", row["project"], home) and not row["foreign"]:
            out.append((value._session_start(row["metrics"], row["first_day"] or row["date"]), row["path"]))
    return out


def _open_db(f):
    if not f.TRENDS_DB.exists():
        return None
    conn = f._init_trends_db()
    return conn


def _sum(rows, key):
    return sum(r.get(key, 0) or 0 for r in rows)


def compute(days=30, now=None, f=None, home=None, trims=None, projection_days=30):
    """Realized structural savings from the trim ledger, plus the forward
    30-day run-rate of the trims still holding. Never raises."""
    f = f or _compat.facade()
    days = int(days or 30)
    zero = {"events": 0, "tokens_saved": 0, "cost_saved_usd": 0.0, "baseline_source": None,
            "baseline_date": None, "overhead_delta": 0, "read_savings_usd": 0.0,
            "write_1h_savings_usd": 0.0, "write_5m_savings_usd": 0.0, "evidence": "measured",
            "method": "trim_ledger", "status": "no_trims", "message": NO_TRIMS, "trims": [],
            "estimated": {"tokens_saved": 0, "cost_saved_usd": 0.0, "trims": 0},
            "unattributed": {"tokens_saved": 0, "cost_saved_usd": 0.0, "trims": 0},
            "projected_30d": {"measured": {"tokens": 0, "usd": 0.0}, "estimated": {"tokens": 0, "usd": 0.0}}}
    try:
        if trims is None:
            from token_estimate import estimate_tokens
            from . import structural_archives
            trims = ledger.load_trims(f.SNAPSHOT_DIR)
            trims += structural_archives.discover(f.CLAUDE_DIR, trims, estimate_tokens)
        if not trims and not f.TRENDS_DB.exists():
            return zero
        conn = _open_db(f)
        if conn is None:
            return {**zero, "status": "no_data", "message": "no usage data yet"}
        home = str(home or f.HOME)
        try:
            listing.apply_listing_check(trims, _local_sessions(conn, home), f.CHARS_PER_TOKEN, f.SNAPSHOT_DIR)
            for t in trims:
                if t.get("listing_check") == "pending" and t["tier"] == "measured":
                    t["tier"] = "estimated"  # not yet verified against what was sent
                if t.get("origin") == "dated_archive" and t.get("listing_check") == "measured":
                    t["tier"] = "measured"
            start, end = value.window(days, now)
            card = value.card_resolver(f)
            from . import setup_budget_monitor, setup_connector_ledger
            from token_estimate import estimate_tokens
            window_start = datetime.strptime(start, "%Y-%m-%d").astimezone().isoformat()
            window_end = (now or datetime.now()).astimezone().isoformat()
            budget = setup_budget_monitor.measured_values(conn, trims, estimate_tokens, card, window_start, window_end)
            connector = setup_connector_ledger.measured_values(conn, trims, estimate_tokens, card, window_start, window_end)
            if not trims and not budget["calls"] and not connector["calls"]:
                return {**zero, "budget_observed": budget, "connector_observed": connector}
            real = value.value_trims(conn, trims, start, end, card, home)
            pstart, pend = value.window(projection_days, now)
            proj = value.value_trims(conn, trims, pstart, pend, card, home, projected=True)
            cover = value.call_coverage(conn, start)
        finally:
            conn.close()
        rows = []
        for t in trims:
            r, p = real["trims"][t["id"]], proj["trims"][t["id"]]
            rows.append({
                "id": t["id"], "component": t["component"], "item": t["item"], "scope": t["scope"],
                "source": t["source"], "tier": t["tier"], "ts": t["ts"], "closed_at": t.get("closed_at"),
                "evidence": t.get("evidence", ""), "origin": t.get("origin", ""),
                "tokens_per_call": t["tokens"], "tokens_estimated": t.get("tokens_estimated", t["tokens"]),
                "listing_check": t.get("listing_check"), "listing_factor": t.get("listing_factor"),
                "requests": r["requests"], "tokens_saved": r["read_tokens"] + r["write_tokens"],
                "read_tokens": r["read_tokens"], "write_tokens": r["write_tokens"],
                "cost_saved_usd": r["usd"], "read_usd": r["read_usd"],
                "write_1h_usd": r["write_1h_usd"], "write_5m_usd": r["write_5m_usd"],
                "projected_30d_tokens": p["read_tokens"] + p["write_tokens"],
                "projected_30d_usd": p["usd"],
            })
        tiers = {k: [r for r in rows if r["tier"] == k] for k in ("measured", "estimated", "unattributed")}
        meas = tiers["measured"]
        open_meas = [r for r in meas if not r["closed_at"]]

        def band(rs):
            return {"tokens_saved": int(_sum(rs, "tokens_saved")),
                    "cost_saved_usd": round(_sum(rs, "cost_saved_usd"), 4), "trims": len(rs)}

        return {
            "events": int(_sum(meas, "requests")) + budget["calls"] + connector["calls"],
            "tokens_saved": int(_sum(meas, "tokens_saved")) + budget["tokens_saved"] + connector["tokens_saved"],
            "cost_saved_usd": round(_sum(meas, "cost_saved_usd") + budget["usd"] + connector["usd"], 8),
            "read_savings_usd": round(_sum(meas, "read_usd") + budget["read_usd"] + connector["read_usd"], 8),
            "write_1h_savings_usd": round(_sum(meas, "write_1h_usd") + budget["write_1h_usd"] + connector["write_1h_usd"], 8),
            "write_5m_savings_usd": round(_sum(meas, "write_5m_usd") + budget["write_5m_usd"] + connector["write_5m_usd"], 8),
            "baseline_source": "trim_ledger",
            "baseline_date": min([r["ts"] for r in rows] +
                                 [c["changed_at"] for c in budget["candidates"] if c["calls"]] +
                                 [c["changed_at"] for c in connector["actions"] if c["calls"]], default=None),
            "overhead_delta": int(_sum([r for r in open_meas if r["scope"] == "global"], "tokens_per_call")),
            "evidence": "measured", "method": "trim_ledger", "status": "ok", "message": "",
            "trims": rows,
            "estimated": band(tiers["estimated"]),
            "unattributed": band(tiers["unattributed"]),
            "projected_30d": {
                k: {"tokens": int(_sum(tiers[k], "projected_30d_tokens")),
                    "usd": round(_sum(tiers[k], "projected_30d_usd"), 4)} for k in ("measured", "estimated")},
            "budget_observed": budget, "connector_observed": connector,
            "requests": real["requests"], "unmatched_requests": real["unmatched_requests"],
            "unpriced_tokens": real["unpriced_tokens"], "by_day": real["days"],
            **cover,
        }
    except Exception as exc:  # never break the savings summary
        return {**zero, "status": "error", "message": type(exc).__name__}


def price_synthetic(s, days, now=None, f=None, home=None, scope="global"):
    """Forward value of S prefix tokens over the window, on the same per-request
    pricing as realized trims. Used by the opportunity figure."""
    f = f or _compat.facade()
    if s <= 0 or not f.TRENDS_DB.exists():
        return None
    t = {"id": "synthetic", "component": "synthetic", "item": "*", "scope": scope, "tokens": int(s),
         "ts_epoch": 0.0, "closed_at": None}
    conn = _open_db(f)
    try:
        start, end = value.window(days, now)
        res = value.value_trims(conn, [t], start, end, value.card_resolver(f), str(home or f.HOME), projected=True)
        cover = value.call_coverage(conn, start)
    finally:
        conn.close()
    a = res["trims"]["synthetic"]
    return {"events": a["requests"], "tokens_saved": a["read_tokens"] + a["write_tokens"],
            "read_savings_usd": a["read_usd"], "write_1h_savings_usd": a["write_1h_usd"],
            "write_5m_savings_usd": a["write_5m_usd"], "cost_saved_usd": a["usd"], **cover}


def skill_listing_budget(days=30, now=None, f=None, home=None):
    """Still on the table: the skill listing's growth since its smallest month,
    priced on every local call in the window at that call's cache-read rate.
    {"available": False, "reason": ...} when the history cannot show it."""
    f = f or _compat.facade()
    try:
        if not f.TRENDS_DB.exists():
            return {"available": False, "reason": "no usage data yet"}
        conn = _open_db(f)
        try:
            sessions = _local_sessions(conn, str(home or f.HOME))
        finally:
            conn.close()
        end = (now or datetime.now()).timestamp()
        growth = listing.listing_growth(sessions, end, f.CHARS_PER_TOKEN)
        if growth is None:
            return {"available": False, "reason": "not enough skill listings in transcripts"}
        priced = price_synthetic(growth["growth_tokens"], days, now=now, f=f, home=home) if growth["growth_tokens"] > 0 else None
        return {"available": True, "period_days": int(days or 30), **growth,
                "calls": int((priced or {}).get("events", 0)),
                "cost_usd": round(float((priced or {}).get("cost_saved_usd", 0.0)), 4),
                "basis": "growth tokens x local calls in the window x each call's cache-read rate"}
    except Exception as exc:  # never break the savings summary
        return {"available": False, "reason": type(exc).__name__}


def attested_whatif(sizes, ts, days=30, now=None, f=None, home=None):
    """Side by side value of owner-attested historic trims, NOT recorded.
    The choice of size is the owner's; this only prices the options."""
    f = f or _compat.facade()
    out = []
    for n in sizes:
        t = ledger.make_trim("attested_setup", "historic_prefix", "global", int(n), 0, ts, "attested")
        r = compute(days=days, now=now, f=f, home=home, trims=ledger.load_trims_from([t]))
        row = (r.get("trims") or [{}])[0]
        out.append({"tokens_per_call": int(n), "window_tokens": row.get("tokens_saved", 0),
                    "window_usd": row.get("cost_saved_usd", 0.0),
                    "projected_30d_tokens": row.get("projected_30d_tokens", 0),
                    "projected_30d_usd": row.get("projected_30d_usd", 0.0),
                    "requests": row.get("requests", 0)})
    return out


def _print(obj):
    print(json.dumps(obj, indent=1, default=str))


def cli(argv=None):
    """structural.py <status|tick|attest|whatif|close|seed> ..."""
    f = _compat.facade()
    ap = argparse.ArgumentParser(prog="structural.py")
    sub = ap.add_subparsers(dest="cmd")
    st = sub.add_parser("status")
    st.add_argument("--days", type=int, default=30)
    tk = sub.add_parser("tick", help="snapshot the setup now, open/close trims")
    tk.add_argument("--force", action="store_true")
    at = sub.add_parser("attest", help="record an owner-attested historic trim (estimated tier)")
    at.add_argument("--tokens", type=int, required=True)
    at.add_argument("--date", required=True, help="ISO date/time the trim took effect")
    at.add_argument("--note", default="")
    at.add_argument("--apply", action="store_true", help="write it (default: dry run)")
    wi = sub.add_parser("whatif", help="price candidate attested sizes side by side, write nothing")
    wi.add_argument("--tokens", required=True, help="comma list, e.g. 5150,11550,15000")
    wi.add_argument("--date", required=True)
    wi.add_argument("--days", type=int, default=30)
    cl = sub.add_parser("close", help="close a trim whose item came back")
    cl.add_argument("--id", required=True)
    cl.add_argument("--reason", default="manual")
    sd = sub.add_parser("seed", help="one-time import of a past audit from its backups (dry run by default)")
    sd.add_argument("--backup-dir", required=True)
    sd.add_argument("--skills-archive", default="")
    sd.add_argument("--commands-archive", default="")
    sd.add_argument("--ts", default="", help="when the trims took effect (default: newest README mtime)")
    sd.add_argument("--apply", action="store_true")
    sd.add_argument("--revert", default="", help="void a previous seed batch id")
    oc = sub.add_parser("observed-change", help="compare two transcripts without booking savings")
    oc.add_argument("--before", required=True)
    after = oc.add_mutually_exclusive_group(required=True)
    after.add_argument("--after")
    after.add_argument("--after-manifest", help="JSON array of session_id and path")
    oc.add_argument("--changed-at", required=True)
    oc.add_argument("--component", required=True,
                    choices=("skill_listing", "deferred_tools_delta", "mcp_instructions_delta"))
    oc.add_argument("--target", action="append", default=[])
    bo = sub.add_parser("budget-observations", help="show unbooked observed budget-change candidates")
    bo.add_argument("--days", type=int, default=30)
    cd = sub.add_parser("connector-disconnected", help="record an explicitly disconnected connector")
    cd.add_argument("--project", required=True, help="project path or encoded project scope")
    cd.add_argument("--before", required=True, help="transcript containing the prior listing")
    cd.add_argument("--changed-at", required=True)
    cd.add_argument("--component", required=True, choices=("deferred_tools_delta", "mcp_instructions_delta"))
    cd.add_argument("--target", action="append", required=True)
    cd.add_argument("--apply", action="store_true", help="confirm the connector was disconnected")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        _print(compute(days=a.days, f=f))
    elif a.cmd == "tick":
        _print(ledger.session_start_tick(f, force=a.force) or {"skipped": "throttled"})
    elif a.cmd == "attest":
        t = ledger.make_trim("attested_setup", "historic_prefix", "global", a.tokens, 0,
                             a.date, "attested", origin="owner_attested", note=a.note)
        if a.apply:
            _print({"written": ledger.record_trim(f.SNAPSHOT_DIR, t), "trim": t})
        else:
            _print({"dry_run": True, "trim": t})
    elif a.cmd == "whatif":
        sizes = [int(x) for x in a.tokens.split(",") if x.strip()]
        _print(attested_whatif(sizes, a.date, days=a.days, f=f))
    elif a.cmd == "close":
        ledger.close_trim(f.SNAPSHOT_DIR, a.id, ledger.now_iso(), a.reason)
        _print({"closed": a.id})
    elif a.cmd == "seed":
        from . import structural_seed
        _print(structural_seed.run(a, f))
    elif a.cmd == "observed-change":
        from . import setup_attachment_counter
        from token_estimate import estimate_tokens
        if a.after_manifest:
            with open(a.after_manifest, encoding="utf-8") as fh:
                entries = json.load(fh)
            sessions = setup_attachment_counter.manifest_sessions(entries)
        else:
            sessions = [{"session_id": "one", "rows": setup_attachment_counter.read_transcript(a.after)}]
        r = setup_attachment_counter.measure_sessions(
            setup_attachment_counter.read_transcript(a.before), sessions, a.changed_at, a.component,
            estimate_tokens, value.card_resolver(f), targets=a.target)
        _print({"booked": False, "attribution_verified": False,
                "token_counts": "estimated from observed attachment text", "component": a.component, **r})
    elif a.cmd == "budget-observations":
        from . import setup_budget_monitor
        from token_estimate import estimate_tokens
        if not f.TRENDS_DB.is_file():
            _print({"booked": False, "attribution_verified": False, "candidates": []})
        else:
            now = datetime.now(timezone.utc)
            uri = f.TRENDS_DB.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as conn:
                _print(setup_budget_monitor.evaluate(conn, estimate_tokens, value.card_resolver(f),
                                                    (now - timedelta(days=max(1, a.days))).isoformat(), now.isoformat()))
    elif a.cmd == "connector-disconnected":
        from . import setup_connector_ledger
        project = ledger.encode_project(os.path.realpath(a.project)) if os.path.isabs(a.project) else a.project
        conn = f._init_trends_db() if a.apply else sqlite3.connect(":memory:")
        try:
            registered = setup_connector_ledger.register(conn, project, a.component, a.changed_at,
                                                          a.before, a.target, True)
        finally:
            conn.close()
        _print({"registered": registered} if a.apply else
               {"dry_run": True, "eligible": registered, "duplicate_checked": False})
    else:
        ap.print_help()
        return 2
    return 0
