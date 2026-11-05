#!/usr/bin/env python3
"""TO Cowork telemetry collector.

One process, five jobs:
  1. `/probe`            — receives to-hook-probe phone-home POSTs (the only
                           evidence channel for cloud sessions).
  2. `/v1/{logs,traces,metrics}` — receives Cowork's OTel OTLP/HTTP export
                           (org-admin points the endpoint here; Team/
                           Enterprise; http/protobuf or http/json — gRPC is
                           not supported for Cowork).
  3. `/healthz`          — liveness for cowork_doctor.py.
  4. `--summarize`       — smoke-test: sum api_request token fields from the
                           captured bodies to confirm telemetry is flowing.
  5. `--ingest`          — parse captured api_request/tool_result events into
                           per-session rows and write them into Token
                           Optimizer's trends.db (platform='cowork'), reusing
                           measure.py's own schema helpers so Cowork sessions
                           render in the same trends/dashboard as Claude Code.
     `--cost-view`       — read-only cross-agent spend view: total tokens and
                           USD grouped by host (claude-code / codex / cowork /
                           hermes / copilot) from the same trends store.

Ingestion notes:
  - Dedup key mirrors the hermes:/copilot: pattern: jsonl_path =
    "cowork:<session.id>", platform = "cowork".
  - Upsert (not INSERT OR IGNORE): OTel capture accumulates while a session
    runs, so re-ingest refreshes totals. Captured files are append-only, so
    totals are monotonic.
  - Only log EVENTS are parsed (api_request, tool_result). OTLP metrics
    counters carry the same token totals and would double-count.
  - Protobuf OTLP bodies are stored base64-raw, not decoded (decoding needs
    the otlp proto schema or a dependency); both summarize and ingest skip
    them and report the skip count.

Run on loopback behind a protected HTTPS ingress reachable from Cowork
VMs/cloud (the ingress domain must be on Cowork's allowlist):
    python3 to_collector.py --host 127.0.0.1 --port 4318

Then, on the machine where Token Optimizer lives:
    python3 to_collector.py --ingest
    python3 to_collector.py --cost-view --days 30
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import importlib.util
import json
import math
import os
import sqlite3
import stat
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MAX_BODY = 5 * 1024 * 1024  # cap a single POST at 5MB; reply 413 beyond
MAX_LINE = 8 * 1024 * 1024  # skip any single capture line larger than this (OOM guard)
_INT64_MAX = 2**63 - 1  # SQLite INTEGER ceiling; values beyond it fail the bind
# Reported OTel cost is trusted only within this multiple of the token-derived
# figure. A per-request delta lands close to derived (cache-TTL slack aside); a
# session-cumulative counter inflates ~linearly with call count, so anything
# well above derived is rejected in favour of the derived value.
COST_SANITY_MULTIPLE = 2.0
POST_CAPTURE_FILES = {
    "/probe": "probe.jsonl",
    "/v1/logs": "otlp-logs.jsonl",
    "/v1/traces": "otlp-traces.jsonl",
    "/v1/metrics": "otlp-metrics.jsonl",
}
POST_READ_TIMEOUT_SECONDS = 30
_capture_write_lock = threading.Lock()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _assert_private_capture_dir(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise PermissionError("Capture directory is not a regular directory")
    if os.name != "nt" and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
        raise PermissionError("Capture directory is not private")


def _append_private_capture(path: Path, line: str) -> None:
    # ThreadingHTTPServer can append to the same JSONL file from several
    # requests at once. Keep each complete record together in this process.
    with _capture_write_lock:
        if path.is_symlink():
            raise PermissionError("Capture destination is a symlink")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if path.is_symlink():
                raise PermissionError("Capture destination is a symlink")
            if not stat.S_ISREG(info.st_mode):
                raise PermissionError("Capture destination is not a regular file")
            if os.name != "nt" and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
                raise PermissionError("Capture file is not private")
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                fd = -1
                handle.write(line)
        finally:
            if fd >= 0:
                os.close(fd)


class CollectorHandler(BaseHTTPRequestHandler):
    data_dir: Path  # set by serve()

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
        pass

    def _reply(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path.rstrip("/") in ("", "/healthz"):
            self._reply(200, {"ok": True, "service": "to-cowork-collector", "time": _now()})
        else:
            self._reply(404, {"ok": False})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        # SECURITY TODO: this write surface is unauthenticated —
        # anything POSTed here becomes billing rows. Auth/allowlist is enforced
        # at the org edge (reverse proxy / shared secret) and must stay there;
        # do not treat this endpoint as trusted.
        capture_name = POST_CAPTURE_FILES.get(self.path)
        if capture_name is None:
            self._reply(404, {"ok": False, "error": "unknown endpoint"})
            return
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._reply(411, {"ok": False, "error": "Content-Length required"})
            return
        get_all = getattr(self.headers, "get_all", None)
        if callable(get_all) and len(get_all("Content-Length", [])) != 1:
            self._reply(400, {"ok": False, "error": "invalid request length"})
            return
        length = raw_length.strip()
        if (not 1 <= len(length) <= 10 or not length.isascii()
                or not length.isdecimal()
                or self.headers.get("Transfer-Encoding") is not None):
            self._reply(400, {"ok": False, "error": "invalid request length"})
            return
        declared = int(length)
        if declared > MAX_BODY:
            # Do NOT truncate-then-200: a silently clipped batch fails json.loads,
            # is stored as unparseable text, and every event in it is lost while
            # the exporter believes it was delivered. Reply 413 so the OTel
            # exporter retries or splits the batch instead.
            self._reply(413, {"ok": False, "error": "payload too large",
                              "max_body": MAX_BODY, "content_length": declared})
            return
        try:
            if hasattr(self, "connection"):
                self.connection.settimeout(POST_READ_TIMEOUT_SECONDS)
            body = self.rfile.read(declared) if declared else b""
        except (OSError, TimeoutError):
            self._reply(408, {"ok": False, "error": "request body timed out"})
            return
        if len(body) != declared:
            self._reply(400, {"ok": False, "error": "incomplete request body"})
            return
        content_type = self.headers.get("Content-Type", "")
        record: dict[str, Any] = {
            "ts": _now(),
            "path": self.path,
            "content_type": content_type,
            "remote": self.client_address[0],
            "headers": {"X-TO-Probe-Event": self.headers.get("X-TO-Probe-Event", "")[:64]}
            if self.headers.get("X-TO-Probe-Event") else {},
        }
        try:
            record["body"] = json.loads(body.decode("utf-8"))
            kind = "json"
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            try:
                record["body_text"] = body.decode("utf-8")
                kind = "text"
            except UnicodeDecodeError:
                record["body_b64"] = base64.b64encode(body).decode("ascii")
                kind = "binary"
        record["kind"] = kind

        out = self.data_dir / capture_name
        try:
            _assert_private_capture_dir(self.data_dir)
            _append_private_capture(out, json.dumps(record) + "\n")
        except OSError as exc:
            print(f"[to-collector] capture write refused: {type(exc).__name__}", file=sys.stderr)
            self._reply(500, {"ok": False, "error": "capture storage unavailable"})
            return
        self._reply(200, {"ok": True})


def _walk(node: Any):
    pending = [node]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            yield current
            pending.extend(reversed(tuple(current.values())))
        elif isinstance(current, list):
            pending.extend(reversed(current))


def _attr_map(attrs: Any) -> dict[str, Any]:
    """Flatten an OTLP attributes list [{key, value:{...Value}}] to a dict."""
    out: dict[str, Any] = {}
    if isinstance(attrs, list):
        for a in attrs:
            if isinstance(a, dict) and isinstance(a.get("key"), str):
                value = a.get("value")
                if isinstance(value, dict):
                    value = next(iter(value.values()), None)
                out[a["key"]] = value
    return out


def _iter_events(data_dir: Path):
    """Yield (merged_event_dict, receipt_ts_iso) for every dict node in the
    captured OTLP log bodies, with OTLP attribute lists flattened in. Tolerant
    of both flat events and nested resourceLogs/scopeLogs/logRecords shapes.
    Counts (but skips) undecodable protobuf records and unwalkable text records
    via the returned tally."""
    tally = {"binary_skipped": 0, "text_skipped": 0, "oversize_lines": 0,
             "undecodable_lines": 0, "files": []}

    def gen():
        for path in sorted(data_dir.glob("otlp-logs*.jsonl")):
            tally["files"].append(path.name)
            # Stream line-by-line rather than read_text() the whole file: a
            # multi-GB capture must not OOM the ingest. An
            # individual over-long line is skipped, not buffered.
            try:
                fh = path.open("rb")
            except OSError:
                continue
            with fh:
                while True:
                    line = fh.readline(MAX_LINE + 1)
                    if not line:
                        break
                    if len(line) > MAX_LINE:
                        tally["oversize_lines"] += 1
                        # readline with a limit leaves the rest of this record
                        # in the stream. Drain it in bounded chunks so the next
                        # valid record is still parsed.
                        while line and not line.endswith(b"\n"):
                            line = fh.readline(MAX_LINE + 1)
                        continue
                    line = line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, RecursionError):
                        tally["undecodable_lines"] += 1
                        continue
                    if not isinstance(record, dict):
                        tally["undecodable_lines"] += 1
                        continue
                    kind = record.get("kind")
                    if kind == "binary":
                        # protobuf OTLP body, base64-stored, not decoded here
                        tally["binary_skipped"] += 1
                        continue
                    if kind == "text" or record.get("body") is None:
                        # body failed JSON decode at capture time (e.g. a POST
                        # the exporter truncated before 413 landed): never
                        # walkable, so tally it instead of silently dropping.
                        tally["text_skipped"] += 1
                        continue
                    receipt_ts = record.get("ts")
                    for node in _walk(record.get("body")):
                        attrs = _attr_map(node.get("attributes"))
                        merged = {**{k: v for k, v in node.items() if not isinstance(v, (dict, list))}, **attrs}
                        yield merged, receipt_ts

    return gen(), tally


def _int(value: Any) -> int:
    """Accept nonnegative integral token counts within SQLite's range."""
    try:
        if isinstance(value, bool):
            return 0
        if isinstance(value, float):
            if not math.isfinite(value) or not value.is_integer():
                return 0
            n = int(value)
        elif isinstance(value, (int, str)):
            n = int(value)
        else:
            return 0
    except (TypeError, ValueError, OverflowError):
        return 0
    if n > _INT64_MAX or n < 0:
        return 0
    return n


def _as_utc(dt: datetime) -> datetime:
    """Force a datetime to aware UTC. `fromisoformat` on a zoneless string
    yields a naive datetime; comparing it against the aware UTC datetimes from
    timeUnixNano raises TypeError and kills the run. Naive input is
    assumed UTC (the exporter emits UTC); aware input is converted."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _event_time(merged: dict[str, Any], receipt_ts: Any) -> datetime | None:
    """Best-effort event timestamp: event.timestamp attr (ISO) →
    timeUnixNano/observedTimeUnixNano → collector receipt time."""
    iso = merged.get("event.timestamp") or merged.get("timestamp")
    if iso:
        try:
            return _as_utc(datetime.fromisoformat(str(iso).replace("Z", "+00:00")))
        except ValueError:
            pass
    for key in ("timeUnixNano", "time_unix_nano", "observedTimeUnixNano", "observed_time_unix_nano"):
        raw = merged.get(key)
        if raw:
            try:
                nanos = int(raw)
                if nanos > 0:
                    return datetime.fromtimestamp(nanos / 1e9, tz=timezone.utc)
            except (TypeError, ValueError, OSError, OverflowError):
                pass
    if receipt_ts:
        try:
            return _as_utc(datetime.fromisoformat(str(receipt_ts).replace("Z", "+00:00")))
        except ValueError:
            pass
    return None


def parse_cowork_sessions(data_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Group captured api_request/tool_result events per session.id.

    Returns ({session_id: session_dict}, stats). Events without a session.id
    cannot be keyed into trends and are counted in stats["no_session_id"].
    """
    events, tally = _iter_events(data_dir)
    sessions: dict[str, dict[str, Any]] = {}
    stats = {"api_request_events": 0, "tool_result_events": 0, "no_session_id": 0,
             "duplicate_events": 0}
    # Event-level dedup for one ingest run: the per-session upsert dedups on the
    # session key, not on individual events, so an OTLP client retry after a
    # slow-but-200 reply, log rotation, or a second otlp-logs* file would union
    # identical events into the sum and inflate tokens/cost. A
    # (session, time, model, tokens) seen-set collapses replays.
    seen_events: set = set()

    for merged, receipt_ts in events:
        name = str(merged.get("event.name") or merged.get("name") or "")
        is_api = "api_request" in name
        is_tool = "tool_result" in name
        if not (is_api or is_tool):
            continue
        sid = merged.get("session.id") or merged.get("session_id")
        if not sid:
            stats["no_session_id"] += 1
            continue
        sid = str(sid)
        time_key = str(
            merged.get("timeUnixNano") or merged.get("time_unix_nano")
            or merged.get("observedTimeUnixNano") or merged.get("observed_time_unix_nano")
            or merged.get("event.timestamp") or merged.get("timestamp") or ""
        )
        s = sessions.setdefault(sid, {
            "session_id": sid,
            "api_calls": 0,
            "breakdown": {},   # model -> {fresh_input, cache_read, cache_create, output}
            "tool_calls": {},  # tool_name -> count
            "model_events": {},  # model -> api_request event count
            "reported_cost_usd": 0.0,
            "daily_usage": {}, "daily_calls": {},
            "first_ts": None,
            "last_ts": None,
        })
        ts = _event_time(merged, receipt_ts)
        if ts is not None:
            if s["first_ts"] is None or ts < s["first_ts"]:
                s["first_ts"] = ts
            if s["last_ts"] is None or ts > s["last_ts"]:
                s["last_ts"] = ts
        if is_api:
            model = str(merged.get("model") or "unknown")
            in_tok = _int(merged.get("input_tokens"))
            out_tok = _int(merged.get("output_tokens"))
            cr_tok = _int(merged.get("cache_read_tokens"))
            cc_tok = _int(merged.get("cache_creation_tokens"))
            ekey = (sid, time_key, "api", model, in_tok, out_tok, cr_tok, cc_tok)
            if ekey in seen_events:
                stats["duplicate_events"] += 1
                continue
            seen_events.add(ekey)
            stats["api_request_events"] += 1
            s["api_calls"] += 1
            bd = s["breakdown"].setdefault(
                model, {"fresh_input": 0, "cache_read": 0, "cache_create": 0, "output": 0}
            )
            s["model_events"][model] = s["model_events"].get(model, 0) + 1
            bd["fresh_input"] += in_tok
            bd["output"] += out_tok
            bd["cache_read"] += cr_tok
            bd["cache_create"] += cc_tok
            activity = _event_time(merged, None)
            day = activity.astimezone().date().isoformat() if activity else "undated"
            daily = s["daily_usage"].setdefault(day, {}).setdefault(model, {
                "fresh_input": 0, "cache_read": 0, "cache_create": 0,
                "cache_create_5m": 0, "cache_create_1h": 0, "output": 0})
            daily["fresh_input"] += in_tok
            daily["output"] += out_tok
            daily["cache_read"] += cr_tok
            daily["cache_create"] += cc_tok
            daily["cache_create_5m"] += cc_tok
            s["daily_calls"][day] = s["daily_calls"].get(day, 0) + 1
            try:
                s["reported_cost_usd"] += float(merged.get("cost_usd") or 0)
            except (TypeError, ValueError):
                pass
        else:
            tool = str(merged.get("tool_name") or merged.get("name") or "unknown")
            ekey = (sid, time_key, "tool", tool)
            if ekey in seen_events:
                stats["duplicate_events"] += 1
                continue
            seen_events.add(ekey)
            stats["tool_result_events"] += 1
            s["tool_calls"][tool] = s["tool_calls"].get(tool, 0) + 1

    stats.update(tally)
    return sessions, stats


def summarize(data_dir: Path) -> dict[str, Any]:
    """Smoke-test summary: pull api_request token fields out of captured
    OTLP JSON bodies. Protobuf (binary) records are counted but not decoded."""
    sessions, stats = parse_cowork_sessions(data_dir)
    totals = {"api_request_events": stats["api_request_events"], "input_tokens": 0,
              "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0}
    models: dict[str, int] = {}  # model -> api_request event count
    for s in sessions.values():
        for model, bd in s["breakdown"].items():
            totals["input_tokens"] += bd["fresh_input"]
            totals["output_tokens"] += bd["output"]
            totals["cache_read_tokens"] += bd["cache_read"]
            totals["cache_creation_tokens"] += bd["cache_create"]
            models[model] = models.get(model, 0) + s["model_events"].get(model, 0)
    probe = data_dir / "probe.jsonl"
    probe_posts = sum(1 for _ in probe.open(encoding="utf-8")) if probe.exists() else 0
    return {"data_dir": str(data_dir), "otlp_files": stats.get("files", []),
            "probe_posts": probe_posts,
            "binary_records_skipped": stats.get("binary_skipped", 0),
            "text_records_skipped": stats.get("text_skipped", 0),
            "oversize_lines_skipped": stats.get("oversize_lines", 0),
            "undecodable_lines": stats.get("undecodable_lines", 0),
            "duplicate_events_skipped": stats.get("duplicate_events", 0),
            "events_without_session_id": stats["no_session_id"],
            "totals": totals, "models": models, "sessions": len(sessions)}


# ---------------------------------------------------------------------------
# trends.db ingestion — reuses measure.py's schema helpers (no second schema)
# ---------------------------------------------------------------------------

def _measure_candidates() -> list[Path]:
    """Where measure.py can live, in preference order."""
    cands: list[Path] = []
    env = os.environ.get("TOKEN_OPTIMIZER_MEASURE_PATH")
    if env:
        cands.append(Path(env).expanduser())
    # Repo layout: <repo>/cowork/collector/to_collector.py → <repo>/skills/...
    here = Path(__file__).resolve()
    cands.append(here.parents[2] / "skills" / "token-optimizer" / "scripts" / "measure.py")
    # Installed-plugin layouts (marketplace + custom-plugin upload)
    plugins = Path.home() / ".claude" / "plugins"
    for pattern in (
        "*/skills/token-optimizer/scripts/measure.py",
        "*/*/skills/token-optimizer/scripts/measure.py",
        "*/*/*/skills/token-optimizer/scripts/measure.py",
    ):
        cands.extend(sorted(plugins.glob(pattern)))
    return cands


@contextmanager
def _measure_import_scope():
    """Keep a collector call's canonical facade separate from its caller's."""
    names = lambda: [key for key in sys.modules if key == 'measure' or key == '_measure'
                     or key.startswith('_measure.') or key in ('plugin_env', 'runtime_env')]
    previous = {key: sys.modules[key] for key in names()}
    search = list(sys.path)
    try:
        for key in previous:
            sys.modules.pop(key, None)
        yield
    finally:
        for key in names():
            sys.modules.pop(key, None)
        sys.modules.update(previous)
        sys.path[:] = search


def _load_measure(measure_path: str | None):
    """Import measure.py as a module, or return (None, tried_paths)."""
    cands = [Path(measure_path).expanduser()] if measure_path else _measure_candidates()
    for cand in cands:
        if not cand.is_file():
            continue
        # measure.py imports siblings (hook_io, codex_session, ...) bare, so its
        # directory must be importable.
        script_dir = str(cand.parent)
        added = script_dir not in sys.path
        if added:
            sys.path.insert(0, script_dir)
        try:
            spec = importlib.util.spec_from_file_location("measure", str(cand))
            mod = importlib.util.module_from_spec(spec)
            sys.modules['measure'] = mod
            spec.loader.exec_module(mod)
            return mod, cands
        except Exception as exc:  # noqa: BLE001 - report and try the next candidate
            print(f"[to-collector] failed to import {cand}: {exc}", file=sys.stderr)
            if added:
                sys.path.remove(script_dir)
    return None, cands


@_measure_import_scope()
def ingest(data_dir: Path, measure_path: str | None = None, db_override: str | None = None,
           quiet: bool = False) -> int:
    """Write parsed Cowork sessions into TO's trends.db via measure.py helpers.

    Upserts one session_log row per Cowork session (platform='cowork',
    jsonl_path='cowork:<session.id>'), then rebuilds the daily aggregate
    tables. Returns a process exit code.
    """
    measure, tried = _load_measure(measure_path)
    if measure is None:
        print("[to-collector] measure.py not found — cannot ingest without the "
              "canonical trends schema. Tried:", file=sys.stderr)
        for c in tried:
            print(f"  - {c}", file=sys.stderr)
        print("Set TOKEN_OPTIMIZER_MEASURE_PATH or pass --measure-path.", file=sys.stderr)
        return 1

    if db_override:
        # Test hook: point measure's module constants at a scratch DB before
        # _init_trends_db() touches the real one.
        db = Path(db_override).expanduser()
        measure.TRENDS_DB = db
        measure.SNAPSHOT_DIR = db.parent

    sessions, stats = parse_cowork_sessions(data_dir)
    if not sessions:
        if not quiet:
            print(f"[to-collector] no Cowork sessions found in {data_dir} "
                  f"(binary records skipped: {stats.get('binary_skipped', 0)}, "
                  f"events without session.id: {stats['no_session_id']}). "
                  "If the org exports metrics-only OTel, enable log events — "
                  "ingestion reads api_request events.")
        return 0

    conn = measure._init_trends_db()
    written = 0
    written_paths: list[str] = []
    removed_paths: list[str] = []
    row_errors = 0
    cross_platform_skipped = 0
    unpriced_models: set[str] = set()
    try:
        for sid, s in sorted(sessions.items()):
            jsonl_path = f"cowork:{sid}"
            # Cross-source double-count guard: TO's own Stop hook
            # fires inside Cowork and writes a platform='claude' row keyed by the
            # in-VM transcript path but carrying this same session_uuid. If such a
            # row already exists under a non-cowork platform, ingesting the OTel
            # copy would count the session twice in --cost-view. idx_session_log_uuid
            # is non-unique, so query it and skip the cowork row when a sibling
            # under another platform is present.
            dup = conn.execute(
                "SELECT platform, jsonl_path FROM session_log "
                "WHERE session_uuid = ? AND COALESCE(platform, '') != 'cowork' LIMIT 1",
                (sid,),
            ).fetchone()
            if dup:
                cross_platform_skipped += 1
                # The Cowork copy may have landed on an earlier ingest before
                # Claude's Stop hook wrote its row. Remove that stale copy too.
                removed = conn.execute(
                    "DELETE FROM session_log WHERE jsonl_path = ? AND platform = 'cowork'",
                    (jsonl_path,),
                ).rowcount
                if removed > 0:
                    removed_paths.append(jsonl_path)
                if not quiet:
                    print(f"[to-collector] skip cowork session {sid}: already counted "
                          f"under platform={dup[0]!r} ({dup[1]})", file=sys.stderr)
                continue

            bd = s["breakdown"]
            fresh = sum(m["fresh_input"] for m in bd.values())
            cache_read = sum(m["cache_read"] for m in bd.values())
            cache_create = sum(m["cache_create"] for m in bd.values())
            output = sum(m["output"] for m in bd.values())
            full_input = fresh + cache_read + cache_create
            cache_hit_rate = (cache_read / full_input) if full_input > 0 else 0.0
            # Billable-token model mix, same formula collect_sessions uses
            model_usage = {m: v["fresh_input"] + v["cache_create"] + v["output"]
                           for m, v in bd.items()}
            first, last = s["first_ts"], s["last_ts"]
            # Bucket to the LOCAL calendar day, like every other collector
            # (measure.py uses local mtime; the copilot path converts explicitly).
            # first_ts is aware UTC; a 19:30 PDT session must file under today,
            # not tomorrow-UTC. collected_at stays local for the same reason.
            local_first = first.astimezone() if first else datetime.now().astimezone()
            date = local_first.strftime("%Y-%m-%d")
            duration_minutes = max(0.0, (last - first).total_seconds() / 60) if first and last else 0.0

            # Cost: prefer the token-derived figure. Cowork's OTel cost counter
            # may be session-cumulative rather than a per-request delta, which
            # would inflate the sum ~linearly with call count; until a captured
            # payload proves per-request deltas, accept the reported value only
            # when it lands within a sane multiple of derived, and never accept a
            # negative.
            derived = measure._cost_from_model_breakdown(bd)
            reported = s["reported_cost_usd"]
            row_unpriced = [m for m in bd if not measure._is_priced_model(m)]
            unpriced_models.update(row_unpriced)
            if reported < 0:
                reported = 0.0
            if derived > 0 and reported > 0 and reported <= derived * COST_SANITY_MULTIPLE:
                cost_usd, cost_source = reported, "otel_reported"
            elif derived > 0:
                cost_usd = derived
                # Flag rather than silently ship $0-priced models inside a
                # "derived" figure the reader would trust as complete.
                cost_source = "otel_derived_partial" if row_unpriced else "otel_derived"
            elif reported > 0 and not row_unpriced:
                cost_usd, cost_source = reported, "otel_reported"
            elif reported > 0:
                # No priced models to derive from — reported is the only signal.
                cost_usd, cost_source = reported, "otel_reported_unverified"
            else:
                cost_usd = 0.0
                cost_source = "otel_derived_partial" if row_unpriced else "otel_derived"

            # OTel carries no cache-TTL split; attribute all cache_create to the
            # 5m column (the derived-cost path prices it at the 5m rate) instead
            # of hardcoding both TTL columns to 0.
            try:
                cur = conn.execute(
                    """INSERT INTO session_log
                         (jsonl_path, date, project, duration_minutes, input_tokens,
                          output_tokens, message_count, api_calls, cache_hit_rate,
                          cache_create_1h_tokens, cache_create_5m_tokens, cache_ttl_scanned,
                          skills_json, subagents_json, tool_calls_json, model_usage_json,
                          all_model_usage_json, model_usage_breakdown_json, version, slug,
                          topic, collected_at, quality_score, quality_grade,
                          stale_waste_tokens, session_uuid,
                          is_sidechain, cost_usd, cost_source, platform)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, '{}', '{}', ?, ?, ?, ?,
                               NULL, ?, NULL, ?, 0, 'F', 0, ?, 0, ?, ?, 'cowork')
                       ON CONFLICT(jsonl_path) DO UPDATE SET
                         date = excluded.date,
                         duration_minutes = excluded.duration_minutes,
                         input_tokens = excluded.input_tokens,
                         output_tokens = excluded.output_tokens,
                         message_count = excluded.message_count,
                         api_calls = excluded.api_calls,
                         cache_hit_rate = excluded.cache_hit_rate,
                         cache_create_5m_tokens = excluded.cache_create_5m_tokens,
                         tool_calls_json = excluded.tool_calls_json,
                         model_usage_json = excluded.model_usage_json,
                         all_model_usage_json = excluded.all_model_usage_json,
                         model_usage_breakdown_json = excluded.model_usage_breakdown_json,
                         collected_at = excluded.collected_at,
                         session_uuid = excluded.session_uuid,
                         cost_usd = excluded.cost_usd,
                         cost_source = excluded.cost_source""",
                    (
                        jsonl_path, date, "cowork", duration_minutes, full_input,
                        output, s["api_calls"], s["api_calls"], cache_hit_rate,
                        cache_create,
                        json.dumps(s["tool_calls"]), json.dumps(model_usage),
                        json.dumps(model_usage), json.dumps(bd),
                        f"cowork-{sid[:8]}", datetime.now().isoformat(), sid,
                        round(cost_usd, 6), cost_source,
                    ),
                )
            except (sqlite3.Error, OverflowError, ValueError) as exc:
                # One malformed field (e.g. an out-of-range int that survived
                # _int, a bad bind) must never abort the whole ingest — mirror
                # the copilot rollup's per-row skip-and-tally.
                row_errors += 1
                if not quiet:
                    print(f"[to-collector] skipped cowork session {sid}: {exc}",
                          file=sys.stderr)
                continue
            if cur.rowcount > 0:
                days = measure._activity_days(s["daily_usage"])
                usage = json.dumps(s["daily_usage"])
                conn.execute(
                    "UPDATE session_log SET date=?,first_activity_day=?,last_activity_day=?,daily_usage_json=?,"
                    "parent_daily_usage_json=?,daily_calls_json=? WHERE jsonl_path=?",
                    (days[-1] if days else "", days[0] if days else None, days[-1] if days else None,
                     usage, usage, json.dumps(s["daily_calls"]), jsonl_path))
                written += 1
            written_paths.append(jsonl_path)
        if written > 0 or removed_paths:
            measure._rebuild_aggregate_tables(conn)
        conn.commit()
        # Match hermes/copilot: stamp the schema version so a DB first created by
        # an ingest run doesn't trip the next Claude collect into a full rebuild.
        try:
            conn.execute("PRAGMA user_version = 3")
            conn.commit()
        except sqlite3.Error:
            pass
        # Read back the SPECIFIC rows written this run, not every cowork row ever
        # — the latter passes even when this run wrote nothing.
        if written_paths:
            placeholders = ",".join("?" for _ in written_paths)
            check = conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM(input_tokens),0), "
                f"COALESCE(SUM(output_tokens),0), COALESCE(SUM(cost_usd),0) "
                f"FROM session_log WHERE jsonl_path IN ({placeholders})",
                written_paths,
            ).fetchone()
        else:
            check = (0, 0, 0, 0.0)
        if removed_paths:
            remaining_removed = sum(
                conn.execute("SELECT COUNT(*) FROM session_log WHERE jsonl_path = ?", (path,)).fetchone()[0]
                for path in removed_paths
            )
        else:
            remaining_removed = 0
    finally:
        conn.close()

    stats["cross_platform_skipped"] = cross_platform_skipped
    stats["cross_platform_removed"] = len(removed_paths)
    stats["row_errors"] = row_errors
    if not quiet:
        print(f"[to-collector] upserted {written} Cowork session(s) into {measure.TRENDS_DB}")
        print(f"[to-collector] verified in DB (this run): {check[0]} rows, "
              f"input={check[1]:,} output={check[2]:,} cost=${check[3]:.4f}")
        if removed_paths:
            print(f"[to-collector] removed {len(removed_paths)} stale Cowork duplicate(s); "
                  f"verified remaining: {remaining_removed}")
        skipped_bits = []
        if stats.get("binary_skipped"):
            skipped_bits.append(f"{stats['binary_skipped']} protobuf records (undecoded)")
        if stats.get("text_skipped"):
            skipped_bits.append(f"{stats['text_skipped']} unwalkable text records")
        if stats.get("oversize_lines"):
            skipped_bits.append(f"{stats['oversize_lines']} over-{MAX_LINE // (1024 * 1024)}MB lines")
        if stats.get("duplicate_events"):
            skipped_bits.append(f"{stats['duplicate_events']} duplicate events")
        if stats["no_session_id"]:
            skipped_bits.append(f"{stats['no_session_id']} events without session.id")
        if cross_platform_skipped:
            skipped_bits.append(f"{cross_platform_skipped} sessions already counted under another platform")
        if row_errors:
            skipped_bits.append(f"{row_errors} rows failed to write")
        if skipped_bits:
            print(f"[to-collector] skipped: {', '.join(skipped_bits)}")
        if unpriced_models:
            print(f"[to-collector] warning: unpriced model(s) contributed $0 to derived cost: "
                  f"{', '.join(sorted(unpriced_models))}")
    # A partial ingest must be visible to quiet automation through its exit code.
    return 0 if row_errors == 0 and check[0] >= written and remaining_removed == 0 else 1


# ---------------------------------------------------------------------------
# Cross-agent cost view
# ---------------------------------------------------------------------------

_HOST_LABELS = {"claude": "claude-code", "codex": "codex", "cowork": "cowork",
                "hermes": "hermes", "copilot": "copilot"}


def _row_host(platform: str | None, jsonl_path: str | None) -> str:
    """Same inference _init_trends_db's platform backfill uses, for legacy
    NULL-platform rows."""
    if platform:
        return _HOST_LABELS.get(platform, platform)
    p = jsonl_path or ""
    if "/.codex/" in p:
        return "codex"
    for prefix in ("hermes", "copilot", "cowork"):
        if p.startswith(prefix + ":"):
            return prefix
    return "claude-code"


@_measure_import_scope()
def cost_view(measure_path: str | None = None, db_override: str | None = None,
              days: int = 30, as_json: bool = False) -> int:
    """Print total tokens/cost grouped by host from the trends store."""
    measure, _ = _load_measure(measure_path)
    if db_override:
        db = Path(db_override).expanduser()
    elif measure is not None:
        db = measure.TRENDS_DB
    else:
        db = Path.home() / ".claude" / "token-optimizer" / "trends.db"
    if not Path(db).exists():
        print(f"[to-collector] trends DB not found: {db}", file=sys.stderr)
        return 1

    cutoff = None
    if days > 0:
        cutoff = datetime.fromtimestamp(time.time() - days * 86400).strftime("%Y-%m-%d")

    # Build the read-only URI with Path.as_uri() so a db path containing spaces,
    # '?', '#', or other URI-significant characters is escaped correctly instead
    # of string-interpolated.
    ro_uri = f"{Path(db).resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(ro_uri, uri=True)
    try:
        query = ("SELECT platform, jsonl_path, input_tokens, output_tokens, cost_usd, "
                 "model_usage_breakdown_json FROM session_log")
        params: tuple = ()
        if cutoff:
            query += " WHERE date >= ?"
            params = (cutoff,)
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    hosts: dict[str, dict[str, Any]] = {}
    underived = 0
    for platform, jsonl_path, inp, out, cost_usd, bd_json in rows:
        host = _row_host(platform, jsonl_path)
        h = hosts.setdefault(host, {"sessions": 0, "input_tokens": 0, "output_tokens": 0,
                                    "cost_usd": 0.0})
        h["sessions"] += 1
        h["input_tokens"] += int(inp or 0)
        h["output_tokens"] += int(out or 0)
        if cost_usd is not None:
            h["cost_usd"] += float(cost_usd)
        elif measure is not None:
            try:
                bd = json.loads(bd_json) if bd_json else {}
            except (json.JSONDecodeError, TypeError):
                bd = {}
            h["cost_usd"] += measure._cost_from_model_breakdown(bd) if isinstance(bd, dict) else 0.0
        else:
            underived += 1

    result = {"db": str(db), "period_days": days if days > 0 else "all",
              "hosts": {k: {**v, "cost_usd": round(v["cost_usd"], 2)}
                        for k, v in sorted(hosts.items(), key=lambda kv: -kv[1]["cost_usd"])},
              "total_cost_usd": round(sum(h["cost_usd"] for h in hosts.values()), 2),
              "sessions_without_cost": underived}
    if as_json:
        print(json.dumps(result, indent=2))
        return 0

    label = f"last {days} days" if days > 0 else "all time"
    print(f"Cross-agent spend ({label}) — {db}")
    print(f"{'host':<12} {'sessions':>9} {'input tok':>14} {'output tok':>12} {'cost USD':>10}")
    for host, h in result["hosts"].items():
        print(f"{host:<12} {h['sessions']:>9,} {h['input_tokens']:>14,} "
              f"{h['output_tokens']:>12,} {h['cost_usd']:>10,.2f}")
    print(f"{'TOTAL':<12} {sum(h['sessions'] for h in hosts.values()):>9,} "
          f"{sum(h['input_tokens'] for h in hosts.values()):>14,} "
          f"{sum(h['output_tokens'] for h in hosts.values()):>12,} "
          f"{result['total_cost_usd']:>10,.2f}")
    if underived:
        print(f"note: {underived} session(s) have no stored cost and measure.py was "
              "not importable — their cost is excluded.")
    return 0


def serve(host: str, port: int, data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _assert_private_capture_dir(data_dir)
    CollectorHandler.data_dir = data_dir
    server = ThreadingHTTPServer((host, port), CollectorHandler)
    print(f"[to-collector] listening on http://{host}:{port} -> {data_dir}")
    print("[to-collector] endpoints: POST /probe, POST /v1/{logs,traces,metrics}, GET /healthz")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[to-collector] stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TO Cowork telemetry collector (probe phone-home + OTLP/HTTP + trends ingestion).")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4318)
    parser.add_argument("--data-dir", default=str(Path.home() / ".token-optimizer" / "cowork"))
    parser.add_argument("--summarize", action="store_true",
                        help="Summarize captured telemetry instead of serving")
    parser.add_argument("--ingest", action="store_true",
                        help="Ingest captured telemetry into TO's trends.db (platform=cowork)")
    parser.add_argument("--cost-view", action="store_true",
                        help="Print tokens/cost grouped by host (claude-code/codex/cowork/...) from trends.db")
    parser.add_argument("--days", type=int, default=30,
                        help="Window for --cost-view (0 = all time; default 30)")
    parser.add_argument("--json", action="store_true", help="JSON output for --cost-view")
    parser.add_argument("--measure-path", default=None,
                        help="Explicit path to measure.py (else env TOKEN_OPTIMIZER_MEASURE_PATH, repo layout, installed plugin)")
    parser.add_argument("--db", default=None,
                        help="Override trends.db path (testing; default is measure.py's TRENDS_DB)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir).expanduser()
    if args.summarize:
        print(json.dumps(summarize(data_dir), indent=2))
        return 0
    if args.ingest:
        return ingest(data_dir, measure_path=args.measure_path, db_override=args.db,
                      quiet=args.quiet)
    if args.cost_view:
        return cost_view(measure_path=args.measure_path, db_override=args.db,
                         days=args.days, as_json=args.json)
    serve(args.host, args.port, data_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
