"""The fallback dashboard-launcher.cmd is written as UTF-8, but cmd.exe reads
a batch file in the console (OEM) code page. A profile path like
C:\\Users\\José came out garbled, the shim logged "daemon script missing" and the
dashboard daemon never started. The shim now switches cmd to UTF-8 on a plain
ASCII line before any path appears, and quotes the path it echoes inside a
( ... ) block, where an unquoted ")" or "&" broke the batch parse.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "token-optimizer" / "scripts"))


@pytest.fixture
def m(monkeypatch, tmp_path):
    (tmp_path / "claude").mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    import measure
    return measure


def test_utf8_code_page_is_set_before_any_path(m):
    text = m._generate_windows_launcher_cmd(
        "C:\\Users\\Jos\u00e9 (R&D)\\.claude\\token-optimizer\\daemon.py",
        "C:\\Users\\Jos\u00e9 (R&D)\\.claude\\token-optimizer\\logs")
    lines = text.encode("utf-8").split(b"\r\n")
    chcp = [i for i, line in enumerate(lines) if line.strip().lower().startswith(b"chcp 65001")]
    assert chcp, "no UTF-8 code page switch"
    first_non_ascii = next(i for i, line in enumerate(lines) if any(b > 127 for b in line))
    assert chcp[0] < first_non_ascii
    assert all(b < 128 for line in lines[:chcp[0] + 1] for b in line)


def test_echoed_path_inside_the_block_is_quoted(m):
    text = m._generate_windows_launcher_cmd("C:\\a (x)\\daemon.py", "C:\\a (x)\\logs")
    missing = [line for line in text.split("\r\n") if "daemon script missing" in line]
    assert missing and '"%DAEMON_SCRIPT%"' in missing[0], missing
