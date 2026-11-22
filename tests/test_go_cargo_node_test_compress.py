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
    compress_for_command,
    compress_go_test,
    compress_node_test,
    compress_structured_test_output,
    detect_runner,
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


class TestReviewRegressions:
    """Real-shaped regression fixtures covering all 7 review items."""

    def test_item1_go_benchmark_exit_0_never_dropped(self):
        bench_lines = [
            "goos: darwin",
            "goarch: arm64",
            "pkg: ex/a",
            "cpu: Apple M2 Pro",
        ]
        for i in range(40):
            bench_lines.append(
                f"BenchmarkWorker{i}-8   \t 1000000\t      {900 + i} ns/op\t     128 B/op\t       2 allocs/op"
            )
        bench_lines.extend(["PASS", "ok  \tex/a\t3.2s"])
        raw = "\n".join(bench_lines)

        assert compress_go_test(raw) is None
        out = bash_compress.compress("go test -bench=. ./...", raw, returncode=0)
        assert out == raw
        assert "BenchmarkWorker0-8" in out
        assert "BenchmarkWorker39-8" in out
        assert "939 ns/op" in out

    def test_item1_go_mixed_verbose_tests_and_benchmarks_preserves_benchmarks(self):
        lines = []
        for i in range(40):
            lines.append(f"=== RUN   TestFast{i}")
            lines.append(f"--- PASS: TestFast{i} (0.00s)")
        lines.extend([
            "goos: linux",
            "goarch: amd64",
            "pkg: ex/a",
            "=== RUN   BenchmarkHash",
            "BenchmarkHash-8   \t  500000\t      2140 ns/op",
            "PASS",
            "ok  \tex/a\t2.4s",
        ])
        raw = "\n".join(lines)
        out = compress_go_test(raw)
        assert out is not None
        assert "40 passed" in out
        assert "BenchmarkHash-8" in out
        assert "2140 ns/op" in out
        assert "TestFast0" not in out

    def test_item2_go_compile_error_behind_build_failed_preserved(self):
        lines = []
        for i in range(30):
            lines.append(f"=== RUN   TestGood{i}")
            lines.append(f"--- PASS: TestGood{i} (0.00s)")
        lines.extend([
            "PASS",
            "ok  \tex/good\t0.11s",
            "# ex/b [ex/b.test]",
            "./b.go:12:5: undefined: bar",
            "./b.go:19:11: cannot use x (variable of type string) as int value",
            "FAIL\tex/b [build failed]",
            "FAIL",
        ])
        raw = "\n".join(lines)
        out = bash_compress.compress("go test -v ./...", raw, returncode=0)
        assert "./b.go:12:5: undefined: bar" in out
        assert "./b.go:19:11: cannot use x" in out
        assert "FAIL\tex/b [build failed]" in out
        assert "TestGood0" not in out

    def test_item2_go_panic_stack_frames_preserved(self):
        lines = []
        for i in range(30):
            lines.append(f"=== RUN   TestOK{i}")
            lines.append(f"--- PASS: TestOK{i} (0.00s)")
        lines.extend([
            "=== RUN   TestNilHandler",
            "panic: runtime error: invalid memory address or nil pointer dereference",
            "[signal SIGSEGV: segmentation violation code=0x2 addr=0x0 pc=0x104a1b2c0]",
            "",
            "goroutine 19 [running]:",
            "ex/srv.(*Server).Handle(0x0, {0x104b2, 0x104c3})",
            "\t/Users/dev/srv/server.go:88 +0x24",
            "ex/srv.TestNilHandler(0x14000102340)",
            "\t/Users/dev/srv/server_test.go:15 +0x38",
            "exit status 2",
            "FAIL\tex/srv\t0.112s",
        ])
        raw = "\n".join(lines)
        out = compress_go_test(raw)
        assert out is not None
        assert "panic: runtime error: invalid memory address" in out
        assert "goroutine 19 [running]:" in out
        assert "ex/srv.(*Server).Handle" in out
        assert "/Users/dev/srv/server.go:88 +0x24" in out
        assert "/Users/dev/srv/server_test.go:15 +0x38" in out
        assert "FAIL\tex/srv\t0.112s" in out

    def test_item2_go_example_got_want_preserved(self):
        lines = []
        for i in range(25):
            lines.append(f"=== RUN   TestFmt{i}")
            lines.append(f"--- PASS: TestFmt{i} (0.00s)")
        lines.extend([
            "=== RUN   ExampleFormatDate",
            "--- FAIL: ExampleFormatDate (0.00s)",
            "got:",
            "2026-01-01",
            "want:",
            "2026/01/01",
            "FAIL",
            "FAIL\tex/fmt\t0.015s",
        ])
        raw = "\n".join(lines)
        out = compress_go_test(raw)
        assert out is not None
        assert "--- FAIL: ExampleFormatDate (0.00s)" in out
        assert "got:\n2026-01-01\nwant:\n2026/01/01" in out

    def test_item2_go_helper_file_logs_and_data_race_preserved(self):
        lines = []
        for i in range(25):
            lines.append(f"=== RUN   TestPool{i}")
            lines.append(f"--- PASS: TestPool{i} (0.00s)")
        lines.extend([
            "=== RUN   TestRaceCounter",
            "==================",
            "WARNING: DATA RACE",
            "Write at 0x00c00001a0b0 by goroutine 8:",
            "  ex/pool.inc()",
            "      /src/pool/counter.go:14 +0x3c",
            "Previous read at 0x00c00001a0b0 by goroutine 7:",
            "  ex/pool.read()",
            "      /src/pool/counter.go:19 +0x28",
            "==================",
            "    helper.go:42: unexpected worker state 3",
            "2026/11/22 10:00:00 WARN retrying connection",
            "--- FAIL: TestRaceCounter (0.02s)",
            "FAIL",
            "FAIL\tex/pool\t0.095s",
        ])
        raw = "\n".join(lines)
        out = compress_go_test(raw)
        assert out is not None
        assert "WARNING: DATA RACE" in out
        assert "/src/pool/counter.go:14 +0x3c" in out
        assert "helper.go:42: unexpected worker state 3" in out
        assert "2026/11/22 10:00:00 WARN retrying connection" in out
        assert "--- FAIL: TestRaceCounter (0.02s)" in out

    def test_item2_cargo_abort_after_passing_suite_not_false_green(self):
        lines = [
            "     Running unittests src/lib.rs (target/debug/deps/core-111)",
            "running 20 tests",
        ]
        for i in range(20):
            lines.append(f"test core::case_{i} ... ok")
        lines.extend([
            "test result: ok. 20 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.05s",
            "",
            "     Running tests/integration.rs (target/debug/deps/integration-222)",
            "running 5 tests",
            "fatal runtime error: stack overflow",
            "error: test failed, to rerun pass `--test integration`",
            "",
            "Caused by:",
            "  process didn't exit successfully: `target/debug/deps/integration-222` (signal: 6, SIGABRT: process abort signal)",
        ])
        raw = "\n".join(lines)
        out = compress_cargo_test(raw)
        assert out is not None
        assert "(errors/abort)" in out
        assert "fatal runtime error: stack overflow" in out
        assert "error: test failed, to rerun pass `--test integration`" in out
        assert "Caused by:" in out
        assert "SIGABRT: process abort signal" in out
        assert "core::case_0" not in out

    def test_item2_cargo_nocapture_inline_panic_and_rerun_hint_preserved(self):
        lines = [
            "     Running unittests src/lib.rs (target/debug/deps/app-999)",
            "running 25 tests",
        ]
        for i in range(24):
            lines.append(f"test mod_a::t_{i} ... ok")
        lines.extend([
            "thread 'mod_a::t_boom' panicked at src/parser.rs:77:13:",
            "index out of bounds: the len is 2 but the index is 5",
            "note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace",
            "test mod_a::t_boom ... FAILED",
            "",
            "failures:",
            "    mod_a::t_boom",
            "",
            "test result: FAILED. 24 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.08s",
            "error: test failed, to rerun pass `--lib`",
        ])
        raw = "\n".join(lines)
        out = compress_cargo_test(raw)
        assert out is not None
        assert "thread 'mod_a::t_boom' panicked at src/parser.rs:77:13:" in out
        assert "index out of bounds: the len is 2 but the index is 5" in out
        assert "error: test failed, to rerun pass `--lib`" in out
        assert "test mod_a::t_boom ... FAILED" in out

    def test_item2_and_7_node_spec_reporter_passing_and_failing_preserves_test_at(self):
        pass_lines = ["▶ cli options"]
        for i in range(30):
            pass_lines.append(f"  ✔ parses flag variant {i} (0.{i:02d}ms)")
        pass_lines.extend([
            "▶ cli options (12.4ms)",
            "ℹ tests 30",
            "ℹ suites 1",
            "ℹ pass 30",
            "ℹ fail 0",
            "ℹ cancelled 0",
            "ℹ skipped 0",
            "ℹ todo 0",
            "ℹ duration_ms 14.2",
        ])
        raw_pass = "\n".join(pass_lines)
        out_pass = compress_node_test(raw_pass)
        assert out_pass is not None
        assert len(out_pass) < len(raw_pass) * 0.30
        assert "ℹ tests 30" in out_pass
        assert "ℹ pass 30" in out_pass
        assert "parses flag variant 0" not in out_pass

        fail_lines = ["▶ cli options"]
        for i in range(25):
            fail_lines.append(f"  ✔ parses flag variant {i} (0.{i:02d}ms)")
        fail_lines.extend([
            "  ✖ rejects duplicate list flag (1.42ms)",
            "▶ cli options (15.1ms)",
            "ℹ tests 26",
            "ℹ suites 1",
            "ℹ pass 25",
            "ℹ fail 1",
            "ℹ cancelled 0",
            "ℹ skipped 0",
            "ℹ todo 0",
            "ℹ duration_ms 18.7",
            "",
            "✖ failing tests:",
            "",
            "test at tests/cli.test.js:42:3",
            "✖ rejects duplicate list flag (1.42ms)",
            "  AssertionError [ERR_ASSERTION]: Expected values to be strictly deep-equal:",
            "  + actual - expected",
            "  + 'a'",
            "  - 'b'",
            "      at TestContext.<anonymous> (file:///app/tests/cli.test.js:44:10)",
        ])
        raw_fail = "\n".join(fail_lines)
        out_fail = compress_node_test(raw_fail)
        assert out_fail is not None
        assert "test at tests/cli.test.js:42:3" in out_fail
        assert "✖ rejects duplicate list flag (1.42ms)" in out_fail
        assert "AssertionError [ERR_ASSERTION]" in out_fail
        assert "file:///app/tests/cli.test.js:44:10" in out_fail
        assert "ℹ fail 1" in out_fail
        assert "parses flag variant 0" not in out_fail

    def test_item3_pytest_containing_go_fail_line_not_misparsed_as_go(self):
        lines = [
            "============================= test session starts ==============================",
            "platform darwin -- Python 3.12.0, pytest-8.3.0",
            "collected 21 items",
            "",
        ]
        for i in range(20):
            lines.append(f"tests/test_runner.py::test_case_{i} PASSED")
        lines.extend([
            "tests/test_runner.py::test_parses_go_fixture FAILED",
            "",
            "=================================== FAILURES ===================================",
            "___________________________ test_parses_go_fixture _____________________________",
            "    def test_parses_go_fixture():",
            "        sample = '--- FAIL: TestInner (0.00s)'",
            ">       assert parse(sample) == 'expected'",
            "E       AssertionError: assert 'actual' == 'expected'",
            "----------------------------- Captured stdout call -----------------------------",
            "--- FAIL: TestInner (0.00s)",
            "=========================== short test summary info ============================",
            "FAILED tests/test_runner.py::test_parses_go_fixture - AssertionError",
            "========================= 1 failed, 20 passed in 0.42s =========================",
        ])
        raw = "\n".join(lines)
        assert detect_runner("pytest tests/test_runner.py") is None
        assert compress_for_command(raw, "pytest tests/test_runner.py") is None
        out = bash_compress.compress("pytest tests/test_runner.py", raw, returncode=0)
        assert "go test:" not in out
        assert "test_parses_go_fixture" in out
        assert "AssertionError: assert 'actual' == 'expected'" in out
        assert "FAILED tests/test_runner.py::test_parses_go_fixture" in out
        assert "1 failed, 20 passed" in out

    def test_item4_node_app_js_with_test_flag_is_not_test_runner(self):
        assert bash_compress._detect_pattern("node app.js --test") is None
        assert bash_compress._detect_pattern("node -- --test") is None
        assert bash_compress._detect_pattern('node -e "console.log(1)" --test') is None
        assert build_output_compress.classify("node app.js --test") is False
        assert build_output_compress.classify("node -- --test") is False
        assert detect_runner("node app.js --test") is None
        assert detect_runner("node -- --test") is None

        # Valid invocations with --test before the script/file path still match
        assert bash_compress._detect_pattern("node --test tests/") == "pytest"
        assert bash_compress._detect_pattern("node --experimental-strip-types --test a.test.ts") == "pytest"
        assert build_output_compress.classify("node --test tests/") is True
        assert build_output_compress.classify("node --experimental-strip-types --test a.test.ts") is True
        assert detect_runner("node --test tests/") == "node"
        assert detect_runner("node --experimental-strip-types --test a.test.ts") == "node"

    def test_item5_cargo_doctest_with_spaces_counted_and_preserved(self):
        lines = [
            "   Doc-tests mycrate",
            "running 20 tests",
        ]
        for i in range(18):
            lines.append(f"test src/lib.rs - helper_{i} (line {10 + i}) ... ok")
        lines.append("test src/lib.rs - slow_doc (line 99) ... ignored")
        lines.append("test src/lib.rs - foo (line 5) ... FAILED")
        lines.extend([
            "",
            "failures:",
            "",
            "---- src/lib.rs - foo (line 5) stdout ----",
            "Test executable failed (exit status: 101).",
            "thread 'main' panicked at src/lib.rs:7:1:",
            "assertion `left == right` failed",
            "",
            "failures:",
            "    src/lib.rs - foo (line 5)",
            "",
            "test result: FAILED. 18 passed; 1 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.45s",
        ])
        raw = "\n".join(lines)
        out = compress_cargo_test(raw)
        assert out is not None
        assert "cargo test: 18 ok, 1 ignored, 1 FAILED" in out
        assert "test src/lib.rs - foo (line 5) ... FAILED" in out
        assert "helper_0" not in out

        # Also verify build_output_compress preserves failing doctest and strips passing ones
        bo = build_output_compress.compress("cargo test --doc", raw)
        assert bo is not None
        assert "test src/lib.rs - foo (line 5) ... FAILED" in bo

    def test_item6_go_t_log_from_passing_and_skipped_tests_not_orphaned(self):
        lines = []
        for i in range(20):
            lines.append(f"=== RUN   TestFast{i}")
            lines.append(f"--- PASS: TestFast{i} (0.00s)")
        lines.extend([
            "=== RUN   TestLoggedPass",
            "    handler_test.go:44: request took 2ms",
            "    handler_test.go:45: response status 200",
            "--- PASS: TestLoggedPass (0.00s)",
            "=== RUN   TestSkippedReason",
            "    handler_test.go:52: skipping without docker",
            "--- SKIP: TestSkippedReason (0.00s)",
            "PASS",
            "ok  \tex/handler\t0.05s",
        ])
        raw = "\n".join(lines)
        out = compress_go_test(raw)
        assert out is not None
        assert "go test: 21 passed, 1 skipped" in out
        assert "handler_test.go:44" not in out
        assert "handler_test.go:45" not in out
        assert "handler_test.go:52" not in out
        assert "ok  \tex/handler\t0.05s" in out
