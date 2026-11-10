"""Definition-only implementations; mutable state belongs to measure."""
from __future__ import annotations
from . import _compat, dashboard_history, structural_ledger

def _daemon_is_running():
    """Return True iff the dashboard daemon identity probe passes.

    Uses the same magic-string check as `daemon-status` so a foreign
    process on 24842 is never mistaken for ours. Short timeout (0.5s)
    because this is called from interactive paths where users are
    waiting for a browser to open.
    """
    _measure_facade = _compat.facade()
    import urllib.error
    import urllib.request

    magic = _measure_facade.DAEMON_IDENTITY_MAGIC.encode("utf-8")
    for host in ("127.0.0.1", "[::1]"):
        url = f"http://{host}:{_measure_facade.DAEMON_PORT}/__to_ping"
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
    _measure_facade = _compat.facade()
    proc = _measure_facade.subprocess.Popen(
        argv,
        stdin=_measure_facade.subprocess.DEVNULL,
        stdout=_measure_facade.subprocess.DEVNULL,
        stderr=_measure_facade.subprocess.DEVNULL,
        start_new_session=(_measure_facade.os.name == "posix"),
        creationflags=_measure_facade._NO_WINDOW,
    )
    try:
        rc = proc.wait(timeout=10)
    except _measure_facade.subprocess.TimeoutExpired:
        # A slow or hung opener: reap it in the background (no zombie, no
        # ResourceWarning) and say where the page is, so a hang is never silent.
        import threading
        threading.Thread(target=proc.wait, daemon=True).start()
        print(f"  Browser is slow to open. If nothing appears, open: {argv[-1]}")
        return
    if rc != 0:
        raise _measure_facade.subprocess.CalledProcessError(rc, argv)

def _open_in_browser(filepath):
    """Open a file in the default browser. Cross-platform.

    Kept for non-dashboard callers. For dashboard flows use
    _open_dashboard() which prefers the bookmarkable URL when the
    daemon is live.
    """
    _measure_facade = _compat.facade()
    filepath = str(filepath)
    system = _measure_facade.platform.system()
    try:
        if system == "Darwin":
            _measure_facade._launch_opener(["open", filepath])
        elif system == "Linux":
            _measure_facade._launch_opener(["xdg-open", filepath])
        elif system == "Windows":
            _measure_facade.os.startfile(filepath)
        else:
            raise OSError(f"Unsupported platform: {system}")
    except (_measure_facade.subprocess.CalledProcessError, OSError, FileNotFoundError):
        url = _measure_facade.Path(filepath).as_uri()
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
    _measure_facade = _compat.facade()
    if _measure_facade._daemon_is_running():
        url = f"http://localhost:{_measure_facade.DAEMON_PORT}/token-optimizer"
        system = _measure_facade.platform.system()
        try:
            if system == "Darwin":
                _measure_facade._launch_opener(["open", url])
            elif system == "Linux":
                _measure_facade._launch_opener(["xdg-open", url])
            elif system == "Windows":
                _measure_facade.os.startfile(url)
            else:
                raise OSError(f"Unsupported platform: {system}")
            print(f"  Opened: {url}")
            return
        except (_measure_facade.subprocess.CalledProcessError, OSError, FileNotFoundError):
            print(f"\n  Could not auto-open browser. Open manually:\n  {url}")
            return
    # Daemon down: open the file directly
    _measure_facade._open_in_browser(fallback_filepath)

def _serve_dashboard(filepath, port=None, host=None):
    """Serve the dashboard over HTTP for headless/remote access."""
    _measure_facade = _compat.facade()
    import http.server
    import socketserver
    import socket

    filepath = _measure_facade.Path(filepath).resolve()
    serve_dir = str(filepath.parent)
    filename = filepath.name
    _measure_facade._get_or_create_daemon_token()
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
        _measure_facade.sys.exit(1)

    handler = http.server.SimpleHTTPRequestHandler

    class DashboardHandler(handler):
        def __init__(self, *a, **kw):
            _measure_facade = _compat.facade()
            super().__init__(*a, directory=serve_dir, **kw)

        def log_message(self, *args):
            _measure_facade = _compat.facade()
            pass  # suppress per-request logs (overrides BaseHTTPRequestHandler)

        def end_headers(self):
            _measure_facade = _compat.facade()
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy", _measure_facade._DASHBOARD_CSP,
            )
            super().end_headers()

        def _is_dashboard_request(self):
            _measure_facade = _compat.facade()
            requested = self.path.lstrip("/").split("?")[0]
            return requested in ("", "token-optimizer", filename)

        def _redirect_root(self):
            _measure_facade = _compat.facade()
            if self._is_dashboard_request():
                if self.path.lstrip("/").split("?")[0] != filename:
                    self.send_response(302)
                    self.send_header("Location", f"/{filename}")
                    self.end_headers()
                    return True
            return False

        def _check_allowed(self):
            """Only serve the dashboard file itself, nothing else."""
            _measure_facade = _compat.facade()
            if not self._is_dashboard_request():
                self.send_error(403, "Forbidden")
                return False
            return True

        def do_GET(self):
            _measure_facade = _compat.facade()
            path_only = self.path.split("?")[0]
            # API health probe (lets dashboard detect our server vs generic)
            if path_only == "/api/health":
                self._json_response(200, {"ok": True, "server": "token-optimizer"})
                return
            # v5.4.19 identity magic probe (defeats foreign-port masquerade)
            if path_only == "/__to_ping":
                self._json_response(200, {"ok": True, "magic": _measure_facade.DAEMON_IDENTITY_MAGIC})
                return
            # v5.4.19 per-install token endpoint (H-2/M-4). Only served to
            # localhost Origin+Host so a foreign site cannot exfiltrate it.
            if path_only == "/api/token":
                origin = self.headers.get("Origin", "")
                host = self.headers.get("Host", "")
                origin_ok = (not origin) or any(
                    origin.startswith(p) for p in ("http://127.0.0.1:", "http://localhost:", "http://[::1]:")
                )
                if not origin_ok or not _measure_facade._is_localhost_host_header(host):
                    self.send_error(403, "Forbidden")
                    return
                self._json_response(200, {"token": _measure_facade._read_daemon_token()})
                return
            # Live savings recompute. A browser refresh against the daemon shows
            # current savings/cache numbers WITHOUT paying for a full (multi-second)
            # dashboard regen: the page fetches this and patches the tiles in place.
            # Read-only, localhost-guarded (same DNS-rebinding defense as the static
            # assets below), fail-open (a slow/failed eval degrades to a safe shape).
            if path_only == "/api/savings":
                if not _measure_facade._is_localhost_host_header(self.headers.get("Host", "")):
                    self.send_error(421, "Misdirected Request")
                    return
                try:
                    payload = _measure_facade._live_savings_payload(days=30)
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
            if not _measure_facade._is_localhost_host_header(self.headers.get("Host", "")):
                self.send_error(421, "Misdirected Request")
                return
            super().do_GET()

        def do_HEAD(self):
            _measure_facade = _compat.facade()
            if self._redirect_root():
                return
            if not self._check_allowed():
                return
            if not _measure_facade._is_localhost_host_header(self.headers.get("Host", "")):
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
            _measure_facade = _compat.facade()
            origin = self.headers.get("Origin", "")
            if not origin or not any(origin.startswith(p) for p in ("http://127.0.0.1:", "http://localhost:", "http://[::1]:")):
                self.send_error(403, "Forbidden: invalid origin")
                return

            # H-1 fix: Host header allowlist (localhost / 127.0.0.1 / [::1]).
            if not _measure_facade._is_localhost_host_header(self.headers.get("Host", "")):
                self.send_error(421, "Misdirected Request")
                return

            # M-4 fix: constant-time per-install token check.
            expected_tok = _measure_facade._read_daemon_token()
            got_tok = self.headers.get("X-TO-Token", "")
            if not expected_tok or not _measure_facade.hmac.compare_digest(expected_tok, got_tok):
                self.send_error(403, "Forbidden: invalid token")
                return

            path = self.path.split("?")[0]

            # Body size limit
            MAX_BODY = 65536  # 64KB
            content_len = int(self.headers.get("Content-Length", 0))
            if content_len > MAX_BODY:
                self.send_error(413, "Request body too large")
                return

            body = {}
            if content_len > 0:
                try:
                    body = _measure_facade.json.loads(self.rfile.read(content_len))
                except (_measure_facade.json.JSONDecodeError, ValueError):
                    pass

            if not isinstance(body, dict): body = {}
            if path == "/api/regenerate":
                choice = body.get("days", dashboard_history.load(_measure_facade.RUNTIME_DIR))
                if not dashboard_history.valid(choice):
                    self._json_response(400, {"ok": False, "msg": "invalid history window"})
                    return
                try:
                    days = dashboard_history.resolve_days(choice, _measure_facade.TRENDS_DB)
                    out = _measure_facade.generate_standalone_dashboard(days=days, quiet=True, force=True)
                    if out and "days" in body:
                        dashboard_history.save(_measure_facade.RUNTIME_DIR, choice)
                    self._json_response(200 if out else 500, {"ok": bool(out)})
                except Exception:
                    self._json_response(500, {"ok": False, "msg": "regeneration failed"})
                return

            if path == "/api/session-turns":
                raw_path = body.get("jsonl_path", "")
                if not raw_path:
                    self._json_response(400, {"error": "Missing 'jsonl_path' field"})
                    return
                try:
                    jsonl_path = _measure_facade.Path(raw_path).expanduser().resolve()
                    allowed_root = (_measure_facade.CLAUDE_DIR / "projects").resolve()
                    jsonl_path.relative_to(allowed_root)
                except (ValueError, OSError):
                    self._json_response(403, {"error": "Forbidden: invalid session path"})
                    return
                if not jsonl_path.exists():
                    self._json_response(404, {"error": "Session log not found"})
                    return
                turns = _measure_facade.parse_session_turns(jsonl_path)
                self._json_response(200, {"ok": True, "turns": turns})
                return

            name = body.get("name", "")
            if not name:
                self._json_response(400, {"error": "Missing 'name' field"})
                return

            ok = False
            msg = ""
            if path == "/api/skill/archive":
                if _measure_facade.detect_runtime() == "codex":
                    ok = _measure_facade._manage_codex_skill("disable", raw_path=name)
                    msg = f"Disabled Codex skill: {name}" if ok else f"Failed to disable Codex skill: {name}"
                else:
                    ok = _measure_facade._manage_skill("archive", name)
                    msg = f"Archived skill: {name}" if ok else f"Failed to archive: {name}"
            elif path == "/api/skill/restore":
                if _measure_facade.detect_runtime() == "codex":
                    ok = _measure_facade._manage_codex_skill("enable", raw_path=name)
                    msg = f"Enabled Codex skill: {name}" if ok else f"Failed to enable Codex skill: {name}"
                else:
                    ok = _measure_facade._manage_skill("restore", name)
                    msg = f"Restored skill: {name}" if ok else f"Failed to restore: {name}"
            elif path == "/api/mcp/disable":
                if _measure_facade.detect_runtime() == "codex":
                    ok = _measure_facade._manage_codex_mcp("disable", name)
                    msg = f"Disabled Codex MCP server: {name}" if ok else f"Failed to disable Codex MCP: {name}"
                else:
                    ok = _measure_facade._manage_mcp("disable", name)
                    msg = f"Disabled MCP server: {name}" if ok else f"Failed to disable: {name}"
            elif path == "/api/mcp/enable":
                if _measure_facade.detect_runtime() == "codex":
                    ok = _measure_facade._manage_codex_mcp("enable", name)
                    msg = f"Enabled Codex MCP server: {name}" if ok else f"Failed to enable Codex MCP: {name}"
                else:
                    ok = _measure_facade._manage_mcp("enable", name)
                    msg = f"Enabled MCP server: {name}" if ok else f"Failed to enable: {name}"
            elif path == "/api/v5/toggle":
                # v5 feature toggle: body has {"name": "feature_name", "enabled": true/false}
                enabled = bool(body.get("enabled", False))
                if name not in _measure_facade.V5_FEATURES:
                    self._json_response(400, {"error": f"Unknown v5 feature: {name}"})
                    return
                # An env-managed feature can't be toggled from the dashboard: a
                # config.json write would be silently shadowed by the env var,
                # leaving the UI out of sync with runtime. Reject it honestly
                # rather than pretend the toggle took effect.
                if _measure_facade._resolve_feature_env(_measure_facade.V5_FEATURES[name]["env_var"]) is not None:
                    self._json_response(409, {
                        "ok": False,
                        "msg": f"{_measure_facade.V5_FEATURES[name]['label']} is set via an environment "
                               f"variable and cannot be toggled from the dashboard. "
                               f"Change {_measure_facade.V5_FEATURES[name]['env_var']} in your settings.",
                    })
                    return
                ok = _measure_facade._set_v5_feature(name, enabled)
                label = _measure_facade.V5_FEATURES[name]["label"]
                msg = f"{'Enabled' if enabled else 'Disabled'}: {label}" if ok else f"Failed to toggle {name}"
                # Strip internal implementation keys before returning over HTTP
                public_status = {}
                for fname, info in _measure_facade._get_v5_feature_status().items():
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
                    fresh_manage = _measure_facade._collect_management_data()
                except Exception:
                    pass

            self._json_response(
                200 if ok else 500,
                {"ok": ok, "message": msg, "manage": fresh_manage}
            )

        def _json_response(self, code, data):
            _measure_facade = _compat.facade()
            body = _measure_facade.json.dumps(data, default=str).encode("utf-8")
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
            _measure_facade = _compat.facade()
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
    _measure_facade = _compat.facade()
    trends = data.get("trends")
    if isinstance(trends, dict):
        for day in trends.get("daily", []) or []:
            for session in day.get("session_details", []) or []:
                path = session.get("jsonl_path")
                if path:
                    session["jsonl_path"] = _measure_facade._display_path(path)

    quality = data.get("quality")
    if isinstance(quality, dict):
        sf = quality.get("session_file")
        if sf:
            quality["session_file"] = _measure_facade.Path(sf).name

    mr = data.get("memory_review")
    if isinstance(mr, dict):
        target = mr.get("target")
        if target:
            mr["target"] = _measure_facade._display_path(target)

    return data

def _codex_state_summary():
    """Collect Codex runtime-state metrics for display, or None unless Codex.

    Combines read-only SQLite state (subagent costs, memory overhead, goal
    budgets) with the current session's parsed signals (rate limits, effort,
    compaction compression). Returns None on the Claude runtime so dashboard,
    report, and doctor consumers can treat the key as optional (no Claude-path
    behavior change). All sources degrade to empty rather than raising.
    """
    _measure_facade = _compat.facade()
    if _measure_facade.detect_runtime() != "codex":
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
        current_tid = _measure_facade.codex_state.current_thread_id()
    except Exception:
        current_tid = None
    res = _measure_facade._codex_thread_resolution()
    if current_tid:
        subagents = _measure_facade.codex_state.subagent_costs(root_thread_id=current_tid)
        goals = _measure_facade.codex_state.goal_budgets(root_thread_id=current_tid)
    else:
        subagents = {**_measure_facade.codex_state._empty_subagents(), "unresolved": True}
        goals = {**_measure_facade.codex_state._empty_goals(), "unresolved": True}
    memory = _measure_facade.codex_state.memory_overhead()
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
            session = _measure_facade.codex_session.parse_session_jsonl(current)
            if session:
                summary.update({
                    "rate_limits": session.get("rate_limits"),
                    "effort": session.get("effort"),
                    "effort_breakdown": session.get("effort_breakdown"),
                    "tool_duration_p90_ms": session.get("tool_duration_p90_ms"),
                    "task_duration_ms_max": session.get("task_duration_ms_max"),
                    "ttft_ms_avg": session.get("ttft_ms_avg"),
                })
            quality = _measure_facade.codex_session.parse_jsonl_for_quality(current)
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

def codex_state_report(as_json=None):
    """Print the Codex runtime-state summary (subagents, memory, goals, signals)."""
    _measure_facade = _compat.facade()
    summary = _measure_facade._codex_state_summary()
    if as_json:
        print(_measure_facade.json.dumps(summary, indent=2, default=str))
        return
    if summary is None:
        print("Codex runtime state is only available on the Codex runtime.")
        return
    print("\nToken Optimizer - Codex Runtime State")
    print("=" * 38)

    if summary.get("thread_resolution") == "unresolved":
        reason = summary.get("thread_resolution_reason")
        why = (f"no transcript found for {summary.get('selected_thread_id')}"
               if reason == "transcript_not_found" else _measure_facade._codex_identity_reason())
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
    _measure_facade = _compat.facade()
    coord = _measure_facade.Path(coord_path)
    if not coord.exists():
        print(f"Error: coord-path does not exist: {coord_path}")
        print(_measure_facade._hint("Usage: python3 measure.py dashboard --coord-path /tmp/token-optimizer-XXXXXXXXXX"))
        _measure_facade.sys.exit(1)

    # Locate the template
    script_dir = _measure_facade.Path(_measure_facade.__file__).resolve().parent
    template_path = script_dir.parent / "assets" / "dashboard.html"
    if not template_path.exists():
        print(f"Error: dashboard template not found at: {template_path}")
        _measure_facade.sys.exit(1)

    # Re-measure current state
    print("  Measuring current token overhead...")
    components = _measure_facade.measure_components()
    totals = _measure_facade.calculate_totals(components)
    baselines = _measure_facade.get_session_baselines(5)

    calibration = _measure_facade.detect_calibration_gap(components, totals, baselines)

    ctx_window, ctx_source = _measure_facade.detect_context_window()
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
        trends = _measure_facade._collect_trends_data(days=dashboard_history.selected_days(_measure_facade.RUNTIME_DIR, _measure_facade.TRENDS_DB))
    except Exception:
        trends = None
    print("  Checking session health...")
    try:
        health = _measure_facade._collect_health_data()
    except Exception:
        health = None

    # Generate coach data for the Coach tab (reuse already-collected components/trends)
    print("  Generating coach data...")
    try:
        coach = _measure_facade.generate_coach_data(components=components, trends=trends)
    except Exception:
        coach = None

    # Collect context quality data (v2.0)
    print("  Analyzing context quality...")
    quality = _measure_facade._collect_quality_for_dashboard()

    hook_status = _measure_facade._collect_hook_status_for_dashboard()

    management = _measure_facade._collect_management_data(components=components, trends=trends)

    print("  Collecting savings data...")
    savings_data = _measure_facade._dashboard_savings_data(days=30)

    # Savings-truth U6: lean-resume carry is a SHADOW diagnostic. It is emitted
    # for review only and is never summed into any savings total.
    try:
        import lean_resume_carry as _lean_resume_carry
        if isinstance(savings_data, dict):
            savings_data["lean_resume_carry"] = _lean_resume_carry.summary(days=30)
    except Exception:
        if isinstance(savings_data, dict):
            savings_data["lean_resume_carry"] = {"available": False, "evidence": "shadow"}

    try:
        compression_coverage = _measure_facade._get_compression_coverage(days=30)
    except Exception:
        compression_coverage = {"available": False, "tiers": {}, "first_read_proxy": None}

    try:
        cohort_tripwire = _measure_facade._cohort_tripwire_state()
    except Exception:
        cohort_tripwire = {"cohorts": [], "demoted": []}
    compression_coverage["cohort_tripwire"] = cohort_tripwire

    # Cache-TTL watchdog (opportunity tier — observed waste, potential recovery).
    # Standalone analysis; never reaches the realized savings headline. Fail-open.
    try:
        cache_health = _measure_facade._cache_ttl_waste_cached(days=30)
    except Exception:
        cache_health = {"available": False, "tier": "opportunity"}

    # U7: keep-warm cache-automation sub-block carried inside the cache-health
    # payload (realized vs spend vs NET, tripwire ratio, the 5 tile states,
    # predictor transparency). Fail-open so it never blocks dashboard regen.
    try:
        if isinstance(cache_health, dict):
            cache_health["keepwarm"] = _measure_facade.keepwarm_cache_health_block(days=30)
    except Exception:
        pass

    # Fall back to auto-recommendations if LLM plan is missing
    auto_plan_flag = False
    if not plan:
        print("  No LLM plan found, generating auto-recommendations...")
        plan, rec_count = _measure_facade.generate_auto_recommendations(components, trends=trends, days=30)
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
        runway = _measure_facade.runway_snapshot(days=30)
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
        "generated_at": _measure_facade.datetime.now().isoformat(),
        "version": _measure_facade.TOKEN_OPTIMIZER_VERSION,
        "runtime": _measure_facade.detect_runtime(),
        "runtime_label": _measure_facade.runtime_name_for_humans(),
        "codex_state": _measure_facade._codex_state_summary(),
    }
    data = _measure_facade._sanitize_dashboard_paths(data)

    # Load template and inject data
    template = template_path.read_text(encoding="utf-8")
    try:  # U9: inline renderer modules kept beside the template (fail-open)
        import dashboard_assets as _dashboard_assets
        template = _dashboard_assets.inline(template, template_path.parent)
    except Exception:
        pass
    data_json = _measure_facade.json.dumps(data, ensure_ascii=True, default=str)
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
    fd = _measure_facade.os.open(str(out_path), _measure_facade.os.O_WRONLY | _measure_facade.os.O_CREAT | _measure_facade.os.O_TRUNC, 0o600)
    with _measure_facade.os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(injected)
    print(f"  Dashboard written to: {out_path}")

    # v5.3.6 / v5.4.10: mirror the same HTML to BOTH dashboard paths so
    # the daemon serves fresh audit content regardless of which path it
    # was configured to use. Same dual-path pattern as v5.4.7 daemon script.
    legacy_dashboard = _measure_facade.RUNTIME_DIR / "_backups" / "token-optimizer" / "dashboard.html"
    wrote_mirror = False
    skipped_all = True
    for mirror_path in {_measure_facade.DASHBOARD_PATH, legacy_dashboard}:
        # Version-downgrade guard: an older build's audit must not clobber a
        # shared dashboard a newer build already wrote. The per-run audit artifact
        # (out_path) is still written above; only the SHARED served copies are
        # guarded. Fail-open (see _dashboard_on_disk_is_newer).
        if _measure_facade._dashboard_on_disk_is_newer(mirror_path):
            continue
        skipped_all = False
        try:
            mirror_path.parent.mkdir(parents=True, exist_ok=True)
            mirror_fd = _measure_facade.os.open(str(mirror_path), _measure_facade.os.O_WRONLY | _measure_facade.os.O_CREAT | _measure_facade.os.O_TRUNC, 0o600)
            with _measure_facade.os.fdopen(mirror_fd, "w", encoding="utf-8") as f:
                f.write(injected)
            # The sidecar must name the build that wrote THIS HTML, or the guard
            # reads a stale version and a middle build could clobber a newer file.
            _measure_facade._write_dashboard_meta(mirror_path)
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
    _measure_facade._open_dashboard(fallback_filepath=out_path)
    return str(out_path)

def _shell_script_path():
    """This script's resolved path, quoted to paste into Bash, PowerShell and cmd.exe."""
    _measure_facade = _compat.facade()
    return _measure_facade.shell_path(_measure_facade.Path(_measure_facade.__file__).resolve())

def _measure_cli(*args):
    """The command that runs this script, for hints a user will paste into a shell.

    Always the resolved script path (quoted): a bare `python3 measure.py` only works
    from inside the scripts directory, which is where nobody is. On Windows the
    interpreter is `python`, and the path uses forward slashes and double quotes, the
    one form cmd.exe, PowerShell and Git Bash all read as a single argument
    (`runtime_env.shell_path`).
    """
    _measure_facade = _compat.facade()
    parts = [_measure_facade.hint_python(), _measure_facade._shell_script_path()]
    parts.extend(args)
    return " ".join(parts)

def _runtime_cli(runtime, *args):
    """`_measure_cli` pinned to a runtime for hints.

    macOS/Linux: the Bash `TOKEN_OPTIMIZER_RUNTIME=NAME python3 <script>` prefix,
    unchanged. cmd.exe and PowerShell cannot parse that, so on Windows it is
    `python <script> --runtime NAME` (a leading global flag, see `consume_runtime_flag`).
    """
    _measure_facade = _compat.facade()
    if _measure_facade._windows_hints():
        parts = [_measure_facade.hint_python(), _measure_facade._shell_script_path(), "--runtime", runtime]
    else:
        parts = [f"TOKEN_OPTIMIZER_RUNTIME={runtime}", _measure_facade.hint_python(), _measure_facade._shell_script_path()]
    parts.extend(args)
    return " ".join(parts)

def _hint(text):
    """Make a printed hint pasteable from any directory.

    Hints are written as `python3 measure.py <subcommand>`; this swaps that
    bare prefix for the resolved, quoted script path (`_measure_cli()`). A
    `TOKEN_OPTIMIZER_RUNTIME=X` prefix (Bash only) becomes `--runtime X` on Windows.
    """
    _measure_facade = _compat.facade()
    text = _measure_facade._ENV_PREFIXED_BARE_HINT.sub(lambda m: ((_measure_facade := _compat.facade()), _measure_facade._runtime_cli(m.group(1)))[1], text)
    return text.replace("python3 measure.py", _measure_facade._measure_cli())

def _subagent_cache_cmd(action=None):
    """The `subagent-cache [action]` command users are told to run (advice, enable, undo)."""
    _measure_facade = _compat.facade()
    return _measure_facade._measure_cli("subagent-cache", *([action] if action else []))

def _display_path(absolute_path):
    """Replace the user's home directory with ~ for display purposes.

    Avoids exposing full filesystem paths like /Users/alex/.claude/... in the UI.
    """
    _measure_facade = _compat.facade()
    home = str(_measure_facade.Path.home())
    s = str(absolute_path)
    if s == home:
        return "~"
    if s.startswith(home + _measure_facade.os.sep):
        return "~" + s[len(home):]
    return s

def _collect_hook_status_for_dashboard():
    """Collect hook installation status for dashboard toggle panel."""
    _measure_facade = _compat.facade()
    if _measure_facade.detect_runtime() == "codex":
        return _measure_facade._collect_codex_hook_status_for_dashboard()
    if _measure_facade.detect_runtime() == "hermes":
        return _measure_facade._collect_hermes_hook_status_for_dashboard()
    if _measure_facade.detect_runtime() == "copilot":
        return _measure_facade._collect_copilot_hook_status_for_dashboard()
    if _measure_facade.detect_runtime() == "cursor":
        return _measure_facade._collect_cursor_hook_status_for_dashboard()
    if _measure_facade.detect_runtime() == "antigravity":
        return _measure_facade._collect_antigravity_hook_status_for_dashboard()
    if _measure_facade.detect_runtime() == "grok":
        return _measure_facade._collect_grok_hook_status_for_dashboard()

    settings, _ = _measure_facade._read_settings_json()

    # Check each hook type
    session_end_installed = _measure_facade._is_hook_installed(settings)
    smart_compact_status = _measure_facade._is_smart_compact_installed(settings)

    # For executable commands: absolute path with shlex.quote (handles spaces/quotes)
    mp_cmd = _measure_facade._shell_script_path()

    return {
        "session_end": {
            "installed": session_end_installed,
            "label": "Session Tracking",
            "description": "Collects usage data after each session. Powers Trends and Health tabs.",
            "install_cmd": f"{_measure_facade.hint_python()} {mp_cmd} setup-hook",
            "uninstall_cmd": f"{_measure_facade.hint_python()} {mp_cmd} setup-hook --uninstall",
        },
        "smart_compact": {
            "installed": all(smart_compact_status.values()),
            "partial": any(smart_compact_status.values()) and not all(smart_compact_status.values()),
            "detail": smart_compact_status,
            "label": "Smart Compaction",
            "description": "Captures session state before compaction, restores it after. Protects your working memory.",
            "install_cmd": f"{_measure_facade.hint_python()} {mp_cmd} setup-smart-compact",
            "uninstall_cmd": f"{_measure_facade.hint_python()} {mp_cmd} setup-smart-compact --uninstall",
        },
    }

def _collect_codex_hook_status_for_dashboard():
    """Collect Codex project hook status for dashboard toggle panel."""
    _measure_facade = _compat.facade()
    import codex_doctor

    mp_cmd = _measure_facade._shell_script_path()
    project = _measure_facade.Path.cwd().resolve(strict=False)
    checks = codex_doctor.run_checks(project=project)
    by_name = {check["name"]: check for check in checks}

    def _ok(name):
        _measure_facade = _compat.facade()
        return by_name.get(name, {}).get("status") == "OK"

    project_arg = _measure_facade.shlex.quote(str(project))
    base = f"{_measure_facade._runtime_cli('codex')} codex-install --project {project_arg}"
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
            "install_cmd": f"{_measure_facade._runtime_cli('codex')} codex-compact-prompt --install",
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
    _measure_facade = _compat.facade()
    import copilot_doctor  # noqa: PLC0415

    mp_cmd = _measure_facade._shell_script_path()
    checks = copilot_doctor.run_checks()
    by_name = {check["name"]: check for check in checks}

    def _ok(name):
        _measure_facade = _compat.facade()
        return by_name.get(name, {}).get("status") == "ok"

    install_cmd = f"{_measure_facade._runtime_cli('copilot')} copilot-install"
    doctor_cmd = f"{_measure_facade._runtime_cli('copilot')} copilot-doctor"

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
            "uninstall_cmd": f"{_measure_facade._runtime_cli('copilot')} copilot-uninstall",
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
    _measure_facade = _compat.facade()
    import cursor_doctor  # noqa: PLC0415

    mp_cmd = _measure_facade._shell_script_path()
    checks = cursor_doctor.run_checks()
    by_name = {check["name"]: check for check in checks}

    def _ok(name):
        _measure_facade = _compat.facade()
        return by_name.get(name, {}).get("status") == "ok"

    install_cmd = f"{_measure_facade._runtime_cli('cursor')} cursor-install"
    doctor_cmd = f"{_measure_facade._runtime_cli('cursor')} cursor-doctor"

    return {
        "cursor_hooks": {
            "installed": _ok("TO hook config"),
            "partial": by_name.get("TO hook config", {}).get("status") == "warn",
            "label": "Cursor Hooks",
            "description": "Merges Token Optimizer into ~/.cursor/hooks.json: sessionStart continuity restore, preToolUse Shell bash compression, postToolUse tally + nudges, preCompact capture, stop-time rollup and session-end dashboard refresh.",
            "install_cmd": install_cmd,
            "uninstall_cmd": f"{_measure_facade._runtime_cli('cursor')} cursor-uninstall",
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
    _measure_facade = _compat.facade()
    import grok_doctor  # noqa: PLC0415

    mp_cmd = _measure_facade._shell_script_path()
    checks = grok_doctor.run_checks()
    by_name = {check["name"]: check for check in checks}

    def _ok(name):
        _measure_facade = _compat.facade()
        return by_name.get(name, {}).get("status") == "ok"

    install_cmd = f"{_measure_facade._runtime_cli('grok')} grok-install"
    doctor_cmd = f"{_measure_facade._runtime_cli('grok')} grok-doctor"

    return {
        "grok_hooks": {
            "installed": _ok("TO hook config"),
            "partial": by_name.get("TO hook config", {}).get("status") == "warn",
            "label": "Grok Build Hooks",
            "description": "Wires Token Optimizer into $GROK_HOME/hooks/token-optimizer.json: sessionStart continuity restore, userPromptSubmit quality tracking, preToolUse bash compression (capability-gated), postToolUse crash-recovery tally + nudges, stop-time rollup.",
            "install_cmd": install_cmd,
            "uninstall_cmd": f"{_measure_facade._runtime_cli('grok')} grok-uninstall",
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
            "install_cmd": f"{_measure_facade._runtime_cli('grok')} open-dashboard",
            "uninstall_cmd": "",
        },
    }

def _collect_hermes_hook_status_for_dashboard():
    """Collect Hermes plugin hook status for dashboard toggle panel.

    Mirrors _collect_codex_hook_status_for_dashboard(). Uses hermes_doctor
    to determine whether the plugin payload is installed and wired correctly,
    and exposes the same install/uninstall command surface.
    """
    _measure_facade = _compat.facade()
    import hermes_doctor  # noqa: PLC0415

    mp_cmd = _measure_facade._shell_script_path()
    checks = hermes_doctor.run_checks()
    by_name = {check["name"]: check for check in checks}

    def _ok(name):
        _measure_facade = _compat.facade()
        return by_name.get(name, {}).get("status") == "OK"

    install_cmd = f"{_measure_facade._runtime_cli('hermes')} hermes-install"
    doctor_cmd = f"{_measure_facade._runtime_cli('hermes')} hermes-doctor"

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
            "install_cmd": f"{_measure_facade._runtime_cli('hermes')} open-dashboard",
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
    _measure_facade = _compat.facade()
    import antigravity_doctor  # noqa: PLC0415

    mp_cmd = _measure_facade._shell_script_path()
    checks = antigravity_doctor.run_checks()
    by_name = {check["name"]: check for check in checks}

    def _ok(name):
        _measure_facade = _compat.facade()
        return by_name.get(name, {}).get("status") == "ok"

    install_cmd = f"{_measure_facade._runtime_cli('antigravity')} antigravity-install"
    doctor_cmd = f"{_measure_facade._runtime_cli('antigravity')} antigravity-doctor"

    return {
        "antigravity_plugin": {
            "installed": _ok("plugin directory") and _ok("plugin payload"),
            "label": "Antigravity Plugin",
            "description": "Installs the Token Optimizer plugin into ~/.gemini/config/plugins/token-optimizer/. Provides continuity restore, context nudges, bash compression, and stop rollup.",
            "install_cmd": install_cmd,
            "uninstall_cmd": f"{_measure_facade._runtime_cli('antigravity')} antigravity-uninstall",
        },
        "antigravity_hooks": {
            "installed": _ok("plugin hooks"),
            "partial": by_name.get("plugin hooks", {}).get("status") == "warn",
            "label": "Antigravity Hook Declarations",
            "description": "Verifies hooks.json declares PreInvocation, PreToolUse (run_command matcher), and Stop.",
            "install_cmd": install_cmd,
            "uninstall_cmd": f"{_measure_facade._runtime_cli('antigravity')} antigravity-uninstall",
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
            "install_cmd": f"{_measure_facade._runtime_cli('antigravity')} open-dashboard",
            "uninstall_cmd": "",
        },
    }

def _parse_skill_frontmatter(path: Path) -> dict:
    _measure_facade = _compat.facade()
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
    meta["tokens"] = _measure_facade._estimate_tokens(discovery_text)
    meta["body_tokens"] = _measure_facade._estimate_tokens(text)
    return meta

def _collect_codex_skill_inventory(cfg: dict, *, project: Path) -> dict[str, list[dict]]:
    _measure_facade = _compat.facade()
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
                    resolved = _measure_facade.Path(raw_path).expanduser().resolve(strict=False)
                    disabled_paths.add(str(resolved))
                    if resolved.name != "SKILL.md":
                        disabled_paths.add(str((resolved / "SKILL.md").resolve(strict=False)))

    candidates: list[tuple[_measure_facade.Path, str]] = []
    for root, source in (
        (project / ".codex" / "skills", "project"),
        (_measure_facade.RUNTIME_DIR / "skills", "user"),
    ):
        if root.exists():
            candidates.extend((p, source) for p in root.rglob("SKILL.md"))

    plugin_cache = _measure_facade.RUNTIME_DIR / "plugins" / "cache"
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
        _measure_facade = _compat.facade()
        try:
            rel = _measure_facade.Path(path_str).relative_to(plugin_cache.resolve(strict=False))
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
        _measure_facade = _compat.facade()
        nums = _measure_facade.re.findall(r"\d+", name)
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
    mp_cmd = _measure_facade._shell_script_path()
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
        meta = _measure_facade._parse_skill_frontmatter(path)
        item = {
            "name": meta.get("name") or path.parent.name,
            "skill_name": meta.get("name") or path.parent.name,
            "description": meta.get("description", ""),
            "tokens": meta.get("tokens", 0),
            "source": source,
            "path": _measure_facade._display_path(resolved),
            "disable_cmd": f"{_measure_facade._runtime_cli('codex')} codex-skill disable --path {_measure_facade.shlex.quote(resolved)}",
            "enable_cmd": f"{_measure_facade._runtime_cli('codex')} codex-skill enable --path {_measure_facade.shlex.quote(resolved)}",
        }
        _pkey = _plugin_key_for(resolved)
        if _pkey is not None:
            item["plugin"] = _pkey
        if resolved in disabled_paths or (_pkey is not None and _pkey in disabled_plugin_keys):
            disabled.append(item)
        else:
            active.append(item)
    active.sort(key=lambda item: ((_measure_facade := _compat.facade()), (item["source"], item["name"]))[1])
    disabled.sort(key=lambda item: ((_measure_facade := _compat.facade()), (item["source"], item["name"]))[1])
    return {"active": active, "disabled": disabled}

def _collect_codex_mcp_inventory(cfg: dict) -> list[dict]:
    _measure_facade = _compat.facade()
    servers = cfg.get("mcp_servers")
    if not isinstance(servers, dict):
        return []
    items = []
    mp_cmd = _measure_facade._shell_script_path()
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
            "tokens": _measure_facade.TOKENS_PER_DEFERRED_TOOL,
            "enabled": enabled,
            "disable_cmd": f"{_measure_facade._runtime_cli('codex')} codex-mcp disable {_measure_facade.shlex.quote(str(name))}",
            "enable_cmd": f"{_measure_facade._runtime_cli('codex')} codex-mcp enable {_measure_facade.shlex.quote(str(name))}",
        })
    return sorted(items, key=lambda item: ((_measure_facade := _compat.facade()), item["name"])[1])

def _collect_codex_plugin_inventory(cfg: dict) -> list[dict]:
    _measure_facade = _compat.facade()
    plugins = cfg.get("plugins")
    if not isinstance(plugins, dict):
        return []
    items = []
    for name, plugin_cfg in plugins.items():
        enabled = True
        if isinstance(plugin_cfg, dict):
            enabled = bool(plugin_cfg.get("enabled", True))
        items.append({"name": name, "enabled": enabled})
    return sorted(items, key=lambda item: ((_measure_facade := _compat.facade()), item["name"])[1])

def _codex_config_path() -> Path:
    _measure_facade = _compat.facade()
    return _measure_facade.runtime_home() / "config.toml"

def _safe_codex_config_path() -> Path:
    _measure_facade = _compat.facade()
    home = _measure_facade.runtime_home().resolve(strict=False)
    path = _measure_facade._codex_config_path()
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

def _codex_config_lock():
    """Bounded portable lease for config.toml writes.

    Serializes concurrent read-modify-write cycles on config.toml (skill
    enable/disable, MCP toggles). Contenders skip the mutation after 75ms
    rather than blocking a hook.
    """
    _measure_facade = _compat.facade()
    lease_path = _measure_facade._CODEX_CONFIG_LOCK_PATH.with_suffix(".lease")
    with _measure_facade.lease_lock(
        lease_path,
        deadline=_measure_facade.current_deadline(),
        acquire_timeout=0.075,
    ) as acquired:
        yield acquired

def _skill_mgmt_lock():
    """Bounded portable lease serializing skill archive/restore mutations.

    The dashboard daemon and a CLI `measure.py skill archive|restore` run in
    separate processes with no in-process serialization, so this cross-process
    lease closes the archive/restore TOCTOU windows.
    Contenders skip the mutation after 75ms rather than blocking a hook.
    """
    _measure_facade = _compat.facade()
    lock_path = _measure_facade.CLAUDE_DIR / "_backups" / ".skill-mgmt.lock"
    lease_path = lock_path.with_suffix(".lease")
    with _measure_facade.lease_lock(
        lease_path,
        deadline=_measure_facade.current_deadline(),
        acquire_timeout=0.075,
    ) as acquired:
        yield acquired


def _write_codex_config(text: str) -> None:
    _measure_facade = _compat.facade()
    path = _measure_facade._safe_codex_config_path()
    with _measure_facade._codex_config_lock() as acquired:
        if not acquired:
            return
        _measure_facade.codex_io.atomic_write(path, text)


def _toml_string(value: str) -> str:
    _measure_facade = _compat.facade()
    return _measure_facade.json.dumps(value)


def _set_toml_key(block: str, key: str, value: str) -> str:
    _measure_facade = _compat.facade()
    pattern = _measure_facade.re.compile(rf"(?m)^([ \t]*){_measure_facade.re.escape(key)}([ \t]*=[^\n]*)$")
    if pattern.search(block):
        return pattern.sub(lambda match: ((_measure_facade := _compat.facade()), f"{match.group(1)}{key} = {value}")[1], block, count=1)
    suffix = "" if block.endswith("\n") else "\n"
    return block + suffix + f"{key} = {value}\n"


def _iter_toml_array_table_spans(text: str, header: str):
    _measure_facade = _compat.facade()
    pattern = _measure_facade.re.compile(rf"(?m)^[ \t]*\[\[{_measure_facade.re.escape(header)}\]\][ \t]*(?:#.*)?$")
    next_header = _measure_facade.re.compile(r"(?m)^[ \t]*\[\[?[^\]\n]+\]?\][ \t]*(?:#.*)?$")
    for match in pattern.finditer(text):
        next_match = next_header.search(text, match.end())
        yield match.start(), next_match.start() if next_match else len(text)


def _decode_toml_string(raw: str) -> str:
    _measure_facade = _compat.facade()
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        try:
            return _measure_facade.json.loads(raw)
        except _measure_facade.json.JSONDecodeError:
            return raw.strip('"')
    return raw.strip("'\"")


def _codex_skill_target(raw_path: str | None = None, name: str | None = None) -> Path | None:
    _measure_facade = _compat.facade()
    project = _measure_facade.Path.cwd().resolve(strict=False)
    cfg = _measure_facade._read_codex_config()
    inventory = _measure_facade._collect_codex_skill_inventory(cfg, project=project)
    all_items = inventory["active"] + inventory["disabled"]
    if raw_path:
        target = _measure_facade.Path(raw_path).expanduser().resolve(strict=False)
        if target.name != "SKILL.md":
            target = target / "SKILL.md"
        for item in all_items:
            if _measure_facade.Path(item["path"]).expanduser().resolve(strict=False) == target:
                return target
        return target if target.exists() else None
    if name:
        matches = [item for item in all_items if item["name"] == name or item.get("skill_name") == name]
        if len(matches) == 1:
            return _measure_facade.Path(matches[0]["path"]).expanduser().resolve(strict=False)
    return None


def _manage_codex_skill(action: str, *, raw_path: str | None = None, name: str | None = None) -> bool:
    _measure_facade = _compat.facade()
    target = _measure_facade._codex_skill_target(raw_path=raw_path, name=name)
    if not target:
        return False
    try:
        text = _measure_facade._safe_codex_config_path().read_text(encoding="utf-8")
    except OSError:
        text = ""

    enabled = action == "enable"
    target_str = str(target)
    replacement = None
    for start, end in _measure_facade._iter_toml_array_table_spans(text, "skills.config"):
        block = text[start:end]
        path_match = _measure_facade.re.search(r"(?m)^[ \t]*path[ \t]*=[ \t]*(.+?)[ \t]*(?:#.*)?$", block)
        if not path_match:
            continue
        configured = _measure_facade.Path(_measure_facade._decode_toml_string(path_match.group(1))).expanduser().resolve(strict=False)
        if configured.name != "SKILL.md":
            configured = configured / "SKILL.md"
        if configured == target:
            replacement = text[:start] + _measure_facade._set_toml_key(block, "enabled", "true" if enabled else "false") + text[end:]
            break

    if replacement is None:
        suffix = "" if not text or text.endswith("\n") else "\n"
        replacement = (
            text
            + suffix
            + "\n[[skills.config]]\n"
            + f"path = {_measure_facade._toml_string(target_str)}\n"
            + f"enabled = {'true' if enabled else 'false'}\n"
        )
    _measure_facade._write_codex_config(replacement)
    return True


def _toml_table_header_variants(prefix: str, name: str) -> list[str]:
    _measure_facade = _compat.facade()
    if _measure_facade.re.fullmatch(r"[A-Za-z0-9_-]+", name):
        return [f"[{prefix}.{name}]"]
    return [f"[{prefix}.{_measure_facade._toml_string(name)}]"]


def _set_codex_named_table_enabled(prefix: str, name: str, enabled: bool) -> bool:
    _measure_facade = _compat.facade()
    path = _measure_facade._safe_codex_config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    next_header = _measure_facade.re.compile(r"(?m)^[ \t]*\[[^\]\n]+\][ \t]*(?:#.*)?$")
    for header in _measure_facade._toml_table_header_variants(prefix, name):
        pattern = _measure_facade.re.compile(rf"(?m)^[ \t]*{_measure_facade.re.escape(header)}[ \t]*(?:#.*)?$")
        match = pattern.search(text)
        if not match:
            continue
        next_match = next_header.search(text, match.end())
        end = next_match.start() if next_match else len(text)
        block = text[match.start():end]
        updated = _measure_facade._set_toml_key(block, "enabled", "true" if enabled else "false")
        _measure_facade._write_codex_config(text[:match.start()] + updated + text[end:])
        return True
    return False


def _manage_codex_mcp(action: str, name: str) -> bool:
    _measure_facade = _compat.facade()
    return _measure_facade._set_codex_named_table_enabled("mcp_servers", name, action == "enable")


def _collect_management_data(components=None, trends=None):
    """Collect data for the Manage tab: active/archived skills, MCP servers."""
    _measure_facade = _compat.facade()
    if components is None:
        components = _measure_facade.measure_components()

    mp_cmd = _measure_facade._shell_script_path()
    if _measure_facade.detect_runtime() == "codex":
        project = _measure_facade.Path.cwd().resolve(strict=False)
        project_arg = _measure_facade.shlex.quote(str(project))
        base = f"{_measure_facade._runtime_cli('codex')} codex-install --project {project_arg}"
        cfg = _measure_facade._read_codex_config()
        codex_skills = _measure_facade._collect_codex_skill_inventory(cfg, project=project)
        codex_mcp = _measure_facade._collect_codex_mcp_inventory(cfg)
        codex_mcp_active = [item for item in codex_mcp if item.get("enabled", True)]
        codex_mcp_disabled = [item for item in codex_mcp if not item.get("enabled", True)]
        codex_plugins = _measure_facade._collect_codex_plugin_inventory(cfg)
        return {
            "mode": "codex",
            "codex": {
                "project": _measure_facade._display_path(project),
                "install_cmd": base,
                "install_quiet_profile_cmd": base + " --profile quiet",
                "install_balanced_profile_cmd": base + " --profile balanced",
                "install_telemetry_profile_cmd": base + " --profile telemetry",
                "install_aggressive_profile_cmd": base + " --profile aggressive",
                "install_with_bash_compression_cmd": base + " --enable-bash-compression",
                "install_with_hot_path_hooks_cmd": base + " --enable-hot-path-hooks --enable-prompt-hooks",
                "install_with_status_line_cmd": base + " --enable-status-line",
                "refresh_cmd": f"{_measure_facade._runtime_cli('codex')} session-end-flush --trigger manual --no-defer",
                "doctor_cmd": f"{_measure_facade._runtime_cli('codex')} codex-doctor --project {project_arg}",
                "dashboard_cmd": f"{_measure_facade._runtime_cli('codex')} dashboard",
            },
            "skills": {"active": codex_skills["active"], "archived": [], "disabled": codex_skills["disabled"]},
            "mcp_servers": {"active": codex_mcp_active, "disabled": codex_mcp_disabled, "cloud": []},
            "plugins": codex_plugins,
            "v5_features": _measure_facade._get_v5_feature_status(),
        }

    # Hermes: list its own skills (~/.hermes/skills) and MCP servers
    # (config.yaml mcp_servers) read-only, without scanning CLAUDE_DIR,
    # settings.json or claude_desktop_config.json.
    if _measure_facade.detect_runtime() == "hermes":
        hermes_cfg = _measure_facade._hermes_config(_measure_facade.runtime_home())
        hermes_skills = _measure_facade._hermes_skill_inventory(_measure_facade.runtime_home(), hermes_cfg)
        hermes_mcp = _measure_facade._hermes_mcp_inventory(hermes_cfg)
        return {
            "mode": "hermes",
            "read_only": True,
            "skills": hermes_skills,
            "mcp_servers": {"active": [i for i in hermes_mcp if i["enabled"]],
                            "disabled": [i for i in hermes_mcp if not i["enabled"]], "cloud": []},
            "plugins": [],
            "v5_features": _measure_facade._get_v5_feature_status(),
        }

    # Cursor: same shape as Hermes — Cursor has no Claude-style skills/MCP/
    # settings.json management surface, and falling through would silently
    # collect CLAUDE_DIR/_backups and Claude skills/MCP under a Cursor pin.
    if _measure_facade.detect_runtime() == "cursor":
        return {
            "mode": "cursor",
            "skills": {"active": [], "archived": []},
            "mcp_servers": {"active": [], "disabled": [], "cloud": []},
            "plugins": [],
            "v5_features": _measure_facade._get_v5_feature_status(),
        }

    backups_dir = _measure_facade.CLAUDE_DIR / "_backups"

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
            "archive_cmd": f"{_measure_facade.hint_python()} {mp_cmd} skill archive {_measure_facade.shlex.quote(name)}",
        })

    # Archived skills (scan backup dirs)
    archived_skills = []
    if backups_dir.exists():
        for archive_dir in sorted(backups_dir.iterdir(), reverse=True):
            if not archive_dir.is_dir() or not archive_dir.name.startswith(_measure_facade.SKILL_ARCHIVE_DIR_PREFIXES):
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
                    is_symlink_record = not has_skill_md and (item / _measure_facade.SYMLINK_TARGET_MARKER).exists()
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
                    "restore_cmd": f"{_measure_facade.hint_python()} {mp_cmd} skill restore {_measure_facade.shlex.quote(item.name)}",
                })

    # MCP servers (local settings.json)
    settings, _ = _measure_facade._read_settings_json()
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
            "disable_cmd": f"{_measure_facade.hint_python()} {mp_cmd} mcp disable {_measure_facade.shlex.quote(name)}",
        })

    disabled_mcps = []
    for name in sorted(disabled_config.keys()):
        disabled_mcps.append({
            "name": name,
            "source": "local",
            "enable_cmd": f"{_measure_facade.hint_python()} {mp_cmd} mcp enable {_measure_facade.shlex.quote(name)}",
        })

    # Cloud-synced MCP servers (Claude Desktop config)
    cloud_mcps = []
    desktop_config = _measure_facade.HOME / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if desktop_config.exists():
        try:
            dc = _measure_facade.json.loads(desktop_config.read_text(encoding="utf-8"))
            for name in sorted(dc.get("mcpServers", {}).keys()):
                if name not in mcp_servers_config and name not in disabled_config:
                    cfg = dc["mcpServers"][name]
                    cloud_mcps.append({
                        "name": name,
                        "source": "cloud",
                        "command": cfg.get("command", ""),
                    })
        except (_measure_facade.json.JSONDecodeError, OSError):
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
        "v5_features": _measure_facade._get_v5_feature_status(),
    }


def plugin_cleanup(dry_run=None, quiet=None):
    """Report stale plugin cache dirs and archive local/plugin skill overlaps.

    Two actions:
    1. Stale cache REPORTING: lists old plugin version dirs in ~/.claude/plugins/cache/
       not referenced by installPath. Does NOT delete them because installPath is not
       always the authoritative source (Claude Code's loader may resolve via marketplace
       source, especially for directory-sourced plugins). Users should review manually.
    2. Local/plugin overlap: archives local skills in ~/.claude/skills/ that duplicate
       plugin-installed skills (only bare SKILL.md; keeps skills with custom reference files).
    """
    _measure_facade = _compat.facade()
    import shutil

    actions_taken = []

    # Defense-in-depth: under a foreign runtime (OpenCode, Copilot),
    # do not touch ~/.claude.
    if _measure_facade._is_foreign_runtime():
        if not quiet:
            print(f"  Skipped: {_measure_facade.runtime_name_for_humans()} runtime detected; not modifying ~/.claude.")
        return actions_taken

    # --- Report 1: Stale cache version dirs (report only, never delete) ---
    registry = _measure_facade.CLAUDE_DIR / "plugins" / "installed_plugins.json"
    cache_dir = _measure_facade.CLAUDE_DIR / "plugins" / "cache"

    active_paths = set()
    if registry.exists():
        try:
            with open(registry, "r", encoding="utf-8") as f:
                data = _measure_facade.json.load(f)
            for plugin_key, installs in data.get("plugins", {}).items():
                if not isinstance(installs, list):
                    continue
                for inst in installs:
                    raw = inst.get("installPath", "")
                    if raw:
                        active_paths.add(str(_measure_facade.Path(raw).resolve()))
        except (_measure_facade.json.JSONDecodeError, OSError):
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
                data = _measure_facade.json.load(f)

            # Load enabledPlugins to only check active plugins
            enabled = None
            if _measure_facade._is_regular_file(_measure_facade.SETTINGS_PATH):
                try:
                    settings = _measure_facade.json.loads(_measure_facade.SETTINGS_PATH.read_text(encoding="utf-8"))
                    enabled = settings.get("enabledPlugins")
                except (_measure_facade.json.JSONDecodeError, OSError):
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
                    install_path = _measure_facade.Path(raw)
                    if not install_path.exists():
                        continue
                    skills_path = install_path / "skills"
                    if skills_path.is_dir():
                        for item in skills_path.iterdir():
                            if item.is_dir() and (item / "SKILL.md").exists():
                                plugin_skill_names.add(item.name)
        except (_measure_facade.json.JSONDecodeError, OSError):
            pass

    # Check ~/.claude/skills/ for overlaps
    # Only archive if local skill is a plain symlink OR has no extra files beyond SKILL.md.
    # Local skills with custom reference files (loaded on-demand) have content the plugin
    # version lacks, so archiving them would lose functionality.
    skills_dir = _measure_facade.CLAUDE_DIR / "skills"
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

    backups_dir = _measure_facade.CLAUDE_DIR / "_backups"
    if overlaps:
        if not quiet:
            print(f"  Local/plugin overlaps: {len(overlaps)} skills loaded twice")
        today = _measure_facade.time.strftime("%Y%m%d")
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
                        target = _measure_facade.os.readlink(item)
                        dest.mkdir(parents=True, exist_ok=True)
                        (dest / _measure_facade.SYMLINK_TARGET_MARKER).write_text(target, encoding="utf-8")
                        item.unlink()
                    else:
                        shutil.move(str(item), str(dest))
                    if not quiet:
                        print(f"    archived: {item.name} -> {archive_dir.name}/")
                    structural_ledger.record_skill_action("archive", item.name, dest)
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
    _measure_facade = _compat.facade()
    with _measure_facade._skill_mgmt_lock() as acquired:
        if not acquired:
            return False
        return _measure_facade._manage_skill_locked(action, name)


def _manage_skill_locked(action, name):
    _measure_facade = _compat.facade()
    import shutil

    # Defense-in-depth: never mutate ~/.claude/skills when running
    # under a foreign runtime (OpenCode, Copilot), even if reached outside the
    # CLI dispatch guard (e.g. the dashboard /api/v5/toggle path).
    if _measure_facade._is_foreign_runtime():
        print(f"  [!] Refusing to modify ~/.claude skills under the {_measure_facade.runtime_name_for_humans()} runtime.")
        return False

    # Validate name: prevent path traversal
    if not isinstance(name, str) or not name or "/" in name or "\\" in name or name in (".", "..") or "\0" in name:
        print(f"  [!] Invalid skill name: {name}")
        return False
    skills_dir = _measure_facade.CLAUDE_DIR / "skills"
    # Containment check on the UN-resolved path. The name is already validated as
    # a single path component, so the join cannot escape skills_dir. Do NOT use
    # .resolve() here: legitimate skills are often symlinks pointing outside
    # ~/.claude/skills, and resolving the target would misflag them.
    candidate = skills_dir / name
    if candidate.parent != skills_dir:
        print(f"  [!] Path traversal detected: {name}")
        return False
    backups_dir = _measure_facade.CLAUDE_DIR / "_backups"
    today = _measure_facade.datetime.now().strftime("%Y%m%d")
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
                target = _measure_facade.os.readlink(src)
                dst.mkdir(parents=True, exist_ok=True)
                (dst / _measure_facade.SYMLINK_TARGET_MARKER).write_text(target, encoding="utf-8")
                src.unlink()
            else:
                shutil.move(str(src), str(dst))
        except OSError as e:
            print(f"  [!] Failed to archive '{name}': {e}")
            return False
        structural_ledger.record_skill_action("archive", name, dst)
        print(f"  Archived: {name} -> {archive_dir.name}/")
        return True

    elif action == "restore":
        # Search all archive dirs for this skill
        if backups_dir.exists():
            for ad in sorted(backups_dir.iterdir(), reverse=True):
                if not ad.is_dir() or not ad.name.startswith(_measure_facade.SKILL_ARCHIVE_DIR_PREFIXES):
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
                marker = src / _measure_facade.SYMLINK_TARGET_MARKER
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
                structural_ledger.record_skill_action("restore", name, dst)
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
    _measure_facade = _compat.facade()
    if _measure_facade._is_foreign_runtime():
        print(f"  [!] Refusing to modify ~/.claude settings under the {_measure_facade.runtime_name_for_humans()} runtime.")
        return False

    # Writes `settings` back.
    settings, _ok = _measure_facade._read_settings_for_write()
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
        if _measure_facade._write_settings_atomic(settings):
            print(f"  Disabled MCP server: {name}")
            return True
        print(f"  Could not disable MCP server '{name}'; settings.json was not changed.", file=_measure_facade.sys.stderr)
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
        if _measure_facade._write_settings_atomic(settings, allow_removing_keys={"_disabledMcpServers"}):
            print(f"  Enabled MCP server: {name}")
            return True
        print(f"  Could not enable MCP server '{name}'; settings.json was not changed.", file=_measure_facade.sys.stderr)
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
    _measure_facade = _compat.facade()
    files = mem.get("files") or []
    return {
        "codex": True,
        "target": mem.get("path", ""),
        "thread_count": int(mem.get("thread_count", 0) or 0),
        "total_tokens": int(mem.get("tokens", 0) or 0),
        "largest_thread_tokens": max((int(f.get("memory_tokens", 0) or 0) for f in files), default=0),
        "scan_partial": bool(mem.get("truncated")),
    }


def generate_standalone_dashboard(days=None, quiet=None, force=None):
    """Generate a persistent Trends + Health dashboard (no audit data needed).

    Outputs to DASHBOARD_PATH (~/.claude/_backups/token-optimizer/dashboard.html).
    Used by the SessionEnd hook for auto-refresh and for standalone viewing.
    """
    # Skip regeneration if dashboard was updated within the last 60 seconds
    # (prevents rapid /clear cycles from blocking on repeated full pipelines).
    # force=True bypasses the throttle -- used by ensure-health's
    # version-mismatch regen path so a freshly-updated plugin always
    # wins over a recently-written stale file.
    _measure_facade = _compat.facade()
    if days is None:
        days = dashboard_history.resolve_days(dashboard_history.load(_measure_facade.RUNTIME_DIR), _measure_facade.TRENDS_DB)
    if quiet and not force and _measure_facade.DASHBOARD_PATH.exists():
        try:
            age = _measure_facade.time.time() - _measure_facade.DASHBOARD_PATH.stat().st_mtime
            if age < 60:
                return str(_measure_facade.DASHBOARD_PATH)
        except OSError:
            pass
    if _measure_facade._running_under_hook():
        _measure_facade._arm_dashboard_selfheal_on_timeout(days=days)

    # Version-downgrade guard: never let an OLDER build overwrite a dashboard a
    # NEWER build already wrote (the "fixed but still broken" regression -- a
    # stale daemon or pre-upgrade session clobbering a just-shipped fix). Checked
    # BEFORE the expensive data build so an older writer skips cheaply. force does
    # NOT bypass this: force governs the 60s throttle, not version precedence.
    # Newer/equal writers proceed (equal is not strictly newer). Fail-open.
    if _measure_facade._dashboard_on_disk_is_newer(_measure_facade.DASHBOARD_PATH):
        if not quiet:
            print(f"  [Token Optimizer] Skipping dashboard write: the on-disk dashboard "
                  f"was written by a newer version than this build (v{_measure_facade.TOKEN_OPTIMIZER_VERSION}).")
        return str(_measure_facade.DASHBOARD_PATH)

    script_dir = _measure_facade.Path(_measure_facade.__file__).resolve().parent
    template_path = script_dir.parent / "assets" / "dashboard.html"
    if not template_path.exists():
        if not quiet:
            print(f"Error: dashboard template not found at: {template_path}")
        return None

    if not quiet:
        print("  Measuring current token overhead...")
    components = _measure_facade.measure_components()
    totals = _measure_facade.calculate_totals(components)
    baselines = _measure_facade.get_session_baselines(5)

    calibration = _measure_facade.detect_calibration_gap(components, totals, baselines)

    ctx_window, ctx_source = _measure_facade.detect_context_window()
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
        trends = _measure_facade._collect_trends_data(days=days)
    except Exception:
        trends = None

    if not quiet:
        print("  Checking session health...")
    try:
        health = _measure_facade._collect_health_data()
    except Exception:
        health = None

    # Generate auto-recommendations from rules engine
    if not quiet:
        print("  Generating auto-recommendations...")
    auto_plan, rec_count = _measure_facade.generate_auto_recommendations(components, trends=trends, days=days)
    if not quiet and rec_count > 0:
        print(f"  Found {rec_count} auto-recommendations")

    # Generate coach data for the Coach tab (reuse already-collected components/trends)
    if not quiet:
        print("  Generating coach data...")
    try:
        coach = _measure_facade.generate_coach_data(components=components, trends=trends)
    except Exception:
        coach = None

    # Collect context quality data (v2.0)
    if not quiet:
        print("  Analyzing context quality...")
    quality = _measure_facade._collect_quality_for_dashboard()

    # Collect hook installation status for dashboard toggles
    hook_status = _measure_facade._collect_hook_status_for_dashboard()

    # Collect management data for Manage tab
    if not quiet:
        print("  Collecting management data...")
    management = _measure_facade._collect_management_data(components=components, trends=trends)

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
    _turn_preload_budget = _measure_facade._int_env("TOKEN_OPTIMIZER_TURN_PRELOAD_MAX", 40)
    # Also bound per-file size: parse_session_turns reads a whole transcript, and a
    # single long session JSONL can reach 100MB+. Preloading 40 of those under the
    # 45s regen-step / 20s flush budget trips REGEN_STEP_TIMEOUT and can kill a
    # flush worker mid-dashboard-write. Skip oversized transcripts here; served mode
    # still fetches their rows on demand, exactly like older rows past the count cap.
    _turn_preload_maxbytes = _measure_facade._int_env("TOKEN_OPTIMIZER_TURN_PRELOAD_MAX_BYTES", 8_000_000)
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
                jsonl_resolved = str(_measure_facade.Path(jsonl_path).expanduser()) if jsonl_path.startswith("~") else jsonl_path
                if not _measure_facade.os.path.exists(jsonl_resolved):
                    continue
                try:
                    if _measure_facade.os.path.getsize(jsonl_resolved) > _turn_preload_maxbytes:
                        continue
                except OSError:
                    continue
                turns = _measure_facade.parse_session_turns(jsonl_resolved)
                if turns:
                    session_turns[session_key] = turns
    except Exception:
        pass

    pricing_tier = _measure_facade._load_pricing_tier()
    ttl_period_summary = []
    for period in (7, 30):
        try:
            ttl_period_summary.append(_measure_facade._build_ttl_period_summary(period))
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
        if _measure_facade.detect_runtime() == "codex" and mem.get("exists"):
            mr_data = _measure_facade._codex_memory_review_data(mem)
        elif mem.get("exists") and mem.get("path"):
            mem_path = _measure_facade.Path(mem["path"])
            memory_dir = mem_path.parent
            parsed = _measure_facade._mr_parse_memory_index(str(mem_path))
            files_on_disk = _measure_facade._mr_scan_topic_files(str(memory_dir))
            linked_basenames = {_measure_facade.Path(link["target"]).name for link in parsed["links_all"]}
            files_set = set(files_on_disk)
            orphan_count = len(files_set - linked_basenames)
            broken_count = len([link for link in parsed["links_all"] if _measure_facade.Path(link["target"]).name not in files_set])
            total_lines = parsed["total_lines"]
            sev_medium = (1 if total_lines > _measure_facade._MR_MEMORY_LINE_LIMIT else 0) + (1 if broken_count > 0 else 0)
            mr_data = {
                "target": str(mem_path),
                "total_lines": total_lines,
                "entry_count": len(parsed["entries"]),
                "topic_files_count": len(files_on_disk),
                "linked_files_count": len(linked_basenames & files_set),
                "findings": [],  # lightweight mode skips full findings
                "severity_counts": {"high": 0, "medium": sev_medium, "low": orphan_count},
                "savings": {"total_tokens": max(0, total_lines - _measure_facade._MR_MEMORY_LINE_LIMIT) * 15, "by_category": {}},
                "truncated": total_lines > _measure_facade._MR_MEMORY_LINE_LIMIT,
                "truncated_lines": max(0, total_lines - _measure_facade._MR_MEMORY_LINE_LIMIT),
            }
    except Exception:
        pass

    # CLAUDE.md health summary for dashboard card (from already-computed components)
    claude_md_health = None
    try:
        if _measure_facade.detect_runtime() not in ("claude", "codex"):
            # Only Claude Code and Codex have an instruction-file chain this
            # card describes; other runtimes would show a "CLAUDE.md" card.
            raise LookupError
        instruction_tokens = 0
        context_window = components.get("context_window") or _measure_facade.detect_context_window()[0]
        for key in components:
            if (
                (_measure_facade.detect_runtime() == "codex" and key.startswith("agents_md"))
                or (_measure_facade.detect_runtime() != "codex" and key.startswith("claude_md"))
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
    savings_data = _measure_facade._dashboard_savings_data(days=30, include_billing_mode=True)
    if savings_data != {"total_tokens": 0, "total_cost_usd": 0.0, "by_category": {}}:
        try:
            savings_data["billing_mode"] = _measure_facade.keepwarm_billing_mode()
        except Exception:
            pass

    # Cache-TTL watchdog (opportunity tier — observed waste, potential recovery).
    # Standalone analysis; never reaches the realized savings headline. Fail-open.
    # Mirrors generate_dashboard's cache_health wiring so the standalone/daemon
    # dashboard renders the same cache-health panel as the full audit dashboard.
    try:
        cache_health = _measure_facade._cache_ttl_waste_cached(days=30)
    except Exception:
        cache_health = {"available": False, "tier": "opportunity"}

    # U7: keep-warm cache-automation sub-block carried inside the cache-health
    # payload (realized vs spend vs NET, tripwire ratio, the 5 tile states,
    # predictor transparency). Fail-open so it never blocks dashboard regen.
    try:
        if isinstance(cache_health, dict):
            cache_health["keepwarm"] = _measure_facade.keepwarm_cache_health_block(days=30)
    except Exception:
        pass

    # Subscription runway. This generator produces the dashboard the CLI writes,
    # so the key must be assembled here too: the template gates the card on
    # `data.runway` and silently renders nothing when it is absent, which is
    # indistinguishable from the card being switched off. None on API-billed
    # setups or when the meter has never been read (a stale-but-present reading is
    # kept and dated on the card), and the template omits the card.
    try:
        runway = _measure_facade.runway_snapshot(days=30)
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
        "archive_replacement_check": _archive_replacement_check(_measure_facade),
        "cache_health": cache_health,
        "standalone": True,
        "auto_plan": True,
        "generated_at": _measure_facade.datetime.now().isoformat(),
        "pricing_tier": pricing_tier,
        "pricing_tier_label": _measure_facade._pricing_tier_label(pricing_tier),
        "pricing_tiers": {} if _measure_facade.detect_runtime() == "codex" else {k: v["label"] for k, v in _measure_facade.PRICING_TIERS.items()},
        "ttl_period_summary": ttl_period_summary,
        "session_turns": session_turns,
        "memory_review": mr_data,
        "claude_md_health": claude_md_health,
        "v5_recommendation": _measure_facade._get_v5_savings_recommendation(),
        "version": _measure_facade.TOKEN_OPTIMIZER_VERSION,
        "runtime": _measure_facade.detect_runtime(),
        "runtime_label": _measure_facade.runtime_name_for_humans(),
    }
    data = _measure_facade._sanitize_dashboard_paths(data)

    template = template_path.read_text(encoding="utf-8")
    try:  # U9: inline renderer modules kept beside the template (fail-open)
        import dashboard_assets as _dashboard_assets
        template = _dashboard_assets.inline(template, template_path.parent)
    except Exception:
        pass
    data_json = _measure_facade.json.dumps(data, ensure_ascii=True, default=str)
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
    legacy_dashboard = _measure_facade.RUNTIME_DIR / "_backups" / "token-optimizer" / "dashboard.html"
    write_paths = {_measure_facade.DASHBOARD_PATH, legacy_dashboard}
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
            tmp_fd, tmp_name = _measure_facade.tempfile.mkstemp(dir=str(wp.parent), prefix=".dash-", suffix=".tmp")
            try:
                if hasattr(_measure_facade.os, "fchmod"):  # POSIX only (os.fchmod is absent on Windows)
                    _measure_facade.os.fchmod(tmp_fd, 0o600)
                with _measure_facade.os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    f.write(injected)
                _measure_facade.os.replace(tmp_name, str(wp))
            except BaseException:
                # BaseException, not OSError: the SessionEnd flush worker runs this
                # under a 20s hook budget that raises _HookTimeout (a BaseException).
                # A bare `except OSError` would let a mid-write timeout skip the
                # unlink and leak a ~5MB .dash-*.tmp. Clean up on any interruption,
                # then re-raise so the timeout still propagates to the worker.
                try:
                    _measure_facade.os.unlink(tmp_name)
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
                _measure_facade._write_dashboard_meta_atomic(_measure_facade._dashboard_meta_path(wp))
        except OSError:
            pass

    if not quiet:
        print(f"  Dashboard: {_measure_facade.DASHBOARD_PATH}")
        print(f"  Local:  {_measure_facade.DASHBOARD_PATH.as_uri()}")
        print(f"  Remote: {_measure_facade.hint_python()} {_measure_facade._display_path(_measure_facade.Path(_measure_facade.__file__).resolve())} dashboard --serve")

    return str(_measure_facade.DASHBOARD_PATH)


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
    _measure_facade = _compat.facade()
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
                _measure_facade.sys.exit(1)
            serve = True

    def _note_hook_bounded():
        """One stderr line when this run is bounded as a hook.

        A piped-stdin invocation (an agent's shell tool, a runner script) is
        indistinguishable from a real hook, so the 20s budget can kill a heavy
        rebuild and the run exits 1 with no explanation -- the "dashboard
        quietly skips itself" report. Real hook launchers pass --quiet, so
        gating on it keeps hook logs clean while every non-quiet bounded run
        says why and names the --user escape hatch. stderr only: stdout stays
        clean for the `Dashboard:` line consumers parse.
        """
        if "--quiet" in args or "-q" in args:
            return
        print(
            "[Token Optimizer] dashboard: stdin is not a terminal, so this run "
            "is treated as a hook and the rebuild is capped at a "
            f"{_measure_facade._dashboard_hook_budget_seconds():g}s budget; it "
            "may be killed before it finishes or opens. Pass --user to force "
            "an interactive run.",
            file=_measure_facade.sys.stderr,
        )

    if not cp:
        choice = dashboard_history.load(_measure_facade.RUNTIME_DIR)
        explicit = False
        quiet = "--quiet" in args or "-q" in args
        # --force bypasses the 60s write throttle. The detached version-bump
        # self-heal passes it so a stale file just written by a killed regen
        # cannot throttle-skip the heal (the PR #154 version guard still applies).
        force = "--force" in args
        for i, a in enumerate(args):
            if a == "--days" and i + 1 < len(args):
                try:
                    choice = 'all' if args[i + 1] == 'all' else int(args[i + 1])
                    if not dashboard_history.valid(choice):
                        raise ValueError()
                    explicit = True
                except ValueError:
                    print("[Error] --days must be a positive integer or all")
                    _measure_facade.sys.exit(1)
        days = dashboard_history.resolve_days(choice, _measure_facade.TRENDS_DB)
        force = force or explicit
        deadline = (
            None if (serve or not _measure_facade._running_under_hook()) else _measure_facade._install_hook_budget(_measure_facade._dashboard_hook_budget_seconds())
        )
        if deadline is not None:
            _note_hook_bounded()
        timed_out = False
        try:
            lock = (_measure_facade._dashboard_selfheal_build_lock()
                    if _measure_facade.os.environ.get("TOKEN_OPTIMIZER_DASHBOARD_SELFHEAL") == "1"
                    else _measure_facade.nullcontext(True))
            with lock as acquired:
                if not acquired:
                    _measure_facade.sys.exit(0)
                out = _measure_facade.generate_standalone_dashboard(days=days, quiet=quiet, force=force)
        except _measure_facade._HookTimeout:
            out = None
            timed_out = True
        finally:
            _measure_facade._clear_hook_budget(deadline)
        # Bug B self-heal: a bounded hook regen that the 20s budget killed leaves
        # the on-disk dashboard stale. Finish it in a detached, unbounded child so
        # the file catches up without ever blocking the session. forward
        # `force` -- a killed --force regen just wrote a stale file seconds ago, so
        # the detached retry must ALSO bypass the 60s write throttle or it
        # throttle-skips and the file stays stale (the PR #154 version guard still
        # applies, so a forced heal never clobbers a newer dashboard).
        if timed_out:
            _measure_facade._spawn_detached_dashboard_selfheal(days=days, force=force)
        if out and explicit and not _measure_facade.os.environ.get("TOKEN_OPTIMIZER_DASHBOARD_SELFHEAL"):
            try:
                dashboard_history.save(_measure_facade.RUNTIME_DIR, choice)
            except OSError:
                pass
        if out and serve:
            _measure_facade._serve_dashboard(out, port=serve_port, host=serve_host)
        elif out and not quiet:
            _measure_facade._open_dashboard(fallback_filepath=out)
        _measure_facade.sys.exit(0 if out else 1)
    deadline = (
        None if (serve or not _measure_facade._running_under_hook()) else _measure_facade._install_hook_budget(_measure_facade._dashboard_hook_budget_seconds())
    )
    if deadline is not None:
        _note_hook_bounded()
    timed_out = False
    try:
        out = _measure_facade.generate_dashboard(cp)
    except _measure_facade._HookTimeout:
        out = None
        timed_out = True
    finally:
        _measure_facade._clear_hook_budget(deadline)
    # Bug B self-heal (coord-path regen): same as the standalone branch above.
    if timed_out:
        _measure_facade._spawn_detached_dashboard_selfheal()
    if serve and out:
        _measure_facade._serve_dashboard(out, port=serve_port, host=serve_host)


def _collect_trends_from_db(days=None):
    """Query SQLite trends DB for aggregated usage data.

    Returns same dict shape as _collect_trends_from_jsonl, or None if DB
    doesn't exist or has no data for the requested period.
    """
    _measure_facade = _compat.facade()
    if not _measure_facade.TRENDS_DB.exists():
        return None

    try:
        conn = _measure_facade._init_trends_db()
        conn.row_factory = _measure_facade.sqlite3.Row
        # Verify it's a valid DB before proceeding
        conn.execute("SELECT 1 FROM session_log LIMIT 1")
    except (_measure_facade.sqlite3.Error, _measure_facade.sqlite3.DatabaseError):
        try:
            conn.close()
        except Exception:
            pass
        return None

    try:
        _measure_facade._read_path_upkeep(conn, days)
        return _measure_facade._query_trends_db(conn, days)
    except (_measure_facade.sqlite3.Error, _measure_facade.sqlite3.DatabaseError):
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
    _measure_facade = _compat.facade()
    columns = {r[1] for r in conn.execute("PRAGMA table_info(session_log)")}
    if "last_activity_day" not in columns:
        return "", ()
    return " AND (last_activity_day IS NULL OR last_activity_day >= ?)", (cutoff,)


def _query_trends_db(conn, days):
    """Internal: run all queries against the trends DB. Caller handles errors."""
    _measure_facade = _compat.facade()
    cutoff = (_measure_facade.datetime.now() - _measure_facade.timedelta(days=days)).strftime("%Y-%m-%d")

    _measure_facade.ensure_activity_view(conn)
    window_pred, window_params = _measure_facade._activity_window_predicate(conn, cutoff)
    population_metrics = _measure_facade._query_dashboard_population_metrics(conn, days)
    spent_basis = _measure_facade._dashboard_spent_token_basis(conn, days=days)
    coverage = _measure_facade.activity_coverage(conn)

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
        raw, provenance = _measure_facade._spent_daily_usage(parent_raw, combined_raw)
        usage = _measure_facade._safe_json_dict(raw)
        in_window = False
        for day in _measure_facade._activity_days(usage):
            if day < cutoff:
                continue
            in_window = True
            for model, bucket in usage[day].items():
                normalized = _measure_facade._normalize_model_name(model)
                if normalized and isinstance(bucket, dict):
                    reported_model_mix[normalized] = reported_model_mix.get(normalized, 0) + _measure_facade._bucket_int(bucket.get("fresh_input")) + _measure_facade._bucket_int(bucket.get("output"))
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
            calls = _measure_facade.json.loads(tr["tool_calls_json"])
            for tool, count in calls.items():
                total_tools[tool] = total_tools.get(tool, 0) + count
        except (_measure_facade.json.JSONDecodeError, TypeError):
            pass
    total_tools = dict(sorted(total_tools.items(), key=lambda x: ((_measure_facade := _compat.facade()), -x[1])[1]))

    # Installed skills vs used (normalize names: usage logs use SKILL.md name, install list uses dir name)
    components = _measure_facade.measure_components()
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
    never_used_overhead = len(never_used) * _measure_facade.TOKENS_PER_SKILL_APPROX

    # Trajectory
    snapshots = _measure_facade._load_overhead_snapshots()
    current_total = _measure_facade.calculate_totals(components).get("estimated_total", 0)

    # Daily breakdown from session_log
    pricing_tier = _measure_facade._load_pricing_tier()
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
        _measure_facade = _compat.facade()
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
            skills = _measure_facade.json.loads(sr["skills_json"]) if sr["skills_json"] else {}
        except (_measure_facade.json.JSONDecodeError, TypeError):
            skills = {}
        try:
            subagents_meta = _measure_facade.json.loads(sr["subagents_json"]) if sr["subagents_json"] else {}
        except (_measure_facade.json.JSONDecodeError, TypeError):
            subagents_meta = {}

        # One pricing path for every per-session dollar figure (the dashboard
        # here, `session-cost` for plain-language questions): _price_session_row.
        inp_total = sr["input_tokens"] or 0
        out_total = sr["output_tokens"] or 0
        chr_val = sr["cache_hit_rate"] or 0
        cache_create_1h = sr["cache_create_1h_tokens"] or 0
        cache_create_5m = sr["cache_create_5m_tokens"] or 0
        priced_row = _measure_facade._price_session_row(sr, pricing_tier)
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
                "session_key": _measure_facade._make_session_key(jsonl_path),
                "jsonl_path": jsonl_path,
                "topic": sr["topic"],
                "project": _measure_facade._clean_project_name(sr["project"]),
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
                "model": _measure_facade._normalize_model_name(dom_model) or dom_model,
                "model_count": len(mu) if mu else 1,
            }
            # Prefer stored quality score (persisted during collect), fall back to recomputation
            if sr["quality_score"] is not None:
                sd["quality_score"] = sr["quality_score"]
                sd["quality_grade"] = sr["quality_grade"] or _measure_facade.score_to_grade(round(sr["quality_score"]))
                sd["quality_band"] = _measure_facade.score_to_band(sr["quality_score"])
            else:
                sq = _measure_facade.score_session_quality(sd)
                sd["quality_score"] = sq["score"]
                sd["quality_grade"] = sq["grade"]
                sd["quality_band"] = sq["band"]
            d["session_details"].append(sd)
        if window_unpriced > 0:
            total_unpriced_sessions += 1

    daily_sorted = sorted(daily.values(), key=lambda x: ((_measure_facade := _compat.facade()), x["date"])[1], reverse=True)
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
            d["worst_grade"] = min(grades, key=lambda grade: ((_measure_facade := _compat.facade()), grade_rank.get(grade, 999))[1])

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
    pricing_tier = _measure_facade._load_pricing_tier()
    tier_label = _measure_facade._pricing_tier_label(pricing_tier)

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
            "used": dict(sorted(skill_sessions.items(), key=lambda x: ((_measure_facade := _compat.facade()), -x[1])[1])),
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
    _measure_facade = _compat.facade()
    _measure_facade.ensure_activity_view(conn)
    cutoff = (_measure_facade.datetime.now() - _measure_facade.timedelta(days=days)).strftime("%Y-%m-%d")
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


def _collect_trends_from_jsonl(days=None):
    """Use the same dated queries without a persistent DB (read-only fallback).

    Discover all transcripts: mtime is only an ingestion hint. Deduplicate the
    complete history before slicing, including history replayed into a new file.
    """
    _measure_facade = _compat.facade()
    conn = _measure_facade.sqlite3.connect(":memory:")
    conn.row_factory = _measure_facade.sqlite3.Row
    try:
        conn.executescript(_measure_facade._SCHEMA)
        _measure_facade.ensure_activity_schema(conn)
        ledger = {}
        best = {}
        for filepath, mtime, project in _measure_facade._find_all_jsonl_files(None):
            parsed = _measure_facade._parse_session_jsonl(filepath)
            if not parsed:
                continue
            key = _measure_facade._canonical_session_uuid(filepath) or str(filepath)
            rank = _measure_facade._session_rank(parsed.get("message_count"), parsed.get("total_input_tokens"), parsed.get("total_output_tokens"))
            if key not in best or rank > best[key][0]:
                best[key] = (rank, filepath, mtime, project, parsed)
        for _, filepath, mtime, project, original in best.values():
            subagent_files = _measure_facade._session_subagent_files(filepath)
            merged = _measure_facade._session_merged_requests(filepath, original, subagent_files)
            if merged is not None:
                net, counted = _measure_facade._net_and_record(merged, ledger)
                parsed = _measure_facade._parsed_net_of_counted(original, counted)
                daily = _measure_facade._daily_usage_from_merged(net)
                calls = dict(parsed.get("daily_calls") or {})
                for u in net.values():
                    if not u.get("advisor") and u.get("n", 1) and u.get("day"):
                        day = u["day"]
                        calls[day] = calls.get(day, 0) + 1
            else:
                parsed = original
                daily = _measure_facade._session_daily_usage(filepath, parsed=original,
                                             subagent_files=subagent_files)
                calls = dict(original.get("daily_calls") or {})
            daily_metrics = _measure_facade._session_daily_metrics(parsed, subagent_files)
            # Same bounds rule as collect_sessions: combined/parent usage and
            # metadata days, never the file's ingestion timestamp.
            bounds = _measure_facade._activity_bounds(daily, parsed.get("daily_usage"), daily_metrics)
            quality = _measure_facade.score_session_quality(parsed)
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
                "model_usage_json": _measure_facade.json.dumps(parsed.get("model_usage") or {}),
                "model_usage_breakdown_json": _measure_facade.json.dumps(parsed.get("model_usage_breakdown") or {}),
                "daily_usage_json": _measure_facade.json.dumps(daily), "parent_daily_usage_json": _measure_facade.json.dumps(parsed.get("daily_usage") or {}),
                "daily_calls_json": _measure_facade.json.dumps(calls), "daily_metrics_json": _measure_facade.json.dumps(daily_metrics),
                "skills_json": _measure_facade.json.dumps(parsed.get("skills_used") or {}),
                "subagents_json": _measure_facade.json.dumps(parsed.get("subagents_used") or {}),
                "tool_calls_json": _measure_facade.json.dumps(parsed.get("tool_calls") or {}),
                "slug": parsed.get("slug"), "topic": parsed.get("topic"),
                "quality_score": quality["score"], "quality_grade": quality["grade"],
                "is_sidechain": int(bool(parsed.get("is_sidechain"))), "transcript_mtime": mtime,
            }
            conn.execute("INSERT INTO session_log (" + ",".join(values) + ") VALUES ("
                         + ",".join("?" for _ in values) + ")", tuple(values.values()))
        _measure_facade._rebuild_aggregate_tables(conn)
        return _measure_facade._query_trends_db(conn, days)
    finally:
        conn.close()


def _collect_git_commits(days=None):
    """Scan known git repos for commits within the time window.

    Checks project directories under ~/.claude/projects/ (reversing the
    dashed name to a real path) and skill repos under ~/.claude/skills/.

    Returns: { "2026-03-01": [{"repo": "name", "commits": ["msg1", ...]}], ... }
    """
    _measure_facade = _compat.facade()
    cutoff_date = (_measure_facade.datetime.now() - _measure_facade.timedelta(days=days)).strftime("%Y-%m-%d")

    # Collect candidate repo paths
    repo_paths = {}  # path -> display name

    # 1. From project directories
    projects_base = _measure_facade.CLAUDE_DIR / "projects"
    if projects_base.exists():
        for project_dir in projects_base.iterdir():
            if not project_dir.is_dir():
                continue
            # Reverse dashed name to real path: -Users-alex-myproject -> /Users/alex/myproject
            real_path = "/" + project_dir.name.lstrip("-").replace("-", "/")
            rp = _measure_facade.Path(real_path)
            if rp.is_dir() and (rp / ".git").exists():
                repo_paths[str(rp)] = _measure_facade._clean_project_name(project_dir.name)

    # 2. From skill repos
    skills_dir = _measure_facade.CLAUDE_DIR / "skills"
    if skills_dir.exists():
        for skill_dir in skills_dir.iterdir():
            if skill_dir.is_dir() and (skill_dir / ".git").exists():
                repo_paths[str(skill_dir)] = skill_dir.name

    if not repo_paths:
        return {}

    result = {}  # date -> [{"repo": name, "commits": [msg, ...]}]

    for repo_path, display_name in repo_paths.items():
        try:
            proc = _measure_facade.subprocess.run(
                ["git", "-C", repo_path, "log", "--oneline",
                 f"--since={cutoff_date}", "--format=%ai|%s"],
                capture_output=True, text=True, timeout=10, creationflags=_measure_facade._NO_WINDOW,
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
        except (_measure_facade.subprocess.TimeoutExpired, OSError):
            continue

    return result


def _collect_trends_data(days=None):
    """Collect trends data, preferring SQLite DB when available.

    Falls back to live JSONL parsing if DB doesn't exist or is empty.
    """
    # Try SQLite first (faster, accumulated data)
    _measure_facade = _compat.facade()
    result = _measure_facade._collect_trends_from_db(days)
    if result is not None:
        result["git_commits"] = _measure_facade._collect_git_commits(days)
        return result
    # Fall back to live JSONL parsing
    result = _measure_facade._collect_trends_from_jsonl(days)
    if result is not None:
        result["git_commits"] = _measure_facade._collect_git_commits(days)
    return result


def _build_ttl_period_summary(period_days):
    """Build a compact TTL mix summary for a given period."""
    _measure_facade = _compat.facade()
    trends = _measure_facade._collect_trends_data(days=period_days)
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


def usage_trends(days=None, as_json=None):
    """Analyze usage trends across all Claude Code sessions."""
    _measure_facade = _compat.facade()
    trends = _measure_facade._collect_trends_data(days)
    if trends is None:
        print(f"\nNo session logs found in the last {days} days.")
        print(f"Looked in: {_measure_facade.CLAUDE_DIR / 'projects' / '*' / '*.jsonl'}")
        return

    if as_json:
        result = dict(trends)
        result.pop("trajectory", None)
        print(_measure_facade.json.dumps(result, indent=2, default=str))
        return

    session_count = trends["session_count"]
    avg_dur = trends["avg_duration_minutes"]
    avg_in = trends["avg_input_tokens"]
    avg_out = trends["avg_output_tokens"]

    def _fmt_tokens(n):
        _measure_facade = _compat.facade()
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
        for skill, count in sorted(skill_sessions.items(), key=lambda x: ((_measure_facade := _compat.facade()), -x[1])[1])[:15]:
            dots = "." * max(2, 30 - len(skill))
            print(f"    {_measure_facade._strip_ansi(str(skill))} {dots} {count} session{'s' if count != 1 else ''}")
        if len(skill_sessions) > 15:
            print(f"    ... and {len(skill_sessions) - 15} more")
    else:
        print(f"  No skill invocations found in {session_count} sessions.")

    if never_used:
        approx_overhead = len(never_used) * _measure_facade.TOKENS_PER_SKILL_APPROX
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
        for agent, count in sorted(total_subagents.items(), key=lambda x: ((_measure_facade := _compat.facade()), -x[1])[1]):
            dots = "." * max(2, 30 - len(agent))
            print(f"  {_measure_facade._strip_ansi(str(agent))} {dots} {count} spawned")

    total_model_tokens = trends["model_mix"]
    if total_model_tokens:
        # Model shares count fresh input + output (cache reads excluded), unlike
        # the per-session average above; say so, or the two read as contradictory.
        print("\nMODEL MIX (share of tokens, excl. cache reads)")
        grand_total = sum(total_model_tokens.values())
        for model, tokens in sorted(total_model_tokens.items(), key=lambda x: ((_measure_facade := _compat.facade()), -x[1])[1]):
            pct = tokens / grand_total * 100 if grand_total else 0
            dots = "." * max(2, 26 - len(model))
            print(f"  {_measure_facade._strip_ansi(str(model))} {dots} {pct:.0f}% of tokens ({_fmt_tokens(tokens)})")

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


def _status_bar_dir():
    _measure_facade = _compat.facade()
    return _measure_facade.SNAPSHOT_DIR / "status-bar"


def _status_bar_key(session_id):
    """A file-name-safe key for a session: long ids are shortened with a hash so a
    file name never exceeds what the filesystem allows."""
    _measure_facade = _compat.facade()
    sid = str(session_id)
    if len(sid) <= 100:
        return sid
    return sid[:64] + "-" + _measure_facade.hashlib.sha256(sid.encode("utf-8")).hexdigest()[:16]


def _status_bar_cache_path(session_id):
    _measure_facade = _compat.facade()
    return _measure_facade._status_bar_dir() / f"{_measure_facade._status_bar_key(session_id)}.json"


def _status_bar_lock_path(session_id):
    _measure_facade = _compat.facade()
    return _measure_facade._status_bar_dir() / f"{_measure_facade._status_bar_key(session_id)}.refresh.lock"


def _realized_savings_buckets(conn, cutoff, session_uuid=None, by_day=None):
    """Realized savings per bucket, netted exactly like the window headline.

    Buckets are local days (savings_events / compression_events timestamps are
    naive local ISO) when *by_day*, else one bucket keyed "". With
    *session_uuid* only that session's rows count. Each bucket is
    {"tokens": int, "usd": float, "events": int}. Raises sqlite3.Error so the
    caller can tell a busy or broken DB from an empty one.
    """
    _measure_facade = _compat.facade()
    params = [cutoff]
    if session_uuid is not None:
        # Other runtimes' ids are not UUIDs: their rows keep the id in session_id.
        params.extend([session_uuid, session_uuid])
    se_sql = _measure_facade._BUCKET_SAVINGS_SQL[
        _measure_facade._counted_table(conn, "savings_events"), bool(by_day), session_uuid is not None]
    se_table = _measure_facade._counted_table(conn, "savings_events")
    if "detail" not in {r[1] for r in conn.execute(f"PRAGMA table_info({se_table})")}: se_sql = se_sql.replace(_measure_facade._counted_cap.reexpand_type(), "event_type")
    ce_sql = _measure_facade._BUCKET_COMPRESSION_SQL[
        _measure_facade._counted_table(conn, "compression_events"), bool(by_day), session_uuid is not None]
    se_rows = conn.execute(se_sql, tuple(params)).fetchall()
    ce_rows = conn.execute(ce_sql, tuple(params)).fetchall()
    ce_session_rates = _measure_facade._compression_session_rates(
        conn, [r[6] for r in ce_rows
               if not _measure_facade._compression_known_model(r[5])],
        [r[7] for r in ce_rows if not _measure_facade._compression_known_model(r[5])])
    deltas, _rt, _ut = _measure_facade._session_model_reprice(
        conn, cutoff, session_uuid=session_uuid, by_day=True)

    se_by_bucket = {}
    for bucket, event_type, cnt, tok, cost in se_rows:
        se_by_bucket.setdefault(bucket or "", []).append((event_type, cnt, tok, cost))
    deltas_by_bucket = {}
    for (day, event_type), delta in deltas.items():
        key = (day or "") if by_day else ""
        per = deltas_by_bucket.setdefault(key, {})
        per[event_type] = per.get(event_type, 0.0) + delta

    fallback_rate = _measure_facade._estimate_compression_cost_per_mtok()
    tier_data = _measure_facade.PRICING_TIERS.get(_measure_facade._load_pricing_tier(), _measure_facade.PRICING_TIERS["anthropic"])
    ce_by_bucket = {}
    for bucket, feature, cnt, orig, comp, model, s_uuid, sid in ce_rows:
        tokens_saved = (orig or 0) - (comp or 0)
        key = s_uuid if s_uuid in ce_session_rates else ("session_id", sid)
        rate = _measure_facade._compression_priced_rate(model, key, ce_session_rates, fallback_rate, tier_data)
        feats = ce_by_bucket.setdefault(bucket or "", {})
        entry = feats.setdefault(feature, {"events": 0, "tokens_saved": 0, "cost_saved_usd": 0.0})
        entry["events"] += cnt or 0
        entry["tokens_saved"] += tokens_saved
        entry["cost_saved_usd"] = round(
            entry["cost_saved_usd"] + round(tokens_saved * rate / 1_000_000, 6), 6)

    out = {}
    for bucket in set(se_by_bucket) | set(ce_by_bucket):
        by_category, tok, cost, events, _popped, _rp = _measure_facade._net_realized_savings(
            se_by_bucket.get(bucket, []), deltas_by_bucket.get(bucket, {}))
        a_tok, a_cost, a_events = _measure_facade._merge_v5_compression(
            by_category, ce_by_bucket.get(bucket, {}), fallback_rate)
        out[bucket] = {
            "tokens": int(tok + a_tok),
            "usd": round(cost + a_cost, 4),
            "events": int(events + a_events),
        }
    return out


def _open_trends_db_readonly(timeout=None):
    """Read-only connection to trends.db; never creates or migrates it."""
    _measure_facade = _compat.facade()
    uri = _measure_facade.Path(_measure_facade.TRENDS_DB).resolve().as_uri() + "?mode=ro"
    conn = _measure_facade.sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    return conn


def _dashboard_saved_tokens(summary):
    """Return the dashboard token total and its measured floor."""
    _measure_facade = _compat.facade()
    s = summary or {}

    if isinstance(s.get("action_savings"), dict) and isinstance(s.get("token_headline"), dict):
        from . import savings_story
        return savings_story.story_token_total(s), max(0, int(s.get("total_tokens") or 0))

    def num(v):
        _measure_facade = _compat.facade()
        try:
            return max(0, int(float(v or 0)))
        except (TypeError, ValueError, OverflowError):
            return 0

    def tok(obj):
        _measure_facade = _compat.facade()
        try:
            return num((obj or {}).get("tokens_saved", 0))
        except AttributeError:
            return 0

    estimated = tok(s.get("hint_followed") or s.get("hint_followed_estimated"))
    measured = num(s.get("total_tokens", 0))
    return measured + estimated, measured


def _status_bar_savings_or_reason(session_id, _retried=None):
    """Compute the savings object. Returns (savings_dict, None) or (None, reason)."""
    _measure_facade = _compat.facade()
    if not _measure_facade.Path(_measure_facade.TRENDS_DB).exists():
        return None, "no savings recorded yet"
    today = _measure_facade.datetime.now().date()
    first_day = today - _measure_facade.timedelta(days=_measure_facade._STATUS_BAR_DAYS - 1)
    cutoff = _measure_facade.datetime(first_day.year, first_day.month, first_day.day).isoformat()
    try:
        conn = _measure_facade._open_trends_db_readonly()
        try:
            conn.execute("BEGIN")
            has_counted = bool(conn.execute("SELECT 1 FROM counted_reread LIMIT 1").fetchone())
            days = _measure_facade._realized_savings_buckets(conn, cutoff, by_day=True)
            window = _measure_facade._realized_savings_buckets(conn, cutoff).get("", {"tokens": 0})
            sess = _measure_facade._realized_savings_buckets(conn, "", session_uuid=session_id).get(
                "", {"tokens": 0, "usd": 0.0})
            conn.execute("COMMIT")
        finally:
            conn.close()
    except _measure_facade.sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "locked" in msg or "busy" in msg:
            return None, "savings database busy"
        if "no such column" in msg or "no such table" in msg:
            # A database from before these columns: the normal writer migrates it once.
            try:
                migrated = _measure_facade._init_trends_db()
                if migrated is not None:
                    migrated.close()
            except Exception:
                return None, "savings database unreadable"
            if _retried:
                return None, "savings database unreadable"
            return _measure_facade._status_bar_savings_or_reason(session_id, _retried=True)
        return None, "savings database unreadable"
    except (_measure_facade.sqlite3.Error, OSError, ValueError):
        return None, "savings database unreadable"
    daily = []
    for i in range(_measure_facade._STATUS_BAR_DAYS):
        d = (first_day + _measure_facade.timedelta(days=i)).isoformat()
        b = days.get(d, {})
        daily.append({"date": d, "tokens": int(b.get("tokens", 0)),
                      "usd": float(b.get("usd", 0.0))})
    headline = _measure_facade._get_merged_savings(days=_measure_facade._STATUS_BAR_DAYS)
    from . import headline as headline_module
    counted = _measure_facade._counted_cumulative_summary(days=_measure_facade._STATUS_BAR_DAYS) if has_counted else {"available": False}
    headline["counted_period"] = counted
    headline["token_headline"] = headline_module.token_headline(headline)
    _measure_facade._apply_counted_oneshot_pricing(headline, counted)
    headline["action_savings"] = _measure_facade._action_savings_components(headline, counted)
    shown, measured = _measure_facade._dashboard_saved_tokens(headline)
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
        "computed_at": _measure_facade.time.time(),
    }, None


def _status_bar_write_cache(session_id, savings, reason):
    _measure_facade = _compat.facade()
    d = _measure_facade._status_bar_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = _measure_facade._status_bar_cache_path(session_id)
    if savings is None:
        # A failed refresh keeps the last good figures (they read as stale), never null them.
        prior = _measure_facade._status_bar_read_cache(session_id)
        if prior is not None and prior.get("savings") is not None:
            return
    tmp = path.with_name(f".{path.name}.{_measure_facade.os.getpid()}.tmp")
    tmp.write_text(_measure_facade.json.dumps({
        "schema": _measure_facade._STATUS_BAR_SCHEMA,
        "computed_at": _measure_facade.time.time(),
        "savings": savings,
        "reason": reason,
    }), encoding="utf-8")
    _measure_facade.os.replace(tmp, path)
    # Bound the directory: drop caches for sessions untouched for 30 days.
    cutoff = _measure_facade.time.time() - _measure_facade._STATUS_BAR_CACHE_RETENTION_S
    orphan_cutoff = _measure_facade.time.time() - 86400
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
    _measure_facade = _compat.facade()
    try:
        data = _measure_facade.json.loads(_measure_facade._status_bar_cache_path(session_id).read_text(encoding="utf-8"))
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
    _measure_facade = _compat.facade()
    token = _measure_facade._status_bar_acquire_lock(session_id)
    if token is None:
        return False
    # The child releases the lock only if it still holds this token, so a child
    # that outlives its lock never deletes a newer caller's lock.
    proc = _measure_facade.spawn_detached(
        [_measure_facade._detached_python_exe(), str(_measure_facade.MEASURE_PY_PATH), "status-bar",
         "--session", session_id, "--sync", "--release-lock"],
        stdin=_measure_facade.subprocess.DEVNULL, stdout=_measure_facade.subprocess.DEVNULL, stderr=_measure_facade.subprocess.DEVNULL,
        env={**_measure_facade.os.environ, _measure_facade._STATUS_BAR_LOCK_TOKEN_ENV: token})
    if proc is None:
        _measure_facade._log_spawn_failure("status-bar refresh spawn failed")
        _measure_facade._status_bar_release_lock(session_id, token)
        return False
    return True


def _status_bar_acquire_lock(session_id):
    """Take the session's refresh lock; its owner token, or None when it is held."""
    _measure_facade = _compat.facade()
    lock = _measure_facade._status_bar_lock_path(session_id)
    token = _measure_facade.os.urandom(16).hex()
    fd = None
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = _measure_facade.os.open(str(lock), _measure_facade.os.O_CREAT | _measure_facade.os.O_EXCL | _measure_facade.os.O_WRONLY)
        except FileExistsError:
            if _measure_facade.time.time() - lock.stat().st_mtime < _measure_facade._STATUS_BAR_LOCK_STALE_S:
                return None
            # Take an abandoned lock over by renaming it aside. Rename is atomic,
            # so when two callers race only one moves the file; the other gets
            # FileNotFoundError and backs off instead of deleting the winner's lock.
            quarantine = lock.with_name(
                f"{lock.name}.{_measure_facade.os.getpid()}.{_measure_facade.time.time_ns()}.stale")
            try:
                _measure_facade.os.rename(str(lock), str(quarantine))
            except OSError:
                return None
            try:
                moved_fresh = (_measure_facade.time.time() - _measure_facade.os.stat(str(quarantine)).st_mtime
                               < _measure_facade._STATUS_BAR_LOCK_STALE_S)
            except OSError:
                moved_fresh = False
            if moved_fresh:
                # Another caller replaced the stale lock between our check and the
                # rename, so we moved its live lock. Put it back without
                # overwriting anything (link fails if the name is taken) and back off.
                _measure_facade._status_bar_put_back(quarantine, lock)
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
                fd = _measure_facade.os.open(str(lock), _measure_facade.os.O_CREAT | _measure_facade.os.O_EXCL | _measure_facade.os.O_WRONLY)
            except FileExistsError:
                return None
        written = _measure_facade.os.write(fd, token.encode("ascii"))
        _measure_facade.os.close(fd)
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
                _measure_facade.os.close(fd)
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
    _measure_facade = _compat.facade()
    if not token:
        return False
    lock = _measure_facade._status_bar_lock_path(session_id)
    aside = lock.with_name(f"{lock.name}.{_measure_facade.os.getpid()}.{_measure_facade.time.time_ns()}.release")
    try:
        _measure_facade.os.rename(str(lock), str(aside))
    except OSError:
        return False
    try:
        mine = aside.read_text(encoding="ascii", errors="replace").strip() == token
    except OSError:
        mine = False
    if not mine:
        _measure_facade._status_bar_put_back(aside, lock)
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
    _measure_facade = _compat.facade()
    try:
        _measure_facade.os.link(str(aside), str(lock))
    except (OSError, AttributeError):
        pass


def _status_bar_compactions(path, session_id=None):
    """How many times this transcript was compacted (compact_boundary rows), or None.

    Counts rows whose parsed JSON is a system compact_boundary (the same key can
    sit inside a structured tool result). The quality cache's own count can lag
    a compaction, and the band shows this number right after one. The transcript
    only grows, so with a session id the count resumes after the last whole line
    the previous call read (kept beside the status cache).
    """
    _measure_facade = _compat.facade()
    memo = None
    if session_id:
        try:
            memo = _measure_facade._status_bar_dir() / f"compactions-{_measure_facade._status_bar_key(_measure_facade.sanitize_session_id(session_id))}.json"
        except Exception:
            memo = None
    start, count = 0, 0
    try:
        size = _measure_facade.os.path.getsize(path)
    except OSError:
        return None
    if memo is not None:
        try:
            m = _measure_facade.json.loads(memo.read_text(encoding="utf-8"))
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
                if _measure_facade._COMPACT_MARK not in line:
                    continue
                try:
                    row = _measure_facade.json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(row, dict) and row.get("type") == "system" and row.get("subtype") == "compact_boundary":
                    count += 1
        if memo is not None:
            try:
                # Never written backwards: a slower reader that ended earlier leaves a newer memo alone.
                try:
                    cur = _measure_facade.json.loads(memo.read_text(encoding="utf-8"))
                    newer = cur.get("path") == str(path) and int(cur.get("size", -1)) > end
                except (OSError, ValueError, TypeError):
                    newer = False
                if not newer:
                    memo.parent.mkdir(parents=True, exist_ok=True)
                    tmp = memo.with_name(f".{memo.name}.{_measure_facade.os.getpid()}.tmp")
                    tmp.write_text(_measure_facade.json.dumps({"path": str(path), "size": end, "count": count}), encoding="utf-8")
                    _measure_facade.os.replace(tmp, memo)
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
    _measure_facade = _compat.facade()
    last_ts = None
    lifetime = None
    model = None

    def _take(raw):
        _measure_facade = _compat.facade()
        nonlocal last_ts, lifetime, model
        raw = raw.strip()
        if not raw or b'"assistant"' not in raw:
            return
        try:
            rec = _measure_facade.json.loads(raw)
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
            last_ts = _measure_facade._keepwarm_iso_to_epoch(rec.get("timestamp"))
        if lifetime is None:
            try:
                kind = _measure_facade._keepwarm_ttl_kind(usage)
            except (TypeError, ValueError, OverflowError):
                kind = "unknown"  # one malformed row; the older rows still count
            if kind != "unknown":
                lifetime = kind

    try:
        with open(path, "rb") as fh:
            pos = fh.seek(0, _measure_facade.os.SEEK_END)
            carry = b""
            scanned = 0
            while pos > 0 and scanned < _measure_facade._STATUS_BAR_TAIL_MAX_BYTES:
                step = min(_measure_facade._STATUS_BAR_TAIL_CHUNK, pos)
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
    _measure_facade = _compat.facade()
    dirs = [_measure_facade.QUALITY_CACHE_DIR, _measure_facade.RUNTIME_DIR / "token-optimizer"]
    env_pd = _measure_facade.os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
    if env_pd:
        dirs.append(_measure_facade.Path(env_pd) / "token-optimizer")
    try:
        for d in (_measure_facade.RUNTIME_DIR / "plugins" / "data").iterdir():
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
    _measure_facade = _compat.facade()
    best, best_mtime = None, -1.0
    for d in _measure_facade._status_bar_quality_cache_dirs():
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
    _measure_facade = _compat.facade()
    newest_file = None
    for d in _measure_facade._status_bar_quality_cache_dirs():
        try:
            for f in (d / "checkpoints").glob(f"{session_id}-*.md"):
                mt = int(f.stat().st_mtime)
                if newest_file is None or mt > newest_file:
                    newest_file = mt
        except OSError:
            continue
    cached = _measure_facade._status_bar_cached_checkpoint_epoch(session_id)
    found = [v for v in (cached, newest_file) if v is not None]
    return max(found) if found else None


def _status_bar_cached_checkpoint_epoch(session_id):
    """last_checkpoint_epoch from the freshest quality-cache-<sid>.json, or None.

    None when the checkpoint it names is no longer on disk (retention removed
    it), so the band never reports a save that cannot be restored.
    """
    _measure_facade = _compat.facade()
    best = _measure_facade._status_bar_freshest(f"quality-cache-{session_id}.json")
    if best is None:
        return None
    try:
        cache = _measure_facade.json.loads(best.read_text(encoding="utf-8"))
        val = cache.get("last_checkpoint_epoch")
        saved = cache.get("last_checkpoint_path")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    if not isinstance(saved, str) or not saved:
        return None
    try:
        if not _measure_facade.Path(saved).is_file():
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
    _measure_facade = _compat.facade()
    best = _measure_facade._status_bar_freshest(f"resumable-{session_id}.json")
    if best is None:
        return None
    try:
        flag = _measure_facade.json.loads(best.read_text(encoding="utf-8"))
        # Only a checkpoint on this work: an unrelated recent one is noise here.
        if flag.get("relevant") is not True:
            return None
        cp = _measure_facade.Path(str(flag.get("checkpoint") or ""))
        # Only a file in one of Token Optimizer's own checkpoint folders is read.
        homes = {str((d / "checkpoints").resolve()) for d in _measure_facade._status_bar_quality_cache_dirs()}
        if cp.suffix != ".md" or str(cp.resolve().parent) not in homes:
            return None
        epoch = int(cp.stat().st_mtime)
    except (OSError, ValueError, AttributeError, TypeError, RuntimeError):
        return None
    if not cp.name or cp.name.startswith(session_id):
        return None
    try:
        about = _measure_facade._checkpoint_descriptor(cp) or None
    except Exception:
        about = None
    return {"epoch": epoch, "about": about}


def _status_bar_prompt_cache(session_id):
    """The session's native prompt_cache object, bridged by statusline.js, or None.

    Claude Code v2.1.251+ hands the status line a `prompt_cache` object
    (ttl, expires_at, warm, hit_ratio, ...; main conversations only).
    statusline.js mirrors it to prompt-cache-<sid>.json so this payload can
    source the cache countdown/cold state from it instead of guessing from
    the transcript. Stale or malformed files fall back to the transcript.
    """
    _measure_facade = _compat.facade()
    best = _measure_facade._status_bar_freshest(f"prompt-cache-{session_id}.json")
    if best is None:
        return None
    try:
        data = _measure_facade.json.loads(best.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        if _measure_facade.time.time() - float(data.get("timestamp", 0)) / 1000 > _measure_facade._PROMPT_CACHE_SIDECAR_MAX_AGE_S:
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


def status_bar_payload(session_id, transcript=None, sync=None):
    """Build the status-bar JSON object (see STATUS_BAR_HELP). Never raises."""
    _measure_facade = _compat.facade()
    sid = _measure_facade.sanitize_session_id(session_id)
    session_note = None
    # --session may carry a pasted truncated id. A <6-char id
    # sanitizes to "unknown"; a longer prefix finds no exact transcript.
    # Resolve a UNIQUE prefix to the real session; when it matches zero or
    # many, say so in savings_reason instead of degrading silently.
    prefix = _measure_facade.re.sub(r"[^a-zA-Z0-9_-]", "", str(session_id or ""))
    try:
        path = _measure_facade.Path(transcript) if transcript else (
            _measure_facade._find_session_jsonl_by_id(sid) if sid != "unknown" else None)
    except Exception:
        path = None
    if path is None and prefix:
        matches = _measure_facade._session_id_prefix_matches(prefix)
        if len(matches) == 1:
            path = matches[0]
            sid = matches[0].stem
        elif len(matches) > 1:
            session_note = ("session id prefix '%s' is ambiguous "
                            "(%d sessions match)" % (prefix, len(matches)))
        elif sid == "unknown":
            session_note = "no session id matches '%s'" % prefix
    out = {
        "schema": _measure_facade._STATUS_BAR_SCHEMA,
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
                _measure_facade._status_bar_transcript_state(path))
            out["compactions"] = _measure_facade._status_bar_compactions(path, sid)
        # The native prompt_cache object (v2.1.251+) is authoritative for the
        # cache countdown/cold state when statusline.js bridged it recently;
        # the transcript-derived lifetime stays as the fallback.
        pc = _measure_facade._status_bar_prompt_cache(sid)
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
        model = session_model or _measure_facade.os.environ.get("CLAUDE_MODEL") or _measure_facade.os.environ.get("ANTHROPIC_MODEL")
        resolved = _measure_facade._resolve_compact_window(model)
        out["compactWindow"] = {
            "tokens": resolved["tokens"] if resolved["user_override"] else None,
            "source": resolved["source"],
        }
    except Exception:
        pass
    try:
        out["last_checkpoint_epoch"] = _measure_facade._status_bar_checkpoint_epoch(sid)
    except Exception:
        pass
    try:
        out["earlier_checkpoint"] = _measure_facade._status_bar_earlier_checkpoint(sid)
    except Exception:
        pass

    try:
        if sync:
            savings, reason = _measure_facade._status_bar_savings_or_reason(sid)
            try:
                _measure_facade._status_bar_write_cache(sid, savings, reason)
            except OSError:
                pass
            out["savings"], out["savings_reason"] = savings, reason
            out["savings_age_s"] = 0.0
            out["savings_state"] = "fresh" if savings is not None else "unavailable"
            return out
        if not _measure_facade.Path(_measure_facade.TRENDS_DB).exists():
            out["savings_reason"] = "no savings recorded yet"
            return out
        cached = _measure_facade._status_bar_read_cache(sid)
        if cached is None:
            out["savings_state"] = "loading"
            out["savings_reason"] = "computing savings"
            out["refresh_started"] = _measure_facade._status_bar_start_refresh(sid)
            return out
        age = max(0.0, _measure_facade.time.time() - float(cached["computed_at"]))
        out["savings"] = cached.get("savings")
        out["savings_reason"] = cached.get("reason") if out["savings"] is None else None
        out["savings_age_s"] = round(age, 1)
        stale = age > _measure_facade._STATUS_BAR_CACHE_MAX_AGE_S
        if out["savings"] is None:
            out["savings_state"] = "unavailable"
        else:
            out["savings_state"] = "stale" if stale else "fresh"
        if stale:
            out["refresh_started"] = _measure_facade._status_bar_start_refresh(sid)
    except Exception:
        out["savings"] = None
        out["savings_state"] = "unavailable"
        out["savings_reason"] = "savings unavailable"
    return out


def _status_bar_cli(args):
    """`status-bar` subcommand. Prints one JSON object; exit 0 (2 on bad usage)."""
    _measure_facade = _compat.facade()
    if "--help" in args or "-h" in args:
        print(_measure_facade.STATUS_BAR_HELP)
        _measure_facade.sys.exit(0)

    def _opt(name):
        _measure_facade = _compat.facade()
        if name in args:
            i = args.index(name)
            # A following flag is not a value: `--session --json` has no session.
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                return args[i + 1]
        return None

    session = _opt("--session")
    if not session:
        print(_measure_facade.STATUS_BAR_HELP, file=_measure_facade.sys.stderr)
        _measure_facade.sys.exit(2)
    sync = "--sync" in args
    child = sync and "--release-lock" in args
    sid = _measure_facade.sanitize_session_id(session)
    token = _measure_facade.os.environ.get(_measure_facade._STATUS_BAR_LOCK_TOKEN_ENV, "") if child else None
    if sync and not child:
        # A direct --sync takes the same lock as the background refresh; when one is
        # already running, this answers from the cache instead of computing twice.
        token = _measure_facade._status_bar_acquire_lock(sid) if sid != "unknown" else None
        if token is None:
            sync = False
    # The background child gets a hard wall-clock budget: a hung read never lingers.
    def _out_of_time():
        # Killed by its budget: leave a reason for the band and free the lock.
        _measure_facade = _compat.facade()
        try:
            _measure_facade._status_bar_write_cache(sid, None, "savings took too long to compute")
        except OSError:
            pass
        _measure_facade._status_bar_release_lock(sid, token)
        _measure_facade._log_spawn_failure("status-bar refresh child ran out of time")

    budget = _measure_facade._install_hook_budget(_measure_facade._STATUS_BAR_CHILD_BUDGET_S, on_timeout=_out_of_time) if child else None
    try:
        payload = _measure_facade.status_bar_payload(session, transcript=_opt("--transcript"), sync=sync)
    except Exception:
        payload = {"schema": _measure_facade._STATUS_BAR_SCHEMA, "session_id": sid, "savings": None,
                   "savings_state": "unavailable", "savings_reason": "savings unavailable"}
    finally:
        if token:
            _measure_facade._status_bar_release_lock(sid, token)
        if budget is not None:
            try:
                budget.cancel()
            except Exception:
                pass
    # Always one JSON object, exit 0: JSON has no Infinity or NaN, so those read as null.
    print(_measure_facade.json.dumps(_measure_facade._status_bar_finite(payload), allow_nan=False))
    _measure_facade.sys.exit(0)


def _status_bar_finite(v):
    """The payload with every non-finite float (inf, nan) replaced by None."""
    _measure_facade = _compat.facade()
    if isinstance(v, float):
        return v if _measure_facade.math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _measure_facade._status_bar_finite(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_measure_facade._status_bar_finite(x) for x in v]
    return v


def _archive_replacement_check(facade):
    """Health of archive replacements, read back from transcripts (archive_verify)."""
    try:
        import archive_verify
        conn = facade.sqlite3.connect(facade.TRENDS_DB.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
        try:
            return archive_verify.health(conn)
        finally:
            conn.close()
    except Exception:
        return {"status": "unknown", "verified": 0, "full": 0, "full_streak": 0}
