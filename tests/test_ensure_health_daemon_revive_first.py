"""The dashboard-daemon ensure/revive must NOT do any work on
the SessionStart critical path -- zero chance of slowing a user's session start.

Before: ``run_ensure_health`` ran the daemon self-heal inline, so a missing plist
was only reinstalled if the health scan finished under the 8s budget. A first
pass moved the revive first but still ran it inline under a 6s guard -- still work
on the session-start path. This decoupled version dispatches the revive as a
DETACHED, fire-and-forget ``daemon-revive`` child (start_new_session / detach
flags via ``spawn_detached``, stdio -> DEVNULL) and returns immediately. The child
reinstalls a missing plist / revives a dead daemon fully off the hot path; launchd
persists a once-installed daemon and the mid-session pulse is a second recovery
path.

These tests prove:
  * ``_ensure_health_daemon_revive_first`` dispatches a DETACHED ``daemon-revive``
    child and returns, WITHOUT running ``_ensure_dashboard_daemon`` inline and
    WITHOUT arming any wall-clock budget (no session-start latency).
  * It is fail-open: a spawn that raises, or returns None (spawn failed), never
    raises into the hook.
  * The ensure-health dispatch still calls the revive dispatcher BEFORE the 8s
    budget / ``run_ensure_health`` (reorder guard, so a future edit can't put the
    daemon recovery behind the health budget again).

Run: python3 -m pytest tests/test_ensure_health_daemon_revive_first.py -v
"""

from __future__ import annotations
from helpers.measure_split import measure_source as _split_measure_source

import importlib
import re
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture()
def m(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="to-daemon-revive-first-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    # This file replaces every daemon spawn before exercising orchestration.
    monkeypatch.setattr(mod, "_daemon_snapshot_sandboxed", lambda: False)
    monkeypatch.setattr(mod, "_is_foreign_runtime", lambda: False)
    monkeypatch.setattr(mod, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(mod, "_normalized_platform", lambda: "Darwin")
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


def test_revive_dispatches_detached_daemon_revive_child(m, monkeypatch):
    """The revive is a DETACHED ``daemon-revive`` child, not inline work."""
    calls = {}
    monkeypatch.setattr(m, "spawn_detached", lambda cmd, **kw: calls.setdefault("cmd", cmd) or object())
    # If the daemon self-heal were run inline, this would fire -- it must NOT.
    monkeypatch.setattr(
        m, "_ensure_dashboard_daemon",
        lambda *a, **k: calls.setdefault("inline", True),
    )
    m._ensure_health_daemon_revive_first()
    assert "cmd" in calls, "revive did not spawn a detached child"
    assert calls["cmd"][-1] == "daemon-revive", f"child is not daemon-revive: {calls['cmd']}"
    assert "inline" not in calls, "daemon self-heal ran INLINE on the session-start path"


def test_revive_adds_no_wall_clock_budget(m, monkeypatch):
    """No ``_install_hook_budget`` on the session-start revive path (zero latency)."""
    monkeypatch.setattr(m, "spawn_detached", lambda cmd, **kw: object())
    budgets = []
    monkeypatch.setattr(m, "_install_hook_budget", lambda s: budgets.append(s) or object())
    m._ensure_health_daemon_revive_first()
    assert budgets == [], f"revive armed a wall-clock budget on the session path: {budgets}"


def test_revive_fail_open_on_spawn_exception(m, monkeypatch):
    """A spawn that raises must be swallowed (never break SessionStart)."""

    def boom(*a, **k):
        raise RuntimeError("spawn explode")

    monkeypatch.setattr(m, "spawn_detached", boom)
    m._ensure_health_daemon_revive_first()  # must not raise


def test_revive_fail_open_on_spawn_none(m, monkeypatch):
    """spawn_detached returning None (spawn failed) is logged, never raised."""
    logged = []
    monkeypatch.setattr(m, "spawn_detached", lambda cmd, **kw: None)
    monkeypatch.setattr(m, "_log_spawn_failure", lambda msg: logged.append(msg))
    m._ensure_health_daemon_revive_first()  # must not raise
    assert logged, "a failed spawn (None) was not logged"


def test_dispatch_source_calls_revive_first_before_budget():
    """Reorder guard: in the ensure-health dispatch block, the call to
    ``_ensure_health_daemon_revive_first()`` must appear BEFORE
    ``_install_hook_budget(8)`` and ``run_ensure_health()`` so a future edit
    cannot put the (now detached) daemon recovery behind the health budget."""
    src = _split_measure_source(SCRIPTS / "measure.py")
    mm = re.search(
        r'elif args\[0\] == "ensure-health":(?P<body>.*?)(?=\n    elif args\[0\] ==)',
        src,
        re.S,
    )
    assert mm, "could not locate the ensure-health dispatch block"
    body = mm.group("body")
    i_revive = body.find("_ensure_health_daemon_revive_first()")
    i_budget = body.find("_install_hook_budget(8)")
    i_health = body.find("run_ensure_health()")
    assert i_revive != -1, "dispatch no longer calls _ensure_health_daemon_revive_first()"
    assert i_budget != -1, "dispatch no longer arms the 8s budget"
    assert i_health != -1, "dispatch no longer calls run_ensure_health"
    assert i_revive < i_budget < i_health, (
        "ordering broken: the detached revive dispatch must precede the 8s budget "
        "and run_ensure_health"
    )
