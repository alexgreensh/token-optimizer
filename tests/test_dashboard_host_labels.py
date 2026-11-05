"""Cursor and Antigravity dashboards must not show Claude-only surfaces.

Those runtimes have no CLAUDE.md or MEMORY.md, no /token-optimizer audit
command and no ~/.claude folder. Hermes got its own labels in v5.13.63; this
covers the leftovers on the other two: the CLAUDE.md, MEMORY.md and Commands
rows on Overview, the "/token-optimizer" pointers, and the Manage tab note
about ~/.claude archive folders. Claude and Codex must keep their rows.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "skills/token-optimizer/assets/dashboard.html"


def _components(runtime):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required to execute the dashboard renderer")
    html = TEMPLATE.read_text(encoding="utf-8")
    start = html.index("function isNoInstructionHost(")
    end = html.index("/* ========== DASHBOARD CONTROLLER")
    prefix = (
        "const esc = x => String(x == null ? '' : x);\n"
        "const fn = x => String(x);\n"
        "const codexMemPartialPrefix = () => '';\n"
    )
    script = (
        prefix + html[start:end]
        + "\nconst data = JSON.parse(process.argv[2]);"
        + "\nprocess.stdout.write(JSON.stringify(getComponentDetails(data).map(c => c.key)));"
    )
    payload = {
        "runtime": runtime,
        "runtime_label": runtime.title(),
        "snapshot": {"components": {}, "component_details": {}},
    }
    result = subprocess.run(
        [node, "-", json.dumps(payload)],
        input=script, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("runtime", ["cursor", "antigravity"])
def test_no_claude_rows_on_cursor_and_antigravity(runtime):
    keys = _components(runtime)
    assert keys == ["skills", "mcp_tools", "core_system"]


def test_claude_keeps_its_instruction_memory_and_command_rows():
    keys = _components("claude")
    for k in ("claude_md", "memory_md", "commands"):
        assert k in keys


def test_codex_keeps_agents_md_row():
    keys = _components("codex")
    assert "agents_md" in keys and "claude_md" not in keys


def test_template_scopes_host_branch_to_cursor_and_antigravity():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "data.runtime === 'cursor' || data.runtime === 'antigravity'" in html
    # Claude and Codex text stays reachable.
    assert "run /token-optimizer" in html
    assert "~/.claude/_backups/skills-archived-DATE/" in html
    # The Manage skill and MCP controls are hidden for these runtimes.
    assert "!m.read_only && !isNoInstructionHost(data)" in html
    # Fallback label for old payloads without snapshot.instruction_label.
    assert "'agent instructions'" in html
