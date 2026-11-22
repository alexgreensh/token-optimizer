#!/usr/bin/env python3
"""Token Optimizer v5: Compression Benchmark Framework.

Runs fixture-based tests against compression patterns. Each fixture defines:
- raw CLI output (input)
- expected compressed output (or pattern to match)
- critical info that MUST be preserved
- critical info that MUST NOT be lost

Token counting uses bytes/4 proxy (closer to BPE than word count).
"""

import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Token estimation (same as measure.py _estimate_tokens)
# ---------------------------------------------------------------------------

def estimate_tokens(text):
    """Estimate token count using bytes/4 proxy."""
    if not text:
        return 0
    return len(text.encode("utf-8", errors="replace")) // 4


# ---------------------------------------------------------------------------
# Built-in fixtures (curated input/output pairs)
# ---------------------------------------------------------------------------

FIXTURES = [
    # --- git status ---
    {
        "name": "git_status_basic",
        "category": "git",
        "command": "git status",
        "raw_output": (
            "On branch main\n"
            "Your branch is ahead of 'origin/main' by 2 commits.\n"
            "  (use \"git push\" to publish your local commits)\n\n"
            "Changes to be committed:\n"
            "  (use \"git restore --staged <file>...\" to unstage)\n"
            "\tnew file:   src/auth.py\n"
            "\tmodified:   src/utils.py\n\n"
            "Changes not staged for commit:\n"
            "  (use \"git add <file>...\" to update what will be committed)\n"
            "\tmodified:   README.md\n\n"
            "Untracked files:\n"
            "  (use \"git add <file>...\" to include in what will be committed)\n"
            "\ttmp/debug.log\n"
        ),
        "must_preserve": [
            "main",           # branch name
            "ahead",          # ahead/behind status
            "2 commits",      # count
            "src/auth.py",    # staged file
            "src/utils.py",   # staged file
            "README.md",      # unstaged file
        ],
        "min_compression": 0.30,  # must save at least 30%
    },
    {
        "name": "git_status_clean",
        "category": "git",
        "command": "git status",
        "raw_output": (
            "On branch main\n"
            "Your branch is up to date with 'origin/main'.\n\n"
            "nothing to commit, working tree clean\n"
        ),
        "must_preserve": ["main", "clean"],
        "min_compression": 0.0,  # short output, may not compress
    },
    {
        "name": "git_status_not_git",
        "category": "git",
        "command": "git status",
        "raw_output": "fatal: not a git repository (or any of the parent directories): .git\n",
        "must_preserve": ["fatal", "not a git repository"],
        "min_compression": 0.0,  # error, pass through
    },

    # --- git log ---
    {
        "name": "git_log_basic",
        "category": "git",
        "command": "git log --oneline -10",
        "raw_output": (
            "a1b2c3d Fix authentication bug in login flow\n"
            "e4f5g6h Add user preferences API endpoint\n"
            "i7j8k9l Update dependencies to latest versions\n"
            "m0n1o2p Refactor database connection pooling\n"
            "q3r4s5t Fix race condition in session handler\n"
        ),
        "must_preserve": [
            "a1b2c3d", "e4f5g6h",  # commit hashes
            "authentication", "preferences",  # key words from messages
        ],
        "min_compression": 0.0,  # already compact
    },

    # --- pytest ---
    {
        "name": "pytest_mixed",
        "category": "test_runner",
        "command": "pytest tests/",
        "raw_output": (
            "============================= test session starts ==============================\n"
            "platform darwin -- Python 3.11.5, pytest-7.4.0, pluggy-1.3.0\n"
            "rootdir: /Users/dev/project\n"
            "plugins: anyio-4.0.0\n"
            "collected 47 items\n\n"
            "tests/test_auth.py::test_login_success PASSED\n"
            "tests/test_auth.py::test_login_invalid_password PASSED\n"
            "tests/test_auth.py::test_login_missing_email PASSED\n"
            "tests/test_auth.py::test_token_refresh PASSED\n"
            "tests/test_auth.py::test_token_expired PASSED\n"
            "tests/test_api.py::test_get_users PASSED\n"
            "tests/test_api.py::test_get_users_pagination PASSED\n"
            "tests/test_api.py::test_create_user PASSED\n"
            "tests/test_api.py::test_create_user_duplicate PASSED\n"
            "tests/test_api.py::test_update_user PASSED\n"
            "tests/test_api.py::test_delete_user PASSED\n"
            "tests/test_api.py::test_delete_user_not_found PASSED\n"
            "tests/test_db.py::test_connection_pool PASSED\n"
            "tests/test_db.py::test_transaction_rollback PASSED\n"
            "tests/test_db.py::test_migration_up PASSED\n"
            "tests/test_db.py::test_migration_down PASSED\n"
            "tests/test_db.py::test_concurrent_writes PASSED\n"
            "tests/test_db.py::test_deadlock_recovery PASSED\n"
            "tests/test_utils.py::test_hash_password PASSED\n"
            "tests/test_utils.py::test_validate_email PASSED\n"
            "tests/test_utils.py::test_sanitize_input PASSED\n"
            "tests/test_utils.py::test_rate_limiter PASSED\n"
            "tests/test_utils.py::test_retry_decorator PASSED\n"
            "tests/test_integration.py::test_full_login_flow PASSED\n"
            "tests/test_integration.py::test_api_auth_required PASSED\n"
            "tests/test_integration.py::test_data_consistency FAILED\n"
            "tests/test_integration.py::test_cache_invalidation PASSED\n"
            "\n"
            "=================================== FAILURES ===================================\n"
            "___________________ test_data_consistency ______________________________________\n\n"
            "    def test_data_consistency():\n"
            "        user = create_user(name='test')\n"
            ">       assert user.profile.settings == default_settings()\n"
            "E       AssertionError: assert {'theme': 'dark'} == {'theme': 'light'}\n"
            "E         Differing items:\n"
            "E         {'theme': 'dark'} != {'theme': 'light'}\n\n"
            "tests/test_integration.py:42: AssertionError\n"
            "=========================== short test summary info ============================\n"
            "FAILED tests/test_integration.py::test_data_consistency - AssertionError: ...\n"
            "========================= 26 passed, 1 failed in 3.42s ========================\n"
        ),
        "must_preserve": [
            "26 passed",
            "1 failed",
            "test_data_consistency",   # failed test name
            "AssertionError",          # error type
            "theme",                   # error detail
        ],
        "min_compression": 0.50,  # should compress significantly
    },
    {
        "name": "pytest_all_pass",
        "category": "test_runner",
        "command": "pytest tests/ -q",
        "raw_output": (
            "47 passed in 2.31s\n"
        ),
        "must_preserve": ["47 passed"],
        "min_compression": 0.0,  # already minimal
    },

    # --- npm install ---
    {
        "name": "npm_install_verbose",
        "category": "package_install",
        "command": "npm install",
        "raw_output": (
            "npm warn deprecated inflight@1.0.6: This module is not supported\n"
            "npm warn deprecated glob@7.2.3: See migration docs\n"
            "npm warn deprecated rimraf@3.0.2: See migration docs\n"
            "\n"
            "added 145 packages, and audited 146 packages in 8s\n\n"
            "12 packages are looking for funding\n"
            "  run `npm fund` for details\n\n"
            "found 0 vulnerabilities\n"
        ),
        "must_preserve": [
            "145 packages",
            "0 vulnerabilities",
        ],
        "min_compression": 0.0,  # Already compact output, compression may not exceed threshold
    },

    # --- Token preservation (security) ---
    {
        "name": "output_contains_aws_key",
        "category": "security",
        "command": "git log --format=%B -1",
        "raw_output": (
            "Fix config loading\n\n"
            "Accidentally committed: AKIAIOSFODNN7EXAMPLE\n"
            "Also found: sk-proj-abc123def456\n"
        ),
        "must_preserve": [
            "AKIAIOSFODNN7EXAMPLE",   # AWS key must never be stripped
            "sk-proj-abc123def456",   # OpenAI key must never be stripped
        ],
        "min_compression": 0.0,
    },
    {
        "name": "output_contains_github_pat",
        "category": "security",
        "command": "env",
        "raw_output": "GITHUB_TOKEN=ghp_ABCDEFghijklmnopqrstuvwxyz0123456789\nPATH=/usr/bin\n",
        "must_preserve": ["ghp_ABCDEFghijklmnopqrstuvwxyz0123456789"],
        "min_compression": 0.0,
    },
    {
        "name": "output_contains_slack_token",
        "category": "security",
        "command": "cat .env",
        "raw_output": "SLACK_TOKEN=xoxb-FAKE-TEST-FIXTURE-00000\nDEBUG=true\n",
        "must_preserve": ["xoxb-FAKE-TEST-FIXTURE-00000"],
        "min_compression": 0.0,
    },

    # --- Error preservation ---
    {
        "name": "command_not_found",
        "category": "error",
        "command": "unknowncmd --version",
        "raw_output": "bash: unknowncmd: command not found\n",
        "must_preserve": ["command not found", "unknowncmd"],
        "min_compression": 0.0,
    },
    {
        "name": "permission_denied",
        "category": "error",
        "command": "cat /etc/shadow",
        "raw_output": "cat: /etc/shadow: Permission denied\n",
        "must_preserve": ["Permission denied", "/etc/shadow"],
        "min_compression": 0.0,
    },

    # --- ls / directory listing ---
    {
        "name": "ls_large_directory",
        "category": "directory_listing",
        "command": "ls -la",
        "raw_output": "\n".join(
            [f"-rw-r--r--  1 user  staff  {1024+i*100}  Apr  9 10:00  file_{i:03d}.py"
             for i in range(60)]
        ) + "\n",
        "must_preserve": ["file_000.py"],  # first entry preserved; last truncated by design
        "min_compression": 0.10,  # 60-entry dir truncated to 50 = ~16% savings
    },

    # --- tee-on-failure (Unit 1) ---
    # When a command fails, the full raw output is returned verbatim so the
    # user can debug. These fixtures exercise the predicate + passthrough path.
    {
        "name": "tee_pytest_nonzero_exit",
        "category": "tee_on_failure",
        "command": "pytest tests/",
        # Realistic pytest failure output — would normally compress, but must
        # return raw because the exit code is non-zero.
        "raw_output": (
            "============================= test session starts ==============================\n"
            "platform darwin -- Python 3.11.0, pytest-7.4.0, pluggy-1.0.0\n"
            "rootdir: /work\n"
            "collected 60 items\n\n"
            + "tests/test_a.py .......\n" * 40
            + "tests/test_b.py F\n\n"
            "=================================== FAILURES ===================================\n"
            "__________________________________ test_bug _____________________________________\n"
            ">       assert foo == bar\n"
            "E       AssertionError\n"
            "=========================== 1 failed, 40 passed in 1.23s ========================\n"
        ),
        "returncode": 1,
        "stderr": "",
        "expect_raw_passthrough": True,
    },
    {
        "name": "tee_linter_stderr_error_exit_zero",
        "category": "tee_on_failure",
        "command": "ruff check .",
        # Linter prints noisy diagnostics on stdout but exits 0 because it
        # only reports warnings. An "error:" marker on stderr still means
        # the user needs the raw detail.
        "raw_output": (
            "src/mod_a.py:10:5: F401 unused import\n"
            "src/mod_b.py:22:1: E501 line too long\n"
            "src/mod_c.py:44:9: W293 whitespace\n"
            "src/mod_d.py:10:5: F401 unused import\n"
            "src/mod_e.py:22:1: E501 line too long\n"
            "src/mod_f.py:44:9: W293 whitespace\n"
            "Found 6 errors.\n"
        ),
        "returncode": 0,
        "stderr": "error: configuration file ruff.toml has a deprecated option\n",
        "expect_raw_passthrough": True,
    },
    # --- lint handler (Unit 3a) ---
    {
        "name": "lint_ruff_typical",
        "category": "lint",
        "command": "ruff check .",
        "raw_output": (
            "src/auth.py:10:5: F401 [*] `os` imported but unused\n"
            "src/auth.py:22:1: E501 Line too long (124 > 100)\n"
            "src/auth.py:44:9: W293 Whitespace on blank line\n"
            "src/api.py:12:5: F401 [*] `json` imported but unused\n"
            "src/api.py:56:1: E501 Line too long (118 > 100)\n"
            "src/api.py:78:1: E501 Line too long (115 > 100)\n"
            "src/models.py:8:5: F401 [*] `typing.Any` imported but unused\n"
            "src/models.py:33:1: E501 Line too long (108 > 100)\n"
            "src/models.py:44:9: W293 Whitespace on blank line\n"
            "src/views.py:14:5: F401 [*] `datetime` imported but unused\n"
            "src/views.py:28:1: E501 Line too long (102 > 100)\n"
            "src/views.py:99:9: W293 Whitespace on blank line\n"
            "Found 12 errors.\n"
            "[*] 4 fixable with the `--fix` option.\n"
        ),
        "must_preserve": ["F401", "E501", "Found 12 errors"],
        "min_compression": 0.30,
    },
    {
        "name": "lint_with_credential_leak",
        "category": "lint",
        "command": "pylint src/",
        # Pylint output that accidentally echoes a GitHub PAT in a disabled
        # warning message — the credential must survive compression via the
        # pre-scan re-injection path.
        "raw_output": (
            "************* Module src.secrets_demo\n"
            "src/secrets_demo.py:4:0: C0114 (missing-module-docstring)\n"
            "src/secrets_demo.py:7:0: W0611 Unused variable 'token'\n"
            "src/secrets_demo.py:8:0: C0103 Variable name doesn't conform: SECRET_TOKEN=ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789\n"
            "src/secrets_demo.py:12:0: E1101 Instance has no 'save' member\n"
            "src/secrets_demo.py:18:0: W0611 Unused variable 'x'\n"
            "src/secrets_demo.py:22:0: C0114 missing-module-docstring\n"
            "src/secrets_demo.py:33:0: W0612 Unused import\n"
            "src/secrets_demo.py:44:0: E0602 Undefined variable\n"
            "src/secrets_demo.py:55:0: C0103 Bad name\n"
            "src/secrets_demo.py:66:0: W0611 Unused variable\n"
            "src/secrets_demo.py:77:0: W0612 Unused\n"
            "Your code has been rated at 3.50/10\n"
        ),
        "must_preserve": ["ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"],
        "min_compression": 0.0,  # tolerate any ratio — credential is the priority
    },
    # --- logs handler (Unit 3b) ---
    {
        "name": "logs_tail_repeated_lines",
        "category": "logs",
        "command": "tail -n 200 app.log",
        "raw_output": (
            "[2026-04-11 10:00:01] INFO request=GET /api/users status=200\n"
            + "[2026-04-11 10:00:02] INFO heartbeat ok\n" * 40
            + "[2026-04-11 10:01:00] WARN slow query 812ms\n"
            + "[2026-04-11 10:01:01] INFO heartbeat ok\n" * 30
            + "[2026-04-11 10:02:00] ERROR connection reset\n"
        ),
        "must_preserve": ["slow query", "connection reset"],
        "min_compression": 0.50,
    },
    {
        "name": "logs_repeated_credential_safety",
        "category": "logs",
        "command": "tail -n 100 debug.log",
        # 5 consecutive identical lines that EACH contain the same AWS access
        # key. Compression collapses the run, but the preservation scan must
        # keep at least one verbatim copy so the key survives.
        "raw_output": (
            "[2026-04-11 10:00:00] DEBUG worker starting\n"
            + "[2026-04-11 10:00:01] DEBUG config AWS_KEY=AKIAIOSFODNN7EXAMPLE loaded\n" * 5
            + "[2026-04-11 10:00:05] INFO worker ready\n"
            + "[2026-04-11 10:00:06] INFO processing batch 1\n" * 30
            + "[2026-04-11 10:00:40] INFO batch 1 complete\n"
        ),
        "must_preserve": ["AKIAIOSFODNN7EXAMPLE"],
        "min_compression": 0.0,  # safety is the only assertion that matters
    },
    # --- tree handler (Unit 3c) ---
    {
        "name": "tree_deep_project",
        "category": "tree",
        "command": "tree",
        "raw_output": (
            "project\n"
            "├── src\n"
            "│   ├── auth\n"
            "│   │   ├── login.py\n"
            "│   │   ├── tokens.py\n"
            "│   │   └── helpers\n"
            "│   │       ├── validation.py\n"
            "│   │       └── crypto.py\n"
            "│   ├── api\n"
            "│   │   ├── routes.py\n"
            "│   │   └── middleware\n"
            "│   │       └── auth.py\n"
            "│   └── models\n"
            "│       ├── user.py\n"
            "│       └── session.py\n"
            "├── tests\n"
            "│   ├── unit\n"
            "│   │   ├── test_auth.py\n"
            "│   │   └── test_api.py\n"
            "│   └── integration\n"
            "│       ├── conftest.py\n"
            "│       └── test_flow.py\n"
            "├── docs\n"
            "│   ├── README.md\n"
            "│   └── api\n"
            "│       └── reference.md\n"
            "├── scripts\n"
            "│   └── deploy.sh\n"
            "├── setup.py\n"
            "└── README.md\n"
            "\n"
            "12 directories, 16 files\n"
        ),
        "must_preserve": ["project", "src", "tests", "docs"],
        "min_compression": 0.15,
    },
    {
        "name": "tree_with_credential_in_filename",
        "category": "tree",
        "command": "tree",
        # A deeply nested secret file must still surface (via preservation scan)
        # even if the containing directories sit beyond depth 2.
        "raw_output": (
            "secrets\n"
            "├── tier1\n"
            "│   ├── tier2\n"
            "│   │   ├── tier3\n"
            "│   │   │   └── ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789.txt\n"
            "│   │   └── other.txt\n"
            "│   └── readme.md\n"
            "├── tier1b\n"
            "│   └── notes.md\n"
            "├── tier1c\n"
            "│   ├── a.txt\n"
            "│   └── b.txt\n"
            "├── tier1d\n"
            "│   └── c.txt\n"
            "├── tier1e\n"
            "│   └── d.txt\n"
            "├── tier1f\n"
            "│   └── e.txt\n"
            "├── tier1g\n"
            "│   └── f.txt\n"
            "├── tier1h\n"
            "│   └── g.txt\n"
            "├── tier1i\n"
            "│   └── h.txt\n"
            "├── tier1j\n"
            "│   └── i.txt\n"
            "├── tier1k\n"
            "│   └── j.txt\n"
            "└── tier1l\n"
            "    └── k.txt\n"
            "\n"
            "15 directories, 14 files\n"
        ),
        "must_preserve": ["ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"],
        "min_compression": 0.0,
    },
    # --- progress handler (Unit 3d) ---
    {
        "name": "progress_docker_build_typical",
        "category": "progress",
        "command": "docker build -t myimg .",
        "raw_output": (
            "#1 [internal] load build definition from Dockerfile\n"
            "#1 transferring dockerfile: 500B done\n"
            "#1 DONE 0.0s\n"
            "#2 [internal] load .dockerignore\n"
            "#2 transferring context: 200B done\n"
            "#2 DONE 0.0s\n"
            "#3 [internal] load metadata for docker.io/library/python:3.11-slim\n"
            "#3 DONE 0.3s\n"
            "#4 [1/6] FROM docker.io/library/python:3.11-slim\n"
            "#4 resolve docker.io/library/python:3.11-slim\n"
            "#4 sha256:abc123\n"
            "#4 DONE 0.1s\n"
            "#5 [internal] load build context\n"
            "#5 transferring context: 12.34kB done\n"
            "#5 DONE 0.0s\n"
            "#6 [2/6] WORKDIR /app\n"
            "#6 DONE 0.0s\n"
            "#7 [3/6] COPY requirements.txt .\n"
            "#7 DONE 0.0s\n"
            "#8 [4/6] RUN pip install -r requirements.txt\n"
            "#8 Downloading flask-2.3.3-py3-none-any.whl\n"
            "#8 Downloading werkzeug-2.3.7-py3-none-any.whl\n"
            "#8 Collecting flask\n"
            "#8 Collecting werkzeug\n"
            "#8 Installing collected packages: werkzeug, flask\n"
            "#8 Successfully installed flask-2.3.3 werkzeug-2.3.7\n"
            "#8 DONE 5.4s\n"
            "#9 [5/6] COPY . .\n"
            "#9 DONE 0.0s\n"
            "#10 [6/6] CMD [\"python\", \"app.py\"]\n"
            "#10 DONE 0.0s\n"
            "#11 exporting to image\n"
            "#11 exporting layers\n"
            "#11 exporting manifest sha256:def456\n"
            "#11 naming to docker.io/library/myimg\n"
            "#11 DONE 0.1s\n"
        ),
        "must_preserve": ["Successfully installed", "naming to"],
        "min_compression": 0.30,
    },
    {
        "name": "progress_docker_build_with_error",
        "category": "progress",
        "command": "docker build -t badimg .",
        "raw_output": (
            "#1 [internal] load build definition from Dockerfile\n"
            "#1 DONE 0.0s\n"
            "#2 [internal] load .dockerignore\n"
            "#2 DONE 0.0s\n"
            "#3 [internal] load metadata\n"
            "#3 DONE 0.2s\n"
            "#4 [1/4] FROM docker.io/library/alpine:3.18\n"
            "#4 DONE 0.0s\n"
            "#5 [2/4] RUN apk add --no-cache curl\n"
            "#5 0.123 fetch https://dl-cdn.alpinelinux.org/alpine/v3.18/main/x86_64/APKINDEX.tar.gz\n"
            "#5 0.456 Downloading curl\n"
            "#5 0.789 Installing curl\n"
            "#5 1.234 ERROR: unable to select packages: curl-nonexistent\n"
            "#5 ERROR: failed to solve: process \"apk add curl-nonexistent\" did not complete successfully: exit code: 1\n"
            "#5 DONE 1.3s\n"
            "#6 [3/4] RUN echo 'hello'\n"
            "#6 DONE 0.0s\n"
            "#7 [4/4] CMD [\"sh\"]\n"
            "#7 DONE 0.0s\n"
            "failed to solve: process did not complete successfully: exit code: 1\n"
        ),
        "must_preserve": ["ERROR", "curl-nonexistent", "failed to solve"],
        "min_compression": 0.20,
    },
    # --- list handler (Unit 3e) ---
    {
        "name": "list_pip_large",
        "category": "list",
        "command": "pip list",
        "raw_output": (
            "Package            Version\n"
            "------------------ ---------\n"
            + "\n".join(f"pkg_{i:03d}            {i}.0.0" for i in range(40))
            + "\n"
        ),
        "must_preserve": ["pkg_000", "Package"],
        "min_compression": 0.30,
    },
    {
        "name": "list_docker_ps_many_containers",
        "category": "list",
        "command": "docker ps",
        "raw_output": (
            "CONTAINER ID   IMAGE           COMMAND       CREATED       STATUS       PORTS     NAMES\n"
            + "\n".join(
                f"abc{i:09d}   ubuntu:22.04    \"/bin/bash\"   {i} hours ago Up {i} hours            ctr_{i:02d}"
                for i in range(30)
            )
            + "\n"
        ),
        "must_preserve": ["CONTAINER", "ctr_00"],
        "min_compression": 0.40,
    },
    # --- B3 regression: Dutch error line inside npm output must survive ---
    {
        "name": "npm_install_dutch_fout_preserved",
        "category": "npm_install",
        "command": "npm install",
        "raw_output": (
            "npm WARN deprecated foo@1.0.0: use bar instead\n"
            + "added 42 packages from 30 contributors\n" * 3
            + "audited 100 packages in 1.2s\n"
            + "fout: kan package niet installeren: vue-router\n"
            + "found 0 vulnerabilities\n" * 2
            + "some filler line\n" * 20
        ),
        "must_preserve": ["fout", "vue-router"],
        "min_compression": 0.0,
    },
    # --- B4 regression: filename containing "nothing to commit" ---
    {
        "name": "git_status_filename_nothing_to_commit",
        "category": "git",
        "command": "git status",
        "raw_output": (
            "On branch main\n"
            "Your branch is up to date with 'origin/main'.\n\n"
            "Untracked files:\n"
            "  (use \"git add <file>...\" to include in what will be committed)\n"
            "\tnothing to commit.txt\n"
            "\tsecret.key\n"
            "\tcredentials.json\n\n"
            "nothing added to commit but untracked files present (use \"git add\" to track)\n"
        ),
        "must_preserve": ["nothing to commit.txt", "secret.key", "credentials.json"],
        "must_not_contain": ["branch: main, clean"],
        "min_compression": 0.0,
    },
    # --- C6 regression: "Found N errors" must be summary, not error count ---
    {
        "name": "build_found_n_errors_classified_as_summary",
        "category": "build",
        "command": "tsc --noEmit",
        "raw_output": (
            "src/a.ts(1,1): error TS2322: type mismatch\n" * 3
            + "src/b.ts(2,1): error TS2304: unknown name\n" * 3
            + "src/c.ts(3,1): error TS2322: type mismatch\n" * 3
            + "src/d.ts(4,1): error TS2339: no such member\n" * 3
            + "Found 12 errors in 4 files.\n"
            + "Errors  Files\n"
            + "     3  src/a.ts\n"
            + "     3  src/b.ts\n"
            + "     3  src/c.ts\n"
            + "     3  src/d.ts\n"
        ),
        "must_preserve": ["TS2322", "Found 12 errors"],
        "must_not_contain": ["13 error/warning", "14 error/warning", "15 error/warning", "16 error/warning"],
        "min_compression": 0.0,
    },
    # --- adversarial: git status porcelain v2 must route to raw ---
    {
        "name": "git_status_porcelain_v2_routes_to_raw",
        "category": "git",
        "command": "git status --porcelain=v2 --branch",
        "raw_output": (
            "# branch.oid 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b\n"
            "# branch.head main\n"
            "# branch.upstream origin/main\n"
            "# branch.ab +0 -0\n"
            "1 .M N... 100644 100644 100644 abcd1234 abcd1234 src/app.py\n"
            "1 .M N... 100644 100644 100644 ef567890 ef567890 src/db.py\n"
            "1 M. N... 100644 100644 100644 11111111 22222222 src/staged.py\n"
            "? untracked.txt\n"
            "? .env.new\n"
        ),
        "must_preserve": ["src/app.py", "src/db.py", "src/staged.py", "untracked.txt", ".env.new"],
        "must_not_contain": ["branch: ?"],
        "min_compression": 0.0,
    },
    # --- adversarial: git status -s must route to raw ---
    {
        "name": "git_status_short_routes_to_raw",
        "category": "git",
        "command": "git status -sb",
        "raw_output": (
            "## main...origin/main [ahead 2]\n"
            " M src/handler.py\n"
            "MM src/config.py\n"
            "?? tmp/debug.log\n"
            "?? .secrets.env\n"
        ),
        "must_preserve": ["src/handler.py", "src/config.py", "tmp/debug.log", ".secrets.env"],
        "must_not_contain": ["branch: ?"],
        "min_compression": 0.0,
    },
    # --- adversarial: preserved line substring of compressed line still surfaces ---
    {
        "name": "lint_preserved_line_inside_longer_compressed_line",
        "category": "lint",
        "command": "eslint .",
        "raw_output": (
            "  1. src/app.py E501 too long - see report: disk full\n"
            "  2. src/app.py E501 too long\n"
            "  3. src/app.py E501 too long\n"
            "  4. src/db.py E501 too long\n"
            "  5. src/api.py E501 too long\n"
            "  6. src/auth.py E501 too long\n"
            "  7. src/io.py E501 too long\n"
            "  8. src/fs.py E501 too long\n"
            "  9. src/net.py E501 too long\n"
            " 10. src/log.py E501 too long\n"
            " 11. src/ui.py E501 too long\n"
            " 12. src/ops.py E501 too long\n"
            " 13. fatal: disk full\n"
        ),
        # `fatal: disk full` is a preservation trigger and must land as its
        # own line in the compressed output, even though the phrase happens
        # to be contained in the first lint sample line.
        "must_preserve": ["fatal: disk full"],
        "min_compression": 0.0,
    },
    # --- adversarial: "no errors" substring in a real error line ---
    {
        "name": "build_no_errors_phrase_in_real_error_line",
        "category": "build",
        "command": "tsc --noEmit",
        "raw_output": (
            "[1/5] Compiling src/index.ts\n"
            "src/index.ts(42,10): error TS2322: Expected 'no errors' but found 'undefined'.\n"
            "src/index.ts(43,1): error TS2345: Argument type mismatch.\n"
            "[2/5] Compiling src/auth.ts\n"
            "src/auth.ts(10,5): error TS2322: Type 'string' is not assignable.\n"
            "src/auth.ts(12,5): error TS2344: Constraint violation.\n"
            "src/auth.ts(15,5): error TS2339: Property 'foo' does not exist.\n"
            "src/auth.ts(18,5): error TS2341: Private access.\n"
            "src/auth.ts(20,5): error TS2342: Index signature missing.\n"
            "src/auth.ts(22,5): error TS2343: Generic type unresolved.\n"
            "src/auth.ts(24,5): error TS2344: Constraint violation.\n"
            "src/auth.ts(26,5): error TS2345: Argument mismatch.\n"
            "src/auth.ts(28,5): error TS2346: Call signature mismatch.\n"
            "src/auth.ts(30,5): error TS2347: Untyped function call.\n"
            "src/auth.ts(32,5): error TS2348: Constructor signature missing.\n"
            "src/auth.ts(34,5): error TS2349: Callable expected.\n"
            "[3/5] Done in 0.4s\n"
            "Found 14 errors in 2 files. Watching for file changes.\n"
        ),
        "must_preserve": ["TS2322", "Expected 'no errors'", "Found 14 errors"],
        # Handler header must NOT undercount the first error line; "no
        # errors" is part of a real compiler message here.
        "must_not_contain": ["13 error/warning lines"],
        "min_compression": 0.0,
    },
    # --- R3 correctness: "N0 errors" must not match the "0 errors" prefix ---
    {
        "name": "build_ten_errors_not_classified_as_clean",
        "category": "build",
        "command": "tsc --noEmit",
        "raw_output": (
            "src/a.ts(1,1): error TS2322: type mismatch\n" * 3
            + "src/b.ts(2,1): error TS2304: unknown name\n" * 3
            + "src/c.ts(3,1): error TS2322: type mismatch\n" * 2
            + "src/d.ts(4,1): error TS2339: no such member\n" * 2
            + "10 errors found across 4 files\n"
        ),
        "must_preserve": ["TS2322", "10 errors found"],
        "min_compression": 0.0,
    },
    # --- adversarial: FOUT web-perf text must not false-trigger preservation ---
    {
        "name": "foreign_fout_web_perf_false_positive_avoided",
        "category": "lint",
        "command": "ruff check src/",
        "raw_output": (
            "src/perf.js:1:1: E501 line exceeds 80 characters\n"
            "src/perf.js:2:1: E501 line exceeds 80 characters\n"
            "src/perf.js:3:1: E501 line exceeds 80 characters\n"
            "src/perf.js:4:1: E501 line exceeds 80 characters\n"
            "src/perf.js:5:1: E501 line exceeds 80 characters\n"
            "src/perf.js:6:1: E501 line exceeds 80 characters\n"
            "src/perf.js:7:1: E501 FOUT detected 340ms unstyled window on /index.html\n"
            "src/perf.js:8:1: E501 line exceeds 80 characters\n"
            "src/perf.js:9:1: E501 line exceeds 80 characters\n"
            "src/perf.js:10:1: E501 line exceeds 80 characters\n"
            "src/perf.js:11:1: E501 line exceeds 80 characters\n"
            "src/perf.js:12:1: E501 line exceeds 80 characters\n"
            "Found 12 errors.\n"
        ),
        # FOUT (Flash Of Unstyled Text — web-perf term) appears in a real
        # lint finding on line 7. The tightened foreign-error pattern
        # requires a trailing colon, so `FOUT detected 340ms` does NOT
        # trigger the preservation path. The lint handler groups all 12
        # findings under E501, picks line 1 as the sample, and drops
        # lines 2-12 including the FOUT one — so the FOUT text never
        # surfaces in the compressed output.
        "must_preserve": ["E501", "Found 12 errors"],
        "must_not_contain": ["FOUT detected 340ms unstyled window"],
        "min_compression": 0.30,
    },
    # --- C-R2-5 regression: blocked prefix must not shadow real lint code ---
    {
        "name": "lint_cve_does_not_shadow_real_code",
        "category": "lint",
        "command": "ruff check .",
        "raw_output": (
            "src/audit.py:10:5: E501 line too long (CVE2024 referenced)\n"
            "src/audit.py:12:5: E501 another long line\n"
            "src/audit.py:14:5: E501 third long line\n"
            "src/audit.py:16:5: F401 unused import\n"
            "src/audit.py:18:5: F401 unused import\n"
            "src/audit.py:20:5: F401 unused import\n"
            "src/audit.py:22:5: F401 unused import\n"
            "src/audit.py:24:5: F401 unused import\n"
            "Found 8 errors.\n"
        ),
        "must_preserve": ["F401", "E501"],
        "must_not_contain": ["CVE2024 x"],
        "min_compression": 0.0,
    },
    # --- C-R2-4 regression: clean tsc build must not mislabel as errors ---
    {
        "name": "build_tsc_clean_not_mislabeled",
        "category": "build",
        "command": "tsc --noEmit",
        "raw_output": (
            "src/a.ts:1: checking\n" * 15
            + "src/b.ts:1: checking\n" * 10
            + "Found 0 errors. Watching for file changes.\n"
        ),
        "must_preserve": ["Found 0 errors"],
        # Must NOT emit a misleading "1 error/warning lines" header
        "must_not_contain": ["1 error/warning lines", "2 error/warning lines"],
        "min_compression": 0.0,
    },

    # --- B1 regression: silent build must not fabricate "no errors" ---
    {
        "name": "build_silent_no_error_keywords",
        "category": "build",
        "command": "go build -v ./...",
        # Go build -v emits per-package context with no "error"/"warning"
        # keywords on a silent build. The handler used to lie and return
        # "[build output: no errors or warnings detected; output elided]".
        # Fix: return raw so the 10% ratio gate lets it through unchanged.
        "raw_output": "\n".join(
            f"github.com/acme/svc/pkg_{i}" for i in range(30)
        ) + "\n",
        "must_preserve": ["pkg_0", "pkg_29"],
        "min_compression": 0.0,
    },
    # --- B2 regression: non-English stderr must trigger tee passthrough ---
    {
        "name": "tee_german_stderr_error_exit_zero",
        "category": "tee_on_failure",
        "command": "eslint src/",
        "raw_output": "src/foo.js:10:5 some warning that would normally compress " * 20 + "\n",
        "returncode": 0,
        "stderr": "Fehler: Konfigurationsdatei nicht gefunden\n",
        "expect_raw_passthrough": True,
    },
    {
        "name": "tee_chinese_stderr_error_exit_zero",
        "category": "tee_on_failure",
        "command": "pylint src/",
        "raw_output": "src/foo.py: C0103 bad name " * 40 + "\n",
        "returncode": 0,
        "stderr": "错误: 配置文件损坏\n",
        "expect_raw_passthrough": True,
    },
    # --- C2 regression: lint must not fake rule codes from CVE/HTTP/RFC ---
    {
        "name": "lint_must_not_treat_cve_as_rule_code",
        "category": "lint",
        "command": "eslint src/",
        "raw_output": (
            "src/audit.js:10:5  error  URL https://cve.mitre.org/cgi-bin/cvename.cgi?name=CVE2024 linked  no-unused-vars\n"
            "src/audit.js:12:5  error  See RFC7231 section 4.3 for details  no-undef\n"
            "src/audit.js:14:5  error  HTTP404 returned on GET  no-console\n"
            "src/audit.js:16:5  error  ISO9001 compliance check failed  eqeqeq\n"
            "src/audit.js:18:5  error  Use MD5 or SHA256 only  no-magic-numbers\n"
            "src/audit.js:20:5  error  plain text response  no-unused-vars\n"
            "src/audit.js:22:5  error  event loop blocked  no-console\n"
            "src/audit.js:24:5  error  stale cache hit  prefer-const\n"
            "src/audit.js:26:5  error  subtle bug here  curly\n"
            "src/audit.js:28:5  error  type coercion  eqeqeq\n"
            "\n✖ 10 problems (10 errors)\n"
        ),
        "must_preserve": ["no-unused-vars", "10 problems"],
        # The handler must NOT produce lines like "CVE2024 x1", "HTTP404 x1",
        # "RFC7231 x1", "ISO9001 x1", "MD5 x1", "SHA x1" — they would be
        # fake rule codes. We verify via must_not_contain.
        "must_not_contain": ["CVE2024 x", "HTTP404 x", "RFC7231 x", "ISO9001 x"],
        "min_compression": 0.0,
    },
    # --- C3 regression: trailing warnings must not hide the pass count ---
    {
        "name": "pytest_trailing_deprecation_warnings_pass_count_survives",
        "category": "test",
        "command": "pytest tests/",
        "raw_output": (
            "============================= test session starts ==============================\n"
            + "tests/test_a.py .\n" * 30
            + "============================== 30 passed in 2.11s ==============================\n"
            + "DeprecationWarning: future-removal 1\n" * 20
        ),
        "must_preserve": ["30 passed"],
        "min_compression": 0.30,
    },
    # --- Sec-2 regression: JWT credential preservation ---
    {
        "name": "logs_jwt_credential_preserved",
        "category": "logs",
        "command": "tail -n 60 auth.log",
        "raw_output": (
            "[2026-04-11] INFO auth start\n"
            + "[2026-04-11] INFO heartbeat ok\n" * 40
            + "[2026-04-11] INFO token issued eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c\n"
            + "[2026-04-11] INFO heartbeat ok\n" * 10
        ),
        "must_preserve": ["eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"],
        "min_compression": 0.0,
    },
    # --- Sec-2 regression: Google API key preservation ---
    {
        "name": "logs_google_api_key_preserved",
        "category": "logs",
        "command": "tail -n 60 app.log",
        "raw_output": (
            "[2026-04-11] DEBUG config loaded\n" * 30
            + "[2026-04-11] INFO GOOGLE_API_KEY=AIzaSyBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abc set\n"
            + "[2026-04-11] DEBUG config loaded\n" * 10
        ),
        "must_preserve": ["AIzaSyBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abc"],
        "min_compression": 0.0,
    },
    # --- SEC-R2-1 regression: credential in OSC 8 hyperlink label text ---
    {
        "name": "logs_osc8_hyperlink_credential_preserved",
        "category": "logs",
        "command": "tail -n 60 audit.log",
        # The AWS access key is embedded as the visible label of an OSC 8
        # hyperlink. The ANSI stripper must preserve the label text so the
        # preservation scan can see it before compression runs.
        "raw_output": (
            "[2026-04-11] INFO audit start\n" * 30
            + "[2026-04-11] INFO token visit \x1b]8;;https://iam.example.com/keys\x07AKIAIOSFODNN7EXAMPLE\x1b]8;;\x07\n"
            + "[2026-04-11] INFO audit continue\n" * 15
        ),
        "must_preserve": ["AKIAIOSFODNN7EXAMPLE"],
        "min_compression": 0.0,
    },

    # --- Sec-2 regression: DB URI with embedded password preservation ---
    {
        "name": "logs_db_uri_credential_preserved",
        "category": "logs",
        "command": "tail -n 50 db.log",
        "raw_output": (
            "[2026-04-11] INFO db connect start\n" * 30
            + "[2026-04-11] ERROR failed: postgres://admin:sUperS3cret@db.internal:5432/prod\n"
            + "[2026-04-11] INFO retry scheduled\n" * 15
        ),
        "must_preserve": ["postgres://admin:sUperS3cret@db.internal:5432/prod"],
        "min_compression": 0.0,
    },

    # --- build handler (Unit 3f) ---
    {
        "name": "build_tsc_with_errors",
        "category": "build",
        "command": "tsc --noEmit",
        "raw_output": (
            "src/api/users.ts(10,5): error TS2322: Type 'string' is not assignable to type 'number'.\n"
            "src/api/users.ts(22,8): error TS2304: Cannot find name 'foo'.\n"
            "src/api/users.ts(44,12): error TS2339: Property 'bar' does not exist on type 'User'.\n"
            "src/lib/helpers.ts(5,1): error TS1005: ',' expected.\n"
            "src/lib/helpers.ts(8,3): error TS2345: Argument of type '{}' is not assignable.\n"
            "src/lib/helpers.ts(12,9): error TS2531: Object is possibly 'null'.\n"
            "src/models/user.ts(3,1): error TS2305: Module has no exported member 'Foo'.\n"
            "src/models/user.ts(15,4): error TS2307: Cannot find module 'bar'.\n"
            "src/index.ts(1,1): error TS6133: 'x' is declared but its value is never read.\n"
            "src/index.ts(5,5): error TS2322: Type 'null' is not assignable to type 'string'.\n"
            "src/index.ts(10,8): error TS2345: Argument of wrong type.\n"
            "src/utils/date.ts(3,1): error TS2300: Duplicate identifier 'parseDate'.\n"
            "Found 12 errors in 5 files.\n"
            "\n"
            "Errors  Files\n"
            "     3  src/api/users.ts\n"
            "     3  src/lib/helpers.ts\n"
            "     2  src/models/user.ts\n"
            "     3  src/index.ts\n"
            "     1  src/utils/date.ts\n"
        ),
        "must_preserve": ["TS2322", "Found 12 errors"],
        # Broken builds naturally do not compress much — we preserve every
        # error line plus the summary. The value of this fixture is the
        # "must_preserve" contract and the "Found N errors" aggregate being
        # routed to the summary bucket (not mis-counted as an error line).
        "min_compression": 0.0,
    },
    {
        "name": "build_vite_clean",
        "category": "build",
        "command": "vite build",
        # Clean build with asset tables — handler should drop all the asset
        # chatter and emit a "no errors detected" marker.
        "raw_output": (
            "vite v5.0.0 building for production...\n"
            "transforming...\n"
            "✓ 240 modules transformed.\n"
            "dist/index.html                  0.47 kB │ gzip:  0.30 kB\n"
            "dist/assets/index-abc.css       12.34 kB │ gzip:  3.00 kB\n"
            "dist/assets/vendor-def.js      220.11 kB │ gzip: 70.50 kB\n"
            "dist/assets/page-home-ghi.js    33.22 kB │ gzip: 11.10 kB\n"
            "dist/assets/page-about-jkl.js   22.11 kB │ gzip:  8.01 kB\n"
            "dist/assets/page-users-mno.js   44.44 kB │ gzip: 14.00 kB\n"
            "dist/assets/page-posts-pqr.js   55.55 kB │ gzip: 17.00 kB\n"
            "dist/assets/page-admin-stu.js   66.66 kB │ gzip: 20.00 kB\n"
            "dist/assets/lib-vwx.js          77.77 kB │ gzip: 22.00 kB\n"
            "dist/assets/polyfill-yz.js      11.11 kB │ gzip:  3.50 kB\n"
            "dist/assets/utils-123.js         8.88 kB │ gzip:  2.50 kB\n"
            "dist/assets/state-456.js         9.99 kB │ gzip:  3.00 kB\n"
            "dist/assets/router-789.js        4.44 kB │ gzip:  1.50 kB\n"
            "dist/assets/ui-abc.js           15.55 kB │ gzip:  5.00 kB\n"
            "dist/assets/icons-def.js         7.77 kB │ gzip:  2.00 kB\n"
            "dist/assets/fonts-ghi.js         3.33 kB │ gzip:  1.20 kB\n"
            "dist/assets/i18n-jkl.js         10.10 kB │ gzip:  3.30 kB\n"
            "dist/assets/hooks-mno.js         5.50 kB │ gzip:  1.80 kB\n"
            "dist/assets/api-pqr.js          12.12 kB │ gzip:  4.00 kB\n"
            "dist/assets/types-stu.js         2.20 kB │ gzip:  0.80 kB\n"
            "dist/assets/test-vwx.js          1.10 kB │ gzip:  0.40 kB\n"
            "✓ built in 4.23s\n"
        ),
        "must_preserve": ["built in"],
        "min_compression": 0.40,
    },
    # --- test-runner extensions (Unit 3g) ---
    {
        "name": "test_exts_playwright_mixed",
        "category": "test_exts",
        "command": "npx playwright test",
        "raw_output": (
            "Running 40 tests using 4 workers\n\n"
            + "  ok [chromium] › tests/login.spec.ts:10:3 › login works\n" * 10
            + "  ok [firefox] › tests/login.spec.ts:10:3 › login works\n" * 10
            + "  ok [webkit] › tests/login.spec.ts:10:3 › login works\n" * 10
            + "  x [chromium] › tests/checkout.spec.ts:22:5 › checkout flow\n"
            "    Error: expect(received).toBe(expected)\n"
            "    Expected: 200\n"
            "    Received: 500\n"
            "  ok [firefox] › tests/checkout.spec.ts:22:5 › checkout flow\n"
            "  x [webkit] › tests/checkout.spec.ts:22:5 › checkout flow\n"
            "    Error: Timeout exceeded\n"
            "    Expected: true\n"
            "    Received: false\n"
            + "  ok [chromium] › tests/misc.spec.ts:5:1 › smoke\n" * 7
            + "\n  38 passed (12.3s)\n  2 failed\n"
        ),
        "must_preserve": ["passed", "failed"],
        "min_compression": 0.30,
    },
    {
        "name": "test_exts_cypress_all_pass",
        "category": "test_exts",
        "command": "cypress run",
        "raw_output": (
            "  Running:  login.cy.ts\n\n"
            + "    ✓ shows login form\n" * 15
            + "    ✓ submits successfully\n" * 15
            + "\n"
            "  30 passing (4s)\n"
        ),
        "must_preserve": ["passing"],
        "min_compression": 0.20,
    },
    {
        "name": "test_exts_mocha_short_should_not_compress",
        "category": "test_exts",
        "command": "mocha test/",
        "raw_output": (
            "  Array\n"
            "    ✓ should return -1 when value not present\n"
            "\n"
            "  1 passing (5ms)\n"
        ),
        "must_preserve": ["passing"],
        "min_compression": 0.0,
    },

    {
        "name": "build_short_should_not_compress",
        "category": "build",
        "command": "tsc --noEmit",
        "raw_output": "Found 0 errors. Watching for file changes.\n",
        "must_preserve": ["Found 0 errors"],
        "min_compression": 0.0,
    },

    {
        "name": "list_short_should_not_compress",
        "category": "list",
        "command": "brew list",
        "raw_output": "git\npython@3.11\nnode\nruff\nvulture\n",
        "must_preserve": ["git", "python@3.11"],
        "min_compression": 0.0,
    },

    {
        "name": "progress_docker_short_should_not_compress",
        "category": "progress",
        "command": "docker build -t tiny .",
        "raw_output": (
            "#1 [internal] load Dockerfile\n"
            "#1 DONE 0.0s\n"
            "#2 [1/1] FROM scratch\n"
            "#2 DONE 0.0s\n"
            "#3 exporting\n"
            "#3 DONE 0.0s\n"
        ),
        "must_preserve": ["FROM scratch"],
        "min_compression": 0.0,
    },

    {
        "name": "tree_shallow_should_not_compress",
        "category": "tree",
        "command": "tree -L 1",
        "raw_output": (
            "project\n"
            "├── src\n"
            "├── tests\n"
            "├── docs\n"
            "├── README.md\n"
            "└── setup.py\n"
            "\n"
            "3 directories, 2 files\n"
        ),
        "must_preserve": ["src", "tests"],
        "min_compression": 0.0,
    },

    {
        "name": "logs_mixed_no_dups_should_not_compress",
        "category": "logs",
        "command": "journalctl -u myservice",
        # 22 different lines, no adjacent duplicates — handler must bail out.
        "raw_output": "\n".join(
            f"[2026-04-11 10:{i:02d}:00] INFO unique event {i} data=foo_{i}"
            for i in range(22)
        ) + "\n",
        "must_preserve": ["event 0", "event 21"],
        "min_compression": 0.0,
    },
    {
        "name": "lint_too_short_should_not_compress",
        "category": "lint",
        "command": "eslint src/",
        "raw_output": (
            "/Users/alex/project/src/foo.js\n"
            "  2:1  error  'foo' is defined but never used  no-unused-vars\n\n"
            "✖ 1 problem (1 error, 0 warnings)\n"
        ),
        "must_preserve": ["no-unused-vars", "1 error"],
        "min_compression": 0.0,  # short input — fall through raw
    },

    {
        "name": "tee_clean_run_exit_zero_no_stderr",
        "category": "tee_on_failure",
        "command": "pytest tests/",
        # Clean run — tee predicate must NOT trigger, so this output is free
        # to compress through the normal pytest handler.
        "raw_output": (
            "============================= test session starts ==============================\n"
            "platform darwin -- Python 3.11.0, pytest-7.4.0, pluggy-1.0.0\n"
            "collected 60 items\n\n"
            + "tests/test_a.py .......\n" * 40
            + "tests/test_b.py .......\n" * 20
            + "\n============================== 60 passed in 1.23s ==============================\n"
        ),
        "returncode": 0,
        "stderr": "",
        "must_preserve": ["60 passed"],
        "min_compression": 0.10,
    },
    # --- v5.8 test-runner extension fixtures ---
    {
        "name": "test_exts_unittest_verbose",
        "category": "test_exts",
        "command": "python3 -m unittest",
        "raw_output": (
            "test_01 (test_module.TestSomething) ... ok\n"
            "test_02 (test_module.TestSomething) ... ok\n"
            "test_03 (test_module.TestSomething) ... ok\n"
            "test_04 (test_module.TestSomething) ... ok\n"
            "test_05 (test_module.TestSomething) ... ok\n"
            "\n"
            "----------------------------------------------------------------------\n"
            "Ran 5 tests in 0.001s\n\n"
            "OK\n"
        ),
        "must_preserve": ["Ran 5", "OK"],
        "min_compression": 0.0,
    },
    {
        "name": "test_exts_unittest_large",
        "category": "test_exts",
        "command": "python3 -m unittest",
        "raw_output": (
            "".join(
                f"test_{i:03d} (test_module.TestSuite.test_case_{i}) ... {'ok' if i % 5 != 0 else 'FAIL'}\n"
                for i in range(60)
            )
            + "\n"
            + "======================================================================\n"
            + "FAIL: test_005 (test_module.TestSuite.test_case_5)\n"
            + "Traceback (most recent call last):\n"
            + "  File test_module.py, line 10, in test_case_5\n"
            + "    self.assertEqual(a, b)\n"
            + "AssertionError: 1 != 2\n"
            + "\n"
            + "----------------------------------------------------------------------\n"
            + "Ran 60 tests in 0.123s\n\n"
            + "FAILED (failures=12)\n"
        ),
        "must_preserve": ["Ran 60", "FAILED", "failures=12"],
        "min_compression": 0.30,
    },
    {
        "name": "test_exts_gradle_verbose",
        "category": "test_exts",
        "command": "gradle test",
        "raw_output": (
            "> Task :compileTestJava\n"
            "> Task :processTestResources\n"
            "> Task :testClasses\n"
            + "".join(
                f"> Task :test\n  com.example.TestClass{i}.testMethod{i} PASSED\n"
                for i in range(40)
            )
            + "\nBUILD SUCCESSFUL in 5s\n"
            + "12 actionable tasks: 12 executed\n"
        ),
        "must_preserve": ["BUILD SUCCESSFUL", "PASSED"],
        "min_compression": 0.30,
    },
    {
        "name": "test_exts_ava_verbose",
        "category": "test_exts",
        "command": "npx ava",
        "raw_output": (
            "".join(
                f"  ✔ test-{i} › should do something meaningful [{i}ms]\n"
                for i in range(50)
            )
            + "\n  50 tests passed\n"
        ),
        "must_preserve": ["50 tests passed"],
        "min_compression": 0.30,
    },
    {
        "name": "test_exts_deno_verbose",
        "category": "test_exts",
        "command": "deno test",
        "raw_output": (
            "running 40 tests from ./test/\n"
            + "".join(
                f"test_{i} ... ok ({i}ms)\n"
                for i in range(40)
            )
            + "\nok | 40 passed | 0 failed (120ms)\n"
        ),
        "must_preserve": ["40 passed", "0 failed"],
        "min_compression": 0.30,
    },
    {
        "name": "test_exts_bun_verbose",
        "category": "test_exts",
        "command": "bun test",
        "raw_output": (
            "".join(
                f"✓ test_{i}.test.ts > test_case_{i} [0.{i:02d}ms]\n"
                for i in range(50)
            )
            + "\n50 pass, 0 fail, 50 expect() calls\nRan all 50 tests across 1 file in 0.12s\n"
        ),
        "must_preserve": ["50 pass", "0 fail"],
        "min_compression": 0.30,
    },
    {
        "name": "test_exts_tox_verbose",
        "category": "test_exts",
        "command": "tox",
        "raw_output": (
            "py311: commands[0] > pytest\n"
            + "".join(
                f"  tests/test_module_{i}.py .........\n"
                for i in range(30)
            )
            + "  300 passed in 2.34s\n\n"
            + "py310: commands[0] > pytest\n"
            + "".join(
                f"  tests/test_module_{i}.py .........\n"
                for i in range(30)
            )
            + "  300 passed in 2.56s\n\n"
            + "  congratulations :)\n"
        ),
        "must_preserve": ["300 passed", "congratulations"],
        "min_compression": 0.30,
    },

    # --- JSON handler ---
    {
        "name": "json_array_tabular",
        "category": "json",
        "command": "jq .",
        "raw_output": json.dumps([
            {"id": 1, "name": "alpha", "status": "active", "value": 100},
            {"id": 2, "name": "beta", "status": "active", "value": 200},
            {"id": 3, "name": "gamma", "status": "inactive", "value": 300},
            {"id": 4, "name": "delta", "status": "active", "value": 400},
            {"id": 5, "name": "epsilon", "status": "active", "value": 500},
        ] * 10, indent=2),
        "must_preserve": ["alpha", "beta", "gamma", "id", "name", "status"],
        "min_compression": 0.40,
    },
    {
        "name": "json_array_tabular_with_credential",
        "category": "json",
        "command": "jq .",
        "raw_output": json.dumps([
            {"id": 1, "name": "alpha", "token": "sk-abc123def456ghi789jkl012mno345pqr678", "status": "active"},
            {"id": 2, "name": "beta", "token": "sk-xyz987wvu654tsr321qpo098nml765kji432", "status": "active"},
            {"id": 3, "name": "gamma", "token": "sk-test123test456test789test012test345", "status": "inactive"},
            {"id": 4, "name": "delta", "token": "sk-live111live222live333live444live555", "status": "active"},
            {"id": 5, "name": "epsilon", "token": "sk-key111key222key333key444key555key", "status": "active"},
        ] * 10, indent=2),
        "must_preserve": ["sk-abc123def456ghi789jkl012mno345pqr678", "sk-xyz987wvu654tsr321qpo098nml765kji432"],
        "min_compression": 0.30,
    },
    {
        "name": "json_too_small",
        "category": "json",
        "command": "jq .",
        "raw_output": json.dumps({"a": 1, "b": 2}),
        "must_preserve": [],
        "min_compression": 0.0,
    },
    {
        "name": "json_object_large",
        "category": "json",
        "command": "python3 -m json.tool",
        "raw_output": json.dumps({f"key_{i}": f"value_{i}_with_some_padding_to_increase_size" for i in range(50)}, indent=2),
        "must_preserve": ["key_0", "key_49"],
        "min_compression": 0.50,
    },
    # --- CSV/TSV handler ---
    {
        "name": "csv_large",
        "category": "csv",
        "command": "csvtool",
        "raw_output": "id,name,status,value\n" + "".join(
            f"{i},item_{i},active,{i * 100}\n" for i in range(100)
        ),
        "must_preserve": ["id", "name", "status", "item_0", "item_14"],
        "min_compression": 0.50,
    },
    {
        "name": "tsv_large",
        "category": "csv",
        "command": "csvtool",
        "raw_output": "id\tname\tstatus\tvalue\n" + "".join(
            f"{i}\titem_{i}\tactive\t{i * 100}\n" for i in range(100)
        ),
        "must_preserve": ["id", "name", "item_0", "item_14"],
        "min_compression": 0.50,
    },
    {
        "name": "csv_with_credential",
        "category": "csv",
        "command": "csvtool",
        "raw_output": "id,name,token,status\n" + "".join(
            f"{i},item_{i},{'sk-abc123def456ghi789jkl012mno345pqr678' if i == 5 else ''},active\n" for i in range(50)
        ),
        "must_preserve": ["sk-abc123def456ghi789jkl012mno345pqr678"],
        "min_compression": 0.30,
    },
    {
        "name": "csv_too_small",
        "category": "csv",
        "command": "csvtool",
        "raw_output": "id,name\n1,alpha\n2,beta\n",
        "must_preserve": [],
        "min_compression": 0.0,
    },
    # --- Stack trace handler ---
    {
        "name": "stack_trace_python",
        "category": "stack_trace",
        "command": "python3 app.py",
        "raw_output": (
            "Traceback (most recent call last):\n"
            + "".join(
                f'  File "/app/module_{i}.py", line {i * 10}, in func_{i}\n'
                f"    result = do_something_{i}()\n"
                for i in range(50)
            )
            + "ValueError: invalid configuration value\n"
        ),
        "must_preserve": ["Traceback", "ValueError", "module_0"],
        "min_compression": 0.40,
    },
    {
        "name": "stack_trace_java",
        "category": "stack_trace",
        "command": "java -jar app.jar",
        "raw_output": (
            "Exception in thread \"main\" java.lang.NullPointerException\n"
            + "".join(
                f"\tat com.example.service.Service{i}.method(Service{i}.java:{i * 20})\n"
                for i in range(60)
            )
        ),
        "must_preserve": ["NullPointerException", "Service0"],
        "min_compression": 0.40,
    },
    {
        "name": "stack_trace_with_credential",
        "category": "stack_trace",
        "command": "python3 app.py",
        "raw_output": (
            "Traceback (most recent call last):\n"
            '  File "/app/config.py", line 10, in load_config\n'
            '    token = "ghp_abcdefghijklmnopqrstuvwxyz0123456789AB"\n'
            + "".join(
                f'  File "/app/module_{i}.py", line {i * 10}, in func_{i}\n'
                f"    result = do_something_{i}()\n"
                for i in range(50)
            )
            + "ValueError: invalid configuration value\n"
        ),
        "must_preserve": ["ghp_abcdefghijklmnopqrstuvwxyz0123456789AB", "ValueError"],
        "min_compression": 0.30,
    },
    {
        "name": "stack_trace_too_small",
        "category": "stack_trace",
        "command": "python3 app.py",
        "raw_output": (
            "Traceback (most recent call last):\n"
            '  File "/app/main.py", line 5, in <module>\n'
            "    raise ValueError('test')\n"
            "ValueError: test\n"
        ),
        "must_preserve": ["ValueError"],
        "min_compression": 0.0,
    },
    # --- Kubernetes handler ---
    {
        "name": "k8s_get_pods_large",
        "category": "k8s",
        "command": "kubectl get pods",
        "raw_output": (
            "NAME                             READY   STATUS    RESTARTS   AGE\n"
            + "".join(
                f"app-pod-{i:04d}                 1/1     Running   0          {i}h\n"
                for i in range(100)
            )
        ),
        "must_preserve": ["NAME", "READY", "STATUS", "app-pod-0000", "app-pod-0014"],
        "min_compression": 0.50,
    },
    {
        "name": "k8s_get_pods_with_credential",
        "category": "k8s",
        "command": "kubectl get pods",
        "raw_output": (
            "NAME                             READY   STATUS    RESTARTS   AGE\n"
            + "".join(
                f"app-pod-{i:04d}                 1/1     Running   0          {i}h\n"
                for i in range(50)
            )
            + "secret-pod                          1/1     Running   0          1h\n"
            + "token: sk-abc123def456ghi789jkl012mno345pqr678\n"
        ),
        "must_preserve": ["sk-abc123def456ghi789jkl012mno345pqr678", "app-pod-0000"],
        "min_compression": 0.30,
    },
    {
        "name": "k8s_too_small",
        "category": "k8s",
        "command": "kubectl get pods",
        "raw_output": (
            "NAME                READY   STATUS    RESTARTS   AGE\n"
            "app-pod-0001        1/1     Running   0          1h\n"
            "app-pod-0002        1/1     Running   0          2h\n"
        ),
        "must_preserve": [],
        "min_compression": 0.0,
    },
    # --- Cloud CLI handler ---
    {
        "name": "cloud_cli_gcloud_large",
        "category": "cloud_cli",
        "command": "gcloud compute instances list",
        "raw_output": (
            "NAME            ZONE           MACHINE_TYPE   PREEMPTIBLE  INTERNAL_IP  EXTERNAL_IP     STATUS\n"
            + "".join(
                f"instance-{i:04d}  us-central1-a  e2-medium      False        10.0.{i // 256}.{i % 256}  35.{i // 1000}.{i % 1000}.{i % 256}  RUNNING\n"
                for i in range(100)
            )
        ),
        "must_preserve": ["NAME", "STATUS", "instance-0000", "RUNNING"],
        "min_compression": 0.40,
    },
    {
        "name": "cloud_cli_aws_large",
        "category": "cloud_cli",
        "command": "aws ec2 describe-instances",
        "raw_output": (
            "InstanceId          InstanceType   State    LaunchTime\n"
            + "".join(
                f"i-{i:016x}  t2.micro       running  2026-01-{(i % 28) + 1:02d}\n"
                for i in range(100)
            )
        ),
        "must_preserve": ["InstanceId", "running", "i-0000000000000000"],
        "min_compression": 0.40,
    },
    {
        "name": "cloud_cli_with_credential",
        "category": "cloud_cli",
        "command": "gcloud compute instances list",
        "raw_output": (
            "NAME            ZONE           MACHINE_TYPE   STATUS\n"
            + "".join(
                f"instance-{i:04d}  us-central1-a  e2-medium      RUNNING\n"
                for i in range(50)
            )
            + "AKIAIOSFODNN7EXAMPLE\n"
        ),
        "must_preserve": ["AKIAIOSFODNN7EXAMPLE", "instance-0000"],
        "min_compression": 0.30,
    },
    {
        "name": "cloud_cli_too_small",
        "category": "cloud_cli",
        "command": "gcloud compute instances list",
        "raw_output": "NAME    ZONE    STATUS\ninst1   us-a    RUNNING\n",
        "must_preserve": [],
        "min_compression": 0.0,
    },

    # --- v5.9 search results handler ---
    {
        "name": "grep_many_matches",
        "category": "search_results",
        "command": "rg --line-number 'def' src/",
        "raw_output": "\n".join(
            f"src/module_{f:03d}.py:{l}:    def func_{f}_{l}(self):"
            for f in range(20)
            for l in range(1, 11)
        ),
        "must_preserve": ["module_000", "module_019"],
        "min_compression": 0.50,
    },
    {
        "name": "grep_few_matches",
        "category": "search_results",
        "command": "rg --line-number 'TODO' src/",
        "raw_output": (
            "src/main.py:42:# TODO: fix this later\n"
            "src/utils.py:15:# TODO: add error handling\n"
            "src/models.py:89:# TODO: refactor\n"
        ),
        "must_preserve": ["TODO", "main.py", "utils.py", "models.py"],
        "min_compression": 0.0,
    },
    {
        "name": "grep_with_credential",
        "category": "search_results",
        "command": "rg --line-number 'key' .",
        "raw_output": "\n".join(
            f"config_{i:03d}.py:1:api_key = 'sk-{'a' * 20}{i:03d}'"
            for i in range(40)
        ) + "\nconfig_000.py:2:token = 'AKIAIOSFODNN7EXAMPLE'",
        "must_preserve": ["AKIAIOSFODNN7EXAMPLE"],
        "min_compression": 0.30,
    },
    {
        "name": "grep_non_file_format",
        "category": "search_results",
        "command": "rg --line-number 'test' README.md",
        "raw_output": "This is a README file.\nIt has some text.\nBut no file:line format.\n" * 15,
        "must_preserve": [],
        "min_compression": 0.0,
    },
]


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def run_single_fixture(fixture, compressor=None):
    """Run a single fixture. Returns (passed, result_dict)."""
    name = fixture["name"]
    raw = fixture["raw_output"]
    must_preserve = fixture.get("must_preserve", [])
    min_compression = fixture.get("min_compression", 0.0)

    raw_tokens = estimate_tokens(raw)

    # If no compressor provided, just validate the fixture structure
    if compressor is None:
        return True, {
            "name": name,
            "category": fixture.get("category", "unknown"),
            "raw_tokens": raw_tokens,
            "compressed_tokens": raw_tokens,
            "ratio": 0.0,
            "preserved_all": True,
            "status": "no_compressor",
        }

    compressor_kwargs = {}
    if "returncode" in fixture:
        compressor_kwargs["returncode"] = fixture["returncode"]
    if "stderr" in fixture:
        compressor_kwargs["stderr"] = fixture["stderr"]

    try:
        if fixture.get("compressor") == "builtin_tool":
            from builtin_tool_compress import compress_benchmark_fixture
            compressed = compress_benchmark_fixture(fixture)
        else:
            compressed = compressor(fixture["command"], raw, **compressor_kwargs)
    except Exception as e:
        return False, {"name": name, "error": str(e), "status": "compressor_error"}

    compressed_tokens = estimate_tokens(compressed)
    ratio = 1.0 - compressed_tokens / max(raw_tokens, 1)

    # Tee-on-failure check: raw output must pass through verbatim.
    if fixture.get("expect_raw_passthrough"):
        if compressed != raw:
            return False, {
                "name": name,
                "category": fixture.get("category", "unknown"),
                "raw_tokens": raw_tokens,
                "compressed_tokens": compressed_tokens,
                "ratio": round(ratio, 4),
                "preserved_all": False,
                "missing_items": ["raw_passthrough"],
                "status": "FAIL",
            }
        return True, {
            "name": name,
            "category": fixture.get("category", "unknown"),
            "command": fixture.get("command", ""),
            "raw_tokens": raw_tokens,
            "compressed_tokens": compressed_tokens,
            "ratio": 0.0,
            "preserved_all": True,
            "status": "pass",
        }

    # Check preservation
    missing = []
    for item in must_preserve:
        if item not in compressed:
            missing.append(item)

    preserved_all = len(missing) == 0

    # Check minimum compression ratio
    meets_ratio = ratio >= min_compression

    # Negative assertion: certain strings must NOT appear in the output
    # (used to catch fake lint rule codes like CVE2024).
    must_not_contain = fixture.get("must_not_contain", [])
    forbidden_found = []
    for item in must_not_contain:
        if item in compressed:
            forbidden_found.append(item)
    no_forbidden = len(forbidden_found) == 0

    passed = preserved_all and meets_ratio and no_forbidden

    return passed, {
        "name": name,
        "category": fixture.get("category", "unknown"),
        "command": fixture.get("command", ""),
        "raw_tokens": raw_tokens,
        "compressed_tokens": compressed_tokens,
        "ratio": round(ratio, 4),
        "min_required": min_compression,
        "meets_ratio": meets_ratio,
        "preserved_all": preserved_all,
        "missing_items": missing,
        "forbidden_found": forbidden_found,
        "status": "pass" if passed else "FAIL",
    }


def run_benchmarks(compressor=None, as_json=False):
    """Run all benchmark fixtures. Returns True if all pass."""
    results = []
    all_passed = True

    # Load external fixtures if any exist
    fixtures = list(FIXTURES)
    fixture_dir = Path(__file__).resolve().parent / "fixtures"
    if fixture_dir.is_dir():
        for fp in sorted(fixture_dir.glob("*.json")):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    external = json.load(f)
                if isinstance(external, list):
                    fixtures.extend(external)
                elif isinstance(external, dict):
                    fixtures.append(external)
            except (json.JSONDecodeError, OSError) as e:
                print(f"  [WARN] Skipping malformed fixture {fp.name}: {e}", file=sys.stderr)

    for fixture in fixtures:
        passed, result = run_single_fixture(fixture, compressor=compressor)
        results.append(result)
        if not passed:
            all_passed = False

    if as_json:
        summary = {
            "total": len(results),
            "passed": sum(1 for r in results if r.get("status") in ("pass", "no_compressor")),
            "failed": sum(1 for r in results if r.get("status") == "FAIL"),
            "errors": sum(1 for r in results if r.get("status") == "compressor_error"),
            "results": results,
        }
        print(json.dumps(summary, indent=2))
    else:
        total_raw = sum(r.get("raw_tokens", 0) for r in results)
        total_comp = sum(r.get("compressed_tokens", 0) for r in results)

        print(f"\n  Compression Benchmark ({len(results)} fixtures)")
        print(f"  {'=' * 60}")

        for r in results:
            status = r.get("status", "?")
            if status == "no_compressor":
                marker = "[ ]"
            elif status == "pass":
                marker = "[OK]"
            elif status == "FAIL":
                marker = "[!!]"
            else:
                marker = "[ER]"

            name = r.get("name", "?")
            raw_t = r.get("raw_tokens", 0)
            comp_t = r.get("compressed_tokens", 0)
            ratio = r.get("ratio", 0)

            print(f"  {marker} {name:35s}  {raw_t:>6} -> {comp_t:>6} tokens  ({ratio:>5.1%})")

            if r.get("missing_items"):
                for item in r["missing_items"]:
                    print(f"        MISSING: {item!r}")
            if r.get("forbidden_found"):
                for item in r["forbidden_found"]:
                    print(f"        FORBIDDEN: {item!r}")

        print(f"\n  Total: {total_raw:,} raw tokens, {total_comp:,} compressed")
        if total_raw > 0:
            print(f"  Overall ratio: {1.0 - total_comp / total_raw:.1%}")

        passed_count = sum(1 for r in results if r.get("status") in ("pass", "no_compressor"))
        failed_count = sum(1 for r in results if r.get("status") == "FAIL")
        print(f"\n  {passed_count} passed, {failed_count} failed")
        if not compressor:
            print("  (No compressor provided -- fixture validation only)")
        print()

    return all_passed


if __name__ == "__main__":
    as_json = "--json" in sys.argv
    ok = run_benchmarks(as_json=as_json)
    sys.exit(0 if ok else 1)
