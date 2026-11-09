"""Cursor adapter for the fleet auditor (Token Optimizer's Cursor data planes).

Split out of fleet.py to keep it under the repo's 1,000-line file-size ratchet
(scripts/check_file_size.py). Base classes come from fleet_base (single class
identity whether fleet.py is imported or run as __main__). HOME is NOT imported
from fleet: fleet.py injects _HOME_RESOLVER at import time, a lambda reading
its own module global, so the tests' monkeypatch of fleet.HOME keeps working
and `python3 fleet.py` never double-loads a second fleet module. Standalone
import falls back to Path.home().
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from shared import normalize_model_name  # noqa: E402  (fleet.py sets sys.path first)

from fleet_base import AgentRun, BaseAdapter, TokenBreakdown, _is_run_collected

try:  # noqa: E402
    import cursor_session as _cursor_session  # type: ignore
    import cursor_state as _cursor_state  # type: ignore
except Exception:  # pragma: no cover - optional Cursor adapter dependency
    # Standalone copies (no token-optimizer/scripts beside this file) still
    # collect Cursor tallies through the adapter's native fallback reader.
    _cursor_session = None
    _cursor_state = None

_HOME_RESOLVER = None  # injected by fleet.py at import: lambda reading fleet's HOME global

def _cursor_home() -> Path:
    """Cursor's home directory for the fleet collector.

    TOKEN_OPTIMIZER_CURSOR_HOME wins when it passes the same confinement
    runtime_env.cursor_home() applies to the hook bridge (absolute, existing,
    non-symlink dir, confined under the home root, plus the WSL /mnt opt-in);
    a rejected override falls back to the default so the reader and the bridge
    agree on one home. Cursor has no documented CURSOR_HOME of its own.
    """
    root = _HOME_RESOLVER() if _HOME_RESOLVER is not None else Path.home()
    override = os.environ.get("TOKEN_OPTIMIZER_CURSOR_HOME", "").strip()
    if override:
        candidate = Path(override).expanduser()
        if _cursor_home_override_safe(candidate, root):
            return candidate
    return root / ".cursor"


def _cursor_home_override_safe(candidate: Path, root: Path) -> bool:
    """Mirror runtime_env's safe-home checks, scoped to fleet's HOME root.

    runtime_env._is_safe_home_dir confines overrides under runtime_env's own
    home root; fleet's root is _HOME_RESOLVER() (== Path.home() in production,
    a tmp dir in tests), so the containment check is re-implemented here.
    runtime_env is imported lazily only for the WSL /mnt opt-in and may be
    absent in standalone copies of this adapter.
    """
    try:
        home = Path(root).resolve(strict=False)
        resolved = candidate.resolve(strict=False)
        if (candidate.is_absolute()
                and resolved != home
                and resolved.is_relative_to(home)
                and candidate.is_dir()
                and not candidate.is_symlink()):
            return True
    except (OSError, RuntimeError, ValueError):
        pass
    try:
        import runtime_env as _re  # noqa: PLC0415

        return _re._wsl_mnt_safe_home(candidate) is not None
    except Exception:
        return False


def _cursor_trends_db_candidates(home: Path) -> list[Path]:
    """Candidate rollup trends.db locations, in priority order.

    measure.py's cursor-rollup writes to $TOKEN_OPTIMIZER_SNAPSHOT_DIR/trends.db
    when that override is set, else <runtime_home>/_backups/token-optimizer/
    trends.db — under the cursor runtime that is <cursor_home>/_backups/...
    """
    candidates: list[Path] = []
    snap = os.environ.get("TOKEN_OPTIMIZER_SNAPSHOT_DIR", "").strip()
    if snap:
        candidates.append(Path(snap).expanduser() / "trends.db")
    candidates.append(home / "_backups" / "token-optimizer" / "trends.db")
    return candidates


class CursorAdapter(BaseAdapter):
    """Adapter for Cursor via Token Optimizer's own Cursor data planes.

    Reads (never writes) the two planes the Cursor hook bridge and rollup
    already produce:

    - Tallies: ``<cursor_home>/token-optimizer/sessions/<conversation_id>.json``
      — the authoritative record (turns, tool calls, models, compactions, cwd,
      timestamps). Written by cursor_hook_bridge._update_tally.
    - Rollup trends: ``<cursor_home>/_backups/token-optimizer/trends.db``
      ``session_log`` rows (``platform='cursor'``) carrying the token figures
      the rollup already resolved (state.vscdb tokenCount, else a transcript
      estimate, else zero).

    Honesty rules (R15 / KD3): Cursor exposes no local billing data, so
    cost_usd is always 0.0 and cost surfaces render "no cost data" — never a
    computed or implied $0.00 and never a pricing-table lookup. A tally with
    no trends row is collected with token_source='cursor_tally_only' and zero
    tokens, and scan() SAYS the figures are unknown — a silent zero is a
    defect (#149 class).
    """

    name = "cursor"
    display_name = "Cursor"

    _MAX_TALLY_FILES = 10_000
    _MAX_TALLY_BYTES = 4 * 1024 * 1024

    def _tally_dir(self, home: Path) -> Path:
        return home / "token-optimizer" / "sessions"

    # -- detection ----------------------------------------------------------

    def detect(self) -> tuple[bool, float, str]:
        home = _cursor_home()
        tally_dir = self._tally_dir(home)
        if tally_dir.is_dir():
            try:
                n = sum(1 for p in tally_dir.iterdir() if p.is_file() and p.suffix == ".json")
            except OSError:
                n = 0
            if n:
                return True, 1.0, f"Found {n} Token Optimizer tally file(s) in {tally_dir}"
        if home.exists():
            return True, 0.3, (f"Found {home} but no Token Optimizer session tallies — "
                               f"install the Cursor hooks (measure.py cursor-install) and run a session")
        return False, 0.0, f"{home} not found"

    # -- data-plane readers -------------------------------------------------

    def _read_tally_native(self, path: Path) -> dict | None:
        """Fallback tally reader for standalone copies (no cursor_state import).

        Mirrors cursor_state.read_tally's defensive posture: bounded bytes,
        JSON object required, None on any failure. The idle-finalisation pass
        is cursor_state's and is skipped here (documented drift risk).
        """
        try:
            if path.stat().st_size > self._MAX_TALLY_BYTES:
                return None
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def _read_tallies(self, home: Path) -> tuple[list[tuple[Path, dict]], list[str]]:
        sessions_dir = self._tally_dir(home)
        tallies: list[tuple[Path, dict]] = []
        errors: list[str] = []
        try:
            paths = sorted(p for p in sessions_dir.iterdir() if p.is_file() and p.suffix == ".json")
        except OSError as exc:
            return [], [f"cannot list {sessions_dir}: {exc}"]
        for path in paths[: self._MAX_TALLY_FILES]:
            if _cursor_state is not None:
                tally = _cursor_state.read_tally(path)
                if tally is not None:
                    tally = _cursor_state.idle_finalise(tally)
            else:
                tally = self._read_tally_native(path)
            if tally is None:
                errors.append(f"skipped unreadable tally {path.name} (corrupt or oversized)")
                continue
            tallies.append((path, tally))
        if len(paths) > self._MAX_TALLY_FILES:
            errors.append(f"tally cap {self._MAX_TALLY_FILES} reached; remaining tallies skipped this pass")
        return tallies, errors

    def _trends_rows(self, home: Path) -> tuple[dict[str, dict], str | None]:
        """Read platform='cursor' session_log rows, keyed by conversation_id.

        Returns (rows, note): note is None on success, else a human string
        explaining the absence (no DB / no table / no columns) so scan() can
        report it instead of silently zeroing tokens. Opened mode=ro: the
        collector can never write to the rollup's DB.
        """
        db_path = next((p for p in _cursor_trends_db_candidates(home) if p.is_file()), None)
        if db_path is None:
            return {}, (f"trends.db not found under {home / '_backups' / 'token-optimizer'} "
                        f"— token figures unknown; run measure.py cursor-rollup to resolve them")
        try:
            conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            return {}, f"trends.db at {db_path} would not open read-only: {exc}"
        try:
            available = {r[1] for r in conn.execute("PRAGMA table_info(session_log)").fetchall()}
            want = {"jsonl_path", "input_tokens", "output_tokens", "message_count",
                    "quality_score", "quality_grade", "cost_source"}
            if not {"jsonl_path", "input_tokens", "output_tokens"} <= available:
                return {}, f"trends.db at {db_path} lacks the session_log columns the collector needs"
            cols = sorted(want & available)
            if "platform" in available:
                query = f"SELECT {', '.join(cols)} FROM session_log WHERE platform = 'cursor'"
            else:
                query = f"SELECT {', '.join(cols)} FROM session_log WHERE jsonl_path LIKE 'cursor:%'"
            rows = {}
            for row in conn.execute(query):
                key = str(row["jsonl_path"] or "")
                if not key.startswith("cursor:"):
                    continue
                rows[key[len("cursor:"):]] = {c: row[c] for c in cols}
            return rows, None
        except sqlite3.Error as exc:
            return {}, f"trends.db at {db_path} read failed: {exc}"
        finally:
            conn.close()

    # -- collection ----------------------------------------------------------

    def scan(self, since: datetime, conn: sqlite3.Connection | None = None) -> tuple[list[AgentRun], list[str]]:
        home = _cursor_home()
        if not self._tally_dir(home).is_dir():
            return [], [f"{self._tally_dir(home)} not found — Token Optimizer has recorded no Cursor "
                        f"sessions (install: measure.py cursor-install, then run a session; 0 is not a measurement)"]

        tallies, errors = self._read_tallies(home)
        trends, note = self._trends_rows(home)
        if note:
            errors.append(note)

        runs: list[AgentRun] = []
        missing_tokens = 0
        since_ts = since.timestamp()
        for tally_path, tally in tallies:
            cid = str(tally.get("conversation_id") or tally.get("session_id") or "")
            if not cid:
                errors.append(f"skipped tally {tally_path.name}: no conversation_id")
                continue
            try:
                first_ts = float(tally["first_ts"]) if tally.get("first_ts") is not None else None
                updated_at = float(tally["updated_at"]) if tally.get("updated_at") is not None else None
            except (TypeError, ValueError, OverflowError):
                first_ts = updated_at = None
            last_activity = updated_at if updated_at is not None else first_ts
            if last_activity is None:
                errors.append(f"skipped cursor tally {cid}: no usable timestamps")
                continue
            # Window on LAST activity, not start: long-lived IDE composer chats
            # span days and stay relevant while they are active.
            if last_activity < since_ts:
                continue
            source_path = f"cursor:tally#{cid}"
            if conn and _is_run_collected(conn, "cursor", source_path, cid):
                continue

            def _count(key) -> int:
                try:
                    return max(0, int(tally.get(key) or 0))
                except (TypeError, ValueError, OverflowError):
                    return 0

            turns = _count("turns")
            tool_calls = _count("tool_calls")

            models = tally.get("models") if isinstance(tally.get("models"), dict) else {}
            model_raw = ""
            if models:
                try:
                    model_raw = str(max(models, key=lambda k: int(models.get(k) or 0)))
                except (TypeError, ValueError):
                    model_raw = ""
            model = (normalize_model_name(model_raw) or model_raw) if model_raw else "unknown"
            if _cursor_session is not None:
                ctx_window = _cursor_session.context_window_for_model(model_raw or model)
            else:
                ctx_window = 128_000  # matches cursor_session's conservative default

            row = trends.get(cid)
            if row is None:
                missing_tokens += 1
                tokens = TokenBreakdown()
                token_source = "cursor_tally_only"
            else:
                try:
                    tokens = TokenBreakdown(
                        input=max(0, int(row.get("input_tokens") or 0)),
                        output=max(0, int(row.get("output_tokens") or 0)),
                        cache_read=0,   # Cursor hooks report no cache breakdown (yet)
                        cache_write=0,
                    )
                except (TypeError, ValueError, OverflowError):
                    # Schema-defensive: a drifted/crafted row degrades to
                    # tally-only honesty instead of killing the scan.
                    missing_tokens += 1
                    tokens = TokenBreakdown()
                    row = None
                token_source = "cursor_rollup" if row is not None else "cursor_tally_only"

            duration = 0.0
            if first_ts is not None and updated_at is not None and updated_at >= first_ts:
                duration = updated_at - first_ts

            project = "unknown"
            if isinstance(tally.get("cwd"), str) and tally["cwd"]:
                project = tally["cwd"]
            else:
                roots = tally.get("workspace_roots")
                if isinstance(roots, list) and roots and isinstance(roots[0], str) and roots[0]:
                    project = roots[0]

            outcome = "success"
            if turns == 0 and tool_calls == 0:
                outcome = "empty"

            try:
                started = datetime.fromtimestamp(
                    first_ts if first_ts is not None else last_activity, timezone.utc)
            except (OSError, OverflowError, ValueError):
                # A crafted/pathological tally float must not kill the scan.
                errors.append(f"skipped cursor tally {cid}: out-of-range timestamp")
                continue

            runs.append(AgentRun(
                system="cursor",
                session_id=cid,
                agent_name="main",
                project=project,
                timestamp=started,
                duration_seconds=duration,
                tokens=tokens,
                cost_usd=0.0,  # Cursor exposes no local billing data — never priced.
                model=model,
                context_window_size=ctx_window,
                run_type="manual",
                outcome=outcome,
                message_count=turns,
                source_path=source_path,
                token_source=token_source,
            ))

        if missing_tokens:
            errors.append(
                f"{missing_tokens} cursor session(s) have no trends row yet; their token figures are "
                f"unknown (shown as 0) — run measure.py cursor-rollup to resolve them"
            )
        return runs, errors

    def parse_config(self) -> dict:
        home = _cursor_home()
        hooks_path = home / "hooks.json"
        to_bridge = False
        try:
            # Shape-proof check: the bridge path appears anywhere in the file.
            raw = hooks_path.read_text(encoding="utf-8", errors="replace")[:1_000_000]
            to_bridge = "token-optimizer" in raw
        except OSError:
            pass
        return {
            "home": str(home),
            "hooks_json_present": hooks_path.is_file(),
            "to_bridge_installed": to_bridge,
            "trends_db_present": any(p.is_file() for p in _cursor_trends_db_candidates(home)),
            # Tier-1 detectors read these keys; Cursor has no comparable config
            # surface today, so they stay empty and no detector can false-fire.
            "skills": [],
            "mcp_servers": [],
            "hooks": {},
        }

