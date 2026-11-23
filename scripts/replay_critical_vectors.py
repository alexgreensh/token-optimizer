#!/usr/bin/env python3
"""Independent replay gate critical-line detector (SPEC-local-adapt Part C).

MUST NOT import emit_safety_gate, visibility_rule, large_output_policy, or any
compressor module. Vectors are written strictly from failure taxonomies.
"""
from __future__ import annotations

import random
import re
from typing import Any

# Failure taxonomy vectors (independent of compressor regexes)
TAXONOMY_VECTORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Shell failures
    ("shell", re.compile(
        r"(?:command not found|No such file or directory|Permission denied|"
        r"\bKilled\b|Segmentation fault|core dumped)",
    )),
    # Git failures / conflicts
    ("git", re.compile(
        r"(?:^CONFLICT\s*\(|\s!\s+\[rejected\]|^fatal:)",
    )),
    # Test runner failures & assertion evidence
    ("test", re.compile(
        r"(?:^\s*Expected:|^\s*Received:|\bFAILED\b|^not ok\s+\d+|^---\s+FAIL:|panicked at)",
    )),
    # Build / compiler diagnostics
    ("build", re.compile(
        r"(?:error\[E\d+\]|\bTS\d{3,5}\b|^\s*-->\s*\S+:\d+)",
    )),
    # Infra / orchestrator failures
    ("infra", re.compile(
        r"(?:OOMKilled|CrashLoopBackOff|exceeded its progress deadline|forces replacement)",
    )),
    # Runtime fatal / traceback / non-zero exit text
    ("runtime", re.compile(
        r"(?:\bFATAL\b|Traceback \(most recent call last\):|^\s*File \"[^\"]+\", line \d+|exit code [1-9]\d*)",
    )),
)

_DECL_MARKER_RE = re.compile(
    r"(?:\.\.\.|omitted|more lines|more matches|more rows|more entries|archived|hidden|folded|expand [0-9a-f]{8,})",
    re.IGNORECASE,
)


def find_critical_lines(raw_text: str, exit_code: int = 0) -> list[tuple[int, str, str]]:
    """Return list of (0-based line index, line text, taxonomy_class)."""
    hits: list[tuple[int, str, str]] = []
    if not raw_text:
        return hits
    lines = raw_text.splitlines()
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        for cls, pat in TAXONOMY_VECTORS:
            if pat.search(line):
                hits.append((idx, line, cls))
                break
    return hits


def _visible_line_set(text: str) -> set[str]:
    return {ln.strip() for ln in (text or "").splitlines() if ln.strip()}


def check_event(raw_text: str, emitted_text: str, exit_code: int = 0) -> dict[str, Any]:
    """Check one (raw, emitted, exit_code) event for CRITICAL_HIDDEN,
    EVIDENCE_HIDDEN, and UNDECLARED_HIDDEN.
    """
    raw_lines = (raw_text or "").splitlines()
    emitted_lines = (emitted_text or "").splitlines()
    if emitted_text == raw_text or not raw_lines:
        return {
            "CRITICAL_HIDDEN": 0,
            "EVIDENCE_HIDDEN": 0,
            "UNDECLARED_HIDDEN": 0,
            "critical_details": [],
            "evidence_details": [],
        }

    vis = _visible_line_set(emitted_text)
    crit_hits = find_critical_lines(raw_text, exit_code=exit_code)
    critical_hidden: list[str] = []
    evidence_hidden: list[str] = []

    if exit_code != 0 and len(emitted_lines) < len(raw_lines):
        # Non-zero exit must remain explicitly visible if compressed
        if not re.search(rf"\bexit(?:\s+code|\s+status)?\s*[:=]?\s*{exit_code}\b", emitted_text, re.IGNORECASE):
            critical_hidden.append(f"exit_code={exit_code}")

    for idx, line, cls in crit_hits:
        if line.strip() not in vis:
            critical_hidden.append(f"L{idx + 1}:{cls}")
            continue
        # Context check: two non-empty lines before and after each kept critical line
        # must remain visible, or the event is EVIDENCE_HIDDEN.
        for offset in (-2, -1, 1, 2):
            ni = idx + offset
            if 0 <= ni < len(raw_lines):
                ctx = raw_lines[ni].strip()
                if ctx and ctx not in vis:
                    evidence_hidden.append(f"L{idx + 1}+({offset}):L{ni + 1}")

    undeclared = 0
    dropped = sum(1 for ln in raw_lines if ln.strip() and ln.strip() not in vis)
    if dropped > 0 and not _DECL_MARKER_RE.search(emitted_text):
        undeclared = dropped

    return {
        "CRITICAL_HIDDEN": len(critical_hidden),
        "EVIDENCE_HIDDEN": len(evidence_hidden),
        "UNDECLARED_HIDDEN": undeclared,
        "critical_details": critical_hidden,
        "evidence_details": evidence_hidden,
    }


_SEEDED_FAULTS = (
    "bash: line 3: jqq: command not found",
    "cat: config/prod.yaml: No such file or directory",
    "open /etc/shadow: Permission denied",
    "Killed",
    "Segmentation fault (core dumped)",
    "CONFLICT (content): Merge conflict in src/app.py",
    " ! [rejected]        main -> main (non-fast-forward)",
    "fatal: not a git repository",
    "  Expected: 42",
    "  Received: 41",
    "FAILED tests/test_auth.py::test_token_expiry",
    "not ok 14 - should reject expired jwt",
    "--- FAIL: TestRouterDispatch (0.01s)",
    "thread 'main' panicked at src/main.rs:42:9:",
    "error[E0382]: borrow of moved value: `cfg`",
    "src/index.ts(18,5): error TS2339: Property 'foo' does not exist",
    "  --> src/lib.rs:57:18",
    "pod/api-7f9b OOMKilled",
    "pod/worker-2 CrashLoopBackOff",
    "deployment \"web\" exceeded its progress deadline",
    "  # aws_instance.web must be replaced (forces replacement)",
    "logs/app.log:222: FATAL database down: exit code 137",
)


def seeded_fault_self_test(seed: int = 42, count: int = 50) -> dict[str, Any]:
    """Inject *count* synthetic outputs each with one planted critical line at a
    random position, run a deliberately broken compressor that hides it, and
    require the gate to detect count/count. Raises RuntimeError if < count.
    """
    rng = random.Random(seed)
    detected = 0
    for i in range(count):
        n_lines = rng.randint(40, 120)
        pos = rng.randint(5, n_lines - 6)
        fault = _SEEDED_FAULTS[i % len(_SEEDED_FAULTS)]
        lines = [f"info line {k}: ok status=200" for k in range(n_lines)]
        lines[pos] = fault
        raw = "\n".join(lines)
        # Deliberately broken compressor: drops the middle including `pos`
        broken = "\n".join(lines[:3] + [f"... {n_lines - 6} lines omitted ..."] + lines[-3:])
        res = check_event(raw, broken, exit_code=0)
        if res["CRITICAL_HIDDEN"] > 0:
            detected += 1
    out = {"detected": detected, "total": count, "passed": detected == count}
    if detected < count:
        raise RuntimeError(f"Seeded-fault self-test failed: {detected}/{count} detected")
    return out


def dedupe_replay_rows(rows: list[dict[str, Any]], cutoff_iso: str | None = None) -> list[dict[str, Any]]:
    """Deduplicate replay rows by message id and tool_use_id, windowed by row timestamp."""
    seen_msg: set[str] = set()
    seen_tool: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = row.get("timestamp") or ""
        if cutoff_iso and (not ts or ts < cutoff_iso):
            continue
        mid = row.get("message_id") or (row.get("message") or {}).get("id")
        tid = row.get("tool_use_id")
        if mid:
            if mid in seen_msg:
                continue
            seen_msg.add(mid)
        if tid:
            if tid in seen_tool:
                continue
            seen_tool.add(tid)
        out.append(row)
    return out


if __name__ == "__main__":
    res = seeded_fault_self_test()
    print(f"seeded-fault self-test: {res['detected']}/{res['total']} PASS")
