"""Shared savings story totals."""

def _number(value):
    try:
        return int(float(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def verbosity_logged(summary):
    return sum(float(row.get('cost_saved_usd') or 0) for name, row in
               (summary.get('by_category') or {}).items()
               if str(name).startswith('verbosity') and isinstance(row, dict))


def excluded_since_install(summary):
    routing_row = summary.get('model_routing') or {}
    lean_row = summary.get('resume_lean_estimated') or {}
    estimate = summary.get('verbosity_steer') or {}
    routing = (float(routing_row['realized_cost_usd']) if routing_row.get('realized_cost_usd') is not None
               and routing_row.get('evidence') not in ('pending', 'unavailable') else None)
    lean = float(lean_row['cost_saved_usd']) if lean_row.get('cost_saved_usd') is not None else None
    verbosity = (verbosity_logged(summary) + float(estimate.get('cost_saved_usd') or 0)
                 if estimate or any(str(k).startswith('verbosity') for k in (summary.get('by_category') or {})) else None)
    return {'routing': routing, 'lean_resumes': lean, 'verbosity': verbosity}


def story_token_total(s):
    """Match the token-bearing rows actually shown in the savings story."""
    a, h = s.get('action_savings') or {}, s.get('token_headline') or {}
    d, o = s.get('structural_detail') or {}, s.get('output_reread') or {}
    ledger = d.get('method') == 'trim_ledger'
    mt = _number(d.get('tokens_saved')) if ledger else 0
    mu = float(d.get('cost_saved_usd') or 0) if ledger else 0
    removed = (_number(h.get('context_removed_tokens')) + _number(h.get('output_not_generated_tokens'))
               if h.get('context_removed_tokens') is not None else
               _number(h.get('removed_tokens')) - _number(h.get('reload_avoided_tokens')))
    total = max(0, removed - mt) if float(a.get('logged_usd') or 0) - mu != 0 else 0
    if a.get('reread_available') is not False and a.get('reread_usd') is not None and float(a.get('reread_usd') or 0) != 0:
        total += _number(h.get('reread_tokens'))
    if ledger and mu > 0:
        total += mt
    estimate = d.get('estimated') or {}
    if ledger and float(estimate.get('cost_saved_usd') or 0) > 0:
        total += _number(estimate.get('tokens_saved'))
    if float(o.get('cost_saved_usd') or 0) > 0:
        total += _number(o.get('tokens_saved'))
    if float(a.get('estimated_usd') or 0) - float((a.get('estimates_usd') or {}).get('output_reread') or 0) >= 0.5:
        total += _number(h.get('reload_avoided_tokens'))
    return total

