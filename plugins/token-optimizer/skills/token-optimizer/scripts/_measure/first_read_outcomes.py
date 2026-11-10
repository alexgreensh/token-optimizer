"""Conservative transcript-only first-read outcome diagnostics.

An observation is usable only when its Read result and, for a map, hook output
are visible and its follow-up window is complete. These signals are proxies,
not proof of causality; absence of an explicit cancellation is not proof that
the task was completed. Project/task labels used for matching stay in memory.
"""
import hashlib
import argparse
import json
import math
import re
from collections import defaultdict

_TEST = re.compile(r"(?:^|[;&|]\s*)(?:python\S*\s+-m\s+pytest|pytest|npm\s+(?:run\s+)?test|cargo\s+test|go\s+test)\b")
_CANCEL = re.compile(r"^\s*(?:cancel (?:this|the task)|stop (?:this|the task)|abandon (?:this|the task))\s*[.!]?\s*$", re.I)


def _hash(value):
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()


def _blocks(row):
    message = row.get("message") or {}
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def inspect(rows, exposure, window=10):
    """Inspect a single exposure without returning source text or paths."""
    if window < 1:
        raise ValueError("window must be positive")
    token = exposure.get("tool_use_id")
    arm = exposure.get("arm")
    out = {"confirmed": False, "complete": False, "failed_test": False, "revert": False,
           "repeated_region": False, "abandoned": False, "harm": False}
    if not token or arm not in ("map", "no_map", "legacy_skeleton"):
        return out
    started = read_ok = delivered = False
    turns, seen_messages, seen_tools = 0, set(), set()
    tests, finished_tests, pending_edits, prior_edit = set(), set(), {}, {}
    unsupported_edit = False
    for row in rows:
        typ = row.get("type")
        blocks = _blocks(row)
        exposure_turn = False
        if not started:
            started = any(isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Read"
                          and b.get("id") and _hash(b["id"]) == token for b in blocks)
            if not started:
                continue
            exposure_turn = True
        elif typ == "assistant":
            msg = row.get("message") or {}
            mid = msg.get("id") or row.get("uuid")
            if mid is None or mid not in seen_messages:
                turns += 1
                if mid is not None:
                    seen_messages.add(mid)
            if turns > window:
                break
        if typ == "attachment":
            a = row.get("attachment") or {}
            if a.get("toolUseID") and _hash(a["toolUseID"]) == token:
                content = str(a.get("content") or "") + str(a.get("stdout") or "")
                if arm == "legacy_skeleton":
                    delivered |= "large-file first read served as" in content
                else:
                    delivered |= "[Token Optimizer] Periphery skeletons" in content
        if typ == "user":
            content = (row.get("message") or {}).get("content")
            if isinstance(content, str) and _CANCEL.fullmatch(content):
                out["abandoned"] = True
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                tid = block.get("tool_use_id")
                if tid and _hash(tid) == token:
                    if arm == "legacy_skeleton":
                        read_ok |= "large-file first read served as" in str(block.get("content") or "")
                    else:
                        read_ok |= block.get("is_error") is False
                tool_result = row.get("toolUseResult") or {}
                exit_code = tool_result.get("exitCode") if isinstance(tool_result, dict) else None
                if tid in tests and (isinstance(block.get("is_error"), bool) or isinstance(exit_code, int)):
                    finished_tests.add(tid)
                    if block.get("is_error") is True or (isinstance(exit_code, int) and exit_code != 0):
                        out["failed_test"] = True
                if tid in pending_edits and isinstance(block.get("is_error"), bool):
                    path, old_id, new_id = pending_edits.pop(tid)
                    if block["is_error"] is False:
                        previous = prior_edit.get(path)
                        if previous and old_id == previous[1]:
                            out["repeated_region"] = True
                            if new_id == previous[0]:
                                out["revert"] = True
                        prior_edit[path] = (old_id, new_id)
            if exposure_turn or block.get("type") != "tool_use" or not block.get("id"):
                continue
            tid = block["id"]
            if tid in seen_tools:
                continue
            seen_tools.add(tid)
            name, args = block.get("name"), block.get("input") or {}
            if not isinstance(args, dict):
                continue
            if name == "Bash" and isinstance(args.get("command"), str) and _TEST.search(args["command"]):
                tests.add(tid)
            if name in ("Write", "MultiEdit") or (name == "Edit" and args.get("replace_all")):
                unsupported_edit = True
                continue
            if name != "Edit":
                continue
            if not args.get("file_path"):
                unsupported_edit = True
                continue
            old, new = args.get("old_string"), args.get("new_string")
            if not isinstance(old, str) or not old or not isinstance(new, str):
                unsupported_edit = True
                continue
            pending_edits[tid] = (_hash(args["file_path"]), _hash(old), _hash(new))
    out["confirmed"] = read_ok and (arm == "no_map" or delivered)
    out["complete"] = ((turns >= window or out["abandoned"]) and tests <= finished_tests
                       and not pending_edits and not unsupported_edit)
    out["harm"] = any(out[k] for k in ("failed_test", "revert", "repeated_region", "abandoned"))
    return out


def _wilson(k, n):
    if not n:
        return None
    z = 1.959963984540054
    p = k / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    radius = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denominator
    return [max(0.0, center - radius), min(1.0, center + radius)]


def compare(observations):
    """Match independent sessions within exact cohort, project and task labels."""
    groups = defaultdict(lambda: {"map": [], "no_map": []})
    seen_sessions = set()
    for row in observations:
        if not row.get("confirmed") or not row.get("complete") or row.get("arm") not in ("map", "no_map"):
            continue
        if not all(row.get(k) for k in ("cohort", "project", "task_type", "session_id", "tool_use_id")):
            continue
        session_key = (row["arm"], row["session_id"])
        if session_key in seen_sessions:
            continue
        seen_sessions.add(session_key)
        groups[(row["cohort"], row["project"], row["task_type"])][row["arm"]].append(
            (row["session_id"], bool(row.get("harm"))))
    pairs = defaultdict(list)
    used_sessions = set()
    for (cohort, _, _), arms in groups.items():
        available = list(arms["no_map"])
        for session, harm in arms["map"]:
            if session in used_sessions:
                continue
            match = next((i for i, (other, _) in enumerate(available)
                          if other != session and other not in used_sessions), None)
            if match is not None:
                other, control_harm = available.pop(match)
                used_sessions.update((session, other))
                pairs[cohort].append((harm, control_harm))
    result = {}
    for cohort, values in pairs.items():
        if not values:
            continue
        n = len(values)
        on = sum(a for a, _ in values)
        off = sum(b for _, b in values)
        difference = (on - off) / n
        radius = math.sqrt(2 * math.log(40) / n)
        result[cohort] = {"pairs": n, "map_harm": on, "control_harm": off,
                          "map_rate": on / n, "control_rate": off / n,
                          "map_interval_95": _wilson(on, n), "control_interval_95": _wilson(off, n),
                          "difference": difference,
                          "difference_interval_95": [max(-1.0, difference - radius), min(1.0, difference + radius)]}
    return result


def analyse_sessions(sessions, exposures, window=10):
    """Replay explicit transcript records against privacy-preserving exposure IDs.

    Each session supplies session_id, project, task_type and transcript rows.
    Project and task_type are matching labels only and are never returned.
    """
    by_session = defaultdict(list)
    variants = defaultdict(set)
    exposures = list(exposures)
    for e in exposures:
        variants[(e.get("session_id"), e.get("tool_use_id"))].add(
            (e.get("arm"), e.get("language"), e.get("band")))
    seen = set()
    for e in exposures:
        key = (e.get("session_id"), e.get("tool_use_id"), e.get("arm"))
        if not all(key) or key in seen or len(variants[key[:2]]) != 1:
            continue
        seen.add(key)
        by_session[key[0]].append(e)
    observations = []
    seen_sessions = set()
    for s in sessions:
        sid = _hash(s.get("session_id")) if s.get("session_id") else ""
        if not sid or sid in seen_sessions:
            continue
        seen_sessions.add(sid)
        for exposure in by_session.get(sid, []):
            language, band = exposure.get("language"), exposure.get("band")
            if not language or not band:
                continue
            outcome = inspect(s.get("rows") or [], exposure, window=window)
            outcome.update(session_id=sid, tool_use_id=exposure["tool_use_id"], arm=exposure["arm"],
                           cohort=f"{language}|{band}", project=s.get("project"), task_type=s.get("task_type"))
            observations.append(outcome)
    return {"unique_observations": len(observations),
            "unconfirmed": sum(not x["confirmed"] for x in observations),
            "censored": sum(x["confirmed"] and not x["complete"] for x in observations),
            "cohorts": compare(observations)}


def _read_jsonl(path):
    rows = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="JSON array: transcript_path, session_id, project, task_type")
    parser.add_argument("exposures", help="periphery_exposures.jsonl")
    parser.add_argument("--window", type=int, default=10)
    args = parser.parse_args(argv)
    if not 1 <= args.window <= 100:
        parser.error("window must be between 1 and 100")
    with open(args.manifest, encoding="utf-8") as fh:
        entries = json.load(fh)
    sessions = []
    for entry in entries:
        sessions.append({"session_id": entry["session_id"], "project": entry["project"],
                         "task_type": entry["task_type"], "rows": _read_jsonl(entry["transcript_path"])})
    print(json.dumps(analyse_sessions(sessions, _read_jsonl(args.exposures), args.window), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
