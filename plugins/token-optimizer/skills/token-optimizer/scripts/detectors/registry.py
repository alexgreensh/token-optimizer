"""Detector registry: session and config detectors with run helper."""

from detectors.retry_churn import detect_retry_churn
from detectors.tool_cascade import detect_tool_cascade
from detectors.looping import detect_looping
from detectors.overpowered import detect_overpowered
from detectors.weak_model import detect_weak_model
from detectors.bad_decomposition import detect_bad_decomposition
from detectors.wasteful_thinking import detect_wasteful_thinking
from detectors.output_waste import detect_output_waste
from detectors.cache_instability import detect_cache_instability
from detectors.respond_to_bash import detect_respond_to_bash

ALL_DETECTORS = [
    {"name": "respond_to_bash_commands", "fn": detect_respond_to_bash},
    {"name": "retry_churn", "fn": detect_retry_churn},
    {"name": "tool_cascade", "fn": detect_tool_cascade},
    {"name": "looping", "fn": detect_looping},
    {"name": "overpowered", "fn": detect_overpowered},
    {"name": "weak_model", "fn": detect_weak_model},
    {"name": "bad_decomposition", "fn": detect_bad_decomposition},
    {"name": "wasteful_thinking", "fn": detect_wasteful_thinking},
    {"name": "output_waste", "fn": detect_output_waste},
    {"name": "cache_instability", "fn": detect_cache_instability},
]

_TRIAGE_MIN_TOKENS = 5000


def run_all_detectors(session_data):
    """Run all session and config detectors. Returns sorted findings list."""
    findings = []
    for d in ALL_DETECTORS:
        try:
            results = d["fn"](session_data)
            for r in (results or []):
                if r.get("always_show") or r.get("confidence", 0) > 0.3:
                    findings.append(r)
        except Exception as e:
            import sys
            print(f"[token-optimizer] detector {d['name']} failed: {type(e).__name__}: {e}", file=sys.stderr)
            continue
    findings.sort(key=lambda f: f.get("confidence", 0), reverse=True)
    # Deduplicate config-check findings (always_show=True) only.
    # Session-data detectors may emit multiple distinct findings with the same name.
    seen_config: set = set()
    deduped = []
    for f in findings:
        if f.get("always_show"):
            name = f.get("name")
            if name not in seen_config:
                seen_config.add(name)
                deduped.append(f)
        else:
            deduped.append(f)
    return deduped


def triage(findings):
    """Filter findings to actionable ones."""
    return [
        f for f in findings
        if f.get("always_show") or f.get("savings_tokens", 0) > _TRIAGE_MIN_TOKENS
    ]
