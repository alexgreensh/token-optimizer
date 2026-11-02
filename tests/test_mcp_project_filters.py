#!/usr/bin/env python3
"""Regression tests for project-scoped MCP disable filters and honest
labelling of what the MCP estimate covers (#301).

Reported against v5.13.63: the static report ignored project-scoped
`enabledPlugins: false` (.claude/settings.local.json) and the per-project
`disabledMcpServers` list in ~/.claude.json, so turning a server or plugin off
for one project left the after-change total unchanged. And the MCP line
bundled no server instructions while the wording read like full overhead --
instructions arrive at handshake time (capped at 2 KB of text), live in no
local file, and must be labelled NOT MEASURED, never estimated.

Run directly:  python3 -m pytest tests/test_mcp_project_filters.py -q
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


def _write_json(path, blob):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(blob, indent=2), encoding="utf-8")
    return path


def _rig(tmp_path, claude_json=None):
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)
    if claude_json is not None:
        _write_json(home / ".claude.json", claude_json)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    return home, claude_dir, cwd


def _install_plugin(claude_dir, plugin_key, mcp_blob):
    name = plugin_key.split("@")[0]
    install_path = claude_dir / "plugins" / "cache" / name / "1.0.0"
    install_path.mkdir(parents=True, exist_ok=True)
    _write_json(install_path / ".mcp.json", mcp_blob)
    registry = claude_dir / "plugins" / "installed_plugins.json"
    data = json.loads(registry.read_text()) if registry.exists() else {"version": 2, "plugins": {}}
    data["plugins"][plugin_key] = [{"scope": "user", "installPath": str(install_path)}]
    _write_json(registry, data)
    return install_path


# ---------- per-project disabledMcpServers ----------

def test_disabled_for_cwd_project_excluded(tmp_path):
    proj = tmp_path / "proj"
    home, claude_dir, cwd = _rig(tmp_path, {
        "mcpServers": {"github": {"command": "npx"}},
        "projects": {
            str(proj): {"mcpServers": {"localdb": {"command": "db"}}, "disabledMcpServers": ["github"]},
        },
    })
    assert cwd == proj
    result = measure.count_mcp_tools_and_servers(home=home, claude_dir=claude_dir, cwd=cwd)
    assert "github" not in result["server_names"]
    assert "localdb" in result["server_names"]
    assert result["disabled_for_project"] == ["github"]


def test_disabled_elsewhere_still_counted(tmp_path):
    """A server disabled for project A is still loaded in project B."""
    home, claude_dir, cwd = _rig(tmp_path, {
        "mcpServers": {"github": {"command": "npx"}},
        "projects": {
            "/some/other/project": {"disabledMcpServers": ["github"]},
        },
    })
    result = measure.count_mcp_tools_and_servers(home=home, claude_dir=claude_dir, cwd=cwd)
    assert "github" in result["server_names"]
    assert result["disabled_for_project"] == []


def test_disabled_from_subdirectory_of_project(tmp_path):
    """Running from a subdirectory of the project still honours its disable list."""
    proj = tmp_path / "proj"
    home, claude_dir, cwd = _rig(tmp_path, {
        "mcpServers": {"github": {"command": "npx"}},
        "projects": {str(proj): {"disabledMcpServers": ["github"]}},
    })
    nested = cwd / "src" / "pkg"
    nested.mkdir(parents=True)
    result = measure.count_mcp_tools_and_servers(home=home, claude_dir=claude_dir, cwd=nested)
    assert "github" not in result["server_names"]
    assert result["disabled_for_project"] == ["github"]


def test_windows_spelled_paths_match(tmp_path):
    """Windows path keys match regardless of separator and drive-letter case."""
    home, claude_dir, cwd = _rig(tmp_path, {
        "mcpServers": {"github": {"command": "npx"}},
        "projects": {"C:\\Users\\Tester\\Proj": {"disabledMcpServers": ["github"]}},
    })
    result = measure.count_mcp_tools_and_servers(
        home=home, claude_dir=claude_dir, cwd="c:/Users/tester/Proj")
    assert "github" not in result["server_names"]


# ---------- project-scoped enabledPlugins: false ----------

def test_plugin_disabled_in_settings_local_skipped(tmp_path):
    home, claude_dir, cwd = _rig(tmp_path)
    _install_plugin(claude_dir, "playwright@official", {"mcpServers": {"playwright": {"command": "npx"}}})
    _write_json(cwd / ".claude" / "settings.local.json", {"enabledPlugins": {"playwright@official": False}})
    result = measure.count_mcp_tools_and_servers(home=home, claude_dir=claude_dir, cwd=cwd)
    assert "playwright" not in result["server_names"]


def test_plugin_disabled_only_for_other_project_still_counted(tmp_path):
    home, claude_dir, cwd = _rig(tmp_path)
    _install_plugin(claude_dir, "playwright@official", {"mcpServers": {"playwright": {"command": "npx"}}})
    other = tmp_path / "other"
    _write_json(other / ".claude" / "settings.local.json", {"enabledPlugins": {"playwright@official": False}})
    result = measure.count_mcp_tools_and_servers(home=home, claude_dir=claude_dir, cwd=cwd)
    assert "playwright" in result["server_names"]


def test_project_override_reenables_user_disabled_plugin(tmp_path):
    """Closest scope wins per key: user settings off, project settings on."""
    home, claude_dir, cwd = _rig(tmp_path)
    _install_plugin(claude_dir, "playwright@official", {"mcpServers": {"playwright": {"command": "npx"}}})
    _write_json(claude_dir / "settings.json", {"enabledPlugins": {"playwright@official": False}})
    _write_json(cwd / ".claude" / "settings.local.json", {"enabledPlugins": {"playwright@official": True}})
    result = measure.count_mcp_tools_and_servers(home=home, claude_dir=claude_dir, cwd=cwd)
    assert "playwright" in result["server_names"]


# ---------- honest labelling ----------

def test_server_instructions_not_measured(tmp_path):
    home, claude_dir, cwd = _rig(tmp_path, {"mcpServers": {"github": {"command": "npx"}}})
    result = measure.count_mcp_tools_and_servers(home=home, claude_dir=claude_dir, cwd=cwd)
    instructions = result["server_instructions"]
    assert instructions["status"] == "NOT MEASURED"
    assert "2 KB" in instructions["note"]
    # The estimate itself must stay the tool surface only.
    assert result["tokens"] == result["tool_count_estimate"] * measure.TOKENS_PER_DEFERRED_TOOL
    assert "tool surface only" in result["note"]


def test_tokens_drop_when_server_disabled(tmp_path):
    """The measurable complaint: disabling a server for this project lowers the total."""
    blob = {
        "mcpServers": {"github": {"command": "npx"}, "slack": {"command": "npx"}},
        "projects": {str(tmp_path / "proj"): {"disabledMcpServers": ["github"]}},
    }
    home, claude_dir, cwd = _rig(tmp_path, blob)
    before = measure.count_mcp_tools_and_servers(home=home, claude_dir=claude_dir, cwd=tmp_path)
    after = measure.count_mcp_tools_and_servers(home=home, claude_dir=claude_dir, cwd=cwd)
    assert after["tokens"] < before["tokens"]


def test_git_bash_spelled_cwd_matches_windows_key(tmp_path):
    """A Git Bash cwd (/c/Users/...) matches a Windows project key."""
    home, claude_dir, cwd = _rig(tmp_path, {
        "mcpServers": {"github": {"command": "npx"}},
        "projects": {"C:\\Users\\Tester\\Proj": {"disabledMcpServers": ["github"]}},
    })
    result = measure.count_mcp_tools_and_servers(
        home=home, claude_dir=claude_dir, cwd="/c/Users/Tester/Proj")
    assert "github" not in result["server_names"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX case-distinct check")
def test_posix_case_distinct_projects_not_conflated(tmp_path):
    """On POSIX, /Home/Proj and /home/proj are different projects; only the
    exact case match may contribute a disable list."""
    home, claude_dir, cwd = _rig(tmp_path, {
        "mcpServers": {"github": {"command": "npx"}},
        "projects": {str(tmp_path / "PROJ"): {"disabledMcpServers": ["github"]}},
    })
    result = measure.count_mcp_tools_and_servers(home=home, claude_dir=claude_dir, cwd=cwd)
    assert "github" in result["server_names"]
