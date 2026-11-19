"""Limit realized routing credit to the period with routing guidance."""
from datetime import date, datetime, timedelta
from . import _compat


def routing_enabled_since(data_dir):
    try:
        value = (data_dir / 'routing_injected_at').read_text(encoding='utf-8').strip()
        return date.fromisoformat(value).isoformat()
    except (OSError, ValueError, TypeError):
        return None


def compute(days=30, since=None):
    f = _compat.facade()
    enabled = routing_enabled_since(f.SNAPSHOT_DIR)
    cutoff = str(since)[:10] if since else (datetime.now() - timedelta(days=days or 30)).strftime('%Y-%m-%d')
    if enabled:
        cutoff = max(cutoff, enabled)
    unrestricted = getattr(f, '_routing_unrestricted', None)
    if unrestricted is None:
        unrestricted = f._measure_providers['pricing']._compute_model_routing_savings
    result = unrestricted(days=days, since=cutoff)
    result['routing_enabled'] = bool(enabled)
    if not enabled:
        result['realized_cost_usd'] = 0.0
        result['measured_baseline_realized_cost_usd'] = None
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
    if early and early.get('shares'):
        return early['shares'], 'history', False, early

    stated = f._pretool_baseline_mix()
    if stated:
        return stated, 'pretool_baseline', True, None
    if shares and stated_frozen:
        return shares, 'frozen_baseline', True, None
    return None, None, False, None
