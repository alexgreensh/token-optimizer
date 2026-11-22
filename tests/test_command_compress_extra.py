"""Tests for B2 additional command compressors (command_compress_extra.py)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import bash_compress
import bash_compress_hook
import bash_whitelist
import benchmark
import build_output_compress
import command_compress_extra as extra
import pipeline_analyzer
import post_run_classify


# ---------------------------------------------------------------------------
# Realistic fixtures
# ---------------------------------------------------------------------------

def _sample_git_diff_stat(n_files: int = 45) -> str:
    lines = []
    for i in range(n_files):
        folder = ["src/api", "src/core", "tests", "docs"][i % 4]
        churn = 180 if i == 5 else (120 if i == 12 else (i % 10) + 1)
        bars = "+" * min(churn, 30)
        lines.append(f" {folder}/file_{i:02d}.py | {churn:3d} {bars}")
    lines.append(f" {n_files} files changed, 812 insertions(+), 204 deletions(-)")
    return "\n".join(lines) + "\n"


def _sample_git_ls_files(n_files: int = 60) -> str:
    lines = ["README.md", "pyproject.toml"]
    for i in range(n_files):
        d = ["src/core", "src/api", "tests/unit"][i % 3]
        lines.append(f"{d}/module_{i:02d}.py")
    return "\n".join(lines) + "\n"


def _sample_gh_run_log(n_noise: int = 20, with_error: bool = True) -> str:
    lines = [
        "test\tSet up job\t2026-11-22T10:00:01Z Current runner version: '2.320.0'",
        "test\tSet up job\t2026-11-22T10:00:01Z Operating System: Ubuntu 24.04.1 LTS",
        "test\tSet up job\t2026-11-22T10:00:01Z Runner Image: ubuntu-24.04",
        "test\tSet up job\t2026-11-22T10:00:01Z GITHUB_TOKEN Permissions: contents: read",
        "test\tSet up job\t2026-11-22T10:00:01Z Prepare workflow directory",
        "test\tSet up job\t2026-11-22T10:00:02Z Prepare all required actions",
        "test\tSet up job\t2026-11-22T10:00:02Z Getting action download info",
        "test\tSet up job\t2026-11-22T10:00:03Z Download action repository 'actions/checkout@v4'",
        "test\tSet up job\t2026-11-22T10:00:03Z Complete job name: test",
    ]
    for i in range(n_noise):
        lines.append(
            f"test\tRun actions/checkout@v4\t2026-11-22T10:00:{10 + (i % 49):02d}Z Syncing repository: org/repo chunk {i:02d}"
        )
    lines.append("test\tRun actions/cache@v4\t2026-11-22T10:00:58Z Cache restored from key: pip-abc")
    if with_error:
        lines.extend([
            "test\tRun pytest\t2026-11-22T10:00:59Z def test_api_status():",
            "test\tRun pytest\t2026-11-22T10:00:59Z     resp = client.get('/health')",
            "test\tRun pytest\t2026-11-22T10:01:00Z >       assert resp.status_code == 200",
            "test\tRun pytest\t2026-11-22T10:01:00Z E       AssertionError: assert 500 == 200",
            "test\tRun pytest\t2026-11-22T10:01:00Z tests/test_api.py:77: AssertionError",
            "test\tRun pytest\t2026-11-22T10:01:01Z ##[error]Process completed with exit code 1.",
        ])
    lines.extend([
        "test\tPost Run actions/checkout@v4\t2026-11-22T10:01:02Z Post job cleanup.",
        "test\tComplete job\t2026-11-22T10:01:03Z Cleaning up orphan processes",
    ])
    return "\n".join(lines) + "\n"


def _sample_gh_list(n_rows: int = 35) -> str:
    lines = ["STATUS\tTITLE\tWORKFLOW\tBRANCH\tEVENT\tID\tELAPSED\tAGE"]
    for i in range(n_rows):
        status = "completed\tfailure" if i == 25 else "completed\tsuccess"
        lines.append(f"{status}\tPR #{100 + i} update module {i}\tCI\tmain\tpush\t{900000 + i}\t1m20s\t{i}h")
    return "\n".join(lines) + "\n"


def _sample_terraform_plan() -> str:
    lines = ["Acquiring state lock. This may take a few moments..."]
    for i in range(25):
        lines.append(f"aws_subnet.sub_{i:02d}: Refreshing state... [id=subnet-{i:04d}]")
    lines.extend([
        "data.aws_ami.ubuntu: Reading...",
        "data.aws_ami.ubuntu: Read complete after 1s [id=ami-123456]",
        "",
        "Terraform used the selected providers to generate the following execution plan.",
        "",
        "  # aws_instance.api will be created",
        '  + resource "aws_instance" "api" {',
        '      + ami           = "ami-123456"',
        '      + instance_type = "t3.large"',
        "      + arn           = (known after apply)",
        "      + id            = (known after apply)",
        "      + private_ip    = (known after apply)",
        "      + public_ip     = (known after apply)",
        "      + public_dns    = (known after apply)",
        "      + private_dns   = (known after apply)",
        "    }",
        "",
        "  # aws_security_group.api will be updated in-place",
        '  ~ resource "aws_security_group" "api" {',
        '      ~ description = "old" -> "new" # forces replacement',
        "        # (14 unchanged attributes hidden)",
        "    }",
        "",
        "Warning: Argument is deprecated",
        "  on main.tf line 42, in resource \"aws_instance\" \"api\":",
        "│ Deprecated attribute used.",
        "",
        "Plan: 1 to add, 1 to change, 0 to destroy.",
    ])
    return "\n".join(lines) + "\n"


def _sample_kubectl_describe() -> str:
    lines = [
        "Name:             worker-6f8b9d",
        "Namespace:        prod",
        "Node:             node-02/10.0.0.12",
        "Status:           Running",
        "IP:               10.244.2.15",
        "Annotations:",
    ]
    for i in range(12):
        lines.append(f"  annotation.example.com/key-{i:02d}: val-{i:02d}")
    lines.extend([
        "Containers:",
        "  worker:",
        "    Image:          registry.io/worker:v2.1.0",
        "    State:          Waiting",
        "      Reason:       CrashLoopBackOff",
        "    Last State:     Terminated",
        "      Reason:       OOMKilled",
        "      Exit Code:    137",
        "    Ready:          False",
        "    Restart Count:  4",
        "    Environment:",
    ])
    for i in range(15):
        lines.append(f"      VAR_{i:02d}: value_{i:02d}")
    lines.extend([
        "Conditions:",
        "  Type              Status",
        "  Ready             False",
        "Events:",
        "  Type     Reason   Age   From     Message",
        "  ----     ------   ----  ----     -------",
        "  Warning  BackOff  30s   kubelet  Back-off restarting failed container worker",
    ])
    return "\n".join(lines) + "\n"


def _sample_pip_install(n_pkgs: int = 20) -> str:
    lines = []
    for i in range(n_pkgs):
        lines.append(f"Collecting pkg-{i:02d}==1.0.{i}")
        lines.append(f"  Using cached pkg_{i:02d}-1.0.{i}-py3-none-any.whl ({40 + i} kB)")
    lines.append("Requirement already satisfied: certifi in ./venv/lib/python3.12/site-packages")
    lines.append("Installing collected packages: " + ", ".join(f"pkg-{i:02d}" for i in range(n_pkgs)))
    lines.append("WARNING: pip is configured with locations that require TLS/SSL")
    lines.append("Successfully installed " + " ".join(f"pkg-{i:02d}-1.0.{i}" for i in range(n_pkgs)))
    return "\n".join(lines) + "\n"


def _sample_brew_install() -> str:
    lines = [
        "==> Fetching dependencies for ripgrep: pcre2",
        "==> Fetching pcre2",
        "==> Downloading https://ghcr.io/v2/homebrew/core/pcre2/manifests/10.44",
        "######################################################################## 100.0%",
        "==> Downloading https://ghcr.io/v2/homebrew/core/pcre2/blobs/sha256:abc",
        "######################################################################## 100.0%",
        "==> Fetching ripgrep",
        "==> Downloading https://ghcr.io/v2/homebrew/core/ripgrep/manifests/14.1.1",
        "######################################################################## 100.0%",
        "==> Downloading https://ghcr.io/v2/homebrew/core/ripgrep/blobs/sha256:def",
        "######################################################################## 100.0%",
        "==> Installing dependencies for ripgrep: pcre2",
        "==> Installing ripgrep dependency: pcre2",
        "==> Pouring pcre2--10.44.arm64_sequoia.bottle.tar.gz",
        "🍺  /opt/homebrew/Cellar/pcre2/10.44: 238 files, 6.7MB",
        "==> Installing ripgrep",
        "==> Pouring ripgrep--14.1.1.arm64_sequoia.bottle.tar.gz",
        "==> Caveats",
        "zsh completions have been installed to: /opt/homebrew/share/zsh/site-functions",
        "==> Summary",
        "🍺  /opt/homebrew/Cellar/ripgrep/14.1.1: 12 files, 5.1MB",
        "==> Running `brew cleanup ripgrep`...",
    ]
    return "\n".join(lines) + "\n"


def _sample_mypy_output() -> str:
    lines = [
        'src/a.py:12: error: Incompatible types in assignment (expression has type "int", variable has type "str")  [assignment]',
    ]
    for i in range(10):
        lines.append(f"src/a.py:12: note:     Overload candidate {i}: def f(x: str) -> None")
    lines.extend([
        'src/b.py:45: error: Argument 1 to "foo" has incompatible type "None"; expected "int"  [arg-type]',
        'src/b.py:45: note: See https://mypy.readthedocs.io/en/stable/error_codes.html',
        'src/b.py:50: note: Revealed type is "builtins.list[builtins.str]"',
        'src/c.py:99: error: Name "bar" is not defined  [name-defined]',
    ])
    for i in range(6):
        lines.append(f"src/c.py:99: note:     Candidate detail {i}")
    lines.append("Found 3 errors in 3 files (checked 18 source files)")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Unit tests for each new compressor
# ---------------------------------------------------------------------------

class TestExtraCompressors:
    def test_git_diff_stat_preserves_summary_and_all_names(self):
        raw = _sample_git_diff_stat(45)
        out = bash_compress.compress("git diff --stat origin/main", raw, returncode=0)
        assert "45 files changed, 812 insertions(+), 204 deletions(-)" in out
        # Every single basename must be preserved losslessly
        for i in range(45):
            assert f"file_{i:02d}.py" in out
        assert "src/core/" in out
        assert "src/api/" in out
        assert len(out) < len(raw) * 0.75

    def test_git_grep_routes_to_search_results(self):
        assert bash_compress._detect_pattern("git grep -n handle_request") == "search_results"
        assert bash_whitelist.is_whitelisted("git grep -n handle_request")
        lines = [f"src/mod_{i % 3}.py:{i + 1}:    handle_request(ctx)" for i in range(45)]
        raw = "\n".join(lines) + "\n"
        out = bash_compress.compress("git grep -n handle_request", raw, returncode=0)
        assert "src/mod_0.py" in out
        assert len(out) < len(raw) * 0.70

    def test_git_ls_files_groups_by_directory_losslessly(self):
        raw = _sample_git_ls_files(60)
        out = bash_compress.compress("git ls-files", raw, returncode=0)
        assert "62 paths total" in out
        assert "src/core/" in out
        assert "README.md" in out
        # Every single basename must be preserved losslessly
        for i in range(60):
            assert f"module_{i:02d}.py" in out
        assert len(out) < len(raw) * 0.75

    def test_gh_run_view_log_preserves_errors_and_context(self):
        raw = _sample_gh_run_log(20, with_error=True)
        out = bash_compress.compress("gh run view 12345 --log", raw, returncode=0)
        assert "tests/test_api.py:77: AssertionError" in out
        assert "##[error]Process completed with exit code 1." in out
        assert "assert resp.status_code == 200" in out
        assert "def test_api_status():" in out
        assert len(out) < len(raw) * 0.55

    def test_gh_list_preserves_failed_rows_past_head(self):
        raw = _sample_gh_list(35)
        out = bash_compress.compress("gh run list --limit 50", raw, returncode=0)
        assert "PR #125" in out  # row 25 is a failure and must be preserved
        assert "failure" in out
        assert len(out) < len(raw) * 0.65

    def test_terraform_plan_preserves_actions_warnings_and_plan_line(self):
        raw = _sample_terraform_plan()
        out = bash_compress.compress("terraform plan", raw, returncode=0)
        assert "# aws_instance.api will be created" in out
        assert "# aws_security_group.api will be updated in-place" in out
        assert "forces replacement" in out
        assert "Warning: Argument is deprecated" in out
        assert "on main.tf line 42" in out
        assert "Plan: 1 to add, 1 to change, 0 to destroy." in out
        assert "Refreshing state..." not in out
        assert len(out) < len(raw) * 0.65

    def test_kubectl_describe_preserves_events_and_oom_status(self):
        raw = _sample_kubectl_describe()
        out = bash_compress.compress("kubectl describe pod worker-6f8b9d", raw, returncode=0)
        assert "Name:             worker-6f8b9d" in out
        assert "CrashLoopBackOff" in out
        assert "OOMKilled" in out
        assert "Exit Code:    137" in out
        assert "Events:" in out
        assert "Back-off restarting failed container worker" in out
        assert "VAR_14: value_14" not in out
        assert len(out) < len(raw) * 0.75

    def test_pip_install_via_build_output_compress(self):
        raw = _sample_pip_install(20)
        assert build_output_compress.classify("pip install -r requirements.txt")
        assert build_output_compress.classify("pip3 install fastapi")
        out = build_output_compress.compress("pip install -r requirements.txt", raw)
        assert out is not None
        assert "WARNING: pip is configured" in out
        assert "Successfully installed" in out
        assert "Using cached pkg_05" not in out
        assert len(out) < len(raw) * 0.45

    def test_brew_install_preserves_caveats_and_summary(self):
        raw = _sample_brew_install()
        assert build_output_compress.classify("brew install ripgrep")
        out = extra.compress_package_install(raw)
        assert out is not None
        assert "==> Caveats" in out
        assert "zsh completions have been installed" in out
        assert "🍺  /opt/homebrew/Cellar/ripgrep/14.1.1" in out
        assert "Downloading https://ghcr.io" not in out
        assert len(out) < len(raw) * 0.50

    def test_mypy_preserves_every_error_and_revealed_type(self):
        raw = _sample_mypy_output()
        assert pipeline_analyzer.is_read_only_pipeline("mypy src/")[0]
        assert bash_whitelist.is_whitelisted("mypy src/")
        out = bash_compress.compress("mypy src/", raw, returncode=0)
        assert "src/a.py:12: error:" in out
        assert "src/b.py:45: error:" in out
        assert "src/c.py:99: error:" in out
        assert "Revealed type is" in out
        assert "Found 3 errors in 3 files" in out
        assert "Overload candidate 5" not in out
        assert len(out) < len(raw) * 0.85


# ---------------------------------------------------------------------------
# Regression tests for Round 2 review items (zero information loss)
# ---------------------------------------------------------------------------

class TestReviewRegressions:
    def test_gh_run_view_log_keeps_entire_failing_step_body(self):
        """Review item 1: keep every line of a failing step (assertions, E lines, test body)."""
        lines = [
            "ci\tSet up job\t2026-11-22T10:00:01Z Current runner version: '2.320.0'",
            "ci\tSet up job\t2026-11-22T10:00:01Z Operating System: Ubuntu 24.04.1 LTS",
            "ci\tSet up job\t2026-11-22T10:00:01Z Runner Image: ubuntu-24.04",
            "ci\tSet up job\t2026-11-22T10:00:01Z GITHUB_TOKEN Permissions: contents: read",
            "ci\tSet up job\t2026-11-22T10:00:01Z Prepare workflow directory",
            "ci\tSet up job\t2026-11-22T10:00:02Z Prepare all required actions",
            "ci\tSet up job\t2026-11-22T10:00:02Z Getting action download info",
        ]
        for i in range(25):
            lines.append(
                f"ci\tRun actions/checkout@v4\t2026-11-22T10:00:{10 + (i % 45):02d}Z Fetching the repository object {i:02d}"
            )
        lines.extend([
            "ci\tRun actions/cache@v4\t2026-11-22T10:00:55Z Cache restored from key: pytest-cache-v1",
            "ci\tRun pytest\t2026-11-22T10:01:00Z _______________ test_effective_window _______________",
            "ci\tRun pytest\t2026-11-22T10:01:00Z def test_effective_window():",
            "ci\tRun pytest\t2026-11-22T10:01:00Z     cfg = Config(default_window=3600, override=30)",
            "ci\tRun pytest\t2026-11-22T10:01:00Z     store = Store(cfg)",
            "ci\tRun pytest\t2026-11-22T10:01:00Z     store.refresh()",
            "ci\tRun pytest\t2026-11-22T10:01:00Z >       assert store.effective_window() == 30",
            "ci\tRun pytest\t2026-11-22T10:01:00Z E       assert 3600 == 30",
            "ci\tRun pytest\t2026-11-22T10:01:00Z tests/test_store.py:412: AssertionError",
            "ci\tRun pytest\t2026-11-22T10:01:01Z FAILED tests/test_store.py::test_effective_window",
            "ci\tRun pytest\t2026-11-22T10:01:01Z ##[error]Process completed with exit code 1.",
            "ci\tPost Run actions/checkout@v4\t2026-11-22T10:01:02Z Post job cleanup.",
            "ci\tComplete job\t2026-11-22T10:01:03Z Cleaning up orphan processes",
        ])
        raw = "\n".join(lines) + "\n"
        out = bash_compress.compress("gh run view 99999 --log-failed", raw, returncode=0)
        assert "def test_effective_window():" in out
        assert "cfg = Config(default_window=3600, override=30)" in out
        assert ">       assert store.effective_window() == 30" in out
        assert "E       assert 3600 == 30" in out
        assert "tests/test_store.py:412: AssertionError" in out
        assert "FAILED tests/test_store.py::test_effective_window" in out
        assert "Process completed with exit code 1." in out
        assert "Cache restored from key" not in out
        assert len(out) < len(raw) * 0.65

    def test_gh_run_view_without_log_not_routed_and_keeps_x_rows(self):
        """Review item 2: gh run view <id> without --log does not hit gh_run_log; ^X rows survive."""
        assert extra.detect_pattern("gh run view 123456") is None
        assert extra.detect_pattern("gh run view 123456 --log") == "gh_run_log"
        assert extra.detect_pattern("gh run view 123456 --log-failed") == "gh_run_log"

        lines = [
            "X main CI · 123456",
            "Triggered via push about 5 minutes ago",
            "",
            "JOBS",
        ]
        for i in range(60):
            if i == 24:
                lines.append("X linux-py312-unit-tests in 4m12s (ID 555024)")
            elif i == 31:
                lines.append("  X macos-arm64-integration in 6m01s (ID 555031)")
            else:
                lines.append(f"✓ job-matrix-shard-{i:02d}-ubuntu-latest in 1m{i % 60:02d}s (ID {555000 + i})")
        raw = "\n".join(lines) + "\n"
        out = bash_compress.compress("gh run view 123456", raw, returncode=0)
        assert "X linux-py312-unit-tests in 4m12s (ID 555024)" in out
        assert "X macos-arm64-integration in 6m01s (ID 555031)" in out

    def test_gh_list_no_cap_on_failing_rows_and_exact_count(self):
        """Review item 3: 33 failing rows past head -> all 33 emitted, marker says 33."""
        lines = ["STATUS\tTITLE\tWORKFLOW\tBRANCH\tEVENT\tID\tELAPSED\tAGE"]
        for i in range(11):
            lines.append(f"completed\tsuccess\tPR #{i} ok\tCI\tmain\tpush\t{1000 + i}\t1m\t{i}h")
        for i in range(33):
            lines.append(f"completed\tfailure\tPR #{200 + i} broken\tCI\tmain\tpush\t{2000 + i}\t1m\t{i}h")
        for i in range(40):
            lines.append(f"completed\tsuccess\tPR #{300 + i} ok\tCI\tmain\tpush\t{3000 + i}\t1m\t{i}h")
        raw = "\n".join(lines) + "\n"
        out = bash_compress.compress("gh run list --limit 100", raw, returncode=0)
        assert "[33 additional failing/flagged rows preserved]" in out
        for i in range(33):
            assert f"PR #{200 + i} broken" in out

    def test_terraform_plan_never_drops_changed_attributes(self):
        """Review item 4: never drop ->, forces replacement, or +/- attribute lines."""
        lines = ["Acquiring state lock. This may take a few moments..."]
        for i in range(20):
            lines.append(f"aws_subnet.sub_{i:02d}: Refreshing state... [id=subnet-{i:04d}]")
        lines.extend([
            "",
            "Terraform used the selected providers to generate the following execution plan.",
            "",
            "  # aws_db_instance.primary must be replaced",
            '-/+ resource "aws_db_instance" "primary" {',
        ])
        for i in range(15):
            lines.append(f'      ~ param_{i:02d} = "v1" -> "v2"')
        lines.extend([
            '      ~ engine_version = "14.9" -> "16.1" # forces replacement',
            "      ~ storage_encrypted = true -> false",
            '      + ingress_cidr = "0.0.0.0/0"',
            '      - legacy_flag = "enabled"',
        ])
        for i in range(10):
            lines.append(f"      + computed_{i:02d} = (known after apply)")
        lines.extend([
            "        # (18 unchanged attributes hidden)",
            "    }",
            "",
            "Plan: 1 to add, 0 to change, 1 to destroy.",
        ])
        raw = "\n".join(lines) + "\n"
        out = bash_compress.compress("terraform plan", raw, returncode=0)
        assert 'engine_version = "14.9" -> "16.1" # forces replacement' in out
        assert "storage_encrypted = true -> false" in out
        assert '+ ingress_cidr = "0.0.0.0/0"' in out
        assert '- legacy_flag = "enabled"' in out
        for i in range(15):
            assert f'param_{i:02d} = "v1" -> "v2"' in out
        assert "computed_05 = (known after apply)" not in out
        assert "Plan: 1 to add, 0 to change, 1 to destroy." in out

    def test_mypy_keeps_expected_got_notes_collapses_overload_dumps(self):
        """Review item 5: keep error-attached Expected/Got notes; collapse long overload dumps."""
        lines = [
            'src/auth.py:42: error: Argument 1 to "verify" has incompatible type "int"; expected "str"  [arg-type]',
            'src/auth.py:42: note: Following member(s) of "int" have conflicts:',
            'src/auth.py:42: note:     Expected:',
            'src/auth.py:42: note:         def encode(self, encoding: str = ...) -> bytes',
            'src/auth.py:42: note:     Got:',
            'src/auth.py:42: note:         <nothing>',
            'src/api.py:88: error: No overload variant of "get" matches  [call-overload]',
            'src/api.py:88: note: Possible overload variants:',
        ]
        for i in range(10):
            lines.append(f"src/api.py:88: note:     def get(self, key: T{i}) -> R{i}")
        lines.append("Found 2 errors in 2 files (checked 10 source files)")
        raw = "\n".join(lines) + "\n"
        out = bash_compress.compress("mypy src/", raw, returncode=0)
        assert 'Following member(s) of "int" have conflicts:' in out
        assert "Expected:" in out
        assert "def encode(self, encoding: str = ...) -> bytes" in out
        assert "Got:" in out
        assert "<nothing>" in out
        assert "overload variant notes collapsed" in out
        assert "def get(self, key: T5)" not in out

    def test_git_ls_passes_through_when_savings_under_25_percent(self):
        """Review item 6: pass through if lossless dir grouping saves < ~25%."""
        # Root-level files have no repeated dir prefix to save bytes on.
        raw = "\n".join(f"file_{i:02d}.py" for i in range(30)) + "\n"
        assert extra.compress_git_ls(raw) is None

    def test_kubectl_describe_preserves_secret_and_config_env_refs(self):
        """Review item 7: never collapse Environment entries referencing secrets/config."""
        raw = _sample_kubectl_describe().replace(
            "      VAR_10: value_10\n      VAR_11: value_11\n      VAR_12: value_12",
            "      DATABASE_URL: <set to the key 'url' in secret 'db-creds'>  Optional: false\n"
            "      API_KEY: valueFrom secretKeyRef api-secrets/key\n"
            "      APP_CFG: valueFrom configMapKeyRef app-cm/cfg",
        )
        out = bash_compress.compress("kubectl describe pod worker-6f8b9d", raw, returncode=0)
        assert "DATABASE_URL:" in out
        assert "API_KEY: valueFrom secretKeyRef" in out
        assert "APP_CFG: valueFrom configMapKeyRef" in out
        assert "VAR_14: value_14" not in out


# ---------------------------------------------------------------------------
# Regression tests for EVERY safety rule
# ---------------------------------------------------------------------------

class TestSafetyRules:
    def test_short_outputs_untouched(self):
        short_stat = " a.py | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n"
        assert extra.compress_git_diff_stat(short_stat) is None
        assert extra.compress_git_ls("a.py\nb.py\nc.py\n") is None
        assert extra.compress_gh_run_log("line1\nline2\n") is None
        assert extra.compress_gh_list("ID\tTITLE\n1\tfoo\n") is None
        assert extra.compress_terraform_plan("No changes.\n") is None
        assert extra.compress_kubectl_describe("Name: pod\nStatus: Running\n") is None
        assert extra.compress_package_install("Successfully installed foo-1.0\n") is None
        assert extra.compress_mypy("Success: no issues found in 3 source files\n") is None

    def test_fail_open_on_exception(self):
        raw = _sample_terraform_plan()
        with mock.patch.object(extra, "compress_terraform_plan", side_effect=RuntimeError("boom")):
            assert extra.compress_for_pattern("terraform_plan", raw, "terraform plan") is None
            # bash_compress must fail open and never raise
            res = bash_compress.compress("terraform plan", raw, returncode=0)
            assert "Plan: 1 to add, 1 to change, 0 to destroy." in res

    def test_nonzero_exit_tees_raw_output(self):
        raw = _sample_terraform_plan()
        out = bash_compress.compress("terraform plan", raw, returncode=1)
        assert out == raw

    def test_credential_preserved_in_extra_and_build_compress(self):
        # Synthetic AWS-style key embedded inside a collapsed section must survive
        raw = _sample_kubectl_describe().replace(
            "      VAR_10: value_10",
            "      AWS_KEY: AKIAIOSFODNN7EXAMPLE",
        )
        out = bash_compress.compress("kubectl describe pod worker-6f8b9d", raw, returncode=0)
        assert "AKIAIOSFODNN7EXAMPLE" in out

        pip_raw = _sample_pip_install(20).replace(
            "  Using cached pkg_08-1.0.8-py3-none-any.whl (48 kB)",
            "  Using cached https://user:ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789@pypi.internal/pkg.whl",
        )
        pip_out = build_output_compress.compress("pip install -r requirements.txt", pip_raw)
        assert pip_out is not None
        assert "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789" in pip_out

    def test_gh_read_only_vs_write_gating(self):
        assert pipeline_analyzer.is_read_only_pipeline("gh run view 12345 --log")[0]
        assert pipeline_analyzer.is_read_only_pipeline("gh pr list --state open")[0]
        assert pipeline_analyzer.is_read_only_pipeline("gh issue list")[0]
        assert not pipeline_analyzer.is_read_only_pipeline("gh pr merge 42")[0]
        assert not pipeline_analyzer.is_read_only_pipeline("gh issue create --title x")[0]
        assert not pipeline_analyzer.is_read_only_pipeline("gh run cancel 12345")[0]
        assert not bash_whitelist.is_whitelisted("gh pr merge 42")
        assert not bash_whitelist.is_auto_approvable("gh run view 12345 --log")

    def test_post_run_classify_unwraps_cd_prefix_for_new_patterns(self):
        assert post_run_classify.eligible_effective("cd /repo && terraform plan") == "terraform plan"
        assert post_run_classify.eligible_effective("cd /repo && gh run view 123 --log") == "gh run view 123 --log"
        assert post_run_classify.eligible_effective("cd /repo && mypy src/") == "mypy src/"

    def test_hook_archives_and_attaches_expand_pointer(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path))
        monkeypatch.setenv("CLAUDE_SESSION_ID", "test-b2-archive-sess")
        raw = _sample_terraform_plan()
        payload = {
            "tool_name": "Bash",
            "session_id": "test-b2-archive-sess",
            "tool_input": {"command": "terraform plan"},
            "tool_response": {"stdout": raw, "stderr": "", "exit_code": 0},
        }
        captured = []
        monkeypatch.setattr(
            bash_compress_hook,
            "_emit_updated_tool_output",
            lambda out, err, nudge=None: captured.append(out),
        )
        bash_compress_hook._run(payload)
        assert len(captured) == 1
        assert "expand" in captured[0].lower() or "Full result archived" in captured[0]
        assert "Plan: 1 to add, 1 to change, 0 to destroy." in captured[0]


# ---------------------------------------------------------------------------
# Compression safety benchmark suite (loads skills/.../fixtures/b2_more_commands.json)
# ---------------------------------------------------------------------------

def test_benchmark_b2_fixtures_pass():
    fixture_file = SCRIPTS_DIR / "fixtures" / "b2_more_commands.json"
    assert fixture_file.is_file()
    fixtures = json.loads(fixture_file.read_text(encoding="utf-8"))
    assert len(fixtures) >= 7
    for fx in fixtures:
        passed, result = benchmark.run_single_fixture(fx, compressor=bash_compress.compress)
        assert passed, f"Benchmark fixture {fx['name']} failed: {result}"
