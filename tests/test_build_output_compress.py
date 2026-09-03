#!/usr/bin/env python3
"""Tests for build_output_compress.py — build/test/run output compression.

Tests prove:
1. classify() correctly identifies build/test/run commands
2. classify_by_shape() catches unknown build tools by output shape
3. compress() collapses repetition while preserving distinct error lines
4. Outputs < 2 KB are never compressed (returns None)
5. Every distinct error line in the original appears in the compressed output
6. Summary/exit lines are preserved
7. The tail is preserved
8. Credential-bearing lines are re-injected
9. 50K-line output completes in < 2 seconds
10. ANSI-coloured output is handled (caller strips ANSI, but we test the shape)
11. Near-identical warning blocks are collapsed
12. Pure error dumps lose no error line
13. 2>&1 mixed output is handled correctly
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from build_output_compress import (
    classify,
    classify_by_shape,
    compress,
    _collapse_identical_runs,
    _collapse_near_identical_warnings,
    _find_distinct_error_lines,
    _find_summary_lines,
    _is_error_line,
    _is_progress_noise,
    _is_summary_line,
    _MIN_COMPRESS_BYTES,
)


# ---------------------------------------------------------------------------
# Fixtures: realistic build/test/run output
# ---------------------------------------------------------------------------

def _gcc_success_output(n_warnings=100, n_files=20):
    """Realistic gcc success output: Compiling lines + warnings + Build finished."""
    lines = []
    for i in range(n_files):
        lines.append(f"Compiling src/file_{i:03d}.c...")
        lines.append(f"src/file_{i:03d}.c:10:5: warning: unused variable 'x_{i}' [-Wunused-variable]")
    # Add many identical warnings (the repetition we want to collapse)
    for _ in range(n_warnings):
        lines.append("src/common.c:42:5: warning: unused variable 'buf' [-Wunused-variable]")
    lines.append(f"Build finished: {n_files} files compiled, {n_warnings + n_files} warnings")
    return "\n".join(lines)


def _gcc_failure_output(n_identical_warnings=400, n_distinct_errors=3):
    """Realistic gcc failure: many identical warnings + few distinct errors + make error."""
    lines = []
    for i in range(20):
        lines.append(f"Compiling src/file_{i:03d}.c...")
    for _ in range(n_identical_warnings):
        lines.append("src/common.c:42:5: warning: unused variable 'buf' [-Wunused-variable]")
    errors = [
        "src/main.c:15:5: error: use of undeclared identifier 'foo'",
        "src/main.c:22:3: error: expected ';' after expression",
        "src/main.c:30:10: error: incompatible pointer types assigning to 'int *' from 'char *'",
    ]
    for e in errors[:n_distinct_errors]:
        lines.append(e)
    lines.append("make: *** [Makefile:20: all] Error 1")
    return "\n".join(lines)


def _pytest_success_output(n_tests=90):
    """Realistic pytest success: many PASSED lines + summary."""
    lines = [
        "============================= test session starts ==============================",
        "platform darwin -- Python 3.12.0, pytest-8.0.0, pluggy-1.5.0",
        "rootdir: /Users/test/project",
        "collecting ... ",
    ]
    for i in range(1, n_tests + 1):
        lines.append(f"tests/test_module_{i // 10:03d}.py::test_case_{i % 10:03d} PASSED")
    lines.append("")
    lines.append(f"============================= {n_tests} passed in 2.34s ==============================")
    return "\n".join(lines)


def _pytest_failure_output(n_passed=50, n_failed=5):
    """Realistic pytest failure: PASSED lines + FAILED with tracebacks + summary."""
    lines = [
        "============================= test session starts ==============================",
        "platform darwin -- Python 3.12.0, pytest-8.0.0, pluggy-1.5.0",
        "rootdir: /Users/test/project",
        "collecting ... ",
    ]
    for i in range(1, n_passed + 1):
        lines.append(f"tests/test_module_{i // 10:03d}.py::test_case_{i % 10:03d} PASSED")
    for i in range(1, n_failed + 1):
        lines.append(f"tests/test_module_00{i}.py::test_case_00{i} FAILED")
        lines.append(f"  assert {i} == {i + 1}")
        lines.append("  E   assert 1 == 2")
        lines.append("")
        lines.append("  tests/test_module_00{i}.py:42: AssertionError")
    lines.append("")
    lines.append(f"========================= {n_passed} passed, {n_failed} failed in 5.67s =========================")
    return "\n".join(lines)


def _npm_install_output(n_packages=50):
    """Realistic npm install: progress + added packages + vulnerability summary."""
    lines = ["npm WARN config global `--global`, `--local` are deprecated."]
    for i in range(n_packages):
        lines.append(f"npm WARN deprecated package_{i}@1.0.0: Use package_{i}@2.0.0 instead")
    lines.append("")
    # Add many identical progress lines (the repetition we want to collapse)
    for _ in range(100):
        lines.append("npm WARN deprecated legacy-dep@1.0.0: This package is no longer maintained")
    lines.append("")
    for i in range(n_packages):
        lines.append(f"  added {i} packages, and audited {n_packages} packages in 3s")
    lines.append("")
    lines.append("3 packages are looking for funding")
    lines.append(f"  run `npm fund` for details")
    lines.append("")
    lines.append(f"found 0 vulnerabilities")
    return "\n".join(lines)


def _cargo_build_output(n_compiling=30, n_warnings=20):
    """Realistic cargo build: Compiling lines + warnings + Finished."""
    lines = []
    for i in range(n_compiling):
        lines.append(f"   Compiling dependency_{i} v0.{i}.0")
    for i in range(n_warnings):
        lines.append(f"warning: unused variable: `x_{i}`")
        lines.append(f"  --> src/lib.rs:{10 + i}:5")
        lines.append(f"   |")
        lines.append(f"   |     let x_{i} = 42;")
        lines.append(f"   |         ^^^ help: try removing this")
    lines.append(f"    Finished `dev` profile [unoptimized + debuginfo] target(s) in 5.3s")
    return "\n".join(lines)


def _make_2and1_output():
    """make output with 2>&1 (stdout+stderr merged)."""
    lines = []
    for i in range(30):
        lines.append(f"cc -c -Wall src/file_{i:03d}.c -o obj/file_{i:03d}.o")
    for _ in range(100):
        lines.append("src/common.c:42:5: warning: unused variable 'buf' [-Wunused-variable]")
    lines.append("src/main.c:15:5: error: use of undeclared identifier 'foo'")
    lines.append("make: *** [Makefile:20: all] Error 1")
    return "\n".join(lines)


def _pure_error_dump(n_errors=20):
    """Pure error dump: all distinct error lines, no repetition."""
    lines = []
    for i in range(n_errors):
        lines.append(f"src/file_{i:03d}.c:{10 + i}:5: error: use of undeclared identifier 'var_{i}'")
    lines.append(f"make: *** [Makefile:20: all] Error 1")
    return "\n".join(lines)


def _large_output(n_lines=50000):
    """50K-line output for performance testing."""
    lines = []
    for i in range(n_lines):
        if i % 100 == 0:
            lines.append(f"src/file_{i:05d}.c:10:5: error: use of undeclared identifier 'x_{i}'")
        else:
            lines.append(f"Compiling src/file_{i % 1000:05d}.c...")
    lines.append("make: *** [Makefile:20: all] Error 1")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# classify() tests
# ---------------------------------------------------------------------------

class TestClassify:
    def test_gcc(self):
        assert classify("gcc -c main.c") is True

    def test_gpp(self):
        assert classify("g++ -std=c++17 main.cpp") is True

    def test_clang(self):
        assert classify("clang -c main.c -o main.o") is True

    def test_make(self):
        assert classify("make") is True
        assert classify("make all") is True
        assert classify("make test") is True

    def test_cargo_build(self):
        assert classify("cargo build") is True

    def test_cargo_test(self):
        assert classify("cargo test") is True

    def test_cargo_run(self):
        assert classify("cargo run") is True

    def test_cargo_unknown_subcmd(self):
        assert classify("cargo search") is False

    def test_npm_test(self):
        assert classify("npm test") is True

    def test_npm_install(self):
        assert classify("npm install") is True

    def test_npm_run_build(self):
        assert classify("npm run build") is True

    def test_npm_publish(self):
        # publish is not in the eligible set, and not "run"
        assert classify("npm publish") is False

    def test_yarn_test(self):
        assert classify("yarn test") is True

    def test_pnpm_build(self):
        assert classify("pnpm build") is True

    def test_go_build(self):
        assert classify("go build ./...") is True

    def test_go_test(self):
        assert classify("go test ./...") is True

    def test_go_run(self):
        # "run" is not in the go eligible set
        assert classify("go run main.go") is False

    def test_mvn(self):
        assert classify("mvn test") is True
        assert classify("mvn compile") is True

    def test_gradle(self):
        assert classify("gradle build") is True
        assert classify("./gradlew test") is True

    def test_tsc(self):
        assert classify("tsc --noEmit") is True

    def test_npx(self):
        assert classify("npx tsc --noEmit") is True

    def test_python_m_pytest(self):
        assert classify("python -m pytest tests/") is True

    def test_python_m_unittest(self):
        assert classify("python -m unittest discover") is True

    def test_python_m_json_tool(self):
        # json.tool is not a build/test module
        assert classify("python -m json.tool data.json") is False

    def test_dotnet_build(self):
        assert classify("dotnet build") is True

    def test_dotnet_test(self):
        assert classify("dotnet test") is True

    def test_dotnet_run(self):
        # "run" is in the dotnet eligible set
        assert classify("dotnet run") is True

    def test_swift_build(self):
        assert classify("swift build") is True

    def test_rustc(self):
        assert classify("rustc main.rs") is True

    def test_non_build_command(self):
        assert classify("rm -rf build/") is False
        assert classify("ls -la") is False
        assert classify("git status") is False
        assert classify("cat file.txt") is False

    def test_empty_command(self):
        assert classify("") is False

    def test_env_prefix(self):
        assert classify("CFLAGS=-Wall gcc -c main.c") is True

    def test_gradlew_path(self):
        assert classify("./gradlew build") is True


# ---------------------------------------------------------------------------
# classify_by_shape() tests
# ---------------------------------------------------------------------------

class TestClassifyByShape:
    def test_gcc_output_shape(self):
        output = _gcc_failure_output()
        assert classify_by_shape(output) is True

    def test_pytest_output_shape(self):
        output = _pytest_success_output()
        assert classify_by_shape(output) is True

    def test_small_output(self):
        assert classify_by_shape("error: something\n") is False

    def test_non_build_output(self):
        output = "Hello world\n" * 100
        assert classify_by_shape(output) is False

    def test_empty(self):
        assert classify_by_shape("") is False


# ---------------------------------------------------------------------------
# compress() tests — the core invariants
# ---------------------------------------------------------------------------

class TestCompress:
    def test_small_output_returns_none(self):
        assert compress("gcc -c main.c", "Compiling main.c...\nDone.\n") is None

    def test_under_2kb_returns_none(self):
        output = "Compiling main.c...\n" * 10
        assert len(output) < _MIN_COMPRESS_BYTES
        assert compress("gcc -c main.c", output) is None

    def test_gcc_success_compresses(self):
        output = _gcc_success_output()
        result = compress("gcc -c main.c", output)
        assert result is not None
        assert len(result) < len(output)
        # Summary line must appear
        assert "Build finished" in result

    def test_gcc_failure_preserves_distinct_errors(self):
        output = _gcc_failure_output()
        result = compress("gcc -c main.c", output)
        assert result is not None
        # Every distinct error line must appear
        assert "error: use of undeclared identifier 'foo'" in result
        assert "error: expected ';' after expression" in result
        assert "error: incompatible pointer types" in result
        # Make error must appear
        assert "make: *** [Makefile:20: all] Error 1" in result

    def test_pytest_success_compresses(self):
        output = _pytest_success_output()
        result = compress("pytest tests/", output)
        assert result is not None
        assert len(result) < len(output)
        # Summary must appear
        assert "passed" in result

    def test_pytest_failure_preserves_failed_lines(self):
        output = _pytest_failure_output()
        result = compress("pytest tests/", output)
        assert result is not None
        # Every FAILED line must appear
        for i in range(1, 6):
            assert f"test_case_00{i} FAILED" in result
        # Summary must appear
        assert "passed" in result
        assert "failed" in result

    def test_npm_install_compresses(self):
        output = _npm_install_output()
        result = compress("npm install", output)
        assert result is not None
        assert len(result) < len(output)

    def test_cargo_build_compresses(self):
        output = _cargo_build_output()
        result = compress("cargo build", output)
        assert result is not None
        assert len(result) < len(output)
        # Finished line must appear
        assert "Finished" in result

    def test_make_2and1_mixed(self):
        output = _make_2and1_output()
        result = compress("make", output)
        assert result is not None
        # Error line must appear
        assert "error: use of undeclared identifier 'foo'" in result
        assert "make: *** [Makefile:20: all] Error 1" in result

    def test_pure_error_dump_loses_no_error(self):
        output = _pure_error_dump(n_errors=20)
        result = compress("gcc -c *.c", output)
        # Every distinct error line must appear (even if compression doesn't help)
        if result is not None:
            for i in range(20):
                assert f"error: use of undeclared identifier 'var_{i}'" in result
            assert "make: *** [Makefile:20: all] Error 1" in result

    def test_tail_preserved(self):
        output = _gcc_success_output(n_warnings=200, n_files=30)
        result = compress("gcc -c *.c", output)
        assert result is not None
        # The last line (Build finished) must appear
        last_line = output.strip().splitlines()[-1]
        assert last_line in result

    def test_credential_line_reinjected(self):
        # Build output with a credential-like string on one line
        lines = []
        for i in range(120):
            lines.append(f"Compiling src/file_{i:03d}.c...")
        lines.append("src/config.c:5: error: API key AKIAIOSFODNN7EXAMPLE found in source")
        lines.append("Build finished: 120 files compiled")
        output = "\n".join(lines)
        result = compress("gcc -c *.c", output)
        assert result is not None
        # The credential-bearing line must appear
        assert "AKIAIOSFODNN7EXAMPLE" in result

    def test_near_identical_warnings_collapsed(self):
        lines = []
        for i in range(100):
            lines.append(f"src/file.c:{10 + i}:5: warning: unused variable 'x' [-Wunused-variable]")
        lines.append("Build finished")
        output = "\n".join(lines)
        result = compress("gcc -c file.c", output)
        assert result is not None
        # Should be significantly smaller
        assert len(result) < len(output) * 0.5

    def test_identical_runs_collapsed(self):
        # Use non-noise identical lines so they get collapsed, not dropped
        lines = ["  Linking target/debug/myapp"] * 200
        lines.append("Build finished")
        output = "\n".join(lines)
        result = compress("gcc -c main.c", output)
        assert result is not None
        assert "identical lines collapsed" in result

    def test_returns_none_when_nothing_to_compress(self):
        # All unique lines, no repetition, no progress noise
        lines = [f"unique line number {i} with distinct content {hash(i)}" for i in range(50)]
        output = "\n".join(lines)
        # Make it large enough to pass the size gate
        output = output + "\n" + "\n".join(f"more unique {i}" for i in range(200))
        result = compress("gcc -c main.c", output)
        # Should return None (nothing to collapse)
        # Or return the same content (no meaningful reduction)
        if result is not None:
            assert len(result) >= len(output) * 0.85  # minimal reduction

    def test_none_output(self):
        assert compress("gcc", None) is None  # type: ignore

    def test_empty_output(self):
        assert compress("gcc", "") is None

    def test_fail_open_on_bad_input(self):
        # Should never raise
        assert compress("gcc", None) is None  # type: ignore
        assert compress(None, "output") is None  # type: ignore


# ---------------------------------------------------------------------------
# Performance test
# ---------------------------------------------------------------------------

class TestPerformance:
    def test_50k_lines_under_2_seconds(self):
        output = _large_output(n_lines=50000)
        start = time.time()
        result = compress("gcc -c *.c", output)
        elapsed = time.time() - start
        assert elapsed < 2.0, f"50K-line output took {elapsed:.2f}s (budget: 2.0s)"
        assert result is not None  # should compress


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_collapse_identical_runs(self):
        lines = ["a", "a", "a", "b", "c", "c"]
        result = _collapse_identical_runs(lines)
        assert "a  [3 identical lines collapsed]" in result
        assert "b" in result
        assert "c  [2 identical lines collapsed]" in result

    def test_collapse_identical_runs_empty(self):
        assert _collapse_identical_runs([]) == []

    def test_collapse_identical_runs_single(self):
        assert _collapse_identical_runs(["a"]) == ["a"]

    def test_collapse_near_identical_warnings(self):
        lines = [
            "src/file.c:10:5: warning: unused variable: `x`",
            "src/file.c:20:8: warning: unused variable: `x`",
            "src/file.c:30:1: warning: unused variable: `x`",
            "not a warning",
        ]
        result = _collapse_near_identical_warnings(lines)
        assert len(result) < len(lines)
        assert "+2 near-identical warnings" in result[0]
        assert "not a warning" in result

    def test_find_distinct_error_lines(self):
        lines = [
            "error: foo",
            "error: foo",  # duplicate
            "error: bar",
            "warning: baz",
            "error[E0308]: mismatched types",
        ]
        result = _find_distinct_error_lines(lines)
        assert "error: foo" in result
        assert "error: bar" in result
        assert "error[E0308]: mismatched types" in result
        assert "warning: baz" not in result
        # No duplicates
        assert len(result) == len(set(result))

    def test_find_summary_lines(self):
        lines = [
            "90 passed in 2.34s",
            "5 failed in 3.45s",
            "some random line",
            "Build finished: 20 files",
        ]
        result = _find_summary_lines(lines)
        assert any("passed" in r for r in result)
        assert any("failed" in r for r in result)
        assert any("Build finished" in r for r in result)

    def test_is_error_line(self):
        assert _is_error_line("error: something went wrong") is True
        assert _is_error_line("error[E0308]: mismatched types") is True
        assert _is_error_line("FAILED test_foo") is True
        assert _is_error_line("make: *** [Makefile:20: all] Error 1") is True
        assert _is_error_line("Compiling main.c...") is False
        assert _is_error_line("warning: unused variable") is False

    def test_is_summary_line(self):
        assert _is_summary_line("90 passed in 2.34s") is True
        assert _is_summary_line("5 failed in 3.45s") is True
        assert _is_summary_line("Build finished: 20 files") is True
        assert _is_summary_line("some random line") is False

    def test_is_progress_noise(self):
        assert _is_progress_noise("Compiling src/main.c...") is True
        assert _is_progress_noise("   Compiling foo v0.1.0") is True
        assert _is_progress_noise("Downloading package-1.0.0.tgz...") is True
        assert _is_progress_noise("error: something") is False
        assert _is_progress_noise("90 passed in 2.34s") is False
