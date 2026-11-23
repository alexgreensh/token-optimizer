"""Read-only savings headlines for the dashboard, CLI and status cache."""
import argparse
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import sqlite3

from . import counted_closes, savings_headline


@contextmanager
def _reader(facade, conn=None):
    if conn is not None:
        yield conn
        return
    try:
        db = sqlite3.connect(facade.TRENDS_DB.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
    except (AttributeError, OSError, sqlite3.Error):
        yield None
        return
    try:
        yield db
    finally:
        db.close()


def _today():
    return datetime.now(timezone.utc).date()


def _money(value):
    return "$" + format(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP), ",")


def _signed_money(value, *, whole=False):
    amount = _money(abs(value)) if whole else f"${abs(value):,.2f}"
    return ("-" if value < 0 else "+") + amount


def _headline(balance, url, *, overall=False):
    usd, tokens, since = balance["usd"], balance["tokens"], balance["since"]
    day = date.fromisoformat(since) if since else None
    correction = balance["last_correction"]
    if correction and not 0 <= (_today() - date.fromisoformat(correction["date"])).days <= 14:
        correction = None
    if usd >= 1 or overall:
        amount = "under $1" if 0 < usd < 1 else _money(usd)
        short_since = day.strftime("%b %Y") if day and not overall else "install"
        long_since = f"{day:%b} {day.day}, {day.year}" if day else "install"
        long_verb = "saved" if overall else "counted"
        display = {"kind": "since", "short": f"Saved {amount} since {short_since}",
                   "long": f"{amount} {long_verb} since {long_since}", "url": url}
    else:
        display = savings_headline._display(0, None, tokens, url)
    if display and correction:
        display["short"] += f" (corrected {_signed_money(correction['delta_usd'], whole=True)})"
        display["long"] += f". {correction['reason']}"
    return {"schema": 1, "usd": usd, "tokens": tokens, "since": since,
            "rechecking": balance["held_days"] > 0, "held_days": balance["held_days"],
            "last_correction": correction, "display": display}


def build(facade, conn=None, dashboard_url=None):
    """Build the counted-only headline from stored daily balances."""
    with _reader(facade, conn) as db:
        return _headline(counted_closes.total(db), dashboard_url or savings_headline.DASHBOARD_SAVINGS_URL)


def build_overall(facade, conn=None, dashboard_url=None):
    """Build the all-in since-install headline from the latest daily balance."""
    with _reader(facade, conn) as db:
        balance = counted_closes.total_overall(db)
        if balance is None:
            return None
        return _headline(balance, dashboard_url or savings_headline.DASHBOARD_SAVINGS_URL, overall=True)


def cache_value(facade, session_id):
    """Supply the status consumer's since-install headline, even after a timeout."""
    with _reader(facade) as conn:
        if conn is None:
            return None
        return build_overall(facade, conn) or build(facade, conn)


def correction_line(correction):
    return f"{correction['date']}  {_signed_money(correction['delta_usd'])}  {correction['reason']}"


def main(facade, args):
    parser = argparse.ArgumentParser(prog="measure.py counted-total")
    parser.add_argument("--json", action="store_true", dest="as_json")
    options = parser.parse_args(args)
    with _reader(facade) as conn:
        data = build(facade, conn)
        overall = counted_closes.total_overall(conn) or {}
        changes = counted_closes.corrections(conn, _today())
    if options.as_json:
        print(json.dumps(data | {"corrections": changes}, indent=2))
        return 0
    print((data["display"] or {}).get("long") or
          f"${data['usd']:,.2f} counted since {data['since'] or 'install'}")
    estimated = overall.get("estimated_usd")
    last_30d = overall.get("last_30d_usd")
    if estimated is not None:
        print(f"Estimated on top (can move): ${estimated:,.2f}")
    if last_30d is not None:
        print(f"Last 30 days (moves as days roll off): ${last_30d:,.2f}")
    for change in changes:
        print(correction_line(change))
    return 0
