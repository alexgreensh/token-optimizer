"""The daemon must regenerate the dashboard it actually serves.

The generated server can be launched by a different runtime than the one whose
plugin-data directory it serves.  A baked .codex fallback in that case produces
an apparently successful regeneration with no visible change in the .claude
dashboard.
"""

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _replace_constant(source, name, value):
    pattern = rf"^{name} = .*$"
    literal = value if isinstance(value, (int, float)) else str(value)
    # Lambda replacement: a string template would re-process backslash escapes
    # in the repr'd path, collapsing C:\\Users to C:\Users and producing an
    # invalid '\U...' unicode escape on Windows.
    updated, count = re.subn(pattern, lambda m: f"{name} = {literal!r}", source, count=1, flags=re.MULTILINE)
    assert count == 1, f"missing generated daemon constant: {name}"
    return updated


def test_regenerate_advances_served_dashboard_mtime_and_uses_served_install(tmp_path):
    scripts = Path(__file__).resolve().parents[1] / "skills" / "token-optimizer" / "scripts"
    sys.path.insert(0, str(scripts))
    import measure

    claude_marketplace = tmp_path / ".claude" / "plugins" / "marketplaces" / "alexgreensh-token-optimizer"
    claude_measure = claude_marketplace / "skills" / "token-optimizer" / "scripts" / "measure.py"
    claude_measure.parent.mkdir(parents=True)
    claude_measure.write_text(
        "import pathlib, sys\n"
        "if sys.argv[1] == 'dashboard':\n"
        "    pathlib.Path(" + repr(str(tmp_path / ".claude" / "plugins" / "data" / "token-optimizer-alexgreensh-token-optimizer" / "data" / "dashboard.html")) + ").write_text('claude-refresh')\n",
        encoding="utf-8",
    )
    codex_measure = tmp_path / ".codex" / "plugins" / "cache" / "alexgreensh-token-optimizer" / "token-optimizer" / "5.13.1" / "skills" / "token-optimizer" / "scripts" / "measure.py"
    codex_measure.parent.mkdir(parents=True)
    codex_measure.write_text("raise SystemExit('wrong install')\n", encoding="utf-8")

    served = tmp_path / ".claude" / "plugins" / "data" / "token-optimizer-alexgreensh-token-optimizer" / "data"
    served.mkdir(parents=True)
    dashboard = served / "dashboard.html"
    dashboard.write_text("old", encoding="utf-8")
    token = served / "daemon-token"
    token.write_text("test-token\n", encoding="utf-8")
    host = served / "dashboard-host"
    host.write_text("127.0.0.1\n", encoding="utf-8")

    source = measure._generate_daemon_script()
    source = _replace_constant(source, "DASHBOARD", dashboard)
    source = _replace_constant(source, "TOKEN_PATH", token)
    source = _replace_constant(source, "HOST_PATH", host)
    source = _replace_constant(source, "THRASH_PATH", served / ".daemon-thrash")
    source = _replace_constant(source, "LOG_DIR", served / "logs")
    source = _replace_constant(source, "REGEN_LOG", served / "daemon-regen.log")
    source = _replace_constant(source, "PORT", 24991)
    source = _replace_constant(source, "MEASURE_PY_FALLBACK", codex_measure)
    server = tmp_path / "dashboard-server.py"
    server.write_text(source, encoding="utf-8")

    before = dashboard.stat().st_mtime_ns
    proc = subprocess.Popen([sys.executable, str(server)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.time() + 5
        response = None
        while time.time() < deadline:
            try:
                response = urllib.request.urlopen(
                    urllib.request.Request("http://127.0.0.1:24991/api/regenerate", method="POST",
                                            headers={"Host": "localhost:24991", "Origin": "http://localhost:24991",
                                                     "X-TO-Token": "test-token", "Content-Length": "0"}),
                    timeout=1,
                )
                break
            except urllib.error.HTTPError as error:
                response = error
                break
            except (ConnectionRefusedError, urllib.error.URLError):
                time.sleep(0.05)
        else:
            detail = proc.stderr.read().decode("utf-8", "replace") if proc.poll() is not None else "still running"
            raise AssertionError("daemon did not start: " + detail)
        payload = json.loads(response.read().decode("utf-8"))
        assert payload["ok"] is True
        assert payload["measure_py"] == str(claude_measure)
        assert dashboard.stat().st_mtime_ns > before
        assert dashboard.read_text(encoding="utf-8") == "claude-refresh"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_regenerate_resolves_served_install_despite_mismatched_marketplace_identity(tmp_path):
    """5.13.3 regression: a .codex-generated daemon serving a .claude dashboard bakes
    the GENERATING runtime's marketplace identity, which names no dir under the SERVED
    runtime. The resolver must still discover the served runtime's own token-optimizer
    marketplace by scanning its marketplaces root, instead of silently falling back to
    the stale cross-runtime cache (the bug that made Regenerate no-op after upgrade)."""
    scripts = Path(__file__).resolve().parents[1] / "skills" / "token-optimizer" / "scripts"
    sys.path.insert(0, str(scripts))
    import measure

    claude_marketplace = tmp_path / ".claude" / "plugins" / "marketplaces" / "alexgreensh-token-optimizer"
    claude_measure = claude_marketplace / "skills" / "token-optimizer" / "scripts" / "measure.py"
    claude_measure.parent.mkdir(parents=True)
    claude_measure.write_text(
        "import pathlib, sys\n"
        "if sys.argv[1] == 'dashboard':\n"
        "    pathlib.Path(" + repr(str(tmp_path / ".claude" / "plugins" / "data" / "token-optimizer-alexgreensh-token-optimizer" / "data" / "dashboard.html")) + ").write_text('claude-refresh')\n",
        encoding="utf-8",
    )
    codex_measure = tmp_path / ".codex" / "plugins" / "cache" / "alexgreensh-token-optimizer" / "token-optimizer" / "5.13.1" / "skills" / "token-optimizer" / "scripts" / "measure.py"
    codex_measure.parent.mkdir(parents=True)
    codex_measure.write_text("raise SystemExit('wrong install')\n", encoding="utf-8")

    served = tmp_path / ".claude" / "plugins" / "data" / "token-optimizer-alexgreensh-token-optimizer" / "data"
    served.mkdir(parents=True)
    dashboard = served / "dashboard.html"
    dashboard.write_text("old", encoding="utf-8")
    token = served / "daemon-token"
    token.write_text("test-token\n", encoding="utf-8")
    host = served / "dashboard-host"
    host.write_text("127.0.0.1\n", encoding="utf-8")

    source = measure._generate_daemon_script()
    source = _replace_constant(source, "DASHBOARD", dashboard)
    source = _replace_constant(source, "TOKEN_PATH", token)
    source = _replace_constant(source, "HOST_PATH", host)
    source = _replace_constant(source, "THRASH_PATH", served / ".daemon-thrash")
    source = _replace_constant(source, "LOG_DIR", served / "logs")
    source = _replace_constant(source, "REGEN_LOG", served / "daemon-regen.log")
    source = _replace_constant(source, "PORT", 24992)
    source = _replace_constant(source, "MEASURE_PY_FALLBACK", codex_measure)
    # The baked identity is the GENERATING (.codex) runtime's and names no dir here.
    source = _replace_constant(source, "MEASURE_PY_MARKETPLACE", "codex-generating-identity-absent-under-served")
    server = tmp_path / "dashboard-server.py"
    server.write_text(source, encoding="utf-8")

    before = dashboard.stat().st_mtime_ns
    proc = subprocess.Popen([sys.executable, str(server)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.time() + 5
        response = None
        while time.time() < deadline:
            try:
                response = urllib.request.urlopen(
                    urllib.request.Request("http://127.0.0.1:24992/api/regenerate", method="POST",
                                            headers={"Host": "localhost:24992", "Origin": "http://localhost:24992",
                                                     "X-TO-Token": "test-token", "Content-Length": "0"}),
                    timeout=1,
                )
                break
            except urllib.error.HTTPError as error:
                response = error
                break
            except (ConnectionRefusedError, urllib.error.URLError):
                time.sleep(0.05)
        else:
            detail = proc.stderr.read().decode("utf-8", "replace") if proc.poll() is not None else "still running"
            raise AssertionError("daemon did not start: " + detail)
        payload = json.loads(response.read().decode("utf-8"))
        assert payload["ok"] is True
        assert payload["measure_py"] == str(claude_measure)
        assert dashboard.stat().st_mtime_ns > before
        assert dashboard.read_text(encoding="utf-8") == "claude-refresh"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_sidechain_pool_ignores_pre_classifier_fix_sidecar(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "skills" / "token-optimizer" / "scripts"
    sys.path.insert(0, str(scripts))
    import measure

    monkeypatch.setattr(measure, "SNAPSHOT_DIR", tmp_path / "snapshot")
    monkeypatch.setattr(measure, "TRENDS_DB", tmp_path / "missing-trends.db")
    monkeypatch.setattr(measure, "CLAUDE_DIR", tmp_path / "missing-claude")
    monkeypatch.setattr(measure, "_subagent_pool_sidecar_memo", None)
    monkeypatch.setattr(measure, "_subagent_pool_memo", {"key": None, "ts": 0.0, "payload": None})
    measure.SNAPSHOT_DIR.mkdir()
    (measure.SNAPSHOT_DIR / "subagent_pool.json").write_text(json.dumps({
        "30.0|0.8369|claude|anthropic|True": {
            "actual_usd": 1000.0, "counterfactual_usd": 0.0,
            "transformation_usd": -1000.0, "sessions": 99,
            "_cached_ts": time.time(),
        }
    }), encoding="utf-8")

    result = measure._subagent_pool_savings(
        baseline_opus_share=0.8369, days=30, tier="anthropic")
    assert result["transformation_usd"] == 0.0
    assert result["sessions"] == 0
