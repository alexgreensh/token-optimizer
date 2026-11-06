"""session-end-flush must defer by DEFAULT.

A legacy bare `measure.py session-end-flush` hook (pre-5.11.77 script installs,
no --defer flag) fossilizes in ~/.claude/settings.json, and no self-heal path
rewrites it. Before this fix the dispatch deferred only when `--defer` was in
argv, so the fossil ran the heavy flush synchronously inline on updated 5.11.81
scripts and wedged Windows stop-hooks at 3/4.

The fix inverts the gate: defer unless `--no-defer` is passed. These tests pin
that decision at `_dispatch_session_end_flush` without spawning any worker.
"""

from __future__ import annotations
from helpers.measure_split import measure_source as _split_measure_source

import importlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture()
def m(monkeypatch):
    import measure as mod
    calls = {"defer": [], "inline": []}
    monkeypatch.setattr(mod, "_defer_session_end_flush", lambda a: calls["defer"].append(list(a)))
    monkeypatch.setattr(mod, "_run_session_end_flush_worker", lambda a: calls["inline"].append(list(a)))
    return mod, calls


def test_bare_fossil_command_defers(m):
    """THE fix: `session-end-flush` with no flags (the fossil) must DEFER."""
    mod, calls = m
    assert mod._dispatch_session_end_flush(["session-end-flush"]) == "deferred"
    assert calls["defer"] == [["session-end-flush"]]
    assert calls["inline"] == []


def test_no_defer_runs_inline(m):
    mod, calls = m
    assert mod._dispatch_session_end_flush(["session-end-flush", "--trigger", "manual", "--no-defer"]) == "inline"
    assert calls["inline"] and not calls["defer"]


def test_hook_command_with_defer_still_defers(m):
    """The shipped plugin hook (`--trigger stop --quiet --defer`) is unaffected."""
    mod, calls = m
    assert mod._dispatch_session_end_flush(["session-end-flush", "--trigger", "stop", "--quiet", "--defer"]) == "deferred"
    assert calls["defer"] and not calls["inline"]


def test_manual_refresh_cmd_carries_no_defer():
    """The codex manual refresh_cmd must stay synchronous (carry --no-defer) so a
    user running it and immediately opening the dashboard sees fresh data."""
    src = _split_measure_source(SCRIPTS / "measure.py")
    assert "session-end-flush --trigger manual --no-defer" in src


def test_both_trees_have_the_dispatch():
    top = _split_measure_source(SCRIPTS / "measure.py")
    mirror = _split_measure_source(REPO / "plugins" / "token-optimizer" / "skills" / "token-optimizer"
              / "scripts" / "measure.py")
    assert "_dispatch_session_end_flush" in top
    assert top == mirror, "measure.py trees must be byte-identical"
