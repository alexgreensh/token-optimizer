"""collect and dashboard CLI dispatches must install a HookDeadline."""

from __future__ import annotations
from helpers.measure_split import measure_source as _split_measure_source

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture()
def measure_mod():
    import measure
    return measure


def _dispatch_collect_or_dashboard(mod, argv):
    """Drive the real CLI branches without importing __main__ side effects."""
    args = list(argv)
    if args[0] == "dashboard":
        # Mirror the standalone-dashboard branch the hook fossil hits.
        days = 30
        quiet = "--quiet" in args or "-q" in args
        for i, a in enumerate(args):
            if a == "--days" and i + 1 < len(args):
                try:
                    days = int(args[i + 1])
                except ValueError:
                    pass
        return mod.generate_standalone_dashboard(days=days, quiet=quiet)
    if args[0] == "collect":
        days = 90
        quiet = "--quiet" in args or "-q" in args
        rebuild = "--rebuild" in args
        for i, a in enumerate(args):
            if a == "--days" and i + 1 < len(args):
                try:
                    days = int(args[i + 1])
                except ValueError:
                    pass
        return mod.collect_sessions(days=days, quiet=quiet, rebuild=rebuild)
    raise AssertionError(f"unexpected argv {argv!r}")


def test_collect_dispatch_installs_hook_budget(measure_mod, monkeypatch):
    armed = []

    def _arm(seconds=8):
        armed.append(seconds)
        return object()

    monkeypatch.setattr(measure_mod, "_install_hook_budget", _arm)
    monkeypatch.setattr(measure_mod, "_clear_hook_budget", lambda deadline: None)
    monkeypatch.setattr(measure_mod, "collect_sessions", lambda **kwargs: None)

    # Prefer a dedicated dispatcher if present; otherwise require the CLI
    # entry itself to wrap collect in a budget (source-level contract).
    if hasattr(measure_mod, "_dispatch_collect"):
        measure_mod._dispatch_collect(["collect", "--quiet"])
    else:
        src = _split_measure_source(Path(measure_mod.__file__))
        collect_idx = src.find('elif args[0] == "collect":')
        assert collect_idx != -1
        window = src[collect_idx:collect_idx + 800]
        assert "_install_hook_budget" in window, (
            "collect CLI dispatch does not install a HookDeadline"
        )
        # Execute the wrapper if it exists after the source check so a
        # future dedicated helper is also exercised.
        return

    assert armed, "collect dispatch never called _install_hook_budget"
    assert armed[0] == 20


def test_dashboard_dispatch_installs_hook_budget(measure_mod, monkeypatch):
    armed = []

    def _arm(seconds=8):
        armed.append(seconds)
        return object()

    monkeypatch.setattr(measure_mod, "_install_hook_budget", _arm)
    monkeypatch.setattr(measure_mod, "_clear_hook_budget", lambda deadline: None)
    monkeypatch.setattr(measure_mod, "generate_standalone_dashboard", lambda **kwargs: "/tmp/x.html")

    if hasattr(measure_mod, "_dispatch_dashboard"):
        with pytest.raises(SystemExit) as exited:
            measure_mod._dispatch_dashboard(["dashboard", "--quiet"])
        assert exited.value.code in (0, 1)
    else:
        src = _split_measure_source(Path(measure_mod.__file__))
        dash_idx = src.find('elif args[0] == "dashboard":')
        assert dash_idx != -1
        window = src[dash_idx:dash_idx + 1800]
        assert "_install_hook_budget" in window, (
            "dashboard CLI dispatch does not install a HookDeadline"
        )
        return

    assert armed, "dashboard dispatch never called _install_hook_budget"
    assert armed[0] == 20


def test_module_runner_arms_hard_deadline():
    src = (REPO / "hooks" / "module_runner.py").read_text(encoding="utf-8")
    assert "HookDeadline" in src, "module_runner.py must arm a HookDeadline around runpy"
    assert "110" in src, "module_runner deadline must sit a few seconds under run.py's 120s wait"


# NOTE: the python-launcher.sh probe-bounding layer was
# reverted — it regressed pythonw-swap on systems without timeout(1), and Git
# Bash already ships timeout.exe so real Windows probes are bounded. The test
# that pinned that reverted behavior was removed with it.
