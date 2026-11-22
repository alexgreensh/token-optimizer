#!/usr/bin/env python3
"""Structured test-output compressors for Go, Cargo, and Node (`node --test`).

Stdlib-only helper called by ``bash_compress._compress_pytest`` for test
runners whose verbose output formats do not match pytest's ``N passed`` /
``=== FAILURES ===`` layout:

  - ``go test -v ./...`` (``=== RUN``, ``--- PASS:``, ``--- SKIP:``,
    ``--- FAIL:``, ``ok  \\tpkg\\t0.1s``, ``FAIL\\tpkg\\t0.1s``)
  - ``cargo test`` across single-crate and multi-crate workspaces
    (``running N tests``, ``test name ... ok/ignored/FAILED``,
    ``failures:``, ``test result: ...``)
  - ``node --test`` TAP and spec output (``ok N -`` / ``not ok N -``,
    ``✔`` / ``✖``, ``# tests`` / ``ℹ tests`` summary lines)

Safety invariants:
  - Never touches short outputs (< 15 lines).
  - Never drops failing test names, assertion details, panics, or package/suite
    summary lines.
  - Returns ``None`` when the output does not match a known format or does not
    shrink, so the caller falls through cleanly.
"""
from __future__ import annotations

import re

_MIN_LINES = 15

# --- Go test patterns ---
_GO_RUN_RE = re.compile(r"^\s*=== (?:RUN|PAUSE|CONT)\s+")
_GO_PASS_RE = re.compile(r"^\s*--- PASS:\s+")
_GO_SKIP_RE = re.compile(r"^\s*--- SKIP:\s+")
_GO_FAIL_RE = re.compile(r"^\s*--- FAIL:\s+")
_GO_PKG_OK_RE = re.compile(r"^ok\s+\S+\t")
_GO_PKG_FAIL_RE = re.compile(r"^FAIL\t\S+")
_GO_PKG_NOTEST_RE = re.compile(r"^\?\s+\S+\t\[no test files\]")
_GO_TEST_FILE_LINE_RE = re.compile(r"^\s+\S+_test\.go:\d+:")
_GO_PANIC_RE = re.compile(r"^(?:panic:|fatal error:|goroutine \d+)")

# --- Cargo test patterns ---
_CARGO_RUNNING_RE = re.compile(r"^running \d+ tests?\s*$")
_CARGO_TEST_OK_RE = re.compile(r"^test \S+ \.\.\. ok\s*$")
_CARGO_TEST_IGNORED_RE = re.compile(r"^test \S+ \.\.\. ignored\s*$")
_CARGO_TEST_FAILED_RE = re.compile(r"^test \S+ \.\.\. FAILED\s*$")
_CARGO_RESULT_RE = re.compile(r"^test result:\s+")
_CARGO_DOC_HEADER_RE = re.compile(r"^\s*Doc-tests\s+\S+")
_CARGO_RUNNING_BIN_RE = re.compile(r"^\s*Running\s+(?:unittests|tests/|`?target)")

# --- Node --test patterns ---
_NODE_TAP_OK_RE = re.compile(r"^\s*ok \d+\s+-\s+")
_NODE_TAP_FAIL_RE = re.compile(r"^\s*not ok \d+\s+-\s+")
_NODE_SPEC_OK_RE = re.compile(r"^\s*[✔✓]\s+")
_NODE_SPEC_FAIL_RE = re.compile(r"^\s*[✖✗]\s+")
_NODE_SUMMARY_RE = re.compile(
    r"^(?:#|ℹ)\s+(?:tests|suites|pass|fail|cancelled|skipped|todo|duration_ms)\s+\S+"
)


def compress_go_test(output: str) -> str | None:
    """Compress verbose ``go test`` output while keeping all failures and summaries."""
    lines = output.strip().splitlines()
    if len(lines) < _MIN_LINES:
        return None

    has_go_marker = any(
        _GO_RUN_RE.match(ln)
        or _GO_PASS_RE.match(ln)
        or _GO_FAIL_RE.match(ln)
        or _GO_PKG_OK_RE.match(ln)
        or _GO_PKG_FAIL_RE.match(ln)
        for ln in lines
    )
    if not has_go_marker:
        return None

    passed = 0
    skipped = 0
    failed = 0
    pkg_summaries: list[str] = []
    failure_lines: list[str] = []

    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        if _GO_PASS_RE.match(ln):
            passed += 1
            i += 1
            continue
        if _GO_SKIP_RE.match(ln):
            skipped += 1
            i += 1
            continue
        if _GO_FAIL_RE.match(ln):
            failed += 1
            failure_lines.append(ln)
            i += 1
            # Capture indented diagnostic lines immediately following --- FAIL:
            while i < n and (
                lines[i].startswith((" ", "\t")) or _GO_TEST_FILE_LINE_RE.match(lines[i])
            ):
                failure_lines.append(lines[i])
                i += 1
            continue
        if _GO_TEST_FILE_LINE_RE.match(ln) or _GO_PANIC_RE.match(ln):
            # Go prints failure diagnostics BEFORE `--- FAIL:` inside a test run.
            failure_lines.append(ln)
            i += 1
            while i < n and lines[i].startswith((" ", "\t")):
                failure_lines.append(lines[i])
                i += 1
            continue
        if _GO_PKG_OK_RE.match(ln) or _GO_PKG_FAIL_RE.match(ln) or _GO_PKG_NOTEST_RE.match(ln):
            pkg_summaries.append(ln)
            i += 1
            continue
        if _GO_RUN_RE.match(ln) or ln.strip() in ("PASS", "FAIL"):
            i += 1
            continue
        i += 1

    if passed == 0 and skipped == 0 and failed == 0 and not pkg_summaries:
        return None

    header_parts = []
    if passed:
        header_parts.append(f"{passed} passed")
    if failed:
        header_parts.append(f"{failed} failed")
    if skipped:
        header_parts.append(f"{skipped} skipped")

    out_parts: list[str] = []
    if header_parts:
        out_parts.append(f"go test: {', '.join(header_parts)}")
    if failure_lines:
        out_parts.append("\n".join(failure_lines))
    if pkg_summaries:
        out_parts.append("\n".join(pkg_summaries))

    result = "\n\n".join(out_parts)
    if not result or len(result) >= len(output):
        return None
    return result


def compress_cargo_test(output: str) -> str | None:
    """Compress verbose ``cargo test`` output across single or multi-crate runs."""
    lines = output.strip().splitlines()
    if len(lines) < _MIN_LINES:
        return None

    has_cargo_marker = any(_CARGO_RESULT_RE.match(ln) for ln in lines) and any(
        _CARGO_RUNNING_RE.match(ln)
        or _CARGO_TEST_OK_RE.match(ln)
        or _CARGO_TEST_FAILED_RE.match(ln)
        for ln in lines
    )
    if not has_cargo_marker:
        return None

    ok_count = 0
    ignored_count = 0
    failed_tests: list[str] = []
    result_lines: list[str] = []
    failure_blocks: list[str] = []

    in_failures = False
    current_suite = ""
    for ln in lines:
        if _CARGO_RUNNING_BIN_RE.match(ln) or _CARGO_DOC_HEADER_RE.match(ln):
            current_suite = ln.strip()
            in_failures = False
            continue
        if _CARGO_TEST_OK_RE.match(ln):
            ok_count += 1
            continue
        if _CARGO_TEST_IGNORED_RE.match(ln):
            ignored_count += 1
            continue
        if _CARGO_TEST_FAILED_RE.match(ln):
            failed_tests.append(ln)
            continue
        if ln.strip() == "failures:":
            in_failures = True
            failure_blocks.append(ln)
            continue
        if _CARGO_RESULT_RE.match(ln):
            in_failures = False
            label = f"[{current_suite}] {ln}" if current_suite else ln
            result_lines.append(label)
            continue
        if in_failures:
            failure_blocks.append(ln)

    if not result_lines and not failed_tests:
        return None

    out_parts: list[str] = []
    if ok_count or ignored_count:
        note = f"cargo test: {ok_count} ok"
        if ignored_count:
            note += f", {ignored_count} ignored"
        if failed_tests:
            note += f", {len(failed_tests)} FAILED"
        out_parts.append(note)
    if failed_tests:
        out_parts.append("\n".join(failed_tests))
    if failure_blocks:
        out_parts.append("\n".join(failure_blocks))
    if result_lines:
        out_parts.append("\n".join(result_lines))

    result = "\n\n".join(out_parts)
    if not result or len(result) >= len(output):
        return None
    return result


def compress_node_test(output: str) -> str | None:
    """Compress verbose ``node --test`` TAP or spec output."""
    lines = output.strip().splitlines()
    if len(lines) < _MIN_LINES:
        return None

    summary_lines = [ln for ln in lines if _NODE_SUMMARY_RE.match(ln)]
    if len(summary_lines) < 2:
        return None

    passed = 0
    failed_blocks: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        if _NODE_TAP_OK_RE.match(ln) or _NODE_SPEC_OK_RE.match(ln):
            passed += 1
            i += 1
            continue
        if _NODE_TAP_FAIL_RE.match(ln) or _NODE_SPEC_FAIL_RE.match(ln):
            failed_blocks.append(ln)
            i += 1
            # Keep indented YAML/error details following a failing test
            while i < n and (lines[i].startswith((" ", "\t")) or lines[i].strip() in ("---", "...")):
                failed_blocks.append(lines[i])
                if lines[i].strip() == "...":
                    i += 1
                    break
                i += 1
            continue
        i += 1

    if passed == 0 and not failed_blocks:
        return None

    out_parts: list[str] = []
    if failed_blocks:
        out_parts.append("\n".join(failed_blocks))
    out_parts.append("\n".join(summary_lines))

    result = "\n\n".join(out_parts)
    if not result or len(result) >= len(output):
        return None
    return result


def compress_structured_test_output(output: str) -> str | None:
    """Try Go, Cargo, and Node test compressors in order; return None if unmatched."""
    for fn in (compress_go_test, compress_cargo_test, compress_node_test):
        res = fn(output)
        if res is not None:
            return res
    return None
