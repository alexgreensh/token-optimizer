"""U14 — Antigravity runtime isolation and identity in measure.py.

The ``antigravity`` runtime must be isolated from ``~/.claude`` everywhere the
engine scans, and must own a distinct daemon identity (port 24847, label
``com.token-optimizer.antigravity-dashboard``, its own Windows task name) so the
dashboard its Stop hook spawns is real. These tests drive ``measure`` with
``TOKEN_OPTIMIZER_RUNTIME=antigravity`` and ``CLAUDE_DIR`` pointed at a raising
sentinel, so any accidental ``~/.claude`` read fails loudly.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


def _scripts_modules():
    return [
        name for name, mod in list(sys.modules.items())
        if getattr(mod, "__file__", None)
        and str(SCRIPTS) in str(Path(mod.__file__).resolve().parent)
    ]


def _purge_scripts_modules():
    for name in _scripts_modules():
        del sys.modules[name]


@pytest.fixture(autouse=True)
def _cleanup_modules():
    snapshot = {name: sys.modules[name] for name in _scripts_modules()}
    yield
    _purge_scripts_modules()
    sys.modules.update(snapshot)


class _RaisingPath:
    """Sentinel that raises on ANY attribute access, proving ~/.claude is untouched."""

    def __getattr__(self, name):
        raise AssertionError(f"CLAUDE_DIR.{name} was accessed under antigravity")


def _import_measure(monkeypatch, tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(snap))
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "antigravity")
    monkeypatch.setenv("TOKEN_OPTIMIZER_NO_PROC_SCAN", "1")
    monkeypatch.setenv("TOKEN_OPTIMIZER_ANTIGRAVITY_HOME", str(tmp_path / "gemini"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    _purge_scripts_modules()
    mod = importlib.import_module("measure")
    mod.CLAUDE_DIR = _RaisingPath()
    return mod, snap


# --------------------------------------------------------------------------- #
# foreign runtime + exemption identity
# --------------------------------------------------------------------------- #

def test_antigravity_is_foreign_and_exempts_dashboard(monkeypatch, tmp_path):
    mod, _ = _import_measure(monkeypatch, tmp_path)
    assert "antigravity" in mod._FOREIGN_RUNTIMES
    assert mod._FOREIGN_RUNTIME_EXEMPTIONS["antigravity"] == frozenset({"dashboard"})
    assert mod._is_foreign_runtime() is True


def test_daemon_identity_distinct(monkeypatch, tmp_path):
    mod, _ = _import_measure(monkeypatch, tmp_path)
    assert mod.DAEMON_PORT == 24847
    assert mod.DAEMON_LABEL == "com.token-optimizer.antigravity-dashboard"
    assert mod.WINDOWS_TASK_NAME == "TokenOptimizerAntigravityDashboard"
    assert mod.WINDOWS_TASK_NAME not in ("TokenOptimizerDashboard",)
    # The sweep set now carries the antigravity variant too.
    assert "TokenOptimizerAntigravityDashboard" in mod._ALL_WINDOWS_TASK_NAMES
    assert "antigravity" in mod._DAEMON_ALL_SUFFIXES


# --------------------------------------------------------------------------- #
# no ~/.claude scan on the dashboard's component / JSONL paths
# --------------------------------------------------------------------------- #

def test_measure_components_reads_no_claude_dir(monkeypatch, tmp_path):
    mod, _ = _import_measure(monkeypatch, tmp_path)
    components = mod._measure_antigravity_components()
    assert "antigravity_plugin" in components
    assert components["core_system"]["tokens"] == 0
    # measure_components() routes to the same antigravity path.
    routed = mod.measure_components()
    assert "antigravity_plugin" in routed
    assert "claude_md_global" not in routed


def test_jsonl_guards_return_empty_without_claude(monkeypatch, tmp_path):
    mod, _ = _import_measure(monkeypatch, tmp_path)
    # Every JSONL scan path must short-circuit before touching CLAUDE_DIR.
    assert mod.get_session_baselines() == []
    assert mod._find_all_jsonl_files() == []
    assert mod._find_current_session_jsonl() is None
    assert mod._find_session_jsonl_by_id("abc-123") is None


# --------------------------------------------------------------------------- #
# savings transformation: list-price estimate, no token-savings headline
# --------------------------------------------------------------------------- #

def test_savings_transformation_reports_estimated_billing(monkeypatch, tmp_path):
    mod, snap = _import_measure(monkeypatch, tmp_path)
    res = mod._estimate_before_after_savings(days=30)
    assert res["reason"] == "estimated_billing"
    assert res["monthly_savings_usd"] == 0.0
    assert res["counterfactual_monthly_usd"] == 0.0
    assert res["actual_monthly_usd"] == 0.0
    assert res["breakdown"] == []


# --------------------------------------------------------------------------- #
# Claude-target commands print the notice, never scan ~/.claude
# --------------------------------------------------------------------------- #

def test_report_prints_antigravity_notice_and_exits_zero(tmp_path):
    env = os.environ.copy()
    gemini = tmp_path / "gemini"
    gemini.mkdir(parents=True, exist_ok=True)
    env.update({
        "TOKEN_OPTIMIZER_RUNTIME": "antigravity",
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(tmp_path / "snap"),
        "TOKEN_OPTIMIZER_NO_PROC_SCAN": "1",
        "TOKEN_OPTIMIZER_ANTIGRAVITY_HOME": str(gemini),
    })
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "measure.py"), "report"],
        capture_output=True, text=True, env=env, timeout=180,
    )
    assert out.returncode == 0
    assert "Google Antigravity runtime detected" in out.stdout
    # No Claude audit output leaked through.
    assert "Token Optimizer Audit" not in out.stdout
