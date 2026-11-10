"""Credit each real session once when valuing setup trims.

The same session can sit in more than one project dir (a Total Recall cloud
stub, a copied or restored project folder). Two things keep it from being
credited twice:

  * canonical_sessions(): one session_log row per real session. Copies are
    grouped by session uuid, and rows with no uuid by the first request they
    billed. The most complete copy wins (most API calls, then messages, then
    tokens, then a local-looking row over a proven-foreign one, then the
    newest row), so the project, start time and foreign flag used for scope
    come from one deterministic row instead of whichever row was read last.
    A losing copy is not dropped: it resolves to the winner, so any request
    it alone holds is still credited once, under the winner.

  * request_weight(): usage_request_claims keeps one row per (request, session
    copy). Only the copy that owns the request carries its calls; the other
    copies hold calls = 0. A zero-call claim is a duplicate share and credits
    nothing. Only a legacy row with no call count (NULL) counts as one call.
"""
from __future__ import annotations

import hashlib


def _hash63(text):
    digest = hashlib.blake2b(str(text).encode("utf-8", "replace"), digest_size=8).digest()
    return int.from_bytes(digest, "big") >> 1


def request_weight(calls):
    """Calls to credit for one claim row: 0 for a duplicate share, 1 for a legacy NULL."""
    if calls is None:
        return 1
    return max(0, int(calls))


def _first_requests(conn):
    """owner_hash -> first billed request (earliest day, then request hash)."""
    try:
        rows = conn.execute(
            "SELECT owner_hash, request_hash, day FROM usage_request_claims").fetchall()
    except Exception:
        return {}
    first = {}
    for owner, request, day in rows:
        key = (day or "", request)
        if owner not in first or key < first[owner]:
            first[owner] = key
    return {owner: key[1] for owner, key in first.items()}


def _completeness(row, foreign):
    """Higher is more complete. row = (id, uuid, path, ..., api_calls, messages, tokens)."""
    return (int(row["api_calls"] or 0), int(row["messages"] or 0), int(row["tokens"] or 0),
            not foreign, int(row["id"] or 0))


def canonical_sessions(conn, is_foreign):
    """Rows of session_log with one winner per real session.

    Returns (rows, canon): rows is a list of dicts (id, uuid, path, project,
    metrics, first_day, date, foreign), one per real session; canon maps each
    row's path to its winner's row, so a copy resolves to the session it
    duplicates. is_foreign(uuid, path) is the proven-foreign test.
    """
    out = []
    for r in conn.execute(
            "SELECT id, session_uuid, jsonl_path, project, daily_metrics_json, first_activity_day, date, "
            "api_calls, message_count, COALESCE(input_tokens,0) + COALESCE(output_tokens,0) FROM session_log"):
        out.append({"id": r[0], "uuid": r[1] or "", "path": r[2], "project": r[3], "metrics": r[4],
                    "first_day": r[5], "date": r[6], "api_calls": r[7], "messages": r[8],
                    "tokens": r[9], "foreign": bool(is_foreign(r[1], r[2]))})
    first = None
    groups = {}
    for row in out:
        if row["uuid"]:
            key = ("uuid", row["uuid"])
        else:
            if first is None:
                first = _first_requests(conn)
            request = first.get(_hash63(row["path"])) if row["path"] else None
            key = ("request", request) if request is not None else ("path", row["path"], row["id"])
        groups.setdefault(key, []).append(row)
    winners, canon = [], {}
    for rows in groups.values():
        best = max(rows, key=lambda x: _completeness(x, x["foreign"]))
        winners.append(best)
        for row in rows:
            canon[row["path"]] = best
    return winners, canon
