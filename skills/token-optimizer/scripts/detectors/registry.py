"""Detector registry: session-level waste detectors with run helper."""

from detectors.retry_churn import detect_retry_churn
from detectors.tool_cascade import detect_tool_cascade
from detectors.looping import detect_looping
from detectors.overpowered import detect_overpowered
from detectors.weak_model import detect_weak_model
from detectors.bad_decomposition import detect_bad_decomposition
from detectors.wasteful_thinking import detect_wasteful_thinking

ALL_DETECTORS = [
    {"name": "retry_churn", "fn": detect_retry_churn},
    {"name": "tool_cascade", "fn": detect_tool_cascade},
    {"name": "looping", "fn": detect_looping},
    {"name": "overpowered", "fn": detect_overpowered},
    {"name": "weak_model", "fn": detect_weak_model},
    {"name": "bad_decomposition", "fn": detect_bad_decomposition},
    {"name": "wasteful_thinking", "fn": detect_wasteful_thinking},
]

_TRIAGE_MIN_TOKENS = 5000


def run_all_detectors(session_data):
    """Run all session-level detectors. Returns sorted findings list."""
    findings = []
    for d in ALL_DETECTORS:
        try:
            results = d["fn"](session_data)
            findings.extend(r for r in (results or []) if r.get("confidence", 0) > 0.3)
        except Exception as e:
            import sys
            print(f"[token-optimizer] detector {d['name']} failed: {type(e).__name__}: {e}", file=sys.stderr)
            continue
    findings.sort(key=lambda f: f.get("confidence", 0), reverse=True)
    return findings


def triage(findings):
    """Filter findings to actionable ones."""
    return [f for f in findings if f.get("savings_tokens", 0) > _TRIAGE_MIN_TOKENS]
