"""Keep the newest and newest usable checkpoint for every session."""


def exclude_latest(prune, checkpoints, session_id, is_restorable):
    if not prune:
        return prune
    candidates = {session_id(cp["filename"]) for cp in prune}
    newest = {}
    for checkpoint in checkpoints:
        sid = session_id(checkpoint["filename"])
        if sid in candidates:
            newest.setdefault(sid, checkpoint)
    keep = {cp["filename"] for cp in newest.values()}
    missing = {sid for sid, cp in newest.items() if not is_restorable(cp["path"])}
    for cp in checkpoints:
        sid = session_id(cp["filename"])
        if sid in missing and is_restorable(cp["path"]):
            keep.add(cp["filename"])
            missing.remove(sid)
    return [cp for cp in prune if cp["filename"] not in keep]
