"""Model-routing guidance on by default: SessionStart injection + install markers.

Routing advice used to be manual-only (``measure.py inject-routing``), so most
installs never had it and the savings report could not attribute routing
behavior to Token Optimizer. This module makes it default:

  * On SessionStart (Claude Code and Codex only) the MODEL_ROUTING managed
    block is injected into the user's GLOBAL instruction file
    (``~/.claude/CLAUDE.md`` / ``$CODEX_HOME/AGENTS.md``) through the existing
    ``inject_managed_block``. The check is cheap: no history scans, one small
    file read, and the block is regenerated without the volatile "last 30
    days" stats line so its bytes are stable across days (prompt-cache
    stability). ``inject_managed_block`` reports "unchanged" and writes
    nothing when the block already matches, so repeat sessions are no-ops.
  * ``routing_injected_at`` (ISO date) is written to the data dir ONCE, the
    first time the block lands. ``routing_enabled_since()`` / the credit
    window helpers expose it so the savings code only credits routing from
    the date the guidance actually existed.
  * Opt-out: ``TOKEN_OPTIMIZER_ROUTING=0`` skips injection.
    ``measure.py inject-routing --uninstall`` removes the block everywhere
    and records ``routing_removed_at``; SessionStart never re-adds it after
    that (a later manual ``inject-routing`` re-enables and clears the mark).
  * First-run audit offer: once per install, one short line suggests
    ``/token-optimizer`` to apply setup trims, only while the trim ledger
    (``structural_ledger``) has no recorded trims.

Everything fails open: any error means "do nothing this session", never a
broken hook.
"""
from __future__ import annotations

import os
import json
from datetime import datetime, timezone
from pathlib import Path

from . import _compat

ENV_FLAG = "TOKEN_OPTIMIZER_ROUTING"
SECTION = "MODEL_ROUTING"
INJECTED_MARKER = "routing_injected_at"
REMOVED_MARKER = "routing_removed_at"
AUDIT_OFFER_MARKER = "once-audit-offer.json"
FAILURE_MARKER = "routing_injection_failure"

# Runtimes whose instruction file this feature knows how to write.
_ROUTING_RUNTIMES = ("claude", "codex")

AUDIT_OFFER_TEXT = (
    "[Token Optimizer] Run /token-optimizer to find and apply setup trims."
)

_INJECT_NOTICE = (
    "[Token Optimizer] Added model-routing guidance to {name}. "
    "Opt out: TOKEN_OPTIMIZER_ROUTING=0."
)


def _facade(f=None):
    return f if f is not None else _compat.facade()


def _data_dir(f=None):
    return Path(_facade(f).SNAPSHOT_DIR)


def _marker_path(f, name):
    return _data_dir(f) / name


def _read_marker(f, name):
    """Marker contents (stripped) or None. Never raises."""
    try:
        text = _marker_path(f, name).read_text(encoding="utf-8").strip()
        return text or None
    except (OSError, UnicodeDecodeError):
        return None


def _write_marker(f, name, value):
    """Overwrite a marker. Returns True on write, False on any error."""
    try:
        path = _marker_path(f, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return True
    except OSError:
        return False


def _write_marker_once(f, name, value):
    """Create a marker only when absent (O_EXCL). True when this call wrote it."""
    try:
        path = _marker_path(f, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value)
    except OSError:
        pass
    return True


def _utc_today():
    """ISO date (UTC) used for markers and the pinned block timestamp."""
    return datetime.now(timezone.utc).date().isoformat()


# ------------------------------------------------------------------ markers


def routing_injected_at(f=None, data_dir=None):
    """ISO date Token Optimizer's routing block first landed, or None."""
    if data_dir is not None:
        f = _compat.namespace({"SNAPSHOT_DIR": data_dir})
    return _read_marker(f, INJECTED_MARKER)


def routing_removed_at(f=None, data_dir=None):
    """ISO date the routing block was uninstalled, or None."""
    if data_dir is not None:
        f = _compat.namespace({"SNAPSHOT_DIR": data_dir})
    return _read_marker(f, REMOVED_MARKER)


def routing_enabled_since(f=None, data_dir=None):
    """Start of the routing-enabled window (``routing_injected_at``), or None.

    This is the date the savings code credits realized routing from. It stays
    set after an uninstall: pair it with :func:`routing_disabled_at` for the
    end of the window, or use :func:`routing_credit_window` for both.
    """
    return routing_injected_at(f, data_dir)


def routing_disabled_at(f=None, data_dir=None):
    """End of the routing-enabled window (``routing_removed_at``), or None."""
    return routing_removed_at(f, data_dir)


def routing_credit_window(f=None, data_dir=None):
    """``(enabled_since, disabled_at)`` ISO dates; either side may be None."""
    return routing_enabled_since(f, data_dir), routing_disabled_at(f, data_dir)


HISTORY = 'routing_intervals.jsonl'


def _intervals(directory, injected=None, removed=None):
    path = Path(directory) / HISTORY
    try:
        events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]
    except FileNotFoundError:
        return [(injected, removed)] if injected else []
    result = []
    for event in events:
        if event.get('injected_at') and (not result or result[-1][1]):
            result.append((event['injected_at'], None))
        elif event.get('removed_at') and result and result[-1][1] is None:
            result[-1] = (result[-1][0], event['removed_at'])
    return result


def _append_interval(directory, event, injected=None, removed=None):
    path = Path(directory) / HISTORY
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() and injected:
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'injected_at': injected}) + '\n')
            if removed:
                stream.write(json.dumps({'removed_at': removed}) + '\n')
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(event) + '\n')


def routing_credit_intervals(f=None, data_dir=None):
    if data_dir is not None:
        f = _compat.namespace({'SNAPSHOT_DIR': data_dir})
    return _intervals(_data_dir(f), routing_injected_at(f), routing_removed_at(f))


# ------------------------------------------------------- instruction files


def managed_instruction_candidates(f):
    """Same candidate list the ``inject-routing`` CLI resolves against.

    Claude Code: project CLAUDE.md files then the global ``~/.claude/CLAUDE.md``.
    Codex: project AGENTS.md files then the global ``$CODEX_HOME/AGENTS.md``.
    """
    f = _facade(f)
    cwd = Path.cwd()
    if f.detect_runtime() == "codex":
        return [cwd / "AGENTS.md", cwd / "AGENTS.override.md",
                Path(f.runtime_home()) / "AGENTS.md"]
    return [cwd / "CLAUDE.md", cwd / ".claude" / "CLAUDE.md",
            Path(f.CLAUDE_DIR) / "CLAUDE.md"]


def global_instruction_file(f):
    """The user's GLOBAL instruction file (last candidate), never a project one."""
    candidates = managed_instruction_candidates(f)
    return candidates[-1] if candidates else None


# ----------------------------------------------------------------- opt-out


def _env_disabled(environ):
    raw = (environ.get(ENV_FLAG) or "").strip().lower()
    return raw in ("0", "false", "no", "off")


# --------------------------------------------------------------- injection


def _routing_block(f):
    """The MODEL_ROUTING block WITHOUT the volatile stats line and WITHOUT a
    history scan (``trends={}`` skips ``_collect_trends_data``)."""
    return _facade(f).generate_model_routing_block(trends={})


def _inject_once(f, notices):
    """Inject the routing block into the global instruction file. Idempotent."""
    f = _facade(f)
    if routing_removed_at(f):
        return  # uninstalled on purpose; never re-add
    block = _routing_block(f)
    if not block:
        return
    target = global_instruction_file(f)
    if target is None:
        return
    injected = routing_injected_at(f)
    # Pin the block's "updated" stamp to the first-injection date so the bytes
    # are identical on every later day (cache stability, no daily rewrite).
    stamp = f"{injected or _utc_today()}T00:00"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        import injection
        result = injection.inject_managed_block(
            str(target), SECTION, block, timestamp=stamp)
    except Exception as exc:
        _write_marker(f, FAILURE_MARKER, type(exc).__name__)
        return
    action = result.get("action")
    if action in ("inserted", "replaced", "unchanged"):
        try:
            _marker_path(f, FAILURE_MARKER).unlink(missing_ok=True)
        except OSError:
            pass
    else:
        _write_marker(f, FAILURE_MARKER, "injection failed")
    if action in ("inserted", "replaced", "unchanged") and not injected:
        # The block is present (fresh write or already there): stamp the
        # credit-window start once. "unchanged" still counts -- a missing
        # marker with a live block means the date was lost, not the guidance.
        record_injection(f)
    if action in ("inserted", "replaced"):
        notices.append(_INJECT_NOTICE.format(name=target.name))


def record_injection(f=None):
    """Stamp ``routing_injected_at`` and clear ``routing_removed_at``.

    Called by ``measure.py inject-routing`` after a real (non-dry-run)
    injection so manual installs feed the same credit window as the
    SessionStart path. A re-enable after ``--uninstall`` restarts the window
    at today; a first injection keeps the earliest date (write-once).
    """
    f = _facade(f)
    injected, removed = routing_credit_window(f)
    if not injected or removed:
        _append_interval(_data_dir(f), {'injected_at': _utc_today()}, injected, removed)
    if _read_marker(f, REMOVED_MARKER) is not None:
        _write_marker(f, INJECTED_MARKER, _utc_today())
    else:
        _write_marker_once(f, INJECTED_MARKER, _utc_today())
    try:
        _marker_path(f, REMOVED_MARKER).unlink(missing_ok=True)
    except OSError:
        pass


def uninstall_routing(f=None):
    """Remove MODEL_ROUTING blocks from every instruction candidate and record
    ``routing_removed_at``. Returns {"files": [paths removed]}."""
    f = _facade(f)
    removed = []
    try:
        import injection
        for candidate in managed_instruction_candidates(f):
            if not candidate.exists():
                continue
            result = injection.remove_managed_block(str(candidate), SECTION)
            if result.get("action") == "removed":
                removed.append(str(candidate))
    except Exception:
        pass
    injected, prior_removed = routing_credit_window(f)
    if injected and not prior_removed:
        _append_interval(_data_dir(f), {'removed_at': _utc_today()}, injected, prior_removed)
    _write_marker(f, REMOVED_MARKER, _utc_today())
    return {"files": removed}


# ------------------------------------------------------------ audit offer


def _trims_recorded(f):
    """True when the setup-trim ledger holds any trim. Fails closed (True)
    when the ledger cannot be read, so a broken read never nags."""
    try:
        from . import structural_ledger
        return bool(structural_ledger.load_trims(_data_dir(f)))
    except Exception:
        return True


def _maybe_offer_audit(f, notices):
    """Once per install: one line suggesting /token-optimizer for setup trims,
    only while no setup trims are recorded. Marker written before the notice
    so a failed print cannot re-nag every session."""
    f = _facade(f)
    if _read_marker(f, AUDIT_OFFER_MARKER) is not None:
        return
    if _trims_recorded(f):
        _write_marker_once(f, AUDIT_OFFER_MARKER, _utc_today())
        return
    _write_marker_once(f, AUDIT_OFFER_MARKER, _utc_today())
    notices.append(AUDIT_OFFER_TEXT)


# ------------------------------------------------------------- entry point


def session_start(f=None, environ=None):
    """SessionStart body. Returns user-facing notice strings (possibly empty).

    Cheap by contract: env checks, two marker stats, one instruction-file
    read, one small ledger read. No history or transcript scans.

    Under pytest this is a no-op: every write target (instruction file,
    SNAPSHOT_DIR markers, trim ledger) resolves to the developer's real home
    unless a test redirects it, and the suite drives hooks in-process. Same
    guard as sessions.py / cache_policy.py. Tests that want the real path
    delete PYTEST_CURRENT_TEST and point every env override at tmp dirs.
    """
    f = _facade(f)
    environ = os.environ if environ is None else environ
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return []
    notices = []
    try:
        runtime = f.detect_runtime()
    except Exception:
        return notices
    if runtime not in _ROUTING_RUNTIMES:
        return notices
    if not _env_disabled(environ):
        try:
            _inject_once(f, notices)
        except Exception:
            pass
    try:
        _maybe_offer_audit(f, notices)
    except Exception:
        pass
    return notices
