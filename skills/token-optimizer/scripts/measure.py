#!/usr/bin/env python3
"""
Token Overhead Measurement Script
Captures real token counts from Claude Code session logs + file-level estimates.
Used by Token Optimizer skill in Phase 0 (before) and Phase 5 (after).

Usage:
    python3 measure.py quick              # Quick scan: overhead + degradation risk + top offenders
    python3 measure.py quick --json       # Machine-readable quick scan
    python3 measure.py doctor             # Health check: verify all components
    python3 measure.py cloud-setup-script # Claude Code cloud: print the environment setup script (see --help)
    python3 measure.py cloud-doctor       # Claude Code cloud: check the in-VM install
    python3 measure.py drift              # Drift report: compare against last snapshot
    python3 measure.py report             # Full standalone report
    python3 measure.py snapshot before    # Save pre-optimization snapshot
    python3 measure.py snapshot after     # Save post-optimization snapshot
    python3 measure.py compare            # Compare before vs after
    python3 measure.py dashboard                         # Standalone dashboard (Trends + Health)
    python3 measure.py dashboard --coord-path /tmp/...   # Full dashboard (after audit)
    python3 measure.py dashboard --serve [--port 9000]   # Serve over HTTP (headless)
    python3 measure.py dashboard --quiet                 # Regenerate silently (for hooks)
    python3 measure.py health             # Check running session health
    python3 measure.py trends             # Usage trends (last 30 days)
    python3 measure.py trends --days 7    # Usage trends (shorter window)
    python3 measure.py trends --json      # Machine-readable output
    python3 measure.py coach               # Interactive coaching data
    python3 measure.py coach --json        # Coaching data as JSON
    python3 measure.py coach --focus skills # Focus on skill optimization
    python3 measure.py collect             # Collect sessions into SQLite DB
    python3 measure.py collect --quiet     # Silent mode (for SessionEnd hook)
    python3 measure.py conversation [session-id] # Per-turn token breakdown
    python3 measure.py conversation --json       # Machine-readable per-turn data
    python3 measure.py pricing-tier              # Show/set pricing tier
    python3 measure.py pricing-tier vertex-regional # Set to Vertex AI Regional
    python3 measure.py jsonl-inspect [session-id]  # JSONL session file stats
    python3 measure.py jsonl-trim                  # Trim large tool results (dry-run)
    python3 measure.py jsonl-trim --apply           # Trim with backup + sidecar
    python3 measure.py jsonl-dedup                 # Find duplicate system reminders (dry-run)
    python3 measure.py jsonl-dedup --apply          # Remove duplicates with backup
    python3 measure.py deterministic-candidates          # Workflow parts that could be plain code, not model calls
    python3 measure.py deterministic-candidates --days 14 --json  # Custom window, machine-readable
    python3 measure.py validate-impact                 # Compare before/after optimization metrics
    python3 measure.py validate-impact --strategy halves # Split sessions chronologically in half
    python3 measure.py validate-impact --days 14 --json  # Custom window, machine-readable
    python3 measure.py attention-score               # Score CLAUDE.md against attention curve
    python3 measure.py attention-score FILE           # Score any file
    python3 measure.py attention-score --json         # Machine-readable output
    python3 measure.py attention-optimize             # Dry-run: propose section reordering
    python3 measure.py attention-optimize --apply     # Apply reordering (backup + write)
    python3 measure.py plugin-cleanup                   # Detect duplicates + archive local/plugin overlaps
    python3 measure.py plugin-cleanup --dry-run         # Preview what would change
    python3 measure.py cleanup-duplicate-hooks          # Remove settings.json hooks the plugin already provides
    python3 measure.py cleanup-duplicate-hooks --dry-run # Preview what would be removed
    python3 measure.py archive-result                  # PostToolUse hook: archive large tool results
    python3 measure.py expand TOOL_USE_ID              # Retrieve archived tool result
    python3 measure.py expand TOOL_USE_ID --lines A:B  # Bounded window: lines A-B
    python3 measure.py expand TOOL_USE_ID --start N --chars M  # Bounded char window
    python3 measure.py expand TOOL_USE_ID --match TEXT # Context windows around matches
    python3 measure.py expand --list                   # List all archived results
    python3 measure.py archive-cleanup [SESSION_ID]    # Clean up archived tool results

    Global flags:
    --context-size N                      # Override context window (e.g., 1000000)

Snapshots are saved to SNAPSHOT_DIR under the active runtime home.

Copyright (C) 2026 Alex Greenshpun
SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""
from __future__ import annotations

import bisect
import copy
import errno
import hashlib
import heapq
import hmac
import io
import itertools
import json
import math
import os
import stat
import glob
import re
import secrets
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
import tempfile
import textwrap
import time
import types
import unicodedata
import platform
import posixpath
import shutil
from collections import Counter, deque
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11 fallback
    tomllib = None


def _redirect_bytecode_out_of_repo():
    """Keep ``__pycache__`` out of a repo when measure.py is run by hand.

    Hooks get this from hooks/run.py (it exports PYTHONPYCACHEPREFIX to every
    hook child). A direct ``python3 measure.py expand ...`` from the model's
    Bash tool does not go through run.py, so the same rule is applied here,
    BEFORE the sibling imports below would write bytecode beside this file:
    in a hosted VM, or when the scripts dir sits inside a git work tree, send
    bytecode to the per-user cache (same dir run.py uses). Never raises."""
    try:
        if getattr(sys, "pycache_prefix", None) or os.environ.get("PYTHONPYCACHEPREFIX", "").strip():
            return
        remote = (
            os.environ.get("CLAUDE_CODE_REMOTE", "").strip().lower() in ("1", "true", "yes", "on")
            or bool(os.environ.get("CLAUDE_CODE_REMOTE_SESSION_ID", "").strip())
            or bool(os.environ.get("TOKEN_OPTIMIZER_CLOUD_KIT_VERSION", "").strip())
            or os.environ.get("TOKEN_OPTIMIZER_REMOTE_HOST", "").strip().lower() in ("cloud", "cowork")
        )
        if not remote:
            here = Path(__file__).resolve().parent
            for candidate in (here, *here.parents):
                if (candidate / ".git").exists():
                    remote = True
                    break
        if not remote:
            return
        base = None
        raw = os.environ.get("LOCALAPPDATA" if os.name == "nt" else "XDG_CACHE_HOME", "").strip()
        if raw and os.path.isabs(raw):
            base = Path(raw)
        if base is None:
            base = Path.home() / ".cache"
        prefix = str(base / "token-optimizer" / "pycache")
        sys.pycache_prefix = prefix
        os.environ["PYTHONPYCACHEPREFIX"] = prefix
    except Exception:
        pass


# Only for a direct CLI run: an importing process (a hook runner, the test
# suite) already chose its bytecode location, and must not have its
# environment rewritten by an import. (Spelled as a tuple test: the plain
# main-guard line must appear only once in this file, see below.)
if __name__ in ("__main__",):
    _redirect_bytecode_out_of_repo()
    # A leading `--runtime NAME` must reach the environment BEFORE plugin_env
    # (imported just below) fixes its data directories from the active
    # runtime at import. Applied later, the flag changed hints but every
    # archive, checkpoint and trends path still pointed at the default
    # runtime (`measure.py --runtime hermes expand <id>` found nothing).
    from runtime_env import consume_runtime_flag as _consume_runtime_flag_early
    sys.argv[:] = _consume_runtime_flag_early(sys.argv, os.environ)

from _measure import _compat as _measure_compat
_measure_providers = _measure_compat.prepare(sys.modules[__name__])

from hook_io import read_stdin_hook_input as _read_stdin_hook_input_shared
from hook_runtime import (
    HookDeadline,
    LeaseLock,
    current_deadline,
    lease_lock,
    _sweep_stale_leases,
)
import plugin_env
from plugin_env import (
    _all_plugin_data_dirs,
    interpret_flag_value,
    reclaim_orphaned_plugin_data_dirs,
    resolve_plugin_data_dir,
    snapshot_dir_candidates,
)
from utf8_io import enforce_utf8_io, reexec_in_utf8_mode
from runtime_env import (
    _safe_home, claude_home, consume_runtime_flag, detect_runtime, hint_python, is_cowork,
    is_claude_code_cloud, project_config_value, run_origin,
    runtime_home, runtime_name_for_humans, shell_path, _windows_hints,
)

# Spelled without the usual main-guard line on purpose: that exact line must
# appear once in this file, at the entry point (tests patch in front of it).
if __name__ in ("__main__",):
    # `--runtime NAME` is the cmd.exe/PowerShell spelling of TOKEN_OPTIMIZER_RUNTIME=NAME
    # (printed on Windows, where the env-prefix form does not parse). It must land in
    # the environment before anything below reads the runtime.
    sys.argv[:] = consume_runtime_flag(sys.argv, os.environ)
from spawn_utils import spawn_detached

# Every console-attached child we spawn on Windows flashes a cmd
# window when the host is the GitHub Desktop app (no inherited console). The
# getattr degrades to 0 on POSIX and on Windows builds without the constant, so
# `creationflags=_NO_WINDOW` is a literal no-op everywhere but Windows. Detached
# spawns (spawn_utils.spawn_detached) OR this in on top of DETACHED_PROCESS --
# CreateProcess ignores CREATE_NO_WINDOW when DETACHED_PROCESS is set, so the OR
# is harmless and keeps a single grep-able invariant across every spawn site.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


_windows_stdio_kwargs = _measure_compat.bind(_measure_providers['runtime']._windows_stdio_kwargs, __name__)


_run_script_child = _measure_compat.bind(_measure_providers['runtime']._run_script_child, __name__, defaults=((),))


_detached_python_exe = _measure_compat.bind(_measure_providers['runtime']._detached_python_exe, __name__)


import antigravity_session
import checkpoint_state
import codex_io
import codex_session
from session_activity import activity_days as _activity_days, activity_bounds as _activity_bounds, activity_coverage, ensure_activity_schema, ensure_activity_view, repair_activity_dates
import deterministic_candidates
import codex_state
import copilot_session
import cursor_session
import hermes_session

CHARS_PER_TOKEN = 4.0
_SKILL_DESC_TRUNCATION_LIMIT = 1536

HOME = _safe_home()
RUNTIME_DIR = runtime_home()
CLAUDE_DIR = claude_home()

# Commands that scan or MUTATE the Claude Code setup (~/.claude). When this skill
# is invoked from inside OpenCode (which loads ~/.claude/skills by default),
# running these against ~/.claude is wrong — see the OpenCode guardrail in
# __main__ and _opencode_audit_notice() below.
#
# Two groups, both blocked under OpenCode:
#   - scan/report:   report, quick, doctor, skill, mcp, plugin-cleanup
#   - write/install: these touch SETTINGS_PATH (~/.claude/settings.json) or
#     CLAUDE_DIR/CLAUDE.md directly (NOT RUNTIME_DIR), so the RUNTIME_DIR
#     redirection does not protect them. ensure-health is additionally guarded
#     at its source (run_ensure_health) since it can be reached as a hook.
_CLAUDE_TARGET_CMDS = frozenset(
    {
        # scan / report
        "report", "quick", "doctor", "skill", "mcp", "plugin-cleanup",
        # write / install (mutate ~/.claude/settings.json or CLAUDE.md)
        "ensure-health", "setup-hook", "setup-all-hooks",
        "cleanup-duplicate-hooks", "setup-daemon", "setup-quality-bar",
        "setup-smart-compact", "inject-routing", "inject-coach",
        "setup-coach-injection", "check-staleness", "subagent-cache",
        # The dashboard daemon serves Claude-targeted data and derives its
        # port/label identity from the runtime ternaries, which default to
        # Claude for unknown runtimes — wrong identity under a foreign host.
        "dashboard",
        # read-only scan (block under foreign runtime).
        # These derive their data from _find_all_jsonl_files() / measure_components()
        # which read CLAUDE_DIR/projects (~/.claude) directly with NO OpenCode
        # branch, so under a foreign runtime they would scan the wrong tree.
        # Blocking is at command-DISPATCH level only (the `args[0] in ...` guard
        # in __main__); internal function callers (copilot-rollup -> usage_trends(),
        # hermes-summary -> usage_trends(), copilot-summary -> _copilot_summary())
        # are NOT subcommands and stay unaffected. Verified: none of these 14 is
        # invoked as a top-level subcommand under opencode/copilot (the OpenCode
        # plugin is a native TS build; the Copilot bridge only shells out to
        # `copilot-rollup`). See the foreign-runtime dispatch regressions in tests/test_devin_review_regressions.py.
        "collect", "conversation", "quality", "health", "kill-stale",
        "drift", "coach", "trends", "savings", "snapshot", "compare",
        "git-context", "dashboard-diagnose", "validate-impact",
        # Usage recommendations: the refresh replays Claude transcripts and
        # reads ~/.claude/settings.json; surfaces read its record file only.
        "recommendations",
    }
)
# Back-compat alias (pre-Copilot name).
_OPENCODE_CLAUDE_TARGET_CMDS = _CLAUDE_TARGET_CMDS

# Runtimes that must never scan or mutate the Claude Code setup (~/.claude).
# OpenCode, GitHub Copilot, and Hermes can all end up invoking
# this skill (OpenCode loads ~/.claude/skills by default; Copilot via direct
# invocation from a Copilot session; Hermes via its plugin which loads the
# shared skill tree). The Claude-targeting commands above are blocked for all
# of them; runtime-data commands are unaffected because RUNTIME_DIR resolves
# to the foreign runtime's own home. Hermes is a foreign runtime by design
# (HERMES_HOME routes runtime_home() to ~/.hermes; it has a full session
# adapter: hermes_session.py, _collect_hermes_sessions, hermes_doctor.py) —
# before this, trends/savings/quality/drift/coach fell through to the
# CLAUDE_DIR scan path in measure_components()/_find_all_jsonl_files() and
# produced empty output against the wrong tree (the isolation leak).
_FOREIGN_RUNTIMES = frozenset({"opencode", "copilot", "hermes", "cursor", "antigravity", "grok", "devin"})

# Per-runtime exemptions: foreign-runtime subcommands that a NATIVE flow
# invokes as a TOP-LEVEL subcommand AND that are runtime-aware (do not scan
# ~/.claude). These are exempted from the dispatch-level block below.
#
# Hermes exempts ``dashboard``: hermes_hook_bridge.run_dashboard()
# (hermes/__init__.py:419 -> hermes_hook_bridge.py:222) shells to
# ``python3 measure.py dashboard`` with TOKEN_OPTIMIZER_RUNTIME=hermes as the
# ``hermes token-optimizer`` CLI command. The dashboard is hermes-aware — its
# daemon identity is runtime-suffixed (port 24844, label
# com.token-optimizer.hermes-dashboard; see _DAEMON_PORT_BY_RUNTIME), and
# measure_components() routes to _measure_hermes_components() (no ~/.claude
# scan), and _find_all_jsonl_files() returns [] under hermes (sessions come
# from state.db via hermes-rollup, not ~/.claude/projects JSONL). So allowing
# it does not violate the isolation principle.
#
# Cursor exempts ``dashboard`` for the same reason as Hermes: its hook bridge
# shells to ``measure.py dashboard`` on session-end, the daemon is runtime-
# suffixed (port 24846), measure_components() routes to _measure_cursor_
# components() and _find_all_jsonl_files() returns [] under cursor — sessions
# come from the tally + cursor-rollup, never ~/.claude/projects JSONL.
#
# Antigravity exempts ``dashboard`` for the same reason as Hermes: its Stop
# hook (antigravity_hook_bridge.handle_stop) shells to ``measure.py dashboard``
# with TOKEN_OPTIMIZER_RUNTIME=antigravity. The dashboard is antigravity-aware
# — daemon identity is runtime-suffixed (port 24847, label
# com.token-optimizer.antigravity-dashboard), measure_components() routes to
# _measure_antigravity_components() (no ~/.claude scan), and
# _find_all_jsonl_files() returns [] under antigravity (sessions come from the
# read-only conversation store via antigravity-rollup, not ~/.claude/projects
# JSONL). So allowing it does not violate the isolation principle.
#
# OpenCode and Copilot exempt NOTHING: their native flows (the OpenCode TS
# plugin; copilot_hook_bridge.py) never invoke a _CLAUDE_TARGET_CMDS
# subcommand as a top-level command — they shell to copilot-rollup /
# copilot-summary / copilot-doctor / copilot-install / copilot-home, all of
# which are outside _CLAUDE_TARGET_CMDS. Verified by grep of the hook bridges.
_FOREIGN_RUNTIME_EXEMPTIONS: dict[str, frozenset[str]] = {
    "hermes": frozenset({"dashboard"}),
    "cursor": frozenset({"dashboard"}),
    "antigravity": frozenset({"dashboard"}),
    "grok": frozenset({"dashboard"}),
}


_is_foreign_runtime = _measure_compat.bind(_measure_providers['runtime']._is_foreign_runtime, __name__)


_copilot_audit_notice = _measure_compat.bind(_measure_providers['runtime']._copilot_audit_notice, __name__)


_cursor_audit_notice = _measure_compat.bind(_measure_providers['runtime']._cursor_audit_notice, __name__)


_grok_audit_notice = _measure_compat.bind(_measure_providers['runtime']._grok_audit_notice, __name__)


_foreign_audit_notice = _measure_compat.bind(_measure_providers['runtime']._foreign_audit_notice, __name__)


_devin_audit_notice = _measure_compat.bind(_measure_providers['runtime']._devin_audit_notice, __name__)


_opencode_audit_notice = _measure_compat.bind(_measure_providers['runtime']._opencode_audit_notice, __name__)


_hermes_audit_notice = _measure_compat.bind(_measure_providers['runtime']._hermes_audit_notice, __name__)


_antigravity_audit_notice = _measure_compat.bind(_measure_providers['runtime']._antigravity_audit_notice, __name__)

# Sentinel file written inside an archived symlinked skill, recording the
# original link target so restore can recreate the symlink. A dir
# holds EITHER SKILL.md (real skill) XOR this marker (symlinked skill).
SYMLINK_TARGET_MARKER = ".symlink-target"

# Backup directories that hold restorable skills. Manual `skill archive` writes
# to skills-archived-DATE; the auto-dedup pass writes to skills-deduped-DATE.
# Restore and the dashboard scan must recognize BOTH so dedup'd skills stay
# recoverable from the UI and the CLI.
SKILL_ARCHIVE_DIR_PREFIXES = ("skills-archived", "skills-deduped")

# Plugin-data-aware paths: prefer runtime-specific plugin data when set,
# else discover via installed_plugins.json so dashboard CLI runs find live data
# (v5.4.23+), else fall back to legacy paths for symlink/script installs.
_RESOLVED_PLUGIN_DATA = resolve_plugin_data_dir()
_PLUGIN_DATA = str(_RESOLVED_PLUGIN_DATA) if _RESOLVED_PLUGIN_DATA else None
# v5.11.1: TOKEN_OPTIMIZER_SNAPSHOT_DIR sandbox override (test isolation;
# production no-op when unset). measure.py resolves SNAPSHOT_DIR independently of
# plugin_env.resolve_snapshot_dir, so honor the same env knob here for parity
# with archive_result/read_cache (which go through the shared resolver).
_SNAPSHOT_DIR_OVERRIDE = os.environ.get("TOKEN_OPTIMIZER_SNAPSHOT_DIR", "").strip()
if _SNAPSHOT_DIR_OVERRIDE:
    SNAPSHOT_DIR = Path(_SNAPSHOT_DIR_OVERRIDE).expanduser()
    _CONFIG_BASE = SNAPSHOT_DIR / "config"
elif _RESOLVED_PLUGIN_DATA is not None:
    SNAPSHOT_DIR = _RESOLVED_PLUGIN_DATA / "data"
    _CONFIG_BASE = _RESOLVED_PLUGIN_DATA / "config"
else:
    SNAPSHOT_DIR = RUNTIME_DIR / "_backups" / "token-optimizer"
    _CONFIG_BASE = None  # resolved below after constants


_windows_profile_dir = _measure_compat.bind(_measure_providers['runtime']._windows_profile_dir, __name__)


_ACCOUNT_HOME_IS_WINDOWS = os.name == "nt"  # module attr so tests can drive both branches


_real_account_home = _measure_compat.bind(_measure_providers['runtime']._real_account_home, __name__)


_same_path = _measure_compat.bind(_measure_providers['runtime']._same_path, __name__)


_is_under = _measure_compat.bind(_measure_providers['runtime']._is_under, __name__)


_FOREIGN_HOME_OVERRIDE_ENV = "TOKEN_OPTIMIZER_ALLOW_FOREIGN_HOME_SCHEDULER"


_foreign_home_override = _measure_compat.bind(_measure_providers['runtime']._foreign_home_override, __name__)


_compute_home_redirected = _measure_compat.bind(_measure_providers['runtime']._compute_home_redirected, __name__)


# Frozen at import, like _SNAPSHOT_DIR_OVERRIDE, so later env drift cannot
# separate the guard from the paths it protects.
_HOME_REDIRECTED = _compute_home_redirected()

_daemon_snapshot_sandboxed = _measure_compat.bind(_measure_providers['runtime']._daemon_snapshot_sandboxed, __name__)


_scheduler_install_refused = _measure_compat.bind(_measure_providers['runtime']._scheduler_install_refused, __name__)


_SCHEDULER_REFUSAL = (
    "refused: HOME is redirected away from this account's real home, or this "
    "account's existing Token Optimizer job belongs to a different install "
    "(another CLAUDE_CONFIG_DIR or data dir); scheduler jobs are per account. "
    "Run `setup-daemon --uninstall` from the install that owns it first, or set "
    "TOKEN_OPTIMIZER_ALLOW_FOREIGN_HOME_SCHEDULER=1 to override"
)

# Windows only: the task is read with a `schtasks /Query` (~0.1 s), and several
# SessionStart paths consult the guard, so its answer is reused for a short
# while. Keyed on the reader itself so a test that swaps it is never served a
# stale answer. File-based jobs (plist, systemd unit) are re-read every time.
_WINDOWS_TASK_PATHS_CACHE = {}
_WINDOWS_TASK_PATHS_TTL_S = 30.0


_reset_job_guard_cache = _measure_compat.bind(_measure_providers['runtime']._reset_job_guard_cache, __name__)


_windows_task_job_paths_cached = _measure_compat.bind(_measure_providers['runtime']._windows_task_job_paths_cached, __name__)


_this_install_roots = _measure_compat.bind(_measure_providers['runtime']._this_install_roots, __name__)


_plist_job_paths = _measure_compat.bind(_measure_providers['runtime']._plist_job_paths, __name__)


_systemd_unit_job_paths = _measure_compat.bind(_measure_providers['runtime']._systemd_unit_job_paths, __name__)


_windows_task_job_paths = _measure_compat.bind(_measure_providers['runtime']._windows_task_job_paths, __name__)


_scheduler_job_paths = _measure_compat.bind(_measure_providers['runtime']._scheduler_job_paths, __name__)


_scheduler_job_points_elsewhere = _measure_compat.bind(_measure_providers['runtime']._scheduler_job_points_elsewhere, __name__)


# Cowork dual-write: the per-session state dirs -- QUALITY_CACHE_DIR
# (quality-cache-*.json, resumable-*.json, run-once markers) and CHECKPOINT_DIR
# -- default to RUNTIME_DIR (~/.claude/token-optimizer). But SNAPSHOT_DIR
# resolves to the plugin-data dir (~/.claude/plugins/data/<id>/data) whenever
# CLAUDE_PLUGIN_DATA is set. On desktop CLAUDE_PLUGIN_DATA is usually absent from
# the hook env so both live under ~/.claude and coincide; inside Cowork it IS set
# (docs-grounding.md §2: ${CLAUDE_PLUGIN_DATA} = ~/.claude/plugins/data/{id}/ is
# the canonical persistent dir), so the two bases DIVERGE -- state double-writes
# to ~/.claude/token-optimizer AND the plugin-data tree, and the once-per-session
# guard fires once per base. Route ALL per-session state to the SAME base as
# SNAPSHOT_DIR when running in Cowork with a resolved plugin-data dir. Desktop is
# untouched (stays on RUNTIME_DIR), and the snapshot-override sandbox path is
# untouched so existing test isolation is preserved. Gated on is_cowork().
if (
    not _SNAPSHOT_DIR_OVERRIDE
    and _RESOLVED_PLUGIN_DATA is not None
    and is_cowork()
):
    _STATE_BASE = _RESOLVED_PLUGIN_DATA
else:
    _STATE_BASE = RUNTIME_DIR


_remote_lean_profile = _measure_compat.bind(_measure_providers['runtime']._remote_lean_profile, __name__)


DASHBOARD_PATH = SNAPSHOT_DIR / "dashboard.html"
# Headline-shape marker for the current dashboard data contract. Bumped only when the data
# shape changes; ensure-health regenerates any dashboard whose sidecar meta lacks it.
_DASHBOARD_SHAPE_MARKER = "estimated_volume_transformation_usd"


_dashboard_meta_path = _measure_compat.bind(_measure_providers['runtime']._dashboard_meta_path, __name__)


_parse_semver = _measure_compat.bind(_measure_providers['runtime']._parse_semver, __name__)


# A dashboard HTML smaller than this is treated as absent/corrupt, so the version
# guard will not honor a "newer" sidecar sitting next to a broken file (a real
# dashboard is 4-5MB; the bundled template alone is tens of KB). This is the
# anti-wedge floor: without it, a sidecar that names an impossibly-high version
# next to an empty/truncated HTML would block EVERY future writer, including the
# newest build, and never self-heal.
_DASHBOARD_MIN_VALID_BYTES = 4096


_write_dashboard_meta = _measure_compat.bind(_measure_providers['runtime']._write_dashboard_meta, __name__)


_write_dashboard_meta_atomic = _measure_compat.bind(_measure_providers['runtime']._write_dashboard_meta_atomic, __name__)


_dashboard_on_disk_is_newer = _measure_compat.bind(_measure_providers['runtime']._dashboard_on_disk_is_newer, __name__)


_dashboard_meta_is_fresh = _measure_compat.bind(_measure_providers['runtime']._dashboard_meta_is_fresh, __name__)


_use_codex_session_adapter = _measure_compat.bind(_measure_providers['runtime']._use_codex_session_adapter, __name__, defaults=(None,))


_use_hermes_session_adapter = _measure_compat.bind(_measure_providers['runtime']._use_hermes_session_adapter, __name__)


_use_copilot_session_adapter = _measure_compat.bind(_measure_providers['runtime']._use_copilot_session_adapter, __name__)


_use_cursor_session_adapter = _measure_compat.bind(_measure_providers['runtime']._use_cursor_session_adapter, __name__)


_use_antigravity_session_adapter = _measure_compat.bind(_measure_providers['runtime']._use_antigravity_session_adapter, __name__)


_use_grok_session_adapter = _measure_compat.bind(_measure_providers['runtime']._use_grok_session_adapter, __name__)

# Tokens per skill frontmatter (loaded at startup)
TOKENS_PER_SKILL_APPROX = 100
# Skill wrapper overhead: previously estimated at 35 tokens per skill for boilerplate
# Claude Code wraps around each skill entry. As of v2.1.94+, measurements show this
# overhead is negligible or zero — skills load their frontmatter content directly.
SKILL_WRAPPER_OVERHEAD = 0
# Tokens per command frontmatter — commands are loaded ON-DEMAND (deferred), not at startup.
# They appear in the slash-command menu but their full content is only loaded when invoked.
# Count only the menu entry overhead (~5 tokens per command name), not full frontmatter.
TOKENS_PER_COMMAND_APPROX = 5
# Tokens per MCP deferred tool name in Tool Search menu
TOKENS_PER_DEFERRED_TOOL = 15
# Tokens per eagerly-loaded MCP tool (full schema in system prompt)
TOKENS_PER_EAGER_TOOL = 150
# Average tools per MCP server (fallback when tool count unknown)
AVG_TOOLS_PER_SERVER = 10
# Known MCP server tool counts (public/marketplace servers only, updated 2026-04)
_KNOWN_SERVER_TOOL_COUNTS = {
    "brightdata": 4,
    "claude-in-chrome": 20,
    "exa": 3,
    "tavily": 5,
    "memory": 8,
    "memory-semantic": 11,
    "context7": 2,
    "perplexity-ask": 1,
}
# Overhead per CLAUDE.md file injection (XML wrapper + headers + disclaimer)
CLAUDE_MD_INJECTION_OVERHEAD = 75

# ========== Pricing Tiers ==========
# Per-MTok pricing for Claude models across providers.
# Non-Claude models are unaffected by tier selection.

# Fallback Claude rate cards: a card-for-card mirror of the "anthropic"
# section of the bundled pricing/prices.json, kept in literals so pricing
# stays correct when the file is absent or disabled. A regression
# test pins literal == bundled, so a card the refresh pipeline adds must be
# mirrored here at the same time. First-party rates apply on anthropic /
# vertex-global / bedrock; vertex-regional is +10% via _claude_cards.
_FALLBACK_CLAUDE_MODELS = {
    # cache_write = 5-minute TTL (1.25x input); cache_write_1h = 1-hour TTL (2x input).
    # Verified 2026-09-24 from platform.claude.com/docs/en/about-claude/pricing.
    "fable":  {"input": 10.0, "output": 50.0, "cache_read": 1.0,  "cache_write": 12.5,  "cache_write_1h": 20.0},
    "fable_5": {"input": 10.0, "output": 50.0, "cache_read": 1.0, "cache_write": 12.5, "cache_write_1h": 20.0},
    # Fable 5.1 / Mythos 5.1: cache reads are 0.025x input ($0.25), not 0.1x.
    "fable_5_1": {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": 12.5, "cache_write_1h": 20.0},
    "mythos_5": {"input": 10.0, "output": 50.0, "cache_read": 1.0, "cache_write": 12.5, "cache_write_1h": 20.0},
    "mythos_5_1": {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": 12.5, "cache_write_1h": 20.0},
    # Opus 5.5 is $4/$20 with 0.05x cache reads; Opus 4.5+ is $5/$25; Opus <=4.1 is $15/$75.
    "opus_5_5": {"input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 5.0, "cache_write_1h": 8.0},
    "opus":   {"input": 5.0,  "output": 25.0, "cache_read": 0.5,  "cache_write": 6.25,  "cache_write_1h": 10.0},
    "opus_3":  {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75, "cache_write_1h": 30.0},
    "opus_4":  {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75, "cache_write_1h": 30.0},
    "opus_4_1": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75, "cache_write_1h": 30.0},
    "opus_4_5": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25, "cache_write_1h": 10.0},
    "opus_4_6": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25, "cache_write_1h": 10.0},
    "opus_4_7": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25, "cache_write_1h": 10.0},
    "opus_4_8": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25, "cache_write_1h": 10.0},
    "opus_5":  {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25, "cache_write_1h": 10.0},
    # Sonnet 5's launch rate became the standard price (2026-08-29, see the
    # _SONNET_INTRO_PRICING_UNTIL block below); Sonnet <=4.6 stays $3/$15.
    "sonnet": {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5, "cache_write_1h": 4.0},
    "sonnet_4": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75, "cache_write_1h": 6.0},
    "sonnet_4_5": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75, "cache_write_1h": 6.0},
    "sonnet_4_6": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75, "cache_write_1h": 6.0},
    "sonnet_5": {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5, "cache_write_1h": 4.0},
    # Sonnet 5.5's cache reads are 0.05x input ($0.10), not the usual 0.1x.
    "sonnet_5_5": {"input": 2.0, "output": 10.0, "cache_read": 0.1, "cache_write": 2.5, "cache_write_1h": 4.0},
    # Sonnet 4.6 / 4.5 / 4.0 keep the $3/$15 card. Sonnet 5 (2026-06-30) is $2/$10,
    # so one shared "sonnet" bucket silently misprices the older generation by 50%.
    "sonnet_legacy": {"input": 3.0,  "output": 15.0, "cache_read": 0.3,  "cache_write": 3.75,  "cache_write_1h": 6.0},
    "haiku":  {"input": 1.0,  "output": 5.0,  "cache_read": 0.1,  "cache_write": 1.25,  "cache_write_1h": 2.0},
    "haiku_3": {"input": 0.25, "output": 1.25, "cache_read": 0.03, "cache_write": 0.3, "cache_write_1h": 0.5},
    "haiku_3_5": {"input": 0.8, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0, "cache_write_1h": 1.6},
    "haiku_4_5": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25, "cache_write_1h": 2.0},
    # Haiku 5.5 is $0.10/$0.50 for prompts up to 100K tokens; longer prompts pay
    # the 5x long-context card below.
    "haiku_5_5": {"input": 0.1, "output": 0.5, "cache_read": 0.01, "cache_write": 0.125, "cache_write_1h": 0.2},
}

# Long-context surcharge cards, applied when a request's full prompt
# (input + cache reads + cache writes) exceeds
# ANTHROPIC_LONG_CONTEXT_INPUT_THRESHOLD. Claude Haiku 5.5 is priced by
# prompt length: >100K pays 5x on every rate. Verified 2026-10-10 from
# platform.claude.com/docs/en/about-claude/pricing ("Long context
# pricing") and LiteLLM's claude-haiku-5-5 *_above_100k_tokens fields.
# Mirrors the bundled "anthropic_long_context" section.
_FALLBACK_CLAUDE_MODELS_LC = {
    "haiku_5_5": {"input": 0.5, "output": 2.5, "cache_read": 0.05, "cache_write": 0.625, "cache_write_1h": 1.0},
}


_claude_cards = _measure_compat.bind(_measure_providers['pricing']._claude_cards, __name__, defaults=(1.0,))


PRICING_TIERS = {
    "anthropic": {
        "label": "Anthropic API",
        "claude_models": _claude_cards(_FALLBACK_CLAUDE_MODELS),
        "claude_models_lc": _claude_cards(_FALLBACK_CLAUDE_MODELS_LC),
    },
    "vertex-global": {
        "label": "Vertex AI Global",
        "claude_models": _claude_cards(_FALLBACK_CLAUDE_MODELS),
        "claude_models_lc": _claude_cards(_FALLBACK_CLAUDE_MODELS_LC),
    },
    "vertex-regional": {
        "label": "Vertex AI Regional",
        # Vertex regional applies a +10% surcharge on all Claude rates.
        "claude_models": _claude_cards(_FALLBACK_CLAUDE_MODELS, 1.1),
        "claude_models_lc": _claude_cards(_FALLBACK_CLAUDE_MODELS_LC, 1.1),
    },
    "bedrock": {
        "label": "AWS Bedrock",
        "claude_models": _claude_cards(_FALLBACK_CLAUDE_MODELS),
        "claude_models_lc": _claude_cards(_FALLBACK_CLAUDE_MODELS_LC),
    },
}

# Anthropic long-context surcharge threshold: a request whose full prompt
# (input + cache reads + cache writes) exceeds this bills at the
# "claude_models_lc" card when the model has one. Haiku 5.5's threshold is
# 100K; other 1M Claude models have no long-context surcharge.
ANTHROPIC_LONG_CONTEXT_INPUT_THRESHOLD = 100_000

# --- Sonnet 5 introductory pricing (date-gated) ------------------------------------------
# Sonnet 5 launched with INTRODUCTORY pricing ($2/$10 per MTok; cache_read 0.2, cache_write
# 2.5 [5m] / 4.0 [1h]) in effect through 2026-08-31; the STANDARD rate ($3/$15) resumes
# 2026-09-01. Since 2026-08-29 that launch rate IS the standard price (see
# _apply_sonnet_intro_pricing below), so the _FALLBACK_CLAUDE_MODELS literal above already
# carries $2/$10 on every tier -- matching what the bundled file forces onto Vertex/Bedrock
# anyway -- and the swap below is a belt-and-braces no-op kept for its pinned-date tests.
# Verified 2026-07-10 and re-verified 2026-10-10 from
# platform.claude.com/docs/en/about-claude/pricing.
_SONNET_STANDARD_RATES = {"input": 3.0, "output": 15.0, "cache_read": 0.3,
                          "cache_write": 3.75, "cache_write_1h": 6.0}
_SONNET_INTRO_RATES = {"input": 2.0, "output": 10.0, "cache_read": 0.2,
                       "cache_write": 2.5, "cache_write_1h": 4.0}
# UTC anchor so the flip happens at the SAME instant as the TS ports (openclaw/opencode gate on
# a UTC-midnight Date.parse), avoiding a ~day of cross-engine disagreement around the boundary.
_SONNET_INTRO_PRICING_UNTIL = datetime(2026, 9, 1, tzinfo=timezone.utc)


_pricing_as_of = _measure_compat.bind(_measure_providers['pricing']._pricing_as_of, __name__)


_apply_sonnet_intro_pricing = _measure_compat.bind(_measure_providers['pricing']._apply_sonnet_intro_pricing, __name__, defaults=(None,))


_apply_sonnet_intro_pricing()

OPENAI_MODEL_PRICING = {
    # https://developers.openai.com/api/docs/models/gpt-6-astra
    "gpt-6-astra": {"input": 10.0, "cache_read": 1.0, "cache_write": 12.50, "output": 50.0},
    # Prices per 1M tokens from OpenAI API pricing/model docs.
    # GPT-5.x family
    "gpt-5-codex": {"input": 1.25, "cache_read": 0.125, "output": 10.0},
    "gpt-5.1-codex": {"input": 1.25, "cache_read": 0.125, "output": 10.0},
    "gpt-5.1-codex-mini": {"input": 0.25, "cache_read": 0.025, "output": 2.0},
    "gpt-5.1": {"input": 1.25, "cache_read": 0.125, "output": 10.0},
    "gpt-5.2": {"input": 1.75, "cache_read": 0.175, "output": 14.0},
    "gpt-5.2-codex": {"input": 1.75, "cache_read": 0.175, "output": 14.0},
    "gpt-5.3-codex": {"input": 1.75, "cache_read": 0.175, "output": 14.0},
    "gpt-5": {"input": 1.25, "cache_read": 0.125, "output": 10.0},
    "gpt-5-mini": {"input": 0.25, "cache_read": 0.025, "output": 2.0},
    "gpt-5-nano": {"input": 0.05, "cache_read": 0.005, "output": 0.40},
    "gpt-5.4": {"input": 2.5, "cache_read": 0.25, "output": 15.0},
    "gpt-5.4-mini": {"input": 0.75, "cache_read": 0.075, "output": 4.5},
    "gpt-5.4-nano": {"input": 0.20, "cache_read": 0.02, "output": 1.25},
    "gpt-5.5": {"input": 5.0, "cache_read": 0.50, "output": 30.0},
    "gpt-5.5-pro": {"input": 30.0, "cache_read": 30.0, "output": 180.0},  # cache_read N/A per OpenAI; billed at full input rate
    # GPT-5.6 family. Cache writes cost 1.25x the applicable input rate.
    "gpt-5.6-sol": {"input": 5.0, "cache_read": 0.50, "cache_write": 6.25, "output": 30.0},
    "gpt-5.6-terra": {"input": 2.0, "cache_read": 0.20, "cache_write": 2.50, "output": 12.0},
    "gpt-5.6-luna": {"input": 0.20, "cache_read": 0.02, "cache_write": 0.25, "output": 1.20},
    # GPT-4.x family
    "gpt-4.1": {"input": 2.0, "cache_read": 0.50, "output": 8.0},
    "gpt-4.1-mini": {"input": 0.40, "cache_read": 0.10, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "cache_read": 0.025, "output": 0.40},
    "gpt-4o": {"input": 2.50, "cache_read": 1.25, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "cache_read": 0.075, "output": 0.60},
    # o-series reasoning models
    "o3-pro": {"input": 20.0, "cache_read": 5.0, "output": 80.0},
    "o3-mini": {"input": 1.10, "cache_read": 0.55, "output": 4.40},
    "o3": {"input": 2.0, "cache_read": 0.50, "output": 8.0},
    "o4-mini": {"input": 1.10, "cache_read": 0.275, "output": 4.40},
}
OPENAI_LONG_CONTEXT_PRICING = {
    "gpt-6-astra": {"input": 20.0, "cache_read": 2.0, "cache_write": 25.0, "output": 75.0},
    "gpt-5.4": {"input": 5.0, "cache_read": 0.50, "output": 22.5},
    "gpt-5.5": {"input": 10.0, "cache_read": 1.0, "output": 45.0},
    "gpt-5.6-sol": {"input": 10.0, "cache_read": 1.0, "cache_write": 12.50, "output": 45.0},
    "gpt-5.6-terra": {"input": 4.0, "cache_read": 0.40, "cache_write": 5.0, "output": 18.0},
    "gpt-5.6-luna": {"input": 0.40, "cache_read": 0.04, "cache_write": 0.50, "output": 1.80},
}
OPENAI_LONG_CONTEXT_INPUT_THRESHOLD = 272_000

# --- gpt-5.6-sol promotional pricing (date-gated) -------------------------------
# OpenAI documents the $4/$20 rate as "available at least through November 21, 2026."
# The canonical OPENAI_MODEL_PRICING / OPENAI_LONG_CONTEXT_PRICING literals above hold
# the STANDARD card ($5/$30); while the promo window is open we swap the promo card in
# so dollar savings stay accurate today AND flip back automatically after 2026-11-21
# with no manual edit. Mirrors _apply_sonnet_intro_pricing (same _pricing_as_of gate).
_GPT56_SOL_STANDARD = {"input": 5.0, "cache_read": 0.50, "cache_write": 6.25, "output": 30.0}
_GPT56_SOL_PROMO = {"input": 4.0, "cache_read": 0.40, "cache_write": 5.0, "output": 20.0}
_GPT56_SOL_LC_STANDARD = {"input": 10.0, "cache_read": 1.0, "cache_write": 12.50, "output": 45.0}
_GPT56_SOL_LC_PROMO = {"input": 8.0, "cache_read": 0.80, "cache_write": 10.0, "output": 30.0}
_GPT56_SOL_PROMO_UNTIL = datetime(2026, 11, 21, tzinfo=timezone.utc)


_apply_gpt56_sol_promo_pricing = _measure_compat.bind(_measure_providers['pricing']._apply_gpt56_sol_promo_pricing, __name__, defaults=(None,))


_apply_gpt56_sol_promo_pricing()

GEMINI_MODEL_PRICING = {
    # Prices per 1M tokens from ai.google.dev/gemini-api/docs/pricing (May 2026).
    "gemini-3.6-flash": {"input": 1.50, "cache_read": 0.15, "output": 7.50},  # released 2026-07-21; 17% output cut vs 3.5-flash
    "gemini-3.5-flash": {"input": 1.50, "cache_read": 0.15, "output": 9.0},
    "gemini-3.1-pro-preview": {"input": 2.0, "cache_read": 0.20, "output": 12.0},
    "gemini-3.1-flash-lite": {"input": 0.25, "cache_read": 0.025, "output": 1.50},
    "gemini-3.1-pro": {"input": 2.0, "cache_read": 0.20, "output": 12.0},
    # cache_read = 10% of input, consistent with every other Gemini 3.x entry (was 0.0,
    # which silently zeroed cache-read cost -> understated cost -> mis-stated savings if hit).
    "gemini-3-pro": {"input": 2.0, "cache_read": 0.20, "output": 12.0},
    "gemini-3-flash": {"input": 0.50, "cache_read": 0.05, "output": 3.0},
    "gemini-2.5-pro": {"input": 1.25, "cache_read": 0.125, "output": 10.0},
    "gemini-2.5-flash": {"input": 0.30, "cache_read": 0.03, "output": 2.50},
    "gemini-2.5-flash-lite": {"input": 0.10, "cache_read": 0.01, "output": 0.40},
}
GEMINI_LONG_CONTEXT_PRICING = {
    "gemini-2.5-pro": {"input": 2.50, "cache_read": 0.25, "output": 15.0},
    "gemini-3.1-pro-preview": {"input": 4.0, "cache_read": 0.40, "output": 18.0},
}
GEMINI_LONG_CONTEXT_INPUT_THRESHOLD = 200_000


# --- Bundled price table (auto-refreshed) --------------------------------------
# pricing/prices.json is regenerated daily in CI by scripts/refresh_prices.py from
# Anthropic's pricing page and the LiteLLM price feed, and ships with each release,
# so new models and price changes arrive without anyone hand-editing the tables
# above. It is read ONCE at import, read-only: no network, no locks, no writes.
# The literals above stay as the fallback; the file only adds or updates cards,
# never removes one. A missing, oversized or malformed file is ignored.
_PRICES_FILE_MAX_BYTES = 2 * 1024 * 1024
_PRICE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PRICE_FIELDS = ("input", "output", "cache_read", "cache_write", "cache_write_1h")
BUNDLED_PRICES_STATUS = {"loaded": False, "path": None, "generated_at": None, "error": None}


_bundled_prices_path = _measure_compat.bind(_measure_providers['pricing']._bundled_prices_path, __name__)


_clean_price_cards = _measure_compat.bind(_measure_providers['pricing']._clean_price_cards, __name__)


_PROMO_MANAGED_OPENAI_KEYS = frozenset({"gpt-5.6-sol"})


_apply_bundled_prices = _measure_compat.bind(_measure_providers['pricing']._apply_bundled_prices, __name__, defaults=(None,))


# Tests pin a pricing regime with TOKEN_OPTIMIZER_PRICING_AS_OF; the bundled file
# reflects today's prices, so it is skipped there to keep those regimes exact.
if not os.environ.get("TOKEN_OPTIMIZER_PRICING_AS_OF"):
    _apply_bundled_prices()

# ---------------------------------------------------------------------------
# PROVIDER CACHE-PROFILE REGISTRY  (one source of truth for cache economics)
# ---------------------------------------------------------------------------
# Token Optimizer is model-AGNOSTIC. Cache economics differ by provider and are
# NEVER hardcoded to Anthropic semantics. Each profile describes HOW a provider's
# prompt cache works so the cache-TTL watchdog can resolve a session's model to
# the right detection strategy and the right (verified) remedy.
#
# cache_kind:
#   "explicit_ttl"        - user sets a TTL; expiry forces a re-WRITE that is
#                           directly billable (Anthropic API). Cache-creation
#                           tokens are reported per turn -> rewrite detection.
#   "automatic_discount"  - cache is automatic, no creation event; cached reads
#                           are discounted. Expiry shows up as a COLLAPSE in the
#                           cached/prompt ratio between turns -> collapse detection.
#   "explicit_storage"    - automatic discount PLUS an opt-in explicit cache with
#                           a user TTL and per-hour storage pricing (Gemini).
#   "none"                - the model has no prompt cache; prefix churn is free of
#                           cache penalty. Honest "no cache economics" bucket.
#
# Verified 2026-06-11 against current public docs (see cache-ttl-stage2.md report
# for citations). `confidence` is "verified" when every number was confirmed from
# the provider's own docs, "estimated" otherwise (the output labels it).
PROVIDER_CACHE_PROFILES = {
    "anthropic_api": {
        # Raw Anthropic API/SDK/agent-harness sessions (e.g. Hermes routing to
        # Anthropic). The user CAN set cache_control ttl, so the 5-min default is
        # the live boundary and the 1h-cache_control counterfactual is valid.
        "label": "Anthropic (API / SDK / agent harness)",
        "cache_kind": "explicit_ttl",
        "effective_ttl_seconds": 300,      # 5-minute default TTL = detection boundary
        "write_premium": 1.25,             # 5m write = 1.25x input
        "extended_ttl_seconds": 3600,      # opt-in 1-hour TTL (cache_control)
        "extended_write_premium": 2.0,     # 1h write = 2x input (once)
        "cached_discount": 0.1,            # cache read = 0.1x input
        "min_cacheable_tokens": 1024,
        "remedy_key": "anthropic_api",
        "recoverable_kind": "recoverable_1h",  # 1h-cache_control counterfactual valid
        "confidence": "verified",
    },
    "claude_code": {
        # Claude Code REQUESTS a 1-hour prompt cache TTL (the platform default;
        # the historical "silent downgrade to 5 minutes" was a bug, fixed in
        # v2.1.129). There is no user TTL knob. Empirically the cache survives
        # sub-hour pauses, so the detection boundary is 1 hour, not 5 minutes.
        # Waste is only the prefix re-writes that follow pauses LONGER than the
        # hour — avoidable only behaviorally (resume within the hour / batch work),
        # NOT via any setting. The 1h-cache_control counterfactual does NOT apply
        # (Claude Code already holds a 1h cache).
        "label": "Claude Code",
        "cache_kind": "explicit_ttl",
        "effective_ttl_seconds": 3600,     # platform holds 1h (no user knob) = boundary
        "write_premium": 2.0,              # 1h write = 2x input
        "cached_discount": 0.1,            # cache read = 0.1x input
        "min_cacheable_tokens": 1024,
        "remedy_key": "claude_code",
        "recoverable_kind": "avoidable_behavioral",  # no 1h counterfactual; behavioral only
        "confidence": "verified",
    },
    "openai": {
        "label": "OpenAI / Codex (GPT-5.x, o-series)",
        "cache_kind": "automatic_discount",
        "effective_ttl_seconds": 300,      # 5-10min inactivity; use 5min floor
        "max_ttl_seconds": 3600,           # max ~1 hour in_memory
        "write_premium": 1.0,              # no cache-WRITE charge (automatic)
        "extended_ttl_seconds": 86400,     # prompt_cache_retention="24h" knob
        "cached_discount": 0.1,            # cached input = 0.1x (90% off)
        "min_cacheable_tokens": 1024,      # exact-prefix match >=1024 tokens
        "remedy_key": "openai",
        "recoverable_kind": "recoverable_window",  # collapse avoided by resuming in window
        "confidence": "verified",
    },
    "gemini": {
        "label": "Google Gemini (2.5+)",
        "cache_kind": "explicit_storage",
        "effective_ttl_seconds": 300,      # implicit cache reuse window (approx)
        "write_premium": 1.0,              # implicit caching: no write charge
        # explicit_default_ttl_seconds: explicit cached_content TTL defaults to 1h
        # (3600s) — kept for future use when explicit-cache detection lands.
        "explicit_default_ttl_seconds": 3600,
        "explicit_storage_priced": True,   # per-hour storage billing on explicit cache
        "cached_discount": 0.1,            # ~75-90% off (derived per-model from rate card)
        "min_cacheable_tokens": 1024,      # 1024 Flash / 2048 Pro
        "remedy_key": "gemini",
        "recoverable_kind": "recoverable_window",
        "confidence": "verified",
    },
    "deepseek": {
        "label": "code review (Context Caching on Disk)",
        "cache_kind": "automatic_discount",
        "effective_ttl_seconds": 3600,     # persists hours-to-days; conservative 1h
        "write_premium": 1.0,              # automatic, no write charge
        "cached_discount": 0.1,            # cache hit = 1/10 input (90% off)
        "min_cacheable_tokens": 1,         # no documented minimum
        "remedy_key": "deepseek",
        "recoverable_kind": "recoverable_window",
        "confidence": "verified",
    },
    "unknown": {
        "label": "Unknown / no documented cache",
        "cache_kind": "none",
        "effective_ttl_seconds": None,
        "write_premium": 1.0,
        "cached_discount": None,
        "min_cacheable_tokens": None,
        "remedy_key": "none",
        "recoverable_kind": "none",
        "confidence": "verified",  # the honest "we make no claims" profile
    },
}


_resolve_cache_profile = _measure_compat.bind(_measure_providers['pricing']._resolve_cache_profile, __name__, defaults=(None,))


_KNOWN_PROVIDER_PREFIXES = {
    "anthropic", "openai", "google", "gemini", "vertex", "bedrock",
    "openrouter", "gateway", "litellm", "azure", "aws",
}

# Bedrock inference-profile ids are DOT-qualified
# ("us.anthropic.claude-haiku-4-5", "global.anthropic.claude-sonnet-4-5"):
# a leading chain of known provider/region tokens separated by dots. Dots stay
# out of the slash/colon loop so ids like "claude-3.5-sonnet" or "gpt-4.1"
# are never cut on a version dot.
_DOTTED_PROVIDER_TOKENS = _KNOWN_PROVIDER_PREFIXES | {
    "amazon", "us", "us-gov", "eu", "ap", "apac", "au", "ca", "cn",
    "global", "jp", "sa", "me", "af", "il",
}
_DOTTED_PROVIDER_PREFIX_RE = re.compile(
    r"^(?:(?:" + "|".join(sorted(_DOTTED_PROVIDER_TOKENS)) + r")\.)+")


_strip_provider_prefixes = _measure_compat.bind(_measure_providers['pricing']._strip_provider_prefixes, __name__)

CODEX_DEFAULT_EFFECTIVE_CONTEXT_WINDOW = 258_400
_context_window_cache = None

CONFIG_DIR = _CONFIG_BASE if _CONFIG_BASE else RUNTIME_DIR / "token-optimizer"
CONFIG_PATH = CONFIG_DIR / "config.json"


_load_pricing_tier = _measure_compat.bind(_measure_providers['pricing']._load_pricing_tier, __name__)


_pricing_tier_label = _measure_compat.bind(_measure_providers['pricing']._pricing_tier_label, __name__)


_save_pricing_tier = _measure_compat.bind(_measure_providers['pricing']._save_pricing_tier, __name__)


_get_model_cost = _measure_compat.bind(_measure_providers['pricing']._get_model_cost, __name__, defaults=(0, 0, None, None, None, False,))


_is_priced_model = _measure_compat.bind(_measure_providers['pricing']._is_priced_model, __name__, defaults=(None,))


_PRICE_ALIAS_CACHE = {}
_PRICE_MATCH_MEMO = {}
_MISSING = object()


_price_aliases = _measure_compat.bind(_measure_providers['pricing']._price_aliases, __name__)


# A dated or numbered snapshot of a priced id: -2026-01-01, -20260101, -0613,
# -001 or @20260101. Anything else after a card id is a different model.
_PRICE_SNAPSHOT_RE = re.compile(r"(?:-\d{4}-\d{2}-\d{2}|-\d{8}|-\d{3,4}|@\d{8})")


_match_price_alias = _measure_compat.bind(_measure_providers['pricing']._match_price_alias, __name__)


_normalize_openai_model_name = _measure_compat.bind(_measure_providers['pricing']._normalize_openai_model_name, __name__)


_GEMINI_DEPRECATION_WARNED = set()

_normalize_gemini_model_name = _measure_compat.bind(_measure_providers['pricing']._normalize_gemini_model_name, __name__)


# Process-local cache for _resolve_session_model to avoid re-reading JSONL
# files during a single measure.py invocation.
_RESOLVED_MODEL_CACHE = {}


_resolve_session_model = _measure_compat.bind(_measure_providers['pricing']._resolve_session_model, __name__, defaults=(None,))


_simulate_model_switch = _measure_compat.bind(_measure_providers['pricing']._simulate_model_switch, __name__, defaults=("sonnet",))


_cost_from_model_breakdown = _measure_compat.bind(_measure_providers['pricing']._cost_from_model_breakdown, __name__, defaults=(None, None, None,))


_fmt_context_window = _measure_compat.bind(_measure_providers['runtime']._fmt_context_window, __name__)


# ANSI/VT escape sequences: CSI (\x1b[ ... final byte), OSC (\x1b] ... BEL or
# ST), charset/two-byte sequences. Session-log text is attacker-influenceable;
# strip before echoing it to a terminal (coach previews, subagent names).
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;:<=>?]*[ -/]*[@-~]"   # CSI (full ECMA-48 parameter bytes 0x30-0x3f)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL or ST
    r"|\x1b[()][0-2A-Z]"                # charset selection
    r"|\x1b[@-Z\\-_]"                   # remaining two-byte escapes
    r"|\x1b\]"                          # bare OSC introducer (unterminated)
    r"|\x1b\[[\x00-\x1f]*[ -/]*[@-~]"   # CSI with control bytes before final
    r"|\x1b\["                          # bare CSI introducer (unterminated)
)
_BEL_RE = re.compile(r"\x07")


_strip_ansi = _measure_compat.bind(_measure_providers['runtime']._strip_ansi, __name__)


_safe_int = _measure_compat.bind(_measure_providers['runtime']._safe_int, __name__)


_context_usage = _measure_compat.bind(_measure_providers['runtime']._context_usage, __name__)


_valid_context_usage = _measure_compat.bind(_measure_providers['runtime']._valid_context_usage, __name__)


_resume_usage = _measure_compat.bind(_measure_providers['runtime']._resume_usage, __name__)


_USAGE_BUCKET_KEYS = ("inp", "out", "cr", "cc", "cc_1h", "cc_5m")


_usage_buckets = _measure_compat.bind(_measure_providers['runtime']._usage_buckets, __name__)


_billed_usage_parts = _measure_compat.bind(_measure_providers['runtime']._billed_usage_parts, __name__, defaults=(None,))


_price_usage_parts = _measure_compat.bind(_measure_providers['runtime']._price_usage_parts, __name__, defaults=(None, False, True,))


_turn_prefix_cached = _measure_compat.bind(_measure_providers['runtime']._turn_prefix_cached, __name__)


_turn_resume_write = _measure_compat.bind(_measure_providers['runtime']._turn_resume_write, __name__)


_context_token_count = _measure_compat.bind(_measure_providers['runtime']._context_token_count, __name__)


estimate_tokens_from_file = _measure_compat.bind(_measure_providers['runtime'].estimate_tokens_from_file, __name__)


_project_claude_dir = _measure_compat.bind(_measure_providers['runtime']._project_claude_dir, __name__)


_skill_scan_dirs = _measure_compat.bind(_measure_providers['runtime']._skill_scan_dirs, __name__)


_agent_scan_dirs = _measure_compat.bind(_measure_providers['runtime']._agent_scan_dirs, __name__)


estimate_tokens_from_frontmatter = _measure_compat.bind(_measure_providers['runtime'].estimate_tokens_from_frontmatter, __name__)


count_lines = _measure_compat.bind(_measure_providers['runtime'].count_lines, __name__)


resolve_real_path = _measure_compat.bind(_measure_providers['runtime'].resolve_real_path, __name__)


_encode_project_dir_name = _measure_compat.bind(_measure_providers['runtime']._encode_project_dir_name, __name__)


# MSIX-packaged apps (the Claude desktop app on Windows) get a virtualized
# %APPDATA%: after any `cd` in Git Bash the Win32 cwd is the physical
# ...\\AppData\\Local\\Packages\\<pkg>\\LocalCache\\Roaming\\... path, while Claude Code
# names project dirs from the logical ...\\AppData\\Roaming\\... path (#265).
_MSIX_REDIRECT_RE = re.compile(
    r"^(?P<home>[A-Za-z]:[\\/]Users[\\/][^\\/]+)[\\/]AppData[\\/]Local[\\/]Packages[\\/]"
    r"[^\\/]+[\\/]LocalCache[\\/](?P<kind>Roaming|Local)(?P<rest>(?:[\\/].*)?)$",
    re.IGNORECASE,
)


_unvirtualize_msix_path = _measure_compat.bind(_measure_providers['runtime']._unvirtualize_msix_path, __name__)


cwd_to_project_dir_name = _measure_compat.bind(_measure_providers['runtime'].cwd_to_project_dir_name, __name__)


_project_dir_from_transcript = _measure_compat.bind(_measure_providers['runtime']._project_dir_from_transcript, __name__)


find_projects_dir = _measure_compat.bind(_measure_providers['runtime'].find_projects_dir, __name__, defaults=(None, None, True,))


get_session_baselines = _measure_compat.bind(_measure_providers['runtime'].get_session_baselines, __name__, defaults=(10,))


# ========== MCP server discovery ==========
#
# Real MCP servers live in three places. The pre-fix implementation read only
# ~/.claude/settings.json (which carries no mcpServers block on a normal
# install) and the Claude DESKTOP app config (a different product whose servers
# never enter a Claude Code context), so it reported 0 servers and made the
# deferred-vs-eager structural saving read as zero.
#
#   1. ~/.claude.json -- the top-level "mcpServers" block AND every
#      projects.<path>.mcpServers block. Most servers live here. A server can
#      appear under many project keys; it is counted ONCE.
#   2. Plugin-provided servers -- each enabled plugin's .mcp.json (or the
#      "mcpServers" key of its plugin.json), resolved through
#      ~/.claude/plugins/installed_plugins.json.
#   3. Account-level connectors (Gmail, Google Drive, Google Calendar,
#      Notion, ...) -- provisioned SERVER-SIDE at claude.ai, present in no
#      local file. They are reported as a separate NOT MEASURED category and
#      are never estimated, guessed, or folded into server_count/tokens.

# Keys that mark a dict as an MCP server definition rather than arbitrary JSON.
_MCP_SERVER_DEF_KEYS = ("command", "url", "httpUrl", "type", "args", "transport")

_MCP_CONNECTORS_NOTE = (
    "Account-level connectors (Gmail, Google Drive, Google Calendar, Notion, ...) "
    "are provisioned server-side at claude.ai and appear in NO local config file. "
    "They are NOT counted in server_count and NOT estimated. Eagerly loaded "
    "connector tool schemas enter the context the same way and are likewise "
    "absent from every local file, so they are NOT estimated either."
)

_MCP_SERVER_INSTRUCTIONS_NOTE = (
    "Each MCP server can supply an `instructions` block at handshake time "
    "(Claude Code caps it at 2 KB of text, ~500 tokens). Instructions live in "
    "the server's runtime response, not in any local config file, so they are "
    "NOT counted in the MCP estimate. The MCP line is the tool surface only."
)


_mcp_read_json = _measure_compat.bind(_measure_providers['audit']._mcp_read_json, __name__, defaults=(None,))


_mcp_servers_from_blob = _measure_compat.bind(_measure_providers['audit']._mcp_servers_from_blob, __name__)


_mcp_plugin_config_paths = _measure_compat.bind(_measure_providers['audit']._mcp_plugin_config_paths, __name__, defaults=(None, None,))


get_mcp_config_paths = _measure_compat.bind(_measure_providers['audit'].get_mcp_config_paths, __name__, defaults=(None, None, None,))


count_mcp_tools_and_servers = _measure_compat.bind(_measure_providers['audit'].count_mcp_tools_and_servers, __name__, defaults=(None, None, None,))


_has_paths_frontmatter = _measure_compat.bind(_measure_providers['audit']._has_paths_frontmatter, __name__)


_detect_imports = _measure_compat.bind(_measure_providers['audit']._detect_imports, __name__)


TOKEN_RELEVANT_ENV_VARS = [
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
    "CLAUDE_CODE_MAX_THINKING_TOKENS",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    "MAX_MCP_OUTPUT_TOKENS",
    "ENABLE_TOOL_SEARCH",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
    "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING",
    "BASH_MAX_OUTPUT_LENGTH",
    "ENABLE_CLAUDEAI_MCP_SERVERS",
]


_check_settings_env = _measure_compat.bind(_measure_providers['audit']._check_settings_env, __name__)


_get_frontmatter_description_length = _measure_compat.bind(_measure_providers['audit']._get_frontmatter_description_length, __name__)


_scan_plugin_skills_and_commands = _measure_compat.bind(_measure_providers['audit']._scan_plugin_skills_and_commands, __name__)


measure_components = _measure_compat.bind(_measure_providers['audit'].measure_components, __name__)


_measure_codex_components = _measure_compat.bind(_measure_providers['audit']._measure_codex_components, __name__)


_measure_cursor_components = _measure_compat.bind(_measure_providers['audit']._measure_cursor_components, __name__)


_HERMES_SKILL_SCAN_CAP = 5000


_hermes_config = _measure_compat.bind(_measure_providers['audit']._hermes_config, __name__)


_hermes_config_fallback = _measure_compat.bind(_measure_providers['audit']._hermes_config_fallback, __name__)


_hermes_skill_inventory = _measure_compat.bind(_measure_providers['audit']._hermes_skill_inventory, __name__, defaults=(None,))


_hermes_mcp_inventory = _measure_compat.bind(_measure_providers['audit']._hermes_mcp_inventory, __name__)


_measure_hermes_components = _measure_compat.bind(_measure_providers['audit']._measure_hermes_components, __name__)


_measure_antigravity_components = _measure_compat.bind(_measure_providers['audit']._measure_antigravity_components, __name__)


calculate_totals = _measure_compat.bind(_measure_providers['audit'].calculate_totals, __name__)


_is_1m_model = _measure_compat.bind(_measure_providers['audit']._is_1m_model, __name__)


_CLAUDE_MODEL_ID_RE = re.compile(
    r"^(?:claude[-_])?(fable|mythos|opus|sonnet|haiku)"
    r"(?:[-_](\d+))?(?:[-_](\d+))?"
)


_claude_model_window = _measure_compat.bind(_measure_providers['audit']._claude_model_window, __name__)


_configured_model_string = _measure_compat.bind(_measure_providers['audit']._configured_model_string, __name__)


_plain_46_family = _measure_compat.bind(_measure_providers['audit']._plain_46_family, __name__)


_promote_plain_46_window = _measure_compat.bind(_measure_providers['audit']._promote_plain_46_window, __name__)


_context_window_for_model_str = _measure_compat.bind(_measure_providers['audit']._context_window_for_model_str, __name__)


# ---------------------------------------------------------------------------
# Effective compact-window resolver (the single source for "where this session
# will auto-compact"). Verified 2026-10-10 from
# code.claude.com/docs/en/model-config#context-window-and-auto-compaction and
# the settings reference:
#   precedence: env CLAUDE_CODE_AUTO_COMPACT_WINDOW > per-model
#   modelSettings[<canonical id>].autoCompactWindow (written by /autocompact)
#   > top-level autoCompactWindow > default (~967K on 1M-native models, the
#   model limit on 200K models). Explicit values clamp to 100000..1000000 and
#   cap at the model's own window. CLAUDE_AUTOCOMPACT_PCT_OVERRIDE is then the
#   percentage of the window already used when compaction runs -- lower values
#   compact EARLIER and it can never raise the threshold.
#
# modelSettings shape: verified 2026-10-10 from a real settings.json and from the
# Claude Code 2.1.296 binary's schema --
#   "modelSettings": {"claude-opus-5-5": {"autoCompactWindow": 500000 | "auto"}, ...}
#   keyed by FULL model id, one object per model; /autocompact writes it.
#   "auto" means the window tuned for the model and replaces the top-level
#   autoCompactWindow for that model (it does not fall through to it).
# ---------------------------------------------------------------------------
_COMPACT_WINDOW_MIN = 100_000
_COMPACT_WINDOW_MAX = 1_000_000
# Native 1M models compact at ~967K by default (docs: "approximately 967K").
_COMPACT_WINDOW_1M_DEFAULT = 967_000


# Host parse of CLAUDE_CODE_AUTO_COMPACT_WINDOW (Claude Code 2.1.296 Dd/FOo/GFe):
# scientific notation and thousand separators first, then a decimal prefix.
# NaN or <= 0 is INVALID and IGNORED (the next source applies); a valid value is
# capped at 1M and floored at 100K by _clamp_compact_window. Shared vectors:
# tests/fixtures/compact_window_env_vectors.json (Python, statusline.js, parse.ts).
_HOST_SCI_RE = re.compile(r"^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)[eE][+-]?[0-9]+$")
_HOST_GROUPED_RE = re.compile("^[+-]?[0-9]{1,3}([_,\u00a0\u202f ])[0-9]{3}(?:\\1[0-9]{3})*$")
_HOST_SEPARATOR_RE = re.compile("[_,\u00a0\u202f ]")
_HOST_INT_PREFIX_RE = re.compile(r"^[+-]?[0-9]+")
_HOST_FLOAT_PREFIX_RE = re.compile(r"^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?")


_host_parse_int = _measure_compat.bind(_measure_providers['audit']._host_parse_int, __name__)


_parse_compact_window_value = _measure_compat.bind(_measure_providers['audit']._parse_compact_window_value, __name__)


_clamp_compact_window = _measure_compat.bind(_measure_providers['audit']._clamp_compact_window, __name__)


_canonical_compact_model_id = _measure_compat.bind(_measure_providers['audit']._canonical_compact_model_id, __name__)


_model_settings_window = _measure_compat.bind(_measure_providers['audit']._model_settings_window, __name__)


_compact_window_env = _measure_compat.bind(_measure_providers['audit']._compact_window_env, __name__)


_resolve_compact_window = _measure_compat.bind(_measure_providers['audit']._resolve_compact_window, __name__, defaults=(None, None,))


effective_compact_window = _measure_compat.bind(_measure_providers['audit'].effective_compact_window, __name__, defaults=(None, None,))


_codex_config_cache: tuple[float, dict] | None = None


_read_codex_config = _measure_compat.bind(_measure_providers['audit']._read_codex_config, __name__)


_codex_config_int = _measure_compat.bind(_measure_providers['audit']._codex_config_int, __name__)


_codex_config_model = _measure_compat.bind(_measure_providers['audit']._codex_config_model, __name__)


_latest_codex_logged_context_window = _measure_compat.bind(_measure_providers['audit']._latest_codex_logged_context_window, __name__)


detect_context_window = _measure_compat.bind(_measure_providers['audit'].detect_context_window, __name__)


# CLI override for context size (set by --context-size flag parsing)
_cli_context_size = None


# Long-context quality curves.
#
# These are quality estimates, not hard truth. They calibrate the "context fill"
# signal differently by model family so a GPT-5.5 Codex session is not scored
# with an Anthropic-shaped 1M curve. The rest of the quality score still comes
# from session behavior: stale reads, bloated results, duplicates, compactions,
# decision density, and agent efficiency.
#
# Claude/legacy curve: fill percentage -> estimated quality score.
_MRCR_CURVE = [
    (0.0, 98),   # Near-empty: peak performance
    (0.10, 96),  # 100K filled: minimal degradation
    (0.25, 93),  # 250K filled: published 256K MRCR
    (0.50, 88),  # 500K filled: "lost in the middle" begins
    (0.60, 84),  # 600K: noticeable degradation
    (0.70, 80),  # 700K: auto-compact zone
    (0.80, 78),  # 800K: significant quality drop
    (0.90, 77),  # 900K: severe
    (1.00, 76),  # 1M filled: published 1M MRCR
]

_OPENAI_GPT55_MRCR_TOKENS = [
    # OpenAI published MRCR v2 8-needle bins for GPT-5.5. Values are smoothed
    # into a monotonic risk curve so "more full" never improves the fill signal
    # just because one benchmark bin was noisy.
    (0, 98),
    (8_000, 98),
    (16_000, 96),
    (32_000, 94),
    (64_000, 90),
    (128_000, 86),
    (256_000, 84),
    (512_000, 81),
    (1_000_000, 74),
]

_OPENAI_GPT5_MRCR_TOKENS = [
    # GPT-5-family fallback. OpenAI reports better long-context scaling than
    # older baselines, but non-5.5 releases should stay slightly conservative.
    (0, 98),
    (32_000, 94),
    (64_000, 90),
    (128_000, 85),
    (256_000, 80),
    (512_000, 72),
    (1_000_000, 64),
]

_GEMINI_MRCR_TOKENS = [
    # Gemini 3/3.1 Pro: steepest degradation among frontier models.
    # MRCR v2 8-needle: 84.9% at 128K, collapses to 26.3% at 1M.
    (0, 98),
    (8_000, 97),
    (32_000, 95),
    (64_000, 92),
    (128_000, 85),
    (256_000, 72),
    (512_000, 50),
    (1_000_000, 26),
]


_interpolate_curve = _measure_compat.bind(_measure_providers['audit']._interpolate_curve, __name__)


_quality_curve_for_model = _measure_compat.bind(_measure_providers['audit']._quality_curve_for_model, __name__)


_estimate_quality_from_fill = _measure_compat.bind(_measure_providers['audit']._estimate_quality_from_fill, __name__, defaults=(None, None,))


_estimate_quality_with_curve = _measure_compat.bind(_measure_providers['audit']._estimate_quality_with_curve, __name__, defaults=(None, None,))


_degradation_band = _measure_compat.bind(_measure_providers['audit']._degradation_band, __name__)


score_to_grade = _measure_compat.bind(_measure_providers['audit'].score_to_grade, __name__)


score_to_band = _measure_compat.bind(_measure_providers['audit'].score_to_band, __name__)


_estimate_messages_until_compact = _measure_compat.bind(_measure_providers['audit']._estimate_messages_until_compact, __name__, defaults=(5000, None,))


_auto_snapshot = _measure_compat.bind(_measure_providers['audit']._auto_snapshot, __name__)


quick_scan = _measure_compat.bind(_measure_providers['audit'].quick_scan, __name__, defaults=(False,))


doctor = _measure_compat.bind(_measure_providers['audit'].doctor, __name__, defaults=(False,))


git_context = _measure_compat.bind(_measure_providers['audit'].git_context, __name__, defaults=(False,))


drift_check = _measure_compat.bind(_measure_providers['audit'].drift_check, __name__, defaults=(False,))


detect_calibration_gap = _measure_compat.bind(_measure_providers['audit'].detect_calibration_gap, __name__, defaults=(None,))


sanitize_label = _measure_compat.bind(_measure_providers['audit'].sanitize_label, __name__)


_build_snapshot = _measure_compat.bind(_measure_providers['audit']._build_snapshot, __name__)


take_snapshot = _measure_compat.bind(_measure_providers['audit'].take_snapshot, __name__)


_auto_capture_pristine_baseline = _measure_compat.bind(_measure_providers['audit']._auto_capture_pristine_baseline, __name__)


print_snapshot_summary = _measure_compat.bind(_measure_providers['audit'].print_snapshot_summary, __name__)


compare_snapshots = _measure_compat.bind(_measure_providers['audit'].compare_snapshots, __name__)


full_report = _measure_compat.bind(_measure_providers['audit'].full_report, __name__)


def _daemon_is_running():
    """Return True iff the dashboard daemon identity probe passes.

    Uses the same magic-string check as `daemon-status` so a foreign
    process on 24842 is never mistaken for ours. Short timeout (0.5s)
    because this is called from interactive paths where users are
    waiting for a browser to open.
    """
    import urllib.error
    import urllib.request

    magic = DAEMON_IDENTITY_MAGIC.encode("utf-8")
    for host in ("127.0.0.1", "[::1]"):
        url = f"http://{host}:{DAEMON_PORT}/__to_ping"
        try:
            with urllib.request.urlopen(url, timeout=0.5) as resp:
                if resp.read(len(magic) + 8).strip() == magic:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return False


def _launch_opener(argv):
    """Run a browser opener detached from our terminal (#199).

    The opener and the browser it starts must not inherit our stdin/stdout/
    stderr: Chrome logs freely to whatever stderr it gets, which painted over
    TUI hosts like OpenCode. A new session keeps a TUI's Ctrl-C from reaching
    the browser. We still wait briefly so a failing opener (bad exit code)
    falls back to printing the URL; a slow opener is assumed to have launched.
    """
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=(os.name == "posix"),
        creationflags=_NO_WINDOW,
    )
    try:
        rc = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        # A slow or hung opener: reap it in the background (no zombie, no
        # ResourceWarning) and say where the page is, so a hang is never silent.
        import threading
        threading.Thread(target=proc.wait, daemon=True).start()
        print(f"  Browser is slow to open. If nothing appears, open: {argv[-1]}")
        return
    if rc != 0:
        raise subprocess.CalledProcessError(rc, argv)


def _open_in_browser(filepath):
    """Open a file in the default browser. Cross-platform.

    Kept for non-dashboard callers. For dashboard flows use
    _open_dashboard() which prefers the bookmarkable URL when the
    daemon is live.
    """
    filepath = str(filepath)
    system = platform.system()
    try:
        if system == "Darwin":
            _launch_opener(["open", filepath])
        elif system == "Linux":
            _launch_opener(["xdg-open", filepath])
        elif system == "Windows":
            os.startfile(filepath)
        else:
            raise OSError(f"Unsupported platform: {system}")
    except (subprocess.CalledProcessError, OSError, FileNotFoundError):
        url = Path(filepath).as_uri()
        print("\n  Could not auto-open browser. Open manually:")
        print(f"  {url}")


def _open_dashboard(fallback_filepath):
    """Open the dashboard, preferring the bookmarkable URL.

    When the daemon is live, open http://localhost:24842/token-optimizer
    so the user lands on their bookmark-ready URL instead of a
    throwaway file:// path. The caller must still have written the
    freshest content to DASHBOARD_PATH so the daemon serves current
    data -- this helper doesn't touch files, only decides which
    address to open.

    Falls back to opening the file directly when the daemon isn't
    live or the identity probe fails.
    """
    if _daemon_is_running():
        url = f"http://localhost:{DAEMON_PORT}/token-optimizer"
        system = platform.system()
        try:
            if system == "Darwin":
                _launch_opener(["open", url])
            elif system == "Linux":
                _launch_opener(["xdg-open", url])
            elif system == "Windows":
                os.startfile(url)
            else:
                raise OSError(f"Unsupported platform: {system}")
            print(f"  Opened: {url}")
            return
        except (subprocess.CalledProcessError, OSError, FileNotFoundError):
            print(f"\n  Could not auto-open browser. Open manually:\n  {url}")
            return
    # Daemon down: open the file directly
    _open_in_browser(fallback_filepath)


def _serve_dashboard(filepath, port=8080, host="127.0.0.1"):
    """Serve the dashboard over HTTP for headless/remote access."""
    import http.server
    import socketserver
    import socket

    filepath = Path(filepath).resolve()
    serve_dir = str(filepath.parent)
    filename = filepath.name

    # Find an available port if the default is taken
    for attempt_port in range(port, port + 20):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host, attempt_port))
            port = attempt_port
            break
        except OSError:
            continue
    else:
        print(f"  Error: no available port in range {port}-{port + 19}")
        sys.exit(1)

    handler = http.server.SimpleHTTPRequestHandler

    class DashboardHandler(handler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=serve_dir, **kw)

        def log_message(self, *args):
            pass  # suppress per-request logs (overrides BaseHTTPRequestHandler)

        def end_headers(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy", _DASHBOARD_CSP,
            )
            super().end_headers()

        def _is_dashboard_request(self):
            requested = self.path.lstrip("/").split("?")[0]
            return requested in ("", "token-optimizer", filename)

        def _redirect_root(self):
            if self._is_dashboard_request():
                if self.path.lstrip("/").split("?")[0] != filename:
                    self.send_response(302)
                    self.send_header("Location", f"/{filename}")
                    self.end_headers()
                    return True
            return False

        def _check_allowed(self):
            """Only serve the dashboard file itself, nothing else."""
            if not self._is_dashboard_request():
                self.send_error(403, "Forbidden")
                return False
            return True

        def do_GET(self):
            path_only = self.path.split("?")[0]
            # API health probe (lets dashboard detect our server vs generic)
            if path_only == "/api/health":
                self._json_response(200, {"ok": True, "server": "token-optimizer"})
                return
            # v5.4.19 identity magic probe (defeats foreign-port masquerade)
            if path_only == "/__to_ping":
                self._json_response(200, {"ok": True, "magic": DAEMON_IDENTITY_MAGIC})
                return
            # v5.4.19 per-install token endpoint (H-2/M-4). Only served to
            # localhost Origin+Host so a foreign site cannot exfiltrate it.
            if path_only == "/api/token":
                origin = self.headers.get("Origin", "")
                host = self.headers.get("Host", "")
                origin_ok = (not origin) or any(
                    origin.startswith(p) for p in ("http://127.0.0.1:", "http://localhost:", "http://[::1]:")
                )
                if not origin_ok or not _is_localhost_host_header(host):
                    self.send_error(403, "Forbidden")
                    return
                self._json_response(200, {"token": _read_daemon_token()})
                return
            # Live savings recompute. A browser refresh against the daemon shows
            # current savings/cache numbers WITHOUT paying for a full (multi-second)
            # dashboard regen: the page fetches this and patches the tiles in place.
            # Read-only, localhost-guarded (same DNS-rebinding defense as the static
            # assets below), fail-open (a slow/failed eval degrades to a safe shape).
            if path_only == "/api/savings":
                if not _is_localhost_host_header(self.headers.get("Host", "")):
                    self.send_error(421, "Misdirected Request")
                    return
                try:
                    payload = _live_savings_payload(days=30)
                except Exception:
                    self._json_response(500, {"ok": False})
                    return
                self._json_response(200, payload)
                return
            if self._redirect_root():
                return
            if not self._check_allowed():
                return
            # H-1 DNS rebinding defense: GETs to static assets must come from localhost Host
            if not _is_localhost_host_header(self.headers.get("Host", "")):
                self.send_error(421, "Misdirected Request")
                return
            super().do_GET()

        def do_HEAD(self):
            if self._redirect_root():
                return
            if not self._check_allowed():
                return
            if not _is_localhost_host_header(self.headers.get("Host", "")):
                self.send_error(421, "Misdirected Request")
                return
            super().do_HEAD()

        def do_POST(self):
            """Handle API requests for skill/MCP management.

            v5.4.19 hardening (C-2/H-1/M-4):
              - Reject empty Origin (no more bypass via curl/fetch-no-cors).
              - Enforce Host allowlist (defeats DNS rebinding).
              - Require X-TO-Token matching per-install secret.
            """
            # C-2 fix: require a non-empty, localhost-prefixed Origin.
            origin = self.headers.get("Origin", "")
            if not origin or not any(origin.startswith(p) for p in ("http://127.0.0.1:", "http://localhost:", "http://[::1]:")):
                self.send_error(403, "Forbidden: invalid origin")
                return

            # H-1 fix: Host header allowlist (localhost / 127.0.0.1 / [::1]).
            if not _is_localhost_host_header(self.headers.get("Host", "")):
                self.send_error(421, "Misdirected Request")
                return

            # M-4 fix: constant-time per-install token check.
            expected_tok = _read_daemon_token()
            got_tok = self.headers.get("X-TO-Token", "")
            if not expected_tok or not hmac.compare_digest(expected_tok, got_tok):
                self.send_error(403, "Forbidden: invalid token")
                return

            path = self.path.split("?")[0]

            # Body size limit
            MAX_BODY = 65536  # 64KB
            content_len = int(self.headers.get("Content-Length", 0))
            if content_len > MAX_BODY:
                self.send_error(413, "Request body too large")
                return

            # Read JSON body
            body = {}
            if content_len > 0:
                try:
                    body = json.loads(self.rfile.read(content_len))
                except (json.JSONDecodeError, ValueError):
                    pass

            if path == "/api/session-turns":
                raw_path = body.get("jsonl_path", "")
                if not raw_path:
                    self._json_response(400, {"error": "Missing 'jsonl_path' field"})
                    return
                try:
                    jsonl_path = Path(raw_path).expanduser().resolve()
                    allowed_root = (CLAUDE_DIR / "projects").resolve()
                    jsonl_path.relative_to(allowed_root)
                except (ValueError, OSError):
                    self._json_response(403, {"error": "Forbidden: invalid session path"})
                    return
                if not jsonl_path.exists():
                    self._json_response(404, {"error": "Session log not found"})
                    return
                turns = parse_session_turns(jsonl_path)
                self._json_response(200, {"ok": True, "turns": turns})
                return

            name = body.get("name", "")
            if not name:
                self._json_response(400, {"error": "Missing 'name' field"})
                return

            ok = False
            msg = ""
            if path == "/api/skill/archive":
                if detect_runtime() == "codex":
                    ok = _manage_codex_skill("disable", raw_path=name)
                    msg = f"Disabled Codex skill: {name}" if ok else f"Failed to disable Codex skill: {name}"
                else:
                    ok = _manage_skill("archive", name)
                    msg = f"Archived skill: {name}" if ok else f"Failed to archive: {name}"
            elif path == "/api/skill/restore":
                if detect_runtime() == "codex":
                    ok = _manage_codex_skill("enable", raw_path=name)
                    msg = f"Enabled Codex skill: {name}" if ok else f"Failed to enable Codex skill: {name}"
                else:
                    ok = _manage_skill("restore", name)
                    msg = f"Restored skill: {name}" if ok else f"Failed to restore: {name}"
            elif path == "/api/mcp/disable":
                if detect_runtime() == "codex":
                    ok = _manage_codex_mcp("disable", name)
                    msg = f"Disabled Codex MCP server: {name}" if ok else f"Failed to disable Codex MCP: {name}"
                else:
                    ok = _manage_mcp("disable", name)
                    msg = f"Disabled MCP server: {name}" if ok else f"Failed to disable: {name}"
            elif path == "/api/mcp/enable":
                if detect_runtime() == "codex":
                    ok = _manage_codex_mcp("enable", name)
                    msg = f"Enabled Codex MCP server: {name}" if ok else f"Failed to enable Codex MCP: {name}"
                else:
                    ok = _manage_mcp("enable", name)
                    msg = f"Enabled MCP server: {name}" if ok else f"Failed to enable: {name}"
            elif path == "/api/v5/toggle":
                # v5 feature toggle: body has {"name": "feature_name", "enabled": true/false}
                enabled = bool(body.get("enabled", False))
                if name not in V5_FEATURES:
                    self._json_response(400, {"error": f"Unknown v5 feature: {name}"})
                    return
                # An env-managed feature can't be toggled from the dashboard: a
                # config.json write would be silently shadowed by the env var,
                # leaving the UI out of sync with runtime. Reject it honestly
                # rather than pretend the toggle took effect.
                if _resolve_feature_env(V5_FEATURES[name]["env_var"]) is not None:
                    self._json_response(409, {
                        "ok": False,
                        "msg": f"{V5_FEATURES[name]['label']} is set via an environment "
                               f"variable and cannot be toggled from the dashboard. "
                               f"Change {V5_FEATURES[name]['env_var']} in your settings.",
                    })
                    return
                ok = _set_v5_feature(name, enabled)
                label = V5_FEATURES[name]["label"]
                msg = f"{'Enabled' if enabled else 'Disabled'}: {label}" if ok else f"Failed to toggle {name}"
                # Strip internal implementation keys before returning over HTTP
                public_status = {}
                for fname, info in _get_v5_feature_status().items():
                    public_status[fname] = {k: v for k, v in info.items() if k not in ("env_var", "config_key")}
                # Return updated status so dashboard refreshes
                self._json_response(200 if ok else 500, {
                    "ok": ok,
                    "msg": msg,
                    "v5_features": public_status,
                })
                return
            else:
                self._json_response(404, {"error": "Unknown endpoint"})
                return

            # After state change, regenerate dashboard data for the manage tab
            fresh_manage = None
            if ok:
                try:
                    fresh_manage = _collect_management_data()
                except Exception:
                    pass

            self._json_response(
                200 if ok else 500,
                {"ok": ok, "message": msg, "manage": fresh_manage}
            )

        def _json_response(self, code, data):
            body = json.dumps(data, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            origin = self.headers.get("Origin", "")
            server_port = getattr(self.server, "server_port", None)
            if server_port is None and getattr(self.server, "server_address", None):
                server_port = self.server.server_address[1]
            if origin in (f"http://127.0.0.1:{server_port}", f"http://localhost:{server_port}"):
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            """Handle CORS preflight."""
            self.send_response(204)
            origin = self.headers.get("Origin", "")
            server_port = getattr(self.server, "server_port", None)
            if server_port is None and getattr(self.server, "server_address", None):
                server_port = self.server.server_address[1]
            if origin in (f"http://127.0.0.1:{server_port}", f"http://localhost:{server_port}"):
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-TO-Token")
            self.end_headers()

    display_host = "localhost" if host == "127.0.0.1" else host
    print("\n  Serving dashboard at:")
    print(f"    http://{display_host}:{port}/")
    if host == "0.0.0.0":
        print("    (accessible from any machine on your network)")
    print("\n  Press Ctrl+C to stop.\n")

    with socketserver.TCPServer((host, port), DashboardHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  Server stopped.")


def _sanitize_dashboard_paths(data):
    """Redact home-directory prefixes from paths embedded in dashboard HTML.

    Applies to ALL runtimes (Claude Code and Codex). Replaces /Users/xxx
    or /home/xxx with ~ so the rendered dashboard doesn't expose the full
    filesystem path. Paths that the daemon API needs (e.g. jsonl_path for
    on-demand session-turn loading) are kept functional since ~ is only
    cosmetic in the JSON, and the API endpoint resolves paths itself.
    """
    trends = data.get("trends")
    if isinstance(trends, dict):
        for day in trends.get("daily", []) or []:
            for session in day.get("session_details", []) or []:
                path = session.get("jsonl_path")
                if path:
                    session["jsonl_path"] = _display_path(path)

    quality = data.get("quality")
    if isinstance(quality, dict):
        sf = quality.get("session_file")
        if sf:
            quality["session_file"] = Path(sf).name

    mr = data.get("memory_review")
    if isinstance(mr, dict):
        target = mr.get("target")
        if target:
            mr["target"] = _display_path(target)

    return data


def _codex_state_summary():
    """Collect Codex runtime-state metrics for display, or None unless Codex.

    Combines read-only SQLite state (subagent costs, memory overhead, goal
    budgets) with the current session's parsed signals (rate limits, effort,
    compaction compression). Returns None on the Claude runtime so dashboard,
    report, and doctor consumers can treat the key as optional (no Claude-path
    behavior change). All sources degrade to empty rather than raising.
    """
    if detect_runtime() != "codex":
        return None
    # Resolve the invoking thread id ONCE (CODEX_THREAD_ID) and scope every
    # thread-specific Codex-state metric to it: subagent and goal totals to
    # its goal subtree. When the identity is unresolved, do NOT fall back to
    # the whole-DB aggregation for those: that would present other threads'
    # tokens and goals as the current thread's. Report an explicit unresolved
    # state instead. Memory overhead is different: it is a machine-wide
    # metric across all threads on this machine, so it is measured without a
    # thread filter and needs no identity.
    try:
        current_tid = codex_state.current_thread_id()
    except Exception:
        current_tid = None
    res = _codex_thread_resolution()
    if current_tid:
        subagents = codex_state.subagent_costs(root_thread_id=current_tid)
        goals = codex_state.goal_budgets(root_thread_id=current_tid)
    else:
        subagents = {**codex_state._empty_subagents(), "unresolved": True}
        goals = {**codex_state._empty_goals(), "unresolved": True}
    memory = codex_state.memory_overhead()
    summary = {
        "subagents": subagents,
        "memory": memory,
        "goals": goals,
        "selected_thread_id": current_tid,
        "thread_resolution": res["status"],
        "thread_resolution_reason": res["reason"],
        "rate_limits": None,
        "effort": None,
        "compaction": None,
    }
    import codex_models
    summary['available_models'] = codex_models.visible_models()
    try:
        # Session signals follow the same identity: never the newest-mtime file.
        current = res["path"]
        if current:
            session = codex_session.parse_session_jsonl(current)
            if session:
                summary.update({
                    "rate_limits": session.get("rate_limits"),
                    "effort": session.get("effort"),
                    "effort_breakdown": session.get("effort_breakdown"),
                    "tool_duration_p90_ms": session.get("tool_duration_p90_ms"),
                    "task_duration_ms_max": session.get("task_duration_ms_max"),
                    "ttft_ms_avg": session.get("ttft_ms_avg"),
                })
            quality = codex_session.parse_jsonl_for_quality(current)
            if quality and quality.get("compaction_ratios"):
                ratios = [r["ratio"] for r in quality["compaction_ratios"] if r.get("ratio") is not None]
                summary["compaction"] = {
                    "count": quality.get("compactions", 0),
                    "ratios": quality["compaction_ratios"],
                    "avg_ratio": round(sum(ratios) / len(ratios), 3) if ratios else None,
                }
    except Exception:
        pass
    return summary


def codex_state_report(as_json=False):
    """Print the Codex runtime-state summary (subagents, memory, goals, signals)."""
    summary = _codex_state_summary()
    if as_json:
        print(json.dumps(summary, indent=2, default=str))
        return
    if summary is None:
        print("Codex runtime state is only available on the Codex runtime.")
        return
    print("\nToken Optimizer - Codex Runtime State")
    print("=" * 38)

    if summary.get("thread_resolution") == "unresolved":
        reason = summary.get("thread_resolution_reason")
        why = (f"no transcript found for {summary.get('selected_thread_id')}"
               if reason == "transcript_not_found" else _codex_identity_reason())
        print(f"\nCurrent thread: unresolved ({why}). "
              + ("Session signals are not shown; thread-scoped subagent and goal totals are not shown."
                 if not summary.get("selected_thread_id")
                 else f"Session signals are not shown; totals below are for thread {summary.get('selected_thread_id')}, not its transcript."))

    sub = summary["subagents"]
    if sub.get("unresolved"):
        print("\nSubagents: unresolved (no invoking thread id)")
    elif sub.get("available"):
        print(f"\nSubagents: {sub['total_subagents']} total "
              f"({sub['open_subagents']} open, {sub['closed_subagents']} closed, "
              f"{sub['leaked_subagents']} leaked)")
        print(f"  Subagent token cost: {sub['total_child_tokens']:,}")
        for leak in sub.get("leaked", [])[:5]:
            age = f"{leak['age_minutes']}min" if leak["age_minutes"] is not None else "unknown age"
            print(f"  ! leaked {leak['child_thread_id'][:16]} ({leak['tokens_used']:,} tokens, open {age})")
    else:
        print("\nSubagents: no state data yet (no spawn edges recorded)")

    mem = summary["memory"]
    if mem.get("available") and mem["thread_count"]:
        print(f"\nMemory overhead: {mem['total_memory_tokens']:,} tokens across {mem['thread_count']}{'+' if mem.get('truncated') else ''} thread(s) on this machine"
              f"{' (partial: scan cap reached)' if mem.get('truncated') else ''}; "
              f"largest {mem['max_thread_memory_tokens']:,}")
    else:
        print("\nMemory overhead: none recorded")

    goals = summary["goals"]
    if goals.get("unresolved"):
        print("\nGoals: unresolved (no invoking thread id)")
    elif goals.get("available") and goals["total_goals"]:
        print(f"\nGoals: {goals['total_goals']} ({goals['active_goals']} active)")
        if goals["budget_limited"] or goals["usage_limited"] or goals["over_budget"]:
            print(f"  ! {goals['budget_limited']} budget-limited, {goals['usage_limited']} usage-limited, "
                  f"{goals['over_budget']} over budget")
    else:
        print("\nGoals: none active")

    rl = summary.get("rate_limits") or {}
    primary = rl.get("primary")
    if isinstance(primary, dict) and primary.get("used_percent") is not None:
        print(f"\nRate limit (primary): {primary['used_percent']:.0f}% used")
    if summary.get("effort"):
        print(f"Effort (dominant this session): {summary['effort']}")
    if summary.get("task_duration_ms_max"):
        print(f"Longest task: {summary['task_duration_ms_max'] / 1000:.0f}s"
              + (f" (first token ~{summary['ttft_ms_avg'] / 1000:.0f}s avg)" if summary.get("ttft_ms_avg") else ""))
    comp = summary.get("compaction")
    if comp and comp.get("avg_ratio") is not None:
        print(f"Compaction: {comp['count']} this session, "
              f"avg compression to {comp['avg_ratio'] * 100:.0f}% of pre-compaction context")
    print()


def generate_dashboard(coord_path):
    """Generate an interactive HTML dashboard from audit results."""
    coord = Path(coord_path)
    if not coord.exists():
        print(f"Error: coord-path does not exist: {coord_path}")
        print(_hint("Usage: python3 measure.py dashboard --coord-path /tmp/token-optimizer-XXXXXXXXXX"))
        sys.exit(1)

    # Locate the template
    script_dir = Path(__file__).resolve().parent
    template_path = script_dir.parent / "assets" / "dashboard.html"
    if not template_path.exists():
        print(f"Error: dashboard template not found at: {template_path}")
        sys.exit(1)

    # Re-measure current state
    print("  Measuring current token overhead...")
    components = measure_components()
    totals = calculate_totals(components)
    baselines = get_session_baselines(5)

    calibration = detect_calibration_gap(components, totals, baselines)

    ctx_window, ctx_source = detect_context_window()
    snapshot = {
        "components": components,
        "totals": totals,
        "session_baselines": baselines,
        "calibration": calibration,
        "context_window": ctx_window,
        "context_source": ctx_source,
    }

    # Read audit files
    audit_dir = coord / "audit"
    audit = {}
    audit_files = {
        "claudemd": "claudemd.md",
        "memorymd": "memorymd.md",
        "skills": "skills.md",
        "mcp": "mcp.md",
        "commands": "commands.md",
        "advanced": "advanced.md",
    }
    for key, filename in audit_files.items():
        fpath = audit_dir / filename
        if fpath.exists():
            try:
                audit[key] = fpath.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                audit[key] = None
        else:
            audit[key] = None

    found = sum(1 for v in audit.values() if v)
    print(f"  Loaded {found}/{len(audit_files)} audit files")

    # Read optimization plan
    plan_path = coord / "analysis" / "optimization-plan.md"
    plan = None
    if plan_path.exists():
        try:
            plan = plan_path.read_text(encoding="utf-8")
            print(f"  Loaded optimization plan ({len(plan)} chars)")
        except (OSError, UnicodeDecodeError):
            pass

    # Collect trends and health data
    print("  Collecting usage trends...")
    try:
        trends = _collect_trends_data(days=30)
    except Exception:
        trends = None
    print("  Checking session health...")
    try:
        health = _collect_health_data()
    except Exception:
        health = None

    # Generate coach data for the Coach tab (reuse already-collected components/trends)
    print("  Generating coach data...")
    try:
        coach = generate_coach_data(components=components, trends=trends)
    except Exception:
        coach = None

    # Collect context quality data (v2.0)
    print("  Analyzing context quality...")
    quality = _collect_quality_for_dashboard()

    # Collect hook installation status for dashboard toggles
    hook_status = _collect_hook_status_for_dashboard()

    # Collect management data for Manage tab. The full audit dashboard should
    # expose the same Codex skill/MCP controls as the standalone dashboard.
    management = _collect_management_data(components=components, trends=trends)

    # Savings data for dashboard. Merged view = measured runtime + realized
    # structural + opportunity (unclaimed) + estimated uncaptured runtime, all
    # tier-labelled. Wrapped so a slow/failed savings eval never blocks regen.
    print("  Collecting savings data...")
    savings_data = _dashboard_savings_data(days=30)

    # U6: tiered compression-coverage diagnostic (kept separate from savings;
    # shadow/opportunity ratios never feed the headline). Fail-open to empty.
    try:
        compression_coverage = _get_compression_coverage(days=30)
    except Exception:
        compression_coverage = {"available": False, "tiers": {}, "first_read_proxy": None}

    # Tripwire verdict for the coverage panel (per-cohort live edit-rate +
    # auto-demotions). Read-only; never recomputes/demotes here. Fail-open.
    try:
        cohort_tripwire = _cohort_tripwire_state()
    except Exception:
        cohort_tripwire = {"cohorts": [], "demoted": []}
    compression_coverage["cohort_tripwire"] = cohort_tripwire

    # Cache-TTL watchdog (opportunity tier — observed waste, potential recovery).
    # Standalone analysis; never reaches the realized savings headline. Fail-open.
    try:
        cache_health = _cache_ttl_waste_cached(days=30)
    except Exception:
        cache_health = {"available": False, "tier": "opportunity"}

    # U7: keep-warm cache-automation sub-block carried inside the cache-health
    # payload (realized vs spend vs NET, tripwire ratio, the 5 tile states,
    # predictor transparency). Fail-open so it never blocks dashboard regen.
    try:
        if isinstance(cache_health, dict):
            cache_health["keepwarm"] = keepwarm_cache_health_block(days=30)
    except Exception:
        pass

    # Fall back to auto-recommendations if LLM plan is missing
    auto_plan_flag = False
    if not plan:
        print("  No LLM plan found, generating auto-recommendations...")
        plan, rec_count = generate_auto_recommendations(components, trends=trends, days=30)
        if plan:
            auto_plan_flag = True
            print(f"  Generated {rec_count} auto-recommendations as fallback")
        else:
            plan = None

    # Subscription runway: leads the savings surface for rate-limited plans,
    # where a dollar figure is the wrong currency. None on API-billed setups or
    # when the meter has never been read at all (a stale-but-present reading is
    # kept and dated on the card), and the template simply omits the card.
    try:
        runway = runway_snapshot(days=30)
    except Exception:
        runway = None

    # Assemble data
    data = {
        "runway": runway,
        "snapshot": snapshot,
        "audit": audit,
        "plan": plan,
        "trends": trends,
        "health": health,
        "coach": coach,
        "quality": quality,
        "manage": management,
        "hooks": hook_status,
        "savings": savings_data,
        "compression_coverage": compression_coverage,
        "cache_health": cache_health,
        "auto_plan": auto_plan_flag,
        "generated_at": datetime.now().isoformat(),
        "version": TOKEN_OPTIMIZER_VERSION,
        "runtime": detect_runtime(),
        "runtime_label": runtime_name_for_humans(),
        "codex_state": _codex_state_summary(),
    }
    data = _sanitize_dashboard_paths(data)

    # Load template and inject data
    template = template_path.read_text(encoding="utf-8")
    data_json = json.dumps(data, ensure_ascii=True, default=str)
    data_json = data_json.replace("</", "<\\/")  # Prevent </script> injection
    placeholder = "window.__TOKEN_DATA__ = null;"
    injected = template.replace(placeholder, f"window.__TOKEN_DATA__ = {data_json};", 1)
    if injected == template:
        print("  [Warning] Data injection failed: placeholder not found in template.")

    # Write audit output to the coord_path (historical behavior so
    # audits keep a self-contained artifact per run).
    out_dir = coord / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "dashboard.html"
    fd = os.open(str(out_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(injected)
    print(f"  Dashboard written to: {out_path}")

    # v5.3.6 / v5.4.10: mirror the same HTML to BOTH dashboard paths so
    # the daemon serves fresh audit content regardless of which path it
    # was configured to use. Same dual-path pattern as v5.4.7 daemon script.
    legacy_dashboard = RUNTIME_DIR / "_backups" / "token-optimizer" / "dashboard.html"
    wrote_mirror = False
    skipped_all = True
    for mirror_path in {DASHBOARD_PATH, legacy_dashboard}:
        # Version-downgrade guard: an older build's audit must not clobber a
        # shared dashboard a newer build already wrote. The per-run audit artifact
        # (out_path) is still written above; only the SHARED served copies are
        # guarded. Fail-open (see _dashboard_on_disk_is_newer).
        if _dashboard_on_disk_is_newer(mirror_path):
            continue
        skipped_all = False
        try:
            mirror_path.parent.mkdir(parents=True, exist_ok=True)
            mirror_fd = os.open(str(mirror_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(mirror_fd, "w", encoding="utf-8") as f:
                f.write(injected)
            # The sidecar must name the build that wrote THIS HTML, or the guard
            # reads a stale version and a middle build could clobber a newer file.
            _write_dashboard_meta(mirror_path)
            wrote_mirror = True
        except OSError:
            pass
    # Only a real write FAILURE warrants the warning; an intentional guard skip
    # (all shared copies are already newer) is not a failure.
    if not wrote_mirror and not skipped_all:
        print("  [Warning] Could not mirror dashboard to daemon paths.")

    # Prefer the bookmarkable URL when the daemon is live; fall back to
    # the coord-path file://. This removes the v5.3.5-era UX bug where
    # users with a live daemon still got a /tmp/coord.../dashboard.html
    # file:// URL instead of their bookmark.
    _open_dashboard(fallback_filepath=out_path)
    return str(out_path)


def _shell_script_path():
    """This script's resolved path, quoted to paste into Bash, PowerShell and cmd.exe."""
    return shell_path(Path(__file__).resolve())


def _measure_cli(*args):
    """The command that runs this script, for hints a user will paste into a shell.

    Always the resolved script path (quoted): a bare `python3 measure.py` only works
    from inside the scripts directory, which is where nobody is. On Windows the
    interpreter is `python`, and the path uses forward slashes and double quotes, the
    one form cmd.exe, PowerShell and Git Bash all read as a single argument
    (`runtime_env.shell_path`).
    """
    parts = [hint_python(), _shell_script_path()]
    parts.extend(args)
    return " ".join(parts)


def _runtime_cli(runtime, *args):
    """`_measure_cli` pinned to a runtime for hints.

    macOS/Linux: the Bash `TOKEN_OPTIMIZER_RUNTIME=NAME python3 <script>` prefix,
    unchanged. cmd.exe and PowerShell cannot parse that, so on Windows it is
    `python <script> --runtime NAME` (a leading global flag, see `consume_runtime_flag`).
    """
    if _windows_hints():
        parts = [hint_python(), _shell_script_path(), "--runtime", runtime]
    else:
        parts = [f"TOKEN_OPTIMIZER_RUNTIME={runtime}", hint_python(), _shell_script_path()]
    parts.extend(args)
    return " ".join(parts)


_ENV_PREFIXED_BARE_HINT = re.compile(r"TOKEN_OPTIMIZER_RUNTIME=(\w+) python3 measure\.py")


def _hint(text):
    """Make a printed hint pasteable from any directory.

    Hints are written as `python3 measure.py <subcommand>`; this swaps that
    bare prefix for the resolved, quoted script path (`_measure_cli()`). A
    `TOKEN_OPTIMIZER_RUNTIME=X` prefix (Bash only) becomes `--runtime X` on Windows.
    """
    text = _ENV_PREFIXED_BARE_HINT.sub(lambda m: _runtime_cli(m.group(1)), text)
    return text.replace("python3 measure.py", _measure_cli())


def _subagent_cache_cmd(action=None):
    """The `subagent-cache [action]` command users are told to run (advice, enable, undo)."""
    return _measure_cli("subagent-cache", *([action] if action else []))


def _display_path(absolute_path):
    """Replace the user's home directory with ~ for display purposes.

    Avoids exposing full filesystem paths like /Users/alex/.claude/... in the UI.
    """
    home = str(Path.home())
    s = str(absolute_path)
    if s == home:
        return "~"
    if s.startswith(home + os.sep):
        return "~" + s[len(home):]
    return s


def _collect_hook_status_for_dashboard():
    """Collect hook installation status for dashboard toggle panel."""
    if detect_runtime() == "codex":
        return _collect_codex_hook_status_for_dashboard()
    if detect_runtime() == "hermes":
        return _collect_hermes_hook_status_for_dashboard()
    if detect_runtime() == "copilot":
        return _collect_copilot_hook_status_for_dashboard()
    if detect_runtime() == "cursor":
        return _collect_cursor_hook_status_for_dashboard()
    if detect_runtime() == "antigravity":
        return _collect_antigravity_hook_status_for_dashboard()
    if detect_runtime() == "grok":
        return _collect_grok_hook_status_for_dashboard()

    settings, _ = _read_settings_json()

    # Check each hook type
    session_end_installed = _is_hook_installed(settings)
    smart_compact_status = _is_smart_compact_installed(settings)

    # For executable commands: absolute path with shlex.quote (handles spaces/quotes)
    mp_cmd = _shell_script_path()

    return {
        "session_end": {
            "installed": session_end_installed,
            "label": "Session Tracking",
            "description": "Collects usage data after each session. Powers Trends and Health tabs.",
            "install_cmd": f"{hint_python()} {mp_cmd} setup-hook",
            "uninstall_cmd": f"{hint_python()} {mp_cmd} setup-hook --uninstall",
        },
        "smart_compact": {
            "installed": all(smart_compact_status.values()),
            "partial": any(smart_compact_status.values()) and not all(smart_compact_status.values()),
            "detail": smart_compact_status,
            "label": "Smart Compaction",
            "description": "Captures session state before compaction, restores it after. Protects your working memory.",
            "install_cmd": f"{hint_python()} {mp_cmd} setup-smart-compact",
            "uninstall_cmd": f"{hint_python()} {mp_cmd} setup-smart-compact --uninstall",
        },
    }


def _collect_codex_hook_status_for_dashboard():
    """Collect Codex project hook status for dashboard toggle panel."""
    import codex_doctor

    mp_cmd = _shell_script_path()
    project = Path.cwd().resolve(strict=False)
    checks = codex_doctor.run_checks(project=project)
    by_name = {check["name"]: check for check in checks}

    def _ok(name):
        return by_name.get(name, {}).get("status") == "OK"

    project_arg = shlex.quote(str(project))
    base = f"{_runtime_cli('codex')} codex-install --project {project_arg}"
    try:
        hooks_text = (project / ".codex" / "hooks.json").read_text(encoding="utf-8")
    except OSError:
        hooks_text = ""
    return {
        "codex_project": {
            "installed": _ok("Project hooks"),
            "label": "Codex Project Hooks",
            "description": "Installs the aggressive default profile (max savings): SessionStart/UserPromptSubmit for quality tracking, Stop for dashboard refresh and checkpoint capture, plus silent PostToolUse archiving and context-intel. All hooks run silently (no visible Codex Desktop rows). Bash compression stays opt-in (Codex can't rewrite command input yet).",
            "install_cmd": base,
            "uninstall_cmd": base + " --uninstall",
        },
        "codex_compact_prompt": {
            "installed": _ok("Compact prompt"),
            "partial": by_name.get("Compact prompt", {}).get("status") == "WARN",
            "label": "Codex Compact Prompt",
            "description": "Adds Token Optimizer compact guidance to Codex config so manual compaction preserves decisions, files, and continuation state.",
            "install_cmd": f"{_runtime_cli('codex')} codex-compact-prompt --install",
            "uninstall_cmd": "Edit ~/.codex/config.toml and remove compact_prompt / experimental_compact_prompt_file",
        },
        "codex_bash_compression": {
            "installed": "PreToolUse" in hooks_text and "bash_hook.py" in hooks_text,
            "partial": False,
            "label": "Experimental Bash Compression",
            "description": "Codex PreToolUse currently cannot rewrite command input, so true invisible Bash compression is not available yet. This hook is experimental and visible.",
            "install_cmd": base + " --enable-bash-compression",
            "uninstall_cmd": base + " --disable-bash-compression",
        },
        "codex_balanced_profile": {
            "installed": "UserPromptSubmit" in hooks_text and "codex_hook_bridge.py" in hooks_text,
            "label": "Balanced Quality Profile",
            "description": "Default profile. Enables prompt/session hooks for live quality cache and loop nudges with far less noise than per-tool hooks.",
            "install_cmd": base,
            "uninstall_cmd": base + " --uninstall",
        },
        "codex_telemetry_profile": {
            "installed": "PostToolUse" in hooks_text and ("archive_result.py" in hooks_text or "context_intel.py" in hooks_text),
            "label": "PostToolUse Telemetry Profile",
            "description": "Enables exact tool-output archiving/context-intel. Wired to run silently (archive redirects output, context-intel emits nothing, Bash-only matcher avoids unsupported MCP replacement), so no visible Codex Desktop rows. Included in the aggressive default.",
            "install_cmd": base + " --profile telemetry",
            "uninstall_cmd": base + " --uninstall",
        },
        "codex_status_line": {
            "installed": _ok("Codex CLI status line"),
            "partial": by_name.get("Codex CLI status line", {}).get("status") == "WARN",
            "label": "Codex CLI Status Line",
            "description": "Shows model, fast mode, context remaining/used, token count, branch, and cwd in the Codex terminal UI.",
            "install_cmd": base + " --enable-status-line",
            "uninstall_cmd": "Edit ~/.codex/config.toml and remove the Token Optimizer [tui] status line block",
        },
        "codex_stop_refresh": {
            "installed": _ok("Feature: Session continuity and dashboard refresh"),
            "label": "Stop Refresh and Continuity",
            "description": "On Codex Stop, collects sessions, regenerates the dashboard, and saves a checkpoint for continuity.",
            "install_cmd": base + " --profile quiet",
            "uninstall_cmd": base + " --uninstall",
        },
    }


def _collect_copilot_hook_status_for_dashboard():
    """Hook status for the dashboard toggle panel under the Copilot runtime.

    Mirrors the Codex/Hermes collectors. copilot_doctor uses lowercase
    statuses ("ok"/"warn"/"fail") and check names from copilot_doctor.py.
    """
    import copilot_doctor  # noqa: PLC0415

    mp_cmd = _shell_script_path()
    checks = copilot_doctor.run_checks()
    by_name = {check["name"]: check for check in checks}

    def _ok(name):
        return by_name.get(name, {}).get("status") == "ok"

    install_cmd = f"{_runtime_cli('copilot')} copilot-install"
    doctor_cmd = f"{_runtime_cli('copilot')} copilot-doctor"

    return {
        "copilot_hooks": {
            "installed": _ok("TO hook config"),
            # Installed stays tied to the hook config; a dead legacy `stop` entry
            # or a bridge that cannot find measure.py (session-end refresh paused), or a
            # damaged hook payload (missing bridge modules) is installed-but-partial,
            # not healthy.
            "partial": any(
                by_name.get(n, {}).get("status") == "warn"
                for n in ("TO hook config", "TO session-end hook", "session-end refresh")
            ) or by_name.get("hook payload", {}).get("status") in ("warn", "fail"),
            "label": "Copilot CLI Hooks",
            "description": "Wires Token Optimizer into ~/.copilot/hooks/: sessionStart continuity restore, preToolUse bash compression (capability-gated), postToolUse crash-recovery tally + nudges, session-end rollup.",
            "install_cmd": install_cmd,
            "uninstall_cmd": f"{_runtime_cli('copilot')} copilot-uninstall",
        },
        "copilot_capabilities": {
            "installed": _ok("capabilities"),
            "partial": any(
                by_name.get(n, {}).get("status") == "warn"
                for n in ("capabilities", "capabilities freshness", "capability matrix age")
            ),
            "label": "Hook Capability Matrix",
            "description": "Per-CLI-version map of which Copilot hook powers actually work (upstream fields break release to release). Engine features auto-gate on it; reseeds when the CLI version changes.",
            "install_cmd": install_cmd,
            "uninstall_cmd": doctor_cmd,
        },
        "copilot_cli_data": {
            "installed": _ok("CLI session data"),
            "partial": by_name.get("CLI session data", {}).get("status") == "warn",
            "label": "Copilot CLI Sessions",
            "description": "Reads ~/.copilot/session-state/ for per-session token totals, compactions, and tool activity. Crash-killed sessions are recovered from partial data.",
            "install_cmd": doctor_cmd,
            "uninstall_cmd": doctor_cmd,
        },
        "copilot_vscode_data": {
            "installed": _ok("VS Code debug-logs") or _ok("VS Code OTel DB"),
            "partial": by_name.get("VS Code data plane", {}).get("status") == "warn",
            "label": "VS Code Copilot Data",
            "description": "Per-request AI-credit cost from Copilot Chat debug logs (authoritative), or the OTel trace DB as fallback. One active source at a time — never summed.",
            "install_cmd": doctor_cmd,
            "uninstall_cmd": doctor_cmd,
        },
    }


def _collect_cursor_hook_status_for_dashboard():
    """Hook status for the dashboard toggle panel under the Cursor runtime.

    Mirrors _collect_copilot_hook_status_for_dashboard(). cursor_doctor uses
    lowercase statuses ("ok"/"warn"/"fail") and check names from
    cursor_doctor.py; the toggles surface install, payload, and data readiness.
    """
    import cursor_doctor  # noqa: PLC0415

    mp_cmd = _shell_script_path()
    checks = cursor_doctor.run_checks()
    by_name = {check["name"]: check for check in checks}

    def _ok(name):
        return by_name.get(name, {}).get("status") == "ok"

    install_cmd = f"{_runtime_cli('cursor')} cursor-install"
    doctor_cmd = f"{_runtime_cli('cursor')} cursor-doctor"

    return {
        "cursor_hooks": {
            "installed": _ok("TO hook config"),
            "partial": by_name.get("TO hook config", {}).get("status") == "warn",
            "label": "Cursor Hooks",
            "description": "Merges Token Optimizer into ~/.cursor/hooks.json: sessionStart continuity restore, preToolUse Shell bash compression, postToolUse tally + nudges, preCompact capture, stop-time rollup and session-end dashboard refresh.",
            "install_cmd": install_cmd,
            "uninstall_cmd": f"{_runtime_cli('cursor')} cursor-uninstall",
        },
        "cursor_payload": {
            "installed": _ok("hook payload"),
            "partial": any(
                by_name.get(n, {}).get("status") == "warn"
                for n in ("measure-path locator", "persisted python")
            ),
            "label": "Cursor Hook Payload",
            "description": "The installed hook bridge plus its measure-path locator and a trusted absolute Python interpreter (never a bare python3).",
            "install_cmd": install_cmd,
            "uninstall_cmd": doctor_cmd,
        },
        "cursor_data": {
            "installed": _ok("IDE token plane") or _ok("CLI transcript plane"),
            "partial": any(
                by_name.get(n, {}).get("status") == "warn"
                for n in ("IDE token plane", "CLI transcript plane")
            ),
            "label": "Cursor Session Data",
            "description": "Best-effort token counts from state.vscdb (IDE) with a chars-over-four transcript estimate as fallback; the hook tally is always authoritative for calls/turns/compactions.",
            "install_cmd": doctor_cmd,
            "uninstall_cmd": doctor_cmd,
        },
    }


def _collect_grok_hook_status_for_dashboard():
    """Hook status for the dashboard toggle panel under the Grok Build runtime.

    Mirrors the Codex/Hermes/Antigravity collectors. grok_doctor reports
    lowercase statuses ("ok"/"warn"/"fail") and check names matching
    grok_doctor.py's emitted names exactly.
    """
    import grok_doctor  # noqa: PLC0415

    mp_cmd = _shell_script_path()
    checks = grok_doctor.run_checks()
    by_name = {check["name"]: check for check in checks}

    def _ok(name):
        return by_name.get(name, {}).get("status") == "ok"

    install_cmd = f"{_runtime_cli('grok')} grok-install"
    doctor_cmd = f"{_runtime_cli('grok')} grok-doctor"

    return {
        "grok_hooks": {
            "installed": _ok("TO hook config"),
            "partial": by_name.get("TO hook config", {}).get("status") == "warn",
            "label": "Grok Build Hooks",
            "description": "Wires Token Optimizer into $GROK_HOME/hooks/token-optimizer.json: sessionStart continuity restore, userPromptSubmit quality tracking, preToolUse bash compression (capability-gated), postToolUse crash-recovery tally + nudges, stop-time rollup.",
            "install_cmd": install_cmd,
            "uninstall_cmd": f"{_runtime_cli('grok')} grok-uninstall",
        },
        "grok_session_store": {
            "installed": _ok("session store"),
            "partial": by_name.get("session store", {}).get("status") == "warn",
            "label": "Grok Build Sessions",
            "description": "Reads $GROK_HOME/sessions/ for per-session token totals (updates.jsonl), signals, compactions, and cost (costUsdTicks). Crash-killed sessions recovered from partial data.",
            "install_cmd": doctor_cmd,
            "uninstall_cmd": doctor_cmd,
        },
        "grok_dashboard_port": {
            # The doctor's check name is "dashboard daemon" (lowercase, with
            # the port in the detail text, not the name) — the lookup must
            # match it exactly or the toggle can never turn green.
            "installed": _ok("dashboard daemon"),
            "label": "Dashboard Port 24848",
            "description": "Confirms that port 24848 is available or already serving the Grok Build Token Optimizer dashboard.",
            "install_cmd": f"{_runtime_cli('grok')} open-dashboard",
            "uninstall_cmd": "",
        },
    }


def _collect_hermes_hook_status_for_dashboard():
    """Collect Hermes plugin hook status for dashboard toggle panel.

    Mirrors _collect_codex_hook_status_for_dashboard(). Uses hermes_doctor
    to determine whether the plugin payload is installed and wired correctly,
    and exposes the same install/uninstall command surface.
    """
    import hermes_doctor  # noqa: PLC0415

    mp_cmd = _shell_script_path()
    checks = hermes_doctor.run_checks()
    by_name = {check["name"]: check for check in checks}

    def _ok(name):
        return by_name.get(name, {}).get("status") == "OK"

    install_cmd = f"{_runtime_cli('hermes')} hermes-install"
    doctor_cmd = f"{_runtime_cli('hermes')} hermes-doctor"

    return {
        "hermes_plugin": {
            "installed": _ok("Plugin directory") and _ok("Plugin files"),
            "label": "Hermes Plugin",
            "description": "Installs the Token Optimizer plugin into ~/.hermes/plugins/token-optimizer/. Provides pre_llm_call nudges, post_api_request capture, and session rollup hooks.",
            "install_cmd": install_cmd,
            "uninstall_cmd": install_cmd + " --uninstall",
        },
        "hermes_hooks": {
            "installed": _ok("Declared hooks"),
            "partial": by_name.get("Declared hooks", {}).get("status") == "WARN",
            "label": "Hermes Hook Declarations",
            "description": "Verifies that plugin.yaml declares all required hooks: pre_llm_call, post_api_request, on_session_finalize, on_session_end.",
            "install_cmd": install_cmd,
            "uninstall_cmd": install_cmd + " --uninstall",
        },
        "hermes_state_db": {
            "installed": _ok("state.db readable"),
            "partial": by_name.get("state.db", {}).get("status") == "WARN",
            "label": "Hermes State DB",
            "description": "Checks that ~/.hermes/state.db is present and readable. Created automatically on first Hermes session.",
            "install_cmd": doctor_cmd,
            "uninstall_cmd": "",
        },
        "hermes_dashboard_port": {
            "installed": _ok(f"Dashboard port {hermes_doctor.DASHBOARD_PORT}"),
            "label": "Dashboard Port 24844",
            "description": "Confirms that port 24844 is available or already serving the Hermes Token Optimizer dashboard.",
            "install_cmd": f"{_runtime_cli('hermes')} open-dashboard",
            "uninstall_cmd": "",
        },
    }


def _collect_antigravity_hook_status_for_dashboard():
    """Antigravity plugin hook status for the dashboard toggle panel.

    Mirrors _collect_hermes_hook_status_for_dashboard(). Uses antigravity_doctor
    to determine whether the plugin payload is installed and wired, and exposes
    the same install/uninstall command surface. antigravity_doctor reports
    lowercase statuses ("ok"/"warn"/"fail").
    """
    import antigravity_doctor  # noqa: PLC0415

    mp_cmd = _shell_script_path()
    checks = antigravity_doctor.run_checks()
    by_name = {check["name"]: check for check in checks}

    def _ok(name):
        return by_name.get(name, {}).get("status") == "ok"

    install_cmd = f"{_runtime_cli('antigravity')} antigravity-install"
    doctor_cmd = f"{_runtime_cli('antigravity')} antigravity-doctor"

    return {
        "antigravity_plugin": {
            "installed": _ok("plugin directory") and _ok("plugin payload"),
            "label": "Antigravity Plugin",
            "description": "Installs the Token Optimizer plugin into ~/.gemini/config/plugins/token-optimizer/. Provides continuity restore, context nudges, bash compression, and stop rollup.",
            "install_cmd": install_cmd,
            "uninstall_cmd": f"{_runtime_cli('antigravity')} antigravity-uninstall",
        },
        "antigravity_hooks": {
            "installed": _ok("plugin hooks"),
            "partial": by_name.get("plugin hooks", {}).get("status") == "warn",
            "label": "Antigravity Hook Declarations",
            "description": "Verifies hooks.json declares PreInvocation, PreToolUse (run_command matcher), and Stop.",
            "install_cmd": install_cmd,
            "uninstall_cmd": f"{_runtime_cli('antigravity')} antigravity-uninstall",
        },
        "antigravity_consent": {
            "installed": _ok("consent record"),
            "partial": by_name.get("consent record", {}).get("status") == "warn",
            "label": "Data Consent",
            "description": "Checks that the install-time consent record exists, which gates data collection.",
            "install_cmd": install_cmd,
            "uninstall_cmd": doctor_cmd,
        },
        "antigravity_dashboard_port": {
            # The doctor's check name is "dashboard daemon" (lowercase, with
            # the port in the detail text, not the name) — the lookup must
            # match it exactly or the toggle can never turn green.
            "installed": _ok("dashboard daemon"),
            "label": "Dashboard Port 24847",
            "description": "Confirms that port 24847 is available or already serving the Antigravity Token Optimizer dashboard.",
            "install_cmd": f"{_runtime_cli('antigravity')} open-dashboard",
            "uninstall_cmd": "",
        },
    }


def _parse_skill_frontmatter(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    meta = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key in {"name", "description"}:
                    meta[key] = value
    if "name" not in meta:
        meta["name"] = path.parent.name
    # Codex discovers skills from metadata; the SKILL.md body is loaded when
    # the skill is selected. Count the discovery surface here, not the full
    # on-demand skill body, otherwise startup/context recommendations are wildly
    # inflated for large skills.
    discovery_text = f"name: {meta.get('name', '')}\ndescription: {meta.get('description', '')}\n"
    meta["tokens"] = _estimate_tokens(discovery_text)
    meta["body_tokens"] = _estimate_tokens(text)
    return meta


def _collect_codex_skill_inventory(cfg: dict, *, project: Path) -> dict[str, list[dict]]:
    disabled_paths = set()
    skills_cfg = cfg.get("skills")
    if isinstance(skills_cfg, dict):
        entries = skills_cfg.get("config")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("enabled", True):
                    continue
                raw_path = entry.get("path")
                if isinstance(raw_path, str):
                    resolved = Path(raw_path).expanduser().resolve(strict=False)
                    disabled_paths.add(str(resolved))
                    if resolved.name != "SKILL.md":
                        disabled_paths.add(str((resolved / "SKILL.md").resolve(strict=False)))

    candidates: list[tuple[Path, str]] = []
    for root, source in (
        (project / ".codex" / "skills", "project"),
        (RUNTIME_DIR / "skills", "user"),
    ):
        if root.exists():
            candidates.extend((p, source) for p in root.rglob("SKILL.md"))

    plugin_cache = RUNTIME_DIR / "plugins" / "cache"
    if plugin_cache.exists():
        candidates.extend((p, "plugin") for p in plugin_cache.rglob("skills/*/SKILL.md"))

    # Plugins disabled in config.toml ([plugins."name@marketplace"] enabled=false)
    # do not expose their cached skills to the model — they belong in "disabled",
    # not "active", or the inventory reports an advertised surface larger than
    # what Codex actually loads.
    disabled_plugin_keys: set[str] = set()
    plugins_cfg = cfg.get("plugins")
    if isinstance(plugins_cfg, dict):
        for _pkey, _pcfg in plugins_cfg.items():
            if isinstance(_pcfg, dict) and not _pcfg.get("enabled", True):
                disabled_plugin_keys.add(str(_pkey))

    def _plugin_key_for(path_str: str) -> str | None:
        # Cache layout: <cache>/<marketplace>/<plugin>/<version>/skills/<name>/SKILL.md
        try:
            rel = Path(path_str).relative_to(plugin_cache.resolve(strict=False))
        except ValueError:
            return None
        parts = rel.parts
        if len(parts) >= 5 and parts[-3] == "skills":
            return f"{parts[1]}@{parts[0]}"
        return None

    # The cache keeps old versions after an upgrade, but Codex loads one
    # version per plugin. Counting every cached copy inflated the plugin
    # skill total several times over (issue #276).
    def _version_key(name: str):
        nums = re.findall(r"\d+", name)
        return tuple(int(n) for n in nums) if nums else (-1,)

    newest_version: dict[tuple[str, str], str] = {}
    for path, source in candidates:
        if source != "plugin":
            continue
        try:
            rel = path.relative_to(plugin_cache)
        except ValueError:
            continue
        if len(rel.parts) >= 6 and rel.parts[-3] == "skills":
            key = (rel.parts[0], rel.parts[1])
            current = newest_version.get(key)
            if current is None or _version_key(rel.parts[2]) > _version_key(current):
                newest_version[key] = rel.parts[2]

    active = []
    disabled = []
    seen: set[str] = set()
    mp_cmd = _shell_script_path()
    for path, source in candidates:
        if source == "plugin":
            try:
                rel = path.relative_to(plugin_cache)
            except ValueError:
                rel = None
            if (rel is not None and len(rel.parts) >= 6 and rel.parts[-3] == "skills"
                    and newest_version.get((rel.parts[0], rel.parts[1])) != rel.parts[2]):
                continue
        resolved = str(path.expanduser().resolve(strict=False))
        if resolved in seen:
            continue
        seen.add(resolved)
        meta = _parse_skill_frontmatter(path)
        item = {
            "name": meta.get("name") or path.parent.name,
            "skill_name": meta.get("name") or path.parent.name,
            "description": meta.get("description", ""),
            "tokens": meta.get("tokens", 0),
            "source": source,
            "path": _display_path(resolved),
            "disable_cmd": f"{_runtime_cli('codex')} codex-skill disable --path {shlex.quote(resolved)}",
            "enable_cmd": f"{_runtime_cli('codex')} codex-skill enable --path {shlex.quote(resolved)}",
        }
        _pkey = _plugin_key_for(resolved)
        if _pkey is not None:
            item["plugin"] = _pkey
        if resolved in disabled_paths or (_pkey is not None and _pkey in disabled_plugin_keys):
            disabled.append(item)
        else:
            active.append(item)
    active.sort(key=lambda item: (item["source"], item["name"]))
    disabled.sort(key=lambda item: (item["source"], item["name"]))
    return {"active": active, "disabled": disabled}


def _collect_codex_mcp_inventory(cfg: dict) -> list[dict]:
    servers = cfg.get("mcp_servers")
    if not isinstance(servers, dict):
        return []
    items = []
    mp_cmd = _shell_script_path()
    for name, server in servers.items():
        if not isinstance(server, dict):
            server = {}
        enabled = bool(server.get("enabled", True))
        transport = "http" if server.get("url") else "stdio"
        command = server.get("url") or server.get("command") or ""
        items.append({
            "name": name,
            "command": str(command),
            "transport": transport,
            "tokens": TOKENS_PER_DEFERRED_TOOL,
            "enabled": enabled,
            "disable_cmd": f"{_runtime_cli('codex')} codex-mcp disable {shlex.quote(str(name))}",
            "enable_cmd": f"{_runtime_cli('codex')} codex-mcp enable {shlex.quote(str(name))}",
        })
    return sorted(items, key=lambda item: item["name"])


def _collect_codex_plugin_inventory(cfg: dict) -> list[dict]:
    plugins = cfg.get("plugins")
    if not isinstance(plugins, dict):
        return []
    items = []
    for name, plugin_cfg in plugins.items():
        enabled = True
        if isinstance(plugin_cfg, dict):
            enabled = bool(plugin_cfg.get("enabled", True))
        items.append({"name": name, "enabled": enabled})
    return sorted(items, key=lambda item: item["name"])


def _codex_config_path() -> Path:
    return runtime_home() / "config.toml"


def _safe_codex_config_path() -> Path:
    home = runtime_home().resolve(strict=False)
    path = _codex_config_path()
    if path.exists() and path.is_symlink():
        raise ValueError(f"{path} must not be a symlink")
    parent = path.parent
    if parent.exists():
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError(f"{parent} must be a real directory")
        if not parent.resolve(strict=True).is_relative_to(home):
            raise ValueError(f"{parent} escapes Codex home")
    else:
        parent.mkdir(mode=0o700)
    return path


_CODEX_CONFIG_LOCK_PATH = runtime_home() / ".codex-config.lock"


@contextmanager
def _codex_config_lock():
    """Bounded portable lease for config.toml writes.

    Serializes concurrent read-modify-write cycles on config.toml (skill
    enable/disable, MCP toggles). Contenders skip the mutation after 75ms
    rather than blocking a hook.
    """
    lease_path = _CODEX_CONFIG_LOCK_PATH.with_suffix(".lease")
    with lease_lock(
        lease_path,
        deadline=current_deadline(),
        acquire_timeout=0.075,
    ) as acquired:
        yield acquired


@contextmanager
def _skill_mgmt_lock():
    """Bounded portable lease serializing skill archive/restore mutations.

    The dashboard daemon and a CLI `measure.py skill archive|restore` run in
    separate processes with no in-process serialization, so this cross-process
    lease closes the archive/restore TOCTOU windows.
    Contenders skip the mutation after 75ms rather than blocking a hook.
    """
    lock_path = CLAUDE_DIR / "_backups" / ".skill-mgmt.lock"
    lease_path = lock_path.with_suffix(".lease")
    with lease_lock(
        lease_path,
        deadline=current_deadline(),
        acquire_timeout=0.075,
    ) as acquired:
        yield acquired


def _write_codex_config(text: str) -> None:
    path = _safe_codex_config_path()
    with _codex_config_lock() as acquired:
        if not acquired:
            return
        codex_io.atomic_write(path, text)


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _set_toml_key(block: str, key: str, value: str) -> str:
    pattern = re.compile(rf"(?m)^([ \t]*){re.escape(key)}([ \t]*=[^\n]*)$")
    if pattern.search(block):
        return pattern.sub(lambda match: f"{match.group(1)}{key} = {value}", block, count=1)
    suffix = "" if block.endswith("\n") else "\n"
    return block + suffix + f"{key} = {value}\n"


def _iter_toml_array_table_spans(text: str, header: str):
    pattern = re.compile(rf"(?m)^[ \t]*\[\[{re.escape(header)}\]\][ \t]*(?:#.*)?$")
    next_header = re.compile(r"(?m)^[ \t]*\[\[?[^\]\n]+\]?\][ \t]*(?:#.*)?$")
    for match in pattern.finditer(text):
        next_match = next_header.search(text, match.end())
        yield match.start(), next_match.start() if next_match else len(text)


def _decode_toml_string(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.strip('"')
    return raw.strip("'\"")


def _codex_skill_target(raw_path: str | None = None, name: str | None = None) -> Path | None:
    project = Path.cwd().resolve(strict=False)
    cfg = _read_codex_config()
    inventory = _collect_codex_skill_inventory(cfg, project=project)
    all_items = inventory["active"] + inventory["disabled"]
    if raw_path:
        target = Path(raw_path).expanduser().resolve(strict=False)
        if target.name != "SKILL.md":
            target = target / "SKILL.md"
        for item in all_items:
            if Path(item["path"]).expanduser().resolve(strict=False) == target:
                return target
        return target if target.exists() else None
    if name:
        matches = [item for item in all_items if item["name"] == name or item.get("skill_name") == name]
        if len(matches) == 1:
            return Path(matches[0]["path"]).expanduser().resolve(strict=False)
    return None


def _manage_codex_skill(action: str, *, raw_path: str | None = None, name: str | None = None) -> bool:
    target = _codex_skill_target(raw_path=raw_path, name=name)
    if not target:
        return False
    try:
        text = _safe_codex_config_path().read_text(encoding="utf-8")
    except OSError:
        text = ""

    enabled = action == "enable"
    target_str = str(target)
    replacement = None
    for start, end in _iter_toml_array_table_spans(text, "skills.config"):
        block = text[start:end]
        path_match = re.search(r"(?m)^[ \t]*path[ \t]*=[ \t]*(.+?)[ \t]*(?:#.*)?$", block)
        if not path_match:
            continue
        configured = Path(_decode_toml_string(path_match.group(1))).expanduser().resolve(strict=False)
        if configured.name != "SKILL.md":
            configured = configured / "SKILL.md"
        if configured == target:
            replacement = text[:start] + _set_toml_key(block, "enabled", "true" if enabled else "false") + text[end:]
            break

    if replacement is None:
        suffix = "" if not text or text.endswith("\n") else "\n"
        replacement = (
            text
            + suffix
            + "\n[[skills.config]]\n"
            + f"path = {_toml_string(target_str)}\n"
            + f"enabled = {'true' if enabled else 'false'}\n"
        )
    _write_codex_config(replacement)
    return True


def _toml_table_header_variants(prefix: str, name: str) -> list[str]:
    if re.fullmatch(r"[A-Za-z0-9_-]+", name):
        return [f"[{prefix}.{name}]"]
    return [f"[{prefix}.{_toml_string(name)}]"]


def _set_codex_named_table_enabled(prefix: str, name: str, enabled: bool) -> bool:
    path = _safe_codex_config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    next_header = re.compile(r"(?m)^[ \t]*\[[^\]\n]+\][ \t]*(?:#.*)?$")
    for header in _toml_table_header_variants(prefix, name):
        pattern = re.compile(rf"(?m)^[ \t]*{re.escape(header)}[ \t]*(?:#.*)?$")
        match = pattern.search(text)
        if not match:
            continue
        next_match = next_header.search(text, match.end())
        end = next_match.start() if next_match else len(text)
        block = text[match.start():end]
        updated = _set_toml_key(block, "enabled", "true" if enabled else "false")
        _write_codex_config(text[:match.start()] + updated + text[end:])
        return True
    return False


def _manage_codex_mcp(action: str, name: str) -> bool:
    return _set_codex_named_table_enabled("mcp_servers", name, action == "enable")


def _collect_management_data(components=None, trends=None):
    """Collect data for the Manage tab: active/archived skills, MCP servers."""
    if components is None:
        components = measure_components()

    mp_cmd = _shell_script_path()
    if detect_runtime() == "codex":
        project = Path.cwd().resolve(strict=False)
        project_arg = shlex.quote(str(project))
        base = f"{_runtime_cli('codex')} codex-install --project {project_arg}"
        cfg = _read_codex_config()
        codex_skills = _collect_codex_skill_inventory(cfg, project=project)
        codex_mcp = _collect_codex_mcp_inventory(cfg)
        codex_mcp_active = [item for item in codex_mcp if item.get("enabled", True)]
        codex_mcp_disabled = [item for item in codex_mcp if not item.get("enabled", True)]
        codex_plugins = _collect_codex_plugin_inventory(cfg)
        return {
            "mode": "codex",
            "codex": {
                "project": _display_path(project),
                "install_cmd": base,
                "install_quiet_profile_cmd": base + " --profile quiet",
                "install_balanced_profile_cmd": base + " --profile balanced",
                "install_telemetry_profile_cmd": base + " --profile telemetry",
                "install_aggressive_profile_cmd": base + " --profile aggressive",
                "install_with_bash_compression_cmd": base + " --enable-bash-compression",
                "install_with_hot_path_hooks_cmd": base + " --enable-hot-path-hooks --enable-prompt-hooks",
                "install_with_status_line_cmd": base + " --enable-status-line",
                "refresh_cmd": f"{_runtime_cli('codex')} session-end-flush --trigger manual --no-defer",
                "doctor_cmd": f"{_runtime_cli('codex')} codex-doctor --project {project_arg}",
                "dashboard_cmd": f"{_runtime_cli('codex')} dashboard",
            },
            "skills": {"active": codex_skills["active"], "archived": [], "disabled": codex_skills["disabled"]},
            "mcp_servers": {"active": codex_mcp_active, "disabled": codex_mcp_disabled, "cloud": []},
            "plugins": codex_plugins,
            "v5_features": _get_v5_feature_status(),
        }

    # Hermes: list its own skills (~/.hermes/skills) and MCP servers
    # (config.yaml mcp_servers) read-only, without scanning CLAUDE_DIR,
    # settings.json or claude_desktop_config.json.
    if detect_runtime() == "hermes":
        hermes_cfg = _hermes_config(runtime_home())
        hermes_skills = _hermes_skill_inventory(runtime_home(), hermes_cfg)
        hermes_mcp = _hermes_mcp_inventory(hermes_cfg)
        return {
            "mode": "hermes",
            "read_only": True,
            "skills": hermes_skills,
            "mcp_servers": {"active": [i for i in hermes_mcp if i["enabled"]],
                            "disabled": [i for i in hermes_mcp if not i["enabled"]], "cloud": []},
            "plugins": [],
            "v5_features": _get_v5_feature_status(),
        }

    # Cursor: same shape as Hermes — Cursor has no Claude-style skills/MCP/
    # settings.json management surface, and falling through would silently
    # collect CLAUDE_DIR/_backups and Claude skills/MCP under a Cursor pin.
    if detect_runtime() == "cursor":
        return {
            "mode": "cursor",
            "skills": {"active": [], "archived": []},
            "mcp_servers": {"active": [], "disabled": [], "cloud": []},
            "plugins": [],
            "v5_features": _get_v5_feature_status(),
        }

    backups_dir = CLAUDE_DIR / "_backups"

    # Active skills
    active_skills = []
    skills_detail = components.get("skills_detail", {})
    for name in sorted(components.get("skills", {}).get("names", [])):
        sd = skills_detail.get(name, {})
        active_skills.append({
            "name": name,
            "skill_name": sd.get("skill_name", name),
            "tokens": sd.get("frontmatter_tokens", 100),
            "description": sd.get("description", ""),
            "archive_cmd": f"{hint_python()} {mp_cmd} skill archive {shlex.quote(name)}",
        })

    # Archived skills (scan backup dirs)
    archived_skills = []
    if backups_dir.exists():
        for archive_dir in sorted(backups_dir.iterdir(), reverse=True):
            if not archive_dir.is_dir() or not archive_dir.name.startswith(SKILL_ARCHIVE_DIR_PREFIXES):
                continue
            # "skills-archived-DATE" / "skills-deduped-DATE" -> "DATE"
            date_part = archive_dir.name.split("-", 2)[2] if archive_dir.name.count("-") >= 2 else ""
            try:
                entries = sorted(archive_dir.iterdir())
            except OSError:
                continue
            for item in entries:
                # One unreadable entry must not blank the whole Manage tab.
                try:
                    if not item.is_dir():
                        continue
                    # A real archived skill carries SKILL.md; a symlinked skill is
                    # archived as a dir holding only the marker. SKILL.md
                    # takes precedence so a real skill is never mislabeled as a link.
                    # Surface both so symlinked skills stay restorable from the UI.
                    has_skill_md = (item / "SKILL.md").exists()
                    is_symlink_record = not has_skill_md and (item / SYMLINK_TARGET_MARKER).exists()
                    if not has_skill_md and not is_symlink_record:
                        continue
                    desc = "Symlinked skill" if is_symlink_record else ""
                    if not is_symlink_record:
                        content = (item / "SKILL.md").read_text(encoding="utf-8")[:2000]
                        if content.startswith("---"):
                            end = content.find("---", 3)
                            if end > 0:
                                for line in content[3:end].split("\n"):
                                    if line.strip().startswith("description:"):
                                        desc = line.strip()[12:].strip()[:100]
                                        break
                except OSError:
                    continue
                archived_skills.append({
                    "name": item.name,
                    "archived_date": date_part,
                    "archive_dir": archive_dir.name,
                    "description": desc,
                    "symlink": is_symlink_record,
                    "restore_cmd": f"{hint_python()} {mp_cmd} skill restore {shlex.quote(item.name)}",
                })

    # MCP servers (local settings.json)
    settings, _ = _read_settings_json()
    mcp_servers_config = settings.get("mcpServers", {})
    disabled_config = settings.get("_disabledMcpServers", {})

    active_mcps = []
    for name in sorted(mcp_servers_config.keys()):
        cfg = mcp_servers_config[name]
        tool_count = len(cfg.get("tools", []))
        active_mcps.append({
            "name": name,
            "source": "local",
            "tool_count": tool_count,
            "command": cfg.get("command", ""),
            "disable_cmd": f"{hint_python()} {mp_cmd} mcp disable {shlex.quote(name)}",
        })

    disabled_mcps = []
    for name in sorted(disabled_config.keys()):
        disabled_mcps.append({
            "name": name,
            "source": "local",
            "enable_cmd": f"{hint_python()} {mp_cmd} mcp enable {shlex.quote(name)}",
        })

    # Cloud-synced MCP servers (Claude Desktop config)
    cloud_mcps = []
    desktop_config = HOME / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if desktop_config.exists():
        try:
            dc = json.loads(desktop_config.read_text(encoding="utf-8"))
            for name in sorted(dc.get("mcpServers", {}).keys()):
                if name not in mcp_servers_config and name not in disabled_config:
                    cfg = dc["mcpServers"][name]
                    cloud_mcps.append({
                        "name": name,
                        "source": "cloud",
                        "command": cfg.get("command", ""),
                    })
        except (json.JSONDecodeError, OSError):
            pass

    return {
        "skills": {
            "active": active_skills,
            "archived": archived_skills,
        },
        "mcp_servers": {
            "active": active_mcps,
            "disabled": disabled_mcps,
            "cloud": cloud_mcps,
        },
        "v5_features": _get_v5_feature_status(),
    }


def plugin_cleanup(dry_run=False, quiet=False):
    """Report stale plugin cache dirs and archive local/plugin skill overlaps.

    Two actions:
    1. Stale cache REPORTING: lists old plugin version dirs in ~/.claude/plugins/cache/
       not referenced by installPath. Does NOT delete them because installPath is not
       always the authoritative source (Claude Code's loader may resolve via marketplace
       source, especially for directory-sourced plugins). Users should review manually.
    2. Local/plugin overlap: archives local skills in ~/.claude/skills/ that duplicate
       plugin-installed skills (only bare SKILL.md; keeps skills with custom reference files).
    """
    import shutil

    actions_taken = []

    # Defense-in-depth: under a foreign runtime (OpenCode, Copilot),
    # do not touch ~/.claude.
    if _is_foreign_runtime():
        if not quiet:
            print(f"  Skipped: {runtime_name_for_humans()} runtime detected; not modifying ~/.claude.")
        return actions_taken

    # --- Report 1: Stale cache version dirs (report only, never delete) ---
    registry = CLAUDE_DIR / "plugins" / "installed_plugins.json"
    cache_dir = CLAUDE_DIR / "plugins" / "cache"

    active_paths = set()
    if registry.exists():
        try:
            with open(registry, "r", encoding="utf-8") as f:
                data = json.load(f)
            for plugin_key, installs in data.get("plugins", {}).items():
                if not isinstance(installs, list):
                    continue
                for inst in installs:
                    raw = inst.get("installPath", "")
                    if raw:
                        active_paths.add(str(Path(raw).resolve()))
        except (json.JSONDecodeError, OSError):
            pass

    stale_dirs = []
    if cache_dir.exists():
        for marketplace in sorted(cache_dir.iterdir()):
            if not marketplace.is_dir():
                continue
            for plugin in sorted(marketplace.iterdir()):
                if not plugin.is_dir():
                    continue
                for version_dir in sorted(plugin.iterdir()):
                    if not version_dir.is_dir():
                        continue
                    resolved = str(version_dir.resolve())
                    if resolved not in active_paths:
                        has_skills = (version_dir / "skills").is_dir()
                        stale_dirs.append({
                            "path": version_dir,
                            "display": f"{marketplace.name}/{plugin.name}/{version_dir.name}",
                            "has_skills": has_skills,
                        })

    if stale_dirs:
        skills_stale = [d for d in stale_dirs if d["has_skills"]]
        if not quiet:
            print(f"\n  Stale plugin cache: {len(stale_dirs)} unreferenced dirs ({len(skills_stale)} with skills)")
            print("    These are NOT auto-deleted (Claude Code's loader may still use them).")
            print("    Review manually: ls ~/.claude/plugins/cache/")
    elif not quiet:
        print("\n  Stale plugin cache: clean")

    # --- Local skills that duplicate plugin skills ---
    # Scan plugin skills to get the set of skill directory names
    plugin_skill_names = set()
    if registry.exists():
        try:
            with open(registry, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Load enabledPlugins to only check active plugins
            enabled = None
            if _is_regular_file(SETTINGS_PATH):
                try:
                    settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                    enabled = settings.get("enabledPlugins")
                except (json.JSONDecodeError, OSError):
                    pass

            for plugin_key, installs in data.get("plugins", {}).items():
                if not isinstance(installs, list):
                    continue
                if enabled is not None and not enabled.get(plugin_key, False):
                    continue
                for inst in installs:
                    raw = inst.get("installPath", "")
                    if not raw:
                        continue
                    install_path = Path(raw)
                    if not install_path.exists():
                        continue
                    skills_path = install_path / "skills"
                    if skills_path.is_dir():
                        for item in skills_path.iterdir():
                            if item.is_dir() and (item / "SKILL.md").exists():
                                plugin_skill_names.add(item.name)
        except (json.JSONDecodeError, OSError):
            pass

    # Check ~/.claude/skills/ for overlaps
    # Only archive if local skill is a plain symlink OR has no extra files beyond SKILL.md.
    # Local skills with custom reference files (loaded on-demand) have content the plugin
    # version lacks, so archiving them would lose functionality.
    skills_dir = CLAUDE_DIR / "skills"
    overlaps = []
    if skills_dir.exists() and plugin_skill_names:
        for item in sorted(skills_dir.iterdir()):
            if not item.is_dir() or not (item / "SKILL.md").exists():
                continue
            if item.name in plugin_skill_names:
                # Safe to archive: symlinks (just a pointer) or bare skills (only SKILL.md)
                if item.is_symlink():
                    overlaps.append(item)
                else:
                    extra_files = [f.name for f in item.iterdir()
                                   if f.name != "SKILL.md" and not f.name.startswith(".")]
                    if not extra_files:
                        overlaps.append(item)
                    elif not quiet:
                        print(f"  [skip] {item.name}: local copy has extra files ({', '.join(extra_files[:3])}), keeping it")

    backups_dir = CLAUDE_DIR / "_backups"
    if overlaps:
        if not quiet:
            print(f"  Local/plugin overlaps: {len(overlaps)} skills loaded twice")
        today = time.strftime("%Y%m%d")
        archive_dir = backups_dir / f"skills-deduped-{today}"
        for item in overlaps:
            if dry_run:
                if not quiet:
                    print(f"    [dry-run] would archive: {item.name} (exists as plugin + local)")
            else:
                try:
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    dest = archive_dir / item.name
                    if dest.exists():
                        if not quiet:
                            print(f"    [skip] {item.name}: already archived today")
                        continue
                    # Move (handles both dirs and symlinks)
                    if item.is_symlink():
                        # For symlinks: record target, then remove the symlink
                        target = os.readlink(item)
                        dest.mkdir(parents=True, exist_ok=True)
                        (dest / SYMLINK_TARGET_MARKER).write_text(target, encoding="utf-8")
                        item.unlink()
                    else:
                        shutil.move(str(item), str(dest))
                    if not quiet:
                        print(f"    archived: {item.name} -> {archive_dir.name}/")
                    actions_taken.append(f"archived duplicate local skill: {item.name}")
                except OSError as e:
                    if not quiet:
                        print(f"    [error] {item.name}: {e}")
    elif not quiet:
        print("  Local/plugin overlaps: none")

    if not quiet:
        if actions_taken:
            print(f"\n  {len(actions_taken)} fixes applied. Restart Claude Code to take effect.")
            print(f"  Restore archived skills from: {backups_dir}/skills-deduped-*/")
        elif not dry_run:
            print("\n  Everything clean. No duplicates found.")
        print()

    return actions_taken


def _manage_skill(action, name):
    """Archive or restore a skill.

    Skills in ~/.claude/skills are often symlinks pointing into a shared skills
    repo. Archiving a symlinked skill records the link target and removes only
    the link, never the real source.

    Held under a cross-process lock so a dashboard-daemon call and a CLI call
    can't interleave their archive/restore mutations.
    """
    with _skill_mgmt_lock() as acquired:
        if not acquired:
            return False
        return _manage_skill_locked(action, name)


def _manage_skill_locked(action, name):
    import shutil

    # Defense-in-depth: never mutate ~/.claude/skills when running
    # under a foreign runtime (OpenCode, Copilot), even if reached outside the
    # CLI dispatch guard (e.g. the dashboard /api/v5/toggle path).
    if _is_foreign_runtime():
        print(f"  [!] Refusing to modify ~/.claude skills under the {runtime_name_for_humans()} runtime.")
        return False

    # Validate name: prevent path traversal
    if not isinstance(name, str) or not name or "/" in name or "\\" in name or name in (".", "..") or "\0" in name:
        print(f"  [!] Invalid skill name: {name}")
        return False
    skills_dir = CLAUDE_DIR / "skills"
    # Containment check on the UN-resolved path. The name is already validated as
    # a single path component, so the join cannot escape skills_dir. Do NOT use
    # .resolve() here: legitimate skills are often symlinks pointing outside
    # ~/.claude/skills, and resolving the target would misflag them.
    candidate = skills_dir / name
    if candidate.parent != skills_dir:
        print(f"  [!] Path traversal detected: {name}")
        return False
    backups_dir = CLAUDE_DIR / "_backups"
    today = datetime.now().strftime("%Y%m%d")
    archive_dir = backups_dir / f"skills-archived-{today}"

    if action == "archive":
        src = skills_dir / name
        if not src.exists() and not src.is_symlink():
            print(f"  Skill '{name}' not found in {skills_dir}")
            return False
        dst = archive_dir / name
        if dst.exists() or dst.is_symlink():
            print(f"  Skill '{name}' already archived. Remove it first.")
            return False
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
            if src.is_symlink():
                # Record the link target, remove only the link (not the real source)
                target = os.readlink(src)
                dst.mkdir(parents=True, exist_ok=True)
                (dst / SYMLINK_TARGET_MARKER).write_text(target, encoding="utf-8")
                src.unlink()
            else:
                shutil.move(str(src), str(dst))
        except OSError as e:
            print(f"  [!] Failed to archive '{name}': {e}")
            return False
        print(f"  Archived: {name} -> {archive_dir.name}/")
        return True

    elif action == "restore":
        # Search all archive dirs for this skill
        if backups_dir.exists():
            for ad in sorted(backups_dir.iterdir(), reverse=True):
                if not ad.is_dir() or not ad.name.startswith(SKILL_ARCHIVE_DIR_PREFIXES):
                    continue
                src = ad / name
                if not src.exists() and not src.is_symlink():
                    continue
                dst = skills_dir / name
                if dst.exists() or dst.is_symlink():
                    print(f"  Skill '{name}' already exists in skills/. Remove it first.")
                    return False
                # A symlink record is a dir holding the marker and NO SKILL.md.
                # Requiring SKILL.md to be absent stops a real skill that happens
                # to ship the marker file from being turned into a link + deleted.
                marker = src / SYMLINK_TARGET_MARKER
                is_symlink_record = (
                    src.is_dir() and marker.exists() and not (src / "SKILL.md").exists()
                )
                try:
                    if is_symlink_record:
                        target = marker.read_text(encoding="utf-8").strip()
                        if not target:
                            print(f"  [!] Archived link target for '{name}' is empty; cannot restore.")
                            return False
                        dst.symlink_to(target)
                        # Link recreated; drop the archive record. If that fails,
                        # remove the just-created link so a retry isn't blocked.
                        try:
                            shutil.rmtree(src)
                        except OSError as e:
                            dst.unlink(missing_ok=True)
                            print(f"  [!] Failed to clear archive for '{name}': {e}")
                            return False
                    else:
                        shutil.move(str(src), str(dst))
                except OSError as e:
                    print(f"  [!] Failed to restore '{name}': {e}")
                    return False
                print(f"  Restored: {name} from {ad.name}/")
                # Clean up empty archive dir
                try:
                    if not list(ad.iterdir()):
                        ad.rmdir()
                except OSError:
                    pass
                return True
        print(f"  Skill '{name}' not found in any archive directory.")
        return False
    else:
        print(f"  Unknown action: {action}")
        return False


def _manage_mcp(action, name):
    """Disable or enable an MCP server by moving between mcpServers and _disabledMcpServers."""
    # Defense-in-depth: never mutate ~/.claude/settings.json under
    # a foreign runtime (OpenCode, Copilot), even when reached outside the CLI
    # dispatch guard (e.g. the dashboard /api/mcp/{enable,disable} HTTP path),
    # mirroring _manage_skill_locked.
    if _is_foreign_runtime():
        print(f"  [!] Refusing to modify ~/.claude settings under the {runtime_name_for_humans()} runtime.")
        return False

    # Writes `settings` back.
    settings, _ok = _read_settings_for_write()
    if not _ok or not settings:
        print("  settings.json not found, empty, or unreadable")
        return False

    active = settings.get("mcpServers", {})
    disabled = settings.get("_disabledMcpServers", {})

    if action == "disable":
        if name not in active:
            print(f"  MCP server '{name}' not found in active servers.")
            return False
        config = active.pop(name)
        disabled[name] = config
        settings["_disabledMcpServers"] = disabled
        settings["mcpServers"] = active
        if _write_settings_atomic(settings):
            print(f"  Disabled MCP server: {name}")
            return True
        print(f"  Could not disable MCP server '{name}'; settings.json was not changed.", file=sys.stderr)
        return False

    elif action == "enable":
        if name not in disabled:
            print(f"  MCP server '{name}' not found in disabled servers.")
            return False
        config = disabled.pop(name)
        active[name] = config
        settings["mcpServers"] = active
        if disabled:
            settings["_disabledMcpServers"] = disabled
        else:
            settings.pop("_disabledMcpServers", None)
        # Deliberate top-level removal (the last disabled server was re-enabled),
        # declared so the choke-point guard permits exactly this key.
        if _write_settings_atomic(settings, allow_removing_keys={"_disabledMcpServers"}):
            print(f"  Enabled MCP server: {name}")
            return True
        print(f"  Could not enable MCP server '{name}'; settings.json was not changed.", file=sys.stderr)
        return False
    else:
        print(f"  Unknown action: {action}")
        return False


def _codex_memory_review_data(mem):
    """Dashboard memory card data for the Codex machine-wide memory component.

    Codex memory lives in state_*.sqlite rows, not a MEMORY.md file, so there is
    no line count, entry index, topic-file listing or link audit. Only report
    what was measured: thread count, tokens, largest thread, and scan status.
    """
    files = mem.get("files") or []
    return {
        "codex": True,
        "target": mem.get("path", ""),
        "thread_count": int(mem.get("thread_count", 0) or 0),
        "total_tokens": int(mem.get("tokens", 0) or 0),
        "largest_thread_tokens": max((int(f.get("memory_tokens", 0) or 0) for f in files), default=0),
        "scan_partial": bool(mem.get("truncated")),
    }


def generate_standalone_dashboard(days=30, quiet=False, force=False):
    """Generate a persistent Trends + Health dashboard (no audit data needed).

    Outputs to DASHBOARD_PATH (~/.claude/_backups/token-optimizer/dashboard.html).
    Used by the SessionEnd hook for auto-refresh and for standalone viewing.
    """
    # Skip regeneration if dashboard was updated within the last 60 seconds
    # (prevents rapid /clear cycles from blocking on repeated full pipelines).
    # force=True bypasses the throttle -- used by ensure-health's
    # version-mismatch regen path so a freshly-updated plugin always
    # wins over a recently-written stale file.
    if quiet and not force and DASHBOARD_PATH.exists():
        try:
            age = time.time() - DASHBOARD_PATH.stat().st_mtime
            if age < 60:
                return str(DASHBOARD_PATH)
        except OSError:
            pass
    if _running_under_hook():
        _arm_dashboard_selfheal_on_timeout(days=days)

    # Version-downgrade guard: never let an OLDER build overwrite a dashboard a
    # NEWER build already wrote (the "fixed but still broken" regression -- a
    # stale daemon or pre-upgrade session clobbering a just-shipped fix). Checked
    # BEFORE the expensive data build so an older writer skips cheaply. force does
    # NOT bypass this: force governs the 60s throttle, not version precedence.
    # Newer/equal writers proceed (equal is not strictly newer). Fail-open.
    if _dashboard_on_disk_is_newer(DASHBOARD_PATH):
        if not quiet:
            print(f"  [Token Optimizer] Skipping dashboard write: the on-disk dashboard "
                  f"was written by a newer version than this build (v{TOKEN_OPTIMIZER_VERSION}).")
        return str(DASHBOARD_PATH)

    script_dir = Path(__file__).resolve().parent
    template_path = script_dir.parent / "assets" / "dashboard.html"
    if not template_path.exists():
        if not quiet:
            print(f"Error: dashboard template not found at: {template_path}")
        return None

    if not quiet:
        print("  Measuring current token overhead...")
    components = measure_components()
    totals = calculate_totals(components)
    baselines = get_session_baselines(5)

    calibration = detect_calibration_gap(components, totals, baselines)

    ctx_window, ctx_source = detect_context_window()
    snapshot = {
        "components": components,
        "totals": totals,
        "session_baselines": baselines,
        "calibration": calibration,
        "context_window": ctx_window,
        "context_source": ctx_source,
    }

    if not quiet:
        print("  Collecting usage trends...")
    try:
        trends = _collect_trends_data(days=days)
    except Exception:
        trends = None

    if not quiet:
        print("  Checking session health...")
    try:
        health = _collect_health_data()
    except Exception:
        health = None

    # Generate auto-recommendations from rules engine
    if not quiet:
        print("  Generating auto-recommendations...")
    auto_plan, rec_count = generate_auto_recommendations(components, trends=trends, days=days)
    if not quiet and rec_count > 0:
        print(f"  Found {rec_count} auto-recommendations")

    # Generate coach data for the Coach tab (reuse already-collected components/trends)
    if not quiet:
        print("  Generating coach data...")
    try:
        coach = generate_coach_data(components=components, trends=trends)
    except Exception:
        coach = None

    # Collect context quality data (v2.0)
    if not quiet:
        print("  Analyzing context quality...")
    quality = _collect_quality_for_dashboard()

    # Collect hook installation status for dashboard toggles
    hook_status = _collect_hook_status_for_dashboard()

    # Collect management data for Manage tab
    if not quiet:
        print("  Collecting management data...")
    management = _collect_management_data(components=components, trends=trends)

    # Collect per-turn data for the default visible 7-day table in local-file mode.
    # Served mode can fetch older rows on demand, but the static dashboard needs a
    # bounded preload so it stays responsive and doesn't balloon in size.
    if not quiet:
        print("  Collecting per-turn data for recent sessions...")
    session_turns = {}
    # Bound the preload: the visible table only shows the most recent sessions, but a
    # busy 7-day window can hold hundreds, and each parse_session_turns re-reads a full
    # transcript. Cap total preloaded sessions so a fresh (cold-cache) dashboard gen in
    # the deferred flush worker never turns this into a multi-second parse storm. Older
    # rows are fetched on demand in served mode.
    _turn_preload_budget = _int_env("TOKEN_OPTIMIZER_TURN_PRELOAD_MAX", 40)
    # Also bound per-file size: parse_session_turns reads a whole transcript, and a
    # single long session JSONL can reach 100MB+. Preloading 40 of those under the
    # 45s regen-step / 20s flush budget trips REGEN_STEP_TIMEOUT and can kill a
    # flush worker mid-dashboard-write. Skip oversized transcripts here; served mode
    # still fetches their rows on demand, exactly like older rows past the count cap.
    _turn_preload_maxbytes = _int_env("TOKEN_OPTIMIZER_TURN_PRELOAD_MAX_BYTES", 8_000_000)
    try:
        for day in (trends or {}).get("daily", [])[:7]:
            if len(session_turns) >= _turn_preload_budget:
                break
            for session in day.get("session_details", []):
                if len(session_turns) >= _turn_preload_budget:
                    break
                session_key = session.get("session_key")
                jsonl_path = session.get("jsonl_path")
                if not session_key or session_key in session_turns or not jsonl_path:
                    continue
                jsonl_resolved = str(Path(jsonl_path).expanduser()) if jsonl_path.startswith("~") else jsonl_path
                if not os.path.exists(jsonl_resolved):
                    continue
                try:
                    if os.path.getsize(jsonl_resolved) > _turn_preload_maxbytes:
                        continue
                except OSError:
                    continue
                turns = parse_session_turns(jsonl_resolved)
                if turns:
                    session_turns[session_key] = turns
    except Exception:
        pass

    pricing_tier = _load_pricing_tier()
    ttl_period_summary = []
    for period in (7, 30):
        try:
            ttl_period_summary.append(_build_ttl_period_summary(period))
        except Exception:
            ttl_period_summary.append({
                "label": f"{period}d: unavailable",
                "period_days": period,
                "mixed_sessions": 0,
                "five_only_sessions": 0,
                "one_hour_only_sessions": 0,
            })
    # Lightweight memory health for dashboard cards (reuses already-computed components,
    # no second measure_components() call, no full detector suite)
    mr_data = None
    try:
        mem = components.get("memory_md", {})
        if detect_runtime() == "codex" and mem.get("exists"):
            mr_data = _codex_memory_review_data(mem)
        elif mem.get("exists") and mem.get("path"):
            mem_path = Path(mem["path"])
            memory_dir = mem_path.parent
            parsed = _mr_parse_memory_index(str(mem_path))
            files_on_disk = _mr_scan_topic_files(str(memory_dir))
            linked_basenames = {Path(link["target"]).name for link in parsed["links_all"]}
            files_set = set(files_on_disk)
            orphan_count = len(files_set - linked_basenames)
            broken_count = len([link for link in parsed["links_all"] if Path(link["target"]).name not in files_set])
            total_lines = parsed["total_lines"]
            sev_medium = (1 if total_lines > _MR_MEMORY_LINE_LIMIT else 0) + (1 if broken_count > 0 else 0)
            mr_data = {
                "target": str(mem_path),
                "total_lines": total_lines,
                "entry_count": len(parsed["entries"]),
                "topic_files_count": len(files_on_disk),
                "linked_files_count": len(linked_basenames & files_set),
                "findings": [],  # lightweight mode skips full findings
                "severity_counts": {"high": 0, "medium": sev_medium, "low": orphan_count},
                "savings": {"total_tokens": max(0, total_lines - _MR_MEMORY_LINE_LIMIT) * 15, "by_category": {}},
                "truncated": total_lines > _MR_MEMORY_LINE_LIMIT,
                "truncated_lines": max(0, total_lines - _MR_MEMORY_LINE_LIMIT),
            }
    except Exception:
        pass

    # CLAUDE.md health summary for dashboard card (from already-computed components)
    claude_md_health = None
    try:
        if detect_runtime() not in ("claude", "codex"):
            # Only Claude Code and Codex have an instruction-file chain this
            # card describes; other runtimes would show a "CLAUDE.md" card.
            raise LookupError
        instruction_tokens = 0
        context_window = components.get("context_window") or detect_context_window()[0]
        for key in components:
            if (
                (detect_runtime() == "codex" and key.startswith("agents_md"))
                or (detect_runtime() != "codex" and key.startswith("claude_md"))
            ) and components[key].get("exists"):
                instruction_tokens += components[key].get("tokens", 0)
        instruction_pct = instruction_tokens / context_window * 100 if context_window else 0
        instruction_status = "good"
        if coach:
            for p in coach.get("patterns_bad", []):
                if "CLAUDE.md" in p.get("name", "") or "AGENTS.md" in p.get("name", ""):
                    instruction_status = "warning" if p.get("severity") == "medium" else "notice"
                    break
        claude_md_health = {
            "tokens": instruction_tokens,
            "pct": round(instruction_pct, 1),
            "status": instruction_status,
        }
    except Exception:
        pass

    # Merged savings (measured + realized + opportunity + estimated, tier-labelled).
    # Wrapped so a slow/failed eval never breaks the SessionEnd dashboard regen.
    savings_data = _dashboard_savings_data(days=30, include_billing_mode=True)
    if savings_data != {"total_tokens": 0, "total_cost_usd": 0.0, "by_category": {}}:
        try:
            savings_data["billing_mode"] = keepwarm_billing_mode()
        except Exception:
            pass

    # Cache-TTL watchdog (opportunity tier — observed waste, potential recovery).
    # Standalone analysis; never reaches the realized savings headline. Fail-open.
    # Mirrors generate_dashboard's cache_health wiring so the standalone/daemon
    # dashboard renders the same cache-health panel as the full audit dashboard.
    try:
        cache_health = _cache_ttl_waste_cached(days=30)
    except Exception:
        cache_health = {"available": False, "tier": "opportunity"}

    # U7: keep-warm cache-automation sub-block carried inside the cache-health
    # payload (realized vs spend vs NET, tripwire ratio, the 5 tile states,
    # predictor transparency). Fail-open so it never blocks dashboard regen.
    try:
        if isinstance(cache_health, dict):
            cache_health["keepwarm"] = keepwarm_cache_health_block(days=30)
    except Exception:
        pass

    # Subscription runway. This generator produces the dashboard the CLI writes,
    # so the key must be assembled here too: the template gates the card on
    # `data.runway` and silently renders nothing when it is absent, which is
    # indistinguishable from the card being switched off. None on API-billed
    # setups or when the meter has never been read (a stale-but-present reading is
    # kept and dated on the card), and the template omits the card.
    try:
        runway = runway_snapshot(days=30)
    except Exception:
        runway = None

    data = {
        "runway": runway,
        "snapshot": snapshot,
        "audit": {},
        "plan": auto_plan if auto_plan else None,
        "trends": trends,
        "health": health,
        "coach": coach,
        "quality": quality,
        "manage": management,
        "hooks": hook_status,
        "savings": savings_data,
        "cache_health": cache_health,
        "standalone": True,
        "auto_plan": True,
        "generated_at": datetime.now().isoformat(),
        "pricing_tier": pricing_tier,
        "pricing_tier_label": _pricing_tier_label(pricing_tier),
        "pricing_tiers": {} if detect_runtime() == "codex" else {k: v["label"] for k, v in PRICING_TIERS.items()},
        "ttl_period_summary": ttl_period_summary,
        "session_turns": session_turns,
        "memory_review": mr_data,
        "claude_md_health": claude_md_health,
        "v5_recommendation": _get_v5_savings_recommendation(),
        "version": TOKEN_OPTIMIZER_VERSION,
        "runtime": detect_runtime(),
        "runtime_label": runtime_name_for_humans(),
    }
    data = _sanitize_dashboard_paths(data)

    template = template_path.read_text(encoding="utf-8")
    data_json = json.dumps(data, ensure_ascii=True, default=str)
    data_json = data_json.replace("</", "<\\/")  # Prevent </script> injection
    placeholder = "window.__TOKEN_DATA__ = null;"
    injected = template.replace(placeholder, f"window.__TOKEN_DATA__ = {data_json};", 1)
    if injected == template:
        if not quiet:
            print("  [Warning] Data injection failed: placeholder not found in template.")
        return None

    # v5.4.10: dual-path write (same pattern as daemon script in v5.4.7).
    # DASHBOARD_PATH depends on SNAPSHOT_DIR which resolves differently in
    # plugin-hook context (runtime plugin data set) vs standalone CLI.
    # The daemon script's DASHBOARD constant points to whichever path was
    # active when setup-daemon ran. Write to BOTH so the daemon always
    # serves current content regardless of which path it expects.
    legacy_dashboard = RUNTIME_DIR / "_backups" / "token-optimizer" / "dashboard.html"
    write_paths = {DASHBOARD_PATH, legacy_dashboard}
    wrote_any = False
    for wp in write_paths:
        try:
            wp.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: three uncoordinated callers can target this same path
            # (background freshness regen, manual POST /api/regenerate, and the
            # SessionEnd flush worker's inline gen). A plain O_TRUNC write lets two
            # of them interleave into a torn/mixed file, and a GET between the
            # truncate and the final byte serves partial HTML. Write to a temp
            # sibling then os.replace so every reader sees one complete generation.
            tmp_fd, tmp_name = tempfile.mkstemp(dir=str(wp.parent), prefix=".dash-", suffix=".tmp")
            try:
                if hasattr(os, "fchmod"):  # POSIX only (os.fchmod is absent on Windows)
                    os.fchmod(tmp_fd, 0o600)
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    f.write(injected)
                os.replace(tmp_name, str(wp))
            except BaseException:
                # BaseException, not OSError: the SessionEnd flush worker runs this
                # under a 20s hook budget that raises _HookTimeout (a BaseException).
                # A bare `except OSError` would let a mid-write timeout skip the
                # unlink and leak a ~5MB .dash-*.tmp. Clean up on any interruption,
                # then re-raise so the timeout still propagates to the worker.
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            wrote_any = True
        except OSError:
            continue
    if not wrote_any:
        if not quiet:
            print("  [Error] Failed to write dashboard to any path")
        return None

    # Sidecar meta (version + shape) next to each written HTML, so ensure-health's
    # staleness check reads a few bytes instead of the whole 4-5MB file. Written
    # AFTER the HTML os.replace above and itself atomic: a torn or
    # (html_new, meta_old) sidecar can let an OLDER concurrent build's downgrade
    # guard clobber a NEWER dashboard, so both writes must be all-or-nothing.
    for wp in write_paths:
        try:
            if wp.exists():
                _write_dashboard_meta_atomic(_dashboard_meta_path(wp))
        except OSError:
            pass

    if not quiet:
        print(f"  Dashboard: {DASHBOARD_PATH}")
        print(f"  Local:  {DASHBOARD_PATH.as_uri()}")
        print(f"  Remote: {hint_python()} {_display_path(Path(__file__).resolve())} dashboard --serve")

    return str(DASHBOARD_PATH)


def _acquire_session_end_flush_lock(max_age_seconds=120):
    """Return a lock directory path, or None if another worker is active."""
    lock_dir = SNAPSHOT_DIR / ".session-end-flush.lock"
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        lock_dir.mkdir(mode=0o700)
        return lock_dir
    except FileExistsError:
        try:
            age = time.time() - lock_dir.stat().st_mtime
            if age > max_age_seconds:
                lock_dir.rmdir()
                try:
                    lock_dir.mkdir(mode=0o700)
                except FileExistsError:
                    return None
                return lock_dir
        except OSError:
            pass
        return None
    except OSError:
        return None


def _release_session_end_flush_lock(lock_dir):
    if not lock_dir:
        return
    try:
        lock_dir.rmdir()
    except OSError:
        pass


def _session_refresh_due(min_interval_seconds=120):
    """Throttle expensive Stop-hook dashboard rebuilds."""
    marker = SNAPSHOT_DIR / ".last-session-end-refresh"
    try:
        if marker.exists() and time.time() - marker.stat().st_mtime < min_interval_seconds:
            return False
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return True
    except OSError:
        # If the marker cannot be read/written, prefer correctness over
        # staleness; the wall-clock budget still caps the worker.
        return True


HOOK_STATUS_FILE = RUNTIME_DIR / "token-optimizer" / "hook-status.json"


def _pid_is_running(pid):
    """True when process ``pid`` exists, on every OS. Never raises.

    ``os.kill(pid, 0)`` is a liveness probe only on POSIX. On Windows signal 0
    is CTRL_C_EVENT: Python calls GenerateConsoleCtrlEvent, which fails for
    any pid when the caller has no console (hooks run detached, so a live lock
    owner read as dead) and, with a console, interrupts the whole console
    group. Windows asks the process table instead. When the answer is unknown
    the process counts as running, so a lock is never stolen from a live owner.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        return _win_pid_is_running(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def _win_pid_is_running(pid):
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # 5 = access denied: the process exists but is not ours.
            # 87 = invalid parameter: no such process.
            return ctypes.get_last_error() != 87
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return True


def _record_hook_status(key, result):
    """Remember the last outcome of a background hook job, for the doctor.

    The Stop hook's output goes to NUL and its runner always exits 0, so
    without this a user cannot tell "ran", "skipped on purpose" and "broke"
    apart (issue #276). Best effort: never raises.
    """
    try:
        try:
            data = json.loads(HOOK_STATUS_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data[key] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "result": result}
        HOOK_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = HOOK_STATUS_FILE.with_name(HOOK_STATUS_FILE.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, HOOK_STATUS_FILE)
    except Exception:
        pass


def _flush_capture_checkpoint(args):
    """The checkpoint step of the deferred Stop/SessionEnd flush worker.

    The worker has no stdin, so the Stop runner hands it the session identity
    the host gave the hook as ``--session-id`` / ``--transcript-path``. On Codex
    with no identity from the host or the environment, no checkpoint is
    written: an anonymous "unknown" file can outrank real ones on restore
    (issue #278). Returns the checkpoint path or None.
    """
    flush_trigger = "end"
    sid = None
    transcript = None
    for i, a in enumerate(args):
        if a == "--trigger" and i + 1 < len(args):
            flush_trigger = args[i + 1]
        elif a == "--session-id" and i + 1 < len(args):
            sid = args[i + 1]
        elif a == "--transcript-path" and i + 1 < len(args):
            transcript = args[i + 1]
    if (_use_codex_session_adapter() and not sid and not transcript
            and _find_current_session_jsonl() is None):
        _record_hook_status(
            "stop_capture",
            "skipped: the Stop hook payload and environment carried no session id")
        return None
    path = compact_capture(trigger=flush_trigger, backfill_tools=True,
                           session_id=sid, transcript_path=transcript)
    _record_hook_status(
        "stop_capture",
        "ok: checkpoint written" if path else "no checkpoint written (session transcript not found)")
    return path


def _run_session_end_flush_worker(args, budget_seconds=20):
    """Run heavyweight Stop-hook work outside Codex's visible hook budget.

    ``budget_seconds`` bounds the whole flush; the inline cloud path passes what
    is left of the calling hook's own deadline so the flush can never outlive
    the hook timeout."""
    lock_dir = _acquire_session_end_flush_lock()
    if lock_dir is None:
        _record_hook_status("stop_flush", "skipped: another refresh was already running")
        return
    _lean = _remote_lean_profile()
    old_budget = _install_hook_budget(budget_seconds)
    _flush_result = "error: stopped early"
    try:
        if not _session_refresh_due():
            _flush_result = "skipped: last refresh and checkpoint were under 2 minutes ago"
        else:
            _flush_result = "ran"
        if _flush_result.startswith("ran"):
            try:
                # First-ever collection: DEEP backfill of all history still on disk
                # (not just the rolling 90-day window), so a long-time user's baseline
                # freezes from their REAL earliest sessions immediately -- we never make
                # someone with existing history wait for a fresh 30-day window. Claude
                # Code purges old transcripts over time, so we grab everything still
                # present the first chance we get; collect is incremental + zero-token
                # (it skips files already collected). Subsequent flushes use the rolling
                # 90-day window. The marker also re-triggers once after an upgrade to this
                # version, so existing users (e.g. installs that only started collecting
                # recently) get the deep backfill applied retroactively.
                _backfill_marker = SNAPSHOT_DIR / ".initial-backfill-done"
                _attempts_marker = SNAPSHOT_DIR / ".initial-backfill-attempts"
                _deep = not _backfill_marker.exists()
                _collect_days = _INITIAL_BACKFILL_DAYS if _deep else 90
                if _deep:
                    # Count this attempt BEFORE the heavy call, so a _HookTimeout (which is a
                    # BaseException that bypasses `except Exception` below) still increments the
                    # counter. Once we hit the cap, retire the deep glob unconditionally so it
                    # can never re-fire forever; the rolling 90-day window keeps data current.
                    try:
                        _attempts = int(_attempts_marker.read_text().strip() or "0")
                    except (OSError, ValueError):
                        _attempts = 0
                    try:
                        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
                        _attempts_marker.write_text(str(_attempts + 1))
                    except OSError:
                        pass
                    if _attempts + 1 >= _INITIAL_BACKFILL_MAX_ATTEMPTS:
                        try:
                            _backfill_marker.touch()
                        except OSError:
                            pass
                collect_sessions(days=_collect_days, quiet=True, rebuild=False)
                # Reached only when collect_sessions returns NORMALLY (no timeout): the deep
                # backfill completed within budget, so mark it done and stop re-globbing.
                if _deep:
                    try:
                        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
                        _backfill_marker.touch()
                    except OSError:
                        pass
            except Exception:
                pass
            try:
                # No browser in a cloud VM: the 4 MB dashboard rebuild is skipped
                # there (an explicit /token-dashboard still builds it on demand).
                if not _lean and not (_dashboard_hook_backoff_active() or _dashboard_selfheal_inflight()):
                    generate_standalone_dashboard(days=30, quiet=True)
            except _HookTimeout:
                # Gap 4: the bounded SessionEnd regen was killed by the 20s budget
                # (a large history's 4 MB build does not fit), so it may have left
                # a stale-or-degraded dashboard. Hand the rebuild to the same
                # detached, unbounded self-heal the explicit-open path uses, so the
                # file catches up off the hot path instead of waiting for the user
                # to re-open. Re-raised so the outer handler still runs cleanup.
                try:
                    _spawn_detached_dashboard_selfheal(days=30)
                except Exception:
                    pass
                raise
            except Exception:
                pass
            try:
                _flush_capture_checkpoint(args)
            except Exception:
                pass
            # Data-retention enforcement (enterprise compliance)
            try:
                _cleanup_quality_cache()
            except Exception:
                pass
            try:
                from archive_result import cleanup_old_archives
                cleanup_old_archives()
            except Exception:
                pass
            try:
                reclaim_orphaned_plugin_data_dirs()
            except Exception:
                pass
            try:
                _rotate_checkpoint_events()
            except Exception:
                pass
            try:
                _prune_trends_db()
            except Exception:
                pass
    except _HookTimeout:
        _flush_result = "timed out: refresh hit its time budget and resumes next turn"
    except Exception as exc:
        _flush_result = f"error: {type(exc).__name__}"
        raise
    else:
        # Flush work completed without a timeout. M-5: release the flush lock
        # BEFORE running post-flush extensions, so a slow/hung extension does
        # not extend lock hold time and block concurrent session-end flushes.
        # The flush work is already complete; the extension runs with the
        # user's privileges, not under the lock. The budget (SIGALRM watchdog)
        # is kept so a hung extension is still hard-killed — only the lock is
        # released early.
        _release_session_end_flush_lock(lock_dir)
        lock_dir = None
        # Optional local post-flush extensions (admin-placed, OFF by default).
        # See _run_post_flush_extensions: nothing is loaded unless the file
        # exists, every failure is swallowed, and extensions get the remaining
        # flush budget.
        try:
            _run_post_flush_extensions(
                time_left_fn=old_budget.remaining,
                version=TOKEN_OPTIMIZER_VERSION)
        except (Exception, SystemExit):
            pass
    finally:
        # Only release the lock if the else branch didn't already (timeout
        # path or an unexpected exception escaping the try block). The budget
        # is always cleared here — the else branch keeps it for the extension.
        if lock_dir is not None:
            _release_session_end_flush_lock(lock_dir)
        _clear_hook_budget(old_budget)
        _record_hook_status("stop_flush", _flush_result)


def _run_post_flush_extensions(time_left_fn=None, version="unknown"):
    """Load and run an optional local extension after the flush work.

    A user or admin may place a single file at
    ``<CONFIG_DIR>/extensions/post_flush.py`` defining ``run(context)``. It is
    OFF by default: with no file present this function does nothing (no import,
    no I/O beyond one existence check). The file is loaded ONLY from that exact
    path — never from the repo, the snapshot dir, or an env override — and only
    when it is safe to execute: owned by the current user, not
    group/world-writable, and with extensions/config directories that are not
    group/world-writable either (a peer able to write the file or its parent
    directory can inject code). Everything is fail-open: any exception the extension
    raises (or the load itself) is swallowed; the hook always completes. The
    extension receives the remaining flush budget via ``context["time_left_fn"]``
    and should defer work that does not fit; the watchdog may hard-exit the
    worker when the budget expires.

    fd hygiene mirrors module_runner.py's marker writer (C-5): O_NOFOLLOW
    refuses symlinks at the kernel level (a symlinked extension path is not
    followed), O_NONBLOCK refuses FIFOs (a FIFO at the extension path would
    block the worker thread indefinitely, preventing the finally from
    releasing the flush lock), and an st_uid == os.getuid() owner check on
    POSIX refuses an extension file planted by another user. Non-regular
    files (char/block devices) are refused via S_ISREG. The read is bounded
    to 1 MB (M-4) so a huge or sparse file cannot OOM the worker.

    SystemExit is caught alongside Exception (H-6): an extension calling
    sys.exit() must not escape the fail-open boundary and terminate the
    worker. Every decision point emits a [Token Optimizer] stderr line (H-5)
    so an admin who installs a broken extension gets feedback.

    context keys: trends_db, snapshot_dir, config_dir, runtime, version,
    time_left_fn. Token Optimizer ships no extensions and takes no
    responsibility for third-party ones; they run with the user's privileges.
    """
    ext_dir = CONFIG_DIR / "extensions"
    ext_path = ext_dir / "post_flush.py"
    try:
        # Open once and judge the fd, not the path: a stat()-then-exec would
        # leave a window where the file is swapped (or symlink-retargeted)
        # between the permission check and the code actually executed.
        # O_NOFOLLOW + O_NONBLOCK mirror module_runner.py's marker defense
        # (C-5): a symlink at ext_path fails ELOOP, a FIFO fails ENXIO
        # instead of blocking the worker forever. Both are absent on Windows;
        # getattr fallback of 0 makes them no-ops there.
        flags = (os.O_RDONLY
                 | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
        try:
            fd = os.open(ext_path, flags)
        except OSError as e:
            # FileNotFoundError (no extension installed, the common case) is
            # silent. ELOOP (symlink) / ENXIO (FIFO) get a stderr note so an
            # admin sees the security rejection (H-5).
            if e.errno != errno.ENOENT:
                sys.stderr.write(
                    f"[Token Optimizer] post-flush extension refused at "
                    f"{ext_path}: {os.strerror(e.errno)} (errno {e.errno})\n")
            return None
        try:
            st = os.fstat(fd)
            # C-5 / L-3: refuse non-regular files (char/block devices read
            # garbage data and are not executable code).
            if not stat.S_ISREG(st.st_mode):
                sys.stderr.write(
                    f"[Token Optimizer] post-flush extension refused at "
                    f"{ext_path}: not a regular file\n")
                return None
            # C-5: owner check on POSIX. An extension file planted by another
            # user (e.g. a world-readable config dir) is local code injection.
            # Windows ACLs are the protection there, not uid bits.
            if os.name != "nt":
                if st.st_uid != os.getuid():
                    sys.stderr.write(
                        f"[Token Optimizer] post-flush extension refused at "
                        f"{ext_path}: owner {st.st_uid} != current user "
                        f"{os.getuid()}\n")
                    return None
                # POSIX mode bits only: Windows reports 0o666 for every writable
                # file (no group/other split), so this check would reject every
                # extension there. Same hasattr-style POSIX gate as os.fchmod.
                if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    sys.stderr.write(
                        f"[Token Optimizer] post-flush extension refused at "
                        f"{ext_path}: group/world-writable\n")
                    return None
            # M-4: bounded read. Cap at 1 MB so a very large or sparse file
            # cannot OOM the worker and prevent the finally from releasing
            # the flush lock.
            read_size = min(st.st_size if st.st_size > 0 else 1_048_576,
                            1_048_576)
            source = os.read(fd, read_size)
        finally:
            # L-2: wrap os.close in try/except so a bad fd never escapes the
            # fail-open boundary.
            try:
                os.close(fd)
            except OSError:
                pass
        # Directory gates (POSIX): a group/world-writable extensions or config
        # directory lets a peer replace post_flush.py before the open above, so
        # refuse those too. Directories are judged by stat (they cannot be
        # opened like a regular file); Windows mode bits are unreliable so the
        # check is skipped there just like the file check above.
        if os.name != "nt":
            for directory in (ext_dir, CONFIG_DIR):
                try:
                    if directory.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                        sys.stderr.write(
                            f"[Token Optimizer] post-flush extension refused: "
                            f"directory {directory} is group/world-writable\n")
                        return None
                except OSError:
                    pass
        module = types.ModuleType("_to_post_flush_ext")
        module.__file__ = str(ext_path)
        try:
            exec(compile(source, str(ext_path), "exec"), module.__dict__)
        except Exception as e:
            sys.stderr.write(
                f"[Token Optimizer] post-flush extension failed to load at "
                f"{ext_path}: {type(e).__name__}\n")
            return None
        run = getattr(module, "run", None)
        if not callable(run):
            sys.stderr.write(
                f"[Token Optimizer] post-flush extension at {ext_path}: "
                f"no callable run(context) defined\n")
            return None
        context = {
            "trends_db": TRENDS_DB,
            "snapshot_dir": SNAPSHOT_DIR,
            "config_dir": CONFIG_DIR,
            "runtime": detect_runtime(),
            "version": version,
            "time_left_fn": time_left_fn,
        }
        try:
            result = run(context)
        except SystemExit as e:
            # H-6: SystemExit is a BaseException, not an Exception. Without
            # this catch, an extension calling sys.exit() would propagate
            # uncaught past the loader, past the caller's except Exception,
            # and terminate the worker — violating the docstring's guarantee
            # that "the hook always completes."
            sys.stderr.write(
                f"[Token Optimizer] post-flush extension at {ext_path} "
                f"called sys.exit({e.code!r}); swallowed (fail-open)\n")
            return None
        except Exception as e:
            sys.stderr.write(
                f"[Token Optimizer] post-flush extension at {ext_path} "
                f"raised {type(e).__name__}; swallowed (fail-open)\n")
            return None
        sys.stderr.write(
            f"[Token Optimizer] post-flush extension at {ext_path} ran "
            f"successfully\n")
        return result
    except (Exception, SystemExit):
        # Outer boundary: any unexpected failure (including a SystemExit that
        # somehow bypassed the inner catch) stays fail-open.
        return None


def _defer_session_end_flush(args):
    """Detach the expensive dashboard/session refresh so Stop returns quickly."""
    try:
        cmd = [
            _detached_python_exe(),
            str(Path(__file__).resolve()),
            "session-end-flush-worker",
            *args[1:],
        ]
        env = os.environ.copy()
        env["TOKEN_OPTIMIZER_RUNTIME"] = detect_runtime()
        _proc = spawn_detached(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(Path.cwd()),
            env=env,
            close_fds=True,
        )
        if _proc is None:
            _log_spawn_failure("session-end flush spawn failed")
    except Exception:
        pass


def _dispatch_session_end_flush(args, budget_seconds=None):
    """Route the `session-end-flush` CLI entry.

    Defer by DEFAULT so a legacy bare `session-end-flush` hook fossilized in
    settings.json (pre-5.11.77 script installs, no --defer) stops running the
    flush synchronously inline (which wedged Windows stop-hooks at 3/4). Only
    `--no-defer` forces the inline worker (manual refresh / tests). Returns the
    chosen mode string so callers/tests can assert the decision without spawning.
    """
    if "--no-defer" in args:
        _run_session_end_flush_worker(args)
        return "inline"
    if _remote_lean_profile():
        # Claude Code cloud: a detached worker can be cut off when the VM is
        # reclaimed right after the session ends, losing the session's rows.
        # Run the flush in this process instead, bounded by what is left of the
        # hook's own deadline (Stop 13 s, SessionEnd 58 s) so it never runs past
        # the hook timeout. A flush that does not fit resumes on the next Stop.
        # The Stop runner shares ONE deadline across its subcommands and passes
        # this one's fair share (``budget_seconds``); taking the whole remainder
        # instead let the flush overrun into keepwarm-arm's slice, the shared
        # HookDeadline then fired os._exit(0) and the runner's buffered stdout
        # was lost (review #9).
        _run_session_end_flush_worker(args, budget_seconds=_inline_flush_budget_seconds(cap=budget_seconds))
        return "inline-remote"
    _defer_session_end_flush(args)
    return "deferred"


def _inline_flush_budget_seconds(default=8.0, margin=0.5, cap=None):
    """Seconds the inline (cloud) session-end flush may use: the remaining time
    on the active hook deadline minus a margin, or ``default`` without one, and
    never more than ``cap`` (the caller's fair share of a shared deadline)
    minus the margin. Floor 0.1 s: a spent share gets a token attempt that
    the flush lock and the budget cut short, never an overrun."""
    budget = default
    try:
        from hook_runtime import current_deadline
        deadline = current_deadline()
        if deadline is not None:
            budget = min(20.0, deadline.remaining() - margin)
    except Exception:
        pass
    if cap is not None:
        try:
            budget = min(budget, float(cap) - margin)
        except (TypeError, ValueError):
            pass
    return max(0.1, budget)


def _dispatch_collect(args):
    """CLI ``collect`` entry. Bounded ONLY on the hook path.

    A fossilized SessionEnd hook that chains the ``collect`` and ``dashboard``
    subcommands never reaches ``session-end-flush``. The 20s HookDeadline is
    the last line of defense so an escaped fossil still closes the hook pipe
    on Windows.

    The budget is armed only when ``_running_under_hook()`` is
    true (hook runner / fossil context) AND ``--rebuild`` is not present.
    Arming it unconditionally killed interactive ``collect --rebuild`` on a
    large history mid-transaction (os._exit(0) -> SQLite rollback -> the
    "needs rebuild" flag stays true -> every later run wedges, exiting 0).
    ``--rebuild`` is exempt regardless of context: it is an intentionally
    heavy operation and bounding it mid-transaction is the wedge itself.
    """
    days = 90
    quiet = "--quiet" in args or "-q" in args
    for i, a in enumerate(args):
        if a == "--days" and i + 1 < len(args):
            try:
                days = int(args[i + 1])
            except ValueError:
                pass
    rebuild = "--rebuild" in args
    deadline = (
        _install_hook_budget(20) if (_running_under_hook() and not rebuild) else None
    )
    try:
        collect_sessions(days=days, quiet=quiet, rebuild=rebuild)
    except _HookTimeout:
        pass
    finally:
        _clear_hook_budget(deadline)

    # Existing-script-install self-heal. An old script
    # install has the collect&&dashboard fossil in settings.json and NO
    # ensure-health SessionStart hook, so run_ensure_health never reaches it.
    # The fossil's OWN hook-path run reconciles settings.json here, removing
    # the fossil going forward. Runs AFTER the flush and its 20s budget are
    # cleared, so it never delays the flush itself; the self-heal carries its
    # OWN short deadline (see the helper) so a stalled settings write can't
    # hold the hook pipe open. Throttled to 24h, fail-open. Gated on the hook
    # path so an interactive terminal `collect` never triggers it.
    if _running_under_hook():
        try:
            _maybe_self_heal_sessionend_fossils_on_hook()
        except Exception:
            pass


def _spawn_detached_dashboard_selfheal(days=30, force=False, *, hook_timeout=True):
    """Rebuild the dashboard in a DETACHED, unbounded background process.

    Bug B self-heal: when a bounded hook-path regen is killed by the 20s budget
    (a large history whose 4 MB rebuild does not fit the window), the on-disk
    dashboard used to stay stale forever -- the SessionEnd/Stop hook kept getting
    killed and never caught up. Instead, on that timeout we hand the rebuild to a
    child that:
      * runs with TOKEN_OPTIMIZER_INTERACTIVE=1 so it is UNBOUNDED (it finishes
        the rebuild however long it takes),
      * is fully detached (start_new_session / DETACHED_PROCESS) so it outlives
        the hook and never holds the hook's stdout pipe open,
      * is quiet and never opens a browser.
    The child is unbounded, so it cannot itself time out and re-spawn -- no loop.
    Fire-and-forget; never raises (a failed self-heal must not break the hook).

    ``force`` appends ``--force`` so the child bypasses the 60s write throttle.
    Used by the version-bump self-heal: a stale file written seconds ago by a
    just-killed regen must not throttle-skip the heal. The PR #154 version guard
    still applies inside generate_standalone_dashboard (force governs the throttle,
    not version precedence), so a forced heal never clobbers a newer dashboard.
    """
    if not _dashboard_heal_spawn_due():
        if hook_timeout:
            _record_dashboard_hook_timeout()
        return
    try:
        env = dict(os.environ)
        env["TOKEN_OPTIMIZER_INTERACTIVE"] = "1"
        env["TOKEN_OPTIMIZER_DASHBOARD_SELFHEAL"] = "1"
        env.pop("TOKEN_OPTIMIZER_HOOK", None)
        # Detach so the child outlives the killed hook: POSIX new session, or
        # (on Windows) DETACHED_PROCESS. start_new_session is POSIX-only.
        popen_kw = {} if os.name == "nt" else {"start_new_session": True}
        argv = [sys.executable, os.path.abspath(__file__),
                "dashboard", "--quiet", "--days", str(int(days))]
        if force:
            argv.append("--force")
        # creationflags MUST be inlined and mention _NO_WINDOW (CREATE_NO_WINDOW,
        # 0 off-Windows) so a console-less Windows host never flashes a cmd window
        # -- and so the spawn-site invariant test can see it. DETACHED_PROCESS is 0
        # off-Windows, so this evaluates to 0 on POSIX (the only valid value there).
        child = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=env,
            creationflags=(getattr(subprocess, "DETACHED_PROCESS", 0) | _NO_WINDOW),
            **popen_kw,
        )
        try:
            # The marker describes the rebuild child, not the hook that exits.
            _replace_dashboard_heal_pid(str(os.getpid()), str(child.pid))
        except (AttributeError, OSError):
            pass
        if hook_timeout:
            _record_dashboard_hook_timeout()
    except Exception as _e:
        try:
            marker = SNAPSHOT_DIR / _DASHBOARD_HEAL_LOCK_NAME
            if marker.read_text().strip() == str(os.getpid()):
                marker.unlink()
        except OSError:
            pass
        # Fire-and-forget: never raise (a failed self-heal must not break the
        # hook). But DO leave a breadcrumb — every other spawn site calls
        # _log_spawn_failure; this was the only one that swallowed silently,
        # leaving the dashboard stale forever with no diagnostic trail.
        try:
            _log_spawn_failure(
                "dashboard self-heal spawn failed: %s: %s" % (type(_e).__name__, _e)
            )
        except Exception:
            pass


# Thundering-herd guard for the version-bump dashboard self-heal. The marker is
# a short-lived inflight breadcrumb, NOT a held-for-duration lock: the first
# concurrent SessionStart claims it and spawns the detached forced rebuild; every
# other session that reaches the stale-sidecar branch within the window sees the
# fresh marker and no-ops. Reclaimed once older than the window so a genuinely
# later version bump can heal again (and a crashed spawner never wedges the heal).
_DASHBOARD_HEAL_LOCK_NAME = "dashboard.heal.lock"
_DASHBOARD_HEAL_LOCK_STALE_SECONDS = 60
_DASHBOARD_HOOK_BACKOFF_SECONDS = 3600
_DASHBOARD_BACKOFF_CLOCK_SKEW_SECONDS = 5


def _dashboard_hook_backoff_active():
    """Back off only after the detached child actually refreshed the dashboard."""
    marker = SNAPSHOT_DIR / "dashboard.hook.backoff"
    try:
        marker_mtime = marker.stat().st_mtime
        dashboard_mtime = DASHBOARD_PATH.stat().st_mtime
        age = time.time() - dashboard_mtime
        if dashboard_mtime < marker_mtime or not -_DASHBOARD_BACKOFF_CLOCK_SKEW_SECONDS <= age < _DASHBOARD_HOOK_BACKOFF_SECONDS:
            return False
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(str(marker), flags)
        try:
            return (stat.S_ISREG(os.fstat(fd).st_mode)
                    and os.read(fd, 64).decode("ascii", errors="ignore") == TOKEN_OPTIMIZER_VERSION)
        finally:
            os.close(fd)
    except OSError:
        return False


def _record_dashboard_hook_timeout():
    """Record the attempt; an updated dashboard activates the one-hour cooldown."""
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        marker = SNAPSHOT_DIR / "dashboard.hook.backoff"
        flags = (os.O_WRONLY | os.O_CREAT
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        fd = os.open(str(marker), flags, 0o600)
        try:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                os.ftruncate(fd, 0)
                os.write(fd, TOKEN_OPTIMIZER_VERSION.encode("ascii"))
        finally:
            os.close(fd)
    except OSError:
        pass


def _replace_dashboard_heal_pid(expected, child_pid):
    marker = SNAPSHOT_DIR / _DASHBOARD_HEAL_LOCK_NAME
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(str(marker), flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return
        if os.read(fd, 64).decode("ascii", errors="ignore").strip() != expected:
            return
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, child_pid.encode("ascii"))
        os.ftruncate(fd, len(child_pid))
    finally:
        os.close(fd)


def _dashboard_selfheal_inflight():
    with _dashboard_selfheal_build_lock() as acquired:
        return acquired is False


@contextmanager
def _dashboard_selfheal_build_lock():
    """Let one detached rebuild run; the OS releases this lock after a crash."""
    fd = None
    held = None
    lock_attempted = False
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SNAPSHOT_DIR / "dashboard.selfheal.build.lock"
        flags = (os.O_RDWR | os.O_CREAT
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        fd = os.open(str(path), flags, 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "dashboard self-heal lock is not a regular file")
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            lock_attempted = True
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            lock_attempted = True
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = True
    except OSError as exc:
        if lock_attempted and (exc.errno in (errno.EACCES, errno.EAGAIN) or getattr(exc, "winerror", None) == 33):
            held = False
        else:
            _log_spawn_failure("dashboard self-heal lock failed: %s" % exc)
    try:
        yield held
    finally:
        if fd is not None:
            try:
                if held:
                    if os.name == "nt":
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _dashboard_heal_spawn_due():
    """Return True iff THIS caller should spawn a dashboard heal.

    Bug (thundering herd): on a version bump every concurrent
    SessionStart's ensure-health tick sees the stale sidecar and each spawns a
    DETACHED, unbounded, FORCED ~9s rebuild -- N sessions => N simultaneous
    builds all racing os.replace on the same dashboard.html (the very lock
    contention the api/regenerate retry was calibrated around). This collapses
    that to ONE: the first caller atomically claims ``dashboard.heal.lock``
    (O_CREAT|O_EXCL, the same pidfile shape as ``_keepwarm_tick_singleton`` and
    ``_daemon_install_lock``) and spawns; concurrent callers within the staleness
    window see the fresh marker and skip. The marker is deliberately NOT unlinked
    on the happy path -- it must OUTLIVE this call to suppress the herd, and it is
    reclaimed by the next caller once it ages past the window.

    The child holds a separate OS lock for the full rebuild. Check it here so
    even an expired marker cannot launch another child while one is working.
    Fail-OPEN on filesystem errors; the child lock still prevents duplicate work.
    """
    if _dashboard_selfheal_inflight():
        return False
    marker = SNAPSHOT_DIR / _DASHBOARD_HEAL_LOCK_NAME
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return True
    fd = None
    try:
        fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # A recent spawn already claimed the marker. Reclaim ONLY if it is stale;
        # a fresh marker means a heal is already in flight, so no-op.
        try:
            age = time.time() - marker.stat().st_mtime
        except OSError:
            return True  # unreadable mtime: don't suppress the heal
        if age <= _DASHBOARD_HEAL_LOCK_STALE_SECONDS:
            return False
        # Known bounded residual (same unlink+O_EXCL shape as
        # _keepwarm_tick_singleton): the fresh-claim path above is race-free, but
        # if TWO ticks both observe the SAME stale marker and both pass the age
        # check before either unlinks, both can reclaim and spawn -- at most 2
        # rebuilds, only at the 60s staleness edge with concurrent sessions. That
        # is consistent with the fail-open contract (an extra rebuild is harmless;
        # the detached child is itself idempotent via os.replace + the version
        # guard), so it is accepted rather than serialised with a heavier lock.
        try:
            os.unlink(str(marker))
            fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False  # lost the reclaim race to another session -- it heals
        except OSError:
            return True
    except OSError:
        return True
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return True


def _dispatch_dashboard(args):
    """CLI ``dashboard`` entry. Bounded ONLY on the hook path.

    Interactive ``--serve`` is unbounded on purpose: a user watching the
    dashboard must not have the process killed after 20s. A plain interactive
    ``dashboard`` run (no ``--serve``) from a terminal is also left unbounded
    The 20s budget exists to cap the escaped fossil on the hook
    pipe, not to kill a user-driven generation. ``--serve`` and "stdin is a
    tty" both mark an interactive context; the budget arms only when
    ``_running_under_hook()`` is true and ``--serve`` is absent.
    """
    cp = None
    serve = False
    serve_port = 8080
    serve_host = "127.0.0.1"
    for i, a in enumerate(args):
        if a == "--coord-path" and i + 1 < len(args):
            cp = args[i + 1]
        elif a == "--serve":
            serve = True
        elif a == "--host" and i + 1 < len(args):
            serve_host = args[i + 1]
            serve = True
        elif a == "--port" and i + 1 < len(args):
            try:
                serve_port = int(args[i + 1])
            except ValueError:
                print(f"[Error] Invalid --port value: {args[i + 1]}")
                sys.exit(1)
            serve = True
    if not cp:
        days = 30
        quiet = "--quiet" in args or "-q" in args
        # --force bypasses the 60s write throttle. The detached version-bump
        # self-heal passes it so a stale file just written by a killed regen
        # cannot throttle-skip the heal (the PR #154 version guard still applies).
        force = "--force" in args
        for i, a in enumerate(args):
            if a == "--days" and i + 1 < len(args):
                try:
                    days = int(args[i + 1])
                except ValueError:
                    pass
        deadline = (
            None if (serve or not _running_under_hook()) else _install_hook_budget(_dashboard_hook_budget_seconds())
        )
        timed_out = False
        try:
            lock = (_dashboard_selfheal_build_lock()
                    if os.environ.get("TOKEN_OPTIMIZER_DASHBOARD_SELFHEAL") == "1"
                    else nullcontext(True))
            with lock as acquired:
                if not acquired:
                    sys.exit(0)
                out = generate_standalone_dashboard(days=days, quiet=quiet, force=force)
        except _HookTimeout:
            out = None
            timed_out = True
        finally:
            _clear_hook_budget(deadline)
        # Bug B self-heal: a bounded hook regen that the 20s budget killed leaves
        # the on-disk dashboard stale. Finish it in a detached, unbounded child so
        # the file catches up without ever blocking the session. forward
        # `force` -- a killed --force regen just wrote a stale file seconds ago, so
        # the detached retry must ALSO bypass the 60s write throttle or it
        # throttle-skips and the file stays stale (the PR #154 version guard still
        # applies, so a forced heal never clobbers a newer dashboard).
        if timed_out:
            _spawn_detached_dashboard_selfheal(days=days, force=force)
        if out and serve:
            _serve_dashboard(out, port=serve_port, host=serve_host)
        elif out and not quiet:
            _open_dashboard(fallback_filepath=out)
        sys.exit(0 if out else 1)
    deadline = (
        None if (serve or not _running_under_hook()) else _install_hook_budget(_dashboard_hook_budget_seconds())
    )
    timed_out = False
    try:
        out = generate_dashboard(cp)
    except _HookTimeout:
        out = None
        timed_out = True
    finally:
        _clear_hook_budget(deadline)
    # Bug B self-heal (coord-path regen): same as the standalone branch above.
    if timed_out:
        _spawn_detached_dashboard_selfheal()
    if serve and out:
        _serve_dashboard(out, port=serve_port, host=serve_host)


_generate_codex_auto_recommendations = _measure_compat.bind(_measure_providers['audit']._generate_codex_auto_recommendations, __name__, defaults=(None, 30,))


_generate_foreign_auto_recommendations = _measure_compat.bind(_measure_providers['audit']._generate_foreign_auto_recommendations, __name__, defaults=(None, 30,))


# Token Optimizer's OWN skills. The tool must NEVER recommend archiving, trimming,
# or cutting the very features that do the measuring — suggesting a user delete
# token-coach/fleet-auditor to "save ~200 tokens" is self-cannibalizing and reads
# as the plugin undermining itself. These are always excluded from
# unused-skill / archive recommendations regardless of invocation history: an
# audit tool you never explicitly "invoke" is not the same as an unused skill.
_OWN_TOOL_SKILLS = frozenset({
    "token-optimizer", "token-coach", "token-dashboard", "fleet-auditor",
    "resume-checkpoint",
    "token-optimizer:token-optimizer", "token-optimizer:token-coach",
    "token-optimizer:token-dashboard", "token-optimizer:fleet-auditor",
    "token-optimizer:resume-checkpoint",
    "token-optimizer:health", "token-optimizer:quick",
})


_is_own_tool_skill = _measure_compat.bind(_measure_providers['audit']._is_own_tool_skill, __name__)


_skill_slim_clause = _measure_compat.bind(_measure_providers['audit']._skill_slim_clause, __name__)


generate_auto_recommendations = _measure_compat.bind(_measure_providers['audit'].generate_auto_recommendations, __name__, defaults=(None, 30,))


_DETCAND_COACH_BUDGET_S = 8.0
_DETCAND_COACH_MAX_SESSIONS = 60
_DETCAND_CLI_BUDGET_S = 60.0
_DETCAND_CLI_MAX_SESSIONS = 300


_deterministic_candidates_data = _measure_compat.bind(_measure_providers['audit']._deterministic_candidates_data, __name__, defaults=(30, _DETCAND_COACH_BUDGET_S, _DETCAND_COACH_MAX_SESSIONS, True, None,))


_cmd_deterministic_candidates = _measure_compat.bind(_measure_providers['audit']._cmd_deterministic_candidates, __name__)


generate_coach_data = _measure_compat.bind(_measure_providers['audit'].generate_coach_data, __name__, defaults=(None, None, None, False,))


_resolve_platform_models = _measure_compat.bind(_measure_providers['pricing']._resolve_platform_models, __name__)


_cmd_route = _measure_compat.bind(_measure_providers['pricing']._cmd_route, __name__)


generate_model_routing_block = _measure_compat.bind(_measure_providers['pricing'].generate_model_routing_block, __name__, defaults=(None,))


generate_coach_block = _measure_compat.bind(_measure_providers['audit'].generate_coach_block, __name__, defaults=(None, None,))


_find_all_jsonl_files = _measure_compat.bind(_measure_providers['sessions']._find_all_jsonl_files, __name__, defaults=(30,))


_find_subagent_jsonl_files = _measure_compat.bind(_measure_providers['sessions']._find_subagent_jsonl_files, __name__)


_extract_skills_and_agents_from_subagent = _measure_compat.bind(_measure_providers['sessions']._extract_skills_and_agents_from_subagent, __name__)


_extract_agent_type = _measure_compat.bind(_measure_providers['sessions']._extract_agent_type, __name__)


_analyze_subagent_costs = _measure_compat.bind(_measure_providers['sessions']._analyze_subagent_costs, __name__, defaults=(None,))


_extract_costly_prompts = _measure_compat.bind(_measure_providers['sessions']._extract_costly_prompts, __name__, defaults=(None, 5,))


_clean_project_name = _measure_compat.bind(_measure_providers['sessions']._clean_project_name, __name__)


_extract_topic = _measure_compat.bind(_measure_providers['sessions']._extract_topic, __name__)


_parse_session_jsonl_cache = {}
_PARSE_CACHE_MAX = 2000


# A slash-command invocation is written into the user turn as
# <command-name>name</command-name> (with or without a leading slash), alongside
# a <command-args> sibling. Skills driven by a slash command -- and
# disable-model-invocation skills, which can ONLY be invoked that way -- never
# emit a Skill tool_use, so without this they look "unused" no matter how often
# they run. The closing tag is required so a bare mention cannot match.
_SLASH_COMMAND_RE = re.compile(r"<command-name>\s*/?\s*([A-Za-z0-9:_+.-]+)\s*</command-name>")


_leading_user_text = _measure_compat.bind(_measure_providers['sessions']._leading_user_text, __name__)


_resolve_installed_skill = _measure_compat.bind(_measure_providers['sessions']._resolve_installed_skill, __name__)


_sidechain_path_reason = _measure_compat.bind(_measure_providers['sessions']._sidechain_path_reason, __name__)


_osrc_prompt_text = _measure_compat.bind(_measure_providers['sessions']._osrc_prompt_text, __name__)


_usage_request_key = _measure_compat.bind(_measure_providers['sessions']._usage_request_key, __name__, defaults=(None,))


_request_local_day = _measure_compat.bind(_measure_providers['sessions']._request_local_day, __name__)


_empty_model_buckets = _measure_compat.bind(_measure_providers['sessions']._empty_model_buckets, __name__)


_net_request_is_empty = _measure_compat.bind(_measure_providers['sessions']._net_request_is_empty, __name__)


_aggregate_request_usage = _measure_compat.bind(_measure_providers['sessions']._aggregate_request_usage, __name__)


_parse_session_jsonl = _measure_compat.bind(_measure_providers['sessions']._parse_session_jsonl, __name__, defaults=(None, None,))


parse_session_turns = _measure_compat.bind(_measure_providers['sessions'].parse_session_turns, __name__)


score_session_quality = _measure_compat.bind(_measure_providers['sessions'].score_session_quality, __name__)


# Sonnet generations older than Sonnet 5 (released 2026-06-30) bill at $3/$15 rather
# than $2/$10. Matches 4.6 / 4.5 / 4 / 3.7 / 3.5 / 3 in both dashed and dotted forms
# (e.g. "claude-sonnet-4-6", "claude-3-7-sonnet-20250219", "sonnet-4.5").
_SONNET_LEGACY_RE = re.compile(r"(?:^|[^0-9])(?:4[-._]?6|4[-._]?5|4[-._]?0|4|3[-._]?7|3[-._]?5|3)(?:[^0-9]|$)")


_record_model = _measure_compat.bind(_measure_providers['sessions']._record_model, __name__)


_normalize_model_name = _measure_compat.bind(_measure_providers['sessions']._normalize_model_name, __name__)


_CLAUDE_GENERATION_RE = re.compile(
    r"(fable|mythos|opus|sonnet|haiku)[-._]?(\d+)(?:[-._](\d{1,2}))?(?![0-9])")
# Claude 3-era ids put the version before the family ("claude-3-5-sonnet-20241022").
_CLAUDE_LEGACY_ID_RE = re.compile(r"claude-(\d)(?:[-.](\d))?-(opus|sonnet|haiku)(?![a-z])")


_claude_price_key = _measure_compat.bind(_measure_providers['sessions']._claude_price_key, __name__)


_load_overhead_snapshots = _measure_compat.bind(_measure_providers['sessions']._load_overhead_snapshots, __name__)


# ========== SQLite Trends DB ==========
# Pure Python, no Claude API. Runs standalone via `measure.py collect`.

TRENDS_DB = SNAPSHOT_DIR / "trends.db"

_SCHEMA = """
-- Counted-cumulative ledger: per-removal prevented-re-read records. One row per
-- counted removal event (savings_events 'se:<id>', measured compression_events
-- 'ce:<id>', or pre-ledger transcript marker 'mk:<sid>:<line>'). reread_* is
-- the removal's tokens x the deduped API turns from the event to the next real
-- compact_boundary in ITS OWN session, priced per-turn at that turn's model
-- cache-read rate. Computed incrementally in the collect pass; dashboard regen
-- only ever SUMs this table (never walks transcripts).
CREATE TABLE IF NOT EXISTS counted_reread (
    event_key TEXT PRIMARY KEY,
    source TEXT,
    session_uuid TEXT,
    event_ts TEXT,
    event_month TEXT,
    tokens INTEGER DEFAULT 0,
    oneshot_usd REAL DEFAULT 0.0,
    reread_tokens INTEGER DEFAULT 0,
    reread_usd REAL DEFAULT 0.0,
    turns_counted INTEGER DEFAULT 0,
    transcript_mtime REAL,
    computed_at TEXT,
    pricing_detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_counted_reread_session ON counted_reread (session_uuid);

CREATE TABLE IF NOT EXISTS session_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jsonl_path TEXT UNIQUE,
    date TEXT NOT NULL,
    project TEXT,
    duration_minutes REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    message_count INTEGER,
    api_calls INTEGER,
    cache_hit_rate REAL,
    cache_create_1h_tokens INTEGER DEFAULT 0,
    cache_create_5m_tokens INTEGER DEFAULT 0,
    cache_ttl_scanned INTEGER DEFAULT 0,
    avg_call_gap_seconds REAL,
    max_call_gap_seconds REAL,
    p95_call_gap_seconds REAL,
    skills_json TEXT,
    subagents_json TEXT,
    tool_calls_json TEXT,
    model_usage_json TEXT,
    all_model_usage_json TEXT,
    model_usage_breakdown_json TEXT,
    version TEXT,
    slug TEXT,
    topic TEXT,
    collected_at TEXT,
    quality_score REAL,
    quality_grade TEXT,
    stale_waste_tokens INTEGER DEFAULT 0,
    session_uuid TEXT,
    is_sidechain INTEGER DEFAULT 0,
    sidechain_reason TEXT,
    reported_input_tokens INTEGER,
    reported_output_tokens INTEGER,
    reported_model_usage_json TEXT,
    daily_usage_json TEXT,
    origin TEXT
);

-- Transcript copies of a session that lost dedupe to a more complete copy
-- (#200). Tracked so an unchanged losing copy is not re-parsed every flush,
-- while a copy that grows is re-checked and can take over.
CREATE TABLE IF NOT EXISTS session_log_aliases (
    jsonl_path TEXT PRIMARY KEY,
    session_uuid TEXT,
    collected_at TEXT
);

-- What each session has counted of each API request. The same request can sit
-- in more than one transcript (a resumed or forked session carries its parent's
-- history, and a copy may hold a longer streamed reply than another), so the
-- sessions together count the largest figure seen for it, once. The hashes are
-- of the request id and the session id; the rest is that session's share, with
-- the model and day it was billed to so the share survives its transcript
-- losing the record.
CREATE TABLE IF NOT EXISTS usage_request_claims (
    request_hash INTEGER NOT NULL,
    owner_hash INTEGER NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    inp INTEGER NOT NULL DEFAULT 0,
    out INTEGER NOT NULL DEFAULT 0,
    cr INTEGER NOT NULL DEFAULT 0,
    cc INTEGER NOT NULL DEFAULT 0,
    cc_1h INTEGER NOT NULL DEFAULT 0,
    cc_5m INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    day TEXT,
    PRIMARY KEY (request_hash, owner_hash)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_usage_request_claims_owner ON usage_request_claims(owner_hash);

CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    session_count INTEGER,
    total_input INTEGER,
    total_output INTEGER,
    total_duration REAL,
    avg_cache_hit REAL,
    avg_quality_score REAL,
    worst_grade TEXT
);

CREATE TABLE IF NOT EXISTS skill_daily (
    date TEXT,
    skill TEXT,
    session_count INTEGER,
    invocations INTEGER,
    PRIMARY KEY (date, skill)
);

CREATE TABLE IF NOT EXISTS model_daily (
    date TEXT,
    model TEXT,
    total_tokens INTEGER,
    PRIMARY KEY (date, model)
);

CREATE TABLE IF NOT EXISTS subagent_daily (
    date TEXT,
    agent_type TEXT,
    spawn_count INTEGER,
    PRIMARY KEY (date, agent_type)
);

CREATE TABLE IF NOT EXISTS savings_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    tokens_saved INTEGER DEFAULT 0,
    cost_saved_usd REAL DEFAULT 0.0,
    session_id TEXT,
    session_uuid TEXT,
    detail TEXT,
    model TEXT,
    unjoinable INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS compression_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT,
    session_uuid TEXT,
    feature TEXT NOT NULL,
    command_pattern TEXT,
    original_tokens INTEGER DEFAULT 0,
    compressed_tokens INTEGER DEFAULT 0,
    compression_ratio REAL DEFAULT 0.0,
    quality_preserved INTEGER DEFAULT 1,
    verified INTEGER DEFAULT 0,
    detail TEXT,
    model TEXT,
    tier TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_log_date ON session_log (date);
CREATE INDEX IF NOT EXISTS idx_session_log_date_collected ON session_log (date DESC, collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_savings_events_ts ON savings_events (timestamp);
CREATE INDEX IF NOT EXISTS idx_compression_events_ts ON compression_events (timestamp);
"""


_classify_jsonl_is_sidechain = _measure_compat.bind(_measure_providers['trends']._classify_jsonl_is_sidechain, __name__, defaults=(200,))


_scan_jsonl_is_sidechain = _measure_compat.bind(_measure_providers['trends']._scan_jsonl_is_sidechain, __name__, defaults=(200,))


_backfill_is_sidechain = _measure_compat.bind(_measure_providers['trends']._backfill_is_sidechain, __name__, defaults=(500,))


_backfill_reported_token_usage = _measure_compat.bind(_measure_providers['trends']._backfill_reported_token_usage, __name__, defaults=(500,))


_backfill_outsourcerer_sidechain = _measure_compat.bind(_measure_providers['trends']._backfill_outsourcerer_sidechain, __name__)


_repair_osrc_sidechain_misflag = _measure_compat.bind(_measure_providers['trends']._repair_osrc_sidechain_misflag, __name__)


_recompute_session_tokens = _measure_compat.bind(_measure_providers['trends']._recompute_session_tokens, __name__, defaults=(0.1, None,))


_log_migration_error = _measure_compat.bind(_measure_providers['trends']._log_migration_error, __name__)


_SkipMigration = _measure_compat.bind(_measure_providers['trends']._SkipMigration, __name__)


_origin_insert_parts = _measure_compat.bind(_measure_providers['trends']._origin_insert_parts, __name__)


_claude_projects_like_patterns = _measure_compat.bind(_measure_providers['trends']._claude_projects_like_patterns, __name__)


# session_log.jsonl_path with Windows backslashes folded to forward slashes,
# so one path pattern matches rows written on every OS (the platform backfill).
_PLATFORM_PATH_SQL = "REPLACE(jsonl_path, '\\', '/')"


# Counted views: the single place that decides which ledger rows count as
# savings. Every savings total reads these views instead of the raw tables, so
# the CLI report, dashboard, status bar and weekly card always agree. Rows are
# never deleted or rewritten; the views only leave out rows that are not
# savings:
#   - media results (screenshots and other image-bearing tool results). Images
#     bill by pixels, not by base64 length, so archiving one saved nothing.
#   - fixture sessions written by test suites and benchmarks.
#   - twin rows: two installs firing on one call write the same row within a
#     few seconds; only the first one counts.
_COUNTED_TWIN_WINDOW_SECONDS = 3.0
# Session ids that only test suites and benchmark harnesses use. Matched with
# GLOB (case-sensitive, anchored). Real ids are UUIDs, short hex agent ids, or
# harness ids such as Codex "rollout-<date>-<uuid>"; none of them match.
_FIXTURE_SESSION_GLOBS = (
    "test-session-*", "failwire-*", "unit-c-never*", "torture-*",
    "f3bench-sess*", "sess_test*",
)
# Tool names whose results carry images. Matched against the archive event's
# detail text ("replaced <tool> (...)" / "blocked re-fetch of <tool> (...)" /
# "<tool_use_id> (<tool>)" on a re-expansion).
_MEDIA_TOOL_GLOBS = (
    "*claude-in-chrome__computer*", "*computer_use__*", "*[Ss]creenshot*",
)
_MEDIA_ARCHIVE_EVENT_TYPES = ("tool_archive", "tool_archive_refetch_block", "tool_archive_reexpand")
# Older re-expansion rows carry only the tool_use_id. One is paired with the
# archive row it reverses: same session, archived up to an hour before, and the
# re-expansion is the archived size plus the pointer stub (a few hundred tokens).
_REEXPAND_PAIR_WINDOW_SECONDS = 3600
_REEXPAND_PAIR_STUB_TOKENS = 1200


_sql_quote = _measure_compat.bind(_measure_providers['trends']._sql_quote, __name__)


_fixture_session_sql = _measure_compat.bind(_measure_providers['trends']._fixture_session_sql, __name__, defaults=("session_id",))


_is_fixture_session_id = _measure_compat.bind(_measure_providers['trends']._is_fixture_session_id, __name__)


_sql_variants = _measure_compat.bind(_measure_providers['trends']._sql_variants, __name__)


_counted_twin_sql = _measure_compat.bind(_measure_providers['trends']._counted_twin_sql, __name__)


_media_reexpand_pair_sql = _measure_compat.bind(_measure_providers['trends']._media_reexpand_pair_sql, __name__)


_counted_view_statements = _measure_compat.bind(_measure_providers['trends']._counted_view_statements, __name__)


_COUNTED_VIEW_SQL = _counted_view_statements()


_counted_media_candidate_statement = _measure_compat.bind(_measure_providers['trends']._counted_media_candidate_statement, __name__)


_COUNTED_MEDIA_CANDIDATE_SQL = _counted_media_candidate_statement()


_ensure_counted_views = _measure_compat.bind(_measure_providers['trends']._ensure_counted_views, __name__)


# counted_reread rows whose source event the counted views leave out.
_COUNTED_REREAD_EXCLUDED_SQL = (
    "(event_key IN (SELECT 'se:'||id FROM main.savings_events "
    "EXCEPT SELECT 'se:'||id FROM counted_savings_events) OR "
    "event_key IN (SELECT 'ce:'||id FROM main.compression_events "
    "EXCEPT SELECT 'ce:'||id FROM counted_compression_events))"
)
_COUNTED_REREAD_PURGE_EXCLUDED_SQL = (
    f"DELETE FROM counted_reread WHERE {_COUNTED_REREAD_EXCLUDED_SQL}"
)

# The only names _counted_table can return: the counted view, then the raw table.
_COUNTED_SOURCES = {
    "savings_events": ("counted_savings_events", "savings_events"),
    "compression_events": ("counted_compression_events", "compression_events"),
}


_counted_table = _measure_compat.bind(_measure_providers['trends']._counted_table, __name__)


_counted_sql = _measure_compat.bind(_measure_providers['trends']._counted_sql, __name__)


_ACTIVITY_DATING_JOURNAL = "activity_dating_v1_journal"
_ACTIVITY_AGGREGATE_TABLES = ("daily_stats", "model_daily", "skill_daily", "subagent_daily")


_connect_trends_db = _measure_compat.bind(_measure_providers['trends']._connect_trends_db, __name__)


_snapshot_activity_dating_prestate = _measure_compat.bind(_measure_providers['trends']._snapshot_activity_dating_prestate, __name__)


_archive_pre_activity_state_files = _measure_compat.bind(_measure_providers['trends']._archive_pre_activity_state_files, __name__)


rollback_activity_dating = _measure_compat.bind(_measure_providers['trends'].rollback_activity_dating, __name__)


_init_trends_db = _measure_compat.bind(_measure_providers['trends']._init_trends_db, __name__)


_compute_call_gap_stats = _measure_compat.bind(_measure_providers['trends']._compute_call_gap_stats, __name__)


_make_session_key = _measure_compat.bind(_measure_providers['trends']._make_session_key, __name__)


_retry_db_operation = _measure_compat.bind(_measure_providers['trends']._retry_db_operation, __name__, defaults=(3, 0.1,))


_backfill_session_metrics = _measure_compat.bind(_measure_providers['trends']._backfill_session_metrics, __name__, defaults=(30, 50,))


_backfill_daily_usage = _measure_compat.bind(_measure_providers['trends']._backfill_daily_usage, __name__, defaults=(30, 20.0,))


_extract_session_uuid = _measure_compat.bind(_measure_providers['trends']._extract_session_uuid, __name__)


_log_spawn_failure = _measure_compat.bind(_measure_providers['trends']._log_spawn_failure, __name__)


_log_savings_event = _measure_compat.bind(_measure_providers['trends']._log_savings_event, __name__, defaults=(None, None, None, None,))


_cached_estimate_fn = None

_estimate_tokens = _measure_compat.bind(_measure_providers['trends']._estimate_tokens, __name__)


_log_compression_event = _measure_compat.bind(_measure_providers['trends']._log_compression_event, __name__, defaults=("", "", None, None, True, False, None, None,))


_compression_known_model = _measure_compat.bind(_measure_providers['trends']._compression_known_model, __name__)


_compression_row_rate = _measure_compat.bind(_measure_providers['trends']._compression_row_rate, __name__)


_UNSTAMPED_MODELS = frozenset({"", "unknown", "<synthetic>", "none", "null"})


_compression_session_rates = _measure_compat.bind(_measure_providers['trends']._compression_session_rates, __name__, defaults=((),))


_compression_mix_input_terms = _measure_compat.bind(_measure_providers['trends']._compression_mix_input_terms, __name__)


_compression_rate_coverage = _measure_compat.bind(_measure_providers['trends']._compression_rate_coverage, __name__)


_compression_priced_rate = _measure_compat.bind(_measure_providers['trends']._compression_priced_rate, __name__)


_get_compression_summary = _measure_compat.bind(_measure_providers['trends']._get_compression_summary, __name__, defaults=(30, None,))


# U6: human labels for coverage features. These ARE in _V5_COMPRESSION_CATEGORIES
# (so tier=measured rows reach the savings headline). Shadow/opportunity rows are
# excluded by the tier filter in _get_compression_summary, not by this set.
# Three-tier accounting. The single source of truth for tier ordering +
# display, mirrored by compression_log._VALID_TIERS on the write side.
_COMPRESSION_TIERS = ("measured", "estimated", "opportunity")
_TIER_DISPLAY_NAMES = {
    "measured": "Measured (active compression)",
    "estimated": "Estimated",
    "opportunity": "Opportunity (shadow, not active)",
}

# Mirror of read_cache.FEATURE_* — kept as literals here to avoid importing the
# hot-path read hook into this dashboard/CLI module.
_FEATURE_FIRST_READ_SKELETON = "first_read_skeleton"
_FEATURE_FIRST_READ_EDIT_FOLLOWUP = "first_read_edit_followup"

# Render the agent-result harm rate from the single source of truth
# (archive_result._AGENT_RESULT_HARM_PCT) so this label never drifts from the
# backfill number. Fail-open to a bare label if the import is unavailable.
try:
    from archive_result import _AGENT_RESULT_HARM_PCT as _AGENT_HARM_PCT
    _AGENT_RESULT_LABEL = (
        f"Agent/Task result (measure-only, harm {_AGENT_HARM_PCT:.0f}%)"
    )
except Exception:  # pragma: no cover
    _AGENT_RESULT_LABEL = "Agent/Task result (measure-only)"

_COVERAGE_FEATURE_LABELS = {
    "bash_generic": "Bash (unmatched output)",
    _FEATURE_FIRST_READ_SKELETON: "First-read skeleton",
    _FEATURE_FIRST_READ_EDIT_FOLLOWUP: "First-read edit follow-up",
    # Tripwire signal. NOT a _V5_COMPRESSION_CATEGORIES feature (0-token
    # opportunity-tier marker), so it can never reach the realized headline.
    "cohort_demoted": "Cohort auto-demoted (tripwire)",
    # Agent-result pool, measure-only opportunity (harm proxy failed the
    # gate). NOT a headline category; surfaces the pool in coverage only.
    "agent_result_skeleton": _AGENT_RESULT_LABEL,
    # Loop warnings: observed signal, counterfactual value. Kept out of
    # _V5_COMPRESSION_LABELS so NULL-tier legacy rows cannot read as measured;
    # the coverage view still shows the observed loop volume here.
    "loop_detection": "Loop detection (warning)",
}

# First-read shadow promotion gate: a cohort graduates from shadow to active
# only when the post-read edit rate is below this. Lower edit rate = the model
# rarely needed the full file = safer to serve a skeleton.
_FIRST_READ_PROMOTION_EDIT_RATE = 0.15


_coverage_feature_label = _measure_compat.bind(_measure_providers['trends']._coverage_feature_label, __name__)


_get_compression_coverage = _measure_compat.bind(_measure_providers['trends']._get_compression_coverage, __name__, defaults=(30,))


# ---------------------------------------------------------------------------
# Runtime quality TRIPWIRE for first-read active cohorts.
#
# The "no live shadow phase" directive replaces shadow validation with two
# safety nets: (1) history-replay backfill validates a cohort BEFORE promotion;
# (2) this tripwire watches the LIVE per-cohort edit-rate and auto-DEMOTES a
# cohort to measure-only the moment its rolling edit-rate exceeds the gate. The
# edit-rate is measured/measured: the count of active `first_read_skeleton`
# (tier=measured) events per cohort vs the count of `first_read_edit_followup`
# (tier=measured) events per cohort, over the last _TRIPWIRE_WINDOW events.
#
# Hysteresis (no flapping): the tripwire ONLY demotes. Re-promotion happens
# exclusively via the explicit CLI (`measure.py cohorts promote ...`) or the next
# backfill run, never automatically on the next-window dip. The demoted set is
# persisted to a 0600 sidecar the hot-path read consults (mtime-gated), and a
# `cohort_demoted` compression_events row (tier=opportunity, 0 tokens) is logged
# once per (cohort) transition so it surfaces in compression-stats + dashboard.
# ---------------------------------------------------------------------------

# Mirror of read_cache.FIRST_READ_ACTIVE_COHORTS — the cohorts eligible for the
# tripwire. Kept as literals here (the dashboard/CLI module must not import the
# hot-path read hook). A drift test pins the two lists together.
_FIRST_READ_ACTIVE_COHORTS = frozenset({
    ("python", "16-64KB"),
    ("python", "64-256KB"),
    ("typescript", "16-64KB"),
    ("typescript", "64-256KB"),
})

# Interpolated cohorts: promoted on a thin sample because the SAME language
# already passed the full gate in an ADJACENT band (typescript 16-64KB passed →
# its 64-256KB band graduates). They carry a HIGHER exposure risk
# (fewer historical reads) so the runtime tripwire judges them on a SMALLER
# sample floor — a demotion can fire after just _TRIPWIRE_MIN_SAMPLES_INTERP
# active skeletons instead of the full-gate floor. Mirror of read_cache's
# interpolated tier; the drift test pins them together.
_INTERPOLATED_COHORTS = frozenset({
    ("typescript", "64-256KB"),
})

_active_cohorts_drift_warned = False


_resolve_active_cohorts = _measure_compat.bind(_measure_providers['trends']._resolve_active_cohorts, __name__)


_TRIPWIRE_SIDECAR_NAME = "cohort_tripwire.json"
_TRIPWIRE_WINDOW = 50          # rolling window of recent active skeletons per cohort
_TRIPWIRE_EDIT_RATE = 0.15     # demote above this (same gate as promotion)
_TRIPWIRE_MIN_SAMPLES = 10     # full-gate cohorts: judge after this many active reads
_TRIPWIRE_MIN_SAMPLES_INTERP = 5  # interpolated cohorts: tighter floor (thin sample)
_TRIPWIRE_CACHE_TTL_SECONDS = 300
_FEATURE_COHORT_DEMOTED = "cohort_demoted"
_TRIPWIRE_LOCK_NAME = ".cohort_tripwire.lock"

# Rate-limit the stderr demotion push signal (T8) to once per process per cohort.
_tripwire_warned_cohorts: set = set()


_tripwire_min_samples = _measure_compat.bind(_measure_providers['trends']._tripwire_min_samples, __name__)


_cohort_basis = _measure_compat.bind(_measure_providers['trends']._cohort_basis, __name__)


_cohort_from_pattern = _measure_compat.bind(_measure_providers['trends']._cohort_from_pattern, __name__)


_compute_cohort_edit_rates = _measure_compat.bind(_measure_providers['trends']._compute_cohort_edit_rates, __name__, defaults=(None,))


_valid_tripwire_payload = _measure_compat.bind(_measure_providers['trends']._valid_tripwire_payload, __name__)


_read_tripwire_sidecar = _measure_compat.bind(_measure_providers['trends']._read_tripwire_sidecar, __name__, defaults=(False,))


_write_tripwire_sidecar = _measure_compat.bind(_measure_providers['trends']._write_tripwire_sidecar, __name__, defaults=(None,))


_tripwire_lock = _measure_compat.bind(_measure_providers['trends']._tripwire_lock, __name__, decorators=(contextmanager,))


evaluate_cohort_tripwire = _measure_compat.bind(_measure_providers['trends'].evaluate_cohort_tripwire, __name__, defaults=(False,))


_cohort_tripwire_state = _measure_compat.bind(_measure_providers['trends']._cohort_tripwire_state, __name__)


cohorts_promote = _measure_compat.bind(_measure_providers['trends'].cohorts_promote, __name__)


# ---------------------------------------------------------------------------
# Cache-TTL watchdog (opportunity tier — OBSERVED waste, POTENTIAL recovery).
#
# Anthropic dropped the *default* prompt-cache TTL from 60min to 5min in
# March 2026. A session with a natural pause >5min between turns can silently
# re-pay a cache WRITE on the same prefix instead of getting a cheap cache READ.
# We detect that re-write pattern from the JSONL turn stream and quantify it.
#
# TIER DISCIPLINE: this signal is OPPORTUNITY tier. It is a standalone analysis
# function — it does NOT write to compression_events and is NOT a
# _V5_COMPRESSION_CATEGORIES feature, so it can never reach the realized
# savings headline (_get_compression_summary / _get_merged_savings read only
# compression_events rows where tier='measured'). No shared write path exists.
# ---------------------------------------------------------------------------

# The 5-minute default-TTL boundary, in seconds. A gap strictly greater than
# this is assumed to have expired a 5m-TTL cache entry (Anthropic default since
# March 2026). Strict `>` so an exact-300s gap counts as not-yet-expired.
# Runtimes the Python cache-report engine can actually read per-turn cache +
# timestamp data for (Claude JSONL, Codex rollouts). Referenced by BOTH the
# runtime dispatch in _get_cache_ttl_waste AND _cache_coverage_gaps so wiring a
# third platform here can never leave it listed as a coverage gap at the same
# time. Add a runtime to this set and to the dispatch together.
_CACHE_WIRED_RUNTIMES = frozenset({"claude", "codex"})

# A turn's cache_creation must be at least this fraction of the previous turn's
# total cached prefix (read + creation) to be classed as a re-WRITE of the same
# prefix rather than incremental growth. Heuristic: JSONL does not expose the
# cache key, so we use re-created volume as the prefix-rewrite proxy.
_CACHE_REWRITE_PREFIX_FRACTION = 0.5

# automatic_discount detection: a turn N counts as "had a healthy cache" when its
# cached/prompt ratio is at least this high; the NEXT turn (after a gap > the
# profile TTL) counts as a "collapse" when its cached/prompt ratio falls below the
# collapse threshold while its prompt size stays comparable to turn N's prompt.
_CACHE_COLLAPSE_HEALTHY_RATIO = 0.40
_CACHE_COLLAPSE_THRESHOLD_RATIO = 0.10
_CACHE_COLLAPSE_PROMPT_COMPARABLE_FRACTION = 0.5  # next prompt >= 50% of prev prompt


_input_and_cached_read_rates = _measure_compat.bind(_measure_providers['cache_policy']._input_and_cached_read_rates, __name__)


_claude_rates_for_model = _measure_compat.bind(_measure_providers['cache_policy']._claude_rates_for_model, __name__)


_get_cache_ttl_waste = _measure_compat.bind(_measure_providers['cache_policy']._get_cache_ttl_waste, __name__, defaults=(30, False,))


# Sidecar result cache for the cache-health scan. The scan walks every JSONL /
# rollout in the window (~4.4s on a 30-day machine) and runs inside interactive
# dashboard renders, so the result is memoized to a 0600 sidecar with a 5-min
# mtime gate (the same idiom as _current_overhead_tokens). The `days` parameter
# is stored in the payload so a different window recomputes instead of returning
# a mismatched cache.
# W7: 15-minute TTL (was 5). The cold compute walks every JSONL/rollout in the
# window — measured ~4.1s on a 30-day machine — and runs inside interactive
# dashboard renders, so a longer TTL means that ~4s spike happens at most once
# per 15 minutes instead of once per 5. The window is still well under any
# session where the waste number would meaningfully change.
_CACHE_HEALTH_CACHE_TTL_SECONDS = 900

# Module-level in-process (mtime, payload) memo for the cache-health sidecar, the
# same idiom read_cache._demoted_cohorts uses: a process that reads the sidecar
# twice pays one stat the second time. Keyed implicitly by file identity (mtime);
# the runtime/days guard below re-validates the payload contents.
_cache_health_memo: "tuple[float, dict] | None" = None


_cache_ttl_waste_cached = _measure_compat.bind(_measure_providers['cache_policy']._cache_ttl_waste_cached, __name__, defaults=(30, False,))


# ========== Keep-warm state sidecar (arm record + resume detection) ==========
# Operational state ONLY (not the savings ledger; realized rows live in the
# SQLite savings_events path). This sidecar tracks which paused sessions are "armed"
# so the keepwarm-tick loop can later decide whether to ping. Two hard rules
# govern this region:
#   * The arm-record WRITER runs inside the Stop hook hot path. It must be
#     dumb and fast (well under the 500ms hook budget): read the transcript tail,
#     append one line, exit. NO eligibility evaluation, NO policy, NO network. If
#     anything is missing or unreadable it writes nothing and returns silently so
#     the hook chain is never broken.
#   * Eligibility and disarm are NOT hook responsibilities (no native resume
#     signal exists). The tick loop reads this sidecar, classifies each record via
#     the pure `classify_keepwarm_record` helper, and compacts resolved rows.
# Atomic 0600 writes (mkstemp+fchmod+os.replace) under flock, mirroring the
# cache_health.json / cohort_tripwire.json sites above. NOT compression_log.py
# (that is a SQLite writer despite its name).

_KEEPWARM_SIDECAR_NAME = "keepwarm_state.jsonl"
_KEEPWARM_LOCK_NAME = ".keepwarm_state.lock"
# Records older than this (by their arm ts) are dropped on compaction regardless of
# state: an arm that never resumed and never got pinged is dead weight after 14d.
_KEEPWARM_MAX_AGE_SECONDS = 14 * 24 * 3600
# How many tail lines of a transcript the fast arm-writer scans for the last
# usage-bearing turn. Bounded so a multi-MB transcript never blows the hook budget.
_KEEPWARM_TAIL_LINES = 400


_keepwarm_sidecar_path = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_sidecar_path, __name__)


_keepwarm_lock = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_lock, __name__, decorators=(contextmanager,))


_keepwarm_append_line = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_append_line, __name__)


_keepwarm_ttl_kind = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_ttl_kind, __name__)


_keepwarm_read_tail = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_read_tail, __name__, defaults=(_KEEPWARM_TAIL_LINES,))


_keepwarm_last_usage = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_last_usage, __name__)


_keepwarm_prefix_proxy = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_prefix_proxy, __name__)


_keepwarm_iso_to_epoch = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_iso_to_epoch, __name__)


write_keepwarm_arm_record = _measure_compat.bind(_measure_providers['cache_policy'].write_keepwarm_arm_record, __name__, defaults=(None,))


_valid_keepwarm_record = _measure_compat.bind(_measure_providers['cache_policy']._valid_keepwarm_record, __name__)


load_keepwarm_records = _measure_compat.bind(_measure_providers['cache_policy'].load_keepwarm_records, __name__, defaults=(None,))


classify_keepwarm_record = _measure_compat.bind(_measure_providers['cache_policy'].classify_keepwarm_record, __name__, defaults=(None,))


compact_keepwarm_sidecar = _measure_compat.bind(_measure_providers['cache_policy'].compact_keepwarm_sidecar, __name__, defaults=(None, None,))


# ========== Keep-warm billing + consent gate (eligibility) ==========
# THE pre-ping gate. The tick loop calls keepwarm_gate() before it touches any
# armed record; it returns (allowed, reason) and nothing pings unless allowed is
# True. Two independent conditions must both hold:
#   * billing == 'api'  — keep-warm only recovers a *write premium*, which only
#     API-key billing pays. On subscription auth a ping spends quota and saves
#     nothing (never net-negative), so subscription is a HARD off and is
#     never even asked for consent.
#   * consent == 'enabled' — opt-in only (the default-on rule explicitly does NOT
#     extend to token-spending features). Tri-state,
#     sticky, stored via the same _write_config_flag/_config_lock surface as the
#     rest of TO's config (0600 atomic write). States: unasked -> asked ->
#     enabled|declined. enabled/declined are terminal unless an explicit CLI
#     flip (keepwarm-enable / keepwarm-disable) moves them.
# Detection is CONSERVATIVE by construction: anything we cannot positively
# identify as API-key billing is treated as subscription (= off). Better to skip
# a legitimate ping than to ever burn a subscription user's quota.
# Every reader here is PURE + injectable (env dict + config paths) so the
# tests can drive each ladder rung without touching the real machine.

_KEEPWARM_CONSENT_KEY = "keepwarm_consent"
# Valid sticky consent states. 'unasked' is the implicit default (no key yet).
_KEEPWARM_CONSENT_STATES = ("unasked", "asked", "declined", "enabled")
# Env marker the ping subprocess sets on itself so a keepwarm-tick that runs
# *inside* a ping resume can never recursively arm/ping again.
_KEEPWARM_PING_ENV = "TOKEN_OPTIMIZER_KEEPWARM_PING"

# ===== LLM-PING KILL: the ONLY token-spending step is off by default =====
# Scope it tight: the WATCHER is awesome and stays ON -- the tick loop, meter
# reading, resume/cache-drop classification, forecast, and quota model are all
# code-only and spend ZERO tokens, so they keep watching "when the cash drops."
# The ONE thing that spins up an LLM and costs tokens is the keep-warm ping
# (`claude -p` in _keepwarm_fire_ping). THAT is gated off by default and never
# ships hot. An operator can allow the LLM ping for internal dogfood ONLY by
# explicitly setting the escape-hatch env var truthy; absent/false = ping OFF.
# Everything else about keep-warm runs normally.
_KEEPWARM_ALLOW_LLM_PING_ENV = "TOKEN_OPTIMIZER_KEEPWARM_ALLOW_LLM_PING"


_keepwarm_llm_ping_disabled = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_llm_ping_disabled, __name__, defaults=(None,))


_keepwarm_default_claude_json_path = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_default_claude_json_path, __name__)


_keepwarm_default_settings_path = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_default_settings_path, __name__)


_keepwarm_env_says_api = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_env_says_api, __name__)


_keepwarm_json_says_api = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_json_says_api, __name__)


keepwarm_billing_mode = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_billing_mode, __name__, defaults=(None, None, None,))


keepwarm_consent = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_consent, __name__)


_keepwarm_set_consent = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_set_consent, __name__)


keepwarm_mark_asked = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_mark_asked, __name__)


keepwarm_enable = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_enable, __name__, defaults=(None, None, None, False,))


_keepwarm_enable_install_scheduler = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_enable_install_scheduler, __name__)


keepwarm_disable = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_disable, __name__)


keepwarm_consent_status = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_consent_status, __name__, defaults=(None, None, None,))


keepwarm_gate = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_gate, __name__, defaults=(None, None, None,))


_keepwarm_tripwire_off = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_tripwire_off, __name__)


# ========== Keep-warm scheduler (OS-parity honesty) ==========
# Installs a launchd interval agent that runs `measure.py keepwarm-tick` every
# ~5min, fully decoupled from the dashboard HTTP daemon. Hard invariants:
#   * Distinct label `com.token-optimizer.keepwarm` -- never collides with the
#     dashboard's `com.token-optimizer.dashboard`.
#   * Its OWN install lock (_keepwarm_scheduler_install_lock) -- NEVER reuses
#     `_daemon_install_lock`, so a stuck dashboard install can't block keep-warm
#     and vice versa.
#   * Does NOT touch the dashboard plist, the dashboard staleness-regen loop, or
#     the dashboard install lock. The ensure-health repair below is a separate,
#     additive, consent-gated step that only ever manages the keep-warm agent.
#   * install REFUSES unless keepwarm_gate() allows (consent drives install).
#     A user who has not consented (unasked/declined) or whose billing is
#     subscription gets a printed reason and exit 1 -- nothing is written.
#   * Install marker sidecar (0600) records installed_at + plist path. ensure-
#     health's repair keys on marker AND consent: a user-deleted plist with a
#     marker + consent=enabled is regenerated; declined/unasked/absent-marker is
#     NEVER (re)installed (sticky-opt-out).
# Per-OS honesty: macOS launchd is implemented fully. The dashboard
# dispatcher's systemd/schtasks arms ARE real installers, but keep-warm only
# implements macOS in this unit; Linux/Windows print an honest documented-gap
# message + exit 1 (README parity row says macOS-only). We deliberately do
# NOT mirror the dashboard's systemd/schtasks into keep-warm yet -- that is
# explicit deferred scope, surfaced rather than silently half-built.

_KEEPWARM_SCHEDULER_LABEL = "com.token-optimizer.keepwarm"
# Marker filename lives under SNAPSHOT_DIR (resolved at call time so tests that
# monkeypatch SNAPSHOT_DIR get an isolated marker).
_KEEPWARM_SCHEDULER_MARKER_NAME = ".keepwarm-scheduler-marker"
_KEEPWARM_SCHEDULER_LOCK_NAME = ".keepwarm-scheduler.lock"
_KEEPWARM_SCHEDULER_LOG_NAME = "keepwarm-tick.log"
# A tiny JSON stamp written on every real (non-dry) tick. Drives the scheduler's
# last_tick_ok/last_tick_age freshness signal independently of whether the log
# carries a traceback (the log can be quiet-clean yet the scheduler dead).
_KEEPWARM_TICK_STAMP_NAME = ".keepwarm-tick-stamp.json"
# StartInterval: 5min (matches the "~5min" cadence). ThrottleInterval 60s so
# launchd never hot-loops the tick if it exits fast. Log rotation guard truncates
# the combined stdout/stderr log when it exceeds this size, on install and on the
# tick's own check.
_KEEPWARM_SCHEDULER_START_INTERVAL = 300
_KEEPWARM_SCHEDULER_THROTTLE = 60
_KEEPWARM_SCHEDULER_LOG_MAX_BYTES = 1024 * 1024  # 1MB


_keepwarm_scheduler_plist_path = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_scheduler_plist_path, __name__)


_keepwarm_scheduler_marker_path = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_scheduler_marker_path, __name__)


_keepwarm_scheduler_log_path = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_scheduler_log_path, __name__)


_keepwarm_tick_stamp_path = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_tick_stamp_path, __name__)


_keepwarm_scheduler_log_append = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_scheduler_log_append, __name__)


_keepwarm_write_tick_stamp = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_write_tick_stamp, __name__, defaults=(None,))


_keepwarm_read_tick_stamp = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_read_tick_stamp, __name__)


_keepwarm_measure_script_path = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_measure_script_path, __name__)


_keepwarm_python3_path = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_python3_path, __name__)


_keepwarm_resolve_claude_bin = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_resolve_claude_bin, __name__)


_keepwarm_scheduler_install_lock = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_scheduler_install_lock, __name__, defaults=(False,))


_APPEND_LOG_MAX_BYTES = 1024 * 1024


_cap_append_log = _measure_compat.bind(_measure_providers['cache_policy']._cap_append_log, __name__, defaults=(_APPEND_LOG_MAX_BYTES,))


_keepwarm_rotate_log = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_rotate_log, __name__, defaults=(_KEEPWARM_SCHEDULER_LOG_MAX_BYTES,))


_keepwarm_generate_scheduler_plist = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_generate_scheduler_plist, __name__)


_keepwarm_write_scheduler_marker = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_write_scheduler_marker, __name__, defaults=("__unset__",))


_keepwarm_read_scheduler_marker = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_read_scheduler_marker, __name__)


_keepwarm_remove_scheduler_marker = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_remove_scheduler_marker, __name__)


_keepwarm_scheduler_last_tick_ok = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_scheduler_last_tick_ok, __name__)


_keepwarm_scheduler_last_tick_age = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_scheduler_last_tick_age, __name__, defaults=(None,))


_keepwarm_scheduler_install_macos = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_scheduler_install_macos, __name__, defaults=(False,))


keepwarm_scheduler_install = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_scheduler_install, __name__, defaults=(None, False,))


keepwarm_scheduler_uninstall = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_scheduler_uninstall, __name__, defaults=(False,))


keepwarm_scheduler_status = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_scheduler_status, __name__, defaults=(None,))


keepwarm_scheduler_repair = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_scheduler_repair, __name__, defaults=(None,))


# ========== Keep-warm policy core + ping executor ==========
# This region is the "brain and the trigger" for keep-warm cache automation:
#   * keepwarm_policy_decision()  -- PURE. Zero IO. Drives both the live tick and
#     the history-backfill replay, so it MUST stay side-effect free.
#   * _keepwarm_fire_ping()       -- the ONLY side-effectful part. Locks, idle-
#     re-checks, pre-logs spend, runs the CLI ping, books the outcome.
#   * keepwarm-tick               -- the CLI loop (gate -> load -> classify ->
#     decide -> fire), wired in the dispatcher at the bottom of measure.py.
#
# Economics (survival-curve-driven, never a hardcoded clock):
#   A kept-warm prefix avoids a future re-WRITE of that prefix when the user
#   resumes. The avoided cost = prefix_tokens * write_rate (per token) * the
#   probability the user actually resumes at the current elapsed time. We only
#   keep pinging while the cumulative ping cost we have ALREADY spent stays below
#   that expected avoided write -- otherwise we abort the pause (break-even).
#
#   write_rate is TTL-dependent: a 1h entry would be re-written at the 2.0x
#   premium; a 5m entry at the 1.25x premium. The 5m premium is so low that the
#   formula almost always yields probe-only-or-nothing -- the FORMULA decides,
#   not a constant (Risk: default-TTL 1h->5m regression).
#
# Phases (state machine): probe = pings #1 and #2 at a TTL-aware cadence.
# Sustain = pings #3+ ONLY when history_stats.predictor_continue is True (the
# backfill wires the real predictor; default False = probe-only floor, measured
# +$13/30d, positive by construction) AND the tripwire allows sustain. 12h is an
# absolute hard cap on elapsed pause age regardless of phase.

# Survival curve P(resume | still paused at elapsed) from measured 30d history.
# Keys are elapsed-since-pause-start seconds
# bucket lower-bounds; we use the nearest bucket at-or-below the elapsed time.
_KEEPWARM_SURVIVAL_TABLE = (
    (3600, 0.245),    # >=1h paused
    (7200, 0.149),    # >=2h
    (14400, 0.119),   # >=4h
    (28800, 0.073),   # >=8h
    (57600, 0.032),   # >=16h
)
# Eligibility floor: prefixes smaller than this are not worth a ping (the read
# cost of the ping itself rivals the avoided write).
_KEEPWARM_PREFIX_FLOOR = 10000
# Absolute hard cap on pause age. Past this we always stop, regardless of phase
# or predictor (a blind 12h *clock* cap is net-negative, but as an upper
# bound on predictor-gated sustain it is the safety ceiling).
_KEEPWARM_HARD_CAP_SECONDS = 12 * 3600
# Probe cadence for 1h entries: ping at ~effective-window-minus-safety-margin so
# the refresh lands before the 1h cache would expire. 3600 - 300 margin = 3300s.
_KEEPWARM_1H_WINDOW_SECONDS = 3600
_KEEPWARM_1H_SAFETY_MARGIN = 300
_KEEPWARM_1H_CADENCE = _KEEPWARM_1H_WINDOW_SECONDS - _KEEPWARM_1H_SAFETY_MARGIN
# 5m entries: the window is 300s; cadence (if viable at all) is window minus a
# proportional margin. Viability is decided by the break-even formula, NOT this.
_KEEPWARM_5M_WINDOW_SECONDS = 300
_KEEPWARM_5M_SAFETY_MARGIN = 60
_KEEPWARM_5M_CADENCE = _KEEPWARM_5M_WINDOW_SECONDS - _KEEPWARM_5M_SAFETY_MARGIN
# Per-TTL write premium multiplier on the input rate, DERIVED from PRICING_TIERS
# at module init (single source of truth -- no hand-copied numbers). The anthropic
# tier's cache_write / input ratio is the 5m premium; cache_write_1h / input is
# the 1h premium. 'unknown' falls back to the 5m (conservative-low) premium.
# A future PRICING_TIERS rate edit propagates here automatically.
_keepwarm_derive_pricing = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_derive_pricing, __name__)


(_KEEPWARM_INPUT_USD_PER_TOKEN,
 _KEEPWARM_CACHE_READ_USD_PER_TOKEN,
 _KEEPWARM_WRITE_PREMIUM) = _keepwarm_derive_pricing()
# Budget cap passed to --max-budget-usd. A single bland ping reads the prefix as
# a cache hit (~$0.01-0.04 even for large prefixes) and writes a tiny refresh; a
# few cents is a generous ceiling that still aborts a runaway.
_KEEPWARM_PING_BUDGET_USD = 0.10
# The ping subprocess wall-clock ceiling. One shot per tick; no in-tick retry.
_KEEPWARM_PING_TIMEOUT_SECONDS = 180
# Idle re-check: the transcript must have been untouched for at least this long
# immediately before we fire, else the session may be live -> skip.
_KEEPWARM_IDLE_RECHECK_SECONDS = 120
# Per-session ping lock filename prefix in SNAPSHOT_DIR.
_KEEPWARM_PING_LOCK_PREFIX = ".keepwarm_ping."
# Whole-tick single-instance lock filename.
_KEEPWARM_TICK_LOCK_NAME = ".keepwarm_tick.lock"
# Ping-spend / outcome ledger sidecar (operational accounting only; realized
# savings live in the SQLite path, NOT here).
_KEEPWARM_LEDGER_NAME = "keepwarm_ledger.jsonl"
# A lockfile whose owning PID is gone and whose mtime is older than this is
# considered stale and reclaimed (a crashed tick must not wedge a session).
_KEEPWARM_STALE_LOCK_SECONDS = 600
# A 'firing' ledger row older than this with no sibling outcome row is treated as
# orphaned spend (the tick died between pre-log and outcome). The ping timeout is
# 180s; 600s leaves generous slack for a slow ping that simply hasn't resolved.
_KEEPWARM_ORPHAN_FIRING_SECONDS = 600
# The bland, tool-discouraging ping prompt (verified form A).
_KEEPWARM_PING_PROMPT = "Reply with exactly: ok"


_keepwarm_model_norm = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_model_norm, __name__)


_keepwarm_p_resume = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_p_resume, __name__)


_keepwarm_avoided_write_usd = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_avoided_write_usd, __name__)


_keepwarm_ping_cost_est = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_ping_cost_est, __name__)


_keepwarm_cadence_for = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_cadence_for, __name__)


keepwarm_policy_decision = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_policy_decision, __name__, defaults=(None, None,))


_keepwarm_ledger_path = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_ledger_path, __name__)


_keepwarm_ledger_append = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_ledger_append, __name__)


_keepwarm_scrub_error_reason = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_scrub_error_reason, __name__, defaults=(120,))


_keepwarm_acquire_ping_lock = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_acquire_ping_lock, __name__, defaults=(None,))


_keepwarm_release_ping_lock = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_release_ping_lock, __name__)


_keepwarm_cwd_is_contained = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_cwd_is_contained, __name__)


_keepwarm_extract_cwd = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_extract_cwd, __name__)


# Charset gate for values placed after `claude` flags (security M1). Model IDs and
# session IDs are both [A-Za-z0-9._-]; anything else (whitespace, leading-dash flag
# spoofing, shell metacharacters, control bytes) is rejected before the subprocess.
# First character must NOT be a dash: a leading `-` would let `--model`, `-h`,
# `--resume`, `-p` etc. pass as a value and shift the downstream flag positions.
_KEEPWARM_ARG_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]{0,127}$")


_keepwarm_valid_arg = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_valid_arg, __name__)


_keepwarm_build_ping_cmd = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_build_ping_cmd, __name__, defaults=("claude", _KEEPWARM_PING_BUDGET_USD,))


_keepwarm_parse_ping_json = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_parse_ping_json, __name__)


_keepwarm_fire_ping = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_fire_ping, __name__, defaults=(None, None,))


_keepwarm_emit_tick_observability = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_emit_tick_observability, __name__)


_keepwarm_tick_singleton = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_tick_singleton, __name__, decorators=(contextmanager,))


keepwarm_tick = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_tick, __name__, defaults=(None, False, None, None, None, None, None,))


_keepwarm_rewrite_records = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_rewrite_records, __name__, defaults=(None,))


# ========== Keep-warm resume predictor + history-backfill ==========
# The +$119 upper bound gated sustain on hindsight (it knew which pauses resumed).
# Live code cannot. This ships an IMPLEMENTABLE predictor scored ONLY on features a
# tick can compute at ping time, then proves on the real 30d history that turning
# sustain on with that predictor still beats probe-only > 0 before promotion.
#
# Two hard properties (oracle-leakage guards, asserted by tests):
#   * The predictor's features are all computable from data that exists AT or
#     BEFORE the ping instant: the session's own past resume count, sibling/
#     machine activity in the recent window ENDING at the ping time, and the
#     elapsed-time survival prior. None reads the future (whether THIS pause will
#     resume). The replay reconstructs each feature as-of the ping time only.
#   * The predictor is a pure function over an injected feature dict; it never
#     touches the filesystem or clock. Backfill feeds reconstructed-at-ping-time
#     features; the live tick feeds real mtime scans through the same
#     signature. A "future-only" signal handed in is structurally ignored.

# Score weights (transparent, tunable). A pause is predicted to continue (sustain
# allowed) when the summed score clears the threshold. The survival prior is the
# dominant term: a pause already well past the dense 1-2h resume cluster is a poor
# bet no matter the side signals.
_KEEPWARM_PRED_W_PRIOR_RESUMES = 0.30   # session has its own history of >1h resumes
_KEEPWARM_PRED_W_SIBLING = 0.20         # another transcript in the SAME project is warm
_KEEPWARM_PRED_W_MACHINE = 0.10         # any machine-level Claude activity recently
_KEEPWARM_PRED_W_SURVIVAL = 1.0         # weight on P(resume|elapsed) survival prior
_KEEPWARM_PRED_THRESHOLD = 0.30         # continue sustain when score >= this
# Recency window (seconds) for sibling / machine activity signals. A sibling or
# machine touched within this window of the ping time counts as "active".
_KEEPWARM_PRED_ACTIVITY_WINDOW_SECONDS = 2 * 3600


keepwarm_resume_predictor = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_resume_predictor, __name__)


# --- History reconstruction: rebuild per-session pause events from transcripts.
# A "pause event" is a >1h gap between two consecutive usage turns whose next turn
# RE-WRITES the prior cached prefix (the watchdog's exact waste criterion); the
# avoided write is the right-hand side of break-even. A "tail" is the final turn
# of a session that never resumed within the window. Replay charges probe cost to
# tails and books savings only on covered, resumed pauses (the honesty rule).

_keepwarm_reconstruct_session_events = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_reconstruct_session_events, __name__)


_keepwarm_replay_pause = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_replay_pause, __name__, defaults=(None, None, None, False,))


_keepwarm_schedule_covers = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_schedule_covers, __name__)


_keepwarm_replay_history_stats = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_replay_history_stats, __name__)


_keepwarm_index_active = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_index_active, __name__, defaults=(None,))


keepwarm_backfill = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_backfill, __name__, defaults=(30, None, None, None,))


# --- Promotion fence sidecar (0600). The tick EXTENDS this file with the rolling
# tripwire ratio + demotion ladder; the backfill only writes the promotion-gated
# verdict.

_KEEPWARM_PROMOTION_NAME = "keepwarm_promotion.json"
# Reconciliation bands: probe-only landed +$13 (allow window
# drift); oracle-sustain landed +$119. If the replay is wildly outside these we
# do NOT silently promote -- the caller reports the divergence.
_KEEPWARM_RECON_PROBE_TARGET = 13.0
_KEEPWARM_RECON_PROBE_TOL = 15.0
_KEEPWARM_RECON_ORACLE_TARGET = 119.0
_KEEPWARM_RECON_ORACLE_TOL_FRAC = 0.30


_keepwarm_promotion_path = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_promotion_path, __name__)


keepwarm_backfill_reconcile = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_backfill_reconcile, __name__)


keepwarm_promotion_decide = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_promotion_decide, __name__)


_keepwarm_write_promotion_sidecar = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_write_promotion_sidecar, __name__, defaults=(None,))


keepwarm_backfill_run = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_backfill_run, __name__, defaults=(30, None, True, None, None,))


keepwarm_forecast = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_forecast, __name__, defaults=(30, None, None, None,))


# ========== Keep-warm accounting + tripwire + report ==========
# The ONLY place keep-warm savings are ever booked. A realized row is
# written exactly once per (session, pause), and only when the resumed turn's
# REAL usage proves a warm resume -- never off the resume CLASSIFICATION alone
# (classify_keepwarm_record maps expired/unreadable to 'resumed' too; see the
# caveat in that docstring). Realized $ enters the canonical merge via the savings_events SQLite
# path (event_type 'keepwarm'); ping SPEND stays ledger-side and is surfaced as
# a dedicated cost line. NET (realized - spend) is the prominent number. The raw
# ledger is NEVER summed into the merge by hand.

# event_type written to savings_events for a realized warm-resume avoided write.
_KEEPWARM_SAVINGS_EVENT_TYPE = "keepwarm"
# Fraction-of-prefix thresholds for the warm-vs-cold resume verdict.
_KEEPWARM_WARM_READ_FRACTION = 0.50   # cache_read >= this * prefix  -> warm read
_KEEPWARM_WARM_WRITE_FRACTION = 0.50  # cache_creation < this * prefix -> not a re-write
# Ledger statuses the tick adds beside the firing/ok/error/timeout rows. 'realized' and
# 'loss' are the idempotency markers per (session, arm_ts) pause.
_KEEPWARM_LEDGER_REALIZED = "realized"
_KEEPWARM_LEDGER_LOSS = "loss"


_keepwarm_realized_write_usd = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_realized_write_usd, __name__)


_keepwarm_pause_key = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_pause_key, __name__)


_keepwarm_load_ledger_rows = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_load_ledger_rows, __name__, defaults=(None,))


_KEEPWARM_LEDGER_RETAIN_DAYS = 30
_KEEPWARM_LEDGER_MAX_BYTES = 5 * 1024 * 1024  # 5MB


_keepwarm_compact_ledger = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_compact_ledger, __name__, defaults=(None, _KEEPWARM_LEDGER_RETAIN_DAYS,))


_keepwarm_booked_pause_keys = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_booked_pause_keys, __name__, defaults=(None,))


_keepwarm_first_usage_after = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_first_usage_after, __name__, defaults=(None,))


keepwarm_resume_is_warm = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_resume_is_warm, __name__)


_keepwarm_log_realized_savings = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_log_realized_savings, __name__, defaults=(None, None, None,))


keepwarm_detect_realized = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_detect_realized, __name__, defaults=(None, None, None,))


keepwarm_spend_summary = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_spend_summary, __name__, defaults=(None, None, None,))


# ========== Keep-warm SUBSCRIPTION quota-value model (2026-06-12) ==========
# HEADLINE DESIGN CORRECTION: the dollar model hard-offs
# subscription billing because a keep-warm ping "saves $0" -- the WRONG currency.
# On a Max/Pro SUBSCRIPTION the scarce resource is RATE-LIMIT QUOTA (the 5h / 7d
# windows), not cash. A cold resume (>cache TTL) re-writes the prefix at the
# 1.25-2x cache-WRITE premium, and on a subscription those extra tokens burn the
# 5h/7d quota -- the actual constraint. Keep-warm avoids that rewrite, so it SAVES
# QUOTA even when it saves $0. This model re-denominates keep-warm in METER terms:
#   spend = meter % a ping consumes (a cache-READ of the prefix; measured ~0)
#   saved = meter % a cold resume would burn re-WRITING the prefix, avoided
#   net   = saved - spend
#
# It is a READ-ONLY MEASUREMENT INSTRUMENT. It does NOT change keepwarm_gate():
# subscription stays hard-off here. Flipping subscription from hard-off to
# quota-ROI-on is a SEPARATE, explicitly-consented decision once real meter
# samples confirm the modeled prior. Two layers:
#   * MODELED prior -- under cache-tier-weighted metering (a cache READ is far
#     cheaper on the meter than a cache WRITE, the same shape as dollars), the
#     meter ratio saved/spent EQUALS the dollar ratio realized/spend that
#     keepwarm_spend_summary already computes. So modeled_ratio = realized_usd /
#     spend_usd. A prior, NOT a measurement.
#   * MEASURED overlay -- real before/after meter deltas captured around live
#     pings (keepwarm_meter_samples.jsonl). Refines the SPEND side with the
#     observed per-ping meter movement (hypothesis: ~0, from the 6-ping lab run).
#     The SAVED side stays modeled until controlled cold/warm resume meter samples
#     exist (handover step 4).

_KEEPWARM_METER_SAMPLES_NAME = "keepwarm_meter_samples.jsonl"
# A rate-limits.json older than this is treated as stale (the meter is account-
# global and slow-moving, but a stale read should not anchor a live ping delta).
_KEEPWARM_METER_STALE_SECONDS = 1800
# Minimum number of completed-ping meter samples before the MEASURED per-ping
# meter cost overrides the modeled prior (one or two noisy account-global reads
# must not flip the verdict).
_KEEPWARM_QUOTA_MIN_PING_SAMPLES = 5
# A modeled quota ratio (saved/spent) at/above this reads as a net-positive
# candidate worth confirming with measurement. >1 = saves more quota than spent.
_KEEPWARM_QUOTA_RATIO_POSITIVE = 1.0


_keepwarm_rate_limits_path = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_rate_limits_path, __name__)


_keepwarm_read_meters = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_read_meters, __name__, defaults=(None, None,))


_keepwarm_meter_samples_path = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_meter_samples_path, __name__)


_keepwarm_meter_sample_append = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_meter_sample_append, __name__)


_keepwarm_load_meter_samples = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_load_meter_samples, __name__, defaults=(None,))


_keepwarm_median = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_median, __name__)


_keepwarm_measured_ping_meter = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_measured_ping_meter, __name__)


keepwarm_quota_value_summary = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_quota_value_summary, __name__, defaults=(7, None, None, None, None, None,))


# ---- Keep-warm tripwire (clone of the sidecar discipline) ----
# Rolling 7-day realized/spend ratio drives a sticky demotion ladder
# sustain -> probe-only -> off. Two strikes: first breach demotes sustain (moot
# while sustain is unpromoted, but wired); a second breach (probe-only STILL
# <= 1.0 over a full 7d window with >= 20 pings) demotes to 'off' = the gate
# refuses with reason 'tripwire-off'. Fail-closed: a corrupt sidecar reads as
# the MOST-demoted state ('off'). Re-promotion only via an explicit
# keepwarm-enable re-run (records a fresh promoted_at). Leading health signal:
# pings_on_never_resumed / total_pings (alarm > 50%).
_KEEPWARM_TRIPWIRE_NAME = "keepwarm_tripwire.json"
_KEEPWARM_TRIPWIRE_LOCK_NAME = ".keepwarm_tripwire.lock"
_KEEPWARM_TRIPWIRE_WINDOW_DAYS = 7
_KEEPWARM_TRIPWIRE_RATIO_GATE = 1.0       # demote at ratio <= this
_KEEPWARM_TRIPWIRE_MIN_PINGS = 20         # min pings in window before off-demote
# Distinct-window strike semantics: a NEW strike requires at
# least this long since the previous strike, so a money-losing config has to fail
# across two SEPARATE days -- not two ticks 5min apart -- before it demotes.
_KEEPWARM_TRIPWIRE_STRIKE_WINDOW_SECONDS = 24 * 3600
_KEEPWARM_TRIPWIRE_ALARM_FRACTION = 0.50  # health alarm above this
# The demotion ladder, most-permissive first. Fail-closed maps to the last one.
_KEEPWARM_TRIPWIRE_LADDER = ("sustain", "probe-only", "off")


_keepwarm_tripwire_path = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_tripwire_path, __name__)


_valid_keepwarm_tripwire_payload = _measure_compat.bind(_measure_providers['cache_policy']._valid_keepwarm_tripwire_payload, __name__)


keepwarm_tripwire_mode = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_tripwire_mode, __name__)


_keepwarm_tripwire_lock = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_tripwire_lock, __name__, decorators=(contextmanager,))


_write_keepwarm_tripwire_sidecar = _measure_compat.bind(_measure_providers['cache_policy']._write_keepwarm_tripwire_sidecar, __name__, defaults=(None, None,))


_keepwarm_tripwire_health = _measure_compat.bind(_measure_providers['cache_policy']._keepwarm_tripwire_health, __name__, defaults=(None,))


evaluate_keepwarm_tripwire = _measure_compat.bind(_measure_providers['cache_policy'].evaluate_keepwarm_tripwire, __name__, defaults=(None, None, False,))


keepwarm_tripwire_repromote = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_tripwire_repromote, __name__, defaults=(None,))


keepwarm_report = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_report, __name__, defaults=(30, None,))


keepwarm_cache_health_block = _measure_compat.bind(_measure_providers['cache_policy'].keepwarm_cache_health_block, __name__, defaults=(30, None,))


# ===========================================================================
# Subagent prompt-cache TTL advice (`subagentPromptCacheTtl`).
#
# Claude Code gives subagents a 5-minute prompt cache even on a subscription,
# so a subagent returned to after more than 5 minutes rewrites its whole
# prefix. Claude Code >= 2.1.243 supports raising that per install via the
# `subagentPromptCacheTtl: "1h"` setting (docs say 2.1.242; the changelog
# floor is 2.1.243 -- use the higher). Token Optimizer measures whether it
# pays on the user's own transcripts and RECOMMENDS it; it never sets the
# key for you.
#
# ADVISE-ONLY: the automatic path never writes
# `subagentPromptCacheTtl`. Claude Code keeps subagents at 5 minutes because
# 1h costs more on average for agent-shaped work; our verdict is an estimate,
# and editing a user's settings on an estimate is not ok. What remains:
#   * SessionStart still keeps the cached payoff verdict fresh (the detached
#     background scan) and records what it says (marker `auto_decision`),
#     for every regime: unset ("would have saved about $X"), already "1h"
#     ("is costing / is paying"), and thin data (neutral).
#   * The verdict is surfaced as a per-user recommendation -- numbers plus
#     the exact command -- in `subagent-cache status`, doctor, quick, coach
#     (text and JSON), and the audit checklist.
#   * TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H=1 is the ONLY automatic write (an
#     explicit opt-in, and the only path that keeps the 14-day auto-revert
#     tripwire). =0 stays never (and undoes a value TO set earlier).
#   * `subagent-cache enable` / `disable` are the explicit commands; enable
#     is unconditional, disable is final (marker "removed" is sticky for
#     every automatic path, including the opt-in).
#
# Hard rules (each pinned by tests/test_subagent_cache_ttl.py):
#   * A value the user set is never overridden -- any pre-existing key (even
#     "5m") reads as "user-set" and is left alone.
#   * Env outranks: CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL or
#     FORCE_PROMPT_CACHING_5M set -> do nothing (documented precedence:
#     FORCE_PROMPT_CACHING_5M > bucket env > bucket setting).
#   * A managed/project/local settings file that already sets the key wins;
#     a plugin setting cannot (a plugin's own settings.json is ignored by
#     Claude Code for this key), so the USER settings.json is the only right
#     place -- and only when nothing else already answers.
#   * Claude Code >= 2.1.243, else skip with a reason (fail-closed on an
#     unknown version: never set on what we cannot verify).
#   * A marker in TO's own data dir remembers that TO set it (timestamp +
#     previous state). disable removes the key ONLY when the marker says TO
#     set it AND the value is still "1h". If the user removes or changes the
#     key after we set it, the marker flips to "user-declined" and the
#     automatic path never sets it again.
#   * Opt-out env TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H=0|false|off|no: never
#     set; and if TO set it earlier, undo.
#   * Unknown-state settings (unreadable, malformed, non-object, or -- for
#     automatic writes -- missing): never write. Explicit `enable` creates a
#     genuinely missing settings.json and says so.
#   * The verdict judgement is evidence-gated: a recommendation to turn it
#     on requires the user's own last 30 days to say it pays (>=
#     _SUBAGENT_CACHE_TRIPWIRE_MIN_REQUESTS subagent requests AND saving >=
#     _SUBAGENT_CACHE_AUTO_ENABLE_MARGIN x the 1h write premium).
#   * The scan never runs inside a hook. SessionStart reads one small cached
#     verdict (subagent_cache_verdict.json); a missing/day-old one starts the
#     scan as a detached child (subagent_cache_scan.lock keeps it to one at a
#     time; time budget + file cap, partial = no verdict) and the verdict is
#     judged at a later session start.
#   * The payoff is judged from the user's OWN transcripts, in both regimes
#     (see subagent_cache_payoff): rewrites a 1h TTL avoids (within one agent,
#     and across spawns sharing a prefix) and, once the setting is on, the
#     reads it really realized, against the 2x-vs-1.25x write premium.
#     Under the force-env opt-in, 14+ days of post-enable data and at least
#     _SUBAGENT_CACHE_TRIPWIRE_MIN_REQUESTS subagent requests with a NEGATIVE
#     net estimate -> the next SessionStart reverts (only when TO set it),
#     records "auto-reverted", and says so in one line. Without the opt-in
#     the same window only feeds the "is costing ... turn off" advice --
#     a key is never removed silently. The verdict is re-judged at most once
#     a day -- the payoff scan walks real transcripts and must not run on
#     every session start.
# ===========================================================================

_SUBAGENT_CACHE_KEY = "subagentPromptCacheTtl"
_SUBAGENT_CACHE_MARKER_NAME = "subagent_cache_state.json"
# Docs say v2.1.242; the changelog says 2.1.243. Use the higher floor.
_SUBAGENT_CACHE_CC_FLOOR = (2, 1, 243)
_SUBAGENT_CACHE_OPTOUT_ENV = "TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H"
# A 5m cache write that follows this gap would have been a read at 1h.
_SUBAGENT_CACHE_GAP_LOW = 300     # 5 minutes: below this the old write is alive
_SUBAGENT_CACHE_GAP_HIGH = 3600   # 1 hour: above this the 1h cache is gone too
# Tripwire: judge only once this much post-enable data exists.
_SUBAGENT_CACHE_TRIPWIRE_MIN_DAYS = 14
# ...and re-judge at most this often. The payoff scan walks real
# transcripts, so a verdict stands for a day; running it on every
# session start past day 14 would blow the hook time budget.
_SUBAGENT_CACHE_TRIPWIRE_REJUDGE_SECONDS = 86400
# ...and never on a thin sample: the window must hold at least this many
# subagent requests that touched the cache (read or wrote). A user with almost
# no subagents has no evidence either way and is never auto-reverted.
_SUBAGENT_CACHE_TRIPWIRE_MIN_REQUESTS = 200
# The "turn it on" recommendation is evidence-gated on the user's own last 30
# days: at least _SUBAGENT_CACHE_TRIPWIRE_MIN_REQUESTS subagent requests AND
# an estimated saving of at least this multiple of the estimated 1h write
# premium. A heavy subagent user measured -$25 net at 1.0x, so break-even is
# not enough: the 15% margin keeps "roughly a wash" from being recommended.
_SUBAGENT_CACHE_AUTO_ENABLE_MARGIN = 1.15
# The payoff scan walks real transcripts (about 9 s on a heavy history), so it
# never runs in a hook. A detached child writes this small verdict file; session
# start only reads it. A verdict older than a day is re-scanned, not applied.
_SUBAGENT_CACHE_VERDICT_NAME = "subagent_cache_verdict.json"
_SUBAGENT_CACHE_VERDICT_MAX_AGE = 86400
_SUBAGENT_CACHE_SCAN_LOCK_NAME = "subagent_cache_scan.lock"
# A lock older than the scan's own time budget plus slack is abandoned.
_SUBAGENT_CACHE_SCAN_LOCK_STALE = 300
_SUBAGENT_CACHE_SCAN_TOKEN_ENV = "TO_SUBAGENT_CACHE_SCAN_TOKEN"
# Scan bounds (the child is detached, so these protect the machine, not a hook).
# Running out of either makes the scan PARTIAL, and a partial scan is no verdict.
_SUBAGENT_CACHE_SCAN_BUDGET_SECONDS = 120
_SUBAGENT_CACHE_SCAN_MAX_FILES = 20000


_subagent_cache_marker_path = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_marker_path, __name__)


_subagent_cache_read_marker = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_read_marker, __name__)


_subagent_cache_write_marker = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_write_marker, __name__)


_subagent_cache_optout = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_optout, __name__, defaults=(None,))


_subagent_cache_force_on = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_force_on, __name__, defaults=(None,))


_subagent_cache_claude_code_version = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_claude_code_version, __name__)


_SUBAGENT_CACHE_UNKNOWN = object()   # probe not yet run sentinel
_SUBAGENT_CACHE_CC_VERSION = _SUBAGENT_CACHE_UNKNOWN


_subagent_cache_managed_settings_path = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_managed_settings_path, __name__)


_subagent_cache_external_settings_sources = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_external_settings_sources, __name__)


_subagent_cache_external_key_holder = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_external_key_holder, __name__)


_subagent_cache_claude_only = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_claude_only, __name__)


_subagent_cache_platform_gap = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_platform_gap, __name__, defaults=("not Claude Code",))


_subagent_cache_undo = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_undo, __name__, defaults=(False,))


# ---- cached verdict, background scan, evidence gate ----------------------

_subagent_cache_verdict_path = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_verdict_path, __name__)


_subagent_cache_scan_lock_path = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_scan_lock_path, __name__)


_subagent_cache_write_json_atomic = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_write_json_atomic, __name__)


_subagent_cache_read_verdict = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_read_verdict, __name__)


_subagent_cache_verdict_for = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_verdict_for, __name__, defaults=(None,))


_reclaim_stale_lock_file = _measure_compat.bind(_measure_providers['cache_policy']._reclaim_stale_lock_file, __name__)


_subagent_cache_scan_acquire_lock = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_scan_acquire_lock, __name__, defaults=(None,))


_subagent_cache_scan_release_lock = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_scan_release_lock, __name__)


subagent_cache_scan_run = _measure_compat.bind(_measure_providers['cache_policy'].subagent_cache_scan_run, __name__, defaults=(None, None, None, None, None,))


_subagent_cache_spawn_scan = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_spawn_scan, __name__, defaults=(None, None,))


_subagent_cache_judge_payoff = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_judge_payoff, __name__)


_subagent_cache_record_decision = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_record_decision, __name__)


_subagent_cache_auto_gate = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_auto_gate, __name__)


subagent_cache_enable = _measure_compat.bind(_measure_providers['cache_policy'].subagent_cache_enable, __name__, defaults=(None, True,))


subagent_cache_disable = _measure_compat.bind(_measure_providers['cache_policy'].subagent_cache_disable, __name__, defaults=(None,))


_subagent_cache_payoff_zero = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_payoff_zero, __name__)


subagent_cache_payoff = _measure_compat.bind(_measure_providers['cache_policy'].subagent_cache_payoff, __name__, defaults=(30, None, None, None, None,))


evaluate_subagent_cache_tripwire = _measure_compat.bind(_measure_providers['cache_policy'].evaluate_subagent_cache_tripwire, __name__, defaults=(None, None,))


_subagent_cache_session_start_lines = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_session_start_lines, __name__, defaults=(None,))


_subagent_cache_recommendation = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_recommendation, __name__, defaults=(None,))


_subagent_cache_status_decision = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_status_decision, __name__)


subagent_cache_status = _measure_compat.bind(_measure_providers['cache_policy'].subagent_cache_status, __name__, defaults=(30, None, False,))


subagent_cache_block = _measure_compat.bind(_measure_providers['cache_policy'].subagent_cache_block, __name__, defaults=(30, None,))


# ===========================================================================
# Usage recommendations: ONE daily-measured record that every surface
# reads.
#
# `usage_recommendations.json` (schema 1) carries one item per usage-based
# recommendation -- ``compact_window`` and ``subagent_cache`` -- each in one of
# four states: recommend | keep | not_enough_data | not_applicable. `command`
# is only ever populated for `recommend`. Both measurements reuse the existing
# bounded scans: the compact_advice replay (coach cap + wall-clock budget) and
# the cached subagent-cache payoff verdict (refreshing it through the existing
# detached-scan machinery when it is missing or stale). Token Optimizer NEVER
# writes either setting; the record only advises.
#
# `measure.py recommendations refresh` is the one refresher. It never runs
# inside a hook: session start and the dashboard data collection only check
# the record's age and spawn a detached child (same O_EXCL lock + env-token
# pattern as the subagent scan; spawn_detached owns the Windows no-console
# flags and _detached_python_exe prefers pythonw.exe). A missing or day-old
# record spawns; a partial or crashed run writes complete:false and is
# retried once the record ages out again.
#
# `usage_recommendations_history.jsonl` gets one compact line per refresh:
# measured_ts plus, per item, the state, the setting value observed, and the
# headline measure (cache-read tokens per session; subagent net estimate).
# Capped at 180 lines, rewritten atomically when over.
#
# Wins are conservative: an entry is only emitted when an earlier history line
# said `recommend`, the observed setting later matched the recommended value,
# at least 7 days passed, the minimum data bar holds now, and the measured
# outcome moved the expected way. The wording is "since you changed it" --
# measured, never causal.
# ===========================================================================

_RECS_RECORD_NAME = "usage_recommendations.json"
_RECS_HISTORY_NAME = "usage_recommendations_history.jsonl"
_RECS_LOCK_NAME = "usage_recommendations_refresh.lock"
# A lock older than the scans' own budgets plus slack is abandoned.
_RECS_LOCK_STALE = 300
_RECS_TOKEN_ENV = "TO_USAGE_RECS_REFRESH_TOKEN"
_RECS_REFRESH_SECONDS = 86400          # measured daily
_RECS_WINDOW_DAYS = 30
_RECS_HISTORY_CAP = 180
_RECS_WIN_MIN_DAYS = 7
# Compact-window gates: at most ~5 extra compactions a day and at least 10%
# of cache-read tokens avoided, priced-net positive, before we recommend.
_RECS_MAX_EXTRA_COMPACTIONS_PER_DAY = 5
# The refresh runs detached, so it replays the whole window. The coach's
# 150-session / 4-second slice is for inline callers: on a slice the extra
# compactions are undercounted and the smallest window wins by mistake.
_RECS_COMPACT_BUDGET_SECONDS = 120.0
_RECS_MIN_CACHE_READ_SHARE_PCT = 10.0
_RECS_LABELS = {"compact_window": "Compact window",
                "subagent_cache": "Subagent cache"}


_recs_record_path = _measure_compat.bind(_measure_providers['cache_policy']._recs_record_path, __name__)


_recs_history_path = _measure_compat.bind(_measure_providers['cache_policy']._recs_history_path, __name__)


_recs_lock_path = _measure_compat.bind(_measure_providers['cache_policy']._recs_lock_path, __name__)


_recs_read_record = _measure_compat.bind(_measure_providers['cache_policy']._recs_read_record, __name__)


_recs_lock_acquire = _measure_compat.bind(_measure_providers['cache_policy']._recs_lock_acquire, __name__, defaults=(None,))


_recs_lock_release = _measure_compat.bind(_measure_providers['cache_policy']._recs_lock_release, __name__)


_recs_compact_item = _measure_compat.bind(_measure_providers['cache_policy']._recs_compact_item, __name__, defaults=(None,))


_recs_subagent_item = _measure_compat.bind(_measure_providers['cache_policy']._recs_subagent_item, __name__, defaults=(None,))


_recs_history_append = _measure_compat.bind(_measure_providers['cache_policy']._recs_history_append, __name__)


_recs_read_history = _measure_compat.bind(_measure_providers['cache_policy']._recs_read_history, __name__)


_recs_setting_matches = _measure_compat.bind(_measure_providers['cache_policy']._recs_setting_matches, __name__)


_recs_measure_improved = _measure_compat.bind(_measure_providers['cache_policy']._recs_measure_improved, __name__)


_recs_usd = _measure_compat.bind(_measure_providers['cache_policy']._recs_usd, __name__, defaults=(False,))


_recs_win_line = _measure_compat.bind(_measure_providers['cache_policy']._recs_win_line, __name__)


_recs_wins = _measure_compat.bind(_measure_providers['cache_policy']._recs_wins, __name__)


_recs_refresh_locked = _measure_compat.bind(_measure_providers['cache_policy']._recs_refresh_locked, __name__)


_recs_write_provisional = _measure_compat.bind(_measure_providers['cache_policy']._recs_write_provisional, __name__)


usage_recommendations_refresh = _measure_compat.bind(_measure_providers['cache_policy'].usage_recommendations_refresh, __name__, defaults=(None, None,))


_recs_spawn_refresh = _measure_compat.bind(_measure_providers['cache_policy']._recs_spawn_refresh, __name__, defaults=(None,))


_recs_ensure_fresh = _measure_compat.bind(_measure_providers['cache_policy']._recs_ensure_fresh, __name__, defaults=(None,))


_recs_block = _measure_compat.bind(_measure_providers['cache_policy']._recs_block, __name__, defaults=(None,))


_recs_item_line = _measure_compat.bind(_measure_providers['cache_policy']._recs_item_line, __name__)


_recs_covers = _measure_compat.bind(_measure_providers['cache_policy']._recs_covers, __name__)


_recs_cli = _measure_compat.bind(_measure_providers['cache_policy']._recs_cli, __name__)


_coach_cli = _measure_compat.bind(_measure_providers['cache_policy']._coach_cli, __name__)


_subagent_cache_cli = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_cli, __name__)


_subagent_cache_payoff_lines = _measure_compat.bind(_measure_providers['cache_policy']._subagent_cache_payoff_lines, __name__)


_dominant_turn_model = _measure_compat.bind(_measure_providers['pricing']._dominant_turn_model, __name__)


_detect_explicit_ttl = _measure_compat.bind(_measure_providers['pricing']._detect_explicit_ttl, __name__, defaults=(None, None,))


_detect_automatic_collapse = _measure_compat.bind(_measure_providers['pricing']._detect_automatic_collapse, __name__, defaults=(None, None,))


_gap_seconds = _measure_compat.bind(_measure_providers['pricing']._gap_seconds, __name__)


_cache_remedy_text = _measure_compat.bind(_measure_providers['pricing']._cache_remedy_text, __name__)


# Platforms whose per-turn cache+timestamp data the Python cache-report engine
# CANNOT read, with the honest reason. Filtered at call time by both the active
# runtime AND _CACHE_WIRED_RUNTIMES: a platform that becomes wired (added to
# _CACHE_WIRED_RUNTIMES + the dispatch) is automatically dropped from the gap
# list, so it can never be both measured and listed as a gap.
_CACHE_COVERAGE_GAP_REASONS = {
    "hermes": (
        "Hermes state.db stores only per-SESSION aggregate cache tokens "
        "(no per-turn cache+timestamp), and cache_read is documented "
        "unreliable — cache-TTL/collapse waste cannot be measured."
    ),
    "openclaw": (
        "OpenClaw is a TypeScript gateway with its own session store; the "
        "Python engine has no per-turn cache+timestamp read path into it."
    ),
    "opencode": (
        "OpenCode is a TypeScript plugin that persists only per-session cache "
        "aggregates to its own trends DB (no per-turn cache+timestamp series "
        "the Python engine can read)."
    ),
    "copilot": (
        "GitHub Copilot is credits-billed and exposes no per-turn cache "
        "detail (cached-token counts are not surfaced), so cache-TTL waste "
        "cannot be computed — only credit consumption is visible."
    ),
    "cursor": (
        "Cursor transcripts carry no usage fields and state.vscdb tokenCount "
        "is best-effort (staff: often zero, no cache split) — so cache-TTL "
        "waste cannot be measured, only a chars-over-four estimate is visible."
    ),
    "antigravity": (
        "Antigravity exposes per-generation cache_read totals in gen_metadata "
        "but no per-turn cache+timestamp series, so cache-TTL collapse waste "
        "cannot be measured."
    ),
    "grok": (
        "Grok Build's updates.jsonl turn_completed records expose per-turn "
        "cachedRead/cacheCreation tokens, but the Python cache-report engine "
        "has no per-turn cache+timestamp read path into $GROK_HOME/sessions/ "
        "— only aggregate token/cost reaches trends.db."
    ),
}


_cache_coverage_gaps = _measure_compat.bind(_measure_providers['pricing']._cache_coverage_gaps, __name__)


_session_mix_class_terms = _measure_compat.bind(_measure_providers['pricing']._session_mix_class_terms, __name__)


# Event classes whose stored cost is NOT an input-class rate and must never be
# re-anchored to a session's input mix. resume_lean prices at the cache-read /
# 1h cache-write rate of the avoided reload (v5.13.1 resume_lean rework); it is
# estimated-tier and never reaches the counted headline, so the reprice leaves
# its rows exactly as logged.
_REPRICE_SKIP_EVENT_TYPES = frozenset({"resume_lean"})

# Token class each event type's avoided tokens belong to. The default is fresh
# input. A measured verbosity reduction is output tokens (counted from the
# turns' output usage), so it is priced at the output rate.
# A keep-warm saving is the avoided 1h cache write of the prefix, so it follows the
# session model's 1h cache-write rate.
_EVENT_RATE_CLASS = {"verbosity_steer_measured": "output", "verbosity_steer": "output",
                     "keepwarm": "cache_create_1h"}

# One statement per (source, has model_usage_json, has all_model_usage_json, by_day,
# session-scoped). Grouping on column 1 is a no-op when it is the constant NULL.
_REPRICE_SQL = _sql_variants(
    "SELECT {day}, e.event_type, SUM(e.tokens_saved), SUM(e.cost_saved_usd), {mu}, {amu}, {mdl} "
    "FROM {table} e "
    "LEFT JOIN session_log s ON e.session_uuid = s.session_uuid "
    "WHERE e.timestamp >= ? {scope}"
    "GROUP BY 1, e.event_type, e.session_uuid, 7",
    table={"counted_savings_events": "counted_savings_events", "savings_events": "savings_events"},
    mu={True: "s.model_usage_json", False: "NULL"},
    amu={True: "s.all_model_usage_json", False: "NULL"},
    day={True: "substr(e.timestamp, 1, 10)", False: "NULL"},
    scope={True: "AND (e.session_uuid = ? OR (e.session_uuid IS NULL AND e.session_id = ?)) ", False: ""},
    mdl={True: "e.model", False: "NULL"},
)


_stored_model_is_unpriced = _measure_compat.bind(_measure_providers['pricing']._stored_model_is_unpriced, __name__)


_session_model_reprice = _measure_compat.bind(_measure_providers['pricing']._session_model_reprice, __name__, defaults=(None, False, None,))


# ---------------------------------------------------------------------------
# The cumulative COUNTED number for the "Counted to date" card.
# Method: context REMOVALS only, trajectory held fixed -- each removal's
# prevented re-reads are the deduped API turns from the event to the next REAL
# compact_boundary in its own session, priced at that turn's model cache-read
# rate. resume_lean (counterfactual lever) and opportunity/unapplied rows are
# excluded; the one-shot floor prices each removal once at the input rate of
# the first call after it. Pre-ledger months (before _COUNTED_LEDGER_ERA_START)
# are represented by the transcripts' own "Full result archived" markers so
# thin early logging cannot zero the period out.
# ---------------------------------------------------------------------------

# Removal event classes that ride the context until compaction -- METERED
# magnitudes only: the counted card may only
# compound removals whose token count was measured. Deliberately absent:
#   * resume_lean -- estimated tier; the avoided reload is a counterfactual;
#   * checkpoint_restore -- estimated tier; the credit is the working set the
#     resume WOULD have re-read, inferred from session reads, never observed;
#   * mcp_cap / hint_followed -- estimated-MAGNITUDE (fixed 5,000/event and
#     conservative-guess sizes; _get_savings_summary relocates both to the
#     estimated tier, so a counted annuity here would cross tiers);
#   * verbosity_* -- output trims: generated once, never re-read.
_COUNTED_SE_EVENT_TYPES = (
    "tool_archive", "tool_archive_reexpand", "tool_archive_refetch_block",
    "structure_map", "delta_read",
)
# compression_events features that are bookkeeping, not removals.
_COUNTED_CE_EXCLUDED_FEATURES = (
    "quality_nudge", "loop_detection", "cohort_demoted", "fresh_session_nudge",
    "cache_drop_warning", "first_read_edit_followup",
    "delta_read",  # read_cache emits both ledgers; savings_events owns this key
    "structure_map",  # same: logged to both ledgers, savings_events owns it
)
# Ledger events count from here; earlier months use transcript markers instead
# (the Mar-Apr ledger logged 4+13 events against 73+232 physical markers).
_COUNTED_LEDGER_ERA_START = "2026-05-01"
# Old-format markers carry no size; median of the 33 sized Mar-May markers.
_COUNTED_MARKER_MEDIAN_TOKENS = 3100
_COUNTED_MARKER_RX = re.compile(r"Full result archived(?: \(([0-9,]+) chars\))?")
# Per-collect-pass caps: the backlog drains over flushes (same philosophy as
# _COLLECT_MAX_PER_RUN); `counted-backfill` runs uncapped interactively.
_COUNTED_MAX_SESSIONS_PER_PASS = 8
_COUNTED_MARKER_FILES_PER_PASS = 120
_COUNTED_PASS_DEADLINE_SECONDS = 6.0
_COUNTED_META_MARKER_DONE = "counted_marker_backfill_done"
_COUNTED_META_MARKER_CURSOR = "counted_marker_backfill_cursor"


_counted_event_utc = _measure_compat.bind(_measure_providers['savings']._counted_event_utc, __name__)


_counted_walk_transcript = _measure_compat.bind(_measure_providers['savings']._counted_walk_transcript, __name__, defaults=(False,))


_COUNTED_MEDIA_BLOCK_TYPES = ("image", "document", "audio", "video")
_COUNTED_MEDIA_DIMS_RX = re.compile(r"\b(\d{2,5})x(\d{2,5})\b")
_COUNTED_MEDIA_TOOL_RX = re.compile(r"^replaced (\S+) \(")
# Seconds between an archive event and the tool result it replaced.
_COUNTED_MEDIA_MATCH_SECONDS = 10.0


_claude_image_tokens = _measure_compat.bind(_measure_providers['savings']._claude_image_tokens, __name__)


_counted_media_results = _measure_compat.bind(_measure_providers['savings']._counted_media_results, __name__)


_counted_media_net_tokens = _measure_compat.bind(_measure_providers['savings']._counted_media_net_tokens, __name__)


_counted_exact_card = _measure_compat.bind(_measure_providers['savings']._counted_exact_card, __name__, defaults=(None,))


_COUNTED_COVERAGE_KEYS = (
    "oneshot_unpriced_credit_tokens", "oneshot_unpriced_debit_tokens",
    "reread_unpriced_credit_tokens", "reread_unpriced_debit_tokens",
    "priced_oneshot_events", "priced_reread_turns",
    # Removals priced at the cache-write class the next call used, and removals
    # priced at the input rate as a minimum (events, and tokens in absolute value).
    "oneshot_class_events", "oneshot_floor_events", "oneshot_floor_tokens",
)


_counted_coverage_totals = _measure_compat.bind(_measure_providers['savings']._counted_coverage_totals, __name__)


_counted_pricing_note = _measure_compat.bind(_measure_providers['savings']._counted_pricing_note, __name__, defaults=(False,))


_counted_oneshot_rate = _measure_compat.bind(_measure_providers['savings']._counted_oneshot_rate, __name__)


_counted_score_detail = _measure_compat.bind(_measure_providers['savings']._counted_score_detail, __name__, defaults=(False,))


_counted_score_event = _measure_compat.bind(_measure_providers['savings']._counted_score_event, __name__)


_counted_window_summary = _measure_compat.bind(_measure_providers['savings']._counted_window_summary, __name__)


_counted_candidate_events = _measure_compat.bind(_measure_providers['savings']._counted_candidate_events, __name__)


_counted_session_path = _measure_compat.bind(_measure_providers['savings']._counted_session_path, __name__)


# Eligibility is per row. A global migration flag never certifies old prices.
_COUNTED_PRICING_VERSION = "4"
_COUNTED_META_PRICING = "counted_pricing_version"


_counted_pricing_detail = _measure_compat.bind(_measure_providers['savings']._counted_pricing_detail, __name__)


_ensure_counted_pricing_schema = _measure_compat.bind(_measure_providers['savings']._ensure_counted_pricing_schema, __name__)


_reprice_counted_rows_once = _measure_compat.bind(_measure_providers['savings']._reprice_counted_rows_once, __name__)


_counted_file_proof = _measure_compat.bind(_measure_providers['savings']._counted_file_proof, __name__, defaults=((),))


_counted_mark_incomplete = _measure_compat.bind(_measure_providers['savings']._counted_mark_incomplete, __name__, defaults=(None,))


_counted_legacy_append_continuous = _measure_compat.bind(_measure_providers['savings']._counted_legacy_append_continuous, __name__)


_counted_store_score = _measure_compat.bind(_measure_providers['savings']._counted_store_score, __name__, defaults=(None, "metered", False, None, False,))


_counted_stored_rows = _measure_compat.bind(_measure_providers['savings']._counted_stored_rows, __name__)


_update_counted_cumulative = _measure_compat.bind(_measure_providers['savings']._update_counted_cumulative, __name__, defaults=(_COUNTED_MAX_SESSIONS_PER_PASS, _COUNTED_PASS_DEADLINE_SECONDS, True,))


_counted_marker_backfill = _measure_compat.bind(_measure_providers['savings']._counted_marker_backfill, __name__, defaults=(_COUNTED_MARKER_FILES_PER_PASS, _COUNTED_PASS_DEADLINE_SECONDS, True,))


_purge_counted_without_transcripts = _measure_compat.bind(_measure_providers['savings']._purge_counted_without_transcripts, __name__)


_counted_backfill_all = _measure_compat.bind(_measure_providers['savings']._counted_backfill_all, __name__, defaults=(True,))


_counted_session_cache_mix = _measure_compat.bind(_measure_providers['savings']._counted_session_cache_mix, __name__, defaults=("cache_read",))


_counted_rollup_rows = _measure_compat.bind(_measure_providers['savings']._counted_rollup_rows, __name__, defaults=(None,))


_counted_row_awaits_rescore = _measure_compat.bind(_measure_providers['savings']._counted_row_awaits_rescore, __name__)


_counted_cumulative_summary = _measure_compat.bind(_measure_providers['savings']._counted_cumulative_summary, __name__, defaults=(None, None,))


_dashboard_savings_data = _measure_compat.bind(_measure_providers['savings']._dashboard_savings_data, __name__, defaults=(30, False, False,))



_net_realized_savings = _measure_compat.bind(_measure_providers['savings']._net_realized_savings, __name__)


_get_savings_summary = _measure_compat.bind(_measure_providers['savings']._get_savings_summary, __name__, defaults=(30, None,))


_is_file_collected = _measure_compat.bind(_measure_providers['sessions']._is_file_collected, __name__, defaults=(False,))


_adapter_activity_ts = _measure_compat.bind(_measure_providers['sessions']._adapter_activity_ts, __name__)


_store_adapter_activity = _measure_compat.bind(_measure_providers['sessions']._store_adapter_activity, __name__)


_insert_normalized_session = _measure_compat.bind(_measure_providers['sessions']._insert_normalized_session, __name__, defaults=(False,))


_safe_json_dict = _measure_compat.bind(_measure_providers['sessions']._safe_json_dict, __name__)


_rebuild_aggregate_tables = _measure_compat.bind(_measure_providers['sessions']._rebuild_aggregate_tables, __name__)


_needs_model_daily_rebuild = _measure_compat.bind(_measure_providers['sessions']._needs_model_daily_rebuild, __name__)


_needs_streaming_dedup_rebuild = _measure_compat.bind(_measure_providers['sessions']._needs_streaming_dedup_rebuild, __name__)


_migrate_model_daily = _measure_compat.bind(_measure_providers['sessions']._migrate_model_daily, __name__, defaults=(False,))


_migrate_streaming_dedup = _measure_compat.bind(_measure_providers['sessions']._migrate_streaming_dedup, __name__, defaults=(False,))


_hermes_refresh_session_totals = _measure_compat.bind(_measure_providers['sessions']._hermes_refresh_session_totals, __name__)


_collect_hermes_sessions = _measure_compat.bind(_measure_providers['sessions']._collect_hermes_sessions, __name__, defaults=(90, False, False,))


_HERMES_SKILLS_MAX = 200


_parse_hermes_rollup_skills = _measure_compat.bind(_measure_providers['sessions']._parse_hermes_rollup_skills, __name__)


_parse_hermes_rollup_context = _measure_compat.bind(_measure_providers['sessions']._parse_hermes_rollup_context, __name__)


# Private scratch dir for the short-lived transcript a Hermes checkpoint is
# built from. Files are removed right after use; stragglers from a killed run
# are swept after a day.
_HERMES_TRANSCRIPT_STALE_SECONDS = 24 * 3600


_hermes_transcript_dir = _measure_compat.bind(_measure_providers['sessions']._hermes_transcript_dir, __name__)


_sweep_hermes_transcripts = _measure_compat.bind(_measure_providers['sessions']._sweep_hermes_transcripts, __name__, defaults=(None,))


hermes_checkpoint = _measure_compat.bind(_measure_providers['sessions'].hermes_checkpoint, __name__, defaults=("stop", None, None,))


_HERMES_PROFILE_HELP = (
    "\nProfiles: reads and writes the Hermes home in HERMES_HOME (default ~/.hermes).\n"
    "For a profile: HERMES_HOME=~/.hermes/profiles/<name> measure.py <command>"
)
_HERMES_SUBCOMMAND_HELP = {
    "hermes-rollup": (
        "usage: measure.py hermes-rollup [--quiet] [--session ID] [--platform P] [--reason R]\n"
        "                                [--context-tokens N] [--skills-json JSON]\n\n"
        "Ingest the last 90 days of Hermes sessions from state.db into trends.db.\n"
        "Run by the Hermes plugin at session end; safe to run by hand (idempotent).\n"
        "--session, --platform and --reason are accepted for the plugin's call shape;\n"
        "every recent session is collected either way." + _HERMES_PROFILE_HELP
    ),
    "hermes-checkpoint": (
        "usage: measure.py hermes-checkpoint --session ID [--trigger T] [--fill-pct N]\n"
        "                                    [--cwd DIR] [--quiet]\n\n"
        "Write a checkpoint for a Hermes session from its state.db messages." + _HERMES_PROFILE_HELP
    ),
    "hermes-turn-context": (
        "usage: measure.py hermes-turn-context < payload.json\n\n"
        "Per-turn context for the Hermes plugin (continuity pointer, prior-session hint,\n"
        "lean-output nudge). Reads one JSON payload on stdin, prints one JSON object." + _HERMES_PROFILE_HELP
    ),
    "hermes-summary": (
        "usage: measure.py hermes-summary [--days N] [--quiet] [--session ID]\n\n"
        "Ingest recent Hermes sessions, then print usage trends." + _HERMES_PROFILE_HELP
    ),
}


_hermes_checkpoint_cli = _measure_compat.bind(_measure_providers['sessions']._hermes_checkpoint_cli, __name__)


hermes_turn_context = _measure_compat.bind(_measure_providers['sessions'].hermes_turn_context, __name__)


_resolve_copilot_home_wsl_aware = _measure_compat.bind(_measure_providers['sessions']._resolve_copilot_home_wsl_aware, __name__, defaults=(None,))


_write_copilot_restore_context = _measure_compat.bind(_measure_providers['sessions']._write_copilot_restore_context, __name__, defaults=(False,))


_grok_summary = _measure_compat.bind(_measure_providers['sessions']._grok_summary, __name__)


_collect_grok_sessions = _measure_compat.bind(_measure_providers['sessions']._collect_grok_sessions, __name__, defaults=(90, False, False,))


_copilot_summary = _measure_compat.bind(_measure_providers['sessions']._copilot_summary, __name__)


_collect_copilot_sessions = _measure_compat.bind(_measure_providers['sessions']._collect_copilot_sessions, __name__, defaults=(90, False, False,))


_write_cursor_restore_context = _measure_compat.bind(_measure_providers['sessions']._write_cursor_restore_context, __name__, defaults=(False,))


_cursor_summary = _measure_compat.bind(_measure_providers['sessions']._cursor_summary, __name__)


_collect_cursor_sessions = _measure_compat.bind(_measure_providers['sessions']._collect_cursor_sessions, __name__, defaults=(90, False, False,))


_antigravity_restore_clean = _measure_compat.bind(_measure_providers['sessions']._antigravity_restore_clean, __name__)


_write_antigravity_restore_context = _measure_compat.bind(_measure_providers['sessions']._write_antigravity_restore_context, __name__, defaults=(False,))


_price_antigravity_session = _measure_compat.bind(_measure_providers['sessions']._price_antigravity_session, __name__)


_antigravity_summary = _measure_compat.bind(_measure_providers['sessions']._antigravity_summary, __name__)


_collect_antigravity_sessions = _measure_compat.bind(_measure_providers['sessions']._collect_antigravity_sessions, __name__, defaults=(90, False, False,))


UNDATED_USAGE_KEY = "undated"


_merge_daily_usage = _measure_compat.bind(_measure_providers['sessions']._merge_daily_usage, __name__)


_session_merged_requests = _measure_compat.bind(_measure_providers['sessions']._session_merged_requests, __name__)


# Bump when the way usage is attributed to a session changes. Rows stored by an
# older version are re-parsed from their transcript the next time collect runs.
_USAGE_PARSER_VERSION = 3


_stable_hash63 = _measure_compat.bind(_measure_providers['sessions']._stable_hash63, __name__)


_request_base_key = _measure_compat.bind(_measure_providers['sessions']._request_base_key, __name__)


_usage_owner_id = _measure_compat.bind(_measure_providers['sessions']._usage_owner_id, __name__)


_live_usage_owners = _measure_compat.bind(_measure_providers['sessions']._live_usage_owners, __name__)


_SHARE_FIELDS = ("inp", "out", "cr", "cc", "cc_1h", "cc_5m")


_empty_share = _measure_compat.bind(_measure_providers['sessions']._empty_share, __name__)


_claim_state = _measure_compat.bind(_measure_providers['sessions']._claim_state, __name__)


_net_of_counted = _measure_compat.bind(_measure_providers['sessions']._net_of_counted, __name__, defaults=(None,))


_retained_requests = _measure_compat.bind(_measure_providers['sessions']._retained_requests, __name__)


_claim_requests = _measure_compat.bind(_measure_providers['sessions']._claim_requests, __name__)


_net_and_record = _measure_compat.bind(_measure_providers['sessions']._net_and_record, __name__)


_parsed_with_requests = _measure_compat.bind(_measure_providers['sessions']._parsed_with_requests, __name__)


_parsed_net_of_counted = _measure_compat.bind(_measure_providers['sessions']._parsed_net_of_counted, __name__, defaults=(None,))


_parsed_net_of_ledger = _measure_compat.bind(_measure_providers['sessions']._parsed_net_of_ledger, __name__)


_stale_usage_row_paths = _measure_compat.bind(_measure_providers['sessions']._stale_usage_row_paths, __name__)


_stamp_usage_parser_version = _measure_compat.bind(_measure_providers['sessions']._stamp_usage_parser_version, __name__)


_daily_usage_from_merged = _measure_compat.bind(_measure_providers['sessions']._daily_usage_from_merged, __name__)


_session_daily_metrics = _measure_compat.bind(_measure_providers['sessions']._session_daily_metrics, __name__)


_session_daily_usage = _measure_compat.bind(_measure_providers['sessions']._session_daily_usage, __name__, defaults=(None, None, None, None,))


_session_activity_evidence = _measure_compat.bind(_measure_providers['sessions']._session_activity_evidence, __name__, defaults=(None, None, None, None,))


_session_rank = _measure_compat.bind(_measure_providers['sessions']._session_rank, __name__)


_SESSION_DIR_INDEX = {}
_SESSION_DIR_INDEX_TTL = 60.0


_session_dir_index = _measure_compat.bind(_measure_providers['sessions']._session_dir_index, __name__)


_session_copy_files = _measure_compat.bind(_measure_providers['sessions']._session_copy_files, __name__)


_session_subagent_files = _measure_compat.bind(_measure_providers['sessions']._session_subagent_files, __name__)


_canonical_session_uuid = _measure_compat.bind(_measure_providers['sessions']._canonical_session_uuid, __name__)


_dedupe_session_rows = _measure_compat.bind(_measure_providers['sessions']._dedupe_session_rows, __name__)


# Per-pass budgets for collect_sessions, by drain mode:
#   None     hook path: the per-pass caps every flush has always used.
#   "inline" a user-run collect (dashboard skill, terminal): no session-count
#            caps, each stage stopped by a wall-clock budget instead.
#   "full"   the detached upgrade drain: no caps at all, commits often so
#            hook collects running at the same time are never locked out.
_COLLECT_DRAIN_BUDGETS = {
    None: {"count_capped": True, "parse_seconds": None, "commit_batch": None,
           "daily_seconds": 10.0, "counted_sessions": _COUNTED_MAX_SESSIONS_PER_PASS,
           "counted_seconds": _COUNTED_PASS_DEADLINE_SECONDS},
    "inline": {"count_capped": False, "parse_seconds": 15.0, "commit_batch": 20,
               "daily_seconds": 15.0, "counted_sessions": 10**9, "counted_seconds": 30.0},
    "full": {"count_capped": False, "parse_seconds": None, "commit_batch": 20,
             "daily_seconds": 3600.0, "counted_sessions": 10**9, "counted_seconds": None},
}
_UPGRADE_DRAIN_META = "upgrade_drain_done"
_UPGRADE_DRAIN_ATTEMPTS_META = "upgrade_drain_attempts"
_UPGRADE_DRAIN_MAX_ATTEMPTS = 3
_UPGRADE_DRAIN_LOCK_NAME = "upgrade-drain.lock"
# A drain re-parses the whole history once; a lock older than this belongs to
# a child that died, so the next pass may start another.
_UPGRADE_DRAIN_LOCK_STALE_SECONDS = 1800


_collect_drain_mode = _measure_compat.bind(_measure_providers['sessions']._collect_drain_mode, __name__)


_upgrade_drain_key = _measure_compat.bind(_measure_providers['sessions']._upgrade_drain_key, __name__)


_upgrade_meta_get = _measure_compat.bind(_measure_providers['sessions']._upgrade_meta_get, __name__)


_upgrade_meta_set = _measure_compat.bind(_measure_providers['sessions']._upgrade_meta_set, __name__)


_upgrade_drain_mark_done = _measure_compat.bind(_measure_providers['sessions']._upgrade_drain_mark_done, __name__)


_upgrade_drain_after_pass = _measure_compat.bind(_measure_providers['sessions']._upgrade_drain_after_pass, __name__)


_upgrade_drain_claim_lock = _measure_compat.bind(_measure_providers['sessions']._upgrade_drain_claim_lock, __name__)


_upgrade_drain_release_lock = _measure_compat.bind(_measure_providers['sessions']._upgrade_drain_release_lock, __name__)


_spawn_upgrade_drain = _measure_compat.bind(_measure_providers['sessions']._spawn_upgrade_drain, __name__)


_upgrade_drain_days = _measure_compat.bind(_measure_providers['sessions']._upgrade_drain_days, __name__)


_run_upgrade_drain = _measure_compat.bind(_measure_providers['sessions']._run_upgrade_drain, __name__, defaults=(None, True,))


collect_sessions = _measure_compat.bind(_measure_providers['sessions'].collect_sessions, __name__, defaults=(90, False, False,))


conn_total_sessions = _measure_compat.bind(_measure_providers['sessions'].conn_total_sessions, __name__)


_read_path_upkeep = _measure_compat.bind(_measure_providers['sessions']._read_path_upkeep, __name__)


_session_price_buckets = _measure_compat.bind(_measure_providers['sessions']._session_price_buckets, __name__)


_price_session_row = _measure_compat.bind(_measure_providers['sessions']._price_session_row, __name__)


# ---------------------------------------------------------------------------
# session-cost: "what did the Garden session yesterday cost?" answered from
# trends.db, priced by _price_session_row (the dashboard's own number). Strictly
# read-only: the DB is opened mode=ro (WAL readers never block a writer) and,
# only if that open fails, immutable=1. Nothing here creates, migrates or
# writes a file.
# ---------------------------------------------------------------------------

# Filler words a plain question carries ("what did the ... session cost").
# Dropped before matching so passing the user's own words still works.
_SESSION_COST_STOPWORDS = frozenset("""
a an and are at be by cost costs costed did do does for from how i in is it
its me much my of on or price priced pricing session sessions spend spent
that the this to was were what which with
""".split())
_SESSION_COST_ID_RX = re.compile(r"^[0-9a-f]{8}[0-9a-f-]*$", re.IGNORECASE)
_SESSION_COST_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SESSION_COST_COLS = (
    "id, jsonl_path, date, project, duration_minutes, input_tokens, output_tokens, "
    "message_count, api_calls, cache_hit_rate, cache_create_1h_tokens, "
    "cache_create_5m_tokens, model_usage_json, model_usage_breakdown_json, "
    "daily_usage_json, slug, topic, session_uuid, is_sidechain, subagents_json, "
    "collected_at, origin, platform"
)
SESSION_COST_BASIS = (
    "API-equivalent list price ({tier}). On a subscription plan this is what the "
    "work would have cost at API rates, not a bill."
)


_session_cost_norm = _measure_compat.bind(_measure_providers['savings']._session_cost_norm, __name__)


_session_cost_resolve_date = _measure_compat.bind(_measure_providers['savings']._session_cost_resolve_date, __name__, defaults=(None,))


_session_cost_parse_args = _measure_compat.bind(_measure_providers['savings']._session_cost_parse_args, __name__)


_session_cost_open_db = _measure_compat.bind(_measure_providers['savings']._session_cost_open_db, __name__)


_session_cost_haystack = _measure_compat.bind(_measure_providers['savings']._session_cost_haystack, __name__)


_session_cost_word_hits = _measure_compat.bind(_measure_providers['savings']._session_cost_word_hits, __name__)


_session_cost_row_id = _measure_compat.bind(_measure_providers['savings']._session_cost_row_id, __name__)


_session_cost_components = _measure_compat.bind(_measure_providers['savings']._session_cost_components, __name__)


_session_cost_entry = _measure_compat.bind(_measure_providers['savings']._session_cost_entry, __name__, defaults=(None, None,))


_session_cost_uncollected = _measure_compat.bind(_measure_providers['savings']._session_cost_uncollected, __name__)


session_cost_query = _measure_compat.bind(_measure_providers['savings'].session_cost_query, __name__)


_fmt_session_cost_tokens = _measure_compat.bind(_measure_providers['savings']._fmt_session_cost_tokens, __name__)


_fmt_session_cost_duration = _measure_compat.bind(_measure_providers['savings']._fmt_session_cost_duration, __name__)


_usd = _measure_compat.bind(_measure_providers['savings']._usd, __name__)


_print_session_cost_entry = _measure_compat.bind(_measure_providers['savings']._print_session_cost_entry, __name__, defaults=(None,))


session_cost_main = _measure_compat.bind(_measure_providers['savings'].session_cost_main, __name__)


def _collect_trends_from_db(days=30):
    """Query SQLite trends DB for aggregated usage data.

    Returns same dict shape as _collect_trends_from_jsonl, or None if DB
    doesn't exist or has no data for the requested period.
    """
    if not TRENDS_DB.exists():
        return None

    try:
        conn = _init_trends_db()
        conn.row_factory = sqlite3.Row
        # Verify it's a valid DB before proceeding
        conn.execute("SELECT 1 FROM session_log LIMIT 1")
    except (sqlite3.Error, sqlite3.DatabaseError):
        try:
            conn.close()
        except Exception:
            pass
        return None

    try:
        _read_path_upkeep(conn, days)
        return _query_trends_db(conn, days)
    except (sqlite3.Error, sqlite3.DatabaseError):
        return None
    finally:
        conn.close()


def _activity_window_predicate(conn, cutoff):
    """SQL fragment + params limiting session_log scans to rows that can hold
    activity inside the window, or ("", ()) when the activity bounds columns
    are unavailable (pre-migration shapes), keeping the unfiltered scan.

    last_activity_day is maintained by every daily_usage_json writer (collect,
    _backfill_daily_usage, _store_adapter_activity, repair_activity_dates and
    _collect_trends_from_jsonl). Bounds cover the union of combined usage,
    parent usage and metadata days. NULL bounds keep rows with no dated evidence.
    The fragment is a superset of "has an in-window slice", never a subset:
    metrics-only days can only widen last_activity_day, so filtering on it can
    keep extra rows but can never drop a row that contributes window slices."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(session_log)")}
    if "last_activity_day" not in columns:
        return "", ()
    return " AND (last_activity_day IS NULL OR last_activity_day >= ?)", (cutoff,)


def _query_trends_db(conn, days):
    """Internal: run all queries against the trends DB. Caller handles errors."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    ensure_activity_view(conn)
    window_pred, window_params = _activity_window_predicate(conn, cutoff)
    population_metrics = _query_dashboard_population_metrics(conn, days)
    spent_basis = _dashboard_spent_token_basis(conn, days=days)
    coverage = activity_coverage(conn)

    # Basic stats
    # v5.4.9: "Total tokens" matches Claude Code Desktop methodology exactly:
    # fresh_input + output. Both cache_read and cache_create are excluded from
    # the headline since Desktop's "X in · Y out" per-model view uses only
    # those two fields. session_log.input_tokens stores total_full_input
    # (fresh + cache_read + cache_create). We derive fresh by subtracting both
    # cache categories using stored fields:
    #   fresh = input_tokens * (1 - cache_hit_rate) - cache_create_*
    #   where input_tokens * (1 - cache_hit_rate) = fresh + cache_create
    # cache_create_1h/5m are stored separately in session_log since v5.3.5.
    # v5.4.9: per-session clamping for fresh_input. Aggregate-level subtraction
    # lets sessions where cache_create_stored > (input_tokens * (1 - chr)) eat
    # positive contributions from other sessions due to rounding drift between
    # cache_hit_rate (float) and cache_create_* (int). Clamp at per-row level.
    row = conn.execute(
        """SELECT COUNT(DISTINCT id) as cnt,
                  COALESCE(SUM(duration_minutes), 0) as total_dur,
                  COALESCE(SUM(input_tokens), 0) as total_in,
                  COALESCE(SUM(output_tokens), 0) as total_out,
                  COALESCE(SUM(message_count), 0) as total_msgs,
                  COALESCE(SUM(
                    COALESCE(cache_create_1h_tokens, 0)
                    + COALESCE(cache_create_5m_tokens, 0)
                  ), 0) as total_cache_create
           FROM session_activity WHERE date >= ?""", (cutoff,)
    ).fetchone()

    # Per-session fresh_input with non-negative clamp (see comment above).
    fresh_rows = conn.execute(
        """SELECT input_tokens * (1.0 - COALESCE(cache_hit_rate, 0)) as bp_c,
                  COALESCE(cache_create_1h_tokens, 0) + COALESCE(cache_create_5m_tokens, 0) as cc
           FROM session_activity WHERE date >= ?""", (cutoff,)
    ).fetchall()
    _total_fresh_clamped = sum(max(0, (r["bp_c"] or 0) - (r["cc"] or 0)) for r in fresh_rows)

    session_count = row["cnt"]
    if session_count == 0 and coverage["complete"]:
        return None

    total_duration = row["total_dur"]
    total_input = row["total_in"]
    total_output = row["total_out"]
    total_messages = row["total_msgs"]
    total_fresh_input = int(_total_fresh_clamped)
    total_cache_create = int(row["total_cache_create"])
    total_cache_read = max(0, int(total_input) - total_fresh_input - total_cache_create)
    # Billable tokens are retained as a separate diagnostic, not used for the
    # official-compatible spent-token headline. Cache writes are real billing
    # cost, but including them in the headline makes the spent basis diverge
    # from the provider's fresh-input plus streamed-output stats.
    total_billable_tokens = total_fresh_input + total_cache_create + total_output

    # Skill usage
    skill_rows = conn.execute(
        """SELECT skill, SUM(session_count) as sess, SUM(invocations) as inv
           FROM skill_daily WHERE date >= ? GROUP BY skill ORDER BY sess DESC""",
        (cutoff,),
    ).fetchall()
    skill_sessions = {r["skill"]: r["sess"] for r in skill_rows}

    # Model mix
    model_rows = conn.execute(
        """SELECT model, SUM(total_tokens) as tot
           FROM model_daily WHERE date >= ? GROUP BY model ORDER BY tot DESC""",
        (cutoff,),
    ).fetchall()
    billable_model_mix = {r["model"]: r["tot"] for r in model_rows}
    reported_model_mix = {}
    reported_combined_rows = 0
    for parent_raw, combined_raw in conn.execute(
            "SELECT parent_daily_usage_json,daily_usage_json FROM session_log "
            "WHERE COALESCE(is_sidechain,0)=0" + window_pred, window_params):
        raw, provenance = _spent_daily_usage(parent_raw, combined_raw)
        usage = _safe_json_dict(raw)
        in_window = False
        for day in _activity_days(usage):
            if day < cutoff:
                continue
            in_window = True
            for model, bucket in usage[day].items():
                normalized = _normalize_model_name(model)
                if normalized and isinstance(bucket, dict):
                    reported_model_mix[normalized] = reported_model_mix.get(normalized, 0) + _bucket_int(bucket.get("fresh_input")) + _bucket_int(bucket.get("output"))
        if provenance == "combined" and in_window:
            reported_combined_rows += 1
    model_mix = reported_model_mix or billable_model_mix

    # Subagents
    sub_rows = conn.execute(
        """SELECT agent_type, SUM(spawn_count) as tot
           FROM subagent_daily WHERE date >= ? GROUP BY agent_type ORDER BY tot DESC""",
        (cutoff,),
    ).fetchall()
    subagents = {r["agent_type"]: r["tot"] for r in sub_rows}

    # Tool calls (aggregate from session_log JSON)
    total_tools = {}
    tool_rows = conn.execute(
        "SELECT tool_calls_json FROM session_activity WHERE date >= ? AND tool_calls_json IS NOT NULL",
        (cutoff,),
    ).fetchall()
    for tr in tool_rows:
        try:
            calls = json.loads(tr["tool_calls_json"])
            for tool, count in calls.items():
                total_tools[tool] = total_tools.get(tool, 0) + count
        except (json.JSONDecodeError, TypeError):
            pass
    total_tools = dict(sorted(total_tools.items(), key=lambda x: -x[1]))

    # Installed skills vs used (normalize names: usage logs use SKILL.md name, install list uses dir name)
    components = measure_components()
    installed_skills = set(components.get("skills", {}).get("names", []))
    name_to_dir = components.get("skills", {}).get("name_to_dir", {})
    used_skills_raw = set(skill_sessions.keys())
    # Map used skill names to directory names where possible.
    # Handles: exact match, SKILL.md name mapping, and namespaced sub-skills
    # (e.g., "compound-engineering:ce-brainstorm" counts "compound-engineering" as used).
    used_skills = set()
    for s in used_skills_raw:
        if s in installed_skills:
            used_skills.add(s)
        elif s in name_to_dir:
            used_skills.add(name_to_dir[s])
        elif ":" in s:
            parent = s.split(":")[0]
            if parent in installed_skills:
                used_skills.add(parent)
            elif parent in name_to_dir:
                used_skills.add(name_to_dir[parent])
            else:
                used_skills.add(s)
        else:
            used_skills.add(s)
    never_used = installed_skills - used_skills
    never_used_overhead = len(never_used) * TOKENS_PER_SKILL_APPROX

    # Trajectory
    snapshots = _load_overhead_snapshots()
    current_total = calculate_totals(components).get("estimated_total", 0)

    # Daily breakdown from session_log
    pricing_tier = _load_pricing_tier()
    daily = {}
    total_cost_usd = 0.0
    total_cost_priced_tokens = 0
    total_cost_unpriced_tokens = 0
    total_unpriced_sessions = 0
    session_rows = conn.execute(
        """SELECT date, jsonl_path, duration_minutes, input_tokens, output_tokens,
                  message_count, api_calls, cache_hit_rate,
                  cache_create_1h_tokens, cache_create_5m_tokens,
                  avg_call_gap_seconds, max_call_gap_seconds, p95_call_gap_seconds, skills_json,
                  subagents_json, model_usage_json, slug, topic, project,
                  model_usage_breakdown_json, daily_usage_json, parent_daily_usage_json,
                  quality_score, quality_grade
           FROM session_log WHERE 1=1""" + window_pred + """ ORDER BY date DESC""",
        window_params,
    ).fetchall()

    def _day_entry(date):
        if date not in daily:
            daily[date] = {
                "date": date,
                "sessions": 0,
                "total_input": 0,
                "total_output": 0,
                "skills_used": {},
                "session_details": [],
            }
        return daily[date]

    for sr in session_rows:
        try:
            skills = json.loads(sr["skills_json"]) if sr["skills_json"] else {}
        except (json.JSONDecodeError, TypeError):
            skills = {}
        try:
            subagents_meta = json.loads(sr["subagents_json"]) if sr["subagents_json"] else {}
        except (json.JSONDecodeError, TypeError):
            subagents_meta = {}

        # One pricing path for every per-session dollar figure (the dashboard
        # here, `session-cost` for plain-language questions): _price_session_row.
        inp_total = sr["input_tokens"] or 0
        out_total = sr["output_tokens"] or 0
        chr_val = sr["cache_hit_rate"] or 0
        cache_create_1h = sr["cache_create_1h_tokens"] or 0
        cache_create_5m = sr["cache_create_5m_tokens"] or 0
        priced_row = _price_session_row(sr, pricing_tier)
        mu = priced_row["model_usage"]
        dom_model = priced_row["dom_model"]
        du = priced_row["daily_usage"]
        session_total_cost = priced_row["total_cost"]
        # A day before the window is dropped.
        slices = [sl for sl in priced_row["slices"] if sl[0] >= cutoff]

        window_unpriced = 0
        jsonl_path = sr["jsonl_path"]
        # A session active on several days is listed on each; its latest day is
        # the primary entry. Per-session stats (skills, cache/TTL mix, coaching)
        # must read only primary entries or a multi-day session counts twice.
        primary_date = max((sl[0] for sl in slices), default=None)
        for date, day_cost, day_priced, day_unpriced, day_in, day_out in slices:
            d = _day_entry(date)
            d["sessions"] += 1
            d["total_input"] += day_in
            d["total_output"] += day_out
            if date == primary_date:
                for skill, cnt in skills.items():
                    d["skills_used"][skill] = d["skills_used"].get(skill, 0) + cnt
            total_cost_usd += day_cost
            total_cost_priced_tokens += day_priced
            total_cost_unpriced_tokens += day_unpriced
            window_unpriced += day_unpriced

            sd = {
                "metadata_basis": "lifetime session metadata; day_input/output and cost describe activity",
                "duration_minutes": round(sr["duration_minutes"] or 0, 1),
                "input_tokens": inp_total,
                "output_tokens": out_total,
                "message_count": sr["message_count"] or 0,
                "api_calls": sr["api_calls"] or 0,
                "skills": list(skills.keys()),
                "subagents": list(subagents_meta.keys()),
                "cache_hit_rate": round(chr_val, 3),
                "cache_create_1h_tokens": cache_create_1h,
                "cache_create_5m_tokens": cache_create_5m,
                "avg_call_gap_seconds": sr["avg_call_gap_seconds"],
                "max_call_gap_seconds": sr["max_call_gap_seconds"],
                "p95_call_gap_seconds": sr["p95_call_gap_seconds"],
                "slug": sr["slug"],
                "session_key": _make_session_key(jsonl_path),
                "jsonl_path": jsonl_path,
                "topic": sr["topic"],
                "project": _clean_project_name(sr["project"]),
                # cost_usd is THIS day's share; session_cost_usd is the whole session.
                "cost_usd": round(day_cost, 4),
                "session_cost_usd": round(session_total_cost, 4),
                # input/output_tokens above are whole-session totals (the row
                # describes the session); these are this day's share, the same
                # numbers the day's total_input/total_output add up.
                "day_input_tokens": day_in,
                "day_output_tokens": day_out,
                "spans_days": len(du) > 1,
                "continuation": date != primary_date,
                "cost_priced_tokens": day_priced,
                "cost_unpriced_tokens": day_unpriced,
                "model": _normalize_model_name(dom_model) or dom_model,
                "model_count": len(mu) if mu else 1,
            }
            # Prefer stored quality score (persisted during collect), fall back to recomputation
            if sr["quality_score"] is not None:
                sd["quality_score"] = sr["quality_score"]
                sd["quality_grade"] = sr["quality_grade"] or score_to_grade(round(sr["quality_score"]))
                sd["quality_band"] = score_to_band(sr["quality_score"])
            else:
                sq = score_session_quality(sd)
                sd["quality_score"] = sq["score"]
                sd["quality_grade"] = sq["grade"]
                sd["quality_band"] = sq["band"]
            d["session_details"].append(sd)
        if window_unpriced > 0:
            total_unpriced_sessions += 1

    daily_sorted = sorted(daily.values(), key=lambda x: x["date"], reverse=True)
    grade_rank = {grade: idx for idx, grade in enumerate(["F", "D", "C", "B", "A", "S"])}
    for d in daily_sorted:
        details = d.get("session_details", [])
        d["total_cost_usd"] = round(sum(float(sd.get("cost_usd") or 0.0) for sd in details), 4)
        d["cost_priced_tokens"] = sum(int(sd.get("cost_priced_tokens") or 0) for sd in details)
        d["cost_unpriced_tokens"] = sum(int(sd.get("cost_unpriced_tokens") or 0) for sd in details)
        scores = [float(sd["quality_score"]) for sd in details if sd.get("quality_score") is not None]
        if scores:
            d["avg_quality_score"] = round(sum(scores) / len(scores), 1)
        grades = [sd.get("quality_grade") for sd in details if sd.get("quality_grade")]
        if grades:
            d["worst_grade"] = min(grades, key=lambda grade: grade_rank.get(grade, 999))

    # Rolling quality trend from session_log
    quality_trend_rows = conn.execute(
        """SELECT date,
                  AVG(quality_score) as avg_q,
                  MIN(quality_score) as min_q,
                  MAX(quality_score) as max_q,
                  COUNT(*) as n
           FROM session_log
           WHERE date >= ? AND quality_score IS NOT NULL
           GROUP BY date ORDER BY date""",
        (cutoff,),
    ).fetchall()
    quality_trend = [
        {
            "date": r["date"],
            "avg_quality": round(r["avg_q"], 1),
            "min_quality": round(r["min_q"], 1),
            "max_quality": round(r["max_q"], 1),
            "sessions": r["n"],
        }
        for r in quality_trend_rows
    ]

    # conn.close() removed — caller (_collect_trends_from_db) owns the connection
    # and closes it in its finally block (double-close fix).

    # Pricing tier info for dashboard
    pricing_tier = _load_pricing_tier()
    tier_label = _pricing_tier_label(pricing_tier)

    return {
        "period_days": days,
        "session_count": session_count,
        "total_input_tokens": total_input,           # raw (includes cache reads + cache creates)
        "total_output_tokens": total_output,
        "total_fresh_input": total_fresh_input,      # v5.4.9: billable fresh input (Desktop-parity)
        "total_cache_read": total_cache_read,
        "total_cache_create": total_cache_create,    # separate (bills at 1.25x fresh)
        "total_tokens": spent_basis["tokens"],
        "total_billable_tokens": total_billable_tokens,
        "spent_token_basis": spent_basis,
        "activity_coverage": coverage,
        "total_tokens_raw": total_input + total_output,  # includes cache, for debugging
        "total_messages": total_messages,            # Desktop-parity headline
        "avg_duration_minutes": round(total_duration / session_count, 1) if session_count else 0,
        "avg_input_tokens": round(total_input / session_count) if session_count else 0,
        "avg_output_tokens": round(total_output / session_count) if session_count else 0,
        "skills": {
            "used": dict(sorted(skill_sessions.items(), key=lambda x: -x[1])),
            "installed_count": len(installed_skills),
            "never_used": sorted(never_used),
            "never_used_overhead": never_used_overhead,
        },
        "subagents": subagents,
        "model_mix": model_mix,
        "billable_model_mix": billable_model_mix,
        "model_mix_combined_fallback_rows": reported_combined_rows,
        "tool_calls": total_tools,
        "trajectory": {
            "snapshots": snapshots,
            "current_total": current_total,
        },
        "daily": daily_sorted,
        "total_cost_usd": round(total_cost_usd, 4),
        "cost_priced_tokens": total_cost_priced_tokens,
        "cost_unpriced_tokens": total_cost_unpriced_tokens,
        "cost_unpriced_sessions": total_unpriced_sessions,
        "cost_coverage_pct": round(100 * total_cost_priced_tokens / max(total_cost_priced_tokens + total_cost_unpriced_tokens, 1), 1),
        "cost_note": "Costs exclude sessions whose logs do not expose a recognized model id." if total_cost_unpriced_tokens else None,
        "quality_trend": quality_trend,
        "pricing_tier": pricing_tier,
        "pricing_tier_label": tier_label,
        "source": "sqlite",
        "population_metrics": population_metrics,
    }


def _query_dashboard_population_metrics(conn, days):
    """Return explicitly labelled dashboard populations from session_log."""
    ensure_activity_view(conn)
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    row = conn.execute(
        """SELECT COUNT(*) AS all_n,
                  SUM(CASE WHEN COALESCE(is_sidechain,0)=0 THEN 1 ELSE 0 END) AS main_n,
                  SUM(CASE WHEN COALESCE(is_sidechain,0)=1 THEN 1 ELSE 0 END) AS delegated_n,
                  SUM(CASE WHEN COALESCE(is_sidechain,0)=0 AND COALESCE(message_count,0)>=20 THEN 1 ELSE 0 END) AS eligible_n,
                  SUM(CASE WHEN COALESCE(is_sidechain,0)=1 OR COALESCE(message_count,0)<20 THEN 1 ELSE 0 END) AS ineligible_n,
                  AVG(CASE WHEN COALESCE(is_sidechain,0)=0 AND COALESCE(message_count,0)>=20 THEN cache_hit_rate END) AS eligible_hit,
                  AVG(CASE WHEN COALESCE(is_sidechain,0)=1 OR COALESCE(message_count,0)<20 THEN cache_hit_rate END) AS ineligible_hit
           FROM session_log WHERE rowid IN (SELECT id FROM session_activity WHERE date >= ?)""", (cutoff,)
    ).fetchone()
    return {
        "all_sessions": int(row["all_n"] or 0),
        "main_work_sessions": int(row["main_n"] or 0),
        "delegated_sessions": int(row["delegated_n"] or 0),
        "cache_eligible_main_sessions": int(row["eligible_n"] or 0),
        "cache_ineligible_sessions": int(row["ineligible_n"] or 0),
        "cache_eligible_main_hit_rate": round(float(row["eligible_hit"] or 0), 4),
        "cache_ineligible_hit_rate": round(float(row["ineligible_hit"] or 0), 4),
    }


def _collect_trends_from_jsonl(days=30):
    """Use the same dated queries without a persistent DB (read-only fallback).

    Discover all transcripts: mtime is only an ingestion hint. Deduplicate the
    complete history before slicing, including history replayed into a new file.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        ensure_activity_schema(conn)
        ledger = {}
        best = {}
        for filepath, mtime, project in _find_all_jsonl_files(None):
            parsed = _parse_session_jsonl(filepath)
            if not parsed:
                continue
            key = _canonical_session_uuid(filepath) or str(filepath)
            rank = _session_rank(parsed.get("message_count"), parsed.get("total_input_tokens"), parsed.get("total_output_tokens"))
            if key not in best or rank > best[key][0]:
                best[key] = (rank, filepath, mtime, project, parsed)
        for _, filepath, mtime, project, original in best.values():
            subagent_files = _session_subagent_files(filepath)
            merged = _session_merged_requests(filepath, original, subagent_files)
            if merged is not None:
                net, counted = _net_and_record(merged, ledger)
                parsed = _parsed_net_of_counted(original, counted)
                daily = _daily_usage_from_merged(net)
                calls = dict(parsed.get("daily_calls") or {})
                for u in net.values():
                    if not u.get("advisor") and u.get("n", 1) and u.get("day"):
                        day = u["day"]
                        calls[day] = calls.get(day, 0) + 1
            else:
                parsed = original
                daily = _session_daily_usage(filepath, parsed=original,
                                             subagent_files=subagent_files)
                calls = dict(original.get("daily_calls") or {})
            daily_metrics = _session_daily_metrics(parsed, subagent_files)
            # Same bounds rule as collect_sessions: combined/parent usage and
            # metadata days, never the file's ingestion timestamp.
            bounds = _activity_bounds(daily, parsed.get("daily_usage"), daily_metrics)
            quality = score_session_quality(parsed)
            values = {
                "jsonl_path": str(filepath), "date": bounds[-1] if bounds else "",
                "first_activity_day": bounds[0] if bounds else None,
                "last_activity_day": bounds[-1] if bounds else None,
                "project": project,
                "duration_minutes": parsed.get("duration_minutes"), "input_tokens": parsed.get("total_input_tokens"),
                "output_tokens": parsed.get("total_output_tokens"), "message_count": parsed.get("message_count"),
                "api_calls": parsed.get("api_calls"), "cache_hit_rate": parsed.get("cache_hit_rate"),
                "cache_create_1h_tokens": parsed.get("total_cache_create_1h", 0),
                "cache_create_5m_tokens": parsed.get("total_cache_create_5m", 0),
                "avg_call_gap_seconds": parsed.get("avg_call_gap_seconds"),
                "max_call_gap_seconds": parsed.get("max_call_gap_seconds"),
                "p95_call_gap_seconds": parsed.get("p95_call_gap_seconds"),
                "model_usage_json": json.dumps(parsed.get("model_usage") or {}),
                "model_usage_breakdown_json": json.dumps(parsed.get("model_usage_breakdown") or {}),
                "daily_usage_json": json.dumps(daily), "parent_daily_usage_json": json.dumps(parsed.get("daily_usage") or {}),
                "daily_calls_json": json.dumps(calls), "daily_metrics_json": json.dumps(daily_metrics),
                "skills_json": json.dumps(parsed.get("skills_used") or {}),
                "subagents_json": json.dumps(parsed.get("subagents_used") or {}),
                "tool_calls_json": json.dumps(parsed.get("tool_calls") or {}),
                "slug": parsed.get("slug"), "topic": parsed.get("topic"),
                "quality_score": quality["score"], "quality_grade": quality["grade"],
                "is_sidechain": int(bool(parsed.get("is_sidechain"))), "transcript_mtime": mtime,
            }
            conn.execute("INSERT INTO session_log (" + ",".join(values) + ") VALUES ("
                         + ",".join("?" for _ in values) + ")", tuple(values.values()))
        _rebuild_aggregate_tables(conn)
        return _query_trends_db(conn, days)
    finally:
        conn.close()


def _collect_git_commits(days=30):
    """Scan known git repos for commits within the time window.

    Checks project directories under ~/.claude/projects/ (reversing the
    dashed name to a real path) and skill repos under ~/.claude/skills/.

    Returns: { "2026-03-01": [{"repo": "name", "commits": ["msg1", ...]}], ... }
    """
    cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    # Collect candidate repo paths
    repo_paths = {}  # path -> display name

    # 1. From project directories
    projects_base = CLAUDE_DIR / "projects"
    if projects_base.exists():
        for project_dir in projects_base.iterdir():
            if not project_dir.is_dir():
                continue
            # Reverse dashed name to real path: -Users-alex-myproject -> /Users/alex/myproject
            real_path = "/" + project_dir.name.lstrip("-").replace("-", "/")
            rp = Path(real_path)
            if rp.is_dir() and (rp / ".git").exists():
                repo_paths[str(rp)] = _clean_project_name(project_dir.name)

    # 2. From skill repos
    skills_dir = CLAUDE_DIR / "skills"
    if skills_dir.exists():
        for skill_dir in skills_dir.iterdir():
            if skill_dir.is_dir() and (skill_dir / ".git").exists():
                repo_paths[str(skill_dir)] = skill_dir.name

    if not repo_paths:
        return {}

    result = {}  # date -> [{"repo": name, "commits": [msg, ...]}]

    for repo_path, display_name in repo_paths.items():
        try:
            proc = subprocess.run(
                ["git", "-C", repo_path, "log", "--oneline",
                 f"--since={cutoff_date}", "--format=%ai|%s"],
                capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                continue
            for line in proc.stdout.strip().split("\n"):
                if "|" not in line:
                    continue
                date_part, msg = line.split("|", 1)
                date = date_part.strip()[:10]  # YYYY-MM-DD
                if date not in result:
                    result[date] = []
                # Find or create repo entry for this date
                repo_entry = None
                for entry in result[date]:
                    if entry["repo"] == display_name:
                        repo_entry = entry
                        break
                if repo_entry is None:
                    repo_entry = {"repo": display_name, "commits": []}
                    result[date].append(repo_entry)
                repo_entry["commits"].append(msg.strip())
        except (subprocess.TimeoutExpired, OSError):
            continue

    return result


def _collect_trends_data(days=30):
    """Collect trends data, preferring SQLite DB when available.

    Falls back to live JSONL parsing if DB doesn't exist or is empty.
    """
    # Try SQLite first (faster, accumulated data)
    result = _collect_trends_from_db(days)
    if result is not None:
        result["git_commits"] = _collect_git_commits(days)
        return result
    # Fall back to live JSONL parsing
    result = _collect_trends_from_jsonl(days)
    if result is not None:
        result["git_commits"] = _collect_git_commits(days)
    return result


def _build_ttl_period_summary(period_days):
    """Build a compact TTL mix summary for a given period."""
    trends = _collect_trends_data(days=period_days)
    if not trends:
        return {
            "label": f"{period_days}d: no cache-write data",
            "period_days": period_days,
            "mixed_sessions": 0,
            "five_only_sessions": 0,
            "one_hour_only_sessions": 0,
        }

    mixed_sessions = 0
    five_only_sessions = 0
    one_hour_only_sessions = 0
    for day in trends.get("daily", []):
        for session in day.get("session_details", []):
            if session.get("continuation"):
                continue
            ttl_1h = session.get("cache_create_1h_tokens", 0) or 0
            ttl_5m = session.get("cache_create_5m_tokens", 0) or 0
            if ttl_1h and ttl_5m:
                mixed_sessions += 1
            elif ttl_5m and not ttl_1h:
                five_only_sessions += 1
            elif ttl_1h and not ttl_5m:
                one_hour_only_sessions += 1

    if mixed_sessions == 0 and five_only_sessions == 0:
        label = f"{period_days}d: all 1h-only"
    else:
        parts = []
        if mixed_sessions:
            parts.append(f"{mixed_sessions} mixed")
        if five_only_sessions:
            parts.append(f"{five_only_sessions} 5m-only")
        label = f"{period_days}d: " + ", ".join(parts)

    return {
        "label": label,
        "period_days": period_days,
        "mixed_sessions": mixed_sessions,
        "five_only_sessions": five_only_sessions,
        "one_hour_only_sessions": one_hour_only_sessions,
    }


def usage_trends(days=30, as_json=False):
    """Analyze usage trends across all Claude Code sessions."""
    trends = _collect_trends_data(days)
    if trends is None:
        print(f"\nNo session logs found in the last {days} days.")
        print(f"Looked in: {CLAUDE_DIR / 'projects' / '*' / '*.jsonl'}")
        return

    if as_json:
        result = dict(trends)
        result.pop("trajectory", None)
        print(json.dumps(result, indent=2, default=str))
        return

    session_count = trends["session_count"]
    avg_dur = trends["avg_duration_minutes"]
    avg_in = trends["avg_input_tokens"]
    avg_out = trends["avg_output_tokens"]

    def _fmt_tokens(n):
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.0f}K"
        return str(int(n))

    print(f"\nUSAGE TRENDS (last {days} days)")
    print("=" * 55)
    print(f"\n  Sessions: {session_count} | Avg duration: {avg_dur:.0f} min | Avg tokens/session: {_fmt_tokens(avg_in)} in (incl. cache reads) / {_fmt_tokens(avg_out)} out")

    skill_sessions = trends["skills"]["used"]
    installed_count = trends["skills"]["installed_count"]
    never_used = trends["skills"]["never_used"]

    print("\nSKILLS")
    if skill_sessions:
        print(f"  Used ({len(skill_sessions)} of {installed_count} installed):")
        for skill, count in sorted(skill_sessions.items(), key=lambda x: -x[1])[:15]:
            dots = "." * max(2, 30 - len(skill))
            print(f"    {_strip_ansi(str(skill))} {dots} {count} session{'s' if count != 1 else ''}")
        if len(skill_sessions) > 15:
            print(f"    ... and {len(skill_sessions) - 15} more")
    else:
        print(f"  No skill invocations found in {session_count} sessions.")

    if never_used:
        approx_overhead = len(never_used) * TOKENS_PER_SKILL_APPROX
        print(f"\n  Never used (last {days} days):")
        names = sorted(never_used)
        line = "    "
        for i, name in enumerate(names):
            addition = name + (", " if i < len(names) - 1 else "")
            if len(line) + len(addition) > 72:
                print(line.rstrip(", "))
                line = "    " + addition
            else:
                line += addition
        if line.strip():
            print(line.rstrip(", "))
        print(f"    ({len(never_used)} skills, ~{approx_overhead:,} tokens overhead)")

    total_subagents = trends["subagents"]
    if total_subagents:
        print("\nSUBAGENTS")
        for agent, count in sorted(total_subagents.items(), key=lambda x: -x[1]):
            dots = "." * max(2, 30 - len(agent))
            print(f"  {_strip_ansi(str(agent))} {dots} {count} spawned")

    total_model_tokens = trends["model_mix"]
    if total_model_tokens:
        # Model shares count fresh input + output (cache reads excluded), unlike
        # the per-session average above; say so, or the two read as contradictory.
        print("\nMODEL MIX (share of tokens, excl. cache reads)")
        grand_total = sum(total_model_tokens.values())
        for model, tokens in sorted(total_model_tokens.items(), key=lambda x: -x[1]):
            pct = tokens / grand_total * 100 if grand_total else 0
            dots = "." * max(2, 26 - len(model))
            print(f"  {_strip_ansi(str(model))} {dots} {pct:.0f}% of tokens ({_fmt_tokens(tokens)})")

    trajectory = trends.get("trajectory", {})
    snapshots = trajectory.get("snapshots", [])
    if snapshots:
        print("\nOVERHEAD TRAJECTORY (from saved snapshots)")
        for snap in snapshots:
            ts = snap["timestamp"][:10] if snap["timestamp"] else "unknown"
            label = snap["label"]
            total = snap["total"]
            print(f"  {ts}: {total:,} tokens ({label})")

        current_total = trajectory.get("current_total", 0)
        if snapshots and current_total:
            latest = snapshots[-1]["total"]
            drift = current_total - latest
            if abs(drift) > 500:
                direction = "+" if drift > 0 else ""
                print(f"  Today:  {current_total:,} tokens (current)")
                print(f"  Drift since last snapshot: {direction}{drift:,} tokens")

    print()


_parse_elapsed_time = _measure_compat.bind(_measure_providers['daemon']._parse_elapsed_time, __name__)


_format_elapsed = _measure_compat.bind(_measure_providers['daemon']._format_elapsed, __name__)


_PROJECT_JSONL_STAT_CACHE = None
_PROJECT_JSONL_STAT_CACHE_TS = 0.0
_PROJECT_JSONL_STAT_TTL = 30.0


_scan_project_jsonl_stats = _measure_compat.bind(_measure_providers['daemon']._scan_project_jsonl_stats, __name__)


_find_session_version_for_pid = _measure_compat.bind(_measure_providers['daemon']._find_session_version_for_pid, __name__)


_CCD_CLI_LAUNCHER_SEGMENT = "/.claude/remote/ccd-cli/"
# The remote launcher's argv[0] IS the versioned binary, whose basename is the
# bare version (e.g. `.../ccd-cli/2.1.271`, optionally `.exe` on Windows). Anchor
# to that version-shaped basename so a bundled/spawned binary under the version
# dir (`.../ccd-cli/2.1.271/rg`), a non-version file (`.../ccd-cli/helper.sh`), a
# `.download` partial, or the bare directory do NOT match. A bare-substring test
# swept all of those in and inflated the session count. See issue #192.
_CCD_CLI_LAUNCHER_RE = re.compile(
    re.escape(_CCD_CLI_LAUNCHER_SEGMENT) + r"\d+(?:\.\d+)+(?:\.exe)?$"
)


_command_matches_process = _measure_compat.bind(_measure_providers['daemon']._command_matches_process, __name__)


# POSIX process identity (macOS and Linux). Same contract as the Windows block
# further down (issue #211): `kill_stale_sessions` acts on this inventory, and
# process age or a `claude` argv[0] alone does not show that a conversation is
# abandoned. A claude process can be hosted by the desktop app (driven over
# stream-json, wrapped by Claude.app's `disclaimer` helper), an IDE, the SDK or
# a headless `-p` run, or be a subcommand (`mcp`, `doctor` ...). So identity is
# established from the real argv and the parent chain, and fails closed:
# only a positively identified terminal CLI process is ever terminated.
_POSIX_TERMINAL_PARENTS = frozenset({
    # shells
    "sh", "bash", "zsh", "fish", "dash", "ksh", "ksh93", "mksh", "pdksh", "tcsh", "csh",
    "ash", "rbash", "xonsh", "nu", "nushell", "elvish", "pwsh", "osh", "oil",
    # multiplexers and persistent-session hosts (a tmux-hosted interactive
    # session IS a terminal session)
    "tmux", "screen", "byobu", "zellij", "dtach", "abduco", "mosh-server",
    # remote logins
    "sshd", "sshd-session", "sshd-auth",
    # terminal emulators that may be a direct parent
    "terminal", "iterm2", "ghostty", "alacritty", "kitty", "wezterm", "wezterm-gui",
    "hyper", "warp", "tabby", "rio", "foot", "xterm", "urxvt", "rxvt", "st", "konsole",
    "gnome-terminal-", "gnome-terminal-server", "terminator", "tilix", "xfce4-terminal",
    "lxterminal", "mate-terminal", "kgx", "ptyxis",
})
_POSIX_NO_TTY = ("??", "-", "?")
_POSIX_ANCESTRY_MAX_HOPS = 16
# pid ppid [uid] tty lstart etime command. uid is optional only so a row without
# it still parses for display; such a session has no provable owner (uid None)
# and kill-stale never terminates it. A tty is never all digits, so the optional
# numeric field cannot swallow it.
_POSIX_PS_ROW_RE = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+(?:(\d+)\s+)?(\S+)\s+"
    r"(\S+\s+\S+\s+\d+\s+\d{1,2}:\d{2}:\d{2}\s+\d{4})\s+"  # lstart (C locale)
    r"(\S+)\s+(.*?)\s*$"
)
_POSIX_NAME_ROW_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(.+?)\s*$")


_posix_comm_name = _measure_compat.bind(_measure_providers['daemon']._posix_comm_name, __name__)


_posix_comm_is_claude = _measure_compat.bind(_measure_providers['daemon']._posix_comm_is_claude, __name__)


_posix_is_claude_host = _measure_compat.bind(_measure_providers['daemon']._posix_is_claude_host, __name__)


_posix_option_args = _measure_compat.bind(_measure_providers['daemon']._posix_option_args, __name__)


_posix_is_headless = _measure_compat.bind(_measure_providers['daemon']._posix_is_headless, __name__)


_posix_read_argv = _measure_compat.bind(_measure_providers['daemon']._posix_read_argv, __name__)


_posix_exe_path = _measure_compat.bind(_measure_providers['daemon']._posix_exe_path, __name__, defaults=(None,))


_posix_dir_is_electron_app = _measure_compat.bind(_measure_providers['daemon']._posix_dir_is_electron_app, __name__)


_posix_is_init_parent = _measure_compat.bind(_measure_providers['daemon']._posix_is_init_parent, __name__)


_posix_ancestry_state = _measure_compat.bind(_measure_providers['daemon']._posix_ancestry_state, __name__, defaults=(frozenset(),))


_classify_posix_claude_process = _measure_compat.bind(_measure_providers['daemon']._classify_posix_claude_process, __name__, defaults=(frozenset(),))


_posix_process_names = _measure_compat.bind(_measure_providers['daemon']._posix_process_names, __name__)


_posix_ancestor_pids = _measure_compat.bind(_measure_providers['daemon']._posix_ancestor_pids, __name__)


_collect_posix_claude_sessions = _measure_compat.bind(_measure_providers['daemon']._collect_posix_claude_sessions, __name__, defaults=("claude",))


_local_start_label = _measure_compat.bind(_measure_providers['daemon']._local_start_label, __name__)


_parse_wmi_datetime = _measure_compat.bind(_measure_providers['daemon']._parse_wmi_datetime, __name__)


# A culture whose time separator is "." renders 15:04:05 as 15.04.05 when a
# .NET custom format reaches ToString() without InvariantCulture. The collectors
# now pin the invariant culture; this keeps the parser side tolerant too, so an
# older cached or third-party value degrades to a correct time, not to a session
# silently marked UNVERIFIED.
_PS_DOTTED_TIME_RE = re.compile(r"(?<=[T ])(\d{2})\.(\d{2})\.(\d{2})(?=$|[Zz+\-.,])")


# .NET ToString('o') writes seven fractional digits. Before Python 3.11,
# fromisoformat accepts exactly three or six, so pad/trim the fraction to six.
_PS_FRACTION_RE = re.compile(r"(?<=\d{2}:\d{2}:\d{2})\.(\d+)")


_normalize_ps_iso = _measure_compat.bind(_measure_providers['daemon']._normalize_ps_iso, __name__)


_parse_iso_process_datetime = _measure_compat.bind(_measure_providers['daemon']._parse_iso_process_datetime, __name__)


_windows_process_creation = _measure_compat.bind(_measure_providers['daemon']._windows_process_creation, __name__)


# Windows process identity (issue #211). The Claude desktop app is Electron:
# its main process, GPU/renderer/utility/crashpad children and the SSH broker
# are all image-named claude*.exe, and the Code tab hosts its real sessions as
# claude.exe children driven over stream-json. Image name alone cannot tell a
# terminal CLI session from any of those, and kill_stale_sessions acts on this
# inventory, so identity must be established from the command line and fail
# closed: only a positively identified terminal CLI process is ever terminated.
_WIN_HEADLESS_ARGS = ("--output-format", "--input-format", "--sdk-url")
_WIN_ELECTRON_MARKERS = (
    ("resources", "app.asar"),
    ("icudtl.dat",),
    ("chrome_100_percent.pak",),
    ("resources.pak",),
)


_windows_start_times_agree = _measure_compat.bind(_measure_providers['daemon']._windows_start_times_agree, __name__, defaults=(2,))


_windows_cim_process_details = _measure_compat.bind(_measure_providers['daemon']._windows_cim_process_details, __name__)


_windows_process_names = _measure_compat.bind(_measure_providers['daemon']._windows_process_names, __name__)


# Image names whose parent has always exited by the time anyone looks (logon and
# service-host roots): a chain ending here is complete, not broken.
_WINDOWS_ANCESTRY_ROOTS = frozenset({
    "explorer.exe", "winlogon.exe", "wininit.exe", "services.exe", "smss.exe",
    "svchost.exe", "userinit.exe", "system",
})


_windows_ancestor_pids = _measure_compat.bind(_measure_providers['daemon']._windows_ancestor_pids, __name__, defaults=(None,))


_windows_dir_is_electron_app = _measure_compat.bind(_measure_providers['daemon']._windows_dir_is_electron_app, __name__)


_WIN_TERMINAL_PARENTS = frozenset({
    "cmd.exe", "powershell.exe", "pwsh.exe", "bash.exe", "sh.exe", "zsh.exe",
    "wt.exe", "windowsterminal.exe", "conhost.exe", "openconsole.exe",
    "mintty.exe", "wezterm-gui.exe", "alacritty.exe",
})
_WIN_HEADLESS_FLAGS = ("--output-format", "--input-format", "--sdk-url", "--print", "--ide")
_WIN_HEADLESS_SUBCOMMANDS = frozenset({
    "mcp", "doctor", "update", "install", "config", "migrate-installer",
    "remote-control", "plugin", "agents", "setup-token", "login", "logout",
})
# Processes that anchor a process tree on Windows. Reaching one proves the
# ancestry walk ended at a real root instead of at missing information.
_WIN_STABLE_ROOTS = frozenset({
    "explorer.exe", "winlogon.exe", "wininit.exe", "sihost.exe", "system",
    "csrss.exe", "smss.exe",
})
# Service / scheduler hosts: a claude started from one is a background job, not
# an interactive terminal, even when the chain reaches a real root.
_WIN_SERVICE_HOSTS = frozenset({
    "svchost.exe", "services.exe", "taskeng.exe", "taskhostw.exe", "taskhost.exe",
})
_WIN_ANCESTRY_MAX_HOPS = 16


_windows_ancestry_state = _measure_compat.bind(_measure_providers['daemon']._windows_ancestry_state, __name__)


_windows_cmdline_tokens = _measure_compat.bind(_measure_providers['daemon']._windows_cmdline_tokens, __name__)


_windows_option_args = _measure_compat.bind(_measure_providers['daemon']._windows_option_args, __name__)


_read_strict_csv = _measure_compat.bind(_measure_providers['daemon']._read_strict_csv, __name__)


_classify_windows_claude_process = _measure_compat.bind(_measure_providers['daemon']._classify_windows_claude_process, __name__, defaults=(None,))


_collect_windows_claude_sessions = _measure_compat.bind(_measure_providers['daemon']._collect_windows_claude_sessions, __name__, defaults=("claude", True,))


_resolve_runtime_bin = _measure_compat.bind(_measure_providers['daemon']._resolve_runtime_bin, __name__)


_semver_key = _measure_compat.bind(_measure_providers['daemon']._semver_key, __name__)


_collect_health_data = _measure_compat.bind(_measure_providers['daemon']._collect_health_data, __name__)


health_selfcheck = _measure_compat.bind(_measure_providers['daemon'].health_selfcheck, __name__)


session_health = _measure_compat.bind(_measure_providers['daemon'].session_health, __name__)


_windows_revalidate_terminal_cli = _measure_compat.bind(_measure_providers['daemon']._windows_revalidate_terminal_cli, __name__, defaults=(None,))


_posix_revalidate_terminal_cli = _measure_compat.bind(_measure_providers['daemon']._posix_revalidate_terminal_cli, __name__, defaults=(None, "terminal_cli",))


_revalidate_terminal_cli = _measure_compat.bind(_measure_providers['daemon']._revalidate_terminal_cli, __name__, defaults=("terminal_cli",))


_parse_kill_stale_args = _measure_compat.bind(_measure_providers['daemon']._parse_kill_stale_args, __name__)


_kill_stale_owns = _measure_compat.bind(_measure_providers['daemon']._kill_stale_owns, __name__)


kill_stale_sessions = _measure_compat.bind(_measure_providers['daemon'].kill_stale_sessions, __name__, defaults=(12, False, False,))


# ========== Hook Management ==========

SETTINGS_PATH = CLAUDE_DIR / "settings.json"
_SETTINGS_WRITE_READ_STATE = threading.local()
MEASURE_PY_PATH = Path(__file__).resolve()
if sys.platform == "win32":
    # Claude Code runs hooks through Git Bash even on native Windows, so
    # this must be POSIX-shell syntax: >/dev/null (a cmd null redirect would
    # become a literal file named NUL in the CWD) and forward-slash,
    # single-quoted paths (not cmd.exe list2cmdline quoting). sys.executable
    # with forward slashes runs fine from Git Bash; bare `python3` often does
    # not exist there.
    _py = shlex.quote(str(sys.executable).replace("\\", "/"))
    _mp = shlex.quote(str(MEASURE_PY_PATH).replace("\\", "/"))
    HOOK_COMMAND = (
        f"{_py} {_mp} session-end-flush --trigger end"
        f" >/dev/null 2>&1"
    )
else:
    HOOK_COMMAND = f"python3 '{MEASURE_PY_PATH}' session-end-flush --trigger end"
# Recognizes Token Optimizer's SessionEnd hook command: a script named
# ``measure.py`` invoked with the ``collect`` or ``session-end-flush``
# subcommand as its first positional argument. The path may be single-quoted
# (``'…/measure.py'`` — the format HOOK_COMMAND uses) and preceded by a
# python interpreter or a launcher chain. Word boundary on ``measure``
# prevents matching longer filenames (``other_measure.py``); requiring the
# subcommand to immediately follow the script (modulo an optional closing
# quote and whitespace) prevents matching unrelated commands that merely
# mention ``measure.py`` and ``collect`` in prose
# (e.g. ``echo 'run measure.py to collect data'``) or glued with a dot
# (``measure.py.collect``). Shared by _is_hook_installed and
# _is_token_optimizer_session_end_hook so detection and removal stay in sync.
# The alternation is anchored with a negative lookahead
# ``(?![\w-])`` instead of a trailing ``\b``. ``\b`` treats ``-`` as a word
# boundary, so ``\bcollect\b`` matched the ``collect`` inside ``collect-foo``
# (a hypothetical ``collect-*`` subcommand) and ``session-end-flush`` inside
# ``session-end-flush-archive``. The lookahead rejects a following word char
# OR hyphen, so only the bare subcommands match.
_TO_SESSION_END_CMD_RE = re.compile(
    r"\bmeasure\.py['\"]?\s+(?:collect|session-end-flush)(?![\w-])"
)


def _is_plugin_installed():
    """Check if token-optimizer is installed as a Claude Code plugin.

    Plugin hooks (hooks.json) auto-install all hooks, so if the plugin is
    installed, we don't need to check settings.json for individual hooks.
    """
    registry = CLAUDE_DIR / "plugins" / "installed_plugins.json"
    if registry.exists():
        try:
            with open(registry, "r", encoding="utf-8") as f:
                data = json.load(f)
            plugins = data.get("plugins", {})
            for key in plugins:
                if "token-optimizer" in key.lower():
                    return True
        except (json.JSONDecodeError, PermissionError, OSError):
            pass
    # Cowork search-path fallback. When installed_plugins.json is absent or
    # doesn't list us (account-synced installs may not register the same way), but
    # THIS script is running from the synced-plugin tree, we ARE plugin-installed.
    # Additive: desktop hosts have no /plugins/synced/ segment so this never
    # changes desktop behavior.
    if _is_running_from_synced_plugin():
        return True
    return False


def _is_compaction_runner(cmd, event):
    """Recognize runner path arguments for the events each runner dispatches."""
    if not isinstance(cmd, str):
        return False
    name = "sessionstart_runner" if event == "SessionStart" else "stop_runner"
    if event not in ("SessionStart", "Stop", "SessionEnd"):
        return False
    return bool(re.search(r"(?:^|[\s\"'/\\])" + name + r"\.py(?:$|[\s\"';])", cmd))


def _is_hook_installed(settings=None, settings_only=False):
    """Check if the SessionEnd measure.py collect hook is installed.

    Returns True if any SessionEnd hook command contains 'measure.py collect'.
    Recognizes both old (collect-only) and new (collect + dashboard) hook commands.
    Also checks plugin cache hooks (auto-installed via marketplace plugin), unless
    ``settings_only`` asks only whether settings.json carries it (doctor's label).
    """
    # Check user settings.json
    if settings is None:
        if _is_regular_file(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8-sig") as f:
                    settings = json.load(f)
            except (json.JSONDecodeError, PermissionError, OSError):
                settings = {}
        else:
            settings = {}

    hooks = settings.get("hooks", {})
    session_end = hooks.get("SessionEnd", [])
    if isinstance(session_end, list):
        for entry in session_end:
            hook_list = entry.get("hooks", []) if isinstance(entry, dict) else []
            for hook in hook_list:
                cmd = hook.get("command", "") if isinstance(hook, dict) else ""
                if isinstance(cmd, str) and (
                    _TO_SESSION_END_CMD_RE.search(cmd) or _is_compaction_runner(cmd, "SessionEnd")
                ):
                    return True

    if settings_only:
        return False

    # Check plugin cache hooks (marketplace plugin auto-install)
    plugin_cache = CLAUDE_DIR / "plugins" / "cache"
    if plugin_cache.exists():
        import glob as globmod
        for hooks_file in globmod.glob(str(plugin_cache / "*" / "token-optimizer" / "*" / "hooks" / "hooks.json")):
            try:
                with open(hooks_file, "r", encoding="utf-8") as f:
                    plugin_hooks = json.load(f)
                ph = plugin_hooks.get("hooks", {}).get("SessionEnd", [])
                if isinstance(ph, list):
                    for entry in ph:
                        hook_list = entry.get("hooks", []) if isinstance(entry, dict) else []
                        for hook in hook_list:
                            cmd = hook.get("command", "") if isinstance(hook, dict) else ""
                            if isinstance(cmd, str) and (
                                _TO_SESSION_END_CMD_RE.search(cmd) or _is_compaction_runner(cmd, "SessionEnd")
                            ):
                                return True
            except (json.JSONDecodeError, PermissionError, OSError):
                continue

    return False


def _sessionend_cmd_is_flush(cmd) -> bool:
    """True when ``cmd`` is the current SessionEnd shape (session-end-flush)."""
    if not isinstance(cmd, str) or not cmd:
        return False
    return bool(re.search(r"\bmeasure\.py['\"]?\s+session-end-flush\b", cmd))


def _sessionend_cmd_is_collect_fossil(cmd) -> bool:
    """True when ``cmd`` is the collect/dashboard SessionEnd fossil.

    ``_TO_SESSION_END_CMD_RE`` matches both ``collect`` and ``session-end-flush``.
    Anything that matches and is not the flush shape is the inline heavy fossil.
    """
    if not isinstance(cmd, str) or not cmd:
        return False
    if _sessionend_cmd_is_flush(cmd):
        return False
    return bool(_TO_SESSION_END_CMD_RE.search(cmd))


def _is_hook_current(settings=None):
    """True when the installed SessionEnd hook is the session-end-flush shape.

    The collect-then-dashboard fossil is never current: it runs the
    heavy flush inline and unbounded. The pre-v5.11 win32 cmd-null-redirect
    form is also never current. setup_hook's upgrade branch rewrites anything
    that returns False.
    """
    if settings is None:
        if not _is_regular_file(SETTINGS_PATH):
            return False
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8-sig") as f:
                settings = json.load(f)
        except (json.JSONDecodeError, PermissionError, OSError):
            return False

    hooks = settings.get("hooks", {})
    session_end = hooks.get("SessionEnd", [])
    if not isinstance(session_end, list):
        return False
    for entry in session_end:
        hook_list = entry.get("hooks", []) if isinstance(entry, dict) else []
        for hook in hook_list:
            cmd = hook.get("command", "") if isinstance(hook, dict) else ""
            if not isinstance(cmd, str):
                continue
            if _sessionend_cmd_is_collect_fossil(cmd):
                return False
            if _sessionend_cmd_is_flush(cmd):
                # A flush-shaped command that still uses the cmd.exe
                # NUL-device redirect is not current on win32.
                if sys.platform == "win32" and re.search(r">\s*NUL\b", cmd):
                    return False
                return True
    return False


def check_hook():
    """Exit 0 if SessionEnd measure.py collect hook is installed, 1 if not."""
    sys.exit(0 if _is_hook_installed() else 1)


_SETTINGS_LOCK_PATH = SETTINGS_PATH.parent / ".settings.lock"


_settings_lock = _measure_compat.bind(_measure_providers['lifecycle']._settings_lock, __name__, defaults=(False,), decorators=(contextmanager,))


def _settings_write_guard(settings_data, allow_removing_keys=None, dest=None):
    """Refuse any settings.json write that would DROP a top-level key.

    The data-loss class of bug: a caller reads settings.json with the
    lossy ``_read_settings_json()`` (which collapses a missing, malformed, or
    unreadable file to ``{}``), mutates that ``{}``, and writes it back --
    erasing every key the user owned. The fix added
    ``_read_settings_json_checked()`` but converted only 2 of 20 call sites,
    so the class stayed live. This is the structural fix: ONE choke point that
    every write passes through, so no call site can drop keys by accident
    regardless of how it obtained its dict.

    Returns (allowed: bool, reason: str). Never raises.

    Rules:
      * Outgoing value must be a dict.
      * No file on disk -> nothing can be lost -> allow.
      * File on disk unreadable / malformed / not an object -> REFUSE. We
        cannot prove the write is non-destructive, and "unknown" is exactly
        the state that produced the original bug.
      * Any top-level key present on disk but absent from the outgoing dict
        must be declared in ``allow_removing_keys``, else REFUSE.
      * Same one level down inside ``env``: a var present in the on-disk
        ``env`` map but absent from the outgoing ``env`` map must be
        declared as ``env.VAR`` (or the whole subtree licensed by
        declaring ``env``), else REFUSE.

    Deliberate removals stay possible: pass ``allow_removing_keys={"statusLine"}``
    (uninstall paths) or a wider set. The opt-in is per-key and explicit, so a
    caller can never silently widen its blast radius.

    Cost: one extra read of a ~4KB file. Safe for the SessionStart budget.
    """
    if not isinstance(settings_data, dict):
        return False, f"outgoing settings is {type(settings_data).__name__}, not a dict"
    target = dest if dest is not None else SETTINGS_PATH
    if os.path.lexists(target) and not _is_regular_file(target):
        # never open() a FIFO/socket/device (it can block forever).
        return False, "settings.json on disk is not a regular file; cannot prove this write is non-destructive"
    try:
        with open(target, "r", encoding="utf-8-sig") as f:
            current = json.load(f)
    except FileNotFoundError:
        return True, "no settings.json on disk; nothing to preserve"
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return False, "settings.json on disk is malformed; cannot prove this write is non-destructive"
    except (PermissionError, OSError) as e:
        return False, f"settings.json on disk is unreadable ({e.__class__.__name__}); cannot prove this write is non-destructive"
    # a file with no write bits was frozen by the user on purpose
    # (chmod 444, or the read-only attribute on Windows). The atomic-write
    # trick (temp file + os.replace) would happily REPLACE it, so the
    # permission check has to happen here, before any write is attempted.
    if os.stat(target).st_mode & (
            stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0:
        return False, ("settings.json has no write bits set -- it was made "
                       "read-only on purpose; refusing to replace a file "
                       "the user froze")
    if not isinstance(current, dict):
        return False, "settings.json on disk is not a JSON object; refusing to overwrite"
    allowed = set(allow_removing_keys or ())
    dropped = sorted(set(current) - set(settings_data) - allowed)
    if dropped:
        return False, "would DROP top-level key(s): " + ", ".join(dropped)
    # the same data-loss class one level down. `env` is a flat
    # string->string map a caller can carry forward while silently dropping
    # a var inside it (e.g. CLAUDE_AUTOCOMPACT_PCT_OVERRIDE). Diff its keys
    # too. A deliberate nested removal stays possible by declaring the
    # dotted name in allow_removing_keys ({"env.MY_VAR"}); declaring the
    # top-level "env" licenses the whole subtree.
    if "env" not in allowed:
        cur_env = current.get("env")
        out_env = settings_data.get("env")
        if isinstance(cur_env, dict) and isinstance(out_env, dict):
            nested = sorted(
                "env." + key
                for key in set(cur_env) - set(out_env)
                if "env." + key not in allowed
            )
            if nested:
                return False, "would DROP env var(s): " + ", ".join(nested)
    return True, "ok"


def _report_settings_write_refusal(why):
    print(
        "  [Token Optimizer] REFUSED settings.json write: " + why + ".\n"
        "  Nothing was written; your settings.json is untouched. This guard "
        "exists because a write like this silently erased user settings. "
        "If the removal was intended, the caller must declare it "
        "via allow_removing_keys.",
        file=sys.stderr,
    )


def _log_settings_lease_denied():
    """Durable breadcrumb when the settings lease is denied (write-return audit).

    The lease denial path in ``_write_settings_atomic`` was completely silent:
    no stderr, no log, no ``last_refusal`` breadcrumb. Callers reported
    "settings.json write was refused" but there was no way to distinguish a
    transient lease denial (contention, retries on next hook fire) from a
    guard refusal (destructive payload, needs a code fix). This writes one
    line to the daemon log dir so the denial is discoverable post-hoc, and
    sets ``last_refusal`` so the existing reporting infrastructure can tell
    the two paths apart. Never raises.
    """
    _SETTINGS_WRITE_READ_STATE.last_refusal = (
        "lease denied (another writer holds the settings lock)"
    )
    try:
        DAEMON_LOG_DIR.mkdir(parents=True, exist_ok=True)
        _cap_append_log(DAEMON_LOG_DIR / "settings-lease-denials.log")
        with open(DAEMON_LOG_DIR / "settings-lease-denials.log", "a",
                  encoding="utf-8") as f:
            f.write("%s lease denied\n" % time.strftime("%Y-%m-%dT%H:%M:%S"))
    except Exception:
        pass


# Why a write was held back because settings.json changed under it.
_SETTINGS_CHANGED_REFUSAL = (
    "settings.json changed while Token Optimizer was writing it (another "
    "program saved it twice in a row); nothing was written, your latest edit is kept"
)
_SETTINGS_WRITE_TRIES = 2


def _settings_file_identity(path=None):
    """(st_mtime_ns, st_size, st_ino) of the settings file, ``("absent",)`` when unreadable.

    A cheap fingerprint taken at the merge read and compared right before
    ``os.replace``. It narrows the window in which an unlocked external editor
    can be overwritten to the gap between that last stat and the replace, a
    few milliseconds. It does not close the window: nothing stops an editor
    that ignores our lease.
    """
    try:
        st = os.stat(path if path is not None else SETTINGS_PATH)
    except (OSError, ValueError):
        return ("absent",)
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _settings_write_refusal_reason():
    """Why the last ``_write_settings_atomic`` on this thread returned False."""
    return (getattr(_SETTINGS_WRITE_READ_STATE, "last_refusal", None)
            or "settings.json locked or guard refused the write")


def _existing_line_ending(path):
    """"\r\n" when the file at ``path`` is mostly CRLF, else "\n".

    A new file, an unreadable one, or one with no line break gets "\n". A mixed
    file follows whichever ending it has more of.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read(1_048_576)
    except OSError:
        return "\n"
    crlf = raw.count(b"\r\n")
    return "\r\n" if crlf > raw.count(b"\n") - crlf else "\n"


def _note_settings_os_error(exc):
    """Record an OSError from the temp write / replace as the refusal reason."""
    hint = ("; another program may have it open"
            if isinstance(exc, PermissionError) else "")
    _SETTINGS_WRITE_READ_STATE.last_refusal = (
        f"could not write settings.json ({exc.__class__.__name__}: {exc}){hint}"
    )
    _SETTINGS_WRITE_READ_STATE.os_error = True


def _write_settings_atomic_locked(settings_data, allow_removing_keys=None, _report_refusal=True,
                                  expect_identity=None):
    """Atomic settings.json write assuming the settings lease is ALREADY held.

    This is the lock-free body of ``_write_settings_atomic``, extracted so
    ``_reconcile_sessionend_fossils`` can hold ``_settings_lock()`` across
    fresh-read + re-apply + write as ONE critical section.
    Calling ``_write_settings_atomic`` from inside a held ``_settings_lock()``
    would re-acquire the non-reentrant lease and deadlock, so callers that
    already hold the lease MUST use this primitive instead.

    Never call this without already holding ``_settings_lock()``; it provides
    no serialization of its own. Same tempfile + os.replace + mode/symlink
    semantics as ``_write_settings_atomic`` (see the checked-read fix). Returns True iff the
    write landed.

    ``expect_identity`` is the ``_settings_file_identity`` taken when the
    payload was merged. When given and the file no longer matches right before
    ``os.replace``, nothing is replaced and ``identity_changed`` is set on the
    thread state so the caller can re-merge.
    """
    _SETTINGS_WRITE_READ_STATE.identity_changed = False
    _SETTINGS_WRITE_READ_STATE.os_error = False
    # Write THROUGH a symlink and preserve the mode.
    # os.replace onto the link path detaches it, turning a dotfiles-managed
    # symlink into a regular file (the user's repo silently stops tracking
    # their settings) and dropping 0644 to mkstemp's 0600. Resolve the link
    # first so the temp file lands in the real target's directory, and copy
    # the destination's existing mode onto it.
    try:
        dest = SETTINGS_PATH.resolve(strict=False)
    except (OSError, ValueError):
        dest = SETTINGS_PATH
    # Choke point for the key-loss class. Compared against `dest` (the
    # symlink-resolved real file), which is exactly the path os.replace lands
    # on, so a relocated/symlinked settings.json is checked against the bytes
    # that are actually about to be overwritten.
    _ok, _why = _settings_write_guard(settings_data, allow_removing_keys, dest)
    if not _ok:
        _SETTINGS_WRITE_READ_STATE.last_refusal = _why
        if _report_refusal:
            _report_settings_write_refusal(_why)
        return False
    try:
        dest_mode = stat.S_IMODE(os.stat(dest).st_mode)
    except OSError:
        dest_mode = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(dest.parent),
            prefix=".settings-",
            suffix=".json",
        )
    except OSError as exc:
        _note_settings_os_error(exc)
        return False
    try:
        # newline="" switches off text-mode translation (Windows would turn
        # every "\n" into "\r\n"); the ending is the file's own, set below.
        eol = _existing_line_ending(dest)
        with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="") as f:
            f.write(json.dumps(settings_data, indent=2, ensure_ascii=False)
                    .replace("\n", eol) + eol)
        if dest_mode is None:
            # Created from nothing: mkstemp gave 0600, but a fresh settings.json
            # should follow the umask like any file the user's tools create.
            # (os.umask can only be read by setting it; restored at once.)
            if os.name != "nt":
                try:
                    _umask = os.umask(0)
                    os.umask(_umask)
                    dest_mode = 0o666 & ~_umask
                except OSError:
                    dest_mode = None
        if dest_mode is not None:
            try:
                os.chmod(tmp_path, dest_mode)
            except OSError:
                pass
        if expect_identity is not None and _settings_file_identity(dest) != expect_identity:
            _SETTINGS_WRITE_READ_STATE.identity_changed = True
            return False
        os.replace(tmp_path, str(dest))
        tmp_path = None  # successfully replaced; do not unlink the destination
    except OSError as exc:
        # Windows refuses the replace while another program holds the file open
        # (a sharing violation); a full disk fails the temp write. Both are a
        # refused write with a real reason, never a traceback. The finally
        # below removes the temp file.
        _note_settings_os_error(exc)
        return False
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return True


def _merge_concurrent_settings(snapshot, mine, allow_removing_keys=None):
    """Three-way merge of our payload onto what is on disk right now.

    ``snapshot`` is the ``(path, base)`` pair recorded by
    ``_read_settings_for_write``; ``mine`` is the caller's payload derived from
    that read. Only the keys the caller actually CHANGED relative to ``base``
    are applied on top of the fresh file, so a value edit or key removal made
    by another editor between our read and our write is kept, as long as it
    landed before ``_write_settings_atomic`` fingerprinted the file (an edit
    after that is caught by the identity check before the replace and merged on
    a second try). ``env`` is merged per variable for the same reason. Returns the merged dict, or None
    when there is nothing to merge (no recorded read, the file is unchanged, or
    the payload drops keys it was not licensed to drop, which the write guard
    then refuses as before). Caller must hold the settings lease.
    """
    if not snapshot or not isinstance(mine, dict):
        return None
    snapshot_path, base = snapshot
    try:
        current_path = str(SETTINGS_PATH.resolve(strict=False))
    except (OSError, ValueError):
        current_path = str(SETTINGS_PATH)
    if snapshot_path != current_path or not isinstance(base, dict):
        return None
    allowed = set(allow_removing_keys or ())
    if set(base) - set(mine) - allowed:
        return None
    fresh, _path, fresh_ok = _read_settings_json_checked()
    if not fresh_ok or not isinstance(fresh, dict) or not SETTINGS_PATH.exists():
        return None
    if fresh == base:
        return None
    merged = dict(fresh)
    for key, value in mine.items():
        if key in base and base[key] == value:
            continue
        base_env, fresh_env = base.get("env"), fresh.get("env")
        if key == "env" and isinstance(value, dict) and isinstance(base_env, dict) \
                and isinstance(fresh_env, dict):
            env = dict(fresh_env)
            for var, val in value.items():
                if var not in base_env or base_env[var] != val:
                    env[var] = val
            for var in base_env:
                if var not in value and ("env" in allowed or "env." + var in allowed):
                    env.pop(var, None)
            merged["env"] = env
        else:
            merged[key] = value
    for key in allowed:
        if key not in mine:
            merged.pop(key, None)
    return merged


def _write_settings_atomic(settings_data, allow_removing_keys=None,
                           user_initiated=False):
    """Write settings.json atomically using tempfile + os.replace().

    Acquires the advisory ``_settings_lock()`` lease to prevent concurrent
    writes from clobbering each other (e.g., during SessionStart when
    multiple hooks may modify settings.json), then delegates the actual
    tempfile + os.replace to ``_write_settings_atomic_locked``.

    ``user_initiated=True`` marks an explicit user command: the
    lease bypasses the cohort throttle and retries briefly so a deliberate
    mutation cannot lose to a herd writer's tombstone. Automated/hook paths
    keep the non-blocking lease.

    Uses try/finally (not try/except Exception) so cleanup also runs when
    _HookTimeout (a BaseException) fires mid-write. Setting tmp_path to
    None after a successful os.replace prevents the finally clause from
    unlinking the already-renamed destination. Any exception encountered
    during the write propagates naturally after cleanup.

    A concurrent edit by a program that ignores our lease is handled on a best
    effort basis: the merge keeps what it saved before our read, and a
    size/mtime/inode check right before ``os.replace`` re-merges once if it
    saved in between. That narrows the window to milliseconds; it cannot
    close it, because an unlocked writer is not stopped.

    Returns True iff the write actually landed, False when the advisory lease
    was denied and nothing was written. Callers that report
    success to the user MUST check this -- a lease miss is logged to
    ``DAEMON_LOG_DIR/settings-lease-denials.log`` and sets ``last_refusal``
    so callers can distinguish lease denial from guard refusal (write-return
    audit 2026-08-29). All 22 call sites now check the return value; there
    are no remaining fire-and-forget callers.

    Callers that already hold ``_settings_lock()`` (e.g.
    ``_reconcile_sessionend_fossils``) MUST call
    ``_write_settings_atomic_locked`` instead -- this primitive would
    re-acquire the non-reentrant lease and deadlock.
    """
    snapshot = getattr(_SETTINGS_WRITE_READ_STATE, "snapshot", None)
    _SETTINGS_WRITE_READ_STATE.snapshot = None
    # A reason left by an earlier write must not explain this one.
    _SETTINGS_WRITE_READ_STATE.last_refusal = None
    with _settings_lock(user_initiated=user_initiated) as acquired:
        if not acquired:
            # Lease denial was completely silent (write-return audit 2026-08-29).
            # Log a durable breadcrumb and set last_refusal so callers can
            # distinguish "lease denied" from "guard refused".
            _log_settings_lease_denied()
            return False
        # Fingerprint the file before the merge reads it, then re-check right
        # before os.replace. If another program saved in between, re-merge onto
        # what it saved (two tries), so its edit is not written over. This
        # narrows the window to the stat-to-replace gap (milliseconds); an
        # editor that ignores our lease can still win inside that gap.
        for _attempt in range(_SETTINGS_WRITE_TRIES):
            identity = _settings_file_identity() if snapshot else None
            payload = _merge_concurrent_settings(snapshot, settings_data, allow_removing_keys)
            if payload is None:
                payload = settings_data
            if _write_settings_atomic_locked(payload, allow_removing_keys,
                                             _report_refusal=False,
                                             expect_identity=identity):
                return True
            if not getattr(_SETTINGS_WRITE_READ_STATE, "identity_changed", False):
                break
        else:
            _SETTINGS_WRITE_READ_STATE.last_refusal = _SETTINGS_CHANGED_REFUSAL
            _report_settings_write_refusal(_SETTINGS_CHANGED_REFUSAL)
            return False

        if getattr(_SETTINGS_WRITE_READ_STATE, "os_error", False):
            # The OS refused the temp write or the replace; a re-merge cannot
            # help and the guard did not refuse, so say nothing more. The
            # reason is in last_refusal for the caller to report.
            return False

        refusal = getattr(_SETTINGS_WRITE_READ_STATE, "last_refusal", None)

        def refuse():
            if refusal:
                _report_settings_write_refusal(refusal)
            return False

        # Callers commonly read settings before acquiring the lease. If another
        # writer added a top-level key in that interval, the guard correctly
        # refuses the stale payload. Only a payload derived from a recorded
        # read may be retried; a direct destructive payload must remain refused.
        if not isinstance(settings_data, dict) or not snapshot:
            return refuse()
        snapshot_path, base = snapshot
        try:
            current_path = str(SETTINGS_PATH.resolve(strict=False))
        except (OSError, ValueError):
            current_path = str(SETTINGS_PATH)
        if snapshot_path != current_path or not isinstance(base, dict):
            return refuse()

        removed = set(base) - set(settings_data)
        allowed = set(allow_removing_keys or ())
        if removed - allowed:
            return refuse()

        identity = _settings_file_identity()
        fresh, fresh_ok = _read_settings_for_write()
        if not fresh_ok or not isinstance(fresh, dict):
            return refuse()
        changed = {
            key: value
            for key, value in settings_data.items()
            if key not in base or base[key] != value
        }
        merged = dict(fresh)
        merged.update(changed)
        for key in allowed:
            if key not in settings_data:
                merged.pop(key, None)
        if _write_settings_atomic_locked(merged, allow_removing_keys, expect_identity=identity):
            return True
        if getattr(_SETTINGS_WRITE_READ_STATE, "identity_changed", False):
            _SETTINGS_WRITE_READ_STATE.last_refusal = _SETTINGS_CHANGED_REFUSAL
            _report_settings_write_refusal(_SETTINGS_CHANGED_REFUSAL)
        return False


# CLAUDE_AUTOCOMPACT_PCT_OVERRIDE is a DOCUMENTED setting (verified
# 2026-10-10 from code.claude.com/docs/en/model-config#context-window-and-auto-compaction):
# "Set the percentage (1-100) of the compact window already used at which
# auto-compaction runs. The variable can't raise the threshold, so values
# above the default percentage are ignored." Lower = compacts earlier. It
# applies to subagents too. An earlier release misread it as undocumented
# with inverted semantics and AUTO-DELETED it from the user's settings.json;
# that deletion is gone. Token Optimizer only ever EXPLAINS the value
# (see _autocompact_pct_override_explanation), never writes it.
_AUTOCOMPACT_PCT_LOW_NOTE = 25  # below this % of the window, flag "very early"


def _autocompact_pct_override_value(settings=None):
    """Read-only lookup of CLAUDE_AUTOCOMPACT_PCT_OVERRIDE.

    Returns ``(value, source)``: the raw string value and where it came from
    (``"process env"``, ``"settings env"``, or ``(None, None)`` when unset).
    Never writes, never mutates the passed dict.
    """
    env_val = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
    if env_val is not None:
        return env_val, "process env"
    if settings is None:
        settings, _ok = _read_settings_for_write()
    if isinstance(settings, dict):
        env_block = settings.get("env")
        if isinstance(env_block, dict) and "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" in env_block:
            return env_block.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"), "settings env"
    return None, None


def _autocompact_pct_override_explanation(settings=None):
    """Explain-only: what CLAUDE_AUTOCOMPACT_PCT_OVERRIDE does, per the docs.

    Returns a list of check tuples ``(status, name, detail)`` for doctor.
    Read-only: this must never touch settings.json.
    """
    value, source = _autocompact_pct_override_value(settings)
    if value is None:
        return [("OK", "Env vars",
                 "no CLAUDE_AUTOCOMPACT_PCT_OVERRIDE override (not set; "
                 "documented: sets the used %% of the compact window at which "
                 "auto-compaction runs, lower = earlier)")]
    detail = (
        f"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE={value} ({source}) is DOCUMENTED: "
        "auto-compaction runs when this percentage of the compact window is "
        "already USED, so a lower value compacts EARLIER; it cannot raise "
        "the threshold. Kept as-is; Token Optimizer never edits it."
    )
    try:
        pct = int(str(value).strip())
    except (TypeError, ValueError):
        return [("!!", "Env override", detail + " (value is not an integer 1-100)")]
    if not 1 <= pct <= 100:
        return [("!!", "Env override", detail + " (outside the documented 1-100 range)")]
    if pct <= _AUTOCOMPACT_PCT_LOW_NOTE:
        detail += (
            f" NOTE: {pct}% is very low -- sessions will compact at {pct}% "
            "of the compact window, very early. Raise it (or remove the "
            "override) if compactions feel too frequent."
        )
    return [("OK", "Env override", detail)]


def _is_token_optimizer_session_end_hook(hook: dict) -> bool:
    """True when a single hook entry is Token Optimizer's SessionEnd hook.

    Matches the same command signature ``_is_hook_installed`` recognizes:
    a command invoking ``measure.py`` with the ``collect`` or
    ``session-end-flush`` subcommand as its first positional argument
    (``_TO_SESSION_END_CMD_RE``). The path may be quoted. Non-dict entries
    are left untouched (returned False) so a malformed entry never gets
    silently dropped.
    """
    if not isinstance(hook, dict):
        return False
    cmd = hook.get("command", "")
    if not isinstance(cmd, str):
        return False
    return bool(_TO_SESSION_END_CMD_RE.search(cmd))


def _remove_token_optimizer_session_end_hooks(settings: dict) -> int:
    """Strip Token Optimizer's SessionEnd hooks from ``settings`` in place.

    Mirrors the proven filter pattern in ``setup_smart_compact --uninstall``:
    iterate the ``SessionEnd`` event only, drop hook entries whose command is
    Token Optimizer's, keep every other hook (the user's own SessionEnd hooks
    and ALL other hook events are left intact). Empty groups are removed; if
    ``SessionEnd`` ends up empty, the key is dropped. Returns the count of
    Token Optimizer hook entries removed. Never raises.
    """
    if not isinstance(settings, dict):
        return 0
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    session_end = hooks.get("SessionEnd")
    if not isinstance(session_end, list) or not session_end:
        return 0
    removed = 0
    new_groups = []
    for group in session_end:
        if not isinstance(group, dict):
            new_groups.append(group)
            continue
        hook_list = group.get("hooks")
        if not isinstance(hook_list, list):
            new_groups.append(group)
            continue
        kept_hooks = [h for h in hook_list if not _is_token_optimizer_session_end_hook(h)]
        removed += len(hook_list) - len(kept_hooks)
        if kept_hooks:
            group["hooks"] = kept_hooks
            new_groups.append(group)
    if new_groups:
        hooks["SessionEnd"] = new_groups
    else:
        hooks.pop("SessionEnd", None)
    return removed


def _sessionend_has_current_flush(settings: dict) -> bool:
    """True when settings.json already has a session-end-flush SessionEnd hook."""
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return False
    session_end = hooks.get("SessionEnd")
    if not isinstance(session_end, list):
        return False
    for group in session_end:
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks", []) or []:
            if isinstance(hook, dict) and _sessionend_cmd_is_flush(hook.get("command", "")):
                return True
    return False


def _rewrite_collect_fossil_hook(hook: dict) -> dict:
    """Rewrite a collect/dashboard fossil in place to HOOK_COMMAND.

    Preserves type/async/timeout when present. Fossils written before
    d1b0b53 lack async — add it so the host does not wait synchronously.
    """
    rewritten = dict(hook)
    rewritten["type"] = hook.get("type") or "command"
    rewritten["command"] = HOOK_COMMAND
    if "async" not in rewritten:
        rewritten["async"] = True
    return rewritten


def _apply_sessionend_fossil_reconcile(settings, result):
    """Apply the fossil heal mutation to ``settings`` in place.

    Returns True if anything changed. The ``result`` counters (``rewritten`` /
    ``removed`` / ``stop_removed``) are incremented for the changes made in
    THIS call only. Idempotent: a second call on an already-healed settings
    is a no-op and returns False.

    Extracted from ``_reconcile_sessionend_fossils`` so the caller can re-read
    fresh under the lease and re-apply instead of writing a
    snapshot that was read unlocked. If a current-shape session-end-flush
    SessionEnd hook already exists, collect/dashboard fossils are removed.
    Otherwise each fossil is rewritten in place to HOOK_COMMAND (preserving
    async/timeout; adding async:true when absent). Also drops Stop-event
    collect / bare session-end-flush duplicates when the plugin already
    provides Stop.
    """
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return False

    has_current = _sessionend_has_current_flush(settings)
    mutated = False

    session_end = hooks.get("SessionEnd")
    if isinstance(session_end, list) and session_end:
        new_groups = []
        for group in session_end:
            if not isinstance(group, dict):
                new_groups.append(group)
                continue
            hook_list = group.get("hooks")
            if not isinstance(hook_list, list):
                new_groups.append(group)
                continue
            kept = []
            for hook in hook_list:
                if not isinstance(hook, dict):
                    kept.append(hook)
                    continue
                cmd = hook.get("command", "")
                if not _sessionend_cmd_is_collect_fossil(cmd):
                    kept.append(hook)
                    continue
                if has_current:
                    result["removed"] += 1
                    mutated = True
                    continue
                kept.append(_rewrite_collect_fossil_hook(hook))
                result["rewritten"] += 1
                mutated = True
                has_current = True
            if kept:
                group = dict(group)
                group["hooks"] = kept
                new_groups.append(group)
        if mutated:
            if new_groups:
                hooks["SessionEnd"] = new_groups
            else:
                hooks.pop("SessionEnd", None)

    # Stop event: drop collect fossils and extra session-end-flush entries
    # when a plugin-provided (or already-present) Stop flush exists. The
    # first flush-shaped Stop hook is kept; extras and collect fossils go.
    stop = hooks.get("Stop")
    if isinstance(stop, list) and stop:
        plugin_provides_stop_flush = False
        try:
            if _is_plugin_installed():
                plugin_hooks_path = _find_plugin_hooks_json()
                if plugin_hooks_path:
                    plugin_hooks = json.loads(plugin_hooks_path.read_text(encoding="utf-8"))
                    for group in (plugin_hooks.get("hooks") or {}).get("Stop") or []:
                        if not isinstance(group, dict):
                            continue
                        for hook in group.get("hooks") or []:
                            if isinstance(hook, dict) and _sessionend_cmd_is_flush(
                                hook.get("command", "")
                            ):
                                plugin_provides_stop_flush = True
                                break
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            plugin_provides_stop_flush = False

        saw_stop_flush = False
        new_stop_groups = []
        stop_mutated = False
        for group in stop:
            if not isinstance(group, dict):
                new_stop_groups.append(group)
                continue
            hook_list = group.get("hooks")
            if not isinstance(hook_list, list):
                new_stop_groups.append(group)
                continue
            kept = []
            for hook in hook_list:
                if not isinstance(hook, dict):
                    kept.append(hook)
                    continue
                cmd = hook.get("command", "")
                is_collect = _sessionend_cmd_is_collect_fossil(cmd)
                is_flush = _sessionend_cmd_is_flush(cmd)
                if is_collect:
                    result["stop_removed"] += 1
                    stop_mutated = True
                    continue
                if is_flush:
                    if plugin_provides_stop_flush or saw_stop_flush:
                        result["stop_removed"] += 1
                        stop_mutated = True
                        continue
                    saw_stop_flush = True
                kept.append(hook)
            if kept:
                group = dict(group)
                group["hooks"] = kept
                new_stop_groups.append(group)
        if stop_mutated:
            mutated = True
            if new_stop_groups:
                hooks["Stop"] = new_stop_groups
            else:
                hooks.pop("Stop", None)

    return mutated


def _reconcile_sessionend_fossils():
    """Rename-aware heal for the collect && dashboard settings.json fossil.

    Reachability (honest):
      * Plugin installs: the SessionStart ``ensure-health`` hook
        (hooks/hooks.json) calls run_ensure_health, which calls this.
      * NEW script installs: the updated examples/hooks-starter.json
        SessionStart command runs ensure-health, which calls this.
      * EXISTING script installs: their old settings.json has the fossil and
        NO ensure-health hook, so run_ensure_health never runs for them. They
        are reached by the self-heal in _dispatch_collect: when their
        fossil invokes post-fix measure.py ``collect`` on the hook path, that
        run calls this once (throttled, fail-open) and removes/rewrites the
        fossil going forward. A separate pre-fix measure.py copy on disk is
        NOT reached until the user updates -- no code path can reach a copy
        that never runs this module.

    If a current-shape session-end-flush SessionEnd hook already exists,
    fossils are removed. Otherwise each fossil is rewritten in place to
    HOOK_COMMAND (preserving async/timeout; adding async:true when absent).
    Also drops Stop-event collect / bare session-end-flush duplicates when
    the plugin already provides Stop. Atomic, backed up, fail-open. Returns a
    result dict so tests can assert the decision.

    The initial read is unlocked (a cheap probe to
    decide whether any heal work exists, avoiding lease/backup churn when
    there is none). Once work is found, ``_settings_lock()`` is held across
    fresh-read + re-apply + atomic-write as ONE critical section, so a
    concurrent writer (another hook, the user, a settings sync) cannot land
    between the fresh read and the write and be clobbered -- the previous
    code re-read OUTSIDE the lease and only _write_settings_atomic acquired
    it internally, leaving a lost-update window. The write uses
    ``_write_settings_atomic_locked``, which does NOT reacquire the lease
    (the lease is non-reentrant; reacquiring it here would deadlock). A
    denied lease or a bad re-read is a silent no-op (fail-open): the fossil
    persists, which is recoverable; an overwritten settings.json is not.
    """
    result = {"rewritten": 0, "removed": 0, "stop_removed": 0, "reason": "ok"}
    try:
        settings, _, ok = _read_settings_json_checked()
        if not ok:
            result["reason"] = "settings_unreadable"
            return result
        if not settings:
            result["reason"] = "no_settings"
            return result
        if not isinstance(settings.get("hooks"), dict):
            result["reason"] = "no_hooks"
            return result

        # Unlocked probe: decide whether any heal work exists without taking
        # the lease or writing a backup. `settings` is mutated in place here
        # but discarded after the decision -- the authoritative mutation is
        # re-applied to a fresh read under the lease below.
        if not _apply_sessionend_fossil_reconcile(settings, result):
            result["reason"] = "nothing_to_do"
            return result

        # Hold _settings_lock() across fresh-read +
        # re-apply + atomic-write as ONE critical section. The previous code
        # re-read OUTSIDE the lease and only _write_settings_atomic acquired
        # it internally, so a concurrent writer could land between the fresh
        # read and the lease acquisition and be clobbered. The write uses
        # _write_settings_atomic_locked, which performs the tempfile+replace
        # WITHOUT reacquiring _settings_lock() (the lease is non-reentrant;
        # reacquiring it here would deadlock). A denied lease or bad re-read
        # is fail-open (the fossil stays; recoverable) and retries on the
        # next hook fire.
        with _settings_lock() as acquired:
            if not acquired:
                result["reason"] = "write_skipped"
                return result

            # Backup the on-disk state we are about to overwrite, taken UNDER
            # the lease so it reflects exactly the pre-mutation file (a
            # backup taken outside the lease could capture a state a
            # concurrent writer already superseded).
            backup_dir = CLAUDE_DIR / "_backups" / "token-optimizer"
            try:
                backup_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                backup_path = backup_dir / f"settings.json.pre-fossil-reconcile-{ts}"
                if SETTINGS_PATH.exists():
                    import shutil
                    shutil.copy2(str(SETTINGS_PATH), str(backup_path))
            except OSError:
                pass

            # Fresh read UNDER the lease; reset counters so they reflect only
            # this re-application, not the unlocked probe.
            fresh, _, read_ok = _read_settings_json_checked()
            result["rewritten"] = 0
            result["removed"] = 0
            result["stop_removed"] = 0
            if not read_ok or not fresh:
                result["reason"] = "write_skipped"
                return result
            if not isinstance(fresh.get("hooks"), dict):
                result["reason"] = "no_hooks"
                return result
            if not _apply_sessionend_fossil_reconcile(fresh, result):
                result["reason"] = "nothing_to_do"
                return result
            if not _write_settings_atomic_locked(fresh):
                result["reason"] = "write_skipped"
                return result
        return result
    except Exception:
        result["reason"] = "fail_open"
        return result


def _maybe_self_heal_sessionend_fossils_on_hook():
    """Hook-path self-heal for the collect/dashboard fossil.

    The SessionStart ``ensure-health`` hook reaches plugin installs and NEW
    script installs (via the updated examples/hooks-starter.json). An EXISTING
    script install has an old settings.json with the ``collect && dashboard``
    fossil and NO ``ensure-health`` hook, so ``run_ensure_health`` never runs
    for it and ``_reconcile_sessionend_fossils`` is unreachable from there.
    The fossil's OWN run closes that gap: when post-fix measure.py runs
    ``collect`` on the hook path, this opportunistically calls
    ``_reconcile_sessionend_fossils`` once, so the fossil's own execution
    removes/rewrites it in settings.json going forward -- reaching the
    population the SessionStart hook cannot.

    Throttled to once per 24h via the shared ``last_hook_heal_check`` config
    flag (the same flag run_ensure_health uses), so it does not run on every
    hook fire. Fail-open and time-bounded: any error is swallowed so a
    reconcile failure can never affect the hook flush, and the reconcile runs
    under its own 10s HookDeadline so a stalled settings write cannot hold the
    hook's stdout pipe open (os._exit(0) on expiry EOFs the pipe). A lease miss
    (``reason == "write_skipped"``) leaves the flag untouched so the next
    hook fire retries; any other outcome (healed, nothing-to-do,
    unreadable) advances the flag to suppress redundant retries for 24h.

    Never raises. Called from ``_dispatch_collect`` AFTER the collect flush
    and its 20s budget are cleared, so it never delays or blocks the flush.
    """
    # Bound the ENTIRE self-heal with one short deadline, installed before the
    # first filesystem touch (the throttle read) and cleared only after the
    # last (the throttle write). Every step here -- _read_config_flag, the
    # reconcile's settings read/backup/write, and _write_config_flag's own
    # dir-create/lease/temp-write/os.replace -- is synchronous I/O that, on a
    # stalled filesystem, would otherwise hold the hook's stdout pipe open
    # unbounded and re-wedge the very hang the fossil fix prevents (the collect budget was
    # already cleared by the caller). HookDeadline os._exit(0)s on expiry, so
    # the process exits and the pipe EOFs no matter WHICH step stalls; the flag
    # stays unadvanced so the heal retries on the next hook fire.
    deadline = _install_hook_budget(10)
    try:
        last_check = _read_config_flag("last_hook_heal_check", 0)
        now = int(time.time())
        if now - int(last_check or 0) <= 86400:  # 24h
            return
        fossil = _reconcile_sessionend_fossils()
        # write_skipped = lease denied or fresh read failed -> retry next fire.
        if fossil.get("reason", "ok") != "write_skipped":
            _write_config_flag("last_hook_heal_check", now)
    except Exception:
        pass
    finally:
        _clear_hook_budget(deadline)


def setup_hook(dry_run=False, uninstall=False, user_initiated=False):
    """Install or uninstall the SessionEnd hook for automatic usage collection.

    ``uninstall=True`` removes ONLY Token Optimizer's own SessionEnd hook
    entries from ``settings.json`` (commands containing ``measure.py`` plus
    ``collect`` or ``session-end-flush``), leaving the user's other SessionEnd
    hooks and all other hook events intact. Mirrors the proven filter pattern
    in ``setup_smart_compact --uninstall``. Supports ``--dry-run``.

    setup-hook targets ~/.claude/settings.json with a Claude Code hook that
    uses {"async": true}. Async hooks are a Claude Code feature; Codex skips
    them (see codex_doctor) and other runtimes use their own installers. So
    never write this hook under a non-Claude runtime — Codex must go through
    codex-install, which writes a synchronous .codex/hooks.json entry.
    """
    if detect_runtime() != "claude":
        # Informational on every path (including dry-run / uninstall) so a
        # non-Claude invocation isn't a silent no-op.
        print(
            "[Token Optimizer] setup-hook targets Claude Code (settings.json). "
            "For Codex use codex-install; other runtimes use their own installer."
        )
        return

    # Load existing settings
    settings = {}
    if SETTINGS_PATH.exists():
        if not _is_regular_file(SETTINGS_PATH):
            # Never open() a FIFO: it blocks forever.
            print(f"[Error] Could not read {SETTINGS_PATH}: it is not a regular file.")
            sys.exit(1)
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8-sig") as f:
                settings = json.load(f)
        except (json.JSONDecodeError, PermissionError, OSError) as e:
            print(f"[Error] Could not read {SETTINGS_PATH}: {e}")
            sys.exit(1)

    if uninstall:
        removed = _remove_token_optimizer_session_end_hooks(settings)
        if dry_run:
            print(f"\n  [Dry run] Would remove {removed} Token Optimizer SessionEnd hook(s) from {SETTINGS_PATH}")
            print("  Run without --dry-run to apply.\n")
            return
        if removed == 0:
            print("[Token Optimizer] No Token Optimizer SessionEnd hook found in settings.json. Nothing to remove.")
            return
        # Backup settings.json before mutating (mirrors the install path).
        backup_dir = CLAUDE_DIR / "_backups" / "token-optimizer"
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = backup_dir / f"settings.json.pre-hook-uninstall-{ts}"
        if SETTINGS_PATH.exists():
            import shutil
            shutil.copy2(str(SETTINGS_PATH), str(backup_path))
        try:
            if not _write_settings_atomic(settings, user_initiated=user_initiated):
                print("[Error] settings.json write was refused; no SessionEnd hooks were removed.", file=sys.stderr)
                return
            print(f"[Token Optimizer] Removed {removed} Token Optimizer SessionEnd hook(s) from settings.json.")
            print(f"  Backup: {backup_path}")
            print("  Other hooks (yours and other tools') were left intact.")
        except PermissionError:
            print(f"[Error] Permission denied writing {SETTINGS_PATH}.")
            sys.exit(1)
        return

    # Check if hook is installed and whether it needs upgrading
    installed = _is_hook_installed(settings)
    current = _is_hook_current(settings)

    # Plugin users get this hook from hooks.json — skip writing to settings.json
    is_plugin = _is_running_from_plugin_cache() or _is_plugin_installed()
    if is_plugin:
        if installed:
            print("[Token Optimizer] SessionEnd hook active via plugin hooks.json. Nothing to do.")
        else:
            print("[Token Optimizer] Running as plugin. SessionEnd hook managed by hooks.json.")
        return

    if installed and current:
        print("[Token Optimizer] SessionEnd hook already installed and up to date. Nothing to do.")
        return

    upgrading = installed and not current

    # Build the hook entry
    new_hook = {"type": "command", "command": HOOK_COMMAND, "async": True}

    if "hooks" not in settings:
        settings["hooks"] = {}

    hooks = settings["hooks"]

    if upgrading:
        # Replace the collect/dashboard fossil with session-end-flush.
        session_end = hooks.get("SessionEnd", [])
        if isinstance(session_end, list):
            for entry in session_end:
                hook_list = entry.get("hooks", []) if isinstance(entry, dict) else []
                for i, hook in enumerate(hook_list):
                    cmd = hook.get("command", "") if isinstance(hook, dict) else ""
                    if _sessionend_cmd_is_collect_fossil(cmd) or (
                        "measure.py" in cmd and "collect" in cmd
                    ):
                        hook_list[i] = new_hook
                        break
    elif "SessionEnd" not in hooks:
        hooks["SessionEnd"] = [{"hooks": [new_hook]}]
    else:
        session_end = hooks["SessionEnd"]
        if isinstance(session_end, list) and len(session_end) > 0:
            first_entry = session_end[0]
            if isinstance(first_entry, dict):
                if "hooks" not in first_entry:
                    first_entry["hooks"] = []
                first_entry["hooks"].append(new_hook)
            else:
                session_end.append({"hooks": [new_hook]})
        else:
            hooks["SessionEnd"] = [{"hooks": [new_hook]}]

    if dry_run:
        action = "upgrade" if upgrading else "install"
        print(f"[Token Optimizer] Dry run. Would {action} a SessionEnd hook.\n")
        print("  What it does:")
        print("    When you close a Claude Code session, it automatically:")
        print("    1. Saves your session stats (skills used, tokens, model mix)")
        print("    2. Refreshes your dashboard with the latest data\n")
        print("  Where data is stored:")
        print(f"    {SNAPSHOT_DIR / 'trends.db'}")
        print(f"    {DASHBOARD_PATH}\n")
        print("  JSON that would be added to settings.json:")
        print(json.dumps(hooks.get("SessionEnd", []), indent=2))
        print("\n  No changes written.")
        return

    # Backup settings.json
    backup_dir = CLAUDE_DIR / "_backups" / "token-optimizer"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"settings.json.pre-hook-{ts}"
    if SETTINGS_PATH.exists():
        import shutil
        shutil.copy2(str(SETTINGS_PATH), str(backup_path))

    # Write atomically
    try:
        if not _write_settings_atomic(settings, user_initiated=user_initiated):
            print("[Error] settings.json write was refused; SessionEnd hook was not installed.", file=sys.stderr)
            return
        action = "upgraded" if upgrading else "installed"
        print(f"[Token Optimizer] SessionEnd hook {action}.")
        print(f"  Backup: {backup_path}")
        print("  Hook collects data + regenerates dashboard after each session.")
        print(f"  Dashboard: {DASHBOARD_PATH}")
    except PermissionError:
        print(f"[Error] Permission denied writing {SETTINGS_PATH}.")
        print("Add this manually to your settings.json hooks.SessionEnd:\n")
        print(json.dumps({"type": "command", "command": HOOK_COMMAND, "async": True}, indent=2))
        sys.exit(1)


# ========== Persistent Dashboard Daemon ==========

_read_plugin_version = _measure_compat.bind(_measure_providers['daemon']._read_plugin_version, __name__, defaults=("0.0.0",))


TOKEN_OPTIMIZER_VERSION = _read_plugin_version()
_DASHBOARD_CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
# Per-runtime daemon identity. Each runtime gets a distinct port + label so a
# dashboard under one runtime never collides with another's. Copilot uses 24845
# (matching copilot_doctor.DAEMON_PORT); the `dashboard` command is also blocked
# for foreign runtimes in _CLAUDE_TARGET_CMDS, so this is defense-in-depth.
_DAEMON_RUNTIME = detect_runtime()
_DAEMON_SUFFIX_BY_RUNTIME = {"codex": "codex", "hermes": "hermes", "copilot": "copilot", "cursor": "cursor", "antigravity": "antigravity", "grok": "grok"}
_DAEMON_PORT_BY_RUNTIME = {"codex": 24843, "hermes": 24844, "copilot": 24845, "cursor": 24846, "antigravity": 24847, "grok": 24848}
_DAEMON_RUNTIME_SUFFIX = _DAEMON_SUFFIX_BY_RUNTIME.get(_DAEMON_RUNTIME, "claude")
DAEMON_LABEL = (
    f"com.token-optimizer.{_DAEMON_RUNTIME_SUFFIX}-dashboard"
    if _DAEMON_RUNTIME_SUFFIX != "claude"
    else "com.token-optimizer.dashboard"
)
DAEMON_PORT = _DAEMON_PORT_BY_RUNTIME.get(_DAEMON_RUNTIME, 24842)
LAUNCH_AGENTS_DIR = _safe_home() / "Library" / "LaunchAgents"
PLIST_PATH = LAUNCH_AGENTS_DIR / f"{DAEMON_LABEL}.plist"
DAEMON_LOG_DIR = SNAPSHOT_DIR / "logs"
DAEMON_TOKEN_PATH = SNAPSHOT_DIR / "daemon-token"  # 0600, per-install CSRF secret
# v5.11.1: persisted bind-host for the daemon. setup-daemon writes the
# effective TOKEN_OPTIMIZER_DASHBOARD_HOST here so the launchd/systemd/Task
# Scheduler daemon -- which runs with an empty environment and therefore never
# sees the env var -- can read it at runtime. The generated script also reads
# this file, so the ensure-health auto-regen path keeps honoring it across
# version bumps without baking the value into the script (which would regress
# to 127.0.0.1 on the next regen).
DAEMON_HOST_PATH = SNAPSHOT_DIR / "dashboard-host"
# Hosts the daemon is allowed to BIND. v5.11.1: "::1" is intentionally
# excluded -- the daemon uses socketserver.TCPServer which is AF_INET, so a
# "::1" bind raises OSError -> sys.exit(0) -> silent daemon death (confirmed
# live). This is the bind allowlist only; the generated script's
# _LOCALHOST_ADDRS still includes "::1" because that list validates Host
# HEADERS, not bind addresses.
_DAEMON_HOST_ALLOWLIST = ("127.0.0.1", "localhost", "0.0.0.0")
DAEMON_THRASH_BREADCRUMB = SNAPSHOT_DIR / ".daemon-thrash"  # tombstone
# The .daemon-thrash breadcrumb is written by TWO
# distinct paths with opposite lifetimes. The 3-strikes thrash path writes an
# EMPTY file (a transient "dashboard missing" tombstone that legitimately
# self-heals after 60s once the dashboard reappears -- e.g. an update window).
# The UNINSTALL path must write a tombstone that OUTLIVES the uninstall and is
# cleared ONLY by a reinstall (setup_daemon unlinks it). A size-0 file could not
# tell the two apart, so a surviving daemon script self-cleared the uninstall
# tombstone after 60s and minted a fresh CSRF token post-uninstall. Give the
# uninstall tombstone distinctive content so the daemon can honor it forever.
_UNINSTALL_TOMBSTONE_MARKER = "uninstalled"

# STICKY install-failed marker. Lives in the same state dir as the
# .daemon-thrash tombstone and follows the same convention (a dot-prefixed
# breadcrumb whose PRESENCE is authoritative, independent of config.json, which
# a corrupt read could silently flip back to permissive).
#
# Why it exists: before this, a daemon install that failed for a structural
# reason (no `schtasks` on the PATH, a locked SNAPSHOT_DIR, a Python that cannot
# spawn) was retried by `_ensure_dashboard_daemon` on the next SessionStart and
# by `_daemon_midsession_pulse` every ~5min. On Windows each retry shells out to
# schtasks/netstat, so a permanently-broken install flashed a cmd window on
# essentially every prompt, forever. The throttles bound the RATE, not the
# lifetime -- they never stop.
#
# Lifetime: armed ONLY by DEFINITIVE, permanent
# failure classes -- an MS-Store Python alias (structurally impossible install),
# schtasks missing from the machine, or task creation denied by policy
# ("Access is denied"). TRANSIENT classes (a subprocess timeout, a one-off
# nonzero exit, a fork EAGAIN, a lost install-lock race, a momentarily-bound
# port, a missing dashboard file) must NEVER arm it -- those are already
# bounded by the 24h ensure throttle / 300s revive throttle / 24h heal
# throttle, and a single launchctl hiccup on macOS must not permanently kill a
# daemon over a Windows-only cosmetic bug. It never expires on time, but it IS
# cleared by any later VERIFIED success: an explicit `setup-daemon` that
# succeeds, or the ensure path observing a live, identity-verified daemon
# (which disproves the "structurally broken" claim -- e.g. the install-lock
# race where the loser armed it while the winner installed fine).
DAEMON_INSTALL_FAILED_BREADCRUMB = SNAPSHOT_DIR / ".daemon-install-failed"

DAEMON_IDENTITY_MAGIC = (
    f"token-optimizer-{_DAEMON_RUNTIME_SUFFIX}-dashboard-v1"
    if _DAEMON_RUNTIME_SUFFIX != "claude"
    else "token-optimizer-dashboard-v1"
)

# v5.11.68: every runtime suffix that can register a daemon
# scheduler artifact. Daemon uninstall sweeps ALL of these by name so a
# scheduler registration whose plugin-data dir already vanished (e.g. a
# sibling identity removed by the platform's own GC) still gets unregistered,
# not just the currently-resolved runtime's. The per-runtime label/unit/task
# names are derived from the suffix the same way DAEMON_LABEL /
# SYSTEMD_UNIT_NAME / WINDOWS_TASK_NAME are above.
_DAEMON_ALL_SUFFIXES = ("claude", "codex", "hermes", "copilot", "cursor", "antigravity", "grok")
_ALL_LAUNCH_AGENT_LABELS = tuple(
    "com.token-optimizer.dashboard" if s == "claude"
    else f"com.token-optimizer.{s}-dashboard"
    for s in _DAEMON_ALL_SUFFIXES
)
_ALL_SYSTEMD_UNIT_NAMES = tuple(
    "token-optimizer-dashboard.service" if s == "claude"
    else f"token-optimizer-{s}-dashboard.service"
    for s in _DAEMON_ALL_SUFFIXES
)
# Windows task names mirror the WINDOWS_TASK_NAME ternary below: copilot and
# opencode have no dedicated branch there (they fall into the `else` ->
# TokenOptimizerDashboard bucket), so the variant set is: Claude (+ copilot/
# opencode fallback), Codex, Hermes, and Antigravity.
_ALL_WINDOWS_TASK_NAMES = (
    "TokenOptimizerDashboard",
    "TokenOptimizerCodexDashboard",
    "TokenOptimizerHermesDashboard",
    "TokenOptimizerAntigravityDashboard",
)


_scheduler_names_to_sweep = _measure_compat.bind(_measure_providers['daemon']._scheduler_names_to_sweep, __name__)


_sweep_identity_daemon_files = _measure_compat.bind(_measure_providers['daemon']._sweep_identity_daemon_files, __name__)


_print_identity_sweep_report = _measure_compat.bind(_measure_providers['daemon']._print_identity_sweep_report, __name__)


_daemon_per_identity_files = _measure_compat.bind(_measure_providers['daemon']._daemon_per_identity_files, __name__)


_daemon_identity_snapshot_dirs = _measure_compat.bind(_measure_providers['daemon']._daemon_identity_snapshot_dirs, __name__)


_daemon_sweep_dir_is_safe = _measure_compat.bind(_measure_providers['daemon']._daemon_sweep_dir_is_safe, __name__)


_unlink_if_exists = _measure_compat.bind(_measure_providers['daemon']._unlink_if_exists, __name__)


_unlink_reporting = _measure_compat.bind(_measure_providers['daemon']._unlink_reporting, __name__)



_get_or_create_daemon_token = _measure_compat.bind(_measure_providers['daemon']._get_or_create_daemon_token, __name__)


_read_state_file = _measure_compat.bind(_measure_providers['daemon']._read_state_file, __name__)


_read_daemon_token = _measure_compat.bind(_measure_providers['daemon']._read_daemon_token, __name__)


_read_dashboard_host_file = _measure_compat.bind(_measure_providers['daemon']._read_dashboard_host_file, __name__)


_persist_dashboard_host = _measure_compat.bind(_measure_providers['daemon']._persist_dashboard_host, __name__, defaults=(True,))


_is_localhost_host_header = _measure_compat.bind(_measure_providers['daemon']._is_localhost_host_header, __name__)


_generate_daemon_script = _measure_compat.bind(_measure_providers['daemon']._generate_daemon_script, __name__)


_write_file_if_changed = _measure_compat.bind(_measure_providers['daemon']._write_file_if_changed, __name__, defaults=(None,))


_launchagent_loaded = _measure_compat.bind(_measure_providers['daemon']._launchagent_loaded, __name__)


_generate_plist = _measure_compat.bind(_measure_providers['daemon']._generate_plist, __name__)


_HOOKS_JSON_CANDIDATES = [
    # Script install path (installed by install.sh to ~/.claude/token-optimizer)
    RUNTIME_DIR / "token-optimizer" / "hooks" / "hooks.json",
    # Dev path (symlinked/local checkout)
    Path(__file__).resolve().parents[3] / "hooks" / "hooks.json",
]


def _find_plugin_hooks_json():
    """Locate the plugin's hooks.json. Returns Path or None."""
    for candidate in _HOOKS_JSON_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def _resolve_hook_command(template_cmd, plugin_root):
    """Resolve ${CLAUDE_PLUGIN_ROOT} and similar placeholders to absolute paths.

    Script installs don't use Claude Code's plugin loader, so we need to
    substitute ${CLAUDE_PLUGIN_ROOT} ourselves with the install directory.

    Claude Code runs hooks through Git Bash on Windows too, so the bash
    launcher template is already the correct form there. The only
    Windows-specific hardening is substituting a forward-slash root, so
    backslash sequences in user names (\\t, \\n, ...) can't be mangled inside
    bash double quotes. The pre-fix native cmd.exe conversion (list2cmdline +
    cmd null redirect) created a literal NUL file and silently broke hooks.
    """
    if not template_cmd:
        return template_cmd
    root = str(plugin_root)
    if platform.system() == "Windows":
        root = root.replace("\\", "/")
    # No shell-metachar escaping needed here: the sole caller (setup_all_hooks)
    # rejects roots containing ' " ` $ ; & | < > (SEC-001) before this runs, so
    # the substituted root is always inert in the bash double-quote context.
    resolved = template_cmd.replace("${CLAUDE_PLUGIN_ROOT}", root)
    resolved = resolved.replace("$CLAUDE_PLUGIN_ROOT", root)
    return resolved


def _windows_hook_command_is_stale(existing_cmd, resolved_cmd):
    """True when SessionStart should replace a legacy Windows hook command.

    Legacy means the pre-fix native cmd.exe form (list2cmdline argv + a cmd
    null redirect), which fails under Git Bash. The current form is the bash
    launcher, identical in shape to POSIX.
    """
    return (
        platform.system() == "Windows"
        and existing_cmd != resolved_cmd
        and re.search(r">\s*NUL\b", existing_cmd) is not None
    )


# Flags that don't distinguish functionally different hooks (output control only).
# These are stripped when computing hook identity for dedup.
_COSMETIC_FLAGS = frozenset({"--quiet", "-q", "--warn", "--verbose", "-v"})

# Shell noise tokens stripped during subshell identity extraction.
_SHELL_NOISE = frozenset({
    "2>/dev/null", "||", "true", "&&", "|", "sort",
    "tail", "-V", "-1", "-n", "head",
})


def _hook_command_identity(cmd):
    """Extract a stable functional identity from a hook command for dedup.

    Tokenizes with shlex, finds the LAST .py file (skipping `test -f ...`
    guards), and returns: script_name + first subcommand + first mode flag,
    ignoring cosmetic flags like --quiet/--warn/--verbose.

    Examples:
      'python3 measure.py quality-cache --warn --quiet' -> 'measure.py:quality-cache'
      'python3 measure.py quality-cache --quiet' -> 'measure.py:quality-cache' (DEDUPS)
      'python3 read_cache.py --quiet' -> 'read_cache.py'
      'python3 read_cache.py --clear --quiet' -> 'read_cache.py:--clear'
      'python3 read_cache.py --invalidate --quiet' -> 'read_cache.py:--invalidate'
      'echo COMPACTION GUIDANCE...' -> 'echo:COMPACTION'
    """
    if not cmd:
        return ""

    # The plugin's version resolver (D="$(ls -d ... | sort -V | tail -n1)") is
    # plumbing, not identity: drop that one flat $(...) so a resolver command
    # keeps the identity of its handler instead of collapsing to "run.py" for
    # every hook (which would make setup_all_hooks/dedup treat all of an event's
    # hooks as one).
    if 'D="$(ls -d ' in cmd:
        cmd = re.sub(r'D="\$\(ls -d [^()]*\)"', 'D=""', cmd)

    # Subshell fallback: commands with $(...) or backticks confuse shlex —
    # shell noise (2>/dev/null, |, sort, -V) leaks into identity tokens.
    # Extract .py name via regex and find subcommand/flags after the closing ).
    # Uses re.search (first match) unlike the shlex path (last .py token);
    # known malformed patterns have only one .py token so this is equivalent.
    if "$(" in cmd or "`" in cmd:
        py_match = re.search(r'([\w.-]+\.py)', cmd)
        if py_match:
            script_name = py_match.group(1)
            # Find tokens after the subshell close (after last ) or `)
            after = ""
            last_paren = cmd.rfind(")")
            last_bt = cmd.rfind("`")
            cut = max(last_paren, last_bt)
            if cut >= 0 and cut + 1 < len(cmd):
                after = cmd[cut + 1:]
            after_tokens = after.split()
            clean = [t for t in after_tokens
                     if t not in _COSMETIC_FLAGS and t not in _SHELL_NOISE
                     and not t.startswith("2>")]
            subcmd = ""
            mode_flag = ""
            for t in clean:
                if not t.startswith("-"):
                    subcmd = t
                    break
            for t in clean:
                if t.startswith("-") and t not in _COSMETIC_FLAGS:
                    mode_flag = t
                    break
            if subcmd and mode_flag:
                return f"{script_name}:{subcmd}:{mode_flag}"
            elif subcmd:
                return f"{script_name}:{subcmd}"
            elif mode_flag:
                return f"{script_name}:{mode_flag}"
            return script_name

    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        return cmd[:80]

    # Strip `|| true` / `&&` trailers
    while tokens and tokens[-1] in ("||", "true", "&&"):
        tokens.pop()

    # Find the LAST .py token (skips `test -f '<path>.py'` guard)
    py_idx = -1
    for i, t in enumerate(tokens):
        if t.endswith(".py"):
            py_idx = i

    if py_idx < 0:
        # Non-python hook (echo, curl, etc). Use script name + first meaningful token.
        for i, t in enumerate(tokens):
            if t not in ("test", "-f", "&&", "||", "true", "python3", "python", "bash", "-c"):
                # Found the command name -- use it + next word as identity
                next_word = tokens[i + 1] if i + 1 < len(tokens) else ""
                # For echo, use first word of content as discriminator
                if t == "echo" and next_word:
                    first_word = next_word.split()[0] if next_word.split() else ""
                    return f"echo:{first_word[:20]}"
                return f"{t}:{next_word[:20]}"
        return cmd[:80]

    script_name = Path(tokens[py_idx]).name
    tail = tokens[py_idx + 1:]
    if not tail:
        return script_name

    # First non-flag token = subcommand (e.g. "quality-cache", "compact-capture")
    subcmd = ""
    for t in tail:
        if not t.startswith("-"):
            subcmd = t
            break

    # First MODE flag (non-cosmetic) = discriminator for scripts without subcommands
    mode_flag = ""
    for t in tail:
        if t.startswith("-") and t not in _COSMETIC_FLAGS:
            mode_flag = t
            break

    # Always include mode_flag in identity when present, even if
    # subcmd exists. This distinguishes 'measure.py compact-restore' from
    # 'measure.py compact-restore --new-session-only' which otherwise collide.
    if subcmd and mode_flag:
        return f"{script_name}:{subcmd}:{mode_flag}"
    elif subcmd:
        return f"{script_name}:{subcmd}"
    elif mode_flag:
        return f"{script_name}:{mode_flag}"
    else:
        return script_name


def _cleanup_duplicate_plugin_hooks_from_settings(dry_run=False):
    """Remove token-optimizer hook commands from settings.json that the plugin
    already provides via its hooks.json.

    Context: users who installed via install.sh before the marketplace plugin
    existed (or alongside it) have settings.json entries AND plugin hooks.json
    entries for the same commands. Claude Code merges both, so every hook fires
    twice per event — wasted CPU, racy SQLite writes, undercounted savings.

    This is the inverse of setup_all_hooks: when the plugin is installed, we
    remove the duplicates from settings.json so the plugin's copy is the single
    source of truth.

    Safety:
    - Only runs when _is_plugin_installed() is True (refuses otherwise).
    - Only removes hooks whose (event, matcher, identity) tuple EXACTLY matches
      an entry in plugin hooks.json. User-custom or third-party hooks are
      preserved untouched.
    - Identity is computed via _hook_command_identity() which normalizes
      cosmetic flags (--quiet/--warn) so 'quality-cache --warn --quiet' and
      'quality-cache --quiet' dedup to the same identity.
    - Atomic: uses _write_settings_atomic with file lock.
    - Idempotent: returns {"removed": 0} when there are no duplicates to remove.
    - Preserves all hook fields and ordering for kept entries.

    Returns: {"removed": N, "reason": str, "dry_run": bool}
    """
    if not _is_plugin_installed():
        return {"removed": 0, "reason": "plugin_not_installed", "dry_run": dry_run}

    hooks_json_path = _find_plugin_hooks_json()
    if not hooks_json_path:
        return {"removed": 0, "reason": "plugin_hooks_json_not_found", "dry_run": dry_run}

    try:
        plugin_hooks = json.loads(hooks_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {"removed": 0, "reason": f"plugin_hooks_json_read_error: {e}", "dry_run": dry_run}

    desired = plugin_hooks.get("hooks", {})
    if not desired:
        return {"removed": 0, "reason": "plugin_hooks_empty", "dry_run": dry_run}

    # Build the set of (event, matcher, identity) tuples that the plugin provides.
    # Any settings.json hook matching one of these is a duplicate we can remove.
    plugin_identities = set()
    for event, handler_groups in desired.items():
        if not isinstance(handler_groups, list):
            continue
        for group in handler_groups:
            if not isinstance(group, dict):
                continue
            matcher = group.get("matcher", "")
            for h in group.get("hooks", []):
                if not isinstance(h, dict):
                    continue
                ident = _hook_command_identity(h.get("command", ""))
                if ident:
                    plugin_identities.add((event, matcher, ident))

    if not plugin_identities:
        return {"removed": 0, "reason": "no_plugin_identities", "dry_run": dry_run}

    # Read current settings.
    # Writes a dict derived from `current` back.
    current, _rs_ok = _read_settings_for_write()
    if not _rs_ok:
        current = {}
    current_hooks = current.get("hooks") if current else None
    if not current_hooks:
        return {"removed": 0, "reason": "no_settings_hooks", "dry_run": dry_run}

    # Safety: if settings.json exists non-empty but parsed empty,
    # refuse to touch it — concurrent write may have corrupted it.
    if not current:
        try:
            if SETTINGS_PATH.exists() and SETTINGS_PATH.stat().st_size > 0:
                return {"removed": 0, "reason": "settings_corrupted", "dry_run": dry_run}
        except OSError:
            pass

    # Walk each event and drop hooks whose identity matches the plugin.
    # Preserve all other hooks (including non-token-optimizer ones) untouched.
    removed = 0
    new_hooks = {}
    for event, handler_groups in current_hooks.items():
        if not isinstance(handler_groups, list):
            # Unknown shape — preserve as-is, don't touch it.
            new_hooks[event] = handler_groups
            continue
        new_groups = []
        for group in handler_groups:
            if not isinstance(group, dict):
                new_groups.append(group)
                continue
            matcher = group.get("matcher", "")
            original_hook_list = group.get("hooks", [])
            if not isinstance(original_hook_list, list):
                new_groups.append(group)
                continue
            kept_hooks = []
            for h in original_hook_list:
                if not isinstance(h, dict):
                    kept_hooks.append(h)
                    continue
                ident = _hook_command_identity(h.get("command", ""))
                if ident and (event, matcher, ident) in plugin_identities:
                    removed += 1
                    continue
                kept_hooks.append(h)
            # Only keep the group if it still has hooks; an empty "hooks" array
            # is legal but pointless, so drop the whole group in that case.
            if kept_hooks:
                new_group = dict(group)
                new_group["hooks"] = kept_hooks
                new_groups.append(new_group)
        if new_groups:
            new_hooks[event] = new_groups
        # If every group in this event was removed, drop the event key too.

    if removed == 0:
        return {"removed": 0, "reason": "no_duplicates_found", "dry_run": dry_run}

    if dry_run:
        return {"removed": removed, "reason": "dry_run", "dry_run": True}

    new_settings = dict(current)
    new_settings["hooks"] = new_hooks
    try:
        if not _write_settings_atomic(new_settings):
            return {"removed": 0, "reason": "write_failed", "dry_run": False}
    except (PermissionError, OSError) as e:
        return {"removed": 0, "reason": f"write_failed: {e}", "dry_run": False}

    return {"removed": removed, "reason": "success", "dry_run": False}


# Hook commands written by the Claude Code cloud kit (cloud_kit.py, run by the
# npm package's install-cloud.sh) all run this runner.
_CLOUD_KIT_RUNNER_MARKER = "/token-optimizer-cloud-run.sh"


def _cloud_kit_owns_hooks():
    """True when the Claude Code cloud kit wired this machine's hooks.

    In a cloud VM the kit's run.sh launches the real hooks with
    CLAUDE_PLUGIN_ROOT pointing at the npm package, and nothing marks that as a
    plugin install. Without this check the SessionStart self-heal sees "script
    install with missing hooks" and adds its canonical set next to the kit's,
    so every hook fires twice. The kit owns the hook map (generated from
    hooks.json and refreshed on every install), so self-heal stands down.
    Signals: the env var run.sh exports, or a kit runner in settings.json.
    """
    if os.environ.get("TOKEN_OPTIMIZER_CLOUD_KIT_VERSION"):
        return True
    try:
        current, _ok = _read_settings_for_write()
        hooks = (current or {}).get("hooks") or {}
        if not isinstance(hooks, dict):
            return False
        for groups in hooks.values():
            for group in groups if isinstance(groups, list) else []:
                for h in (group.get("hooks") or []) if isinstance(group, dict) else []:
                    if isinstance(h, dict) and _CLOUD_KIT_RUNNER_MARKER in str(h.get("command", "")):
                        return True
    except Exception:
        return False
    return False


def setup_all_hooks(dry_run=False, verbose=False):
    """Merge the plugin's hooks.json into the user's settings.json.

    This is the canonical way to install all Token Optimizer hooks for
    script-install users (install.sh) and for healing drift on existing
    installs (called from ensure-health at SessionStart).

    Returns a dict with counts: {"added": N, "skipped": N, "plugin_root": str}.

    Safety:
    - Idempotent: dedups by script identity, never adds duplicates
    - Atomic: uses _write_settings_atomic with file lock
    - Preserves all existing hooks (token-optimizer + third-party)
    - Preserves all hook fields (type, command, async, timeout, etc.)
    - Resolves ${CLAUDE_PLUGIN_ROOT} to absolute path for script installs
    - Refuses to write if settings.json appears corrupted (non-empty file but parses to {})
    - Rejects plugin_root containing shell metacharacters
    """
    if _cloud_kit_owns_hooks():
        if verbose:
            print("  [setup-all-hooks] hooks are managed by the Claude Code cloud kit; nothing to do")
        return {"added": 0, "skipped": 0, "plugin_root": None, "reason": "cloud_kit"}
    hooks_json_path = _find_plugin_hooks_json()
    if not hooks_json_path:
        if verbose:
            print("  [setup-all-hooks] plugin hooks.json not found")
        return {"added": 0, "skipped": 0, "plugin_root": None, "error": "hooks.json not found"}

    plugin_root = hooks_json_path.parent.parent
    plugin_root_str = str(plugin_root)

    # SEC-001 fix: reject plugin_root with shell metacharacters that would break
    # out of single-quoted strings in hook templates or inject commands.
    # v5.4.8: backslash is excluded on Windows since it's the legitimate path
    # separator (C:\Users\...\.claude\plugins\...). The remaining chars still
    # block shell-injection vectors on both platforms. UNC paths (\\server\...)
    # are rejected explicitly to prevent SMB auth leak via hook execution.
    _DANGEROUS_PATH_CHARS = set("'\"`$;&|<>\n\r\x00")
    if platform.system() != "Windows":
        _DANGEROUS_PATH_CHARS.add("\\")
    elif plugin_root_str.startswith("\\\\") or plugin_root_str.startswith("//"):
        msg = f"Plugin root is a UNC path, refusing to install hooks: {plugin_root_str!r}"
        if verbose:
            print(f"  [setup-all-hooks] {msg}")
        return {"added": 0, "skipped": 0, "plugin_root": plugin_root_str, "error": msg}
    if any(c in plugin_root_str for c in _DANGEROUS_PATH_CHARS):
        msg = f"Plugin root contains unsafe characters: {plugin_root_str!r}"
        if verbose:
            print(f"  [setup-all-hooks] {msg}")
        return {"added": 0, "skipped": 0, "plugin_root": plugin_root_str, "error": msg}

    try:
        plugin_hooks = json.loads(hooks_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        if verbose:
            print(f"  [setup-all-hooks] could not read {hooks_json_path}: {e}")
        return {"added": 0, "skipped": 0, "plugin_root": plugin_root_str, "error": str(e)}

    desired = plugin_hooks.get("hooks", {})
    if not desired:
        return {"added": 0, "skipped": 0, "plugin_root": plugin_root_str}

    # Read current settings
    # Writes a dict derived from `current` back.
    current, _rs_ok = _read_settings_for_write()
    if not _rs_ok:
        current = {}

    # If settings.json exists with non-zero size but parsed as empty,
    # it was probably corrupted by a concurrent write. Refuse to overwrite.
    if not current:
        try:
            if SETTINGS_PATH.exists() and SETTINGS_PATH.stat().st_size > 0:
                msg = "settings.json exists with non-zero size but parsed empty (possible concurrent write). Refusing to overwrite."
                if verbose:
                    print(f"  [setup-all-hooks] {msg}")
                return {"added": 0, "skipped": 0, "plugin_root": plugin_root_str, "error": msg}
        except OSError:
            pass

    current_hooks = dict(current.get("hooks", {}))

    added = 0
    skipped = 0

    for event, handler_groups in desired.items():
        if event not in current_hooks:
            current_hooks[event] = []

        # Build an index of existing command identities per matcher,
        # and also track the path used (for stale-path detection).
        existing_ids = {}  # matcher -> set of identity strings
        existing_entries = {}  # matcher -> {identity -> (group, hook_dict)}
        for group in current_hooks[event]:
            matcher = group.get("matcher", "")
            existing_ids.setdefault(matcher, set())
            existing_entries.setdefault(matcher, {})
            for h in group.get("hooks", []):
                cmd = h.get("command", "")
                ident = _hook_command_identity(cmd)
                if ident:
                    existing_ids[matcher].add(ident)
                    existing_entries[matcher][ident] = (group, h)

        for group in handler_groups:
            matcher = group.get("matcher", "")
            for h in group.get("hooks", []):
                template_cmd = h.get("command", "")
                resolved_cmd = _resolve_hook_command(template_cmd, plugin_root)
                ident = _hook_command_identity(resolved_cmd)

                if ident and ident in existing_ids.get(matcher, set()):
                    # SEC-003 fix: check if the existing hook points to our current
                    # plugin_root. Only applies to hooks that actually contain a path
                    # (i.e., .py scripts). Echo/bash hooks without paths always skip.
                    existing_group, existing_hook = existing_entries[matcher][ident]
                    existing_cmd = existing_hook.get("command", "")
                    has_path = ".py" in existing_cmd or ".py" in resolved_cmd
                    # On Windows, an existing command can point at the current
                    # root yet still be the legacy native cmd.exe form.
                    # Replace it with the Git-Bash launcher during ensure-health.
                    windows_command_stale = _windows_hook_command_is_stale(
                        existing_cmd, resolved_cmd
                    )
                    # Retrofit the fail-open launcher guard onto a pre-guard baked
                    # command even when the root is unchanged (a stable-dir script
                    # install upgraded in place keeps the same root, so root_in_cmd
                    # would otherwise skip it and it would never gain the guard).
                    # Fires only when the new template carries the guard and the
                    # existing command lacks it.
                    guard_stale = (
                        "[ -r " in resolved_cmd and "[ -r " not in existing_cmd
                    )
                    # _resolve_hook_command now embeds a
                    # forward-slash root on Windows, but plugin_root_str keeps
                    # native backslashes, so a raw substring test never matches
                    # post-fix and every hook would be "replaced" on every run
                    # (perpetual settings.json rewrite). Normalize separators.
                    root_in_cmd = (
                        plugin_root_str.replace("\\", "/") in existing_cmd.replace("\\", "/")
                    )
                    if (not has_path or root_in_cmd) and not windows_command_stale and not guard_stale:
                        skipped += 1
                        if verbose:
                            print(f"  [skip] {event}[{matcher}] {ident} (already present)")
                        continue
                    # Stale path -- replace in place
                    existing_hook["command"] = resolved_cmd
                    # Preserve all fields from new hook (async, timeout, etc.)
                    for k, v in h.items():
                        if k != "command":
                            existing_hook[k] = v
                    added += 1
                    if verbose:
                        print(f"  [replace] {event}[{matcher}] {ident} (stale path updated)")
                    continue

                # Find or create the matcher group
                target_group = None
                for g in current_hooks[event]:
                    if g.get("matcher", "") == matcher:
                        target_group = g
                        break
                if target_group is None:
                    target_group = {"hooks": []}
                    if matcher:
                        target_group["matcher"] = matcher
                    current_hooks[event].append(target_group)

                # Preserve all fields from source hook (async, timeout, type, etc.)
                new_hook = dict(h)
                new_hook["command"] = resolved_cmd
                target_group.setdefault("hooks", []).append(new_hook)
                existing_ids.setdefault(matcher, set()).add(ident)
                existing_entries.setdefault(matcher, {})[ident] = (target_group, new_hook)
                added += 1
                if verbose:
                    print(f"  [add]  {event}[{matcher}] {ident}")

    if added == 0:
        return {"added": 0, "skipped": skipped, "plugin_root": plugin_root_str}

    if dry_run:
        if verbose:
            print(f"  [dry-run] would add {added} hooks, skip {skipped}")
        return {"added": added, "skipped": skipped, "plugin_root": plugin_root_str, "dry_run": True}

    # Apply the merge
    new_settings = dict(current)
    new_settings["hooks"] = current_hooks

    try:
        if not _write_settings_atomic(new_settings):
            if verbose:
                print("  [setup-all-hooks] settings.json write was refused; no hooks added.")
            return {"added": 0, "skipped": skipped, "plugin_root": plugin_root_str, "error": "write refused"}
    except (PermissionError, OSError) as e:
        if verbose:
            print(f"  [setup-all-hooks] could not write settings.json: {e}")
        return {"added": 0, "skipped": skipped, "plugin_root": plugin_root_str, "error": str(e)}

    # Record the heal timestamp so ensure-health's 24h throttle
    # suppresses the redundant run after install.sh.
    try:
        _write_config_flag("last_hook_heal_check", int(time.time()))
    except Exception:
        pass

    return {"added": added, "skipped": skipped, "plugin_root": plugin_root_str}


_ensure_dashboard_file = _measure_compat.bind(_measure_providers['daemon']._ensure_dashboard_file, __name__, defaults=(False,))


_verify_daemon_port = _measure_compat.bind(_measure_providers['daemon']._verify_daemon_port, __name__, defaults=(1, None, 1,))


_daemon_install_lock = _measure_compat.bind(_measure_providers['daemon']._daemon_install_lock, __name__, defaults=(False,))


_reclaim_posix_daemon_port = _measure_compat.bind(_measure_providers['daemon']._reclaim_posix_daemon_port, __name__, defaults=(DAEMON_PORT, "dashboard-server.py",))


_daemon_ports_to_reclaim = _measure_compat.bind(_measure_providers['daemon']._daemon_ports_to_reclaim, __name__)


_reclaim_daemon_ports = _measure_compat.bind(_measure_providers['daemon']._reclaim_daemon_ports, __name__)


_daemon_access_lines = _measure_compat.bind(_measure_providers['daemon']._daemon_access_lines, __name__)


_print_daemon_network_note = _measure_compat.bind(_measure_providers['daemon']._print_daemon_network_note, __name__)


_install_launchd_daemon = _measure_compat.bind(_measure_providers['daemon']._install_launchd_daemon, __name__, defaults=(False, False, None,))


_write_uninstall_tombstone = _measure_compat.bind(_measure_providers['daemon']._write_uninstall_tombstone, __name__, defaults=(None,))


# ---------------------------------------------------------------------------
# Sticky install-failed marker (no-revive after a failed install)
# ---------------------------------------------------------------------------

_daemon_install_failed_marker_state = _measure_compat.bind(_measure_providers['daemon']._daemon_install_failed_marker_state, __name__)


_daemon_install_failed_marker_present = _measure_compat.bind(_measure_providers['daemon']._daemon_install_failed_marker_present, __name__)


_daemon_install_failed_reason = _measure_compat.bind(_measure_providers['daemon']._daemon_install_failed_reason, __name__)


_write_daemon_install_failed_marker = _measure_compat.bind(_measure_providers['daemon']._write_daemon_install_failed_marker, __name__)


_clear_daemon_install_failed_marker = _measure_compat.bind(_measure_providers['daemon']._clear_daemon_install_failed_marker, __name__)


_uninstall_launchd_daemon = _measure_compat.bind(_measure_providers['daemon']._uninstall_launchd_daemon, __name__, defaults=(False, False,))



WINDOWS_TASK_NAME = (
    "TokenOptimizerCodexDashboard" if _DAEMON_RUNTIME == "codex"
    else ("TokenOptimizerHermesDashboard" if _DAEMON_RUNTIME == "hermes"
          else ("TokenOptimizerAntigravityDashboard" if _DAEMON_RUNTIME == "antigravity"
                else "TokenOptimizerDashboard"))
)
WINDOWS_LAUNCHER_NAME = "dashboard-launcher.cmd"


_is_ms_store_python_alias = _measure_compat.bind(_measure_providers['daemon']._is_ms_store_python_alias, __name__)


_resolve_windows_pythonw = _measure_compat.bind(_measure_providers['daemon']._resolve_windows_pythonw, __name__)


_windows_gui_python = _measure_compat.bind(_measure_providers['daemon']._windows_gui_python, __name__)


_windows_task_exec_action = _measure_compat.bind(_measure_providers['daemon']._windows_task_exec_action, __name__)


_compose_windows_user_id = _measure_compat.bind(_measure_providers['daemon']._compose_windows_user_id, __name__)


_generate_windows_launcher_cmd = _measure_compat.bind(_measure_providers['daemon']._generate_windows_launcher_cmd, __name__)


_probe_windows_port_owner = _measure_compat.bind(_measure_providers['daemon']._probe_windows_port_owner, __name__)


_generate_schtasks_xml = _measure_compat.bind(_measure_providers['daemon']._generate_schtasks_xml, __name__, defaults=("",))


_install_task_scheduler_daemon = _measure_compat.bind(_measure_providers['daemon']._install_task_scheduler_daemon, __name__, defaults=(False, False, None,))


_uninstall_task_scheduler_daemon = _measure_compat.bind(_measure_providers['daemon']._uninstall_task_scheduler_daemon, __name__, defaults=(False, False,))



# Runtime-suffix-derived (not a codex-vs-else ternary) so hermes/copilot get
# their own unit + launcher names instead of colliding on the Claude ones, the
# same way DAEMON_LABEL / WINDOWS_TASK_NAME already distinguish every runtime.
SYSTEMD_UNIT_NAME = (
    f"token-optimizer-{_DAEMON_RUNTIME_SUFFIX}-dashboard.service"
    if _DAEMON_RUNTIME_SUFFIX != "claude"
    else "token-optimizer-dashboard.service"
)
LINUX_LAUNCHER_NAME = (
    f"{_DAEMON_RUNTIME_SUFFIX}-dashboard-launcher.sh"
    if _DAEMON_RUNTIME_SUFFIX != "claude"
    else "dashboard-launcher.sh"
)


_generate_linux_launcher_sh = _measure_compat.bind(_measure_providers['daemon']._generate_linux_launcher_sh, __name__)


_probe_systemd_user_bus = _measure_compat.bind(_measure_providers['daemon']._probe_systemd_user_bus, __name__)


_systemd_user_unit_path = _measure_compat.bind(_measure_providers['daemon']._systemd_user_unit_path, __name__)


_systemd_user_unit_path_for = _measure_compat.bind(_measure_providers['daemon']._systemd_user_unit_path_for, __name__)


_generate_systemd_user_unit = _measure_compat.bind(_measure_providers['daemon']._generate_systemd_user_unit, __name__)


_install_systemd_user_daemon = _measure_compat.bind(_measure_providers['daemon']._install_systemd_user_daemon, __name__, defaults=(False, False, None,))


_uninstall_systemd_user_daemon = _measure_compat.bind(_measure_providers['daemon']._uninstall_systemd_user_daemon, __name__, defaults=(False, False,))



_normalized_platform = _measure_compat.bind(_measure_providers['daemon']._normalized_platform, __name__)


_setup_daemon_cli = _measure_compat.bind(_measure_providers['daemon']._setup_daemon_cli, __name__)


setup_daemon = _measure_compat.bind(_measure_providers['daemon'].setup_daemon, __name__, defaults=(False, False, False, True,))


# ---------------------------------------------------------------------------
# Uninstall cleanup orchestrator (cleanup command)
# ---------------------------------------------------------------------------

# Paths that are intentionally PRESERVED across an uninstall. These hold
# session data, checkpoints, and trends the user may want to keep or migrate.
# Listed explicitly so the cleanup command's --dry-run doubles as retained-
# paths DISCLOSURE: the user sees exactly what stays before agreeing to
# remove anything.
_retained_paths = _measure_compat.bind(_measure_providers['daemon']._retained_paths, __name__)


_backup_settings_file = _measure_compat.bind(_measure_providers['daemon']._backup_settings_file, __name__)


_is_our_hook_entry = _measure_compat.bind(_measure_providers['daemon']._is_our_hook_entry, __name__)


_remove_our_settings_entries = _measure_compat.bind(_measure_providers['daemon']._remove_our_settings_entries, __name__)


cleanup = _measure_compat.bind(_measure_providers['daemon'].cleanup, __name__, defaults=(False, False,))


_daemon_service_installed = _measure_compat.bind(_measure_providers['daemon']._daemon_service_installed, __name__, defaults=(None,))


_daemon_ensure_throttle_seconds = _measure_compat.bind(_measure_providers['daemon']._daemon_ensure_throttle_seconds, __name__)


_daemon_pulse_probe_seconds = _measure_compat.bind(_measure_providers['daemon']._daemon_pulse_probe_seconds, __name__)


_daemon_pulse_revive_seconds = _measure_compat.bind(_measure_providers['daemon']._daemon_pulse_revive_seconds, __name__)


_ensure_dashboard_daemon = _measure_compat.bind(_measure_providers['daemon']._ensure_dashboard_daemon, __name__, defaults=(False,))


_daemon_served_version = _measure_compat.bind(_measure_providers['daemon']._daemon_served_version, __name__, defaults=(None, None,))


_daemon_midsession_pulse = _measure_compat.bind(_measure_providers['daemon']._daemon_midsession_pulse, __name__)


# Task-action binaries that own a console, i.e. that Windows paints a window
# for when Task Scheduler launches them. Only the interpreter names OUR
# installers have ever baked as an <Exec> command:
# generic ``cmd.exe``/``conhost.exe`` were dropped because we never register
# those shapes, and matching them meant a user's own wrapper arrangement in
# our task slot would be "positively identified as ours" and wiped.
_WINDOWS_CONSOLE_ACTION_EXES = frozenset({
    "py.exe", "python.exe", "python3.exe",
})


_windows_action_is_console_flasher = _measure_compat.bind(_measure_providers['daemon']._windows_action_is_console_flasher, __name__)


_windows_task_action_info = _measure_compat.bind(_measure_providers['daemon']._windows_task_action_info, __name__, defaults=(None,))


_windows_task_action_command = _measure_compat.bind(_measure_providers['daemon']._windows_task_action_command, __name__, defaults=(None,))


_windows_action_path_exists = _measure_compat.bind(_measure_providers['daemon']._windows_action_path_exists, __name__)


_windows_action_is_dead_path = _measure_compat.bind(_measure_providers['daemon']._windows_action_is_dead_path, __name__)


_daemon_resurrection_blocked = _measure_compat.bind(_measure_providers['daemon']._daemon_resurrection_blocked, __name__)


_heal_windows_task_action = _measure_compat.bind(_measure_providers['daemon']._heal_windows_task_action, __name__, defaults=(False, False,))


_legacy_daemon_dir = _measure_compat.bind(_measure_providers['daemon']._legacy_daemon_dir, __name__)


_heal_one_launcher_shim = _measure_compat.bind(_measure_providers['daemon']._heal_one_launcher_shim, __name__)


_heal_windows_launcher_shim = _measure_compat.bind(_measure_providers['daemon']._heal_windows_launcher_shim, __name__)


_heal_windows_console_flash = _measure_compat.bind(_measure_providers['daemon']._heal_windows_console_flash, __name__)


_restart_dashboard_daemon = _measure_compat.bind(_measure_providers['daemon']._restart_dashboard_daemon, __name__)


_apply_daemon_restart_outcome = _measure_compat.bind(_measure_providers['daemon']._apply_daemon_restart_outcome, __name__)


# ========== Context Quality Analyzer (v2.0) ==========
# Measures content QUALITY inside a session, not just quantity.
# Pure JSONL analysis, no model calls, no hooks required.

# unified per-session state base (see _STATE_BASE near SNAPSHOT_DIR).
# In Cowork this is the resolved plugin-data dir so checkpoints no longer
# double-write under both ~/.claude/token-optimizer and the plugin-data tree;
# on desktop it is RUNTIME_DIR, byte-identical to the previous behavior.
CHECKPOINT_DIR = _STATE_BASE / "token-optimizer" / "checkpoints"
CHECKPOINT_EVENT_LOG = _STATE_BASE / "token-optimizer" / "checkpoint-events.jsonl"

# v6 dual-score architecture: ResourceHealth (monotonic warning) + SessionEfficiency (behavioral).
# ResourceHealth can only worsen within a session (no rolling-window signals).
# SessionEfficiency uses rolling windows and can rise or fall freely.
# Score schema version. v2 (COMPACTION-QUALITY.md): compaction count carries
# zero quality weight -- measured post-boundary recovery signals live in a
# separate evidence panel instead of the count-only penalty. Consumers must
# never compare or dampen a v1 score against a v2 score.
_QUALITY_SCORE_VERSION = 2

_RESOURCE_HEALTH_WEIGHTS = {
    # Compaction count is descriptive metadata, not a quality signal: the
    # count-only penalty is replaced by measured recovery evidence
    # (recovery_status/recovery_evidence). Weights renormalized to 1.0.
    "context_fill_degradation": 5 / 7,
    "absolute_waste_tokens": 2 / 7,
}

_SESSION_EFFICIENCY_WEIGHTS = {
    "stale_reads": 0.30,
    "bloated_results": 0.30,
    "decision_density": 0.20,
    "agent_efficiency": 0.20,
}

_int_env = _measure_compat.bind(_measure_providers['quality_archive']._int_env, __name__)


_float_env = _measure_compat.bind(_measure_providers['quality_archive']._float_env, __name__)


# Rolling window size for ratio-based signals.
# Ratio signals (stale_reads, bloated_results, decision_density, agent_efficiency)
# use only the last N operations to prevent denominator-expansion bias where
# scores climb as the session progresses even though context health is degrading.
_QUALITY_ROLLING_WINDOW = _int_env("TOKEN_OPTIMIZER_QUALITY_WINDOW", 20)
# Below this many messages a session is too new to judge decision density — a
# brand-new session hasn't accumulated decisions yet, so scoring it 0 would
# wrongly drag SessionEfficiency down ~20 points. Stay neutral instead.
_DENSITY_MIN_MESSAGES = _int_env("TOKEN_OPTIMIZER_DENSITY_MIN_MESSAGES", 6)

# Fill-based warning thresholds that fire independently of the composite score.
# These cannot be masked by improving ratio signals.
_FILL_WARN_THRESHOLDS = [
    (0.85, "CRITICAL", "85% context fill, compact now"),
    (0.75, "WARNING", "75% context fill, consider compacting"),
]

# Tool call thresholds: instruction adherence degrades after ~15 tool calls on
# 200K models (COLM 2025, codeongrass.com practitioner analysis). On 1M context
# models the degradation is much later because tool results are a smaller fraction
# of the window. Thresholds scale superlinearly (x^1.3) with detected context
# window: 1M gets ~8x the baseline; 200K gets 1x. Additionally, tool call
# warnings are gated on fill_pct >= 50% in compute_quality_score() because at
# low fill, tool calls don't cause instruction adherence issues regardless of
# count. Env var overrides still take precedence.
_scaled_tool_call_thresholds = _measure_compat.bind(_measure_providers['quality_archive']._scaled_tool_call_thresholds, __name__)

# Resolved at the call site, never at import (#246). detect_context_window()
# reads helpers defined ~11k lines further down this module
# (_resolve_feature_env, _TRUTHY_ENV_VALUES, the settings.json loaders). At
# import time those names do not exist yet, so a module-level call raised
# NameError, the broad fallback above swallowed it, and every process kept the
# 200K thresholds (25/40) whatever window it actually ran on: a 1M session
# got "CRITICAL: 40+ tool calls" instead of the scaled 324.
# detect_context_window() caches on its inputs, so resolving per call costs a
# tuple compare. tests/test_tool_call_thresholds_lazy.py traces the import for
# any swallowed NameError so this class of bug cannot come back quietly.
_tool_call_warn_thresholds = _measure_compat.bind(_measure_providers['quality_archive']._tool_call_warn_thresholds, __name__)

# Configurable via env vars
_CHECKPOINT_MAX_FILES = _int_env("TOKEN_OPTIMIZER_CHECKPOINT_FILES", 10)
# Restore eligibility is governed by the retention-days window below, not a
# short TTL. The former TOKEN_OPTIMIZER_CHECKPOINT_TTL (300s) gated out the
# common stop/end triggers and produced 0 restores; checkpoint freshness for
# fire-cooldown is handled separately by _CHECKPOINT_COOLDOWN_SECONDS.
_CHECKPOINT_RETENTION_DAYS = _int_env("TOKEN_OPTIMIZER_CHECKPOINT_RETENTION_DAYS", 7)
# Retention is per session: each session keeps its newest few checkpoints plus
# the newest of each trigger, so one busy session can never push another
# session's checkpoints out. RETENTION_MAX is only a disk ceiling across all
# sessions; under it, every session's newest checkpoint is the last to go.
_CHECKPOINT_RETENTION_MAX = _int_env("TOKEN_OPTIMIZER_CHECKPOINT_RETENTION_MAX", 400)
_CHECKPOINT_PER_SESSION = _int_env("TOKEN_OPTIMIZER_CHECKPOINT_PER_SESSION", 5)
# Relevance gating: the lightweight prompt-continuity hint used to
# inject a full multi-line `[RECOVERED DATA ...]` block for any prior session
# scoring as low as 0.30, even when the prior session was on a DIFFERENT topic.
# A two-tier gate fixes that without losing the signal entirely:
#   * _RELEVANCE_TEASER_FLOOR (env: TOKEN_OPTIMIZER_RELEVANCE_THRESHOLD, default
#     0.3) -- the COLLECTION floor. Candidates at/above this are still gathered
#     and ranked, so a 0.33 off-topic session stays visible as a one-line
#     teaser. Back-compat: the original env var name still controls this floor.
#   * _RELEVANCE_FULL_THRESHOLD (env: TOKEN_OPTIMIZER_RELEVANCE_FULL_THRESHOLD,
#     default 0.5) -- the FULL-BLOCK threshold. Only the TOP session, when its
#     score is at/above this, emits the multi-line RECOVERED block + the
#     other-sessions list (byte-identical to the pre-fix behavior). Below it,
#     the top session degrades to a single-line teaser and the other-sessions
#     list is suppressed.
_RELEVANCE_TEASER_FLOOR = _float_env("TOKEN_OPTIMIZER_RELEVANCE_THRESHOLD", 0.3)
_RELEVANCE_FULL_THRESHOLD = _float_env("TOKEN_OPTIMIZER_RELEVANCE_FULL_THRESHOLD", 0.5)
# Back-compat alias: existing internal references to _RELEVANCE_THRESHOLD keep
# working and mean the teaser/collection floor (the historical 0.3 default).
_RELEVANCE_THRESHOLD = _RELEVANCE_TEASER_FLOOR

# Cross-product coordination: when a co-installed tool owns durable
# cross-session memory, Token Optimizer's low-relevance continuity teaser is
# redundant noise, so it is suppressed. The other tool signals its presence by
# dropping a neutral coordination marker file (auto-detected on disk) or by
# exporting an opt-in env var; TO coordinates with whatever is present without
# knowing or naming which tool it is. Detection is best-effort, cached once per
# process, and fully guarded so it can never raise.
_EXTERNAL_MEMORY_CACHE = None


_external_memory_present = _measure_compat.bind(_measure_providers['quality_archive']._external_memory_present, __name__)


# Data-retention controls (enterprise compliance)
_QUALITY_CACHE_RETENTION_DAYS = _int_env("TOKEN_OPTIMIZER_QUALITY_CACHE_RETENTION_DAYS", 7)
_CHECKPOINT_EVENT_MAX = _int_env("TOKEN_OPTIMIZER_CHECKPOINT_EVENT_MAX", 1000)
_TRENDS_RETENTION_DAYS = _int_env("TOKEN_OPTIMIZER_TRENDS_RETENTION_DAYS", 0)  # 0 = unlimited
_ARCHIVE_RETENTION_HOURS = _int_env("TOKEN_OPTIMIZER_ARCHIVE_RETENTION_HOURS", 24)

# Progressive checkpoint thresholds (% fill, fires once each per session)
_PROGRESSIVE_BANDS = [20, 35, 50, 65, 80]
_PROGRESSIVE_ENABLED = os.environ.get("TOKEN_OPTIMIZER_PROGRESSIVE_CHECKPOINTS", "1") not in ("0", "false", "no", "off")
_QUALITY_CHECKPOINT_THRESHOLDS = [80, 70, 50, 40]
_CHECKPOINT_COOLDOWN_SECONDS = _int_env("TOKEN_OPTIMIZER_CHECKPOINT_COOLDOWN_SECONDS", 90)
# A post-compact restore treats the session's newest PreCompact ("auto") checkpoint
# as the moment of the compaction it is restoring for, when it is this recent.
# Compaction (PreCompact capture -> summary request -> SessionStart(compact)) runs
# in minutes; an older one belongs to an earlier compaction whose own PreCompact
# capture is all this one has to go on (e.g. #253: the capture was cut off).
_COMPACT_RESTORE_ANCHOR_MAX_AGE_S = 1800
_EDIT_BATCH_WRITE_THRESHOLD = _int_env("TOKEN_OPTIMIZER_EDIT_BATCH_WRITE_THRESHOLD", 4)
_EDIT_BATCH_FILE_THRESHOLD = _int_env("TOKEN_OPTIMIZER_EDIT_BATCH_FILE_THRESHOLD", 3)
_CHECKPOINT_TELEMETRY_ENABLED = os.environ.get("TOKEN_OPTIMIZER_CHECKPOINT_TELEMETRY", "0").lower() in ("1", "true", "yes", "on")

# Shared decision-detection regex (used by both quality analyzer and state extractor)
_DECISION_RE = re.compile(
    r'\b(chose|decided|because|instead of|went with|going with|switched to|'
    r'prefer|better to|should use|will use|picking|opting for|let\'s use|'
    r'using .+ over|settled on|sticking with)\b',
    re.IGNORECASE
)

# Continuation phrases for session relevance matching (require 2+ word phrases, not single words)
_CONTINUATION_PHRASES = {"continue where", "pick up", "carry on", "resume where", "left off", "where we left"}
_CONTINUATION_WORDS = {"continue", "resume"}  # These alone are strong enough signals


sanitize_session_id = _measure_compat.bind(_measure_providers['quality_archive'].sanitize_session_id, __name__)


_extract_user_text = _measure_compat.bind(_measure_providers['quality_archive']._extract_user_text, __name__)


_HARNESS_TASK_PREFIXES = (
    "<local-command-caveat>", "<local-command-stdout>",
    "<local-command-stderr>", "<task-notification>",
    "[SYSTEM NOTIFICATION", "[Request interrupted by user",
    "Your response above was cut off mid-stream",
)
# Built-in commands that only change the session or UI. Run bare, they are not
# a task. With arguments, or when an installed skill owns the name, they are.
_LOCAL_SLASH_COMMANDS = frozenset((
    "add-dir", "agents", "bug", "config", "context", "continue", "cost",
    "doctor", "exit", "export", "fast", "feedback", "help", "hooks", "ide",
    "init", "login", "logout", "mcp", "memory", "model", "output-style",
    "permissions", "privacy-settings", "quit", "release-notes", "resume",
    "rewind", "settings", "stats", "status", "statusline", "terminal-setup",
    "theme", "upgrade", "usage", "vim",
))
_COMMAND_TASK_TAG_RE = re.compile(r"\s*<command-(name|message|args)>")


_command_task_text = _measure_compat.bind(_measure_providers['quality_archive']._command_task_text, __name__)


_task_user_text = _measure_compat.bind(_measure_providers['quality_archive']._task_user_text, __name__)


_read_stdin_hook_input = _measure_compat.bind(_measure_providers['quality_archive']._read_stdin_hook_input, __name__, defaults=(65536,))


_read_windows_stdin_bytes = _measure_compat.bind(_measure_providers['quality_archive']._read_windows_stdin_bytes, __name__)


_read_throttle_only_hook_input = _measure_compat.bind(_measure_providers['quality_archive']._read_throttle_only_hook_input, __name__)


_quality_cache_throttle_only_state = _measure_compat.bind(_measure_providers['quality_archive']._quality_cache_throttle_only_state, __name__)


# ========== Compaction recovery evidence (matched-window event study) ==========
# Mirrors the retrospective audit in research/arxiv-2611.05306: for every
# compact_boundary, compare the last <=20 completed tool calls before it with
# the first <=20 after it, matched on the smaller side and requiring >=10 each.
# This state is deliberately NEVER reset at a boundary -- unlike the quality
# accumulators above, which describe only the current post-compaction window.
# Only call metadata (names, arg hashes, flags) is retained; no result text.

_RECOVERY_WINDOW_CALLS = 20
_RECOVERY_MIN_MATCHED_CALLS = 10
_RECOVERY_PENDING_MAX = 100_000
_RECOVERY_EVENTS_KEPT = 40

_REC_LOOP_NOTICE_RE = re.compile(r"\[Token Optimizer\] (?:Retry loop:|Loop:)")
_REC_RESTORE_NOTICE_RE = re.compile(r"\[Token Optimizer\] Post-compact recovery \(")
_REC_CAPTURE_NOTICE_RE = re.compile(r"Captured .{0,70}checkpoint", re.IGNORECASE)
_REC_EXIT_CODE_RE = re.compile(
    r"(?:Exit code|exited with (?:code|status)|Process exited with code)\s*:?\s*([1-9]\d*)",
    re.IGNORECASE)
_REC_EXPAND_RE = re.compile(
    r"(?:measure\.py|token.optimizer)[\s\S]{0,150}\bexpand\b|\bexpand\s+(?:toolu_|exec-|[a-f0-9]{12,})")
_HOOK_TEXT_KEYS = ("text", "content", "stdout", "systemMessage",
                   "additionalContext", "hookSpecificOutput",
                   "attachment", "message", "payload")


_hook_payload_text = _measure_compat.bind(_measure_providers['quality_archive']._hook_payload_text, __name__, defaults=(0,))


_recovery_window_metrics = _measure_compat.bind(_measure_providers['quality_archive']._recovery_window_metrics, __name__)


_finalize_compaction_boundary = _measure_compat.bind(_measure_providers['quality_archive']._finalize_compaction_boundary, __name__)


_recovery_boundary_eval = _measure_compat.bind(_measure_providers['quality_archive']._recovery_boundary_eval, __name__)


_rec_track_tool_use = _measure_compat.bind(_measure_providers['quality_archive']._rec_track_tool_use, __name__)


_rec_track_tool_result = _measure_compat.bind(_measure_providers['quality_archive']._rec_track_tool_result, __name__)


_parse_jsonl_for_quality = _measure_compat.bind(_measure_providers['quality_archive']._parse_jsonl_for_quality, __name__)


_STALE_READ_DISTANCE_THRESHOLD = 120  # ~20-30 turns in record space


detect_stale_reads = _measure_compat.bind(_measure_providers['quality_archive'].detect_stale_reads, __name__)


_session_stale_waste_tokens = _measure_compat.bind(_measure_providers['quality_archive']._session_stale_waste_tokens, __name__)


detect_reread_loops = _measure_compat.bind(_measure_providers['quality_archive'].detect_reread_loops, __name__)


detect_bloated_results = _measure_compat.bind(_measure_providers['quality_archive'].detect_bloated_results, __name__)


detect_duplicates = _measure_compat.bind(_measure_providers['quality_archive'].detect_duplicates, __name__)


_session_recovery_db_events = _measure_compat.bind(_measure_providers['quality_archive']._session_recovery_db_events, __name__, defaults=(None,))


_assess_compaction_recovery = _measure_compat.bind(_measure_providers['quality_archive']._assess_compaction_recovery, __name__, defaults=(None,))


_recovery_panel_detail = _measure_compat.bind(_measure_providers['quality_archive']._recovery_panel_detail, __name__)


compute_quality_score = _measure_compat.bind(_measure_providers['quality_archive'].compute_quality_score, __name__, defaults=(None,))


_codex_identity_reason = _measure_compat.bind(_measure_providers['quality_archive']._codex_identity_reason, __name__)


_codex_thread_resolution = _measure_compat.bind(_measure_providers['quality_archive']._codex_thread_resolution, __name__)


_codex_unresolved_message = _measure_compat.bind(_measure_providers['quality_archive']._codex_unresolved_message, __name__)


_current_jsonl_for_hook = _measure_compat.bind(_measure_providers['quality_archive']._current_jsonl_for_hook, __name__)


_find_current_session_jsonl = _measure_compat.bind(_measure_providers['quality_archive']._find_current_session_jsonl, __name__)


_find_session_jsonl_by_id = _measure_compat.bind(_measure_providers['quality_archive']._find_session_jsonl_by_id, __name__)


_session_id_prefix_matches = _measure_compat.bind(_measure_providers['quality_archive']._session_id_prefix_matches, __name__)


quality_analyzer = _measure_compat.bind(_measure_providers['quality_archive'].quality_analyzer, __name__, defaults=(None, False,))


_collect_quality_for_dashboard = _measure_compat.bind(_measure_providers['quality_archive']._collect_quality_for_dashboard, __name__)


# ========== JSONL Toolkit (v3.0) ==========
# Read/write utilities for JSONL session files: inspect, trim, dedup.


_extract_tool_result_text = _measure_compat.bind(_measure_providers['quality_archive']._extract_tool_result_text, __name__)


_TOOL_FAILURE_RE = re.compile(
    r"("
    r"\btraceback\b|"
    r"\bexception\b|"
    r"\bfailed\b|"
    r"\bfailure\b|"
    r"\bfatal:|"
    r"\berror:|"
    r"\bpermission denied\b|"
    r"\bno such file or directory\b|"
    r"\bcommand not found\b|"
    r"\bexit (?:code|status) [2-9]\d*\b|"
    r"\bexited with code [2-9]\d*\b|"
    r"\breturned non-zero\b|"
    r"\breturncode [2-9]\d*\b|"
    r"\btimed out\b|"
    r"\bsyntaxerror\b|"
    r"\btypeerror\b|"
    r"\bvalueerror\b|"
    r"\bassertionerror\b|"
    r"\bnpm err!|"
    r"\btests? failed\b"
    r")",
    re.IGNORECASE,
)


_TOOL_SUCCESS_COUNT_RE = re.compile(
    r"\b(?:0 failed|0 failures|0 errors|no failures|no errors)\b",
    re.IGNORECASE,
)
_TOOL_NONZERO_FAILURE_COUNT_RE = re.compile(
    r"\b[1-9]\d*\s+(?:failed|failures|errors)\b",
    re.IGNORECASE,
)


_tool_result_looks_failed = _measure_compat.bind(_measure_providers['quality_archive']._tool_result_looks_failed, __name__)


_resolve_jsonl_path = _measure_compat.bind(_measure_providers['quality_archive']._resolve_jsonl_path, __name__, defaults=(None,))


_jsonl_record_text_size = _measure_compat.bind(_measure_providers['quality_archive']._jsonl_record_text_size, __name__)


_classify_record = _measure_compat.bind(_measure_providers['quality_archive']._classify_record, __name__)


jsonl_inspect = _measure_compat.bind(_measure_providers['quality_archive'].jsonl_inspect, __name__, defaults=(None, False,))


jsonl_trim = _measure_compat.bind(_measure_providers['quality_archive'].jsonl_trim, __name__, defaults=(None, False, 4000,))


jsonl_dedup = _measure_compat.bind(_measure_providers['quality_archive'].jsonl_dedup, __name__, defaults=(None, False,))


# ========== Lost-in-the-Middle Optimizer (v3.0) ==========
# Scores files against the U-shaped attention curve: LLMs attend more to
# the beginning (0-30%) and end (70-100%) of context, less to the middle.
# Flags critical rules (NEVER/ALWAYS/MUST/etc.) that land in the low-attention zone.

_CRITICAL_PATTERN = re.compile(
    r'\b(NEVER|ALWAYS|MUST|NON-NEGOTIABLE|IMPORTANT|CRITICAL)\b',
    re.IGNORECASE
)

_LOW_ZONE_START = 0.30
_LOW_ZONE_END = 0.70


_parse_sections = _measure_compat.bind(_measure_providers['audit']._parse_sections, __name__)


_find_critical_rules = _measure_compat.bind(_measure_providers['audit']._find_critical_rules, __name__)


_classify_zone = _measure_compat.bind(_measure_providers['audit']._classify_zone, __name__)


_score_attention = _measure_compat.bind(_measure_providers['audit']._score_attention, __name__)


# ---------------------------------------------------------------------------
# Memory Review — structural auditor for MEMORY.md and CLAUDE.md
# ---------------------------------------------------------------------------

_MR_MEMORY_LINE_LIMIT = 200  # Claude auto-loads only the first N lines of MEMORY.md

_MR_LINK_RE = re.compile(r'\[([^\]]*)\]\((?!https?://)([^)]+\.md)\)')
_MR_DATE_RE = re.compile(r'(?:Set|Updated|Added|Created)\s+(\d{4}-\d{2}-\d{2})')
_MR_TASK_HEADING_RE = re.compile(r'^##\s+(TODO|Backlog|Open Tasks|OPEN TASKS|Action Items)', re.IGNORECASE)
_MR_CHECKBOX_RE = re.compile(r'^\s*-\s*\[[ x]\]')
_MR_RULE_RE = re.compile(r'\b(NEVER|ALWAYS|MUST|CRITICAL|NON-NEGOTIABLE)\b')
_MR_STALE_KEYWORD_RE = re.compile(r'\b(RESOLVED|SUPERSEDED|PRIOR STATE|Old notes|DEPRECATED)\b', re.IGNORECASE)


_mr_parse_memory_index = _measure_compat.bind(_measure_providers['audit']._mr_parse_memory_index, __name__)


_mr_scan_topic_files = _measure_compat.bind(_measure_providers['audit']._mr_scan_topic_files, __name__)


_mr_detect_orphans = _measure_compat.bind(_measure_providers['audit']._mr_detect_orphans, __name__)


_mr_detect_inline_content = _measure_compat.bind(_measure_providers['audit']._mr_detect_inline_content, __name__)


_mr_detect_staleness = _measure_compat.bind(_measure_providers['audit']._mr_detect_staleness, __name__, defaults=(180,))


_mr_detect_duplicates = _measure_compat.bind(_measure_providers['audit']._mr_detect_duplicates, __name__)


_mr_detect_task_leakage = _measure_compat.bind(_measure_providers['audit']._mr_detect_task_leakage, __name__)


_mr_detect_truncation_waste = _measure_compat.bind(_measure_providers['audit']._mr_detect_truncation_waste, __name__)


_mr_detect_taxonomy = _measure_compat.bind(_measure_providers['audit']._mr_detect_taxonomy, __name__)


_mr_compute_savings = _measure_compat.bind(_measure_providers['audit']._mr_compute_savings, __name__)


memory_review = _measure_compat.bind(_measure_providers['audit'].memory_review, __name__, defaults=(False, False, 180, None,))


_mr_apply_fixes = _measure_compat.bind(_measure_providers['audit']._mr_apply_fixes, __name__)


_analyze_attention_sections = _measure_compat.bind(_measure_providers['audit']._analyze_attention_sections, __name__)


attention_score = _measure_compat.bind(_measure_providers['audit'].attention_score, __name__, defaults=(None, False,))


attention_optimize = _measure_compat.bind(_measure_providers['audit'].attention_optimize, __name__, defaults=(None, True, False,))


# ========== Tool Result Archive (v3.0) ==========
# PostToolUse hook handler that archives large tool results to disk so they
# survive compaction. Provides `expand` command to retrieve archived results.

_ARCHIVE_THRESHOLD = 4096  # chars: only archive results >= this size
_ARCHIVE_PREVIEW_SIZE = 1000  # chars: preview included in replacement output


_archive_dir_for_session = _measure_compat.bind(_measure_providers['quality_archive']._archive_dir_for_session, __name__)


_ensure_private_dir = _measure_compat.bind(_measure_providers['quality_archive']._ensure_private_dir, __name__)


archive_result = _measure_compat.bind(_measure_providers['quality_archive'].archive_result, __name__, defaults=(False,))


_sanitize_tool_use_id = _measure_compat.bind(_measure_providers['quality_archive']._sanitize_tool_use_id, __name__)


_summarize_tool_output_for_recovery = _measure_compat.bind(_measure_providers['quality_archive']._summarize_tool_output_for_recovery, __name__)


_archived_response_hash = _measure_compat.bind(_measure_providers['quality_archive']._archived_response_hash, __name__)


_codex_backfill_tool_archive = _measure_compat.bind(_measure_providers['quality_archive']._codex_backfill_tool_archive, __name__, defaults=(None, None, 20,))


_REEXPAND_RANGES_RE = re.compile(r"\bchars ((?:\d+-\d+)(?:,\d+-\d+)*)\s*$")


_merge_char_ranges = _measure_compat.bind(_measure_providers['quality_archive']._merge_char_ranges, __name__)


_subtract_char_ranges = _measure_compat.bind(_measure_providers['quality_archive']._subtract_char_ranges, __name__)


_reexpand_detail_ranges = _measure_compat.bind(_measure_providers['quality_archive']._reexpand_detail_ranges, __name__)


_log_reexpand_debit = _measure_compat.bind(_measure_providers['quality_archive']._log_reexpand_debit, __name__, defaults=("", None,))


_find_archive_entry = _measure_compat.bind(_measure_providers['quality_archive']._find_archive_entry, __name__, defaults=(None, False,))


# Every runtime whose archive an unpinned expand may fall back to, in lookup order.
_EXPAND_FALLBACK_RUNTIMES = ("claude", "codex", "hermes", "copilot", "cursor",
                             "opencode", "antigravity", "grok", "devin")


_expand_may_cross_runtimes = _measure_compat.bind(_measure_providers['quality_archive']._expand_may_cross_runtimes, __name__)


_cross_runtime_archive_roots = _measure_compat.bind(_measure_providers['quality_archive']._cross_runtime_archive_roots, __name__)


_expand_window_command = _measure_compat.bind(_measure_providers['quality_archive']._expand_window_command, __name__, defaults=("",))


_expand_hit_header = _measure_compat.bind(_measure_providers['quality_archive']._expand_hit_header, __name__)


_expand_bounded = _measure_compat.bind(_measure_providers['quality_archive']._expand_bounded, __name__)


_aw_line_count = _measure_compat.bind(_measure_providers['quality_archive']._aw_line_count, __name__)


expand_archived = _measure_compat.bind(_measure_providers['quality_archive'].expand_archived, __name__, defaults=(None, None, False, None,))


expand_search = _measure_compat.bind(_measure_providers['quality_archive'].expand_search, __name__, defaults=(None, 20,))


archive_cleanup = _measure_compat.bind(_measure_providers['quality_archive'].archive_cleanup, __name__, defaults=(None,))


_purge_all_data = _measure_compat.bind(_measure_providers['quality_archive']._purge_all_data, __name__, defaults=(False, False,))


# ========== Security Report ==========

_security_report = _measure_compat.bind(_measure_providers['quality_archive']._security_report, __name__, defaults=(False,))


# ========== Smart Compaction System (v2.0) ==========
# PreCompact state capture, SessionStart restoration, Compact Instructions generation.
# All logic in Python for cross-platform compatibility.

_iter_tail_lines = _measure_compat.bind(_measure_providers['continuity']._iter_tail_lines, __name__, defaults=(1 << 20,))


_tool_result_text = _measure_compat.bind(_measure_providers['continuity']._tool_result_text, __name__)


_tool_call_target = _measure_compat.bind(_measure_providers['continuity']._tool_call_target, __name__)


_first_task_user_message = _measure_compat.bind(_measure_providers['continuity']._first_task_user_message, __name__)


_extract_session_state = _measure_compat.bind(_measure_providers['continuity']._extract_session_state, __name__, defaults=(500,))


_capture_git_state = _measure_compat.bind(_measure_providers['continuity']._capture_git_state, __name__, defaults=(None,))


_TRIGGER_ALLOWED_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


_sanitize_trigger = _measure_compat.bind(_measure_providers['continuity']._sanitize_trigger, __name__)


_confine_transcript_path = _measure_compat.bind(_measure_providers['continuity']._confine_transcript_path, __name__)


_redaction_skip_notice = _measure_compat.bind(_measure_providers['continuity']._redaction_skip_notice, __name__)


_SkipTranscriptQuality = _measure_compat.bind(_measure_providers['continuity']._SkipTranscriptQuality, __name__)


# --- Budgeted checkpoint renderer (gap-checkpoint) -------------------------
#
# The old renderer cut the active task and continuation to a fixed 300 chars,
# which could drop the actual next step. Sections now carry a tier:
#
#   "hard"     — objective, user constraints, next action: ALWAYS retained,
#                bounded only by their own per-section caps.
#   "keep"     — structured state; tail lines are dropped only after every
#                optional section is gone and the budget still does not fit.
#   "optional" — background; shrunk/dropped first, highest ``cut`` value first.
#
# The budget is a target, not a hard ceiling: hard-tier sections are emitted
# even when they alone exceed it, because retention beats size.
_CHECKPOINT_RENDER_BUDGET = _int_env("TOKEN_OPTIMIZER_CHECKPOINT_RENDER_BUDGET", 12000)


_checkpoint_section = _measure_compat.bind(_measure_providers['continuity']._checkpoint_section, __name__, defaults=("optional", None, 50,))


_cap_section_lines = _measure_compat.bind(_measure_providers['continuity']._cap_section_lines, __name__)


_render_checkpoint_sections = _measure_compat.bind(_measure_providers['continuity']._render_checkpoint_sections, __name__, defaults=(None,))


_checkpoint_sections = _measure_compat.bind(_measure_providers['continuity']._checkpoint_sections, __name__, defaults=(None, None,))


compact_capture = _measure_compat.bind(_measure_providers['continuity'].compact_capture, __name__, defaults=(None, None, "auto", None, None, None, False, None,))


# U-B: cap on credited checkpoint-restore recovery, so a pathological working
# set can never inflate the realized number. ~a large-but-bounded context.
_CHECKPOINT_RECOVERY_TOKEN_CAP = 200_000


_checkpoint_restore_recovery_tokens = _measure_compat.bind(_measure_providers['continuity']._checkpoint_restore_recovery_tokens, __name__)


_neutralize_recovered_body = _measure_compat.bind(_measure_providers['continuity']._neutralize_recovered_body, __name__, defaults=(4000,))


_extract_checkpoint_section = _measure_compat.bind(_measure_providers['continuity']._extract_checkpoint_section, __name__)


_remove_checkpoint_section = _measure_compat.bind(_measure_providers['continuity']._remove_checkpoint_section, __name__)


_checkpoint_has_restorable_body = _measure_compat.bind(_measure_providers['continuity']._checkpoint_has_restorable_body, __name__)


_order_checkpoints_for_restore = _measure_compat.bind(_measure_providers['continuity']._order_checkpoints_for_restore, __name__)


_merge_norm = _measure_compat.bind(_measure_providers['continuity']._merge_norm, __name__)


_checkpoint_time_label = _measure_compat.bind(_measure_providers['continuity']._checkpoint_time_label, __name__)


_merge_earlier_checkpoint_context = _measure_compat.bind(_measure_providers['continuity']._merge_earlier_checkpoint_context, __name__, defaults=(3,))


_open_work_signals = _measure_compat.bind(_measure_providers['continuity']._open_work_signals, __name__)


_opening_context_text = _measure_compat.bind(_measure_providers['continuity']._opening_context_text, __name__)


# --- U3: near-zero-cost statusline existence signal for a resumable checkpoint ---
#
# When an eligible checkpoint exists for a new session (D4: age + own-session
# filtered, whether or not the billed pointer clears the relevance bar), write a
# per-session flag file beside the quality cache. The Claude statusline
# (statusline.js) reads it and shows a compact ``⤸resumable`` token in the
# terminal UI ONLY -- never in any hook ``additionalContext`` payload, so it
# costs zero billed tokens. Codex has no non-billed render surface for this
# (its native status_line is a static item list), so it carries no equivalent
# signal -- see codex_statusline.py. The flag is ts-gated (30 min) so the signal
# does not outlive the resumable window.
_RESUMABLE_FLAG_TTL_MS = 30 * 60 * 1000


_resumable_flag_path = _measure_compat.bind(_measure_providers['continuity']._resumable_flag_path, __name__)


_write_resumable_flag = _measure_compat.bind(_measure_providers['continuity']._write_resumable_flag, __name__, defaults=(False,))


_clear_resumable_flag = _measure_compat.bind(_measure_providers['continuity']._clear_resumable_flag, __name__)


_once_per_session_marker = _measure_compat.bind(_measure_providers['continuity']._once_per_session_marker, __name__)


_ran_once_this_session = _measure_compat.bind(_measure_providers['continuity']._ran_once_this_session, __name__)


_mark_ran_this_session = _measure_compat.bind(_measure_providers['continuity']._mark_ran_this_session, __name__)


_safe_event = _measure_compat.bind(_measure_providers['continuity']._safe_event, __name__)


_emit_additional_context = _measure_compat.bind(_measure_providers['continuity']._emit_additional_context, __name__, defaults=("SessionStart",))


_emit_codex_session_start = _measure_compat.bind(_measure_providers['continuity']._emit_codex_session_start, __name__)


# --- SessionStart stdout contract -------------------------------------------
# Codex (verified against 0.150.0-alpha.12.2, `codex exec` + its own embedded
# `session-start.command.output` JSON schema) parses SessionStart hook stdout
# like this:
#
#   * whitespace-only stdout            -> accepted, no-op
#   * stdout NOT starting with { or [   -> ignored as plain text, accepted
#   * stdout starting with { or [       -> the WHOLE buffer must parse as ONE
#                                          JSON document AND validate against
#                                          the SessionStart output schema
#                                          (additionalProperties: false at both
#                                          levels; hookSpecificOutput requires
#                                          hookEventName). Anything else fails
#                                          with "hook returned invalid session
#                                          start JSON output" and the host
#                                          discards the entire hook result.
#
# Every Token Optimizer human-readable hook line begins with "[Token Optimizer]",
# i.e. a "[" -- Codex reads that as the start of a JSON array and rejects it. Two
# or more concatenated `{"systemMessage": ...}` objects (NDJSON) are rejected for
# the same reason. So SessionStart-path stdout is collapsed here into AT MOST ONE
# JSON object, which both Claude Code and Codex accept.
_HOOK_OUTPUT_TOP_KEYS = ("continue", "stopReason", "suppressOutput", "systemMessage")


_sanitize_hook_output_payload = _measure_compat.bind(_measure_providers['continuity']._sanitize_hook_output_payload, __name__)


_collapse_hook_stdout = _measure_compat.bind(_measure_providers['continuity']._collapse_hook_stdout, __name__, defaults=("SessionStart",))


_emit_hook_stdout_envelope = _measure_compat.bind(_measure_providers['continuity']._emit_hook_stdout_envelope, __name__, defaults=("SessionStart",))


_run_hook_emitting_json = _measure_compat.bind(_measure_providers['continuity']._run_hook_emitting_json, __name__, defaults=("SessionStart",))


compact_restore = _measure_compat.bind(_measure_providers['continuity'].compact_restore, __name__, defaults=(None, None, False, False,))


_checkpoint_work_paths = _measure_compat.bind(_measure_providers['continuity']._checkpoint_work_paths, __name__)


_checkpoint_descriptor = _measure_compat.bind(_measure_providers['continuity']._checkpoint_descriptor, __name__)


_read_checkpoint_sidecar = _measure_compat.bind(_measure_providers['continuity']._read_checkpoint_sidecar, __name__)


_safe_recovered_scalar = _measure_compat.bind(_measure_providers['continuity']._safe_recovered_scalar, __name__, defaults=(160,))


_manifest_tail = _measure_compat.bind(_measure_providers['continuity']._manifest_tail, __name__, defaults=(3,))


_checkpoint_topic_score = _measure_compat.bind(_measure_providers['continuity']._checkpoint_topic_score, __name__, defaults=(None,))


# --- U2: content-based, cwd-free, IDF-weighted checkpoint relevance scorer ---
#
# Defensible relevance of an opening prompt (or any opening-context text) against
# a checkpoint's SIDECAR fields, without folder/path-prefix matching. Overlap
# is weighted by inverse document frequency across the checkpoint pool so generic
# glue ("the", "run", "fix", "build") that appears in every checkpoint cannot
# dominate. Recency is only a weak prior; cwd is an OPTIONAL same-work bonus and
# is never required. Sanitizes harness markup out of sidecar fields first so a
# polluted ``active_task`` (e.g. ``<task-notification>`` noise) cannot skew the
# score. Never raises: a non-UTF-8 / unreadable checkpoint scores 0.0 for itself
# and never aborts the candidate loop.
#
# Calibration source for the threshold: the U7 replay benchmark over the real
# resume/fresh first-prompt mix (tests/baselines/replay-metrics.json). Tuned so
# genuine resumes clear it and fresh/unrelated openings stay below it.
_clamp = _measure_compat.bind(_measure_providers['continuity']._clamp, __name__)


CHECKPOINT_RELEVANCE_THRESHOLD = _clamp(
    _float_env("TOKEN_OPTIMIZER_CHECKPOINT_RELEVANCE_THRESHOLD", 0.25), 0.1, 0.9
)

# Recency prior: a small, weak bonus for a fresh checkpoint, never enough on its
# own to clear the threshold (a bare "continue" with no topical or same-work
# signal must stay below the bar -- the bare-continue guard). Window matches the legacy scorer.
_RELEVANCE_RECENCY_BONUS = 0.05
_RELEVANCE_RECENCY_WINDOW_MIN = 180
# Optional same-work bonus when a caller hands a cwd and the checkpoint's
# working set lives under it. Small enough that content overlap still governs;
# the scorer works WITHOUT it.
_RELEVANCE_CWD_BONUS = 0.10
# Resume-intent bonus (U6 calibration): when the prompt carries a resume cue
# ("continue working on...", "resume the...", "pick up where...") AND there is
# SOME content overlap (content_score > 0), add this bonus. Real resume prompts
# are often long and verbose, so pure precision under-scores them; the bonus
# lifts genuine resume prompts over the threshold while fresh prompts that
# happen to mention a topic (no resume cue) stay below. A bare "continue" with
# no topical tokens has content_score = 0.0, so it gets NO bonus and stays
# below the bar.
_RELEVANCE_RESUME_INTENT_BONUS = 0.15
# The resume-intent bonus is scaled by precision so a prompt that names a
# DIFFERENT project and only grazes a shared container word cannot ride the flat
# cue over the threshold (cross-client false positive). A FLOOR keeps genuine but
# verbose resumes -- which name the right project yet pad with unmatched words
# ("...for full parity of the recent changes") -- from being over-penalized:
# effective factor = FLOOR + (1-FLOOR)*precision, so precision only modulates the
# top (1-FLOOR) of the bonus. Env-tunable.
_RELEVANCE_RESUME_BONUS_PRECISION_FLOOR = _clamp(
    _float_env("TOKEN_OPTIMIZER_RELEVANCE_RESUME_BONUS_PRECISION_FLOOR", 0.5), 0.0, 0.99
)

# D3 (stuffing defense): cap each term's IDF contribution so a single ultra-rare
# token cannot spike the score, and length-normalize the overlap (see
# checkpoint_relevance_score) so an opening that pads itself with many keywords
# cannot reach 1.0 by precision alone. Calibrated so genuine resumes still clear.
_RELEVANCE_IDF_CAP = _clamp(_float_env("TOKEN_OPTIMIZER_RELEVANCE_IDF_CAP", 3.0), 1.0, 100.0)

# Path-identity weighting (root-cause fix): a checkpoint's project identity lives
# in its file PATHS, not its prose. A word that recurs across many of a
# checkpoint's modified_files / recent_reads paths (meridian x10, attention x10,
# optimizer x10) is a strong identity signal; the SAME word appearing once in an
# active_task/decision sentence is incidental. Because the doc token set is
# frequency-blind, an unrelated review/handoff checkpoint that merely QUOTES a
# project name in prose scored identically to the real project checkpoint. So we
# weight each doc-side token by how often it appears in the checkpoint's PATHS
# (capped, D3-style, so one mega-repeated word cannot dominate). Prose-only
# tokens keep weight 1.0, honoring "use active_task as a weak secondary signal at
# most". Query side and IDF are untouched, so the bare-continue guard (empty
# topical set -> content 0.0 -> no bonus) and the fresh-precision negatives are
# unaffected: this only re-ranks checkpoints that ALREADY share topical tokens.
_RELEVANCE_PATH_TF_WEIGHT = _clamp(
    _float_env("TOKEN_OPTIMIZER_RELEVANCE_PATH_TF_WEIGHT", 0.5), 0.0, 2.0)
_RELEVANCE_PATH_TF_CAP = int(_clamp(
    _int_env("TOKEN_OPTIMIZER_RELEVANCE_PATH_TF_CAP", 8), 1, 50))

# FILESYSTEM-SCAFFOLDING words. A single-client checkpoint pool has
# uniform IDF, so structural container words (retainer, deliverables, clients,
# reports) weigh the same as true project-identity words. These are NOT project
# names; they name the folder skeleton every project shares. They are EXCLUDED
# from the hit set (so they grant no path-TF boost and a resume that only grazes
# them fires no bonus) but KEPT in the doc denominator (so the cross-client recall
# guard keeps its teeth). A genuine resume must still hit >=1 non-scaffolding
# identity token for content to score. Proven necessary: compound-segment-only
# (no lexical stoplist) FAILS in a diverse multi-client pool.
_CHECKPOINT_SCAFFOLD_STOPWORDS = frozenset({
    # folder-skeleton container words
    "retainer", "deliverables", "deliverable", "clients", "client",
    "reports", "report", "references", "reference", "scripts", "script",
    # NOTE: only UNIVERSAL filesystem/dev-scaffolding words belong here -- never a
    # word that can be part of a real project slug. "company"/"brain" were removed
    # (2026-08-12): they name a real sub-project (meridian-company-brain), and listing
    # them made "continue working on the company brain" score 0.0 on a genuine
    # resume (a curated-against-one-tree false negative).
    "config", "configs", "src", "lib", "libs",
    "app", "apps", "data", "notes", "note", "file", "files", "docs", "doc",
    "projects", "project", "sessions", "session", "build", "builds",
    # file-extension + doc-type basename noise: a dated brief filename like
    # ``2026-08-11__BRIEF.html`` splits into {brief, html} -- shared by EVERY
    # client's reports folder, so it is scaffolding, not project identity. Without
    # these a pasted path false-matched a different client on the shared basename.
    # (Only len>=4 Latin tokens survive _topic_token_kept, so 2-3 char extensions
    # like md/py/js/yml are already dropped; these are the >=4 ones.)
    "html", "htm", "json", "yaml", "toml", "xml", "csv", "jpeg", "text",
    "brief", "draft", "readme", "index", "summary", "main", "tests", "test",
    "utils", "util", "dist", "temp", "tmp", "cache",
})

# Harness / task-notification markup that can pollute ``active_task``. Stripped
# before tokenizing so forged sentinels and scheduler noise do not masquerade as
# topical content. Tags are removed wholesale; the residual text is then passed
# through _safe_recovered_scalar for control-char / whitespace normalization.
_SIDECAR_MARKUP_TAG_RE = re.compile(r"<[A-Za-z/][^<>]*>")
_SIDECAR_MARKUP_SENTINELS = (
    "task-notification", "system:", "scheduled task", "[token optimizer]",
)


_sanitize_sidecar_text = _measure_compat.bind(_measure_providers['continuity']._sanitize_sidecar_text, __name__, defaults=(400,))


# File-path words carry a checkpoint's project identity. Real checkpoints store
# identity in the DIRECTORY segments of their file paths, e.g.
# .../projects/meridian/.../meridian-competitor-monitor/reports/2026-08-11__BRIEF.html
# -- the distinctive words (meridian, competitor, monitor) live in the dirs, not the
# basename. Split the WHOLE path (dirs AND basename) on / \ - _ . : and whitespace
# so that path -> {clients, meridian, competitor, monitor, reports, ...} and a
# natural spoken prompt ("the meridian competitor monitor") can overlap it. Pool IDF
# then down-weights the generic containers (users, clients, projects, reports,
# retainer, deliverables) that appear in most checkpoints, so no hand-kept
# stoplist is needed. Kept separate from _TOPIC_TOKEN_RE (which deliberately KEEPS
# separators inside a token, i18n parity) so non-path fields are untouched.
_PATH_WORD_SPLIT_RE = re.compile(r"[\\/\-_.:\s]+")


_path_topic_words = _measure_compat.bind(_measure_providers['continuity']._path_topic_words, __name__)


_checkpoint_path_tf = _measure_compat.bind(_measure_providers['continuity']._checkpoint_path_tf, __name__)


_checkpoint_sidecar_doc_tokens = _measure_compat.bind(_measure_providers['continuity']._checkpoint_sidecar_doc_tokens, __name__)


checkpoint_relevance_score = _measure_compat.bind(_measure_providers['continuity'].checkpoint_relevance_score, __name__, defaults=(None, None,))


codex_prompt_hints = _measure_compat.bind(_measure_providers['continuity'].codex_prompt_hints, __name__, defaults=("", None, None, 60 * 24 * 7,))


# Natural-language cue that the user wants to pick up prior work ("continue the
# token-optimizer work, check what we discussed last time"). When present, the
# continuity hook upgrades from a one-line hint to a FULL lean reconstruction,
# scoped to the same project. Kept tight to avoid firing on incidental "continue".
_RESUME_INTENT_RE = re.compile(
    # H2: "continue <path>" -- a bare "continue" followed by a pasted absolute or
    # relative path ("continue /Users/.../meridian-competitor-monitor",
    # "continue \\srv\\share\\...") is a resume cue: the path IS the topic. Matched
    # without a trailing \b so it fires on the leading slash/backslash/tilde/dot.
    r"(?:continue\s+[\\/~.])|"
    r"\b(last session|previous session|prior session|earlier session|last time|"
    r"where we left off|pick(?:ing)? up where|"
    # "from" added so "continue from checkpoint" / "continue from where we left off"
    # is recognized; the cue is the verb "continue from", NOT a bare "from
    # checkpoint" (which would false-match "import data from checkpoint file").
    # "to" is deliberately EXCLUDED (D2): "continue to <verb>" ("continue to
    # refactor", "continue to write the tests") means keep doing the CURRENT task,
    # not resume a prior session -- matching it re-opened fresh-session lean
    # injection on Codex for any such prompt in a project with recent checkpoints.
    r"continue (?:working|where|on|our|the|with|that|this|from)|"
    r"carry on (?:with|where)|what we (?:discussed|talked about|were (?:doing|working))|"
    r"resume (?:our|that|this|work|the (?:work|session|project|task|conversation|thread|discussion))|"
    r"recap (?:of )?(?:our|the|last)|"
    r"yesterday we|earlier we|we were working on)\b",
    re.IGNORECASE,
)

# Distinctive-topic bar: above this, the prompt names a topic (e.g. "the token
# optimizer one") so we keyword-rank; below it, the ask is vague ("continue last
# session") so we fall back to the most-recent same-project checkpoint.
_RESUME_NAMED_TOPIC_BAR = _float_env("TOKEN_OPTIMIZER_RESUME_TOPIC_BAR", 0.22)

# Staleness cap for the VAGUE-continue fallback: when the prompt
# names no distinguishing topic we may only surface the most-recent same-project
# checkpoint if it is fresher than this. Beyond it, blindly reopening whichever
# sibling session was last active in the same folder is how an UNRELATED session's
# checkpoint gets injected -- so we return nothing instead of guessing. The
# keyword-winner path (topic named) is NOT capped; only the recency fallback is.
_RESUME_RECENCY_CAP_MIN = _int_env("TOKEN_OPTIMIZER_RESUME_RECENCY_MIN", 480)

# A session id named IN the prompt ("continue session aaaa1111", full UUID, or
# "session id: <uuid>"). Two shapes: an 8-40 hex run (or full 8-4-4-4-12 UUID)
# that immediately follows the word "session" (optionally "id"), OR a bare full
# UUID anywhere. A bare short hex token NOT after "session" is deliberately NOT
# matched, so ordinary hex words in the prompt aren't mistaken for a session id.
# The "session" branch is listed first and captures a full UUID greedily so
# "...session <uuid>" yields the whole id, not just its first 8-hex chunk.
_UUID_RE_SRC = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_PROMPT_SESSION_ID_RE = re.compile(
    r"\bsession(?:[\s_-]*id)?[\s:#]+(" + _UUID_RE_SRC + r"|[0-9a-f]{8,40})\b"
    r"|\b(" + _UUID_RE_SRC + r")\b",
    re.IGNORECASE,
)


_extract_session_id_from_prompt = _measure_compat.bind(_measure_providers['continuity']._extract_session_id_from_prompt, __name__)


# M2: CJK / non-Latin resume cues (继续 zh, 이어서 / 계속 ko, 続ける ja). Kept in a
# SEPARATE regex, consulted only for intent DETECTION -- deliberately NOT part of
# _RESUME_INTENT_RE, whose .sub() strips cues from a prompt residual. CJK scripts
# have no word boundaries, so a glued Han run ("继续数据库迁移工作") is ONE token;
# stripping "继续" from the prompt but not the checkpoint would break the identical-
# run match the i18n parity fixture pins. Detection-only avoids that.
_CJK_RESUME_CUE_RE = re.compile(r"继续|이어서|계속|続ける")


_resume_intent = _measure_compat.bind(_measure_providers['continuity']._resume_intent, __name__)


# Generic glue words that carry no topic once the resume cue is removed; keeping
# them would let "session"/"work" falsely match every checkpoint.
_RESUME_TOPIC_STOPWORDS = frozenset({
    "session", "sessions", "work", "working", "worked", "continue", "resume",
    "last", "time", "previous", "prior", "earlier", "thing", "things", "stuff",
    "check", "discussed", "talked", "about", "where", "left", "back", "again",
    "what", "that", "this", "with", "from", "into", "please", "yesterday",
})


# --- Non-English topic tokenizer — single source of truth on the Python side ---
# Two branches: an ASCII/accented-Latin run, OR a whole non-ASCII run as one token, so a token
# never mixes ASCII and non-ASCII ("measure.py를" -> "measure.py" + "를").
# The Latin-1/Extended-A ranges deliberately skip U+00D7 (×) and U+00F7 (÷): they sit inside
# the block but are math symbols, not letters. U+0100–U+024F is Latin Extended-A/B, all letters.
_TOPIC_TOKEN_RE = re.compile(r"[a-zA-Z0-9_.:À-ÖØ-öø-ÿĀ-ɏ/-]+|[^\x00-\x7F]+")
# Script-aware length floor, defined by fixed code-point ranges so Python, OpenClaw and
# OpenCode classify every character the same way on any Unicode version.
#   * Any char at or above U+3000 (Han, Kana, Hangul, fullwidth forms): a 2-char word such as
#     결제 (payment) or 모듈 (module) is a full topic, so keep len>=2.
#   * Other non-Latin letters (Cyrillic, Greek, Arabic, Hebrew, Indic...): keep len>=3, which
#     keeps "кот" but drops 2-letter function words such as "на", "في", "של".
#   * Latin (ASCII, accented, extended, letterlike): the len>3 English stopword heuristic.
# Combining marks (accents, niqqud, harakat) never count toward the length.
_TOPIC_WIDE_MIN = 0x3000
_TOPIC_LATIN_RANGES = (
    (0x0000, 0x036F), (0x1D00, 0x1DFF), (0x1E00, 0x1EFF), (0x2000, 0x214F),
    (0x2C60, 0x2C7F), (0xA720, 0xA7FF), (0xAB30, 0xAB6F),
)
_TOPIC_MARK_RANGES = (
    (0x0300, 0x036F), (0x0483, 0x0489), (0x0591, 0x05C7), (0x0610, 0x061A), (0x064B, 0x065F),
    (0x0670, 0x0670), (0x06D6, 0x06ED), (0x1AB0, 0x1AFF), (0x1DC0, 0x1DFF), (0x200B, 0x200F),
    (0x20D0, 0x20FF), (0xFE00, 0xFE0F), (0xFE20, 0xFE2F),
)


_in_ranges = _measure_compat.bind(_measure_providers['continuity']._in_ranges, __name__)


_topic_token_kept = _measure_compat.bind(_measure_providers['continuity']._topic_token_kept, __name__)


_topic_tokens = _measure_compat.bind(_measure_providers['continuity']._topic_tokens, __name__, defaults=(frozenset(),))


_resume_topic_score = _measure_compat.bind(_measure_providers['continuity']._resume_topic_score, __name__)


_checkpoint_in_project = _measure_compat.bind(_measure_providers['continuity']._checkpoint_in_project, __name__)


# Tokenizer for the per-item keep/drop rule. DELIBERATELY ASCII-only, and DELIBERATELY
# NOT the (wider) resume-topic tokenizer _TOPIC_TOKEN_RE. Do not "unify" them:
# widening this to match non-ASCII would make a non-Latin item produce 3+ tokens that then
# fail the ASCII-only keep-set overlap test, dropping needed lines from the selected session.
# The keep set is built from the prompt + cwd + in-project paths, which are ASCII in practice,
# so a non-Latin item must stay inconclusive (<3 tokens) and be kept, not overlap-tested.
_RECOVER_TOKEN_RE = re.compile(r"[a-zA-Z0-9_./:-]+")


_recover_item_tokens = _measure_compat.bind(_measure_providers['continuity']._recover_item_tokens, __name__)


_keep_recovered_item = _measure_compat.bind(_measure_providers['continuity']._keep_recovered_item, __name__)


_cwd_roots = _measure_compat.bind(_measure_providers['continuity']._cwd_roots, __name__)


_path_under_cwd = _measure_compat.bind(_measure_providers['continuity']._path_under_cwd, __name__)


_is_absolute_path = _measure_compat.bind(_measure_providers['continuity']._is_absolute_path, __name__)


_cross_project_file_drop = _measure_compat.bind(_measure_providers['continuity']._cross_project_file_drop, __name__)


_checkpoint_has_cross_project_path = _measure_compat.bind(_measure_providers['continuity']._checkpoint_has_cross_project_path, __name__)


_in_project_paths = _measure_compat.bind(_measure_providers['continuity']._in_project_paths, __name__)


_continuity_keep_tokens = _measure_compat.bind(_measure_providers['continuity']._continuity_keep_tokens, __name__)


_continuity_resume_block = _measure_compat.bind(_measure_providers['continuity']._continuity_resume_block, __name__)


_checkpoint_session_id = _measure_compat.bind(_measure_providers['continuity']._checkpoint_session_id, __name__, defaults=(None,))


_resume_dedup_tag = _measure_compat.bind(_measure_providers['continuity']._resume_dedup_tag, __name__)


_resume_block_already_injected = _measure_compat.bind(_measure_providers['continuity']._resume_block_already_injected, __name__)


_clear_resume_dedup_markers = _measure_compat.bind(_measure_providers['continuity']._clear_resume_dedup_markers, __name__)


_continuity_prompt_hint = _measure_compat.bind(_measure_providers['continuity']._continuity_prompt_hint, __name__, defaults=("", None, None, 60 * 24 * 7,))


# ---------------------------------------------------------------------------
# Cold-resume-lean (keep-warm v2): reopen a forgotten/cold session cheaply by
# reconstructing a LEAN context from data TO already captured (checkpoints +
# session_log), instead of warming a server-side cache with billed pings. The
# whole reconstruction path is token-free: pure sqlite + file reads, no LLM,
# no subprocess to `claude`. The only token cost is the fresh session's normal
# first turn reading the injected block.
# ---------------------------------------------------------------------------

_best_checkpoint_for_session = _measure_compat.bind(_measure_providers['continuity']._best_checkpoint_for_session, __name__, defaults=(False,))


_session_log_row = _measure_compat.bind(_measure_providers['continuity']._session_log_row, __name__)


resume_lean_candidates = _measure_compat.bind(_measure_providers['continuity'].resume_lean_candidates, __name__, defaults=(15, 30,))


_lean_list = _measure_compat.bind(_measure_providers['continuity']._lean_list, __name__, defaults=(140, None,))


build_lean_resume_context = _measure_compat.bind(_measure_providers['continuity'].build_lean_resume_context, __name__, defaults=(3500, None, None, "confident",))


_resume_lean_already_credited = _measure_compat.bind(_measure_providers['continuity']._resume_lean_already_credited, __name__)


# What a cold `claude --resume` re-sends is the target's last-turn context; both
# arms pay the shared system prefix, so it is subtracted from the credit.
_RESUME_LEAN_SHARED_PREFIX_TOKENS = 30_000
# Inside the 1h cache TTL a cold resume is mostly a cache HIT (the prefix is
# byte-identical), so the avoided reload prices at cache_read; past it, the
# cache is gone and the reload is a 1h cache write.
_RESUME_LEAN_WARM_WINDOW_SECONDS = 3600


_transcript_last_turn = _measure_compat.bind(_measure_providers['continuity']._transcript_last_turn, __name__)


_log_resume_lean_savings = _measure_compat.bind(_measure_providers['continuity']._log_resume_lean_savings, __name__)


checkpoint_trigger = _measure_compat.bind(_measure_providers['continuity'].checkpoint_trigger, __name__, defaults=(None, None, None, False,))


_COMPACT_INSTR_HEAD = "When summarizing this session, pay special attention to:"
_COMPACT_INSTR_TAIL = (
    "Always include the specific next step with enough detail to continue without asking."
)
_COMPACT_INSTR_BULLETS = frozenset({
    "- Architectural decisions and their reasoning",
    "- Skill invocations and their outcomes",
    "- External service interactions and their results",
    "- Modified file paths with line ranges",
    "- Error-fix sequences (what was tried, what failed, what worked)",
    "- Open questions and unresolved TODOs",
    "- Agent/team state (task assignments, completion status)",
})
# Separator used when our instructions are appended to the user's own text.
_COMPACT_INSTR_MARKER = "\n\n# Token Optimizer additions:\n"


_compact_instructions_generated = _measure_compat.bind(_measure_providers['continuity']._compact_instructions_generated, __name__)


_split_compact_instructions = _measure_compat.bind(_measure_providers['continuity']._split_compact_instructions, __name__)


_remove_our_compact_instructions = _measure_compat.bind(_measure_providers['continuity']._remove_our_compact_instructions, __name__)


generate_compact_instructions = _measure_compat.bind(_measure_providers['continuity'].generate_compact_instructions, __name__, defaults=(False, False, False, False,))


_DYNAMIC_COMPACT_CAP = 2500
_STATIC_COMPACT_FALLBACK = (
    "COMPACTION GUIDANCE: Preserve code changes, key decisions, "
    "and file paths. Discard intermediate attempts, explanations, "
    "and verbose tool output."
)

_MODE_PRESERVE_HINTS = {
    "code": "Focus: preserve edited files, their test files, and build output. Drop exploration reads.",
    "debug": "Focus: preserve error messages, stack traces, and the investigated file. Drop unrelated reads.",
    "review": "Focus: preserve file list, findings, and decisions. Drop full file contents (summaries suffice).",
    "infra": "Focus: preserve command outputs and config changes. Drop source code reads.",
    "general": "",
}


_build_anchor_state = _measure_compat.bind(_measure_providers['continuity']._build_anchor_state, __name__)


dynamic_compact_instructions = _measure_compat.bind(_measure_providers['continuity'].dynamic_compact_instructions, __name__, defaults=(None,))


# ========== Session Continuity Engine (v2.0) ==========
# Extends Smart Compaction for session death recovery.

keyword_relevance_score = _measure_compat.bind(_measure_providers['continuity'].keyword_relevance_score, __name__)


list_checkpoints = _measure_compat.bind(_measure_providers['continuity'].list_checkpoints, __name__, defaults=(None,))


_CHECKPOINT_NAME_RE = re.compile(r"^(?P<sid>.+)-\d{8}-\d{6}(?:-(?P<trigger>[^.]+))?\.md$")


_checkpoint_session = _measure_compat.bind(_measure_providers['continuity']._checkpoint_session, __name__)


_checkpoints_to_prune = _measure_compat.bind(_measure_providers['continuity']._checkpoints_to_prune, __name__, defaults=(None, None, None, None,))


_cleanup_checkpoints = _measure_compat.bind(_measure_providers['continuity']._cleanup_checkpoints, __name__)


_cleanup_quality_cache = _measure_compat.bind(_measure_providers['continuity']._cleanup_quality_cache, __name__)


_rotate_checkpoint_events = _measure_compat.bind(_measure_providers['continuity']._rotate_checkpoint_events, __name__)


_prune_trends_db = _measure_compat.bind(_measure_providers['continuity']._prune_trends_db, __name__)


_safe_checkpoint_file = _measure_compat.bind(_measure_providers['continuity']._safe_checkpoint_file, __name__)


# ========== Hook Setup: Smart Compaction (v2.0) ==========

def _is_running_from_plugin_cache():
    """Check if this script is running from a Claude Code plugin cache directory."""
    resolved = str(Path(__file__).resolve())
    if os.name == "nt":
        resolved = resolved.replace("\\", "/")
    return "/plugins/cache/" in resolved


def _is_running_from_synced_plugin():
    """Check if this script is running from a Cowork account-synced plugin dir.

    Cowork installs land under ``~/.claude/plugins/synced/<...>/`` (issue
    audit: "the resolver's search paths didn't include the synced plugins
    location"), NOT under ``/plugins/cache/``. That path segment is the reliable
    self-location marker in Cowork -- ``${CLAUDE_PLUGIN_ROOT}`` is not always
    injected into the hook env (Claude Code does not guarantee env injection), so we recognize the synced
    tree by our own resolved location. Desktop paths never contain it.
    """
    resolved = str(Path(__file__).resolve())
    if os.name == "nt":
        resolved = resolved.replace("\\", "/")
    return "/plugins/synced/" in resolved


def _get_measure_py_path():
    """Get the path to this measure.py script.

    When running from a plugin cache OR a Cowork synced-plugin dir, returns a
    ${CLAUDE_PLUGIN_ROOT}-based path so that settings.json hooks survive version
    upgrades / account re-syncs. Otherwise returns the resolved absolute path.
    """
    if _is_running_from_plugin_cache() or _is_running_from_synced_plugin():
        # Use the variable that Claude Code resolves dynamically per version
        return "${CLAUDE_PLUGIN_ROOT}/skills/token-optimizer/scripts/measure.py"
    return str(Path(__file__).resolve())


def _read_settings_json():
    """Read ~/.claude/settings.json, return (data, path).

    Lossy by design for read-only callers: a missing file, malformed JSON, and
    an unreadable file all collapse to ``{}``. Any caller that will WRITE the
    result back must use ``_read_settings_json_checked`` instead -- see the
    checked-read fix,
    where a failed re-read returned ``{}`` and that empty dict was
    written straight over the user's whole settings.json.
    """
    data, path, _ok = _read_settings_json_checked()
    return data, path


def _is_regular_file(path) -> bool:
    """True when ``path`` (symlinks followed) is a regular file. Never opens it."""
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except (OSError, ValueError):
        return False


def _read_settings_json_checked():
    """Read settings.json, return (data, path, ok).

    ``ok`` is False when the file exists but could not be parsed or read
    (malformed JSON, permission denied, I/O error) -- i.e. when ``{}`` means
    "unknown", not "empty". A missing file is ``({}, path, True)``: genuinely
    empty and safe to build on.

    Write paths MUST branch on ``ok`` and refuse to write when it is False;
    round-tripping an unknown-state ``{}`` destroys every key the user has.
    """
    if SETTINGS_PATH.exists():
        # open() on a FIFO blocks until a writer appears, which would
        # stall a SessionStart hook to its deadline. Anything that is not a
        # regular file (FIFO, socket, device, directory) is "unknown", not data.
        if not _is_regular_file(SETTINGS_PATH):
            return {}, SETTINGS_PATH, False
        try:
            # utf-8-sig tolerates a BOM (whether the host tolerates
            # one is not verified, but a BOM-prefixed file must at least be
            # readable here so "unknown" never means "lost"). Non-JSONC
            # comments stay malformed.
            with open(SETTINGS_PATH, "r", encoding="utf-8-sig") as f:
                return json.load(f), SETTINGS_PATH, True
        except (json.JSONDecodeError, UnicodeDecodeError, PermissionError, OSError):
            # UnicodeDecodeError is a ValueError, not an OSError: a cp1252
            # file saved by an ANSI editor is "unknown", not a traceback.
            return {}, SETTINGS_PATH, False
    return {}, SETTINGS_PATH, True


def _read_settings_for_write(allow_missing=False):
    """Read settings.json for a caller that intends to WRITE it back.

    Returns ``(data, ok)``. Unlike ``_read_settings_json_checked``, a MISSING
    file is ``ok=False`` by default.

    Rationale: ``_read_settings_json_checked`` reports a
    missing file as ``ok=True`` on the theory that "genuinely empty is safe to
    build on". For a READ that is fine. For a WRITE it is not: settings.json
    existing is the normal state for any established install, so "the file
    isn't there" is far more likely to be a transient window (a rename, a sync
    tool, another process mid-swap, a relocated CLAUDE_CONFIG_DIR) than a
    genuine fresh install. Rebuilding from ``{}`` in that window is precisely
    how a full settings.json becomes a two-key stub.

    Automated/hook callers should use the default (``allow_missing=False``) and
    simply do nothing when ``ok`` is False -- skipping one self-heal tick is
    recoverable, an erased settings.json is not. Explicit, user-invoked
    installers may pass ``allow_missing=True`` so a genuine first-run install
    can still create the file.
    """
    data, _path, ok = _read_settings_json_checked()
    if not ok:
        return {}, False
    if not isinstance(data, dict):
        # valid JSON that is not an object ([1,2,3], "x", 42) must be
        # the same clean refusal as malformed JSON -- downstream callers
        # mutate the value as a dict and would otherwise traceback.
        return {}, False
    if not SETTINGS_PATH.exists() and not allow_missing:
        return {}, False
    try:
        resolved = str(SETTINGS_PATH.resolve(strict=False))
        _SETTINGS_WRITE_READ_STATE.snapshot = (resolved, copy.deepcopy(data))
    except (OSError, ValueError):
        _SETTINGS_WRITE_READ_STATE.snapshot = None
    return data, True


def _smart_compact_hook_commands():
    """Return the hook commands for smart compaction.

    Each command is fail-open guarded: if measure.py is unreadable (e.g. a
    plugin refresh GC'd the version dir the baked ${CLAUDE_PLUGIN_ROOT} path
    resolved to), the hook exits 0 silently instead of erroring on every event.
    Mirrors the launcher guard in hooks.json.
    """
    py, mp = _settings_hook_python_and_script(_get_measure_py_path())
    guard = f"[ -r {mp} ] || exit 0; "
    return {
        "PreCompact": f"{guard}{py} {mp} compact-capture --trigger auto",
        "SessionStart": f"{guard}{py} {mp} compact-restore",
        "Stop": f"{guard}{py} {mp} compact-capture --trigger stop",
        "SessionEnd": f"{guard}{py} {mp} compact-capture --trigger end",
    }


def _settings_hook_python_and_script(mp):
    """(interpreter, script) shell tokens for a settings.json hook command.

    Claude Code runs these through Git Bash on Windows, where bare ``python3``
    is often missing or the Microsoft Store stub (exit 9009 on every event),
    so Windows names the running interpreter with forward slashes, quoted for
    bash (an apostrophe in the path is safe). POSIX keeps ``python3 '<mp>'``.
    """
    if sys.platform == "win32":
        return (shlex.quote(str(sys.executable).replace("\\", "/")),
                shlex.quote(str(mp).replace("\\", "/")))
    return "python3", f"'{mp}'"


def _is_smart_compact_installed(settings=None):
    """Check which smart compact hooks are installed.

    Returns dict of event -> bool.
    Checks both user settings.json and plugin cache hooks.
    """
    if settings is None:
        settings, _ = _read_settings_json()

    # Merge user hooks with plugin hooks for detection
    all_hooks = dict(settings.get("hooks", {}))

    # Also check plugin cache hooks (marketplace plugin auto-install)
    plugin_cache = CLAUDE_DIR / "plugins" / "cache"
    if plugin_cache.exists():
        import glob as globmod
        for hooks_file in globmod.glob(str(plugin_cache / "*" / "token-optimizer" / "*" / "hooks" / "hooks.json")):
            try:
                with open(hooks_file, "r", encoding="utf-8") as f:
                    plugin_hooks = json.load(f).get("hooks", {})
                for event, groups in plugin_hooks.items():
                    if event not in all_hooks:
                        all_hooks[event] = groups
                    else:
                        all_hooks[event] = all_hooks[event] + groups
            except (json.JSONDecodeError, PermissionError, OSError):
                continue

    status = {}
    for event in ("PreCompact", "SessionStart", "Stop", "SessionEnd"):
        installed = False
        event_hooks = all_hooks.get(event, [])
        for hook_group in event_hooks:
            for hook in hook_group.get("hooks", []):
                cmd = hook.get("command", "")
                # SessionEnd uses session-end-flush in plugin (handles both
                # collection + end-of-session checkpoint). Accept it alongside
                # the older compact-capture / compact-restore signatures.
                if not isinstance(cmd, str):
                    continue
                if _is_compaction_runner(cmd, event) or "measure.py" in cmd and (
                    "compact-capture" in cmd
                    or "compact-restore" in cmd
                    or (event == "SessionEnd" and ("session-end-flush" in cmd or "collect" in cmd))
                ):
                    installed = True
                    break
        status[event] = installed

    return status


def setup_smart_compact(dry_run=False, uninstall=False, status_only=False,
                        user_initiated=False):
    """Install, uninstall, or check status of smart compaction hooks.

    Appends to existing hooks (never overwrites). Safe to run multiple times.
    """
    settings, settings_path = _read_settings_json()
    if not status_only:
        # Every non-status branch below writes `settings` back.
        settings, _ok = _read_settings_for_write(allow_missing=True)
        settings_path = SETTINGS_PATH
        if not _ok:
            print(
                "[Token Optimizer] settings.json is unreadable or malformed; "
                "refusing to modify smart compaction hooks.",
                file=sys.stderr,
            )
            return
    current_status = _is_smart_compact_installed(settings)
    commands = _smart_compact_hook_commands()

    if status_only:
        print("\n  Smart Compaction Hook Status")
        print(f"  {'=' * 40}")
        for event, installed in current_status.items():
            icon = "installed" if installed else "not installed"
            print(f"    {event:15s} {icon}")
        all_installed = all(current_status.values())
        if all_installed:
            print("\n  All hooks installed. Smart Compaction is active.")
        else:
            missing = [e for e, v in current_status.items() if not v]
            print(f"\n  Missing: {', '.join(missing)}")
            print(_hint("  Run: python3 measure.py setup-smart-compact"))
        print()
        return

    if uninstall:
        hooks = settings.get("hooks", {})
        removed = 0
        for event in ("PreCompact", "SessionStart", "Stop", "SessionEnd"):
            if event not in hooks:
                continue
            new_groups = []
            for group in hooks[event]:
                new_hooks = [
                    h for h in group.get("hooks", [])
                    if "compact-capture" not in h.get("command", "")
                    and "compact-restore" not in h.get("command", "")
                ]
                if new_hooks:
                    group["hooks"] = new_hooks
                    new_groups.append(group)
                else:
                    removed += 1
            if new_groups:
                hooks[event] = new_groups
            elif event in hooks:
                del hooks[event]

        if dry_run:
            print(f"\n  [Dry run] Would remove {removed} smart compact hook(s) from {settings_path}")
            print("  Run without --dry-run to apply.\n")
            return

        settings["hooks"] = hooks
        # setup-smart-compact also installed compactInstructions; take ours
        # back out (a user-written part stays).
        ci_removed = _remove_our_compact_instructions(settings)
        if not _write_settings_atomic(settings, allow_removing_keys={"compactInstructions"},
                                      user_initiated=user_initiated):
            print("[Error] settings.json write was refused; smart compact hooks were not removed.", file=sys.stderr)
            return
        print(f"[Token Optimizer] Removed smart compact hooks. {removed} hook(s) removed.")
        if ci_removed:
            print("[Token Optimizer] Removed Token Optimizer compact instructions.")
        return

    # Install
    # Plugin users get all smart compact hooks from hooks.json — skip settings.json.
    # Synced (Cowork) installs get them from the cowork hooks.json too; include the
    # synced check so we never bake a ${CLAUDE_PLUGIN_ROOT} path into settings.json for
    # them (a single-quoted placeholder only resolves on text-substitution hosts).
    is_plugin = (
        _is_running_from_plugin_cache()
        or _is_running_from_synced_plugin()
        or _is_plugin_installed()
    )
    if is_plugin:
        all_active = all(current_status.values())
        if all_active:
            print("[Token Optimizer] Smart Compaction active via plugin hooks.json. Nothing to do.")
        else:
            print("[Token Optimizer] Smart Compaction managed by plugin hooks.json.")
        return

    hooks = settings.setdefault("hooks", {})
    installed = []
    skipped = []

    for event, command in commands.items():
        if event == "SessionStart":
            # SessionStart needs TWO hook groups:
            # 1. Post-compaction recovery (matcher: "compact")
            # 2. New-session checkpoint pointer (no matcher, --new-session-only)
            hooks.setdefault(event, [])
            event_hooks = hooks[event]

            has_compact_matcher = any(
                g.get("matcher") == "compact"
                and any("compact-restore" in h.get("command", "") for h in g.get("hooks", []))
                for g in event_hooks
            )
            new_session_cmd = command + " --new-session-only"
            has_new_session = any(
                "matcher" not in g
                and any("--new-session-only" in h.get("command", "") for h in g.get("hooks", []))
                for g in event_hooks
            )

            added = False
            if not has_compact_matcher:
                event_hooks.append({"matcher": "compact", "hooks": [{"type": "command", "command": command}]})
                added = True
            if not has_new_session:
                event_hooks.append({"hooks": [{"type": "command", "command": new_session_cmd}]})
                added = True

            if added:
                installed.append(event)
            else:
                skipped.append(event)
            continue

        if current_status.get(event):
            skipped.append(event)
            continue

        # Append to existing hook groups for this event
        hook_entry = {"type": "command", "command": command}
        hook_group = {"hooks": [hook_entry]}

        if event not in hooks:
            hooks[event] = []
        hooks[event].append(hook_group)
        installed.append(event)

    if dry_run:
        print("\n  [Dry run] Smart Compaction hook preview")
        print(f"  {'=' * 40}")
        if installed:
            print(f"  Would install hooks for: {', '.join(installed)}")
        if skipped:
            print(f"  Already installed (skip): {', '.join(skipped)}")
        print(f"\n  Settings file: {settings_path}")
        print("  Hook commands:")
        for event in installed:
            print(f"    {event}: {commands[event]}")
        print("\n  Run without --dry-run to apply.\n")
        return

    if not installed:
        print("[Token Optimizer] All smart compact hooks already installed.")
        return

    settings["hooks"] = hooks
    if not _write_settings_atomic(settings, user_initiated=user_initiated):
        print("[Error] settings.json write was refused; smart compact hooks were not installed.", file=sys.stderr)
        return

    print("[Token Optimizer] Smart Compaction installed.")
    print(f"  Hooks added: {', '.join(installed)}")
    if skipped:
        print(f"  Already had: {', '.join(skipped)}")

    # Also install compact instructions (tells Claude WHAT to preserve)
    print()
    generate_compact_instructions(install=True, user_initiated=user_initiated)

    print("\n  What happens now:")
    print("    Compact Instructions: Guides Claude on what to preserve during compaction")
    print("    PreCompact hook:      Captures structured state before compaction")
    print("    SessionStart hook:    Restores what was lost after compaction")
    print("    Stop hook:            Saves checkpoint when session ends normally")
    print("    SessionEnd hook:      Saves checkpoint on /clear or termination")
    print(f"\n  Checkpoints stored in: {CHECKPOINT_DIR}")
    print(_hint("  To remove: python3 measure.py setup-smart-compact --uninstall"))


# same unified base as CHECKPOINT_DIR/SNAPSHOT_DIR. RUNTIME_DIR on desktop
# (unchanged); the resolved plugin-data dir in Cowork, so quality-cache,
# resumable-*.json, and once-*.json run-once markers stop double-writing to two
# bases and the once-per-session guard fires exactly once.
QUALITY_CACHE_DIR = _STATE_BASE / "token-optimizer"
QUALITY_CACHE_PATH = QUALITY_CACHE_DIR / "quality-cache.json"  # legacy global fallback


_quality_cache_path_for = _measure_compat.bind(_measure_providers['quality_archive']._quality_cache_path_for, __name__, defaults=(None,))


_quality_cache_throttle_marker = _measure_compat.bind(_measure_providers['quality_archive']._quality_cache_throttle_marker, __name__, defaults=(None, None, None,))


_quality_cache_tick_due = _measure_compat.bind(_measure_providers['quality_archive']._quality_cache_tick_due, __name__, defaults=(None, None,))


# --- Stale-lease sweeper throttle ------------------------------------
# The sweeper itself lives in hook_runtime._sweep_stale_leases (fail-open, never
# raises). It is invoked here OPPORTUNISTICALLY + THROTTLED to at most once per
# 24h via a one-stat marker, mirroring the quality-cache throttle pattern. It is
# wired into ensure-health (SessionStart), NEVER the hot per-tool-call path, so
# the scan cost never lands on PostToolUse. The marker is touched AFTER a run so
# a sweeper crash/timeout retries next session rather than waiting a full day.
_QLEASE_SWEEP_THROTTLE_SECONDS = 86400
_QLEASE_SWEEP_MARKER = QUALITY_CACHE_DIR / ".qlease-sweep-throttle"


_qlease_sweep_due = _measure_compat.bind(_measure_providers['quality_archive']._qlease_sweep_due, __name__)


maybe_sweep_stale_leases = _measure_compat.bind(_measure_providers['quality_archive'].maybe_sweep_stale_leases, __name__, defaults=(None,))


_write_checkpoint_atomic = _measure_compat.bind(_measure_providers['quality_archive']._write_checkpoint_atomic, __name__)


# --- Hook wall-clock guard ----------------------------------------------------
# Hook handlers invoked from hooks/hooks.json are hard-stopped if they exceed a
# wall-clock budget. HookDeadline uses one daemon-watchdog implementation on
# Linux, macOS, Windows/MSYS2, and Codex; it can interrupt blocking I/O where a
# signal flag or cooperative timer cannot.
#
# _HookTimeout inherits from BaseException (not Exception) so inner `except
# Exception: pass` blocks inside guarded handlers do NOT swallow it. Same
# pattern Python uses for KeyboardInterrupt and SystemExit. The outer
# dispatch's `except _HookTimeout` still catches it by exact type.

_HookTimeout = _measure_compat.bind(_measure_providers['quality_archive']._HookTimeout, __name__)


_install_hook_budget = _measure_compat.bind(_measure_providers['quality_archive']._install_hook_budget, __name__, defaults=(8, None,))


# Just under hook_runtime.BUDGET_PRECOMPACT_CAPTURE (15 s), so this inner guard
# is the one that ends a slow PreCompact save, with room past the 7-9 s measured
# on Windows (#253). Stop/StopFailure stay capped by their 2.5 s entry budget.
_COMPACT_CAPTURE_BUDGET_DEFAULT_S = 14
_COMPACT_CAPTURE_BUDGET_MAX_S = 60


_compact_capture_budget_seconds = _measure_compat.bind(_measure_providers['quality_archive']._compact_capture_budget_seconds, __name__)


_dashboard_hook_budget_seconds = _measure_compat.bind(_measure_providers['quality_archive']._dashboard_hook_budget_seconds, __name__, defaults=(20,))


_arm_dashboard_selfheal_on_timeout = _measure_compat.bind(_measure_providers['quality_archive']._arm_dashboard_selfheal_on_timeout, __name__, defaults=(30,))


_running_under_hook = _measure_compat.bind(_measure_providers['quality_archive']._running_under_hook, __name__)


_settings_has_any_to_hooks = _measure_compat.bind(_measure_providers['quality_archive']._settings_has_any_to_hooks, __name__)


_should_skip_self_heal_hooks = _measure_compat.bind(_measure_providers['quality_archive']._should_skip_self_heal_hooks, __name__)


_clear_hook_budget = _measure_compat.bind(_measure_providers['quality_archive']._clear_hook_budget, __name__)


_write_quality_cache = _measure_compat.bind(_measure_providers['quality_archive']._write_quality_cache, __name__)


_extract_session_start_ts = _measure_compat.bind(_measure_providers['quality_archive']._extract_session_start_ts, __name__)


_extract_active_agents = _measure_compat.bind(_measure_providers['quality_archive']._extract_active_agents, __name__)


_read_quality_cache = _measure_compat.bind(_measure_providers['quality_archive']._read_quality_cache, __name__)


_checkpoint_cooldown_remaining = _measure_compat.bind(_measure_providers['quality_archive']._checkpoint_cooldown_remaining, __name__)


_record_checkpoint_metadata = _measure_compat.bind(_measure_providers['quality_archive']._record_checkpoint_metadata, __name__, kwdefaults={'fill_pct': None, 'quality_score': None})


_append_checkpoint_event = _measure_compat.bind(_measure_providers['quality_archive']._append_checkpoint_event, __name__, kwdefaults={'fill_pct': None, 'quality_score': None})


checkpoint_stats = _measure_compat.bind(_measure_providers['quality_archive'].checkpoint_stats, __name__, defaults=(7, False,))


_current_edit_batch_stats = _measure_compat.bind(_measure_providers['quality_archive']._current_edit_batch_stats, __name__)


_maybe_checkpoint_on_quality_or_milestone = _measure_compat.bind(_measure_providers['quality_archive']._maybe_checkpoint_on_quality_or_milestone, __name__)


_maybe_progressive_checkpoint = _measure_compat.bind(_measure_providers['quality_archive']._maybe_progressive_checkpoint, __name__)


_NUDGE_COOLDOWN_SECONDS = 300  # 5 minutes between nudges

# Fill % at which the gentle lean-output nudge becomes eligible. The behavioral
# SessionEfficiency gate below keeps healthy sessions quiet even when fill is
# moderate. Named + env-tunable rather than inline, because the previous magic
# 25 was documented as three different numbers across the docs.
_verbosity_nudge_min_fill = _measure_compat.bind(_measure_providers['quality_archive']._verbosity_nudge_min_fill, __name__)


_VERBOSITY_NUDGE_MIN_FILL = _verbosity_nudge_min_fill()
_VERBOSITY_NUDGE_EFFICIENCY_THRESHOLD = 75
# Lean-output nudge pacing: at most this many per session, this far apart.
_VERBOSITY_MAX_NUDGES = 3
_VERBOSITY_COOLDOWN_SEC = 300
_NUDGE_SESSION_CAP = 3
# Fresh-session nudge: when a session is BOTH long (high fill) and degraded
# (quality < threshold), suggest starting a fresh session -- cold-resume-lean
# rebuilds the context for free, so nothing is lost. Fires once per session.
_FRESH_NUDGE_QUALITY_THRESHOLD = _int_env("TOKEN_OPTIMIZER_FRESH_NUDGE_QUALITY", 70)
_FRESH_NUDGE_MIN_FILL = _int_env("TOKEN_OPTIMIZER_FRESH_NUDGE_MIN_FILL", 45)
_FRESH_NUDGE_LEAN_BLOCK_TOKENS = 1000  # what a fresh lean-resume re-injects
# A2: a compaction only counts as nudge follow-through if it happens within this
# window of the nudge firing. A compaction hours later was not a response to it.
_NUDGE_FOLLOWTHROUGH_WINDOW_SECONDS = 600  # 10 minutes
_LOOP_SESSION_CAP = 2
_LOOP_LAST_MESSAGES = 4

# Transient nudge/loop/warning state that must survive across the separate
# UserPromptSubmit and PostCompact hook processes. compute_quality_score() builds
# a fresh result each run, so quality_cache() carries these forward from the prior
# on-disk cache. Any new "_nudge_*"/"_loop_*"/"_warn_*"/warning-dedup key set in
# _maybe_nudge, _maybe_loop_warning, or _maybe_quality_warn MUST be registered
# here — a sibling key silently missing from this set is exactly what broke the
# nudge follow-through credit (A6).
_COMPACT_BUMP_WINDOW_S = 60
_COMPACT_BOUNDARY_FRESH_S = 120
_COMPACT_TAIL_BYTES = 4 << 20


_recent_compact_boundary = _measure_compat.bind(_measure_providers['quality_archive']._recent_compact_boundary, __name__, defaults=(_COMPACT_BOUNDARY_FRESH_S, None,))


_settled_compactions = _measure_compat.bind(_measure_providers['quality_archive']._settled_compactions, __name__, defaults=(None,))


_CARRY_KEYS = (
    "_nudge_fill_pct_at_fire",
    "_nudge_fire_epoch",
    "_nudge_count",
    "_nudge_last_epoch",
    "_nudge_previous_score",
    "_fresh_nudge_fired",
    "_loop_warning_count",
    "progressive_bands_captured",
    "_last_fill_warn_level",
    "_last_tool_call_warn_level",
    "_warn_count",
    "_warn_last_epoch",
    "_warn_last_band",
    "_context_advisory_fired",
)


_quality_scores_comparable = _measure_compat.bind(_measure_providers['quality_archive']._quality_scores_comparable, __name__)


_check_realtime_loops = _measure_compat.bind(_measure_providers['quality_archive']._check_realtime_loops, __name__)


_checkpoint_restore_credited_recently = _measure_compat.bind(_measure_providers['quality_archive']._checkpoint_restore_credited_recently, __name__, defaults=(_NUDGE_FOLLOWTHROUGH_WINDOW_SECONDS,))


_fresh_session_savings_estimate = _measure_compat.bind(_measure_providers['quality_archive']._fresh_session_savings_estimate, __name__, defaults=(None,))


_fresh_session_turn_economics = _measure_compat.bind(_measure_providers['quality_archive']._fresh_session_turn_economics, __name__, defaults=(None,))


_maybe_fresh_session_nudge = _measure_compat.bind(_measure_providers['quality_archive']._maybe_fresh_session_nudge, __name__, defaults=(False,))


_maybe_nudge = _measure_compat.bind(_measure_providers['quality_archive']._maybe_nudge, __name__, defaults=(False,))


# --- Absolute-token gate + context-size advisory -------------------------
# One gate, shared by every compaction/restart-recommending surface: the
# live context must measure >=300K tokens with real telemetry. Below that a
# size remedy is never suggested, no matter how bad the score looks.
_SIZE_NUDGE_MIN_TOKENS = 300_000

# Context-size advisory (COMPACTION-QUALITY.md section 1): once per session,
# suggest *considering* compaction at a safe pause when current context passes
# the configured threshold. Eligibility, never an instruction.
_CONTEXT_ADVISORY_DEFAULT_TOKENS = 350_000
_CONTEXT_ADVISORY_MIN_TOKENS = 300_000
_CONTEXT_ADVISORY_MAX_TOKENS = 400_000
_CONTEXT_ADVISORY_POST_COMPACT_TOKENS = 35_000
_CONTEXT_ADVISORY_SHADOW_TOKENS = (300_000, 350_000, 400_000)
_CONTEXT_ADVISORY_TELEMETRY_MAX_AGE_S = 300


_absolute_context_tokens = _measure_compat.bind(_measure_providers['quality_archive']._absolute_context_tokens, __name__, defaults=(None,))


_context_advisory_threshold = _measure_compat.bind(_measure_providers['quality_archive']._context_advisory_threshold, __name__)


_advisory_checkpoint_has_restorable_body = _measure_compat.bind(_measure_providers['quality_archive']._advisory_checkpoint_has_restorable_body, __name__)


_session_has_current_checkpoint = _measure_compat.bind(_measure_providers['quality_archive']._session_has_current_checkpoint, __name__, defaults=(None,))


_advisory_blockers = _measure_compat.bind(_measure_providers['quality_archive']._advisory_blockers, __name__)


_log_context_advisory_shadow = _measure_compat.bind(_measure_providers['quality_archive']._log_context_advisory_shadow, __name__)


_maybe_context_size_advisory = _measure_compat.bind(_measure_providers['quality_archive']._maybe_context_size_advisory, __name__, defaults=(False,))


_maybe_quality_warn = _measure_compat.bind(_measure_providers['quality_archive']._maybe_quality_warn, __name__)


_maybe_loop_warning = _measure_compat.bind(_measure_providers['quality_archive']._maybe_loop_warning, __name__, defaults=(False,))


_acquire_quality_lock = _measure_compat.bind(_measure_providers['quality_archive']._acquire_quality_lock, __name__, defaults=(0.15, False,))


_release_quality_lock = _measure_compat.bind(_measure_providers['quality_archive']._release_quality_lock, __name__)


quality_cache = _measure_compat.bind(_measure_providers['quality_archive'].quality_cache, __name__, defaults=(120, 70, False, None, False, False, None, False, False,))


def _stable_marketplace_script_path(script_name):
    """Version-INDEPENDENT path to a bundled script, or None.

    Plugin-cache installs live at::

        <claude>/plugins/cache/<marketplace>/<plugin>/<VERSION>/skills/...

    so any absolute path written into settings.json dies on the next update.
    The marketplace clone at::

        <claude>/plugins/marketplaces/<marketplace>/skills/...

    carries the same tree at a path with no version segment, so it survives
    updates. Returns that path only when it actually exists and stays confined
    under the runtime marketplaces dir (no symlink escape); otherwise None, so
    the caller falls back to the version-pinned write plus self-heal.
    """
    try:
        here = Path(__file__).resolve()
        parts = here.parts
        idx = len(parts) - 1 - parts[::-1].index("cache")
        marketplace = parts[idx + 1]
    except (ValueError, IndexError, OSError):
        return None
    try:
        base = (CLAUDE_DIR / "plugins" / "marketplaces").resolve(strict=False)
        candidate = base / marketplace / "skills" / "token-optimizer" / "scripts" / script_name
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file() or not resolved.is_relative_to(base):
            return None
    except (OSError, ValueError):
        return None
    return str(resolved)


def _get_statusline_path():
    """Get the path to the bundled statusline.js script.

    Always returns an absolute path. Unlike hook commands in hooks.json,
    settings.json statusLine may not resolve ${CLAUDE_PLUGIN_ROOT} -- which is
    why this cannot simply mirror _get_measure_py_path().

    Plugin-cache installs previously got the VERSION-PINNED cache path, which
    breaks the moment the plugin updates. _fix_stale_settings_paths() repairs
    it, but only at the NEXT SessionStart, so the first session after an update
    rendered a blank status line before the heal landed. Prefer the
    version-independent marketplace clone path when it resolves; fall back to
    the version-pinned path (still self-healed) when it does not -- a pruned
    marketplace clone, or any non-plugin-cache install.

    Either way the self-disabling guard inside statusline.js still applies:
    both candidate trees ship the sibling files it checks for.
    """
    if _is_running_from_plugin_cache():
        stable = _stable_marketplace_script_path("statusline.js")
        if stable:
            return stable
    return str(Path(__file__).resolve().parent / "statusline.js")


def _fix_stale_settings_paths():
    """Detect and fix stale versioned plugin cache paths in settings.json.

    When a plugin updates (e.g., 3.0.0 -> 3.1.0), any hooks or statusLine
    entries written to settings.json with the old versioned path break silently.

    Since this runs from ensure-health (called at SessionStart via the NEW
    version's hooks.json), Path(__file__).resolve() gives us the current
    version's path. We find any old versioned paths and rewrite them.

    Works by replacing paths in the serialized JSON, which handles all keys
    (hooks, statusLine, and any future settings) without key-specific iteration.

    Note: This rewrites old versioned paths to the current version's absolute
    path, not to ${CLAUDE_PLUGIN_ROOT}. The variable may not be resolved in
    settings.json. This creates a self-healing loop (3.0.0 → 3.1.0, then
    3.1.0 → 3.2.0 on next upgrade). The loop is intentional and cheap
    (runs every SessionStart, takes milliseconds).

    Returns number of stale roots replaced, or 0 on failure/no-op.
    """
    if not _is_running_from_plugin_cache():
        return 0
    try:
        # This WRITES the result back. An unknown-state {} here
        # would be json.dumps'd, string-substituted, and written over the user's
        # whole file. Refuse on a bad or missing read.
        settings, _ok = _read_settings_for_write()
        if not _ok or not settings:
            return 0
    except Exception:
        return 0

    settings_text = json.dumps(settings)

    if os.name == "nt":
        # Our current plugin root (e.g., C:\Users\...\plugins\cache\org\token-optimizer\3.1.0)
        current_root = str(Path(__file__).resolve().parent.parent.parent.parent)
        normalized_text = settings_text.replace("\\\\", "/")
        if "/plugins/cache/" not in normalized_text or "token-optimizer" not in normalized_text:
            return 0
        pattern = r'((?:[A-Za-z]:)?/[^"\'\\]+/plugins/cache/[^/]+/token-optimizer/[^/]+)'
        stale_roots = {
            m.group(1) for m in re.finditer(pattern, normalized_text)
            if m.group(1) != current_root.replace("\\", "/")
        }
        new_text = settings_text
        replacement = current_root.replace("\\", "\\\\")
        for stale_root in stale_roots:
            new_text = new_text.replace(stale_root.replace("/", "\\\\"), replacement)
    else:
        if "/plugins/cache/" not in settings_text or "token-optimizer" not in settings_text:
            return 0

        # Our current plugin root (e.g., /home/user/.claude/plugins/cache/org/token-optimizer/3.1.0)
        current_root = str(Path(__file__).resolve().parent.parent.parent.parent)
        # Find all versioned token-optimizer plugin cache paths that differ from ours
        stale_roots = set()
        for m in re.finditer(r'(/[^"\'\\]+/plugins/cache/[^/]+/token-optimizer/[^/]+)', settings_text):
            found_root = m.group(1)
            if found_root != current_root:
                stale_roots.add(found_root)
        new_text = settings_text
        for stale_root in stale_roots:
            new_text = new_text.replace(stale_root, current_root)

    if not stale_roots:
        return 0

    # Replace stale roots directly in the serialized JSON, then parse back.
    # This avoids mutating the original dict (no partial-state on write failure)
    # and covers all keys without key-specific iteration.
    if new_text == settings_text:
        return 0

    try:
        new_settings = json.loads(new_text)
        if not _write_settings_atomic(new_settings):
            return 0
    except Exception:
        return 0

    return len(stale_roots)


def _migrate_statusline_to_stable_path():
    """Migrate an already-installed statusLine from a version-pinned cache path
    to the update-surviving marketplace clone path.

    G3 C-P2-3: the ca654fe stable-path fix only reaches FRESH installs.
    ``setup_quality_bar`` skips the statusLine whenever one is already installed,
    and ``_is_quality_bar_installed`` counts BOTH the old version-pinned cache
    path and the new clone path as "installed" (it only matches ``statusline.js``
    + ``token-optimizer`` in the command). ``_fix_stale_settings_paths`` rewrites
    cache-root -> *current* cache-root, still version-pinned. So the installed
    base that originally hit "status line blank after every update" keeps the
    version-pinned path and keeps hitting the blank-window-on-first-session.

    This lifts them onto the clone path once, at ensure-health. Acts only on
    plugin-cache installs whose OWN statusLine command still references a
    ``/plugins/cache/`` version-pinned ``statusline.js`` and where the stable
    clone path resolves. It does a targeted substring swap (not a wholesale
    rewrite) so any surrounding command wrapper is preserved. Runs AFTER
    ``_fix_stale_settings_paths`` so the cache path is already the current
    version's before it is lifted to the clone.

    Returns True if it rewrote the command, else False.
    """
    if not _is_running_from_plugin_cache():
        return False
    stable = _stable_marketplace_script_path("statusline.js")
    if not stable:
        return False
    try:
        # Writes `new_settings` (a copy of `settings`) back.
        settings, _ok = _read_settings_for_write()
        if not _ok or not settings:
            return False
    except Exception:
        return False
    sl = settings.get("statusLine") or {}
    cmd = sl.get("command", "")
    if not cmd or "statusline.js" not in cmd or "token-optimizer" not in cmd:
        return False
    # Only normalize separators on Windows; backslash is a legal POSIX
    # filename char, so an unconditional replace would change POSIX behavior.
    normalized_cmd = cmd.replace("\\", "/") if os.name == "nt" else cmd
    m = re.search(r'(?:[A-Za-z]:)?/[^"\'\\]+/plugins/cache/[^/]+/token-optimizer/[^"\'\\]*statusline\.js', normalized_cmd)
    if not m:
        return False
    old_path = cmd[m.start():m.end()]
    if old_path == stable:
        return False
    if "\\" in cmd:
        new_cmd = cmd[:m.start()] + stable + cmd[m.end():]
    else:
        new_cmd = cmd.replace(old_path, stable)
    if new_cmd == cmd:
        return False
    new_settings = dict(settings)
    new_sl = dict(sl)
    new_sl["command"] = new_cmd
    new_settings["statusLine"] = new_sl
    try:
        return bool(_write_settings_atomic(new_settings))
    except Exception:
        return False


_STATUSLINE_JS_IN_CMD_RE = re.compile(r"""(['"])([^'"]*statusline\.js)\1|([^\s'"]*statusline\.js)""")


def _statusline_js_in_command(cmd):
    """(start, end, path) of the statusline.js path inside a statusLine
    command (quoted or bare), or None."""
    m = _STATUSLINE_JS_IN_CMD_RE.search(cmd or "")
    if not m:
        return None
    if m.group(2) is not None:
        return m.start(2), m.end(2), m.group(2)
    return m.start(3), m.end(3), m.group(3)


def _statusline_cmd_is_ours(cmd):
    """True when a statusLine command runs Token Optimizer's statusline.js.

    Judged by the folder the script sits in (``token-optimizer/`` or
    ``token-optimizer/scripts/``), not by "token-optimizer" appearing anywhere
    in the command: another tool's statusline.js under a home or project folder
    that happens to contain that name is not ours. A command whose script path
    cannot be parsed falls back to the plain substring test.
    """
    if not isinstance(cmd, str) or "statusline.js" not in cmd or "token-optimizer" not in cmd:
        return False
    found = _statusline_js_in_command(cmd)
    if not found:
        return True
    parts = [p for p in re.split(r"[\\/]", found[2].lower()) if p]
    if len(parts) < 2 or parts[-1] != "statusline.js":
        return False
    if parts[-2] == "token-optimizer":
        return True
    return len(parts) >= 3 and parts[-2] == "scripts" and parts[-3] == "token-optimizer"


def _path_is_under(path, base):
    try:
        Path(path).expanduser().resolve(strict=False).relative_to(Path(base).resolve(strict=False))
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def _heal_dangling_statusline():
    """Re-point OUR statusLine when it runs a statusline.js that is gone or
    that sits in Codex's plugin cache.

    Before 5.13.51 a Codex-loaded plugin hook was detected as Claude Code, and
    its settings heal pointed Claude Code's statusLine at the Codex plugin
    cache (deleted on Codex's next plugin update, so the status line went
    blank). The stale-path heal only rewrites ``/plugins/cache/`` roots and
    only on plugin-cache installs, so a script install, a missing file outside
    a cache, or a still-present Codex copy kept the bad command.

    Acts only in Claude Code (never a hosted VM's lean profile), only on a
    statusLine that is ours (``statusline.js`` + ``token-optimizer`` in the
    command), and only when the referenced file is missing or under
    ``<codex home>/plugins/``. An existing statusline.js from another Token
    Optimizer checkout (a dev clone) is the user's choice and is left alone.
    Only the path inside the command changes; every other key is preserved.
    Returns True if it rewrote the command.
    """
    try:
        if detect_runtime() != "claude" or _remote_lean_profile():
            return False
        settings, _ok = _read_settings_for_write()
        if not _ok or not settings:
            return False
        sl = settings.get("statusLine")
        if not isinstance(sl, dict):
            return False
        cmd = sl.get("command") or ""
        if not _statusline_cmd_is_ours(cmd):
            return False
        found = _statusline_js_in_command(cmd)
        if not found:
            return False
        start, end, old_path = found
        ours = _get_statusline_path()
        if not ours or not Path(ours).is_file():
            return False
        if os.path.normcase(os.path.abspath(old_path)) == os.path.normcase(os.path.abspath(ours)):
            return False
        from runtime_env import codex_home as _codex_home
        in_codex = _path_is_under(old_path, _codex_home() / "plugins")
        if Path(old_path).expanduser().is_file() and not in_codex:
            return False
        new_cmd = cmd[:start] + ours + cmd[end:]
        if cmd[start - 1:start] not in ("'", '"') and any(c.isspace() for c in ours):
            new_cmd = cmd[:start] + "'" + ours + "'" + cmd[end:]
        new_settings = dict(settings)
        new_sl = dict(sl)
        new_sl["command"] = new_cmd
        new_settings["statusLine"] = new_sl
        return bool(_write_settings_atomic(new_settings))
    except Exception:
        return False


def _heal_keepwarm_plist_path():
    """Rewrite an installed keep-warm launchd plist whose measure.py path went
    stale onto the update-surviving marketplace clone path, then reload the agent.

    Companion to the _keepwarm_measure_script_path stable-path preference (which
    only helps FRESH plist writes). An agent installed by an older build carries a
    version-pinned ``/plugins/cache/.../<VERSION>/measure.py`` path that dies on the
    next plugin update; launchd then fires an old (or missing) script every ~5min,
    so the dashboard's savings freeze at whatever the last good tick produced. This
    lifts an existing plist onto the clone path once, at ensure-health, and
    bootout+bootstraps so the corrected path takes effect immediately.

    Acts only on plugin-cache installs whose OWN keep-warm plist exists and whose
    embedded measure.py path differs from the resolved clone path. Editing the path
    of a plist the user already has is a correctness fix, not a (re)install, so it
    never resurrects a declined/uninstalled agent. The plist only lives under
    ~/Library/LaunchAgents on macOS, so on other platforms it simply won't exist
    and this no-ops. Best effort: any failure leaves the plist untouched, False.
    """
    # Per-account scheduler job: never touched from a redirected HOME
    # (a test sandbox or scratch run would re-point the user's live job).
    # Frozen flag only up here: this runs in ensure-health on EVERY
    # SessionStart, and the full guard's job check is a `schtasks /Query` on
    # Windows, where this macOS heal never has a plist to fix. The full guard
    # runs below, once a stale plist exists and needs a rewrite.
    if _daemon_snapshot_sandboxed():
        return False
    if not _is_running_from_plugin_cache():
        return False
    stable = _stable_marketplace_script_path("measure.py")
    if not stable:
        return False
    plist_path = _keepwarm_scheduler_plist_path()
    try:
        if not plist_path.is_file():
            return False
    except OSError:
        return False
    import plistlib
    try:
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
    except Exception:
        return False
    args = data.get("ProgramArguments")
    if not isinstance(args, list):
        return False
    # The script arg is the measure.py entry (not python3 or the subcommand).
    idx = next((i for i, a in enumerate(args)
                if isinstance(a, str) and a.endswith("measure.py")), None)
    if idx is None or args[idx] == stable:
        return False
    # A rewrite is due. Refuse it when the account's job belongs to another
    # install (rewriting and reloading would re-point it at this one).
    if _scheduler_install_refused():
        return False
    new_args = list(args)
    new_args[idx] = stable
    new_data = dict(data)
    new_data["ProgramArguments"] = new_args
    # Atomic replace so a crash mid-write can't leave a truncated plist.
    try:
        fd, tmp = tempfile.mkstemp(prefix=".keepwarm-plist.", suffix=".tmp",
                                   dir=str(plist_path.parent))
        try:
            with os.fdopen(fd, "wb") as f:
                plistlib.dump(new_data, f)
            os.replace(tmp, str(plist_path))
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except Exception:
        return False
    # Reload so launchd fires the corrected path (mirrors the installer's
    # idempotent bootout-then-bootstrap). Best effort; the on-disk fix stands even
    # if the reload can't run (non-macOS, no launchctl, unprivileged).
    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        uid = getuid()
        for argv in (["launchctl", "bootout", f"gui/{uid}", str(plist_path)],
                     ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)]):
            try:
                subprocess.run(argv, capture_output=True, text=True,
                               errors="replace", timeout=10, creationflags=_NO_WINDOW)
            except (OSError, subprocess.TimeoutExpired):
                pass
    return True


# Known TO script names used to identify token-optimizer hooks.
_TO_SCRIPT_NAMES = frozenset({
    "measure.py", "read_cache.py", "bash_hook.py",
    "archive_result.py", "context_intel.py",
})


def _fix_malformed_hook_commands():
    """Detect and remove malformed hook commands from settings.json.

    Targets two patterns that break hooks silently:
    1. Subshell commands ($(...) or backticks) referencing TO scripts —
       written by Claude during interactive sessions, not by our installers.
    2. Double-$HOME paths ($HOME/Users/X/... or ${HOME}/Users/X/...) —
       expand to /Users/X/Users/X/... which doesn't exist.

    Only removes hooks that reference known TO script names, so other
    plugins' hooks are never touched. After removal, calls setup_all_hooks
    for script-install users (plugin users get hooks from hooks.json
    automatically). Resets last_hook_heal_check so the next session's
    24h gate runs the full heal cycle.

    Returns the number of removed hook entries.
    """
    try:
        # Writes `new_settings` (built from `current`) back.
        current, _ok = _read_settings_for_write()
        if not _ok:
            return 0
    except Exception:
        return 0

    current_hooks = current.get("hooks") if current else None
    if not current_hooks or not isinstance(current_hooks, dict):
        return 0

    removed = 0
    new_hooks = {}

    for event, handler_groups in current_hooks.items():
        if not isinstance(handler_groups, list):
            new_hooks[event] = handler_groups
            continue
        new_groups = []
        for group in handler_groups:
            if not isinstance(group, dict):
                new_groups.append(group)
                continue
            original_hook_list = group.get("hooks", [])
            if not isinstance(original_hook_list, list):
                new_groups.append(group)
                continue
            kept_hooks = []
            for h in original_hook_list:
                if not isinstance(h, dict):
                    kept_hooks.append(h)
                    continue
                cmd = h.get("command", "")
                if _is_malformed_to_hook(cmd):
                    removed += 1
                    continue
                kept_hooks.append(h)
            if kept_hooks:
                new_group = dict(group)
                new_group["hooks"] = kept_hooks
                new_groups.append(new_group)
        if new_groups:
            new_hooks[event] = new_groups

    if removed == 0:
        return 0

    new_settings = dict(current)
    new_settings["hooks"] = new_hooks

    try:
        if not _write_settings_atomic(new_settings):
            return 0
    except (PermissionError, OSError):
        return 0

    # Reset 24h throttle so next session re-runs the full hook heal cycle.
    try:
        _write_config_flag("last_hook_heal_check", 0)
    except Exception:
        pass

    # Script-install users need immediate reprovisioning (plugin users
    # get hooks from hooks.json automatically, no gap).
    if not _is_plugin_installed():
        try:
            heal_result = setup_all_hooks(dry_run=False, verbose=False)
            if heal_result.get("error"):
                print(f"  [Token Optimizer] Warning: hook re-provisioning failed after cleanup: {heal_result['error']}", file=sys.stderr)
        except Exception as _e:
            print(f"  [Token Optimizer] Warning: hook re-provisioning failed after cleanup: {_e}", file=sys.stderr)

    return removed


def _is_malformed_to_hook(cmd):
    """Check if a hook command is a malformed token-optimizer entry.

    Returns True if the command references a known TO script AND contains
    either a subshell pattern or a double-$HOME expansion.
    """
    if not cmd:
        return False
    has_to_script = any(
        re.search(r'(?:^|[/\s])' + re.escape(name) + r'(?:$|[\s])', cmd)
        for name in _TO_SCRIPT_NAMES
    )
    if not has_to_script:
        return False
    has_subshell = "$(" in cmd or "`" in cmd
    has_double_home = False
    if platform.system() == "Darwin":
        has_double_home = (
            "$HOME/Users/" in cmd or "$HOME/home/" in cmd
            or "${HOME}/Users/" in cmd or "${HOME}/home/" in cmd
        )
    return has_subshell or has_double_home


# Marker substrings that prove a UserPromptSubmit hook already drives the
# quality-cache update. Two shapes exist in the wild and BOTH must count as
# "installed", or ensure-health appends a duplicate legacy hook every session:
#   * legacy standalone hook  -> `python3 '<mp>' quality-cache --quiet`
#     (pre-5.11.93 script installs; the literal "quality-cache" is present).
#   * consolidated dispatcher (v5.11.93+) ->
#     `... hooks/userpromptsubmit_runner.py ...`, which runs quality-cache
#     *inside* the runner, so "quality-cache" NEVER appears in the command.
# Matching only the first shape made every detection site treat the shipped
# consolidated dispatcher as "hook missing", regressing the consolidation.
_QUALITY_CACHE_HOOK_MARKERS = ("quality-cache", "userpromptsubmit_runner.py")


def _command_drives_quality_cache(command):
    """True if a hook *command string* already runs the quality-cache update.

    Recognizes both the legacy standalone hook and the consolidated
    in-process dispatcher (see `_QUALITY_CACHE_HOOK_MARKERS`).
    """
    blob = str(command or "")
    return any(marker in blob for marker in _QUALITY_CACHE_HOOK_MARKERS)


def _quality_cache_hook_present(groups):
    """True if any UserPromptSubmit hook *group* already drives quality-cache.

    `groups` is a settings.json / hooks.json ``UserPromptSubmit`` list. This is
    the two-marker generalization of the old ``any("quality-cache" in str(h)
    for h in groups)`` scan: it stringifies each group so it stays robust to the
    canonical ``{"hooks": [{"command": ...}]}`` shape and any bare/legacy entry,
    while recognizing the consolidated dispatcher that carries no literal
    "quality-cache" substring.
    """
    return any(_command_drives_quality_cache(str(group)) for group in (groups or []))


def _is_quality_bar_installed(settings=None):
    """Check which quality bar components are installed.

    Returns dict with 'statusline' and 'hook' bools.
    """
    if settings is None:
        settings, _ = _read_settings_json()

    result = {"statusline": False, "hook": False}

    # Check statusline
    sl = (settings.get("statusLine") or {})
    cmd = sl.get("command", "")
    if _statusline_cmd_is_ours(cmd):
        result["statusline"] = True

    # Check UserPromptSubmit hook (settings.json). Recognizes both the legacy
    # standalone hook and the consolidated dispatcher.
    hooks = (settings.get("hooks") or {})
    for group in (hooks.get("UserPromptSubmit") or []):
        for hook in (group.get("hooks") or []):
            if _command_drives_quality_cache(hook.get("command") or ""):
                result["hook"] = True
                break

    # Also check plugin cache hooks (matching _is_smart_compact_installed pattern)
    if not result["hook"]:
        plugin_cache = CLAUDE_DIR / "plugins" / "cache"
        if plugin_cache.exists():
            import glob as globmod
            for hooks_file in globmod.glob(str(plugin_cache / "*" / "token-optimizer" / "*" / "hooks" / "hooks.json")):
                try:
                    with open(hooks_file, "r", encoding="utf-8") as f:
                        plugin_hooks = json.load(f).get("hooks", {})
                    for group in (plugin_hooks.get("UserPromptSubmit") or []):
                        for hook in (group.get("hooks") or []):
                            if _command_drives_quality_cache(hook.get("command") or ""):
                                result["hook"] = True
                                break
                except (json.JSONDecodeError, PermissionError, OSError):
                    continue

    return result


def _set_quality_bar_disabled(disabled):
    """Persist the quality-bar opt-out flag via the atomic+locked config writer.

    Makes `setup-quality-bar --uninstall` sticky across SessionStart auto-
    restore: ensure-health and quality-cache self-heal both already gate on
    this flag. Routed through _write_config_flag so concurrent writers
    (toggle clicks, ensure-health timestamps) never see partial JSON.
    """
    _write_config_flag("quality_bar_disabled", bool(disabled))


def _set_daemon_disabled(disabled):
    """Persist the dashboard-daemon opt-out flag via the atomic+locked writer.

    Makes `setup-daemon --uninstall` sticky across the SessionStart ensure-
    health self-install: `_ensure_dashboard_daemon` gates on this flag so a
    deliberate uninstall is never resurrected. Symmetric with
    `_set_quality_bar_disabled`; an explicit `setup-daemon` install clears it
    (explicit opt-in). Routed through _write_config_flag so it never races the
    ensure-health timestamp / dashboard-toggle writers.
    """
    _write_config_flag("daemon_disabled", bool(disabled))


# ── VS Code status-bar extension auto-install ─────────────────────────────
# The terminal status line never appears for people who live in a VS Code-family
# editor's panel rather than its integrated terminal. The companion extension
# `alexgreensh.token-optimizer-statusline` surfaces the same context/efficiency/
# limits in the editor's own status bar. It's published on the VS Code Marketplace
# (VS Code) and OpenVSX (Cursor / Windsurf / VSCodium), reading the data this
# plugin already writes. ensure-health installs it automatically, once, when it
# detects a VS Code-family editor with a usable CLI — silent, default-on, with a
# sticky opt-out symmetric to the quality bar and dashboard daemon.
_VSCODE_EXTENSION_ID = "alexgreensh.token-optimizer-statusline"


def _set_vscode_ext_disabled(disabled):
    """Persist the VS Code extension auto-install opt-out (sticky across
    SessionStart), symmetric with `_set_quality_bar_disabled` /
    `_set_daemon_disabled`. `setup-vscode-extension --uninstall` sets it; an
    explicit `--install` clears it (opt back in)."""
    _write_config_flag("vscode_ext_disabled", bool(disabled))


def _detect_vscode_family_cli():
    """Return (editor_label, cli_name) when running inside a VS Code-family editor
    with an install-capable CLI on PATH, else (None, None).

    VS Code, Cursor, Windsurf, and VSCodium all set TERM_PROGRAM=vscode (they are
    VS Code forks) and export VSCODE_* markers in the integrated terminal. We pick
    the fork-specific CLI first (its `--install-extension` resolves from the fork's
    own registry — OpenVSX for Cursor/Windsurf), falling back to `code`.
    """
    import shutil  # noqa: PLC0415

    term = os.environ.get("TERM_PROGRAM", "")
    in_vscode = term == "vscode" or any(k.startswith("VSCODE_") for k in os.environ)
    if not in_vscode:
        return None, None
    bundle = os.environ.get("__CFBundleIdentifier", "").lower()
    ipc = os.environ.get("VSCODE_IPC_HOOK", "").lower() + os.environ.get(
        "VSCODE_GIT_ASKPASS_MAIN", ""
    ).lower()
    label, preferred = "VS Code", []
    if "cursor" in bundle or "cursor" in ipc:
        label, preferred = "Cursor", ["cursor"]
    elif "windsurf" in bundle or "exafunction" in bundle or "windsurf" in ipc:
        label, preferred = "Windsurf", ["windsurf"]
    elif "vscodium" in bundle or "codium" in ipc:
        label, preferred = "VSCodium", ["codium"]
    for name in preferred + ["code", "code-insiders", "cursor", "windsurf", "codium"]:
        if shutil.which(name):
            return label, name
    return None, None


def _ensure_vscode_extension():
    """Auto-install the Token Optimizer VS Code status-bar extension (Claude only,
    default-on, sticky opt-out `vscode_ext_disabled`, throttled to once/24h).
    Idempotent: no-op when already installed or no VS Code-family CLI is present.
    Never raises — a hook must never break the session."""
    import time as _time  # noqa: PLC0415

    if _read_config_flag("vscode_ext_disabled", False):
        return
    label, cli = _detect_vscode_family_cli()
    if not cli:
        return
    # Throttle: at most one attempt per 24h. Installing once is enough; the window
    # avoids per-session `--list-extensions` spawns while still re-offering to a
    # user who removed it. Stamp BEFORE the work so a hang never re-fires next tick.
    now = _time.time()
    try:
        if now - float(_read_config_flag("vscode_ext_last_check", 0) or 0) < 86400:
            return
    except (TypeError, ValueError):
        pass
    _write_config_flag("vscode_ext_last_check", now)
    try:
        listed = subprocess.run(
            [cli, "--list-extensions"], capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW
        )
    except (OSError, subprocess.SubprocessError):
        return
    if listed.returncode == 0 and _VSCODE_EXTENSION_ID.lower() in (listed.stdout or "").lower():
        return  # already installed
    try:
        r = subprocess.run(
            [cli, "--install-extension", _VSCODE_EXTENSION_ID],
            capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if r.returncode == 0:
        print(
            f"  [Token Optimizer] Installed the VS Code status-bar extension in {label}. "
            "Reload the window to see it (Cmd/Ctrl+Shift+P -> Reload Window). "
            "Opt out anytime: measure.py setup-vscode-extension --uninstall"
        )


def _read_config_flag(key, default=False):
    """Read a single flag from config.json. Returns default on any error.

    A key the user's config.json does not set falls back to the repo-scoped
    project layer ($CLAUDE_PROJECT_DIR/.claude/token-optimizer.json, allowlisted
    non-secret keys only; see runtime_env.project_config), so a Claude Code
    cloud session whose home is fresh every time still sees the team's
    committed opt-outs. The user's own value always wins."""
    try:
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(cfg, dict) and key in cfg:
                return cfg[key]
    except (json.JSONDecodeError, OSError):
        pass
    try:
        value = project_config_value(key)
        if value is not None:
            return value
    except Exception:
        pass
    return default


_CONFIG_LOCK_PATH = CONFIG_DIR / ".config.lock"


@contextmanager
def _config_lock():
    """Bounded portable lease for config.json writes.

    The v5 toggle endpoint can trigger 5+ concurrent read-modify-write
    cycles on CONFIG_PATH when a user rage-clicks dashboard checkboxes,
    and SessionStart ensure-health also writes last_hook_heal_check.
    Without serialization, interleaved writers silently clobber each other's
    keys. Contenders skip the mutation after 75ms rather than blocking a hook.
    """
    lease_path = _CONFIG_LOCK_PATH.with_suffix(".lease")
    with lease_lock(
        lease_path,
        deadline=current_deadline(),
        acquire_timeout=0.075,
    ) as acquired:
        yield acquired


def _write_config_flag(key, value):
    """Merge a single flag into config.json. Non-fatal on I/O errors.

    v5.4.19: wrapped in _config_lock + tempfile + os.replace
    so concurrent writers (daemon toggles, ensure-health timestamps,
    welcome flags) never race on a non-atomic write. Readers always see
    either the pre-write state or the fully-written state, never partial
    JSON. tmp_path=None sentinel prevents the finally clause from
    unlinking an already-renamed destination.
    """
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    with _config_lock() as acquired:
        if not acquired:
            return
        cfg = {}
        if CONFIG_PATH.exists():
            try:
                loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    cfg = loaded
            except (json.JSONDecodeError, OSError):
                pass
        cfg[key] = value
        tmp_path = None
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(CONFIG_DIR),
                prefix=".config-",
                suffix=".json",
            )
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
                f.write("\n")
            os.replace(tmp_path, str(CONFIG_PATH))
            tmp_path = None
        except OSError:
            pass
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# v5 Active Compression feature flags
# ---------------------------------------------------------------------------
# These are the single source of truth for whether each v5 feature is active.
# Read order: env var (highest priority) > config.json > default.
# This lets power users override via env, but persistent state lives in config.json
# so dashboard toggles work without touching shell profiles.

V5_FEATURES = {
    "quality_nudges": {
        "env_var": "TOKEN_OPTIMIZER_QUALITY_NUDGES",
        "config_key": "v5_quality_nudges",
        "default": True,  # ON by default, extends existing warn behavior
        "label": "Quality Nudges",
        "what": "Warns you when your context quality drops suddenly (e.g., from 85 to 60).",
        "value": "Catches context rot early so you can /compact at the right moment. Prevents silent quality loss that leads to bad decisions.",
        "impact_pct": 5,  # avg prevented waste from better compaction timing
        "how": "Shows a one-line message like '[Token Optimizer] Quality dropped to 58. Consider /compact.' Fires at most 3 times per session with a 5-minute cooldown.",
        "risk": "None. Only adds a short warning to context, never removes anything.",
        "risk_level": "none",
        "recommended": True,
    },
    "context_size_advisory": {
        "env_var": "TOKEN_OPTIMIZER_CONTEXT_ADVISORY",
        "config_key": "v5_context_size_advisory",
        "default": True,
        "label": "Context-Size Advisory",
        "what": "Once a session passes ~350K live-context tokens, gently notes that a checkpoint exists and compaction is an option at a safe pause.",
        "value": "Surfaces the size signal proactively at a measured threshold instead of waiting for quality to degrade. Eligibility, not an instruction: at most once per session.",
        "impact_pct": 0,
        "how": "Checks current host/transcript token telemetry (never a guessed window) against the configured threshold (300-400K, default 350K), requires progress beyond the first prompt, a safe pause between turns, and a valid current checkpoint. Silent below 300K and whenever telemetry is missing or contradictory.",
        "risk": "None. Adds one short advisory at most once per session.",
        "risk_level": "none",
        "recommended": True,
    },
    "loop_detection": {
        "env_var": "TOKEN_OPTIMIZER_LOOP_DETECTION",
        "config_key": "v5_loop_detection",
        "default": True,
        "label": "Loop Detection",
        "what": "Detects when the AI is stuck retrying the same thing and warns you.",
        "value": "Catches loops before they burn through tokens. A single caught loop typically saves 10-50K tokens.",
        "impact_pct": 8,  # avg based on post-hoc detector findings
        "how": "Watches for 3+ similar messages in the last 4 turns, or 3+ identical failing tool calls. Fires at most 2 times per session.",
        "risk": "None. Only adds a short warning to context, never removes anything.",
        "risk_level": "none",
        "recommended": True,
    },
    "delta_mode": {
        "env_var": "TOKEN_OPTIMIZER_READ_CACHE_DELTA",
        "config_key": "v5_delta_mode",
        "default": True,  # v5.1: ON by default - biggest real-world savings
        "label": "Delta Mode (Smart Re-reads)",
        "what": "When the AI re-reads a file after editing it, shows only what changed instead of the whole file.",
        "value": "This is the biggest single win. Typical sessions re-read the same file 2-5 times. Delta mode sends only what changed.",
        "impact_pct": 20,  # based on analysis of real sessions (65%+ re-read rate)
        "how": "Stores file content (up to 50KB per file) in a local cache. On re-read, computes a compact diff. Falls back to full re-read if the diff is too large or the file is too big.",
        "risk": "Low. If the AI needed the full file to understand the change in context, the diff alone might not be enough. This is why we fall back to full re-read on large changes (>1500 chars) and big files (>2000 lines).",
        "risk_level": "low",
        "recommended": True,
    },
    "structure_map_beta": {
        "env_var": "TOKEN_OPTIMIZER_STRUCTURE_MAP",
        "config_key": "v5_structure_map_beta",
        "env_truthy_value": "beta",  # tri-state: only the exact token enables
        "default": False,
        "label": "Structure Map Measurement",
        "what": "Logs compression events when structure map fires, so you can track actual savings via compression-stats.",
        "value": "Structure Map itself is always ON (soft-block mode). This flag enables local measurement logging. A 180K-token file re-read becomes 250 tokens.",
        "impact_pct": 30,
        "how": "Writes one row to your local SQLite when a code file re-read gets replaced with a function/class summary. The substitution runs regardless of this flag. This only controls whether savings events are recorded.",
        "risk": "None. Local SQLite writes only. The actual substitution is controlled by the read-cache mode, not this flag.",
        "risk_level": "none",
        "recommended": True,
    },
    "bash_compress": {
        "env_var": "TOKEN_OPTIMIZER_BASH_COMPRESS",
        "config_key": "v5_bash_compress",
        "default": True,
        "label": "Bash Output Compression",
        "what": "Rewrites 'git status', 'pytest', 'npm install' etc. to return compressed summaries instead of verbose output.",
        "value": "Strips hundreds of lines of test/build/git output down to just the essentials. Best for sessions with lots of CLI commands.",
        "impact_pct": 10,  # benchmark showed 38% on compressible commands, adjusted for session mix
        "how": "A PreToolUse hook intercepts safe read-only commands and routes them through a compression wrapper. Only whitelisted commands (git status/log/diff, pytest, jest, npm install, ls) are touched. Compound commands (anything with &&, ;, |, $()) are never touched.",
        "risk": "Low. Compression is lossy by design: 'git diff' truncates to 30 lines on large diffs, 'pytest' shows pass/fail counts but strips individual passing tests, 'git log' drops merge commit details. For routine checks this is fine. For careful diff review or debugging specific test failures, set TOKEN_OPTIMIZER_BASH_COMPRESS=0 to disable temporarily.",
        "risk_level": "low",
        "recommended": True,
    },
    "first_read_shadow": {
        "env_var": "TOKEN_OPTIMIZER_FIRST_READ_SHADOW",
        "config_key": "v5_first_read_shadow",
        "default": True,
        "label": "First-Read Skeleton (master switch)",
        "what": "Master switch for the first-read skeleton feature. Off disables BOTH the active skeleton serving and the shadow measurement -- first reads pass through untouched.",
        "value": "Controls whether Token Optimizer ever shortens a large file on its FIRST read (not just re-reads). Turn off to opt out of first-read substitution entirely.",
        "impact_pct": 0,  # gate, not an independent saver; savings attributed to first_read_active
        "how": "When on, large first reads of supported CODE files (python/typescript, 16KB-256KB) in validated cohorts may be served as a skeleton; everything else is measured only. When off, the entire first-read path is skipped.",
        "risk": "Low. The full file is always one `expand <key>`, ranged Read, or direct Edit away, and the original is archived before any substitution (fail-open: if archiving fails, the full file is served).",
        "risk_level": "low",
        "recommended": True,
    },
    "first_read_active": {
        "env_var": "TOKEN_OPTIMIZER_FIRST_READ_ACTIVE",
        "config_key": "v5_first_read_active",
        "default": True,
        "label": "First-Read Skeleton (serve, not just measure)",
        "what": "When on, validated CODE cohorts are served a structural skeleton (signatures/imports) on first read instead of the full file. When off, those cohorts are measured only and served full.",
        "value": "A large first-read code file (e.g. a 180KB module) comes back as a compact API skeleton, with the full content one `expand` away. As of v5.11.27 markdown/prose is NOT skeletoned -- only code.",
        "impact_pct": 15,
        "how": "Requires the master switch (above) on. The full original is archived first and recoverable via `expand <key>`, a ranged Read, or a direct Edit. A runtime tripwire auto-demotes a cohort back to measure-only if its live edit-rate exceeds the safety gate.",
        "risk": "Low. Code skeletons preserve structure; the full file is always recoverable. Set TOKEN_OPTIMIZER_FIRST_READ_ACTIVE=0 (or toggle here) to serve full content while keeping measurement.",
        "risk_level": "low",
        "recommended": True,
    },
}


_TRUTHY_ENV_VALUES = ("1", "true", "yes", "on")


# settings.json files Claude Code injects env vars from, highest precedence first.
# A user who "marks" a flag here expects Token Optimizer to honour it -- but the
# process that regenerates the dashboard (often the long-lived daemon) does NOT
# inherit Claude Code's per-session env injection, so os.environ alone misses it.
# Reading these files directly closes that gap.
def _settings_env_paths():
    # Path.cwd() raises FileNotFoundError if the working directory was deleted out
    # from under a long-lived process (plausible for the daemon). Degrade to the
    # global settings only rather than crashing every caller.
    paths = []
    try:
        cwd = Path.cwd()
    except OSError:
        cwd = None
    if cwd is not None:
        paths.append(cwd / ".claude" / "settings.local.json")
        paths.append(cwd / ".claude" / "settings.json")
    paths.append(CLAUDE_DIR / "settings.local.json")
    paths.append(CLAUDE_DIR / "settings.json")
    return tuple(paths)


# Parsed settings files, memoized by mtime. The detection paths read the same
# <=4 files dozens of times per dashboard run; without this each call re-opened
# and re-parsed them. Keying on st_mtime_ns keeps it correct in
# a long-lived daemon: an edited file is re-read, an unchanged one is not.
_settings_file_cache = {}


def _load_settings_file(path):
    """Return the parsed dict for a settings.json-style file, or None if it is
    absent, not a regular file, unreadable, malformed, or not a JSON object.

    is_file() (not exists()) is deliberate: it is False for a directory, broken
    symlink, or FIFO, so a hostile repo cannot plant a named pipe at
    .claude/settings.json and hang the dashboard on open(). UnicodeDecodeError
    (a ValueError, not an OSError) is caught so a non-UTF-8 file degrades to None
    instead of crashing the run.
    """
    try:
        if not path.is_file():
            return None
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    key = str(path)
    cached = _settings_file_cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    parsed = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            parsed = loaded
    except (json.JSONDecodeError, UnicodeDecodeError, PermissionError, OSError):
        parsed = None
    _settings_file_cache[key] = (mtime, parsed)
    return parsed


def _settings_env_value(var_name):
    """Return the value of an env var as configured in any settings.json `env`
    block (project local/shared first, then global), or None if absent.

    Lets feature-enablement detection see flags a user set in settings.json even
    when the generating process did not inherit that env injection.
    """
    for path in _settings_env_paths():
        loaded = _load_settings_file(path)
        if loaded is None:
            continue
        env = loaded.get("env")
        if isinstance(env, dict) and var_name in env:
            val = env[var_name]
            return str(val) if val is not None else None
    return None


def _resolve_feature_env(env_var):
    """Effective env-var value for a feature: real process env wins (it already
    includes settings.json injection when present), else fall back to reading
    settings.json directly. Returns None when the var is set nowhere."""
    env_val = os.environ.get(env_var)
    if env_val is not None:
        return env_val
    return _settings_env_value(env_var)


def _model_routing_handled_elsewhere():
    """True when subagent models are already routed outside settings.json.

    A model-routing plugin sets the model per Agent call, so a pinned main
    model is a choice, not a leak, and "remove the model key" saves nothing
    (issue #275). Matches any enabled plugin with "router" in its name, plus
    an explicit opt-out for routing done some other way.
    """
    if os.environ.get("TOKEN_OPTIMIZER_SKIP_MODEL_PIN_HINT", "").strip().lower() in ("1", "true", "yes"):
        return True
    try:
        plugins = _resolve_settings_value("enabledPlugins", {}) or {}
    except Exception:
        return False
    if not isinstance(plugins, dict):
        return False
    return any(
        enabled and "router" in str(name).split("@", 1)[0].lower()
        for name, enabled in plugins.items()
    )


def _resolve_settings_value(key, default=None):
    """Return a top-level settings.json key (e.g. includeGitInstructions, model)
    resolving project local/shared over global local/shared, first match wins.
    Lets recommendations honour a setting the user marked at the project level,
    not just in global ~/.claude/settings.json."""
    for path in _settings_env_paths():
        loaded = _load_settings_file(path)
        if loaded is None:
            continue
        if key in loaded:
            return loaded[key]
    return default


def _env_val_enables(feature_name, env_val):
    """Back-compat shim: interpret a feature's value via the shared gate.

    Retained for any external/test caller. Resolves tri-state via the registry's
    env_truthy_value and coerces the shared interpreter's "unrecognized" (None)
    to the feature default, preserving the historical bool return. New code
    should call plugin_env.interpret_flag_value directly."""
    feat = V5_FEATURES.get(feature_name) or {}
    r = interpret_flag_value(env_val, env_truthy_value=feat.get("env_truthy_value"))
    return r if r is not None else bool(feat.get("default", False))


def _is_v5_feature_enabled(feature_name):
    """Check if a v5 feature is enabled. Env var wins, then config, then default.

    Value interpretation is delegated to plugin_env.interpret_flag_value so the
    dashboard reader, the read-hook (is_v5_flag_enabled), and the env path all
    share ONE gate and can never disagree. An unrecognized value at
    a source falls through to the next; the registry default is the final
    fallback. The structure_map tri-state still enables only on the exact "beta"
    token (carried as env_truthy_value in the registry)."""
    feat = V5_FEATURES.get(feature_name)
    if not feat:
        return False
    tv = feat.get("env_truthy_value")

    # Env var (process env or settings.json) wins.
    env_val = _resolve_feature_env(feat["env_var"])
    if env_val is not None:
        r = interpret_flag_value(env_val, env_truthy_value=tv)
        if r is not None:
            return r

    # Config.json (user, then plugin-data, via _read_config_flag).
    config_val = _read_config_flag(feat["config_key"], None)
    if config_val is not None:
        r = interpret_flag_value(config_val, env_truthy_value=tv)
        if r is not None:
            return r

    return feat["default"]


def _set_v5_feature(feature_name, enabled):
    """Enable or disable a v5 feature via config.json (persistent, dashboard-friendly)."""
    feat = V5_FEATURES.get(feature_name)
    if not feat:
        return False
    _write_config_flag(feat["config_key"], bool(enabled))
    return True


def _show_v5_welcome():
    """Print the first-run welcome screen for v5 features."""
    print()
    print("  " + "=" * 68)
    print("  Token Optimizer v5: Active Compression")
    print("  " + "=" * 68)
    print()
    print("  Hi! Token Optimizer just got smarter. It can now actively reduce")
    print("  the tokens your sessions use -- not just measure them.")
    print()
    print("  Here's what's new. You can turn any of these on or off anytime")
    print("  from the dashboard (token-dashboard) or with the command:")
    print("    measure.py v5 enable <feature>")
    print("    measure.py v5 disable <feature>")
    print()

    status = _get_v5_feature_status()
    for name, info in status.items():
        mark = "[ON]" if info["enabled"] else "[OFF]"
        rec = " (recommended)" if info["recommended"] else ""
        print(f"  {mark} {info['label']}{rec}")
        print(f"       What it does: {info['what']}")
        print(f"       Risk: {info['risk']}")
        print()

    print("  " + "-" * 68)
    print("  DEFAULTS (what's on right now):")
    print("    - Quality Nudges      ON  (harmless, just warnings)")
    print("    - Loop Detection      ON  (harmless, just warnings)")
    print("    - Delta Mode          ON  (smart re-reads, big savings)")
    print("    - Bash Compression    ON  (lossy, disable: TOKEN_OPTIMIZER_BASH_COMPRESS=0)")
    print("    - Structure Map        ON  (soft-block, measurement: TOKEN_OPTIMIZER_STRUCTURE_MAP=beta)")
    print()
    print("  Want to change these? Three ways:")
    print("    1. Dashboard:  token-dashboard  (visit the Manage tab)")
    print("    2. CLI:        measure.py v5 enable|disable <feature>")
    print("    3. Env var:    TOKEN_OPTIMIZER_<NAME>=0 in your shell")
    print()
    print("  Full docs: measure.py v5 info <feature>")
    print("  All your data stays 100% local on your machine.")
    print()
    print("DATA NOTICE")
    print("Token Optimizer stores the following data locally on your machine:")
    print("  - Session metrics and trends (trends.db)")
    print("  - Checkpoint snapshots with truncated conversation context")
    print("  - Quality score cache")
    print("  - Tool output archives (auto-deleted after 24h)")
    print("  - Per-session file read cache (auto-deleted after 48h)")
    print()
    print("No data is transmitted externally. Zero network calls. Zero telemetry.")
    print("Run 'measure.py consent --show' to check consent status.")
    print("Run 'measure.py purge --confirm' to delete all Token Optimizer data.")
    print()
    _write_config_flag("enterprise_consent_shown", True)


def _get_v5_feature_status():
    """Return status dict for all v5 features (for dashboard/UI)."""
    status = {}
    codex_hooks_text = ""
    if detect_runtime() == "codex":
        try:
            codex_hooks_text = (Path.cwd() / ".codex" / "hooks.json").read_text(encoding="utf-8")
        except OSError:
            codex_hooks_text = ""
    for name, feat in V5_FEATURES.items():
        # Resolve env from the process AND settings.json so the dashboard sees
        # flags a user "marked" even when this process (e.g. the daemon) did not
        # inherit Claude Code's env injection.
        env_val = _resolve_feature_env(feat["env_var"])
        config_val = _read_config_flag(feat["config_key"], None)
        enabled = _is_v5_feature_enabled(name)
        source = "default"
        recommended = feat["recommended"]
        if env_val is not None:
            # The user has taken manual control via an env var. Don't nag them to
            # enable it (and the dashboard toggle can't override an env var anyway),
            # regardless of whether the value enables or disables the feature.
            source = "env"
            recommended = False
        elif config_val is not None:
            source = "config"
        status[name] = {
            "label": feat["label"],
            "what": feat["what"],
            "value": feat.get("value", ""),
            "impact_pct": feat.get("impact_pct", 0),
            "how": feat["how"],
            "risk": feat["risk"],
            "risk_level": feat.get("risk_level", "none"),
            "recommended": recommended,
            "enabled": enabled,
            "source": source,
            "env_var": feat["env_var"],
            "config_key": feat["config_key"],
        }
        if detect_runtime() == "codex":
            if name in {"quality_nudges", "loop_detection"}:
                hook_enabled = "UserPromptSubmit" in codex_hooks_text and "codex_hook_bridge.py" in codex_hooks_text
                status[name]["enabled"] = hook_enabled
                status[name]["source"] = "codex hook" if hook_enabled else "codex opt-in"
                status[name]["managed_by_hooks"] = True
                status[name]["how"] = "Requires the Codex UserPromptSubmit hook. The default balanced Codex install enables it; quiet mode disables live quality nudges."
            elif name == "bash_compress":
                hook_enabled = "PreToolUse" in codex_hooks_text and "bash_hook.py" in codex_hooks_text
                status[name]["enabled"] = False
                status[name]["recommended"] = False
                status[name]["source"] = "codex experimental hook" if hook_enabled else "codex api gap"
                status[name]["value"] = "Codex currently reports Bash usage, but true invisible Bash command rewriting is not supported yet."
                status[name]["how"] = "Codex PreToolUse can block commands, but current Codex support does not apply updatedInput, so invisible Bash compression stays experimental and opt-in."
            elif name in {"delta_mode", "structure_map_beta"}:
                status[name]["enabled"] = False
                status[name]["recommended"] = False
                status[name]["source"] = "codex api gap"
                status[name]["unavailable"] = True
                status[name]["value"] = "Not yet active in Codex. Token Optimizer measures the gap, but safe substitution needs richer Codex hook payloads."
                status[name]["how"] = "Claude can intercept read flows for this feature. Current Codex hooks do not expose Read tool payloads or a safe response-substitution path, so Token Optimizer will not pretend to enable it."
    return status


def _get_v5_savings_recommendation():
    """Calculate total potential savings from disabled v5 features.

    Returns a dict suitable for Overview tab display:
    {
        "total_disabled_impact_pct": 23,
        "disabled_features": [{label, impact_pct, config_key}, ...],
        "enabled_features": [{label}, ...],
        "recommendation": "Turn on 2 more features to save up to 23% more tokens per session",
        "has_savings_available": True,
    }
    """
    status = _get_v5_feature_status()
    disabled = []
    enabled = []
    total_impact = 0

    for name, info in status.items():
        if info["enabled"]:
            enabled.append({"name": name, "label": info["label"]})
        else:
            disabled.append({
                "name": name,
                "label": info["label"],
                "impact_pct": info["impact_pct"],
                "value": info["value"],
                "risk_level": info["risk_level"],
                "recommended": info["recommended"],
                "source": info.get("source", ""),
            })
            # Only count recommended features for the headline savings estimate
            if info["recommended"]:
                total_impact += info["impact_pct"]

    # Also include non-recommended but safe features for a secondary estimate.
    # An env-managed feature is excluded: if the user set it off via env that is a
    # deliberate opt-out, not headroom we should pitch back to them.
    _excluded_sources = {"codex api gap", "codex experimental hook", "env"}
    additional_impact = sum(
        f["impact_pct"] for f in disabled
        if not f["recommended"]
        and f["risk_level"] in ("none", "low")
        and f.get("source") not in _excluded_sources
    )
    aggressive_impact = total_impact + additional_impact + sum(
        f["impact_pct"] for f in disabled
        if not f["recommended"]
        and f["risk_level"] == "moderate"
        and f.get("source") not in _excluded_sources
    )

    has_savings = total_impact > 0 or aggressive_impact > 0

    if detect_runtime() == "codex" and not has_savings:
        recommendation = "Codex-safe active features are enabled. Claude-only read substitution features are shown as API gaps, not promised savings."
    elif total_impact > 0:
        recommendation = f"Turn on {sum(1 for f in disabled if f['recommended'])} more recommended feature(s) to save up to {total_impact}% more tokens per session"
    elif aggressive_impact > 0:
        recommendation = f"All recommended features are on. Enable opt-in features for up to {aggressive_impact}% more savings (with trade-offs)"
    else:
        recommendation = "All v5 features enabled. You're running at maximum token efficiency."

    return {
        "total_disabled_impact_pct": total_impact,
        "aggressive_impact_pct": aggressive_impact,
        "disabled_features": disabled,
        "enabled_features": enabled,
        "recommendation": recommendation,
        "has_savings_available": has_savings,
        "runtime_note": "Codex cannot yet use Claude's delta-read or structure-map substitution hooks." if detect_runtime() == "codex" else "",
    }


def setup_quality_bar(dry_run=False, uninstall=False, status_only=False, force=False, quiet=False,
                      user_initiated=False):
    """Install, uninstall, or check quality bar (status line + cache hook).

    Installs:
      1. UserPromptSubmit hook that updates quality cache every 2 min
      2. StatusLine config pointing to bundled statusline.js

    If user already has a foreign statusLine configured, shows integration
    instructions instead of replacing it — unless force=True, which is used
    by the SessionStart clobber-recovery path (presence of our cache hook
    is strong evidence the user had our full quality bar previously).

    quiet=True suppresses all user-facing output (the foreign-status-line
    advisory and install chatter) while preserving every side-effect. Used by
    the automated self-heal callers (UserPromptSubmit / SessionStart hooks) so
    a silent repair never re-injects the integration advisory into context on
    every turn. The advisory is only shown on an explicit,
    interactive `setup-quality-bar` run.

    user_initiated=True is passed only by the `setup-quality-bar` CLI command
    (the same for setup-hook / setup-smart-compact / compact-instructions):
    an explicit command takes the settings lease past the hook-herd
    reservation, so commands typed back to back are not refused. The
    automatic heal callers keep the default and the non-blocking lease.

    Side-effects on config.json:
      install        -> clears "quality_bar_disabled" (explicit opt-in)
      --uninstall    -> sets   "quality_bar_disabled" (sticky opt-out)
    """
    # Claude Code cloud has no status line, and in a cloud VM TO's hooks are
    # the cloud kit's, which its setup script merges into the VM's
    # ~/.claude/settings.json: a silent self-heal that adds our statusLine +
    # cache hook to that same file would double-register the hook. This holds
    # with the lean profile switched off (TOKEN_OPTIMIZER_REMOTE_LEAN=0) too:
    # turning lean off brings back daemon/keep-warm work, never a second copy
    # of the hooks. Only the automatic (quiet) callers are skipped; an
    # explicit `setup-quality-bar` still runs.
    if quiet and not (dry_run or uninstall or status_only) and (
            _remote_lean_profile() or is_claude_code_cloud() or _cloud_kit_owns_hooks()):
        return
    # This function WRITES `settings` back. The old
    # `_read_settings_json()` collapsed a malformed / unreadable / missing file
    # to `{}`, and the write below then rebuilt settings.json from that `{}` --
    # erasing every key the user owned. Under a plugin install the cache hook is
    # skipped, so the rebuilt file contained ONLY `statusLine`: the exact
    # observed wipe signature. Refuse to act on an unknown state instead.
    # status_only is a pure read, so it keeps the lossy reader.
    if status_only:
        settings, settings_path = _read_settings_json()
    else:
        settings, _ok = _read_settings_for_write(allow_missing=not quiet)
        settings_path = SETTINGS_PATH
        if not _ok:
            if not quiet:
                print(
                    "[Token Optimizer] settings.json is unreadable, malformed, or "
                    "missing right now; skipping quality bar setup rather than "
                    "risk overwriting it.",
                    file=sys.stderr,
                )
            return
    current = _is_quality_bar_installed(settings)
    mp = _get_measure_py_path()
    sl_path = _get_statusline_path()

    if status_only:
        print("\n  Quality Bar Status")
        print(f"  {'=' * 40}")
        print(f"    Status line:  {'installed' if current['statusline'] else 'not installed'}")
        print(f"    Cache hook:   {'installed' if current['hook'] else 'not installed'}")
        if current["statusline"] and current["hook"]:
            print("\n  Quality Bar is fully active.")
        else:
            missing = []
            if not current["statusline"]:
                missing.append("status line")
            if not current["hook"]:
                missing.append("cache hook")
            print(f"\n  Missing: {', '.join(missing)}")
            print(_hint("  Run: python3 measure.py setup-quality-bar"))
        print()
        return

    if uninstall:
        hooks = settings.get("hooks", {})
        removed = 0

        # Remove UserPromptSubmit quality-cache hooks.
        # intent, do NOT broaden: this filter matches ONLY the legacy
        # `quality-cache` command literal on purpose. Unlike _is_our_hook_entry
        # (whose whitelist recognises the userpromptsubmit_runner.py
        # dispatcher), uninstall must NOT strip the dispatcher: it is multi-purpose
        # and deleting it here would take unrelated hooks down with the quality
        # bar. The narrow literal is the correct, deliberate scope for uninstall.
        if "UserPromptSubmit" in hooks:
            new_groups = []
            for group in hooks["UserPromptSubmit"]:
                new_hooks = [
                    h for h in group.get("hooks", [])
                    if "quality-cache" not in h.get("command", "")
                ]
                if new_hooks:
                    group["hooks"] = new_hooks
                    new_groups.append(group)
                else:
                    removed += 1
            if new_groups:
                hooks["UserPromptSubmit"] = new_groups
            else:
                del hooks["UserPromptSubmit"]

        # Remove statusLine if it's ours
        sl = settings.get("statusLine", {})
        if _statusline_cmd_is_ours(sl.get("command", "")):
            del settings["statusLine"]
            removed += 1

        if dry_run:
            print(f"\n  [Dry run] Would remove {removed} quality bar component(s)")
            print("  Run without --dry-run to apply.\n")
            return

        settings["hooks"] = hooks
        # Deliberate top-level removal: declare it so the choke-point guard
        # allows this one and only this one.
        if not _write_settings_atomic(settings, allow_removing_keys={"statusLine"},
                                      user_initiated=user_initiated):
            print("[Error] settings.json write was refused; Quality Bar was not removed.", file=sys.stderr)
            return
        _set_quality_bar_disabled(True)
        print(f"[Token Optimizer] Quality bar removed. {removed} component(s) removed.")
        print("  Opt-out is sticky: SessionStart will not auto-restore.")
        print(_hint("  To re-enable later, run: python3 measure.py setup-quality-bar"))
        return

    # Install
    installed = []
    skipped = []
    warnings = []
    is_plugin = _is_running_from_plugin_cache() or _is_plugin_installed()

    # 1. UserPromptSubmit hook for quality cache
    # Skip when running as a plugin — hooks.json already provides this hook,
    # and writing it to settings.json creates a stale-path risk.
    if is_plugin and current["hook"]:
        skipped.append("cache hook (plugin hooks.json; settings.json entry is redundant)")
    elif is_plugin:
        skipped.append("cache hook (plugin hooks.json)")
    elif current["hook"]:
        skipped.append("cache hook")
    else:
        hooks = settings.setdefault("hooks", {})
        _py, _mp = _settings_hook_python_and_script(mp)
        hook_cmd = f"{_py} {_mp} quality-cache --quiet"
        hook_entry = {"type": "command", "command": hook_cmd}
        hook_group = {"hooks": [hook_entry]}
        hooks.setdefault("UserPromptSubmit", []).append(hook_group)
        installed.append("cache hook")

    # 2. StatusLine
    if current["statusline"]:
        skipped.append("status line")
    else:
        existing_sl = settings.get("statusLine", {})
        if not force and (existing_sl.get("command") or existing_sl.get("url")):
            # User has their own status line - don't replace (unless force=True,
            # used by ensure-health clobber recovery when our cache hook is
            # still present, which is strong evidence the foreign statusLine
            # is a clobber rather than an intentional choice).
            try:
                _rt_seg = RUNTIME_DIR.relative_to(HOME).as_posix()
                _rt_base = f"os.homedir(), '{_rt_seg}'"
            except ValueError:
                # Runtime dir outside HOME (e.g. CLAUDE_CONFIG_DIR on a
                # relocated volume): print its absolute path, never ~/.claude.
                _rt_base = json.dumps(RUNTIME_DIR.as_posix())
            warnings.append(
                "You already have a custom status line configured.\n"
                "  To integrate quality scoring, add this to your status line script:\n\n"
                "    // Read context quality score\n"
                f"    const qFile = path.join({_rt_base}, 'token-optimizer', 'quality-cache.json');\n"
                "    let qScore = '';\n"
                "    if (fs.existsSync(qFile)) {\n"
                "      try {\n"
                "        const q = JSON.parse(fs.readFileSync(qFile, 'utf8'));\n"
                "        const s = q.score;\n"
                "        if (s < 50) qScore = ' | \\x1b[31mContextQ:' + s + '\\x1b[0m';\n"
                "        else if (s < 70) qScore = ' | \\x1b[33mContextQ:' + s + '\\x1b[0m';\n"
                "        else qScore = ' | \\x1b[2mContextQ:' + s + '\\x1b[0m';\n"
                "      } catch (e) {}\n"
                "    }\n"
                "    // Append qScore to your output\n"
            )
            skipped.append("status line (custom detected)")
        else:
            settings["statusLine"] = {
                "type": "command",
                # A plain '...' wrap broke on an apostrophe in the path
                # (C:\Users\O'Brien). Keep the familiar form otherwise.
                "command": (f"node '{sl_path}'" if "'" not in str(sl_path)
                            else f"node {shlex.quote(str(sl_path))}"),
            }
            installed.append("status line")

    if dry_run:
        print("\n  [Dry run] Quality Bar preview")
        print(f"  {'=' * 40}")
        if installed:
            print(f"  Would install: {', '.join(installed)}")
        if skipped:
            print(f"  Already installed / skipped: {', '.join(skipped)}")
        if warnings:
            print()
            for w in warnings:
                print(f"  Note: {w}")
        print("\n  Run without --dry-run to apply.\n")
        return

    if not installed and not warnings:
        # Already fully installed — still make sure the opt-out flag is clear
        # (handles the rare case where a user manually set the flag but also
        # still has the components in place).
        _set_quality_bar_disabled(False)
        if not quiet:
            print("[Token Optimizer] Quality bar already fully installed.")
        return

    if installed:
        if not _write_settings_atomic(settings, user_initiated=user_initiated):
            if not quiet:
                print("[Error] settings.json write was refused; Quality Bar was not installed.", file=sys.stderr)
            return
        # Explicit install is an explicit opt-in — clear any prior opt-out.
        _set_quality_bar_disabled(False)

    if quiet:
        # Automated self-heal context (hooks). All side-effects above are
        # already applied; stay silent so the foreign-status-line advisory and
        # install chatter never bloat per-turn / per-session context.
        return

    if installed:
        print("[Token Optimizer] Quality Bar installed.")
        print(f"  Components: {', '.join(installed)}")
        if skipped:
            print(f"  Already had: {', '.join(skipped)}")
        print("\n  What you'll see:")
        print("    Status line:  model | effort | project ████ 43% | ContextQ:74")
        print("    Quality updates every ~2 minutes during active sessions")
        print("    Colors: green (85%+), dim (70-84%), yellow (50-69%), red (<50%)")
        print(_hint("\n  To remove: python3 measure.py setup-quality-bar --uninstall"))

    if warnings:
        print()
        for w in warnings:
            print(f"  {w}")
        if "cache hook" in installed:
            print("  The cache hook is installed. Quality data will be written to:")
            print(f"    {QUALITY_CACHE_PATH}")


# ========== Savings Dashboard (v3.0) ==========

# Legacy savings categories — stored in the savings_events table and
# maintained across v2 / v3 / v4. Each key corresponds to an event_type
# passed to _log_savings_event().
_LEGACY_SAVINGS_LABELS = {
    "setup_optimization": "Setup optimization",
    # tool_digest removed: phantom label from v3.0.0 — no code ever logged a
    # tool_digest event. The row showed "0 events" forever, making a
    # never-implemented feature look broken. Removed to stop the confusion.
    # Checkpoint restores: ESTIMATED. The restore itself is observed, but the
    # credited working set is inferred from the session's own read history --
    # the avoided re-read never happened, so nothing was metered.
    # _get_savings_summary relocates the event_type to the estimated tier.
    "checkpoint_restore": "Checkpoint restores [est]",
    # resume_lean: cold-resume-lean reopens a forgotten session by reconstructing
    # a lean context from its checkpoint instead of `claude --resume` re-writing
    # the full transcript cold. Counterfactual trigger -> estimated tier
    # (popped from the measured total in _get_savings_summary).
    "resume_lean": "Lean resumes",
    "tool_archive": "Tool replacements",
    "structure_map": "Structure maps",
    # mcp_cap: ESTIMATED (not measured). Claude Code truncates MCP output
    # before PostToolUse fires, so the true pre-cap size is invisible to TO.
    # Logged by archive_result.py when an MCP result is at/near the configured
    # MAX_MCP_OUTPUT_TOKENS cap. Label carries [est] to distinguish from
    # measured categories. Phase 2 will replace with actual measurement.
    "mcp_cap": "MCP output cap [est]",
    # contextignore_block: MEASURED. A .contextignore rule denies the read
    # outright (read_cache.py), so the file's tokens never enter context — a
    # real prevention, priced at the file-size estimate at the block site.
    "contextignore_block": "Ignored-file reads blocked",
    # redundant_block: MEASURED. block-mode hard-denies a redundant reread with no
    # structure-map replacement, so the file's tokens never re-enter context.
    "redundant_block": "Redundant rereads blocked",
    # verbosity_steer: ESTIMATED. The UserPromptSubmit hook injects a conciseness
    # nudge when context is under pressure. Output token reduction is estimated
    # (10-15% of affected response tokens) since we can't measure the
    # counterfactual output the model would have produced without the nudge.
    "verbosity_steer": "Verbosity steering [est]",
    # verbosity_steer_measured: the output reduction actually observed after a
    # conciseness nudge, compared with the session's own pre-nudge replies.
    "verbosity_steer_measured": "Concise output (measured)",
    # tool_archive_refetch_block: a repeat call for an already-archived result
    # was answered from the archive instead of re-running the tool.
    "tool_archive_refetch_block": "Re-fetches answered from archive",
    # keepwarm: cache rewrites avoided by keeping the prompt cache warm.
    "keepwarm": "Cache kept warm",
}

# v5 Active Compression categories — stored in compression_events and
# written by read_cache.py / measure.py v5 paths. Keys must match the
# `feature=` strings passed to _log_compression_event().
_V5_COMPRESSION_LABELS = {
    "delta_read": "Delta reads",
    "quality_nudge": "Quality nudges",
    # v5.0 bash compression handlers
    "bash_compress_git": "Bash compress (git)",
    "bash_compress_pytest": "Bash compress (pytest)",
    "bash_compress_jest": "Bash compress (jest)",
    "bash_compress_npm": "Bash compress (npm)",
    "bash_compress_ls": "Bash compress (ls)",
    # v5.1 bash compression handlers (labels ship ahead of the handlers'
    # own telemetry writers, so the display renders cleanly the moment a
    # handler starts logging events).
    "bash_compress_lint": "Bash compress (lint)",
    "bash_compress_logs": "Bash compress (logs)",
    "bash_compress_tree": "Bash compress (tree)",
    "bash_compress_progress": "Bash compress (progress)",
    "bash_compress_list": "Bash compress (list)",
    "bash_compress_build": "Bash compress (build)",
    "bash_compress_test_exts": "Bash compress (test runners)",
    # v5.2 bash compression handlers
    "bash_compress_search": "Bash compress (search results)",
    "bash_compress_stack": "Bash compress (stack traces)",
    "bash_compress_k8s": "Bash compress (k8s)",
    "bash_compress_cloud": "Bash compress (cloud CLI)",
    # Unmatched-output coverage (tier=measured). U5: first-read skeleton —
    # only its ACTIVE (tier=measured) rows reach the headline; shadow rows are
    # tier=opportunity and filtered out in _get_compression_summary.
    "bash_generic": "Bash compress (other commands)",
    "first_read_skeleton": "First-read code skeleton",
    # Unit B PostToolUse path: pipelines/metachar commands that PreToolUse
    # rejects. bash_compress_hook.py logs these (tier=measured, verified) but
    # the label key was missing, so the whole path was silently excluded from
    # the headline. Adding the key here re-admits it (categories derive from
    # these keys), correcting an UNDER-count.
    "bash_compress_pipeline": "Bash compress (pipelines)",
    # Session-stateful cross-turn dedup: a repeated read-only command collapses
    # to a one-line note or a delta. A per-command tool (no session memory)
    # cannot do this, so it is TO's own bucket, not folded into pipelines.
    "crossturn_dedup": "Cross-turn dedup",
    "bash_compress_json": "Bash compress (JSON)",
    "bash_compress_csv": "Bash compress (CSV)",
    # Build and compiler output (make, cargo, gcc, tsc, ...) trimmed to the
    # errors and the summary line.
    "build_output_compress": "Build output compression",
    # Codex sessions: command output compressed by the Codex hook path.
    "codex_command_compress": "Command output (Codex)",
}

# compression_events features that are bookkeeping, shadow measurement, or a
# counterfactual signal rather than metered savings. Every feature a hook logs
# must be in _V5_COMPRESSION_LABELS or here (enforced by tests), so a new saving
# can never be logged and left out of the totals by accident.
# loop_detection: the loop turns are observed spend, but the logged
# original-compressed delta is the loop's own volume, not tokens removed from
# context -- the warning changes future behavior, it compresses nothing.
# Legacy NULL-tier rows would otherwise read as measured; the avoided-
# continuation value lives only in _estimate_behavioral_savings (estimated).
_NON_SAVINGS_COMPRESSION_FEATURES = frozenset({
    "fresh_session_nudge", "cohort_demoted", "first_read_edit_followup",
    "cache_drop_warning", "agent_result_skeleton", "unknown", "loop_detection",
    "context_advisory_shadow", "context_size_advisory",
})

# Unified label dict used by the savings-report renderer. Legacy entries
# are authoritative when a key ever collides (dedup rule in
# _get_merged_savings).
# Structural savings are computed at report time from baseline delta ×
# context loads since baseline, so they live in their own label dict rather
# than being tied to a specific event_type in savings_events.
_STRUCTURAL_SAVINGS_LABELS = {
    "structural_savings": "Structural (cumulative)",
}

_SAVINGS_CATEGORY_LABELS = {**_LEGACY_SAVINGS_LABELS, **_V5_COMPRESSION_LABELS, **_STRUCTURAL_SAVINGS_LABELS}

# Derived whitelist: the set of feature keys that _get_merged_savings is
# allowed to pull out of compression_events. Deriving this from the label
# dict keeps the two in lockstep — add a key to _V5_COMPRESSION_LABELS and
# it automatically shows up in the merged view.
_V5_COMPRESSION_CATEGORIES = frozenset(_V5_COMPRESSION_LABELS.keys())


_estimate_compression_cost_per_mtok = _measure_compat.bind(_measure_providers['savings']._estimate_compression_cost_per_mtok, __name__, defaults=(None,))


_window_input_rate_per_mtok = _measure_compat.bind(_measure_providers['savings']._window_input_rate_per_mtok, __name__, defaults=(30,))


_resolve_structural_baseline = _measure_compat.bind(_measure_providers['savings']._resolve_structural_baseline, __name__, defaults=(30,))


_runway_routing_lever = _measure_compat.bind(_measure_providers['savings']._runway_routing_lever, __name__, defaults=(30,))


_routing_direction = _measure_compat.bind(_measure_providers['savings']._routing_direction, __name__)


_routing_mix_reprice_ratio = _measure_compat.bind(_measure_providers['savings']._routing_mix_reprice_ratio, __name__, defaults=(None, 30, None,))


_routing_reprice_basis = _measure_compat.bind(_measure_providers['savings']._routing_reprice_basis, __name__)


# Minimum per-window API-credit overage before its dollar line is shown. Below
# this the figure is immaterial (e.g. a freshly reset weekly window worth a few
# cents) and reads as noise, so the window shows only its headroom bars.
_MIN_OVERAGE_USD_SHOWN = 10.0


_RUNWAY_SAVED_SQL = _counted_sql(
    "savings_events",
    "SELECT event_type, COALESCE(SUM(tokens_saved),0) FROM {table} "
    "WHERE timestamp >= ? AND event_type != 'verbosity_steer' GROUP BY event_type")
_RUNWAY_COMPRESSED_TOTAL_SQL = _counted_sql(
    "compression_events",
    "SELECT COALESCE(SUM(original_tokens-compressed_tokens),0) "
    "FROM {table} WHERE timestamp >= ? AND (tier IS NULL OR tier = 'measured')")
_RUNWAY_COMPRESSED_SQL = _counted_sql(
    "compression_events",
    "SELECT feature, COALESCE(SUM(original_tokens-compressed_tokens),0) "
    "FROM {table} WHERE timestamp >= ? "
    "AND (tier IS NULL OR tier = 'measured') GROUP BY feature")


# Events the savings report moves out of its measured total (one-time setup
# trim, capped MCP output, followed hints, cold resumes, checkpoint restores).
# The runway figure is the measured floor, so it leaves them out too.
_RUNWAY_ESTIMATED_EVENT_TYPES = frozenset(
    {"setup_optimization", "mcp_cap", "hint_followed", "resume_lean",
     "checkpoint_restore"})


_runway_saved_tokens = _measure_compat.bind(_measure_providers['savings']._runway_saved_tokens, __name__, defaults=(False,))


runway_snapshot = _measure_compat.bind(_measure_providers['savings'].runway_snapshot, __name__, defaults=(30, None,))


_active_model_cache_rates = _measure_compat.bind(_measure_providers['savings']._active_model_cache_rates, __name__)


_mix_cache_rates = _measure_compat.bind(_measure_providers['savings']._mix_cache_rates, __name__, defaults=(30,))


_OVERHEAD_CACHE_TTL_SECONDS = _int_env("TOKEN_OPTIMIZER_OVERHEAD_CACHE_TTL", 3600)

# In-process cache for measure_components(): a filesystem scan of
# skills/MEMORY/CLAUDE. A single _get_merged_savings call needs it from both
# _current_overhead_tokens and _unused_skill_overhead_tokens; without this it
# would scan twice per SessionEnd regen. Short TTL so a long-lived process
# still picks up config changes.
_COMPONENTS_CACHE = {"ts": 0.0, "data": None}
_COMPONENTS_CACHE_TTL_SECONDS = _int_env("TOKEN_OPTIMIZER_COMPONENTS_CACHE_TTL", 60)


_cached_measure_components = _measure_compat.bind(_measure_providers['savings']._cached_measure_components, __name__)


_current_overhead_tokens = _measure_compat.bind(_measure_providers['savings']._current_overhead_tokens, __name__)


_per_session_prefix_tokens = _measure_compat.bind(_measure_providers['savings']._per_session_prefix_tokens, __name__, defaults=(0,))


_compound_structural = _measure_compat.bind(_measure_providers['savings']._compound_structural, __name__)


_compute_structural_savings = _measure_compat.bind(_measure_providers['savings']._compute_structural_savings, __name__, defaults=(30,))


_unused_skill_overhead_tokens = _measure_compat.bind(_measure_providers['savings']._unused_skill_overhead_tokens, __name__, defaults=(30,))


_compute_structural_potential = _measure_compat.bind(_measure_providers['savings']._compute_structural_potential, __name__, defaults=(30,))


_estimate_uncaptured_runtime = _measure_compat.bind(_measure_providers['savings']._estimate_uncaptured_runtime, __name__, defaults=(30, None, None,))


# How many extra iterations a caught loop would have run if not stopped. Used
# only for the clearly-labelled behavioral estimate; configurable + conservative.
_BEHAVIORAL_PREVENTED_LOOP_ITERATIONS = _int_env("TOKEN_OPTIMIZER_PREVENTED_LOOP_ITERATIONS", 3)
# B5: conservative continuation factor for the loop behavioral estimate. The
# avoided continuation is a counterfactual, so we floor it at one more equivalent
# looping span (1.0x the measured looped volume) rather than guessing iterations.
_BEHAVIORAL_CONTINUATION_FACTOR = _float_env("TOKEN_OPTIMIZER_LOOP_CONTINUATION_FACTOR", 1.0)


_LOOP_TOTALS_SQL = _counted_sql(
    "compression_events",
    "SELECT COUNT(*), COALESCE(SUM(original_tokens - compressed_tokens), 0) "
    "FROM {table} WHERE feature = 'loop_detection' AND timestamp >= ?")
_LOOP_DETAILS_SQL = _counted_sql(
    "compression_events",
    "SELECT detail FROM {table} "
    "WHERE feature = 'loop_detection' AND timestamp >= ? AND detail IS NOT NULL")


_estimate_behavioral_savings = _measure_compat.bind(_measure_providers['savings']._estimate_behavioral_savings, __name__, defaults=(30,))


_CONTAMINATION_COHORT_MIN = _int_env("TOKEN_OPTIMIZER_COHORT_MIN", 3)

# U-D: conservative per-avoided-read token estimate and a per-session cap so the
# avoided-search estimate stays grounded and can never run away.
_AVG_EXPLORATORY_READ_TOKENS = _int_env("TOKEN_OPTIMIZER_AVG_READ_TOKENS", 2500)
_RETRIEVAL_AVOIDED_READS_CAP = _int_env("TOKEN_OPTIMIZER_AVOIDED_READS_CAP", 8)


_NUDGE_FIRED_SQL = _counted_sql(
    "compression_events",
    "SELECT DISTINCT session_id FROM {table} "
    "WHERE feature='quality_nudge' AND timestamp >= ? AND session_id IS NOT NULL")
_NUDGE_HEEDED_SQL = _counted_sql(
    "compression_events",
    "SELECT DISTINCT session_id FROM {table} "
    "WHERE feature='quality_nudge' AND verified=1 AND timestamp >= ? AND session_id IS NOT NULL")


_estimate_contamination_exit_savings = _measure_compat.bind(_measure_providers['savings']._estimate_contamination_exit_savings, __name__, defaults=(30,))


_CHECKPOINT_RESTORED_SQL = _counted_sql(
    "savings_events",
    "SELECT DISTINCT session_id FROM {table} "
    "WHERE event_type='checkpoint_restore' AND timestamp >= ? AND session_id IS NOT NULL")


_estimate_handover_rerun_savings = _measure_compat.bind(_measure_providers['savings']._estimate_handover_rerun_savings, __name__, defaults=(30,))


_RETRIEVAL_SERVED_SQL = _counted_sql(
    "savings_events",
    "SELECT DISTINCT session_uuid, session_id FROM {table} "
    "WHERE event_type IN ('checkpoint_restore','tool_archive') "
    "AND timestamp >= ?")


_estimate_retrieval_serve_savings = _measure_compat.bind(_measure_providers['savings']._estimate_retrieval_serve_savings, __name__, defaults=(30,))


_model_mix_shares = _measure_compat.bind(_measure_providers['pricing']._model_mix_shares, __name__, defaults=(14, None,))


_default_model_for_runtime = _measure_compat.bind(_measure_providers['pricing']._default_model_for_runtime, __name__)


_friendly_model = _measure_compat.bind(_measure_providers['pricing']._friendly_model, __name__)


_mix_label = _measure_compat.bind(_measure_providers['pricing']._mix_label, __name__)


_model_rate_per_mtok = _measure_compat.bind(_measure_providers['pricing']._model_rate_per_mtok, __name__)


_model_output_rate_per_mtok = _measure_compat.bind(_measure_providers['pricing']._model_output_rate_per_mtok, __name__)


_session_output_fraction = _measure_compat.bind(_measure_providers['pricing']._session_output_fraction, __name__, defaults=(30,))


# Token classes routing is priced by. cache_read is billed at the cache-read rate,
# cache_create at the 5-minute or 1-hour write rate, and "unsplit" holds tokens
# from rows that never stored a class split (excluded from comparison).
_ROUTING_CLASSES = ("fresh_input", "cache_create_5m", "cache_create_1h",
                    "cache_read", "output", "unsplit")


_ROUTING_BILLABLE_CLASSES = ("fresh_input", "cache_create_5m", "cache_create_1h",
                             "output", "unsplit")



_foreign_savings_card_exact = _measure_compat.bind(_measure_providers['pricing']._foreign_savings_card_exact, __name__)


_routing_card_exact = _measure_compat.bind(_measure_providers['pricing']._routing_card_exact, __name__)


_routing_model_status = _measure_compat.bind(_measure_providers['pricing']._routing_model_status, __name__)


_routing_class_rates = _measure_compat.bind(_measure_providers['pricing']._routing_class_rates, __name__)


_GENERATION_KEY_RE = re.compile(r"^(opus|sonnet|haiku|fable)_(\d+)(?:_(\d+))?$")


_newest_generation_model = _measure_compat.bind(_measure_providers['pricing']._newest_generation_model, __name__)


_routing_target_model = _measure_compat.bind(_measure_providers['pricing']._routing_target_model, __name__)


_routing_reference_model = _measure_compat.bind(_measure_providers['pricing']._routing_reference_model, __name__)


_bucket_int = _measure_compat.bind(_measure_providers['pricing']._bucket_int, __name__)


_add_routing_buckets = _measure_compat.bind(_measure_providers['pricing']._add_routing_buckets, __name__)


_routing_pending_sessions = _measure_compat.bind(_measure_providers['pricing']._routing_pending_sessions, __name__, defaults=(30, None,))


_routing_usage_by_model = _measure_compat.bind(_measure_providers['pricing']._routing_usage_by_model, __name__, defaults=(30, None,))


_earliest_model_mix = _measure_compat.bind(_measure_providers['pricing']._earliest_model_mix, __name__, defaults=(14,))


# Share of remaining Opus tokens assumed routable to a cheaper model (grunt
# work: data gathering, search, simple edits). Conservative + configurable.
_ROUTABLE_OPUS_FRACTION = _float_env("TOKEN_OPTIMIZER_ROUTABLE_OPUS_FRACTION", 0.2)


# Explicit pre-Token-Optimizer Opus share, when the user knows their baseline
# (history may not reach back far enough). 0.0 = unset (fall back to snapshot/history).
_BASELINE_OPUS_SHARE = _float_env("TOKEN_OPTIMIZER_BASELINE_OPUS_SHARE", 0.0)


_pretool_baseline_mix = _measure_compat.bind(_measure_providers['pricing']._pretool_baseline_mix, __name__)


set_pretool_baseline = _measure_compat.bind(_measure_providers['pricing'].set_pretool_baseline, __name__)


_compute_model_routing_savings = _measure_compat.bind(_measure_providers['pricing']._compute_model_routing_savings, __name__, defaults=(30, None,))


# Share of full-file Writes assumed avoidable via a targeted Edit, and the
# output tokens a typical avoidable rewrite emits beyond an Edit. Conservative.
_AVOIDABLE_WRITE_FRACTION = _float_env("TOKEN_OPTIMIZER_AVOIDABLE_WRITE_FRACTION", 0.3)
_AVOIDABLE_WRITE_OUTPUT_TOKENS = _int_env("TOKEN_OPTIMIZER_AVOIDABLE_WRITE_TOKENS", 1500)


_sum_tool_calls = _measure_compat.bind(_measure_providers['savings']._sum_tool_calls, __name__, defaults=(30,))


_estimate_output_waste = _measure_compat.bind(_measure_providers['savings']._estimate_output_waste, __name__, defaults=(30,))


# A call gap beyond this many seconds likely dropped the prefix from cache,
# forcing a full re-write on the next turn. Default 5m (the 5m cache TTL).
_CACHE_DROP_GAP_SECONDS = _int_env("TOKEN_OPTIMIZER_CACHE_DROP_GAP_SECONDS", 300)


_estimate_cache_drop_savings = _measure_compat.bind(_measure_providers['savings']._estimate_cache_drop_savings, __name__, defaults=(30,))


# --- Robust per-user baseline (the stable "before" anchor) ---------------------
# The transformation headline compares a typical pre-Token-Optimizer session to a
# typical session now. That comparison is only honest if "before" is a STABLE,
# robust anchor, captured once. Re-deriving it from the earliest-N cohort every run
# is fragile: a few onboarding-era marathon sessions (a 269M-token install spike)
# dominate the mean and inflate the number. So we compute the baseline ONCE over a
# stable early window, with the onboarding tail trimmed, and freeze it to disk.
# Bump when the estimator changes so frozen baselines from an older method are
# recomputed (and re-frozen) instead of silently reused. v2: winsorized mean over a
# 30-day window (was a 14-day trimmed mean, which under-counted real heavy sessions).
# v3: fixed the winsorization cap index. v2 used `int(n*cap_pct)-1`, which on a small
# window (n<100) collapsed to "cap the single largest session" -- far more aggressive
# than the intended top 1% -- so the baseline (n>=30) and the recent window (different
# n) were capped at different effective percentiles, biasing the comparison upward. v3
# uses a true nearest-rank percentile that applies the SAME tail fraction at every
# sample size, so small windows are no longer over-capped and the two sides stay fair.
# v4: store the full model-mix shares (not just opus_share) so the before/after
# transformation prices each era at its real model mix -- works for Codex/GPT and any
# provider, not just Anthropic opus-vs-sonnet.
_BASELINE_VERSION = 4
_BASELINE_ONBOARDING_DAYS = _int_env("TOKEN_OPTIMIZER_BASELINE_ONBOARDING_DAYS", 1)
_BASELINE_EARLY_WINDOW_DAYS = _int_env("TOKEN_OPTIMIZER_BASELINE_WINDOW_DAYS", 30)
# Percentile at which a single session's total tokens is capped (winsorized). 0.99
# caps only the top 1% of sessions (one-off bulk ops), keeping all real heavy work.
# Below ~1/(1-cap_pct) sessions the top 1% rounds to nothing, so small windows fall
# back to a plain mean rather than over-capping (see _winsorized_mean_session).
_BASELINE_WINSOR_PCT = _float_env("TOKEN_OPTIMIZER_BASELINE_WINSOR_PCT", 0.99)
_BASELINE_MIN_STABLE_SESSIONS = _int_env("TOKEN_OPTIMIZER_BASELINE_MIN_SESSIONS", 30)
# Minimum recent sessions before the after-window is trusted for the comparison.
# Matched to a sane floor so the recent-side estimate is not built on a handful of
# sessions (a thin after-window otherwise swings the headline session to session).
_AFTER_MIN_SESSIONS = _int_env("TOKEN_OPTIMIZER_AFTER_MIN_SESSIONS", 10)
# Quality gates for the human-session pool. Sessions below these thresholds
# are not real interactive work — they are empty transcripts, headless API
# calls, or transient sessions that inflate count and skew per-session costs.
# Applied as additional WHERE filters alongside is_sidechain = 0.
_MIN_INPUT_TOKENS = _int_env("TOKEN_OPTIMIZER_MIN_INPUT_TOKENS", 1000)
_MIN_DURATION_MINUTES = _float_env("TOKEN_OPTIMIZER_MIN_DURATION_MINUTES", 1.0)
# Shared SQL fragment to exclude empty/transient sessions that inflate count
# and skew per-session costs. After the 2026-08-29 quality-filter audit this is
# used ONLY at two sites: _short_session_pool_savings (via the NEGATED form
# below) and _estimate_before_after_savings (on recent_rows, for the
# sessions_per_month count that multiplies the frozen anchor). It was removed
# from 6 other sites where it silently excluded 85% of August sessions and
# distorted the baseline anchor, model mix, cache-hit rate, and per-session
# estimates. Used alongside COALESCE(is_sidechain, 0) = 0.
_SESSION_QUALITY_FILTER = (
    "AND COALESCE(input_tokens, 0) >= {} "
    "AND COALESCE(duration_minutes, 0) >= {} "
).format(_MIN_INPUT_TOKENS, _MIN_DURATION_MINUTES)
# Exact complement of _SESSION_QUALITY_FILTER, built from the same constants so
# the two predicates can never drift. Selects the below-threshold sessions whose
# real billed tokens are priced as their own transformation pool (they stay out
# of the session COUNT, which multiplies the frozen typical-session anchor).
_SESSION_QUALITY_FILTER_NEGATED = (
    "AND NOT (COALESCE(input_tokens, 0) >= {} "
    "AND COALESCE(duration_minutes, 0) >= {}) "
).format(_MIN_INPUT_TOKENS, _MIN_DURATION_MINUTES)
# Depth of the ONE-TIME initial backfill (first flush after install/upgrade). A
# long-time user has months of transcripts still on disk; grab them all so the baseline
# freezes from their real earliest sessions immediately rather than waiting for a fresh
# 30-day window. Bounded so the one-off parse stays sane. Rolling collections use 90d.
_INITIAL_BACKFILL_DAYS = _int_env("TOKEN_OPTIMIZER_INITIAL_BACKFILL_DAYS", 365)
# Commit collected sessions in batches so an interrupted backfill banks progress instead
# of discarding the whole transaction. Without this, a deep first-run backfill that exceeds
# the SessionEnd hook's _HookTimeout would commit NOTHING and never write its success
# marker, re-parsing from scratch every flush in a permanent loop.
_COLLECT_COMMIT_BATCH = _int_env("TOKEN_OPTIMIZER_COLLECT_COMMIT_BATCH", 200)
# Per-run cap on NEWLY-parsed sessions. A large uncollected backlog (e.g. after an
# upgrade, or on a machine with thousands of transcripts) would otherwise re-parse
# every uncollected file on a single flush and block turn-end for a minute+. Files
# are processed newest-first, so recent sessions are always collected and the old
# backlog drains a bounded chunk per flush instead of storming. 0 = unbounded.
_COLLECT_MAX_PER_RUN = _int_env("TOKEN_OPTIMIZER_COLLECT_MAX_PER_RUN", 150)
# Hard cap on deep-backfill attempts. collect_sessions raises _HookTimeout (a BaseException,
# NOT caught by `except Exception`) when the 365-day parse exceeds the hook budget, so the
# success marker would never be written and the deep glob would re-fire on every SessionEnd.
# The batch-commit above means each attempt banks real progress, so a few attempts collect a
# large history incrementally; this cap then guarantees termination (after N tries we stop the
# deep glob and let the rolling 90-day window keep things current). Belt and suspenders.
_INITIAL_BACKFILL_MAX_ATTEMPTS = _int_env("TOKEN_OPTIMIZER_INITIAL_BACKFILL_MAX_ATTEMPTS", 5)


_session_token_vector = _measure_compat.bind(_measure_providers['savings']._session_token_vector, __name__)


_DASHBOARD_SPENT_TOKEN_BASIS = (
    "deduplicated fresh_input + output by activity day (top-level)"
)
_DASHBOARD_SPENT_COMBINED_BASIS = (
    "deduplicated fresh_input + output by activity day "
    "(parent+delegated combined; top-level provenance incomplete)"
)


_spent_daily_usage = _measure_compat.bind(_measure_providers['savings']._spent_daily_usage, __name__)


_dashboard_spent_token_basis = _measure_compat.bind(_measure_providers['savings']._dashboard_spent_token_basis, __name__, defaults=(30,))


_winsorized_mean_session = _measure_compat.bind(_measure_providers['savings']._winsorized_mean_session, __name__, defaults=(_BASELINE_WINSOR_PCT,))


_compute_baseline_state = _measure_compat.bind(_measure_providers['savings']._compute_baseline_state, __name__)


_BASELINE_KNOWN_VERSIONS = (3, 4)   # shapes this build can read or migrate forward
_BASELINE_MAX_BYTES = 256 * 1024    # a baseline is ~1KB; anything larger is not one
# Shrink guard: a recapture this much cheaper than the incumbent is refused.
# Rationale: a baseline that shrinks almost always means the HISTORY thinned
# (retention, pruning, a moved data dir, a changed session population), not that
# the user genuinely got cheaper. Deterministic recaptures move 0%, so these
# thresholds cannot false-fire on an honest re-run.
_BASELINE_SHRINK_COST_PCT = 0.15
_BASELINE_SHRINK_CACHE_READ_PCT = 0.25


_write_baseline_state = _measure_compat.bind(_measure_providers['savings']._write_baseline_state, __name__)


_baseline_is_well_formed = _measure_compat.bind(_measure_providers['savings']._baseline_is_well_formed, __name__)


_baseline_priced_cost = _measure_compat.bind(_measure_providers['savings']._baseline_priced_cost, __name__)


_migrate_baseline = _measure_compat.bind(_measure_providers['savings']._migrate_baseline, __name__)


_baseline_adoption_candidates = _measure_compat.bind(_measure_providers['savings']._baseline_adoption_candidates, __name__)


_adopt_orphaned_baseline = _measure_compat.bind(_measure_providers['savings']._adopt_orphaned_baseline, __name__)


_baseline_shrink_is_material = _measure_compat.bind(_measure_providers['savings']._baseline_shrink_is_material, __name__)


_get_baseline_state = _measure_compat.bind(_measure_providers['savings']._get_baseline_state, __name__, defaults=(True,))


_cost_per_session = _measure_compat.bind(_measure_providers['savings']._cost_per_session, __name__)


_mix_from_session_rows = _measure_compat.bind(_measure_providers['savings']._mix_from_session_rows, __name__)

# Module-level memo for the subagent pool (dashboard perf): the transcript scan
# is bounded and the result is cached for _SUBAGENT_POOL_TTL seconds, re-keyed on
# the requested window. _parse_session_jsonl already does requestId-MAX streaming
# dedup, so a cold scan is the only cost we are amortizing.
_subagent_pool_memo = {"key": None, "ts": 0.0, "payload": None}
_SUBAGENT_POOL_TTL = 900.0  # 15 min, matches the cache-health sidecar cadence
# Bump whenever sidechain classification changes. Old sidecars can contain a
# signed negative pool from the earlier marker scan and must never feed a new
# full-dashboard net calculation.
_SUBAGENT_POOL_CACHE_SCHEMA = "sidechain-classifier-v2"
# Long windows (the since-install pool, days >> 30) change slowly and cost the
# most to recompute; give them a longer sidecar life so a dashboard regen never
# pays the full-history scan more than a few times a day.
_SUBAGENT_POOL_TTL_LONG = 21600.0  # 6h
_SUBAGENT_SCAN_MAX_FILES = 4000  # bound the scan so the dashboard never stalls


_subagent_pool_ttl = _measure_compat.bind(_measure_providers['savings']._subagent_pool_ttl, __name__)

# Cross-process L2 cache for the subagent-pool scan. The module memo above is
# per-process only, but every deferred SessionEnd/Stop flush spawns a NEW python
# process (_defer_session_end_flush), so without a persistent sidecar each flush
# cold-scans up to _SUBAGENT_SCAN_MAX_FILES transcripts (~13s). This sidecar makes
# a fresh worker read the 15-min-old result instead of rescanning -- the exact
# idiom cache_health.json uses (_cache_ttl_waste_cached). (mtime, payload) memo
# gives a process that reads twice a single stat on the second call.
_subagent_pool_sidecar_memo = None  # type: ignore[var-annotated]


_subagent_pool_key_str = _measure_compat.bind(_measure_providers['savings']._subagent_pool_key_str, __name__)


_read_subagent_pool_sidecar = _measure_compat.bind(_measure_providers['savings']._read_subagent_pool_sidecar, __name__, defaults=(_SUBAGENT_POOL_TTL,))


_write_subagent_pool_sidecar = _measure_compat.bind(_measure_providers['savings']._write_subagent_pool_sidecar, __name__)


_opus_baseline_shares = _measure_compat.bind(_measure_providers['savings']._opus_baseline_shares, __name__)


_opus_floor_consented = _measure_compat.bind(_measure_providers['savings']._opus_floor_consented, __name__)


_subagent_model_cost = _measure_compat.bind(_measure_providers['savings']._subagent_model_cost, __name__)


_subagent_pool_savings = _measure_compat.bind(_measure_providers['savings']._subagent_pool_savings, __name__, defaults=(30, None, False, True,))


_short_session_pool_savings = _measure_compat.bind(_measure_providers['savings']._short_session_pool_savings, __name__, defaults=(None, True,))


_baseline_progress = _measure_compat.bind(_measure_providers['savings']._baseline_progress, __name__)


_SESSION_WEIGHT_MIN_ANCHOR_SESSIONS = _int_env(
    "TOKEN_OPTIMIZER_WEIGHT_MIN_ANCHOR_SESSIONS", 100)


# Constant measuring-stick rate card ($/MTok: fresh input / output / cache-read /
# cache-write) for the session-weight pool's HEADLINE arm. Both eras are priced at
# these SAME rates so model-mix and vendor price drift (Sonnet 4.6 -> Sonnet 5
# $3->$2-class generational moves, Fable 5 entering the mix) cannot leak into the
# transformation claim -- only billed-volume behaviour (tokens per unit of work and
# the fresh/cache class split) moves it. On this ledger that drift is ~$1.2K/mo of
# the engine-priced per-call delta (37%); claiming it as "your transformation" would
# be crediting Token Optimizer for Anthropic's price cuts. The engine-priced variant
# is still computed and disclosed separately. Cache-write is deliberately priced at
# the flat 5m rate for ALL writes so TTL-mix drift between eras cannot count either.
_WEIGHT_POOL_FLAT_RATES = {"input": 5.0, "output": 25.0, "cache_read": 0.50,
                           "cache_write": 6.25}


_price_parent_window = _measure_compat.bind(_measure_providers['savings']._price_parent_window, __name__)


_price_parent_rows = _measure_compat.bind(_measure_providers['savings']._price_parent_rows, __name__, defaults=(0,))


_PRETOOL_ANCHOR_FILE = "workload_anchor_pretool.json"


_pinned_workload_anchor = _measure_compat.bind(_measure_providers['savings']._pinned_workload_anchor, __name__)


_pretool_window = _measure_compat.bind(_measure_providers['savings']._pretool_window, __name__)


_pretool_anchor_step = _measure_compat.bind(_measure_providers['savings']._pretool_anchor_step, __name__, defaults=(5.0, None,))


_pretool_session_row = _measure_compat.bind(_measure_providers['savings']._pretool_session_row, __name__)


_stable_workload_anchor = _measure_compat.bind(_measure_providers['savings']._stable_workload_anchor, __name__)


_TRANSFORMATION_FLAT_BAND = 0.05


_crosscheck_flat = _measure_compat.bind(_measure_providers['savings']._crosscheck_flat, __name__)


_crosscheck_agrees = _measure_compat.bind(_measure_providers['savings']._crosscheck_agrees, __name__)


_signed_usd = _measure_compat.bind(_measure_providers['savings']._signed_usd, __name__)


_crosscheck_verdict = _measure_compat.bind(_measure_providers['savings']._crosscheck_verdict, __name__)


_session_weight_pool_savings = _measure_compat.bind(_measure_providers['savings']._session_weight_pool_savings, __name__, defaults=(30, None, None,))


_price_parent_activity_window = _measure_compat.bind(_measure_providers['savings']._price_parent_activity_window, __name__)


_weekly_full_savings = _measure_compat.bind(_measure_providers['savings']._weekly_full_savings, __name__, defaults=(None, None,))


_positive_full_value = _measure_compat.bind(_measure_providers['savings']._positive_full_value, __name__)


_weight_pool_payload = _measure_compat.bind(_measure_providers['savings']._weight_pool_payload, __name__)


_estimate_before_after_savings = _measure_compat.bind(_measure_providers['savings']._estimate_before_after_savings, __name__, defaults=(30, None, None, None,))


_estimate_stale_reads_reclaimable = _measure_compat.bind(_measure_providers['savings']._estimate_stale_reads_reclaimable, __name__, defaults=(30,))


_progressive_disclosure_summary = _measure_compat.bind(_measure_providers['savings']._progressive_disclosure_summary, __name__, defaults=(30,))


_logged_oneshot_pricing = _measure_compat.bind(_measure_providers['savings']._logged_oneshot_pricing, __name__)


_apply_counted_oneshot_pricing = _measure_compat.bind(_measure_providers['savings']._apply_counted_oneshot_pricing, __name__)


_merge_v5_compression = _measure_compat.bind(_measure_providers['savings']._merge_v5_compression, __name__)


_allocate_cents = _measure_compat.bind(_measure_providers['savings']._allocate_cents, __name__, defaults=(None,))


_format_cents = _measure_compat.bind(_measure_providers['savings']._format_cents, __name__)


_action_savings_display = _measure_compat.bind(_measure_providers['savings']._action_savings_display, __name__, defaults=(False,))


_action_savings_components = _measure_compat.bind(_measure_providers['savings']._action_savings_components, __name__, defaults=(None,))


_get_merged_savings = _measure_compat.bind(_measure_providers['savings']._get_merged_savings, __name__, defaults=(30, None,))


# ---------------------------------------------------------------------------
# status-bar: the desktop band's single read.
#
# One spawn answers everything the band needs from Token Optimizer's stores.
# Savings are the slow part (~0.5 s warm, ~10 s cold on a large trends.db), so
# they are served from a per-session JSON cache under SNAPSHOT_DIR/status-bar/
# and refreshed by ONE detached child when the cache is older than 60 s. The
# transcript and quality-cache fields are cheap and always read live.
# ---------------------------------------------------------------------------

_STATUS_BAR_SCHEMA = 1
_STATUS_BAR_DAYS = 30
_STATUS_BAR_CACHE_MAX_AGE_S = 60
_STATUS_BAR_LOCK_STALE_S = 120
# The detached refresh child reads its lock's owner token from this variable.
_STATUS_BAR_LOCK_TOKEN_ENV = "TO_STATUS_BAR_LOCK_TOKEN"
_STATUS_BAR_CACHE_RETENTION_S = 30 * 86400
_STATUS_BAR_CHILD_BUDGET_S = 90
_STATUS_BAR_TAIL_CHUNK = 1 << 20
_STATUS_BAR_TAIL_MAX_BYTES = 16 << 20

STATUS_BAR_HELP = """\
measure.py status-bar --session <id> --json [--transcript PATH]

One JSON read for the desktop status band. Always exits 0 with a JSON object;
a missing or busy trends.db, or a missing transcript, gives nulls, not errors.

Fields:
  schema                 1
  session_id             the sanitized session id
  savings                null, or the object below
    unit                 "tokens": the band shows tokens saved
    session_tokens       this session's realized (metered) tokens saved
    session_usd          the same in USD
    daily                30 entries, oldest first, one per LOCAL day ending
                         today: {"date": "YYYY-MM-DD", "tokens": int, "usd": float};
                         zeros on days with no savings (metered only)
    total_30d_usd        exactly _get_merged_savings(days=30)["total_cost_usd"],
                         the realized metered figure (rolling 30 x 24 h)
    total_30d_tokens     the dashboard's Tokens Saved headline for the same
                         window: measured + estimated (_dashboard_saved_tokens);
                         0 when nothing was measured, as the dashboard hides it
    total_30d_measured_tokens  _get_merged_savings(days=30)["total_tokens"], the floor
    computed_at          epoch seconds the savings were computed
    The session figure and the daily entries are savings_events + realized
    compression_events rows, netted by the same helpers as the headline
    (model-mix reprice, estimated-tier relocations, tool_archive re-expand
    netting, v5 dedup). The 30-day headline also carries structural and
    estimated savings, which cannot be split by session or day, so the daily
    entries need not add up to it. The band shows total_30d_tokens.
  savings_state          "fresh" (cache <= 60 s old), "stale" (served from an
                         older cache while a refresh runs), "loading" (no cache
                         yet, refresh started), or "unavailable"
  savings_age_s          age of the served savings in seconds, or null
  savings_reason         one short reason when savings is null, else null
  refresh_started        true when this call started the one detached refresh
  last_request_epoch     epoch seconds of the last MAIN-thread assistant
                         request in the transcript (subagent rows ignored)
  cache_lifetime         "1h" | "5m" | null: the last measured cache-write
                         lifetime on the main thread (null = unmeasured).
                         Prefers the native prompt_cache object that
                         statusline.js bridges on Claude Code v2.1.251+; falls
                         back to transcript usage rows on older hosts
  cache_expires_at       epoch seconds the prompt cache expires, from the
                         bridged native prompt_cache object, else null
  cache_warm             native prompt_cache.warm when bridged, else null
  cache_source           "prompt_cache" | "transcript" | null: which source
                         cache_lifetime came from
  compactWindow          {"tokens": int|null, "source": str}: the resolver's
                         compact window for this session's model
                         (CLAUDE_CODE_AUTO_COMPACT_WINDOW > /autocompact
                         modelSettings > autoCompactWindow > model default).
                         null tokens = no override known, use what the host
                         reports
  last_checkpoint_epoch  this session's newest checkpoint (quality cache or
                         checkpoint file) across Token Optimizer's storage dirs
  compactions            compact_boundary rows in the transcript, or null
  earlier_checkpoint     {epoch, about} for the earlier session's checkpoint
                         flagged as resumable for this one at start, or null

Options:
  --session ID     required: the Claude Code session id
  --json           accepted for convention; output is always JSON
  --transcript P   read this transcript instead of looking it up by id
  --sync           compute savings now and rewrite the cache (the refresh child)
"""


def _status_bar_dir():
    return SNAPSHOT_DIR / "status-bar"


def _status_bar_key(session_id):
    """A file-name-safe key for a session: long ids are shortened with a hash so a
    file name never exceeds what the filesystem allows."""
    sid = str(session_id)
    if len(sid) <= 100:
        return sid
    return sid[:64] + "-" + hashlib.sha256(sid.encode("utf-8")).hexdigest()[:16]


def _status_bar_cache_path(session_id):
    return _status_bar_dir() / f"{_status_bar_key(session_id)}.json"


def _status_bar_lock_path(session_id):
    return _status_bar_dir() / f"{_status_bar_key(session_id)}.refresh.lock"


_BUCKET_DAY_SQL = {True: "substr(timestamp, 1, 10)", False: "''"}
_BUCKET_SESSION_SQL = {
    True: "AND (session_uuid = ? OR (session_uuid IS NULL AND session_id = ?)) ", False: ""}
_BUCKET_SAVINGS_SQL = _sql_variants(
    "SELECT {day}, event_type, COUNT(*), SUM(tokens_saved), "
    "SUM(cost_saved_usd) FROM {table} WHERE timestamp >= ? {scope}"
    "GROUP BY 1, event_type ORDER BY 4 DESC",
    table={"counted_savings_events": "counted_savings_events", "savings_events": "savings_events"},
    day=_BUCKET_DAY_SQL, scope=_BUCKET_SESSION_SQL)
_BUCKET_COMPRESSION_SQL = _sql_variants(
    "SELECT {day}, feature, COUNT(*), SUM(original_tokens), "
    "SUM(compressed_tokens), model, session_uuid, session_id FROM {table} "
    "WHERE timestamp >= ? {scope}"
    "AND (tier IS NULL OR tier = 'measured') "
    "GROUP BY 1, feature, model, session_uuid, session_id",
    table={"counted_compression_events": "counted_compression_events",
           "compression_events": "compression_events"},
    day=_BUCKET_DAY_SQL, scope=_BUCKET_SESSION_SQL)


def _realized_savings_buckets(conn, cutoff, session_uuid=None, by_day=False):
    """Realized savings per bucket, netted exactly like the window headline.

    Buckets are local days (savings_events / compression_events timestamps are
    naive local ISO) when *by_day*, else one bucket keyed "". With
    *session_uuid* only that session's rows count. Each bucket is
    {"tokens": int, "usd": float, "events": int}. Raises sqlite3.Error so the
    caller can tell a busy or broken DB from an empty one.
    """
    params = [cutoff]
    if session_uuid is not None:
        # Other runtimes' ids are not UUIDs: their rows keep the id in session_id.
        params.extend([session_uuid, session_uuid])
    se_sql = _BUCKET_SAVINGS_SQL[
        _counted_table(conn, "savings_events"), bool(by_day), session_uuid is not None]
    ce_sql = _BUCKET_COMPRESSION_SQL[
        _counted_table(conn, "compression_events"), bool(by_day), session_uuid is not None]
    se_rows = conn.execute(se_sql, tuple(params)).fetchall()
    ce_rows = conn.execute(ce_sql, tuple(params)).fetchall()
    ce_session_rates = _compression_session_rates(
        conn, [r[6] for r in ce_rows
               if not _compression_known_model(r[5])],
        [r[7] for r in ce_rows if not _compression_known_model(r[5])])
    deltas, _rt, _ut = _session_model_reprice(
        conn, cutoff, session_uuid=session_uuid, by_day=True)

    se_by_bucket = {}
    for bucket, event_type, cnt, tok, cost in se_rows:
        se_by_bucket.setdefault(bucket or "", []).append((event_type, cnt, tok, cost))
    deltas_by_bucket = {}
    for (day, event_type), delta in deltas.items():
        key = (day or "") if by_day else ""
        per = deltas_by_bucket.setdefault(key, {})
        per[event_type] = per.get(event_type, 0.0) + delta

    fallback_rate = _estimate_compression_cost_per_mtok()
    tier_data = PRICING_TIERS.get(_load_pricing_tier(), PRICING_TIERS["anthropic"])
    ce_by_bucket = {}
    for bucket, feature, cnt, orig, comp, model, s_uuid, sid in ce_rows:
        tokens_saved = (orig or 0) - (comp or 0)
        key = s_uuid if s_uuid in ce_session_rates else ("session_id", sid)
        rate = _compression_priced_rate(model, key, ce_session_rates, fallback_rate, tier_data)
        feats = ce_by_bucket.setdefault(bucket or "", {})
        entry = feats.setdefault(feature, {"events": 0, "tokens_saved": 0, "cost_saved_usd": 0.0})
        entry["events"] += cnt or 0
        entry["tokens_saved"] += tokens_saved
        entry["cost_saved_usd"] = round(
            entry["cost_saved_usd"] + round(tokens_saved * rate / 1_000_000, 6), 6)

    out = {}
    for bucket in set(se_by_bucket) | set(ce_by_bucket):
        by_category, tok, cost, events, _popped, _rp = _net_realized_savings(
            se_by_bucket.get(bucket, []), deltas_by_bucket.get(bucket, {}))
        a_tok, a_cost, a_events = _merge_v5_compression(
            by_category, ce_by_bucket.get(bucket, {}), fallback_rate)
        out[bucket] = {
            "tokens": int(tok + a_tok),
            "usd": round(cost + a_cost, 4),
            "events": int(events + a_events),
        }
    return out


def _open_trends_db_readonly(timeout=2.0):
    """Read-only connection to trends.db; never creates or migrates it."""
    uri = Path(TRENDS_DB).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    return conn


def _dashboard_saved_tokens(summary):
    """The dashboard's Tokens Saved headline: measured plus the estimated tier.

    Mirrors tokensSavedCardHtml in assets/dashboard.html exactly (same fields,
    same `a || b` fallbacks), so the status bar and the dashboard never disagree.
    """
    s = summary or {}

    def num(v):
        try:
            return max(0, int(float(v or 0)))
        except (TypeError, ValueError, OverflowError):
            return 0

    def tok(obj):
        try:
            return num((obj or {}).get("tokens_saved", 0))
        except AttributeError:
            return 0

    estimated = (tok(s.get("mcp_cap_estimated"))
                 + tok(s.get("hint_followed") or s.get("hint_followed_estimated"))
                 + tok(s.get("verbosity_steer") or s.get("verbosity_steer_estimated"))
                 + tok(s.get("resume_lean_estimated"))
                 + tok(s.get("checkpoint_restore_estimated")))
    measured = num(s.get("total_tokens", 0))
    # The dashboard draws no Tokens Saved card only when nothing was measured
    # AND nothing estimated exists -- estimates still surface on their own.
    return measured + estimated, measured


def _status_bar_savings_or_reason(session_id, _retried=False):
    """Compute the savings object. Returns (savings_dict, None) or (None, reason)."""
    if not Path(TRENDS_DB).exists():
        return None, "no savings recorded yet"
    today = datetime.now().date()
    first_day = today - timedelta(days=_STATUS_BAR_DAYS - 1)
    cutoff = datetime(first_day.year, first_day.month, first_day.day).isoformat()
    try:
        conn = _open_trends_db_readonly()
        try:
            # One read transaction: both bucket reads see the same committed rows (the
            # 30-day headline below runs on its own connection, as the dashboard's does).
            conn.execute("BEGIN")
            days = _realized_savings_buckets(conn, cutoff, by_day=True)
            window = _realized_savings_buckets(conn, cutoff).get("", {"tokens": 0})
            sess = _realized_savings_buckets(conn, "", session_uuid=session_id).get(
                "", {"tokens": 0, "usd": 0.0})
            conn.execute("COMMIT")
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "locked" in msg or "busy" in msg:
            return None, "savings database busy"
        if "no such column" in msg or "no such table" in msg:
            # A database from before these columns: the normal writer migrates it once.
            try:
                migrated = _init_trends_db()
                if migrated is not None:
                    migrated.close()
            except Exception:
                return None, "savings database unreadable"
            if _retried:
                return None, "savings database unreadable"
            return _status_bar_savings_or_reason(session_id, _retried=True)
        return None, "savings database unreadable"
    except (sqlite3.Error, OSError, ValueError):
        return None, "savings database unreadable"
    daily = []
    for i in range(_STATUS_BAR_DAYS):
        d = (first_day + timedelta(days=i)).isoformat()
        b = days.get(d, {})
        daily.append({"date": d, "tokens": int(b.get("tokens", 0)),
                      "usd": float(b.get("usd", 0.0))})
    headline = _get_merged_savings(days=_STATUS_BAR_DAYS)
    shown, measured = _dashboard_saved_tokens(headline)
    # The headline pass fails open to zeros; when the window, netted the way the
    # headline nets it, shows savings but the headline is zero, it failed: say so
    # rather than cache a false 0. Single days are not compared, since a debit on
    # one day can honestly cancel savings on another.
    if measured == 0 and int(window.get("tokens", 0)) > 0:
        return None, "savings database busy"
    return {
        "unit": "tokens",
        "session_tokens": int(sess.get("tokens", 0)),
        "session_usd": float(sess.get("usd", 0.0)),
        "daily": daily,
        "total_30d_usd": headline["total_cost_usd"],
        # The dashboard's headline (measured + estimated); the measured floor beside it.
        "total_30d_tokens": shown,
        "total_30d_measured_tokens": measured,
        "computed_at": time.time(),
    }, None


def _status_bar_write_cache(session_id, savings, reason):
    d = _status_bar_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = _status_bar_cache_path(session_id)
    if savings is None:
        # A failed refresh keeps the last good figures (they read as stale), never null them.
        prior = _status_bar_read_cache(session_id)
        if prior is not None and prior.get("savings") is not None:
            return
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({
        "schema": _STATUS_BAR_SCHEMA,
        "computed_at": time.time(),
        "savings": savings,
        "reason": reason,
    }), encoding="utf-8")
    os.replace(tmp, path)
    # Bound the directory: drop caches for sessions untouched for 30 days.
    cutoff = time.time() - _STATUS_BAR_CACHE_RETENTION_S
    orphan_cutoff = time.time() - 86400
    try:
        for f in d.iterdir():
            try:
                name = f.name
                # Temp files and set-aside locks left by a crash go after a day.
                orphan = name.endswith((".tmp", ".stale", ".release"))
                if (orphan and f.stat().st_mtime < orphan_cutoff) or (
                        name.endswith(".json") and f.stat().st_mtime < cutoff):
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _status_bar_read_cache(session_id):
    try:
        data = json.loads(_status_bar_cache_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("computed_at"), (int, float)):
        return None
    return data


def _status_bar_start_refresh(session_id):
    """Start ONE detached `status-bar --sync` child. Returns True if started.

    A lock file (O_EXCL) makes concurrent callers start at most one child; the
    child removes it when done, and a lock older than 120 s counts as abandoned.
    The lock holds a random owner token, passed to the child in the
    TO_STATUS_BAR_LOCK_TOKEN environment variable; the child deletes the lock
    only while it still holds that token.
    """
    token = _status_bar_acquire_lock(session_id)
    if token is None:
        return False
    # The child releases the lock only if it still holds this token, so a child
    # that outlives its lock never deletes a newer caller's lock.
    proc = spawn_detached(
        [_detached_python_exe(), str(MEASURE_PY_PATH), "status-bar",
         "--session", session_id, "--sync", "--release-lock"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, _STATUS_BAR_LOCK_TOKEN_ENV: token})
    if proc is None:
        _log_spawn_failure("status-bar refresh spawn failed")
        _status_bar_release_lock(session_id, token)
        return False
    return True


def _status_bar_acquire_lock(session_id):
    """Take the session's refresh lock; its owner token, or None when it is held."""
    lock = _status_bar_lock_path(session_id)
    token = os.urandom(16).hex()
    fd = None
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.time() - lock.stat().st_mtime < _STATUS_BAR_LOCK_STALE_S:
                return None
            # Take an abandoned lock over by renaming it aside. Rename is atomic,
            # so when two callers race only one moves the file; the other gets
            # FileNotFoundError and backs off instead of deleting the winner's lock.
            quarantine = lock.with_name(
                f"{lock.name}.{os.getpid()}.{time.time_ns()}.stale")
            try:
                os.rename(str(lock), str(quarantine))
            except OSError:
                return None
            try:
                moved_fresh = (time.time() - os.stat(str(quarantine)).st_mtime
                               < _STATUS_BAR_LOCK_STALE_S)
            except OSError:
                moved_fresh = False
            if moved_fresh:
                # Another caller replaced the stale lock between our check and the
                # rename, so we moved its live lock. Put it back without
                # overwriting anything (link fails if the name is taken) and back off.
                _status_bar_put_back(quarantine, lock)
                try:
                    quarantine.unlink()
                except OSError:
                    pass
                return None
            try:
                quarantine.unlink()
            except OSError:
                pass
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                return None
        written = os.write(fd, token.encode("ascii"))
        os.close(fd)
        fd = None
        if written != len(token):
            # A short write leaves a lock nobody can prove they own: remove ours now.
            try:
                lock.unlink()
            except OSError:
                pass
            return None
        return token
    except OSError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                lock.unlink()
            except OSError:
                pass
        return None


def _status_bar_release_lock(session_id, token):
    """Delete the refresh lock only when it still holds `token`. Never raises.

    The lock is moved aside first (one atomic rename) and only then checked, so a
    lock another process created after ours is never deleted by mistake: when the
    moved file is not ours it goes back, unless a newer lock already took its name.
    """
    if not token:
        return False
    lock = _status_bar_lock_path(session_id)
    aside = lock.with_name(f"{lock.name}.{os.getpid()}.{time.time_ns()}.release")
    try:
        os.rename(str(lock), str(aside))
    except OSError:
        return False
    try:
        mine = aside.read_text(encoding="ascii", errors="replace").strip() == token
    except OSError:
        mine = False
    if not mine:
        _status_bar_put_back(aside, lock)
    try:
        aside.unlink()
    except OSError:
        pass
    return mine


def _status_bar_put_back(aside, lock):
    """Return a lock moved aside to its name, never over a newer one.

    The caller still removes `aside` afterwards. A hard link puts it back only
    while the name is free. Without hard links it is dropped: a check-then-rename
    could replace a newer lock, and losing an old one only allows a duplicate
    refresh.
    """
    try:
        os.link(str(aside), str(lock))
    except (OSError, AttributeError):
        pass


# Prefilter only: the parsed row decides. Matching the bare word keeps rows from
# writers that space their JSON ("subtype": "compact_boundary") in the count.
_COMPACT_MARK = b"compact_boundary"


def _status_bar_compactions(path, session_id=None):
    """How many times this transcript was compacted (compact_boundary rows), or None.

    Counts rows whose parsed JSON is a system compact_boundary (the same key can
    sit inside a structured tool result). The quality cache's own count can lag
    a compaction, and the band shows this number right after one. The transcript
    only grows, so with a session id the count resumes after the last whole line
    the previous call read (kept beside the status cache).
    """
    memo = None
    if session_id:
        try:
            memo = _status_bar_dir() / f"compactions-{_status_bar_key(sanitize_session_id(session_id))}.json"
        except Exception:
            memo = None
    start, count = 0, 0
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if memo is not None:
        try:
            m = json.loads(memo.read_text(encoding="utf-8"))
            if m.get("path") == str(path) and 0 <= int(m.get("size", -1)) <= size:
                start, count = int(m["size"]), int(m["count"])
        except (OSError, ValueError, TypeError, KeyError):
            start, count = 0, 0
    try:
        end = start
        with open(path, "rb") as fh:
            if start > 0:
                # Resume only on a line boundary; anything else recounts from the top.
                fh.seek(start - 1)
                if fh.read(1) != b"\n":
                    start, count, end = 0, 0, 0
            fh.seek(start)
            for line in fh:
                if not line.endswith(b"\n"):
                    break  # a row still being written: the next call reads it whole
                end += len(line)
                if _COMPACT_MARK not in line:
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(row, dict) and row.get("type") == "system" and row.get("subtype") == "compact_boundary":
                    count += 1
        if memo is not None:
            try:
                # Never written backwards: a slower reader that ended earlier leaves a newer memo alone.
                try:
                    cur = json.loads(memo.read_text(encoding="utf-8"))
                    newer = cur.get("path") == str(path) and int(cur.get("size", -1)) > end
                except (OSError, ValueError, TypeError):
                    newer = False
                if not newer:
                    memo.parent.mkdir(parents=True, exist_ok=True)
                    tmp = memo.with_name(f".{memo.name}.{os.getpid()}.tmp")
                    tmp.write_text(json.dumps({"path": str(path), "size": end, "count": count}), encoding="utf-8")
                    os.replace(tmp, memo)
            except OSError:
                pass
        return count
    except OSError:
        return None


def _status_bar_transcript_state(path):
    """(last_request_epoch, cache_lifetime, model) from a transcript, read from the end.

    last_request_epoch: timestamp of the newest MAIN-thread assistant row that
    carries usage (isSidechain / agentId rows and <synthetic> rows skipped).
    cache_lifetime: the newest non-unknown _keepwarm_ttl_kind on main-thread rows,
    so read-only turns after a 1h write still report "1h". model: the newest
    main-thread assistant row's message.model (raw id, for the compact-window
    resolver). Scans at most 16 MB backwards. Returns (None, None, None) when
    the file is missing or unreadable.
    """
    last_ts = None
    lifetime = None
    model = None

    def _take(raw):
        nonlocal last_ts, lifetime, model
        raw = raw.strip()
        if not raw or b'"assistant"' not in raw:
            return
        try:
            rec = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            return
        if rec.get("isSidechain") is True or rec.get("agentId"):
            return
        msg = rec.get("message")
        if not isinstance(msg, dict) or msg.get("model") == "<synthetic>":
            return
        if model is None:
            m = msg.get("model")
            if isinstance(m, str) and m:
                model = m
        usage = msg.get("usage")
        if not isinstance(usage, dict) or not usage:
            return
        if last_ts is None:
            last_ts = _keepwarm_iso_to_epoch(rec.get("timestamp"))
        if lifetime is None:
            try:
                kind = _keepwarm_ttl_kind(usage)
            except (TypeError, ValueError, OverflowError):
                kind = "unknown"  # one malformed row; the older rows still count
            if kind != "unknown":
                lifetime = kind

    try:
        with open(path, "rb") as fh:
            pos = fh.seek(0, os.SEEK_END)
            carry = b""
            scanned = 0
            while pos > 0 and scanned < _STATUS_BAR_TAIL_MAX_BYTES:
                step = min(_STATUS_BAR_TAIL_CHUNK, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + carry
                scanned += step
                parts = buf.split(b"\n")
                carry = parts[0]
                for raw in reversed(parts[1:]):
                    _take(raw)
                    if last_ts is not None and lifetime is not None and model is not None:
                        return last_ts, lifetime, model
            if pos == 0 and carry:
                _take(carry)
    except (OSError, TypeError, ValueError):
        return None, None, None
    return last_ts, lifetime, model


def _status_bar_quality_cache_dirs():
    dirs = [QUALITY_CACHE_DIR, RUNTIME_DIR / "token-optimizer"]
    env_pd = os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
    if env_pd:
        dirs.append(Path(env_pd) / "token-optimizer")
    try:
        for d in (RUNTIME_DIR / "plugins" / "data").iterdir():
            if "token-optimizer" in d.name:
                dirs.append(d / "token-optimizer")
    except OSError:
        pass
    seen, out = set(), []
    for d in dirs:
        key = str(d)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def _status_bar_freshest(name):
    """The newest file called `name` across Token Optimizer's storage dirs, or None."""
    best, best_mtime = None, -1.0
    for d in _status_bar_quality_cache_dirs():
        f = d / name
        try:
            mt = f.stat().st_mtime
        except OSError:
            continue
        if mt > best_mtime:
            best, best_mtime = f, mt
    return best


def _status_bar_checkpoint_epoch(session_id):
    """When this session last saved a checkpoint, epoch seconds, or None.

    The newer of the quality cache's last_checkpoint_epoch (quality-triggered
    saves only) and this session's newest checkpoint file, so stop and
    compaction checkpoints count too.
    """
    newest_file = None
    for d in _status_bar_quality_cache_dirs():
        try:
            for f in (d / "checkpoints").glob(f"{session_id}-*.md"):
                mt = int(f.stat().st_mtime)
                if newest_file is None or mt > newest_file:
                    newest_file = mt
        except OSError:
            continue
    cached = _status_bar_cached_checkpoint_epoch(session_id)
    found = [v for v in (cached, newest_file) if v is not None]
    return max(found) if found else None


def _status_bar_cached_checkpoint_epoch(session_id):
    """last_checkpoint_epoch from the freshest quality-cache-<sid>.json, or None.

    None when the checkpoint it names is no longer on disk (retention removed
    it), so the band never reports a save that cannot be restored.
    """
    best = _status_bar_freshest(f"quality-cache-{session_id}.json")
    if best is None:
        return None
    try:
        cache = json.loads(best.read_text(encoding="utf-8"))
        val = cache.get("last_checkpoint_epoch")
        saved = cache.get("last_checkpoint_path")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    if not isinstance(saved, str) or not saved:
        return None
    try:
        if not Path(saved).is_file():
            return None
    except (OSError, ValueError):
        return None
    return val


def _status_bar_earlier_checkpoint(session_id):
    """The earlier session's checkpoint flagged for this one at start, or None.

    Reads the resumable flag the SessionStart pointer writes (freshest across
    the quality-cache dirs), so the band names the same checkpoint the
    terminal status line marks as resumable.
    """
    best = _status_bar_freshest(f"resumable-{session_id}.json")
    if best is None:
        return None
    try:
        flag = json.loads(best.read_text(encoding="utf-8"))
        # Only a checkpoint on this work: an unrelated recent one is noise here.
        if flag.get("relevant") is not True:
            return None
        cp = Path(str(flag.get("checkpoint") or ""))
        # Only a file in one of Token Optimizer's own checkpoint folders is read.
        homes = {str((d / "checkpoints").resolve()) for d in _status_bar_quality_cache_dirs()}
        if cp.suffix != ".md" or str(cp.resolve().parent) not in homes:
            return None
        epoch = int(cp.stat().st_mtime)
    except (OSError, ValueError, AttributeError, TypeError, RuntimeError):
        return None
    if not cp.name or cp.name.startswith(session_id):
        return None
    try:
        about = _checkpoint_descriptor(cp) or None
    except Exception:
        about = None
    return {"epoch": epoch, "about": about}


_PROMPT_CACHE_SIDECAR_MAX_AGE_S = 600


def _status_bar_prompt_cache(session_id):
    """The session's native prompt_cache object, bridged by statusline.js, or None.

    Claude Code v2.1.251+ hands the status line a `prompt_cache` object
    (ttl, expires_at, warm, hit_ratio, ...; main conversations only).
    statusline.js mirrors it to prompt-cache-<sid>.json so this payload can
    source the cache countdown/cold state from it instead of guessing from
    the transcript. Stale or malformed files fall back to the transcript.
    """
    best = _status_bar_freshest(f"prompt-cache-{session_id}.json")
    if best is None:
        return None
    try:
        data = json.loads(best.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        if time.time() - float(data.get("timestamp", 0)) / 1000 > _PROMPT_CACHE_SIDECAR_MAX_AGE_S:
            return None
    except (TypeError, ValueError):
        return None
    ttl = data.get("ttl")
    if ttl not in ("5m", "1h"):
        ttl = None
    try:
        expires_at = int(data["expires_at"]) if data.get("expires_at") is not None else None
    except (TypeError, ValueError):
        expires_at = None
    warm = data.get("warm") if isinstance(data.get("warm"), bool) else None
    return {"ttl": ttl, "expires_at": expires_at, "warm": warm}


def status_bar_payload(session_id, transcript=None, sync=False):
    """Build the status-bar JSON object (see STATUS_BAR_HELP). Never raises."""
    sid = sanitize_session_id(session_id)
    session_note = None
    # --session may carry a pasted truncated id. A <6-char id
    # sanitizes to "unknown"; a longer prefix finds no exact transcript.
    # Resolve a UNIQUE prefix to the real session; when it matches zero or
    # many, say so in savings_reason instead of degrading silently.
    prefix = re.sub(r"[^a-zA-Z0-9_-]", "", str(session_id or ""))
    try:
        path = Path(transcript) if transcript else (
            _find_session_jsonl_by_id(sid) if sid != "unknown" else None)
    except Exception:
        path = None
    if path is None and prefix:
        matches = _session_id_prefix_matches(prefix)
        if len(matches) == 1:
            path = matches[0]
            sid = matches[0].stem
        elif len(matches) > 1:
            session_note = ("session id prefix '%s' is ambiguous "
                            "(%d sessions match)" % (prefix, len(matches)))
        elif sid == "unknown":
            session_note = "no session id matches '%s'" % prefix
    out = {
        "schema": _STATUS_BAR_SCHEMA,
        "session_id": sid,
        "savings": None,
        "savings_state": "unavailable",
        "savings_age_s": None,
        "savings_reason": None,
        "refresh_started": False,
        "last_request_epoch": None,
        "cache_lifetime": None,
        "cache_expires_at": None,
        "cache_warm": None,
        "cache_source": None,
        "compactWindow": {"tokens": None, "source": "unresolved"},
        "last_checkpoint_epoch": None,
        "earlier_checkpoint": None,
        "compactions": None,
    }
    if sid == "unknown" or session_note:
        out["savings_reason"] = session_note or "no session id"
        return out

    try:
        session_model = None
        if path is not None:
            out["last_request_epoch"], out["cache_lifetime"], session_model = (
                _status_bar_transcript_state(path))
            out["compactions"] = _status_bar_compactions(path, sid)
        # The native prompt_cache object (v2.1.251+) is authoritative for the
        # cache countdown/cold state when statusline.js bridged it recently;
        # the transcript-derived lifetime stays as the fallback.
        pc = _status_bar_prompt_cache(sid)
        if pc is not None:
            if pc["ttl"]:
                out["cache_lifetime"] = pc["ttl"]
            out["cache_expires_at"] = pc["expires_at"]
            out["cache_warm"] = pc["warm"]
            out["cache_source"] = "prompt_cache"
        elif out["cache_lifetime"] is not None:
            out["cache_source"] = "transcript"
        # The resolver's compact window for THIS session's model, so the band
        # can honor /autocompact and autoCompactWindow, not just the env var.
        # tokens is null when no override is known: the band then uses the
        # host's own reported number.
        model = session_model or os.environ.get("CLAUDE_MODEL") or os.environ.get("ANTHROPIC_MODEL")
        resolved = _resolve_compact_window(model)
        out["compactWindow"] = {
            "tokens": resolved["tokens"] if resolved["user_override"] else None,
            "source": resolved["source"],
        }
    except Exception:
        pass
    try:
        out["last_checkpoint_epoch"] = _status_bar_checkpoint_epoch(sid)
    except Exception:
        pass
    try:
        out["earlier_checkpoint"] = _status_bar_earlier_checkpoint(sid)
    except Exception:
        pass

    try:
        if sync:
            savings, reason = _status_bar_savings_or_reason(sid)
            try:
                _status_bar_write_cache(sid, savings, reason)
            except OSError:
                pass
            out["savings"], out["savings_reason"] = savings, reason
            out["savings_age_s"] = 0.0
            out["savings_state"] = "fresh" if savings is not None else "unavailable"
            return out
        if not Path(TRENDS_DB).exists():
            out["savings_reason"] = "no savings recorded yet"
            return out
        cached = _status_bar_read_cache(sid)
        if cached is None:
            out["savings_state"] = "loading"
            out["savings_reason"] = "computing savings"
            out["refresh_started"] = _status_bar_start_refresh(sid)
            return out
        age = max(0.0, time.time() - float(cached["computed_at"]))
        out["savings"] = cached.get("savings")
        out["savings_reason"] = cached.get("reason") if out["savings"] is None else None
        out["savings_age_s"] = round(age, 1)
        stale = age > _STATUS_BAR_CACHE_MAX_AGE_S
        if out["savings"] is None:
            out["savings_state"] = "unavailable"
        else:
            out["savings_state"] = "stale" if stale else "fresh"
        if stale:
            out["refresh_started"] = _status_bar_start_refresh(sid)
    except Exception:
        out["savings"] = None
        out["savings_state"] = "unavailable"
        out["savings_reason"] = "savings unavailable"
    return out


def _status_bar_cli(args):
    """`status-bar` subcommand. Prints one JSON object; exit 0 (2 on bad usage)."""
    if "--help" in args or "-h" in args:
        print(STATUS_BAR_HELP)
        sys.exit(0)

    def _opt(name):
        if name in args:
            i = args.index(name)
            # A following flag is not a value: `--session --json` has no session.
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                return args[i + 1]
        return None

    session = _opt("--session")
    if not session:
        print(STATUS_BAR_HELP, file=sys.stderr)
        sys.exit(2)
    sync = "--sync" in args
    child = sync and "--release-lock" in args
    sid = sanitize_session_id(session)
    token = os.environ.get(_STATUS_BAR_LOCK_TOKEN_ENV, "") if child else None
    if sync and not child:
        # A direct --sync takes the same lock as the background refresh; when one is
        # already running, this answers from the cache instead of computing twice.
        token = _status_bar_acquire_lock(sid) if sid != "unknown" else None
        if token is None:
            sync = False
    # The background child gets a hard wall-clock budget: a hung read never lingers.
    def _out_of_time():
        # Killed by its budget: leave a reason for the band and free the lock.
        try:
            _status_bar_write_cache(sid, None, "savings took too long to compute")
        except OSError:
            pass
        _status_bar_release_lock(sid, token)
        _log_spawn_failure("status-bar refresh child ran out of time")

    budget = _install_hook_budget(_STATUS_BAR_CHILD_BUDGET_S, on_timeout=_out_of_time) if child else None
    try:
        payload = status_bar_payload(session, transcript=_opt("--transcript"), sync=sync)
    except Exception:
        payload = {"schema": _STATUS_BAR_SCHEMA, "session_id": sid, "savings": None,
                   "savings_state": "unavailable", "savings_reason": "savings unavailable"}
    finally:
        if token:
            _status_bar_release_lock(sid, token)
        if budget is not None:
            try:
                budget.cancel()
            except Exception:
                pass
    # Always one JSON object, exit 0: JSON has no Infinity or NaN, so those read as null.
    print(json.dumps(_status_bar_finite(payload), allow_nan=False))
    sys.exit(0)


def _status_bar_finite(v):
    """The payload with every non-finite float (inf, nan) replaced by None."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _status_bar_finite(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_status_bar_finite(x) for x in v]
    return v


# ---------------------------------------------------------------------------
# compact-advice: replay the user's own session history against candidate
# compact windows. Read-only, deterministic, no model calls. Every figure is
# an ESTIMATE derived from recorded prompt sizes; the output states its
# assumptions and never presents a window as a recommendation.
#
# Replay rule: a recorded compact_boundary ALWAYS happens; a candidate window
# can only ADD compactions before it. Extra compactions are never negative,
# and a candidate at or above where the session really compacted (or, for an
# open segment, above its real peak) is a no-op for that session. The row for
# the user's own resolved window is the reference: 0 extra, 0 net by
# construction.
# ---------------------------------------------------------------------------

_ADVICE_CANDIDATE_WINDOWS = (300_000, 400_000, 500_000, 650_000, 800_000)
# What one compaction costs. The summary length cannot be read back from a
# transcript, so it stays an assumption. The other two are MEASURED from the
# user's own recorded compactions and these constants are only the fallback
# used when fewer than _ADVICE_MIN_MEASURED real compactions exist.
_ADVICE_SUMMARY_OUTPUT_TOKENS = 4_000   # summary the model writes (output-priced), assumed
_ADVICE_POST_COMPACT_CONTEXT = 30_000   # fallback: context on the first request after a compact
_ADVICE_REREAD_DEFAULT = 20_000         # fallback: tokens re-read after a compact
_ADVICE_MIN_MEASURED = 5                # recorded compactions needed to trust a median
_ADVICE_REREAD_TURNS = 10               # assistant turns inspected after a boundary
_ADVICE_MIN_SESSIONS = 3                # below this, say "history too thin"
_ADVICE_ASSUMED_TAG = "assumed, too few real compactions to measure"
_ADVICE_REREAD_TOOLS = frozenset({"Read", "Grep", "Glob", "Bash"})
# coach --json: same discipline as the other coach blocks (cap, cache, never fails).
_ADVICE_COACH_MAX_SESSIONS = 150
_ADVICE_COACH_BUDGET_SECONDS = 4.0
_ADVICE_COACH_CACHE_TTL_SECONDS = 6 * 3600
_ADVICE_PATH_TOKEN_RE = re.compile(r"""[^\s"'<>|;&()=,]+""")
_ADVICE_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:/")


_advice_norm_path = _measure_compat.bind(_measure_providers['quality_archive']._advice_norm_path, __name__)


_advice_tool_targets = _measure_compat.bind(_measure_providers['quality_archive']._advice_tool_targets, __name__)


_advice_result_text = _measure_compat.bind(_measure_providers['quality_archive']._advice_result_text, __name__)


_advice_session_data = _measure_compat.bind(_measure_providers['quality_archive']._advice_session_data, __name__)


_advice_session_turns = _measure_compat.bind(_measure_providers['quality_archive']._advice_session_turns, __name__)


_advice_median = _measure_compat.bind(_measure_providers['quality_archive']._advice_median, __name__)


_advice_measured_assumptions = _measure_compat.bind(_measure_providers['quality_archive']._advice_measured_assumptions, __name__)


_advice_replay = _measure_compat.bind(_measure_providers['quality_archive']._advice_replay, __name__, defaults=(_ADVICE_POST_COMPACT_CONTEXT,))


_advice_quality_by_fill_band = _measure_compat.bind(_measure_providers['quality_archive']._advice_quality_by_fill_band, __name__)


_advice_billing_mode = _measure_compat.bind(_measure_providers['quality_archive']._advice_billing_mode, __name__)


_advice_round_pct = _measure_compat.bind(_measure_providers['quality_archive']._advice_round_pct, __name__)


compact_advice = _measure_compat.bind(_measure_providers['quality_archive'].compact_advice, __name__, defaults=(30, None, None,))


_advice_cache_path = _measure_compat.bind(_measure_providers['quality_archive']._advice_cache_path, __name__)


_advice_coach_view = _measure_compat.bind(_measure_providers['quality_archive']._advice_coach_view, __name__)


_coach_compact_advice_block = _measure_compat.bind(_measure_providers['quality_archive']._coach_compact_advice_block, __name__, defaults=(30,))


_advice_usd = _measure_compat.bind(_measure_providers['quality_archive']._advice_usd, __name__)


_compact_advice_cli = _measure_compat.bind(_measure_providers['quality_archive']._compact_advice_cli, __name__)


_install_date = _measure_compat.bind(_measure_providers['savings']._install_date, __name__)


_savings_since_install = _measure_compat.bind(_measure_providers['savings']._savings_since_install, __name__)


_live_savings_payload = _measure_compat.bind(_measure_providers['savings']._live_savings_payload, __name__, defaults=(30,))


_HEADLINE_HISTORY_FILE = "headline_history.json"


_headline_tripwire = _measure_compat.bind(_measure_providers['savings']._headline_tripwire, __name__)


_reread_savings_for_window = _measure_compat.bind(_measure_providers['savings']._reread_savings_for_window, __name__)


savings_report = _measure_compat.bind(_measure_providers['savings'].savings_report, __name__, defaults=(30, False,))


validate_impact = _measure_compat.bind(_measure_providers['savings'].validate_impact, __name__, defaults=("auto", 30, False,))


# ========== Settings Self-Heal (recovery for already-damaged users) ==========
#
# The wipe bug (setup_quality_bar writing {} over settings.json) is FIXED at
# the write guard (_settings_write_guard). But users ALREADY damaged stay
# damaged: their permissions, env, mcpServers, model, enabledPlugins,
# extraKnownMarketplaces are gone. This is the RECOVERY: a silent, additive,
# high-water-mark-based restore that fires only when the SURVIVOR FINGERPRINT
# proves a wipe (every surviving key is machine-written, zero user-authored
# keys survive).
#
# Gate (replaces the rejected 3+ key count rule):
#   1. SURVIVOR FINGERPRINT: every key in the live file is machine-written.
#      If even one user-authored key survives (permissions, model, etc.), do
#      nothing -- the user was editing deliberately.
#   2. High-water mark exists and has user-authored keys absent from live.
#   3. Absent keys are not tombstoned.
#   4. ABSENT-ONLY merge: restore only keys entirely absent, never overwrite.
#   5. SILENT to user, logged for us. Fail open, always.
#

# Keys that Token Optimizer or the host writes automatically to settings.json.
# If the live file's key set is a SUBSET of this (or the file is empty {}),
# the file is wipe residue. If ANY key outside this set survives, the user
# was editing deliberately.
_SETTINGS_MACHINE_WRITTEN_KEYS = frozenset({
    "cleanupPeriodDays",      # TO: run_ensure_health
    "statusLine",             # TO: setup_quality_bar
    "hooks",                  # TO: setup_all_hooks, setup_hook, etc.
    "compactInstructions",    # TO: generate_compact_instructions
    "mcpServers",             # TO: _manage_mcp
    "_disabledMcpServers",    # TO: _manage_mcp
    "env",                    # Host: settings env injection; TO historically (removed writer)
    "enabledPlugins",         # Host: /plugin UI
})


def _settings_heal_dir() -> Path:
    """Resolve the directory where self-heal data lives.

    Defaults to ``CONFIG_DIR / "settings_heal"`` which is
    ``~/.claude/token-optimizer/settings_heal`` on a standard install -- the
    same threat model as settings.json itself, so we harden permissions.
    """
    return CONFIG_DIR / "settings_heal"


def _settings_heal_ensure_dir() -> Path | None:
    """Create the heal dir with 0o700 permissions and return it, or None on
    failure. Idempotent: if the dir exists, verifies and tightens permissions."""
    try:
        d = _settings_heal_dir()
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(d), 0o700)
        except OSError:
            pass
        return d
    except OSError:
        return None


def _settings_write_file_secure(path: Path, content: str) -> bool:
    """Write a file with 0o600 permissions (atomic via tempfile + rename).
    Returns True on success."""
    try:
        d = path.parent
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(d), 0o700)
        except OSError:
            pass
        fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".heal-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, str(path))
            return True
        finally:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass
    except OSError:
        pass
    return False


def _settings_is_wipe_residue(data: dict) -> bool:
    """SURVIVOR FINGERPRINT: True when every key in the live file is
    machine-written, OR the file is empty {}. If even one user-authored key
    survives, this is NOT a wipe -- the user was editing deliberately.

    An empty {} is treated as wipe residue because the most complete wipe
    leaves exactly {}. A fresh install also starts as {}, but a fresh install
    has no high-water mark to restore from, so the heal is a no-op in that
    case. This is the safe default: if we can't tell, we check for a
    high-water mark and only heal if one exists."""
    if not isinstance(data, dict):
        return False
    if not data:
        return True  # empty {} is the most complete wipe
    return all(k in _SETTINGS_MACHINE_WRITTEN_KEYS for k in data)


def _settings_has_user_keys(data: dict) -> bool:
    """True when the data has at least one user-authored key (outside the
    machine-written set). Used to decide whether to snapshot vs. heal."""
    if not isinstance(data, dict):
        return False
    return any(k not in _SETTINGS_MACHINE_WRITTEN_KEYS for k in data)


def _settings_read_highwater() -> dict | None:
    """Read the high-water mark snapshot, or None if it doesn't exist."""
    try:
        p = _settings_heal_dir() / "highwater.json"
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _settings_update_highwater(data: dict) -> bool:
    """Update the high-water mark if this sighting has MORE top-level keys
    AND has at least one user-authored key. A file with only machine-written
    keys is never a valid high-water candidate (it could be wipe residue
    with more keys than the real highwater). Returns True if updated."""
    if not isinstance(data, dict) or not data:
        return False
    # Poisoning guard: only snapshot files that have user-authored keys.
    # A file with only machine-written keys is wipe residue, not a valid
    # high-water candidate.
    if not _settings_has_user_keys(data):
        return False
    try:
        d = _settings_heal_ensure_dir()
        if d is None:
            return False
        existing = _settings_read_highwater()
        if existing is None or len(data) > len(existing):
            if not _settings_write_file_secure(
                d / "highwater.json",
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            ):
                return False
            # Record metadata.
            meta = {
                "key_count": len(data),
                "key_set": sorted(data.keys()),
                "timestamp": datetime.now().isoformat(),
            }
            _settings_write_file_secure(
                d / "highwater.meta.json",
                json.dumps(meta, indent=2) + "\n",
            )
            return True
    except (OSError, TypeError):
        pass
    return False


def _settings_read_tombstones() -> set:
    """Read the set of tombstoned keys (keys the user removed after a heal)."""
    try:
        p = _settings_heal_dir() / "tombstones.json"
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return set(data.get("tombstoned_keys", []))
    except (json.JSONDecodeError, OSError):
        pass
    return set()


def _settings_write_tombstone(key: str) -> None:
    """Add a tombstone for a key the user deliberately removed post-heal.
    Uses atomic write via _settings_write_file_secure."""
    try:
        d = _settings_heal_ensure_dir()
        if d is None:
            return
        p = d / "tombstones.json"
        existing = set()
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                existing = set(data.get("tombstoned_keys", []))
        existing.add(key)
        _settings_write_file_secure(
            p,
            json.dumps(
                {"tombstoned_keys": sorted(existing),
                 "updated": datetime.now().isoformat()},
                indent=2,
            ) + "\n",
        )
    except (OSError, json.JSONDecodeError):
        pass


def _settings_heal_log(action: str, **fields) -> None:
    """Append a structured record to the heal log (JSONL). Capped at 1000
    entries to prevent unbounded growth."""
    try:
        d = _settings_heal_ensure_dir()
        if d is None:
            return
        entry = {"timestamp": datetime.now().isoformat(), "action": action}
        entry.update(fields)
        log_path = d / "heal_log.jsonl"
        # Cap log size: if >1000 lines, keep the last 500.
        if log_path.is_file():
            try:
                lines = log_path.read_text(encoding="utf-8").strip().split("\n")
                if len(lines) > 1000:
                    _settings_write_file_secure(
                        log_path,
                        "\n".join(lines[-500:]) + "\n",
                    )
            except (OSError, json.JSONDecodeError):
                pass
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _settings_check_tombstones(live: dict, previously_restored: set) -> None:
    """If the live file is healthy (has user keys) and a previously restored
    key is now absent, tombstone it. Called on every healthy sighting."""
    if not previously_restored:
        return
    if not _settings_has_user_keys(live):
        return
    for key in previously_restored:
        if key not in live:
            _settings_write_tombstone(key)


def _settings_read_restored_keys() -> set:
    """Read the set of keys restored in the last heal (for tombstone tracking).
    Reads from the END of the log for efficiency."""
    try:
        p = _settings_heal_dir() / "heal_log.jsonl"
        if not p.is_file():
            return set()
        # Read the last few lines to find the most recent restore entry.
        # This avoids loading the entire log on every healthy sighting.
        lines = p.read_text(encoding="utf-8").strip().split("\n")
        for line in reversed(lines):
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("action") == "restore":
                return set(entry.get("restored_keys", []))
        return set()
    except (OSError, json.JSONDecodeError):
        pass
    return set()


def _settings_write_atomic_silent(settings_data, allow_removing_keys=None) -> bool:
    """Silent variant of _write_settings_atomic: suppresses the guard's stderr
    refusal message so the heal stays silent to the user. The heal's own log
    captures the outcome."""
    # Save and restore stderr to suppress the guard's refusal message.
    # The guard prints to sys.stderr when it refuses a write; for the heal
    # path, that message would leak to the user's SessionStart output.
    _old_stderr = sys.stderr
    try:
        sys.stderr = open(os.devnull, "w")
        return _write_settings_atomic(settings_data, allow_removing_keys)
    finally:
        try:
            sys.stderr.close()
        except Exception:
            pass
        sys.stderr = _old_stderr


# ========== Authoritative source reconstruction ==========
#
# A SNAPSHOT IS NOT AUTHORITATIVE, it is only a hint. Before restoring any key,
# validate its value against the live authoritative record on disk and prefer
# that record over the snapshot wherever one exists. This prevents restoring
# stale values (e.g., a stale marketplace path that broke plugin discovery).
#
# Authoritative sources:
#   extraKnownMarketplaces -> ~/.claude/plugins/known_marketplaces.json
#   enabledPlugins         -> ~/.claude/plugins/installed_plugins.json (user scope)
#                             intersected with transcript evidence of actual use
#
# Snapshot fallback (for keys with no authoritative source):
#   env, compactInstructions, permissions, model, etc. -> high-water mark
#   For these, the high-water mark is the oldest rich snapshot (only updates
#   on MORE keys, so the newest post-damage version never replaces it).


def _settings_reconstruct_extra_known_marketplaces() -> dict | None:
    """Rebuild extraKnownMarketplaces from the authoritative on-disk record:
    ~/.claude/plugins/known_marketplaces.json.

    The settings.json version only stores the ``source`` sub-object (not
    installLocation/lastUpdated/autoUpdate), so we extract just that from
    each marketplace entry. This is the live authoritative record -- it
    reflects corrections the user made (e.g., fixing a stale path) that a
    two-week-old snapshot would not have.
    """
    try:
        p = CLAUDE_DIR / "plugins" / "known_marketplaces.json"
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        result = {}
        for name, entry in data.items():
            if isinstance(entry, dict) and isinstance(entry.get("source"), dict):
                source = entry["source"]
                # Validate: source must have a "source" string key.
                if isinstance(source.get("source"), str):
                    result[name] = {"source": source}
        return result if result else None
    except (json.JSONDecodeError, OSError):
        return None


def _settings_scan_transcripts_for_plugin_evidence(
    days: int = 14, deadline_seconds: float = 3.0,
) -> set:
    """Scan recent session transcripts for evidence of plugin use.

    Returns a set of plugin KEYS (``pluginname@marketplace``) that have
    evidence of actual use. Evidence means:
      * a plugin-qualified skill name like ``pluginname:skillname`` in a
        Skill tool_use block, OR
      * a plugin MCP surface like ``mcp__plugin_<pluginname>_<server>__``
        in a tool_use block.

    Time-bounded: bails out after ``deadline_seconds`` and returns whatever
    evidence was gathered so far. This is the fail-open principle: a partial
    scan is better than no scan, and the heal must not block SessionStart.
    """
    try:
        # Build plugin name -> set of full keys map.
        p = CLAUDE_DIR / "plugins" / "installed_plugins.json"
        if not p.is_file():
            return set()
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return set()
        plugins = data.get("plugins", {})
        if not isinstance(plugins, dict):
            return set()
        name_to_keys: dict[str, set[str]] = {}
        for plugin_key in plugins:
            parts = plugin_key.split("@", 1)
            if len(parts) == 2:
                name_to_keys.setdefault(parts[0], set()).add(plugin_key)
        if not name_to_keys:
            return set()

        # Build a single regex matching any known plugin name.
        names_sorted = sorted(name_to_keys.keys(), key=len, reverse=True)
        name_alt = "|".join(re.escape(n) for n in names_sorted)
        mcp_re = re.compile(r"mcp__plugin_(" + name_alt + r")_\w")
        skill_re = re.compile(r'"skill"\s*:\s*"(' + name_alt + r'):\w')

        evidence_keys: set[str] = set()
        start = time.time()
        jsonl_files = _find_all_jsonl_files(days=days)
        for jf, _mtime, _project in jsonl_files:
            if time.time() - start > deadline_seconds:
                break  # bail out: partial evidence is better than blocking
            try:
                content = jf.read_text(encoding="utf-8", errors="ignore")
                for m in mcp_re.finditer(content):
                    for key in name_to_keys.get(m.group(1), ()):
                        evidence_keys.add(key)
                for m in skill_re.finditer(content):
                    for key in name_to_keys.get(m.group(1), ()):
                        evidence_keys.add(key)
            except OSError:
                continue
        return evidence_keys
    except Exception:
        return set()


def _settings_reconstruct_enabled_plugins() -> dict | None:
    """Rebuild enabledPlugins from installed_plugins.json (user scope)
    intersected with transcript evidence of actual use.

    A plugin is included iff:
      1. It has a user-scope install record in installed_plugins.json, AND
      2. There is evidence of its use in recent session transcripts (a
         plugin-qualified skill name or MCP surface).

    This correctly EXCLUDES plugins that are installed but deliberately
    disabled (no transcript evidence because the user stopped using them).
    The installed-plugins list alone would wrongly re-enable them.
    """
    try:
        p = CLAUDE_DIR / "plugins" / "installed_plugins.json"
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        plugins = data.get("plugins", {})
        if not isinstance(plugins, dict):
            return None

        # Step 1: collect user-scope plugin keys.
        user_plugins = set()
        for plugin_key, records in plugins.items():
            if not isinstance(records, list):
                continue
            for rec in records:
                if isinstance(rec, dict) and rec.get("scope") == "user":
                    user_plugins.add(plugin_key)
                    break

        # Step 2: scan transcripts for evidence of actual use.
        evidence = _settings_scan_transcripts_for_plugin_evidence()

        # Step 3: intersect -- only plugins that are both installed (user
        # scope) AND have evidence of use.
        enabled = {k: True for k in user_plugins & evidence}
        return enabled if enabled else None
    except (json.JSONDecodeError, OSError):
        return None


# Keys that have an authoritative on-disk source for reconstruction.
# For these keys, the snapshot is NEVER used -- we always rebuild from disk.
_SETTINGS_AUTHORITATIVE_RECONSTRUCTORS = {
    "extraKnownMarketplaces": _settings_reconstruct_extra_known_marketplaces,
    "enabledPlugins": _settings_reconstruct_enabled_plugins,
}


def _settings_validate_value(key: str, value) -> bool:
    """Schema-validate a settings.json value before restoring it.

    A malformed value is worse than a missing key: a stale or corrupt
    extraKnownMarketplaces entry can break plugin discovery, a malformed
    enabledPlugins can re-enable a disabled plugin. Every restored value
    must pass this check before it is written.
    """
    if key == "extraKnownMarketplaces":
        if not isinstance(value, dict):
            return False
        for name, entry in value.items():
            if not isinstance(name, str) or not isinstance(entry, dict):
                return False
            source = entry.get("source")
            if not isinstance(source, dict):
                return False
            if not isinstance(source.get("source"), str):
                return False
        return True
    if key == "enabledPlugins":
        if not isinstance(value, dict):
            return False
        for k, v in value.items():
            if not isinstance(k, str) or not isinstance(v, bool):
                return False
        return True
    if key == "env":
        if not isinstance(value, dict):
            return False
        return all(isinstance(k, str) and isinstance(v, str)
                   for k, v in value.items())
    if key == "cleanupPeriodDays":
        return isinstance(value, int) and value > 0
    if key == "statusLine":
        if not isinstance(value, dict):
            return False
        return isinstance(value.get("command"), str)
    if key == "compactInstructions":
        return isinstance(value, str)
    if key == "hooks":
        if not isinstance(value, dict):
            return False
        return True  # basic shape check; hooks are machine-managed
    if key == "mcpServers":
        if not isinstance(value, dict):
            return False
        return True  # basic shape check
    # For unknown/user keys, accept any JSON-serializable value.
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def _settings_resolve_restore_value(key: str, highwater: dict) -> tuple:
    """Resolve the value to restore for a given absent key.

    Returns (value, source) where source is one of:
      "authoritative" -- rebuilt from the live on-disk record
      "snapshot"      -- taken from the high-water mark snapshot
      (None, None)    -- could not resolve; skip this key

    For keys with an authoritative source (extraKnownMarketplaces,
    enabledPlugins), the snapshot is NEVER used -- we always rebuild from
    disk. For all other keys, we fall back to the snapshot.
    """
    # Try authoritative source first.
    reconstructor = _SETTINGS_AUTHORITATIVE_RECONSTRUCTORS.get(key)
    if reconstructor is not None:
        try:
            value = reconstructor()
            if value is not None and _settings_validate_value(key, value):
                return value, "authoritative"
        except Exception:
            pass
        # If the authoritative source fails, do NOT fall back to the snapshot
        # for these keys. A stale snapshot value for extraKnownMarketplaces
        # or enabledPlugins is worse than no value (it can break plugin
        # discovery or re-enable a disabled plugin).
        return None, None

    # Fall back to snapshot for keys without an authoritative source.
    if key in highwater:
        value = highwater[key]
        if _settings_validate_value(key, value):
            return value, "snapshot"
    return None, None


def _maybe_self_heal_settings() -> None:
    """Silent self-heal for settings.json wipe damage. Called at the START of
    run_ensure_health, BEFORE any settings writes, so we see the file as the
    user left it.

    Flow:
      1. Read settings.json via the safe checked reader.
      2. If the file is healthy (has user-authored keys):
         a. Check tombstones: if a previously restored key is now absent,
            tombstone it.
         b. Snapshot: update the high-water mark if this sighting is richer.
         c. Record time-proximity signal (last healthy sighting timestamp).
      3. If the file is wipe residue (all machine-written keys, or empty {}):
         a. Read the high-water mark. If none, fail open (nothing to restore).
         b. Compute absent keys = high-water keys - live keys - tombstoned.
         c. If no absent keys, fail open.
         d. Backup the live file via _backup_settings_file. If backup fails,
            ABORT (no restore without a backup).
         e. Re-read settings.json under the settings lock to avoid value race.
         f. Additive merge: add only absent keys from the high-water mark.
         g. Write via _settings_write_atomic_silent (suppresses guard stderr).
         h. Log the restoration.

    SILENT: no stdout, no stderr to the user. All diagnostics go to the heal
    log. FAIL OPEN: any error returns without touching the file.
    """
    try:
        data, _path, ok = _read_settings_json_checked()
        if not ok or not isinstance(data, dict):
            return  # unreadable or malformed: fail open

        heal_dir = _settings_heal_ensure_dir()
        if heal_dir is None:
            return  # can't create heal dir: fail open

        # --- Healthy sighting: check tombstones + snapshot ---
        if _settings_has_user_keys(data):
            # Check if any previously restored keys were deliberately removed.
            previously_restored = _settings_read_restored_keys()
            _settings_check_tombstones(data, previously_restored)
            # Snapshot: update high-water mark if richer.
            if _settings_update_highwater(data):
                _settings_heal_log(
                    "snapshot",
                    key_count=len(data),
                    key_set=sorted(data.keys()),
                )
            # Time-proximity signal: record last healthy sighting timestamp.
            _settings_write_file_secure(
                heal_dir / "last_healthy.json",
                json.dumps({"timestamp": datetime.now().isoformat(),
                            "key_count": len(data)}) + "\n",
            )
            return  # healthy file: no heal needed

        # --- Wipe residue: check for heal conditions ---
        if not _settings_is_wipe_residue(data):
            return  # has unknown keys that aren't machine-written: fail open

        highwater = _settings_read_highwater()
        if not highwater:
            return  # no high-water mark: nothing to restore from

        # Compute absent keys (in highwater but not in live, not tombstoned).
        tombstones = _settings_read_tombstones()
        absent_keys = sorted(
            set(highwater.keys()) - set(data.keys()) - tombstones
        )
        if not absent_keys:
            return  # nothing to restore (all missing keys are tombstoned)

        # Pre-restore backup (reuses the existing _backup_settings_file helper).
        # If the backup fails, ABORT: no restore without a reversible backup.
        backup_dir = heal_dir / "backups"
        backup_path = _backup_settings_file(backup_dir)
        if backup_path is None:
            _settings_heal_log(
                "restore_aborted",
                reason="backup_failed",
                attempted_keys=absent_keys,
            )
            return  # fail open: no restore without a backup

        # Re-read settings.json to get the freshest state before merging.
        # This minimizes the value-race window: the initial read at the top
        # may be stale by the time we write. The _settings_write_guard inside
        # _write_settings_atomic will also re-read and refuse if keys were
        # added concurrently, but re-reading here ensures our merge uses the
        # latest values for present keys.
        fresh_data, _fresh_path, fresh_ok = _read_settings_json_checked()
        if fresh_ok and isinstance(fresh_data, dict):
            data = fresh_data
            # Recompute absent keys against the fresh read (a concurrent writer
            # may have added some of the keys we were about to restore).
            absent_keys = sorted(
                set(highwater.keys()) - set(data.keys()) - tombstones
            )
            if not absent_keys:
                return  # nothing to restore after fresh read

        # Additive merge: add ONLY absent keys.
        # Never overwrite a key that is present in the live file.
        # For each key, resolve the value:
        #   - Authoritative source (extraKnownMarketplaces, enabledPlugins):
        #     rebuild from the live on-disk record, NOT the snapshot.
        #   - All other keys: fall back to the high-water mark snapshot.
        # Every value is schema-validated before it is used. A malformed
        # value is worse than a missing key.
        merged = dict(data)
        restored = []
        sources = {}
        for key in absent_keys:
            value, source = _settings_resolve_restore_value(key, highwater)
            if value is not None:
                merged[key] = value
                restored.append(key)
                sources[key] = source
            # If value is None, skip this key: either the authoritative
            # source failed (and we do NOT fall back to a stale snapshot
            # for authoritative keys), or the snapshot value failed
            # schema validation.

        if not restored:
            return  # no keys could be restored after validation

        # Write via the silent atomic writer (suppresses guard stderr).
        # The guard will verify we're not dropping any keys.
        if _settings_write_atomic_silent(merged):
            _settings_heal_log(
                "restore",
                restored_keys=restored,
                sources=sources,
                from_snapshot=str(heal_dir / "highwater.json"),
                backup_path=str(backup_path),
                live_key_count=len(data),
                healed_key_count=len(merged),
            )
    except Exception:
        # Fail open, always. A heal that breaks a session is a worse bug
        # than the one it heals.
        pass


def run_ensure_health():
    """Body of the ensure-health subcommand. Extracted into its own function
    so the CLI dispatch can wrap the call in a hook wall-clock guard without
    reindenting 200 lines of logic. Called by SessionStart hook.

    Side effects: writes settings.json (cleanupPeriodDays, hook heal, path
    repair, quality bar install), writes config flags (heal timestamp,
    welcome shown, nudge shown), creates / prunes checkpoint and cache
    files, and may spawn a detached verified installer subprocess on
    script-install systems. All side effects are idempotent.

    Task ordering matters: fast, always-safe writes (cleanupPeriodDays)
    run first so they are guaranteed to complete even if a later task
    exhausts the wall-clock budget. (The old "bad env var removal" first
    task is gone: CLAUDE_AUTOCOMPACT_PCT_OVERRIDE is documented and is
    never touched here.)
    """
    # Foreign-runtime guardrail, defense-in-depth: every Claude
    # write below is gated on `not _is_codex`, so under OpenCode or Copilot
    # they would all fire against ~/.claude. The CLI dispatch already blocks
    # `ensure-health` for foreign runtimes; this early return also covers any
    # non-CLI caller (e.g. the Copilot sessionStart hook bridge).
    if _is_foreign_runtime():
        return

    _is_codex = detect_runtime() == "codex"
    _is_hermes = detect_runtime() == "hermes"
    # Positive identity, never a catch-all negation: foreign runtimes
    # (opencode, copilot) returned above, but if that guard is ever bypassed
    # they must not be treated as Claude and have ~/.claude written.
    _is_claude = detect_runtime() == "claude"
    # Claude Code cloud VM: skip everything that only pays off on a machine
    # that outlives the session (see _remote_lean_profile).
    _lean = _remote_lean_profile()

    # Settings self-heal: runs BEFORE any other settings writes so we see the
    # file as the user left it. Claude Code ONLY -- the wipe bug only affects
    # ~/.claude/settings.json, and Codex/Hermes use different config formats.
    # Silent, additive, high-water-mark-based recovery for users already
    # whose settings.json has lost top-level keys. See
    # _maybe_self_heal_settings().
    if _is_claude:
        try:
            _maybe_self_heal_settings()
        except Exception:
            pass  # fail open: a heal error must never break SessionStart

    # Preserve session transcripts: set cleanupPeriodDays if not configured.
    # Claude Code only: writes to ~/.claude/settings.json.
    # Codex and Hermes don't have this setting; their sessions persist by default.
    # Not in a cloud VM: its transcripts go away with the VM regardless.
    if _is_claude and not _lean:
        try:
            # Writes this dict back, so it must not act on an
            # unknown-state or missing-file {}. The `_cp_data` truthiness check
            # below already blocked the empty case; `_read_settings_for_write`
            # makes the intent explicit and covers the malformed case too.
            _cp_data, _cp_ok = _read_settings_for_write()
            if _cp_ok and _cp_data and "cleanupPeriodDays" not in _cp_data:
                _cp_data = dict(_cp_data)
                _cp_data["cleanupPeriodDays"] = 99999
                if _write_settings_atomic(_cp_data):
                    print("  [Token Optimizer] Set cleanupPeriodDays=99999 (preserves transcripts for trends)", file=sys.stderr)
                else:
                    print("  [Token Optimizer] cleanupPeriodDays was not changed (settings.json locked or refused).", file=sys.stderr)
        except Exception as _e:
            print(f"  [Token Optimizer] cleanupPeriodDays write failed: {_e}", file=sys.stderr)
    # CLAUDE_AUTOCOMPACT_PCT_OVERRIDE: documented setting. ensure-health
    # used to AUTO-DELETE it from settings.json; that path is gone and must
    # never return. Read-only here: nothing to do at startup. See
    # _autocompact_pct_override_explanation (doctor) for the explain-only path.

    # Subagent prompt-cache TTL (1h): ADVISE-ONLY by default. The
    # session start never writes the key itself -- it keeps the cached payoff
    # verdict fresh (a detached background scan) and records what the verdict
    # says, so `subagent-cache status`, doctor, quick and coach can carry the
    # per-user recommendation. Only TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H=1 opts
    # in to the automatic write + 14-day tripwire, and only that path can
    # return a user-facing line (one-time set / auto-revert notice).
    # Marker-gated BEFORE any settings read, so the steady-state session
    # start pays one stat/read and moves on (the "must stay inside the hook
    # time budget" rule). Claude Code only -- and never inside Cowork (which
    # must not read ~/.claude). Fail-open: a problem here must never break
    # SessionStart.
    if _is_claude and not is_cowork():
        try:
            for _sc_line in _subagent_cache_session_start_lines():
                print(json.dumps({"systemMessage": _sc_line}))
        except Exception:
            pass

    # Usage recommendations: keep the daily-measured record fresh.
    # Session start only stats the record and may spawn the detached refresh;
    # the measurement itself never runs inside a hook. Fail-open.
    if _is_claude and not is_cowork():
        try:
            _recs_ensure_fresh()
        except Exception:
            pass

    # Capture the pristine structural baseline once on first run. Records the
    # pre-pruning prefix overhead that structural savings are measured against.
    # One-time (no-op once the snapshot exists); never blocks SessionStart.
    try:
        if _auto_capture_pristine_baseline():
            print("  [Token Optimizer] Captured baseline snapshot for structural savings", file=sys.stderr)
    except Exception:
        pass

    # v5.3.8: auto-regenerate the dashboard HTML when the on-disk file
    # is stale relative to the currently installed plugin version.
    # Users upgrading via /plugin update keep seeing the OLD dashboard
    # because the file is only rewritten by /token-dashboard,
    # /token-optimizer, or the SessionEnd hook -- none of which fires
    # on /plugin update. Non-blocking: wrap in try/except so a bad
    # regen never breaks SessionStart.
    #
    # v5.4.9: dual marker check. Version string alone isn't enough --
    # if a future release shares TOKEN_OPTIMIZER_VERSION with existing
    # HTML but changes the data shape, users would silently see stale
    # numbers. We also check for a SHAPE marker keyed to the newest data
    # contract, so a dashboard generated before that contract is always
    # rebuilt even if its version string somehow matches. The marker tracks
    # the latest headline shape, so any dashboard missing the marker predates
    # the corrected calculation and is force-refreshed.
    try:
        if DASHBOARD_PATH.exists() and not _lean:
            # Cheap staleness check via the sidecar meta (~40 bytes) instead of
            # scanning the 4-5MB HTML every SessionStart. The dashboard is fresh
            # iff the sidecar's version+shape match the running build. A missing
            # sidecar = a dashboard generated before this scheme: fall back to the
            # one-time full-HTML scan, then the regen writes the sidecar so every
            # subsequent session takes the cheap path.
            _fresh = False
            _meta_path = _dashboard_meta_path(DASHBOARD_PATH)
            if _meta_path.exists():
                try:
                    _meta = json.loads(_meta_path.read_text(encoding="utf-8"))
                    # An older session must NOT regenerate (and thus clobber with
                    # pre-fix code) a dashboard a NEWER build wrote; a strictly-newer
                    # on-disk build reads as fresh. See _dashboard_meta_is_fresh.
                    _fresh = _dashboard_meta_is_fresh(_meta)
                except (OSError, json.JSONDecodeError, ValueError):
                    _fresh = False
            else:
                # Legacy dashboard with no sidecar: scan the HTML once.
                try:
                    with open(str(DASHBOARD_PATH), "r", encoding="utf-8", errors="replace") as _f:
                        _html = _f.read()
                    _fresh = (f'"version": "{TOKEN_OPTIMIZER_VERSION}"' in _html
                              and _DASHBOARD_SHAPE_MARKER in _html)
                except OSError:
                    _fresh = False
            if not _fresh:
                # Hand the ~9s rebuild to a DETACHED, unbounded, FORCED child
                # instead of running it in-process. run_ensure_health() runs under
                # an 8s SIGALRM budget (see the ensure-health dispatch), and this
                # rebuild is ~9s, so an in-process call reliably tripped
                # _HookTimeout (a BaseException the local `except Exception` cannot
                # catch). That aborted the WHOLE ensure-health tick BEFORE the
                # daemon-script auto-update block below ever ran -- so on a version
                # bump both the HTML AND the daemon script stayed stale, and unlike
                # the CLI dashboard path there was no detached-child fallback here
                # to ever catch the HTML up. The detached child finishes the forced
                # rebuild off the hot path (re-reading a now-healthy meter, which
                # clears the runway placeholder), and this cheap spawn returns at
                # once so execution always reaches the daemon-script update.
                # thundering herd: guard the spawn with a short-lived
                # inflight marker so N concurrent SessionStarts on a version bump
                # collapse to ONE detached rebuild instead of N racing os.replace
                # on the same file. Mirrors the sibling _daemon_install_lock(
                # soft_fail=True) serialisation the daemon-script update below
                # already uses; _dashboard_heal_spawn_due() fails OPEN so a lock
                # glitch never skips the heal.
                try:
                    _spawn_detached_dashboard_selfheal(days=30, force=True, hook_timeout=False)
                    print(f"  [Token Optimizer] Refreshing dashboard to v{TOKEN_OPTIMIZER_VERSION} (background)", file=sys.stderr)
                except Exception as _e:
                    print(f"  [Token Optimizer] dashboard refresh failed: {_e}", file=sys.stderr)
    except Exception as _e:
        print(f"  [Token Optimizer] dashboard staleness check failed: {_e}", file=sys.stderr)

    # Auto-regenerate daemon script when it's outdated (e.g. after plugin update).
    # The daemon is a generated file outside the plugin, so it doesn't update
    # automatically. Staleness marker (v5.4.6+): the generated script embeds the
    # version that created it. Any mismatch against the current running version
    # triggers a regen + restart. This is version-bound, not capability-bound,
    # so every future version bump propagates the daemon refresh automatically.
    #
    # Path-aware write (v5.4.7+): users who ran setup-daemon BEFORE the plugin-data
    # migration have a launchd plist / systemd unit / scheduled task pointing at
    # the legacy path (~/.claude/_backups/token-optimizer/dashboard-server.py).
    # Users who set it up AFTER point at the plugin-data SNAPSHOT_DIR. We write
    # to BOTH locations so whichever one the service manager expects gets served.
    try:
        current_marker = f'TOKEN_OPTIMIZER_DAEMON_VERSION = "{TOKEN_OPTIMIZER_VERSION}"'
        legacy_dir = RUNTIME_DIR / "_backups" / "token-optimizer"
        candidate_paths = set() if (_daemon_snapshot_sandboxed() or _lean) else {
            SNAPSHOT_DIR / "dashboard-server.py", legacy_dir / "dashboard-server.py"}
        # The auto-update refresh writes dashboard-server.py (not the LaunchAgent
        # plist) and reloads via `launchctl kickstart`, which restarts the process
        # without re-registering the background item -- so it does NOT fire the
        # macOS "App Background Activity" banner. That banner comes from plist
        # rewrites and bootout+bootstrap, which the install/repair paths now guard
        # with _write_file_if_changed + _launchagent_loaded. So this block keeps its
        # original version-marker staleness check (daemon stays version-current).
        needs_refresh = False
        for p in candidate_paths:
            if p.exists():
                try:
                    if current_marker not in p.read_text(encoding="utf-8", errors="replace"):
                        needs_refresh = True
                        break
                except OSError:
                    continue
        if needs_refresh:
            # v5.4.19 (a follow-up fix): serialise concurrent ensure-health updates so
            # two Claude sessions starting simultaneously don't both write the
            # daemon script and fight over launchctl kickstart. soft_fail=True
            # means the second session just skips the update (the first will
            # land it within milliseconds).
            with _daemon_install_lock(soft_fail=True) as acquired:
                if not acquired:
                    # Another session is already refreshing -- no-op.
                    pass
                else:
                    # Make sure the per-install token exists before writing
                    # the new script (which references DAEMON_TOKEN_PATH).
                    _get_or_create_daemon_token()
                    new_script = _generate_daemon_script()
                    # Track per-path write
                    # outcomes so we can detect silent drift (e.g., legacy
                    # path is unwritable and every SessionStart loops through
                    # this code printing "Auto-updated" without the daemon
                    # ever actually picking up the new script).
                    write_failures = []
                    wrote_any = False
                    for p in candidate_paths:
                        try:
                            p.parent.mkdir(parents=True, exist_ok=True)
                            # atomic daemon-script write: temp sibling +
                            # os.replace instead of a raw write_text. This block
                            # runs under the 8s ensure-health hook budget; a
                            # SIGALRM (_HookTimeout) mid-write would truncate
                            # dashboard-server.py, leaving a syntactically broken
                            # daemon that never starts. os.replace swaps the file
                            # in one step so a reader/loader sees the old or the new
                            # script whole, never a torn one.
                            tmp_fd, tmp_name = tempfile.mkstemp(
                                dir=str(p.parent), prefix=".dashsrv-", suffix=".tmp")
                            try:
                                if hasattr(os, "fchmod"):  # POSIX only
                                    os.fchmod(tmp_fd, 0o600)
                                with os.fdopen(tmp_fd, "w", encoding="utf-8") as _f:
                                    _f.write(new_script)
                                os.replace(tmp_name, str(p))
                            except BaseException:
                                # BaseException, not OSError: a mid-write
                                # _HookTimeout (the 8s budget) must still clean up
                                # the temp and propagate. OSError is caught below as
                                # a per-path write failure.
                                try:
                                    os.unlink(tmp_name)
                                except OSError:
                                    pass
                                raise
                            wrote_any = True
                        except OSError as e:
                            write_failures.append((p, e))
                    if write_failures and not wrote_any:
                        # All candidate paths failed: surface so user can
                        # debug instead of silently no-oping every session.
                        sys.stderr.write(
                            "[Token Optimizer] Warning: daemon auto-update "
                            "could not write any dashboard-server.py path:\n"
                        )
                        for p, e in write_failures:
                            sys.stderr.write(f"  {p}: {e}\n")
                        # Bail out of the refresh flow — don't kickstart a
                        # daemon we haven't actually updated.
                        return
                    elif write_failures:
                        # Partial success: one path landed, another didn't.
                        # Log once so silent drift on the failing path is
                        # visible, then proceed with kickstart on the path
                        # that did update.
                        sys.stderr.write(
                            "[Token Optimizer] Note: daemon auto-update "
                            "skipped unwritable path(s):\n"
                        )
                        for p, e in write_failures:
                            sys.stderr.write(f"  {p}: {e}\n")
                    # Restart the daemon so the new script takes effect, then
                    # apply escalation + honest logging. Extracted to
                    # _apply_daemon_restart_outcome for testability (the reap /
                    # kickstart / verify path is the single source of truth).
                    # Gate the restart on the SHARED
                    # gate -- a version bump must not /Run a daemon whose user
                    # tombstoned/disabled it or whose install is condemned.
                    # The script write above is inert and may stand either way.
                    if _daemon_resurrection_blocked() is None:
                        _, _msg = _apply_daemon_restart_outcome(
                            _restart_dashboard_daemon(_normalized_platform()))
                        print(f"  [Token Optimizer] {_msg}", file=sys.stderr)
    except Exception as _e:
        print(f"  [Token Optimizer] daemon auto-update check failed: {_e}", file=sys.stderr)

    # Dashboard daemon self-heal: INSTALL it if missing, RESTART it if dead.
    # Default-on for every Claude user (loopback-only + token-authed + ~0 cost),
    # sticky `daemon_disabled` opt-out -- the quality-bar posture, not the
    # marker-gated keep-warm one. Runs AFTER the auto-update block above: that
    # block owns the has-daemon (stale-script) case; this owns the absent/dead
    # case so a lost plist / never-ran-the-audit user still gets the bookmarkable
    # localhost URL. Cheap-gate-first (config read + stat + 1s port probe) and the
    # costly install/restart is internally throttled, so the healthy hot path
    # stays inside the SessionStart budget. Non-blocking (try/except).
    try:
        _daemon_ensure = _ensure_dashboard_daemon()
        if _daemon_ensure == "installed":
            # User-visible onboarding notice (bookmarkable URL). Emitted as a
            # systemMessage so the CC UI shows it to the user without sending
            # it to the model. See the noop-install-failed branch below for the
            # full stdout/stderr/systemMessage rationale.
            print(json.dumps({"systemMessage":
                "  [Token Optimizer] Dashboard daemon installed. "
                f"URL: http://localhost:{DAEMON_PORT}/token-optimizer "
                "(path required). Opt out: measure.py setup-daemon --uninstall"}))
        elif _daemon_ensure == "restarted":
            print("  [Token Optimizer] Restarted the dashboard daemon.", file=sys.stderr)
        elif _daemon_ensure == "restart-stale":
            # The dead-daemon restart came back still serving a stale version --
            # surface it instead of swallowing it silently until the throttle
            # window (a crashed-on-bind daemon would otherwise go unreported).
            print("  [Token Optimizer] dashboard daemon restarted but still "
                  "serving a stale version; run: measure.py setup-daemon",
                  file=sys.stderr)
        elif _daemon_ensure == "noop-install-failed":
            # Persistent, user-recoverable state that the user MUST see.
            # Three channels, three outcomes on a SessionStart hook:
            #   - plain stdout  -> injected into model context every turn (tax)
            #   - stderr        -> invisible in the CC UI on exit 0 (only in
            #                      the Ctrl+O transcript), so the wedge goes
            #                      silent -- the exact failure the
            #                      persistent-disable wedge fix addresses.
            #   - systemMessage -> folded by the runner into the hook envelope,
            #                      rendered to the USER as "<hook> says: ...",
            #                      and NOT sent to the model (zero model tokens).
            # So a systemMessage is the only channel that is both user-visible
            # and model-silent. `measure.py doctor` also reports the marker.
            _mk_reason = _daemon_install_failed_reason()
            print(json.dumps({"systemMessage":
                "  [Token Optimizer] Dashboard daemon self-heal disabled: "
                f"install failed permanently ({_mk_reason or 'reason unknown'}). "
                f"Retry: {_measure_cli()} setup-daemon"}))
    except Exception as _e:
        print(f"  [Token Optimizer] dashboard daemon self-heal failed: {_e}", file=sys.stderr)

    # Bounded stale-lease sweeper. Self-heals the unbounded ``.qlease``
    # tombstone / orphan candidate / legacy ``.qlock`` litter that release()
    # intentionally leaves for the next contender (most sessions get none, so
    # they leak forever). Throttled to once per 24h via a one-stat marker, capped
    # at max_files per run, and fail-open -- it NEVER raises into the hook and
    # runs AFTER the daemon revive so it can never starve it. Not on the hot
    # per-tool-call path (this is the SessionStart ensure-health path only).
    try:
        maybe_sweep_stale_leases()
    except Exception:
        pass

    # VS Code-family status-bar extension auto-install. Only meaningful for the
    # Claude runtime running inside a VS Code/Cursor/Windsurf editor; the detector
    # no-ops everywhere else (plain terminal, foreign runtimes). Default-on with a
    # sticky opt-out and a 24h throttle, mirroring the quality bar / daemon.
    # Non-blocking (try/except) so an editor CLI hiccup never breaks SessionStart.
    if _is_claude:
        try:
            _ensure_vscode_extension()
        except Exception as _e:
            print(f"  [Token Optimizer] VS Code extension self-install failed: {_e}", file=sys.stderr)

    # Keep-warm scheduler repair. ADDITIVE + consent-gated +
    # cheap. Regenerates a user-deleted/stale keep-warm agent ONLY when the
    # install marker exists AND consent allows (sticky-opt-out:
    # user-removed != regen-target unless WE installed it and consent still
    # holds). The no-op hot path is file-existence checks only (no subprocess)
    # so it stays well inside the ensure-health budget. Completely separate from
    # the dashboard daemon block above: distinct label, own install lock, never
    # touches the dashboard plist/lock/regen loop. Non-blocking (try/except).
    try:
        _kw_repair = keepwarm_scheduler_repair()
        if _kw_repair == "repaired":
            print("  [Token Optimizer] Repaired keep-warm scheduler agent.", file=sys.stderr)
    except Exception as _e:
        print(f"  [Token Optimizer] keep-warm scheduler repair failed: {_e}", file=sys.stderr)

    # v5.2.0 migration notice for Windows users. v5.1.0 and earlier shipped
    # a hooks.json that used POSIX shell syntax, which silently failed on
    # Windows PowerShell -- the plugin installed but every hook was a
    # no-op. After upgrade to v5.2.0 the wrapper-based hooks work. Surface
    # a one-time notice so users know to re-check their dashboard and run
    # the self-check. Shown exactly once per install (idempotent flag).
    try:
        if platform.system() == "Windows":
            if not _read_config_flag("v52_windows_welcome_shown", False):
                selfcheck_path = str(Path(__file__).resolve())
                print("  [Token Optimizer v5.2.0] Windows support is now live.", file=sys.stderr)
                print("  v5.1.0 hooks ran via POSIX shell and failed silently on Windows;", file=sys.stderr)
                print("  v5.2.0 uses a cross-platform wrapper so everything now works.", file=sys.stderr)
                print("  Verify with: python3 \"" + selfcheck_path + "\" health-selfcheck", file=sys.stderr)
                _write_config_flag("v52_windows_welcome_shown", True)
    except Exception:
        pass
    # v5.0.2: Self-heal hooks with correct semantics for each install mode.
    # Claude Code only: reads/writes ~/.claude/settings.json for hook management.
    # Never in a cloud VM (lean profile): the cloud kit's setup script wires
    # the hooks into the VM's ~/.claude/settings.json, so adding the canonical
    # set there too would fire every hook twice (setup_all_hooks also stands
    # down via _cloud_kit_owns_hooks when the lean profile is off).
    if not _is_codex and not _lean:
        try:
            last_check = _read_config_flag("last_hook_heal_check", 0)
            now = int(time.time())
            if now - int(last_check or 0) > 86400:  # 24h
                # Layer 2: rewrite/remove the collect&&dashboard fossil in
                # BOTH install modes. Exact-identity dedup cannot match the
                # renamed shape, and setup_all_hooks is additive-only.
                try:
                    fossil = _reconcile_sessionend_fossils()
                    rewritten = fossil.get("rewritten", 0)
                    removed_fossils = fossil.get("removed", 0) + fossil.get("stop_removed", 0)
                    if rewritten or removed_fossils:
                        print(
                            "  [Token Optimizer] Reconciled SessionEnd fossil hook(s) "
                            f"(rewritten={rewritten}, removed={removed_fossils}). Restart to apply.",
                            file=sys.stderr,
                        )
                except Exception:
                    pass
                if _is_plugin_installed():
                    cleanup_result = _cleanup_duplicate_plugin_hooks_from_settings(dry_run=False)
                    removed = cleanup_result.get("removed", 0)
                    if removed > 0:
                        print(f"  [Token Optimizer] Removed {removed} duplicate hook(s) from settings.json. Restart to apply.", file=sys.stderr)
                    _write_config_flag("last_hook_heal_check", now)
                else:
                    # Double-registration guard (2026-08-29): if our
                    # SessionStart hook is firing but ~/.claude/settings.json
                    # has ZERO Token Optimizer hooks, our hooks are coming
                    # from another settings layer (--settings flag, project
                    # settings, managed policy). Adding them here would
                    # double-register and every hook would fire twice. The
                    # host does not expose the --settings path or the merged
                    # hook set to hook subprocesses, so the detection is
                    # indirect: hook-firing + zero-our-hooks-in-settings.json
                    # → hooks are from another layer → skip.
                    _skip_result = _should_skip_self_heal_hooks()
                    _skip, _confident = _skip_result
                    if _skip:
                        if _confident:
                            # Confident skip: settings.json is readable and has
                            # zero TO hooks → hooks are from another layer → safe
                            # to advance the 24h throttle.
                            _write_config_flag("last_hook_heal_check", now)
                        # else: uncertain skip (missing/malformed settings.json).
                        # Do NOT advance the throttle so the next SessionStart
                        # re-checks sooner instead of locking out healing for 24h.
                        print("  [Token Optimizer] Hooks from another layer; skipping heal.",
                              file=sys.stderr)
                    else:
                        heal_result = setup_all_hooks(dry_run=False, verbose=False)
                        added = heal_result.get("added", 0)
                        if added > 0:
                            print(f"  [Token Optimizer] Self-healed {added} missing hook(s) in settings.json. Restart to activate.", file=sys.stderr)
                        elif added == 0 and not heal_result.get("error"):
                            _write_config_flag("last_hook_heal_check", now)
        except Exception:
            pass
    # v5.1: First-run welcome. Shows once when v5 is first seen on this machine.
    # Routed to stderr so the welcome banner never enters the model's context.
    # Not in a cloud VM: every cloud session starts on a fresh VM, so the
    # "first run" banner would repeat every session.
    try:
        if not _lean and not _read_config_flag("v5_welcome_shown", False):
            from contextlib import redirect_stdout as _redirect_welcome
            with _redirect_welcome(sys.stderr):
                _show_v5_welcome()
            _write_config_flag("v5_welcome_shown", True)
    except Exception:
        pass
    # Fix stale versioned plugin cache paths in settings.json.
    # Claude Code only: reads/writes ~/.claude/settings.json.
    if not _is_codex:
        try:
            _stale_fixed = _fix_stale_settings_paths()
            if _stale_fixed:
                print(f"  [Token Optimizer] Fixed {_stale_fixed} stale plugin path(s) in settings.json", file=sys.stderr)
        except Exception as _e:
            print(f"  [Token Optimizer] stale path fix failed: {_e}", file=sys.stderr)
    # Lift an existing version-pinned statusLine onto the update-surviving
    # marketplace clone path so the installed base stops seeing a blank status
    # line on the first session after every plugin update (G3 C-P2-3). Runs
    # after the stale-path fix so the cache path is already current before it is
    # migrated to the clone. Claude Code only.
    if not _is_codex:
        try:
            if _migrate_statusline_to_stable_path():
                print("  [Token Optimizer] Migrated statusLine to stable path", file=sys.stderr)
        except Exception as _e:
            print(f"  [Token Optimizer] statusLine path migration failed: {_e}", file=sys.stderr)
        try:
            if _heal_dangling_statusline():
                print("  [Token Optimizer] Re-pointed a statusLine that ran a missing "
                      "or Codex-cache statusline.js", file=sys.stderr)
        except Exception as _e:
            print(f"  [Token Optimizer] statusLine heal failed: {_e}", file=sys.stderr)
        # Same class for the keep-warm launchd agent: an existing plist pinned to a
        # version-cache measure.py path dies on plugin update and freezes the
        # dashboard's savings. Lift it onto the clone path (macOS-only in practice).
        try:
            if _heal_keepwarm_plist_path():
                print("  [Token Optimizer] Healed keep-warm agent to stable path", file=sys.stderr)
        except Exception as _e:
            print(f"  [Token Optimizer] keep-warm path heal failed: {_e}", file=sys.stderr)
        # Same class again: a Windows install made by an older build
        # already has a console-flashing Scheduled Task action and .cmd shim on
        # disk. Fixing the generators only helps the next install, so repair the
        # installed artifacts in place. Strict no-op off Windows, and gated so it
        # can never resurrect a declined/uninstalled/failed daemon.
        try:
            if _heal_windows_console_flash():
                print("  [Token Optimizer] Healed Windows daemon launcher, console flashing removed", file=sys.stderr)
        except Exception as _e:
            print(f"  [Token Optimizer] Windows console-flash heal failed: {_e}", file=sys.stderr)
    # Remove malformed hook commands (subshell patterns, double-$HOME paths).
    # Claude Code only: reads/writes ~/.claude/settings.json.
    if not _is_codex:
        try:
            _malformed_fixed = _fix_malformed_hook_commands()
            if _malformed_fixed:
                print(f"  [Token Optimizer] Removed {_malformed_fixed} malformed hook(s) from settings.json. Restart to apply.", file=sys.stderr)
        except Exception as _e:
            print(f"  [Token Optimizer] malformed hook fix failed: {_e}", file=sys.stderr)
    # Plugin cleanup is available as `measure.py plugin-cleanup` but NOT auto-run.
    # Deleting cache dirs on SessionStart can break plugins that load hooks from
    # dogfood/development paths. Users should run it manually after review.
    # Migrate data to CLAUDE_PLUGIN_DATA on first run (v2.1.78+).
    # Idempotent per file: the "not exists" guard means a retry after
    # partial progress copies only the missing files. The marker is
    # written after the copy loop so a hook-budget timeout mid-copy
    # re-enters the migration on the next SessionStart and eventually
    # completes. Leaving the marker last preserves correctness under
    # interrupt; the only downside is slow completion on pathologically
    # slow filesystems, which is acceptable for a one-time path.
    # Migration also fires when plugin-data was discovered via installed_plugins
    # (CLI dashboard path), not just when Claude Code set the env var.
    _migration_target = _PLUGIN_DATA or (str(_RESOLVED_PLUGIN_DATA) if _RESOLVED_PLUGIN_DATA else None)
    if _migration_target:
        # Outer OSError guard closes a latent unhandled-exception path: if
        # SNAPSHOT_DIR / CONFIG_DIR mkdir fails (unwritable, disk full),
        # the OSError would otherwise propagate through _run_ensure_health
        # and miss the outer dispatch's _HookTimeout catch.
        try:
            _legacy_data = RUNTIME_DIR / "_backups" / "token-optimizer"
            _legacy_config = RUNTIME_DIR / "token-optimizer"
            _migrated_marker = Path(_migration_target) / ".migrated"
            if not _migrated_marker.exists():
                import shutil
                SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                for src_dir, dst_dir in [(_legacy_data, SNAPSHOT_DIR), (_legacy_config, CONFIG_DIR)]:
                    if src_dir.is_dir():
                        for f in src_dir.iterdir():
                            if f.is_file() and not (dst_dir / f.name).exists():
                                try:
                                    shutil.copy2(f, dst_dir / f.name)
                                except OSError:
                                    pass
                try:
                    _migrated_marker.touch()
                except OSError:
                    pass
        except OSError:
            pass
    # Clean up orphaned temp files from interrupted atomic writes
    # Note: .settings.lock is NOT cleaned up (zero-byte sentinel, not a leak;
    # deleting it while held could break the advisory lock for other processes)
    for f in SETTINGS_PATH.parent.glob(".settings-*.json"):
        try:
            if time.time() - f.stat().st_mtime > 3600:
                f.unlink()
        except OSError:
            pass
    # Sweep orphaned checkpoint temp files left by an interrupted atomic
    # write in _write_checkpoint_atomic. Defensive: the helper already
    # cleans up on OSError, but a signal interrupt between mkstemp and
    # the tuple unpack can leave an unnamed temp file on disk (rare in
    # CPython, but unhandled by the helper itself).
    try:
        for f in CHECKPOINT_DIR.glob(".checkpoint-*.md"):
            try:
                if time.time() - f.stat().st_mtime > 3600:
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass
    # Prune old quality-cache and decisions files (older than 7 days)
    _prune_cutoff = time.time() - 7 * 86400
    try:
        cache_files = sorted(
            QUALITY_CACHE_DIR.glob("quality-cache-*.json"),
            key=lambda f: f.stat().st_mtime, reverse=True
        )
        for f in cache_files[10:]:  # Keep 10 most recent regardless of age
            try:
                if f.stat().st_mtime < _prune_cutoff:
                    f.unlink()
            except OSError:
                pass
    except (OSError, ValueError):
        pass
    try:
        decisions_dir = SNAPSHOT_DIR / "read-cache" / "decisions"
        if decisions_dir.is_dir():
            for f in decisions_dir.glob("*.jsonl"):
                try:
                    if f.stat().st_mtime < _prune_cutoff:
                        f.unlink()
                except OSError:
                    pass
    except (OSError, ValueError):
        pass
    # Auto-install / auto-restore quality bar on SessionStart.
    # Claude Code only: reads/writes ~/.claude/settings.json for statusLine and hooks.
    # Not in a cloud VM: there is no status line to show it in.
    if not _is_codex and not _lean:
        try:
            _eh_qb_disabled = _read_config_flag("quality_bar_disabled", False)
            _eh_is_plugin = _is_running_from_plugin_cache() or _is_plugin_installed()
            if not _eh_qb_disabled and _is_regular_file(SETTINGS_PATH):
                try:
                    settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, ValueError):
                    # Malformed settings.json (often a transient partial write by
                    # another tool). Skip the quality-bar self-heal this session
                    # rather than risk overwriting a recoverable file — and stay
                    # silent so a broken file doesn't spam stderr every session.
                    settings = None
                if isinstance(settings, dict):
                    has_statusline = bool(settings.get("statusLine"))
                    if _eh_is_plugin:
                        # Plugin/marketplace installs: the cache hook is supplied by
                        # hooks.json (intentionally absent from settings.json,
                        # so there is no settings.json cache-hook signal to detect
                        # a clobber with. Manage only the statusLine, and act solely
                        # when it is ABSENT. This makes the quality bar default-on for
                        # plugin users without (a) re-running every session once it is
                        # present, (b) re-emitting the foreign-statusline advisory
                        # or (c) overwriting a user's own custom
                        # statusLine. setup_quality_bar skips the redundant settings
                        # hook for plugin installs; here it only ever writes our
                        # statusLine into an otherwise-empty slot.
                        if not has_statusline:
                            setup_quality_bar(quiet=True)
                            # Only announce success if OUR statusLine actually landed.
                            # A concurrent foreign write in the read→write window would
                            # make setup_quality_bar skip silently; never claim an
                            # install we didn't deliver.
                            _eh_sl = (_read_settings_json()[0].get("statusLine") or {}).get("command", "") or ""
                            if _statusline_cmd_is_ours(_eh_sl):
                                print(
                                    "  [Token Optimizer] Quality statusline enabled "
                                    "(context % + quality score). "
                                    "Opt out: measure.py setup-quality-bar --uninstall",
                                    file=sys.stderr,
                                )
                    else:
                        statusline_cmd = (settings.get("statusLine") or {}).get("command", "") or ""
                        statusline_is_ours = _statusline_cmd_is_ours(statusline_cmd)
                        hooks = settings.get("hooks", {}).get("UserPromptSubmit", [])
                        # Recognize the consolidated dispatcher as well as
                        # the legacy standalone hook, else this re-adds the
                        # legacy hook every SessionStart.
                        has_cache_hook = _quality_cache_hook_present(hooks)
                        if has_statusline and not statusline_is_ours and has_cache_hook:
                            print(
                                "  [Token Optimizer] Statusline was replaced (e.g. /statusline). "
                                "Auto-restored. Opt out: measure.py setup-quality-bar --uninstall",
                                file=sys.stderr,
                            )
                            setup_quality_bar(force=True, quiet=True)
                        elif not has_statusline or (has_statusline and not has_cache_hook):
                            setup_quality_bar(quiet=True)
        except Exception as _e:
            print(f"  [Token Optimizer] quality bar setup failed: {_e}", file=sys.stderr)
    # Auto-update check (once per day, script-installed users only).
    # Route through install.sh so script installs update to the latest verified
    # release tag and pass out-of-band checksums instead of pulling moving main.
    try:
        install_dir = RUNTIME_DIR / "token-optimizer"
        update_marker = install_dir / ".last-update-check"
        installer = install_dir / "install.sh"
        if not _lean and (install_dir / ".git").is_dir() and installer.exists():
            should_check = True
            if update_marker.exists():
                age = time.time() - update_marker.stat().st_mtime
                should_check = age > 86400  # Once per day
            if should_check:
                update_log = install_dir / ".last-update.log"
                log_fd = os.open(str(update_log), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    _proc = spawn_detached(
                        ["bash", str(installer)],
                        cwd=str(install_dir),
                        stdout=log_fd, stderr=subprocess.STDOUT,
                    )
                finally:
                    # Popen duplicates the fd via start_new_session/creationflags;
                    # the parent must close its own copy so the fd doesn't leak
                    # across daily checks (EMFILE after ~1024 stale opens).
                    os.close(log_fd)
                if _proc is None:
                    _log_spawn_failure("update installer spawn failed")
                update_marker.touch()
    except Exception:
        pass
    # One-time first-run nudge for marketplace users: Claude Code ships
    # third-party marketplaces with auto-update off by default, and plugin
    # authors cannot change that default. Tell the user how to flip it
    # so they get future bug fixes automatically. Shown exactly once,
    # suppressed if the user has opted out of the quality bar entirely,
    # and skipped for script-install / dev-symlink users (they get their
    # updates via the daily verified installer above or via their local checkout).
    try:
        already_shown = _read_config_flag("autoupdate_nudge_shown", False)
        qb_disabled = _read_config_flag("quality_bar_disabled", False)
        # Claude Code steps only: Codex loads the same plugin build from its
        # own plugin cache, and its users were told to open Claude Code's
        # /plugin menu.
        if (not _lean
                and detect_runtime() == "claude"
                and _is_running_from_plugin_cache()
                and not already_shown
                and not qb_disabled):
            # User-visible onboarding tip. Emitted as a systemMessage so the
            # CC UI shows it to the user without sending it to the model.
            # See the noop-install-failed branch in the daemon block above for
            # the full stdout/stderr/systemMessage rationale.
            print(json.dumps({"systemMessage":
                "  [Token Optimizer] First-run tip: enable auto-update for this marketplace\n"
                "  so you get bug fixes automatically. In Claude Code:\n\n"
                "      /plugin  ->  Marketplaces  ->  alexgreensh-token-optimizer\n"
                "               ->  Enable auto-update"}))
            _write_config_flag("autoupdate_nudge_shown", True)
    except Exception:
        pass


# The dashboard-daemon ensure/revive is DECOUPLED from the SessionStart
# critical path entirely. It is dispatched as a DETACHED, fire-and-forget child
# (never run inline, never under a wall-clock guard), so a new session spends ZERO
# measurable time on install / kickstart / landing verification -- there is no
# chance it can slow a user's session start. The child runs ``daemon-revive``
# (-> _ensure_dashboard_daemon(force=True)) fully off the hot path, exactly like
# the mid-session pulse's revive. launchd KeepAlive+RunAtLoad persists a
# once-installed daemon; the detached child only needs to reinstall a MISSING
# plist / revive a dead daemon, which it does without the session ever waiting on
# it. Fail-open: a spawn failure must never break SessionStart.


def _ensure_health_daemon_revive_first():
    """Dispatch the dashboard-daemon ensure/revive as a DETACHED child.

    The SessionStart path must add no measurable latency, so this NEVER performs
    the install/kickstart/verify work inline. It fires a fire-and-forget
    ``daemon-revive`` child (detached via spawn_detached: start_new_session on
    POSIX / DETACHED_PROCESS+breakaway on Windows, stdio -> DEVNULL) and returns
    immediately. The child reinstalls a missing plist / revives a dead daemon off
    the hot path; launchd then persists a once-installed daemon. The mid-session
    pulse provides a second, session-independent recovery path. Idempotent +
    fail-open: a spawn failure is swallowed, never raised into the hook.
    """
    if _remote_lean_profile():
        return "noop-remote"
    # Frozen flag only (SessionStart hot path); the detached daemon-revive child
    # runs the writers, which apply the full _scheduler_install_refused().
    if _daemon_snapshot_sandboxed():
        return "noop-sandbox"
    try:
        _proc = spawn_detached(
            [_detached_python_exe(), str(MEASURE_PY_PATH), "daemon-revive"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL)
        if _proc is None:
            _log_spawn_failure("SessionStart daemon-revive spawn failed")
    except Exception:
        # Fail-open: a daemon-revive spawn failure must never break SessionStart.
        pass


# The provenance label is untrusted input rendered into the model's context.
# Long enough for "env: CLAUDE_CODE_DISABLE_1M_CONTEXT", short enough that a
# hostile value cannot carry a payload or bloat the window.
_WINDOW_SOURCE_MAX_CHARS = 48


_format_window_size = _measure_compat.bind(_measure_providers['quality_archive']._format_window_size, __name__)


_safe_note_text = _measure_compat.bind(_measure_providers['quality_archive']._safe_note_text, __name__, defaults=("", _WINDOW_SOURCE_MAX_CHARS,))


_format_reduced_window_note = _measure_compat.bind(_measure_providers['quality_archive']._format_reduced_window_note, __name__)


_format_context_fill = _measure_compat.bind(_measure_providers['quality_archive']._format_context_fill, __name__)


_model_window_fill = _measure_compat.bind(_measure_providers['quality_archive']._model_window_fill, __name__)


_format_window_note = _measure_compat.bind(_measure_providers['quality_archive']._format_window_note, __name__)


_verbosity_measured_savings = _measure_compat.bind(_measure_providers['quality_archive']._verbosity_measured_savings, __name__)


_transcript_request_outputs = _measure_compat.bind(_measure_providers['quality_archive']._transcript_request_outputs, __name__)


_calibrate_prior_verbosity_nudges = _measure_compat.bind(_measure_providers['quality_archive']._calibrate_prior_verbosity_nudges, __name__)


_canonical_session_id_for_compare = _measure_compat.bind(_measure_providers['quality_archive']._canonical_session_id_for_compare, __name__)


run_verbosity_steer = _measure_compat.bind(_measure_providers['quality_archive'].run_verbosity_steer, __name__, defaults=(None, True, None,))


_verbosity_nudge_text = _measure_compat.bind(_measure_providers['quality_archive']._verbosity_nudge_text, __name__)


if __name__ == "__main__":
    _measure_providers['cli'].main()
