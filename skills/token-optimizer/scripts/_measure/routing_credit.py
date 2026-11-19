"""Limit realized routing credit to the period with routing guidance."""
from datetime import date, datetime, timedelta
from . import _compat, routing_default


def routing_enabled_since(data_dir):
    enabled, _ = routing_default.routing_credit_window(data_dir=data_dir)
    try:
        return date.fromisoformat(enabled).isoformat()
    except (ValueError, TypeError):
        return None


def compute(days=30, since=None):
    f = _compat.facade()
    intervals = routing_default.routing_credit_intervals(data_dir=f.SNAPSHOT_DIR)
    cutoff = str(since) if since else (datetime.now() - timedelta(days=days or 30)).strftime('%Y-%m-%d')
    unrestricted = getattr(f, '_routing_unrestricted', None)
    if unrestricted is None:
        unrestricted = f._measure_providers['pricing']._compute_model_routing_savings
    valid = []
    for enabled, removed in intervals:
        try:
            enabled = date.fromisoformat(enabled).isoformat()
            removed = date.fromisoformat(removed).isoformat() if removed else None
        except (ValueError, TypeError):
            continue
        start = max(cutoff, enabled)
        if not removed or start < removed:
            valid.append((start, removed))
    results = [unrestricted(days=days, since=start, **({'until': end} if end else {}))
               for start, end in valid]
    result = dict(results[-1]) if results else unrestricted(days=days, since=cutoff)
    result['routing_enabled'] = bool(intervals)
    for key in ('realized_cost_usd', 'measured_baseline_realized_cost_usd'):
        values = [item.get(key) for item in results if item.get(key) is not None]
        result[key] = sum(values) if values else (0.0 if key == 'realized_cost_usd' else None)
    return result


def baseline(f):
    """Choose the most specific measured baseline before any stated share."""
    from . import baseline_provenance

    frozen = f._get_baseline_state() or {}
    shares = frozen.get('model_shares') or {}
    stated_frozen = frozen.get('opus_share_source') == 'pretool_baseline'
    window = frozen.get('window') or {}
    coverage = window.get('coverage')
    if not coverage and window.get('start') and window.get('end'):
        coverage = baseline_provenance.window_coverage(
            window['start'], window['end'], baseline_provenance.install_evidence(f))['coverage']
    if shares and not stated_frozen and coverage == 'pre_install':
        return shares, 'frozen_baseline', False, None

    snap = baseline_provenance._read_json(f.SNAPSHOT_DIR / 'snapshot_before.json') or {}
    mix = snap.get('model_mix') or {}
    if mix.get('activity_basis') == 1 and mix.get('shares'):
        return mix['shares'], 'snapshot', False, None

    early = f._earliest_model_mix()
    first = (early or {}).get('first_date')
    boundary = routing_enabled_since(f.SNAPSHOT_DIR) or str(snap.get('timestamp') or '')[:10] or date.today().isoformat()
    if shares and window.get('end') and (not first or first > window['end'] or coverage == 'post_install'):
        return shares, 'frozen_baseline', stated_frozen, None
    if early and early.get('shares') and (not first or first < boundary):
        return early['shares'], 'history', False, early

    stated = f._pretool_baseline_mix()
    if stated:
        return stated, 'pretool_baseline', True, None
    if shares and stated_frozen:
        return shares, 'frozen_baseline', True, None
    return None, None, False, None
