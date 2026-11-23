"""The rolling 30-day savings headline for older clients.

Uses action savings, realized routing, and the token headline for the same window.
The stable since-install headline lives in counted_headline.

usd_week stays null: there is no honest weekly figure yet and 30 days are never
prorated into one. display is null whenever no threshold is met, which is how
surfaces know to show no number at all.
"""
from __future__ import annotations

import math

# The dashboard's Savings deep link. Claude's daemon serves on 24842; callers
# on another runtime may pass dashboard_url to build().
DASHBOARD_SAVINGS_URL = "http://localhost:24842/token-optimizer#savings"

_WEEK_MIN_USD = 20.0
_MONTH_MIN_USD = 60.0
_TOKENS_MIN = 1_000_000


def _usd(value):
    """A finite dollar figure, or None."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _count(value):
    """A non-negative integer token count, or 0."""
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _usd_text(value):
    """Whole dollars with a thousands separator: 1234.4 -> '$1,234'."""
    return "${:,.0f}".format(value)


def _token_text(value):
    """One decimal for K/M/B, matching the dashboard's token format."""
    v = float(value or 0)
    if v >= 1e9:
        return "{:.1f}B".format(v / 1e9)
    if v >= 1e6:
        return "{:.1f}M".format(v / 1e6)
    if v >= 1e3:
        return "{:.1f}K".format(v / 1e3)
    return "{:,.0f}".format(v)


def _display(usd_30d, usd_week, tokens_30d, url):
    """Pick the one number a surface may show, or None."""
    if usd_week is not None and usd_week >= _WEEK_MIN_USD:
        kind = "week"
        amount = _usd_text(usd_week)
        short = "Saved {} this week".format(amount)
        long = "{} saved this week".format(amount)
    elif usd_30d >= _MONTH_MIN_USD:
        kind = "30d"
        amount = _usd_text(usd_30d)
        tok = _token_text(tokens_30d)
        short = "Saved {} in the last 30 days".format(amount)
        long = "{} saved in the last 30 days".format(amount)
        if tokens_30d > 0:
            short += " · {} kept out".format(tok)
            long += " · {} tokens kept out of context".format(tok)
    elif tokens_30d >= _TOKENS_MIN:
        kind = "tokens"
        tok = _token_text(tokens_30d)
        short = "{} tokens kept out of context".format(tok)
        long = short
    else:
        return None
    return {"kind": kind, "short": short, "long": long, "url": url}


def build(summary, dashboard_url=DASHBOARD_SAVINGS_URL):
    """The savings_headline contract object for one savings summary.

    `summary` is the dashboard's savings payload (or the status-bar headline
    built from the same inputs). Missing pieces read as zero, which lands on display null.
    Never raises.
    """
    try:
        s = summary if isinstance(summary, dict) else {}
        action = s.get("action_savings") or {}
        if not isinstance(action, dict):
            action = {}
        headline = s.get("token_headline") or {}
        if not isinstance(headline, dict):
            headline = {}
        if "total_tokens" not in headline:
            # The dashboard payload also nests the same object under runway.
            runway = s.get("runway") or {}
            if isinstance(runway, dict):
                nested = runway.get("token_headline") or {}
                if isinstance(nested, dict):
                    headline = nested

        routing = s.get("model_routing")
        realized = _usd(routing.get("realized_cost_usd")) if isinstance(routing, dict) else None
        if realized is None:
            realized = _usd(action.get("routing_usd"))
        # Match the dashboard: add the positive realized routing estimate once.
        usd_30d = (_usd(action.get("total_usd")) or 0.0) + max(0.0, realized or 0.0)
        tokens_30d = _count(headline.get("total_tokens"))
        usd_week = None
        url = dashboard_url if isinstance(dashboard_url, str) and dashboard_url else DASHBOARD_SAVINGS_URL
        return {
            "label": "last 30 days",
            "usd_30d": usd_30d,
            "usd_week": usd_week,
            "tokens_30d": tokens_30d,
            "display": _display(usd_30d, usd_week, tokens_30d, url),
        }
    except Exception:
        return {"label": "last 30 days", "usd_30d": 0.0, "usd_week": None, "tokens_30d": 0, "display": None}
