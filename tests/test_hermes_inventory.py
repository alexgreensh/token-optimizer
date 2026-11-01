"""Hermes dashboard inventory (#298): skills from <hermes home>/skills and MCP
servers from config.yaml, plus the context files Hermes loads every session.

Run: python3 -m pytest tests/test_hermes_inventory.py -v
"""
import importlib
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"

CONFIG = """\
model: anthropic/claude-opus-5-5
skills:
  disabled:
    - old-skill
mcp_servers:
  filesystem:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/projects"]
  remote:
    url: https://mcp.example.com/mcp
    headers:
      Authorization: "Bearer ***"
  parked:
    command: uvx
    enabled: false
"""


def _skill(root, rel, name, desc):
    d = root / rel
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n\nBody text " * 1, encoding="utf-8")


@pytest.fixture()
def hermes(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    m = importlib.import_module("measure")
    home = tmp_path / "hermes"
    skills = home / "skills"
    _skill(skills, "devops/deploy", "deploy", "Ship the app to staging.")
    _skill(skills, "mlops/axolotl", "axolotl", "Fine-tune with Axolotl.")
    _skill(skills, "misc/old-skill", "old-skill", "Disabled in config.")
    _skill(skills, ".archive/stale-one", "stale-one", "Archived by the curator.")
    _skill(skills, ".hub/cache/thing", "thing", "Hub cache, not a skill in use.")
    (home / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (home / "SOUL.md").write_text("You are a careful agent.\n" * 20, encoding="utf-8")
    (home / "memories").mkdir()
    (home / "memories" / "MEMORY.md").write_text("- fact one\n- fact two\n", encoding="utf-8")
    (home / "memories" / "USER.md").write_text("- prefers short answers\n", encoding="utf-8")
    monkeypatch.setattr(m, "runtime_home", lambda: home)
    monkeypatch.setattr(m, "detect_runtime", lambda: "hermes")
    yield m, home
    sys.modules.pop("measure", None)


def _check_config(cfg):
    servers = cfg["mcp_servers"]
    assert set(servers) == {"filesystem", "remote", "parked"}
    assert servers["parked"]["enabled"] is False
    assert servers["remote"]["url"] == "https://mcp.example.com/mcp"
    assert cfg["skills"]["disabled"] == ["old-skill"]


def test_config_reader_with_and_without_pyyaml(hermes):
    m, home = hermes
    _check_config(m._hermes_config(home))
    _check_config(m._hermes_config_fallback(CONFIG))


def test_fallback_reads_flow_list_for_disabled_skills(hermes):
    m, _ = hermes
    cfg = m._hermes_config_fallback("skills:\n  disabled: [a, 'b']\nmcp_servers:\n  x:\n    command: y\n")
    assert cfg["skills"]["disabled"] == ["a", "b"]
    assert cfg["mcp_servers"] == {"x": {"command": "y"}}


def test_skill_inventory_splits_active_archived_disabled(hermes):
    m, home = hermes
    inv = m._hermes_skill_inventory(home)
    assert [s["name"] for s in inv["active"]] == ["deploy", "axolotl"]
    assert inv["active"][0]["category"] == "devops"
    assert [s["name"] for s in inv["archived"]] == ["stale-one"]
    assert [s["name"] for s in inv["disabled"]] == ["old-skill"]
    assert all(s["tokens"] > 0 for s in inv["active"])


def test_components_count_what_hermes_loads(hermes):
    m, _ = hermes
    c = m._measure_hermes_components()
    assert c["skills"]["count"] == 2 and c["skills"]["archived_count"] == 1 and c["skills"]["disabled_count"] == 1
    assert c["mcp_tools"]["server_count"] == 2 and c["mcp_tools"]["disabled_count"] == 1
    assert c["mcp_tools"]["tokens"] == 2 * m.TOKENS_PER_DEFERRED_TOOL
    assert c["hermes_soul"]["exists"] and c["hermes_soul"]["tokens"] > 0
    assert c["hermes_memory"]["exists"] and len(c["hermes_memory"]["files"]) == 2
    # Plugin code runs in the Hermes process and is never sent to the model.
    assert c["hermes_plugin"]["tokens"] == 0


def test_manage_panel_lists_hermes_inventory_read_only(hermes):
    m, _ = hermes
    data = m._collect_management_data(components={}, trends={})
    assert data["mode"] == "hermes" and data["read_only"] is True
    assert [s["name"] for s in data["skills"]["active"]] == ["deploy", "axolotl"]
    assert [s["name"] for s in data["mcp_servers"]["active"]] == ["filesystem", "remote"]
    assert [s["name"] for s in data["mcp_servers"]["disabled"]] == ["parked"]
    # No Claude archive/restore commands: the panel must not offer to move these.
    for item in data["skills"]["active"] + data["mcp_servers"]["active"]:
        assert "archive_cmd" not in item and "disable_cmd" not in item


def test_empty_hermes_home_is_clean(hermes, tmp_path, monkeypatch):
    m, _ = hermes
    empty = tmp_path / "empty-hermes"
    empty.mkdir()
    monkeypatch.setattr(m, "runtime_home", lambda: empty)
    c = m._measure_hermes_components()
    assert c["skills"]["count"] == 0 and c["mcp_tools"]["server_count"] == 0
    assert c["hermes_soul"]["exists"] is False


def test_dashboard_has_hermes_labels_and_read_only_manage():
    html = (SCRIPTS.parent / "assets" / "dashboard.html").read_text(encoding="utf-8")
    assert "function hermesComponentDetails(c)" in html
    assert "if (data.runtime === 'hermes') return hermesComponentDetails(c);" in html
    assert "if (!isCodex && !m.read_only) {" in html
    # Runtime-specific text reads the runtime label instead of naming Claude Code.
    assert "It updates only while Claude Code is running" not in html
    assert "Core System is fixed by Claude Code." not in html
