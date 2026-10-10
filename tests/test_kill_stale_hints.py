"""Printed kill-stale hints and the dashboard's copy button tell the truth.

Review F6: the dashboard's "Copy Kill Command" copied ``kill-stale --dry-run`` (a
preview that kills nothing) under a label that promised a kill, and every printed
hint said ``python3 measure.py ...`` with no path, which only works from inside
the scripts directory. Other notices already print the resolved script path.
"""

import importlib.util
import shlex
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MEASURE_PATH = REPO / "skills" / "token-optimizer" / "scripts" / "measure.py"
DASHBOARD = REPO / "skills" / "token-optimizer" / "assets" / "dashboard.html"


def _load_measure():
    scripts = str(MEASURE_PATH.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("measure_kill_stale_hints_under_test", MEASURE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_command_prefix_uses_the_resolved_script_path():
    measure = _load_measure()
    expected = f"python3 {shlex.quote(str(MEASURE_PATH.resolve()))}"
    assert measure._measure_cli() == expected
    assert measure._measure_cli("kill-stale", "--dry-run") == expected + " kill-stale --dry-run"


def test_a_script_path_with_spaces_stays_one_shell_word(monkeypatch):
    measure = _load_measure()
    monkeypatch.setattr(measure, "__file__", "/Users/a b/plugins/token optimizer/measure.py")
    cmd = measure._measure_cli("kill-stale")
    assert shlex.split(cmd)[:2] == ["python3", str(Path("/Users/a b/plugins/token optimizer/measure.py").resolve())]


def test_health_data_hands_the_dashboard_the_resolved_command(monkeypatch):
    measure = _load_measure()
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    data = measure._collect_health_data()
    assert data["cli"] == measure._measure_cli()


def test_no_kill_stale_hint_prints_the_bare_script_name():
    src = MEASURE_PATH.read_text(encoding="utf-8")
    bare = [ln.strip() for ln in src.splitlines() if "python3 measure.py kill-stale" in ln]
    assert not bare, bare


def test_orphan_hints_print_the_resolved_command(monkeypatch, capsys):
    measure = _load_measure()
    prefix = measure._measure_cli()
    recs = []
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(measure, "_collect_health_data", lambda: {"running_sessions": [
        {"pid": 1, "identity": "orphan_cli", "flags": ["ORPHAN"], "elapsed_seconds": 13 * 3600,
         "started": "x", "command": "claude", "elapsed_human": "13h"}]})
    monkeypatch.setattr(measure.os, "getpid", lambda: 9999)
    monkeypatch.setattr(measure.os, "getppid", lambda: 9998)
    monkeypatch.setattr(measure, "_posix_ancestor_pids", lambda pid: set())
    measure.kill_stale_sessions(threshold_hours=12)
    out = capsys.readouterr().out
    assert f"{prefix} kill-stale --include-orphans --hours 12" in out


def test_dashboard_labels_a_preview_as_a_preview():
    html = DASHBOARD.read_text(encoding="utf-8")
    bulk = html[html.index("var killStaleBtn"):html.index("kill-pid-btn').forEach")]
    # the CLI-routed branch copies a dry-run, and says so
    assert "--dry-run" in bulk
    assert "Copy Preview Command" in html
    # the label that promises a kill is only the plain `kill <pid>` branch
    assert html.count("Copy Kill Command") >= 1
    start = html.index('id="kill-stale-btn"')
    block = html[html.rindex("staleCount > 0", 0, start):start + 300]
    assert "taggedInventory ? 'Copy Preview Command' : 'Copy Kill Command'" in block
    assert "' + killLabel + '" in block


def test_dashboard_resets_the_button_to_its_own_label():
    html = DASHBOARD.read_text(encoding="utf-8")
    bulk = html[html.index("var killStaleBtn"):html.index("kill-pid-btn').forEach")]
    # after "Copied!" the button must go back to the label it had, not a hard-coded kill label
    assert "killStaleBtn.textContent = 'Copy Kill Command'" not in bulk


def test_dashboard_commands_use_the_resolved_cli_from_health_data():
    html = DASHBOARD.read_text(encoding="utf-8")
    assert "health.cli" in html and "h.cli" in html
    assert "python3 measure.py kill-stale" not in html


def test_hints_with_markup_escape_the_resolved_path():
    """A hint that carries markup is assigned to innerHTML, so the resolved script
    path (which may hold &, < or quotes) goes through esc() there."""
    import re
    src = DASHBOARD.read_text(encoding="utf-8")
    assert "function tcliHtml(" in src
    assert ".join(esc(String(cli)))" in src
    for line in src.splitlines():
        if "tcli('" in line and "esc(" not in line:
            assert not re.search(r"<span|<div|<code|&middot;", line), line.strip()[:120]
