"""Regression tests for daemon threading + non-blocking health.

The generated dashboard daemon used `socketserver.TCPServer` (single-threaded),
so a synchronous `POST /api/regenerate` (10-17s build) blocked EVERY other
request. Liveness probes (`/api/health`, `/__to_ping`) use 0.5-1s timeouts, so
during a regen the daemon read as DEAD, which could trigger a revive ->
`kickstart -k` that restarted the daemon mid-regen and aborted the click.

The fix:
  1. Switch the server to `socketserver.ThreadingTCPServer`.
  2. Guard shared per-request globals (`_regen_inflight`, `_last_regen`,
     `_MEASURE_PY_CACHE`, `_REJECT_LOG_LAST_TS`) with `_STATE_LOCK`.
  3. `/api/health` and `/__to_ping` already answer from constants without doing
     work, so under the threading server they answer instantly even mid-regen.

These tests boot the REAL generated daemon script in a subprocess against a
fake `measure.py` whose `collect`/`dashboard` steps sleep, so a regen takes a
known ~3s without depending on real session data. They assert:
  - A GET /api/health sent WHILE a regen is running returns in well under the
    regen duration (the liveness probe is not blocked).
  - A second POST /api/regenerate sent while the first is running is refused
    with 409 (the _regen_inflight guard still prevents overlap).
  - The generated source ships ThreadingTCPServer + _STATE_LOCK (structural).
"""

import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

# How long the fake measure.py sleeps PER regen step. Two steps (collect,
# dashboard) run, so a full regen takes ~2*REGEN_STEP_SLEEP. Chosen well above
# any health-probe timeout so a blocked probe would reliably time out, and well
# below the per-test budget.
REGEN_STEP_SLEEP = 1.5


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _generated_src() -> str:
    import measure

    return measure._generate_daemon_script()


# --- structural guards -------------------------------------------------------

def test_generated_daemon_uses_threading_server():
    src = _generated_src()
    assert "ThreadingTCPServer" in src, (
        "generated daemon is not on a ThreadingTCPServer; a synchronous regen "
        "would block liveness probes again"
    )
    assert "socketserver.TCPServer((" not in src, (
        "generated daemon still instantiates the single-threaded TCPServer"
    )
    assert "daemon_threads = True" in src, (
        "ThreadingTCPServer must set daemon_threads=True so worker threads die "
        "with the daemon and never block process exit"
    )
    assert "allow_reuse_address = True" in src, (
        "ThreadingTCPServer must set allow_reuse_address=True so a fast "
        "KeepAlive respawn does not hit TIME_WAIT on the port"
    )


def test_generated_daemon_has_state_lock():
    src = _generated_src()
    assert re.search(r"^_STATE_LOCK = threading\.Lock\(\)$", src, re.M), (
        "generated daemon is missing the _STATE_LOCK that guards shared "
        "per-request globals under the threading server"
    )
    # The regen-inflight guard must take the lock for the check+set so two
    # concurrent clicks cannot both pass the overlap check.
    m = re.search(r'if clean in \("api/regenerate", "api/regenerate/status"\):.*?(?=\n        self\.send_error\(403)',
                  src, re.S)
    assert m, "api/regenerate handler not found in generated daemon"
    regen_block = m.group(0)
    assert "_regen_inflight" in regen_block
    assert "with _STATE_LOCK:" in regen_block, (
        "api/regenerate does not take _STATE_LOCK around the _regen_inflight "
        "check+set; overlapping regens could race past the guard"
    )
    # Progress updates take the lock briefly, but subprocess.run must not be
    # nested in a lock block.
    import ast
    tree = ast.parse(src)
    worker = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'run_regen')
    calls = [n for n in ast.walk(worker) if isinstance(n, ast.Call) and ast.unparse(n.func) == 'subprocess.run']
    assert calls
    for block in (n for n in ast.walk(worker) if isinstance(n, ast.With)):
        if any(ast.unparse(item.context_expr) == '_STATE_LOCK' for item in block.items):
            assert not any(call in ast.walk(block) for call in calls)


def test_health_and_identity_routes_do_no_work():
    """The cheap liveness routes must answer from constants, not touch the
    filesystem or kick a regen, so they stay instant under the threading server."""
    src = _generated_src()
    # /api/health is handled before _require_localhost and before any file work.
    # The generated source has single braces (the f-string {{ }} is evaluated).
    m = re.search(r'if clean == "api/health":\s*self\._json_response\(200, \{"ok": True,'
                  r' "server": "token-optimizer-daemon", "version": TOKEN_OPTIMIZER_DAEMON_VERSION\}\)',
                  src)
    assert m, "/api/health does not answer from a constant payload"
    # __to_ping returns the identity magic constant.
    m = re.search(r'def _respond_identity\(self\):.*?body = IDENTITY_MAGIC\.encode\("utf-8"\)',
                  src, re.S)
    assert m, "/__to_ping does not answer from the IDENTITY_MAGIC constant"


# --- bounded concurrency (structural) ----------------------------------------

def test_generated_daemon_bounds_worker_threads():
    """ThreadingTCPServer spawns one thread per connection with no cap.
    The generated daemon must bound concurrency with a semaphore and drop idle
    connections with a socket timeout, or a slowloris / request flood spawns
    unbounded threads + child processes."""
    src = _generated_src()
    # A fixed, conservative cap via a BoundedSemaphore.
    m = re.search(r"DAEMON_MAX_WORKERS = (\d+)", src)
    assert m, "generated daemon has no DAEMON_MAX_WORKERS cap"
    cap = int(m.group(1))
    assert 4 <= cap <= 32, f"worker cap {cap} outside the conservative range"
    assert "_WORKER_SEM = threading.BoundedSemaphore(DAEMON_MAX_WORKERS)" in src, (
        "worker cap is not a BoundedSemaphore over DAEMON_MAX_WORKERS"
    )
    # The accept loop must acquire NON-BLOCKING (so it never stalls) and answer a
    # 503 on saturation instead of spawning another thread.
    assert re.search(r"def process_request\(self, request, client_address\):.*?"
                     r"_WORKER_SEM\.acquire\(blocking=False\).*?"
                     r"503 Service Unavailable", src, re.S), (
        "process_request does not non-blocking-acquire the worker semaphore and "
        "answer 503 on saturation"
    )
    # The permit must be released in process_request_thread's finally (every exit
    # path, handler exception included) so the pool can never leak permits.
    m = re.search(r"def process_request_thread\(self, request, client_address\):(.*?)"
                  r"(?=\n    with _DaemonServer)", src, re.S)
    assert m, "process_request_thread override not found"
    prt = m.group(1)
    assert "finally:" in prt and "_WORKER_SEM.release()" in prt, (
        "process_request_thread does not release the worker permit in a finally"
    )
    # A per-connection socket read timeout kills slowloris (idle connection can't
    # pin a worker forever). Set via the handler's `timeout` class attribute, which
    # StreamRequestHandler.setup() applies as settimeout().
    assert re.search(r"^\s*timeout = DAEMON_REQUEST_TIMEOUT$", src, re.M), (
        "Handler does not set a socket read timeout (slowloris can pin a worker)"
    )
    assert re.search(r"DAEMON_REQUEST_TIMEOUT = [\d.]+", src), (
        "DAEMON_REQUEST_TIMEOUT is not defined"
    )


def test_generated_daemon_background_refresh_respects_regen_inflight():
    """the background stale-while-revalidate refresh must claim the SAME
    _regen_inflight flag (under _STATE_LOCK) as the manual regen path, or under the
    threading server a background refresh and a manual regen could run at once."""
    src = _generated_src()
    m = re.search(r"def _maybe_refresh_dashboard\(self\):(.*?)(?=\n    def do_OPTIONS)",
                  src, re.S)
    assert m, "_maybe_refresh_dashboard not found"
    body = m.group(1)
    assert "global _last_regen, _regen_inflight" in body, (
        "_maybe_refresh_dashboard does not declare _regen_inflight global"
    )
    # It must bail when a regen is already in flight, and claim the flag before
    # spawning the child, all under the lock.
    assert re.search(r"with _STATE_LOCK:.*?if _regen_inflight:.*?return.*?"
                     r"_regen_inflight = True", body, re.S), (
        "background refresh does not check-and-set _regen_inflight under _STATE_LOCK"
    )
    # A reaper clears the flag when the fire-and-forget child exits (bounded, so it
    # can never stick True).
    assert "threading.Thread(target=_reap_bg_regen" in body, (
        "background refresh does not hand the guard to a reaper thread"
    )
    reaper = re.search(r"def _reap_bg_regen\(proc\):(.*?)(?=\n\n)", src, re.S)
    assert reaper, "_reap_bg_regen not found"
    rbody = reaper.group(1)
    assert "proc.wait(timeout=REGEN_BG_TIMEOUT)" in rbody, (
        "reaper does not wait on the child with a bounded timeout"
    )
    assert "finally:" in rbody and "_regen_inflight = False" in rbody, (
        "reaper does not clear _regen_inflight in a finally (could stick True)"
    )
    # And the not-spawned path in _maybe_refresh_dashboard must also clear the flag
    # it claimed (skip / launch failure), so it can never wedge True.
    assert re.search(r"if not spawned:.*?_regen_inflight = False", body, re.S), (
        "background refresh does not release the claimed flag when it never spawns"
    )


# --- live subprocess behaviour ----------------------------------------------

@pytest.fixture
def live_daemon(tmp_path, monkeypatch):
    """Boot the real generated daemon against a fake measure.py whose regen
    steps sleep, so a regen takes a known ~3s without real session data."""
    import measure

    port = _free_port()
    data = tmp_path / "data"
    data.mkdir()
    dashboard = data / "dashboard.html"
    dashboard.write_text("<html>ok</html>", encoding="utf-8")
    token_path = data / "daemon-token"
    token = "test-token-160"
    token_path.write_text(token, encoding="utf-8")
    host_path = data / "dashboard-host"
    host_path.write_text("127.0.0.1", encoding="utf-8")
    thrash_path = data / ".daemon-thrash"
    log_dir = data / "logs"
    log_dir.mkdir()

    # Build the fake measure.py at the path the daemon's _resolve_measure_py
    # will walk: <container>/<version>/skills/token-optimizer/scripts/measure.py.
    # _resolve_measure_py derives version_dir = parents[3] of the fallback and
    # container = dirname(version_dir), then lists container for version dirs.
    identity = "test-identity"
    version = "9.9.9"
    fake_measure = (
        tmp_path / "install" / identity / "token-optimizer" / version
        / "skills" / "token-optimizer" / "scripts" / "measure.py"
    )
    fake_measure.parent.mkdir(parents=True)
    fake_measure.write_text(
        "import sys, time\n"
        "import sys as _s\n"
        # Sleep per step so a full regen (collect + dashboard) takes ~2x sleep.
        "time.sleep(%r)\n"
        "raise SystemExit(0)\n" % REGEN_STEP_SLEEP,
        encoding="utf-8",
    )

    # Monkeypatch the module globals _generate_daemon_script reads so the
    # generated script points at our temp paths + fake measure.py + free port.
    monkeypatch.setattr(measure, "DASHBOARD_PATH", dashboard)
    monkeypatch.setattr(measure, "DAEMON_TOKEN_PATH", token_path)
    monkeypatch.setattr(measure, "DAEMON_HOST_PATH", host_path)
    monkeypatch.setattr(measure, "DAEMON_THRASH_BREADCRUMB", thrash_path)
    monkeypatch.setattr(measure, "DAEMON_LOG_DIR", log_dir)
    monkeypatch.setattr(measure, "DAEMON_PORT", port)
    # measure_py_literal = repr(str(Path(__file__).resolve())); point __file__
    # at the fake measure.py so the daemon resolves to it.
    monkeypatch.setattr(measure, "__file__", str(fake_measure))

    src = measure._generate_daemon_script()
    compile(src, "<generated-daemon-160>", "exec")
    daemon_script = tmp_path / "daemon.py"
    daemon_script.write_text(src, encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, str(daemon_script)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    base = f"http://127.0.0.1:{port}"

    def _stop():
        proc.terminate()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    # Wait for the daemon to bind and answer the identity probe.
    deadline = time.time() + 60
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate(timeout=1)
            pytest.fail(
                "daemon exited before binding. rc=%s stderr=%s"
                % (proc.returncode, err.decode("utf-8", "replace")[:800])
            )
        try:
            with urllib.request.urlopen(base + "/__to_ping", timeout=0.5) as r:
                if r.read(len("token-optimizer-dashboard-v1") + 8).strip():
                    ready = True
                    break
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.1)
    if not ready:
        _stop()
        pytest.fail("daemon did not become ready on port %d" % port)

    class _Daemon:
        pass

    d = _Daemon()
    d.base = base
    d.port = port
    d.token = token
    d.proc = proc
    d._stop = _stop
    d.dashboard = dashboard
    d.regen_step_sleep = REGEN_STEP_SLEEP
    try:
        yield d
    finally:
        _stop()


def _port_from_base(base):
    return int(base.rsplit(":", 1)[1])


def test_health_probe_is_not_blocked_during_regen(live_daemon):
    """A GET /api/health sent WHILE a regen is running must return in well under
    the regen duration. Under the old single-threaded server this blocked for
    the whole ~3s build (and a 0.5-1s liveness probe timed out -> false death).
    """
    base = live_daemon.base
    token = live_daemon.token
    port = _port_from_base(base)

    regen_done = threading.Event()
    regen_started = threading.Event()
    regen_error = {}

    def _run_regen():
        try:
            req = urllib.request.Request(
                base + "/api/regenerate", data=b"{}", method="POST",
                headers={
                    "Origin": f"http://127.0.0.1:{port}",
                    "Host": f"127.0.0.1:{port}",
                    "X-TO-Token": token,
                    "Content-Type": "application/json",
                },
            )
            regen_started.set()
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
            regen_done.set()
        except Exception as e:  # noqa: BLE001
            regen_error["err"] = repr(e)
            regen_done.set()

    t = threading.Thread(target=_run_regen, daemon=True)
    t.start()
    # Let the regen POST actually reach the server and start the subprocess.
    assert regen_started.wait(timeout=60), "regen thread did not start"
    # Give the handler time to enter the synchronous subprocess.run() block.
    time.sleep(0.4)

    # The regen takes ~2*REGEN_STEP_SLEEP. Probe health with a timeout well
    # BELOW that: if the server is still single-threaded, this times out.
    probe_timeout = max(1.0, REGEN_STEP_SLEEP)  # < 2*REGEN_STEP_SLEEP
    t0 = time.time()
    try:
        with urllib.request.urlopen(base + "/api/health", timeout=probe_timeout) as r:
            body = r.read()
            elapsed = time.time() - t0
    except (urllib.error.URLError, OSError) as e:
        live_daemon._stop()
        pytest.fail(
            "health probe was blocked/timed out during a regen: %r" % e
        )

    assert elapsed < 2 * REGEN_STEP_SLEEP, (
        f"health probe took {elapsed:.2f}s during a regen that lasts "
        f"~{2*REGEN_STEP_SLEEP}s -- it was blocked by the synchronous regen"
    )
    assert b'"ok": true' in body.lower() or b'"ok":true' in body.lower(), (
        "health probe did not return the ok payload: %r" % body
    )

    # Let the regen finish so the fixture can tear down cleanly.
    assert regen_done.wait(timeout=30), (
        "regen did not complete: %s" % regen_error.get("err", "(no error)")
    )
    assert "err" not in regen_error, "regen failed: %s" % regen_error.get("err")


def test_overlapping_regen_is_refused(live_daemon):
    """A second POST /api/regenerate sent while the first is running must be
    refused with 409 immediately. The _regen_inflight guard predates the fix; the
    fix keeps it working under the threading server via _STATE_LOCK."""
    base = live_daemon.base
    token = live_daemon.token
    port = _port_from_base(base)

    first_started = threading.Event()
    second_result = {}

    def _run_first():
        try:
            req = urllib.request.Request(
                base + "/api/regenerate", data=b"{}", method="POST",
                headers={
                    "Origin": f"http://127.0.0.1:{port}",
                    "Host": f"127.0.0.1:{port}",
                    "X-TO-Token": token,
                    "Content-Type": "application/json",
                },
            )
            first_started.set()
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
        except Exception as e:  # noqa: BLE001
            second_result.setdefault("first_err", repr(e))

    t = threading.Thread(target=_run_first, daemon=True)
    t.start()
    assert first_started.wait(timeout=60), "first regen did not start"
    time.sleep(0.4)  # let the first enter the synchronous subprocess.run()

    # Fire a second regen while the first is mid-flight. It must come back fast
    # with 409, not queue behind the first for ~3s.
    t0 = time.time()
    req = urllib.request.Request(
        base + "/api/regenerate", data=b"{}", method="POST",
        headers={
            "Origin": f"http://127.0.0.1:{port}",
            "Host": f"127.0.0.1:{port}",
            "X-TO-Token": token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            second_result["code"] = r.getcode()
            second_result["body"] = r.read()
    except urllib.error.HTTPError as e:
        second_result["code"] = e.code
        second_result["body"] = e.read()
    elapsed = time.time() - t0

    assert second_result.get("code") == 409, (
        "second overlapping regen was not refused with 409; got %s body=%r"
        % (second_result.get("code"), second_result.get("body"))
    )
    assert elapsed < 2 * REGEN_STEP_SLEEP, (
        f"second regen took {elapsed:.2f}s to be refused -- it queued behind "
        "the first instead of being rejected immediately"
    )
    # Let the first finish for clean teardown.
    t.join(timeout=30)
    assert "first_err" not in second_result, (
        "first regen failed: %s" % second_result.get("first_err")
    )


# --- bounded concurrency (live) ----------------------------------------------

def _daemon_config():
    """Read the worker cap + request timeout the generated daemon ships with, so
    the live tests stay in lockstep with the template values."""
    src = _generated_src()
    cap = int(re.search(r"DAEMON_MAX_WORKERS = (\d+)", src).group(1))
    timeout = float(re.search(r"DAEMON_REQUEST_TIMEOUT = ([\d.]+)", src).group(1))
    return cap, timeout


def test_saturation_returns_503_and_does_not_spawn_unbounded_threads(live_daemon):
    """with the worker cap saturated by held connections, one more
    connection must be answered with 503 (never a newly spawned unbounded worker).
    Each held connection connects but sends no request line, so its worker blocks
    on readline and keeps its permit for the whole request-timeout window."""
    cap, req_timeout = _daemon_config()
    port = live_daemon.port

    held = []
    try:
        # Saturate: open exactly `cap` connections. process_request acquires a
        # permit synchronously in the single accept loop BEFORE spawning the
        # worker thread, so once these are accepted all permits are gone.
        for _ in range(cap):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(("127.0.0.1", port))
            held.append(s)
        # Give the accept loop time to accept + acquire for all `cap` sockets.
        time.sleep(1.0)

        # One more connection must be refused with 503 (the accept loop sends it
        # at accept time when the semaphore is exhausted, then closes).
        extra = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        extra.settimeout(req_timeout + 5)
        extra.connect(("127.0.0.1", port))
        try:
            # We need not even send a request: saturation is answered at accept.
            data = b""
            deadline = time.time() + req_timeout + 4
            while time.time() < deadline:
                chunk = extra.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\r\n\r\n" in data:
                    break
        finally:
            extra.close()

        assert data.startswith(b"HTTP/1.1 503"), (
            "a connection past the worker cap was not refused with 503 (unbounded "
            "thread creation regression); got %r" % data[:80]
        )
    finally:
        for s in held:
            try:
                s.close()
            except OSError:
                pass


def test_idle_connection_times_out(live_daemon):
    """slowloris: a connection that connects but never finishes sending a
    request line must be dropped by the daemon after the socket read timeout, so
    it cannot pin a bounded worker forever. The server closing the socket surfaces
    as recv() returning b'' within ~DAEMON_REQUEST_TIMEOUT."""
    _cap, req_timeout = _daemon_config()
    port = live_daemon.port

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Client waits generously past the server's own timeout so we observe the
    # SERVER closing the connection, not a client-side timeout.
    s.settimeout(req_timeout + 8)
    s.connect(("127.0.0.1", port))
    # Send a partial request line and never terminate it -> handler's readline
    # blocks until the server-side socket timeout fires.
    try:
        s.sendall(b"GET / HTTP/1.1\r\n")  # no blank line -> request never completes
    except OSError:
        pass
    t0 = time.time()
    try:
        # BaseHTTPRequestHandler catches socket.timeout, sets close_connection and
        # returns -> the server closes the socket, so recv returns b'' (EOF).
        data = s.recv(4096)
    except socket.timeout:
        s.close()
        pytest.fail(
            "server did not close an idle/slow connection within %.1fs; a "
            "slowloris connection can pin a worker forever" % (req_timeout + 8)
        )
    elapsed = time.time() - t0
    s.close()

    # Closed (EOF) or a timeout/408-style response, but it MUST have happened
    # around the server timeout, not hung open indefinitely.
    assert elapsed <= req_timeout + 6, (
        "idle connection was not dropped near the %.1fs server timeout (took "
        "%.2fs)" % (req_timeout, elapsed)
    )
    assert data == b"" or data.startswith(b"HTTP/"), (
        "unexpected data from a dropped idle connection: %r" % data[:80]
    )


def test_background_and_manual_regen_cannot_overlap(live_daemon):
    """the background stale-while-revalidate refresh now shares
    _regen_inflight with the manual regen path. A manual POST fired while a
    background refresh is in flight must be refused with 409 -- and once the
    background child is reaped the flag must clear, so a later manual regen
    succeeds (no path leaves _regen_inflight stuck True)."""
    base = live_daemon.base
    token = live_daemon.token
    port = live_daemon.port

    # Make the cached dashboard stale so a GET kicks a background regen. The
    # freshness window is 120s; back-date the file well past it.
    old = time.time() - 600
    os.utime(live_daemon.dashboard, (old, old))

    # A GET on the dashboard runs _maybe_refresh_dashboard synchronously, which
    # spawns the fire-and-forget background child and sets _regen_inflight=True
    # BEFORE the response returns. The fake measure.py sleeps REGEN_STEP_SLEEP,
    # so the flag stays set for that window.
    with urllib.request.urlopen(base + "/", timeout=60) as r:
        r.read()

    # Immediately fire a manual regen: it must see the in-flight background regen
    # and refuse with 409 (the two paths cannot overlap).
    req = urllib.request.Request(
        base + "/api/regenerate", data=b"{}", method="POST",
        headers={
            "Origin": f"http://127.0.0.1:{port}",
            "Host": f"127.0.0.1:{port}",
            "X-TO-Token": token,
            "Content-Type": "application/json",
        },
    )
    code = None
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            code = r.getcode()
            r.read()
    except urllib.error.HTTPError as e:
        code = e.code
        e.read()
    assert code == 409, (
        "manual regen was not refused while a background refresh was in flight; "
        "got %s -- the background path does not respect _regen_inflight" % code
    )

    # Wait for the background child to finish and the reaper to clear the flag.
    # (background = one ~REGEN_STEP_SLEEP step; give generous margin.)
    time.sleep(live_daemon.regen_step_sleep + 2.0)

    # A manual regen must now succeed: the flag was cleared, not left stuck True.
    req2 = urllib.request.Request(
        base + "/api/regenerate", data=b"{}", method="POST",
        headers={
            "Origin": f"http://127.0.0.1:{port}",
            "Host": f"127.0.0.1:{port}",
            "X-TO-Token": token,
            "Content-Type": "application/json",
        },
    )
    code2 = None
    body2 = b""
    try:
        with urllib.request.urlopen(req2, timeout=30) as r:
            code2 = r.getcode()
            body2 = r.read()
    except urllib.error.HTTPError as e:
        code2 = e.code
        body2 = e.read()
    assert code2 == 202, (
        "manual regen after the background refresh finished did not succeed "
        "(got %s, body=%r) -- _regen_inflight may be stuck True" % (code2, body2[:200])
    )
    import json
    job_id = json.loads(body2)['job_id']
    deadline = time.monotonic() + 15
    state = None
    while time.monotonic() < deadline:
        req3 = urllib.request.Request(base + '/api/regenerate/status', data=json.dumps({'job_id': job_id}).encode(),
            headers={'Origin': base, 'X-TO-Token': token, 'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req3, timeout=3) as response:
            state = json.load(response)['state']
        if state != 'running':
            break
        time.sleep(.1)
    assert state == 'done'
