"""Select a verifiable local-session maximum without promoting tiny sessions."""


def verified_best(conn, facade, ranked):
    if facade is None or not ranked:
        return None
    ordered = sorted(ranked, reverse=True)
    filtered = False
    for share, session_id in ordered:
        try:
            path = facade._counted_session_path(conn, session_id)
            if path is None:
                return None
            turns, _, _ = facade._counted_walk_transcript(path)
            peak = max((turn[3][0] + turn[3][3] for turn in turns
                        if len(turn) > 3 and turn[3]), default=0)
        except (OSError, TypeError, ValueError, IndexError):
            return None
        if not filtered and (len(turns) < 20 or peak < 50000):
            filtered = True
        if not filtered or len(turns) >= 20:
            return {'net_max': share, 'filtered': filtered,
                    'turns': len(turns), 'context_tokens': peak}
    return None
