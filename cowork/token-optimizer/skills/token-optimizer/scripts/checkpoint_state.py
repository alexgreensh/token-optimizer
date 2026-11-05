"""Versioned task-state extraction for session checkpoints.

gap-checkpoint (arxiv-2611.05306 findings §3.1 + §4): a checkpoint must carry a
versioned, source-backed picture of WHERE the task stands — objective, open
requirements with status, verified facts vs assumptions, checks with outcomes,
dead ends, the immediate next action and the done condition — not just the
last user/assistant lines cut to 300 chars.

Determinism contract: every field is derived from observable transcript
events (user messages, tool calls, tool results, todo snapshots). Nothing is
inferred semantically; fields with no observable basis are named in
``unknown_fields`` instead of being guessed. All text arriving here is
already credential-redacted by the caller; this module never sees raw
transcript secrets.

The module is stdlib-only and shared by every adapter (Claude JSONL,
Codex rollout, Hermes translated transcripts) so all hosts emit the same
sidecar shape.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

TASK_STATE_VERSION = 2

# Bounds: the sidecar is JSON and the .md renderer applies its own budget, but
# the extractor itself must stay small enough to serialize cheaply on every
# Stop/SessionEnd hook fire.
_MAX_CONSTRAINTS = 12
_MAX_REQUIREMENTS = 20
_MAX_FACTS = 8
_MAX_CHECKS = 15
_MAX_DEAD_ENDS = 8
_MAX_ASSUMPTIONS = 8
_MAX_CHANGED_FILES = 20
_TEXT = 400  # per-item text cap
OBJECTIVE_MAX_CHARS = 1500

# Constraint cues in USER text. Deliberately conservative: a false positive
# costs one extra preserved line, a false negative can drop the only stated
# constraint, so the pattern list errs toward recall.
_CONSTRAINT_RE = re.compile(
    r"\b(?:must(?:n['’]t|\s+not)?|never|always|do(?:es)?\s+not|don['’]t|"
    r"should(?:\s+not|n['’]t)?|cannot|can['’]t|won['’]t|shall|required|"
    r"requirement|constraint|forbidden|avoid|only|no\s+more\s+than|"
    r"at\s+most|exactly|without|must\s+keep|do\s+not\s+dare)\b",
    re.IGNORECASE,
)

# Hedge cues in ASSISTANT text. Assumptions are the complement of verified
# facts: the model saying "probably"/"I assume" is an observable transcript
# signal, so it is extracted (never inferred) and kept distinct from facts.
_ASSUMPTION_RE = re.compile(
    r"\b(?:i\s+assume|assuming|presumably|probably|i\s+think|i\s+believe|"
    r"might|may\s+be|not\s+sure|unclear|appears\s+to|seems\s+to|guess)\b",
    re.IGNORECASE,
)

# A check only counts as a "verified fact" when the tool looks like a
# verification step (test/build/lint run), not any successful command.
_VERIFY_RE = re.compile(
    r"(?:^|[;&|])\s*(?:env\s+)?(?:[A-Za-z_]\w*=\S+\s+)*"
    r"(?:[^\s;|&]*[/\\])?(?:"
    r"python(?:\d+(?:\.\d+)*)?\s+-m\s+(?:pytest|unittest)|"
    r"pytest|py\.test|ctest|rspec|jest|vitest|mypy|eslint|ruff\s+check|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:test|build|lint|check|run\s+(?:test|build|lint|check))|"
    r"(?:npx|pnpm\s+exec|bunx)\s+(?:jest|vitest|eslint|tsc)|"
    r"cargo\s+(?:test|check|build|clippy)|go\s+(?:test|build|vet)|"
    r"make(?:\s+(?:test|check|build|lint|all))?|mvn\s+(?:test|verify|package)|"
    r"gradle\s+(?:test|check|build)|tsc)(?=\s|$)",
    re.IGNORECASE,
)

_FAIL_RE = re.compile(
    r"traceback \(most recent call last\)|\bcommand not found\b|"
    r"\bcannot find\b|\b[1-9]\d*\s+failed\b|\bFAILED\b|\berror:\s|"
    r"\bexit(?:ed)?\s+(?:with\s+)?(?:code\s+)?[1-9]",
    re.IGNORECASE,
)
_PASS_RE = re.compile(
    r"\b\d+\s+passed\b|\b0\s+failed\b|\btest result:\s*ok\b|"
    r"^\s*(?:all (?:checks|tests) passed|BUILD SUCCESS(?:FUL)?|Build succeeded)\b|"
    r"^\s*PASS(?:\s|$)|^ok\s+\S+|^OK\s*$|^Found 0 errors\b",
    re.IGNORECASE | re.MULTILINE,
)

_DONE_STATUSES = {"completed", "done", "complete"}
_IN_PROGRESS_STATUSES = {"in_progress", "in-progress", "in progress"}
_OPEN_STATUSES = _IN_PROGRESS_STATUSES | {"pending", "todo", "open"}


def _clip(text: Any, limit: int = _TEXT) -> str:
    """Whitespace-collapse and cap a transcript string."""
    return " ".join(str(text or "").split())[:limit]


def _src(kind: str, ident: Any = None) -> dict[str, Any]:
    return {"kind": kind, "id": str(ident) if ident is not None else None}


def _norm(text: Any) -> str:
    return " ".join(str(text or "").lower().split())


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def result_excerpt(text: str) -> str:
    """Bound already-redacted output while retaining runner summary tails."""
    if len(text) <= 2000:
        return text
    return text[:500] + "\n…\n" + text[-1500:]


def _check_outcome(is_error: Any, exit_code: Any, text: str) -> tuple[str, bool]:
    """(outcome, explicit_success) for one tool call/result pair.

    ``explicit_success`` requires a pass marker in the summary tail. Exit 0
    completes a check but alone cannot make its target a verified fact.
    """
    if is_error or (exit_code is not None and exit_code != 0):
        return "failed", False
    t = (text or "")[-1500:]
    fail_hits = len(_FAIL_RE.findall(t))
    pass_hits = len(_PASS_RE.findall(t))
    if fail_hits and fail_hits >= pass_hits:
        return "failed", False
    if pass_hits or exit_code == 0:
        return "completed", bool(pass_hits)
    return "unknown", False


def _looks_like_verification(tool: str, target: str) -> bool:
    return bool(_VERIFY_RE.search(target))


def build_task_state(
    *,
    user_texts: Iterable[tuple[str, str | None]] = (),
    assistant_texts: Iterable[tuple[str, str | None]] = (),
    tool_calls: Iterable[dict[str, Any]] = (),
    tool_results: Iterable[dict[str, Any]] = (),
    todos: Iterable[tuple[str, str]] = (),
    changed_files: Iterable[tuple[str, str]] = (),
    archive_pointers: Iterable[str] = (),
    objective: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the versioned task_state dict (sidecar schema v2).

    All inputs are already-redacted, normalized observations:

    - ``user_texts`` / ``assistant_texts``: ``(text, source_id)`` pairs in
      transcript order.
    - ``tool_calls``: ``{"id", "name", "target"}`` in transcript order.
    - ``tool_results``: ``{"tool_use_id", "is_error", "exit_code", "text"}``.
    - ``todos``: ``(content, status)`` latest snapshot.
    - ``changed_files``: ``(path, op)`` file-mutating tool observations.
    - ``objective``: ``{"text", "source"}`` from the caller's head-scan of the
      FIRST user message; ``None`` when the transcript has none.
    """
    user_texts = [(str(t).strip(), sid) for t, sid in user_texts if t and str(t).strip()]
    assistant_texts = [(_clip(t, 500), sid) for t, sid in assistant_texts if _clip(t, 500)]
    tool_calls = [c for c in tool_calls if isinstance(c, dict) and c.get("id")]
    tool_results = [r for r in tool_results if isinstance(r, dict)]
    todos = [( _clip(c, 160), str(s or "").strip().lower()) for c, s in todos if _clip(c, 160)]

    # --- objective: the FIRST user message (head-scan), never the last "continue".
    if objective and objective.get("text"):
        objective_obj = {
            "text": _clip(objective["text"], OBJECTIVE_MAX_CHARS),
            "source": objective.get("source") or _src("user_message"),
        }
    elif user_texts:
        # Fallback: first user text observed in the extraction window. For
        # sessions within the tail window this IS the first message.
        objective_obj = {"text": _clip(user_texts[0][0], OBJECTIVE_MAX_CHARS),
                         "source": _src("user_message", user_texts[0][1])}
    else:
        objective_obj = None

    # --- user constraints: pattern-extracted candidates from user messages.
    constraints: list[dict[str, Any]] = []
    seen_constraints: set[str] = set()
    constraint_inputs = [
        (text, _src("user_message", sid)) for text, sid in user_texts]
    if objective and objective.get("text"):
        constraint_inputs.insert(0, (str(objective["text"]), objective_obj["source"]))
    for text, source in constraint_inputs:
        for sent in _sentences(text):
            sent = " ".join(sent.split())
            cue = _CONSTRAINT_RE.search(sent)
            if not cue:
                continue
            # A cue beyond the sentence cap must not disappear behind its
            # background prefix. Preserve nearby context and mark the excerpt.
            start = max(0, cue.start() - 60) if cue.end() > 300 else 0
            clip = _clip(("… " if start else "") + sent[start:], 300)
            key = _norm(clip)
            if key in seen_constraints:
                continue
            seen_constraints.add(key)
            constraints.append({"text": clip, "source": source})
            if len(constraints) >= _MAX_CONSTRAINTS:
                break
        if len(constraints) >= _MAX_CONSTRAINTS:
            break

    # --- requirements: latest todo snapshot, with explicit status.
    requirements = [
        {"text": text, "status": status or "unknown",
         "source": _src("todo_snapshot")}
        for text, status in todos[:_MAX_REQUIREMENTS]
    ]

    # --- checks: pair each tool call with its result, in transcript order.
    results_by_id: dict[str, dict[str, Any]] = {}
    for r in tool_results:
        rid = str(r.get("tool_use_id") or "")
        if not rid:
            continue
        previous = results_by_id.get(rid)
        if previous is None:
            results_by_id[rid] = dict(r)
        elif r.get("exit_code") is not None:
            results_by_id[rid] = dict(r, text=r.get("text") or previous.get("text") or "")
        elif previous.get("exit_code") is None:
            results_by_id[rid] = dict(r, is_error=bool(r.get("is_error") or previous.get("is_error")),
                                     text=r.get("text") or previous.get("text") or "")
        elif not previous.get("text"):
            previous["text"] = r.get("text") or ""

    checks: list[dict[str, Any]] = []
    succeeded_targets: set[str] = set()
    for call in tool_calls[-40:]:
        cid = str(call.get("id"))
        res = results_by_id.get(cid, {})
        outcome, explicit = _check_outcome(
            res.get("is_error"), res.get("exit_code"), str(res.get("text") or ""))
        target = _clip(call.get("target"), 200)
        checks.append({
            "tool": _clip(call.get("name"), 60),
            "target": target,
            "outcome": outcome,
            "explicit_success": explicit,
            "source_id": cid,
            "source": _src("tool_result", cid),
        })
        if outcome == "completed":
            succeeded_targets.add(_norm(f"{call.get('name')} {target}"))
            succeeded_targets.add(_norm(target))
    checks = checks[-_MAX_CHECKS:]

    # --- verified facts: only explicit successes on verification-looking tools.
    verified_facts: list[dict[str, Any]] = []
    seen_facts: set[str] = set()
    for chk in checks:
        if chk["outcome"] != "completed" or not chk["explicit_success"]:
            continue
        if not _looks_like_verification(chk["tool"], chk["target"]):
            continue
        label = chk["target"] or chk["tool"]
        text = _clip(f"{chk['tool']}: {label} — passed", 300)
        key = _norm(text)
        if key in seen_facts:
            continue
        seen_facts.add(key)
        verified_facts.append({"text": text, "source": chk["source"]})
        if len(verified_facts) >= _MAX_FACTS:
            break

    # --- dead ends: failed checks never later superseded by a success on the
    # same target. Read frequency is irrelevant here — a hot dead end is
    # still a dead end.
    dead_ends: list[dict[str, Any]] = []
    seen_dead: set[str] = set()
    for chk in checks:
        if chk["outcome"] != "failed":
            continue
        tnorm = _norm(f"{chk['tool']} {chk['target']}")
        tnorm_target = _norm(chk["target"])
        if tnorm in succeeded_targets or (tnorm_target and tnorm_target in succeeded_targets):
            continue  # the approach eventually worked — not a dead end
        text = _clip(f"failed: {chk['tool']} {chk['target']}", 300)
        key = _norm(text)
        if key in seen_dead:
            continue
        seen_dead.add(key)
        dead_ends.append({
            "text": text,
            "tool": chk["tool"],
            "target": chk["target"],
            "source": chk["source"],
        })
        if len(dead_ends) >= _MAX_DEAD_ENDS:
            break

    # --- assumptions: hedged statements observable in assistant text.
    assumptions: list[dict[str, Any]] = []
    seen_assume: set[str] = set()
    for text, sid in assistant_texts:
        for sent in _sentences(text):
            if not _ASSUMPTION_RE.search(sent):
                continue
            clip = _clip(sent, 300)
            key = _norm(clip)
            if key in seen_assume:
                continue
            seen_assume.add(key)
            assumptions.append({"text": clip, "source": _src("assistant_message", sid)})
            if len(assumptions) >= _MAX_ASSUMPTIONS:
                break
        if len(assumptions) >= _MAX_ASSUMPTIONS:
            break

    # --- changed files: deterministic mutation observations only.
    changed = [
        {"path": _clip(p, 400), "op": _clip(op, 40) or "modified",
         "source": _src("tool_use")}
        for p, op in list(changed_files)[:_MAX_CHANGED_FILES]
        if _clip(p, 400)
    ]

    # --- next action: first open requirement (in_progress beats pending),
    # else the last assistant statement (marked as such — a claim, not state).
    next_action = None
    for wanted in (_IN_PROGRESS_STATUSES, _OPEN_STATUSES - _IN_PROGRESS_STATUSES):
        for req in requirements:
            if str(req.get("status") or "").lower() in wanted:
                next_action = {
                    "text": _clip(req["text"], 300),
                    "basis": "requirements",
                    "source": req["source"],
                }
                break
        if next_action:
            break
    if next_action is None and assistant_texts:
        last_text, last_sid = assistant_texts[-1]
        next_action = {
            "text": _clip(last_text, 300),
            "basis": "assistant_statement",
            "source": _src("assistant_message", last_sid),
        }

    # --- done condition: only when EVERY tracked requirement reports done.
    done_condition = None
    if todos and all(s in _DONE_STATUSES for _, s in todos):
        done_condition = {
            "text": f"all {len(todos)} tracked requirements marked completed",
            "basis": "todo_snapshot",
            "source": _src("todo_snapshot"),
        }

    # --- unknowns: named, never guessed.
    unknown_fields: list[str] = []
    if not objective_obj:
        unknown_fields.append("objective")
    if not constraint_inputs:
        unknown_fields.append("user_constraints")
    if not todos:
        unknown_fields.append("requirements")
    if not next_action:
        unknown_fields.append("next_action")
    if not done_condition:
        unknown_fields.append("done_condition")
    if not assumptions:
        unknown_fields.append("assumptions")

    return {
        "version": TASK_STATE_VERSION,
        "objective": objective_obj,
        "user_constraints": constraints,
        "requirements": requirements,
        "verified_facts": verified_facts,
        "assumptions": assumptions,
        "changed_files": changed,
        "checks": checks,
        "dead_ends": dead_ends,
        "next_action": next_action,
        "done_condition": done_condition,
        "archive_pointers": [_clip(p, 300) for p in archive_pointers if _clip(p, 300)][:20],
        "unknown_fields": unknown_fields,
    }
