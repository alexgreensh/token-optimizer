"""Warn or ask before a large Claude Code session rewrites its cold cache."""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import re
import signal
import sqlite3
import tempfile
import time
from contextlib import closing, contextmanager, nullcontext
from datetime import datetime, timedelta
from pathlib import Path


LABEL = "Cold-session guard"
ESTIMATE_KEY = "cold_guard_estimated"
MODES = ("off", "warn", "ask")
CHOICES = ("continue", "compact", "fresh", "unknown")
_PASS = {"kind": "pass"}
_MIN_CONTEXT = 300_000
_HOUR = 3600
_CHOICE_WINDOW = 600
_RETENTION = 14 * 86400
_CHECKPOINT_BUDGET = 5.0
_WARNING_BUDGET = 0.25
_OUTPUT_BUDGET = 0.10
_TAIL_BYTES = 1024 * 1024
_MARKER_NAME = re.compile(r"[0-9a-f]{64}\.json\Z")
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
_MODEL_ID = re.compile(r"[A-Za-z0-9_.:@/-]{1,160}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{32}:[0-9a-f]{64}\Z")
_WIN_WORKER = """
import json, sys
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from _measure import cold_guard
with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
    import measure
    path, session_id, checkpoint_dir = json.load(sys.stdin)
    measure.CHECKPOINT_DIR = Path(checkpoint_dir)
    saved = cold_guard._checkpoint_saved(measure, Path(path), session_id, float(sys.argv[2]))
print(json.dumps(saved))
"""


class BudgetExceeded(BaseException):
    """Interrupt a guard without exiting the runner or being swallowed by a checkpoint."""


@contextmanager
def deadline(seconds):
    """Use the earlier alarm when nested inside the runner's shorter budget."""
    if not hasattr(signal, "setitimer"):
        # Windows bounds checkpoint capture in a child process.
        yield
        return
    old_wait, old_interval = signal.getitimer(signal.ITIMER_REAL)
    if 0 < old_wait <= seconds:
        yield
        return
    old_handler = signal.getsignal(signal.SIGALRM)
    started = time.monotonic()

    def expired(_signum, _frame):
        raise BudgetExceeded(seconds)

    signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
        if time.monotonic() - started >= seconds:
            raise BudgetExceeded(seconds)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_wait:
            signal.setitimer(
                signal.ITIMER_REAL, max(1e-6, old_wait - (time.monotonic() - started)), old_interval,
            )


def bounded_evaluate(facade, hook_input, now, seconds):
    expires = time.monotonic() + seconds
    with deadline(seconds):
        return evaluate(facade, hook_input, now, expires)


def _checkpoint_in_worker(facade, path, session_id, seconds):
    """Give only checkpoint capture a killable process on Windows."""
    import subprocess
    import sys

    try:
        child = subprocess.run(
            [sys.executable, "-c", _WIN_WORKER, str(Path(__file__).resolve().parents[1]), str(seconds)],
            input=json.dumps([str(path), session_id, str(facade.CHECKPOINT_DIR)]),
            capture_output=True, text=True,
            encoding="utf-8", timeout=seconds, check=True,
        )
        return json.loads(child.stdout) is True
    except (subprocess.SubprocessError, OSError, ValueError):
        return False


def _configured_mode(facade):
    value = facade._read_config_flag("cold_guard_mode", "warn")
    return value if value in MODES else "warn"


def _mode(facade):
    value = os.environ.get("TOKEN_OPTIMIZER_COLD_GUARD")
    return value if value in MODES else _configured_mode(facade)


def _marker_dir(facade):
    return Path(facade.SNAPSHOT_DIR) / "cold-guard"


def _marker_path(directory, session_id):
    return directory / (hashlib.sha256(session_id.encode("utf-8")).hexdigest() + ".json")


def _marker_paths(directory):
    try:
        with os.scandir(directory) as entries:
            yield from (
                directory / entry.name for entry in entries
                if _MARKER_NAME.fullmatch(entry.name) and entry.is_file(follow_symlinks=False)
            )
    except OSError:
        pass


def _read_marker(path):
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError("invalid cold-guard marker")
    return data


def _maybe_marker(path):
    try:
        return _read_marker(path)
    except FileNotFoundError:
        return {}


def _write_marker(path, state):
    fd, temp = tempfile.mkstemp(prefix=".marker-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        if os.name == "posix":
            dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        Path(temp).unlink(missing_ok=True)


def _marker_lock(facade, directory):
    return facade.lease_lock(directory / ".markers.lease", acquire_timeout=0.025, reclaim_released=True)


def _prune(facade, directory, now):
    cutoff = now - _RETENTION
    stale = []
    for path in _marker_paths(directory):
        try:
            if path.stat().st_mtime < cutoff:
                stale.append(path)
        except OSError:
            continue
        if len(stale) == 50:
            break
    if not stale:
        return
    with _marker_lock(facade, directory) as held:
        if held:
            for path in stale:
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                except OSError:
                    pass


def _project_id(directory):
    return hashlib.sha256(directory.name.encode("utf-8")).hexdigest()


def _transcript(facade, hook_input, session_id):
    path = facade._confine_transcript_path(hook_input.get("transcript_path"))
    projects = (facade.CLAUDE_DIR / "projects").resolve()
    if path and path.parent.parent == projects and path.name == f"{session_id}.jsonl":
        return path
    return None


def _price_card(facade, model, context):
    tiers = facade.PRICING_TIERS
    tier = tiers.get(facade._load_pricing_tier(), tiers["anthropic"])
    return facade._counted_exact_card(model, tier, prompt_tokens=context) or {}


def _write_key(ttl):
    return "cache_write_1h" if ttl == "1h" else "cache_write"


def _rate(card, key):
    rate = card.get(key)
    return rate if isinstance(rate, (int, float)) and math.isfinite(rate) and rate >= 0 else None


def texts(facade, context, model, idle, ttl):
    minutes = int(idle // 60)
    hours, minutes = divmod(minutes, 60)
    size = f"{context / 1000:,.0f}K tokens at write price"
    rate = _rate(_price_card(facade, model, context), _write_key(ttl))
    if rate is not None:
        size += f", about ${context * rate / 1_000_000:,.2f} at API rates"
    cold = f"cache went cold (idle {hours}h {minutes}m)."
    return {
        "warn": (
            f"Token Optimizer: {cold} This message re-reads {size}. "
            "To be asked first next time: measure.py cold-guard set ask"
        ),
        "block": (
            "Token Optimizer held this message. Nothing was sent and nothing is lost: your text is below.\n"
            f"{cold.capitalize()} Sending re-reads {size}. A checkpoint is saved.\n"
            "  1. Start fresh: type /clear, then send your message again. The checkpoint joins it. Cheapest.\n"
            "  2. Compact: type /compact, then send again. Pays one full re-read now; later messages get cheaper.\n"
            "  3. Continue: send the same message again. It goes through.\n"
            "This will not stop you again for this pause. Turn off: measure.py cold-guard set warn"
        ),
    }


def _blocks(marker):
    prior = marker.get("previous_blocks", [])
    if isinstance(prior, list):
        for block in prior:
            if isinstance(block, dict) and isinstance(block.get("told_at"), (float, int)):
                yield block
    if marker.get("kind") == "block" and isinstance(marker.get("told_at"), (float, int)):
        yield marker


def _recent_block(marker, now):
    return any(0 <= now - block["told_at"] < _HOUR for block in _blocks(marker))


def _model_id(value):
    return value if isinstance(value, str) and _MODEL_ID.fullmatch(value) else None


def _checkpoint_saved(facade, path, session_id, seconds):
    try:
        with deadline(seconds):
            checkpoint = facade.compact_capture(transcript_path=str(path), session_id=session_id)
            return bool(checkpoint and facade._checkpoint_has_restorable_body(checkpoint))
    except (Exception, BudgetExceeded, facade._HookTimeout):
        return False


def _prompt_fingerprint(prompt, salt):
    digest = hashlib.blake2b(prompt.strip().encode("utf-8", errors="surrogatepass"),
                             key=bytes.fromhex(salt), digest_size=32)
    return f"{salt}:{digest.hexdigest()}"


def _publish(facade, path, state):
    """Recheck the decision while committing it so racing hooks hold only once."""
    with _marker_lock(facade, path.parent) as held:
        if not held:
            return False
        current = _maybe_marker(path)
        now = state["told_at"]
        told_at = current.get("told_at")
        if current.get("gap_key") == state["gap_key"]:
            return False
        if isinstance(told_at, (float, int)) and told_at > now:
            return False
        if _recent_block(current, now):
            state["kind"] = "warn"
            state.pop("prompt_fingerprint", None)
        history = [
            {key: value for key, value in block.items() if key != "previous_blocks"}
            for block in _blocks(current) if now - block["told_at"] < _RETENTION
        ]
        if history:
            state["previous_blocks"] = history
        _write_marker(path, state)
        return True


def _bounded_last_usage(path):
    """Inspect a fixed transcript suffix where the hook has no alarm."""
    try:
        with path.open("rb") as stream:
            start = max(0, stream.seek(0, os.SEEK_END) - _TAIL_BYTES)
            stream.seek(start)
            lines = stream.read(_TAIL_BYTES).split(b"\n")
    except OSError:
        return None, None, None
    for line in reversed(lines[bool(start):]):
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(row, dict) and not _delegated(row) and (found := _assistant_usage(row)):
            return *found, row.get("timestamp") or row.get("ts")
    return None, None, None


def evaluate(facade, hook_input, now, expires=None):
    """Return a decision without emitting output or retaining any prompt text."""
    directory = _marker_dir(facade)
    _prune(facade, directory, now)
    prompt = hook_input.get("prompt") or hook_input.get("current_prompt") or hook_input.get("user_prompt")
    session_id = hook_input.get("session_id")
    if (not isinstance(prompt, str) or not prompt.strip() or prompt.lstrip().startswith("/")
            or "agent_id" in hook_input or "TOKEN_OPTIMIZER_KEEPWARM_PING" in os.environ
            or not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id)):
        return dict(_PASS)
    mode = _mode(facade)
    if mode == "off":
        return dict(_PASS)
    path = _transcript(facade, hook_input, session_id)
    if path is None:
        return dict(_PASS)
    alarm_available = hasattr(signal, "setitimer")
    usage, model, ts = facade._keepwarm_last_usage(path) if alarm_available else _bounded_last_usage(path)
    model = _model_id(model)
    context = facade._context_token_count(usage)
    last = facade._keepwarm_iso_to_epoch(ts)
    if context < _MIN_CONTEXT or last is None:
        return dict(_PASS)
    ttl = "1h" if facade._keepwarm_ttl_kind(usage) == "1h" else "5m"
    lifetime = _HOUR if ttl == "1h" else 300
    idle = now - last
    if idle < max(_HOUR, lifetime):
        return dict(_PASS)
    marker_path = _marker_path(directory, session_id)
    current = _maybe_marker(marker_path)
    if current.get("gap_key") == str(ts):
        return dict(_PASS)
    text = texts(facade, context, model, idle, ttl)
    state = {
        "gap_key": str(ts), "told_at": now, "kind": "warn", "context": context, "model": model,
        "ttl": ttl, "session_id": session_id, "project_id": _project_id(path.parent),
    }
    if (mode == "ask" and not _recent_block(current, now) and not hook_input.get("attachments")
            and "[Image #" not in prompt and "[Pasted text #" not in prompt):
        from . import keepwarm_safety
        if keepwarm_safety.session_owner(facade.SNAPSHOT_DIR, session_id, now):
            remaining = _CHECKPOINT_BUDGET if expires is None else expires - time.monotonic() - _WARNING_BUDGET
            save = _checkpoint_saved if alarm_available else _checkpoint_in_worker
            if remaining > 0 and save(facade, path, session_id, min(remaining, _CHECKPOINT_BUDGET)):
                state["kind"] = "block"
                state["prompt_fingerprint"] = _prompt_fingerprint(prompt, os.urandom(16).hex())
    if expires is not None and time.monotonic() >= expires:
        raise BudgetExceeded
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not _publish(facade, marker_path, state):
            return dict(_PASS)
    except OSError:
        return dict(_PASS)
    kind = state["kind"]
    # A durable decision should still reach the host if bookkeeping stalls.
    log_budget = None if expires is None else expires - time.monotonic() - _OUTPUT_BUDGET
    if log_budget is None or log_budget > 0:
        try:
            with nullcontext() if log_budget is None else deadline(log_budget):
                facade._log_compression_event("cold_guard", session_id=session_id, detail=kind, tier="estimated")
        except BudgetExceeded:
            pass
    return {"kind": kind, "text": text[kind]}


def _markers(facade):
    for path in _marker_paths(_marker_dir(facade)):
        try:
            yield path, _read_marker(path)
        except (OSError, ValueError):
            continue


def _delegated(row):
    return row.get("isSidechain") or row.get("agentId") or row.get("teamName")


def _rows(facade, path):
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or _delegated(row):
                continue
            stamp = facade._keepwarm_iso_to_epoch(row.get("timestamp") or row.get("ts"))
            if stamp is not None:
                yield stamp, row


def _assistant_usage(row):
    message = row.get("message")
    if row.get("type") == "assistant" and isinstance(message, dict):
        usage = message.get("usage")
        model = message.get("model") or row.get("model")
        if isinstance(usage, dict) and usage and model != "<synthetic>":
            return usage, model
    return None


def _user_text(row):
    message = row.get("message")
    if (row.get("type") != "user" or row.get("isMeta") or row.get("isCompactSummary")
            or not isinstance(message, dict) or message.get("role") != "user"):
        return None
    content = message.get("content")
    if isinstance(content, str):
        if not content.lstrip().startswith(("<command-name>", "<local-command-stdout>")):
            return content
        return None
    if isinstance(content, list) and content and all(
        isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        for part in content
    ):
        return "\n".join(part["text"] for part in content)
    return ""


def _handover(facade, usage, model):
    splits = usage.get("cache_creation")
    splits = splits if isinstance(splits, dict) else {}
    hour, short = (max(0, facade._safe_int(splits.get(key) or usage.get(key))) for key in (
        "ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens",
    ))
    total = facade._safe_int(usage.get("cache_creation_input_tokens"))
    return {"model": _model_id(model), "context": facade._context_token_count(usage),
            "write_1h": hour, "write_5m": short, "write_unknown": max(0, total - hour - short)}


def _fresh_session(facade, project, old, block):
    fingerprint = block.get("prompt_fingerprint")
    if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
        return None
    salt = fingerprint.split(":", 1)[0]
    told_at = block["told_at"]
    until = told_at + _CHOICE_WINDOW
    candidate = None
    for path in project.glob("*.jsonl"):
        if path == old or path.is_symlink() or not _SESSION_ID.fullmatch(path.stem):
            continue
        try:
            if path.stat().st_mtime < told_at:
                continue
            with _read_rows(facade, path) as rows:
                first = next(rows, None)
                if not first or not told_at <= first[0] <= until:
                    continue
                for stamp, row in itertools.chain((first,), rows):
                    if not told_at <= stamp <= until or _assistant_usage(row):
                        break
                    prompt = _user_text(row)
                    if prompt is None:
                        continue
                    if _prompt_fingerprint(prompt, salt) == fingerprint:
                        if candidate:
                            return None  # Two sessions resent the same held prompt.
                        candidate = {"fresh_session_id": path.stem}
                        usage = next((use for _, reply in rows if (use := _assistant_usage(reply))), None)
                        if usage is not None:
                            candidate["handover"] = _handover(facade, *usage)
                    break
        except OSError:
            continue
    return candidate


@contextmanager
def _read_rows(facade, path):
    rows = _rows(facade, path)
    try:
        yield rows
    finally:
        rows.close()


def _choice(facade, project, block):
    session_id = block.get("session_id")
    if not project or not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        return {"choice": "unknown"}
    old = project / f"{session_id}.jsonl"
    if old.is_symlink():
        return {"choice": "unknown"}
    told_at = block["told_at"]
    compact = False
    try:
        with _read_rows(facade, old) as rows:
            for stamp, row in rows:
                if not told_at <= stamp <= told_at + _CHOICE_WINDOW:
                    continue
                if row.get("type") == "system" and row.get("subtype") == "compact_boundary":
                    compact = True
                if _assistant_usage(row):
                    return {"choice": "compact" if compact else "continue"}
    except OSError:
        return {"choice": "unknown"}
    fresh = _fresh_session(facade, project, old, block)
    return {"choice": "fresh", **fresh} if fresh else {"choice": "unknown"}


def resolve(facade, now):
    """Reconcile holds from transcript evidence. Never called on prompt submission."""
    _prune(facade, _marker_dir(facade), now)
    projects = None
    for path, marker in _markers(facade):
        pending = [
            block for block in _blocks(marker)
            if now - block["told_at"] >= _CHOICE_WINDOW
            and block.get("choice") not in ("continue", "compact")
        ]
        if not pending:
            continue
        if projects is None:
            root = facade.CLAUDE_DIR / "projects"
            projects = {_project_id(p): p for p in root.glob("*") if p.is_dir() and not p.is_symlink()}
        before = json.dumps(marker)
        for block in pending:
            outcome = _choice(facade, projects.get(block.get("project_id")), block)
            for key in ("fresh_session_id", "handover"):
                if key not in outcome:
                    block.pop(key, None)
            block.update(outcome)
        try:
            with _marker_lock(facade, path.parent) as held:
                if held and json.dumps(_maybe_marker(path)) == before and json.dumps(marker) != before:
                    _write_marker(path, marker)
        except (OSError, ValueError):
            pass


def _net_estimate(facade, block):
    handover = block.get("handover")
    if handover is None or handover["write_unknown"]:
        return None
    old = _price_card(facade, block["model"], block["context"])
    old_write = _rate(old, _write_key(block["ttl"]))
    old_read = _rate(old, "cache_read")
    if old_write is None or old_read is None:
        return None
    gross = block["context"] * (old_write - old_read)
    recipient = _price_card(facade, handover["model"], handover["context"])
    cost = 0.0
    for ttl in ("1h", "5m"):
        tokens = handover[f"write_{ttl}"]
        if tokens:
            write = _rate(recipient, _write_key(ttl))
            if write is None:
                return None
            cost += tokens * write
    old_input = _rate(old, "input")
    new_input = _rate(recipient, "input")
    units = gross / old_input if old_input else 0.0
    if cost and new_input:
        units -= cost / new_input
    return gross / 1_000_000, cost / 1_000_000, units


def _overlapping_credits(facade, days, since, now, handover, mod_fresh):
    """Use the receipt identities from the pools that contribute to this report."""
    from . import mod_fresh_booking

    restores = set()
    mod_starts = {}
    handover_counted = float((handover or {}).get("cost_saved_usd") or 0) > 0
    mod_counted = mod_fresh_booking.additive(mod_fresh, handover) > 0
    if handover_counted or mod_counted:
        cutoff = datetime.fromtimestamp(now) - timedelta(days=days)
        with closing(facade._open_trends_db_readonly(timeout=1.0)) as conn:
            if handover_counted:
                table = facade._counted_table(conn, "savings_events")
                restored_ids = {sid for sid, in conn.execute(
                    facade._CHECKPOINT_RESTORED_SQL[table], (cutoff.isoformat(),)
                )}
                restores = restored_ids.intersection(
                    Path(path).stem for path, in conn.execute(
                        "SELECT jsonl_path FROM session_log WHERE date >= ?", (cutoff.date().isoformat(),)
                    ) if path
                )
            if mod_counted:
                mod_starts = {sid: datetime.fromisoformat(stamp).timestamp() for sid, stamp in conn.execute(
                    "SELECT session_uuid, timestamp FROM mod_fresh_receipts "
                    "WHERE timestamp >= ? AND estimated_usd > 0", (since or cutoff.isoformat(),)
                )}
    return restores, mod_starts


def estimate(facade, days=30, since=None, now=None, *, handover=None, mod_fresh=None):
    """Read the signed estimate from retained, resolved choices."""
    now = time.time() if now is None else now
    days = days or 30
    cutoff = facade._keepwarm_iso_to_epoch(since) if since else None
    if cutoff is None:
        cutoff = now - days * 86400
    fresh = sorted(
        (block for _, marker in _markers(facade) for block in _blocks(marker)
         if block.get("choice") == "fresh" and block.get("fresh_session_id") and block.get("session_id")
         and isinstance(block.get("prompt_fingerprint"), str) and _FINGERPRINT.fullmatch(block["prompt_fingerprint"])),
        key=lambda block: (block["told_at"], block["session_id"]),
    )
    gross = cost = units = 0.0
    priced = unpriced = 0
    if fresh:
        try:
            restores, mod_starts = _overlapping_credits(facade, days, since, now, handover, mod_fresh)
        except (OSError, sqlite3.Error, ValueError):
            # An unavailable overlap check cannot support an additional credit.
            fresh = []
    seen = set()
    for block in fresh:
        target = block["fresh_session_id"]
        if target in seen:
            continue
        seen.add(target)
        if not cutoff <= block["told_at"] <= now:
            continue
        mod_at = mod_starts.get(block["session_id"])
        if target in restores or (mod_at is not None and block["told_at"] <= mod_at <= block["told_at"] + _CHOICE_WINDOW):
            continue
        if facade._resume_lean_already_credited(block["session_id"]):
            continue
        try:
            values = _net_estimate(facade, block)
        except (KeyError, TypeError, ValueError):
            values = None
        if values is None:
            unpriced += 1
            continue
        avoided_cost, handover_cost, net_units = values
        gross += avoided_cost
        cost += handover_cost
        units += net_units
        priced += 1
    return {"label": LABEL, "events": priced, "unpriced_events": unpriced,
            "gross_usd": round(gross, 4), "handover_cost_usd": round(cost, 4),
            "cost_saved_usd": round(gross - cost, 4), "limit_units_saved": round(units, 2)}


def on_savings_refresh(facade, summary):
    from . import savings_story

    now = time.time()
    resolve(facade, now)
    previous = summary.get(ESTIMATE_KEY)
    current = estimate(
        facade, days=summary.get("period_days"), since=savings_story.install_anchor(), now=now,
        handover=summary.get("handover_rerun"), mod_fresh=summary.get("mod_fresh_estimated"),
    )
    summary[ESTIMATE_KEY] = current
    if previous != current and "action_savings" in summary:
        summary["action_savings"] = facade._action_savings_components(summary, summary.get("counted_period"))
        if "savings_headline" in summary:
            from . import savings_headline
            summary["savings_headline"] = savings_headline.build(summary)
    if previous != current and summary.get("since_install") is not None:
        summary["since_install"] = facade._savings_since_install()


def print_estimate(summary, cents, format_cents, period):
    row = summary.get(ESTIMATE_KEY) or {}
    if row.get("events"):
        amount = cents.get(ESTIMATE_KEY, 0)
        sign = "-" if amount < 0 else ""
        print(f"  + est. {LABEL}: {sign}${format_cents(abs(amount))}{period} "
              f"({row['events']:,} fresh starts, net of handover writes) [estimated]")
    if row.get("unpriced_events"):
        print(f"  i  {LABEL}: {row['unpriced_events']:,} fresh starts without complete cache-write pricing")


def _opportunity(facade, days):
    drops = facade._estimate_cache_drop_savings(days=days)
    return {"days": days, "events": int(drops.get("est_drops") or 0),
            "cost_saved_usd": float(drops.get("cost_saved_usd") or 0)}


def pool(facade, days=30):
    resolve(facade, time.time())
    return _opportunity(facade, days)


def plan_item(facade):
    if _mode(facade) == "ask":
        return None
    opportunity = _opportunity(facade, 30)
    events, dollars = opportunity["events"], opportunity["cost_saved_usd"]
    if events < 3:
        return None
    return {
        "id": "cold_guard:ask", "kind": "cold_guard",
        "label": "Ask before re-reading a cold session over 300K",
        "tokens_per_request": None, "net_usd_month": None, "own_cost_usd_month": 0.0,
        "basis": f"{events:,} cold resumes cost ${dollars:,.2f} in your last 30 days; "
                 "the saving depends on what you choose each time",
        "undo": "measure.py cold-guard set warn", "default_selected": True,
    }


def _set_mode(facade, mode):
    facade._write_config_flag("cold_guard_mode", mode)
    if facade._read_config_flag("cold_guard_mode") != mode:
        raise OSError("cold-guard mode was not written")


def apply_item(facade, item):
    previous = _configured_mode(facade)
    _set_mode(facade, "ask")
    return {'ok': True, 'before': previous, 'after': 'ask'}


def undo_item(facade, receipt):
    before = receipt.get('before')
    if before not in MODES or _configured_mode(facade) != receipt.get('after'):
        return False
    _set_mode(facade, before)
    return True


def main(facade, args):
    parser = argparse.ArgumentParser(prog="measure.py cold-guard")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "pool"):
        subcommands.add_parser(command).add_argument("--json", action="store_true")
    subcommands.add_parser("set").add_argument("mode", choices=MODES)
    options = parser.parse_args(args)
    if options.command == "set":
        _set_mode(facade, options.mode)
        effective = _mode(facade)
        print(f"{LABEL} set to {options.mode}."
              + (f" The environment overrides it to {effective}." if effective != options.mode else ""))
        return 0
    opportunity = pool(facade)
    result = opportunity
    if options.command == "status":
        counts = dict.fromkeys(CHOICES, 0)
        for _, marker in _markers(facade):
            for block in _blocks(marker):
                choice = block.get("choice")
                if choice in counts:
                    counts[choice] += 1
        result = {"mode": _mode(facade), "pool": opportunity, "counts": counts}
    if options.json:
        print(json.dumps(result, indent=2))
    else:
        if options.command == "status":
            print(f"{LABEL} mode: {result['mode']}")
            print("Choices: " + ", ".join(f"{choice} {count}" for choice, count in result["counts"].items()))
        print(f"Last {opportunity['days']} days: {opportunity['events']:,} cold resumes, "
              f"${opportunity['cost_saved_usd']:,.2f} of cache rewrites at API rates.")
    return 0
