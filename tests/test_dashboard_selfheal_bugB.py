"""Bug B: the dashboard must self-heal and must not be killed on a user open.

Two mechanisms guard the "it didn't regenerate, it did not self-heal" report:

  * an explicit user open (the token-dashboard skill, the daemon's Regenerate
    button) sets TOKEN_OPTIMIZER_INTERACTIVE=1 so the 20s hook budget does NOT
    arm and a heavy rebuild runs to completion (unbounded);
  * when a bounded hook-path regen IS killed by the budget, the dispatch spawns
    a DETACHED, unbounded child to finish the rebuild off the hot path, so the
    on-disk dashboard catches up instead of staying stale forever.

Run: python3 -m pytest tests/test_dashboard_selfheal_bugB.py -v
"""
import importlib
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def m(tmp_path, monkeypatch):
    # The self-heal throttle claims a marker file in the data dir; without a
    # private one the test passes once per throttle window, then fails.
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


def test_interactive_marker_defeats_hook_detection(m, monkeypatch):
    """The positive marker wins over the non-tty heuristic: a user open piped by
    a skill/daemon must NOT be treated as a hook (else the 20s budget kills it)."""
    monkeypatch.delenv("TOKEN_OPTIMIZER_HOOK", raising=False)
    monkeypatch.setattr(sys, "argv", ["measure.py", "dashboard"])
    for val in ("1", "true", "YES", "on"):
        monkeypatch.setenv("TOKEN_OPTIMIZER_INTERACTIVE", val)
        assert m._running_under_hook() is False, f"marker {val!r} must force interactive"


def test_hook_still_detected_without_marker(m, monkeypatch):
    """Without the marker, the explicit hook signals still bound the run (the
    freeze guard must survive)."""
    monkeypatch.delenv("TOKEN_OPTIMIZER_INTERACTIVE", raising=False)
    monkeypatch.setenv("TOKEN_OPTIMIZER_HOOK", "1")
    monkeypatch.setattr(sys, "argv", ["measure.py", "dashboard"])
    assert m._running_under_hook() is True


def test_selfheal_spawns_detached_unbounded_child(m, monkeypatch):
    """On a budget timeout the dispatch hands the rebuild to a detached child
    that is marked INTERACTIVE (so the child itself is unbounded -> no re-timeout,
    no spawn loop) and started in its own session/detached, never raising."""
    captured = {}

    class _FakePopen:
        def __init__(self, argv, **kw):
            captured["argv"] = argv
            captured["kw"] = kw

    monkeypatch.setattr(m.subprocess, "Popen", _FakePopen)
    m._spawn_detached_dashboard_selfheal(days=30)

    argv = captured["argv"]
    assert argv[1:] == [m.os.path.abspath(m.__file__), "dashboard", "--quiet", "--days", "30"]
    env = captured["kw"]["env"]
    assert env.get("TOKEN_OPTIMIZER_INTERACTIVE") == "1", "child must be unbounded"
    assert "TOKEN_OPTIMIZER_HOOK" not in env, "child must not inherit the hook marker"
    # detached: POSIX new session or Windows detached flags
    assert captured["kw"].get("start_new_session") is True or "creationflags" in captured["kw"]
    assert captured["kw"]["stdin"] == m.subprocess.DEVNULL


def test_selfheal_never_raises(m, monkeypatch):
    """A failed self-heal spawn must be swallowed -- it can never break the hook."""
    def _boom(*a, **k):
        raise OSError("no fork for you")
    monkeypatch.setattr(m.subprocess, "Popen", _boom)
    m._spawn_detached_dashboard_selfheal()  # must not raise


def test_sessionend_flush_regen_wires_selfheal_on_timeout(m):
    """Gap 4: the SessionEnd flush-worker regen is bounded by the 20s budget, so a
    _HookTimeout there must also trigger the detached self-heal -- otherwise a big
    history's SessionEnd rebuild truncates and the daemon can serve the truncated
    file within its freshness window. Guard statically that the flush path both
    calls generate_standalone_dashboard AND spawns the self-heal on _HookTimeout,
    so the wiring can't silently regress (the same self-heal used by the open path
    is unit-tested above)."""
    import inspect
    src = inspect.getsource(m)
    # locate the flush-worker's standalone regen call and its enclosing try/except
    idx = src.find("generate_standalone_dashboard(days=30, quiet=True)")
    assert idx != -1, "flush-worker standalone regen call not found"
    window = src[idx: idx + 1000]
    assert "except _HookTimeout" in window, "flush regen must catch _HookTimeout"
    assert "_spawn_detached_dashboard_selfheal" in window, \
        "flush regen must spawn the detached self-heal on timeout (Gap 4)"
