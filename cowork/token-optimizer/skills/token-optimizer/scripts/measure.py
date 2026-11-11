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
except ImportError:  # Python 3.9/3.10, without runtime pip dependencies
    from _measure._vendor import tomli as tomllib


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

from _measure import _compat as _measure_compat, counted_pricing_v5 as _counted_pricing_v5, transcript_backfill as _transcript_backfill, counted_subagents as _counted_subagents, headline as _headline, structural_report as _structural_report
_measure_providers = _measure_compat.prepare(sys.modules[__name__])
from _measure import counted_cap as _counted_cap, output_reread as _output_reread, transcript_product as _transcript_product  # noqa: E402
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
        "recommendations", "coverage-replay", "file-refetch",
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
    "fable_5_1": {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": 12.5, "cache_write_1h": 20.0}, "fable_5_2": {"input": 8.0, "output": 40.0, "cache_read": 0.2, "cache_write": 10.0, "cache_write_1h": 16.0},
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


_daemon_is_running = _measure_compat.bind(_measure_providers['dashboard']._daemon_is_running, __name__)


_launch_opener = _measure_compat.bind(_measure_providers['dashboard']._launch_opener, __name__)


_open_in_browser = _measure_compat.bind(_measure_providers['dashboard']._open_in_browser, __name__)


_open_dashboard = _measure_compat.bind(_measure_providers['dashboard']._open_dashboard, __name__)


_serve_dashboard = _measure_compat.bind(_measure_providers['dashboard']._serve_dashboard, __name__, defaults=(8080, "127.0.0.1",))


_sanitize_dashboard_paths = _measure_compat.bind(_measure_providers['dashboard']._sanitize_dashboard_paths, __name__)


_codex_state_summary = _measure_compat.bind(_measure_providers['dashboard']._codex_state_summary, __name__)


codex_state_report = _measure_compat.bind(_measure_providers['dashboard'].codex_state_report, __name__, defaults=(False,))


generate_dashboard = _measure_compat.bind(_measure_providers['dashboard'].generate_dashboard, __name__)


_shell_script_path = _measure_compat.bind(_measure_providers['dashboard']._shell_script_path, __name__)


_measure_cli = _measure_compat.bind(_measure_providers['dashboard']._measure_cli, __name__)


_runtime_cli = _measure_compat.bind(_measure_providers['dashboard']._runtime_cli, __name__)


_ENV_PREFIXED_BARE_HINT = re.compile(r"TOKEN_OPTIMIZER_RUNTIME=(\w+) python3 measure\.py")


_hint = _measure_compat.bind(_measure_providers['dashboard']._hint, __name__)


_subagent_cache_cmd = _measure_compat.bind(_measure_providers['dashboard']._subagent_cache_cmd, __name__, defaults=(None,))


_display_path = _measure_compat.bind(_measure_providers['dashboard']._display_path, __name__)


_collect_hook_status_for_dashboard = _measure_compat.bind(_measure_providers['dashboard']._collect_hook_status_for_dashboard, __name__)


_collect_codex_hook_status_for_dashboard = _measure_compat.bind(_measure_providers['dashboard']._collect_codex_hook_status_for_dashboard, __name__)


_collect_copilot_hook_status_for_dashboard = _measure_compat.bind(_measure_providers['dashboard']._collect_copilot_hook_status_for_dashboard, __name__)


_collect_cursor_hook_status_for_dashboard = _measure_compat.bind(_measure_providers['dashboard']._collect_cursor_hook_status_for_dashboard, __name__)


_collect_grok_hook_status_for_dashboard = _measure_compat.bind(_measure_providers['dashboard']._collect_grok_hook_status_for_dashboard, __name__)


_collect_hermes_hook_status_for_dashboard = _measure_compat.bind(_measure_providers['dashboard']._collect_hermes_hook_status_for_dashboard, __name__)


_collect_antigravity_hook_status_for_dashboard = _measure_compat.bind(_measure_providers['dashboard']._collect_antigravity_hook_status_for_dashboard, __name__)


_parse_skill_frontmatter = _measure_compat.bind(_measure_providers['dashboard']._parse_skill_frontmatter, __name__)


_collect_codex_skill_inventory = _measure_compat.bind(_measure_providers['dashboard']._collect_codex_skill_inventory, __name__)


_collect_codex_mcp_inventory = _measure_compat.bind(_measure_providers['dashboard']._collect_codex_mcp_inventory, __name__)


_collect_codex_plugin_inventory = _measure_compat.bind(_measure_providers['dashboard']._collect_codex_plugin_inventory, __name__)


_codex_config_path = _measure_compat.bind(_measure_providers['dashboard']._codex_config_path, __name__)


_safe_codex_config_path = _measure_compat.bind(_measure_providers['dashboard']._safe_codex_config_path, __name__)


_CODEX_CONFIG_LOCK_PATH = runtime_home() / ".codex-config.lock"


_codex_config_lock = _measure_compat.bind(_measure_providers['dashboard']._codex_config_lock, __name__, decorators=(contextmanager,))


_skill_mgmt_lock = _measure_compat.bind(_measure_providers['dashboard']._skill_mgmt_lock, __name__, decorators=(contextmanager,))


_write_codex_config = _measure_compat.bind(_measure_providers['dashboard']._write_codex_config, __name__)


_toml_string = _measure_compat.bind(_measure_providers['dashboard']._toml_string, __name__)


_set_toml_key = _measure_compat.bind(_measure_providers['dashboard']._set_toml_key, __name__)


_iter_toml_array_table_spans = _measure_compat.bind(_measure_providers['dashboard']._iter_toml_array_table_spans, __name__)


_decode_toml_string = _measure_compat.bind(_measure_providers['dashboard']._decode_toml_string, __name__)


_codex_skill_target = _measure_compat.bind(_measure_providers['dashboard']._codex_skill_target, __name__, defaults=(None, None,))


_manage_codex_skill = _measure_compat.bind(_measure_providers['dashboard']._manage_codex_skill, __name__, kwdefaults={'raw_path': None, 'name': None})


_toml_table_header_variants = _measure_compat.bind(_measure_providers['dashboard']._toml_table_header_variants, __name__)


_set_codex_named_table_enabled = _measure_compat.bind(_measure_providers['dashboard']._set_codex_named_table_enabled, __name__)


_manage_codex_mcp = _measure_compat.bind(_measure_providers['dashboard']._manage_codex_mcp, __name__)


_collect_management_data = _measure_compat.bind(_measure_providers['dashboard']._collect_management_data, __name__, defaults=(None, None,))


plugin_cleanup = _measure_compat.bind(_measure_providers['dashboard'].plugin_cleanup, __name__, defaults=(False, False,))


_manage_skill = _measure_compat.bind(_measure_providers['dashboard']._manage_skill, __name__)


_manage_skill_locked = _measure_compat.bind(_measure_providers['dashboard']._manage_skill_locked, __name__)


_manage_mcp = _measure_compat.bind(_measure_providers['dashboard']._manage_mcp, __name__)


_codex_memory_review_data = _measure_compat.bind(_measure_providers['dashboard']._codex_memory_review_data, __name__)


generate_standalone_dashboard = _measure_compat.bind(_measure_providers['dashboard'].generate_standalone_dashboard, __name__, defaults=(None, False, False,))


_acquire_session_end_flush_lock = _measure_compat.bind(_measure_providers['lifecycle']._acquire_session_end_flush_lock, __name__, defaults=(120,))


_release_session_end_flush_lock = _measure_compat.bind(_measure_providers['lifecycle']._release_session_end_flush_lock, __name__)


_session_refresh_due = _measure_compat.bind(_measure_providers['lifecycle']._session_refresh_due, __name__, defaults=(120,))


HOOK_STATUS_FILE = RUNTIME_DIR / "token-optimizer" / "hook-status.json"


_pid_is_running = _measure_compat.bind(_measure_providers['lifecycle']._pid_is_running, __name__)


_win_pid_is_running = _measure_compat.bind(_measure_providers['lifecycle']._win_pid_is_running, __name__)


_record_hook_status = _measure_compat.bind(_measure_providers['lifecycle']._record_hook_status, __name__)


_flush_capture_checkpoint = _measure_compat.bind(_measure_providers['lifecycle']._flush_capture_checkpoint, __name__)


_run_session_end_flush_worker = _measure_compat.bind(_measure_providers['lifecycle']._run_session_end_flush_worker, __name__, defaults=(20,))


_run_post_flush_extensions = _measure_compat.bind(_measure_providers['lifecycle']._run_post_flush_extensions, __name__, defaults=(None, "unknown",))


_defer_session_end_flush = _measure_compat.bind(_measure_providers['lifecycle']._defer_session_end_flush, __name__)


_dispatch_session_end_flush = _measure_compat.bind(_measure_providers['lifecycle']._dispatch_session_end_flush, __name__, defaults=(None,))


_inline_flush_budget_seconds = _measure_compat.bind(_measure_providers['lifecycle']._inline_flush_budget_seconds, __name__, defaults=(8.0, 0.5, None,))


_dispatch_collect = _measure_compat.bind(_measure_providers['lifecycle']._dispatch_collect, __name__)


_spawn_detached_dashboard_selfheal = _measure_compat.bind(_measure_providers['lifecycle']._spawn_detached_dashboard_selfheal, __name__, defaults=(None, False,), kwdefaults={'hook_timeout': True})


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


_dashboard_hook_backoff_active = _measure_compat.bind(_measure_providers['lifecycle']._dashboard_hook_backoff_active, __name__)


_record_dashboard_hook_timeout = _measure_compat.bind(_measure_providers['lifecycle']._record_dashboard_hook_timeout, __name__)


_replace_dashboard_heal_pid = _measure_compat.bind(_measure_providers['lifecycle']._replace_dashboard_heal_pid, __name__)


_dashboard_selfheal_inflight = _measure_compat.bind(_measure_providers['lifecycle']._dashboard_selfheal_inflight, __name__)


_dashboard_selfheal_build_lock = _measure_compat.bind(_measure_providers['lifecycle']._dashboard_selfheal_build_lock, __name__, decorators=(contextmanager,))


_dashboard_heal_spawn_due = _measure_compat.bind(_measure_providers['lifecycle']._dashboard_heal_spawn_due, __name__)


_dispatch_dashboard = _measure_compat.bind(_measure_providers['dashboard']._dispatch_dashboard, __name__)


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
    "test-session-*", "test-failbuild-*", "failwire-*", "unit-c-never*", "torture-*",
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


# Keep-warm pings fail closed until live-session refresh is proven safe.
_KEEPWARM_CONSENT_KEY = "keepwarm_consent"
# Valid sticky consent states. 'unasked' is the implicit default (no key yet).
_KEEPWARM_CONSENT_STATES = ("unasked", "asked", "declined", "enabled")
# Env marker the ping subprocess sets on itself so a keepwarm-tick that runs
# *inside* a ping resume can never recursively arm/ping again.
_KEEPWARM_PING_ENV = "TOKEN_OPTIMIZER_KEEPWARM_PING"

# The legacy allow flag remains a supported explicit opt-out when set false.
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
# Check every minute and throttle launchd to prevent a hot loop.
_KEEPWARM_SCHEDULER_START_INTERVAL = 60
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
_KEEPWARM_5M_SAFETY_MARGIN = 120
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
    "SELECT {day}, " + _counted_cap.reexpand_type("e") + ", SUM(e.tokens_saved), SUM(e.cost_saved_usd), {mu}, {amu}, {mdl} "
    "FROM {table} e "
    "LEFT JOIN session_log s ON e.session_uuid = s.session_uuid "
    "WHERE e.timestamp >= ? {scope}"
    "GROUP BY 1, 2, e.session_uuid, 7",
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
#   * verbosity_* -- output reductions are estimated, not counted removals.
_COUNTED_SE_EVENT_TYPES = (
    "tool_archive", "tool_archive_reexpand", "tool_archive_refetch_block",
    "structure_map", "delta_read", "codemap_outline",
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
_COUNTED_PRICING_VERSION = "9"
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


_counted_rollup_rows = _measure_compat.bind(_measure_providers['savings']._counted_rollup_rows, __name__, defaults=(None, None))


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


_collect_trends_from_db = _measure_compat.bind(_measure_providers['dashboard']._collect_trends_from_db, __name__, defaults=(30,))


_activity_window_predicate = _measure_compat.bind(_measure_providers['dashboard']._activity_window_predicate, __name__)


_query_trends_db = _measure_compat.bind(_measure_providers['dashboard']._query_trends_db, __name__)


_query_dashboard_population_metrics = _measure_compat.bind(_measure_providers['dashboard']._query_dashboard_population_metrics, __name__)


_collect_trends_from_jsonl = _measure_compat.bind(_measure_providers['dashboard']._collect_trends_from_jsonl, __name__, defaults=(30,))


_collect_git_commits = _measure_compat.bind(_measure_providers['dashboard']._collect_git_commits, __name__, defaults=(30,))


_collect_trends_data = _measure_compat.bind(_measure_providers['dashboard']._collect_trends_data, __name__, defaults=(30,))


_build_ttl_period_summary = _measure_compat.bind(_measure_providers['dashboard']._build_ttl_period_summary, __name__)


usage_trends = _measure_compat.bind(_measure_providers['dashboard'].usage_trends, __name__, defaults=(30, False,))


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


_is_plugin_installed = _measure_compat.bind(_measure_providers['lifecycle']._is_plugin_installed, __name__)


_is_compaction_runner = _measure_compat.bind(_measure_providers['lifecycle']._is_compaction_runner, __name__)


_is_hook_installed = _measure_compat.bind(_measure_providers['lifecycle']._is_hook_installed, __name__, defaults=(None, False,))


_sessionend_cmd_is_flush = _measure_compat.bind(_measure_providers['lifecycle']._sessionend_cmd_is_flush, __name__)


_sessionend_cmd_is_collect_fossil = _measure_compat.bind(_measure_providers['lifecycle']._sessionend_cmd_is_collect_fossil, __name__)


_is_hook_current = _measure_compat.bind(_measure_providers['lifecycle']._is_hook_current, __name__, defaults=(None,))


check_hook = _measure_compat.bind(_measure_providers['lifecycle'].check_hook, __name__)


_SETTINGS_LOCK_PATH = SETTINGS_PATH.parent / ".settings.lock"


_settings_lock = _measure_compat.bind(_measure_providers['lifecycle']._settings_lock, __name__, defaults=(False,), decorators=(contextmanager,))


_settings_write_guard = _measure_compat.bind(_measure_providers['lifecycle']._settings_write_guard, __name__, defaults=(None, None,))


_report_settings_write_refusal = _measure_compat.bind(_measure_providers['lifecycle']._report_settings_write_refusal, __name__)


_log_settings_lease_denied = _measure_compat.bind(_measure_providers['lifecycle']._log_settings_lease_denied, __name__)


# Why a write was held back because settings.json changed under it.
_SETTINGS_CHANGED_REFUSAL = (
    "settings.json changed while Token Optimizer was writing it (another "
    "program saved it twice in a row); nothing was written, your latest edit is kept"
)
_SETTINGS_WRITE_TRIES = 2


_settings_file_identity = _measure_compat.bind(_measure_providers['lifecycle']._settings_file_identity, __name__, defaults=(None,))


_settings_write_refusal_reason = _measure_compat.bind(_measure_providers['lifecycle']._settings_write_refusal_reason, __name__)


_existing_line_ending = _measure_compat.bind(_measure_providers['lifecycle']._existing_line_ending, __name__)


_note_settings_os_error = _measure_compat.bind(_measure_providers['lifecycle']._note_settings_os_error, __name__)


_write_settings_atomic_locked = _measure_compat.bind(_measure_providers['lifecycle']._write_settings_atomic_locked, __name__, defaults=(None, True, None,))


_merge_concurrent_settings = _measure_compat.bind(_measure_providers['lifecycle']._merge_concurrent_settings, __name__, defaults=(None,))


_write_settings_atomic = _measure_compat.bind(_measure_providers['lifecycle']._write_settings_atomic, __name__, defaults=(None, False,))


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


_autocompact_pct_override_value = _measure_compat.bind(_measure_providers['lifecycle']._autocompact_pct_override_value, __name__, defaults=(None,))


_autocompact_pct_override_explanation = _measure_compat.bind(_measure_providers['lifecycle']._autocompact_pct_override_explanation, __name__, defaults=(None,))


_is_token_optimizer_session_end_hook = _measure_compat.bind(_measure_providers['lifecycle']._is_token_optimizer_session_end_hook, __name__)


_remove_token_optimizer_session_end_hooks = _measure_compat.bind(_measure_providers['lifecycle']._remove_token_optimizer_session_end_hooks, __name__)


_sessionend_has_current_flush = _measure_compat.bind(_measure_providers['lifecycle']._sessionend_has_current_flush, __name__)


_rewrite_collect_fossil_hook = _measure_compat.bind(_measure_providers['lifecycle']._rewrite_collect_fossil_hook, __name__)


_apply_sessionend_fossil_reconcile = _measure_compat.bind(_measure_providers['lifecycle']._apply_sessionend_fossil_reconcile, __name__)


_reconcile_sessionend_fossils = _measure_compat.bind(_measure_providers['lifecycle']._reconcile_sessionend_fossils, __name__)


_maybe_self_heal_sessionend_fossils_on_hook = _measure_compat.bind(_measure_providers['lifecycle']._maybe_self_heal_sessionend_fossils_on_hook, __name__)


setup_hook = _measure_compat.bind(_measure_providers['lifecycle'].setup_hook, __name__, defaults=(False, False, False,))


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


_find_plugin_hooks_json = _measure_compat.bind(_measure_providers['lifecycle']._find_plugin_hooks_json, __name__)


_resolve_hook_command = _measure_compat.bind(_measure_providers['lifecycle']._resolve_hook_command, __name__)


_windows_hook_command_is_stale = _measure_compat.bind(_measure_providers['lifecycle']._windows_hook_command_is_stale, __name__)


# Flags that don't distinguish functionally different hooks (output control only).
# These are stripped when computing hook identity for dedup.
_COSMETIC_FLAGS = frozenset({"--quiet", "-q", "--warn", "--verbose", "-v"})

# Shell noise tokens stripped during subshell identity extraction.
_SHELL_NOISE = frozenset({
    "2>/dev/null", "||", "true", "&&", "|", "sort",
    "tail", "-V", "-1", "-n", "head",
})


_hook_command_identity = _measure_compat.bind(_measure_providers['lifecycle']._hook_command_identity, __name__)


_cleanup_duplicate_plugin_hooks_from_settings = _measure_compat.bind(_measure_providers['lifecycle']._cleanup_duplicate_plugin_hooks_from_settings, __name__, defaults=(False,))


# Hook commands written by the Claude Code cloud kit (cloud_kit.py, run by the
# npm package's install-cloud.sh) all run this runner.
_CLOUD_KIT_RUNNER_MARKER = "/token-optimizer-cloud-run.sh"


_cloud_kit_owns_hooks = _measure_compat.bind(_measure_providers['lifecycle']._cloud_kit_owns_hooks, __name__)


setup_all_hooks = _measure_compat.bind(_measure_providers['lifecycle'].setup_all_hooks, __name__, defaults=(False, False,))


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

# ResourceHealth combines fill and waste with a bounded continuity prior.
# SessionEfficiency remains a separate diagnostic; versions are not compared.
_QUALITY_SCORE_VERSION = 3

_RESOURCE_HEALTH_WEIGHTS = {
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

_is_running_from_plugin_cache = _measure_compat.bind(_measure_providers['lifecycle']._is_running_from_plugin_cache, __name__)


_is_running_from_synced_plugin = _measure_compat.bind(_measure_providers['lifecycle']._is_running_from_synced_plugin, __name__)


_get_measure_py_path = _measure_compat.bind(_measure_providers['lifecycle']._get_measure_py_path, __name__)


_read_settings_json = _measure_compat.bind(_measure_providers['lifecycle']._read_settings_json, __name__)


_is_regular_file = _measure_compat.bind(_measure_providers['lifecycle']._is_regular_file, __name__)


_read_settings_json_checked = _measure_compat.bind(_measure_providers['lifecycle']._read_settings_json_checked, __name__)


_read_settings_for_write = _measure_compat.bind(_measure_providers['lifecycle']._read_settings_for_write, __name__, defaults=(False,))


_smart_compact_hook_commands = _measure_compat.bind(_measure_providers['lifecycle']._smart_compact_hook_commands, __name__)


_settings_hook_python_and_script = _measure_compat.bind(_measure_providers['lifecycle']._settings_hook_python_and_script, __name__)


_is_smart_compact_installed = _measure_compat.bind(_measure_providers['lifecycle']._is_smart_compact_installed, __name__, defaults=(None,))


setup_smart_compact = _measure_compat.bind(_measure_providers['lifecycle'].setup_smart_compact, __name__, defaults=(False, False, False, False,))


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


_stable_marketplace_script_path = _measure_compat.bind(_measure_providers['lifecycle']._stable_marketplace_script_path, __name__)


_get_statusline_path = _measure_compat.bind(_measure_providers['lifecycle']._get_statusline_path, __name__)


_fix_stale_settings_paths = _measure_compat.bind(_measure_providers['lifecycle']._fix_stale_settings_paths, __name__)


_migrate_statusline_to_stable_path = _measure_compat.bind(_measure_providers['lifecycle']._migrate_statusline_to_stable_path, __name__)


_STATUSLINE_JS_IN_CMD_RE = re.compile(r"""(['"])([^'"]*statusline\.js)\1|([^\s'"]*statusline\.js)""")


_statusline_js_in_command = _measure_compat.bind(_measure_providers['lifecycle']._statusline_js_in_command, __name__)


_statusline_cmd_is_ours = _measure_compat.bind(_measure_providers['lifecycle']._statusline_cmd_is_ours, __name__)


_path_is_under = _measure_compat.bind(_measure_providers['lifecycle']._path_is_under, __name__)


_heal_dangling_statusline = _measure_compat.bind(_measure_providers['lifecycle']._heal_dangling_statusline, __name__)


_heal_keepwarm_plist_path = _measure_compat.bind(_measure_providers['lifecycle']._heal_keepwarm_plist_path, __name__)


# Known TO script names used to identify token-optimizer hooks.
_TO_SCRIPT_NAMES = frozenset({
    "measure.py", "read_cache.py", "bash_hook.py",
    "archive_result.py", "context_intel.py",
})


_fix_malformed_hook_commands = _measure_compat.bind(_measure_providers['lifecycle']._fix_malformed_hook_commands, __name__)


_is_malformed_to_hook = _measure_compat.bind(_measure_providers['lifecycle']._is_malformed_to_hook, __name__)


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


_command_drives_quality_cache = _measure_compat.bind(_measure_providers['lifecycle']._command_drives_quality_cache, __name__)


_quality_cache_hook_present = _measure_compat.bind(_measure_providers['lifecycle']._quality_cache_hook_present, __name__)


_is_quality_bar_installed = _measure_compat.bind(_measure_providers['lifecycle']._is_quality_bar_installed, __name__, defaults=(None,))


_set_quality_bar_disabled = _measure_compat.bind(_measure_providers['lifecycle']._set_quality_bar_disabled, __name__)


_set_daemon_disabled = _measure_compat.bind(_measure_providers['lifecycle']._set_daemon_disabled, __name__)


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


_set_vscode_ext_disabled = _measure_compat.bind(_measure_providers['lifecycle']._set_vscode_ext_disabled, __name__)


_detect_vscode_family_cli = _measure_compat.bind(_measure_providers['lifecycle']._detect_vscode_family_cli, __name__)


_ensure_vscode_extension = _measure_compat.bind(_measure_providers['lifecycle']._ensure_vscode_extension, __name__)


_read_config_flag = _measure_compat.bind(_measure_providers['lifecycle']._read_config_flag, __name__, defaults=(False,))


_CONFIG_LOCK_PATH = CONFIG_DIR / ".config.lock"


_config_lock = _measure_compat.bind(_measure_providers['lifecycle']._config_lock, __name__, decorators=(contextmanager,))


_write_config_flag = _measure_compat.bind(_measure_providers['lifecycle']._write_config_flag, __name__)


# ---------------------------------------------------------------------------
# v5 Active Compression feature flags
# ---------------------------------------------------------------------------
# These are the single source of truth for whether each v5 feature is active.
# Read order: env var (highest priority) > config.json > default.
# This lets power users override via env, but persistent state lives in config.json
# so dashboard toggles work without touching shell profiles.

V5_FEATURES = {**__import__('_measure.keepwarm_economics', fromlist=['feature_settings']).feature_settings(),
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
_settings_env_paths = _measure_compat.bind(_measure_providers['lifecycle']._settings_env_paths, __name__)


# Parsed settings files, memoized by mtime. The detection paths read the same
# <=4 files dozens of times per dashboard run; without this each call re-opened
# and re-parsed them. Keying on st_mtime_ns keeps it correct in
# a long-lived daemon: an edited file is re-read, an unchanged one is not.
_settings_file_cache = {}


_load_settings_file = _measure_compat.bind(_measure_providers['lifecycle']._load_settings_file, __name__)


_settings_env_value = _measure_compat.bind(_measure_providers['lifecycle']._settings_env_value, __name__)


_resolve_feature_env = _measure_compat.bind(_measure_providers['lifecycle']._resolve_feature_env, __name__)


_model_routing_handled_elsewhere = _measure_compat.bind(_measure_providers['lifecycle']._model_routing_handled_elsewhere, __name__)


_resolve_settings_value = _measure_compat.bind(_measure_providers['lifecycle']._resolve_settings_value, __name__, defaults=(None,))


_env_val_enables = _measure_compat.bind(_measure_providers['lifecycle']._env_val_enables, __name__)


_is_v5_feature_enabled = _measure_compat.bind(_measure_providers['lifecycle']._is_v5_feature_enabled, __name__)


_set_v5_feature = _measure_compat.bind(_measure_providers['lifecycle']._set_v5_feature, __name__)


_show_v5_welcome = _measure_compat.bind(_measure_providers['lifecycle']._show_v5_welcome, __name__)


_get_v5_feature_status = _measure_compat.bind(_measure_providers['lifecycle']._get_v5_feature_status, __name__)


_get_v5_savings_recommendation = _measure_compat.bind(_measure_providers['lifecycle']._get_v5_savings_recommendation, __name__)


setup_quality_bar = _measure_compat.bind(_measure_providers['lifecycle'].setup_quality_bar, __name__, defaults=(False, False, False, False, False, False,))


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
    "tool_archive": "Tool replacements", "bash_archive_reexpand": "Bash output re-expanded",
    "structure_map": "Structure maps", "codemap_outline": "Code map re-read outlines", "codemap_lookup": "Code map lookups [est]",
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
    "verbosity_steer_measured": "Concise output [est]",
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
    "context_advisory_shadow", "context_size_advisory", "post_run_classify_shadow",
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
_V5_COMPRESSION_CATEGORIES = frozenset(_V5_COMPRESSION_LABELS.keys()) - {"quality_nudge"}


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
    "SELECT " + _counted_cap.reexpand_type() + ", COALESCE(SUM(tokens_saved),0) FROM {table} "
    "WHERE timestamp >= ? AND event_type != 'verbosity_steer' GROUP BY 1")
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
     "checkpoint_restore", "codemap_lookup"})


_runway_saved_tokens = _measure_compat.bind(_measure_providers['savings']._runway_saved_tokens, __name__, defaults=(False,))


runway_snapshot = _measure_compat.bind(_measure_providers['savings'].runway_snapshot, __name__, defaults=(30, None, None))


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


_action_savings_display = _measure_compat.bind(_measure_providers['savings']._action_savings_display, __name__, defaults=(False, 0.0))


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


_status_bar_dir = _measure_compat.bind(_measure_providers['dashboard']._status_bar_dir, __name__)


_status_bar_key = _measure_compat.bind(_measure_providers['dashboard']._status_bar_key, __name__)


_status_bar_cache_path = _measure_compat.bind(_measure_providers['dashboard']._status_bar_cache_path, __name__)


_status_bar_lock_path = _measure_compat.bind(_measure_providers['dashboard']._status_bar_lock_path, __name__)


_BUCKET_DAY_SQL = {True: "substr(timestamp, 1, 10)", False: "''"}
_BUCKET_SESSION_SQL = {
    True: "AND (session_uuid = ? OR (session_uuid IS NULL AND session_id = ?)) ", False: ""}
_BUCKET_SAVINGS_SQL = _sql_variants(
    "SELECT {day}, " + _counted_cap.reexpand_type() + ", COUNT(*), SUM(tokens_saved), "
    "SUM(cost_saved_usd) FROM {table} WHERE timestamp >= ? {scope}"
    "GROUP BY 1, 2 ORDER BY 4 DESC",
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


_realized_savings_buckets = _measure_compat.bind(_measure_providers['dashboard']._realized_savings_buckets, __name__, defaults=(None, False,))


_open_trends_db_readonly = _measure_compat.bind(_measure_providers['dashboard']._open_trends_db_readonly, __name__, defaults=(2.0,))


_dashboard_saved_tokens = _measure_compat.bind(_measure_providers['dashboard']._dashboard_saved_tokens, __name__)


_status_bar_savings_or_reason = _measure_compat.bind(_measure_providers['dashboard']._status_bar_savings_or_reason, __name__, defaults=(False,))


_status_bar_write_cache = _measure_compat.bind(_measure_providers['dashboard']._status_bar_write_cache, __name__)


_status_bar_read_cache = _measure_compat.bind(_measure_providers['dashboard']._status_bar_read_cache, __name__)


_status_bar_start_refresh = _measure_compat.bind(_measure_providers['dashboard']._status_bar_start_refresh, __name__)


_status_bar_acquire_lock = _measure_compat.bind(_measure_providers['dashboard']._status_bar_acquire_lock, __name__)


_status_bar_release_lock = _measure_compat.bind(_measure_providers['dashboard']._status_bar_release_lock, __name__)


_status_bar_put_back = _measure_compat.bind(_measure_providers['dashboard']._status_bar_put_back, __name__)


# Prefilter only: the parsed row decides. Matching the bare word keeps rows from
# writers that space their JSON ("subtype": "compact_boundary") in the count.
_COMPACT_MARK = b"compact_boundary"


_status_bar_compactions = _measure_compat.bind(_measure_providers['dashboard']._status_bar_compactions, __name__, defaults=(None,))


_status_bar_transcript_state = _measure_compat.bind(_measure_providers['dashboard']._status_bar_transcript_state, __name__)


_status_bar_quality_cache_dirs = _measure_compat.bind(_measure_providers['dashboard']._status_bar_quality_cache_dirs, __name__)


_status_bar_freshest = _measure_compat.bind(_measure_providers['dashboard']._status_bar_freshest, __name__)


_status_bar_checkpoint_epoch = _measure_compat.bind(_measure_providers['dashboard']._status_bar_checkpoint_epoch, __name__)


_status_bar_cached_checkpoint_epoch = _measure_compat.bind(_measure_providers['dashboard']._status_bar_cached_checkpoint_epoch, __name__)


_status_bar_earlier_checkpoint = _measure_compat.bind(_measure_providers['dashboard']._status_bar_earlier_checkpoint, __name__)


_PROMPT_CACHE_SIDECAR_MAX_AGE_S = 600


_status_bar_prompt_cache = _measure_compat.bind(_measure_providers['dashboard']._status_bar_prompt_cache, __name__)


status_bar_payload = _measure_compat.bind(_measure_providers['dashboard'].status_bar_payload, __name__, defaults=(None, False,))


_status_bar_cli = _measure_compat.bind(_measure_providers['dashboard']._status_bar_cli, __name__)


_status_bar_finite = _measure_compat.bind(_measure_providers['dashboard']._status_bar_finite, __name__)


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


_settings_heal_dir = _measure_compat.bind(_measure_providers['lifecycle']._settings_heal_dir, __name__)


_settings_heal_ensure_dir = _measure_compat.bind(_measure_providers['lifecycle']._settings_heal_ensure_dir, __name__)


_settings_write_file_secure = _measure_compat.bind(_measure_providers['lifecycle']._settings_write_file_secure, __name__)


_settings_is_wipe_residue = _measure_compat.bind(_measure_providers['lifecycle']._settings_is_wipe_residue, __name__)


_settings_has_user_keys = _measure_compat.bind(_measure_providers['lifecycle']._settings_has_user_keys, __name__)


_settings_read_highwater = _measure_compat.bind(_measure_providers['lifecycle']._settings_read_highwater, __name__)


_settings_update_highwater = _measure_compat.bind(_measure_providers['lifecycle']._settings_update_highwater, __name__)


_settings_read_tombstones = _measure_compat.bind(_measure_providers['lifecycle']._settings_read_tombstones, __name__)


_settings_write_tombstone = _measure_compat.bind(_measure_providers['lifecycle']._settings_write_tombstone, __name__)


_settings_heal_log = _measure_compat.bind(_measure_providers['lifecycle']._settings_heal_log, __name__)


_settings_check_tombstones = _measure_compat.bind(_measure_providers['lifecycle']._settings_check_tombstones, __name__)


_settings_read_restored_keys = _measure_compat.bind(_measure_providers['lifecycle']._settings_read_restored_keys, __name__)


_settings_write_atomic_silent = _measure_compat.bind(_measure_providers['lifecycle']._settings_write_atomic_silent, __name__, defaults=(None,))


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


_settings_reconstruct_extra_known_marketplaces = _measure_compat.bind(_measure_providers['lifecycle']._settings_reconstruct_extra_known_marketplaces, __name__)


_settings_scan_transcripts_for_plugin_evidence = _measure_compat.bind(_measure_providers['lifecycle']._settings_scan_transcripts_for_plugin_evidence, __name__, defaults=(14, 3.0,))


_settings_reconstruct_enabled_plugins = _measure_compat.bind(_measure_providers['lifecycle']._settings_reconstruct_enabled_plugins, __name__)


# Keys that have an authoritative on-disk source for reconstruction.
# For these keys, the snapshot is NEVER used -- we always rebuild from disk.
_SETTINGS_AUTHORITATIVE_RECONSTRUCTORS = {
    "extraKnownMarketplaces": _settings_reconstruct_extra_known_marketplaces,
    "enabledPlugins": _settings_reconstruct_enabled_plugins,
}


_settings_validate_value = _measure_compat.bind(_measure_providers['lifecycle']._settings_validate_value, __name__)


_settings_resolve_restore_value = _measure_compat.bind(_measure_providers['lifecycle']._settings_resolve_restore_value, __name__)


_maybe_self_heal_settings = _measure_compat.bind(_measure_providers['lifecycle']._maybe_self_heal_settings, __name__)


run_ensure_health = _measure_compat.bind(_measure_providers['lifecycle'].run_ensure_health, __name__)


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


_ensure_health_daemon_revive_first = _measure_compat.bind(_measure_providers['lifecycle']._ensure_health_daemon_revive_first, __name__)


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
