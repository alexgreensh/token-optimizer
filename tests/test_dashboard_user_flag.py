"""`measure.py dashboard --user`: an argv flag marking a user-initiated run.

The desktop band's "Full dashboard" link runs `measure.py dashboard` through a
runner that cannot pass env and has a non-tty stdin, so `_running_under_hook()`
reads it as a hook and arms the 20s hook budget. A person clicking a link is
not a hook: `--user` declares an interactive run (same effect as
TOKEN_OPTIMIZER_INTERACTIVE=1, but on argv).

Run: python3 -m pytest tests/test_dashboard_user_flag.py -v
"""
import importlib
import io
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def m(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("TOKEN_OPTIMIZER_INTERACTIVE", raising=False)
    monkeypatch.delenv("TOKEN_OPTIMIZER_HOOK", raising=False)
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    # A runner-style stdin: piped, not a tty.
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    yield mod
    sys.modules.pop("measure", None)


def _stub_dispatch(m, monkeypatch):
    calls = {"budget": 0, "opened": 0, "generated": 0}

    def fake_budget(_seconds):
        calls["budget"] += 1
        return None

    def fake_generate(**_kw):
        calls["generated"] += 1
        return "/x/dashboard.html"

    monkeypatch.setattr(m, "_install_hook_budget", fake_budget)
    monkeypatch.setattr(m, "_clear_hook_budget", lambda _d: None)
    monkeypatch.setattr(m, "generate_standalone_dashboard", fake_generate)
    monkeypatch.setattr(m, "_open_dashboard", lambda **_kw: calls.__setitem__("opened", calls["opened"] + 1))
    return calls


def _run(m, args):
    with pytest.raises(SystemExit) as exc:
        m._dispatch_dashboard(args)
    return exc.value.code


def test_piped_stdin_without_flag_is_a_hook(m, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["measure.py", "dashboard"])
    assert m._running_under_hook() is True


def test_user_flag_means_not_a_hook(m, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["measure.py", "dashboard", "--user"])
    assert m._running_under_hook() is False


def test_user_flag_bypasses_budget_and_still_opens_browser(m, monkeypatch):
    calls = _stub_dispatch(m, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["measure.py", "dashboard", "--user"])
    assert _run(m, ["dashboard", "--user"]) == 0
    assert calls["budget"] == 0, "user-initiated run must not arm the hook budget"
    assert calls["generated"] == 1
    assert calls["opened"] == 1, "the browser must still open"


def test_hook_context_without_flag_still_honours_budget(m, monkeypatch):
    calls = _stub_dispatch(m, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["measure.py", "dashboard"])
    assert _run(m, ["dashboard"]) == 0
    assert calls["budget"] == 1, "a hook-context run keeps the 20s budget"


def test_explicit_hook_flag_still_bounded_without_user(m, monkeypatch):
    calls = _stub_dispatch(m, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["measure.py", "dashboard", "--hook"])
    _run(m, ["dashboard", "--hook"])
    assert calls["budget"] == 1


def test_unknown_flag_handling_unchanged(m, monkeypatch):
    """An unknown flag is ignored exactly as before, with or without --user."""
    calls = _stub_dispatch(m, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["measure.py", "dashboard", "--bogus"])
    assert _run(m, ["dashboard", "--bogus"]) == 0
    assert calls["budget"] == 1
    assert calls["opened"] == 1


def test_user_flag_is_inert_when_budget_unrelated(m, monkeypatch):
    """--quiet with --user still regenerates without opening a browser."""
    calls = _stub_dispatch(m, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["measure.py", "dashboard", "--user", "--quiet"])
    assert _run(m, ["dashboard", "--user", "--quiet"]) == 0
    assert calls["budget"] == 0
    assert calls["opened"] == 0
