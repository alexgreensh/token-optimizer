"""Printed and dashboard hints must paste from any directory.

A bare ``python3 measure.py <cmd>`` only works from inside the scripts
directory, which is where nobody is. Every hint a user is told to run goes
through the resolved, shell-quoted script path: ``_hint()`` / ``_measure_cli()``
in measure.py, ``with_measure_cli()`` in runtime_env.py (runtime doctors and
installers), ``tcli()`` in the dashboard. This test greps for the bare form.
"""

from __future__ import annotations

import ast
import re
import shlex
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
DASHBOARD = REPO / "skills" / "token-optimizer" / "assets" / "dashboard.html"
BARE = "python3 measure.py"
WRAPPERS = {"_hint", "with_measure_cli", "_with_measure_cli"}
HINT_MODULES = [
    "measure.py", "antigravity_doctor.py", "antigravity_install.py", "copilot_doctor.py",
    "copilot_install.py", "cursor_doctor.py", "cursor_install.py", "grok_doctor.py",
    "grok_install.py",
]

sys.path.insert(0, str(SCRIPTS))


def _bare_hint_sites(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    doc_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                doc_nodes.add(body[0].value)
    # measure.py's own `Usage:` listing documents the command grammar; it is the
    # last block of main() and not a fix-it hint.
    usage_from = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print"
                and node.args and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "Usage:"):
            usage_from = node.lineno
    bad = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str) and BARE in node.value):
            continue
        if node in doc_nodes:
            continue
        if usage_from and node.lineno >= usage_from:
            continue
        cur, wrapped = node, False
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, ast.Call) and getattr(cur.func, "id", "") in WRAPPERS:
                wrapped = True
                break
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)) and cur.name in WRAPPERS:
                wrapped = True  # the wrapper's own replace("python3 measure.py", ...)
                break
        if not wrapped:
            bad.append(f"{path.name}:{node.lineno}: {node.value.strip()[:80]!r}")
    return bad


def test_no_bare_python3_measure_py_hint_in_python_sources():
    bad = []
    for name in HINT_MODULES:
        bad.extend(_bare_hint_sites(SCRIPTS / name))
    bad.extend(_bare_hint_sites(REPO / "skills" / "fleet-auditor" / "scripts" / "fleet.py"))
    assert not bad, "bare `python3 measure.py` hints (wrap in _hint()/with_measure_cli()):\n" + "\n".join(bad)


def test_dashboard_hints_go_through_tcli_or_health_cli():
    html = DASHBOARD.read_text(encoding="utf-8")
    assert "function tcli(" in html
    bad = []
    for n, line in enumerate(html.splitlines(), 1):
        if BARE not in line or "tcli(" in line or "h.cli ||" in line or "health.cli) ||" in line:
            continue
        if line.lstrip().startswith(("//", "*")) or "split('python3 measure.py')" in line:
            continue
        bad.append(f"dashboard.html:{n}: {line.strip()[:100]}")
    assert not bad, "\n".join(bad)


def test_hint_uses_the_resolved_quoted_path_even_with_spaces(tmp_path, monkeypatch):
    import runtime_env

    spaced = tmp_path / "install with space" / "runtime_env.py"
    spaced.parent.mkdir()
    monkeypatch.setattr(runtime_env, "__file__", str(spaced))
    out = runtime_env.with_measure_cli("fix: python3 measure.py setup-hook now")
    expected_script = str((spaced.parent / "measure.py").resolve())
    assert out == f"fix: python3 {shlex.quote(expected_script)} setup-hook now"
    assert BARE not in out


def test_measure_hint_matches_measure_cli(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    sys.modules.pop("measure", None)
    import measure

    try:
        assert measure._hint("run python3 measure.py doctor") == "run " + measure._measure_cli("doctor")
        assert re.search(r"python3 \S*measure\.py doctor$", measure._hint("python3 measure.py doctor"))
    finally:
        sys.modules.pop("measure", None)
