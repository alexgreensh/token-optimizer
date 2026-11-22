"""Tests for Go, Cargo, and Node (`node --test`) test-output compression."""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import bash_compress
import build_output_compress
from go_cargo_test_compress import (
    compress_cargo_test,
    compress_go_test,
    compress_node_test,
    compress_structured_test_output,
)


def _sample_go_test_passing(n_tests: int = 80) -> str:
    lines = []
    for i in range(n_tests):
        lines.append(f"=== RUN   TestRouteHandler{i}")
        lines.append(f"--- PASS: TestRouteHandler{i} (0.00s)")
    lines.append("=== RUN   TestSkippedCase")
    lines.append("--- SKIP: TestSkippedCase (0.00s)")
    lines.append("PASS")
    lines.append("ok  \tgithub.com/example/pkg/router\t0.412s")
    lines.append("?\tgithub.com/example/pkg/types\t[no test files]")
    return "\n".join(lines)


def _sample_go_test_failing(n_pass: int = 60) -> str:
    lines = []
    for i in range(n_pass):
        lines.append(f"=== RUN   TestOK{i}")
        lines.append(f"--- PASS: TestOK{i} (0.00s)")
    lines.append("=== RUN   TestBrokenMiddleware")
    lines.append("    middleware_test.go:88: expected status 401, got 200")
    lines.append("    middleware_test.go:89: header X-Auth missing")
    lines.append("--- FAIL: TestBrokenMiddleware (0.01s)")
    lines.append("FAIL")
    lines.append("FAIL\tgithub.com/example/pkg/middleware\t0.231s")
    lines.append("ok  \tgithub.com/example/pkg/router\t0.118s")
    return "\n".join(lines)


def _sample_cargo_test_workspace(n_per_crate: int = 30) -> str:
    lines = []
    for crate in ("core", "cli", "storage"):
        lines.append(f"     Running unittests src/lib.rs (target/debug/deps/{crate}-abc123)")
        lines.append(f"running {n_per_crate} tests")
        for i in range(n_per_crate):
            lines.append(f"test {crate}::tests::case_{i} ... ok")
        lines.append(f"test result: ok. {n_per_crate} passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.12s")
        lines.append("")
    return "\n".join(lines)


def _sample_cargo_test_failing() -> str:
    lines = ["     Running unittests src/lib.rs (target/debug/deps/app-1234)", "running 25 tests"]
    for i in range(24):
        lines.append(f"test suite::test_ok_{i} ... ok")
    lines.append("test suite::test_timeout_retry ... FAILED")
    lines.extend([
        "",
        "failures:",
        "",
        "---- suite::test_timeout_retry stdout ----",
        "thread 'suite::test_timeout_retry' panicked at src/retry.rs:42:9:",
        "assertion `left == right` failed",
        "  left: 3",
        " right: 5",
        "",
        "failures:",
        "    suite::test_timeout_retry",
        "",
        "test result: FAILED. 24 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.09s",
    ])
    return "\n".join(lines)


def _sample_node_test_tap(n_pass: int = 40, with_failure: bool = False) -> str:
    lines = ["TAP version 13"]
    for i in range(1, n_pass + 1):
        lines.append(f"ok {i} - parses option variant {i}")
    if with_failure:
        lines.extend([
            f"not ok {n_pass + 1} - rejects duplicate list flag",
            "  ---",
            "  duration_ms: 1.42",
            "  failureType: 'testCodeFailure'",
            "  error: 'Expected values to be strictly deep-equal'",
            "  ...",
        ])
    total = n_pass + (1 if with_failure else 0)
    lines.extend([
        f"1..{total}",
        f"# tests {total}",
        f"# pass {n_pass}",
        f"# fail {1 if with_failure else 0}",
        "# cancelled 0",
        "# skipped 0",
        "# duration_ms 42.5",
    ])
    return "\n".join(lines)


class TestGoTestCompression:
    def test_passing_go_test_v_compresses_heavily(self):
        raw = _sample_go_test_passing(80)
        out = compress_go_test(raw)
        assert out is not None
        assert len(out) < len(raw) * 0.15
        assert "go test: 80 passed, 1 skipped" in out
        assert "ok  \tgithub.com/example/pkg/router\t0.412s" in out
        assert "[no test files]" in out
        assert "TestRouteHandler0" not in out

    def test_failing_go_test_preserves_every_failure_and_assertion(self):
        raw = _sample_go_test_failing(60)
        out = compress_go_test(raw)
        assert out is not None
        assert len(out) < len(raw) * 0.25
        assert "60 passed, 1 failed" in out
        assert "--- FAIL: TestBrokenMiddleware (0.01s)" in out
        assert "middleware_test.go:88: expected status 401, got 200" in out
        assert "middleware_test.go:89: header X-Auth missing" in out
        assert "FAIL\tgithub.com/example/pkg/middleware\t0.231s" in out
        assert "ok  \tgithub.com/example/pkg/router\t0.118s" in out
        assert "TestOK0" not in out

    def test_short_go_test_is_untouched(self):
        raw = "=== RUN   TestOne\n--- PASS: TestOne (0.00s)\nPASS\nok  \texample/pkg\t0.01s"
        assert compress_go_test(raw) is None

    def test_bash_compress_dispatches_go_test_v(self):
        raw = _sample_go_test_passing(80)
        compressed = bash_compress.compress("go test -v ./...", raw, returncode=0)
        assert len(compressed) < len(raw) * 0.20
        assert "ok  \tgithub.com/example/pkg/router" in compressed


class TestCargoTestCompression:
    def test_multi_crate_cargo_test_keeps_all_summaries(self):
        raw = _sample_cargo_test_workspace(30)
        out = compress_cargo_test(raw)
        assert out is not None
        assert len(out) < len(raw) * 0.20
        assert "90 ok" in out
        assert out.count("test result: ok.") == 3
        assert "core-abc123" in out
        assert "cli-abc123" in out
        assert "storage-abc123" in out

    def test_failing_cargo_test_preserves_panic_and_assertion(self):
        raw = _sample_cargo_test_failing()
        out = compress_cargo_test(raw)
        assert out is not None
        assert "test suite::test_timeout_retry ... FAILED" in out
        assert "panicked at src/retry.rs:42:9" in out
        assert "left: 3" in out
        assert "right: 5" in out
        assert "test result: FAILED. 24 passed; 1 failed" in out
        assert "test_ok_0" not in out

    def test_bash_compress_dispatches_cargo_test(self):
        raw = _sample_cargo_test_workspace(30)
        compressed = bash_compress.compress("cargo test --workspace", raw, returncode=0)
        assert len(compressed) < len(raw) * 0.25
        assert compressed.count("test result: ok.") == 3


class TestNodeTestCompression:
    def test_node_test_tap_passing(self):
        raw = _sample_node_test_tap(40, with_failure=False)
        out = compress_node_test(raw)
        assert out is not None
        assert len(out) < len(raw) * 0.25
        assert "# tests 40" in out
        assert "# pass 40" in out
        assert "parses option variant 1" not in out

    def test_node_test_tap_failing_preserves_error(self):
        raw = _sample_node_test_tap(40, with_failure=True)
        out = compress_node_test(raw)
        assert out is not None
        assert "not ok 41 - rejects duplicate list flag" in out
        assert "Expected values to be strictly deep-equal" in out
        assert "# fail 1" in out

    def test_bash_compress_detects_node_test_not_plain_node(self):
        assert bash_compress._detect_pattern("node --test bench-check/list-options.test.js") == "pytest"
        assert bash_compress._detect_pattern("node script.js") is None
        raw = _sample_node_test_tap(40, with_failure=False)
        compressed = bash_compress.compress("node --test tests/", raw, returncode=0)
        assert len(compressed) < len(raw) * 0.25


class TestBuildOutputCompressGoCargoNode:
    def test_classify_node_test_only(self):
        assert build_output_compress.classify("node --test tests/") is True
        assert build_output_compress.classify("node server.js") is False

    def test_build_output_compress_strips_go_pass_keeps_fail(self):
        raw = _sample_go_test_failing(60)
        out = build_output_compress.compress("CGO_ENABLED=0 go test -v ./...", raw)
        assert out is not None
        assert len(out) < len(raw) * 0.35
        assert "--- FAIL: TestBrokenMiddleware (0.01s)" in out
        assert "middleware_test.go:88: expected status 401, got 200" in out
        assert "FAIL\tgithub.com/example/pkg/middleware\t0.231s" in out

    def test_build_output_compress_strips_cargo_ok_keeps_summary(self):
        raw = _sample_cargo_test_workspace(30)
        out = build_output_compress.compress("RUSTFLAGS=-Dwarnings cargo test", raw)
        assert out is not None
        assert len(out) < len(raw) * 0.30
        assert out.count("test result: ok.") == 3

    def test_build_output_compress_strips_node_pass_keeps_fail(self):
        raw = _sample_node_test_tap(40, with_failure=True)
        out = build_output_compress.compress("node --test bench-check/list-options.test.js", raw)
        assert out is not None
        assert "not ok 41 - rejects duplicate list flag" in out
        assert "# fail 1" in out

    def test_non_matching_output_returns_none(self):
        assert compress_structured_test_output("plain text\n" * 20) is None
