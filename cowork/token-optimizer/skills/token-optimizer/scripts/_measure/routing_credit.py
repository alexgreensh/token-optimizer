"""Calculate routing credit independently of routing guidance state."""
from datetime import date
from . import _compat, routing_default


def routing_enabled_since(data_dir):
    enabled, _ = routing_default.routing_credit_window(data_dir=data_dir)
    try:
        return date.fromisoformat(enabled).isoformat()
    except (ValueError, TypeError):
        return None


def compute(days=30, since=None):
    f = _compat.facade()
    unrestricted = getattr(f, '_routing_unrestricted', None)
    if unrestricted is None:
        from . import routing_savings
        unrestricted = routing_savings.unrestricted
    return unrestricted(days=days, since=since)


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
    install_date = getattr(f, '_install_date', lambda: None)()
    boundary = install_date or str(snap.get('timestamp') or '')[:10] or date.today().isoformat()
    if early and early.get('shares') and (not first or first < boundary):
        return early['shares'], 'history', False, early

    stated = f._pretool_baseline_mix()
    if stated:
        return stated, 'pretool_baseline', True, None
    if shares and stated_frozen:
        return shares, 'frozen_baseline', True, None
    # Last resort: a window from after install already carries Token
    # Optimizer's effect, so it never outranks a pre-install or stated mix.
    # It is still better than no baseline; it is labeled post_install.
    if shares and window.get('end'):
        return shares, 'frozen_baseline', False, None
    return None, None, False, None
