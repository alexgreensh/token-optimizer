"""The one savings headline shared by the band, the status line and the dashboard.

Computes the contract object (briefs/ux-contract.md) from the same summary the
dashboard Savings hero reads: ``action_savings.total_usd`` plus the model
routing figure, and ``token_headline.total_tokens``. Every surface prints this
one object so they can never quote different dollars for the same window.

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
        short = "Saved {}/30d".format(amount)
        long = "{} saved in 30 days".format(amount)
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
    built from the same inputs): action_savings, model_routing and
    token_headline. Missing pieces read as zero, which lands on display null.
    Never raises.
    """
    try:
        s = summary if isinstance(summary, dict) else {}
        action = s.get("action_savings") or {}
        if not isinstance(action, dict):
            action = {}
        routing = s.get("model_routing") or {}
        if not isinstance(routing, dict):
            routing = {}
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

        total = _usd(action.get("total_usd")) or 0.0
        # The hero adds the routing estimate on top of the action total; the
        # same figure sits in model_routing.realized_cost_usd (action_savings'
        # routing_usd is copied from it).
        routing_usd = _usd(routing.get("realized_cost_usd"))
        if routing_usd is None:
            routing_usd = _usd(action.get("routing_usd"))
        usd_30d = total + max(0.0, routing_usd or 0.0)
        tokens_30d = _count(headline.get("total_tokens"))
        usd_week = None
        url = dashboard_url if isinstance(dashboard_url, str) and dashboard_url else DASHBOARD_SAVINGS_URL
        return {
            "usd_30d": usd_30d,
            "usd_week": usd_week,
            "tokens_30d": tokens_30d,
            "display": _display(usd_30d, usd_week, tokens_30d, url),
        }
    except Exception:
        return {"usd_30d": 0.0, "usd_week": None, "tokens_30d": 0, "display": None}
