"""Weekly pilot report: `measure.py pilot-report`.

One paste-able Slack summary a pilot user can run once a week. Plain text,
under a dozen lines, no personal paths; `--json` emits the same data. Every
figure comes from the same payloads the dashboard renders, so the report can
never quote a different dollar than the Savings page.

Cross-lane adapters: the install-anchored baseline (t2), the routing injection
markers and `routing_enabled_since()` (t3), and the `nudge_events` table (t4)
land in sibling modules in this release. Each reader probes the canonical
helper first and falls back to the on-disk contract, so this module merges
cleanly whether or not those lanes have landed yet. Every piece is guarded:
the command never raises and never blocks on missing data.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import _compat

# Sibling modules the parallel lanes may add, most likely name first.
_ROUTING_MODULES = ("routing_credit", "install_baseline", "routing_state", "routing_enabled")
_NUDGE_MODULES = ("nudge_followthrough", "nudges", "nudge_events")
_NUDGE_FUNCS = ("weekly_counts", "week_counts", "followthrough_week", "summary", "counts")
# nudge_events columns, most likely name first (t4 spec: session_uuid, kind,
# ts, context_tokens, model; the resolver adds a status-ish column).
_TS_COLS = ("ts", "timestamp", "created_at", "emitted_at")
_STATUS_COLS = ("status", "outcome", "resolution", "result", "state",
                "classification", "followup", "followed")
_FOLLOWED_VALUES = ("followed", "1", "true", "yes")


def _facade(facade):
    return facade if facade is not None else _compat.facade()


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _money(value):
    if value is None:
        return None
    try:
        return "${:,.2f}".format(float(value))
    except (TypeError, ValueError):
        return None


def _iso_day(value):
    """First YYYY-MM-DD inside a string, or None."""
    try:
        text = str(value or "")[:10]
        datetime.strptime(text, "%Y-%m-%d")
        return text
    except (TypeError, ValueError):
        return None


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _marker_date(path):
    """A date from a marker file: plain ISO text, or a JSON object with a
    date-ish value. t3 writes `routing_injected_at` / `routing_removed_at`."""
    try:
        raw = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    if raw.startswith("{"):
        data = _read_json(path) or {}
        for key in ("date", "since", "injected_at", "enabled_since", "ts", "timestamp"):
            day = _iso_day(data.get(key))
            if day:
                return day
        return None
    return _iso_day(raw)


def _sibling(module_name, func_name):
    """A function on a sibling _measure module, or None when absent."""
    try:
        mod = importlib.import_module("." + module_name, __package__)
    except Exception:
        return None
    return getattr(mod, func_name, None)


def _call_flex(fn, *argsets):
    """Call fn with the first signature that does not TypeError.

    Returns (value, answered): answered is False when every signature raised,
    so callers fall through to their own fallback instead of trusting a
    half-answer.
    """
    for args in argsets:
        try:
            return fn(*args), True
        except TypeError:
            continue
        except Exception:
            return None, False
    return None, False


# ---------------------------------------------------------------------------
# Install date
# ---------------------------------------------------------------------------

def _install(facade):
    """(date, source) of the install evidence, or (None, None)."""
    try:
        from . import baseline_provenance
        ev = baseline_provenance.install_evidence(facade) or {}
        if ev.get("date"):
            return ev.get("date"), ev.get("source")
    except Exception:
        pass
    for name in ("install_baseline",):
        for fname in ("install_date", "install_evidence", "installed_at"):
            fn = _sibling(name, fname)
            if fn is None:
                continue
            val, answered = _call_flex(fn, (facade,), (facade.SNAPSHOT_DIR,), ())
            if answered:
                day = _iso_day(val.get("date") if isinstance(val, dict) else val)
                if day:
                    return day, (val.get("source") if isinstance(val, dict) else None)
    try:
        day = facade._install_date()
        if day:
            return day, None
    except Exception:
        pass
    return None, None


# ---------------------------------------------------------------------------
# Savings payloads
# ---------------------------------------------------------------------------

def _dashboard(facade, days):
    """The dashboard savings payload for a window, or {} on any failure."""
    try:
        data = facade._dashboard_savings_data(days=days)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _capacity(before_after):
    """(+pct, basis) from a before_after payload.

    Mirrors the dashboard limits card: the counterfactual/actual cost ratio
    minus one, shown only when the transformation state is ok and the
    counterfactual actually exceeds actual. `basis` names what the "before"
    side is; a fallback window (the tool may already have been running in the
    earliest tracked month) is labeled as such instead of claiming "before".
    """
    ba = before_after if isinstance(before_after, dict) else {}
    actual = _num(ba.get("actual_monthly_usd"))
    before = _num(ba.get("counterfactual_monthly_usd"))
    if ba.get("transformation_state") != "ok" or not (actual > 0 and before > actual):
        return None, None
    pct = int(round((before / actual - 1.0) * 100))
    prov = ba.get("baseline_provenance") or {}
    if prov.get("coverage") == "pre_install":
        basis = "your 30 days before install"
    else:
        basis = "your earliest tracked month (fallback window)"
    return pct, basis


def _routing_in_total(all_in):
    """The routing dollars already inside all_in.total_usd.

    `_savings_since_install` adds the routing figure only when a baseline
    kind exists; components_usd.routing_estimated carries that exact amount.
    """
    all_in = all_in if isinstance(all_in, dict) else {}
    parts = all_in.get("components_usd") or {}
    amount = parts.get("routing_estimated")
    if amount is None:
        amount = all_in.get("routing_usd") if all_in.get("routing_baseline_source") else 0.0
    return _num(amount)


def _since_install(data, routing_on):
    """All-in saved-since-install with the measured/estimated/routing split.

    Routing credit exists only while Token Optimizer actually injected its
    routing guidance: when routing is off, the routing component is removed
    from the total instead of being reported as a saving.
    """
    si = (data or {}).get("since_install") or {}
    all_in = si.get("all_in") or {}
    out = {"saved_usd": None, "measured_usd": None,
           "estimated_usd": None, "routing_usd": None}
    if isinstance(all_in, dict) and all_in:
        routing_in = _routing_in_total(all_in)
        total = _num(all_in.get("total_usd"))
        display_total = total - routing_in
        saved = display_total + (routing_in if routing_on else 0.0)
        estimated = _num(all_in.get("estimated_usd"))
        routing = routing_in if routing_on else 0.0
        out.update(saved_usd=round(saved, 2),
                   estimated_usd=round(estimated, 2),
                   routing_usd=round(routing, 2),
                   measured_usd=round(max(0.0, saved - estimated - routing), 2))
    elif si:
        # Pre-all-in shape: measured + estimated only, no routing credit.
        out["saved_usd"] = round(_num(si.get("total_usd")), 2)
        out["measured_usd"] = round(_num(si.get("measured_usd")), 2)
        out["estimated_usd"] = round(_num(si.get("estimated_usd")), 2)
    return out


def _saved_7d(data7, routing_on):
    """All-in dollars saved in the last 7 days (the headline recipe:
    action_savings.total_usd plus realized routing when guidance is on)."""
    action = (data7 or {}).get("action_savings") or {}
    total = action.get("total_usd")
    if total is None:
        return None
    routing = _num(((data7 or {}).get("model_routing") or {}).get("realized_cost_usd"))
    return round(_num(total) + (routing if routing_on else 0.0), 2)


# ---------------------------------------------------------------------------
# Routing guidance state
# ---------------------------------------------------------------------------

def _norm_routing(val):
    """Normalize a routing_enabled_since() result to {"on", "since"}."""
    if val is None or val is False:
        return {"on": False, "since": None}
    if val is True:
        return {"on": True, "since": None}
    if isinstance(val, str):
        day = _iso_day(val)
        return {"on": bool(day), "since": day} if day else None
    if isinstance(val, dict):
        since = _iso_day(val.get("since") or val.get("enabled_since")
                         or val.get("injected_at") or val.get("date"))
        removed = _iso_day(val.get("removed_at") or val.get("uninstalled_at"))
        on = val.get("on", val.get("enabled"))
        if on is None:
            on = bool(since) and not (removed and since < removed)
        return {"on": bool(on), "since": since if bool(on) else None}
    return None


def _routing_markers(facade):
    """Fallback: routing_injected_at / routing_removed_at markers in the data
    dir, or a routing_state.json carrying the same fields."""
    snap = getattr(facade, "SNAPSHOT_DIR", None)
    if snap is None:
        return {"on": False, "since": None}
    snap = Path(snap)
    injected = _marker_date(snap / "routing_injected_at")
    removed = _marker_date(snap / "routing_removed_at")
    if injected is None:
        state = _read_json(snap / "routing_state.json") or _read_json(snap / "routing.json") or {}
        injected = _iso_day(state.get("injected_at") or state.get("enabled_since")
                            or state.get("since") or state.get("date"))
        if removed is None:
            removed = _iso_day(state.get("removed_at") or state.get("uninstalled_at"))
    on = bool(injected) and not (removed and removed > injected)
    return {"on": on, "since": injected if on else None}


def _routing(facade):
    """{"on": bool, "since": "YYYY-MM-DD"|None} for routing guidance."""
    for name in _ROUTING_MODULES:
        fn = _sibling(name, "routing_enabled_since")
        if fn is None:
            continue
        snap = getattr(facade, "SNAPSHOT_DIR", None)
        val, answered = _call_flex(fn, (facade,), (snap,), ())
        if not answered:
            continue
        state = _norm_routing(val)
        if state is not None:
            return state
    return _routing_markers(facade)


# ---------------------------------------------------------------------------
# Setup trims
# ---------------------------------------------------------------------------

def _trims(facade, data):
    """Count of setup trims still applied, or None when unreadable."""
    try:
        from . import structural_ledger
        rows = structural_ledger.load_trims(facade.SNAPSHOT_DIR)
        return sum(1 for t in rows if not t.get("closed_at"))
    except Exception:
        pass
    try:
        rows = ((data or {}).get("structural_detail") or {}).get("trims") or []
        return sum(1 for t in rows if not t.get("closed_at"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Nudge follow-through
# ---------------------------------------------------------------------------

def _norm_counts(val):
    """Normalize a helper result to {"followed": int|None, "total": int}."""
    followed = total = None
    if isinstance(val, dict):
        followed = val.get("followed", val.get("followed_count", val.get("n_followed")))
        total = val.get("total", val.get("emitted", val.get("count", val.get("nudges"))))
    elif isinstance(val, (tuple, list)) and len(val) >= 2:
        followed, total = val[0], val[1]
    else:
        return None
    try:
        total = int(total)
    except (TypeError, ValueError):
        return None
    try:
        followed = int(followed) if followed is not None else None
    except (TypeError, ValueError):
        followed = None
    return {"followed": followed, "total": total}


def _nudges_helper(facade):
    for name in _NUDGE_MODULES:
        for fname in _NUDGE_FUNCS:
            fn = _sibling(name, fname)
            if fn is None:
                continue
            val, answered = _call_flex(fn, (facade,), (facade.SNAPSHOT_DIR,), ())
            if not answered:
                continue
            counts = _norm_counts(val)
            if counts is not None:
                return counts
    return None


def _parse_ts(value):
    """Epoch seconds or ISO-8601 -> aware datetime, or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _nudges_sql(facade):
    """Read nudge_events directly: (followed, total) over the last 7 days.

    `followed` is None when the table has no status column yet (emissions are
    recorded but the follow-through resolver has not classified them). Rows
    are filtered in Python so ISO, epoch and mixed timestamp formats all work.
    """
    try:
        conn = facade._open_trends_db_readonly(timeout=1.0)
    except Exception:
        try:
            db = getattr(facade, "TRENDS_DB", None)
            if db is None or not Path(db).exists():
                return {"followed": 0, "total": 0}
            conn = sqlite3.connect(str(db))
        except Exception:
            return None
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "nudge_events" not in tables:
            return {"followed": 0, "total": 0}
        cols = [r[1] for r in conn.execute("PRAGMA table_info(nudge_events)")]
        ts_col = next((c for c in _TS_COLS if c in cols), None)
        if not ts_col:
            return None
        status_col = next((c for c in _STATUS_COLS if c in cols), None)
        select = ts_col if not status_col else "%s, %s" % (ts_col, status_col)
        rows = conn.execute("SELECT %s FROM nudge_events" % select).fetchall()
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        total = 0
        followed = 0 if status_col else None
        for row in rows:
            dt = _parse_ts(row[0])
            if dt is None or dt < cutoff:
                continue
            total += 1
            if status_col and str(row[1] or "").strip().lower() in _FOLLOWED_VALUES:
                followed += 1
        return {"followed": followed, "total": total}
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _nudges(facade):
    counts = _nudges_helper(facade)
    if counts is not None:
        return counts
    counts = _nudges_sql(facade)
    return counts if counts is not None else {"followed": None, "total": None}


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def collect(facade=None):
    """The pilot report as a plain dict. Never raises."""
    try:
        f = _facade(facade)
    except Exception:
        f = None
    report = {
        "version": None, "install_date": None, "install_source": None,
        "days_installed": None,
        "capacity_pct": None, "capacity_basis": None,
        "saved_since_install_usd": None,
        "saved_7d_usd": None,
        "split": {"measured_usd": None, "estimated_usd": None,
                  "routing_usd": None},
        "routing": {"on": False, "since": None},
        "setup_trims_applied": None,
        "nudges": {"followed": None, "total": None},
    }
    if f is None:
        return report
    try:
        report["version"] = getattr(f, "TOKEN_OPTIMIZER_VERSION", None) or "unknown"
    except Exception:
        report["version"] = "unknown"

    install_date = install_source = None
    try:
        install_date, install_source = _install(f)
    except Exception:
        pass
    report["install_date"] = install_date
    report["install_source"] = install_source
    days_installed = None
    if install_date:
        try:
            days_installed = (f.datetime.now().date()
                              - f.datetime.fromisoformat(install_date).date()).days + 1
            days_installed = max(1, days_installed)
        except Exception:
            days_installed = None
    report["days_installed"] = days_installed

    # The capacity window is "since install": the whole post-install period,
    # bounded at 30 days for stability (the dashboard card's own window).
    capacity_days = min(30, days_installed) if days_installed else 30
    data = _dashboard(f, capacity_days)
    pct, basis = _capacity(data.get("before_after"))
    report["capacity_pct"] = pct
    report["capacity_basis"] = basis

    try:
        routing = _routing(f)
    except Exception:
        routing = {"on": False, "since": None}
    report["routing"] = routing
    routing_on = bool(routing.get("on"))

    since = _since_install(data, routing_on)
    report["saved_since_install_usd"] = since["saved_usd"]
    report["split"] = {"measured_usd": since["measured_usd"],
                       "estimated_usd": since["estimated_usd"],
                       "routing_usd": since["routing_usd"]}

    try:
        data7 = _dashboard(f, 7)
        report["saved_7d_usd"] = _saved_7d(data7, routing_on)
    except Exception:
        pass
    try:
        report["setup_trims_applied"] = _trims(f, data)
    except Exception:
        report["setup_trims_applied"] = None

    try:
        report["nudges"] = _nudges(f)
    except Exception:
        pass
    return report


def render(report):
    """Plain-text lines for Slack. Always under 12 lines, never a path."""
    report = report if isinstance(report, dict) else {}
    version = report.get("version") or "unknown"
    install = report.get("install_date") or "date not recorded"
    lines = ["Token Optimizer %s, installed %s" % (version, install)]

    pct = report.get("capacity_pct")
    if pct is None:
        lines.append("Capacity since install: not measured yet")
    else:
        lines.append("Capacity since install: %+d%% more work per token vs %s"
                     % (pct, report.get("capacity_basis") or "your baseline period"))

    saved = _money(report.get("saved_since_install_usd"))
    lines.append("Saved since install: %s (all-in, API-price equivalent)"
                 % (saved if saved is not None else "not available yet"))
    saved7 = _money(report.get("saved_7d_usd"))
    lines.append("Saved in the last 7 days: %s"
                 % (saved7 if saved7 is not None else "not available yet"))

    split = report.get("split") or {}
    parts = [_money(split.get(k)) for k in ("measured_usd", "estimated_usd", "routing_usd")]
    if any(p is not None for p in parts):
        lines.append("Since install split - measured %s / estimated %s / routing %s"
                     % tuple(p if p is not None else "$0.00" for p in parts))
    else:
        lines.append("Since install split: not available yet")

    routing = report.get("routing") or {}
    if routing.get("on"):
        since = routing.get("since")
        lines.append("Routing guidance: on%s" % (" since " + since if since else ""))
    else:
        lines.append("Routing guidance: off")

    trims = report.get("setup_trims_applied")
    if trims:
        lines.append("Setup trims: %d applied" % trims)
    elif trims == 0:
        lines.append("Setup trims: none yet (run /token-optimizer)")
    else:
        lines.append("Setup trims: not counted yet")

    nudges = report.get("nudges") or {}
    followed, total = nudges.get("followed"), nudges.get("total")
    if followed is None or total is None:
        lines.append("Nudges: follow-up not resolved yet")
    else:
        lines.append("Nudges: followed %d of %d this week" % (followed, total))
    return lines[:12]


def main(argv=None, facade=None):
    """CLI entry: `pilot-report [--json]`. Returns a process exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = False
    for arg in args:
        if arg == "--json":
            as_json = True
        elif arg in ("-h", "--help"):
            print("Usage: python3 measure.py pilot-report [--json]")
            print("  Weekly pilot report for Slack. --json prints the same data as JSON.")
            return 0
        else:
            print("[Error] Unknown pilot-report option: %s" % arg)
            return 2
    try:
        report = collect(facade)
    except Exception:
        report = {"error": "pilot report unavailable"}
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        for line in render(report):
            print(line)
    return 0
