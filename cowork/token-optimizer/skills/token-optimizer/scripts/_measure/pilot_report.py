"""Weekly pilot report: `measure.py pilot-report`.

One paste-able Slack summary a pilot user can run once a week. Plain text,
under a dozen lines, no personal paths; `--json` emits the same data. Every
figure comes from the same payloads the dashboard renders, so the report can
never quote a different dollar than the Savings page.

The report uses the shipped baseline, routing, ledger and nudge readers.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from . import _compat



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
    """All-in saved-since-install with the measured/estimated/routing split."""
    si = (data or {}).get("since_install") or {}
    all_in = si.get("all_in") or {}
    out = {"saved_usd": None, "measured_usd": None,
           "estimated_usd": None, "routing_usd": None}
    if isinstance(all_in, dict) and all_in:
        routing_in = _routing_in_total(all_in)
        total = _num(all_in.get("total_usd"))
        saved = total
        estimated = _num(all_in.get("estimated_usd"))
        routing = routing_in
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
    action_savings.total_usd plus realized routing)."""
    action = (data7 or {}).get("action_savings") or {}
    total = action.get("total_usd")
    if total is None:
        return None
    routing = _num(((data7 or {}).get("model_routing") or {}).get("realized_cost_usd"))
    return round(_num(total) + routing, 2)


# ---------------------------------------------------------------------------
# Routing guidance state
# ---------------------------------------------------------------------------

def _routing(facade):
    from . import routing_default
    snap = getattr(facade, 'SNAPSHOT_DIR', None)
    if snap is None:
        return {'on': False, 'since': None}
    try:
        failure = (Path(snap) / 'routing_injection_failure').read_text(encoding='utf-8').strip()
        if failure:
            return {'on': False, 'since': None, 'failure': failure[:200]}
    except OSError:
        pass
    injected, removed = routing_default.routing_credit_window(data_dir=snap)
    return {'on': bool(injected and not removed), 'since': injected if injected and not removed else None}


# ---------------------------------------------------------------------------
# Setup trims
# ---------------------------------------------------------------------------

def _trims(facade, data):
    from . import structural_ledger
    try:
        return sum(1 for row in structural_ledger.load_trims(facade.SNAPSHOT_DIR) if not row.get('closed_at'))
    except Exception:
        return None


def _nudges(facade):
    from . import nudge_followthrough
    try:
        return nudge_followthrough.weekly_counts(facade)
    except Exception:
        return {'followed': None, 'total': None}


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
        data7 = data if capacity_days == 7 else _dashboard(f, 7)
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
    if routing.get("failure"):
        lines.append("Routing guidance: failed (%s)" % routing["failure"])
    elif routing.get("on"):
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
