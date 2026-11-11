"""Where each "before" baseline came from, and whether Token Optimizer was
already running inside it.

The transformation card and the routing estimate both compare today against an
earlier window of the user's own history. That window is only a true "before"
when it ends before the install. A fresh install with no older transcripts, a
rebuilt ledger, or a lost baseline file all leave an earliest window in which
the tool was already working, so the comparison understates the result. This
module finds the install date from the evidence on disk, classifies each
baseline window against it, and writes the sentence every surface shows.

Pure reads. Never raises.
"""
import json
import re
import sqlite3
from datetime import datetime, timedelta

ROUTING_HISTORY_DAYS = 14
# Days of a baseline window that may fall after install before the window stops
# counting as pre-install (the install day itself, plus a short tail).
_TAIL_SHARE = 0.10
_INSTALL_PROOF = ("snapshot", "first_event")


def _day(value):
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _iso(day):
    return day.strftime("%Y-%m-%d") if day else None


def _read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _first_event_day(facade):
    """Date of the earliest event Token Optimizer logged, or None."""
    try:
        if not facade.TRENDS_DB.exists():
            return None
        conn = facade._open_trends_db_readonly(timeout=1.0)
    except Exception:
        return None
    found = []
    try:
        for table in ("savings_events", "compression_events"):
            try:
                row = conn.execute(
                    "SELECT MIN(timestamp) FROM " + table + " WHERE timestamp >= '2000'").fetchone()
            except sqlite3.Error:
                continue
            day = _day(row[0]) if row and row[0] else None
            if day:
                found.append(day)
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    return min(found) if found else None


def _earliest_session_day(facade):
    try:
        if not facade.TRENDS_DB.exists():
            return None
        conn = facade._init_trends_db()
        try:
            facade.ensure_activity_view(conn)
            row = conn.execute("SELECT MIN(date) FROM session_activity WHERE date >= '2000'").fetchone()
        finally:
            conn.close()
        return _day(row[0]) if row and row[0] else None
    except Exception:
        return None


def install_evidence(facade):
    """{"date", "source"}: the earliest dated proof the tool was installed.

    "snapshot" is the first-run baseline snapshot, "first_event" the earliest
    logged event (it wins when a later audit re-took the snapshot). With
    neither, the earliest session on record stands in as "earliest_session":
    good enough to date a since-install total, but not proof of an install, so
    no baseline window is classified against it.
    """
    proofs = []
    try:
        snap = _read_json(facade.SNAPSHOT_DIR / "snapshot_before.json")
        day = _day((snap or {}).get("timestamp"))
        if day:
            proofs.append((day, "snapshot"))
    except Exception:
        pass
    day = _first_event_day(facade)
    if day:
        proofs.append((day, "first_event"))
    if proofs:
        day, source = min(proofs, key=lambda item: item[0])
        return {"date": _iso(day), "source": source}
    day = _earliest_session_day(facade)
    return {"date": _iso(day), "source": "earliest_session" if day else None}


def window_coverage(start, end, install):
    """Classify the inclusive window [start, end] against the install date.

    coverage: "pre_install" (a true before), "partial" (installed part-way
    through), "post_install" (the tool ran for the whole window), or "unknown"
    (no window, or no proof of when the tool was installed).
    """
    s, e = _day(start), _day(end)
    install = install or {}
    out = {"start": _iso(s), "end": _iso(e), "coverage": "unknown",
           "install_date": install.get("date"), "install_source": install.get("source"),
           "window_days": None, "post_install_days": None}
    if not s or not e or e < s:
        return out
    days = (e - s).days + 1
    out["window_days"] = days
    d = _day(install.get("date"))
    if not d or install.get("source") not in _INSTALL_PROOF:
        return out
    post = 0 if d > e else (e - max(s, d)).days + 1
    out["post_install_days"] = post
    if post <= max(1, int(days * _TAIL_SHARE)):
        out["coverage"] = "pre_install"
    elif d <= s:
        out["coverage"] = "post_install"
    else:
        out["coverage"] = "partial"
    return out


def _span(cov):
    return "%s to %s" % (cov["start"], cov["end"]) if cov.get("start") and cov.get("end") else ""


def _transformation_copy(cov):
    span = _span(cov)
    where = " (%s)" % span if span else ""
    state = cov["coverage"]
    if state == "pre_install":
        return ("compared with your own baseline from before you installed%s." % where, "")
    lead = "compared with your earliest tracked month%s." % where
    if state == "post_install":
        note = ("Token Optimizer was already running in that month (installed %s), so this is the "
                "change since then. Your usage from before install is not on record, so the full "
                "before-and-after gap cannot be measured and this figure understates it."
                % cov["install_date"])
    elif state == "partial":
        note = ("Token Optimizer was installed on %s, inside that month (%d of its %d days came "
                "after install), so the true gap is larger and this figure understates it."
                % (cov["install_date"], cov["post_install_days"], cov["window_days"]))
    else:
        note = ("The install date is not on record, so it is not known whether Token Optimizer "
                "was already running in that month.")
    if cov.get("pending_anchor") == "building":
        note += (" A closer baseline is still being built from your older transcripts; "
                 "this figure will update when it is ready.")
    return lead, note


def transformation(facade, baseline, weight_pool):
    """Provenance of the transformation card's "before" arm.

    anchor: "pinned_window" (the frozen early window, priced from transcripts),
    "calendar_month" (earliest complete month still in the ledger) or
    "frozen_session" (the frozen typical session, no work-unit pool).
    """
    try:
        baseline = baseline or {}
        window = baseline.get("window") or {}
        anchor, start, end, pending = "frozen_session", window.get("start"), window.get("end"), None
        if weight_pool:
            label = str(weight_pool.get("anchor_month") or "")
            month = re.fullmatch(r"(\d{4})-(\d{2})", label)
            if month:
                anchor = "calendar_month"
                first = datetime(int(month.group(1)), int(month.group(2)), 1)
                start = _iso(first)
                end = _iso((first + timedelta(days=32)).replace(day=1) - timedelta(days=1))
                state = _read_json(facade.SNAPSHOT_DIR / facade._PRETOOL_ANCHOR_FILE) or {}
                if state.get("status") == "building":
                    pending = "building"
            else:
                anchor = "pinned_window"
                dates = re.findall(r"\d{4}-\d{2}-\d{2}", label)
                if len(dates) == 2:
                    start, end = dates
        cov = window_coverage(start, end, install_evidence(facade))
        try:
            stated = baseline.get("opus_share_source") == "pretool_baseline"
            share = float(baseline.get("opus_share") or 0)
        except (TypeError, ValueError):
            stated, share = False, 0.0
        cov.update(anchor=anchor, pending_anchor=pending,
                   mix_source="stated" if stated else ("measured" if share > 0 else "current_mix"))
        cov["lead"], cov["note"] = _transformation_copy(cov)
        cov["mix_note"] = {
            "stated": "stated by you (set-baseline)",
            "measured": "measured in your earliest tracked month",
            "current_mix": "not measured, so your current mix is used on both sides",
        }[cov["mix_source"]]
        return cov
    except Exception:
        return None


def weight_pool_caveat(anchor_label, provenance):
    """The method paragraph under a work-normalized transformation."""
    text = (
        "Work-normalized counterfactual: every API call you dispatched this "
        "period, priced at the baseline's cost per call ("
        + str(anchor_label) + "), at ONE constant rate card both eras -- so only how much "
        "context each call carries (and how often the cache serves it) moves "
        "the number; model-price and mix changes between eras are excluded and "
        "disclosed separately, not claimed. Sessions are not used as the unit "
        "because the lighter workflow itself splits the same work into more, "
        "smaller sessions, which would count capacity gained as money saved. "
        "The delta includes every volume mechanism at once (context "
        "compression, lean output, loop prevention, continuity handovers), so "
        "the metered and estimated pools are not added on top -- they would "
        "double-count; the directly-metered floor still shows in its own card. "
        "Delegated (subagent) work is excluded from this figure. The per-call delta "
        "cannot separate Token Optimizer's effect from your own behaviour "
        "drift -- the metered floor is the part we can prove action by action."
    )
    note = (provenance or {}).get("note")
    return text + (" " + note if note else "")


def routing_baseline(facade):
    """(shares, source, stated, early_mix) for realized routing when the user
    has no stated baseline file: the mix captured at install, else the frozen
    baseline (stable, and the same one the transformation card uses), else the
    earliest days still in the ledger, which move if the ledger is rebuilt.
    """
    try:
        snap = _read_json(facade.SNAPSHOT_DIR / "snapshot_before.json") or {}
        mix = snap.get("model_mix") or {}
        shares = (mix.get("shares") or {}) if mix.get("activity_basis") == 1 else {}
        if shares:
            return shares, "snapshot", False, None
    except Exception:
        pass
    try:
        frozen = _read_json(facade.SNAPSHOT_DIR / "baseline_state.json") or {}
        shares = frozen.get("model_shares")
        if (frozen.get("activity_basis") == 1 and isinstance(shares, dict)
                and any(isinstance(v, (int, float)) and v > 0 for v in shares.values())):
            return (dict(shares), "frozen_baseline",
                    frozen.get("opus_share_source") == "pretool_baseline", None)
    except Exception:
        pass
    early = facade._earliest_model_mix()
    if early:
        return early["shares"], "history", False, early
    return None, None, False, None


def _routing_window(facade, source):
    if source == "snapshot":
        snap = _read_json(facade.SNAPSHOT_DIR / "snapshot_before.json") or {}
        end = _day(snap.get("timestamp"))
        return (_iso(end - timedelta(days=ROUTING_HISTORY_DAYS)), _iso(end)) if end else (None, None)
    if source == "frozen_baseline":
        window = (_read_json(facade.SNAPSHOT_DIR / "baseline_state.json") or {}).get("window") or {}
        return window.get("start"), window.get("end")
    if source == "history":
        first = _day((facade._earliest_model_mix() or {}).get("first_date"))
        return (_iso(first), _iso(first + timedelta(days=ROUTING_HISTORY_DAYS))) if first else (None, None)
    return None, None


def routing(facade, result):
    """Provenance of the routing estimate's baseline mix, with display copy."""
    try:
        result = result or {}
        source = result.get("baseline_source")
        if not source:
            return None
        try:
            pct = "%d%%" % round(float(result.get("baseline_opus_share") or 0) * 100)
        except (TypeError, ValueError):
            pct = ""
        if source == "pretool_baseline" or result.get("baseline_stated"):
            return {"source": source, "kind": "stated", "coverage": "stated",
                    "lead": "At your stated %s Opus baseline." % pct,
                    "short": "assuming %s Opus before Token Optimizer" % pct,
                    "ledger": "Estimated routing, at your stated %s Opus baseline" % pct}
        install = install_evidence(facade)
        if source == "snapshot" and install.get("source") == "snapshot":
            # The mix was read from the days before the first-run snapshot.
            start, end = _routing_window(facade, source)
            cov = window_coverage(start, end, {"date": None, "source": None})
            cov.update(coverage="pre_install", install_date=install["date"], install_source="snapshot")
        else:
            cov = window_coverage(*_routing_window(facade, source), install)
        span = _span(cov)
        when = " from %s" % span if span else ""
        state = cov["coverage"]
        if state == "pre_install":
            lead = "Compared with your measured model mix before install (%s Opus%s)." % (pct, when)
            short = "against your measured %s Opus mix before install" % pct
        elif state in ("post_install", "partial"):
            lead = ("Compared with your earliest measured model mix (%s Opus%s). Token Optimizer was "
                    "already running then (installed %s), so this is the shift since that period and "
                    "understates the shift since install." % (pct, when, cov["install_date"]))
            short = ("against your earliest measured mix, %s Opus%s, with Token Optimizer already "
                     "running" % (pct, when))
        else:
            lead = ("Compared with your earliest measured model mix (%s Opus%s). The install date is "
                    "not on record, so it is not known whether Token Optimizer was already running "
                    "then." % (pct, when))
            short = "against your earliest measured mix, %s Opus%s" % (pct, when)
        cov.update(source=source, kind="measured", lead=lead, short=short,
                   ledger="Estimated routing, " + short)
        return cov
    except Exception:
        return None
