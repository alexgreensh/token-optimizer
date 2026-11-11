"""Keep the latest checkpoint when its session has recent transcript activity."""
import time


def exclude_active_latest(prune, checkpoints, claude_dir, session_id):
    newest = {}
    for checkpoint in checkpoints:
        newest.setdefault(session_id(checkpoint["filename"]), checkpoint["filename"])
    candidates = {session_id(cp["filename"]) for cp in prune
                  if newest.get(session_id(cp["filename"])) == cp["filename"]}
    if not candidates:
        return prune
    active = set()
    try:
        for project in (claude_dir / "projects").iterdir():
            for sid in candidates - active:
                try:
                    transcript = project / (sid + ".jsonl")
                    if transcript.is_file() and transcript.stat().st_mtime >= time.time() - 48 * 3600:
                        active.add(sid)
                except OSError:
                    continue
    except OSError:
        pass
    return [cp for cp in prune if not (session_id(cp["filename"]) in active and
                                       newest.get(session_id(cp["filename"])) == cp["filename"])]
