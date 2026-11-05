#!/usr/bin/env python3
"""Regression tests for the MCP server counter in measure.py.

The pre-fix counter read only ~/.claude/settings.json (which carries no
mcpServers block on a normal install) and the Claude DESKTOP app config, so it
reported 0 servers on machines running a dozen of them. That zero fed the
structural-savings math, making the deferred-vs-eager MCP saving invisible.

These tests pin the three real sources:
  1. ~/.claude.json -- top-level mcpServers AND every projects.<path>.mcpServers
     block (deduped by server name: one server, many project keys, counted once)
  2. plugin-provided servers, via ~/.claude/plugins/installed_plugins.json
     (+ the enabledPlugins filter in settings.json)
  3. account-level connectors -- NOT MEASURED, never fabricated

plus the degradation contract: a missing file is silent, a malformed file is a
recorded SKIP, and neither crashes the measurement.

Run directly:  python3 -m pytest tests/test_mcp_counter.py -q

Set TOKOPT_MEASURE_SCRIPTS to a directory holding a different measure.py to run
this suite against another build (used to demonstrate the RED run against the
pre-fix implementation).
"""

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = Path(os.environ.get("TOKOPT_MEASURE_SCRIPTS") or (REPO / "skills" / "token-optimizer" / "scripts"))
sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402


# ---------- fixture builders (every byte lands in a real file on disk) ----------

def _write_json(path, blob):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(blob, indent=2), encoding="utf-8")
    return path


def _make_home(tmp_path, claude_json=None):
    """A fake $HOME, optionally holding a ~/.claude.json."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    if claude_json is not None:
        _write_json(home / ".claude.json", claude_json)
    return home


def _make_claude_dir(tmp_path, settings=None):
    """A fake ~/.claude, optionally holding a settings.json."""
    claude_dir = tmp_path / "home" / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    if settings is not None:
        _write_json(claude_dir / "settings.json", settings)
    return claude_dir


def _install_plugin(claude_dir, plugin_key, mcp_blob, filename=".mcp.json"):
    """Install a plugin that ships `mcp_blob` and register it in the registry."""
    name = plugin_key.split("@")[0]
    install_path = claude_dir / "plugins" / "cache" / name / "1.0.0"
    install_path.mkdir(parents=True, exist_ok=True)
    if mcp_blob is not None:
        _write_json(install_path / filename, mcp_blob)

    registry = claude_dir / "plugins" / "installed_plugins.json"
    data = json.loads(registry.read_text()) if registry.exists() else {"version": 2, "plugins": {}}
    data["plugins"][plugin_key] = [{"scope": "user", "installPath": str(install_path)}]
    _write_json(registry, data)
    return install_path


def _point_measure_at(monkeypatch, home, claude_dir, cwd):
    """Repoint the production zero-arg call path at the fixture tree."""
    monkeypatch.setattr(measure, "HOME", Path(home))
    monkeypatch.setattr(measure, "CLAUDE_DIR", Path(claude_dir))
    cwd = Path(cwd)
    cwd.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(cwd)


# ---------- source 1: ~/.claude.json, top level + per-project blocks ----------

def test_counts_top_level_and_dedups_across_project_blocks(tmp_path, monkeypatch):
    """The headline bug: servers in ~/.claude.json must be counted, once each.

    `beta` is declared under two different projects.<path> keys and `alpha`
    under both the top level and a project block -- each must land in the count
    exactly once.
    """
    home = _make_home(tmp_path, {
        "mcpServers": {"alpha": {"command": "alpha-server"}},
        "projects": {
            "/Users/x/proj-one": {"mcpServers": {"alpha": {"command": "a"},
                                                 "beta": {"command": "b"}}},
            "/Users/x/proj-two": {"mcpServers": {"beta": {"command": "b"},
                                                 "gamma": {"url": "https://g"}}},
            "/Users/x/proj-none": {"allowedTools": [], "history": []},
        },
    })
    claude_dir = _make_claude_dir(tmp_path, {"cleanupPeriodDays": 30})
    _point_measure_at(monkeypatch, home, claude_dir, tmp_path / "cwd")

    result = measure.count_mcp_tools_and_servers()

    assert result["server_count"] == 3, result["server_names"]
    assert sorted(result["server_names"]) == ["alpha", "beta", "gamma"]
    assert result["server_names"].count("beta") == 1
    assert result["project_blocks_with_servers"] == 2
    # Every name traces back to the file and key that declared it.
    assert result["server_sources"]["alpha"].endswith(".claude.json:mcpServers")
    assert "projects[/Users/x/proj-one].mcpServers" in result["server_sources"]["beta"]


def test_counter_is_non_zero_and_prices_tools_when_servers_exist(tmp_path, monkeypatch):
    """With servers present the counter must report non-zero servers AND tokens."""
    home = _make_home(tmp_path, {
        "mcpServers": {"tavily": {"command": "t"}},
        "projects": {"/p": {"mcpServers": {"context7": {"url": "https://c"}}}},
    })
    claude_dir = _make_claude_dir(tmp_path, {})
    _point_measure_at(monkeypatch, home, claude_dir, tmp_path / "cwd")

    result = measure.count_mcp_tools_and_servers()

    assert result["server_count"] == 2
    assert result["tool_count_estimate"] > 0
    assert result["tokens"] > 0
    assert result["loading_mode"] == "deferred"


def test_project_scoped_mcp_json_is_read(tmp_path, monkeypatch):
    """A project's own .mcp.json is a real source too — once approved.

    .mcp.json servers connect only after approval (interactive choices persist
    in ~/.claude.json projects.<path>.enabledMcpjsonServers), so an approved
    server counts with project scope; a pending one does not.
    """
    cwd = tmp_path / "cwd"
    home = _make_home(tmp_path, {
        "projects": {str(cwd): {"enabledMcpjsonServers": ["proj-server"]}},
    })
    claude_dir = _make_claude_dir(tmp_path)
    _write_json(cwd / ".mcp.json", {"mcpServers": {"proj-server": {"command": "p"},
                                                "pending-server": {"command": "q"}}})
    _point_measure_at(monkeypatch, home, claude_dir, cwd)

    result = measure.count_mcp_tools_and_servers()

    assert result["server_names"] == ["proj-server"]
    assert result["server_scopes"]["proj-server"] == "project"
    assert result["pending_project_approval"] == ["pending-server"]


# ---------- source 2: plugin-provided servers ----------

def test_enabled_plugin_servers_counted_and_disabled_ones_skipped(tmp_path, monkeypatch):
    """enabledPlugins gates plugin MCP servers; enabled ones must still count."""
    home = _make_home(tmp_path)
    claude_dir = _make_claude_dir(tmp_path, {
        "enabledPlugins": {"on@mkt": True, "off@mkt": False},
    })
    _install_plugin(claude_dir, "on@mkt", {"mcpServers": {"enabled-srv": {"url": "https://on"}}})
    _install_plugin(claude_dir, "off@mkt", {"mcpServers": {"disabled-srv": {"url": "https://off"}}})
    _point_measure_at(monkeypatch, home, claude_dir, tmp_path / "cwd")

    result = measure.count_mcp_tools_and_servers()

    assert result["server_names"] == ["enabled-srv"]
    assert result["server_scopes"]["enabled-srv"] == "plugin"
    assert "disabled-srv" not in result["server_names"]


def test_bare_shape_plugin_mcp_json_is_understood(tmp_path, monkeypatch):
    """Some plugins ship {"name": {...}} with no mcpServers wrapper.

    The official playwright plugin's .mcp.json is exactly this shape.
    """
    home = _make_home(tmp_path)
    claude_dir = _make_claude_dir(tmp_path)
    _install_plugin(claude_dir, "playwright@official",
                    {"playwright": {"command": "npx", "args": ["@playwright/mcp@latest"]}})
    _point_measure_at(monkeypatch, home, claude_dir, tmp_path / "cwd")

    result = measure.count_mcp_tools_and_servers()

    assert result["server_names"] == ["playwright"]


def test_plugin_json_without_mcp_servers_declares_nothing(tmp_path, monkeypatch):
    """A plugin.json of ordinary metadata must not be mistaken for a server map."""
    home = _make_home(tmp_path)
    claude_dir = _make_claude_dir(tmp_path)
    _install_plugin(claude_dir, "plain@mkt",
                    {"name": "plain", "version": "1.0.0", "commands": ["a", "b"]},
                    filename="plugin.json")
    _point_measure_at(monkeypatch, home, claude_dir, tmp_path / "cwd")

    result = measure.count_mcp_tools_and_servers()

    assert result["server_count"] == 0
    assert result["sources_skipped"] == []


def test_server_in_both_project_block_and_plugin_counted_once(tmp_path, monkeypatch):
    """Cross-source dedup: context7 lives in a project block AND a plugin."""
    home = _make_home(tmp_path, {
        "projects": {"/p": {"mcpServers": {"context7": {"url": "https://c"}}}},
    })
    claude_dir = _make_claude_dir(tmp_path)
    _install_plugin(claude_dir, "context7@official",
                    {"mcpServers": {"context7": {"type": "http", "url": "https://c"}}})
    _point_measure_at(monkeypatch, home, claude_dir, tmp_path / "cwd")

    result = measure.count_mcp_tools_and_servers()

    assert result["server_count"] == 1
    assert result["server_names"] == ["context7"]


# ---------- source 3: account connectors are NOT MEASURED ----------

def test_account_connectors_reported_as_not_measured_and_never_counted(tmp_path, monkeypatch):
    """Connectors live server-side at claude.ai: label them, never guess a number."""
    home = _make_home(tmp_path, {"mcpServers": {"alpha": {"command": "a"}}})
    claude_dir = _make_claude_dir(tmp_path)
    _point_measure_at(monkeypatch, home, claude_dir, tmp_path / "cwd")

    result = measure.count_mcp_tools_and_servers()
    connectors = result["account_connectors"]

    assert connectors["status"] == "NOT MEASURED"
    assert connectors["count"] is None
    assert "claude.ai" in connectors["note"]
    # The unknown must not leak into the measured numbers.
    assert result["server_count"] == len(result["server_names"]) == 1


# ---------- empty / missing / malformed ----------

def test_empty_everything_is_zero_without_crashing(tmp_path, monkeypatch):
    """No config anywhere: a clean zero, no skips, connectors still labelled."""
    home = _make_home(tmp_path)
    claude_dir = _make_claude_dir(tmp_path)
    _point_measure_at(monkeypatch, home, claude_dir, tmp_path / "cwd")

    result = measure.count_mcp_tools_and_servers()

    assert result["server_count"] == 0
    assert result["server_names"] == []
    assert result["tokens"] == 0
    assert result["project_blocks_with_servers"] == 0
    # Missing files are silent, not skips.
    assert result["sources_skipped"] == []
    assert result["account_connectors"]["status"] == "NOT MEASURED"


def test_malformed_claude_json_degrades_to_a_skip(tmp_path, monkeypatch):
    """Truncated JSON must be recorded as a skip, and the other sources survive."""
    home = _make_home(tmp_path)
    (home / ".claude.json").write_text('{"mcpServers": {"alpha": ', encoding="utf-8")
    claude_dir = _make_claude_dir(tmp_path)
    _install_plugin(claude_dir, "on@mkt", {"mcpServers": {"plugin-srv": {"url": "https://on"}}})
    _point_measure_at(monkeypatch, home, claude_dir, tmp_path / "cwd")

    result = measure.count_mcp_tools_and_servers()  # must not raise

    skipped_paths = [s["path"] for s in result["sources_skipped"]]
    assert any(p.endswith(".claude.json") for p in skipped_paths), result["sources_skipped"]
    assert result["sources_skipped"][0]["reason"] == "JSONDecodeError"
    # A bad file degrades that source only -- the rest of the count survives.
    assert result["server_names"] == ["plugin-srv"]


def test_malformed_plugin_registry_degrades_to_a_skip(tmp_path, monkeypatch):
    """A corrupt installed_plugins.json must not take the whole count down."""
    home = _make_home(tmp_path, {"mcpServers": {"alpha": {"command": "a"}}})
    claude_dir = _make_claude_dir(tmp_path)
    registry = claude_dir / "plugins" / "installed_plugins.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text("not json at all", encoding="utf-8")
    _point_measure_at(monkeypatch, home, claude_dir, tmp_path / "cwd")

    result = measure.count_mcp_tools_and_servers()  # must not raise

    assert result["server_names"] == ["alpha"]
    assert any(s["path"].endswith("installed_plugins.json") for s in result["sources_skipped"])


def test_mcp_servers_key_of_wrong_type_declares_nothing(tmp_path, monkeypatch):
    """mcpServers present but a list, not a map -- declare nothing, do not crash."""
    home = _make_home(tmp_path, {"mcpServers": ["alpha", "beta"]})
    claude_dir = _make_claude_dir(tmp_path)
    _point_measure_at(monkeypatch, home, claude_dir, tmp_path / "cwd")

    result = measure.count_mcp_tools_and_servers()

    assert result["server_count"] == 0
    # The file WAS read (it parsed) -- we simply decline to invent servers
    # from a key of the wrong shape.
    assert any(path.endswith(".claude.json") for path in result["sources_read"])
    assert result["sources_skipped"] == []


# ---------- the path list ----------

def test_config_paths_cover_the_real_sources(tmp_path):
    """get_mcp_config_paths must list ~/.claude.json and plugin configs,
    and must NOT list the Claude Desktop app config."""
    home = _make_home(tmp_path, {"mcpServers": {}})
    claude_dir = _make_claude_dir(tmp_path, {})
    _install_plugin(claude_dir, "on@mkt", {"mcpServers": {"s": {"url": "https://s"}}})
    cwd = tmp_path / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)

    paths = [str(p) for p in measure.get_mcp_config_paths(home=home, claude_dir=claude_dir, cwd=cwd)]

    assert str(home / ".claude.json") in paths
    assert any(p.endswith(".mcp.json") and "plugins" in p for p in paths), paths
    assert not any("claude_desktop_config.json" in p for p in paths), paths
    assert len(paths) == len(set(paths)), "path list must be deduped"


def test_injection_arguments_are_honoured(tmp_path):
    """The same fixture tree read through explicit arguments, no globals touched."""
    home = _make_home(tmp_path, {
        "projects": {"/p": {"mcpServers": {"beta": {"command": "b"}}}},
    })
    claude_dir = _make_claude_dir(tmp_path)

    result = measure.count_mcp_tools_and_servers(
        home=home, claude_dir=claude_dir, cwd=tmp_path / "cwd")

    assert result["server_names"] == ["beta"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
