"""Version-bump dashboard/daemon self-heal must not strand the runway placeholder.

ROOT CAUSE (compound). ``run_ensure_health()`` runs under an 8s SIGALRM hook
budget (``_install_hook_budget(8)`` in the ensure-health dispatch). Its stale-HTML
self-heal used to rebuild the dashboard IN-PROCESS via
``generate_standalone_dashboard(quiet=True, force=True)`` -- a ~9s build that
reliably tripped ``_HookTimeout`` (a ``BaseException`` the local ``except
Exception`` cannot catch). That aborted the WHOLE ensure-health tick BEFORE the
daemon-script auto-update block ran, so on a version bump BOTH the HTML and the
daemon script stayed stale. And unlike the CLI ``dashboard`` path, the in-process
call had no detached-child fallback to ever catch the HTML up. The runway card
then kept serving the "Your live 5-hour and weekly limit view refreshes while you
are actively working..." placeholder even though the live meter was healthy.

FIX (class-level). The stale-HTML self-heal now hands the forced rebuild to the
existing DETACHED, unbounded child (``_spawn_detached_dashboard_selfheal(force=
True)``); the cheap spawn returns at once, so the daemon-script auto-update below
is always reached (both heal). The daemon's synchronous ``api/regenerate`` also
forces past the 60s write throttle and retries once on a hang instead of failing
on a single ``REGEN_STEP_TIMEOUT`` shot.

These tests fail at edit time if the fix is reverted or weakened, which is the
recurrence that matters -- this dashboard bug has been "fixed" by symptom patches
several times. Where there is no JS/subprocess runtime in the harness, the
contract is asserted against the shipped source text (the same approach
test_runway_card_wiring.py / test_dashboard_regen_retry.py take).

Run: python3 -m pytest tests/test_dashboard_version_selfheal.py -v
"""
from _activity_fixtures import stamp_activity
import importlib
import inspect
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
ASSET = REPO / "skills" / "token-optimizer" / "assets" / "dashboard.html"

PLACEHOLDER = "Your live 5-hour and weekly limit view refreshes"


@pytest.fixture()
def m(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="to-version-selfheal-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


# --------------------------------------------------------------------------
# 1. The detached self-heal learns --force WITHOUT changing its default argv.
# --------------------------------------------------------------------------

def test_selfheal_default_argv_is_unchanged(m, monkeypatch):
    """The Bug B invariant still holds: the DEFAULT spawn carries no --force, so
    the existing detached-child contract (test_dashboard_selfheal_bugB) is intact.
    """
    captured = {}

    class _FakePopen:
        def __init__(self, argv, **kw):
            captured["argv"] = argv

    monkeypatch.setattr(m.subprocess, "Popen", _FakePopen)
    m._spawn_detached_dashboard_selfheal(days=30)
    assert captured["argv"][1:] == [
        m.os.path.abspath(m.__file__), "dashboard", "--quiet", "--days", "30"
    ]
    assert "--force" not in captured["argv"]


def test_selfheal_force_appends_force_flag(m, monkeypatch):
    """force=True appends --force so the child bypasses the 60s write throttle --
    the version-bump heal must not throttle-skip on a stale file a killed regen
    just wrote."""
    captured = {}

    class _FakePopen:
        def __init__(self, argv, **kw):
            captured["argv"] = argv

    monkeypatch.setattr(m.subprocess, "Popen", _FakePopen)
    m._spawn_detached_dashboard_selfheal(days=30, force=True)
    assert captured["argv"][-1] == "--force"
    assert captured["argv"][1:] == [
        m.os.path.abspath(m.__file__), "dashboard", "--quiet", "--days", "30", "--force"
    ]


# --------------------------------------------------------------------------
# 2. The dashboard CLI threads --force into generate_standalone_dashboard.
# --------------------------------------------------------------------------

def _run_dispatch(m, monkeypatch, args):
    seen = {}

    def _fake_gen(days=30, quiet=False, force=False):
        seen["force"] = force
        seen["quiet"] = quiet
        return "/tmp/dash.html"

    monkeypatch.setattr(m, "generate_standalone_dashboard", _fake_gen)
    monkeypatch.setattr(m, "_running_under_hook", lambda: False)
    monkeypatch.setattr(m, "_open_dashboard", lambda **k: None)
    with pytest.raises(SystemExit):
        m._dispatch_dashboard(args)
    return seen


def test_dispatch_dashboard_threads_force(m, monkeypatch):
    seen = _run_dispatch(m, monkeypatch, ["--quiet", "--force"])
    assert seen["force"] is True


def test_dispatch_dashboard_defaults_to_no_force(m, monkeypatch):
    seen = _run_dispatch(m, monkeypatch, ["--quiet"])
    assert seen["force"] is False


# --------------------------------------------------------------------------
# 3. ensure-health: stale HTML heal is detached+forced, NOT in-process, and the
#    daemon-script auto-update block follows it (so the fast spawn guarantees the
#    daemon update is reached -- the compound-strand regression guard).
# --------------------------------------------------------------------------

def test_ensure_health_hands_html_heal_to_detached_forced_child(m):
    src = inspect.getsource(m.run_ensure_health)
    # The stale-HTML branch must hand off to the detached, forced child...
    assert "_spawn_detached_dashboard_selfheal(days=30, force=True, hook_timeout=False)" in src, (
        "the stale-HTML self-heal must spawn the detached forced child, not run "
        "the ~9s rebuild in-process under the 8s hook budget"
    )
    # ...and must NOT run the rebuild in-process (that is what tripped _HookTimeout
    # and aborted the tick before the daemon-script update could run).
    assert "generate_standalone_dashboard(quiet=True, force=True)" not in src, (
        "the in-process forced regen is the compound-strand bug; it must be gone"
    )


def test_ensure_health_daemon_update_runs_after_html_heal(m):
    """The daemon-script auto-update (version-marker check + regen) must sit AFTER
    the HTML self-heal. With the heal now a fast detached spawn, the tick always
    reaches the daemon update -- the block that was stranded when the in-process
    regen blew the budget."""
    src = inspect.getsource(m.run_ensure_health)
    heal_at = src.index("_spawn_detached_dashboard_selfheal(days=30, force=True, hook_timeout=False)")
    daemon_at = src.index('TOKEN_OPTIMIZER_DAEMON_VERSION = "')
    assert heal_at < daemon_at, "HTML heal must precede the daemon-script update"
    # And the daemon update actually regenerates the script when the marker drifts.
    assert "_generate_daemon_script()" in src
    # compound-strand guard, stronger than ordering alone: NO in-process
    # generate_standalone_dashboard( call may sit BETWEEN the detached spawn and
    # the daemon marker. Ordering alone can't catch a future edit that re-adds a
    # blocking ~9s rebuild there -- that rebuild would blow the 8s budget, raise
    # _HookTimeout, and strand the daemon-script update all over again (the exact
    # regression this branch fixed). The window between the two markers must stay
    # free of the in-process build.
    between = src[heal_at:daemon_at]
    assert "generate_standalone_dashboard(" not in between, (
        "an in-process generate_standalone_dashboard() reappeared between the "
        "detached heal spawn and the daemon-script update -- that re-strands the "
        "daemon block under the 8s hook budget (the compound bug this branch fixed)"
    )


# --------------------------------------------------------------------------
# 4. Daemon api/regenerate: force past the throttle + retry on a hang, not a
#    single REGEN_STEP_TIMEOUT shot.
# --------------------------------------------------------------------------

def test_daemon_regen_forces_dashboard_step_and_retries(m):
    script = m._generate_daemon_script()
    # The dashboard step forces past the 60s throttle so a human's Regenerate can
    # never silently no-op into serving the same stale file.
    assert 'argv.append("--force")' in script
    # retry ONLY the flaky "dashboard" step, and NEVER double the per-step
    # cap. The old "2 attempts per step + doubled retry cap" put the worst case at
    # REGEN_STEP_TIMEOUT*(1+2)*2 = 6x = 270s, WORSE than the ~240s wedge the
    # original cap fixed. `collect` has no observed hang, so it is single-shot.
    assert '_max_attempts = 2 if step == "dashboard" else 1' in script
    assert "for _attempt in range(_max_attempts)" in script
    assert "REGEN_STEP_TIMEOUT * 2" not in script, (
        "the retry must not double the per-step cap -- that regressed the worst "
        "case to ~270s, worse than the wedge the cap was added to fix"
    )
    assert "except subprocess.TimeoutExpired" in script
    # The generated daemon must still be valid Python.
    import ast
    ast.parse(script)


def test_daemon_regen_worst_case_is_bounded_well_under_240s(m):
    """retry cap, computed bound. Parse REGEN_STEP_TIMEOUT out of the
    GENERATED daemon script and assert the true worst case -- one collect shot
    plus two dashboard shots, each capped at REGEN_STEP_TIMEOUT (no doubling) --
    stays comfortably under the ~240s wedge the original per-step cap fixed. This
    guards the *number*, not just the code shape: a future edit that bumps the cap
    or re-adds a retry to `collect` trips here."""
    import re
    script = m._generate_daemon_script()
    match = re.search(r"REGEN_STEP_TIMEOUT\s*=\s*(\d+)", script)
    assert match, "REGEN_STEP_TIMEOUT literal missing from the generated daemon"
    step_timeout = int(match.group(1))
    # collect: 1 attempt; dashboard: 2 attempts; every attempt capped at the flat
    # per-step timeout (no *2 anywhere in the retry path).
    worst_case = step_timeout * (1 + 2)
    assert worst_case < 240, (
        f"regen worst case {worst_case}s must stay under the 240s wedge "
        f"(REGEN_STEP_TIMEOUT={step_timeout})"
    )
    # And it must be strictly better than the retired doubled-cap design (6x).
    assert worst_case < step_timeout * 6


# --------------------------------------------------------------------------
# 5. The stranded placeholder IS the empty-windows state, and a healthy meter
#    fills the windows -- so a completed forced refresh clears the placeholder.
# --------------------------------------------------------------------------

def _temp_trends(m, monkeypatch):
    """A trends DB with real consumed + saved so the context lever is non-trivial
    (mirrors test_runway_meter_freshness)."""
    tmp = Path(tempfile.mkdtemp(prefix="to-version-selfheal-db-"))
    dbp = tmp / "trends.db"
    conn = sqlite3.connect(str(dbp))
    conn.executescript(
        """
        CREATE TABLE session_log (id INTEGER PRIMARY KEY, date TEXT,
            input_tokens INTEGER, output_tokens INTEGER);
        CREATE TABLE savings_events (id INTEGER PRIMARY KEY, timestamp TEXT,
            event_type TEXT, tokens_saved INTEGER);
        CREATE TABLE compression_events (id INTEGER PRIMARY KEY, timestamp TEXT,
            original_tokens INTEGER, compressed_tokens INTEGER, tier TEXT);
        """
    )
    today = datetime.now().date().isoformat()
    now_iso = datetime.now().isoformat()
    conn.execute("INSERT INTO session_log(date,input_tokens,output_tokens) VALUES(?,?,?)",
                 (today, 1_000_000, 200_000))
    conn.execute("INSERT INTO savings_events(timestamp,event_type,tokens_saved) VALUES(?,?,?)",
                 (now_iso, "archive", 50_000))
    stamp_activity(conn)
    conn.commit()
    conn.close()
    monkeypatch.setattr(m, "_init_trends_db", lambda: sqlite3.connect(str(dbp)))
    monkeypatch.setattr(m, "_input_rate_mix_ratio", lambda days=30: 1.4)


def test_healthy_meter_fills_runway_windows(m, monkeypatch):
    """A FRESH, healthy live meter yields non-empty runway windows. The dashboard
    template renders the placeholder ONLY when the windows are empty (asserted
    below), so a refresh that re-reads a healthy meter fills the bars and the
    stranded placeholder cannot survive it."""
    _temp_trends(m, monkeypatch)
    monkeypatch.setattr(m, "_keepwarm_read_meters", lambda **k: {
        "available": True, "stale": False, "five_hour_pct": 12.0,
        "seven_day_pct": 10.0, "age_s": 3.0, "ts": time.time() - 3})
    r = m.runway_snapshot(days=30)
    assert r is not None
    assert {w["key"] for w in r["windows"]} == {"five_hour", "seven_day"}


def test_stale_meter_empties_windows_the_placeholder_state(m, monkeypatch):
    """The mirror image: a meter older than every window empties the windows list.
    That empty-windows state is exactly what the template renders as the
    placeholder -- so a dashboard generated while the meter was stale strands the
    placeholder until a refresh with a fresh meter (the fix) rebuilds it."""
    _temp_trends(m, monkeypatch)
    # age_s beyond the 7d span (168h) -> both windows are dropped as reset.
    monkeypatch.setattr(m, "_keepwarm_read_meters", lambda **k: {
        "available": True, "stale": True, "five_hour_pct": 12.0,
        "seven_day_pct": 10.0, "age_s": 200 * 3600, "ts": time.time() - 200 * 3600})
    r = m.runway_snapshot(days=30)
    # Card still exists (headline multiplier from the ledger) but has no windows,
    # which is the placeholder branch.
    assert r is not None
    assert r["windows"] == []


def test_placeholder_is_strictly_the_empty_windows_branch():
    """Source contract: the placeholder text appears exactly once in the shipped
    dashboard, as the else-branch of ``rwCards ? <bars> : <placeholder>``, and
    ``rwCards`` is built from ``rw.windows``. So whenever the injected runway
    carries windows, the bars render and the placeholder cannot show -- tying the
    functional tests above to what the user actually sees."""
    html = ASSET.read_text(encoding="utf-8")
    assert html.count(PLACEHOLDER) == 1
    idx = html.index(PLACEHOLDER)
    window = html[idx - 400:idx]
    assert "rwCards" in window and "?" in window, "placeholder must be the rwCards ternary else-branch"
    assert "rw.windows.map(" in html, "rwCards must derive from rw.windows"


# --------------------------------------------------------------------------
# 6. Thundering-herd guard: concurrent version-bump heals collapse to 1.
# --------------------------------------------------------------------------

def test_heal_spawn_due_dedups_within_window(m):
    """The first caller within the staleness window claims the marker and spawns;
    concurrent callers see the fresh marker and no-op. This is what collapses N
    simultaneous SessionStart rebuilds on a version bump down to ONE."""
    assert m._dashboard_heal_spawn_due() is True   # first claims + spawns
    assert m._dashboard_heal_spawn_due() is False  # a concurrent tick no-ops
    assert m._dashboard_heal_spawn_due() is False  # ...and so does every other
    marker = m.SNAPSHOT_DIR / m._DASHBOARD_HEAL_LOCK_NAME
    assert marker.exists(), "the inflight marker must persist to suppress the herd"


def test_heal_spawn_due_reclaims_stale_marker(m):
    """A marker older than the staleness window is reclaimed, so a genuinely later
    version bump can heal again and a crashed spawner never wedges the heal
    forever."""
    assert m._dashboard_heal_spawn_due() is True
    assert m._dashboard_heal_spawn_due() is False
    marker = m.SNAPSHOT_DIR / m._DASHBOARD_HEAL_LOCK_NAME
    old = time.time() - (m._DASHBOARD_HEAL_LOCK_STALE_SECONDS + 5)
    m.os.utime(str(marker), (old, old))
    assert m._dashboard_heal_spawn_due() is True   # stale -> reclaim + spawn
    assert m._dashboard_heal_spawn_due() is False  # fresh again -> suppress


def test_ensure_health_guards_spawn_with_herd_lock(m):
    """Every self-heal call, including ensure-health, uses the central spawn guard."""
    src = inspect.getsource(m._spawn_detached_dashboard_selfheal)
    guard_at = src.index("_dashboard_heal_spawn_due()")
    spawn_at = src.index("subprocess.Popen(")
    assert guard_at < spawn_at, "the herd guard must gate the spawn, not follow it"


def test_heal_spawn_due_fails_open(m, monkeypatch):
    """Fail-OPEN: if the marker filesystem op raises unexpectedly, spawn anyway.
    A missed dedup costs one extra rebuild; skipping the heal would reintroduce
    the stale-dashboard bug this whole branch exists to kill."""
    def _boom(*a, **k):
        raise OSError("disk gone")
    monkeypatch.setattr(m.os, "open", _boom)
    assert m._dashboard_heal_spawn_due() is True
