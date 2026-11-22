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
  - Drops ONLY known pass noise (passing/skipped test markers and passing
    ``t.Log`` lines). Unrecognised lines, diagnostics, compile errors, panic
    stack frames, ``Example`` ``got:``/``want:``, data-race warnings,
    benchmarks, Cargo aborts/``Caused by:``/``SIGABRT``/``--nocapture``
    output, and Node ``test at file:line`` locations are preserved verbatim.
  - Only sniffs the format of the detected runner when dispatched from a
    command, so pytest output is never misparsed as Go/Cargo/Node.
  - Returns ``None`` when the output does not match a known format or does not
    shrink, so the caller falls through cleanly.
"""
from __future__ import annotations

import re
import shlex

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
_GO_FAILURE_SIGNAL_RE = re.compile(
    r"(?:^panic:|^fatal error:|^goroutine \d+|WARNING:\s*DATA RACE|\[build failed\]|\[setup failed\])"
)

# --- Cargo test patterns ---
_CARGO_RUNNING_RE = re.compile(r"^running \d+ tests?\s*$")
_CARGO_TEST_OK_RE = re.compile(r"^test .+? \.\.\. ok(?:\s+\(compile fail\))?\s*$")
_CARGO_TEST_IGNORED_RE = re.compile(r"^test .+? \.\.\. ignored(?:,\s*.*)?\s*$")
_CARGO_TEST_FAILED_RE = re.compile(r"^test .+? \.\.\. FAILED\s*$")
_CARGO_RESULT_RE = re.compile(r"^test result:\s+")
_CARGO_DOC_HEADER_RE = re.compile(r"^\s*Doc-tests\s+\S+")
_CARGO_RUNNING_BIN_RE = re.compile(r"^\s*Running\s+(?:unittests|tests/|`?target)")
_CARGO_ERROR_SIGNAL_RE = re.compile(
    r"(?:^error(?:\[E\d+\])?:|^Caused by:|panicked at|fatal runtime error:|SIGABRT|SIGSEGV|SIGKILL|test result:\s*FAILED)"
)

# --- Node --test patterns ---
_NODE_TAP_VERSION_RE = re.compile(r"^TAP version \d+\s*$")
_NODE_TAP_PLAN_RE = re.compile(r"^\s*1\.\.\d+\s*$")
_NODE_TAP_SUBTEST_RE = re.compile(r"^\s*# Subtest:\s+")
_NODE_TAP_OK_RE = re.compile(r"^\s*ok \d+\s+-\s+")
_NODE_TAP_FAIL_RE = re.compile(r"^\s*not ok \d+\s+-\s+")
_NODE_SPEC_OK_RE = re.compile(r"^\s*[✔✓]\s+")
_NODE_SPEC_FAIL_RE = re.compile(r"^\s*[✖✗]\s+")
_NODE_SPEC_SUITE_RE = re.compile(r"^\s*▶\s+")
_NODE_SUMMARY_RE = re.compile(
    r"^(?:#|ℹ)\s+(?:tests|suites|pass|fail|cancelled|skipped|todo|duration_ms)\s+\S+"
)
_NODE_YAML_PASS_KEY_RE = re.compile(r"^\s+(?:duration_ms|type):\s+")

_SHELL_SPLIT_TOKENS = frozenset({"|", "&&", "||", ";"})


def is_node_test_args(args: list[str]) -> bool:
    """Return True iff ``--test`` appears in ``node`` options before the first non-option token."""
    for tok in args:
        if tok == "--":
            return False
        if tok == "--test":
            return True
        if not tok.startswith("-"):
            return False
    return False


def detect_runner(command_str: str) -> str | None:
    """Return ``'go'``, ``'cargo'``, or ``'node'`` for supported test commands, else ``None``."""
    if not command_str or not command_str.strip():
        return None
    try:
        tokens = shlex.split(command_str)
    except ValueError:
        return None
    if not tokens:
        return None

    # Inspect the primary command segment before any pipe/operator.
    stage: list[str] = []
    for tok in tokens:
        if tok in _SHELL_SPLIT_TOKENS:
            break
        stage.append(tok)
    if not stage:
        return None

    idx = 0
    if stage[idx] == "env":
        idx += 1
    while idx < len(stage) and "=" in stage[idx] and not stage[idx].startswith("-"):
        idx += 1
    if idx >= len(stage):
        return None

    cmd = stage[idx].rsplit("/", 1)[-1]
    subcmd = stage[idx + 1] if idx + 1 < len(stage) else ""

    if cmd == "go" and subcmd == "test":
        return "go"
    if cmd == "cargo" and subcmd == "test":
        return "cargo"
    if cmd == "node" and is_node_test_args(stage[idx + 1:]):
        return "node"
    return None


def compress_go_test(output: str) -> str | None:
    """Compress verbose ``go test`` output while keeping all non-pass lines and summaries."""
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
    kept_lines: list[str] = []

    # Buffer `=== RUN` and indented `*_test.go:line:` t.Log lines until we see
    # whether the test passes/skips (drop its t.Log) or fails/panics (keep all).
    pending_run: str | None = None
    pending_logs: list[str] = []
    in_test_log = False

    def _flush_pending(include_run: bool = True) -> None:
        nonlocal pending_run, pending_logs, in_test_log
        if include_run and pending_run is not None:
            kept_lines.append(pending_run)
        if pending_logs:
            kept_lines.extend(pending_logs)
        pending_run = None
        pending_logs = []
        in_test_log = False

    for ln in lines:
        if _GO_RUN_RE.match(ln):
            # Previous pending_logs without a PASS/SKIP belong to an unfinished
            # or parallel test; never drop them silently.
            if pending_logs:
                _flush_pending(include_run=True)
            pending_run = ln
            in_test_log = False
            continue

        if _GO_PASS_RE.match(ln):
            passed += 1
            pending_run = None
            pending_logs = []
            in_test_log = False
            continue

        if _GO_SKIP_RE.match(ln):
            skipped += 1
            pending_run = None
            pending_logs = []
            in_test_log = False
            continue

        if _GO_FAIL_RE.match(ln):
            failed += 1
            # --- FAIL: already names the test; flush any buffered diagnostics.
            _flush_pending(include_run=False)
            kept_lines.append(ln)
            continue

        if _GO_PKG_OK_RE.match(ln) or _GO_PKG_FAIL_RE.match(ln) or _GO_PKG_NOTEST_RE.match(ln):
            if pending_logs:
                _flush_pending(include_run=True)
            else:
                pending_run = None
                in_test_log = False
            pkg_summaries.append(ln)
            continue

        if ln.strip() == "PASS":
            if pending_logs:
                _flush_pending(include_run=True)
            else:
                pending_run = None
                in_test_log = False
            continue

        if ln.strip() == "FAIL":
            if pending_logs:
                _flush_pending(include_run=True)
            else:
                pending_run = None
                in_test_log = False
            continue

        # Buffer clean `*_test.go:line:` t.Log lines (and indented continuations)
        # while inside a test so passing/skipped test logs don't leak into output.
        if (
            pending_run is not None
            and not _GO_FAILURE_SIGNAL_RE.search(ln)
            and (
                _GO_TEST_FILE_LINE_RE.match(ln)
                or (in_test_log and ln.startswith((" ", "\t")) and ln.strip())
            )
        ):
            pending_logs.append(ln)
            in_test_log = True
            continue

        # Everything else (compile errors, panic frames, Example got/want,
        # helper.go diagnostics, log lines, WARNING: DATA RACE, benchmarks)
        # is NOT known pass noise: keep it verbatim.
        _flush_pending(include_run=True)
        kept_lines.append(ln)

    if pending_logs:
        _flush_pending(include_run=True)

    # If no passing/skipped tests were collapsed, there is nothing to compress
    # (e.g. pure `go test -bench=.` or pure compile-error output).
    if passed == 0 and skipped == 0:
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
    if kept_lines:
        out_parts.append("\n".join(kept_lines))
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
    kept_lines: list[str] = []
    has_error_signal = False

    in_failures = False
    current_suite = ""
    suite_emitted_result = True

    for ln in lines:
        if not in_failures and (_CARGO_RUNNING_BIN_RE.match(ln) or _CARGO_DOC_HEADER_RE.match(ln)):
            current_suite = ln.strip()
            suite_emitted_result = False
            continue
        if not in_failures and _CARGO_RUNNING_RE.match(ln):
            continue
        if not in_failures and _CARGO_TEST_OK_RE.match(ln):
            ok_count += 1
            continue
        if not in_failures and _CARGO_TEST_IGNORED_RE.match(ln):
            ignored_count += 1
            continue
        if _CARGO_TEST_FAILED_RE.match(ln):
            failed_tests.append(ln)
            has_error_signal = True
            continue
        if ln.strip() == "failures:":
            in_failures = True
            has_error_signal = True
            kept_lines.append(ln)
            continue
        if _CARGO_RESULT_RE.match(ln):
            in_failures = False
            suite_emitted_result = True
            if "FAILED" in ln:
                has_error_signal = True
            label = f"[{current_suite}] {ln}" if current_suite else ln
            result_lines.append(label)
            continue
        if in_failures:
            kept_lines.append(ln)
            continue
        if not ln.strip():
            continue

        # Any other non-empty line (abort/SIGABRT, `error: test failed`,
        # `Caused by:`, `--nocapture` inline panics, `to rerun pass`, compiler
        # warnings/errors) is NOT known pass noise: keep it verbatim.
        if _CARGO_ERROR_SIGNAL_RE.search(ln):
            has_error_signal = True
            if current_suite and not suite_emitted_result:
                kept_lines.append(f"[{current_suite}]")
                suite_emitted_result = True
        kept_lines.append(ln)

    if ok_count == 0 and ignored_count == 0:
        return None
    if not result_lines and not failed_tests and not kept_lines:
        return None

    out_parts: list[str] = []
    note = f"cargo test: {ok_count} ok"
    if ignored_count:
        note += f", {ignored_count} ignored"
    if failed_tests:
        note += f", {len(failed_tests)} FAILED"
    elif has_error_signal:
        note += " (errors/abort)"
    out_parts.append(note)

    if failed_tests:
        out_parts.append("\n".join(failed_tests))
    if kept_lines:
        out_parts.append("\n".join(kept_lines))
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

    has_any_failure = any(
        _NODE_TAP_FAIL_RE.match(ln) or _NODE_SPEC_FAIL_RE.match(ln)
        for ln in lines
    )

    passed = 0
    kept_lines: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        ln = lines[i]
        if _NODE_SUMMARY_RE.match(ln):
            i += 1
            continue
        if _NODE_TAP_OK_RE.match(ln):
            passed += 1
            i += 1
            # Drop optional duration-only YAML metadata block on passing TAP tests.
            if i < n and lines[i].strip() == "---":
                j = i + 1
                yaml_only_pass = True
                while j < n and lines[j].strip() != "...":
                    if not _NODE_YAML_PASS_KEY_RE.match(lines[j]):
                        yaml_only_pass = False
                        break
                    j += 1
                if yaml_only_pass and j < n and lines[j].strip() == "...":
                    i = j + 1
            continue
        if _NODE_SPEC_OK_RE.match(ln):
            passed += 1
            i += 1
            continue
        if not has_any_failure and (
            _NODE_TAP_VERSION_RE.match(ln)
            or _NODE_TAP_PLAN_RE.match(ln)
            or _NODE_TAP_SUBTEST_RE.match(ln)
            or _NODE_SPEC_SUITE_RE.match(ln)
        ):
            i += 1
            continue
        if has_any_failure and (
            _NODE_TAP_VERSION_RE.match(ln)
            or _NODE_TAP_PLAN_RE.match(ln)
        ):
            i += 1
            continue
        if has_any_failure and _NODE_TAP_SUBTEST_RE.match(ln):
            # Drop `# Subtest:` only if the immediately following line is a passing `ok`.
            if i + 1 < n and _NODE_TAP_OK_RE.match(lines[i + 1]):
                i += 1
                continue
        if not ln.strip():
            # Keep single blank lines inside failure sections for readability.
            if kept_lines and kept_lines[-1] != "":
                kept_lines.append("")
            i += 1
            continue

        # Everything else (not ok, ✖, `test at file:line`, YAML error blocks,
        # AssertionError, diffs, stack traces) is NOT known pass noise: keep it.
        kept_lines.append(ln)
        i += 1

    while kept_lines and kept_lines[-1] == "":
        kept_lines.pop()

    if passed == 0:
        return None

    out_parts: list[str] = []
    if kept_lines:
        out_parts.append("\n".join(kept_lines))
    out_parts.append("\n".join(summary_lines))

    result = "\n\n".join(out_parts)
    if not result or len(result) >= len(output):
        return None
    return result


def compress_structured_test_output(output: str, runner: str | None = None) -> str | None:
    """Compress Go, Cargo, or Node test output.

    When ``runner`` is given (``'go'``, ``'cargo'``, ``'node'``), only that
    runner's compressor is invoked so one runner's output is never misparsed
    as another. Unknown explicit runners return ``None``.
    """
    dispatch = {
        "go": compress_go_test,
        "cargo": compress_cargo_test,
        "node": compress_node_test,
    }
    if runner is not None:
        fn = dispatch.get(runner)
        return fn(output) if fn is not None else None
    for fn in (compress_go_test, compress_cargo_test, compress_node_test):
        res = fn(output)
        if res is not None:
            return res
    return None


def compress_for_command(output: str, command_str: str) -> str | None:
    """Detect the runner from ``command_str`` and compress only that runner's format."""
    runner = detect_runner(command_str)
    if runner is None:
        return None
    return compress_structured_test_output(output, runner=runner)
