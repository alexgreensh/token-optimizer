"""Bounded, evidence-weighted prior for continuity across compactions."""

RHO = 0.90
FLOOR = 0.45


def continuity(count, events):
    """Return the continuity multiplier and effective compaction count."""
    count = max(0, int(count or 0))
    events = [event for event in events if isinstance(event, dict)][:count]
    effective = float(count - len(events))
    for event in events:
        pre, post = event.get('pre_tokens'), event.get('post_tokens')
        if isinstance(pre, (int, float)) and isinstance(post, (int, float)):
            size = max(.25, min(1., (pre - post) / 150000.))
        else:
            size = 1.
        restored = event.get('restore_evidence') in ('both', 'transcript', 'db')
        status = event.get('status')
        if status == 'needs_attention' or (event.get('capture_notice') and event.get('restore_evidence') == 'none'):
            evidence = 1.5
        elif status == 'no_concerning_signal_observed':
            evidence = .5 if restored else .75
        else:
            evidence = .75 if restored else 1.
        effective += size * evidence
    return FLOOR + (1 - FLOOR) * RHO ** effective, effective


def session_quality(context_tokens=None, context_window=None, model=None, compactions=0):
    """Adapt a runtime's observed current occupancy to the common scorer."""
    import measure
    measured = isinstance(context_tokens, (int, float)) and not isinstance(context_tokens, bool) and context_tokens > 0
    data = {
        'messages': [], 'tool_results': [], 'tool_result_meta': [],
        'system_reminders': [], 'reads': [], 'writes': [], 'compactions': compactions,
        'tool_calls': 0, 'agent_dispatches': [], 'decisions': [], 'total_entries': 0,
        'context_tokens': context_tokens if measured else None, 'model': model,
        'model_context_window': context_window, 'unmeasured': not measured,
    }
    result = measure.compute_quality_score(data)
    result['band'] = measure.score_to_band(result['score']) if measured else 'Unmeasured'
    return result
