#!/usr/bin/env python3
"""Token Optimizer - PostToolUse Archive Result (standalone entry point).

Archives large tool results to disk so they survive compaction.
Standalone extraction for minimal startup overhead (~40ms vs ~135ms).

Security hardening:
  - 0o600 permissions on all written files
  - stdin capped at 1MB
  - Archive entries capped at 5MB with truncation marker
  - Session ID sanitized against path traversal
  - tool_use_id mapped to a safe archive key; unsafe ids hash, never traverse

SOURCE OF TRUTH for _sanitize_session_id: session_store.py.
SOURCE OF TRUTH for read_stdin_hook_input: hook_io.py.
"""

from __future__ import annotations

import fnmatch
import heapq
import io
import itertools
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import hashlib
import time

try:
    from credential_patterns import redact_credentials as _redact_creds_shared
    from credential_patterns import RedactionConfigError, pop_redaction_warning
except ImportError:
    _redact_creds_shared = None
    RedactionConfigError = None
    pop_redaction_warning = None
from bash_compress import _TOKEN_PATTERNS
from hook_io import read_stdin_hook_input
from hook_runtime import LeaseLock
from plugin_env import resolve_snapshot_dir, snapshot_dir_candidates
from refetch_fingerprint import ARGS_HASH_KEY, expand_command, is_live_state_tool, tool_fingerprint
from recovery_output import is_recovery_output
from runtime_env import detect_runtime, settings_env_value
from session_store import SessionStore, _sanitize_session_id as sanitize_sid

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHARS_PER_TOKEN = 4.0  # legacy proxy; retained only for the conservative MCP-cap proximity threshold
try:
    # Calibrated chars/token (~3.3 for code) — shared ratio basis for savings
    # estimates so tool_archive numbers match the rest of the pipeline.
    from token_estimate import CODE_CHARS_PER_TOKEN
except Exception:  # pragma: no cover - fail-open if shared estimator missing
    CODE_CHARS_PER_TOKEN = 3.3
_ARCHIVE_THRESHOLD = 4096       # chars: only archive results >= this size
_ARCHIVE_PREVIEW_SIZE = 1500    # chars: preview included in replacement output
_ARCHIVE_MAX_SIZE = 5_242_880   # 5MB: truncate responses beyond this
# Read this far past the cap when redacting, so a secret that straddles the cap
# is whole when the redactor sees it (the cut is applied after redaction).
_ARCHIVE_CAP_REDACT_OVERSHOOT = 4096
_STDIN_MAX_BYTES = _ARCHIVE_MAX_SIZE + 262_144  # 5MB response plus JSON overhead
_ARCHIVE_RETENTION_HOURS_DEFAULT = 24
_ARCHIVE_RETENTION_MAX_FILES_DEFAULT = 1000
_ARCHIVE_RETENTION_MAX_BYTES_DEFAULT = 104_857_600  # 100 MiB
_ARCHIVE_ACTIVE_GRACE_SECONDS_DEFAULT = 86_400
_ARCHIVE_CLEANUP_INTERVAL_SECONDS_DEFAULT = 60

# Best-effort sweep staleness threshold for orphan lease artifacts in
# ``archive_root/.locks/``. ``HookDeadline._watch`` calls ``os._exit(0)``, which
# bypasses ``LeaseLock.release()`` (the only unlinker of the candidate hard
# link), so ``.{sid}.lease.candidate-{nonce}`` files leak. Archive lock sites
# use ``LeaseLock(..., acquire_timeout=0)`` with NO deadline, so the max lease a
# candidate can represent is the default ``lease_seconds + reclaim_grace``
# (10.0 + 0.25 = 10.25s). The sweep reaps candidates older than this; a younger
# candidate is a live acquisition in flight and is never touched.
LOCK_SWEEP_STALE_SECONDS = 10.25
# A reclaim claim is swept on a far longer fuse than a lease candidate, and the
# asymmetry is deliberate. Reaping a live CANDIDATE is harmless: the acquirer
# simply fails to acquire and fails open. Reaping a live RECLAIM claim is not --
# the claim name is deterministic per generation precisely so that concurrent
# same-generation reclaimers collide and exactly one wins. Free that name while
# a reclaimer is still mid-sequence and a second reclaimer can take it, so two
# processes can end up believing they hold the lock. A reclaimer paused past ten
# seconds is plausible (suspended process, slow NFS, debugger); paused past five
# minutes is not, and the orphaned-claim deadlock this cures is bounded and rare.
RECLAIM_SWEEP_STALE_SECONDS = 300.0

# WS4: Agent/Task result progressive disclosure — shipped MEASURE-ONLY.
#
# A head+tail+pointer treatment WOULD save ~74% of the agent-result pool, but the
# 2026-06-11 history backfill measured a 39.1% harm proxy: in 39% of cases the
# parent's NEXT assistant turn quotes (>=20-char verbatim) text from the
# would-be-elided MIDDLE. Sub-agents put their findings in the middle (head =
# preamble, tail = usage/continuation block), so an active replacement would lose
# content the parent needs. Per the configured exception rule the DATA decides: 39.1%
# >> the 15% gate, so this ships measure-only. We log the would-be saving as an
# OPPORTUNITY-tier event (never the realized headline) so the dashboard shows the
# pool + the harm number; the full result is archived for `expand` either way.
# Re-evaluate active only if a middle-preserving treatment lands a sub-15% proxy.
_AGENT_RESULT_MIN_CHARS = 8 * 1024   # only measure sub-agent results > 8KB
_AGENT_RESULT_HEAD_CHARS = 2000
_AGENT_RESULT_TAIL_CHARS = 2000
_AGENT_RESULT_TOOL_NAMES = frozenset({"Task", "Agent"})
_FEATURE_AGENT_RESULT = "agent_result_skeleton"
_AGENT_RESULT_HARM_PCT = 39.1        # from the 2026-06-11 backfill (surfaced)

# Plugin-data-aware paths (env > installed_plugins.json > legacy)
SNAPSHOT_DIR = resolve_snapshot_dir()
TRENDS_DB = SNAPSHOT_DIR / "trends.db"
_SAVINGS_DB_TIMEOUT_SECONDS = 0.05
_SAVINGS_DB_BUSY_TIMEOUT_MS = 50
_DEFAULT_SAVINGS_COST_PER_MTOK = 3.0  # Sonnet input rate; safe fallback for hook-only pricing.
_HOOK_INPUT_COST_PER_MTOK = {
    "gpt-6-astra": 10.0,
    "gpt-5.6-sol": 5.0,
    "gpt-5.6-terra": 2.0,
    "gpt-5.6-luna": 0.20,
    "gpt-5.6": 5.0,
    "gpt-5.5-pro": 30.0,
    "gpt-5.1-codex-mini": 0.25,
    "gpt-4o-mini": 0.15,
    "gpt-5-codex": 1.25,
    "gpt-5.1-codex": 1.25,
    "gpt-5.2-codex": 1.75,
    "gpt-5.3-codex": 1.75,
    "gpt-5-mini": 0.25,
    "gpt-5-nano": 0.05,
    "gpt-5": 1.25,
    "gpt-5.4": 2.5,
    "gpt-5.5": 5.0,
    "gpt-4o": 2.5,
}

# gpt-5.6-sol promotional pricing (date-gated). OpenAI documents the $4/$20 rate
# as "available at least through November 21, 2026." _HOOK_INPUT_COST_PER_MTOK
# holds the STANDARD input rate ($5); while the promo window is open we swap the
# promo rate in so hook savings dollars stay accurate today AND flip back
# automatically after 2026-11-21. Mirrors measure.py _apply_gpt56_sol_promo_pricing.
_GPT56_SOL_PROMO_UNTIL = datetime(2026, 11, 21, tzinfo=timezone.utc)


def _apply_gpt56_sol_promo_pricing(as_of=None):
    """Swap the gpt-5.6-sol hook input rate to the promotional rate while active (idempotent)."""
    override = os.environ.get("TOKEN_OPTIMIZER_PRICING_AS_OF")
    if as_of is None and override:
        try:
            as_of = datetime.strptime(override, "%Y-%m-%d")
        except ValueError:
            as_of = None
    d = as_of or datetime.now(timezone.utc)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    if d < _GPT56_SOL_PROMO_UNTIL:
        _HOOK_INPUT_COST_PER_MTOK["gpt-5.6-sol"] = 4.0
        _HOOK_INPUT_COST_PER_MTOK["gpt-5.6"] = 4.0
        return True
    _HOOK_INPUT_COST_PER_MTOK["gpt-5.6-sol"] = 5.0
    _HOOK_INPUT_COST_PER_MTOK["gpt-5.6"] = 5.0
    return False


_apply_gpt56_sol_promo_pricing()

_SAVINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS savings_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    tokens_saved INTEGER NOT NULL DEFAULT 0,
    cost_saved_usd REAL NOT NULL DEFAULT 0.0,
    session_id TEXT,
    detail TEXT
);
"""


def _chmod_private_file(path: Path) -> None:
    try:
        if path.exists() and not path.is_symlink():
            os.chmod(str(path), 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Helpers (SOURCE OF TRUTH: measure.py — keep in sync)
# ---------------------------------------------------------------------------

def _sanitize_session_id(sid: str | None) -> str:
    return sanitize_sid(sid or "")


# \Z, not $: "$" also matches before a trailing newline ("abc\n").
_ARCHIVE_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]+\Z")


def _safe_archive_key(tool_use_id: str) -> str:
    """Filesystem-safe archive key for a tool_use_id.

    Ids outside [a-zA-Z0-9_-] (+, space, /, ., ') map to a deterministic digest
    instead of being skipped — the archive still lands, expand's own
    [a-zA-Z0-9_-]+ key validation accepts the digest, and no raw id ever
    reaches a path, so traversal is impossible by construction.
    """
    if _ARCHIVE_KEY_RE.match(tool_use_id):
        return tool_use_id
    digest = hashlib.sha256(
        tool_use_id.encode("utf-8", errors="replace")).hexdigest()[:24]
    return f"h{digest}"




def _archive_dir_for_session(session_id: str) -> Path:
    """Return the archive directory for a given session."""
    sid = _sanitize_session_id(session_id)
    return SNAPSHOT_DIR / "tool-archive" / sid


def _redact_credentials(text: str) -> str:
    """Replace credential-matching substrings before archiving.

    Prefers credential_patterns.redact_credentials (labeled placeholders like
    [CREDENTIAL REDACTED: AWS access key]). Falls back to _TOKEN_PATTERNS
    from bash_compress with generic [REDACTED] if the shared module is
    unavailable.
    """
    if _redact_creds_shared is not None:
        return _redact_creds_shared(text)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def _int_env(name: str, default: int) -> int:
    """Read a non-negative integer without adding a heavy config import."""
    try:
        value = int(os.environ.get(name, str(default)))
        return value if value >= 0 else default
    except (TypeError, ValueError):
        return default


def _archive_tree_usage(session_dir: Path) -> tuple[int, int]:
    """Return (regular-file count, bytes) without following symlinks."""
    files = 0
    total_bytes = 0
    for root, dirnames, filenames in os.walk(session_dir, followlinks=False):
        root_path = Path(root)
        dirnames[:] = [
            name for name in dirnames
            if not (root_path / name).is_symlink()
        ]
        for name in filenames:
            path = root_path / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                files += 1
                total_bytes += os.lstat(path).st_size
            except OSError:
                continue
    return files, total_bytes


def _archive_roots() -> list[Path]:
    """Return every safe archive root, active identity first."""
    roots: list[Path] = []
    seen: set[str] = set()
    for snapshot_dir in snapshot_dir_candidates():
        archive_root = snapshot_dir / "tool-archive"
        try:
            key = os.path.normcase(str(archive_root.resolve(strict=False)))
        except (OSError, ValueError):
            continue
        if key not in seen:
            roots.append(archive_root)
            seen.add(key)
    return roots


def _session_lock_path(archive_root: Path, session_id: str) -> Path:
    return archive_root / ".locks" / f"{_sanitize_session_id(session_id)}.lease"


def _sweep_stale_lock_candidates(archive_root: Path) -> None:
    """Best-effort: unlink orphan ``.{sid}.lease.candidate-{nonce}`` and
    ``.{sid}.lease.reclaim-{digest}`` artifacts left in
    ``archive_root/.locks/`` when ``os._exit`` bypasses ``LeaseLock.release``
    or the ``finally`` that unlinks a reclaim claim.

    ``cleanup_old_archives`` skips dot-dirs as *session* candidates (the
    ``.locks`` directory is never pruned as a session); this is a separate,
    bounded scan of its contents. Only artifacts whose ``st_mtime`` is older
    than ``LOCK_SWEEP_STALE_SECONDS`` (the max lease duration for archive locks)
    are reaped, so a live acquisition or in-flight reclaimer is never broken.

    The reclaim claim name is **deterministic per generation**
    (``sha256("owner:{nonce}")[:32]``); a reclaimer killed between ``os.link``
    and the ``finally`` unlink orphans a claim that, because the name is
    deterministic, permanently deadlocks every future same-generation
    reclaimer (``FileExistsError`` -> fail open forever). Reaping the stale
    claim here bounds that deadlock from *permanent* to *until the next
    cleanup pass* WITHOUT touching the deterministic claim name in
    ``hook_runtime._reclaim_path`` (the determinism is load-bearing
    serialization: it is what makes concurrent same-generation reclaimers
    exactly-one-winner). ``OSError`` is swallowed per entry so a locked or
    vanishing file never aborts the hook.
    """
    locks_dir = archive_root / ".locks"
    try:
        entries = list(locks_dir.iterdir())
    except (OSError, ValueError):
        return
    now = time.time()
    candidate_cutoff = now - LOCK_SWEEP_STALE_SECONDS
    reclaim_cutoff = now - RECLAIM_SWEEP_STALE_SECONDS
    for entry in entries:
        if ".lease.candidate-" in entry.name:
            cutoff = candidate_cutoff
        elif ".lease.reclaim-" in entry.name:
            cutoff = reclaim_cutoff
        else:
            continue
        try:
            if entry.is_dir():
                continue
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            pass


def _mark_session_active(session_dir: Path) -> None:
    """Refresh the bounded activity heartbeat used by aggregate cleanup."""
    marker = session_dir / ".active"
    try:
        marker.touch(mode=0o600, exist_ok=True)
        os.chmod(marker, 0o600)
    except OSError:
        pass


def _session_is_active(session_dir: Path) -> bool:
    grace = _int_env(
        "TOKEN_OPTIMIZER_ARCHIVE_ACTIVE_GRACE_SECONDS",
        _ARCHIVE_ACTIVE_GRACE_SECONDS_DEFAULT,
    )
    try:
        marker = session_dir / ".active"
        return (
            not marker.is_symlink()
            and marker.is_file()
            and time.time() - os.lstat(marker).st_mtime <= grace
        )
    except OSError:
        return True


def _prune_session_entries(
    session_dir: Path,
    *,
    cutoff: float | None = None,
    files_needed: int = 0,
    bytes_needed: int = 0,
) -> None:
    """Prune oldest result entries without removing an active session dir."""
    entries: list[tuple[float, Path, int]] = []
    try:
        candidates = list(session_dir.glob("*.json"))
    except OSError:
        return
    for path in candidates:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            stat = os.lstat(path)
            entries.append((stat.st_mtime, path, stat.st_size))
        except OSError:
            continue
    entries.sort(key=lambda item: item[0])
    removed_files = 0
    removed_bytes = 0
    removed_any = False
    for mtime, path, size in entries:
        expired = cutoff is not None and mtime < cutoff
        needed = (
            removed_files < max(0, files_needed)
            or removed_bytes < max(0, bytes_needed)
        )
        if not expired and not needed:
            continue
        try:
            path.unlink()
            removed_files += 1
            removed_bytes += size
            removed_any = True
        except OSError:
            continue
    if not removed_any:
        return

    # Keep the manifest bounded with the result set. The caller holds the
    # session lease, so replacement cannot race another archive writer.
    manifest = session_dir / "manifest.jsonl"
    try:
        if manifest.is_symlink() or not manifest.is_file():
            return
        retained = {path.stem for path in session_dir.glob("*.json")}
        lines: list[str] = []
        for line in manifest.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
                if record.get("tool_use_id") in retained:
                    lines.append(line)
            except (TypeError, json.JSONDecodeError):
                continue
        if not lines:
            manifest.unlink()
            return
        fd, tmp_path = tempfile.mkstemp(
            prefix=".manifest.", suffix=".tmp", dir=str(session_dir), text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, manifest)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except (OSError, ValueError):
        pass


def cleanup_old_archives(
    max_age_hours: int | None = None,
    skip_session_id: str | None = None,
) -> int:
    """Delete expired archives and enforce aggregate file/byte caps.

    Best-effort: individual OSError is swallowed so a locked or missing
    directory never aborts the hook. The hot path reads its knobs locally to
    avoid importing measure.py. Returns the count of removed session dirs.
    """
    if max_age_hours is None:
        max_age_hours = _int_env(
            "TOKEN_OPTIMIZER_ARCHIVE_RETENTION_HOURS",
            _ARCHIVE_RETENTION_HOURS_DEFAULT,
        )
    max_files = _int_env(
        "TOKEN_OPTIMIZER_ARCHIVE_RETENTION_MAX_FILES",
        _ARCHIVE_RETENTION_MAX_FILES_DEFAULT,
    )
    max_bytes = _int_env(
        "TOKEN_OPTIMIZER_ARCHIVE_RETENTION_MAX_BYTES",
        _ARCHIVE_RETENTION_MAX_BYTES_DEFAULT,
    )
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    skip_sid = _sanitize_session_id(skip_session_id) if skip_session_id else None
    sessions: list[tuple[float, Path, Path, int, int, bool]] = []
    for archive_root in _archive_roots():
        if not archive_root.exists() or archive_root.is_symlink():
            continue
        # Sweep orphan ``.lease.candidate-*`` artifacts from ``.locks/`` (a
        # dot-dir the session scan below intentionally skips). Best-effort;
        # OSError-swallowed internally.
        _sweep_stale_lock_candidates(archive_root)
        try:
            candidates = list(archive_root.iterdir())
        except OSError:
            continue
        for session_dir in candidates:
            if session_dir.name.startswith("."):
                continue
            if session_dir.is_symlink() or not session_dir.is_dir():
                continue
            protected = (
                (skip_sid is not None and session_dir.name == skip_sid)
                or _session_is_active(session_dir)
            )
            try:
                mtime = os.lstat(session_dir).st_mtime
                if protected:
                    lock = LeaseLock(
                        _session_lock_path(archive_root, session_dir.name),
                        acquire_timeout=0,
                    )
                    if lock.acquire():
                        try:
                            _prune_session_entries(session_dir, cutoff=cutoff)
                        finally:
                            lock.release()
                elif mtime < cutoff:
                    lock = LeaseLock(
                        _session_lock_path(archive_root, session_dir.name),
                        acquire_timeout=0,
                    )
                    if lock.acquire():
                        try:
                            if not _session_is_active(session_dir):
                                shutil.rmtree(session_dir, ignore_errors=True)
                                if not session_dir.exists():
                                    removed += 1
                                    continue
                        finally:
                            lock.release()
                file_count, byte_count = _archive_tree_usage(session_dir)
                sessions.append((
                    mtime,
                    archive_root,
                    session_dir,
                    file_count,
                    byte_count,
                    protected,
                ))
            except OSError:
                pass
    total_files = sum(item[3] for item in sessions)
    total_bytes = sum(item[4] for item in sessions)
    for _mtime, archive_root, session_dir, file_count, byte_count, protected in sorted(
        sessions, key=lambda item: item[0]
    ):
        over_files = max_files > 0 and total_files > max_files
        over_bytes = max_bytes > 0 and total_bytes > max_bytes
        if not (over_files or over_bytes):
            break
        if protected:
            lock = LeaseLock(
                _session_lock_path(archive_root, session_dir.name),
                acquire_timeout=0,
            )
            if not lock.acquire():
                continue
            try:
                before_files, before_bytes = _archive_tree_usage(session_dir)
                _prune_session_entries(
                    session_dir,
                    files_needed=max(0, total_files - max_files)
                    if max_files > 0 else 0,
                    bytes_needed=max(0, total_bytes - max_bytes)
                    if max_bytes > 0 else 0,
                )
                after_files, after_bytes = _archive_tree_usage(session_dir)
                total_files -= max(0, before_files - after_files)
                total_bytes -= max(0, before_bytes - after_bytes)
            finally:
                lock.release()
            continue
        lock = LeaseLock(
            _session_lock_path(archive_root, session_dir.name),
            acquire_timeout=0,
        )
        if not lock.acquire():
            continue
        try:
            if _session_is_active(session_dir):
                continue
            shutil.rmtree(session_dir, ignore_errors=True)
            if not session_dir.exists():
                removed += 1
                total_files -= file_count
                total_bytes -= byte_count
        except OSError:
            pass
        finally:
            lock.release()
    return removed


def _cleanup_archives_if_due(session_id: str) -> None:
    """Rate-limit the recursive retention scan on synchronous hook paths."""
    interval = _int_env(
        "TOKEN_OPTIMIZER_ARCHIVE_CLEANUP_INTERVAL_SECONDS",
        _ARCHIVE_CLEANUP_INTERVAL_SECONDS_DEFAULT,
    )
    marker = SNAPSHOT_DIR / ".archive-cleanup.last"
    lock = LeaseLock(
        SNAPSHOT_DIR / ".archive-cleanup.lease",
        acquire_timeout=0,
    )
    if not lock.acquire():
        return
    try:
        try:
            if interval > 0 and time.time() - marker.stat().st_mtime < interval:
                return
        except OSError:
            pass
        try:
            marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            marker.touch(mode=0o600, exist_ok=True)
        except OSError:
            return
        cleanup_old_archives(skip_session_id=session_id)
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Savings event logging (mirrors read_cache.py pattern — self-contained so
# this hook stays fast without importing measure.py at startup)
# ---------------------------------------------------------------------------

def _log_savings_event(event_type: str, tokens_saved: int, session_id: str | None, detail: str) -> None:
    """Write a savings event row to the trends DB.

    This is intentionally self-contained and fail-fast: archive_result.py runs
    in the PostToolUse hot path, so it must never import measure.py or wait on
    a locked dashboard DB. Never raises.
    """
    if tokens_saved <= 0:
        return
    conn: sqlite3.Connection | None = None
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(str(SNAPSHOT_DIR), 0o700)
        conn = sqlite3.connect(str(TRENDS_DB), timeout=_SAVINGS_DB_TIMEOUT_SECONDS)
        _chmod_private_file(TRENDS_DB)
        conn.execute(f"PRAGMA busy_timeout={_SAVINGS_DB_BUSY_TIMEOUT_MS}")
        # WAL lets a savings write proceed while a dashboard reader holds the DB,
        # instead of silently dropping the event on SQLITE_BUSY under parallel MCP
        # calls. (The -wal/-shm chmod below already assumed WAL; this enables it.)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SAVINGS_SCHEMA)
        cost_per_mtok = _estimate_savings_cost_per_mtok()
        cost_saved = tokens_saved * cost_per_mtok / 1_000_000
        conn.execute(
            "INSERT INTO savings_events (timestamp, event_type, tokens_saved, cost_saved_usd, session_id, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), event_type, tokens_saved, cost_saved, session_id, detail),
        )
        conn.commit()
        for suffix in ("", "-wal", "-shm"):
            _chmod_private_file(Path(str(TRENDS_DB) + suffix))
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _estimate_savings_cost_per_mtok() -> float:
    """Return a tiny hook-safe input-token price estimate.

    Full model-aware attribution lives in measure.py. This hot-path hook keeps
    the import graph small and uses only env hints, undercounting to Sonnet when
    the active model is unknown.
    """
    override = os.environ.get("TOKEN_OPTIMIZER_COST_PER_MTOK", "").strip()
    if override:
        try:
            value = float(override)
            if value > 0:
                return value
        except ValueError:
            pass

    is_codex = detect_runtime() == 'codex'
    model = (
        os.environ.get("CLAUDE_MODEL")
        or os.environ.get("ANTHROPIC_MODEL")
        or os.environ.get("CODEX_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or os.environ.get("MODEL")
        or ""
    ).lower()
    if is_codex:
        model = (os.environ.get('CODEX_MODEL') or os.environ.get('OPENAI_MODEL') or '').lower()
    model = re.sub(r"[\s_]+", "-", model.rsplit("/", 1)[-1].rsplit(":", 1)[-1])
    if "fable" in model:
        return 10.0
    if "opus" in model:
        return 5.0
    if "haiku" in model:
        return 1.0
    for alias, rate in _HOOK_INPUT_COST_PER_MTOK.items():
        if model == alias or model.startswith(alias + "-"):
            return rate
    return 0.0 if is_codex else _DEFAULT_SAVINGS_COST_PER_MTOK


# ---------------------------------------------------------------------------
# MCP output-cap savings estimation
# ---------------------------------------------------------------------------

# Fraction of the cap we assume was saved by the native truncation.
# Rationale: we cannot know the true pre-cap size (Claude Code truncates before
# PostToolUse fires). A conservative fixed estimate of 50% of the cap is used:
# i.e., if the cap is 10 000 tokens and the result is at the cap, we assume the
# true output was at least 1.5x the cap, so the native cap saved ~0.5x the cap.
# This deliberately understates rather than overstates; Phase 2 will replace
# this with actual measurement via a PreToolUse size probe.
_MCP_CAP_ESTIMATED_RATIO = 0.5   # fraction of cap_tokens logged as saved
# How close to the cap (in chars) a result must be before we flag it as capped.
# 90% prevents false positives from MCP tools that naturally return near-cap
# amounts without being truncated.
_MCP_CAP_PROXIMITY_PCT = 0.90    # result must be >= 90% of cap_chars to flag

_MCP_CAP_CACHE_UNSET = object()
_MCP_CAP_TOKENS_CACHE = _MCP_CAP_CACHE_UNSET


def _resolve_mcp_cap_tokens() -> int | None:
    """Read explicit MAX_MCP_OUTPUT_TOKENS from env or user settings.json.

    Resolution order mirrors the Claude Code env injection chain:
      1. Process environment (set by Claude Code from settings.json at startup)
      2. ~/.claude/settings.json "env" block (direct read — for callers running
         outside the Claude Code process, e.g. in tests)

    Never raises. Returns None when no explicit cap is configured so we do not
    invent savings from an assumed platform default.
    """
    global _MCP_CAP_TOKENS_CACHE
    if _MCP_CAP_TOKENS_CACHE is not _MCP_CAP_CACHE_UNSET:
        return _MCP_CAP_TOKENS_CACHE

    # 1. Env var (most common — Claude Code injects this from settings.json)
    env_val = os.environ.get("MAX_MCP_OUTPUT_TOKENS", "").strip()
    if env_val:
        try:
            v = int(env_val)
            if v > 0:
                _MCP_CAP_TOKENS_CACHE = v
                return v
        except ValueError:
            pass

    # 2. settings.json "env" block — for out-of-process callers
    raw = settings_env_value("MAX_MCP_OUTPUT_TOKENS")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                _MCP_CAP_TOKENS_CACHE = v
                return v
        except ValueError:
            pass

    _MCP_CAP_TOKENS_CACHE = None
    return None


# Built-in exemptions for MCP servers whose whole purpose is returning large
# verbatim code/docs the agent must read inline — compressing those to a preview
# defeats the reason they were called. Kept deliberately narrow (only tools whose
# PRIMARY output is verbatim source/docs) so the optimizer still compresses chatty
# general MCPs. The user's TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_TOOLS is ADDITIVE to
# this list, so opting more tools out stays a one-line change. To drop a default,
# set TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_DEFAULTS=off.
_DEFAULT_EXEMPT_PATTERNS: tuple[str, ...] = (
    # Scoped to the specific verbatim content-fetch tools only — NOT whole
    # servers. octocode/github/deepwiki also expose search/tree/list tools whose
    # compression is desirable, so exempting the whole server would bloat context
    # and defeat the optimizer. Only tools whose PRIMARY output is verbatim
    # code/docs the agent must read inline are listed.
    "mcp__context7__query-docs",            # context7: verbatim library docs
    "mcp__octocode__githubGetFileContent",  # octocode: full remote file content
    "mcp__octocode__localGetFileContent",   # octocode: full local file content
    "mcp__github__get_file_contents",       # official github MCP file fetcher
    "mcp__deepwiki__read_wiki_contents",    # deepwiki: verbatim repo docs
)

_EXEMPT_PATTERNS_UNSET: tuple[str, ...] | None = ("\0__unset__",)
_EXEMPT_PATTERNS_CACHE: tuple[str, ...] | None = _EXEMPT_PATTERNS_UNSET


def _resolve_exempt_tool_patterns() -> tuple[str, ...]:
    """Resolve the per-tool archive allowlist (built-in defaults + user opt-ins).

    Some tools are *documented* to return large verbatim payloads (source
    fetchers, doc retrievers). Compressing those to a metadata preview defeats
    the reason they were called, so they are exempt from MCP output replacement
    (follow-up to the exempt-tools expansion).

    The list is the union of ``_DEFAULT_EXEMPT_PATTERNS`` (well-known code/doc
    MCPs, on by default) and the user's ``TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_TOOLS``
    — a comma-separated list of fnmatch globs matched against the full tool name,
    e.g. ``mcp__mytool__*,*my_repo_file``. The user value is ADDITIVE, so opting
    another tool out is a one-line change. Set
    ``TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_DEFAULTS=off`` to drop the built-ins.
    Resolution mirrors _resolve_mcp_cap_tokens: process env first, then the
    settings.json "env" block. Never raises.
    """
    global _EXEMPT_PATTERNS_CACHE
    if _EXEMPT_PATTERNS_CACHE is not _EXEMPT_PATTERNS_UNSET:
        return _EXEMPT_PATTERNS_CACHE

    raw = os.environ.get("TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_TOOLS", "").strip()
    defaults_flag = os.environ.get("TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_DEFAULTS", "").strip()
    if not raw or not defaults_flag:
        # settings.json "env" block — for out-of-process callers.
        if not raw:
            raw = settings_env_value("TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_TOOLS")
        if not defaults_flag:
            defaults_flag = settings_env_value("TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_DEFAULTS")

    user_patterns = tuple(p.strip() for p in raw.split(",") if p.strip())
    base = () if defaults_flag.lower() in ("off", "false", "0", "none") else _DEFAULT_EXEMPT_PATTERNS
    # Union, preserving order, defaults first.
    seen: dict[str, None] = {}
    for p in (*base, *user_patterns):
        seen.setdefault(p, None)
    patterns = tuple(seen)
    _EXEMPT_PATTERNS_CACHE = patterns
    return patterns


def _is_archive_exempt(tool_name: str) -> bool:
    """True when tool_name matches any configured allowlist glob."""
    if not tool_name:
        return False
    return any(fnmatch.fnmatch(tool_name, pat) for pat in _resolve_exempt_tool_patterns())


def _maybe_log_mcp_cap_savings(tool_name: str, original_char_count: int, session_id: str | None) -> None:
    """Log estimated native MCP-cap savings independently of archive writes."""
    if "__" not in tool_name:
        return
    cap_tokens = _resolve_mcp_cap_tokens()
    if cap_tokens is None:
        return
    cap_chars = cap_tokens * CHARS_PER_TOKEN
    cap_threshold = int(cap_chars * _MCP_CAP_PROXIMITY_PCT)
    if original_char_count < cap_threshold:
        return
    estimated_saved = max(0, int(cap_tokens * _MCP_CAP_ESTIMATED_RATIO))
    if estimated_saved <= 0:
        return
    _log_savings_event(
        "mcp_cap",
        estimated_saved,
        session_id=session_id,
        detail=(
            f"estimated: {tool_name} result at cap "
            f"({original_char_count:,} chars >= "
            f"{cap_threshold:,} threshold, "
            f"cap={cap_tokens:,} tok, "
            f"assumed saved ~{estimated_saved:,} tok [estimated])"
        ),
    )


# Content-block types that carry media rather than text. The model needs these
# verbatim: an archive pointer in place of a screenshot leaves it blind, and
# counting base64 chars as tokens logs savings that never happened (images are
# billed by pixel dimensions, not encoded length).
_MEDIA_BLOCK_TYPES = frozenset({"image", "document", "audio", "video"})


def _is_media_block(block) -> bool:
    if not isinstance(block, dict):
        return False
    if block.get("type") in _MEDIA_BLOCK_TYPES:
        return True
    # MCP embedded resource carrying binary: {"type": "resource", "resource": {"blob": ...}}
    res = block.get("resource")
    return isinstance(res, dict) and isinstance(res.get("blob"), str) and bool(res.get("mimeType"))


def _contains_media_block(value) -> bool:
    """True when a raw tool_response holds an image/document/audio block.

    Only looks where hosts put content blocks: the response itself, a top-level
    list of blocks, or a "content" list (the MCP result shape). It does not
    recurse into arbitrary data, so a Notion or Slack payload that merely
    describes an image node still archives normally. Never raises.
    """
    try:
        candidates = []
        if isinstance(value, dict):
            candidates.append(value)
            if isinstance(value.get("content"), list):
                candidates.extend(value["content"])
        elif isinstance(value, list):
            for item in value:
                candidates.append(item)
                if isinstance(item, dict) and isinstance(item.get("content"), list):
                    candidates.extend(item["content"])
        return any(_is_media_block(c) for c in candidates)
    except Exception:
        return False


def _tool_response_to_text(value) -> str:
    """Normalize Claude PostToolUse response shapes into archiveable text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [_tool_response_to_text(item) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        parts: list[str] = []
        for key in ("stdout", "stderr", "error", "text", "output", "result", "content"):
            if key not in value:
                continue
            text = _tool_response_to_text(value.get(key))
            if not text:
                continue
            if key in ("stdout", "stderr", "error"):
                parts.append(f"{key}:\n{text}")
            else:
                parts.append(text)
        if parts:
            return "\n\n".join(parts)
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _ensure_private_archive_dir(archive_dir: Path) -> bool:
    try:
        if archive_dir.exists():
            st = archive_dir.lstat()
            if archive_dir.is_symlink() or not archive_dir.is_dir():
                return False
            if getattr(st, "st_nlink", 1) < 1:
                return False
        archive_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(str(archive_dir), 0o700)
        return True
    except OSError:
        return False


def _atomic_write_json(path: Path, payload: dict) -> None:
    if path.exists() and (path.is_symlink() or path.lstat().st_nlink > 1):
        raise OSError("unsafe archive entry path")
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _append_manifest_line(path: Path, payload: dict) -> None:
    if path.exists() and (path.is_symlink() or path.lstat().st_nlink > 1):
        raise OSError("unsafe archive manifest path")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
        os.chmod(path, 0o600)
    except Exception:
        raise


# ---------------------------------------------------------------------------
# Progressive-disclosure spine: reusable archive-original + pointer.
#
# Lets non-MCP / PreToolUse compressors (first-read code, bash, structured
# data) persist the FULL original and hand the model a retrievable pointer,
# exactly like the MCP eviction path. Cache-safety is structural: these run in
# arrival hooks (PreToolUse / PostToolUse) BEFORE the content's first API send,
# so they never mutate already-cached context. expand_archived locates an entry
# by filename stem, so a derived key retrieves identically to a tool_use_id --
# no expand change is needed.
# ---------------------------------------------------------------------------

def derive_archive_key(session_id: str | None, file_path: str, mtime_ns: int) -> str:
    """Stable archive key for PreToolUse paths that have no tool_use_id.

    sha256(session_id + file_path + mtime_ns)[:16]. Hex output satisfies
    expand_archived's [a-zA-Z0-9_-]+ key validation.
    """
    raw = f"{session_id or ''}|{file_path}|{mtime_ns}".encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:16]


def _expand_instruction(key: str, tool_name: str | None = None) -> str:
    """The actionable retrieval line for an archive footer.

    The prior footer said only "Retrieve with: expand <id>", but `expand` is a
    measure.py subcommand, not a callable tool — the model had no way to act on it
    and re-fetched instead. Emit the exact runnable Bash command (shared with the
    re-fetch guard via expand_command, so the two can never diverge) plus an
    explicit anti-re-fetch instruction so the correct path is unambiguous.
    """
    if tool_name and is_live_state_tool(tool_name):
        # A browser/screen read is a snapshot; calling again is how you get the
        # CURRENT state, so don't tell the model not to.
        return (
            "This is a snapshot of that moment; call the tool again for current state. "
            f"To re-read this snapshot, run in Bash:\n    {expand_command(key)}"
        )
    dont = f"Do NOT call {tool_name} again" if tool_name else "Do NOT re-run the original tool"
    return (
        f"{dont} to get this data — read the saved copy by running this in Bash:\n"
        f"    {expand_command(key)}"
    )


def archive_entry_exists(session_id: str | None, key: str) -> bool:
    """True when the archive entry file for ``key`` actually exists on disk.

    Reusable by all pointer producers (archive_result, read_cache, bash_compress)
    so they can re-check the entry survived a post-write retention prune before
    emitting a pointer that would send the model to a missing file.
    Never raises.
    """
    try:
        if not key:
            return False
        entry_path = _archive_dir_for_session(session_id or "unknown") / f"{key}.json"
        return entry_path.is_file() and not entry_path.is_symlink()
    except Exception:
        return False


def build_archive_pointer(preview: str, original_chars: int, key: str) -> str:
    """Standard progressive-disclosure pointer appended after a compressed preview."""
    return (
        f"{preview}\n\n"
        f"[Full result archived ({original_chars:,} chars) — saved to disk, not lost.\n"
        # The closing bracket sits on its own line so a line-copy of the
        # command never picks it up (the stale "expand <id>]" copy failed).
        f"{_expand_instruction(key)}\n]"
    )


def archive_original(content: str, session_id: str | None, key: str,
                     tool_name: str, quiet: bool = True,
                     file_path: str | None = None,
                     language: str | None = None) -> str | None:
    """Persist a full original (credential-redacted) under `key`; return the key.

    Reusable by non-MCP / PreToolUse compressors so nothing is ever lost. Mirrors
    the MCP archive path (same dir layout + manifest) so expand_archived retrieves
    it unchanged. Fail-open: returns None on any failure (caller serves raw).

    `file_path`/`language`: when the caller knows which file is being
    degraded (the first-read skeleton path does), record it so a degraded read is
    self-identifiable from the archive without transcript archaeology. Additive
    and always present (null when unknown) so the record schema stays uniform.
    """
    try:
        if not key or not re.match(r"^[a-zA-Z0-9_-]+$", key):
            return None
        archive_dir = _archive_dir_for_session(session_id or "unknown")
        if not _ensure_private_archive_dir(archive_dir):
            return None
        session_lock = LeaseLock(
            _session_lock_path(archive_dir.parent, session_id or "unknown"),
            acquire_timeout=0.075,
            cohort_throttle=False,
        )
        if not session_lock.acquire():
            return None
        try:
            safe_response = _redact_credentials(content)
            # Redact the path too — a path can embed a token/secret (e.g. a URL-ish
            # segment). Stored uniformly: null when the caller didn't supply one.
            safe_path = _redact_credentials(file_path) if file_path else None
            meta = {
                "tool_name": tool_name,
                "tool_use_id": key,
                ARGS_HASH_KEY: None,  # non-MCP progressive-disclosure path; guard skips these.
                "file_path": safe_path,
                "language": language,
                "chars": len(safe_response),
                "original_chars": len(content),
                "tokens_est": int(len(safe_response) / CODE_CHARS_PER_TOKEN),
                "truncated": False,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "archived_from": "compress_with_preservation",
            }
            _mark_session_active(archive_dir)
            entry_path = archive_dir / f"{key}.json"
            _atomic_write_json(entry_path, {**meta, "response": safe_response})
            _append_manifest_line(archive_dir / "manifest.jsonl", meta)
            return key
        finally:
            session_lock.release()
    except Exception:
        if not quiet:
            print(f"[Tool Archive] archive_original failed for {tool_name}; serving raw.", file=sys.stderr)
        return None


def _log_agent_result_opportunity(tool_response: str, session_id: str | None) -> None:
    """Record a measure-only opportunity event for a large Agent/Task result.

    The would-be saving is the head+tail+pointer treatment's delta. tier =
    opportunity so it NEVER reaches the realized savings headline (agent_result_
    skeleton is not a _V5_COMPRESSION_CATEGORIES feature). Uses the hot-path
    compression_log writer (no measure.py import). Fail-open.
    """
    head = tool_response[:_AGENT_RESULT_HEAD_CHARS]
    tail = (tool_response[-_AGENT_RESULT_TAIL_CHARS:]
            if len(tool_response) > _AGENT_RESULT_TAIL_CHARS else "")
    orig_tokens = int(len(tool_response) / CODE_CHARS_PER_TOKEN)
    would_be_tokens = int(len(head + tail) / CODE_CHARS_PER_TOKEN)
    if orig_tokens <= 0 or would_be_tokens >= orig_tokens:
        return
    try:
        from compression_log import log_compression_event
        log_compression_event(
            feature=_FEATURE_AGENT_RESULT,
            session_id=session_id,
            command_pattern="agent",
            tier="opportunity",
            verified=False,
            quality_preserved=False,  # 39.1% harm proxy: a skeleton WOULD elide value
            detail=f"would-be head+tail; harm_proxy={_AGENT_RESULT_HARM_PCT}%",
            original_tokens=orig_tokens,
            compressed_tokens=would_be_tokens,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Structure-aware MCP output compression
# ---------------------------------------------------------------------------

# Bounds for the synchronous PostToolUse hot path. splitlines() / sort over a
# multi-MB MCP payload measured ~865ms (perf regression on a 5MB / 290k-line path
# listing); these caps keep classification + preview under the 500ms hook budget.
# The FULL result is archived regardless, so `expand` loses nothing.
_DETECT_HEAD_CHARS = 16_384    # classify line type from the head slice, not the whole payload
_PATHS_SAMPLE_LINES = 10_000   # lines sampled for the path preview


def _is_extend_char(ch: str) -> bool:
    """True when ``ch`` continues the grapheme cluster of the char before it:
    combining marks, variation selectors, emoji modifiers, tag chars."""
    if not ch:
        return False
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Mc", "Me"):
        return True
    cp = ord(ch)
    return (
        0xFE00 <= cp <= 0xFE0F            # variation selectors
        or 0xE0100 <= cp <= 0xE01EF       # supplementary variation selectors
        or 0x1F3FB <= cp <= 0x1F3FF       # emoji modifiers (skin tone)
        or 0xE0020 <= cp <= 0xE007F       # tag chars (subdivision flags)
    )


def _is_regional_indicator(ch: str) -> bool:
    return bool(ch) and 0x1F1E6 <= ord(ch) <= 0x1F1FF


def _breaks_cluster(text: str, end: int) -> bool:
    """True when cutting ``text`` at ``end`` lands inside a grapheme cluster.

    Covers the common invisible-joiner cases without a dependency: a ZWJ next
    to the cut (the sequence is one cluster — backing off removes the whole
    partial emoji rather than rendering a different one), an extend char right
    after the cut (combining mark, modifier, selector, tag), and an unpaired
    regional indicator at the tail (half a flag).
    """
    prev = text[end - 1]
    nxt = text[end] if end < len(text) else ""
    if prev == "\u200d" or nxt == "\u200d":
        return True
    if _is_extend_char(nxt):
        return True
    if _is_regional_indicator(prev):
        run = 0
        i = end - 1
        while i >= 0 and _is_regional_indicator(text[i]):
            run += 1
            i -= 1
        if run % 2 == 1:
            return True
    return False


def _preview_slice(text: str, limit: int = _ARCHIVE_PREVIEW_SIZE) -> str:
    """``text[:limit]`` backed off to a grapheme-cluster boundary.

    A raw codepoint slice can end mid-cluster — a ZWJ sequence cut in two
    renders a DIFFERENT emoji than the archived one, a combining mark left at
    the tail attaches to the footer, an odd regional indicator shows half a
    flag. Walk the cut back while it would split a cluster; a text made of one
    giant cluster degrades to an empty preview (the pointer still recovers).
    """
    end = min(limit, len(text))
    while end > 0 and _breaks_cluster(text, end):
        end -= 1
    return text[:end]


def _detect_output_type(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            json.loads(stripped[:100_000])
            return "json"
        except (json.JSONDecodeError, RecursionError):
            pass
    # Bound the scan: splitlines() over a multi-MB payload is a hot-path latency
    # trap; the first lines in the head slice are enough to classify.
    lines = stripped[:_DETECT_HEAD_CHARS].splitlines()[:50]
    if len(lines) > 5:
        path_like = sum(1 for ln in lines if "/" in ln or "\\" in ln)
        if path_like > len(lines) * 0.6:
            return "paths"
    if len(lines) > 5:
        sep_count = sum(1 for ln in lines if set(ln.strip()) <= set("-=| +") and ln.strip())
        if sep_count >= 1:
            return "table"
    return "text"


def _compress_mcp_preview(text: str, output_type: str) -> str:
    if output_type == "json":
        return _compress_mcp_json(text)
    if output_type == "paths":
        return _compress_mcp_paths(text)
    if output_type == "table":
        return _compress_mcp_table(text)
    return _preview_slice(text)


# Value-preserving columnar compression thresholds.
_TABULAR_MIN_ROWS = 3            # too few rows to be worth a schema header
_TABULAR_MAX_COLS = 40          # wider than this is not really tabular
_TABULAR_CELL_CAP = 100         # per-cell char cap (keeps values, bounds width)
_TABULAR_TOTAL_CAP = 8192       # if the columnar form exceeds this, evict instead
_TABULAR_MIN_REDUCTION = 0.40   # must be >=40% smaller than original to engage


def _compress_mcp_json_tabular(data, original_text: str):
    """Value-preserving columnar compression for homogeneous arrays-of-dicts.

    Strips the repeated per-row schema (emit keys once) while KEEPING values, so
    the model can still read any cell inline instead of paying a full re-expand.
    Returns the columnar string, or None to fall back to the eviction preview
    when the data isn't tabular enough or the result isn't a clear win.
    """
    try:
        if not isinstance(data, list) or len(data) < _TABULAR_MIN_ROWS:
            return None
        dict_rows = [r for r in data if isinstance(r, dict)]
        if len(dict_rows) < max(_TABULAR_MIN_ROWS, int(len(data) * 0.8)):
            return None  # not predominantly homogeneous dicts
        # Ordered union of keys (first-seen order), bounded width.
        cols: list[str] = []
        seen = set()
        for row in dict_rows:
            for k in row.keys():
                if k not in seen:
                    seen.add(k)
                    cols.append(str(k))
        if not cols or len(cols) > _TABULAR_MAX_COLS:
            return None

        def _cell(v) -> str:
            s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            s = s.replace("\n", " ").replace("\t", " ")
            return s[:_TABULAR_CELL_CAP] + ("…" if len(s) > _TABULAR_CELL_CAP else "")

        lines = [
            f"JSON array ({len(data)} items), columnar (schema-stripped, values preserved):",
            "cols: " + ", ".join(cols),
        ]
        for row in data:
            if isinstance(row, dict):
                lines.append("- " + " | ".join(_cell(row.get(c, "")) for c in cols))
            else:
                lines.append("- " + _cell(row))
        result = "\n".join(lines)

        # Only engage when it's a clear win and stays within the inline budget.
        if len(result) > _TABULAR_TOTAL_CAP:
            return None
        if len(result) > len(original_text) * (1.0 - _TABULAR_MIN_REDUCTION):
            return None
        return result
    except Exception:
        return None


def _compress_mcp_json(text: str) -> str:
    try:
        data = json.loads(text[:500_000])
    except (json.JSONDecodeError, RecursionError):
        return _preview_slice(text)

    # U3: value-preserving columnar path for homogeneous arrays-of-dicts.
    # Keeps values usable inline so the model rarely needs a full re-expand;
    # falls through to the eviction preview below when it isn't a clear win.
    tabular = _compress_mcp_json_tabular(data, text)
    if tabular is not None:
        return tabular

    parts: list[str] = []
    if isinstance(data, dict):
        parts.append(f"JSON object ({len(data)} keys):")
        for key in list(data.keys())[:15]:
            val = data[key]
            if isinstance(val, list):
                parts.append(f"  {key}: [{len(val)} items]")
            elif isinstance(val, dict):
                subkeys = list(val.keys())[:5]
                suffix = "..." if len(val) > 5 else ""
                parts.append(f"  {key}: {{{', '.join(subkeys)}{suffix}}}")
            elif isinstance(val, str) and len(val) > 80:
                parts.append(f'  {key}: "{val[:77]}..."')
            else:
                parts.append(f"  {key}: {json.dumps(val)[:80]}")
        if len(data) > 15:
            parts.append(f"  ... ({len(data) - 15} more keys)")
    elif isinstance(data, list):
        parts.append(f"JSON array ({len(data)} items):")
        for item in data[:5]:
            if isinstance(item, dict):
                keys = list(item.keys())[:5]
                suffix = "..." if len(item) > 5 else ""
                parts.append(f"  {{{', '.join(keys)}{suffix}}}")
            else:
                parts.append(f"  {json.dumps(item)[:80]}")
        if len(data) > 5:
            parts.append(f"  ... ({len(data) - 5} more items)")

    result = "\n".join(parts)
    return _preview_slice(result) if len(result) > _ARCHIVE_PREVIEW_SIZE else result


def _compress_mcp_paths(text: str) -> str:
    # Exact total via a single C-level scan (no full materialization), then tally
    # directories over a bounded SAMPLE of lines so a 290k-line listing can't stall
    # the synchronous hook. heapq.nlargest avoids sorting the whole dir dict.
    total_lines = text.count("\n") + 1
    dirs: dict[str, int] = {}
    sampled = 0
    for line in itertools.islice(io.StringIO(text), _PATHS_SAMPLE_LINES):
        sampled += 1
        stripped = line.strip()
        if "/" in stripped:
            dir_name = stripped.rsplit("/", 1)[0]
            dirs[dir_name] = dirs.get(dir_name, 0) + 1
    truncated = total_lines > sampled
    plus = "+" if truncated else ""

    header = f"{total_lines} paths across {len(dirs)}{plus} directories"
    if truncated:
        header += f" (dirs sampled from first {sampled:,} lines)"
    parts = [header + ":"]
    for dir_name, count in heapq.nlargest(10, dirs.items(), key=lambda kv: kv[1]):
        parts.append(f"  {dir_name}/ ({count}{plus} files)")
    if len(dirs) > 10:
        parts.append(f"  ... ({len(dirs) - 10} more directories)")

    result = "\n".join(parts)
    return _preview_slice(result) if len(result) > _ARCHIVE_PREVIEW_SIZE else result


def _compress_mcp_table(text: str) -> str:
    lines = text.strip().splitlines()
    header = lines[:2]
    data = [ln for ln in lines[2:] if ln.strip()]
    result = header + data[:10]
    if len(data) > 10:
        result.append(f"... ({len(data) - 10} more rows, {len(data)} total)")
    return _preview_slice("\n".join(result))


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def archive_result(quiet: bool = False) -> None:
    """PostToolUse hook handler: archive large tool results to disk.

    Reads hook JSON from stdin. If tool_response >= _ARCHIVE_THRESHOLD chars,
    saves the full result to disk and (for MCP tools) outputs a trimmed
    replacement via stdout with updatedMCPToolOutput.

    Logs a savings event only when MCP output is actually replaced. Native
    Bash/Read/etc. archives are durability metadata unless a PreToolUse path
    suppresses the original output before it enters context.
    """
    hook_input = read_stdin_hook_input(_STDIN_MAX_BYTES)
    if not hook_input:
        return

    tool_name = hook_input.get("tool_name", "")
    # Recovery must finish in one call: never archive or replace the original again.
    if is_recovery_output(tool_name, hook_input.get("tool_input", {})):
        return
    tool_use_id = hook_input.get("tool_use_id", "")
    raw_response = hook_input.get("tool_response", "")
    # Screenshots and other media pass through untouched: no archive, no
    # replacement, no fingerprint (so the re-fetch guard can never block a
    # fresh screenshot either).
    if _contains_media_block(raw_response):
        return
    tool_response = _tool_response_to_text(raw_response)
    session_id = hook_input.get("session_id", "")

    if not tool_response:
        return

    original_char_count = len(tool_response)
    _maybe_log_mcp_cap_savings(tool_name, original_char_count, session_id)

    if original_char_count < _ARCHIVE_THRESHOLD:
        return

    if not tool_use_id or not session_id:
        if not quiet:
            print("[Tool Archive] Missing tool_use_id or session_id, skipping.", file=sys.stderr)
        return

    # Map tool_use_id to a filesystem-safe archive key. Unsafe characters (+,
    # space, /, ., ') used to skip archiving entirely — the full result passed
    # through uncompressed with no expand path and nothing on disk. A
    # deterministic digest keeps the archive, satisfies expand's own key
    # validation, and makes path traversal impossible by construction.
    raw_tool_use_id = tool_use_id
    tool_use_id = _safe_archive_key(tool_use_id)
    if tool_use_id != raw_tool_use_id and not quiet:
        print(f"[Tool Archive] tool_use_id {raw_tool_use_id!r} has unsafe "
              f"characters; archived under key {tool_use_id}", file=sys.stderr)

    now = datetime.now(timezone.utc)
    truncated = original_char_count > _ARCHIVE_MAX_SIZE

    if truncated:
        # Redact a window that reaches past the cap BEFORE cutting at it: a
        # secret straddling the cap is otherwise a prefix no pattern
        # recognises. If the redactor refuses, fall back to the plain cut; the
        # redaction below then raises the same way and skips the write.
        try:
            tool_response = _redact_credentials(
                tool_response[:_ARCHIVE_MAX_SIZE + _ARCHIVE_CAP_REDACT_OVERSHOOT]
            )[:_ARCHIVE_MAX_SIZE]
        except Exception:
            tool_response = tool_response[:_ARCHIVE_MAX_SIZE]
        tool_response += (
            f"\n\n[TRUNCATED at 5MB. Original size: {original_char_count} chars]"
        )

    char_count = _ARCHIVE_MAX_SIZE if truncated else original_char_count
    token_est = int(char_count / CODE_CHARS_PER_TOKEN)

    archive_dir = _archive_dir_for_session(session_id)
    if not _ensure_private_archive_dir(archive_dir):
        if not quiet:
            print(f"[Tool Archive] Unsafe archive directory for {tool_name}; leaving output unchanged.", file=sys.stderr)
        return

    # Fingerprint the call (name + args) so the PreToolUse re-fetch guard can
    # detect an identical re-fetch of this result (self-healing).
    # ONLY for MCP tools whose result we actually replace with a preview. Exempt
    # allowlisted tools serve their full fresh result (see below), so the guard
    # must NOT block a re-call of them — record args_hash=None so it never matches.
    if "__" in tool_name and not _is_archive_exempt(tool_name) and not is_live_state_tool(tool_name):
        args_hash = tool_fingerprint(tool_name, hook_input.get("tool_input", {}))
    else:
        args_hash = None

    meta = {
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        ARGS_HASH_KEY: args_hash,
        "chars": char_count,
        "original_chars": original_char_count,
        "tokens_est": token_est,
        "truncated": truncated,
        "timestamp": now.isoformat(),
        "archived_from": "PostToolUse",
    }
    if raw_tool_use_id != tool_use_id:
        # Traceability: the key is the safe digest; keep the host's id readable
        # (redacted — it is caller text) for debugging joins.
        try:
            meta["tool_use_id_raw"] = _redact_credentials(raw_tool_use_id)
        except Exception:
            meta["tool_use_id_raw"] = "<untrusted>"

    # Redact credential patterns before writing to disk.
    # Performed on the (possibly truncated) response so no plaintext secrets
    # ever reach the archive file, even transiently.
    try:
        safe_response = _redact_credentials(tool_response)
    except Exception as exc:
        # A configured-but-broken custom pattern file means redaction cannot
        # be trusted to cover the user's own secret shapes — fail CLOSED: skip
        # the archive write entirely and let the output pass through in memory.
        if RedactionConfigError is None or not isinstance(exc, RedactionConfigError):
            raise
        if not quiet:
            print(f"[Tool Archive] {exc}; result not archived.", file=sys.stderr)
        if pop_redaction_warning is not None:
            msg = pop_redaction_warning()
            if msg:
                # Sole hook output for this run: a single JSON object is a
                # valid envelope on every host.
                print(json.dumps({"systemMessage": msg}))
        return

    session_lock = LeaseLock(
        _session_lock_path(archive_dir.parent, session_id),
        acquire_timeout=0.075,
        cohort_throttle=False,
    )
    if not session_lock.acquire():
        if not quiet:
            print(f"[Tool Archive] Archive busy for {tool_name}; leaving output unchanged.", file=sys.stderr)
        return
    try:
        _mark_session_active(archive_dir)
        entry_path = archive_dir / f"{tool_use_id}.json"
        _atomic_write_json(entry_path, {**meta, "response": safe_response})
        manifest_path = archive_dir / "manifest.jsonl"
        _append_manifest_line(manifest_path, meta)
    except OSError:
        if not quiet:
            print(f"[Tool Archive] Failed to archive {tool_name} result; leaving output unchanged.", file=sys.stderr)
        return
    finally:
        session_lock.release()

    # The recursive cross-identity retention pass is throttled and separately
    # leased, so ordinary synchronous PostToolUse calls do only O(1) metadata
    # work instead of restatting the full archive tree.
    try:
        _cleanup_archives_if_due(session_id)
    except Exception:
        pass

    # Re-check that the entry survived the retention pass.  _cleanup_archives_if_due
    # runs AFTER the write and retention ceilings prune even protected sessions
    # (TOKEN_OPTIMIZER_ARCHIVE_RETENTION_MAX_BYTES=1 reproduces this), so the entry
    # we just wrote can be gone by the time we build the pointer.  When it is, serve
    # the full result — a missing-entry pointer would strand the model with an
    # unrecoverable expand.
    replacement_emitted = False  # set below only when the pointer is actually emitted
    entry_exists = archive_entry_exists(session_id, tool_use_id)
    if not entry_exists:
        if not quiet:
            print(
                f"[Tool Archive] Entry {tool_use_id} was pruned by retention pass "
                f"after write; serving full result instead of pointer.",
                file=sys.stderr,
            )
        # Fall through: the MCP replacement path below is skipped because
        # replacement_emitted stays False, so the hook returns without printing
        # an updatedMCPToolOutput — the original tool output reaches context.

    if not quiet:
        print(f"[Tool Archive] Archived {tool_name} result ({char_count:,} chars, ~{token_est:,} tokens): {tool_use_id}", file=sys.stderr)

    # WS4: measure-only opportunity event for large Agent/Task results. We do NOT
    # replace the output (the 39.1% harm proxy fails the gate), but we record the
    # would-be saving so coverage/dashboard surface the pool. The full result is
    # already archived above, so an `expand` path exists regardless. Fail-open.
    if tool_name in _AGENT_RESULT_TOOL_NAMES and original_char_count >= _AGENT_RESULT_MIN_CHARS:
        try:
            _log_agent_result_opportunity(tool_response, session_id)
        except Exception:
            pass

    store = None
    try:
        tool_type = "mcp" if "__" in tool_name else tool_name.lower()
        command_or_path = hook_input.get("tool_input", {}).get("command") or hook_input.get("tool_input", {}).get("file_path") or tool_name
        # Redact before persisting: a command/path can embed a token or secret, and
        # SessionStore is durable. Hash the REDACTED response so the identity hash
        # matches the redacted preview/archive rather than pre-redaction plaintext.
        safe_command_or_path = _redact_credentials(str(command_or_path))
        output_hash = hashlib.sha256(safe_response[:10000].encode("utf-8", errors="replace")).hexdigest()[:16]
        # U5: source lineage tags. When the tool input carried a file_path,
        # record it (redacted) plus the detected language and the archive
        # origin so the row is self-identifiable and joinable to the file
        # archive meta without transcript archaeology.
        raw_file_path = hook_input.get("tool_input", {}).get("file_path")
        lineage_source_file = _redact_credentials(str(raw_file_path)) if raw_file_path else None
        lineage_language = None
        if lineage_source_file:
            try:
                from structure_map import detect_structure_language
                lineage_language = detect_structure_language(lineage_source_file)
            except Exception:
                lineage_language = None
        store = SessionStore(session_id)
        store.insert_tool_output(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            tool_type=tool_type,
            command_or_path=safe_command_or_path[:500],
            output_hash=output_hash,
            output_chars=char_count,
            output_tokens_est=token_est,
            compressed_preview=_preview_slice(safe_response),
            source_file_path=lineage_source_file[:500] if lineage_source_file else None,
            language=lineage_language,
            archived_from="PostToolUse",
            output_text=safe_response[:50000],
        )
    except Exception:
        pass
    finally:
        if store is not None:
            store.close()

    # Allowlisted tools are documented large-payload
    # fetchers: skip the replacement so the full verbatim result reaches
    # context. It's still archived to disk above, so `expand <id>` works —
    # same treatment as _AGENT_RESULT_TOOL_NAMES (archive, don't replace).
    if "__" in tool_name and _is_archive_exempt(tool_name):
        if not quiet:
            print(f"[Tool Archive] {tool_name} is allowlisted; serving full result (archived for expand).", file=sys.stderr)
        return

    # For MCP tools (tool_name contains "__"): output replacement via stdout
    # No pressure gate here: the compressed replacement SAVES tokens.
    # Suppressing it would cause the full uncompressed response to flow through.
    #
    # Skip the replacement when the entry was pruned by the retention pass that
    # ran after write. Emitting a pointer to a missing entry strands the model.
    if not entry_exists:
        return

    if "__" in tool_name:
        output_type = _detect_output_type(safe_response)
        preview = _compress_mcp_preview(safe_response, output_type)
        suffix = f" ({output_type})" if output_type != "text" else ""
        if original_char_count > _ARCHIVE_MAX_SIZE:
            replacement = preview + (
                f"\n\n[Full result archived ({original_char_count:,} chars{suffix}, truncated to 5MB) — "
                f"saved to disk, not lost.\n{_expand_instruction(tool_use_id, tool_name)}\n]"
            )
        else:
            replacement = preview + (
                f"\n\n[Full result archived ({char_count:,} chars{suffix}) — saved to disk, not lost.\n"
                f"{_expand_instruction(tool_use_id, tool_name)}\n]"
            )
        original_tokens = int(original_char_count / CODE_CHARS_PER_TOKEN)
        replacement_tokens = int(len(replacement) / CODE_CHARS_PER_TOKEN)
        tokens_saved = max(0, original_tokens - replacement_tokens)
        _log_savings_event(
            "tool_archive",
            tokens_saved,
            session_id=session_id,
            detail=(
                f"replaced {tool_name} "
                f"({original_char_count:,} chars -> {len(replacement):,} chars)"
            ),
        )
        output = json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedMCPToolOutput": replacement,
            }
        })
        print(output)


if __name__ == "__main__":
    args = sys.argv[1:]
    quiet = "--quiet" in args or "-q" in args
    archive_result(quiet=quiet)
